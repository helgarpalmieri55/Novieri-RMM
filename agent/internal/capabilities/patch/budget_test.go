package patch

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/budget"
	"github.com/toeshee/it-suite/agent/internal/scheduler"
)

// THE `patch.scan` / `patch.apply` IDLE BUDGET, MEASURED.
//
// CLAUDE.md names patch FIRST in its list of heavy capabilities that "stay
// dormant and only allocate/initialize when a job for them actually runs, then
// release", and it says in the same breath that a number owed a benchmark and
// not given one is a design target and must be described as such. It also
// records why: health.check's six falsifiers "sat unrun for an entire phase"
// because the code lived where CI could not reach it. That excuse is gone.
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
// threshold. A regression turns CI red.
//
// # What is NOT asserted: wall-clock CPU
//
// The same refusal, for the same reason, as internal/budget's package comment
// and site's budget file: a shared runner's variance exceeds the signal, a
// threshold loose enough not to flake is loose enough to pass a real
// regression, and a flaky budget test gets skipped -- which reads as coverage
// while measuring nothing. `~0% CPU at idle` stays a DESIGN TARGET for this
// capability and is approached through the machine-independent proxies below.
//
// # The falsifiers, stated
//
//  1. A file descriptor, goroutine, timer-wheel entry or allocation resident on
//     an agent with no patch job in flight.
//  2. Resident memory that does not return to its pre-job baseline after one
//     scan and one apply.
//  3. Anything accumulating per job -- a cache of the last scan, a map keyed by
//     identifier -- invisible to a per-op allocation count and only visible
//     after a device has been up for weeks.
//  4. A per-job cost that grows by an order of magnitude, which is what
//     unmarshalling into `map[string]any` or building documents with fmt would
//     do while every assertion above stayed green.

// ---------------------------------------------------------------------------
// Falsifier 1: anything resident with no patch job in flight
// ---------------------------------------------------------------------------

