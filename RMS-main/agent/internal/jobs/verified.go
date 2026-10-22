package jobs

import (
	"context"
	"encoding/json"
	"fmt"
	"time"
)

// VerifiedJob is a job that has passed every check in verify.go, and it is the
// ONLY thing a capability accepts.
//
// # Invariant #1, made structural — and why this is an interface
//
// docs/modules/agent/plan.md Task 7 specifies a STRUCT carrying an unexported
// zero-size field, on the grounds that "Go permits a composite literal with
// unexported fields only inside the declaring package, so no capability package
// can manufacture one".
//
// That is not what Go does, and testdata/badclient proved it: the rule forbids
// SPECIFYING an unexported field from another package, not OMITTING it. On
// go1.24.7,
//
//	jobs.VerifiedJob{ID: "forged", Type: "script.run", Device: "any"}
//
// compiled cleanly from an external package. The plan's structural guarantee
// was decoration. Measured, not recalled: `go build ./testdata/badclient`
// exited 0 against the struct form.
//
// An interface with an unexported method is the construct Go actually forbids:
// a type outside this package cannot supply verifiedByTheSignedJobRunner, so it
// cannot satisfy VerifiedJob, so it cannot be passed to a Handler. There is no
// exported implementation, no exported constructor, and no build tag that adds
// one. construct_test.go builds a package that tries the forge and asserts it
// does NOT compile — that assertion now fails when the guarantee is removed,
// which is the whole point of having it.
//
// The cost is that fields are methods. That is the price of the guarantee being
// real, and every capability package pays it.
type VerifiedJob interface {
	// ID, Type and Device are the signed identity of this job. Device always
	// equals this agent's own device_id.
	ID() string
	Type() string
	Device() string

	// Params are the EXACT bytes of the `params` member as they arrived.
	//
	// Not re-marshalled: the signature covers the received bytes
	// (canonicalised), so handing a capability a re-encoded copy would mean the
	// document that ran and the document that was signed are two things that
	// merely agreed at the time. Each capability unmarshals this into its own
	// typed struct.
	Params() json.RawMessage

	IssuedAt() time.Time
	ExpiresAt() time.Time

	// Nonce is the server's per-dispatch token, already recorded in the replay
	// store by the time a capability sees this.
	Nonce() string

	// verifiedByTheSignedJobRunner is unexported and therefore unimplementable
	// outside this package. Do not export it. Do not add a public type that
	// satisfies it. It is the enforcement of invariant #1 on the endpoint.
	verifiedByTheSignedJobRunner()
}

// verifiedJob is the one implementation, and it is unexported. It is
// constructed in exactly one place: the tail of Verifier.Verify, after all five
// checks have passed.
type verifiedJob struct {
	id        string
	jobType   string
	device    string
	params    json.RawMessage
	issuedAt  time.Time
	expiresAt time.Time
	nonce     string
}

func (j *verifiedJob) ID() string                  { return j.id }
func (j *verifiedJob) Type() string                { return j.jobType }
func (j *verifiedJob) Device() string              { return j.device }
func (j *verifiedJob) Params() json.RawMessage     { return j.params }
func (j *verifiedJob) IssuedAt() time.Time         { return j.issuedAt }
func (j *verifiedJob) ExpiresAt() time.Time        { return j.expiresAt }
func (j *verifiedJob) Nonce() string               { return j.nonce }
func (*verifiedJob) verifiedByTheSignedJobRunner() {}

// Refusal is a capability's way of answering `refused` rather than `failed`.
//
// # Why the distinction is worth a type
//
// gateway/dispatch.py reads a `refused` result's `result["reason"]`, and the
// two states mean different things to the control plane. `failed` is "this
// went wrong, try again"; `refused` is "this agent will not do this", and at
// least one module acts on it: app/modules/ipam/sweep.py marks a device that
// refuses `net.sweep` INELIGIBLE FOR SWEEPS FOR SEVEN DAYS and selects another
// device on the segment (spec §5.3 — "capability advertisement is the fast
// path; refusal is the correct fallback").
//
// So a capability that refuses is asking to be taken out of rotation, and that
// is the right answer for "this host cannot open a raw socket" and the wrong
// one for "the network was busy". Returning a plain error stays `failed`, which
// is the safe default; refusing is deliberate and rare.
//
// The reason travels into the server's hash-chained audit log via
// JobEngine.refuse, so it goes through the same sanitiser every other refusal
// does: nothing job-supplied is echoed, and the text is this agent's own
// sentence.
type Refusal struct{ Reason string }

func (r *Refusal) Error() string { return r.Reason }

// Refuse builds a Refusal. The message is the agent's own words -- do not
// format job-supplied strings into it.
func Refuse(format string, args ...any) error {
	return &Refusal{Reason: fmt.Sprintf(format, args...)}
}

// Handler is a capability. It receives a job that has been verified and a
// context bounded by the job's own expires_at.
//
// Task 10's capabilities.Handler is this type; the registry lives in that
// package and this one only consumes it through Registry, so the dependency
// runs capabilities -> jobs and never back.
type Handler func(context.Context, VerifiedJob) (any, error)

// Registry maps a job type to the capability that runs it.
//
// It is an interface here so this package does not import the capability
// registry (that import would be a cycle, since capabilities take a
// VerifiedJob). Lookup returning false is what makes the unknown_type refusal
// work, and it is the only reason this package knows job types exist at all.
type Registry interface {
	Lookup(jobType string) (Handler, bool)
}

// NonceStore is the replay defence. SeenOrRecord is ONE operation on purpose:
// splitting "have I seen this?" from "remember it" opens a window between the
// two, and a replay defence with a window is not one.
//
// It returns true when the nonce must be treated as already seen -- including
// when the store cannot record it. Fail closed: a store at its cap refuses
// rather than forgets. Task 8 provides the persisted implementation.
type NonceStore interface {
	SeenOrRecord(nonce string, expiresAt time.Time) bool
}

// Sender is the outbound half of the gateway connection. *transport.Conn
// satisfies it, and so does Task 8's buffered outbox.
//
// It is an interface so this package never holds a socket: the runner decides
// what to say, not how it travels.
type Sender interface {
	Send(ctx context.Context, msg any) error
}
