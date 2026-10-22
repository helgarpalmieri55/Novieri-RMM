package health

import (
	"context"
	"testing"
	"time"
)

// §4.2's tick behaviour, driven by a fake sampler and a recording scheduler.
// Every assertion here is on the EMITTED LINES or on a call counter, never on
// an absence of error.

func kinds(r *rig) []string {
	var out []string
	for _, l := range r.sender.lines() {
		out = append(out, l.Kind+":"+l.State)
	}
	return out
}

func TestABreachIsEmittedOnTheForTicksTickAndNotBefore(t *testing.T) {
	// §4.2: the counter reaches for_ticks and the check was not breaching ->
	// emit `breach`. Before that, nothing.
	r := newRig(t)
	r.apply(t, defaultPolicy(1, numericCheck("cpu", "cpu.busy_pct", "gt", "90", 3)))
	r.tick.fallback = num(95)

	r.sched.ticks(t, 2)
	if n := r.sender.sends.Load(); n != 0 {
		t.Fatalf("%d frames after 2 of 3 required ticks; a breach is duration-gated", n)
	}
	r.sched.tick(t)
	lines := r.sender.lines()
	if len(lines) != 1 {
		t.Fatalf("%d lines on the for_ticks tick, want exactly 1: %v", len(lines), kinds(r))
	}
	if lines[0].Kind != "breach" || lines[0].State != "breaching" {
		t.Fatalf("got %+v; want kind=breach state=breaching", lines[0])
	}
	if lines[0].Value != "95.0" {
		t.Fatalf("value = %q; §5.1's example renders a percentage with one decimal", lines[0].Value)
	}

	// And it does not repeat: an edge is an edge.
	r.sender.reset()
	r.sched.ticks(t, 5)
	if n := r.sender.sends.Load(); n != 0 {
		t.Fatalf("%d frames across 5 further breaching ticks; `breach` is a transition, not a heartbeat", n)
	}
}

func TestRecoveryIsASingleSampleAndIsNotDurationGated(t *testing.T) {
	// §4.2: "check was breaching and the comparison no longer matches (single
	// sample -- recovery is NOT duration-gated) -> emit recovery". The asymmetry
	// is deliberate: a slow-to-open, fast-to-close alert errs toward silence.
	r := newRig(t)
	r.apply(t, defaultPolicy(1, numericCheck("cpu", "cpu.busy_pct", "gt", "90", 3)))
	r.tick.fallback = num(95)
	r.sched.ticks(t, 3)
	r.sender.reset()

	r.tick.fallback = num(12.34)
	r.sched.tick(t)
	lines := r.sender.lines()
	if len(lines) != 1 {
		t.Fatalf("%d lines on the first non-matching sample, want 1: %v", len(lines), kinds(r))
	}
	if lines[0].Kind != "recovery" || lines[0].State != "ok" {
		t.Fatalf("got %+v; want kind=recovery state=ok", lines[0])
	}
	if lines[0].Value != "12.3" {
		t.Fatalf("value = %q, want %q", lines[0].Value, "12.3")
	}
}

func TestTheConsecutiveCounterResetsOnANonMatchingSample(t *testing.T) {
	// Two matching ticks, one miss, two more matching ticks: no breach. A
	// counter that did not reset would open an alert for a threshold that was
	// never crossed for the required duration.
	r := newRig(t)
	r.apply(t, defaultPolicy(1, numericCheck("cpu", "cpu.busy_pct", "gt", "90", 3)))
	r.tick.fallback = num(95)
	r.sched.ticks(t, 2)
	r.tick.fallback = num(1)
	r.sched.tick(t)
	r.tick.fallback = num(95)
	r.sched.ticks(t, 2)
	if n := r.sender.sends.Load(); n != 0 {
		t.Fatalf("%d frames; the consecutive counter did not reset across the miss: %v", n, kinds(r))
	}
	r.sched.tick(t) // the third consecutive match
	if got := kinds(r); len(got) != 1 || got[0] != "breach:breaching" {
		t.Fatalf("kinds = %v, want one breach on the third consecutive match", got)
	}
}

func TestAQuietTickWritesNothingToTheGateway(t *testing.T) {
	// §0.5 falsifier 4, as behaviour rather than as a budget number. An endpoint
	// that reports on every tick regardless of whether anything changed spends
	// the CONTROL PLANE's budget instead of its own.
	r := newRig(t)
	r.apply(t, defaultPolicy(1, twelveChecks()...))
	r.tick.fallback = num(3)
	r.posture.fallback = Sample{Available: true, Bool: true}

	r.sched.ticks(t, 100)
	if n := r.sender.sends.Load(); n != 0 {
		t.Fatalf("%d gateway writes across 100 ticks with nothing to say", n)
	}
	// Not vacuous: with something to say, it says it.
	r.tick.fallback = num(99)
	r.sched.ticks(t, 3)
	if n := r.sender.sends.Load(); n == 0 {
		t.Fatal("no frame after a genuine breach; the assertion above is vacuous")
	}
}

