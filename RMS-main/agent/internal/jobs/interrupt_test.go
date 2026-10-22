package jobs

import (
	"context"
	"log/slog"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// The endpoint half of cycle 2 of
// docs/superpowers/specs/2026-08-22-job-lifecycle-design.md, §7 tests 14-18.
//
// Every negative case here asserts on a FRAME the runner sent or on the spy's
// invocation counter, never on an error return. §7 test 16 says why in one
// line: "the silence in this ordering is the failure mode, so the test asserts
// the message, not the suppression".

// blockingCapability parks until its context is cancelled, then reports which
// happened. It is how "the agent asked the capability to stop" becomes an
// observation rather than a claim.
type blockingCapability struct {
	started  chan string
	mu       sync.Mutex
	ctxErrs  []error
	finished chan struct{}
}

func newBlocking() *blockingCapability {
	return &blockingCapability{
		started:  make(chan string, 8),
		finished: make(chan struct{}, 8),
	}
}

func (b *blockingCapability) handle(ctx context.Context, j VerifiedJob) (any, error) {
	b.started <- j.ID()
	<-ctx.Done()
	b.mu.Lock()
	b.ctxErrs = append(b.ctxErrs, ctx.Err())
	b.mu.Unlock()
	b.finished <- struct{}{}
	return nil, ctx.Err()
}

func (b *blockingCapability) awaitStart(t *testing.T, d time.Duration) string {
	t.Helper()
	select {
	case id := <-b.started:
		return id
	case <-time.After(d):
		t.Fatalf("the capability never started within %s", d)
		return ""
	}
}

func (b *blockingCapability) awaitFinish(t *testing.T, d time.Duration) {
	t.Helper()
	select {
	case <-b.finished:
	case <-time.After(d):
		t.Fatalf("the capability was never released within %s", d)
	}
}

// interruptHarness is newHarness with a capability that blocks on its context,
// so an interrupt is observable.
type interruptHarness struct {
	block  *blockingCapability
	sender *fakeSender
	runner *Runner
	logs   *logBuf
}

func newInterruptHarness(t *testing.T, s signer) *interruptHarness {
	t.Helper()
	block := newBlocking()
	sender := &fakeSender{}
	logs := &logBuf{}
	r, err := New(Options{
		BaseContext: t.Context(),
		DeviceID:    testDeviceID,
		SigningKey:  s.pub,
		Nonces:      newMemNonces(),
		Registry:    mapRegistry{"hello": block.handle, "script.run": block.handle},
		Sender:      sender,
		Now:         func() time.Time { return baseNow },
		Logger:      slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug})),
	})
	if err != nil {
		t.Fatal(err)
	}
	return &interruptHarness{block: block, sender: sender, runner: r, logs: logs}
}

// jobWithID is validJob with a caller-chosen id, so a test can name the target
// of a cancel.
//
// `expires_at` IS A REAL FUTURE INSTANT, not baseNow+10m, and the difference is
// load-bearing here in a way it is not for the verification tests. Verification
// runs against the injected clock (`Now: baseNow`), but `Runner.run` derives
// the job's context deadline from `ExpiresAt()` against the REAL clock -- so a
// fixture whose expiry is a date in the past hands the capability a context
// that is already `DeadlineExceeded`, and every interruption assertion below
// would pass for the wrong reason.
func jobWithID(id, nonce string) string {
	return jobText(
		id,
		"hello",
		testDeviceID,
		`{"message":"hello from the control plane"}`,
		ts(baseNow.Add(-time.Second)),
		ts(time.Now().Add(10*time.Minute)),
		nonce,
	)
}

// --------------------------------------------------------------------------- //
// §7 test 14 -- a cancel for a RUNNING job cancels its context.
// --------------------------------------------------------------------------- //

