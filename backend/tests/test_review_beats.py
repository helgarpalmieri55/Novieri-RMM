"""The install link's two beats (spec 2026-09-28 §1.1 automatic switch-off,
§1.5 rejection completion; plan D1, D2)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.ca import LocalCA
from app.core.config import Settings, override_settings
from app.models import Agent, AuditEvent, Device, DomainEvent, InstallLink, Job, JobStatus
from app.services import device_review, install_link
from tests.ipam_fixtures import seed_agent, seed_device, seed_org

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _settings(pg_cluster: Any, tmp_path: Path) -> Settings:
    return Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
    )


async def _count(admin_engine: AsyncEngine, org_id: UUID, action: str) -> int:
    async with AsyncSession(admin_engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.org_id == org_id, AuditEvent.action == action)
                )
            ).scalar_one()
        )


async def test_the_auto_off_beat_records_a_passed_switch_off_once(
    admin_engine: AsyncEngine, pg_cluster: Any, tmp_path: Path
) -> None:
    from app import worker

    due = (await seed_org(admin_engine, "ao")).org_id
    later = (await seed_org(admin_engine, "ao2")).org_id
    async with AsyncSession(admin_engine) as session:
        session.add_all(
            [
                InstallLink(
                    org_id=due,
                    enabled=True,
                    code_hash=uuid4().hex * 2,
                    networks=["10.0.0.0/8"],
                    disable_at=NOW - timedelta(minutes=1),
                    enabled_at=NOW - timedelta(hours=3),
                    client_id=due,
                    site_id=due,
                ),
                InstallLink(
                    org_id=later,
                    enabled=True,
                    code_hash=uuid4().hex * 2,
                    networks=["10.0.0.0/8"],
                    disable_at=NOW + timedelta(days=1),
                    enabled_at=NOW - timedelta(hours=3),
                    client_id=later,
                    site_id=later,
                ),
            ]
        )
        await session.commit()

    with override_settings(_settings(pg_cluster, tmp_path)):
        await worker.install_link_auto_off_all_orgs(NOW)
        await worker.install_link_auto_off_all_orgs(NOW + timedelta(minutes=1))

    async with AsyncSession(admin_engine) as session:
        rows = {
            r.org_id: r
            for r in (
                await session.execute(
                    select(InstallLink).where(InstallLink.org_id.in_([due, later]))
                )
            ).scalars()
        }
        audit = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == due, AuditEvent.action == "enrollment.link_disabled"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows[due].enabled is False and rows[due].enabled_at is None
    assert rows[later].enabled is True
    assert len(audit) == 1 and audit[0].meta["reason"] == "scheduled"
    assert await _count(admin_engine, later, "enrollment.link_disabled") == 0


async def _rejected(
    admin_engine: AsyncEngine,
    *,
    hours_ago: float,
    confirmed: bool,
    job_status: JobStatus | None = None,
) -> tuple[UUID, UUID, str]:
    """A rejected device with an agent; `confirmed` seeds a `succeeded`
    uninstall, `job_status` an uninstall that ended some other way."""
    if confirmed:
        job_status = JobStatus.succeeded
    org = await seed_org(admin_engine, "rj")
    device_id = await seed_device(
        admin_engine,
        org.org_id,
        review_state="rejected",
        rejected_at=NOW - timedelta(hours=hours_ago),
    )
    await seed_agent(admin_engine, org.org_id, device_id, capabilities={"agent.uninstall": True})
    async with AsyncSession(admin_engine) as session:
        serial = (
            await session.execute(select(Agent.cert_serial).where(Agent.device_id == device_id))
        ).scalar_one()
        if job_status is not None:
            session.add(
                Job(
                    org_id=org.org_id,
                    type="agent.uninstall",
                    target_type="device",
                    target_id=device_id,
                    params={},
                    nonce=uuid4().hex,
                    issued_at=NOW - timedelta(hours=1),
                    expires_at=NOW + timedelta(hours=23),
                    status=job_status,
                    finished_at=NOW - timedelta(minutes=30),
                    result={"scheduled": True} if job_status == JobStatus.succeeded else None,
                )
            )
        await session.commit()
    return org.org_id, device_id, serial


async def test_a_rejection_completes_when_the_agent_confirms_or_after_24_hours(
    admin_engine: AsyncEngine, pg_cluster: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import worker

    confirmed = await _rejected(admin_engine, hours_ago=1, confirmed=True)
    timed_out = await _rejected(admin_engine, hours_ago=25, confirmed=False)
    waiting = await _rejected(admin_engine, hours_ago=1, confirmed=False)

    revoked_connections: list[UUID] = []

    async def fake_publish_revoke(_redis: Any, device_id: UUID) -> None:
        revoked_connections.append(device_id)

    monkeypatch.setattr(worker, "publish_revoke", fake_publish_revoke)
    with override_settings(_settings(pg_cluster, tmp_path)):
        await worker.review_rejections_all_orgs(NOW)
        await worker.review_rejections_all_orgs(NOW + timedelta(minutes=1))  # idempotent

    ca = LocalCA(tmp_path / "ca")
    async with AsyncSession(admin_engine) as session:
        for org_id, device_id, serial in (confirmed, timed_out):
            device = await session.get(Device, device_id)
            agent = (
                await session.execute(select(Agent).where(Agent.device_id == device_id))
            ).scalar_one()
            assert device is not None and device.status == "decommissioned"
            assert agent.cert_revoked is True
            assert ca.is_revoked(serial)
            assert device_id in revoked_connections
            assert await _count(admin_engine, org_id, "device.rejection_completed") == 1
        still = await session.get(Device, waiting[1])
        still_agent = (
            await session.execute(select(Agent).where(Agent.device_id == waiting[1]))
        ).scalar_one()
    assert (
        still is not None and still.status != "decommissioned" and still_agent.cert_revoked is False
    )
    assert waiting[1] not in revoked_connections

    async with AsyncSession(admin_engine) as session:
        reasons = {
            e.target_id: e.meta["reason"]
            for e in (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "device.rejection_completed",
                        AuditEvent.org_id.in_([confirmed[0], timed_out[0]]),
                    )
                )
            ).scalars()
        }
    assert reasons == {str(confirmed[1]): "agent_confirmed", str(timed_out[1]): "timeout"}

    # The device row carries the outcome too, not only the audit trail.
    async with AsyncSession(admin_engine) as session:
        outcomes = {
            device_id: (await session.get(Device, device_id)).rejection_outcome  # type: ignore[union-attr]
            for _org, device_id, _serial in (confirmed, timed_out, waiting)
        }
    assert outcomes == {
        confirmed[1]: "agent_confirmed",
        timed_out[1]: "timeout",
        waiting[1]: None,
    }

    # Only the rejection that may have left the agent behind tells anyone, and
    # it tells them once although the beat ran twice.
    assert await _removal_events(admin_engine, confirmed[0]) == []
    assert await _removal_events(admin_engine, waiting[0]) == []
    [event] = await _removal_events(admin_engine, timed_out[0])
    assert event.subject_type == "device" and event.subject_id == timed_out[1]
    assert event.actor_user_id is None
    assert event.payload["device_id"] == str(timed_out[1])
    assert event.payload["rejection_outcome"] == "timeout"


async def _removal_events(admin_engine: AsyncEngine, org_id: UUID) -> list[DomainEvent]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(DomainEvent).where(
                        DomainEvent.org_id == org_id,
                        DomainEvent.event_type == "device.removal_incomplete",
                    )
                )
            ).scalars()
        )


@pytest.mark.parametrize("status", [JobStatus.failed, JobStatus.refused, JobStatus.expired])
async def test_an_uninstall_that_ended_without_success_completes_as_not_confirmed(
    admin_engine: AsyncEngine,
    pg_cluster: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: JobStatus,
) -> None:
    """The agent answered and did NOT remove itself (or never collected the
    job before it expired). Waiting out the rest of the 24 hours would change
    nothing: the certificate is revoked now, and the row says the agent did
    not confirm, so the console can tell the technician to remove it by hand."""
    from app import worker

    org_id, device_id, serial = await _rejected(
        admin_engine, hours_ago=1, confirmed=False, job_status=status
    )

    async def fake_publish_revoke(_redis: Any, _device_id: UUID) -> None:
        return None

    monkeypatch.setattr(worker, "publish_revoke", fake_publish_revoke)
    with override_settings(_settings(pg_cluster, tmp_path)):
        await worker.review_rejections_all_orgs(NOW)

    async with AsyncSession(admin_engine) as session:
        device = await session.get(Device, device_id)
        agent = (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()
        [event] = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "device.rejection_completed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert device is not None and device.status == "decommissioned"
    assert device.rejection_outcome == "agent_did_not_confirm"
    assert agent.cert_revoked is True and LocalCA(tmp_path / "ca").is_revoked(serial)
    assert event.meta["reason"] == "agent_did_not_confirm"
    assert event.meta["job_status"] == status.value
    [notice] = await _removal_events(admin_engine, org_id)
    assert notice.payload["rejection_outcome"] == "agent_did_not_confirm"
    assert notice.payload["device_id"] == str(device_id)


def test_both_beats_are_scheduled_every_minute(pg_cluster: Any, tmp_path: Path) -> None:
    from app import worker

    with override_settings(_settings(pg_cluster, tmp_path)):
        schedule = worker.create_celery().conf.beat_schedule
    assert schedule["install-link-auto-off"]["task"] == "app.worker.install_link_auto_off"
    assert schedule["install-link-auto-off"]["schedule"] == install_link.AUTO_OFF_INTERVAL_S == 60.0
    assert schedule["device-review-completion"]["task"] == "app.worker.device_review_completion"
    assert (
        schedule["device-review-completion"]["schedule"]
        == device_review.COMPLETION_INTERVAL_S
        == 60.0
    )
