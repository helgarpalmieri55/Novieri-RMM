// Package packaging_test is what CI can honestly say about the two platforms it
// cannot run.
//
// The Linux installer is executed for real (see agent/packaging/README.md
// "What was verified"). The Windows service and the launchd job are not: this
// repository's CI is ubuntu-latest, and nothing here boots a Windows SCM or a
// macOS launchd. What CAN be checked without those is structure — that the
// files exist, that they parse, that the hardening directives are present, and
// above all that no packaging artifact puts the enrollment token somewhere the
// whole machine can read it.
//
// That last one is the reason this file exists rather than a README paragraph
// promising the same thing. A token in `Environment=` in a unit file is printed
// by `systemctl show`, survives in /etc long after the token is spent, and
// nothing else in this repository would notice.
package packaging_test

import (
	"encoding/xml"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func read(t *testing.T, rel string) string {
	t.Helper()
	b, err := os.ReadFile(rel)
	if err != nil {
		t.Fatalf("%s: %v", rel, err)
	}
	return string(b)
}

// unitDirectives parses a systemd unit into "Section.Key" -> [values].
//
// Deliberately naive — systemd's own parser this is not — but enough to assert
// on directives, and it fails loudly on a line that is neither a comment, a
// section header, nor a key=value.
func unitDirectives(t *testing.T, text string) map[string][]string {
	t.Helper()
	out := map[string][]string{}
	section := ""
	for i, line := range strings.Split(text, "\n") {
		l := strings.TrimSpace(line)
		if l == "" || strings.HasPrefix(l, "#") || strings.HasPrefix(l, ";") {
			continue
		}
		if strings.HasPrefix(l, "[") && strings.HasSuffix(l, "]") {
			section = strings.Trim(l, "[]")
			continue
		}
		k, v, ok := strings.Cut(l, "=")
		if !ok {
			t.Fatalf("unit line %d is neither a section, a comment nor key=value: %q", i+1, l)
		}
		out[section+"."+strings.TrimSpace(k)] = append(out[section+"."+strings.TrimSpace(k)], strings.TrimSpace(v))
	}
	return out
}

// plistKeys returns the <key> names in a plist and asserts it is well-formed
// XML on the way.
func plistKeys(t *testing.T, text string) map[string]bool {
	t.Helper()
	dec := xml.NewDecoder(strings.NewReader(text))
	keys := map[string]bool{}
	inKey := false
	for {
		tok, err := dec.Token()
		if err != nil {
			if err.Error() == "EOF" {
				break
			}
			t.Fatalf("the launchd plist is not well-formed XML: %v", err)
		}
		switch e := tok.(type) {
		case xml.StartElement:
			inKey = e.Name.Local == "key"
		case xml.CharData:
			if inKey {
				keys[strings.TrimSpace(string(e))] = true
			}
		case xml.EndElement:
			inKey = false
		}
	}
	return keys
}

// maintainerScripts are dpkg's, and are POSIX sh rather than bash by
// requirement: dpkg guarantees /bin/sh and nothing more.
var maintainerScripts = []string{"linux/debian/postinst", "linux/debian/prerm", "linux/debian/postrm"}

func isMaintainerScript(p string) bool {
	for _, m := range maintainerScripts {
		if p == m {
			return true
		}
	}
	return false
}

// shellScripts returns the bash scripts: every *.sh plus the extension-less
// commands that are installed onto endpoints. The dpkg maintainer scripts are
// excluded and checked separately.
func shellScripts(t *testing.T) []string {
	t.Helper()
	out := []string{"linux/its-agent-enroll"}
	err := filepath.WalkDir(".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() && strings.HasSuffix(p, ".sh") && !isMaintainerScript(p) {
			out = append(out, p)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(out) < 5 {
		t.Fatalf("only %d shell scripts found; this test would pass close to vacuously", len(out))
	}
	return out
}

// ---------------------------------------------------------------------------

func TestEveryPlatformHasItsArtifacts(t *testing.T) {
	// The point of Task 14: putting an agent on a real endpoint must not require
	// hand-copying a binary and hand-writing a unit file.
	for _, f := range []string{
		"README.md",
		"build.sh",
		"linux/its-agent.service",
		"linux/install.sh",
		"linux/uninstall.sh",
		"linux/build-deb.sh",
		"linux/its-agent-enroll",
		"linux/debian/control.in",
		"linux/debian/postinst",
		"linux/debian/prerm",
		"linux/debian/postrm",
		"darwin/com.toeshee.itsagent.plist",
		"darwin/install.sh",
		"darwin/uninstall.sh",
		"windows/install.ps1",
		"windows/uninstall.ps1",
		// The Windows counterpart of linux/its-agent-enroll. Managed
		// distribution (GPO, Intune) installs and enrols in TWO steps, because
		// the package is staged on a share every machine reads and must
		// therefore carry no credential -- see
		// docs/modules/agent-distribution/spec-managed-distribution.md §1.1.
		// Without this script the only way to enrol a Windows endpoint was to
		// hand the token to install.ps1, i.e. to the thing being staged.
		"windows/its-agent-enroll.ps1",
	} {
		if _, err := os.Stat(f); err != nil {
			t.Errorf("missing packaging artifact: %v", err)
		}
	}
}

// THE token test. Every artifact that lands on disk on the endpoint, or that
// `systemctl show` / `launchctl print` will echo, is checked for the token
// having been baked into it.
func TestNoServiceDefinitionCarriesTheEnrollmentToken(t *testing.T) {
	for _, f := range []string{
		"linux/its-agent.service",
		"darwin/com.toeshee.itsagent.plist",
	} {
		text := read(t, f)
		if strings.Contains(text, "ITS_AGENT_ENROLL_TOKEN") {
			t.Errorf("%s names ITS_AGENT_ENROLL_TOKEN: a service definition is world-readable "+
				"and outlives the token. It belongs in a 0600 file in the data dir "+
				"(config.EnrollTokenPath), which the agent deletes once it is spent.", f)
		}
	}
}

// The service command line is the other place a token must never be: argv is
// world-readable through /proc/<pid>/cmdline for as long as the agent runs.
func TestTheServiceCommandLineTakesNoArguments(t *testing.T) {
	d := unitDirectives(t, read(t, "linux/its-agent.service"))
	exec := d["Service.ExecStart"]
	if len(exec) != 1 {
		t.Fatalf("want exactly one ExecStart, got %v", exec)
	}
	if fields := strings.Fields(exec[0]); len(fields) != 1 {
		t.Errorf("ExecStart carries arguments (%q); every one of them is visible in `ps`", exec[0])
	}

	// launchd's equivalent: ProgramArguments must hold the binary and nothing
	// else. Read from the array itself rather than by grepping the file, which
	// would match the word "enroll-token" in a comment and pass or fail for the
	// wrong reason.
	args := programArguments(t, read(t, "darwin/com.toeshee.itsagent.plist"))
	if len(args) != 1 {
		t.Errorf("launchd ProgramArguments = %v; want the binary alone — every argument is visible in `ps`", args)
	}
}

// programArguments returns the <string> values of the plist's
// ProgramArguments array.
func programArguments(t *testing.T, text string) []string {
	t.Helper()
	_, after, ok := strings.Cut(text, "<key>ProgramArguments</key>")
	if !ok {
		t.Fatal("the plist has no ProgramArguments key")
	}
	block, _, ok := strings.Cut(after, "</array>")
	if !ok {
		t.Fatal("ProgramArguments is not followed by an array")
	}
	var args []string
	for {
		_, rest, found := strings.Cut(block, "<string>")
		if !found {
			return args
		}
		val, tail, found := strings.Cut(rest, "</string>")
		if !found {
			t.Fatal("unterminated <string> in ProgramArguments")
		}
		args = append(args, strings.TrimSpace(val))
		block = tail
	}
}

func TestTheSystemdUnitIsHardenedAndRestarts(t *testing.T) {
	d := unitDirectives(t, read(t, "linux/its-agent.service"))

	// Each of these is a claim agent/packaging/README.md makes. A README that
	// says "ProtectSystem=strict" and a unit that does not is worse than
	// neither, because it is the README an auditor reads.
	want := map[string]string{
		"Service.Type":             "simple",
		"Service.Restart":          "always",
		"Service.NoNewPrivileges":  "yes",
		"Service.ProtectSystem":    "strict",
		"Service.ProtectHome":      "yes",
		"Service.PrivateTmp":       "yes",
		"Service.StateDirectory":   "its-agent",
		"Service.LockPersonality":  "yes",
		"Service.RestrictSUIDSGID": "yes",
	}
	for k, v := range want {
		got := d[k]
		if len(got) != 1 || got[0] != v {
			t.Errorf("%s = %v, want %q", k, got, v)
		}
	}
	for _, k := range []string{"Service.RestartSec", "Service.CapabilityBoundingSet", "Service.AmbientCapabilities"} {
		if len(d[k]) == 0 {
			t.Errorf("%s is unset", k)
		}
	}

	// Invariant #2: no listening port, therefore no socket activation and no
	// dependency on one.
	for k := range d {
		if strings.Contains(k, "Sockets") || strings.Contains(k, "ListenStream") {
			t.Errorf("%s: the agent opens no listening port (invariant #2)", k)
		}
	}

	// The data directory is the only thing it may write to, and the mode is the
	// one config.DirMode enforces from the other side.
	if got := d["Service.StateDirectoryMode"]; len(got) != 1 || got[0] != "0700" {
		t.Errorf("StateDirectoryMode = %v, want 0700 (the device private key lives there)", got)
	}
	rw := d["Service.ReadWritePaths"]
	if len(rw) != 1 || !strings.Contains(rw[0], "/var/lib/its-agent") {
		t.Errorf("ReadWritePaths = %v, want the state directory and nothing else", rw)
	}
}

func TestTheLaunchdPlistIsWellFormedAndDaemonic(t *testing.T) {
	text := read(t, "darwin/com.toeshee.itsagent.plist")
	keys := plistKeys(t, text)
	for _, k := range []string{"Label", "ProgramArguments", "RunAtLoad", "KeepAlive", "StandardErrorPath", "EnvironmentVariables"} {
		if !keys[k] {
			t.Errorf("the plist has no %s key", k)
		}
	}
	if keys["Sockets"] {
		t.Error("the plist requests socket activation; the agent opens no listening port (invariant #2)")
	}
	if !strings.Contains(text, "<string>com.toeshee.itsagent</string>") {
		t.Error("the Label must be com.toeshee.itsagent — the uninstall script boots it out by that name")
	}
}

func TestShellScriptsAreStrictAndExecutable(t *testing.T) {
	for _, p := range shellScripts(t) {
		text := read(t, p)
		if !strings.HasPrefix(text, "#!/usr/bin/env bash\n") && !strings.HasPrefix(text, "#!/bin/bash\n") {
			t.Errorf("%s: no bash shebang", p)
		}
		if !strings.Contains(text, "set -euo pipefail") {
			t.Errorf("%s: no `set -euo pipefail`; a half-applied install is worse than a failed one", p)
		}
		// `set -x` would echo the enrollment token to the installer's stdout and
		// into whatever log the deployment tool keeps.
		for _, line := range strings.Split(text, "\n") {
			if strings.TrimSpace(line) == "set -x" {
				t.Errorf("%s: `set -x` would echo the enrollment token", p)
			}
		}
		if runtime.GOOS == "windows" {
			// git for Windows does not materialize the POSIX executable bit
			// on checkout, and os.Stat on NTFS cannot report one regardless
			// of what the Linux checkout that ships this script carries. The
			// property under test is a fact about the git-tracked mode these
			// scripts carry for the Linux/macOS hosts that actually run
			// them, not something observable on this runner's filesystem.
			continue
		}
		info, err := os.Stat(p)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm()&0o111 == 0 {
			t.Errorf("%s is mode %04o: not executable, so `./install.sh` fails for the operator "+
				"and git records it that way for everyone", p, info.Mode().Perm())
		}
	}
}

// dpkg maintainer scripts: POSIX sh, `set -e`, and executable — dpkg runs them
// directly and a non-executable one aborts an install halfway.
func TestMaintainerScriptsArePosixAndExecutable(t *testing.T) {
	for _, p := range maintainerScripts {
		text := read(t, p)
		if !strings.HasPrefix(text, "#!/bin/sh\n") {
			t.Errorf("%s: dpkg guarantees /bin/sh and nothing more; use a POSIX sh shebang", p)
		}
		if strings.Contains(text, "pipefail") {
			t.Errorf("%s: `pipefail` is a bashism; dash will fail the whole install on it", p)
		}
		if !strings.Contains(text, "set -e") {
			t.Errorf("%s: no `set -e`", p)
		}
		if runtime.GOOS == "windows" {
			// Same as TestShellScriptsAreStrictAndExecutable: not observable
			// via os.Stat on NTFS regardless of the git-tracked mode.
			continue
		}
		info, err := os.Stat(p)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm()&0o111 == 0 {
			t.Errorf("%s is mode %04o: dpkg cannot execute it and the install aborts mid-way",
				p, info.Mode().Perm())
		}
	}
}

// The package is a DISTRIBUTABLE artifact: it is copied to file servers, baked
// into images and cached by apt. A credential in it would be a credential in
// all of those. Enrollment is therefore a separate post-install step, and this
// asserts nothing in the packaging pipeline quietly reintroduces one.
func TestThePackageCarriesNoCredentials(t *testing.T) {
	for _, p := range append([]string{"linux/build-deb.sh", "linux/debian/control.in"}, maintainerScripts...) {
		for i, line := range strings.Split(read(t, p), "\n") {
			l := strings.TrimSpace(line)
			// Comments and the instructions printed to the operator legitimately
			// name the variable; what must not exist is an assignment that puts
			// a value into the package or its scripts.
			if strings.HasPrefix(l, "#") || strings.HasPrefix(l, "echo") || strings.HasPrefix(l, "printf") {
				continue
			}
			if strings.Contains(l, "ITS_AGENT_ENROLL_TOKEN=") {
				t.Errorf("%s:%d assigns ITS_AGENT_ENROLL_TOKEN; the .deb must carry no token: %s", p, i+1, l)
			}
		}
	}
	// postinst must not start an unenrolled agent: it exits 2 on every start,
	// and Restart=always turns that into a restart loop that reads as a crash.
	postinst := read(t, "linux/debian/postinst")
	if !strings.Contains(postinst, "identity.json") || !strings.Contains(postinst, "enroll-token") {
		t.Error("postinst does not test for an enrolled/credentialled endpoint before starting the service")
	}
	// And purge must take the credentials with it, by name.
	postrm := read(t, "linux/debian/postrm")
	for _, want := range []string{"enroll-token", "agent.key"} {
		if !strings.Contains(postrm, want) {
			t.Errorf("postrm does not remove %s on purge", want)
		}
	}
}

// ---------------------------------------------------------------------------
// Managed distribution (docs/modules/agent-distribution/spec-managed-distribution.md §5)
// ---------------------------------------------------------------------------

// Falsifier 7. install.ps1 started the service whether or not the endpoint had
// anything to enrol with, and `sc.exe failure ... restart` above it turns that
// into a restart loop: the agent exits 2 on every start with neither a token
// nor an identity.
//
// That was survivable while every Windows install carried a token in the same
// breath. Managed distribution makes "installed, not yet enrolled" the NORMAL
// intermediate state, so the old behaviour would have been a crashing service
// on every machine in the domain and an event log full of it.
//
// linux/debian/postinst has always got this right and is checked for the same
// property by TestThePackageCarriesNoCredentials; this is its Windows half.
func TestWindowsInstallDoesNotStartAnUnenrolledAgent(t *testing.T) {
	text := read(t, "windows/install.ps1")
	if !strings.Contains(text, "identity.json") {
		t.Error("windows/install.ps1 does not test for an existing identity before starting the service")
	}
	if !strings.Contains(text, "enroll-token") {
		t.Error("windows/install.ps1 does not test for a token before starting the service")
	}
	// The negative that matters: no UNCONDITIONAL start. `Start-Service` must
	// be reached only through a branch, so a future edit that hoists it back to
	// the top level fails here rather than in a domain.
	//
	// INDENTATION IS THE SIGNAL, and it is a real one in this file rather than a
	// convention borrowed from elsewhere: every statement in install.ps1 sits at
	// column 0 unless it is inside a block. A `Start-Service` at column 0 is
	// therefore unconditional by construction. Crude, and it fails in the safe
	// direction -- a reformatted file goes red rather than quietly green.
	for _, line := range strings.Split(text, "\n") {
		if strings.HasPrefix(line, "Start-Service") {
			t.Errorf("windows/install.ps1 has an unguarded Start-Service (%q); an agent with no "+
				"token and no identity exits 2 on every start and the failure actions restart it forever", line)
		}
	}
	// Non-vacuity: the guarded call really is present, so the loop above is not
	// passing over a file that simply never starts the service.
	if !strings.Contains(text, "Start-Service -Name $ServiceName") {
		t.Error("windows/install.ps1 never starts the service at all; the guard above proves nothing")
	}
}

// The enrol helper is the step that carries the credential on the managed
// path, so it is held to every rule linux/its-agent-enroll is held to.
func TestTheWindowsEnrolHelperRefusesATokenOnTheCommandLine(t *testing.T) {
	text := read(t, "windows/its-agent-enroll.ps1")

	// -Token must be a DECLARED parameter that is refused, not an unknown one.
	// PowerShell's own error for an undeclared parameter teaches nothing and
	// sends the operator looking for a different spelling.
	if !strings.Contains(text, "$Token") {
		t.Error("windows/its-agent-enroll.ps1 does not declare -Token at all; an operator who tries " +
			"it gets PowerShell's unknown-parameter error instead of the reason")
	}
	if !strings.Contains(text, "if ($Token) {") || !strings.Contains(text, "throw") {
		t.Error("windows/its-agent-enroll.ps1 declares -Token without refusing it")
	}
	// And the two ways it IS accepted.
	if !strings.Contains(text, "ITS_AGENT_ENROLL_TOKEN") {
		t.Error("windows/its-agent-enroll.ps1 does not read the token from the environment")
	}
	if !strings.Contains(text, "TokenFromStdin") {
		t.Error("windows/its-agent-enroll.ps1 offers no stdin path; that is the form a pipeline needs")
	}
	// The token goes to a file with the directory ACL applied, never to the
	// service Environment value -- which anyone who can read the service key
	// can read, and which outlives the token by months.
	if !strings.Contains(text, "enroll-token") {
		t.Error("windows/its-agent-enroll.ps1 does not write the token file")
	}
	for _, line := range strings.Split(text, "\n") {
		l := strings.TrimSpace(line)
		if strings.HasPrefix(l, "#") || strings.HasPrefix(l, "Write-Host") {
			continue
		}
		if strings.Contains(l, "ITS_AGENT_ENROLL_TOKEN=") {
			t.Errorf("windows/its-agent-enroll.ps1:%q puts the token in an environment block", l)
		}
	}
	// Re-running at every boot is what a GPO startup script DOES, so enrolling
	// twice must be a no-op rather than a second device record.
	if !strings.Contains(text, "identity.json") {
		t.Error("windows/its-agent-enroll.ps1 does not short-circuit on an already-enrolled endpoint; " +
			"a GPO startup script runs at every boot")
	}
}

// ---------------------------------------------------------------------------
// The two paths nobody has ever executed
// (docs/modules/agent-distribution/spec.md §3, first-run-verification.md)
//
// Everything below is what a reviewer can check by READING, turned into
// something CI re-checks. It is not a substitute for running these scripts on
// a real Windows host and a real Mac -- that is
// docs/modules/agent-distribution/first-run-verification.md, and it is the
// only thing that retires the "never executed" marker. What these do is stop
// the specific defects found by reading on 2026-08-26 from coming back.
// ---------------------------------------------------------------------------

// powershellScripts is every .ps1 under packaging/.
func powershellScripts(t *testing.T) []string {
	t.Helper()
	var out []string
	err := filepath.WalkDir(".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() && strings.HasSuffix(p, ".ps1") {
			out = append(out, p)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(out) < 3 {
		t.Fatalf("only %d PowerShell scripts found; this test would pass close to vacuously", len(out))
	}
	return out
}

// stripPowerShellLiterals removes block comments, line comments, quoted strings
// and here-strings, leaving the code skeleton.
//
// A PARSER THIS IS NOT, and CI has no PowerShell to be one with. It exists so
// the bracket check below counts brackets in CODE rather than brackets inside
// the refusal message, and so a `#` inside a path string does not swallow the
// rest of a line. Every construct it handles is one these three files actually
// use; anything it gets wrong fails in the loud direction, which is the right
// direction for a check nobody can run the real parser against.
func stripPowerShellLiterals(src string) string {
	var b strings.Builder
	r := []rune(src)
	for i := 0; i < len(r); i++ {
		switch {
		// <# block comment #>
		case r[i] == '<' && i+1 < len(r) && r[i+1] == '#':
			for i+1 < len(r) && !(r[i] == '#' && r[i+1] == '>') {
				i++
			}
			i++
		// here-strings: @' ... '@ and @" ... "@ , terminator at line start.
		case r[i] == '@' && i+1 < len(r) && (r[i+1] == '\'' || r[i+1] == '"'):
			q := r[i+1]
			i += 2
			for i < len(r) {
				if r[i] == q && i+1 < len(r) && r[i+1] == '@' &&
					(i == 0 || r[i-1] == '\n') {
					i++
					break
				}
				if r[i] == '\n' {
					b.WriteRune('\n')
				}
				i++
			}
		case r[i] == '\'':
			i++
			for i < len(r) {
				if r[i] == '\'' {
					if i+1 < len(r) && r[i+1] == '\'' { // '' is an escaped quote
						i += 2
						continue
					}
					break
				}
				if r[i] == '\n' {
					b.WriteRune('\n')
				}
				i++
			}
		case r[i] == '"':
			i++
			for i < len(r) {
				if r[i] == '`' { // backtick escapes the next rune
					i += 2
					continue
				}
				if r[i] == '"' {
					break
				}
				if r[i] == '\n' {
					b.WriteRune('\n')
				}
				i++
			}
		case r[i] == '#':
			for i < len(r) && r[i] != '\n' {
				i++
			}
			b.WriteRune('\n')
		default:
			b.WriteRune(r[i])
		}
	}
	return b.String()
}

// The nearest thing to `powershell -Command { ... }` parsing that a Linux CI
// can do: every brace, bracket and parenthesis in the code (not in a string,
// not in a comment) closes, in order.
//
// This is the check that would have caught the class of defect a script nobody
// has ever run is most likely to hold -- an unterminated block that fails at
// PARSE time, on the endpoint, after the file is already on a share.
func TestPowerShellScriptsBalanceTheirBrackets(t *testing.T) {
	pairs := map[rune]rune{')': '(', ']': '[', '}': '{'}
	for _, p := range powershellScripts(t) {
		code := stripPowerShellLiterals(read(t, p))
		var stack []rune
		line := 1
		for _, c := range code {
			switch c {
			case '\n':
				line++
			case '(', '[', '{':
				stack = append(stack, c)
			case ')', ']', '}':
				if len(stack) == 0 {
					t.Errorf("%s:%d: closing %q with nothing open", p, line, string(c))
					continue
				}
				if got := stack[len(stack)-1]; got != pairs[c] {
					t.Errorf("%s:%d: %q closes %q", p, line, string(c), string(got))
				}
				stack = stack[:len(stack)-1]
			}
		}
		if len(stack) != 0 {
			t.Errorf("%s: %d bracket(s) never closed (innermost %q); PowerShell would refuse to parse this file",
				p, len(stack), string(stack[len(stack)-1]))
		}
	}
	// Non-vacuity: the stripper must not be eating the whole file.
	if code := stripPowerShellLiterals(read(t, "windows/install.ps1")); !strings.Contains(code, "New-Service") {
		t.Error("stripPowerShellLiterals removed the code as well as the strings; the balance check above proves nothing")
	}
}

// Every .ps1 fails loudly and refuses uninitialised variables. Without both, a
// mistyped variable is an empty string and a failed step is a step that
// silently did nothing -- on a script that installs a service as SYSTEM.
func TestPowerShellScriptsAreStrictAndFailLoudly(t *testing.T) {
	for _, p := range powershellScripts(t) {
		text := read(t, p)
		if !strings.Contains(text, "$ErrorActionPreference = 'Stop'") {
			t.Errorf("%s: no `$ErrorActionPreference = 'Stop'`; a half-applied install is worse than a failed one", p)
		}
		if !strings.Contains(text, "Set-StrictMode -Version Latest") {
			t.Errorf("%s: no `Set-StrictMode`", p)
		}
		if strings.Contains(text, "Start-Transcript") {
			t.Errorf("%s: Start-Transcript would capture the enrollment token", p)
		}
	}
}

// Cmdlets and parameters that do not exist on every shell these scripts will
// meet. Windows Server ships Windows PowerShell 5.1; a modern admin workstation
// and an Intune script may be PowerShell 7. A script that works on one and not
// the other is a script that fails on the half of the fleet nobody tested.
func TestPowerShellScriptsAvoidShellSpecificCmdlets(t *testing.T) {
	banned := map[string]string{
		// Never ported to PowerShell Core; `Get-EventLog` is 5.1-only. It was
		// also advice that could not work here at all -- nothing in this
		// packaging registers an event source, so the command answered "The
		// source name does not exist" on 5.1 too.
		"Get-EventLog": "removed in PowerShell 6+; and nothing here registers an event source, so it never worked",
		// `-AsPlainText` on ConvertFrom-SecureString arrived in PowerShell 7.0.
		// On 5.1 the same line SUCCEEDS and returns a DPAPI-encrypted blob, so
		// the documented example put an encrypted string in
		// ITS_AGENT_ENROLL_TOKEN and the control plane answered a bare 403 that
		// reads exactly like a revoked token. Silent and wrong is worse than
		// loud and wrong.
		"ConvertFrom-SecureString": "-AsPlainText is PowerShell 7.0+; on 5.1 this silently yields a DPAPI blob, not the token",
	}
	for _, p := range powershellScripts(t) {
		for i, line := range strings.Split(read(t, p), "\n") {
			for cmdlet, why := range banned {
				if strings.Contains(line, cmdlet) {
					t.Errorf("%s:%d uses %s: %s", p, i+1, cmdlet, why)
				}
			}
		}
	}
}

// Every install/enrol entry point refuses the token as an argument AND says
// why. windows/install.ps1 was the one that did not: -Token was simply not
// declared, so PowerShell answered "A parameter cannot be found that matches
// parameter name 'Token'" -- which sends the operator looking for a different
// spelling rather than for stdin, and leaves the token in the history file
// either way.
func TestEveryEntryPointRefusesATokenOnTheCommandLine(t *testing.T) {
	for _, p := range []string{"linux/install.sh", "darwin/install.sh", "linux/its-agent-enroll"} {
		text := read(t, p)
		if !strings.Contains(text, "--token|--token=*)") {
			t.Errorf("%s has no `--token` case in its argument loop; an operator who tries it "+
				"deserves the explanation, not a silent unknown-option error", p)
		}
		if !strings.Contains(text, "ITS_AGENT_ENROLL_TOKEN") {
			t.Errorf("%s does not read the token from the environment", p)
		}
	}
	for _, p := range []string{"windows/install.ps1", "windows/its-agent-enroll.ps1"} {
		text := read(t, p)
		if !strings.Contains(text, "[string]$Token") {
			t.Errorf("%s does not DECLARE -Token, so PowerShell answers with its own "+
				"unknown-parameter error instead of the reason", p)
		}
		if !strings.Contains(text, "if ($Token) {") || !strings.Contains(text, "throw") {
			t.Errorf("%s declares -Token without refusing it", p)
		}
		if !strings.Contains(text, "ITS_AGENT_ENROLL_TOKEN") {
			t.Errorf("%s does not read the token from the environment", p)
		}
		if !strings.Contains(text, "TokenFromStdin") {
			t.Errorf("%s offers no stdin path; that is the form a pipeline needs", p)
		}
	}
}

// ORDER, not presence. The previous version of its-agent-enroll.ps1 wrote the
// enrollment token to disk and THEN checked whether the service existed, so on
// a machine where install.ps1 had never succeeded -- exactly the machine a GPO
// startup script meets, since the script runs at every boot including the ones
// after a failed install -- the deployment credential was left on disk with
// nothing to spend it and nothing to remove it. On a fleet that is the token on
// every machine the install missed.
func TestTheWindowsEnrolHelperChecksTheServiceBeforeWritingTheToken(t *testing.T) {
	text := read(t, "windows/its-agent-enroll.ps1")
	check := strings.Index(text, "if (-not (Test-Path $serviceKey))")
	// The token lands where the file is created (review I4: CreateNew, never
	// WriteAllText into a file that may already exist).
	write := strings.Index(text, "[System.IO.File]::Open($tokenFile, [System.IO.FileMode]::CreateNew")
	if check < 0 {
		t.Fatal("windows/its-agent-enroll.ps1 no longer checks that the service exists")
	}
	if write < 0 {
		t.Fatal("windows/its-agent-enroll.ps1 no longer writes the token file")
	}
	if check > write {
		t.Error("windows/its-agent-enroll.ps1 writes the enrollment token BEFORE it checks that " +
			"the service is installed; a machine where install.ps1 never ran is left holding " +
			"a fleet credential it can never spend")
	}
	// The ACL must also be applied before the token lands, or the token is
	// briefly readable through whatever ACL the directory inherited.
	if acl := strings.Index(text, "Set-Acl -Path $DataDir"); acl < 0 || acl > write {
		t.Error("windows/its-agent-enroll.ps1 writes the token before restricting the directory ACL")
	}
}

// Windows holds an open section on a running executable's image file, so
// Copy-Item over its-agent.exe while the service runs fails with "The process
// cannot access the file because it is being used by another process". The
// previous version copied first and stopped second, which made every
// upgrade-in-place fail -- and never a first install, which is why reading the
// script is the only way this was going to be found.
func TestTheWindowsInstallerStopsTheServiceBeforeReplacingTheBinary(t *testing.T) {
	text := read(t, "windows/install.ps1")
	stop := strings.Index(text, "Stop-Service -Name $ServiceName -Force")
	copyIdx := strings.Index(text, "Copy-Item -Path $Binary -Destination $target")
	if stop < 0 || copyIdx < 0 {
		t.Fatalf("windows/install.ps1 no longer stops the service (%d) or copies the binary (%d)", stop, copyIdx)
	}
	if stop > copyIdx {
		t.Error("windows/install.ps1 replaces its-agent.exe before stopping the service; " +
			"a re-install over a running agent fails on a locked image file")
	}
}

// The macOS half of falsifier 7 (spec-managed-distribution.md §6), which was
// fixed for Windows and left standing for the Mac. `KeepAlive` in the plist is
// unconditional and the agent exits 2 with neither identity nor token, so
// bootstrapping an unenrolled daemon is a respawn every ThrottleInterval
// seconds, forever. ThrottleInterval bounds the RATE of that loop; it does not
// end it.
func TestTheMacInstallerDoesNotBootstrapAnUnenrolledAgent(t *testing.T) {
	text := read(t, "darwin/install.sh")
	if !strings.Contains(text, "identity.json") || !strings.Contains(text, "enroll-token") {
		t.Error("darwin/install.sh does not test for an identity or a token before bootstrapping; " +
			"an agent with neither exits 2 on every start and KeepAlive respawns it forever")
	}
	guard := strings.Index(text, `[ ! -f "${STATEDIR}/identity.json" ]`)
	boot := strings.Index(text, "launchctl bootstrap system")
	if guard < 0 || boot < 0 || guard > boot {
		t.Error("darwin/install.sh's unenrolled guard does not precede `launchctl bootstrap`")
	}
	// `launchctl enable` must come FIRST: a label in launchd's disabled
	// database makes bootstrap fail, and under `set -e` an enable placed after
	// it never runs -- so the install can never recover from one earlier
	// `launchctl disable`.
	enable := strings.Index(text, `launchctl enable "system/${LABEL}"`)
	if enable < 0 {
		t.Fatal("darwin/install.sh never enables the launchd label")
	}
	if enable > boot {
		t.Error("darwin/install.sh runs `launchctl enable` AFTER `launchctl bootstrap`; " +
			"if the label was ever disabled, bootstrap fails and set -e means enable never runs")
	}
}

// keyBearingFiles are the data-directory files that hold PRIVATE KEY material
// or a live credential, named exactly as agent/internal/config declares them.
//
// `agent.key.pending` and `previous.json` were missing from every uninstaller's
// by-name list. `previous.json` carries {cert, chain, key} for the identity a
// rotation replaced (internal/enroll/rotate.go) and `agent.key.pending` is the
// key a pending CSR was built over -- so a purge whose recursive remove failed
// left device private keys behind on all three platforms.
var keyBearingFiles = []string{"enroll-token", "agent.key", "agent.key.pending", "previous.json"}

func TestEveryUninstallerDestroysEveryCredentialByName(t *testing.T) {
	// Anchored to the agent's own constants, so renaming one there fails here
	// rather than silently orphaning a private key on every endpoint.
	cfg, err := os.ReadFile("../internal/config/config.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range keyBearingFiles {
		if !strings.Contains(string(cfg), `"`+f+`"`) {
			t.Errorf("internal/config no longer declares %q; this list is stale", f)
		}
	}
	for _, p := range []string{"linux/debian/postrm", "darwin/uninstall.sh", "windows/uninstall.ps1"} {
		text := read(t, p)
		for _, f := range keyBearingFiles {
			if !strings.Contains(text, f) {
				t.Errorf("%s does not remove %s by name; a failed recursive remove leaves "+
					"device private key material on the endpoint", p, f)
			}
		}
	}
}
