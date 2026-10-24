"""Third-party app updates (winget, Homebrew) through the patch model.

App updates arrive in the same scan document with `source` naming the package
manager, are approved into rings like OS updates, and are keyed by package id,
so a package offered again after an install is outstanding again under the
approval already given. `app_sources` says whether each manager could be
scanned, read back from the latest completed scan.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    Device,
    DevicePatchStatus,
    Job,
    JobStatus,
    Organization,
    Patch,
    PatchApproval,
)
from app.modules.patch.apply import dispatch_apply
from app.modules.patch.handler import register_job_types
from app.modules.patch.ingest import ingest_scan
from app.modules.patch.reporting import app_source_summary, device_report, update_rollup
from app.modules.patch.schema import PatchScanDocument

register_job_types()

CHROME = {
    "source": "winget",
    "identifier": "Google.Chrome",
    "title": "Google Chrome 128.0 → 129.0",
    "severity": "unknown",
}
KB = {"source": "windows_update", "identifier": "KB1", "title": "CU", "severity": "critical"}


async def _org_device(admin_engine: AsyncEngine, os_family: str = "windows") -> tuple[UUID, UUID]:
    slug = f"apps-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(
            org_id=org.id, hostname="wks", os_family=os_family, client_id=org.id, site_id=org.id
        )
        session.add(device)
        await session.commit()
        return org.id, device.id


async def _scan(app_engine: AsyncEngine, org_id: UUID, device_id: UUID, *patches: dict) -> None:
    doc = PatchScanDocument(
        os_family="windows",
        patches=list(patches),  # type: ignore[arg-type]
        scanned_at="2026-09-23T10:00:00Z",
    )
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        device = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
        await ingest_scan(session, device, doc)


async def _states(admin_engine: AsyncEngine, device_id: UUID) -> dict[str, str]:
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                select(Patch.identifier, DevicePatchStatus.state)
                .join(Patch, Patch.id == DevicePatchStatus.patch_id)
                .where(DevicePatchStatus.device_id == device_id)
            )
        ).all()
    return dict(rows)  # type: ignore[arg-type]


async def _mark_installed(admin_engine: AsyncEngine, device_id: UUID) -> None:
    async with AsyncSession(admin_engine) as session:
        for status in (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.device_id == device_id)
            )
        ).scalars():
            status.state = "installed"
        await session.commit()


async def test_an_app_offered_again_after_install_is_outstanding_again(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _org_device(admin_engine)
    await _scan(app_engine, org_id, device_id, CHROME, KB)
    assert await _states(admin_engine, device_id) == {"Google.Chrome": "missing", "KB1": "missing"}

    await _mark_installed(admin_engine, device_id)
    # Chrome has a newer version out; the KB is re-offered too (a scan quirk)
    # but an OS update is never downgraded from installed.
    await _scan(app_engine, org_id, device_id, CHROME, KB)
    assert await _states(admin_engine, device_id) == {
        "Google.Chrome": "missing",
        "KB1": "installed",
    }


async def test_an_approved_app_is_dispatched_by_id_like_an_os_update(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from tests.test_patch_reboot import _FakeJobEngine

    org_id, device_id = await _org_device(admin_engine)
    await _scan(app_engine, org_id, device_id, CHROME, KB)
    async with AsyncSession(admin_engine) as session:
        chrome = (
            await session.execute(
                select(Patch).where(Patch.org_id == org_id, Patch.identifier == "Google.Chrome")
            )
        ).scalar_one()
        session.add(PatchApproval(org_id=org_id, patch_id=chrome.id, ring_name="broad"))
        await session.commit()
    engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await dispatch_apply(session, engine, org_id=org_id, now=datetime.now(UTC))
    [created] = engine.created
    # Only the approved app travels; the unapproved KB does not.
    assert created["params"]["patches"] == [{"source": "winget", "identifier": "Google.Chrome"}]


async def test_the_update_list_separates_apps_from_os_updates(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _org_device(admin_engine)
    await _scan(app_engine, org_id, device_id, CHROME, KB)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        apps = await update_rollup(session, org_id, kind="apps")
        os_only = await update_rollup(session, org_id, kind="os")
        both = await update_rollup(session, org_id)
    assert [r["identifier"] for r in apps["items"]] == ["Google.Chrome"]
    assert [r["identifier"] for r in os_only["items"]] == ["KB1"]
    assert both["total"] == 2


async def test_app_source_status_is_read_from_the_latest_scan(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _org_device(admin_engine)
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine) as session:
        for age, state, detail in (
            (2, "failed", "source agreement"),
            (1, "unsupported", "winget (App Installer) is not installed on this device"),
        ):
            session.add(
                Job(
                    org_id=org_id,
                    type="patch.scan",
                    target_type="device",
                    target_id=device_id,
                    params={},
                    nonce=uuid4().hex,
                    issued_at=now,
                    expires_at=now,
                    status=JobStatus.succeeded,
                    finished_at=now - timedelta(hours=age),
                    result={
                        "app_sources": [{"source": "winget", "state": state, "detail": detail}]
                    },
                )
            )
        await session.commit()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        report = await device_report(session, org_id, device_id)
        summary = await app_source_summary(session, org_id, "windows")
        other = await app_source_summary(session, org_id, "macos")
    assert report["app_sources"] == [
        {
            "source": "winget",
            "state": "unsupported",
            "detail": "winget (App Installer) is not installed on this device",
        }
    ]
    assert summary == [
        {
            "source": "winget",
            "state": "unsupported",
            "devices": 1,
            "detail": "winget (App Installer) is not installed on this device",
        }
    ]
    assert other == []


async def test_the_app_routes_are_read_gated(api_env) -> None:
    org_id = await api_env.create_org("patch")
    nobody = await api_env.create_user(org_id)
    reader = await api_env.create_user(org_id, "read-only")
    for path in ("/api/v1/patch/app-sources", "/api/v1/patch/updates?kind=apps"):
        denied = await api_env.client.get(path, headers=await api_env.headers(org_id, nobody))
        assert denied.status_code == 403, path
        ok = await api_env.client.get(path, headers=await api_env.headers(org_id, reader))
        assert ok.status_code == 200, (path, ok.text)
    bad = await api_env.client.get(
        "/api/v1/patch/updates?kind=drivers", headers=await api_env.headers(org_id, reader)
    )
    assert bad.status_code == 422
