package update

import (
	"context"
	"time"
)

// Helper phases, as passed to --update-helper.
const (
	HelperApply    = "apply"
	HelperRollback = "rollback"
)

// ExitHandover is the exit code of a binary that stops so a rollback can run.
const ExitHandover = 7

// ServiceManager is the per-OS service control (spec §5.2, §5.5). The real
// implementations live in service_<os>.go; tests use a fake.
type ServiceManager interface {
	// Identify returns the service's configured binary path, provided this
	// process is the service's main process running that same file. Errors
	// carry ReasonNotRunningAsService or ReasonUnsupportedLayout.
	Identify(ctx context.Context) (string, error)
	// LaunchHelper starts `prev --update-helper=<phase> --state <l.StatePath()>`
	// outside the service's lifetime.
	LaunchHelper(ctx context.Context, prev, phase string, l Layout, targetDir string) error
	// StopForSwap is true where a running image cannot be replaced (Windows).
	StopForSwap() bool
	Stop(ctx context.Context) error
	Start(ctx context.Context) error
	Restart(ctx context.Context) error
	// HelperExiting is the helper's last act (macOS unloads its own job).
	HelperExiting(ctx context.Context)
}

// Stopper stops a timer.
type Stopper interface{ Stop() bool }

// Clock is time for the state machine; tests drive it by hand.
type Clock interface {
	Now() time.Time
	AfterFunc(d time.Duration, f func()) Stopper
}

// RealClock is the wall clock.
type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) AfterFunc(d time.Duration, f func()) Stopper { return time.AfterFunc(d, f) }
