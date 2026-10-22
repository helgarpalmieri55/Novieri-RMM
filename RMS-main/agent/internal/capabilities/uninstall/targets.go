package uninstall

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Targets is everything one installed agent consists of. It mirrors what the
// installers under agent/packaging/ place and what their uninstall scripts
// remove -- and nothing else.
type Targets struct {
	// ServiceName is the systemd unit / Windows service name, or the launchd label.
	ServiceName string
	// Binary is the INSTALLED binary (not the helper's own image).
	Binary string
	// DataDir holds the identity, the private key and the pinned anchors.
	DataDir string
	// Credentials are removed BY NAME before the recursive remove, so a
	// partial failure still takes the private keys (uninstall.ps1's rule).
	Credentials []string
	// Remove are service definitions, config and log directories.
	Remove []string
	// RemoveIfEmpty are directories removed only if nothing else is in them.
	RemoveIfEmpty []string
}

// For returns the targets for one OS. Pure, so every OS is testable here.
func For(goos, binary, dataDir string) Targets {
	t := Targets{
		ServiceName: "its-agent",
		Binary:      binary,
		DataDir:     dataDir,
		Credentials: []string{"enroll-token", "agent.key", "agent.key.pending", "previous.json"},
	}
	switch goos {
	case "windows":
		// install.ps1 / the self-installer: %ProgramFiles%\ITSuite\its-agent.exe.
		// The directory holds nothing else; it goes only if it is empty.
		t.RemoveIfEmpty = []string{winDir(binary)}
	case "darwin":
		t.ServiceName = "com.toeshee.itsagent"
		t.Remove = []string{"/Library/LaunchDaemons/com.toeshee.itsagent.plist", "/var/log/its-agent"}
	default:
		// linux/install.sh. A .deb-installed agent is purged by dpkg instead
		// (platformSystem.PurgePackage), which removes its own unit and binary.
		t.Remove = []string{"/etc/systemd/system/its-agent.service", "/etc/its-agent"}
	}
	return t
}

// winDir is filepath.Dir for a Windows path, on any GOOS (For is tested on Linux).
func winDir(p string) string {
	if i := strings.LastIndex(p, `\`); i > 0 {
		return p[:i]
	}
	return p
}

// identityFile is what makes a data directory an agent's (config.identityFile).
const identityFile = "identity.json"

// ownsDataDir reports whether the data directory holds an agent identity. The
// helper removes a data directory RECURSIVELY only when it does; otherwise only
// the named credentials go (review M3), because the path came from the
// service's environment and could have been pointed anywhere.
func ownsDataDir(t Targets) bool {
	info, err := os.Stat(filepath.Join(t.DataDir, identityFile))
	return err == nil && info.Mode().IsRegular()
}

// helperImage is the staged copy's file name (Windows).
const helperImage = "its-agent-uninstall.exe"

// helperStaging is where the Windows helper's copy goes (review M1):
// `%ProgramData%\ITSuite\uninstall-<n>\its-agent-uninstall.exe`, in a
// directory of its own that the launcher creates owned by Administrators
// and SYSTEM+Administrators-only -- never the service's temp directory.
// Pure, so it is tested on any GOOS.
func helperStaging(programData string, n int64) (dir, exe string) {
	if programData == "" {
		programData = `C:\ProgramData`
	}
	dir = strings.TrimRight(programData, `\`) + `\ITSuite\uninstall-` + strconv.FormatInt(n, 10)
	return dir, dir + `\` + helperImage
}

// bootDeletions is what the helper hands to MoveFileEx(DELAY_UNTIL_REBOOT)
// for its own image: the image, and -- only when it is a staging directory
// helperStaging made, `...\ITSuite\uninstall-<n>` -- that directory after it,
// so the directory is already empty when its turn comes.
func bootDeletions(self string) []string {
	out := []string{self}
	dir := winDir(self)
	parent := winDir(dir)
	if dir != self && parent != dir &&
		strings.HasPrefix(strings.ToLower(winBase(dir)), "uninstall-") &&
		strings.EqualFold(winBase(parent), "ITSuite") {
		out = append(out, dir)
	}
	return out
}

// winBase is the last element of a Windows path, on any GOOS.
func winBase(p string) string {
	if i := strings.LastIndex(p, `\`); i >= 0 {
		return p[i+1:]
	}
	return p
}

// checkTargets refuses targets that cannot be an installed agent. The helper
// deletes recursively; a data directory of "/" is a wiped machine.
func checkTargets(t Targets) error {
	dir := filepath.Clean(t.DataDir)
	if !filepath.IsAbs(dir) {
		return fmt.Errorf("uninstall: data directory %q is not absolute", t.DataDir)
	}
	if filepath.Dir(dir) == dir {
		return fmt.Errorf("uninstall: data directory %q is a filesystem root", t.DataDir)
	}
	base := strings.ToLower(filepath.Base(t.Binary))
	if base != "its-agent" && base != "its-agent.exe" {
		return fmt.Errorf("uninstall: %q is not an its-agent binary", t.Binary)
	}
	return nil
}
