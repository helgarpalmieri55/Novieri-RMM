package update

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// StableConnection is how long a connection must stay up to confirm a trial.
// It is transport.DefaultResetAfter: the transport's own definition of a
// connection that counts.
const StableConnection = 60 * time.Second

// Sender is the persisted outbox (jobs.Outbox satisfies it).
type Sender interface {
	Send(ctx context.Context, msg any) error
}

// ResumeOptions configures Resume.
type ResumeOptions struct {
	Version  string
	DataDir  string
	Clock    Clock
	Services ServiceManager
	// Exit ends this process; it must not return in production.
	Exit   func(code int)
	Logger *slog.Logger
}

// Session is a pending update seen at start-up. A nil *Session is valid and
// does nothing, which is the common case.
type Session struct {
	opts    ResumeOptions
	l       Layout
	pending *State // a terminal state still to be reported

	mu       sync.Mutex
	state    *State
	sender   Sender
	inTrial  bool
	deadline Stopper
	stable   Stopper
}

// Resume inspects <data>/update/state.json once at start-up (spec §5.3, §5.4).
// With no state file it costs one os.Stat and returns nil.
func Resume(o ResumeOptions) *Session {
	statePath := filepath.Join(o.DataDir, "update", "state.json")
	if _, err := os.Stat(statePath); err != nil {
		return nil
	}
	if o.Clock == nil {
		o.Clock = RealClock{}
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	if o.Exit == nil {
		o.Exit = os.Exit
	}
	s := &Session{opts: o, l: NewLayout(o.DataDir)}
	st, err := ReadState(statePath)
	if err != nil {
		o.Logger.Warn("self-update state is unreadable; ignoring it", "err", err)
		return nil
	}
	s.state = st
	s.resume()
	return s
}

func (s *Session) log() *slog.Logger { return s.opts.Logger }

func (s *Session) write() {
	if err := WriteState(s.l.StatePath(), s.state); err != nil {
		s.log().Error("could not persist self-update state", "err", err)
	}
}

func (s *Session) resume() {
	st := s.state
	v := s.opts.Version
	helperAlive := HelperAlive(s.l.HelperLock())
	now := s.opts.Clock.Now()

	switch {
	case st.Phase.Terminal():
		if !st.Reported {
			s.pending = st
		}

	case st.Phase == PhaseTrial && sameVersion(v, st.ToVersion),
		st.Phase == PhaseSwapping && sameVersion(v, st.ToVersion):
		if st.Phase == PhaseSwapping {
			// Rebooted between the rename and the trial write: this binary
			// keeps its own deadline from here.
			d := now.Add(time.Duration(st.HealthWindowS) * time.Second)
			st.Phase, st.TrialDeadline, st.Starts = PhaseTrial, &d, 0
		}
		st.Starts++
		s.write()
		switch {
		case st.Starts > MaxStarts:
			s.rollback(ReasonCrashLoop)
		case st.TrialDeadline == nil || !now.Before(*st.TrialDeadline):
			s.rollback(ReasonNoConfirmation)
		default:
			s.mu.Lock()
			s.inTrial = true
			s.deadline = s.opts.Clock.AfterFunc(st.TrialDeadline.Sub(now), func() {
				s.rollback(ReasonNoConfirmation)
			})
			s.mu.Unlock()
			s.log().Info("self-update trial: confirming once the gateway connection holds",
				"to_version", st.ToVersion, "start", st.Starts, "deadline", st.TrialDeadline)
		}

	case st.Phase == PhaseRollingBack && !sameVersion(v, st.FromVersion):
		// The binary under trial, while a rollback is decided.
		if !helperAlive {
			s.launchRollback()
		}
		s.opts.Exit(ExitHandover)

	case st.Phase == PhaseRollingBack:
		if !helperAlive {
			// The helper died after restoring the file; finish its record.
			st.Phase = PhaseRolledBack
			if st.Reason == "" {
				st.Reason = ReasonNoConfirmation
			}
			st.Reported = false
			s.write()
			s.pending = st
		}

	default:
		// staged, swapping or trial with the old (or a manually installed)
		// binary running. A live helper is still working; a dead one was
		// killed before the swap took effect.
		if helperAlive {
			return
		}
		st.Phase, st.Reason, st.Reported = PhaseAborted, ReasonInterrupted, false
		s.write()
		s.pending = st
	}
}

func sameVersion(a, b string) bool {
	va, err1 := ParseVersion(a)
	vb, err2 := ParseVersion(b)
	if err1 != nil || err2 != nil {
		return a == b
	}
	return Compare(va, vb) == 0
}

// rollback records the decision and hands over to the helper. Only the
// holder of the decision lock acts; a loser exits and lets the winner act.
func (s *Session) rollback(reason string) {
	s.mu.Lock()
	if s.deadline != nil {
		s.deadline.Stop()
	}
	if s.stable != nil {
		s.stable.Stop()
	}
	s.inTrial = false
	s.mu.Unlock()

	won, err := TryDecide(s.l.DecisionLock(), DecideRollback)
	if err != nil {
		s.log().Error("self-update decision lock", "err", err)
	}
	if !won {
		if ReadDecision(s.l.DecisionLock()) == DecideConfirm {
			return
		}
		s.opts.Exit(ExitHandover)
		return
	}
	s.state.Phase, s.state.Reason = PhaseRollingBack, reason
	s.write()
	s.log().Warn("self-update trial failed; rolling back", "reason", reason)
	if !HelperAlive(s.l.HelperLock()) {
		s.launchRollback()
	}
	s.opts.Exit(ExitHandover)
}

func (s *Session) launchRollback() {
	if s.opts.Services == nil {
		return
	}
	err := s.opts.Services.LaunchHelper(context.Background(), s.l.PrevBinary(), HelperRollback, s.l,
		filepath.Dir(s.state.TargetPath))
	if err != nil {
		s.log().Error("could not launch the rollback helper", "err", err)
	}
}

// Attach gives the session the outbox. Any terminal outcome not yet reported
// is enqueued now and then marked reported.
func (s *Session) Attach(ctx context.Context, sender Sender) {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.sender = sender
	s.mu.Unlock()
	if s.pending == nil {
		return
	}
	st := s.pending
	msg := wire.JobResult{JobID: st.JobID, State: wire.StateSucceeded,
		Result: ResultBody(st, OutcomeUpdated)}
	if st.Phase != PhaseConfirmed {
		outcome := OutcomeAborted
		if st.Phase == PhaseRolledBack || st.Phase == PhaseRollbackFailed {
			outcome = OutcomeRolledBack
		}
		body := ResultBody(st, outcome)
		body["reason"] = st.Reason
		if st.PrevSHA256 != "" {
			body["sha256"] = st.PrevSHA256
		}
		msg = wire.JobResult{JobID: st.JobID, State: wire.StateFailed, Result: body}
	}
	if err := sender.Send(ctx, msg); err != nil {
		s.log().Warn("could not enqueue the self-update result; will retry at next start", "err", err)
		return
	}
	st.Reported = true
	s.write()
	s.pending = nil
}

// ResultBody is the AgentUpdateResult body for a state.
func ResultBody(st *State, outcome string) map[string]any {
	body := map[string]any{
		"outcome":      outcome,
		"from_version": st.FromVersion,
		"to_version":   st.ToVersion,
		"sha256":       st.SHA256,
		"starts":       st.Starts,
	}
	if st.Signature != "" {
		body["signature"] = string(st.Signature)
	}
	return body
}

// Connected is called when a gateway connection is established.
func (s *Session) Connected() {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.inTrial {
		return
	}
	if s.stable != nil {
		s.stable.Stop()
	}
	s.stable = s.opts.Clock.AfterFunc(StableConnection, s.confirm)
}

// Disconnected is called when that connection is lost.
func (s *Session) Disconnected() {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stable != nil {
		s.stable.Stop()
		s.stable = nil
	}
}

// confirm: enqueue the success, then write confirmed, then stop the timer.
func (s *Session) confirm() {
	s.mu.Lock()
	if !s.inTrial {
		s.mu.Unlock()
		return
	}
	sender := s.sender
	s.mu.Unlock()

	won, err := TryDecide(s.l.DecisionLock(), DecideConfirm)
	if err != nil || !won {
		return
	}
	reported := false
	if sender != nil {
		msg := wire.JobResult{JobID: s.state.JobID, State: wire.StateSucceeded,
			Result: ResultBody(s.state, OutcomeUpdated)}
		if err := sender.Send(context.Background(), msg); err != nil {
			s.log().Error("could not enqueue the self-update result; will retry at next start", "err", err)
		} else {
			reported = true
		}
	}
	// Unreported until the enqueue succeeds: Resume retries it on a later
	// start, as it does for rollbacks and aborts.
	s.state.Phase, s.state.Reported = PhaseConfirmed, reported
	s.write()

	s.mu.Lock()
	s.inTrial = false
	if s.deadline != nil {
		s.deadline.Stop()
	}
	s.mu.Unlock()
	s.log().Info("self-update confirmed", "version", s.state.ToVersion)
}
