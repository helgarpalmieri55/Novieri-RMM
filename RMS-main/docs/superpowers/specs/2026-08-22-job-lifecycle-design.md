# Job lifecycle verbs — cancel and retry — design

**Date:** 2026-08-22
**Layer:** platform kernel (`app/jobs/`, `app/gateway/`, `agent/internal/jobs/`)
**Closes:** recommendation 1 of `docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`
— findings **B41** (BLOCKER) and **A33** (HIGH).
**Status:** approved 2026-08-22, with the operator taking §9's three
recommendations. **All three cycles are built** (2026-08-26).

- **Cycle 1** -- the `cancelled` enum value and the three columns (migration
  0071), `POST /jobs/{job_id}/cancel` withdrawing a `queued` job, §3.3's
  delivery-marking reorder and its row locks, §3.4's sweep guard, the two audit
  events, and the console's badge and copy.
- **Cycle 2** -- the `job.cancel` job type and its two exported schemas, the
  agent capability (`agent/internal/capabilities/jobcancel`), the runner's
  bounded cancellation state (`agent/internal/jobs/interrupt.go`), §3.5's
  result mapping, the `cancellable` flag from §9.1, the three allowlist
  entries, and the Go CI tests including §7's test 19.
- **Cycle 3** -- `retry_of_job_id` (migration 0076), `POST
  /jobs/{job_id}/retry`, the `retry_relinker` hook and its partition test, RBAC
  via `dispatch_permission_for`, and relinkers for `app.deploy` and
  `script.run` per §9.3(b).

**§11 records the four places this document turned out to be wrong or
incomplete, and what was built instead.** Read it before treating any
individual section below as the last word.

---

## 1. The problem, stated exactly

`app/api/v1/jobs.py` has three routes: `POST ""`, `GET ""`, `GET "/{job_id}"`.
The signed-job engine has **no lifecycle verbs at all**. A job that is queued
for a machine that has been off for a week cannot be withdrawn; a job that
failed cannot be re-run; a `script.run` executing on the wrong device cannot be
stopped. The console states the limit rather than faking it
(`JobsPage.tsx:313-316`, tested at `JobsPage.test.tsx:264`), which was the right
call and is not a fix.

Two facts make this more than a pair of CRUD routes.

**Cancel is a command to an endpoint.** CLAUDE.md invariant 1: *"Every job the
agent executes must be cryptographically signed by the server and verified by
the agent before execution. No unsigned command path exists, ever."* A
mechanism that makes an endpoint stop what it is doing is a command to that
endpoint. It is signed, or it does not exist.

**The wire is a contract.** A fleet is always mixed-version. Whatever cancel
becomes, an agent built six months ago has to do something defined when it
arrives, and the control plane has to behave correctly when it cannot know
whether the agent understood.

---

## 2. What the code can and cannot do today

Read before proposing anything; every claim below is from the source.

**The server's job state machine** (`app/jobs/engine.py::_ALLOWED`) is
`queued → {dispatched, running, succeeded, failed, expired, refused}`,
`dispatched → {running, succeeded, failed, expired, refused}`,
`running → {succeeded, failed, refused}`, and **terminal states never
transition** — `succeeded`, `failed`, `expired`, `refused` all have an empty
allowed-set. `JobStatus` (`app/models/core.py:297`) is a Postgres enum named
`job_status` with those seven values.

**Delivery bookkeeping is asymmetric, and it matters here.**
`GatewayServer._drain_queued` marks a job `dispatched` *inside* the transaction
and *then* writes to the socket. `DispatchSubscriber` does the reverse: it
`await ws.send_text(data)` and *then* calls `on_delivered`, which flips
`queued → dispatched`. So on the live-dispatch path there is a window in which
the envelope is on the wire and the row still says `queued`. Neither path takes
a row lock, so two transactions can both read `queued` and both write.

**There is no cancellation channel on the agent, at all.** `Runner.run`
(`agent/internal/jobs/runner.go:220`) derives `jobCtx` from the dispatcher's
`BaseContext` with a deadline of the job's own `expires_at`, and nothing else
can cancel it. `Dispatcher` holds per-capability queues and a heavy semaphore;
a queued item cannot be removed from a channel, and `exec1` abandons a heavy
job only when the *process* is stopping. Capability responsiveness to `ctx` is
uneven and known: `backup`, `netsweep`, `site` and `logs` check `ctx.Err()` in
their loops; `deploy` and `patch` shell out through `exec.CommandContext` with
a `WaitDelay`, so cancelling kills the direct child and — as both files record
from measurement — not necessarily its descendants; `inventory` and `provision`
check nothing.

**An unknown inbound `kind` is dropped in silence.** `wire.Decode`
(`agent/internal/wire/wire.go`) returns `ErrKind`; `cmd/agent/main.go:344`'s
`inbound` falls through to `runner.Handle`, which decodes again, logs
`"undecodable frame from gateway; dropped without answer"` and **returns
without sending anything**. The connection survives. So a new server→agent kind
is safe for an old fleet and produces *no signal whatsoever* — which is the
worst possible property for a verb whose whole difficulty is knowing what the
endpoint did.

