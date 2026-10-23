package jobs

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/canonical"
)

// memNonces is the Task 7 stand-in for Task 8's persisted store. Replay across
// a process restart is Task 8's test, because it is Task 8's file format.
type memNonces struct{ seen map[string]bool }

func newMemNonces() *memNonces { return &memNonces{seen: map[string]bool{}} }

func (m *memNonces) SeenOrRecord(nonce string, _ time.Time) bool {
	if m.seen[nonce] {
		return true
	}
	m.seen[nonce] = true
	return false
}

// harness assembles a runner whose only capability is the spy.
type harness struct {
	spy    *spyCapability
	sender *fakeSender
	runner *Runner
	logs   *logBuf
	nonces *memNonces
}

func newHarness(t *testing.T, s signer) *harness {
	t.Helper()
	spy := newSpy(t)
	sender := &fakeSender{}
	logs := &logBuf{}
	nonces := newMemNonces()
	r, err := New(Options{
		// The unit-test stand-in for main.go's process context: the default
		// Dispatcher New builds requires one, so that no caller can end up
		// running jobs under a per-connection context (dispatcher.go).
		BaseContext: t.Context(),
		DeviceID:    testDeviceID,
		SigningKey:  s.pub,
		Nonces:      nonces,
		// script.run is registered TOO, and that is load-bearing: it makes the
		// tampered-type case an escalation to a capability this build actually
		// has. Registering only "hello" would have let that case pass on the
		// type check when the signature check was deleted -- observed, and
		// exactly the fixture blindness this file exists to avoid.
		Registry: mapRegistry{"hello": spy.handle, "script.run": spy.handle},
		Sender:   sender,
		Now:      func() time.Time { return baseNow },
		Logger:   slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug})),
	})
	if err != nil {
		t.Fatal(err)
	}
	return &harness{spy: spy, sender: sender, runner: r, logs: logs, nonces: nonces}
}

// ---------------------------------------------------------------------------
// The happy path. Without this one, every negative case below is vacuous.
// ---------------------------------------------------------------------------

func TestAValidSignedJobRunsExactlyOnce(t *testing.T) {
	s := newSigner(t)
	h := newHarness(t, s)

	h.runner.Handle(context.Background(), s.frame(t, validJob("nonce-ok")))

	h.spy.awaitCount(t, 1, 2*time.Second)
	h.sender.awaitAtLeast(t, 2, 2*time.Second)
	if got := h.sender.kinds(); len(got) != 2 || got[0] != "job_status" || got[1] != "job_result" {
		t.Fatalf("frames %v; want job_status then job_result", got)
	}
	if st := h.sender.only(t, "job_status"); st["state"] != "running" {
		t.Fatalf("job_status state %v; protocol.py types it as the literal \"running\"", st["state"])
	}
	res := h.sender.only(t, "job_result")
	if res["state"] != "succeeded" {
		t.Fatalf("job_result state %v, want succeeded", res["state"])
	}
	body, _ := res["result"].(map[string]any)
	if body["greeting"] != "hello from the control plane" {
		t.Fatalf("result %v; the capability's return value must reach the server at the top level", res["result"])
	}
	if got := h.spy.seen[0].Type(); got != "hello" {
		t.Fatalf("capability saw type %q", got)
	}
}

// ---------------------------------------------------------------------------
// The five checks, one falsifiable case each (plus the shapes that must not
// reach a check at all)
// ---------------------------------------------------------------------------

