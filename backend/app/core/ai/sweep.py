"""The three clocks an `ai_action` lives under (§5.2, §6.4, §8.3).

**NONE OF THEM IS A POLLER.** Each is either a beat that runs a bounded UPDATE
per org, or a hook on a path that was already writing the row it needs:

* **expiry** -- a `previewed` (or transient `proposed`) row whose `expires_at`
  has passed becomes `expired`, and writes `ai.action_expired`. Per org, with one
  org's failure contained, because `app/worker.py::_OrgCycleFailures` exists
  precisely because the opposite shipped once;
* **settle** -- a `dispatched` row follows its job to a terminal state, set BY
  THE JOB ENGINE'S OWN TERMINAL TRANSITIONS (§5.2: "set by the same result path
  that already updates `jobs`, so this layer runs no poller");
* **rationale blanking** -- the model's prose expires on the TRANSCRIPT's clock
  while every other column on the row survives forever (§8.3).

**WHY `confirm_action` DOES NOT MARK A ROW `expired` ITSELF.** §6.4's 410 path
raises, its caller's tenant transaction rolls back, and a `state = 'expired'`
written there would be rolled back with it -- leaving a transition a reader
believes in and the database never performs. THIS SWEEP is what moves the row,
and every confirm re-checks the clock, so a row sitting `previewed` past its
expiry is not confirmable in the meantime.

**WHY RETENTION BLANKS RATHER THAN DELETES.** `ai_action.session_id` is
`ON DELETE RESTRICT` (§5.1): an action that reached `dispatched` names a SIGNED
JOB THAT RAN ON SOMEBODY'S MACHINE, and that record is not deletable on the
terms a transcript is. So the transcript sweep must not delete the row, and --
the part that would otherwise be discovered in production -- it must not try to
delete the SESSION either. `retention.sweep_org` therefore skips sessions with
surviving actions, blanking their rationales instead. RESTRICT is not
conditional: a session whose actions are all `rejected` is equally undeletable.
"""

import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.models.ai import AiAction

__all__ = [
    "EXPIRY_INTERVAL_S",
    "blank_rationales",
    "expire_stale_actions",
    "settle_action_for_job",
]

logger = logging.getLogger("its.ai.sweep")

#: How often the expiry beat runs. **60 s, matching `ai_settings.action_expiry_s`'s
#: OWN LOWER BOUND** (`BETWEEN 60 AND 86400`): a sweep slower than the shortest
#: expiry an org can configure would leave rows visibly `previewed` for longer
#: than the org asked, and the confirmation screen's countdown would disagree with
#: the row it describes. It is a constant HERE rather than a literal in
#: `app/worker.py` so the beat cannot drift from the bound that justifies it.
EXPIRY_INTERVAL_S = 60.0