**An unknown job TYPE is refused, loudly and on the record.** `Verifier.Verify`
looks the type up in the capability registry and refuses `unknown_type` before
anything runs; `Runner.refuse` sends a `job_result` with `state: "refused"`;
`JobEngine.refuse` writes `jobs.refused` into the hash-chained audit log. This
is already the documented compatibility story for `agent.cert.renew`
(`docs/kernel/wire-protocol/README.md`, "New control plane → old agent").

**Retry half-exists and cannot serve an operator.**
`JobEngine.resign_for_retry` re-signs a job in place with a fresh nonce,
`issued_at`, `expires_at` and `attempts += 1`, and runs the type's
`params_refresher`. It is called from exactly one place — `app/worker.py:252`,
the stuck-dispatch sweep — and it **raises `InvalidTransition` for any job that
is not `queued` or `dispatched`**, so it cannot re-run a `failed`, `expired` or
`refused` job. That is the case an operator has.

**Module records are keyed on `jobs.id`.** `ingest_deploy_result` finds its row
with `DeploymentResult.job_id == job.id` and, when there is none, "is skipped
gracefully rather than raising". Patch, backup and deploy all carry
`{"job_id": str(job.id)}` through their audit and UI events. IPAM is the
exception: `sweep_run_id` travels in the *signed params*. This asymmetry
decides §6.

---

## 3. Decisions — cancel

### 3.1 Cancel is a signed job of a new type, `job.cancel`. It is not a new wire kind.

