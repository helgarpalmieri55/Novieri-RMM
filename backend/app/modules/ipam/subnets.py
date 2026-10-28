"""Subnets and ranges: containment, the derived hierarchy, and the serialised
overlap rule (`docs/modules/ipam/spec.md` §2.4, §2.5, §0.2, §0.3, §11.3).

**`parent_id` IS A DERIVED CACHE, NEVER SUPPLIED (§0.2).** It is recomputed on
every subnet write as "the smallest other prefix in the same VRF that contains
this one", and no create or update path accepts it. A hand-maintained hierarchy
can disagree with the addresses it describes -- somebody parents
`192.168.1.0/24` under `10.0.0.0/8` and the tree renders happily while every
containment query says otherwise. Deriving it makes that state unrepresentable.
The consequence, accepted in §0.2 and implemented in `recompute_parents`:
inserting a supernet BETWEEN existing rows re-parents its new children, so a
subnet write is not a single-row write. It is bounded (only rows contained by
the new prefix, in the same VRF) and it happens under the lock below.

**OVERLAP IS A SERVICE RULE UNDER AN ADVISORY LOCK, NOT AN `EXCLUDE`
CONSTRAINT (§0.3).** The check and the insert run together under
`pg_advisory_xact_lock(hashtext('ipam:' || org || ':' || vrf))` -- the same
serialisation technique `audit_service.record` uses to keep the hash chain
linear. Two concurrent overlapping inserts serialise; the second sees the first
and is refused. The guarantee is application-strength, not
constraint-strength: a direct SQL insert bypasses it, which is true of every
business rule in this codebase. The rules that must survive a bypass are the
TENANCY rules, and those are RLS.

**WHAT "OVERLAP" ACTUALLY MEANS HERE, because the spec's wording and Postgres's
`cidr` type do not quite meet.** §0.3 states the rule as "no two SIBLINGS
overlap". Two *canonical* CIDR prefixes cannot partially overlap -- they nest or
they are disjoint, and Postgres refuses a non-canonical one outright
(`'10.0.0.1/24'::cidr` is an error, "bits set to right of mask"). So a literal
reading of "two siblings overlap" describes a state this type system cannot
represent, and a check written to that letter is a no-op that no test can
falsify. The rule that is both real and constructible is the one §0.2's
`container`/`assignable` split already implies:

> **No two `assignable` subnets in one VRF may overlap.** At most one subnet
> that HOLDS ADDRESSES may cover any given address.

That is what everything downstream depends on -- `containing_subnet`,
utilisation, next-free, the sweep target set, orphan detection -- each of which
asks "which subnet holds this address" and must get ONE answer. A `container`
may nest freely with anything, in both directions: it is explicitly "a pure-
hierarchy prefix (a /8 that exists so the tree makes sense)" and holds no
addresses, so it cannot be the second answer to that question. Two equal
prefixes remain the database's job (`uq_ipam_subnet_org_vrf_prefix`).

**EVERY QUERY CARRIES AN EXPLICIT `org_id` PREDICATE** in addition to RLS
(invariant #5), and those predicates are pinned on the ADMIN engine by
`tests/test_ipam_subnets.py` -- through `org_session` RLS rejects first and a
test passes with the predicate deleted.

**A SUBNET IS ITS VRF'S CLIENT'S** (migration 0240, Annex A amended): the
trigger copies the client from the NOT NULL `vrf_id`. A create names a VRF, or
a client whose `global` VRF it goes into (`registry.vrf_for_new_row`); its
site and its VLAN are of that client, on create and on edit (422).
"""

from datetime import UTC, datetime
from ipaddress import ip_network
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import Device, DocRecord
from app.modules.ipam.clients import loaded_client, site_of_client
from app.modules.ipam.errors import (
    IpamClientError,
    IpamConflictError,
    IpamNotFoundError,
    IpamValidationError,
)
from app.modules.ipam.models import (
    IpamAddress,
    IpamDrift,
    IpamObservation,
    IpamRange,
    IpamSubnet,
    IpamSweepRun,
)
from app.modules.ipam.net import IPNetwork, parse_address
from app.modules.ipam.registry import counted, get_vlan, vrf_for_new_row

