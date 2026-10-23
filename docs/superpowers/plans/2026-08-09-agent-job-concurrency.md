# Agent Job Concurrency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move agent capability execution off the transport's single read goroutine, so a running job can no longer block the reader that must deliver its own reply.

**Architecture:** `runner.Handle` keeps its signature and stays on the read goroutine for the fast, ordered work — decode, verify, registry lookup — then hands the verified job to a new `Dispatcher` and returns. The `Dispatcher` owns per-capability queues keyed on the job type's module prefix, one worker goroutine per key (concurrent across capabilities, serial within one), a semaphore shared by declared-heavy capabilities, and bounded queues that refuse visibly rather than dropping or waiting forever.

**Tech Stack:** Go 1.24, standard library only. `github.com/coder/websocket` is already a dependency and is not touched.

**Spec:** `docs/superpowers/specs/2026-08-09-agent-job-concurrency-design.md` (commit `1efc444`). Where this plan and the spec disagree, the spec wins — raise the conflict rather than silently following either.

## Global Constraints

- **No new dependency.** Standard library only.
- **The agent idle budget is a hard requirement, not a goal.** Low-tens-of-MB resident, ~0% CPU at idle. Goroutine count at idle must return to baseline — no worker exists for a capability that has no work.
- **No unsigned command path exists, ever.** Verification stays inline on the read goroutine, in wire order. No task may move, skip, or defer a verification check.
- **Assert the state of the world, not "did not raise."** A test that asserts only that a call returned is not a test of concurrency.
- **Tests run locally under `go test -race`; do not edit `.github/workflows/`.** No CI change is needed, but not for the reason this line originally gave. The `agent` CI job already runs `go test ./... -count=1` over `agent/`, so every ordinary Go test this cycle adds **is** gated on every push. What is not gated is `-race` — the CI test step omits it deliberately, because the race detector's shadow memory would corrupt the idle-budget RSS thresholds measured in the same job. Caveat verified on run 31333534459: the job's `staticcheck` step runs before `test` and currently fails on ten pre-existing findings, so Actions skips the test step. The gate is configured; it is blocked by a red step above it.
- **Never `git add -A`, `git add .`, `git commit -a`, `git checkout .`, or `git stash`.** Run a bare `git reset` first, then `git add` explicit paths. Use `git worktree` if you need a clean tree to compare against.
- Push with `git push -u origin claude/handoff-spec-review-f45idd`.
- **Out of scope:** snapshot providers (`Options.OpenSnapshot`). `backup.run` will still refuse after this cycle — for one stated reason instead of two.

---

### Task 1: The harness that can see the deadlock

**Files:**
- Create: `agent/internal/capabilities/concurrency_harness_test.go`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces — later tasks build every concurrency test on this exact surface, so implement all of it now even though Task 1 only exercises part:
  - `type harnessOptions struct { register func(reg *capabilities.Registry, send func(context.Context, any) error) }` — `register` is called before the runner starts, so a test can add its own probe capability. Nil means "defaults only".
  - `func newConcurrencyHarness(t *testing.T, opts harnessOptions) *concurrencyHarness`
  - `(*concurrencyHarness).gateway` with `send(t *testing.T, frame []byte)`
  - `(*concurrencyHarness).sendJob(jobType string, payload map[string]any) string` — signs and pushes a job frame, returns the job id
  - `(*concurrencyHarness).awaitResult(t *testing.T, jobID string, d time.Duration) wire.JobResult`
  - `(*concurrencyHarness).awaitLog(t *testing.T, jobID, substr string, d time.Duration)` — blocks until a `job_log` for that id contains `substr`
  - `(*concurrencyHarness).encodeHeartbeat(t *testing.T) []byte` — any valid inbound frame the agent will read and ignore; used to prove the reader is alive
  - `(*concurrencyHarness).close()`
  - The harness wires a real `chunkreply.Registry` into its inbound routing, so a probe capability can block on `replies.Query(...)` — a wakeup only the read goroutine can serve. Task 6 depends on this.

**Names:** `harness`/`newHarness` collide with pre-existing declarations in this package, so the types are `concurrencyHarness`/`newConcurrencyHarness`. Every other name above is exact.

**Why this is first:** no test anywhere in `agent/` composes `transport.Run` + `runner.Handle` + a real capability. That gap is why the deadlock was invisible to a fully green suite. A harness that cannot observe the deadlock is worse than none, so Step 4 proves it can.

`newFakeGateway` is private to `internal/transport`'s test package and cannot be imported. Duplicate a minimal websocket server here — this is the established convention in this tree, stated at `internal/capabilities/netsweep/netsweep_test.go:1240`: "It is duplicated rather than shared."

- [ ] **Step 1: Write the harness**

Create `agent/internal/capabilities/concurrency_harness_test.go` in `package capabilities_test`. Model the fake gateway on `internal/transport/transport_test.go:159-185`. It must:

- accept a websocket with `websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})`
- expose `send(t *testing.T, frame []byte)` so a test can push an inbound frame at a chosen moment
- collect every outbound frame on a buffered channel, decoded with `wire.Decode`

