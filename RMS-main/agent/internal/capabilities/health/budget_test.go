package health

import (
	"context"
	"fmt"
	"runtime"
	"sync/atomic"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/budget"
	"github.com/toeshee/it-suite/agent/internal/scheduler"
)

// §0.5's SIX FALSIFIERS, as assertions that fail CI.
//
// They shipped named and unrun, for an entire phase. §0.5's second amendment
// says so in its own words: "All six falsifiers are still unrun. Not one of
// them has ever been measured, by this CI or any other." The stated cause was
// that the agent lived in a repository this CI could not see. That excuse
// expired when the agent moved in-tree; this file is where it stops being
// quoted.
//
// One test per falsifier, named for it, each wired to internal/budget's
// harness. The allocation claims are made through budget.Measure, which calls
// testing.Benchmark from an ORDINARY Test -- because `go test -bench` EXITS 0
// ON A REGRESSION: a benchmark that got ten times worse prints a bigger number
// and the build stays green. That was measured on this project
// (internal/budget's package comment) rather than assumed.
//
// WHAT IS NOT ASSERTED HERE: §0.5's "< 0.1% of one core averaged over the tick
// interval", as wall-clock. A shared runner's variance exceeds the signal; a
// threshold loose enough not to flake is loose enough to pass a real
// regression, and a flaky budget test gets disabled -- which reads as coverage
// while measuring nothing. It is asserted through internal/budget's
// machine-independent proxies instead: allocation counts, goroutine counts,
// network writes, and retained heap across a hundredfold tick spread.

// discardSender counts frames and KEEPS NONE.
//
// The spy every behavioural test uses retains each message so it can be
// asserted on, and that retention is invisible until you measure retained heap:
// the first version of falsifier 2b reported 53 KB of growth across 10,000
// ticks and blamed the engine. It was the test's own message log. A budget
// measurement whose apparatus retains is a budget measurement of the apparatus.
type discardSender struct{ sends atomic.Int64 }

func (d *discardSender) Send(context.Context, any) error { d.sends.Add(1); return nil }

// ---------------------------------------------------------------------------
// Two rigs: one driving the engine directly, one on the REAL timer wheel
// ---------------------------------------------------------------------------

// directEngine drives Engine.onTick with no wheel and no spy that retains.
//
// The wheel's own per-wakeup cost is already asserted by internal/budget
// (TestIdleWheelAllocatesNothingPerWakeup); measuring it again here would fold
// two subjects into one number. What this rig isolates is the TICK ENGINE.
func directEngine(t testing.TB, pol policyJSON) (*Engine, *discardSender, *spySampler, *spySampler) {
	t.Helper()
	sender := &discardSender{}
	tick, posture := newSpySampler(), newSpySampler()
	tick.fallback = num(3) // healthy: nothing to say
	posture.fallback = Sample{Available: true, Bool: true}
	e := NewEngine(Options{
		Scheduler: &fakeSched{}, Sender: sender, Tick: tick, Posture: posture,
		Now: func() time.Time { return testStart }, Logger: quiet(),
	})
	parsed, _, err := parsePolicy(pol.raw(t))
	if err != nil {
		t.Fatal(err)
	}
	if err := e.install(parsed); err != nil {
		t.Fatal(err)
	}
	return e, sender, tick, posture
}

// quietTick returns an op that performs exactly one QUIET tick.
//
// It holds the sub-tick counter at zero, and that is deliberate rather than
// convenient. A quiet tick is BY DEFINITION one where neither the posture
// sub-tick nor the summary is due, and testing.Benchmark picks its own
// iteration count: measured over ~470,000 iterations, the ~1,600 summary ticks
// folded into that run cost 5 bytes per op and INTEGER DIVISION rounded the
// allocation count to zero. The assertion passed while the thing it claims to
// measure was never isolated. That was observed, not predicted, and it is why
// the counter is pinned here.
func quietTick(e *Engine) func() {
	ctx := context.Background()
	return func() {
		e.ticks = 0
		e.onTick(ctx)
	}
}

