package update

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Phase is one state of spec §5.3's machine.
type Phase string

const (
	PhaseStaged         Phase = "staged"
	PhaseSwapping       Phase = "swapping"
	PhaseTrial          Phase = "trial"
	PhaseConfirmed      Phase = "confirmed"
	PhaseRollingBack    Phase = "rolling_back"
	PhaseRolledBack     Phase = "rolled_back"
	PhaseAborted        Phase = "aborted"
	PhaseRollbackFailed Phase = "rollback_failed"
)

// Terminal reports whether nothing further happens in this phase.
func (p Phase) Terminal() bool {
	switch p {
	case PhaseConfirmed, PhaseRolledBack, PhaseAborted, PhaseRollbackFailed:
		return true
	}
	return false
}

// State is <data>/update/state.json. Only a verified handler creates it; the
// helper and Resume advance it.
type State struct {
	Phase         Phase           `json:"phase"`
	JobID         string          `json:"job_id"`
	FromVersion   string          `json:"from_version"`
	ToVersion     string          `json:"to_version"`
	SHA256        string          `json:"sha256"`
	PrevSHA256    string          `json:"prev_sha256"`
	TargetPath    string          `json:"target_path"`
	HealthWindowS int             `json:"health_window_s"`
	TrialDeadline *time.Time      `json:"trial_deadline,omitempty"`
	Starts        int             `json:"starts"`
	Reason        string          `json:"reason,omitempty"`
	Reported      bool            `json:"reported"`
	Signature     SignatureStatus `json:"signature"`
	RolloutID     string          `json:"rollout_id,omitempty"`
}

// MaxStarts is how many trial starts are tolerated before a crash loop is
// declared (spec §5.3: starts > 3).
const MaxStarts = 3

// BinaryName is the agent's file name on this OS.
func BinaryName() string {
	if runtime.GOOS == "windows" {
		return "its-agent.exe"
	}
	return "its-agent"
}

// Layout names every path under <data>/update.
type Layout struct{ Dir string }

// NewLayout returns the layout for an agent data directory.
func NewLayout(dataDir string) Layout { return Layout{Dir: filepath.Join(dataDir, "update")} }

// LayoutForState recovers the layout from a state-file path.
func LayoutForState(statePath string) Layout { return Layout{Dir: filepath.Dir(statePath)} }

func (l Layout) DataDir() string      { return filepath.Dir(l.Dir) }
func (l Layout) StatePath() string    { return filepath.Join(l.Dir, "state.json") }
func (l Layout) StagedDir() string    { return filepath.Join(l.Dir, "staged") }
func (l Layout) PrevDir() string      { return filepath.Join(l.Dir, "prev") }
func (l Layout) Download() string     { return filepath.Join(l.StagedDir(), "download.part") }
func (l Layout) StagedBinary() string { return filepath.Join(l.StagedDir(), BinaryName()) }
func (l Layout) PrevBinary() string   { return filepath.Join(l.PrevDir(), BinaryName()) }
func (l Layout) DecisionLock() string { return filepath.Join(l.Dir, "decision.lock") }
func (l Layout) HelperLock() string   { return filepath.Join(l.Dir, "helper.lock") }

// ReadState reads the state file. A missing file is os.ErrNotExist.
func ReadState(path string) (*State, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var s State
	if err := json.NewDecoder(io.LimitReader(f, 64<<10)).Decode(&s); err != nil {
		return nil, fmt.Errorf("update state is unreadable: %w", err)
	}
	return &s, nil
}

// WriteState replaces the state file atomically: temp file, fsync, rename.
func WriteState(path string, s *State) error {
	b, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	_ = os.Remove(tmp)
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.Write(b); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Decision is what the winner of the decision lock decided.
type Decision string

const (
	DecideConfirm  Decision = "confirm"
	DecideRollback Decision = "rollback"
)

// TryDecide creates the decision lock with O_EXCL. Exactly one caller per
// update wins; everyone else stands down (spec §5.3).
func TryDecide(path string, d Decision) (bool, error) {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		if errors.Is(err, os.ErrExist) {
			return false, nil
		}
		return false, err
	}
	_, werr := f.WriteString(string(d))
	serr := f.Sync()
	cerr := f.Close()
	return true, errors.Join(werr, serr, cerr)
}

// ReadDecision returns the decision taken so far, "" if none.
func ReadDecision(path string) Decision {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return Decision(strings.TrimSpace(string(b)))
}
