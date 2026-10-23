//go:build e2e

package e2e

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/pem"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/update"
)

// TestSelfUpdateEndToEnd runs spec-self-update.md §11's six scenarios against
// a real service, and scenario 7's socket sampler throughout.
func TestSelfUpdateEndToEnd(t *testing.T) {
	binDir := os.Getenv("E2E_BIN_DIR")
	if binDir == "" {
		t.Fatal("E2E_BIN_DIR is unset; run internal/update/e2e/build.sh first")
	}
	repoRoot, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if v := os.Getenv("E2E_REPO_ROOT"); v != "" {
		repoRoot = v
	}

	relKey := loadReleaseKey(t, filepath.Join(binDir, "release-key.pem"))
	jobPub, jobKey, _ := ed25519.GenerateKey(rand.Reader)
	p := newPKI(t)
	plat := newPlatform()

	caPath := filepath.Join(t.TempDir(), "e2e-ca.pem")
	if err := os.WriteFile(caPath, p.caPEM, 0o644); err != nil {
		t.Fatal(err)
	}
	plat.trustCA(t, caPath)
	t.Cleanup(func() { plat.untrustCA(t) })

	gw := startGateway(t, p, jobKey)
	art := startArtifacts(t, p, binDir)
	seedDataDir(t, plat.dataDir(), p, gw.url, jobPub)

	r := &rig{
		t: t, p: plat, gw: gw, art: art, relKey: relKey, binDir: binDir, repoRoot: repoRoot,
		layout: update.NewLayout(plat.dataDir()),
	}
	every := 3 * time.Second
	if strings.EqualFold(os.Getenv("RUNNER_OS"), "windows") {
		every = 10 * time.Second
	}
	r.sampler = startSampler(t, plat, every)
	t.Cleanup(func() { plat.uninstall(t, repoRoot) })

	// A as a real service, through the shipped installer.
	plat.install(t, repoRoot, filepath.Join(binDir, "A"+exe()))
	installed := plat.configuredBinary(t)
	gw.waitVersion(versionA, 3*time.Minute)
	if caps := gw.live().caps; !strings.Contains(caps, "agent.update") {
		t.Fatalf("A advertised %q", caps)
	}

	step := func(name string, fn func()) {
		t.Logf("=== scenario %s", name)
		start := time.Now()
		fn()
		t.Logf("=== scenario %s passed in %s", name, time.Since(start).Round(time.Second))
	}

	step("1: A to B succeeds", func() {
		id := gw.send("agent.update", r.params("B", versionB, nil))
		r.expectDeferred(gw.waitResult(id, 8*time.Minute), "succeeded", "updated", "")
		gw.waitVersion(versionB, time.Minute)
		if got := plat.configuredBinary(t); got != installed {
			t.Fatalf("the service definition changed: %q -> %q", installed, got)
		}
		if r.targetSHA() != art.sha["B"] {
			t.Fatal("the target does not hold B")
		}
		if s, _ := update.FileSHA256(r.layout.PrevBinary()); s != art.sha["A"] {
			t.Fatal("prev/ does not hold A")
		}
		r.waitHelperGone(time.Minute)
	})

	step("2: B to C rolls back for want of confirmation", func() {
		id := gw.send("agent.update", r.params("C", versionC, nil))
		r.expectDeferred(gw.waitResult(id, 10*time.Minute), "failed", "rolled_back", "no_confirmation")
		gw.waitVersion(versionB, 2*time.Minute)
		if r.targetSHA() != art.sha["B"] {
			t.Fatal("B was not restored")
		}
		r.waitHelperGone(time.Minute)
	})

	step("3: B to D aborts on its self-test", func() {
		id := gw.send("agent.update", r.params("D", versionD, nil))
		r.expectDeferred(gw.waitResult(id, 6*time.Minute), "failed", "aborted", "selftest_failed")
		gw.waitVersion(versionB, 2*time.Minute)
		if r.targetSHA() != art.sha["B"] {
			t.Fatal("B is no longer the target")
		}
		r.waitHelperGone(time.Minute)
	})

	step("4: a tampered artefact changes nothing", func() {
		before := gw.connCount()
		live := gw.live()
		id := gw.send("agent.update", r.params("tampered", versionD, nil))
		res := gw.waitResult(id, 3*time.Minute)
		if res.State != "failed" || res.Result["error"] != "hash_mismatch" {
			t.Fatalf("result = %+v", res)
		}
		time.Sleep(5 * time.Second)
		if gw.connCount() != before || gw.live() != live {
			t.Fatal("the service was restarted for a tampered artefact")
		}
		entries, _ := os.ReadDir(filepath.Dir(plat.target()))
		for _, e := range entries {
			if strings.HasPrefix(e.Name(), ".its-agent.new") {
				t.Fatalf("%s appeared in the target directory", e.Name())
			}
		}
		if _, err := os.Stat(r.layout.StagedBinary()); err == nil {
			t.Fatal("a tampered artefact was staged")
		}
		if r.targetSHA() != art.sha["B"] {
			t.Fatal("the target changed")
		}
	})

	step("5: B to A needs the rollback flag", func() {
		id := gw.send("agent.update", r.params("A", versionA, nil))
		res := gw.waitResult(id, 2*time.Minute)
		if res.State != "refused" || res.Result["reason"] != update.ReasonDowngradeNotPermitted {
			t.Fatalf("result = %+v", res)
		}
		id = gw.send("agent.update", r.params("A", versionA, func(p map[string]any) { p["rollback"] = true }))
		r.expectDeferred(gw.waitResult(id, 8*time.Minute), "succeeded", "updated", "")
		gw.waitVersion(versionA, time.Minute)
		r.waitHelperGone(time.Minute)
	})

	step("6: with the helper killed, the new binary's own deadline rolls back", func() {
		id := gw.send("agent.update", r.params("C", versionC, nil))
		waitPhase(t, r.layout, update.PhaseTrial, 5*time.Minute)
		time.Sleep(5 * time.Second)
		plat.killHelper(t)
		t.Log("helper killed during the trial")
		r.expectDeferred(gw.waitResult(id, 10*time.Minute), "failed", "rolled_back", "no_confirmation")
		gw.waitVersion(versionA, 2*time.Minute)
		if r.targetSHA() != art.sha["A"] {
			t.Fatal("A was not restored")
		}
		r.waitHelperGone(time.Minute)
	})

	step("7: no its-agent process ever listened", func() {
		samples, violations := r.sampler.finish()
		if samples < 10 {
			t.Fatalf("only %d socket samples were taken", samples)
		}
		if len(violations) > 0 {
			t.Fatalf("listening sockets owned by its-agent: %v", violations)
		}
		t.Logf("%d samples, no listening socket", samples)
	})
}

func loadReleaseKey(t *testing.T, path string) ed25519.PrivateKey {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	block, _ := pem.Decode(b)
	if block == nil {
		t.Fatal("release key is not PEM")
	}
	k, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	return k.(ed25519.PrivateKey)
}

func waitPhase(t *testing.T, l update.Layout, want update.Phase, d time.Duration) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if st, err := update.ReadState(l.StatePath()); err == nil && st.Phase == want {
			return
		}
		time.Sleep(time.Second)
	}
	t.Fatalf("state never reached %s within %s", want, d)
}