func TestEveryRefusalIsMadeByItsOwnCheck(t *testing.T) {
	s := newSigner(t)
	otherKey := newSigner(t)

	cases := []struct {
		name string
		// frame builds the inbound bytes. Each case differs from `valid` in
		// exactly ONE property, so the check named in `reason` is the only one
		// that can refuse it -- which is what makes deleting that check turn
		// this case red rather than leaving it green on a neighbour's back.
		frame  func(t *testing.T) []byte
		reason RefusalReason
		// wantReason, when set, pins the EXACT reason string, so the case
		// cannot pass because some other code path produced the same
		// RefusalReason.
		wantReason string
		// why names the check whose deletion this case falsifies.
		why string
	}{
		{
			name: "tampered_params",
			// Signature covers canonical bytes of the whole job object, so a
			// single digit inside params invalidates it. Nothing else changes:
			// device, window, nonce and type all still pass.
			frame: func(t *testing.T) []byte {
				f := string(s.frame(t, validJob("n-tamper-params")))
				out := strings.Replace(f, `"rate_pps":50`, `"rate_pps":51`, 1)
				if out == f {
					t.Fatal("fixture did not tamper anything; the case would be vacuous")
				}
				return []byte(out)
			},
			reason: ReasonBadSignature,
			why:    "signature",
		},
		{
			name: "tampered_type_escalation",
			// The escalation shape: a `hello` job rewritten to `script.run`
			// with the server's signature left in place.
			frame: func(t *testing.T) []byte {
				f := string(s.frame(t, validJob("n-tamper-type")))
				out := strings.Replace(f, `"type":"hello"`, `"type":"script.run"`, 1)
				if out == f {
					t.Fatal("fixture did not tamper anything")
				}
				return []byte(out)
			},
			reason: ReasonBadSignature,
			why:    "signature",
		},
		{
			name: "absent_signature",
			frame: func(t *testing.T) []byte {
				job := validJob("n-nosig")
				return []byte(`{"v":1,"kind":"job","job":` + job + `,"sig":""}`)
			},
			reason: ReasonBadSignature,
			why:    "signature (refused at the wire layer; see the sub-test below)",
		},
		{
			name: "signed_by_an_unpinned_key",
			// Structurally perfect, signed by a key this agent never pinned.
			frame:  func(t *testing.T) []byte { return otherKey.frame(t, validJob("n-otherkey")) },
			reason: ReasonBadSignature,
			why:    "signature",
		},
		{
			name: "signed_over_non_canonical_bytes",
			// The exact bug a naive Go port produces: sign json.dumps with
			// default separators instead of canonical_bytes. Every other
			// property of the job is correct.
			frame: func(t *testing.T) []byte {
				job := validJob("n-noncanon")
				spaced := nonCanonicalRender(t, job)
				if string(spaced) == mustCanonical(t, job) {
					t.Fatal("the non-canonical rendering is identical to the canonical one")
				}
				sig := base64.StdEncoding.EncodeToString(ed25519.Sign(s.priv, spaced))
				return []byte(`{"v":1,"kind":"job","job":` + job + `,"sig":"` + sig + `"}`)
			},
			reason: ReasonBadSignature,
			why:    "signature",
		},
		{
			name: "addressed_to_another_device",
			// Signed by the REAL key over its own contents: the signature is
			// valid and the job is fresh. Only the device check can refuse it.
			// This is the case a signature-only agent executes.
			frame: func(t *testing.T) []byte {
				return s.frame(t, jobText(
					"9c3f0a11-0000-4000-8000-000000000002", "hello", otherDevice,
					`{"message":"for someone else"}`,
					ts(baseNow.Add(-time.Second)), ts(baseNow.Add(10*time.Minute)),
					"n-otherdevice"))
			},
			reason: ReasonWrongDevice,
			why:    "device",
		},
		{
			name: "expired",
			frame: func(t *testing.T) []byte {
				return s.frame(t, jobText(
					"9c3f0a11-0000-4000-8000-000000000003", "hello", testDeviceID, `{}`,
					ts(baseNow.Add(-10*time.Minute)), ts(baseNow.Add(-time.Second)),
					"n-expired"))
			},
			reason: ReasonExpired,
			why:    "freshness",
		},
		{
			name: "issued_beyond_the_skew_allowance",
			frame: func(t *testing.T) []byte {
				return s.frame(t, jobText(
					"9c3f0a11-0000-4000-8000-000000000004", "hello", testDeviceID, `{}`,
					ts(baseNow.Add(10*time.Minute)), ts(baseNow.Add(70*time.Minute)),
					"n-future"))
			},
			reason: ReasonNotYetValid,
			why:    "freshness",
		},
		{
			name: "unregistered_job_type",
			// Correctly signed, correctly addressed, fresh, unreplayed -- and
			// this build has no handler for it.
			frame: func(t *testing.T) []byte {
				return s.frame(t, jobText(
					"9c3f0a11-0000-4000-8000-000000000005", "patch.apply", testDeviceID, `{}`,
					ts(baseNow.Add(-time.Second)), ts(baseNow.Add(10*time.Minute)),
					"n-unknown-type"))
			},
			reason:     ReasonUnknownType,
			wantReason: "unknown_type: no capability is registered for this job type",
			why:        "type",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := newHarness(t, s)
			h.runner.Handle(context.Background(), tc.frame(t))

			h.spy.assertDidNotRun(t, tc.name+" (the "+tc.why+" check owns this case)")

			if tc.name == "absent_signature" {
				// wire.Decode refuses an empty `sig` before a job identity
				// exists, so there is nothing to address a job_result to. The
				// operator-visible artefact is the log line; assert on it
				// rather than pretending a refusal was sent.
				if kinds := h.sender.kinds(); len(kinds) != 0 {
					t.Fatalf("frames %v emitted for a frame that never parsed", kinds)
				}
				if !strings.Contains(h.logs.String(), "empty sig") {
					t.Fatalf("nothing in the log names the empty signature:\n%s", h.logs.String())
				}
				return
			}

			res := h.sender.only(t, "job_result")
			if res["state"] != "refused" {
				t.Fatalf("state %v, want refused (gateway/dispatch.py reads refused specially)", res["state"])
			}
			got := reasonOf(t, res)
			if !strings.HasPrefix(got, string(tc.reason)) {
				t.Fatalf("reason %q, want it to start with %q", got, tc.reason)
			}
			if tc.wantReason != "" && got != tc.wantReason {
				// The EXACT string, not a prefix. Two places can report
				// unknown_type -- the type check in Verify and the runner's
				// defensive re-lookup -- and a prefix assertion cannot tell
				// which one fired, so deleting the check in Verify would leave
				// this test green on the fallback's back.
				t.Fatalf("reason %q, want exactly %q (a different code path answered)", got, tc.wantReason)
			}
			for _, k := range h.sender.kinds() {
				if k == "job_status" {
					t.Fatal("job_status{running} was sent for a refused job: the server was told work started")
				}
			}
		})
	}
}

