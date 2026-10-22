package update

import (
	"context"
	"log/slog"
	"os"
	"time"
)

// HelperOptions configures the transient helper (spec §5.2). It takes no
// network input: one state file in a root-only directory.
type HelperOptions struct {
	StatePath string
	Phase     string
	Services  ServiceManager
	Clock     Clock
	Sleep     func(ctx context.Context, d time.Duration) error
	// SelfTest runs a candidate's --update-selftest expecting wantVersion.
	SelfTest  func(ctx context.Context, binary, wantVersion string) error
	Logger    *slog.Logger
	PollEvery time.Duration
}

// HelperPollEvery is the helper's watch interval. It polls only while an
// update is in flight; the idle agent never runs this.
const HelperPollEvery = 5 * time.Second

const rollbackAttempts = 3

type helper struct {
	HelperOptions
	l Layout
}

// RunHelper is the body of `--update-helper`. It returns the exit code.
func RunHelper(ctx context.Context, o HelperOptions) int {
	if o.Clock == nil {
		o.Clock = RealClock{}
	}
	if o.Sleep == nil {
		o.Sleep = sleepCtx
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	if o.PollEvery <= 0 {
		o.PollEvery = HelperPollEvery
	}
	if o.SelfTest == nil {
		dataDir := LayoutForState(o.StatePath).DataDir()
		o.SelfTest = func(ctx context.Context, bin, want string) error {
			return RunSelfTest(ctx, bin, dataDir, want)
		}
	}
	h := &helper{HelperOptions: o, l: LayoutForState(o.StatePath)}

	release, err := AcquireHelperLock(h.l.HelperLock())
	if err != nil {
		h.Logger.Error("another update helper is running", "err", err)
		return 1
	}
	defer release()
	defer h.Services.HelperExiting(context.Background())

	st, err := ReadState(o.StatePath)
	if err != nil {
		h.Logger.Error("update state unreadable", "err", err)
		return 1
	}
	switch {
	case o.Phase == HelperApply && st.Phase == PhaseStaged:
		return h.apply(ctx, st)
	case o.Phase == HelperRollback && st.Phase == PhaseRollingBack:
		return h.rollback(ctx, st)
	}
	h.Logger.Warn("update helper has nothing to do", "helper", o.Phase, "phase", st.Phase)
	return 0
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

func (h *helper) write(st *State) {
	if err := WriteState(h.StatePath, st); err != nil {
		h.Logger.Error("could not persist update state", "err", err)
	}
}

// bounce restarts the service by whichever means this OS needs.
func (h *helper) bounce(ctx context.Context) {
	var err error
	if h.Services.StopForSwap() {
		err = h.Services.Start(ctx)
	} else {
		err = h.Services.Restart(ctx)
	}
	if err != nil {
		h.Logger.Error("could not restart the agent service", "err", err)
	}
}

func (h *helper) apply(ctx context.Context, st *State) int {
	st.Phase = PhaseSwapping
	h.write(st)

	abort := func(token string, stopped bool) int {
		st.Phase, st.Reason, st.Reported = PhaseAborted, token, false
		h.write(st)
		if stopped {
			h.bounce(ctx)
		} else {
			// The old binary reports the abort on its next start.
			if err := h.Services.Restart(ctx); err != nil {
				h.Logger.Error("could not restart the agent service", "err", err)
			}
		}
		_ = os.RemoveAll(h.l.StagedDir())
		h.Logger.Warn("update aborted before the swap", "reason", token)
		return 0
	}

	p, err := Prepare(ctx, h.l.StagedBinary(), st.SHA256, st.TargetPath,
		func(ctx context.Context, bin string) error { return h.SelfTest(ctx, bin, st.ToVersion) })
	if err != nil {
		return abort(tokenOr(err, ReasonInstallFailed), false)
	}
	if h.Services.StopForSwap() {
		if err := h.Services.Stop(ctx); err != nil {
			p.Discard()
			h.Logger.Error("could not stop the agent service", "err", err)
			return abort(ReasonServiceControlFailed, true)
		}
	}
	if err := p.Commit(); err != nil {
		return abort(tokenOr(err, ReasonInstallFailed), h.Services.StopForSwap())
	}

	// Trial is written before the restart, so the new binary knows it is on
	// trial from its first start.
	deadline := h.Clock.Now().Add(time.Duration(st.HealthWindowS) * time.Second)
	st.Phase, st.TrialDeadline, st.Starts = PhaseTrial, &deadline, 0
	h.write(st)
	h.bounce(ctx)
	h.Logger.Info("update swapped; watching the trial", "to_version", st.ToVersion, "deadline", deadline)
	return h.watch(ctx)
}

func tokenOr(err error, def string) string {
	if t := TokenOf(err); t != "" {
		return t
	}
	return def
}

func (h *helper) watch(ctx context.Context) int {
	for {
		if err := h.Sleep(ctx, h.PollEvery); err != nil {
			return 1
		}
		st, err := ReadState(h.StatePath)
		if err != nil {
			continue
		}
		switch st.Phase {
		case PhaseConfirmed:
			_ = os.RemoveAll(h.l.StagedDir())
			h.Logger.Info("update confirmed", "version", st.ToVersion)
			return 0
		case PhaseRollingBack:
			return h.rollback(ctx, st)
		case PhaseTrial:
			reason := ""
			switch {
			case st.Starts > MaxStarts:
				reason = ReasonCrashLoop
			case st.TrialDeadline == nil || !h.Clock.Now().Before(*st.TrialDeadline):
				reason = ReasonNoConfirmation
			}
			if ReadDecision(h.l.DecisionLock()) == DecideRollback {
				// Decided by the new binary, which died before recording it.
				if reason == "" {
					reason = ReasonNoConfirmation
				}
			} else if reason == "" {
				continue
			} else if won, _ := TryDecide(h.l.DecisionLock(), DecideRollback); !won {
				continue
			}
			st.Phase, st.Reason = PhaseRollingBack, reason
			h.write(st)
			return h.rollback(ctx, st)
		default:
			return 0
		}
	}
}

func (h *helper) rollback(ctx context.Context, st *State) int {
	if st.Reason == "" {
		st.Reason = ReasonNoConfirmation
	}
	var err error
	stopped := false
	for attempt := 0; attempt < rollbackAttempts; attempt++ {
		var p *Prepared
		p, err = Prepare(ctx, h.l.PrevBinary(), st.PrevSHA256, st.TargetPath,
			func(ctx context.Context, bin string) error { return h.SelfTest(ctx, bin, st.FromVersion) })
		// Every attempt: SCM recovery may have restarted the failing binary
		// since the last one, and a running image cannot be renamed over.
		if err == nil && h.Services.StopForSwap() {
			if err = h.Services.Stop(ctx); err == nil {
				stopped = true
			} else {
				p.Discard()
			}
		}
		if err == nil {
			err = p.Commit()
		}
		if err == nil {
			break
		}
		h.Logger.Error("rollback attempt failed", "attempt", attempt+1, "err", err)
		if serr := h.Sleep(ctx, 2*time.Second); serr != nil {
			break
		}
	}
	if err != nil {
		st.Phase, st.Reason, st.Reported = PhaseRollbackFailed, ReasonRollbackFailed, false
		h.write(st)
		if h.Services.StopForSwap() && !stopped {
			_ = h.Services.Restart(ctx)
		} else {
			h.bounce(ctx)
		}
		h.Logger.Error("rollback failed; this endpoint needs manual repair")
		return 1
	}
	// Recorded before the restart so the old binary reports it at once.
	st.Phase, st.Reported = PhaseRolledBack, false
	h.write(st)
	h.bounce(ctx)
	_ = os.RemoveAll(h.l.StagedDir())
	h.Logger.Warn("update rolled back", "reason", st.Reason, "version", st.FromVersion)
	return 0
}