__all__ = [
    "RANGE_KINDS",
    "SUBNET_EDITABLE_FIELDS",
    "SUBNET_KINDS",
    "containing_subnet",
    "create_range",
    "create_subnet",
    "delete_range",
    "delete_subnet",
    "get_subnet",
    "list_ranges",
    "list_subnets",
    "lock_address_plan",
    "recompute_parents",
    "set_sweep_settings",
    "subnet_tree",
    "update_subnet",
]

SUBNET_KINDS = ("container", "assignable")
RANGE_KINDS = ("dhcp_pool", "reserved", "excluded")

#: `prefix`, `vrf_id` and `parent_id` are all absent, for three different
#: reasons. `parent_id` is DERIVED (§0.2) and a client-supplied value is exactly
#: the state deriving it exists to make unrepresentable. `prefix` and `vrf_id`
#: are the row's IDENTITY: `uq_ipam_subnet_org_vrf_prefix` is keyed on them, and
#: an "edit" that moves either is a different subnet -- delete and re-create it,
#: so the audit log says so.
SUBNET_EDITABLE_FIELDS = (
    "kind",
    "name",
    "description",
    "site_id",
    "vlan_id",
    "gateway",
    "dns_servers",
    "doc_record_id",
)

_MAX_NAME = 200


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
    *,
    client_id: UUID,
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
        client_id=client_id,
    )
    await session.flush()