// nonCanonicalRender re-renders a job object with Python's DEFAULT separators
// (", " and ": "), which is what json.dumps(job) produces when somebody forgets
// canonical_bytes.
func nonCanonicalRender(t *testing.T, job string) []byte {
	t.Helper()
	var v map[string]any
	dec := json.NewDecoder(strings.NewReader(job))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		t.Fatal(err)
	}
	c, err := canonical.Bytes(v)
	if err != nil {
		t.Fatal(err)
	}
	// Only the separators differ; the key order and the escaping do not. That
	// is precisely the near-miss C5 warns about.
	out := strings.ReplaceAll(string(c), `":`, `": `)
	out = strings.ReplaceAll(out, `,"`, `, "`)
	return []byte(out)
}

func mustCanonical(t *testing.T, job string) string {
	t.Helper()
	b, err := canonical.BytesFromRaw(json.RawMessage(job))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// ---------------------------------------------------------------------------
// Replay
// ---------------------------------------------------------------------------

func TestAReplayedNonceIsRefusedAndTheCapabilityRunsOnlyOnce(t *testing.T) {
	s := newSigner(t)
	h := newHarness(t, s)
	frame := s.frame(t, validJob("n-replay"))

	h.runner.Handle(context.Background(), frame)
	h.runner.Handle(context.Background(), frame)

	// The falsifying observation for the nonce check is the SECOND invocation,
	// not the returned error: a runner that verified the signature and ran the
	// job twice would satisfy every other assertion in this file.
	//
	// Anchored on BOTH deliveries' own terminal frames, not sampled the moment
	// the spy's counter first looks right: since Task 6, a genuine job is
	// dispatched to a worker goroutine rather than run inline, so if the nonce
	// check were broken and let the SECOND delivery dispatch too, the counter
	// could read 1 for a moment before a second run made it 2 -- a false PASS
	// on exactly the property this test exists to falsify. Waiting for two
	// job_result frames total means whichever path each delivery actually
	// took (synchronous refusal, or a dispatch reported later) has already
	// concluded before the counter or the frame list below is read.
	h.sender.awaitKindCount(t, "job_result", 2, 2*time.Second)
	if n := h.spy.count(); n != 1 {
		t.Fatalf("capability ran %d times for one job replayed twice; want 1", n)
	}
	var refusals int
	for _, f := range h.sender.frames() {
		if f["kind"] == "job_result" && f["state"] == "refused" {
			refusals++
			if got := reasonOf(t, f); !strings.HasPrefix(got, string(ReasonReplayedNonce)) {
				t.Fatalf("reason %q, want %q", got, ReasonReplayedNonce)
			}
		}
	}
	if refusals != 1 {
		t.Fatalf("%d refusals, want exactly 1", refusals)
	}
}

func TestAForgedEnvelopeCannotPoisonALegitimateNonce(t *testing.T) {
	// The nonce is recorded only after the signature, device and freshness
	// checks pass. If it were recorded first, anyone able to reach the socket
	// could pre-burn the nonce of a job the server is about to send, and the
	// genuine job would then be refused as a replay -- a denial of service
	// built out of the replay defence.
	s := newSigner(t)
	other := newSigner(t)
	h := newHarness(t, s)

	h.runner.Handle(context.Background(), other.frame(t, validJob("n-shared")))
	if n := h.spy.count(); n != 0 {
		t.Fatalf("the forged job ran (%d)", n)
	}

	h.runner.Handle(context.Background(), s.frame(t, validJob("n-shared")))
	// Bounded wait: this job is genuine and dispatched asynchronously (Task 6).
	h.spy.awaitCount(t, 1, 2*time.Second)
}

// ---------------------------------------------------------------------------
// Frames that must not reach a check at all
// ---------------------------------------------------------------------------

func TestFramesThatAreNotAV1JobAreDroppedWithoutAnswer(t *testing.T) {
	s := newSigner(t)
	job := validJob("n-shape")
	valid := string(s.frame(t, job))

	cases := []struct {
		name, frame, wantLog string
	}{
		{
			name:    "protocol_version_2",
			frame:   strings.Replace(valid, `"v":1`, `"v":2`, 1),
			wantLog: "unsupported protocol version",
		},
		{
			// There is exactly one server -> agent kind (protocol.py:150-153).
			// A second instruction path is the thing invariant #1 exists to
			// prevent, so an unknown kind is refused rather than explored.
			name:    "a_second_instruction_path",
			frame:   strings.Replace(valid, `"kind":"job"`, `"kind":"policy"`, 1),
			wantLog: "unknown message kind",
		},
		{
			name:    "truncated_json",
			frame:   valid[:len(valid)/2],
			wantLog: "malformed",
		},
		{
			name:    "not_json_at_all",
			frame:   "\x00\x01\x02 not json",
			wantLog: "malformed",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := newHarness(t, s)
			h.runner.Handle(context.Background(), []byte(tc.frame))
			h.spy.assertDidNotRun(t, tc.name)
			if kinds := h.sender.kinds(); len(kinds) != 0 {
				t.Fatalf("frames %v emitted for a frame with no addressable job", kinds)
			}
			if !strings.Contains(h.logs.String(), tc.wantLog) {
				t.Fatalf("log does not mention %q:\n%s", tc.wantLog, h.logs.String())
			}
		})
	}
}

