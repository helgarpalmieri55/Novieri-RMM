package patch

import (
	"context"
	"fmt"
	"strings"
)

// The macOS engine.
//
// No build tag, for the same reason apt.go has none: the file compiles
// everywhere and is SELECTED only on darwin (engine_darwin.go), so its parsing
// and its outcome mapping run in this repository's CI on Linux. What has never
// executed anywhere is `softwareupdate` itself -- see
// docs/modules/patch/agent-capabilities.md §6, which lists that plainly, and
// note that the fixture in testdata/softwareupdate-list-RECONSTRUCTED.txt is RECONSTRUCTED
// FROM THE DOCUMENTED FORMAT rather than captured from a Mac.
//
// # Why softwareupdate and not something quieter
//
// `/usr/sbin/softwareupdate` is the narrowest binary that can enumerate and
// install Apple's updates. The alternative is the private SoftwareUpdate
// framework, which has no stable ABI. Nothing here goes near a shell, and
// nothing here goes near `osascript` -- a SYSTEM process driving AppleScript is
// the macOS equivalent of spawning PowerShell.
//
// # PD-05: best-effort, and the limit is stated
//
// spec.md's PD-05 says "best-effort `softwareupdate` for scan + apply where
// [it] is scriptable". The place it stops being scriptable is a macOS OS update
// on Apple Silicon, which requires a volume-owner's credentials
// (`--user`/`--stdinpass`). This agent has NO stored credentials to offer, and
// inventing a path that did would collide with invariant #3 -- credentials live
// encrypted in the vault and are decrypted at point of use, which is a
// server-side facility with no agent-side equivalent today. Such an update
// fails with Apple's own authorization message in `last_error`, which is the
// honest outcome.

const sourceSoftwareUpdate = "softwareupdate"

// softwareUpdate is the absolute path. macOS puts it in /usr/sbin and there is
// no reason to consult PATH for it.
const softwareUpdate = "/usr/sbin/softwareupdate"

type macEngine struct {
	run runner

	// restartOnInstall records, per `<source>:<identifier>`, whether Apple
	// flagged this update `Action: restart`. It is built by Available and read
	// by Install, and it exists because PatchApplyResult has ONE reboot boolean
	// and no per-patch equivalent -- so the engine has to remember.
	restartOnInstall map[string]bool
	// restartPending is set when an update flagged for restart was installed,
	// or when softwareupdate said so in its own words.
	restartPending bool
}

//lint:ignore U1000 consumed by engine_darwin.go; staticcheck runs at GOOS=linux
func newMacEngine() *macEngine {
	return &macEngine{run: execRunner, restartOnInstall: map[string]bool{}}
}

func (e *macEngine) Family() string { return "macos" }
func (e *macEngine) Close()         {}

// Available lists what Apple is offering.
//
// `--list` alone. NOT `--list --all-installers` and NOT `--fetch-full-installer`:
// those enumerate full macOS installers, which are 12 GB downloads and a major
// OS upgrade rather than a patch. Offering an operator a major-version upgrade
// in the same approval UI as a security fix is how a fleet gets upgraded by
// accident.
func (e *macEngine) Available(ctx context.Context) ([]entry, error) {
	res := e.run(ctx, nil, softwareUpdate, "--list")
	if !res.ok() {
		return nil, fmt.Errorf("softwareupdate --list exited %d: %s",
			res.ExitCode, sanitizeError(firstNonEmpty(res.Stderr, res.Stdout)))
	}
	// softwareupdate writes its listing to STDERR on several macOS releases and
	// to stdout on others. Reading both is not sloppiness: it is the difference
	// between an engine that works on Ventura and one that reports every Mac in
	// the estate as fully patched, which is the most dangerous wrong answer
	// this capability can give.
	entries := parseSoftwareUpdateList(res.Stdout + "\n" + res.Stderr)

	e.restartOnInstall = make(map[string]bool, len(entries))
	out := make([]entry, 0, len(entries))
	for _, se := range entries {
		e.restartOnInstall[se.entry.key()] = se.restart
		out = append(out, se.entry)
	}
	return out, nil
}

