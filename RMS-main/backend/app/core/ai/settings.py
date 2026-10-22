"""`ai_settings`: the effective read, and the audited write (spec §3.4, §9,
§10.6, §11.2).

**THE READ WRITES NOTHING.** `get_settings` returns the EFFECTIVE values --
configured, or the documented defaults -- and creates no row. A `GET` that
lazily INSERTs makes a read request a write, populates the table for every org
that ever opened the settings screen, and changes nothing about behaviour. This
is `app/modules/sla/settings.py`'s stated reason, and it is the same one here.

**THE FEATURE NO LONGER SHIPS OFF, AND THIS FILE NO LONGER DECIDES WHETHER IT
RUNS** (§0.14, the operator's decision of 2026-08-25). What used to live here
were two of the four gates between a fresh deployment and a working assistant:
a per-org `enabled` flag defaulting false, and a required `egress_acknowledged`
on any write that left it on. **Both are gone.** The deployment's `ITS_AI_*`
environment is now the only switch -- a control plane that has one configured
provider has the assistant for every organization on it, and one that has none
does not. Permission (`ai.use` / `ai.admin`) is untouched and still gates who may
use and who may configure.

**WHERE THE ACKNOWLEDGEMENT WENT -- IT MOVED, IT DID NOT VANISH.** The thing it
existed for is real and unchanged: tool results carry hostnames, ticket subjects
and bodies, software inventory, alert text, licence and vendor names, spend
figures and audit action names to the configured provider. The consent to that
is now the act of setting `ITS_AI_PROVIDERS` and its companions in `.env` --
deliberate, server-side, made by the person who runs the control plane rather
than by an org-admin clicking a box, and reviewable in configuration management
like every other line of that file. The console still shows the full disclosure
of what leaves and what never leaves; it shows it as information rather than as
a checkbox.

**AND THE AUDIT EVENT RECORDS THE DESTINATION INSTEAD OF THE TICKBOX.** A
`egress_acknowledged: true` that can no longer be false is a constant
masquerading as a record, so it is not written. In its place
`ai.settings_changed` carries `provider` -- the key of the provider this
deployment was configured with at the moment of the change, which is a fact that
genuinely varies between deployments and over time, and which answers the
question the tickbox was standing in for: where was this organization's data
going when this setting was written. Never the base URL, never key material.

**WHAT REMAINS ORG-CONFIGURABLE** is what was always cost and retention rather
than reach: transcript retention, the two rate ceilings, the daily token budget,
the action expiry and the per-turn proposal cap.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.errors import AiValidationError
from app.core.ai.providers import configured_provider
from app.core.audit import audit_service
from app.core.i18n import tr
from app.models.ai import AiSettings

__all__ = [
    "BOUNDS",
    "DEFAULTS",
    "EffectiveAiSettings",
    "get_settings",
    "get_settings_row",
    "update_settings",
]

logger = logging.getLogger("its.ai_admin.settings")


@dataclass(frozen=True)
class EffectiveAiSettings:
    """What this org's AI surface actually does, whether or not a row exists.

    **NO `enabled` AND NO `provider_key` SINCE §0.14.** Whether the assistant
    answers is a property of the deployment's `ITS_AI_*` configuration, not of a
    row in this table, and a field here that every read set to the same value
    would be exactly the constant-masquerading-as-configuration this build
    removed.
    """

    transcript_retention_days: int = 30
    max_turns_per_user_per_minute: int = 6
    max_turns_per_org_per_hour: int = 200
    daily_token_budget: int | None = None
    # §6.4 and §6.5, on `ai_settings` because that is where the four tunables
    # that already carry CHECK ceilings live and every write to it is already
    # `ai.admin` and already audited (recorded spec gap; both sections said
    # "org-configurable, CHECK-bounded" without saying where).
    action_expiry_s: int = 900
    max_proposals_per_turn: int = 3
    configured: bool = False


DEFAULTS = EffectiveAiSettings()

# The CHECK constraints `ai_settings` carries, restated here so a bad value is a
# 400 naming the field rather than a 500 carrying a constraint name. The DATABASE
# is still the authority -- a non-HTTP caller is bound by the CHECK even if it
# never passes through this function, which is why both exist.
BOUNDS: dict[str, tuple[int, int]] = {
    "transcript_retention_days": (1, 365),
    "max_turns_per_user_per_minute": (1, 60),
    "max_turns_per_org_per_hour": (1, 5000),
    "action_expiry_s": (60, 86400),
    "max_proposals_per_turn": (1, 10),
}

_WRITABLE = (
    "transcript_retention_days",
    "max_turns_per_user_per_minute",
    "max_turns_per_org_per_hour",
    "daily_token_budget",
    "action_expiry_s",
    "max_proposals_per_turn",
)


async def get_settings_row(session: AsyncSession, org_id: UUID) -> AiSettings | None:
    """This org's row, or `None`. Org-scoped explicitly (invariant #5): `org_id`
    is the primary key across the whole table, so without the predicate this
    returns whichever tenant's row the planner hands back first."""
    return (
        await session.execute(select(AiSettings).where(AiSettings.org_id == org_id))
    ).scalar_one_or_none()


