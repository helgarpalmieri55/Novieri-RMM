//go:build e2e && darwin

package e2e

import (
	"crypto/sha1"
	"encoding/hex"
	"encoding/pem"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type darwinPlatform struct{}

func newPlatform() platform { return darwinPlatform{} }

func (darwinPlatform) dataDir() string { return "/var/lib/its-agent" }
func (darwinPlatform) target() string  { return "/usr/local/libexec/its-agent" }

func (darwinPlatform) install(t *testing.T, repoRoot, binary string) {
	run(t, "bash", filepath.Join(repoRoot, "agent/packaging/darwin/install.sh"), "--binary", binary)
}

func (darwinPlatform) uninstall(t *testing.T, repoRoot string) {
	_, _ = runQuiet("bash", filepath.Join(repoRoot, "agent/packaging/darwin/uninstall.sh"))
}

const systemKeychain = "/Library/Keychains/System.keychain"

// darwinCAHash is the SHA-1 of the trusted CA's DER, which is how `security`
// names one certificate unambiguously.
var darwinCAHash string

func (darwinPlatform) trustCA(t *testing.T, pemPath string) {
	b, err := os.ReadFile(pemPath)
	if err != nil {
		t.Fatal(err)
	}
	block, _ := pem.Decode(b)
	if block == nil {
		t.Fatalf("%s holds no PEM block", pemPath)
	}
	sum := sha1.Sum(block.Bytes)
	darwinCAHash = strings.ToUpper(hex.EncodeToString(sum[:]))
	run(t, "security", "add-trusted-cert", "-d", "-r", "trustRoot", "-k", systemKeychain, pemPath)
}

// untrustCA deletes the certificate from the System keychain. It does NOT
// call `security remove-trusted-cert -d`: removing admin trust settings asks
// for interactive authorization even as root, and on a headless runner that
// prompt is never answered -- the first macOS run sat in this cleanup for 46
// minutes after all seven scenarios had passed. Deleting the certificate is
// non-interactive as root, and a trust setting left naming a certificate that
// no longer exists anchors nothing. The CA's private key lived only in this
// process, so even a leftover certificate could sign nothing new.
func (darwinPlatform) untrustCA(*testing.T) {
	if darwinCAHash != "" {
		_, _ = runQuiet("security", "delete-certificate", "-Z", darwinCAHash, systemKeychain)
	}
}

func (darwinPlatform) configuredBinary(t *testing.T) string {
	out := run(t, "launchctl", "print", "system/com.toeshee.itsagent")
	for _, line := range strings.Split(out, "\n") {
		if k, v, ok := strings.Cut(strings.TrimSpace(line), " = "); ok && k == "program" {
			return v
		}
	}
	t.Fatalf("launchctl print names no program:\n%s", out)
	return ""
}

func (darwinPlatform) killHelper(t *testing.T) {
	run(t, "pkill", "-9", "-f", "--", "--update-helper")
}

func (darwinPlatform) helperGone() (bool, string) {
	out, err := runQuiet("launchctl", "print", "system/com.toeshee.itsagent.update")
	if err != nil {
		return true, ""
	}
	// A loaded job that is not running still counts: the helper unloads
	// itself as its last act.
	return false, strings.TrimSpace(out)
}

func (darwinPlatform) listeners() ([]string, error) {
	out, err := runQuiet("lsof", "-nP", "-iTCP", "-sTCP:LISTEN")
	if err != nil && strings.TrimSpace(out) != "" {
		return nil, err
	}
	return agentLines(out), nil
}
