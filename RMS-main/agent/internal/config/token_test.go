package config_test

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// The installer's problem: it holds a one-shot enrollment token and has to hand
// it to a service it is about to start.
//
//   - argv is out — /proc/<pid>/cmdline is world-readable, so `ps` shows it to
//     every local user for the lifetime of the process.
//   - the unit's Environment= is out — `systemctl show its-agent` prints it, and
//     it survives in the unit file on disk long after the token is spent.
//
// What is left is a 0600 file in the data directory, which the agent reads at
// first boot and DELETES once the token has bought a certificate. These tests
// pin that path, its permission floor, and its precedence against the env var
// that scripts/demo_e2e_agent.py uses.

func writeToken(t *testing.T, dir, token string, mode os.FileMode) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(dir, "enroll-token")
	if err := os.WriteFile(p, []byte(token), mode); err != nil {
		t.Fatal(err)
	}
	// WriteFile's mode is masked by the umask; the test needs the mode it asked
	// for, because the mode is what is under test.
	if err := os.Chmod(p, mode); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestEnrollTokenPathIsInTheDataDir(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(envDir, dir)
	t.Setenv(envURL, "http://127.0.0.1:8000")
	t.Setenv(envToken, "tok")
	c, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(dir, "enroll-token")
	if c.EnrollTokenPath() != want {
		t.Fatalf("EnrollTokenPath() = %q, want %q", c.EnrollTokenPath(), want)
	}
}

func TestLoadReadsTheTokenFromAFileWhenTheEnvIsUnset(t *testing.T) {
	dir := t.TempDir()
	writeToken(t, dir, "tok.from.file\n", 0o600)
	t.Setenv(envDir, dir)
	t.Setenv(envURL, "http://127.0.0.1:8000")
	t.Setenv(envToken, "")

	c, err := config.Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	// Trailing newline trimmed: every editor and every `printf ... >` in an
	// installer adds one, and a token with a stray \n is a bare 403 that reads
	// like a revoked token.
	if c.EnrollToken != "tok.from.file" {
		t.Fatalf("EnrollToken = %q, want %q", c.EnrollToken, "tok.from.file")
	}
}

func TestTheEnvironmentWinsOverTheTokenFile(t *testing.T) {
	dir := t.TempDir()
	writeToken(t, dir, "tok.from.file", 0o600)
	t.Setenv(envDir, dir)
	t.Setenv(envURL, "http://127.0.0.1:8000")
	t.Setenv(envToken, "tok.from.env")

	c, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.EnrollToken != "tok.from.env" {
		t.Fatalf("EnrollToken = %q; the env var is what demo_e2e_agent.py sets and must win", c.EnrollToken)
	}
}

// Fail closed. A token readable by any local user is a credential disclosure
// (invariant #3), and the agent cannot un-disclose it by proceeding quietly.
func TestLoadRefusesATokenFileOthersCanRead(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix mode bits are not the access control on windows")
	}
	for _, mode := range []os.FileMode{0o644, 0o640, 0o604, 0o666} {
		t.Run(mode.String(), func(t *testing.T) {
			dir := t.TempDir()
			writeToken(t, dir, "tok", mode)
			t.Setenv(envDir, dir)
			t.Setenv(envURL, "http://127.0.0.1:8000")
			t.Setenv(envToken, "")

			_, err := config.Load()
			if err == nil {
				t.Fatalf("Load accepted a %v enrollment token file", mode)
			}
			if !strings.Contains(err.Error(), "enroll-token") {
				t.Fatalf("the error must name the offending file, got: %v", err)
			}
		})
	}
}

// ...but a bad mode on a SPENT token file must not brick an enrolled agent.
// The token is only consulted when there is no identity, so a leftover file
// with the wrong mode is deleted (see enroll.LoadOrEnroll), not fatal.
func TestAnEnrolledAgentIgnoresALeftoverTokenFile(t *testing.T) {
	dir := t.TempDir()
	writeToken(t, dir, "spent", 0o644)
	if err := os.WriteFile(filepath.Join(dir, "identity.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(envDir, dir)
	t.Setenv(envURL, "")
	t.Setenv(envToken, "")

	c, err := config.Load()
	if err != nil {
		t.Fatalf("an enrolled agent must start regardless of a leftover token file: %v", err)
	}
	if c.EnrollToken != "" {
		t.Fatalf("EnrollToken = %q; a spent token must not be carried into a running agent", c.EnrollToken)
	}
}

func TestAnEmptyTokenFileIsNoToken(t *testing.T) {
	dir := t.TempDir()
	writeToken(t, dir, "   \n", 0o600)
	t.Setenv(envDir, dir)
	t.Setenv(envURL, "http://127.0.0.1:8000")
	t.Setenv(envToken, "")

	if _, err := config.Load(); err == nil {
		t.Fatal("want ErrNoEnrollment when the token file holds only whitespace")
	}
}

func TestConsumeEnrollTokenRemovesTheFileAndIsIdempotent(t *testing.T) {
	dir := t.TempDir()
	p := writeToken(t, dir, "tok", 0o600)
	t.Setenv(envDir, dir)
	t.Setenv(envURL, "http://127.0.0.1:8000")
	t.Setenv(envToken, "")
	c, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}

	if err := c.ConsumeEnrollToken(); err != nil {
		t.Fatalf("ConsumeEnrollToken: %v", err)
	}
	if _, err := os.Stat(p); !os.IsNotExist(err) {
		t.Fatalf("the token file survived being consumed: %v", err)
	}
	// Called on every enrolled boot, so a missing file is the normal case.
	if err := c.ConsumeEnrollToken(); err != nil {
		t.Fatalf("ConsumeEnrollToken on an absent file: %v", err)
	}
}
