package deploy

import (
	"strings"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// JOB-SUPPLIED STRINGS THAT BECOME ARGV ELEMENTS, AND THE ONLY WAY THEY MAY.
//
// `install_args` is a single free-form string in AppDeployParams, and the
// manager package identifier is read out of `detection_rule`. Both arrive
// inside the signed envelope, so they are AUTHORISED — but authorised is not
// the same as safe to hand to a Windows command-line parser, and the whole
// point of this file is that neither is ever concatenated into one.
//
// Two rules:
//
//  1. Splitting happens HERE, on whitespace, producing argv ELEMENTS. os/exec
//     hands those to the kernel (POSIX) or quotes them per Windows' own
//     CommandLineToArgvW rules (Windows). Nothing this package writes ever
//     builds a command line.
//  2. Every token must match a conservative allowlist. On POSIX that is
//     belt-and-braces — there is no shell to interpret a metacharacter. On
//     Windows it is not: msiexec, winget and choco each re-parse their own
//     arguments, cmd-style escapes (`^`, `!`, `%`) survive some of those
//     paths, and an argument that starts a nested quoting context is a real
//     class of bug. A token outside the allowlist is REFUSED, with no attempt
//     to escape or repair it — a repaired argument is an argument nobody
//     reviewed.
//
// The cost is stated plainly: an operator cannot pass a quoted path with a
// space (`TRANSFORMS="C:\Program Files\x.mst"`). That is a real limitation and
// it is recorded in docs/modules/app-deploy/agent-capability.md rather than
// worked around with a quoting parser this build cannot test against a real
// Windows argument parser.

const (
	// maxArgTokens bounds how many arguments a deployment may carry. Real
	// silent-install argument lists are a handful of flags.
	maxArgTokens = 32
	// maxArgLen bounds one token. A Windows path is comfortably under this.
	maxArgLen = 256
	// maxPackageID bounds a manager package identifier. `Mozilla.Firefox`,
	// `googlechrome`, `Microsoft.VisualStudioCode` — all far under this.
	maxPackageID = 128
)

// argAllowed reports whether a rune may appear in an argv element this agent
// passes to an installer.
//
// Deliberately NOT a denylist. A denylist of shell metacharacters is a claim
// about which characters are dangerous in every parser an argument might pass
// through, and neither this author nor this CI container can check that claim
// against msiexec, winget, choco and installer(8). An allowlist is a claim
// about which characters are NEEDED, which is checkable.
//
// What is permitted, and why each is needed:
//
//	A-Z a-z 0-9   identifiers, flags, MSI property values
//	. _ -         `--no-progress`, `-y`, `file.msi`, `MY_PROP`
//	/ \           `/qn`, `C:\ProgramData\log.txt`
//	: =           `C:`, `REBOOT=ReallySuppress`
//	+ , @ #       version strings, list separators, package pinning
//	{ }           MSI product codes: `{90160000-...}`
//
// What is NOT permitted, and each is a specific attack rather than a category:
// space and tab (a token is one argument, and a token containing a separator
// is two arguments wearing one's clothes); the double quote and the single
// quote (they open a quoting context the next parser resolves differently from
// this one); the backtick and the dollar sign (PowerShell expansion, and choco
// runs its arguments through PowerShell);
// `& | ; < > ( )` (cmd operators); `^` and `!` (cmd escape and delayed
// expansion, which survive several layers of re-parsing); `%` (cmd variable
// expansion); `*` and `?` (globs the callee expands); `~` (short-name paths);
// anything non-ASCII or non-printable.
func argAllowed(r rune) bool {
	switch {
	case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
		return true
	}
	switch r {
	case '.', '_', '-', '/', '\\', ':', '=', '+', ',', '@', '#', '{', '}':
		return true
	}
	return false
}

// splitArgs turns `install_args` into validated argv elements.
//
// A nil or empty string yields no arguments and no error: most packages have
// none. Any violation is a REFUSAL — a params document this agent will not act
// on is not a transient condition, and the reason is the agent's own sentence
// with NOTHING job-supplied echoed into it. That last point is not decoration:
// the refusal reason travels into the server's hash-chained audit log, and
// `install_args` is exactly the kind of field an operator puts a credential
// in.
func splitArgs(raw *string) ([]string, error) {
	if raw == nil || strings.TrimSpace(*raw) == "" {
		return nil, nil
	}
	fields := strings.Fields(*raw)
	if len(fields) > maxArgTokens {
		return nil, jobs.Refuse("app.deploy: install_args carries more than %d arguments; "+
			"nothing was executed", maxArgTokens)
	}
	out := make([]string, 0, len(fields))
	for i, f := range fields {
		if len(f) > maxArgLen {
			return nil, jobs.Refuse("app.deploy: install_args argument %d is longer than %d "+
				"characters; nothing was executed", i+1, maxArgLen)
		}
		for _, r := range f {
			if !argAllowed(r) {
				// The POSITION is named and the CONTENT is not. An operator can
				// find argument 3 in the console they typed it into; an audit
				// log that quoted it would be an audit log that leaks whatever
				// they put there.
				return nil, jobs.Refuse("app.deploy: install_args argument %d contains a "+
					"character this agent will not pass to an installer. Permitted: letters, "+
					"digits and . _ - / \\ : = + , @ # { }. Nothing was executed", i+1)
			}
		}
		out = append(out, f)
	}
	return out, nil
}

// validPackageID checks a manager package identifier against the same
// allowlist, plus one extra rule: it may not START with `-` or `/`.
//
// That rule is the whole point. `winget install --id --uninstall` is not a
// package named `--uninstall`; it is a flag. An identifier that can become a
// flag is an identifier that can change the verb, and the allowlist alone does
// not stop it because `-` is a legitimate character INSIDE a package id.
func validPackageID(id string) bool {
	if id == "" || len(id) > maxPackageID {
		return false
	}
	if id[0] == '-' || id[0] == '/' {
		return false
	}
	for _, r := range id {
		if !argAllowed(r) {
			return false
		}
	}
	return true
}
