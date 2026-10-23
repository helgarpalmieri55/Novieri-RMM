// Package budget turns the agent's idle-footprint claim into an assertion that
// can fail CI.
//
// CLAUDE.md: "low-tens-of-MB resident memory and ~0% CPU at idle", and
// "Measured, not asserted. The numbers above are a claim about running code, so
// they are owed a benchmark that runs in CI and a stated falsifier."
//
// # Why this package exists rather than a plain benchmark
//
// `go test -bench` EXITS 0 ON A REGRESSION. A benchmark that got ten times
// worse prints a bigger number and the build stays green. That is not a
// mechanism; it is a log line. It is also exactly how
// docs/modules/endpoint-health-alerting/spec.md §0.5's six falsifiers sat unrun
// for an entire phase.
//
// So the numbers are produced by testing.Benchmark called from an ORDINARY Test
// function, and the Test asserts on them. A regression turns CI red.
//
// # What is NOT asserted: ~0% CPU
//
// The CPU figure in CLAUDE.md is not asserted as a wall-clock percentage, and
// this is a deliberate refusal rather than an oversight.
//
// A shared CI runner cannot reproduce a CPU-time measurement tightly enough to
// gate a build. The variance between a quiet runner and a noisy one is larger
// than the signal, so a threshold loose enough not to flake is loose enough to
// pass a genuine regression, and a threshold tight enough to catch one flakes.
// A flaky budget test gets skipped, and a skipped test is worse than an honest
// absence: it reads as coverage while measuring nothing. The CPU number
// therefore stays a DESIGN TARGET and is described that way, per CLAUDE.md's
// own instruction.
//
// # The four proxies that ARE asserted, and what each stands in for
//
//  1. ZERO ALLOCATIONS ON THE IDLE PATH (Result.AllocsPerOp).
//     A busy loop almost always allocates -- a closure per wakeup, a timer per
//     entry, a slice per pass. When it does not, proxies 2 and 4 catch it.
//     This is the cheapest, sharpest signal available and it is perfectly
//     reproducible on any runner, because an allocation count is a property of
//     the code and not of the machine. Falsifier: AllocsPerOp > 0.
//
//  2. A CONSTANT GOROUTINE COUNT (Result.Goroutines).
//     "~0% CPU at idle" is broken most often by a spawned poller -- a
//     ticker-per-entry, a watchdog, a reconnect loop that leaks one goroutine
//     per attempt. Goroutine count is exact, cheap and machine-independent.
//     Falsifier: the count grows with the number of scheduled entries, or grows
//     across repeated idle cycles.
//
//  3. ZERO NETWORK WRITES ON A QUIET TICK.
//     §0.5 falsifier 4. An endpoint that reports on every tick regardless of
//     whether anything changed costs the CONTROL PLANE its idle budget instead
//     of its own, which is worse: it is invisible on the endpoint and shows up
//     as ingest load nobody can attribute. Falsifier: a send counter above zero
//     after a tick that found nothing to say.
//
//  4. FLAT ALLOCATIONS ACROSS 100 VERSUS 1000 TICKS (Result.RSSDelta and the
//     accumulation comparison).
//     §0.5 falsifier 2. Per-tick allocation that is FREED still shows
//     AllocsPerOp > 0 and is caught by proxy 1; per-tick allocation that is
//     RETAINED -- an unbounded history slice, a map keyed by tick -- is the
//     footprint bug that only appears after a device has been up for a week.
//     Ten times the ticks must not cost ten times the resident memory.
//     Falsifier: RSS or retained heap scaling with the tick count.
//
// Together they cover the two halves of the CLAUDE.md sentence: proxies 1-3
// stand in for the CPU claim (nothing runs when nothing is due), proxies 2 and
// 4 for the memory claim (nothing accumulates).
package budget

import (
	"os"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
	"testing"
)

// Result is one measurement.
type Result struct {
	Name string
	// N is how many iterations testing.Benchmark settled on.
	N int
	// AllocsPerOp and BytesPerOp come from testing.BenchmarkResult. They are
	// properties of the code, so they are identical on a laptop and on a shared
	// runner -- which is what makes them safe to gate a build with.
	AllocsPerOp int64
	BytesPerOp  int64
	// RSSDelta is the change in resident set size across the measurement, in
	// bytes. Read from /proc/self/statm on Linux; elsewhere it falls back to
	// runtime.MemStats.HeapAlloc after two GCs, which measures RETAINED heap
	// rather than RSS and is stated as such rather than quietly substituted.
	RSSDelta int64
	// RSSIsProxy reports that RSSDelta is the HeapAlloc fallback, not real RSS.
	RSSIsProxy bool
	// Goroutines is the NET change in goroutine count across the measurement.
	Goroutines int
}

