"""`canonical_bytes` is a cross-language contract; these guard it as one.

`app/jobs/canonical.py::canonical_bytes` produces the bytes every job
signature is computed over. Since `CLAUDE.md` commit `a32cead` the Go agent
lives under `agent/` and recomputes those bytes on the endpoint before
verifying the server's Ed25519 signature. Go's `encoding/json` cannot produce
them (HTML escapes, U+2028/U+2029, and float64 number rendering), so the agent
hand-rolls the encoder and pins it to
`agent/internal/canonical/testdata/corpus.jsonl` -- generated from THIS
function by `scripts/export_canonical_corpus.py`.

That makes any change to `canonical_bytes` a FLEET-WIDE BREAK with no
versioning story: signatures are computed by the new server and checked by
already-deployed agents built against the old bytes. The observable failure is
every job refused at the endpoint, with the Python suite green (it signs and
verifies with the same function) and the Go suite green (it is pinned to a
stale corpus). These tests are the only place that goes red.

`test_canonical_corpus_is_current` is the important one. The signed-job file is
checked for internal consistency rather than byte equality because its
signatures come from a freshly generated key each run.
"""

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from app.jobs.canonical import canonical_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTDATA = REPO_ROOT / "agent" / "internal" / "canonical" / "testdata"
CORPUS = TESTDATA / "corpus.jsonl"
SIGNED_JOBS = TESTDATA / "signed_jobs.jsonl"


def _load_generator():
    """Import scripts/export_canonical_corpus.py by path (not a package)."""
    path = REPO_ROOT / "scripts" / "export_canonical_corpus.py"
    spec = importlib.util.spec_from_file_location("_export_canonical_corpus", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _corpus_lines() -> list[dict]:
    return [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line]


def test_canonical_corpus_is_current() -> None:
    """The Go agent's canonical encoder is pinned to this file.

    If this guard were deleted, the observation is a corpus whose `canonical`
    values disagree with `app/jobs/canonical.py` -- and the Go side would keep
    passing its own tests while producing bytes the server never signed. Every
    job in the fleet would then be REFUSED, with no test in this repository
    red.
    """
    mismatches = []
    for index, entry in enumerate(_corpus_lines()):
        expected = canonical_bytes(entry["input"])
        recorded = base64.b64decode(entry["canonical"])
        if expected != recorded:
            mismatches.append(
                f"line {index + 1} ({entry['name']}): corpus has "
                f"{recorded!r}, canonical_bytes now produces {expected!r}"
            )
    assert not mismatches, (
        "the committed corpus no longer matches canonical_bytes:\n"
        + "\n".join(mismatches)
        + "\n\nIf canonical_bytes changed deliberately, understand first that it "
        "is a cross-language signing contract with no versioning story: every "
        "already-deployed agent verifies against the OLD bytes and will refuse "
        "every job. Regenerate with "
        "`backend/.venv/bin/python scripts/export_canonical_corpus.py`."
    )


def test_corpus_covers_the_cases_the_go_encoder_can_get_wrong() -> None:
    """A shrinking corpus is how a divergence stops being tested.

    Each name below is a way Go's encoder can differ from Python's, or a place
    a reader would reasonably expect it to. Dropping one silently retires the
    test that covers it on the Go side.
    """
    names = {entry["name"] for entry in _corpus_lines()}
    required = {
        "empty_object",
        "key_sort_order",
        "empty_and_escaped_keys",
        "html_characters",  # Go escapes < > & ; Python does not
        "backspace_and_formfeed",  # short forms
        "line_and_paragraph_separators",  # Go escapes U+2028/9 even with SetEscapeHTML(false)
        "non_ascii_passthrough",  # ensure_ascii=False
        "non_bmp",  # surrogate pair on the wire -> one 4-byte sequence
        "short_form_escapes",
        "control_characters",  # lowercase \\u00xx, DEL unescaped
        "numbers",  # float64 cannot hold 9007199254740993
        "literals",
        "nesting_and_empty_containers",
        "array_of_objects_keeps_order",
        "net_sweep_params",
        "health_policy_apply_params",
        "job_payload_shape",
    }
    assert required <= names, f"corpus lost these cases: {sorted(required - names)}"


def test_corpus_case_names_are_unique() -> None:
    names = [entry["name"] for entry in _corpus_lines()]
    assert len(names) == len(set(names)), "duplicate corpus case name"


def test_regenerating_the_corpus_reproduces_the_committed_file(tmp_path: Path) -> None:
    """The generator and the committed file must not have drifted apart.

    `test_canonical_corpus_is_current` catches a change to `canonical_bytes`.
    This catches the other direction: a case added to or edited in the
    generator without regenerating, which would leave the Go side pinned to a
    corpus nobody can reproduce.
    """
    generated = _load_generator().write_corpus(tmp_path)
    assert generated.read_text(encoding="utf-8") == CORPUS.read_text(encoding="utf-8"), (
        "scripts/export_canonical_corpus.py no longer reproduces the committed "
        "corpus.jsonl. Re-run it."
    )


def test_signed_jobs_verify_against_their_own_recorded_canonical_bytes() -> None:
    """The end-to-end fixture the Go side verifies Ed25519 signatures against.

    Byte equality is not asserted here: each regeneration signs with a fresh
    key, so the file is checked for INTERNAL consistency instead -- the frame's
    job payload canonicalises to the recorded bytes, and the recorded signature
    verifies over them with the recorded public key. A corrupted fixture would
    otherwise let the Go test "verify" against something the server never
    produced.
    """
    entries = [
        json.loads(line) for line in SIGNED_JOBS.read_text(encoding="utf-8").splitlines() if line
    ]
    assert entries, "signed_jobs.jsonl is empty"
    for entry in entries:
        frame = json.loads(entry["frame"])
        assert frame["v"] == 1 and frame["kind"] == "job"
        # The exact key set app/jobs/engine.py::_payload signs.
        assert set(frame["job"]) == {
            "id",
            "type",
            "device",
            "params",
            "issued_at",
            "expires_at",
            "nonce",
        }
        expected = canonical_bytes(frame["job"])
        assert expected == base64.b64decode(entry["canonical"]), (
            f"{entry['name']}: recorded canonical bytes are not what canonical_bytes produces"
        )
        key = load_pem_public_key(entry["public_key_pem"].encode())
        assert isinstance(key, Ed25519PublicKey)
        try:
            key.verify(base64.b64decode(frame["sig"]), expected)
        except InvalidSignature:  # pragma: no cover - a corrupted fixture
            pytest.fail(f"{entry['name']}: recorded signature does not verify over the payload")
