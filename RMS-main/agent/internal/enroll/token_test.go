package enroll

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// The installer's token file has a lifecycle, and every one of its transitions
// is a security property:
//
//   - it is READ once, at first boot, by config.Load;
//   - it is DELETED as soon as it has bought a certificate — a spent token left
//     on disk is a credential at rest that buys nothing (invariant #3);
//   - it is KEPT when enrollment was rejected, because the operator may be
//     fixing the URL, not the token, and deleting it would force a new one to
//     be minted for a retry that would otherwise just work;
//   - it never reaches a log line.
//
// Task 14 Step 4 of docs/modules/agent/plan.md asks for the deletion to be
// tested. These are those tests.

// cfgWithTokenFile builds a config whose token comes from a 0600 file rather
// than the environment — the installer's path, not the demo script's.
func cfgWithTokenFile(t *testing.T, cp *fakeCP, token string) config.Config {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "data")
	if err := os.MkdirAll(dir, config.DirMode); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(dir, "enroll-token")
	if err := os.WriteFile(p, []byte(token+"\n"), config.TokenMode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(p, config.TokenMode); err != nil {
		t.Fatal(err)
	}
	t.Setenv(config.EnvDataDir, dir)
	t.Setenv(config.EnvEnrollURL, cp.srv.URL)
	t.Setenv(config.EnvEnrollToken, "")

	cfg, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.EnrollToken != token {
		t.Fatalf("the token file was not read: EnrollToken = %q", cfg.EnrollToken)
	}
	return cfg
}

func TestASuccessfulEnrollmentDeletesTheInstallersTokenFile(t *testing.T) {
	cp := newFakeCP(t)
	cfg := cfgWithTokenFile(t, cp, "tok.installer")

	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	if _, err := os.Stat(cfg.EnrollTokenPath()); !os.IsNotExist(err) {
		t.Fatalf("%s survived a successful enrollment: %v", cfg.EnrollTokenPath(), err)
	}
}

// A rejected enrollment is the operator's to retry. Burning the file here would
// mean a typo in the URL costs a fresh token from the console.
func TestARejectedEnrollmentKeepsTheTokenFile(t *testing.T) {
	cp := newFakeCP(t)
	cp.status = 403
	cfg := cfgWithTokenFile(t, cp, "tok.installer")

	_, err := LoadOrEnroll(context.Background(), cfg, Options{})
	if !errors.Is(err, ErrRejected) {
		t.Fatalf("want a rejection, got %v", err)
	}
	if _, err := os.Stat(cfg.EnrollTokenPath()); err != nil {
		t.Fatalf("the token file must survive a rejection so a retry is possible: %v", err)
	}
}

// The reboot case. config.Load deliberately does not read the token file once
// an identity exists, so nothing else would ever clean this up — and a token
// file that outlives its use is exactly what an attacker with local read wants.
func TestAnAlreadyEnrolledAgentDeletesASpentTokenFile(t *testing.T) {
	cp := newFakeCP(t)
	cfg := cfgWithTokenFile(t, cp, "tok.installer")

	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err != nil {
		t.Fatalf("first boot: %v", err)
	}
	// Someone re-runs the installer, or a configuration-management tool
	// re-applies the token file, over an agent that is already enrolled.
	if err := os.WriteFile(cfg.EnrollTokenPath(), []byte("tok.leftover"), config.TokenMode); err != nil {
		t.Fatal(err)
	}

	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err != nil {
		t.Fatalf("second boot: %v", err)
	}
	if _, err := os.Stat(cfg.EnrollTokenPath()); !os.IsNotExist(err) {
		t.Fatalf("a spent token file survived an enrolled boot: %v", err)
	}
	if n := cp.requests.Load(); n != 1 {
		t.Fatalf("the control plane saw %d enrollments; an enrolled agent must never re-enroll", n)
	}
}

func TestTheTokenNeverReachesALogLine(t *testing.T) {
	cp := newFakeCP(t)
	const token = "tok.aVeryDistinctiveSecret"
	cfg := cfgWithTokenFile(t, cp, token)

	var buf bytes.Buffer
	if _, err := LoadOrEnroll(context.Background(), cfg, Options{Logger: logTo(&buf)}); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	if bytes.Contains(buf.Bytes(), []byte(token)) {
		t.Fatalf("the enrollment token reached the log:\n%s", buf.String())
	}
}
