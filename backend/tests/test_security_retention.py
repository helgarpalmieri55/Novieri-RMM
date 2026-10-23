from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Device, Organization, Policy, SecurityEvent
from app.modules.security_logs.retention import apply_retention


async def _seed(
    admin_engine: AsyncEngine, *, old: int, recent: int, retention_days: int | None = None
) -> tuple[UUID, UUID]:
    slug = f"ret-{uuid4().hex[:6]}"
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(org_id=org.id, hostname="r1", os_family="linux")
        session.add(device)
        await session.flush()
        if retention_days is not None:
            session.add(
                Policy(
                    org_id=org.id,
                    module="security_logs",
                    config={"retention_days": retention_days, "ship_interval_s": 60},
                )
            )
        for i in range(old):
            session.add(
                SecurityEvent(
                    org_id=org.id,
                    device_id=device.id,
                    event_uid=f"old{i}",
                    ts=now - timedelta(days=400),
                    event_type="logon_success",
                )
            )
        for i in range(recent):
            session.add(
                SecurityEvent(
                    org_id=org.id,
                    device_id=device.id,
                    event_uid=f"new{i}",
                    ts=now - timedelta(days=5),
                    event_type="logon_failure",
                )
            )
        await session.commit()
        return org.id, device.id


async def test_retention_deletes_old_keeps_recent_and_audits(
    admin_engine, app_engine
) -> None:
    org_id, _ = await _seed(admin_engine, old=5, recent=3)
    deleted = await apply_retention(org_id, admin_engine, app_engine)
    assert deleted == 5

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        remaining = (
            await session.execute(select(func.count()).select_from(SecurityEvent))
        ).scalar_one()
        assert remaining == 3
        audit = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "security_logs.retention_applied")
            )
        ).scalar_one()
        assert audit.meta["deleted"] == 5
        assert "cutoff" in audit.meta


async def test_custom_retention_policy_honored(admin_engine, app_engine) -> None:
    # 30-day policy: the 400-day-old rows go, and so would anything >30d
    org_id, _ = await _seed(admin_engine, old=2, recent=2, retention_days=30)
    deleted = await apply_retention(org_id, admin_engine, app_engine)
    assert deleted == 2
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        remaining = (
            await session.execute(select(func.count()).select_from(SecurityEvent))
        ).scalar_one()
    assert remaining == 2


async def test_noop_sweep_writes_no_audit_event(admin_engine, app_engine) -> None:
    org_id, _ = await _seed(admin_engine, old=0, recent=2)
    assert await apply_retention(org_id, admin_engine, app_engine) == 0
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "security_logs.retention_applied")
            )
        ).scalar_one()
    assert count == 0


async def test_retention_does_not_delete_a_neighbour_orgs_events(
    admin_engine, app_engine
) -> None:
    """THE DELETE RUNS ON THE BYPASSRLS ADMIN ENGINE, SO ITS TENANT PREDICATE IS
    THE ONLY THING SCOPING IT.

    `apply_retention` deletes through `AsyncSession(admin_engine)` -- it has to,
    because the app role holds no DELETE on `security_events` (0006's
    append-only grants). That means Postgres will not reject a cross-tenant row
    here the way it does everywhere else: `SecurityEvent.org_id == org_id` in
    the batch select is the entire isolation boundary, and removing it deletes
    every org's expired evidence rather than one org's.

    The three tests above cannot see that. Each seeds a single org, so the
    returned count and the surviving rows are identical whether or not the
    predicate is there -- verified by deleting it, at which point all three
    still passed. This one seeds a NEIGHBOUR whose events are old enough to be
    swept by the victim's window, and asserts both directions: the neighbour's
    rows survive, and the target's own still go, so it cannot pass by deleting
    nothing at all.
    """
    org_id, _ = await _seed(admin_engine, old=4, recent=1)
    neighbour_id, _ = await _seed(admin_engine, old=6, recent=2)

    deleted = await apply_retention(org_id, admin_engine, app_engine)
    assert deleted == 4

    async with org_session(neighbour_id, engine=app_engine, clients=ClientScope.all()) as session:
        surviving = (
            await session.execute(select(func.count()).select_from(SecurityEvent))
        ).scalar_one()
    assert surviving == 8, "retention swept a neighbour org's security events"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        remaining = (
            await session.execute(select(func.count()).select_from(SecurityEvent))
        ).scalar_one()
    assert remaining == 1
