package jobs

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"runtime/debug"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// Options configures a Runner. Every field without a documented default is
// required: New fails rather than substituting something weaker.
type Options struct {
	// DeviceID is this agent's device_id, read from the enrollment identity.
	// It is never read from configuration.
	DeviceID string
	// SigningKey is the Ed25519 key pinned at enrollment. Nothing else may be
	// used to verify a job, ever.
	SigningKey ed25519.PublicKey
	// Nonces is the replay store.
	Nonces NonceStore
	// Registry maps job types to capabilities.
	Registry Registry
	// Sender carries status, logs and results back to the control plane.
	Sender Sender
	// Now defaults to time.Now. Injected so freshness is asserted on observed
	// values rather than by sleeping.
	Now func() time.Time
	// Logger defaults to slog.Default().
	Logger *slog.Logger
	// Dispatcher runs verified jobs off the read goroutine. Nil builds the
	// default: queue depth 8, heavy budget 2, heavy keys DefaultHeavyKeys.
	Dispatcher *Dispatcher
	// BaseContext is the PROCESS context every job runs under, and is required
	// when Dispatcher is nil (it is what the default Dispatcher is built with).
	// It must not be a per-connection context: see DispatcherOptions.BaseContext
	// for what happens when a job's lifetime is the connection's.
	BaseContext context.Context
}

// Runner is the only path from the wire to a capability.
//
// transport.Recv hands it raw bytes and it hands a capability a *VerifiedJob,
// with the five checks in between. There is no other entry point, no build tag
// that skips a check, and no test hook that constructs a VerifiedJob -- see
// verified.go and construct_test.go.
type Runner struct {
	verifier   *Verifier
	registry   Registry
	sender     Sender
	now        func() time.Time
	log        *slog.Logger
	dispatcher *Dispatcher

	// The cancellation state (interrupt.go). One mutex, two bounded
	// structures, NO goroutine and NO timer -- the idle budget forbids
	// anything here that wakes up.
	mu        sync.Mutex
	running   map[string]context.CancelFunc
	cancelled cancelledSet
}

// DefaultHeavyKeys names the capabilities that share the heavy budget. Exported
// so a test can assert every entry still matches a registered job type -- a
// typo here would silently un-cap a heavy capability.
//
// PLAN DEFECT found by this task's own TestEveryHeavyKeyNamesARegisteredCapability
// (Task 7, docs/superpowers/plans/2026-08-09-agent-job-concurrency.md): the map
// this replaced said "deploy", but deploy's two job types are "app.deploy" and
// "script.run" (internal/capabilities/deploy/deploy.go) -- capabilityKey splits
// on the first '.', so "deploy" never matched either one. Both have run outside
// the heavy budget, unthrottled, since they were registered (commit fb16e23):
// dispatcher_test.go's own coverage (TestOnlyTwoHeavyCapabilitiesRunAtOnce) used
// a synthetic "deploy.script" job type nothing is registered under, so it never
// caught the drift either. Corrected to the capabilityKey values the real
// deploy job types actually produce.
func DefaultHeavyKeys() map[string]bool {
	return map[string]bool{"backup": true, "patch": true, "app": true, "script": true}
}

// New builds a Runner or fails.
func New(o Options) (*Runner, error) {
	v, err := NewVerifier(o.DeviceID, o.SigningKey, o.Nonces, o.Registry)
	if err != nil {
		return nil, err
	}
	if o.Sender == nil {
		return nil, fmt.Errorf("%w: no sender (a job whose result cannot be reported is a job the "+
			"operator watches hang)", ErrIncomplete)
	}
	r := &Runner{
		verifier: v, registry: o.Registry, sender: o.Sender, now: o.Now, log: o.Logger,
		running:   make(map[string]context.CancelFunc),
		cancelled: newCancelledSet(),
	}
	if r.now == nil {
		r.now = time.Now
	}
	if r.log == nil {
		r.log = slog.Default()
	}
	if o.Dispatcher == nil {
		d, err := NewDispatcher(DispatcherOptions{
			BaseContext: o.BaseContext,
			QueueDepth:  8,
			HeavyBudget: 2,
			HeavyKeys:   DefaultHeavyKeys(),
			Exec:        r.run,
			Logger:      o.Logger,
		})
		if err != nil {
			return nil, err
		}
		o.Dispatcher = d
	}
	r.dispatcher = o.Dispatcher
	return r, nil
}

