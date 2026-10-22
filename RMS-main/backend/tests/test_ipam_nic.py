"""The NIC attribute parser (`docs/modules/ipam/spec.md` §4.1, §0.17 layer 2).

WHAT THESE TESTS DELIBERATELY VARY, and why it matters here more than usual:

* **The hostile inputs are not all one shape.** `attributes` a list, an
  `addresses` entry that is an int, an `address` that is a 10 KB string,
  `identifier` `None`, a component with no `attributes` at all and an object
  that RAISES from its own attribute access are six different ways to break a
  parser, and a parser can be total against five of them and not the sixth.
* **`origin`, which used to be hard-coded in `_nic` and is now a parameter.**
  It was safe to freeze while nothing read it; a frozen value is unobservable
  now that §3.2's `allocation_method` is derived from it, and "the parser reads
  origin" and "the parser ignores origin and everything happens to be dhcp"
  would be the same green suite. `_nic` defaults to `dhcp` -- deliberately NOT
  the parser's own fallback -- so a test asserting `dhcp` is observing the read.
* **Every "returns nothing" assertion is `== []`, never `pytest.raises`.** The
  guarantee under test is "degrades to no observation", and `pytest.raises`
  passing tells you a `TypeError` was raised, which is the defect. A test that
  asserts "no exception" observes nothing when the parser starts returning
  garbage instead.

This parser is layer 2 of §0.17's two layers. Layer 1 (the SAVEPOINT in
`inventory.hooks`) is tested in `tests/test_inventory_ingest.py`, separately,
because a test of either alone passes against an implementation missing the
other.

WHY EVERY FIXTURE HERE CARRIES `attributes.addresses[]` AND NEVER
`attributes.ip`, stated because the previous revision of this file was full of
the latter. §14.1's block is now the ONLY address key: the agent stopped
emitting `ip` and this parser stopped reading it (OQ-2, closed 2026-08-05).

That removal is the reason most of these fixtures were rewritten rather than
left alone, and the reason matters. A test that seeds `{"ip": "127.0.0.1"}` and
asserts `== []` still PASSES against a parser that has stopped reading `ip`
entirely -- but it is no longer observing `is_documentable`, it is observing
nothing at all. Such a test is green and worthless. Every "yields nothing"
fixture below therefore expresses its input in the shape the parser actually
reads, so the assertion still discriminates. The single exception is
`test_the_removed_legacy_ip_key_is_not_read`, whose whole subject is the key's
absence and which carries a positive control in the same call so it cannot pass
by the parser being broken.
"""

from typing import Any

import pytest

from app.modules.ipam.nic import ORIGINS, NicObservation, observations_from_components


class _Component:
    """A stand-in for a `HardwareComponent` row or a `ComponentDoc`.

    `nic.py` imports nothing from `sqlalchemy` (§13), so it must duck-type
    whatever it is handed. Testing it against a plain object rather than an ORM
    row is what keeps that true.
    """

    def __init__(self, type: str = "nic", identifier: Any = "", attributes: Any = None) -> None:
        self.type = type
        self.identifier = identifier
        self.attributes = {} if attributes is None else attributes


class _Exploding:
    """A component whose attribute access raises. Nothing in this repository
    produces one -- it is here because "never raises" must survive the input
    nobody thought of, and `getattr` on a property is one line of somebody
    else's code."""

    type = "nic"

    @property
    def identifier(self) -> str:
        raise RuntimeError("boom")

    @property
    def attributes(self) -> dict[str, Any]:
        raise RuntimeError("boom")


