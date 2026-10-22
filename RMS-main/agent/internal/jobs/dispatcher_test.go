package jobs

import (
	"context"
	"encoding/json"
	"errors"
	"runtime"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// fakeJob: a VerifiedJob test double
//
// VerifiedJob carries an unexported method (verifiedByTheSignedJobRunner, see
// verified.go) precisely so nothing outside internal/jobs can construct one --
// construct_test.go enforces that with a build that must fail. This file is
// package jobs, not jobs_test, so implementing that method here is legitimate:
// it is the same trust boundary every other in-package test relies on, not a
// hole in it.
// ---------------------------------------------------------------------------

// testVerifiedJob is dispatcher_test.go's own VerifiedJob implementation. It
// exists only for dispatcher tests, which need a VerifiedJob whose Type()
// drives capabilityKey but otherwise carry no meaningful payload.
type testVerifiedJob struct {
	jobType string
}

func (j *testVerifiedJob) ID() string                  { return "fake-" + j.jobType }
func (j *testVerifiedJob) Type() string                { return j.jobType }
func (j *testVerifiedJob) Device() string              { return testDeviceID }
func (j *testVerifiedJob) Params() json.RawMessage     { return json.RawMessage(`{}`) }
func (j *testVerifiedJob) IssuedAt() time.Time         { return time.Now() }
func (j *testVerifiedJob) ExpiresAt() time.Time        { return time.Now().Add(time.Hour) }
func (j *testVerifiedJob) Nonce() string               { return "nonce-" + j.jobType }
func (*testVerifiedJob) verifiedByTheSignedJobRunner() {}

// fakeJob builds a VerifiedJob for dispatcher tests, keyed only by jobType
// (which is all Dispatcher ever looks at: capabilityKey(job.Type())).
func fakeJob(jobType string) VerifiedJob {
	return &testVerifiedJob{jobType: jobType}
}

// newTestDispatcher builds a Dispatcher or fails the test immediately.
//
// BaseContext defaults to t.Context() -- the test's own lifetime, which is the
// unit-test stand-in for the process context main.go passes. It is required by
// NewDispatcher precisely so no caller can silently end up with a
// connection-scoped one; supplying it here keeps that requirement real without
// making every test restate it.
func newTestDispatcher(t *testing.T, o DispatcherOptions) *Dispatcher {
	t.Helper()
	if o.BaseContext == nil {
		o.BaseContext = t.Context()
	}
	d, err := NewDispatcher(o)
	if err != nil {
		t.Fatalf("NewDispatcher: %v", err)
	}
	return d
}

// drain waits until every capability queue has been fully worked and its
// worker has exited (d.queues empty), or fails the test after timeout.
//
// It reads d.queues under d.mu -- the exact lock Submit and worker use -- so
// an empty map here is a genuine observation that no worker is mid-flight,
// not a guess based on timing.
func (d *Dispatcher) drain(t *testing.T, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for {
		d.mu.Lock()
		n := len(d.queues)
		d.mu.Unlock()
		if n == 0 {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("dispatcher did not drain within %s (%d capability queue(s) still tracked)", timeout, n)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// waitForPending polls d.Pending(key) until it reaches want, or fails the
// test after timeout. Used where a test needs a queue at a specific depth
// before its next Submit, rather than assuming a goroutine scheduling race
// resolves a particular way.
func waitForPending(t *testing.T, d *Dispatcher, key string, want int, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for {
		if got := d.Pending(key); got == want {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("Pending(%q) did not reach %d within %s (last = %d)", key, want, timeout, d.Pending(key))
		}
		time.Sleep(1 * time.Millisecond)
	}
}

func TestTwoJobsOfOneCapabilityNeverOverlap(t *testing.T) {
	var mu sync.Mutex
	var active, maxActive int
	d := newTestDispatcher(t, DispatcherOptions{
		QueueDepth: 8,
		Exec: func(ctx context.Context, j VerifiedJob, h Handler) {
			mu.Lock()
			active++
			if active > maxActive {
				maxActive = active
			}
			mu.Unlock()
			time.Sleep(20 * time.Millisecond)
			mu.Lock()
			active--
			mu.Unlock()
		},
	})

	var wg sync.WaitGroup
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _ = d.Submit(fakeJob("backup.run"), nil) }()
	}
	wg.Wait()
	d.drain(t, 5*time.Second)

	mu.Lock()
	defer mu.Unlock()
	if maxActive != 1 {
		t.Fatalf("max concurrent backup jobs = %d, want 1", maxActive)
	}
}

func TestDifferentCapabilitiesRunConcurrently(t *testing.T) {
	started := make(chan string, 2)
	release := make(chan struct{})
	// closeRelease is idempotent: the deferred call protects an early
	// t.Fatalf (which would otherwise leave both Exec goroutines blocked on
	// <-release for the rest of the binary), and the explicit call on the
	// success path unblocks them before drain -- without double-closing.
	closeRelease := sync.OnceFunc(func() { close(release) })
	defer closeRelease()

	d := newTestDispatcher(t, DispatcherOptions{
		QueueDepth: 8,
		Exec: func(ctx context.Context, j VerifiedJob, h Handler) {
			started <- capabilityKey(j.Type())
			<-release
		},
	})

	_ = d.Submit(fakeJob("patch.apply"), nil)
	_ = d.Submit(fakeJob("inventory.collect"), nil)

	seen := map[string]bool{}
	for i := 0; i < 2; i++ {
		select {
		case k := <-started:
			seen[k] = true
		case <-time.After(2 * time.Second):
			t.Fatalf("only %d capabilities started; want both running at once", len(seen))
		}
	}

	if !seen["patch"] || !seen["inventory"] {
		t.Fatalf("started = %v, want both patch and inventory", seen)
	}

	// Unblock both workers and wait for them to exit before returning, so a
	// goroutine-count assertion elsewhere in this file starts from a clean
	// baseline rather than racing this test's own workers.
	closeRelease()
	d.drain(t, 5*time.Second)
}

// TestNoWorkerGoroutineExistsUntilAJobArrives pins both halves of the idle
// budget: no worker exists before there is work, AND every worker actually
// exits once its queue drains.
//
// One submitted job is not enough to test the second half: with a single
// capability, a worker that leaked (looped forever instead of returning)
// would settle the goroutine count at exactly base+1, which the old
// tolerance treated as success. Three DISTINCT capability keys, held open
// until every worker has started, force the live count to base+3 -- a
// leaked worker then keeps the post-drain count above base+1 too, so the
// assertion can no longer be satisfied by a spin loop that never returns.
func TestNoWorkerGoroutineExistsUntilAJobArrives(t *testing.T) {
	base := runtime.NumGoroutine()

	keys := []string{"alpha.run", "beta.run", "gamma.run"}
	started := make(chan struct{}, len(keys))
	hold := make(chan struct{})
	d := newTestDispatcher(t, DispatcherOptions{
		QueueDepth: 8,
		Exec: func(context.Context, VerifiedJob, Handler) {
			started <- struct{}{}
			<-hold
		},
	})

	if got := runtime.NumGoroutine(); got > base+1 {
		t.Fatalf("goroutines after NewDispatcher = %d, base = %d; a dispatcher with no work must start none", got, base)
	}

	for _, k := range keys {
		if err := d.Submit(fakeJob(k), nil); err != nil {
			t.Fatalf("Submit(%s): %v", k, err)
		}
	}

	// Wait for all three distinct-capability workers to actually be running
	// (blocked in Exec on <-hold) before reading the goroutine count, so the
	// rise assertion below is not racing worker startup.
	for i := range keys {
		select {
		case <-started:
		case <-time.After(2 * time.Second):
			t.Fatalf("only %d of %d workers had started", i, len(keys))
		}
	}

	if got := runtime.NumGoroutine(); got < base+len(keys) {
		t.Fatalf("goroutines while %d capabilities are held open = %d, base = %d; want at least base+%d "+
			"(one worker per distinct capability key)", len(keys), got, base, len(keys))
	}

	close(hold)
	d.drain(t, 5*time.Second)

	deadline := time.Now().Add(2 * time.Second)
	for runtime.NumGoroutine() > base+1 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := runtime.NumGoroutine(); got > base+1 {
		t.Fatalf("goroutines after drain = %d, base = %d; workers must exit when their queue empties", got, base)
	}
}

// TestABurstOfSubmitsLosesNoJobAcrossWorkerRestarts exercises the exit/spawn
// window directly: worker's exit decision (delete from d.queues, return) and
// Submit's lookup-or-create must be mutually exclusive, or a Submit that
// observes the queue still in the map can hand work to a worker already past
// its last read -- and the job vanishes with no error anywhere.
//
// The burst is submitted with NO pause between iterations, on purpose.
// QueueDepth=8 combined with a near-instant Exec means a tight loop of
// Submits outruns both the buffered channel and the worker's respawn cycle:
// the single worker repeatedly empties the queue and takes the empty-queue
// branch while more Submits are still landing, which is the only place the
// spawn/exit window lives.
//
// Measured 2026-08-09: moving the exit path in dispatcher.go's worker out
// from under d.mu (the bug this test exists to catch) failed 20/20 runs at
// -race -count=20. Losses ranged from a single job to 18, varying with
// scheduler timing -- the size of the loss is not the signal, ran !=
// accepted is.
func TestABurstOfSubmitsLosesNoJobAcrossWorkerRestarts(t *testing.T) {
	const n = 500
	var ran atomic.Int64
	d := newTestDispatcher(t, DispatcherOptions{
		QueueDepth: 8,
		Exec: func(context.Context, VerifiedJob, Handler) {
			ran.Add(1)
		},
	})

	accepted := 0
	for i := 0; i < n; i++ {
		if err := d.Submit(fakeJob("hello"), nil); err == nil {
			accepted++
		}
	}
	d.drain(t, 10*time.Second)

	if got := int(ran.Load()); got != accepted {
		t.Fatalf("ran %d of %d accepted submits; every accepted job must run exactly once", got, accepted)
	}
}

func TestOnlyTwoHeavyCapabilitiesRunAtOnce(t *testing.T) {
	var mu sync.Mutex
	var active, maxActive int
	release := make(chan struct{})
	// This HeavyKeys map, and "deploy.script" below, are this test's OWN
	// fixture data, deliberately decoupled from the real registry -- this is
	// a generic Dispatcher unit test and has no capabilities.Registry to
	// check against. Note the drift on purpose: DefaultHeavyKeys (runner.go)
	// is the real, shipped map, and it does NOT contain "deploy" -- it was
	// corrected to "app"/"script" after TestEveryHeavyKeyNamesARegisteredCapability
	// (registry_test.go) found that "deploy" never matched either real deploy
	// job type (app.deploy, script.run). This file's "deploy"/"deploy.script"
	// stay as a synthetic third heavy key/type; a reader diffing the two maps
	// should reach for DefaultHeavyKeys as the one that must match production,
	// not this one.
	heavyStarted := make(chan struct{}, 3)
	d := newTestDispatcher(t, DispatcherOptions{
		QueueDepth:  8,
		HeavyKeys:   map[string]bool{"backup": true, "patch": true, "deploy": true},
		HeavyBudget: 2,
		Exec: func(context.Context, VerifiedJob, Handler) {
			mu.Lock()
			active++
			if active > maxActive {
				maxActive = active
			}
			mu.Unlock()
			heavyStarted <- struct{}{}
			<-release
			mu.Lock()
			active--
			mu.Unlock()
		},
	})

	for _, jt := range []string{"backup.run", "patch.apply"} {
		if err := d.Submit(fakeJob(jt), nil); err != nil {
			t.Fatalf("Submit(%s) = %v", jt, err)
		}
	}
	// Both slots must be provably HELD before the third job is submitted.
	// heavyStarted is signalled from inside Exec, which a heavy capability
	// only reaches after exec1 has acquired its semaphore slot.
	for i := 0; i < 2; i++ {
		select {
		case <-heavyStarted:
		case <-time.After(2 * time.Second):
			t.Fatalf("only %d of 2 heavy jobs had acquired a semaphore slot", i)
		}
	}

	if err := d.Submit(fakeJob("deploy.script"), nil); err != nil {
		t.Fatalf("Submit(deploy.script) = %v", err)
	}
	// Pending("deploy") reaching 0 means the third worker has DEQUEUED its job
	// and is inside exec1 -- which, with the budget full, can only be the
	// semaphore acquire. Only then does maxActive mean anything.
	//
	// This replaces a 200ms sleep, which could read maxActive == 2 on a loaded
	// runner where only two workers had started yet, and therefore passed with
	// the semaphore acquire REMOVED ENTIRELY. Same shape 392db12 fixed in
	// TestALightCapabilityIsNotHeldByTheHeavyBudget. Falsified: with the
	// acquire removed this now fails 20/20 at -count=20.
	waitForPending(t, d, "deploy", 0, 2*time.Second)

	mu.Lock()
	got := maxActive
	mu.Unlock()
	close(release)
	d.drain(t, 5*time.Second)

	if got != 2 {
		t.Fatalf("max concurrent heavy jobs = %d, want 2", got)
	}
}

func TestALightCapabilityIsNotHeldByTheHeavyBudget(t *testing.T) {
	release := make(chan struct{})
	closeRelease := sync.OnceFunc(func() { close(release) })
	defer closeRelease()

	ran := make(chan string, 4)
	// heavyStarted is signalled from inside Exec, which for a heavy capability
	// only runs after exec1 has already acquired its semaphore slot. Waiting
	// for BOTH signals before submitting inventory proves the heavy budget
	// (2/2) is genuinely saturated when inventory runs -- not merely that
	// inventory happened to win a race against two unsynchronized heavy
	// Submits. Without this barrier a regression that routed every
	// capability through the semaphore could still let inventory grab a
	// slot on a meaningful fraction of runs and pass by accident.
	heavyStarted := make(chan struct{}, 2)
	d := newTestDispatcher(t, DispatcherOptions{
		QueueDepth:  8,
		HeavyKeys:   map[string]bool{"backup": true, "patch": true},
		HeavyBudget: 2,
		Exec: func(ctx context.Context, j VerifiedJob, h Handler) {
			if capabilityKey(j.Type()) == "inventory" {
				ran <- "inventory"
				return
			}
			heavyStarted <- struct{}{}
			ran <- "heavy"
			<-release
		},
	})

	_ = d.Submit(fakeJob("backup.run"), nil)
	_ = d.Submit(fakeJob("patch.apply"), nil)

	for i := 0; i < 2; i++ {
		select {
		case <-heavyStarted:
		case <-time.After(2 * time.Second):
			t.Fatalf("only %d of 2 heavy jobs had acquired a semaphore slot", i)
		}
	}

	_ = d.Submit(fakeJob("inventory.collect"), nil)

	deadline := time.After(2 * time.Second)
	for i := 0; i < 3; i++ {
		select {
		case got := <-ran:
			if got == "inventory" {
				closeRelease()
				d.drain(t, 5*time.Second)
				return // inventory ran while both heavy slots were held
			}
		case <-deadline:
			closeRelease()
			d.drain(t, 5*time.Second)
			t.Fatal("inventory.collect never ran while the heavy budget was full")
		}
	}
	closeRelease()
	d.drain(t, 5*time.Second)
	t.Fatal("inventory.collect never ran while the heavy budget was full")
}

func TestAFullQueueIsRefusedRatherThanDropped(t *testing.T) {
	release := make(chan struct{})
	// closeRelease is guarded so it fires exactly once on every path,
	// including a t.Fatalf inside waitForPending below: Fatalf triggers
	// runtime.Goexit(), which unwinds this goroutine through its deferred
	// calls without ever reaching the unconditional close() further down --
	// leaving the backup worker permanently blocked on <-release otherwise.
	// runtime.NumGoroutine() is process-global (TestNoWorkerGoroutineExistsUntilAJobArrives
	// reads it as a baseline), so that leak is not local to this test.
	closeRelease := sync.OnceFunc(func() { close(release) })
	defer closeRelease()

	d := newTestDispatcher(t, DispatcherOptions{
		QueueDepth: 2,
		Exec:       func(context.Context, VerifiedJob, Handler) { <-release },
	})

	// One job occupies the worker; two more fill the queue. The first Submit
	// must be picked up by the worker (and the worker blocked in Exec on
	// <-release) before the next two land, or they may not both fit in the
	// depth-2 buffer -- so wait on Pending("backup") dropping back to 0
	// rather than assume the worker won the race against this goroutine.
	if err := d.Submit(fakeJob("backup.run"), nil); err != nil {
		t.Fatalf("Submit 0 = %v, want nil", err)
	}
	waitForPending(t, d, "backup", 0, 2*time.Second)

	for i := 1; i < 3; i++ {
		if err := d.Submit(fakeJob("backup.run"), nil); err != nil {
			t.Fatalf("Submit %d = %v, want nil", i, err)
		}
	}
	waitForPending(t, d, "backup", 2, 2*time.Second)

	err := d.Submit(fakeJob("backup.run"), nil)
	closeRelease()
	d.drain(t, 5*time.Second)

	if !errors.Is(err, ErrQueueFull) {
		t.Fatalf("Submit on a full queue = %v, want ErrQueueFull", err)
	}
}
