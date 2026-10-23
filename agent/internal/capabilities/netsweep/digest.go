package netsweep

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"unicode/utf8"
)

// Digest is `scope_digest` (docs/modules/ipam/spec.md §5.2, §6.2, §6.4), on the
// endpoint.
//
// # "Exactly one implementation", and what it can mean across two languages
//
// app/modules/ipam/net.py states the rule: "**The only implementation.** The
// dispatcher computes it over the list it signs; the result handler recomputes
// it over the job's own signed `params.targets` and compares. A second
// implementation anywhere would let both sides agree while checking nothing."
//
// The agent cannot call that function, and it must have the answer BEFORE it
// emits a packet -- §6.4's rejection happens after the probes have already gone
// out. So the narrower, literal rule is the one the agent keeps:
//
//	IT HASHES params.targets EXACTLY AS RECEIVED.
//
// No sorting. No `collapse_addresses`. No re-rendering of a CIDR through a
// parser. No normalisation of `10.0.0.010` to `10.0.0.10`, no lowercasing of an
// IPv6 literal, no dropping of a duplicate. If this function ever derived its
// own list and hashed that, the digest could agree BY LUCK on the well-formed
// cases and stop matching what was authorized on every other one -- silently,
// with nothing failing. That is the failure mode the "one implementation"
// sentence exists to prevent, and re-deriving the input is how you reintroduce
// it while appearing to obey the rule.
//
// # Why this is NOT internal/canonical
//
// docs/modules/agent/plan.md Task 13 step 1 says to "reuse `canonical` for the
// string escaping (a CIDR string is ASCII, but the encoder is already correct
// and a second string escaper is a second thing to get wrong)".
//
// THAT IS WRONG, and it was checked rather than assumed. The two Python
// functions are different encoders:
//
//	app/jobs/canonical.py   json.dumps(..., ensure_ascii=False)
//	app/modules/ipam/net.py json.dumps(targets, separators=(",",":"))   <- default ensure_ascii=TRUE
//
// So `canonical_bytes` emits "café" as raw UTF-8 and `scope_digest` emits
// "café". internal/canonical implements the first one, exactly, and is
// pinned to a corpus of it. Using it here would produce a digest Python never
// computes for any non-ASCII target.
//
// A well-formed target list is ASCII and the two agree, which is precisely why
// this would have shipped and never been noticed until a mangled or malicious
// params object arrived. testdata/scope_digest.jsonl carries `non_ascii_target`
// and `astral_target` for that reason. The direction of the disagreement is
// safe either way -- two digests that differ mean the agent REFUSES before
// probing -- but "safe by accident" is not a property, and a second agent
// reading the plan would have made the same substitution.
func Digest(targets []string) string {
	sum := sha256.Sum256(encodeStringList(targets))
	return hex.EncodeToString(sum[:])
}

// encodeStringList is `json.dumps(targets, separators=(",", ":"))` for a list of
// strings, byte for byte.
//
// CPython's `json.encoder.py_encode_basestring_ascii`, restated: escape `"` and
// `\`; use the short forms \b \t \n \f \r; everything else below 0x20 and
// EVERYTHING at or above 0x7f becomes \uXXXX with LOWERCASE hex; a codepoint
// above U+FFFF becomes a surrogate PAIR.
//
// The 0x7f rule is the one a reader will not expect, because
// internal/canonical (ensure_ascii=False) leaves DEL alone. Verified against
// CPython rather than recalled: json.dumps(["a\x7fb"]) is `["ab"]`.
func encodeStringList(items []string) []byte {
	var buf bytes.Buffer
	buf.WriteByte('[')
	for i, s := range items {
		if i > 0 {
			buf.WriteByte(',')
		}
		writeASCIIString(&buf, s)
	}
	buf.WriteByte(']')
	return buf.Bytes()
}

const hexDigits = "0123456789abcdef"

func writeASCIIString(buf *bytes.Buffer, s string) {
	buf.WriteByte('"')
	for i := 0; i < len(s); {
		c := s[i]
		if c < utf8.RuneSelf {
			i++
			switch {
			case c == '"':
				buf.WriteString(`\"`)
			case c == '\\':
				buf.WriteString(`\\`)
			case c == '\b':
				buf.WriteString(`\b`)
			case c == '\f':
				buf.WriteString(`\f`)
			case c == '\n':
				buf.WriteString(`\n`)
			case c == '\r':
				buf.WriteString(`\r`)
			case c == '\t':
				buf.WriteString(`\t`)
			case c >= 0x20 && c < 0x7f:
				buf.WriteByte(c)
			default:
				// Everything else below 0x20, and DEL. Python writes
				// '\\u%04x'.
				writeU(buf, rune(c))
			}
			continue
		}
		r, size := utf8.DecodeRuneInString(s[i:])
		i += size
		if r == utf8.RuneError && size == 1 {
			// Invalid UTF-8. Python would never have produced this: it decoded
			// the JSON that carried the target, so its string is valid text.
			// Emitting U+FFFD is what Go's own decoder already did to these
			// bytes, so this reproduces the value the agent actually holds
			// rather than inventing a third answer. The digest will not match
			// and the job is refused -- which is the correct outcome for params
			// that arrived mangled.
			r = utf8.RuneError
		}
		if r > 0xFFFF {
			// Surrogate pair, exactly as CPython emits it.
			r -= 0x10000
			writeU(buf, 0xD800+(r>>10))
			writeU(buf, 0xDC00+(r&0x3FF))
			continue
		}
		writeU(buf, r)
	}
	buf.WriteByte('"')
}

func writeU(buf *bytes.Buffer, r rune) {
	buf.WriteString(`\u`)
	buf.WriteByte(hexDigits[(r>>12)&0xF])
	buf.WriteByte(hexDigits[(r>>8)&0xF])
	buf.WriteByte(hexDigits[(r>>4)&0xF])
	buf.WriteByte(hexDigits[r&0xF])
}