// Installed is EMPTY for macOS, and the reason is narrower than apt's.
//
// `softwareupdate --history` exists, but its output is a fixed-width THREE
// COLUMN table of display names, versions and localised dates with no stable
// machine-readable form and no relationship to the `--list` labels this engine
// uses as identifiers. Parsing it would produce identifiers that never match
// anything the catalog holds, so every one of them would cost the control plane
// a SELECT that finds nothing (ingest.py's `unresolved_keys` loop).
//
// A parser written against a format nobody here has ever seen is worse than an
// honest empty list: it would look like coverage. See agent-capabilities.md §3.
func (e *macEngine) Installed(ctx context.Context) ([]string, error) { return nil, nil }

// Install applies one label.
//
// THE LABEL IS APPLE'S, NOT THE JOB'S. `ent` came out of Available, i.e. out of
// softwareupdate's own listing; the signed job selected it by an equality test
// on `<source>:<identifier>` and its string never travels further. A label
// contains spaces ("macOS Sequoia 15.6.1-24G90") and that is harmless here
// because it is ONE argv element and there is no shell to split it.
func (e *macEngine) Install(ctx context.Context, ent entry) error {
	// THREE ARGV ELEMENTS, AND THE ABSENCES ARE DELIBERATE.
	//
	// There is NO `--restart`/`-R`. Not restarting is softwareupdate's DEFAULT,
	// and opting in is what `-R` is for -- so this build reports
	// `reboot_required` and leaves the decision to the control plane by simply
	// not asking for one. An earlier draft passed a `--no-restart` flag to say
	// that explicitly; softwareupdate has no such flag, and since nothing here
	// can be executed on a Mac, that invented flag would have made EVERY macOS
	// install fail with a usage error and nothing in CI would have shown it.
	// Expressing the intent by omission is the version that cannot be wrong.
	//
	// There is no `--no-scan` either: the listing that produced this label was
	// taken moments ago in the same job, but letting softwareupdate scan is
	// what makes it resolve the label against its current catalog rather than a
	// cached one.
	res := e.run(ctx, nil,
		softwareUpdate,
		"--install",
		ent.Identifier,
	)
	if !res.ok() {
		return fmt.Errorf("softwareupdate --install exited %d: %s",
			res.ExitCode, sanitizeError(firstNonEmpty(res.Stderr, res.Stdout)))
	}
	if e.restartOnInstall[ent.key()] || mentionsRestart(res.Stdout+" "+res.Stderr) {
		e.restartPending = true
	}
	return nil
}

// RebootRequired reports what this job's own installs asked for.
//
// macOS has no `/var/run/reboot-required` equivalent to consult, so the only
// truthful source is the `Action: restart` flag Apple published for the updates
// this job actually installed, plus softwareupdate's own closing sentence.
// A Mac that needs a restart for some UNRELATED reason is not detected, and
// this understates rather than invents.
func (e *macEngine) RebootRequired(ctx context.Context) bool { return e.restartPending }

// Outside an install this engine cannot tell whether a restart is pending.
func (e *macEngine) rebootBlindAtScan() {}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

// softwareUpdateEntry is one listed update plus the restart flag, which is
// engine state rather than a wire field.
type softwareUpdateEntry struct {
	entry   entry
	restart bool
}

