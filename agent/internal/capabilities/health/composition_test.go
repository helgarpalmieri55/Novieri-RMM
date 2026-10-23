package health

// The composition test.
//
// Two independent reviews named the same blind spot: every existing test
// either drives the sampler alone (posture_budget_test.go's falsifier E) or
// drives the engine through a FAKE scheduler (fakeSched in helpers_test.go,
// budget_test.go's directEngine). Nothing wires a real scheduler.Wheel to a
// real Engine to a posture sampler built the way production builds it
// (Options.Posture / NewPostureSampler / newPostureSampler) and asks the
// question that actually matters: when a posture pass wedges, does the WHEEL
// come back?
//
// That question has a real answer to get wrong. engine.go's onTick holds e.mu
// across its whole sample loop, and scheduler.go's fire() invokes every due
// entry SYNCHRONOUSLY, in one goroutine, with no lock released in between --
// see fire()'s "Due entries are collected under the lock and invoked WITHOUT
// it" comment and posture.go's header, which states the mechanism in full and
// records that before the pass budget existed, "a machine with two wedged
// disks stalled every scheduled entry on the agent for about a minute." That
// was found by a reviewer reading code. This test is what would have found it
// by running.
//
// FALSIFYING OBSERVATION: a second wheel entry, due at the same simulated
// instant as a health tick whose only check is a wedged posture probe, is
// invoked more than posturePassBudget (shortened here to run in
// milliseconds, the same technique posture_budget_test.go's falsifier E
// uses) plus a fixed slack after the tick becomes due. Remove the pass
// budget's enforcement in posture.go (or make Engine.onTick hold its mutex
// without bound) and the wedged runner's own bound is what finally releases
// the canary -- multiple seconds later here -- and the assertion below fails
// with the measured delay. See the task report
// (.git/sdd/composition-test-report.md) for that failure captured verbatim.

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/scheduler"
)

