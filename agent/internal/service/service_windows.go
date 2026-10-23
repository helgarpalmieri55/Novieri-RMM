//go:build windows

package service

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"sync/atomic"
	"time"

	"golang.org/x/sys/windows/svc"
)

// defaultStopGrace bounds how long a Stop control waits for the agent to
// return. The SCM's own patience is finite (30 s by default before it reports
// the service as hung), so this is deliberately under it: a stuck flush should
// end as a logged, bounded stop rather than as an SCM timeout that the operator
// reads as a crash.
const defaultStopGrace = 20 * time.Second

// Run runs main under the Windows Service Control Manager when the process was
// started by it, and as an ordinary console program otherwise.
//
// The detection matters for operability: the same binary must be startable by
// hand for `--version`, for a first-boot enrollment check, and for reading the
// startup log without the Event Viewer. svc.IsWindowsService looks at whether
// the parent process is `services.exe` in session 0, which is what .NET does
// for the same question.
func Run(main func(context.Context) int) int {
	isService, err := svc.IsWindowsService()
	if err != nil {
		// Undecidable, so choose the mode that still runs the agent. A wrong
		// guess towards "console" produces a service that exits with 1053 and a
		// clear event; a wrong guess towards "service" produces a hand-started
		// binary that hangs waiting for a dispatcher that will never connect.
		fmt.Fprintf(os.Stderr, "its-agent: cannot tell whether this is a service start (%v); running as a console program\n", err)
		isService = false
	}
	if !isService {
		return runConsole(main)
	}

	h := &handler{main: main, stopGrace: defaultStopGrace}
	if err := svc.Run(Name, h); err != nil {
		fmt.Fprintf(os.Stderr, "its-agent: service dispatcher: %v\n", err)
		return 1
	}
	return int(h.code.Load())
}

func runConsole(main func(context.Context) int) int {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	return main(ctx)
}

// handler is the SCM control loop.
//
// It has exactly one authority over the agent: it can cancel its context. It
// dispatches no work and knows nothing about jobs — invariant #1's only route
// into a capability is the signed-job runner, and a service control is not one.
type handler struct {
	main      func(context.Context) int
	stopGrace time.Duration
	code      atomic.Int32
}

func (h *handler) Execute(args []string, r <-chan svc.ChangeRequest, s chan<- svc.Status) (bool, uint32) {
	const accepted = svc.AcceptStop | svc.AcceptShutdown

	s <- svc.Status{State: svc.StartPending}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan int, 1)
	go func() { done <- h.main(ctx) }()

	// Reported BEFORE the agent has connected, on purpose. "Running" here means
	// the process is up and answering controls; connectivity is the control
	// plane's own observation (agents.last_seen_at), and a service that stays
	// StartPending until a gateway answers is a service the SCM kills on a
	// network outage.
	s <- svc.Status{State: svc.Running, Accepts: accepted}

	for {
		select {
		case code := <-done:
			// The agent exited on its own: a terminal 4401/4403, an unusable
			// identity, a runner that cannot verify. Report it as a
			// SERVICE-SPECIFIC code so the SCM's recovery action restarts it and
			// the number in the event log is the agent's, not a win32 error that
			// means something else entirely.
			h.code.Store(int32(code))
			s <- svc.Status{State: svc.StopPending}
			return code != 0, uint32(code)

		case c := <-r:
			switch c.Cmd {
			case svc.Interrogate:
				s <- c.CurrentStatus
			case svc.Stop, svc.Shutdown:
				s <- svc.Status{State: svc.StopPending}
				cancel()
				select {
				case code := <-done:
					h.code.Store(int32(code))
				case <-time.After(h.stopGrace):
					fmt.Fprintf(os.Stderr, "its-agent: agent did not stop within %s; stopping anyway\n", h.stopGrace)
				}
				return false, 0
			default:
				// Pause/Continue are not accepted, so the SCM will not send
				// them; anything else is ignored rather than guessed at.
			}
		}
	}
}
