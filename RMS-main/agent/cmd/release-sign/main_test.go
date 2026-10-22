package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/update"
)

// A throwaway key, generated per test. No private key is ever committed.
func throwawayPEM(t *testing.T) (string, ed25519.PublicKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})), pub
}

func TestSignProducesSignaturesTheAgentAccepts(t *testing.T) {
	keyPEM, pub := throwawayPEM(t)
	dir := t.TempDir()
	bin := filepath.Join(dir, "its-agent-linux-amd64")
	if err := os.WriteFile(bin, []byte("binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	win := filepath.Join(dir, "its-agent-windows-arm64.exe")
	if err := os.WriteFile(win, []byte("pe binary"), 0o755); err != nil {
		t.Fatal(err)
	}

	var out bytes.Buffer
	if err := run([]string{"sign", "-version", "v1.4.0", bin, win}, keyPEM, &out); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct{ path, goos, goarch string }{
		{bin, "linux", "amd64"}, {win, "windows", "arm64"},
	} {
		sig, err := os.ReadFile(c.path + ".sig")
		if err != nil {
			t.Fatal(err)
		}
		sha, _ := update.FileSHA256(c.path)
		status, token := update.CheckReleaseSignature(pub, "1.4.0", c.goos, c.goarch, sha,
			strings.TrimSpace(string(sig)))
		if status != update.SignatureVerified || token != "" {
			t.Fatalf("%s: %q %q", c.path, status, token)
		}
	}
	if strings.Contains(out.String(), "PRIVATE KEY") {
		t.Fatal("the private key reached the output")
	}
}

func TestPubkeyPrintsTheCompiledInForm(t *testing.T) {
	keyPEM, pub := throwawayPEM(t)
	var out bytes.Buffer
	if err := run([]string{"pubkey"}, keyPEM, &out); err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(out.String()) != base64.StdEncoding.EncodeToString(pub) {
		t.Fatalf("pubkey = %q", out.String())
	}
}

func TestGenkeyProducesAKeyTheOtherCommandsAccept(t *testing.T) {
	var key bytes.Buffer
	if err := run([]string{"genkey"}, "", &key); err != nil {
		t.Fatal(err)
	}
	var pub bytes.Buffer
	if err := run([]string{"pubkey"}, key.String(), &pub); err != nil {
		t.Fatalf("pubkey rejected a generated key: %v", err)
	}
	if _, err := update.ParseReleaseKey(strings.TrimSpace(pub.String())); err != nil {
		t.Fatal(err)
	}
}

func TestMissingOrWrongKeyFails(t *testing.T) {
	if err := run([]string{"pubkey"}, "", &bytes.Buffer{}); err == nil {
		t.Fatal("no key accepted")
	}
	if err := run([]string{"pubkey"}, "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
		&bytes.Buffer{}); err == nil {
		t.Fatal("garbage key accepted")
	}
}

func TestSignRefusesAFileItCannotPlace(t *testing.T) {
	keyPEM, _ := throwawayPEM(t)
	bad := filepath.Join(t.TempDir(), "agent.bin")
	_ = os.WriteFile(bad, []byte("x"), 0o644)
	if err := run([]string{"sign", "-version", "1.0.0", bad}, keyPEM, &bytes.Buffer{}); err == nil {
		t.Fatal("signed a file whose platform is not in its name")
	}
	if err := run([]string{"sign", "-version", "dev", bad}, keyPEM, &bytes.Buffer{}); err == nil {
		t.Fatal("signed a non-semver version")
	}
}