// ---------------------------------------------------------------------------
// The additive-field survival property RawJob exists for
// ---------------------------------------------------------------------------

func TestAJobCarryingAFieldThisAgentDoesNotKnowStillVerifies(t *testing.T) {
	// docs/kernel/wire-protocol/README.md: additive, or versioned, or it is a
	// break. A newer control plane that adds a field to JobPayload signs the
	// object INCLUDING that field. Canonicalising a re-marshalled JobPayload
	// would drop it and produce a signature mismatch on every deployed agent
	// at once -- an outage with no red test anywhere. Verification therefore
	// canonicalises wire.Envelope.RawJob, the bytes as received.
	s := newSigner(t)
	h := newHarness(t, s)
	job := `{"id":"9c3f0a11-0000-4000-8000-00000000000a","type":"hello","device":"` + testDeviceID +
		`","params":{"message":"hi"},"issued_at":"` + ts(baseNow.Add(-time.Second)) +
		`","expires_at":"` + ts(baseNow.Add(10*time.Minute)) +
		`","nonce":"n-additive","priority":7,"trace_id":"abc"}`

	h.runner.Handle(context.Background(), s.frame(t, job))

	h.spy.awaitCount(t, 1, 2*time.Second)
}

func TestParamsReachTheCapabilityAsTheExactReceivedBytes(t *testing.T) {
	s := newSigner(t)
	h := newHarness(t, s)
	params := `{"z":1,"a":{"nested":[1,2,3]},"s":"a <b> & c"}`
	job := jobText("9c3f0a11-0000-4000-8000-00000000000b", "hello", testDeviceID, params,
		ts(baseNow.Add(-time.Second)), ts(baseNow.Add(10*time.Minute)), "n-params")

	h.runner.Handle(context.Background(), s.frame(t, job))

	h.spy.awaitCount(t, 1, 2*time.Second)
	if got := string(h.spy.seen[0].Params()); got != params {
		t.Fatalf("params delivered as %s, want the received bytes %s", got, params)
	}
}

