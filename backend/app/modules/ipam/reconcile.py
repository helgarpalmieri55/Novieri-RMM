"""Reconciliation: the drift classes, the three gates, and the daily recompute
(`docs/modules/ipam/spec.md` §4.3, §4.4, §0.8, §0.11, §0.16, §11.3).

**THE GATES ARE THIS MODULE'S WHOLE THESIS.** Every IPAM that got ignored got
ignored because its drift list was mostly laptops that were asleep. Three gates
precede every absence-based class:

* **Gate E -- evidence available NOW.** A `net.sweep` run for the subnet
  succeeded within `W_obs`, or some device reported a current inventory
  observation inside it. No evidence source ran ⇒ no absence drift for the whole
  subnet. **Absence of evidence is not evidence of absence.**
* **Gate H -- evidence has EVER existed.** The same two queries with the window
  dropped (§0.16). It separates `dormant` ("something looked before, nothing can
  look now") from the never-looked-at case, which is not dormant and is not
  drift. Without it, every address in every never-swept subnet is dormant on day
  one and the dormant tab is the whole registry.
* **Gate L -- the claimant is alive.** `dev(D) IS NULL` or the claimant's agent
  is online or was seen within `W_obs`.

Note the asymmetry, because it is the idea: **silence is not information, but a
live device that disagrees with the plan is.**

**GATE E AND GATE L READ `Agent`, NEVER `Device`.** `Device.last_seen_at` is
written only by inventory ingestion (`app/modules/inventory/ingest.py`) and by
enrollment, so it means "last inventory sweep", not "last contact". An
implementation reading it marks every connected-but-not-recently-swept machine
dormant -- the exact noise §0.11 exists to eliminate. `tests/ipam_fixtures.py`
carries `seed_agent` for this, and the test that pins it gives an ONLINE agent a
sixty-day-old `Device.last_seen_at`.

**`conflict` IS BEHIND A GATE TOO, AND ITS GATE IS INSIDE THE PREDICATE.** Two
of its three disjuncts are presence-based -- two MACs answering, or an inventory
row that disagrees -- so the evidence is the finding. The third is purely
documentary: two `ipam_address` rows in different VRFs. §4.3 gates that one with
"**and those VRFs share a swept segment**", and `_cross_vrf_claims` is where that
half lives. Dropped, it fires on two unrelated routing domains that both use
`10.0.0.0/24` -- the ordinary reason VRFs exist -- and §3.2's assignment check
then makes the false positive a permanent block on that address in BOTH VRFs.

**NOTHING HERE EVER CHANGES `ipam_address.state`** (§4.3). `documented_absent` is
a reclaim *candidate*; automatic reclaim is on the never list.

**`dormant` IS NOT A DRIFT CLASS.** It is materialised, because it needs the same
stable identity to be acknowledgeable and to render in its own tab, and it is
excluded from `DRIFT_CLASSES`, from `list_drift`'s default, from the counts and
from events. "37 addresses we cannot currently verify" is worth knowing and is
not the same information as "37 addresses are wrong".

**RECOMPUTATION UPSERTS ON A STABLE KEY** (§0.8) -- `(org_id, vrf_id, address,
drift_class)`, a TOTAL unique index, so plain `index_elements` inference works.
`first_detected_at`, `acknowledged_by/at`, `suppressed_until` and `note` are
never in the `set_` clause: an un-acknowledgeable list is a list nobody opens
twice, and a delete-and-reinsert would throw every acknowledgement away every
night. A resolved item is **stamped, not deleted**, which is what makes "we
closed 40 address-plan defects this quarter" an evidence export rather than an
anecdote.

**ONE EVENT PER ITEM, ON THE SECOND CONSECUTIVE DETECTION ONLY** (§0.8). One
sweep that ran while a switch was rebooting is not news. No aggregate shape, so a
notification rule can route `conflict` differently from `documented_absent`.

**GATE E AND GATE H MATCH OBSERVATIONS BY CONTAINMENT, NOT BY `subnet_id`.** The
spec writes both gates as `subnet_id = S.id`; this uses
`o.vrf_id = S.vrf_id AND o.address <<= S.prefix`, which agrees with it on every
row where `subnet_id` is set and additionally catches the case
`allocation.utilization` already guards against in the same words: an observation
recorded before its subnet was registered carries `subnet_id IS NULL` until its
next sighting re-derives it. Counting one way here and another way there is how
two functions come to contradict each other about the same segment.
"""

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_network
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql import text

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.events import publish
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import Agent, Organization
from app.modules.ipam.clients import loaded_client
from app.modules.ipam.errors import IpamNotFoundError, IpamValidationError
from app.modules.ipam.models import (
    IpamAddress,
    IpamDrift,
    IpamObservation,
    IpamRange,
    IpamSubnet,
)
from app.modules.ipam.net import IPNetwork, parse_address
from app.modules.ipam.observations import observation_ttl_days

