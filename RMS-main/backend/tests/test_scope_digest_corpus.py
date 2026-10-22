"""`scope_digest` is a cross-language contract; these guard it as one.

`app/modules/ipam/net.py::scope_digest` binds what the server authorized at
dispatch to what the agent claims it probed. The module docstring calls it
"the only implementation", and the dispatcher (§6.2) and the result handler
(§6.4) do both call this one function -- but the AGENT cannot, and it must have
the answer BEFORE it emits a packet, because §6.4's rejection happens after the
probes have already gone out.

So the Go `net.sweep` capability recomputes it, and the rule it keeps is the
narrower one from `docs/modules/agent/plan.md` C6: it hashes `params.targets`
EXACTLY AS RECEIVED -- no sorting, no `collapse_addresses`, no re-rendering, no
normalisation. It is pinned to
`agent/internal/capabilities/netsweep/testdata/scope_digest.jsonl`, generated
from THIS function by `scripts/export_scope_digest_corpus.py`.

That makes any change to `scope_digest` a fleet-wide break with no versioning
story: the new server signs a digest that already-deployed agents compute
differently, every sweep is refused at the endpoint, and both suites stay green
-- the Python one because it hashes and compares with the same function, the Go
one because it is pinned to a stale corpus. These tests are the only place that
goes red.

`test_scope_digest_corpus_is_current` is the important one.
"""

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from app.modules.ipam.net import canonical_targets, scope_digest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = (
    REPO_ROOT
    / "agent"
    / "internal"
    / "capabilities"
    / "netsweep"
    / "testdata"
    / "scope_digest.jsonl"
)