Wire it to a real `jobs.Runner` built with `jobs.New(jobs.Options{...})` and a real `capabilities.Registry`, and run `transport.Run` against the fake gateway's URL with `runner.Handle` as the handler — exactly as `agent/cmd/agent/main.go:253-265` composes them.

- [ ] **Step 2: Write a passing end-to-end test**

```go
func TestAJobRunsEndToEndThroughTheRealTransport(t *testing.T) {
	h := newConcurrencyHarness(t, harnessOptions{})
	defer h.close()

	id := h.sendJob("hello", nil)

	res := h.awaitResult(t, id, 5*time.Second)
	if got := (struct {
		state string
		id    string
	}{res.State, res.JobID}); got.state != wire.StateSucceeded || got.id != id {
		t.Fatalf("got %+v, want state=%s id=%s", got, wire.StateSucceeded, id)
	}
}
```

- [ ] **Step 3: Run it**

Run: `cd agent && go test ./internal/capabilities/ -run TestAJobRunsEndToEnd -race -count=1 -v`
Expected: PASS. The harness composes the three layers correctly.

- [ ] **Step 4: Prove the harness can observe the deadlock**

This step produces **evidence, not a committed test**. Temporarily register a capability that sends a frame and then blocks waiting for a correlated inbound reply, and have the gateway send that reply only after it observes the job's `running` status:

```go
// TEMPORARY - do not commit.
must(reg.Register("probe.block", func(ctx context.Context, j jobs.VerifiedJob) (any, error) {
	if err := sender.Send(ctx, wire.JobLog{JobID: j.ID(), Line: "waiting"}); err != nil {
		return nil, err
	}
	select {
	case <-replyArrived:
		return map[string]any{"ok": true}, nil
	case <-time.After(3 * time.Second):
		return nil, fmt.Errorf("reply never arrived")
	}
}))
```

Run it. **Expected: FAIL** — the reply is not read while the capability blocks, so the capability times out. Record the observed failure verbatim in your report.

If it **passes**, stop and report that. It would mean the harness is not actually composing the read loop with the capability, and every later task in this plan would be built on an instrument that cannot detect what it exists to detect.

- [ ] **Step 5: Revert the temporary probe and confirm**

Run: `cd agent && git diff --stat` — the only changed file must be `concurrency_harness_test.go`, and it must contain no `probe.block` registration.

- [ ] **Step 6: Commit**

