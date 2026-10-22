"""POST /enroll/v1 with `link_code` (install-link spec §1.4, §5 Part 1)."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, AuditEvent, Device, DomainEvent, InstallLink
from tests.install_link_fixtures import FACTS, TRAEFIK, make_csr_pem, peer_client, switch_on

NOT_FOUND = {"detail": "Not Found"}
INSIDE = "203.0.113.5, 10.50.0.9"  # rightmost (Traefik's) is inside 10.50.0.0/16


def _body(code: str) -> dict:
    return {
        "link_code": code,
        "csr_pem": make_csr_pem(),
        "device_facts": FACTS,
        "capabilities": {"agent.uninstall": True, "inventory.collect": True},
    }


async def _enroll(api_env, code: str, *, peer: str = TRAEFIK, xff: str | None = INSIDE):
    async with peer_client(api_env, peer) as client:
        headers = {"X-Forwarded-For": xff} if xff is not None else {}
        return await client.post("/enroll/v1", json=_body(code), headers=headers)


async def test_a_link_enrollment_creates_a_pending_device_with_a_certificate(api_env) -> None:
    org_id = await api_env.create_org("lnk")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    response = await _enroll(api_env, code)
    assert response.status_code == 201, response.text
    body = response.json()
    device_id = UUID(body["device_id"])
    assert body["cert_pem"].startswith("-----BEGIN CERTIFICATE")

    async with AsyncSession(api_env.admin_engine) as session:
        device = await session.get(Device, device_id)
        agent = (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()
        audit = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "agent.enrolled_via_link"
                )
            )
        ).scalar_one()
        event = (
            await session.execute(
                select(DomainEvent).where(
                    DomainEvent.org_id == org_id, DomainEvent.event_type == "device.joined_via_link"
                )
            )
        ).scalar_one()
    assert device is not None and device.org_id == org_id
    assert (device.review_state, device.provenance, device.source) == ("pending", "agent", "agent")
    assert str(device.link_joined_from) == "10.50.0.9" and device.link_joined_at is not None
    assert agent.capabilities == {"agent.uninstall": True, "inventory.collect": True}
    assert audit.meta["source_address"] == "10.50.0.9"
    assert code not in str(audit.meta)
    assert event.payload["device_id"] == str(device_id)
    assert event.payload["source_address"] == "10.50.0.9"


@pytest.mark.parametrize(
    "case", ["off", "wrong_code", "outside", "switched_off_by_time", "malformed"]
)
async def test_every_refusal_is_the_same_404(api_env, case: str) -> None:
    org_id = await api_env.create_org("lnk")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    xff = INSIDE
    if case == "off":
        admin = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
        await api_env.client.put(
            "/api/v1/install-link",
            json={"enabled": False, "networks": ["10.50.0.0/16"]},
            headers=admin,
        )
    elif case == "wrong_code":
        code = code[:-1] + ("A" if code[-1] != "A" else "B")
    elif case == "outside":
        xff = "10.50.0.9, 192.0.2.44"
    elif case == "switched_off_by_time":
        async with AsyncSession(api_env.admin_engine) as session:
            await session.execute(
                update(InstallLink)
                .where(InstallLink.org_id == org_id)
                .values(disable_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
    elif case == "malformed":
        code = "not-a-code"
    response = await _enroll(api_env, code, xff=xff)
    assert response.status_code == 404, response.text
    assert response.json() == NOT_FOUND


async def test_a_client_supplied_header_cannot_claim_an_allowed_network(api_env) -> None:
    org_id = await api_env.create_org("lnk")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    # Through Traefik: the client's own claim is NOT the rightmost entry.
    assert (await _enroll(api_env, code, xff="10.50.0.9, 203.0.113.5")).status_code == 404
    # Straight to the API from outside the trusted set: the header is ignored.
    assert (await _enroll(api_env, code, peer="198.51.100.7", xff="10.50.0.9")).status_code == 404
    # The same peer, when it IS inside the network, is let in on its own address.
    assert (await _enroll(api_env, code, peer="10.50.3.3", xff=None)).status_code == 201


async def test_regenerating_stops_the_old_code_at_once(api_env) -> None:
    org_id = await api_env.create_org("lnk")
    old = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    admin = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    new = (
        (await api_env.client.post("/api/v1/install-link/regenerate", headers=admin))
        .json()["url"]
        .rsplit("/i/", 1)[1]
    )
    assert (await _enroll(api_env, old)).status_code == 404
    assert (await _enroll(api_env, new)).status_code == 201


async def test_the_fifty_first_enrollment_in_an_hour_is_429_and_audited_once(api_env) -> None:
    org_id = await api_env.create_org("lnk")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    joined = datetime.now(UTC) - timedelta(minutes=10)
    async with AsyncSession(api_env.admin_engine) as session:
        session.add_all(
            [
                Device(
                    org_id=org_id,
                    hostname=f"pc-{i}",
                    review_state="pending",
                    link_joined_from="10.50.0.9",
                    link_joined_at=joined,
                )
                for i in range(50)
            ]
        )
        await session.commit()
    first = await _enroll(api_env, code)
    second = await _enroll(api_env, code)
    assert first.status_code == second.status_code == 429
    # The oldest of the fifty joined 10 minutes ago: a slot frees in ~50.
    retry_after = int(first.headers["retry-after"])
    assert 49 * 60 <= retry_after <= 50 * 60 + 5, retry_after
    async with AsyncSession(api_env.admin_engine) as session:
        audited = (
            await session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "enrollment.link_rate_limited"
                )
            )
        ).scalar_one()
        devices = (
            await session.execute(
                select(func.count()).select_from(Device).where(Device.org_id == org_id)
            )
        ).scalar_one()
    assert audited == 1
    assert devices == 50


async def test_a_code_only_ever_enrols_into_its_own_org(api_env) -> None:
    org_a = await api_env.create_org("lnk-a")
    org_b = await api_env.create_org("lnk-b")
    code_a = await switch_on(api_env, org_a, ["10.50.0.0/16"])
    await switch_on(api_env, org_b, ["10.50.0.0/16"])
    response = await _enroll(api_env, code_a)
    assert response.status_code == 201
    async with AsyncSession(api_env.admin_engine) as session:
        device = await session.get(Device, UUID(response.json()["device_id"]))
        in_b = (
            await session.execute(
                select(func.count()).select_from(Device).where(Device.org_id == org_b)
            )
        ).scalar_one()
    assert device is not None and device.org_id == org_a
    assert in_b == 0


async def test_concurrent_enrollments_cannot_overrun_the_hourly_limit(api_env) -> None:
    """The link row is locked FOR UPDATE for the whole enrollment, so six
    requests racing for a limit of three produce exactly three machines."""
    org_id = await api_env.create_org("lnk")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"], hourly_limit=3)
    responses = await asyncio.gather(*[_enroll(api_env, code) for _ in range(6)])
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 201, 201, 429, 429, 429], [r.text for r in responses]
    async with AsyncSession(api_env.admin_engine) as session:
        joined = (
            await session.execute(
                select(func.count()).select_from(Device).where(Device.org_id == org_id)
            )
        ).scalar_one()
    assert joined == 3


async def test_a_body_must_carry_exactly_one_credential(api_env) -> None:
    both = {"token": "x.y", "link_code": "A" * 32, "csr_pem": make_csr_pem(), "device_facts": FACTS}
    neither = {"csr_pem": make_csr_pem(), "device_facts": FACTS}
    assert (await api_env.client.post("/enroll/v1", json=both)).status_code == 422
    assert (await api_env.client.post("/enroll/v1", json=neither)).status_code == 422


async def test_the_link_path_never_links_onto_an_existing_asset(api_env) -> None:
    """Plan D3: agent-reported MACs are not evidence enough to hand an
    operator-vouched row to a machine nobody has reviewed."""
    org_id = await api_env.create_org("lnk")
    mac = "00:11:22:33:44:55"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        # The `nics` shape `app/services/matching.py::_device_macs` reads.
        asset = Device(
            org_id=org_id,
            hostname="lab-pc-07",
            source="manual",
            provenance="operator",
            nics={"items": [{"identifier": "eth0", "mac": mac}]},
        )
        session.add(asset)
        await session.commit()
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    body = _body(code) | {"device_facts": FACTS | {"macs": [mac]}}
    async with peer_client(api_env, TRAEFIK) as client:
        response = await client.post("/enroll/v1", json=body, headers={"X-Forwarded-For": INSIDE})
    assert response.status_code == 201
    assert UUID(response.json()["device_id"]) != asset.id

    # CONTROL, so the assertion above cannot pass on a fixture the matcher
    # never reads: the TOKEN path, with the same MAC, does link to the asset.
    admin = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    token = (
        await api_env.client.post(
            "/api/v1/enrollment-tokens",
            json={"name": "control", "expires_in_hours": 1, "max_uses": 1},
            headers=admin,
        )
    ).json()["token"]
    linked = await api_env.client.post(
        "/enroll/v1",
        json={"token": token, "csr_pem": make_csr_pem(), "device_facts": FACTS | {"macs": [mac]}},
    )
    assert linked.status_code == 201 and UUID(linked.json()["device_id"]) == asset.id


async def test_retry_after_is_never_below_a_minute(api_env) -> None:
    """An agent retries link enrollment on 429 after Retry-After; a slot that
    frees in seconds still gets a minute, so a fleet does not hammer the API."""
    org_id = await api_env.create_org("lnk")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"], hourly_limit=1)
    async with AsyncSession(api_env.admin_engine) as session:
        session.add(
            Device(
                org_id=org_id,
                hostname="pc-old",
                review_state="pending",
                link_joined_from="10.50.0.9",
                link_joined_at=datetime.now(UTC) - timedelta(minutes=59, seconds=50),
            )
        )
        await session.commit()
    response = await _enroll(api_env, code)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) == 60