def _nic(
    identifier: Any = "", *addresses: str, origin: str = "dhcp", **attributes: Any
) -> _Component:
    """A NIC in §14.1's shape, for the tests whose subject is something OTHER
    than the block's own parsing (MAC forms, dedup, discard, totality).

    A helper rather than a literal per test because the alternative -- repeating
    `{"addresses": [{"address": ..., "family": ..., "origin": ...}]}` twenty
    times -- is how a fixture rewrite introduces a typo that makes half the file
    observe nothing while staying green.

    `origin` IS A PARAMETER AND NOT A LITERAL. It used to be hard-coded `"dhcp"`
    here, which was harmless while nothing read it and is not now: a value this
    helper holds constant is a value no test in this file can observe. The tests
    whose subject IS the origin pass it explicitly; the rest keep `dhcp`, which
    is deliberately NOT the parser's fallback so a test that asserts `dhcp` is
    observing the read rather than the default.
    """
    attributes["addresses"] = [
        {"address": a, "family": "ipv6" if ":" in a else "ipv4", "origin": origin}
        for a in (addresses or ("10.1.2.42/24",))
    ]
    return _Component(identifier=identifier, attributes=attributes)


def test_the_block_yields_one_observation() -> None:
    """§14.1's block is the ONE IP-bearing NIC shape the parser reads."""
    parsed = observations_from_components(
        [
            _Component(
                identifier="a4:bb:6d:11:22:33",
                attributes={"addresses": [{"address": "10.1.2.42/24"}]},
            )
        ]
    )
    assert parsed == [NicObservation(address="10.1.2.42", mac="a4:bb:6d:11:22:33", name="")]


def test_the_removed_legacy_ip_key_is_not_read() -> None:
    """OQ-2, closed 2026-08-05: `attributes.ip` is gone from BOTH sides.

    THE POSITIVE CONTROL IS IN THE SAME CALL, and it is the whole reason this
    test is worth writing. "The legacy NIC produced no observation" is also what
    a completely broken parser produces, so on its own this assertion would pass
    against `return []`. The second component establishes that the parser is
    working and that the first one's silence is the removal.

    FALSIFYING OBSERVATION: the address list. Restoring the `attributes.get("ip")`
    read makes this print `['10.1.2.42', '10.9.9.9']` instead of `['10.9.9.9']`.
    """
    parsed = observations_from_components(
        [
            _Component(identifier="a4:bb:6d:11:22:33", attributes={"ip": "10.1.2.42"}),
            _Component(
                identifier="a4:bb:6d:11:22:34",
                attributes={"addresses": [{"address": "10.9.9.9/24"}]},
            ),
        ]
    )
    assert [o.address for o in parsed] == ["10.9.9.9"]


def test_an_identifier_that_is_not_a_mac_yields_mac_none() -> None:
    """§4.1 point 4: `identifier` is documented as "e.g. NIC MAC" and constrained
    to nothing, so it is a MAC only when it PARSES as one.

    IF THIS GUARD WERE DELETED (identifier used as the MAC unconditionally) the
    observation carries `mac='Ethernet0'`, which is not a `macaddr` -- the
    service-level test asserts the STORED ROW, where the mutation shows up as a
    `DataError` rather than as a different code path.
    """
    parsed = observations_from_components([_nic("Ethernet0")])
    assert parsed == [
        NicObservation(
            address="10.1.2.42", mac=None, name="Ethernet0", allocation_origin="dhcp"
        )
    ]


@pytest.mark.parametrize(
    "identifier,expected",
    [
        ("A4-BB-6D-11-22-33", "a4:bb:6d:11:22:33"),
        ("a4bb.6d11.2233", "a4:bb:6d:11:22:33"),
        ("a4:bb:6d:11:22:33", "a4:bb:6d:11:22:33"),
        ("", None),
        ("00:11:22:33:44", None),
        (None, None),
        (12345, None),
    ],
)
def test_mac_forms_normalise_or_degrade(identifier: Any, expected: str | None) -> None:
    parsed = observations_from_components([_nic(identifier)])
    assert [o.mac for o in parsed] == [expected]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1/8",
        "::1/128",
        "169.254.3.4/16",
        "fe80::1%4/64",
        "224.0.0.1/32",
        "0.0.0.0/0",
        "::/0",
    ],
)
def test_undocumentable_addresses_are_discarded(address: str) -> None:
    """§4.1: a NIC reporting `169.254.x` is reporting that DHCP failed, which is
    not an IPAM fact.

    IF `is_documentable` WERE DROPPED this yields one observation per address --
    seven rows of noise per device, and the loopback row is true of every host
    in the estate.

    SEEDED IN THE BLOCK, NOT IN THE REMOVED `ip` KEY. Phrased against `ip` this
    assertion is green against a parser that has stopped reading `ip` AND
    against one that has lost `is_documentable` entirely, because neither ever
    looks at the value. It would be a test of nothing.
    """
    assert observations_from_components([_nic("", address)]) == []


