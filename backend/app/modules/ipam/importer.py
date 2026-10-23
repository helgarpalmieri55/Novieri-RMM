"""CSV import, CSV export, and the documentation `network`-record converter
(`docs/modules/ipam/spec.md` §8.1, §8.2, §8.3, §0.14, §0.18, §11.3).

**IMPORT IS TWO STEPS, BECAUSE A ONE-SHOT IMPORT INTO A PLAN OF RECORD IS HOW A
PLAN OF RECORD GETS DESTROYED.** `preview` parses, validates and diffs, and
**writes nothing**; `apply` re-derives the same diff and applies it in one
transaction.

**AND THE DIGEST IS A CONSISTENCY CHECK OVER ONE REQUEST'S INPUTS, NOT PROOF
THAT A PREVIEW EVER HAPPENED.** This is worth stating plainly because the
sentence that used to be here -- "the digest is what makes the two steps be
about the same file; without it, 'preview this, apply that' is one paste away"
-- claimed more than the server enforces, and the claim was the dangerous part.
Nothing server-side records that a preview ran. `apply` hashes the bytes and the
mapping it was handed *in that same request*, so the only thing it can catch is
a client echoing a digest that does not describe what it is now sending: a stale
digest left over from an earlier preview, a re-mapped set of columns, a
different sheet pasted into a form that kept the old token. It cannot catch a
caller who never previewed at all -- that caller computes the digest itself, in
one line, from the file it is about to apply.

Two consequences follow, and both are deliberate:

1. **THE DIGEST COVERS THE MAPPING AS WELL AS THE FILE** (`preview_digest`).
   The mapping decides which spreadsheet column becomes `asset_label` and which
   becomes `hostname`, so "the same file with a different mapping" is a
   different import of the same bytes -- and a digest over the bytes alone
   accepted it silently, with the previewed digest, against the diff the
   operator approved. That was a real gap in the check the digest *does*
   perform, and closing it costs one hash of a canonicalised mapping.
2. **THE PREVIEW IS NOT PERSISTED, AND THAT IS A JUDGED TRADE, NOT AN
   OVERSIGHT.** Binding `apply` to a stored preview would need a table, a
   migration, an expiry policy, a per-org row budget for previews nobody
   applies, and a second reason for an import to fail ("your preview expired")
   -- to stop a caller who holds `ipam.manage`, is acting inside their own org,
   and is authorized to write every row they are writing, from skipping their
   own confirmation step. That is not a privilege boundary; it is a
   double-entry affordance. What actually bounds it is stated rather than
   assumed: `ipam.manage` is admin-tier (§11.2), the apply is ONE transaction
   so a partial sheet is impossible, every input row comes back with a verdict
   whatever happens, and `ipam.import_applied` records the file's sha256, the
   mapping and the counts -- so an import nobody previewed is fully visible
   afterwards even though it was not prevented. If this module ever gains an
   import path reachable by a lower-privilege role, that trade changes and the
   preview must be persisted.

`frontend/src/modules/ipam/ImportPage.tsx` clears the preview when the file or
the mapping changes, which is the right behaviour for the accident this is
really about -- but per invariant #6 that is an affordance, not a control, and
this docstring no longer describes it as one.

**UNPARSEABLE ROWS ARE REPORTED, NEVER SILENTLY SKIPPED.** Silent skipping is how
an import "succeeds" and loses a third of the sheet, and the operator finds out a
quarter later. Every row that goes in comes back out with a verdict, and
`len(response.rows) == len(file rows)` is a test, not a hope.

**IMPORT NEVER CREATES AN AUTHORIZATION AND NEVER ENABLES A SWEEP** (§8.1). If it
did, an import would be a way to authorize scanning without holding
`ipam.scope.manage` -- the one grant §11.2 keeps away from technicians because it
decides where this platform emits network traffic. Both negatives are tested
explicitly; there is no code path here that touches `ipam_scan_authorization` or
`ipam_subnet.sweep_enabled`, and that is the point.

**THE `network`-RECORD CONVERTER IS NOT A DATA MIGRATION** (§8.3). A silent
conversion would hit unparseable CIDRs, already-registered prefixes and gateways
outside their prefix with no operator to ask, so it is operator-invoked,
**`dry_run=True` BY DEFAULT**, and every row it will not convert comes back in
`skipped` **with a reason**. The source record is **archived, not deleted**, so
`doc_links` and `doc_versions` survive -- `doc_versions.record_id` is a real
foreign key.

**EVERY OPERATOR- OR AGENT-SUPPLIED TEXT COLUMN IN AN EXPORT GOES THROUGH
`escape_formula`** (§8.2, `app/core/csv_export.py`) -- per column, never blanket
over the row. A cell beginning `=`, `+`, `-` or `@` executes as a formula the
instant the file is opened in Excel; a server-formatted timestamp, UUID or enum
cannot carry a payload and escaping it would only corrupt it for a machine
reader. `dormant` is excluded from `drift.csv`: it is not drift (§4.3), and an
export of "drift" that contained it would be the mixed list §0.11 says nobody
opens twice.
"""

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import ip_network
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.csv_export import escape_formula
from app.core.i18n import tr
from app.models import DocRecord
from app.modules.ipam.addresses import assign, reserve, update_address
from app.modules.ipam.allocation import ALLOCATION_METHODS
from app.modules.ipam.errors import IpamValidationError
from app.modules.ipam.models import IpamAddress, IpamDrift, IpamSubnet, IpamVlan
from app.modules.ipam.net import parse_address, parse_mac
from app.modules.ipam.reconcile import DRIFT_CLASSES
from app.modules.ipam.registry import ensure_default_vrf
from app.modules.ipam.subnets import create_subnet

