"""The IPAM API surface and its RBAC completeness
(`docs/modules/ipam/spec.md` §9, §11.2, §0.13, §0.4, §0.5, §0.10).

**RBAC COMPLETENESS IS DERIVED FROM THE ROUTER**, not from a list somebody has
to remember to update: every route is walked, `route_permission` is called on
each, and a route registered without `require_permission` returns `None` there
and fails by existing -- whatever module added it. The route table is pinned by
EQUALITY and by an exact COUNT: a `>=` assertion hides a missing route, and a
subset assertion hides a route that was never mounted.

**THE PERMISSION SPLIT IS ASSERTED AS BEHAVIOUR, NOT AS A TABLE ROW.** A
technician dispatches a sweep inside an authorized scope and is refused a scan
authorization, in the same test, against the same fixture. *If `ipam.sweep.run`
and `ipam.scope.manage` were collapsed*, the second call observes 201 -- and
anyone who can run a scan can decide what may be scanned, which is the whole
control (§11.2).

**§0.13's "reachable with `ipam.read` alone" CANNOT BE ASSERTED BELOW THE
ROUTER.** `address_history` is a plain service call; only a request carrying a
token whose roles exclude `audit.read` can show that the narrow projection is
reachable and the general audit API is not. That assertion lives here, and it
checks BOTH halves against the same principal.

WHAT THESE FIXTURES DELIBERATELY VARY:

* **The cross-org matrix uses a REAL id from a REAL second org.** A random UUID
  would 404 against any implementation, including one with no tenancy at all.
* **The technician and the read-only principal are issued against the SAME org
  and the SAME rows**, so a 403 is a statement about the role rather than about
  what the fixture happened to contain.
* **The route-count assertion is a number, not a `>=`.**
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import PERMISSIONS, route_permission
from app.models import IpamScanAuthorization
from app.modules.ipam import api
from app.modules.ipam.addresses import AddressHistoryEntry
from app.modules.ipam.errors import IpamNotFoundError
from app.modules.ipam.handler import register_job_types
from app.modules.ipam.subnets import delete_subnet, get_subnet, list_subnets
from tests.ipam_fixtures import (
    seed_agent,
    seed_device,
    seed_range,
    seed_subnet,
    seed_vrf,
    upsert_observation_row,
)

IPAM = "/api/v1/ipam"
NOW = datetime.now(UTC)


# --------------------------------------------------------------------------- #
# §9's route table -- the pinned contract
# --------------------------------------------------------------------------- #

EXPECTED_ROUTES: dict[tuple[str, str], str] = {
    ("GET", f"{IPAM}/vrfs"): "ipam.read",
    ("POST", f"{IPAM}/vrfs"): "ipam.manage",
    ("GET", f"{IPAM}/sites"): "ipam.read",
    ("POST", f"{IPAM}/sites"): "ipam.manage",
    ("GET", f"{IPAM}/vlans"): "ipam.read",
    ("POST", f"{IPAM}/vlans"): "ipam.manage",
    ("GET", f"{IPAM}/subnets"): "ipam.read",
    ("POST", f"{IPAM}/subnets"): "ipam.manage",
    ("GET", f"{IPAM}/subnets/{{subnet_id}}"): "ipam.read",
    ("PUT", f"{IPAM}/subnets/{{subnet_id}}"): "ipam.manage",
    ("DELETE", f"{IPAM}/subnets/{{subnet_id}}"): "ipam.manage",
    ("GET", f"{IPAM}/subnets/{{subnet_id}}/addresses"): "ipam.read",
    ("GET", f"{IPAM}/subnets/{{subnet_id}}/utilization"): "ipam.read",
    ("GET", f"{IPAM}/subnets/{{subnet_id}}/next-free"): "ipam.read",
    ("POST", f"{IPAM}/subnets/{{subnet_id}}/allocate"): "ipam.assign",
    ("GET", f"{IPAM}/subnets/{{subnet_id}}/ranges"): "ipam.read",
    ("POST", f"{IPAM}/subnets/{{subnet_id}}/ranges"): "ipam.manage",
    ("DELETE", f"{IPAM}/subnets/{{subnet_id}}/ranges/{{range_id}}"): "ipam.manage",
    ("GET", f"{IPAM}/addresses/{{address_id}}"): "ipam.read",
    ("POST", f"{IPAM}/addresses"): "ipam.assign",
    ("PUT", f"{IPAM}/addresses/{{address_id}}"): "ipam.assign",
    ("POST", f"{IPAM}/addresses/{{address_id}}/release"): "ipam.assign",
    ("GET", f"{IPAM}/addresses/{{address_id}}/history"): "ipam.read",
    ("GET", f"{IPAM}/search"): "ipam.read",
    ("GET", f"{IPAM}/drift"): "ipam.read",
    ("POST", f"{IPAM}/drift/{{drift_id}}/acknowledge"): "ipam.assign",
    ("POST", f"{IPAM}/drift/{{drift_id}}/suppress"): "ipam.assign",
    ("GET", f"{IPAM}/orphans"): "ipam.read",
    ("GET", f"{IPAM}/scan-authorizations"): "ipam.read",
    ("POST", f"{IPAM}/scan-authorizations"): "ipam.scope.manage",
    ("DELETE", f"{IPAM}/scan-authorizations/{{authorization_id}}"): "ipam.scope.manage",
    ("POST", f"{IPAM}/subnets/{{subnet_id}}/sweep"): "ipam.sweep.run",
    ("PUT", f"{IPAM}/subnets/{{subnet_id}}/sweep-settings"): "ipam.sweep.manage",
    ("GET", f"{IPAM}/subnets/{{subnet_id}}/sweep-runs"): "ipam.read",
    ("POST", f"{IPAM}/import/preview"): "ipam.manage",
    ("POST", f"{IPAM}/import/apply"): "ipam.manage",
    ("POST", f"{IPAM}/import/doc-records"): "ipam.manage",
    ("GET", f"{IPAM}/export/subnets.csv"): "ipam.read",
    ("GET", f"{IPAM}/export/addresses.csv"): "ipam.read",
    ("GET", f"{IPAM}/export/drift.csv"): "ipam.read",
}

#: PINNED. A `>=` assertion hides a missing route; this number changes only when
#: §9's table does, which makes adding a route a deliberate edit here too.
EXPECTED_ROUTE_COUNT = 40


# --------------------------------------------------------------------------- #
# Router flattening -- FastAPI 0.139's `include_router` appends a wrapper
# --------------------------------------------------------------------------- #


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """`(full path, route)` for every real route reachable from `router`.

    `include_router` in this FastAPI appends a `_IncludedRouter` wrapper rather
    than flattening; the wrapper exposes the router it wrapped as
    `.original_router` and the prefix it was mounted under as
    `.include_context.prefix`. Copied from `tests/test_email_intake_api.py`, and
    the same in every module that derives completeness from the router.
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


