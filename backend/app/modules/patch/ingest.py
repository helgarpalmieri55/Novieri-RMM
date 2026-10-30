"""Patch scan ingestion (docs/modules/patch/spec.md §6).

Mirrors `app/modules/inventory/ingest.py`'s shape: runs inside the job's tenant
transaction, upserts the org's patch catalog, reconciles each device's
per-patch status against the scan, and applies the PD-02 auto-approve rule for
security-severity patches. Does not publish a UI event — the job-result
handler (a later task) owns that.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.audit import audit_service
from app.models import Device, DevicePatchStatus, Patch, PatchApproval
from app.modules.patch.policy import resolve_patch_policy
from app.modules.patch.reboot import note_scan
from app.modules.patch.schema import APP_SOURCES, PatchEntry, PatchScanDocument

AUTO_APPROVE_SEVERITIES = {"critical", "important"}
AUTO_APPROVE_RING = "test"


class PatchValidationError(Exception):
    pass


def parse_wire_time(raw: str | None) -> datetime | None:
    """An agent RFC3339 time, or None when absent or unreadable: a bad
    timestamp costs the reboot bookkeeping, never the scan."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class IngestStats:
    patches_seen: int
    missing: int
    installed: int
    auto_approved: int


async def _upsert_patch(
    session: AsyncSession, org_id: UUID, doc_os_family: str, entry: PatchEntry
) -> Patch:
    existing = (
        await session.execute(
            select(Patch).where(
                Patch.org_id == org_id,
                Patch.os_family == doc_os_family,
                Patch.source == entry.source,
                Patch.identifier == entry.identifier,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        patch = Patch(
            org_id=org_id,
            os_family=doc_os_family,
            source=entry.source,
            identifier=entry.identifier,
            title=entry.title,
            severity=entry.severity,
            kb=entry.kb,
            cve=entry.cve,
            superseded_by=None,
        )
        session.add(patch)
        await session.flush()
        return patch
    # Update catalog fields; never touch `priority` (Sentinel owns it).
    existing.title = entry.title
    existing.severity = entry.severity
    existing.kb = entry.kb
    existing.cve = entry.cve
    session.add(existing)
    return existing


async def _resolve_superseded_by(
    session: AsyncSession, org_id: UUID, doc_os_family: str, entry: PatchEntry, patch: Patch
) -> None:
    """Resolve `entry.superseded_by` (a wire identifier) to a catalog `Patch.id`
    and set/clear it on `patch`. Mirrors the graceful-skip lookup style used
    for `installed_identifiers`: if the superseding patch isn't in the catalog
    yet, leave the field `None` rather than fabricating a reference.
    """
    if not entry.superseded_by:
        patch.superseded_by = None
        session.add(patch)
        return
    superseding = (
        await session.execute(
            select(Patch).where(
                Patch.org_id == org_id,
                Patch.os_family == doc_os_family,
                Patch.source == entry.source,
                Patch.identifier == entry.superseded_by,
            )
        )
    ).scalar_one_or_none()
    if superseding is None or superseding.id == patch.id:
        patch.superseded_by = None
    else:
        patch.superseded_by = superseding.id
    session.add(patch)


async def ingest_scan(
    session: AsyncSession, device: Device, doc: PatchScanDocument
) -> IngestStats:
    now = datetime.now(UTC)
    org_id = device.org_id

    if doc.os_family != device.os_family:
        raise PatchValidationError(  # i18n-exempt: stored as the job's failure reason
            f"scan os_family {doc.os_family!r} does not match device os_family "
            f"{device.os_family!r}"
        )

    installed_ids = set(doc.installed_identifiers)

    # 1. Upsert the org's patch catalog for every reported patch. All catalog
    # rows are inserted/updated first (title/severity/kb/cve; `priority` still
    # never touched) so that pass 2 below can resolve an intra-scan supersede
    # relationship even when both patches are new this scan.
    patches: list[Patch] = []
    for entry in doc.patches:
        patches.append(await _upsert_patch(session, org_id, doc.os_family, entry))
    await session.flush()

    # 2. Second pass: resolve/clear `superseded_by` now that every entry in
    # this scan has a catalog row.
    for entry, catalog_patch in zip(doc.patches, patches, strict=True):
        await _resolve_superseded_by(session, org_id, doc.os_family, entry, catalog_patch)
    await session.flush()

    patch_by_key: dict[str, Patch] = {f"{p.source}:{p.identifier}": p for p in patches}

    # `installed_identifiers` can reference a patch this scan didn't re-report
    # (the agent only lists outstanding patches in `patches`; a patch applied
    # since the last scan may now be reported only as installed). Resolve
    # those against the existing catalog so they still reconcile correctly
    # instead of being wrongly classified `not_applicable` below.
    unresolved_keys = [key for key in installed_ids if key not in patch_by_key]
    if unresolved_keys:
        pairs = [key.split(":", 1) for key in unresolved_keys if ":" in key]
        for source, identifier in pairs:
            resolved = (
                await session.execute(
                    select(Patch).where(
                        Patch.org_id == org_id,
                        Patch.os_family == doc.os_family,
                        Patch.source == source,
                        Patch.identifier == identifier,
                    )
                )
            ).scalar_one_or_none()
            if resolved is not None:
                patch_by_key[f"{source}:{identifier}"] = resolved

    # Existing status rows for this device, keyed by patch_id.
    existing_status = {
        row.patch_id: row
        for row in (
            await session.execute(
                select(DevicePatchStatus).where(
                    DevicePatchStatus.org_id == org_id,
                    DevicePatchStatus.device_id == device.id,
                )
            )
        )
        .scalars()
        .all()
    }

    reconciled_patch_ids: set[UUID] = set()
    missing_count = 0
    installed_count = 0
    newly_missing: list[Patch] = []

    # 3a. Everything confirmed installed this scan (whether scanned or only
    # known from an earlier catalog upsert) -> `installed`.
    for key in installed_ids:
        patch = patch_by_key.get(key)
        if patch is None:
            continue  # no catalog entry for this identifier; nothing to reconcile
        reconciled_patch_ids.add(patch.id)
        status = existing_status.get(patch.id)
        if status is None:
            status = DevicePatchStatus(
                org_id=org_id,
                device_id=device.id,
                patch_id=patch.id,
                state="installed",
                applied_at=now,
            )
            session.add(status)
            existing_status[patch.id] = status
        elif status.state != "installed":
            status.state = "installed"
            status.applied_at = now
            session.add(status)
        installed_count += 1

    # 3b. Scanned patches not confirmed installed -> `missing` (never downgrade
    # an already-approved/installed row).
    for patch in patches:
        if patch.id in reconciled_patch_ids:
            continue
        reconciled_patch_ids.add(patch.id)
        status = existing_status.get(patch.id)
        if status is None:
            status = DevicePatchStatus(
                org_id=org_id,
                device_id=device.id,
                patch_id=patch.id,
                state="missing",
                detected_at=now,
            )
            session.add(status)
            existing_status[patch.id] = status
            missing_count += 1
            newly_missing.append(patch)
        elif status.state == "approved":
            pass  # never downgrade
        elif status.state == "installed" and patch.source not in APP_SOURCES:
            pass  # never downgrade
        elif status.state == "installed":
            # An app is keyed by package id, so being offered again means a
            # newer version: outstanding, under the approval already given.
            status.state = "missing"
            session.add(status)
            missing_count += 1
        else:
            was_missing = status.state == "missing"
            status.state = "missing"
            session.add(status)
            missing_count += 1
            if not was_missing:
                newly_missing.append(patch)

    await session.flush()

    # 4. Patches no longer reported (and not confirmed installed) ->
    # `not_applicable`.
    for patch_id, status in existing_status.items():
        if patch_id in reconciled_patch_ids:
            continue
        if status.state in ("missing", "approved"):
            status.state = "not_applicable"
            session.add(status)

    # 5. Auto-approve rule (PD-02).
    auto_approved = 0
    policy = await resolve_patch_policy(session, org_id, device.id)
    if policy.auto_approve_security:
        for patch in newly_missing:
            if patch.severity not in AUTO_APPROVE_SEVERITIES:
                continue
            # The auto-approval is the REPORTING DEVICE'S CLIENT'S (5a; final
            # review D Important 1): the policy that asked for it is of a
            # group of that client, and a global row would clear the patch
            # for every client's ring -- a decision only a person with every
            # client takes (`approve.py`).
            #
            # A PERSON'S REVOCATION WINS (controller decision on the final
            # review): a revoked (patch, ring) approval, the global one or
            # the client's own, stops it -- no row, the device not marked
            # approved, nothing audited. A live one of either owner already
            # covers the client's machines: nothing to insert. Another
            # client's row, revoked or not, says nothing about this client.
            owners = (
                await session.execute(
                    select(PatchApproval.client_id, PatchApproval.revoked).where(
                        PatchApproval.org_id == org_id,
                        or_(
                            PatchApproval.client_id.is_(None),
                            PatchApproval.client_id == device.client_id,
                        ),
                        PatchApproval.patch_id == patch.id,
                        PatchApproval.ring_name == AUTO_APPROVE_RING,
                    )
                )
            ).all()
            if any(revoked for _, revoked in owners):
                continue
            if not owners:
                # Two scans of the client's machines can both miss the
                # look-up; the loser's insert is a no-op, not a unique
                # violation that rolls back its whole scan.
                await session.execute(
                    pg_insert(PatchApproval)
                    .values(
                        id=uuid7(),
                        org_id=org_id,
                        client_id=device.client_id,
                        patch_id=patch.id,
                        ring_name=AUTO_APPROVE_RING,
                        approved_by=None,
                        revoked=False,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["org_id", "client_id", "patch_id", "ring_name"]
                    )
                )
            status = existing_status.get(patch.id)
            if status is not None:
                status.state = "approved"
                session.add(status)
            auto_approved += 1
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="patch.auto_approved",
                target_type="patch",
                target_id=str(patch.id),
                client_id=device.client_id,
                metadata={"device_id": str(device.id), "ring": AUTO_APPROVE_RING},
            )

    await session.flush()

    # 6. Restarts: a boot after an open restart closes it; a pending restart
    # with none open opens one.
    await note_scan(
        session,
        org_id=org_id,
        device_id=device.id,
        reboot_required=doc.reboot_required,
        booted_at=parse_wire_time(doc.booted_at),
        now=now,
    )

    return IngestStats(
        patches_seen=len(doc.patches),
        missing=missing_count,
        installed=installed_count,
        auto_approved=auto_approved,
    )
