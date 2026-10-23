#!/usr/bin/env python3
"""Generate the golden corpus that pins the Go agent's canonical JSON encoder.

Run from repo root with the backend venv:
    backend/.venv/bin/python scripts/export_canonical_corpus.py

Writes two files under agent/internal/canonical/testdata/:

  corpus.jsonl      one {"name","why","input","canonical"} object per line.
                    `canonical` is base64 of canonical_bytes(input).
  signed_jobs.jsonl full JobEnvelope frames produced by the REAL signer
                    (app.core.signing.LocalEd25519Signer over
                    app.jobs.engine's payload shape), with the verification key.

WHY THIS FILE EXISTS
--------------------
`canonical_bytes` is now a CROSS-LANGUAGE contract. Go's `encoding/json` cannot
reproduce it -- it HTML-escapes `<`, `>` and `&`; it emits \\u0008/\\u000c where
Python emits \\b/\\f; it escapes U+2028/U+2029, which Python with
ensure_ascii=False does not; and decoding into `any` turns every number into a
float64, which reformats integers above 2^53. So the Go encoder is hand-rolled,
and a hand-rolled encoder pinned to its author's understanding of CPython's
`json.encoder` is pinned to nothing. It is pinned to THIS FILE, which is
produced by calling the Python function.

The failure mode of "close enough" is not garbled output. It is every job in
the fleet being REFUSED at the endpoint -- signature verification comparing the
agent's canonical bytes against a signature computed over the server's --
while every test in this repository stays green. Or, worse, someone relaxing
verification on the endpoint to make the symptom go away, which retires
invariant #1 on the only machine where it matters.

`backend/tests/test_canonical_corpus.py` asserts the committed corpus is
exactly what the current `canonical_bytes` produces, so a change to that
function goes red here rather than in the field.

THE NUMBER ARGUMENT
-------------------
The Go encoder emits a received number token VERBATIM rather than reformatting
it, and that is only sound because the server produces the wire text and the
canonical text from the same Python object with the same encoder: the gateway
sends `json.dumps(envelope)` (app/gateway/dispatch.py:65,
app/gateway/main.py:250) and signs `canonical_bytes(payload)`. `json.dumps`
renders ints with `int.__repr__` and floats with `float.__repr__` in both
places, so the token that arrives is already the token that was signed. The
`e21`/`1e-07`/`9007199254740993` cases below exist to keep that true.
"""

import base64
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.jobs.canonical import canonical_bytes  # noqa: E402

OUT_DIR = REPO_ROOT / "agent" / "internal" / "canonical" / "testdata"