func TestASummaryCarriesEveryCheckAndTheInstalledPolicyVersion(t *testing.T) {
	// §4.2: "every summary_interval_s the agent emits one summary: every
	// applicable check's current state, current value and the installed
	// policy_version". It is the reconciliation channel (§0.4's version compare).
	r := newRig(t)
	r.apply(t, summaryEveryTick(11,
		numericCheck("cpu", "cpu.busy_pct", "gt", "90", 3),
		numericCheck("mem", "memory.used_pct", "gt", "90", 3),
		postureCheck("enc", "posture.disk_encryption", "true"),
	))
	r.tick.fallback = num(41)
	r.posture.fallback = Sample{Available: true, Bool: true}

	r.sched.tick(t)
	if n := r.sender.sends.Load(); n != 1 {
		t.Fatalf("%d messages for one summary, want 1", n)
	}
	msg := r.sender.messages[0]
	if msg.PolicyVersion != 11 {
		t.Fatalf("policy_version = %d, want the installed 11", msg.PolicyVersion)
	}
	if len(msg.Reports) != 3 {
		t.Fatalf("%d summary lines for a 3-check policy: %v", len(msg.Reports), kinds(r))
	}
	seen := map[string]string{}
	for _, l := range msg.Reports {
		if l.Kind != "summary" {
			t.Fatalf("line %+v is not a summary", l)
		}
		seen[l.CheckID] = l.Value
	}
	if seen["cpu"] != "41.0" || seen["mem"] != "41.0" {
		t.Fatalf("summary values = %v; a percentage renders with one decimal", seen)
	}
	// The posture check has never been sampled -- its sub-tick has not come
	// round -- so it reports the third state rather than a number nobody
	// observed.
	for _, l := range msg.Reports {
		if l.CheckID == "enc" && l.State != "unavailable" {
			t.Fatalf("a posture check that has never been sampled reports %q; §0.7: absent is not "+
				"the same as healthy", l.State)
		}
	}
}

// ---------------------------------------------------------------------------
// §0.5 falsifier 3, as behaviour: posture probes never run on the fast tick
// ---------------------------------------------------------------------------

func TestPostureChecksDoNotRunOnTheFastTick(t *testing.T) {
	// §0.5 falsifier 3. The expensive probes (BitLocker/FileVault/LUKS, AV
	// health, firewall state -- the WMI / powershell / sc / systemctl
	// shell-outs) run on the posture SUB-TICK.
	//
	// FALSIFYING OBSERVATION: the posture sampler's call counter above zero
	// across posture_interval_s/tick_interval_s - 1 fast ticks.
	r := newRig(t)
	// 300 s tick, 3600 s posture -> 12 fast ticks per posture sub-tick.
	r.apply(t, defaultPolicy(1,
		numericCheck("cpu", "cpu.busy_pct", "gt", "90", 3),
		postureCheck("enc", "posture.disk_encryption", "true"),
	))
	r.tick.fallback = num(1)
	r.posture.fallback = Sample{Available: true, Bool: true}

	r.sched.ticks(t, 11)
	if n := r.posture.calls.Load(); n != 0 {
		t.Fatalf("the posture prober ran %d times across 11 fast ticks; §0.5 falsifier 3", n)
	}
	if n := r.tick.calls.Load(); n != 11 {
		t.Fatalf("the fast sampler ran %d times across 11 ticks, want 11 -- otherwise the "+
			"assertion above is vacuous because nothing ticked", n)
	}
	r.sched.tick(t) // the twelfth: the posture sub-tick
	if n := r.posture.calls.Load(); n != 1 {
		t.Fatalf("the posture prober ran %d times on the posture sub-tick, want 1", n)
	}
}

// ---------------------------------------------------------------------------
// §0.7: unavailable is a real third state
// ---------------------------------------------------------------------------

