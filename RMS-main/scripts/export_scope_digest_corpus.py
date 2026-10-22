#!/usr/bin/env python3
"""Generate the golden corpus that pins the Go agent's `scope_digest`.

Run from repo root with the backend venv:
    backend/.venv/bin/python scripts/export_scope_digest_corpus.py

Writes `agent/internal/capabilities/netsweep/testdata/scope_digest.jsonl`, one
`{"name","why","targets","digest","payload"}` object per line. `digest` is
`app.modules.ipam.net.scope_digest(targets)`; `payload` is the exact JSON text
it hashes, base64-encoded, so a Go failure can show WHICH BYTES diverged rather
than only that two hex strings differ.

WHY THIS FILE EXISTS
--------------------
`docs/modules/ipam/spec.md` §6.4 and `app/modules/ipam/net.py` both say
`scope_digest` has "exactly one implementation": the dispatcher computes it over
the list it signs and the result handler recomputes it over the job's own signed
`params.targets`, so the two cannot drift.

That sentence cannot be literally true across two languages. The agent cannot
call the Python function, and it must recompute the digest before it emits a
single packet (§6.4's rejection is after the probes have already gone out). The
useful, narrower rule -- recorded in `docs/modules/agent/plan.md` C6 -- is that
the agent hashes `params.targets` EXACTLY AS RECEIVED: no sorting, no
`collapse_addresses`, no re-rendering of CIDRs, no normalisation. If it ever
derived its own list and hashed that, the digest could agree by luck and the
authorized-scope check would silently stop matching what was authorized.

This file is what makes "the same rule, in two languages" checkable.

THE DIVERGENCE THIS CORPUS EXISTS TO PIN
----------------------------------------
`scope_digest` uses `json.dumps(targets, separators=(",", ":"))` with
`ensure_ascii` at its DEFAULT of True. `app/jobs/canonical.py::canonical_bytes`
uses `ensure_ascii=False`. They are NOT the same encoder, and the agent's
canonical-JSON writer -- which was built for the signing contract -- produces
different bytes for any non-ASCII input.

A CIDR string is ASCII, so this never bites on well-formed params. It bites on
malformed ones, and it bites in the safe direction: two encoders that disagree
produce two digests that disagree, and the agent REFUSES the job before probing.
The `non_ascii_target` and `astral_target` cases below exist so the Go side is
pinned to Python's answer rather than to the one it would get by reusing the
signing encoder. That reuse is what `docs/modules/agent/plan.md` Task 13 step 1
told the builder to do; it would have been wrong.

`backend/tests/test_scope_digest_corpus.py` keeps this file current, so a change
to `scope_digest` goes red there rather than in the field.
"""

import base64
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.modules.ipam.net import scope_digest  # noqa: E402

OUT_DIR = REPO_ROOT / "agent" / "internal" / "capabilities" / "netsweep" / "testdata"