// engineOnWheel installs a policy on a real scheduler.Wheel and returns a
// function that advances exactly one tick interval.
//
// The real wheel, not a stand-in: §0.5's mechanism is "one entry on the
// EXISTING timer wheel", and a measurement taken against a fake scheduler would
// not be measuring the thing the claim is about.
func engineOnWheel(t testing.TB, pol policyJSON) (*Engine, *discardSender, *spySampler, *spySampler, func(), func()) {
	t.Helper()
	f := scheduler.NewFake(testStart)
	w := scheduler.New(f, quiet())
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = w.Run(ctx) }()
	f.AwaitTimers(t, 1)

	sender := &discardSender{}
	tick, posture := newSpySampler(), newSpySampler()
	tick.fallback = num(3)
	posture.fallback = Sample{Available: true, Bool: true}

	e := NewEngine(Options{
		Scheduler: w, Sender: sender, Tick: tick, Posture: posture,
		Now: func() time.Time { return f.Now() }, Logger: quiet(),
	})
	parsed, _, err := parsePolicy(pol.raw(t))
	if err != nil {
		t.Fatal(err)
	}
	rearms := f.Rearms()
	if err := e.install(parsed); err != nil {
		t.Fatal(err)
	}
	f.AwaitRearms(t, rearms+1)

	step := time.Duration(pol.TickIntervalS) * time.Second
	stop := func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("Wheel.Run did not return")
		}
	}
	return e, sender, tick, posture, func() { f.Advance(step) }, stop
}

// defaultProfile is §0.5's: "<= 12 checks, 300 s fast tick, 3600 s posture
// sub-tick".
func defaultProfile(summaryS int) policyJSON {
	return policyJSON{
		PolicyVersion: 12, TickIntervalS: 300, PostureIntervalS: 3600,
		SummaryIntervalS: summaryS, Checks: twelveChecks(),
	}
}

// ---------------------------------------------------------------------------
// Falsifier 1: RSS delta above 2 MB on the default profile
// ---------------------------------------------------------------------------

func TestFalsifier1_RSSDeltaOnTheDefaultProfileStaysUnder2MB(t *testing.T) {
	// §0.5 falsifier 1: "RSS delta above 2 MB on the default profile."
	//
	// FALSIFYING OBSERVATION: RSSDelta above 2 MiB, printed in bytes, for a
	// stated number of ticks. RSS comes from /proc/self/statm on Linux;
	// internal/budget reports whether it fell back to a heap proxy rather than
	// quietly substituting one.
	//
	// Measured with budget.Fixed and NOT with testing.Benchmark, and the reason
	// is recorded in Fixed's own doc comment: run through testing.Benchmark this
	// engine settled on ~478,000 iterations, which at a 300-second tick is about
	// 4,555 years of simulated time in two wall-clock seconds. RSS grew 5.4 MB
	// with nothing leaking -- a garbage RATE wearing a footprint's clothes. The
	// claim §0.5 makes is about a device that is up for weeks, so the tick count
	// has to be a stated number.
	const limit = 2 << 20

	// (a) the quiet steady state, over 100,000 ticks -- roughly a year of
	//     simulated uptime at a 300 s tick.
	quiet, _, _, _ := directEngine(t, defaultProfile(86400))
	r := budget.Fixed("health-tick-quiet-100k", 100_000, func() func() { return quietTick(quiet) })
	if r.RSSDelta > limit && !raceDetector {
		t.Fatalf("§0.5 falsifier 1: RSS grew %d bytes across %d quiet ticks of a 12-check policy "+
			"(limit %d, rss_is_heap_proxy=%v)", r.RSSDelta, r.N, limit, r.RSSIsProxy)
	}
	t.Logf("falsifier 1a: %d quiet ticks, RSS delta %d bytes (proxy=%v), %d allocs/op, %d B/op",
		r.N, r.RSSDelta, r.RSSIsProxy, r.AllocsPerOp, r.BytesPerOp)

	// (b) the worst case the caps permit: a summary on EVERY tick, so every
	//     tick renders 12 values, builds a message and hands it to the sender.
	//     A thousand of those is about three and a half days at a 300 s tick.
	busy, sender, _, _, advance, stop := engineOnWheel(t, defaultProfile(300))
	defer stop()
	rb := budget.Fixed("health-tick-summary-every-tick", 1000, func() func() { return advance })
	if rb.RSSDelta > limit && !raceDetector {
		t.Fatalf("§0.5 falsifier 1: RSS grew %d bytes across %d ticks that each emitted a full "+
			"summary (limit %d)", rb.RSSDelta, rb.N, limit)
	}
	if sender.sends.Load() < 1000 {
		t.Fatalf("only %d summaries across 1000 ticks; the measurement above is not of the "+
			"reporting path at all", sender.sends.Load())
	}
	t.Logf("falsifier 1b: %d ticks each emitting a %d-check summary, RSS delta %d bytes, "+
		"%d allocs/op, %d B/op, %d frames (race detector %v -- the threshold is not applied "+
		"under -race; see race_on_test.go)",
		rb.N, busy.Checks(), rb.RSSDelta, rb.AllocsPerOp, rb.BytesPerOp, sender.sends.Load(),
		raceDetector)
}

