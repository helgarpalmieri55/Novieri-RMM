package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/update"
)

// The contract the self-update helper relies on, against a real binary: it
// runs `--version` on prev/ and `--update-selftest` on the candidate beside
// the target (spec §4.2 step 6, §5.1 step 3).
func TestTheBuiltBinaryHonoursTheSelfTestContract(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the agent")
	}
	dir := t.TempDir()
	build := func(name, version string, tags ...string) string {
		out := filepath.Join(dir, name)
		if runtime.GOOS == "windows" {
			out += ".exe"
		}
		args := []string{"build", "-o", out, "-ldflags", "-X main.version=" + version}
		if len(tags) > 0 {
			args = append(args, "-tags", tags[0])
		}
		cmd := exec.Command("go", append(args, ".")...)
		cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
		if b, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("go build: %v\n%s", err, b)
		}
		return out
	}
	good := build("good", "1.2.3")
	bad := build("bad", "1.2.4", "e2e_badselftest")
	dataDir := seedIdentity(t)
	ctx := context.Background()

	if err := update.CheckRunsAndReports(ctx, good, "1.2.3"); err != nil {
		t.Fatalf("--version: %v", err)
	}
	if err := update.RunSelfTest(ctx, good, dataDir, "1.2.3"); err != nil {
		t.Fatalf("--update-selftest: %v", err)
	}
	if err := update.RunSelfTest(ctx, good, dataDir, "1.2.4"); err == nil {
		t.Fatal("a self-test reporting another version passed")
	}
	if err := update.RunSelfTest(ctx, bad, dataDir, "1.2.4"); err == nil {
		t.Fatal("an e2e_badselftest build passed its self-test")
	}
	if err := update.RunSelfTest(ctx, good, t.TempDir(), "1.2.3"); err == nil {
		t.Fatal("a self-test with no identity passed")
	}
}

// seedIdentity writes an enrolled data directory: identity, key and pins.
func seedIdentity(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "device"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		IsCA:         true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	kder, _ := x509.MarshalPKCS8PrivateKey(key)
	jobPub, _, _ := ed25519.GenerateKey(rand.Reader)
	pubDER, _ := x509.MarshalPKIXPublicKey(jobPub)
	write := func(name string, v any) {
		b, ok := v.([]byte)
		if !ok {
			b, _ = json.Marshal(v)
		}
		if err := os.WriteFile(filepath.Join(dir, name), b, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("agent.key", pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: kder}))
	write("pins.json", map[string]string{
		"ca_bundle_pem":              string(certPEM),
		"job_signing_public_key_pem": string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: pubDER})),
	})
	write("identity.json", map[string]string{
		"device_id": "device", "gateway_url": "wss://gateway.invalid/agent", "cert_pem": string(certPEM),
	})
	return dir
}