#: The `ai_action` states the expiry sweep may move. `proposed` is transient --
#: the preview is computed in the same transaction as the insert, so a row
#: OBSERVED in `proposed` means a crash between the two, and expiring it is the
#: only honest outcome (§5.2).
_EXPIRABLE = ("proposed", "previewed")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def expire_stale_actions(
    session: AsyncSession, *, org_id: UUID, now: datetime | None = None
) -> int:
    """Move this org's stale previews to `expired`, one audit event each.

    THE `org_id` PREDICATE IS EXPLICIT AND IN ADDITION TO RLS (§9.1). This sweep
    is driven per org from the worker, so RLS is scoping it in production -- but
    an admin-engine caller (a test, a migration fixup, a future maintenance
    script) has no RLS to lean on, and a sweep that expired every org's rows
    because it was called on the wrong engine is not a defect anybody would see
    until an administrator's confirmation vanished.

    Rows are loaded and updated individually rather than by one bulk `UPDATE`
    because §8.2 requires ONE `ai.action_expired` PER ROW carrying that row's own
    `age_s`; a bulk update returns a count and cannot say which rows it moved.
    """
    now = now or datetime.now(UTC)
    rows = list(
        (
            await session.execute(
                select(AiAction).where(
                    AiAction.org_id == org_id,
                    AiAction.state.in_(_EXPIRABLE),
                    AiAction.expires_at < now,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        was = row.state
        row.state = "expired"
        row.updated_at = now
        await session.flush()
        await audit_service.record(
            session,
            org_id=org_id,
            # **`system`, NOT `user`, AND THIS IS A RECORDED DEVIATION FROM
            # §8.2's "actor_type='user' throughout".** That sentence continues
            # "actor_id = the proposing or confirming human", and a clock has
            # neither. Attributing an expiry to the human who proposed the action
            # would put a decision they did not make under their name, in the one
            # log nobody can correct. The kernel's own `jobs.expired` is
            # `system`/`None` for the same reason.
            actor_type="system",
            actor_id=None,
            action="ai.action_expired",
            target_type="ai_action",
            target_id=str(row.id),
            metadata={
                "ai_action_id": str(row.id),
                # Age of the PROPOSAL, not time past the deadline: "how long did
                # this sit unanswered" is the question an operator asks, and it
                # is comparable across orgs whose `action_expiry_s` differ.
                "age_s": int((now - _aware(row.created_at)).total_seconds()),
                "persona": row.persona,
                "job_type": row.job_type,
                "state_before": was,
            },
        )
    return len(rows)


async def settle_action_for_job(
    session: AsyncSession,
    *,
    org_id: UUID,
    job_id: UUID,
    terminal_state: Literal["succeeded", "failed", "refused", "expired", "cancelled"],
) -> None:
    """Follow a dispatched action to its job's terminal state (§5.2).

    **THE `state == 'dispatched'` PREDICATE IS THE IDEMPOTENCE.** A job can reach
    a terminal state more than once from this layer's point of view -- a redelivery,
    a manual fixup, an expiry sweep racing a late result -- and a second write
    would move an already-settled row and, worse, would be indistinguishable in
    the row's own columns from a first one. There is no idempotency key on
    `ai_action` (§0.5) and this does not add one: the state machine is the
    mechanism, exactly as it is for a second confirm.

    **`cancelled` IS A TERMINAL STATE LIKE THE OTHERS AND IS SETTLED LIKE THEM**
    (2026-08-22-job-lifecycle-design.md, cycle 1). An operator may withdraw a
    queued job the AI layer proposed, and leaving its `ai_action` in
    `dispatched` forever would be the same lie the paragraph above names -- the
    confirmation history asserting that work is in flight on somebody's machine
    when the job was stopped before it left this process. It settles as
    `failed`, with every other non-`succeeded` outcome, because the row records
    whether the proposed action HAPPENED and a withdrawn one did not.

    NO AUDIT EVENT. §8.2's table has five `ai.action_*` events and this is not one
    of them: the job's own `jobs.succeeded`/`jobs.failed`/`jobs.refused` record
    already says what happened to the thing that ran, and `ai.action_dispatched`
    already names the job id that ties the two together. A sixth event would be a
    second copy of a fact the chain already holds.
    """
    row = (
        await session.execute(
            select(AiAction).where(
                AiAction.org_id == org_id,
                AiAction.job_id == job_id,
                AiAction.state == "dispatched",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    row.state = "settled" if terminal_state == "succeeded" else "failed"
    row.updated_at = datetime.now(UTC)
    await session.flush()


async def blank_rationales(session: AsyncSession, *, org_id: UUID, cutoff: datetime) -> int:
    """§8.3 / §13 test 23: blank `rationale` AND NOTHING ELSE.

    The rationale is model prose downstream of retrieved attacker text, so it
    carries the transcript's retention obligation -- "every copy of that text owes
    the same clock". Every OTHER column survives, **including on `dispatched`
    rows**, because a dispatched action names a signed job that ran on somebody's
    machine and "what did the AI dispatch last March" must stay answerable.

    `created_at < cutoff` IS THE DISCRIMINATOR AND IS NOT OPTIONAL. Without it
    this blanks the rationale of a proposal a human is looking at RIGHT NOW, on
    the confirmation screen, which is §6.3's "why it was suggested" region going
    blank between the preview and the click.
    """
    result = await session.execute(
        update(AiAction)
        .where(
            AiAction.org_id == org_id,
            AiAction.rationale != "",
            AiAction.created_at < cutoff,
        )
        .values(rationale="")
    )
    return int(getattr(result, "rowcount", 0) or 0)
