package site

import (
	"context"
	"encoding/json"
	"net/netip"
	"runtime"
	"sort"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// THE `site.wake` IDLE BUDGET, MEASURED.
//
// docs/modules/site-server/spec.md §5.4 shipped this capability's footprint as
// a DESIGN TARGET with two named falsifiers and no benchmark, and said so:
// "`CLAUDE.md` forbids reporting a footprint number as verified until a
// benchmark runs and a falsifier is stated, and this section states neither".
// docs/kernel/wire-protocol/README.md repeats it. This file is where that
// stops being true.
//
// §5.4's two falsifiers, verbatim:
//
//	1. "a socket or timer-wheel entry resident on an agent with no wake job in
//	   flight"
//	2. "resident memory that does not return to its pre-job baseline after one"
//
// # Why none of this is a Benchmark function
//
// `go test -bench` EXITS 0 ON A REGRESSION. That was measured on this project,
// not assumed: internal/budget's package comment records a planted 64 KiB/op
// regression printing `65536 B/op` and still exiting 0. A budget stated as a
// benchmark is a log line, not a gate.
//
// So every number below is produced by `testing.Benchmark` called from an
// ORDINARY Test (through internal/budget's harness) and COMPARED against a
// threshold. A regression turns CI red. The `agent` job runs these under
// `go test ./...` and additionally prints them, by name, in its own step.
//
// # What is NOT asserted here: wall-clock CPU
//
// Same refusal, for the same reason, as internal/budget's package comment and
// health's falsifier file: a shared runner's variance exceeds the signal, a
// threshold loose enough not to flake is loose enough to pass a real
// regression, and a flaky budget test gets skipped -- which reads as coverage
// while measuring nothing. `~0% CPU at idle` stays a design target for this
// capability, and is approached through the machine-independent proxies below:
// allocation counts, goroutine counts, file descriptors, and retained heap
// across a hundredfold spread of wakes.

// ---------------------------------------------------------------------------
// Falsifier 1: anything resident with no wake job in flight
// ---------------------------------------------------------------------------

func TestFalsifier1_AnIdleAgentHoldsNothingForThisCapability(t *testing.T) {
	// §5.4 falsifier 1: "a socket or timer-wheel entry resident on an agent with
	// no wake job in flight."
	//
	// The socket and timer-wheel halves are asserted directly in site_test.go
	// (TestNothingIsOpenedBeforeAJobArrivesAndEverythingIsReleasedAfter reads
	// /proc/self/fd around a REAL wake; TestTheCapabilityAddsNothingToTheTimerWheel
	// reads scheduler.Wheel.Len). What is added here is the third resident cost
	// those two cannot see: allocation and goroutines on the ONLY code path that
	// runs while the agent is idle and this capability is compiled in.
	//
	// That path is the registry lookup the runner performs per inbound frame,
	// plus constructing the Service. Neither may allocate, because both happen
	// on an agent that is doing nothing.
	//
	// FALSIFYING OBSERVATION: AllocsPerOp > 0, or a goroutine delta.
	r := budget.AssertNoAllocations(t, "site.wake dormant", func() func() {
		s := New(Options{Logger: quiet()})
		return func() {
			if s.Runs() != 0 {
				panic("the dormancy measurement ran a wake")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("falsifier 1: %d iterations, %d allocs/op, %d B/op, goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)
}

// ---------------------------------------------------------------------------
// Falsifier 2: memory that does not return to its pre-job baseline
// ---------------------------------------------------------------------------

func TestFalsifier2_ResidentMemoryReturnsToItsPreJobBaseline(t *testing.T) {
	// §5.4 falsifier 2: "resident memory that does not return to its pre-job
	// baseline after one [wake]".
	//
	// Measured with budget.Fixed and budget.Retained, and NOT with
	// testing.Benchmark, for the reason recorded in Fixed's own doc comment:
	// testing.Benchmark picks its own iteration count, and a threshold against
	// an unstated number of iterations measures a GARBAGE RATE rather than a
	// footprint. The health engine read 5.4 MB of RSS growth that way with
	// nothing leaking.
	//
	// FALSIFYING OBSERVATION: RSSDelta above its limit in (b), printed in
	// bytes, for a STATED wake count -- budget.Fixed calls debug.FreeOSMemory
	// before reading RSS, so what it reports is what is actually still
	// resident rather than pages the scavenger has not got round to yet; or
	// retained heap above its limit in (a) and (c), where an n as small as 1
	// makes RSS itself too noisy a signal to threshold on (see (a)'s comment
	// below for the measurement that established that).
	//
	// Three measurements, because the falsifier fails in three ways.

	// (a) THE FALSIFIER AS WRITTEN: one wake, and nothing left behind.
	//
	//     PREVIOUSLY (superseded 2026-08-13): asserted on budget.Fixed's
	//     RSSDelta against a one-page (4 KiB) limit, with the comment "A single
	//     wake dials nothing here and allocates ~528 bytes, so anything
	//     approaching a page of growth is a capability that kept something.
	//     Observed: RSS delta -135,168 bytes". That framing was itself evidence
	//     of the problem: -135,168 bytes is 33 pages, not "approaching a page",
	//     and it was the PASSING case. Rerun here and observed directly (10
	//     consecutive `go test` invocations, this file, this test): RSSDelta at
	//     n=1 ranged from +200,704 to -102,400 bytes, and a further 300-sample
	//     run (budget.Fixed, n=1, 60 iterations x 5) spanned -102,400 to
	//     +200,704 -- up to 49 pages either direction. Process RSS at one
	//     iteration is not this capability's footprint; it is netpoller and GC
	//     background-worker activity, arena growth, and scheduler noise, all
	//     read off the SAME two debug.FreeOSMemory calls that (with a stated,
	//     large enough n) make (b) below reliable. This is the same
	//     garbage-rate trap Fixed's own doc comment describes for
	//     testing.Benchmark's variable iteration count, one layer over: RSS
	//     wearing this capability's clothes at n=1 rather than at an
	//     unstated n.
	//
	//     Fixed now: retained heap (the same metric (c) below already uses,
	//     via budget.Retained), which measures what the falsifier actually
	//     asks about -- did the one wake keep anything -- without process-wide
	//     RSS noise in the reading. Measured directly over the same 300
	//     samples: retained heap at n=1 stayed within 0-192 bytes throughout,
	//     under both a plain build and `-race` (shadow memory is off-heap, so
	//     it does not inflate HeapAlloc the way it inflates RSS -- see
	//     race_on_test.go). The limit keeps the original one-page number
	//     (4 KiB): now more than 20x the observed 300-sample maximum, rather
	//     than a tight bound on an unstable measurement.
	//
	//     SUPERSEDED AGAIN, same date, same underlying trap one layer deeper:
	//     a SINGLE n=1 sample of retained heap is not this capability's
	//     footprint either, under load. Reproduced directly (no external load
	//     needed) with `go test ./internal/capabilities/site/ -count=50
	//     -cpu=1,2,4 -run TestFalsifier2`, and separately with a CPU+allocation
	//     load generator running concurrently and no -cpu at all: "one wake
	//     retained 5504 bytes (limit 4096)". A heap-profile diff across
	//     exactly that window attributed the growth entirely to package
	//     runtime -- `runtime.startm` creating a brand-new OS thread (its `m`
	//     struct, profiling stack, `g0`/`gsignal` stacks) -- the one-time,
	//     never-repeated, never-freed cost of the scheduler deciding it needs
	//     another thread than it already has parked. Ordinarily that cost
	//     lands during TestFalsifier1, which runs first in this file and whose
	//     budget.Measure already calls runtime.Gosched (see its own doc
	//     comment), which is why the original 300-sample history read clean:
	//     the thread pool was already warm by the time this test ran. -cpu's
	//     GOMAXPROCS changes force fresh thread demand directly; ordinary
	//     machine contention causes the same thing by delaying exactly when a
	//     background worker (sysmon, the scavenger, a GC mark worker) first
	//     actually needs to run concurrently with the test goroutine. Either
	//     way, if that moment lands inside ONE n=1 sample's narrow
	//     before/after bracket instead of before it, that sample reads a
	//     footprint the capability does not have.
	//
	//     oneWake now calls settleRuntime (see its doc comment) before every
	//     sample, which forces exactly that thread demand to resolve outside
	//     the measured window and cut the reproduction's failure rate by
	//     roughly an order of magnitude -- but did not reach zero: the
	//     -cpu=1,2,4 reproduction still failed about once per 10-15 runs of
	//     `-count=50` with settling in place, because thread creation can be
	//     handed off to run asynchronously rather than completing before
	//     settleRuntime's wait returns, and no fixed wait bounds that on an
	//     arbitrarily contended machine. So a single sample, however well
	//     settled first, is still gambling on one instant in time. The fix
	//     this file's own CLAUDE.md-endorsed category for a noisy measurement
	//     ("measure ... the median of several samples") applies HERE instead
	//     of widening the limit: oneWakeSamples independent one-wake
	//     measurements, each with its own fresh Service and its own
	//     settleRuntime call, and the MEDIAN is what is asserted. A stray
	//     thread-creation event pulls at most one sample of oneWakeSamples
	//     off zero; the median only moves if a MAJORITY of samples retained
	//     something, which is what an actual per-wake leak would do (see the
	//     replanted-leak proof this test's history refers readers to) and a
	//     scheduler artifact would not.
	const oneWakeLimit = 4 << 10
	const oneWakeSamples = 7
	samples := make([]int64, oneWakeSamples)
	for i := range samples {
		samples[i] = budget.Retained(1, oneWake(t))
	}
	oneRetained := median(samples)
	if oneRetained > oneWakeLimit {
		t.Fatalf("§5.4 falsifier 2: one wake retained %d bytes (limit %d, samples %v); the capability "+
			"did not return to its pre-job baseline", oneRetained, oneWakeLimit, samples)
	}
	t.Logf("falsifier 2a: 1 wake, retained heap median %d bytes over samples %v (limit %d)",
		oneRetained, samples, oneWakeLimit)

	// (b) THE SAME QUESTION OVER A WORKING LIFE. One wake returning to baseline
	//     is necessary and not sufficient: a capability that kept 64 bytes per
	//     wake would pass (a) forever and still be a footprint bug on a device
	//     that has been up for a month. 10,000 wakes is a stated number and a
	//     lot of them -- a site server waking every machine on a /24 twice a day
	//     reaches it in about three weeks.
	//
	//     The limit is 2 MiB, the same one §0.5 falsifier 1 uses for the health
	//     engine, and it is deliberately well clear of the measurement rather
	//     than snug against it: 10,000 wakes churn ~5 MB of transient garbage,
	//     and even after debug.FreeOSMemory the ALLOCATOR ARENA that churn grew
	//     is what RSS reports. Observed across repeated runs on one machine:
	//     250 KiB, 434 KiB, 721 KiB, 766 KiB, with retained heap 0 every time.
	//     A threshold set at the top of that spread would flake, and a flaky
	//     budget test gets skipped -- which reads as coverage while measuring
	//     nothing. The retention check in (c) is the sharp instrument; this one
	//     is the coarse guard against a genuine leak, which would be tens of MB.
	const lifetimeLimit = 2 << 20
	const wakes = 10_000
	r := budget.Fixed("site.wake-x10000", wakes, oneWake(t))
	if r.RSSDelta > lifetimeLimit && !raceDetector {
		t.Fatalf("§5.4 falsifier 2: RSS grew %d bytes across %d wakes (limit %d, "+
			"rss_is_heap_proxy=%v)", r.RSSDelta, r.N, lifetimeLimit, r.RSSIsProxy)
	}
	t.Logf("falsifier 2b: %d wakes, RSS delta %d bytes (proxy=%v), %d allocs/op, %d B/op, "+
		"goroutine delta %d", r.N, r.RSSDelta, r.RSSIsProxy, r.AllocsPerOp, r.BytesPerOp,
		r.Goroutines)

	// The half an RSS reading cannot answer. Allocation that is FREED never
	// shows as growth; allocation that is RETAINED -- a slice of every wake, a
	// map keyed by MAC -- is invisible to a per-op count and only appears after
	// a device has been up for weeks. Compared across a HUNDREDFOLD spread,
	// because internal/budget already measured that a tenfold spread does not
	// catch a planted 24-byte-per-tick leak.
	const few, many = 100, 10_000
	small := budget.Retained(few, oneWake(t))
	large := budget.Retained(many, oneWake(t))
	// 16 KiB, the same allowance health's tick engine uses: retaining even 24
	// bytes per wake would be ~237 KiB of difference here, an order of magnitude
	// above the allowance in one direction while the clean measurement sits an
	// order of magnitude below it in the other.
	const allowance = 16 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("§5.4 falsifier 2: %d wakes retained %d bytes more than %d wakes (allowance %d); "+
			"something accumulates per wake", many, delta, few, allowance)
	}
	t.Logf("falsifier 2c: retained heap %d wakes = %d bytes, %d wakes = %d bytes, delta = %d",
		few, small, many, large, large-small)
}

// ---------------------------------------------------------------------------
// The per-wake cost itself, gated rather than logged
// ---------------------------------------------------------------------------

func TestOneWakeCostsABoundedAmountAndTheBoundIsChecked(t *testing.T) {
	// Neither falsifier covers the per-wake cost, and it is the number a
	// regression would move first: a wake that started building its packet with
	// fmt.Sprintf, or unmarshalling into a map[string]any, would allocate an
	// order of magnitude more while every assertion above stayed green (the cost
	// is transient, so it neither stays resident nor accumulates).
	//
	// Produced by testing.Benchmark through budget.Measure -- from an ordinary
	// Test, which then COMPARES. The measurement excludes the socket: the
	// subject is this capability's own arithmetic, and folding a real dial into
	// it would fold the runtime's netpoller allocation into a number that is
	// supposed to be about parsing params and building 102 bytes.
	//
	// FALSIFYING OBSERVATION: bytes-per-wake above the ceiling. Measured at 528
	// B/op and 13 allocs/op over ~460,000 iterations on this runner -- one params
	// unmarshal, one 102-byte packet, one result -- and the ceiling is 4 KiB:
	// comfortably above the measurement, and far below what unmarshalling into a
	// map[string]any or building the packet with fmt would cost.
	const ceiling = 4 << 10
	r := budget.Measure("site.wake-one-wake", oneWake(t))
	if r.N == 0 {
		t.Fatal("the benchmark ran zero iterations; the measurement is vacuous")
	}
	if r.BytesPerOp > ceiling {
		t.Fatalf("one wake allocates %d bytes/op (%d allocs) over %d iterations, above the %d "+
			"ceiling. CLAUDE.md: 'Any change that regresses idle footprint is a defect.'",
			r.BytesPerOp, r.AllocsPerOp, r.N, ceiling)
	}
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("one wake: %d iterations, %d allocs/op, %d B/op (ceiling %d), goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, ceiling, r.Goroutines)
}

func TestTheMagicPacketCostsAtMostOneFixedSizeAllocation(t *testing.T) {
	// The innermost cost, isolated. It is a fixed 102-byte buffer built from six
	// bytes: at most ONE allocation, and never a growing one. If that ever
	// becomes more, the change that did it also changed what can reach the wire.
	//
	// AT MOST one, not exactly one, and the difference was measured rather than
	// guessed: the first version of this test asserted `== 1` and read 0. The
	// packet does not escape in this loop, so escape analysis puts it on the
	// stack; in run() it is handed to a Sender and does escape. Both are
	// correct, and an assertion that pinned the number would fail on whichever
	// side of an inlining decision the compiler landed.
	//
	// FALSIFYING OBSERVATION: more than one allocation, or bytes per call above
	// a single packet's worth -- which is what an append-without-capacity or a
	// fmt.Sprintf-based rewrite would produce.
	mac := wantMAC()
	r := budget.Measure("magic-packet", func() func() {
		return func() {
			if len(magicPacket(mac)) != magicPacketLen {
				panic("wrong packet length")
			}
		}
	})
	if r.AllocsPerOp > 1 {
		t.Fatalf("magicPacket does %d allocations per call (%d B/op); it builds one fixed %d-byte "+
			"buffer from six bytes and must not do more", r.AllocsPerOp, r.BytesPerOp, magicPacketLen)
	}
	if r.BytesPerOp > 2*magicPacketLen {
		t.Fatalf("magicPacket allocates %d bytes per call for a %d-byte packet; the buffer is "+
			"built with make(..., 0, magicPacketLen) and must not grow by appending",
			r.BytesPerOp, magicPacketLen)
	}
	t.Logf("magic packet: %d iterations, %d allocs/op, %d B/op (packet is %d bytes)",
		r.N, r.AllocsPerOp, r.BytesPerOp, magicPacketLen)
}

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

// median returns the middle value of samples, sorted first. samples is
// sorted in place; callers that still need the original order should copy
// first. Used instead of a mean so ONE outlier sample -- a transient,
// non-capability event landing inside that sample's window -- cannot pull
// the reported number, where an even number of samples averages the two
// middle values rather than picking one arbitrarily.
func median(samples []int64) int64 {
	sort.Slice(samples, func(i, j int) bool { return samples[i] < samples[j] })
	n := len(samples)
	if n == 0 {
		return 0
	}
	if n%2 == 1 {
		return samples[n/2]
	}
	return (samples[n/2-1] + samples[n/2]) / 2
}

// oneWake returns a setup producing one complete wake through the capability,
// with a spy sender and a non-sleeping clock.
//
// The spy retains NOTHING. The behavioural spy in site_test.go keeps every
// datagram so it can be asserted on, and that retention is invisible until you
// measure retained heap: 10,000 wakes × 102 bytes is a megabyte of the test's
// own apparatus, reported as the capability's footprint. health's falsifier
// file was bitten by exactly this (a spy sender retaining a message log read as
// 53 KB of engine growth), so the measurement rig here counts and discards.
func oneWake(t *testing.T) func() func() {
	t.Helper()
	raw := paramsJSON(t, testMAC, testBroadcast, map[string]any{"port": 9, "repeat": 3})
	return func() func() {
		// settleRuntime FIRST, before anything this measurement will be charged
		// for. See its own comment: this pays a real but process-wide, one-time
		// Go-runtime cost that has nothing to do with this capability, so it must
		// happen before budget.Retained/budget.Fixed take their "before" reading,
		// not during the window between "before" and "after".
		settleRuntime()

		s := New(Options{
			OpenSender: func(context.Context, netip.AddrPort) (Sender, error) { return discard{}, nil },
			Clock:      noSleepClock{},
			Logger:     quiet(),
		})
		ctx := context.Background()
		// One warm-up wake outside the measurement, so a lazily initialised
		// anything is not charged to the first iteration.
		if _, err := s.run(ctx, raw); err != nil {
			panic(err)
		}
		return func() {
			if _, err := s.run(ctx, json.RawMessage(raw)); err != nil {
				panic(err)
			}
		}
	}
}

// settleRuntime forces the Go runtime's own OS-thread pool to whatever size
// GOMAXPROCS currently demands, synchronously, before returning.
//
// # Why this exists: a load-triggered failure, root-caused rather than papered
// over
//
// budget.Retained(1, ...) failed once in CI under machine contention (two
// subagents and the controller competing for the runner) after eight isolated
// reruns all passed, and was reproduced here on demand -- deterministically,
// with no external load at all -- by `go test ./internal/capabilities/site/
// -count=50 -cpu=1,2,4 -run TestFalsifier2`, and separately by the same command
// WITHOUT -cpu while a CPU+allocation load generator ran concurrently on this
// machine. Both reproductions printed the same shape of failure: "one wake
// retained 5504 bytes (limit 4096)".
//
// A heap-profile diff across exactly that window (MemProfileRate=1, one
// warm-up call, budget.Retained's own two-GC bracket around one measured call)
// attributed the growth to a small, FIXED set of call sites, every one of them
// inside package runtime and none of them reachable from this capability's
// code:
//
//	runtime.allocm            <- runtime.startm <- runtime.goschedImpl   (2048 B)
//	runtime.mProfStackInit    <- runtime.mcommoninit <- runtime.allocm   (1152 B, x2)
//	runtime.malg              <- runtime.mpreinit <- runtime.allocm     ( 448 B, x2)
//	runtime.mcommoninit       <- runtime.allocm <- runtime.startm       ( 256 B)
//
// That is `runtime.startm` creating a brand-new M (OS thread) -- its `m`
// struct, its profiling stack, its `g0`/`gsignal` system goroutines -- which
// happens exactly once per additional OS thread the scheduler ever needs for
// the life of the process, never repeated and never freed. Elsewhere in this
// same test binary that cost is invisible: TestFalsifier1 runs first and
// budget.Measure's own `runtime.Gosched()` call (its doc comment: "counted
// after a GC and a scheduler yield") routinely pays it there, which is why the
// falsifier's own history recorded a clean 0-192 byte spread over 300 samples.
// It stops being invisible exactly when the scheduler needs ANOTHER thread it
// did not already have parked -- which -cpu's GOMAXPROCS changes force
// directly, and which ordinary machine contention can also cause by delaying
// when a background worker (the scavenger goroutine observed mid-sleep in the
// same profile, GC background mark workers, sysmon) first actually runs
// concurrently with the test goroutine. Whichever triggers it, if that moment
// falls inside budget.Retained's narrow one-call bracket instead of before it,
// the runtime's bookkeeping reads as this capability's retained heap.
//
// This is falsifier 2's category (a), restated in CLAUDE.md's own terms: "the
// metric still picks up allocations that are not the capability's" -- an
// entire OS thread's worth of runtime bookkeeping landing between the two
// measurements. It is not category (c): replanting the historical 8 KiB
// leak (see this file's own history) still fails the test with this fix in
// place, so the fix has not blunted the falsifier, only stopped it from
// blaming the wrong allocator.
//
// # The fix, and why it belongs here and not in a bigger limit
//
// budget.Retained cannot be changed to fix this -- Retained's contract (one
// stated n, two GC-bracketed readings) is correct and reused by (b) and (c)
// below, which never observed this failure precisely because their n is large
// enough to make one thread's ~5.5 KB disappear into either a legitimate
// signal (a real leak) or noise below their allowance. n=1 is the one case
// with no such cushion, by design -- (a)'s whole point is "one wake, and
// NOTHING left behind." So the fix is to make sure the thing that is not this
// capability's cost has already happened before budget.Retained ever takes its
// first reading, the same way TestFalsifier1's Gosched() call already does
// for the common case, done explicitly here so it does not depend on which
// other tests happened to run first or on how the scheduler used its window.
//
// It force-runs GOMAXPROCS goroutines AT THE SAME TIME, none of which can
// multiplex onto fewer OS threads without one blocking the others, so the
// runtime cannot satisfy them without however many Ms GOMAXPROCS currently
// calls for. That is a stronger settle than an extra runtime.GC() -- a GC
// pause does not by itself force concurrent execution across every P -- and it
// is cheap: a couple of tens of milliseconds (three rounds, see below), paid
// at most three times per Falsifier2 run (n=1, n=100, n=10000 each call
// oneWake's setup fresh), never inside the timed closure
// budget.Retained/budget.Fixed actually measures.
//
// # What would make this test flaky again
//
// Removing this call, or moving it inside the closure budget.Retained times
// (which would then correctly, and wrongly, charge the wake for a thread the
// SCHEDULER decided to create). Reaching for a bigger limit instead of this
// would hide a real future leak just as effectively: the working budget
// (4 KiB) is already ~20x the 0-192 byte range this suite has actually
// measured for a genuine zero-retention wake, so a limit large enough to
// swallow one runtime thread's ~5.5 KB stops being a bound on this capability
// at all.
func settleRuntime() {
	n := runtime.GOMAXPROCS(0)
	// Three rounds, not one. A single round's wg.Wait() can return before the
	// scheduler has actually finished starting every OS thread it decided it
	// wanted -- `startm` can hand off to sysmon rather than block the calling
	// goroutine until the new M exists -- so one round closed most, not all,
	// of the gap this settling exists for (measured: it cut the failure rate
	// under the -cpu=1,2,4 reproduction from roughly 2-in-5 runs of
	// `-count=50` to roughly 1-in-15, not to zero). Repeating the barrier
	// gives any thread creation still in flight after round one a chance to
	// land before round two's GC, rather than in the measured wake.
	for round := 0; round < 3; round++ {
		start := make(chan struct{})
		var wg sync.WaitGroup
		for i := 0; i < n; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				// Busy for long enough that every one of the n goroutines is
				// actually running at once rather than the scheduler
				// multiplexing them onto fewer threads one after another.
				end := time.Now().Add(2 * time.Millisecond)
				for time.Now().Before(end) {
				}
			}()
		}
		close(start)
		wg.Wait()
		// A real wall-clock pause, not the test's noSleepClock -- this is
		// settling apparatus, not the measured wake, so it is allowed to cost
		// wall time. It gives sysmon and any hand-off-based thread creation
		// from this round room to finish before the next round's barrier (or,
		// on the last round, before budget.Retained/budget.Fixed take their
		// "before" reading) rather than during the measured call.
		time.Sleep(2 * time.Millisecond)
	}
	runtime.GC()
	runtime.GC()
}

// discard accepts every datagram and keeps none.
type discard struct{}

func (discard) Send([]byte) error { return nil }
func (discard) Close() error      { return nil }

// noSleepClock returns a fixed instant and never sleeps. A budget measurement
// that slept 200 ms per repeat would run three wakes in a second and settle on
// an iteration count too small to mean anything.
type noSleepClock struct{}

func (noSleepClock) Now() time.Time                             { return budgetEpoch }
func (noSleepClock) Sleep(context.Context, time.Duration) error { return nil }

var budgetEpoch = time.Date(2026, 8, 6, 12, 0, 0, 0, time.UTC)
