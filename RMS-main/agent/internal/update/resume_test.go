package update

import (
	"os"
	"reflect"
	"testing"
	"time"
)

const (
	oldV = "1.0.0"
	newV = "1.1.0"
	jobA = "0192a000-0000-7000-8000-00000000000a"
)

type resumeRig struct {
	t      *testing.T
	l      Layout
	clock  *fakeClock
	svc    *fakeServices
	exits  *exitRecorder
	sender *recordingSender
}

func newResumeRig(t *testing.T) *resumeRig {
	return &resumeRig{
		t:      t,
		l:      NewLayout(t.TempDir()),
		clock:  newFakeClock(),
		svc:    &fakeServices{},
		exits:  &exitRecorder{},
		sender: &recordingSender{},
	}
}

func (r *resumeRig) resume(version string) *Session {
	s := Resume(ResumeOptions{
		Version:  version,
		DataDir:  r.l.DataDir(),
		Clock:    r.clock,
		Services: r.svc,
		Exit:     r.exits.exit,
		Logger:   quietLog(),
	})
	s.Attach(r.t.Context(), r.sender)
	return s
}

func (r *resumeRig) trialState(deadlineIn time.Duration, starts int) *State {
	d := r.clock.Now().Add(deadlineIn)
	return &State{
		Phase: PhaseTrial, JobID: jobA, FromVersion: oldV, ToVersion: newV,
		SHA256: testSHA, PrevSHA256: testSHA, TargetPath: "/usr/local/bin/its-agent",
		HealthWindowS: 600, TrialDeadline: &d, Starts: starts, Signature: SignatureVerified,
	}
}

func TestResumeWithNoStateFileDoesNothing(t *testing.T) {
	r := newResumeRig(t)
	s := r.resume(newV)
	if s != nil {
		t.Fatalf("Resume returned a session with no state file")
	}
	s.Connected()
	s.Disconnected()
	if r.clock.live() != 0 || len(r.sender.results()) != 0 || len(r.svc.Calls()) != 0 {
		t.Fatal("a nil session did something")
	}
}

func TestTrialConfirmsAfterAStableConnection(t *testing.T) {
	r := newResumeRig(t)
	mustWriteState(t, r.l, r.trialState(10*time.Minute, 0))

	s := r.resume(newV)
	if got := mustReadState(t, r.l).Starts; got != 1 {
		t.Fatalf("starts = %d, want 1", got)
	}
	if r.clock.live() != 1 {
		t.Fatalf("want exactly the deadline timer armed, have %d", r.clock.live())
	}

	s.Connected()
	r.clock.Advance(StableConnection - time.Second)
	if len(r.sender.results()) != 0 {
		t.Fatal("confirmed before the connection had lasted")
	}
	r.clock.Advance(time.Second)

	res := r.sender.results()
	if len(res) != 1 || res[0].State != "succeeded" || res[0].JobID != jobA {
		t.Fatalf("results = %+v", res)
	}
	if res[0].Result["outcome"] != OutcomeUpdated || res[0].Result["to_version"] != newV {
		t.Fatalf("result body = %v", res[0].Result)
	}
	st := mustReadState(t, r.l)
	if st.Phase != PhaseConfirmed || !st.Reported {
		t.Fatalf("state = %+v", st)
	}
	if ReadDecision(r.l.DecisionLock()) != DecideConfirm {
		t.Fatal("confirm did not take the decision lock")
	}
	if r.clock.live() != 0 {
		t.Fatal("the deadline timer survived confirmation")
	}
	// A later start does nothing.
	if again := r.resume(newV); again != nil && len(r.sender.results()) != 1 {
		t.Fatal("a confirmed update was reported twice")
	}
}

func TestADroppedConnectionDoesNotConfirm(t *testing.T) {
	r := newResumeRig(t)
	mustWriteState(t, r.l, r.trialState(10*time.Minute, 0))
	s := r.resume(newV)
	s.Connected()
	r.clock.Advance(30 * time.Second)
	s.Disconnected()
	r.clock.Advance(StableConnection)
	if len(r.sender.results()) != 0 {
		t.Fatal("a connection that dropped confirmed the update")
	}
	if mustReadState(t, r.l).Phase != PhaseTrial {
		t.Fatal("phase moved")
	}
}

