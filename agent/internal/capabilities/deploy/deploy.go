// Package deploy implements the two capabilities that execute
// attacker-relevant code as SYSTEM/root: `script.run` and `app.deploy`
// (docs/modules/app-deploy/spec.md §5, §6, §9).
//
// # Why one package for two job types
//
// They share the whole download / verify / execute machinery: the bounded
// process spawn, the output cap, the exit-code mapping, the restricted
// scratch directory, the argv planner. Go has no way to share unexported
// machinery across packages, and exporting it would mean the seam that keeps
// job-supplied strings off a command line is a public API anyone can route
// around. `cert` already registers two job types from one package for the same
// reason, so this is the established shape rather than a new one.
//
// # The authorization is the signature, and nothing else
//
// A `script.run` job carries the SCRIPT BODY inside the Ed25519-signed
// envelope (AD-02). An `app.deploy` job carries the installer's SHA-256 inside
// the same envelope (AD-07). In both cases the thing that authorizes execution
// is the server's signature over the params, checked by internal/jobs before
// this package is reached — there is no local allowlist, no cached approval,
// no "we ran this last week" path, and no second entry point. Both handlers
// take a jobs.VerifiedJob, which is an interface with an unexported method, so
// this package cannot be handed anything that has not been verified.
//
// For `app.deploy` the consequence is worth stating on its own, because it is
// the one that is easy to get backwards: THE DOWNLOAD IS NOT THE
// AUTHORIZATION. Object storage is not a trust boundary — whoever can write
// the bucket can change the bytes. What authorizes an installer to run is the
// signature over the params that NAMED ITS HASH, so the hash is checked
// against the fetched bytes and the executable path does not exist until it
// matches. See install.go's `stage`.
//
// # Antivirus and EDR are a design constraint, not an afterthought
//
// This runs on Windows 11 with Defender and, in most tenants, a third-party
// EDR as well. `script.run` is definitionally the behaviour EDR exists to
// catch: a SYSTEM-level service spawning an interpreter. Three rules follow,
// and they are enforced in code rather than remembered:
//
//  1. NOTHING IS EVER A COMMAND LINE. Every process is spawned with an
//     explicit argv through os/exec — never `sh -c`, never `cmd /c`, never a
//     concatenated string. There is no word splitting to escape and no
//     metacharacter to worry about, and the source walker in exec_test.go
//     fails the build if a shell appears.
//  2. NO `-EncodedCommand`, EVER. A base64 payload on a PowerShell command
//     line is a top-tier malware indicator and is the single loudest thing
//     this capability could do. A PowerShell body is written to a file in a
//     directory only SYSTEM and Administrators can open, and `-File` names
//     that path. scratch.go and perm_windows.go own the directory ACL.
//  3. THE FULL SPAWN LIST IS DOCUMENTED. plan.go is the only place a process
//     is named, every name in it is a compile-time constant, and
//     TestTheDocumentedSpawnListIsTheOnlyOneThePlannerCanProduce asserts the
//     list an EDR team would be given is the list the planner can actually
//     produce.
//
// # Never log a body, never log an argument
//
// A script body and an installer's arguments carry credentials — that is what
// operators put in them. This package's rule is the one internal/jobs already
// applies to refusal reasons: the agent's own sentences only, nothing
// job-supplied echoed. `params.Body` and `params.InstallArgs` are never
// formatted into a log record, an error, or a refusal reason.
// TestNoLogRecordErrorOrRefusalEverCarriesTheScriptBodyOrItsArguments plants a
// distinctive secret in both and searches everything this package emits.
//
// The one thing that IS returned is the executed process's OUTPUT, because
// that is the contract (`InstallOutcome.log`) and the operator asked for it.
// It is capped, tail-kept and stripped to printable ASCII before it can reach
// an operator's console.
//
// # Dormancy
//
// Registration is a map insert. Nothing here opens a file, dials anything or
// starts a goroutine until a verified job arrives; the scratch directory, the
// HTTP client and the child process all come into being inside the job and are
// gone before the handler returns. budget_test.go measures that rather than
// describing it.
package deploy

