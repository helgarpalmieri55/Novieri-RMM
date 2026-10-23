"""An asset with no hostname and no OS must not break a read path (§6.5).

WHY THIS FILE EXISTS SEPARATELY FROM THE API TESTS. Spec §2 says the migration
is trivial and "the audit of read paths is the work". Dropping two `NOT NULL`s
changes what EVERY existing consumer may receive, and none of them was written
expecting `None`. The failure modes are not symmetrical:

  * a Pydantic response model declaring `hostname: str` raises at SERIALIZATION
    time, so the device list 500s for the WHOLE ORG because one row is a
    switch;
  * an f-string renders `None` as the four characters "None", which an operator
    reads as a hostname;
  * `.lower()` or a dict lookup on `None` raises deep inside a sweep.

The first is the dangerous one: one warehouse laptop takes out the fleet list
for everybody. mypy found the other three at the point of the schema change
(see 0061's commit); these are the ones only a request can show.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session
from app.models import Device
from tests.conftest import ApiEnv


async def _bare_asset(api_env: ApiEnv, org_id: UUID) -> UUID:
    """A switch: no hostname, no OS, no agent. The row this feature exists for."""
    async with org_session(org_id, engine=api_env.app_engine) as session:
        device = Device(
            org_id=org_id,
            hostname=None,
            os_family=None,
            asset_type="network",
            lifecycle="unmanageable",
            source="manual",
            # 0078 pairs the two: a `manual` row is one a person entered.
            provenance="operator",
            description="core switch, rack 3",
            primary_ip="172.22.104.1",
        )
        session.add(device)
        await session.flush()
        device_id = device.id
        await session.commit()
    return device_id


async def test_the_device_list_survives_an_asset_with_no_identity(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    await _bare_asset(api_env, org_id)

    resp = await api_env.client.get(
        "/api/v1/devices", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()["items"][0]
    assert row["hostname"] is None
    assert row["os_family"] is None
    # NOT the string "None", and not "". Both read as a fact the platform does
    # not have. The console decides how to render an absence; the API's job is
    # to say that it IS one.
    assert row["hostname"] != "None"


async def test_the_device_detail_route_carries_the_asset_fields(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    device_id = await _bare_asset(api_env, org_id)

    resp = await api_env.client.get(
        f"/api/v1/devices/{device_id}", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["asset_type"] == "network"
    assert body["lifecycle"] == "unmanageable"
    assert body["source"] == "manual"
    assert body["primary_ip"] == "172.22.104.1"
    # No agent row, so no agent state -- which is how the console knows to omit
    # Patch, Backup and Health rather than render them empty.
    assert body["agent_status"] is None


async def test_a_managed_device_still_reports_what_it_always_did(
    api_env: ApiEnv,
) -> None:
    """Non-vacuity for the two above, and a non-regression for the fleet.

    Every existing device predates 0061, so it must come back with the
    defaults that describe it truthfully: it came from an agent, and it is in
    service. A migration that defaulted otherwise would have reclassified the
    entire estate in one statement.
    """
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        device = Device(org_id=org_id, hostname="ws-01", os_family="linux")
        session.add(device)
        await session.commit()

    resp = await api_env.client.get(
        "/api/v1/devices", headers=await api_env.headers(org_id, admin)
    )
    row = resp.json()["items"][0]
    assert row["hostname"] == "ws-01"
    assert row["source"] == "agent"
    assert row["lifecycle"] == "in_service"


async def test_another_orgs_asset_answers_404_and_the_row_stays_where_it_was(
    api_env: ApiEnv,
) -> None:
    """Falsifier 9: the other tenant gets a 404, and the row does not move.

    Through the API it must 404 for the other tenant; on the BYPASSRLS ADMIN
    engine the row must still belong to org A. Be exact about which half does
    what. The 404 is RLS -- the route runs on the app role, so this holds with
    the read path's `org_id` filter deleted, and the status code is what it
    pins (404, never a 403, which would confirm the id exists). The
    admin-engine read is not a second, RLS-free look at that filter; it is the
    check that the request did not MOVE or rewrite the row on its way to
    answering 404.
    """
    org_a = await api_env.create_org("assets-a")
    org_b = await api_env.create_org("assets-b")
    admin_b = await api_env.create_user(org_b, "org-admin")
    device_id = await _bare_asset(api_env, org_a)

    resp = await api_env.client.get(
        f"/api/v1/devices/{device_id}", headers=await api_env.headers(org_b, admin_b)
    )
    assert resp.status_code == 404, resp.text

    async with AsyncSession(api_env.admin_engine) as session:
        owner = (
            await session.execute(select(Device.org_id).where(Device.id == device_id))
        ).scalar_one()
    assert owner == org_a
    assert owner != org_b