// Handle processes one inbound frame.
//
// It is called on the transport's read goroutine and MUST NOT BLOCK. Decode,
// verification and registry lookup happen here, in wire order; capability
// execution is handed to the Dispatcher and this returns immediately.
//
// It used to block for the whole capability, so that job ordering was the
// wire's ordering. That cost the agent every inbound frame arriving while a job
// ran -- including a reply the job itself was waiting for, which deadlocked the
// backup dedup exchange, and including the WebSocket pongs whose absence made
// the gateway close the connection. Ordering is now kept per capability by the
// Dispatcher, which is the only place it ever mattered.
//
// Handle never panics, and a panicking capability never takes the process
// down, because two independent barriers sit between a capability and the
// worker goroutine it runs on:
//
//   - invoke's recover (this file) is the inner one: Exec is r.run, which
//     calls invoke, so a capability's panic is caught right where it happens
//     and turned into a `failed` job_result -- mirroring the gateway's own
//     `except Exception` around handle_agent_message, because one bad
//     capability must not take the connection down with it.
//   - Dispatcher.exec1's recover (dispatcher.go) is the outer, independent
//     backstop: it covers the whole worker call, not just invoke's call to
//     the capability. If invoke's recover is ever bypassed -- a future Exec
//     that does not route through invoke -- exec1 still stops the panic from
//     crashing the process, but it produces no job_result at all, only a
//     local log line. That job then sits `running` until the control plane
//     expires it: a silent hang, not a crash, and harder for an operator to
//     notice than either would be.
func (r *Runner) Handle(ctx context.Context, frame []byte) {
	msg, err := wire.Decode(frame)
	if err != nil {
		// No job identity exists yet, so there is nobody to answer. wire.Decode
		// refuses an empty `sig`, a version other than 1, an unknown kind and
		// anything unparseable -- all before a job_id can be trusted, and
		// JobLogMsg/JobResultMsg both require a job_id the server can bind to a
		// real job for this device (_load_job_for_device). Inventing one from an
		// unverified frame would be acting on it.
		//
		// The server observes the job time out, which is the correct state for
		// a frame this agent could not authenticate. The local log is the
		// operator-visible artefact.
		r.log.Warn("undecodable frame from gateway; dropped without answer",
			"err", err, "bytes", len(frame))
		return
	}
	env, ok := msg.(*wire.Envelope)
	if !ok {
		r.log.Warn("malformed: inbound frame decoded to an unexpected type",
			"type", fmt.Sprintf("%T", msg))
		return
	}

	job, reason, detail := r.verifier.Verify(env, r.now())
	if reason != "" {
		// Log the full detail locally; send the bounded, non-echoing form.
		r.log.Warn("job refused",
			"job_id", env.Job.ID, "job_type", env.Job.Type, "reason", string(reason), "detail", detail)
		r.refuse(ctx, env.Job.ID, reason, detail)
		return
	}

	handler, ok := r.registry.Lookup(job.Type())
	if !ok || handler == nil {
		// Unreachable: Verify's type check already looked this up against the
		// same registry, which is written once at init and read-only
		// thereafter. Kept because the only alternative is calling a nil
		// handler, and a registry that changed under us must refuse rather than
		// take the agent down.
		//
		// The DETAIL is deliberately different from Verify's. Two code paths
		// reporting an identical string would mean a test asserting on
		// unknown_type could not tell which one answered, and the type check in
		// Verify could be deleted with the suite still green -- observed while
		// building this, and fixed by making the two distinguishable.
		r.log.Error("capability vanished between verification and dispatch", "job_type", job.Type())
		r.refuse(ctx, job.ID(), ReasonUnknownType,
			errors.New("the capability registry changed under the runner"))
		return
	}

	// No ctx: the job runs under the Dispatcher's process-scoped BaseContext,
	// not under the connection ctx this function was called with. ctx is still
	// the right one for the refusal below -- that is a send, on the read
	// goroutine, while the connection is up.
	if err := r.dispatcher.Submit(job, handler); err != nil {
		// A full queue is a real condition the operator must see. Refusing is
		// what puts it in the server's job record; dropping it silently would
		// show up only as a job that timed out for no stated reason.
		r.log.Warn("capability queue full; refusing",
			"job_id", job.ID(), "job_type", job.Type())
		r.refuse(ctx, job.ID(), ReasonQueueFull,
			Refuse("this capability already has the maximum number of queued jobs"))
	}
}

