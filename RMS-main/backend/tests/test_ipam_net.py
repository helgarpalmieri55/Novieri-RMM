"""Pure address arithmetic -- IPAM plan Task 2 (spec §0.1, §0.4, §0.5, §4.1, §6.2).

`app.modules.ipam.net` has no database, no ORM and no I/O, so nothing here has an
excuse to sample where enumeration is possible. The set operations are checked
**exhaustively** over every hole geometry inside a /29 and every block pair
inside a /28, and the randomised sweeps are seeded (`DIFF_SEED`, the convention
`tests/test_sla_calendar.py` established) so a failure reproduces.

**How the reference stays independent of the implementation.** A differential
test whose reference shares the implementation's model agrees with its bugs by
construction. So the reference here uses a *different data model and a different
stdlib primitive*:

- **set ops** -- the implementation is CIDR algebra (`subnet_of`,
  `address_exclude`, `collapse_addresses`); the reference expands to integer
  address **sets**, applies `-` and `&`, and rebuilds CIDRs from contiguous runs
  via `summarize_address_range`.
- **`usable_hosts`** -- the implementation delegates to CPython's
  `IPv4Network.hosts()`; the reference reads RFC 3021 as arithmetic over
  `int(network_address) .. int(broadcast_address)`.
- **`is_documentable`** -- the implementation asks `.is_loopback` /
  `.is_link_local` / `.is_multicast` / `.is_unspecified`; the reference tests
  containment in the literal RFC prefixes (`127/8`, `169.254/16`, `224/4`,
  `::1/128`, `fe80::/10`, `ff00::/8`).
- **`scope_digest`** -- the implementation is `sha256(json.dumps(...))`; the
  reference is four hex constants pinned as literals in this file.

Neither side can be edited into agreement with the other by a single change, and
the reference contains no branch that mirrors an implementation branch.
"""

from __future__ import annotations

import ast
import inspect
import random
from collections.abc import Iterable, Iterator
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
    summarize_address_range,
)
from itertools import combinations, islice
from pathlib import Path

import pytest

from app.modules.ipam import net
from app.modules.ipam.net import (
    DEFAULT_ENUMERATION_MAX_HOSTS,
    canonical_targets,
    intersect,
    is_documentable,
    is_enumerable,
    parse_address,
    parse_mac,
    range_to_cidrs,
    scope_digest,
    subtract,
    usable_host_count,
    usable_hosts,
)

IPNet = IPv4Network | IPv6Network

DIFF_SEED = 20260802
DIFF_CASES = 1_500


# ---------------------------------------------------------------------------
# The independent reference: integer address sets, rebuilt as CIDRs from runs.
#
# Nothing below calls into `net.py`, and nothing below uses `address_exclude`,
# `subnet_of`, `supernet_of` or `collapse_addresses` -- the four primitives the
# implementation is built from. Usable only on small networks, which is why the
# exhaustive sweeps live inside a /29, a /28 and a /24.
# ---------------------------------------------------------------------------


def _ints(net_: IPNet) -> set[int]:
    return set(range(int(net_.network_address), int(net_.broadcast_address) + 1))


def _cover(nets: Iterable[IPNet]) -> dict[int, set[int]]:
    out: dict[int, set[int]] = {4: set(), 6: set()}
    for n in nets:
        out[n.version] |= _ints(n)
    return out


def _runs(values: set[int]) -> list[tuple[int, int]]:
    """Maximal contiguous runs of a set of integers, ascending."""
    runs: list[tuple[int, int]] = []
    for value in sorted(values):
        if runs and value == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], value)
        else:
            runs.append((value, value))
    return runs


def _rebuild(cover: dict[int, set[int]]) -> list[str]:
    """The minimal CIDR cover of an integer address set, v4 block then v6 block.

    A contiguous run's minimal cover is exactly `summarize_address_range`, and
    runs are separated by at least one absent address, so no block may span two
    runs. The concatenation is therefore minimal overall.
    """
    out: list[str] = []
    for version in (4, 6):
        cls: type[IPv4Address] | type[IPv6Address] = IPv4Address if version == 4 else IPv6Address
        for lo, hi in _runs(cover[version]):
            out += [str(n) for n in summarize_address_range(cls(lo), cls(hi))]
    return out


def _reference_subtract(prefixes: Iterable[IPNet], holes: Iterable[IPNet]) -> list[str]:
    left, right = _cover(prefixes), _cover(holes)
    return _rebuild({v: left[v] - right[v] for v in (4, 6)})


def _reference_intersect(a: Iterable[IPNet], b: Iterable[IPNet]) -> list[str]:
    left, right = _cover(a), _cover(b)
    return _rebuild({v: left[v] & right[v] for v in (4, 6)})


def _blocks(within: IPNet) -> list[IPNet]:
    """Every CIDR block inside `within`, all prefix lengths, `within` included."""
    out: list[IPNet] = [within]
    length = within.prefixlen
    while length < within.max_prefixlen:
        length += 1
        out += list(within.subnets(new_prefix=length))
    return out


def _as_str(nets: Iterable[IPNet]) -> list[str]:
    return [str(n) for n in nets]


# ---------------------------------------------------------------------------
# Structural: this module is pure, and `scope_digest` exists exactly once.
# ---------------------------------------------------------------------------

_IPAM_DIR = Path(net.__file__).parent