// ---------------------------------------------------------------------------
// Falsifier 2: per-tick allocation proportional to elapsed ticks
// ---------------------------------------------------------------------------

func TestFalsifier2_TheQuietTickAllocatesNothingAndNothingAccumulates(t *testing.T) {
	// §0.5 falsifier 2: "Any per-tick heap allocation proportional to elapsed
	// ticks (i.e. anything that accumulates)."
	//
	// Two halves, because they fail differently:
	//
	//  (a) allocation that is FREED shows up as AllocsPerOp > 0 and never as
	//      growth. It is the sharpest signal available and it is a property of
	//      the code, so it is identical on a laptop and on a shared runner.
	//  (b) allocation that is RETAINED -- an unbounded history slice, a map
	//      keyed by tick -- is invisible to (a) and only appears after a device
	//      has been up for a week. Measured as retained heap across a
	//      HUNDREDFOLD spread, because internal/budget already measured that a
	//      tenfold spread does NOT catch a planted 24-byte-per-tick leak.
	e, sender, tick, _ := directEngine(t, defaultProfile(86400))
	r := budget.AssertNoAllocations(t, "health-quiet-tick", func() func() { return quietTick(e) })
	budget.AssertNoGoroutineGrowth(t, r, 0)
	if n := sender.sends.Load(); n != 0 {
		t.Fatalf("%d frames during the 'quiet tick' measurement; the number above is an average "+
			"over ticks that were not quiet", n)
	}
	if tick.calls.Load() == 0 {
		t.Fatal("the sampler was never called; the measurement is of an engine that did nothing")
	}
	// BYTES AS WELL AS ALLOCATION COUNT, and this is not belt-and-braces.
	// AllocsPerOp is MemAllocs/N, an integer division, and an APPEND AMORTISES:
	// a slice that grows by doubling allocates ~30 times across 1.2 million
	// ticks, which divides to 0. A deliberately planted 24-byte-per-tick leak
	// (one time.Time appended to a history slice -- exactly §0.5 falsifier 2's
	// shape) was measured here as "0 allocs/op, 154 B/op": the allocation count
	// alone would have passed it, and did.
	if r.BytesPerOp != 0 {
		t.Fatalf("§0.5 falsifier 2: the quiet tick allocated %d bytes/op across %d ticks "+
			"(allocs/op %d -- an amortised append rounds THAT to zero)",
			r.BytesPerOp, r.N, r.AllocsPerOp)
	}
	t.Logf("falsifier 2a: %d quiet ticks, %d allocs/op, %d B/op, goroutine delta %d, %d frames",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.Goroutines, sender.sends.Load())

	const few, many = 100, 10_000
	small := budget.Retained(few, func() func() {
		q, _, _, _ := directEngine(t, defaultProfile(300))
		return func() { q.onTick(context.Background()) }
	})
	large := budget.Retained(many, func() func() {
		q, _, _, _ := directEngine(t, defaultProfile(300))
		return func() { q.onTick(context.Background()) }
	})
	// A flat allowance over a hundredfold spread, not a ratio on two small
	// numbers. Retaining even 24 bytes per tick -- one time.Time in a history
	// slice -- is 237 KiB of difference here, while the healthy measurement is
	// a few hundred bytes. Summaries fire on EVERY tick in this policy, so the
	// reporting path is inside the measurement rather than excluded from it.
	// 16 KiB, not the 64 KiB internal/budget uses for the wheel: the clean
	// measurement here is 0 bytes and the planted 24-byte-per-tick leak read
	// 258,160, so the allowance can sit far below the signal without sitting
	// near the noise.
	const allowance = 16 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("§0.5 falsifier 2: %d ticks retained %d bytes more than %d ticks (allowance %d); "+
			"something accumulates per tick", many, delta, few, allowance)
	}
	t.Logf("falsifier 2b: retained heap %d ticks = %d bytes, %d ticks = %d bytes, delta = %d",
		few, small, many, large, large-small)
}