async def get_settings(session: AsyncSession, org_id: UUID) -> EffectiveAiSettings:
    """The effective settings. **Creates no row** -- see the module header."""
    row = await get_settings_row(session, org_id)
    if row is None:
        return DEFAULTS
    return EffectiveAiSettings(
        transcript_retention_days=row.transcript_retention_days,
        max_turns_per_user_per_minute=row.max_turns_per_user_per_minute,
        max_turns_per_org_per_hour=row.max_turns_per_org_per_hour,
        daily_token_budget=row.daily_token_budget,
        action_expiry_s=row.action_expiry_s,
        max_proposals_per_turn=row.max_proposals_per_turn,
        configured=True,
    )


def _validate(values: dict[str, Any]) -> None:
    for field, (low, high) in BOUNDS.items():
        if field in values and not (low <= int(values[field]) <= high):
            raise AiValidationError(
                tr(
                    "{field} must be between {low} and {high}; got {values}",
                    field=field,
                    low=low,
                    high=high,
                    values=values[field],
                )
            )
    budget = values.get("daily_token_budget")
    if budget is not None and int(budget) <= 0:
        raise AiValidationError(tr(
            "daily_token_budget must be positive; got {budget}",
            budget=budget,
        ))


async def update_settings(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    **changes: Any,
) -> AiSettings:
    """Upsert this org's settings and write `ai.settings_changed` with OLD AND
    NEW values.

    Old values are recorded because "who set this org's retention to 365 days"
    is only answerable if the previous value is in the row.

    **NOTHING THIS FUNCTION WRITES CAN TURN THE ASSISTANT ON, OFF, OR POINT IT
    SOMEWHERE ELSE** (§0.14). Every writable field is a cost or retention dial;
    reach is server configuration. That is why the enable check and the
    `egress_acknowledged` gate that used to stand here are gone rather than
    relocated.
    """
    unknown = set(changes) - set(_WRITABLE)
    if unknown:
        raise AiValidationError(tr(
            "unknown ai settings field(s): {unknown}",
            unknown=sorted(unknown),
        ))
    _validate(changes)

    row = await get_settings_row(session, org_id)
    before = {
        field: getattr(row, field) if row is not None else getattr(DEFAULTS, field)
        for field in _WRITABLE
    }
    after = before | changes

    if row is None:
        # Check-then-insert would race two concurrent first-ever writes for one
        # org; the row is keyed on `org_id`, so the loser of that race would
        # abort its transaction with an IntegrityError. Merging into the object
        # the session already holds keeps the last writer winning instead.
        row = AiSettings(org_id=org_id)
        session.add(row)
    for field in _WRITABLE:
        setattr(row, field, after[field])
    row.updated_by = actor_id
    row.updated_at = datetime.now(UTC)
    await session.flush()

    changed = {field: value for field, value in changes.items() if before[field] != value}
    # THE DEPLOYMENT'S DESTINATION, NOT A TICKBOX (see this module's header).
    # `None` when this control plane has no provider configured, which is a real
    # and different state from "configured, and it is `local`" -- so the field is
    # written on every event rather than omitted when empty.
    provider = configured_provider()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ai.settings_changed",
        target_type="ai_settings",
        target_id=str(org_id),
        metadata={
            "fields": sorted(changed),
            "old": {field: before[field] for field in changed},
            "new": {field: changed[field] for field in changed},
            "provider": provider.key if provider is not None else None,
        },
    )
    return row