def test_net_module_imports_nothing_from_the_application() -> None:
    """Definition of done: no `app.models`, `sqlalchemy` or `app.core` import.

    Asserted over the parsed source rather than over `sys.modules`, so an
    import added inside a function body is caught too. If the guard were
    deleted a later edit could pull the ORM in here, and the exhaustive tests
    below would silently become database tests.
    """
    tree = ast.parse(Path(net.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    forbidden = {
        name
        for name in imported
        if name.split(".")[0] in {"sqlalchemy", "asyncpg", "fastapi", "pydantic"}
        or name.startswith(("app.models", "app.core", "app.jobs", "app.gateway"))
    }
    assert forbidden == set(), forbidden
    assert imported <= {"hashlib", "ipaddress", "json", "re", "collections.abc", "__future__"}


def test_scope_digest_has_exactly_one_implementation_in_the_ipam_package() -> None:
    """§6.2/§6.4: the dispatcher and the result handler must call the *same*
    function. Two digests that drift is how a scope check passes on both sides
    while checking nothing -- the failure is silent, and it is a security
    failure, so it is pinned structurally rather than left to review.

    If a later task re-implements the digest locally, this observes that file's
    name in `offenders` -- not merely "an exception was raised".
    """
    offenders: list[str] = []
    for path in sorted(_IPAM_DIR.glob("*.py")):
        if path.name == "net.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.lstrip(
                "_"
            ).startswith("scope_digest"):
                offenders.append(f"{path.name}::{node.name}")
    assert offenders == [], offenders


def test_every_exported_name_exists() -> None:
    for name in net.__all__:
        assert hasattr(net, name), name


# ---------------------------------------------------------------------------
# parse_address
# ---------------------------------------------------------------------------

HOSTILE: list[str] = [
    "",
    " ",
    "\n",
    "\x00",
    "dhcp",
    "DHCP",
    "none",
    "None",
    "null",
    "not an ip",
    "10.1.2.999",
    "10.1.2",
    "1.2.3.4.5",
    "10.0.0.256",
    "10.0.0.01",  # leading zero: ambiguous octal, refused since CPython 3.9.5
    "0x0a000001",
    "3232235777",
    "１.2.3.4",  # fullwidth digits
    "١.2.3.4",  # arabic-indic digits
    "1::2::3",
    "::ffff:999.1.1.1",
    "/24",
    "%eth0",
    "10.0.0.1/",
    "10.0.0.1%",
    "/",
    "%",
    "10.0.0.1/24/24",
    "-1.-1.-1.-1",
    "10.0.0.1;DROP TABLE ipam_address",
    "<script>alert(1)</script>",
    "10.0.0.1\n10.0.0.2",
    "10.0.0 .1",
    "x" * 10_240,
    "1" * 10_240,
    "10.0.0.1" + "/24" * 2_000,
    ":" * 10_240,
]


@pytest.mark.parametrize("value", HOSTILE, ids=lambda v: repr(v[:24]))
def test_parse_address_is_total_against_hostile_input(value: str) -> None:
    """Never raises. §4.1: a NIC attribute block that fails to parse degrades
    to "no observation" -- a raising parser would fail the whole
    `inventory.collect` job (`JobEngine.complete` converts a raising result
    handler to `failed`), so totality here is what stops this module breaking
    Phase 1."""
    assert parse_address(value) is None


def test_parse_address_never_raises_on_random_bytes() -> None:
    rng = random.Random(DIFF_SEED)
    alphabet = "0123456789abcdefABCDEF.:%/ xyz[]-"
    for _ in range(2_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 48)))
        result = parse_address(text)
        assert result is None or isinstance(result, IPv4Address | IPv6Address)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.1.2.42", "10.1.2.42"),
        ("  10.1.2.42\t", "10.1.2.42"),
        ("10.1.2.42/24", "10.1.2.42"),  # prefix length stripped
        ("10.1.2.42/32", "10.1.2.42"),
        ("0.0.0.0", "0.0.0.0"),
        ("255.255.255.255", "255.255.255.255"),
        ("::1", "::1"),
        ("2001:db8::1/64", "2001:db8::1"),
        ("fe80::1%4", "fe80::1"),  # zone id stripped
        ("fe80::1%eth0", "fe80::1"),
        ("fe80::1%eth0/64", "fe80::1"),
        ("FE80::AB", "fe80::ab"),
    ],
)
def test_parse_address_accepts_and_normalises(value: str, expected: str) -> None:
    parsed = parse_address(value)
    assert parsed is not None
    assert str(parsed) == expected


@pytest.mark.parametrize("value", ["::ffff:10.0.0.1", "::ffff:a00:1", "::FFFF:10.0.0.1"])
def test_an_ipv4_mapped_address_parses_to_one_address_whatever_its_spelling(
    value: str,
) -> None:
    """Asserted as an address, never as text, because **CPython re-spells this
    one between patch releases**: 3.12.3 renders it `::ffff:a00:1`, later 3.12s
    render it `::ffff:10.0.0.1`. This case sat in the table above with the
    3.12.3 spelling hardcoded, passed on the machine it was written on, and
    failed in CI on a newer 3.12 -- a green local suite proving nothing about
    the runner.

    The property that actually matters is the one asserted here: every spelling
    of a mapped address parses to a single address, so it is one key wherever
    IPAM stores or compares it. `str()` is CPython's business; equality is ours.
    """
    parsed = parse_address(value)
    assert parsed is not None
    assert parsed == ip_address("::ffff:10.0.0.1")
    assert parsed.version == 6


def test_a_zone_id_is_stripped_rather_than_carried() -> None:
    """`ip_address("fe80::1%4")` parses in 3.12 and compares **unequal** to
    `fe80::1`. Carrying the zone would make the same interface's address a
    different key on every ingest path that formatted it differently."""
    assert ip_address("fe80::1%4") != ip_address("fe80::1")
    assert parse_address("fe80::1%4") == ip_address("fe80::1")


