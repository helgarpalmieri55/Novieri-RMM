"""`app/modules/site_server/net.py` — the pure half (spec §0.2, §5.2).

No database anywhere in this file, because there is none in the module under
test. That is the point of factoring it out: the rules that decide WHERE a
datagram is sent are testable exhaustively without a fixture, and exhaustively
is how they are tested.
"""

from ipaddress import IPv4Network

import pytest

from app.modules.site_server import net
from app.modules.site_server.errors import SiteServerValidationError

# --------------------------------------------------------------------------- #
# parse_segment
# --------------------------------------------------------------------------- #


def test_a_prefix_with_host_bits_set_is_refused_and_one_without_is_accepted() -> None:
    """BOTH HALVES IN ONE TEST. A `parse_segment` that refused everything would
    pass a test that only asserts the refusal, and would then make every
    promotion impossible."""
    with pytest.raises(SiteServerValidationError) as excinfo:
        net.parse_segment("10.20.7.4/24")
    assert "10.20.7.4/24" in str(excinfo.value)

    assert net.parse_segment("10.20.7.0/24") == IPv4Network("10.20.7.0/24")


@pytest.mark.parametrize(
    "value",
    ["", "   ", "not-a-cidr", "10.20.7.0/33", "10.20.7.0/-1", "999.1.1.0/24"],
)
def test_garbage_is_refused(value: str) -> None:
    with pytest.raises(SiteServerValidationError):
        net.parse_segment(value)


def test_an_ipv6_segment_is_refused_by_name() -> None:
    """A DECISION, NOT AN OVERSIGHT (see the module docstring).

    A magic packet rides an IPv4 UDP broadcast; IPv6 has no broadcast address at
    all. Accepting an IPv6 prefix would mean computing a "broadcast" that cannot
    carry the packet, and reporting `sent: true` for a datagram that reaches
    nothing. The `cidr[]` column still admits IPv6 — narrowing a shipped column
    type is expensive and narrowing a service rule is not.
    """
    with pytest.raises(SiteServerValidationError) as excinfo:
        net.parse_segment("2001:db8::/64")
    assert "IPv6" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# broadcast_for
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("10.20.7.0/24", "10.20.7.255"),
        ("10.20.0.0/16", "10.20.255.255"),
        ("10.0.0.0/8", "10.255.255.255"),
        ("192.168.1.0/30", "192.168.1.3"),
        # RFC 3021 point-to-point: no reserved broadcast address exists, and the
        # upper address is the only other host on the link.
        ("192.168.1.0/31", "192.168.1.1"),
        # A single host: the "broadcast" IS that host, so the datagram is a
        # directed unicast. Stated rather than stumbled into — the alternative,
        # widening to an enclosing prefix, would put the packet on a segment the
        # operator never declared.
        ("192.168.1.7/32", "192.168.1.7"),
    ],
)
def test_broadcast_addresses(segment: str, expected: str) -> None:
    assert net.broadcast_for(segment) == expected


def test_broadcast_for_accepts_a_parsed_network_too() -> None:
    assert net.broadcast_for(IPv4Network("172.16.0.0/12")) == "172.31.255.255"


# --------------------------------------------------------------------------- #
# containing_segment
# --------------------------------------------------------------------------- #


def test_the_most_specific_segment_wins() -> None:
    """THE FIXTURE MUST HOLD TWO OVERLAPPING SEGMENTS OR IT PROVES NOTHING.

    With one declared segment, "picked the most specific" and "picked the only
    one" are the same answer. Here `10.20.0.0/16` and `10.20.7.0/24` both
    contain the address, and only one of them yields a broadcast that reaches
    it: `10.20.7.255`, not `10.20.255.255`.
    """
    segments = ["10.20.0.0/16", "10.20.7.0/24"]
    chosen = net.containing_segment(segments, "10.20.7.31")
    assert chosen == IPv4Network("10.20.7.0/24")
    assert net.broadcast_for(chosen) == "10.20.7.255"


def test_declaration_order_does_not_change_the_answer() -> None:
    """The same two segments typed the other way round give the same result — a
    "first match wins" implementation fails exactly one of these two."""
    forwards = net.containing_segment(["10.20.0.0/16", "10.20.7.0/24"], "10.20.7.31")
    backwards = net.containing_segment(["10.20.7.0/24", "10.20.0.0/16"], "10.20.7.31")
    assert (forwards, backwards) == (IPv4Network("10.20.7.0/24"), IPv4Network("10.20.7.0/24"))