```bash
git reset
git add agent/internal/capabilities/concurrency_harness_test.go
git commit -m "test(agent): a harness that composes transport, runner and a real capability

No test in this tree put transport.Run, runner.Handle and a capability
together, which is why the dedup deadlock survived a fully green suite. The
fake gateway is duplicated rather than shared, per the convention stated in
netsweep_test.go.

Verified this harness can observe the deadlock before relying on it: a
temporary capability that blocks on a correlated reply times out against
current code. That probe is not committed; the permanent test lands with the
Dispatcher."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 2: capabilityKey

**Files:**
- Create: `agent/internal/jobs/key.go`
- Test: `agent/internal/jobs/key_test.go`

**Interfaces:**
- Consumes: nothing.
- Produces: `func capabilityKey(jobType string) string` — package-private, used by Task 3's `Dispatcher`.

- [ ] **Step 1: Write the failing test**

```go
func TestCapabilityKeyGroupsAJobTypeByItsModule(t *testing.T) {
	for _, tc := range []struct{ in, want string }{
		{"backup.run", "backup"},
		{"backup.restore", "backup"},
		{"patch.apply", "patch"},
		{"hello", "hello"},
		{"a.b.c", "a"},
		{"", ""},
		{".leading", ".leading"},
	} {
		if got := capabilityKey(tc.in); got != tc.want {
			t.Errorf("capabilityKey(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}
```

The `.leading` case is deliberate: a type starting with `.` must not key to the empty string, which would silently merge unrelated capabilities into one queue.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd agent && go test ./internal/jobs/ -run TestCapabilityKey -count=1`
Expected: FAIL — `undefined: capabilityKey`

- [ ] **Step 3: Write the implementation**

```go
package jobs

import "strings"

// capabilityKey is the serialization key for a job type: everything before the
// first dot, or the whole string when there is none.
//
// backup.run and backup.restore both key to "backup" ON PURPOSE. The shared
// mutable resource is the repository, not the verb -- keying on the exact job
// type would let a run write chunks while a restore reads the same repository,
// which surfaces as a corrupted restore rather than as a test failure.
//
// The index must be > 0, not >= 0: a type starting with a dot would otherwise
// key to "" and silently share a queue with every other malformed type.
func capabilityKey(jobType string) string {
	if i := strings.IndexByte(jobType, '.'); i > 0 {
		return jobType[:i]
	}
	return jobType
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd agent && go test ./internal/jobs/ -run TestCapabilityKey -race -count=1`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git reset
git add agent/internal/jobs/key.go agent/internal/jobs/key_test.go
git commit -m "feat(agent/jobs): capabilityKey groups job types by module"
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 3: The Dispatcher — queues, workers, serialization

**Files:**
- Create: `agent/internal/jobs/dispatcher.go`
- Test: `agent/internal/jobs/dispatcher_test.go`

**Interfaces:**
- Consumes: `capabilityKey` (Task 2).
- Produces:
  - `type DispatcherOptions struct { QueueDepth int; HeavyKeys map[string]bool; HeavyBudget int; Exec func(context.Context, VerifiedJob, Handler); Logger *slog.Logger }`
  - `func NewDispatcher(o DispatcherOptions) (*Dispatcher, error)`
  - `func (d *Dispatcher) Submit(ctx context.Context, job VerifiedJob, h Handler) error`
  - `var ErrQueueFull = errors.New("jobs: capability queue is full")`
  - `func (d *Dispatcher) Pending(key string) int` — test-facing count for assertions.

`Exec` is injected so this package's tests never need a signing key: Task 6 passes `Runner.run`.

- [ ] **Step 1: Write the failing tests**

```go
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
		go func() { defer wg.Done(); _ = d.Submit(context.Background(), fakeJob("backup.run"), nil) }()
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
	d := newTestDispatcher(t, DispatcherOptions{
		QueueDepth: 8,
		Exec: func(ctx context.Context, j VerifiedJob, h Handler) {
			started <- capabilityKey(j.Type())
			<-release
		},
	})

	_ = d.Submit(context.Background(), fakeJob("patch.apply"), nil)
	_ = d.Submit(context.Background(), fakeJob("inventory.collect"), nil)

	seen := map[string]bool{}
	for i := 0; i < 2; i++ {
		select {
		case k := <-started:
			seen[k] = true
		case <-time.After(2 * time.Second):
			t.Fatalf("only %d capabilities started; want both running at once", len(seen))
		}
	}
	close(release)

	if !seen["patch"] || !seen["inventory"] {
		t.Fatalf("started = %v, want both patch and inventory", seen)
	}
}

func TestNoWorkerGoroutineExistsUntilAJobArrives(t *testing.T) {
	base := runtime.NumGoroutine()
	d := newTestDispatcher(t, DispatcherOptions{QueueDepth: 8, Exec: func(context.Context, VerifiedJob, Handler) {}})

	if got := runtime.NumGoroutine(); got > base+1 {
		t.Fatalf("goroutines after NewDispatcher = %d, base = %d; a dispatcher with no work must start none", got, base)
	}

	_ = d.Submit(context.Background(), fakeJob("hello"), nil)
	d.drain(t, 5*time.Second)

	deadline := time.Now().Add(2 * time.Second)
	for runtime.NumGoroutine() > base+1 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := runtime.NumGoroutine(); got > base+1 {
		t.Fatalf("goroutines after drain = %d, base = %d; workers must exit when their queue empties", got, base)
	}
}
```

Write `fakeJob(jobType string) VerifiedJob` and the `newTestDispatcher` / `drain` helpers in the same file. **`VerifiedJob` cannot be constructed by tests** — `construct_test.go` enforces that deliberately. Use the same in-package construction path `runner_test.go` already uses for its own fixtures; read it first and follow it exactly. If no such path exists, stop and report rather than adding a test hook that manufactures a `VerifiedJob`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && go test ./internal/jobs/ -run 'TestTwoJobsOfOneCapability|TestDifferentCapabilities|TestNoWorkerGoroutine' -count=1`
Expected: FAIL — `undefined: NewDispatcher`

- [ ] **Step 3: Write the implementation**

```go
package jobs

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
)

// ErrQueueFull is returned by Submit when a capability's queue is at depth.
// The caller turns it into a refusal: the server must record `refused` with a
// reason rather than watch the job time out.
var ErrQueueFull = errors.New("jobs: capability queue is full")

type DispatcherOptions struct {
	// QueueDepth bounds each capability's queue.
	QueueDepth int
	// HeavyKeys names the capability keys that share HeavyBudget.
	HeavyKeys map[string]bool
	// HeavyBudget caps how many heavy capabilities execute at once.
	HeavyBudget int
	// Exec runs one verified job. Injected so this package's tests do not
	// need a signing key; the agent passes Runner.run.
	Exec func(context.Context, VerifiedJob, Handler)
	// Logger defaults to slog.Default().
	Logger *slog.Logger
}

// Dispatcher runs verified jobs off the transport's read goroutine.
//
// Concurrent ACROSS capabilities, strictly serial WITHIN one. That split is the
// whole design: job ordering only ever mattered per capability, and holding the
// read goroutine to get it cost the agent every inbound message that arrives
// while a job runs -- including the reply the job itself is waiting for.
type Dispatcher struct {
	mu     sync.Mutex
	queues map[string]chan work
	depth  int
	heavy  map[string]bool
	sem    chan struct{}
	exec   func(context.Context, VerifiedJob, Handler)
	log    *slog.Logger
}

type work struct {
	ctx context.Context
	job VerifiedJob
	h   Handler
}

func NewDispatcher(o DispatcherOptions) (*Dispatcher, error) {
	if o.QueueDepth <= 0 {
		return nil, fmt.Errorf("%w: queue depth must be positive", ErrIncomplete)
	}
	if o.Exec == nil {
		return nil, fmt.Errorf("%w: no exec (a dispatcher that cannot run a job is not one)", ErrIncomplete)
	}
	d := &Dispatcher{
		queues: make(map[string]chan work),
		depth:  o.QueueDepth,
		heavy:  o.HeavyKeys,
		exec:   o.Exec,
		log:    o.Logger,
	}
	if d.log == nil {
		d.log = slog.Default()
	}
	if o.HeavyBudget > 0 {
		d.sem = make(chan struct{}, o.HeavyBudget)
	}
	return d, nil
}

// Submit queues a job for its capability. It NEVER blocks: the caller is the
// transport's read goroutine, and blocking it is the defect this type exists
// to remove.
func (d *Dispatcher) Submit(ctx context.Context, job VerifiedJob, h Handler) error {
	key := capabilityKey(job.Type())

	d.mu.Lock()
	defer d.mu.Unlock()

	q, ok := d.queues[key]
	if !ok {
		q = make(chan work, d.depth)
		d.queues[key] = q
		go d.worker(key, q)
	}
	select {
	case q <- work{ctx: ctx, job: job, h: h}:
		return nil
	default:
		return ErrQueueFull
	}
}

// Pending reports queued (not running) jobs for a key. Test-facing.
func (d *Dispatcher) Pending(key string) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	if q, ok := d.queues[key]; ok {
		return len(q)
	}
	return 0
}

// worker drains one capability's queue and exits when it is empty, so an idle
// agent runs no goroutine per capability.
//
// The exit decision is made UNDER THE SAME MUTEX Submit uses. Without that, a
// Submit that observes the queue in the map can hand work to a worker already
// past its last read, and the job is lost with no error anywhere.
func (d *Dispatcher) worker(key string, q chan work) {
	for {
		select {
		case w := <-q:
			d.exec1(w)
		default:
			d.mu.Lock()
			if len(q) > 0 {
				d.mu.Unlock()
				continue
			}
			delete(d.queues, key)
			d.mu.Unlock()
			return
		}
	}
}

func (d *Dispatcher) exec1(w work) {
	// A panic anywhere in the worker -- not just inside the capability, which
	// Runner.invoke already recovers -- would otherwise take the whole agent
	// process down, because this is no longer the read goroutine's stack.
	defer func() {
		if p := recover(); p != nil {
			d.log.Error("dispatcher worker panicked",
				"job_type", w.job.Type(), "panic", fmt.Sprint(p))
		}
	}()
	d.exec(w.ctx, w.job, w.h)
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agent && go test ./internal/jobs/ -run 'TestTwoJobsOfOneCapability|TestDifferentCapabilities|TestNoWorkerGoroutine' -race -count=1 -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git reset
git add agent/internal/jobs/dispatcher.go agent/internal/jobs/dispatcher_test.go
git commit -m "feat(agent/jobs): a dispatcher that is serial per capability

Concurrent across capabilities, strictly serial within one. Workers are spawned
on first job and exit when their queue empties, so an idle agent runs no
per-capability goroutine. The spawn/exit decision is made under the same mutex
Submit uses -- without it a Submit can hand work to a worker already past its
last read, losing the job silently."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 4: The spawn/exit race, under -race

**Files:**
- Modify: `agent/internal/jobs/dispatcher_test.go`

**Interfaces:**
- Consumes: `Dispatcher`, `Submit`, `Pending` (Task 3).
- Produces: nothing new.

**Why its own task:** Task 3's tests pass whether or not the mutex covers the exit path. This one fails without it. A reviewer can reject this while approving Task 3.

- [ ] **Step 1: Write the failing-under-mutation test**

```go
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
		if err := d.Submit(context.Background(), fakeJob("hello"), nil); err == nil {
			accepted++
		}
		if i%7 == 0 {
			time.Sleep(time.Millisecond) // let the worker drain and exit
		}
	}
	d.drain(t, 10*time.Second)

	if got := int(ran.Load()); got != accepted {
		t.Fatalf("ran %d of %d accepted submits; every accepted job must run exactly once", got, accepted)
	}
}
```

The interleaved sleep is load-bearing: it drives the worker to the empty-queue branch repeatedly, which is the only way to exercise the exit/spawn window.

- [ ] **Step 2: Run it**

Run: `cd agent && go test ./internal/jobs/ -run TestABurstOfSubmits -race -count=5`
Expected: PASS

- [ ] **Step 3: Falsify it — prove the test can detect the race**

Move the exit path out from under the mutex in `dispatcher.go`'s `worker`:

```go
		default:
			// FALSIFICATION - revert this.
			if len(q) > 0 {
				continue
			}
			d.mu.Lock()
			delete(d.queues, key)
			d.mu.Unlock()
			return
```

**Confirm the edit applied by re-reading the file**, then run:

Run: `cd agent && go test ./internal/jobs/ -run TestABurstOfSubmits -race -count=5`
Expected: **FAIL** — either a lost job (`ran` < `accepted`) or a race report.

Report the actual output. If it passes all 5 runs, the test does not detect the race it exists for: report that rather than proceeding.

- [ ] **Step 4: Revert and confirm**

Run: `cd agent && git diff agent/internal/jobs/dispatcher.go`
Expected: empty. The falsification must not reach the commit.

- [ ] **Step 5: Commit**

```bash
git reset
git add agent/internal/jobs/dispatcher_test.go
git commit -m "test(agent/jobs): a submit burst loses no job across worker restarts

Falsified: moving the exit path out from under Submit's mutex makes this fail
with a lost job or a race report."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 5: The heavy semaphore and bounded-queue refusal

**Files:**
- Modify: `agent/internal/jobs/dispatcher.go`
- Modify: `agent/internal/jobs/dispatcher_test.go`

**Interfaces:**
- Consumes: `Dispatcher` (Task 3).
- Produces: heavy-gating behaviour inside `exec1`; `ErrQueueFull` observable from `Submit`.

- [ ] **Step 1: Write the failing tests**

```go
func TestOnlyTwoHeavyCapabilitiesRunAtOnce(t *testing.T) {
	var mu sync.Mutex
	var active, maxActive int
	release := make(chan struct{})
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
			<-release
			mu.Lock()
			active--
			mu.Unlock()
		},
	})

	for _, jt := range []string{"backup.run", "patch.apply", "deploy.script"} {
		if err := d.Submit(context.Background(), fakeJob(jt), nil); err != nil {
			t.Fatalf("Submit(%s) = %v", jt, err)
		}
	}

	time.Sleep(200 * time.Millisecond)
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
	ran := make(chan string, 4)
	d := newTestDispatcher(t, DispatcherOptions{
		QueueDepth:  8,
		HeavyKeys:   map[string]bool{"backup": true, "patch": true},
		HeavyBudget: 2,
		Exec: func(ctx context.Context, j VerifiedJob, h Handler) {
			if capabilityKey(j.Type()) == "inventory" {
				ran <- "inventory"
				return
			}
			ran <- "heavy"
			<-release
		},
	})

	_ = d.Submit(context.Background(), fakeJob("backup.run"), nil)
	_ = d.Submit(context.Background(), fakeJob("patch.apply"), nil)
	_ = d.Submit(context.Background(), fakeJob("inventory.collect"), nil)

	deadline := time.After(2 * time.Second)
	for i := 0; i < 3; i++ {
		select {
		case got := <-ran:
			if got == "inventory" {
				close(release)
				return // inventory ran while both heavy slots were held
			}
		case <-deadline:
			close(release)
			t.Fatal("inventory.collect never ran while the heavy budget was full")
		}
	}
	close(release)
	t.Fatal("inventory.collect never ran while the heavy budget was full")
}