func TestFalsifier2_TheRealHostSamplerAccumulatesNothingEither(t *testing.T) {
	// The half of falsifier 2 that a scripted sampler cannot answer.
	//
	// The assertion above measures the tick ENGINE, and says so. The real host
	// sampler is NOT allocation-free -- os.Open allocates an *os.File per read,
	// which is a constant, freed cost rather than an accumulating one. What must
	// hold for it is the property that decides whether a device is still healthy
	// after a week: retained heap flat across a hundredfold spread.
	//
	// FALSIFYING OBSERVATION: retained bytes scaling with the tick count.
	//
	// WHAT THIS MEASURES DEPENDS ON THE PLATFORM, and that changed under this
	// test without a line of it being edited. This file carries NO build tag,
	// so this test compiles for every GOOS and measures whatever
	// NewHostSampler() returns there. On Linux it drives the real /proc-reading
	// sampler and is genuine evidence. Before the Windows sampler existed it
	// drove a do-nothing stub everywhere else -- so the sentence above ("The
	// real host sampler is NOT allocation-free") was false there and this
	// assertion passed trivially. Windows and macOS now have real samplers,
	// which makes this meaningful on both and runnable on neither: no machine
	// on this project executes either OS.
	//
	// (Corrected 2026-08-14: this said its twin was
	// TestWindowsFalsifier_TheRealSamplerAccumulatesNothing. That was wrong in
	// a way worth recording, because it hid a coverage gap behind a claim of
	// symmetry. This test IS its own Windows counterpart -- untagged, four
	// checks, through the engine. The test in sampler_windows_test.go is a
	// DIFFERENT thing: the sampler driven directly, bypassing the engine. It
	// drove two checks when this comment called it a twin, so between them
	// `process.present` -- the Toolhelp snapshot, the likeliest place for a
	// Windows leak -- was covered by neither. It now drives all five.)
	//
	// HOW FAR THE CHECKS BELOW GO: hostSamplerTick installs a policy naming
	// cpu, memory, disk.free_pct and disk.free_bytes -- FOUR. `process.present`
	// is not among them on any platform, so on Linux it is still measured by
	// nothing. That is a real remaining gap and is stated here rather than
	// left for someone to infer from the policy literal.
	const few, many = 100, 10_000
	small := budget.Retained(few, hostSamplerTick(t))
	large := budget.Retained(many, hostSamplerTick(t))
	// The clean measurement was a few hundred bytes (the sampler's reused 8 KiB
	// scratch, allocated lazily on first use) until `process.present` joined the
	// policy on 2026-08-14, and is now ~7 KiB on this runner. That rise was
	// checked rather than accepted, because "still under the allowance" is how a
	// real leak gets waved through. Retention was measured at four tick counts:
	//
	//     100 -> 16 B    1,000 -> 960 B    10,000 -> 8,112 B    50,000 -> 4,672 B
	//
	// NON-MONOTONIC, and that is the answer: 50,000 ticks retain LESS than
	// 10,000. A per-tick leak is linear, so it would have read ~40 KB at 50,000.
	// This is bounded GC jitter around a small constant, not accumulation.
	//
	// The planted leak read 258,912 through this same path, so 32 KiB still
	// separates clean from leaking by an order of magnitude in both directions.
	const allowance = 32 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("§0.5 falsifier 2 (real host sampler on %s): %d ticks retained %d bytes more "+
			"than %d (allowance %d)", runtime.GOOS, many, delta, few, allowance)
	}
	t.Logf("falsifier 2c (real host sampler on %s): retained %d ticks = %d bytes, %d ticks = "+
		"%d bytes, delta = %d", runtime.GOOS, few, small, many, large, large-small)
}

