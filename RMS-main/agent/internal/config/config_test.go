package config_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// The env var names are fixed by scripts/demo_e2e_agent.py:191-198, which
// launches the real binary with exactly these three. Renaming one breaks the
// end-to-end demo, not just this test.
const (
	envURL   = "ITS_AGENT_ENROLL_URL"
	envToken = "ITS_AGENT_ENROLL_TOKEN"
	envDir   = "ITS_AGENT_DATA_DIR"
)

func TestLoadRequiresEnrollURLOnFirstRun(t *testing.T) {
	t.Setenv(envDir, t.TempDir())
	t.Setenv(envURL, "")
	t.Setenv(envToken, "")
	if c, err := config.Load(); err == nil {
		t.Fatalf("want error when %s is unset and no identity exists; got %+v", envURL, c)
	}
}

func TestLoadRequiresTokenOnFirstRun(t *testing.T) {
	t.Setenv(envDir, t.TempDir())
	t.Setenv(envURL, "http://127.0.0.1:8000")
	t.Setenv(envToken, "")
	if c, err := config.Load(); err == nil {
		t.Fatalf("want error when %s is unset and no identity exists; got %+v", envToken, c)
	}
}

// An already-enrolled agent has no enrollment token: the token is single-use
// and consumed at first boot (app/api/v1/enrollment.py). Demanding one on every
// restart would make a reboot un-survivable.
func TestLoadSucceedsWithoutEnrollmentWhenAnIdentityExists(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(envDir, dir)
	t.Setenv(envURL, "")
	t.Setenv(envToken, "")
	if err := os.WriteFile(filepath.Join(dir, "identity.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := config.Load(); err != nil {
		t.Fatalf("an enrolled agent must start with no enrollment env: %v", err)
	}
}

func TestPathsAreRootedAtDataDir(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(envDir, dir)
	t.Setenv(envURL, "http://127.0.0.1:8000")
	t.Setenv(envToken, "tok")
	c, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	paths := map[string]string{
		"IdentityPath": c.IdentityPath(),
		"KeyPath":      c.KeyPath(),
		"PinsPath":     c.PinsPath(),
		"NoncePath":    c.NoncePath(),
		"BufferPath":   c.BufferPath(),
	}
	seen := map[string]string{}
	for name, p := range paths {
		if !strings.HasPrefix(p, dir+string(filepath.Separator)) {
			t.Errorf("%s = %s escapes the data dir %s", name, p, dir)
		}
		if other, dup := seen[p]; dup {
			t.Errorf("%s and %s both resolve to %s", name, other, p)
		}
		seen[p] = name
	}
}

// A data dir supplied by the environment is a path, not a template: a value
// that traverses out of itself would put the device private key somewhere the
// operator did not choose.
func TestDataDirDefaultsWhenUnset(t *testing.T) {
	t.Setenv(envDir, "")
	t.Setenv(envURL, "http://127.0.0.1:8000")
	t.Setenv(envToken, "tok")
	c, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.DataDir == "" {
		t.Fatal("DataDir must have a platform default")
	}
	if !filepath.IsAbs(c.DataDir) {
		t.Fatalf("default DataDir %q must be absolute", c.DataDir)
	}
}

func TestEnrollURLTrailingSlashIsNormalised(t *testing.T) {
	t.Setenv(envDir, t.TempDir())
	t.Setenv(envURL, "http://127.0.0.1:8000/")
	t.Setenv(envToken, "tok")
	c, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	// Task 5 posts to EnrollURL + "/enroll/v1"; a retained trailing slash makes
	// that "//enroll/v1" and the server 404s.
	if c.EnrollURL != "http://127.0.0.1:8000" {
		t.Fatalf("EnrollURL = %q, want the trailing slash trimmed", c.EnrollURL)
	}
}
