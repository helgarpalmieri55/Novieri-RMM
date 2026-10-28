"""Derived `status`, utilisation, next-free and the atomic allocator
(`docs/modules/ipam/spec.md` §3.1, §3.3, §0.4, §0.5, §0.6, §0.16).

**`status` IS DERIVED AND STORED NOWHERE.** `ipam_address.state` is what a
HUMAN asserted -- `free | reserved | assigned | deprecated` -- and `status` is a
pure function of it, the covering ranges, the current observations and the three
gates. If `in_use` were a value of `state`, an agent reporting an address would
have to write that column, and a device coming online would silently overwrite
what an operator documented (§0.6). `derive_status` is pure so it can be
table-tested exhaustively and so the frontend's `status.ts` can be tested
against the same table in the same order.

**THE TABLE IS A CHAIN, NOT A SET** (§0.16). It is evaluated strictly top to
bottom, first match wins. Read as an unordered set it is contradictory: `dhcp`
and `conflicting` both overlap `in_use`, and `stale`, `assigned` and `dormant`
all describe a documented address with no current observation. Rows 5 and 6 are
additionally DISJOINT BY PREDICATE -- Gate H appears negated in one and asserted
in the other -- so the ordering is a safety net rather than the mechanism.

**THE ENUMERATION CEILING IS ONE RULE WITH TWO APPLICATIONS** (§0.4, §0.5). An
IPv4 prefix above `Policy(module="ipam").config.enumeration_max_hosts` and
*every* IPv6 prefix are refused for next-free and the address grid, with one
error and one message. Utilisation above the ceiling reports COUNTS with a
**null** percentage -- not zero, not an estimate, null. A /8 is 16,777,214
addresses of which ~99% would carry no information; materialising them is
arithmetically hopeless, which is why §0.4 makes rows sparse and this makes the
refusal explicit rather than slow.

**`free` IS COUNTED WITH THE SAME ANTI-JOIN `next_free` SELECTS FROM.** Two
predicate sets is how a subnet comes to report 100% used while still handing out
an address; there is one query and `utilization` counts what `next_free` would
offer.

**`allocate` IS THE NON-RACING PATH** (§3.3). `next_free` is advisory -- two
operators who both call it and then both assign race for `.42` -- so `allocate`
picks and claims in ONE transaction under the same `lock_address_plan` the
subnet writes take. The lock helper is imported, never re-implemented: two
lock keys that differ by one character are two locks that never contend.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_network
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import Device, Policy
from app.modules.ipam.errors import (
    IpamConflictError,
    IpamTooLargeError,
    IpamValidationError,
)
from app.modules.ipam.models import (
    IpamAddress,
    IpamDrift,
    IpamObservation,
    IpamSubnet,
)
from app.modules.ipam.net import (
    DEFAULT_ENUMERATION_MAX_HOSTS,
    IPNetwork,
    is_enumerable,
    parse_address,
    parse_mac,
    usable_host_count,
    usable_hosts,
)
from app.modules.ipam.observations import observation_ttl_days
from app.modules.ipam.subnets import get_subnet, lock_address_plan
from app.services.client_sites import loaded_client

__all__ = [
    "ADDRESS_STATES",
    "ADDRESS_STATUSES",
    "ALLOCATION_METHODS",
    "MAX_NEXT_FREE",
    "Utilization",
    "GridEntry",
    "address_grid",
    "derive_allocation_method",
    "allocate",
    "claim_address",
    "covering_range_kinds",
    "derive_status",
    "enumeration_max_hosts",
    "is_network_or_broadcast",
    "next_free",
    "utilization",
    "validate_claimant",
]

#: §3.1's nine values, in table order. `status.ts` (§12) mirrors this list.
ADDRESS_STATUSES = (
    "dhcp",
    "conflicting",
    "in_use",
    "stale",
    "assigned",
    "dormant",
    "reserved",
    "undocumented",
    "free",
)

#: §2.6's CHECK, restated. NOT `in_use`, NOT `stale`, NOT `conflicting`: those
#: are derived, and a caller passing one to `derive_status` has read §0.6
#: backwards.
ADDRESS_STATES = ("free", "reserved", "assigned", "deprecated")

ALLOCATION_METHODS = ("static", "dhcp", "slaac", "discovered", "unknown")

#: §14.1's `origin` -> §3.2's `allocation_method`. NOT AN ALIAS TABLE: the two
#: vocabularies share three words and disagree about the other two, and the
#: disagreements are the whole content of this map.
#:
#: * `link_local` IS NOT AN ALLOCATION METHOD -- §3.2 says so in those words. An
#:   address a NIC gave itself because DHCP failed was not allocated by anybody,
#:   and §4.1's discard rule means such an address never becomes an observation
#:   at all. This entry exists for the case that rule cannot catch: an endpoint
#:   reporting `link_local` on a DOCUMENTABLE address, which is a bug on the
#:   endpoint, and the honest answer to it is "unknown" rather than a method
#:   nobody chose.
#: * `discovered` IS A LEGAL `allocation_method` AND IS NEVER PRODUCED HERE. It
#:   is what an IMPORT may carry (§8.1) -- an operator saying "this came off a
#:   scan of the old estate" -- and inventory evidence is not that. Mapping an
#:   observation to `discovered` would make the two indistinguishable in the
#:   grid, which is §0.6's mistake in a different column.
#:
#: TOTAL OVER `nic.ORIGINS` and asserted so by
#: `tests/test_ipam_allocation_method.py`: an origin the parser can produce and
#: this map has no entry for would silently read as `unknown`, which is the same
#: answer a broken NIC gets, and the two must not be confusable by accident.
ALLOCATION_METHOD_BY_ORIGIN: Mapping[str, str] = {
    "static": "static",
    "dhcp": "dhcp",
    "slaac": "slaac",
    "link_local": "unknown",
    "unknown": "unknown",
}

#: Which of the two answers `derive_allocation_method` gave. §0.6's split, in a
#: field: "an operator asserted this" and "a device reported this" are different
#: facts, and a column that renders them identically is how an operator concludes
#: the IPAM edited their record. `unknown` is a third case and not a flavour of
#: the other two -- nobody has said anything.
ALLOCATION_METHOD_SOURCES = ("documented", "observed", "unknown")


@dataclass(frozen=True)
class DerivedAllocationMethod:
    """§3.2's `allocation_method`, answered at READ TIME.

    Two fields because one would lose the fact that matters. `method` is what to
    render; `source` is who said it.
    """

    method: str
    source: str


def derive_allocation_method(
    *, documented: str | None, observed: Sequence[tuple[datetime, str]]
) -> DerivedAllocationMethod:
    """§3.2, resolved. PURE: no session, no I/O, no clock.

    THE OPERATOR WINS, AND THAT IS §0.6 RATHER THAN A PREFERENCE. `state`,
    `device_id` and `allocation_method` on `ipam_address` are what a HUMAN
    asserted, and no ingestion path may write them -- "the agent's report can
    create an address row and can never change its `state`". The same sentence
    governs this column: a device reporting `dhcp` for an address an operator
    documented as `static` does not get to rewrite the plan of record. (It is
    also not silently discarded: `source` says `documented`, so a caller that
    wants to surface the disagreement has both halves. Materialising it as a
    drift class is §4.3 work and is not in this function.)

    A STORED `unknown` IS NOT AN ASSERTION. It is the column's default -- what a
    row created by `create_address`, `reserve` or an import with no mapping gets
    -- so treating it as one would leave the observed method unreachable for
    exactly the rows §3.2 complains about. `documented=None` (no `ipam_address`
    row at all, which §0.4's sparseness makes the ordinary case) is the same.

    THE MOST RECENT OBSERVATION WINS, and only one that maps to something. A
    machine re-imaged from a DHCP lease onto a static address reports `static`
    from then on, and the old sighting is stale evidence rather than a
    contradiction. Sweep observations always carry `unknown` (ARP and ICMP see
    that an address answers, not how it was configured), so they never displace
    an inventory answer no matter how recently they ran -- which is why the
    filter is "maps to a real method", not "is the newest row".

    `observed` is `(last_seen_at, allocation_origin)` pairs and the caller is NOT
    required to sort them: an ordering contract that lives in a docstring is one
    a second caller gets wrong.
    """
    if documented is not None and documented != "unknown":
        return DerivedAllocationMethod(method=documented, source="documented")
    best: tuple[datetime, str] | None = None
    for seen_at, origin in observed:
        method = ALLOCATION_METHOD_BY_ORIGIN.get(origin, "unknown")
        if method == "unknown":
            continue
        # Ties broken on the METHOD's own text so two sightings stamped to the
        # same microsecond do not make this function's answer depend on row
        # order -- a flake that would appear once a quarter and be unreproducible.
        if best is None or (seen_at, method) > best:
            best = (seen_at, method)
    if best is not None:
        return DerivedAllocationMethod(method=best[1], source="observed")
    return DerivedAllocationMethod(method="unknown", source="unknown")


#: An operator planning a rollout asks for tens, not thousands. The cap keeps a
#: `count=1000000` from turning §0.4's bounded enumeration into an unbounded
#: response body.
MAX_NEXT_FREE = 256


def _utcnow() -> datetime:
    return datetime.now(UTC)


def derive_status(
    *,
    state: str | None,
    in_dhcp_pool: bool,
    has_current_observation: bool,
    conflicting: bool,
    gate_e: bool,
    gate_l: bool,
    gate_h: bool,
) -> str:
    """§3.1's table. PURE: no session, no I/O, no clock.

    `state=None` means there is no `ipam_address` row at all, which §0.4's
    sparseness makes the ordinary case -- most addresses in a plan have never
    been mentioned.

    The gates are §4.3's, computed once per subnet by the caller:
    * **Gate E** -- evidence available for this subnet WITHIN `W_obs`.
    * **Gate L** -- claimant liveness: no claimant, or the claimant's AGENT is
      online or seen within `W_obs`. Read from `Agent`, never from
      `Device.last_seen_at`, which means "last inventory sweep".
    * **Gate H** -- has this segment EVER been looked at (no window). It is what
      separates `dormant` ("something looked before, nothing can look now") from
      `assigned` ("nothing has ever looked, so there is nothing to be dormant
      about"). Without it every address in every never-swept subnet is dormant
      on day one and the dormant tab is the whole registry.
    """
    if state is not None and state not in ADDRESS_STATES:
        raise IpamValidationError(
            tr(
                "state must be one of {address_states} or None; got {state} -- `in_use`, "
                "`stale` and `conflicting` are DERIVED (§0.6), never stored",
                address_states=list(ADDRESS_STATES),
                state=repr(state),
            )
        )
    documented = state in ("assigned", "reserved")

    # 1. A DHCP pool takes precedence over everything below it: whatever the
    #    plan says, that address is handed out by a DHCP server.
    if in_dhcp_pool:
        return "dhcp"
    # 2. A disagreement the fleet can prove outranks anything the plan asserts.
    if conflicting:
        return "conflicting"
    # 3. Documented and answering.
    if state == "assigned" and has_current_observation:
        return "in_use"
    # 4. Documented, silent, and we CAN currently see -- so the silence means
    #    something.
    if documented and not has_current_observation and gate_e and gate_l:
        return "stale"
    # 5. S6: nothing has ever looked at this segment. Silence is not
    #    information, and the documented state is all the information there is.
    if state == "assigned" and not has_current_observation and not gate_h:
        return "assigned"
    # 6. Something looked before; nothing can look now.
    if documented and not has_current_observation and gate_h and not (gate_e and gate_l):
        return "dormant"
    # 7. Held deliberately, none of the above.
    if state == "reserved":
        return "reserved"
    # 8. Answering, and the plan does not say so.
    if state in (None, "free", "deprecated") and has_current_observation:
        return "undocumented"
    # 9. Everything else, including "no row at all".
    return "free"


async def enumeration_max_hosts(session: AsyncSession, org_id: UUID) -> int:
    """§0.4's ceiling for this org. Default 65,536; clamped, never trusted.

    The policy is operator-editable JSONB; a `0` or a string there would refuse
    every subnet in the estate, so a nonsense value falls back to the default.
    """
    config: Any = (
        await session.execute(
            select(Policy.config)
            .where(Policy.org_id == org_id, Policy.module == "ipam")
            .limit(1)
        )
    ).scalar_one_or_none()
    if not isinstance(config, dict):
        return DEFAULT_ENUMERATION_MAX_HOSTS
    raw = config.get("enumeration_max_hosts", DEFAULT_ENUMERATION_MAX_HOSTS)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return DEFAULT_ENUMERATION_MAX_HOSTS
    return raw


@dataclass(frozen=True)
class Utilization:
    """§0.4's answer at both sizes.

    Above the ceiling (and for every IPv6 prefix) `capacity`, `free`, `used` and
    `percentage` are ALL `None` and the counts still hold: reporting a
    percentage nobody can compute is worse than reporting none, because a `0.0`
    reads as "empty" rather than as "unknown".
    """

    subnet_id: UUID
    prefix: str
    enumerable: bool
    capacity: int | None
    free: int | None
    used: int | None
    percentage: float | None
    assigned: int
    reserved: int
    deprecated: int
    observed: int


def _prefix_of(subnet: IpamSubnet) -> IPNetwork:
    # asyncpg hands a `cidr` column back as an `IPv4Network`/`IPv6Network`, so
    # this is a normalisation and not a parse in the common case.
    return ip_network(str(subnet.prefix), strict=False)


def _enumeration_bounds(prefix: IPNetwork) -> tuple[int, int]:
    """The inclusive offset range `usable_hosts` would yield, as integers for
    `generate_series`. RFC 3021: `/31` and `/32` use every address; at masklen
    <= 30 the network and broadcast addresses are excluded."""
    if prefix.prefixlen >= prefix.max_prefixlen - 1:
        return 0, prefix.num_addresses - 1
    return 1, prefix.num_addresses - 2


def is_network_or_broadcast(address: str, prefix: IPNetwork) -> bool:
    """§3.2's endpoint predicate, shared with the assignment workflow.

    False for every address in a `/31` or `/32` (RFC 3021) and for IPv6, where
    there is no broadcast address and the all-zeros host is the subnet-router
    anycast address rather than a forbidden one.
    """
    parsed = parse_address(address)
    if parsed is None or parsed.version != prefix.version:
        return False
    if prefix.version == 6 or prefix.prefixlen >= prefix.max_prefixlen - 1:
        return False
    return parsed in (prefix.network_address, prefix.broadcast_address)


async def _assignable(session: AsyncSession, org_id: UUID, subnet_id: UUID) -> IpamSubnet:
    subnet = await get_subnet(session, org_id, subnet_id)
    if subnet.kind != "assignable":
        raise IpamValidationError(
            tr(
                "subnet {subnet_prefix} is a container: it exists so the tree reads and "
                "holds no addresses (§0.2). Utilisation, next-free and allocation apply "
                "only to assignable subnets",
                subnet_prefix=subnet.prefix,
            )
        )
    return subnet


async def _free_addresses(
    session: AsyncSession,
    org_id: UUID,
    subnet: IpamSubnet,
    prefix: IPNetwork,
    *,
    limit: int | None,
    count_only: bool = False,
) -> list[str] | int:
    """§3.3's anti-join. THE ONE PLACE the "is this address free" rule lives.

    A candidate is free when it is none of:
    * the network or broadcast address (handled by the offset bounds);
    * inside any `dhcp_pool`, `reserved` or `excluded` range of this subnet;
    * carrying an `ipam_address` row in state `reserved`, `assigned` or
      `deprecated` (a `free` row does NOT block -- release keeps the row, §0.4);
    * **currently observed.**

    That last clause is the reconciliation paying for itself: an address that
    answers on the wire is not free whatever the spreadsheet says, and handing
    it out is an outage. It is bounded by `W_obs`, so a machine that has been
    gone for three months does not hold its address forever.

    `generate_series` runs over the offsets rather than the addresses because
    `inet` has no series generator; the ceiling caps the row count and `LIMIT`
    short-circuits the scan.
    """
    low, high = _enumeration_bounds(prefix)
    projection = "count(*) AS free_count" if count_only else "candidate"
    tail = "" if count_only else " ORDER BY candidate"
    if limit is not None and not count_only:
        tail += " LIMIT :limit"
    statement = text(
        "WITH candidates AS ("
        "  SELECT CAST(:base AS inet) + i AS candidate"
        "  FROM generate_series(CAST(:low AS bigint), CAST(:high AS bigint)) AS i"
        ")"
        f" SELECT {projection} FROM candidates c"
        " WHERE NOT EXISTS ("
        "   SELECT 1 FROM ipam_address a"
        "   WHERE a.org_id = :org AND a.vrf_id = :vrf AND a.address = c.candidate"
        "     AND a.state IN ('reserved', 'assigned', 'deprecated'))"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM ipam_range r"
        "   WHERE r.org_id = :org AND r.subnet_id = :subnet"
        "     AND c.candidate BETWEEN r.start_address AND r.end_address)"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM ipam_observation o"
        "   WHERE o.org_id = :org AND o.vrf_id = :vrf AND o.address = c.candidate"
        "     AND o.last_seen_at >= :cutoff)"
        f"{tail}"
    )
    params: dict[str, Any] = {
        "base": str(prefix.network_address),
        "low": low,
        "high": high,
        "org": str(org_id),
        "vrf": str(subnet.vrf_id),
        "subnet": str(subnet.id),
        "cutoff": _utcnow() - timedelta(days=await observation_ttl_days(session, org_id)),
    }
    if limit is not None and not count_only:
        params["limit"] = limit
    rows = (await session.execute(statement, params)).all()
    if count_only:
        return int(rows[0].free_count)
    return [str(row.candidate) for row in rows]


async def next_free(
    session: AsyncSession, org_id: UUID, subnet_id: UUID, count: int = 1
) -> list[str]:
    """The first `count` free addresses ascending (§3.3). ADVISORY.

    Two operators who both call this and then both assign race for `.42`. The
    non-racing path is `allocate`, which picks and claims under the lock; this
    exists for preview and for planning.
    """
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_NEXT_FREE:
        raise IpamValidationError(
            tr("count must be between 1 and {max_next_free}", max_next_free=MAX_NEXT_FREE)
        )
    subnet = await _assignable(session, org_id, subnet_id)
    prefix = _prefix_of(subnet)
    if not is_enumerable(prefix, await enumeration_max_hosts(session, org_id)):
        raise IpamTooLargeError(
            tr(
                "subnet too large to enumerate: {prefix} (§0.4's ceiling, and every IPv6 "
                "prefix -- §0.5)",
                prefix=prefix,
            )
        )
    found = await _free_addresses(session, org_id, subnet, prefix, limit=count)
    assert isinstance(found, list)
    return found


async def utilization(session: AsyncSession, org_id: UUID, subnet_id: UUID) -> Utilization:
    """How full is this subnet (§0.4)?

    The counts are always real. `capacity`, `free`, `used` and `percentage` are
    `None` above the ceiling and for IPv6, because the honest answer there is
    "not computable", and every alternative -- 0, an estimate, a sample --
    renders as a number an operator would act on.
    """
    subnet = await _assignable(session, org_id, subnet_id)
    prefix = _prefix_of(subnet)
    # COUNTED BY CONTAINMENT, NOT BY `subnet_id`. The anti-join below matches
    # rows on (vrf, address) -- it has to, because `ipam_address.subnet_id` is
    # nullable (§4.2 orphans) and an observation recorded before its subnet was
    # registered carries NULL until its next sighting re-derives it. Counting
    # these by `subnet_id` while excluding by address is how `observed` reports
    # 0 for an address that `free` has already excluded, i.e. how one function
    # comes to contradict itself.
    scope = {
        "org": str(org_id),
        "vrf": str(subnet.vrf_id),
        "prefix": str(prefix),
        "cutoff": _utcnow() - timedelta(days=await observation_ttl_days(session, org_id)),
    }
    counts = (
        await session.execute(
            text(
                "SELECT"
                "  count(*) FILTER (WHERE state = 'assigned') AS assigned,"
                "  count(*) FILTER (WHERE state = 'reserved') AS reserved,"
                "  count(*) FILTER (WHERE state = 'deprecated') AS deprecated"
                " FROM ipam_address"
                " WHERE org_id = :org AND vrf_id = :vrf"
                "   AND address <<= CAST(:prefix AS inet)"
            ),
            scope,
        )
    ).one()
    observed = (
        await session.execute(
            text(
                "SELECT count(DISTINCT address) AS observed FROM ipam_observation"
                " WHERE org_id = :org AND vrf_id = :vrf"
                "   AND address <<= CAST(:prefix AS inet)"
                "   AND last_seen_at >= :cutoff"
            ),
            scope,
        )
    ).one()

    enumerable = is_enumerable(prefix, await enumeration_max_hosts(session, org_id))
    capacity: int | None = None
    free: int | None = None
    used: int | None = None
    percentage: float | None = None
    if enumerable:
        capacity = usable_host_count(prefix)
        counted = await _free_addresses(
            session, org_id, subnet, prefix, limit=None, count_only=True
        )
        assert isinstance(counted, int)
        free = counted
        used = capacity - free
        percentage = round(used * 100 / capacity, 2) if capacity else 0.0
    return Utilization(
        subnet_id=subnet.id,
        prefix=str(prefix),
        enumerable=enumerable,
        capacity=capacity,
        free=free,
        used=used,
        percentage=percentage,
        assigned=int(counts.assigned),
        reserved=int(counts.reserved),
        deprecated=int(counts.deprecated),
        observed=int(observed.observed),
    )


# ---------------------------------------------------------------------------
# Claiming an address (§3.2's predicates, shared with the assignment workflow)
# ---------------------------------------------------------------------------


def validate_claimant(device_id: UUID | None, asset_label: str) -> bool:
    """EXACTLY ONE claimant (§2.6's CHECK, restated so the operator gets a 400).

    Returns whether the claimant is an `asset_label`. Both claimants, or
    neither, is how "assigned to nothing" gets into a plan of record. The CHECK
    is what actually holds; this is the difference between a message naming the
    rule and a `ProgrammingError` naming a constraint.
    """
    has_label = bool(asset_label and asset_label.strip())
    if bool(device_id) == has_label:
        raise IpamValidationError(
            tr(
                "an assignment needs exactly one claimant: device_id XOR asset_label "
                "(both, or neither, is how 'assigned to nothing' gets into a plan of "
                "record)"
            )
        )
    return has_label


async def covering_range_kinds(
    session: AsyncSession, org_id: UUID, subnet_id: UUID, address: str
) -> set[str]:
    """The kinds of `ipam_range` covering `address` -- `dhcp_pool`, `reserved`,
    `excluded`. Empty when none does."""
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT kind FROM ipam_range"
                " WHERE org_id = :org AND subnet_id = :subnet"
                "   AND CAST(:addr AS inet) BETWEEN start_address AND end_address"
            ),
            {"org": str(org_id), "subnet": str(subnet_id), "addr": address},
        )
    ).all()
    return {row.kind for row in rows}


async def claim_address(
    session: AsyncSession,
    org_id: UUID,
    subnet: IpamSubnet,
    address: str,
    *,
    actor_id: UUID | None,
    device_id: UUID | None = None,
    asset_label: str = "",
    hostname: str = "",
    description: str = "",
    mac: str | None = None,
    allocation_method: str = "static",
    origin: str = "manual",
) -> IpamAddress:
    """Write the assignment. THE ONE WRITER of `ipam.address_assigned`.

    `allocate` (§3.3) and the assignment workflow (§3.2) both land here, so the
    §3.2 predicates -- one claimant, not a DHCP pool, not the network or
    broadcast address, no unresolved `conflict` drift, a device that resolves
    in-org -- are enforced once rather than in two places that drift apart.

    THE ACTOR IS NEVER AN AGENT. There is no ingestion path into this function;
    §0.6's split is what stops a device coming online from overwriting an
    operator's record, and it is a property of who calls this, enforced by there
    being no call from `observations.py` at all.
    """
    prefix = _prefix_of(subnet)
    parsed = parse_address(address)
    if parsed is None or parsed not in prefix:
        raise IpamValidationError(
            tr("{address} is not an address inside {prefix}", address=repr(address), prefix=prefix)
        )
    if subnet.kind != "assignable":
        raise IpamValidationError(
            tr("subnet {prefix} is a container and holds no addresses", prefix=prefix)
        )
    if allocation_method not in ALLOCATION_METHODS:
        raise IpamValidationError(
            tr(
                "allocation_method must be one of {allocation_methods}",
                allocation_methods=list(ALLOCATION_METHODS),
            )
        )

    has_label = validate_claimant(device_id, asset_label)
    if device_id is not None:
        found = (
            await session.execute(
                select(Device.id).where(Device.org_id == org_id, Device.id == device_id)
            )
        ).scalar_one_or_none()
        if found is None:
            raise IpamValidationError(tr("device {device_id} not found", device_id=device_id))
    mac_text: str | None = None
    if mac:
        mac_text = parse_mac(str(mac))
        if mac_text is None:
            raise IpamValidationError(tr("{mac} is not a valid MAC address", mac=repr(mac)))

    if is_network_or_broadcast(str(parsed), prefix):
        raise IpamValidationError(
            tr(
                "{parsed} is the network or broadcast address of {prefix} and cannot be assigned",
                parsed=parsed,
                prefix=prefix,
            )
        )
    kinds = await covering_range_kinds(session, org_id, subnet.id, str(parsed))
    if "dhcp_pool" in kinds:
        # §3.2: modelling a DHCP reservation as a static row inside a dynamic
        # pool creates a record that is true in the IPAM and false on the DHCP
        # server. A reservation is expressed by carving it OUT of the pool.
        raise IpamConflictError(
            tr(
                "{parsed} is inside a dhcp_pool range: shrink the pool or carve a reserved "
                "range out of it, then assign",
                parsed=parsed,
            )
        )
    # §3.2's last predicate. **AN ACKNOWLEDGED OR CURRENTLY SUPPRESSED FINDING
    # DOES NOT BLOCK.** Read as `resolved_at IS NULL` alone, the only way past a
    # conflict an operator has judged is to delete one of the rows that produced
    # it and wait up to 24h for the next pass -- `acknowledge` and `suppress`
    # write neither `resolved_at` nor anything else this query reads, so the
    # judgement an operator recorded, under `ipam.assign` and in the audit log,
    # bought them nothing. Both are deliberate, RBAC'd, audited acts (§11.3),
    # and suppression additionally REQUIRES a note and expires on its own, so
    # honouring them here adds a way through that is on the record rather than a
    # way around that is not.
    now = _utcnow()
    unresolved = (
        await session.execute(
            select(IpamDrift.id).where(
                IpamDrift.org_id == org_id,
                IpamDrift.vrf_id == subnet.vrf_id,
                IpamDrift.address == str(parsed),
                IpamDrift.drift_class == "conflict",
                IpamDrift.resolved_at.is_(None),
                IpamDrift.acknowledged_at.is_(None),
                or_(
                    IpamDrift.suppressed_until.is_(None),
                    IpamDrift.suppressed_until <= now,
                ),
            )
        )
    ).scalar_one_or_none()
    if unresolved is not None:
        raise IpamConflictError(
            tr(
                "{parsed} has an unresolved conflict finding: resolve, acknowledge or "
                "suppress it before assigning",
                parsed=parsed,
            )
        )

    row = (
        await session.execute(
            select(IpamAddress).where(
                IpamAddress.org_id == org_id,
                IpamAddress.vrf_id == subnet.vrf_id,
                IpamAddress.address == str(parsed),
            )
        )
    ).scalar_one_or_none()
    previous_state = row.state if row is not None else None
    if row is None:
        row = IpamAddress(
            org_id=org_id,
            vrf_id=subnet.vrf_id,
            subnet_id=subnet.id,
            address=str(parsed),
            origin=origin,
        )
        session.add(row)
    elif row.state == "assigned":
        raise IpamConflictError(
            tr("{parsed} is already assigned; release it before assigning it again", parsed=parsed)
        )
    row.subnet_id = subnet.id
    row.state = "assigned"
    row.device_id = device_id
    row.asset_label = asset_label if has_label else ""
    row.hostname = hostname
    row.description = description
    row.mac = mac_text
    row.allocation_method = allocation_method
    row.assigned_by = actor_id
    row.assigned_at = _utcnow()
    row.released_at = None
    row.updated_at = _utcnow()
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ipam.address_assigned",
        target_type="ipam_address",
        target_id=str(row.id),
        # The subnet's, as the address's is: both are its VRF's.
        client_id=subnet.client_id,
        metadata={
            "address": str(parsed),
            "subnet_id": str(subnet.id),
            "vrf_id": str(subnet.vrf_id),
            "from_state": previous_state,
            "to_state": "assigned",
            "claimant": str(device_id) if device_id else asset_label,
            "claimant_type": "device" if device_id else "asset_label",
            "allocation_method": allocation_method,
        },
    )
    await session.flush()
    await loaded_client(session, row)  # an UPDATE expired it; the response names it
    return row


async def allocate(
    session: AsyncSession,
    org_id: UUID,
    subnet_id: UUID,
    *,
    actor_id: UUID | None,
    device_id: UUID | None = None,
    asset_label: str = "",
    hostname: str = "",
    description: str = "",
    allocation_method: str = "static",
) -> IpamAddress:
    """Pick the first free address and claim it, in ONE transaction (§3.3).

    UNDER THE SAME ADVISORY LOCK the subnet writes take. Without it two
    concurrent allocations read snapshots in which the other's row does not
    exist, both pick `.1`, and one of them either loses to
    `uq_ipam_address_org_vrf_addr` or -- worse, if the loser's write were an
    UPDATE -- silently takes an address somebody else was just handed.
    """
    subnet = await _assignable(session, org_id, subnet_id)
    await lock_address_plan(session, org_id, subnet.vrf_id)
    candidates = await next_free(session, org_id, subnet_id, 1)
    if not candidates:
        raise IpamConflictError(
            tr("no free address remains in {subnet_prefix}", subnet_prefix=subnet.prefix)
        )
    return await claim_address(
        session,
        org_id,
        subnet,
        candidates[0],
        actor_id=actor_id,
        device_id=device_id,
        asset_label=asset_label,
        hostname=hostname,
        description=description,
        allocation_method=allocation_method,
    )


# ---------------------------------------------------------------------------
# The address grid (§3.1, §0.4) -- the one place `derive_status` is called with
# real inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GridEntry:
    """One row of §12's address grid. `status` is DERIVED and stored nowhere."""

    address: str
    status: str
    state: str | None
    address_id: UUID | None
    device_id: UUID | None
    asset_label: str
    mac: str | None
    hostname: str
    last_seen_at: datetime | None
    range_kinds: list[str]
    #: §3.2, DERIVED at read time exactly as `status` is (`derive_allocation_
    #: method`), and stored nowhere. `allocation_method_source` says whether an
    #: operator asserted it or the fleet reported it; rendering the first without
    #: the second is §0.6's mistake in a second column.
    allocation_method: str
    allocation_method_source: str


async def address_grid(
    session: AsyncSession,
    org_id: UUID,
    subnet_id: UUID,
    *,
    offset: int = 0,
    limit: int = 256,
    now: datetime | None = None,
) -> list[GridEntry]:
    """§3.1's table, evaluated for a page of a subnet's addresses.

    REFUSED ABOVE §0.4's CEILING AND FOR IPv6 (§0.5) -- one rule, two
    applications, one error message. An operator with a /8 gets a hierarchy,
    drift and search, not a grid that times out.

    The gates come from `reconcile.subnet_gates` / `reconcile.live_device_ids`
    rather than from a second derivation here: the grid and the drift page must
    not be able to disagree about whether a segment was looked at.
    `last_seen_at` is exposed as a timestamp for the caller to render as an
    AGE -- never as liveness (§0.10, §7).

    **`conflicting` COMES FROM `reconcile.is_conflicting`, FOR THE SAME REASON.**
    §3.1 row 2 says the grid's `conflicting` IS §4.3's conflict predicate, and
    this function used to compute its own -- `len(macs) > 1 or
    claimant_mismatch`, without the cross-VRF disjunct. Two implementations of
    one predicate is how the drift page and the address grid come to give
    different answers about the same address on the same afternoon.
    """
    from app.modules.ipam.reconcile import (
        cross_vrf_claims,
        is_conflicting,
        live_device_ids,
        subnet_gates,
    )

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1024:
        raise IpamValidationError(tr("limit must be between 1 and 1024"))
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise IpamValidationError(tr("offset must be a non-negative integer"))

    subnet = await _assignable(session, org_id, subnet_id)
    prefix = _prefix_of(subnet)
    if not is_enumerable(prefix, await enumeration_max_hosts(session, org_id)):
        raise IpamTooLargeError(
            tr(
                "subnet too large to enumerate: {prefix} (§0.4's ceiling, and every IPv6 "
                "prefix -- §0.5)",
                prefix=prefix,
            )
        )

    stamp = now or _utcnow()
    cutoff = stamp - timedelta(days=await observation_ttl_days(session, org_id))
    gates = await subnet_gates(session, org_id, now=stamp)
    gate_e, gate_h = gates.get(subnet.id, (False, False))
    live = await live_device_ids(session, org_id, now=stamp)
    cross_vrf = await cross_vrf_claims(session, org_id, now=stamp)

    documented = {
        str(row.address): row
        for row in (
            (
                await session.execute(
                    select(IpamAddress).where(
                        IpamAddress.org_id == org_id,
                        IpamAddress.vrf_id == subnet.vrf_id,
                        text("address <<= CAST(:grid_prefix AS inet)").bindparams(
                            grid_prefix=str(prefix)
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    observations: dict[str, list[IpamObservation]] = {}
    for row in (
        (
            await session.execute(
                select(IpamObservation).where(
                    IpamObservation.org_id == org_id,
                    IpamObservation.vrf_id == subnet.vrf_id,
                    IpamObservation.last_seen_at >= cutoff,
                    text("address <<= CAST(:grid_prefix AS inet)").bindparams(
                        grid_prefix=str(prefix)
                    ),
                )
            )
        )
        .scalars()
        .all()
    ):
        observations.setdefault(str(row.address), []).append(row)

    bands = (
        await session.execute(
            text(
                "SELECT kind, start_address, end_address FROM ipam_range"
                " WHERE org_id = :org AND subnet_id = :subnet"
            ),
            {"org": str(org_id), "subnet": str(subnet.id)},
        )
    ).all()
    ranges = [
        (row.kind, int(parse_address(str(row.start_address)) or 0),
         int(parse_address(str(row.end_address)) or 0))
        for row in bands
    ]

    entries: list[GridEntry] = []
    for index, candidate in enumerate(usable_hosts(prefix)):
        if index < offset:
            continue
        if len(entries) >= limit:
            break
        address = str(candidate)
        value = int(candidate)
        kinds = sorted({kind for kind, lo, hi in ranges if lo <= value <= hi})
        here = observations.get(address, [])
        doc = documented.get(address)
        macs = {o.mac for o in here if o.mac}
        conflicting = is_conflicting(
            doc, here, cross_vrf=len(cross_vrf.get(address, ())) > 1
        )
        status = derive_status(
            state=doc.state if doc is not None else None,
            in_dhcp_pool="dhcp_pool" in kinds,
            has_current_observation=bool(here),
            conflicting=conflicting,
            gate_e=gate_e,
            gate_l=doc is None or doc.device_id is None or doc.device_id in live,
            gate_h=gate_h,
        )
        allocation = derive_allocation_method(
            documented=doc.allocation_method if doc is not None else None,
            observed=[(o.last_seen_at, o.allocation_origin) for o in here],
        )
        entries.append(
            GridEntry(
                address=address,
                status=status,
                state=doc.state if doc is not None else None,
                address_id=doc.id if doc is not None else None,
                device_id=doc.device_id if doc is not None else None,
                asset_label=doc.asset_label if doc is not None else "",
                mac=doc.mac if doc is not None else next(iter(sorted(macs)), None),
                hostname=(
                    doc.hostname
                    if doc is not None and doc.hostname
                    else next((o.hostname for o in here if o.hostname), "")
                ),
                last_seen_at=max((o.last_seen_at for o in here), default=None),
                range_kinds=kinds,
                allocation_method=allocation.method,
                allocation_method_source=allocation.source,
            )
        )
    return entries
