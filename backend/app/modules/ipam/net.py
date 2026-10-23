"""Pure address arithmetic for the address plan (spec §0.1, §0.4, §0.5, §4.1, §6.2).

No database, no ORM, no async, no I/O. Everything downstream is built on these
functions -- containment, the allocator, the authorized target set, and the
result-scope check -- so a boundary bug here is a *security* bug in the sweep
governance path (§6.2/§6.4), not a cosmetic one.

Three properties are load-bearing and are stated here because they are the
reason this module exists as a separate file:

1. **`scope_digest` has exactly one implementation.** It binds what the server
   authorized at dispatch to what the agent claims it probed. Two digests that
   drift is how a scope check passes on both sides while checking nothing --
   silently, with nothing failing. The dispatcher (§6.2) and the result handler
   (§6.4) both call *this* function.

2. **Every parser is total.** `parse_address` and `parse_mac` return `None`
   rather than raising, on any input, at any length. They sit on the inventory
   ingest path, and `JobEngine.complete` turns a raising result handler into a
   `failed` job -- a raising parser here would let a malformed NIC attribute
   break Phase 1's inventory collection (§4.1).

3. **The enumeration ceiling is one rule with two applications** (§0.4, §0.5):
   large IPv4 prefixes and *all* of IPv6 are never enumerated. `usable_hosts`
   yields nothing for IPv6 and `is_enumerable` refuses it, exactly as it refuses
   an IPv4 prefix over the ceiling. This deliberately parts company with
   `IPv6Network.hosts()`, which answers a /127 and a /128 -- the alternative is
   a family-dependent special case in the allocator that fires only on the one
   shape nobody allocates from.

Addresses are compared and ordered as addresses, never as text: `10.4.7.9`
precedes `10.4.7.10`, and `10.4.70.0/24` is not a near-miss for anything in
`10.4.7.0/24` (§0.1).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    collapse_addresses,
    ip_address,
    summarize_address_range,
)

__all__ = [
    "DEFAULT_ENUMERATION_MAX_HOSTS",
    "IPAddress",
    "IPNetwork",
    "canonical_targets",
    "intersect",
    "is_documentable",
    "is_enumerable",
    "parse_address",
    "parse_mac",
    "range_to_cidrs",
    "scope_digest",
    "subtract",
    "usable_host_count",
    "usable_hosts",
]

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network

#: §0.4's `Policy(module="ipam").config.enumeration_max_hosts` default. The
#: ceiling itself is configurable; this is the value the policy seeds with.
DEFAULT_ENUMERATION_MAX_HOSTS = 65_536

#: Longest text that could conceivably be an address: 39 characters of IPv6, a
#: `/128`, and a generous zone id. Anything longer is refused before parsing so
#: a 10 KB attribute value costs a length check rather than a parse.
_MAX_ADDRESS_TEXT = 128

#: `a4:bb:6d:11:22:33` and `A4-BB-6D-11-22-33`. The backreference is what
#: refuses `a4-bb:6d-11:22-33`: one separator, used consistently.
_MAC_PAIRS = re.compile(
    r"\A[0-9a-fA-F]{2}(?P<sep>[:-])(?:[0-9a-fA-F]{2}(?P=sep)){4}[0-9a-fA-F]{2}\Z"
)
#: `a4bb.6d11.2233` -- the Cisco form an ARP table prints.
_MAC_DOTTED = re.compile(r"\A[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\Z")
_MAC_SEPARATORS = str.maketrans("", "", ":-.")


def parse_address(value: str) -> IPAddress | None:
    """The address in `value`, or `None` if there is not one. Never raises.

    A prefix length (`10.1.2.42/24`) and a zone id (`fe80::1%4`) are stripped
    first. The zone matters: `ip_address("fe80::1%4")` parses in 3.12 and
    compares **unequal** to `fe80::1`, so carrying it would make one interface's
    address two different keys depending on which ingest path formatted it.

    Stripped, not ignored: a suffix that is present must be well formed, so
    `10.0.0.1/`, `10.0.0.1%` and `10.0.0.1/24/24` are `None` rather than
    silently accepted as `10.0.0.1`.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_ADDRESS_TEXT:
        return None
    text, slash, masklen = text.partition("/")
    if slash and not (masklen.isascii() and masklen.isdigit() and int(masklen) <= 128):
        return None
    text, percent, zone = text.partition("%")
    if percent and not zone:
        return None
    if not text:
        return None
    try:
        return ip_address(text)
    except ValueError:
        return None