// hostSamplerTick drives the engine with the REAL platform sampler.
func hostSamplerTick(t *testing.T) func() func() {
	return func() func() {
		e := NewEngine(Options{
			Scheduler: &fakeSched{}, Sender: &discardSender{}, Tick: NewHostSampler(),
			Posture: NewPostureSampler(), Now: func() time.Time { return testStart }, Logger: quiet(),
		})
		pol, _, err := parsePolicy(defaultPolicy(1,
			numericCheck("cpu", "cpu.busy_pct", "gt", "99.9", 3),
			numericCheck("mem", "memory.used_pct", "gt", "99.9", 3),
			numericCheck("disk", "disk.free_pct", "lt", "0.1", 3),
			numericCheck("bytes", "disk.free_bytes", "lt", "1", 3),
			// process.present, added 2026-08-14. It was in NO test anywhere --
			// not this one, not the Windows falsifier -- so the check whose
			// implementations allocate most (a Toolhelp snapshot on Windows, a
			// kern.proc.all slice on macOS, a /proc walk on Linux) was the one
			// check measured by nothing. The selector is chosen NOT to match so
			// every process is walked on every tick: a match returns early, and
			// the worst case is what a budget test wants.
			processCheck("proc", "a-process-that-does-not-exist"),
		).raw(t))
		if err != nil {
			t.Fatal(err)
		}
		if err := e.install(pol); err != nil {
			t.Fatal(err)
		}
		ctx := context.Background()
		e.onTick(ctx) // warm the reused buffers before the measurement starts
		return func() { e.onTick(ctx) }
	}
}

// ---------------------------------------------------------------------------
// Falsifier 3: posture probes on the fast tick
// ---------------------------------------------------------------------------

func TestFalsifier3_PostureProbesNeverRunOnTheFastTick(t *testing.T) {
	// §0.5 falsifier 3: "Posture probes (BitLocker/FileVault/LUKS state, AV
	// health, firewall state -- the WMI / powershell / sc / systemctl
	// shell-outs) running on the fast tick instead of the posture sub-tick."
	//
	// FALSIFYING OBSERVATION: the posture prober's call counter above zero
	// across posture_interval_s / tick_interval_s - 1 fast ticks. On the default
	// profile that is 3600/300 - 1 = 11.
	_, _, tick, posture, advance, stop := engineOnWheel(t, defaultProfile(86400))
	defer stop()

	const fastTicks = 3600/300 - 1
	for i := 0; i < fastTicks; i++ {
		advance()
	}
	if n := posture.calls.Load(); n != 0 {
		t.Fatalf("§0.5 falsifier 3: the posture prober ran %d times across %d fast ticks",
			n, fastTicks)
	}
	if n := tick.calls.Load(); n == 0 {
		t.Fatal("the fast sampler never ran either; nothing ticked and this test is vacuous")
	}
	advance() // the twelfth tick IS the posture sub-tick
	if n := posture.calls.Load(); n == 0 {
		t.Fatal("the posture prober never ran on the posture sub-tick either; the assertion above " +
			"passes because posture checks never run at all")
	}
	t.Logf("falsifier 3: 0 posture calls across %d fast ticks (%d fast samples), %d on the sub-tick",
		fastTicks, tick.calls.Load(), posture.calls.Load())
}

// ---------------------------------------------------------------------------
// Falsifier 4: a network write on a quiet tick
// ---------------------------------------------------------------------------

