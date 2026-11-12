"""`GET /portal/me` (own authentication phase 2, Task 4, decision 8): the
person using the portal, for the portal's own pages -- a technician with a
portal code as much as a contact -- and nothing for somebody without one."""

from typing import Any

API = "/api/v1/portal/me"


async def test_a_technician_holding_the_portal_role_is_named(api_env: Any) -> None:
    org_id = await api_env.create_org("portal-me")
    sub = await api_env.create_user(org_id, "requester")
    got = await api_env.client.get(API, headers=await api_env.headers(org_id, sub))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["kind"] == "staff"
    assert body["email"] == f"{sub}@test.local"
    assert body["permissions"] == ["portal.ticket.read", "portal.ticket.write"]


async def test_somebody_without_a_portal_code_gets_nothing(api_env: Any) -> None:
    org_id = await api_env.create_org("portal-me-no")
    sub = await api_env.create_user(org_id, "technician")
    got = await api_env.client.get(API, headers=await api_env.headers(org_id, sub))
    assert got.status_code == 403


async def test_without_a_session_it_is_401(api_env: Any) -> None:
    assert (await api_env.client.get(API)).status_code == 401
