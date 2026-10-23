package update

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"strings"
)

// releaseSigningKey is the base64 Ed25519 public half of the offline
// release-signing key. The release workflow sets it with
//
//	-ldflags "-X github.com/toeshee/it-suite/agent/internal/update.releaseSigningKey=<b64>"
//
// It is compiled in, never taken from job params: a key the control plane
// supplied would be the control plane vouching for itself.
var releaseSigningKey = ""

// SignatureStatus is reported in AgentUpdateResult.signature.
type SignatureStatus string

const (
	SignatureVerified    SignatureStatus = "verified"
	SignatureNotRequired SignatureStatus = "not_required"
)

// ReleaseKey returns this build's pinned release key, nil when none is pinned.
func ReleaseKey() (ed25519.PublicKey, error) { return ParseReleaseKey(releaseSigningKey) }

// ParseReleaseKey parses a base64 Ed25519 public key; "" means no pin.
func ParseReleaseKey(b64 string) (ed25519.PublicKey, error) {
	b64 = strings.TrimSpace(b64)
	if b64 == "" {
		return nil, nil
	}
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, errors.New("the compiled-in release signing key is not a base64 Ed25519 public key")
	}
	return ed25519.PublicKey(raw), nil
}

// ReleaseMessage is what the release workflow signs: the version, platform and
// digest together, so a signed binary cannot be relabelled as another version
// or platform.
func ReleaseMessage(version, goos, goarch, sha256hex string) []byte {
	if v, err := ParseVersion(version); err == nil {
		version = v.String()
	}
	return []byte("its-agent-release/v1\n" + version + "\n" + goos + "\n" + goarch + "\n" +
		strings.ToLower(sha256hex) + "\n")
}

// CheckReleaseSignature returns the status, or a refusal token.
func CheckReleaseSignature(pub ed25519.PublicKey, version, goos, goarch, sha256hex, sigB64 string) (SignatureStatus, string) {
	if pub == nil {
		return SignatureNotRequired, ""
	}
	if strings.TrimSpace(sigB64) == "" {
		return "", ReasonSignatureRequired
	}
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(sigB64))
	if err != nil || len(sig) != ed25519.SignatureSize {
		return "", ReasonSignatureInvalid
	}
	if !ed25519.Verify(pub, ReleaseMessage(version, goos, goarch, sha256hex), sig) {
		return "", ReasonSignatureInvalid
	}
	return SignatureVerified, ""
}