def _ipam_routes(app: Any) -> list[tuple[str, Any]]:
    return [(path, route) for path, route in _flatten(app.router) if path.startswith(IPAM)]


def _method_paths(app: Any) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, route in _ipam_routes(app)
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #


class _Env:
    def __init__(self, api_env, org_id: UUID) -> None:
        self.api_env = api_env
        self.org_id = org_id
        self.vrf_id: UUID
        self.subnet_id: UUID
        self.admin: dict[str, str]
        self.technician: dict[str, str]
        self.readonly: dict[str, str]
        self.device_id: UUID


async def _seed(api_env, *, prefix: str = "10.70.0.0/24", authorize: bool = True) -> _Env:
    register_job_types()
    org_id = await api_env.create_org("ipam-api")
    env = _Env(api_env, org_id)
    env.vrf_id = await seed_vrf(api_env, org_id)
    env.subnet_id = await seed_subnet(
        api_env, org_id, vrf_id=env.vrf_id, prefix=prefix
    )
    env.admin = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    env.technician = await api_env.headers(
        org_id, await api_env.create_user(org_id, "technician")
    )
    env.readonly = await api_env.headers(
        org_id, await api_env.create_user(org_id, "read-only")
    )

    # A device that is genuinely eligible to sweep: an online agent and a
    # current inventory observation INSIDE the subnet. Without both, a
    # dispatch test observes `no_agent` and proves nothing about the gate it
    # meant to exercise.
    env.device_id = await seed_device(api_env, org_id)
    await seed_agent(api_env, org_id, env.device_id, status="online", last_seen_at=NOW)
    host = prefix.rsplit(".", 1)[0] + ".9"
    await upsert_observation_row(
        api_env, org_id, vrf_id=env.vrf_id, address=host, source="inventory",
        method="nic", confidence="high", device_id=env.device_id, subnet_id=env.subnet_id,
        now=NOW,
    )
    if authorize:
        # Seeded directly, and `authorized_by` is a REAL user: §2.9 makes that
        # column NOT NULL because there is no system-authorized scope, and a
        # fixture that dodged it would be exercising a schema this module does
        # not have.
        authorizer = await _a_user(api_env, org_id)
        async with AsyncSession(api_env.admin_engine) as session:
            session.add(
                IpamScanAuthorization(
                    org_id=org_id,
                    vrf_id=env.vrf_id,
                    prefix=prefix,
                    authorized_by=authorizer,
                    authorized_at=NOW - timedelta(days=1),
                )
            )
            await session.commit()
    return env


async def _a_user(api_env, org_id: UUID) -> UUID:
    from app.models import User

    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(User.id).where(User.org_id == org_id).limit(1))
        ).scalar_one()


# --------------------------------------------------------------------------- #
# Completeness
# --------------------------------------------------------------------------- #


async def test_the_routes_mounted_are_exactly_the_spec_table(api_env) -> None:
    """Pins the FLATTENER as well as the route table.

    If `_flatten` ever stops finding real routes -- a FastAPI that changes
    `_IncludedRouter`'s shape, or a `register()` that is never called -- this
    fails loudly instead of letting every RBAC assertion below pass over an
    empty list. Equality and a count, never `>=`.
    """
    found = _method_paths(api_env.app)
    assert found == set(EXPECTED_ROUTES)
    assert len(found) == EXPECTED_ROUTE_COUNT


