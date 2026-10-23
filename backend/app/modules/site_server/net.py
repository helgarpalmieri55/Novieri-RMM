"""Segment parsing, broadcast computation and MAC canonicalisation.

**PURE.** No database, no ORM, no `sqlalchemy` import, no `app.models` import —
the one `app.core` import is `app.core.i18n`, a dictionary lookup and a context
variable read that words the refusals in the request's language — so every
rule here is testable exhaustively without a fixture, and it is cheap enough to
test exhaustively that there is no excuse not to.

**THIS FILE IS WHERE §0.2's RULE IS IMPLEMENTED: the endpoint never decides what
it may touch.** `broadcast_for` turns an operator-declared segment into an
explicit broadcast ADDRESS, which is what goes into the signed job params. The
agent broadcasts to exactly that address and to nothing else. A CIDR handed down
for the agent to resolve would make an authorization revoked in the control
plane depend on the endpoint re-deriving something, which is the same class of
mistake IPAM's enumerated `targets` exists to avoid.

**IPv4 ONLY, AND THAT IS A DECISION RATHER THAN AN OVERSIGHT.** A Wake-on-LAN
magic packet is carried by an IPv4 UDP broadcast. IPv6 has no broadcast address
at all — the nearest equivalent is the all-nodes link-local multicast group, a
different protocol with different routing behaviour and a different
authorization question. `parse_segment` therefore refuses an IPv6 prefix by
name, rather than accepting one and producing an address that cannot carry the
packet. The `cidr[]` column admits IPv6 because narrowing a shipped column type
later is expensive and narrowing a service rule is not.
"""

from collections.abc import Iterable
from ipaddress import AddressValueError, IPv4Address, IPv4Network, NetmaskValueError, ip_network

from app.core.i18n import tr
from app.modules.site_server.errors import SiteServerValidationError

__all__ = [
    "broadcast_for",
    "containing_segment",
    "normalize_mac",
    "parse_address",
    "parse_segment",
    "segment_strings",
]

_MAC_LENGTH = 12


def parse_segment(value: str) -> IPv4Network:
    """An operator-declared segment, or a refusal naming what is wrong.

    `strict=True`: `10.20.7.4/24` is REFUSED rather than silently rounded to
    `10.20.7.0/24`. Rounding an operator's typo is how a declaration comes to
    mean something nobody wrote, and the segment is what the broadcast address
    is computed from — so the silent correction would be a silent change to
    where a datagram lands.
    """
    if not isinstance(value, str) or not value.strip():
        raise SiteServerValidationError(tr("segment must be a non-empty CIDR string"))
    text = value.strip()
    try:
        network = ip_network(text, strict=True)
    except ValueError as exc:
        # `ip_network` folds "host bits set" and "not an address at all" into
        # one ValueError. Both are the operator's mistake and both are 400s;
        # the underlying message names which.
        raise SiteServerValidationError(
            tr("invalid segment {text}: {exc}", text=repr(text), exc=exc)
        ) from None
    if not isinstance(network, IPv4Network):
        raise SiteServerValidationError(
            tr(
                "segment {text} is IPv6: a Wake-on-LAN magic packet is carried by an IPv4 UDP "
                "broadcast and IPv6 has no broadcast address, so an IPv6 segment cannot be "
                "woken. Declare the IPv4 segment this server serves",
                text=repr(text),
            )
        )
    return network


def parse_address(value: str) -> IPv4Address:
    """A bare IPv4 address (the `segment`/observation side), or a refusal."""
    try:
        return IPv4Address(value.strip())
    except (AddressValueError, NetmaskValueError, AttributeError) as exc:
        raise SiteServerValidationError(
            tr("invalid IPv4 address {value}: {exc}", value=repr(value), exc=exc)
        ) from None


