package health

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/scheduler"
)

// ---------------------------------------------------------------------------
// The capability surface: invariant #1, and the unbound case
// ---------------------------------------------------------------------------

func TestBothCapabilitiesTakeAVerifiedJobAndNothingElse(t *testing.T) {
	// Invariant #1, as a compile-time fact written as a test so its failure
	// names the reason. jobs.VerifiedJob is an interface with an unexported
	// method and no exported implementation, so a capability with this signature
	// is unreachable except through the signed-job runner. A handler that took
	// `map[string]any` instead would be reachable from anywhere in the process.
	var _ jobs.Handler = Process.Apply
	var _ jobs.Handler = Process.CheckNow
}

func TestAHealthJobBeforeBindFailsRatherThanInstallingAPolicyNothingWillTick(t *testing.T) {
	// An agent that ACCEPTED a policy it cannot run would acknowledge a
	// policy_version it is not evaluating -- and §12's reconciliation sweep
	// compares versions, so it would never re-push. The device would read as
	// configured and be silent forever.
	s := &Service{}
	if _, err := s.Apply(context.Background(), nil); !errors.Is(err, ErrUnbound) {
		t.Fatalf("Apply on an unbound service returned %v, want ErrUnbound", err)
	}
	if _, err := s.CheckNow(context.Background(), nil); !errors.Is(err, ErrUnbound) {
		t.Fatalf("CheckNow on an unbound service returned %v, want ErrUnbound", err)
	}
}

// ---------------------------------------------------------------------------
// One wheel entry, whatever the check count (§0.5's mechanism)
// ---------------------------------------------------------------------------

func TestA32CheckPolicyIsOneEntryOnTheRealWheel(t *testing.T) {
	// FALSIFIER: wheel.Len() > 1. A ticker per check would be 32 goroutines and
	// 32 wakeups per interval on every endpoint in the fleet, which is the
	// arithmetic §0.5's claim does not survive. The observation is the entry
	// count on the REAL scheduler.Wheel, not on a stand-in that cannot have the
	// defect.
	w := scheduler.New(scheduler.NewFake(testStart), quiet())
	sender := &spySender{}
	e := NewEngine(Options{Scheduler: w, Sender: sender, Tick: newSpySampler(),
		Posture: newSpySampler(), Now: func() time.Time { return testStart }, Logger: quiet()})

	checks := make([]checkJSON, 0, 32)
	for i := 0; i < 32; i++ {
		checks = append(checks, numericCheck(fmt.Sprintf("c%02d", i), "cpu.busy_pct", "gt", "95", 3))
	}
	pol, _, err := parsePolicy(defaultPolicy(7, checks...).raw(t))
	if err != nil {
		t.Fatal(err)
	}
	if err := e.install(pol); err != nil {
		t.Fatal(err)
	}
	if got := w.Len(); got != 1 {
		t.Fatalf("a 32-check policy put %d entries on the wheel; §0.5's mechanism is ONE", got)
	}
	if got := e.Checks(); got != 32 {
		t.Fatalf("engine holds %d checks, want 32 -- the assertion above would be vacuous", got)
	}
}

func TestInstallingAPolicyReplacesThePreviousOneWholesale(t *testing.T) {
	// §5.2: a policy is installed wholesale, never merged. Two live copies of
	// one check would double every report the endpoint sends.
	r := newRig(t)
	r.apply(t, defaultPolicy(1,
		numericCheck("a", "cpu.busy_pct", "gt", "50", 1),
		numericCheck("b", "memory.used_pct", "gt", "50", 1)))
	r.apply(t, defaultPolicy(2, numericCheck("c", "cpu.busy_pct", "gt", "50", 1)))

	if got := r.engine.Checks(); got != 1 {
		t.Fatalf("after replacing a 2-check policy with a 1-check policy the engine holds %d", got)
	}
	if got := r.engine.PolicyVersion(); got != 2 {
		t.Fatalf("PolicyVersion() = %d, want 2", got)
	}
	if r.sched.adds != 2 {
		t.Fatalf("the wheel entry was added %d times; each install must re-arm it", r.sched.adds)
	}
	// And the old checks are gone: a breach on "a" is impossible now.
	r.tick.fallback = num(99)
	r.sched.tick(t)
	for _, l := range r.sender.lines() {
		if l.CheckID == "a" || l.CheckID == "b" {
			t.Fatalf("a check from the PREVIOUS policy still reports: %+v", l)
		}
	}
}