The one server→agent kind that carries an instruction is `job` (`wire.go`'s own
comment: the two grant kinds "answer a question this agent asked while running
a signed job, and carry no instruction"). Cancel carries an instruction, so it
is a job: signed over canonical bytes, verified against the pinned Ed25519 key,
device-bound, nonce-checked, freshness-checked. Invariant 1 is satisfied by
construction rather than by a second mechanism that has to be argued about.

**Params and result** (new schemas under `docs/kernel/wire-protocol/`, exported
by `scripts/export_wire_schema.py` like every other pair):

```
JobCancelParams   { target_job_id: str }                      # server → agent, inside a signed JobEnvelope
JobCancelResult   { target_job_id: str,
                    outcome: "interrupted" | "dropped_before_start"
                           | "already_finished" | "unknown" }  # agent → server, an ordinary JobResultMsg
```

`JobResultMsg.state` for a `job.cancel` is `succeeded` whenever the agent
carried out its instruction — including `unknown`, which is a true and useful
answer. **No enum on the wire is widened and no `kind` is added.** The whole
wire change is two new schema files for a new job type, which is the additive
shape this protocol already has precedent for twice over.

**Rejected — a `job_cancel` wire kind.** §2 measured what an old agent does with
one: drops it, logs locally, answers nothing. The control plane would then have
to distinguish "old agent, cancel impossible" from "new agent, job already
finished" by timeout, i.e. by guessing. The job-type route turns exactly that
question into a `refused: unknown_type` on the record.

**Rejected — adding `"cancelled"` to `JobResultMsg.state`.** It is a widening
the server can accept safely, but the direction that bites is a *newer agent*
against an older control plane, where `parse_agent_message` drops the whole
message and the job hangs to expiry with only a `logger.warning`. §3.5 gets the
same information without touching that enum.

### 3.2 The state machine

One new value on the `job_status` enum — `cancelled`, terminal — and three new
columns on `jobs`. Not two new states: a job that is running while a cancel is
in flight **is still running**, and replacing that with a `cancelling` badge
would delete a true fact to display a pending one.

| column | type | meaning |
|---|---|---|
| `cancel_requested_at` | `timestamptz NULL` | a human asked, at this time |
| `cancel_requested_by` | `uuid NULL → users.id` | who asked. Not returned by any API route — the actor is in the audit log, and `JobOut` discloses no user id today |
| `cancel_outcome` | `varchar(24) NULL` | `requested` \| `withdrawn` \| `dropped` \| `interrupted` \| `too_late` \| `unsupported` |

Transitions added to `_ALLOWED`:

```
queued     → cancelled      # withdrawal: nothing was ever written to a socket
dispatched → cancelled      # the endpoint says it dropped the job before starting it
running    → cancelled      # the endpoint says it interrupted it
cancelled  → {}             # terminal, like every other terminal state
```

Per source state, cancel **means** something different, and the route says which:

- **`queued`** — *withdrawal*. Server-side only; no `job.cancel` job is created.
  The row moves straight to `cancelled` with `cancel_outcome = "withdrawn"`.
  This is the only outcome the platform can assert on its own authority, and
  §3.3 is what makes the assertion true.
- **`dispatched`** — *asked, not confirmed*. `cancel_requested_at` is stamped,
  `cancel_outcome = "requested"`, status is **unchanged**, and a `job.cancel`
  job is created and published for the same device.
- **`running`** — identical to `dispatched`. The endpoint is mid-execution and
  the server has no authority over it whatsoever.
- **`succeeded` / `failed` / `expired` / `refused` / `cancelled`** — **refused,
  `409 Conflict`**, with a message naming the state ("this job finished at
  14:02 and cannot be cancelled"). Not a 404 (the job exists and the caller may
  read it), not a silent 200 (a no-op reported as a success is the class of
  defect this programme has been closing).
- A second cancel on a job that already has `cancel_requested_at` — **409**,
  naming the time of the first request. It does not create a second
  `job.cancel`.

### 3.3 Delivery is marked *before* the socket write, so `queued` means "never sent"

`DispatchSubscriber` currently sends and then marks. That makes "queued"
un-provable, and a withdrawal built on it would be the "Cancelled when it means
asked" defect in its rarest and most misleading form. **Both dispatch paths
mark `dispatched` before writing to the connection**, matching what
`_drain_queued` already does.

The cost is stated rather than discovered: a job recorded `dispatched` whose
write then failed. That case already exists and is already handled — the worker
sweep re-signs and republishes a `dispatched` job that has been silent for
`STUCK_DISPATCH_AFTER` (10 minutes). We are trading a rare invisible
over-execution for a rare visible re-dispatch.

Both the cancel route and `_on_delivered` take `SELECT … FOR UPDATE` on the job
row and re-read the status inside the lock. Without it, under READ COMMITTED,
the cancel and the delivery mark both read `queued` and the later writer wins
silently — `_transition`'s guard never fires, because each transaction is
holding a row it read before the other committed.

### 3.4 The stuck-dispatch sweep must not resurrect a cancelled job

`sweep_org` (`app/worker.py:237`) re-signs and republishes every `dispatched`
job older than 10 minutes with `attempts < max_attempts`. A job whose cancel
was requested and not yet confirmed is exactly such a job. **Its query gains
`Job.cancel_requested_at.is_(None)`.** This is the quietest defect in the
design — the operator cancels, and ten minutes later the platform dispatches it
again — and it gets its own test.

### 3.5 The endpoint's answer, and who writes the terminal state

Two writers, and the rule between them is unambiguous.

**The `job.cancel` result handler** (a `JobType.result_handler`, running in the
gateway process inside the tenant transaction) reads the outcome and closes the
target when the target will never speak again:

| `JobCancelResult.outcome` | target job written to | `cancel_outcome` |
|---|---|---|
| `dropped_before_start` | **`cancelled`** — it never ran, and nothing else will speak for it | `dropped` |
| `interrupted` | left alone — the target sends its own result, and the rule below maps it | `interrupted` |
| `already_finished` | left alone — it reached a terminal state on its own | `too_late` |
| `unknown` | left alone — this agent has never seen the job | `too_late` |

**The target job's own result** arrives through the ordinary path. When
`cancel_requested_at IS NOT NULL`:

- a `failed` or `refused` result is recorded as **`cancelled`**, and the agent's
  result body is stored verbatim in `jobs.result` so nothing is lost.
  `cancel_outcome` is set to `dropped` when the refusal reason is the agent's own
  cancellation constant (§3.8's ordering race) and to `interrupted` otherwise —
  the distinction is "it never started" versus "it was stopped mid-flight", and
  §3.6 shows the operator a different sentence for each;
- a `succeeded` result is recorded as **`succeeded`**. The job finished before
  the interrupt landed. The console says so. This is the case the design exists
  to tell the truth about.

**The target job id comes from the params the server signed, never from the
result body.** A compromised endpoint may not name a different job to close.
The handler additionally requires the target to carry the same `org_id` and the
same `target_id` (device) as the cancel job — the same rule
`_load_job_for_device` applies to every other agent message.

**Expiry is unchanged.** A job whose cancel was requested and that then reaches
`expires_at` with no word from the endpoint is written `expired`, not
`cancelled`. Nobody ever told us it stopped.

### 3.6 What the console says, and what the audit log records

The console renders three distinct things and never conflates them:

| stored state | console |
|---|---|
| `cancelled`, `cancel_outcome = "withdrawn"` | **Cancelled** — "withdrawn before it was sent to the device" |
| `cancelled`, `cancel_outcome = "dropped"` | **Cancelled** — "the endpoint discarded it before it started" |
| `cancelled`, `cancel_outcome = "interrupted"` | **Cancelled** — "stopped on the endpoint at 14:07" |
| `running`/`dispatched` + `cancel_requested_at` | **Cancel requested** — "asked at 14:02; the endpoint has not confirmed" |
| `succeeded` + `cancel_requested_at` | **Succeeded** — "the cancel arrived too late; the job completed" |
| `cancel_outcome = "unsupported"` | **Cancel not supported** — "this endpoint's agent is too old to be interrupted; the job will run or expire" |

The word "Cancelled" appears only where the platform has evidence. `Badge`
variants live in `frontend/src/modules/jobs/jobState.ts`, whose
`Record<JobStatus, BadgeVariant>` is exhaustive — adding `cancelled` to the
`JobStatus` union makes the compiler name every place that has to decide.

Audit events, both `actor_type` correct and both inside the tenant transaction:

- **`jobs.cancel_requested`** — `actor_type="user"`, `actor_id=ctx.user_id`,
  `target_type="job"`, metadata `{type, device, from_status, mechanism:
  "withdrawn"|"agent_asked", cancel_job_id}`. `mechanism` is the load-bearing
  field: it is the record of whether the platform stopped this itself or merely
  asked.
- **`jobs.cancelled`** — written where the terminal state is written,
  `actor_type="system"` for a withdrawal and `"agent"` for an endpoint
  confirmation, metadata `{type, device, cancel_outcome}`.
- **`jobs.cancel_unsupported`** — `actor_type="agent"`, written when the
  `job.cancel` job comes back `refused: unknown_type`. This is the mixed-version
  case on the record rather than in a log file.

### 3.7 Mixed-version compatibility, in both directions

**New control plane → old agent.** The `job.cancel` envelope is an ordinary
signed job. The old agent's registry has no `job.cancel` capability, so
`Verifier.Verify` refuses `unknown_type` **before anything runs** and sends a
`job_result`. The control plane sets `cancel_outcome = "unsupported"`, writes
`jobs.cancel_unsupported`, appends a `job_logs` line on the target, and the
console says the endpoint cannot be interrupted. **The operator learns the
truth, from the endpoint, in seconds.** The target job is untouched and runs or
expires exactly as it would have. No new field is required of the old agent and
no message it sends is rejected.

**New agent → old control plane.** The new agent gains a capability and a
`cancelled` set. It never *initiates* anything: `job.cancel` arrives only if a
control plane sends one, and an old one never does. `JobCancelResult` is only
emitted in reply to a `job.cancel`. There is no request an older control plane
would answer with a 404 or a validation error, and the agent behaves exactly as
it did before the capability existed.

**Withdrawal needs no agent at all.** The most common cancel — a job queued for
a machine that is off — is served entirely server-side, at every agent version
in the field, on day one.

### 3.8 The agent side

Two pieces of state on `Runner`, both bounded, both guarded by one mutex, and
**neither one a goroutine or a timer** — the idle budget is a hard requirement
(CLAUDE.md), so nothing here may wake up:

- `running map[string]context.CancelFunc` — populated in `Runner.run` before
  `invoke`, deleted in a `defer`. One entry per job actually executing, so it is
  bounded by the dispatcher's concurrency, not by fleet history.
- `cancelled` — a bounded FIFO set of job ids (256 entries, oldest evicted on
  insert; no sweep, no expiry timer). It closes the ordering race in which a
  `job.cancel` overtakes its target.

**The suppressed target answers for itself, and this is not optional.** When a
`job.cancel` arrives before its target, the cancel job's own result is `unknown`
(this agent has never seen that id) and the server can write nothing. The target
then arrives, is matched against the set, and is **never invoked** — so if the
agent stayed silent the job would hang until `expires_at` and the operator would
see `expired` for a job they cancelled. Instead the runner sends an ordinary
`job_result` for the *target*, `state: "refused"`, with a new
`RefusalReason` constant whose text is this agent's own sentence (`safeReason`
echoes nothing job-supplied). §3.5's rule — a `refused` result on a job with
`cancel_requested_at` set is stored as `cancelled` — then closes it correctly,
and the server upgrades `cancel_outcome` from `too_late` to `dropped`. No wire
enum is widened: `refused` is a state `JobResultMsg` already carries.

`Dispatcher` is untouched. `capabilityKey("job.cancel")` is `"job"` — a queue of
its own, and `"job"` is deliberately **not** added to `DefaultHeavyKeys` — so a
cancel is never queued behind the heavy job it is cancelling and never waits on
the heavy semaphore. That property is the reason the type is named `job.cancel`
and not, say, `backup.cancel`, and it gets a test.

The capability is wired with the existing bind-after-construct pattern that
`backup.Process.Bind` already uses in `cmd/agent/main.go`, because the registry
must exist before the runner and the capability needs the runner's interrupt
handle.

**Interruption is best-effort and the spec says so.** Cancelling a job's context
stops a capability that checks `ctx`; for `deploy` and `patch` it kills the
direct child process and, as both files already record from measurement on this
project, not necessarily its descendants. `JobCancelResult.outcome =
"interrupted"` therefore means *the agent asked the capability to stop*, and the
target job's own result is what says whether it did. §9.1 is the operator
question that follows from this.

---

## 4. Decisions — retry

### 4.1 A retry is a NEW job row, never a re-dispatch of the old one

`resign_for_retry` mutates in place: same `id`, new nonce, `attempts += 1`,
`result` and `finished_at` overwritten. That is right for the stuck-dispatch
sweep, where the job never reached a terminal state. It is wrong for an
operator retry of a `failed` job, for three reasons that are properties of the
code and not preferences:

1. It would move a **terminal** state back to `queued`. "Terminal states never
   transition" is a stated invariant of `_ALLOWED` and the thing that makes a
   job's outcome final.
2. `settle_action_for_job` settles an `ai_action` on the *first* terminal state
   and is idempotent on `state == 'dispatched'`. A job that un-terminals leaves
   the AI confirmation history saying an action settled while its job runs
   again — a lie in exactly the record that exists to be trustworthy.
3. The first attempt's `result` is destroyed. The audit chain keeps the
   `jobs.failed` event, but the failure body an operator retries *from* is gone
   the moment they retry.

So: `POST /jobs/{job_id}/retry` creates a new `Job` row through
`JobEngine.create` — new id, new nonce, fresh signature, params re-validated
against the type's model and passed through the type's `params_refresher` — with
a new column `retry_of_job_id uuid NULL → jobs.id`. The original row is never
written to. Replay is impossible: the signature covers a different `id` and a
different `nonce`, and the agent's nonce store refuses the old one anyway.

`jobs.dispatched` for the new job carries `{"retry_of": "<old id>"}` in its
metadata, so the chain shows two acts and their relationship.

### 4.2 Which terminal states are retryable

| from | retryable | why |
|---|---|---|
| `expired` | **yes** | never confirmed to have run; the device was off. The commonest case. |
| `failed` | **yes** | it ran and the handler raised. The operator decides whether to run it again. |
| `refused` | **yes** | `queue_full` is transient by construction; the others refuse again, cheaply, audibly, and on the record. |
| `cancelled` | **yes** | a fresh deliberate act. The console confirms, naming the earlier cancellation. |
| `succeeded` | **no — 409** | re-running a succeeded job is a **new dispatch**, not a retry, and it belongs on the route that owns the record and the bounds. |
| `queued` / `dispatched` / `running` | **no — 409** | not finished. The stuck-dispatch sweep already owns this case. |

### 4.3 Idempotency is the module's, and the kernel refuses to pretend otherwise

"What stops a retried `app.deploy` from installing twice?" — nothing in the
kernel does, and nothing in the kernel can. `jobs.idempotency_key` exists on the
model and is written by no code in the repository. The answer lives in the
capability (`app.deploy` carries a `detection_rule`) and in the module's own
record (`DeploymentResult`), which is precisely why §4.4 refuses to re-dispatch
a module-governed type from the kernel route.

### 4.4 Retry is governed exactly as dispatch is, and for the identical reason

`POST /jobs` refuses module-governed types because the bypass "is not a missing
permission — it is a missing RECORD" (`direct_dispatch.py`). Retry has the same
hole and one more: a new job id **orphans the module's record**.
`ingest_deploy_result` looks up `DeploymentResult.job_id == job.id` and, finding
nothing, "is skipped gracefully" — so a kernel retry of a failed `app.deploy`
would install the package and leave the deployment showing pending forever.
IPAM is safe by accident (its `sweep_run_id` is in the signed params); deploy,
patch and backup are not.

Therefore:

- The retry route accepts `DIRECTLY_DISPATCHABLE_JOB_TYPES` — `hello`,
  `inventory.collect`, `logs.ship` — plus any type whose registration declares a
  **`retry_relinker`**: a new optional `JobType` hook,
  `async (session, old_job, new_job) -> None`, alongside the three that already
  exist (`result_handler`, `params_refresher`, `params_redactor`). Deploy's is
  two lines: repoint `DeploymentResult.job_id`.
- Everything else is **403**, with a message naming the module's own re-run
  route — the same shape and the same voice as `MODULE_GOVERNED_REASONS`, so the
  console can render "Retry on the deployment" as a link rather than a dead
  button.
- **Deny by default**, asserted as a partition against the live registry, so a
  module registering a new job type breaks a test rather than picking a side.

§9.3 is the operator question about how far this reaches on day one.

---

## 5. RBAC, tenancy, and the four allowlists

**Retry requires `dispatch_permission_for(job.type)`**, the existing map in
`app/jobs/dispatch_permissions.py` — the permission the *owning route* requires.
For the three kernel-owned types that is `jobs.dispatch`, so nothing changes for
a technician; for anything else it forecloses the escalation in which
`jobs.dispatch` re-runs a `patch.apply`. `dispatch_permission_for` returns
`None` for an unclassified type and for one with no human dispatch path, and
**`None` means refuse** at this call site as at every other.

**Cancel** is §9.2, an open question. The recommendation is the same rule —
whoever may start it may stop it — and it needs no new permission code. Note
what it implies and accept it deliberately: the three `NO_HUMAN_DISPATCH_PATH`
types (`health.policy.apply`, `agent.cert.renew`, `agent.cert.install`) become
**uncancellable from the console**, which is correct — they are minted by the
kernel's own state machines from state the kernel minted, and an operator
cancelling a certificate rotation is cancelling the thing that keeps the
endpoint reachable.

**Tenancy, and the trap.** No new table, so no new RLS policy: three columns on
`jobs`, which already carries `FORCE ROW LEVEL SECURITY` and the
`tenant_isolation` policy. That is exactly the situation
`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` §0 warns about —
everything served through `org_session` runs as `app_user` (**NOBYPASSRLS**), so
deleting the application's `WHERE org_id` predicate turns **no route test red**.

Both new routes carry an explicit `Job.org_id == ctx.org_id` predicate, and the
tenancy test for each uses idiom 3 from that audit: repoint
`env.app.state.app_engine = env.admin_engine` and drive the **real route**, so
the application predicate is the only thing standing between the caller and a
neighbour's job. A test that does not do this asserts nothing and must not be
written; the audit's naming rule applies — a test that drives `org_session` and
asserts invisibility is testing RLS and is named `..._is_hidden_by_rls`.

**`job.cancel` must be classified in four places**, because four deny-by-default
allowlists each assert a partition over the live registry and a new type breaks
all of them at once:

| allowlist | entry | why |
|---|---|---|
| `MODULE_GOVERNED_REASONS` | yes, message naming **no route** | it satisfies the `direct_dispatch` partition; `test_dispatch_permissions.py`'s `_EXPECTED_UNPARSED` gains `job.cancel` alongside `health.policy.apply` and the two `agent.cert.*` types |
| `NO_HUMAN_DISPATCH_PATH` | yes | there is no route a human calls to dispatch a `job.cancel`: it is minted by the cancel route from a job the kernel already signed, exactly like the certificate-rotation legs |
| `APPROVABLE_JOB_TYPES` | **no** — `NON_APPROVABLE_REASONS` gains an entry | a machine may not cancel fleet work |
| `_DESTRUCTIVE_JOB_TYPES` | **no** | it delivers no payload and changes no state on the host; what it does is *stop* |

The consequence of rows 2 and 3 together: **`job.cancel` can never be reached by
the AI layer.** That is deliberate and worth stating — a cancel is a denial
verb, and the AI surface is not where denial verbs belong.

---

## 6. Migration

One migration, forward-only, and two things in it need care:

- `ALTER TYPE job_status ADD VALUE 'cancelled'`. Postgres 16 (`deploy/docker-compose.yml`)
  allows this inside a transaction provided the new value is not *used* in the
  same transaction. This migration writes no rows, so one migration is enough —
  but nothing else may be added to it that inserts or casts `'cancelled'`.
- **The downgrade has to work.** CI's `migrations` job runs
  `upgrade head → downgrade base → upgrade head` (CLAUDE.md), and Postgres
  cannot remove an enum value. The downgrade recreates `job_status` without
  `cancelled` after mapping any `cancelled` row to `failed`, drops the four new
  columns, and is written to run on a database that has them. This is not the
  `0001` carve-out: this is a new migration whose `downgrade()` must be correct
  the first time.

Columns: `cancel_requested_at`, `cancel_requested_by`, `cancel_outcome`,
`retry_of_job_id`. No new index — `ix_jobs_org_status` already serves the
console's filters, and the worker sweep's new `cancel_requested_at IS NULL`
predicate rides an existing `status = 'dispatched'` scan.

---

## 7. Verification

Every claim below is a test that must be able to fail.

**Server — cancel**

1. Cancelling a `queued` job writes `cancelled`/`withdrawn`, creates **no**
   `job.cancel` job, and the job is no longer returned by
   `queued_for_device` — i.e. a reconnecting agent never sees it.
2. Cancelling a `dispatched` job leaves `status = dispatched`, stamps
   `cancel_requested_at`, and creates exactly one signed `job.cancel` job for
   the same device whose params name the target.
3. Cancelling a `succeeded`, `failed`, `expired`, `refused` or `cancelled` job
   is a 409 naming the state; a second cancel is a 409 naming the first request.
4. A `failed` result for a job with `cancel_requested_at` set is stored as
   `cancelled` **with the agent's result body intact**; a `succeeded` result for
   the same job is stored as `succeeded`.
5. `sweep_org` does **not** re-sign or republish a `dispatched` job whose cancel
   was requested (§3.4).
6. Concurrency: a cancel and `_on_delivered` racing on the same `queued` row
   produce exactly one winner and never a job that is both `cancelled` and
   delivered (`FOR UPDATE`, §3.3).
7. A `job.cancel` result naming a target in another org, or on another device,
   is discarded and audited — it never writes the target row.
8. Tenancy, on the admin engine (§5): a cancel for a neighbour org's job is a
   404, and deleting the `org_id` predicate turns this red.

**Server — retry**

9. Retrying a `failed` job creates a new row with a new id, a new nonce, a
   different signature, `retry_of_job_id` set, and leaves the original row
   byte-for-byte unchanged.
10. Retrying a `succeeded` job is a 409; retrying a `running` job is a 409.
11. Retrying `patch.apply` (or any governed type with no `retry_relinker`) is a
    403 whose message names the module route.
12. Retry is refused to a caller who holds `jobs.dispatch` but not the type's
    own `dispatch_permission_for` code.
13. The partition test over the live registry fails when a new job type is
    registered without being classified.

**Agent — Go CI, which runs on every push**

14. A `job.cancel` for a running job cancels that job's context and reports
    `interrupted`; the target job reports its own terminal state.
15. A `job.cancel` for a job id the agent has never seen reports `unknown` and
    touches nothing.
16. A `job.cancel` arriving **before** its target: the cancel answers `unknown`,
    the target is never invoked, **and the agent sends a `job_result` for the
    target** with `state: "refused"` — the silence in this ordering is the
    failure mode, so the test asserts the message, not the suppression.
17. `capabilityKey("job.cancel") == "job"`, `"job"` is not in
    `DefaultHeavyKeys`, and a cancel dispatched while the heavy budget is full
    executes immediately.
18. The bounded `cancelled` set evicts at its cap and never grows without bound.
19. **Compatibility, asserted rather than asserted-about:** a registry built
    *without* the cancel capability, given a valid signed `job.cancel`, produces
    a `job_result` with `state: "refused"` and reason `unknown_type` — this is
    the old-fleet path in §3.7 and it is the single most important test here.

**Idle budget.** The agent gains two maps and one mutex, no goroutine and no
timer. The falsifier: the existing idle benchmark shows any wake-up, or any
resident-memory growth proportional to jobs seen, after N cancel cycles. Until
that benchmark runs green with the capability registered, the footprint claim is
a design target and is described that way.

**Falsifiers for this design's own claims**

| claim | falsifying observation |
|---|---|
| A withdrawn job never reaches an endpoint | an agent executing a job the console shows as `cancelled`/`withdrawn` |
| The console never says "Cancelled" without evidence | a `dispatched` job rendering as Cancelled before any endpoint answer |
| An old agent answers a cancel loudly | a `job.cancel` to a pre-cancel agent that produces no `job_result` |
| A cancel is never queued behind the job it cancels | a `job.cancel` whose execution waits on the heavy semaphore |
| A retry cannot orphan a module record | a retried `app.deploy` whose `DeploymentResult` never leaves pending |
| Cancel is unforgeable | a `job.cancel` result closing a job named anywhere other than the signed params |

---

## 8. Known limits, stated rather than discovered later

- **Interruption is best-effort.** A cancelled `script.run` kills the direct
  child; a grandchild it spawned may survive (`deploy/exec.go`, `patch/exec.go`,
  both from measurement on this project). `interrupted` means asked, and the
  target job's own result is the evidence.
- **Windows COM calls take no cancellation token.** `patch/wua_windows.go` and
  `health/posture_windows.go` both say so. A `patch.apply` inside a WUA search
  cannot be interrupted until that call returns.
- **`inventory` and `provision` check no context** and will run to completion.
  Making a capability interruptible is that capability's work, not this design's.
- **A cancel to an offline device is queued like any other job** and is drained
  on reconnect, by which time the target has usually expired. The agent answers
  `unknown`; the console already shows `expired`.
- **`jobs.idempotency_key` stays unused.** §4.3 explains why it is not the answer
  to double execution, and inventing a use for it here would be a second
  half-mechanism.

---

## 9. SETTLED — the operator took all three recommendations (2026-08-22)

Recorded rather than left reading "OPEN": §9.2 is the one that bound cycle 1
and it is implemented as recommended — cancel is gated by
`dispatch_permission_for(type)`, no new permission code. §9.1 and §9.3 govern
cycles 2 and 3 and are not exercised by anything built yet.

### 9.1 Which job types may be *interrupted* after dispatch

Withdrawal (a `queued` job) is safe for every type — nothing ran — and stays
available for every type whatever is decided here. Interruption is not safe by
default: a `patch.apply` stopped mid-install leaves a half-patched machine, and
a `backup.run` stopped mid-run leaves a partial repository the module has to
reconcile. The mechanism is decided (a `cancellable` flag on `JobType`, deny by
default, gating **only** the `dispatched`/`running` path; a cancel on a
non-cancellable type in one of those states is **409 with a reason** rather than
a cancel that quietly does nothing). The day-one set is a product judgement.

| option | what it means |
|---|---|
| **(a) every type** | maximum operator control; accepts half-finished installs |
| **(b) interruptible only where the module claims it** | *recommended* |
| **(c) withdrawal only, no interruption at all in cycle 1** | ships fastest, closes B41 only for queued jobs |

**Recommendation: (b)**, with `script.run`, `net.sweep` and `backup.run`
interruptible on day one — those three already check `ctx.Err()` in their own
loops (§2), so the flag would be claiming something the code does — and
`patch.apply`, `app.deploy`, `provision.enroll`, `backup.restore` and
`remote.session.open` **not**: a half-applied patch or a half-written restore is
worse than the job you wanted to stop. `inventory.collect` is deliberately not on
the list either, for the opposite reason — it checks no context (§8), so the flag
would be a claim its code does not honour. Each can be granted later, in its own
module's cycle, by the people who know what its rollback looks like.

### 9.2 Which permission gates cancel

`e25bbe7` made adding a permission code a deliberate decision, so this is asked
rather than assumed.

| option | consequence |
|---|---|
| **(a) `dispatch_permission_for(type)`** — whoever may start it may stop it | *recommended*. No new code, no escalation, symmetric with retry. Kernel-minted types become uncancellable (§5), which is correct. |
| (b) `jobs.dispatch` for everything | simplest; lets a holder of `jobs.dispatch` stop a `patch.apply` they could not have started |
| (c) a new `jobs.cancel` code | lets a service-desk tier stop fleet work without being able to start any. Costs a code, a migration, and a decision about which seeded roles hold it — and a fleet-wide cancel is a denial-of-service on patching and backup, so it is not obviously a *lower* tier than dispatch |

**Recommendation: (a).** If the operator wants a service-desk tier that can stop
but not start, that is (c) and it should be argued on its own merits, not
smuggled in as the default.

### 9.3 How far retry reaches on day one

`retry_relinker` (§4.4) is the mechanism either way; the question is who ships an
implementation in this cycle.

| option | what B41 gets |
|---|---|
| (a) kernel-owned types only — `hello`, `inventory.collect`, `logs.ship` | honest, tiny, and does not cover the jobs operators actually retry |
| **(b) kernel-owned types + relinkers for `app.deploy` and `script.run`** | *recommended*. Deploy's relinker is a two-line repoint of `DeploymentResult.job_id`; those two are the types the audit's screenshots are full of |
| (c) every governed type at once | five modules' records, five reviews, in a cycle that is already three parts |

**Recommendation: (b)**, with patch, backup, IPAM and provisioning adding
relinkers in their own cycles. Until a type has one, its Retry button is a link
to the module's own re-run route, which is a true statement about where that
capability's record lives.

---

## 10. This is three cycles, not one

Each ships something the operator can use and each can be reviewed on its own
terms. Building them as one change would put a kernel dispatch-ordering change,
a new agent capability and a new job-creation path into a single review.

**Cycle 1 — Withdrawal, and honest state.** The `cancelled` enum value, the
three columns, the migration, `POST /jobs/{job_id}/cancel` withdrawing a
`queued` job and returning 409 with a reason for everything else, §3.3's
ordering fix and its row locks, §3.4's sweep exclusion, the audit events, and
the console's badges and copy. **No wire change and no agent change**, so it
works against every agent in the field on the day it lands. Closes the half of
B41 that is a queue full of jobs for machines that are off.

**Cycle 2 — Interruption.** The `job.cancel` job type and its two schemas, the
agent capability, the running registry and the bounded cancelled set, the
result-handler mapping in §3.5, the `cancellable` flag from §9.1, the four
allowlist entries, and the Go CI tests — including test 19, the old-fleet
refusal. This is the security-relevant cycle and the one that touches the wire
contract.

**Cycle 3 — Retry.** `retry_of_job_id`, `POST /jobs/{job_id}/retry`, the
`retry_relinker` hook and its partition test, RBAC via
`dispatch_permission_for`, and whichever relinkers §9.3 settles on.

Cycle 1 is a prerequisite for cycle 2 (the state and the columns). Cycle 3 is
independent of both and could run in parallel with cycle 2 if the two touch
`app/api/v1/jobs.py` in sequence rather than at once.

---

## 11. WHERE THIS DOCUMENT WAS WRONG, and what was built instead (2026-08-26)

Recorded here rather than edited into the sections above, so that a reader can
see both what was designed and what survived contact with the code. Four items.

### 11.1 `dropped_before_start` is in the contract and this agent cannot produce it

§3.5's table maps a `JobCancelResult.outcome` of `dropped_before_start` to
`cancel_outcome = "dropped"` and to closing the target, and §3.8 specifies the
agent's cancellation state as exactly two structures: a map of jobs **actually
executing** and a bounded set of cancelled ids. **Those two statements cannot
both hold.** With that state, "sitting in the Dispatcher's queue" and "never
seen" are the same observation -- absent from the running map -- so the agent
has no way to report `dropped_before_start`, and claiming to would be a claim
its code cannot make. It answers `unknown`.

Nothing is lost, because §3.8 already provides the answer by another route and
calls it "not optional": the suppressed target reports for **itself** with
`refused: cancelled_before_start`, and §3.5's own rule upgrades the target's
`cancel_outcome` from `too_late` to `dropped`. So the operator sees "the
endpoint discarded it before it started" either way -- from the target's mouth
rather than the cancel's.

**The value stays in the schema and the server keeps accepting it**, which is
the decision worth recording. Narrowing an enum an agent may already send is a
break, and a future agent that tracks its own dispatcher queue can answer more
precisely with no server change. `internal/capabilities/jobcancel/schema_test.go`
pins the enum, including this value, and says why.

### 11.2 §6 assumed one migration; the cycle split forced two

§6 lists four columns -- `cancel_requested_at`, `cancel_requested_by`,
`cancel_outcome`, `retry_of_job_id` -- in "one migration, forward-only". §10
then split the work into three cycles, and cycle 1 shipped migration **0071**
with the three cancellation columns only, because `retry_of_job_id` belongs to
a route that did not exist yet and a column nothing writes is a column nobody
reviews. Cycle 3 therefore added migration **0076** for the fourth. Migrations
are forward-only, so this is two migrations and not an edit to a shipped one;
0076's `downgrade()` is one `DROP COLUMN` and was verified through CI's own
`upgrade head -> downgrade base -> upgrade head` round trip before it landed.

### 11.3 A retry needed a second audit event, and §4.1 named only one

§4.1 says `jobs.dispatched` for the new job carries `{"retry_of": "<old id>"}`,
"so the chain shows two acts and their relationship". That is built. It is not
sufficient on its own, and the gap is invariant #4 rather than a preference:
`jobs.dispatched` is `actor_type="system"` and is written by the **gateway at
delivery**. For the case an operator retries most -- a device that was off --
delivery is hours away or never, so a named human re-running fleet work would
have left **no record at all** until the machine came back.

So the retry route also writes `jobs.retry_requested`, `actor_type="user"`,
with `retry_of` and the original's terminal status, at the moment the human
acted. The two events answer different questions and neither replaces the
other.

### 11.4 `JobEngine.withdraw` needed its own source-state guard once §3.2 landed

Cycle 1 relied on `_ALLOWED` to keep `withdraw` honest: `queued -> cancelled`
was the only transition into `cancelled`, so a non-queued job could not reach
it. Cycle 2 adds `dispatched -> cancelled` and `running -> cancelled`, which an
endpoint's answer may legitimately write -- and that silently made `withdraw`
able to stamp `cancel_outcome = "withdrawn"`, the sentence "this never reached
a device", on a job the fleet already had in hand. `withdraw` now checks the
source state itself. The lesson generalises: a guard that works because a set
happens to be small stops working when the set grows, and `_ALLOWED` is a
transition table, not an authorization.

### 11.5 One thing §9.1 leaves open that the build did not close

§9.1's day-one interruptible set is `script.run`, `net.sweep` and `backup.run`,
and that is what shipped. **`patch.apply` and `app.deploy` are the two types an
operator is most likely to want to stop**, and both are deliberately excluded
because a half-applied patch or a half-written install is worse than the job
you wanted to stop. The console tells them so in words. Granting either is a
product decision for that module's own cycle, by the people who know what its
rollback looks like -- and it is one line plus its own tests, not a redesign.
