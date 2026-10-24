"""The health, alert and approved-action API
(`docs/modules/endpoint-health-alerting/spec.md` §10, §11).

Thirty-two route registrations: twenty-three under `/api/v1/health`, two under
`/api/v1/devices/{device_id}`, and seven under `/api/v1/approved-actions`.
`register(app)` mounts this router under `/api/v1`.

**RBAC (invariant #6, §11.1).** Every route depends on `require_permission`
DIRECTLY, and completeness is DERIVED FROM THE ROUTER by
`app.core.rbac.route_permission` (`tests/test_health_api.py`) rather than from a
hand-maintained list a later route addition could silently miss. Five codes,
and the splits between them are deliberate, not accidental:

* `health.read` / `health.admin` -- the ordinary read/write pair. `health.admin`
  is admin tier because it decides what the entire fleet alarms on and can
  silence every alert in the organisation by deleting one assignment.
* `health.alert.manage` -- acknowledge, mute, close, and `check-now`.
  Technician tier: this is the day job, and it must not require the grant that
  can rewrite the policy. It runs no code on an endpoint, but `check-now` does
  mint a signed job, which is why it is not a read code -- see below.
* `health.remediate` -- dispatch a remediation, approve one, refuse one.
  Deliberately NOT bundled into `health.admin`: configuring what alarms and
  executing code as SYSTEM/root on an endpoint are different acts with
  different blast radii, and a `health.admin` holder is refused
  `POST /alerts/{id}/remediate` at this layer.

  **AND ON THE TWO ROUTES THAT DISPATCH, IT IS NOT THE WHOLE CHECK (§20).**
  `remediate` and `actions/{id}/approve` resolve a catalogue row carrying a
  `job_type`, and each additionally requires the permission THAT job type's own
  module route requires -- `app/jobs/dispatch_permissions.py`, where a `None`
  answer is a refusal. Gated on `health.remediate` alone, its holder could fire
  a `patch.apply` job here while being refused the same job on
  `POST /api/v1/patch/apply`: privilege escalation across a module boundary and
  invariant #6 violated. The check is inside the service, not in a dependency,
  because the permission required depends on the request body -- see
  `remediate_alert_route`'s docstring. `refuse` is deliberately NOT gated that
  way: refusing creates no job, and requiring the higher grant to decline would
  leave requests nobody may clear sitting until they expire.
* `health.action.admin` -- the registry, the autonomy dial, the script
  exemption. The highest grant in the module: it decides what MAY run
  automatically as SYSTEM/root across the fleet. A `health.remediate` holder is
  refused every `approved-actions` write.

ONE ROUTE DELIBERATELY CARRIES SOMEONE ELSE'S PERMISSION.
`GET /devices/{device_id}/health` is gated on **`devices.read`** (§10), for the
reason the SLA module gates its per-ticket view on `ticket.read`: a technician
looking at a device needs its health without holding health administration
rights, and `health.read` is not seeded to the technician tier.

**`check-now` WAS THE SECOND, AND IS NOT ANY MORE (§10's amendment).**
`POST /devices/{device_id}/health/check-now` was gated on `health.read` because
that is what §10's table says, with this docstring recording the cost as
"still rate-limited by nothing, which is the honest cost of following the
table". The cost turned out to be unpayable, and it was reproduced rather than
argued: a principal holding EXACTLY `health.read` -- the code migration 0040
grants to `read-only` and `auditor`, the two lowest seeded tiers -- minted 30
signed jobs from 30 consecutive requests, all 200. There is no fleet-wide
target in this module (§8.4), but there does not need to be: a `for` loop over
a 5,000-device fleet is one, one device at a time, driven by an account whose
name says it may not change anything.

Both halves are now closed, because either alone leaves the other open:

* **The gate is `health.alert.manage`.** Minting a signed job is a WRITE at an
  endpoint whatever the job does, and invariant #6 is about every job dispatch,
  not only the destructive ones. `health.alert.manage` is the module's
  operate-but-do-not-configure code and is seeded to the technician tier, which
  is exactly the audience for "sample this box now"; `health.read` keeps every
  route that only reads. This makes `check-now` no longer the one route in the
  repo where a read-tier code dispatches a job. Migration 0040's comment
  describing `health.alert.manage` as dispatching nothing is now narrower than
  the permission -- it is a shipped migration and is left alone; §10 and
  `app/core/rbac.py` carry the current definition.
* **And it is fused.** A technician can loop too, and the gate cannot tell a
  loop from a click. `_check_now_fuse` bounds it per device and per org,
  counted against `jobs` -- §2.9's rule, the same one `actions.py::_fuse_breach`
  follows, chosen over `notification/api.py`'s Redis counter because the thing
  being limited IS a `jobs` row and a second counter would be a second truth.

**ROUTE ORDERING.** Literal segments before `{id}` parameters, the lesson every
module since Phase 11 carries: `/health/check-types`, `/health/alerts`,
`/health/assignments`, `/health/profiles` and `/health/settings` are literal
siblings and none of them may be reachable as a `{profile_id}`. The same rule
one level down: `/health/alerts/summary` is registered BEFORE
`/health/alerts/{alert_id}`, whose parameter is a `UUID` -- so the wrong order
is not a shadowed route but a 422 on a totals endpoint, which a console reads
as a server fault.

**ERROR TRANSLATION HAPPENS OUTSIDE THE `org_session` BLOCK.** `org_session`
wraps `session.begin()`, so the transaction commits when the block exits
NORMALLY and rolls back when an exception leaves it. Catching INSIDE does not
orphan anything by itself -- `_http_error` re-raises -- but it makes the fatal
variant writable: catch, then let the block exit normally, and a partially
applied edit commits. The stake here is concrete: `replace_checks` clears open
alerts as `policy_removed`, retires check rows, bumps `policy_version` and
re-pushes, all in one transaction. A handler that swallowed an error mid-way
would commit a profile whose alerts were cleared for checks that are still
running.

**NOT-MINE IS 404, NEVER 403.** `HealthNotFoundError` -- raised
indistinguishably for "absent" and "belongs to another org" by every `get_*` in
this module -- becomes 404. A 403 would confirm the id exists somewhere, which
is a cross-tenant existence oracle. `HealthConflictError` is 409 (a profile
still assigned, a check whose alerts block a hard delete); every other
`HealthValidationError` is 400. Routing is by TYPE, never by matching exception
prose.

**THE SIGNED JOBS THIS MODULE PUTS ON A DEVICE, AND WHERE THEY ARE PUBLISHED.**
Three routes create jobs: `check-now`, `remediate`, and `actions/{id}/approve`;
five more (profile update/delete, checks replace, assignment create/delete)
queue a `health.policy.apply` reconcile. Every one of them creates the job
INSIDE the tenant transaction and publishes to Redis only AFTER the block has
exited -- `app/modules/deploy/scripts.py::run_script` states the reason in its
own docstring: a job published before the caller's transaction commits could be
handed to an agent with no durable server record if the commit later failed.
`tests/test_health_egress.py` pins BOTH halves structurally: `api.py` is the
only file under `app/modules/health/` permitted to name `publish_job` at all,
and no `publish_job` call in it may appear inside an `async with org_session`
block.

**WHY A CONFIG WRITE RUNS THE RECONCILE RATHER THAN A TARGETED PUSH.** "The
affected devices" of a profile edit cannot be enumerated without resolving each
device (§3's four levels, the group walk, the OS filter), which is exactly what
`sweep.run_policy_reconcile` does -- and it pushes ONLY where the acknowledged
`policy_version` is behind, so a device whose ack already matches gets zero
jobs. A hand-rolled "devices on this profile" query would additionally miss the
case that matters most: deleting an assignment, where the affected devices are
the ones that no longer resolve to it.

**BUSINESS LOGIC IS NOT HERE.** Profiles/checks/assignments are `profiles.py`,
resolution is `resolution.py`, alert transitions are `alerts.py`, remediation is
`actions.py`, the registry is `approved_actions.py`, settings are `settings.py`.
These handlers resolve the principal, call one service function, map errors, and
publish after commit.
"""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.approved_actions import (
    ApprovedAction,
    get_script_exemption,
    redact_secret_values,
)
from app.core.i18n import tr, tr_marked
from app.core.rbac import user_permission_codes
from app.core.tenancy import org_session
from app.gateway.dispatch import publish_job
from app.models import Agent, Device, Job, Script, Ticket
from app.modules.health.actions import (
    approve_action,
    dispatch_manual_remediation,
    refuse_action,
)
from app.modules.health.alerts import (
    acknowledge,
    close,
    get_alert,
    list_alerts,
    mute,
    summarise_open_alerts,
)
from app.modules.health.approved_actions import (
    create_approved_action,
    delete_approved_action,
    get_action,
    grant_script_exemption,
    list_approved_actions,
    revoke_script_exemption,
    update_approved_action,
)
from app.modules.health.errors import (
    HealthConflictError,
    HealthForbiddenError,
    HealthNotFoundError,
    HealthValidationError,
)
from app.modules.health.jobtypes import CHECK_NOW_JOB_TYPE
from app.modules.health.models import (
    DeviceHealthCheckState,
    DeviceHealthPolicyAck,
    HealthAlert,
    HealthAlertAction,
    HealthCheck,
    HealthProfile,
    HealthProfileAssignment,
)
from app.modules.health.policy_push import push_check_now
from app.modules.health.profiles import (
    clone_profile,
    count_checks,
    create_assignment,
    create_profile,
    delete_assignment,
    delete_profile,
    effective_for_seconds,
    get_profile,
    list_assignments,
    list_checks,
    list_profiles,
    replace_checks,
    update_profile,
)
from app.modules.health.registry import CHECK_TYPES, render_threshold
from app.modules.health.resolution import resolve_profile_for_device
from app.modules.health.schema import (
    AlertActionOut,
    AlertDetailOut,
    AlertOut,
    AlertRefuseIn,
    AlertSummaryOut,
    ApprovedActionCreate,
    ApprovedActionOut,
    ApprovedActionUpdate,
    AssignmentCreate,
    AssignmentOut,
    CheckOut,
    ChecksReplace,
    CheckTypeOut,
    DeviceCheckStateOut,
    DeviceHealthOut,
    ExemptScriptIn,
    JobDispatchedOut,
    MuteIn,
    ProfileChecksOut,
    ProfileClone,
    ProfileCreate,
    ProfileOut,
    ProfileUpdate,
    RemediateIn,
    ScriptExemptionOut,
    SettingsOut,
    SettingsUpdate,
)
from app.modules.health.settings import get_settings, get_settings_row, update_settings
from app.modules.health.sweep import run_policy_reconcile

