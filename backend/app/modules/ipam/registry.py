"""VRF, site and VLAN registry (`docs/modules/ipam/spec.md` §2.1-§2.3, §0.7,
§0.15, §11.3).

The three tables everything else in this module points at. Nothing here is
interesting on its own; three properties of it are.

**`ensure_default_vrf` IS AN UPSERT, NOT A CHECK-THEN-INSERT.** §0.7 makes
`ipam_subnet.vrf_id` and `ipam_address.vrf_id` `NOT NULL` with a `global` row
created lazily on first use, precisely because a nullable scoping column
produces two uniqueness rules instead of one -- `NULL` is distinct from `NULL`
in a unique index, so `(org, NULL, 10.0.0.0/24)` is insertable twice and the
duplicate-prefix bug `uq_ipam_subnet_org_vrf_prefix` exists to prevent walks in
through the default case. That default row is therefore created on the first
subnet write of an org, which is exactly when two concurrent writes are most
likely: `SELECT ... IF NOT FOUND INSERT` is a race both of them lose. This
project has run two sweeps to remove that bug class, so this one is
`ON CONFLICT DO NOTHING` + `SELECT`, and the concurrency is tested rather than
argued.

**`ensure_default_vrf` DELIBERATELY WRITES NO AUDIT EVENT, and that is a lock
ordering decision, not an omission.** `audit_service.record` takes
`pg_advisory_xact_lock(hashtext('audit:' || org))` and holds it to commit;
`subnets.create_subnet` takes `pg_advisory_xact_lock(hashtext('ipam:' || org ||
':' || vrf))` and then audits. `ensure_default_vrf` necessarily runs BEFORE the
`ipam:` lock, since the lock key contains the VRF id it returns. Auditing here
would therefore make one transaction take `audit:` then `ipam:`, while a
concurrent subnet create in an org that already has its default VRF takes
`ipam:` then `audit:` -- a textbook lock-order inversion, and Postgres resolves
those by aborting somebody. The `global` row is a schema-mandated default, its
creation is a side effect of a subnet write that IS audited and that carries the
VRF, and an operator who creates a VRF deliberately goes through `create_vrf`,
which audits.

**VLAN uniqueness is a PARTIAL INDEX PAIR (§2.3, §0.15) and every write against
it infers.** `ON CONFLICT ON CONSTRAINT` fails at RUNTIME against a partial
unique index -- there is no `pg_constraint` row to name -- so `create_vlan`
targets `index_elements=` plus `index_where=`, with the predicate written
EXACTLY as migration 0043 wrote it: Postgres infers the matching partial index
from the column list plus the predicate, and a differently-shaped-but-equivalent
predicate infers nothing.

**EVERY QUERY CARRIES AN EXPLICIT `org_id` PREDICATE**, in addition to RLS
(invariant #5). RLS masks an application predicate: a cross-org test driven
through `org_session` passes even when the predicate has been deleted, because
RLS rejects first. The predicates here are pinned on the ADMIN engine by
`tests/test_ipam_registry.py`.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import DocRecord
from app.modules.ipam.errors import (
    IpamConflictError,
    IpamNotFoundError,
    IpamValidationError,
)
from app.modules.ipam.models import (
    IpamAddress,
    IpamDrift,
    IpamObservation,
    IpamScanAuthorization,
    IpamSite,
    IpamSubnet,
    IpamVlan,
    IpamVrf,
)

__all__ = [
    "DEFAULT_VRF_NAME",
    "SITE_EDITABLE_FIELDS",
    "VLAN_EDITABLE_FIELDS",
    "VRF_EDITABLE_FIELDS",
    "create_site",
    "create_vlan",
    "create_vrf",
    "delete_site",
    "delete_vlan",
    "delete_vrf",
    "ensure_default_vrf",
    "get_site",
    "get_vlan",
    "get_vrf",
    "list_sites",
    "list_vlans",
    "list_vrfs",
    "update_site",
    "update_vlan",
    "update_vrf",
]

#: §0.7. Named, not spelled out at three call sites.
DEFAULT_VRF_NAME = "global"

VRF_EDITABLE_FIELDS = ("name", "rd", "description")
SITE_EDITABLE_FIELDS = ("name", "doc_record_id", "description")
VLAN_EDITABLE_FIELDS = ("site_id", "vid", "name", "description")

_MAX_VRF_NAME = 100
_MAX_VRF_RD = 64
_MAX_SITE_NAME = 200
_MAX_VLAN_NAME = 100

# The partial pair from §2.3, keyed by whether the VLAN is site-scoped. The
# `index_where` strings must match migration 0043's predicates CHARACTER FOR
# CHARACTER -- inference is by column list plus predicate, and an equivalent
# predicate written differently infers nothing and raises at runtime.
_VLAN_INDEX_BY_SCOPE: dict[bool, tuple[str, list[str], str]] = {
    True: ("uq_ipam_vlan_org_site_vid", ["org_id", "site_id", "vid"], "site_id IS NOT NULL"),
    False: ("uq_ipam_vlan_org_vid_global", ["org_id", "vid"], "site_id IS NULL"),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _audit(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    action: str,
    target_type: str,
    target_id: UUID,
    metadata: dict[str, Any],
) -> None:
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        metadata=metadata,
    )
    await session.flush()


def _validate_text(value: Any, field: str, maximum: int, *, required: bool = True) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or (required and not value.strip()):
        raise IpamValidationError(tr("{field} must be a non-empty string", field=field))
    if len(value) > maximum:
        raise IpamValidationError(
            tr("{field} must be at most {maximum} characters", field=field, maximum=maximum)
        )


def _reject_unknown(fields: dict[str, Any], editable: tuple[str, ...], noun: str) -> None:
    """A key outside the editable set is REFUSED, not ignored.

    Silently dropping it returns 200 for a typo'd body having changed nothing,
    and audits a no-op edit.
    """
    if not fields:
        raise IpamValidationError(tr("no fields supplied"))
    unknown = sorted(set(fields) - set(editable))
    if unknown:
        raise IpamValidationError(
            tr(
                "unknown or server-owned {noun} field(s): {unknown}; editable fields are "
                "{editable}",
                noun=noun,
                unknown=unknown,
                editable=list(editable),
            )
        )


# ---------------------------------------------------------------------------
# VRFs
# ---------------------------------------------------------------------------


async def get_vrf(session: AsyncSession, org_id: UUID, vrf_id: UUID) -> IpamVrf:
    """Org-scoped load. A VRF id from another org is indistinguishable from a
    nonexistent one -- both raise `IpamNotFoundError` (404, never 403)."""
    vrf = (
        await session.execute(
            select(IpamVrf).where(IpamVrf.org_id == org_id, IpamVrf.id == vrf_id)
        )
    ).scalar_one_or_none()
    if vrf is None:
        raise IpamNotFoundError(tr("VRF {vrf_id} not found", vrf_id=vrf_id))
    return vrf


async def list_vrfs(session: AsyncSession, org_id: UUID) -> list[IpamVrf]:
    return list(
        (
            await session.execute(
                select(IpamVrf).where(IpamVrf.org_id == org_id).order_by(IpamVrf.name)
            )
        )
        .scalars()
        .all()
    )


async def ensure_default_vrf(session: AsyncSession, org_id: UUID) -> UUID:
    """The org's `global` VRF, created lazily on first use (§0.7).

    `ON CONFLICT DO NOTHING` + `SELECT`, NEVER check-then-insert: this runs on
    the first subnet write of an org, which is exactly the moment two concurrent
    writes both miss an in-memory pre-check and one of them gets an
    `IntegrityError` in a code path that has nothing to do with duplicate names.

    Inference is on `uq_ipam_vrf_org_name`, a total unique CONSTRAINT (both
    columns are `NOT NULL`), so plain `index_elements` is enough and no
    `index_where` is involved -- unlike the VLAN pair below.

    No audit event; see this module's docstring for why (it is a lock-ordering
    decision, not an omission).
    """
    now = _utcnow()
    await session.execute(
        pg_insert(IpamVrf)
        .values(
            org_id=org_id,
            name=DEFAULT_VRF_NAME,
            description="Default routing domain, created automatically.",
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["org_id", "name"])
    )
    vrf_id = (
        await session.execute(
            select(IpamVrf.id).where(
                IpamVrf.org_id == org_id, IpamVrf.name == DEFAULT_VRF_NAME
            )
        )
    ).scalar_one_or_none()
    if vrf_id is None:  # pragma: no cover -- the insert above just guaranteed it
        raise IpamValidationError(tr("could not create the default VRF"))
    return vrf_id


async def create_vrf(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    *,
    name: str,
    rd: str | None = None,
    description: str = "",
) -> IpamVrf:
    _validate_text(name, "name", _MAX_VRF_NAME)
    _validate_text(rd, "rd", _MAX_VRF_RD, required=False)

    now = _utcnow()
    inserted_id = (
        await session.execute(
            pg_insert(IpamVrf)
            .values(
                org_id=org_id,
                name=name,
                rd=rd,
                description=description,
                created_by=actor_id,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_ipam_vrf_org_name")
            .returning(IpamVrf.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise IpamConflictError(
            tr("a VRF named {name} already exists in this org", name=repr(name))
        )

    vrf = await get_vrf(session, org_id, inserted_id)
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.vrf_created",
        "ipam_vrf",
        vrf.id,
        {"name": name, "rd": rd},
    )
    return vrf


async def update_vrf(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    vrf_id: UUID,
    fields: dict[str, Any],
) -> IpamVrf:
    _reject_unknown(fields, VRF_EDITABLE_FIELDS, tr("VRF"))
    vrf = await get_vrf(session, org_id, vrf_id)
    merged = {field: getattr(vrf, field) for field in VRF_EDITABLE_FIELDS} | fields
    _validate_text(merged["name"], "name", _MAX_VRF_NAME)
    _validate_text(merged["rd"], "rd", _MAX_VRF_RD, required=False)

    if "name" in fields and fields["name"] != vrf.name:
        await _refuse_duplicate_name(session, IpamVrf, org_id, str(fields["name"]), tr("VRF"))
    previous = {field: getattr(vrf, field) for field in fields}
    for field, value in fields.items():
        setattr(vrf, field, value)
    vrf.updated_at = _utcnow()
    await session.flush()
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.vrf_updated",
        "ipam_vrf",
        vrf.id,
        {"from": _jsonable(previous), "to": _jsonable(fields)},
    )
    return vrf


async def delete_vrf(
    session: AsyncSession, org_id: UUID, actor_id: UUID | None, vrf_id: UUID
) -> None:
    """Refused while anything still points at it, with the referents NAMED.

    Not by letting the FK raise: an `IntegrityError` surfaces as a 500 whose
    text names a constraint, which tells an operator nothing about which
    subnets are in the way.
    """
    vrf = await get_vrf(session, org_id, vrf_id)
    await _refuse_if_referenced(
        session,
        org_id,
        tr("VRF"),
        vrf.name,
        [
            (IpamSubnet, IpamSubnet.vrf_id, "subnet"),
            (IpamAddress, IpamAddress.vrf_id, "address"),
            (IpamObservation, IpamObservation.vrf_id, "observation"),
            (IpamDrift, IpamDrift.vrf_id, "drift finding"),
            (IpamScanAuthorization, IpamScanAuthorization.vrf_id, "scan authorization"),
        ],
        vrf_id,
    )
    await session.delete(vrf)
    await session.flush()
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.vrf_deleted",
        "ipam_vrf",
        vrf_id,
        {"name": vrf.name},
    )


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


async def get_site(session: AsyncSession, org_id: UUID, site_id: UUID) -> IpamSite:
    site = (
        await session.execute(
            select(IpamSite).where(IpamSite.org_id == org_id, IpamSite.id == site_id)
        )
    ).scalar_one_or_none()
    if site is None:
        raise IpamNotFoundError(tr("site {site_id} not found", site_id=site_id))
    return site


async def list_sites(session: AsyncSession, org_id: UUID) -> list[IpamSite]:
    return list(
        (
            await session.execute(
                select(IpamSite).where(IpamSite.org_id == org_id).order_by(IpamSite.name)
            )
        )
        .scalars()
        .all()
    )


async def _validate_doc_record(
    session: AsyncSession, org_id: UUID, doc_record_id: UUID | None
) -> None:
    """§2.2's optional link to a documentation `location` record, resolved IN
    THIS ORG.

    Checked in the service rather than left to the FK: the FK is org-blind, so
    a cross-org `doc_record_id` would be accepted by the database and would
    quietly join one tenant's site to another tenant's document. RLS does not
    save this either -- referential-integrity checks run as the referenced
    table's owner.
    """
    if doc_record_id is None:
        return
    found = (
        await session.execute(
            select(DocRecord.id).where(
                DocRecord.org_id == org_id, DocRecord.id == doc_record_id
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise IpamValidationError(
            tr("documentation record {doc_record_id} not found", doc_record_id=doc_record_id)
        )


async def create_site(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    *,
    name: str,
    doc_record_id: UUID | None = None,
    description: str = "",
) -> IpamSite:
    _validate_text(name, "name", _MAX_SITE_NAME)
    await _validate_doc_record(session, org_id, doc_record_id)

    now = _utcnow()
    inserted_id = (
        await session.execute(
            pg_insert(IpamSite)
            .values(
                org_id=org_id,
                name=name,
                doc_record_id=doc_record_id,
                description=description,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_ipam_site_org_name")
            .returning(IpamSite.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise IpamConflictError(
            tr("a site named {name} already exists in this org", name=repr(name))
        )

    site = await get_site(session, org_id, inserted_id)
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.site_created",
        "ipam_site",
        site.id,
        {"name": name, "doc_record_id": str(doc_record_id) if doc_record_id else None},
    )
    return site


async def update_site(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    site_id: UUID,
    fields: dict[str, Any],
) -> IpamSite:
    _reject_unknown(fields, SITE_EDITABLE_FIELDS, tr("site"))
    site = await get_site(session, org_id, site_id)
    merged = {field: getattr(site, field) for field in SITE_EDITABLE_FIELDS} | fields
    _validate_text(merged["name"], "name", _MAX_SITE_NAME)
    if "doc_record_id" in fields:
        await _validate_doc_record(session, org_id, fields["doc_record_id"])
    if "name" in fields and fields["name"] != site.name:
        await _refuse_duplicate_name(session, IpamSite, org_id, str(fields["name"]), tr("site"))

    previous = {field: getattr(site, field) for field in fields}
    for field, value in fields.items():
        setattr(site, field, value)
    site.updated_at = _utcnow()
    await session.flush()
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.site_updated",
        "ipam_site",
        site.id,
        {"from": _jsonable(previous), "to": _jsonable(fields)},
    )
    return site


async def delete_site(
    session: AsyncSession, org_id: UUID, actor_id: UUID | None, site_id: UUID
) -> None:
    site = await get_site(session, org_id, site_id)
    await _refuse_if_referenced(
        session,
        org_id,
        tr("site"),
        site.name,
        [
            (IpamSubnet, IpamSubnet.site_id, "subnet"),
            (IpamVlan, IpamVlan.site_id, "VLAN"),
        ],
        site_id,
    )
    await session.delete(site)
    await session.flush()
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.site_deleted",
        "ipam_site",
        site_id,
        {"name": site.name},
    )


# ---------------------------------------------------------------------------
# VLANs
# ---------------------------------------------------------------------------


async def get_vlan(session: AsyncSession, org_id: UUID, vlan_id: UUID) -> IpamVlan:
    vlan = (
        await session.execute(
            select(IpamVlan).where(IpamVlan.org_id == org_id, IpamVlan.id == vlan_id)
        )
    ).scalar_one_or_none()
    if vlan is None:
        raise IpamNotFoundError(tr("VLAN {vlan_id} not found", vlan_id=vlan_id))
    return vlan


async def list_vlans(
    session: AsyncSession, org_id: UUID, *, site_id: UUID | None = None
) -> list[IpamVlan]:
    query = select(IpamVlan).where(IpamVlan.org_id == org_id)
    if site_id is not None:
        query = query.where(IpamVlan.site_id == site_id)
    return list(
        (await session.execute(query.order_by(IpamVlan.vid, IpamVlan.site_id)))
        .scalars()
        .all()
    )


def _validate_vid(vid: Any) -> None:
    if not isinstance(vid, int) or isinstance(vid, bool):
        raise IpamValidationError(tr("vid must be an integer; got {vid}", vid=repr(vid)))
    if not 1 <= vid <= 4094:
        raise IpamValidationError(tr("vid must be between 1 and 4094; got {vid}", vid=vid))


async def create_vlan(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    *,
    vid: int,
    site_id: UUID | None = None,
    name: str = "",
    description: str = "",
) -> IpamVlan:
    """Insert against ONE of the two partial unique indexes (§2.3, §0.15).

    Which one depends on whether the VLAN is site-scoped, and the pair is total
    between them: VLAN 10 exists once per site and once org-wide. `ON CONFLICT
    ON CONSTRAINT` would fail at runtime against either -- a partial unique
    index has no `pg_constraint` row to name -- so this infers, and the
    inference is exercised by a test rather than assumed from the DDL.
    """
    _validate_vid(vid)
    _validate_text(name, "name", _MAX_VLAN_NAME, required=False)
    if site_id is not None:
        await get_site(session, org_id, site_id)  # in-org, 404 otherwise

    _, index_elements, index_where = _VLAN_INDEX_BY_SCOPE[site_id is not None]
    now = _utcnow()
    inserted_id = (
        await session.execute(
            pg_insert(IpamVlan)
            .values(
                org_id=org_id,
                site_id=site_id,
                vid=vid,
                name=name,
                description=description,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=index_elements, index_where=text(index_where)
            )
            .returning(IpamVlan.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        scope = (
            tr("at site {site_id}", site_id=site_id)
            if site_id is not None
            else tr("org-wide")
        )
        raise IpamConflictError(tr("VLAN {vid} already exists {scope}", vid=vid, scope=scope))

    vlan = await get_vlan(session, org_id, inserted_id)
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.vlan_created",
        "ipam_vlan",
        vlan.id,
        {"vid": vid, "site_id": str(site_id) if site_id else None, "name": name},
    )
    return vlan


async def update_vlan(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    vlan_id: UUID,
    fields: dict[str, Any],
) -> IpamVlan:
    _reject_unknown(fields, VLAN_EDITABLE_FIELDS, tr("VLAN"))
    vlan = await get_vlan(session, org_id, vlan_id)
    merged = {field: getattr(vlan, field) for field in VLAN_EDITABLE_FIELDS} | fields
    _validate_vid(merged["vid"])
    _validate_text(merged["name"], "name", _MAX_VLAN_NAME, required=False)
    new_site_id: UUID | None = merged["site_id"]
    if "site_id" in fields and new_site_id is not None:
        await get_site(session, org_id, new_site_id)

    # Moving a VLAN between sites, or from a site to org-wide, moves it between
    # the two partial indexes -- so the duplicate check has to be made against
    # the index the row is moving TO, not the one it currently sits under.
    if (merged["vid"], new_site_id) != (vlan.vid, vlan.site_id):
        await _refuse_duplicate_vlan(session, org_id, int(merged["vid"]), new_site_id)

    previous = {field: getattr(vlan, field) for field in fields}
    for field, value in fields.items():
        setattr(vlan, field, value)
    vlan.updated_at = _utcnow()
    await session.flush()
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.vlan_updated",
        "ipam_vlan",
        vlan.id,
        {"from": _jsonable(previous), "to": _jsonable(fields)},
    )
    return vlan


async def delete_vlan(
    session: AsyncSession, org_id: UUID, actor_id: UUID | None, vlan_id: UUID
) -> None:
    vlan = await get_vlan(session, org_id, vlan_id)
    await _refuse_if_referenced(
        session,
        org_id,
        tr("VLAN"),
        str(vlan.vid),
        [(IpamSubnet, IpamSubnet.vlan_id, "subnet")],
        vlan_id,
    )
    await session.delete(vlan)
    await session.flush()
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.vlan_deleted",
        "ipam_vlan",
        vlan_id,
        {"vid": vlan.vid, "site_id": str(vlan.site_id) if vlan.site_id else None},
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _jsonable(fields: dict[str, Any]) -> dict[str, Any]:
    """Audit metadata is JSONB; a `UUID` is not JSON."""
    return {k: (str(v) if isinstance(v, UUID) else v) for k, v in fields.items()}


async def _refuse_duplicate_name(
    session: AsyncSession, model: Any, org_id: UUID, name: str, noun: str
) -> None:
    existing = (
        await session.execute(
            select(model.id).where(model.org_id == org_id, model.name == name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise IpamConflictError(
            tr("a {noun} named {name} already exists in this org", noun=noun, name=repr(name))
        )


async def _refuse_duplicate_vlan(
    session: AsyncSession, org_id: UUID, vid: int, site_id: UUID | None
) -> None:
    query = select(IpamVlan.id).where(IpamVlan.org_id == org_id, IpamVlan.vid == vid)
    # `site_id == None` compiles to `IS NULL`; `== site_id` with a UUID does not
    # match the NULL rows. The two branches are the two partial indexes.
    query = query.where(
        IpamVlan.site_id.is_(None) if site_id is None else IpamVlan.site_id == site_id
    )
    if (await session.execute(query)).scalar_one_or_none() is not None:
        scope = (
            tr("at site {site_id}", site_id=site_id)
            if site_id is not None
            else tr("org-wide")
        )
        raise IpamConflictError(tr("VLAN {vid} already exists {scope}", vid=vid, scope=scope))


def counted(count: int, noun: str) -> str:
    """A count and a noun (3 subnets, 1 VLAN) in the request's language.

    English forms its plural by adding "s"; Spanish does not, so every noun the
    registry counts is spelled out in both numbers.
    """
    one = count == 1
    if noun == "subnet":
        return tr("{count} subnet", count=count) if one else tr("{count} subnets", count=count)
    if noun == "VLAN":
        return tr("{count} VLAN", count=count) if one else tr("{count} VLANs", count=count)
    if noun == "address":
        return tr("{count} address", count=count) if one else tr("{count} addresses", count=count)
    if noun == "range":
        return tr("{count} range", count=count) if one else tr("{count} ranges", count=count)
    if noun == "observation":
        return (
            tr("{count} observation", count=count)
            if one
            else tr("{count} observations", count=count)
        )
    if noun == "drift finding":
        return (
            tr("{count} drift finding", count=count)
            if one
            else tr("{count} drift findings", count=count)
        )
    if noun == "scan authorization":
        return (
            tr("{count} scan authorization", count=count)
            if one
            else tr("{count} scan authorizations", count=count)
        )
    if noun == "sweep run":
        return (
            tr("{count} sweep run", count=count) if one else tr("{count} sweep runs", count=count)
        )
    return f"{count} {noun}{'' if one else 's'}"


async def _refuse_if_referenced(
    session: AsyncSession,
    org_id: UUID,
    noun: str,
    label: str,
    referents: list[tuple[Any, Any, str]],
    target_id: UUID,
) -> None:
    """Refuse a delete while anything still points at the row, NAMING what.

    "Do not rely on the FK error text" (plan, Task 3): the database's message
    names a constraint, not the three subnets an operator has to move first.
    """
    blocking: list[str] = []
    for model, column, singular in referents:
        count = (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(model.org_id == org_id, column == target_id)
            )
        ).scalar_one()
        if count:
            blocking.append(counted(count, singular))
    if blocking:
        raise IpamConflictError(
            tr(
                "{noun} {label} is still referenced by {blocking}",
                noun=noun,
                label=repr(label),
                blocking=", ".join(blocking),
            )
        )
