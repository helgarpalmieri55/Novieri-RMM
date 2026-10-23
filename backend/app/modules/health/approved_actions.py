"""The health module's service layer over the KERNEL approved-action registry
(`docs/modules/endpoint-health-alerting/spec.md` §8.2, §8.3, §8.4, §11.3).

THE REGISTRY IS NOT OWNED HERE. `app/core/approved_actions.py` holds the two
models, the closed maps, `is_destructive` and `render_params_template`, because
Phase 17's AI layer shares them (`docs/06-roadmap.md`: "one registry, one dial,
one audit"). This file is the CRUD, the validation and the audit -- the parts
that belong to the module registering into the registry rather than to the
registry itself.

FOUR PROPERTIES ARE ENFORCED AT WRITE TIME, and each closes a specific hole
(§8.2):

  - **`job_type` against the LIVE registry, AND against the approvable
    allowlist.** An approved action naming a job type that does not exist is a
    configuration bug that must surface when someone writes it, not at 03:00
    when an alert fires and the dispatcher raises `UnknownJobType` with the
    alert already open. Being registered is not enough: `APPROVABLE_JOB_TYPES`
    is deny-by-default, because a job type whose authorization lives in another
    module (ipam's scan authorization, backup's repository keys, provisioning's
    run rows, health's own policy reconciliation) is one this registry cannot
    govern -- reaching it through an approved action is a bypass, not a
    remediation.
  - **`script_id` + `script_version` pinned, and required for `script.run`
    only.** Without the version, approving a script approves its future
    contents -- `update_script` bumps `version` IN PLACE and the approved body
    is not retained anywhere (§2.10).
  - **`params_template` rendered, refused if it names a credential, AND
    validated against the job type's own params model.** Rendering catches an
    illegal brace token; only the params model catches a template the job type
    will refuse. `JobEngine.create` raises a bare `ValueError` for that, at
    dispatch, inside the sweep's per-org transaction -- which rolls back that
    whole tenant's cycle, every 30s, indefinitely, behind one log line. Same
    posture as everything else here: it must surface when someone writes it, not
    at 03:00. The credential check is invariant #3: a `params_template` is
    plaintext JSONB on a row any `health.read` holder can list, so a secret
    frozen into one is a stored credential in the application database AND a
    credential returned in an API response.
  - **`destructive` DERIVED AND STORED, never read from the request body.**
    `is_destructive` is the single derivation; a caller that could set the
    column could set `autonomous` on a `patch.apply` by writing
    `destructive: false`, and the CHECK that refuses that pairing reads the
    stored column. The same stored column is shared by every action pinning a
    script exemption, so both exemption paths re-derive ALL of them -- see
    `_actions_pinning`.

THE AUTONOMY GATE IS CHECKED TWICE AND NEITHER IS REDUNDANT. This layer refuses
`autonomous` + destructive/high/non-device as a typed 400 naming which of the
three tripped; `ck_approved_action_autonomous_is_bounded` refuses it as a
database constraint that a future persona cannot relax in Python -- it takes a
migration, which a reviewer sees. The service is the authority for the message;
the database is the authority for the fact.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.approved_actions import (
    AUTONOMY_LEVELS,
    MAX_COOLDOWN_S,
    MAX_PER_DEVICE_PER_DAY,
    MAX_PER_ORG_PER_HOUR,
    MAX_TARGET_SCOPES,
    RISK_CLASSES,
    ApprovedAction,
    ApprovedActionScriptExemption,
    ApprovedActionTemplateError,
    get_script_exemption,
    is_destructive,
    non_approvable_reason,
    render_params_template,
    secret_bearing_keys,
)
from app.core.audit import audit_service
from app.core.i18n import N_, tr, tr_marked
from app.jobs.registry import get_job_type, registered_names
from app.models import Script
from app.modules.health.errors import (
    HealthConflictError,
    HealthNotFoundError,
    HealthValidationError,
)
from app.modules.health.models import HealthAlertAction, HealthCheck

__all__ = [
    "create_approved_action",
    "delete_approved_action",
    "get_action",
    "grant_script_exemption",
    "list_approved_actions",
    "revoke_script_exemption",
    "update_approved_action",
]

logger = logging.getLogger("its.health.approved_actions")

# Columns a caller may set. `destructive` is deliberately ABSENT: it is derived.
_WRITABLE: frozenset[str] = frozenset(
    {
        "key",
        "name",
        "description",
        "job_type",
        "params_template",
        "script_id",
        "script_version",
        "risk_class",
        "max_target_scope",
        "autonomy",
        "personas",
        "cooldown_s",
        "max_per_device_per_day",
        "max_per_org_per_hour",
        "active",
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def get_action(session: AsyncSession, org_id: UUID, action_id: UUID) -> ApprovedAction:
    action = (
        await session.execute(
            select(ApprovedAction).where(
                ApprovedAction.org_id == org_id, ApprovedAction.id == action_id
            )
        )
    ).scalar_one_or_none()
    if action is None:
        raise HealthNotFoundError(tr("approved action {action_id} not found", action_id=action_id))
    return action


async def list_approved_actions(
    session: AsyncSession, org_id: UUID
) -> list[ApprovedAction]:
    return list(
        (
            await session.execute(
                select(ApprovedAction)
                .where(ApprovedAction.org_id == org_id)
                .order_by(ApprovedAction.key)
            )
        )
        .scalars()
        .all()
    )


# Each fuse's `(floor, ceiling, why the ceiling)`. THE CEILING IS THE HALF THAT
# MATTERS: a fuse checked only for `> 0` is an unbounded operator integer, and
# §8.4's "fleet-wide is structurally impossible" was resting on it. The numbers
# and their anchors live with the CHECK constraint in
# `app/models/approved_actions.py`; the message lives here, exactly as it does
# for the autonomy gate.
_FUSE_BOUNDS: dict[str, tuple[int, int, str]] = {
    "cooldown_s": (
        0,
        MAX_COOLDOWN_S,
        N_(
            "a cooldown longer than 30 days is indistinguishable from switching the action off, "
            "and this value is put straight into a timedelta at dispatch"
        ),
    ),
    "max_per_device_per_day": (
        1,
        MAX_PER_DEVICE_PER_DAY,
        N_(
            "at the default 3600s cooldown a device can be reached at most 24 times a day; this "
            "bounds one endpoint's exposure at roughly four machine-decided jobs an hour"
        ),
    ),
    "max_per_org_per_hour": (
        1,
        MAX_PER_ORG_PER_HOUR,
        N_(
            "the sweep dispatches up to 500 rows every 30s, so an unbounded value turns 'one "
            "job, one device' into 60,000 machine-decided jobs an hour for this tenant; an org "
            "may not be authorized for more in an HOUR than the sweep can emit in one CYCLE"
        ),
    ),
}


def _validate_enums(fields: dict[str, Any]) -> None:
    for column, allowed in (
        ("risk_class", RISK_CLASSES),
        ("autonomy", AUTONOMY_LEVELS),
        ("max_target_scope", MAX_TARGET_SCOPES),
    ):
        value = fields.get(column)
        if value is not None and value not in allowed:
            raise HealthValidationError(
                tr(
                    "{column} must be one of {allowed}; got {value}",
                    column=column,
                    allowed=list(allowed),
                    value=repr(value),
                )
            )
    for column, (floor, ceiling, why) in _FUSE_BOUNDS.items():
        value = fields.get(column)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            raise HealthValidationError(
                tr("{column} must be an integer; got {value}", column=column, value=repr(value))
            )
        if value < floor:
            raise HealthValidationError(
                tr(
                    "{column} must be at least {floor}; got {value}",
                    column=column,
                    floor=floor,
                    value=value,
                )
                if floor
                else tr("{column} must not be negative; got {value}", column=column, value=value)
            )
        if value > ceiling:
            raise HealthValidationError(
                tr(
                    "{column} must be at most {ceiling}; got {value}. {why}",
                    column=column,
                    ceiling=ceiling,
                    value=value,
                    why=tr_marked(why),
                )
            )


def _validate_params_against_job_type(job_type: str, rendered: dict[str, Any]) -> None:
    """The RENDERED template, checked against the job type's OWN params model.

    RENDERING IS NOT VALIDATION, and the gap between them is an org-wide poison
    pill. `render_params_template` walks the structure and refuses illegal brace
    tokens; it knows nothing about which keys the job type has or which of them
    are required. A template the job type will refuse is therefore stored with a
    201, and the first thing that notices is `JobEngine.create` -- which raises a
    bare `ValueError`, not a typed health error, at DISPATCH time.

    WHAT THAT COSTS IF IT ESCAPES, which is the reason this check is here rather
    than in a nicer error message downstream: `execute_pending_actions` does not
    catch `ValueError`, so it reaches `sweep._for_each_org`'s broad
    `except Exception`, which ROLLS BACK THE WHOLE ORG'S TRANSACTION AND SKIPS
    ITS CYCLE. Every remediation, every ticket and every notify for that tenant,
    every 30 seconds, indefinitely, behind one log line -- caused by one badly
    written approved action that the API accepted. Through the API at dispatch
    time it is an uncaught 500.

    `script.run` IS THE ONE JOB TYPE WHOSE TEMPLATE MUST BE EMPTY, and that is
    not an exemption from this check -- it is the strictest form of it. The
    dispatcher builds `ScriptRunParams` from the PINNED `scripts` ROW and never
    from the template (§8.2: "duplicating the pin into the template would create
    two truths that can disagree"), so a non-empty template here is config that
    an operator can read, believe and edit, and that nothing will ever execute.
    Validating it against `ScriptRunParams` instead would be worse: it would
    make an `interpreter`/`body` pair that is silently ignored look correct.
    """
    if job_type == "script.run":
        if rendered:
            raise HealthValidationError(
                tr(
                    "params_template must be empty for job_type {job_type}: the dispatcher "
                    "builds ScriptRunParams from the PINNED scripts row and never from the "
                    "template, so anything here is silently ignored at dispatch (spec §8.2). "
                    "Got keys {rendered}",
                    job_type=repr(job_type),
                    rendered=sorted(rendered),
                )
            )
        return

    model = get_job_type(job_type).params_model
    try:
        model.model_validate(rendered)
    except ValidationError as exc:
        raise HealthValidationError(
            tr(
                "params_template is not valid for job_type {job_type}: {exc}. An approved "
                "action stored with params its job type refuses is not a bad row, it is a "
                "poison pill: the dispatcher raises at 03:00 inside the org's sweep "
                "transaction and rolls back that tenant's whole cycle, every 30s",
                job_type=repr(job_type),
                exc=exc,
            )
        ) from exc


async def _validate_and_derive(
    session: AsyncSession, org_id: UUID, fields: dict[str, Any]
) -> bool:
    """Every §8.2 write-time check, returning the DERIVED `destructive`."""
    unknown = sorted(set(fields) - _WRITABLE)
    if unknown:
        # `destructive` lands here when a caller sends it, which is the point:
        # refused loudly rather than ignored, so nobody believes they set it.
        raise HealthValidationError(
            tr(
                "unknown or non-writable approved-action field(s): {unknown}; "
                "`destructive` is DERIVED from job_type/params/exemption and is never "
                "accepted from a request body (spec §8.4)",
                unknown=unknown,
            )
        )

    _validate_enums(fields)

    job_type = fields.get("job_type")
    if not job_type:
        raise HealthValidationError(tr("job_type is required"))
    if job_type not in registered_names():
        raise HealthValidationError(
            tr(
                "job_type {job_type} is not a registered job type; known types are "
                "{registered_names}",
                job_type=repr(job_type),
                registered_names=sorted(registered_names()),
            )
        )

    # DENY BY DEFAULT, and BEFORE anything else about the row is considered:
    # being registered means an agent can run it, not that a machine may decide
    # to. A job type outside the allowlist has governance this registry cannot
    # express, and no combination of risk_class, autonomy and fuses restores it.
    refusal = non_approvable_reason(job_type)
    if refusal is not None:
        raise HealthValidationError(
            tr(
                "job_type {job_type} may not be named by an approved action: {refusal}",
                job_type=repr(job_type),
                refusal=tr_marked(refusal),
            )
        )

    script_id = fields.get("script_id")
    script_version = fields.get("script_version")
    if job_type == "script.run":
        if script_id is None or script_version is None:
            raise HealthValidationError(
                tr(
                    "script.run approved actions must pin both script_id and script_version: "
                    "without the version, approving a script approves its future contents"
                )
            )
        live = (
            await session.execute(
                select(Script.version).where(Script.org_id == org_id, Script.id == script_id)
            )
        ).scalar_one_or_none()
        if live is None:
            raise HealthValidationError(
                tr("script {script_id} does not resolve in this org", script_id=script_id)
            )
    elif script_id is not None or script_version is not None:
        raise HealthValidationError(
            tr(
                "script_id/script_version are only valid for job_type='script.run', got {job_type}",
                job_type=repr(job_type),
            )
        )

    template = fields.get("params_template") or {}
    try:
        # Rendered at WRITE time against placeholder values, so an illegal token
        # is a 400 now rather than a `KeyError` at 03:00 with an alert open
        # (§8.2's reason 2 for refusing `str.format`).
        rendered = render_params_template(
            job_type, template, device_id=UUID(int=0), selector="probe"
        )
    except ApprovedActionTemplateError as exc:
        raise HealthValidationError(str(exc)) from exc
    # BEFORE the params-model check, deliberately. A credential in a key the
    # model does not have would otherwise be refused as "extra fields not
    # permitted", which reads as "rename the field" -- and the operator's next
    # attempt puts the same secret in a key the model DOES accept.
    secret_keys = secret_bearing_keys(template)
    if secret_keys:
        raise HealthValidationError(
            tr(
                "params_template may not carry a credential: key(s) {secret_keys} name a "
                "secret, and a params_template is plaintext JSONB on a config row that "
                "every holder of `health.read` can list. Stored credentials live encrypted "
                "in the vault and are decrypted only at point of use (invariant #3)",
                secret_keys=secret_keys,
            )
        )
    _validate_params_against_job_type(job_type, rendered)

    exemption = None
    if job_type == "script.run" and script_id is not None and script_version is not None:
        # The pin is guaranteed non-NULL here by the branch above, which is the
        # only path that reaches this line for `script.run`. Narrowed explicitly
        # rather than asserted: `is_destructive` FAILS CLOSED on a missing pin
        # (§8.4's amendment), so if this ever stops holding the answer is
        # "destructive", not a crash.
        exemption = await get_script_exemption(
            session, org_id=org_id, script_id=script_id, script_version=script_version
        )
    destructive = is_destructive(
        job_type,
        template,
        exemption,
        script_id=script_id,
        script_version=script_version,
    )

    autonomy = fields.get("autonomy")
    scope = fields.get("max_target_scope", "device")
    if autonomy == "autonomous":
        reasons = []
        if destructive:
            reasons.append(tr("it is destructive"))
        if fields.get("risk_class") == "high":
            reasons.append(tr("its risk_class is 'high'"))
        if scope != "device":
            reasons.append(tr("its max_target_scope is {scope}", scope=repr(scope)))
        if reasons:
            raise HealthValidationError(
                tr(
                    "autonomy='autonomous' is refused because {reasons}",
                    reasons=tr(" and ").join(reasons),
                )
            )
    return destructive


async def create_approved_action(
    session: AsyncSession, org_id: UUID, actor_id: UUID, fields: dict[str, Any]
) -> ApprovedAction:
    destructive = await _validate_and_derive(session, org_id, fields)
    now = _utcnow()
    action = ApprovedAction(
        org_id=org_id,
        destructive=destructive,
        created_by=actor_id,
        created_at=now,
        updated_at=now,
        **{k: v for k, v in fields.items() if k in _WRITABLE},
    )
    session.add(action)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="health.action_created",
        target_type="approved_action",
        target_id=str(action.id),
        metadata={
            "key": action.key,
            "job_type": action.job_type,
            "autonomy": action.autonomy,
            "destructive": destructive,
            "risk_class": action.risk_class,
        },
    )
    await session.flush()
    return action


async def update_approved_action(
    session: AsyncSession,
    org_id: UUID,
    action_id: UUID,
    actor_id: UUID,
    fields: dict[str, Any],
) -> ApprovedAction:
    """Update, re-deriving `destructive` from the RESULTING row.

    THE AUTONOMY CHANGE IS AUDITED WITH ITS OLD AND NEW VALUE (§11.3). "Who set
    this action to autonomous" is only answerable if the previous value is in
    the audit row -- the new one is already in the table, so an audit record
    carrying only the new value answers a question nobody asked.
    """
    action = await get_action(session, org_id, action_id)
    merged = {
        column: fields.get(column, getattr(action, column))
        for column in _WRITABLE
    }
    destructive = await _validate_and_derive(session, org_id, dict(merged))

    previous_autonomy = action.autonomy
    for column, value in merged.items():
        setattr(action, column, value)
    action.destructive = destructive
    action.updated_at = _utcnow()
    session.add(action)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="health.action_updated",
        target_type="approved_action",
        target_id=str(action.id),
        metadata={"key": action.key, "changed": sorted(fields), "destructive": destructive},
    )
    if "autonomy" in fields and fields["autonomy"] != previous_autonomy:
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="health.action_autonomy_changed",
            target_type="approved_action",
            target_id=str(action.id),
            metadata={
                "key": action.key,
                "previous": previous_autonomy,
                "new": action.autonomy,
            },
        )
    await session.flush()
    return action


async def delete_approved_action(
    session: AsyncSession, org_id: UUID, action_id: UUID, actor_id: UUID
) -> None:
    """Delete an approved action, refusing -- and naming what is in the way --
    while anything still references it.

    TWO REFUSALS, EACH NAMING ITS BLOCKERS, on the shape
    `profiles.delete_profile` already uses:

    1. **A live health check declares it as its remediation.**
       `health_check.remediate_approved_action_id` is `ON DELETE RESTRICT`
       (migration 0044). The refusal is here for the MESSAGE, not for the fact:
       the FK is what makes it true against a second caller and against direct
       SQL. The alternative shape, `ON DELETE SET NULL`, was rejected -- it
       would disarm every check naming the action silently, which is the right
       DIRECTION (fail closed) arrived at without anyone being told. Retiring a
       check DOES disarm it (`profiles._retire_or_delete_checks`), because a
       retired check can never be edited again and its stale declaration would
       block this delete forever.
    2. **A `health_alert_action` row records that it was dispatched.** That FK
       predates migration 0044 and had no message in front of it, so this path
       used to surface as a raw `IntegrityError` naming a constraint and no
       alerts. It is a genuine block, not a bug: the row is the record of what
       a machine decided to run against a device, and §2.4's rule is that
       history is never destroyed because an admin deleted a config row. It
       clears itself -- §12's retention sweep removes aged alerts and their
       action rows, exactly as `delete_profile` documents for its own alert
       block. `active=False` is the path meanwhile.
    """
    action = await get_action(session, org_id, action_id)
    key = action.key
    declaring = list(
        (
            await session.execute(
                select(HealthCheck.id).where(
                    HealthCheck.org_id == org_id,
                    HealthCheck.remediate_approved_action_id == action_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if declaring:
        raise HealthConflictError(
            tr(
                "approved action {key} is still declared as the remediation of {declaring} "
                "health check(s); clear it from them first: {check_ids}",
                key=repr(key),
                declaring=len(declaring),
                check_ids=[str(c) for c in declaring[:5]],
            )
        )

    dispatched = list(
        (
            await session.execute(
                select(HealthAlertAction.alert_id)
                .where(
                    HealthAlertAction.org_id == org_id,
                    HealthAlertAction.approved_action_id == action_id,
                )
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    if dispatched:
        raise HealthConflictError(
            tr(
                "approved action {key} has been dispatched and its history cannot be "
                "destroyed; deactivate it instead (active=false). Alerts still referencing "
                "it include: {alert_ids}",
                key=repr(key),
                alert_ids=[str(a) for a in dispatched],
            )
        )
    await session.delete(action)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="health.action_deleted",
        target_type="approved_action",
        target_id=str(action_id),
        metadata={"key": key},
    )
    await session.flush()


async def _actions_pinning(
    session: AsyncSession, org_id: UUID, *, script_id: UUID, script_version: int
) -> list[ApprovedAction]:
    """EVERY approved action in the org pinning this exact pair.

    The exemption is keyed `(script_id, script_version)` and is EXPLICITLY
    SHARED between approved actions (`app/models/approved_actions.py`: "two
    approved actions naming the same pinned version share one exemption rather
    than needing two identical grants"). One shared row is therefore an input to
    the derivation of EVERY action pinning the pair, and granting or revoking it
    while re-deriving only the action the call names leaves the others stored
    with a value the derivation no longer produces.

    That staleness is invisible to every other layer:
    `ck_approved_action_autonomous_is_bounded` reads the STORED column, so it
    cannot see it, and the dispatcher branches on `autonomy` and never reads
    `destructive` at all. The stored column IS the gate, so it has to be right
    on every row the shared row feeds.
    """
    return list(
        (
            await session.execute(
                select(ApprovedAction)
                .where(
                    ApprovedAction.org_id == org_id,
                    ApprovedAction.script_id == script_id,
                    ApprovedAction.script_version == script_version,
                )
                .order_by(ApprovedAction.key)
            )
        )
        .scalars()
        .all()
    )


def _rederive_from_exemption(
    action: ApprovedAction, exemption: ApprovedActionScriptExemption | None
) -> bool:
    """Re-derive and store `destructive` on one action, demoting `autonomous`
    when it becomes destructive. Returns whether the autonomy was demoted.

    The demotion is not a convenience: the CHECK would refuse the flush anyway,
    so without it a revoke that touches an autonomous sibling is a 500 and the
    revoke does not happen at all.
    """
    action.destructive = is_destructive(
        action.job_type,
        action.params_template or {},
        exemption,
        script_id=action.script_id,
        script_version=action.script_version,
    )
    demoted = False
    if action.destructive and action.autonomy == "autonomous":
        action.autonomy = "require_approval"
        demoted = True
    return demoted


async def _apply_exemption_change(
    session: AsyncSession,
    org_id: UUID,
    *,
    script_id: UUID,
    script_version: int,
    exemption: ApprovedActionScriptExemption | None,
    actor_id: UUID,
    audit_action: str,
    reason: str | None = None,
) -> None:
    """Re-derive, store and audit `destructive` on every action pinning the pair.

    ONE AUDIT RECORD PER AFFECTED ACTION, not one for the action the caller
    named: "which approved actions did this grant make autonomous-eligible" is
    only answerable if each of them is in the log. `affected` names the whole
    set on every record, so a reader of any one of them can see it was not
    alone.
    """
    affected = await _actions_pinning(
        session, org_id, script_id=script_id, script_version=script_version
    )
    demotions = {action.id: _rederive_from_exemption(action, exemption) for action in affected}
    for action in affected:
        session.add(action)
    await session.flush()

    keys = [action.key for action in affected]
    for action in affected:
        if demotions[action.id]:
            logger.warning(
                "health: the script exemption for %s v%s changed and made approved action %s "
                "destructive; its autonomy was demoted from 'autonomous' to 'require_approval'",
                script_id,
                script_version,
                action.key,
            )
        metadata: dict[str, Any] = {
            "script_id": str(script_id),
            "script_version": script_version,
            "destructive": action.destructive,
            "autonomy_demoted": demotions[action.id],
            "affected": keys,
        }
        if reason is not None:
            metadata["reason"] = reason
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action=audit_action,
            target_type="approved_action",
            target_id=str(action.id),
            metadata=metadata,
        )
    await session.flush()


async def grant_script_exemption(
    session: AsyncSession,
    org_id: UUID,
    action_id: UUID,
    *,
    actor_id: UUID,
    reason: str = "",
) -> ApprovedActionScriptExemption:
    """`POST /approved-actions/{id}/exempt-script` (§10, §8.4 clause 2).

    THE PAIR COMES FROM THE ACTION'S PIN, NEVER FROM THE REQUEST BODY, so an
    exemption can never be granted for a version the action does not name.
    Refused when the pinned version no longer matches the live `scripts.version`
    -- exempting a version that has already been edited away is a no-op that
    reads as a grant, which is the worst of both.

    Granting it RE-DERIVES `destructive` ON EVERY APPROVED ACTION PINNING THAT
    PAIR, not only on the one named in the call. The exemption row is shared by
    construction (§2.10), so it is an input to all of their derivations, and a
    stored column that disagrees with the derivation is exactly what §8.4 exists
    to make impossible.
    """
    action = await get_action(session, org_id, action_id)
    if action.job_type != "script.run":
        raise HealthValidationError(
            tr(
                "approved action {action_key} is {action_job_type}; only script.run "
                "actions carry a script exemption",
                action_key=repr(action.key),
                action_job_type=repr(action.job_type),
            )
        )
    live = (
        await session.execute(
            select(Script.version).where(
                Script.org_id == org_id, Script.id == action.script_id
            )
        )
    ).scalar_one_or_none()
    if live is None:
        raise HealthValidationError(
            tr(
                "script {action_script_id} does not resolve in this org",
                action_script_id=action.script_id,
            )
        )
    if live != action.script_version:
        raise HealthValidationError(
            tr(
                "approved action {action_key} pins script version {action_script_version} "
                "but the live script is version {live}; exempting a version that has "
                "already been edited away is a no-op that reads as a grant",
                action_key=repr(action.key),
                action_script_version=action.script_version,
                live=live,
            )
        )

    if action.script_id is None or action.script_version is None:  # pragma: no cover
        # `ck_approved_action_script_pin` makes this unrepresentable for a
        # `script.run` row; the check is here so the type narrows and so a row
        # written by a direct SQL fixup fails loudly rather than silently
        # granting an exemption for `(NULL, NULL)`.
        raise HealthValidationError(
            tr(
                "approved action {action_key} is script.run but carries no pin",
                action_key=repr(action.key),
            )
        )
    existing = await get_script_exemption(
        session,
        org_id=org_id,
        script_id=action.script_id,
        script_version=action.script_version,
    )
    if existing is None:
        existing = ApprovedActionScriptExemption(
            org_id=org_id,
            script_id=action.script_id,
            script_version=action.script_version,
            reason=reason,
            granted_by=actor_id,
            granted_at=_utcnow(),
        )
        session.add(existing)
        await session.flush()

    await _apply_exemption_change(
        session,
        org_id,
        script_id=action.script_id,
        script_version=action.script_version,
        exemption=existing,
        actor_id=actor_id,
        audit_action="health.action.destructive_exempted",
        reason=reason,
    )
    return existing


async def revoke_script_exemption(
    session: AsyncSession,
    org_id: UUID,
    action_id: UUID,
    *,
    actor_id: UUID,
) -> None:
    """The matching DELETE (§10). Re-derives `destructive` ON EVERY APPROVED
    ACTION PINNING THE PAIR, which is what makes a revoke meaningful.

    The row deleted here is SHARED (§2.10): it is keyed
    `(script_id, script_version)` precisely so two approved actions naming the
    same pinned version need only one grant. Re-deriving only the action this
    call names therefore leaves every sibling stored as non-destructive with no
    exemption behind it -- and `autonomous` if it was granted that -- which is a
    `script.run` of arbitrary code signed and dispatched as SYSTEM/root with
    nothing backing it. Nothing downstream can notice:
    `ck_approved_action_autonomous_is_bounded` reads the stored column, and the
    dispatcher branches on `autonomy` and never reads `destructive`.
    """
    action = await get_action(session, org_id, action_id)
    if action.script_id is None or action.script_version is None:
        raise HealthValidationError(
            tr(
                "approved action {action_key} is {action_job_type} and carries no script "
                "pin, so it has no exemption to revoke",
                action_key=repr(action.key),
                action_job_type=repr(action.job_type),
            )
        )
    existing = await get_script_exemption(
        session,
        org_id=org_id,
        script_id=action.script_id,
        script_version=action.script_version,
    )
    if existing is None:
        raise HealthNotFoundError(
            tr(
                "no script exemption exists for {action_script_id} v{action_script_version}",
                action_script_id=action.script_id,
                action_script_version=action.script_version,
            )
        )
    script_id = action.script_id
    script_version = action.script_version
    await session.delete(existing)
    await session.flush()

    await _apply_exemption_change(
        session,
        org_id,
        script_id=script_id,
        script_version=script_version,
        exemption=None,
        actor_id=actor_id,
        audit_action="health.action.destructive_exemption_revoked",
    )
