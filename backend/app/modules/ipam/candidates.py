"""Observed addresses the inventory has never tied to a device (spec §3).

READ THIS BEFORE CHANGING THE QUERY. `ipam_observation.device_id` is set only
on `source='inventory'` rows -- a device reporting its own NICs -- and never on
a sweep, deliberately (ipam spec §6.4: letting a sweep assert identity would be
a compromised agent's cheapest way to re-point an assignment). So a managed
machine that answers a ping has TWO observation rows, one carrying a device id
and one not, and `WHERE device_id IS NULL` therefore returns **the entire
managed fleet**. The exclusions below are about the ADDRESS and the MAC across
all of an org's observations, never about a single row.

DISCOVERY NOW CREATES ASSETS -- THE OPERATOR RULED ON 2026-08-26. Until that
date this module refused to, and the refusal was argued here at length: an echo
reply is not an asset, a sweep on a corporate LAN answers for printers, phones,
a colleague's laptop on the wrong VLAN and the coffee machine, and rows built
from that are indistinguishable from rows somebody entered. The operator was
shown that argument and its consequences, chose to record everything a sweep
finds, and this file implements that choice: `auto_create_from_sweep` writes a
`devices` row for every host a completed `net.sweep` reports.

WHAT ANSWERS THE OLD ARGUMENT IS `devices.provenance`, NOT A REFUSAL TO WRITE.
The half of the objection that was actually load-bearing is "indistinguishable"
-- an inventory that cannot say which rows a person stood behind is one nobody
can audit later. Every row this module writes now says so in a column:
`promoted` when an operator picked it off the Discovered screen, `sweep` when
the platform recorded it on its own evidence. The coffee machine and the domain
controller are both in the inventory, and the inventory knows which one a human
vouched for.

`promote` therefore remains, and is not a fallback behind a flag: it is how an
operator RECORDS THAT THEY VOUCH for something, and it is still the only path
that takes their asset type, lifecycle, owner and location.

GROUPED IN SQL, following `observations.orphan_groups`, and for the same
reason: the row set is "every observation this org holds", which is bounded by
nothing the caller controls, while the answer is small.

EVERYTHING HERE IS PER CLIENT (MSP clients, invariant 5a). Two clients can run
the same 10.0.0.0/24 on two unrelated wires, so an address is a machine only
within a client: a candidate is a `(client, address)` pair, "already known"
means known to THAT client's inventory -- another client's machine at the same
address or with the same MAC hides nothing -- and what a promotion or a sweep
writes is filed under the client of the subnet it was seen on, at that
subnet's site. A subnet without a site gives none to file under: its machines
stay candidates (decision C7, plan 2026-10-26).
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.i18n import tr
from app.modules.ipam.models import IpamSubnet
from app.modules.ipam.net import parse_mac
from app.modules.ipam.observations import _currency_cutoff
from app.modules.ipam.oui import oui_prefix, vendor

#: Rows returned by default. A `/16` sweep can observe tens of thousands of
#: addresses; an unbounded list is a memory event in the API process and an
#: unusable screen. The cap is not silent -- `CandidatePage.total` carries the
#: real count so the console can say "3 of 7 000" rather than letting an
#: operator read a truncated list as the whole answer.
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


@dataclass(frozen=True)
class AssetCandidate:
    """One address the fleet has seen and the inventory cannot account for."""

    address: str
    mac: str | None
    #: The manufacturer, or None. NEVER a guess -- see `ipam/oui.py`.
    vendor: str | None
    #: `AA:BB:CC`, shown when `vendor` is None so the operator has something to
    #: look up rather than an empty cell.
    oui_prefix: str
    #: Reverse DNS, when the sweep got one. The weakest signal there is, which
    #: is why it is displayed and never matched on.
    hostname: str
    first_seen_at: datetime
    last_seen_at: datetime
    sightings: int
    #: The strongest evidence anything has for this address: `medium` means ARP
    #: (authoritative for address<->MAC on-segment), `low` means ICMP
    #: (something answered, and that is all it means).
    confidence: str
    subnet_id: UUID | None
    #: Whose network saw it: its observations' client (their VRF's). The same
    #: address in two clients is two candidates.
    client_id: UUID


@dataclass(frozen=True)
class CandidatePage:
    items: list[AssetCandidate]
    #: How many candidates exist, not how many were returned.
    total: int
    truncated: bool


#: THE THREE EXCLUSIONS ARE THE WHOLE VALUE OF THIS QUERY.
#:
#:   1. no observation of this ADDRESS in the candidate's client carries a
#:      device id -- without it, every managed machine that answers a ping is
#:      listed;
#:   2. no observation of this MAC in that client does either -- a laptop that
#:      took a new DHCP lease is still known, and would otherwise reappear as
#:      something new;
#:   3. no `devices` row of that client already claims the address as its
#:      `primary_ip` -- an asset an operator typed in by hand must not be
#:      offered back to them as a discovery.
#:
#: ALL THREE WITHIN THE CLIENT, and grouped by `(client, address)`: X's
#: machine at 10.0.0.9 says nothing about what answered at Y's 10.0.0.9.
#: Across clients the exclusions would also depend on who is looking -- RLS
#: shows a technician of X only X's machines -- so one sighting would be a
#: candidate to some people and not to others.
#:
#: Deliberately NOT excluded on hostname. A reverse-DNS name is the weakest
#: signal there is and the spec refuses it even for LINKING (§4 rule 3); using
#: it to HIDE a candidate is the same mistake pointed the other way, and would
#: silently drop a real unknown machine that happens to share a name with
#: something in the inventory.
_CANDIDATES_SQL = """
SELECT o.client_id                   AS client_id,
       o.address                     AS address,
       max(o.mac::text)              AS mac,
       max(o.hostname)               AS hostname,
       min(o.first_seen_at)          AS first_seen_at,
       max(o.last_seen_at)           AS last_seen_at,
       sum(o.sighting_count)         AS sightings,
       max(o.confidence)             AS confidence,
       max(o.subnet_id::text)        AS subnet_id
  FROM ipam_observation o
 WHERE o.org_id = :org{client_filter}
   AND o.last_seen_at >= :cutoff
   AND NOT EXISTS (SELECT 1 FROM ipam_observation k
                    WHERE k.org_id = o.org_id
                      AND k.client_id = o.client_id
                      AND k.address = o.address
                      AND k.device_id IS NOT NULL)
   AND (o.mac IS NULL OR NOT EXISTS (
            SELECT 1 FROM ipam_observation k
             WHERE k.org_id = o.org_id
               AND k.client_id = o.client_id
               AND k.mac = o.mac
               AND k.device_id IS NOT NULL))
   AND NOT EXISTS (SELECT 1 FROM devices d
                    WHERE d.org_id = o.org_id
                      AND d.client_id = o.client_id
                      AND d.primary_ip = o.address)
 GROUP BY o.client_id, o.address