def test_addresses_order_numerically_not_lexically() -> None:
    """§0.1's whole argument for native types: in text `10.4.7.9` sorts after
    `10.4.7.10`, and `10.4.70.0/24` matches a `LIKE '10.4.7%'` on a segment it
    has nothing to do with."""
    nine, ten = parse_address("10.4.7.9"), parse_address("10.4.7.10")
    assert nine is not None and ten is not None
    assert nine < ten
    assert str(nine) > str(ten)  # the text answer, which is the wrong one

    far = ip_network("10.4.70.0/24")
    near = ip_network("10.4.7.0/24")
    assert str(far).startswith("10.4.7")  # the text near-miss a LIKE would swallow
    assert not far.overlaps(near)
    assert intersect([far], [near]) == []


# ---------------------------------------------------------------------------
# parse_mac
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "a4:bb:6d:11:22:33",
        "A4:BB:6D:11:22:33",
        "a4-bb-6d-11-22-33",
        "A4-BB-6D-11-22-33",
        "a4bb.6d11.2233",
        "A4BB.6D11.2233",
        "  a4:bb:6d:11:22:33  ",
    ],
)
def test_parse_mac_normalises_the_three_textual_forms(value: str) -> None:
    """§0.1/§4: MAC mismatch is a drift class, and it is a comparison between a
    string a Windows agent formatted and a string an ARP table formatted. Without
    normalisation that class is a false-positive generator."""
    assert parse_mac(value) == "a4:bb:6d:11:22:33"


def test_all_three_forms_of_the_same_mac_collapse_to_one_value() -> None:
    forms = ["A4-BB-6D-11-22-33", "a4:bb:6d:11:22:33", "a4bb.6d11.2233"]
    assert len({parse_mac(f) for f in forms}) == 1


def test_parse_mac_round_trips_exhaustively_over_a_seeded_sample() -> None:
    """Every accepted form of a random MAC maps to the same colon form, and that
    form re-parses to itself."""
    rng = random.Random(DIFF_SEED)
    for _ in range(1_000):
        octets = [rng.randrange(256) for _ in range(6)]
        want = ":".join(f"{o:02x}" for o in octets)
        digits = "".join(f"{o:02X}" if rng.random() < 0.5 else f"{o:02x}" for o in octets)
        pairs = [digits[i : i + 2] for i in range(0, 12, 2)]
        forms = [
            ":".join(pairs),
            "-".join(pairs),
            ".".join(digits[i : i + 4] for i in range(0, 12, 4)),
        ]
        for form in forms:
            assert parse_mac(form) == want
        assert parse_mac(want) == want


BAD_MACS: list[str] = [
    "",
    " ",
    "a4:bb:6d:11:22",  # five octets
    "a4:bb:6d:11:22:33:44",  # seven
    "a4:bb:6d:11:22:3g",  # non-hex
    "a4bb6d112233",  # bare hex is not one of the three forms
    "a4-bb:6d-11:22-33",  # mixed separators
    "a4bb.6d11.22.33",
    "a4bb.6d11",
    "a4bb.6d11.2233.4455",
    "a4::bb:6d:11:22:33",
    "10.0.0.1",
    "eth0",
    "Intel(R) Ethernet Connection",
    "{4E1B2C3D-0000-0000-0000-000000000000}",
    "a" * 10_240,
    "a4:bb:6d:11:22:33:",
    "\x00a4:bb:6d:11:22:33",
    "a4:bb:6d:11:22:33\x00",
]


@pytest.mark.parametrize("value", BAD_MACS, ids=lambda v: repr(v[:24]))
def test_parse_mac_is_total_against_hostile_input(value: str) -> None:
    """§4.1 point 4: inventory's `identifier` is documented as "e.g. NIC MAC"
    and constrained to nothing. Anything that is not a MAC must record
    `mac = NULL`, not a half-parsed string."""
    assert parse_mac(value) is None


def test_parse_mac_never_raises_on_random_text() -> None:
    rng = random.Random(DIFF_SEED + 1)
    alphabet = "0123456789abcdefABCDEF:-. zZ"
    for _ in range(2_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        result = parse_mac(text)
        if result is not None:
            assert len(result) == 17
            assert parse_mac(result) == result


# ---------------------------------------------------------------------------
# is_documentable  (§4.1's discard rule, in one place)
# ---------------------------------------------------------------------------

_UNDOCUMENTABLE = [
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),
    ip_network("224.0.0.0/4"),
    ip_network("0.0.0.0/32"),
    ip_network("::1/128"),
    ip_network("fe80::/10"),
    ip_network("ff00::/8"),
    ip_network("::/128"),
]


def _reference_documentable(addr: IPv4Address | IPv6Address) -> bool:
    """The rule as literal RFC prefixes, not as CPython's boolean properties.

    IPv4-mapped v6 (`::ffff:127.0.0.1`) is resolved to its v4 address first --
    in CPython 3.12 `IPv6Address("::ffff:127.0.0.1").is_loopback` is **False**,
    so a property-only implementation would document a loopback address.
    """
    if isinstance(addr, IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return not any(addr in n for n in _UNDOCUMENTABLE if n.version == addr.version)


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1",
        "127.255.255.254",
        "::1",
        "169.254.3.4",
        "fe80::1",
        "febf::1",  # top of fe80::/10
        "224.0.0.1",
        "239.255.255.255",
        "ff02::1",
        "0.0.0.0",
        "::",
        "::ffff:127.0.0.1",  # loopback wearing a v6 hat
        "::ffff:169.254.3.4",
    ],
)
def test_is_documentable_is_false_for_the_discard_classes(value: str) -> None:
    """§4.1: a NIC reporting `169.254.x` is reporting that DHCP failed, which is
    not an IPAM fact."""
    addr = parse_address(value)
    assert addr is not None
    assert is_documentable(addr) is False


