"""The health API: routes, RBAC completeness, tenancy, and the four refusals
that only exist at this layer (`docs/modules/endpoint-health-alerting/spec.md`
§10, §11; plan Task 13).

ROUTE DISCOVERY IS FROM THE APP, NOT FROM THE MODULE ROUTER, AND NOT FROM A
HAND-MAINTAINED LIST. `FastAPI.routes` IS `self.router.routes` (the same list
object) in FastAPI 0.139.2, and `include_router` appends a single lazy
`_IncludedRouter` wrapper whose own `.path` is `None` -- so filtering EITHER
collection by a path prefix yields an empty list forever, and every RBAC
assertion built on it passes over nothing. `_flatten` below descends through
`_IncludedRouter.original_router` and re-applies `include_context.prefix`,
which is the working flattener `tests/test_email_intake_api.py` established and
`tests/test_sla_api.py` carried. `EXPECTED_ROUTES` pins the EXACT (method, path)
-> permission MAP, not a count and not a `>=`: a superset assertion cannot fail
when a route goes missing, and a count cannot fail when a route is gated on the
wrong code.

THE PARAMETER EVERY FIXTURE HERE DELIBERATELY VARIES, because this project's
recurring defect is a fixture that holds constant the thing a bug would change:

* **Which permission the caller holds.** `_holder` mints a user with EXACTLY
  the codes named. `api_env.create_user(org_id, "org-admin")` is never used for
  a gate assertion: an org-admin holds all five health codes plus
  `devices.read`, so every gate looks identical to them and a route wired to
  the wrong code passes. The two sharp pairs are asserted in single tests: a
  `health.admin` holder gets 403 on `POST /alerts/{id}/remediate` and 200 on
  `PATCH /health/profiles/{id}`; a `health.remediate` holder gets 403 on
  `POST /approved-actions` and 200 on the remediate route.
* **Whether the target row belongs to another org.** Every 404 test seeds the
  row in a DIFFERENT org and then asserts ON THE ADMIN ENGINE that it really
  exists -- otherwise "404 because it is not yours" and "404 because there is
  nothing there" are the same test, and the second passes against an
  implementation with no tenancy predicate at all.
* **Whether the device has a profile, and whether it has answered.** The device
  view is asserted against a device with a profile whose checks have NO
  `device_health_check_state` row beside one that has answered, so "everything
  is unknown" and "everything is ok" each fail.
* **Whether `destructive` would be derived TRUE.** The `destructive`-is-derived
  test uses `patch.apply` + `reboot: "forced"`, which derives TRUE, and sends
  `destructive: false` in the body. With a job type that derives FALSE the
  refusal and the bug produce the same stored row.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import PERMISSIONS, route_permission
from app.models import (
    Agent,
    ApprovedAction,
    ApprovedActionScriptExemption,
    AuditEvent,
    DeviceHealthPolicyAck,
    HealthAlert,
    HealthCheck,
    HealthOrgSettings,
    HealthProfile,
    HealthProfileAssignment,
    Job,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.modules.health.errors import HealthNotFoundError
from app.modules.health.settings import SETTING_BOUNDS
from tests.health_action_fixtures import (
    seed_action_row,
    seed_approved_action,
    seed_script,
    seed_ticketing,
)
from tests.health_fixtures import (
    seed_agent,
    seed_alert,
    seed_assignment,
    seed_check,
    seed_device,
    seed_org,
    seed_profile,
)

HEALTH = "/api/v1/health"
DEVICE_HEALTH = "/api/v1/devices/{device_id}/health"
DEVICE_CHECK_NOW = "/api/v1/devices/{device_id}/health/check-now"
ACTIONS = "/api/v1/approved-actions"

# Spec §10's table, expanded one entry per METHOD and fully prefixed, mapped to
# the permission each route must be gated on. GET/PATCH/DELETE share a path and
# each needs its own gate, so the key is (method, path) rather than path alone.
#
# Two entries are deliberate deviations from this module's own read/admin pair
# and are the two most worth pinning: `GET /devices/{id}/health` takes
# `devices.read` (§10 -- a technician looking at a device needs its health
# without holding health administration rights), and the exempt-script DELETE
# comes from §10's PROSE ("the matching DELETE revokes it") rather than from its
# table, which lists only the POST.
EXPECTED_ROUTES: dict[tuple[str, str], str] = {
    ("GET", f"{HEALTH}/profiles"): "health.read",
    ("POST", f"{HEALTH}/profiles"): "health.admin",
    ("GET", f"{HEALTH}/profiles/{{profile_id}}"): "health.read",
    ("PATCH", f"{HEALTH}/profiles/{{profile_id}}"): "health.admin",
    ("DELETE", f"{HEALTH}/profiles/{{profile_id}}"): "health.admin",
    ("POST", f"{HEALTH}/profiles/{{profile_id}}/clone"): "health.admin",
    ("GET", f"{HEALTH}/profiles/{{profile_id}}/checks"): "health.read",
    ("PUT", f"{HEALTH}/profiles/{{profile_id}}/checks"): "health.admin",
    ("GET", f"{HEALTH}/assignments"): "health.read",
    ("POST", f"{HEALTH}/assignments"): "health.admin",
    ("DELETE", f"{HEALTH}/assignments/{{assignment_id}}"): "health.admin",
    ("GET", f"{HEALTH}/check-types"): "health.read",
    ("GET", f"{HEALTH}/alerts"): "health.read",
    ("GET", f"{HEALTH}/alerts/summary"): "health.read",
    ("GET", f"{HEALTH}/alerts/{{alert_id}}"): "health.read",
    ("POST", f"{HEALTH}/alerts/{{alert_id}}/acknowledge"): "health.alert.manage",
    ("POST", f"{HEALTH}/alerts/{{alert_id}}/mute"): "health.alert.manage",
    ("POST", f"{HEALTH}/alerts/{{alert_id}}/close"): "health.alert.manage",
    ("POST", f"{HEALTH}/alerts/{{alert_id}}/remediate"): "health.remediate",
    (
        "POST",
        f"{HEALTH}/alerts/{{alert_id}}/actions/{{action_id}}/approve",
    ): "health.remediate",
    (
        "POST",
        f"{HEALTH}/alerts/{{alert_id}}/actions/{{action_id}}/refuse",
    ): "health.remediate",
    ("GET", f"{HEALTH}/settings"): "health.read",
    ("PUT", f"{HEALTH}/settings"): "health.admin",
    ("GET", DEVICE_HEALTH): "devices.read",
    ("POST", DEVICE_CHECK_NOW): "health.alert.manage",
    ("GET", ACTIONS): "health.read",
    ("POST", ACTIONS): "health.action.admin",
    ("GET", f"{ACTIONS}/{{action_id}}"): "health.read",
    ("PATCH", f"{ACTIONS}/{{action_id}}"): "health.action.admin",
    ("DELETE", f"{ACTIONS}/{{action_id}}"): "health.action.admin",
    ("POST", f"{ACTIONS}/{{action_id}}/exempt-script"): "health.action.admin",
    ("DELETE", f"{ACTIONS}/{{action_id}}/exempt-script"): "health.action.admin",
}

ALL_HEALTH_CODES = (
    "health.read",
    "health.admin",
    "health.alert.manage",
    "health.remediate",
    "health.action.admin",
)


# --------------------------------------------------------------------------- #
# Router flattening (see the module docstring).
# --------------------------------------------------------------------------- #


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """`(full path, route)` for every real route reachable from `router`.

    `include_router` in this FastAPI appends a `_IncludedRouter` wrapper rather
    than flattening; the wrapper exposes the router it wrapped as
    `.original_router` and the prefix it was mounted under as
    `.include_context.prefix`. Descend through both, recursively, so a router
    included into a router included into the app is still found.
    """
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(_flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found


def _health_routes(app: Any) -> list[tuple[str, Any]]:
    """Every route this module registered: the `/health` tree, the
    `approved-actions` tree, AND the two that hang off `/devices/{id}` -- which
    live outside both prefixes and are the two whose permissions differ from
    every other, so selecting on `/health` alone would silently drop exactly the
    routes most worth checking.

    `/livez` and `/readyz` are deliberately not matched: they are the kernel's
    unauthenticated liveness/readiness probes, they start with no `/api/v1`
    prefix, and pulling them in would make the completeness assertion below
    fail for routes that must NOT carry a permission.
    """
    return [
        (path, route)
        for path, route in _flatten(app.router)
        if path.startswith(f"{HEALTH}/")
        or path.startswith(f"{ACTIONS}")
        or path.startswith("/api/v1/devices/{device_id}/health")
    ]


def _method_paths(app: Any) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, route in _health_routes(app)
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }


# --------------------------------------------------------------------------- #
# Seeding.
# --------------------------------------------------------------------------- #


async def _holder(api_env: Any, org_id: UUID, *codes: str) -> dict[str, str]:
    """Headers for a user holding EXACTLY `codes` and nothing else.

    See the module docstring: an org-admin fixture makes every gate in this
    module look identical, and the read/admin/alert.manage/remediate/action.admin
    split is only observable to a caller who holds one code and not the others.
    """
    sub = f"sub-{uuid4().hex[:10]}"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        role = Role(org_id=org_id, name=f"role-{uuid4().hex[:8]}")
        session.add_all([user, role])
        await session.flush()
        for code in codes:
            permission_id = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
    return await api_env.headers(org_id, sub)


async def _everything(api_env: Any, org_id: UUID) -> dict[str, str]:
    """A caller holding all five health codes plus `devices.read` -- for tests
    whose subject is NOT the gate."""
    return await _holder(api_env, org_id, *ALL_HEALTH_CODES, "devices.read")


async def _profile_row(api_env: Any, profile_id: UUID) -> HealthProfile:
    """Read on the BYPASSRLS admin engine with no org filter, so an assertion
    about what was stored cannot be satisfied by RLS hiding the truth."""
    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(HealthProfile).where(HealthProfile.id == profile_id))
        ).scalar_one()


async def _alert_row(api_env: Any, alert_id: UUID) -> HealthAlert:
    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(HealthAlert).where(HealthAlert.id == alert_id))
        ).scalar_one()


async def _action_rows(api_env: Any, org_id: UUID) -> list[ApprovedAction]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(ApprovedAction).where(ApprovedAction.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )


async def _audit_actions(api_env: Any, org_id: UUID) -> list[str]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent.action)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def _jobs_for(api_env: Any, org_id: UUID) -> list[Job]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(Job).where(Job.org_id == org_id).order_by(Job.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _seed_agent(api_env: Any, org_id: UUID, device_id: UUID, last_seen_at: Any) -> None:
    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        session.add(
            Agent(
                org_id=org_id,
                device_id=device_id,
                cert_fingerprint=uuid4().hex,
                cert_serial=uuid4().hex[:16],
                cert_not_after=now + timedelta(days=365),
                enrolled_at=now,
                last_seen_at=last_seen_at,
            )
        )
        await session.commit()


def _cpu_check(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "check_type": "cpu.busy_pct",
        "comparator": "gt",
        "threshold_num": 95,
        "for_ticks": 3,
        "severity": "warning",
    }
    body.update(kw)
    return body


# --------------------------------------------------------------------------- #
# RBAC completeness, DERIVED from the router (invariant #6).
# --------------------------------------------------------------------------- #


async def test_the_routes_are_mounted_under_the_versioned_prefix(api_env) -> None:
    """Pins the FLATTENER as well as the route table.

    If `_flatten` ever stops finding real routes -- a FastAPI that changes
    `_IncludedRouter`'s shape, or a `register()` that is never called -- this
    fails loudly instead of letting every RBAC assertion below pass over an
    empty list. Equality, not `>=`.
    """
    assert _method_paths(api_env.app) == set(EXPECTED_ROUTES)


async def test_every_route_carries_a_permission_and_it_is_the_right_one(api_env) -> None:
    """COMPLETENESS DERIVED FROM THE ROUTER (`route_permission`), not from a
    list a later route addition could silently miss.

    Two assertions, and both are needed. The first is the completeness check: a
    route registered without `require_permission` returns `None` here and fails
    by existing, whatever module added it. The second pins WHICH code -- a route
    gated on `health.read` where §10 says `health.admin` is fully gated and
    completely wrong, and the first assertion cannot see it.

    GUARD OBSERVABILITY: delete `Depends(require_permission(...))` from any one
    route in `api.py` and this fails naming that exact (method, path).
    """
    found = {
        (method, path): route_permission(route)
        for path, route in _health_routes(api_env.app)
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    ungated = sorted(key for key, code in found.items() if code is None)
    assert ungated == [], f"routes with no permission dependency: {ungated}"
    assert found == EXPECTED_ROUTES


def test_every_permission_this_module_gates_on_is_a_registered_code() -> None:
    """A typo'd code is not a weaker gate, it is a 500 -- `has_permission`
    raises `ValueError` for an unknown code. Pinned here so the failure is a
    named test rather than a request-time crash on a route nobody exercised."""
    unknown = sorted({code for code in EXPECTED_ROUTES.values() if code not in PERMISSIONS})
    assert unknown == []


# --------------------------------------------------------------------------- #
# Profiles, checks, assignments: happy path + the 403.
# --------------------------------------------------------------------------- #


async def test_profile_crud_round_trip_and_the_policy_version_bumps(api_env) -> None:
    org = await seed_org(api_env, "hapi")
    headers = await _everything(api_env, org.org_id)

    created = await api_env.client.post(
        f"{HEALTH}/profiles",
        headers=headers,
        json={"name": "Fleet baseline", "tick_interval_s": 300},
    )
    assert created.status_code == 201, created.text
    profile_id = UUID(created.json()["id"])
    assert created.json()["policy_version"] == 1
    assert created.json()["check_count"] == 0
    assert created.json()["assignment_count"] == 0

    listed = await api_env.client.get(f"{HEALTH}/profiles", headers=headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [str(profile_id)]

    patched = await api_env.client.patch(
        f"{HEALTH}/profiles/{profile_id}", headers=headers, json={"name": "Renamed"}
    )
    assert patched.status_code == 200, patched.text
    # A plain RENAME bumps the version (§2.1): `tick_interval_s` is part of the
    # pushed policy, so "the checks did not change" is not "the policy did not
    # change".
    assert patched.json()["policy_version"] == 2
    assert (await _profile_row(api_env, profile_id)).name == "Renamed"

    fetched = await api_env.client.get(f"{HEALTH}/profiles/{profile_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Renamed"

    cloned = await api_env.client.post(
        f"{HEALTH}/profiles/{profile_id}/clone", headers=headers, json={"name": "Copy"}
    )
    assert cloned.status_code == 201, cloned.text
    assert cloned.json()["id"] != str(profile_id)
    # A clone carries NO assignment (§0.2) -- one that did would silently
    # retarget the source's devices at a profile nobody has reviewed.
    assert cloned.json()["assignment_count"] == 0

    deleted = await api_env.client.delete(f"{HEALTH}/profiles/{profile_id}", headers=headers)
    assert deleted.status_code == 204
    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(
                select(HealthProfile.id).where(HealthProfile.id == profile_id)
            )
        ).scalar_one_or_none() is None


async def test_a_reader_cannot_write_a_profile_and_an_admin_can(api_env) -> None:
    """The read/admin split, observed by a caller who holds ONE of the pair.

    Both halves in one test: with a fixture that only checked the 403, a route
    gated on a permission NOBODY holds would pass.
    """
    org = await seed_org(api_env, "hsplit")
    reader = await _holder(api_env, org.org_id, "health.read")
    admin = await _holder(api_env, org.org_id, "health.admin")

    refused = await api_env.client.post(
        f"{HEALTH}/profiles", headers=reader, json={"name": "nope"}
    )
    assert refused.status_code == 403
    allowed = await api_env.client.post(
        f"{HEALTH}/profiles", headers=admin, json={"name": "yes"}
    )
    assert allowed.status_code == 201
    # ... and the admin cannot READ it back, which is what proves the two codes
    # are genuinely separate rather than one implying the other.
    assert (await api_env.client.get(f"{HEALTH}/profiles", headers=admin)).status_code == 403
    assert (await api_env.client.get(f"{HEALTH}/profiles", headers=reader)).status_code == 200


async def test_checks_are_replaced_as_a_set_and_render_both_durations(api_env) -> None:
    org = await seed_org(api_env, "hchk")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id, tick_interval_s=300)

    put = await api_env.client.put(
        f"{HEALTH}/profiles/{profile_id}/checks",
        headers=headers,
        json={
            "checks": [
                _cpu_check(),
                {
                    "check_type": "disk.free_pct",
                    "selector": "*",
                    "comparator": "lt",
                    "threshold_num": 10,
                    "for_seconds": 900,
                    "severity": "critical",
                },
            ]
        },
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["tick_interval_s"] == 300
    by_type = {row["check_type"]: row for row in body["checks"]}
    assert set(by_type) == {"cpu.busy_pct", "disk.free_pct"}
    # §0.3: BOTH numbers, and `for_seconds` converted against THIS profile's
    # tick. 900s / 300s = 3 ticks.
    assert by_type["disk.free_pct"]["for_ticks"] == 3
    assert by_type["disk.free_pct"]["for_seconds"] == 900
    assert by_type["cpu.busy_pct"]["for_seconds"] == 900
    # The registry facts ride inline so the editor can show, before saving,
    # which OS families can answer the check (§4.1, §13).
    assert by_type["cpu.busy_pct"]["available_os_families"] == ["linux", "macos", "windows"]
    assert by_type["cpu.busy_pct"]["threshold_display"] == "95"

    # Replacing with a SUBSET removes the other; the profile's version moves.
    again = await api_env.client.put(
        f"{HEALTH}/profiles/{profile_id}/checks",
        headers=headers,
        json={"checks": [_cpu_check()]},
    )
    assert again.status_code == 200
    assert [row["check_type"] for row in again.json()["checks"]] == ["cpu.busy_pct"]
    assert again.json()["policy_version"] > body["policy_version"]

    listed = await api_env.client.get(
        f"{HEALTH}/profiles/{profile_id}/checks", headers=headers
    )
    assert [row["check_type"] for row in listed.json()["checks"]] == ["cpu.busy_pct"]


async def test_a_dropped_check_with_an_alert_is_retired_and_its_alert_is_cleared(
    api_env,
) -> None:
    """§2.4's amendment through the API: the alert row SURVIVES with a
    resolvable `check_id`, cleared `policy_removed`.

    Asserted on the ADMIN engine against the stored rows, not on the response:
    the response of a PUT that dropped a check says nothing about what happened
    to the alert, which is exactly the fact §10 promises.
    """
    org = await seed_org(api_env, "hretire")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    alert_id = await seed_alert(api_env, org, profile_id=profile_id, check_id=check_id)

    put = await api_env.client.put(
        f"{HEALTH}/profiles/{profile_id}/checks", headers=headers, json={"checks": []}
    )
    assert put.status_code == 200, put.text
    assert put.json()["checks"] == []

    alert = await _alert_row(api_env, alert_id)
    assert alert.state == "cleared"
    assert alert.cleared_reason == "policy_removed"
    assert alert.check_id == check_id
    async with AsyncSession(api_env.admin_engine) as session:
        check = (
            await session.execute(select(HealthCheck).where(HealthCheck.id == check_id))
        ).scalar_one()
    assert check.retired_at is not None


async def test_a_profile_that_is_still_assigned_is_a_409_naming_the_assignment(
    api_env,
) -> None:
    org = await seed_org(api_env, "h409")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)
    assignment_id = await seed_assignment(api_env, org.org_id, profile_id)

    refused = await api_env.client.delete(f"{HEALTH}/profiles/{profile_id}", headers=headers)
    assert refused.status_code == 409, refused.text
    assert str(assignment_id) in refused.json()["detail"]
    # And the profile is still there -- the refusal rolled nothing back because
    # nothing was written, but a handler that swallowed the error would have
    # returned 204 over a live row.
    assert (await _profile_row(api_env, profile_id)).id == profile_id


async def test_assignments_round_trip_and_name_their_resolution_level(api_env) -> None:
    org = await seed_org(api_env, "hassign")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)

    created = await api_env.client.post(
        f"{HEALTH}/assignments",
        headers=headers,
        json={"profile_id": str(profile_id), "device_id": str(org.device_id)},
    )
    assert created.status_code == 201, created.text
    # §0.2's level, named server-side rather than inferred by the console from
    # which id happens to be NULL.
    assert created.json()["level"] == "device"
    assignment_id = created.json()["id"]

    org_default = await api_env.client.post(
        f"{HEALTH}/assignments", headers=headers, json={"profile_id": str(profile_id)}
    )
    assert org_default.status_code == 201
    assert org_default.json()["level"] == "org"

    listed = await api_env.client.get(f"{HEALTH}/assignments", headers=headers)
    assert {row["level"] for row in listed.json()} == {"device", "org"}

    removed = await api_env.client.delete(
        f"{HEALTH}/assignments/{assignment_id}", headers=headers
    )
    assert removed.status_code == 204
    async with AsyncSession(api_env.admin_engine) as session:
        remaining = list(
            (
                await session.execute(
                    select(HealthProfileAssignment.id).where(
                        HealthProfileAssignment.org_id == org.org_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(remaining) == 1


async def test_a_second_assignment_at_the_same_level_is_a_400(api_env) -> None:
    """The three PARTIAL unique indexes (§0.2) reached through HTTP. NULLs do
    not collide in a plain unique constraint, so without them two org-default
    rows would both be storable and "which applies" would be a tiebreak nobody
    chose."""
    org = await seed_org(api_env, "hdup")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)

    first = await api_env.client.post(
        f"{HEALTH}/assignments", headers=headers, json={"profile_id": str(profile_id)}
    )
    assert first.status_code == 201
    second = await api_env.client.post(
        f"{HEALTH}/assignments", headers=headers, json={"profile_id": str(profile_id)}
    )
    assert second.status_code == 400, second.text
    assert "already exists" in second.json()["detail"]


async def test_the_check_type_registry_is_served_closed_and_with_its_os_matrix(
    api_env,
) -> None:
    org = await seed_org(api_env, "hreg")
    headers = await _holder(api_env, org.org_id, "health.read")
    response = await api_env.client.get(f"{HEALTH}/check-types", headers=headers)
    assert response.status_code == 200
    by_name = {row["name"]: row for row in response.json()}
    # The two facts §13 needs and cannot compute: which OS families can ANSWER
    # a check, and which check the SERVER evaluates.
    assert by_name["posture.antivirus_healthy"]["os_families"] == ["windows"]
    assert by_name["agent.offline"]["evaluated_by"] == "server"
    assert by_name["service.state"]["enum_values"] == ["running", "stopped"]
    assert by_name["cpu.busy_pct"]["legal_comparators"] == [
        "eq",
        "gt",
        "gte",
        "lt",
        "lte",
        "neq",
    ]


# --------------------------------------------------------------------------- #
# Alerts: the three states that must not render alike.
# --------------------------------------------------------------------------- #


async def test_acknowledge_mute_and_close_are_three_different_stored_states(
    api_env,
) -> None:
    """An ACKNOWLEDGED alert, a MUTED alert and a CLEARED alert are different
    things and the API must not let them collapse.

    Asserted on the STORED ROW as well as on the response: an implementation
    that returned the right JSON while storing `cleared` for an acknowledge
    would pass a response-only test, and the sweeps read the row.
    """
    org = await seed_org(api_env, "halert")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)

    acked_id = await seed_alert(api_env, org, profile_id=profile_id, check_id=check_id)
    response = await api_env.client.post(
        f"{HEALTH}/alerts/{acked_id}/acknowledge", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "acknowledged"
    # STILL OPEN: no clear stamp of any kind.
    assert response.json()["cleared_at"] is None
    assert response.json()["cleared_reason"] is None
    stored = await _alert_row(api_env, acked_id)
    assert (stored.state, stored.cleared_at, stored.acknowledged_by is not None) == (
        "acknowledged",
        None,
        True,
    )

    muted_device = await seed_device(api_env, org.org_id)
    muted_id = await seed_alert(
        api_env, org, profile_id=profile_id, check_id=check_id, device_id=muted_device
    )
    until = datetime.now(UTC) + timedelta(hours=4)
    response = await api_env.client.post(
        f"{HEALTH}/alerts/{muted_id}/mute",
        headers=headers,
        json={"muted_until": until.isoformat()},
    )
    assert response.status_code == 200, response.text
    # A MUTED ALERT IS STILL OPEN. `muted_until` is present and no clear stamp
    # is -- the two facts a console needs to avoid reading it as resolved.
    assert response.json()["state"] == "muted"
    assert response.json()["muted_until"] is not None
    assert response.json()["cleared_at"] is None
    stored = await _alert_row(api_env, muted_id)
    assert stored.state == "muted" and stored.cleared_at is None

    closed_device = await seed_device(api_env, org.org_id)
    closed_id = await seed_alert(
        api_env, org, profile_id=profile_id, check_id=check_id, device_id=closed_device
    )
    response = await api_env.client.post(f"{HEALTH}/alerts/{closed_id}/close", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "cleared"
    # `manual` is what separates a human closing an alert from anything being
    # fixed -- §7.4's reporting rule rests on it.
    assert response.json()["cleared_reason"] == "manual"
    stored = await _alert_row(api_env, closed_id)
    assert (stored.state, stored.cleared_reason, stored.cleared_by is not None) == (
        "cleared",
        "manual",
        True,
    )

    assert {
        "health.alert_acknowledged",
        "health.alert_muted",
        "health.alert_closed",
    } <= set(await _audit_actions(api_env, org.org_id))


async def test_a_mute_beyond_thirty_days_is_refused_with_the_bound_named(api_env) -> None:
    org = await seed_org(api_env, "hmute")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    alert_id = await seed_alert(api_env, org, profile_id=profile_id, check_id=check_id)

    response = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/mute",
        headers=headers,
        json={"muted_until": (datetime.now(UTC) + timedelta(days=45)).isoformat()},
    )
    assert response.status_code == 400, response.text
    assert "30 days" in response.json()["detail"]
    # The refusal left the alert alone; nothing half-applied.
    assert (await _alert_row(api_env, alert_id)).state == "active"


async def test_the_alert_detail_carries_the_action_rows_and_why_one_was_blocked(
    api_env,
) -> None:
    """§13's alert detail. The `last_error` of a `blocked_rate_limit` row names
    the fuse, its window and the job ids counted against it -- and two approved
    actions sharing a `job_type` share one fuse, so nothing else on the row can
    answer "what consumed the budget". A state badge alone makes a blocked
    remediation unexplainable."""
    org = await seed_org(api_env, "hdetail")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    script_id = await seed_script(api_env, org.org_id, org.user_id)
    action_id = await seed_approved_action(
        api_env,
        org.org_id,
        org.user_id,
        job_type="script.run",
        script_id=script_id,
        script_version=1,
        autonomy="require_approval",
        risk_class="medium",
    )
    alert_id = await seed_alert(api_env, org, profile_id=profile_id, check_id=check_id)
    await seed_action_row(
        api_env,
        org.org_id,
        alert_id,
        "remediate",
        state="blocked_rate_limit",
        approved_action_id=action_id,
        last_error="max_per_org_per_hour: 20 script.run job(s) already ran; job ids ['abc']",
    )

    response = await api_env.client.get(f"{HEALTH}/alerts/{alert_id}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["actions"]) == 1
    action = body["actions"][0]
    assert action["state"] == "blocked_rate_limit"
    assert "max_per_org_per_hour" in action["last_error"]
    # §13: "nobody approves a script they cannot identify".
    assert action["script_name"] is not None
    assert action["script_version"] == 1
    assert action["autonomy"] == "require_approval"


# --------------------------------------------------------------------------- #
# The two permission pairs §14 names by hand.
# --------------------------------------------------------------------------- #


async def test_health_admin_cannot_remediate_but_can_edit_a_profile(api_env) -> None:
    """§11.1: configuring what alarms and executing code as SYSTEM/root on an
    endpoint are different acts with different blast radii, so `health.remediate`
    is deliberately NOT bundled into `health.admin`.

    Both halves in one test against ONE caller: the 403 alone would also pass
    for a caller who holds nothing.
    """
    org = await seed_org(api_env, "hadmin")
    headers = await _holder(api_env, org.org_id, "health.admin")
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    alert_id = await seed_alert(api_env, org, profile_id=profile_id, check_id=check_id)

    refused = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/remediate",
        headers=headers,
        json={"action_key": "whatever"},
    )
    assert refused.status_code == 403
    allowed = await api_env.client.patch(
        f"{HEALTH}/profiles/{profile_id}", headers=headers, json={"active": False}
    )
    assert allowed.status_code == 200, allowed.text
    # And no job reached anything.
    assert await _jobs_for(api_env, org.org_id) == []


async def test_health_remediate_cannot_write_the_approved_action_registry(api_env) -> None:
    """§11.1: `health.action.admin` is the highest grant in the module -- it
    decides what MAY run automatically as SYSTEM/root across the fleet -- and a
    holder of `health.remediate` must not have it.

    The positive half uses the SAME caller on the remediate route, so "this user
    can do nothing" cannot explain the 403.
    """
    org = await seed_org(api_env, "hrem")
    # `jobs.dispatch` for the same reason as in the approve test: §20's second
    # permission for `inventory.collect`, a precondition of the positive half
    # rather than this test's subject.
    headers = await _holder(
        api_env, org.org_id, "health.remediate", "health.read", "jobs.dispatch"
    )
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    alert_id = await seed_alert(api_env, org, profile_id=profile_id, check_id=check_id)
    action_id = await seed_approved_action(
        api_env, org.org_id, org.user_id, key="collect", autonomy="require_approval"
    )

    for method, path, body in (
        ("post", ACTIONS, {"key": "k", "name": "n", "job_type": "inventory.collect",
                           "risk_class": "low", "autonomy": "suggest"}),
        ("patch", f"{ACTIONS}/{action_id}", {"active": False}),
        ("delete", f"{ACTIONS}/{action_id}", None),
        ("post", f"{ACTIONS}/{action_id}/exempt-script", {"reason": "x"}),
        ("delete", f"{ACTIONS}/{action_id}/exempt-script", None),
    ):
        call = getattr(api_env.client, method)
        response = await (call(path, headers=headers, json=body) if body is not None
                          else call(path, headers=headers))
        assert response.status_code == 403, f"{method} {path}: {response.status_code}"

    # ... and the same caller CAN dispatch the remediation the registry describes.
    allowed = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/remediate",
        headers=headers,
        json={"action_key": "collect"},
    )
    assert allowed.status_code == 200, allowed.text
    jobs = await _jobs_for(api_env, org.org_id)
    assert [job.type for job in jobs] == ["inventory.collect"]
    # Invariant #1: it went through the signed engine.
    assert jobs[0].signature is not None
    assert "health.remediation_dispatched" in await _audit_actions(api_env, org.org_id)


async def test_device_health_is_readable_with_devices_read_alone(api_env) -> None:
    """§10's deliberate exception, proved by a caller who holds `devices.read`
    and NONE of the five health codes: 200 on the device view and 403 on the
    health console, IN THE SAME TEST. With an org-admin fixture a route wrongly
    gated on `health.read` would be indistinguishable."""
    org = await seed_org(api_env, "hdev")
    headers = await _holder(api_env, org.org_id, "devices.read")

    allowed = await api_env.client.get(
        DEVICE_HEALTH.format(device_id=org.device_id), headers=headers
    )
    assert allowed.status_code == 200, allowed.text
    refused = await api_env.client.get(f"{HEALTH}/alerts", headers=headers)
    assert refused.status_code == 403
    # And the check-now route is NOT reachable with `devices.read` either: it
    # dispatches a signed job, and §10's amendment gates it on
    # `health.alert.manage`.
    refused = await api_env.client.post(
        DEVICE_CHECK_NOW.format(device_id=org.device_id), headers=headers
    )
    assert refused.status_code == 403


# --------------------------------------------------------------------------- #
# The device view: the three things it refuses to conflate.
# --------------------------------------------------------------------------- #


async def test_a_device_with_no_profile_says_so_rather_than_looking_healthy(
    api_env,
) -> None:
    """§3's most dangerous possible default. `has_profile=false` and an EMPTY
    check list are different answers, and this asserts both fields so an
    implementation that returned `has_profile=true` with no checks (or omitted
    the flag) fails."""
    org = await seed_org(api_env, "hnoprof")
    headers = await _everything(api_env, org.org_id)
    response = await api_env.client.get(
        DEVICE_HEALTH.format(device_id=org.device_id), headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_profile"] is False
    assert body["profile"] is None
    assert body["assignment"] is None
    assert body["resolution_level"] is None
    assert body["checks"] == []


async def test_the_device_view_names_the_assignment_and_reports_unknown_not_ok(
    api_env,
) -> None:
    """§0.2 ("a resolution nobody can see is a resolution nobody trusts") and
    §0.7 (absent is not healthy), against a fixture where BOTH are observable:
    one check the device has answered (`ok`) beside one it never has
    (`unknown`). With only unanswered checks, an implementation that reported
    everything as `ok` and one that reported everything as `unknown` are
    indistinguishable."""
    from app.models import DeviceHealthCheckState

    org = await seed_org(api_env, "hdevview")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id, tick_interval_s=600)
    answered = await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct")
    silent = await seed_check(
        api_env, org.org_id, profile_id, check_type="memory.used_pct", comparator="gt"
    )
    assignment_id = await seed_assignment(
        api_env, org.org_id, profile_id, device_group_id=org.group_id
    )
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        session.add(
            DeviceHealthCheckState(
                org_id=org.org_id,
                device_id=org.device_id,
                check_id=answered,
                selector="",
                state="ok",
                last_value="12.5",
                policy_version=1,
            )
        )
        await session.commit()

    response = await api_env.client.get(
        DEVICE_HEALTH.format(device_id=org.device_id), headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_profile"] is True
    # WHICH assignment produced it, and at which level.
    assert body["assignment"]["id"] == str(assignment_id)
    assert body["resolution_level"] == "group"
    states = {row["check_id"]: row for row in body["checks"]}
    assert states[str(answered)]["state"] == "ok"
    assert states[str(answered)]["last_value"] == "12.5"
    assert states[str(silent)]["state"] == "unknown"
    assert states[str(silent)]["last_value"] == ""
    # §0.3 again, on the device view: 3 ticks x 600 s.
    assert states[str(silent)]["for_seconds"] == 1800
    # No ack row at all -> drift, and the sweep will re-push (§2.11).
    assert body["acknowledged_policy_version"] is None
    assert body["policy_drift"] is True


async def test_an_offline_device_still_shows_its_open_alerts(api_env) -> None:
    """§0.6/§12's negative, at the API. An alert does NOT clear because the
    device went quiet, so the device view must show `agent_online=false`
    ALONGSIDE the still-open alert rather than an empty, healthy-looking list.

    The fixture varies the thing that matters: the same assertion is made for an
    online device, so "always reports offline" and "never reports the alerts"
    both fail.
    """
    org = await seed_org(api_env, "hoffline")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    await seed_assignment(api_env, org.org_id, profile_id)

    quiet = org.device_id
    await _seed_agent(api_env, org.org_id, quiet, datetime.now(UTC) - timedelta(hours=6))
    quiet_alert = await seed_alert(api_env, org, profile_id=profile_id, check_id=check_id)

    talkative = await seed_device(api_env, org.org_id)
    await _seed_agent(api_env, org.org_id, talkative, datetime.now(UTC))
    live_alert = await seed_alert(
        api_env, org, profile_id=profile_id, check_id=check_id, device_id=talkative
    )

    offline_view = (
        await api_env.client.get(DEVICE_HEALTH.format(device_id=quiet), headers=headers)
    ).json()
    online_view = (
        await api_env.client.get(DEVICE_HEALTH.format(device_id=talkative), headers=headers)
    ).json()

    assert offline_view["agent_online"] is False
    assert online_view["agent_online"] is True
    # The quiet device's alert is STILL OPEN and still on its view.
    assert [a["id"] for a in offline_view["open_alerts"]] == [str(quiet_alert)]
    assert [a["id"] for a in online_view["open_alerts"]] == [str(live_alert)]
    assert (await _alert_row(api_env, quiet_alert)).state == "active"


# --------------------------------------------------------------------------- #
# Cross-org: 404, never 403, and the row really exists.
# --------------------------------------------------------------------------- #


async def test_another_orgs_rows_are_404_and_they_really_exist(api_env) -> None:
    """Four ids from a DIFFERENT org, each asserted present on the admin engine
    first. Without that assertion "404 because it is not yours" and "404 because
    there is nothing there" are the same test, and the second passes against an
    implementation with no tenancy predicate at all.

    404 rather than 403 is the point: a 403 confirms the id exists somewhere,
    which is a cross-tenant existence oracle.
    """
    mine = await seed_org(api_env, "hmine")
    theirs = await seed_org(api_env, "htheirs")
    headers = await _everything(api_env, mine.org_id)

    their_profile = await seed_profile(api_env, theirs.org_id)
    their_check = await seed_check(api_env, theirs.org_id, their_profile)
    their_alert = await seed_alert(
        api_env, theirs, profile_id=their_profile, check_id=their_check
    )
    their_assignment = await seed_assignment(api_env, theirs.org_id, their_profile)
    their_action = await seed_approved_action(api_env, theirs.org_id, theirs.user_id)

    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(
                select(HealthProfile.id).where(HealthProfile.id == their_profile)
            )
        ).scalar_one() == their_profile
        assert (
            await session.execute(select(HealthAlert.id).where(HealthAlert.id == their_alert))
        ).scalar_one() == their_alert
        assert (
            await session.execute(
                select(ApprovedAction.id).where(ApprovedAction.id == their_action)
            )
        ).scalar_one() == their_action
        assert (
            await session.execute(
                select(HealthProfileAssignment.id).where(
                    HealthProfileAssignment.id == their_assignment
                )
            )
        ).scalar_one() == their_assignment

    for path in (
        f"{HEALTH}/profiles/{their_profile}",
        f"{HEALTH}/profiles/{their_profile}/checks",
        f"{HEALTH}/alerts/{their_alert}",
        f"{ACTIONS}/{their_action}",
        DEVICE_HEALTH.format(device_id=theirs.device_id),
    ):
        response = await api_env.client.get(path, headers=headers)
        assert response.status_code == 404, f"{path} -> {response.status_code}"

    for path in (
        f"{HEALTH}/alerts/{their_alert}/acknowledge",
        f"{HEALTH}/alerts/{their_alert}/close",
    ):
        assert (await api_env.client.post(path, headers=headers)).status_code == 404

    assert (
        await api_env.client.delete(
            f"{HEALTH}/assignments/{their_assignment}", headers=headers
        )
    ).status_code == 404
    assert (
        await api_env.client.delete(f"{ACTIONS}/{their_action}", headers=headers)
    ).status_code == 404
    assert (
        await api_env.client.post(
            DEVICE_CHECK_NOW.format(device_id=theirs.device_id), headers=headers
        )
    ).status_code == 404

    # NOTHING was touched in the other org.
    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(select(HealthAlert.state).where(HealthAlert.id == their_alert))
        ).scalar_one() == "active"
        assert (
            await session.execute(
                select(ApprovedAction.id).where(ApprovedAction.id == their_action)
            )
        ).scalar_one_or_none() is not None


async def test_the_apis_own_lookups_carry_an_org_predicate_on_the_admin_engine(
    api_env, a_ticket_for
) -> None:
    """RLS MASKS AN APPLICATION PREDICATE, so the cross-org tests above cannot
    see one that has been deleted: driven through `org_session`, RLS rejects
    first and every assertion still passes.

    `api.py` adds three reads of its own that no service owns -- `_device`,
    `_devices_by_id` and `_ticket_number` -- and each carries an explicit
    `org_id` predicate in addition to RLS (invariant #5). This exercises them on
    the ADMIN (BYPASSRLS) engine, where RLS is not the thing doing the
    rejecting, so deleting a predicate is observable.

    GUARD OBSERVABILITY: delete `Device.org_id == org_id` from `_device` and
    this fails because the call RETURNS the other org's device instead of
    raising; delete it from `_devices_by_id` and the map comes back populated.
    """
    from app.modules.health import api as health_api

    mine = await seed_org(api_env, "hpreda")
    theirs = await seed_org(api_env, "hpredb")
    their_ticket = await a_ticket_for(theirs.org_id, "health-pred")
    mine_ticket = await a_ticket_for(mine.org_id, "health-pred-mine")
    async with AsyncSession(api_env.admin_engine) as session:
        try:
            await health_api._device(session, mine.org_id, theirs.device_id)
        except HealthNotFoundError:
            pass
        else:  # pragma: no cover - only reached by the mutation
            raise AssertionError(
                "_device returned another org's device on the admin engine; its "
                "org_id predicate is missing and RLS was the only thing hiding it"
            )
        # ... and it DOES find its own, so the assertion above is not satisfied
        # by a lookup that finds nothing at all.
        assert (await health_api._device(session, mine.org_id, mine.device_id)).id == (
            mine.device_id
        )

        assert await health_api._devices_by_id(
            session, mine.org_id, [theirs.device_id]
        ) == {}
        assert set(
            await health_api._devices_by_id(session, mine.org_id, [mine.device_id])
        ) == {mine.device_id}

        # `_ticket_number` is the alert detail's join into the ticket core. It
        # is asserted BOTH ways against real rows: another org's ticket resolves
        # to `None`, this org's resolves to its number. Without the second half
        # an implementation that always returned `None` would pass.
        assert await health_api._ticket_number(session, mine.org_id, their_ticket.id) is None
        assert (
            await health_api._ticket_number(session, mine.org_id, mine_ticket.id)
        ) is not None


async def test_the_lists_never_leak_another_orgs_rows(api_env) -> None:
    mine = await seed_org(api_env, "hlista")
    theirs = await seed_org(api_env, "hlistb")
    headers = await _everything(api_env, mine.org_id)

    their_profile = await seed_profile(api_env, theirs.org_id)
    their_check = await seed_check(api_env, theirs.org_id, their_profile)
    await seed_alert(api_env, theirs, profile_id=their_profile, check_id=their_check)
    await seed_assignment(api_env, theirs.org_id, their_profile)
    await seed_approved_action(api_env, theirs.org_id, theirs.user_id)

    my_profile = await seed_profile(api_env, mine.org_id)

    assert [row["id"] for row in (
        await api_env.client.get(f"{HEALTH}/profiles", headers=headers)
    ).json()] == [str(my_profile)]
    assert (await api_env.client.get(f"{HEALTH}/alerts", headers=headers)).json() == []
    assert (await api_env.client.get(f"{HEALTH}/assignments", headers=headers)).json() == []
    assert (await api_env.client.get(ACTIONS, headers=headers)).json() == []


# --------------------------------------------------------------------------- #
# GET /health/alerts/summary -- the count the badge could not take
# --------------------------------------------------------------------------- #


async def test_the_alert_summary_totals_every_open_alert_over_the_whole_org(
    api_env,
) -> None:
    """THE ROUTE THE SHELL BADGE DID NOT HAVE. Without it the console read one
    page of `GET /health/alerts` and filtered it in the browser -- a floor, and
    on a fleet with more than a page of open alerts a silent one.

    The severities are deliberately mixed and one alert is CLEARED, so an
    implementation that counted every row, or only the `active` ones, or only
    one severity, fails here rather than in a fleet.
    """
    org = await seed_org(api_env, "hsumapi")
    headers = await _holder(api_env, org.org_id, "health.read")
    profile_id = await seed_profile(api_env, org.org_id)

    for index, (state, severity) in enumerate(
        (
            ("active", "critical"),
            ("acknowledged", "warning"),
            ("muted", "info"),
            ("cleared", "critical"),
        )
    ):
        # A DISTINCT SELECTOR PER CHECK. `health_check` is unique on
        # `(profile_id, check_type, selector, os_family)`, so four checks
        # differing only in severity is an IntegrityError.
        check_id = await seed_check(
            api_env, org.org_id, profile_id, severity=severity, selector=f"sel-{index}"
        )
        extra: dict[str, Any] = {}
        if state == "cleared":
            extra = {"cleared_at": datetime.now(UTC), "cleared_reason": "recovery"}
        elif state == "muted":
            extra = {"muted_until": datetime.now(UTC) + timedelta(days=1)}
        await seed_alert(
            api_env,
            org,
            profile_id=profile_id,
            check_id=check_id,
            selector=f"sel-{index}",
            severity=severity,
            state=state,
            **extra,
        )

    response = await api_env.client.get(f"{HEALTH}/alerts/summary", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "open": 3,
        "by_state": {"active": 1, "acknowledged": 1, "muted": 1},
        "by_severity": {"info": 1, "warning": 1, "critical": 1},
        "worst_severity": "critical",
    }


async def test_the_alert_summary_is_a_literal_segment_and_not_an_alert_id(
    api_env,
) -> None:
    """ROUTE ORDERING, which this module's docstring calls the lesson every
    module since Phase 11 carries. `/health/alerts/{alert_id}` takes a UUID, so
    a `summary` registered after it is not merely shadowed -- it answers 422
    for a path that is supposed to be a totals endpoint, and the console reads
    that as a server fault rather than as a missing route.
    """
    org = await seed_org(api_env, "hsumord")
    headers = await _holder(api_env, org.org_id, "health.read")

    response = await api_env.client.get(f"{HEALTH}/alerts/summary", headers=headers)
    assert response.status_code == 200, (
        f"`summary` was matched as an {{alert_id}}: {response.status_code} {response.text}"
    )
    assert response.json()["open"] == 0


async def test_the_alert_summary_never_counts_another_orgs_alerts(api_env) -> None:
    """The HTTP half of invariant #5 for this route. It passes under RLS alone
    -- which is exactly why `tests/test_health_alerts.py` asserts the service's
    own `org_id` predicate on the BYPASSRLS admin engine, where deleting it is
    observable. Both are needed; neither is sufficient.

    BOTH SIDES ARE ASSERTED so that a summary answering zero for everybody
    cannot pass: the other org's own caller sees its two.
    """
    mine = await seed_org(api_env, "hsumx")
    theirs = await seed_org(api_env, "hsumy")
    my_headers = await _holder(api_env, mine.org_id, "health.read")
    their_headers = await _holder(api_env, theirs.org_id, "health.read")

    their_profile = await seed_profile(api_env, theirs.org_id)
    for index in range(2):
        their_check = await seed_check(
            api_env, theirs.org_id, their_profile, selector=f"sel-{index}"
        )
        await seed_alert(
            api_env,
            theirs,
            profile_id=their_profile,
            check_id=their_check,
            selector=f"sel-{index}",
        )

    mine_body = (
        await api_env.client.get(f"{HEALTH}/alerts/summary", headers=my_headers)
    ).json()
    theirs_body = (
        await api_env.client.get(f"{HEALTH}/alerts/summary", headers=their_headers)
    ).json()

    assert mine_body["open"] == 0
    assert mine_body["worst_severity"] is None
    assert theirs_body["open"] == 2


# --------------------------------------------------------------------------- #
# The approved-action registry.
# --------------------------------------------------------------------------- #


async def test_a_legacy_row_never_returns_its_stored_credential_to_health_read(
    api_env,
) -> None:
    """INVARIANT #3 ON THE READ SIDE. The write path refuses a `params_template`
    naming a credential, and that gate cannot reach a row written before it
    shipped -- so this row is seeded DIRECTLY, bypassing the service, which is
    exactly the shape a pre-gate row has.

    `health.read` is the caller, deliberately: it is the broad, non-admin grant
    (auditor and read-only hold it), so before this fix a user who could not
    even edit the registry could read the repository encryption key out of it,
    repeatably, over HTTP.

    ASSERTED ON THE SERVED DOCUMENT AND ON THE STORED ROW BOTH. The secret must
    be absent from the entire response body -- checked as a substring of the raw
    text, so it cannot survive in some field the schema grows later -- and it
    must STILL BE IN THE DATABASE, because a "redaction" that overwrote the
    stored value would destroy the row instead of protecting it, and the admin
    could no longer tell what had leaked.
    """
    from app.core.approved_actions import REDACTED_PLACEHOLDER

    org = await seed_org(api_env, "hleak")
    headers = await _holder(api_env, org.org_id, "health.read")
    action_id = await seed_approved_action(
        api_env,
        org.org_id,
        org.user_id,
        job_type="app.deploy",
        params_template={
            "package_id": "p1",
            "repo_key": "SUPERSECRET-REPO-KEY",
            "detection_rule": {"vault": {"admin_password": "hunter2"}},
        },
        autonomy="suggest",
    )

    detail = await api_env.client.get(f"{ACTIONS}/{action_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert "SUPERSECRET-REPO-KEY" not in detail.text, (
        f"the stored repository key was returned to a health.read holder: {detail.text}"
    )
    assert "hunter2" not in detail.text, (
        f"a nested stored credential was returned to a health.read holder: {detail.text}"
    )
    body = detail.json()
    assert body["params_template"]["repo_key"] == REDACTED_PLACEHOLDER
    assert body["params_template"]["package_id"] == "p1", "non-secret params must survive"
    assert body["params_template_redacted_keys"] == [
        "detection_rule.vault.admin_password",
        "repo_key",
    ]

    # The LIST route renders through the same function; asserted separately
    # because it is the route a console actually calls, and a fix applied to one
    # of the seven approved-action routes is not a fix.
    listing = await api_env.client.get(ACTIONS, headers=headers)
    assert listing.status_code == 200, listing.text
    assert "SUPERSECRET-REPO-KEY" not in listing.text
    assert "hunter2" not in listing.text

    # The row itself is untouched: redaction is a read-path transform, not a
    # destructive rewrite of the evidence.
    rows = await _action_rows(api_env, org.org_id)
    assert [row.params_template["repo_key"] for row in rows] == ["SUPERSECRET-REPO-KEY"]


async def test_a_clean_template_reports_nothing_redacted(api_env) -> None:
    """The contrast. Without it, "no secret in the response" would also be
    satisfied by a redaction that emptied every template, and the registry
    screen would show nothing at all."""
    org = await seed_org(api_env, "hnoleak")
    headers = await _holder(api_env, org.org_id, "health.read")
    action_id = await seed_approved_action(
        api_env,
        org.org_id,
        org.user_id,
        job_type="app.deploy",
        params_template={"package_id": "p1", "detection_rule": {"path": "C:\\app"}},
        autonomy="suggest",
    )
    body = (await api_env.client.get(f"{ACTIONS}/{action_id}", headers=headers)).json()
    assert body["params_template"] == {
        "package_id": "p1",
        "detection_rule": {"path": "C:\\app"},
    }
    assert body["params_template_redacted_keys"] == []


async def test_destructive_is_derived_and_a_body_that_supplies_it_is_refused(
    api_env,
) -> None:
    """§8.4. `destructive` is derived from the job type, the params and the
    exemption, and stored -- it is never accepted from a request body.

    THE FIXTURE VARIES THE THING THAT MATTERS: `patch.apply` with
    `reboot: "forced"` derives TRUE, and the body claims `false`. With a job
    type that derived FALSE, the refusal and the bug would store the same row
    and this test would prove nothing.

    Two refusals, both asserted: `extra="forbid"` makes the HTTP answer a 422
    naming the key, and NO row is created; the same body without `destructive`
    stores the DERIVED value. The plan predicted the field would be "ignored" --
    it is not, and it must not be: a caller who believes they set
    `destructive: false` and got a 201 believes they disarmed the autonomy gate.
    """
    org = await seed_org(api_env, "hderive")
    headers = await _holder(api_env, org.org_id, "health.action.admin", "health.read")
    body: dict[str, Any] = {
        "key": "forced-patch",
        "name": "Force a patch reboot",
        "job_type": "patch.apply",
        # `patches` is required by `PatchApplyParams`; the template is validated
        # against that model at write time, because a template the job type
        # refuses is a `ValueError` inside the org's sweep transaction later.
        "params_template": {"patches": [], "reboot": "forced"},
        "risk_class": "medium",
        "autonomy": "require_approval",
    }

    refused = await api_env.client.post(
        ACTIONS, headers=headers, json={**body, "destructive": False}
    )
    assert refused.status_code == 422, refused.text
    assert "destructive" in refused.text
    assert await _action_rows(api_env, org.org_id) == []

    created = await api_env.client.post(ACTIONS, headers=headers, json=body)
    assert created.status_code == 201, created.text
    assert created.json()["destructive"] is True
    rows = await _action_rows(api_env, org.org_id)
    assert [row.destructive for row in rows] == [True]


async def test_an_unregistered_job_type_is_a_400_naming_the_known_types(api_env) -> None:
    """§2.9: validated at WRITE time against the LIVE job-type registry, so an
    unregistered type is a 400 now rather than an `UnknownJobType` discovered at
    dispatch with an alert already open."""
    org = await seed_org(api_env, "hjt")
    headers = await _holder(api_env, org.org_id, "health.action.admin")
    response = await api_env.client.post(
        ACTIONS,
        headers=headers,
        json={
            "key": "bogus",
            "name": "Bogus",
            "job_type": "definitely.not.registered",
            "risk_class": "low",
            "autonomy": "suggest",
        },
    )
    assert response.status_code == 400, response.text
    assert "not a registered job type" in response.json()["detail"]
    assert await _action_rows(api_env, org.org_id) == []


async def test_an_autonomous_destructive_action_is_refused_at_the_api(api_env) -> None:
    """§8.3's hard gate reached over HTTP. The CHECK constraint is the fact and
    `_validate_and_derive` is the message; this asserts the message arrives as a
    400 rather than as a 500 from an `IntegrityError`."""
    org = await seed_org(api_env, "hgate")
    headers = await _holder(api_env, org.org_id, "health.action.admin")
    # `patch.apply` rather than `backup.restore`: both derive `destructive` by
    # `is_destructive` clause 1, but `backup.restore` is no longer nameable by an
    # approved action at all (its params carry the repository encryption key), so
    # it would be refused before the autonomy gate this test is about.
    response = await api_env.client.post(
        ACTIONS,
        headers=headers,
        json={
            "key": "auto-patch",
            "name": "Apply patches automatically",
            "job_type": "patch.apply",
            "params_template": {"patches": [], "reboot": "never"},
            "risk_class": "medium",
            "autonomy": "autonomous",
        },
    )
    assert response.status_code == 400, response.text
    assert "autonomous" in response.json()["detail"]
    assert await _action_rows(api_env, org.org_id) == []


async def test_the_script_exemption_is_granted_from_the_pin_and_revoked_again(
    api_env,
) -> None:
    """§2.10 / §10: the `(script_id, script_version)` pair comes from the
    ACTION'S OWN PIN and never from the body, so an exemption cannot be granted
    for a version the action does not name.

    The revoke half is asserted too, because an exemption removed without
    re-deriving `destructive` would leave the action stored as non-destructive
    and therefore still eligible for `autonomous`.
    """
    org = await seed_org(api_env, "hexempt")
    headers = await _holder(api_env, org.org_id, "health.action.admin", "health.read")
    script_id = await seed_script(api_env, org.org_id, org.user_id)
    created = await api_env.client.post(
        ACTIONS,
        headers=headers,
        json={
            "key": "restart-spooler",
            "name": "Restart the spooler",
            "job_type": "script.run",
            "script_id": str(script_id),
            "script_version": 1,
            "risk_class": "low",
            "autonomy": "require_approval",
        },
    )
    assert created.status_code == 201, created.text
    action_id = created.json()["id"]
    # `script.run` is DESTRUCTIVE BY DEFAULT (§8.4 clause 2): the server cannot
    # know what a PowerShell body does.
    assert created.json()["destructive"] is True
    assert created.json()["script_exemption"] is None

    granted = await api_env.client.post(
        f"{ACTIONS}/{action_id}/exempt-script",
        headers=headers,
        json={"reason": "reviewed by the platform team"},
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["destructive"] is False
    exemption = granted.json()["script_exemption"]
    # §13's first-class view: script name, pinned version, grantor, reason.
    assert exemption["script_version"] == 1
    assert exemption["reason"] == "reviewed by the platform team"
    assert exemption["script_name"] is not None
    async with AsyncSession(api_env.admin_engine) as session:
        stored = list(
            (
                await session.execute(
                    select(ApprovedActionScriptExemption).where(
                        ApprovedActionScriptExemption.org_id == org.org_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [(row.script_id, row.script_version) for row in stored] == [(script_id, 1)]

    revoked = await api_env.client.delete(
        f"{ACTIONS}/{action_id}/exempt-script", headers=headers
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["destructive"] is True
    assert revoked.json()["script_exemption"] is None

    assert {
        "health.action.destructive_exempted",
        "health.action.destructive_exemption_revoked",
    } <= set(await _audit_actions(api_env, org.org_id))


async def test_exempting_a_non_script_action_is_a_400(api_env) -> None:
    org = await seed_org(api_env, "hexempt2")
    headers = await _holder(api_env, org.org_id, "health.action.admin")
    action_id = await seed_approved_action(api_env, org.org_id, org.user_id)
    response = await api_env.client.post(
        f"{ACTIONS}/{action_id}/exempt-script", headers=headers, json={}
    )
    assert response.status_code == 400, response.text
    assert "script.run" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Approve / refuse a remediation.
# --------------------------------------------------------------------------- #


async def test_approving_a_remediation_dispatches_one_signed_job(api_env) -> None:
    """`jobs.dispatch` is held IN ADDITION to `health.remediate` because the
    seeded action's job type is `inventory.collect`, whose own kernel dispatch
    route is gated on it (§20). The gate itself is
    `tests/test_health_remediation_authz.py`'s subject; here it is a
    precondition, named so this test does not look like it is asserting that one
    permission suffices."""
    org = await seed_org(api_env, "happrove")
    headers = await _holder(
        api_env, org.org_id, "health.remediate", "health.read", "jobs.dispatch"
    )
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    alert_id = await seed_alert(api_env, org, profile_id=profile_id, check_id=check_id)
    approved_id = await seed_approved_action(
        api_env, org.org_id, org.user_id, autonomy="require_approval"
    )
    row_id = await seed_action_row(
        api_env,
        org.org_id,
        alert_id,
        "remediate",
        state="awaiting_approval",
        approved_action_id=approved_id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    response = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/actions/{row_id}/approve", headers=headers
    )
    assert response.status_code == 200, response.text
    jobs = await _jobs_for(api_env, org.org_id)
    assert len(jobs) == 1
    assert jobs[0].signature is not None
    assert str(jobs[0].id) == response.json()["job_id"]
    assert response.json()["device_id"] == str(org.device_id)
    actions = await _audit_actions(api_env, org.org_id)
    assert "health.remediation_approved" in actions
    assert "health.remediation_dispatched" in actions


async def test_refusing_a_remediation_skips_it_forever(api_env) -> None:
    from app.models import HealthAlertAction

    org = await seed_org(api_env, "hrefuse")
    headers = await _holder(api_env, org.org_id, "health.remediate")
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    alert_id = await seed_alert(api_env, org, profile_id=profile_id, check_id=check_id)
    approved_id = await seed_approved_action(
        api_env, org.org_id, org.user_id, autonomy="require_approval"
    )
    row_id = await seed_action_row(
        api_env,
        org.org_id,
        alert_id,
        "remediate",
        state="awaiting_approval",
        approved_action_id=approved_id,
    )

    response = await api_env.client.post(
        f"{HEALTH}/alerts/{alert_id}/actions/{row_id}/refuse",
        headers=headers,
        json={"reason": "we are rebuilding this box"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "skipped"
    assert "rebuilding" in response.json()["last_error"]
    async with AsyncSession(api_env.admin_engine) as session:
        stored = (
            await session.execute(
                select(HealthAlertAction).where(HealthAlertAction.id == row_id)
            )
        ).scalar_one()
    assert stored.state == "skipped"
    assert await _jobs_for(api_env, org.org_id) == []


# --------------------------------------------------------------------------- #
# The signed jobs this API puts on devices, and the config pushes.
# --------------------------------------------------------------------------- #


async def test_check_now_creates_one_signed_job_and_publishes_it(api_env) -> None:
    """§5.3. The job is created inside the tenant transaction and published to
    the device's dispatch channel only after it commits.

    The publish is observed on a REAL Redis subscription rather than by trusting
    the handler: a test that only asserted the `jobs` row would pass against a
    handler that never published, which is a job that never runs -- nothing
    re-drains pending `jobs` when an agent connects.
    """
    import redis.asyncio as aioredis

    from app.gateway.dispatch import dispatch_channel

    org = await seed_org(api_env, "hnow")
    headers = await _holder(api_env, org.org_id, "health.alert.manage")

    redis = aioredis.from_url(api_env.settings.redis_url, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.subscribe(dispatch_channel(org.device_id))
    try:
        response = await api_env.client.post(
            DEVICE_CHECK_NOW.format(device_id=org.device_id), headers=headers
        )
        assert response.status_code == 200, response.text
        jobs = await _jobs_for(api_env, org.org_id)
        assert [job.type for job in jobs] == ["health.check.now"]
        assert jobs[0].signature is not None

        published: dict[str, Any] | None = None
        for _ in range(50):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if message is not None:
                published = json.loads(message["data"])
                break
        assert published is not None, "no envelope reached the device's dispatch channel"
        assert published["job"]["device"] == str(org.device_id)
        assert published["job"]["id"] == str(jobs[0].id)
    finally:
        await pubsub.aclose()
        await redis.aclose()


async def test_health_read_alone_cannot_mint_a_single_check_now_job(api_env) -> None:
    """§10's amendment. `health.read` is what migration 0040 grants to
    `read-only` and `auditor`, and `check-now` mints a SIGNED JOB.

    THE FIXTURE VARIES THE ONLY THING THAT MAKES THIS FALSIFIABLE: the same
    caller, in the same test, gets 200 on a route that only reads
    (`GET /health/alerts`) and 403 on the one that dispatches. A test that only
    asserted the 403 would pass against a caller whose token was simply broken.

    Reproduced before the gate moved: a principal holding exactly `health.read`
    issued 30 consecutive requests and got 30 signed jobs, all 200. The
    falsifier here is therefore the JOB COUNT, not the status code -- a signed
    job that reached a device is the damage, and the status code is only how it
    was asked for.

    GUARD OBSERVABILITY: put `health.read` back on `check_now_route` and this
    reports jobs minted by a read-only principal.
    """
    org = await seed_org(api_env, "hnowro")
    headers = await _holder(api_env, org.org_id, "health.read")

    readable = await api_env.client.get(f"{HEALTH}/alerts", headers=headers)
    assert readable.status_code == 200, readable.text

    codes = set()
    for _ in range(5):
        response = await api_env.client.post(
            DEVICE_CHECK_NOW.format(device_id=org.device_id), headers=headers
        )
        codes.add(response.status_code)
    jobs = await _jobs_for(api_env, org.org_id)
    assert [job.type for job in jobs] == [], (
        f"a principal holding exactly health.read minted {len(jobs)} signed job(s) "
        f"({[j.type for j in jobs]}); status codes were {codes}"
    )
    assert codes == {403}


async def test_check_now_is_fused_per_device_and_the_second_click_mints_nothing(
    api_env,
) -> None:
    """§10's amendment, second half. The gate bounds WHO; the fuse bounds HOW
    MANY, and a technician can loop just as easily as a read-only account.

    The observable is again the job count: the second request must not put a
    second `health.check.now` on the wire, because the report the device is
    already producing answers it. `429` rather than `400` -- the request is well
    formed and the state refuses it.

    THE PARAMETER THIS DELIBERATELY VARIES is the DEVICE: a second device in the
    same org is dispatched successfully in the same test, so a fuse that
    accidentally became org-wide-per-minute (or that simply refused everything
    after the first call) fails here rather than looking correct.

    GUARD OBSERVABILITY: delete the per-device branch of `_check_now_fuse` and
    this reports 2 jobs on one device.
    """
    org = await seed_org(api_env, "hnowfuse")
    other_device = await seed_device(api_env, org.org_id)
    headers = await _holder(api_env, org.org_id, "health.alert.manage")

    first = await api_env.client.post(
        DEVICE_CHECK_NOW.format(device_id=org.device_id), headers=headers
    )
    assert first.status_code == 200, first.text
    second = await api_env.client.post(
        DEVICE_CHECK_NOW.format(device_id=org.device_id), headers=headers
    )
    elsewhere = await api_env.client.post(
        DEVICE_CHECK_NOW.format(device_id=other_device), headers=headers
    )

    jobs = await _jobs_for(api_env, org.org_id)
    targets = [str(job.target_id) for job in jobs]
    assert targets.count(str(org.device_id)) == 1, (
        f"two check-now clicks on one device minted {targets.count(str(org.device_id))} "
        f"signed jobs; the second answered {second.status_code}"
    )
    assert second.status_code == 429, second.text
    assert "60s" in second.json()["detail"]
    # ... and the fuse is per DEVICE, not a blanket per-org minute.
    assert elsewhere.status_code == 200, elsewhere.text
    assert targets.count(str(other_device)) == 1


async def test_a_config_write_queues_a_policy_push_for_the_affected_device(
    api_env,
) -> None:
    """Every write that moves a device's effective policy re-pushes it (§12).

    THE FIXTURE VARIES THE ONLY THING THAT MAKES THIS FALSIFIABLE: a device that
    is ASSIGNED beside one that is not. A handler that pushed to everything and
    one that pushed to nothing are indistinguishable when every device resolves
    the same way.
    """
    org = await seed_org(api_env, "hpush")
    await seed_agent(api_env, org.org_id, org.device_id)
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_check(api_env, org.org_id, profile_id)

    # No assignment yet: creating the profile pushed nothing.
    assert await _jobs_for(api_env, org.org_id) == []

    created = await api_env.client.post(
        f"{HEALTH}/assignments",
        headers=headers,
        json={"profile_id": str(profile_id), "device_id": str(org.device_id)},
    )
    assert created.status_code == 201, created.text
    jobs = await _jobs_for(api_env, org.org_id)
    assert [job.type for job in jobs] == ["health.policy.apply"]
    assert jobs[0].target_id == org.device_id
    assert jobs[0].signature is not None

    # A second, unassigned device gets nothing even though the org now has a
    # profile: resolution, not "every device in the org", decides.
    other = await seed_device(api_env, org.org_id)
    await seed_agent(api_env, org.org_id, other)
    assert {job.target_id for job in await _jobs_for(api_env, org.org_id)} == {org.device_id}
    assert other not in {job.target_id for job in await _jobs_for(api_env, org.org_id)}

    # Editing the check set bumps the version and pushes again.
    put = await api_env.client.put(
        f"{HEALTH}/profiles/{profile_id}/checks",
        headers=headers,
        json={"checks": [_cpu_check(threshold_num=80)]},
    )
    assert put.status_code == 200, put.text
    assert len(await _jobs_for(api_env, org.org_id)) == 2

    # And deleting the assignment sends an EXPLICIT DISARM rather than silence
    # (§5.2) -- but ONLY for a device that has ACKNOWLEDGED a policy.
    #
    # THIS IS THE CODE CONTRADICTING THE PLAN, and the code wins.
    # `sweep.run_policy_reconcile` skips a device where `resolved is None and
    # acknowledged is None` ("never armed and nothing resolves"), so a device
    # that was sent a policy but has not yet acknowledged it receives NO disarm
    # when its assignment is deleted -- it converges only once it acknowledges
    # and a later reconcile runs. The ack row below is what an agent's
    # `health.policy.apply` result handler writes; without it this assertion
    # would be asserting the skip, not the disarm.
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        session.add(
            DeviceHealthPolicyAck(
                device_id=org.device_id,
                org_id=org.org_id,
                profile_id=profile_id,
                policy_version=(await _profile_row(api_env, profile_id)).policy_version,
            )
        )
        await session.commit()

    assignment_id = created.json()["id"]
    removed = await api_env.client.delete(
        f"{HEALTH}/assignments/{assignment_id}", headers=headers
    )
    assert removed.status_code == 204
    jobs = await _jobs_for(api_env, org.org_id)
    assert len(jobs) == 3
    assert jobs[-1].params["checks"] == []
    assert jobs[-1].target_id == org.device_id


# --------------------------------------------------------------------------- #
# Settings.
# --------------------------------------------------------------------------- #


async def test_settings_report_effective_values_and_the_get_writes_nothing(
    api_env,
) -> None:
    """§2.8: `GET` returns the EFFECTIVE settings for an org with no row and
    creates nothing, so a `health.read` request never becomes a write.

    Asserted by counting rows on the admin engine before and after, not by the
    absence of an exception.
    """
    org = await seed_org(api_env, "hset")
    headers = await _everything(api_env, org.org_id)

    response = await api_env.client.get(f"{HEALTH}/settings", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["configured"] is False
    assert response.json()["flap_threshold"] == 4
    # Neither queue nor category configured -> alert tickets cannot be opened at
    # all (`declared_actions.declared_action_kinds`), which the screen must say.
    assert response.json()["alert_tickets_enabled"] is False

    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(
                select(HealthOrgSettings.org_id).where(HealthOrgSettings.org_id == org.org_id)
            )
        ).scalar_one_or_none() is None

    refs = await seed_ticketing(api_env, org.org_id, org.user_id)
    put = await api_env.client.put(
        f"{HEALTH}/settings",
        headers=headers,
        json={
            "flap_threshold": 8,
            "default_alert_queue_id": str(refs.queue_id),
            "default_ticket_category_id": str(refs.category_id),
        },
    )
    assert put.status_code == 200, put.text
    assert put.json()["configured"] is True
    assert put.json()["flap_threshold"] == 8
    assert put.json()["alert_tickets_enabled"] is True
    # An unsupplied field keeps its DEFAULT rather than being nulled.
    assert put.json()["offline_after_s"] == 900
    assert "health.settings_updated" in await _audit_actions(api_env, org.org_id)


async def test_an_out_of_range_setting_is_refused_and_names_the_bound(api_env) -> None:
    org = await seed_org(api_env, "hsetbad")
    headers = await _everything(api_env, org.org_id)
    response = await api_env.client.put(
        f"{HEALTH}/settings", headers=headers, json={"flap_threshold": 999}
    )
    # Pydantic refuses it first (422); the service would refuse it again with a
    # 400 naming the bound, and the CHECK constraint owns the fact. All three
    # exist deliberately -- see `schema.SettingsUpdate`.
    assert response.status_code == 422, response.text
    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(
                select(HealthOrgSettings.org_id).where(HealthOrgSettings.org_id == org.org_id)
            )
        ).scalar_one_or_none() is None


def test_the_settings_schema_bounds_match_the_service_bounds() -> None:
    """Three copies of each bound exist (Pydantic, `SETTING_BOUNDS`, the CHECK
    constraint) and each catches something the others cannot. This pins the
    first two against each other so they cannot drift into refusing different
    values -- at which point the API's 422 and the service's 400 would disagree
    about what is legal."""
    from app.modules.health.schema import SettingsUpdate

    for field, (low, high) in SETTING_BOUNDS.items():
        meta = SettingsUpdate.model_fields[field].metadata
        bounds = {type(m).__name__: getattr(m, "ge", getattr(m, "le", None)) for m in meta}
        assert bounds.get("Ge") == low, field
        assert bounds.get("Le") == high, field


# --------------------------------------------------------------------------- #
# `extra="forbid"` -- the wire half of a refusal the services already make.
# --------------------------------------------------------------------------- #


async def test_a_typo_in_a_request_body_is_refused_rather_than_dropped(api_env) -> None:
    """Pydantic's DEFAULT is `ignore`, which would strip a typo'd key before the
    service could refuse it -- returning 200/201 for a body that changed nothing
    and auditing a no-op edit.

    Every request model in this module therefore sets `extra="forbid"`. Asserted
    across four models, and each assertion checks the STORED ROW as well as the
    status: a 422 that had already written something would be worse than the
    silent drop.
    """
    org = await seed_org(api_env, "hforbid")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id, name="untouched")

    typo = await api_env.client.post(
        f"{HEALTH}/profiles", headers=headers, json={"name": "x", "tick_intervals": 300}
    )
    assert typo.status_code == 422, typo.text
    assert "tick_intervals" in typo.text

    typo = await api_env.client.patch(
        f"{HEALTH}/profiles/{profile_id}", headers=headers, json={"nmae": "renamed"}
    )
    assert typo.status_code == 422, typo.text
    assert (await _profile_row(api_env, profile_id)).name == "untouched"

    typo = await api_env.client.put(
        f"{HEALTH}/profiles/{profile_id}/checks",
        headers=headers,
        json={"checks": [_cpu_check(sevrity="critical")]},
    )
    assert typo.status_code == 422, typo.text
    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(
                select(HealthCheck.id).where(HealthCheck.profile_id == profile_id)
            )
        ).scalars().all() == []

    typo = await api_env.client.put(
        f"{HEALTH}/settings", headers=headers, json={"flap_thresholds": 8}
    )
    assert typo.status_code == 422, typo.text
    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(
                select(HealthOrgSettings.org_id).where(HealthOrgSettings.org_id == org.org_id)
            )
        ).scalar_one_or_none() is None


async def test_supplying_both_for_ticks_and_for_seconds_is_refused(api_env) -> None:
    """`CheckIn.supplied()` is `exclude_unset` precisely so the service can tell
    "supplied" from "defaulted" for these two keys (§0.3). A model that
    defaulted `for_ticks=3` would make every body look like it supplied both,
    and this refusal would fire for every caller."""
    org = await seed_org(api_env, "hboth")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)

    both = await api_env.client.put(
        f"{HEALTH}/profiles/{profile_id}/checks",
        headers=headers,
        json={"checks": [_cpu_check(for_ticks=3, for_seconds=900)]},
    )
    assert both.status_code == 400, both.text
    assert "two answers to one question" in both.json()["detail"]

    # ... and neither one alone trips it, which is what proves the refusal is
    # about the PAIR rather than about either key.
    only_seconds = _cpu_check(for_seconds=900)
    only_seconds.pop("for_ticks")
    assert (
        await api_env.client.put(
            f"{HEALTH}/profiles/{profile_id}/checks",
            headers=headers,
            json={"checks": [only_seconds]},
        )
    ).status_code == 200
    assert (
        await api_env.client.put(
            f"{HEALTH}/profiles/{profile_id}/checks",
            headers=headers,
            json={"checks": [_cpu_check()]},
        )
    ).status_code == 200


async def test_a_check_type_outside_the_closed_registry_is_a_400(api_env) -> None:
    org = await seed_org(api_env, "hclosed")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)
    response = await api_env.client.put(
        f"{HEALTH}/profiles/{profile_id}/checks",
        headers=headers,
        json={"checks": [_cpu_check(check_type="cpu.temperature")]},
    )
    assert response.status_code == 400, response.text
    assert "closed registry" in response.json()["detail"]


async def test_a_check_pinned_to_an_os_that_cannot_answer_it_is_a_400(api_env) -> None:
    """§4.1: a check pinned to an OS that cannot answer it would be stored,
    pushed to nothing, and read as a rule that never evaluates. SQL cannot
    refuse it -- `ck_health_check_os_family` accepts every family -- so the
    service is the whole guard, and this is its wire half."""
    org = await seed_org(api_env, "hos")
    headers = await _everything(api_env, org.org_id)
    profile_id = await seed_profile(api_env, org.org_id)
    response = await api_env.client.put(
        f"{HEALTH}/profiles/{profile_id}/checks",
        headers=headers,
        json={
            "checks": [
                {
                    "check_type": "posture.antivirus_healthy",
                    "comparator": "is",
                    "threshold_bool": True,
                    "os_family": "linux",
                }
            ]
        },
    )
    assert response.status_code == 400, response.text
    assert "cannot answer" in response.json()["detail"]
