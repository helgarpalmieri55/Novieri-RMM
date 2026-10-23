package provision

import (
	"encoding/json"
	"os"
	"runtime"
	"sort"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// THE `provision.enroll` IDLE BUDGET, MEASURED.
//
// docs/modules/provisioning/spec.md §15 stated this capability's footprint in
// one sentence -- "`provision.enroll` is dormant until a job; the agent's role
// is light post-config verification" -- with no benchmark and no falsifier.
// CLAUDE.md forbids reporting a footprint as verified until a benchmark runs
// and a falsifier is stated. This file is where that stops being a claim.
//
// The falsifiers, stated here and copied into spec §9.2:
//
//  1. Anything resident for this capability on an agent with no provisioning
//     job in flight: an allocation, a goroutine, or an open file descriptor.
//  2. Resident memory that does not return to its pre-job baseline after a
//     verification, or that grows with the number of verifications.
//  3. A verification that costs more than a bounded amount of transient
//     allocation -- the number a rewrite into `map[string]any` would move first.
//
// # Why none of this is a Benchmark function
//
// `go test -bench` EXITS 0 ON A REGRESSION. That was measured on this project,
// not assumed: internal/budget's package comment records a planted 64 KiB/op
// regression printing `65536 B/op` and still exiting 0. A budget stated as a
// benchmark is a log line, not a gate. So every number below is produced by
// `testing.Benchmark` called from an ORDINARY Test (through internal/budget's
// harness) and COMPARED against a threshold.
//
// # What is NOT asserted here: wall-clock CPU
//
// Same refusal, for the same reason, as internal/budget's package comment: a
// shared runner's variance exceeds the signal, a threshold loose enough not to
// flake is loose enough to pass a real regression, and a flaky budget test gets
// skipped -- which reads as coverage while measuring nothing. `~0% CPU at idle`
// stays a design target for this capability and is approached through the
// machine-independent proxies below.

func TestFalsifier1_AnIdleAgentHoldsNothingForThisCapability(t *testing.T) {
	// The only code path that runs while the agent is idle and this capability
	// is compiled in is the registry lookup the runner performs per inbound
	// frame, plus constructing the Service. Neither may allocate, because both
	// happen on an agent that is doing nothing.
	//
	// FALSIFYING OBSERVATION: AllocsPerOp > 0, or a goroutine delta.
	r := budget.AssertNoAllocations(t, "provision.enroll dormant", func() func() {
		s := New(Options{Logger: quiet()})
		return func() {
			if s.Runs() != 0 {
				panic("the dormancy measurement ran a verification")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("falsifier 1: %d iterations, %d allocs/op, %d B/op, goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)
}

func TestFalsifier1_NoFileDescriptorIsHeldAcrossAVerification(t *testing.T) {
	// The other half of falsifier 1, and the half an allocation count cannot
	// see. This capability's production Host reads a hostname and (on macOS) a
	// directory; the health probe reads three files out of the data directory.
	// All of them must be closed before the job returns.
	//
	// FALSIFYING OBSERVATION: an extra entry in /proc/self/fd after a
	// verification that did open something.
	if runtime.GOOS != "linux" {
		t.Skip("reads /proc/self/fd; CI is linux")
	}
	s := New(Options{Host: &fakeHost{name: "ws-0142"}, Health: healthy, Logger: quiet()})
	raw := paramsJSON(t, "ws-0142", map[string]any{"domain_join": map[string]any{"domain": "corp"}}, nil)

	// Warm-up outside the measurement: the first read of anything can leave a
	// runtime-internal descriptor behind that belongs to the runtime, not here.
	if _, err := s.run(raw); err != nil {
		t.Fatal(err)
	}
	before := openDescriptors(t)
	for i := 0; i < 50; i++ {
		if _, err := s.run(raw); err != nil {
			t.Fatal(err)
		}
	}
	if after := openDescriptors(t); after > before {
		t.Fatalf("50 verifications left %d extra open file descriptors (%d -> %d)",
			after-before, before, after)
	}
}

func TestFalsifier2_ResidentMemoryReturnsToItsPreJobBaseline(t *testing.T) {
	// Measured with budget.Fixed and NOT with testing.Benchmark, for the reason
	// recorded in Fixed's own doc comment: testing.Benchmark picks its own
	// iteration count, and an RSS threshold against an unstated number of
	// iterations measures a GARBAGE RATE rather than a footprint.
	//
	// FALSIFYING OBSERVATION: RSSDelta above the limit, printed in bytes, for a
	// STATED verification count.

	// (a) The falsifier as written: ONE verification, RSS back where it started.
	//
	// The MEDIAN of several independent samples, asserted against the SAME
	// limit -- the category `capabilities/site` and `capabilities/logs`
	// already use for a one-job RSS delta. A single sample went red on
	// macos-latest on main (fe705b0, 2026-09-29: 10496 bytes against 4096)
	// while every other number in the run stayed flat: one first-touch page
	// is a sample, a leak is a majority. The limit is exactly as it was.
	const oneLimit = 4 << 10
	const samples = 7
	oneRuns := make([]int64, samples)
	var one budget.Result
	for i := range oneRuns {
		one = budget.Fixed("provision.enroll-x1", 1, oneVerification(t))
		oneRuns[i] = one.RSSDelta
	}
	// Median over a COPY, so the messages keep arrival order.
	oneRSS := median(append([]int64(nil), oneRuns...))
	if oneRSS > oneLimit && !raceDetector {
		t.Fatalf("RSS grew %d bytes (median) across ONE verification (limit %d, samples %v, "+
			"rss_is_heap_proxy=%v); the capability did not return to its pre-job baseline",
			oneRSS, oneLimit, oneRuns, one.RSSIsProxy)
	}
	t.Logf("falsifier 2a: 1 verification, RSS delta median %d bytes over samples %v (proxy=%v), "+
		"%d allocs, %d bytes", oneRSS, oneRuns, one.RSSIsProxy, one.AllocsPerOp, one.BytesPerOp)

	// (b) The same question over a working life. One verification returning to
	//     baseline is necessary and not sufficient: a capability retaining 64
	//     bytes per job would pass (a) forever. 10,000 is a stated number and
	//     far more provisioning runs than one endpoint will ever see -- which
	//     is the point: the margin is enormous, so a failure here is a leak and
	//     not a threshold artefact.
	const lifetimeLimit = 2 << 20
	r := budget.Fixed("provision.enroll-x10000", 10_000, oneVerification(t))
	if r.RSSDelta > lifetimeLimit && !raceDetector {
		t.Fatalf("RSS grew %d bytes across %d verifications (limit %d, rss_is_heap_proxy=%v)",
			r.RSSDelta, r.N, lifetimeLimit, r.RSSIsProxy)
	}
	t.Logf("falsifier 2b: %d verifications, RSS delta %d bytes (proxy=%v), %d allocs/op, %d B/op, "+
		"goroutine delta %d", r.N, r.RSSDelta, r.RSSIsProxy, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)

	// (c) The half an RSS reading cannot answer. Allocation that is FREED never
	//     shows as growth; allocation that is RETAINED -- a slice of every
	//     result, a map keyed by run id -- is invisible to a per-op count.
	//     Compared across a HUNDREDFOLD spread, because internal/budget already
	//     measured that a tenfold spread does not catch a planted 24-byte leak.
	const few, many = 100, 10_000
	small := budget.Retained(few, oneVerification(t))
	large := budget.Retained(many, oneVerification(t))
	const allowance = 16 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("%d verifications retained %d bytes more than %d (allowance %d); something "+
			"accumulates per job", many, delta, few, allowance)
	}
	t.Logf("falsifier 2c: retained heap %d = %d bytes, %d = %d bytes, delta = %d",
		few, small, many, large, large-small)
}

func TestFalsifier3_OneVerificationCostsABoundedAmount(t *testing.T) {
	// The number a regression would move first. `post_config` is
	// map[string]json.RawMessage precisely so an unknown member is never
	// decoded; a rewrite to map[string]any would allocate a live Go value for
	// every member of every document, and every assertion above would stay
	// green because the cost is transient.
	//
	// FALSIFYING OBSERVATION: bytes-per-verification above the ceiling.
	//
	// THE CEILING WAS 8 KiB AND THAT WAS TOO LOOSE TO MEAN ANYTHING. Measured,
	// not reasoned about: the exact regression this comment names -- post_config
	// decoded eagerly into map[string]any, live Go values for every member --
	// read 3,792 B/op and 103 allocs/op and PASSED, because the clean
	// measurement is 1,488 B/op and the ceiling sat five and a half times above
	// it. A gate with that much slack absorbs a regression that more than
	// doubles the cost, which is the only kind of regression this assertion
	// could ever have been for.
	//
	// 3 KiB is a little over twice the clean measurement -- enough headroom that
	// a Go release shifting json's internals will not flake it, and below the
	// planted regression by a comfortable margin.
	const ceiling = 3 << 10
	// AND AN ALLOCATION-COUNT CEILING, which is the sharper of the two here and
	// is the property stated directly: "a live Go value for every member of
	// every document" is a COUNT before it is a size. Clean is 40 allocs/op
	// against the six-member post_config oneVerification builds; the planted
	// map[string]any read 103.
	const allocCeiling = 64
	r := budget.Measure("provision.enroll-one", oneVerification(t))
	if r.N == 0 {
		t.Fatal("the benchmark ran zero iterations; the measurement is vacuous")
	}
	if r.BytesPerOp > ceiling {
		t.Fatalf("one verification allocates %d bytes/op (%d allocs) over %d iterations, above "+
			"the %d ceiling. CLAUDE.md: 'Any change that regresses idle footprint is a defect.'",
			r.BytesPerOp, r.AllocsPerOp, r.N, ceiling)
	}
	if r.AllocsPerOp > allocCeiling {
		t.Fatalf("one verification does %d allocations per op (%d bytes) over %d iterations, above "+
			"the %d ceiling. `post_config` is map[string]json.RawMessage precisely so an unknown "+
			"member is never decoded; this many allocations means every member of every document "+
			"is being turned into a live Go value", r.AllocsPerOp, r.BytesPerOp, r.N, allocCeiling)
	}
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("one verification: %d iterations, %d allocs/op (ceiling %d), %d B/op (ceiling %d), "+
		"goroutine delta %d", r.N, r.AllocsPerOp, allocCeiling, r.BytesPerOp, ceiling, r.Goroutines)
}

// oneVerification returns a setup producing one complete verification through
// the capability, against a params document with a realistic post_config --
// including members this package never reads, which is where a map[string]any
// rewrite would show up.
//
// The Host retains NOTHING beyond a call counter: a spy that kept every result
// would report the test's own apparatus as the capability's footprint, which is
// a mistake this module has already made once (health's falsifier file read
// 53 KB of engine growth from a message log in a spy sender).
func oneVerification(t *testing.T) func() func() {
	t.Helper()
	raw := paramsJSON(t, "ws-0142", map[string]any{
		"hostname":    "ws-0142",
		"domain_join": map[string]any{"domain": "corp.toeshee.local", "ou": "OU=Workstations"},
		"local_admin": map[string]any{"username": "toeshee-admin"},
		"timezone":    "Europe/London",
		"locale":      "en-GB",
		"apps":        []string{"chrome", "office", "zoom", "slack"},
	}, nil)
	return func() func() {
		s := New(Options{
			Host:    &fakeHost{name: "ws-0142", domain: DomainStatus{Supported: true, Joined: true, Domain: "CORP"}},
			Health:  func(time.Time) (bool, string) { return true, "" },
			Version: "1.4.0",
			Now:     func() time.Time { return budgetEpoch },
			Logger:  quiet(),
		})
		// One warm-up outside the measurement, so a lazily initialised anything
		// is not charged to the first iteration.
		if _, err := s.run(raw); err != nil {
			panic(err)
		}
		return func() {
			if _, err := s.run(json.RawMessage(raw)); err != nil {
				panic(err)
			}
		}
	}
}

var budgetEpoch = time.Date(2026, 8, 6, 12, 0, 0, 0, time.UTC)

// openDescriptors counts this process's open file descriptors.
func openDescriptors(t *testing.T) int {
	t.Helper()
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatalf("cannot read /proc/self/fd: %v", err)
	}
	return len(entries)
}

// median returns the middle value of samples, sorted in place.
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
