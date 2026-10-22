package update

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"sort"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// fakeClock fires timers only when the test says so.
type fakeClock struct {
	mu     sync.Mutex
	now    time.Time
	timers []*fakeTimer
}

type fakeTimer struct {
	clock   *fakeClock
	at      time.Time
	f       func()
	stopped bool
	fired   bool
}

func (t *fakeTimer) Stop() bool {
	t.clock.mu.Lock()
	defer t.clock.mu.Unlock()
	was := !t.stopped && !t.fired
	t.stopped = true
	return was
}

func newFakeClock() *fakeClock {
	return &fakeClock{now: time.Date(2026, 9, 23, 10, 0, 0, 0, time.UTC)}
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) AfterFunc(d time.Duration, f func()) Stopper {
	c.mu.Lock()
	defer c.mu.Unlock()
	t := &fakeTimer{clock: c, at: c.now.Add(d), f: f}
	c.timers = append(c.timers, t)
	return t
}

// Advance moves time and fires every due, live timer in deadline order.
func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.now = c.now.Add(d)
	var due []*fakeTimer
	for _, t := range c.timers {
		if !t.stopped && !t.fired && !t.at.After(c.now) {
			t.fired = true
			due = append(due, t)
		}
	}
	c.mu.Unlock()
	sort.Slice(due, func(i, j int) bool { return due[i].at.Before(due[j].at) })
	for _, t := range due {
		t.f()
	}
}

func (c *fakeClock) live() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := 0
	for _, t := range c.timers {
		if !t.stopped && !t.fired {
			n++
		}
	}
	return n
}

// fakeServices records service-manager calls.
type fakeServices struct {
	mu          sync.Mutex
	calls       []string
	target      string
	identifyErr error
	stopForSwap bool
	launchErr   error
	onRestart   func()
}

func (f *fakeServices) record(s string) {
	f.mu.Lock()
	f.calls = append(f.calls, s)
	f.mu.Unlock()
}

func (f *fakeServices) Calls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

func (f *fakeServices) Identify(context.Context) (string, error) {
	f.record("identify")
	return f.target, f.identifyErr
}

func (f *fakeServices) LaunchHelper(_ context.Context, _ string, phase string, _ Layout, _ string) error {
	f.record("launch:" + phase)
	return f.launchErr
}

func (f *fakeServices) StopForSwap() bool { return f.stopForSwap }

func (f *fakeServices) Stop(context.Context) error  { f.record("stop"); return nil }
func (f *fakeServices) Start(context.Context) error { f.record("start"); return nil }

func (f *fakeServices) Restart(context.Context) error {
	f.record("restart")
	if f.onRestart != nil {
		f.onRestart()
	}
	return nil
}

func (f *fakeServices) HelperExiting(context.Context) { f.record("helper-exiting") }

// recordingSender captures results sent through the outbox.
type recordingSender struct {
	mu   sync.Mutex
	sent []wire.JobResult
	err  error
}

func (r *recordingSender) Send(_ context.Context, msg any) error {
	if r.err != nil {
		return r.err
	}
	res, ok := msg.(wire.JobResult)
	if !ok {
		return errors.New("not a job result")
	}
	r.mu.Lock()
	r.sent = append(r.sent, res)
	r.mu.Unlock()
	return nil
}

func (r *recordingSender) results() []wire.JobResult {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]wire.JobResult(nil), r.sent...)
}

type exitRecorder struct {
	mu    sync.Mutex
	codes []int
}

func (e *exitRecorder) exit(code int) {
	e.mu.Lock()
	e.codes = append(e.codes, code)
	e.mu.Unlock()
}

func (e *exitRecorder) got() []int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]int(nil), e.codes...)
}

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func mustWriteState(t *testing.T, l Layout, s *State) {
	t.Helper()
	if err := os.MkdirAll(l.Dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := WriteState(l.StatePath(), s); err != nil {
		t.Fatal(err)
	}
}

func mustReadState(t *testing.T, l Layout) *State {
	t.Helper()
	s, err := ReadState(l.StatePath())
	if err != nil {
		t.Fatal(err)
	}
	return s
}
