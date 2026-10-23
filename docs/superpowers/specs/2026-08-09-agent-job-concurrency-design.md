# Design — the agent's job concurrency model

**Status:** approved in brainstorming, not yet implemented.
**Scope:** move capability execution off the transport's read goroutine, with
concurrency serialized per capability.
**Out of scope:** snapshot providers (`Options.OpenSnapshot`). Their own cycle,
covering Windows VSS, macOS APFS and Linux LVM together — see §7.

Read `CLAUDE.md` first. The agent idle budget governs this work, and the
"no unsigned command path" invariant constrains where verification may happen.

---

## 0. Why this cycle

The previous cycle built the backup transport and then measured it against real
MinIO. The measurement disproved its own premise: `backup.run` does not complete
end to end. Two blockers, recorded in `HANDOFF.md` §2 and
`docs/modules/backup/spec.md` §6.1. This cycle addresses the second.

`jobs.Runner.Handle` runs on the transport's single read goroutine and blocks
for the whole capability. Its doc comment states this is deliberate:

> It is called on the transport's read goroutine, in wire order, and it blocks
> for the duration of the capability. That is deliberate: job ordering is the
> wire's ordering, and nothing needs an unbounded inbound queue.

**The final clause is what expired.** The §5.2 dedup exchange is the first thing
that needs an inbound message *while a job is running*: `backup.run` sends a
`backup_chunk_query` and blocks for the `backup_chunk_grant`, whose only reader
is the goroutine the run is occupying.

Measured live: the server published the grant, it sat unread for the full 60s
`chunkreply` deadline, the run failed naming the unanswered query, and only
after `Handle` returned did the agent read it and log `dropped a backup chunk
grant that nothing was waiting for`.

### The symptom is wider than backup

`coder/websocket` services ping/pong inside `Read`. A blocked reader therefore
sends no pong, and the same live run measured the gateway closing the connection
at that moment. This is not specific to the dedup exchange: **any capability
occupying the read goroutine longer than the server's ping timeout drops the
connection.** `patch.apply`, `app.deploy` and a long `backup.run` all qualify.
The dedup deadlock is the first symptom to be caught, not the only one present.

### The lesson this cycle inherits

The previous cycle's design §1.1 chose a correlation registry and rejected
transport changes **without checking whether the transport can deliver a reply
while a job runs.** It cannot. Every test injected `Options.QueryChunks` and
answered it in-process, so a fully green suite said nothing about it.

No test anywhere in `agent/` composes `transport.Run` + `runner.Handle` + a real
capability. That gap is why the defect was invisible, and it remains open. This
cycle closes it first (§5).

---

## 1. Resolved decisions

Each was a real choice with a rejected alternative, recorded so a later session
does not silently re-litigate one.

### 1.1 Fix the concurrency model, not the symptom

**Chosen:** treat the blocked read goroutine as the defect.

**Rejected — unblock the dedup exchange only** (e.g. a reply pump that services
correlated replies while a job blocks): it would make `backup.run` work while
leaving the reader occupied, so pong starvation and connection drops under long
jobs would remain — a defect already measured, not hypothetical.

### 1.2 Concurrent across capabilities, serialized within one

**Chosen:** different capabilities run in parallel; two jobs of the same
capability never overlap.

**Rejected — one job at a time, off the read goroutine:** preserves today's
ordering guarantee exactly and is the smallest change, but leaves
`inventory.collect` queued behind a 40-minute `patch.apply` on a fleet endpoint.

**Rejected — bounded concurrent workers with no per-capability rule:** abandons
ordering within a capability, which is where ordering actually matters.

### 1.3 The serialization key is the capability, not the job type

**Chosen:** `capabilityKey(jobType)` = everything before the first `.`, or the
whole string when there is none. `backup.run` and `backup.restore` both key to
`backup`; `hello` keys to `hello`.

**Why:** the shared mutable resource is the repository, not the verb. Keying on
the exact job type would let `backup.run` write chunks while `backup.restore`
reads the same repository — a correctness risk that surfaces as a corrupted
restore, not as a test failure.

**Rejected — each capability declares its own key:** most precise, but it is a
new contract every capability author must get right, and getting it wrong is
silent.

### 1.4 Only declared-heavy capabilities share a concurrency budget

**Chosen:** a `HeavyKeys` set in `DispatcherOptions`, with a **heavy budget of
2**. Heavy capabilities acquire a semaphore slot before executing; light ones
never touch it.

**Corrected against the shipped code.** This section originally named
`backup`, `patch`, `deploy` and located the set in `main.go`. Both were wrong.
The set is `jobs.DefaultHeavyKeys()` in `agent/internal/jobs/runner.go`, and it
is `{backup, patch, app, script}`: keys are capability prefixes, and deploy's
job types are `app.deploy` and `script.run`, so `deploy` matched nothing.
`main.go` leaves `Options.Dispatcher` nil and takes that default.

**Why 2:** one long heavy job must not stall a second indefinitely, but a
constrained endpoint should not have three heavy capabilities allocating at
once. Like the queue depth, this is reasoned rather than measured — see §6.

