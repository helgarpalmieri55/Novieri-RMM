"""Transcript retention: a HARD DELETE on a per-org clock (spec §0.7, §3.2,
§3.3).

**HARD DELETE, NOT A SOFT FLAG.** §0.7's trade is that a question's TEXT is kept
out of the append-only audit log precisely so it CAN be deleted; a soft-deleted
transcript would be the same permanent record with a nicer name, and would make
the whole reason `ai_message` exists as a separate table a fiction.

**PER ORG, NOT PER DEPLOYMENT.** `ai_settings.transcript_retention_days` is an
org tunable, bounded 1..365 by a CHECK, so one org keeping 90 days does not
force another to. The sweep therefore resolves the window per org rather than
taking one from configuration.

**THE FLAGS GO TOO.** `ai_injection_flag.excerpt` is the one place retrieved
text is deliberately stored (§3.3), and it is retrieved text from unauthenticated
sources. Sweeping messages while leaving excerpts would keep exactly the most
sensitive stored bytes forever.

**AND SO DOES `ai_session.title`, WHICH IS QUESTION TEXT UNDER ANOTHER NAME.**
`store.persist_turn` sets it to the first question's first 200 characters. A
session is only DELETED once it has no messages left, so a conversation that is
still in use kept its first question -- "does CFO-laptop-7 have the embargoed
acquisition patch installed" -- on `SessionOut.title` indefinitely, long after
the message carrying that same text was hard-deleted. That is the §0.7 promise
broken by a denormalized copy: the promise is about the TEXT, not about the row
it happens to sit in. The title is therefore expired on exactly the clock of the
message it was copied from, by blanking rather than by deleting the session --
deleting a conversation an administrator is still using would be a much larger
surprise than an unnamed one.

THE ANCHOR IS `seq == 0`, and it is exact rather than approximate. The title is
written on a session's FIRST turn, whose user row is always `seq 0` and is
therefore always the session's oldest message; if the sweep deleted anything
from a session it deleted that row, and if it deleted nothing that row is still
there. So "no `seq 0` survives" is precisely "the text the title was copied from
is gone". `session.created_at < cutoff` would NOT be: a session opened 40 days
ago whose first turn ran yesterday would lose its title 29 days early.

WHAT A SESSION LIST SHOWS AFTERWARDS: an empty `title`, the same value a session
that has never had a turn carries, with `created_at`/`last_turn_at`/
`message_count` untouched. `AiSessionOut.title` is `str` and never null, so no
client has to learn a new state. The console has no conversation list yet --
`ChatPanel` renders one conversation -- so nothing renders this today; when one
is built, an empty title must render as the conversation's DATE, which is the
correct rendering of a conversation whose early history has expired. §3.1 of the
spec carries that as a requirement rather than leaving it to this comment.

BOUNDARY: STRICTLY OLDER THAN THE CUTOFF. A row created exactly `days` ago
survives; the next tick deletes it. `<=` at the boundary is a day of data an
administrator was told they had.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.ai.settings import get_settings
from app.core.ai.sweep import blank_rationales
from app.core.tenancy import org_session
from app.models import Organization
from app.models.ai import AiAction, AiInjectionFlag, AiMessage, AiSession

__all__ = ["RETENTION_INTERVAL_S", "sweep_all_orgs", "sweep_org"]

logger = logging.getLogger("its.ai_admin.retention")

# Daily, matching every other retention beat in `app/worker.py`. Retention is a
# clock, not a detector: running it more often costs writes and changes nothing.
RETENTION_INTERVAL_S = 86400.0


def _deleted(result: object) -> int:
    """`rowcount` off a DELETE or an UPDATE. `Session.execute` is typed as returning a
    `Result`, which has no `rowcount`; the object a DML statement actually
    returns is a `CursorResult`, which does. Narrowed in one place rather than
    with three `cast`s at the call sites."""
    return int(getattr(result, "rowcount", 0) or 0)


async def sweep_org(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> dict[str, int]:
    """Delete this org's expired transcript rows, flags, and emptied sessions.

    EVERY STATEMENT CARRIES AN EXPLICIT `org_id` PREDICATE, in addition to RLS.
    RLS scopes an `org_session`, so a cross-org test driven through one passes
    even with the predicate deleted -- it is the ADMIN-ENGINE test that pins
    this, and this sweep is called on the admin engine by nothing in production,
    which is exactly why the predicate must be here rather than assumed.
    """
    now = now or datetime.now(UTC)
    effective = await get_settings(session, org_id)
    cutoff = now - timedelta(days=effective.transcript_retention_days)

    messages = await session.execute(
        delete(AiMessage).where(AiMessage.org_id == org_id, AiMessage.created_at < cutoff)
    )
    flags = await session.execute(
        delete(AiInjectionFlag).where(
            AiInjectionFlag.org_id == org_id, AiInjectionFlag.created_at < cutoff
        )
    )

    # A conversation with nothing left in it is not a conversation. Bounded by
    # `created_at < cutoff` as well so a session opened seconds ago -- which has
    # no messages yet, and whose first turn is still running -- is never swept
    # out from under its own turn.
    surviving = (
        select(AiMessage.session_id)
        .where(AiMessage.org_id == org_id)
        .group_by(AiMessage.session_id)
    )
    # **A SESSION WITH A SURVIVING `ai_action` CANNOT BE DELETED, AND THIS SWEEP
    # HAS TO KNOW THAT RATHER THAN DISCOVER IT.** `ai_action.session_id` is
    # `ON DELETE RESTRICT` (§5.1), so once act mode has minted a row in a
    # conversation, the `DELETE` below stops being "removes an emptied
    # conversation" and becomes `ForeignKeyViolationError` -- which
    # `sweep_all_orgs` would contain, silently, leaving that org's transcript
    # rows deleted and its sessions accumulating forever with nothing but a log
    # line to say so. RESTRICT IS NOT CONDITIONAL: a session whose actions are
    # all `rejected` is as undeletable as one that dispatched. So the excluded
    # set is every session this org has an action for, they are COUNTED
    # separately, and their transcripts are still swept -- only the empty shell
    # of the conversation survives, carrying the record of what it dispatched.
    acted = select(AiAction.session_id).where(AiAction.org_id == org_id)
    retained = list(
        (
            await session.execute(
                select(AiSession.id).where(
                    AiSession.org_id == org_id,
                    AiSession.created_at < cutoff,
                    AiSession.id.not_in(surviving),
                    AiSession.id.in_(acted),
                )
            )
        )
        .scalars()
        .all()
    )
    sessions = await session.execute(
        delete(AiSession).where(
            AiSession.org_id == org_id,
            AiSession.created_at < cutoff,
            AiSession.id.not_in(surviving),
            AiSession.id.not_in(acted),
        )
    )

    # §8.3: the model's prose expires on the TRANSCRIPT's clock; every other
    # column on `ai_action` survives forever, including on dispatched rows.
    rationales = await blank_rationales(session, org_id=org_id, cutoff=cutoff)

    # The denormalized copy of the first question, on the SURVIVORS. Run last,
    # after the emptied sessions are gone, so this counts only conversations that
    # are still there to be listed -- a session that was deleted outright did not
    # have a title blanked, it stopped existing. Same transaction as the message
    # delete, so the subquery sees the transcript as it will be committed. Rows
    # already blank are excluded so a second sweep reports 0 rather than
    # rewriting every conversation in the org every day.
    titles = await session.execute(
        update(AiSession)
        .where(
            AiSession.org_id == org_id,
            AiSession.title != "",
            AiSession.id.not_in(
                select(AiMessage.session_id).where(
                    AiMessage.org_id == org_id, AiMessage.seq == 0
                )
            ),
        )
        .values(title="")
    )

    return {
        "messages": _deleted(messages),
        "flags": _deleted(flags),
        "sessions": _deleted(sessions),
        # Reported separately from `sessions`: a blanked title and a deleted
        # conversation are different outcomes, and a sweep that stopped blanking
        # would otherwise be invisible in the counts.
        "titles": _deleted(titles),
        # Both reported for the same reason: "the sweep ran and deleted nothing"
        # and "the sweep skipped every session in the org" are different facts,
        # and only one of them is a problem.
        "rationales": rationales,
        "sessions_retained": len(retained),
    }


async def sweep_all_orgs(
    *, engine: AsyncEngine, admin_engine: AsyncEngine, now: datetime | None = None
) -> int:
    """Every org, one transaction each. One bad org does not stop the sweep --
    the same containment `health.sweep` and `security_logs.retention` use, and
    for the same reason: a single org with an unexpected row must not leave
    every other org's transcripts undeleted.

    THE GUARD IS `Exception`, NOT `SQLAlchemyError`, and the difference is not
    theoretical: `sweep_org`'s FIRST statement is `get_settings`, which resolves
    the org's `transcript_retention_days` and can fail on something that is not
    a database error at all. A guard that names one library's base class is a
    guard that contains the failures we already thought of, which is exactly the
    set that was never going to take the fleet down. `health/sweep.py::
    _for_each_org` and `ipam/reconcile.py::reconcile_sweep_all_orgs` both catch
    `Exception` for this reason; this sweep is the odd one out."""
    async with AsyncSession(admin_engine) as session:
        org_ids = list((await session.execute(select(Organization.id))).scalars())

    total = 0
    for org_id in org_ids:
        try:
            async with org_session(org_id, engine=engine) as scoped:
                counts = await sweep_org(scoped, org_id, now=now)
            # `org_session` opens the transaction and commits on exit; a commit
            # inside the block would close it under the context manager.
            #
            # `sessions_retained` is DELIBERATELY EXCLUDED: it counts rows this
            # sweep decided NOT to touch, and adding it to a total named "how
            # many rows were swept" would make a sweep that deleted nothing look
            # busy. It is reported per org for operators, not summed.
            total += sum(value for key, value in counts.items() if key != "sessions_retained")
        except Exception:  # noqa: BLE001 - contained by design, see the docstring
            logger.exception(
                "ai transcript retention failed for org %s; its cycle is skipped and "
                "every other org is still swept",
                org_id,
            )
    return total
