package jobcancel_test

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/budget"
	"github.com/toeshee/it-suite/agent/internal/canonical"
	"github.com/toeshee/it-suite/agent/internal/capabilities"
	"github.com/toeshee/it-suite/agent/internal/capabilities/jobcancel"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// testDeviceID is this fixture agent's own device_id. The runner refuses a job
// addressed to anything else (`wrong_device`), so it is not decoration.
const testDeviceID = "6f1a2c34-5b6d-4e7f-8a9b-0c1d2e3f4a5b"

// A VerifiedJob a test can hand the capability directly.
//
// It satisfies jobs.VerifiedJob ONLY because this file is in package
// `jobcancel_test` and... it does not, and that is the point: the interface
// carries an unexported method, so no package outside internal/jobs can
// implement it. The capability is therefore exercised through the REAL runner
// below wherever a VerifiedJob is needed, which is the only way to reach it and
// exactly the guarantee invariant #1 rests on (internal/jobs/verified.go).

// fakeInterrupter records what it was asked to stop and answers a fixed
// outcome.
type fakeInterrupter struct {
	mu      sync.Mutex
	asked   []string
	outcome jobs.CancelOutcome
}

func (f *fakeInterrupter) Interrupt(id string) jobs.CancelOutcome {
	f.mu.Lock()
	f.asked = append(f.asked, id)
	f.mu.Unlock()
	return f.outcome
}

func (f *fakeInterrupter) seen() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.asked...)
}

func quiet() *slog.Logger {
	return slog.New(slog.NewTextHandler(discard{}, &slog.HandlerOptions{Level: slog.LevelError}))
}

type discard struct{}

func (discard) Write(p []byte) (int, error) { return len(p), nil }

// --------------------------------------------------------------------------- //
// Registration and queueing (§7 test 17's static half).
// --------------------------------------------------------------------------- //

func TestTheCapabilityIsInThisBuildsRegistry(t *testing.T) {
	h, ok := capabilities.Lookup(jobcancel.JobType)
	if !ok || h == nil {
		t.Fatalf("Lookup(%q) = (%v, %v); a build that cannot be interrupted must "+
			"say `unknown_type`, and this build claims it can", jobcancel.JobType, h, ok)
	}
}

func TestTheJobTypeIsTheOneTheServerRegisters(t *testing.T) {
	// The server's `app/jobs/registry.py` registers exactly this string. A
	// mismatch is not a compile error on either side; it is every cancel in the
	// fleet coming back `unknown_type`.
	if jobcancel.JobType != "job.cancel" {
		t.Fatalf("JobType = %q, want \"job.cancel\"", jobcancel.JobType)
	}
}

// --------------------------------------------------------------------------- //
// The capability's own behaviour, driven through the real runner.
// --------------------------------------------------------------------------- //

func TestCancelReportsWhatTheRunnerFound(t *testing.T) {
	for _, outcome := range []jobs.CancelOutcome{
		jobs.OutcomeInterrupted,
		jobs.OutcomeUnknown,
	} {
		t.Run(string(outcome), func(t *testing.T) {
			fake := &fakeInterrupter{outcome: outcome}
			svc := jobcancel.New(jobcancel.Options{Interrupter: fake, Logger: quiet()})
			h := newRunnerHarness(t, map[string]jobs.Handler{jobcancel.JobType: svc.Cancel})

			target := "9c3f0a11-0000-4000-8000-0000000000d1"
			h.deliver(t, jobcancel.JobType, `{"target_job_id":"`+target+`"}`, "n-"+string(outcome))

			res := h.awaitResult(t)
			if res["state"] != "succeeded" {
				t.Fatalf("state = %v; a cancel that CARRIED OUT its instruction succeeded, "+
					"even when the answer is `unknown`", res["state"])
			}
			body, _ := res["result"].(map[string]any)
			if body["outcome"] != string(outcome) {
				t.Fatalf("outcome = %v, want %v", body["outcome"], outcome)
			}
			if body["target_job_id"] != target {
				t.Fatalf("target_job_id = %v; it must be the SIGNED params' value verbatim, "+
					"because the server compares it against what it signed", body["target_job_id"])
			}
			if got := fake.seen(); len(got) != 1 || got[0] != target {
				t.Fatalf("the runner was asked to stop %v, want exactly [%s]", got, target)
			}
			if svc.Runs() != 1 {
				t.Fatalf("Runs() = %d, want 1", svc.Runs())
			}
		})
	}
}