func TestAFullQueueIsRefusedRatherThanDropped(t *testing.T) {
	release := make(chan struct{})
	d := newTestDispatcher(t, DispatcherOptions{
		QueueDepth: 2,
		Exec: func(context.Context, VerifiedJob, Handler) { <-release },
	})

	// One job occupies the worker; two more fill the queue.
	for i := 0; i < 3; i++ {
		if err := d.Submit(context.Background(), fakeJob("backup.run"), nil); err != nil {
			t.Fatalf("Submit %d = %v, want nil", i, err)
		}
	}

	err := d.Submit(context.Background(), fakeJob("backup.run"), nil)
	close(release)
	d.drain(t, 5*time.Second)

	if !errors.Is(err, ErrQueueFull) {
		t.Fatalf("Submit on a full queue = %v, want ErrQueueFull", err)
	}
}
```

The third test's comment explains the count: with depth 2, the first `Submit` is taken by the worker, the next two fill the buffer, and the fourth must be refused. If timing makes that flaky, assert on `Pending("backup")` reaching depth before the final `Submit` rather than loosening the assertion.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && go test ./internal/jobs/ -run 'TestOnlyTwoHeavy|TestALightCapability|TestAFullQueue' -count=1`
Expected: the two heavy tests FAIL (no gating yet). `TestAFullQueue` may already pass — Task 3's `Submit` returns `ErrQueueFull`. Note which passed and why in your report.

