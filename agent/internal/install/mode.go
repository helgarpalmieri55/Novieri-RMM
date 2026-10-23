// Package install is the Windows self-installer for the install link
// (docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md
// §1.3), and the Linux and macOS install mode the SSH push runs on its target
// (pushunix.go, push_unix.go: `its-agent install --token-file <file>
// --enroll-url <origin>`, which does what packaging/{linux,darwin}/install.sh
// do).
//
// A technician opens the organisation's install link on the machine and
// downloads `ITMS-Agent-Setup_<host>[@<port>]_<code>.exe` -- the published,
// signed artefact, byte for byte, under a name that carries the console's host
// and the link code (FromFileName; the operator's decision of 2026-09-28
// replaced plan D6's Mark of the Web, so a copy from a share or a USB stick
// works too). Started with no arguments under that name, the binary installs
// itself: it re-launches elevated (one UAC prompt), copies itself to
// %ProgramFiles%\ITSuite, sets install.ps1's owner and ACL on the data
// directory and on its parent %ProgramData%\ITSuite (parent first), writes
// the code to the token file, registers and starts the service with
// `https://<host>[:<port>]` as its enrollment URL, and shows one small window.
// That URL is verified against the system trust store like any other; nothing
// on this path skips TLS verification.
//
// THE CODE NEVER TOUCHES ARGV AS AN ARGUMENT (packaging README rule 1). It is
// read from this process's own file name; the elevated re-launch carries only
// ElevatedArg and no environment of ours; the service is registered with no
// arguments and its environment carries only the data directory and the URL.
// See plan D11 for the one place it does appear: the installer's own image
// path.
//
// IDLE BUDGET. The service never calls into this package: cmd/agent decides
// "installer mode?" from FilePrefix and ElevatedArg, which are constants, and
// dormant_test.go holds this package to having nothing to initialise.
package install

import "strings"

// FilePrefix is the start of the file name the install link serves
// (backend link_page.windows_file_name).
const FilePrefix = "ITMS-Agent-Setup_"

// ElevatedArg is the ONLY argument the UAC re-launch carries.
const ElevatedArg = "--elevated-install"

// EnvNoUI, set to "1", replaces the result window with a line on stderr and,
// ONLY in a process that is already elevated, skips the host question
// (asksHost). The Windows e2e job (Task 13) sets it: a hosted runner has no one
// to click OK, and runs elevated.
const EnvNoUI = "ITS_AGENT_INSTALLER_NO_UI"

// asksHost decides whether the host question (confirmText) is asked. Never in
// the UAC re-launch: that exists only because the person said Yes. Otherwise
// always -- except when EnvNoUI is set AND this process is already elevated,
// which is the e2e runner (final review M6). EnvNoUI alone is an environment
// variable anyone who can write the user's environment can set; honoured
// unelevated, it would take away the one question standing between an
// unauthenticated file name and the UAC prompt.
func asksHost(relaunched, noUI, elevated bool) bool {
	if relaunched {
		return false
	}
	return !(noUI && elevated)
}

// confirmText is the Yes/No question asked before anything is elevated
// (review I3). The console comes from a file NAME, which anyone who can hand
// a technician a file can write; the question puts that name in front of the
// person who is about to approve the UAC prompt.
func confirmText(link Link) string {
	return "Install the ITMS agent and connect this computer to " +
		strings.TrimPrefix(link.Console, "https://") + "?"
}
