package netsweep

import (
	"context"
	"net/netip"
	"runtime"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// THE `net.sweep` IDLE BUDGET, MEASURED.
//
// # This file did not exist, and that is the finding
//
// CLAUDE.md requires every capability's footprint to be "measured by a
// benchmark that runs, or the number is not claimed". netsweep and inventory
// are the two capabilities that never got a budget file at all — not a weak
// gate, none — so there was no number to be wrong and nothing to regress
// against.
//
// # Why none of this is a Benchmark function
//
// `go test -bench` EXITS 0 ON A REGRESSION. Measured on this project, not
// assumed: internal/budget's package comment records a planted 64 KiB/op
// regression printing `65536 B/op` and still exiting 0. So every number below
// is produced through internal/budget's harness from an ORDINARY Test and
// COMPARED.
//
// # What is NOT asserted: wall-clock CPU
//
// Same refusal as internal/budget's package comment: a shared runner's variance
// exceeds the signal, and a flaky budget test gets skipped — which reads as
// coverage while measuring nothing. `~0% CPU at idle` stays a design target.

func TestFalsifier1_AnIdleAgentHoldsNothingForThisCapability(t *testing.T) {
	// The only code path that runs while the agent is idle and this capability
	// is compiled in is the registry lookup per inbound frame plus constructing
	// the Service. Neither may allocate: both happen on an agent doing nothing.
	//
	// FALSIFYING OBSERVATION: AllocsPerOp > 0, or a goroutine delta.
	r := budget.AssertNoAllocations(t, "net.sweep dormant", func() func() {
		s := New(Options{Logger: quiet()})
		return func() {
			if s.Runs() != 0 {
				panic("the dormancy measurement ran a sweep")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("falsifier 1: %d iterations, %d allocs/op, %d B/op, goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)
}

// ---------------------------------------------------------------------------
// Falsifier 2: the plan, which is the only thing here that scales
// ---------------------------------------------------------------------------

func TestFalsifier2_EnumerationCostsABoundedAmountPerAddressAndTheBoundIsChecked(t *testing.T) {
	// `enumerate` is the one place in this capability whose cost is set by the
	// job rather than by a constant: it expands every signed prefix into a slice
	// of netip.Addr and a dedup map keyed by the same.
	//
	// MEASURED, on this runner, one call each:
	//
	//	/24     254 addresses        53,840 bytes        34 allocs
	//	/20   4,094 addresses     1,007,360 bytes        72 allocs
	//	/16  65,534 addresses    18,548,208 bytes       577 allocs
	//	/12 1,048,574 addresses 295,858,432 bytes     8,292 allocs
	//
	// That is ~282 bytes per enumerated address — a 24-byte netip.Addr in the
	// slice, the same again as a map key, and the growth of both.
	//
	// This test gates the PER-ADDRESS cost, which is the number a regression
	// would move first: an implementation that kept a struct per address, or
	// that resolved a name during enumeration, would multiply it while every
	// other assertion in this file stayed green.
	//
	// FALSIFYING OBSERVATION: bytes-per-enumerated-address above the ceiling, or
	// an ALLOCATION COUNT that scales with the address count.
	//
	// THE BYTE CEILING ALONE IS NOT ENOUGH, and that was measured rather than
	// assumed. Planting the exact regression this test is for — one rendered
	// addr.String() retained per address — moved the cost from 283 to 383
	// bytes/address and PASSED, because 512 is not quite twice the clean
	// measurement and a 100-byte-per-address regression fits underneath it. A
	// ceiling with that little daylight is the same slack that let provision's
	// 8 KiB gate absorb its own named regression.
	//
	// The ALLOCATION COUNT is the sharp instrument here, and it states the
	// property directly: enumeration appends into a slice and a map, both of
	// which grow by DOUBLING, so its allocation count is logarithmic in the
	// address count and not proportional to it. Clean is 580 allocations for
	// 65,534 addresses — 0.009 each. The planted per-address string read 66,140,
	// which is 1.009 each: a factor of 114, and no threshold in between is
	// arbitrary.
	const ceiling = 512
	// One allocation per sixteen addresses. Clean sits 7x under this; anything
	// allocating per address sits 16x over it.
	const allocsPerAddrDivisor = 16
	const target = "10.0.0.0/16"
	// The bound is set ABOVE the prefix on purpose. This test measures the cost
	// of enumerating an address; falsifier 2b below measures that the bound
	// binds. Passing a binding limit here would silently turn this into a second
	// copy of that test and stop measuring per-address cost at all.
	const unbinding = 1 << 17

	plan, errs := enumerate([]string{target}, unbinding)
	if len(errs) != 0 {
		t.Fatalf("enumerating %s reported %v", target, errs)
	}
	if len(plan) == 0 {
		t.Fatal("enumeration produced nothing; the measurement would be vacuous")
	}

	r := budget.Fixed("net.sweep-enumerate-/16", 1, func() func() {
		return func() {
			out, _ := enumerate([]string{target}, unbinding)
			if len(out) == 0 {
				panic("enumeration produced nothing")
			}
		}
	})
	perAddr := r.BytesPerOp / int64(len(plan))
	if perAddr > ceiling {
		t.Fatalf("enumerating %s cost %d bytes for %d addresses, %d bytes each (ceiling %d, "+
			"%d allocs)", target, r.BytesPerOp, len(plan), perAddr, ceiling, r.AllocsPerOp)
	}
	if allocCeiling := int64(len(plan) / allocsPerAddrDivisor); r.AllocsPerOp > allocCeiling {
		t.Fatalf("enumerating %s did %d allocations for %d addresses (ceiling %d, %d bytes). "+
			"The slice and the dedup map both grow by doubling, so the count must be "+
			"logarithmic in the address count; this one allocates per address",
			target, r.AllocsPerOp, len(plan), allocCeiling, r.BytesPerOp)
	}
	t.Logf("falsifier 2: enumerating %s produced %d addresses for %d bytes (%d allocs), "+
		"%d bytes/address (ceiling %d), %d allocs (ceiling %d)",
		target, len(plan), r.BytesPerOp, r.AllocsPerOp, perAddr, ceiling,
		r.AllocsPerOp, len(plan)/allocsPerAddrDivisor)
}

func TestFalsifier2b_TheCostOfPlanningIsSetByMaxTargetsAndNotByThePrefix(t *testing.T) {
	// THIS TEST REPLACES ONE THAT RECORDED THE DEFECT IT NOW GATES. Until this
	// commit `enumerate` took no limit: it expanded EVERY address of EVERY
	// signed prefix, and `run` applied `max_targets` afterwards with
	// `plan[:budget]`. The bound sized the ANSWER while the thing that produced
	// it was unbounded.
	//
	// MEASURED ON THIS REPOSITORY, BEFORE AND AFTER, one call each:
	//
	//	                         before (unbounded)        after (bounded)
	//	/12, max_targets 4096       295,863,936 B             1,007,456 B
	//	/8,  max_targets 65536    5,035,381,632 B            18,548,400 B
	//
	// Five gigabytes of peak allocation to probe at most 65,536 addresses, on a
	// process whose stated budget in CLAUDE.md is "low-tens-of-MB resident
	// memory". Two orders of magnitude over, and INVISIBLE to a resident-memory
	// or retained-heap reading — the slice and the dedup map are both freed
	// before the handler returns, so falsifier 3 above ran green throughout.
	// That is the whole reason this file gates CUMULATIVE ALLOCATION.
	//
	// # The sharp instrument here is the RATIO, not the byte ceiling
	//
	// Same lesson as falsifier 2, applied before it could bite: an absolute
	// bytes-per-address ceiling has to be picked, and a picked number has slack
	// a regression can hide in. So the primary assertion is DIMENSIONLESS and
	// self-calibrating — plan the SAME number of addresses out of two prefixes
	// 256x apart in size, and require the cost to be the same.
	//
	// It states the property exactly: the cost is set by the BOUND, not by the
	// PREFIX. A /20 is smaller than the bound, so its leg is the cost of the
	// bound not binding at all; a /12 is 256x larger, so under the old code its
	// leg was 256x more expensive. Measured: the ratio is 1.00 now and was 294
	// before. There is no arbitrary threshold in between.
	//
	// NOTE THE DELIBERATE CHOICE OF /12 RATHER THAN /8 for the large leg. The
	// /8 has more daylight (271x) but a CI runner that fails this gate would
	// allocate five gigabytes to find out and be OOM-killed instead of failing
	// cleanly — a gate that cannot report its own failure is not a gate. The
	// /12 fails in 295 MB and under a second.
	//
	// FALSIFYING OBSERVATION: planning N addresses out of a large prefix costing
	// materially more than planning N out of a small one, or the per-planned-
	// address cost rising above the secondary ceiling.
	const (
		bound = 4097          // what run() passes for the default max_targets
		big   = "10.0.0.0/12" // 1,048,574 addresses — 256x the bound
		small = "10.0.0.0/20" // 4,094 addresses — smaller than the bound
	)
	// Ratio of 1.25. The two legs plan within three addresses of each other, so
	// clean is 1.00; the unbounded implementation this replaces was 294.
	const ratioNumer, ratioDenom = 5, 4
	// Secondary, and only a sanity bound: clean measures 245 bytes per planned
	// address (a 24-byte netip.Addr in the slice, the same again as a map key,
	// and the doubling overshoot of both). It is stated loosely on purpose
	// because the doubling overshoot moves it with the bound — the ratio above
	// is what carries this test's sharpness, exactly as the allocation count
	// carries falsifier 2's.
	const perAddrCeiling = 384

	bigPlan, bigErrs := enumerate([]string{big}, bound)
	smallPlan, smallErrs := enumerate([]string{small}, bound)
	if len(bigErrs) != 0 || len(smallErrs) != 0 {
		t.Fatalf("enumeration reported %v / %v", bigErrs, smallErrs)
	}
	if len(bigPlan) != bound {
		t.Fatalf("%s planned %d addresses, not the bound of %d; the large leg is not actually "+
			"bounded and this measurement would be vacuous", big, len(bigPlan), bound)
	}
	if len(smallPlan) >= bound {
		t.Fatalf("%s planned %d addresses, which reaches the bound of %d; the small leg must be "+
			"the cost of the bound NOT binding", small, len(smallPlan), bound)
	}

	measure := func(target string) int64 {
		return budget.Fixed("net.sweep-plan-"+target, 1, func() func() {
			return func() {
				out, _ := enumerate([]string{target}, bound)
				if len(out) == 0 {
					panic("enumeration produced nothing")
				}
			}
		}).BytesPerOp
	}
	bigBytes, smallBytes := measure(big), measure(small)

	if bigBytes*int64(ratioDenom) > smallBytes*int64(ratioNumer) {
		t.Fatalf("planning %d addresses out of %s cost %d bytes but out of %s cost %d — a %.1fx "+
			"ratio, above %.2f. The prefix is 256x larger and the bound is identical, so the cost "+
			"must be too: this implementation expands the whole prefix and bounds the answer "+
			"afterwards, which is the defect that cost 5 GB for a /8",
			bound, big, bigBytes, small, smallBytes,
			float64(bigBytes)/float64(smallBytes), float64(ratioNumer)/float64(ratioDenom))
	}
	if perAddr := bigBytes / int64(bound); perAddr > perAddrCeiling {
		t.Fatalf("planning %d addresses out of %s cost %d bytes, %d each (ceiling %d)",
			bound, big, bigBytes, perAddr, perAddrCeiling)
	}
	t.Logf("falsifier 2b: planning %d addresses cost %d bytes out of %s and %d bytes out of %s "+
		"— ratio %.2f (ceiling %.2f), %d bytes per planned address (ceiling %d)",
		bound, bigBytes, big, smallBytes, small,
		float64(bigBytes)/float64(smallBytes), float64(ratioNumer)/float64(ratioDenom),
		bigBytes/int64(bound), perAddrCeiling)
}

// ---------------------------------------------------------------------------
// Falsifier 3: anything retained across sweeps
// ---------------------------------------------------------------------------

func TestFalsifier3_NothingAccumulatesAcrossSweeps(t *testing.T) {
	// The half an allocation count cannot answer. Allocation that is FREED never
	// shows as growth; allocation that is RETAINED — a cache of the last plan, a
	// map of every host ever seen — is invisible to a per-op count and only
	// appears after a device has been up for weeks.
	//
	// Compared across a HUNDREDFOLD spread, because internal/budget already
	// measured that a tenfold spread does not catch a planted 24-byte-per-tick
	// leak.
	//
	// FALSIFYING OBSERVATION: 100 sweeps retaining materially more than 1.
	const few, many = 1, 100
	small := budget.Retained(few, oneSweep(t))
	large := budget.Retained(many, oneSweep(t))

	const allowance = 512 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("%d sweeps retained %d bytes more than %d (allowance %d); something accumulates "+
			"per sweep", many, delta, few, allowance)
	}
	t.Logf("falsifier 3: retained heap after %d sweep = %d bytes, after %d = %d bytes, delta = %d",
		few, small, many, large, large-small)
}

func TestASweepLeavesNoGoroutineAndClosesItsEmitter(t *testing.T) {
	// A sweep opens a raw socket and rate-limits with a clock. A missed Close is
	// a descriptor per job; a spawned prober that is never joined is a goroutine
	// per job.
	//
	// FALSIFYING OBSERVATION: a goroutine delta, or an emitter left open.
	spy := newSpyEmitter()
	svc := sweepWith(t, Options{OpenEmitter: spy.open, Logger: quiet()})
	raw := paramsJSON(t, []string{"192.0.2.0/28"}, map[string]any{"max_targets": 16})

	runtime.GC()
	before := runtime.NumGoroutine()
	if _, err := svc.run(context.Background(), raw); err != nil {
		t.Fatal(err)
	}
	runtime.GC()
	runtime.Gosched()

	if delta := runtime.NumGoroutine() - before; delta > 0 {
		t.Fatalf("one sweep left %d extra goroutine(s)", delta)
	}
	if spy.closes != spy.opens {
		t.Fatalf("the sweep opened %d emitters and closed %d", spy.opens, spy.closes)
	}
	if len(spy.probes()) == 0 {
		t.Fatal("the sweep probed nothing; the assertions above are vacuous")
	}
	t.Logf("goroutines: %d before, %d after; %d emitter(s) opened, %d closed, %d probes",
		before, runtime.NumGoroutine(), spy.opens, spy.closes, len(spy.probes()))
}

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

// oneSweep returns a setup producing one complete sweep against an emitter that
// RETAINS NOTHING.
//
// The retention matters and it is not incidental. spyEmitter keeps every probed
// address so the scope tests can assert on them; used here it would report the
// TEST'S OWN storage as the capability's footprint. health's falsifier file was
// bitten by exactly this (a spy sender retaining a message log read as 53 KB of
// engine growth), and backup's records the same.
func oneSweep(t *testing.T) func() func() {
	t.Helper()
	raw := paramsJSON(t, []string{"192.0.2.0/24"}, map[string]any{"max_targets": 64})
	return func() func() {
		svc := sweepWith(t, Options{
			OpenEmitter: func(context.Context) (Emitter, error) { return discardEmitter{}, nil },
			Logger:      quiet(),
		})
		ctx := context.Background()
		return func() {
			doc, err := svc.run(ctx, raw)
			if err != nil {
				panic(err)
			}
			if doc == nil {
				panic("the measured sweep produced no document")
			}
		}
	}
}

// discardEmitter accepts every probe and keeps none.
type discardEmitter struct{}

func (discardEmitter) Send(netip.Addr) error { return nil }

func (discardEmitter) Responded() map[netip.Addr]bool { return nil }

func (discardEmitter) Close() error { return nil }