func TestTrialDeadlineRollsBackThroughTheHelper(t *testing.T) {
	r := newResumeRig(t)
	mustWriteState(t, r.l, r.trialState(10*time.Minute, 0))
	r.resume(newV)

	r.clock.Advance(10 * time.Minute)

	st := mustReadState(t, r.l)
	if st.Phase != PhaseRollingBack || st.Reason != ReasonNoConfirmation {
		t.Fatalf("state = %+v", st)
	}
	if !reflect.DeepEqual(r.svc.Calls(), []string{"launch:rollback"}) {
		t.Fatalf("calls = %v", r.svc.Calls())
	}
	if !reflect.DeepEqual(r.exits.got(), []int{ExitHandover}) {
		t.Fatalf("exits = %v", r.exits.got())
	}
	if len(r.sender.results()) != 0 {
		t.Fatal("the new binary reported the rollback itself")
	}
}

func TestADeadlineThatPassedWhileDownRollsBackAtStart(t *testing.T) {
	r := newResumeRig(t)
	mustWriteState(t, r.l, r.trialState(-time.Second, 1))
	r.resume(newV)
	if st := mustReadState(t, r.l); st.Phase != PhaseRollingBack || st.Reason != ReasonNoConfirmation {
		t.Fatalf("state = %+v", st)
	}
	if !reflect.DeepEqual(r.exits.got(), []int{ExitHandover}) {
		t.Fatalf("exits = %v", r.exits.got())
	}
}

func TestACrashLoopRollsBack(t *testing.T) {
	r := newResumeRig(t)
	mustWriteState(t, r.l, r.trialState(10*time.Minute, MaxStarts))
	r.resume(newV)
	st := mustReadState(t, r.l)
	if st.Phase != PhaseRollingBack || st.Reason != ReasonCrashLoop || st.Starts != MaxStarts+1 {
		t.Fatalf("state = %+v", st)
	}
	if r.clock.live() != 0 {
		t.Fatal("a crash-looping binary armed a timer")
	}
}

func TestWithALiveHelperTheNewBinaryLeavesTheRollbackToIt(t *testing.T) {
	r := newResumeRig(t)
	mustWriteState(t, r.l, r.trialState(10*time.Minute, 0))
	release, err := AcquireHelperLock(r.l.HelperLock())
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	r.resume(newV)
	r.clock.Advance(10 * time.Minute)
	if len(r.svc.Calls()) != 0 {
		t.Fatalf("launched a second helper: %v", r.svc.Calls())
	}
	if mustReadState(t, r.l).Phase != PhaseRollingBack {
		t.Fatal("did not record the rollback decision")
	}
}

func TestConfirmLosesToARollbackAlreadyDecided(t *testing.T) {
	r := newResumeRig(t)
	mustWriteState(t, r.l, r.trialState(10*time.Minute, 0))
	s := r.resume(newV)
	if won, err := TryDecide(r.l.DecisionLock(), DecideRollback); !won || err != nil {
		t.Fatal("could not plant the helper's decision")
	}
	s.Connected()
	r.clock.Advance(StableConnection)
	if len(r.sender.results()) != 0 {
		t.Fatal("confirmed after the helper decided to roll back")
	}
	if mustReadState(t, r.l).Phase == PhaseConfirmed {
		t.Fatal("state says confirmed")
	}
}

func TestADeadlineAfterConfirmationDoesNothing(t *testing.T) {
	r := newResumeRig(t)
	mustWriteState(t, r.l, r.trialState(90*time.Second, 0))
	s := r.resume(newV)
	s.Connected()
	r.clock.Advance(StableConnection)
	r.clock.Advance(time.Hour)
	if len(r.exits.got()) != 0 || mustReadState(t, r.l).Phase != PhaseConfirmed {
		t.Fatal("a confirmed update was rolled back")
	}
}

func TestTheOldBinaryReportsARollbackOnce(t *testing.T) {
	r := newResumeRig(t)
	st := r.trialState(0, 2)
	st.Phase, st.Reason = PhaseRolledBack, ReasonNoConfirmation
	mustWriteState(t, r.l, st)

	r.resume(oldV)
	res := r.sender.results()
	if len(res) != 1 || res[0].State != "failed" {
		t.Fatalf("results = %+v", res)
	}
	body := res[0].Result
	if body["outcome"] != OutcomeRolledBack || body["reason"] != ReasonNoConfirmation ||
		body["from_version"] != oldV || body["to_version"] != newV {
		t.Fatalf("body = %v", body)
	}
	if !mustReadState(t, r.l).Reported {
		t.Fatal("not marked reported")
	}
	r.resume(oldV)
	if len(r.sender.results()) != 1 {
		t.Fatal("reported twice")
	}
}

func TestAFailedSendIsRetriedOnTheNextStart(t *testing.T) {
	r := newResumeRig(t)
	st := r.trialState(0, 0)
	st.Phase, st.Reason = PhaseAborted, ReasonSelftestFailed
	mustWriteState(t, r.l, st)
	r.sender.err = os.ErrClosed
	r.resume(oldV)
	if mustReadState(t, r.l).Reported {
		t.Fatal("marked reported although the enqueue failed")
	}
	r.sender.err = nil
	r.resume(oldV)
	if len(r.sender.results()) != 1 || !mustReadState(t, r.l).Reported {
		t.Fatal("the report was not made on the next start")
	}
}

