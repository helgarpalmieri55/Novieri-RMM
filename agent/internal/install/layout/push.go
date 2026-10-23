package layout

import (
	"errors"
	"strings"
)

// THE SSH PUSH'S HAND-OFF, as constants shared by both ends: the pushing
// agent (capabilities/pushinstall, which places the files and runs the fixed
// command) and the placed binary (internal/install's `install` mode, which
// does the work). Invariant 2a(b): the command is a constant template filled
// with these compiled-in paths and ONE validated value, the enroll origin. The
// enrollment token is never an argument -- it is a 0600 file.
const (
	// PushSubcommand is the placed binary's install mode: `its-agent install`.
	PushSubcommand = "install"
	// FlagTokenFile names the staged 0600 token file. The token itself is
	// never an argument (argv is world-readable through /proc/<pid>/cmdline).
	FlagTokenFile = "--token-file"
	// FlagEnrollURL carries the console origin the service enrolls with.
	FlagEnrollURL = "--enroll-url"
	// UnixPushStagingDir is where the SSH push places the artefact and the
	// token before running the install mode, which deletes both.
	UnixPushStagingDir = "/opt/toeshee"
	// UnixStagedBinary and UnixStagedToken are the two files placed there.
	UnixStagedBinary = UnixPushStagingDir + "/agent.pkg"
	UnixStagedToken  = UnixPushStagingDir + "/enroll.token"
)

// CheckEnrollOrigin accepts `https://<host>[:<port>]` and nothing else: a DNS
// name or IPv4 literal of letters, digits, dots and hyphens, and a port of 1
// to 65535. No path, no query, no userinfo, no IPv6 brackets, no character a
// shell, PowerShell or an XML plist would read as anything but itself. This is
// what the installed service's ITS_AGENT_ENROLL_URL holds; the enrollment
// client appends /enroll/v1. The control plane refuses the same set before it
// decrypts a credential (backend dispatch._enroll_base_url).
//
// A function, not a package-level regexp: this package is linked into every
// service process and must cost nothing to initialise.
func CheckEnrollOrigin(origin string) error {
	rest, ok := strings.CutPrefix(origin, "https://")
	if !ok {
		return errors.New("the enroll URL must start with https://")
	}
	host, port, hasPort := strings.Cut(rest, ":")
	if host == "" || len(host) > 253 {
		return errors.New("the enroll URL has no usable host")
	}
	for i := 0; i < len(host); i++ {
		c := host[i]
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '.' || c == '-') {
			return errors.New("the enroll URL must be a bare https origin (a host and an optional port, no path)")
		}
	}
	if host[0] == '.' || host[0] == '-' {
		return errors.New("the enroll URL's host cannot start with a dot or a hyphen")
	}
	if hasPort {
		if port == "" || len(port) > 5 {
			return errors.New("the enroll URL's port is not a number from 1 to 65535")
		}
		n := 0
		for i := 0; i < len(port); i++ {
			if port[i] < '0' || port[i] > '9' {
				return errors.New("the enroll URL's port is not a number from 1 to 65535")
			}
			n = n*10 + int(port[i]-'0')
		}
		if n < 1 || n > 65535 {
			return errors.New("the enroll URL's port is not a number from 1 to 65535")
		}
	}
	return nil
}
