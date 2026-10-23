// Package scheduler is the agent's one source of recurring work: a single
// goroutine, a min-heap of deadlines, and one timer armed at the nearest.
//
// Implements the scheduling half of docs/05-agent-spec.md's idle budget, and
// the mechanism docs/modules/endpoint-health-alerting/spec.md §0.5 assumes: a
// 32-check health policy is ONE entry on this wheel, not 32 tickers.
//
// # Why not a ticker per entry
//
// time.Ticker per entry is a goroutine and a wakeup per entry. Thirty-two
// checks would be thirty-two of each, on every endpoint in the fleet, all day,
// to do nothing. The idle budget in CLAUDE.md ("low-tens-of-MB resident and ~0%
// CPU at idle") is not reachable that way, and the failure is invisible: each
// individual ticker is cheap, and nobody notices until a customer with two
// thousand endpoints asks why their VDI hosts are warm.
//
// # Why not a polling loop
//
// A loop that wakes every second and asks "is anything due?" is the shape that
// makes an idle agent cost CPU forever. The Clock interface in clock.go is
// deliberately narrow -- Now and NewTimer, no Sleep -- so a polling loop cannot
// be written against it without reaching outside the interface, which is
// visible in review.
//
// TestAnIdleWheelDoesNotPoll is the assertion: an hour of simulated time with
// nothing due produces zero wakeups and zero callbacks. A polling loop would
// report ~3600.
//
// # Allocation
//
// A firing allocates nothing. The due-entry scratch slice is reused, the heap
// is a hand-rolled sift over []*entry rather than container/heap (whose
// interface boxing allocates on every Push), and the context handed to a
// callback is the one Run was given. internal/budget asserts AllocsPerOp == 0
// from an ordinary Test, so a regression fails CI instead of being a number in
// a benchmark log nobody reads.
package scheduler

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"runtime/debug"
	"sync"
	"sync/atomic"
	"time"
)

// idleParkFor is how long the timer is armed when the wheel holds no entries.
//
// It is not "forever" because there is no such duration, and it is not short
// because a short one is a poll. A century is a value the wheel will never
// reach, and any Add wakes the loop immediately through the wake channel, so
// the number only ever decides how long a wheel with nothing in it sleeps.
const idleParkFor = 100 * 365 * 24 * time.Hour

// ErrInterval is returned by Add for a non-positive interval. An interval of
// zero re-arms at the same instant forever: a busy loop wearing a timer's
// clothes.
var ErrInterval = errors.New("scheduler: interval must be positive")

type entry struct {
	id    string
	every time.Duration
	fn    func(context.Context)
	next  time.Time
	index int
}

// Wheel is a timer-driven scheduler. One goroutine, whatever the entry count.
type Wheel struct {
	clock Clock
	log   *slog.Logger

	mu    sync.Mutex
	heap  []*entry
	byID  map[string]*entry
	due   []*entry // reused scratch, so a firing allocates nothing
	wake  chan struct{}
	woken atomic.Int64
}

// New builds a wheel. It does not start anything: Run does.
func New(clock Clock, log *slog.Logger) *Wheel {
	if clock == nil {
		clock = Real{}
	}
	if log == nil {
		log = slog.Default()
	}
	return &Wheel{
		clock: clock,
		log:   log,
		byID:  map[string]*entry{},
		wake:  make(chan struct{}, 1),
	}
}

// Add schedules fn to run every `every`, starting one interval from now.
//
// An existing entry with the same id is REPLACED. health.policy.apply installs
// a policy wholesale, and two live copies of one check would double every
// report the endpoint sends.
func (w *Wheel) Add(id string, every time.Duration, fn func(context.Context)) error {
	if every <= 0 {
		return fmt.Errorf("%w (id %q, got %v)", ErrInterval, id, every)
	}
	if fn == nil {
		return fmt.Errorf("scheduler: entry %q has no function", id)
	}
	w.mu.Lock()
	if old, ok := w.byID[id]; ok {
		w.removeAt(old.index)
	}
	e := &entry{id: id, every: every, fn: fn, next: w.clock.Now().Add(every)}
	w.push(e)
	w.byID[id] = e
	w.mu.Unlock()
	w.signal()
	return nil
}

// Remove drops an entry. Safe to call from inside a callback: the wheel does
// not hold its lock while a callback runs.
func (w *Wheel) Remove(id string) {
	w.mu.Lock()
	if e, ok := w.byID[id]; ok {
		w.removeAt(e.index)
		delete(w.byID, id)
	}
	w.mu.Unlock()
	w.signal()
}

