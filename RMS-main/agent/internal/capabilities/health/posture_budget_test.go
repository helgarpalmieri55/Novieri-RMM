package health

// The posture probes' own idle-budget falsifiers.
//
// §0.5 falsifier 3 drives a STAND-IN sampler: it measures that the engine calls
// the posture object only on the sub-tick, which is a property of engine.go,
// not of these probes. These five measure the probes themselves, and they exist
// because the probes SPAWN PROCESSES -- a cost none of the original six covers.
//
// A and B are ordinary Tests calling testing.Benchmark, not `go test -bench`,
// for the reason internal/budget records: -bench exits 0 on a regression. C, D
// and E are plain tests, because what they measure is elapsed time and command
// count rather than allocation.
//
// EVERY WAIT HERE IS BOUNDED, deliberately. Each of these tests exists to catch
// a regression that makes something take forever; a test that WAITS forever for
// it reports nothing, blocks CI on a timeout with no message, and is the shape
// concurrency_harness_test.go had to be fixed for in this same range.

import (
	"context"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// benchSampler builds a sampler for the retention/allocation falsifiers below.
// It deliberately does NOT use fakeRun (see posture_test.go): fakeRun records
// every call into its calls/argv slices, and budget.Retained calls the
// returned closure n times against the SAME fake, so the fake's own
// bookkeeping grows with n. That makes a retention benchmark measure the
// test double instead of the probe under test. Measured on this machine:
//
//	NON-RECORDING runner: 1 pass = 208 B, 100 passes = 0 B,      delta = -208
//	RECORDING fake:       1 pass = 256 B, 100 passes = 29040 B,  delta = 28784
//
// So benchSampler uses a plain closure that answers from a fixed table by
// binary name and records nothing. Do not change this to fakeRun -- that
// would silently turn falsifier A back into a test of the double.
func benchSampler() Sampler {
	run := func(_ context.Context, name string, _ ...string) postureResult {
		switch name {
		case "findmnt":
			return postureResult{Stdout: "/dev/mapper/vg0-root\n"}
		case "lsblk":
			return postureResult{Stdout: "vg0-root lvm\nluks-abc crypt\nvda disk\n"}
		default:
			return postureResult{NotFound: true, Err: errNotInstalled}
		}
	}
	return newPostureSampler(run, map[string]probeFunc{
		"posture.disk_encryption": probeDiskEncryptionLinux,
	})
}

// Falsifier A: a posture pass must retain nothing.
func TestPostureFalsifierA_APassRetainsNothingAcrossAHundredOfThem(t *testing.T) {
	// A pass whose budget expired returns `unavailable` WITHOUT running the
	// probe (posture.go), and a benchmark of that measures nothing at all. The
	// budget is 20s and testing.Benchmark's default benchtime is 1s, so it
	// cannot happen today -- this counter is what makes that an observation
	// rather than an assumption, and it turns `-benchtime=30s` into a failure
	// with a reason instead of a suspiciously good number.
	skipped := 0
	sample := func(s Sampler) {
		if !s.Sample("posture.disk_encryption", "").Available {
			skipped++
		}
	}
	one := budget.Retained(1, func() func() {
		s := benchSampler()
		return func() { sample(s) }
	})
	hundred := budget.Retained(100, func() func() {
		s := benchSampler()
		return func() { sample(s) }
	})
	if skipped > 0 {
		t.Fatalf("posture falsifier A: %d of the measured passes answered `unavailable` and so ran no probe. "+
			"This measured the short-circuit, not the pass.", skipped)
	}
	const limit = 4 << 10
	if d := hundred - one; d > limit {
		t.Errorf("posture falsifier A: 1 pass retained %d bytes, 100 retained %d — delta %d exceeds %d. "+
			"§4.3: between ticks, nothing.", one, hundred, d, limit)
	}
	t.Logf("posture falsifier A: retained 1 pass = %d bytes, 100 passes = %d bytes, delta = %d",
		one, hundred, hundred-one)
}

// Falsifier B: no goroutine may outlive a pass.
//
// This one drives the REAL runner and spawns a real process per iteration,
// because a pass has exactly ONE source of goroutines and a fake runner never
// reaches it: exec.Cmd starts an internal io.Copy goroutine per stream whenever
// Stdout/Stderr is not an *os.File, and postureBoundedBuffer is not one. Driven
// through benchSampler this test asserted a property true by construction --
// the same defect falsifier A was rewritten to escape.
func TestPostureFalsifierB_APassLeavesNoGoroutineBehind(t *testing.T) {
	if _, err := os.Stat("/bin/sh"); err != nil {
		t.Skip("SKIPPING: /bin/sh is absent, so this run would spawn nothing and measure no goroutine.")
	}
	skipped := 0
	// The cheapest possible real command: a shell that execs nothing and
	// exits. What is being measured is the spawn machinery, not the tool.
	spawn := func(ctx context.Context, run postureRunner, _ string) Sample {
		if run(ctx, "/bin/sh", "-c", "exit 0").ok() {
			return boolSample(true)
		}
		return unavailable()
	}
	r := budget.Measure("posture-pass", func() func() {
		s := newPostureSampler(execPostureRunner, map[string]probeFunc{"posture.disk_encryption": spawn})
		return func() {
			if !s.Sample("posture.disk_encryption", "").Available {
				skipped++
			}
		}
	})
	if skipped > 0 {
		t.Fatalf("posture falsifier B: %d of %d passes did not complete a spawn (`unavailable`), "+
			"so the goroutine count they contributed is meaningless.", skipped, r.N)
	}
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("posture falsifier B: %d allocs/op, %d B/op, goroutine delta %d over %d real spawns",
		r.AllocsPerOp, r.BytesPerOp, r.Goroutines, r.N)
}

// Falsifier C: a hung command cannot hold the pass open.
//
// This one uses the REAL runner, because a fake cannot spawn anything and the
// whole claim is about processes. It runs a command that outlives its own
// deadline and asserts the call returns promptly anyway -- the WaitDelay
// discipline asserted rather than assumed. It bites: patch/exec.go measured
// 30.003s for this exact shape before WaitDelay existed, against the 10s limit
// below.
//
// # What this test DELETED, and why
//
// It used to also count `/proc/self/task/*/children` before and after and
// assert no child survived. That assertion could not fail, in either direction:
//
//   - the DIRECT child (`sh`) is killed by the context and then reaped by
//     cmd.Wait, so it is gone from the children list by the time the call
//     returns; and
//   - the GRANDCHILD (`sleep 60`) never appears in that list at all. It is a
//     child of `sh`, and when `sh` dies it reparents to init -- which is
//     precisely the case posture.go:74-79 documents, and precisely why
//     WaitDelay is needed.
//
// So `after - before` was structurally 0. Worse, the name was a claim this
// package cannot honour: a grandchild DOES outlive the pass, by design. What
// bounds the agent is that the pass stops WAITING for it, and that is what the
// elapsed-time assertion below measures.
func TestPostureFalsifierC_AHungCommandCannotHoldThePassOpen(t *testing.T) {
	if _, err := os.Stat("/bin/sh"); err != nil {
		t.Skip("SKIPPING the only real-process proof: /bin/sh is absent, so nothing here spawned a process.")
	}
	start := time.Now()
	// A grandchild that holds the stdout pipe open is exactly the shape
	// patch/exec.go measured at 30.003s before WaitDelay existed.
	res := execPostureRunner(context.Background(), "/bin/sh", "-c", "sleep 60 & wait")
	elapsed := time.Since(start)

	if max := postureDeadline + postureWaitDelay + 3*time.Second; elapsed > max {
		t.Errorf("posture falsifier C: a hung probe took %s, want under %s. cmd.WaitDelay is what bounds it.",
			elapsed, max)
	}
	if res.Err == nil {
		t.Errorf("a command that outlives its deadline returned success: %+v", res)
	}
	t.Logf("posture falsifier C: a 60s command under a %s deadline returned in %s",
		postureDeadline, elapsed)
}

// Falsifier E: a whole pass is bounded, even when every command in it wedges.
//
// This is design §9's "A probe cannot hang the tick", which had no test. It is
// the one that matters most, because the engine holds `e.mu` across the whole
// sample loop and internal/scheduler invokes wheel callbacks SYNCHRONOUSLY:
// a pass that runs long does not slow the health module down, it stops EVERY
// scheduled entry on the agent and blocks `health.check.now` behind the mutex.
// Per-command deadlines do not bound that -- they multiply by the number of
// commands in the pass.
//
// The pass budget is shortened here so the test costs milliseconds; the
// mechanism under test (posturePassBudget shared by every Sample call in one
// pass) is the production one.
//
// TO SEE IT FAIL: delete the `WithDeadline` in postureSampler.Sample, or the
// short-circuit above it. Four checks then cost four wedgeCost each and every
// one answers a definite `true`. Captured in the task report.
//
// The short-circuit's SECOND half -- posture.go's "no command is run for
// them" -- is a separate claim from "no definite answer", and `definite == 0`
// alone does not measure it: deleting only the short-circuit (leaving
// WithDeadline in place) still passes an already-expired ctx to every
// remaining probe, so `run` above returns immediately via its `<-ctx.Done()`
// arm without ever reaching `wedgeCost` -- the elapsed-time and `definite`
// assertions below stay green while three probes each still called `run`
// that should never have been invoked at all. `ranCount` closes that gap
// through the same counting-seam technique falsifier D uses: with the guard
// intact exactly ONE command runs (the first, wedged check); the other three
// are skipped before `run` is ever called.
func TestPostureFalsifierE_AWedgedPassIsBoundedByThePassBudget(t *testing.T) {
	const (
		passBudget = 300 * time.Millisecond
		// What ONE command costs when nothing bounds it. It is a finite wait,
		// not a block-forever: a runner that never returns would make a
		// regressed deadline DEADLOCK this test instead of failing it, which
		// is the defect falsifier D was fixed for in this same file.
		wedgeCost = 2 * time.Second
		checks    = 4
	)
	var mu sync.Mutex
	ranCount := 0
	run := func(ctx context.Context, _ string, _ ...string) postureResult {
		mu.Lock()
		ranCount++
		mu.Unlock()
		select {
		case <-ctx.Done():
			return postureResult{ExitCode: -1, Err: ctx.Err()}
		case <-time.After(wedgeCost):
			return postureResult{Stdout: "an unbounded command eventually answered"}
		}
	}
	// A probe that answers `true` if its command completed. That makes the
	// second half of the claim observable: when the budget expires the rest of
	// the pass must report `unavailable`, NEVER a definite answer.
	wedged := func(ctx context.Context, r postureRunner, _ string) Sample {
		if r(ctx, "wedged-tool").ok() {
			return boolSample(true)
		}
		return unavailable()
	}
	probes := map[string]probeFunc{}
	types := make([]string, 0, checks)
	for i := 0; i < checks; i++ {
		ct := fmt.Sprintf("posture.wedged_%d", i)
		probes[ct] = wedged
		types = append(types, ct)
	}
	s := newPostureSampler(run, probes).(*postureSampler)
	s.passBudget = passBudget

	start := time.Now()
	definite := 0
	for _, ct := range types {
		if got := s.Sample(ct, ""); got.Available {
			definite++
		}
	}
	elapsed := time.Since(start)

	if limit := passBudget + wedgeCost/2; elapsed > limit {
		t.Errorf("posture falsifier E: a pass of %d wedged checks took %s, want under %s. "+
			"Per-command deadlines multiply; only a whole-pass budget bounds the wheel.",
			checks, elapsed, limit)
	}
	if definite != 0 {
		t.Errorf("posture falsifier E: %d of %d checks answered definitely after the pass budget expired, want 0. "+
			"An expired budget reports `unavailable`, never a verdict nothing measured.", definite, checks)
	}
	mu.Lock()
	ran := ranCount
	mu.Unlock()
	if ran != 1 {
		t.Errorf("posture falsifier E: %d of %d checks reached `run`, want exactly 1 (only the first, "+
			"wedged check). posture.go's short-circuit must stop the remaining %d before they ever call "+
			"the runner, not merely keep them from answering definitely.", ran, checks, checks-1)
	}
	t.Logf("posture falsifier E: %d wedged checks under a %s pass budget took %s, %d definite answers, %d commands run",
		checks, passBudget, elapsed, definite, ran)
}

// TestPostureOrderAWedgedCheckStarvesEverythingAfterItInPolicyOrder is not one
// of the five falsifiers above -- it is the coverage the pass budget's
// interaction with check ORDER had none of at all before this test.
//
// pol.checks is server-supplied and Engine.onTick iterates it by index, same
// order, every tick (policy.go, engine.go). So when a wedged check sits
// EARLIER in that order than a fast, healthy one, the wedged check consumes
// the entire pass budget by itself, and the fast check after it never gets a
// chance to run its command at all -- it reports `unavailable` not because it
// wedged, but because nothing was left of the budget by the time its turn
// came. Nothing here rotates the order, so this is not a one-time cost: it
// repeats on every pass for as long as the wedged check stays ahead of the
// fast one in policy. See posturePassBudget's doc comment in posture.go.
func TestPostureOrderAWedgedCheckStarvesEverythingAfterItInPolicyOrder(t *testing.T) {
	const passBudget = 300 * time.Millisecond
	fastRan := false
	run := func(ctx context.Context, name string, _ ...string) postureResult {
		if name == "fast-tool" {
			fastRan = true
			return postureResult{Stdout: "ok"}
		}
		// wedged-tool: never returns on its own, only when the pass's ctx
		// expires. That is what "wedged" means for this test.
		<-ctx.Done()
		return postureResult{ExitCode: -1, Err: ctx.Err()}
	}
	wedged := func(ctx context.Context, r postureRunner, _ string) Sample {
		if r(ctx, "wedged-tool").ok() {
			return boolSample(true)
		}
		return unavailable()
	}
	fast := func(ctx context.Context, r postureRunner, _ string) Sample {
		if r(ctx, "fast-tool").ok() {
			return boolSample(true)
		}
		return unavailable()
	}
	// Policy order: the wedged check first, the fast one second -- the exact
	// shape the corrected comment in posture.go describes.
	s := newPostureSampler(run, map[string]probeFunc{
		"posture.wedged": wedged,
		"posture.fast":   fast,
	}).(*postureSampler)
	s.passBudget = passBudget

	first := s.Sample("posture.wedged", "")
	if first.Available {
		t.Fatalf("posture.wedged = %+v, want unavailable: its own command never returns on its own", first)
	}

	second := s.Sample("posture.fast", "")
	if second.Available {
		t.Errorf("posture.fast (second in policy order, same pass) = %+v, want unavailable: the pass "+
			"budget was already spent by the wedged check ahead of it", second)
	}
	if fastRan {
		t.Errorf("posture.fast's command ran even though the pass budget was already spent by the check " +
			"ahead of it in policy order -- a starved check must not spawn anything")
	}
}

// Falsifier D: a concurrent pass must run zero commands.
func TestPostureFalsifierD_TheSecondConcurrentPassRunsZeroCommands(t *testing.T) {
	// How long this test is willing to wait for something that is supposed to
	// be immediate. Generous, because on a loaded CI runner a goroutine can be
	// slow to be scheduled; still finite, for the reason stated below.
	const patience = 5 * time.Second
	release := make(chan struct{})
	entered := make(chan struct{})
	var mu sync.Mutex
	var ran []string
	run := func(ctx context.Context, name string, args ...string) postureResult {
		mu.Lock()
		ran = append(ran, name)
		mu.Unlock()
		return postureResult{Stdout: "vg0-root lvm\n"}
	}
	blocking := func(ctx context.Context, r postureRunner, _ string) Sample {
		close(entered)
		<-release
		r(ctx, "held-probe")
		return boolSample(true)
	}
	s := newPostureSampler(run, map[string]probeFunc{"posture.disk_encryption": blocking})

	go func() { s.Sample("posture.disk_encryption", "") }()
	select {
	case <-entered:
	case <-time.After(patience):
		close(release)
		t.Fatalf("the first pass never entered its probe within %s", patience)
	}

	mu.Lock()
	baseline := len(ran)
	mu.Unlock()

	// EVERY wait in this test is bounded. The guard is a TryLock, so a losing
	// caller returns immediately; were it to regress to a plain Lock, these
	// calls would block until the held pass finished -- which is never, here.
	// An unbounded wait would then DEADLOCK the test rather than fail it, and
	// a test that hangs on regression reports nothing at all. That is the shape
	// concurrency_harness_test.go was fixed for in this same range.
	got := make(chan Sample, 5)
	go func() {
		for i := 0; i < 5; i++ {
			got <- s.Sample("posture.disk_encryption", "")
		}
	}()
	for i := 0; i < 5; i++ {
		select {
		case sample := <-got:
			if sample.Available {
				t.Errorf("concurrent pass %d = %+v, want unavailable", i, sample)
			}
		case <-time.After(patience):
			close(release)
			t.Fatalf("posture falsifier D: concurrent pass %d did not return within %s. "+
				"The guard is TryLock, not Lock: a caller that finds a pass in flight is answered "+
				"`unavailable` NOW, never queued behind a second set of shell-outs.", i, patience)
		}
	}
	mu.Lock()
	after := len(ran)
	mu.Unlock()
	if after != baseline {
		t.Errorf("posture falsifier D: five concurrent passes ran %d commands, want 0. "+
			"A health.check.now landing on a posture sub-tick must not stack shell-outs.", after-baseline)
	}
	close(release)
	t.Logf("posture falsifier D: 5 concurrent passes ran %d commands", after-baseline)
}