router = APIRouter(tags=["health"])

# `(job_id, envelope)` -- what every job-creating service in this module returns
# for the caller to publish after commit.
Envelope = tuple[UUID, dict[str, Any]]


def _http_error(exc: HealthValidationError) -> HTTPException:
    """By TYPE, never by message text.

    `HealthNotFoundError` is 404 and is the SAME error for "absent" and "another
    org's", so it adds no oracle. `HealthConflictError` is 409: a profile that is
    still assigned and a check whose alerts block a hard delete are both "the
    request is well formed and the state refuses it", which is what 409 means and
    what §10 promises ("returns 409 naming the blocking alerts, never a raw
    IntegrityError"). Both are subclasses of `HealthValidationError`, so the
    order of these two checks is load-bearing.

    `HealthForbiddenError` is 403, and it is the one deliberate exception to
    "NOT-MINE IS 404" above -- because it is not about not-mine. It is raised
    only after the alert AND the approved action have both resolved in the
    caller's own org (§20's second permission), so it confirms the existence of
    nothing a `health.remediate` holder could not already read, and a 404 would
    tell them their own catalogue entry had vanished.
    """
    if isinstance(exc, HealthNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, HealthForbiddenError):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, HealthConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


async def _publish(request: Request, envelopes: Iterable[Envelope]) -> None:
    """Publish signed envelopes to their target devices.

    CALLED ONLY AFTER THE `org_session` BLOCK HAS EXITED, never inside it -- see
    the module docstring, and `tests/test_health_egress.py`, which asserts the
    absence structurally rather than by review.

    The device id comes out of the ENVELOPE (`envelope["job"]["device"]`), the
    same way `app/worker.py`'s sweep publishers read it, rather than being
    carried alongside: the envelope is what the agent will act on, so taking the
    routing key from anywhere else would let a job be delivered to a device it
    was not signed for.
    """
    redis = request.app.state.redis
    for _job_id, envelope in envelopes:
        await publish_job(redis, UUID(envelope["job"]["device"]), envelope)


async def _reconcile(
    session: AsyncSession, request: Request, org_id: UUID
) -> list[Envelope]:
    """Re-push `health.policy.apply` wherever this write moved a device's
    effective policy ahead of its acknowledgement (§12, §2.11).

    Returns the envelopes; the CALLER publishes them after commit.
    """
    return await run_policy_reconcile(
        session, org_id, job_engine=request.app.state.job_engine
    )


# --------------------------------------------------------------------------- #
# Response builders. One per shape, so no two routes can disagree about what a
# profile or an alert looks like.
# --------------------------------------------------------------------------- #