// ---------------------------------------------------------------------------
// Runner behaviour around the capability
// ---------------------------------------------------------------------------

func TestAPanickingCapabilityIsReportedFailedAndTheAgentSurvives(t *testing.T) {
	s := newSigner(t)
	h := newHarness(t, s)
	h.spy.panicWith = "capability exploded"

	h.runner.Handle(context.Background(), s.frame(t, validJob("n-panic")))

	res := h.sender.only(t, "job_result")
	if res["state"] != "failed" {
		t.Fatalf("state %v, want failed", res["state"])
	}
	body, _ := res["result"].(map[string]any)
	if msg, _ := body["error"].(string); !strings.Contains(msg, "panic") {
		t.Fatalf("result %v does not name the panic", res["result"])
	}
	if strings.Contains(h.logs.String(), "goroutine") == false {
		t.Log("no stack in the local log; not fatal, but the operator loses the trace")
	}
	// And the runner still works afterwards.
	h.spy.panicWith = nil
	h.runner.Handle(context.Background(), s.frame(t, validJob("n-after-panic")))
	h.spy.awaitCount(t, 2, 2*time.Second)
}

func TestTheCapabilityContextIsBoundedByExpiresAt(t *testing.T) {
	s := newSigner(t)
	spy := newSpy(t)
	sender := &fakeSender{}
	var deadline time.Time
	var hasDeadline bool
	r, err := New(Options{
		BaseContext: t.Context(),
		DeviceID:    testDeviceID,
		SigningKey:  s.pub,
		Nonces:      newMemNonces(),
		Registry: mapRegistry{"hello": func(ctx context.Context, j VerifiedJob) (any, error) {
			deadline, hasDeadline = ctx.Deadline()
			return spy.handle(ctx, j)
		}},
		Sender: sender,
		Now:    func() time.Time { return baseNow },
		Logger: slog.New(slog.NewTextHandler(&logBuf{}, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	r.Handle(context.Background(), s.frame(t, validJob("n-deadline")))

	// Waits for the job_result: the capability closure above (which writes
	// deadline/hasDeadline) now runs on a Dispatcher worker goroutine, so
	// reading those two variables without first synchronizing against its
	// completion is a data race (they are written with no lock of their own).
	// only()'s poll takes fakeSender's mutex on every iteration, and the last
	// such Lock happens-after the Send call the capability's own completion
	// triggers, which is what makes the plain reads below safe.
	sender.only(t, "job_result")

	if !hasDeadline {
		t.Fatal("the capability ran with no deadline: a job that outlives expires_at can never be reported")
	}
	if want := baseNow.Add(10 * time.Minute); !deadline.Equal(want) {
		t.Fatalf("deadline %v, want expires_at %v", deadline, want)
	}
}

func TestANonObjectCapabilityResultIsReportedFailedNotDropped(t *testing.T) {
	// JobResultMsg.result is an object server-side. A capability returning a
	// bare list or string would produce a message the gateway drops at
	// parse_agent_message with nothing but a logger.warning, so the job would
	// hang until it expired. Fail it loudly instead.
	s := newSigner(t)
	h := newHarness(t, s)
	h.spy.result = []any{"not", "an", "object"}

	h.runner.Handle(context.Background(), s.frame(t, validJob("n-badresult")))

	res := h.sender.only(t, "job_result")
	if res["state"] != "failed" {
		t.Fatalf("state %v, want failed", res["state"])
	}
}

func TestANilCapabilityResultSucceedsWithNoResultBody(t *testing.T) {
	s := newSigner(t)
	h := newHarness(t, s)
	h.spy.result = nil

	h.runner.Handle(context.Background(), s.frame(t, validJob("n-nilresult")))

	res := h.sender.only(t, "job_result")
	if res["state"] != "succeeded" {
		t.Fatalf("state %v, want succeeded", res["state"])
	}
}

// ---------------------------------------------------------------------------
// Construction guards: a Runner that cannot verify must not exist
// ---------------------------------------------------------------------------

func TestARunnerCannotBeBuiltWithoutTheThingsVerificationNeeds(t *testing.T) {
	s := newSigner(t)
	base := func() Options {
		return Options{
			BaseContext: t.Context(),
			DeviceID:    testDeviceID,
			SigningKey:  s.pub,
			Nonces:      newMemNonces(),
			Registry:    mapRegistry{},
			Sender:      &fakeSender{},
		}
	}
	cases := map[string]func(*Options){
		"no signing key":    func(o *Options) { o.SigningKey = nil },
		"short signing key": func(o *Options) { o.SigningKey = ed25519.PublicKey{1, 2, 3} },
		"no device id":      func(o *Options) { o.DeviceID = "" },
		"no nonce store":    func(o *Options) { o.Nonces = nil },
		"no registry":       func(o *Options) { o.Registry = nil },
		"no sender":         func(o *Options) { o.Sender = nil },
		// A Runner with no base context would build a Dispatcher with none
		// either, and the only context left to run a job under would be the
		// caller's -- the transport's, per connection. See
		// DispatcherOptions.BaseContext.
		"no base context": func(o *Options) { o.BaseContext = nil },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			o := base()
			mutate(&o)
			if _, err := New(o); err == nil {
				t.Fatalf("New succeeded with %s: a runner missing that cannot enforce invariant #1", name)
			}
		})
	}
	if _, err := New(base()); err != nil {
		t.Fatalf("New rejected a complete configuration: %v", err)
	}
}

func TestTheSkewAllowanceIsSmallerThanTheShortestJobTTLInThePlatform(t *testing.T) {
	// net.sweep signs a 600s TTL (docs/modules/ipam/spec.md §5.2). A skew
	// allowance at or above the shortest TTL would mean a job could be accepted
	// after its own window on a drifted clock.
	const shortestTTL = 600 * time.Second
	if MaxClockSkew >= shortestTTL {
		t.Fatalf("MaxClockSkew %v is not smaller than the shortest job TTL %v", MaxClockSkew, shortestTTL)
	}
}

func TestSignatureIsCheckedBeforeAnythingElseIsBelieved(t *testing.T) {
	// An unsigned envelope's `device`, `expires_at` and `type` are all
	// attacker-controlled. They must not determine even the REPORTED reason:
	// this frame is expired AND addressed elsewhere AND of an unknown type, and
	// the only honest thing to say about it is that the signature is wrong.
	s := newSigner(t)
	other := newSigner(t)
	h := newHarness(t, s)

	frame := other.frame(t, jobText(
		"9c3f0a11-0000-4000-8000-00000000000c", "patch.apply", otherDevice, `{}`,
		ts(baseNow.Add(-time.Hour)), ts(baseNow.Add(-30*time.Minute)), "n-multi"))
	h.runner.Handle(context.Background(), frame)

	h.spy.assertDidNotRun(t, "a frame failing every check")
	got := reasonOf(t, h.sender.only(t, "job_result"))
	if !strings.HasPrefix(got, string(ReasonBadSignature)) {
		t.Fatalf("reason %q; a frame whose signature is invalid must be reported as %q "+
			"and nothing else -- the other fields were never authenticated",
			got, ReasonBadSignature)
	}
}

func TestARefusalNeverEchoesJobSuppliedText(t *testing.T) {
	// gateway/dispatch.py hands result["reason"] to JobEngine.refuse, which
	// writes it into the hash-chained audit log. Echoing params there would let
	// whoever can shape a job write into an operator's audit view.
	s := newSigner(t)
	h := newHarness(t, s)
	poison := "SUPER-SECRET-CANARY-\u202egnihtemos"
	job := jobText("9c3f0a11-0000-4000-8000-00000000000d", "patch.apply", testDeviceID,
		`{"message":"`+poison+`"}`,
		ts(baseNow.Add(-time.Second)), ts(baseNow.Add(10*time.Minute)), "n-poison")

	h.runner.Handle(context.Background(), s.frame(t, job))

	reason := reasonOf(t, h.sender.only(t, "job_result"))
	if strings.Contains(reason, poison) {
		t.Fatalf("refusal reason %q echoes job-supplied text into the audit log", reason)
	}
	if len(reason) > MaxRefusalReasonLen {
		t.Fatalf("refusal reason is %d bytes, cap is %d", len(reason), MaxRefusalReasonLen)
	}
}

func TestASendFailureDoesNotStopTheCapabilityFromBeingReported(t *testing.T) {
	// The buffer in Task 8 is what makes this recoverable; here the contract is
	// only that a failed send is logged and does not panic or wedge the runner.
	s := newSigner(t)
	h := newHarness(t, s)
	h.sender.err = errTestSend

	h.runner.Handle(context.Background(), s.frame(t, validJob("n-sendfail")))

	// h.sender.err makes every Send fail, so fakeSender never records a frame
	// -- only()'s wait (which polls the recorded frames) cannot be used to
	// synchronize here. Wait on the two independent, thread-safe observables
	// instead: the spy's own counter and the log buffer's own mutex.
	h.spy.awaitCount(t, 1, 2*time.Second)
	h.logs.awaitContains(t, "send", 2*time.Second)
}

var errTestSend = errors.New("send failed")

func TestTheNonceIsRecordedEvenWhenTheTypeIsUnknown(t *testing.T) {
	// This pins the ORDER, which is signature -> device -> freshness -> nonce
	// -> type. The nonce is consumed by a job that then turns out to be
	// unrunnable, and that is deliberate: SeenOrRecord is one atomic operation
	// on purpose, because splitting "have I seen this?" from "remember it"
	// opens a window between the two, and a replay defence with a window is not
	// one. The cost is a slot in a store that is bounded at NonceCap and pruned
	// by expiry; the server signs a fresh nonce on every re-dispatch
	// (JobEngine.resign sets job.nonce = uuid4), so nothing legitimate is ever
	// refused for it.
	s := newSigner(t)
	h := newHarness(t, s)
	job := jobText("9c3f0a11-0000-4000-8000-00000000000e", "patch.apply", testDeviceID, `{}`,
		ts(baseNow.Add(-time.Second)), ts(baseNow.Add(10*time.Minute)), "n-unknown")

	h.runner.Handle(context.Background(), s.frame(t, job))

	if !h.nonces.seen["n-unknown"] {
		t.Fatal("the nonce check did not run before the type check; the documented order is " +
			"signature -> device -> freshness -> nonce -> type")
	}
}

// ---------------------------------------------------------------------------
// A capability that REFUSES, as distinct from one that failed
// ---------------------------------------------------------------------------

func TestACapabilityRefusalIsReportedRefusedWithItsReason(t *testing.T) {
	// The two states are not cosmetic: `failed` is "this went wrong, try again"
	// and `refused` is "this agent will not do this". app/modules/ipam/sweep.py
	// reads the difference and marks a device that refuses `net.sweep`
	// INELIGIBLE FOR SWEEPS FOR SEVEN DAYS, selecting another device on the
	// segment (spec §5.3). So a capability opts into it explicitly, with a
	// typed error, rather than by wording an ordinary one a certain way.
	//
	// FALSIFYING OBSERVATION: state `failed` for a capability that returned a
	// *Refusal.
	s := newSigner(t)
	h := newHarness(t, s)
	h.spy.err = Refuse("this host cannot do that at all")

	h.runner.Handle(context.Background(), s.frame(t, validJob("n-refusal")))

	res := h.sender.only(t, "job_result")
	if res["state"] != "refused" {
		t.Fatalf("state %v, want refused", res["state"])
	}
	body, ok := res["result"].(map[string]any)
	if !ok {
		t.Fatalf("result = %v", res["result"])
	}
	// dispatch.py:334-337 reads result["reason"], defaulting to "agent
	// refused" -- a refusal without one tells the operator nothing.
	if body["reason"] != "this host cannot do that at all" {
		t.Fatalf("reason = %v", body["reason"])
	}
}

func TestAnOrdinaryCapabilityErrorStaysFailed(t *testing.T) {
	// The complement, and the reason the type exists: `failed` is the default
	// and refusing has to be deliberate. A capability whose network call timed
	// out must not take its device out of the sweep rotation for a week.
	s := newSigner(t)
	h := newHarness(t, s)
	h.spy.err = errors.New("the network was busy")

	h.runner.Handle(context.Background(), s.frame(t, validJob("n-plainerr")))

	res := h.sender.only(t, "job_result")
	if res["state"] != "failed" {
		t.Fatalf("state %v, want failed", res["state"])
	}
}

func TestARefusalReasonIsSanitisedBeforeItReachesTheAuditLog(t *testing.T) {
	// The reason lands in the server's hash-chained audit log via
	// JobEngine.refuse. Bidi overrides and control characters in an operator's
	// console are a way to make one record read as another, and an audit record
	// an attacker can write into is not an audit record.
	s := newSigner(t)
	h := newHarness(t, s)
	h.spy.err = Refuse("refused\u202e reversed \x07 and belled")

	h.runner.Handle(context.Background(), s.frame(t, validJob("n-refusal-sanitise")))

	res := h.sender.only(t, "job_result")
	body := res["result"].(map[string]any)
	reason, _ := body["reason"].(string)
	for _, bad := range []string{"\u202e", "\x07"} {
		if strings.Contains(reason, bad) {
			t.Fatalf("reason %q still carries %q", reason, bad)
		}
	}
	if len(reason) > MaxRefusalReasonLen {
		t.Fatalf("reason is %d bytes, above %d", len(reason), MaxRefusalReasonLen)
	}
}