func TestTheTargetIsReadOnlyFromTheSignedParams(t *testing.T) {
	// There is no other input. The capability takes a jobs.VerifiedJob and
	// nothing else, and `Params()` are the exact bytes the server signed --
	// so this test is really an assertion that the code reads no second source.
	fake := &fakeInterrupter{outcome: jobs.OutcomeUnknown}
	svc := jobcancel.New(jobcancel.Options{Interrupter: fake, Logger: quiet()})
	h := newRunnerHarness(t, map[string]jobs.Handler{jobcancel.JobType: svc.Cancel})

	signed := "9c3f0a11-0000-4000-8000-0000000000d2"
	h.deliver(t, jobcancel.JobType,
		`{"target_job_id":"`+signed+`","also":"9c3f0a11-0000-4000-8000-0000000000ff"}`,
		"n-extra")

	h.awaitResult(t)
	if got := fake.seen(); len(got) != 1 || got[0] != signed {
		t.Fatalf("asked to stop %v; only the signed `target_job_id` may be read", got)
	}
}

func TestAnEmptyOrSelfNamingTargetIsRefused(t *testing.T) {
	for name, params := range map[string]string{
		"empty":   `{"target_job_id":""}`,
		"missing": `{}`,
	} {
		t.Run(name, func(t *testing.T) {
			fake := &fakeInterrupter{outcome: jobs.OutcomeUnknown}
			svc := jobcancel.New(jobcancel.Options{Interrupter: fake, Logger: quiet()})
			h := newRunnerHarness(t, map[string]jobs.Handler{jobcancel.JobType: svc.Cancel})
			h.deliver(t, jobcancel.JobType, params, "n-"+name)

			res := h.awaitResult(t)
			if res["state"] != "refused" {
				t.Fatalf("state = %v, want refused", res["state"])
			}
			if got := fake.seen(); len(got) != 0 {
				t.Fatalf("the runner was asked to stop %v for a cancel naming nothing", got)
			}
		})
	}
}

func TestACancelThatNamesItselfIsRefused(t *testing.T) {
	// The one shape that could loop: the runner would be asked to interrupt the
	// very job doing the asking, whose context is the one this capability is
	// running under. `9c3f0a11-0000-4000-8000-00000000c0de` is the id the
	// signer below stamps on every fixture frame.
	fake := &fakeInterrupter{outcome: jobs.OutcomeInterrupted}
	svc := jobcancel.New(jobcancel.Options{Interrupter: fake, Logger: quiet()})
	h := newRunnerHarness(t, map[string]jobs.Handler{jobcancel.JobType: svc.Cancel})
	h.deliver(t, jobcancel.JobType,
		`{"target_job_id":"9c3f0a11-0000-4000-8000-00000000c0de"}`, "n-self")

	res := h.awaitResult(t)
	if res["state"] != "refused" {
		t.Fatalf("state = %v, want refused", res["state"])
	}
	if got := fake.seen(); len(got) != 0 {
		t.Fatalf("the runner was asked to stop %v for a cancel naming itself", got)
	}
}

func TestAnUnboundCapabilityRefusesAndNamesWhy(t *testing.T) {
	// The answer matters: `unknown_type` would tell the operator this build can
	// never be interrupted, which would be false. A refusal naming the missing
	// binding is a fact they can act on.
	svc := jobcancel.New(jobcancel.Options{Logger: quiet()})
	h := newRunnerHarness(t, map[string]jobs.Handler{jobcancel.JobType: svc.Cancel})
	h.deliver(t, jobcancel.JobType,
		`{"target_job_id":"9c3f0a11-0000-4000-8000-0000000000d3"}`, "n-unbound")

	res := h.awaitResult(t)
	if res["state"] != "refused" {
		t.Fatalf("state = %v, want refused", res["state"])
	}
	body, _ := res["result"].(map[string]any)
	reason, _ := body["reason"].(string)
	if !strings.Contains(reason, "never bound") {
		t.Fatalf("reason = %q; it must name the missing binding", reason)
	}
}

