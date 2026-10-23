package logs

import (
	"context"
	"os"
	"runtime"
	"sort"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// median returns the middle value of samples, sorted in place. The same
// helper, for the same reason, as `capabilities/site`'s: a mean lets ONE
// outlier sample pull the reported number, and an RSS reading is exactly the
// kind of measurement a single unrelated event can perturb.
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

// THE `logs.ship` IDLE BUDGET, MEASURED.
//
// docs/modules/security-logs/spec.md never stated one: the agent collectors
// were out of scope for the control-plane phase (§1), so there was no footprint
// to state. This file states it and measures it at the same time, and §10.6 of
// that spec carries the table.
//
// The falsifiers:
//
//  1. Anything resident for this capability on an agent with no ship job in
//     flight: an allocation, a goroutine, an open file descriptor, or a
//     standing OS log subscription.
//  2. Memory that scales with the SIZE OF THE LOG rather than with the batch.
//     This is the one that matters most here and the one a per-op allocation
//     count cannot see: a Windows Security log is routinely hundreds of
//     megabytes, and the failure mode is a collector that reads it into a
//     slice. Measured by shipping 100,000 records and comparing against 1,000.
//  3. Memory that does not return to baseline after a job, or that accumulates
//     across jobs.
//  4. A per-event cost above a stated ceiling.
//
// # Why none of this is a Benchmark function
//
// `go test -bench` EXITS 0 ON A REGRESSION -- measured on this project, not
// assumed: internal/budget's package comment records a planted 64 KiB/op
// regression printing `65536 B/op` and still exiting 0. So every number below
// is produced by `testing.Benchmark` from an ORDINARY Test and COMPARED.
//
// # What is NOT asserted: wall-clock CPU
//
// Same refusal as internal/budget's package comment: a shared runner's variance
// exceeds the signal, and a flaky budget test gets skipped -- which reads as
// coverage while measuring nothing. `~0% CPU at idle` stays a design target for
// this capability.

func TestFalsifier1_AnIdleAgentHoldsNothingForThisCapability(t *testing.T) {
	// The only code path that runs while the agent is idle and this capability
	// is compiled in is the registry lookup per inbound frame plus constructing
	// the Service. Neither may allocate.
	//
	// FALSIFYING OBSERVATION: AllocsPerOp > 0, or a goroutine delta.
	r := budget.AssertNoAllocations(t, "logs.ship dormant", func() func() {
		s := New(Options{Logger: quiet()})
		return func() {
			if s.Runs() != 0 {
				panic("the dormancy measurement ran a ship")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("falsifier 1: %d iterations, %d allocs/op, %d B/op, goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)
}

func TestFalsifier1_BindOpensNoSubscriptionAndTouchesNoLog(t *testing.T) {
	// Bind is called from main at startup, on every agent, whether or not a
	// `logs.ship` job ever arrives. If it opened a subscription, EVERY endpoint
	// in the fleet would hold an Event Log handle forever -- and health's
	// §0.5 falsifier 5 ("an event-log check type opening its OWN OS log
	// subscription") would have a subscription to borrow, which is exactly what
	// main.go's `BorrowEventLog: nil` says does not exist.
	//
	// FALSIFYING OBSERVATION: a Source call, a file descriptor, or a goroutine
	// after Bind.
	if runtime.GOOS != "linux" {
		t.Skip("reads /proc/self/fd; CI is linux")
	}
	log := newFakeLog(1000)
	beforeFDs, beforeGo := openDescriptors(t), runtime.NumGoroutine()
	s := &Service{}
	s.Bind(Options{Sender: &countingSender{}, Source: log, Cursors: &memCursor{}, Logger: quiet()})
	if log.opens != 0 {
		t.Fatalf("Bind opened the security log %d times", log.opens)
	}
	if after := openDescriptors(t); after > beforeFDs {
		t.Fatalf("Bind left %d extra file descriptors open", after-beforeFDs)
	}
	if after := runtime.NumGoroutine(); after > beforeGo {
		t.Fatalf("Bind started %d goroutines", after-beforeGo)
	}
	if s.Runs() != 0 {
		t.Fatal("Bind ran a ship")
	}
}

func TestFalsifier1_NoFileDescriptorSurvivesAJob(t *testing.T) {
	// The reader and the cursor file are both opened inside the job. A leak here
	// is a descriptor per job on every endpoint in the fleet, forever.
	//
	// FALSIFYING OBSERVATION: an extra /proc/self/fd entry after 20 jobs
	// against a REAL cursor file (the in-memory store would not exercise the
	// path that opens anything).
	if runtime.GOOS != "linux" {
		t.Skip("reads /proc/self/fd; CI is linux")
	}
	dir := t.TempDir()
	store := fileCursorStore{path: dir + "/security-logs-cursor.json"}
	opts := Options{Sender: &countingSender{}, Now: fixedNow, Logger: quiet()}
	s := New(Options{})

	// Warm-up outside the measurement.
	if _, err := s.ship(context.Background(), opts, newFakeLog(50), store); err != nil {
		t.Fatal(err)
	}
	before := openDescriptors(t)
	for i := 0; i < 20; i++ {
		if _, err := s.ship(context.Background(), opts, newFakeLog(50+i), store); err != nil {
			t.Fatal(err)
		}
	}
	if after := openDescriptors(t); after > before {
		t.Fatalf("20 ships left %d extra open file descriptors (%d -> %d); the reader or the "+
			"cursor file is not being closed", after-before, before, after)
	}
}

func TestFalsifier2_MemoryDoesNotScaleWithTheSizeOfTheSecurityLog(t *testing.T) {
	// THE ASSERTION THIS CAPABILITY MOST NEEDS. A Windows Security log is
	// routinely hundreds of megabytes; the obvious implementation reads it into
	// a slice and the obvious test -- a hundred records -- never notices.
	//
	// So: ship a job over a 100,000-record log and a job over a 1,000-record
	// log, and require the RETAINED heap to be within an allowance. A collector
	// that accumulated even the NORMALISED events (rather than flushing and
	// reusing one 500-entry batch) would differ by ~99,000 events' worth --
	// tens of megabytes.
	//
	// Note the per-job bound also applies: one job consumes at most
	// maxEventsPerJob records, so the 100,000-record run reads 5,000 of them.
	// That IS the streaming guarantee under test -- the bound is what makes the
	// log's size irrelevant -- and the counts are asserted below so the test
	// cannot pass by doing nothing.
	//
	// FALSIFYING OBSERVATION: retained heap scaling with the log's size.
	small := budget.Retained(2, oneShip(t, 1_000))
	large := budget.Retained(2, oneShip(t, 100_000))
	const allowance = 512 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("a 100,000-record log retained %d bytes more than a 1,000-record one "+
			"(allowance %d); the collector's footprint scales with the log rather than with the "+
			"batch", delta, allowance)
	}
	t.Logf("falsifier 2: retained heap 1,000-record log = %d bytes, 100,000-record log = %d "+
		"bytes, delta = %d (allowance %d)", small, large, large-small, allowance)

	// AND THE GATE THAT ACTUALLY CATCHES THE WRONG IMPLEMENTATION, because the
	// budget.Retained comparison above DOES NOT. That was measured, not
	// reasoned about: with `reader.Next()` drained into a `[]Record` up front
	// and kept alive for the whole of drain(), the retained delta read -64
	// BYTES — it shrank — and this test passed. budget.Retained has exactly the
	// blind spot an after-the-fact RSS reading has, and for the same reason: it
	// is read after the job returns, and the thing being forbidden is transient
	// by construction. The slurped slice is garbage by the time ReadMemStats
	// runs.
	//
	// Cumulative allocation can see it, it is deterministic, and it is
	// machine-independent.
	//
	// THE COMPARISON HAS TO BE FAIR, and this is the part that takes care.
	// A 1,000-record log and a 100,000-record log do NOT do the same work: one
	// job consumes at most maxEventsPerJob records, so the small log is
	// exhausted at 1,000 while the large one stops at 5,000. Five times the
	// work legitimately costs five times the allocation, and a gate on that
	// pair would either be vacuous or permanently red.
	//
	// So both sides are logs of AT LEAST maxEventsPerJob records. Both consume
	// exactly maxEventsPerJob, both normalise exactly the same number of
	// records, and both flush exactly the same number of batches. A streaming
	// collector therefore allocates THE SAME for a 5,000-record log and a
	// 100,000-record one; the 95,000-record difference is invisible to it. A
	// collector that reads the log into a slice allocates a rendered payload for
	// every record it slurped, and the difference is the whole log.
	//
	// FALSIFYING OBSERVATION: the 100,000-record job allocating materially more
	// than the 5,000-record one. Measured 29 KB apart streaming (and NEGATIVE —
	// the large log allocated slightly less), against 166,634,104 bytes apart
	// with the slurp planted. The allowance is 16 MiB: three orders of magnitude
	// above the streaming difference and a tenth of the regression's.
	const allocAllowance = 16 << 20
	atCap := budget.Fixed("logs.ship-5k-log", 1, oneShip(t, maxEventsPerJob))
	overCap := budget.Fixed("logs.ship-100k-log", 1, oneShip(t, 100_000))
	if delta := overCap.BytesPerOp - atCap.BytesPerOp; delta > allocAllowance {
		t.Fatalf("a job over a 100,000-record log allocated %d bytes more in total than a job "+
			"over a %d-record one (allowance %d), even though BOTH consume exactly %d records. "+
			"The extra 95,000 records were read into memory instead of streamed one at a time",
			delta, maxEventsPerJob, allocAllowance, maxEventsPerJob)
	}
	t.Logf("falsifier 2: one job over a %d-record log allocated %d bytes; over a 100,000-record "+
		"log %d bytes; difference %d (allowance %d). Both consumed %d records",
		maxEventsPerJob, atCap.BytesPerOp, overCap.BytesPerOp,
		overCap.BytesPerOp-atCap.BytesPerOp, allocAllowance, maxEventsPerJob)

	// And the run is not vacuous: it really did ship the per-job maximum.
	sender := &countingSender{}
	res, err := New(Options{}).ship(context.Background(),
		Options{Sender: sender, Now: fixedNow, Logger: quiet()}, newFakeLog(100_000), &memCursor{})
	if err != nil {
		t.Fatal(err)
	}
	if res.Shipped+res.Skipped != maxEventsPerJob {
		t.Fatalf("the 100,000-record job consumed %d records; the per-job bound is %d",
			res.Shipped+res.Skipped, maxEventsPerJob)
	}
	if res.Batches < 5 {
		t.Fatalf("only %d batches; the flush path is barely exercised", res.Batches)
	}
	t.Logf("falsifier 2: one job over a 100,000-record log shipped %d events in %d batches "+
		"(%d dropped by the taxonomy filter), truncated=%v",
		res.Shipped, res.Batches, res.Skipped, res.Truncated)
}

func TestFalsifier3_ResidentMemoryReturnsToItsPreJobBaseline(t *testing.T) {
	// Measured with budget.Fixed and NOT with testing.Benchmark, for the reason
	// in Fixed's doc comment: testing.Benchmark picks its own iteration count,
	// and an RSS threshold against an unstated number of iterations measures a
	// GARBAGE RATE rather than a footprint.
	//
	// FALSIFYING OBSERVATION: RSSDelta above the limit for a STATED job count.
	//
	// THE MEDIAN OF SEVERAL SAMPLES, AND THE LIMITS ARE UNCHANGED. A single
	// RSS sample of this job measures the runtime's WARM-UP, not the job. That
	// is measured, not supposed: taking seven samples in arrival order prints
	//
	//     [868352 -49152 -172032 -188416 -286720 -409600 -356352]
	//
	// -- the first grows by ~850 KB and every one after it SHRINKS. The first
	// ship maps arenas the runtime then keeps, and `debug.FreeOSMemory` can
	// ask for them back but the kernel decides. The old single-sample form
	// read that first number and nothing else, so it was asserting a one-time
	// mapping cost against a per-job limit, and it failed on a GitHub runner
	// at 4,530,176 bytes over thirty jobs and on another machine at 802,816
	// over one -- two different assertions tripping on code that had not
	// changed. Every SHARP instrument here (allocs/op, B/op, retained heap
	// across a hundredfold spread, goroutine delta) stayed green throughout.
	//
	// So the fix is this file's own CLAUDE.md-endorsed category for a noisy
	// measurement, the one `capabilities/site`'s falsifier 2 already uses:
	// measure the MEDIAN of several independent samples and assert the SAME
	// number. Warm-up pulls one sample; the median moves only if a MAJORITY
	// grew, which is what a real leak does and a first-touch mapping does not.
	// Widening either limit would have been the other thing, and is not done
	// here: both are exactly as they were.
	const oneLimit = 512 << 10
	const samples = 7
	oneRuns := make([]int64, samples)
	var one budget.Result
	for i := range oneRuns {
		one = budget.Fixed("logs.ship-x1", 1, oneShip(t, 2_000))
		oneRuns[i] = one.RSSDelta
	}
	// Median over a COPY, so the log below keeps arrival order: which sample
	// grew matters when reading a failure, and `median` sorts in place.
	oneRSS := median(append([]int64(nil), oneRuns...))
	if oneRSS > oneLimit && !raceDetector {
		t.Fatalf("RSS grew %d bytes across ONE job over a 2,000-record log (limit %d, "+
			"samples %v, rss_is_heap_proxy=%v)", oneRSS, oneLimit, oneRuns, one.RSSIsProxy)
	}
	t.Logf("falsifier 3a: 1 job (2,000 records), RSS delta median %d bytes over samples %v "+
		"(proxy=%v), %d allocs, %d bytes",
		oneRSS, oneRuns, one.RSSIsProxy, one.AllocsPerOp, one.BytesPerOp)

	// Across a working life. `ship_interval_s` defaults to 60, so 30 jobs is
	// half an hour of a single endpoint. A larger number would be a better
	// measurement and this one is bounded by wall clock: an XML unmarshal per
	// record costs ~70 us on this runner, so 30 jobs of 700 records is already
	// ~1.5 s and is ~10x that under -race. The SHARP instrument for
	// accumulation is (c) below, which compares a hundredfold spread; this is
	// the coarse guard against a genuine leak, which would be tens of MB.
	// Three samples rather than seven: each is thirty jobs, so the wall clock
	// the comment above bounds this by applies to the sampling too. Three is
	// enough for a median to need a majority -- two of three -- to move.
	const lifetimeLimit = 4 << 20
	const lifetimeSamples = 3
	lifeRuns := make([]int64, lifetimeSamples)
	var r budget.Result
	for i := range lifeRuns {
		r = budget.Fixed("logs.ship-x30", 30, oneShip(t, 700))
		lifeRuns[i] = r.RSSDelta
	}
	lifeRSS := median(append([]int64(nil), lifeRuns...))
	if lifeRSS > lifetimeLimit && !raceDetector {
		t.Fatalf("RSS grew %d bytes across %d jobs (limit %d, samples %v, "+
			"rss_is_heap_proxy=%v)", lifeRSS, r.N, lifetimeLimit, lifeRuns, r.RSSIsProxy)
	}
	t.Logf("falsifier 3b: %d jobs, RSS delta median %d bytes over samples %v (proxy=%v), "+
		"%d allocs/op, %d B/op, goroutine delta %d",
		r.N, lifeRSS, lifeRuns, r.RSSIsProxy, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)

	// The half an RSS reading cannot answer: allocation that is RETAINED.
	// Compared across a hundredfold spread of JOBS, because internal/budget
	// already measured that a tenfold spread does not catch a planted 24-byte
	// leak.
	const few, many = 1, 100
	small := budget.Retained(few, oneShip(t, 200))
	large := budget.Retained(many, oneShip(t, 200))
	const allowance = 64 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("%d jobs retained %d bytes more than %d (allowance %d); something accumulates "+
			"per job", many, delta, few, allowance)
	}
	t.Logf("falsifier 3c: retained heap %d jobs = %d bytes, %d jobs = %d bytes, delta = %d",
		few, small, many, large, large-small)
}

func TestFalsifier4_ThePerEventCostIsBoundedAndChecked(t *testing.T) {
	// The number a regression would move first: an XML unmarshal into
	// `map[string]any`, a `raw` built with fmt, a per-record regexp compile, or
	// a batch slice grown by append instead of reused.
	//
	// FALSIFYING OBSERVATION: bytes-per-EVENT above the ceiling. The ceiling is
	// 32 KiB per event, which sounds generous and is not: one rendered Security
	// event is ~2 KB of XML, and the unmarshal plus the taxonomy map plus the
	// wire struct is the bulk of it.
	const ceiling = 32 << 10
	const records = 1_500
	r := budget.Measure("logs.ship-1500-records", oneShip(t, records))
	if r.N == 0 {
		t.Fatal("the benchmark ran zero iterations; the measurement is vacuous")
	}
	perEvent := r.BytesPerOp / records
	if perEvent > ceiling {
		t.Fatalf("one event costs %d bytes (%d B/op over %d records, %d allocs/op), above the "+
			"%d ceiling. CLAUDE.md: 'Any change that regresses idle footprint is a defect.'",
			perEvent, r.BytesPerOp, records, r.AllocsPerOp, ceiling)
	}
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("falsifier 4: %d iterations over %d records, %d allocs/op, %d B/op, %d bytes/event "+
		"(ceiling %d), goroutine delta %d",
		r.N, records, r.AllocsPerOp, r.BytesPerOp, perEvent, ceiling, r.Goroutines)
}

// oneShip returns a setup producing one complete ship over a synthetic log of
// `records` records.
//
// The sender COUNTS AND DISCARDS. A spy that kept every event would report the
// test's own apparatus as this capability's footprint -- 100,000 events is tens
// of megabytes of retention that has nothing to do with the code under test.
// health's falsifier file was bitten by exactly this (a spy sender retaining a
// message log read as 53 KB of engine growth).
//
// The LOG ITSELF is built once, outside the measured function, for the same
// reason: generating 100,000 synthetic records inside the loop would measure
// the generator.
func oneShip(t *testing.T, records int) func() func() {
	t.Helper()
	return func() func() {
		log := newFakeLog(records)
		s := New(Options{})
		opts := Options{Sender: &countingSender{}, Now: fixedNow, Logger: quiet()}
		ctx := context.Background()
		return func() {
			// A FRESH cursor per iteration, so every iteration does the same
			// amount of work. A shared cursor would make the first iteration
			// ship everything and the rest ship nothing, and the average would
			// describe neither.
			if _, err := s.ship(ctx, opts, log, &memCursor{}); err != nil {
				panic(err)
			}
		}
	}
}

// openDescriptors counts this process's open file descriptors.
func openDescriptors(t *testing.T) int {
	t.Helper()
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatalf("cannot read /proc/self/fd: %v", err)
	}
	return len(entries)
}