func TestUnavailableNeverBreachesAndDoesNotAdvanceTheCounter(t *testing.T) {
	r := newRig(t)
	r.apply(t, summaryEveryTick(1, numericCheck("cpu", "cpu.busy_pct", "gt", "90", 2)))
	r.tick.fallback = unavailable()

	r.sched.ticks(t, 20)
	for _, l := range r.sender.lines() {
		if l.Kind != "summary" {
			t.Fatalf("an unanswerable check emitted an edge: %+v", l)
		}
		if l.State != "unavailable" {
			t.Fatalf("state = %q, want unavailable", l.State)
		}
		if l.Value != "" {
			t.Fatalf("value = %q; there is no value to report for a check that could not be answered", l.Value)
		}
	}
	// One answerable matching sample must NOT immediately breach: for_ticks is
	// 2 and the unanswerable ticks advanced nothing.
	r.sender.reset()
	r.tick.fallback = num(99)
	r.sched.tick(t)
	for _, l := range r.sender.lines() {
		if l.Kind == "breach" {
			t.Fatalf("a single matching sample breached a for_ticks=2 check; the unanswerable "+
				"ticks advanced the counter: %+v", l)
		}
	}
}

func TestUnavailableNeitherOpensNorClearsAnAlert(t *testing.T) {
	// §0.7: "`unavailable` never opens an alert and NEVER CLEARS ONE." An alert
	// cleared because the check stopped being answerable is a silent loss of a
	// real finding.
	//
	// FALSIFIER: a `recovery` line emitted when a breaching check becomes
	// unanswerable.
	r := newRig(t)
	r.apply(t, defaultPolicy(1, numericCheck("cpu", "cpu.busy_pct", "gt", "90", 1)))
	r.tick.fallback = num(99)
	r.sched.tick(t) // breach
	r.sender.reset()

	r.tick.fallback = unavailable()
	r.sched.ticks(t, 5)
	if n := r.sender.sends.Load(); n != 0 {
		t.Fatalf("%d frames while a breaching check was unanswerable: %v", n, kinds(r))
	}
	// It comes back healthy: the recovery that was owed is emitted now.
	r.tick.fallback = num(1)
	r.sched.tick(t)
	if got := kinds(r); len(got) != 1 || got[0] != "recovery:ok" {
		t.Fatalf("kinds = %v; the recovery owed from before the outage was never sent", got)
	}
}

func TestUnavailableThenBreachingAgainIsNotASecondEdge(t *testing.T) {
	// The other half of the memory: coming back to `breaching` after an outage
	// is not a new edge, because the alert the first one opened is still open.
	r := newRig(t)
	r.apply(t, defaultPolicy(1, numericCheck("cpu", "cpu.busy_pct", "gt", "90", 1)))
	r.tick.fallback = num(99)
	r.sched.tick(t)
	r.tick.fallback = unavailable()
	r.sched.tick(t)
	r.sender.reset()

	r.tick.fallback = num(99)
	r.sched.ticks(t, 3)
	if n := r.sender.sends.Load(); n != 0 {
		t.Fatalf("%d frames re-opening an alert that never closed: %v", n, kinds(r))
	}
}

// ---------------------------------------------------------------------------
// §0.5 falsifier 5 / §4.1†: eventlog checks borrow, they never subscribe
// ---------------------------------------------------------------------------

func TestEventlogChecksReportUnavailableAndOpenNoSubscription(t *testing.T) {
	// §0.5 item 5 and §4.1†. The `eventlog.*` types are counters read from the
	// security-logs capability's EXISTING OS subscription. That capability is
	// not built, so they report `unavailable` -- and they must not open a second
	// subscription to make themselves work.
	//
	// FALSIFYING OBSERVATION: the borrow counter above zero. See
	// budget_test.go's falsifier-5 test for the same observation stated as a
	// budget assertion.
	r := newRig(t)
	r.apply(t, summaryEveryTick(1,
		numericCheck("dsk", "eventlog.disk_error", "gt", "0", 1),
		numericCheck("svc", "eventlog.service_crash", "gt", "0", 1),
		numericCheck("krn", "eventlog.kernel_fault", "gt", "0", 1),
	))
	r.sched.ticks(t, 10)

	if n := r.log.borrows.Load(); n != 0 {
		t.Fatalf("the eventlog checks borrowed a log subscription %d times", n)
	}
	if n := r.log.reads.Load(); n != 0 {
		t.Fatalf("the eventlog checks read a log subscription %d times", n)
	}
	if n := r.tick.calls.Load(); n != 0 {
		t.Fatalf("an eventlog check reached the host sampler %d times; those types never do", n)
	}
	lines := r.sender.lines()
	if len(lines) == 0 {
		t.Fatal("no summary lines at all; this test would be vacuous")
	}
	for _, l := range lines {
		if l.State != "unavailable" {
			t.Fatalf("%+v: every eventlog.* check reports unavailable in v1 (§4.1†)", l)
		}
	}
}

