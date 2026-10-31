"""Profile, check and assignment configuration
(`docs/modules/endpoint-health-alerting/spec.md` §2.1-§2.3, §0.2, §0.3, §10).

`policy_version` IS SERVER-OWNED AND BUMPS ON EVERY WRITE PATH. §2.1: it
increments on ANY write to the profile or to its checks, and no API accepts it.
It is what §0.4's reconciliation sweep compares against
`device_health_policy_ack`, and what §0.8's open predicate tolerates exactly one
version of drift on -- so a renamed profile whose version did not move is a
profile §12 believes every device has already acknowledged. Every mutating
function in this module calls `bump_policy_version`, and a test asserts it for a
plain RENAME rather than only for a check edit, because a bump that lives in
`replace_checks` alone passes the check test and nothing else.

THE VALIDATION HERE IS THE ONLY THING SQL CANNOT DO. `ck_health_check_one_
threshold` enforces "exactly one of `threshold_num`/`threshold_bool`"; pairing
the RIGHT one with the check type requires the registry, and SQL cannot consult
`app/modules/health/registry.py`. The same is true of comparator legality (`gt`
is a legal member of the comparator CHECK; it is not legal on a boolean check)
and of OS availability (`posture.antivirus_healthy` pinned to `linux` would be
stored, pushed to nothing, and read as a rule that never evaluates). This module
is the whole guard for all three.

ASSIGNMENTS ARE UPSERTS AGAINST PARTIAL INDEXES. Three partial unique indexes,
one per resolution level (§0.2), because NULLs do not collide in a plain unique
constraint. `create_assignment` therefore targets them with
`index_elements=`/`index_where=` and NEVER `ON CONFLICT ON CONSTRAINT`: a
partial unique index with no backing `ADD CONSTRAINT` has no `pg_constraint` row
to name, and `ON CONSTRAINT` fails at RUNTIME with `InvalidObjectDefinition` --
which only a test that actually executes the statement can see. This is the same
shape and the same reasoning as `app/modules/sla/policies.py::create_policy`.

A CHECK DECLARES ITS ACTIONS HERE, AND REMEDIATION IS THE ASYMMETRIC ONE
(§0.10, §2.2's amendment, migration 0044). `declares_notify` and
`declares_open_ticket` default TRUE, so a spec that says nothing keeps the
behaviour every check had before the columns existed; `declares_notify: false`
is §7.3's silent-by-design check and is an explicit operator act.
`remediate_approved_action_id` defaults to None and has no boolean beside it --
declaring remediation and naming the approved action it dispatches are ONE act,
because a remediate row without an action id is a row the worker can only skip.
It is resolved ORG-SCOPED, which is what stops one org arming a check against
another org's approved action: the FK alone would not, since a Postgres
foreign-key check does not consult RLS. This is `_reject_cross_org_target`'s
problem and answer on a third column. The field is an ID rather than a key
precisely because checks are replaced as a WHOLE SET (§10): the read side
returns the same identifier, so a UI that GETs the set and PUTs it back cannot
silently disarm a remediation.

REMOVING A CHECK IS A RETIRE, NOT A DELETE, WHEN IT HAS EVER ALARMED (§2.4's
amendment). `health_alert.check_id` is `ON DELETE RESTRICT` and is part of
`uq_health_alert_open_key`, so it can be neither nullable nor dangling. The
removal path therefore clears the check's non-cleared alerts with
`cleared_reason='policy_removed'` (§0.8 clause 4) and stamps `retired_at`; a
check that never opened an alert is hard-deleted, so the ordinary "added by
mistake, removed the same minute" path leaves no permanent row consuming one of
the 32 slots.

EVERY QUERY CARRIES AN EXPLICIT `org_id` PREDICATE, in addition to RLS
(invariant #5). RLS masks an application predicate: a cross-org test driven
through `org_session` passes even when the predicate has been deleted, because
RLS rejects first. The predicates here are pinned on the ADMIN engine by
`tests/test_health_profiles.py`.
"""

import logging
import math
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from app.core.approved_actions import ApprovedAction
from app.core.audit import audit_service
from app.core.global_rows import refuse_if_still_readable, refuse_unless_every_client
from app.core.i18n import N_, tr
from app.modules.health.alerts import clear_alert
from app.modules.health.errors import (
    HealthConflictError,
    HealthNotFoundError,
    HealthUnprocessableError,
    HealthValidationError,
)
from app.modules.health.models import (
    DeviceHealthCheckState,
    DeviceHealthPolicyAck,
    HealthAlert,
    HealthCheck,
    HealthFlapState,
    HealthProfile,
    HealthProfileAssignment,
)
from app.modules.health.registry import (
    CHECK_TYPES,
    MAX_CHECKS_PER_PROFILE,
    OS_FAMILIES,
    SEVERITIES,
    threshold_column_for,
)
from app.modules.health.resolution import resolve_profile_for_device
from app.modules.health.settings import get_settings
from app.services.client_sites import ClientRefusesNewRow, named_client_for_new_rows

__all__ = [
    "PROFILE_EDITABLE_FIELDS",
    "bump_policy_version",
    "clear_alerts_for_inapplicable_checks",
    "clone_profile",
    "create_assignment",
    "create_profile",
    "delete_assignment",
    "delete_profile",
    "effective_for_seconds",
    "get_profile",
    "list_assignments",
    "list_checks",
    "list_profiles",
    "replace_checks",
    "update_profile",
]

logger = logging.getLogger("its.health.profiles")

PROFILE_EDITABLE_FIELDS = (
    "name",
    "description",
    "tick_interval_s",
    "posture_interval_s",
    "summary_interval_s",
    "active",
)

_INTERVAL_BOUNDS: dict[str, tuple[int, int]] = {
    "tick_interval_s": (60, 3600),
    "posture_interval_s": (900, 86400),
    "summary_interval_s": (300, 86400),
}

_MAX_NAME = 200
_MAX_SELECTOR = 200