# (name, why, value). `why` is written into the corpus so a Go subtest failure
# names the divergence it is about, not just an index.
CASES: list[tuple[str, str, Any]] = [
    ("empty_object", "the floor", {}),
    ("single_key", "the floor", {"a": 1}),
    (
        "key_sort_order",
        "Python sorts keys by code point; Go sorts strings by UTF-8 byte. Those "
        "agree for valid UTF-8, and this case is what proves it rather than the "
        "argument that they do",
        {"b": 1, "a": 2, "A": 3, "á": 4, "à": 5, "Z": 6, "z": 7, "É": 8},
    ),
    (
        "empty_and_escaped_keys",
        "keys go through the same string encoder as values; an encoder that "
        "escapes only values passes every value-only corpus",
        {"": "empty key", "a\nb": "newline in key", "<&>": "html in key"},
    ),
    (
        "html_characters",
        "encoding/json escapes < > & to \\u003c \\u003e \\u0026 unless "
        "SetEscapeHTML(false); Python never escapes them",
        {"s": "<script>a && b</script> --> done"},
    ),
    (
        "backspace_and_formfeed",
        "Python emits the short forms \\b and \\f; encoding/json emits \\u0008 "
        "and \\u000c",
        {"s": "a\bb\fc"},
    ),
    (
        "line_and_paragraph_separators",
        "encoding/json escapes U+2028/U+2029 as \\u2028/\\u2029; Python with "
        "ensure_ascii=False passes them through raw",
        # Written as escapes on purpose: these are invisible in an editor and
        # a literal one is exactly the kind of character a tool silently eats.
        {"s": "line\u2028sep para\u2029sep"},
    ),
    (
        "non_ascii_passthrough",
        "ensure_ascii=False means non-ASCII is raw UTF-8, not \\uXXXX",
        {"s": "日本語 — ✓ café Über шт"},
    ),
    (
        "non_bmp",
        "the gateway sends json.dumps with ensure_ascii=True, so an emoji "
        "arrives on the wire as a surrogate PAIR and must canonicalise back to "
        "one raw 4-byte UTF-8 sequence",
        {"s": "\U0001f680 \U0001f600 \U0001f1ec\U0001f1e7"},
    ),
    (
        "short_form_escapes",
        "the escapes both encoders agree on -- included so a rewrite that gets "
        "the disagreements right cannot get these wrong unnoticed",
        {"s": 'quote" back\\ slash/ tab\t nl\n cr\r'},
    ),
    (
        "control_characters",
        "everything below 0x20 without a short form is \\u00xx with LOWERCASE "
        "hex in Python; DEL (0x7f) is NOT escaped by either",
        {"s": "\x00\x01\x0b\x0e\x1f\x7f"},
    ),
    (
        "numbers",
        "9007199254740993 is the integer float64 cannot hold: decoding into "
        "`any` renders it 9007199254740992. -0.0, 1e+21 and 1e-07 pin Python's "
        "repr formatting, which Go's strconv does not reproduce",
        {
            "n": 0,
            "m": -0.0,
            "big": 9007199254740993,
            "huge": 123456789012345678901234567890,
            "f": 1.5,
            "e": 1e21,
            "tiny": 1e-7,
            "neg": -1,
            "negf": -1.5,
            "zero_f": 0.0,
            "third": 0.1 + 0.2,
        },
    ),
    ("literals", "true/false/null are emitted literally", {"t": True, "f": False, "z": None}),
    (
        "nesting_and_empty_containers",
        "an encoder that special-cases the empty object usually forgets the "
        "empty array, and vice versa",
        {"a": [1, {"b": []}, [], [[]], {}], "deep": {"x": {"y": {"z": 1}}}, "e": {}},
    ),
    (
        "array_of_objects_keeps_order",
        "arrays are ORDERED and must not be sorted; only object keys are",
        {"xs": [{"b": 1, "a": 2}, {"z": 3}, {"a": 4}]},
    ),
    (
        "net_sweep_params",
        "a realistic NetSweepParams (docs/modules/ipam/spec.md §5). `targets` "
        "is hashed as given, so its order must survive canonicalisation",
        {
            "subnet_id": "3f2a1b7c-0d4e-4a19-9f8b-2c6d5e4f3a21",
            "sweep_run_id": "b8e1c4d2-77aa-4f3e-8b21-9c0d3e5f7a14",
            "targets": ["10.20.30.0/24", "10.20.31.0/24", "192.168.7.128/25"],
            "scope_digest": "9f2c" + "0" * 60,
            "method": "icmp",
            "rate_pps": 200,
            "max_targets": 65536,
            "budget_s": 900,
            "probe_timeout_ms": 500,
        },
    ),
    (
        "health_policy_apply_params",
        "a realistic HealthPolicyApplyParams (docs/modules/"
        "endpoint-health-alerting/spec.md). `threshold` and `value` are strings "
        "by design; a numeric threshold here would be a different signed byte "
        "string",
        {
            "policy_version": 7,
            "tick_interval_s": 60,
            "posture_interval_s": 300,
            "summary_interval_s": 3600,
            "checks": [
                {
                    "check_id": "disk.free_pct",
                    "check_type": "disk_free_pct",
                    "selector": "/",
                    "comparator": "lt",
                    "threshold": "10",
                    "for_ticks": 3,
                    "cadence": "tick",
                },
                {
                    "check_id": "svc.sshd",
                    "check_type": "service_state",
                    "selector": "sshd",
                    "comparator": "ne",
                    "threshold": "running",
                    "for_ticks": 1,
                    "cadence": "posture",
                },
            ],
        },
    ),
    (
        "job_payload_shape",
        "the exact shape app/jobs/engine.py::_payload signs: seven keys, params "
        "nested, timestamps as rfc3339 strings",
        {
            "id": "6f1c2b3a-4d5e-4f60-8a91-b2c3d4e5f607",
            "type": "inventory.collect",
            "device": "11112222-3333-4444-5555-666677778888",
            "params": {},
            "issued_at": "2026-08-05T10:00:00Z",
            "expires_at": "2026-08-05T10:05:00Z",
            "nonce": "c0ffee00-0000-4000-8000-000000000001",
        },
    ),
    (
        "adversarial_mixture",
        "everything at once, nested -- the case that catches an encoder correct "
        "at the top level and wrong one frame down",
        {
            " key": {"<a>": ["b\bc", {"é": [1e-7, -0.0, None, True]}]},
            "z": "日\U0001f600\x1f&",
            "": [[], {}, ""],
        },
    ),
]


