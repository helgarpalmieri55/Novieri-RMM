"""C2: the heartbeat did nothing, and that opened a false `agent.offline` alert
on every healthy device.

THE DEFECT, as it stood. `handle_agent_message` returned immediately for a
`HeartbeatMsg` (`dispatch.py:290`). `agents.last_seen_at` was written at exactly
two moments: `_mark_online` at connect and `_mark_offline` at disconnect
(`gateway/main.py:260,281`). `run_offline_sweep` opens an `agent.offline` alert
for any active device whose `Agent.last_seen_at` is older than
`health_org_settings.offline_after_s`, whose default is 900 seconds.

So an agent that connected, stayed connected, and heartbeated every minute was
reported OFFLINE after sixteen minutes, with its socket open and jobs flowing
down it. Nothing in the Python suite went red, because nothing in the Python
suite had an agent that stayed connected.

WHAT THESE TESTS ASSERT. Not "no exception". The stored timestamp, and the
alert count out of `run_offline_sweep` -- the second is the one that matters,
because a fix that wrote `last_seen_at` to the wrong row would satisfy the
first.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.signing import LocalEd25519Signer
from app.gateway import dispatch as dispatch_mod
from app.gateway.dispatch import handle_agent_message
from app.gateway.protocol import HeartbeatMsg, parse_agent_message
from app.jobs.engine import JobEngine
from app.models import Agent, AuditEvent, HealthAlert
from app.modules.health.sweep import run_offline_sweep
from tests.health_fixtures import (
    seed_assignment,
    seed_check,
    seed_org,
    seed_profile,
    seed_settings,
)

pytestmark = pytest.mark.usefixtures("admin_engine")

REDIS_URL = "redis://localhost:6379/9"


@pytest.fixture(autouse=True)
def _clean_rate_limiter():
    """The touch rate limiter is process-global by design (it exists to stop a
    chatty agent turning a heartbeat into a write amplifier). Clear it around
    every test so one test's device cannot suppress another's write."""
    dispatch_mod.reset_heartbeat_rate_limit()
    yield
    dispatch_mod.reset_heartbeat_rate_limit()


async def _seed_agent(admin_engine, org_id, device_id, *, last_seen_at):
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Agent(
                org_id=org_id,
                device_id=device_id,
                cert_fingerprint=uuid4().hex,
                cert_serial=uuid4().hex[:16],
                cert_not_after=now + timedelta(days=30),
                enrolled_at=now - timedelta(days=1),
                status="online",
                last_seen_at=last_seen_at,
            )
        )
        await session.commit()


async def _last_seen(admin_engine, device_id):
    async with AsyncSession(admin_engine) as session:
        return (
            await session.execute(select(Agent.last_seen_at).where(Agent.device_id == device_id))
        ).scalar_one()


async def _arm_offline_check(admin_engine, org, *, offline_after_s=900):
    profile_id = await seed_profile(admin_engine, org.org_id)
    await seed_check(
        admin_engine,
        org.org_id,
        profile_id,
        check_type="agent.offline",
        comparator="gt",
        threshold_num=0,
    )
    await seed_assignment(admin_engine, org.org_id, profile_id)
    await seed_settings(admin_engine, org.org_id, offline_after_s=offline_after_s)


async def _deliver_heartbeat(app_engine, tmp_path, *, device_id, org_id, ts=None):
    """Delivered through `parse_agent_message`, not by constructing the model:
    the frame the Go agent puts on the wire is the thing under test, and a
    hand-built model would keep passing if the wire shape drifted."""
    frame = json.dumps(
        {
            "v": 1,
            "kind": "heartbeat",
            "ts": ts or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    msg = parse_agent_message(frame)
    assert isinstance(msg, HeartbeatMsg)
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "hb.key"))
    try:
        await handle_agent_message(
            app_engine, engine, redis, msg, device_id=device_id, org_id=org_id
        )
    finally:
        await redis.aclose()


# ---------------------------------------------------------------------------


async def test_a_heartbeat_advances_last_seen_at(admin_engine, app_engine, tmp_path) -> None:
    org = await seed_org(admin_engine, "hb1")
    stale = datetime.now(UTC) - timedelta(minutes=20)
    await _seed_agent(admin_engine, org.org_id, org.device_id, last_seen_at=stale)

    await _deliver_heartbeat(app_engine, tmp_path, device_id=org.device_id, org_id=org.org_id)

    after = await _last_seen(admin_engine, org.device_id)
    assert after > stale, "the heartbeat left the connect-time timestamp in place"
    assert (datetime.now(UTC) - after).total_seconds() < 60


async def test_a_heartbeat_prevents_the_false_offline_alert(
    admin_engine, app_engine, tmp_path
) -> None:
    """THE ASSERTION THAT MATTERS. A device connected 20 minutes ago, past the
    900s default, heartbeating normally. Without the fix `run_offline_sweep`
    opens one `agent.offline` alert while the socket is open."""
    org = await seed_org(admin_engine, "hb2")
    await _arm_offline_check(admin_engine, org, offline_after_s=900)
    await _seed_agent(
        admin_engine,
        org.org_id,
        org.device_id,
        last_seen_at=datetime.now(UTC) - timedelta(minutes=20),
    )

    await _deliver_heartbeat(app_engine, tmp_path, device_id=org.device_id, org_id=org.org_id)

    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert await run_offline_sweep(session, org.org_id, now=now) == {
            "opened": 0,
            "cleared": 0,
        }, "a heartbeating agent was reported offline while its socket was open"
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        alerts = (
            (
                await session.execute(
                    select(HealthAlert).where(HealthAlert.org_id == org.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert alerts == [], f"opened {[a.check_type for a in alerts]}"


async def test_without_a_heartbeat_the_same_device_is_reported_offline(
    admin_engine, app_engine, tmp_path
) -> None:
    """The negative that makes the test above mean something. Same org, same
    ages, no heartbeat delivered: exactly one alert. If this ever goes green
    with zero, the test above is passing because the sweep is broken, not
    because the fix works."""
    org = await seed_org(admin_engine, "hb3")
    await _arm_offline_check(admin_engine, org, offline_after_s=900)
    await _seed_agent(
        admin_engine,
        org.org_id,
        org.device_id,
        last_seen_at=datetime.now(UTC) - timedelta(minutes=20),
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert await run_offline_sweep(session, org.org_id, now=datetime.now(UTC)) == {
            "opened": 1,
            "cleared": 0,
        }
        await session.commit()


async def test_the_heartbeat_timestamp_comes_from_the_server_not_the_agent(
    admin_engine, app_engine, tmp_path
) -> None:
    """`HeartbeatMsg.ts` is agent-supplied and therefore forgeable. A device
    that sent `ts` a year in the future would otherwise make itself permanently
    un-reportable as offline. The stored value must be server time."""
    org = await seed_org(admin_engine, "hb4")
    await _seed_agent(
        admin_engine,
        org.org_id,
        org.device_id,
        last_seen_at=datetime.now(UTC) - timedelta(minutes=20),
    )

    await _deliver_heartbeat(
        app_engine,
        tmp_path,
        device_id=org.device_id,
        org_id=org.org_id,
        ts="2099-01-01T00:00:00Z",
    )

    after = await _last_seen(admin_engine, org.device_id)
    assert after.year != 2099, "the server stored the agent's claimed timestamp"
    assert (datetime.now(UTC) - after).total_seconds() < 60


async def test_heartbeat_writes_are_rate_limited_per_device(
    admin_engine, app_engine, tmp_path
) -> None:
    """A hostile or merely chatty agent must not be able to turn the heartbeat
    into a write amplifier. The second heartbeat inside the window is dropped,
    so the stored timestamp does not move -- and the limiter is PER DEVICE, so
    one loud agent cannot suppress a quiet one's write."""
    loud = await seed_org(admin_engine, "hb5a")
    quiet = await seed_org(admin_engine, "hb5b")
    stale = datetime.now(UTC) - timedelta(minutes=20)
    await _seed_agent(admin_engine, loud.org_id, loud.device_id, last_seen_at=stale)
    await _seed_agent(admin_engine, quiet.org_id, quiet.device_id, last_seen_at=stale)

    await _deliver_heartbeat(app_engine, tmp_path, device_id=loud.device_id, org_id=loud.org_id)
    first = await _last_seen(admin_engine, loud.device_id)

    # Rewind the row behind the limiter's back: if the second heartbeat wrote,
    # the timestamp would move forward again.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = (
            await session.execute(select(Agent).where(Agent.device_id == loud.device_id))
        ).scalar_one()
        row.last_seen_at = stale
        session.add(row)
        await session.commit()

    for _ in range(50):
        await _deliver_heartbeat(
            app_engine, tmp_path, device_id=loud.device_id, org_id=loud.org_id
        )
    assert await _last_seen(admin_engine, loud.device_id) == stale, (
        "50 heartbeats inside the rate-limit window all wrote"
    )
    assert first > stale  # the first one did write

    await _deliver_heartbeat(app_engine, tmp_path, device_id=quiet.device_id, org_id=quiet.org_id)
    assert await _last_seen(admin_engine, quiet.device_id) > stale, (
        "the limiter is global, not per device"
    )


