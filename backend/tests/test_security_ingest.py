import json
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.gateway.dispatch import handle_agent_message
from app.gateway.protocol import SecurityEventDoc, parse_agent_message
from app.jobs.engine import JobEngine
from app.models import Device, Organization, Policy, SecurityEvent
from app.modules.security_logs.ingest import ingest_events

REDIS_URL = "redis://localhost:6379/9"


def _events(n: int, prefix: str = "e") -> list[SecurityEventDoc]:
    return [
        SecurityEventDoc(
            event_uid=f"{prefix}{i}",
            ts="2026-07-18T09:00:00Z",
            event_type="logon_success" if i % 2 == 0 else "logon_failure",
            username=f"user{i}",
            source="win:4624",
            outcome="success" if i % 2 == 0 else "failure",
            raw={"n": i},
        )
        for i in range(n)
    ]


async def _org_device(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"sec-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(org_id=org.id, hostname="sec1", os_family="windows")
        session.add(device)
        await session.commit()
        return org.id, device.id


async def test_batch_inserts_with_columns(admin_engine, app_engine) -> None:
    org_id, device_id = await _org_device(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        inserted = await ingest_events(session, device, _events(3))
    assert inserted == 3
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        rows = (
            (await session.execute(select(SecurityEvent).order_by(SecurityEvent.event_uid)))
            .scalars()
            .all()
        )
        assert [r.event_uid for r in rows] == ["e0", "e1", "e2"]
        assert rows[1].event_type == "logon_failure"
        assert rows[1].username == "user1"
        assert rows[1].raw == {"n": 1}
        # default policy seeded exactly once
        policies = (
            await session.execute(
                select(func.count()).select_from(Policy).where(Policy.module == "security_logs")
            )
        ).scalar_one()
        assert policies == 1
        policy = (
            await session.execute(select(Policy).where(Policy.module == "security_logs"))
        ).scalar_one()
        assert policy.config == {"retention_days": 365, "ship_interval_s": 60}


async def test_redelivery_dedupes_on_event_uid(admin_engine, app_engine) -> None:
    org_id, device_id = await _org_device(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        assert await ingest_events(session, device, _events(4)) == 4
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        # at-least-once redelivery: same uids plus one new
        assert await ingest_events(session, device, _events(5)) == 1
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        total = (
            await session.execute(select(func.count()).select_from(SecurityEvent))
        ).scalar_one()
    assert total == 5


async def test_gateway_branch_ingests_and_queues_ui_event(
    admin_engine, app_engine, tmp_path
) -> None:
    org_id, device_id = await _org_device(admin_engine)
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await redis.flushdb()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"ui:{org_id}")

    msg = parse_agent_message(
        json.dumps(
            {
                "v": 1,
                "kind": "security_events",
                "events": [
                    {
                        "event_uid": "gw1",
                        "ts": "2026-07-18T10:00:00Z",
                        "event_type": "account_lockout",
                        "username": "victim",
                        "outcome": "failure",
                    }
                ],
            }
        )
    )
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    await handle_agent_message(
        app_engine, engine, redis, msg, device_id=device_id, org_id=org_id
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = (
            await session.execute(
                select(SecurityEvent).where(SecurityEvent.event_uid == "gw1")
            )
        ).scalar_one()
        assert row.device_id == device_id  # bound identity, not message content
        assert row.event_type == "account_lockout"

    event = None
    for _ in range(30):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message:
            event = json.loads(message["data"])
            break
    await pubsub.unsubscribe()
    await redis.aclose()
    assert event is not None
    assert event["kind"] == "security_events_ingested"
    assert event["count"] == 1
    assert event["device_id"] == str(device_id)