func TestFalsifier4_AQuietTickWritesNothingToTheGateway(t *testing.T) {
	// §0.5 falsifier 4: "A network write on a tick with NO STATE TRANSITION AND
	// NO SUMMARY DUE." An endpoint that reports on every tick regardless of
	// whether anything changed spends the CONTROL PLANE's budget instead of its
	// own -- invisible on the device, and ingest load nobody can attribute.
	//
	// FALSIFYING OBSERVATION: the send counter above zero across 1000 ticks on
	// which nothing changed and no summary was due.
	//
	// The interval arithmetic is part of the test, not incidental to it: a
	// 60-second tick with the maximum 86,400-second summary interval is 1,440
	// ticks per summary, so 1,000 ticks fit inside one interval. The first draft
	// used the 300-second default and 1,000 ticks, which spans THREE summaries
	// -- it failed, correctly, reporting 3 writes.
	pol := policyJSON{PolicyVersion: 12, TickIntervalS: 60, PostureIntervalS: 3600,
		SummaryIntervalS: 86400, Checks: twelveChecks()}
	_, sender, tick, _, advance, stop := engineOnWheel(t, pol)
	defer stop()

	const quietTicks = 1000
	for i := 0; i < quietTicks; i++ {
		advance()
	}
	if n := sender.sends.Load(); n != 0 {
		t.Fatalf("§0.5 falsifier 4: %d gateway writes across %d ticks with nothing to say",
			n, quietTicks)
	}
	if n := tick.calls.Load(); n < quietTicks {
		t.Fatalf("only %d samples across %d ticks; the wheel is not driving the engine and this "+
			"test is vacuous", n, quietTicks)
	}
	// Not vacuous: with something to say, it says it. Three ticks, because the
	// default profile's checks are for_ticks=3.
	tick.fallback = num(99)
	advance()
	advance()
	advance()
	if n := sender.sends.Load(); n == 0 {
		t.Fatal("no frame after a genuine breach; the zero above means the engine cannot report " +
			"at all, not that it was quiet")
	}
	t.Logf("falsifier 4: 0 writes across %d quiet ticks (%d samples), %d after the breach",
		quietTicks, tick.calls.Load(), sender.sends.Load())
}

// ---------------------------------------------------------------------------
// Falsifier 5: an eventlog check opening its own subscription
// ---------------------------------------------------------------------------

func TestFalsifier5_NoEventlogCheckOpensItsOwnLogSubscription(t *testing.T) {
	// §0.5 falsifier 5: "An event-log check type opening its OWN OS log
	// subscription." §0.5 item 5 spells the coupling out: the `eventlog.*` types
	// RIDE the security-logs capability's existing subscription, and "two
	// subscriptions to the same log for two modules is exactly the 'always-on
	// collector' the brief forbids". With security-logs absent, every
	// `eventlog.*` check reports `unavailable` (§0.7).
	//
	// FALSIFYING OBSERVATION: the borrow counter above zero. The counter sits on
	// Options.BorrowEventLog, which is the ONLY route by which this package
	// could obtain a subscription -- so a regression that lazily opens one to
	// "make the check work" moves it.
	r := newRig(t)
	r.apply(t, summaryEveryTick(4,
		numericCheck("dsk", "eventlog.disk_error", "gt", "0", 1),
		numericCheck("svc", "eventlog.service_crash", "gt", "0", 1),
		numericCheck("krn", "eventlog.kernel_fault", "gt", "0", 1),
		numericCheck("cpu", "cpu.busy_pct", "gt", "99", 1),
	))
	const ticks = 200
	r.sched.ticks(t, ticks)

	if n := r.log.borrows.Load(); n != 0 {
		t.Fatalf("§0.5 falsifier 5: an eventlog check borrowed/opened a log subscription %d times "+
			"across %d ticks", n, ticks)
	}
	if n := r.log.reads.Load(); n != 0 {
		t.Fatalf("§0.5 falsifier 5: an eventlog check read a log subscription %d times", n)
	}

	var eventlogLines, unavailableLines int
	for _, l := range r.sender.lines() {
		if l.CheckID == "cpu" {
			continue
		}
		eventlogLines++
		if l.State == "unavailable" {
			unavailableLines++
		}
	}
	if eventlogLines == 0 {
		t.Fatal("no eventlog lines were emitted at all; this test would be vacuous")
	}
	if unavailableLines != eventlogLines {
		t.Fatalf("§0.5 falsifier 5: %d of %d eventlog lines reported something other than "+
			"unavailable", eventlogLines-unavailableLines, eventlogLines)
	}
	t.Logf("falsifier 5: 0 subscriptions across %d ticks; %d/%d eventlog lines unavailable",
		ticks, unavailableLines, eventlogLines)
}