async def test_every_route_carries_a_permission_and_it_is_the_right_one(api_env) -> None:
    """COMPLETENESS DERIVED FROM THE ROUTER (invariant #6).

    Two assertions, and both are needed. The first is completeness: a route
    registered without `require_permission` returns `None` here and fails by
    existing. The second pins WHICH code -- a `/scan-authorizations` gated on
    `ipam.read` is fully gated and completely wrong, and the first assertion
    cannot see it.

    GUARD OBSERVABILITY: delete `Depends(require_permission(...))` from any one
    route in `api.py` and this fails naming that exact (method, path).
    """
    found = {
        (method, path): route_permission(route)
        for path, route in _ipam_routes(api_env.app)
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
    }
    ungated = sorted(key for key, code in found.items() if code is None)
    assert ungated == [], f"routes with no permission dependency: {ungated}"
    assert found == EXPECTED_ROUTES


def test_every_permission_this_module_gates_on_is_a_registered_code() -> None:
    """A typo'd code is not a weaker gate, it is a 500 -- `has_permission` raises
    `ValueError` for an unknown code. Pinned here so the failure is a named test
    rather than a request-time crash on a route nobody exercised."""
    unknown = sorted({code for code in EXPECTED_ROUTES.values() if code not in PERMISSIONS})
    assert unknown == []


def test_the_high_privilege_routes_are_the_ones_the_spec_names() -> None:
    """§11.2's control, read off the table: exactly the two `/scan-authorizations`
    mutations carry `ipam.scope.manage`, and nothing else does.

    Stated as a set equality so a future route that quietly reuses the code --
    "it is an admin thing anyway" -- fails here rather than widening the grant
    that decides where this platform emits network traffic.
    """
    scoped = {key for key, code in EXPECTED_ROUTES.items() if code == "ipam.scope.manage"}
    assert scoped == {
        ("POST", f"{IPAM}/scan-authorizations"),
        ("DELETE", f"{IPAM}/scan-authorizations/{{authorization_id}}"),
    }


# --------------------------------------------------------------------------- #
# THE SPLIT, as behaviour (§11.2)
# --------------------------------------------------------------------------- #


async def test_a_technician_can_sweep_an_authorized_scope_but_not_authorize_one(
    api_env,
) -> None:
    """**THE WHOLE CONTROL, IN ONE TEST, AGAINST ONE FIXTURE.**

    Running a sweep inside an already-authorized scope is routine technician
    work; deciding what may be scoped at all is not. *If `ipam.sweep.run` and
    `ipam.scope.manage` were collapsed into one code*, the second call observes
    201 -- and anyone who can run a scan can decide what may be scanned.

    The dispatch is asserted to have really happened (a `dispatched` run with a
    device and a job), not merely to have returned 2xx: a route that silently
    recorded `no_agent` would also be "not a 403".
    """
    env = await _seed(api_env)

    dispatched = await api_env.client.post(
        f"{IPAM}/subnets/{env.subnet_id}/sweep", headers=env.technician
    )
    assert dispatched.status_code == 202, dispatched.text
    body = dispatched.json()
    assert body["state"] == "dispatched"
    assert body["device_id"] == str(env.device_id)
    assert body["job_id"] is not None
    assert body["target_count"] == 256

    refused = await api_env.client.post(
        f"{IPAM}/scan-authorizations",
        headers=env.technician,
        json={"prefix": "10.99.0.0/24", "note": "self-service"},
    )
    assert refused.status_code == 403, refused.text

    allowed = await api_env.client.post(
        f"{IPAM}/scan-authorizations",
        headers=env.admin,
        json={"prefix": "10.99.0.0/24", "note": "approved by change board"},
    )
    assert allowed.status_code == 201, allowed.text


async def test_dispatching_into_an_unauthorized_scope_is_403_and_creates_no_job(
    api_env,
) -> None:
    """§6.3's refusal, surfaced. 403 rather than 404: the subnet exists and the
    caller may see it -- what is missing is a human's authorization to put
    packets on that wire.

    The `jobs` table is asserted empty, because "the response was 403" would
    also hold for an implementation that signed a job and then refused to
    report it.
    """
    from app.models import Job

    env = await _seed(api_env, authorize=False)
    refused = await api_env.client.post(
        f"{IPAM}/subnets/{env.subnet_id}/sweep", headers=env.technician
    )
    assert refused.status_code == 403, refused.text

    async with AsyncSession(api_env.admin_engine) as session:
        jobs = (
            (await session.execute(select(Job).where(Job.org_id == env.org_id)))
            .scalars()
            .all()
        )
    assert jobs == []