import (
	"context"
	"encoding/json"
	"log/slog"
	"os/exec"
	"runtime"
	"sync/atomic"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Job types, matching app/modules/deploy/handler.py's registrations.
const (
	JobTypeDeploy = "app.deploy"
	JobTypeScript = "script.run"
)

// LookPath resolves a program name to an absolute path. Injected so the argv
// planner can be tested for a GOOS this test machine is not.
type LookPath func(name string) (string, error)

// Stat reports whether a path exists. Injected for the same reason: the
// detection rules are pure logic and must be testable without a filesystem
// that happens to look like a Windows one.
type Stat func(path string) bool

// Options wires the capability. Every field has a production default; they
// exist so hash verification, output truncation, exit-code mapping, timeouts
// and refusal classification are unit-testable on a machine with no MSI, no
// .pkg and no winget.
type Options struct {
	// GOOS is the platform the argv planner plans for. Empty means
	// runtime.GOOS. A test sets it to "windows" on Linux; production never
	// sets it.
	GOOS string

	// LookPath resolves interpreters and package managers. Empty means
	// exec.LookPath, except that winget.exe is looked for in the App
	// Installer package directory first (winget.go).
	LookPath LookPath

	// Exec spawns one already-planned process. Empty means spawn (exec.go),
	// which is the ONLY place in this package that starts a process.
	Exec Executor

	// Fetch retrieves an artifact by its `artifact_ref`. Empty means
	// defaultFetcher (fetch.go).
	Fetch Fetcher

	// Stat backs the `file_exists` detection rule. Empty means osStat.
	Stat Stat

	// ScratchRoot is where the per-job restricted directory is created. Empty
	// means the OS temp directory.
	ScratchRoot string

	Logger *slog.Logger
}

// Service is the process-wide holder. The zero value is usable and is what
// `Process` is: it resolves every default at JOB TIME, so registration stays a
// map insert and nothing here touches the disk or the network while the agent
// is idle.
type Service struct {
	opts Options
	// scripts and deploys count completed jobs, so a dormancy assertion
	// outside this package can observe that neither capability has run. A
	// single atomic load each.
	scripts atomic.Int64
	deploys atomic.Int64
}

// Process is the Service the default capability registry registers.
var Process = &Service{}

// New returns a Service with explicit wiring. Tests use it; production uses
// Process.
func New(o Options) *Service { return &Service{opts: o} }

// Scripts reports how many `script.run` jobs this process has completed.
func (s *Service) Scripts() int64 { return s.scripts.Load() }

// Deploys reports how many `app.deploy` jobs this process has completed.
func (s *Service) Deploys() int64 { return s.deploys.Load() }

func (s *Service) logger() *slog.Logger {
	if s.opts.Logger != nil {
		return s.opts.Logger
	}
	return slog.Default()
}

func (s *Service) goos() string {
	if s.opts.GOOS != "" {
		return s.opts.GOOS
	}
	return runtime.GOOS
}

func (s *Service) lookPath() LookPath {
	if s.opts.LookPath != nil {
		return s.opts.LookPath
	}
	// winget.exe is not on LocalSystem's PATH; see winget.go.
	return wingetAwareLookPath(exec.LookPath, locateWinget)
}

func (s *Service) executor() Executor {
	if s.opts.Exec != nil {
		return s.opts.Exec
	}
	return spawn
}

func (s *Service) fetcher() Fetcher {
	if s.opts.Fetch != nil {
		return s.opts.Fetch
	}
	return defaultFetcher
}

func (s *Service) stat() Stat {
	if s.opts.Stat != nil {
		return s.opts.Stat
	}
	return osStat
}

// Run is the `script.run` capability.
//
// It takes a jobs.VerifiedJob and nothing else (invariant #1): the runner has
// already checked the Ed25519 signature over the canonical bytes — WHICH COVER
// THE SCRIPT BODY — against the key pinned at enrollment, that the job targets
// this device, that it is fresh, that its nonce is unseen, and that the type
// is registered. There is no second path into this function and this package
// adds none.
func (s *Service) Run(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	return s.handleScript(ctx, job.Params())
}

// handleScript is Run with the VerifiedJob already unwrapped.
//
// It exists so the counter behaviour is testable. jobs.VerifiedJob is an
// interface with an unexported method — deliberately unimplementable outside
// internal/jobs, which is invariant #1 made structural — so a test in THIS
// package cannot construct one, and without this seam `Run`'s own two lines
// would be the only untested code on the path. There is no exported route to
// it: a caller outside this package still has to produce a VerifiedJob.
func (s *Service) handleScript(ctx context.Context, params json.RawMessage) (any, error) {
	res, err := s.runScript(ctx, params)
	if err != nil {
		// Explicitly nil, not `res`: a typed nil pointer inside an `any` is
		// non-nil to the runner, and resultBody would marshal it to `null`.
		return nil, err
	}
	s.scripts.Add(1)
	return res, nil
}

// Deploy is the `app.deploy` capability. Same statement as Run about what has
// already been checked, plus AD-07: the params the signature covers name the
// artifact's SHA-256, and install.go refuses to produce an executable path for
// bytes that do not match it.
func (s *Service) Deploy(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	return s.handleDeploy(ctx, job.Params())
}

// handleDeploy is Deploy with the VerifiedJob already unwrapped. See
// handleScript for why this seam exists.
func (s *Service) handleDeploy(ctx context.Context, params json.RawMessage) (any, error) {
	res, err := s.deploy(ctx, params)
	if err != nil {
		return nil, err
	}
	s.deploys.Add(1)
	return res, nil
}

// installOutcome implements InstallOutcome.schema.json (app/core/wire.py).
//
// No `omitempty` anywhere: the server model is extra="forbid" but every
// optional member has an explicit default, and emitting `null` for a value
// this agent could not determine is the honest report. `detected_after` in
// particular is a THREE-valued field — true, false, and "this build cannot
// evaluate that rule type" — and collapsing the third into `false` would be a
// claim the agent is not entitled to make.
type installOutcome struct {
	Identifier    string  `json:"identifier"`
	State         string  `json:"state"`
	ExitCode      *int    `json:"exit_code"`
	Log           string  `json:"log"`
	LogRef        *string `json:"log_ref"`
	DetectedAfter *bool   `json:"detected_after"`
}

// InstallOutcome.state's four values. `installed` and `not_applicable` are
// deploy's vocabulary; `succeeded` is script.run's; `failed` belongs to both.
// Emitting the wrong one is not cosmetic: app/modules/deploy/dispatch.py and
// scripts.py both COERCE an out-of-domain state to "failed" and record the
// original, so a script reporting `installed` reads as a failed run.
const (
	stateInstalled     = "installed"
	stateFailed        = "failed"
	stateNotApplicable = "not_applicable"
	stateSucceeded     = "succeeded"
)

// maxIdentifier bounds the `identifier` this agent echoes. It is built from
// job-supplied strings (a script id, a manager package id), and while those
// arrived inside a signed envelope they still land in an operator's console.
const maxIdentifier = 200
