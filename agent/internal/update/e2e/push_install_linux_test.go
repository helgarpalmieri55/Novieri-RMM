//go:build e2e && linux

package e2e

import (
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/capabilities/pushinstall"
	"github.com/toeshee/it-suite/agent/internal/install/layout"
)

// TestPushInstallLinux is the SSH push's target side on a real systemd host:
// the files the push places (the published binary and a 0600 token file, in
// the root-only staging directory), then the ONE fixed command the pushing
// agent runs -- pushinstall.InstallCommand, verbatim, through `sh -c` as the
// SSH session runs it. The placed binary's install mode must install the
// service where agent/packaging/linux/install.sh does, with
// ITS_AGENT_ENROLL_URL in the unit's EnvironmentFile, delete both staged files,
// and the service must enroll with the token against the harness and reach
// the gateway -- after which the agent itself deletes the token file.
func TestPushInstallLinux(t *testing.T) {
	binDir := os.Getenv("E2E_BIN_DIR")
	if binDir == "" {
		t.Fatal("E2E_BIN_DIR is unset; run internal/update/e2e/build.sh first")
	}
	if os.Geteuid() != 0 {
		t.Fatal("TestPushInstallLinux installs a system service and must run as root (sudo)")
	}
	repoRoot, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if v := os.Getenv("E2E_REPO_ROOT"); v != "" {
		repoRoot = v
	}
	const token = "e2e-PUSH-token-0123456789abcdef"

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
	enroll := startEnroll(t, p, gw.url, jobPub)
	enroll.acceptToken(token)
	t.Cleanup(func() {
		plat.uninstall(t, repoRoot)
		_ = os.RemoveAll(layout.UnixPushStagingDir)
	})
	if _, err := os.Stat(plat.dataDir()); !os.IsNotExist(err) {
		t.Fatalf("%s already exists; this test needs a machine with no agent", plat.dataDir())
	}

	// What pushinstall does over SSH before the command: the staging
	// directory, the artefact (0755) and the token (0600).
	run(t, "sh", "-c", pushinstall.PrepareStagingCommand)
	copyFile(t, filepath.Join(binDir, "A"), layout.UnixStagedBinary, 0o755)
	if err := os.WriteFile(layout.UnixStagedToken, []byte(token), 0o600); err != nil {
		t.Fatal(err)
	}

	// NEGATIVE: a path-bearing enroll URL is refused by the pushing side's
	// template and, run by hand, by the install mode -- nothing is installed.
	if _, err := pushinstall.InstallCommand(enroll.url + "/enroll/v1"); err == nil {
		t.Fatal("InstallCommand accepted a path-bearing enroll URL")
	}
	if out, err := exec.Command(layout.UnixStagedBinary, "install", "--token-file", layout.UnixStagedToken,
		"--enroll-url", "http://localhost:1").CombinedOutput(); err == nil {
		t.Fatalf("the install mode accepted an http enroll URL:\n%s", out)
	}
	if _, err := os.Stat(plat.target()); !os.IsNotExist(err) {
		t.Fatalf("a refused install left %s: %v", plat.target(), err)
	}

	command, err := pushinstall.InstallCommand(enroll.url)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(command, token) {
		t.Fatalf("the token is in the install command: %q", command)
	}
	out := run(t, "sh", "-c", command)
	t.Logf("install mode:\n%s", out)

	// Both staged files are gone.
	for _, staged := range []string{layout.UnixStagedBinary, layout.UnixStagedToken} {
		if _, err := os.Stat(staged); !os.IsNotExist(err) {
			t.Errorf("%s was left behind: %v", staged, err)
		}
	}
	// Installed where install.sh installs, as the shipped unit.
	if got := plat.configuredBinary(t); got != plat.target() {
		t.Fatalf("unit ExecStart = %q, want %q", got, plat.target())
	}
	unit, err := os.ReadFile("/etc/systemd/system/its-agent.service")
	if err != nil {
		t.Fatal(err)
	}
	shipped, err := os.ReadFile(filepath.Join(repoRoot, "agent/packaging/linux/its-agent.service"))
	if err != nil {
		t.Fatal(err)
	}
	if string(unit) != string(shipped) {
		t.Fatal("the installed unit is not agent/packaging/linux/its-agent.service")
	}
	// ITS_AGENT_ENROLL_URL where install.sh puts it: the unit's EnvironmentFile.
	envFiles := run(t, "systemctl", "show", "its-agent", "-p", "EnvironmentFiles")
	if !strings.Contains(envFiles, "/etc/its-agent/agent.env") {
		t.Fatalf("EnvironmentFiles = %q", envFiles)
	}
	env, err := os.ReadFile("/etc/its-agent/agent.env")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(env), "\nITS_AGENT_ENROLL_URL="+enroll.url+"\n") || strings.Contains(string(env), token) {
		t.Fatalf("agent.env = %q", env)
	}
	assertMode(t, "/etc/its-agent/agent.env", 0o600)
	assertMode(t, plat.dataDir(), 0o700)
	if active := strings.TrimSpace(run(t, "systemctl", "is-enabled", "its-agent")); active != "enabled" {
		t.Fatalf("its-agent is %q, want enabled", active)
	}

	// The service enrolls with the token, reaches the gateway, and deletes
	// the token file it has spent.
	c := gw.waitVersion(versionA, 3*time.Minute)
	if !strings.Contains(c.caps, "agent.push_install") {
		t.Fatalf("advertised capabilities %q", c.caps)
	}
	deadline := time.Now().Add(time.Minute)
	tokenFile := filepath.Join(plat.dataDir(), "enroll-token")
	for {
		_, err := os.Stat(tokenFile)
		if errors.Is(err, os.ErrNotExist) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s still exists a minute after enrolling: %v", tokenFile, err)
		}
		time.Sleep(time.Second)
	}
	if _, err := os.Stat(filepath.Join(plat.dataDir(), "identity.json")); err != nil {
		t.Fatalf("no identity after enrolling: %v", err)
	}
	var withToken int
	for _, body := range enroll.requests() {
		if body["token"] == token {
			withToken++
		}
	}
	if withToken != 1 {
		t.Fatalf("%d enrollment request(s) carried the pushed token, want 1", withToken)
	}
}

func copyFile(t *testing.T, src, dst string, mode os.FileMode) {
	t.Helper()
	in, err := os.Open(src)
	if err != nil {
		t.Fatal(err)
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, mode)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(out, in); err != nil {
		t.Fatal(err)
	}
	if err := out.Close(); err != nil {
		t.Fatal(err)
	}
}

func assertMode(t *testing.T, path string, want os.FileMode) {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != want {
		t.Errorf("%s mode = %v, want %v", path, info.Mode().Perm(), want)
	}
}