func TestARebootBeforeTheSwapAbortsAsInterrupted(t *testing.T) {
	for _, phase := range []Phase{PhaseStaged, PhaseSwapping} {
		r := newResumeRig(t)
		st := r.trialState(0, 0)
		st.Phase, st.TrialDeadline = phase, nil
		mustWriteState(t, r.l, st)
		r.resume(oldV)
		got := mustReadState(t, r.l)
		if got.Phase != PhaseAborted || got.Reason != ReasonInterrupted || !got.Reported {
			t.Fatalf("%s: state = %+v", phase, got)
		}
		if res := r.sender.results(); len(res) != 1 || res[0].Result["outcome"] != OutcomeAborted {
			t.Fatalf("%s: results = %+v", phase, res)
		}
	}
}

func TestTheOldBinaryLeavesALiveHelperAlone(t *testing.T) {
	r := newResumeRig(t)
	st := r.trialState(0, 0)
	st.Phase, st.TrialDeadline = PhaseStaged, nil
	mustWriteState(t, r.l, st)
	release, err := AcquireHelperLock(r.l.HelperLock())
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	r.resume(oldV)
	if mustReadState(t, r.l).Phase != PhaseStaged || len(r.sender.results()) != 0 {
		t.Fatal("the old binary interfered with a live helper")
	}
}

func TestTheNewBinaryAfterARebootMidSwapStartsItsOwnTrial(t *testing.T) {
	r := newResumeRig(t)
	st := r.trialState(0, 0)
	st.Phase, st.TrialDeadline = PhaseSwapping, nil
	mustWriteState(t, r.l, st)
	r.resume(newV)
	got := mustReadState(t, r.l)
	if got.Phase != PhaseTrial || got.Starts != 1 || got.TrialDeadline == nil ||
		!got.TrialDeadline.Equal(r.clock.Now().Add(600*time.Second)) {
		t.Fatalf("state = %+v", got)
	}
}

func TestTheNewBinaryRelaunchesAnOrphanedRollback(t *testing.T) {
	r := newResumeRig(t)
	st := r.trialState(0, 2)
	st.Phase, st.Reason = PhaseRollingBack, ReasonNoConfirmation
	mustWriteState(t, r.l, st)
	r.resume(newV)
	if !reflect.DeepEqual(r.svc.Calls(), []string{"launch:rollback"}) {
		t.Fatalf("calls = %v", r.svc.Calls())
	}
	if !reflect.DeepEqual(r.exits.got(), []int{ExitHandover}) {
		t.Fatalf("exits = %v", r.exits.got())
	}
}

func TestTheOldBinaryFinishesAnOrphanedRollback(t *testing.T) {
	r := newResumeRig(t)
	st := r.trialState(0, 2)
	st.Phase, st.Reason = PhaseRollingBack, ReasonCrashLoop
	mustWriteState(t, r.l, st)
	r.resume(oldV)
	got := mustReadState(t, r.l)
	if got.Phase != PhaseRolledBack || got.Reason != ReasonCrashLoop || !got.Reported {
		t.Fatalf("state = %+v", got)
	}
}

func TestAConfirmationThatCouldNotBeEnqueuedIsReportedOnTheNextStart(t *testing.T) {
	// Codex review on PR #85: confirm used to mark the state reported even when
	// the enqueue failed, and Resume never reports a confirmed state, so the
	// success was lost and the rollout stalled.
	r := newResumeRig(t)
	mustWriteState(t, r.l, r.trialState(10*time.Minute, 0))
	s := r.resume(newV)
	r.sender.err = os.ErrClosed
	s.Connected()
	r.clock.Advance(StableConnection)

	st := mustReadState(t, r.l)
	if st.Phase != PhaseConfirmed || st.Reported {
		t.Fatalf("state = %+v; a confirmation that was not enqueued must stay unreported", st)
	}

	r.sender.err = nil
	r.resume(newV)
	res := r.sender.results()
	if len(res) != 1 || res[0].State != "succeeded" || res[0].Result["outcome"] != OutcomeUpdated {
		t.Fatalf("results = %+v; want the success reported on the next start", res)
	}
	if !mustReadState(t, r.l).Reported {
		t.Fatal("not marked reported after the retry")
	}
	r.resume(newV)
	if len(r.sender.results()) != 1 {
		t.Fatal("reported twice")
	}
}