async def _profile_out(session: AsyncSession, org_id: UUID, row: HealthProfile) -> ProfileOut:
    assignments = len(await list_assignments(session, org_id, profile_id=row.id))
    return ProfileOut(
        id=row.id,
        name=row.name,
        description=row.description,
        tick_interval_s=row.tick_interval_s,
        posture_interval_s=row.posture_interval_s,
        summary_interval_s=row.summary_interval_s,
        active=row.active,
        policy_version=row.policy_version,
        check_count=await count_checks(session, org_id, row.id),
        assignment_count=assignments,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _threshold_display(check: HealthCheck) -> str:
    """The wire rendering (§5.2), or a placeholder for a type the registry no
    longer knows.

    An unregistered type is not hypothetical: `resolution._check_applies` logs
    and EXCLUDES one, which means the row stays visible in the console while
    evaluating nowhere. Raising here would make that row unreadable, so the
    console shows the stored operands and the caller can see what to delete.
    """
    entry = CHECK_TYPES.get(check.check_type)
    if entry is None:
        return f"{check.threshold_num if check.threshold_num is not None else check.threshold_bool}"
    return render_threshold(
        entry, threshold_num=check.threshold_num, threshold_bool=check.threshold_bool
    )


def _check_out(check: HealthCheck, profile: HealthProfile) -> CheckOut:
    entry = CHECK_TYPES.get(check.check_type)
    return CheckOut(
        id=check.id,
        profile_id=check.profile_id,
        check_type=check.check_type,
        selector=check.selector,
        comparator=check.comparator,
        threshold_num=float(check.threshold_num) if check.threshold_num is not None else None,
        threshold_bool=check.threshold_bool,
        threshold_display=_threshold_display(check),
        for_ticks=check.for_ticks,
        for_seconds=effective_for_seconds(check, profile),
        tick_interval_s=profile.tick_interval_s,
        severity=check.severity,
        os_family=check.os_family,
        enabled=check.enabled,
        declares_notify=check.declares_notify,
        declares_open_ticket=check.declares_open_ticket,
        remediate_approved_action_id=check.remediate_approved_action_id,
        value_kind=entry.value_kind if entry else "numeric",
        cadence=entry.cadence if entry else "tick",
        evaluated_by=entry.evaluated_by if entry else "agent",
        available_os_families=sorted(entry.os_families) if entry else [],
        selector_meaning=tr_marked(entry.selector_meaning) if entry else "",
        created_at=check.created_at,
        updated_at=check.updated_at,
    )


def _assignment_out(row: HealthProfileAssignment, profile_name: str) -> AssignmentOut:
    level = "device" if row.device_id else ("group" if row.device_group_id else "org")
    return AssignmentOut(
        id=row.id,
        profile_id=row.profile_id,
        profile_name=profile_name,
        device_id=row.device_id,
        device_group_id=row.device_group_id,
        level=level,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _alert_out(alert: HealthAlert, device: Device | None) -> AlertOut:
    return AlertOut(
        id=alert.id,
        device_id=alert.device_id,
        device_hostname=device.hostname if device is not None else "",
        device_group_id=device.device_group_id if device is not None else None,
        check_id=alert.check_id,
        profile_id=alert.profile_id,
        check_type=alert.check_type,
        selector=alert.selector,
        severity=alert.severity,
        state=alert.state,
        observed_value=alert.observed_value,
        threshold_display=alert.threshold_display,
        opened_at=alert.opened_at,
        opened_policy_version=alert.opened_policy_version,
        last_seen_at=alert.last_seen_at,
        occurrence_count=alert.occurrence_count,
        flapping=alert.flapping,
        flap_count=alert.flap_count,
        escalated_at=alert.escalated_at,
        acknowledged_at=alert.acknowledged_at,
        acknowledged_by=alert.acknowledged_by,
        muted_until=alert.muted_until,
        muted_by=alert.muted_by,
        cleared_at=alert.cleared_at,
        cleared_reason=alert.cleared_reason,
        cleared_by=alert.cleared_by,
        ticket_id=alert.ticket_id,
        previous_alert_id=alert.previous_alert_id,
    )


def _action_out(
    row: HealthAlertAction, action: ApprovedAction | None, script: Script | None
) -> AlertActionOut:
    return AlertActionOut(
        id=row.id,
        alert_id=row.alert_id,
        kind=row.kind,
        state=row.state,
        approved_action_id=row.approved_action_id,
        approved_action_key=action.key if action is not None else None,
        approved_action_name=action.name if action is not None else None,
        script_id=action.script_id if action is not None else None,
        script_name=script.name if script is not None else None,
        script_version=action.script_version if action is not None else None,
        autonomy=action.autonomy if action is not None else None,
        risk_class=action.risk_class if action is not None else None,
        destructive=action.destructive if action is not None else None,
        job_id=row.job_id,
        ticket_id=row.ticket_id,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        expires_at=row.expires_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _action_row_out(
    row: HealthAlertAction,
    approved: dict[UUID, ApprovedAction],
    scripts: dict[UUID, Script],
) -> AlertActionOut:
    """One action row joined to its approved action and that action's PINNED
    script, both looked up from maps the caller loaded org-scoped.

    A missing approved action is not an error and must not be one: the row is
    kept when the registry entry it named is deleted (`approved_action_id` is a
    plain nullable FK), and an alert whose remediation row cannot be rendered is
    an alert nobody can triage.
    """
    action = approved.get(row.approved_action_id) if row.approved_action_id else None
    script = (
        scripts.get(action.script_id)
        if action is not None and action.script_id is not None
        else None
    )
    return _action_out(row, action, script)


async def _device(session: AsyncSession, org_id: UUID, device_id: UUID) -> Device:
    """Org-scoped device load. Explicit `org_id` predicate in addition to RLS
    (invariant #5): a route handler is not the only caller of this shape, and
    RLS is not the thing that should be doing the filtering in an assertion."""
    device = (
        await session.execute(
            select(Device).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()
    if device is None:
        raise HealthNotFoundError(tr("device {device_id} not found", device_id=device_id))
    return device


async def _devices_by_id(
    session: AsyncSession, org_id: UUID, device_ids: Iterable[UUID]
) -> dict[UUID, Device]:
    ids = list({d for d in device_ids})
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Device).where(Device.org_id == org_id, Device.id.in_(ids))
        )
    ).scalars()
    return {d.id: d for d in rows}


async def _ticket_number(session: AsyncSession, org_id: UUID, ticket_id: UUID | None) -> str | None:
    if ticket_id is None:
        return None
    return (
        await session.execute(
            select(Ticket.number).where(Ticket.org_id == org_id, Ticket.id == ticket_id)
        )
    ).scalar_one_or_none()


async def _approved_action_out(
    session: AsyncSession, org_id: UUID, action: ApprovedAction
) -> ApprovedActionOut:
    """The registry row as the API returns it -- WITH THE TEMPLATE REDACTED.

    `params_template` used to be returned verbatim. The write path refuses a
    template naming a credential, but that gate cannot reach a row that already
    exists, and this is the one function every approved-action route renders
    through (`list`, `get`, `create`, `patch`, and both exemption routes), read
    with `health.read` -- a broad grant that is not admin-tier. A legacy row
    therefore returned a stored credential in an API response to a caller who
    could not even edit it: invariant #3, on the read side, where it is worse
    because it is repeatable by anyone.

    REDACTED RATHER THAN REFUSED, and the withheld paths travel. Refusing to
    render the row would take the whole registry listing down over one bad row
    and hide the exact row that has to be rewritten; the caller needs to see
    that the key exists, that its value was withheld, and which key it is.
    """
    template, redacted_keys = redact_secret_values(action.params_template or {})
    script: Script | None = None
    live_version: int | None = None
    exemption_out: ScriptExemptionOut | None = None
    if action.script_id is not None:
        script = (
            await session.execute(
                select(Script).where(Script.org_id == org_id, Script.id == action.script_id)
            )
        ).scalar_one_or_none()
        live_version = script.version if script is not None else None
        if action.script_version is not None:
            exemption = await get_script_exemption(
                session,
                org_id=org_id,
                script_id=action.script_id,
                script_version=action.script_version,
            )
            if exemption is not None:
                exemption_out = ScriptExemptionOut(
                    id=exemption.id,
                    script_id=exemption.script_id,
                    script_name=script.name if script is not None else None,
                    script_version=exemption.script_version,
                    reason=exemption.reason,
                    granted_by=exemption.granted_by,
                    granted_at=exemption.granted_at,
                )
    return ApprovedActionOut(
        id=action.id,
        key=action.key,
        name=action.name,
        description=action.description,
        job_type=action.job_type,
        params_template=template,
        params_template_redacted_keys=redacted_keys,
        script_id=action.script_id,
        script_name=script.name if script is not None else None,
        script_version=action.script_version,
        script_live_version=live_version,
        risk_class=action.risk_class,
        destructive=action.destructive,
        max_target_scope=action.max_target_scope,
        autonomy=action.autonomy,
        personas=list(action.personas or []),
        cooldown_s=action.cooldown_s,
        max_per_device_per_day=action.max_per_device_per_day,
        max_per_org_per_hour=action.max_per_org_per_hour,
        active=action.active,
        script_exemption=exemption_out,
        created_by=action.created_by,
        created_at=action.created_at,
        updated_at=action.updated_at,
    )


# --------------------------------------------------------------------------- #
# GET/POST /health/profiles (§2.1, §10)
# --------------------------------------------------------------------------- #


@router.get("/health/profiles", response_model=list[ProfileOut])
async def list_profiles_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.read")),
) -> list[ProfileOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_profiles(session, ctx.org_id)
        return [await _profile_out(session, ctx.org_id, row) for row in rows]


@router.post(
    "/health/profiles", status_code=status.HTTP_201_CREATED, response_model=ProfileOut
)
async def create_profile_route(
    body: ProfileCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("health.admin")),
) -> ProfileOut:
    """A new profile has no checks and no assignment, so NO device's effective
    policy changed and no push is queued. That is not an oversight: pushing here
    would put a `health.policy.apply` job on every device that happens to be
    behind, attributed to a profile nobody has assigned yet."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            profile = await create_profile(
                session,
                ctx.org_id,
                ctx.user_id,
                name=body.name,
                description=body.description,
                tick_interval_s=body.tick_interval_s,
                posture_interval_s=body.posture_interval_s,
                summary_interval_s=body.summary_interval_s,
                active=body.active,
            )
            out = await _profile_out(session, ctx.org_id, profile)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# GET/PATCH/DELETE /health/profiles/{profile_id} (§10)
# --------------------------------------------------------------------------- #


@router.get("/health/profiles/{profile_id}", response_model=ProfileOut)
async def get_profile_route(
    profile_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.read")),
) -> ProfileOut:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            profile = await get_profile(session, ctx.org_id, profile_id)
            out = await _profile_out(session, ctx.org_id, profile)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.patch("/health/profiles/{profile_id}", response_model=ProfileOut)
async def update_profile_route(
    profile_id: UUID,
    body: ProfileUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("health.admin")),
) -> ProfileOut:
    """Any write bumps `policy_version` (§2.1) -- including a plain rename, so
    the reconcile below re-pushes even when the check set did not move. That is
    correct: `tick_interval_s` is part of the pushed policy, and a version an
    agent has not acknowledged is a version §0.8's open predicate treats as
    drift."""
    envelopes: list[Envelope] = []
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            profile = await update_profile(
                session, ctx.org_id, ctx.user_id, profile_id, body.supplied()
            )
            out = await _profile_out(session, ctx.org_id, profile)
            envelopes = await _reconcile(session, request, ctx.org_id)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    # Transaction committed: safe to hand the signed jobs to the agents.
    await _publish(request, envelopes)
    return out


@router.delete("/health/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile_route(
    profile_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("health.admin")),
) -> None:
    """Refused with 409 -- naming the blockers -- while an assignment or an alert
    still references it (§10). `active=false` is the retirement path for a
    profile that has alarmed: resolution only considers ACTIVE profiles, so
    deactivating takes it out of service while leaving every alert intact."""
    envelopes: list[Envelope] = []
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await delete_profile(session, ctx.org_id, ctx.user_id, profile_id)
            envelopes = await _reconcile(session, request, ctx.org_id)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    await _publish(request, envelopes)


# --------------------------------------------------------------------------- #
# POST /health/profiles/{profile_id}/clone (§10, §0.2)
# --------------------------------------------------------------------------- #


@router.post(
    "/health/profiles/{profile_id}/clone",
    status_code=status.HTTP_201_CREATED,
    response_model=ProfileOut,
)
async def clone_profile_route(
    profile_id: UUID,
    body: ProfileClone,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("health.admin")),
) -> ProfileOut:
    """The clone carries NO assignment (§0.2) -- a clone that inherited one
    would silently retarget the source's devices at a profile nobody has
    reviewed -- so, as with create, no device's effective policy moved and
    nothing is pushed."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            clone = await clone_profile(
                session, ctx.org_id, ctx.user_id, profile_id, name=body.name
            )
            out = await _profile_out(session, ctx.org_id, clone)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# GET/PUT /health/profiles/{profile_id}/checks (§10, §2.4's amendment)