def write_corpus(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "corpus.jsonl"
    lines = []
    seen: set[str] = set()
    for name, why, value in CASES:
        if name in seen:
            raise ValueError(f"duplicate corpus case name {name!r}")
        seen.add(name)
        lines.append(
            json.dumps(
                {
                    "name": name,
                    "why": why,
                    "input": value,
                    "canonical": base64.b64encode(canonical_bytes(value)).decode("ascii"),
                },
                # ensure_ascii=True on purpose: it is what the gateway uses to
                # put the frame on the wire (json.dumps default), so the Go
                # side decodes the same escape forms it will meet in
                # production -- including surrogate pairs for non-BMP.
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_signed_jobs(out_dir: Path) -> Path:
    """Full envelopes from the real signer, so the Go side can verify end to end.

    Canonical bytes agreeing is the necessary half; the sufficient half is that
    an Ed25519 signature the server produced VERIFIES over the bytes the agent
    computed from the frame it received. This file carries both, so the Go test
    can do the whole chain without a running control plane.
    """
    import asyncio

    from app.core.signing import LocalEd25519Signer

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "signed_jobs.jsonl"

    async def build() -> list[str]:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            signer = LocalEd25519Signer(Path(tmp) / "job-signing.key")
            public_key_pem = await signer.public_key_pem()
            out = []
            jobs: list[tuple[str, str, dict[str, Any]]] = [
                ("plain", "hello", {}),
                ("html_in_params", "hello", {"message": "a <b> & c"}),
                ("unicode_in_params", "hello", {"message": "日本語 \U0001f680"}),
                (
                    "numbers_in_params",
                    "hello",
                    {"big": 9007199254740993, "tiny": 1e-7, "neg_zero": -0.0},
                ),
                (
                    "net_sweep",
                    "net.sweep",
                    {
                        "subnet_id": "3f2a1b7c-0d4e-4a19-9f8b-2c6d5e4f3a21",
                        "sweep_run_id": "b8e1c4d2-77aa-4f3e-8b21-9c0d3e5f7a14",
                        "targets": ["10.20.30.0/24", "10.20.31.0/24"],
                        "scope_digest": "9f2c" + "0" * 60,
                        "rate_pps": 200,
                        "budget_s": 900,
                    },
                ),
            ]
            # Index-based ids, not hash(): PYTHONHASHSEED is random per process,
            # so hashed ids would rewrite the file on every run and every diff
            # would look like a real change.
            for index, (name, job_type, params) in enumerate(jobs):
                # Exactly app/jobs/engine.py::_payload's shape and key set.
                payload = {
                    "id": f"00000000-0000-4000-8000-{index:012d}",
                    "type": job_type,
                    "device": "11112222-3333-4444-5555-666677778888",
                    "params": params,
                    "issued_at": "2026-08-05T10:00:00Z",
                    "expires_at": "2026-08-05T10:05:00Z",
                    "nonce": f"c0ffee00-0000-4000-8000-{index:012d}",
                }
                canonical = canonical_bytes(payload)
                envelope = {
                    "v": 1,
                    "kind": "job",
                    "job": payload,
                    "sig": await signer.sign(canonical),
                }
                out.append(
                    json.dumps(
                        {
                            "name": name,
                            # The frame EXACTLY as the gateway puts it on the
                            # wire: app/gateway/dispatch.py:65 and
                            # app/gateway/main.py:250 both json.dumps it.
                            "frame": json.dumps(envelope),
                            "canonical": base64.b64encode(canonical).decode("ascii"),
                            "public_key_pem": public_key_pem,
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                    )
                )
            return out

    path.write_text("\n".join(asyncio.run(build())) + "\n", encoding="utf-8")
    return path


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DIR
    for path in (write_corpus(out_dir), write_signed_jobs(out_dir)):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