**Stated precisely:** `CLAUDE.md`'s budget governs **idle** footprint, so
concurrent jobs do not violate it directly. The risk this bounds is different —
several heavy capabilities allocating at once on a constrained endpoint.

**Declared per key, not per job type.** Registering `backup.run` heavy but not
`backup.restore` would leave the `backup` key's heaviness ambiguous.

**Residual risk, and what the test does and does not catch:** the list can
drift, in two directions, and only one of them is testable here.
`TestEveryHeavyKeyNamesARegisteredCapability` iterates `HeavyKeys` into the
registry and asserts every entry maps to at least one registered job type, so a
typo or a removed capability fails loudly — that is how the `deploy` entry
above was found. It **cannot** catch the other direction: a heavy capability
registered later and never added to the map. Catching that needs a per-capability
heaviness declaration, which this section rejects immediately below, so an
addition is a review obligation rather than a tested one — and it fails open
(the capability runs unthrottled, silently).

**Rejected — no global bound:** peak footprint becomes the sum of every
capability that happens to have work.

### 1.5 Bounded queues that refuse visibly

**Chosen:** a bounded queue per capability, **depth 8**. When full, the job is
refused through the existing `jobs.Refuse` path with a named reason.

**Why 8:** a queued entry is a `VerifiedJob` plus a handler reference — bytes,
not payload — so the depth is chosen to absorb a plausible burst rather than to
bound memory. Eight is a reasoning-based default, not a measured one; §6 records
that honestly, and the first real deployment is what will show whether it is
right.

**Why:** the server records `refused` and the operator sees why. An unbounded
queue makes a backlog invisible until the server times jobs out, and is
unaccounted memory besides.

**Rejected — no queue, refuse immediately when busy:** a routine burst becomes
a refusal the operator must interpret.

### 1.6 Verification stays on the read goroutine

**Chosen:** `Handle` still decodes, verifies and looks up inline, then submits.
Only capability *execution* moves.

**Why:** verification is fast, must stay in wire order, and a bad-signature
refusal should remain immediate. This also keeps the "no unsigned command path"
invariant on a single, ordered path rather than spread across workers.

---

## 2. Architecture

**Unchanged:** `transport.Run` and its serve loop still call
`handler(ctx, raw)` inline on the single read goroutine. `main.go` still passes
`runner.Handle`. The transport learns nothing about capabilities — the same
boundary the previous cycle's §1.1 defended, and the reason approach C below was
rejected.

**Changed:** `runner.Handle` keeps its signature and stops blocking.

**New:** `Dispatcher` in `internal/jobs`, owning per-capability queues, worker
goroutines, and the heavy semaphore.

### Rejected structural alternatives

- **`Runner` absorbs the queues.** No new type, but `Runner` becomes decode +
  verify + lookup + schedule + execute + recover, and its queueing can only be
  tested through the signing path.
- **A worker pool inside `transport.Run`.** Smallest diff, but it puts
  capability identity, heaviness and per-key serialization inside the transport
  that carries mTLS identity and reconnect logic.

---

## 3. Components

```go
func NewDispatcher(opts DispatcherOptions) *Dispatcher
func (d *Dispatcher) Submit(ctx context.Context, job VerifiedJob, h Handler) error
```

`Submit` never blocks. It returns `ErrQueueFull` when the capability's queue is
at depth; `Runner` turns that into `jobs.Refuse`.

**Worker lifecycle.** No goroutine exists for a key until its first job. The
worker drains its queue and exits when empty; the next `Submit` spawns a fresh
one. Goroutine count at idle is therefore zero, which is what the idle budget
requires.

**The one genuine race.** The spawn/exit decision must be made under the same
mutex that guards the queue map. A worker exiting concurrently with a `Submit`
would otherwise lose the job. This is what `-race` plus the burst test in §5
exist to catch.

---

## 4. Data flow and error handling

### The dedup exchange, end to end

1. Read goroutine receives the job frame → `Handle` decodes, verifies, looks up
   → `Submit` → **returns immediately**.
2. The `backup` worker runs `backup.run`, sends `backup_chunk_query`, and blocks
   in `chunkreply`.
3. The read goroutine — now free — receives the grant → `Handle` routes it to
   `chunkreply` → the worker wakes.
4. The worker finishes; the result goes out through the outbox.

**Step 3 is the fix, and it was structurally impossible before.**

### The highest-risk regression in this change

`Handle`'s contract says a panicking capability is recovered and reported failed,
"because one bad capability must not take the connection down with it." That
`recover` currently sits on the read goroutine.

**Moving execution to a worker goroutine means the recover must move with it.**
A panic on a goroutine with no recover does not fail a job — it terminates the
agent process. That is strictly worse than today's behaviour and gets its own
test.

### Error paths

| Condition | Behaviour |
|---|---|
| Queue full | `ErrQueueFull` → `jobs.Refuse` with a named reason; server records `refused` |
| Capability panics | Recovered **on the worker**; job reported failed; agent keeps serving |
| Heavy budget full | Worker blocks on a context-aware acquire — never the reader, never unbounded |
| Unknown type / bad signature | Unchanged: refused inline, in wire order |
| Disconnect | Running and queued jobs continue; results buffer via the outbox; `OnDisconnect` still fails pending `chunkreply` waits |