# --------------------------------------------------------------------------- #


@router.get("/health/profiles/{profile_id}/checks", response_model=ProfileChecksOut)
async def list_checks_route(
    profile_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.read")),
) -> ProfileChecksOut:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            profile = await get_profile(session, ctx.org_id, profile_id)
            checks = await list_checks(session, ctx.org_id, profile_id)
            out = ProfileChecksOut(
                profile_id=profile.id,
                policy_version=profile.policy_version,
                tick_interval_s=profile.tick_interval_s,
                checks=[_check_out(check, profile) for check in checks],
            )
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.put("/health/profiles/{profile_id}/checks", response_model=ProfileChecksOut)
async def replace_checks_route(
    profile_id: UUID,
    body: ChecksReplace,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("health.admin")),
) -> ProfileChecksOut:
    """Checks are replaced as a SET, never individually (§10).

    A profile's checks only make sense together, and a partial edit can leave a
    profile that no longer matches the `policy_version` an agent acknowledged.
    A check DROPPED by this body is RETIRED, not deleted, when it has ever
    alarmed: its open alerts are cleared `policy_removed`, `retired_at` is
    stamped, and the alert rows survive with a resolvable `check_id` -- which is
    what makes §10's "deleting a check leaves existing alerts intact" true
    rather than aspirational.
    """
    envelopes: list[Envelope] = []
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await replace_checks(
                session,
                ctx.org_id,
                ctx.user_id,
                profile_id,
                [check.supplied() for check in body.checks],
            )
            profile = await get_profile(session, ctx.org_id, profile_id)
            checks = await list_checks(session, ctx.org_id, profile_id)
            out = ProfileChecksOut(
                profile_id=profile.id,
                policy_version=profile.policy_version,
                tick_interval_s=profile.tick_interval_s,
                checks=[_check_out(check, profile) for check in checks],
            )
            envelopes = await _reconcile(session, request, ctx.org_id)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    await _publish(request, envelopes)
    return out


# --------------------------------------------------------------------------- #
# GET/POST /health/assignments, DELETE /health/assignments/{id} (§2.3, §0.2)
# --------------------------------------------------------------------------- #


