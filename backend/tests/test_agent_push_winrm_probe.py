"""The WinRM probe's result (design §2.4, W3): readiness, not a host key.

A `winrm` probe is credential-free and reports whether 5986/5985 answered and
whether a KDC was reachable. There is no host key and nothing for a human to
confirm -- Kerberos authenticates the target -- so the result lands in
`push_host_key.readiness` and the §7.3 confirmation path refuses to treat the
synthetic `winrm` marker as a key.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Job
from app.modules.agent_push import host_key as host_key_service
from app.modules.agent_push.api import _host_key_out
from app.modules.agent_push.dispatch import record_probe_result
from app.modules.agent_push.errors import PushValidationError
from app.modules.agent_push.jobtypes import PROBE_JOB_TYPE
from tests.agent_push_fixtures import seed_confirmed_host_key, seed_org
from tests.ipam_fixtures import seed_device, utcnow

TARGET = "10.72.0.17"


async def _probe_job(admin_engine: AsyncEngine, org, *, transport: str) -> Job:
    """A probe job row as the gateway would hand it to the result handler."""
    device = await seed_device(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        job = Job(
            id=uuid4(),
            org_id=org.org_id,
            target_id=device,
            type=PROBE_JOB_TYPE,
            params={"target_address": TARGET, "transport": transport},
            signature="sig",
            nonce=uuid4().hex,
            issued_at=utcnow(),
            expires_at=utcnow() + timedelta(minutes=5),
            requested_by=org.user_id,
        )
        session.add(job)
        await session.commit()
        return job


async def _record(app_engine, org, job, result) -> None:
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        j = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
        await record_probe_result(session, j, result)


async def test_a_winrm_probe_records_readiness_and_no_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "wp")
    job = await _probe_job(admin_engine, org, transport="winrm")
    await _record(
        app_engine,
        org,
        job,
        {
            "target_address": TARGET,
            "transport": "winrm",
            "host_key_fingerprint": "",
            "host_key_type": "",
            "winrm_https": True,
            "winrm_http": False,
            "kdc_reachable": True,
        },
    )
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = await host_key_service.get_host_key(session, org.org_id, TARGET)
        assert row is not None
        assert row.transport == "winrm"
        assert row.readiness == {"winrm_https": True, "winrm_http": False, "kdc_reachable": True}
        assert row.observed_fingerprint == "winrm"
        assert row.confirmed_fingerprint is None
        out = _host_key_out(row)
    assert (out.winrm_https, out.winrm_http, out.kdc_reachable) == (True, False, True)
    assert out.installable is False

    async with AsyncSession(admin_engine) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "push.winrm_readiness_observed",
                )
            )
        ).scalar_one()
    assert event.target_id == TARGET
    assert event.meta["kdc_reachable"] is True


async def test_a_winrm_readiness_invalidates_a_previously_confirmed_ssh_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The same address now answering as a Windows machine is a different host
    as far as the SSH bound is concerned: the confirmed key no longer equals the
    observation, so an SSH install there is refused until re-probed and
    re-confirmed (fail closed)."""
    org = await seed_org(admin_engine, "wp-flip")
    await seed_confirmed_host_key(admin_engine, org, address=TARGET, fingerprint="SHA256:k")
    job = await _probe_job(admin_engine, org, transport="winrm")
    await _record(
        app_engine,
        org,
        job,
        {"target_address": TARGET, "transport": "winrm", "winrm_http": True},
    )
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        # No device: the probe job names none, so its readiness and the
        # confirmed key it invalidates are the global row's.
        fingerprint, reason = await host_key_service.confirmed_key_for(
            session, org.org_id, TARGET, device_id=None
        )
    assert fingerprint is None and reason == "host_key_changed"


async def test_the_winrm_marker_cannot_be_confirmed_as_a_host_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "wp-confirm")
    job = await _probe_job(admin_engine, org, transport="winrm")
    await _record(
        app_engine,
        org,
        job,
        {"target_address": TARGET, "transport": "winrm", "winrm_https": True},
    )
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(PushValidationError, match="Kerberos"):
            await host_key_service.confirm_host_key(
                session, org.org_id, actor_id=org.user_id, address=TARGET, fingerprint="winrm"
            )


async def test_an_ssh_probe_after_a_winrm_one_clears_the_stale_readiness(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "wp-back")
    job = await _probe_job(admin_engine, org, transport="winrm")
    await _record(
        app_engine,
        org,
        job,
        {"target_address": TARGET, "transport": "winrm", "winrm_https": True},
    )
    ssh_job = await _probe_job(admin_engine, org, transport="ssh")
    await _record(
        app_engine,
        org,
        ssh_job,
        {
            "target_address": TARGET,
            "transport": "ssh",
            "host_key_fingerprint": "SHA256:new",
            "host_key_type": "ssh-ed25519",
        },
    )
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = await host_key_service.get_host_key(session, org.org_id, TARGET)
        assert row is not None
        assert row.transport == "ssh"
        assert row.observed_fingerprint == "SHA256:new"
        assert row.readiness is None


async def test_an_ssh_probe_result_is_unchanged(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "wp-ssh")
    job = await _probe_job(admin_engine, org, transport="ssh")
    await _record(
        app_engine,
        org,
        job,
        {
            "target_address": TARGET,
            "transport": "ssh",
            "host_key_fingerprint": "SHA256:abc",
            "host_key_type": "ssh-ed25519",
        },
    )
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = await host_key_service.get_host_key(session, org.org_id, TARGET)
        assert row is not None
        assert (row.transport, row.observed_fingerprint, row.readiness) == (
            "ssh",
            "SHA256:abc",
            None,
        )


# --------------------------------------------------------------------------- #
# Review Minor 3: the SIGNED params decide, never the site server's echo
# --------------------------------------------------------------------------- #


async def test_a_winrm_probe_whose_result_claims_ssh_records_no_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A site server that echoes `transport: ssh` and a fingerprint for a job
    the server signed as `winrm` does not get a host key pinned for a Windows
    target: the job's own params say what was probed."""
    org = await seed_org(admin_engine, "wp-echo-t")
    job = await _probe_job(admin_engine, org, transport="winrm")
    await _record(
        app_engine, org, job,
        {"target_address": TARGET, "transport": "ssh", "host_key_fingerprint": "SHA256:evil",
         "winrm_https": True},
    )
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = await host_key_service.get_host_key(session, org.org_id, TARGET)
    assert row is not None
    assert (row.transport, row.observed_fingerprint) == ("winrm", "winrm")
    assert row.readiness == {"winrm_https": True, "winrm_http": False, "kdc_reachable": False}


@pytest.mark.parametrize("transport", ["ssh", "winrm"])
async def test_a_probe_result_is_recorded_at_the_signed_address_not_the_echoed_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, transport: str
) -> None:
    org = await seed_org(admin_engine, f"wp-echo-a-{transport}")
    job = await _probe_job(admin_engine, org, transport=transport)
    await _record(
        app_engine, org, job,
        {"target_address": "10.72.0.99", "transport": transport,
         "host_key_fingerprint": "SHA256:abc", "winrm_http": True},
    )
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert await host_key_service.get_host_key(session, org.org_id, "10.72.0.99") is None
        assert await host_key_service.get_host_key(session, org.org_id, TARGET) is not None
