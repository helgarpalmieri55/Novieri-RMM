"""Declared segments versus what inventory actually saw (spec §11, §3.4, §0.8).

**THIS IS WHERE §3.3's DELIBERATE NON-CHECK BECOMES VISIBLE.** Promotion accepts
an operator's CLAIM about which segments a machine serves and does not verify it
(§0.8: a claim earns sweep preference, only evidence earns candidacy). That is
the right rule and it leaves a real question unanswered — "is this declaration
actually true?" — so this is where an operator finds out it is not, instead of
finding out when a sweep silently never prefers the branch server they promoted.

**THE DECLARATION IS AUTHORITATIVE AND THE OBSERVATIONS ARE ONLY EVIDENCE**
(§3.4). Nothing here rewrites `site_server.segments` from what inventory saw. An
inferred declaration would be unauditable — nobody granted it — and would
silently follow a NIC being moved.

**ONLY `source='inventory'` OBSERVATIONS COUNT.** A sweep observation has no
`device_id` at all (IPAM §6.4 pins it to NULL, because a sweep sees addresses
and MACs, not identity), so it can never be evidence about THIS device. Reading
sweep rows here would attribute every address on the segment to whichever
machine happened to probe it.

**IPAM MAY NOT BE INSTALLED, AND THE PROBE IS `to_regclass`, NOT `try/except`.**
IPAM is a pull-forward module and site servers must work in a deployment that
never installs it. A `try`/`except ProgrammingError` around a query against a
missing table would be a trap rather than a fallback: in PostgreSQL a failed
statement ABORTS THE ENCLOSING TRANSACTION, so every subsequent write in the
same request would fail with `current transaction is aborted`. The "graceful"
degradation would poison the request. `SELECT to_regclass(...)` asks without
executing anything that can fail.

**"NO MISMATCHES" AND "WE COULD NOT LOOK" ARE OPPOSITE ANSWERS** and must never
render the same, so absence yields an explicit `evidence="unavailable"` rather
than three empty lists. Three empty lists is also what a perfectly healthy site
server produces, and an operator cannot act on an answer that means both.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.site_server.net import parse_segment, segment_strings
from app.modules.site_server.registry import get_site_server

__all__ = ["SegmentCheck", "segment_check"]

@dataclass(frozen=True)
class SegmentCheck:
    """§11's three sets, plus the marker that says whether they mean anything.

    `evidence` is NOT decoration. With `"unavailable"` all three lists are empty
    because nothing could be looked up, which is the same shape a correctly
    declared, fully observed site server produces — and the opposite meaning.
    """

    site_server_id: UUID
    device_id: UUID
    evidence: Literal["ipam", "unavailable"]
    #: Declared segments with at least one current inventory observation inside.
    matched: list[str] = field(default_factory=list)
    #: Declared, but nothing observed inside them. NOT an error: a freshly
    #: promoted server has no observation until inventory next runs. It does
    #: mean the server has no sweep preference on that segment yet (§6.1).
    declared_unobserved: list[str] = field(default_factory=list)
    #: Addresses observed on this device that fall inside NO declared segment.
    #: Usually a second NIC, sometimes a declaration that is simply wrong.
    observed_undeclared: list[str] = field(default_factory=list)


async def _observation_evidence_available(session: AsyncSession) -> bool:
    """Is IPAM's observation table present in this deployment?

    Factored out so the `"unavailable"` branch is REACHABLE IN A TEST against a
    schema that does have the table — otherwise that branch is only exercisable
    by uninstalling IPAM, which means in practice it is never exercised at all.
    """
    found = (
        await session.execute(text("SELECT to_regclass('ipam_observation') IS NOT NULL AS ok"))
    ).scalar_one()
    return bool(found)


async def _ttl_days(session: AsyncSession, org_id: UUID) -> int:
    from app.modules.ipam.observations import observation_ttl_days

    return await observation_ttl_days(session, org_id)


async def segment_check(
    session: AsyncSession,
    org_id: UUID,
    site_server_id: UUID,
    *,
    now: datetime | None = None,
) -> SegmentCheck:
    """Compare what an operator declared against what inventory observed."""
    site_server = await get_site_server(session, org_id, site_server_id)
    declared = segment_strings(site_server.segments)

    if not await _observation_evidence_available(session):
        return SegmentCheck(
            site_server_id=site_server.id,
            device_id=site_server.device_id,
            evidence="unavailable",
        )

    stamp = now or datetime.now(UTC)
    cutoff = stamp - timedelta(days=await _ttl_days(session, org_id))
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT host(o.address) AS address FROM ipam_observation o"
                " WHERE o.org_id = :org AND o.device_id = :device"
                "   AND o.source = 'inventory' AND o.last_seen_at >= :cutoff"
                " ORDER BY host(o.address)"
            ),
            {"org": str(org_id), "device": str(site_server.device_id), "cutoff": cutoff},
        )
    ).all()
    observed = [row.address for row in rows]

    networks = [parse_segment(value) for value in declared]
    matched: list[str] = []
    declared_unobserved: list[str] = []
    for network, label in zip(networks, declared, strict=True):
        inside = any(_inside(network, address) for address in observed)
        (matched if inside else declared_unobserved).append(label)

    observed_undeclared = [
        address
        for address in observed
        if not any(_inside(network, address) for network in networks)
    ]

    return SegmentCheck(
        site_server_id=site_server.id,
        device_id=site_server.device_id,
        evidence="ipam",
        matched=matched,
        declared_unobserved=declared_unobserved,
        observed_undeclared=observed_undeclared,
    )


def _inside(network: object, address: str) -> bool:
    """Containment, tolerating an IPv6 observation against an IPv4 segment.

    A device legitimately has IPv6 addresses, and `IPv4Network.__contains__`
    raises `TypeError` rather than answering `False` for one. Swallowing that
    here is not a silent failure: an IPv6 address is genuinely not inside an
    IPv4 segment, and it correctly lands in `observed_undeclared`.
    """
    from ipaddress import ip_address

    from app.modules.site_server.net import parse_segment as _parse

    net = network if not isinstance(network, str) else _parse(network)
    try:
        return ip_address(address) in net  # type: ignore[operator]
    except TypeError:
        return False
