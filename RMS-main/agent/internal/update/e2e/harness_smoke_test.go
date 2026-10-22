//go:build e2e

package e2e

import (
	"crypto/ed25519"
	"crypto/rand"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

// TestHarnessSmoke runs without root and without a service: A as a plain
// process against the harness gateway. It proves the harness itself (mTLS,
// headers, job signing) and the pre-download checks on a real binary with a
// compiled-in release key, before the root scenarios spend their minutes.
func TestHarnessSmoke(t *testing.T) {
	binDir := os.Getenv("E2E_BIN_DIR")
	if binDir == "" {
		t.Fatal("E2E_BIN_DIR is unset; run internal/update/e2e/build.sh first")
	}
	relKey := loadReleaseKey(t, filepath.Join(binDir, "release-key.pem"))
	jobPub, jobKey, _ := ed25519.GenerateKey(rand.Reader)
	p := newPKI(t)
	gw := startGateway(t, p, jobKey)
	art := startArtifacts(t, p, binDir)
	data := t.TempDir()
	seedDataDir(t, data, p, gw.url, jobPub)

	cmd := exec.Command(filepath.Join(binDir, "A"+exe()))
	cmd.Env = append(os.Environ(), "ITS_AGENT_DATA_DIR="+data)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })

	c := gw.waitVersion(versionA, time.Minute)
	if c.caps == "" {
		t.Fatal("no capabilities header")
	}
	r := &rig{t: t, gw: gw, art: art, relKey: relKey}

	expect := func(params map[string]any, state, key, want string) {
		t.Helper()
		res := gw.waitResult(gw.send("agent.update", params), time.Minute)
		if res.State != state || res.Result[key] != want {
			t.Fatalf("result = %+v, want %s %s=%s", res, state, key, want)
		}
	}
	unsigned := r.params("B", versionB, func(p map[string]any) { p["release_signature"] = nil })
	expect(unsigned, "refused", "reason", "signature_required")
	relabelled := r.params("B", versionB, func(p map[string]any) { p["target_version"] = "0.0.1-e2e.9" })
	expect(relabelled, "refused", "reason", "signature_invalid")
	expect(r.params("A", versionA, nil), "succeeded", "outcome", "already_current")
	// Every check before the service one passes; a plain process is refused.
	expect(r.params("B", versionB, nil), "refused", "reason", "not_running_as_service")
}