// Len is the number of scheduled entries.
func (w *Wheel) Len() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.heap)
}

// Wakeups counts how many times the loop has been woken by a timer firing. It
// is the observable behind "this does not poll".
func (w *Wheel) Wakeups() int64 { return w.woken.Load() }

// Run drives the wheel until ctx ends. It is the only goroutine.
func (w *Wheel) Run(ctx context.Context) error {
	timer := w.clock.NewTimer(w.nextDelay())
	defer timer.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-w.wake:
			// An entry was added or removed; the nearest deadline may have
			// moved. Re-arm and park again -- this is not a poll, it is an
			// edge.
			timer.Reset(w.nextDelay())
		case now := <-timer.C():
			w.woken.Add(1)
			w.fire(ctx, now)
			timer.Reset(w.nextDelay())
		}
	}
}

// nextDelay is the time until the nearest deadline, or idleParkFor when the
// wheel is empty.
func (w *Wheel) nextDelay() time.Duration {
	w.mu.Lock()
	defer w.mu.Unlock()
	if len(w.heap) == 0 {
		return idleParkFor
	}
	d := w.heap[0].next.Sub(w.clock.Now())
	if d < 0 {
		return 0
	}
	return d
}

// fire runs every entry whose deadline has passed, in deadline order.
//
// Due entries are collected under the lock and invoked WITHOUT it, so a
// callback may call Add or Remove. Holding the lock across a callback is the
// classic deadlock here and TestRemoveFromInsideACallbackDoesNotDeadlock is the
// observation.
//
// Rescheduling is from the previous deadline, not from `now`: re-arming from
// now makes a 60s check drift by however long the callback took, every time,
// and a fleet of drifting checks is a fleet whose reports bunch up.
func (w *Wheel) fire(ctx context.Context, now time.Time) {
	w.mu.Lock()
	w.due = w.due[:0]
	for len(w.heap) > 0 && !w.heap[0].next.After(now) {
		e := w.heap[0]
		w.due = append(w.due, e)
		e.next = e.next.Add(e.every)
		// A callback that took longer than its own interval must not fire twice
		// to "catch up": that is how a slow check becomes a busy loop.
		if !e.next.After(now) {
			e.next = now.Add(e.every)
		}
		w.down(0)
	}
	due := w.due
	w.mu.Unlock()

	for _, e := range due {
		w.invoke(ctx, e)
	}
}

// invoke runs one callback behind a panic barrier. One bad check must not stop
// every other check on the endpoint.
func (w *Wheel) invoke(ctx context.Context, e *entry) {
	defer func() {
		if p := recover(); p != nil {
			w.log.Error("scheduled entry panicked",
				"id", e.id, "panic", fmt.Sprint(p), "stack", string(debug.Stack()))
		}
	}()
	e.fn(ctx)
}

func (w *Wheel) signal() {
	select {
	case w.wake <- struct{}{}:
	default:
	}
}

// ---------------------------------------------------------------------------
// Min-heap over []*entry, hand-rolled
//
// container/heap takes an interface and boxes the pushed value into `any`,
// which allocates on every Push. This wheel's whole claim is that a firing
// allocates nothing, so the heap is written out. It is thirty lines.
// ---------------------------------------------------------------------------

func (w *Wheel) push(e *entry) {
	e.index = len(w.heap)
	w.heap = append(w.heap, e)
	w.up(e.index)
}

func (w *Wheel) removeAt(i int) {
	last := len(w.heap) - 1
	if i != last {
		w.swap(i, last)
	}
	w.heap[last].index = -1
	w.heap = w.heap[:last]
	if i < last {
		w.down(i)
		w.up(i)
	}
}

func (w *Wheel) less(i, j int) bool { return w.heap[i].next.Before(w.heap[j].next) }

func (w *Wheel) swap(i, j int) {
	w.heap[i], w.heap[j] = w.heap[j], w.heap[i]
	w.heap[i].index = i
	w.heap[j].index = j
}

func (w *Wheel) up(i int) {
	for i > 0 {
		parent := (i - 1) / 2
		if !w.less(i, parent) {
			return
		}
		w.swap(i, parent)
		i = parent
	}
}

func (w *Wheel) down(i int) {
	n := len(w.heap)
	for {
		l, r := 2*i+1, 2*i+2
		smallest := i
		if l < n && w.less(l, smallest) {
			smallest = l
		}
		if r < n && w.less(r, smallest) {
			smallest = r
		}
		if smallest == i {
			return
		}
		w.swap(i, smallest)
		i = smallest
	}
}