// run invokes one capability and reports the outcome.
func (r *Runner) run(ctx context.Context, job VerifiedJob, handler Handler) {
	// The job's own expires_at is the deadline. A capability that outran it
	// would be reporting a result for a job the control plane has already
	// written off (JobEngine expires it), so the context ends when the job does.
	//
	// SINCE CYCLE 2 IT IS ALSO THE INTERRUPT HANDLE: `begin` publishes this
	// CancelFunc under the same lock that checks whether a `job.cancel` for
	// this id has already been carried out, so a cancel can neither be lost in
	// the gap nor arrive too early to matter.
	jobCtx, cancel := context.WithDeadline(ctx, job.ExpiresAt())
	defer cancel()

	if !r.begin(job.ID(), cancel) {
		// A `job.cancel` for this job was carried out BEFORE the job itself
		// reached the runner. The capability is never invoked -- and the agent
		// must not fall silent here, because a job that is neither run nor
		// answered hangs until `expires_at` and the operator sees `expired` for
		// a job they cancelled. So the target answers for itself, with an
		// ordinary `job_result`: `refused`, a state JobResultMsg already
		// carries, and this agent's own constant as the reason.
		// `JobEngine.refuse` reads it and stores the job `cancelled` with
		// `cancel_outcome = "dropped"` -- "it never started", as distinct from
		// "it was stopped mid-flight".
		r.log.Info("job was cancelled before it started; not invoking the capability",
			"job_id", job.ID(), "job_type", job.Type())
		r.refuse(ctx, job.ID(), ReasonCancelledBeforeStart,
			Refuse("a signed job.cancel for this job was carried out before the job arrived"))
		return
	}
	defer r.finish(job.ID())

	r.send(ctx, wire.JobStatus{JobID: job.ID(), State: "running"})

	result, err := r.invoke(jobCtx, job, handler)
	var refusal *Refusal
	if errors.As(err, &refusal) {
		// A capability that will not do this at all, as distinct from one that
		// tried and failed. See Refusal: at least one module reads the
		// difference and stops dispatching to this device for a week, so the
		// state is not cosmetic and a capability opts into it explicitly.
		//
		// The reason goes through the same sanitiser as a verifier refusal --
		// it lands in the server's hash-chained audit log, and an audit record
		// an attacker can write into is not an audit record.
		r.log.Warn("capability refused the job",
			"job_id", job.ID(), "job_type", job.Type(), "reason", refusal.Reason)
		r.send(ctx, wire.JobResult{
			JobID:  job.ID(),
			State:  wire.StateRefused,
			Result: map[string]any{"reason": clamp(sanitize(refusal.Reason), MaxRefusalReasonLen)},
		})
		return
	}
	if errors.Is(err, ErrDeferred) {
		if job.Type() == DeferrableJobType {
			r.log.Info("job result deferred to a later process",
				"job_id", job.ID(), "job_type", job.Type())
			return
		}
		err = fmt.Errorf("%s may not defer its result", job.Type())
	}
	if err != nil {
		r.log.Warn("job failed", "job_id", job.ID(), "job_type", job.Type(), "err", err)
		r.send(ctx, wire.JobResult{
			JobID:  job.ID(),
			State:  wire.StateFailed,
			Result: map[string]any{"error": clamp(err.Error(), MaxRefusalReasonLen)},
		})
		return
	}

	body, cerr := resultBody(result)
	if cerr != nil {
		// A result the gateway would drop at parse_agent_message is worse than
		// a failure: the job hangs until it expires and nobody is told why.
		r.log.Error("capability returned an unreportable result",
			"job_id", job.ID(), "job_type", job.Type(), "err", cerr)
		r.send(ctx, wire.JobResult{
			JobID:  job.ID(),
			State:  wire.StateFailed,
			Result: map[string]any{"error": clamp(cerr.Error(), MaxRefusalReasonLen)},
		})
		return
	}
	r.send(ctx, wire.JobResult{JobID: job.ID(), State: wire.StateSucceeded, Result: body})
}

