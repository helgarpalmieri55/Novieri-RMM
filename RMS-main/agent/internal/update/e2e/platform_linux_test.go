//go:build e2e && linux

package e2e

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type linuxPlatform struct{}

func newPlatform() platform { return linuxPlatform{} }

func (linuxPlatform) dataDir() string { return "/var/lib/its-agent" }
func (linuxPlatform) target() string  { return "/usr/local/bin/its-agent" }

func (linuxPlatform) install(t *testing.T, repoRoot, binary string) {
	run(t, "bash", filepath.Join(repoRoot, "agent/packaging/linux/install.sh"), "--binary", binary)
}

func (linuxPlatform) uninstall(t *testing.T, repoRoot string) {
	_, _ = runQuiet("bash", filepath.Join(repoRoot, "agent/packaging/linux/uninstall.sh"))
}

const linuxCA = "/usr/local/share/ca-certificates/its-agent-e2e.crt"

func (linuxPlatform) trustCA(t *testing.T, pemPath string) {
	b, err := os.ReadFile(pemPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(linuxCA, b, 0o644); err != nil {
		t.Fatal(err)
	}
	run(t, "update-ca-certificates")
}

func (linuxPlatform) untrustCA(*testing.T) {
	_ = os.Remove(linuxCA)
	_, _ = runQuiet("update-ca-certificates", "--fresh")
}

func (linuxPlatform) configuredBinary(t *testing.T) string {
	out := run(t, "systemctl", "show", "its-agent", "-p", "ExecStart")
	_, rest, _ := strings.Cut(out, "path=")
	path, _, _ := strings.Cut(rest, " ;")
	return strings.TrimSpace(path)
}

func (linuxPlatform) killHelper(t *testing.T) {
	run(t, "systemctl", "kill", "-s", "SIGKILL", "its-agent-update.service")
}

func (linuxPlatform) helperGone() (bool, string) {
	out, _ := runQuiet("systemctl", "show", "its-agent-update.service", "-p", "LoadState", "-p", "ActiveState")
	active := strings.Contains(out, "ActiveState=active") || strings.Contains(out, "ActiveState=activating") ||
		strings.Contains(out, "ActiveState=deactivating")
	return !active, strings.TrimSpace(out)
}

func (linuxPlatform) listeners() ([]string, error) {
	out, err := runQuiet("ss", "-ltnupH")
	if err != nil {
		return nil, err
	}
	return agentLines(out), nil
}
