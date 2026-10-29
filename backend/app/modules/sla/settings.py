"""The per-org auto-close setting (docs/modules/sla/spec.md §2.6, §9; Task
11's `GET/PUT /sla/settings`).

ONE ROW PER ORG, AND IT IS OPTIONAL. `sla_org_settings.org_id` is UNIQUE and
most orgs will never have a row: §7's sweep already treats "no row" as the
documented default (`sweep.DEFAULT_AUTO_CLOSE_AFTER_DAYS`, 7 days), so this
module never creates a row on a READ. A `GET` that lazily INSERTs -- the shape
`budget.fx.get_settings` had until MSP clients plan 2026-10-26 Task 8 made it
pure too -- would make an `sla.read` request a write, and
would populate the table for every org that ever opened the settings screen
while changing nothing about behaviour. `get_auto_close_after_days` therefore
returns the EFFECTIVE value (configured or default) and writes nothing; only
`set_auto_close_after_days` creates the row.

THE READ DELEGATES TO THE SWEEP'S OWN QUERY, deliberately, rather than
restating it. `sweep._auto_close_after_days` is the predicate that actually
decides which tickets get closed; if this module asked the same question with
its own SELECT and its own default, the two could drift and the API would
confidently display a number the sweep does not use. Reaching across for a
private name is the discipline `sweep.py` itself already follows with
`tracking._response_running`/`_load_policy_in_org`, and for the same stated
reason: a predicate copied into a second file is a predicate that can drift in
one of them.

AUDITED (invariant #4, spec §8: "auto-close settings changes"). The write
records the OLD and the NEW value, because "who set this org's auto-close to
365 days" is only answerable if the previous value is in the row.
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.modules.sla.errors import SlaValidationError
from app.modules.sla.models import SlaOrgSettings

# `sweep` does not import this module, so this is not a cycle -- unlike
# `calendars`/`policies`, whose recompute imports must stay function-local.
from app.modules.sla.sweep import _auto_close_after_days

__all__ = [
    "MAX_AUTO_CLOSE_AFTER_DAYS",
    "get_auto_close_after_days",
    "get_settings_row",
    "set_auto_close_after_days",
]

logger = logging.getLogger("its.sla.settings")

# Ten years. `ck_sla_org_settings_auto_close_positive` only bounds the value
# below; an unbounded upper end lets a typo (`auto_close_after_days: 70000`)
# silently disable auto-close forever, which looks identical to auto-close
# being broken. Bounded here rather than only in the Pydantic schema so a
# non-HTTP caller is bound by it too -- the service layer is the authority
# (`CLAUDE.md`: business logic lives in services, not route handlers).
MAX_AUTO_CLOSE_AFTER_DAYS = 3650


async def get_settings_row(session: AsyncSession, org_id: UUID) -> SlaOrgSettings | None:
    """This org's `sla_org_settings` row, or `None` when it has never been
    configured. Org-scoped explicitly (invariant #5): `org_id` is UNIQUE
    across the whole table, so without the predicate this returns whichever
    tenant's row the planner hands back first."""
    return (
        await session.execute(select(SlaOrgSettings).where(SlaOrgSettings.org_id == org_id))
    ).scalar_one_or_none()


async def get_auto_close_after_days(session: AsyncSession, org_id: UUID) -> int:
    """The value the SWEEP will actually use for this org -- configured, or
    `DEFAULT_AUTO_CLOSE_AFTER_DAYS`. See the module docstring for why this
    delegates instead of re-querying."""
    return await _auto_close_after_days(session, org_id)


async def set_auto_close_after_days(
    session: AsyncSession, org_id: UUID, actor_id: UUID, *, days: int
) -> SlaOrgSettings:
    """Upsert this org's auto-close window (spec §2.6, §9).

    `ON CONFLICT DO UPDATE` on the `org_id` unique constraint rather than
    check-then-insert: two concurrent first-ever writes for the same org can
    both miss an in-memory pre-check before either has written, and this
    project has run two sweeps to remove that bug class
    (`calendars.create_calendar`'s docstring). The loser UPDATEs instead of
    raising an `IntegrityError` that would abort its transaction.

    `created_by` is written on INSERT and never on UPDATE: it records who
    first configured the org, and the audit row below is what records who
    changed it since.
    """
    if days <= 0:
        raise SlaValidationError(
            tr(
                "auto_close_after_days must be a positive integer; got {days}",
                days=days,
            )
        )
    if days > MAX_AUTO_CLOSE_AFTER_DAYS:
        raise SlaValidationError(
            tr(
                "auto_close_after_days must be at most {limit}; got {days}",
                limit=MAX_AUTO_CLOSE_AFTER_DAYS,
                days=days,
            )
        )

    previous = await get_auto_close_after_days(session, org_id)

    await session.execute(
        pg_insert(SlaOrgSettings)
        .values(org_id=org_id, auto_close_after_days=days, created_by=actor_id)
        .on_conflict_do_update(
            index_elements=["org_id"],
            # `updated_at` is set explicitly: the column's `onupdate=` is an
            # ORM/Core UPDATE-statement default and does NOT fire for the
            # UPDATE half of an `ON CONFLICT DO UPDATE`, so without this the
            # row would keep reporting the instant it was first created.
            set_={"auto_close_after_days": days, "updated_at": datetime.now(UTC)},
        )
    )
    await session.flush()

    # `populate_existing`: the upsert above is a Core statement the identity
    # map knows nothing about, so a `SlaOrgSettings` already loaded in this
    # session (by a GET earlier in the same request, say) would otherwise be
    # returned with its stale `auto_close_after_days` intact.
    row = (
        await session.execute(
            select(SlaOrgSettings)
            .where(SlaOrgSettings.org_id == org_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="sla.settings_updated",
        target_type="sla_org_settings",
        target_id=str(row.id),
        metadata={"auto_close_after_days": days, "previous_effective_days": previous},
    )
    await session.flush()
    return row