// invoke calls the capability with a panic barrier.
func (r *Runner) invoke(ctx context.Context, job VerifiedJob, handler Handler) (result any, err error) {
	defer func() {
		if p := recover(); p != nil {
			// The stack goes to the LOCAL log only. Shipping it in a job_result
			// would put agent internals into the control plane's job record for
			// every crash, and the operator-useful fact is that it panicked.
			r.log.Error("capability panicked",
				"job_id", job.ID(), "job_type", job.Type(), "panic", fmt.Sprint(p),
				"stack", string(debug.Stack()))
			err = fmt.Errorf("capability panic: %v", p)
			result = nil
		}
	}()
	return handler(ctx, job)
}

// refuse reports a refusal the way gateway/dispatch.py reads one.
func (r *Runner) refuse(ctx context.Context, jobID string, reason RefusalReason, detail error) {
	r.send(ctx, wire.JobResult{
		JobID:  jobID,
		State:  wire.StateRefused,
		Result: map[string]any{"reason": safeReason(reason, detail)},
	})
}

// safeReason builds the string that lands in the server's hash-chained audit
// log via JobEngine.refuse.
//
// Two rules, both about that log:
//
//   - Nothing job-supplied is echoed. An audit record an attacker can write
//     into is not an audit record. Details are this agent's own sentences.
//   - Before the signature passes, NOTHING in the envelope is trusted enough to
//     describe -- so bad_signature and malformed travel as the bare reason. A
//     JSON decoder's error text can quote the bytes it choked on, which would
//     be a way to smuggle content into an operator's console.
func safeReason(reason RefusalReason, detail error) string {
	switch reason {
	case ReasonBadSignature, ReasonMalformed:
		return string(reason)
	}
	if detail == nil {
		return string(reason)
	}
	return clamp(string(reason)+": "+sanitize(detail.Error()), MaxRefusalReasonLen)
}

// sanitize strips anything that is not a printable ASCII character. Bidi
// overrides and control characters in an operator's console are a way to make
// one record read as another.
func sanitize(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		if r > unicode.MaxASCII || !unicode.IsPrint(r) {
			b.WriteByte('?')
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}

func clamp(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n-3] + "..."
}

// resultBody converts a capability's return value into the object
// JobResultMsg.result must be.
//
// inventory.collect's handler returns the InventoryDocument itself and
// app/modules/inventory/handler.py does InventoryDocument(**result) on the
// TOP-LEVEL dict, so nothing may be nested under a wrapper key here.
func resultBody(v any) (map[string]any, error) {
	if v == nil {
		return nil, nil
	}
	if m, ok := v.(map[string]any); ok {
		return m, nil
	}
	b, err := json.Marshal(v)
	if err != nil {
		return nil, fmt.Errorf("result is not JSON-encodable: %w", err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, fmt.Errorf("result is not a JSON object (%T); JobResultMsg.result must be one", v)
	}
	return m, nil
}

// send is fire-and-log. A dropped outbound frame is a real loss and must leave
// a trace; Task 8's buffer is what makes it recoverable across a disconnect.
func (r *Runner) send(ctx context.Context, msg any) {
	if err := r.sender.Send(ctx, msg); err != nil {
		r.log.Warn("outbound send failed", "err", err, "msg", fmt.Sprintf("%T", msg))
	}
}