@pytest.mark.parametrize(
    "value",
    ["10.1.2.42", "192.168.0.1", "8.8.8.8", "172.16.0.1", "255.255.255.255",
     "2001:db8::1", "fc00::1", "fd12:3456::9", "2606:4700::1111", "::ffff:10.0.0.1"],
)
def test_is_documentable_is_true_for_real_addresses(value: str) -> None:
    addr = parse_address(value)
    assert addr is not None
    assert is_documentable(addr) is True


def test_is_documentable_matches_the_rfc_prefixes_over_an_exhaustive_v4_sweep() -> None:
    """Every address in eight /24s that straddle the discard boundaries, plus a
    seeded sweep of the whole v4 space. The reference is prefix containment;
    the implementation is CPython's classification properties."""
    for base in (
        "0.0.0.0/24",
        "126.255.255.0/24",
        "127.0.0.0/24",
        "127.255.255.0/24",
        "128.0.0.0/24",
        "169.253.255.0/24",
        "169.254.0.0/24",
        "169.254.255.0/24",
        "170.0.0.0/24",
        "223.255.255.0/24",
        "224.0.0.0/24",
        "239.255.255.0/24",
        "240.0.0.0/24",
    ):
        for addr in ip_network(base):
            assert is_documentable(addr) == _reference_documentable(addr), addr

    rng = random.Random(DIFF_SEED + 2)
    for _ in range(20_000):
        addr4 = IPv4Address(rng.randrange(2**32))
        assert is_documentable(addr4) == _reference_documentable(addr4), addr4


def test_is_documentable_matches_the_rfc_prefixes_over_a_v6_sweep() -> None:
    for base in ("::/120", "fe7f:ffff:ffff:ffff:ffff:ffff:ffff:ff00/120", "fe80::/120",
                 "febf:ffff:ffff:ffff:ffff:ffff:ffff:ff00/120", "fec0::/120",
                 "feff:ffff:ffff:ffff:ffff:ffff:ffff:ff00/120", "ff00::/120",
                 "2001:db8::/120"):
        for addr in ip_network(base):
            assert is_documentable(addr) == _reference_documentable(addr), addr

    rng = random.Random(DIFF_SEED + 3)
    for _ in range(20_000):
        addr6 = IPv6Address(rng.randrange(2**128))
        assert is_documentable(addr6) == _reference_documentable(addr6), addr6


# ---------------------------------------------------------------------------
# usable_hosts / usable_host_count / is_enumerable  (RFC 3021, §0.4, §0.5)
# ---------------------------------------------------------------------------


def _reference_usable(net_: IPNet) -> list[str]:
    """RFC 3021 read as arithmetic over the prefix's integer bounds.

    Deliberately *not* `net_.hosts()`: that is what the implementation calls, so
    using it here would prove only that the module can call itself. This is the
    rule as written in the spec -- network and broadcast excluded for masklen
    <= 30, both addresses usable in a /31, the single address usable in a /32,
    and nothing at all for IPv6 (§0.5: never enumerated).
    """
    if net_.version == 6:
        return []
    first, last = int(net_.network_address), int(net_.broadcast_address)
    if net_.prefixlen <= 30:
        first, last = first + 1, last - 1
    return [str(IPv4Address(i)) for i in range(first, last + 1)]


@pytest.mark.parametrize(
    ("prefix", "count", "first", "last"),
    [
        ("10.0.0.0/24", 254, "10.0.0.1", "10.0.0.254"),
        ("10.0.0.0/30", 2, "10.0.0.1", "10.0.0.2"),
        ("10.0.0.0/31", 2, "10.0.0.0", "10.0.0.1"),  # RFC 3021: both hosts
        ("10.0.0.4/31", 2, "10.0.0.4", "10.0.0.5"),
        ("10.0.0.7/32", 1, "10.0.0.7", "10.0.0.7"),
        ("10.0.0.0/29", 6, "10.0.0.1", "10.0.0.6"),
        ("10.0.0.0/16", 65_534, "10.0.0.1", "10.0.255.254"),
    ],
)
def test_usable_hosts_boundary_cases(prefix: str, count: int, first: str, last: str) -> None:
    hosts = list(usable_hosts(ip_network(prefix)))
    assert len(hosts) == count
    assert str(hosts[0]) == first
    assert str(hosts[-1]) == last
    assert usable_host_count(ip_network(prefix)) == count


@pytest.mark.parametrize(
    "prefix", ["2001:db8::/64", "2001:db8::/126", "2001:db8::/127", "2001:db8::1/128", "::/0"]
)
def test_usable_hosts_yields_nothing_for_every_ipv6_prefix(prefix: str) -> None:
    """§0.5 is one rule -- *no enumeration* -- and it applies to the whole v6
    family, not only to the large prefixes."""
    assert list(usable_hosts(ip_network(prefix))) == []
    assert usable_host_count(ip_network(prefix)) == 0
    assert is_enumerable(ip_network(prefix)) is False


def test_a_v6_slash_127_and_slash_128_are_refused_even_though_they_are_trivial() -> None:
    """The one place §0.5 deliberately parts company with the stdlib.
    `IPv6Network.hosts()` returns two addresses for a /127 and one for a /128,
    and both answers are arithmetically correct. They are still refused, because
    the alternative is a family-dependent special case in the allocator that
    only ever fires on the one shape nobody allocates from -- and §0.5's whole
    point is that "no enumeration" is *one* rule with two applications, not two
    rules. If this ever changes it must change in the spec first."""
    assert len(list(ip_network("2001:db8::/127").hosts())) == 2
    assert len(list(ip_network("2001:db8::1/128").hosts())) == 1
    assert list(usable_hosts(ip_network("2001:db8::/127"))) == []
    assert list(usable_hosts(ip_network("2001:db8::1/128"))) == []


