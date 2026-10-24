"""A check made inside a request about ONE record asks about that record's client.

MSP clients spec §2.1: a grant is per client, and holding a code on X says
nothing about Y. Three places check a second permission after loading a record
-- cancelling and retrying a job need the job type's own dispatch code, and an
unattended screen session needs `remote.unattended` -- and they asked
`has_permission` with no client, which counts every-client grants only. A
technician granted the dispatch code on X alone was refused on X's own jobs
(fail closed, but spec §2.1 broken). They now ask about the record's client:
the job's, the device's.

Each test pairs the X case with a Y case, so "the check counts the per-client
grant" is never confused with "the check counts any grant anywhere":

* `jobs.read` and `jobs.dispatch` on X only: X's job is cancelled and retried;
  Y's is a 404 (RLS: the route's `jobs.read` scope is [X]).
* `jobs.read` on every client, `jobs.dispatch` on X only: Y's job is VISIBLE
  and still refused (403) -- the dispatch code is asked about Y, where it is
  not held.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    Agent,
    Client,
    Device,
    Job,
    JobStatus,
    Permission,
    RemotePolicy,
    Role,
    RolePermission,
    Site,
    User,
    UserRole,
)

JOBS = "/api/v1/jobs"
SCREEN = {"remote.screen.open": True}


async def _client(api_env: Any, org_id: UUID, name: str) -> tuple[UUID, UUID]:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        client = Client(org_id=org_id, name=name, kind="customer")
        s.add(client)
        await s.flush()
        site = Site(org_id=org_id, client_id=client.id, name="Principal")
        s.add(site)
        await s.commit()
        return client.id, site.id


async def _device(
    api_env: Any, org_id: UUID, client: tuple[UUID, UUID], *, screen: bool = False
) -> UUID:
    client_id, site_id = client
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org_id,
            hostname=f"h-{uuid4().hex[:6]}",
            os_family="windows",
            client_id=client_id,
            site_id=site_id,
        )
        s.add(device)
        await s.flush()
        if screen:
            s.add(
                Agent(
                    org_id=org_id,
                    device_id=device.id,
                    cert_fingerprint=uuid4().hex + uuid4().hex,
                    cert_serial=uuid4().hex,
                    cert_not_after=datetime.now(UTC) + timedelta(days=30),
                    enrolled_at=datetime.now(UTC),
                    capabilities=SCREEN,
                )
            )
        await s.commit()
        return device.id


async def _job(api_env: Any, org_id: UUID, device_id: UUID, status: JobStatus) -> UUID:
    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        job = Job(
            org_id=org_id,
            type="inventory.collect",
            target_type="device",
            target_id=device_id,
            params={},
            nonce=uuid4().hex,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            status=status,
            signature="sig",
            attempts=1,
            finished_at=None if status is JobStatus.queued else now,
            result=None if status is JobStatus.queued else {"error": "the disk was full"},
        )
        s.add(job)
        await s.commit()
        return job.id


async def _person(
    api_env: Any, org_id: UUID, grants: list[tuple[tuple[str, ...], UUID | None]]
) -> dict[str, str]:
    """Headers for a person holding each `(codes, client)` grant -- a role
    made of exactly `codes`, on that client (None = every client)."""
    sub = f"sub-{uuid4().hex[:10]}"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        s.add(user)
        await s.flush()
        for codes, client_id in grants:
            role = Role(org_id=org_id, name=f"role-{uuid4().hex[:8]}")
            s.add(role)
            await s.flush()
            for code in codes:
                permission_id = (
                    await s.execute(select(Permission.id).where(Permission.code == code))
                ).scalar_one()
                s.add(RolePermission(role_id=role.id, permission_id=permission_id))
            s.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id, client_id=client_id))
        await s.commit()
    return await api_env.headers(org_id, sub)


async def _world(api_env: Any, tag: str) -> tuple[UUID, tuple[UUID, UUID], tuple[UUID, UUID]]:
    org_id = await api_env.create_org(tag)
    return org_id, await _client(api_env, org_id, "X"), await _client(api_env, org_id, "Y")


async def test_cancel_asks_about_the_jobs_own_client(api_env: Any) -> None:
    org_id, x, y = await _world(api_env, "rec-cancel")
    x_job = await _job(api_env, org_id, await _device(api_env, org_id, x), JobStatus.queued)
    y_job = await _job(api_env, org_id, await _device(api_env, org_id, y), JobStatus.queued)

    on_x = await _person(api_env, org_id, [(("jobs.read", "jobs.dispatch"), x[0])])
    ok = await api_env.client.post(f"{JOBS}/{x_job}/cancel", headers=on_x)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "cancelled"
    hidden = await api_env.client.post(f"{JOBS}/{y_job}/cancel", headers=on_x)
    assert hidden.status_code == 404, hidden.text

    # Sees Y's job (jobs.read everywhere), may dispatch only on X.
    reads_all = await _person(
        api_env, org_id, [(("jobs.read",), None), (("jobs.dispatch",), x[0])]
    )
    refused = await api_env.client.post(f"{JOBS}/{y_job}/cancel", headers=reads_all)
    assert refused.status_code == 403, refused.text


async def test_retry_asks_about_the_jobs_own_client(api_env: Any) -> None:
    org_id, x, y = await _world(api_env, "rec-retry")
    x_job = await _job(api_env, org_id, await _device(api_env, org_id, x), JobStatus.failed)
    y_job = await _job(api_env, org_id, await _device(api_env, org_id, y), JobStatus.failed)

    on_x = await _person(api_env, org_id, [(("jobs.read", "jobs.dispatch"), x[0])])
    ok = await api_env.client.post(f"{JOBS}/{x_job}/retry", headers=on_x)
    assert ok.status_code == 201, ok.text
    hidden = await api_env.client.post(f"{JOBS}/{y_job}/retry", headers=on_x)
    assert hidden.status_code == 404, hidden.text

    reads_all = await _person(
        api_env, org_id, [(("jobs.read",), None), (("jobs.dispatch",), x[0])]
    )
    refused = await api_env.client.post(f"{JOBS}/{y_job}/retry", headers=reads_all)
    assert refused.status_code == 403, refused.text
    again = await api_env.client.post(f"{JOBS}/{x_job}/retry", headers=reads_all)
    assert again.status_code == 201, again.text


async def test_unattended_screen_asks_about_the_devices_client(api_env: Any) -> None:
    """`remote.unattended` on X only: X's device opens unattended, Y's device
    (remote.control everywhere) is downgraded to attended -- the existing
    silent downgrade, now decided by Y's grants rather than by every-client
    grants alone."""
    org_id, x, y = await _world(api_env, "rec-unattended")
    x_device = await _device(api_env, org_id, x, screen=True)
    y_device = await _device(api_env, org_id, y, screen=True)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        s.add(RemotePolicy(org_id=org_id, allow_unattended=True))

    headers = await _person(
        api_env,
        org_id,
        [(("devices.read", "remote.control"), None), (("remote.unattended",), x[0])],
    )

    async def _mode(device_id: UUID) -> str:
        body = {"device_id": str(device_id), "reason": "INC-1", "mode": "unattended"}
        response = await api_env.client.post(
            "/api/v1/remote/sessions", json=body, headers=headers
        )
        assert response.status_code == 201, response.text
        return str(response.json()["consent_mode"])

    assert await _mode(x_device) == "unattended"
    assert await _mode(y_device) == "attended"
