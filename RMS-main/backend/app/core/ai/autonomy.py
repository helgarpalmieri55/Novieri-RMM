"""The autonomy dial, which can only ever lower (docs/modules/ai-automation.md
§0.4, §4.1, §4.2).

`approved_action.autonomy` is the ceiling on the ACTION: this thing may run at
this level, whoever asks. `ai_autonomy_ceiling` is the ceiling on the PERSONA:
this persona may reach this far into this risk class, whatever the catalogue
says. They compose by taking the MINIMUM over an ordered vocabulary, so writing
a ceiling row can only ever REDUCE what was already permitted -- there is no
value of the dial that raises anything.

    off  <  suggest  <  require_approval  <  autonomous

`LEVELS` is one longer than what the dial can STORE. `autonomous` exists here
only as an INPUT that can be reduced away: an `approved_action.autonomy` may be
`autonomous` (the health sweep uses it), and a ceiling may not -- there is no
such value in `ck_ai_autonomy_ceiling_level` at all. The third argument to
`min_level` in `effective_autonomy` is the v1 clamp that makes the reduction
unconditional, and it is written as an ARGUMENT rather than as a branch so that
deleting it changes an observable value rather than a code path.

**A MISSING ROW MEANS `off`.** Deny by default: the layer ships with no ceiling
rows, so it ships inert, the way `ai_settings.enabled` ships `false`. A dial
that failed open would be the whole layer failing open, and the failure would be
invisible -- an org that configured nothing would silently be at the catalogue's
own level.

**RESOLVED TWICE, AND THE SECOND TIME IS NOT REDUNDANT** (§4.2): once at
proposal, so an `off` persona never mints a row, and again at confirm, because
the dial is org state an admin may change at any moment and a confirmation is a
decision about *now*.

Every read carries an EXPLICIT `org_id` predicate in addition to RLS (§9.1) --
the kernel registry's own reads do this and its comment says why: a query
leaning on RLS alone returns the wrong row the moment it runs on the admin
engine, which is where the worker sweep and any migration-time fixup run.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.errors import AiValidationError
from app.core.audit import audit_service
from app.core.i18n import tr
from app.models.ai import AI_PERSONAS, CEILING_LEVELS, AiAutonomyCeiling
from app.models.approved_actions import RISK_CLASSES

__all__ = [
    "LEVELS",
    "V1_CLAMP",
    "ceiling_for",
    "effective_autonomy",
    "get_ceilings",
    "list_ceilings",
    "min_level",
    "resolve_action_autonomy",
    "set_ceiling",
]

#: The ordered vocabulary, weakest first. `min_level` is index order over this
#: tuple and nothing else, so a reordering is a visible, single-line change.
LEVELS: tuple[str, ...] = ("off", "suggest", "require_approval", "autonomous")

#: What §4.2's third argument clamps every AI proposal to in v1.
#:
#: **A FINDING ABOUT §4.2, RECORDED RATHER THAN PAPERED OVER: GIVEN §4.1'S CHECK
#: THIS CLAMP CHANGES NO ANSWER A STORED ROW CAN PRODUCE.** `CEILING_LEVELS`
#: stops at `require_approval`, so `min_level(ceiling, autonomy)` is already at
#: most `require_approval` for every ceiling the database can hold. That was
#: verified by deleting it and running the dial's whole suite green, which is
#: why `tests/test_ai_autonomy.py` now carries a case passing a ceiling the
#: database CANNOT store -- otherwise the deletion is unobservable.
#:
#: It is kept, and the reason is written here so that somebody tidying up does
#: not delete a control whose effect is currently zero (the same argument §7.3
#: makes for taint). The database CHECK is the guarantee; this is the second
#: line, against a `ceiling_for` that ever returns a value from outside
#: `CEILING_LEVELS` -- a widened CHECK, an admin-engine fixture, a future
#: migration.
V1_CLAMP = "require_approval"


class _CatalogueRow(Protocol):
    """The three fields the resolver reads off an `approved_action`.

    A Protocol rather than the ORM class, for the reason `ScriptExemptionPin`
    gives: the resolution stays testable without a database, and real rows
    satisfy it directly.
    """

    @property
    def risk_class(self) -> str: ...

    @property
    def autonomy(self) -> str: ...

    @property
    def personas(self) -> Sequence[str]: ...


def min_level(*levels: str) -> str:
    """The weakest of `levels`, by `LEVELS` order.

    UNKNOWN MEANS `off`, NOT "IGNORE IT". A level this module does not
    recognise is a level nobody has reasoned about, and treating it as absent
    would let a future vocabulary entry read as permission. The return value is
    always a member of `LEVELS`, never the unrecognised string itself.
    """
    if not levels:
        return "off"
    return LEVELS[min(LEVELS.index(level) if level in LEVELS else 0 for level in levels)]


def effective_autonomy(*, ceiling: str, action_autonomy: str) -> str:
    """§4.2, exactly: the minimum of the dial, the catalogue row, and the v1
    clamp.

    The clamp is a THIRD ARGUMENT to `min_level` rather than a branch. With
    `ceiling='require_approval'` and `action_autonomy='autonomous'` the answer
    is `require_approval`; delete the clamp and it becomes `autonomous`, which
    is a value no AI persona may reach in v1 and which the test parametrization
    is built to observe.
    """
    return min_level(ceiling, action_autonomy, V1_CLAMP)


async def get_ceilings(session: AsyncSession, *, org_id: UUID) -> dict[tuple[str, str], str]:
    """`(persona, risk_class) -> level` for one org.

    Rows this org has not written are simply ABSENT; callers read `off` for them
    through `ceiling_for`, so this never invents a default row.
    """
    rows = (
        await session.execute(
            select(
                AiAutonomyCeiling.persona,
                AiAutonomyCeiling.risk_class,
                AiAutonomyCeiling.level,
            ).where(AiAutonomyCeiling.org_id == org_id)
        )
    ).all()
    return {(persona, risk_class): level for persona, risk_class, level in rows}


async def ceiling_for(
    session: AsyncSession, *, org_id: UUID, persona: str, risk_class: str
) -> str:
    """One org's ceiling for one persona and risk class, or `off` when absent."""
    level = (
        await session.execute(
            select(AiAutonomyCeiling.level).where(
                AiAutonomyCeiling.org_id == org_id,
                AiAutonomyCeiling.persona == persona,
                AiAutonomyCeiling.risk_class == risk_class,
            )
        )
    ).scalar_one_or_none()
    return level or "off"