__all__ = [
    "DORMANT_CLASS",
    "DRIFT_CLASSES",
    "RECONCILE_INTERVAL_SECONDS",
    "ReconcileStats",
    "acknowledge_drift",
    "cross_vrf_claims",
    "evaluate_org",
    "is_conflicting",
    "list_drift",
    "live_device_ids",
    "mismatched_claimant",
    "reconcile_sweep_all_orgs",
    "subnet_gates",
    "suppress_drift",
]

#: §4.4's beat cadence. A project constant rather than a literal in
#: `app/worker.py`, so the beat entry and the test cannot drift apart.
RECONCILE_INTERVAL_SECONDS = 86400.0

logger = logging.getLogger(__name__)

#: §4.3's table, in spec order. `dormant` is DELIBERATELY ABSENT: it is a bucket,
#: not a finding, and every count, export and event in this module is derived
#: from this tuple rather than from a hand-maintained second list.
DRIFT_CLASSES = (
    "documented_absent",
    "present_undocumented",
    "mac_mismatch",
    "claimant_mismatch",
    "device_moved",
    "conflict",
)
DORMANT_CLASS = "dormant"

_SEVERITY = {
    "documented_absent": "medium",
    "present_undocumented": "low",
    "mac_mismatch": "medium",
    "claimant_mismatch": "high",
    "device_moved": "medium",
    "conflict": "high",
    DORMANT_CLASS: "low",
}

#: §2.7's ordering, so "confidence >= medium" is a comparison rather than a
#: hand-written pair of equality tests at each call site.
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

_DOCUMENTED = ("assigned", "reserved")
_UNDOCUMENTED = ("free", "deprecated")
_MAX_NOTE = 2000


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ReconcileStats:
    """What one pass did. `detected` and `dormant` are separate ON PURPOSE --
    §4.3's whole point is that they are different information."""

    detected: int = 0
    dormant: int = 0
    resolved: int = 0
    published: int = 0


@dataclass(frozen=True)
class _Finding:
    vrf_id: UUID
    address: str
    drift_class: str
    confidence: str
    detail: dict[str, Any]
    subnet_id: UUID | None
    address_id: UUID | None
    device_id: UUID | None


def _best_confidence(rows: list[IpamObservation]) -> str:
    return max(
        (row.confidence for row in rows), key=lambda c: _CONFIDENCE_RANK[c], default="low"
    )


class _Segments:
    """The registry, indexed for containment. Built once per pass.

    Most-specific-match, because a `/24` inside a `/16` container is the subnet
    an address belongs to and the container is the tree it hangs from (§0.2).
    """

    def __init__(self, subnets: list[IpamSubnet]) -> None:
        self.by_id = {row.id: row for row in subnets}
        self._by_vrf: dict[UUID, list[tuple[IPNetwork, IpamSubnet]]] = defaultdict(list)
        for row in subnets:
            self._by_vrf[row.vrf_id].append(
                (ip_network(str(row.prefix), strict=False), row)
            )
        for entries in self._by_vrf.values():
            entries.sort(key=lambda pair: pair[0].prefixlen, reverse=True)

    def containing(self, vrf_id: UUID, address: str) -> IpamSubnet | None:
        parsed = parse_address(address)
        if parsed is None:
            return None
        for network, row in self._by_vrf.get(vrf_id, ()):
            if parsed.version == network.version and parsed in network:
                return row
        return None


# --------------------------------------------------------------------------- #
# §4.3's `conflict` predicate -- ONE definition, three disjuncts
# --------------------------------------------------------------------------- #


def mismatched_claimant(
    doc: IpamAddress | None, observations: Sequence[IpamObservation]
) -> IpamObservation | None:
    """§4.3's `claimant_mismatch`: the observation that disagrees, or `None`.

    Returns the ROW rather than a boolean because the finding's detail payload
    quotes the observed `device_id`, and a caller that only needs the predicate
    reads `is not None`. Only `source='inventory'` evidence can say this: a
    sweep sees addresses and MACs, never identity (§6.4).
    """
    if doc is None or doc.device_id is None:
        return None
    return next(
        (
            row
            for row in observations
            if row.source == "inventory"
            and row.device_id is not None
            and row.device_id != doc.device_id
        ),
        None,
    )


