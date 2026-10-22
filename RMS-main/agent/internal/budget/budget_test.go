package budget_test

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"runtime"
	"sync/atomic"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/budget"
	"github.com/toeshee/it-suite/agent/internal/scheduler"
)

// These are the assertions CLAUDE.md's idle budget is owed. They are ordinary
// Test functions that call testing.Benchmark, so a regression FAILS -- see the
// package comment for why `go test -bench` cannot do this job.

var start = time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// wheelOnFake builds a running wheel with `entries` scheduled at one-minute
// intervals and returns a function that advances the clock by exactly one
// interval -- one wakeup, one firing per entry due.
func wheelOnFake(t testing.TB, entries int) (func(), func()) {
	t.Helper()
	f := scheduler.NewFake(start)
	w := scheduler.New(f, quiet())
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = w.Run(ctx) }()
	f.AwaitTimers(t, 1)
	for i := 0; i < entries; i++ {
		n := f.Rearms()
		if err := w.Add(string(rune('a'+i)), time.Minute, func(context.Context) {}); err != nil {
			t.Fatal(err)
		}
		f.AwaitRearms(t, n+1)
	}
	stop := func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("Wheel.Run did not return")
		}
	}
	return func() { f.Advance(time.Minute) }, stop
}

// ---------------------------------------------------------------------------
// Proxy 1: zero allocations on the idle path
// ---------------------------------------------------------------------------

func TestIdleWheelAllocatesNothingPerWakeup(t *testing.T) {
	// FALSIFIER: AllocsPerOp > 0. An idle wheel that allocates per wakeup
	// accumulates garbage forever on a machine that is doing nothing, and the
	// observation is the allocation COUNT -- not a timing, which a shared runner
	// cannot reproduce.
	var stops []func()
	t.Cleanup(func() {
		for _, s := range stops {
			s()
		}
	})
	r := budget.AssertNoAllocations(t, "idle-wheel-wakeup", func() func() {
		tick, stop := wheelOnFake(t, 1)
		stops = append(stops, stop)
		return tick
	})
	t.Logf("idle-wheel-wakeup: %d iterations, %d allocs/op, %d B/op, RSS delta %d bytes (proxy=%v)",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.RSSDelta, r.RSSIsProxy)
}

func TestAWheelWithNothingDueDoesNotWakeAtAll(t *testing.T) {
	// The complement of the above, and the stronger claim: not "waking is
	// cheap" but "there is no wakeup". A polling loop would report thousands.
	f := scheduler.NewFake(start)
	w := scheduler.New(f, quiet())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = w.Run(ctx) }()
	f.AwaitTimers(t, 1)
	n := f.Rearms()
	if err := w.Add("daily", 24*time.Hour, func(context.Context) {}); err != nil {
		t.Fatal(err)
	}
	f.AwaitRearms(t, n+1)

	before := w.Wakeups()
	for i := 0; i < 24; i++ {
		f.Advance(time.Hour - time.Minute) // never crosses the deadline
	}
	if got := w.Wakeups() - before; got != 0 {
		t.Fatalf("the wheel woke %d times across ~23 simulated hours with nothing due", got)
	}
}

// ---------------------------------------------------------------------------
// Proxy 2: a constant goroutine count
// ---------------------------------------------------------------------------

func TestTheWheelSpawnsNothingPerEntry(t *testing.T) {
	// FALSIFIER: the goroutine count grows with the entry count. A
	// time.Ticker per entry is the shape this catches, and a 32-check health
	// policy would be 32 goroutines and 32 wakeups per interval on every
	// endpoint in the fleet.
	settle()
	base := runtime.NumGoroutine()

	tick1, stop1 := wheelOnFake(t, 1)
	defer stop1()
	tick1()
	settle()
	withOne := runtime.NumGoroutine() - base

	tick32, stop32 := wheelOnFake(t, 32)
	defer stop32()
	tick32()
	settle()
	withBoth := runtime.NumGoroutine() - base

	perWheel := withBoth - withOne
	if perWheel != withOne {
		t.Fatalf("a wheel with 1 entry costs %d goroutine(s); a second wheel with 32 entries "+
			"cost %d -- an entry is spawning something", withOne, perWheel)
	}
}

func TestRepeatedIdleCyclesLeakNoGoroutines(t *testing.T) {
	// The other way "~0%% CPU" breaks: a goroutine leaked per cycle rather than
	// per entry. Measure asserts on the NET change across the whole benchmark.
	var stops []func()
	t.Cleanup(func() {
		for _, s := range stops {
			s()
		}
	})
	r := budget.Measure("idle-cycles", func() func() {
		tick, stop := wheelOnFake(t, 4)
		stops = append(stops, stop)
		return tick
	})
	// Slack accounts for the wheels Measure's own setup left running (one per
	// benchmark attempt) plus runtime bookkeeping. The signal is growth
	// PROPORTIONAL to iterations, which would be thousands, not single digits.
	budget.AssertNoGoroutineGrowth(t, r, 16)
	t.Logf("idle-cycles: %d iterations, goroutine delta %d", r.N, r.Goroutines)
}