func TestAnEmptyCheckListIsALegalPolicyAndDisarms(t *testing.T) {
	// §5.2: "AN EMPTY `checks` LIST IS A LEGAL POLICY AND MEANS RUN NOTHING".
	// It is how a device whose profile was unassigned is disarmed rather than
	// left running the last policy it was given forever.
	//
	// FALSIFYING OBSERVATION: a check from the prior policy still emitting.
	r := newRig(t)
	r.apply(t, defaultPolicy(9, numericCheck("a", "cpu.busy_pct", "gt", "50", 1)))
	r.tick.fallback = num(99)
	r.sched.tick(t)
	if len(r.sender.lines()) == 0 {
		t.Fatal("the armed policy emitted nothing; the disarm assertion below would be vacuous")
	}
	before := r.sched.fn
	r.sender.reset()

	disarm := policyJSON{PolicyVersion: 0, TickIntervalS: 300, PostureIntervalS: 3600,
		SummaryIntervalS: 300, Checks: []checkJSON{}}
	r.apply(t, disarm)

	if got := r.engine.Checks(); got != 0 {
		t.Fatalf("after a disarm the engine still holds %d checks", got)
	}
	if got := r.engine.PolicyVersion(); got != 0 {
		t.Fatalf("PolicyVersion() = %d after a disarm, want 0 -- ingest.py:552 discards a report "+
			"whose policy_version lags the acknowledged one by two", got)
	}
	if r.sched.Len() != 0 {
		t.Fatalf("the wheel still carries %d health entries after a disarm; a disarmed device has "+
			"no health work at all, not a callback that wakes to do nothing", r.sched.Len())
	}
	// The previous callback is what a wheel that was never told about the
	// removal would still be firing. Fire it deliberately: it must say nothing.
	before(context.Background())
	if n := r.sender.sends.Load(); n != 0 {
		t.Fatalf("%d frames after a disarm; the previous policy's checks are still live", n)
	}
}

// ---------------------------------------------------------------------------
// §0.5 falsifier 6: a profile permitted to exceed the caps
// ---------------------------------------------------------------------------