def is_conflicting(
    doc: IpamAddress | None,
    observations: Sequence[IpamObservation],
    *,
    cross_vrf: bool,
) -> bool:
    """§4.3's `conflict` predicate, and §3.1 row 2's `conflicting` status.

    THE GRID AND THE DRIFT PAGE READ THIS FUNCTION, NOT A COPY OF IT. They were
    two implementations that disagreed about the third disjunct, so the address
    grid and the drift list gave different answers about the same address on the
    same afternoon -- the same failure `scope_digest` and `subnet_gates` are
    single-implementation to avoid.
    """
    macs = {row.mac for row in observations if row.mac}
    return (
        len(macs) > 1
        or mismatched_claimant(doc, observations) is not None
        or cross_vrf
    )


def _cross_vrf_claims(
    claims: Iterable[IpamAddress],
    segments: _Segments,
    gate_h: Mapping[UUID, bool],
) -> dict[str, set[UUID]]:
    """§4.3's third disjunct: `{address: the VRFs that claim it across one
    shared, swept segment}`.

    **THE SECOND HALF OF THE CLAUSE IS THE WHOLE CLAUSE.** §4.3 reads "≥2
    `ipam_address` rows in different VRFs claim `A` **and those VRFs share a
    swept segment**". Read as `len(vrfs) > 1` it fires on two unrelated routing
    domains that both use `10.0.0.0/24`, which is the ORDINARY reason VRFs
    exist -- a high-severity finding with an empty evidence payload, on a
    switched-off printer, which §3.2 then turns into a permanent block on
    assigning that address in EITHER VRF.

    WHAT "SHARE A SWEPT SEGMENT" MEANS HERE, deliberately:

    * **A shared segment is a shared `ipam_vlan` row.** A VLAN is this schema's
      name for one broadcast domain (§2.3), so two subnets in different VRFs
      carrying the same `vlan_id` are the same wire, and one address presented
      on it twice is a real duplicate-IP defect. `site_id` is rejected as the
      key: a building is not a segment, and two VRFs at one site are the common
      case, not the defect. A subnet with no VLAN recorded shares no segment
      with anything -- the module has not been told it is on one, and inventing
      the relationship from an overlapping prefix would restore exactly the
      false positive above.
    * **"Swept" is Gate H on that segment** -- some subnet on the VLAN has ever
      had a succeeded sweep run or an observation inside it (§0.16). Two rows in
      a table that nothing has ever verified are a documentation question, not a
      high-severity duplicate-address finding, and without this every org that
      registers one VLAN in two VRFs before turning sweeps on gets the finding
      on day one.

    Only `assigned` rows count as claims: `reserved`, `free` and `deprecated`
    are not two devices holding one address.
    """
    per_segment: dict[str, dict[UUID, set[UUID]]] = defaultdict(lambda: defaultdict(set))
    swept: set[UUID] = set()
    for claim in claims:
        if claim.state != "assigned":
            continue
        subnet = segments.containing(claim.vrf_id, str(claim.address))
        if subnet is None or subnet.vlan_id is None:
            continue
        per_segment[str(claim.address)][subnet.vlan_id].add(claim.vrf_id)
        if gate_h.get(subnet.id, False):
            swept.add(subnet.vlan_id)

    out: dict[str, set[UUID]] = {}
    for address, by_vlan in per_segment.items():
        vrfs = {
            vrf_id
            for vlan_id, claiming in by_vlan.items()
            if vlan_id in swept and len(claiming) > 1
            for vrf_id in claiming
        }
        if vrfs:
            out[address] = vrfs
    return out


