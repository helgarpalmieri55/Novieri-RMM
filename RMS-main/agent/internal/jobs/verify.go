// Package jobs is the endpoint half of invariant #1: nothing runs on this
// machine that the control plane did not sign, and the agent proved it.
//
// Implements docs/03-security-model.md and docs/05-agent-spec.md against the
// signer in app/jobs/engine.py (_payload, _sign) and app/core/signing.py.
// The frame contract is docs/kernel/wire-protocol/JobEnvelope.schema.json.
//
// # The order of the checks, and why it is that order
//
//	signature -> device -> freshness -> nonce -> type
//
// Signature comes first because every other field in the envelope is
// attacker-controlled until it passes. That includes the field naming the
// device, the field naming the type, and the timestamps -- so an unverified
// envelope must not decide even which REASON gets reported, let alone which
// code runs. verify_test.go's TestSignatureIsCheckedBeforeAnythingElseIsBelieved
// pins that with a frame that fails every check at once.
//
// Device comes second because a validly-signed job for a different endpoint is
// the case a signature-only agent executes. Freshness comes third because an
// old job that was genuinely signed for this device is the replay a stale
// clock lets through. The nonce comes fourth because it is the defence against
// a replay INSIDE the freshness window, and it is recorded only once the three
// checks above have passed, so nobody can pre-burn the nonce of a job the
// server is about to send. Type comes last because it is the only check that
// is about this build's capabilities rather than about the job's authenticity.
//
// # What "refused" means on the wire
//
// A refusal is a job_result with state "refused" and result.reason; the gateway
// reads exactly that key (app/gateway/dispatch.py:334-337) and JobEngine.refuse
// writes it into the hash-chained audit log. The reason is therefore kept
// short, specific, and free of job-supplied text: an audit record an attacker
// can write into is not one.
package jobs

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"fmt"
	"time"

	"github.com/toeshee/it-suite/agent/internal/canonical"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// RefusalReason is the vocabulary an operator sees in jobs.refused. It is a
// closed set: every value here is a decision this agent made, not a string it
// received.
type RefusalReason string

const (
	ReasonBadSignature  RefusalReason = "bad_signature"
	ReasonWrongDevice   RefusalReason = "wrong_device"
	ReasonExpired       RefusalReason = "expired"
	ReasonNotYetValid   RefusalReason = "not_yet_valid"
	ReasonReplayedNonce RefusalReason = "replayed_nonce"
	ReasonUnknownType   RefusalReason = "unknown_type"
	ReasonMalformed     RefusalReason = "malformed"
	// ReasonCancelledBeforeStart is the TARGET job's answer when a signed
	// `job.cancel` for it was carried out before the job itself reached the
	// runner (2026-08-22-job-lifecycle-design.md §3.8). Like ReasonQueueFull it
	// is never produced by Verify -- the job is perfectly valid and every check
	// passed; the agent simply will not run something it has already been told
	// to stop.
	//
	// **THIS CONSTANT IS ONE HALF OF A WIRE CONTRACT.** Its other half is
	// `AGENT_CANCELLED_REFUSAL` in `backend/app/jobs/cancel.py`, and it is not
	// a message: it is the token that tells the control plane the difference
	// between "this job never started" (`cancel_outcome = "dropped"`) and "it
	// was stopped mid-flight" (`interrupted`), which the console renders as two
	// different sentences. Changing it on one side only degrades every such
	// cancellation to `interrupted`, silently, with no test in this repository
	// red -- both sides of every test here are built from the same commit.
	ReasonCancelledBeforeStart RefusalReason = "cancelled_before_start"
	// ReasonQueueFull is Runner.Handle's own refusal, not the Verifier's: it is
	// returned after all five checks in Verify have passed, when the
	// Dispatcher's per-capability queue is already at depth. It lives here
	// because RefusalReason's vocabulary is meant to be closed and read in one
	// place, even though this member is never produced by Verify itself.
	ReasonQueueFull RefusalReason = "queue_full"
)

// MaxClockSkew is how far ahead of this agent's clock a job may claim to have
// been issued.
//
// Endpoints drift: a laptop resuming from suspend, a VM whose host clock
// stepped, a machine that has not reached an NTP server since boot. Without an
// allowance those endpoints refuse every job they are sent, which reads to an
// operator as a dead agent.
//
// 120s is deliberately far smaller than the shortest job TTL in the platform
// (net.sweep signs 600s, docs/modules/ipam/spec.md §5.2), so the allowance can
// never cover a whole job window: a job is never accepted after its own
// expires_at, whatever the clock says. Pinned by
// TestTheSkewAllowanceIsSmallerThanTheShortestJobTTLInThePlatform.
//
// It applies to issued_at ONLY. There is no allowance on expires_at: extending
// a job's life on a drifted endpoint is how a job runs after the operator
// believes it has lapsed.
const MaxClockSkew = 120 * time.Second

// MaxRefusalReasonLen bounds what this agent will write into the server's audit
// log for one refusal.
const MaxRefusalReasonLen = 160

// rfc3339 is app/jobs/canonical.py::rfc3339 -- whole seconds, always a literal
// Z. time.RFC3339 also parses offsets, which the server never emits; accepting
// a shape the signer cannot produce buys nothing and widens the input.
const rfc3339 = "2006-01-02T15:04:05Z"

// Verifier holds everything a verification decision depends on. Every field is
// required and none of them is settable from the environment (see
// internal/config): no operator-supplied value can weaken a check.
type Verifier struct {
	deviceID string
	key      ed25519.PublicKey
	nonces   NonceStore
	registry Registry
	skew     time.Duration
}

// ErrIncomplete is returned by New when the runner would be unable to verify.
var ErrIncomplete = errors.New("jobs: incomplete verifier")