func TestBindMakesAnUnboundServiceWork(t *testing.T) {
	fake := &fakeInterrupter{outcome: jobs.OutcomeInterrupted}
	svc := jobcancel.New(jobcancel.Options{Logger: quiet()})
	svc.Bind(fake)
	h := newRunnerHarness(t, map[string]jobs.Handler{jobcancel.JobType: svc.Cancel})
	h.deliver(t, jobcancel.JobType,
		`{"target_job_id":"9c3f0a11-0000-4000-8000-0000000000d4"}`, "n-bound")

	res := h.awaitResult(t)
	if res["state"] != "succeeded" {
		t.Fatalf("state = %v, want succeeded", res["state"])
	}
}

// --------------------------------------------------------------------------- //
// §7 TEST 19 -- THE OLD FLEET. The single most important test here.
// --------------------------------------------------------------------------- //

func TestAnAgentWithoutTheCapabilityRefusesAValidCancelAsUnknownType(t *testing.T) {
	// A registry built WITHOUT the cancel capability is exactly what every
	// agent in the field is today, and this asserts what one does when a new
	// control plane sends it a perfectly valid, correctly signed `job.cancel`.
	//
	// THE ANSWER IS THE WHOLE COMPATIBILITY ARGUMENT. It refuses `unknown_type`
	// BEFORE anything runs and it SENDS A FRAME -- loudly, in seconds, on the
	// record. A new wire `kind` would instead have been dropped in silence
	// (`wire.Decode` returns ErrKind and the runner answers nothing), and the
	// control plane would have had to tell "old agent" from "job already
	// finished" by timeout, i.e. by guessing.
	h := newRunnerHarness(t, map[string]jobs.Handler{"hello": func(context.Context, jobs.VerifiedJob) (any, error) {
		return nil, nil
	}})
	h.deliver(t, jobcancel.JobType,
		`{"target_job_id":"9c3f0a11-0000-4000-8000-0000000000d5"}`, "n-oldfleet")

	res := h.awaitResult(t)
	if res["state"] != "refused" {
		t.Fatalf("an old agent answered %v; the control plane reads `refused` "+
			"with reason `unknown_type` as 'this endpoint cannot be interrupted'", res["state"])
	}
	body, _ := res["result"].(map[string]any)
	reason, _ := body["reason"].(string)
	if !strings.HasPrefix(reason, "unknown_type") {
		t.Fatalf("reason = %q; `app/jobs/cancel.py::handle_cancel_refusal` matches the "+
			"HEAD of this string to write `cancel_outcome = unsupported`", reason)
	}
	// And nothing on the old agent moved: no job_status, no side effect.
	for _, f := range h.sender.frames() {
		if f["kind"] == "job_status" {
			t.Fatalf("the old agent reported %v for a type it cannot run", f)
		}
	}
}

// --------------------------------------------------------------------------- //
// Idle budget.
// --------------------------------------------------------------------------- //