"""

#: One client's candidates -- what a promotion re-derives -- or every client's
#: this session sees.
_ONE_CLIENT = "\n   AND o.client_id = :client"


def _candidates_sql(*, one_client: bool) -> str:
    return _CANDIDATES_SQL.format(client_filter=_ONE_CLIENT if one_client else "")


def _count_sql(*, one_client: bool) -> str:
    return f"SELECT count(*) FROM ({_candidates_sql(one_client=one_client)}) AS c"


def _page_sql(*, one_client: bool) -> str:
    return (
        f"{_candidates_sql(one_client=one_client)}"
        " ORDER BY max(o.last_seen_at) DESC, o.address, o.client_id LIMIT :limit"
    )


def _row_to_candidate(row: object) -> AssetCandidate:
    mac = getattr(row, "mac", None)
    subnet_id = getattr(row, "subnet_id", None)
    return AssetCandidate(
        address=str(row.address),  # type: ignore[attr-defined]
        mac=mac,
        # `or None` rather than `None if not mac`: an empty MAC string is as
        # meaningless as a missing one, and `vendor("")` already answers None.
        vendor=vendor(mac) if mac else None,
        oui_prefix=oui_prefix(mac) if mac else "",
        hostname=row.hostname or "",  # type: ignore[attr-defined]
        first_seen_at=row.first_seen_at,  # type: ignore[attr-defined]
        last_seen_at=row.last_seen_at,  # type: ignore[attr-defined]
        sightings=int(row.sightings),  # type: ignore[attr-defined]
        confidence=row.confidence,  # type: ignore[attr-defined]
        subnet_id=UUID(subnet_id) if subnet_id else None,
        client_id=row.client_id,  # type: ignore[attr-defined]
    )


async def list_candidates(
    session: AsyncSession,
    org_id: UUID,
    *,
    limit: int = DEFAULT_LIMIT,
    client_id: UUID | None = None,
) -> CandidatePage:
    """Candidates, newest sighting first, bounded: `client_id`'s alone, or
    every client's this session can see.

    The count is a second query rather than a window function so the page query
    stays the one thing anybody has to read to understand what a candidate IS.
    Both run against the same `cutoff`, taken once.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    cutoff = await _currency_cutoff(session, org_id)
    params: dict[str, object] = {"org": str(org_id), "cutoff": cutoff}
    one_client = client_id is not None
    if one_client:
        params["client"] = str(client_id)

    total = (await session.execute(text(_count_sql(one_client=one_client)), params)).scalar_one()
    rows = (
        await session.execute(text(_page_sql(one_client=one_client)), {**params, "limit": limit})
    ).all()
    return CandidatePage(
        items=[_row_to_candidate(row) for row in rows],
        total=int(total),
        truncated=int(total) > len(rows),
    )