- [ ] **Step 3: Add heavy gating to `exec1`**

```go
func (d *Dispatcher) exec1(w work) {
	defer func() {
		if p := recover(); p != nil {
			d.log.Error("dispatcher worker panicked",
				"job_type", w.job.Type(), "panic", fmt.Sprint(p))
		}
	}()

	// Heavy capabilities share a budget. Light ones never touch the semaphore,
	// so a full heavy budget cannot delay inventory or health.
	if d.sem != nil && d.heavy[capabilityKey(w.job.Type())] {
		select {
		case d.sem <- struct{}{}:
			defer func() { <-d.sem }()
		case <-w.ctx.Done():
			// The connection went away while this job waited for a slot.
			// Dropping it here is correct: the context that carried it is the
			// transport's, and the control plane expires the job itself.
			d.log.Warn("heavy job abandoned before it started",
				"job_type", w.job.Type(), "err", w.ctx.Err())
			return
		}
	}

	d.exec(w.ctx, w.job, w.h)
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agent && go test ./internal/jobs/ -race -count=1`
Expected: PASS, whole package.

- [ ] **Step 5: Commit**

```bash
git reset
git add agent/internal/jobs/dispatcher.go agent/internal/jobs/dispatcher_test.go
git commit -m "feat(agent/jobs): heavy-capability budget and visible queue refusal

Heavy capabilities share a budget of 2; light ones never touch the semaphore,
so a full heavy budget cannot delay inventory or health. A full queue returns
ErrQueueFull for the caller to turn into a refusal -- never a silent drop."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 6: Wire the Dispatcher into Runner, and land the deadlock test

**Files:**
- Modify: `agent/internal/jobs/runner.go:75-137` (`Handle`) and `Options`
- Modify: `agent/cmd/agent/main.go:234-265`
- Modify: `agent/internal/capabilities/concurrency_harness_test.go`
- Test: `agent/internal/jobs/runner_test.go`

**Interfaces:**
- Consumes: `NewDispatcher`, `Submit`, `ErrQueueFull` (Tasks 3, 5).
- Produces: `jobs.Options.Dispatcher *Dispatcher` — when nil, `New` builds one with `QueueDepth: 8`, `HeavyBudget: 2`, `HeavyKeys: {"backup","patch","deploy"}` and `Exec: r.run`.

**This is the task where the falsifier from Task 1 becomes a permanent, passing test.**

- [ ] **Step 1: Write the failing deadlock test**

Add to `concurrency_harness_test.go`, promoting Task 1 Step 4's temporary probe into a real test:

**CORRECTED — read this before writing the test.** An earlier draft of this plan had the capability block on a channel the *test goroutine* closed. That test would have passed with the deadlock fully present, because nothing about the wakeup depended on the read goroutine. Task 1's implementer caught it.

The wakeup must travel a path **only the transport's read goroutine can serve**. Use the real `chunkreply` registry — the exact mechanism `backup.run` uses, already wired into the harness's inbound routing (`concurrency_harness_test.go:124`, `:152-153`):

```go
func TestACapabilityCanReceiveACorrelatedReplyWhileItRuns(t *testing.T) {
	h := newConcurrencyHarness(t, harnessOptions{ /* register probe.query, below */ })
	defer h.close()

	id := h.sendJob("probe.query", nil)

	res := h.awaitResult(t, id, 10*time.Second)
	if res.State != wire.StateSucceeded {
		t.Fatalf("state = %s, want %s: a capability must be able to receive a correlated "+
			"reply while it runs; the read goroutine is blocked inside Handle",
			res.State, wire.StateSucceeded)
	}
}
```

The `probe.query` capability calls `replies.Query(ctx, wire.ChunkQuery{JobID: j.ID(), QueryID: "q1"})` and returns success only when a grant comes back. The gateway watches its inbound frames for the `backup_chunk_query`, and answers it with a matching `wire.ChunkGrant` — same `JobID` and `QueryID`, or `chunkreply` will not correlate it and the wait will time out regardless of the fix.

Follow Task 1's Step 4 probe, which already did exactly this and is described in `/home/user/Toeshee-RMS/.git/sdd/task-1-report.md`. Add whatever small harness helpers you need (e.g. a hook that fires when the gateway observes a chunk query, and an encoder for the grant) and document them in the file.

**Field names:** `wire.JobLog`'s message field is `Msg`, not `Line`. Check every wire struct field against `internal/wire/wire.go` rather than trusting this plan's snippets — an earlier draft had that one wrong.

**Harness names:** Task 1 renamed `harness`/`newHarness` to `concurrencyHarness`/`newConcurrencyHarness` because the originals collided with pre-existing declarations in the same package. Every other name in this plan (`harnessOptions`, `gateway`, `sendJob`, `awaitResult`, `awaitLog`, `encodeHeartbeat`, `close`) is exact.

- [ ] **Step 2: Run it and confirm it fails**

Run: `cd agent && go test ./internal/capabilities/ -run TestACapabilityCanReceive -race -count=1 -v`
Expected: **FAIL** — the capability times out because the read goroutine is inside `Handle`.

Record the output verbatim. This is the RED that justifies the whole cycle.

- [ ] **Step 3: Add the Dispatcher to Options and New**

In `runner.go`, add to `Options`:

```go
	// Dispatcher runs verified jobs off the read goroutine. Nil builds the
	// default: queue depth 8, heavy budget 2.
	Dispatcher *Dispatcher