The disconnect row was **false as first implemented** and is now measured. A job
submitted with the caller's context inherited the transport's per-connection
one, which `serve` cancels on return — harmless only while `Handle` blocked for
the job's whole duration. `Dispatcher` now requires a `BaseContext` (the
process context, passed from `main.go`) and `Submit` takes no context at all;
`TestAJobSurvivesADisconnectAndItsResultArrivesAfterReconnect` drops the
gateway connection under three running and budget-queued heavy jobs and
requires all three answers after the reconnect.

---

## 5. Testing

The harness the branch review asked for: `transport.Run` + `runner.Handle` +
`Dispatcher` + a real capability, over the existing `newFakeGateway`. Building it
is the **first** task of this cycle — without it the fix has nothing to turn
green.

**The falsifier first.** A capability that blocks on a correlated reply which
only arrives after the job starts. This test **must fail against today's code**
and pass after the change. If it does not fail first, it is not testing the
deadlock, and that is to be reported rather than adjusted until green.

Then, each with the observation that would show it broken:

1. **Reader stays free** — a second frame is received and processed while a long
   job runs. *Broken if:* it is only processed after the first job completes.
2. **Panic does not kill the agent** — capability panics, job reports failed,
   next frame still served.
3. **Serialized within a capability** — two `backup.*` execution windows never
   intersect.
4. **Concurrent across capabilities** — `inventory.collect` completes while
   `patch.apply` still runs.
5. **Queue full refuses visibly** — the refusal reason reaches the server;
   nothing is silently dropped.
6. **Heavy cap holds** — N+1 heavy jobs, never more than N concurrent.
7. **Spawn/exit race** — burst submit-and-drain under `-race`.
8. **Zero goroutines at idle** — count returns to baseline after jobs drain.
9. **`HeavyKeys` integrity** — every entry maps to a registered job type.

**These tests run in CI on every push.** They are ordinary Go tests under
`agent/internal/`, and `.github/workflows/ci.yml`'s `agent` job runs
`go test ./... -count=1` (alongside `gofmt`, `go vet`, `staticcheck` and the
cross-compile matrix). An earlier draft of this section said the opposite; it
was wrong, and this cycle needed no CI change because the job already covered
the tree it added to.

**What is not gated is `-race`.** The CI test step runs without it, and
deliberately: the race detector allocates shadow memory per allocation, which
would corrupt the RSS thresholds the idle-budget step measures (`ci.yml`, "No
-race"). The `-race` verification of everything above is therefore local only,
and re-running it is on whoever changes this code.

**And a caveat verified on the real run, not assumed.** The `agent` job's steps
run `gofmt` → `vet` → `staticcheck` → `test` → idle budget → cross-compile, and
`staticcheck` currently FAILS on ten pre-existing findings. Actions skips every
later step, so the test step has not actually executed on this branch — run
31333534459: staticcheck `failure`, test `skipped`. The gate is configured and
correct; it is blocked by an unrelated red step above it, and unblocking it is
its own piece of work.

---

## 6. What this does not prove

Stated so the next reader inherits the limits rather than assuming coverage.

- **Linux only, against a fake gateway.** The tests prove the concurrency model;
  they do not prove behaviour against a real gateway under real load. Deploy
  validation covers that, and is the operator's step.
- **Pong delivery is asserted by proxy.** Ping/pong is internal to
  `coder/websocket`. Test 1 asserts the observable property — the reader
  continues processing frames during a long job — not the pong frame itself.
- **`backup.run` still will not complete.** `Options.OpenSnapshot` remains nil
  and `defaultOpenSnapshot` refuses `vss`/`lvm`/`apfs`, so a stock binary still
  refuses every `backup.run` — now for **one** clearly-stated reason instead of
  two. See §7.
- **No load or soak testing.** Queue depths and the heavy cap are chosen by
  reasoning, not measurement. The first real deployment is what will show
  whether they are right.

---

## 7. Next cycle: snapshot providers

Decided during this brainstorm, recorded so it is not re-litigated: the
`OpenSnapshot` contract and **all three** platform providers — Windows VSS,
macOS APFS, Linux LVM — are designed and built as one unit in the next cycle.
Designing the interface against one platform and retrofitting two others
produces an interface that fits the first and fights the rest.

Known costs to carry into that cycle:

- **CI cannot execute two thirds of it.** The runner is Linux. `ci.yml` does
  `GOOS=windows go vet` and `go test -c` — compile-only; `go test ./...` never
  executes a `//go:build windows` file. macOS is not built at all.
- **No Windows or macOS endpoint has ever run this agent.** A VSS provider is
  not just a new capability; it is the first execution of that platform path.
- Each platform is a distinct mechanism with a distinct privilege model: VSS via
  COM under SYSTEM; APFS via `tmutil`/`mount_apfs` under root plus a Full Disk
  Access grant that must be pushed by MDM; LVM via `lvcreate -s`, which only
  applies to LVM-backed volumes.
