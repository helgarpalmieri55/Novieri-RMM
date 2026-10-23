// Package jobcancel implements the `job.cancel` capability
// (docs/superpowers/specs/2026-08-22-job-lifecycle-design.md §3.1, §3.8).
//
// # A cancel is a SIGNED JOB, and that is the whole security argument
//
// This capability is reached the same way every other one is: through
// `internal/jobs`, after the runner has checked the Ed25519 signature over the
// canonical bytes against the key pinned at enrollment, that the job targets
// THIS device, that it is fresh, that its nonce is unseen, and that the type is
// registered -- in that order, with the signature first. It takes a
// jobs.VerifiedJob and nothing else, and jobs.VerifiedJob cannot be constructed
// outside its own package.
//
// That matters more here than for most capabilities, not less. A mechanism
// that makes an endpoint abandon work is a denial-of-service primitive: an
// unsigned one would let anyone who can reach the agent's socket stop every
// job on every machine in the fleet, one frame at a time, with nothing on the
// record. Invariant #1 gets no "it is only a cancel" exemption, and this
// package adds no second path.
//
// # What it does NOT decide
//
// Whether this device may be told to stop that job was settled on the control
// plane -- by `dispatch_permission_for(target.type)`, the permission the
// target job's OWN dispatch route requires -- and proved to this endpoint by
// the signature. Nothing here re-derives authorization, because nothing here
// could: the endpoint holds no permission model, and an endpoint that decided
// for itself which cancels to honour would be an endpoint that could decline
// them.
//
// The target job id likewise comes only from the SIGNED PARAMS. This capability
// never reads a job id from anywhere else, and the server reads it back out of
// those same signed params rather than out of the result this capability
// returns -- so a compromised endpoint cannot make the control plane close a
// different job.
//
// # Interruption is best-effort, and this package says so rather than implying
//
// `outcome: "interrupted"` means THE AGENT ASKED THE CAPABILITY TO STOP by
// cancelling its context. What actually stops depends on the capability:
// `backup`, `netsweep`, `site` and `logs` check `ctx.Err()` in their loops;
// `deploy` and `patch` shell out through `exec.CommandContext`, which kills the
// direct child and -- as both files record from measurement on this project --
// not necessarily its descendants. The TARGET job's own result is the evidence
// of what happened; this one only says what was asked.
//
// # Dormancy, and why this capability has no queue of its own to wait behind
//
// Registration is a map insert. Nothing here opens a file, dials anything,
// starts a goroutine or takes a timer-wheel entry; `Bind` stores one pointer.
// The runner's cancellation state is two bounded structures and one mutex, with
// no sweeper and no expiry timer (internal/jobs/interrupt.go).
//
// `jobs.capabilityKey("job.cancel")` is `"job"`, which is a Dispatcher queue of
// its own, and `"job"` is deliberately NOT in `jobs.DefaultHeavyKeys`. So a
// cancel is never queued behind the heavy job it is cancelling and never waits
// on the heavy semaphore -- which is the reason this type is named `job.cancel`
// and not, say, `backup.cancel`. jobcancel_test.go asserts it rather than
// describing it.
package jobcancel