@dataclass(frozen=True)
class PromotionResult:
    """Per address, because a selection is promoted as a batch.

    One stale entry in an operator's selection must not silently discard the
    rest, and "already an asset" is not an error -- two people looking at the
    same list is the normal case.
    """

    address: str
    created: bool
    device_id: UUID | None
    reason: str


async def promote(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID | None,
    client_id: UUID,
    addresses: list[str],
    overrides: dict[str, object] | None = None,
) -> list[PromotionResult]:
    """Turn `client_id`'s selected candidates into `devices` rows with
    `source='discovery'`.

    THE CANDIDATE IS RE-DERIVED HERE, and the request body is never trusted for
    the address, MAC or hostname. A client can post any string; taking identity
    from the body would make this a route that writes arbitrary inventory rows,
    when its entire premise is that the fleet found them. An address that is
    not currently a candidate is refused -- by name, in the result -- rather
    than created.

    **THE CLIENT IS NAMED, AND THE CANDIDATES ARE ITS OWN.** It is loaded like
    every create's (`client_for_ipam_row`: 404 unseen, 409 archived, 422 "Sin
    asignar"), and the candidates are re-derived within it, so Y's sighting is
    "not a candidate" of X whoever asks -- RLS hides it from a technician of X
    anyway, and an every-client one gets the same answer. Each machine is filed
    under THAT client -- the validated, locked one, which its subnet's must
    equal (a sighting naming another client's subnet is not this client's
    candidate) -- AT ITS SUBNET'S SITE: a device needs a site, a client has no
    marked default one, so a candidate whose subnet has no site is refused with
    the sentence that says what to do (decision C7), one in no registered
    subnet with its own, and one whose site is archived as a hand-typed asset
    there would be.

    IDEMPOTENT ON `primary_ip`, WITHIN THE CLIENT. Two operators looking at the
    same list, or one double-click, must not produce two rows; and it must not
    500 either, because from the screen those two outcomes are
    indistinguishable. Another client's machine at the address is not this one.

    NO FAN-OUT: the candidates' subnets are read in one statement and their
    sites in another (FOR SHARE, held to commit, after the client -- see
    `app.modules.ipam.clients` for the order), however many there are.
    """
    from app.core.audit import audit_service
    from app.models import Device, Site
    from app.modules.ipam.clients import client_for_ipam_row

    client_id = await client_for_ipam_row(session, org_id, client_id)
    wanted = list(dict.fromkeys(addresses))
    if not wanted:
        return []

    # The candidate set, taken once. `MAX_LIMIT` also bounds a caller who posts
    # thousands of addresses in a single request.
    page = await list_candidates(session, org_id, limit=MAX_LIMIT, client_id=client_id)
    by_address = {candidate.address: candidate for candidate in page.items}

    existing = {
        str(address): device_id
        for device_id, address in (
            await session.execute(
                text(
                    "SELECT id, host(primary_ip) FROM devices"
                    " WHERE org_id = :org AND client_id = :client"
                    "   AND primary_ip IS NOT NULL"
                ),
                {"org": str(org_id), "client": str(client_id)},
            )
        ).all()
    }

    subnet_ids = {
        candidate.subnet_id
        for candidate in (by_address.get(address) for address in wanted if address not in existing)
        if candidate is not None and candidate.subnet_id is not None
    }
    subnets = (
        {
            row.id: row
            for row in (
                await session.execute(
                    select(IpamSubnet.id, IpamSubnet.client_id, IpamSubnet.site_id).where(
                        IpamSubnet.org_id == org_id, IpamSubnet.id.in_(subnet_ids)
                    )
                )
            ).all()
        }
        if subnet_ids
        else {}
    )
    site_ids = {row.site_id for row in subnets.values() if row.site_id is not None}
    sites = (
        {
            site.id: site
            for site in (
                await session.execute(
                    select(Site)
                    .where(Site.org_id == org_id, Site.id.in_(site_ids))
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        }
        if site_ids
        else {}
    )

    results: list[PromotionResult] = []
    for address in wanted:
        if address in existing:
            results.append(
                PromotionResult(
                    address=address,
                    created=False,
                    device_id=existing[address],
                    reason=tr("already an asset"),
                )
            )
            continue
        candidate = by_address.get(address)
        if candidate is None:
            results.append(
                PromotionResult(
                    address=address,
                    created=False,
                    device_id=None,
                    reason=tr("not a candidate: no current observation for this address"),
                )
            )
            continue
        subnet = subnets.get(candidate.subnet_id) if candidate.subnet_id else None
        if subnet is None:
            # AN ORPHAN: seen outside every registered subnet, so there is no
            # subnet to give a site to -- C7's sentence would point at nothing.
            results.append(
                PromotionResult(
                    address=address,
                    created=False,
                    device_id=None,
                    reason=tr(
                        "this address is in no registered subnet: register its subnet, at a "
                        "site, before adding the machine"
                    ),
                )
            )
            continue
        if subnet.client_id != client_id:
            # NOTHING TIES AN OBSERVATION'S `subnet_id` TO ITS CLIENT, so a
            # sighting of this client can name another client's subnet. Filed
            # under that subnet it would be the other client's machine, at the
            # other client's site, from a promotion that named this one: it is
            # not this client's candidate.
            results.append(
                PromotionResult(
                    address=address,
                    created=False,
                    device_id=None,
                    reason=tr("not a candidate: no current observation for this address"),
                )
            )
            continue
        site = sites.get(subnet.site_id) if subnet.site_id else None
        if site is None:
            results.append(
                PromotionResult(
                    address=address,
                    created=False,
                    device_id=None,
                    reason=tr("assign the subnet to a site before adding its machines"),
                )
            )
            continue
        if site.archived_at is not None:
            results.append(
                PromotionResult(
                    address=address,
                    created=False,
                    device_id=None,
                    reason=tr("that site is archived"),
                )
            )
            continue

        device = Device(
            # STAMPED HERE RATHER THAN LEFT TO THE COLUMN DEFAULT, so the id is
            # readable before the INSERT and the row does not need a flush of
            # its own. `uuid7_pk()`'s default is the same callable; all that
            # moves is when it runs.
            id=uuid7(),
            org_id=org_id,
            # THE CLIENT THE PROMOTION NAMED -- validated and locked, and the
            # subnet's, checked above -- AND THE SUBNET'S SITE (C7).
            client_id=client_id,
            site_id=site.id,
            source="discovery",
            # A PERSON PICKED THIS ONE. That is the whole difference between
            # this path and `auto_create_from_sweep`, and after 2026-08-26 it is
            # the only difference -- both produce a `discovery` row.
            provenance="promoted",
            primary_ip=candidate.address,
            # Reverse DNS if the sweep got one. Displayed, never matched on --
            # it is the weakest signal there is (spec §4).
            hostname=candidate.hostname or None,
            # THE MAC IS WHAT MAKES THIS ROW MATCHABLE LATER, and without it the
            # whole discover -> promote -> install-agent path fails at its last
            # step: the agent enrols, finds nothing to join, and creates a
            # SECOND row for the machine an operator had just curated. Stored in
            # the `nics` shape `inventory/ingest.py` writes, so the matcher has
            # one reader rather than two.
            #
            # Absent when the sighting was ICMP-only -- a ping carries no MAC,
            # and an invented entry is one the matcher would act on.
            nics={"items": [{"identifier": candidate.mac, "mac": candidate.mac}]}
            if candidate.mac
            else None,
            **(overrides or {}),
        )
        session.add(device)
        await session.flush()
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="asset.promoted",
            target_type="device",
            target_id=str(device.id),
            client_id=client_id,
            # The EVIDENCE, not just the outcome: `confidence` says whether ARP
            # or only ICMP saw this, which is what an auditor needs to judge how
            # much the row is worth.
            metadata={
                "address": candidate.address,
                "mac": candidate.mac,
                "confidence": candidate.confidence,
                "sightings": candidate.sightings,
                "subnet_id": str(subnet.id),
            },
        )
        results.append(
            PromotionResult(
                address=address, created=True, device_id=device.id, reason="created"
            )
        )
    return results


# --------------------------------------------------------------------------- #
# Automatic recording (the operator's ruling of 2026-08-26)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SweepSighting:
    """One host a completed sweep reported, already parsed and in scope.

    Taken as a value rather than as the result document, so this function is
    reachable from a test without a signed job -- and so nothing here can read a
    field of the agent's payload that `handler.py` has not already validated.
    """

    address: str
    #: Normalised, or None when only ICMP answered. A ping carries no MAC.
    mac: str | None
    #: Reverse DNS, or "". Displayed, never matched on.
    hostname: str
    #: `medium` = ARP saw it, `low` = ICMP only.
    confidence: str


@dataclass(frozen=True)
class AutoAssetResult:
    """What became of one sighting."""

    address: str
    device_id: UUID | None
    created: bool
    #: `created | readdressed | already recorded | candidate` -- the last for a
    #: new machine on a subnet with no active site to file it at (C7): it
    #: stays on the Discovered list for a person to promote once there is one.
    action: str


#: EVERY LOOKUP BELOW IS THE SWEPT SUBNET'S CLIENT'S (`:client`), as the
#: candidate query's exclusions are: X's machine at 10.0.0.9 is no reason to
#: leave Y's 10.0.0.9 unrecorded, and must never be the row Y's sighting is
#: "already recorded" as.
#:
#: Addresses a MACHINE has claimed as its own.
#:
#: `ipam_observation.device_id` is set only on `source='inventory'` rows -- a
#: device reporting its own NICs -- and never on a sweep (§6.4). It is
#: therefore the only evidence tying a bare address with no MAC to a machine
#: the platform already manages, and without this check every hourly sweep
#: would record the managed fleet a second time.
_CLAIMED_ADDRESSES_SQL = """
SELECT DISTINCT host(o.address) AS address
  FROM ipam_observation o
 WHERE o.org_id = :org AND o.client_id = :client AND o.device_id IS NOT NULL
"""

#: The same for MACs: a laptop that took a new DHCP lease is still known.
_CLAIMED_MACS_SQL = """
SELECT DISTINCT o.mac::text AS mac
  FROM ipam_observation o
 WHERE o.org_id = :org AND o.client_id = :client
   AND o.device_id IS NOT NULL AND o.mac IS NOT NULL
"""

#: Addresses the INVENTORY already holds, whoever put them there.
_ASSET_ADDRESSES_SQL = """
SELECT d.id AS id, host(d.primary_ip) AS address
  FROM devices d
 WHERE d.org_id = :org AND d.client_id = :client AND d.primary_ip IS NOT NULL
"""

#: Every MAC on every asset row, with what the platform may do about it.
#:
#: UNNESTED IN SQL AND NORMALISED IN PYTHON, because `nics` is a JSONB column
#: three code paths write and the MAC in it is in whatever form its writer used
#: -- `A4-BB-6D-11-22-33` from Windows, `a4bb.6d11.2233` from a switch. An
#: equality test in SQL would silently miss those and create a duplicate row for
#: a machine the platform already has.
#:
#: `managed` and `provenance` are carried because they decide whether a row may
#: be CORRECTED: the platform re-addresses a row it wrote itself, and never one
#: an operator, an agent or an enrolment owns.
_ASSET_MACS_SQL = """
SELECT d.id                                        AS id,
       d.provenance                                AS provenance,
       coalesce(n->>'mac', n->>'identifier')       AS mac,
       EXISTS (SELECT 1 FROM agents a WHERE a.device_id = d.id) AS managed
  FROM devices d
  CROSS JOIN LATERAL jsonb_array_elements(
      CASE WHEN jsonb_typeof(d.nics->'items') = 'array'
           THEN d.nics->'items'
           ELSE '[]'::jsonb END) AS n
 WHERE d.org_id = :org AND d.client_id = :client
"""


@dataclass
class _Known:
    """Everything the inventory already accounts for, read once per sweep."""

    addresses: dict[str, UUID | None]
    macs: dict[str, tuple[UUID | None, str, bool]]


async def _known_inventory(session: AsyncSession, org_id: UUID, client_id: UUID) -> _Known:
    """What `client_id`'s inventory already accounts for -- its alone."""
    params = {"org": str(org_id), "client": str(client_id)}
    addresses: dict[str, UUID | None] = {}
    for row in (await session.execute(text(_CLAIMED_ADDRESSES_SQL), params)).all():
        addresses[str(row.address)] = None
    for row in (await session.execute(text(_ASSET_ADDRESSES_SQL), params)).all():
        addresses[str(row.address)] = row.id

    macs: dict[str, tuple[UUID | None, str, bool]] = {}
    for row in (await session.execute(text(_CLAIMED_MACS_SQL), params)).all():
        parsed = parse_mac(str(row.mac))
        if parsed is not None:
            # A machine's own report. Nothing here may edit that row.
            macs[parsed] = (None, "agent", True)
    for row in (await session.execute(text(_ASSET_MACS_SQL), params)).all():
        parsed = parse_mac(str(row.mac or ""))
        if parsed is not None and parsed not in macs:
            macs[parsed] = (row.id, str(row.provenance), bool(row.managed))
    return _Known(addresses=addresses, macs=macs)


async def _site_taking_machines(
    session: AsyncSession, org_id: UUID, subnet: IpamSubnet
) -> UUID | None:
    """The site a swept subnet's new machines are filed at: the subnet's,
    while it and its client take new rows -- read FOR SHARE, site then client,
    and held to commit (`client_sites.site_for_new_rows`). `None` for a subnet
    without a site, or with an archived one: nothing to file under (C7)."""
    from app.services.client_sites import (
        NotAcceptingNewRows,
        SiteNotFound,
        site_for_new_rows,
    )

    if subnet.site_id is None:
        return None
    try:
        site = await site_for_new_rows(session, org_id=org_id, site_id=subnet.site_id)
    except (SiteNotFound, NotAcceptingNewRows):
        return None
    return site.id


async def auto_create_from_sweep(
    session: AsyncSession,
    org_id: UUID,
    *,
    subnet: IpamSubnet,
    sightings: list[SweepSighting],
    sweep_run_id: UUID | None = None,
) -> list[AutoAssetResult]:
    """Record every host a sweep of `subnet` found, exactly once.

    **IN THE SUBNET'S CLIENT, AT ITS SITE** (MSP clients, invariant 5a; C7).
    What is "already known" is that client's inventory alone (`_known_inventory`),
    and a new machine is filed under that client at the subnet's site. A subnet
    with no site -- or an archived one -- has nowhere to file a machine: the
    sighting is reported `candidate` and stays on the Discovered list, where a
    person promotes it once the subnet has a site. Re-addressing a row the
    sweep wrote before needs no site and still happens.

    **THE IDENTITY KEY IS THE MAC WHERE THERE IS ONE, AND THE ADDRESS
    OTHERWISE.** A sweep runs hourly; keyed on address alone, a laptop that
    takes a new DHCP lease overnight becomes a second row every morning and the
    inventory is unusable within a week. ARP gives an authoritative
    address<->MAC binding on-segment (§2.7), so a sighting carrying one is
    matched on it first, and the row it matches has its address CORRECTED
    rather than copied. Only ICMP-only sightings fall back to the address, and
    that fallback is why an ICMP-only machine on DHCP can still, in principle,
    be recorded twice -- there is nothing else about it to key on, and the
    honest answer is to say so rather than to invent a third key.

    **A ROW THE PLATFORM DID NOT WRITE IS NEVER EDITED HERE.** The correction
    above applies to `provenance='sweep'` rows with no agent. An operator's
    record, or a machine's own, moving because something answered a ping
    somewhere else would be the platform rewriting a human's data on the
    weakest evidence it has.

    **EVERY LOOKUP IS `org_id`- AND CLIENT-SCOPED IN THE PREDICATE**
    (invariants 5 and 5a). RLS says the same thing inside a tenant session, but
    this runs wherever the job engine runs it -- under the every-client scope --
    and a missing predicate here does not fail loudly: it concludes that another
    org's or another client's device already covers this address and writes
    nothing, leaving a permanent hole in an inventory nobody is watching.
    """
    from app.core.audit import AuditEntry, audit_service
    from app.models import Device

    if not sightings:
        return []

    # Read before anything is flushed: whose machines these are, and where.
    client_id = subnet.client_id
    subnet_id = subnet.id
    site_id = await _site_taking_machines(session, org_id, subnet)
    known = await _known_inventory(session, org_id, client_id)
    #: Rows created by THIS sweep. A document may legitimately report two
    #: addresses for one dual-homed MAC; the second must not "correct" the row
    #: the first just created back and forth.
    fresh: set[UUID] = set()
    results: list[AutoAssetResult] = []
    #: **EVERY AUDITED WRITE THIS SWEEP MAKES, IN ORDER, APPENDED ONCE.**
    #:
    #: `audit_service.record` takes a per-org advisory lock, reads the org's
    #: head hash and flushes -- three round trips, per row. This function runs
    #: once per host a sweep found, so a /24 paid that 254 times inside one
    #: transaction and a /16 far more; 4096 rows measured 14.7s.
    #:
    #: THE LOCK IS NOT DROPPED. `record_many` takes the same lock, once, and
    #: links the batch in Python from the head it read under it (see its
    #: docstring). Order is preserved because the chain IS the order: a
    #: `readdressed` event that landed after the `created` event of a row it
    #: corrected would read as a correction to something that did not exist yet.
    audit_entries: list[AuditEntry] = []

    for sighting in sightings:
        address = sighting.address
        mac = parse_mac(sighting.mac) if sighting.mac else None

        if address in known.addresses:
            results.append(
                AutoAssetResult(
                    address=address,
                    device_id=known.addresses[address],
                    created=False,
                    action="already recorded",
                )
            )
            continue

        if mac is not None and mac in known.macs:
            device_id, provenance, managed = known.macs[mac]
            action = "already recorded"
            if (
                device_id is not None
                and device_id not in fresh
                and provenance == "sweep"
                and not managed
            ):
                row = await session.get(Device, device_id)
                if row is not None:
                    previous = str(row.primary_ip) if row.primary_ip else None
                    row.primary_ip = address
                    if not row.hostname and sighting.hostname:
                        row.hostname = sighting.hostname
                    session.add(row)
                    audit_entries.append(
                        AuditEntry(
                            actor_type="system",
                            actor_id=None,
                            action="asset.auto_readdressed",
                            target_type="device",
                            target_id=str(device_id),
                            client_id=client_id,
                            metadata={
                                "address": address,
                                "previous_address": previous,
                                "mac": mac,
                                "confidence": sighting.confidence,
                                "sweep_run_id": str(sweep_run_id)
                                if sweep_run_id
                                else None,
                            },
                        )
                    )
                    if previous is not None:
                        known.addresses.pop(previous, None)
                    action = "readdressed"
            known.addresses[address] = device_id
            results.append(
                AutoAssetResult(
                    address=address, device_id=device_id, created=False, action=action
                )
            )
            continue

        if site_id is None:
            # NOWHERE TO FILE IT (C7): no device, no audit -- a candidate.
            results.append(
                AutoAssetResult(
                    address=address, device_id=None, created=False, action="candidate"
                )
            )
            continue

        device = Device(
            # STAMPED, as `promote` does: the audit event and the result below
            # name the row before any flush. Left to the column default it was
            # `None` here -- every `asset.auto_created` event said
            # `target_id="None"` -- until the batch flushed.
            id=uuid7(),
            org_id=org_id,
            # The swept subnet's client and site.
            client_id=client_id,
            site_id=site_id,
            # HOW it arrived is still discovery; WHO stood behind it is nobody.
            source="discovery",
            provenance="sweep",
            # NOT `computer`, which is what an operator-facing default assumes.
            # An echo reply says something answered at an address; it does not
            # say what. `other` is the vocabulary's only honest value, and a
            # wrong one here is repeated on every screen that groups by type.
            asset_type="other",
            primary_ip=address,
            # Reverse DNS when the sweep got one, and NOTHING otherwise. A
            # fabricated name (`unknown-10.0.0.7`) is a string indistinguishable
            # from a real hostname to every screen, export and match rule that
            # reads the column later; a null one renders as the address
            # (`DevicesPage.assetLabel`) and claims nothing.
            hostname=sighting.hostname or None,
            # THE MAC IS WHAT LETS ENROLMENT JOIN THIS ROW INSTEAD OF ADDING A
            # SECOND ONE. `resolve_enrolment_target` matches an enrolling agent
            # to one agentless record on a physical MAC, in the `nics` shape
            # inventory writes -- so the agent lands on the row the sweep
            # created and replaces its guesses with reported facts.
            nics={"items": [{"identifier": mac, "mac": mac}]} if mac else None,
        )
        session.add(device)
        audit_entries.append(
            AuditEntry(
                actor_type="system",
                actor_id=None,
                action="asset.auto_created",
                target_type="device",
                target_id=str(device.id),
                client_id=client_id,
                # THE ACTOR IS THE SYSTEM, NOT THE USER WHO SCHEDULED THE SWEEP.
                # Attributing this to a person would put a human's name against
                # rows no human chose, which is the one thing an audit log must
                # never say. The evidence travels with it: `confidence` records
                # whether ARP or only ICMP saw this.
                metadata={
                    "address": address,
                    "mac": mac,
                    "hostname": sighting.hostname or None,
                    "confidence": sighting.confidence,
                    "provenance": "sweep",
                    "sweep_run_id": str(sweep_run_id) if sweep_run_id else None,
                    "subnet_id": str(subnet_id),
                },
            )
        )
        fresh.add(device.id)
        known.addresses[address] = device.id
        if mac is not None:
            known.macs[mac] = (device.id, "sweep", False)
        results.append(
            AutoAssetResult(
                address=address, device_id=device.id, created=True, action="created"
            )
        )

    # ONE APPEND FOR THE WHOLE SWEEP, and it is INSIDE the caller's transaction
    # exactly as the per-row appends were: the device rows and the audit events
    # that account for them commit together or not at all (invariant #4).
    await audit_service.record_many(session, org_id=org_id, entries=audit_entries)
    await session.flush()
    return results


#: Subnets whose MOST RECENT sweep attempt found no agent able to run it.
#:
#: THIS IS WHY IT EXISTS: an empty candidate list and "nothing could look" are
#: indistinguishable on screen and mean opposite things. The first says the
#: segment is clean; the second says the platform has no idea what is on it.
#: `IpamSweepRun.state='no_agent'` is already a first-class outcome rather than
#: an error in a log (ipam spec §2.10) precisely so a screen can say which of
#: the two it is looking at -- this query is the asset side reading it.
#:
#: The most recent run per subnet, not "any run ever": a subnet that had no
#: agent last month and has one now is swept, and reporting it as unsweepable
#: would be the same lie pointed the other way.
_UNSWEEPABLE_SQL = """
SELECT count(*) FROM (
    SELECT DISTINCT ON (r.subnet_id) r.subnet_id, r.state
      FROM ipam_sweep_run r
     WHERE r.org_id = :org
     ORDER BY r.subnet_id, r.started_at DESC
) AS latest
WHERE latest.state = 'no_agent'
"""


async def unsweepable_subnets(session: AsyncSession, org_id: UUID) -> int:
    """How many subnets nobody can sweep. See `_UNSWEEPABLE_SQL`."""
    return int(
        (await session.execute(text(_UNSWEEPABLE_SQL), {"org": str(org_id)})).scalar_one()
    )