# (name, why, targets). `why` is written into the corpus so a Go subtest failure
# names the divergence it is about, not just an index.
CASES: list[tuple[str, str, list[str]]] = [
    (
        "empty",
        "an empty target set is never dispatched (§6.2), but the digest of one "
        "is still defined and an encoder that special-cases the empty list is a "
        "common bug",
        [],
    ),
    ("one_cidr", "the floor", ["10.20.30.0/24"]),
    (
        "many_cidrs",
        "the ordinary case: a canonical, sorted, minimal list as "
        "`canonical_targets` produces it",
        ["10.20.30.0/24", "10.20.31.0/24", "192.168.7.128/25"],
    ),
    (
        "unsorted_order",
        "THE POINT OF THE WHOLE FILE. `scope_digest` hashes the list AS GIVEN "
        "and deliberately does not sort. An agent that sorted before hashing "
        "would compute a different digest for this input, refuse a legitimate "
        "job -- or worse, agree by luck on the sorted cases and stop checking "
        "anything on the unsorted ones",
        ["192.168.7.128/25", "10.20.31.0/24", "10.20.30.0/24"],
    ),
    (
        "sorted_counterpart",
        "the same three entries in sorted order. Its digest MUST differ from "
        "`unsorted_order`; the Go test asserts that inequality directly",
        ["10.20.30.0/24", "10.20.31.0/24", "192.168.7.128/25"],
    ),
    (
        "duplicate_entries",
        "a repeated entry is not collapsed: the list is hashed as given",
        ["10.20.30.0/24", "10.20.30.0/24"],
    ),
    (
        "ipv6",
        "IPv6 is stored, documented and reconciled but never ENUMERATED (§0.5) "
        "-- `create_sweep` refuses an IPv6 subnet before signing. The digest is "
        "still defined over one, and the lowercase, compressed rendering "
        "`str(IPv6Network)` produces is what must be hashed byte-for-byte",
        ["2001:db8::/64", "2001:db8:1::/48"],
    ),
    (
        "mixed_families",
        "v4 and v6 in one list, v4 first, exactly as `_canonical` orders them",
        ["10.0.0.0/8", "2001:db8::/32"],
    ),
    (
        "single_addresses",
        "/32 and /128 host routes: the shortest and longest well-formed entries",
        ["10.4.7.9/32", "2001:db8::1/128"],
    ),
    (
        "comma_collision_pair",
        "['a','b'] vs ['a,b'] -- the collision the JSON encoding exists to "
        "stop, and precisely a scope-WIDENING confusion: a digest that "
        "concatenated with a separator would let one crafted entry stand in for "
        "two authorized ones",
        ["10.0.0.0/8", "10.1.0.0/16"],
    ),
    (
        "comma_collision_single",
        "the other half of the pair above, as ONE entry containing a comma. Its "
        "digest must differ from `comma_collision_pair`",
        ["10.0.0.0/8,10.1.0.0/16"],
    ),
    (
        "quote_and_backslash",
        "not a CIDR any server would send, and that is the point: the agent "
        "hashes what ARRIVED. A string escaper that differs from Python's would "
        "refuse a legitimate job or accept a mangled one",
        ['a"b', "x\\y"],
    ),
    (
        "control_characters",
        "Python escapes every C0 control, uses the short forms \\b \\t \\n \\f "
        "\\r, and lowercase \\u00xx for the rest -- and with ensure_ascii=True "
        "it ALSO escapes DEL (0x7f), which `canonical_bytes` does not",
        ["\x00\x08\x09\x0a\x0c\x0d\x1f\x7f"],
    ),
    (
        "non_ascii_target",
        "THE DIVERGENCE. `json.dumps` here is ensure_ascii=TRUE, so this is "
        "\\u00e9 -- while `canonical_bytes` (ensure_ascii=False) would emit raw "
        "UTF-8. Reusing the agent's canonical encoder for this digest, which "
        "the plan told the builder to do, produces a different hash",
        ["café", "日本語"],
    ),
    (
        "astral_target",
        "a non-BMP codepoint becomes a SURROGATE PAIR under ensure_ascii=True: "
        "\\ud83d\\ude00, lowercase hex, two escapes for one rune",
        ["\U0001f600"],
    ),
    (
        "u2028_u2029",
        "Go's encoding/json escapes U+2028/U+2029 even with SetEscapeHTML "
        "false; Python-with-ensure_ascii escapes them too, but as part of "
        "escaping everything above 0x7e rather than as a special case",
        ["  "],
    ),
    (
        "html_characters",
        "encoding/json escapes < > & to \\u003c \\u003e \\u0026 unless "
        "SetEscapeHTML(false); Python escapes none of them",
        ["<script>&</script>"],
    ),
    (
        "empty_string_entry",
        "an empty entry is below the schema's minLength 7 and can never be "
        "signed -- but the digest of a mangled list is exactly what the agent "
        "computes when deciding to REFUSE, so it has to be right",
        [""],
    ),
    (
        "max_length_entry",
        "43 characters is the schema's maxLength for one target: a full IPv6 "
        "address with a prefix length",
        ["2001:0db8:85a3:0000:0000:8a2e:0370:7334/128"],
    ),
]


def write_corpus(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "scope_digest.jsonl"
    lines = []
    for name, why, targets in CASES:
        payload = json.dumps(targets, separators=(",", ":")).encode()
        digest = scope_digest(targets)
        # Belt and braces: the exporter recomputes the hash from the payload it
        # records, so a corpus line can never claim a digest that does not
        # follow from the bytes beside it.
        assert digest == hashlib.sha256(payload).hexdigest(), name
        lines.append(
            json.dumps(
                {
                    "name": name,
                    "why": why,
                    "targets": targets,
                    "payload": base64.b64encode(payload).decode(),
                    "digest": digest,
                },
                # ensure_ascii=TRUE for the corpus FILE itself, which is a
                # different decision from the ensure_ascii=True inside
                # `scope_digest` and is made for a different reason. The
                # `u2028_u2029` case carries raw U+2028 and U+2029, and
                # `str.splitlines()` in Python SPLITS ON THOSE -- so a corpus
                # written as raw UTF-8 is a JSONL file whose reader sees a
                # truncated line and a decode error. Observed, not predicted:
                # the keeper test failed on exactly that line.
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    path = write_corpus(OUT_DIR)
    print(f"wrote {path} ({len(CASES)} cases)")


if __name__ == "__main__":
    main()
