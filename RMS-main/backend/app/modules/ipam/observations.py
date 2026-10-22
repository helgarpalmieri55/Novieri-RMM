"""What the fleet reported: observation ingestion, currency, and orphans
(`docs/modules/ipam/spec.md` §4.1, §4.2, §0.6, §0.10, §0.15, §0.17).

**THIS MODULE WRITES `ipam_observation` ROWS AND NOTHING ELSE.** It cannot write
`ipam_address.state`, `device_id` or `assigned_by`, and there is no code path
here that could -- §0.6's split is the untrusted-input control (§6.4), not a
modelling preference. If an agent's report could set `state`, a device coming
online would silently overwrite what an operator documented, which is what "the
IPAM changed my reservation by itself" means. `status` is DERIVED at read time
(`allocation.derive_status`); `state` is what a human asserted.

**THE UPSERT IS THE SCHEMA PROPERTY THAT KEEPS THIS MODULE OUT OF THE
MONITORING BUSINESS** (§0.10 point 4). Rows are upserted on
`(org_id, vrf_id, address, source, mac_key)`, bumping `last_seen_at` and
`sighting_count`; there is no row per probe, no availability percentage and no
RTT anywhere. `mac_key` is the GENERATED column, never `mac`: `mac` is nullable,
every ICMP-only sweep observation has `mac IS NULL`, NULLs do not collide in a
Postgres unique index, and that key would append a fresh row per responding host
per sweep forever (§0.15). The conflict target is written in exactly one place
-- here -- and `tests/ipam_fixtures.py::upsert_observation_row` mirrors it so
the migration test and this service exercise the same statement shape.

**`subnet_id` IS DERIVED BY CONTAINMENT AND RE-DERIVED ON EVERY SIGHTING.** The
device does not report a prefix length (§4.1 point 1) and could not be believed
about one if it did. An address contained by no registered subnet is stored with
`subnet_id = NULL` and surfaced as an ORPHAN (§4.2) -- not an error, and not
drift: nothing is wrong with the plan, the plan is absent. Re-deriving in the
conflict branch is what makes an orphan stop being one once its subnet is
finally registered.

**`Agent.last_ip` IS NOT AN OBSERVATION SOURCE.** It is the TCP peer address of
the WebSocket connection as seen by the gateway (`app/gateway/main.py:243`),
which behind NAT is an egress address belonging to a firewall. Ingesting it
would document the wrong machine at the wrong address.

**EVERY QUERY CARRIES AN EXPLICIT `org_id` PREDICATE** in addition to RLS
(invariant #5), pinned on the ADMIN engine by `tests/test_ipam_observations.py`
-- through `org_session` RLS rejects first and those tests pass with the
predicate deleted.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from app.core.i18n import tr
from app.models import Device, HardwareComponent, Policy
from app.modules.ipam.errors import IpamValidationError
from app.modules.ipam.models import IpamObservation
from app.modules.ipam.net import parse_address, parse_mac
from app.modules.ipam.nic import ORIGINS, UNKNOWN_ORIGIN, observations_from_components
from app.modules.ipam.registry import ensure_default_vrf
from app.modules.ipam.subnets import containing_subnet

__all__ = [
    "DEFAULT_OBSERVATION_TTL_DAYS",
    "OBSERVATION_CONFIDENCE",
    "OBSERVATION_METHODS",
    "OBSERVATION_SOURCES",
    "OrphanGroup",
    "current_observations",
    "inventory_post_ingest_hook",
    "observation_ttl_days",
    "orphan_groups",
    "record_inventory_observations",
    "upsert_observation",
]

#: `W_obs` (§4.2). Fourteen days is ~14 inventory sweeps at the 24h default
#: cadence and >= 2 sweeps at the weekly `net.sweep` default, so a single missed
#: cycle never ages anything out.
DEFAULT_OBSERVATION_TTL_DAYS = 14

OBSERVATION_SOURCES = ("inventory", "sweep", "import", "dhcp")
OBSERVATION_METHODS = ("nic", "arp", "icmp", "manual")
OBSERVATION_CONFIDENCE = ("high", "medium", "low")

#: §14.1's `origin` vocabulary, re-exported from the ONE place that defines it.
#: Not re-typed: `nic.py` normalises to this set and migration 0051 CHECKs it,
#: and a third hand-typed copy here is how a fourth value gets added to two of
#: the three.
OBSERVATION_ALLOCATION_ORIGINS = tuple(sorted(ORIGINS))

#: The five columns of `uq_ipam_obs_key`. The fifth is the GENERATED `mac_key`,
#: never the nullable `mac` -- see this module's docstring.
_CONFLICT_KEY = ["org_id", "vrf_id", "address", "source", "mac_key"]

#: §4.2's orphan grouping: an inferred /24 for IPv4 and /64 for IPv6. Neither is
#: a claim about the real prefix length -- nobody knows it, that is what makes
#: these orphans -- it is the grouping an operator recognises as "the 10.7.3.x
#: range" and can turn into one subnet registration.
_ORPHAN_MASKLEN_V4 = 24
_ORPHAN_MASKLEN_V6 = 64


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class OrphanGroup:
    """Observed addresses inside no registered subnet, grouped (§4.2)."""

    prefix: str
    count: int
    last_seen_at: datetime


async def observation_ttl_days(session: AsyncSession, org_id: UUID) -> int:
    """`W_obs` for this org -- `Policy(module="ipam").config`, default 14.

    CLAMPED, NOT TRUSTED. The policy is operator-editable JSONB, and a `0` or a
    string there would make every observation instantly non-current: the
    allocator's "not currently observed" clause would stop excluding live
    addresses and every §4.3 absence gate would stop gating. A nonsense value
    falls back to the default rather than quietly disabling the reconciliation.
    """
    config: Any = (
        await session.execute(
            select(Policy.config)
            .where(Policy.org_id == org_id, Policy.module == "ipam")
            .limit(1)
        )
    ).scalar_one_or_none()
    if not isinstance(config, dict):
        return DEFAULT_OBSERVATION_TTL_DAYS
    raw = config.get("observation_ttl_days", DEFAULT_OBSERVATION_TTL_DAYS)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_OBSERVATION_TTL_DAYS
    return raw if raw >= 1 else DEFAULT_OBSERVATION_TTL_DAYS


async def _currency_cutoff(session: AsyncSession, org_id: UUID) -> datetime:
    return _utcnow() - timedelta(days=await observation_ttl_days(session, org_id))


async def upsert_observation(
    session: AsyncSession,
    *,
    org_id: UUID,
    vrf_id: UUID,
    address: str,
    mac: str | None,
    hostname: str,
    source: str,
    method: str,
    device_id: UUID | None,
    confidence: str,
    allocation_origin: str = UNKNOWN_ORIGIN,
    sweep_run_id: UUID | None = None,
    now: datetime | None = None,
) -> None:
    """Record one sighting. The ONLY write path into `ipam_observation`.

    `mac_key` is never in `values()`: setting a generated column is a Postgres
    error, which is exactly the property that makes §0.15's guarantee
    unforgettable rather than remembered. It appears only in the conflict
    inference.

    The conflict branch re-derives `subnet_id` and refreshes `hostname`,
    `device_id`, `allocation_origin` and `sweep_run_id`, so a row's evidence is
    current rather than frozen at whatever the first sighting happened to carry.
    It does NOT touch `first_seen_at` -- that is how long this address has been
    known, and §4.3's detail payloads quote it.

    `allocation_origin` DEFAULTS TO `unknown` AND THAT IS THE SWEEP'S ANSWER, not
    an omission: ARP and ICMP establish that an address answers, never how it was
    configured (§2.7's `confidence` records the same limit for the same
    evidence). It is REFRESHED on conflict for the reason `hostname` is -- a
    machine moved from a DHCP lease onto a static address must stop reading
    `dhcp` on its next inventory pass, and freezing the first answer would make
    §3.2's field wrong in a way nothing ever corrects.
    """
    parsed = parse_address(address)
    if parsed is None:
        raise IpamValidationError(tr("{address} is not a valid address", address=repr(address)))
    mac_text: str | None = None
    if mac is not None:
        mac_text = parse_mac(str(mac))
        if mac_text is None:
            raise IpamValidationError(tr("{mac} is not a valid MAC address", mac=repr(mac)))
    if source not in OBSERVATION_SOURCES:
        raise IpamValidationError(
            tr(
                "source must be one of {observation_sources}",
                observation_sources=list(OBSERVATION_SOURCES),
            )
        )
    if method not in OBSERVATION_METHODS:
        raise IpamValidationError(
            tr(
                "method must be one of {observation_methods}",
                observation_methods=list(OBSERVATION_METHODS),
            )
        )
    if confidence not in OBSERVATION_CONFIDENCE:
        raise IpamValidationError(
            tr(
                "confidence must be one of {observation_confidence}",
                observation_confidence=list(OBSERVATION_CONFIDENCE),
            )
        )
    # VALIDATED HERE TOO, even though `nic.py` already normalises. This function
    # is the ONLY write path into the table and it has three callers, of which
    # the NIC parser is one; the sweep result handler and the importer reach it
    # without passing through any normalisation at all. A rejected value is an
    # `IpamValidationError` the caller can see, which is strictly better than the
    # CHECK constraint's IntegrityError -- and better than the silent coercion
    # that a `.get(..., 'unknown')` here would be, because a sweep handler
    # inventing an origin is a bug rather than an untrusted agent's noise.
    if allocation_origin not in ORIGINS:
        raise IpamValidationError(
            tr(
                "allocation_origin must be one of {observation_allocation_origins}",
                observation_allocation_origins=list(OBSERVATION_ALLOCATION_ORIGINS),
            )
        )

    subnet = await containing_subnet(session, org_id, vrf_id, str(parsed))
    stamp = now or _utcnow()
    statement = pg_insert(IpamObservation).values(
        org_id=org_id,
        vrf_id=vrf_id,
        subnet_id=subnet.id if subnet is not None else None,
        address=str(parsed),
        mac=mac_text,
        hostname=hostname[:255],
        source=source,
        method=method,
        device_id=device_id,
        confidence=confidence,
        allocation_origin=allocation_origin,
        sweep_run_id=sweep_run_id,
        first_seen_at=stamp,
        last_seen_at=stamp,
        sighting_count=1,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=_CONFLICT_KEY,
            set_={
                "last_seen_at": statement.excluded.last_seen_at,
                "sighting_count": IpamObservation.sighting_count + 1,
                "hostname": statement.excluded.hostname,
                "subnet_id": statement.excluded.subnet_id,
                "device_id": statement.excluded.device_id,
                "confidence": statement.excluded.confidence,
                "allocation_origin": statement.excluded.allocation_origin,
                "sweep_run_id": statement.excluded.sweep_run_id,
            },
        )
    )


async def record_inventory_observations(session: AsyncSession, device: Device) -> int:
    """Derive this device's observations from its `hardware_components` NICs.

    Reads `hardware_components` with `type='nic'` -- the normalized,
    wholesale-replaced current state inventory's ingest step 4 leaves behind --
    and NOT `Device.nics`, which is a flattened jsonb convenience copy (§4.1).

    Returns the number of addresses recorded, and writes nothing else: no
    `ipam_address` row, no `state`, no claimant (§0.6). THAT INCLUDES
    `ipam_address.allocation_method`, which §3.2 makes operator-set: what this
    path records is `ipam_observation.allocation_origin`, the fleet's REPORT, and
    `allocation.derive_allocation_method` combines the two at read time. Writing
    the reported method onto the address row would be the same defect §0.6 exists
    to prevent, one column across.

    The default VRF is created only when there is something to file in it. A
    device with no documentable address is not a first use of §0.7's lazily
    created `global` row, and creating it anyway would mean every
    `inventory.collect` in the estate wrote an IPAM row.
    """
    components = (
        (
            await session.execute(
                select(HardwareComponent).where(
                    HardwareComponent.org_id == device.org_id,
                    HardwareComponent.device_id == device.id,
                    HardwareComponent.type == "nic",
                )
            )
        )
        .scalars()
        .all()
    )
    parsed = observations_from_components(components)
    if not parsed:
        return 0

    vrf_id = await ensure_default_vrf(session, device.org_id)
    now = _utcnow()
    for observation in parsed:
        await upsert_observation(
            session,
            org_id=device.org_id,
            vrf_id=vrf_id,
            address=observation.address,
            mac=observation.mac,
            hostname=device.hostname or "",
            source="inventory",
            method="nic",
            device_id=device.id,
            # §2.7: the OS reported about itself, over mTLS, as the result of a
            # signed job. `mac_mismatch` (§4.3) reads this column, which is why
            # it is a column and not a comment.
            confidence="high",
            # §3.2, closed. The agent has supplied this since §14.1 landed and
            # this module dropped it on the floor; `nic.py` has already
            # normalised it to `ORIGINS`, so nothing unrecognised reaches the
            # CHECK constraint.
            allocation_origin=observation.allocation_origin,
            now=now,
        )
    return len(parsed)


async def inventory_post_ingest_hook(
    session: AsyncSession, device: Device, doc: Any
) -> None:
    """The function IPAM registers into inventory's extension point (§0.17).

    Deliberately ignores `doc` and re-reads `hardware_components`: it runs AFTER
    `ingest_inventory`, so the table already holds the normalized current state,
    and re-deriving from the raw document would be a second parser of the same
    facts that could disagree with the one inventory stored.

    It does not need its own `try/except`. `run_post_ingest_hooks` owns the
    SAVEPOINT and the `except` -- that guarantee is inventory's to make, not
    each hook author's to remember -- and `nic.observations_from_components` is
    total, so the ordinary "this NIC block is garbage" case never reaches it.
    """
    await record_inventory_observations(session, device)


async def current_observations(
    session: AsyncSession, org_id: UUID, addresses: list[str]
) -> dict[str, list[IpamObservation]]:
    """`O_cur(A)` for each address in `addresses` (§4.2), keyed by canonical text.

    An address with no current observation is ABSENT from the mapping rather
    than mapped to `[]`, so a caller that forgets the empty case gets a
    `KeyError` in a test rather than a silently wrong gate in production.

    Rows outside `W_obs` are excluded, NOT deleted (§4.2): they stop being
    current, and the 365-day retention pass is what bounds the table.
    """
    wanted: list[str] = []
    for value in addresses:
        parsed = parse_address(str(value))
        if parsed is not None:
            wanted.append(str(parsed))
    if not wanted:
        return {}

    cutoff = await _currency_cutoff(session, org_id)
    rows = (
        (
            await session.execute(
                select(IpamObservation).where(
                    IpamObservation.org_id == org_id,
                    IpamObservation.last_seen_at >= cutoff,
                    IpamObservation.address.in_(wanted),
                )
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[str, list[IpamObservation]] = {}
    for row in rows:
        # asyncpg hands an `inet` column back as an `IPv4Address`/`IPv6Address`,
        # so the key is normalised rather than assumed to be the text that went
        # in -- otherwise `10.1.2.42` and `IPv4Address('10.1.2.42')` are two
        # different keys for one address.
        grouped.setdefault(str(row.address), []).append(row)
    return grouped


async def orphan_groups(session: AsyncSession, org_id: UUID) -> list[OrphanGroup]:
    """Currently observed addresses inside no registered subnet, grouped (§4.2).

    Grouped in SQL rather than in Python: the row set is "every orphan this org
    has", which is unbounded by anything the caller controls, and the answer is
    a handful of prefixes. Counted with `count(DISTINCT address)` so an address
    seen by both ARP and ICMP is one address, not two.

    Current only. An orphan nobody has seen in three months is not a subnet
    worth registering today, and the same `W_obs` window governs it as governs
    every other "is this true now" question in the module.
    """
    cutoff = await _currency_cutoff(session, org_id)
    rows = (
        await session.execute(
            text(
                "SELECT network(set_masklen(address, CASE WHEN family(address) = 4"
                "   THEN CAST(:v4 AS integer) ELSE CAST(:v6 AS integer) END)) AS prefix,"
                "  count(DISTINCT address) AS address_count,"
                "  max(last_seen_at) AS last_seen_at"
                " FROM ipam_observation"
                " WHERE org_id = :org AND subnet_id IS NULL AND last_seen_at >= :cutoff"
                " GROUP BY 1 ORDER BY 1"
            ),
            {
                "org": str(org_id),
                "cutoff": cutoff,
                "v4": _ORPHAN_MASKLEN_V4,
                "v6": _ORPHAN_MASKLEN_V6,
            },
        )
    ).all()
    return [
        OrphanGroup(
            prefix=str(row.prefix),
            count=int(row.address_count),
            last_seen_at=row.last_seen_at,
        )
        for row in rows
    ]