@pytest.mark.parametrize(
    "component",
    [
        _Component(attributes=["not", "a", "dict"]),
        _Component(attributes="10.1.2.42"),
        _Component(attributes=None),
        _Component(attributes={}),
        _Component(type="disk", attributes={"addresses": [{"address": "10.1.2.42/24"}]}),
        _Exploding(),
        object(),
        None,
        "10.1.2.42",
    ],
)
def test_malformed_components_yield_no_observations_and_do_not_raise(component: Any) -> None:
    """ASSERTS `== []`, NOT `pytest.raises`.

    `JobEngine.complete` (`app/jobs/engine.py:145-180`) converts a raising result
    handler into a `failed` job, so a `TypeError` from this function would let a
    malformed NIC attribute break Phase 1's inventory collection. "No exception
    was raised" is not an observation; the empty list is.

    THIS TABLE IS ABOUT THE COMPONENT, NOT THE ADDRESS VALUE. It used to also
    carry seven `{"ip": <garbage>}` rows -- an int, a `None`, a list, a 10 KB
    string, `10.1.2.999`, `dhcp`, `""`. With `ip` unread every one of them
    passed for the same reason `{"ip": "10.1.2.42"}` would have: the key is
    never touched. They were deleted rather than left green, and each has an
    exact counterpart in the shape the parser DOES read, in
    `test_a_malformed_addresses_block_yields_no_observations_and_does_not_raise`.
    """
    assert observations_from_components([component]) == []


def test_a_non_iterable_argument_yields_no_observations() -> None:
    """The argument itself is untrusted too -- a caller that hands this a `None`
    where a list was expected gets no observations, not a `TypeError` on the
    inventory result path."""
    assert observations_from_components(None) == []  # type: ignore[arg-type]
    assert observations_from_components(42) == []  # type: ignore[arg-type]


def test_one_bad_component_does_not_suppress_its_good_neighbours() -> None:
    """A per-component guard, not a per-call one. IF THE GUARD WERE AT THE CALL
    LEVEL (one `try` around the whole loop) this observes `[]` instead of the
    two good rows -- one malformed NIC would silently cost a device its entire
    address documentation.

    THREE KINDS OF BAD NEIGHBOUR, not one: attributes that are not a dict, an
    `addresses` value that raises on iteration, and a component whose own
    attribute access raises reach the guard by three different routes, and a
    guard can catch one and not the others. (This absorbed the near-identical
    `..._with_the_block` test, which differed only in which of the three it
    seeded -- once both were phrased against `addresses[]` they were one test
    written twice.)
    """
    parsed = observations_from_components(
        [
            _nic("a4:bb:6d:11:22:33", "10.1.2.42/24"),
            _Component(attributes=["broken"]),
            _Component(attributes={"addresses": _Exploding()}),
            _Exploding(),
            _nic("a4:bb:6d:11:22:34", "10.1.2.43/24"),
        ]
    )
    assert [o.address for o in parsed] == ["10.1.2.42", "10.1.2.43"]


def test_a_prefix_length_and_a_zone_id_are_stripped() -> None:
    """`net.parse_address` strips both, and this is the path that feeds it.

    The block always carries a prefix and a link-local IPv6 address always
    carries a zone, so this is the normal shape rather than an edge case. A zone
    id carried through would make one interface's address two different keys
    depending on which ingest path formatted it.

    `2001:db8::5%eth0/64` is deliberately a GLOBAL address wearing a zone: a
    zoned link-local one would be discarded by `is_documentable` before the
    stripping could be observed at all.
    """
    parsed = observations_from_components(
        [_nic("", "10.1.2.42/24", "2001:db8::5%eth0/64")]
    )
    assert [o.address for o in parsed] == ["10.1.2.42", "2001:db8::5"]


