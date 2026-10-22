package inventory

import (
	"context"
	"io"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// THE `inventory.collect` IDLE BUDGET, MEASURED.
//
// # This file did not exist, and that is the finding
//
// CLAUDE.md requires every capability's footprint to be "measured by a
// benchmark that runs, or the number is not claimed", and records why: the
// `health.check` falsifiers "sat unrun for an entire phase" because the code
// lived where CI could not reach it. inventory and netsweep are the two
// capabilities that never got a budget file at all — not a weak one, not a
// decorative one, none — so there was no number to be wrong. Nothing measured
// this capability's footprint before this file.
//
// # Why none of this is a Benchmark function
//
// `go test -bench` EXITS 0 ON A REGRESSION. Measured on this project, not
// assumed: internal/budget's package comment records a planted 64 KiB/op
// regression printing `65536 B/op` and still exiting 0. So every number below
// is produced by `testing.Benchmark` from an ORDINARY Test and COMPARED.
//
// # What is NOT asserted: wall-clock CPU
//
// Same refusal as internal/budget's package comment: a shared runner's variance
// exceeds the signal, and a flaky budget test gets skipped — which reads as
// coverage while measuring nothing. `~0% CPU at idle` stays a design target.
//
// # The falsifiers this capability owes
//
//  1. Anything resident on an agent with no inventory job in flight.
//  2. Memory that scales with the OUTPUT OF THE TOOLS IT SHELLS OUT TO — the
//     one input neither the operator nor this agent controls. This is the one
//     that matters, and it is the one that was broken.
//  3. Memory that scales with the size of a package database it READS (the
//     dpkg status file) rather than with the entries it keeps.

func TestFalsifier1_AnIdleAgentHoldsNothingForThisCapability(t *testing.T) {
	// The only code path that runs while the agent is idle and this capability
	// is compiled in is the registry lookup per inbound frame plus reading the
	// run counter. Neither may allocate.
	//
	// FALSIFYING OBSERVATION: AllocsPerOp > 0, or a goroutine delta.
	r := budget.AssertNoAllocations(t, "inventory dormant", func() func() {
		return func() {
			if Runs() < 0 {
				panic("impossible run count")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("falsifier 1: %d iterations, %d allocs/op, %d B/op, goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)
}

func TestFalsifier2_AToolThatWritesForeverCostsABoundedBufferAndNotTheAgentsBudget(t *testing.T) {
	// THE ASSERTION THIS CAPABILITY MOST NEEDED, AND THE ONE THAT FOUND A REAL
	// DEFECT RATHER THAN A DECORATIVE GATE.
	//
	// exec.go's maxCommandOutput carries this comment: "A hostile or broken tool
	// that writes forever costs a bounded buffer rather than the agent's memory
	// budget." That claim was false when it was written, and the only test on it
	// — TestRunCapsWhatOneToolMayContribute — could not have caught it, because
	// it asserts on the LENGTH OF THE RETURNED STRING and nothing else. The
	// returned string was capped. The buffer that produced it was not: `run` set
	// cmd.Stdout to a plain bytes.Buffer, let the tool write as much as it
	// liked, and truncated AFTERWARDS.
	//
	// Measured before the fix, through this exact rig: a tool emitting 1 MiB
	// cost 5,283,880 bytes of allocation, and a tool emitting 64 MiB cost
	// 335,577,488. The cost tracked the tool's output, on a process whose whole
	// budget is "low-tens-of-MB", for output that is discarded past the first
	// megabyte anyway.
	//
	// This is the shape of assertion the rest of this sweep kept finding
	// missing, and the reason it is written as a CUMULATIVE ALLOCATION gate
	// rather than an RSS or retained-heap one: the buffer is freed when `run`
	// returns, so an after-the-fact reading of resident memory cannot see it.
	// backup's falsifier 2 records the same finding with a number (-131,072
	// bytes of RSS delta for a planted io.ReadAll, and a passing test).
	//
	// FALSIFYING OBSERVATION: allocation scaling with the tool's output. A
	// bounded implementation costs the same for a 64 MiB tool as for a 1 MiB
	// one, because it stops copying at the cap either way.
	requireCommand(t, "sh")

	const small = 1 << 20
	const large = 64 << 20

	rSmall := budget.Fixed("inventory.run-1MiB-tool", 1, emitting(small))
	rLarge := budget.Fixed("inventory.run-64MiB-tool", 1, emitting(large))

	// The allowance is 4 MiB: comfortably above the ~2 MiB a bounded
	// implementation legitimately touches (one maxCommandOutput buffer plus the
	// string copy of it), and two orders of magnitude below the 330 MB the
	// unbounded one cost.
	const allowance = 4 << 20
	if delta := rLarge.BytesPerOp - rSmall.BytesPerOp; delta > allowance {
		t.Fatalf("a tool that emitted %d bytes cost %d bytes more allocation than one that "+
			"emitted %d (allowance %d). maxCommandOutput is %d, so everything past that is "+
			"discarded — collecting it first means a broken tool sets this agent's memory "+
			"footprint", large, delta, small, allowance, maxCommandOutput)
	}

	// AND AN ABSOLUTE BOUND, because a ratio alone would be satisfied by an
	// implementation that buffered 64 MiB for BOTH.
	if rLarge.BytesPerOp > int64(large) {
		t.Fatalf("running a tool that emitted %d bytes allocated %d bytes in total; a bounded "+
			"reader stops at maxCommandOutput (%d) and never allocates as much as the tool wrote",
			large, rLarge.BytesPerOp, maxCommandOutput)
	}

	// RSS is LOGGED AND NOT GATED. It is read after `run` returns, which is
	// precisely the blind spot this assertion exists to close; gating on it
	// would re-create the hole. The allocation figures above are the
	// deterministic version of the same question.
	t.Logf("falsifier 2: a 1 MiB tool allocated %d bytes (%d allocs, RSS delta %d); a 64 MiB "+
		"tool allocated %d bytes (%d allocs, RSS delta %d). Difference %d (allowance %d), "+
		"cap %d",
		rSmall.BytesPerOp, rSmall.AllocsPerOp, rSmall.RSSDelta,
		rLarge.BytesPerOp, rLarge.AllocsPerOp, rLarge.RSSDelta,
		rLarge.BytesPerOp-rSmall.BytesPerOp, allowance, maxCommandOutput)
}

func TestFalsifier2_TheReturnedStringIsStillCappedAfterTheBoundedRewrite(t *testing.T) {
	// The behavioural half, kept beside the footprint half so a future change
	// cannot buy the budget assertion by breaking the contract — returning
	// nothing at all would allocate nothing and pass everything above.
	//
	// FALSIFYING OBSERVATION: a returned length above the cap, or a tool whose
	// output is under the cap coming back truncated.
	requireCommand(t, "sh")
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	big := run(ctx, "sh", "-c", "yes 0123456789abcdef | head -c "+strconv.Itoa(4<<20))
	if len(big) != maxCommandOutput {
		t.Fatalf("a 4 MiB tool returned %d bytes; the cap is %d and the output was well over it",
			len(big), maxCommandOutput)
	}

	// And a SHORT tool is not truncated: an implementation that always returned
	// exactly maxCommandOutput bytes would pass the assertion above.
	const shortLen = 4096
	short := run(ctx, "sh", "-c", "yes 0123456789abcdef | head -c "+strconv.Itoa(shortLen))
	if len(short) != shortLen {
		t.Fatalf("a %d-byte tool returned %d bytes; output under the cap must arrive whole",
			shortLen, len(short))
	}
	t.Logf("cap: a 4 MiB tool returned %d bytes, a %d-byte tool returned %d",
		len(big), shortLen, len(short))
}

func TestOneCollectionLeaksNoGoroutine(t *testing.T) {
	// A shell-out per fact, and every one of them spawns the runtime's process
	// reaper plumbing. A missed Wait would leave a goroutine per job forever.
	//
	// FALSIFYING OBSERVATION: a goroutine delta across real collections.
	requireCommand(t, "sh")
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	runtime.GC()
	before := runtime.NumGoroutine()
	for i := 0; i < 20; i++ {
		run(ctx, "sh", "-c", "echo hello")
	}
	runtime.GC()
	runtime.Gosched()
	if delta := runtime.NumGoroutine() - before; delta > 0 {
		t.Fatalf("20 shell-outs left %d extra goroutine(s)", delta)
	}
	t.Logf("goroutines: %d before, %d after 20 shell-outs", before, runtime.NumGoroutine())
}

func TestFalsifier3_AnOverlongDpkgStatusLineCostsABoundedBuffer(t *testing.T) {
	// The dpkg status file is read as a stream. A single enormous line (a
	// corrupt database, or a pathological Description) must cost a bounded
	// buffer, not the line.
	//
	// FALSIFYING OBSERVATION: allocation that grows with the line's length.
	small := budget.Fixed("dpkg-1MiB-line", 1, dpkgWithLongLine(1<<20))
	large := budget.Fixed("dpkg-32MiB-line", 1, dpkgWithLongLine(32<<20))
	if delta := large.BytesPerOp - small.BytesPerOp; delta > 1<<20 {
		t.Fatalf("a 32 MiB line cost %d bytes more than a 1 MiB one; the reader must not "+
			"buffer whole lines", delta)
	}
	t.Logf("falsifier: dpkg status with a 1 MiB line allocated %d bytes, with a 32 MiB line %d",
		small.BytesPerOp, large.BytesPerOp)
}

func dpkgWithLongLine(n int) func() func() {
	return func() func() {
		return func() {
			r := io.MultiReader(
				strings.NewReader("Package: big\nStatus: install ok installed\nDescription: x\n "),
				io.LimitReader(repeatReader('y'), int64(n)),
				strings.NewReader("\nVersion: 1.0\n\nPackage: after\nStatus: install ok installed\nVersion: 2\n"),
			)
			got := parseDpkgStatus(r, maxSoftwareScan)
			if len(got) != 2 || got[0].Version != "1.0" || got[1].Name != "after" {
				panic("the long line was not skipped cleanly")
			}
		}
	}
}

type repeatReader byte

func (b repeatReader) Read(p []byte) (int, error) {
	for i := range p {
		p[i] = byte(b)
	}
	return len(p), nil
}

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

// emitting returns a setup that runs a tool printing exactly n bytes.
//
// `yes | head -c n` rather than a Go fixture on purpose: the subject is what
// `run` does with a CHILD PROCESS's stdout, and a fake io.Reader would measure
// a different code path from the one that shipped.
func emitting(n int) func() func() {
	return func() func() {
		ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
		_ = cancel
		script := "yes 0123456789abcdef | head -c " + strconv.Itoa(n)
		return func() {
			out := run(ctx, "sh", "-c", script)
			// Not vacuous: a tool that produced nothing would allocate nothing
			// and pass every assertion above while proving none of them.
			if len(out) == 0 {
				panic("the measured tool produced no output")
			}
		}
	}
}

// requireCommand is defined in exec_test.go; this reference keeps the
// dependency explicit for a reader of this file alone.
var _ = func() any { return exec.LookPath }
