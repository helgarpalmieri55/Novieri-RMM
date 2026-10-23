package update

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// releaseVector is one entry of docs/kernel/wire-protocol/testdata/
// release-signature.json: the file's top level, or one of its more_vectors.
type releaseVector struct {
	PublicKey string `json:"public_key"`
	Version   string `json:"version"`
	OS        string `json:"os"`
	Arch      string `json:"arch"`
	SHA256    string `json:"sha256"`
	Signature string `json:"signature"`
}

func readReleaseVectors(t *testing.T) (releaseVector, []releaseVector) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "docs", "kernel", "wire-protocol", "testdata", "release-signature.json"))
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		releaseVector
		More []releaseVector `json:"more_vectors"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	return file.releaseVector, file.More
}

// One offline release signature, checked by BOTH verifiers: this one, and the
// control plane's (backend tests/test_release_signature_vector.py), which
// checks the agent the Group Policy MSI wraps (GPO MSI spec 2026-09-30 §2.2).
// A change to ReleaseMessage fails here or there, instead of every MSI
// download failing closed in production.
func TestReleaseSignatureVector(t *testing.T) {
	v, _ := readReleaseVectors(t)
	pub, err := ParseReleaseKey(v.PublicKey)
	if err != nil || pub == nil {
		t.Fatalf("the vector's key: %v", err)
	}
	if status, reason := CheckReleaseSignature(pub, v.Version, v.OS, v.Arch, v.SHA256, v.Signature); status != SignatureVerified {
		t.Fatalf("the shared vector does not verify: %q", reason)
	}
	if _, reason := CheckReleaseSignature(pub, v.Version, v.OS, "arm64", v.SHA256, v.Signature); reason == "" {
		t.Fatal("the vector verified for another architecture")
	}
}

// The second vector carries a leading v and upper-case hex, exactly as given:
// both verifiers canonicalise before checking, and must agree that it
// verifies. The Python twin is test_every_further_vector_verifies_as_given.
func TestReleaseSignatureVectorWithVPrefixAndUpperCaseHex(t *testing.T) {
	_, more := readReleaseVectors(t)
	if len(more) == 0 {
		t.Fatal("release-signature.json has no more_vectors")
	}
	sawV, sawUpper := false, false
	for i, v := range more {
		sawV = sawV || strings.HasPrefix(v.Version, "v")
		sawUpper = sawUpper || (v.SHA256 != strings.ToLower(v.SHA256))
		pub, err := ParseReleaseKey(v.PublicKey)
		if err != nil || pub == nil {
			t.Fatalf("more_vectors[%d]'s key: %v", i, err)
		}
		if status, reason := CheckReleaseSignature(pub, v.Version, v.OS, v.Arch, v.SHA256, v.Signature); status != SignatureVerified {
			t.Fatalf("more_vectors[%d] does not verify as given: %q", i, reason)
		}
		if _, reason := CheckReleaseSignature(pub, v.Version, v.OS, v.Arch, strings.Repeat("0", 64), v.Signature); reason == "" {
			t.Fatalf("more_vectors[%d] verified for another digest", i)
		}
	}
	if !sawV || !sawUpper {
		t.Fatalf("more_vectors must carry a v-prefixed version (%v) and upper-case hex (%v)", sawV, sawUpper)
	}
}