def test_duplicate_addresses_across_nics_collapse() -> None:
    """Two NICs reporting the same address and MAC are one observation. The
    upsert would collapse them anyway; doing it here keeps the count this
    function returns honest, which is what the ingest path logs.

    STILL LOAD-BEARING AFTER THE LEGACY KEY'S REMOVAL, which is worth saying
    because dedup was also what kept the two keys from double-counting. One
    interface can report the same address twice on its own (an alias, or the
    v4-mapped form beside the dotted quad), and two NICs bridged onto one
    segment report each other's.
    """
    parsed = observations_from_components(
        [
            _nic("a4:bb:6d:11:22:33", "10.1.2.42/24"),
            _nic("A4-BB-6D-11-22-33", "10.1.2.42/24"),
            _nic("a4:bb:6d:11:22:33", "10.1.2.42/24", "10.1.2.42/24"),
        ]
    )
    assert len(parsed) == 1


def test_the_same_address_on_two_macs_stays_two_observations() -> None:
    """Deduplication is on (address, mac), not on address alone: §0.15's
    accepted failure mode is that a changed MAC lands as a SECOND row, because
    §4.3's `mac_mismatch` and `conflict` predicates need both MACs to be able to
    say anything."""
    parsed = observations_from_components(
        [
            _nic("a4:bb:6d:11:22:33", "10.1.2.42/24"),
            _nic("a4:bb:6d:11:22:99", "10.1.2.42/24"),
        ]
    )
    assert len(parsed) == 2


