package patch

import (
	"context"
	"fmt"
	"os"
	"strings"
)

// The apt engine.
//
// This file has NO build tag on purpose: it compiles everywhere and is
// SELECTED only on linux (engine_linux.go). That is what makes it the one
// engine in this package whose parsing, filtering, error classification and
// install path are all exercised against REAL CAPTURED OUTPUT in this
// repository's CI -- see apt_test.go and testdata/apt-get-s-upgrade.txt, which
// is a verbatim capture from an Ubuntu 24.04 host, not an invention.
//
// docs/modules/patch/spec.md §10 names `apt`/`dnf`/`zypper` for Linux. This
// build implements APT ONLY, and a Linux host without apt refuses rather than
// pretending (agent-capabilities.md §2). The fleet is Windows 11 and macOS; the
// Linux path exists because it is the one that can actually be executed and
// therefore the one that keeps the shared logic honest.

const sourceAPT = "apt"

// aptPath is the narrowest binary that can do this work. Not `apt`, which is
// explicitly documented as having an unstable interface for scripts, and
// certainly not a shell.
const aptGet = "apt-get"

// rebootRequiredFile is Debian/Ubuntu's own signal, written by the kernel and
// libc packages' postinst scripts. Asking the OS beats guessing from what was
// installed.
const rebootRequiredFile = "/var/run/reboot-required"

type aptEngine struct {
	run runner
	// statFile is injected so the reboot-required probe is testable without
	// writing to /var/run.
	statFile func(string) error
}

func newAPTEngine() *aptEngine {
	return &aptEngine{
		run:      execRunner,
		statFile: func(p string) error { _, err := os.Stat(p); return err },
	}
}

func (e *aptEngine) Family() string { return "linux" }
func (e *aptEngine) Close()         {}

// Available enumerates what apt would upgrade.
//
// `-s` is a SIMULATION: it changes nothing, takes no dpkg lock for writing and
// is safe to run on a machine somebody is using. `Debug::NoLocking=1` lets it
// run alongside an unattended-upgrades pass instead of failing on the lock,
// which on a fleet is the difference between a scan that works and one that
// fails every night at the same time.
//
// `upgrade` and not `dist-upgrade`: `upgrade` never installs a new package and
// never removes one, so every line it produces is genuinely an update to
// something already present. `dist-upgrade` can remove packages to satisfy
// dependencies, and a patch scan that reported a removal as an available patch
// would be inviting an operator to approve it.
func (e *aptEngine) Available(ctx context.Context) ([]entry, error) {
	// Refresh the index first, best-effort. A scan against a week-old index
	// reports a week-old answer, and the control plane cannot tell the
	// difference. Failure is NOT fatal: a machine with no network right now
	// still has a package index, and reporting what that index knows beats
	// reporting nothing.
	if r := e.run(ctx, nil, aptGet, "update", "-qq"); !r.ok() {
		// Deliberately not returned. See above.
		_ = r
	}

	res := e.run(ctx, nil, aptGet, "-s", "-o", "Debug::NoLocking=1", "upgrade")
	if !res.ok() {
		// A plain error -> the job is `failed`, not `refused`. apt failing here
		// is transient by nature: a held lock, a broken half-configured
		// package, an unreachable mirror. None of those are "this agent will
		// never do this".
		return nil, fmt.Errorf("apt-get -s upgrade exited %d: %s",
			res.ExitCode, sanitizeError(firstNonEmpty(res.Stderr, res.Stdout)))
	}
	return parseAptSimulation(res.Stdout), nil
}

// Installed is EMPTY for apt, and that is a decision rather than an omission.
//
// The apt-equivalent of "what is already installed" is the entire dpkg
// database -- 500 to 2000 packages on an ordinary machine. ingest.py resolves
// every installed identifier that is not also in `patches` with its OWN SELECT
// (ingest.py's `unresolved_keys` loop), so an agent that reported that database
// would cost the control plane one query per package per device per scan. On a
// thousand-device fleet scanning daily that is millions of queries to learn
// almost nothing.
//
// The cost of the omission is bounded and known: a patch this catalog has seen
// before, which some OTHER mechanism (unattended-upgrades, a human with a
// terminal) installed between two scans, reconciles to `not_applicable` instead
// of `installed`. Both mean "not outstanding on this device". Windows, where
// the set is tens of KB articles rather than the whole package database,
// reports it properly.
func (e *aptEngine) Installed(ctx context.Context) ([]string, error) { return nil, nil }

// Install upgrades one package to the exact version `e` names.
//
// EVERY ARGV ELEMENT IS EITHER A CONSTANT OR SOMETHING APT ITSELF PRINTED.
// `entry` reached here from parseAptSimulation, i.e. from apt-get's own stdout;
// the signed job only ever selected it by an equality test on the key. There is
// no path from a job-supplied string to this argv, and apt_test.go asserts that
// by driving a job whose identifier contains shell metacharacters and checking
// that NO command ran.
func (e *aptEngine) Install(ctx context.Context, ent entry) error {
	name, version, ok := splitAptIdentifier(ent.Identifier)
	if !ok {
		return fmt.Errorf("the identifier %q is not the pkg@version form this engine produces",
			clamp(ent.Identifier, 80))
	}

	res := e.run(ctx,
		aptInstallEnv,
		aptGet,
		"-y",
		"--only-upgrade",
		// Keep the administrator's edited config files. An unattended patch run
		// that silently replaced /etc/ssh/sshd_config would be a worse outcome
		// than an unpatched package, and dpkg's default when it cannot ask is
		// to keep the old file anyway -- stating it makes that independent of
		// how the daemon was configured.
		"-o", "Dpkg::Options::=--force-confold",
		"-o", "Dpkg::Options::=--force-confdef",
		"install",
		name+"="+version,
	)
	if !res.ok() {
		return fmt.Errorf("apt-get install exited %d: %s",
			res.ExitCode, sanitizeError(firstNonEmpty(res.Stderr, res.Stdout)))
	}
	return nil
}

