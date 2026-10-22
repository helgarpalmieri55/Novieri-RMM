package netsweep

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"os"
	"sort"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/canonical"
)

// scope_digest is a CROSS-LANGUAGE contract with no versioning story: the
// server computes it at dispatch and compares the agent's recomputation against
// what it signed. A near-miss is not a garbled string -- it is every sweep in
// the fleet refused, or an authorized-scope check that agrees on the cases
// anyone tests and stops matching on the ones they do not.
//
// So the Go implementation is pinned to testdata/scope_digest.jsonl, produced
// by calling the Python function (scripts/export_scope_digest_corpus.py), and
// backend/tests/test_scope_digest_corpus.py keeps that file current.

type corpusCase struct {
	Name    string   `json:"name"`
	Why     string   `json:"why"`
	Targets []string `json:"targets"`
	Payload string   `json:"payload"`
	Digest  string   `json:"digest"`
}

func loadCorpus(t *testing.T) []corpusCase {
	t.Helper()
	f, err := os.Open("testdata/scope_digest.jsonl")
	if err != nil {
		t.Fatalf("the golden corpus is missing: %v", err)
	}
	defer f.Close()
	var out []corpusCase
	s := bufio.NewScanner(f)
	s.Buffer(make([]byte, 0, 64*1024), 1<<20)
	for s.Scan() {
		if len(s.Bytes()) == 0 {
			continue
		}
		var c corpusCase
		if err := json.Unmarshal(s.Bytes(), &c); err != nil {
			t.Fatalf("corpus line: %v", err)
		}
		out = append(out, c)
	}
	if err := s.Err(); err != nil {
		t.Fatal(err)
	}
	if len(out) < 15 {
		t.Fatalf("the corpus holds %d cases; it has shrunk and the divergences it pinned are "+
			"no longer tested", len(out))
	}
	return out
}

func TestDigestMatchesThePythonImplementationOnEveryCorpusCase(t *testing.T) {
	// FALSIFYING OBSERVATION: a digest that differs from Python's, with the
	// exact bytes each side hashed printed side by side -- which is why the
	// corpus carries the payload and not only the hash.
	for _, c := range loadCorpus(t) {
		t.Run(c.Name, func(t *testing.T) {
			wantPayload, err := base64.StdEncoding.DecodeString(c.Payload)
			if err != nil {
				t.Fatal(err)
			}
			gotPayload := encodeStringList(c.Targets)
			if string(gotPayload) != string(wantPayload) {
				t.Fatalf("%s\n  go:     %q\n  python: %q\n  why this case exists: %s",
					c.Name, gotPayload, wantPayload, c.Why)
			}
			if got := Digest(c.Targets); got != c.Digest {
				t.Fatalf("%s: digest %s, python computes %s (%s)", c.Name, got, c.Digest, c.Why)
			}
		})
	}
}

func TestTheDigestIsOfTheListAsGivenAndNotOfASortedCopy(t *testing.T) {
	// C6, and the whole reason this function may not re-derive its input. The
	// Python function deliberately does not sort: "a caller that skips
	// `canonical_targets` gets a different digest rather than a quietly equal
	// one".
	//
	// FALSIFYING OBSERVATION: the two digests being equal. An agent that sorted
	// before hashing would agree with the server on every canonical list --
	// which is all of them, in practice -- and would therefore pass every test
	// anyone writes while having stopped checking the thing that matters.
	unsorted := []string{"192.168.7.128/25", "10.20.31.0/24", "10.20.30.0/24"}
	sorted := append([]string(nil), unsorted...)
	sort.Strings(sorted)

	if Digest(unsorted) == Digest(sorted) {
		t.Fatalf("Digest(%v) == Digest(%v); the list is hashed AS GIVEN and an agent that "+
			"normalises it can agree with the server by luck", unsorted, sorted)
	}
}

func TestTheJSONEncodingIsWhatStopsTheCommaCollision(t *testing.T) {
	// net.py: "The JSON encoding is what stops `["a","b"]` colliding with
	// `["a,b"]`, which is precisely a scope-widening confusion." A digest built
	// by joining with a separator would let ONE crafted entry stand in for TWO
	// authorized ones.
	pair := []string{"10.0.0.0/8", "10.1.0.0/16"}
	joined := []string{"10.0.0.0/8,10.1.0.0/16"}
	if Digest(pair) == Digest(joined) {
		t.Fatalf("Digest(%v) == Digest(%v)", pair, joined)
	}
}

func TestTheDigestIsNotTheSigningEncoder(t *testing.T) {
	// The plan (Task 13 step 1) says to reuse internal/canonical's string
	// escaping here. This test is why that is wrong, and it is written as an
	// assertion rather than as a comment so the substitution cannot be made
	// later by someone reading the plan.
	//
	// `canonical_bytes` is ensure_ascii=FALSE; `scope_digest` uses json.dumps at
	// its default, ensure_ascii=TRUE. They agree on ASCII -- which every
	// well-formed CIDR is -- and diverge on everything else. "Safe by accident"
	// is not a property.
	const target = "café"
	viaCanonical, err := canonical.Bytes([]any{target})
	if err != nil {
		t.Fatal(err)
	}
	viaDigest := encodeStringList([]string{target})
	if string(viaCanonical) == string(viaDigest) {
		t.Fatalf("internal/canonical and the scope_digest encoder produced the same bytes for "+
			"%q (%q); one of them has changed and the corpus is now pinning the wrong function",
			target, viaDigest)
	}
	// And each is the one its own Python function produces.
	if got, want := string(viaDigest), `["caf\u00e9"]`; got != want {
		t.Fatalf("encodeStringList(%q) = %s, want %s (json.dumps, ensure_ascii=True)",
			target, got, want)
	}
	if got, want := string(viaCanonical), `["café"]`; got != want {
		t.Fatalf("canonical.Bytes(%q) = %s, want %s (canonical_bytes, ensure_ascii=False)",
			target, got, want)
	}
}

func TestDELIsEscapedHereAndNotByTheSigningEncoder(t *testing.T) {
	// The rule a reader will not expect, and the one most likely to be "fixed"
	// by someone porting the escaper from internal/canonical. Verified against
	// CPython rather than recalled: json.dumps(["a\x7fb"]) is ["a\\u007fb"].
	if got, want := string(encodeStringList([]string{"a\x7fb"})), `["a\u007fb"]`; got != want {
		t.Fatalf("encodeStringList = %s, want %s", got, want)
	}
	viaCanonical, err := canonical.Bytes([]any{"a\x7fb"})
	if err != nil {
		t.Fatal(err)
	}
	if got, want := string(viaCanonical), "[\"a\x7fb\"]"; got != want {
		t.Fatalf("canonical.Bytes = %q, want %q -- ensure_ascii=False leaves DEL alone", got, want)
	}
}