def test_usable_hosts_matches_the_rfc_reference_for_every_prefix_in_a_24() -> None:
    """Exhaustive: all 511 CIDR blocks inside 10.0.0.0/24 (every prefix length
    24..32, every offset), address by address."""
    checked = 0
    for block in _blocks(ip_network("10.0.0.0/24")):
        assert _as_str(usable_hosts(block)) == _reference_usable(block), block
        assert usable_host_count(block) == len(_reference_usable(block)), block
        checked += 1
    assert checked == 511


def test_usable_hosts_matches_the_rfc_reference_across_the_whole_v4_prefix_range() -> None:
    """Every prefix length /0../32 at a seeded set of offsets. Shorter than /20
    the reference is too large to enumerate, so those lengths are checked by
    count plus the leading addresses -- and the count is where the network and
    broadcast rule shows up anyway."""
    rng = random.Random(DIFF_SEED + 4)
    for length in range(33):
        for _ in range(4):
            addr = IPv4Address(rng.randrange(2**32))
            block = ip_network(f"{addr}/{length}", strict=False)
            expected_count = block.num_addresses - (2 if length <= 30 else 0)
            assert usable_host_count(block) == expected_count, block
            if length >= 20:
                assert _as_str(usable_hosts(block)) == _reference_usable(block), block
            else:
                head = list(islice(usable_hosts(block), 5))
                assert str(head[0]) == str(
                    block.network_address + (1 if length <= 30 else 0)
                ), block
                assert len(head) == 5


def test_usable_hosts_is_lazy_so_a_large_prefix_costs_nothing_to_open() -> None:
    """§0.4: enumeration is capped by the *caller*. A generator that
    materialised its range would make the ceiling unenforceable -- the cost
    would already have been paid before anyone checked it."""
    hosts = usable_hosts(ip_network("10.0.0.0/8"))
    assert isinstance(hosts, Iterator)
    assert _as_str(islice(hosts, 3)) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert usable_host_count(ip_network("10.0.0.0/8")) == 16_777_214


@pytest.mark.parametrize(
    ("prefix", "enumerable"),
    [
        ("10.0.0.0/24", True),
        ("10.0.0.0/17", True),
        ("10.0.0.0/16", True),  # 65 534 usable, under the 65 536 ceiling
        ("10.0.0.0/15", False),
        ("10.0.0.0/8", False),
        ("10.0.0.1/32", True),
        ("2001:db8::/64", False),
        ("2001:db8::1/128", False),
    ],
)
def test_is_enumerable_carries_one_ceiling_for_both_families(
    prefix: str, enumerable: bool
) -> None:
    """§0.4 and §0.5 are the same rule applied twice: large IPv4 prefixes and
    *all* of IPv6 are refused enumeration. The default ceiling is §0.4's
    `enumeration_max_hosts`."""
    assert DEFAULT_ENUMERATION_MAX_HOSTS == 65_536
    assert is_enumerable(ip_network(prefix)) is enumerable


def test_is_enumerable_honours_a_caller_supplied_ceiling() -> None:
    """The ceiling is `Policy(module="ipam").config.enumeration_max_hosts`, so
    it is a parameter, not a constant baked into the arithmetic."""
    assert is_enumerable(ip_network("10.0.0.0/24"), max_hosts=254) is True
    assert is_enumerable(ip_network("10.0.0.0/24"), max_hosts=253) is False
    assert is_enumerable(ip_network("2001:db8::1/128"), max_hosts=10**9) is False


def test_usable_host_count_agrees_with_enumeration_wherever_enumeration_is_possible() -> None:
    """The count and the iterator are two answers to one question; a subnet
    page that reports 254 and a grid that renders 256 is the defect this pins."""
    rng = random.Random(DIFF_SEED + 5)
    for _ in range(200):
        length = rng.randint(20, 32)
        addr = IPv4Address(rng.randrange(2**32))
        block = ip_network(f"{addr}/{length}", strict=False)
        assert usable_host_count(block) == len(list(usable_hosts(block))), block


# ---------------------------------------------------------------------------
# intersect  (§6.2 -- "an authorization can never widen a sweep")
# ---------------------------------------------------------------------------


def test_intersect_of_a_supernet_authorization_with_a_subnet_returns_the_subnet() -> None:
    """§0.12/§6.2 as arithmetic, before it is tested as a service. An
    authorization for 10.0.0.0/8 must not licence probing anything outside the
    /24 that was asked about."""
    assert _as_str(intersect([ip_network("10.4.7.0/24")], [ip_network("10.0.0.0/8")])) == [
        "10.4.7.0/24"
    ]
    assert _as_str(intersect([ip_network("10.0.0.0/8")], [ip_network("10.4.7.0/24")])) == [
        "10.4.7.0/24"
    ]


def test_intersect_of_disjoint_sets_is_empty() -> None:
    assert intersect([ip_network("10.0.0.0/24")], [ip_network("10.0.1.0/24")]) == []
    assert intersect([ip_network("10.0.0.0/24")], []) == []
    assert intersect([], [ip_network("10.0.0.0/24")]) == []


def test_intersect_never_crosses_address_families() -> None:
    """A v4 authorization cannot bless a v6 target and vice versa. The naive
    implementation raises `TypeError` here, which in a dispatcher is a 500 on a
    dual-stack estate."""
    assert intersect([ip_network("10.0.0.0/24")], [ip_network("2001:db8::/32")]) == []
    mixed = intersect(
        [ip_network("10.0.0.0/24"), ip_network("2001:db8::/32")],
        [ip_network("10.0.0.128/25"), ip_network("2001:db8:1::/48")],
    )
    assert _as_str(mixed) == ["10.0.0.128/25", "2001:db8:1::/48"]