func TestFalsifier_AnIdleAgentHoldsNothingForThisCapability(t *testing.T) {
	// FALSIFYING OBSERVATION: an allocation, or a goroutine, for a capability
	// that has not been asked to do anything. Registration is a map insert and
	// Bind stores one pointer; neither may open a file, dial anything or spawn.
	fake := &fakeInterrupter{outcome: jobs.OutcomeUnknown}
	r := budget.AssertNoAllocations(t, "job.cancel dormant", func() func() {
		svc := jobcancel.New(jobcancel.Options{Interrupter: fake, Logger: quiet()})
		return func() {
			if svc.Runs() != 0 {
				panic("the dormancy measurement ran a cancel")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("dormant: %d iterations, %d allocs/op, %d B/op, goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)
}

func TestFalsifier_BindStartsNothing(t *testing.T) {
	fake := &fakeInterrupter{outcome: jobs.OutcomeUnknown}
	svc := jobcancel.New(jobcancel.Options{Logger: quiet()})
	r := budget.Measure("job.cancel bind", func() func() {
		return func() { svc.Bind(fake) }
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	if r.AllocsPerOp != 0 {
		t.Fatalf("Bind allocates %d per call (%d bytes); it stores one pointer",
			r.AllocsPerOp, r.BytesPerOp)
	}
}

// --------------------------------------------------------------------------- //
// The harness: a REAL runner, because a VerifiedJob cannot be forged.
// --------------------------------------------------------------------------- //

type runnerHarness struct {
	runner *jobs.Runner
	sender *recordingSender
	signer testSigner
}

func newRunnerHarness(t *testing.T, registry map[string]jobs.Handler) *runnerHarness {
	t.Helper()
	s := newTestSigner(t)
	sender := &recordingSender{}
	r, err := jobs.New(jobs.Options{
		BaseContext: t.Context(),
		DeviceID:    testDeviceID,
		SigningKey:  s.pub,
		Nonces:      &permissiveNonces{seen: map[string]bool{}},
		Registry:    mapRegistry(registry),
		Sender:      sender,
		Logger:      quiet(),
	})
	if err != nil {
		t.Fatal(err)
	}
	return &runnerHarness{runner: r, sender: sender, signer: s}
}

func (h *runnerHarness) deliver(t *testing.T, jobType, params, nonce string) {
	t.Helper()
	h.runner.Handle(context.Background(), h.signer.frame(t, jobType, params, nonce))
}

func (h *runnerHarness) awaitResult(t *testing.T) map[string]any {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		for _, f := range h.sender.frames() {
			if f["kind"] == "job_result" {
				return f
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("no job_result within 5s; frames: %v", h.sender.frames())
		}
		time.Sleep(2 * time.Millisecond)
	}
}

type mapRegistry map[string]jobs.Handler

func (m mapRegistry) Lookup(jobType string) (jobs.Handler, bool) {
	h, ok := m[jobType]
	return h, ok
}

type permissiveNonces struct {
	mu   sync.Mutex
	seen map[string]bool
}

func (p *permissiveNonces) SeenOrRecord(nonce string, _ time.Time) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.seen[nonce] {
		return true
	}
	p.seen[nonce] = true
	return false
}

type recordingSender struct {
	mu   sync.Mutex
	sent []map[string]any
}

func (r *recordingSender) Send(_ context.Context, msg any) error {
	// Through wire.Encode, so the frames a test asserts on are the frames that
	// would have gone on the wire -- `kind` and `v` included, and produced by
	// the codec rather than by this fixture's guess about them.
	b, err := wire.Encode(msg)
	if err != nil {
		return err
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return err
	}
	r.mu.Lock()
	r.sent = append(r.sent, m)
	r.mu.Unlock()
	return nil
}

func (r *recordingSender) frames() []map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]map[string]any(nil), r.sent...)
}

// --------------------------------------------------------------------------- //
// The signer. THIS FILE CANNOT FORGE A VerifiedJob AND MUST NOT TRY.
// --------------------------------------------------------------------------- //
//
// `jobs.VerifiedJob` carries an unexported method, so no package outside
// internal/jobs can implement it -- that is invariant #1 made structural on the
// endpoint (internal/jobs/verified.go). The only way to hand this capability a
// job is therefore to sign a real envelope and put it through the real runner,
// which is what every test above does. That is not test ceremony: it is the
// same path a control plane's cancel takes, checks and all.

type testSigner struct {
	pub  ed25519.PublicKey
	priv ed25519.PrivateKey
}

func newTestSigner(t *testing.T) testSigner {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return testSigner{pub: pub, priv: priv}
}

// frame renders the `job` member the way the gateway does and signs the
// CANONICAL BYTES of exactly those bytes -- never a re-marshalled struct, for
// the reason internal/canonical exists.
func (s testSigner) frame(t *testing.T, jobType, params, nonce string) []byte {
	t.Helper()
	now := time.Now().UTC()
	job := fmt.Sprintf(
		`{"id":%q,"type":%q,"device":%q,"params":%s,"issued_at":%q,"expires_at":%q,"nonce":%q}`,
		"9c3f0a11-0000-4000-8000-00000000c0de", jobType, testDeviceID, params,
		now.Add(-time.Second).Format("2006-01-02T15:04:05Z"),
		now.Add(10*time.Minute).Format("2006-01-02T15:04:05Z"),
		nonce,
	)
	msg, err := canonical.BytesFromRaw(json.RawMessage(job))
	if err != nil {
		t.Fatalf("canonicalising the fixture failed: %v", err)
	}
	sig := base64.StdEncoding.EncodeToString(ed25519.Sign(s.priv, msg))
	return []byte(fmt.Sprintf(`{"v":1,"kind":"job","job":%s,"sig":%q}`, job, sig))
}
