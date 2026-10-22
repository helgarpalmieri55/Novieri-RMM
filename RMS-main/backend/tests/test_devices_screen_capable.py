"""`screen_capable` on `GET /devices`: can this device's agent share its screen?

**WHY THE CONSOLE NEEDS IT.** Remote sessions start a screen share, and the
server refuses one with 409 "agent too old to share its screen" when the
agent's self-reported capabilities do not include `remote.screen.open`
(`app/modules/remote/service.py`). Nothing the console could read said so
beforehand, so the first an operator learned of an old agent was the refusal
(`docs/design/cross-module-guidance.md` §2 Remote #2, §3).

**ONE SOURCE OF TRUTH.** The field is derived by the same function the refusal
is decided by (`app.modules.remote.capability.can_share_screen`), from the same
column (`agents.capabilities`). The last test here pins the two together
through HTTP: a device the list calls not capable is one the start route
refuses, and one it calls capable is one the route accepts. A field computed by
a copy of the rule would agree with the refusal only until one of them changed.

**THREE VALUES, NOT TWO.** `null` for a device with no agent at all -- there is
nothing to be too old -- which is a different remedy (install an agent) from
`false` (update the one it has).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Agent, Device, Organization
from app.modules.remote.capability import SCREEN_JOB_TYPE, can_share_screen
from app.services.device_list import device_page

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
SCREEN = {SCREEN_JOB_TYPE: True}


async def _org(admin_engine: AsyncEngine, prefix: str) -> UUID:
    slug = f"{prefix}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _device(
    engine: AsyncEngine,
    org_id: UUID,
    hostname: str,
    *,
    capabilities: dict[str, bool] | None,
    agent: bool = True,
) -> UUID:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=hostname, os_family="windows")
        session.add(device)
        await session.flush()
        if agent:
            session.add(
                Agent(
                    org_id=org_id,
                    device_id=device.id,
                    cert_fingerprint=uuid4().hex + uuid4().hex,
                    cert_serial=uuid4().hex,
                    cert_not_after=datetime.now(UTC) + timedelta(days=300),
                    enrolled_at=datetime.now(UTC),
                    status="online",
                    capabilities=capabilities if capabilities is not None else {},
                )
            )
        await session.commit()
        return device.id


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        (SCREEN, True),
        ({SCREEN_JOB_TYPE: False}, False),
        ({}, False),
        ({"inventory.collect": True}, False),
        (None, False),
        (["remote.screen.open"], False),
    ],
)
def test_the_rule_reads_the_capability_the_agent_reported(
    capabilities: object, expected: bool
) -> None:
    assert can_share_screen(capabilities) is expected


async def test_the_page_carries_the_capability_per_row(admin_engine: AsyncEngine) -> None:
    org_id = await _org(admin_engine, "sc-page")
    await _device(admin_engine, org_id, "a-new", capabilities=SCREEN)
    await _device(admin_engine, org_id, "b-old", capabilities={"inventory.collect": True})
    await _device(admin_engine, org_id, "c-asset", capabilities=None, agent=False)

    async with AsyncSession(admin_engine) as session:
        page = await device_page(session, org_id, now=NOW)

    by_host = {device.hostname: capable for device, _, _, _, capable, _ in page.rows}
    assert by_host == {"a-new": True, "b-old": False, "c-asset": None}


async def test_the_route_serves_screen_capable_per_device(api_env) -> None:
    org_id = await api_env.create_org("sc-route")
    await _device(api_env.admin_engine, org_id, "a-new", capabilities=SCREEN)
    await _device(api_env.admin_engine, org_id, "b-old", capabilities={})
    await _device(api_env.admin_engine, org_id, "c-asset", capabilities=None, agent=False)
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "read-only"))

    response = await api_env.client.get("/api/v1/devices", headers=headers)
    assert response.status_code == 200, response.text
    served = {item["hostname"]: item["screen_capable"] for item in response.json()["items"]}
    assert served == {"a-new": True, "b-old": False, "c-asset": None}

    # The single-device read says the same thing about the same machine.
    for item in response.json()["items"]:
        one = await api_env.client.get(f"/api/v1/devices/{item['id']}", headers=headers)
        assert one.status_code == 200, one.text
        assert one.json()["screen_capable"] == item["screen_capable"]


async def test_the_other_orgs_devices_do_not_appear(api_env) -> None:
    """Tenant-scoped like the rest of the list: another org's capable agent
    is not listed, so it cannot make this org look ready."""
    mine = await api_env.create_org("sc-mine")
    theirs = await api_env.create_org("sc-theirs")
    await _device(api_env.admin_engine, mine, "ours-old", capabilities={})
    await _device(api_env.admin_engine, theirs, "theirs-new", capabilities=SCREEN)
    headers = await api_env.headers(mine, await api_env.create_user(mine, "read-only"))

    body = (await api_env.client.get("/api/v1/devices", headers=headers)).json()
    assert [(i["hostname"], i["screen_capable"]) for i in body["items"]] == [
        ("ours-old", False)
    ]


async def test_the_field_predicts_the_start_routes_answer(api_env) -> None:
    """The console warns on `screen_capable: false`; the server refuses on the
    same fact. If these ever disagree the warning is wrong in one direction or
    the other, so they are asserted together, through HTTP."""
    org_id = await api_env.create_org("sc-agree")
    capable = await _device(api_env.admin_engine, org_id, "a-new", capabilities=SCREEN)
    too_old = await _device(api_env.admin_engine, org_id, "b-old", capabilities={})
    tech = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))

    listed = {
        item["id"]: item["screen_capable"]
        for item in (await api_env.client.get("/api/v1/devices", headers=tech)).json()["items"]
    }
    assert listed == {str(capable): True, str(too_old): False}

    refused = await api_env.client.post(
        "/api/v1/remote/sessions",
        json={"device_id": str(too_old), "reason": "INC-4821 printer"},
        headers=tech,
    )
    assert refused.status_code == 409
    assert "too old" in refused.json()["detail"]

    accepted = await api_env.client.post(
        "/api/v1/remote/sessions",
        json={"device_id": str(capable), "reason": "INC-4821 printer"},
        headers=tech,
    )
    assert accepted.status_code == 201, accepted.text