// aptInstallEnv is set for the install and NOT for the scan.
//
// DEBIAN_FRONTEND=noninteractive is what stops debconf opening a curses dialog
// on a machine with no terminal and waiting there until the job's deadline.
var aptInstallEnv = []string{"DEBIAN_FRONTEND=noninteractive"}

func (e *aptEngine) RebootRequired(ctx context.Context) bool {
	return e.statFile(rebootRequiredFile) == nil
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

// parseAptSimulation reads `apt-get -s upgrade`'s plan.
//
// The line this cares about, captured verbatim from Ubuntu 24.04:
//
//	Inst coreutils [9.4-3ubuntu6.1] (9.4-3ubuntu6.2 Ubuntu:24.04/noble-updates [amd64])
//
// Shape: `Inst <name> [<installed-version>] (<candidate> <origins> [<arch>])`.
// The `[<installed-version>]` group is absent when apt is installing something
// new rather than upgrading it -- which `upgrade` does not do, but `Conf` lines
// and a future `dist-upgrade` would, so the parser tolerates it.
//
// Everything else in the output -- `Conf` lines, the counts, "Reading package
// lists..." -- is ignored. Ignoring rather than failing matters: apt's prose
// is localised and its wording changes between releases, so a parser that
// depended on any of it would break on a machine with LANG set.
func parseAptSimulation(stdout string) []entry {
	var out []entry
	for _, line := range strings.Split(stdout, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "Inst ") {
			continue
		}
		if e, ok := parseAptInstLine(line); ok {
			out = append(out, e)
		}
	}
	return out
}

func parseAptInstLine(line string) (entry, bool) {
	rest := strings.TrimSpace(strings.TrimPrefix(line, "Inst "))

	name, rest, ok := cutField(rest)
	if !ok || name == "" {
		return entry{}, false
	}

	// The optional `[installed-version]` group.
	rest = strings.TrimSpace(rest)
	if strings.HasPrefix(rest, "[") {
		if idx := strings.Index(rest, "]"); idx >= 0 {
			rest = strings.TrimSpace(rest[idx+1:])
		}
	}

	// The `(candidate origins [arch])` group.
	open := strings.Index(rest, "(")
	closeIdx := strings.LastIndex(rest, ")")
	if open < 0 || closeIdx < open {
		return entry{}, false
	}
	inner := rest[open+1 : closeIdx]

	version, origins, _ := cutField(strings.TrimSpace(inner))
	if version == "" {
		return entry{}, false
	}
	// Drop the trailing `[arch]` so it is not mistaken for an origin.
	if idx := strings.LastIndex(origins, "["); idx >= 0 {
		origins = origins[:idx]
	}
	origins = strings.TrimSpace(origins)

	return entry{
		Source: sourceAPT,
		// `pkg@version` is spec §4's stated identifier form for a package
		// source. The version is what makes it reconcile: once installed, this
		// exact string stops being offered, and a later scan of the same
		// machine cannot report it as outstanding again.
		Identifier: name + "@" + version,
		Title:      name + " " + version,
		Severity:   aptSeverity(origins),
	}, true
}

// aptSeverity is the only severity apt can honestly support.
//
// Debian and Ubuntu do NOT publish a per-package severity in the package
// index; what they publish is the archive a version came from. A version from
// `noble-security` (or Debian's `Debian-Security`) is by definition a security
// update, and everything else is a bug-fix or feature update whose severity
// this agent does not know.
//
// `important` and not `critical` for the security case: `critical` is a
// judgement about exploitability that the archive does not make, and PD-02's
// auto-approve rule treats both the same way, so overstating buys nothing and
// would mislead the severity breakdown in the compliance report. Everything
// else is `unknown`, which is a true statement -- Sentinel's `priority` column
// is the seam where a real severity feed lands later (spec §4).
func aptSeverity(origins string) string {
	if strings.Contains(strings.ToLower(origins), "security") {
		return "important"
	}
	return "unknown"
}

// splitAptIdentifier reverses the `pkg@version` construction. The LAST `@` is
// the separator: a Debian package name cannot contain `@`, but an epoch-bearing
// version can contain almost anything else.
func splitAptIdentifier(id string) (name, version string, ok bool) {
	idx := strings.LastIndex(id, "@")
	if idx <= 0 || idx == len(id)-1 {
		return "", "", false
	}
	return id[:idx], id[idx+1:], true
}

// cutField splits off the first whitespace-delimited field.
func cutField(s string) (field, rest string, ok bool) {
	s = strings.TrimSpace(s)
	if s == "" {
		return "", "", false
	}
	if idx := strings.IndexAny(s, " \t"); idx >= 0 {
		return s[:idx], strings.TrimSpace(s[idx+1:]), true
	}
	return s, "", true
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}