async def cross_vrf_claims(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> dict[str, set[UUID]]:
    """`_cross_vrf_claims` for callers outside the reconcile pass (§3.1).

    The CANDIDATE SET IS NARROWED IN SQL to addresses claimed in more than one
    VRF, because that set is nearly always empty and the address grid must not
    load an org's whole `ipam_address` table to render one subnet. The predicate
    itself is the same function the reconcile pass calls: an address claimed in
    one VRF can never satisfy `len(claiming) > 1`, so the narrowed input and the
    full input produce identical output.
    """
    candidates = [
        row.address
        for row in (
            await session.execute(
                text(
                    "SELECT address FROM ipam_address"
                    " WHERE org_id = :org AND state = 'assigned'"
                    " GROUP BY address HAVING count(DISTINCT vrf_id) > 1"
                ),
                {"org": str(org_id)},
            )
        ).all()
    ]
    if not candidates:
        return {}
    claims = list(
        (
            await session.execute(
                select(IpamAddress).where(
                    IpamAddress.org_id == org_id,
                    IpamAddress.state == "assigned",
                    IpamAddress.address.in_([str(value) for value in candidates]),
                )
            )
        )
        .scalars()
        .all()
    )
    subnets = list(
        (
            await session.execute(select(IpamSubnet).where(IpamSubnet.org_id == org_id))
        )
        .scalars()
        .all()
    )
    stamp = now or _utcnow()
    cutoff = stamp - timedelta(days=await observation_ttl_days(session, org_id))
    _, gate_h = await _gates(session, org_id, cutoff)
    return _cross_vrf_claims(claims, _Segments(subnets), gate_h)


async def _gates(
    session: AsyncSession, org_id: UUID, cutoff: datetime
) -> tuple[dict[UUID, bool], dict[UUID, bool]]:
    """`(gate_e, gate_h)` per subnet id.

    ONE QUERY PER EVIDENCE SOURCE, both aggregating in SQL: the row set is
    "every observation and every sweep run this org has", which is unbounded by
    anything the caller controls, and the answer is two booleans per subnet.

    Gate H is Gate E's query with the `W_obs` predicate dropped, computed in the
    same pass -- it is not a fourth thing to keep in sync.
    """
    observed = (
        await session.execute(
            text(
                "SELECT s.id AS subnet_id,"
                "  count(*) FILTER (WHERE o.id IS NOT NULL) AS ever,"
                "  count(*) FILTER (WHERE o.source = 'inventory'"
                "                     AND o.last_seen_at >= :cutoff) AS recent"
                " FROM ipam_subnet s"
                " LEFT JOIN ipam_observation o"
                "   ON o.org_id = s.org_id AND o.vrf_id = s.vrf_id"
                "  AND o.address <<= s.prefix"
                " WHERE s.org_id = :org"
                " GROUP BY s.id"
            ),
            {"org": str(org_id), "cutoff": cutoff},
        )
    ).all()
    swept = (
        await session.execute(
            text(
                "SELECT subnet_id,"
                "  count(*) FILTER (WHERE state = 'succeeded') AS ever,"
                "  count(*) FILTER (WHERE state = 'succeeded'"
                "                     AND started_at >= :cutoff) AS recent"
                " FROM ipam_sweep_run WHERE org_id = :org GROUP BY subnet_id"
            ),
            {"org": str(org_id), "cutoff": cutoff},
        )
    ).all()

    gate_e: dict[UUID, bool] = {}
    gate_h: dict[UUID, bool] = {}
    for row in observed:
        gate_e[row.subnet_id] = row.recent > 0
        gate_h[row.subnet_id] = row.ever > 0
    for row in swept:
        gate_e[row.subnet_id] = gate_e.get(row.subnet_id, False) or row.recent > 0
        gate_h[row.subnet_id] = gate_h.get(row.subnet_id, False) or row.ever > 0
    return gate_e, gate_h


async def live_device_ids(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> set[UUID]:
    """Gate L's input, for callers outside the reconcile pass.

    §3.1's derived `status` needs the same liveness the drift predicates need,
    and the address grid must not grow a SECOND definition of "is this claimant
    alive" -- two that drift is how the grid and the drift page come to disagree
    about the same address in the same afternoon.
    """
    stamp = now or _utcnow()
    return await _live_devices(
        session,
        org_id,
        stamp - timedelta(days=await observation_ttl_days(session, org_id)),
    )


async def subnet_gates(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> dict[UUID, tuple[bool, bool]]:
    """`{subnet_id: (gate_e, gate_h)}` -- the same two queries §4.3 and §0.16
    define, exported so §3.1's `status` derivation reads the gates rather than
    re-deriving them."""
    stamp = now or _utcnow()
    cutoff = stamp - timedelta(days=await observation_ttl_days(session, org_id))
    gate_e, gate_h = await _gates(session, org_id, cutoff)
    return {
        subnet_id: (gate_e.get(subnet_id, False), gate_h.get(subnet_id, False))
        for subnet_id in set(gate_e) | set(gate_h)
    }


async def _live_devices(
    session: AsyncSession, org_id: UUID, cutoff: datetime
) -> set[UUID]:
    """Devices whose AGENT is online or was seen within `W_obs` (§0.11).

    Reads `agents`, never `devices`. See this module's docstring for what
    reading `Device.last_seen_at` would do instead.
    """
    rows = (
        await session.execute(
            select(Agent.device_id, Agent.status, Agent.last_seen_at).where(
                Agent.org_id == org_id
            )
        )
    ).all()
    return {
        row.device_id
        for row in rows
        if row.status == "online"
        or (row.last_seen_at is not None and row.last_seen_at >= cutoff)
    }


async def _ranges(
    session: AsyncSession, org_id: UUID
) -> dict[UUID, list[tuple[str, int, int]]]:
    """Per subnet, `(kind, start, end)` as integers -- containment is an integer
    comparison once the endpoints are parsed, and doing it in Python keeps the
    whole evaluation to a fixed number of queries."""
    rows = (
        (
            await session.execute(
                select(IpamRange).where(IpamRange.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    out: dict[UUID, list[tuple[str, int, int]]] = defaultdict(list)
    for row in rows:
        start = parse_address(str(row.start_address))
        end = parse_address(str(row.end_address))
        if start is None or end is None:  # pragma: no cover - CHECKed at write time
            continue
        out[row.subnet_id].append((row.kind, int(start), int(end)))
    return out


def _in_range_kind(
    ranges: list[tuple[str, int, int]], address: str, kinds: tuple[str, ...]
) -> bool:
    parsed = parse_address(address)
    if parsed is None:  # pragma: no cover - every stored address parses
        return False
    value = int(parsed)
    return any(kind in kinds and start <= value <= end for kind, start, end in ranges)


async def _evaluate(
    session: AsyncSession, org_id: UUID, now: datetime
) -> tuple[list[_Finding], list[_Finding]]:
    """The pure-ish derivation: `(findings, dormant)`.

    Split from the persistence below so the predicates can be read in one place
    without the upsert machinery in the way.
    """
    ttl_days = await observation_ttl_days(session, org_id)
    cutoff = now - timedelta(days=ttl_days)

    subnets = list(
        (
            await session.execute(
                select(IpamSubnet).where(IpamSubnet.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    segments = _Segments(subnets)
    gate_e, gate_h = await _gates(session, org_id, cutoff)
    live = await _live_devices(session, org_id, cutoff)
    ranges = await _ranges(session, org_id)

    addresses = list(
        (
            await session.execute(
                select(IpamAddress).where(IpamAddress.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    current = list(
        (
            await session.execute(
                select(IpamObservation).where(
                    IpamObservation.org_id == org_id,
                    IpamObservation.last_seen_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    documented: dict[tuple[UUID, str], IpamAddress] = {
        (row.vrf_id, str(row.address)): row for row in addresses
    }
    observed: dict[tuple[UUID, str], list[IpamObservation]] = defaultdict(list)
    for row in current:
        observed[(row.vrf_id, str(row.address))].append(row)
    inventory_by_device: dict[UUID, list[IpamObservation]] = defaultdict(list)
    for row in current:
        if row.source == "inventory" and row.device_id is not None:
            inventory_by_device[row.device_id].append(row)

    # §4.3's cross-VRF clause, with BOTH halves -- see `_cross_vrf_claims` for
    # what "share a swept segment" means in code and why dropping it made
    # `10.0.0.5` in a customer's VRF and in ours, two entirely different
    # networks, a high-severity finding.
    claims_by_address = _cross_vrf_claims(addresses, segments, gate_h)

    findings: list[_Finding] = []
    dormant: list[_Finding] = []
    moved_from: set[tuple[UUID, str]] = set()
    moved_to: set[tuple[UUID, str]] = set()
    moved_detail: dict[tuple[UUID, str], _Finding] = {}

    # ---- pass 1: `device_moved`, because it SUPPRESSES two other classes ----
    for key, doc_row in documented.items():
        vrf_id, address = key
        if doc_row.device_id is None or doc_row.state not in _DOCUMENTED:
            continue
        here = observed.get(key, [])
        if any(o.device_id == doc_row.device_id for o in here):
            continue
        for elsewhere in inventory_by_device.get(doc_row.device_id, []):
            other_address = str(elsewhere.address)
            if other_address == address:
                continue
            target_subnet = segments.containing(elsewhere.vrf_id, other_address)
            if target_subnet is None:
                continue
            moved_from.add(key)
            moved_to.add((elsewhere.vrf_id, other_address))
            moved_detail[key] = _Finding(
                vrf_id=vrf_id,
                address=address,
                drift_class="device_moved",
                confidence="high",
                detail={
                    "documented_at": address,
                    "observed_at": other_address,
                    "device_id": str(doc_row.device_id),
                    "observed_subnet_id": str(target_subnet.id),
                },
                subnet_id=doc_row.subnet_id
                or (
                    home.id
                    if (home := segments.containing(vrf_id, address)) is not None
                    else None
                ),
                address_id=doc_row.id,
                device_id=doc_row.device_id,
            )
            break
    findings.extend(moved_detail.values())

    # ---- pass 2: everything else ----
    for key in set(documented) | set(observed):
        vrf_id, address = key
        doc = documented.get(key)
        here = observed.get(key, [])
        subnet = None
        if doc is not None and doc.subnet_id is not None:
            subnet = segments.by_id.get(doc.subnet_id)
        if subnet is None:
            subnet = segments.containing(vrf_id, address)
        subnet_id = subnet.id if subnet is not None else None
        e = gate_e.get(subnet_id, False) if subnet_id is not None else False
        h = gate_h.get(subnet_id, False) if subnet_id is not None else False
        claimant_live = doc is None or doc.device_id is None or doc.device_id in live

        macs = sorted({o.mac for o in here if o.mac})
        other_obs = mismatched_claimant(doc, here)
        claimant_mismatch = other_obs is not None
        cross_vrf_vrfs = claims_by_address.get(address, set())

        if other_obs is not None:
            assert doc is not None  # noqa: S101 - narrowed by `mismatched_claimant`
            findings.append(
                _Finding(
                    vrf_id, address, "claimant_mismatch", "high",
                    {
                        "documented_device_id": str(doc.device_id),
                        "observed_device_id": str(other_obs.device_id),
                    },
                    subnet_id,
                    doc.id if doc is not None else None,
                    doc.device_id if doc is not None else None,
                )
            )
        if is_conflicting(doc, here, cross_vrf=bool(cross_vrf_vrfs)):
            findings.append(
                _Finding(
                    vrf_id, address, "conflict", "high",
                    {
                        "macs": macs,
                        "claimant_mismatch": claimant_mismatch,
                        "vrf_count": len(cross_vrf_vrfs),
                    },
                    subnet_id,
                    doc.id if doc is not None else None,
                    doc.device_id if doc is not None else None,
                )
            )
        if doc is not None and doc.mac:
            offending = [
                o
                for o in here
                if o.mac
                and o.mac != doc.mac
                and _CONFIDENCE_RANK[o.confidence] >= _CONFIDENCE_RANK["medium"]
            ]
            if offending:
                findings.append(
                    _Finding(
                        vrf_id, address, "mac_mismatch",
                        _best_confidence(offending),
                        {
                            "documented_mac": doc.mac,
                            "observed_macs": sorted({o.mac for o in offending if o.mac}),
                        },
                        subnet_id, doc.id, doc.device_id,
                    )
                )

        if doc is not None and doc.state in _DOCUMENTED and not here:
            if e and claimant_live:
                if key not in moved_from:
                    findings.append(
                        _Finding(
                            vrf_id, address, "documented_absent",
                            "high" if doc.device_id is not None else "medium",
                            {
                                "state": doc.state,
                                "device_id": str(doc.device_id) if doc.device_id else None,
                                "asset_label": doc.asset_label,
                                "gate_e": True,
                                "gate_l": True,
                            },
                            subnet_id, doc.id, doc.device_id,
                        )
                    )
            elif h:
                dormant.append(
                    _Finding(
                        vrf_id, address, DORMANT_CLASS, "low",
                        {
                            "state": doc.state,
                            "device_id": str(doc.device_id) if doc.device_id else None,
                            "gate_e": e,
                            "gate_l": claimant_live,
                        },
                        subnet_id, doc.id, doc.device_id,
                    )
                )

        if here and (doc is None or doc.state in _UNDOCUMENTED):
            in_pool = subnet is not None and _in_range_kind(
                ranges.get(subnet.id, []), address, ("dhcp_pool", "excluded")
            )
            registered = subnet is not None and subnet.kind == "assignable"
            if registered and not in_pool and key not in moved_to:
                findings.append(
                    _Finding(
                        vrf_id, address, "present_undocumented",
                        _best_confidence(here),
                        {
                            "macs": macs,
                            "sources": sorted({o.source for o in here}),
                            "hostnames": sorted({o.hostname for o in here if o.hostname}),
                        },
                        subnet_id,
                        doc.id if doc is not None else None,
                        None,
                    )
                )

    return findings, dormant


async def evaluate_org(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> ReconcileStats:
    """Recompute every class for one org and materialise `ipam_drift` (§4.4)."""
    stamp = now or _utcnow()
    findings, dormant_rows = await _evaluate(session, org_id, stamp)

    subnets = {
        row.id: row
        for row in (
            (
                await session.execute(
                    select(IpamSubnet).where(IpamSubnet.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
    }
    published = 0
    seen: set[tuple[UUID, str, str]] = set()
    for finding in [*findings, *dormant_rows]:
        key = (finding.vrf_id, finding.address, finding.drift_class)
        if key in seen:
            continue
        seen.add(key)
        statement = pg_insert(IpamDrift).values(
            org_id=org_id,
            vrf_id=finding.vrf_id,
            subnet_id=finding.subnet_id,
            address=finding.address,
            drift_class=finding.drift_class,
            severity=_SEVERITY[finding.drift_class],
            confidence=finding.confidence,
            detail=finding.detail,
            address_id=finding.address_id,
            device_id=finding.device_id,
            first_detected_at=stamp,
            last_evaluated_at=stamp,
            consecutive_detections=1,
        )
        row = (
            await session.execute(
                statement.on_conflict_do_update(
                    # `uq_ipam_drift_key` is TOTAL, so plain inference works and
                    # no `index_where` is needed (unlike this module's partial
                    # indexes -- §0.15).
                    index_elements=["org_id", "vrf_id", "address", "drift_class"],
                    set_={
                        # NOT `first_detected_at`, NOT `acknowledged_by/at`, NOT
                        # `suppressed_until`, NOT `note`: §0.8's stable identity
                        # is exactly the promise that those survive a recompute.
                        "subnet_id": statement.excluded.subnet_id,
                        "severity": statement.excluded.severity,
                        "confidence": statement.excluded.confidence,
                        "detail": statement.excluded.detail,
                        "address_id": statement.excluded.address_id,
                        "device_id": statement.excluded.device_id,
                        "last_evaluated_at": statement.excluded.last_evaluated_at,
                        "resolved_at": None,
                        "consecutive_detections": IpamDrift.consecutive_detections + 1,
                    },
                ).returning(
                    IpamDrift.id,
                    IpamDrift.consecutive_detections,
                    IpamDrift.acknowledged_at,
                    IpamDrift.suppressed_until,
                )
            )
        ).one()

        if finding.drift_class == DORMANT_CLASS:
            continue
        suppressed = row.suppressed_until is not None and row.suppressed_until > stamp
        if row.consecutive_detections == 2 and row.acknowledged_at is None and not suppressed:
            subnet = subnets.get(finding.subnet_id) if finding.subnet_id else None
            await publish(
                session,
                org_id=org_id,
                event_type="ipam.drift_detected",
                subject_type="ipam_drift",
                subject_id=row.id,
                payload={
                    "drift_id": str(row.id),
                    "drift_class": finding.drift_class,
                    "severity": _SEVERITY[finding.drift_class],
                    "confidence": finding.confidence,
                    "address": finding.address,
                    "subnet_id": str(finding.subnet_id) if finding.subnet_id else None,
                    "subnet_prefix": str(subnet.prefix) if subnet else None,
                    "subnet_name": subnet.name if subnet else None,
                    "device_id": str(finding.device_id) if finding.device_id else None,
                },
                actor_user_id=None,
                client_id=None,  # plan 2026-10-26 Task 11 sets the subnet's client
            )
            published += 1

    resolved = 0
    open_rows = (
        (
            await session.execute(
                select(IpamDrift).where(
                    IpamDrift.org_id == org_id, IpamDrift.resolved_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    for existing in open_rows:
        if (existing.vrf_id, str(existing.address), existing.drift_class) in seen:
            continue
        # STAMPED, NOT DELETED (§0.8). The counter goes to zero so a
        # re-detection starts its two-pass count over rather than firing an
        # event off a stale total.
        existing.resolved_at = stamp
        existing.consecutive_detections = 0
        existing.last_evaluated_at = stamp
        session.add(existing)
        resolved += 1
    await session.flush()
    return ReconcileStats(
        detected=len(
            {(f.vrf_id, f.address, f.drift_class) for f in findings}
        ),
        dormant=len({(f.vrf_id, f.address) for f in dormant_rows}),
        resolved=resolved,
        published=published,
    )


async def list_drift(
    session: AsyncSession,
    org_id: UUID,
    *,
    drift_class: str | None = None,
    severity: str | None = None,
    subnet_id: UUID | None = None,
    include_resolved: bool = False,
) -> list[IpamDrift]:
    """§9's `/drift`. **`dormant` is excluded unless asked for by name** -- it is
    not drift, and a list that mixed them would be the list §0.11 says nobody
    opens twice."""
    query = select(IpamDrift).where(IpamDrift.org_id == org_id)
    if drift_class is not None:
        if drift_class not in (*DRIFT_CLASSES, DORMANT_CLASS):
            raise IpamValidationError(
                tr(
                    "drift_class must be one of {drift_classes}",
                    drift_classes=[*DRIFT_CLASSES, DORMANT_CLASS],
                )
            )
        query = query.where(IpamDrift.drift_class == drift_class)
    else:
        query = query.where(IpamDrift.drift_class.in_(DRIFT_CLASSES))
    if severity is not None:
        query = query.where(IpamDrift.severity == severity)
    if subnet_id is not None:
        query = query.where(IpamDrift.subnet_id == subnet_id)
    if not include_resolved:
        query = query.where(IpamDrift.resolved_at.is_(None))
    return list(
        (await session.execute(query.order_by(IpamDrift.address, IpamDrift.drift_class)))
        .scalars()
        .all()
    )


async def _get_drift(session: AsyncSession, org_id: UUID, drift_id: UUID) -> IpamDrift:
    row = (
        await session.execute(
            select(IpamDrift).where(IpamDrift.org_id == org_id, IpamDrift.id == drift_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise IpamNotFoundError(tr("drift {drift_id} not found", drift_id=drift_id))
    return row


async def acknowledge_drift(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID | None,
    drift_id: UUID,
    note: str = "",
) -> IpamDrift:
    """"I have seen this and it is fine." Requires `ipam.assign` at the API and
    is audited (§11.3); survives every recompute (§0.8)."""
    if not isinstance(note, str) or len(note) > _MAX_NOTE:
        raise IpamValidationError(
            tr("note must be a string of at most {max_note} characters", max_note=_MAX_NOTE)
        )
    row = await _get_drift(session, org_id, drift_id)
    row.acknowledged_by = actor_id
    row.acknowledged_at = _utcnow()
    if note:
        row.note = note
    session.add(row)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ipam.drift_acknowledged",
        target_type="ipam_drift",
        target_id=str(row.id),
        # Read back: the UPDATE expired the trigger-written client.
        client_id=await loaded_client(session, row),
        metadata={
            "drift_class": row.drift_class,
            "address": str(row.address),
            "note": note,
        },
    )
    await session.flush()
    return row


async def suppress_drift(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID | None,
    drift_id: UUID,
    until: datetime,
    note: str,
    now: datetime | None = None,
) -> IpamDrift:
    """Time-boxed silence, with a REQUIRED note (§4.4).

    Muting a finding without saying why is how a drift list becomes a list of
    things somebody once silenced. The note is checked before anything is
    written, so a refused suppression leaves the row exactly as it was.
    """
    if not isinstance(note, str) or not note.strip():
        raise IpamValidationError(
            tr(
                "a note is required to suppress a finding: a silenced item with no reason "
                "is indistinguishable from one nobody looked at"
            )
        )
    if len(note) > _MAX_NOTE:
        raise IpamValidationError(
            tr("note must be at most {max_note} characters", max_note=_MAX_NOTE)
        )
    if until.tzinfo is None:
        raise IpamValidationError(tr("suppressed_until must be timezone-aware"))
    if until <= (now or _utcnow()):
        raise IpamValidationError(tr("suppressed_until must be in the future"))
    row = await _get_drift(session, org_id, drift_id)
    row.suppressed_until = until
    row.note = note
    session.add(row)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ipam.drift_suppressed",
        target_type="ipam_drift",
        target_id=str(row.id),
        # Read back: the UPDATE expired the trigger-written client.
        client_id=await loaded_client(session, row),
        metadata={
            "drift_class": row.drift_class,
            "address": str(row.address),
            "suppressed_until": until.isoformat(),
            "note": note,
        },
    )
    await session.flush()
    return row


async def reconcile_sweep_all_orgs(
    *,
    engine: AsyncEngine,
    admin_engine: AsyncEngine,
    now: datetime | None = None,
) -> int:
    """§4.4's daily pass: one `org_session` per org, enumerated on the admin
    engine. Returns the total number of findings detected across the fleet.

    **ONE UNSWEEPABLE ORG MUST NEVER SKIP EVERY ORG QUEUED BEHIND IT.** On a
    fleet where org #2 of 300 has a row that breaks evaluation, the alternative
    is that 298 orgs are never reconciled again and the only symptom is a log
    line. This is `app/modules/health/sweep.py::_for_each_org`'s reasoning and
    its shape; the tenant GUC is `SET LOCAL`, so one org is the largest unit
    that can commit anyway.

    Nothing here publishes to Redis or dispatches a job: reconciliation reads
    evidence and writes findings. The only outbound effect is the
    `ipam.drift_detected` domain event, written to the kernel's transactional
    outbox on the org's own transaction (§10) -- so an event can never describe
    a finding that rolled back.
    """
    stamp = now or _utcnow()
    async with AsyncSession(admin_engine) as session:
        org_ids = list((await session.execute(select(Organization.id))).scalars().all())

    total = 0
    for org_id in org_ids:
        try:
            async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
                stats = await evaluate_org(session, org_id, now=stamp)
        except Exception:  # noqa: BLE001 - contained by design, see the docstring
            logger.error(
                "ipam.reconcile: evaluating org %s failed; its pass is skipped and "
                "every other org is still reconciled",
                org_id,
                exc_info=True,
            )
            continue
        total += stats.detected
    return total
