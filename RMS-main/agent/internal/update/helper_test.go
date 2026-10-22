package update

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

type helperRig struct {
	t        *testing.T
	l        Layout
	target   string
	clock    *fakeClock
	svc      *fakeServices
	newSHA   string
	oldSHA   string
	onSleep  func(n int)
	sleeps   int
	failTest map[string]bool // version -> self-test fails
}

func newHelperRig(t *testing.T) *helperRig {
	r := &helperRig{
		t:        t,
		l:        NewLayout(t.TempDir()),
		clock:    newFakeClock(),
		svc:      &fakeServices{},
		failTest: map[string]bool{},
	}
	r.target = filepath.Join(t.TempDir(), "bin", BinaryName())
	r.oldSHA = writeFile(t, r.target, "old binary")
	writeFile(t, r.l.PrevBinary(), "old binary")
	r.newSHA = writeFile(t, r.l.StagedBinary(), "new binary")
	mustWriteState(t, r.l, &State{
		Phase: PhaseStaged, JobID: jobA, FromVersion: oldV, ToVersion: newV,
		SHA256: r.newSHA, PrevSHA256: r.oldSHA, TargetPath: r.target, HealthWindowS: 600,
	})
	return r
}

func (r *helperRig) run(phase string) int {
	return RunHelper(r.t.Context(), HelperOptions{
		StatePath: r.l.StatePath(),
		Phase:     phase,
		Services:  r.svc,
		Clock:     r.clock,
		Sleep: func(ctx context.Context, d time.Duration) error {
			r.sleeps++
			if r.sleeps > 1000 {
				return errors.New("the helper never stopped polling")
			}
			r.clock.Advance(d)
			if r.onSleep != nil {
				r.onSleep(r.sleeps)
			}
			return nil
		},
		SelfTest: func(_ context.Context, _ string, want string) error {
			if r.failTest[want] {
				return errors.New("exit 1")
			}
			return nil
		},
		Logger: quietLog(),
	})
}

func (r *helperRig) targetHolds() string { return readFile(r.t, r.target) }

func TestApplySwapsRestartsAndStopsWhenConfirmed(t *testing.T) {
	r := newHelperRig(t)
	r.onSleep = func(n int) {
		if n == 2 {
			st := mustReadState(t, r.l)
			if st.Phase != PhaseTrial || st.TrialDeadline == nil {
				t.Fatalf("helper did not enter trial before restarting: %+v", st)
			}
			if _, err := TryDecide(r.l.DecisionLock(), DecideConfirm); err != nil {
				t.Fatal(err)
			}
			st.Phase, st.Reported = PhaseConfirmed, true
			mustWriteState(t, r.l, st)
		}
	}
	if code := r.run(HelperApply); code != 0 {
		t.Fatalf("exit %d", code)
	}
	if r.targetHolds() != "new binary" {
		t.Fatal("target not swapped")
	}
	if !reflect.DeepEqual(r.svc.Calls(), []string{"restart", "helper-exiting"}) {
		t.Fatalf("calls = %v", r.svc.Calls())
	}
	if _, err := os.Stat(r.l.StagedDir()); !os.IsNotExist(err) {
		t.Fatal("staged/ kept after confirmation")
	}
	if readFile(t, r.l.PrevBinary()) != "old binary" {
		t.Fatal("prev/ lost")
	}
}

func TestApplyWritesTrialBeforeRestarting(t *testing.T) {
	r := newHelperRig(t)
	var phaseAtRestart Phase
	r.svc.onRestart = func() {
		if phaseAtRestart == "" {
			phaseAtRestart = mustReadState(t, r.l).Phase
		}
	}
	r.onSleep = func(int) {
		st := mustReadState(t, r.l)
		st.Phase = PhaseConfirmed
		mustWriteState(t, r.l, st)
	}
	r.run(HelperApply)
	if phaseAtRestart != PhaseTrial {
		t.Fatalf("phase at restart = %q; the new binary would not know it is on trial", phaseAtRestart)
	}
}

func TestApplyWithAFailingSelfTestAbortsBeforeTheSwap(t *testing.T) {
	r := newHelperRig(t)
	r.failTest[newV] = true
	r.run(HelperApply)
	st := mustReadState(t, r.l)
	if st.Phase != PhaseAborted || st.Reason != ReasonSelftestFailed || st.Reported {
		t.Fatalf("state = %+v", st)
	}
	if r.targetHolds() != "old binary" {
		t.Fatal("target changed")
	}
	// Restarted anyway so the old binary reports.
	if !reflect.DeepEqual(r.svc.Calls(), []string{"restart", "helper-exiting"}) {
		t.Fatalf("calls = %v", r.svc.Calls())
	}
}

func TestApplyOnWindowsStopsOnlyAfterTheCandidatePassed(t *testing.T) {
	r := newHelperRig(t)
	r.svc.stopForSwap = true
	r.failTest[newV] = true
	r.run(HelperApply)
	for _, c := range r.svc.Calls() {
		if c == "stop" {
			t.Fatal("stopped the service for a candidate that failed its self-test")
		}
	}

	r = newHelperRig(t)
	r.svc.stopForSwap = true
	r.onSleep = func(int) {
		st := mustReadState(t, r.l)
		st.Phase = PhaseConfirmed
		mustWriteState(t, r.l, st)
	}
	r.run(HelperApply)
	if !reflect.DeepEqual(r.svc.Calls(), []string{"stop", "start", "helper-exiting"}) {
		t.Fatalf("calls = %v", r.svc.Calls())
	}
}

