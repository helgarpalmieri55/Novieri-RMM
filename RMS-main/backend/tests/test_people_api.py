"""People REST API: RBAC on every route, tenant scoping, audit, and the
Person page payload (docs/modules/people/spec.md §3, §4)."""

from typing import Any
from uuid import UUID, uuid4

from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import route_permission
from app.core.tenancy import org_session
from app.models import AuditEvent, Device, Permission, Role, RolePermission, User, UserRole
from app.modules.people.api import router
from tests.conftest import ApiEnv


async def _actions(api_env: ApiEnv, org_id: UUID) -> list[str]:
    async with org_session(org_id, engine=api_env.app_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent.action)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def _device(api_env: ApiEnv, org_id: UUID) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org_id, hostname=f"pc-{uuid4().hex[:6]}", os_family="windows")
        s.add(d)
        await s.commit()
        return d.id


async def _add(api_env: ApiEnv, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    resp = await api_env.client.post("/api/v1/people", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    out: dict[str, Any] = resp.json()
    return out


async def _only(api_env: ApiEnv, org_id: UUID, *codes: str) -> dict[str, str]:
    """A user holding exactly `codes`, through a role of this org's own."""
    subject = await api_env.create_user(org_id)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        role = Role(org_id=org_id, name=f"only-{uuid4().hex[:6]}")
        s.add(role)
        await s.flush()
        for code in codes:
            perm = (await s.execute(select(Permission).where(Permission.code == code))).scalar_one()
            s.add(RolePermission(role_id=role.id, permission_id=perm.id))
        user = (
            await s.execute(select(User).where(User.external_subject == subject))
        ).scalar_one()
        s.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await s.commit()
    return await api_env.headers(org_id, subject)


def test_every_people_route_requires_a_people_permission() -> None:
    codes = {
        f"{sorted(route.methods)[0]} {route.path}": route_permission(route)
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    assert codes, "router has no routes"
    missing = {k: v for k, v in codes.items() if v is None or not v.startswith("people.")}
    assert not missing, missing
    writes = {k: v for k, v in codes.items() if not k.startswith("GET ")}
    assert all(v != "people.read" for v in writes.values()), writes


async def test_read_only_can_list_but_not_write(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("papi")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    tech_h = await api_env.headers(org_id, tech)
    ro_h = await api_env.headers(org_id, ro)
    anyone = await api_env.create_user(org_id)
    none_h = await api_env.headers(org_id, anyone)

    maria = await _add(api_env, tech_h, display_name="Maria Garcia", email="maria@corp.example")
    listing = await api_env.client.get("/api/v1/people?q=garc", headers=ro_h)
    assert listing.status_code == 200
    assert [p["display_name"] for p in listing.json()] == ["Maria Garcia"]
    assert "org_id" not in listing.json()[0]

    assert (await api_env.client.get("/api/v1/people", headers=none_h)).status_code == 403
    denied = await api_env.client.post(
        "/api/v1/people", json={"display_name": "X"}, headers=ro_h
    )
    assert denied.status_code == 403
    patch = await api_env.client.patch(
        f"/api/v1/people/{maria['id']}", json={"department": "Finance"}, headers=ro_h
    )
    assert patch.status_code == 403
    patch = await api_env.client.patch(
        f"/api/v1/people/{maria['id']}", json={"department": "Finance"}, headers=tech_h
    )
    assert patch.status_code == 200 and patch.json()["department"] == "Finance"

    dup = await api_env.client.post(
        "/api/v1/people", json={"display_name": "M", "email": "MARIA@corp.example"},
        headers=tech_h,
    )
    assert dup.status_code == 409
    bad = await api_env.client.post(
        "/api/v1/people", json={"display_name": "M", "email": "not-an-address"}, headers=tech_h
    )
    assert bad.status_code == 422

    actions = await _actions(api_env, org_id)
    assert "people.created" in actions and "people.updated" in actions


async def test_another_orgs_person_and_device_are_not_found(api_env: ApiEnv) -> None:
    org_a = await api_env.create_org("papi")
    org_b = await api_env.create_org("papi")
    a_h = await api_env.headers(org_a, await api_env.create_user(org_a, "org-admin"))
    b_h = await api_env.headers(org_b, await api_env.create_user(org_b, "org-admin"))
    theirs = await _add(api_env, b_h, display_name="Eve", email="eve@b.example")
    their_device = await _device(api_env, org_b)

    got = await api_env.client.get(f"/api/v1/people/{theirs['id']}", headers=a_h)
    assert got.status_code == 404
    assert (
        await api_env.client.patch(
            f"/api/v1/people/{theirs['id']}", json={"job_title": "x"}, headers=a_h
        )
    ).status_code == 404
    assert (
        await api_env.client.get(f"/api/v1/people/devices/{their_device}", headers=a_h)
    ).status_code == 404
    mine = await _device(api_env, org_a)
    assert (
        await api_env.client.put(
            f"/api/v1/people/devices/{mine}", json={"person_id": theirs["id"]}, headers=a_h
        )
    ).status_code == 404
    listing = await api_env.client.get("/api/v1/people", headers=a_h)
    assert all(p["id"] != theirs["id"] for p in listing.json())


async def test_person_page_shows_what_they_hold_and_hides_what_the_viewer_cannot_read(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("papi")
    admin_h = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    ro_h = await _only(api_env, org_id, "people.read", "subscriptions.read")
    maria = await _add(api_env, admin_h, display_name="Maria", email="maria@corp.example")
    device = await _device(api_env, org_id)

    put = await api_env.client.put(
        f"/api/v1/people/devices/{device}", json={"person_id": maria["id"]}, headers=admin_h
    )
    assert put.status_code == 200, put.text
    assert put.json()["person"]["id"] == maria["id"] and put.json()["source"] == "operator"

    sub = await api_env.client.post(
        "/api/v1/subscriptions",
        json={"service_name": "Slack", "billing_cycle": "monthly", "cost_per_cycle": 5,
              "currency": "USD", "seats": 3},
        headers=admin_h,
    )
    seat = await api_env.client.post(
        f"/api/v1/subscriptions/{sub.json()['id']}/seats",
        json={"assignee_name": "Maria", "assignee_email": "maria@corp.example"},
        headers=admin_h,
    )
    assert seat.status_code == 201

    page = await api_env.client.get(f"/api/v1/people/{maria['id']}", headers=admin_h)
    assert page.status_code == 200, page.text
    body = page.json()
    assert [d["device_id"] for d in body["devices"]] == [str(device)]
    assert [s["service_name"] for s in body["seats"]] == ["Slack"]
    assert body["licences"] == [] and body["tickets"] == []
    assert body["checklist"] is None and body["account"] is None

    # Sections the viewer cannot read in their own module are hidden, not empty.
    ro_page = (await api_env.client.get(f"/api/v1/people/{maria['id']}", headers=ro_h)).json()
    assert ro_page["seats"] is not None
    assert ro_page["tickets"] is None and ro_page["licences"] is None


async def test_leaver_needs_offboard_and_starter_does_not(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("papi")
    tech_h = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    admin_h = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    lee = await _add(api_env, admin_h, display_name="Lee", email="lee@corp.example")
    base = f"/api/v1/people/{lee['id']}/checklists"

    refused = await api_env.client.post(base, json={"kind": "leaver"}, headers=tech_h)
    assert refused.status_code == 403

    starter = await api_env.client.post(base, json={"kind": "starter"}, headers=tech_h)
    assert starter.status_code == 201, starter.text
    body = starter.json()
    assert [s["key"] for s in body["steps"]][:2] == ["account", "device"]
    cancel = await api_env.client.post(f"{base}/{body['id']}/cancel", headers=tech_h)
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancelled"

    leaver = await api_env.client.post(base, json={"kind": "leaver"}, headers=admin_h)
    assert leaver.status_code == 201
    lid = leaver.json()["id"]
    # A technician may not tick or close a leaver checklist either.
    tick = await api_env.client.patch(
        f"{base}/{lid}/steps/tickets", json={"state": "done"}, headers=tech_h
    )
    assert tick.status_code == 403
    tick = await api_env.client.patch(
        f"{base}/{lid}/steps/access", json={"state": "skipped", "note": "Never had one"},
        headers=admin_h,
    )
    assert tick.status_code == 200, tick.text
    done = await api_env.client.post(f"{base}/{lid}/complete", headers=admin_h)
    assert done.status_code == 200, done.text
    page = (await api_env.client.get(f"/api/v1/people/{lee['id']}", headers=admin_h)).json()
    assert page["person"]["status"] == "left"
    assert page["past_checklists"][0]["status"] == "completed"

    unknown = await api_env.client.patch(
        f"{base}/{lid}/steps/nope", json={"state": "done"}, headers=admin_h
    )
    assert unknown.status_code in (400, 404)
    actions = await _actions(api_env, org_id)
    for action in (
        "people.checklist_started",
        "people.checklist_cancelled",
        "people.checklist_step_updated",
        "people.checklist_completed",
    ):
        assert action in actions, action


async def test_unmatched_seats_can_be_linked_or_added_as_people(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("papi")
    tech_h = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    sub = await api_env.client.post(
        "/api/v1/subscriptions",
        json={"service_name": "Zoom", "billing_cycle": "monthly", "cost_per_cycle": 5,
              "currency": "USD", "seats": 3},
        headers=tech_h,
    )
    seat = await api_env.client.post(
        f"/api/v1/subscriptions/{sub.json()['id']}/seats",
        json={"assignee_name": "Pat Doe", "assignee_email": "pat@corp.example"},
        headers=tech_h,
    )
    queue = (await api_env.client.get("/api/v1/people/unmatched", headers=tech_h)).json()
    assert [s["assignee_email"] for s in queue["seats"]] == ["pat@corp.example"]

    added = await api_env.client.post(
        f"/api/v1/people/unmatched/seats/{seat.json()['id']}/link", json={}, headers=tech_h
    )
    assert added.status_code == 200, added.text
    assert added.json()["display_name"] == "Pat Doe"
    queue = (await api_env.client.get("/api/v1/people/unmatched", headers=tech_h)).json()
    assert queue["seats"] == []


async def test_by_user_finds_the_person_for_a_ticket_requester(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("papi")
    sub = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, sub)
    me = (await api_env.client.get("/api/v1/me", headers=headers)).json()
    found = await api_env.client.get(f"/api/v1/people/by-user/{me['user_id']}", headers=headers)
    assert found.status_code == 200, found.text
    assert found.json()["user_id"] == me["user_id"]
    missing = await api_env.client.get(f"/api/v1/people/by-user/{uuid4()}", headers=headers)
    assert missing.status_code == 404
