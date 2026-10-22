package scheduler

import (
	"sync"
	"testing"
	"time"
)

// Clock is the wheel's only source of time.
//
// It is injected for two reasons. The obvious one is that a test asserting "an
// entry fires three times across three intervals" must not take three intervals
// of wall-clock to do it. The load-bearing one is that a Clock with exactly
// these two methods cannot express a polling loop: there is no Sleep-in-a-loop
// shape available, only "arm a timer and park on it".
type Clock interface {
	Now() time.Time
	NewTimer(d time.Duration) Timer
}

// Timer is one armed deadline. C never yields more than one value per arming.
type Timer interface {
	C() <-chan time.Time
	// Reset re-arms for d from now. It reports whether the timer was active.
	Reset(d time.Duration) bool
	Stop() bool
}

// Real is the production clock.
type Real struct{}

func (Real) Now() time.Time { return time.Now() }

func (Real) NewTimer(d time.Duration) Timer { return &realTimer{t: time.NewTimer(d)} }

// realTimer wraps time.Timer.
//
// No drain-before-Reset dance. Since Go 1.23 a Timer's channel is unbuffered
// and Stop/Reset no longer leave a stale value behind (go.dev/issue/37196); the
// module requires go 1.24 and the toolchain here is go1.24.7. On an older
// toolchain this type would need to drain, and the wheel would occasionally
// fire an entry one deadline early -- which is why the requirement is written
// in go.mod rather than assumed.
type realTimer struct{ t *time.Timer }

func (r *realTimer) C() <-chan time.Time        { return r.t.C }
func (r *realTimer) Reset(d time.Duration) bool { return r.t.Reset(d) }
func (r *realTimer) Stop() bool                 { return r.t.Stop() }

// Fake is a deterministic clock for tests.
//
// Advance does not merely move a number: it hands each due deadline to whoever
// is parked on it and WAITS for that consumer to re-arm before moving on. A
// clock that just bumped a counter would turn every scheduler test into a race
// against a goroutine, and races get "fixed" with sleeps, and sleeps make the
// suite slow enough that somebody deletes the test.
type Fake struct {
	mu     sync.Mutex
	now    time.Time
	timers []*fakeTimer
	// created counts NewTimer calls and rearms counts Reset/Stop calls, so a
	// test can wait for the wheel to have picked up a change instead of racing
	// it. Racing it would be "fixed" with a sleep, and a suite with sleeps in it
	// gets slow enough that somebody deletes a test.
	created int
	rearms  int
	waiters []chan struct{}

	// guard is the deadlock timer Advance parks against. It is a single reused
	// timer rather than time.After, because time.After allocates a timer and a
	// channel on EVERY call -- and Advance is the operation internal/budget
	// measures, so apparatus allocations would be indistinguishable from the
	// wheel's own.
	guard *time.Timer
}

// Rearms is how many times a timer has been Reset or Stopped.
func (f *Fake) Rearms() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.rearms
}

// AwaitRearms blocks until at least n Reset/Stop calls have happened.
//
// The pattern is: read Rearms(), change the wheel, wait for one more. That is
// the barrier between "Add returned" and "the loop is armed for the new
// deadline" -- without it, Advance can walk straight past a deadline the wheel
// has not been told about yet.
func (f *Fake) AwaitRearms(t testing.TB, n int) {
	t.Helper()
	f.await(t, n, func() int { return f.rearms }, "rearms")
}

// NewFake starts a fake clock at t.
func NewFake(t time.Time) *Fake {
	g := time.NewTimer(time.Hour)
	if !g.Stop() {
		<-g.C
	}
	return &Fake{now: t, guard: g}
}

// advanceGuard is how long Advance waits for a consumer before declaring a
// deadlock. It is a safety valve, not a sleep: on the happy path it is never
// reached.
const advanceGuard = 5 * time.Second

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *Fake) NewTimer(d time.Duration) Timer {
	f.mu.Lock()
	defer f.mu.Unlock()
	t := &fakeTimer{
		f:        f,
		ch:       make(chan time.Time),
		ack:      make(chan struct{}, 1),
		deadline: f.now.Add(d),
		active:   d > 0,
	}
	f.timers = append(f.timers, t)
	f.created++
	f.notifyLocked()
	return t
}

// AwaitTimers blocks until at least n timers have been created, so a test never
// races the wheel's first arming.
func (f *Fake) AwaitTimers(t testing.TB, n int) {
	t.Helper()
	f.await(t, n, func() int { return f.created }, "timers created")
}

// await blocks until read() >= n. read is called with f.mu held.
func (f *Fake) await(t testing.TB, n int, read func() int, what string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		f.mu.Lock()
		got := read()
		var ch chan struct{}
		if got < n {
			ch = make(chan struct{})
			f.waiters = append(f.waiters, ch)
		}
		f.mu.Unlock()
		if got >= n {
			return
		}
		select {
		case <-ch:
		case <-time.After(time.Until(deadline)):
			t.Fatalf("%s reached %d after 5s, want %d", what, got, n)
		}
	}
}

// notifyLocked wakes every await. Called with f.mu held.
func (f *Fake) notifyLocked() {
	for _, w := range f.waiters {
		close(w)
	}
	f.waiters = nil
}

// Advance moves the clock forward, firing every deadline it crosses in order
// and waiting for each to be handled.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	target := f.now.Add(d)
	f.mu.Unlock()

	for {
		f.mu.Lock()
		var next *fakeTimer
		for _, t := range f.timers {
			if !t.active || t.deadline.After(target) {
				continue
			}
			if next == nil || t.deadline.Before(next.deadline) {
				next = t
			}
		}
		if next == nil {
			f.now = target
			f.mu.Unlock()
			return
		}
		f.now = next.deadline
		next.active = false
		fired := f.now
		ch, ack := next.ch, next.ack
		f.mu.Unlock()

		// An ack left over from an unrelated re-arm (an Add that woke the
		// wheel, say) would let the wait below return before this firing has
		// been handled.
		select {
		case <-ack:
		default:
		}

		f.guard.Reset(advanceGuard)
		select {
		case ch <- fired:
		case <-f.guard.C:
			panic("scheduler: nothing consumed a fired timer within " + advanceGuard.String())
		}
		if !f.guard.Stop() {
			select {
			case <-f.guard.C:
			default:
			}
		}

		f.guard.Reset(advanceGuard)
		select {
		case <-ack:
		case <-f.guard.C:
			panic("scheduler: the timer consumer never re-armed within " + advanceGuard.String())
		}
		if !f.guard.Stop() {
			select {
			case <-f.guard.C:
			default:
			}
		}
	}
}

type fakeTimer struct {
	f        *Fake
	ch       chan time.Time
	ack      chan struct{}
	deadline time.Time
	active   bool
}

func (t *fakeTimer) C() <-chan time.Time { return t.ch }

func (t *fakeTimer) Reset(d time.Duration) bool {
	t.f.mu.Lock()
	was := t.active
	t.deadline = t.f.now.Add(d)
	t.active = true
	t.f.rearms++
	t.f.notifyLocked()
	t.f.mu.Unlock()
	t.signal()
	return was
}

func (t *fakeTimer) Stop() bool {
	t.f.mu.Lock()
	was := t.active
	t.active = false
	t.f.rearms++
	t.f.notifyLocked()
	t.f.mu.Unlock()
	t.signal()
	return was
}

// signal tells a waiting Advance that the consumer has re-armed. Non-blocking:
// the buffer of one is the whole handshake.
func (t *fakeTimer) signal() {
	select {
	case t.ack <- struct{}{}:
	default:
	}
}
