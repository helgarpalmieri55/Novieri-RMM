"""A client's default health profile through the API (phase 3 design §5.1,
plan 2026-10-31 Task 3).

`POST /health/assignments` names a device, a group, a client or nothing. A
client's default needs `health.admin` ON THAT CLIENT: the route's gate
narrows the request to the clients where the person holds it, and the client
is read in that session, so any other client does not exist for the request
(404). Unassigned is refused (422) and an archived client too (409). The
resolver already reads the client's default before the global one; these
tests reach it through the API, and the device view names the level.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Client, HealthProfileAssignment
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv
from tests.health_fixtures import seed_profile
from tests.test_client_record_checks import _admin, _device, _holder

ASSIGNMENTS = "/api/v1/health/assignments"


async def _unassigned(env: ApiEnv, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()


async def _defaults(env: ApiEnv, org: UUID) -> dict[UUID | None, UUID]:
    """Every default row of `org` (no device, no group), by client -> profile,
    read on the admin engine."""
    async with AsyncSession(env.admin_engine) as s:
        rows = (
            await s.execute(
                select(HealthProfileAssignment.client_id, HealthProfileAssignment.profile_id)
                .where(
                    HealthProfileAssignment.org_id == org,
                    HealthProfileAssignment.device_id.is_(None),
                    HealthProfileAssignment.device_group_id.is_(None),
                )
            )
        ).all()
    return {client: profile for client, profile in rows}


async def test_a_technician_sets_the_default_of_a_client_they_administer_and_not_another_s(
    api_env: ApiEnv,
) -> None:
    """`health.admin` on X and only `health.read` on Y: X's default is
    written (level `client`, audited with X), Y's is a 404 -- Y is outside
    the clients the route's gate narrowed the request to -- and nothing is
    written for Y."""
    org = await api_env.create_org("hcd-tech")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    profile = await seed_profile(api_env, org)
    _, tech = await _holder(
        api_env, org, {x: ("health.admin", "health.read"), y: ("health.read",)}
    )

    mine = await api_env.client.post(
        ASSIGNMENTS, headers=tech, json={"profile_id": str(profile), "client_id": str(x)}
    )
    assert mine.status_code == 201, mine.text
    body = mine.json()
    assert (body["level"], body["client_id"], body["device_id"], body["device_group_id"]) == (
        "client", str(x), None, None
    )

    theirs = await api_env.client.post(
        ASSIGNMENTS, headers=tech, json={"profile_id": str(profile), "client_id": str(y)}
    )
    assert (theirs.status_code, theirs.json()) == (404, {"detail": "no such client"})
    assert await _defaults(api_env, org) == {x: profile}

    async with AsyncSession(api_env.admin_engine) as s:
        audited = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org, AuditEvent.action == "health.assignment_created"
                )
            )
        ).scalar_one()
    assert audited.client_id == x
    assert audited.meta["level"] == "client"
    assert audited.meta["client_id"] == str(x)


async def test_the_selector_narrows_a_client_default_too(api_env: ApiEnv) -> None:
    """An org-admin narrowed to X by `X-Client-Id` cannot write Y's default:
    the header narrows, never widens."""
    org = await api_env.create_org("hcd-narrow")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    profile = await seed_profile(api_env, org)
    narrowed = {**(await _admin(api_env, org)), "X-Client-Id": str(x)}

    other = await api_env.client.post(
        ASSIGNMENTS, headers=narrowed, json={"profile_id": str(profile), "client_id": str(y)}
    )
    assert (other.status_code, other.json()) == (404, {"detail": "no such client"})
    own = await api_env.client.post(
        ASSIGNMENTS, headers=narrowed, json={"profile_id": str(profile), "client_id": str(x)}
    )
    assert own.status_code == 201, own.text
    assert await _defaults(api_env, org) == {x: profile}


async def test_unassigned_and_archived_clients_take_no_default(api_env: ApiEnv) -> None:
    """404 for a client nobody can see, 409 archived, 422 Unassigned (plan
    2026-10-26 C10), and nothing written for any of them -- asked by an
    org-admin, who can see all three."""
    org = await api_env.create_org("hcd-refused")
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    unassigned = await _unassigned(api_env, org)
    profile = await seed_profile(api_env, org)
    admin = await _admin(api_env, org)
    for client, expected in (
        (uuid4(), (404, "no such client")),
        (archived, (409, "that client is archived")),
        (unassigned, (422, "a health profile cannot be assigned to Unassigned")),
    ):
        r = await api_env.client.post(
            ASSIGNMENTS, headers=admin, json={"profile_id": str(profile), "client_id": str(client)}
        )
        assert (r.status_code, r.json()["detail"]) == expected, r.text
    assert await _defaults(api_env, org) == {}


async def test_a_client_default_names_nothing_else(api_env: ApiEnv) -> None:
    """A client with a device or a group is two targets: refused (400) with
    the sentence that names the four shapes, before anything is read."""
    org = await api_env.create_org("hcd-both")
    x, x_site = await client_with_site(api_env, org)
    device = await _device(api_env, org, x, x_site)
    profile = await seed_profile(api_env, org)
    admin = await _admin(api_env, org)
    for extra in ({"device_id": str(device)}, {"device_group_id": str(uuid4())}):
        r = await api_env.client.post(
            ASSIGNMENTS,
            headers=admin,
            json={"profile_id": str(profile), "client_id": str(x), **extra},
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == (
            "an assignment names a device, a group, a client or none of them (the org "
            "default), never more than one"
        )
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(
                select(HealthProfileAssignment.id).where(HealthProfileAssignment.org_id == org)
            )
        ).all() == []


async def test_one_default_per_client_beside_the_global_one(api_env: ApiEnv) -> None:
    """`uq_health_assignment_client_org` (NULLS NOT DISTINCT): X's second
    default is the 400 that names the first; the global default and X's live
    side by side."""
    org = await api_env.create_org("hcd-dup")
    x, _ = await client_with_site(api_env, org)
    shared = await seed_profile(api_env, org)
    mine = await seed_profile(api_env, org)
    admin = await _admin(api_env, org)

    first = await api_env.client.post(
        ASSIGNMENTS, headers=admin, json={"profile_id": str(mine), "client_id": str(x)}
    )
    assert first.status_code == 201, first.text
    second = await api_env.client.post(
        ASSIGNMENTS, headers=admin, json={"profile_id": str(shared), "client_id": str(x)}
    )
    assert second.status_code == 400, second.text
    assert "already exists" in second.json()["detail"]
    assert first.json()["id"] in second.json()["detail"]
    global_default = await api_env.client.post(
        ASSIGNMENTS, headers=admin, json={"profile_id": str(shared)}
    )
    assert global_default.status_code == 201, global_default.text
    assert global_default.json()["level"] == "org"
    assert await _defaults(api_env, org) == {x: mine, None: shared}


async def test_a_device_resolves_to_its_client_s_default_through_the_api(
    api_env: ApiEnv,
) -> None:
    """Device -> group -> its client's default -> the global one, written
    through the API and read back on the device view, which names the level
    `client` and the client. A machine of another client takes the global
    default."""
    org = await api_env.create_org("hcd-resolve")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    x_device = await _device(api_env, org, x, x_site)
    y_device = await _device(api_env, org, y, y_site)
    shared = await seed_profile(api_env, org)
    mine = await seed_profile(api_env, org)
    admin = await _admin(api_env, org)
    for body in ({"profile_id": str(shared)}, {"profile_id": str(mine), "client_id": str(x)}):
        r = await api_env.client.post(ASSIGNMENTS, headers=admin, json=body)
        assert r.status_code == 201, r.text

    seen_x = await api_env.client.get(f"/api/v1/devices/{x_device}/health", headers=admin)
    assert seen_x.status_code == 200, seen_x.text
    assert (
        seen_x.json()["resolution_level"],
        seen_x.json()["profile"]["id"],
        seen_x.json()["assignment"]["client_id"],
    ) == ("client", str(mine), str(x))
    seen_y = await api_env.client.get(f"/api/v1/devices/{y_device}/health", headers=admin)
    assert (seen_y.json()["resolution_level"], seen_y.json()["profile"]["id"]) == (
        "org", str(shared)
    )

    listed = await api_env.client.get(ASSIGNMENTS, headers=admin)
    assert sorted((row["level"], row["client_id"]) for row in listed.json()) == sorted(
        [("org", None), ("client", str(x))]
    )


async def test_a_technician_removes_their_client_s_default_and_not_another_s(
    api_env: ApiEnv,
) -> None:
    """DELETE needs nothing new: a technician of X removes X's default; Y's
    is not theirs to see (404) and stays."""
    org = await api_env.create_org("hcd-delete")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    profile = await seed_profile(api_env, org)
    admin = await _admin(api_env, org)
    ids: dict[UUID, Any] = {}
    for client in (x, y):
        r = await api_env.client.post(
            ASSIGNMENTS, headers=admin, json={"profile_id": str(profile), "client_id": str(client)}
        )
        assert r.status_code == 201, r.text
        ids[client] = r.json()["id"]
    _, tech = await _holder(api_env, org, {x: ("health.admin", "health.read")})

    hidden = await api_env.client.delete(f"{ASSIGNMENTS}/{ids[y]}", headers=tech)
    assert hidden.status_code == 404, hidden.text
    removed = await api_env.client.delete(f"{ASSIGNMENTS}/{ids[x]}", headers=tech)
    assert removed.status_code == 204, removed.text
    assert await _defaults(api_env, org) == {y: profile}
