package pushinstall

import (
	"context"
	"runtime"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// THE PUSH-INSTALL IDLE BUDGET, MEASURED (spec §11).
//
// Every number below is produced through internal/budget's harness from an
// ORDINARY Test and COMPARED, never from a Benchmark function -- `go test -bench`
// exits 0 on a regression, measured on this project. `~0% CPU at idle` stays a
// design target for the reason internal/budget's package comment gives: a shared
// runner's variance exceeds the signal.
//
// The security falsifiers 3, 4 and 6 (credential zeroed, target from signed
// params only, digest verified before transfer) are in pushinstall_test.go --
// they measure the same code paths and are tested alongside, per spec §11.

// Falsifier 1: an idle agent with this capability compiled in holds zero
// goroutines and allocates zero bytes for it. The only idle-path code is
// constructing the Service (which Process is) and the registry lookup; neither
// may allocate on an agent doing nothing.
//
// FALSIFYING OBSERVATION: AllocsPerOp > 0, or a goroutine delta.
func TestFalsifier1_AnIdleAgentHoldsNothingForThisCapability(t *testing.T) {
	r := budget.AssertNoAllocations(t, "agent.push dormant", func() func() {
		s := New(Options{})
		return func() {
			if s.Runs() != 0 {
				panic("the dormancy measurement ran a job")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("falsifier 1: %d iterations, %d allocs/op, %d B/op, goroutine delta %d",
		r.N, r.AllocsPerOp, r.BytesPerOp, r.Goroutines)
}

// Falsifier 2: no socket, file handle or transport client exists between jobs.
// A completed install with a fake transport must leave no goroutine behind --
// the real transport dials inside the job and closes in a defer, so a leaked
// goroutine here would be a session that outlived its job.
//
// FALSIFYING OBSERVATION: a goroutine delta across a completed job.
func TestFalsifier2_AJobLeavesNoGoroutineBehind(t *testing.T) {
	sess := &fakeSession{unameOut: "Linux x86_64"}
	s := New(Options{Dialer: &fakeDialer{session: sess}, Downloader: &fakeDownloader{data: artefactBytes()}})
	raw := installParamsJSON(t, nil)

	runtime.GC()
	before := runtime.NumGoroutine()
	if _, err := s.install(context.Background(), raw); err != nil {
		t.Fatal(err)
	}
	runtime.GC()
	runtime.Gosched()
	if delta := runtime.NumGoroutine() - before; delta > 0 {
		t.Fatalf("one install left %d extra goroutine(s)", delta)
	}
	if !sess.closed {
		t.Fatal("the session was not closed; a real transport would leak a socket")
	}
	t.Logf("goroutines: %d before, %d after; session closed=%v",
		before, runtime.NumGoroutine(), sess.closed)
}

// Falsifier 2W (Windows push, plan Task 7): the WinRM transport holds nothing
// between jobs either. A completed winrm install with a fake WinRM dialer
// leaves no goroutine behind, and a Service whose winrm seam is unset resolves
// the default dialer only when a winrm job runs -- constructing one (falsifier
// 1 above) allocates nothing, and the default dialer is a zero-size value.
//
// FALSIFYING OBSERVATION: a goroutine delta across a completed winrm job, or a
// default dialer that carries state.
func TestFalsifier2W_AWinRMJobLeavesNoGoroutineBehind(t *testing.T) {
	w := &fakeWinRMDialer{stdout: okStdout}
	s := New(Options{WinRMDialer: w, Downloader: &fakeDownloader{data: artefactBytes()}})
	raw := winrmInstallParamsJSON(t, nil)

	runtime.GC()
	before := runtime.NumGoroutine()
	out, err := s.install(context.Background(), raw)
	if err != nil {
		t.Fatal(err)
	}
	if out.(installResult).Outcome != outcomeInstalled {
		t.Fatalf("result = %+v", out)
	}
	runtime.GC()
	runtime.Gosched()
	if delta := runtime.NumGoroutine() - before; delta > 0 {
		t.Fatalf("one winrm install left %d extra goroutine(s)", delta)
	}
	if !w.runCalled {
		t.Fatal("the winrm dialer never ran; the measurement is vacuous")
	}
	if _, ok := New(Options{}).winrmDialer().(defaultWinRMDialer); !ok {
		t.Fatal("the unset seam does not resolve to the stateless default dialer")
	}
}

// Falsifier 5: peak allocation is bounded by the ARTEFACT size, not by the batch
// or the segment size -- one job, one target, one artefact. The batch lives on
// the SERVER (N jobs); the agent handles one job and holds one artefact in
// memory. This measures that install's allocation tracks the artefact and not
// some multiple of it.
//
// FALSIFYING OBSERVATION: per-install bytes materially exceeding the artefact
// (a copy per hop, the artefact retained after the job).
func TestFalsifier5_PeakAllocationTracksTheArtefactNotABatch(t *testing.T) {
	const artefactSize = 1 << 20 // 1 MiB
	big := make([]byte, artefactSize)
	for i := range big {
		big[i] = byte(i)
	}
	sumParams := installParamsJSON(t, func(m map[string]any) {
		rel := m["release"].(map[string]any)
		rel["sha256"] = hexSum(big)
	})

	// A fresh downloader per op so the artefact bytes are the op's cost, not a
	// fixture the test retains.
	r := budget.Fixed("agent.push-install-1MiB", 1, func() func() {
		return func() {
			sess := &fakeSession{unameOut: "Linux x86_64"}
			s := New(Options{Dialer: &fakeDialer{session: sess}, Downloader: &fakeDownloader{data: append([]byte(nil), big...)}})
			if _, err := s.install(context.Background(), sumParams); err != nil {
				panic(err)
			}
		}
	})

	// A generous multiple of the artefact: the download buffer, the SCP framing
	// copy, and the params. NOT a multiple of any batch count -- there is no
	// batch here. Clean sits well under this; a per-target copy of the artefact
	// or a retained artefact would blow past it.
	const ceiling = 8 * artefactSize
	if r.BytesPerOp > ceiling {
		t.Fatalf("one 1 MiB install allocated %d bytes (%.1fx the artefact, ceiling %dx); "+
			"peak must track the artefact, not a batch", r.BytesPerOp,
			float64(r.BytesPerOp)/float64(artefactSize), ceiling/artefactSize)
	}
	t.Logf("falsifier 5: one %d-byte install allocated %d bytes (%.2fx the artefact)",
		artefactSize, r.BytesPerOp, float64(r.BytesPerOp)/float64(artefactSize))
}