async def test_the_refused_run_reaches_the_database_through_the_route(api_env) -> None:
    """**THE RECORD OF A REFUSAL IS THE ENTIRE POINT OF REFUSING HERE**, and it
    has to survive the 403 on the path an operator actually takes.

    §6.3 refuses on the NEAR side of the signing boundary and records
    `state='refused'` so the subnet page can show it. `create_sweep` used to add
    that row and then raise INSIDE the route's `org_session` block --
    `session.begin()` rolls back on any exception leaving it, so through HTTP
    the row was written and immediately discarded. A refusal nobody can audit is
    indistinguishable from a request nobody made.

    THIS TEST GOES THROUGH THE ROUTE, and that is the whole point of it. The
    service-level test in `test_ipam_sweep_dispatch.py` committed the session
    itself, which the route does not, so it passed against the broken path for
    as long as the path was broken.

    *If the refusal were raised inside the block again*, this observes zero
    `ipam_sweep_run` rows behind an unchanged 403.
    """
    from app.modules.ipam.models import IpamSweepRun

    env = await _seed(api_env, authorize=False)
    refused = await api_env.client.post(
        f"{IPAM}/subnets/{env.subnet_id}/sweep", headers=env.technician
    )
    assert refused.status_code == 403, refused.text

    async with AsyncSession(api_env.admin_engine) as session:
        runs = list(
            (
                await session.execute(
                    select(IpamSweepRun).where(IpamSweepRun.org_id == env.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert [(r.state, r.error, r.target_count, r.job_id) for r in runs] == [
        ("refused", "not authorized", 0, None)
    ]
    assert runs[0].requested_by is not None
    assert runs[0].scope_digest == ""


async def test_a_technician_cannot_change_sweep_settings(api_env) -> None:
    """§11.2: enabling sweeps, designating the agent and setting the interval are
    `ipam.sweep.manage` -- a grant a technician does not hold. The same principal
    that just dispatched a sweep is refused here, which is what makes this a
    statement about the code rather than about the fixture."""
    env = await _seed(api_env)
    refused = await api_env.client.put(
        f"{IPAM}/subnets/{env.subnet_id}/sweep-settings",
        headers=env.technician,
        json={"sweep_enabled": True, "sweep_interval_hours": 168},
    )
    assert refused.status_code == 403, refused.text

    allowed = await api_env.client.put(
        f"{IPAM}/subnets/{env.subnet_id}/sweep-settings",
        headers=env.admin,
        json={"sweep_enabled": True, "sweep_interval_hours": 168},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["sweep_interval_hours"] == 168


async def test_a_read_only_user_cannot_assign_but_can_read(api_env) -> None:
    """§11.2's default grants. Both halves, against one principal: a 403 on every
    mutation is only meaningful if the same token reads successfully."""
    env = await _seed(api_env)
    listed = await api_env.client.get(f"{IPAM}/subnets", headers=env.readonly)
    assert listed.status_code == 200
    assert [row["prefix"] for row in listed.json()] == ["10.70.0.0/24"]

    for method, path, payload in (
        ("post", f"{IPAM}/addresses", {"address": "10.70.0.50", "asset_label": "printer"}),
        ("post", f"{IPAM}/subnets/{env.subnet_id}/allocate", {"asset_label": "printer"}),
        ("post", f"{IPAM}/subnets", {"prefix": "10.90.0.0/24"}),
    ):
        response = await getattr(api_env.client, method)(
            path, headers=env.readonly, json=payload
        )
        assert response.status_code == 403, f"{path}: {response.text}"


# --------------------------------------------------------------------------- #
# §0.13 -- the audit projection, which can ONLY be asserted here
# --------------------------------------------------------------------------- #


async def test_address_history_is_reachable_with_ipam_read_and_without_audit_read(
    api_env,
) -> None:
    """§0.13, and it cannot be asserted below the router.

    `address_history` is a plain service call; only a request carrying a token
    whose roles exclude `audit.read` can show that this narrow projection is
    reachable AND that the general audit API is not. Both halves run against the
    SAME principal -- a test that only called the IPAM route would pass against a
    deployment where read-only holds `audit.read` after all.

    `read-only` holds exactly `ipam.read` of this module's codes (§11.2), so it
    is the strictest principal that should still see an address's own history.
    """
    env = await _seed(api_env)
    created = await api_env.client.post(
        f"{IPAM}/addresses",
        headers=env.admin,
        json={"address": "10.70.0.42", "asset_label": "core-switch"},
    )
    assert created.status_code == 201, created.text
    address_id = created.json()["id"]

    history = await api_env.client.get(
        f"{IPAM}/addresses/{address_id}/history", headers=env.readonly
    )
    assert history.status_code == 200, history.text
    assert [entry["action"] for entry in history.json()] == ["ipam.address_assigned"]

    general = await api_env.client.get("/api/v1/audit", headers=env.readonly)
    assert general.status_code == 403, (
        "the general audit API must still require audit.read -- §0.13 grants a "
        "SLICE, not the log"
    )


async def test_address_history_response_carries_the_detail_11_3_records(
    api_env,
) -> None:
    """§11.3's claimant and method must reach the CLIENT, not just the service.

    `AddressHistoryEntry` has carried `detail` all along; the response model did
    not declare it, and Pydantic's default `extra="ignore"` dropped it in
    silence. The assertion is therefore on the HTTP BODY -- a service-level test
    reads the dataclass and passes against the bug.

    Both transitions §11.3 names a payload for are exercised in one request:
    `ipam.address_assigned` carries claimant + method, `ipam.address_released`
    carries the PREVIOUS claimant. Asserting only the first would pass against a
    projection that stripped `extra` on the release path.
    """
    env = await _seed(api_env)
    created = await api_env.client.post(
        f"{IPAM}/addresses",
        headers=env.admin,
        json={
            "address": "10.70.0.43",
            "device_id": str(env.device_id),
            "allocation_method": "dhcp",
        },
    )
    assert created.status_code == 201, created.text
    address_id = created.json()["id"]
    released = await api_env.client.post(
        f"{IPAM}/addresses/{address_id}/release", headers=env.admin
    )
    assert released.status_code == 200, released.text

    history = await api_env.client.get(
        f"{IPAM}/addresses/{address_id}/history", headers=env.readonly
    )
    assert history.status_code == 200, history.text
    body = history.json()
    assert [entry["action"] for entry in body] == [
        "ipam.address_assigned",
        "ipam.address_released",
    ]

    assigned, release_entry = body
    assert "detail" in assigned, (
        "the response dropped `detail` -- §11.3 records claimant and method on "
        f"ipam.address_assigned and the client received {sorted(assigned)}"
    )
    assert assigned["detail"]["claimant"] == str(env.device_id)
    assert assigned["detail"]["claimant_type"] == "device"
    assert assigned["detail"]["allocation_method"] == "dhcp"
    assert release_entry["detail"]["previous_claimant"] == str(env.device_id)

    # `from_state`/`to_state` are the projection's own columns and must NOT be
    # duplicated into `detail`, or the frontend has two places to read one fact.
    assert "from_state" not in assigned["detail"]
    assert "to_state" not in assigned["detail"]


# --------------------------------------------------------------------------- #
# The splat contract -- DERIVED FROM THE SOURCE, like the route table above
# --------------------------------------------------------------------------- #


def test_every_response_model_built_by_splatting_a_dataclass_forbids_extras() -> None:
    """`Out(**vars(result))` under Pydantic's default `extra="ignore"` is a
    SILENT DATA-LOSS CONSTRUCTION, and it has already cost this module §11.3's
    `detail` on the address history.

    The set of models is DERIVED FROM `api.py`'s SOURCE rather than listed here,
    for the same reason the route table is derived from the router: a hand-kept
    list is a list somebody must remember to update, and the next `**vars(...)`
    added against a plain `BaseModel` would be exactly the defect this test
    exists to prevent -- so it must fail BY EXISTING.
    """
    source = Path(api.__file__).read_text(encoding="utf-8")
    splatted = set(re.findall(r"(\w+)\(\s*\*\*(?:\{\s*\*\*)?vars\(", source))
    assert splatted, "no `**vars(...)` construction found -- this test's premise moved"

    offenders = {
        name
        for name in splatted
        if getattr(api, name).model_config.get("extra") != "forbid"
    }
    assert not offenders, (
        f"{sorted(offenders)} are built with `**vars(...)` but silently ignore "
        "undeclared fields; inherit `_ProjectionOut` so a service dataclass that "
        "grows a field fails loudly instead of dropping it"
    )


def test_a_field_the_history_model_does_not_declare_now_raises() -> None:
    """The behaviour behind the config assertion above. A dataclass that grows a
    field the DTO has not declared must RAISE, not silently drop it."""
    entry = AddressHistoryEntry(
        ts=datetime.now(UTC), actor_type="user", actor_id=None,
        action="ipam.address_assigned", from_state=None, to_state="assigned",
        detail={"claimant": "core-switch"},
    )
    assert api.HistoryEntryOut(**vars(entry)).detail == {"claimant": "core-switch"}
    with pytest.raises(ValidationError):
        api.HistoryEntryOut(**{**vars(entry), "future_field": "dropped in silence"})


# --------------------------------------------------------------------------- #
# Cross-org: 404 on every `{id}` route
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "template", "payload"),
    [
        ("get", "/subnets/{subnet_id}", None),
        ("put", "/subnets/{subnet_id}", {"name": "stolen"}),
        ("delete", "/subnets/{subnet_id}", None),
        ("get", "/subnets/{subnet_id}/addresses", None),
        ("get", "/subnets/{subnet_id}/utilization", None),
        ("get", "/subnets/{subnet_id}/next-free", None),
        ("post", "/subnets/{subnet_id}/allocate", {"asset_label": "x"}),
        ("get", "/subnets/{subnet_id}/ranges", None),
        (
            "post",
            "/subnets/{subnet_id}/ranges",
            {"kind": "dhcp_pool", "start_address": "10.70.0.100",
             "end_address": "10.70.0.200"},
        ),
        ("get", "/subnets/{subnet_id}/sweep-runs", None),
        ("put", "/subnets/{subnet_id}/sweep-settings", {"sweep_enabled": False}),
    ],
)
async def test_another_orgs_subnet_id_is_404_on_every_route(
    api_env, method: str, template: str, payload: dict[str, Any] | None
) -> None:
    """A REAL id belonging to a REAL second org, not a random UUID -- a random
    UUID 404s against any implementation, including one with no tenancy at all.

    404 and never 403: a 403 confirms the id exists, which is a cross-tenant
    existence oracle. RLS makes it the natural outcome rather than a hand-written
    check, and this asserts the outcome rather than the mechanism.
    """
    victim = await _seed(api_env)
    attacker = await _seed(api_env, prefix="10.71.0.0/24")
    path = f"{IPAM}{template}".format(subnet_id=victim.subnet_id)

    response = await getattr(api_env.client, method)(
        path, headers=attacker.admin, **({"json": payload} if payload is not None else {})
    )
    assert response.status_code == 404, f"{method.upper()} {path}: {response.text}"


async def test_another_orgs_address_and_scope_ids_are_404(api_env) -> None:
    """The same rule for the two remaining `{id}` families. The scope id is the
    interesting one: `ipam_scan_authorization` is the module's control surface,
    and a cross-org revoke would be an attacker turning off somebody else's
    audit trail as well as their sweeps."""
    victim = await _seed(api_env)
    attacker = await _seed(api_env, prefix="10.71.0.0/24")

    created = await api_env.client.post(
        f"{IPAM}/addresses",
        headers=victim.admin,
        json={"address": "10.70.0.42", "asset_label": "core-switch"},
    )
    assert created.status_code == 201
    address_id = created.json()["id"]

    scopes = await api_env.client.get(
        f"{IPAM}/scan-authorizations", headers=victim.admin
    )
    scope_id = scopes.json()[0]["id"]

    for method, path in (
        ("get", f"{IPAM}/addresses/{address_id}"),
        ("put", f"{IPAM}/addresses/{address_id}"),
        ("post", f"{IPAM}/addresses/{address_id}/release"),
        ("get", f"{IPAM}/addresses/{address_id}/history"),
        ("delete", f"{IPAM}/scan-authorizations/{scope_id}"),
        ("post", f"{IPAM}/drift/{uuid4()}/acknowledge"),
    ):
        kwargs: dict[str, Any] = {"headers": attacker.admin}
        if method in ("put", "post"):
            kwargs["json"] = {"note": "x"} if "drift" in path else {"hostname": "x"}
        response = await getattr(api_env.client, method)(path, **kwargs)
        assert response.status_code == 404, f"{method.upper()} {path}: {response.text}"

    # ...and the victim's row is untouched.
    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            await session.execute(
                select(IpamScanAuthorization).where(
                    IpamScanAuthorization.id == UUID(scope_id)
                )
            )
        ).scalar_one()
    assert row.revoked_at is None


# --------------------------------------------------------------------------- #
# The status codes §9 names
# --------------------------------------------------------------------------- #


async def test_next_free_above_the_ceiling_and_on_ipv6_is_422(api_env) -> None:
    """§0.4 and §0.5: ONE RULE, TWO APPLICATIONS, ONE ERROR MESSAGE. A /12 and a
    /64 are refused by the same check for the same reason -- the answer would be
    arithmetically hopeless rather than merely slow.

    The enumerable /24 is asserted first, so a 422 here is the ceiling refusing
    rather than the endpoint being broken.
    """
    env = await _seed(api_env)
    ok = await api_env.client.get(
        f"{IPAM}/subnets/{env.subnet_id}/next-free?count=2", headers=env.admin
    )
    assert ok.status_code == 200
    assert ok.json() == ["10.70.0.1", "10.70.0.2"]

    big = await seed_subnet(api_env, env.org_id, vrf_id=env.vrf_id, prefix="10.128.0.0/12")
    v6 = await seed_subnet(api_env, env.org_id, vrf_id=env.vrf_id, prefix="2001:db8:1::/64")
    for subnet_id in (big, v6):
        for path in ("next-free", "addresses"):
            response = await api_env.client.get(
                f"{IPAM}/subnets/{subnet_id}/{path}", headers=env.admin
            )
            assert response.status_code == 422, f"{subnet_id} {path}: {response.text}"
            assert "enumerate" in response.text


async def test_utilization_above_the_ceiling_returns_counts_with_a_null_percentage(
    api_env,
) -> None:
    """§0.4: reporting a percentage nobody can compute is worse than reporting
    none, because a `0.0` reads as "empty" rather than as "unknown". This is the
    one enumeration-bounded route that ANSWERS instead of refusing."""
    env = await _seed(api_env)
    big = await seed_subnet(api_env, env.org_id, vrf_id=env.vrf_id, prefix="10.128.0.0/12")
    response = await api_env.client.get(
        f"{IPAM}/subnets/{big}/utilization", headers=env.admin
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enumerable"] is False
    assert body["percentage"] is None
    assert body["capacity"] is None
    assert body["assigned"] == 0


async def test_assignment_inside_a_dhcp_pool_is_409(api_env) -> None:
    """§3.2: modelling a "DHCP reservation" as a static row inside a dynamic pool
    creates a record that is true in the IPAM and false on the DHCP server. A
    reservation is expressed by CARVING IT OUT of the pool.

    The address just outside the pool is asserted to succeed, so the 409 is the
    pool refusing rather than the route being broken.
    """
    env = await _seed(api_env)
    await seed_range(
        api_env, env.org_id, subnet_id=env.subnet_id, kind="dhcp_pool",
        start_address="10.70.0.100", end_address="10.70.0.200",
    )
    inside = await api_env.client.post(
        f"{IPAM}/addresses",
        headers=env.admin,
        json={"address": "10.70.0.150", "asset_label": "printer"},
    )
    assert inside.status_code == 409, inside.text

    outside = await api_env.client.post(
        f"{IPAM}/addresses",
        headers=env.admin,
        json={"address": "10.70.0.99", "asset_label": "printer"},
    )
    assert outside.status_code == 201, outside.text


async def test_a_sub_hourly_sweep_interval_is_refused_with_400(api_env) -> None:
    """§0.10 point 6, THE CLAMP THAT KEEPS THIS MODULE OUT OF THE MONITORING
    BUSINESS. A one-minute sweep interval is the exact mechanism by which an
    IPAM becomes a ping monitor.

    Zero and one are asserted together: `0` must be refused and `1` must be
    accepted, or "the clamp works" is indistinguishable from "the endpoint
    refuses everything". The stored row is read back, because a 200 that wrote
    nothing would also satisfy the second half.
    """
    from app.models import IpamSubnet

    env = await _seed(api_env)
    refused = await api_env.client.put(
        f"{IPAM}/subnets/{env.subnet_id}/sweep-settings",
        headers=env.admin,
        json={"sweep_enabled": True, "sweep_interval_hours": 0},
    )
    assert refused.status_code == 400, refused.text

    accepted = await api_env.client.put(
        f"{IPAM}/subnets/{env.subnet_id}/sweep-settings",
        headers=env.admin,
        json={"sweep_enabled": True, "sweep_interval_hours": 1},
    )
    assert accepted.status_code == 200, accepted.text

    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            await session.execute(
                select(IpamSubnet).where(IpamSubnet.id == env.subnet_id)
            )
        ).scalar_one()
    assert (row.sweep_enabled, row.sweep_interval_hours) == (True, 1)


async def test_a_duplicate_prefix_is_409_and_a_bad_gateway_is_400(api_env) -> None:
    """The two remaining mapped codes, each from a real service refusal rather
    than from a hand-rolled check in the route."""
    env = await _seed(api_env)
    duplicate = await api_env.client.post(
        f"{IPAM}/subnets", headers=env.admin,
        json={"prefix": "10.70.0.0/24", "vrf_id": str(env.vrf_id)},
    )
    assert duplicate.status_code == 409, duplicate.text

    bad_gateway = await api_env.client.post(
        f"{IPAM}/subnets", headers=env.admin,
        json={"prefix": "10.91.0.0/24", "gateway": "10.92.0.1"},
    )
    assert bad_gateway.status_code == 400, bad_gateway.text


async def test_the_registry_and_grid_round_trip(api_env) -> None:
    """One end-to-end pass over the read surface, so the response builders are
    exercised rather than merely defined -- a `response_model` that cannot build
    is a 500 on a route every other test only ever sends a 403 to."""
    env = await _seed(api_env)
    site = await api_env.client.post(
        f"{IPAM}/sites", headers=env.admin, json={"name": "Manila"}
    )
    assert site.status_code == 201, site.text
    vlan = await api_env.client.post(
        f"{IPAM}/vlans", headers=env.admin,
        json={"vid": 10, "site_id": site.json()["id"], "name": "users"},
    )
    assert vlan.status_code == 201, vlan.text
    assert (await api_env.client.get(f"{IPAM}/vrfs", headers=env.admin)).status_code == 200

    grid = await api_env.client.get(
        f"{IPAM}/subnets/{env.subnet_id}/addresses?limit=12", headers=env.admin
    )
    assert grid.status_code == 200, grid.text
    rows = grid.json()
    assert len(rows) == 12
    assert rows[0]["address"] == "10.70.0.1"
    # `.9` carries the fixture's current inventory observation and no
    # `ipam_address` row, so §3.1 row 8 applies: answering, and the plan does
    # not say so.
    assert [r["status"] for r in rows if r["address"] == "10.70.0.9"] == ["undocumented"]
    assert [r["status"] for r in rows if r["address"] == "10.70.0.1"] == ["free"]

    for path in ("drift", "orphans", "search?q=10.70.0.9", "scan-authorizations"):
        response = await api_env.client.get(f"{IPAM}/{path}", headers=env.admin)
        assert response.status_code == 200, f"{path}: {response.text}"


# --------------------------------------------------------------------------- #
# The publish boundary, on the SYNTAX TREE (mirrors tests/test_health_egress.py)
# --------------------------------------------------------------------------- #


def test_the_api_never_publishes_inside_a_tenant_transaction() -> None:
    """`publish_job` may not appear lexically inside an `async with
    org_session(...)` block in `api.py`.

    THE FAILURE THIS GUARDS IS INVISIBLE IN TESTING: a job published from inside
    the block still reaches the agent in every green test, and only diverges
    when the commit afterwards fails -- at which point an agent is executing a
    signed job the server has no durable record of having issued. That is why it
    is asserted on the syntax tree rather than by review.

    GUARD OBSERVABILITY: move the `await publish_job(...)` in `dispatch_sweep`
    inside its `async with org_session(...)` block and this fails naming that
    handler.
    """
    import ast
    from pathlib import Path

    import app.modules.ipam as ipam_pkg

    tree = ast.parse((Path(ipam_pkg.__file__).parent / "api.py").read_text())
    offenders: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function = "<module>"

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            previous, self.function = self.function, node.name
            self.generic_visit(node)
            self.function = previous

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            previous, self.function = self.function, node.name
            self.generic_visit(node)
            self.function = previous

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
            opens_tenant_txn = any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "org_session"
                for item in node.items
            )
            if opens_tenant_txn:
                inner = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
                if "publish_job" in inner:
                    offenders.append(self.function)
            self.generic_visit(node)

    visitor = _Visitor()
    visitor.visit(tree)
    assert offenders == [], (
        f"these handlers publish INSIDE their tenant transaction: {offenders}; "
        "the envelope must be published only after the org_session block exits"
    )


def test_that_syntactic_guard_can_actually_find_something() -> None:
    """THE MUTATION FOR THE TEST ABOVE, RUN IN-TEST.

    A tree walk that finds nothing because it is looking for the wrong node
    shape is indistinguishable from a clean file. This feeds it a handler that
    publishes inside the block and asserts it is caught, so the empty result
    above is evidence rather than an artefact of the walker.
    """
    import ast

    source = (
        "async def bad_handler(ctx, request):\n"
        "    async with org_session(ctx.org_id, engine=e) as session:\n"
        "        run, envelope = await create_sweep(session, je, ctx.org_id)\n"
        "        await publish_job(request.app.state.redis, d, envelope)\n"
    )
    found = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncWith)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "org_session"
            for item in node.items
        )
        and "publish_job" in ast.unparse(ast.Module(body=node.body, type_ignores=[]))
    ]
    assert len(found) == 1