async def resolve_action_autonomy(
    session: AsyncSession, *, org_id: UUID, persona: str, action: _CatalogueRow
) -> str:
    """§4.2 end to end, for one persona and one catalogue row.

    **THE PERSONA CHECK COMES FIRST AND THE DIAL IS NOT CONSULTED AT ALL**
    (§4.2's last line). A catalogue row that does not carry this persona is
    `off` whatever the org set, because the dial's job is to LOWER what the
    catalogue permits and this row permits this persona nothing. Reading the
    dial first would make an org-wide `require_approval` look like an answer
    about a row that says nothing about this persona.
    """
    if persona not in (action.personas or []):
        return "off"
    ceiling = await ceiling_for(
        session, org_id=org_id, persona=persona, risk_class=action.risk_class
    )
    return effective_autonomy(ceiling=ceiling, action_autonomy=action.autonomy)


async def set_ceiling(
    session: AsyncSession,
    *,
    org_id: UUID,
    persona: str,
    risk_class: str,
    level: str,
    actor_id: UUID | None,
) -> str | None:
    """Upsert one ceiling and audit the change. Returns the level it replaced.

    AN UPSERT ON THE REAL PRIMARY KEY, `(org_id, persona, risk_class)`. That is
    an ordinary composite PK, not a partial index, so naming the columns in
    `ON CONFLICT` is correct here -- the `index_elements=[...]` plus
    `index_where=...` dance other upserts in this repository need is for PARTIAL
    indexes, where `ON CONFLICT ON CONSTRAINT` fails at runtime because a partial
    index is not a named constraint. Said here so nobody "fixes" this one into
    that one.

    THE OLD LEVEL IS READ INSIDE THE SAME TRANSACTION and carried into the audit
    event. A change record that cannot say what it changed FROM answers nothing
    the day somebody asks why an action was proposable last Tuesday.

    THE VOCABULARY IS CHECKED HERE FOR THE 400, NOT FOR THE GUARANTEE. The
    guarantee is `ck_ai_autonomy_ceiling_level` and
    `ck_ai_autonomy_ceiling_end_user_is_off` -- database CHECKs, because §0.4
    refuses to let application code be what stops an AI persona reaching
    `autonomous`. These raise `AiValidationError` so the API answers 400 with a
    message an admin can act on instead of surfacing a constraint violation as a
    500, and both layers are tested separately.
    """
    if persona not in AI_PERSONAS:
        raise AiValidationError(tr(
            "unknown persona {persona}; expected one of {ai_personas}",
            persona=repr(persona),
            ai_personas=AI_PERSONAS,
        ))
    if risk_class not in RISK_CLASSES:
        raise AiValidationError(
            tr(
                "unknown risk class {risk_class}; expected one of {risk_classes}",
                risk_class=repr(risk_class),
                risk_classes=RISK_CLASSES,
            )
        )
    if level not in CEILING_LEVELS:
        raise AiValidationError(
            tr(
                "{level} is not a settable ceiling; expected one of {ceiling_levels}. "
                "'autonomous' is not reachable by an AI persona in v1 -- see "
                "docs/modules/ai-automation.md §0.4",
                level=repr(level),
                ceiling_levels=CEILING_LEVELS,
            )
        )

    previous = (
        await session.execute(
            select(AiAutonomyCeiling.level).where(
                AiAutonomyCeiling.org_id == org_id,
                AiAutonomyCeiling.persona == persona,
                AiAutonomyCeiling.risk_class == risk_class,
            )
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    statement = pg_insert(AiAutonomyCeiling).values(
        org_id=org_id,
        persona=persona,
        risk_class=risk_class,
        level=level,
        updated_by=actor_id,
        updated_at=now,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["org_id", "persona", "risk_class"],
            set_={"level": level, "updated_by": actor_id, "updated_at": now},
        )
    )

    # BOUNDED IDENTIFIERS ONLY (§8.2, §8.3). No prose, no rationale, nothing a
    # caller wrote: an audit row is permanent and un-redactable.
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ai.autonomy_changed",
        target_type="ai_autonomy_ceiling",
        target_id=f"{persona}/{risk_class}",
        metadata={
            "persona": persona,
            "risk_class": risk_class,
            "old_level": previous or "off",
            "new_level": level,
        },
    )
    return previous


async def list_ceilings(session: AsyncSession, *, org_id: UUID) -> list[dict[str, Any]]:
    """Every STORED ceiling for one org, ordered so the console renders stably.

    ABSENT COMBINATIONS ARE NOT FILLED IN. A response that materialised `off`
    for every unset pair would read as configuration somebody made, and the next
    admin would "tidy up" by writing them -- turning the deny-by-default default
    into rows that can be edited upward one at a time.
    """
    rows = (
        (
            await session.execute(
                select(AiAutonomyCeiling)
                .where(AiAutonomyCeiling.org_id == org_id)
                .order_by(AiAutonomyCeiling.persona, AiAutonomyCeiling.risk_class)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "persona": row.persona,
            "risk_class": row.risk_class,
            "level": row.level,
            "updated_by": row.updated_by,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]