// ---------------------------------------------------------------------------
// Proxy 3: zero network writes on a quiet tick
// ---------------------------------------------------------------------------

// countingSender stands in for the gateway connection.
type countingSender struct{ sends atomic.Int64 }

func (c *countingSender) Send(context.Context, any) error { c.sends.Add(1); return nil }

func TestAQuietTickWritesNothingToTheGateway(t *testing.T) {
	// §0.5 falsifier 4. An endpoint that reports on every tick regardless of
	// whether anything changed spends the CONTROL PLANE's budget instead of its
	// own -- invisible on the device, and ingest load nobody can attribute.
	//
	// FALSIFYING OBSERVATION: the send counter above zero after ticks that found
	// nothing to say.
	f := scheduler.NewFake(start)
	w := scheduler.New(f, quiet())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = w.Run(ctx) }()
	f.AwaitTimers(t, 1)

	sender := &countingSender{}
	var changed atomic.Bool // nothing has changed; the tick has nothing to report
	n := f.Rearms()
	if err := w.Add("tick", time.Minute, func(c context.Context) {
		if !changed.Load() {
			return // edge-triggered: report on CHANGE, not on schedule
		}
		_ = sender.Send(c, nil)
	}); err != nil {
		t.Fatal(err)
	}
	f.AwaitRearms(t, n+1)

	f.Advance(100 * time.Minute)

	if got := sender.sends.Load(); got != 0 {
		t.Fatalf("%d gateway writes across 100 quiet ticks; a tick with nothing to say must "+
			"say nothing", got)
	}
	// And the test is not vacuous: with something to report, it reports.
	changed.Store(true)
	f.Advance(time.Minute)
	if got := sender.sends.Load(); got != 1 {
		t.Fatalf("%d writes after a tick that DID have something to report, want 1", got)
	}
}

// ---------------------------------------------------------------------------
// Proxy 4: flat allocations across 100 vs 1000 ticks
// ---------------------------------------------------------------------------

func TestTickCostDoesNotAccumulate(t *testing.T) {
	// §0.5 falsifier 2. Per-tick allocation that is FREED shows up in proxy 1;
	// per-tick allocation that is RETAINED -- an unbounded history slice, a map
	// keyed by tick -- is the footprint bug that only appears after a device has
	// been up for a week. Ten times the ticks must not cost ten times the
	// retained heap.
	//
	// FALSIFYING OBSERVATION: retained bytes scaling with the tick count.
	const few, many = 100, 10_000
	small := retainedAfterTicks(t, few)
	large := retainedAfterTicks(t, many)

	// A HUNDREDFOLD spread, not tenfold, and a flat allowance rather than a
	// ratio. The spread is what makes the signal beat the noise: retaining even
	// 24 bytes per tick -- one time.Time in a history slice -- is 237 KiB of
	// difference here, while the healthy measurement is tens of bytes. A ratio
	// test on two small numbers would be the flaky kind this package refuses to
	// ship, and a tenfold spread with a loose allowance was tried first and did
	// NOT catch a deliberately planted 24-byte-per-tick leak.
	const allowance = 64 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("%d ticks retained %d bytes more than %d ticks (allowance %d); "+
			"something accumulates per tick", many, delta, few, allowance)
	}
	t.Logf("retained heap: %d ticks = %d bytes, %d ticks = %d bytes, delta = %d",
		few, small, many, large, large-small)
}

func retainedAfterTicks(t *testing.T, ticks int) int64 {
	t.Helper()
	tick, stop := wheelOnFake(t, 8)
	defer stop()
	runtime.GC()
	runtime.GC()
	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	for i := 0; i < ticks; i++ {
		tick()
	}
	runtime.GC()
	runtime.GC()
	runtime.ReadMemStats(&after)
	d := int64(after.HeapAlloc) - int64(before.HeapAlloc)
	if d < 0 {
		d = 0
	}
	return d
}

// ---------------------------------------------------------------------------
// The harness itself
// ---------------------------------------------------------------------------

