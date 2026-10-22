//go:build windows

package service

import (
	"context"
	"testing"
	"time"

	"golang.org/x/sys/windows/svc"
)

// THIS FILE NOW RUNS, on the agent CI job's windows-latest runner, in
// addition to being compiled by ubuntu-latest's `GOOS=windows go vet ./...`
// and `GOOS=windows go test -c`.
//
// Until that runner was added, the sentence here read "THIS FILE IS COMPILED
// BY CI AND EXECUTED BY NOBODY" -- accurate at the time, and left as a
// worked example rather than deleted outright: a signature drift or type
// error went red on ubuntu-latest; a behavioural regression did not, because
// nothing there could execute a single line of this file. The tests below
// that fake the SCM's ChangeRequest channel (TestStopControlCancelsThe...
// and its neighbours) needed nothing more than that to be trustworthy, since
// they exercise this package's own logic against a fake, not the real SCM.
// TestIsWindowsServiceDetectsAnOrdinaryProcess is the one addition that
// could not be answered any other way: it calls the real
// svc.IsWindowsService(), which asks Windows a question about this process's
// own parentage that no fake can stand in for.

// statusSink drains the status channel the SCM would otherwise consume and
// records what the handler reported.
func statusSink(t *testing.T) (chan<- svc.Status, func() []svc.State) {
	t.Helper()
	ch := make(chan svc.Status, 32)
	done := make(chan []svc.State, 1)
	go func() {
		var seen []svc.State
		for s := range ch {
			seen = append(seen, s.State)
		}
		done <- seen
	}()
	return ch, func() []svc.State {
		close(ch)
		return <-done
	}
}

// The stop path: a Stop control must cancel the context the agent is running
// under and wait for it to return, so buffered job results are flushed rather
// than truncated by a kill.
func TestStopControlCancelsTheAgentAndWaitsForIt(t *testing.T) {
	requests := make(chan svc.ChangeRequest, 1)
	status, collect := statusSink(t)

	returned := make(chan struct{})
	h := &handler{
		stopGrace: 5 * time.Second,
		main: func(ctx context.Context) int {
			<-ctx.Done()
			close(returned)
			return 0
		},
	}

	go func() {
		requests <- svc.ChangeRequest{Cmd: svc.Stop}
	}()

	ssec, code := h.Execute([]string{Name}, requests, status)
	if ssec || code != 0 {
		t.Fatalf("Execute = (%v, %d), want (false, 0) for a clean stop", ssec, code)
	}
	select {
	case <-returned:
	case <-time.After(time.Second):
		t.Fatal("Execute returned before the agent did; a Stop must not truncate a flush")
	}

	states := collect()
	wantRunning := false
	for _, s := range states {
		if s == svc.Running {
			wantRunning = true
		}
	}
	if !wantRunning {
		t.Fatalf("the handler never reported Running: %v", states)
	}
	if len(states) == 0 || states[len(states)-1] != svc.StopPending {
		t.Fatalf("last reported state = %v, want StopPending", states)
	}
}

// An agent that exits on its own (a terminal 4401 from the gateway, an
// unusable identity) must surface its exit code to the SCM as a
// SERVICE-SPECIFIC one. Reported as a win32 code it would be read as an
// unrelated system error.
func TestAnAgentExitIsReportedAsAServiceSpecificCode(t *testing.T) {
	requests := make(chan svc.ChangeRequest, 1)
	status, collect := statusSink(t)

	h := &handler{
		stopGrace: 5 * time.Second,
		main:      func(context.Context) int { return 4 },
	}
	ssec, code := h.Execute([]string{Name}, requests, status)
	if !ssec || code != 4 {
		t.Fatalf("Execute = (%v, %d), want (true, 4)", ssec, code)
	}
	collect()
}

// Interrogate is always accepted (svc.Accepted's own documentation) and must be
// answered by echoing the current status. Treating it as a stop would take the
// fleet down on a routine SCM poll.
func TestInterrogateEchoesStatusAndKeepsRunning(t *testing.T) {
	requests := make(chan svc.ChangeRequest, 2)
	status, collect := statusSink(t)

	h := &handler{
		stopGrace: 5 * time.Second,
		main:      func(ctx context.Context) int { <-ctx.Done(); return 0 },
	}
	current := svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	go func() {
		requests <- svc.ChangeRequest{Cmd: svc.Interrogate, CurrentStatus: current}
		time.Sleep(50 * time.Millisecond)
		requests <- svc.ChangeRequest{Cmd: svc.Stop}
	}()

	ssec, code := h.Execute([]string{Name}, requests, status)
	if ssec || code != 0 {
		t.Fatalf("Execute = (%v, %d) after an Interrogate + Stop", ssec, code)
	}
	states := collect()
	running := 0
	for _, s := range states {
		if s == svc.Running {
			running++
		}
	}
	if running < 2 {
		t.Fatalf("Interrogate was not echoed: states %v", states)
	}
}

// TestIsWindowsServiceDetectsAnOrdinaryProcess exercises the real Windows
// query Run() uses to choose between svc.Run and runConsole: whether this
// process's parent is services.exe in session 0. A `go test` binary is
// started by the test runner, not by the SCM, so the real answer here must be
// "no" -- and getting that wrong in either direction is a real incident, not
// a theoretical one: a false "yes" would make `its-agent --version` on an
// operator's own machine hang waiting for an SCM dispatcher that will never
// call it, and a false "no" would make the installed service fail to start
// under the SCM at all.
func TestIsWindowsServiceDetectsAnOrdinaryProcess(t *testing.T) {
	isService, err := svc.IsWindowsService()
	if err != nil {
		t.Fatalf("svc.IsWindowsService: %v", err)
	}
	if isService {
		t.Fatal("this test binary was not started by the Service Control Manager, but " +
			"IsWindowsService reported true")
	}
}
