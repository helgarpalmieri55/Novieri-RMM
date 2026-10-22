"""A boxed laptop is not offline (docs/modules/asset-inventory/spec.md §5).

WHAT IS ALREADY SAFE, so this file says what it actually covers.
`run_offline_sweep` joins `agents`, so a record with NO agent can never reach
it -- an asset an operator typed in cannot raise an offline alert today, and
that half of falsifier 7 holds structurally.

THE REACHABLE CASE IS NARROWER AND REAL: a machine that WAS deployed, still has
its agent installed, and has gone back to the warehouse or been retired at the
desk. Its lifecycle says so and it will never check in again until somebody
unboxes it -- and today it alerts every sweep, for ever.

WHY THAT MATTERS MORE THAN ONE ALERT. An alert an operator learns to ignore is
worse than no alert, because the next one that matters is ignored the same way.
Spec §5 names this as the first thing this feature would otherwise do wrong.

Harness reused from `tests/test_gateway_heartbeat.py`: an org with an armed
`agent.offline` check, and an agent whose `last_seen_at` is far past the
cutoff.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Agent, Device
from app.modules.health.sweep import run_offline_sweep
from tests.health_fixtures import (
    HealthOrg,
    seed_assignment,
    seed_check,
    seed_org,
    seed_profile,
    seed_settings,
)

pytestmark = pytest.mark.usefixtures("admin_engine")


async def _arm_offline_check(admin_engine: AsyncEngine, org: HealthOrg) -> None:
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
    await seed_settings(admin_engine, org.org_id, offline_after_s=900)


async def _stale_agent(admin_engine: AsyncEngine, org_id: UUID, device_id: UUID) -> None:
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Agent(
                org_id=org_id,
                device_id=device_id,
                cert_fingerprint=uuid4().hex,
                cert_serial=uuid4().hex[:16],
                cert_not_after=now + timedelta(days=30),
                enrolled_at=now - timedelta(days=30),
                status="online",
                last_seen_at=now - timedelta(days=7),
            )
        )
        await session.commit()


async def _set_lifecycle(admin_engine: AsyncEngine, device_id: UUID, lifecycle: str) -> None:
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            update(Device).where(Device.id == device_id).values(lifecycle=lifecycle)
        )
        await session.commit()


async def test_an_in_service_machine_does_raise_an_offline_alert(
    admin_engine: AsyncEngine,
) -> None:
    """NON-VACUITY, and it comes first deliberately.

    `run_offline_sweep` returns `opened: 0` for an org with no health profile,
    no assignment or no `agent.offline` check -- so all three lifecycle tests
    below would pass over a sweep that does nothing at all. This is the test
    that proves the harness arms the alert this file is about suppressing.
    """
    org = await seed_org(admin_engine, "lc-in")
    await _arm_offline_check(admin_engine, org)
    await _stale_agent(admin_engine, org.org_id, org.device_id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        result = await run_offline_sweep(session, org.org_id)
    assert result["opened"] == 1, result


@pytest.mark.parametrize("lifecycle", ["stock", "unmanageable", "retired"])
async def test_a_machine_that_is_not_in_service_raises_none(
    admin_engine: AsyncEngine, lifecycle: str
) -> None:
    """`stock` is the case that matters most: a laptop back in the warehouse
    with its agent still installed. It is not down, and saying so every sweep
    for as long as it sits there is how an operator learns to ignore this
    alert. `unmanageable` was never expected to check in; `retired` is not a
    to-do item."""
    org = await seed_org(admin_engine, f"lc-{lifecycle}")
    await _arm_offline_check(admin_engine, org)
    await _stale_agent(admin_engine, org.org_id, org.device_id)
    await _set_lifecycle(admin_engine, org.device_id, lifecycle)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        result = await run_offline_sweep(session, org.org_id)
    assert result["opened"] == 0, result