def test_intersect_is_exhaustive_over_every_block_pair_inside_a_28() -> None:
    """All 31 CIDR blocks inside 10.0.0.0/28 against all 31 -- 961 pairs, every
    nesting and disjointness relation two CIDRs can have."""
    blocks = _blocks(ip_network("10.0.0.0/28"))
    assert len(blocks) == 31
    for a in blocks:
        for b in blocks:
            assert _as_str(intersect([a], [b])) == _reference_intersect([a], [b]), (a, b)


def test_intersect_is_commutative_and_idempotent_over_seeded_sets() -> None:
    rng = random.Random(DIFF_SEED + 6)
    blocks = _blocks(ip_network("10.0.0.0/26"))
    for _ in range(DIFF_CASES):
        a = rng.sample(blocks, rng.randint(0, 4))
        b = rng.sample(blocks, rng.randint(0, 4))
        got = _as_str(intersect(a, b))
        assert got == _reference_intersect(a, b), (a, b)
        assert got == _as_str(intersect(b, a)), (a, b)
        assert got == _as_str(intersect(intersect(a, b), intersect(a, b)))


def test_intersect_output_is_canonical_and_minimal() -> None:
    """Two halves of a /24 authorized separately intersect a /24 subnet as one
    /24, not as two /25s -- otherwise the signed target list, and therefore the
    scope digest, depends on how the authorizations happened to be typed in."""
    assert _as_str(
        intersect(
            [ip_network("10.0.0.0/24")],
            [ip_network("10.0.0.0/25"), ip_network("10.0.0.128/25")],
        )
    ) == ["10.0.0.0/24"]


# ---------------------------------------------------------------------------
# subtract  (§6.2 -- the excluded ranges)
# ---------------------------------------------------------------------------


def test_subtract_the_documented_example() -> None:
    assert _as_str(subtract([ip_network("10.0.0.0/24")], [ip_network("10.0.0.128/25")])) == [
        "10.0.0.0/25"
    ]


def test_subtracting_one_host_produces_the_minimal_cover_not_254_slash_32s() -> None:
    """A /24 minus a single address is 8 blocks, not 254. The signed params
    carry this list and `max_targets` caps it, so a non-minimal cover is a
    functional bug and not only an aesthetic one."""
    got = subtract([ip_network("10.0.0.0/24")], [ip_network("10.0.0.100/32")])
    assert _as_str(got) == _reference_subtract(
        [ip_network("10.0.0.0/24")], [ip_network("10.0.0.100/32")]
    )
    assert _as_str(got) == ["10.0.0.0/26", "10.0.0.64/27", "10.0.0.96/30", "10.0.0.101/32",
                            "10.0.0.102/31", "10.0.0.104/29", "10.0.0.112/28", "10.0.0.128/25"]
    covered = {a for n in got for a in n}
    assert len(covered) == 255
    assert ip_address("10.0.0.100") not in covered


def test_subtract_removes_an_excluded_range_and_the_hole_is_really_absent() -> None:
    """Plan Task 8's falsification: `10.0.0.0/24` minus `.100-.200` must not
    contain `.150`. If the subtraction were dropped, `.150` is inside the target
    set and the agent probes an address an operator excluded."""
    holes = range_to_cidrs(ip_address("10.0.0.100"), ip_address("10.0.0.200"))
    got = subtract([ip_network("10.0.0.0/24")], holes)
    covered = {a for n in got for a in n}
    assert ip_address("10.0.0.150") not in covered
    assert ip_address("10.0.0.99") in covered
    assert ip_address("10.0.0.201") in covered
    assert len(covered) == 256 - 101


def test_subtract_of_a_covering_hole_is_empty() -> None:
    """§6.2: an empty target set is never dispatched. This is the arithmetic
    that produces it."""
    assert subtract([ip_network("10.0.0.0/24")], [ip_network("10.0.0.0/8")]) == []
    assert subtract([ip_network("10.0.0.0/24")], [ip_network("10.0.0.0/24")]) == []


def test_subtract_with_no_holes_still_canonicalises() -> None:
    assert _as_str(
        subtract([ip_network("10.0.0.128/25"), ip_network("10.0.0.0/25")], [])
    ) == ["10.0.0.0/24"]
    assert _as_str(
        subtract([ip_network("10.0.0.0/24"), ip_network("10.0.0.64/26")], [])
    ) == ["10.0.0.0/24"]


def test_subtract_never_crosses_address_families() -> None:
    got = subtract(
        [ip_network("10.0.0.0/29"), ip_network("2001:db8::/125")],
        [ip_network("10.0.0.0/32"), ip_network("2001:db8::/128")],
    )
    assert _as_str(got) == _reference_subtract(
        [ip_network("10.0.0.0/29"), ip_network("2001:db8::/125")],
        [ip_network("10.0.0.0/32"), ip_network("2001:db8::/128")],
    )
    assert _as_str(got) == ["10.0.0.1/32", "10.0.0.2/31", "10.0.0.4/30",
                            "2001:db8::1/128", "2001:db8::2/127", "2001:db8::4/126"]
    # A v6 hole cannot punch a v4 prefix.
    assert _as_str(subtract([ip_network("10.0.0.0/24")], [ip_network("2001:db8::/32")])) == [
        "10.0.0.0/24"
    ]


def test_subtract_is_exhaustive_over_every_hole_geometry_inside_a_29() -> None:
    """All 2**8 subsets of the eight addresses in 10.0.0.0/29 -- every possible
    arrangement of holes, including alternating ones that no CIDR can express as
    a single block."""
    base = ip_network("10.0.0.0/29")
    addresses = [ip_network(f"{a}/32") for a in base]
    for mask in range(256):
        holes = [addresses[i] for i in range(8) if mask >> i & 1]
        assert _as_str(subtract([base], holes)) == _reference_subtract([base], holes), mask