def _load_generator():
    """Import scripts/export_scope_digest_corpus.py by path (not a package)."""
    path = REPO_ROOT / "scripts" / "export_scope_digest_corpus.py"
    spec = importlib.util.spec_from_file_location("_export_scope_digest_corpus", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _corpus_lines() -> list[dict]:
    # split("\n"), not splitlines(): Python's splitlines() also splits on
    # U+2028 and U+2029, and this corpus deliberately carries a case containing
    # both. The generator writes the file ensure_ascii=True so the bytes are
    # pure ASCII either way, and this reader does not depend on that.
    return [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").split("\n") if line]


def test_scope_digest_corpus_is_current() -> None:
    """The Go agent's digest is pinned to this file.

    If this guard were deleted, the observation is a corpus whose `digest`
    values disagree with `scope_digest` -- while the Go side keeps passing its
    own tests and computing a hash the server never produces. Every sweep in
    the fleet would then be REFUSED at the endpoint, before a packet, with no
    test in this repository red.
    """
    mismatches = []
    for index, entry in enumerate(_corpus_lines()):
        expected = scope_digest(entry["targets"])
        if expected != entry["digest"]:
            mismatches.append(
                f"line {index + 1} ({entry['name']}): corpus has {entry['digest']}, "
                f"scope_digest now produces {expected}"
            )
    assert not mismatches, (
        "the committed corpus no longer matches scope_digest:\n"
        + "\n".join(mismatches)
        + "\n\nIf scope_digest changed deliberately, understand first that it is a "
        "cross-language authorization check with no versioning story: every "
        "already-deployed agent recomputes it with the OLD rule and will refuse "
        "every sweep. Regenerate with "
        "`backend/.venv/bin/python scripts/export_scope_digest_corpus.py`."
    )


def test_the_recorded_payload_is_the_bytes_the_digest_is_of() -> None:
    """The corpus records the JSON text as well as the hash.

    A hash mismatch alone tells the Go side that two hex strings differ. The
    payload is what lets it print WHICH BYTES diverged, which is the difference
    between "the digest is wrong" and "we escape U+00E9 differently".
    """
    for entry in _corpus_lines():
        payload = base64.b64decode(entry["payload"])
        expected = json.dumps(entry["targets"], separators=(",", ":")).encode()
        assert payload == expected, entry["name"]
        assert hashlib.sha256(payload).hexdigest() == entry["digest"], entry["name"]


def test_corpus_covers_the_cases_the_go_encoder_can_get_wrong() -> None:
    """A shrinking corpus is how a divergence stops being tested.

    Each name below is a way the Go encoder can differ from Python's, or a
    place a reader would reasonably expect it to. `non_ascii_target` and
    `astral_target` are the load-bearing pair: `scope_digest` uses
    `json.dumps` at its DEFAULT `ensure_ascii=True`, while
    `app/jobs/canonical.py::canonical_bytes` uses `ensure_ascii=False`. They
    are different encoders, they agree on every well-formed CIDR, and reusing
    the agent's canonical writer here -- which the agent plan told the builder
    to do -- produces a hash this function never computes.
    """
    names = {entry["name"] for entry in _corpus_lines()}
    required = {
        "empty",
        "one_cidr",
        "many_cidrs",
        "unsorted_order",  # the list is hashed AS GIVEN
        "sorted_counterpart",  # ... and must differ from the unsorted one
        "duplicate_entries",
        "ipv6",
        "mixed_families",
        "single_addresses",
        "comma_collision_pair",  # ["a","b"] vs ["a,b"]
        "comma_collision_single",
        "quote_and_backslash",
        "control_characters",  # DEL is escaped here and not by canonical_bytes
        "non_ascii_target",  # ensure_ascii=True
        "astral_target",  # surrogate pair
        "u2028_u2029",
        "html_characters",
        "empty_string_entry",
        "max_length_entry",
    }
    assert required <= names, f"corpus lost these cases: {sorted(required - names)}"


def test_the_unsorted_case_really_does_differ_from_its_sorted_counterpart() -> None:
    """The property C6 turns on, asserted on the corpus itself.

    `scope_digest` "hashes the list it is given and does not silently sort".
    An agent that normalised before hashing would agree with the server on
    every canonical list -- which is all of them in practice -- and would stop
    checking anything on a list that was not. If these two entries ever hash
    the same, the corpus can no longer catch that.
    """
    by_name = {entry["name"]: entry for entry in _corpus_lines()}
    unsorted = by_name["unsorted_order"]
    ordered = by_name["sorted_counterpart"]
    assert sorted(unsorted["targets"]) == sorted(ordered["targets"])
    assert unsorted["targets"] != ordered["targets"]
    assert unsorted["digest"] != ordered["digest"]


def test_the_comma_collision_pair_hashes_differently() -> None:
    """The JSON encoding is what stops `["a","b"]` colliding with `["a,b"]`.

    net.py names it "precisely a scope-widening confusion": a digest built by
    joining with a separator would let ONE crafted entry stand in for TWO
    authorized ones.
    """
    by_name = {entry["name"]: entry for entry in _corpus_lines()}
    assert by_name["comma_collision_pair"]["digest"] != by_name["comma_collision_single"]["digest"]


def test_canonical_targets_still_feeds_this_function() -> None:
    """The digest is of `canonical_targets`' output in the dispatch path.

    Recorded as a test because C6's rule for the agent -- "hash what arrived,
    never re-derive it" -- is only safe while the SERVER is the side that
    canonicalises. If that ever moved, the agent would be hashing a
    non-canonical list and the two sides would still agree, which is the
    failure this whole arrangement exists to make impossible.
    """
    from ipaddress import ip_network

    # Non-adjacent prefixes on purpose: `_canonical` runs `collapse_addresses`,
    # so 10.20.30.0/24 and 10.20.31.0/24 come back as the single 10.20.30.0/23.
    # That collapsing is exactly why the agent must not re-derive this list --
    # it would have to reproduce the collapse to reproduce the digest.
    nets = [ip_network("10.20.40.0/24"), ip_network("10.20.30.0/24")]
    targets = canonical_targets(nets)
    assert targets == ["10.20.30.0/24", "10.20.40.0/24"]
    assert scope_digest(targets) != scope_digest(list(reversed(targets)))


def test_regenerating_the_corpus_reproduces_the_committed_file(tmp_path: Path) -> None:
    """The generator and the committed file must not have drifted apart.

    `test_scope_digest_corpus_is_current` catches a change to `scope_digest`.
    This catches the other direction: a case added to or edited in the
    generator without regenerating, which would leave the Go side pinned to a
    corpus nobody can reproduce.
    """
    generated = _load_generator().write_corpus(tmp_path)
    assert generated.read_text(encoding="utf-8") == CORPUS.read_text(encoding="utf-8"), (
        "scripts/export_scope_digest_corpus.py no longer reproduces the committed "
        "scope_digest.jsonl. Re-run it."
    )