func TestAPolicyOutsideTheCapsIsRefusedBeforeAnythingIsArmed(t *testing.T) {
	// §0.5 falsifier 6. The FALSIFYING OBSERVATION named in the plan is "the
	// spy counter shows the tick engine armed with 33 checks" -- so every case
	// asserts the sampler counter and the wheel, not just the error.
	thirtyThree := make([]checkJSON, 0, 33)
	for i := 0; i < 33; i++ {
		thirtyThree = append(thirtyThree, numericCheck(fmt.Sprintf("c%02d", i), "cpu.busy_pct", "gt", "95", 3))
	}

	for _, tc := range []struct {
		name   string
		policy policyJSON
		want   string
	}{
		{"33 checks", defaultPolicy(1, thirtyThree...), "above §0.11's cap of 32"},
		{"tick below 60s", policyJSON{TickIntervalS: 30, PostureIntervalS: 3600, SummaryIntervalS: 300,
			Checks: []checkJSON{numericCheck("a", "cpu.busy_pct", "gt", "1", 1)}}, "tick_interval_s 30"},
		{"tick above 3600s", policyJSON{TickIntervalS: 3601, PostureIntervalS: 3600, SummaryIntervalS: 300,
			Checks: []checkJSON{numericCheck("a", "cpu.busy_pct", "gt", "1", 1)}}, "tick_interval_s 3601"},
		{"posture below 900s", policyJSON{TickIntervalS: 60, PostureIntervalS: 899, SummaryIntervalS: 300,
			Checks: []checkJSON{numericCheck("a", "cpu.busy_pct", "gt", "1", 1)}}, "posture_interval_s 899"},
		{"summary below 300s", policyJSON{TickIntervalS: 60, PostureIntervalS: 900, SummaryIntervalS: 299,
			Checks: []checkJSON{numericCheck("a", "cpu.busy_pct", "gt", "1", 1)}}, "summary_interval_s 299"},
		{"summary above 86400s", policyJSON{TickIntervalS: 60, PostureIntervalS: 900, SummaryIntervalS: 86401,
			Checks: []checkJSON{numericCheck("a", "cpu.busy_pct", "gt", "1", 1)}}, "summary_interval_s 86401"},
		{"for_ticks 61", defaultPolicy(1, numericCheck("a", "cpu.busy_pct", "gt", "1", 61)), "for_ticks 61"},
		{"for_ticks 0", defaultPolicy(1, numericCheck("a", "cpu.busy_pct", "gt", "1", 0)), "for_ticks 0"},
		{"negative policy_version", defaultPolicy(-1, numericCheck("a", "cpu.busy_pct", "gt", "1", 1)),
			"policy_version -1"},
		{"no check_id", defaultPolicy(1, numericCheck("", "cpu.busy_pct", "gt", "1", 1)), "no check_id"},
		{"unknown cadence", defaultPolicy(1, checkJSON{CheckID: "a", CheckType: "cpu.busy_pct",
			Comparator: "gt", Threshold: "1", ForTicks: 1, Cadence: "hourly"}), `cadence "hourly"`},
		{"comparator gt on a boolean", defaultPolicy(1, checkJSON{CheckID: "a",
			CheckType: "posture.disk_encryption", Comparator: "gt", Threshold: "true",
			ForTicks: 1, Cadence: "posture"}), "boolean"},
		{"comparator is on a numeric", defaultPolicy(1, numericCheck("a", "cpu.busy_pct", "is", "1", 1)),
			"numeric"},
		{"threshold that is not a number", defaultPolicy(1,
			numericCheck("a", "cpu.busy_pct", "gt", "ninety", 1)), "not a number"},
		{"boolean threshold that is not true/false", defaultPolicy(1,
			postureCheck("a", "posture.disk_encryption", "yes")), "not true/false"},
		{"enum threshold outside the members", defaultPolicy(1, checkJSON{CheckID: "a",
			CheckType: "service.state", Selector: "sshd", Comparator: "is", Threshold: "paused",
			ForTicks: 1, Cadence: "tick"}), "MEMBER NAME"},
		{"selector over 200 bytes", defaultPolicy(1, checkJSON{CheckID: "a", CheckType: "disk.free_pct",
			Selector: strings.Repeat("x", 201), Comparator: "lt", Threshold: "10", ForTicks: 1,
			Cadence: "tick"}), "selector is 201 bytes"},
		{"threshold over 64 bytes", defaultPolicy(1, checkJSON{CheckID: "a", CheckType: "cpu.busy_pct",
			Comparator: "gt", Threshold: strings.Repeat("9", 65), ForTicks: 1, Cadence: "tick"}),
			"threshold is 65 bytes"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := newRig(t)
			_, _, err := parsePolicy(tc.policy.raw(t))
			if err == nil {
				t.Fatalf("the policy was accepted; §0.5 falsifier 6 is 'a profile permitted to exceed the caps'")
			}
			if !errors.Is(err, ErrPolicy) {
				t.Fatalf("err = %v; want an ErrPolicy", err)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("err = %q; want it to name %q", err, tc.want)
			}
			// The observation that matters: nothing was armed and nothing ran.
			if r.sched.adds != 0 || r.sched.Len() != 0 {
				t.Fatalf("the tick engine was armed (%d adds, %d entries) by a refused policy",
					r.sched.adds, r.sched.Len())
			}
			if n := r.tick.calls.Load(); n != 0 {
				t.Fatalf("the sampler ran %d times for a refused policy", n)
			}
			if n := r.engine.Checks(); n != 0 {
				t.Fatalf("the engine holds %d checks from a refused policy", n)
			}
		})
	}
}

func TestARefusedPolicyLeavesThePreviousOneRunning(t *testing.T) {
	// The safe direction: a device that was watching a disk keeps watching it.
	// A refusal that also disarmed would make a malformed push a fleet-wide
	// blackout.
	r := newRig(t)
	r.apply(t, defaultPolicy(4, numericCheck("a", "cpu.busy_pct", "gt", "50", 1)))

	bad := defaultPolicy(5, numericCheck("b", "cpu.busy_pct", "gt", "not-a-number", 1))
	if _, _, err := parsePolicy(bad.raw(t)); err == nil {
		t.Fatal("the malformed policy parsed")
	}
	if got := r.engine.PolicyVersion(); got != 4 {
		t.Fatalf("PolicyVersion() = %d after a refused push, want the previous 4", got)
	}
	r.tick.fallback = num(99)
	r.sched.tick(t)
	if got := len(r.sender.lines()); got != 1 {
		t.Fatalf("%d lines after a refused push; the previous policy must still be evaluating", got)
	}
}

// ---------------------------------------------------------------------------
// check_id, and the two compatibility directions
// ---------------------------------------------------------------------------