// ---------------------------------------------------------------------------
// Comparators and value rendering
// ---------------------------------------------------------------------------

func TestComparatorSemantics(t *testing.T) {
	// The numeric comparator IS the breach predicate (§9.1's alert payload:
	// observed_value 3.1, threshold 10, comparator lt -- an OPEN alert).
	//
	// `is` is the other way round, and §4.1's amendment is the authority:
	// "threshold_bool = true means 'healthy is running'". So a state check
	// breaches when the sample is NOT the threshold. The two conventions are
	// stated in two places in the spec and never side by side; this is where
	// they meet.
	for _, tc := range []struct {
		name       string
		check      checkJSON
		sample     Sample
		wantBreach bool
	}{
		{"gt above", numericCheck("a", "cpu.busy_pct", "gt", "90", 1), num(91), true},
		{"gt equal", numericCheck("a", "cpu.busy_pct", "gt", "90", 1), num(90), false},
		{"gte equal", numericCheck("a", "cpu.busy_pct", "gte", "90", 1), num(90), true},
		{"lt below", numericCheck("a", "disk.free_pct", "lt", "10", 1), num(3.1), true},
		{"lt equal", numericCheck("a", "disk.free_pct", "lt", "10", 1), num(10), false},
		{"lte equal", numericCheck("a", "disk.free_pct", "lte", "10", 1), num(10), true},
		{"eq", numericCheck("a", "cpu.busy_pct", "eq", "0", 1), num(0), true},
		{"neq", numericCheck("a", "cpu.busy_pct", "neq", "0", 1), num(1), true},
		{"boolean is true, sample true", postureCheck("a", "posture.firewall_enabled", "true"),
			Sample{Available: true, Bool: true}, false},
		{"boolean is true, sample false", postureCheck("a", "posture.firewall_enabled", "true"),
			Sample{Available: true, Bool: false}, true},
		{"boolean is false, sample false", postureCheck("a", "posture.firewall_enabled", "false"),
			Sample{Available: true, Bool: false}, false},
		{"enum is running, sample running", checkJSON{CheckID: "a", CheckType: "service.state",
			Selector: "sshd", Comparator: "is", Threshold: "running", ForTicks: 1, Cadence: "tick"},
			Sample{Available: true, Enum: "running"}, false},
		{"enum is running, sample stopped", checkJSON{CheckID: "a", CheckType: "service.state",
			Selector: "sshd", Comparator: "is", Threshold: "running", ForTicks: 1, Cadence: "tick"},
			Sample{Available: true, Enum: "stopped"}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			pol, _, err := parsePolicy(defaultPolicy(1, tc.check).raw(t))
			if err != nil {
				t.Fatal(err)
			}
			if got := tc.sample.matches(&pol.checks[0]); got != tc.wantBreach {
				t.Fatalf("matches = %v, want %v", got, tc.wantBreach)
			}
		})
	}
}

