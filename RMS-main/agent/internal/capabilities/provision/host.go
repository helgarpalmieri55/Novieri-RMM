package provision

import (
	"os"
	"runtime"
)

// thisOSFamily maps GOOS to the control plane's vocabulary, exactly as
// enroll.osFamily does for the enrollment request.
//
// The mapping is duplicated rather than exported from internal/enroll, because
// exporting it would mean this package imports a WRITE-capable symbol path into
// enroll for a three-line switch. `no_second_enrollment_test.go` keeps this
// package's use of internal/enroll to Load and ExpiryState, and a duplicated
// switch is a cheaper price than widening that surface. os_family_test.go pins
// the two copies to the same answers.
//
// darwin -> macos is the one that bites: a Mac reporting "darwin" would never
// match a job whose image says "macos", and every macOS provisioning run in the
// fleet would be refused.
func thisOSFamily() string { return osFamilyFor(runtime.GOOS) }

// osFamilyFor is thisOSFamily with GOOS as a parameter, so the mapping can be
// checked against the exported enum for every GOOS this binary is built for
// rather than only for the one running the test.
func osFamilyFor(goos string) string {
	switch goos {
	case "windows":
		return "windows"
	case "darwin":
		return "macos"
	case "linux":
		return "linux"
	default:
		// Deliberately not "": an empty string would compare equal to a params
		// document that omitted os_family, and parseParams already refuses
		// that case. A GOOS with no server-side family must match nothing.
		return "unsupported:" + goos
	}
}

// osHost is the production Host: the platform's own answers.
//
// Hostname is shared across platforms (os.Hostname is GetComputerNameEx on
// Windows and gethostname elsewhere -- a library call on both, no process).
// DomainStatus is per-platform and lives in domain_windows.go,
// domain_darwin.go and domain_other.go.
type osHost struct{}

func (osHost) Hostname() (string, error) { return os.Hostname() }

func (h osHost) DomainStatus() (DomainStatus, error) { return domainStatus() }
