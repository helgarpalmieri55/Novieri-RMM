"""The assignment workflow and the audit-history projection
(`docs/modules/ipam/spec.md` §3.2, §0.6, §0.13, §11.3).

**EVERY FUNCTION HERE IS AN OPERATOR ACTION. THE ACTOR IS NEVER AN AGENT**
(§3.2's last row). No ingestion path calls into this module -- `observations.py`
writes `ipam_observation` rows and nothing else -- and that is §0.6's split,
which is the control that stops a device coming online from silently
overwriting what an operator documented. It is not a modelling preference: if
`in_use` were a value of `state`, an agent's report would have to write this
table.

**THE TRANSITION TABLE IS §3.2's, AND ONLY §3.2's.** Permitted:

    (absent)          -> reserved
    (absent) | free   -> assigned          (+ exactly one claimant)
    reserved          -> assigned
    assigned          -> free  (release)   clears claimant + mac, stamps released_at
    assigned          -> reserved          clears claimant
    reserved          -> free
    any               -> deprecated

Two readings the table leaves implicit are made explicit here rather than left
to whichever branch a builder wrote first:

* **`assigned -> assigned` is REFUSED.** Re-assigning in place would re-point a
  claimant with no release event, so the history would show a machine's address
  moving with nothing recording that the previous holder lost it. Release, then
  assign.
* **`deprecated` is not a trap, but it is not a shortcut either.** It exits via
  `release` (to `free`) and via `reserve`, both of which carry no claimant --
  but `deprecated -> assigned` is refused, because §3.2 permits `assigned` only
  from absent, `free` or `reserved`. That makes reviving a written-off address
  one deliberate, separately audited step.

**RELEASE KEEPS THE ROW** (§0.4). Sparse means "no row until first mentioned",
not "rows evaporate": the description, the history and the audit target all hang
on that row, and an address that has been used before is exactly the one an
operator wants a record of.

**THERE IS NO `ipam_address_history` TABLE** (§0.13). Assign, release, reserve
and state-change write hash-chained audit events, which are tamper-evident; a
second history table would be neither, and would be a second thing to keep in
sync. `address_history` is a NARROW projection of `audit_events` filtered to
`target_type='ipam_address'` and one id -- deliberately readable by
`ipam.read` alone (§0.13), while the general audit API keeps requiring
`audit.read`.

**EVERY QUERY CARRIES AN EXPLICIT `org_id` PREDICATE** in addition to RLS
(invariant #5), pinned on the ADMIN engine by `tests/test_ipam_addresses.py`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_network
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import AuditEvent, Device
from app.modules.ipam.allocation import (
    ALLOCATION_METHODS,
    claim_address,
    covering_range_kinds,
    is_network_or_broadcast,
)
from app.modules.ipam.errors import (
    IpamConflictError,
    IpamNotFoundError,
    IpamValidationError,
)
from app.modules.ipam.models import IpamAddress, IpamObservation, IpamSubnet
from app.modules.ipam.net import parse_address, parse_mac
from app.modules.ipam.registry import ensure_default_vrf, get_vrf
from app.modules.ipam.subnets import containing_subnet

__all__ = [
    "ADDRESS_EDITABLE_FIELDS",
    "AddressHistoryEntry",
    "SearchResult",
    "address_history",
    "assign",
    "create_address",
    "deprecate",
    "get_address",
    "release",
    "reserve",
    "search",
    "update_address",
]

#: `state` is ABSENT, deliberately. Every state change goes through the
#: transition functions below, each of which checks §3.2's predicates and writes
#: its own audit event. A `state` in this tuple would be a bypass of both.
#: `address`, `vrf_id` and `subnet_id` are absent because they are the row's
#: identity and its derivation respectively.
ADDRESS_EDITABLE_FIELDS = ("hostname", "description", "asset_label", "allocation_method")

_MAX_SEARCH = 200


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AddressHistoryEntry:
    """One row of §0.13's projection: timestamp, actor, action, from/to."""

    ts: datetime
    actor_type: str
    actor_id: UUID | None
    action: str
    from_state: str | None
    to_state: str | None
    detail: dict[str, Any]


