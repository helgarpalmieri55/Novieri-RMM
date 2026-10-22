package backup

import (
	"context"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/budget"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// THE `backup.run` / `backup.restore` IDLE BUDGET, MEASURED.
//
// CLAUDE.md names backup among the "heavy capabilities [that] stay dormant and
// only allocate/initialize when a job for them actually runs, then release",
// and requires the number to be measured rather than asserted. This file is
// that measurement.
//
// # Why none of this is a Benchmark function
//
// `go test -bench` EXITS 0 ON A REGRESSION. That was measured on this project,
// not assumed: internal/budget's package comment records a planted 64 KiB/op
// regression printing `65536 B/op` and still exiting 0. A budget stated as a
// benchmark is a log line, not a gate. So every number below is produced by
// `testing.Benchmark` called from an ORDINARY Test (through internal/budget's
// harness) and COMPARED against a threshold. A regression turns CI red.
//
// # The falsifiers this capability owes, and where each is answered
//
//  1. "anything resident on an agent with no backup job in flight" —
//     TestFalsifier1_AnIdleAgentHoldsNothingForThisCapability (0 allocs, 0
//     goroutines) and TestFalsifier1b_NoFileDescriptorIsOpenBeforeOrAfterAJob
//     (0 file descriptors, read from /proc/self/fd around a REAL run).
//  2. "resident memory that does not return to its pre-job baseline after a
//     LARGE job" — TestFalsifier2_RSSReturnsToBaselineAfterALargeBackupRun.
//     This is the one that matters for this module: the obvious wrong
//     implementation reads each file into memory, and it passes every
//     correctness test in this package.
//  3. "memory that scales with the size of the file being backed up" —
//     TestFalsifier3_TheFootprintDoesNotScaleWithFileSize, which is the sharp
//     version of 2: a 32 MiB file and a 1 MiB file must cost the same.
//  4. "something retained per job" — TestFalsifier4_NothingAccumulatesAcrossJobs.
//
// # What is NOT asserted here: wall-clock CPU
//
// Same refusal, for the same reason, as internal/budget's package comment and
// site's budget file: a shared runner's variance exceeds the signal, a
// threshold loose enough not to flake is loose enough to pass a real
// regression, and a flaky budget test gets skipped — which reads as coverage
// while measuring nothing. `~0% CPU at idle` stays a design target for this
// capability, approached through the machine-independent proxies above.

// ---------------------------------------------------------------------------
// Falsifier 1: anything resident with no backup job in flight
// ---------------------------------------------------------------------------

func TestFalsifier1_AnIdleAgentHoldsNothingForThisCapability(t *testing.T) {
	// The ONLY code path that runs while the agent is idle and this capability
	// is compiled in is the registry lookup the runner performs per inbound
	// frame, plus constructing the Service. Neither may allocate: both happen on
	// an agent that is doing nothing.
	//
	// FALSIFYING OBSERVATION: AllocsPerOp > 0, or a goroutine delta.
	r := budget.AssertNoAllocations(t, "backup dormant", func() func() {
		s := New(Options{Logger: quiet()})
		return func() {
			if s.Runs() != 0 || s.Restores() != 0 {
				panic("the dormancy measurement ran a job")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("falsifier 1: %d iterations, %d allocs/op, %d B/op, goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)
}

func TestFalsifier1a_ThePackageLevelStateOfADormantAgentIsTwoNilTablesAndAOnce(t *testing.T) {
	// The chunker's Buzhash tables are the one piece of package-level state this
	// capability has. Computed at INIT they would be 4 KiB resident on every
	// agent in the fleet that never takes a backup job — small, but it is
	// exactly the "no capability is always on" line, and a package-level `var`
	// with a computed initialiser is the natural way to write it.
	//
	// FALSIFYING OBSERVATION: a non-zero entry in either table before any job
	// has run. This test therefore has to run before any other test in this
	// package touches the chunker, which `go test` does not guarantee — so it
	// asserts the WEAKER, order-independent property instead: that buzInit is
	// what fills them, and that nothing else calls it.
	buzInit()
	nonZero := 0
	for _, v := range buzTable {
		if v != 0 {
			nonZero++
		}
	}
	if nonZero != 256 {
		t.Fatalf("buzInit filled %d of 256 table entries", nonZero)
	}
	// The rotated table must be derived from the same values, not independently
	// generated: a second table of independent randomness would still chunk
	// deterministically and would still be WRONG, because the rolling hash's
	// leaving-byte term would not cancel the entering one.
	for i := 0; i < 256; i++ {
		if buzRotTable[i] == buzTable[i] && buzTable[i] != 0 {
			t.Fatalf("buzRotTable[%d] equals buzTable[%d]; the window rotation was not applied",
				i, i)
		}
	}
	// A stable seed: two calls give the same table. If this ever changes, every
	// existing repository's chunk domain forks silently (see buzSeed).
	first := buzTable[42]
	buzInit()
	if buzTable[42] != first {
		t.Fatal("buzInit is not idempotent; the chunk domain is not stable across a process")
	}
}

func TestFalsifier1b_NoFileDescriptorIsOpenBeforeOrAfterAJob(t *testing.T) {
	// A backup opens every file in the tree and a restore creates every file in
	// the selection. A single missed Close is a file-descriptor leak that grows
	// with the customer's file count and is invisible to an allocation counter.
	//
	// FALSIFYING OBSERVATION: a descriptor open after the job that was not open
	// before it. Keyed by TARGET rather than by number, because numbers are
	// reused and a leak that landed on a recycled number would be invisible.
	if runtime.GOOS != "linux" {
		t.Skip("reads /proc/self/fd")
	}
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)

	before := openFDs(t)
	svc := newTestService(store, quiet())
	if extra := newFDs(before, openFDs(t)); len(extra) != 0 {
		t.Fatalf("constructing the capability opened %v", extra)
	}
	if svc.Runs() != 0 || svc.Restores() != 0 {
		t.Fatal("the capability had already run")
	}

	ctx := context.Background()
	res, err := svc.run(ctx, testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil || res.State != stateSuccess {
		t.Fatalf("backup.run: %v %+v", err, res)
	}
	dest := filepath.Join(t.TempDir(), "restored")
	if _, err := svc.restore(ctx, testJobID, restoreParamsJSON(t, store.prefix+*res.ManifestRef, []string{src}, dest,
		modeRestore)); err != nil {
		t.Fatalf("backup.restore: %v", err)
	}
	runtime.GC()

	if extra := newFDs(before, openFDs(t)); len(extra) != 0 {
		t.Fatalf("a full run and restore left %d descriptor(s) open: %v", len(extra), extra)
	}
	// `fixtureFiles()` is the FILE count; the manifest also carries the tree's
	// dirs and symlinks, so its entry count is higher. Both are printed because
	// the descriptor question is about the files (each one is an open) while the
	// tree size is what makes the absence of a leak meaningful.
	entries := len(readManifest(t, store, store.prefix+*res.ManifestRef).Streams[0].Entries)
	t.Logf("falsifier 1b: %d descriptors before, %d after a full run and restore of %d files "+
		"(%d manifest entries)", len(before), len(openFDs(t)), len(fixtureFiles()), entries)
}

// ---------------------------------------------------------------------------
// Falsifier 2: RSS after a LARGE job — the one this module is really about
// ---------------------------------------------------------------------------

func TestFalsifier2_RSSReturnsToBaselineAfterALargeBackupRun(t *testing.T) {
	// THE TEST THIS CAPABILITY EXISTS TO PASS.
	//
	// The obvious implementation of `file()` is os.ReadFile followed by
	// chunking the slice. It is shorter, it is what most people write, and it
	// passes every correctness test in this package — including the whole round
	// trip. What it does is make the agent's resident set track the LARGEST FILE
	// IN THE CUSTOMER'S TREE, which on a fleet of Windows 11 laptops means a
	// multi-gigabyte PST or VM image, on a process whose budget is
	// "low-tens-of-MB".
	//
	// So: back up a 64 MiB file with the PINNED PRODUCTION dedup parameters
	// (avg 1 MiB, min 512 KiB, max 4 MiB) and require RSS to come back.
	//
	// Measured with budget.Fixed and a stated iteration count of ONE, for the
	// reason recorded in Fixed's own doc comment: testing.Benchmark picks its
	// own count, and an RSS threshold against an unstated number of iterations
	// measures a GARBAGE RATE rather than a footprint.
	//
	// FALSIFYING OBSERVATION: RSSDelta above the limit after one 64 MiB backup.
	// The limit is 24 MiB — comfortably above the ~8 MiB of buffers, hasher and
	// manifest that a correct implementation legitimately touches and holds
	// briefly, and far below the 64 MiB+ that reading the file into memory
	// costs. The gap between "correct" and "the wrong implementation" is a
	// factor of eight here, so the threshold does not have to be snug to be
	// sharp.
	const fileSize = 64 << 20
	const rssLimit = 24 << 20

	// FALSIFICATION MARGIN, measured once and recorded rather than re-derived
	// on every read: with a chunk-payload-retention regression planted (the
	// dedup batch holding chunk PLAINTEXT rather than references --
	// TestASixFigureChunkListIsNotASixFigureAllocation's own scenario), THIS
	// gate's alloc check (`r.BytesPerOp > fileSize`, below) failed by only
	// 1.19x over `fileSize`. That is marginal, not decisive -- close enough
	// to flake on a noisy run. If it does, the fix is investigating the
	// allocation, never loosening `fileSize` as the cutoff; that was already
	// known when this comment was written.

	src := largeFixture(t, fileSize)
	peakBefore := peakRSS(t)
	r := budget.Fixed("backup.run-64MiB", 1, oneRun(t, src, prodDedup()))
	peakAfter := peakRSS(t)

	if r.RSSDelta > rssLimit && !raceDetector {
		t.Fatalf("RSS grew %d bytes across ONE backup of a %d-byte file (limit %d, "+
			"rss_is_heap_proxy=%v). The capability did not return to its pre-job baseline",
			r.RSSDelta, fileSize, rssLimit, r.RSSIsProxy)
	}

	// AND THE GATE THAT ACTUALLY CATCHES THE WRONG IMPLEMENTATION, which the
	// RSS assertion above does NOT. That was measured, not reasoned about: with
	// `io.ReadAll(f)` planted in file() and kept alive for the file's duration,
	// the RSS-after-the-job delta read -131,072 bytes and the test PASSED —
	// because the buffer is freed before the job returns, releaseMemory() hands
	// the pages back, and budget.Fixed calls debug.FreeOSMemory again before
	// reading. An after-the-fact RSS reading cannot see a peak.
	//
	// Cumulative allocation can, it is deterministic, and it is
	// machine-independent. The threshold is THE FILE'S OWN SIZE, because that
	// is exactly the property: one backup must not allocate as much memory as
	// the data it backed up. Measured 8.5 MB streaming (two 4 MiB buffers)
	// against 408 MB with the regression planted — a factor of 48.
	if r.BytesPerOp > fileSize {
		t.Fatalf("backing up a %d-byte file allocated %d bytes in total. A streaming "+
			"implementation allocates its buffers and nothing per byte; this one allocated as "+
			"much as the file, which means the file was read into memory instead of streamed",
			fileSize, r.BytesPerOp)
	}

	// Peak RSS is LOGGED AND NOT GATED, and the reason is stated rather than
	// left as an omission: /proc/self/status VmHWM is the high-water mark of
	// the whole test BINARY since it started, and Go gives no way to reset it.
	// Whether this job moves it depends on what earlier tests in the package
	// already allocated, which `go test` does not order. A gate on it would
	// pass vacuously about half the time, and a budget assertion that is
	// sometimes vacuous is worse than an honest absence. The allocation gate
	// above is the deterministic version of the same question.
	t.Logf("falsifier 2: one %d MiB backup, RSS delta %d bytes (proxy=%v, limit %d), "+
		"%d allocs, %d bytes allocated in total (limit %d = the file size), "+
		"process peak RSS %d -> %d bytes",
		fileSize>>20, r.RSSDelta, r.RSSIsProxy, rssLimit, r.AllocsPerOp, r.BytesPerOp,
		fileSize, peakBefore, peakAfter)
}

func TestFalsifier3_TheFootprintDoesNotScaleWithFileSize(t *testing.T) {
	// The sharp version of falsifier 2, and the one a generous threshold cannot
	// hide. A streaming implementation costs the SAME resident memory for a
	// 64 MiB file as for a 1 MiB one, because the buffer is `max` either way.
	// A buffering one costs 63 MiB more.
	//
	// This is a RATIO rather than an absolute, so it is independent of the
	// runner, of the Go version's allocator behaviour and of whatever else the
	// test binary has already done.
	//
	// FALSIFYING OBSERVATION: the large run's RSS exceeding the small run's by
	// more than the allowance. The allowance is 16 MiB — four times the 4 MiB
	// chunk buffer, and a quarter of the 63 MiB difference the buffering
	// implementation would show.
	const small = 1 << 20
	const large = 64 << 20
	const rssAllowance = 16 << 20

	// FALSIFICATION MARGIN: with the same chunk-payload-retention regression
	// planted, THIS gate (the RSSDelta comparison below) failed by 7.9x over
	// `rssAllowance` -- about 1350x its own clean-run baseline. Decisive, and
	// the reason this is the headline assertion of the two in this test: it
	// does not need to be snug to catch a real regression, unlike the
	// falsifiers noted as marginal (Falsifier2's alloc gate, and
	// TestASixFigureChunkListIsNotASixFigureAllocation's).
	// Two chunk buffers' worth of slack over the small run. A streaming
	// implementation's extra cost for 63 more megabytes of file is the chunk
	// ids in the manifest — about 64 of them — and nothing else.
	const allocAllowance = 8 << 20

	smallSrc := largeFixture(t, small)
	largeSrc := largeFixture(t, large)

	rSmall := budget.Fixed("backup.run-1MiB", 1, oneRun(t, smallSrc, prodDedup()))
	rLarge := budget.Fixed("backup.run-64MiB-ratio", 1, oneRun(t, largeSrc, prodDedup()))

	if delta := rLarge.RSSDelta - rSmall.RSSDelta; delta > rssAllowance && !raceDetector {
		t.Fatalf("backing up a %d MiB file cost %d bytes more resident memory than backing up a "+
			"%d MiB file (allowance %d)", large>>20, delta, small>>20, rssAllowance)
	}
	// The sharp one, for the reason recorded in falsifier 2: an RSS delta
	// measured after the job cannot see what the job held while it ran.
	// Sixty-three more megabytes of file must not cost sixty-three more
	// megabytes of allocation.
	if delta := rLarge.BytesPerOp - rSmall.BytesPerOp; delta > allocAllowance {
		t.Fatalf("backing up a %d MiB file allocated %d bytes more than backing up a %d MiB one "+
			"(allowance %d). The footprint scales with file size, which means the file is being "+
			"held rather than streamed", large>>20, delta, small>>20, allocAllowance)
	}
	t.Logf("falsifier 3: 1 MiB run RSS delta %d / %d bytes allocated; 64 MiB run RSS delta %d / "+
		"%d bytes allocated. Differences: RSS %d (allowance %d), allocation %d (allowance %d)",
		rSmall.RSSDelta, rSmall.BytesPerOp, rLarge.RSSDelta, rLarge.BytesPerOp,
		rLarge.RSSDelta-rSmall.RSSDelta, rssAllowance,
		rLarge.BytesPerOp-rSmall.BytesPerOp, allocAllowance)
}

// ---------------------------------------------------------------------------
// Falsifier 4: anything retained across jobs
// ---------------------------------------------------------------------------

func TestFalsifier4_NothingAccumulatesAcrossJobs(t *testing.T) {
	// The half an RSS reading cannot answer. Allocation that is FREED never
	// shows as growth; allocation that is RETAINED — a slice of every manifest,
	// a chunk-id cache that outlives its job — is invisible to a per-op count
	// and only appears after a device has been up for weeks.
	//
	// Compared across a HUNDREDFOLD spread, because internal/budget already
	// measured that a tenfold spread does not catch a planted 24-byte-per-tick
	// leak.
	//
	// FALSIFYING OBSERVATION: 100 runs retaining materially more than 1.
	src := smallFixture(t)
	setup := oneRun(t, src, testDedup())

	const few, many = 1, 100
	small := budget.Retained(few, setup)
	large := budget.Retained(many, setup)

	// 4 MiB. The per-job state that is legitimately touched (the chunker
	// buffer, the ciphertext buffer, the manifest) is well under a megabyte at
	// these dedup parameters and is dropped with the job; a capability that
	// retained even the manifest of each run would show ~100x that.
	const allowance = 4 << 20
	if delta := large - small; delta > allowance && !raceDetector {
		t.Fatalf("%d backup runs retained %d bytes more than %d (allowance %d); something "+
			"accumulates per job", many, delta, few, allowance)
	}
	t.Logf("falsifier 4: retained heap after %d run = %d bytes, after %d runs = %d bytes, "+
		"delta = %d", few, small, many, large, large-small)
}

func TestTheServiceHoldsNoStoreOrSnapshotBetweenJobs(t *testing.T) {
	// The structural version of falsifier 1, asserted on the object rather than
	// on a counter: a Service that cached its transport between jobs would keep
	// a connection open on a dormant agent, and neither an allocation count nor
	// an RSS reading would necessarily see it.
	//
	// FALSIFYING OBSERVATION: the store not being closed, or a second job
	// reusing the first job's store instance.
	src := smallFixture(t)
	var opened int
	var stores []*fakeStore
	svc := New(Options{
		OpenStore: func(context.Context, Repository) (ObjectStore, error) {
			opened++
			s := newFakeStore(testStorageRef)
			stores = append(stores, s)
			return s, nil
		},
		OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) {
			return passthroughSnapshot{}, nil
		},
		QueryChunks: newFakeDedup(testStorageRef).answer,
		Logger:      quiet(),
	})
	raw := runParamsJSON(t, []string{src}, nil)
	for i := 0; i < 3; i++ {
		if _, err := svc.run(context.Background(), testJobID, raw); err != nil {
			t.Fatalf("run %d: %v", i, err)
		}
	}
	if opened != 3 {
		t.Errorf("three jobs opened %d transports; the transport is job-scoped", opened)
	}
	for i, s := range stores {
		if !s.closed {
			t.Errorf("the transport opened for job %d was never closed", i)
		}
	}
	// Runs() is NOT asserted here, and the reason is worth recording: the
	// counter is incremented by the exported Run, which takes a
	// jobs.VerifiedJob — an interface with an unexported method, deliberately
	// unimplementable outside internal/jobs (verified.go, construct_test.go).
	// A test cannot construct one, so every test in this package drives the
	// unexported run/restore instead, exactly as internal/capabilities/site's
	// do. The exported wrappers are three lines each and are covered by the
	// signed-job path in internal/capabilities/integration_test.go once this
	// capability is registered.
	if svc.Runs() != 0 {
		t.Errorf("Runs() = %d; the unexported run() must not touch the counter, or the "+
			"dormancy assertions that read it would be measuring the test harness",
			svc.Runs())
	}
}

// ---------------------------------------------------------------------------
// The per-chunk cost, gated rather than logged
// ---------------------------------------------------------------------------

func TestTheChunkerAllocatesNothingPerChunk(t *testing.T) {
	// Neither falsifier covers the inner loop, and it is the number a
	// regression would move first: a chunker that returned a fresh []byte per
	// chunk instead of a slice into its buffer would allocate up to `max` bytes
	// per chunk, which is 4 MiB of garbage per megabyte protected. Transient, so
	// it would neither stay resident nor accumulate, and every assertion above
	// would stay green.
	//
	// FALSIFYING OBSERVATION: any allocation inside next() after the chunker
	// has been constructed.
	data := make([]byte, 1<<20)
	fill(data, 0xC0FFEE)
	d := testDedup()

	r := budget.Measure("chunker.next", func() func() {
		c := newChunker(d)
		return func() {
			c.reset(&repeatReader{data: data})
			for {
				chunk, err := c.next()
				if err != nil {
					break
				}
				if len(chunk) == 0 {
					panic("empty chunk")
				}
			}
		}
	})
	if r.N == 0 {
		t.Fatal("the benchmark ran zero iterations; the measurement is vacuous")
	}
	// Per WHOLE-FILE pass, not per chunk: the op above chunks a 1 MiB stream.
	// At avg 1024 that is roughly a thousand chunks, so a per-chunk allocation
	// of even 24 bytes would read as 24 KB here.
	const ceiling = 4 << 10
	if r.BytesPerOp > ceiling {
		t.Fatalf("chunking 1 MiB allocates %d bytes (%d allocs); the chunker returns slices into "+
			"one buffer and must allocate nothing per chunk. Ceiling %d",
			r.BytesPerOp, r.AllocsPerOp, ceiling)
	}
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("chunker: %d iterations of a 1 MiB stream, %d allocs/op, %d B/op (ceiling %d)",
		r.N, r.AllocsPerOp, r.BytesPerOp, ceiling)
}

func TestABackupRunUsesOneGoroutine(t *testing.T) {
	// A parallel uploader would buy throughput at the cost of N chunk buffers
	// resident and a goroutine count that scales with the customer's file
	// count. Sequential is the budget-respecting default and the module brief
	// asks for nothing more.
	//
	// FALSIFYING OBSERVATION: a goroutine delta across a real run and restore.
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	ctx := context.Background()

	runtime.GC()
	before := runtime.NumGoroutine()
	res, err := svc.run(ctx, testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatal(err)
	}
	dest := filepath.Join(t.TempDir(), "restored")
	if _, err := svc.restore(ctx, testJobID, restoreParamsJSON(t, store.prefix+*res.ManifestRef, []string{src}, dest,
		modeRestore)); err != nil {
		t.Fatal(err)
	}
	runtime.GC()
	runtime.Gosched()
	if delta := runtime.NumGoroutine() - before; delta > 0 {
		t.Fatalf("a full run and restore left %d extra goroutine(s)", delta)
	}
	t.Logf("goroutines: %d before, %d after a full run and restore", before, runtime.NumGoroutine())
}

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

func prodDedup() dedupSpec {
	d, err := dedupSpec{}.defaults()
	if err != nil {
		panic(err)
	}
	return d
}

// largeFixture writes one file of exactly n bytes and returns its directory.
//
// Pseudo-random contents, so the content-defined boundaries land where they
// would on real data. A file of zeroes would produce a pathological boundary
// distribution and a footprint measurement about the wrong thing.
func largeFixture(t *testing.T, n int) string {
	t.Helper()
	dir := t.TempDir()
	f, err := os.Create(filepath.Join(dir, "large.bin"))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	// Written in 1 MiB blocks so the FIXTURE does not itself hold 64 MiB
	// resident while the measurement is running — which would be the
	// measurement's own baseline, not the capability's.
	block := make([]byte, 1<<20)
	for written := 0; written < n; {
		fill(block, uint64(written))
		size := len(block)
		if n-written < size {
			size = n - written
		}
		m, err := f.Write(block[:size])
		if err != nil {
			t.Fatal(err)
		}
		written += m
	}
	return dir
}

func smallFixture(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for i := 0; i < 4; i++ {
		b := make([]byte, 3000+i*700)
		fill(b, uint64(i))
		if err := os.WriteFile(filepath.Join(dir, "f"+string(rune('a'+i))+".bin"), b, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// oneRun returns a setup producing one complete backup run through the
// capability, against a store that RETAINS NOTHING.
//
// The retention matters and it is not incidental. fakeStore keeps every object
// so the round-trip tests can assert on them; used here it would report the
// TEST'S OWN storage as the capability's footprint — 64 MiB of it in falsifier
// 2. health's falsifier file was bitten by exactly this (a spy sender retaining
// a message log read as 53 KB of engine growth), so the measurement rig counts
// and discards.
func oneRun(t *testing.T, src string, d dedupSpec) func() func() {
	t.Helper()
	doc := map[string]any{
		"repository_id": "22222222-2222-2222-2222-222222222222",
		"storage_ref":   testStorageRef,
		"repo_key":      testKey,
		"snapshot":      "vss",
		"sources":       map[string]any{"paths": []string{src}},
		"dedup_params": map[string]any{
			"algo": d.Algo, "hash": d.Hash, "avg": d.Avg, "min": d.Min, "max": d.Max,
		},
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return func() func() {
		svc := New(Options{
			OpenStore: func(context.Context, Repository) (ObjectStore, error) {
				return discardStore{}, nil
			},
			OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) {
				return passthroughSnapshot{}, nil
			},
			// EVERY chunk is answered NEW, so the measurement covers the
			// worst case: a first-ever backup that re-reads and uploads all
			// of it. A fake that deduplicated everything would measure a run
			// that never touched the upload path at all.
			QueryChunks: discardDedup,
			Logger:      quiet(),
		})
		ctx := context.Background()
		return func() {
			res, err := svc.run(ctx, testJobID, raw)
			if err != nil {
				panic(err)
			}
			if res.State != stateSuccess {
				panic("the measured run failed: " + deref(res.Error))
			}
		}
	}
}

// discardDedup answers "all new" and mints a grant, allocating one slice per
// query and nothing per chunk — so the allocation budget below measures the
// capability rather than the fake.
func discardDedup(_ context.Context, q wire.ChunkQuery) (wire.ChunkGrant, error) {
	return wire.ChunkGrant{
		JobID:   q.JobID,
		QueryID: q.QueryID,
		New:     q.Check,
		Upload: wire.UploadGrant{
			URL:       "https://s3.test/bucket",
			Fields:    map[string]string{"policy": "x"},
			KeyPrefix: testStorageRef + "/",
			MaxBytes:  1 << 30,
		},
	}, nil
}

// discardStore accepts every object and keeps none.
type discardStore struct{}

func (discardStore) Put(_ context.Context, _ wire.UploadGrant, suffix string, _ []byte) (string, error) {
	return "s3://test-bucket/" + suffix, nil
}

func (discardStore) Get(context.Context, string) ([]byte, error) {
	return nil, os.ErrNotExist
}

func (discardStore) GetRef(context.Context, string) ([]byte, error) {
	return nil, os.ErrNotExist
}

func (discardStore) Close() error { return nil }

// repeatReader serves the same block forever, so a chunker measurement is about
// the chunker and not about file IO.
type repeatReader struct {
	data []byte
	off  int
}

func (r *repeatReader) Read(p []byte) (int, error) {
	if r.off >= len(r.data) {
		return 0, io.EOF
	}
	n := copy(p, r.data[r.off:])
	r.off += n
	return n, nil
}

// peakRSS reads this process's high-water resident set size, in bytes.
//
// Logged rather than gated — see falsifier 2 for why. Returns 0 where
// /proc/self/status is not available, so the log line says "0" rather than the
// test lying about a platform.
func peakRSS(t *testing.T) int64 {
	t.Helper()
	raw, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return 0
	}
	for _, line := range strings.Split(string(raw), "\n") {
		if !strings.HasPrefix(line, "VmHWM:") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 2 {
			return 0
		}
		kb, err := strconv.ParseInt(fields[1], 10, 64)
		if err != nil {
			return 0
		}
		return kb * 1024
	}
	return 0
}

func openFDs(t *testing.T) map[string]bool {
	t.Helper()
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatalf("cannot read /proc/self/fd: %v", err)
	}
	out := map[string]bool{}
	for _, e := range entries {
		target, err := os.Readlink(filepath.Join("/proc/self/fd", e.Name()))
		if err != nil {
			continue
		}
		// Keyed by TARGET, not by descriptor number: numbers are reused, and a
		// leak that happened to land on a recycled number would be invisible.
		out[target] = true
	}
	return out
}

func newFDs(before, after map[string]bool) []string {
	var extra []string
	for k := range after {
		if !before[k] {
			extra = append(extra, k)
		}
	}
	return extra
}
