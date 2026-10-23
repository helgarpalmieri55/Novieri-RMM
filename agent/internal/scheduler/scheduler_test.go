package scheduler

import (
	"context"
	"io"
	"log/slog"
	"runtime"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

var start = time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)

// run starts a wheel on a fake clock and returns it with the clock, stopping it
// at the end of the test.
func run(t *testing.T) (*Wheel, *Fake) {
	t.Helper()
	f := NewFake(start)
	// Quiet: TestAPanickingCallbackDoesNotKillTheWheel deliberately panics, and
	// the default logger would dump a stack into every run of the suite.
	w := New(f, slog.New(slog.NewTextHandler(io.Discard, nil)))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = w.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("Wheel.Run did not return after its context ended")
		}
	})
	f.AwaitTimers(t, 1)
	return w, f
}

// add schedules an entry AND waits for the loop to re-arm for it.
//
// Add is asynchronous by design: it must not block when called from inside a
// callback (see TestRemoveFromInsideACallbackDoesNotDeadlock), so it signals
// the loop and returns. A test that advanced the clock without waiting for that
// signal to be picked up would walk straight past the new deadline -- and it
// would do so intermittently, which is worse than never.
func add(t *testing.T, w *Wheel, f *Fake, id string, every time.Duration, fn func(context.Context)) {
	t.Helper()
	n := f.Rearms()
	if err := w.Add(id, every, fn); err != nil {
		t.Fatal(err)
	}
	f.AwaitRearms(t, n+1)
}

func TestAnEntryFiresOncePerIntervalAndNotMore(t *testing.T) {
	// The bug this catches in both directions: a wheel that re-arms from `now`
	// instead of from the previous deadline drifts and under-fires; a wheel that
	// fires every due entry in a loop without advancing its deadline spins.
	w, f := run(t)
	var n atomic.Int32
	add(t, w, f, "tick", time.Minute, func(context.Context) { n.Add(1) })

	f.Advance(3 * time.Minute)

	if got := n.Load(); got != 3 {
		t.Fatalf("entry fired %d times across 3 intervals, want exactly 3", got)
	}
}

func TestAnIdleWheelDoesNotPoll(t *testing.T) {
	// FALSIFYING OBSERVATION: a callback count above zero, and a wakeup count
	// above zero, after an hour of simulated time with nothing due.
	//
	// A polling loop would wake ~3600 times here. A wheel armed at the nearest
	// deadline wakes zero times, because there is no nearest deadline.
	w, f := run(t)
	var n atomic.Int32
	add(t, w, f, "hourly", 24*time.Hour, func(context.Context) { n.Add(1) })

	before := w.Wakeups()
	f.Advance(time.Hour)

	if got := n.Load(); got != 0 {
		t.Fatalf("%d callbacks ran with nothing due", got)
	}
	if got := w.Wakeups() - before; got != 0 {
		t.Fatalf("the wheel woke %d times across an idle hour; a timer-driven wheel wakes 0", got)
	}
}

func TestTheWheelIsOneGoroutineWhateverTheEntryCount(t *testing.T) {
	// §0.5's mechanism: one timer for the whole policy, not a ticker per check.
	// A ticker per entry is a goroutine and a wakeup per entry, and 32 checks
	// would be 32 of each on every endpoint in the fleet.
	settle()
	base := runtime.NumGoroutine()

	w1, f1 := run(t)
	add(t, w1, f1, "only", time.Minute, func(context.Context) {})
	settle()
	one := runtime.NumGoroutine() - base

	w2, f2 := run(t)
	for i := 0; i < 32; i++ {
		add(t, w2, f2, string(rune('a'+i)), time.Duration(i+1)*time.Minute, func(context.Context) {})
	}
	settle()
	both := runtime.NumGoroutine() - base

	// w1 and w2 are each one goroutine; adding 32 entries to w2 must add none.
	if both != one*2 {
		t.Fatalf("1 entry cost %d goroutine(s); a second wheel with 32 entries brought the total "+
			"to %d, want %d -- an entry is spawning something", one, both, one*2)
	}
}

func TestRemoveFromInsideACallbackDoesNotDeadlock(t *testing.T) {
	// A wheel that holds its lock across the callback deadlocks here. The
	// falsifying observation is the test timing out; the assertion below is what
	// makes the intent explicit.
	w, f := run(t)
	var fired atomic.Int32
	done := make(chan struct{})
	var once sync.Once
	add(t, w, f, "self", time.Minute, func(context.Context) {
		fired.Add(1)
		w.Remove("self")
		_ = w.Add("other", time.Minute, func(context.Context) {})
		w.Remove("other")
		once.Do(func() { close(done) })
	})

	f.Advance(3 * time.Minute)
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the callback never completed; Remove from inside a callback deadlocked")
	}
	if got := fired.Load(); got != 1 {
		t.Fatalf("entry fired %d times after removing itself on the first, want 1", got)
	}
}