def test_subtract_is_exhaustive_over_every_small_hole_set_inside_a_28() -> None:
    """One size up: every set of at most three of the sixteen addresses in
    10.0.0.0/28 -- 697 arrangements, including the alternating ones that force a
    result no single CIDR can express."""
    base = ip_network("10.0.0.0/28")
    addresses: list[IPNet] = [ip_network(f"{a}/32") for a in base]
    checked = 0
    for size in range(4):
        for holes in combinations(addresses, size):
            assert _as_str(subtract([base], list(holes))) == _reference_subtract(
                [base], list(holes)
            ), holes
            checked += 1
    assert checked == 1 + 16 + 120 + 560


def test_subtract_is_exhaustive_over_every_block_pair_inside_a_28() -> None:
    """All 31 blocks singly and all 465 unordered pairs, against a /28 base and
    against a two-/29 base. 992 cases, no sampling."""
    blocks = _blocks(ip_network("10.0.0.0/28"))
    bases: list[list[IPNet]] = [
        [ip_network("10.0.0.0/28")],
        [ip_network("10.0.0.0/29"), ip_network("10.0.0.8/29")],
    ]
    for base in bases:
        for hole in blocks:
            assert _as_str(subtract(base, [hole])) == _reference_subtract(base, [hole]), hole
        for pair in combinations(blocks, 2):
            assert _as_str(subtract(base, list(pair))) == _reference_subtract(
                base, list(pair)
            ), pair


def test_subtract_matches_the_reference_over_seeded_random_sets() -> None:
    """Random prefix sets and random hole sets inside a /24, including holes
    that fall entirely outside the prefixes and holes that swallow them."""
    rng = random.Random(DIFF_SEED + 7)
    # Everything here is small enough for the reference to enumerate. The cases
    # the reference cannot hold -- a hole that swallows a whole /8 -- are pinned
    # by name above rather than sampled.
    universe: list[IPNet] = [
        *_blocks(ip_network("10.0.0.0/26")),
        ip_network("10.0.0.0/24"),
        ip_network("10.0.1.0/24"),
        ip_network("2001:db8::/126"),
        ip_network("2001:db8::/128"),
    ]
    for _ in range(DIFF_CASES):
        prefixes = rng.sample(universe, rng.randint(0, 3))
        holes = rng.sample(universe, rng.randint(0, 4))
        got = _as_str(subtract(prefixes, holes))
        assert got == _reference_subtract(prefixes, holes), (prefixes, holes)
        # Subtracting the same holes twice changes nothing.
        assert got == _as_str(subtract(subtract(prefixes, holes), holes))


def test_subtract_result_is_disjoint_and_maximally_collapsed() -> None:
    rng = random.Random(DIFF_SEED + 8)
    blocks = _blocks(ip_network("10.0.0.0/27"))
    for _ in range(DIFF_CASES):
        holes = rng.sample(blocks, rng.randint(0, 5))
        got = subtract([ip_network("10.0.0.0/27")], holes)
        for a, b in combinations(got, 2):
            assert not a.overlaps(b), (a, b)
        assert got == sorted(got)
        # No two entries may be merged: adjacent siblings would have collapsed.
        for a, b in zip(got, got[1:], strict=False):
            assert not (a.supernet() == b.supernet() and a.prefixlen == b.prefixlen)


# ---------------------------------------------------------------------------
# range_to_cidrs
# ---------------------------------------------------------------------------


def test_range_to_cidrs_covers_exactly_the_range() -> None:
    got = range_to_cidrs(ip_address("10.0.0.100"), ip_address("10.0.0.200"))
    covered = sorted(a for n in got for a in n)
    assert len(covered) == 101
    assert covered[0] == ip_address("10.0.0.100")
    assert covered[-1] == ip_address("10.0.0.200")
    assert _as_str(got) == ["10.0.0.100/30", "10.0.0.104/29", "10.0.0.112/28",
                            "10.0.0.128/26", "10.0.0.192/29", "10.0.0.200/32"]


def test_range_to_cidrs_is_exhaustive_over_every_range_inside_a_28() -> None:
    """All 136 ordered start/end pairs in a /28, checked against the integer
    set they must cover and for minimality."""
    base = list(ip_network("10.0.0.0/28"))
    checked = 0
    for i, start in enumerate(base):
        for end in base[i:]:
            got = range_to_cidrs(start, end)
            covered = {int(a) for n in got for a in n}
            assert covered == set(range(int(start), int(end) + 1)), (start, end)
            assert _as_str(got) == _rebuild({4: covered, 6: set()}), (start, end)
            checked += 1
    assert checked == 136


def test_range_to_cidrs_single_address_and_whole_prefix() -> None:
    assert _as_str(range_to_cidrs(ip_address("10.0.0.7"), ip_address("10.0.0.7"))) == [
        "10.0.0.7/32"
    ]
    assert _as_str(range_to_cidrs(ip_address("10.0.0.0"), ip_address("10.0.0.255"))) == [
        "10.0.0.0/24"
    ]
    assert _as_str(range_to_cidrs(ip_address("::"), ip_address("2001:db8::"))) != []


def test_range_to_cidrs_is_total_on_a_reversed_or_mixed_range() -> None:
    """The table has a CHECK that refuses `start > end`, so this is defence in
    depth -- but every other function in this module is total, and a single
    raising one is the one an ingest path will hit."""
    assert range_to_cidrs(ip_address("10.0.0.200"), ip_address("10.0.0.100")) == []
    assert range_to_cidrs(ip_address("10.0.0.1"), ip_address("2001:db8::1")) == []
    assert range_to_cidrs(ip_address("2001:db8::1"), ip_address("10.0.0.1")) == []


# ---------------------------------------------------------------------------
# canonical_targets and scope_digest
# ---------------------------------------------------------------------------