@router.get("/health/assignments", response_model=list[AssignmentOut])
async def list_assignments_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.read")),
    profile_id: UUID | None = Query(default=None),
) -> list[AssignmentOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_assignments(session, ctx.org_id, profile_id=profile_id)
        names = {
            profile.id: profile.name for profile in await list_profiles(session, ctx.org_id)
        }
        return [_assignment_out(row, names.get(row.profile_id, "")) for row in rows]


@router.post(
    "/health/assignments", status_code=status.HTTP_201_CREATED, response_model=AssignmentOut
)
async def create_assignment_route(
    body: AssignmentCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.admin")),
) -> AssignmentOut:
    """One assignment per resolution level, enforced by three PARTIAL unique
    indexes (§0.2) -- NULLs do not collide in a plain unique constraint, so
    without them "which of these two applies" would be a tiebreak nobody
    chose."""
    envelopes: list[Envelope] = []
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            row = await create_assignment(
                session,
                ctx.org_id,
                ctx.user_id,
                profile_id=body.profile_id,
                device_id=body.device_id,
                device_group_id=body.device_group_id,
            )
            profile = await get_profile(session, ctx.org_id, row.profile_id)
            out = _assignment_out(row, profile.name)
            envelopes = await _reconcile(session, request, ctx.org_id)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    await _publish(request, envelopes)
    return out


@router.delete(
    "/health/assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_assignment_route(
    assignment_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.admin")),
) -> None:
    """The devices this covered fall through to the next resolution level, or to
    no profile at all -- in which case the reconcile sends an EXPLICIT DISARM
    (`push_policy`'s empty check list), because "run nothing" must be an
    instruction rather than silence (§5.2)."""
    envelopes: list[Envelope] = []
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await delete_assignment(session, ctx.org_id, ctx.user_id, assignment_id)
            envelopes = await _reconcile(session, request, ctx.org_id)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    await _publish(request, envelopes)


# --------------------------------------------------------------------------- #
# GET /health/check-types (§4.1)
# --------------------------------------------------------------------------- #


@router.get("/health/check-types", response_model=list[CheckTypeOut])
async def list_check_types_route(
    ctx: AuthContext = Depends(require_permission("health.read")),
) -> list[CheckTypeOut]:
    """The CLOSED registry (§0.1, §4.1), including its per-OS availability.

    Served from the process rather than from a table because that is what it is:
    adding a check type is a code change in BOTH repos plus an edit to §4.1,
    deliberately not a configuration row. The `os_families` set is what lets the
    profile editor show, before saving, that a BitLocker check will report
    `unavailable` on a Linux estate.

    It touches no tenant data, so it opens no `org_session` -- but it is still
    gated on `health.read`, because the registry describes what this platform
    can be made to watch and that is not public.
    """
    return [
        CheckTypeOut(
            name=entry.name,
            value_kind=entry.value_kind,
            cadence=entry.cadence,
            evaluated_by=entry.evaluated_by,
            os_families=sorted(entry.os_families),
            legal_comparators=sorted(entry.legal_comparators),
            selector_meaning=tr_marked(entry.selector_meaning),
            enum_values=list(entry.enum_values),
        )
        for entry in sorted(CHECK_TYPES.values(), key=lambda e: e.name)
    ]


# --------------------------------------------------------------------------- #
# GET /health/settings, PUT /health/settings (§2.8)
# --------------------------------------------------------------------------- #


@router.get("/health/settings", response_model=SettingsOut)
async def get_settings_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.read")),
) -> SettingsOut:
    """EFFECTIVE settings, and whether the org has ever configured any.

    This GET WRITES NOTHING: `settings.get_settings` returns the defaults for an
    org with no row rather than lazily inserting one, so a `health.read` request
    never becomes a write.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        effective = await get_settings(session, ctx.org_id)
        row = await get_settings_row(session, ctx.org_id)
        return SettingsOut(
            **{
                field: getattr(effective, field)
                for field in SettingsOut.model_fields
                if hasattr(effective, field)
            },
            configured=row is not None,
            alert_tickets_enabled=(
                effective.default_alert_queue_id is not None
                and effective.default_ticket_category_id is not None
            ),
            updated_at=row.updated_at if row is not None else None,
            updated_by=row.updated_by if row is not None else None,
        )


@router.put("/health/settings", response_model=SettingsOut)
async def update_settings_route(
    body: SettingsUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("health.admin")),
) -> SettingsOut:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            row = await update_settings(session, ctx.org_id, ctx.user_id, body.supplied())
            effective = await get_settings(session, ctx.org_id)
            out = SettingsOut(
                **{
                    field: getattr(effective, field)
                    for field in SettingsOut.model_fields
                    if hasattr(effective, field)
                },
                configured=True,
                alert_tickets_enabled=(
                    effective.default_alert_queue_id is not None
                    and effective.default_ticket_category_id is not None
                ),
                updated_at=row.updated_at,
                updated_by=row.updated_by,
            )
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# GET /health/alerts, GET /health/alerts/{alert_id} (§2.4, §13)
# --------------------------------------------------------------------------- #


@router.get("/health/alerts", response_model=list[AlertOut])
async def list_alerts_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.read")),
    state: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    device_id: UUID | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[AlertOut]:
    """The console list (§13), newest first and bounded.

    `state` and `severity` are plain strings rather than `Literal`s: an unknown
    value returns an EMPTY list, which is the honest answer to "show me alerts
    in a state that does not exist", where a 422 would read as a server error to
    a UI that built the filter from a stale enum.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        alerts = await list_alerts(
            session,
            ctx.org_id,
            state=state,
            severity=severity,
            device_id=device_id,
            limit=limit,
        )
        devices = await _devices_by_id(session, ctx.org_id, (a.device_id for a in alerts))
        return [_alert_out(alert, devices.get(alert.device_id)) for alert in alerts]