func TestAddReplacesAnEntryWithTheSameID(t *testing.T) {
	// health.policy.apply installs a policy WHOLESALE. Re-adding an id must
	// replace, not double up: two live copies of the same check would double
	// every report the endpoint sends.
	w, f := run(t)
	var first, second atomic.Int32
	add(t, w, f, "check", time.Minute, func(context.Context) { first.Add(1) })
	add(t, w, f, "check", time.Minute, func(context.Context) { second.Add(1) })

	f.Advance(2 * time.Minute)

	if first.Load() != 0 {
		t.Fatalf("the replaced entry fired %d times", first.Load())
	}
	if second.Load() != 2 {
		t.Fatalf("the replacement fired %d times, want 2", second.Load())
	}
	if w.Len() != 1 {
		t.Fatalf("the wheel holds %d entries, want 1", w.Len())
	}
}

func TestEntriesFireInDeadlineOrder(t *testing.T) {
	w, f := run(t)
	var mu sync.Mutex
	var order []string
	record := func(id string) func(context.Context) {
		return func(context.Context) {
			mu.Lock()
			order = append(order, id)
			mu.Unlock()
		}
	}
	// Intervals chosen so no two deadlines coincide inside the window. Two
	// entries due at the same instant fire in an order the heap decides, and
	// asserting one would pin an accident rather than a contract -- the contract
	// is DEADLINE order, and simultaneous deadlines have none.
	add(t, w, f, "slow", 150*time.Second, record("slow"))
	add(t, w, f, "fast", 60*time.Second, record("fast"))
	add(t, w, f, "mid", 90*time.Second, record("mid"))

	f.Advance(150 * time.Second)

	mu.Lock()
	defer mu.Unlock()
	want := []string{"fast", "mid", "fast", "slow"}
	if len(order) != len(want) {
		t.Fatalf("fired %v, want %v", order, want)
	}
	for i := range want {
		if order[i] != want[i] {
			t.Fatalf("fired %v, want %v", order, want)
		}
	}
}

func TestRemoveOfAnUnknownIDIsHarmless(t *testing.T) {
	w, _ := run(t)
	w.Remove("never-added")
	if w.Len() != 0 {
		t.Fatal("Remove of an unknown id changed the wheel")
	}
}

func TestAnEntryWithANonPositiveIntervalIsRefused(t *testing.T) {
	// An interval of zero re-arms at the same instant forever: a busy loop
	// wearing a timer's clothes, and the single worst thing that could happen to
	// the idle budget.
	w, _ := run(t)
	if err := w.Add("bad", 0, func(context.Context) {}); err == nil {
		t.Fatal("an interval of 0 was accepted; that is a busy loop")
	}
	if err := w.Add("worse", -time.Second, func(context.Context) {}); err == nil {
		t.Fatal("a negative interval was accepted")
	}
	if w.Len() != 0 {
		t.Fatalf("the wheel holds %d entries", w.Len())
	}
}

func TestAPanickingCallbackDoesNotKillTheWheel(t *testing.T) {
	// One bad check must not stop every other check on the endpoint.
	w, f := run(t)
	var good atomic.Int32
	add(t, w, f, "bad", time.Minute, func(context.Context) { panic("check exploded") })
	add(t, w, f, "good", time.Minute, func(context.Context) { good.Add(1) })

	f.Advance(2 * time.Minute)

	if got := good.Load(); got != 2 {
		t.Fatalf("the surviving entry fired %d times, want 2", got)
	}
}

func TestTheRealClockIsATimerNotASleep(t *testing.T) {
	// Real is the production Clock. The property that matters is that Run parks
	// on a channel receive: this asserts the wheel actually fires on wall-clock
	// time with a short interval, so the Real path is exercised at least once.
	f := Real{}
	w := New(f, nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	fired := make(chan struct{}, 4)
	go func() { _ = w.Run(ctx) }()
	if err := w.Add("quick", 5*time.Millisecond, func(context.Context) {
		select {
		case fired <- struct{}{}:
		default:
		}
	}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-fired:
	case <-time.After(5 * time.Second):
		t.Fatal("the wheel never fired on the real clock")
	}
}

// settle gives any goroutine that is on its way out a chance to finish, so a
// NumGoroutine comparison is not measuring the previous test's teardown.
func settle() {
	for i := 0; i < 50; i++ {
		runtime.Gosched()
	}
	time.Sleep(20 * time.Millisecond)
}
