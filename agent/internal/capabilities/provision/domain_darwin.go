//go:build darwin

package provision

import (
	"os"
	"path/filepath"
	"strings"
)

// macOS directory membership, read with ONE os.Stat and no process.
//
// A Mac bound to Active Directory has an OpenDirectory configuration plist per
// bound domain under /Library/Preferences/OpenDirectory/Configurations/Active
// Directory/. `dsconfigad -show` reads the same place; so does `dscl localhost
// -list /Active\ Directory`. Reading the directory entry directly costs one
// syscall and spawns nothing -- and on a fleet running third-party EDR, a
// SYSTEM-privileged binary that shells out to inspect directory binding looks
// exactly like reconnaissance, whatever its intent.
//
// NEVER EXECUTED IN THIS REPOSITORY'S CI. The path is compiled by the
// `GOOS=darwin go vet` step; the logic that consumes its output is unit-tested
// on Linux through the Host seam.
//
// The plist FILENAME is the bound domain (e.g. "corp.toeshee.local.plist"),
// which is what domainMatches is given. If the directory exists but is empty,
// this machine has an OpenDirectory stack and no AD binding -- Supported=true,
// Joined=false, which is a real answer and not an absence.
const adConfigDir = "/Library/Preferences/OpenDirectory/Configurations/Active Directory"

func domainStatus() (DomainStatus, error) {
	entries, err := os.ReadDir(adConfigDir)
	if err != nil {
		if os.IsNotExist(err) {
			// No AD configuration directory at all. On macOS that directory is
			// created by the binding, so its absence is a genuine "not bound"
			// rather than a failure to look.
			return DomainStatus{Supported: true, Joined: false}, nil
		}
		// Permission denied, or an OS that has moved the path. Supported=false
		// so run() reports "cannot be asked" instead of "not joined".
		return DomainStatus{}, err
	}
	for _, e := range entries {
		name := e.Name()
		if !strings.EqualFold(filepath.Ext(name), ".plist") {
			continue
		}
		return DomainStatus{
			Supported: true,
			Joined:    true,
			Domain:    strings.TrimSuffix(name, filepath.Ext(name)),
		}, nil
	}
	return DomainStatus{Supported: true, Joined: false}, nil
}