@router.get("/health/alerts/summary", response_model=AlertSummaryOut)
async def alert_summary_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.read")),
) -> AlertSummaryOut:
    """The org's open-alert TOTALS (§13), for the console's health badge.

    WHY IT IS NOT `GET /health/alerts` WITH A COUNT ON IT. The list is a page
    -- 200 by default, 500 at most -- and its `state` filter is single-valued,
    so "open" (active OR acknowledged OR muted, §0.8) cannot be expressed
    against it at all. The shell therefore read one page and filtered it in the
    browser, which under-reports the moment a fleet has more open alerts than
    fit, and made the badge render "at least N" because N was the honest floor
    rather than the number. This route answers the number.

    THE COUNTING HAPPENS IN POSTGRES. `summarise_open_alerts` issues one
    `GROUP BY (state, severity)`; a handler that loaded the rows and counted
    them here would be the same defect one layer down, and the service's own
    tests assert the emitted SQL for that reason.

    REGISTERED BEFORE `/health/alerts/{alert_id}` and it must stay there --
    literal segments before `{id}` parameters, this module's route-ordering
    rule. `alert_id` is a `UUID`, so a `summary` matched as one is not a
    shadowed route, it is a 422 on a totals endpoint.

    GATED `health.read` like its neighbours: it exposes counts of rows the same
    principal may already list one by one.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        summary = await summarise_open_alerts(session, ctx.org_id)
    # The service returns plain `str` keys; the schema's `Literal`s are what
    # narrow them, and Pydantic VALIDATES that here rather than trusting it --
    # a state or severity outside the enums would raise on construction instead
    # of being served as a bucket no client knows how to render.
    return AlertSummaryOut(
        open=summary.open,
        by_state=summary.by_state,
        by_severity=summary.by_severity,
        worst_severity=summary.worst_severity,
    )


@router.get("/health/alerts/{alert_id}", response_model=AlertDetailOut)
async def get_alert_route(
    alert_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.read")),
) -> AlertDetailOut:
    """The full context (§13): the action rows and their states, the linked
    ticket, the predecessor's ticket NUMBER, and the monitoring deep link.

    The deep link is rendered here and FETCHED NOWHERE (§0.12): the server never
    follows it, so it is not an SSRF surface and does not touch
    `app/core/egress.py`.
    """
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            alert = await get_alert(session, ctx.org_id, alert_id)
            device = (
                await session.execute(
                    select(Device).where(
                        Device.org_id == ctx.org_id, Device.id == alert.device_id
                    )
                )
            ).scalar_one_or_none()
            profile = (
                await session.execute(
                    select(HealthProfile.name).where(
                        HealthProfile.org_id == ctx.org_id,
                        HealthProfile.id == alert.profile_id,
                    )
                )
            ).scalar_one_or_none()
            rows = list(
                (
                    await session.execute(
                        select(HealthAlertAction)
                        .where(
                            HealthAlertAction.org_id == ctx.org_id,
                            HealthAlertAction.alert_id == alert_id,
                        )
                        .order_by(HealthAlertAction.created_at, HealthAlertAction.id)
                    )
                )
                .scalars()
                .all()
            )
            approved = {
                action.id: action
                for action in (
                    await session.execute(
                        select(ApprovedAction).where(
                            ApprovedAction.org_id == ctx.org_id,
                            ApprovedAction.id.in_(
                                [r.approved_action_id for r in rows if r.approved_action_id]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            }
            scripts = {
                script.id: script
                for script in (
                    await session.execute(
                        select(Script).where(
                            Script.org_id == ctx.org_id,
                            Script.id.in_(
                                [a.script_id for a in approved.values() if a.script_id]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            }
            previous_ticket: str | None = None
            previous_opened: datetime | None = None
            if alert.previous_alert_id is not None:
                previous = (
                    await session.execute(
                        select(HealthAlert).where(
                            HealthAlert.org_id == ctx.org_id,
                            HealthAlert.id == alert.previous_alert_id,
                        )
                    )
                ).scalar_one_or_none()
                if previous is not None:
                    previous_opened = previous.opened_at
                    previous_ticket = await _ticket_number(
                        session, ctx.org_id, previous.ticket_id
                    )
            effective = await get_settings(session, ctx.org_id)
            template = effective.monitoring_deep_link_template
            # `device.hostname` is nullable since 0061. A deep link built with
            # the literal "None" in it points at nothing and is worse than no
            # link -- an operator clicks it once and stops trusting the field.
            deep_link = (
                template.replace("{hostname}", device.hostname)
                if template is not None and device is not None and device.hostname
                else None
            )
            base = _alert_out(alert, device)
            out = AlertDetailOut(
                **base.model_dump(),
                profile_name=profile or "",
                ticket_number=await _ticket_number(session, ctx.org_id, alert.ticket_id),
                previous_alert_ticket_number=previous_ticket,
                previous_alert_opened_at=previous_opened,
                monitoring_deep_link=deep_link,
                actions=[_action_row_out(row, approved, scripts) for row in rows],
            )
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# The three technician transitions (§0.8, §11.3) -- `health.alert.manage`
# --------------------------------------------------------------------------- #


@router.post("/health/alerts/{alert_id}/acknowledge", response_model=AlertOut)
async def acknowledge_alert_route(
    alert_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.alert.manage")),
) -> AlertOut:
    """`active -> acknowledged`: a named user takes ownership.

    WHAT IT DOES NOT DO IS THE WHOLE CONTENT OF THE STATE (§0.8). It does not
    stop the check, does not prevent auto-clear, and does not cancel a
    remediation already in flight. It suppresses re-notification and escalation.
    An acknowledged alert is still OPEN and is not a cleared one; the response
    carries both `state` and `cleared_reason` so nothing downstream has to guess.
    """
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            alert = await acknowledge(session, ctx.org_id, alert_id, actor_id=ctx.user_id)
            out = _alert_out(alert, await _device(session, ctx.org_id, alert.device_id))
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/health/alerts/{alert_id}/mute", response_model=AlertOut)
async def mute_alert_route(
    alert_id: UUID,
    body: MuteIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.alert.manage")),
) -> AlertOut:
    """`active|acknowledged -> muted`, bounded to 30 days from `opened_at`.

    A MUTED ALERT IS STILL OPEN. The bound is refused here with a typed 400 and
    again by `ck_health_alert_mute_bounded`, because unbounded mutes are how a
    fleet accumulates permanently silent alarms everyone believes are armed.
    """
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            alert = await mute(
                session,
                ctx.org_id,
                alert_id,
                actor_id=ctx.user_id,
                muted_until=body.muted_until,
            )
            out = _alert_out(alert, await _device(session, ctx.org_id, alert.device_id))
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/health/alerts/{alert_id}/close", response_model=AlertOut)
async def close_alert_route(
    alert_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.alert.manage")),
) -> AlertOut:
    """§0.8 clause 6. `cleared_reason='manual'` and `cleared_by` are what
    separate this from every machine-driven clear in reporting: an alert a human
    closed is not evidence that anything was fixed."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            alert = await close(session, ctx.org_id, alert_id, actor_id=ctx.user_id)
            out = _alert_out(alert, await _device(session, ctx.org_id, alert.device_id))
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# Remediation (§8.3, §10) -- `health.remediate`, NEVER `health.admin`
# --------------------------------------------------------------------------- #


@router.post("/health/alerts/{alert_id}/remediate", response_model=JobDispatchedOut)
async def remediate_alert_route(
    alert_id: UUID,
    body: RemediateIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.remediate")),
) -> JobDispatchedOut:
    """Dispatch an approved action against this alert's device, by hand.

    THE FUSES APPLY TO A HUMAN TOO (§8.4): the real fleet-wide risk is 4,000
    device-scoped jobs in one minute, and a manual path exempt from the fuses
    reproduces it one click at a time. The autonomy dial does NOT apply -- the
    human IS the approval -- but every other refusal does, including the
    script-version pin.

    **THIS ROUTE'S GATE IS THE FLOOR, NOT THE WHOLE CHECK (§20).**
    `health.remediate` is checked by the dependency; the permission that
    `body.action_key`'s resolved JOB TYPE requires on its own module route is
    checked inside `dispatch_manual_remediation`, and `None` from
    `dispatch_permission_for` means refuse. It CANNOT be a route dependency: the
    permission required depends on the request body, which no
    `Depends(require_permission(...))` can see -- so `route_permission`'s
    router-derived completeness test will keep seeing only `health.remediate`
    here, exactly as it does for `POST /ai/actions/{id}/confirm`, and that is
    correct rather than a gap.
    """
    envelope: Envelope | None = None
    device_id: UUID
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            alert = await get_alert(session, ctx.org_id, alert_id)
            device_id = alert.device_id
            job_id, signed = await dispatch_manual_remediation(
                session,
                ctx.org_id,
                alert_id,
                action_key=body.action_key,
                actor_id=ctx.user_id,
                permissions=await user_permission_codes(session, ctx.user_id),
                job_engine=request.app.state.job_engine,
            )
            envelope = (job_id, signed)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    await _publish(request, [envelope])
    return JobDispatchedOut(job_id=envelope[0], device_id=device_id)


@router.post(
    "/health/alerts/{alert_id}/actions/{action_id}/approve", response_model=JobDispatchedOut
)
async def approve_action_route(
    alert_id: UUID,
    action_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.remediate")),
) -> JobDispatchedOut:
    """Approve an `awaiting_approval` remediation and dispatch it.

    The approver's id is carried into `jobs.requested_by` AND into the
    `health.remediation_dispatched` audit record: the kernel's own
    `jobs.dispatched` says a job was sent, and this one says why a machine
    decided to send it, with a name on it.

    **IDENTICAL SHAPE TO THE REMEDIATE ROUTE, AND THE SAME SECOND GATE (§20).**
    The row this approves names an approved action whose job type has its own
    module route; `approve_action` requires that route's permission too. A
    console that gated only the dispatch route and not this one would leave the
    escalation intact through one extra click.
    """
    envelope: Envelope | None = None
    device_id: UUID
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            alert = await get_alert(session, ctx.org_id, alert_id)
            device_id = alert.device_id
            job_id, signed = await approve_action(
                session,
                ctx.org_id,
                alert_id,
                action_id,
                actor_id=ctx.user_id,
                permissions=await user_permission_codes(session, ctx.user_id),
                job_engine=request.app.state.job_engine,
            )
            envelope = (job_id, signed)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    await _publish(request, [envelope])
    return JobDispatchedOut(job_id=envelope[0], device_id=device_id)