// Measure runs a benchmark and captures the numbers the idle budget is stated
// in.
//
// setup is called once per benchmark run (testing.Benchmark may run it more
// than once while it settles on an iteration count) and returns the function to
// be measured. Anything expensive -- building a wheel, opening a store --
// belongs in setup, not in the returned function.
//
// It is intended to be called from an ordinary Test. That is the whole point:
// see the package comment.
func Measure(name string, setup func() func()) Result {
	runtime.GC()
	runtime.GC()
	rssBefore, isProxy := residentBytes()
	goBefore := runtime.NumGoroutine()

	br := testing.Benchmark(func(b *testing.B) {
		op := setup()
		b.ReportAllocs()
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			op()
		}
		b.StopTimer()
	})

	runtime.GC()
	runtime.GC()
	rssAfter, _ := residentBytes()
	// Goroutines are counted after a GC and a scheduler yield so a goroutine
	// that is on its way out is not miscounted as a leak.
	runtime.Gosched()
	goAfter := runtime.NumGoroutine()

	r := Result{
		Name:       name,
		N:          br.N,
		RSSDelta:   rssAfter - rssBefore,
		RSSIsProxy: isProxy,
		Goroutines: goAfter - goBefore,
	}
	if br.N > 0 {
		r.AllocsPerOp = br.AllocsPerOp()
		r.BytesPerOp = br.AllocedBytesPerOp()
	}
	return r
}

// Fixed runs EXACTLY n iterations and reports the same numbers Measure does.
//
// # Why a fixed count exists beside testing.Benchmark
//
// testing.Benchmark chooses its own iteration count, and for an ALLOCATION
// COUNT that is a feature: allocs-per-op is a property of the code, so more
// iterations is a better estimate. For an RSS THRESHOLD it is a trap, and the
// trap was measured rather than predicted.
//
// docs/modules/endpoint-health-alerting/spec.md §0.5 falsifier 1 is "RSS delta
// above 2 MB on the default profile" -- a claim about a device that is up for
// weeks. Run through testing.Benchmark, the health tick engine's default
// profile settled on ~478,000 iterations, which at a 300-second tick is about
// 4,555 years of simulated time compressed into two wall-clock seconds. The
// periodic summaries in that window produced transient garbage faster than the
// GC returned pages, and RSS grew 5.4 MB. Nothing was leaking: the number was a
// GARBAGE RATE wearing a footprint's clothes, and a threshold on it would have
// been either meaningless or permanently red.
//
// So: allocation counts through Measure, where the iteration count helps; RSS
// and retained heap through Fixed, where the iteration count has to be a stated
// number of ticks or the threshold means nothing.
//
// AllocsPerOp and BytesPerOp here come from runtime.MemStats rather than from
// testing.BenchmarkResult, and they are INTEGER DIVISIONS by n -- the same
// division that silently rounded a real per-tick cost to zero the first time
// the health engine was measured this way. Prefer Measure when the question is
// "does this allocate at all".
func Fixed(name string, n int, setup func() func()) Result {
	if n < 1 {
		n = 1
	}
	op := setup()

	runtime.GC()
	runtime.GC()
	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	rssBefore, isProxy := residentBytes()
	goBefore := runtime.NumGoroutine()

	for i := 0; i < n; i++ {
		op()
	}

	runtime.GC()
	runtime.GC()
	runtime.ReadMemStats(&after)
	// debug.FreeOSMemory rather than a third GC, and this is the difference
	// between measuring a FOOTPRINT and measuring a RATE. Go's scavenger returns
	// freed pages to the OS lazily, over minutes. A loop that produces a
	// megabyte of transient garbage in ten milliseconds therefore shows a
	// megabyte of RSS growth that a real device -- doing the same work spread
	// over three days -- would never hold. Observed: the health engine's
	// summary path read 1.7 MB against a 2 MB threshold with nothing retained.
	// Forcing the scavenger makes the reported number what is actually still
	// resident.
	debug.FreeOSMemory()
	rssAfter, _ := residentBytes()
	runtime.Gosched()
	// The op -- and everything its closure captures, which is the SUBJECT of
	// the measurement -- must still be reachable when the GC above runs. Go's
	// liveness is precise: after the loop, `op` is dead, and without this the
	// collector reclaims the very engine whose footprint is being measured. See
	// Retained for the observation that produced this line.
	runtime.KeepAlive(op)

	return Result{
		Name:        name,
		N:           n,
		AllocsPerOp: int64(after.Mallocs-before.Mallocs) / int64(n),
		BytesPerOp:  int64(after.TotalAlloc-before.TotalAlloc) / int64(n),
		RSSDelta:    rssAfter - rssBefore,
		RSSIsProxy:  isProxy,
		Goroutines:  runtime.NumGoroutine() - goBefore,
	}
}