// ---------------------------------------------------------------------------
// Falsifier 6: a profile permitted to exceed the caps
// ---------------------------------------------------------------------------

func TestFalsifier6_APolicyOverTheCapsNeverArmsTheTickEngine(t *testing.T) {
	// §0.5 falsifier 6: "A profile permitted to exceed the caps in §0.11."
	//
	// FALSIFYING OBSERVATION: the spy counter showing the tick engine armed with
	// 33 checks. The error return is NOT the observation -- an implementation
	// that returned an error and installed the policy anyway would satisfy an
	// error-only assertion.
	checks := make([]checkJSON, 0, 33)
	for i := 0; i < 33; i++ {
		checks = append(checks, numericCheck(fmt.Sprintf("c%02d", i), "cpu.busy_pct", "gt", "95", 3))
	}
	for _, tc := range []struct {
		name   string
		policy policyJSON
	}{
		{"33 checks", defaultPolicy(1, checks...)},
		{"a 30-second tick", policyJSON{TickIntervalS: 30, PostureIntervalS: 3600,
			SummaryIntervalS: 300, Checks: checks[:1]}},
		{"for_ticks 61", defaultPolicy(1, numericCheck("a", "cpu.busy_pct", "gt", "1", 61))},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := scheduler.New(scheduler.NewFake(testStart), quiet())
			sampler := newSpySampler()
			e := NewEngine(Options{Scheduler: w, Sender: &discardSender{}, Tick: sampler,
				Posture: newSpySampler(), Now: func() time.Time { return testStart }, Logger: quiet()})

			pol, _, err := parsePolicy(tc.policy.raw(t))
			if err == nil {
				// It parsed. Install it, so the observation below is about what
				// the engine DID rather than about what parse returned.
				if ierr := e.install(pol); ierr != nil {
					t.Fatal(ierr)
				}
			}
			if got := w.Len(); got != 0 {
				t.Fatalf("§0.5 falsifier 6: the tick engine is armed (%d wheel entries) with an "+
					"over-cap policy", got)
			}
			if got := e.Checks(); got != 0 {
				t.Fatalf("§0.5 falsifier 6: the tick engine holds %d checks from an over-cap policy", got)
			}
			if n := sampler.calls.Load(); n != 0 {
				t.Fatalf("§0.5 falsifier 6: the sampler ran %d times for an over-cap policy", n)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Dormancy
// ---------------------------------------------------------------------------

func TestTheTickEngineIsDormantUntilAPolicyArrives(t *testing.T) {
	// CLAUDE.md: "heavy capabilities stay dormant and only allocate/initialize
	// when a job for them actually runs". An engine with no policy holds no
	// wheel entry and touches nothing.
	//
	// FALSIFIER: a wheel entry, a goroutine, or a sampler call before the first
	// `health.policy.apply`.
	before := runtime.NumGoroutine()
	w := scheduler.New(scheduler.NewFake(testStart), quiet())
	sampler := newSpySampler()
	e := NewEngine(Options{Scheduler: w, Sender: &discardSender{}, Tick: sampler,
		Posture: newSpySampler(), Now: func() time.Time { return testStart }, Logger: quiet()})
	if w.Len() != 0 {
		t.Fatalf("building an engine put %d entries on the wheel", w.Len())
	}
	if n := sampler.calls.Load(); n != 0 {
		t.Fatalf("building an engine sampled %d times", n)
	}
	if e.PolicyVersion() != 0 {
		t.Fatalf("an unbound engine reports policy_version %d", e.PolicyVersion())
	}
	runtime.Gosched()
	if after := runtime.NumGoroutine(); after > before {
		t.Fatalf("building an engine started %d goroutine(s)", after-before)
	}
}