func TestInterruptingARunningJobCancelsItsContextAndSaysSo(t *testing.T) {
	s := newSigner(t)
	h := newInterruptHarness(t, s)
	const id = "9c3f0a11-0000-4000-8000-0000000000aa"

	h.runner.Handle(context.Background(), s.frame(t, jobWithID(id, "n-run")))
	if got := h.block.awaitStart(t, 2*time.Second); got != id {
		t.Fatalf("started %q, want %q", got, id)
	}

	if outcome := h.runner.Interrupt(id); outcome != OutcomeInterrupted {
		t.Fatalf("Interrupt = %q, want %q", outcome, OutcomeInterrupted)
	}

	// FALSIFYING OBSERVATION: the capability is still parked. A runner that
	// recorded the cancel but never called the CancelFunc would return
	// "interrupted" and stop nothing.
	h.block.awaitFinish(t, 2*time.Second)
	h.block.mu.Lock()
	errs := append([]error(nil), h.block.ctxErrs...)
	h.block.mu.Unlock()
	if len(errs) != 1 || errs[0] != context.Canceled {
		t.Fatalf("capability saw ctx.Err() = %v; want exactly one context.Canceled", errs)
	}

	// The TARGET reports its own terminal state -- the cancel result says what
	// was asked, the target's result says what happened.
	res := h.sender.only(t, "job_result")
	if res["job_id"] != id {
		t.Fatalf("job_result is for %v, want %s", res["job_id"], id)
	}
	if res["state"] != "failed" {
		t.Fatalf("target reported %v; a cancelled capability returns its own error", res["state"])
	}
}