@dataclass(frozen=True)
class SearchResult:
    """§9's one search box. `kind` says how `q` was interpreted, because an
    operator who typed a hostname that happens to parse as something else needs
    to see which question was answered."""

    kind: str
    query: str
    subnets: list[UUID]
    addresses: list[str]
    observations: int
    devices: list[UUID]


async def get_address(session: AsyncSession, org_id: UUID, address_id: UUID) -> IpamAddress:
    """Org-scoped load. Another org's id is indistinguishable from a nonexistent
    one -- both 404, never 403, because a 403 confirms the id exists."""
    row = (
        await session.execute(
            select(IpamAddress).where(
                IpamAddress.org_id == org_id, IpamAddress.id == address_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise IpamNotFoundError(tr("address {address_id} not found", address_id=address_id))
    return row


async def _resolve_target(
    session: AsyncSession, org_id: UUID, address: str, vrf_id: UUID | None
) -> tuple[IpamSubnet, str, UUID]:
    """§3.2's shared preamble: the address parses, a subnet in-org holds it, that
    subnet is `assignable`, and the address is not one of the prefix's
    endpoints.

    Returns `(subnet, canonical address, vrf_id)`. The subnet is DERIVED by
    containment rather than supplied: the caller names an address, and which
    prefix holds it is a fact about the registry, not an argument.
    """
    parsed = parse_address(address)
    if parsed is None:
        raise IpamValidationError(tr("{address} is not a valid address", address=repr(address)))
    if vrf_id is None:
        vrf_id = await ensure_default_vrf(session, org_id)
    else:
        await get_vrf(session, org_id, vrf_id)

    subnet = await containing_subnet(session, org_id, vrf_id, str(parsed))
    if subnet is None:
        raise IpamValidationError(
            tr(
                "{parsed} is inside no registered subnet in this VRF -- register the "
                "subnet first (an observed address with no subnet is an orphan, §4.2, but "
                "a documented one is a typo)",
                parsed=parsed,
            )
        )
    if subnet.kind != "assignable":
        raise IpamValidationError(
            tr(
                "{parsed} falls in the container subnet {subnet_prefix}, which holds no "
                "addresses (§0.2). Register the assignable subnet inside it first",
                parsed=parsed,
                subnet_prefix=subnet.prefix,
            )
        )
    prefix = ip_network(str(subnet.prefix), strict=False)
    if is_network_or_broadcast(str(parsed), prefix):
        raise IpamValidationError(
            tr(
                "{parsed} is the network or broadcast address of {prefix} and cannot be "
                "reserved or assigned",
                parsed=parsed,
                prefix=prefix,
            )
        )
    return subnet, str(parsed), vrf_id


async def _existing(
    session: AsyncSession, org_id: UUID, vrf_id: UUID, address: str
) -> IpamAddress | None:
    return (
        await session.execute(
            select(IpamAddress).where(
                IpamAddress.org_id == org_id,
                IpamAddress.vrf_id == vrf_id,
                IpamAddress.address == address,
            )
        )
    ).scalar_one_or_none()


async def _audit_state_change(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    row: IpamAddress,
    action: str,
    *,
    from_state: str | None,
    to_state: str,
    extra: dict[str, Any] | None = None,
) -> None:
    await audit_service.record(
        session,
        org_id=org_id,
        # §3.2's last row: the actor is never an agent. There is no call site in
        # this module that could pass anything else.
        actor_type="user",
        actor_id=actor_id,
        action=action,
        target_type="ipam_address",
        target_id=str(row.id),
        metadata={
            "address": str(row.address),
            "vrf_id": str(row.vrf_id),
            "subnet_id": str(row.subnet_id) if row.subnet_id else None,
            "from_state": from_state,
            "to_state": to_state,
            **(extra or {}),
        },
    )
    await session.flush()


async def create_address(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    *,
    address: str,
    vrf_id: UUID | None = None,
    hostname: str = "",
    description: str = "",
    origin: str = "manual",
) -> IpamAddress:
    """Document an address without claiming it -- `state='free'`.

    This is the "we know this address exists and here is what it is for" row: a
    label and a description with no claimant. Reserving and assigning are
    separate calls with their own predicates and their own audit events.
    """
    subnet, canonical, vrf = await _resolve_target(session, org_id, address, vrf_id)
    row = await _existing(session, org_id, vrf, canonical)
    if row is not None:
        raise IpamConflictError(
            tr("{canonical} already has a record in this VRF", canonical=canonical)
        )
    row = IpamAddress(
        org_id=org_id,
        vrf_id=vrf,
        subnet_id=subnet.id,
        address=canonical,
        state="free",
        origin=origin,
        hostname=hostname,
        description=description,
    )
    session.add(row)
    await session.flush()
    await _audit_state_change(
        session, org_id, actor_id, row, "ipam.address_state_changed",
        from_state=None, to_state="free",
    )
    return row


async def update_address(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    address_id: UUID,
    fields: dict[str, Any],
) -> IpamAddress:
    """Edit the descriptive fields. `state` IS NOT ONE OF THEM.

    Every state change goes through `assign` / `reserve` / `release` /
    `deprecate`, each of which checks §3.2's predicates. A `state` key accepted
    here would be a bypass of the whole table, and of the audit shape §11.3
    specifies for each transition.
    """
    if not fields:
        raise IpamValidationError(tr("no fields supplied"))
    unknown = sorted(set(fields) - set(ADDRESS_EDITABLE_FIELDS))
    if unknown:
        raise IpamValidationError(
            tr(
                "unknown or server-owned address field(s): {unknown}; editable fields are "
                "{address_editable_fields} -- `state` changes go through "
                "assign/reserve/release/deprecate so §3.2's predicates and §11.3's audit "
                "shape cannot be bypassed",
                unknown=unknown,
                address_editable_fields=list(ADDRESS_EDITABLE_FIELDS),
            )
        )
    row = await get_address(session, org_id, address_id)
    if "allocation_method" in fields and fields["allocation_method"] not in ALLOCATION_METHODS:
        raise IpamValidationError(
            tr(
                "allocation_method must be one of {allocation_methods}",
                allocation_methods=list(ALLOCATION_METHODS),
            )
        )
    if "asset_label" in fields and row.state == "assigned" and row.device_id is not None:
        raise IpamValidationError(
            tr("this address is claimed by a device; releasing it is how the claimant changes")
        )
    previous = {field: getattr(row, field) for field in fields}
    for field, value in fields.items():
        setattr(row, field, value)
    row.updated_at = _utcnow()
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ipam.address_updated",
        target_type="ipam_address",
        target_id=str(row.id),
        metadata={
            "address": str(row.address),
            "from": previous,
            "to": dict(fields),
        },
    )
    await session.flush()
    return row


async def assign(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    *,
    address: str,
    vrf_id: UUID | None = None,
    device_id: UUID | None = None,
    asset_label: str = "",
    hostname: str = "",
    description: str = "",
    mac: str | None = None,
    allocation_method: str = "static",
) -> IpamAddress:
    """(absent) | `free` | `reserved` -> `assigned` (§3.2).

    The predicates -- one claimant, an in-org device, not a `dhcp_pool`, no
    unresolved `conflict` finding -- live in `allocation.claim_address`, which is
    also what `allocate` calls. ONE WRITER: two copies of §3.2's rules is how the
    allocator and the assignment page come to disagree about what is legal.
    """
    subnet, canonical, vrf = await _resolve_target(session, org_id, address, vrf_id)
    row = await _existing(session, org_id, vrf, canonical)
    if row is not None and row.state == "deprecated":
        raise IpamConflictError(
            tr(
                "{canonical} is deprecated ('we know this is gone'); release it first if "
                "it is back in service -- §3.2 permits `assigned` only from absent, free "
                "or reserved",
                canonical=canonical,
            )
        )
    return await claim_address(
        session,
        org_id,
        subnet,
        canonical,
        actor_id=actor_id,
        device_id=device_id,
        asset_label=asset_label,
        hostname=hostname,
        description=description,
        mac=mac,
        allocation_method=allocation_method,
    )


async def reserve(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    *,
    address: str,
    vrf_id: UUID | None = None,
    hostname: str = "",
    description: str = "",
) -> IpamAddress:
    """-> `reserved` (§3.2). Held deliberately, claimed by nobody.

    From `assigned` this CLEARS THE CLAIMANT, which is the table's rule: a
    reservation with a device still attached is an assignment wearing a
    different label, and §3.1 would render it `reserved` while the drift
    predicates still read `device_id`.
    """
    subnet, canonical, vrf = await _resolve_target(session, org_id, address, vrf_id)
    kinds = await covering_range_kinds(session, org_id, subnet.id, canonical)
    if "dhcp_pool" in kinds:
        raise IpamConflictError(
            tr(
                "{canonical} is inside a dhcp_pool range: shrink the pool or carve a "
                "reserved range out of it, then reserve",
                canonical=canonical,
            )
        )
    row = await _existing(session, org_id, vrf, canonical)
    previous = row.state if row is not None else None
    if row is None:
        row = IpamAddress(
            org_id=org_id,
            vrf_id=vrf,
            subnet_id=subnet.id,
            address=canonical,
            origin="manual",
        )
        session.add(row)
    row.subnet_id = subnet.id
    row.state = "reserved"
    row.device_id = None
    row.asset_label = ""
    row.assigned_by = None
    row.assigned_at = None
    if hostname:
        row.hostname = hostname
    if description:
        row.description = description
    row.updated_at = _utcnow()
    await session.flush()
    await _audit_state_change(
        session, org_id, actor_id, row, "ipam.address_reserved",
        from_state=previous, to_state="reserved",
    )
    return row


async def release(
    session: AsyncSession, org_id: UUID, actor_id: UUID | None, address_id: UUID
) -> IpamAddress:
    """`assigned` | `reserved` | `deprecated` -> `free` (§3.2).

    Clears the claimant and the MAC, stamps `released_at`, and KEEPS THE ROW
    (§0.4). The audit event carries the PREVIOUS claimant -- recorded before the
    clear, because recorded after it the log would say an address was released
    from nobody.
    """
    row = await get_address(session, org_id, address_id)
    if row.state == "free":
        raise IpamConflictError(tr("{row_address} is already free", row_address=row.address))
    previous_state = row.state
    previous_claimant = str(row.device_id) if row.device_id else row.asset_label
    row.state = "free"
    row.device_id = None
    row.asset_label = ""
    row.mac = None
    row.assigned_by = None
    row.assigned_at = None
    row.released_at = _utcnow()
    row.updated_at = _utcnow()
    await session.flush()
    await _audit_state_change(
        session, org_id, actor_id, row, "ipam.address_released",
        from_state=previous_state,
        to_state="free",
        extra={"previous_claimant": previous_claimant},
    )
    return row


async def deprecate(
    session: AsyncSession, org_id: UUID, actor_id: UUID | None, address_id: UUID
) -> IpamAddress:
    """any -> `deprecated` (§3.2): "we know this is gone, stop telling us".

    Excluded from next-free and EXEMPT FROM ABSENCE DRIFT -- which is the point.
    An address the operator has written off should not keep generating
    `documented_absent` findings forever.
    """
    row = await get_address(session, org_id, address_id)
    previous_state = row.state
    row.state = "deprecated"
    row.device_id = None
    row.asset_label = ""
    row.assigned_by = None
    row.assigned_at = None
    row.updated_at = _utcnow()
    await session.flush()
    await _audit_state_change(
        session, org_id, actor_id, row, "ipam.address_state_changed",
        from_state=previous_state, to_state="deprecated",
    )
    return row


# ---------------------------------------------------------------------------
# §0.13 -- the narrow audit-history projection
# ---------------------------------------------------------------------------


async def address_history(
    session: AsyncSession, org_id: UUID, address_id: UUID
) -> list[AddressHistoryEntry]:
    """This address's assignment history, from `audit_events` (§0.13).

    Gated on `ipam.read` at the API, deliberately: this is a narrow, single-
    target slice of the audit log for people who do not hold `audit.read`, and
    the general audit API keeps requiring it. The narrowness is the control --
    `target_type` and `target_id` are both pinned, so it can only ever return
    events about the one address the caller already proved they can see.
    """
    # Existence first, in org scope, so an unknown id 404s instead of returning
    # an empty history that reads as "nothing ever happened here".
    await get_address(session, org_id, address_id)
    rows = (
        (
            await session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == "ipam_address",
                    AuditEvent.target_id == str(address_id),
                )
                .order_by(AuditEvent.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        AddressHistoryEntry(
            ts=row.ts,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            action=row.action,
            from_state=row.meta.get("from_state"),
            to_state=row.meta.get("to_state"),
            detail={
                key: value
                for key, value in row.meta.items()
                if key not in ("from_state", "to_state")
            },
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# §9's one search box
# ---------------------------------------------------------------------------


async def search(session: AsyncSession, org_id: UUID, q: str) -> SearchResult:
    """Dispatch on what `q` PARSES AS (§9).

    One endpoint because operators do not know in advance which of these they
    are holding: a bare IP, a CIDR, a MAC, a hostname or a device name. Every
    branch is a native `inet`/`macaddr` operation, not a text match -- in `text`,
    `10.65.70.5` matches `LIKE '10.65.7%'` (§0.1).
    """
    query = (q or "").strip()
    if not query:
        raise IpamValidationError(tr("search query must not be empty"))

    mac = parse_mac(query)
    if mac is not None:
        return await _search_mac(session, org_id, query, mac)
    if "/" in query:
        try:
            network = ip_network(query, strict=False)
        except ValueError:
            network = None
        if network is not None:
            return await _search_cidr(session, org_id, query, str(network))
    address = parse_address(query)
    if address is not None:
        return await _search_address(session, org_id, query, str(address))
    return await _search_text(session, org_id, query)


async def _search_mac(
    session: AsyncSession, org_id: UUID, query: str, mac: str
) -> SearchResult:
    """EVERY ADDRESS THIS MAC HAS BEEN SEEN AT, across every subnet -- "where did
    this printer go".

    Both sides are read: what the plan says (`ipam_address.mac`) and what the
    fleet reported (`ipam_observation.mac`). Reading only the documented side
    answers the question the operator already knew the answer to.
    """
    documented = (
        (
            await session.execute(
                select(IpamAddress.address).where(
                    IpamAddress.org_id == org_id, IpamAddress.mac == mac
                )
            )
        )
        .scalars()
        .all()
    )
    observed = (
        (
            await session.execute(
                select(IpamObservation.address).where(
                    IpamObservation.org_id == org_id, IpamObservation.mac == mac
                )
            )
        )
        .scalars()
        .all()
    )
    addresses = sorted({str(a) for a in documented} | {str(a) for a in observed})
    return SearchResult(
        kind="mac",
        query=query,
        subnets=[],
        addresses=addresses[:_MAX_SEARCH],
        observations=len(observed),
        devices=[],
    )


async def _search_cidr(
    session: AsyncSession, org_id: UUID, query: str, network: str
) -> SearchResult:
    subnets = (
        (
            await session.execute(
                select(IpamSubnet.id).where(
                    IpamSubnet.org_id == org_id,
                    text("ipam_subnet.prefix <<= CAST(:net AS cidr)").bindparams(net=network),
                )
            )
        )
        .scalars()
        .all()
    )
    addresses = (
        (
            await session.execute(
                select(IpamAddress.address)
                .where(
                    IpamAddress.org_id == org_id,
                    text("ipam_address.address <<= CAST(:net AS inet)").bindparams(
                        net=network
                    ),
                )
                .order_by(IpamAddress.address)
                .limit(_MAX_SEARCH)
            )
        )
        .scalars()
        .all()
    )
    observations = (
        await session.execute(
            text(
                "SELECT count(*) AS n FROM ipam_observation"
                " WHERE org_id = :org AND address <<= CAST(:net AS inet)"
            ),
            {"org": str(org_id), "net": network},
        )
    ).one()
    return SearchResult(
        kind="cidr",
        query=query,
        subnets=list(subnets),
        addresses=[str(a) for a in addresses],
        observations=int(observations.n),
        devices=[],
    )


async def _search_address(
    session: AsyncSession, org_id: UUID, query: str, address: str
) -> SearchResult:
    """Which subnet holds it, which record documents it, which observations
    saw it. The subnet lookup is a containment query answered by the GiST
    index, across every VRF -- the operator typing an address does not know
    which routing domain it is in, and asking them to would defeat the box."""
    subnets = (
        (
            await session.execute(
                select(IpamSubnet.id)
                .where(
                    IpamSubnet.org_id == org_id,
                    text("ipam_subnet.prefix >>= CAST(:addr AS inet)").bindparams(
                        addr=address
                    ),
                )
                .order_by(text("masklen(ipam_subnet.prefix) DESC"))
            )
        )
        .scalars()
        .all()
    )
    addresses = (
        (
            await session.execute(
                select(IpamAddress.address).where(
                    IpamAddress.org_id == org_id, IpamAddress.address == address
                )
            )
        )
        .scalars()
        .all()
    )
    observations = (
        await session.execute(
            text(
                "SELECT count(*) AS n FROM ipam_observation"
                " WHERE org_id = :org AND address = CAST(:addr AS inet)"
            ),
            {"org": str(org_id), "addr": address},
        )
    ).one()
    claimants = (
        (
            await session.execute(
                select(IpamAddress.device_id).where(
                    IpamAddress.org_id == org_id,
                    IpamAddress.address == address,
                    IpamAddress.device_id.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )
    devices = [device for device in claimants if device is not None]
    return SearchResult(
        kind="address",
        query=query,
        subnets=list(subnets),
        addresses=[str(a) for a in addresses],
        observations=int(observations.n),
        devices=devices,
    )


async def _search_text(session: AsyncSession, org_id: UUID, query: str) -> SearchResult:
    """A hostname or a device name. The device branch resolves names to ids and
    then reads the addresses claimed by them, so "which addresses does this
    machine hold" is answerable without the operator knowing any of them."""
    pattern = f"%{query}%"
    devices = (
        (
            await session.execute(
                select(Device.id).where(Device.org_id == org_id, Device.hostname.ilike(pattern))
            )
        )
        .scalars()
        .all()
    )
    condition: Any = IpamAddress.hostname.ilike(pattern)
    if devices:
        condition = or_(condition, IpamAddress.device_id.in_(list(devices)))
    addresses = (
        (
            await session.execute(
                select(IpamAddress.address)
                .where(IpamAddress.org_id == org_id, condition)
                .order_by(IpamAddress.address)
                .limit(_MAX_SEARCH)
            )
        )
        .scalars()
        .all()
    )
    subnets = (
        (
            await session.execute(
                select(IpamSubnet.id).where(
                    IpamSubnet.org_id == org_id, IpamSubnet.name.ilike(pattern)
                )
            )
        )
        .scalars()
        .all()
    )
    observations = (
        (
            await session.execute(
                select(IpamObservation.id).where(
                    IpamObservation.org_id == org_id, IpamObservation.hostname.ilike(pattern)
                )
            )
        )
        .scalars()
        .all()
    )
    return SearchResult(
        kind="text",
        query=query,
        subnets=list(subnets),
        addresses=[str(a) for a in addresses],
        observations=len(observations),
        devices=list(devices),
    )