// Retained is the heap still held after n iterations, in bytes.
//
// It answers the question AllocsPerOp cannot: allocation that is FREED shows up
// as allocs-per-op and never as growth, while allocation that is RETAINED -- an
// unbounded history slice, a map keyed by tick -- is invisible to an allocation
// count and only appears after a device has been up for a week. Compare two
// calls a HUNDREDFOLD apart in n: a tenfold spread was tried first and did not
// catch a planted 24-byte-per-tick leak.
func Retained(n int, setup func() func()) int64 {
	op := setup()
	runtime.GC()
	runtime.GC()
	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	for i := 0; i < n; i++ {
		op()
	}
	runtime.GC()
	runtime.GC()
	runtime.ReadMemStats(&after)
	// WITHOUT THIS LINE THIS FUNCTION MEASURES NOTHING, and that was observed
	// rather than reasoned about. Go's GC is liveness-based: after the loop,
	// `op` is dead, so the closure and the object it holds -- the thing whose
	// retention is the question -- are collected before ReadMemStats runs. A
	// deliberately planted 24-byte-per-tick leak in the health tick engine read
	// as 0 bytes retained at 100 ticks AND at 10,000 ticks. A hundredfold
	// spread does not help when the subject is already garbage.
	runtime.KeepAlive(op)
	d := int64(after.HeapAlloc) - int64(before.HeapAlloc)
	if d < 0 {
		return 0
	}
	return d
}

// AssertNoAllocations is proxy 1, packaged.
//
// It fails the test -- it does not log -- because a benchmark that only logs is
// the thing this package exists to replace.
func AssertNoAllocations(t testing.TB, name string, setup func() func()) Result {
	t.Helper()
	r := Measure(name, setup)
	if r.N == 0 {
		t.Fatalf("budget %q: the benchmark ran zero iterations; the measurement is vacuous", name)
	}
	if r.AllocsPerOp != 0 {
		t.Fatalf("budget %q: %d allocations per op (%d bytes) over %d iterations; "+
			"the idle path must allocate nothing. CLAUDE.md: 'Any change that regresses "+
			"idle footprint is a defect.'", name, r.AllocsPerOp, r.BytesPerOp, r.N)
	}
	return r
}

// AssertNoGoroutineGrowth is proxy 2, packaged. `slack` allows for runtime
// bookkeeping goroutines the measurement cannot control.
func AssertNoGoroutineGrowth(t testing.TB, r Result, slack int) {
	t.Helper()
	if r.Goroutines > slack {
		t.Fatalf("budget %q: goroutine count grew by %d (slack %d); "+
			"'~0%% CPU at idle' is broken most often by a spawned poller",
			r.Name, r.Goroutines, slack)
	}
}

// residentBytes reads this process's resident set size.
//
// /proc/self/statm field 2 is resident pages. The bool reports whether the
// value is the HeapAlloc FALLBACK rather than real RSS -- a number that means
// something different must say so, not be silently substituted.
func residentBytes() (int64, bool) {
	raw, err := os.ReadFile("/proc/self/statm")
	if err == nil {
		fields := strings.Fields(string(raw))
		if len(fields) >= 2 {
			if pages, perr := strconv.ParseInt(fields[1], 10, 64); perr == nil {
				return pages * int64(os.Getpagesize()), false
			}
		}
	}
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	return int64(ms.HeapAlloc), true
}