func TestValueRendering(t *testing.T) {
	// §4.1's amendment and §5.1's example. `value` is a STRING max_length 64;
	// an over-length line is dropped by the server WITH THE WHOLE MESSAGE, so
	// it is truncated here.
	for _, tc := range []struct {
		name   string
		check  checkJSON
		sample Sample
		want   string
	}{
		{"percentage", numericCheck("a", "disk.free_pct", "lt", "10", 1), num(3.14159), "3.1"},
		// 41.06, not 41.05: float64 holds 41.05 as 41.04999... and
		// FormatFloat(_, 'f', 1, 64) correctly renders it "41.0". The first
		// draft of this case asserted "41.1" for 41.05 and was wrong about the
		// arithmetic, not about the code.
		{"percentage rounds", numericCheck("a", "cpu.busy_pct", "gt", "1", 1), num(41.06), "41.1"},
		{"byte count is an integer", numericCheck("a", "disk.free_bytes", "lt", "1", 1),
			num(12884901888), "12884901888"},
		{"event count is an integer", numericCheck("a", "eventlog.disk_error", "gt", "0", 1),
			num(7), "7"},
		{"boolean true", postureCheck("a", "posture.disk_encryption", "true"),
			Sample{Available: true, Bool: true}, "true"},
		{"boolean false", postureCheck("a", "posture.disk_encryption", "true"),
			Sample{Available: true, Bool: false}, "false"},
		{"enum member name", checkJSON{CheckID: "a", CheckType: "service.state", Selector: "sshd",
			Comparator: "is", Threshold: "running", ForTicks: 1, Cadence: "tick"},
			Sample{Available: true, Enum: "running"}, "running"},
		{"unavailable renders nothing", numericCheck("a", "cpu.busy_pct", "gt", "1", 1),
			unavailable(), ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			pol, _, err := parsePolicy(defaultPolicy(1, tc.check).raw(t))
			if err != nil {
				t.Fatal(err)
			}
			if got := renderValue(tc.sample, &pol.checks[0]); got != tc.want {
				t.Fatalf("renderValue = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestAnOverLongValueIsTruncatedRatherThanSentWhole(t *testing.T) {
	// HealthReportLine.value is max_length=64. A longer one makes
	// parse_agent_message drop the WHOLE message -- every other line with it --
	// and the only trace is a logger.warning.
	spec := checkSpec{kind: kindEnum, how: renderEnum}
	long := ""
	for len(long) < 200 {
		long += "service-name-"
	}
	got := renderValue(Sample{Available: true, Enum: long}, &spec)
	if len(got) > maxValueLen {
		t.Fatalf("rendered %d bytes; the server's bound is %d and it costs the whole message",
			len(got), maxValueLen)
	}
	// Truncation is on a rune boundary: invalid UTF-8 would fail the gateway's
	// JSON decode, which costs the whole message too.
	multi := ""
	for len(multi) < 200 {
		multi += "日本語"
	}
	got = renderValue(Sample{Available: true, Enum: multi}, &spec)
	if len(got) > maxValueLen {
		t.Fatalf("rendered %d bytes", len(got))
	}
	for _, r := range got {
		if r == '�' {
			t.Fatalf("truncation produced U+FFFD: %q", got)
		}
	}
}

// ---------------------------------------------------------------------------
// health.check.now (§5.3)
// ---------------------------------------------------------------------------

func TestCheckNowSamplesEveryCheckIncludingPostureAndEmitsAFullSummary(t *testing.T) {
	// §5.3: dispatching it tells the agent to sample every check immediately and
	// emit a full summary. Used after a remediation completes, so an operator
	// does not wait a tick interval to learn whether the fix worked.
	//
	// Running the posture probes here is NOT §0.5 falsifier 3: that falsifier is
	// about the scheduled fast tick, and a dispatched job is not one.
	r := newRig(t)
	r.apply(t, defaultPolicy(5,
		numericCheck("cpu", "cpu.busy_pct", "gt", "90", 3),
		postureCheck("enc", "posture.disk_encryption", "true"),
	))
	r.tick.fallback = num(12)
	r.posture.fallback = Sample{Available: true, Bool: true}

	if err := r.engine.CheckNow(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := r.posture.calls.Load(); n != 1 {
		t.Fatalf("check.now called the posture prober %d times, want 1", n)
	}
	if n := r.sender.sends.Load(); n != 1 {
		t.Fatalf("%d messages, want 1", n)
	}
	msg := r.sender.messages[0]
	if msg.PolicyVersion != 5 {
		t.Fatalf("policy_version = %d, want 5", msg.PolicyVersion)
	}
	if len(msg.Reports) != 2 {
		t.Fatalf("%d lines for a 2-check policy: %v", len(msg.Reports), msg.Reports)
	}
}

func TestCheckNowOnADisarmedAgentSendsNothing(t *testing.T) {
	// `reports` is min_length=1: an empty array is rejected WHOLE by
	// parse_agent_message and the only trace is a logger.warning, so from the
	// endpoint it would look exactly like a successful report.
	r := newRig(t)
	if err := r.engine.CheckNow(context.Background()); err != nil {
		t.Fatal(err)
	}
	r.apply(t, policyJSON{PolicyVersion: 0, TickIntervalS: 300, PostureIntervalS: 3600,
		SummaryIntervalS: 300, Checks: []checkJSON{}})
	if err := r.engine.CheckNow(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := r.sender.sends.Load(); n != 0 {
		t.Fatalf("%d frames from a disarmed agent; a message with reports:[] is dropped whole", n)
	}
}

func TestTheTickCallbackOnAnEngineWithNoPolicyIsInert(t *testing.T) {
	// A callback left on the wheel by a wheel implementation that does not
	// support removal must still do nothing.
	e := NewEngine(Options{Scheduler: &fakeSched{}, Sender: &spySender{}, Tick: newSpySampler(),
		Posture: newSpySampler(), Now: func() time.Time { return testStart }, Logger: quiet()})
	e.onTick(context.Background())
	if e.PolicyVersion() != 0 || e.Checks() != 0 {
		t.Fatal("an engine with no policy reported one")
	}
}
