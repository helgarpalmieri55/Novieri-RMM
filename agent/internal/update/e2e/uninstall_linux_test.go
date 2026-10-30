//go:build e2e && linux

package e2e

import (
	"crypto/ed25519"
	"crypto/rand"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestUninstallLinux is review M4: `agent.uninstall` on a real systemd host.
// A is installed by the shipped linux/install.sh over a seeded identity (as
// the self-update scenarios are), a SIGNED agent.uninstall is sent through
// the harness gateway, and the helper -- a transient unit outside the
// service's cgroup -- must stop and disable the unit and remove the unit
// file, /etc/its-agent, the binary and the data directory.
func TestUninstallLinux(t *testing.T) {
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
	jobPub, jobKey, _ := ed25519.GenerateKey(rand.Reader)
	p := newPKI(t)
	plat := newPlatform()
	gw := startGateway(t, p, jobKey)
	seedDataDir(t, plat.dataDir(), p, gw.url, jobPub)
	t.Cleanup(func() { plat.uninstall(t, repoRoot) })

	plat.install(t, repoRoot, filepath.Join(binDir, "A"))
	c := gw.waitVersion(versionA, 3*time.Minute)
	if !strings.Contains(c.caps, "agent.uninstall") {
		t.Fatalf("advertised capabilities %q lack agent.uninstall", c.caps)
	}

	id := gw.send("agent.uninstall", map[string]any{})
	res := gw.waitResult(id, 2*time.Minute)
	if res.State != "succeeded" || res.Result["scheduled"] != true {
		t.Fatalf("agent.uninstall result = %+v, want succeeded/scheduled", res)
	}

	gone := []string{
		plat.target(), plat.dataDir(), "/etc/systemd/system/its-agent.service", "/etc/its-agent",
	}
	deadline := time.Now().Add(3 * time.Minute)
	for {
		state, _ := runQuiet("systemctl", "show", "its-agent.service", "-p", "LoadState")
		var left []string
		for _, path := range gone {
			if _, err := os.Stat(path); !os.IsNotExist(err) {
				left = append(left, path)
			}
		}
		if strings.Contains(state, "LoadState=not-found") && len(left) == 0 {
			break
		}
		if time.Now().After(deadline) {
			helper, _ := runQuiet("journalctl", "-u", "its-agent-uninstall", "--no-pager", "-n", "100")
			t.Fatalf("not removed within 3 minutes: %s, still present %v\nhelper log:\n%s",
				strings.TrimSpace(state), left, helper)
		}
		time.Sleep(2 * time.Second)
	}
	// The helper's last acts are the daemon-reload that makes LoadState
	// not-found, its "its-agent removed" line and its exit, so the loop above
	// can see everything gone a few milliseconds before its process is (CI,
	// 2026-10-29: is-active asked 1 ms after that line, and the unit
	// deactivated in the same second). What is asserted is that the helper
	// exits, not that it already has by the poll that saw the files go.
	exitBy := time.Now().Add(30 * time.Second)
	for {
		out, _ := runQuiet("systemctl", "is-active", "its-agent-uninstall")
		if strings.TrimSpace(out) != "active" {
			break
		}
		if time.Now().After(exitBy) {
			t.Fatal("the uninstall helper unit is still running 30 seconds after everything was removed")
		}
		time.Sleep(200 * time.Millisecond)
	}
}
