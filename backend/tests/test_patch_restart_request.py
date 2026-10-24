"""An operator asks for a restart from the device page.

`POST /api/v1/patch/devices/{id}/restart` books the restart as a
`patch_reboots` row; it never dispatches `patch.reboot` itself. The sweep sends
the signed job inside the device's maintenance window, with the policy's
warning for a signed-in user, exactly as it does for a restart patching left
owing.
"""

import datetime as _dt
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    Agent,
    AuditEvent,
    Device,
    DeviceGroup,
    Job,
    Organization,
    PatchPolicy,
    PatchReboot,
    User,
)
from app.modules.patch.handler import register_job_types
from app.modules.patch.reboot import note_scan, run_reboot_sweep

register_job_types()

SAT = _dt.datetime(2026, 9, 26, 2, 37, tzinfo=_dt.UTC)
WINDOW = "0 2-5 * * 6"


class _FakeJobEngine:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create(self, session: AsyncSession, **kw: Any) -> Job:
        job = Job(
            org_id=kw["org_id"],
            type=kw["type"],
            target_type="device",
            target_id=kw["target_device_id"],
            params=kw["params"],
            nonce=uuid4().hex,
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1),
        )
        session.add(job)
        await session.flush()
        self.created.append(kw)
        return job

    async def signed_envelope(self, job: Job) -> dict[str, Any]:
        return {"v": 1, "kind": "job", "job": {"id": str(job.id)}, "sig": "x"}


async def _device(
    admin_engine: AsyncEngine,
    org_id: UUID,
    *,
    agent: bool = True,
    capabilities: dict[str, Any] | None = None,
    revoked: bool = False,
    reboot: str = "if_required",
    window: str | None = WINDOW,
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        group = DeviceGroup(org_id=org_id, name=f"g-{uuid4().hex[:6]}", client_id=org_id)
        session.add(group)
        await session.flush()
        session.add(
            PatchPolicy(
                org_id=org_id,
                device_group_id=group.id,
                ring_name="broad",
                maintenance_window=window,
                reboot=reboot,
                reboot_deferral_hours=8,
            )
        )
        device = Device(
            org_id=org_id,
            hostname=f"wks-{uuid4().hex[:6]}",
            os_family="windows",
            device_group_id=group.id,
            client_id=org_id,
            site_id=org_id,
        )
        session.add(device)
        await session.flush()
        if agent:
            session.add(
                Agent(
                    org_id=org_id,
                    device_id=device.id,
                    cert_fingerprint=uuid4().hex,
                    cert_serial=uuid4().hex[:16],
                    cert_not_after=SAT + _dt.timedelta(days=365),
                    cert_revoked=revoked,
                    enrolled_at=SAT - _dt.timedelta(days=30),
                    capabilities=(
                        {"patch.reboot": True, "patch.scan": True}
                        if capabilities is None
                        else capabilities
                    ),
                    status="online",
                    last_seen_at=SAT,
                )
            )
        await session.commit()
        return device.id


async def _rows(admin_engine: AsyncEngine, org_id: UUID) -> list[PatchReboot]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(select(PatchReboot).where(PatchReboot.org_id == org_id))
            ).scalars()
        )


async def _requests(admin_engine: AsyncEngine, org_id: UUID) -> list[AuditEvent]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "patch.restart_requested",
                    )
                )
            ).scalars()
        )


def _url(device_id: UUID) -> str:
    return f"/api/v1/patch/devices/{device_id}/restart"


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #


async def test_a_technician_books_a_restart_for_the_window_and_it_is_audited(api_env) -> None:
    org_id = await api_env.create_org("rst")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env.admin_engine, org_id)

    resp = await api_env.client.post(_url(device_id), headers=await api_env.headers(org_id, tech))

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["already_requested"] is False
    assert body["restart"]["state"] == "pending"
    assert body["restart"]["origin"] == "operator"
    assert body["window"]["user_warning_minutes"] == 8 * 60
    [row] = await _rows(api_env.admin_engine, org_id)
    assert (row.device_id, row.origin, row.state) == (device_id, "operator", "pending")
    # A record, never a job: the sweep sends the signed job inside the window.
    assert row.job_id is None

    async with AsyncSession(api_env.admin_engine) as s:
        user_id = (
            await s.execute(
                select(User.id).where(User.org_id == org_id, User.external_subject == tech)
            )
        ).scalar_one()
    [event] = await _requests(api_env.admin_engine, org_id)
    assert (event.actor_type, event.actor_id) == ("user", user_id)
    assert (event.target_type, event.target_id) == ("device", str(device_id))


async def test_asking_twice_books_one_restart(api_env) -> None:
    org_id = await api_env.create_org("rst")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env.admin_engine, org_id)
    headers = await api_env.headers(org_id, tech)

    first = await api_env.client.post(_url(device_id), headers=headers)
    second = await api_env.client.post(_url(device_id), headers=headers)

    assert second.status_code == 202, second.text
    assert second.json()["already_requested"] is True
    assert second.json()["restart"]["id"] == first.json()["restart"]["id"]
    assert len(await _rows(api_env.admin_engine, org_id)) == 1
    assert len(await _requests(api_env.admin_engine, org_id)) == 1


