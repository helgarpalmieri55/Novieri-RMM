//go:build windows

package health

import (
	"testing"
	"unsafe"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// THESE TESTS NOW RUN, on the agent CI job's windows-latest runner.
//
// Until that runner was added, this comment read "THESE TESTS COMPILE AND
// HAVE NEVER RUN" -- true at the time, when `GOOS=windows go vet ./...` and
// `GOOS=windows go test -c` on this project's Linux CI only type-checked and
// linked them. Left here rather than deleted, because the distinction it was
// drawing still matters: CLAUDE.md's complaint about this module's original
// six falsifiers was never that they were unrun, it was that they were unrun
// WHILE BEING CITED AS VERIFICATION. That is also why this file's execution
// on a real Windows runner still is not the whole claim -- it is a
// GitHub-hosted VM, not the fleet's actual Windows 11 hardware, so treat this
// as real execution of real code against a real Windows kernel and WMI/CPU
// counters, not as equivalent to the first-run verification on an actual
// endpoint that docs/modules/agent-distribution/first-run-verification.md
// still asks for.

func TestNewMemoryStatusExSetsItsOwnLength(t *testing.T) {
	// GlobalMemoryStatusEx fails unless dwLength is the struct size. Confining
	// that to one constructor is the mitigation; this is the check on the
	// constructor.
	m := newMemoryStatusEx()
	if got, want := m.Length, uint32(unsafe.Sizeof(m)); got != want {
		t.Fatalf("newMemoryStatusEx().Length = %d, want %d", got, want)
	}
}

func TestWindowsFalsifier_TheRealSamplerAccumulatesNothing(t *testing.T) {
	// Retained heap must stay flat across a hundredfold spread in tick count --
	// the property that decides whether a device is still healthy after a week.
	//
	// FALSIFYING OBSERVATION: retained bytes scaling with the tick count.
	//
	// WHAT THIS IS, PRECISELY, because it used to call itself "the Windows twin
	// of falsifier 2c" and that was misleading in a way worth spelling out.
	// `budget_test.go` carries NO build tag, so
	// TestFalsifier2_TheRealHostSamplerAccumulatesNothingEither compiles and
	// runs on Windows too, driving NewHostSampler() -- the real Windows sampler
	// -- through the tick ENGINE with four checks. That test is its own twin on
	// every platform. This one is not a twin of it and does not need to be.
	//
	// What this adds is the sampler driven DIRECTLY, bypassing the engine, so a
	// leak inside a sampler call cannot hide behind engine-level buffer reuse.
	// That earns its place only if it drives the paths the engine test does not,
	// which until 2026-08-14 it did not: it drove cpu and memory alone, two
	// checks whose Windows implementations are a pair of fixed-size struct reads
	// and the least likely things in this file to accumulate anything.
	//
	// It now drives ALL FIVE, and the two additions are the point:
	//   - `process.present` takes a Toolhelp SNAPSHOT HANDLE per call. That is
	//     by far the most likely accumulation source in the Windows sampler --
	//     a missed CloseHandle is invisible to Go's heap accounting but the
	//     walk itself allocates -- and nothing anywhere tested it.
	//   - `disk.free_pct` with the `*` wildcard walks GetLogicalDriveStrings and
	//     builds a []string plus a string per drive on every tick
	//     (splitDriveStrings is documented as allocating by nature), so it is
	//     the one check expected to allocate per tick. What must hold is that
	//     it does not RETAIN.
	const few, many = 100, 10_000
	tick := func() func() {
		s := NewHostSampler()
		return func() {
			_ = s.Sample("cpu.busy_pct", "")
			_ = s.Sample("memory.used_pct", "")
			_ = s.Sample("disk.free_pct", "*")
			_ = s.Sample("disk.free_bytes", "C:\\")
			// A name chosen NOT to match, deliberately: a match returns on the
			// first hit, so a matching name would walk a fraction of the
			// snapshot. This walks every process every tick, which is the worst
			// case and the one worth measuring.
			_ = s.Sample("process.present", "a-process-that-does-not-exist")
		}
	}
	small := budget.Retained(few, tick)
	large := budget.Retained(many, tick)
	const allowance = 32 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("windows sampler: %d ticks retained %d bytes more than %d (allowance %d)",
			many, delta, few, allowance)
	}
	t.Logf("windows sampler: %d ticks = %d bytes, %d ticks = %d bytes, delta = %d",
		few, small, many, large, large-small)
}