def test_an_address_on_no_declared_segment_is_none() -> None:
    """`None` is a REFUSAL and every caller treats it as one. Never "fall back
    to the first segment" — the caller has no basis for choosing a wire the
    address is not on."""
    assert net.containing_segment(["10.20.0.0/16", "10.30.0.0/16"], "10.40.1.1") is None


def test_containment_is_not_a_string_prefix_match() -> None:
    """`10.200.7.9` starts with `10.20` and is nowhere near `10.20.0.0/16`. This
    is the whole reason the column is `cidr[]` and the comparison is
    `ipaddress`, not `str.startswith`."""
    assert net.containing_segment(["10.20.0.0/16"], "10.200.7.9") is None


def test_containing_segment_accepts_parsed_networks() -> None:
    chosen = net.containing_segment(
        [IPv4Network("10.20.0.0/16"), IPv4Network("10.20.7.0/24")], "10.20.7.31"
    )
    assert chosen == IPv4Network("10.20.7.0/24")


# --------------------------------------------------------------------------- #
# normalize_mac
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        "a4:bb:6d:11:22:33",
        "A4:BB:6D:11:22:33",
        "A4-BB-6D-11-22-33",
        "a4bb.6d11.2233",
        "A4BB6D112233",
        "  a4:bb:6d:11:22:33  ",
    ],
)
def test_every_accepted_form_normalises_to_the_same_value(value: str) -> None:
    """ASSERT THE NORMALISED VALUE, not that parsing succeeded. "It parsed" is
    satisfied by a function that returns its input unchanged, and two spellings
    of one NIC in the audit log is exactly the drift this exists to stop."""
    assert net.normalize_mac(value) == "a4:bb:6d:11:22:33"


@pytest.mark.parametrize(
    "value",
    ["", "a4:bb:6d:11:22", "a4:bb:6d:11:22:33:44", "zz:bb:6d:11:22:33", "a4bb6d11223", "hello"],
)
def test_an_invalid_mac_is_refused(value: str) -> None:
    with pytest.raises(SiteServerValidationError):
        net.normalize_mac(value)


def test_segment_strings_normalises_both_shapes_the_attribute_can_hold() -> None:
    """`SiteServer.segments` genuinely holds two different types depending on
    whether the row was just constructed or read back from Postgres.

    asyncpg decodes `cidr[]` into `ipaddress` objects, so `list(row.segments)`
    is `[IPv4Network(...)]` after a round trip and `['10.20.0.0/16']` before
    one. Feeding the first into an audit metadata dict raises
    `TypeError: not canonically serializable: IPv4Network` from
    `app/jobs/canonical.py` at the hash-chain write, several layers from the
    cause — which is exactly how this was found.
    """
    mixed = [IPv4Network("10.20.0.0/16"), "10.30.7.0/24"]
    assert net.segment_strings(mixed) == ["10.20.0.0/16", "10.30.7.0/24"]


def test_net_is_pure() -> None:
    """No `sqlalchemy`, no `app.models`, no ORM import anywhere in this module.

    Asserted structurally rather than reviewed for: the moment this file can
    reach the database, "the broadcast address is computed from the declared
    segments" stops being a property of a pure function and becomes a property
    of whatever query somebody added.
    """
    import ast
    from pathlib import Path

    source = Path(net.__file__).read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    # The one `app.core` module allowed is `app.core.i18n`: translation is a
    # dictionary lookup and a context-variable read, with no database, and it is
    # how the refusals here are worded in the request's language. It is named
    # exactly, so nothing else under `app.core` comes in with it; the subprocess
    # test below checks what it loads transitively.
    forbidden = {
        name
        for name in imported
        if name.split(".")[0] in {"sqlalchemy", "alembic"}
        or name.startswith("app.models")
        or (name.startswith("app.core") and name != "app.core.i18n")
        or name.startswith("app.jobs")
        or name.startswith("app.gateway")
    }
    assert forbidden == set()


def test_net_loads_nothing_that_reaches_the_database() -> None:
    """The intent of `test_net_is_pure`, checked transitively.

    That test reads this module's own imports; this one imports the module in a
    fresh interpreter and looks at everything that came with it, so a database
    import added to anything `net.py` pulls in fails here too.
    """
    import subprocess
    import sys
    from pathlib import Path

    probe = (
        "import sys, app.modules.site_server.net\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in {'sqlalchemy', 'alembic'}"
        " or m.startswith('app.models') or m == 'app.core.db' or m.startswith('app.core.db.'))\n"
        "print(','.join(bad))\n"
    )
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(backend)!r})\n{probe}"],
        capture_output=True,
        text=True,
        cwd=backend,
        check=True,
    )
    assert result.stdout.strip() == "", result.stdout