async def test_a_restart_patching_left_owing_becomes_the_operators(api_env) -> None:
    """A waiting scan-found restart would be cancelled by the next scan that
    says none is needed; the operator asked for one regardless."""
    org_id = await api_env.create_org("rst")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env.admin_engine, org_id)
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(PatchReboot(org_id=org_id, device_id=device_id, origin="scan", state="pending"))
        await s.commit()

    resp = await api_env.client.post(_url(device_id), headers=await api_env.headers(org_id, tech))

    assert resp.status_code == 202, resp.text
    assert resp.json()["already_requested"] is True
    [row] = await _rows(api_env.admin_engine, org_id)
    assert row.origin == "operator"
    assert len(await _requests(api_env.admin_engine, org_id)) == 1


async def test_read_only_cannot_ask_for_a_restart(api_env) -> None:
    org_id = await api_env.create_org("rst")
    ro = await api_env.create_user(org_id, "read-only")
    device_id = await _device(api_env.admin_engine, org_id)

    resp = await api_env.client.post(_url(device_id), headers=await api_env.headers(org_id, ro))

    assert resp.status_code == 403
    assert await _rows(api_env.admin_engine, org_id) == []


async def test_another_orgs_device_is_not_found(api_env) -> None:
    mine = await api_env.create_org("rst")
    theirs = await api_env.create_org("rst")
    tech = await api_env.create_user(mine, "technician")
    their_device = await _device(api_env.admin_engine, theirs)
    headers = await api_env.headers(mine, tech)

    assert (await api_env.client.post(_url(their_device), headers=headers)).status_code == 404
    assert (await api_env.client.post(_url(uuid4()), headers=headers)).status_code == 404
    assert await _rows(api_env.admin_engine, theirs) == []


async def test_a_device_that_cannot_restart_is_refused(api_env) -> None:
    org_id = await api_env.create_org("rst")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    cases = {
        "no agent": await _device(api_env.admin_engine, org_id, agent=False),
        "revoked": await _device(api_env.admin_engine, org_id, revoked=True),
        "never said": await _device(api_env.admin_engine, org_id, capabilities={}),
        "cannot": await _device(
            api_env.admin_engine, org_id, capabilities={"patch.scan": True}
        ),
    }
    for case, device_id in cases.items():
        resp = await api_env.client.post(_url(device_id), headers=headers)
        assert resp.status_code == 409, (case, resp.text)
    assert await _rows(api_env.admin_engine, org_id) == []
    assert await _requests(api_env.admin_engine, org_id) == []


async def test_device_status_says_when_a_restart_would_happen(api_env) -> None:
    org_id = await api_env.create_org("rst")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env.admin_engine, org_id, window=None)

    resp = await api_env.client.get(
        f"/api/v1/patch/devices/{device_id}/status", headers=await api_env.headers(org_id, tech)
    )

    assert resp.status_code == 200, resp.text
    window = resp.json()["restart_window"]
    assert window["open_now"] is True
    assert window["opens_at"] is not None
    assert window["user_warning_minutes"] == 8 * 60


# --------------------------------------------------------------------------- #
# The sweep, for an operator's restart
# --------------------------------------------------------------------------- #


async def _org(admin_engine: AsyncEngine) -> Organization:
    slug = f"rst-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.commit()
        return org


async def _operator_row(app_engine: AsyncEngine, org_id: UUID, device_id: UUID) -> None:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        session.add(
            PatchReboot(org_id=org_id, device_id=device_id, origin="operator", state="pending")
        )


async def test_the_sweep_restarts_it_in_the_window_even_if_the_os_does_not_ask(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await _org(admin_engine)
    device_id = await _device(admin_engine, org.id)
    await _operator_row(app_engine, org.id, device_id)
    engine = _FakeJobEngine()

    async with org_session(org.id, engine=app_engine, clients=ClientScope.all()) as session:
        closed = SAT - _dt.timedelta(days=1)
        assert await run_reboot_sweep(session, engine, org_id=org.id, now=closed) == []
        pending = await run_reboot_sweep(session, engine, org_id=org.id, now=SAT)

    assert [device for device, _ in pending] == [device_id]
    [created] = engine.created
    assert created["type"] == "patch.reboot"
    assert created["params"]["only_if_required"] is False
    assert created["params"]["deferral_hours"] == 8
    assert created["params"]["not_after"] == "2026-09-26T06:00:00Z"


async def test_a_never_restart_policy_does_not_cancel_an_operators_restart(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await _org(admin_engine)
    device_id = await _device(admin_engine, org.id, reboot="never")
    await _operator_row(app_engine, org.id, device_id)
    engine = _FakeJobEngine()

    async with org_session(org.id, engine=app_engine, clients=ClientScope.all()) as session:
        await run_reboot_sweep(session, engine, org_id=org.id, now=SAT)

    [row] = await _rows(admin_engine, org.id)
    assert row.state == "dispatched"
    assert len(engine.created) == 1


async def test_a_scan_that_needs_no_restart_leaves_an_operators_restart_alone(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await _org(admin_engine)
    device_id = await _device(admin_engine, org.id)
    await _operator_row(app_engine, org.id, device_id)
    async with org_session(org.id, engine=app_engine, clients=ClientScope.all()) as session:
        await note_scan(
            session,
            org_id=org.id,
            device_id=device_id,
            reboot_required=False,
            booted_at=None,
            now=SAT,
        )

    [row] = await _rows(admin_engine, org.id)
    assert row.state == "pending"