func TestMeasureReportsAllocationsWhenThereAreSome(t *testing.T) {
	// A harness that reports zero for everything would make every assertion
	// above vacuous. This is the harness's own falsifier.
	r := budget.Measure("deliberately-allocating", func() func() {
		return func() {
			sink = make([]byte, 1024)
		}
	})
	if r.AllocsPerOp == 0 {
		t.Fatalf("Measure reported 0 allocs/op for a function that allocates 1 KiB per call; "+
			"every budget assertion in this package would be vacuous (N=%d)", r.N)
	}
	t.Logf("deliberately-allocating: %d allocs/op, %d B/op over %d iterations",
		r.AllocsPerOp, r.BytesPerOp, r.N)
}

func TestAssertNoAllocationsFailsOnAnAllocatingFunction(t *testing.T) {
	// And the assertion wrapper actually fails. Run against a fake TB so the
	// failure is observed rather than propagated.
	fake := &recordingTB{TB: t}
	func() {
		defer func() { _ = recover() }() // Fatalf on a real *testing.T would exit the goroutine
		budget.AssertNoAllocations(fake, "should-fail", func() func() {
			return func() { sink = make([]byte, 64) }
		})
	}()
	if !fake.failed {
		t.Fatal("AssertNoAllocations passed a function that allocates 64 bytes per call")
	}
	t.Logf("AssertNoAllocations reported: %s", fake.msg)
}

func TestFixedRunsExactlyTheIterationsAskedForAndSeesTheAllocations(t *testing.T) {
	// Fixed's own falsifier. A harness that reported zero for everything, or
	// that ran a different number of iterations than it was told, would make
	// every RSS assertion written against it vacuous -- and RSS assertions are
	// exactly the ones nobody can eyeball.
	calls := 0
	r := budget.Fixed("fixed-allocating", 5000, func() func() {
		return func() {
			calls++
			sink = make([]byte, 512)
		}
	})
	if calls != 5000 {
		t.Fatalf("Fixed ran the op %d times, want exactly 5000", calls)
	}
	if r.N != 5000 {
		t.Fatalf("Result.N = %d, want 5000", r.N)
	}
	if r.AllocsPerOp < 1 {
		t.Fatalf("Fixed reported %d allocs/op for a function that allocates 512 bytes per call",
			r.AllocsPerOp)
	}
	t.Logf("fixed-allocating: %d allocs/op, %d B/op, RSS delta %d (proxy=%v)",
		r.AllocsPerOp, r.BytesPerOp, r.RSSDelta, r.RSSIsProxy)
}

func TestRetainedSeesRetentionAndIgnoresChurn(t *testing.T) {
	// Retained's own falsifier, in both directions. Churn that is freed must
	// read as ~0; a slice that keeps growing must read as growth. A helper that
	// could not tell the two apart is the helper §0.5 falsifier 2 has been
	// waiting on.
	churn := budget.Retained(20_000, func() func() {
		return func() { sink = make([]byte, 256) }
	})
	if churn > 64<<10 {
		t.Fatalf("Retained reported %d bytes for 20,000 allocations that are all freed; it is "+
			"measuring churn, not retention", churn)
	}
	var kept [][]byte
	held := budget.Retained(20_000, func() func() {
		kept = kept[:0]
		return func() { kept = append(kept, make([]byte, 256)) }
	})
	// `kept` must be READ after the measurement or Go's liveness-based GC
	// collects it while the loop is still running -- which it did on the first
	// run of this test, reporting 152 retained bytes for 5 MB of live slices.
	// A retention test whose subject is dead measures nothing.
	if len(kept) != 20_000 {
		t.Fatalf("kept %d slices, want 20000", len(kept))
	}
	if held < 1<<20 {
		t.Fatalf("Retained reported %d bytes for 20,000 x 256 retained bytes; it cannot see "+
			"retention and every assertion built on it is vacuous", held)
	}
	t.Logf("Retained: churn = %d bytes, held = %d bytes", churn, held)
}

func TestRSSIsReadFromProcOnLinux(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("RSS comes from /proc/self/statm; CI is linux")
	}
	r := budget.Measure("rss-probe", func() func() { return func() {} })
	if r.RSSIsProxy {
		t.Fatal("RSSDelta fell back to HeapAlloc on linux; the reported number is not resident memory")
	}
}

var sink []byte

type recordingTB struct {
	testing.TB
	failed bool
	msg    string
}

func (r *recordingTB) Helper() {}

func (r *recordingTB) Fatalf(format string, args ...any) {
	r.failed = true
	r.msg = fmt.Sprintf(format, args...)
	panic("recordingTB.Fatalf")
}

func (r *recordingTB) Errorf(format string, args ...any) {
	r.failed = true
	r.msg = fmt.Sprintf(format, args...)
}

func settle() {
	for i := 0; i < 50; i++ {
		runtime.Gosched()
	}
	time.Sleep(20 * time.Millisecond)
}
