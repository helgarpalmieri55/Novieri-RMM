"""Per-org health tunables (`docs/modules/endpoint-health-alerting/spec.md`
§2.8, §0.9, §10).

ONE ROW PER ORG, AND IT IS OPTIONAL. Most orgs will never have one: every value
here has a documented default, so `get_settings` returns the EFFECTIVE value
(configured or default) and writes nothing. A `GET` that lazily INSERTs -- the
shape `budget.fx.get_settings` had until MSP clients plan 2026-10-26 Task 8
made it pure too -- would make a `health.read` request a write, and would
populate the table for every org that ever opened the settings screen while
changing nothing about behaviour. This follows `app/modules/sla/settings.py`,
which states the same reasoning for the same shape. Only the PUT creates the
row.

`SETTING_DEFAULTS` MUST AGREE WITH THE COLUMN `server_default`s, and a test
compares them against `information_schema` rather than trusting this file: the
defaults here are what an UNCONFIGURED org behaves as, and the column defaults
are what a CONFIGURED org gets for a field it did not send. If those two
disagree, writing one setting silently changes the other seven.

THE BOUNDS ARE CHECKED TWICE, DELIBERATELY. `SETTING_BOUNDS` refuses an
out-of-range value as a typed 400 here; migration 0042's CHECK constraints
refuse it at the database. Neither is redundant: without the service check the
API's answer to a typo is a 500 from an `IntegrityError` that names no bound,
and without the CHECK the bound survives no second caller, no worker and no
direct SQL fixup. The service layer is the authority for the message; the
database is the authority for the fact.

AUDITED WITH THE OLD VALUE AS WELL AS THE NEW (invariant #4, §11.3). "Who set
this org's flap threshold to 50" is only answerable if the previous value is in
the audit row -- the new one is already in the table.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.modules.health.errors import HealthValidationError
from app.modules.health.models import HealthOrgSettings

__all__ = [
    "SETTING_BOUNDS",
    "SETTING_DEFAULTS",
    "EffectiveHealthSettings",
    "get_settings",
    "get_settings_row",
    "update_settings",
]

logger = logging.getLogger("its.health.settings")

# Every default in §2.8/§0.8/§0.9, and the values migration 0042 wrote as
# column `server_default`s. Pinned against the live schema by
# `tests/test_health_settings.py::test_the_service_defaults_match_the_database_defaults`.
SETTING_DEFAULTS: dict[str, Any] = {
    "flap_window_s": 3600,
    "flap_threshold": 4,
    "flap_cooldown_s": 3600,
    "unavailable_clear_after_s": 86400,
    "alert_retention_days": 400,
    "escalate_after_s": 3600,
    "offline_after_s": 900,
    "approval_ttl_s": 86400,
    "default_alert_queue_id": None,
    "default_ticket_category_id": None,
    "monitoring_deep_link_template": None,
    "auto_resolve_alert_tickets": True,
}

# The same ranges migration 0042's CHECK constraints enforce. Any change here
# is a migration, not an edit: the CHECK is the authority and this is the
# message.
SETTING_BOUNDS: dict[str, tuple[int, int]] = {
    "flap_window_s": (300, 86400),
    "flap_threshold": (2, 50),
    "flap_cooldown_s": (300, 86400),
    "unavailable_clear_after_s": (3600, 2592000),
    "alert_retention_days": (90, 3650),
    "escalate_after_s": (300, 604800),
    "offline_after_s": (300, 86400),
    "approval_ttl_s": (300, 604800),
}

_MAX_DEEP_LINK_LENGTH = 500


@dataclass(frozen=True)
class EffectiveHealthSettings:
    """What this org's sweeps and predicates will actually use.

    A frozen dataclass rather than the ORM row, because the row may not exist:
    callers must not be able to tell "configured with the default" from "never
    configured", or they will start branching on it and the optionality becomes
    behaviour.
    """

    flap_window_s: int
    flap_threshold: int
    flap_cooldown_s: int
    unavailable_clear_after_s: int
    alert_retention_days: int
    escalate_after_s: int
    offline_after_s: int
    approval_ttl_s: int
    default_alert_queue_id: UUID | None
    default_ticket_category_id: UUID | None
    monitoring_deep_link_template: str | None
    auto_resolve_alert_tickets: bool


async def get_settings_row(session: AsyncSession, org_id: UUID) -> HealthOrgSettings | None:
    """This org's row, or `None` when it has never been configured.

    Org-scoped explicitly (invariant #5) even though `org_id` is the primary
    key: RLS would filter this correctly through `org_session`, but a worker or
    an admin-engine caller has no RLS, and a `SELECT` with no predicate over a
    table with one row per org returns whichever tenant's row the planner hands
    back first.
    """
    return (
        await session.execute(
            select(HealthOrgSettings).where(HealthOrgSettings.org_id == org_id)
        )
    ).scalar_one_or_none()


async def get_settings(session: AsyncSession, org_id: UUID) -> EffectiveHealthSettings:
    """The EFFECTIVE settings. Reads only -- see the module docstring."""
    row = await get_settings_row(session, org_id)
    if row is None:
        return EffectiveHealthSettings(**SETTING_DEFAULTS)
    return EffectiveHealthSettings(
        **{field: getattr(row, field) for field in SETTING_DEFAULTS}
    )


def _validate(fields: dict[str, Any]) -> None:
    if not fields:
        raise HealthValidationError(tr("no settings supplied"))
    unknown = sorted(set(fields) - set(SETTING_DEFAULTS))
    if unknown:
        # Refused, not ignored: silently dropping a key returns 200 for a typo'd
        # body having changed nothing, and audits a no-op edit.
        raise HealthValidationError(
            tr(
                "unknown health settings field(s): {unknown}; valid fields are {valid_fields}",
                unknown=unknown,
                valid_fields=sorted(SETTING_DEFAULTS),
            )
        )
    for field, (low, high) in SETTING_BOUNDS.items():
        if field not in fields:
            continue
        value = fields[field]
        if not isinstance(value, int) or isinstance(value, bool):
            raise HealthValidationError(
                tr("{field} must be an integer; got {value}", field=field, value=repr(value))
            )
        if not low <= value <= high:
            raise HealthValidationError(
                tr(
                    "{field} must be between {low} and {high}; got {value}",
                    field=field,
                    low=low,
                    high=high,
                    value=value,
                )
            )
    if fields.get("auto_resolve_alert_tickets") is not None and not isinstance(
        fields["auto_resolve_alert_tickets"], bool
    ):
        raise HealthValidationError(tr("auto_resolve_alert_tickets must be a boolean"))
    template = fields.get("monitoring_deep_link_template")
    if template is not None:
        if not isinstance(template, str) or len(template) > _MAX_DEEP_LINK_LENGTH:
            raise HealthValidationError(
                tr(
                    "monitoring_deep_link_template must be a string of at most "
                    "{max_deep_link_length} characters",
                    max_deep_link_length=_MAX_DEEP_LINK_LENGTH,
                )
            )
    for field in ("flap_threshold",):
        if field in fields and fields[field] is None:
            raise HealthValidationError(tr("{field} cannot be null", field=field))


async def _reject_cross_org_references(
    session: AsyncSession, org_id: UUID, fields: dict[str, Any]
) -> None:
    """`default_alert_queue_id` and `default_ticket_category_id` are plain FKs
    into `ticket_queue`/`ticket_category`, which carry no org-aware constraint
    of their own -- Postgres alone would let this org route its alert tickets
    into another tenant's queue. Mirrors
    `email_intake.mailboxes._resolve_category`.
    """
    from app.models import TicketCategory, TicketQueue

    if (queue_id := fields.get("default_alert_queue_id")) is not None:
        found = (
            await session.execute(
                select(TicketQueue.id).where(
                    TicketQueue.org_id == org_id, TicketQueue.id == queue_id
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise HealthValidationError(
                tr(
                    "default_alert_queue_id {queue_id} does not resolve in this org",
                    queue_id=queue_id,
                )
            )
    if (category_id := fields.get("default_ticket_category_id")) is not None:
        found = (
            await session.execute(
                select(TicketCategory.id).where(
                    TicketCategory.org_id == org_id, TicketCategory.id == category_id
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise HealthValidationError(
                tr(
                    "default_ticket_category_id {category_id} does not resolve in this org",
                    category_id=category_id,
                )
            )


async def update_settings(
    session: AsyncSession, org_id: UUID, actor_id: UUID | None, fields: dict[str, Any]
) -> HealthOrgSettings:
    """Upsert the supplied fields and audit the old and the new values.

    `ON CONFLICT DO UPDATE` on the `org_id` primary key rather than
    check-then-insert: two concurrent first-ever writes for the same org can
    both miss an in-memory pre-check before either has written, and this project
    has run two sweeps to remove that bug class. The loser UPDATEs instead of
    raising an `IntegrityError` that would abort its transaction.
    """
    _validate(fields)
    await _reject_cross_org_references(session, org_id, fields)

    previous = await get_settings(session, org_id)
    previous_values = {field: getattr(previous, field) for field in fields}

    now = datetime.now(UTC)
    await session.execute(
        pg_insert(HealthOrgSettings)
        .values(org_id=org_id, updated_by=actor_id, updated_at=now, **fields)
        .on_conflict_do_update(
            index_elements=["org_id"],
            # `updated_at` is set explicitly: the column's `onupdate=` is an
            # ORM/Core UPDATE-statement default and does NOT fire for the
            # UPDATE half of an `ON CONFLICT DO UPDATE`, so without this the row
            # would keep reporting the instant it was first created.
            set_={**fields, "updated_by": actor_id, "updated_at": now},
        )
    )
    await session.flush()

    # `populate_existing`: the upsert above is a Core statement the identity map
    # knows nothing about, so a `HealthOrgSettings` already loaded in this
    # session would otherwise come back with its stale values intact.
    row = (
        await session.execute(
            select(HealthOrgSettings)
            .where(HealthOrgSettings.org_id == org_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="health.settings_updated",
        target_type="health_org_settings",
        target_id=str(org_id),
        metadata={
            "changed": {k: _jsonable(v) for k, v in fields.items()},
            "previous": {k: _jsonable(v) for k, v in previous_values.items()},
        },
    )
    await session.flush()
    return row


def _jsonable(value: Any) -> Any:
    return str(value) if isinstance(value, UUID) else value