func TestARunningJobIsForgottenWhenItFinishes(t *testing.T) {
	// The `running` map is bounded by concurrency and not by fleet history, and
	// this is the assertion behind that sentence: an entry per job that has
	// EVER run would be an unbounded map on an endpoint that must be
	// near-invisible.
	s := newSigner(t)
	h := newInterruptHarness(t, s)
	const id = "9c3f0a11-0000-4000-8000-0000000000ab"

	h.runner.Handle(context.Background(), s.frame(t, jobWithID(id, "n-forget")))
	h.block.awaitStart(t, 2*time.Second)
	h.runner.Interrupt(id)
	h.block.awaitFinish(t, 2*time.Second)
	h.sender.only(t, "job_result")

	deadline := time.Now().Add(2 * time.Second)
	for {
		h.runner.mu.Lock()
		n := len(h.runner.running)
		h.runner.mu.Unlock()
		if n == 0 {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("the running map still holds %d entries after the job finished", n)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// --------------------------------------------------------------------------- //
// §7 test 15 -- a cancel for a job this agent has never seen.
// --------------------------------------------------------------------------- //

func TestInterruptingAnUnknownJobReportsUnknownAndTouchesNothing(t *testing.T) {
	s := newSigner(t)
	h := newInterruptHarness(t, s)

	if outcome := h.runner.Interrupt("9c3f0a11-0000-4000-8000-0000000000ff"); outcome != OutcomeUnknown {
		t.Fatalf("Interrupt = %q, want %q", outcome, OutcomeUnknown)
	}
	if frames := h.sender.frames(); len(frames) != 0 {
		t.Fatalf("an unknown cancel sent %d frame(s); it must touch nothing: %v", len(frames), frames)
	}
}

// --------------------------------------------------------------------------- //
// §7 test 16 -- the ordering race. THE SILENCE IS THE FAILURE MODE.
// --------------------------------------------------------------------------- //

func TestACancelArrivingBeforeItsTargetSuppressesItAndStillAnswers(t *testing.T) {
	s := newSigner(t)
	h := newInterruptHarness(t, s)
	const id = "9c3f0a11-0000-4000-8000-0000000000ba"

	// The cancel wins the race: this agent has never heard of the job.
	if outcome := h.runner.Interrupt(id); outcome != OutcomeUnknown {
		t.Fatalf("Interrupt = %q, want %q", outcome, OutcomeUnknown)
	}

	// The target arrives afterwards, perfectly valid and correctly signed.
	h.runner.Handle(context.Background(), s.frame(t, jobWithID(id, "n-race")))

	// THE ASSERTION IS THE MESSAGE, NOT THE SUPPRESSION. Without a frame here
	// the job hangs until `expires_at` and the operator sees `expired` for a
	// job they cancelled -- which is the exact defect this design exists to
	// remove, and it is invisible to a test that only checks the capability did
	// not run.
	res := h.sender.only(t, "job_result")
	if res["job_id"] != id {
		t.Fatalf("job_result is for %v, want %s", res["job_id"], id)
	}
	if res["state"] != "refused" {
		t.Fatalf("state = %v; want refused, the state JobResultMsg already carries", res["state"])
	}
	reason := reasonOf(t, res)
	if !hasPrefix(reason, string(ReasonCancelledBeforeStart)) {
		t.Fatalf("reason = %q; the server reads the HEAD of this for `dropped` vs "+
			"`interrupted` (backend/app/jobs/cancel.py::is_agent_cancellation)", reason)
	}

	// And the capability never ran.
	select {
	case id := <-h.block.started:
		t.Fatalf("the suppressed capability RAN for %s", id)
	case <-time.After(200 * time.Millisecond):
	}

	// No `job_status: running` either: a job that was never invoked must not
	// be reported as executing.
	for _, f := range h.sender.frames() {
		if f["kind"] == "job_status" {
			t.Fatalf("a suppressed job reported %v; it never started", f)
		}
	}
}

func TestASuppressedJobStaysSuppressedOnRedelivery(t *testing.T) {
	// The gateway drains a device's queue on EVERY reconnect, so the same
	// envelope can arrive twice. A membership test that consumed its entry
	// would let the second delivery execute a job an operator has cancelled.
	s := newSigner(t)
	h := newInterruptHarness(t, s)
	const id = "9c3f0a11-0000-4000-8000-0000000000bb"

	h.runner.Interrupt(id)
	h.runner.Handle(context.Background(), s.frame(t, jobWithID(id, "n-first")))
	h.sender.awaitAtLeast(t, 1, 2*time.Second)
	h.runner.Handle(context.Background(), s.frame(t, jobWithID(id, "n-second")))

	frames := h.sender.awaitKindCount(t, "job_result", 2, 2*time.Second)
	for _, f := range frames {
		if f["state"] != "refused" {
			t.Fatalf("a re-delivered cancelled job reported %v; want refused", f["state"])
		}
	}
	select {
	case ran := <-h.block.started:
		t.Fatalf("the re-delivered cancelled capability RAN for %s", ran)
	case <-time.After(200 * time.Millisecond):
	}
}

// --------------------------------------------------------------------------- //
// §7 test 17 -- a cancel never waits on the heavy budget.
// --------------------------------------------------------------------------- //

func TestJobCancelHasItsOwnQueueAndIsNotHeavy(t *testing.T) {
	if got := capabilityKey("job.cancel"); got != "job" {
		t.Fatalf("capabilityKey(\"job.cancel\") = %q, want \"job\" -- the type is named "+
			"`job.cancel` precisely so a cancel gets a queue of its own", got)
	}
	if DefaultHeavyKeys()["job"] {
		t.Fatal("\"job\" is in DefaultHeavyKeys: a cancel would wait on the heavy " +
			"semaphore, i.e. queue behind the heavy job it is cancelling")
	}
	// NOT VACUOUS: the keys that ARE heavy are still heavy, so this cannot pass
	// against an empty map.
	for _, key := range []string{"backup", "patch", "app", "script"} {
		if !DefaultHeavyKeys()[key] {
			t.Fatalf("%q is no longer a heavy key; this test has stopped meaning anything", key)
		}
	}
}

func TestACancelExecutesWhileTheHeavyBudgetIsFull(t *testing.T) {
	// The falsifier from §7's table: "a `job.cancel` whose execution waits on
	// the heavy semaphore". Two heavy jobs are parked, filling a budget of two,
	// and a cancel must still run.
	base := t.Context()
	heavy := newBlocking()
	cancelRan := make(chan struct{}, 1)
	d, err := NewDispatcher(DispatcherOptions{
		BaseContext: base,
		QueueDepth:  4,
		HeavyBudget: 2,
		HeavyKeys:   DefaultHeavyKeys(),
		Exec: func(ctx context.Context, j VerifiedJob, h Handler) {
			_, _ = h(ctx, j)
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, jobType := range []string{"backup.run", "patch.apply"} {
		if err := d.Submit(fakeJob(jobType), heavy.handle); err != nil {
			t.Fatal(err)
		}
	}
	heavy.awaitStart(t, 2*time.Second)
	heavy.awaitStart(t, 2*time.Second)

	err = d.Submit(fakeJob("job.cancel"), func(context.Context, VerifiedJob) (any, error) {
		cancelRan <- struct{}{}
		return nil, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-cancelRan:
	case <-time.After(2 * time.Second):
		t.Fatal("the cancel never executed while the heavy budget was full")
	}
}

// --------------------------------------------------------------------------- //
// §7 test 18 -- the bounded set.
// --------------------------------------------------------------------------- //

func TestTheCancelledSetEvictsAtItsCapAndNeverGrows(t *testing.T) {
	c := newCancelledSet()
	for i := 0; i < cancelledSetCap*4; i++ {
		c.add("job-" + strconv.Itoa(i))
		if n := c.len(); n > cancelledSetCap {
			t.Fatalf("the set holds %d entries after %d adds; cap is %d", n, i+1, cancelledSetCap)
		}
	}
	if c.len() != cancelledSetCap {
		t.Fatalf("the set holds %d entries; want exactly the cap %d", c.len(), cancelledSetCap)
	}
	// The OLDEST is what went. An eviction policy that dropped the newest would
	// pass the size check above and lose exactly the cancel that just arrived.
	if c.has("job-0") {
		t.Fatal("the oldest entry survived; eviction is not FIFO")
	}
	if !c.has("job-" + strconv.Itoa(cancelledSetCap*4-1)) {
		t.Fatal("the newest entry was evicted")
	}
	// Re-adding an id already present must not consume a slot.
	before := c.len()
	c.add("job-" + strconv.Itoa(cancelledSetCap*4-1))
	if c.len() != before {
		t.Fatalf("re-adding a known id changed the size from %d to %d", before, c.len())
	}
}

// --------------------------------------------------------------------------- //
// The race the single critical section closes.
// --------------------------------------------------------------------------- //

func TestABeginThatLosesToAnInterruptDoesNotRun(t *testing.T) {
	// `begin` checks suppression and registers the CancelFunc under ONE lock.
	// Split into two acquisitions there is a window in which the job is
	// neither suppressed nor cancellable, and the cancel is lost in silence.
	// Driven directly because the window is too narrow to hit reliably through
	// the dispatcher.
	s := newSigner(t)
	h := newInterruptHarness(t, s)

	if got := h.runner.Interrupt("j-1"); got != OutcomeUnknown {
		t.Fatalf("Interrupt = %q, want %q", got, OutcomeUnknown)
	}
	_, cancel := context.WithCancel(context.Background())
	defer cancel()
	if h.runner.begin("j-1", cancel) {
		t.Fatal("begin admitted a job that had already been cancelled")
	}
	h.runner.mu.Lock()
	_, tracked := h.runner.running["j-1"]
	h.runner.mu.Unlock()
	if tracked {
		t.Fatal("a refused begin still registered the job as running")
	}
	// NOT VACUOUS: a job nobody cancelled is admitted and tracked.
	if !h.runner.begin("j-2", cancel) {
		t.Fatal("begin refused a job nobody had cancelled")
	}
}

func TestInterruptIsSafeUnderConcurrentJobs(t *testing.T) {
	// Run with -race. The whole cancellation state is one mutex; a reader that
	// escaped it would show up here and nowhere else.
	s := newSigner(t)
	h := newInterruptHarness(t, s)
	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			h.runner.Interrupt("j-" + strconv.Itoa(i%8))
		}(i)
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, cancel := context.WithCancel(context.Background())
			defer cancel()
			if h.runner.begin("k-"+strconv.Itoa(i), cancel) {
				h.runner.finish("k-" + strconv.Itoa(i))
			}
		}(i)
	}
	wg.Wait()
}

// --------------------------------------------------------------------------- //
// The idle budget (CLAUDE.md; design §7 "Idle budget").
// --------------------------------------------------------------------------- //

func TestFalsifier_CancellationStateStartsNoGoroutineAndDoesNotGrow(t *testing.T) {
	// THE TWO FALSIFIERS, NAMED IN THE DESIGN: "the existing idle benchmark
	// shows any wake-up, or any resident-memory growth proportional to jobs
	// seen, after N cancel cycles."
	//
	// This is a `testing.Benchmark` driven from an ORDINARY Test and COMPARED,
	// never a Benchmark function: `go test -bench` EXITS 0 ON A REGRESSION,
	// measured on this project (internal/budget's package comment records a
	// planted 64 KiB/op regression printing `65536 B/op` and still exiting 0).
	s := newSigner(t)
	h := newInterruptHarness(t, s)

	// Falsifier A: a cancel cycle spawns nothing. One `Interrupt` plus one
	// `begin`/`finish` pair is the whole hot path, and it must add no
	// goroutine -- '~0% CPU at idle' is broken most often by a spawned poller.
	i := 0
	r := budget.Measure("job.cancel cycle", func() func() {
		return func() {
			i++
			id := "j-" + strconv.Itoa(i)
			h.runner.Interrupt(id)
			_, cancel := context.WithCancel(context.Background())
			if h.runner.begin(id, cancel) {
				h.runner.finish(id)
			}
			cancel()
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)

	// Falsifier B: memory does not grow with the number of jobs SEEN. A
	// hundredfold spread in n, per internal/budget's own note that a tenfold
	// spread failed to catch a planted per-tick leak.
	small := budget.Retained(200, cycler(t, s))
	large := budget.Retained(20_000, cycler(t, s))
	// The bounded set tops out at 256 ids; the whole structure is a few tens of
	// kilobytes at its cap. 256 KiB leaves room for allocator noise and still
	// fails an unbounded map by orders of magnitude (20,000 ids would be
	// megabytes).
	const ceiling = 256 << 10
	if large > ceiling {
		t.Fatalf("20,000 cancel cycles retained %d bytes (200 retained %d); the "+
			"cancellation state must be bounded by its cap, not by jobs seen", large, small)
	}
	t.Logf("cancel cycle: %d allocs/op, %d B/op, goroutine delta %d; retained 200=%dB 20000=%dB",
		r.AllocsPerOp, r.BytesPerOp, r.Goroutines, small, large)
}

// cycler builds one runner and returns the op that drives one cancel cycle
// against it, so `Retained` measures the RUNNER's growth rather than the cost
// of building runners.
func cycler(t *testing.T, s signer) func() func() {
	t.Helper()
	return func() func() {
		h := newInterruptHarness(t, s)
		i := 0
		return func() {
			i++
			id := "j-" + strconv.Itoa(i)
			h.runner.Interrupt(id)
			_, cancel := context.WithCancel(context.Background())
			if h.runner.begin(id, cancel) {
				h.runner.finish(id)
			}
			cancel()
		}
	}
}

func hasPrefix(s, prefix string) bool {
	return len(s) >= len(prefix) && s[:len(prefix)] == prefix
}