// NewVerifier builds a Verifier or fails. There is no partially-configured
// Verifier and no zero value that does less checking: a runner that cannot
// verify must not exist rather than exist and skip.
func NewVerifier(deviceID string, key ed25519.PublicKey, nonces NonceStore, registry Registry) (*Verifier, error) {
	switch {
	case deviceID == "":
		return nil, fmt.Errorf("%w: no device id", ErrIncomplete)
	case len(key) != ed25519.PublicKeySize:
		return nil, fmt.Errorf("%w: job signing key is %d bytes, want %d",
			ErrIncomplete, len(key), ed25519.PublicKeySize)
	case nonces == nil:
		return nil, fmt.Errorf("%w: no nonce store (replay protection)", ErrIncomplete)
	case registry == nil:
		return nil, fmt.Errorf("%w: no capability registry", ErrIncomplete)
	}
	return &Verifier{
		deviceID: deviceID,
		key:      key,
		nonces:   nonces,
		registry: registry,
		skew:     MaxClockSkew,
	}, nil
}

// Verify runs the five checks in order and returns a VerifiedJob only if all of
// them pass.
//
// On refusal it returns (nil, reason, detail). `detail` is for the local log;
// what travels to the server is reason plus a bounded, non-echoing summary --
// see Runner.refuse.
func (v *Verifier) Verify(env *wire.Envelope, now time.Time) (VerifiedJob, RefusalReason, error) {
	if env == nil {
		return nil, ReasonMalformed, errors.New("nil envelope")
	}

	// ---- 1. Signature -----------------------------------------------------
	//
	// Over canonical_bytes of the `job` object AS RECEIVED. Canonicalising a
	// re-marshalled wire.JobPayload would silently drop any field a newer
	// control plane added -- and since the server signed the object including
	// that field, every agent in the fleet would start refusing every job at
	// once, with no test in this repository red. wire.Envelope.RawJob exists
	// for exactly this; do not replace it with a round-trip.
	msg, err := canonical.BytesFromRaw(env.RawJob)
	if err != nil {
		// The bytes the signature covers cannot be reproduced, so there is no
		// signature to check. That is a refusal, not a signature failure.
		return nil, ReasonMalformed, fmt.Errorf("job payload is not canonicalisable: %w", err)
	}
	sig, err := base64.StdEncoding.DecodeString(env.Sig)
	if err != nil {
		return nil, ReasonBadSignature, fmt.Errorf("sig is not standard base64: %w", err)
	}
	if !ed25519.Verify(v.key, msg, sig) {
		return nil, ReasonBadSignature, errors.New(
			"ed25519 verification failed against the key pinned at enrollment")
	}

	// Everything below this line is reading fields the control plane signed.
	job := env.Job

	// ---- 2. Device --------------------------------------------------------
	//
	// Exact string comparison. The server sends str(job.target_id) -- lowercase
	// canonical UUID text -- and an agent that case-folds or re-parses is
	// inventing an equivalence the server never promised. A signature is proof
	// the control plane wrote this job; it is not proof the job was written for
	// THIS endpoint.
	if job.Device != v.deviceID {
		return nil, ReasonWrongDevice, errors.New("job is addressed to another device")
	}

	// ---- 3. Freshness -----------------------------------------------------
	issuedAt, err := time.Parse(rfc3339, job.IssuedAt)
	if err != nil {
		return nil, ReasonMalformed, fmt.Errorf("issued_at is not rfc3339 whole-second Z: %w", err)
	}
	expiresAt, err := time.Parse(rfc3339, job.ExpiresAt)
	if err != nil {
		return nil, ReasonMalformed, fmt.Errorf("expires_at is not rfc3339 whole-second Z: %w", err)
	}
	if !expiresAt.After(issuedAt) {
		return nil, ReasonMalformed, errors.New("expires_at is not after issued_at")
	}
	if !now.Before(expiresAt) {
		return nil, ReasonExpired, fmt.Errorf("expired at %s", expiresAt.Format(rfc3339))
	}
	if issuedAt.After(now.Add(v.skew)) {
		return nil, ReasonNotYetValid, fmt.Errorf("issued at %s, beyond the %s skew allowance",
			issuedAt.Format(rfc3339), v.skew)
	}

	// ---- 4. Nonce ---------------------------------------------------------
	//
	// Reached only once the job is proven to be a genuine, in-window job for
	// this device, so a forged envelope cannot burn the nonce of a real one.
	// The store is keyed by expires_at because freshness already rejects
	// anything older -- an expired nonce cannot be usefully replayed, so it can
	// be pruned.
	if job.Nonce == "" {
		return nil, ReasonMalformed, errors.New("job carries no nonce")
	}
	if v.nonces.SeenOrRecord(job.Nonce, expiresAt) {
		return nil, ReasonReplayedNonce, errors.New("nonce already recorded")
	}

	// ---- 5. Type ----------------------------------------------------------
	//
	// Last, because it is the only check about this build rather than about
	// authenticity. An unregistered type is REFUSED, not failed: IPAM's
	// dispatcher reads a refusal as "this agent cannot do that" and stops
	// dispatching (docs/modules/ipam/spec.md §5.3), which is the correct
	// inference. Reporting `failed` instead would tell the operator the agent
	// tried and broke.
	if _, ok := v.registry.Lookup(job.Type); !ok {
		return nil, ReasonUnknownType, errors.New("no capability is registered for this job type")
	}

	// The ONE construction site of a verified job in this binary.
	return &verifiedJob{
		id:        job.ID,
		jobType:   job.Type,
		device:    job.Device,
		params:    job.Params,
		issuedAt:  issuedAt,
		expiresAt: expiresAt,
		nonce:     job.Nonce,
	}, "", nil
}