async def test_a_heartbeat_writes_no_audit_record(admin_engine, app_engine, tmp_path) -> None:
    """Per-minute per-device volume. `_handle_health_report`'s docstring already
    states the rule: no per-report audit record, because volume would swamp a
    hash-chained log whose value is that a human can read it."""
    org = await seed_org(admin_engine, "hb6")
    await _seed_agent(
        admin_engine, org.org_id, org.device_id, last_seen_at=datetime.now(UTC) - timedelta(hours=1)
    )

    await _deliver_heartbeat(app_engine, tmp_path, device_id=org.device_id, org_id=org.org_id)

    async with AsyncSession(admin_engine) as session:
        actions = list(
            (
                await session.execute(
                    select(AuditEvent.action).where(AuditEvent.org_id == org.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert actions == [], f"the heartbeat wrote audit records: {actions}"


async def test_a_heartbeat_cannot_touch_an_agent_in_another_org(
    admin_engine, app_engine, tmp_path
) -> None:
    """Invariant #5. The device binding comes from the mTLS certificate, but the
    UPDATE still runs inside the connection's tenant session: a device_id from
    another org matches zero rows. The observation is the OTHER org's stored
    timestamp, unchanged."""
    a = await seed_org(admin_engine, "hb7a")
    b = await seed_org(admin_engine, "hb7b")
    stale = datetime.now(UTC) - timedelta(hours=1)
    await _seed_agent(admin_engine, b.org_id, b.device_id, last_seen_at=stale)

    # org A's connection, claiming org B's device.
    await _deliver_heartbeat(app_engine, tmp_path, device_id=b.device_id, org_id=a.org_id)

    assert await _last_seen(admin_engine, b.device_id) == stale, (
        "a heartbeat on one org's connection advanced another org's agent"
    )
