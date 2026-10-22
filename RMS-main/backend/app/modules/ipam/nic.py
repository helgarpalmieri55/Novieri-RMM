"""NIC attribute parsing (`docs/modules/ipam/spec.md` §4.1, §0.17 layer 2).

**PURE AND TOTAL. This function may not raise, for any input, ever.** It runs
inside the `inventory.collect` result path, and `JobEngine.complete`
(`app/jobs/engine.py:145-180`) converts a raising result handler into a `failed`
job -- so a `TypeError` here would let one machine's malformed NIC attribute
break Phase 1's inventory collection for that device. IPAM must not be able to
break inventory.

That guarantee is LAYER 2 of the two §0.17 defines. Layer 1 is the SAVEPOINT in
`app/modules/inventory/hooks.py`. They are independent on purpose: layer 1
contains a database error, which no `except` in this file could (a failed
statement poisons the enclosing transaction whether or not the Python exception
is caught), and layer 2 means the common case -- a NIC block this parser does
not understand -- never reaches layer 1 at all. A test of either alone passes
against an implementation missing the other, so `tests/test_ipam_nic.py` and
`tests/test_inventory_ingest.py` test them separately.

**WHAT IT READS.** `ComponentDoc.attributes` is `dict[str, Any]` and the
exported wire contract declares `"additionalProperties": true` -- there is no
NIC attribute contract in the JSON schema, so the shape below IS the contract
and this docstring is where it lives.

**Exactly one shape**: §14.1's block -- `{"name", "mac", "addresses":
[{"address", "family", "origin"}]}` -- which gives one observation per ADDRESS,
with a prefix length, and a MAC that survives an `identifier` that is a friendly
name.

**THERE WAS A SECOND ONE, AND IT IS GONE.** A legacy `{"identifier": "<mac>",
"attributes": {"ip": "10.1.2.42"}}` was read alongside the block and
deduplicated with it. That dual path existed for exactly one reason: a control
plane deployed before the block reads only `ip`, and this parser is total by
design -- it returns `[]` rather than raising -- so an agent that emitted the
block alone would have silenced a whole passive reconciliation stream with
nothing going red. `docs/kernel/wire-protocol/README.md`'s *loosen inbound
before you tighten outbound* is what made the server half land first.

That insurance was against a **deployed** fleet, and there is none: the platform
is not live, and no agent or control plane runs anywhere
(`docs/modules/agent/open-questions.md` OQ-2, closed 2026-08-05). What remained
was two code paths for one fact, which is how two implementations of the same
thing drift apart -- the shape that already produced an address grid computing
`conflicting` differently from the drift page. The agent stopped emitting `ip`
in the same commit (`agent/internal/capabilities/inventory/nic.go`), so the key
exists on neither side.

**Reinstating it is a wire-protocol decision, not a tolerance fix.** If an
inventory document ever arrives carrying `ip` and nothing else, the correct
outcome is the one this parser already gives -- no observation, no exception --
and the fix is on the emitting side or in a versioned contract change, not a
second reader here.

**REMOVING A KEY IS NOT REMOVING THE TOLERANCE.** Everything above about
totality is unchanged and load-bearing: an `attributes` that is not a dict, an
`addresses` that is not a list, an entry that is not a dict, an unparseable
address, a component whose own attribute access raises -- all still degrade to
"no observation" rather than raising, and `tests/test_ipam_nic.py` still pins
each one.

`origin` IS READ, and it is the one field here that is NORMALISED rather than
taken. `NicObservation.allocation_origin` carries it, `ipam_observation` stores
it (migration 0051) and §3.2's `allocation_method` is derived from it at read
time, so the fact the agent has supplied since §14.1 landed finally reaches the
field it exists for. It is called `allocation_origin` and not `origin` on this
side because `ipam_address.origin` already means something else -- how a ROW got
into the plan, which is a fact about an operator -- and the two are in scope
together downstream.

**AN `origin` THIS FILE DOES NOT RECOGNISE STILL COSTS THE ADDRESS NOTHING.**
The entry is kept, at the same address, with the same MAC and the same name;
only the origin degrades to `"unknown"`, which is the truthful answer. That is
not tightening inbound -- `docs/kernel/wire-protocol/README.md`'s rule forbids
REFUSING what a differently-versioned agent sends, and nothing is refused here.
The day someone adds a sixth origin (`dhcpv6` is the obvious one), addresses
carrying it keep flowing and their allocation method reads "we do not know"
until this side learns the word.

**AND THE NORMALISATION IS WHAT KEEPS THIS FUNCTION'S TOTALITY REACHING THE
DATABASE.** `ipam_observation.allocation_origin` is CHECK-constrained to §14.1's
five values. Passing an unrecognised string through would turn a broken NIC into
an `IntegrityError` on the inventory result path -- a DATABASE error, which no
`except` in this file can contain, because a failed statement poisons the
enclosing transaction whether or not the Python exception is caught. That is the
exact failure layer 1's SAVEPOINT exists to contain, and layer 2's job is to
make sure the common case never gets there. The CHECK and `ORIGINS` below are
two halves of one guarantee.

`identifier` is documented as "e.g. NIC MAC" and constrained to nothing, so it
is treated as a MAC **only when it parses as one** (§4.1 point 4) -- anything
else records `mac = NULL` rather than a garbage MAC that `macaddr` would refuse
at the INSERT.

No `sqlalchemy` import, no `app.models` import, no I/O: pinned by
`test_nic_module_imports_no_database`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.modules.ipam.net import is_documentable, parse_address, parse_mac

__all__ = ["ORIGINS", "UNKNOWN_ORIGIN", "NicObservation", "observations_from_components"]

#: §14.1's `addresses[]`, bounded. This runs inside the `inventory.collect`
#: result handler, in the job's transaction, so a hostile or broken agent
#: reporting ten thousand addresses on one interface must cost a slice of 64
#: rather than ten thousand upserts. Sixty-four aliases on one NIC is already
#: far past anything an estate does on purpose.
_MAX_ADDRESSES = 64

#: A NIC name is display-only -- it is never a reconciliation predicate and is
#: not stored on `ipam_observation`. Bounded anyway: this string comes from an
#: agent, and an unbounded one has no business travelling further than here.
_MAX_NAME = 200

#: §14.1's `origin` vocabulary, and the only values this parser carries forward.
#: Anything else -- a sixth origin a later agent invents, an int, a missing key,
#: an `addresses[]` entry from an agent that predates the field -- becomes
#: `UNKNOWN_ORIGIN`. See the module docstring for why that is a degradation and
#: not a refusal, and for why the degradation has to happen HERE rather than at
#: the INSERT.
#:
#: `migrations/versions/0051_ipam_allocation_origin.py` CHECK-constrains the
#: column to this same set and `tests/test_ipam_migration.py` asserts the two
#: agree, so a value added here without the migration fails a test rather than a
#: production INSERT.
ORIGINS: frozenset[str] = frozenset({"static", "dhcp", "slaac", "link_local", "unknown"})

#: What an unreadable, absent or unrecognised origin becomes. It is a REAL
#: member of §14.1's enum rather than a sentinel: "the endpoint did not tell us"
#: and "the endpoint told us it does not know" are the same fact to every reader
#: downstream, and inventing a sixth value to distinguish them would put a
#: string in the column that the agent's own contract does not contain.
UNKNOWN_ORIGIN = "unknown"


@dataclass(frozen=True)
class NicObservation:
    """One address a device reported about itself, with what came with it.

    Frozen and hashable so the caller can deduplicate on the whole value.
    `address` is the CANONICAL text of the parsed address -- not the string the
    agent sent -- so `10.1.2.42/24`, `10.1.2.42` and (later) `::ffff:10.1.2.42`
    do not become three keys for one interface.
    """

    address: str
    mac: str | None
    name: str
    #: §14.1's `origin` for THIS address, normalised to `ORIGINS`. Never a raw
    #: agent string: see `_origin_of`. Defaulted so that the many fixtures and
    #: callers that do not care about allocation still construct one, and so the
    #: field stays last in the frozen dataclass's ordering.
    allocation_origin: str = UNKNOWN_ORIGIN


def _name_of(identifier: Any, attributes: dict[str, Any], identifier_mac: str | None) -> str:
    """The interface's display name, best effort.

    §14.1's block carries `attributes.name`. The `identifier` fallback stays
    because `name` is not guaranteed: `attributes` has no schema, and an
    `identifier` that is not a MAC is a name (that is exactly the case §4.1
    point 4 describes). Using the identifier when it IS a MAC would label every
    interface with its own hardware address.

    The MAC passed here is the one parsed from `identifier`, NOT the
    component's resolved MAC. They differ exactly when §14.1's `attributes.mac`
    supplied a MAC for an interface whose identifier is a friendly name -- and
    that is the case where the identifier is the best name available.
    """
    candidate = attributes.get("name")
    if not isinstance(candidate, str) or not candidate.strip():
        candidate = identifier if isinstance(identifier, str) and identifier_mac is None else ""
    return candidate.strip()[:_MAX_NAME]


def _origin_of(entry: dict[str, Any]) -> str:
    """§14.1's `origin` for one `addresses[]` entry, normalised.

    A recognised member of `ORIGINS` passes through; EVERYTHING ELSE becomes
    `unknown` -- a value outside the enum, a non-string, a missing key, an entry
    from an agent built before the field existed. None of those cost the entry
    its observation, which is the whole point: this is a degradation of one
    field, not a rejection of the address.

    Deliberately NOT case-folding or trimming. §14.1's enum is the agent's
    contract and `agent/internal/capabilities/inventory/origin.go` emits exactly
    these lower-case words; accepting `"DHCP"` here would be this side quietly
    supporting a second spelling nothing on the wire produces, and the day a real
    second spelling appears it should be a decided wire-protocol change rather
    than something a `.lower()` absorbed years earlier without anyone noticing.
    """
    raw = entry.get("origin")
    if isinstance(raw, str) and raw in ORIGINS:
        return raw
    return UNKNOWN_ORIGIN


def _addresses_in(attributes: dict[str, Any]) -> list[tuple[str, str]]:
    """`(address string, normalised origin)` for §14.1's `attributes.addresses[]`,
    in order.

    Total, like everything else here: a block that is not a list, an entry that
    is not a dict and an `address` that is not a string each contribute nothing
    and never raise. Parsing is left to the caller, which is where the one
    `parse_address` / `is_documentable` / dedup path lives.

    THE ORIGIN IS READ PER ENTRY, NOT PER NIC. One interface routinely holds a
    DHCP v4 lease beside a SLAAC v6 address, which is exactly the case §14.1's
    example block shows; an origin taken once per component would label both with
    whichever came first.
    """
    block = attributes.get("addresses")
    if not isinstance(block, list):
        return []
    found: list[tuple[str, str]] = []
    for entry in block[:_MAX_ADDRESSES]:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("address")
        if isinstance(raw, str):
            found.append((raw, _origin_of(entry)))
    return found


def _mac_of(identifier_mac: str | None, attributes: dict[str, Any]) -> str | None:
    """The interface's MAC: §14.1's `attributes.mac` first, `identifier` second.

    `attributes.mac` is agent-supplied and constrained to nothing, so it is a
    MAC only when it PARSES as one -- the same rule §4.1 point 4 applies to
    `identifier`, and the reason a garbage `mac` does not cost the NIC the good
    MAC in its identifier.

    Preferring the attribute is what makes §4.3's `mac_mismatch` able to fire on
    a device whose `identifier` is a display name ("Ethernet0"), which is what
    Windows reports.
    """
    candidate = attributes.get("mac")
    if isinstance(candidate, str):
        parsed = parse_mac(candidate)
        if parsed is not None:
            return parsed
    return identifier_mac


def observations_from_components(components: Iterable[Any]) -> list[NicObservation]:
    """Every documentable address in `components`, in order, without duplicates.

    Accepts `HardwareComponent` rows, `ComponentDoc` objects, or anything else
    with `type` / `identifier` / `attributes` -- duck-typed deliberately, since
    importing either type here would import a database or a schema into the one
    function whose totality everything else is leaning on.

    Reads §14.1's `attributes.addresses[]` -- the only address key there is --
    deduplicated on `(address, mac)`. Deduplication survives the legacy key's
    removal on purpose: one interface can legitimately report the same address
    twice (an alias, or a v4-mapped form beside its dotted-quad), and §4.3's
    `mac_mismatch` needs the SAME address under two MACs to stay two rows.

    Returns `[]` -- never raises -- for a non-iterable argument, a component
    whose attribute access raises, attributes that are not a dict, an
    `addresses` that is not a list, an entry that is not a dict, an `address`
    that is not a parseable address, and an address that is not documentable
    (loopback, link-local, multicast, unspecified: §4.1's discard rule, which
    lives in `net.is_documentable` so the sweep path applies the same one).

    AN UNRECOGNISED `origin` IS NOT IN THAT LIST, and its absence is deliberate:
    it degrades the FIELD to `unknown` and keeps the observation. See the module
    docstring.
    """
    try:
        iterator = iter(components)
    except TypeError:
        # The ARGUMENT is untrusted too. A caller that hands this `None` gets no
        # observations rather than a `TypeError` on the inventory result path.
        return []

    seen: set[tuple[str, str | None]] = set()
    found: list[NicObservation] = []
    for component in iterator:
        # PER COMPONENT, not per call: one malformed NIC must not cost a device
        # its other interfaces. Everything inside is already total -- this
        # catches the input nobody thought of, such as a component object whose
        # own `__getattr__` raises.
        try:
            if getattr(component, "type", "nic") != "nic":
                continue
            attributes = getattr(component, "attributes", None)
            if not isinstance(attributes, dict):
                continue
            identifier = getattr(component, "identifier", None)
            identifier_mac = parse_mac(identifier) if isinstance(identifier, str) else None
            mac = _mac_of(identifier_mac, attributes)
            name = _name_of(identifier, attributes, identifier_mac)

            # §14.1's block, and nothing else. `attributes.ip` is not read:
            # see the module docstring for why the second reader was removed
            # rather than kept "just in case".
            for raw, origin in _addresses_in(attributes):
                address = parse_address(raw)
                if address is None or not is_documentable(address):
                    continue
                # DEDUPLICATED ON `(address, mac)`, WITH ORIGIN OUT OF THE KEY.
                # Adding it would turn one interface reporting `10.1.2.42` twice
                # with two different origins into TWO observations -- and the
                # upsert key in `ipam_observation` does not carry origin either,
                # so the second would simply overwrite the first on arrival. The
                # first sighting wins here, which is the same rule the rest of
                # this loop follows.
                key = (str(address), mac)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    NicObservation(
                        address=str(address),
                        mac=mac,
                        name=name,
                        allocation_origin=origin,
                    )
                )
        except Exception:  # noqa: BLE001 - totality is the contract, see the docstring
            continue
    return found
