from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    Device,
    DeviceGroup,
    DevicePatchStatus,
    Organization,
    Patch,
    PatchApproval,
    PatchPolicy,
)
from app.modules.patch.ingest import PatchValidationError, ingest_scan
from app.modules.patch.schema import PatchScanDocument


async def _dev(admin_engine, *, auto=False):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="pi", slug=f"pi-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        grp = DeviceGroup(org_id=org.id, name="g", client_id=org.id)
        s.add(grp)
        await s.flush()
        dev = Device(
            org_id=org.id,
            hostname="h",
            os_family="windows",
            device_group_id=grp.id,
            client_id=org.id,
            site_id=org.id,
        )
        s.add(dev)
        if auto:
            s.add(
                PatchPolicy(
                    org_id=org.id,
                    device_group_id=grp.id,
                    ring_name="test",
                    auto_approve_security=True,
                )
            )
        await s.commit()
        return org.id, dev.id


def _doc(sev="critical", installed=None):
    return PatchScanDocument(
        os_family="windows",
        scanned_at=datetime.now(UTC).isoformat(),
        installed_identifiers=installed or [],
        patches=[
            {
                "source": "windows_update",
                "identifier": "KB1",
                "title": "Cum Update",
                "severity": sev,
            }
        ],
    )


async def test_ingest_creates_catalog_and_missing_status(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        stats = await ingest_scan(s, await s.get(Device, device_id), _doc())
        assert stats.missing == 1 and stats.auto_approved == 0
        st = (await s.execute(select(DevicePatchStatus))).scalar_one()
        assert st.state == "missing"


async def test_auto_approve_security_into_test_ring(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine, auto=True)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        stats = await ingest_scan(s, await s.get(Device, device_id), _doc(sev="critical"))
        assert stats.auto_approved == 1
        st = (await s.execute(select(DevicePatchStatus))).scalar_one()
        assert st.state == "approved"
        appr = (await s.execute(select(PatchApproval))).scalar_one()
        assert appr.ring_name == "test" and appr.revoked is False


async def test_auto_approve_skips_non_security(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine, auto=True)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        stats = await ingest_scan(s, await s.get(Device, device_id), _doc(sev="low"))
        assert stats.auto_approved == 0
        assert (await s.execute(select(DevicePatchStatus))).scalar_one().state == "missing"


async def test_installed_identifier_marks_installed(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await ingest_scan(
            s, await s.get(Device, device_id), _doc(installed=["windows_update:KB1"])
        )
        st = (await s.execute(select(DevicePatchStatus))).scalar_one()
        assert st.state == "installed" and st.applied_at is not None


def _doc_two(entry_a_superseded_by="KB2"):
    return PatchScanDocument(
        os_family="windows",
        scanned_at=datetime.now(UTC).isoformat(),
        installed_identifiers=[],
        patches=[
            {
                "source": "windows_update",
                "identifier": "KB1",
                "title": "Cum Update A",
                "severity": "critical",
                "superseded_by": entry_a_superseded_by,
            },
            {
                "source": "windows_update",
                "identifier": "KB2",
                "title": "Cum Update B",
                "severity": "critical",
            },
        ],
    )


async def test_superseded_by_resolves_within_same_scan(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await ingest_scan(s, await s.get(Device, device_id), _doc_two())
        patch_a = (
            await s.execute(select(Patch).where(Patch.identifier == "KB1"))
        ).scalar_one()
        patch_b = (
            await s.execute(select(Patch).where(Patch.identifier == "KB2"))
        ).scalar_one()
        assert patch_a.superseded_by == patch_b.id


async def test_superseded_by_cleared_when_no_longer_reported(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        device = await s.get(Device, device_id)
        await ingest_scan(s, device, _doc_two())
        # Re-scan: KB1 no longer reports a superseded_by.
        await ingest_scan(s, device, _doc_two(entry_a_superseded_by=None))
        patch_a = (
            await s.execute(select(Patch).where(Patch.identifier == "KB1"))
        ).scalar_one()
        assert patch_a.superseded_by is None


async def test_superseded_by_missing_from_catalog_stays_none(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await ingest_scan(s, await s.get(Device, device_id), _doc(sev="critical"))
        # KB1's scan reports superseded_by="KB99", which is not in the catalog.
        doc = PatchScanDocument(
            os_family="windows",
            scanned_at=datetime.now(UTC).isoformat(),
            installed_identifiers=[],
            patches=[
                {
                    "source": "windows_update",
                    "identifier": "KB1",
                    "title": "Cum Update",
                    "severity": "critical",
                    "superseded_by": "KB99",
                }
            ],
        )
        await ingest_scan(s, await s.get(Device, device_id), doc)
        patch_a = (
            await s.execute(select(Patch).where(Patch.identifier == "KB1"))
        ).scalar_one()
        assert patch_a.superseded_by is None


async def test_os_family_mismatch_raises_validation_error(admin_engine, app_engine) -> None:
    org_id, device_id = await _dev(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        device = await s.get(Device, device_id)
        doc = _doc()
        doc.os_family = "linux"
        with pytest.raises(PatchValidationError):
            await ingest_scan(s, device, doc)