def parse_mac(value: str) -> str | None:
    """`value` normalised to the lower-case colon form, or `None`. Never raises.

    The three textual forms `macaddr` accepts and this platform actually sees --
    `A4-BB-6D-11-22-33` from Windows, `a4:bb:6d:11:22:33` from Linux, and
    `a4bb.6d11.2233` from a switch's ARP table -- are one MAC, and §4's
    MAC-mismatch drift class compares them as strings. Unnormalised, that class
    is a false-positive generator (§0.1).

    Inventory's `identifier` is documented as "e.g. NIC MAC" and constrained to
    nothing, so §4.1 point 4 treats it as a MAC only when it parses as one;
    anything else records `mac = NULL`. That is why this is strict about the
    accepted forms rather than salvaging whatever hex it can find.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) not in (14, 17):
        return None
    if not (_MAC_PAIRS.match(text) or _MAC_DOTTED.match(text)):
        return None
    digits = text.translate(_MAC_SEPARATORS).lower()
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def is_documentable(addr: IPAddress) -> bool:
    """§4.1's discard rule, in one place.

    Loopback, link-local, multicast and unspecified addresses are never stored:
    a NIC reporting `169.254.x` is reporting that DHCP failed, which is not an
    IPAM fact, and `127.0.0.1` is true of every host in the estate.

    An IPv4-mapped v6 address is judged by the address it carries. In CPython
    3.12 `IPv6Address("::ffff:127.0.0.1").is_loopback` is `False`, so testing the
    classification properties alone would document a loopback address under a v6
    key.
    """
    if isinstance(addr, IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return not (addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified)


def usable_hosts(prefix: IPNetwork) -> Iterator[IPAddress]:
    """The addresses in `prefix` an operator may assign, ascending. Lazy.

    RFC 3021: both addresses are usable in a `/31` and the single address is
    usable in a `/32`; network and broadcast are excluded at masklen <= 30.
    That rule is CPython's `IPv4Network.hosts()`, which is called rather than
    re-derived.

    IPv6 yields **nothing**, at every prefix length (§0.5). The generator stays
    lazy so opening a /8 costs nothing -- §0.4's ceiling is enforced by the
    caller (`is_enumerable`), and a generator that materialised its range would
    have spent the cost before anyone checked it.
    """
    if isinstance(prefix, IPv6Network):
        return
    yield from prefix.hosts()


def usable_host_count(prefix: IPNetwork) -> int:
    """How many addresses `usable_hosts` would yield -- computed, not counted.

    This is the count of *enumerable* usable hosts, so it is `0` for every IPv6
    prefix (§0.5). It is not the size of the prefix.
    """
    if isinstance(prefix, IPv6Network):
        return 0
    if prefix.prefixlen >= prefix.max_prefixlen - 1:  # /31 and /32, RFC 3021
        return prefix.num_addresses
    return prefix.num_addresses - 2


def is_enumerable(prefix: IPNetwork, max_hosts: int = DEFAULT_ENUMERATION_MAX_HOSTS) -> bool:
    """May this prefix be enumerated address by address? (§0.4, §0.5)

    One rule, two applications: an IPv4 prefix above the ceiling and *any* IPv6
    prefix are both refused, with one error message. Callers above this line
    (next-free, the address grid, utilisation percentage) answer `422 subnet too
    large to enumerate` when it is `False`.
    """
    if isinstance(prefix, IPv6Network):
        return False
    return usable_host_count(prefix) <= max_hosts


def _canonical(nets: Iterable[IPNetwork]) -> list[IPNetwork]:
    """The minimal, sorted, deduplicated CIDR list covering exactly `nets`.

    Families are collapsed separately -- `collapse_addresses` refuses a mixed
    iterable, and mixed networks are not mutually orderable -- and v4 precedes
    v6 so the ordering is total and stable across calls. Stability is what makes
    `scope_digest` reproducible.
    """
    v4 = [n for n in nets if isinstance(n, IPv4Network)]
    v6 = [n for n in nets if isinstance(n, IPv6Network)]
    out: list[IPNetwork] = []
    if v4:
        out += sorted(collapse_addresses(v4))
    if v6:
        out += sorted(collapse_addresses(v6))
    return out


def _exclude(net: IPNetwork, hole: IPNetwork) -> list[IPNetwork]:
    """`net` minus `hole`. Two CIDRs either nest or are disjoint, so there are
    exactly three outcomes and no partial-overlap case to get wrong."""
    if net.version != hole.version:
        return [net]
    if hole.prefixlen <= net.prefixlen:
        # `hole` is the supernet (or the same block): it swallows `net` or misses it.
        return [] if net.subnet_of(hole) else [net]  # type: ignore[arg-type]
    if not hole.subnet_of(net):  # type: ignore[arg-type]
        return [net]
    return list(net.address_exclude(hole))  # type: ignore[arg-type]


def subtract(prefixes: Iterable[IPNetwork], holes: Iterable[IPNetwork]) -> list[IPNetwork]:
    """`prefixes` minus `holes`, as the minimal canonical CIDR list.

    §6.2's excluded ranges: a /24 minus a single address is 8 blocks, not 254
    `/32`s. The list goes into the *signed* job params and is capped by
    `max_targets`, so a non-minimal cover is a functional defect and not only an
    untidy one. Families never interact -- a v6 hole cannot punch a v4 prefix.
    """
    remaining = _canonical(prefixes)
    for hole in holes:
        if not remaining:
            break
        remaining = [piece for net in remaining for piece in _exclude(net, hole)]
    return _canonical(remaining)


def intersect(a: Iterable[IPNetwork], b: Iterable[IPNetwork]) -> list[IPNetwork]:
    """`a` ∩ `b`, as the minimal canonical CIDR list.

    §6.2 takes the intersection with the subnet's own prefix **first**, which is
    what makes §0.12's guarantee arithmetic rather than procedural: an
    authorization broader than the subnet cannot widen a sweep, because the
    intersection of a supernet and a subnet is the subnet.
    """
    left, right = _canonical(a), _canonical(b)
    out: list[IPNetwork] = []
    for x in left:
        for y in right:
            if x.version != y.version:
                continue
            if x.subnet_of(y):  # type: ignore[arg-type]
                out.append(x)
            elif y.subnet_of(x):  # type: ignore[arg-type]
                out.append(y)
    return _canonical(out)


def range_to_cidrs(start: IPAddress, end: IPAddress) -> list[IPNetwork]:
    """The minimal CIDR list covering `start..end` inclusive -- an `ipam_range`
    turned into holes for `subtract`. Total: a reversed or mixed-family range is
    `[]`, not an exception."""
    if start.version != end.version or int(end) < int(start):
        return []
    return list(summarize_address_range(start, end))


def canonical_targets(nets: Iterable[IPNetwork]) -> list[str]:
    """The sweep target list as §6.2 defines it: sorted, deduplicated, minimal,
    rendered as CIDR strings. This is the input `scope_digest` is computed over
    and the value that goes into the signed params."""
    return [str(n) for n in _canonical(nets)]


def scope_digest(targets: list[str]) -> str:
    """sha256 over the canonical target list (§5.2, §6.2, §6.4).

    **The only implementation.** The dispatcher computes it over the list it
    signs; the result handler recomputes it over the job's own signed
    `params.targets` and compares. A second implementation anywhere would let
    both sides agree while checking nothing.

    It hashes the list it is given and does not silently sort -- a caller that
    skips `canonical_targets` gets a different digest rather than a quietly
    equal one. The JSON encoding is what stops `["a","b"]` colliding with
    `["a,b"]`, which is precisely a scope-widening confusion.
    """
    payload = json.dumps(targets, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()