func TestFalsifier1_AnIdleAgentHoldsNothingForThisCapability(t *testing.T) {
	// The ONLY code path that runs while the agent is idle and this capability
	// is compiled in is the registry lookup the runner performs per inbound
	// frame, plus the existence of the Service. Neither may allocate, because
	// both happen on an agent that is doing nothing.
	//
	// FALSIFYING OBSERVATION: AllocsPerOp > 0, or a goroutine delta.
	r := budget.AssertNoAllocations(t, "patch dormant", func() func() {
		s := New(Options{Logger: quiet()})
		return func() {
			if s.Scans() != 0 || s.Applies() != 0 {
				panic("the dormancy measurement ran a job")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("falsifier 1: %d iterations, %d allocs/op, %d B/op, goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)
}

func TestFalsifier1_TheCapabilityAddsNothingToTheTimerWheel(t *testing.T) {
	// No local schedule, asserted rather than described. The scan cadence is
	// server-side (`scan_interval_hours`, spec §12's beat) for the same reason
	// net.sweep's and site.wake's are: a locally scheduled patch apply is a
	// STANDING AUTHORIZATION, signed once and replayed for weeks after the
	// approval was revoked.
	//
	// FALSIFYING OBSERVATION: a wheel entry after a completed scan and apply.
	wheel := scheduler.New(scheduler.NewFake(testEpoch), quiet())
	before := wheel.Len()

	s := serviceWith(&fakeEngine{family: "linux", available: []entry{
		{Source: sourceAPT, Identifier: "a@1", Title: "a", Severity: "low"},
	}})
	if _, err := s.scan(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := s.apply(context.Background(), applyJSON(t, "if_required", ref("apt", "a@1"))); err != nil {
		t.Fatal(err)
	}

	if after := wheel.Len(); after != before {
		t.Fatalf("the timer wheel went from %d entries to %d across one scan and one apply; this "+
			"capability schedules nothing locally", before, after)
	}
}

func TestFalsifier1_NothingIsOpenedBeforeAJobArrivesAndEverythingIsReleasedAfter(t *testing.T) {
	// File descriptors, around REAL jobs. The fake engine opens nothing, so
	// this measures the capability's own plumbing -- which is the part that
	// would grow an fd if someone added a log file, a state file or a cached
	// scan on disk.
	//
	// FALSIFYING OBSERVATION: an fd count that does not return to baseline.
	fds := func() int {
		entries, err := os.ReadDir("/proc/self/fd")
		if err != nil {
			t.Skipf("no /proc/self/fd on this host: %v", err)
		}
		return len(entries)
	}

	idle := fds()
	s := serviceWith(&fakeEngine{family: "linux", available: []entry{
		{Source: sourceAPT, Identifier: "a@1", Title: "a", Severity: "low"},
	}})
	for i := 0; i < 50; i++ {
		if _, err := s.scan(context.Background()); err != nil {
			t.Fatal(err)
		}
		if _, err := s.apply(context.Background(), applyJSON(t, "never", ref("apt", "a@1"))); err != nil {
			t.Fatal(err)
		}
	}
	after := fds()
	if after > idle {
		t.Fatalf("file descriptors went from %d to %d across 50 scans and 50 applies", idle, after)
	}
	t.Logf("falsifier 1: fds %d before, %d after 50 scans and 50 applies", idle, after)
}

// ---------------------------------------------------------------------------
// Falsifier 2 and 3: memory back to baseline, and nothing accumulating
// ---------------------------------------------------------------------------

func TestFalsifier2And3_ResidentMemoryReturnsToBaselineAndNothingAccumulates(t *testing.T) {
	// Measured with budget.Fixed and NOT with testing.Benchmark, for the reason
	// Fixed's own doc comment records: testing.Benchmark picks its own
	// iteration count, and an RSS threshold against an unstated number of
	// iterations measures a GARBAGE RATE rather than a footprint. The health
	// engine read 5.4 MB of RSS growth that way with nothing leaking.

	// (a) One scan plus one apply returns to its pre-job baseline, within one
	//     4 KiB page. Judged on the heap retained after GC, not on RSS: RSS
	//     moves in whole pages for reasons of the runtime's own (scavenger,
	//     arenas), and a one-page RSS limit for a single job failed 4-5 runs
	//     in 10 with the capability retaining 0-96 bytes. RSS is still logged
	//     here and still bounded over a working life in (b).
	//
	//     WARMED ONCE FIRST. The very first job in a process also pays for
	//     process-wide lazy initialisation that is kept for the process's life
	//     and never repeats -- time.Local's zone data, package-level caches --
	//     and on macOS that alone measured 5248 bytes (CI, 2026-09-24) against
	//     Linux's near zero. That is not the capability failing to return to
	//     its baseline, so the baseline is taken after one warm-up job; the
	//     working-life check in (b) is what catches anything that accumulates.
	const oneJobLimit = 4 << 10
	warmed := func() func() {
		op := oneJobPair(t)()
		op()
		return op
	}
	//
	//     THE LOWEST OF THREE INDEPENDENT TRIALS. Even warmed, one run in five
	//     on Linux under a loaded `go test ./...` read ~5.2 KB for a single
	//     job, and the next run of the same binary read ~0 -- heap the runtime
	//     or the test binary held across the ReadMemStats pair, not the
	//     capability (nothing in this package starts a goroutine or a timer).
	//     Each trial builds a fresh service and runs its own job, so a
	//     capability that genuinely keeps memory per job keeps it in all three
	//     and still fails here; a one-off reading cannot. (b) below remains
	//     the check that catches accumulation over a working life.
	retained := int64(-1)
	var trials []int64
	for i := 0; i < 3; i++ {
		r := budget.Retained(1, warmed)
		trials = append(trials, r)
		if retained < 0 || r < retained {
			retained = r
		}
	}
	if retained > oneJobLimit && !raceDetector {
		t.Fatalf("falsifier 2: %d bytes retained on the heap after ONE scan and ONE apply "+
			"in every one of three trials %v (limit %d); the capability did not return to "+
			"its pre-job baseline", retained, trials, oneJobLimit)
	}
	one := budget.Fixed("patch-x1", 1, oneJobPair(t))
	t.Logf("falsifier 2a: 1 scan + 1 apply, %d bytes retained; RSS delta %d bytes (proxy=%v), "+
		"%d allocs, %d bytes", retained, one.RSSDelta, one.RSSIsProxy, one.AllocsPerOp, one.BytesPerOp)

	// (b) THE SAME QUESTION OVER A WORKING LIFE. One job returning to baseline
	//     is necessary and not sufficient: a capability that kept 64 bytes per
	//     job would pass (a) forever and still be a footprint bug on a device
	//     up for a month. 20,000 job pairs is a stated number and a lot of
	//     them -- a device scanning daily and applying in every window reaches
	//     it in decades.
	//
	//     The limit is 4 MiB, deliberately well clear of the measurement rather
	//     than snug against it: the transient garbage 20,000 documents churn
	//     grows the ALLOCATOR ARENA, and that is what RSS reports even after
	//     debug.FreeOSMemory. A threshold set at the top of the observed spread
	//     would flake, and a flaky budget test gets skipped -- which reads as
	//     coverage while measuring nothing. (c) is the sharp instrument; this
	//     is the coarse guard against a genuine leak, which would be tens of MB.
	const lifetimeLimit = 4 << 20
	const jobs = 20_000
	r := budget.Fixed("patch-x20000", jobs, oneJobPair(t))
	if r.RSSDelta > lifetimeLimit && !raceDetector {
		t.Fatalf("falsifier 2: RSS grew %d bytes across %d scan+apply pairs (limit %d, "+
			"rss_is_heap_proxy=%v)", r.RSSDelta, r.N, lifetimeLimit, r.RSSIsProxy)
	}
	t.Logf("falsifier 2b: %d scan+apply pairs, RSS delta %d bytes (proxy=%v), %d allocs/op, "+
		"%d B/op, goroutine delta %d",
		r.N, r.RSSDelta, r.RSSIsProxy, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)

	// (c) FALSIFIER 3, the half an RSS reading cannot answer. Allocation that
	//     is FREED never shows as growth; allocation that is RETAINED -- a
	//     cache of the last scan, a map keyed by identifier that is never
	//     cleared -- is invisible to a per-op count and only appears after
	//     weeks. Compared across a HUNDREDFOLD spread, because internal/budget
	//     already measured that a tenfold spread does not catch a planted
	//     24-byte-per-op leak.
	const few, many = 100, 10_000
	small := budget.Retained(few, oneJobPair(t))
	large := budget.Retained(many, oneJobPair(t))
	const allowance = 16 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("falsifier 3: %d job pairs retained %d bytes more than %d (allowance %d); "+
			"something accumulates per job -- a cached scan, or a map that is never cleared",
			many, delta, few, allowance)
	}
	t.Logf("falsifier 3: retained heap %d pairs = %d bytes, %d pairs = %d bytes, delta = %d",
		few, small, many, large, large-small)
}

// ---------------------------------------------------------------------------
// Falsifier 4: the per-job cost itself, gated rather than logged
// ---------------------------------------------------------------------------

func TestFalsifier4_OneScanCostsABoundedAmountAndTheBoundIsChecked(t *testing.T) {
	// The number a regression would move first. A scan that started
	// unmarshalling into `map[string]any`, or building identifiers with
	// fmt.Sprintf per entry, would allocate an order of magnitude more while
	// every assertion above stayed green -- the cost is transient, so it
	// neither stays resident nor accumulates.
	//
	// The measurement uses a TWENTY-ENTRY engine, which is a realistic Windows
	// 11 machine a month behind. A one-entry measurement would have a ceiling
	// so small that any realistic device blew through it.
	//
	// FALSIFYING OBSERVATION: bytes-per-scan above the ceiling. Verified by
	// planting one: an escaping 4 KiB allocation per entry in normalizeEntry
	// read 96,528 B/op against the 32 KiB ceiling and failed. Worth recording
	// HOW that check has to be written -- the first attempt planted a bare
	// `_ = make([]byte, 64<<10)`, the compiler eliminated it as dead, and the
	// mutation SURVIVED while proving nothing. A regression check that does not
	// escape is not a regression check.
	const ceiling = 32 << 10
	r := budget.Measure("patch-one-scan", oneScan(t))
	if r.N == 0 {
		t.Fatal("the benchmark ran zero iterations; the measurement is vacuous")
	}
	if r.BytesPerOp > ceiling {
		t.Fatalf("one 20-entry scan allocates %d bytes/op (%d allocs) over %d iterations, above "+
			"the %d ceiling. CLAUDE.md: 'Any change that regresses idle footprint is a defect.'",
			r.BytesPerOp, r.AllocsPerOp, r.N, ceiling)
	}
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("one 20-entry scan: %d iterations, %d allocs/op, %d B/op (ceiling %d), goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, ceiling, r.Goroutines)
}

func TestFalsifier4_OneApplyCostsABoundedAmountAndTheBoundIsChecked(t *testing.T) {
	// The apply path's own arithmetic: unmarshalling the params, indexing what
	// the OS offers, and building one outcome per request. The engine does
	// nothing, so this is the capability's cost and not the installer's.
	//
	// FALSIFYING OBSERVATION: bytes-per-apply above the ceiling.
	const ceiling = 32 << 10
	r := budget.Measure("patch-one-apply", oneApply(t))
	if r.N == 0 {
		t.Fatal("the benchmark ran zero iterations; the measurement is vacuous")
	}
	if r.BytesPerOp > ceiling {
		t.Fatalf("one 5-patch apply allocates %d bytes/op (%d allocs) over %d iterations, above "+
			"the %d ceiling", r.BytesPerOp, r.AllocsPerOp, r.N, ceiling)
	}
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("one 5-patch apply: %d iterations, %d allocs/op, %d B/op (ceiling %d), goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, ceiling, r.Goroutines)
}

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

// budgetEngine is a fake that RETAINS NOTHING.
//
// The behavioural fake in patch_test.go keeps every entry Install saw so it can
// be asserted on, and that retention is invisible until you measure retained
// heap: 20,000 applies x 5 entries is megabytes of the TEST's own apparatus
// reported as the capability's footprint. health's falsifier file was bitten by
// exactly this (a spy sender retaining a message log read as 53 KB of engine
// growth), and site's records the same. This one counts and discards.
type budgetEngine struct {
	available []entry
	installs  int
}

func (b *budgetEngine) Family() string                              { return "windows" }
func (b *budgetEngine) Available(context.Context) ([]entry, error)  { return b.available, nil }
func (b *budgetEngine) Installed(context.Context) ([]string, error) { return nil, nil }
func (b *budgetEngine) Install(context.Context, entry) error        { b.installs++; return nil }
func (b *budgetEngine) RebootRequired(context.Context) bool         { return false }
func (b *budgetEngine) Close()                                      {}

// budgetCatalog is 20 entries: a realistic Windows 11 machine a month behind.
func budgetCatalog() []entry {
	ids := []string{
		"KB5034123", "KB5034441", "KB5033375", "KB5032190", "KB5031354",
		"KB5030310", "KB5029351", "KB5028185", "KB5027231", "KB5026372",
		"KB5025239", "KB5024351", "KB5023706", "KB5022845", "KB5021255",
		"KB5020030", "KB5019980", "KB5018410", "KB5017321", "KB5016629",
	}
	out := make([]entry, 0, len(ids))
	for i, id := range ids {
		kb := id
		e := entry{
			Source:     sourceWindowsUpdate,
			Identifier: id,
			Title:      "2026-08 Cumulative Update for Windows 11 Version 24H2 (" + id + ")",
			Severity:   "critical",
			KB:         &kb,
		}
		if i%3 == 0 {
			e.CVE = []string{"CVE-2026-1001", "CVE-2026-1002"}
		}
		out = append(out, e)
	}
	return out
}

func budgetService(t *testing.T) *Service {
	t.Helper()
	return New(Options{
		NewEngine: func(context.Context) (engine, error) {
			return &budgetEngine{available: budgetCatalog()}, nil
		},
		Clock:  func() time.Time { return testEpoch },
		Logger: quiet(),
		// The OS halves are stubbed like the engine: these tests measure the
		// capability's own plumbing. On macOS the real boot-time and console
		// probes left a few KB of runtime garbage in the heap proxy the budget
		// falls back to there, which read as a 5 KB "leak" against a 4 KiB page.
		BootTime:     func(time.Time) (time.Time, bool) { return testEpoch.Add(-time.Hour), true },
		NewRestarter: func() restarter { return budgetRestarter{} },
	})
}

// budgetRestarter keeps nothing, unlike fakeRestarter, which records every
// call and would itself grow across iterations.
type budgetRestarter struct{}

func (budgetRestarter) Session(context.Context) session { return session{} }
func (budgetRestarter) Schedule(context.Context, time.Duration, string) error {
	return nil
}

func oneScan(t *testing.T) func() func() {
	t.Helper()
	return func() func() {
		s := budgetService(t)
		ctx := context.Background()
		// One warm-up outside the measurement, so a lazily initialised anything
		// is not charged to the first iteration.
		if _, err := s.scan(ctx); err != nil {
			panic(err)
		}
		return func() {
			if _, err := s.scan(ctx); err != nil {
				panic(err)
			}
		}
	}
}

func oneApply(t *testing.T) func() func() {
	t.Helper()
	raw := budgetApplyParams(t, 5)
	return func() func() {
		s := budgetService(t)
		ctx := context.Background()
		if _, err := s.apply(ctx, raw); err != nil {
			panic(err)
		}
		return func() {
			if _, err := s.apply(ctx, json.RawMessage(raw)); err != nil {
				panic(err)
			}
		}
	}
}

func oneJobPair(t *testing.T) func() func() {
	t.Helper()
	raw := budgetApplyParams(t, 5)
	return func() func() {
		s := budgetService(t)
		ctx := context.Background()
		if _, err := s.scan(ctx); err != nil {
			panic(err)
		}
		if _, err := s.apply(ctx, raw); err != nil {
			panic(err)
		}
		return func() {
			if _, err := s.scan(ctx); err != nil {
				panic(err)
			}
			if _, err := s.apply(ctx, json.RawMessage(raw)); err != nil {
				panic(err)
			}
		}
	}
}

func budgetApplyParams(t *testing.T, n int) json.RawMessage {
	t.Helper()
	catalog := budgetCatalog()
	refs := make([]map[string]string, 0, n)
	for i := 0; i < n && i < len(catalog); i++ {
		refs = append(refs, ref(catalog[i].Source, catalog[i].Identifier))
	}
	return applyJSON(t, "if_required", refs...)
}
