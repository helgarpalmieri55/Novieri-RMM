"""The scan's automatic approval (PD-02) belongs to the reporting device's
client (final review D Important 1).

It used to be a GLOBAL row: a technician narrowed to Y, holding
`patch.policy.manage`, ticked "approve security updates automatically" on one
of Y's groups, and the first scan of a Y machine cleared the patch for every
client's test ring -- X's machines included, without X's change gate. A
global approval is a decision for every client, which only a person with
every client may take (`approve.py`); a policy of Y's decides for Y.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Client,
    Device,
    DeviceGroup,
    DevicePatchStatus,
    Organization,
    Patch,
    PatchApproval,
    PatchPolicy,
    Site,
)
from app.modules.patch.apply import _ring_approved_patch_ids
from app.modules.patch.ingest import AUTO_APPROVE_RING, ingest_scan
from app.modules.patch.schema import PatchScanDocument

KB = "KB-sec"


@dataclass(frozen=True)
class Estate:
    org: UUID
    x: UUID
    y: UUID
    patch: UUID
    x_device: UUID
    y_devices: tuple[UUID, UUID]


async def _estate(admin_engine: AsyncEngine) -> Estate:
    """X and Y, each with a test-ring group; only Y's approves automatically.
    The patch is already in the catalog, as the first scan anywhere left it."""
    tag = uuid4().hex[:8]
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=f"auto-{tag}", slug=f"auto-{tag}")
        s.add(org)
        await s.flush()
        devices: dict[str, list[UUID]] = {}
        clients: dict[str, UUID] = {}
        for name, auto, count in (("X", False, 1), ("Y", True, 2)):
            client = Client(org_id=org.id, name=f"{name} {tag}", kind="customer")
            s.add(client)
            await s.flush()
            site = Site(org_id=org.id, client_id=client.id, name="Principal")
            group = DeviceGroup(org_id=org.id, client_id=client.id, name="Test ring")
            s.add_all([site, group])
            await s.flush()
            s.add(
                PatchPolicy(
                    org_id=org.id,
                    device_group_id=group.id,
                    ring_name=AUTO_APPROVE_RING,
                    auto_approve_security=auto,
                )
            )
            made = [
                Device(
                    org_id=org.id,
                    client_id=client.id,
                    site_id=site.id,
                    device_group_id=group.id,
                    hostname=f"{name.lower()}-{n}",
                    os_family="windows",
                )
                for n in range(count)
            ]
            s.add_all(made)
            await s.flush()
            devices[name] = [d.id for d in made]
            clients[name] = client.id
        patch = Patch(
            org_id=org.id,
            os_family="windows",
            source="windows_update",
            identifier=KB,
            title="Security update",
            severity="critical",
        )
        s.add(patch)
        await s.commit()
        return Estate(
            org=org.id,
            x=clients["X"],
            y=clients["Y"],
            patch=patch.id,
            x_device=devices["X"][0],
            y_devices=(devices["Y"][0], devices["Y"][1]),
        )


def _scan() -> PatchScanDocument:
    return PatchScanDocument(
        os_family="windows",
        scanned_at=datetime.now(UTC).isoformat(),
        installed_identifiers=[],
        patches=[
            {
                "source": "windows_update",
                "identifier": KB,
                "title": "Security update",
                "severity": "critical",
            }
        ],
    )


async def _ingest(app_engine: AsyncEngine, org: UUID, device_id: UUID) -> int:
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as s:
        device = await s.get(Device, device_id)
        assert device is not None
        return (await ingest_scan(s, device, _scan())).auto_approved


async def _approvals(admin_engine: AsyncEngine, estate: Estate) -> list[PatchApproval]:
    async with AsyncSession(admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(PatchApproval).where(
                        PatchApproval.org_id == estate.org,
                        PatchApproval.patch_id == estate.patch,
                    )
                )
            ).scalars()
        )


async def test_y_s_policy_approves_for_y_s_test_ring_only(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    estate = await _estate(admin_engine)
    assert await _ingest(app_engine, estate.org, estate.y_devices[0]) == 1
    assert await _ingest(app_engine, estate.org, estate.x_device) == 0

    approvals = await _approvals(admin_engine, estate)
    assert [(a.client_id, a.ring_name, a.approved_by) for a in approvals] == [
        (estate.y, AUTO_APPROVE_RING, None)
    ]
    async with org_session(estate.org, engine=app_engine, clients=ClientScope.all()) as s:
        for client, expected in ((estate.x, set()), (estate.y, {estate.patch})):
            cleared = await _ring_approved_patch_ids(
                s,
                org_id=estate.org,
                ring_name=AUTO_APPROVE_RING,
                patch_ids={estate.patch},
                client_id=client,
            )
            assert cleared == expected, client
        x_status = (
            await s.execute(
                select(DevicePatchStatus.state).where(
                    DevicePatchStatus.device_id == estate.x_device
                )
            )
        ).scalar_one()
    assert x_status == "missing"


async def test_the_automatic_approval_audit_names_the_device_s_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    estate = await _estate(admin_engine)
    await _ingest(app_engine, estate.org, estate.y_devices[0])
    async with AsyncSession(admin_engine) as s:
        clients = (
            await s.execute(
                select(AuditEvent.client_id).where(
                    AuditEvent.org_id == estate.org, AuditEvent.action == "patch.auto_approved"
                )
            )
        ).scalars().all()
    assert clients == [estate.y]


async def test_two_scans_of_one_client_make_one_approval(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    estate = await _estate(admin_engine)
    for device in estate.y_devices:
        assert await _ingest(app_engine, estate.org, device) == 1
    assert [a.client_id for a in await _approvals(admin_engine, estate)] == [estate.y]


async def test_two_concurrent_scans_of_one_client_make_one_approval(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The check-then-insert race: scan A's look-up misses, scan B inserts
    and commits, then A inserts. `ON CONFLICT DO NOTHING` on
    `uq_patch_approvals_client_patch_ring` makes A's insert a no-op rather
    than a unique violation that rolls back A's whole scan."""
    estate = await _estate(admin_engine)
    first, second = estate.y_devices
    async with org_session(estate.org, engine=app_engine, clients=ClientScope.all()) as a:
        original = a.execute
        interleaved = False

        async def interleaving_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal interleaved
            result = await original(statement, *args, **kwargs)
            if (
                not interleaved
                and getattr(statement, "is_select", False)
                and "patch_approvals" in str(statement).lower()
            ):
                interleaved = True
                assert await _ingest(app_engine, estate.org, second) == 1
            return result

        a.execute = interleaving_execute  # type: ignore[method-assign]
        device = await a.get(Device, first)
        assert device is not None
        assert (await ingest_scan(a, device, _scan())).auto_approved == 1
    assert interleaved
    assert [a.client_id for a in await _approvals(admin_engine, estate)] == [estate.y]