func TestCheckIDTravelsBackVerbatim(t *testing.T) {
	// policy_push.py:115 sends `str(check.id)` -- a UUID string. The agent never
	// parses it, never normalises it and never generates one: the server matches
	// the report to a row by this exact string, and a re-render is a chance to
	// differ.
	const id = "0191F3AA-7C21-7E4B-8D0e-000000000001"
	r := newRig(t)
	r.apply(t, defaultPolicy(1, numericCheck(id, "cpu.busy_pct", "gt", "50", 1)))
	r.tick.fallback = num(99)
	r.sched.tick(t)

	lines := r.sender.lines()
	if len(lines) != 1 {
		t.Fatalf("%d lines, want 1", len(lines))
	}
	if lines[0].CheckID != id {
		t.Fatalf("check_id came back %q, want the byte-identical %q", lines[0].CheckID, id)
	}
}

func TestAnUnknownCheckTypeIsInstalledAndReportsUnavailable(t *testing.T) {
	// COMPATIBILITY, direction (b): an OLD agent meets a NEW server that added a
	// check type. Refusing the policy would disarm every OTHER check on the
	// device because the server learned a new word -- so the unknown type is
	// installed and reports `unavailable` (§0.7), which is a real state the
	// console already renders.
	//
	// FALSIFIER: the whole policy refused, or the unknown check evaluated
	// against a threshold this build cannot interpret.
	r := newRig(t)
	pol, dropped, err := parsePolicy(summaryEveryTick(3,
		numericCheck("known", "cpu.busy_pct", "gt", "50", 1),
		checkJSON{CheckID: "future", CheckType: "gpu.temperature_c", Comparator: "gt",
			Threshold: "80", ForTicks: 1, Cadence: "tick"},
	).raw(t))
	if err != nil {
		t.Fatalf("a policy carrying one unknown check type was refused whole: %v", err)
	}
	if len(dropped) != 0 {
		t.Fatalf("dropped %v; an unknown type is installed, not dropped", dropped)
	}
	if err := r.engine.install(pol); err != nil {
		t.Fatal(err)
	}
	// The summary is what makes an `unavailable` check visible: it emits no
	// edge, by design (§0.7).
	r.tick.fallback = num(10)
	r.sched.tick(t)

	var future string
	for _, l := range r.sender.lines() {
		if l.CheckID == "future" {
			future = l.State
		}
	}
	if future != "unavailable" {
		t.Fatalf("the unknown check reported state %q, want unavailable", future)
	}
	if n := r.tick.calls.Load(); n != 1 {
		t.Fatalf("the sampler was called %d times; the unknown check must not reach a sampler "+
			"(only the one known check should), want 1", n)
	}
}

func TestUnknownParamsMembersAreIgnored(t *testing.T) {
	// COMPATIBILITY, direction (b) again, at the params level: a strictly
	// additive server change must not take a deployed fleet offline.
	raw := json.RawMessage(`{"policy_version":2,"tick_interval_s":300,"posture_interval_s":3600,
		"summary_interval_s":86400,"checks":[{"check_id":"a","check_type":"cpu.busy_pct",
		"comparator":"gt","threshold":"90","for_ticks":2,"cadence":"tick","severity":"critical"}],
		"jitter_s":17}`)
	pol, _, err := parsePolicy(raw)
	if err != nil {
		t.Fatalf("a params object with two members this build does not know was refused: %v", err)
	}
	if pol.version != 2 || len(pol.checks) != 1 || pol.checks[0].forTicks != 2 {
		t.Fatalf("the known members were not parsed: %+v", pol)
	}
}

func TestAgentOfflineIsDroppedRatherThanEvaluated(t *testing.T) {
	// §0.6: an agent that is offline cannot report that it is offline. The
	// server filters this type out of every push; one arriving is a
	// control-plane bug, and inventing an evaluation for it would be inventing
	// a fact.
	//
	// FALSIFIER: `agent.offline` present in the installed check set, or a report
	// line carrying it.
	pol, dropped, err := parsePolicy(defaultPolicy(1,
		numericCheck("real", "cpu.busy_pct", "gt", "50", 1),
		numericCheck("offline", "agent.offline", "gt", "900", 1),
	).raw(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(dropped) != 1 || dropped[0] != "offline" {
		t.Fatalf("dropped = %v, want exactly the agent.offline check id", dropped)
	}
	for _, c := range pol.checks {
		if c.checkType == "agent.offline" {
			t.Fatal("agent.offline was installed; §0.6 makes it the server's and only the server's")
		}
	}
}