func TestApplyRollsBackWhenNothingConfirms(t *testing.T) {
	r := newHelperRig(t)
	r.run(HelperApply)
	st := mustReadState(t, r.l)
	if st.Phase != PhaseRolledBack || st.Reason != ReasonNoConfirmation || st.Reported {
		t.Fatalf("state = %+v", st)
	}
	if r.targetHolds() != "old binary" {
		t.Fatal("rollback did not restore the old binary")
	}
	if ReadDecision(r.l.DecisionLock()) != DecideRollback {
		t.Fatal("the helper rolled back without the decision lock")
	}
	if !reflect.DeepEqual(r.svc.Calls(), []string{"restart", "restart", "helper-exiting"}) {
		t.Fatalf("calls = %v", r.svc.Calls())
	}
}

func TestApplyRollsBackACrashLoop(t *testing.T) {
	r := newHelperRig(t)
	r.onSleep = func(n int) {
		if n == 1 {
			st := mustReadState(t, r.l)
			st.Starts = MaxStarts + 1
			mustWriteState(t, r.l, st)
		}
	}
	r.run(HelperApply)
	if st := mustReadState(t, r.l); st.Phase != PhaseRolledBack || st.Reason != ReasonCrashLoop {
		t.Fatalf("state = %+v", st)
	}
}

func TestApplyCarriesOutARollbackTheNewBinaryDecided(t *testing.T) {
	r := newHelperRig(t)
	r.onSleep = func(n int) {
		if n == 1 {
			if _, err := TryDecide(r.l.DecisionLock(), DecideRollback); err != nil {
				t.Fatal(err)
			}
			st := mustReadState(t, r.l)
			st.Phase, st.Reason = PhaseRollingBack, ReasonNoConfirmation
			mustWriteState(t, r.l, st)
		}
	}
	r.run(HelperApply)
	if st := mustReadState(t, r.l); st.Phase != PhaseRolledBack {
		t.Fatalf("state = %+v", st)
	}
	if r.targetHolds() != "old binary" {
		t.Fatal("not rolled back")
	}
}

func TestApplyStandsDownWhenConfirmWonTheLock(t *testing.T) {
	r := newHelperRig(t)
	r.onSleep = func(n int) {
		if n == 1 {
			if _, err := TryDecide(r.l.DecisionLock(), DecideConfirm); err != nil {
				t.Fatal(err)
			}
		}
		if n == 200 {
			st := mustReadState(t, r.l)
			st.Phase = PhaseConfirmed
			mustWriteState(t, r.l, st)
		}
	}
	r.run(HelperApply)
	if r.targetHolds() != "new binary" {
		t.Fatal("the helper rolled back an update the new binary had confirmed")
	}
}

func TestRollbackHelperRestoresThePreviousBinary(t *testing.T) {
	r := newHelperRig(t)
	writeFile(t, r.target, "new binary")
	st := mustReadState(t, r.l)
	st.Phase, st.Reason = PhaseRollingBack, ReasonNoConfirmation
	mustWriteState(t, r.l, st)
	if code := r.run(HelperRollback); code != 0 {
		t.Fatalf("exit %d", code)
	}
	if r.targetHolds() != "old binary" {
		t.Fatal("not restored")
	}
	if got := mustReadState(t, r.l); got.Phase != PhaseRolledBack || got.Reason != ReasonNoConfirmation {
		t.Fatalf("state = %+v", got)
	}
}

func TestAFailedRollbackIsRecordedAndTheServiceRestartedAnyway(t *testing.T) {
	r := newHelperRig(t)
	writeFile(t, r.target, "new binary")
	writeFile(t, r.l.PrevBinary(), "corrupted previous binary")
	st := mustReadState(t, r.l)
	st.Phase, st.Reason = PhaseRollingBack, ReasonNoConfirmation
	mustWriteState(t, r.l, st)
	if code := r.run(HelperRollback); code == 0 {
		t.Fatal("a failed rollback exited 0")
	}
	if got := mustReadState(t, r.l); got.Phase != PhaseRollbackFailed || got.Reason != ReasonRollbackFailed {
		t.Fatalf("state = %+v", got)
	}
	if r.targetHolds() != "new binary" {
		t.Fatal("target changed by a failed rollback")
	}
	calls := r.svc.Calls()
	if len(calls) < 1 || calls[0] != "restart" {
		t.Fatalf("calls = %v", calls)
	}
}

func TestOnlyOneHelperRunsAtATime(t *testing.T) {
	r := newHelperRig(t)
	release, err := AcquireHelperLock(r.l.HelperLock())
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	if code := r.run(HelperApply); code == 0 {
		t.Fatal("a second helper ran")
	}
	if mustReadState(t, r.l).Phase != PhaseStaged {
		t.Fatal("the second helper touched the state")
	}
}
