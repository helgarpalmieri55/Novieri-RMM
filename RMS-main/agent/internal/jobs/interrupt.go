package jobs

import "context"

// The endpoint half of cycle 2 of
// docs/superpowers/specs/2026-08-22-job-lifecycle-design.md §3.8.
//
// # Two pieces of state, one mutex, no goroutine and no timer
//
// The agent idle budget is a hard requirement (CLAUDE.md), so NOTHING here may
// wake up. There is no sweeper for the cancelled set, no expiry timer, no
// background reaper: both structures are bounded by construction, and the only
// code that touches them runs on a goroutine that was already running a job.
//
//   - `running` holds one context.CancelFunc per job ACTUALLY EXECUTING,
//     inserted under the same lock that checks for suppression and deleted in a
//     defer. It is bounded by the Dispatcher's concurrency, not by fleet
//     history.
//   - `cancelled` is a bounded FIFO set of job ids: 256 entries, oldest evicted
//     on insert. It closes the ordering race in which a `job.cancel` overtakes
//     the job it names.
//
// # Why a cancel must be able to arrive FIRST, and what happens when it does
//
// The gateway writes frames down one connection and the agent's Dispatcher
// runs capabilities concurrently across queues, so "the cancel arrives after
// its target has started" is the common case and not the only one. When the
// cancel wins the race the runner has never heard of the target: it records
// the id and answers `unknown`, which is true and is what the server can act
// on. The target then arrives, is matched against the set, and is NEVER
// INVOKED -- and the runner answers for it, because silence here would leave
// the job hanging until `expires_at` and the operator would see `expired` for
// a job they cancelled.

// CancelOutcome is `JobCancelResult.outcome` -- see
// docs/kernel/wire-protocol/JobCancelResult.schema.json.
//
// THIS BUILD PRODUCES THREE OF THE FOUR. `dropped_before_start` is in the
// contract and is not emitted here: with the two pieces of state above, a job
// sitting in a Dispatcher queue and a job this agent has never seen are the
// same observation -- absent from `running` -- so claiming to tell them apart
// would be a claim the code cannot make. That case is reported by the TARGET
// instead, which refuses `cancelled_before_start` when it is suppressed, and
// the server upgrades its `cancel_outcome` from `too_late` to `dropped` on
// that answer. The server keeps accepting the value from an agent that can be
// more precise; narrowing an enum an agent may already send is a break.
type CancelOutcome string

const (
	// OutcomeInterrupted: the job was executing and its context has been
	// cancelled. It means THE AGENT ASKED THE CAPABILITY TO STOP and nothing
	// stronger -- interruption is best-effort (design §8), the target job's own
	// result is what says whether it did.
	OutcomeInterrupted CancelOutcome = "interrupted"
	// OutcomeDroppedBeforeStart: accepted by the server, never produced here.
	OutcomeDroppedBeforeStart CancelOutcome = "dropped_before_start"
	// OutcomeAlreadyFinished: accepted by the server, never produced here --
	// the runner keeps no record of jobs that have finished, and inventing one
	// would be unbounded state on an endpoint that must be near-invisible.
	OutcomeAlreadyFinished CancelOutcome = "already_finished"
	// OutcomeUnknown: this agent has no such job executing. A TRUE AND USEFUL
	// ANSWER, not a failure: the cancel job itself succeeded.
	OutcomeUnknown CancelOutcome = "unknown"
)

// cancelledSetCap bounds the suppression set. 256 is far above any plausible
// number of cancels in flight against one endpoint at one moment, and it is a
// CAP rather than a target: the point is that the number exists, so a control
// plane that sent a million cancels could not grow this agent's memory.
const cancelledSetCap = 256

// cancelledSet is a bounded FIFO set of job ids. Not safe for concurrent use;
// Runner.mu guards every call.
type cancelledSet struct {
	ids   map[string]struct{}
	order []string
	next  int
}

func newCancelledSet() cancelledSet {
	return cancelledSet{ids: make(map[string]struct{}, 8)}
}

// add records an id, evicting the oldest once the cap is reached.
func (c *cancelledSet) add(id string) {
	if _, seen := c.ids[id]; seen {
		return
	}
	if len(c.order) < cancelledSetCap {
		c.order = append(c.order, id)
	} else {
		delete(c.ids, c.order[c.next])
		c.order[c.next] = id
		c.next = (c.next + 1) % cancelledSetCap
	}
	c.ids[id] = struct{}{}
}

// has reports whether this job was cancelled before the runner saw it.
//
// IT DOES NOT REMOVE. A membership test that consumed its entry would let a
// re-delivery of the same envelope -- the gateway drains a device's queue on
// every reconnect -- execute a job an operator has already cancelled. The set
// is capped, so leaving the entry costs nothing that is not already bounded.
func (c *cancelledSet) has(id string) bool {
	_, ok := c.ids[id]
	return ok
}

func (c *cancelledSet) len() int { return len(c.ids) }

// Interrupt asks this agent to stop the job named by a verified `job.cancel`,
// and reports what it found. It is the ONLY exported entry point into the
// cancellation state.
//
// **IT TAKES A JOB ID AND NOTHING ELSE, AND IT IS NOT AN AUTHORIZATION
// DECISION.** Whether this endpoint may be told to stop that job was settled
// on the control plane and proved by the signature the runner already
// verified: `internal/capabilities/jobcancel` reaches this only from a
// jobs.VerifiedJob, which cannot be constructed outside this package. Nothing
// here re-derives permission, because nothing here could.
func (r *Runner) Interrupt(targetJobID string) CancelOutcome {
	r.mu.Lock()
	defer r.mu.Unlock()
	if cancel, ok := r.running[targetJobID]; ok {
		// Non-blocking by contract: context.CancelFunc signals and returns, so
		// holding the mutex across it cannot deadlock against the `defer
		// r.finish(...)` the cancelled job will run when its handler returns.
		cancel()
		return OutcomeInterrupted
	}
	r.cancelled.add(targetJobID)
	return OutcomeUnknown
}

// begin registers a job as executing, unless it has already been cancelled.
//
// **THE CHECK AND THE REGISTRATION ARE ONE CRITICAL SECTION, AND SPLITTING
// THEM LOSES CANCELS SILENTLY.** With two separate lock acquisitions there is
// a window in which `run` finds the job not suppressed, `Interrupt` then finds
// it not running and merely records it, and `run` goes on to execute it: the
// operator is told `unknown`, the job runs to completion, and nothing anywhere
// says a cancel was dropped. One lock closes it.
func (r *Runner) begin(jobID string, cancel context.CancelFunc) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cancelled.has(jobID) {
		return false
	}
	r.running[jobID] = cancel
	return true
}

// finish removes a job from the running set. Called from a defer, so it runs
// on the panic path too -- a capability that panics must not leave a dead
// CancelFunc behind for a future job that happens to reuse the id.
func (r *Runner) finish(jobID string) {
	r.mu.Lock()
	delete(r.running, jobID)
	r.mu.Unlock()
}