@router.post(
    "/health/alerts/{alert_id}/actions/{action_id}/refuse", response_model=AlertActionOut
)
async def refuse_action_route(
    alert_id: UUID,
    action_id: UUID,
    body: AlertRefuseIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.remediate")),
) -> AlertActionOut:
    """Refuse it. The row is `skipped`, forever: `UNIQUE (alert_id, kind)` means
    there is no second attempt to make."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            row = await refuse_action(
                session,
                ctx.org_id,
                alert_id,
                action_id,
                actor_id=ctx.user_id,
                reason=body.reason,
            )
            action = (
                (
                    await session.execute(
                        select(ApprovedAction).where(
                            ApprovedAction.org_id == ctx.org_id,
                            ApprovedAction.id == row.approved_action_id,
                        )
                    )
                ).scalar_one_or_none()
                if row.approved_action_id is not None
                else None
            )
            script = (
                (
                    await session.execute(
                        select(Script).where(
                            Script.org_id == ctx.org_id, Script.id == action.script_id
                        )
                    )
                ).scalar_one_or_none()
                if action is not None and action.script_id is not None
                else None
            )
            out = _action_out(row, action, script)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# GET /devices/{device_id}/health -- gated on `devices.read` (§10, §13)
# --------------------------------------------------------------------------- #


@router.get("/devices/{device_id}/health", response_model=DeviceHealthOut)
async def device_health_route(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
) -> DeviceHealthOut:
    """This device's effective profile, WHICH ASSIGNMENT PRODUCED IT, every
    check's current state, and `policy_version` drift (§13, §0.2).

    `devices.read`, NOT `health.read` (§10) -- a technician looking at a device
    needs its health without holding health administration rights.

    THREE THINGS THIS RESPONSE REFUSES TO CONFLATE.
    1. No effective profile is `has_profile=false`, never an empty check list
       that reads as healthy. §3 leaves such a device monitoring NOTHING.
    2. A check with no `device_health_check_state` row is `unknown`, never `ok`.
       Absent is not healthy (§0.7).
    3. `agent_online` is reported beside the check states, because an offline
       device does not clear its alerts (§0.6): a quiet device is not a healthy
       one, and the open alerts on this response are exactly the ones that
       survived it going quiet.
    """
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            device = await _device(session, ctx.org_id, device_id)
            resolved = await resolve_profile_for_device(session, ctx.org_id, device)
            effective = await get_settings(session, ctx.org_id)

            ack = (
                await session.execute(
                    select(DeviceHealthPolicyAck).where(
                        DeviceHealthPolicyAck.org_id == ctx.org_id,
                        DeviceHealthPolicyAck.device_id == device_id,
                    )
                )
            ).scalar_one_or_none()
            states = {
                (row.check_id, row.selector): row
                for row in (
                    await session.execute(
                        select(DeviceHealthCheckState).where(
                            DeviceHealthCheckState.org_id == ctx.org_id,
                            DeviceHealthCheckState.device_id == device_id,
                        )
                    )
                )
                .scalars()
                .all()
            }
            check_states: list[DeviceCheckStateOut] = []
            if resolved is not None:
                for check in resolved.checks:
                    stored = states.get((check.id, check.selector))
                    entry = CHECK_TYPES.get(check.check_type)
                    check_states.append(
                        DeviceCheckStateOut(
                            check_id=check.id,
                            check_type=check.check_type,
                            selector=check.selector,
                            comparator=check.comparator,
                            threshold_display=_threshold_display(check),
                            severity=check.severity,
                            for_ticks=check.for_ticks,
                            for_seconds=effective_for_seconds(check, resolved.profile),
                            evaluated_by=entry.evaluated_by if entry else "agent",
                            state=stored.state if stored is not None else "unknown",
                            last_value=stored.last_value if stored is not None else "",
                            policy_version=stored.policy_version if stored is not None else None,
                            reported_at=stored.reported_at if stored is not None else None,
                        )
                    )

            open_alerts = list(
                (
                    await session.execute(
                        select(HealthAlert)
                        .where(
                            HealthAlert.org_id == ctx.org_id,
                            HealthAlert.device_id == device_id,
                            HealthAlert.state != "cleared",
                        )
                        .order_by(HealthAlert.opened_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            last_seen = (
                await session.execute(
                    select(Agent.last_seen_at).where(
                        Agent.org_id == ctx.org_id, Agent.device_id == device_id
                    )
                )
            ).scalar_one_or_none()
            now = datetime.now(UTC)
            online = (
                last_seen is not None
                and (now - last_seen).total_seconds() < effective.offline_after_s
            )
            out = DeviceHealthOut(
                device_id=device.id,
                hostname=device.hostname,
                os_family=device.os_family,
                has_profile=resolved is not None,
                profile=(
                    await _profile_out(session, ctx.org_id, resolved.profile)
                    if resolved is not None
                    else None
                ),
                assignment=(
                    _assignment_out(resolved.assignment, resolved.profile.name)
                    if resolved is not None
                    else None
                ),
                resolution_level=resolved.level if resolved is not None else None,
                acknowledged_policy_version=ack.policy_version if ack is not None else None,
                policy_drift=(
                    resolved is not None
                    and (ack is None or ack.policy_version != resolved.profile.policy_version)
                ),
                checks=check_states,
                open_alerts=[_alert_out(alert, device) for alert in open_alerts],
                agent_last_seen_at=last_seen,
                agent_online=online,
                offline_after_s=effective.offline_after_s,
            )
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


# §5.3's amendment, and §10's. One `health.check.now` per device per minute
# bounds a loop pointed at ONE device; the hourly org ceiling bounds a loop
# pointed at the fleet. Both are far above a human pressing "refresh now" --
# nothing in this module dispatches `health.check.now` automatically, so the
# console button is the only producer and these numbers only ever bind a script.
CHECK_NOW_DEVICE_COOLDOWN_S = 60
CHECK_NOW_ORG_PER_HOUR = 500


async def _check_now_fuse(
    session: AsyncSession, org_id: UUID, device_id: UUID, *, now: datetime
) -> str | None:
    """The `check-now` fuse, or `None` when it passes.

    COUNTED AGAINST `jobs`, not against a counter (§2.9, and
    `actions.py::_fuse_breach`'s docstring at length): `jobs` is already the
    durable record of every dispatch, so it cannot disagree with what actually
    reached a device, and it does not evaporate when Redis is flushed. The
    consequence is stated rather than papered over, exactly as it is for the
    remediation fuses -- the predicate is `(org_id, type, created_at)`, so a
    `health.check.now` job minted by ANY caller consumes the org budget,
    including one dispatched on another operator's behalf. That is the point:
    the fuse bounds what reaches ENDPOINTS, not what one session may ask for.
    """
    recent_here = (
        await session.execute(
            select(Job.id)
            .where(
                Job.org_id == org_id,
                Job.type == CHECK_NOW_JOB_TYPE,
                Job.target_id == device_id,
                Job.created_at > now - timedelta(seconds=CHECK_NOW_DEVICE_COOLDOWN_S),
            )
            .limit(1)
        )
    ).first()
    if recent_here is not None:
        return tr(
            "this device was already asked to sample inside the last {cooldown_s}s (job "
            "{job_id}); the report it is already producing answers this request",
            cooldown_s=CHECK_NOW_DEVICE_COOLDOWN_S,
            job_id=recent_here[0],
        )

    per_org = (
        await session.execute(
            select(Job.id)
            .where(
                Job.org_id == org_id,
                Job.type == CHECK_NOW_JOB_TYPE,
                Job.created_at > now - timedelta(hours=1),
            )
            .limit(CHECK_NOW_ORG_PER_HOUR)
        )
    ).all()
    if len(per_org) >= CHECK_NOW_ORG_PER_HOUR:
        return tr(
            "{count} {job_type} job(s) have already been dispatched across this "
            "organization in the last hour (limit {limit})",
            count=len(per_org),
            job_type=CHECK_NOW_JOB_TYPE,
            limit=CHECK_NOW_ORG_PER_HOUR,
        )
    return None


@router.post("/devices/{device_id}/health/check-now", response_model=JobDispatchedOut)
async def check_now_route(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.alert.manage")),
) -> JobDispatchedOut:
    """§5.3. Ask the device to sample its checks now and report.

    Unconditional as to POLICY, unlike the policy push: "sample now" is
    meaningful even for a device running an empty policy -- the summary it
    produces is what proves the policy is installed. It is NOT unconditional as
    to VOLUME; see `_check_now_fuse`, and the module docstring for why the gate
    moved off `health.read` at the same time.
    """
    envelope: Envelope | None = None
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            device = await _device(session, ctx.org_id, device_id)
            breach = await _check_now_fuse(
                session, ctx.org_id, device_id, now=datetime.now(UTC)
            )
            if breach is not None:
                # Raised INSIDE the block deliberately: it must roll back, and
                # there is nothing yet written to lose. `_http_error` is for
                # `HealthValidationError`s and a fuse is not a malformed
                # request, so this is a 429 rather than a 400.
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, breach)
            job_id, signed = await push_check_now(
                session,
                request.app.state.job_engine,
                ctx.org_id,
                device,
                requested_by=ctx.user_id,
            )
            envelope = (job_id, signed)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    await _publish(request, [envelope])
    return JobDispatchedOut(job_id=envelope[0], device_id=device_id)


# --------------------------------------------------------------------------- #
# The approved-action registry (§2.9, §8) -- `health.action.admin` to write
# --------------------------------------------------------------------------- #


@router.get("/approved-actions", response_model=list[ApprovedActionOut])
async def list_approved_actions_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.read")),
) -> list[ApprovedActionOut]:
    """The registry, with `destructive`, `risk_class`, `autonomy` and each pin's
    exemption. Readable with `health.read` and writable only with
    `health.action.admin`: seeing what may run automatically is not the same
    grant as deciding it."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_approved_actions(session, ctx.org_id)
        return [await _approved_action_out(session, ctx.org_id, row) for row in rows]


@router.post(
    "/approved-actions",
    status_code=status.HTTP_201_CREATED,
    response_model=ApprovedActionOut,
)
async def create_approved_action_route(
    body: ApprovedActionCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("health.action.admin")),
) -> ApprovedActionOut:
    """`destructive` is DERIVED and stored, never taken from the body (§8.4).

    A body carrying it is a 422 from `ApprovedActionCreate`'s `extra="forbid"`,
    and would be a 400 from `_validate_and_derive` for any non-HTTP caller. Both
    are refusals; neither is a silent drop, because a caller who believes they
    set `destructive: false` and got a 200 believes they disarmed the autonomy
    gate.
    """
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            action = await create_approved_action(
                session, ctx.org_id, ctx.user_id, body.supplied()
            )
            out = await _approved_action_out(session, ctx.org_id, action)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/approved-actions/{action_id}", response_model=ApprovedActionOut)
