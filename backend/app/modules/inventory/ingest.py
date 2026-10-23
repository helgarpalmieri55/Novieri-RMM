"""Inventory ingestion (spec §6). Runs inside the job's tenant transaction —
a failure anywhere rolls the whole sweep back; nothing half-ingests."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.models import (
    Device,
    HardwareComponent,
    InstalledSoftware,
    InventoryChange,
    InventorySnapshot,
    Policy,
)
from app.modules.inventory.rules import evaluate_rules
from app.modules.inventory.schema import InventoryDocument

SNAPSHOT_KEEP = 10
TRACKED_FACTS = ("domain_joined", "disk_encryption", "firewall_enabled")
DEFAULT_SWEEP_POLICY: dict[str, Any] = {"interval_hours": 24}


class InventoryValidationError(Exception):
    pass


@dataclass(frozen=True)
class IngestStats:
    software_added: int
    software_removed: int
    software_updated: int
    changes: int


def _components_of_type(doc: InventoryDocument, kind: str) -> list[dict[str, Any]]:
    return [
        {"identifier": c.identifier, **c.attributes}
        for c in doc.hardware.components
        if c.type == kind
    ]


async def ingest_inventory(
    session: AsyncSession, device: Device, doc: InventoryDocument
) -> IngestStats:
    now = datetime.now(UTC)
    org_id = device.org_id
    first_sweep = (
        await session.execute(
            select(InventorySnapshot.id).where(InventorySnapshot.device_id == device.id).limit(1)
        )
    ).scalar_one_or_none() is None

    previous_facts = await _previous_facts(session, device)

    # 1. Device summary upsert
    changes: list[InventoryChange] = []
    if not first_sweep and doc.os_version and device.os_version != doc.os_version:
        changes.append(
            InventoryChange(
                org_id=org_id,
                device_id=device.id,
                ts=now,
                change_type="os_updated",
                subject="os_version",
                from_value=device.os_version,
                to_value=doc.os_version,
            )
        )
    device.hostname = doc.hostname
    device.fqdn = doc.fqdn
    device.os_version = doc.os_version
    device.architecture = doc.architecture
    device.serial = doc.serial or device.serial
    device.cpu = doc.hardware.cpu
    device.ram_bytes = doc.hardware.ram_bytes
    device.disks = {"items": _components_of_type(doc, "disk")}
    device.nics = {"items": _components_of_type(doc, "nic")}
    device.last_seen_at = now
    session.add(device)

    # 2. Software diff
    existing = {
        row.name: row
        for row in (
            await session.execute(
                select(InstalledSoftware).where(InstalledSoftware.device_id == device.id)
            )
        ).scalars()
    }
    incoming = {s.name: s for s in doc.software}
    # No rows yet means no software list has ever been ingested for this
    # device (agents before 2026-09-23 sent none). Its first list is a
    # baseline, like a first sweep, not one `software_installed` per package.
    software_baseline = first_sweep or not existing

    added = updated = 0
    for name, item in incoming.items():
        current = existing.get(name)
        if current is None:
            # `UniqueConstraint("device_id", "name")` backs this row, and
            # ingestion is agent-driven and high-frequency: two overlapping
            # sweeps for the SAME device (e.g. a scheduled sweep racing a
            # manual "resync now") can both miss `existing` above before
            # either has written, and a plain insert would let the loser
            # 500 on a raw `IntegrityError`. Unlike the reject-with-error
            # duplicate checks elsewhere, this one is genuinely idempotent
            # -- both sweeps are reporting the SAME device's current
            # software list, so the correct resolution isn't "reject the
            # second caller", it's "the row ends up holding whichever
            # version was reported last", exactly like the "already
            # tracked, version changed" branch just below. `ON CONFLICT
            # (device_id, name) DO UPDATE` gives that for free instead of
            # forcing a separate reject-and-retry path.
            await session.execute(
                pg_insert(InstalledSoftware)
                .values(
                    id=uuid7(),
                    org_id=org_id,
                    device_id=device.id,
                    name=name,
                    version=item.version,
                    publisher=item.publisher,
                    install_date=item.install_date,
                )
                .on_conflict_do_update(
                    index_elements=["device_id", "name"],
                    set_={
                        "version": item.version,
                        "publisher": item.publisher,
                        "install_date": item.install_date,
                    },
                )
            )
            added += 1
            if not software_baseline:
                changes.append(
                    InventoryChange(
                        org_id=org_id,
                        device_id=device.id,
                        ts=now,
                        change_type="software_installed",
                        subject=name,
                        from_value=None,
                        to_value=item.version,
                    )
                )
        elif current.version != item.version:
            changes.append(
                InventoryChange(
                    org_id=org_id,
                    device_id=device.id,
                    ts=now,
                    change_type="software_updated",
                    subject=name,
                    from_value=current.version,
                    to_value=item.version,
                )
            )
            current.version = item.version
            current.publisher = item.publisher
            session.add(current)
            updated += 1

    removed = 0
    for name, current in existing.items():
        if name not in incoming:
            changes.append(
                InventoryChange(
                    org_id=org_id,
                    device_id=device.id,
                    ts=now,
                    change_type="software_removed",
                    subject=name,
                    from_value=current.version,
                    to_value=None,
                )
            )
            await session.delete(current)
            removed += 1

    # 3. Tracked-fact changes
    if not first_sweep:
        for key in TRACKED_FACTS:
            before, after = previous_facts.get(key), doc.facts.get(key)
            if before is not None and after is not None and before != after:
                changes.append(
                    InventoryChange(
                        org_id=org_id,
                        device_id=device.id,
                        ts=now,
                        change_type="fact_changed",
                        subject=key,
                        from_value=str(before),
                        to_value=str(after),
                    )
                )

    for change in changes:
        session.add(change)

    # 4. Hardware components: wholesale replace
    await session.execute(
        delete(HardwareComponent).where(HardwareComponent.device_id == device.id)
    )
    for component in doc.hardware.components:
        session.add(
            HardwareComponent(
                org_id=org_id,
                device_id=device.id,
                type=component.type,
                identifier=component.identifier,
                attributes=component.attributes,
            )
        )

    # 5. Snapshot + prune to the latest SNAPSHOT_KEEP
    session.add(
        InventorySnapshot(
            org_id=org_id,
            device_id=device.id,
            taken_at=now,
            raw=doc.model_dump(mode="json"),
            software_count=len(incoming),
            changes_count=len(changes),
        )
    )
    await session.flush()
    stale = (
        (
            await session.execute(
                select(InventorySnapshot)
                .where(InventorySnapshot.device_id == device.id)
                .order_by(InventorySnapshot.id.desc())
                .offset(SNAPSHOT_KEEP)
            )
        )
        .scalars()
        .all()
    )
    for snapshot in stale:
        await session.delete(snapshot)

    # 6. Seed the org's default sweep policy once
    has_policy = (
        await session.execute(select(Policy.id).where(Policy.module == "inventory").limit(1))
    ).scalar_one_or_none()
    if has_policy is None:
        session.add(
            Policy(org_id=org_id, module="inventory", config=dict(DEFAULT_SWEEP_POLICY))
        )

    # 7. Dynamic grouping
    await evaluate_rules(session, device, doc, org_id=org_id)
    await session.flush()

    return IngestStats(
        software_added=added,
        software_removed=removed,
        software_updated=updated,
        changes=len(changes),
    )


async def _previous_facts(session: AsyncSession, device: Device) -> dict[str, Any]:
    latest = (
        await session.execute(
            select(InventorySnapshot.raw)
            .where(InventorySnapshot.device_id == device.id)
            .order_by(InventorySnapshot.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not latest:
        return {}
    facts = latest.get("facts", {})
    return facts if isinstance(facts, dict) else {}
