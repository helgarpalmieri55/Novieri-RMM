//go:build !windows

package service

import (
	"context"
	"os"
	"os/signal"
	"syscall"
)

// Run runs main until it returns, cancelling its context on SIGINT or SIGTERM,
// and returns main's exit code.
//
// systemd sends SIGTERM on `systemctl stop` and at shutdown; launchd sends
// SIGTERM on `launchctl bootout`. Neither needs anything else from the process,
// which is why there is no readiness protocol here: the unit is Type=simple
// deliberately (Type=notify would mean either linking sd_notify or hand-rolling
// the socket protocol, for a service whose readiness nothing waits on).
//
// A SECOND signal is not special-cased. The default disposition is already
// gone by then, so an agent wedged in shutdown is killed by the service manager
// (systemd's TimeoutStopSec) rather than by a hand-rolled escalation here.
func Run(main func(context.Context) int) int {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return main(ctx)
}