async def lock_address_plan(session: AsyncSession, org_id: UUID, vrf_id: UUID) -> None:
    """§0.3's serialisation, per (org, VRF). PUBLIC: the allocator takes the
    SAME lock (§3.3), and a second implementation of the key -- a different
    prefix, a different hash input -- would be two locks that never contend.

    Transaction-scoped, so it releases at COMMIT with no unlock call to forget.
    The key is per-VRF rather than per-org so two customers' address plans do
    not serialise against each other; `audit_service.record` uses the identical
    technique with an `audit:` prefix, which is why the two never collide.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('ipam:' || :org || ':' || :vrf))"),
        {"org": str(org_id), "vrf": str(vrf_id)},
    )


def _parse_prefix(value: Any) -> IPNetwork:
    """Normalise to a canonical network, or raise a readable 400.

    `strict=False`, matching `documentation/schemas.py`'s validator and §8.3's
    converter: an operator typing `10.0.0.1/24` means the /24, and Postgres's
    `cidr` would refuse the literal outright ("bits set to right of mask") with
    a message about masks rather than about their subnet. The normalised value
    goes into the audit metadata, so the log records what was stored and not
    what was typed.
    """
    # `str(value)` rather than an `isinstance(value, str)` gate: asyncpg hands
    # a `cidr` column back as an `IPv4Network`/`IPv6Network`, so the reload
    # paths below (`update_subnet`, `create_range`) pass an object here, not
    # text. Refusing anything non-`str` would make editing a subnet's name
    # raise "prefix must be a string" about a prefix nobody touched.
    if value is None:
        raise IpamValidationError(tr("prefix must be a non-empty string"))
    literal = str(value).strip()
    if not literal:
        raise IpamValidationError(tr("prefix must be a non-empty string"))
    try:
        return ip_network(literal, strict=False)
    except ValueError as exc:
        raise IpamValidationError(
            tr("{literal} is not a valid prefix: {exc}", literal=repr(literal), exc=exc)
        ) from None


def _validate_gateway(gateway: Any, prefix: IPNetwork) -> str | None:
    """Belt and braces with `ck_ipam_subnet_gateway_in`.

    The CHECK is the thing that actually holds; this exists so an operator gets
    "gateway 192.168.1.1 is not inside 10.20.0.0/24" instead of a
    `ProgrammingError` surfacing as a 500.
    """
    if gateway is None or gateway == "":
        return None
    parsed = parse_address(str(gateway))
    if parsed is None:
        raise IpamValidationError(
            tr("gateway {gateway} is not a valid address", gateway=repr(gateway))
        )
    if parsed.version != prefix.version:
        raise IpamValidationError(
            tr(
                "gateway {parsed} is IPv{parsed_version} but the prefix is IPv{prefix_version}",
                parsed=parsed,
                parsed_version=parsed.version,
                prefix_version=prefix.version,
            )
        )
    if parsed not in prefix:
        raise IpamValidationError(
            tr("gateway {parsed} is not inside {prefix}", parsed=parsed, prefix=prefix)
        )
    return str(parsed)


async def _validate_references(
    session: AsyncSession,
    org_id: UUID,
    client_id: UUID,
    *,
    site_id: UUID | None,
    vlan_id: UUID | None,
    doc_record_id: UUID | None,
) -> None:
    """Every reference resolves IN THIS ORG, and the site and the VLAN are the
    subnet's client's (422; another client's the caller cannot see is a 404).
    The foreign keys are org-blind and referential-integrity checks run as the
    referenced table's owner, so RLS does not close this either; the site is
    also pinned by `fk_ipam_subnet_site_client`, the VLAN only here."""
    if site_id is not None:
        await site_of_client(session, org_id, site_id, client_id)
    if vlan_id is not None:
        vlan = await get_vlan(session, org_id, vlan_id)
        if vlan.client_id != client_id:
            raise IpamClientError(
                tr("VLAN {vlan_id} belongs to another client", vlan_id=vlan_id)
            )
    if doc_record_id is not None:
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


async def _refuse_overlap(
    session: AsyncSession,
    org_id: UUID,
    vrf_id: UUID,
    prefix: IPNetwork,
    kind: str,
    *,
    exclude_id: UUID | None = None,
) -> None:
    """No two `assignable` subnets in one VRF may overlap (see the module
    docstring for why the rule is stated this way).

    MUST be called with `lock_address_plan` already held: the check and the write that
    follows it are one critical section, and without the lock two concurrent
    transactions each read a snapshot in which the other's row does not exist
    yet, both pass, and both commit.

    `&&` ("contains or is contained by") is answered by
    `ix_ipam_subnet_prefix_gist`.
    """
    if kind != "assignable":
        return
    query = (
        select(IpamSubnet.id, IpamSubnet.prefix)
        .where(
            IpamSubnet.org_id == org_id,
            IpamSubnet.vrf_id == vrf_id,
            IpamSubnet.kind == "assignable",
            # `&&` is "contains or is contained by". The EQUAL case is
            # excluded deliberately: an exact duplicate is
            # `uq_ipam_subnet_org_vrf_prefix`'s job, and letting it fall
            # through to the insert produces "10.43.0.0/24 is already
            # registered in this VRF" instead of an overlap message telling an
            # operator to mark one of two identical prefixes a container.
            text(
                "ipam_subnet.prefix && CAST(:candidate AS cidr)"
                " AND ipam_subnet.prefix <> CAST(:candidate AS cidr)"
            ).bindparams(candidate=str(prefix)),
        )
        .limit(1)
    )
    if exclude_id is not None:
        query = query.where(IpamSubnet.id != exclude_id)
    clash = (await session.execute(query)).first()
    if clash is not None:
        raise IpamConflictError(
            tr(
                "{prefix} overlaps the assignable subnet {clash_prefix} in this VRF; at "
                "most one subnet holding addresses may cover an address -- mark the wider "
                "one kind='container' if it exists only to make the tree read",
                prefix=prefix,
                clash_prefix=clash.prefix,
            )
        )


async def recompute_parents(
    session: AsyncSession, org_id: UUID, vrf_id: UUID, prefix: IPNetwork | str
) -> None:
    """Re-derive `parent_id` for `prefix` and everything inside it (§0.2).

    ONE statement, and the row set it touches is bounded by `prefix` -- the only
    rows whose parent can change when a prefix is created or destroyed are the
    ones that prefix contains. `<<=` includes the row itself, which is what
    gives the newly inserted subnet its own parent in the same pass.

    The correlated subquery is "the smallest OTHER prefix in the same VRF that
    strictly contains this one": `>>` is strict containment, and the ordering is
    by `masklen` descending, so the longest -- i.e. most specific -- match wins.
    A subnet with no container above it gets `NULL`, which is a legal top-level
    row and not an error.
    """
    await session.execute(
        text(
            "UPDATE ipam_subnet s SET parent_id = ("
            "  SELECT p.id FROM ipam_subnet p"
            "  WHERE p.org_id = s.org_id AND p.vrf_id = s.vrf_id AND p.id <> s.id"
            "    AND p.prefix >> s.prefix"
            "  ORDER BY masklen(p.prefix) DESC LIMIT 1"
            ") WHERE s.org_id = :org AND s.vrf_id = :vrf"
            "  AND s.prefix <<= CAST(:prefix AS cidr)"
        ),
        {"org": str(org_id), "vrf": str(vrf_id), "prefix": str(prefix)},
    )


# ---------------------------------------------------------------------------
# Subnet CRUD
# ---------------------------------------------------------------------------


async def get_subnet(session: AsyncSession, org_id: UUID, subnet_id: UUID) -> IpamSubnet:
    """Org-scoped load. A subnet id from another org is indistinguishable from a
    nonexistent one -- both raise `IpamNotFoundError` (404, never 403)."""
    subnet = (
        await session.execute(
            select(IpamSubnet).where(
                IpamSubnet.org_id == org_id, IpamSubnet.id == subnet_id
            )
        )
    ).scalar_one_or_none()
    if subnet is None:
        raise IpamNotFoundError(tr("subnet {subnet_id} not found", subnet_id=subnet_id))
    return subnet


async def list_subnets(
    session: AsyncSession,
    org_id: UUID,
    *,
    vrf_id: UUID | None = None,
    site_id: UUID | None = None,
    vlan_id: UUID | None = None,
    contains: str | None = None,
    q: str | None = None,
) -> list[IpamSubnet]:
    """§9's `/subnets` filters. `contains=<ip>` is a containment query answered
    by the GiST index, not a text match -- which is the entire reason §0.1 chose
    native `cidr` (`10.4.70.0/24` matches `LIKE '10.4.7%'`)."""
    query = select(IpamSubnet).where(IpamSubnet.org_id == org_id)
    if vrf_id is not None:
        query = query.where(IpamSubnet.vrf_id == vrf_id)
    if site_id is not None:
        query = query.where(IpamSubnet.site_id == site_id)
    if vlan_id is not None:
        query = query.where(IpamSubnet.vlan_id == vlan_id)
    if contains:
        address = parse_address(contains)
        if address is None:
            raise IpamValidationError(
                tr("{contains} is not a valid address", contains=repr(contains))
            )
        query = query.where(
            text("ipam_subnet.prefix >>= CAST(:contains AS inet)").bindparams(
                contains=str(address)
            )
        )
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(
            IpamSubnet.name.ilike(pattern) | IpamSubnet.description.ilike(pattern)
        )
    return list(
        (await session.execute(query.order_by(IpamSubnet.prefix))).scalars().all()
    )


async def containing_subnet(
    session: AsyncSession, org_id: UUID, vrf_id: UUID, address: str
) -> IpamSubnet | None:
    """The MOST SPECIFIC subnet in `vrf_id` holding `address`, or `None`.

    "Most specific" is the whole point: an estate legitimately registers a /8 as
    a `container` and a /24 inside it as `assignable`, and an observation at
    `10.4.7.9` belongs to the /24. Returning the first match, or the widest,
    files every observation under the /8 and empties every subnet page.

    `None` rather than an exception when nothing contains it: an address inside
    no registered subnet is an ORPHAN (§4.2), which is not an error -- it is the
    "24 addresses in 10.7.3.x and no subnet registered" finding that becomes a
    one-click subnet registration.
    """
    parsed = parse_address(address)
    if parsed is None:
        raise IpamValidationError(tr("{address} is not a valid address", address=repr(address)))
    return (
        await session.execute(
            select(IpamSubnet)
            .where(
                IpamSubnet.org_id == org_id,
                IpamSubnet.vrf_id == vrf_id,
                text("ipam_subnet.prefix >>= CAST(:addr AS inet)").bindparams(
                    addr=str(parsed)
                ),
            )
            .order_by(text("masklen(ipam_subnet.prefix) DESC"))
            .limit(1)
        )
    ).scalars().first()


async def subnet_tree(
    session: AsyncSession, org_id: UUID, *, vrf_id: UUID | None = None
) -> list[dict[str, Any]]:
    """The derived hierarchy as nested dicts, ordered by prefix at every level.

    Built from `parent_id`, which is itself derived from containment -- so the
    tree cannot disagree with the addresses, which is §0.2's whole point.
    """
    rows = await list_subnets(session, org_id, vrf_id=vrf_id)
    nodes: dict[UUID, dict[str, Any]] = {
        row.id: {"subnet": row, "children": []} for row in rows
    }
    roots: list[dict[str, Any]] = []
    for row in rows:
        node = nodes[row.id]
        parent = nodes.get(row.parent_id) if row.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)
    return roots


async def create_subnet(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    *,
    prefix: str,
    vrf_id: UUID | None = None,
    client_id: UUID | None = None,
    kind: str = "assignable",
    name: str = "",
    description: str = "",
    site_id: UUID | None = None,
    vlan_id: UUID | None = None,
    gateway: str | None = None,
    dns_servers: list[str] | None = None,
    doc_record_id: UUID | None = None,
) -> IpamSubnet:
    """Register a prefix. NO `parent_id` PARAMETER, deliberately (§0.2).

    In `vrf_id`, whose client is the subnet's (a `client_id` must agree), or
    without one in `client_id`'s `global` VRF (`registry.vrf_for_new_row`)."""
    network = _parse_prefix(prefix)
    if kind not in SUBNET_KINDS:
        raise IpamValidationError(
            tr(
                "kind must be one of {subnet_kinds}; got {kind}",
                subnet_kinds=list(SUBNET_KINDS),
                kind=repr(kind),
            )
        )
    if not isinstance(name, str) or len(name) > _MAX_NAME:
        raise IpamValidationError(
            tr("name must be a string of at most {max_name} characters", max_name=_MAX_NAME)
        )
    gateway_text = _validate_gateway(gateway, network)
    vrf_id, client = await vrf_for_new_row(session, org_id, vrf_id=vrf_id, client_id=client_id)
    await _validate_references(
        session, org_id, client, site_id=site_id, vlan_id=vlan_id, doc_record_id=doc_record_id
    )

    # THE CRITICAL SECTION. Lock, check, insert, re-derive -- in that order, in
    # one transaction. Splitting the check from the insert is the race §0.3
    # exists to close.
    await lock_address_plan(session, org_id, vrf_id)
    await _refuse_overlap(session, org_id, vrf_id, network, kind)

    now = _utcnow()
    inserted_id = (
        await session.execute(
            pg_insert(IpamSubnet)
            .values(
                org_id=org_id,
                vrf_id=vrf_id,
                prefix=str(network),
                kind=kind,
                name=name,
                description=description,
                site_id=site_id,
                vlan_id=vlan_id,
                gateway=gateway_text,
                dns_servers=list(dns_servers or []),
                doc_record_id=doc_record_id,
                created_by=actor_id,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["org_id", "vrf_id", "prefix"]
            )
            .returning(IpamSubnet.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise IpamConflictError(tr("{network} is already registered in this VRF", network=network))

    await recompute_parents(session, org_id, vrf_id, network)
    subnet = await get_subnet(session, org_id, inserted_id)
    await session.refresh(subnet)
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.subnet_created",
        "ipam_subnet",
        subnet.id,
        {
            "prefix": str(network),
            "vrf_id": str(vrf_id),
            "kind": kind,
            "name": name,
            "requested_prefix": prefix,
        },
        client_id=client,
    )
    return subnet


async def update_subnet(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    subnet_id: UUID,
    fields: dict[str, Any],
) -> IpamSubnet:
    """Edit the descriptive fields. `prefix`, `vrf_id` and `parent_id` are not
    among them -- see `SUBNET_EDITABLE_FIELDS`."""
    if not fields:
        raise IpamValidationError(tr("no fields supplied"))
    unknown = sorted(set(fields) - set(SUBNET_EDITABLE_FIELDS))
    if unknown:
        raise IpamValidationError(
            tr(
                "unknown or server-owned subnet field(s): {unknown}; editable fields are "
                "{subnet_editable_fields}",
                unknown=unknown,
                subnet_editable_fields=list(SUBNET_EDITABLE_FIELDS),
            )
        )
    subnet = await get_subnet(session, org_id, subnet_id)
    # Read before the flush below: it expires the trigger-written client.
    client_id = subnet.client_id
    merged = {field: getattr(subnet, field) for field in SUBNET_EDITABLE_FIELDS} | fields
    network = _parse_prefix(subnet.prefix)

    if merged["kind"] not in SUBNET_KINDS:
        raise IpamValidationError(
            tr(
                "kind must be one of {subnet_kinds}; got {merged_kind}",
                subnet_kinds=list(SUBNET_KINDS),
                merged_kind=repr(merged["kind"]),
            )
        )
    gateway_text = _validate_gateway(merged["gateway"], network)
    await _validate_references(
        session,
        org_id,
        client_id,
        site_id=merged["site_id"] if "site_id" in fields else None,
        vlan_id=merged["vlan_id"] if "vlan_id" in fields else None,
        doc_record_id=merged["doc_record_id"] if "doc_record_id" in fields else None,
    )

    # Promoting a `container` to `assignable` can create exactly the overlap the
    # create path refuses, so it takes the same lock and the same check. A
    # kind edit that skipped this is how the rule gets bypassed without anyone
    # writing an overlapping prefix.
    if merged["kind"] != subnet.kind:
        await lock_address_plan(session, org_id, subnet.vrf_id)
        await _refuse_overlap(
            session, org_id, subnet.vrf_id, network, str(merged["kind"]),
            exclude_id=subnet.id,
        )

    previous = {field: getattr(subnet, field) for field in fields}
    for field, value in fields.items():
        setattr(subnet, field, value)
    # Only when the caller actually sent one: writing the normalised form back
    # unconditionally would dirty (and re-UPDATE) the column on every edit to
    # an unrelated field.
    if "gateway" in fields:
        subnet.gateway = gateway_text
    subnet.updated_at = _utcnow()
    await session.flush()
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.subnet_updated",
        "ipam_subnet",
        subnet.id,
        {
            "prefix": str(network),
            "vrf_id": str(subnet.vrf_id),
            "from": _jsonable(previous),
            "to": _jsonable(fields),
        },
        client_id=client_id,
    )
    await loaded_client(session, subnet)  # the response names it
    return subnet


async def delete_subnet(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    subnet_id: UUID,
    *,
    cascade: bool = False,
) -> None:
    """Refuse while anything depends on it, unless `cascade` is asked for.

    ON CASCADE, ADDRESSES KEEP THEIR ROWS with `subnet_id` set to NULL (§0.4):
    "sparse" means "no row until first mentioned", not "rows evaporate", and an
    address that is now inside no registered subnet is an ORPHAN, which is a
    finding rather than a deletion. Observations and drift findings are nulled
    for the same reason -- they are evidence, and evidence about an address does
    not stop being true because somebody deleted the subnet it sat in.

    Ranges and sweep runs ARE deleted, because both carry `subnet_id NOT NULL`
    and neither means anything without it: a DHCP pool is a band inside a
    specific prefix, and a sweep run is a probe of one.
    """
    subnet = await get_subnet(session, org_id, subnet_id)
    dependents = await _dependent_counts(session, org_id, subnet_id)
    if dependents and not cascade:
        raise IpamConflictError(
            tr(
                "subnet {prefix} still holds {holding}; delete with cascade to release them",
                prefix=subnet.prefix,
                holding=", ".join(counted(count, noun) for noun, count in dependents.items()),
            )
        )

    await lock_address_plan(session, org_id, subnet.vrf_id)
    if cascade:
        for nullable in (IpamAddress, IpamObservation, IpamDrift):
            await session.execute(
                update(nullable)
                .where(nullable.org_id == org_id, nullable.subnet_id == subnet_id)
                .values(subnet_id=None)
            )
        for owned in (IpamRange, IpamSweepRun):
            await session.execute(
                delete(owned).where(owned.org_id == org_id, owned.subnet_id == subnet_id)
            )
    prefix, vrf_id, client_id = subnet.prefix, subnet.vrf_id, subnet.client_id
    # DETACH THE CHILDREN FIRST. `parent_id` is a self-referencing foreign key,
    # so deleting a container while anything still points at it is a
    # `ForeignKeyViolationError` -- re-deriving afterwards is too late, because
    # there is no afterwards. Clearing first and re-deriving after is what makes
    # deleting a middle tier of the hierarchy legal, which it must be: the tier
    # is a CACHE of containment (§0.2), not a relationship anyone asserted.
    await session.execute(
        update(IpamSubnet)
        .where(IpamSubnet.org_id == org_id, IpamSubnet.parent_id == subnet_id)
        .values(parent_id=None)
    )
    await session.delete(subnet)
    await session.flush()
    # The children detached above now attach to whatever contains them.
    await recompute_parents(session, org_id, vrf_id, prefix)
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.subnet_deleted",
        "ipam_subnet",
        subnet_id,
        {"prefix": str(prefix), "vrf_id": str(vrf_id), "cascade": cascade},
        client_id=client_id,
    )


async def _dependent_counts(
    session: AsyncSession, org_id: UUID, subnet_id: UUID
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model, noun in (
        (IpamAddress, "address"),
        (IpamRange, "range"),
        (IpamObservation, "observation"),
        (IpamDrift, "drift finding"),
        (IpamSweepRun, "sweep run"),
    ):
        count = (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(model.org_id == org_id, model.subnet_id == subnet_id)
            )
        ).scalar_one()
        if count:
            counts[noun] = count
    return counts


# ---------------------------------------------------------------------------
# Ranges (§2.5)
# ---------------------------------------------------------------------------


async def list_ranges(
    session: AsyncSession, org_id: UUID, subnet_id: UUID
) -> list[IpamRange]:
    await get_subnet(session, org_id, subnet_id)
    return list(
        (
            await session.execute(
                select(IpamRange)
                .where(IpamRange.org_id == org_id, IpamRange.subnet_id == subnet_id)
                .order_by(IpamRange.start_address)
            )
        )
        .scalars()
        .all()
    )


async def create_range(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    subnet_id: UUID,
    *,
    kind: str,
    start_address: str,
    end_address: str,
    name: str = "",
    description: str = "",
) -> IpamRange:
    """A band inside one subnet -- how "the DHCP pool is .100-.200" is expressed
    WITHOUT 101 address rows (§0.4 would be defeated immediately otherwise).

    THE CONTAINMENT CHECK IS HERE AND NOT A CHECK CONSTRAINT because a CHECK
    cannot reach the parent row. `start <= end` IS a CHECK (`ck_ipam_range_
    ordered`); this validates it too so the operator gets a 400 instead of a
    500.
    """
    if kind not in RANGE_KINDS:
        raise IpamValidationError(
            tr(
                "kind must be one of {range_kinds}; got {kind}",
                range_kinds=list(RANGE_KINDS),
                kind=repr(kind),
            )
        )
    subnet = await get_subnet(session, org_id, subnet_id)
    network = _parse_prefix(subnet.prefix)

    start = parse_address(start_address)
    end = parse_address(end_address)
    if start is None or end is None:
        raise IpamValidationError(tr("start_address and end_address must be valid addresses"))
    if start.version != end.version:
        raise IpamValidationError(tr("start_address and end_address must be the same family"))
    if start.version != network.version:
        raise IpamValidationError(
            tr(
                "range is IPv{start_version} but {network} is IPv{network_version}",
                start_version=start.version,
                network=network,
                network_version=network.version,
            )
        )
    if int(end) < int(start):
        raise IpamValidationError(
            tr("start_address {start} is after end_address {end}", start=start, end=end)
        )
    for endpoint in (start, end):
        if endpoint not in network:
            raise IpamValidationError(
                tr("{endpoint} is outside {network}", endpoint=endpoint, network=network)
            )

    # Two ranges of one subnet that overlap make "is this address in a DHCP
    # pool" ambiguous, and §3.2 refuses a static assignment inside a pool on the
    # strength of that answer.
    clash = (
        await session.execute(
            select(IpamRange.id, IpamRange.start_address, IpamRange.end_address).where(
                IpamRange.org_id == org_id,
                IpamRange.subnet_id == subnet_id,
                text(
                    "ipam_range.start_address <= CAST(:end AS inet)"
                    " AND ipam_range.end_address >= CAST(:start AS inet)"
                ).bindparams(start=str(start), end=str(end)),
            )
        )
    ).first()
    if clash is not None:
        raise IpamConflictError(
            tr(
                "{start}-{end} overlaps the existing range "
                "{clash_start_address}-{clash_end_address} in this subnet",
                start=start,
                end=end,
                clash_start_address=clash.start_address,
                clash_end_address=clash.end_address,
            )
        )

    row = IpamRange(
        org_id=org_id,
        subnet_id=subnet_id,
        kind=kind,
        start_address=str(start),
        end_address=str(end),
        name=name,
        description=description,
        created_by=actor_id,
        created_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.range_created",
        "ipam_range",
        row.id,
        {
            "subnet_id": str(subnet_id),
            "prefix": str(network),
            "kind": kind,
            "start_address": str(start),
            "end_address": str(end),
        },
        client_id=subnet.client_id,
    )
    return row


async def delete_range(
    session: AsyncSession, org_id: UUID, actor_id: UUID | None, range_id: UUID
) -> None:
    row = (
        await session.execute(
            select(IpamRange).where(IpamRange.org_id == org_id, IpamRange.id == range_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise IpamNotFoundError(tr("range {range_id} not found", range_id=range_id))
    metadata = {
        "subnet_id": str(row.subnet_id),
        "kind": row.kind,
        "start_address": str(row.start_address),
        "end_address": str(row.end_address),
    }
    client_id = row.client_id
    await session.delete(row)
    await session.flush()
    await _audit(
        session, org_id, actor_id, "ipam.range_deleted", "ipam_range", range_id, metadata,
        client_id=client_id,
    )


def _jsonable(fields: dict[str, Any]) -> dict[str, Any]:
    """Audit metadata is JSONB; a `UUID` is not JSON."""
    return {k: (str(v) if isinstance(v, UUID) else v) for k, v in fields.items()}


async def set_sweep_settings(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    subnet_id: UUID,
    *,
    sweep_enabled: bool,
    sweep_device_id: UUID | None = None,
    sweep_interval_hours: int | None = None,
) -> IpamSubnet:
    """§9's `PUT /subnets/{id}/sweep-settings`. Gated on `ipam.sweep.manage`.

    **ENABLING A SWEEP AUTHORIZES NOTHING** (§0.12). This decides whether and
    how often the server will *try*; whether it *may* is
    `ipam_scan_authorization`, a different table gated on a different, admin-tier
    permission. A subnet with `sweep_enabled` and no authorization records a
    `refused` run every cycle, which is the correct and visible outcome -- and
    is why this function may not touch that table.

    **THE INTERVAL IS CLAMPED TO >= 1 HOUR** (§0.10 point 6), refused here as
    well as by `ck_ipam_subnet_sweep_interval` and by `due_subnet_ids`'
    `GREATEST(..., 1)`. A one-minute sweep interval is the exact mechanism by
    which an IPAM becomes a ping monitor; three layers, because the application
    check is one refactor away from being the only one.

    A designated device must resolve IN THIS ORG: `devices.id` is org-blind, and
    referential integrity runs as the referenced table's owner, so the foreign
    key alone would happily accept another tenant's machine as this segment's
    sweeper.
    """
    subnet = await get_subnet(session, org_id, subnet_id)
    if subnet.kind != "assignable":
        raise IpamValidationError(
            tr(
                "subnet {subnet_prefix} is a container: it holds no addresses (§0.2) and "
                "cannot be swept",
                subnet_prefix=subnet.prefix,
            )
        )
    if sweep_interval_hours is not None:
        if isinstance(sweep_interval_hours, bool) or not isinstance(
            sweep_interval_hours, int
        ):
            raise IpamValidationError(tr("sweep_interval_hours must be an integer or null"))
        if sweep_interval_hours < 1:
            raise IpamValidationError(
                tr(
                    "sweep_interval_hours must be at least 1 (§0.10: a sub-hourly sweep "
                    "interval is how an address plan becomes a ping monitor)"
                )
            )
    if sweep_device_id is not None:
        found = (
            await session.execute(
                select(Device.id).where(
                    Device.org_id == org_id, Device.id == sweep_device_id
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise IpamValidationError(
                tr("device {sweep_device_id} not found", sweep_device_id=sweep_device_id)
            )

    client_id = subnet.client_id  # before the flush that expires it
    before = {
        "sweep_enabled": subnet.sweep_enabled,
        "sweep_device_id": str(subnet.sweep_device_id) if subnet.sweep_device_id else None,
        "sweep_interval_hours": subnet.sweep_interval_hours,
    }
    subnet.sweep_enabled = bool(sweep_enabled)
    subnet.sweep_device_id = sweep_device_id
    subnet.sweep_interval_hours = sweep_interval_hours
    session.add(subnet)
    await session.flush()
    await _audit(
        session,
        org_id,
        actor_id,
        "ipam.sweep_settings_changed",
        "ipam_subnet",
        subnet.id,
        {
            "prefix": str(subnet.prefix),
            "from": before,
            "to": {
                "sweep_enabled": subnet.sweep_enabled,
                "sweep_device_id": str(sweep_device_id) if sweep_device_id else None,
                "sweep_interval_hours": sweep_interval_hours,
            },
        },
        client_id=client_id,
    )
    await loaded_client(session, subnet)  # the response names it
    return subnet