__all__ = [
    "IMPORT_FIELDS",
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_ROWS",
    "ConvertedRecord",
    "DocRecordConversion",
    "ImportPreview",
    "ImportResult",
    "ImportRow",
    "apply_import",
    "convert_doc_records",
    "export_addresses_csv",
    "export_drift_csv",
    "export_subnets_csv",
    "file_digest",
    "preview_digest",
    "preview_import",
]

#: §8.1's limits. Bounded before parsing, not after: a 200 MB paste must cost a
#: length check rather than a `csv.reader` over 200 MB.
MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_ROWS = 50_000

#: The `ipam_field` values a mapping may name. `state` is ABSENT and always will
#: be: §8.1 fixes it from the claimant, and a column that could set it would let
#: a spreadsheet write the one column §0.6 reserves for a human decision made
#: through an audited transition.
IMPORT_FIELDS = (
    "address",
    "hostname",
    "description",
    "asset_label",
    "mac",
    "allocation_method",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def file_digest(content: bytes) -> str:
    """The sha256 OF THE FILE, and only the file.

    This is the value `ipam.import_applied` records, so the audit log names the
    bytes an operator can re-hash a year later. It is **not** what `apply`
    verifies -- see `preview_digest`, which also covers the mapping. Keeping the
    two separate is the point: an audit entry whose "file_digest" were really a
    digest of the file *and* something else would not identify the file.
    """
    return hashlib.sha256(content).hexdigest()


def preview_digest(content: bytes, mapping: dict[str, str]) -> str:
    """What `preview` returns and `apply` must echo: the file AND the mapping.

    **BOTH, BECAUSE THE MAPPING IS HALF THE IMPORT.** It decides which column
    becomes `asset_label` and which becomes `hostname`, so the same bytes under
    two mappings are two different imports -- and a digest over the bytes alone
    accepted the second one carrying the first one's digest.

    Canonical in two ways. The mapping is serialised with sorted keys and no
    whitespace, so `{"a": "x", "b": "y"}` and `{"b": "y", "a": "x"}` -- the same
    mapping, in whatever order a JSON parser happened to yield -- hash the same;
    the mapping arrives as JSON in a query parameter, so key order is not the
    client's to promise. And the serialisation is LENGTH-PREFIXED before the file
    bytes. A JSON object is already self-delimiting, so the prefix is belt and
    braces rather than the thing that closes an attack -- but "the boundary
    between the two inputs is explicit" is a property this function should hold
    on its own terms, not one inherited from the encoding somebody might change.

    Still 64 hex characters, so the wire contract is unchanged.
    """
    canonical = json.dumps(
        dict(sorted(mapping.items())), separators=(",", ":")
    ).encode("utf-8")
    hasher = hashlib.sha256()
    hasher.update(len(canonical).to_bytes(8, "big"))
    hasher.update(canonical)
    hasher.update(content)
    return hasher.hexdigest()


@dataclass(frozen=True)
class ImportRow:
    """One row's verdict. EVERY input row produces exactly one of these --
    including the ones that could not be parsed, which is the whole difference
    between "reported" and "skipped"."""

    line: int
    action: str  # create | update | conflict | error
    address: str
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportPreview:
    digest: str
    total_rows: int
    creates: int
    updates: int
    conflicts: int
    errors: int
    rows: list[ImportRow]


@dataclass(frozen=True)
class ImportResult:
    digest: str
    total_rows: int
    applied: int
    errors: int
    rows: list[ImportRow]


def _decode(content: bytes) -> list[dict[str, str]]:
    if len(content) > MAX_IMPORT_BYTES:
        raise IpamValidationError(
            tr(
                "the file is larger than {max_import_bytes} bytes",
                max_import_bytes=MAX_IMPORT_BYTES,
            )
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IpamValidationError(tr("the file is not valid UTF-8: {exc}", exc=exc)) from None
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        rows.append({k or "": (v or "") for k, v in row.items()})
        if len(rows) > MAX_IMPORT_ROWS:
            raise IpamValidationError(
                tr("the file has more than {max_import_rows} rows", max_import_rows=MAX_IMPORT_ROWS)
            )
    return rows


def _validate_mapping(mapping: dict[str, str]) -> dict[str, str]:
    if not isinstance(mapping, dict) or not mapping:
        raise IpamValidationError(tr("a {{csv_column: ipam_field}} mapping is required"))
    unknown = sorted(set(mapping.values()) - set(IMPORT_FIELDS))
    if unknown:
        raise IpamValidationError(
            tr(
                "unknown ipam_field(s) {unknown}; the mapping may name only {import_fields}",
                unknown=unknown,
                import_fields=list(IMPORT_FIELDS),
            )
        )
    if "address" not in mapping.values():
        raise IpamValidationError(tr("the mapping must name a column for `address`"))
    duplicated = sorted(
        {value for value in mapping.values() if list(mapping.values()).count(value) > 1}
    )
    if duplicated:
        raise IpamValidationError(
            tr("two columns map to the same field(s): {duplicated}", duplicated=duplicated)
        )
    return mapping


def _project(row: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    return {
        field_name: (row.get(column) or "").strip()
        for column, field_name in mapping.items()
    }


def _validate_row(values: dict[str, str]) -> str:
    """The row's error, or `""`. Never raises: a bad row is a REPORTED row."""
    address = parse_address(values.get("address", ""))
    if address is None:
        return tr(
            "{address} is not a valid IP address", address=repr(values.get("address", ""))
        )
    mac = values.get("mac", "")
    if mac and parse_mac(mac) is None:
        return tr("{mac} is not a valid MAC address", mac=repr(mac))
    method = values.get("allocation_method", "")
    if method and method not in ALLOCATION_METHODS:
        return tr(
            "allocation_method must be one of {allocation_methods}",
            allocation_methods=list(ALLOCATION_METHODS),
        )
    return ""


async def _existing(
    session: AsyncSession, org_id: UUID, vrf_id: UUID
) -> dict[str, IpamAddress]:
    rows = (
        (
            await session.execute(
                select(IpamAddress).where(
                    IpamAddress.org_id == org_id, IpamAddress.vrf_id == vrf_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {str(row.address): row for row in rows}


def _classify(values: dict[str, str], row: IpamAddress | None) -> tuple[str, str]:
    """`(action, note)` for one row against the current registry (§8.1's diff).

    `conflict` is specifically "this address is already assigned to SOMEBODY
    ELSE" -- not "a row exists". Re-importing the sheet that produced the plan
    must be an `update`, or every second import is a wall of conflicts nobody
    reads.
    """
    if row is None:
        return "create", ""
    label = values.get("asset_label", "")
    if row.state == "assigned":
        if row.device_id is not None:
            return "conflict", tr(
                "already assigned to device {device_id}", device_id=row.device_id
            )
        if label and row.asset_label and row.asset_label != label:
            return "conflict", tr(
                "already assigned to {asset_label}", asset_label=repr(row.asset_label)
            )
    return "update", ""


async def preview_import(
    session: AsyncSession,
    org_id: UUID,
    *,
    content: bytes,
    mapping: dict[str, str],
) -> ImportPreview:
    """§8.1 step one. **WRITES NOTHING** -- asserted by row counts before and
    after, not by reading the code."""
    mapping = _validate_mapping(mapping)
    raw = _decode(content)
    vrf_id = await ensure_default_vrf(session, org_id)
    existing = await _existing(session, org_id, vrf_id)

    rows: list[ImportRow] = []
    counts = {"create": 0, "update": 0, "conflict": 0, "error": 0}
    for index, source in enumerate(raw, start=2):  # line 1 is the header
        values = _project(source, mapping)
        error = _validate_row(values)
        if error:
            rows.append(
                ImportRow(line=index, action="error", address=values.get("address", ""),
                          error=error)
            )
            counts["error"] += 1
            continue
        address = str(parse_address(values["address"]))
        action, note = _classify(values, existing.get(address))
        rows.append(
            ImportRow(line=index, action=action, address=address, error=note,
                      detail={"asset_label": values.get("asset_label", "")})
        )
        counts[action] += 1

    return ImportPreview(
        digest=preview_digest(content, mapping),
        total_rows=len(raw),
        creates=counts["create"],
        updates=counts["update"],
        conflicts=counts["conflict"],
        errors=counts["error"],
        rows=rows,
    )


async def apply_import(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID | None,
    content: bytes,
    mapping: dict[str, str],
    digest: str,
) -> ImportResult:
    """§8.1 step two: the SAME file, the SAME mapping, in ONE transaction.

    The caller's `org_session` is the transaction; this function never commits,
    so a failure anywhere leaves zero rows written rather than a sheet half
    applied. Audited `ipam.import_applied` with the row counts, the mapping and
    the file's sha256, so the import is reproducible from the audit log alone.

    **WHAT `digest` PROVES AND WHAT IT DOES NOT.** It is recomputed here over
    the bytes and the mapping of THIS request, so it catches a caller whose
    digest describes something other than what it is now sending -- a stale
    token, a re-mapped set of columns, a different sheet. It does not and cannot
    prove that `preview` was ever called: no preview is persisted, so a caller
    that skipped step one computes this value itself. See the module docstring
    for why that trade was taken rather than closed.

    An imported address with a claimant lands `state='assigned'`,
    `origin='imported'` (§8.1); one without lands `reserved`, because "imported
    and assigned to nothing" is exactly the state §2.6's exactly-one-claimant
    CHECK exists to keep out of a plan of record.
    """
    # THE MAPPING IS VALIDATED BEFORE IT IS HASHED. `preview_digest` is total
    # over any dict, so hashing first would let a mapping naming `state` produce
    # a matching digest and be refused one line later -- the same refusal, but
    # reached by a path where an unvalidated value had already been folded into
    # a security-shaped comparison.
    mapping = _validate_mapping(mapping)
    expected = preview_digest(content, mapping)
    if digest != expected:
        raise IpamValidationError(
            tr(
                "the digest does not match this file and mapping: preview and apply must "
                "be about the same file AND the same column mapping (§8.1)"
            )
        )
    actual = file_digest(content)
    preview = await preview_import(session, org_id, content=content, mapping=mapping)
    raw = _decode(content)

    vrf_id = await ensure_default_vrf(session, org_id)
    existing = await _existing(session, org_id, vrf_id)

    applied = 0
    for row, source in zip(preview.rows, raw, strict=True):
        if row.action in ("error", "conflict"):
            continue
        values = _project(source, mapping)
        label = values.get("asset_label", "")
        method = values.get("allocation_method", "") or "unknown"
        current = existing.get(row.address)
        if current is not None and current.state == "assigned":
            # ALREADY DOCUMENTED AND UNCONTESTED (`_classify` returned `update`,
            # so the claimant matches or there is none in the sheet). Only the
            # DESCRIPTIVE fields move: re-assigning would be a state transition,
            # and §0.6 reserves `state` for a human decision made through an
            # audited transition rather than for a spreadsheet re-upload.
            fields = {
                key: values[key]
                for key in ("hostname", "description", "asset_label", "allocation_method")
                if values.get(key)
            }
            if fields:
                await update_address(session, org_id, actor_id, current.id, fields)
            current.origin = "imported"
            session.add(current)
            applied += 1
            continue
        if label:
            await assign(
                session,
                org_id,
                actor_id,
                address=row.address,
                asset_label=label,
                hostname=values.get("hostname", ""),
                description=values.get("description", ""),
                mac=values.get("mac") or None,
                allocation_method=method,
            )
        else:
            await reserve(
                session,
                org_id,
                actor_id,
                address=row.address,
                hostname=values.get("hostname", ""),
                description=values.get("description", ""),
            )
        written = (
            await session.execute(
                select(IpamAddress).where(
                    IpamAddress.org_id == org_id,
                    IpamAddress.address == row.address,
                )
            )
        ).scalar_one()
        written.origin = "imported"
        session.add(written)
        applied += 1
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ipam.import_applied",
        target_type="organization",
        target_id=str(org_id),
        metadata={
            # THE FILE, so an operator can re-hash the sheet they still have and
            # match it against this row. `preview_digest` is recorded beside it
            # rather than instead of it: it is the value the request carried,
            # and it identifies the file only in combination with the mapping,
            # which is on the next line anyway.
            "file_digest": actual,
            "preview_digest": expected,
            "total_rows": preview.total_rows,
            "applied": applied,
            "creates": preview.creates,
            "updates": preview.updates,
            "conflicts": preview.conflicts,
            "errors": preview.errors,
            "mapping": dict(mapping),
        },
    )
    await session.flush()
    return ImportResult(
        digest=expected,
        total_rows=preview.total_rows,
        applied=applied,
        errors=preview.errors,
        rows=preview.rows,
    )


# --------------------------------------------------------------------------- #
# §8.3 -- the documentation `network`-record converter
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConvertedRecord:
    record_id: UUID
    name: str
    prefix: str
    subnet_id: UUID | None
    vlan_id: UUID | None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkippedRecord:
    record_id: UUID
    name: str
    reason: str


@dataclass(frozen=True)
class DocRecordConversion:
    dry_run: bool
    converted: list[ConvertedRecord]
    skipped: list[SkippedRecord]


async def convert_doc_records(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID | None,
    dry_run: bool = True,
) -> DocRecordConversion:
    """§8.3. `dry_run=True` BY DEFAULT, and the default is the safe one.

    Converts each non-archived `network` record whose `fields.cidr` parses,
    using `ip_network(..., strict=False)` -- the same call
    `NetworkFields._valid_cidr` already makes, so a record the documentation
    module accepted is a record this converts. The source is then **archived,
    not deleted**: `doc_versions.record_id` is a real foreign key and
    `doc_links` point at the row.

    Rows that do not convert come back in `skipped` **with reasons**. Nothing is
    guessed: a gateway outside its prefix is dropped with a warning rather than
    silently rewritten, and a VLAN id that names no VLAN in this org leaves
    `vlan_id` null and is reported.
    """
    records = (
        (
            await session.execute(
                select(DocRecord).where(
                    DocRecord.org_id == org_id,
                    DocRecord.record_type == "network",
                    DocRecord.archived.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    vlans = {
        row.vid: row.id
        for row in (
            (await session.execute(select(IpamVlan).where(IpamVlan.org_id == org_id)))
            .scalars()
            .all()
        )
    }
    converted: list[ConvertedRecord] = []
    skipped: list[SkippedRecord] = []

    for record in records:
        fields = record.fields if isinstance(record.fields, dict) else {}
        raw_cidr = fields.get("cidr")
        if not raw_cidr:
            skipped.append(SkippedRecord(record.id, record.name, tr("no cidr field")))
            continue
        try:
            prefix = ip_network(str(raw_cidr), strict=False)
        except ValueError as exc:
            skipped.append(
                SkippedRecord(
                    record.id, record.name, tr("unparseable cidr: {exc}", exc=exc)
                )
            )
            continue

        warnings: list[str] = []
        vlan_id: UUID | None = None
        raw_vlan = fields.get("vlan")
        if raw_vlan not in (None, ""):
            try:
                vlan_id = vlans.get(int(raw_vlan))
            except (TypeError, ValueError):
                vlan_id = None
            if vlan_id is None:
                warnings.append(
                    tr(
                        "no VLAN with vid {raw_vlan} in this org; vlan_id left null",
                        raw_vlan=raw_vlan,
                    )
                )

        gateway: str | None = None
        raw_gateway = str(fields.get("gateway") or "").strip()
        if raw_gateway:
            parsed = parse_address(raw_gateway)
            if parsed is None or parsed.version != prefix.version or parsed not in prefix:
                warnings.append(
                    tr(
                        "gateway {raw_gateway} is not inside {prefix}; left null",
                        raw_gateway=repr(raw_gateway),
                        prefix=prefix,
                    )
                )
            else:
                gateway = str(parsed)

        dns = [
            entry.strip()
            for entry in str(fields.get("dns") or "").split(",")
            if entry.strip()
        ]

        if dry_run:
            converted.append(
                ConvertedRecord(record.id, record.name, str(prefix), None, vlan_id, warnings)
            )
            continue
        try:
            subnet = await create_subnet(
                session,
                org_id,
                actor_id,
                prefix=str(prefix),
                kind="assignable",
                name=record.name,
                vlan_id=vlan_id,
                gateway=gateway,
                dns_servers=dns,
                doc_record_id=record.id,
            )
        except IpamValidationError as exc:
            skipped.append(SkippedRecord(record.id, record.name, str(exc)))
            continue
        # ARCHIVED, NOT DELETED: `doc_versions.record_id` is a real FK and
        # `doc_links` point here.
        record.archived = True
        session.add(record)
        converted.append(
            ConvertedRecord(record.id, record.name, str(prefix), subnet.id, vlan_id, warnings)
        )

    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ipam.import_previewed" if dry_run else "ipam.import_applied",
        target_type="organization",
        target_id=str(org_id),
        metadata={
            "source": "doc_records",
            "dry_run": dry_run,
            "converted": len(converted),
            "skipped": len(skipped),
        },
    )
    await session.flush()
    return DocRecordConversion(dry_run=dry_run, converted=converted, skipped=skipped)


# --------------------------------------------------------------------------- #
# §8.2 -- exports
# --------------------------------------------------------------------------- #


def _render(header: list[str], rows: list[list[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


async def export_subnets_csv(session: AsyncSession, org_id: UUID) -> str:
    """Flat, Power-BI-friendly columns. `name` and `description` are OPERATOR
    FREE TEXT and are escaped; `prefix`, `kind`, the UUIDs and the timestamps are
    server-formatted from typed columns and are not."""
    rows = (
        (
            await session.execute(
                select(IpamSubnet)
                .where(IpamSubnet.org_id == org_id)
                .order_by(IpamSubnet.prefix)
            )
        )
        .scalars()
        .all()
    )
    return _render(
        ["prefix", "kind", "name", "description", "vrf_id", "site_id", "vlan_id",
         "gateway", "sweep_enabled", "sweep_interval_hours", "last_swept_at"],
        [
            [
                str(row.prefix),
                row.kind,
                escape_formula(row.name),
                escape_formula(row.description),
                str(row.vrf_id),
                str(row.site_id) if row.site_id else "",
                str(row.vlan_id) if row.vlan_id else "",
                str(row.gateway) if row.gateway else "",
                row.sweep_enabled,
                row.sweep_interval_hours if row.sweep_interval_hours is not None else "",
                row.last_swept_at.isoformat() if row.last_swept_at else "",
            ]
            for row in rows
        ],
    )


async def export_addresses_csv(session: AsyncSession, org_id: UUID) -> str:
    """`hostname`, `asset_label` and `description` are operator- or
    agent-supplied free text. `state`, `origin` and `allocation_method` are
    CHECK-constrained enums and `mac` is a `macaddr` -- none can begin with a
    dangerous character, and escaping them would only corrupt them for a machine
    reader."""
    rows = (
        (
            await session.execute(
                select(IpamAddress)
                .where(IpamAddress.org_id == org_id)
                .order_by(IpamAddress.address)
            )
        )
        .scalars()
        .all()
    )
    return _render(
        ["address", "state", "origin", "allocation_method", "device_id",
         "asset_label", "mac", "hostname", "description", "subnet_id",
         "assigned_at", "released_at"],
        [
            [
                str(row.address),
                row.state,
                row.origin,
                row.allocation_method,
                str(row.device_id) if row.device_id else "",
                escape_formula(row.asset_label),
                row.mac or "",
                escape_formula(row.hostname),
                escape_formula(row.description),
                str(row.subnet_id) if row.subnet_id else "",
                row.assigned_at.isoformat() if row.assigned_at else "",
                row.released_at.isoformat() if row.released_at else "",
            ]
            for row in rows
        ],
    )


async def export_drift_csv(
    session: AsyncSession, org_id: UUID, *, include_resolved: bool = False
) -> str:
    """**`dormant` IS EXCLUDED** (§4.3): it is not drift, and an export called
    "drift" that contained it would be the mixed list §0.11 says nobody opens
    twice. The filter is `DRIFT_CLASSES`, the same tuple `list_drift` uses, so
    the page and the export cannot disagree.

    `note` is operator free text. `detail` is a jsonb blob carrying MAC
    addresses and hostnames an AGENT supplied, so it is escaped too -- it is
    rendered as text into one cell, and "the server built this string" is not
    the same as "the server chose its contents".
    """
    query = select(IpamDrift).where(
        IpamDrift.org_id == org_id, IpamDrift.drift_class.in_(DRIFT_CLASSES)
    )
    if not include_resolved:
        query = query.where(IpamDrift.resolved_at.is_(None))
    rows = (
        (await session.execute(query.order_by(IpamDrift.address, IpamDrift.drift_class)))
        .scalars()
        .all()
    )
    return _render(
        ["address", "drift_class", "severity", "confidence", "subnet_id", "device_id",
         "first_detected_at", "last_evaluated_at", "consecutive_detections",
         "resolved_at", "acknowledged_at", "suppressed_until", "note", "detail"],
        [
            [
                str(row.address),
                row.drift_class,
                row.severity,
                row.confidence,
                str(row.subnet_id) if row.subnet_id else "",
                str(row.device_id) if row.device_id else "",
                row.first_detected_at.isoformat(),
                row.last_evaluated_at.isoformat(),
                row.consecutive_detections,
                row.resolved_at.isoformat() if row.resolved_at else "",
                row.acknowledged_at.isoformat() if row.acknowledged_at else "",
                row.suppressed_until.isoformat() if row.suppressed_until else "",
                escape_formula(row.note),
                # NOT escaped, and the reason is the guard's own rule: CSV
                # injection is about the LEADING character of a cell, and a JSON
                # object literal always begins with `{`. The agent-supplied MACs
                # and hostnames inside it can never be leading, and prefixing a
                # machine-readable JSON cell with `'` would corrupt it for the
                # reader this column exists for.
                json.dumps(dict(row.detail or {}), sort_keys=True),
            ]
            for row in rows
        ],
    )