async def test_the_subnet_org_predicates_hold_where_rls_is_not_doing_the_rejecting(
    api_env, bypassrls_session: AsyncSession
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    Every cross-org assertion in this file goes through a route, i.e.
    `org_session` on the NOBYPASSRLS `app_user` role, where `ipam_subnet`'s
    forced `tenant_isolation` policy removes the neighbour's row before
    `IpamSubnet.org_id == org_id` is consulted -- so those hold with the
    predicate deleted. `its_admin` carries BYPASSRLS (`tests/conftest.py`), so
    here the predicate is the only scoping.

    THE DELETE IS THE REASON THIS FILE GETS THE TEST. `delete_subnet` resolves
    through `get_subnet` and then, with `cascade=True`, issues bare `UPDATE`
    and `DELETE` statements against five more tables. A missing predicate on
    the resolve does not disclose a neighbour's address plan, it DESTROYS one:
    ranges and sweep runs deleted, addresses, observations and drift findings
    detached from the subnet that explained them. `_dependent_counts` is the
    only thing that would even raise first, and it is scoped by the same
    predicate family.

    BOTH ORGS HOLD THE SAME PREFIX, which is legal -- two tenants may each
    register 10.70.0.0/24 -- and is what makes a miss indistinguishable from a
    hit unless the ids are compared.

    FALSIFIED: deleting `IpamSubnet.org_id == org_id` from `get_subnet` returns
    org B's subnet to org A and lets `delete_subnet` remove it; deleting it
    from `list_subnets` returns both orgs' subnets.
    """
    env_a = await _seed(api_env, prefix="10.70.0.0/24")
    env_b = await _seed(api_env, prefix="10.70.0.0/24")
    actor_a = await _a_user(api_env, env_a.org_id)

    assert [s.id for s in await list_subnets(bypassrls_session, env_a.org_id)] == [
        env_a.subnet_id
    ]
    assert [s.id for s in await list_subnets(bypassrls_session, env_b.org_id)] == [
        env_b.subnet_id
    ]

    with pytest.raises(IpamNotFoundError):
        await get_subnet(bypassrls_session, env_a.org_id, env_b.subnet_id)

    with pytest.raises(IpamNotFoundError):
        await delete_subnet(
            bypassrls_session, env_a.org_id, actor_a, env_b.subnet_id, cascade=True
        )
    await bypassrls_session.rollback()

    surviving = await get_subnet(bypassrls_session, env_b.org_id, env_b.subnet_id)
    assert surviving.id == env_b.subnet_id, (
        "org A's cascading delete reached a neighbour tenant's address plan"
    )