async def get_approved_action_route(
    action_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("health.read")),
) -> ApprovedActionOut:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            action = await get_action(session, ctx.org_id, action_id)
            out = await _approved_action_out(session, ctx.org_id, action)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.patch("/approved-actions/{action_id}", response_model=ApprovedActionOut)
async def update_approved_action_route(
    action_id: UUID,
    body: ApprovedActionUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("health.action.admin")),
) -> ApprovedActionOut:
    """Every autonomy change is audited with its OLD and new value (§11.3):
    "who set this action to autonomous" is only answerable if the previous value
    is in the audit row, since the new one is already in the table."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            action = await update_approved_action(
                session, ctx.org_id, action_id, ctx.user_id, body.supplied()
            )
            out = await _approved_action_out(session, ctx.org_id, action)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.delete("/approved-actions/{action_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_approved_action_route(
    action_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("health.action.admin")),
) -> None:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await delete_approved_action(session, ctx.org_id, action_id, ctx.user_id)
    except HealthValidationError as exc:
        raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# POST/DELETE /approved-actions/{id}/exempt-script (§2.10, §8.4 clause 2)
# --------------------------------------------------------------------------- #


@router.post("/approved-actions/{action_id}/exempt-script", response_model=ApprovedActionOut)
async def exempt_script_route(
    action_id: UUID,
    body: ExemptScriptIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("health.action.admin")),
) -> ApprovedActionOut:
    """"This exact script body is not destructive", granted per exact version.

    THE PAIR COMES FROM THE ACTION'S OWN PIN, NOT FROM THE BODY (§10), so an
    exemption can never be granted for a version the action does not name.
    Refused with 400 when the action is not `script.run`, and when the pinned
    version no longer matches the live `scripts.version` -- exempting a version
    that has already been edited away is a no-op that reads as a grant.

    Returns the ACTION rather than the exemption, because granting one
    re-derives `destructive` on the action and that re-derivation is the
    consequence the caller needs to see.
    """
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await grant_script_exemption(
                session, ctx.org_id, action_id, actor_id=ctx.user_id, reason=body.reason
            )
            action = await get_action(session, ctx.org_id, action_id)
            out = await _approved_action_out(session, ctx.org_id, action)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.delete("/approved-actions/{action_id}/exempt-script", response_model=ApprovedActionOut)
async def revoke_script_exemption_route(
    action_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("health.action.admin")),
) -> ApprovedActionOut:
    """The matching DELETE (§10's prose; the table lists only the POST).

    Revoking RE-DERIVES `destructive`, which is what makes the revoke
    meaningful: an exemption removed without re-deriving would leave the action
    stored as non-destructive and therefore still eligible for `autonomous`. If
    that re-derivation makes it destructive, the service DEMOTES the autonomy to
    `require_approval` with a logged reason rather than letting
    `ck_approved_action_autonomous_is_bounded` produce a 500 -- so the response
    body is the action, and it is worth reading.
    """
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await revoke_script_exemption(session, ctx.org_id, action_id, actor_id=ctx.user_id)
            action = await get_action(session, ctx.org_id, action_id)
            out = await _approved_action_out(session, ctx.org_id, action)
    except HealthValidationError as exc:
        raise _http_error(exc) from None
    return out