# The three partial unique indexes from §0.2, keyed by resolution level.
# `index_where` must be written EXACTLY as migration 0042 wrote the predicate:
# Postgres infers the matching partial index from the column list plus the
# predicate, and a differently-shaped-but-equivalent predicate infers nothing.
_ASSIGNMENT_INDEX_BY_LEVEL: dict[str, tuple[str, list[str], str]] = {
    "device": (
        "uq_health_assignment_device",
        ["org_id", "device_id"],
        "device_id IS NOT NULL",
    ),
    "group": (
        "uq_health_assignment_group",
        ["org_id", "device_group_id"],
        "device_group_id IS NOT NULL",
    ),
    # The default: global (the org's), and per client under NULLS NOT DISTINCT
    # (migration 0231, Task 8b) -- the org's default conflicts with the org's,
    # a client's with that client's. ONE index serves both levels.
    "client": (
        "uq_health_assignment_client_org",
        ["org_id", "client_id"],
        "device_id IS NULL AND device_group_id IS NULL",
    ),
    "org": (
        "uq_health_assignment_client_org",
        ["org_id", "client_id"],
        "device_id IS NULL AND device_group_id IS NULL",
    ),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


async def get_profile(session: AsyncSession, org_id: UUID, profile_id: UUID) -> HealthProfile:
    """Org-scoped load. A profile id from another org is indistinguishable from
    a nonexistent one -- both raise `HealthNotFoundError` (404, never 403)."""
    profile = (
        await session.execute(
            select(HealthProfile).where(
                HealthProfile.org_id == org_id, HealthProfile.id == profile_id
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HealthNotFoundError(
            tr("health profile {profile_id} not found", profile_id=profile_id)
        )
    return profile


async def list_profiles(session: AsyncSession, org_id: UUID) -> list[HealthProfile]:
    return list(
        (
            await session.execute(
                select(HealthProfile)
                .where(HealthProfile.org_id == org_id)
                .order_by(HealthProfile.created_at, HealthProfile.id)
            )
        )
        .scalars()
        .all()
    )


def _validate_intervals(
    tick_interval_s: int, posture_interval_s: int, summary_interval_s: int
) -> None:
    """§0.11's three numbers, refused here as a typed 400 before Postgres
    refuses them as a 500. The cross-field rule matters as much as the ranges:
    a posture sub-tick FASTER than the fast tick is not a slow sub-tick."""
    for field, value in (
        ("tick_interval_s", tick_interval_s),
        ("posture_interval_s", posture_interval_s),
        ("summary_interval_s", summary_interval_s),
    ):
        low, high = _INTERVAL_BOUNDS[field]
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
    if posture_interval_s < tick_interval_s:
        raise HealthValidationError(
            tr(
                "posture_interval_s ({posture_interval_s}) must be at least "
                "tick_interval_s ({tick_interval_s}); the posture sub-tick is the SLOW one",
                posture_interval_s=posture_interval_s,
                tick_interval_s=tick_interval_s,
            )
        )


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise HealthValidationError(tr("name must be a non-empty string"))
    if len(name) > _MAX_NAME:
        raise HealthValidationError(
            tr("name must be at most {max_name} characters", max_name=_MAX_NAME)
        )


async def create_profile(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    *,
    name: str,
    description: str = "",
    tick_interval_s: int = 300,
    posture_interval_s: int = 3600,
    summary_interval_s: int = 1800,
    active: bool = True,
) -> HealthProfile:
    """`ON CONFLICT DO NOTHING` + `RETURNING id` on `uq_health_profile_org_name`,
    not check-then-insert: two concurrent creates of the same name can both miss
    an in-memory pre-check before either has written."""
    _validate_name(name)
    _validate_intervals(tick_interval_s, posture_interval_s, summary_interval_s)

    now = _utcnow()
    inserted_id = (
        await session.execute(
            pg_insert(HealthProfile)
            .values(
                org_id=org_id,
                name=name,
                description=description,
                tick_interval_s=tick_interval_s,
                posture_interval_s=posture_interval_s,
                summary_interval_s=summary_interval_s,
                active=active,
                policy_version=1,
                created_by=actor_id,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_health_profile_org_name")
            .returning(HealthProfile.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise HealthValidationError(
            tr("a health profile named {name} already exists in this org", name=repr(name))
        )

    profile = await get_profile(session, org_id, inserted_id)
    await _audit(
        session,
        org_id,
        actor_id,
        "health.profile_created",
        "health_profile",
        profile.id,
        {
            "name": name,
            "tick_interval_s": tick_interval_s,
            "posture_interval_s": posture_interval_s,
            "summary_interval_s": summary_interval_s,
            "active": active,
        },
    )
    return profile


async def bump_policy_version(session: AsyncSession, org_id: UUID, profile_id: UUID) -> int:
    """Increment the profile's `policy_version` and return the new value.

    An in-database `policy_version + 1` rather than a read-modify-write: two
    concurrent edits that each read 4 and each write 5 leave a device that has
    acknowledged 5 believing it is converged on a policy it never received.
    """
    new_version = (
        await session.execute(
            update(HealthProfile)
            .where(HealthProfile.org_id == org_id, HealthProfile.id == profile_id)
            .values(policy_version=HealthProfile.policy_version + 1, updated_at=_utcnow())
            .returning(HealthProfile.policy_version)
        )
    ).scalar_one_or_none()
    if new_version is None:
        raise HealthNotFoundError(
            tr("health profile {profile_id} not found", profile_id=profile_id)
        )
    return new_version


async def update_profile(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    profile_id: UUID,
    fields: dict[str, Any],
) -> HealthProfile:
    """Apply the supplied fields and bump `policy_version`.

    A key outside `PROFILE_EDITABLE_FIELDS` is REFUSED, not ignored: silently
    dropping it returns 200 for a typo'd body having changed nothing, and audits
    a no-op edit. `policy_version` is in neither list -- it is server-owned, and
    a client-supplied value would let a caller claim convergence for a fleet.
    """
    profile = await get_profile(session, org_id, profile_id)
    if not fields:
        raise HealthValidationError(tr("no fields supplied"))
    unknown = sorted(set(fields) - set(PROFILE_EDITABLE_FIELDS))
    if unknown:
        raise HealthValidationError(
            tr(
                "unknown or server-owned profile field(s): {unknown}; editable fields are "
                "{profile_editable_fields}",
                unknown=unknown,
                profile_editable_fields=list(PROFILE_EDITABLE_FIELDS),
            )
        )

    merged = {field: getattr(profile, field) for field in PROFILE_EDITABLE_FIELDS}
    merged.update(fields)
    _validate_name(str(merged["name"]))
    _validate_intervals(
        int(merged["tick_interval_s"]),
        int(merged["posture_interval_s"]),
        int(merged["summary_interval_s"]),
    )

    previous = {field: getattr(profile, field) for field in fields}
    for field, value in fields.items():
        setattr(profile, field, value)

    savepoint = await session.begin_nested()
    try:
        await session.flush()
    except IntegrityError as exc:
        await savepoint.rollback()
        if "uq_health_profile_org_name" in str(getattr(exc, "orig", exc)):
            raise HealthValidationError(
                tr(
                    "a health profile named {merged_name} already exists in this org",
                    merged_name=repr(merged["name"]),
                )
            ) from exc
        raise
    else:
        await savepoint.commit()

    if "active" in fields:
        # §0.8 clause 4 again: an INACTIVE profile is skipped by resolution
        # (§3), so deactivating one is "the device's assignment changes such
        # that the check no longer applies" wearing a different switch. Only on
        # `active`, because none of the other editable fields -- name,
        # description, the three intervals -- can change what applies.
        await clear_alerts_for_inapplicable_checks(
            session,
            org_id,
            actor_id,
            device_ids=await _devices_with_open_alerts_on_profile(
                session, org_id, profile_id
            ),
        )

    profile.policy_version = await bump_policy_version(session, org_id, profile_id)
    await _audit(
        session,
        org_id,
        actor_id,
        "health.profile_updated",
        "health_profile",
        profile_id,
        {
            "changed": {k: _jsonable(v) for k, v in fields.items()},
            "previous": {k: _jsonable(v) for k, v in previous.items()},
            "policy_version": profile.policy_version,
        },
    )
    return await get_profile(session, org_id, profile_id)


async def delete_profile(
    session: AsyncSession, org_id: UUID, actor_id: UUID | None, profile_id: UUID
) -> None:
    """Hard-delete a profile that nothing depends on, and refuse -- naming what
    is in the way -- when something does.

    TWO REFUSALS, AND EACH NAMES ITS BLOCKERS (§10, §2.4).

    1. An assignment still points at it. "In use" alone leaves the operator to
       find which of a hundred devices; the message carries the ids.
    2. An alert still references it. `health_alert.profile_id` and
       `health_alert.check_id` are both live references, and an alert is an
       incident record §12's retention sweep owns -- deleting the profile would
       either destroy that history or fail with a bare `IntegrityError`. The
       path for a profile that has alarmed is `active=False`: resolution (§3)
       only ever considers active profiles, so deactivating takes it out of
       service while leaving every alert intact, and §12's retention sweep makes
       the row deletable once its alerts age out.

    With both refusals passed, the profile's checks have no alerts by
    construction, so they are hard-deleted rather than retired.
    """
    await get_profile(session, org_id, profile_id)

    assignments = list(
        (
            await session.execute(
                select(HealthProfileAssignment.id).where(
                    HealthProfileAssignment.org_id == org_id,
                    HealthProfileAssignment.profile_id == profile_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if assignments:
        raise HealthConflictError(
            tr(
                "health profile {profile_id} is still assigned; delete these assignments "
                "first: {assignment_ids}",
                profile_id=profile_id,
                assignment_ids=[str(a) for a in assignments],
            )
        )

    alerts = list(
        (
            await session.execute(
                select(HealthAlert.id)
                .where(HealthAlert.org_id == org_id, HealthAlert.profile_id == profile_id)
                .order_by(HealthAlert.opened_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    if alerts:
        raise HealthConflictError(
            tr(
                "health profile {profile_id} has alerts and cannot be deleted; deactivate "
                "it instead (active=false). Most recent alerts: {alert_ids}",
                profile_id=profile_id,
                alert_ids=[str(a) for a in alerts],
            )
        )

    check_ids = list(
        (
            await session.execute(
                select(HealthCheck.id).where(
                    HealthCheck.org_id == org_id, HealthCheck.profile_id == profile_id
                )
            )
        )
        .scalars()
        .all()
    )
    await _delete_checks(session, org_id, check_ids)
    # Stale acks: no assignment resolves to this profile any more (checked
    # above), so an ack row naming it records a policy no device will be sent
    # again. NULL means "acknowledged a disarm" (§2.11), which is what those
    # devices are about to be told.
    await session.execute(
        update(DeviceHealthPolicyAck)
        .where(
            DeviceHealthPolicyAck.org_id == org_id,
            DeviceHealthPolicyAck.profile_id == profile_id,
        )
        .values(profile_id=None)
    )
    await session.execute(
        delete(HealthProfile).where(
            HealthProfile.org_id == org_id, HealthProfile.id == profile_id
        )
    )
    await _audit(
        session,
        org_id,
        actor_id,
        "health.profile_deleted",
        "health_profile",
        profile_id,
        {"checks_deleted": len(check_ids)},
    )


async def clone_profile(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    profile_id: UUID,
    *,
    name: str,
) -> HealthProfile:
    """Deep-copy a profile and its LIVE checks under a new name, with NO
    assignment (§0.2's stated mitigation).

    Three properties, each of which a plausible implementation gets wrong:
    the checks are copied as new rows (a clone that shared rows would make
    editing the copy edit the original); retired checks are NOT copied (a
    retired check is history, and history is never pushed to an endpoint -- §3);
    and no assignment is created (a clone that carried one would silently
    retarget the source's devices at a profile nobody has reviewed).

    The source's `policy_version` does not move: a clone is not a write to the
    source, and bumping it would make §12 re-push the source's whole device set
    for a copy nobody assigned.
    """
    source = await get_profile(session, org_id, profile_id)
    clone = await create_profile(
        session,
        org_id,
        actor_id,
        name=name,
        description=source.description,
        tick_interval_s=source.tick_interval_s,
        posture_interval_s=source.posture_interval_s,
        summary_interval_s=source.summary_interval_s,
        active=source.active,
    )

    now = _utcnow()
    for check in await list_checks(session, org_id, profile_id):
        session.add(
            HealthCheck(
                org_id=org_id,
                profile_id=clone.id,
                check_type=check.check_type,
                selector=check.selector,
                comparator=check.comparator,
                threshold_num=check.threshold_num,
                threshold_bool=check.threshold_bool,
                for_ticks=check.for_ticks,
                severity=check.severity,
                os_family=check.os_family,
                enabled=check.enabled,
                # §0.10's declaration travels with the check. A clone that
                # dropped it would silently re-arm a silent-by-design check,
                # and a clone that dropped only the remediation would read as
                # "the same profile" while answering nothing automatically.
                declares_notify=check.declares_notify,
                declares_open_ticket=check.declares_open_ticket,
                remediate_approved_action_id=check.remediate_approved_action_id,
                created_at=now,
                updated_at=now,
            )
        )
    await session.flush()

    await _audit(
        session,
        org_id,
        actor_id,
        "health.profile_cloned",
        "health_profile",
        clone.id,
        {"source_profile_id": str(profile_id), "name": name},
    )
    return clone


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


async def list_checks(
    session: AsyncSession, org_id: UUID, profile_id: UUID
) -> list[HealthCheck]:
    """The profile's LIVE checks. `retired_at IS NULL` is one of the three
    places §2.4 names as having to carry the soft-delete predicate; a retired
    check leaking into this list leaks into the effective-profile view and into
    every policy push."""
    return list(
        (
            await session.execute(
                select(HealthCheck)
                .where(
                    HealthCheck.org_id == org_id,
                    HealthCheck.profile_id == profile_id,
                    HealthCheck.retired_at.is_(None),
                )
                .order_by(HealthCheck.check_type, HealthCheck.selector)
            )
        )
        .scalars()
        .all()
    )


def effective_for_seconds(check: HealthCheck, profile: HealthProfile) -> int:
    """What `for_ticks` means in wall-clock terms ON THIS PROFILE (§0.3).

    Returned alongside `for_ticks` by the API so the UI can render "3 ticks
    (~15 min at this profile's 5-minute tick)". Changing a profile's tick
    interval therefore VISIBLY changes every check's effective duration, which
    is true, and is better surfaced than hidden behind a stored seconds value
    that silently means something new.
    """
    return check.for_ticks * profile.tick_interval_s


def _validated_check(spec: dict[str, Any], profile: HealthProfile) -> dict[str, Any]:
    """One caller-supplied check -> the column values, or `HealthValidationError`.

    Everything refused here is something the database cannot refuse: the
    threshold COLUMN matching the type's `value_kind`, comparator legality per
    kind, OS answerability, and the selector rule. `ck_health_check_one_
    threshold` is happy with `threshold_num=95` on a boolean check.
    """
    unknown = sorted(
        set(spec)
        - {
            "check_type",
            "selector",
            "comparator",
            "threshold_num",
            "threshold_bool",
            "for_ticks",
            "for_seconds",
            "severity",
            "os_family",
            "enabled",
            # §0.10's declaration (§2.2's amendment, migration 0044). The
            # remediation slot names an approved action BY ID rather than by
            # key, so this field is exactly what the read side returns: checks
            # are replaced as a WHOLE SET (§10), and a UI that GETs the set and
            # PUTs it back must not silently disarm a remediation because the
            # two sides speak different identifiers.
            "declares_notify",
            "declares_open_ticket",
            "remediate_approved_action_id",
        }
    )
    if unknown:
        raise HealthValidationError(tr("unknown check field(s): {unknown}", unknown=unknown))

    check_type = spec.get("check_type")
    if check_type not in CHECK_TYPES:
        raise HealthValidationError(
            tr(
                "check_type {check_type} is not in the closed registry (spec 4.1); adding "
                "one is a code change in both repos",
                check_type=repr(check_type),
            )
        )
    entry = CHECK_TYPES[check_type]

    selector = spec.get("selector", "") or ""
    if not isinstance(selector, str) or len(selector) > _MAX_SELECTOR:
        raise HealthValidationError(
            tr(
                "selector must be a string of at most {max_selector} chars",
                max_selector=_MAX_SELECTOR,
            )
        )
    if selector and not entry.selector_meaning:
        # Otherwise the same check is storable twice in one profile past
        # `uq_health_check_profile_type_selector_os`, and pushed twice.
        raise HealthValidationError(
            tr(
                "{check_type} takes no selector; got {selector}",
                check_type=check_type,
                selector=repr(selector),
            )
        )

    comparator = spec.get("comparator")
    if comparator not in entry.legal_comparators:
        raise HealthValidationError(
            tr(
                "comparator {comparator} is not legal for {check_type} "
                "({value_kind}); legal comparators are {legal_comparators}",
                comparator=repr(comparator),
                check_type=check_type,
                value_kind=entry.value_kind,
                legal_comparators=sorted(entry.legal_comparators),
            )
        )

    threshold_num = spec.get("threshold_num")
    threshold_bool = spec.get("threshold_bool")
    wanted = threshold_column_for(entry.value_kind)
    supplied = [
        name
        for name, value in (("threshold_num", threshold_num), ("threshold_bool", threshold_bool))
        if value is not None
    ]
    if supplied != [wanted]:
        raise HealthValidationError(
            tr(
                "{check_type} is {value_kind}: it takes exactly {wanted} and nothing "
                "else; got {supplied}",
                check_type=check_type,
                value_kind=entry.value_kind,
                wanted=wanted,
                supplied=supplied or ["neither"],
            )
        )
    if wanted == "threshold_bool" and not isinstance(threshold_bool, bool):
        raise HealthValidationError(
            tr("{check_type} requires a boolean threshold_bool", check_type=check_type)
        )

    if "for_ticks" in spec and "for_seconds" in spec:
        raise HealthValidationError(
            tr("supply for_ticks or for_seconds, not both -- two answers to one question")
        )
    if "for_seconds" in spec:
        for_seconds = spec["for_seconds"]
        if not isinstance(for_seconds, int) or isinstance(for_seconds, bool) or for_seconds <= 0:
            raise HealthValidationError(
                tr(
                    "for_seconds must be a positive integer; got {for_seconds}",
                    for_seconds=repr(for_seconds),
                )
            )
        # §0.3: ceil against THIS PROFILE's tick interval. `max(1, ...)` is
        # redundant for a positive input but states the intent: a sub-tick
        # duration is one tick, never zero.
        for_ticks = max(1, math.ceil(for_seconds / profile.tick_interval_s))
    else:
        for_ticks = spec.get("for_ticks", 3)
    if not isinstance(for_ticks, int) or isinstance(for_ticks, bool) or not 1 <= for_ticks <= 60:
        raise HealthValidationError(
            tr("for_ticks must be between 1 and 60; got {for_ticks}", for_ticks=repr(for_ticks))
        )

    severity = spec.get("severity", "warning")
    if severity not in SEVERITIES:
        raise HealthValidationError(
            tr(
                "severity must be one of {severities}; got {severity}",
                severities=list(SEVERITIES),
                severity=repr(severity),
            )
        )

    os_family = spec.get("os_family")
    if os_family is not None:
        if os_family not in OS_FAMILIES:
            raise HealthValidationError(
                tr(
                    "os_family must be one of {os_families} or null; got {os_family}",
                    os_families=list(OS_FAMILIES),
                    os_family=repr(os_family),
                )
            )
        if os_family not in entry.os_families:
            raise HealthValidationError(
                tr(
                    "os_family {os_family} cannot answer {check_type} (spec 4.1); it is "
                    "available on {available_on}. A check pinned to an OS that cannot "
                    "answer it never evaluates.",
                    os_family=repr(os_family),
                    check_type=check_type,
                    available_on=sorted(entry.os_families),
                )
            )

    enabled = spec.get("enabled", True)
    if not isinstance(enabled, bool):
        raise HealthValidationError(tr("enabled must be a boolean"))

    # §0.10. Both default TRUE, which is the behaviour every row written before
    # migration 0044 had; §7.3's silent-by-design check is `declares_notify:
    # false`, an explicit operator act. `remediate_approved_action_id` defaults
    # to None and there is no boolean beside it -- declaring remediation and
    # naming the action it dispatches are one act (invariant #1).
    declares: dict[str, Any] = {}
    for field in ("declares_notify", "declares_open_ticket"):
        value = spec.get(field, True)
        if not isinstance(value, bool):
            raise HealthValidationError(
                tr("{field} must be a boolean; got {value}", field=field, value=repr(value))
            )
        declares[field] = value

    remediate_id = spec.get("remediate_approved_action_id")
    if remediate_id is not None and not isinstance(remediate_id, UUID):
        try:
            remediate_id = UUID(str(remediate_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise HealthValidationError(
                tr(
                    "remediate_approved_action_id must be an approved action's id, or null to "
                    "declare no remediation; got {value}",
                    value=repr(spec["remediate_approved_action_id"]),
                )
            ) from exc

    return {
        "check_type": check_type,
        "selector": selector,
        "comparator": comparator,
        "threshold_num": threshold_num,
        "threshold_bool": threshold_bool,
        "for_ticks": for_ticks,
        "severity": severity,
        "os_family": os_family,
        "enabled": enabled,
        "remediate_approved_action_id": remediate_id,
        **declares,
    }


def _key(values: dict[str, Any]) -> tuple[str, str, str | None]:
    return (values["check_type"], values["selector"], values["os_family"])


async def _validate_declared_remediations(
    session: AsyncSession, org_id: UUID, validated: list[dict[str, Any]]
) -> dict[str, str]:
    """Resolve every declared `remediate_approved_action_id` IN THIS ORG, and
    return the armed checks as `{check key -> action key}` for the audit record.

    THE LOOKUP IS THE CROSS-ORG GUARD, AND IT IS THE WHOLE GUARD.
    `health_check.remediate_approved_action_id` is a plain FK, and a Postgres
    foreign-key check does not consult RLS -- so the FK alone would let one org
    arm a check against another org's approved action, which is a command path
    into a fleet that never approved it. This is the hole
    `_reject_cross_org_target` closes for `device_id`, and the same answer: an
    org-scoped resolution, so an id that belongs to someone else is
    indistinguishable from one that does not exist.

    THE `health` PERSONA IS CHECKED AT WRITE TIME (§8.2's posture: an approved
    action naming something impossible is a configuration bug that must surface
    when someone writes it, not at 03:00 with an alert already open). Without
    it, `_refuse_reason` would mark every remediate row this check ever mints
    `skipped`, and the operator would find out from a `last_error`. The action's
    `active` flag and its `script.run` version pin are deliberately NOT checked
    here: both are live state that can change after the check is written, and
    §7.1 already refuses on them by name at dispatch.
    """
    wanted = {
        values["remediate_approved_action_id"]
        for values in validated
        if values.get("remediate_approved_action_id") is not None
    }
    resolved: dict[UUID, tuple[str, list[str]]] = {}
    if wanted:
        rows = (
            await session.execute(
                select(ApprovedAction.id, ApprovedAction.key, ApprovedAction.personas).where(
                    ApprovedAction.org_id == org_id, ApprovedAction.id.in_(wanted)
                )
            )
        ).all()
        resolved = {row.id: (row.key, list(row.personas or [])) for row in rows}

    armed: dict[str, str] = {}
    for values in validated:
        action_id = values["remediate_approved_action_id"]
        if action_id is None:
            continue
        found = resolved.get(action_id)
        if found is None:
            raise HealthValidationError(
                tr(
                    "approved action {action_id} does not resolve in this org; a check can "
                    "only declare a remediation this org has already approved (spec 8.2)",
                    action_id=action_id,
                )
            )
        key, personas = found
        if "health" not in personas:
            raise HealthValidationError(
                tr(
                    "approved action {key} does not carry the 'health' persona, so every "
                    "remediation it produced would be skipped at dispatch (spec 7.1)",
                    key=repr(key),
                )
            )
        # Keyed by the check's full identity, os_family included: two checks
        # differing only in `os_family` are two DIFFERENT checks (that is what
        # `uq_health_check_profile_type_selector_os` says), and collapsing them
        # here would drop one of two armings from the audit record.
        armed[f"{values['check_type']}:{values['selector']}:{values['os_family'] or 'any'}"] = key
    return armed


async def replace_checks(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    profile_id: UUID,
    specs: list[dict[str, Any]],
) -> list[HealthCheck]:
    """Replace a profile's whole check set in ONE transaction (§10).

    Matched by `(check_type, selector, os_family)`:
      - present in both -> UPDATED IN PLACE, keeping its `check_id`. An alert's
        dedup key is `(org, device, check_id, selector)`, so delete-and-reinsert
        on every PUT would re-open every alert the profile carries, once per
        edit;
      - present only in the profile -> removed per §2.4's amendment: its
        non-cleared alerts are cleared with `cleared_reason='policy_removed'`
        and it is retired, or hard-deleted if it never alarmed;
      - present only in the request -> inserted.

    An EMPTY `specs` is legal and means "run nothing" -- §5.2's disarm.
    """
    profile = await get_profile(session, org_id, profile_id)

    validated = [_validated_check(spec, profile) for spec in specs]
    seen: set[tuple[str, str, str | None]] = set()
    for values in validated:
        key = _key(values)
        if key in seen:
            raise HealthValidationError(
                tr(
                    "duplicate check {key} in one request; the same check against the same "
                    "selector twice in one profile is a config mistake, not two checks",
                    key=key,
                )
            )
        seen.add(key)
    if len(validated) > MAX_CHECKS_PER_PROFILE:
        raise HealthValidationError(
            tr(
                "a health profile may hold at most {max_checks_per_profile} checks (spec "
                "0.11); got {count}",
                max_checks_per_profile=MAX_CHECKS_PER_PROFILE,
                count=len(validated),
            )
        )
    armed = await _validate_declared_remediations(session, org_id, validated)

    existing = {
        (c.check_type, c.selector, c.os_family): c
        for c in await list_checks(session, org_id, profile_id)
    }

    added: list[str] = []
    removed: list[str] = []
    now = _utcnow()

    for values in validated:
        current = existing.pop(_key(values), None)
        if current is None:
            session.add(
                HealthCheck(
                    org_id=org_id, profile_id=profile_id, created_at=now, updated_at=now, **values
                )
            )
            added.append(values["check_type"])
        else:
            for field, value in values.items():
                setattr(current, field, value)
            current.updated_at = now

    if existing:
        removed = sorted(c.check_type for c in existing.values())
        await _retire_or_delete_checks(
            session, org_id, actor_id, [c.id for c in existing.values()]
        )

    await session.flush()

    # §0.8 clause 4's OTHER TWO LIMBS. `_retire_or_delete_checks` above only saw
    # the set difference; a check that is still in the profile but arrived
    # `enabled: false`, or whose `os_family` was narrowed past the devices
    # alerting on it, is not in `existing` and reached nothing. Run after the
    # flush so the resolver reads the checks as this call just wrote them.
    await clear_alerts_for_inapplicable_checks(
        session,
        org_id,
        actor_id,
        device_ids=await _devices_with_open_alerts_on_profile(session, org_id, profile_id),
        now=now,
    )

    new_version = await bump_policy_version(session, org_id, profile_id)
    await _audit(
        session,
        org_id,
        actor_id,
        "health.checks_replaced",
        "health_profile",
        profile_id,
        {
            "added": sorted(added),
            "removed": removed,
            "total": len(validated),
            "policy_version": new_version,
            # Invariant #4. Arming a check with a remediation is the act that
            # puts this profile on a signed-job path to an endpoint, so it is
            # named in the audit record rather than left to be inferred from a
            # column nobody diffed. `silent` is here for the same reason in the
            # other direction: §7.3 says a silent check is unrouted, never
            # uncounted, and "who made this check stop mailing anyone" has to
            # be answerable.
            "remediation_armed": armed,
            "silent": sorted(
                f"{v['check_type']}:{v['selector']}:{v['os_family'] or 'any'}"
                for v in validated
                if not v["declares_notify"]
            ),
        },
    )
    return await list_checks(session, org_id, profile_id)


async def _retire_or_delete_checks(
    session: AsyncSession, org_id: UUID, actor_id: UUID | None, check_ids: list[UUID]
) -> None:
    """§2.4's amendment and §0.8 clause 4, in the order they must happen.

    Clear first, then retire. Clearing without retiring leaves the check
    running; retiring without clearing leaves a permanent alert for a rule that
    no longer exists -- failure mode (a) in §0.8.

    RETIRING ALSO DISARMS: `remediate_approved_action_id` is set back to NULL.
    A retired check is excluded from resolution, from every push and from the
    effective-profile view (§2.2), so it can never open another alert and its
    declaration is dead data -- but the FK is `ON DELETE RESTRICT`, so leaving
    it would make the approved action it names PERMANENTLY undeletable, blocked
    by a row nobody can see or edit. No history is lost: what was actually
    dispatched lives in `health_alert_action` and `jobs`, and the retire itself
    is audited.
    """
    if not check_ids:
        return
    now = _utcnow()
    cleared = (
        await session.execute(
            update(HealthAlert)
            .where(
                HealthAlert.org_id == org_id,
                HealthAlert.check_id.in_(check_ids),
                HealthAlert.state != "cleared",
            )
            .values(
                state="cleared",
                cleared_at=now,
                cleared_reason="policy_removed",
                cleared_by=actor_id,
                muted_until=None,
                updated_at=now,
            )
            .returning(HealthAlert.id)
        )
    ).all()

    # A check that has EVER opened an alert keeps its row: `check_id` is
    # `ON DELETE RESTRICT` and part of the dedup key, so it can be neither
    # nullable nor dangling. One that never alarmed is hard-deleted, so the
    # ordinary "added by mistake" path leaves no permanent row in one of the 32
    # slots.
    referenced = set(
        (
            await session.execute(
                select(HealthAlert.check_id)
                .where(HealthAlert.org_id == org_id, HealthAlert.check_id.in_(check_ids))
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    to_retire = [cid for cid in check_ids if cid in referenced]
    to_delete = [cid for cid in check_ids if cid not in referenced]

    if to_retire:
        await session.execute(
            update(HealthCheck)
            .where(HealthCheck.org_id == org_id, HealthCheck.id.in_(to_retire))
            .values(
                retired_at=now,
                enabled=False,
                # See this function's docstring: a retired check is disarmed,
                # or the approved action it names can never be deleted.
                remediate_approved_action_id=None,
                updated_at=now,
            )
        )
    await _delete_checks(session, org_id, to_delete)
    if cleared:
        logger.info(
            "health: cleared %d alert(s) as policy_removed for %d retired check(s)",
            len(cleared),
            len(to_retire),
        )


async def _devices_with_open_alerts_on_profile(
    session: AsyncSession, org_id: UUID, profile_id: UUID
) -> list[UUID]:
    """Devices holding a non-cleared alert for a check that BELONGS TO
    `profile_id`, whatever profile those devices resolve to now.

    Joined through `health_check.profile_id` rather than read off
    `health_alert.profile_id`: the latter records which profile was effective
    when the alert OPENED, and the case this exists for -- a device reassigned
    to a different profile -- is precisely the case where those two disagree.
    """
    return list(
        (
            await session.execute(
                select(HealthAlert.device_id)
                .join(
                    HealthCheck,
                    (HealthCheck.org_id == HealthAlert.org_id)
                    & (HealthCheck.id == HealthAlert.check_id),
                )
                .where(
                    HealthAlert.org_id == org_id,
                    HealthAlert.state != "cleared",
                    HealthCheck.profile_id == profile_id,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )


async def clear_alerts_for_inapplicable_checks(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    *,
    device_ids: list[UUID] | None = None,
    now: datetime | None = None,
) -> int:
    """§0.8 clause 4 IN FULL. Returns the number of alerts cleared.

    The clause has three limbs -- "the check is removed from the profile,
    DISABLED, or THE DEVICE'S ASSIGNMENT CHANGES such that the check no longer
    applies" -- and only the first was implemented.
    `_retire_or_delete_checks` runs on the SET DIFFERENCE in `replace_checks`, so
    a check that is present but `enabled=false`, a check whose `os_family` was
    narrowed past its devices, a device moved to a different profile, and a
    profile deactivated with `active=false` all left their alerts open FOREVER --
    for the same reason as the offline alert (see `ingest.py`'s docstring): the
    check leaves `agent_checks`, every line naming it is discarded, and no
    ingestion clause can ever reach it again.

    ONE PREDICATE FOR ALL FOUR, AND IT IS THE RESOLVER'S OWN. Rather than a rule
    per limb, this asks `resolve_profile_for_device` what applies to the device
    NOW and clears every non-cleared alert whose `check_id` is not in the
    answer. `_check_applies` is where `enabled`, `retired_at` and both OS filters
    already live, so the four limbs cannot drift apart -- and a fifth way to stop
    a check applying is covered the day it is added to that function rather than
    the day someone remembers this one. `resolved is None` (nothing resolves at
    all) is the disarm case and clears everything, which is the same fact
    `push_policy` turns into an explicit disarm job.

    IT CLEARS THROUGH `clear_alert`, NOT A BULK UPDATE. §0.8's six clauses are
    documented as converging on that one function, which is also what resolves
    the alert's ticket (§7.4) and publishes `health.alert_cleared`; a bulk
    `UPDATE ... SET state='cleared'` moves the row and leaves the ticket open,
    which is the compounding half of the same defect.

    SCOPE. `device_ids=None` means every device in the org that currently holds
    a non-cleared alert -- bounded by open alerts rather than by fleet size, and
    used by the assignment writes, where the blast radius genuinely is the org
    (a group-level or org-default assignment moves devices this function has no
    cheaper way to enumerate). The check-write path passes the narrower set.
    """
    from app.models import Device

    now = now or _utcnow()
    query = (
        select(HealthAlert.device_id)
        .where(HealthAlert.org_id == org_id, HealthAlert.state != "cleared")
        .distinct()
    )
    if device_ids is not None:
        if not device_ids:
            return 0
        query = query.where(HealthAlert.device_id.in_(device_ids))
    targets = list((await session.execute(query)).scalars().all())
    if not targets:
        return 0

    settings = await get_settings(session, org_id)
    cleared = 0
    for device_id in targets:
        device = (
            await session.execute(
                select(Device).where(Device.org_id == org_id, Device.id == device_id)
            )
        ).scalar_one_or_none()
        if device is None:  # pragma: no cover - an alert outliving its device row
            continue
        resolved = await resolve_profile_for_device(session, org_id, device)
        # `checks`, NOT `agent_checks`: the applicable set includes the
        # server-evaluated `agent.offline`, and clearing its alert because it is
        # not in the PUSH set would be this function inventing failure mode (a)
        # for the one check that has the least defence against it.
        applicable = {check.id for check in resolved.checks} if resolved else set()
        alerts = list(
            (
                await session.execute(
                    select(HealthAlert).where(
                        HealthAlert.org_id == org_id,
                        HealthAlert.device_id == device_id,
                        HealthAlert.state != "cleared",
                    )
                )
            )
            .scalars()
            .all()
        )
        for alert in alerts:
            if alert.check_id in applicable:
                continue
            if await clear_alert(
                session,
                org_id,
                alert,
                reason="policy_removed",
                now=now,
                actor_id=actor_id,
                settings=settings,
            ):
                cleared += 1
    await session.flush()
    if cleared:
        logger.info(
            "health: cleared %d alert(s) as policy_removed for checks that no longer "
            "apply to their device (org %s)",
            cleared,
            org_id,
        )
    return cleared


async def _delete_checks(session: AsyncSession, org_id: UUID, check_ids: list[UUID]) -> None:
    """Hard-delete checks that no alert references, with their per-device state.

    `device_health_check_state` and `health_flap_state` both FK to
    `health_check`; leaving their rows behind would block the delete with a
    constraint violation the API cannot explain.
    """
    if not check_ids:
        return
    for model in (DeviceHealthCheckState, HealthFlapState):
        await session.execute(
            delete(model).where(model.org_id == org_id, model.check_id.in_(check_ids))
        )
    await session.execute(
        delete(HealthCheck).where(HealthCheck.org_id == org_id, HealthCheck.id.in_(check_ids))
    )


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


async def list_assignments(
    session: AsyncSession, org_id: UUID, *, profile_id: UUID | None = None
) -> list[HealthProfileAssignment]:
    query = select(HealthProfileAssignment).where(HealthProfileAssignment.org_id == org_id)
    if profile_id is not None:
        query = query.where(HealthProfileAssignment.profile_id == profile_id)
    return list(
        (await session.execute(query.order_by(HealthProfileAssignment.created_at)))
        .scalars()
        .all()
    )


async def _reject_cross_org_target(
    session: AsyncSession, org_id: UUID, *, device_id: UUID | None, device_group_id: UUID | None
) -> None:
    """`device_id`/`device_group_id` are plain FKs with no org-aware constraint.
    Without this, one org could assign a health profile -- and therefore a
    remediation trigger -- to another org's machine."""
    from app.models import Device, DeviceGroup

    if device_id is not None:
        found = (
            await session.execute(
                select(Device.id).where(Device.org_id == org_id, Device.id == device_id)
            )
        ).scalar_one_or_none()
        if found is None:
            raise HealthValidationError(
                tr("device_id {device_id} does not resolve in this org", device_id=device_id)
            )
    if device_group_id is not None:
        found = (
            await session.execute(
                select(DeviceGroup.id).where(
                    DeviceGroup.org_id == org_id, DeviceGroup.id == device_group_id
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise HealthValidationError(
                tr(
                    "device_group_id {device_group_id} does not resolve in this org",
                    device_group_id=device_group_id,
                )
            )


async def _client_for_default(session: AsyncSession, org_id: UUID, client_id: UUID) -> None:
    """The client a default is filed under, read FOR SHARE as the write's
    FIRST lock, before the audit chain's (`app.services.client_sites`).

    THIS IS THE RECORD CHECK (phase 3 design §5.1): the session is the one
    `require_permission("health.admin")` narrowed to the clients where the
    person holds that code, intersected with X-Client-Id -- so a client where
    they do not hold it is not visible here, and is the 404 an unknown one
    gets. Archived is a 409 and Unassigned a 422, as for every module."""
    try:
        await named_client_for_new_rows(
            session,
            org_id=org_id,
            client_id=client_id,
            unassigned=N_("a health profile cannot be assigned to Unassigned"),
        )
    except ClientRefusesNewRow as exc:
        if exc.status_code == 404:
            raise HealthNotFoundError(exc.detail) from None
        if exc.status_code == 409:
            raise HealthConflictError(exc.detail) from None
        raise HealthUnprocessableError(exc.detail) from None


async def create_assignment(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    *,
    profile_id: UUID,
    device_id: UUID | None = None,
    device_group_id: UUID | None = None,
    client_id: UUID | None = None,
) -> HealthProfileAssignment:
    """`ON CONFLICT DO NOTHING` + `RETURNING id` against the ONE partial index
    matching this assignment's resolution level -- not check-then-insert. The
    race loser gets the same typed error a pre-check would raise, and never the
    winner's row.

    `index_elements=` + `index_where=`, never `ON CONFLICT ON CONSTRAINT`: these
    are plain partial unique INDEXES with no `pg_constraint` row, so naming them
    as a constraint fails at runtime with `InvalidObjectDefinition`.

    `client_id` alone is THAT CLIENT'S DEFAULT (phase 3 design §5.1): served to
    its machines before the org's, after their device's and group's. It names
    nothing else -- a device's or a group's row takes its parent's client from
    the 0231 triggers.
    """
    if sum(target is not None for target in (device_id, device_group_id, client_id)) > 1:
        raise HealthValidationError(
            tr(
                "an assignment names a device, a group, a client or none of them (the org "
                "default), never more than one"
            )
        )
    if client_id is not None:
        await _client_for_default(session, org_id, client_id)
    elif device_id is None and device_group_id is None:
        # The org default has no parent, so no client: a global row, written by
        # the bulk INSERT below that `app.core.global_rows`'s flush guard never
        # sees. Refused before anything is read, as `require_all_clients` is.
        refuse_unless_every_client(session, "health_profile_assignment")
    await get_profile(session, org_id, profile_id)
    await _reject_cross_org_target(
        session, org_id, device_id=device_id, device_group_id=device_group_id
    )

    if device_id is not None:
        level = "device"
    elif device_group_id is not None:
        level = "group"
    else:
        level = "client" if client_id is not None else "org"
    _, index_elements, index_where = _ASSIGNMENT_INDEX_BY_LEVEL[level]

    inserted_id = (
        await session.execute(
            pg_insert(HealthProfileAssignment)
            .values(
                org_id=org_id,
                profile_id=profile_id,
                device_id=device_id,
                device_group_id=device_group_id,
                client_id=client_id,
                created_by=actor_id,
                created_at=_utcnow(),
            )
            .on_conflict_do_nothing(
                index_elements=index_elements, index_where=text(index_where)
            )
            .returning(HealthProfileAssignment.id)
        )
    ).scalar_one_or_none()

    if inserted_id is None:
        existing = await _assignment_at_level(
            session,
            org_id,
            device_id=device_id,
            device_group_id=device_group_id,
            client_id=client_id,
        )
        raise HealthValidationError(
            tr(
                "a {level}-level health profile assignment already exists here: assignment "
                "{existing_id} points at profile {existing_profile_id}",
                level=level,
                existing_id=existing.id if existing else "?",
                existing_profile_id=existing.profile_id if existing else "?",
            )
        )

    row = (
        await session.execute(
            select(HealthProfileAssignment).where(
                HealthProfileAssignment.org_id == org_id,
                HealthProfileAssignment.id == inserted_id,
            )
        )
    ).scalar_one()
    # §0.8 clause 4's third limb, and the one that actually reads "the DEVICE'S
    # ASSIGNMENT changes". A new device-level or group-level assignment
    # OVERRIDES a less specific one (§0.2's first-match-wins), so checks from
    # the profile the device used to resolve to stop applying to it -- and their
    # alerts, whose check ids the device will now never report again, would
    # otherwise be permanent. Org-wide scope because a group or org-default
    # assignment moves a set this function has no cheaper way to enumerate; it
    # is bounded by open alerts, not by fleet size.
    await clear_alerts_for_inapplicable_checks(session, org_id, actor_id)
    await _audit(
        session,
        org_id,
        actor_id,
        "health.assignment_created",
        "health_profile_assignment",
        row.id,
        {
            "profile_id": str(profile_id),
            "device_id": str(device_id) if device_id else None,
            "device_group_id": str(device_group_id) if device_group_id else None,
            "client_id": str(row.client_id) if row.client_id else None,
            "level": level,
        },
        client_id=row.client_id,
    )
    return row


async def _assignment_at_level(
    session: AsyncSession,
    org_id: UUID,
    *,
    device_id: UUID | None,
    device_group_id: UUID | None,
    client_id: UUID | None = None,
) -> HealthProfileAssignment | None:
    query = select(HealthProfileAssignment).where(HealthProfileAssignment.org_id == org_id)
    if device_id is not None:
        query = query.where(HealthProfileAssignment.device_id == device_id)
    elif device_group_id is not None:
        query = query.where(HealthProfileAssignment.device_group_id == device_group_id)
    else:
        # A default: the client's own (`client_id`), or the org's -- the
        # global row, with no client.
        same_client = (
            HealthProfileAssignment.client_id.is_(None)
            if client_id is None
            else HealthProfileAssignment.client_id == client_id
        )
        query = query.where(
            HealthProfileAssignment.device_id.is_(None),
            HealthProfileAssignment.device_group_id.is_(None),
            same_client,
        )
    return (await session.execute(query)).scalar_one_or_none()


async def delete_assignment(
    session: AsyncSession, org_id: UUID, actor_id: UUID | None, assignment_id: UUID
) -> None:
    """Remove an assignment. The device it covered falls through to the next
    resolution level, or to no profile at all -- Task 7's push then sends an
    EMPTY check list, because a disarm must be an explicit instruction."""
    deleted = (
        await session.execute(
            delete(HealthProfileAssignment)
            .where(
                HealthProfileAssignment.org_id == org_id,
                HealthProfileAssignment.id == assignment_id,
            )
            .returning(
                HealthProfileAssignment.profile_id,
                HealthProfileAssignment.device_id,
                HealthProfileAssignment.device_group_id,
                HealthProfileAssignment.client_id,
            )
        )
    ).one_or_none()
    if deleted is None:
        # A bulk DELETE, which `app.core.global_rows`'s flush guard never sees:
        # the org-default assignment (no device, no group) is a global row, and
        # RLS let a limited scope read it but made this match nothing. That is
        # a refusal (403), not a "not found".
        await refuse_if_still_readable(
            session,
            HealthProfileAssignment,
            HealthProfileAssignment.org_id == org_id,
            HealthProfileAssignment.id == assignment_id,
        )
        raise HealthNotFoundError(
            tr("health profile assignment {assignment_id} not found", assignment_id=assignment_id)
        )

    # The same clause on the way out. A device that falls through to a LESS
    # specific profile -- or to none at all -- keeps open alerts for checks that
    # no longer apply to it, and `push_policy` is about to tell the agent to
    # stop evaluating exactly those.
    await clear_alerts_for_inapplicable_checks(session, org_id, actor_id)
    await _audit(
        session,
        org_id,
        actor_id,
        "health.assignment_deleted",
        "health_profile_assignment",
        assignment_id,
        {
            "profile_id": str(deleted.profile_id),
            "device_id": str(deleted.device_id) if deleted.device_id else None,
            "device_group_id": (
                str(deleted.device_group_id) if deleted.device_group_id else None
            ),
            "client_id": str(deleted.client_id) if deleted.client_id else None,
        },
        client_id=deleted.client_id,
    )


async def count_checks(session: AsyncSession, org_id: UUID, profile_id: UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(HealthCheck)
            .where(
                HealthCheck.org_id == org_id,
                HealthCheck.profile_id == profile_id,
                HealthCheck.retired_at.is_(None),
            )
        )
    ).scalar_one()


# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    return str(value) if isinstance(value, UUID) else value


async def _audit(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    action: str,
    target_type: str,
    target_id: UUID,
    metadata: dict[str, Any],
    *,
    client_id: UUID | None = None,
) -> None:
    """`client_id` is the row's client when it has one -- an assignment's --
    and None for a profile or a check, which belong to every client."""
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        metadata=metadata,
        client_id=client_id,
    )
    await session.flush()