// parseSoftwareUpdateList reads `softwareupdate --list`.
//
// The format, as documented and as reconstructed in
// testdata/softwareupdate-list-RECONSTRUCTED.txt:
//
//	Software Update found the following new or updated software:
//	* Label: macOS Sequoia 15.6.1-24G90
//		Title: macOS Sequoia 15.6.1, Version: 15.6.1, Size: 7473719KiB, Recommended: YES, Action: restart,
//	* Label: Safari18.6MontereyAuto-18.6
//		Title: Safari, Version: 18.6, Size: 128383KiB, Recommended: YES,
//
// A `* Label:` line names the install target; the indented line after it
// carries comma-separated `Key: Value` attributes. THE LABEL IS THE IDENTIFIER
// because it is the only string `--install` accepts.
//
// A label with no attribute line still produces an entry: the label alone is
// enough to install and enough to reconcile, and dropping it would silently
// hide an available update.
func parseSoftwareUpdateList(out string) []softwareUpdateEntry {
	var (
		result  []softwareUpdateEntry
		pending *softwareUpdateEntry
	)
	flush := func() {
		if pending != nil {
			result = append(result, *pending)
			pending = nil
		}
	}

	for _, line := range strings.Split(out, "\n") {
		trimmed := strings.TrimSpace(line)
		if label, ok := cutLabel(trimmed); ok {
			flush()
			if label == "" {
				continue
			}
			pending = &softwareUpdateEntry{entry: entry{
				Source:     sourceSoftwareUpdate,
				Identifier: label,
				Title:      label,
				Severity:   "unknown",
			}}
			continue
		}
		if pending == nil || !strings.Contains(trimmed, ":") {
			continue
		}
		attrs := parseSoftwareUpdateAttrs(trimmed)
		if len(attrs) == 0 {
			continue
		}
		applySoftwareUpdateAttrs(pending, attrs)
		flush()
	}
	flush()
	return result
}

// cutLabel recognises the `* Label: <label>` line.
func cutLabel(line string) (string, bool) {
	const marker = "* Label:"
	if !strings.HasPrefix(line, marker) {
		return "", false
	}
	return strings.TrimSpace(strings.TrimPrefix(line, marker)), true
}

// parseSoftwareUpdateAttrs splits `Title: X, Version: 1.2, Recommended: YES,`
// into its pairs.
//
// Splitting on "," is imperfect and known to be: a title containing a comma
// would be cut short. That is a cosmetic loss in a field that is already
// clamped to 300 characters, and the alternative -- a regex over a format Apple
// has changed between releases -- fails LOUDLY on a machine nobody can test.
// The keys that matter (`Recommended`, `Action`, `Version`) never contain one.
func parseSoftwareUpdateAttrs(line string) map[string]string {
	attrs := map[string]string{}
	for _, part := range strings.Split(line, ",") {
		key, value, found := strings.Cut(part, ":")
		if !found {
			continue
		}
		key = strings.ToLower(strings.TrimSpace(key))
		value = strings.TrimSpace(value)
		if key != "" && value != "" {
			attrs[key] = value
		}
	}
	return attrs
}

func applySoftwareUpdateAttrs(se *softwareUpdateEntry, attrs map[string]string) {
	if title := attrs["title"]; title != "" {
		if version := attrs["version"]; version != "" && !strings.Contains(title, version) {
			title = title + " " + version
		}
		se.entry.Title = title
	}
	se.entry.Severity = macSeverity(attrs["recommended"])
	se.restart = strings.EqualFold(attrs["action"], "restart") ||
		strings.EqualFold(attrs["action"], "shut down")
}

// macSeverity is the only severity Apple's listing can support.
//
// `softwareupdate --list` publishes NO severity. What it publishes is
// `Recommended: YES|NO`, which is Apple's own statement that an update carries
// security or stability fixes every machine should take; the routine minor
// updates that carry CVE fixes are all marked so.
//
// Mapping that to `important` is a JUDGEMENT and worth naming as one, because
// PD-02's auto-approve rule fires on `critical` and `important`. The
// consequence, precisely: on an org that has turned `auto_approve_security` on,
// every Apple-recommended update auto-approves into the `test` ring. That is
// the intent of the rule -- the test ring is where a security update is
// supposed to land first -- and `moderate` would instead mean no macOS update
// ever auto-approves, quietly making the feature a no-op on half the fleet.
// Everything not recommended stays `unknown`, which is true.
func macSeverity(recommended string) string {
	if strings.EqualFold(strings.TrimSpace(recommended), "YES") {
		return "important"
	}
	return "unknown"
}

// mentionsRestart reads softwareupdate's closing sentence.
//
// Belt and braces for the `Action: restart` flag: an update can decide during
// installation that it needs a restart even when the catalog did not say so.
func mentionsRestart(out string) bool {
	lower := strings.ToLower(out)
	return strings.Contains(lower, "restart your computer") ||
		strings.Contains(lower, "requires that you restart")
}