```

In `New`, after the sender check:

```go
	if o.Dispatcher == nil {
		d, err := NewDispatcher(DispatcherOptions{
			QueueDepth:  8,
			HeavyBudget: 2,
			HeavyKeys:   map[string]bool{"backup": true, "patch": true, "deploy": true},
			Exec:        r.run,
			Logger:      o.Logger,
		})
		if err != nil {
			return nil, err
		}
		o.Dispatcher = d
	}
	r.dispatcher = o.Dispatcher
```

Add `dispatcher *Dispatcher` to the `Runner` struct. Note `Exec: r.run` — the panic recover in `invoke` therefore still wraps every capability call, on the worker's stack.

- [ ] **Step 4: Replace the inline call in Handle**

`Handle`'s final line is currently `r.run(ctx, job, handler)`. Replace it with:

```go
	if err := r.dispatcher.Submit(ctx, job, handler); err != nil {
		// A full queue is a real condition the operator must see. Refusing is
		// what puts it in the server's job record; dropping it silently would
		// show up only as a job that timed out for no stated reason.
		r.log.Warn("capability queue full; refusing",
			"job_id", job.ID(), "job_type", job.Type())
		r.refuse(ctx, job.ID(), ReasonQueueFull,
			Refuse("this capability already has the maximum number of queued jobs"))
	}