def test_canonical_targets_sorts_deduplicates_and_minimises() -> None:
    got = canonical_targets(
        [
            ip_network("10.0.0.128/25"),
            ip_network("10.0.2.0/24"),
            ip_network("10.0.0.0/25"),
            ip_network("10.0.0.128/25"),
        ]
    )
    assert got == ["10.0.0.0/24", "10.0.2.0/24"]
    # Adjacent siblings really do merge -- 10.0.0.0/24 + 10.0.1.0/24 is one /23,
    # which is why the case above uses a non-adjacent second subnet.
    assert canonical_targets([ip_network("10.0.1.0/24"), ip_network("10.0.0.0/24")]) == [
        "10.0.0.0/23"
    ]


def test_canonical_targets_orders_v4_before_v6() -> None:
    got = canonical_targets([ip_network("2001:db8::/64"), ip_network("10.0.0.0/24")])
    assert got == ["10.0.0.0/24", "2001:db8::/64"]


def test_canonical_targets_is_stable_under_input_permutation() -> None:
    rng = random.Random(DIFF_SEED + 9)
    nets = [
        ip_network(n)
        for n in ("10.0.0.0/25", "10.0.2.0/24", "10.0.0.128/25", "2001:db8::/64", "10.0.4.0/23")
    ]
    expected = canonical_targets(nets)
    for _ in range(50):
        shuffled = nets[:]
        rng.shuffle(shuffled)
        assert canonical_targets(shuffled) == expected


@pytest.mark.parametrize(
    ("targets", "digest"),
    [
        ([], "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
        (["10.0.0.0/24"], "1db24cf79eb2b8a9aaf0b2fa26b261f77fc82b99594d2d82a4affa61de30d2cb"),
        (
            ["10.0.0.0/25", "10.0.0.128/25"],
            "a5d033d69944178c18690ac8389e98aa01265ae42cea1f471d3f989d59c4213c",
        ),
        (
            ["10.0.0.0/24", "2001:db8::/64"],
            "fae3136f2ef7fe227e9b08365ba9ff3f9df748766e4f8556f20a6dc871c8f072",
        ),
    ],
)
def test_scope_digest_is_pinned_to_a_golden_value(targets: list[str], digest: str) -> None:
    """Pinned as literals rather than recomputed here, so the test cannot drift
    with the implementation. `[]` is `sha256(b"[]")`, which is independently
    checkable: `printf '[]' | sha256sum`.

    The digest is the binding between what was authorized at dispatch and what
    the agent claims it probed (§6.4). If its construction changes, every
    in-flight signed job stops verifying -- so it changing must be a deliberate,
    visible act.
    """
    assert scope_digest(targets) == digest
    assert len(scope_digest(targets)) == 64


def test_scope_digest_changes_when_any_single_target_changes() -> None:
    base = ["10.0.0.0/25", "10.0.0.128/25"]
    for altered in (
        ["10.0.0.0/25", "10.0.0.129/25"],
        ["10.0.0.0/25"],
        ["10.0.0.0/25", "10.0.0.128/26"],
        ["10.0.0.0/25", "10.0.0.128/25", "10.0.1.0/24"],
        ["10.0.0.0/24"],
        ["10.0.0.0/25", "10.0.0.128/25", ""],
    ):
        assert scope_digest(altered) != scope_digest(base), altered


def test_scope_digest_is_order_sensitive_but_canonical_targets_makes_it_stable() -> None:
    """The digest hashes the list it is given -- it does not silently sort, so
    a caller that skips `canonical_targets` gets a different answer rather than
    a quietly equal one. Stability comes from canonicalising first, which is
    what §6.2 specifies and what the dispatcher does."""
    assert scope_digest(["b", "a"]) != scope_digest(["a", "b"])

    rng = random.Random(DIFF_SEED + 10)
    nets = [ip_network(n) for n in ("10.0.0.0/25", "10.0.0.128/25", "10.0.2.0/24")]
    expected = scope_digest(canonical_targets(nets))
    for _ in range(50):
        shuffled = nets[:]
        rng.shuffle(shuffled)
        assert scope_digest(canonical_targets(shuffled)) == expected


def test_scope_digest_of_the_dispatch_path_equals_the_result_path() -> None:
    """§6.4 step 2: the result handler recomputes the digest from the job's own
    signed `params.targets`. Both sides call this one function, so the round
    trip is the identity -- and the test that would catch a second
    implementation appearing is
    `test_scope_digest_has_exactly_one_implementation_in_the_ipam_package`.
    """
    authorized = intersect([ip_network("10.4.7.0/24")], [ip_network("10.0.0.0/8")])
    targets = canonical_targets(subtract(authorized, [ip_network("10.4.7.128/25")]))
    dispatched = scope_digest(targets)
    # what the agent echoes back, verbatim, in the result document
    assert scope_digest(list(targets)) == dispatched
    assert targets == ["10.4.7.0/25"]


def test_scope_digest_is_not_confusable_by_target_concatenation() -> None:
    """JSON separators, not string joining: `["a/24","b/24"]` must not collide
    with `["a/24,b/24"]`. A digest built by `",".join(targets)` does collide,
    and the collision is exactly a scope-widening attack."""
    assert scope_digest(["10.0.0.0/24", "10.0.1.0/24"]) != scope_digest(
        ["10.0.0.0/24,10.0.1.0/24"]
    )


def test_scope_digest_source_is_the_only_hash_construction_and_is_documented() -> None:
    """The digest's construction is pinned in the source so a review sees a
    change to it as a change to *this* line, not as a diff in an unrelated
    helper."""
    source = inspect.getsource(scope_digest)
    assert "sha256" in source
    assert 'separators=(",", ":")' in source or 'separators=(",",":")' in source
