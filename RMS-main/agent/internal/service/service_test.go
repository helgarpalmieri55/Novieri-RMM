package service_test

import (
	"context"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/service"
)

// Run is the seam between "a process the operator started" and "a service the
// OS started". On unix it is a thin wrapper over signal.NotifyContext; on
// Windows it is the difference between a working service and error 1053 ("the
// service did not respond to the start or control request in a timely
// fashion"), because the SCM requires a dispatcher within ~30 s of start and a
// plain console binary never registers one.
//
// These tests cover the part that runs on every platform. The Windows control
// handler has its own test in service_windows_test.go, which CI COMPILES for
// windows/amd64 and cannot execute — see agent/packaging/README.md.

func TestRunPassesTheExitCodeThrough(t *testing.T) {
	for _, want := range []int{0, 3, 5} {
		got := service.Run(func(context.Context) int { return want })
		if got != want {
			t.Fatalf("Run returned %d, want %d", got, want)
		}
	}
}

// The context Run hands over is the shutdown signal. A cancelled-on-arrival
// context would make every service exit the moment it started.
func TestRunHandsOverALiveContext(t *testing.T) {
	service.Run(func(ctx context.Context) int {
		if ctx == nil {
			t.Fatal("Run passed a nil context")
		}
		select {
		case <-ctx.Done():
			t.Fatalf("the context was already cancelled: %v", ctx.Err())
		default:
		}
		return 0
	})
}

// Cancellation must be wired, whatever the platform's shutdown source is
// (SIGTERM, or the SCM's Stop control). This asserts the plumbing exists by
// cancelling from inside — the platform-specific source is what the two
// implementations differ on.
func TestTheContextIsCancellable(t *testing.T) {
	done := make(chan struct{})
	code := service.Run(func(ctx context.Context) int {
		inner, cancel := context.WithCancel(ctx)
		cancel()
		select {
		case <-inner.Done():
			close(done)
		case <-time.After(time.Second):
			t.Error("a context derived from Run's did not cancel")
		}
		return 0
	})
	if code != 0 {
		t.Fatalf("exit code %d", code)
	}
	<-done
}