```

Add to `verify.go`'s reason constants:

```go
	ReasonQueueFull RefusalReason = "queue_full"
```

Update `Handle`'s doc comment — the old text states the blocking contract this task removes:

```go
// Handle processes one inbound frame.
//
// It is called on the transport's read goroutine and MUST NOT BLOCK. Decode,
// verification and registry lookup happen here, in wire order; capability
// execution is handed to the Dispatcher and this returns immediately.
//
// It used to block for the whole capability, so that job ordering was the
// wire's ordering. That cost the agent every inbound frame arriving while a job
// ran -- including a reply the job itself was waiting for, which deadlocked the
// backup dedup exchange, and including the WebSocket pongs whose absence made
// the gateway close the connection. Ordering is now kept per capability by the
// Dispatcher, which is the only place it ever mattered.
```

- [ ] **Step 5: Wire main.go**

`main.go` needs no change if `New` defaults the dispatcher — confirm by reading `main.go:234-265`. If it constructs `jobs.Options` explicitly, leave `Dispatcher` unset so the default applies, and add a comment naming the default's values so a reader is not hunting for them.

- [ ] **Step 6: Run the deadlock test**

Run: `cd agent && go test ./internal/capabilities/ -run TestACapabilityCanReceive -race -count=1 -v`
Expected: **PASS**

- [ ] **Step 7: Prove the refusal reaches the server, not just the caller**

Task 5 asserted `Submit` returns `ErrQueueFull`. That is not the same claim as the spec's — §5 item 5 requires the refusal to reach the **server**. Add to `concurrency_harness_test.go`:

```go
func TestAFullQueueRefusesToTheServerRatherThanTimingOut(t *testing.T) {
	release := make(chan struct{})
	h := newConcurrencyHarness(t, harnessOptions{
		register: func(reg *capabilities.Registry, _ func(context.Context, any) error) {
			must(reg.Register("probe.slow", func(ctx context.Context, _ jobs.VerifiedJob) (any, error) {
				select {
				case <-release:
					return map[string]any{"ok": true}, nil
				case <-ctx.Done():
					return nil, ctx.Err()
				}
			}))
		},
	})
	defer h.close()

	// Depth 8 plus the one in flight; the tenth must be refused.
	var ids []string
	for i := 0; i < 10; i++ {
		ids = append(ids, h.sendJob("probe.slow", nil))
	}

	res := h.awaitResult(t, ids[len(ids)-1], 5*time.Second)
	close(release)

	reason, _ := res.Result["reason"].(string)
	if got := (struct {
		state  string
		reason string
	}{res.State, reason}); got.state != wire.StateRefused || got.reason == "" {
		t.Fatalf("overflow job = %+v, want state=%s with a non-empty reason", got, wire.StateRefused)
	}
}
```

Run: `cd agent && go test ./internal/capabilities/ -run TestAFullQueueRefusesToTheServer -race -count=1 -v`
Expected: PASS. If the tenth job is instead *accepted*, the queue is not bounded where you think it is — report that rather than raising the loop count until it refuses.

- [ ] **Step 8: Run the whole agent suite**

Run: `cd agent && go build ./... && go vet ./... && go test ./... -race -count=1`
Expected: all packages ok. `internal/capabilities/site` has a known pre-existing flaky RSS-delta test — if it fails once and passes on rerun, say so and move on.

- [ ] **Step 9: Commit**

```bash
git reset
git add agent/internal/jobs/runner.go agent/internal/jobs/verify.go agent/cmd/agent/main.go agent/internal/capabilities/concurrency_harness_test.go
git commit -m "fix(agent): run capabilities off the transport's read goroutine

Handle no longer blocks for the duration of a capability. It decodes, verifies
and looks up on the read goroutine, in wire order, then submits to the
Dispatcher and returns.

This is the deadlock the live MinIO run measured: backup.run sent a chunk query
and waited for a grant that only the goroutine it was occupying could read, so
the grant sat unread for the full 60s deadline. The same blocking stopped
WebSocket pongs, and the gateway closed the connection.

The committed test fails against the previous commit."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 7: Panic containment and HeavyKeys integrity

**Files:**
- Modify: `agent/internal/capabilities/concurrency_harness_test.go`
- Test: `agent/internal/capabilities/registry_test.go`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests**

```go
func TestAPanickingCapabilityFailsItsJobAndTheAgentKeepsServing(t *testing.T) {
	h := newConcurrencyHarness(t, harnessOptions{
		register: func(reg *capabilities.Registry, _ func(context.Context, any) error) {
			must(reg.Register("probe.panic", func(context.Context, jobs.VerifiedJob) (any, error) {
				panic("deliberate")
			}))
		},
	})
	defer h.close()

	bad := h.sendJob("probe.panic", nil)
	if res := h.awaitResult(t, bad, 5*time.Second); res.State != wire.StateFailed {
		t.Fatalf("panicking job state = %s, want %s", res.State, wire.StateFailed)
	}

	// The agent must still be alive and serving.
	good := h.sendJob("hello", nil)
	if res := h.awaitResult(t, good, 5*time.Second); res.State != wire.StateSucceeded {
		t.Fatalf("next job after a panic = %s, want %s", res.State, wire.StateSucceeded)
	}
}

func TestEveryHeavyKeyNamesARegisteredCapability(t *testing.T) {
	registered := capabilities.Types()
	for key := range jobs.DefaultHeavyKeys() {
		found := false
		for _, jt := range registered {
			if strings.HasPrefix(jt, key+".") || jt == key {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("HeavyKeys names %q, which no registered job type matches; "+
				"a typo here silently un-caps a heavy capability", key)
		}
	}
}
```