func TestComposition_AWedgedPosturePassReleasesTheWheelWithinTheBudget(t *testing.T) {
	const (
		// Real wall-clock values. The pass budget is enforced against
		// time.Now() in posture.go, independent of the fake SCHEDULER clock
		// below -- that clock only decides when a tick becomes DUE, not how
		// long a wedged pass takes to answer. Short so the test costs
		// milliseconds; the mechanism under test (posturePassBudget shared
		// across every Sample call in one pass, e.mu held across the sample
		// loop, fire()'s synchronous invoke loop) is the production one.
		passBudget = 150 * time.Millisecond
		// What the wedged command costs if nothing bounds it -- a finite
		// wait, not forever, so a regression makes this test FAIL with a
		// measured delay rather than hang. Chosen well inside
		// scheduler.Fake's own 5s deadlock guard (clock.go's advanceGuard),
		// which would panic the test with a different, less informative
		// message if a single fire() took longer than that.
		wedgeCost = 2 * time.Second
		slack     = 1 * time.Second
	)

	// A postureRunner that blocks like a wedged shell-out, bounded by
	// wedgeCost so a regression fails this test instead of hanging it. Same
	// shape as posture_budget_test.go's falsifier E.
	//
	// posturePassEntered records the instant this runner is ENTERED -- before
	// it blocks -- so the test can assert its ordering premise below rather
	// than assume it. Buffered by 1: only the first entry matters, and a
	// non-blocking send means a hypothetical second call never stalls this
	// probe on a channel nobody is reading yet.
	posturePassEntered := make(chan time.Time, 1)
	run := func(ctx context.Context, _ string, _ ...string) postureResult {
		select {
		case posturePassEntered <- time.Now():
		default:
		}
		select {
		case <-ctx.Done():
			return postureResult{ExitCode: -1, Err: ctx.Err()}
		case <-time.After(wedgeCost):
			return postureResult{Stdout: "an unbounded command eventually answered"}
		}
	}
	wedged := func(ctx context.Context, r postureRunner, _ string) Sample {
		if r(ctx, "wedged-tool").ok() {
			return boolSample(true)
		}
		return unavailable()
	}

	// The production seam: newPostureSampler, the same function
	// NewPostureSampler() calls on every platform (posture.go). passBudget is
	// shortened through the field posture.go documents as existing for
	// exactly this ("a test can shorten it to milliseconds; nothing in the
	// agent changes it") -- the enforcement code path taken is unmodified.
	posture := newPostureSampler(run, map[string]probeFunc{
		"posture.disk_encryption": wedged,
	}).(*postureSampler)
	posture.passBudget = passBudget

	// The real timer wheel, driven by a fake clock so the test is
	// deterministic rather than sleep-tuned (scheduler/clock.go's Fake).
	f := scheduler.NewFake(testStart)
	w := scheduler.New(f, quiet())
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = w.Run(ctx) }()
	f.AwaitTimers(t, 1)
	defer func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("Wheel.Run did not return")
		}
	}()

	// The real Engine, wired through Options.Posture exactly as main.go wires
	// it -- Tick is a spy because this policy has no tick-cadence check, but
	// Options requires it and the engine falls back to `unavailable` for any
	// check it were ever asked to sample through it.
	e := NewEngine(Options{
		Scheduler: w, Sender: &discardSender{}, Tick: newSpySampler(), Posture: posture,
		Now: func() time.Time { return f.Now() }, Logger: quiet(),
	})

	// One check, posture cadence, and posture_interval_s == tick_interval_s
	// (both at the 900s floor) so posturePer == 1: EVERY tick is a posture
	// sub-tick, and the very first tick after install runs the wedged probe.
	pol := policyJSON{
		PolicyVersion: 1, TickIntervalS: 900, PostureIntervalS: 900, SummaryIntervalS: 86400,
		Checks: []checkJSON{postureCheck("wedge", "posture.disk_encryption", "true")},
	}
	parsed, _, err := parsePolicy(pol.raw(t))
	if err != nil {
		t.Fatal(err)
	}
	rearms := f.Rearms()
	if err := e.install(parsed); err != nil {
		t.Fatal(err)
	}
	f.AwaitRearms(t, rearms+1)

	// The canary: a second wheel entry, added from the same simulated instant
	// and with the same interval as the health tick, so it becomes due at
	// EXACTLY the same simulated deadline. It is pushed onto the heap AFTER
	// the health entry, and scheduler.go's fire() always processes the
	// current heap root -- which for two equal deadlines is the entry that
	// was there first, since the heap's `less` is a strict Before and never
	// swaps two equal keys on push. So the health tick (and its wedged
	// posture pass) is invoked BEFORE the canary in the same fire() call:
	// this is the shape "a second scheduled entry due at the same time"
	// actually takes on this wheel, not an assumption.
	var invokedAtNanos atomic.Int64
	canaryRan := make(chan struct{})
	if err := w.Add("canary", 900*time.Second, func(context.Context) {
		invokedAtNanos.Store(time.Now().UnixNano())
		close(canaryRan)
	}); err != nil {
		t.Fatal(err)
	}
	f.AwaitRearms(t, rearms+2)

	if got := w.Len(); got != 2 {
		t.Fatalf("wheel has %d entries, want 2 (health.tick + canary); this test is not exercising two "+
			"co-scheduled entries", got)
	}

	start := time.Now()
	// Both entries become due at the same simulated instant; Advance blocks
	// (in REAL time) until the wheel's single goroutine has handed the fired
	// deadline to the wheel and the wheel has re-armed for the next one --
	// which, per fire()'s synchronous invoke loop, is after BOTH entries have
	// run. See clock.go's Fake.Advance doc comment.
	f.Advance(900 * time.Second)

	select {
	case <-canaryRan:
	case <-time.After(passBudget + wedgeCost + slack + slack):
		t.Fatal("the canary entry never ran at all within a generous bound; the wheel goroutine is " +
			"wedged indefinitely, not merely delayed past the budget")
	}
	elapsed := time.Unix(0, invokedAtNanos.Load()).Sub(start)

	// This test's whole value depends on the wedged posture pass already
	// being in flight -- not merely scheduled, ACTUALLY inside the blocking
	// runner -- at the instant the canary becomes due, so that whatever delay
	// the canary sees is caused by e.mu / fire()'s synchronous invoke loop
	// holding it up, not by something else. That ordering is guaranteed only
	// by scheduler heap mechanics: fire() always processes the current heap
	// root, and push()'s strict Before comparator never swaps two equal
	// deadlines, so the health-tick entry (pushed first, see the canary
	// comment above) is popped and fully invoked -- including entering this
	// wedged runner -- before the canary is popped. That is internal
	// mechanics, not a documented Wheel contract. If a heap change, a
	// different push order, or a scheduler rewrite ever flips it, the canary
	// would run before the wedged pass starts, be delayed by nothing, and the
	// delay assertion below would pass having measured nothing at all. Assert
	// the premise explicitly so that flip fails loudly here instead of
	// silently passing.
	var enteredAt time.Time
	select {
	case enteredAt = <-posturePassEntered:
	case <-time.After(passBudget + wedgeCost + slack + slack):
		t.Fatal("the wedged posture runner was never entered within a generous bound; the health tick " +
			"never invoked its posture pass at all")
	}
	if canaryAt := time.Unix(0, invokedAtNanos.Load()); !enteredAt.Before(canaryAt) {
		t.Fatalf("composition: ordering assumption violated -- the wedged posture probe was entered at "+
			"%s, which is not before the canary ran at %s. This test's delay assertion below only means "+
			"something if the wedged posture pass is already running when the canary becomes due, and "+
			"that depends on scheduler.go's fire()/heap tie-breaking (root-of-heap processing plus "+
			"push()'s strict Before comparator preserving insertion order for equal deadlines) putting "+
			"the health-tick entry ahead of the canary. That tie-breaking no longer holds, so this test "+
			"measured nothing about the pass budget.", enteredAt, canaryAt)
	}

	limit := passBudget + slack
	if elapsed > limit {
		t.Fatalf("composition: a second wheel entry due at the same instant as a wedged posture tick "+
			"ran %s after that instant, want <= %s (posturePassBudget %s + %s slack). The wheel was "+
			"held past the pass budget -- engine.go's e.mu across the sample loop plus scheduler.go's "+
			"synchronous fire() means a posture pass that does not release in bounded time stalls every "+
			"other scheduled entry on the agent, exactly as posture.go's header describes.",
			elapsed, limit, passBudget, slack)
	}
	t.Logf("composition: canary co-scheduled with a wedged posture tick ran %s after the shared due "+
		"instant (limit %s = posturePassBudget %s + %s slack)", elapsed, limit, passBudget, slack)
}
