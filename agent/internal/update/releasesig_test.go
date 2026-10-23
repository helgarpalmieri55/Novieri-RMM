package update

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"strings"
	"testing"
)

func throwawayKey(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return pub, priv
}

const testSHA = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func TestReleaseMessageBindsEveryField(t *testing.T) {
	base := ReleaseMessage("1.2.3", "linux", "amd64", testSHA)
	for name, other := range map[string][]byte{
		"version": ReleaseMessage("1.2.4", "linux", "amd64", testSHA),
		"os":      ReleaseMessage("1.2.3", "darwin", "amd64", testSHA),
		"arch":    ReleaseMessage("1.2.3", "linux", "arm64", testSHA),
		"sha":     ReleaseMessage("1.2.3", "linux", "amd64", strings.Repeat("0", 64)),
	} {
		if string(other) == string(base) {
			t.Errorf("changing %s does not change the signed message", name)
		}
	}
	if string(ReleaseMessage("v1.2.3", "linux", "amd64", strings.ToUpper(testSHA))) != string(base) {
		t.Error("the leading v and hex case must not change the signed message")
	}
}

func TestReleaseSignatureWithNoPinIsNotRequired(t *testing.T) {
	got, token := CheckReleaseSignature(nil, "1.2.3", "linux", "amd64", testSHA, "")
	if got != SignatureNotRequired || token != "" {
		t.Fatalf("got %q/%q", got, token)
	}
}

func TestReleaseSignatureWithAPin(t *testing.T) {
	pub, priv := throwawayKey(t)
	good := base64.StdEncoding.EncodeToString(ed25519.Sign(priv, ReleaseMessage("1.2.3", "linux", "amd64", testSHA)))
	_, otherPriv := throwawayKey(t)
	foreign := base64.StdEncoding.EncodeToString(ed25519.Sign(otherPriv, ReleaseMessage("1.2.3", "linux", "amd64", testSHA)))

	cases := []struct {
		name, version, sig string
		want               SignatureStatus
		token              string
	}{
		{"valid", "1.2.3", good, SignatureVerified, ""},
		{"absent", "1.2.3", "", "", ReasonSignatureRequired},
		{"not base64", "1.2.3", "%%%", "", ReasonSignatureInvalid},
		{"foreign key", "1.2.3", foreign, "", ReasonSignatureInvalid},
		{"relabelled version", "1.2.4", good, "", ReasonSignatureInvalid},
	}
	for _, c := range cases {
		got, token := CheckReleaseSignature(pub, c.version, "linux", "amd64", testSHA, c.sig)
		if got != c.want || token != c.token {
			t.Errorf("%s: got %q/%q, want %q/%q", c.name, got, token, c.want, c.token)
		}
	}
}

func TestParseReleaseKey(t *testing.T) {
	pub, _ := throwawayKey(t)
	if k, err := ParseReleaseKey(""); err != nil || k != nil {
		t.Fatalf("empty pin: %v %v", k, err)
	}
	k, err := ParseReleaseKey(base64.StdEncoding.EncodeToString(pub))
	if err != nil || !k.Equal(pub) {
		t.Fatalf("round trip: %v", err)
	}
	if _, err := ParseReleaseKey("AAAA"); err == nil {
		t.Fatal("a short key must not parse")
	}
}