Neither helper exists yet. Note that `capabilities.mustBuild()` (`registry.go:147`) is **package-private** and this test is in `package capabilities_test`, so it cannot be called — add a package-level `capabilities.Types()` backed by `defaultRegistry` instead, mirroring the existing package-level `Lookup` at `registry.go:137`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && go test ./internal/capabilities/ -run 'TestAPanickingCapability|TestEveryHeavyKey' -count=1`
Expected: FAIL — undefined helpers.

- [ ] **Step 3: Add the helpers**

Extract the heavy-keys map in `runner.go` into:

```go
// DefaultHeavyKeys names the capabilities that share the heavy budget. Exported
// so a test can assert every entry still matches a registered job type -- a
// typo here would silently un-cap a heavy capability.
func DefaultHeavyKeys() map[string]bool {
	return map[string]bool{"backup": true, "patch": true, "deploy": true}
}
```

and use it in `New`. Add `Types()` to `capabilities.Registry` returning sorted keys.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agent && go test ./internal/capabilities/ -run 'TestAPanickingCapability|TestEveryHeavyKey' -race -count=1 -v`
Expected: PASS

- [ ] **Step 5: Falsify the panic test**

Remove the `recover` from `Runner.invoke` (`runner.go:195-206`). Confirm the edit applied, then run `TestAPanickingCapability`.
Expected: the test process **crashes** rather than failing cleanly — that is the regression this test exists to prevent, and the crash is the evidence. Record the output, then revert and confirm `git diff agent/internal/jobs/runner.go` is empty.

- [ ] **Step 6: Full verification and commit**

Run: `cd agent && go build ./... && go vet ./... && go test ./... -race -count=1`

```bash
git reset
git add agent/internal/jobs/runner.go agent/internal/capabilities/registry.go agent/internal/capabilities/concurrency_harness_test.go agent/internal/capabilities/registry_test.go
git commit -m "test(agent): panic containment on the worker, and HeavyKeys integrity

A capability that panics now does so on a worker goroutine rather than the read
goroutine. Falsified: removing invoke's recover crashes the test process instead
of failing the job.

DefaultHeavyKeys is exported so a test can assert every entry still matches a
registered job type -- a typo there silently un-caps a heavy capability."
git push -u origin claude/handoff-spec-review-f45idd
```

---

### Task 8: Update the documents that describe the old contract

**Files:**
- Modify: `HANDOFF.md` §2 and §6
- Modify: `docs/modules/backup/spec.md` §6.1 item 5
- Modify: `docs/05-agent-spec.md` (search for the read-goroutine contract)

**Interfaces:** none.

- [ ] **Step 1: Correct spec §6.1 item 5**

It currently says the dedup exchange deadlocks against the agent's transport, as an open blocker. Rewrite it to record that the deadlock is **fixed**, by what, and what the fix does and does not prove — that the concurrency model is verified on Linux against a fake gateway, and that `backup.run` still refuses on the snapshot provider (item 4), which remains open.

Do **not** claim `backup.run` now completes. It does not.

- [ ] **Step 2: Correct HANDOFF.md**

§2's blocker 2 and §6's open-items table both describe the deadlock as current. Update both. Keep blocker 1 (no snapshot provider) exactly as it is — it is still true and is now the single remaining reason `backup.run` refuses.

Add one line to §4 recording the trap this cycle found: a test suite that injects a capability's transport seam in-process cannot see a deadlock between the capability and the real read loop, and `agent/internal/capabilities/concurrency_harness_test.go` is the harness that can.

- [ ] **Step 3: State the CI position**

Add one line where the new tests are described, stating the CI position accurately: the `agent` job runs `go test ./... -count=1` on every push, so these tests **are** gated; the `-race` verification is local only, because CI deliberately omits `-race` (it would corrupt the idle-budget measurements in the same job). Do not claim `-race` coverage that is not configured, and do not deny the coverage that is.

- [ ] **Step 4: Commit**

```bash
git reset
git add HANDOFF.md docs/modules/backup/spec.md docs/05-agent-spec.md
git commit -m "docs: the read-goroutine deadlock is fixed; the snapshot blocker is not"
git push -u origin claude/handoff-spec-review-f45idd
```

---

## Verification

After Task 8, run and report actual numbers:

```bash
cd agent && go build ./... && go vet ./... && go test ./... -race -count=1
```

Baseline: 22 packages ok. The backend, frontend, mypy and ruff suites are untouched by this plan — do not run them unless something here changed a Python or TypeScript file.

## What this plan does not do

- It does not make `backup.run` complete. `Options.OpenSnapshot` is still nil.
- It does not change CI configuration or gate on CI.
- It does not load-test. Queue depth 8 and heavy budget 2 are reasoned defaults, not measured ones, and the spec's §6 says so.