def broadcast_for(segment: IPv4Network | str) -> str:
    """The explicit broadcast ADDRESS of `segment`.

    `10.20.7.0/24` -> `10.20.7.255`; `10.20.0.0/16` -> `10.20.255.255`.

    TWO EDGE CASES WITH A STATED ANSWER RATHER THAN AN ACCIDENTAL ONE:

    * A `/31` (RFC 3021 point-to-point) has no reserved broadcast address.
      `ipaddress` reports its upper address, and that is what is returned: on a
      point-to-point link the packet reaches the only other host, which is the
      correct and only possible destination.
    * A `/32` is a single host. Its "broadcast" is that host's own address, so
      the datagram is a directed unicast. It will not wake a machine that is
      powered off and has no ARP entry — and that is the honest answer. The
      alternative, widening silently to some enclosing prefix, would send the
      packet to a segment the operator never declared, which is precisely what
      §0.2 forbids.
    """
    network = parse_segment(segment) if isinstance(segment, str) else segment
    return str(network.broadcast_address)


def containing_segment(
    segments: Iterable[IPv4Network | str], address: IPv4Address | str
) -> IPv4Network | None:
    """The MOST SPECIFIC declared segment containing `address`, or `None`.

    Most specific, not first: a branch server commonly declares both
    `10.20.0.0/16` and `10.20.7.0/24`, and the broadcast address that actually
    reaches a host on the /24 is `10.20.7.255`, not `10.20.255.255`. Returning
    the first match would make the answer depend on the order an operator
    happened to type the segments in.

    `None` is a REFUSAL, and every caller must treat it as one. It is never
    "fall back to the first segment": the caller has no basis for choosing a
    segment the address is not on, and guessing puts a datagram on a wire
    nobody authorized.
    """
    target = parse_address(address) if isinstance(address, str) else address
    best: IPv4Network | None = None
    for candidate in segments:
        network = parse_segment(candidate) if isinstance(candidate, str) else candidate
        if target not in network:
            continue
        if best is None or network.prefixlen > best.prefixlen:
            best = network
    return best


def segment_strings(segments: Iterable[IPv4Network | str]) -> list[str]:
    """`SiteServer.segments` as plain strings.

    **THE MODEL DECLARES `Mapped[list[str]]` AND THAT IS A CONVENIENT LIE.**
    asyncpg decodes a `cidr[]` column into `ipaddress` objects, so a row read
    back from the database carries `IPv4Network`s while a row just constructed
    in Python carries the `str`s that were assigned to it. Two shapes for one
    attribute is how an audit metadata dict comes to hold an unserialisable
    object — `app/jobs/canonical.py` refuses it by design, and the hash-chained
    audit write is where it surfaces, several layers from the cause.

    Every use site that leaves this module — audit metadata, API responses, job
    params — goes through here, so there is exactly one place that knows.
    """
    return [str(segment) for segment in segments]


def normalize_mac(value: str) -> str:
    """Any of the four forms an operator might paste, in ONE canonical form.

    `A4-BB-6D-11-22-33`, `a4:bb:6d:11:22:33`, `a4bb.6d11.2233` and
    `A4BB6D112233` all become `a4:bb:6d:11:22:33`. Canonicalising at the edge is
    what stops the same NIC appearing as two different targets in the audit log
    and in the signed params — the reason IPAM stores `macaddr` rather than
    text, applied to a value that never reaches a column.
    """
    if not isinstance(value, str):
        raise SiteServerValidationError(tr("MAC address must be a string"))
    digits = value.strip().lower()
    for separator in (":", "-", ".", " "):
        digits = digits.replace(separator, "")
    if len(digits) != _MAC_LENGTH or any(c not in "0123456789abcdef" for c in digits):
        raise SiteServerValidationError(
            tr(
                "invalid MAC address {value}: expected 12 hex digits, optionally separated by "
                "':', '-' or '.'",
                value=repr(value),
            )
        )
    return ":".join(digits[i : i + 2] for i in range(0, _MAC_LENGTH, 2))