import (
	"context"
	"encoding/json"
	"log/slog"
	"sync"
	"sync/atomic"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// JobType matches `app/jobs/registry.py`'s registration.
const JobType = "job.cancel"

// Interrupter is the runner's cancellation handle. `*jobs.Runner` implements
// it; a test supplies its own so this package can be exercised without a
// signing key.
//
// It is an interface HERE rather than a concrete *jobs.Runner so that the
// dependency keeps running capabilities -> jobs, and so that binding remains
// something main.go does explicitly rather than a package-level global two
// packages share.
type Interrupter interface {
	Interrupt(targetJobID string) jobs.CancelOutcome
}

// params implements JobCancelParams.schema.json.
//
// Unknown members are ignored (encoding/json's default): the inbound half of
// docs/kernel/wire-protocol/README.md's rule, so a strictly additive server
// change does not take a deployed fleet offline.
type params struct {
	TargetJobID string `json:"target_job_id"`
}

// ParamsShape and ResultShape re-export the two wire shapes for
// schema_test.go, which lives in package `jobcancel_test` (so that it drives
// the capability through the real runner, the only way to obtain a
// jobs.VerifiedJob) and therefore cannot see the unexported types.
//
// They are TYPE ALIASES, not copies: a second declaration would be a second
// truth about the wire, and the conformance test would then be asserting
// against itself.

// result implements JobCancelResult.schema.json.
//
// `target_job_id` is echoed BACK FROM THE SIGNED PARAMS, never invented. The
// server compares it against what it signed and discards a result that names
// anything else -- so this field is a consistency check the endpoint cannot
// use to redirect the outcome, and it must be the params' value verbatim.
type result struct {
	TargetJobID string `json:"target_job_id"`
	Outcome     string `json:"outcome"`
}

type (
	// ParamsShape is `params`, exported for the schema conformance test.
	ParamsShape = params
	// ResultShape is `result`, exported for the schema conformance test.
	ResultShape = result
)

// Options wires the capability.
type Options struct {
	Interrupter Interrupter
	Logger      *slog.Logger
}

// Service is the process-wide holder. The zero value is usable and is what
// `Process` is; it holds nothing until main calls Bind.
type Service struct {
	mu   sync.RWMutex
	opts Options
	// runs counts completed cancels, so a dormancy assertion outside this
	// package can observe that this capability has NOT run.
	runs atomic.Int64
}

// Process is the Service the default capability registry registers.
var Process = &Service{}

// New returns a Service with explicit wiring. Tests use it; production uses
// Process plus Bind.
func New(o Options) *Service { return &Service{opts: o} }

// Bind gives the capability the runner's interrupt handle.
//
// THE BIND-AFTER-CONSTRUCT PATTERN IS FORCED, not preferred: the capability
// registry must exist before `jobs.New` can build a runner against it, and this
// capability needs that runner. `backup.Process.Bind` and `health.Process.Bind`
// solve the same ordering the same way.
//
// Registration is unconditional and does not wait for this call, on the
// precedent every other capability sets: an agent that advertised
// interruptibility only when some start-up ordering worked out would be an
// agent whose cancels silently stop being honoured. Unbound, it REFUSES and
// names why, which the control plane can act on; `unknown_type` would tell the
// operator this build cannot be interrupted at all, which would be false.
func (s *Service) Bind(i Interrupter) {
	s.mu.Lock()
	s.opts.Interrupter = i
	s.mu.Unlock()
}

// Runs reports how many cancels this process has carried out.
func (s *Service) Runs() int64 { return s.runs.Load() }

func (s *Service) interrupter() Interrupter {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.opts.Interrupter
}

func (s *Service) logger() *slog.Logger {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.opts.Logger != nil {
		return s.opts.Logger
	}
	return slog.Default()
}

// Cancel is the `job.cancel` capability.
//
// IT SUCCEEDS WHENEVER IT CARRIED OUT ITS INSTRUCTION, including when the
// answer is `unknown`. "This agent has never seen that job" is a true and
// useful report, not a failure: the server writes it as `too_late` and leaves
// the target to speak for itself. Reporting `failed` instead would tell the
// operator the endpoint broke while looking.
func (s *Service) Cancel(_ context.Context, job jobs.VerifiedJob) (any, error) {
	var p params
	if err := json.Unmarshal(job.Params(), &p); err != nil {
		return nil, jobs.Refuse("job.cancel params are not the object this agent expects")
	}
	if p.TargetJobID == "" {
		return nil, jobs.Refuse("job.cancel named no target job")
	}
	if p.TargetJobID == job.ID() {
		// Not merely odd -- it is the one shape that could loop. The runner
		// would be asked to interrupt the very job doing the asking, whose
		// context is the one this capability is running under.
		return nil, jobs.Refuse("a job.cancel may not name itself")
	}
	i := s.interrupter()
	if i == nil {
		return nil, jobs.Refuse(
			"this build's job.cancel capability was never bound to the job runner, " +
				"so it cannot stop anything")
	}

	outcome := i.Interrupt(p.TargetJobID)
	s.runs.Add(1)
	s.logger().Info("job.cancel carried out",
		"job_id", job.ID(), "target_job_id", p.TargetJobID, "outcome", string(outcome))
	return result{TargetJobID: p.TargetJobID, Outcome: string(outcome)}, nil
}
