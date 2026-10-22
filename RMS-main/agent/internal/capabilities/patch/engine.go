package patch

import (
	"context"
	"runtime"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// engine is the per-OS half, and it is deliberately NARROW.
//
// # Why the seam is here and not somewhere more convenient
//
// A Linux container has no Windows Update Agent and no `softwareupdate`, so
// none of the code that actually touches those can run in this repository's CI.
// The response is not to test less; it is to move everything that is not the
// syscall to the other side of this interface. Filtering, matching a signed
// request against what the OS offers, the outcome mapping, the severity
// vocabulary, the clamping, the error classification and the whole shape of
// both documents live in patch.go and are exercised in full on Linux. What is
// left behind this interface is thin, per-OS, and clearly marked as never
// having executed here (docs/modules/patch/agent-capabilities.md §6).
//
// # The security property this interface carries
//
// Install takes an `entry`, and an entry is only ever produced by Available --
// that is, by the OS describing itself. There is no Install(identifier string).
// That is not a style choice: it is what makes "nothing job-supplied ever
// reaches an argument" structural rather than a rule someone has to remember.
// A job's `{source, identifier}` is used as a map key and nothing else, so the
// only way a hostile identifier could reach an installer is if the OS itself
// had already offered a string equal to it.
//
// # Dormancy
//
// An engine is created INSIDE a verified job and Closed when it returns. There
// is no process-wide engine, no cached scan and no background refresh. Close is
// on the interface for exactly one reason: the Windows engine holds COM
// apartment state and interface pointers that must be released, and a seam that
// had no Close would leak them for the life of the process.
type engine interface {
	// Family is the value of PatchScanDocument.os_family for this host. It must
	// be one of windows|linux|macos: ingest_scan raises PatchValidationError
	// when it does not match the device's own os_family, which fails the job.
	Family() string

	// Available is what the OS says is outstanding. Every entry it returns is a
	// thing this engine knows how to install.
	Available(ctx context.Context) ([]entry, error)

	// Installed is `<source>:<identifier>` for updates already present. It is
	// an aid to reconciliation and is allowed to be empty; see
	// agent-capabilities.md §3 for why apt's is deliberately so.
	Installed(ctx context.Context) ([]string, error)

	// Install applies ONE update that Available produced. A nil error means
	// installed; any error is that patch's `last_error` and nothing more --
	// the batch continues.
	Install(ctx context.Context, e entry) error

	// RebootRequired asks the OS whether it wants a restart. It returns a bool
	// and not an error because the wire has one boolean for this and no way to
	// say "I could not tell"; an engine that cannot determine it reports false
	// and logs, which understates rather than invents.
	RebootRequired(ctx context.Context) bool

	// Close releases whatever the engine holds. Always called, in a defer.
	Close()
}

// errNoEngine is the refusal a host with no patch engine returns.
//
// REFUSED AND NOT FAILED, and the distinction is load-bearing (jobs/verified.go):
// `failed` means "try again", `refused` means "this agent will not do this".
// A host with no supported package manager will not grow one by being retried,
// so a refusal is the true statement and a failure would have the control plane
// re-dispatching forever. A LOCKED package database is the opposite case and
// returns a plain error from Available.
func errNoEngine(detail string) error {
	return jobs.Refuse("patch: this agent has no patch engine for %s/%s: %s. Nothing was "+
		"scanned or installed", runtime.GOOS, runtime.GOARCH, detail)
}