def test_nic_module_imports_no_database() -> None:
    """§13: `nic.py` imports nothing from `sqlalchemy`. It runs on the inventory
    result path and its totality is the thing being relied on; a database call
    in here would be a database ERROR in here, which no `except` can contain
    (§0.17)."""
    import ast

    import app.modules.ipam.nic as module

    source = module.__file__
    assert source is not None
    with open(source) as handle:
        tree = ast.parse(handle.read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    # Asserted over the parsed IMPORT STATEMENTS, not over the file's text: this
    # module's docstring names `sqlalchemy` when explaining why it may not
    # import it, and a substring check would fail on the explanation while
    # passing on a lazily imported one inside a function.
    assert not [name for name in imported if name.split(".")[0] == "sqlalchemy"]
    assert not [name for name in imported if name.startswith("app.models")]
    assert imported <= {
        "__future__",
        "collections.abc",
        "dataclasses",
        "typing",
        "app.modules.ipam.net",
    }


# ---------------------------------------------------------------------------
# §14.1's `attributes.addresses[]` block -- the only address key
#
# These tests were written while `attributes.ip` was still read beside the
# block, so several of them seeded BOTH and asserted the pair deduplicated. That
# reading is gone (OQ-2, closed 2026-08-05: the platform is not live, so the
# mixed-version fleet the dual path insured against does not exist). Three tests
# whose entire subject was the interaction between the two keys are deleted --
# `test_both_keys_present_does_not_double_count_the_shared_address`,
# `test_an_ip_the_block_does_not_mention_is_still_observed`, and
# `test_the_legacy_only_shape_is_untouched_by_the_new_branch` -- and what
# replaces all three is `test_the_removed_legacy_ip_key_is_not_read` above,
# which asserts the removal with a positive control beside it.
#
# What did NOT change is the tolerance. Everything below about degrading to
# "no observation" rather than raising is unchanged and still pins §0.17's
# layer 2: removing a KEY is not removing the TOLERANCE, and a parser that
# raised on a malformed block would fail the whole `inventory.collect` job.
# ---------------------------------------------------------------------------


def test_the_addresses_block_yields_one_observation_per_address() -> None:
    """§14.1: one observation per ADDRESS, where the legacy shape gave one per NIC.

    IF THE `addresses[]` BRANCH WERE DELETED this returns `[]` -- not one
    observation, not a wrong one, nothing at all -- which is exactly the silent
    degradation the branch exists to prevent.
    """
    parsed = observations_from_components(
        [
            _Component(
                identifier="a4:bb:6d:11:22:33",
                attributes={
                    "name": "Ethernet0",
                    "mac": "a4:bb:6d:11:22:33",
                    "addresses": [
                        {"address": "10.1.2.42/24", "family": "ipv4", "origin": "dhcp"},
                        {"address": "10.1.2.43/24", "family": "ipv4", "origin": "static"},
                        {"address": "2001:db8::5/64", "family": "ipv6", "origin": "slaac"},
                    ],
                },
            )
        ]
    )
    # THE THREE ORIGINS DIFFER, and that is deliberate: one interface holding a
    # v4 DHCP lease, a static alias and a SLAAC v6 address is the ordinary
    # dual-stack shape, and an implementation that read `origin` once per
    # COMPONENT rather than once per ENTRY passes every other test in this file
    # and fails here.
    assert parsed == [
        NicObservation(
            address="10.1.2.42",
            mac="a4:bb:6d:11:22:33",
            name="Ethernet0",
            allocation_origin="dhcp",
        ),
        NicObservation(
            address="10.1.2.43",
            mac="a4:bb:6d:11:22:33",
            name="Ethernet0",
            allocation_origin="static",
        ),
        NicObservation(
            address="2001:db8::5",
            mac="a4:bb:6d:11:22:33",
            name="Ethernet0",
            allocation_origin="slaac",
        ),
    ]


def test_the_block_carries_the_mac_when_the_identifier_is_a_friendly_name() -> None:
    """§4.3's `mac_mismatch` cannot fire when the NIC identifier is a name.

    `identifier` alone cannot supply it: Windows reports `identifier` as
    "Ethernet0". §14.1's `attributes.mac` is the MAC source that survives that,
    which is the whole reason the drift class can fire on such a device at all.

    IF `attributes.mac` WERE NOT READ this observes `mac=None` and every Windows
    NIC stays outside `mac_mismatch` forever.
    """
    parsed = observations_from_components(
        [
            _Component(
                identifier="Ethernet0",
                attributes={
                    "mac": "A4-BB-6D-11-22-33",  # Windows' form; normalised by parse_mac
                    "addresses": [{"address": "10.1.2.42/24", "family": "ipv4", "origin": "dhcp"}],
                },
            )
        ]
    )
    assert parsed == [
        NicObservation(
            address="10.1.2.42",
            mac="a4:bb:6d:11:22:33",
            name="Ethernet0",
            allocation_origin="dhcp",
        )
    ]


def test_an_unparseable_attributes_mac_falls_back_to_the_identifier() -> None:
    """`attributes.mac` is agent-supplied and constrained to nothing, so it is a
    MAC only when it PARSES as one -- the same rule §4.1 point 4 applies to
    `identifier`. A garbage `mac` must not cost the NIC the good MAC in its
    identifier."""
    parsed = observations_from_components(
        [
            _Component(
                identifier="a4:bb:6d:11:22:33",
                attributes={"mac": "not-a-mac", "addresses": [{"address": "10.1.2.42/24"}]},
            )
        ]
    )
    assert [o.mac for o in parsed] == ["a4:bb:6d:11:22:33"]


def test_undocumentable_addresses_in_the_block_are_discarded() -> None:
    """§4.1's discard rule is the SERVER's, applied in one place
    (`net.is_documentable`).

    The agent deliberately does NOT pre-filter `addresses[]` -- it reports
    loopback and link-local too -- so this is the only place that judgement
    happens, on both ingest paths.
    """
    parsed = observations_from_components(
        [
            _Component(
                identifier="a4:bb:6d:11:22:33",
                attributes={
                    "addresses": [
                        {"address": "127.0.0.1/8", "family": "ipv4", "origin": "static"},
                        {"address": "fe80::1%4/64", "family": "ipv6", "origin": "link_local"},
                        {"address": "169.254.7.7/16", "family": "ipv4", "origin": "unknown"},
                        {"address": "10.1.2.42/24", "family": "ipv4", "origin": "dhcp"},
                    ]
                },
            )
        ]
    )
    assert [o.address for o in parsed] == ["10.1.2.42"]


def test_a_dual_stack_nic_reconciles_both_families() -> None:
    """§0.5: IPv6 is stored, documented and reconciled in v1 -- what is refused
    is ENUMERATION, not the family. One of the four degradations §14.1's block
    closed: the removed single-address key held the v4 address, so a dual-stack
    NIC's IPv6 address was never reconciled from inventory at all.

    (This absorbed `test_ipv6_is_parsed_not_refused`, which made the same
    assertion through the key that no longer exists.)
    """
    parsed = observations_from_components(
        [_nic("a4:bb:6d:11:22:33", "10.1.2.42/24", "2001:db8::42/64")]
    )
    assert [o.address for o in parsed] == ["10.1.2.42", "2001:db8::42"]


def test_an_origin_outside_the_enum_does_not_cost_the_address() -> None:
    """DELIBERATE DEVIATION FROM `docs/modules/agent/plan.md` TASK 11 STEP 1,
    which lists "an `origin` outside the enum" among the inputs that degrade to
    "no observation for that entry".

    Dropping the address would be TIGHTENING INBOUND, which
    `docs/kernel/wire-protocol/README.md` forbids: the day IPAM or an agent adds
    a sixth origin (`dhcpv6` is the obvious one), every address carrying it
    would vanish from the passive stream -- silently, because this parser is
    total. That is the same failure this whole task exists to prevent, one
    version later.

    **`origin` IS NOW READ, AND THIS TEST'S CLAIM IS UNCHANGED BY THAT.** The
    entry keeps its observation, at its address, with its MAC; only the origin
    degrades to `unknown`. Four unrecognisable shapes, because a parser can
    handle three of them and not the fourth: a plausible sixth enum member, a
    non-string, an absent key, and a case variant of a real one.

    ASSERTS THE ADDRESSES AND THE ORIGINS TOGETHER. The address list alone was
    the whole assertion while nothing read `origin`; on its own it now passes
    against an implementation that stored `"dhcpv6"` raw -- which is the version
    that turns a broken NIC into an `IntegrityError` against migration 0051's
    CHECK, on the inventory result path, where no `except` in `nic.py` can
    contain it.
    """
    parsed = observations_from_components(
        [
            _Component(
                identifier="a4:bb:6d:11:22:33",
                attributes={
                    "addresses": [
                        {"address": "10.1.2.42/24", "family": "ipv4", "origin": "dhcpv6"},
                        {"address": "10.1.2.43/24", "family": "ipv4", "origin": 7},
                        {"address": "10.1.2.44/24", "family": "ipv4"},
                        {"address": "10.1.2.45/24", "family": "ipv4", "origin": "DHCP"},
                    ]
                },
            )
        ]
    )
    assert [(o.address, o.allocation_origin) for o in parsed] == [
        ("10.1.2.42", "unknown"),
        ("10.1.2.43", "unknown"),
        ("10.1.2.44", "unknown"),
        ("10.1.2.45", "unknown"),
    ]


def test_every_member_of_the_enum_survives_the_parser() -> None:
    """The positive control for the test above, and the one that keeps it from
    being satisfied by a parser that answers `unknown` for everything.

    All five of §14.1's values, INCLUDING `link_local` -- which reaches here only
    on a documentable address, since `is_documentable` discards the addresses
    that normally carry it, and which is exactly the case §3.2 means when it says
    link-local is not an allocation method.
    """
    origins = ("static", "dhcp", "slaac", "link_local", "unknown")
    parsed = observations_from_components(
        [
            _Component(
                identifier="a4:bb:6d:11:22:33",
                attributes={
                    "addresses": [
                        {
                            "address": f"10.1.2.{index}/24",
                            "family": "ipv4",
                            "origin": origin,
                        }
                        for index, origin in enumerate(origins, start=10)
                    ]
                },
            )
        ]
    )
    assert [o.allocation_origin for o in parsed] == list(origins)
    assert set(ORIGINS) == set(origins)


def test_an_origin_never_splits_one_address_into_two_observations() -> None:
    """Dedup is on `(address, mac)` and the origin is NOT in the key.

    IF IT WERE, one interface reporting the same address twice with two origins
    -- which a v4-mapped form beside a dotted quad produces -- becomes two
    observations here and ONE row in `ipam_observation`, whose upsert key does
    not carry the origin either. The extra observation would be silently
    discarded at the INSERT, so the count this function returns (which the ingest
    path logs) would be a number that never matched the table.

    The FIRST sighting wins, which is the rule the rest of the loop follows.
    """
    parsed = observations_from_components(
        [
            _Component(
                identifier="a4:bb:6d:11:22:33",
                attributes={
                    "addresses": [
                        {"address": "10.1.2.42/24", "family": "ipv4", "origin": "dhcp"},
                        {"address": "10.1.2.42/24", "family": "ipv4", "origin": "static"},
                    ]
                },
            )
        ]
    )
    assert [(o.address, o.allocation_origin) for o in parsed] == [("10.1.2.42", "dhcp")]


@pytest.mark.parametrize(
    "addresses",
    [
        "10.1.2.42/24",
        {"address": "10.1.2.42/24"},
        42,
        None,
        ["10.1.2.42/24"],
        [None],
        [42],
        [[{"address": "10.1.2.42/24"}]],
        [{}],
        [{"address": None}],
        [{"address": 167838250}],
        [{"address": ""}],
        [{"address": "10.1.2.999/24"}],
        [{"address": "dhcp"}],
        [{"address": "x" * 10_240}],
        [{"address": "10.1.2.42/24/24"}],
        [{"addr": "10.1.2.42/24"}],
    ],
)
def test_a_malformed_addresses_block_yields_no_observations_and_does_not_raise(
    addresses: Any,
) -> None:
    """ASSERTS `== []`, NOT `pytest.raises` -- the same rule as the legacy table.

    `JobEngine.complete` turns a raising result handler into a `failed` job, so
    one malformed NIC block would break Phase 1 inventory for that device. IPAM
    must not be able to break inventory.
    """
    assert observations_from_components([_Component(attributes={"addresses": addresses})]) == []


def test_a_malformed_entry_does_not_suppress_the_good_entries_beside_it() -> None:
    """A per-ENTRY guard, not one guard around the component.

    IF ONE BAD ENTRY ABORTED THE WHOLE BLOCK, a NIC with a single unparseable
    alias would lose every other address on the interface -- and silently, since
    the parser returns `[]` rather than raising.

    THE GOOD ENTRIES ARE ON BOTH SIDES OF THE BAD ONES ON PURPOSE. With only a
    trailing good entry this passes against a guard that abandons the block on
    first failure but keeps what it had; with only a leading one it passes
    against a guard that skips to the end.

    (This replaces `test_a_malformed_block_does_not_suppress_the_legacy_key_beside_it`,
    whose surviving address came from the removed `ip` key -- post-removal that
    test observed one address instead of two and could no longer distinguish a
    per-entry guard from a per-component one at all.)
    """
    parsed = observations_from_components(
        [
            _Component(
                identifier="a4:bb:6d:11:22:33",
                attributes={
                    "addresses": [
                        {"address": "10.1.2.42/24"},
                        {"address": "not-an-address"},
                        None,
                        {"address": "10.9.9.9/24"},
                    ],
                },
            )
        ]
    )
    assert [o.address for o in parsed] == ["10.1.2.42", "10.9.9.9"]


def test_the_address_list_is_bounded_per_nic() -> None:
    """A hostile or broken agent must not make one NIC cost the ingest path
    unbounded work. This runs inside the `inventory.collect` result handler, in
    the job's transaction.

    FALSIFYING OBSERVATION: the returned count. An unbounded parser returns
    10,000 observations here and 10,000 upserts follow it.
    """
    addresses = [{"address": f"10.{i // 256 % 256}.{i % 256}.1/24"} for i in range(10_000)]
    parsed = observations_from_components(
        [_Component(identifier="a4:bb:6d:11:22:33", attributes={"addresses": addresses})]
    )
    assert len(parsed) == 64
