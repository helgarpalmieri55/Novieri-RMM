# Subnet Push Runs (GPO MSI and subnet push, Part 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An operator picks one of the organization's authorized subnets and a push credential. The server expands the subnet into the named, vouched-for machines it holds and shows each one's readiness. The operator confirms that exact list, which is bound by a SHA-256 digest. A push run then installs the agent on the ready machines: one first, then at most 50 in flight once a sign-in has succeeded. After 3 sign-in failures the run stops itself, to protect the credential from lockout, and only a person restarts it. Pause, Resume and Cancel are acts of a person, and the run never grows past what was confirmed.

**Architecture:**
- **Module and tables.** Everything lives in `backend/app/modules/agent_push/`. Migration `0200` adds two forced-RLS tables, `push_run` and `push_run_target`. A database trigger seals a run's target list once it is created.
- **Expansion.** `run_list.py` expands a prefix by re-using the existing refusal ladder (`preflight.evaluate_targets`) and the install-only checks. Those checks are extracted from `dispatch_push_batch`, unchanged, as `dispatch.pre_dispatch_checks`. Nothing is duplicated.
- **The pacer.** `runs.py` holds the run lifecycle and the pacer, `advance_run`. It takes a row lock on `push_run`. Its lockout guard (spec §1.4a) sends one target first, opens to 50 only after a sign-in has got through, and moves the run to `stopped_credential` after 3 `auth_failed` results since the run last started or resumed. Before each target it re-runs the whole ladder. It dispatches one target at a time through `dispatch.RunDispatcher`, which calls the same `_dispatch_install` that a ticked push uses.
- **Where the signing happens.** Signing and decryption stay in `dispatch.py`. That is the file the existing structural guard allows.
- **When the pacer runs.** It runs from the API when a person starts or resumes a run, and from a 60-second Celery beat. When a `push_install` job completes, the gateway's result handler records the target's outcome. It does not dispatch, because the gateway holds no vault (see R2).
- **Console.** The Devices push dialog gains a **By subnet** choice. The run pages are new routes under `/devices/push-runs`.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.x (async, typed) · Pydantic v2 · Alembic · PostgreSQL (RLS, `cidr`/`inet`, plpgsql trigger) · Celery beat · React + TypeScript (strict) · TanStack Query v5 · Vite · Vitest.

## Global Constraints

- **The CLAUDE.md invariants hold. This plan adds no exemption.**
  - **1 (signed jobs):** every install a run causes is an ordinary `agent.push_install` job. `JobEngine.create` signs it, inside `dispatch._dispatch_install`, the same function a ticked push calls. No unsigned path is added.
  - **2 / 2a(a):** the control plane never connects to a target. The job goes to the segment's site server, which `preflight._site_server_for` chooses, with no fallback. `tests/test_agent_push_dispatches_nothing.py` stays green without editing its allowlist: `runs.py`, `run_list.py` and `run_contract.py` name no signer, vault or publisher.
  - **3 (vault):** a credential is decrypted only inside `dispatch.RunDispatcher.dispatch_target`, for one target, after every bound has held. Expansion, creation, Pause, Resume, Cancel and every refusal decrypt nothing.
  - **4 (audit):** every privileged act is written to the hash-chained audit log with `audit_service.record`:
    - person-initiated: `push.run.started`, `push.run.paused`, `push.run.resumed`, `push.run.cancelled`;
    - pacer-originated: `push.refused` (one per target, with no decrypt), `push.dispatched` (with `run_id` and `on_behalf_of`), `push.run.stopped` (the lockout guard, spec §1.4a) and `push.run.completed`.
  - **5 (tenancy):** both new tables carry `org_id` under `ENABLE` + `FORCE ROW LEVEL SECURITY` with the `tenant_isolation` policy. Every query also carries an explicit `org_id` predicate.
  - **6 (RBAC):** every new route carries `require_permission("agent.push.run")`. Preview, Start and Resume also require `credentials.checkout` in the service. The pacer re-checks that the run's creator still holds both before each target.
- **2a(e) as amended 2026-09-30.** Starting, pausing, resuming and cancelling are a person's acts. The pacer only continues a confirmed run. It never:
  - creates a run;
  - inserts a target (a database trigger refuses this too);
  - chooses or changes a credential;
  - writes `state = 'running'`, or leaves `stopped_credential`.
  Its one act of its own is the safety stop: `running` becomes `stopped_credential` after `PUSH_RUN_AUTH_FAILURE_LIMIT = 3` sign-in failures (spec §1.4a; the 2a(e) note's last sentence). An AST test pins `_stop_if_locked_out` as the only writer of that state.
  The kernel gates (`direct_dispatch.MODULE_GOVERNED_REASONS`, `approved_actions.NON_APPROVABLE_REASONS`) keep refusing both push job types to every automated, scheduled or AI actor. No AI tool (`app/core/ai/tools/registry.py`) may map to an `/api/v1/agent-push` route.
- **2a(f) as amended 2026-09-30.**
  - `PUSH_RUN_MAX = 1024` ready targets per run. This counts ready machines, not the prefix's size.
  - `PUSH_RUN_CONCURRENCY = 50` in flight, and only after a sign-in has got through. Until then it is 1 (spec §1.4a).
  - One active run (running, paused or `stopped_credential`) per organization.
  - `PUSH_BATCH_MAX = 25` still bounds every ticked push, unchanged.
  - Only rows vouched for under 2a(d) are listed (`VOUCHED_PROVENANCE`, `review_state = 'approved'`).
- **The confirmed list is the authority.**
  - `list_digest` is a SHA-256 over the sorted `(device_id, address)` pairs of the **ready** rows. The canonical form is `f"{device_id}|{address}"` lines, sorted, joined by `\n`, UTF-8, lowercase hex.
  - `POST /api/v1/agent-push/runs` re-expands the subnet and refuses a mismatch with 409 `{"code": "list_changed"}`. That happens before any row is written and before any secret is touched.
  - Targets are written once, at creation. After that the database refuses any insert, and any change to which machine a target names.
- **No wire change and no agent change.** The run reuses `agent.push_install` exactly as it is: the same params, the same result and the same capability. Nothing under `agent/` or `docs/kernel/wire-protocol/` changes.
- **Tests come first in every task.** Security-relevant paths each have a test that fails if the guard is removed.
- **Commits.** Commit once per task, on the working branch. Never push to `main`. Every commit message ends with exactly these two lines:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89
  ```
- **Verification commands.** Run the relevant ones at the end of every task, and all of them in Task 13.
  - **Backend**, from `backend/`:
    - `.venv/bin/ruff check app tests`
    - `.venv/bin/mypy app`
    - `.venv/bin/python -m pytest -q <the touched test files> tests/test_route_session_discipline.py tests/test_agent_push_dispatches_nothing.py tests/test_schema_drift.py tests/test_rls.py`
    - the migration round trip, exactly as `.github/workflows/ci.yml`'s `migrations` job runs it (spelled out in Task 13, Step 2).
  - **Frontend**, from `frontend/`:
    - `npx tsc --noEmit`
    - `npx vitest run`
    - `npm run build`
    - `node scripts/harmony.mjs --routes /devices,/devices/push-runs,/devices/push-runs/run-1 --strict-fixtures`

## Decisions this plan takes where the spec is silent or disagrees with the code

Each decision is recorded here so a reviewer can reject one without re-reading the whole plan.

- **R1: the route prefix is `/api/v1/agent-push/runs`, not the spec's `/api/v1/push/runs`.**
  - The module's router is `APIRouter(prefix="/agent-push")` (`api.py`).
  - Its route-table test pins every path under that prefix.
  - A second prefix would give the same control two homes.
- **R2: the gateway's result handler settles a target. It cannot run the pacer.**
  - The spec has "the existing result handler calls it" (`advance_run`). Two facts in the code rule this out:
    1. `JobEngine.complete` invokes a type's `result_handler` only on `succeeded` (`app/jobs/engine.py`). Failed, refused, expired and cancelled jobs never reach it.
    2. The handler runs in the gateway process, whose `RemoteSessionService` comment says it "never dispatches or holds a vault". Giving the internet-facing gateway a vault, so that it could decrypt domain credentials, would widen invariant 3's exposure for a latency gain.
  - What the plan does instead:
    - `dispatch.record_install_result` calls `runs.settle_target_for_job`, which marks the target from the job row.
    - `advance_run` derives every other terminal state from the `jobs` row, as `agent_update/reconcile.py` does.
    - Freed capacity is refilled by the 60-second beat, and immediately when a person starts or resumes a run in the API process, which holds the vault.
  - Consequence: a freed slot waits at most 60 seconds.
- **R3: the pacer's signature is `advance_run(session, org_id, run_id, *, dispatch, now=None)`.**
  - `org_id` is there for the explicit predicate that every push query carries, on top of RLS.
  - `dispatch` is injected because `tests/test_agent_push_dispatches_nothing.py` allows signing and decryption only in `dispatch.py` and `api.py`. The pacer (`runs.py`) is handed a `RunTargetDispatcher` (`run_contract.py`) and never names `JobEngine`, the vault or `publish_job`.
- **R4: a new `GET /agent-push/runs/prefixes` route, under `agent.push.run`, feeds the subnet picker.**
  - `GET /agent-push/authorizations` is gated on `agent.push.scope.manage`, which a push runner does not hold.
  - The new route returns only active prefixes, with their address count and expiry.
- **R5: only `review_state = 'approved'` rows are listed.**
  - Spec §1.2 says a review state that is "not `rejected`". The ladder it cites, `preflight._asset_for`, requires `approved` (install-link D9).
  - The ladder is the authority. A pending row is not vouched for.
- **R6: "no enrolled agent" is enforced for runs only.**
  - It is not a ladder bound today.
  - Expansion drops devices that have an `agents` row, and the pacer refuses a target that enrolled after the list was confirmed (`already_enrolled`).
  - Ticked pushes are unchanged.
- **R7: the digest covers the ready rows only.**
  - A blocked row is shown and is not installed.
  - A blocked row that turns ready before confirmation must change the digest, or the operator would confirm a machine they saw as "won't be installed".
- **R8: counters.**
  - The spec lists "counters" on `push_run`. The plan stores `target_count`, which is immutable.
  - Per-state counts come from a `GROUP BY` over `push_run_target` on every read. A stored counter would be a second answer that can drift.
- **R9: "a credential deleted mid-run" in practice means unbound or re-scoped.**
  - A credential that was ever scoped cannot be hard-deleted: `push_credential_scope.credential_id` has a plain foreign key.
  - Both cases are handled and tested:
    - an unbound scope gives `credential_not_scoped` per target;
    - a missing row gives `credential_missing`, because `push_run.credential_id` is `ON DELETE SET NULL`.
- **R10: the pacer re-checks the run's creator.**
  - `request_push_batch`'s first rung is the `credentials.checkout` conjunction.
  - The pacer asks whether `run.created_by` still holds `agent.push.run` and `credentials.checkout`. If not, the target is refused with `run_owner_not_permitted`.
  - A resume by someone else does not transfer the run.
- **R11: the machine is confirmed, not the address.**
  - If a target's address now resolves to a different `devices` row than the confirmed `device_id`, the target is refused with `target_changed`.
- **R12: known failures become named refusals; unknown ones propagate.**
  - Four failures that repeat on every tick are recorded as refusals (`not_installable`, `no_release`, `credential_unreadable`, `site_server_awaiting_review`). As exceptions they would poison the run on every tick.
  - Any other exception propagates. That org's tick rolls back and retries, so a signer or OpenBao outage never refuses targets.
- **R13: `PUSH_RUN_LIST_MAX = 4096` listed candidates.**
  - This bounds the expansion's cost. It is refused with the same advice ("choose a narrower subnet").
  - The spec's own bound, 1,024 ready machines, is unchanged.
- **R14: the sealed list is enforced in the database.**
  - A `SECURITY DEFINER` trigger refuses an insert into a sealed run.
  - It also refuses any change to a target's `run_id`, `org_id`, `address` or `position`.
  - An AST test additionally pins that only `runs.create_run` constructs `PushRunTarget`.
- **R15: `RUN_TARGET_LOST_AFTER = 30 min`.**
  - The kernel expires only queued and dispatched jobs.
  - A `running` install still unresolved 30 minutes past its job's expiry is `failed`/`lost`, so a hung install cannot hold a slot for ever.
- **R16: creation is audited as `push.run.started`.**
  - The spec names the audit events for pause, resume, cancel and completion, but not for start. Invariant 4 requires one.
- **R17: pacer actions are audited as `actor_type="system"`, `actor_id=None`.**
  - The metadata carries `on_behalf_of` and `run_id`. This is `agent_update/reconcile.py`'s precedent.
  - The job's `requested_by` is the run's creator.
- **R18: the lockout guard, decided by the operator 2026-09-30 ("one first, stop on failures"; spec §1.4a).**
  - The run's state enum gains `stopped_credential`. It is distinct from `paused`, and it is ACTIVE for the one-active-run rule (Task 2).
  - The pacer dispatches ONE target until a target settled since the run last started or resumed has got past sign-in. After that it dispatches up to 50 (Task 6).
  - `PUSH_RUN_AUTH_FAILURE_LIMIT = 3` `auth_failed` results in that window stop the run, if machines are still waiting.
    - The stop is audited `push.run.stopped` (`actor_type="system"`, `on_behalf_of` the creator), and the `push.run.stopped` event is emitted (Tasks 6 and 7).
    - Installs in flight finish.
    - The pacer never leaves the state.
    - Resume, a person's act, is allowed from `stopped_credential`. It restarts the window, so the run goes back to one (Tasks 5, 9 and 12).
  - The window is `push_run.auth_window_started_at`. The counts are derived from `push_run_target.settled_at` and `signed_in`, not from a counter, so the gateway's settle and the pacer never race on one.
  - The CLAUDE.md 2a(e) note carries the safety-stop sentence verbatim (Task 1).
  - `spec-push-install.md` §5.3's per-credential breaker (`PUSH_AUTH_FAILURES_MAX`, marking a credential `suspect`) is still not built. It would also cover ticked pushes, which keep their 25-at-once shape. Task 13's docs pointer says so.
- **R19: what "got past sign-in" means, read off the agent.** The outcomes come from `agent/internal/capabilities/pushinstall/pushinstall.go` and `winrm_capability.go`; they are the `AgentPushInstallResult.outcome` values the result handler receives.
  - `installed` and `os_mismatch` count. The agent produces them only after an authenticated session ran: over SSH after `Open`, over WinRM from the install script's exit code.
  - `failed` counts only when the result carries `arch_detected`.
    - The WinRM path returns `failed` before the credential is used: for the artefact download, the digest check and the script build.
    - SSH, and the WinRM script-exit failures, return it from inside a signed-in session, and only then does the target report its architecture.
  - `unreachable` never counts. It is also the SSH host-key failure and the WinRM transport failure, both before any authentication.
  - `auth_failed` (`outcomeAuthFailed`) is the failure the guard counts.
  - Kernel failures (`failed`, `refused`, `expired` and `cancelled` job states, and `lost`) prove nothing either way.

## File map

**Docs — modify**
- `CLAUDE.md`: the two dated notes under 2a(e) and 2a(f), verbatim from spec §1.6 (Task 1).
- `docs/modules/agent-distribution/spec-push-install.md`: a pointer in §5.2 to the 2026-09-30 run design (Task 13).

**Backend — create**
- `backend/migrations/versions/0200_push_runs.py`: the two tables, forced RLS, the partial unique "one active run" index and the sealed-list trigger.
- `backend/app/modules/agent_push/run_contract.py`: `Envelope`, `RunTargetDispatch`, `RunTargetDispatcher`. This is the pacer–dispatcher seam.
- `backend/app/modules/agent_push/run_list.py`:
  - `PUSH_RUN_MAX`, `PUSH_RUN_LIST_MAX`;
  - `compute_list_digest`, `RunList`, `RunListRow`;
  - `authorized_prefixes`, `expand_prefix`.
- `backend/app/modules/agent_push/runs.py`:
  - constants: `PUSH_RUN_CONCURRENCY`, `TICK_INTERVAL_S`, `RUN_TARGET_LOST_AFTER`, `RUN_REFUSAL_REASONS`;
  - lifecycle: `create_run`, `get_run`, `list_runs`, `run_counts`, `list_targets`, `pause_run`, `resume_run`, `cancel_run`;
  - pacer: `advance_run`, `settle_target_for_job`, `tick`, `org_ids_with_live_runs`;
  - lockout guard: `PUSH_RUN_AUTH_FAILURE_LIMIT`, `SIGNED_IN_OUTCOMES`, `auth_window`, `_stop_if_locked_out`.
- Tests:
  - `backend/tests/push_run_fixtures.py`
  - `backend/tests/test_push_run_migration.py`
  - `backend/tests/test_push_run_dispatcher.py`
  - `backend/tests/test_push_run_list.py`
  - `backend/tests/test_push_run_lifecycle.py`
  - `backend/tests/test_push_run_pacer.py`
  - `backend/tests/test_push_run_tick.py`
  - `backend/tests/test_push_run_api.py`

**Backend — modify**
- `backend/app/modules/agent_push/models.py`: `PushRun` (including `auth_window_started_at` and `stopped_at`), `PushRunTarget` (including `settled_at` and `signed_in`) and the state tuples (including `stopped_credential`).
- `backend/app/modules/agent_push/errors.py`: `PushListChangedError`.
- `backend/app/modules/agent_push/dispatch.py`:
  - `pre_dispatch_checks` and `InstallChecks`, extracted from `dispatch_push_batch` with no behaviour change;
  - `run_id` threaded through `_dispatch_install` and `_audit_dispatched`;
  - `RunDispatcher`;
  - `record_install_result` settles a run target.
- `backend/app/modules/agent_push/schema.py`: the run request and response models.
- `backend/app/modules/agent_push/api.py`: nine routes under `/runs`, and the 409 `list_changed` mapping.
- `backend/app/core/events.py`: the `push.run.completed` and `push.run.stopped` event types.
- `backend/app/modules/notification/delivery.py`: their email renderers.
- `backend/app/worker.py`: `push_run_tick_all_orgs`, the `push-run-tick` beat entry (60 s) and its Celery task.
- `backend/tests/test_agent_push_api.py`: the route table grows from 9 to 18.
- `backend/tests/test_notification_delivery.py`: a sample payload for the new event.

**Frontend — create**
- `frontend/src/modules/agent-push/runState.ts`: labels, kinds, chips, progress, `isListChanged`.
- `frontend/src/modules/agent-push/PushBySubnetFlow.tsx`: subnet picker, credential picker, the expanded table and Start.
- `frontend/src/modules/agent-push/PushInstallChooser.tsx`: the **Ticked machines** / **By subnet** choice.
- `frontend/src/modules/agent-push/PushRunsPage.tsx` and `frontend/src/modules/agent-push/PushRunPage.tsx`.
- Tests: `frontend/src/modules/agent-push/__tests__/runState.test.ts`, `pushBySubnet.test.tsx`, `pushRuns.test.tsx`.

**Frontend — modify**
- `frontend/src/modules/agent-push/types.ts`, `api.ts` and `fixtures.ts`
- `frontend/src/lib/api.ts`: nine `ApiClient` methods.
- `frontend/src/modules/devices/DevicesPage.tsx`: the entry points and the chooser.
- `frontend/src/App.tsx`: two routes.
- `frontend/src/shell/nav.ts`: `NAV_UNLISTED`.
- `frontend/scripts/routes.mjs`: the routes and a `runId` sample.
- `frontend/src/modules/notification/types.ts` and `labels.ts`, and `frontend/src/lib/noWireNames.test.ts`: the new event.

---

### Task 1: Commit the operator-approved 2a(e)/2a(f) amendment to CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (clauses 2a(e) and 2a(f), currently lines 214–220)

**Interfaces:**
- Consumes: spec §1.6's quoted text, as updated for the lockout guard (commit `09ce09e`): the 2a(e) note ends with the safety-stop sentence.
- Produces: the two dated notes, which every later task's security reasoning cites.

- [ ] **Step 1: Write the failing check**

Run from the repository root:

```bash
python3 - <<'PY'
import pathlib, re
norm = lambda s: re.sub(r"\s+", " ", s).strip()
spec = pathlib.Path("docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md").read_text()
section = spec.split("### 1.6 CLAUDE.md amendment")[1].split("**What this does not claim:**")[0]
quoted = [line[1:] for line in section.splitlines() if line.startswith(">")]
paras = [norm(p) for p in "\n".join(quoted).split("\n\n") if p.strip()]
assert len(paras) == 2, paras
claude = norm(pathlib.Path("CLAUDE.md").read_text())
missing = [p[:40] for p in paras if p not in claude]
assert not missing, f"not yet in CLAUDE.md: {missing}"
print("both notes present verbatim")
PY
```

Expected: FAIL with `AssertionError: not yet in CLAUDE.md: ['**2a(e), added 2026-09-30.** A push run', '**2a(f), added 2026-09-30.** A subnet ma']`.

- [ ] **Step 2: Insert the two notes and change nothing else**

In `CLAUDE.md`, replace this block:

```
   e. **Same RBAC, same audit chain**, under permission codes of its own, and
      never reachable by any automated, scheduled or AI-driven actor.

   f. **The destructive property is bounded, not removed.** A push may be
      expressed only against explicitly named addresses a human selected —
      never a group, a filter, a subnet or a saved search — and never more
      than the configured batch ceiling in one act.
```

with:

```
   e. **Same RBAC, same audit chain**, under permission codes of its own, and
      never reachable by any automated, scheduled or AI-driven actor.

      **2a(e), added 2026-09-30.** A push run the operator confirmed may be
      continued by the platform's pacer, which dispatches the confirmed
      targets as capacity allows. The pacer cannot start a run, add a target,
      widen a list, choose a credential, or resume a paused or cancelled run.
      Starting, pausing, resuming and cancelling are a person's acts,
      RBAC-checked and audited. The pacer's one act of its own is a safety
      stop: after repeated authentication failures it stops the run so a
      credential is not locked out, and only a person may restart it.

   f. **The destructive property is bounded, not removed.** A push may be
      expressed only against explicitly named addresses a human selected —
      never a group, a filter, a subnet or a saved search — and never more
      than the configured batch ceiling in one act.

      **2a(f), added 2026-09-30.** A subnet may be used to *build* the list a
      person confirms, never as the thing confirmed. The act is the
      confirmation of the named machines the subnet expanded to, bound by a
      digest of that list. Only rows vouched for under (d) are ever listed. A
      run holds at most 1,024 named targets, at most 50 of which are in flight
      at once. A push that is not a run keeps the batch ceiling of 25.
```

- [ ] **Step 3: Verify that the text is verbatim and purely additive**

Run the Step 1 script again. Expected: `both notes present verbatim`.

Run: `git diff --numstat CLAUDE.md`
Expected: `16	0	CLAUDE.md`. That is 16 lines added and 0 removed, so no existing sentence changed. The 2a(e) note is eight lines, because it carries the operator's 2026-09-30 safety-stop sentence (spec §1.4a, §1.6).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): 2a(e) and 2a(f) dated notes for confirmed push runs (approved 2026-09-30)

The operator approved docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md
including §1.6's amendment text and the 2a(e) safety-stop sentence (§1.4a).
Added verbatim as dated notes; no existing sentence changes.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 2: Migration 0200 and the models — `push_run`, `push_run_target`, and a sealed list

**Files:**
- Create: `backend/migrations/versions/0200_push_runs.py`
- Modify: `backend/app/modules/agent_push/models.py` (append two classes and three tuples; extend imports and `__all__`)
- Test: `backend/tests/test_push_run_migration.py` (create)

**Interfaces:**
- Consumes: migration `0190` (the head), and the `organizations`, `users`, `credentials`, `devices` and `jobs` tables.
- Produces:
  - `PushRun`, with columns `id, org_id, created_by, credential_id (nullable, ON DELETE SET NULL), prefix (cidr), list_digest (varchar 64), state (varchar 24), target_count, created_at, sealed_at, updated_at, finished_at, auth_window_started_at, stopped_at`.
    - `state` is one of `running`, `paused`, `stopped_credential`, `cancelled`, `completed`. The first three are ACTIVE for the one-active-run rule (spec §1.4a).
    - `auth_window_started_at` is when the run last started or resumed. The lockout guard counts only targets settled at or after it.
    - `stopped_at` is when the pacer's safety stop fired; it is cleared by Resume.
  - `PushRunTarget`, with columns `id, org_id, run_id, device_id (nullable, ON DELETE SET NULL), address (inet), position, state, job_id, outcome (varchar 32), detail (varchar 500), reasons (jsonb list), updated_at, settled_at, signed_in`.
    - `settled_at` is when the target's job outcome was recorded.
    - `signed_in` is true when that outcome proves the credential got past sign-in on the target (Task 6, R19).
    - "An `auth_failed` since the run last started or resumed" is `outcome = 'auth_failed' AND settled_at >= auth_window_started_at`. "A sign-in has succeeded since then" is `signed_in AND settled_at >= auth_window_started_at`. Both are derived from target rows, so there is no counter for the gateway and the pacer to race on.
  - The tuples `PUSH_RUN_STATES`, `PUSH_RUN_ACTIVE_STATES` and `PUSH_RUN_TARGET_STATES`.
  - The trigger `push_run_target_fixed`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_push_run_migration.py`:

```python
"""Migration 0200: `push_run` and `push_run_target` (subnet push design §1.3).

Tenancy is asserted on both tables. So is the one property that makes a run's
list the authority: once a run is sealed, the database refuses a new target and
refuses any change to which machine an existing target names. That holds
whatever code runs above it.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from tests.agent_push_fixtures import PushOrg, seed_org

TABLES = ("push_run", "push_run_target")
DIGEST = "a" * 64


async def _insert_run(
    admin_engine: AsyncEngine, org: PushOrg, *, state: str = "running", sealed: bool = True
) -> UUID:
    run_id = uuid4()
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "INSERT INTO push_run (id, org_id, created_by, credential_id, prefix,"
                " list_digest, state, target_count, created_at, sealed_at, updated_at,"
                " auth_window_started_at)"
                " VALUES (:id, :org, :user, NULL, CAST('10.61.0.0/24' AS cidr), :digest,"
                " :state, 1, now(), NULL, now(), now())"
            ),
            {
                "id": run_id, "org": org.org_id, "user": org.user_id,
                "digest": DIGEST, "state": state,
            },
        )
        await session.execute(
            text(
                "INSERT INTO push_run_target (id, org_id, run_id, device_id, address,"
                " position, state, updated_at)"
                " VALUES (:id, :org, :run, NULL, CAST('10.61.0.5' AS inet), 0, 'waiting', now())"
            ),
            {"id": uuid4(), "org": org.org_id, "run": run_id},
        )
        if sealed:
            await session.execute(
                text("UPDATE push_run SET sealed_at = now() WHERE id = :id"), {"id": run_id}
            )
        await session.commit()
    return run_id


async def _add_target(admin_engine: AsyncEngine, org: PushOrg, run_id: UUID, address: str) -> None:
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "INSERT INTO push_run_target (id, org_id, run_id, device_id, address,"
                " position, state, updated_at)"
                " VALUES (:id, :org, :run, NULL, CAST(:addr AS inet), 7, 'waiting', now())"
            ),
            {"id": uuid4(), "org": org.org_id, "run": run_id, "addr": address},
        )
        await session.commit()


@pytest.mark.parametrize("table", TABLES)
async def test_rls_is_enabled_and_forced(admin_engine: AsyncEngine, table: str) -> None:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert row.relrowsecurity and row.relforcerowsecurity


async def test_another_org_cannot_read_a_run(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    owner = await seed_org(admin_engine, "pr-own")
    other = await seed_org(admin_engine, "pr-oth")
    await _insert_run(admin_engine, owner)
    async with org_session(other.org_id, engine=app_engine) as session:
        runs = (await session.execute(text("SELECT id FROM push_run"))).all()
        targets = (await session.execute(text("SELECT id FROM push_run_target"))).all()
    assert runs == [] and targets == []


async def test_a_run_cannot_be_written_into_another_org(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    mine = await seed_org(admin_engine, "pr-ins")
    theirs = await seed_org(admin_engine, "pr-ins2")
    with pytest.raises(DBAPIError):
        async with org_session(mine.org_id, engine=app_engine) as session:
            await session.execute(
                text(
                    "INSERT INTO push_run (id, org_id, created_by, prefix, list_digest,"
                    " state, target_count, created_at, updated_at, auth_window_started_at)"
                    " VALUES (:id, :org, :user, CAST('10.61.0.0/24' AS cidr), :d,"
                    " 'running', 1, now(), now(), now())"
                ),
                {"id": uuid4(), "org": theirs.org_id, "user": theirs.user_id, "d": DIGEST},
            )


async def test_one_active_run_per_org_is_a_database_rule(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "pr-one")
    await _insert_run(admin_engine, org, state="running")
    with pytest.raises(DBAPIError):
        await _insert_run(admin_engine, org, state="paused")
    # A run the lockout guard stopped is still active (spec §1.4a): a person
    # must resume or cancel it before another run may start.
    with pytest.raises(DBAPIError):
        await _insert_run(admin_engine, org, state="stopped_credential")
    # Finished runs are history, not active: any number may exist.
    await _insert_run(admin_engine, org, state="completed")
    await _insert_run(admin_engine, org, state="cancelled")


async def test_a_sealed_run_takes_no_new_target(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "pr-seal")
    run_id = await _insert_run(admin_engine, org, sealed=True)
    with pytest.raises(DBAPIError) as exc:
        await _add_target(admin_engine, org, run_id, "10.61.0.9")
    assert "sealed" in str(exc.value)


async def test_an_unsealed_run_is_still_being_created_and_takes_targets(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "pr-open")
    run_id = await _insert_run(admin_engine, org, sealed=False)
    await _add_target(admin_engine, org, run_id, "10.61.0.9")


async def test_a_target_never_changes_which_machine_it_names(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "pr-fixed")
    run_id = await _insert_run(admin_engine, org)
    async with AsyncSession(admin_engine) as session:
        # Its state may move ...
        await session.execute(
            text("UPDATE push_run_target SET state = 'refused' WHERE run_id = :r"), {"r": run_id}
        )
        await session.commit()
    with pytest.raises(DBAPIError) as exc:
        async with AsyncSession(admin_engine) as session:
            # ... but never its address.
            await session.execute(
                text(
                    "UPDATE push_run_target SET address = CAST('10.61.0.200' AS inet)"
                    " WHERE run_id = :r"
                ),
                {"r": run_id},
            )
            await session.commit()
    assert "never changes" in str(exc.value)


async def test_the_state_columns_are_constrained(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "pr-ck")
    with pytest.raises(DBAPIError):
        await _insert_run(admin_engine, org, state="resumed-by-pacer")


def test_the_migration_revises_0190() -> None:
    from pathlib import Path

    import migrations.versions as versions_pkg  # type: ignore[import-untyped]

    source = (Path(next(iter(versions_pkg.__path__))) / "0200_push_runs.py").read_text()
    assert 'revision: str = "0200"' in source
    assert 'down_revision: str | None = "0190"' in source
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_migration.py`
Expected: FAIL. Test collection succeeds, the database tests error with `relation "push_run" does not exist`, and `test_the_migration_revises_0190` fails with `FileNotFoundError`.

- [ ] **Step 3: Write the migration**

Create `backend/migrations/versions/0200_push_runs.py`:

```python
"""push_run and push_run_target: a confirmed subnet push, paced 50 at a time
(docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §1.3;
CLAUDE.md 2a(e), 2a(f) as amended 2026-09-30).

Both tables are tenant-scoped under forced RLS, like every push table
(`0077_agent_push.py`, `0190_push_credential_realm.py`).

`uq_push_run_active` makes "one active run per organization" a database rule:
active means running or paused.

THE CONFIRMED LIST IS SEALED. `push_run_target_fixed` refuses:
  * an INSERT into a run whose `sealed_at` is set. `runs.create_run` sets it
    in the transaction that writes the targets, so no later code path, present
    or future, can add a machine to a confirmed run;
  * any UPDATE that changes which machine a target names (`run_id`, `org_id`,
    `address`, `position`). State, job and outcome may move; identity may not.
It is SECURITY DEFINER so its existence check sees the run whatever the
caller's RLS context is, and it pins `search_path`.

Forward-only. `downgrade()` has never run in production (rollback is
restore-from-backup). CI runs upgrade->downgrade->upgrade.

Revision ID: 0200
Revises: 0190
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0200"
down_revision: str | None = "0190"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"
_TABLES = ("push_run", "push_run_target")

_FIXED_FUNCTION = """
CREATE FUNCTION push_run_target_fixed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF EXISTS (SELECT 1 FROM push_run WHERE id = NEW.run_id AND sealed_at IS NOT NULL) THEN
      RAISE EXCEPTION 'push_run % is sealed: a confirmed list is never extended', NEW.run_id
        USING ERRCODE = '23514';
    END IF;
  ELSIF NEW.run_id IS DISTINCT FROM OLD.run_id
     OR NEW.org_id IS DISTINCT FROM OLD.org_id
     OR NEW.address IS DISTINCT FROM OLD.address
     OR NEW.position IS DISTINCT FROM OLD.position THEN
    RAISE EXCEPTION 'a push_run_target names one confirmed machine and never changes which'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "push_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("credentials.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("prefix", postgresql.CIDR(), nullable=False),
        sa.Column("list_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # The lockout guard's window (spec §1.4a): when the run last started or
        # resumed. Only targets settled at or after it are counted.
        sa.Column("auth_window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('running', 'paused', 'stopped_credential', 'cancelled', 'completed')",
            name="ck_push_run_state",
        ),
    )
    op.create_index(
        "uq_push_run_active",
        "push_run",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('running', 'paused', 'stopped_credential')"),
    )
    op.create_index("ix_push_run_org_created", "push_run", ["org_id", "created_at"])

    op.create_table(
        "push_run_target",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("push_run.id"), nullable=False
        ),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("address", postgresql.INET(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True
        ),
        sa.Column("outcome", sa.String(32), nullable=False, server_default=""),
        sa.Column("detail", sa.String(500), nullable=False, server_default=""),
        sa.Column(
            "reasons", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_in", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint(
            "state IN ('waiting', 'in_flight', 'installed', 'failed', 'refused', 'cancelled')",
            name="ck_push_run_target_state",
        ),
    )
    op.create_index(
        "uq_push_run_target_position", "push_run_target", ["run_id", "position"], unique=True
    )
    op.create_index(
        "uq_push_run_target_address", "push_run_target", ["run_id", "address"], unique=True
    )
    op.create_index("ix_push_run_target_state", "push_run_target", ["org_id", "run_id", "state"])
    op.create_index("ix_push_run_target_job", "push_run_target", ["job_id"])

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
        )

    op.execute(_FIXED_FUNCTION)
    op.execute(
        "CREATE TRIGGER push_run_target_fixed BEFORE INSERT OR UPDATE ON push_run_target"
        " FOR EACH ROW EXECUTE FUNCTION push_run_target_fixed()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS push_run_target_fixed ON push_run_target")
    op.execute("DROP FUNCTION IF EXISTS push_run_target_fixed()")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("push_run_target")
    op.drop_table("push_run")
```

- [ ] **Step 4: Write the models**

In `backend/app/modules/agent_push/models.py`:

- Change the SQLAlchemy import line to:

```python
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, text
```

- Extend `__all__`:

```python
__all__ = [
    "PushAuthorization",
    "PushCredentialScope",
    "PushHostKey",
    "PushBatchTarget",
    "PushRun",
    "PushRunTarget",
    "PUSH_RUN_STATES",
    "PUSH_RUN_ACTIVE_STATES",
    "PUSH_RUN_TARGET_STATES",
]
```

- Append at the end of the file:

```python
#: A run's lifecycle (design §1.4a, §1.5). `running`, `paused` and
#: `stopped_credential` are ACTIVE; at most one active run per org
#: (`uq_push_run_active`). `stopped_credential` is the pacer's safety stop after
#: repeated sign-in failures, and only a person's Resume leaves it. `cancelled`
#: is final.
PUSH_RUN_STATES = ("running", "paused", "stopped_credential", "cancelled", "completed")
PUSH_RUN_ACTIVE_STATES = ("running", "paused", "stopped_credential")
#: One confirmed machine's progress. `in_flight` holds a signed job; the four
#: after it are terminal; `waiting` becomes `cancelled` only by a person's Cancel.
PUSH_RUN_TARGET_STATES = ("waiting", "in_flight", "installed", "failed", "refused", "cancelled")


class PushRun(Base):
    """One subnet push a person confirmed (design §1.3; CLAUDE.md 2a(f) as
    amended 2026-09-30).

    **THE LIST IS THE AUTHORITY, NOT THE SUBNET.** `prefix` records what built
    the list; `list_digest` is the SHA-256 of the ready `(device_id, address)`
    pairs the person was shown and confirmed (`run_list.compute_list_digest`).
    The targets are written once, in the transaction that sets `sealed_at`, and
    the `push_run_target_fixed` trigger refuses any later insert.

    `credential_id` is `ON DELETE SET NULL`: a credential that disappears
    mid-run leaves the run readable, and the pacer refuses every remaining
    target as `credential_missing`. `target_count` is fixed at creation; the
    per-state counts are computed on read, never stored.
    """

    __tablename__ = "push_run"
    __table_args__ = (
        CheckConstraint(
            "state IN ('running', 'paused', 'stopped_credential', 'cancelled', 'completed')",
            name="ck_push_run_state",
        ),
        Index(
            "uq_push_run_active",
            "org_id",
            unique=True,
            postgresql_where=text("state IN ('running', 'paused', 'stopped_credential')"),
        ),
        Index("ix_push_run_org_created", "org_id", "created_at"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    #: The person who confirmed the list. NOT NULL: no run starts without one.
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    credential_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), default=None
    )
    prefix: Mapped[str] = mapped_column(CIDR)
    list_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="running")
    target_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    sealed_at: Mapped[datetime | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    #: The lockout guard's window (spec §1.4a): when the run last started or
    #: resumed. `auth_failed` targets and proven sign-ins count only when they
    #: settled at or after this. `create_run` and `resume_run` set it.
    auth_window_started_at: Mapped[datetime] = mapped_column(default=_utcnow)
    #: When the pacer's safety stop fired. Cleared by a person's Resume.
    stopped_at: Mapped[datetime | None] = mapped_column(default=None)


class PushRunTarget(Base):
    """One confirmed machine in a run (design §1.3).

    `device_id` and `address` are what the person confirmed. The pacer refuses
    a target whose address now names another device (`target_changed`), and the
    database refuses any change to `address` or `position`. `reasons` holds the
    named refusals when `state == 'refused'`; `outcome`/`detail` hold what the
    job reported once it was dispatched.
    """

    __tablename__ = "push_run_target"
    __table_args__ = (
        CheckConstraint(
            "state IN ('waiting', 'in_flight', 'installed', 'failed', 'refused', 'cancelled')",
            name="ck_push_run_target_state",
        ),
        Index("uq_push_run_target_position", "run_id", "position", unique=True),
        Index("uq_push_run_target_address", "run_id", "address", unique=True),
        Index("ix_push_run_target_state", "org_id", "run_id", "state"),
        Index("ix_push_run_target_job", "job_id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("push_run.id"))
    device_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), default=None
    )
    address: Mapped[str] = mapped_column(INET)
    position: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="waiting")
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    outcome: Mapped[str] = mapped_column(String(32), server_default="", default="")
    detail: Mapped[str] = mapped_column(String(500), server_default="", default="")
    reasons: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)
    #: When the job's outcome was recorded (the lockout guard's clock).
    settled_at: Mapped[datetime | None] = mapped_column(default=None)
    #: The outcome proves the credential got past sign-in on the target (R19).
    signed_in: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), default=False)
```

`CIDR`, `INET` and `JSONB` are already imported from `sqlalchemy.dialects.postgresql` at the top of this file. `app/models/__init__.py` already imports this module, so both classes register into `Base.metadata` with no further edit.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_migration.py tests/test_schema_drift.py tests/test_rls.py tests/test_migrations.py`
Expected: PASS. The drift tests find the two models matching the migrated columns and indexes. `test_every_org_scoped_table_has_forced_rls_policy` covers both new tables.

- [ ] **Step 6: Lint, type-check, and commit**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: no findings.

```bash
git add backend/migrations/versions/0200_push_runs.py backend/app/modules/agent_push/models.py backend/tests/test_push_run_migration.py
git commit -m "feat(agent-push): push_run and push_run_target under forced RLS, with a sealed list (migration 0200)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---
### Task 3: One dispatch path — extract `pre_dispatch_checks`, add `RunDispatcher`

**Files:**
- Create: `backend/app/modules/agent_push/run_contract.py`
- Modify: `backend/app/modules/agent_push/dispatch.py`:
  - imports and `__all__`;
  - a new `InstallChecks` and `pre_dispatch_checks`;
  - `dispatch_push_batch`, from its `credential = await resolve_push_credential(...)` line down to the first `return PushDispatchResult(... refused=refusals)`;
  - `_dispatch_install` and `_audit_dispatched`;
  - a new `RunDispatcher`.
- Create: `backend/tests/push_run_fixtures.py`
- Test: `backend/tests/test_push_run_dispatcher.py` (create)
- Regression: `tests/test_agent_push_dispatch.py`, `tests/test_agent_push_winrm_dispatch.py` and `tests/test_agent_push_dispatches_nothing.py` must pass **unchanged**.

**Interfaces:**
- Consumes: `preflight.TargetDecision`, `preflight.evaluate_targets`, and the existing `dispatch` internals (`_device_target`, `_device_transport`, `_dns_name_refusal`, `_enroll_base_url`, `_resolve_release`, `_parse_credential_secret`, `_dispatch_install`).
- Produces:
  - `run_contract.Envelope = tuple[UUID, dict[str, Any]]`.
  - `run_contract.RunTargetDispatch(refusals: tuple[str, ...] = (), extras: dict[str, str] = {}, job_id: UUID | None = None, envelope: dict[str, Any] | None = None)`, with a `.dispatched` property.
  - `run_contract.RunTargetDispatcher`, a Protocol: `async (session, *, org_id, run_id, on_behalf_of, credential, decision) -> RunTargetDispatch`.
  - `dispatch.InstallChecks(refusals, extras, transports, platforms, confirmed, enroll_url, realm)`.
  - `dispatch.pre_dispatch_checks(session, org_id, *, decisions, credential, kind, public_console_url) -> InstallChecks`, which raises `PushValidationError` exactly where `dispatch_push_batch` did.
  - `dispatch.RunDispatcher(job_engine, object_store_factory, vault, *, public_console_url)`, whose `.dispatch_target` satisfies `RunTargetDispatcher`.
  - `_dispatch_install(..., run_id: UUID | None = None)`.

- [ ] **Step 1: Write the shared test fixtures**

Create `backend/tests/push_run_fixtures.py`:

```python
"""Seed helpers for the subnet push run tests
(docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §1).

Built on `tests/agent_push_fixtures.py` and on the ADMIN engine, for the same
reason that module gives: the code under test must never set up its own world.
`FakeVault` counts decrypts, so a test can say "nothing was decrypted" as a
number rather than as a hope.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.signing import LocalEd25519Signer
from app.jobs.engine import JobEngine
from tests.agent_push_fixtures import (
    PushOrg,
    seed_agent_release,
    seed_asset,
    seed_confirmed_host_key,
    seed_credential,
    seed_org,
    seed_push_authorization,
    seed_push_credential_scope,
    seed_push_site_server,
)
from tests.ipam_fixtures import admin_engine_of

CONSOLE = "https://itms.example.com"
CRED_JSON = json.dumps({"username": "svc-push", "secret": "hunter2"})


class FakeVault:
    """`checkout` returns the credential's plaintext JSON and counts calls."""

    def __init__(self, plaintext: str = CRED_JSON) -> None:
        self.plaintext = plaintext
        self.calls = 0

    async def checkout(self, session: Any, *, org_id: UUID, credential_id: UUID) -> str:
        self.calls += 1
        return self.plaintext


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []

    def presign_agent_release_get(
        self, version: str, os_name: str, arch: str, *, expires_in: int
    ) -> str:
        self.calls.append((version, os_name, arch, expires_in))
        return f"https://store.test/{version}/{os_name}/{arch}?sig=x&expires={expires_in}"


def job_engine(tmp_path: Path) -> JobEngine:
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "job-signing.key"))


def make_dispatcher(
    tmp_path: Path, *, vault: FakeVault | None = None, store: FakeStore | None = None
) -> Any:
    from app.modules.agent_push.dispatch import RunDispatcher

    chosen_store = store or FakeStore()
    return RunDispatcher(
        job_engine(tmp_path),
        lambda: chosen_store,  # type: ignore[arg-type, return-value]
        vault or FakeVault(),  # type: ignore[arg-type]
        public_console_url=CONSOLE,
    )


async def grant_org_admin(env_or_engine: Any, org: PushOrg) -> None:
    """`org-admin` holds `agent.push.run` and `credentials.checkout`."""
    from app.models import Role, UserRole

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        role = (
            await session.execute(
                select(Role).where(Role.name == "org-admin", Role.org_id.is_(None))
            )
        ).scalar_one()
        session.add(UserRole(org_id=org.org_id, user_id=org.user_id, role_id=role.id))
        await session.commit()


@dataclass(frozen=True)
class RunWorld:
    org: PushOrg
    credential_id: UUID
    prefix: str
    addresses: tuple[str, ...]
    device_ids: tuple[UUID, ...]
    site_server_device_id: UUID


async def run_world(
    env_or_engine: Any,
    tag: str,
    *,
    octet: int,
    count: int,
    confirmed_keys: bool = True,
    with_release: bool = True,
    architecture: str = "amd64",
) -> RunWorld:
    """An org in which every bound holds for `count` Linux machines on
    `10.<octet>.0.0/24`, starting at `.10`. The org's user is an org-admin."""
    engine: AsyncEngine = admin_engine_of(env_or_engine)
    org = await seed_org(engine, tag)
    await grant_org_admin(engine, org)
    credential_id = await seed_credential(engine, org)
    prefix = f"10.{octet}.0.0/24"
    await seed_push_authorization(engine, org, prefix)
    await seed_push_credential_scope(engine, org, credential_id, prefix)
    _ss, ss_device = await seed_push_site_server(engine, org.org_id, segments=[prefix])
    if with_release:
        await seed_agent_release(engine, os_name="linux", arch="amd64")
    addresses: list[str] = []
    device_ids: list[UUID] = []
    for index in range(count):
        address = f"10.{octet}.0.{10 + index}"
        device_ids.append(
            await seed_asset(
                engine, org.org_id, primary_ip=address, architecture=architecture
            )
        )
        if confirmed_keys:
            await seed_confirmed_host_key(engine, org, address=address)
        addresses.append(address)
    return RunWorld(
        org=org,
        credential_id=credential_id,
        prefix=prefix,
        addresses=tuple(addresses),
        device_ids=tuple(device_ids),
        site_server_device_id=ss_device,
    )
```

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_push_run_dispatcher.py`:

```python
"""`dispatch.RunDispatcher`: the pacer's only way to sign a job and decrypt a
credential (design §1.4; CLAUDE.md invariants 1, 3, 4; 2a(e) as amended).

Each test removes exactly one bound and shows it is a NAMED refusal returned
before the vault is touched, never an exception and never a job.
"""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, EnrollmentToken, Job
from app.modules.agent_push import credential_scope, preflight
from app.modules.agent_push.jobtypes import INSTALL_JOB_TYPE
from app.modules.agent_push.models import PushBatchTarget
from app.modules.agent_push.net import parse_target_address
from tests.push_run_fixtures import FakeVault, RunWorld, make_dispatcher, run_world


@pytest.fixture(autouse=True)
def _register_push_job_types() -> None:
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


async def _dispatch_one(app_engine: AsyncEngine, world: RunWorld, dispatcher, run_id):
    async with org_session(world.org.org_id, engine=app_engine) as session:
        credential = await credential_scope.resolve_push_credential(
            session, world.org.org_id, world.credential_id
        )
        (decision,) = await preflight.evaluate_targets(
            session,
            world.org.org_id,
            addresses=[parse_target_address(world.addresses[0])],
            credential_id=world.credential_id,
        )
        return await dispatcher.dispatch_target(
            session,
            org_id=world.org.org_id,
            run_id=run_id,
            on_behalf_of=world.org.user_id,
            credential=credential,
            decision=decision,
        )


async def _count(admin_engine: AsyncEngine, model, org_id) -> int:
    async with AsyncSession(admin_engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(model.org_id == org_id)
                )
            ).scalar_one()
        )


async def test_a_ready_target_becomes_one_signed_job_on_behalf_of_the_run(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rd-ok", octet=71, count=1)
    vault = FakeVault()
    run_id = uuid4()
    outcome = await _dispatch_one(
        app_engine, world, make_dispatcher(tmp_path, vault=vault), run_id
    )

    assert outcome.dispatched and outcome.refusals == ()
    assert vault.calls == 1
    assert outcome.envelope is not None
    assert outcome.envelope["job"]["device"] == str(world.site_server_device_id)
    async with AsyncSession(admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == outcome.job_id))).scalar_one()
        token = (
            await session.execute(
                select(EnrollmentToken).where(EnrollmentToken.org_id == world.org.org_id)
            )
        ).scalar_one()
        grouping = (
            await session.execute(
                select(PushBatchTarget).where(PushBatchTarget.job_id == outcome.job_id)
            )
        ).scalar_one()
        dispatched = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == world.org.org_id,
                    AuditEvent.action == "push.dispatched",
                )
            )
        ).scalar_one()
    assert job.type == INSTALL_JOB_TYPE and job.signature
    assert job.requested_by == world.org.user_id
    assert token.name == f"push:{run_id}:{world.addresses[0]}" and token.max_uses == 1
    assert grouping.batch_id == run_id
    # The pacer acted, on behalf of the person who confirmed the run (R17).
    assert dispatched.actor_type == "system" and dispatched.actor_id is None
    assert dispatched.meta["run_id"] == str(run_id)
    assert dispatched.meta["on_behalf_of"] == str(world.org.user_id)
    assert "hunter2" not in str(dispatched.meta)


async def test_an_unconfirmed_host_key_is_refused_before_the_vault(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rd-hk", octet=72, count=1, confirmed_keys=False)
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    outcome = await _dispatch_one(app_engine, world, dispatcher, uuid4())
    assert outcome.refusals == ("host_key_unconfirmed",) and not outcome.dispatched
    assert vault.calls == 0
    assert await _count(admin_engine, Job, world.org.org_id) == 0
    # The dispatcher records nothing itself: the pacer writes `push.refused`.
    assert await _count(admin_engine, AuditEvent, world.org.org_id) == 0


async def test_an_undeclared_processor_is_a_named_refusal_not_an_exception(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rd-arch", octet=73, count=1, architecture="")
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    outcome = await _dispatch_one(app_engine, world, dispatcher, uuid4())
    assert outcome.refusals == ("not_installable",)
    assert vault.calls == 0


async def test_no_published_release_is_a_named_refusal(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    from app.modules.agent_push import dispatch as dispatch_module
    from app.modules.agent_push.errors import PushValidationError

    world = await run_world(admin_engine, "rd-rel", octet=74, count=1)

    async def _no_release(session, os_name, arch):
        raise PushValidationError(f"no published agent release exists for {os_name}/{arch}")

    monkeypatch.setattr(dispatch_module, "_resolve_release", _no_release)
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    outcome = await _dispatch_one(app_engine, world, dispatcher, uuid4())
    assert outcome.refusals == ("no_release",)
    assert outcome.extras == {"os": "linux", "arch": "amd64"}
    assert vault.calls == 0


async def test_an_unreadable_secret_is_refused_and_nothing_is_signed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rd-sec", octet=75, count=1)
    vault = FakeVault(plaintext="not json")
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    outcome = await _dispatch_one(app_engine, world, dispatcher, uuid4())
    assert outcome.refusals == ("credential_unreadable",)
    assert vault.calls == 1
    assert await _count(admin_engine, Job, world.org.org_id) == 0
    assert await _count(admin_engine, EnrollmentToken, world.org.org_id) == 0
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_dispatcher.py`
Expected: FAIL with `ImportError: cannot import name 'RunDispatcher' from 'app.modules.agent_push.dispatch'`.

- [ ] **Step 4: Write `run_contract.py`**

Create `backend/app/modules/agent_push/run_contract.py`:

```python
"""The seam between the pacer and the one file that signs
(docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §1.4).

`runs.py` decides WHICH confirmed target goes next and records what came of
it. `dispatch.py` is the only file in this package that may sign a job or
decrypt a credential (`tests/test_agent_push_dispatches_nothing.py`). So the
pacer never holds a signer or a vault: it is handed a `RunTargetDispatcher`
and gets back a `RunTargetDispatch`. Both are defined here, in a file that
imports neither side, so `dispatch.py` and `runs.py` never import each other
at module load.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent_push.preflight import TargetDecision

__all__ = ["Envelope", "RunTargetDispatch", "RunTargetDispatcher"]

#: (site server device id, signed envelope), published only after the
#: transaction that created the job has committed.
Envelope = tuple[UUID, dict[str, Any]]


@dataclass(frozen=True)
class RunTargetDispatch:
    """What dispatching ONE confirmed target came to: refused, with every
    named reason, or one signed job. Never both."""

    refusals: tuple[str, ...] = ()
    extras: dict[str, str] = field(default_factory=dict)
    job_id: UUID | None = None
    envelope: dict[str, Any] | None = None

    @property
    def dispatched(self) -> bool:
        return self.job_id is not None and self.envelope is not None


class RunTargetDispatcher(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        run_id: UUID,
        on_behalf_of: UUID,
        credential: Any,
        decision: TargetDecision,
    ) -> RunTargetDispatch: ...
```

- [ ] **Step 5: Extract `pre_dispatch_checks`, keeping behaviour identical**

In `backend/app/modules/agent_push/dispatch.py`:

(a) Imports. Replace `from collections.abc import Callable` with `from collections.abc import Callable, Sequence`. Replace `from app.jobs.engine import JobEngine` with `from app.jobs.engine import DeviceAwaitingReview, JobEngine`. Add:

```python
from app.modules.agent_push.run_contract import RunTargetDispatch
```

Replace `__all__` with:

```python
__all__ = [
    "PushDispatchResult",
    "DISPATCH_REFUSAL_REASONS",
    "InstallChecks",
    "RunDispatcher",
    "dispatch_push_batch",
    "pre_dispatch_checks",
    "record_probe_result",
    "record_install_result",
]
```

(b) Add this directly above `async def dispatch_push_batch(`. The body is the block `dispatch_push_batch` runs today, moved verbatim with its comments. The only edit is that the local variables become the fields of the result:

```python
@dataclass(frozen=True)
class InstallChecks:
    """Every bound the ladder does not cover, found for every target BEFORE any
    job is created and BEFORE any credential is decrypted (review Minor 2).

    Extracted from `dispatch_push_batch` so that a ticked push, a run's
    expansion (`run_list.expand_prefix`) and the pacer (`RunDispatcher`) ask
    the SAME function. A second copy is how "the console said ready" and "the
    dispatcher refused" come to disagree.
    """

    refusals: dict[str, list[str]]
    extras: dict[str, dict[str, str]]
    transports: dict[str, tuple[str, str | None]]
    platforms: dict[str, tuple[str, str]]
    confirmed: dict[str, str]
    enroll_url: str
    realm: str | None


async def pre_dispatch_checks(
    session: AsyncSession,
    org_id: UUID,
    *,
    decisions: Sequence[TargetDecision],
    credential: Any,
    kind: str,
    public_console_url: str | None,
) -> InstallChecks:
    """Reads only. Raises `PushValidationError` where `_device_target` does
    (an install onto an undeclared OS or arch), exactly as before extraction."""
    refusals: dict[str, list[str]] = {}
    extras: dict[str, dict[str, str]] = {}

    def refuse(address: str, reason: str, **detail: str) -> None:
        refusals.setdefault(address, []).append(reason)
        extras.setdefault(address, {}).update(detail)

    # THE TRANSPORT IS THE TARGET'S OS'S (design §2.1). A Windows target with no
    # DNS name, or one that is not a usable DNS name, cannot be authenticated
    # with Kerberos (`HTTP/<fqdn>`, design §2.3, W10). An install also
    # validates the declared OS and arch (`_device_target`); a probe needs
    # only the transport (`_device_transport`).
    transports: dict[str, tuple[str, str | None]] = {}
    # THE TARGET IS READ ONCE (Codex on #90). The install job is built from
    # this same snapshot, never a second read: under READ COMMITTED an edit
    # landing between two reads would pair the new OS's transport and release
    # with the credential and host key validated for the old one.
    platforms: dict[str, tuple[str, str]] = {}
    for decision in decisions:
        assert decision.device_id is not None
        if kind == "install":
            os_name, arch, transport, raw_name = await _device_target(
                session, org_id, decision.device_id
            )
            platforms[decision.address] = (os_name, arch)
        else:
            transport, raw_name = await _device_transport(session, org_id, decision.device_id)
        dns_name: str | None = None
        if transport == "winrm":
            dns_name, reason = _dns_name_refusal(raw_name)
            if reason is not None:
                refuse(decision.address, reason, dns_name=raw_name or "")
        transports[decision.address] = (transport, dns_name)

    enroll_url = ""
    realm: str | None = None
    confirmed: dict[str, str] = {}
    if kind == "install":
        # THE CREDENTIAL MUST BE THE TRANSPORT'S (spec §6.2 per transport): an
        # SSH secret is never presented to a WinRM target, nor a Windows domain
        # password to an SSH one.
        for address, (transport, _name) in sorted(transports.items()):
            if credential.type != CREDENTIAL_TYPE_FOR_TRANSPORT[transport]:
                refuse(address, "wrong_credential_kind")

        # WHERE A PUSHED COMPUTER ENROLLS (W2 superseded): the configured
        # console, for every transport. Without it the pushed agent has
        # nowhere to enroll, so every target of the batch is refused.
        try:
            enroll_url = _enroll_base_url(public_console_url)
        except PushValidationError:
            for address in transports:
                refuse(address, "no_console_url")

        # THE REALM IS THE CREDENTIAL'S (review I1), recorded when it was
        # saved, and every Windows target must be inside it. The KDC that will
        # see the account's pre-authentication is found for THIS realm, never
        # for whatever DNS name a device row carries.
        winrm_targets = {
            address: name for address, (transport, name) in transports.items()
            if transport == "winrm" and name
        }
        if winrm_targets and credential.type == CREDENTIAL_TYPE_FOR_TRANSPORT["winrm"]:
            realm = await credential_realm.realm_for(session, org_id, credential.id)
            for address, name in sorted(winrm_targets.items()):
                if realm is None:
                    refuse(address, "realm_not_recorded", dns_name=name)
                elif not credential_realm.fqdn_in_realm(name, realm):
                    refuse(address, "outside_realm", dns_name=name, realm=realm)

        # THE ONE INSTALL-ONLY BOUND THE FOUR DO NOT COVER: a confirmed host key
        # (§7.3), for every SSH target. A push to a host with no confirmed key,
        # or one whose key has changed, is refused -- not prompted, not warned
        # about. A WINRM target has no host key to pin: Kerberos authenticates
        # it (design §2.3, W4), so it carries the `kerberos` sentinel instead.
        for decision in decisions:
            if transports[decision.address][0] == "winrm":
                confirmed[decision.address] = KERBEROS_HOST_KEY_SENTINEL
                continue
            fingerprint, reason = await host_key_service.confirmed_key_for(
                session, org_id, decision.address
            )
            if fingerprint is None:
                assert reason is not None
                refuse(decision.address, reason)
            else:
                confirmed[decision.address] = fingerprint

    return InstallChecks(
        refusals=refusals,
        extras=extras,
        transports=transports,
        platforms=platforms,
        confirmed=confirmed,
        enroll_url=enroll_url,
        realm=realm,
    )
```

(c) In `dispatch_push_batch`, replace everything from the line `credential = await resolve_push_credential(session, org_id, credential_id)` down to and including

```python
    if refusals:
        await _record_refusals(
            session, org_id=org_id, actor_id=actor_id, credential=credential,
            refusals=refusals, extras=extras,
        )
        return PushDispatchResult(batch_id=None, kind=kind, refused=refusals)
```

with:

```python
    credential = await resolve_push_credential(session, org_id, credential_id)

    # EVERY BOUND THE LADDER DOES NOT COVER, FOR EVERY TARGET, BEFORE ANY JOB IS
    # CREATED AND BEFORE ANY CREDENTIAL IS DECRYPTED -- `pre_dispatch_checks`,
    # the one function the run's expansion and the pacer also call.
    checks = await pre_dispatch_checks(
        session,
        org_id,
        decisions=outcome.decisions,
        credential=credential,
        kind=kind,
        public_console_url=public_console_url,
    )
    if checks.refusals:
        await _record_refusals(
            session, org_id=org_id, actor_id=actor_id, credential=credential,
            refusals=checks.refusals, extras=checks.extras,
        )
        return PushDispatchResult(batch_id=None, kind=kind, refused=checks.refusals)
    transports = checks.transports
    platforms = checks.platforms
    confirmed = checks.confirmed
    enroll_url = checks.enroll_url
    realm = checks.realm
```

The rest of `dispatch_push_batch` already reads `transports`, `platforms`, `confirmed`, `enroll_url` and `realm` by those names, and stays as it is.

- [ ] **Step 6: Run the regression suites before going further**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_agent_push_dispatch.py tests/test_agent_push_winrm_dispatch.py tests/test_agent_push_api.py tests/test_agent_push_dispatches_nothing.py`
Expected: PASS, with no test edited. The extraction changed no behaviour.

- [ ] **Step 7: Thread `run_id` through the install path, and add `RunDispatcher`**

(a) In `_dispatch_install`, add a keyword parameter after `batch_id: UUID,`:

```python
    run_id: UUID | None = None,
```

and change its `_audit_dispatched(...)` call to pass `run_id=run_id,` as its last argument.

(b) In `_audit_dispatched`, add a keyword parameter after `enroll_token_id: UUID | None,`:

```python
    run_id: UUID | None = None,
```

and replace its final `await audit_service.record(...)` with:

```python
    if run_id is not None:
        # THE PACER ACTED, ON BEHALF OF THE PERSON WHO CONFIRMED THE RUN
        # (CLAUDE.md 2a(e) as amended 2026-09-30; agent_update's precedent).
        # The chain must not say a human clicked at this moment; it names the
        # human whose confirmation this continues, and the run.
        metadata["run_id"] = str(run_id)
        metadata["on_behalf_of"] = str(actor_id)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system" if run_id is not None else "user",
        actor_id=None if run_id is not None else actor_id,
        action="push.dispatched",
        target_type="push_target",
        target_id=decision.address,
        metadata=metadata,
    )
```

(c) Add at the end of the "dispatch" section, directly above the `# Result handlers` banner:

```python
class RunDispatcher:
    """The pacer's ONE way to dispatch one confirmed run target
    (design §1.4; CLAUDE.md 2a(e) as amended 2026-09-30).

    **THE SAME PATH A TICKED PUSH TAKES.** `pre_dispatch_checks` for the
    install-only bounds, `_resolve_release`, then `vault.checkout` at this
    moment, for this one target, then `_dispatch_install`, which mints the
    single-use 30-minute token, presigns, signs the job for the segment's site
    server and audits `push.dispatched` with the run's id.

    **A REFUSAL IS RETURNED, NEVER RAISED, AND NEVER RECORDED HERE.** The
    pacer writes `push.refused` with the run's context. Only failures that
    would recur on every tick are named refusals (R12): anything else
    propagates, so a signer outage rolls the tick back rather than refusing
    targets.
    """

    def __init__(
        self,
        job_engine: JobEngine,
        object_store_factory: Callable[[], ObjectStore],
        vault: VaultService,
        *,
        public_console_url: str | None,
    ) -> None:
        self._job_engine = job_engine
        self._object_store_factory = object_store_factory
        self._object_store: ObjectStore | None = None
        self._vault = vault
        self._public_console_url = public_console_url

    async def dispatch_target(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        run_id: UUID,
        on_behalf_of: UUID,
        credential: Any,
        decision: TargetDecision,
    ) -> RunTargetDispatch:
        address = decision.address
        try:
            checks = await pre_dispatch_checks(
                session,
                org_id,
                decisions=[decision],
                credential=credential,
                kind="install",
                public_console_url=self._public_console_url,
            )
        except PushValidationError:
            return RunTargetDispatch(refusals=("not_installable",))
        if checks.refusals:
            return RunTargetDispatch(
                refusals=tuple(checks.refusals[address]),
                extras=dict(checks.extras.get(address, {})),
            )
        os_name, arch = checks.platforms[address]
        transport, dns_name = checks.transports[address]
        try:
            await _resolve_release(session, os_name, arch)
        except PushValidationError:
            return RunTargetDispatch(refusals=("no_release",), extras={"os": os_name, "arch": arch})

        # THE POINT OF USE (invariant 3): every bound has held for THIS target.
        try:
            raw = await self._vault.checkout(
                session, org_id=org_id, credential_id=credential.id
            )
            username, secret = _parse_credential_secret(raw, credential_type=credential.type)
        except (ValueError, PushValidationError):
            return RunTargetDispatch(refusals=("credential_unreadable",))
        if checks.realm is not None:
            try:
                account_realm: str | None = credential_realm.realm_of_account(username)
            except credential_realm.InvalidWindowsDomainAccount:
                account_realm = None
            if account_realm != checks.realm:
                return RunTargetDispatch(
                    refusals=("realm_changed",), extras={"realm": checks.realm}
                )

        if self._object_store is None:
            self._object_store = self._object_store_factory()
        try:
            # A SAVEPOINT, because the enrollment token is written before
            # `JobEngine.create` can refuse a site server awaiting review; the
            # token must not outlive the refusal.
            async with session.begin_nested():
                job, envelope = await _dispatch_install(
                    session,
                    self._job_engine,
                    self._object_store,
                    org_id=org_id,
                    actor_id=on_behalf_of,
                    decision=decision,
                    username=username,
                    secret=secret,
                    credential_name=credential.name,
                    credential_id=credential.id,
                    host_key_fingerprint=checks.confirmed[address],
                    os_name=os_name,
                    arch=arch,
                    transport=transport,
                    dns_name=dns_name,
                    enroll_url=checks.enroll_url,
                    batch_id=run_id,
                    run_id=run_id,
                )
        except DeviceAwaitingReview:
            return RunTargetDispatch(refusals=("site_server_awaiting_review",))
        return RunTargetDispatch(job_id=job.id, envelope=envelope)
```

- [ ] **Step 8: Run all the dispatch suites**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_dispatcher.py tests/test_agent_push_dispatch.py tests/test_agent_push_winrm_dispatch.py tests/test_agent_push_api.py tests/test_agent_push_dispatches_nothing.py`
Expected: PASS. `test_only_the_dispatcher_signs_or_decrypts` still passes for `run_contract.py`, which names no signer, vault or publisher.

- [ ] **Step 9: Lint, type-check, and commit**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: no findings.

```bash
git add backend/app/modules/agent_push/run_contract.py backend/app/modules/agent_push/dispatch.py backend/tests/push_run_fixtures.py backend/tests/test_push_run_dispatcher.py
git commit -m "refactor(agent-push): one pre-dispatch check path; RunDispatcher signs one run target through _dispatch_install

pre_dispatch_checks is extracted verbatim from dispatch_push_batch (existing
suites pass unchanged). RunDispatcher returns named refusals before any
decrypt and audits push.dispatched as the system, on behalf of the run's
creator, with the run id.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 4: Expansion and the digest — `run_list.py`

**Files:**
- Create: `backend/app/modules/agent_push/run_list.py`
- Test: `backend/tests/test_push_run_list.py` (create)

**Interfaces:**
- Consumes:
  - `preflight.evaluate_targets` and `preflight.VOUCHED_PROVENANCE`;
  - `dispatch.pre_dispatch_checks`;
  - `credential_scope.resolve_push_credential`;
  - `net.parse_prefix` and `net.parse_target_address`;
  - `rbac.has_permission`.
- Produces:
  - `PUSH_RUN_MAX = 1024` and `PUSH_RUN_LIST_MAX = 4096`.
  - `LIST_REASONS: dict[str, str]`.
  - `RunListRow(device_id, address, hostname, os_family, reasons)`, with `.ready`.
  - `RunList(prefix, credential_id, credential_name, rows, unclaimed_count)`, with `.ready_rows` and `.list_digest`.
  - `compute_list_digest(pairs) -> str`.
  - `authorized_prefixes(session, org_id, *, now=None) -> list[PushAuthorization]`.
  - `expand_prefix(session, org_id, *, actor_id, prefix, credential_id, public_console_url, now=None) -> RunList`, which may raise `PushPermissionError`, `PushNotFoundError` or `PushValidationError`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_push_run_list.py`:

```python
"""Expansion: a subnet becomes the named list a person confirms
(design §1.2, §1.3; CLAUDE.md 2a(d), 2a(f) as amended 2026-09-30).

Every test seeds a row that a WRONG expansion would list, and asserts it is
absent, or present with its named reason.
"""

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, EnrollmentToken, Job
from app.modules.agent_push import run_list
from app.modules.agent_push.errors import PushPermissionError, PushValidationError
from tests.agent_push_fixtures import (
    seed_asset,
    seed_push_authorization,
    seed_push_credential_scope,
)
from tests.ipam_fixtures import seed_agent
from tests.push_run_fixtures import CONSOLE, RunWorld, run_world


async def _expand(
    app_engine: AsyncEngine, world: RunWorld, *, prefix: str | None = None, actor=None
):
    async with org_session(world.org.org_id, engine=app_engine) as session:
        return await run_list.expand_prefix(
            session,
            world.org.org_id,
            actor_id=actor or world.org.user_id,
            prefix=prefix or world.prefix,
            credential_id=world.credential_id,
            public_console_url=CONSOLE,
        )


async def test_the_list_is_exactly_the_vouched_unenrolled_machines_of_the_prefix(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rl-set", octet=81, count=2)
    org = world.org.org_id
    # Found by a sweep, never claimed: not listed, but counted.
    await seed_asset(
        admin_engine, org, primary_ip="10.81.0.20", source="discovery", provenance="sweep"
    )
    # Already running the agent: not listed.
    enrolled = await seed_asset(admin_engine, org, primary_ip="10.81.0.21")
    await seed_agent(admin_engine, org, enrolled)
    # Waiting for review: not vouched for, not listed (R5).
    await seed_asset(admin_engine, org, primary_ip="10.81.0.22", review_state="pending")
    # Outside the prefix: not listed.
    await seed_asset(admin_engine, org, primary_ip="10.99.0.5")
    # Vouched for, never probed: LISTED, blocked, with its reason.
    unprobed = await seed_asset(admin_engine, org, primary_ip="10.81.0.23")

    listed = await _expand(app_engine, world)

    by_address = {row.address: row for row in listed.rows}
    assert set(by_address) == {"10.81.0.10", "10.81.0.11", "10.81.0.23"}
    assert [r.address for r in listed.ready_rows] == ["10.81.0.10", "10.81.0.11"]
    assert by_address["10.81.0.23"].device_id == unprobed
    assert by_address["10.81.0.23"].reasons == ("host_key_unconfirmed",)
    assert listed.unclaimed_count == 1


async def test_each_blocked_reason_is_named(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rl-why", octet=82, count=1)
    org = world.org.org_id
    # A /23 authorization reaches 10.82.1.0/24; the credential scope and the
    # site server segment cover only 10.82.0.0/24.
    await seed_push_authorization(admin_engine, world.org, "10.82.0.0/23")
    await seed_asset(admin_engine, org, primary_ip="10.82.1.5")
    # A Windows machine with no DNS name, offered an SSH credential.
    await seed_asset(admin_engine, org, primary_ip="10.82.0.30", os_family="windows")

    listed = await _expand(app_engine, world, prefix="10.82.0.0/23")
    by_address = {row.address: row for row in listed.rows}
    assert "credential_not_scoped" in by_address["10.82.1.5"].reasons
    assert "no_site_server" in by_address["10.82.1.5"].reasons
    assert "no_dns_name" in by_address["10.82.0.30"].reasons
    assert "wrong_credential_kind" in by_address["10.82.0.30"].reasons
    assert by_address["10.82.0.10"].ready


async def test_the_digest_is_over_the_sorted_ready_pairs(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rl-dig", octet=83, count=2)
    listed = await _expand(app_engine, world)
    lines = sorted(f"{r.device_id}|{r.address}" for r in listed.ready_rows)
    assert listed.list_digest == hashlib.sha256("\n".join(lines).encode()).hexdigest()
    pairs = [(r.device_id, r.address) for r in listed.ready_rows]
    assert run_list.compute_list_digest(pairs) == run_list.compute_list_digest(reversed(pairs))

    # A ready machine turning blocked changes the digest (R7).
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("DELETE FROM push_host_key WHERE org_id = :o AND address = CAST(:a AS inet)"),
            {"o": world.org.org_id, "a": world.addresses[0]},
        )
        await session.commit()
    assert (await _expand(app_engine, world)).list_digest != listed.list_digest


async def test_only_an_active_authorization_exactly_as_authorized_may_be_expanded(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rl-auth", octet=84, count=1)
    with pytest.raises(PushValidationError, match="not one of this organization"):
        await _expand(app_engine, world, prefix="10.84.0.0/25")
    await seed_push_authorization(admin_engine, world.org, "10.84.4.0/24", revoked=True)
    with pytest.raises(PushValidationError, match="not one of this organization"):
        await _expand(app_engine, world, prefix="10.84.4.0/24")


async def test_more_ready_machines_than_a_run_holds_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch
) -> None:
    world = await run_world(admin_engine, "rl-max", octet=85, count=2)
    monkeypatch.setattr(run_list, "PUSH_RUN_MAX", 1)
    with pytest.raises(PushValidationError, match="narrower"):
        await _expand(app_engine, world)


async def test_a_list_too_long_to_read_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch
) -> None:
    world = await run_world(admin_engine, "rl-long", octet=86, count=2)
    monkeypatch.setattr(run_list, "PUSH_RUN_LIST_MAX", 1)
    with pytest.raises(PushValidationError, match="narrower"):
        await _expand(app_engine, world)


async def test_expansion_requires_credentials_checkout(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from app.models import User

    world = await run_world(admin_engine, "rl-perm", octet=87, count=1)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        stranger = User(
            org_id=world.org.org_id,
            external_subject=f"sub-{uuid4().hex[:8]}",
            email=f"{uuid4().hex[:8]}@test.local",
        )
        session.add(stranger)
        await session.commit()
    with pytest.raises(PushPermissionError, match="credentials.checkout"):
        await _expand(app_engine, world, actor=stranger.id)


async def test_expansion_writes_nothing_and_decrypts_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rl-ro", octet=88, count=2)
    await _expand(app_engine, world)
    async with AsyncSession(admin_engine) as session:
        for model in (Job, EnrollmentToken, AuditEvent):
            count = (
                await session.execute(
                    select(func.count()).select_from(model).where(model.org_id == world.org.org_id)
                )
            ).scalar_one()
            assert count == 0, model.__name__
    # `expand_prefix` takes no vault: decrypting is not expressible here.
    import inspect

    assert "vault" not in inspect.signature(run_list.expand_prefix).parameters


def test_the_bounds_are_the_approved_numbers() -> None:
    from app.modules.agent_push.preflight import PUSH_BATCH_MAX

    assert run_list.PUSH_RUN_MAX == 1024
    assert PUSH_BATCH_MAX == 25


async def test_a_second_scope_on_the_credential_does_not_widen_the_prefix(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A credential scoped wider than the authorization lists nothing outside
    the chosen prefix: the prefix bounds the list, not the scope."""
    world = await run_world(admin_engine, "rl-wide", octet=89, count=1)
    await seed_push_credential_scope(admin_engine, world.org, world.credential_id, "10.89.0.0/16")
    await seed_asset(admin_engine, world.org.org_id, primary_ip="10.89.7.7")
    listed = await _expand(app_engine, world)
    assert [r.address for r in listed.rows] == ["10.89.0.10"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_list.py`
Expected: FAIL with `ImportError: cannot import name 'run_list' from 'app.modules.agent_push'`.

- [ ] **Step 3: Write `run_list.py`**

Create `backend/app/modules/agent_push/run_list.py`:

```python
"""A subnet, expanded into the named machines a person confirms
(docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §1.2, §1.3;
CLAUDE.md 2a(d), 2a(f) as amended 2026-09-30).

**A SUBNET BUILDS THE LIST; IT IS NEVER THE THING CONFIRMED.** The subnet must
be one of the organization's active push authorizations, exactly as
authorized. It expands to the vouched-for, approved, unenrolled machines whose
address it contains. Each is evaluated by the SAME ladder a ticked push runs
(`preflight.evaluate_targets`) and the SAME install-only checks
(`dispatch.pre_dispatch_checks`). A machine that fails any of them is listed
as blocked, with every reason, and is not part of what is confirmed.

**THE DIGEST IS OVER THE READY PAIRS** (R7). What a person confirms is "install
on these machines". A blocked machine that turns ready between showing and
confirming must change the digest, or they would have confirmed a machine
they were told would not be touched.

**READS ONLY.** Nothing here writes a row, decrypts a secret or signs
anything, and `expand_prefix` takes no vault. Every query carries an explicit
`org_id` predicate in addition to RLS.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import has_permission
from app.modules.agent_push.credential_scope import resolve_push_credential
from app.modules.agent_push.dispatch import pre_dispatch_checks
from app.modules.agent_push.errors import PushPermissionError, PushValidationError
from app.modules.agent_push.models import PushAuthorization
from app.modules.agent_push.net import IPAddress, parse_prefix, parse_target_address
from app.modules.agent_push.preflight import VOUCHED_PROVENANCE, TargetDecision, evaluate_targets

__all__ = [
    "LIST_REASONS",
    "PUSH_RUN_LIST_MAX",
    "PUSH_RUN_MAX",
    "RunList",
    "RunListRow",
    "authorized_prefixes",
    "compute_list_digest",
    "expand_prefix",
]

#: CLAUDE.md 2a(f) as amended 2026-09-30: at most 1,024 named targets per run.
#: It counts READY machines, not the prefix's size: a /16 whose vouched-for,
#: ready machines number 300 is fine (design §1.2).
PUSH_RUN_MAX = 1024

#: How many vouched-for candidates one expansion evaluates (R13). A list longer
#: than this is not something a person reads, and each candidate costs the
#: ladder's queries. It is refused with the same advice.
PUSH_RUN_LIST_MAX = 4096

#: The one blocking reason expansion adds to the ladder's and dispatch's.
LIST_REASONS: dict[str, str] = {
    "not_installable": (
        "the machine's operating system or processor is not recorded, or is not one "
        "push can install onto. Set its push install details on the machine's page"
    ),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RunListRow:
    """One listed machine. `reasons` empty means ready."""

    device_id: UUID
    address: str
    hostname: str
    os_family: str
    reasons: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.reasons


def compute_list_digest(pairs: Iterable[tuple[UUID, str]]) -> str:
    """SHA-256 over the sorted `(device_id, address)` pairs: one
    `<device_id>|<address>` line each, sorted, joined by a newline, UTF-8,
    lowercase hex. Only the server computes it; the console echoes it back."""
    lines = sorted(f"{device_id}|{address}" for device_id, address in pairs)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunList:
    prefix: str
    credential_id: UUID
    credential_name: str
    rows: tuple[RunListRow, ...]
    unclaimed_count: int

    @property
    def ready_rows(self) -> tuple[RunListRow, ...]:
        return tuple(row for row in self.rows if row.ready)

    @property
    def list_digest(self) -> str:
        return compute_list_digest((row.device_id, row.address) for row in self.ready_rows)


async def authorized_prefixes(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> list[PushAuthorization]:
    """The subnets a run may be built from: this org's ACTIVE push
    authorizations. Nothing else may be chosen, and nothing may be typed."""
    stamp = now or _utcnow()
    rows = await session.execute(
        select(PushAuthorization)
        .where(
            PushAuthorization.org_id == org_id,
            PushAuthorization.revoked_at.is_(None),
            or_(PushAuthorization.expires_at.is_(None), PushAuthorization.expires_at > stamp),
        )
        .order_by(PushAuthorization.prefix)
    )
    return list(rows.scalars())


async def _is_authorized_prefix(
    session: AsyncSession, org_id: UUID, prefix: str, now: datetime
) -> bool:
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM push_authorization"
                " WHERE org_id = :org AND revoked_at IS NULL"
                "   AND (expires_at IS NULL OR expires_at > :now)"
                "   AND prefix = CAST(:prefix AS cidr)"
                " LIMIT 1"
            ),
            {"org": str(org_id), "now": now, "prefix": prefix},
        )
    ).first()
    return row is not None


async def _candidate_addresses(
    session: AsyncSession, org_id: UUID, prefix: str
) -> list[IPAddress]:
    """Every address inside `prefix` that a vouched-for, approved row holds
    (CLAUDE.md 2a(d), R5). Containment is decided by Postgres in native `inet`."""
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT primary_ip FROM devices"
                " WHERE org_id = :org"
                "   AND primary_ip <<= CAST(:prefix AS cidr)"
                "   AND provenance = ANY(CAST(:vouched AS text[]))"
                "   AND review_state = 'approved'"
                " ORDER BY primary_ip"
            ),
            {"org": str(org_id), "prefix": prefix, "vouched": list(VOUCHED_PROVENANCE)},
        )
    ).all()
    return [parse_target_address(str(row.primary_ip)) for row in rows]


async def _unclaimed_count(session: AsyncSession, org_id: UUID, prefix: str) -> int:
    """Rows a sweep recorded on its own inside `prefix`: never listed, only
    counted, so the operator can go and claim them (design §1.2)."""
    return int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM devices"
                    " WHERE org_id = :org AND primary_ip <<= CAST(:prefix AS cidr)"
                    "   AND provenance = 'sweep'"
                ),
                {"org": str(org_id), "prefix": prefix},
            )
        ).scalar_one()
    )


async def _enrolled(session: AsyncSession, org_id: UUID, device_ids: list[UUID]) -> set[UUID]:
    if not device_ids:
        return set()
    rows = (
        await session.execute(
            text(
                "SELECT device_id FROM agents"
                " WHERE org_id = :org AND device_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"org": str(org_id), "ids": [str(i) for i in device_ids]},
        )
    ).all()
    return {UUID(str(row.device_id)) for row in rows}


async def _device_facts(
    session: AsyncSession, org_id: UUID, device_ids: list[UUID]
) -> dict[UUID, tuple[str, str]]:
    if not device_ids:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT id, hostname, os_family FROM devices"
                " WHERE org_id = :org AND id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"org": str(org_id), "ids": [str(i) for i in device_ids]},
        )
    ).all()
    return {UUID(str(row.id)): (row.hostname or "", row.os_family or "") for row in rows}


async def expand_prefix(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID,
    prefix: str,
    credential_id: UUID,
    public_console_url: str | None,
    now: datetime | None = None,
) -> RunList:
    """The list for `prefix` and `credential_id`, as a person will be shown it.

    Raises, in this order:
    * `PushPermissionError` when the caller does not hold `credentials.checkout`.
      This is the same conjunction `request_push_batch` checks first, because a
      caller who may not use a stored secret must not be told which machines it
      would reach;
    * `PushNotFoundError` / `PushValidationError` for a credential that is not
      this org's, or not a push credential;
    * `PushValidationError` for a prefix that is not an active authorization
      exactly as authorized, a list longer than `PUSH_RUN_LIST_MAX`, or more
      than `PUSH_RUN_MAX` ready machines.
    """
    if not await has_permission(session, actor_id, "credentials.checkout"):
        raise PushPermissionError(
            "a subnet push requires the `credentials.checkout` permission as well as "
            "`agent.push.run`: it presents a stored secret to every machine it lists"
        )
    stamp = now or _utcnow()
    canonical = str(parse_prefix(prefix))
    credential = await resolve_push_credential(session, org_id, credential_id)
    if not await _is_authorized_prefix(session, org_id, canonical, stamp):
        raise PushValidationError(
            f"{canonical} is not one of this organization's active push authorizations. "
            "A subnet push is built from one of those, exactly as authorized; a holder "
            "of agent.push.scope.manage authorizes a prefix"
        )
    addresses = await _candidate_addresses(session, org_id, canonical)
    if len(addresses) > PUSH_RUN_LIST_MAX:
        raise PushValidationError(
            f"{canonical} holds {len(addresses)} machines somebody vouched for, more than "
            f"the {PUSH_RUN_LIST_MAX} one list can show a person. Choose a narrower subnet"
        )
    decisions = await evaluate_targets(
        session, org_id, addresses=addresses, credential_id=credential_id, now=stamp
    )
    candidates: list[tuple[TargetDecision, UUID]] = []
    for decision in decisions:
        if decision.device_id is not None:
            candidates.append((decision, decision.device_id))
    device_ids = [device_id for _decision, device_id in candidates]
    enrolled = await _enrolled(session, org_id, device_ids)
    facts = await _device_facts(session, org_id, device_ids)

    rows: list[RunListRow] = []
    for decision, device_id in candidates:
        if device_id in enrolled:
            continue
        reasons = list(decision.refusals)
        try:
            checks = await pre_dispatch_checks(
                session,
                org_id,
                decisions=[decision],
                credential=credential,
                kind="install",
                public_console_url=public_console_url,
            )
        except PushValidationError:
            reasons.append("not_installable")
        else:
            reasons.extend(
                r for r in checks.refusals.get(decision.address, []) if r not in reasons
            )
        hostname, os_family = facts.get(device_id, ("", ""))
        rows.append(
            RunListRow(
                device_id=device_id,
                address=decision.address,
                hostname=hostname,
                os_family=os_family,
                reasons=tuple(reasons),
            )
        )
    rows.sort(key=lambda row: ip_address(row.address))

    ready = sum(1 for row in rows if row.ready)
    if ready > PUSH_RUN_MAX:
        raise PushValidationError(
            f"{canonical} expands to {ready} machines ready for a push; a run holds at "
            f"most {PUSH_RUN_MAX}. Choose a narrower subnet"
        )
    return RunList(
        prefix=canonical,
        credential_id=credential.id,
        credential_name=credential.name,
        rows=tuple(rows),
        unclaimed_count=await _unclaimed_count(session, org_id, canonical),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_list.py tests/test_agent_push_dispatches_nothing.py`
Expected: PASS. The dispatches-nothing guard parametrizes over `run_list.py` and finds no signing name in it.

- [ ] **Step 5: Lint, type-check, and commit**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: no findings.

```bash
git add backend/app/modules/agent_push/run_list.py backend/tests/test_push_run_list.py
git commit -m "feat(agent-push): expand an authorized subnet into the named list a person confirms, digest over ready pairs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---
### Task 5: The run lifecycle — create, pause, resume, cancel, read

**Files:**
- Modify: `backend/app/modules/agent_push/errors.py` (`PushListChangedError`)
- Create: `backend/app/modules/agent_push/runs.py` (the lifecycle half; Task 6 adds the pacer and Task 8 the beat)
- Modify: `backend/tests/push_run_fixtures.py` (add `start_run`)
- Test: `backend/tests/test_push_run_lifecycle.py` (create)

**Interfaces:**
- Consumes: `run_list.expand_prefix`, `models.PushRun`, `models.PushRunTarget`, `models.PUSH_RUN_ACTIVE_STATES`, `models.PUSH_RUN_TARGET_STATES`.
- Produces:
  - `errors.PushListChangedError(PushConflictError)`.
  - `runs.PUSH_RUN_CONCURRENCY = 50`.
  - `runs.create_run(session, org_id, *, actor_id, prefix, credential_id, list_digest, public_console_url, now=None) -> PushRun`.
  - `runs.get_run(session, org_id, run_id) -> PushRun`, which raises `PushNotFoundError`.
  - `runs.list_runs(session, org_id, *, limit=50) -> list[PushRun]`.
  - `runs.run_counts(session, org_id, run_id) -> dict[str, int]`, keyed by every `PUSH_RUN_TARGET_STATES` value.
  - `runs.list_targets(session, org_id, run_id, *, offset, limit) -> tuple[int, list[PushRunTarget]]`.
  - `runs.pause_run`, `runs.resume_run` and `runs.cancel_run`, each `(session, org_id, *, run_id, actor_id, now=None) -> PushRun`.
    - Pause works only from `running`.
    - Resume works from `paused` or `stopped_credential`. It restarts the lockout guard's window: `auth_window_started_at = now`, and `stopped_at` is cleared.
    - Cancel works from any active state, including `stopped_credential`.

- [ ] **Step 1: Add the `start_run` fixture**

Append to `backend/tests/push_run_fixtures.py`:

```python
async def start_run(app_engine: AsyncEngine, world: RunWorld) -> UUID:
    """Expand, then confirm exactly what was shown, as a person would. Commits
    the run and dispatches NOTHING (no pacer is called here)."""
    from app.core.tenancy import org_session
    from app.modules.agent_push import run_list, runs

    async with org_session(world.org.org_id, engine=app_engine) as session:
        listed = await run_list.expand_prefix(
            session,
            world.org.org_id,
            actor_id=world.org.user_id,
            prefix=world.prefix,
            credential_id=world.credential_id,
            public_console_url=CONSOLE,
        )
        run = await runs.create_run(
            session,
            world.org.org_id,
            actor_id=world.org.user_id,
            prefix=world.prefix,
            credential_id=world.credential_id,
            list_digest=listed.list_digest,
            public_console_url=CONSOLE,
        )
        return run.id
```

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_push_run_lifecycle.py`:

```python
"""A run's life: confirmed, paused, resumed, cancelled -- each a person's act,
RBAC-checked and audited (design §1.3, §1.5; CLAUDE.md 2a(e) as amended).
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, EnrollmentToken, Job, User
from app.modules.agent_push import run_list, runs
from app.modules.agent_push.errors import (
    PushConflictError,
    PushListChangedError,
    PushNotFoundError,
    PushPermissionError,
)
from app.modules.agent_push.models import PushRun, PushRunTarget
from tests.push_run_fixtures import CONSOLE, RunWorld, run_world, start_run


async def _audits(admin_engine: AsyncEngine, org_id, action: str) -> list[AuditEvent]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == action
                    )
                )
            ).scalars()
        )


async def _count(admin_engine: AsyncEngine, model, org_id) -> int:
    async with AsyncSession(admin_engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(model.org_id == org_id)
                )
            ).scalar_one()
        )


async def _create(app_engine: AsyncEngine, world: RunWorld, digest: str):
    async with org_session(world.org.org_id, engine=app_engine) as session:
        return await runs.create_run(
            session,
            world.org.org_id,
            actor_id=world.org.user_id,
            prefix=world.prefix,
            credential_id=world.credential_id,
            list_digest=digest,
            public_console_url=CONSOLE,
        )


async def _control(app_engine: AsyncEngine, world: RunWorld, verb: str, run_id, actor=None):
    fn = {"pause": runs.pause_run, "resume": runs.resume_run, "cancel": runs.cancel_run}[verb]
    async with org_session(world.org.org_id, engine=app_engine) as session:
        return await fn(
            session, world.org.org_id, run_id=run_id, actor_id=actor or world.org.user_id
        )


async def test_a_confirmed_list_becomes_a_sealed_run_and_nothing_is_dispatched(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-new", octet=91, count=2)
    run_id = await start_run(app_engine, world)

    async with AsyncSession(admin_engine) as session:
        run = (await session.execute(select(PushRun).where(PushRun.id == run_id))).scalar_one()
        targets = list(
            (
                await session.execute(
                    select(PushRunTarget)
                    .where(PushRunTarget.run_id == run_id)
                    .order_by(PushRunTarget.position)
                )
            ).scalars()
        )
    assert run.state == "running" and run.sealed_at is not None
    assert run.target_count == 2 and str(run.prefix) == world.prefix
    assert [str(t.address) for t in targets] == list(world.addresses)
    assert [t.position for t in targets] == [0, 1]
    assert {t.state for t in targets} == {"waiting"}
    assert [t.device_id for t in targets] == list(world.device_ids)

    (started,) = await _audits(admin_engine, world.org.org_id, "push.run.started")
    assert started.actor_type == "user" and started.actor_id == world.org.user_id
    assert started.meta["target_count"] == 2
    assert started.meta["list_digest"] == run.list_digest
    # Creating a run signs nothing and mints nothing: the pacer does that.
    assert await _count(admin_engine, Job, world.org.org_id) == 0
    assert await _count(admin_engine, EnrollmentToken, world.org.org_id) == 0


async def test_a_changed_list_is_refused_before_anything_is_written(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-chg", octet=92, count=2)
    async with org_session(world.org.org_id, engine=app_engine) as session:
        shown = await run_list.expand_prefix(
            session,
            world.org.org_id,
            actor_id=world.org.user_id,
            prefix=world.prefix,
            credential_id=world.credential_id,
            public_console_url=CONSOLE,
        )
    # Between showing and confirming, one machine stops being ready.
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("DELETE FROM push_host_key WHERE org_id = :o AND address = CAST(:a AS inet)"),
            {"o": world.org.org_id, "a": world.addresses[1]},
        )
        await session.commit()

    with pytest.raises(PushListChangedError):
        await _create(app_engine, world, shown.list_digest)
    with pytest.raises(PushListChangedError):
        await _create(app_engine, world, "0" * 64)
    assert await _count(admin_engine, PushRun, world.org.org_id) == 0
    assert await _count(admin_engine, AuditEvent, world.org.org_id) == 0


async def test_one_active_run_per_organization(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-one", octet=93, count=1)
    first = await start_run(app_engine, world)
    with pytest.raises(PushConflictError, match="one active run"):
        await start_run(app_engine, world)
    await _control(app_engine, world, "pause", first)
    with pytest.raises(PushConflictError, match="one active run"):
        await start_run(app_engine, world)
    await _control(app_engine, world, "cancel", first)
    await start_run(app_engine, world)


async def test_pause_resume_and_cancel_are_audited_and_cancel_is_final(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-ctl", octet=94, count=2)
    run_id = await start_run(app_engine, world)

    assert (await _control(app_engine, world, "pause", run_id)).state == "paused"
    assert (await _control(app_engine, world, "resume", run_id)).state == "running"
    cancelled = await _control(app_engine, world, "cancel", run_id)
    assert cancelled.state == "cancelled" and cancelled.finished_at is not None

    for action in ("push.run.paused", "push.run.resumed", "push.run.cancelled"):
        (event,) = await _audits(admin_engine, world.org.org_id, action)
        assert event.actor_type == "user" and event.actor_id == world.org.user_id
        assert event.target_id == str(run_id)
    (cancel_event,) = await _audits(admin_engine, world.org.org_id, "push.run.cancelled")
    assert cancel_event.meta["cancelled_waiting"] == 2

    async with AsyncSession(admin_engine) as session:
        states = set(
            (
                await session.execute(
                    select(PushRunTarget.state).where(PushRunTarget.run_id == run_id)
                )
            ).scalars()
        )
    assert states == {"cancelled"}
    for verb in ("resume", "pause", "cancel"):
        with pytest.raises(PushConflictError):
            await _control(app_engine, world, verb, run_id)


async def test_resume_from_a_safety_stop_is_a_persons_act_and_restarts_the_window(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Spec §1.4a: a `stopped_credential` run is still active, cannot be
    paused, and only a person's Resume restarts it -- with a fresh window, so the
    pacer starts again from one target."""
    world = await run_world(admin_engine, "rc-stop", octet=99, count=1)
    run_id = await start_run(app_engine, world)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "UPDATE push_run SET state = 'stopped_credential', stopped_at = now(),"
                " auth_window_started_at = now() - interval '1 hour' WHERE id = :r"
            ),
            {"r": run_id},
        )
        await session.commit()
    with pytest.raises(PushConflictError, match="one active run"):
        await start_run(app_engine, world)
    with pytest.raises(PushConflictError):
        await _control(app_engine, world, "pause", run_id)

    before = datetime.now(UTC)
    resumed = await _control(app_engine, world, "resume", run_id)
    assert resumed.state == "running" and resumed.stopped_at is None
    assert resumed.auth_window_started_at >= before
    (event,) = await _audits(admin_engine, world.org.org_id, "push.run.resumed")
    assert event.actor_id == world.org.user_id
    assert event.meta["from_state"] == "stopped_credential"


async def test_resume_requires_credentials_checkout(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-res", octet=95, count=1)
    run_id = await start_run(app_engine, world)
    await _control(app_engine, world, "pause", run_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        stranger = User(
            org_id=world.org.org_id,
            external_subject=f"sub-{uuid4().hex[:8]}",
            email=f"{uuid4().hex[:8]}@test.local",
        )
        session.add(stranger)
        await session.commit()
    with pytest.raises(PushPermissionError, match="credentials.checkout"):
        await _control(app_engine, world, "resume", run_id, actor=stranger.id)


async def test_another_orgs_run_is_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    owner = await run_world(admin_engine, "rc-own", octet=96, count=1)
    other = await run_world(admin_engine, "rc-oth", octet=97, count=1)
    run_id = await start_run(app_engine, owner)
    with pytest.raises(PushNotFoundError):
        await _control(app_engine, other, "pause", run_id)
    with pytest.raises(PushNotFoundError):
        async with org_session(other.org.org_id, engine=app_engine) as session:
            await runs.get_run(session, other.org.org_id, run_id)


async def test_counts_and_pages_are_read_in_position_order(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-page", octet=98, count=3)
    run_id = await start_run(app_engine, world)
    async with org_session(world.org.org_id, engine=app_engine) as session:
        counts = await runs.run_counts(session, world.org.org_id, run_id)
        total, page = await runs.list_targets(session, world.org.org_id, run_id, offset=1, limit=1)
    assert counts == {
        "waiting": 3, "in_flight": 0, "installed": 0, "failed": 0, "refused": 0, "cancelled": 0,
    }
    assert total == 3 and [t.position for t in page] == [1]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_lifecycle.py`
Expected: FAIL with `ImportError: cannot import name 'PushListChangedError'`.

- [ ] **Step 4: Add the error**

In `backend/app/modules/agent_push/errors.py`, add `"PushListChangedError",` to `__all__`, and add this after `class PushConflictError`:

```python
class PushListChangedError(PushConflictError):
    """The subnet no longer expands to the list the person was shown
    (design §1.3). Refused before any row is written or any secret touched;
    the console shows the list again. A 409 whose body names `list_changed`,
    so the console can tell it from "a run is already active"."""
```

- [ ] **Step 5: Write the lifecycle half of `runs.py`**

Create `backend/app/modules/agent_push/runs.py`:

```python
"""Push runs: a subnet push a person confirmed, continued 50 at a time
(docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §1.3-§1.5;
CLAUDE.md 2a(e), 2a(f) as amended 2026-09-30).

**A PERSON STARTS, PAUSES, RESUMES AND CANCELS. THE PACER ONLY CONTINUES.**
`create_run` writes the confirmed list once and seals it. The database trigger
`push_run_target_fixed` then refuses any further target, and only
`create_run` constructs a `PushRunTarget` (an AST test pins this). The pacer
(`advance_run`, below) never writes `state = 'running'`: only `create_run` and
`resume_run` do, and both are a person's act.

**THIS FILE SIGNS NOTHING AND DECRYPTS NOTHING.** The pacer is handed a
`RunTargetDispatcher` (`run_contract.py`), which `dispatch.RunDispatcher`
implements. `tests/test_agent_push_dispatches_nothing.py` holds this file to
that.

**EVERY QUERY CARRIES AN EXPLICIT `org_id` PREDICATE**, as in the rest of the
package, in addition to RLS.
"""

import hmac
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.rbac import has_permission
from app.modules.agent_push.errors import (
    PushConflictError,
    PushListChangedError,
    PushNotFoundError,
    PushPermissionError,
    PushValidationError,
)
from app.modules.agent_push.models import (
    PUSH_RUN_ACTIVE_STATES,
    PUSH_RUN_TARGET_STATES,
    PushRun,
    PushRunTarget,
)
from app.modules.agent_push.run_list import expand_prefix

__all__ = [
    "PUSH_RUN_CONCURRENCY",
    "cancel_run",
    "create_run",
    "get_run",
    "list_runs",
    "list_targets",
    "pause_run",
    "resume_run",
    "run_counts",
]

#: CLAUDE.md 2a(f) as amended 2026-09-30: at most 50 of a run's targets in
#: flight at once. Changed only by a code change, like `PUSH_BATCH_MAX`.
PUSH_RUN_CONCURRENCY = 50


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _lock_org_runs(session: AsyncSession, org_id: UUID) -> None:
    """Serialize run creation per org, so two confirmations cannot both pass
    the "no active run" check. `uq_push_run_active` is the backstop."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('push_run:' || :org))"),
        {"org": str(org_id)},
    )


async def create_run(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID,
    prefix: str,
    credential_id: UUID,
    list_digest: str,
    public_console_url: str | None,
    now: datetime | None = None,
) -> PushRun:
    """A person confirms the list they were shown. It becomes a sealed run.

    **THE LIST IS EXPANDED AGAIN AND ITS DIGEST COMPARED BEFORE ANY ROW IS
    WRITTEN OR ANY SECRET IS TOUCHED** (design §1.3). There is no vault
    parameter here: creating a run decrypts nothing and signs nothing.
    Refuses:
    * everything `expand_prefix` refuses, including the `credentials.checkout`
      conjunction;
    * `PushListChangedError` when the digest does not match;
    * `PushValidationError` when nothing is ready;
    * `PushConflictError` when a run is already running, paused or stopped by
      the lockout guard.
    """
    stamp = now or _utcnow()
    confirmed = await expand_prefix(
        session,
        org_id,
        actor_id=actor_id,
        prefix=prefix,
        credential_id=credential_id,
        public_console_url=public_console_url,
        now=stamp,
    )
    if not hmac.compare_digest(confirmed.list_digest, list_digest):
        raise PushListChangedError(
            "the machines in this subnet changed since the list was shown, so the "
            "confirmation no longer describes them. Nothing was started; read the "
            "list again and confirm what it shows now"
        )
    ready = confirmed.ready_rows
    if not ready:
        raise PushValidationError(
            "no machine in this subnet is ready for a push, so there is nothing to confirm"
        )
    await _lock_org_runs(session, org_id)
    active = (
        await session.execute(
            select(PushRun.id).where(
                PushRun.org_id == org_id, PushRun.state.in_(PUSH_RUN_ACTIVE_STATES)
            )
        )
    ).first()
    if active is not None:
        raise PushConflictError(
            "this organization already has a push run that is running, paused or stopped "
            "after sign-in failures: one active run per organization. Let it finish, or "
            "cancel it, first"
        )

    run = PushRun(
        org_id=org_id,
        created_by=actor_id,
        credential_id=confirmed.credential_id,
        prefix=confirmed.prefix,
        list_digest=confirmed.list_digest,
        state="running",
        target_count=len(ready),
        created_at=stamp,
        updated_at=stamp,
        # The lockout guard's window opens now: one target first (spec §1.4a).
        auth_window_started_at=stamp,
    )
    session.add(run)
    await session.flush()
    for position, row in enumerate(ready):
        session.add(
            PushRunTarget(
                org_id=org_id,
                run_id=run.id,
                device_id=row.device_id,
                address=row.address,
                position=position,
                state="waiting",
                updated_at=stamp,
            )
        )
    await session.flush()
    # SEALED IN THE SAME TRANSACTION: from here the database refuses any new
    # target for this run (`push_run_target_fixed`).
    run.sealed_at = stamp
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="push.run.started",
        target_type="push_run",
        target_id=str(run.id),
        metadata={
            "prefix": confirmed.prefix,
            "credential_id": str(confirmed.credential_id),
            "credential_name": confirmed.credential_name,
            "target_count": len(ready),
            "blocked_count": len(confirmed.rows) - len(ready),
            "list_digest": confirmed.list_digest,
            "concurrency": PUSH_RUN_CONCURRENCY,
        },
    )
    return run


async def get_run(session: AsyncSession, org_id: UUID, run_id: UUID) -> PushRun:
    run = (
        await session.execute(
            select(PushRun).where(PushRun.org_id == org_id, PushRun.id == run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise PushNotFoundError(f"push run {run_id} not found")
    return run


async def _locked_run(session: AsyncSession, org_id: UUID, run_id: UUID) -> PushRun:
    run = (
        await session.execute(
            select(PushRun)
            .where(PushRun.org_id == org_id, PushRun.id == run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        raise PushNotFoundError(f"push run {run_id} not found")
    return run


async def list_runs(session: AsyncSession, org_id: UUID, *, limit: int = 50) -> list[PushRun]:
    rows = await session.execute(
        select(PushRun)
        .where(PushRun.org_id == org_id)
        .order_by(PushRun.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def run_counts(session: AsyncSession, org_id: UUID, run_id: UUID) -> dict[str, int]:
    """Per-state counts, computed on read (R8): no stored counter to drift."""
    rows = (
        await session.execute(
            select(PushRunTarget.state, func.count())
            .where(PushRunTarget.org_id == org_id, PushRunTarget.run_id == run_id)
            .group_by(PushRunTarget.state)
        )
    ).all()
    counts = {state: 0 for state in PUSH_RUN_TARGET_STATES}
    for state, number in rows:
        counts[str(state)] = int(number)
    return counts


async def list_targets(
    session: AsyncSession, org_id: UUID, run_id: UUID, *, offset: int, limit: int
) -> tuple[int, list[PushRunTarget]]:
    await get_run(session, org_id, run_id)
    total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(PushRunTarget)
                .where(PushRunTarget.org_id == org_id, PushRunTarget.run_id == run_id)
            )
        ).scalar_one()
    )
    rows = await session.execute(
        select(PushRunTarget)
        .where(PushRunTarget.org_id == org_id, PushRunTarget.run_id == run_id)
        .order_by(PushRunTarget.position)
        .offset(offset)
        .limit(limit)
    )
    return total, list(rows.scalars())


async def _audit_control(
    session: AsyncSession,
    run: PushRun,
    *,
    actor_id: UUID,
    action: str,
    extra: dict[str, Any] | None = None,
) -> None:
    await audit_service.record(
        session,
        org_id=run.org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        target_type="push_run",
        target_id=str(run.id),
        metadata={
            "prefix": str(run.prefix),
            "state": run.state,
            "counts": await run_counts(session, run.org_id, run.id),
            **(extra or {}),
        },
    )


async def pause_run(
    session: AsyncSession, org_id: UUID, *, run_id: UUID, actor_id: UUID,
    now: datetime | None = None,
) -> PushRun:
    """New dispatches stop; installs already in flight finish (design §1.5)."""
    run = await _locked_run(session, org_id, run_id)
    if run.state != "running":
        raise PushConflictError(f"only a running push run can be paused; this one is {run.state}")
    run.state = "paused"
    run.updated_at = now or _utcnow()
    await session.flush()
    await _audit_control(session, run, actor_id=actor_id, action="push.run.paused")
    return run


async def resume_run(
    session: AsyncSession, org_id: UUID, *, run_id: UUID, actor_id: UUID,
    now: datetime | None = None,
) -> PushRun:
    """A PERSON's act. Only here and in `create_run` does a run become
    `running`; the pacer cannot resume (CLAUDE.md 2a(e) as amended).

    Resumes a `paused` run, or a `stopped_credential` run the lockout guard
    stopped (spec §1.4a), typically after the credential was fixed. Either way
    the guard's window restarts: `auth_window_started_at` moves to now, so
    earlier `auth_failed` results no longer count and the run goes back to ONE
    target in flight until a sign-in succeeds again.

    Needs `credentials.checkout` as well as `agent.push.run`, because
    resuming causes stored secrets to be decrypted and presented again."""
    if not await has_permission(session, actor_id, "credentials.checkout"):
        raise PushPermissionError(
            "resuming a push run requires the `credentials.checkout` permission as well "
            "as `agent.push.run`: it presents a stored secret to the machines still waiting"
        )
    stamp = now or _utcnow()
    run = await _locked_run(session, org_id, run_id)
    if run.state not in ("paused", "stopped_credential"):
        final = " A cancelled run is final." if run.state == "cancelled" else ""
        raise PushConflictError(
            "only a paused push run, or one stopped after sign-in failures, can be "
            f"resumed; this one is {run.state}.{final}"
        )
    from_state = run.state
    run.state = "running"
    run.auth_window_started_at = stamp
    run.stopped_at = None
    run.updated_at = stamp
    await session.flush()
    await _audit_control(
        session, run, actor_id=actor_id, action="push.run.resumed",
        extra={"from_state": from_state},
    )
    return run


async def cancel_run(
    session: AsyncSession, org_id: UUID, *, run_id: UUID, actor_id: UUID,
    now: datetime | None = None,
) -> PushRun:
    """Every waiting target becomes `cancelled`, and the run is final. Installs
    already in flight finish, and their outcomes are still recorded."""
    stamp = now or _utcnow()
    run = await _locked_run(session, org_id, run_id)
    if run.state not in PUSH_RUN_ACTIVE_STATES:
        raise PushConflictError(
            f"this push run is already {run.state}; cancelling is final and cannot be repeated"
        )
    result = await session.execute(
        update(PushRunTarget)
        .where(
            PushRunTarget.org_id == org_id,
            PushRunTarget.run_id == run.id,
            PushRunTarget.state == "waiting",
        )
        .values(state="cancelled", updated_at=stamp)
        .execution_options(synchronize_session=False)
    )
    run.state = "cancelled"
    run.finished_at = stamp
    run.updated_at = stamp
    await session.flush()
    await _audit_control(
        session,
        run,
        actor_id=actor_id,
        action="push.run.cancelled",
        extra={"cancelled_waiting": int(result.rowcount or 0)},  # type: ignore[attr-defined]
    )
    return run
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_lifecycle.py tests/test_push_run_migration.py tests/test_agent_push_dispatches_nothing.py`
Expected: PASS.

- [ ] **Step 7: Lint, type-check, and commit**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: no findings. If mypy reports the `type: ignore` on `result.rowcount` as unused, delete that comment. Its only purpose is to cover SQLAlchemy stub versions that type `execute()` as returning `Result` rather than `CursorResult`.

```bash
git add backend/app/modules/agent_push/errors.py backend/app/modules/agent_push/runs.py backend/tests/push_run_fixtures.py backend/tests/test_push_run_lifecycle.py
git commit -m "feat(agent-push): push run lifecycle -- confirm a digest-bound list, pause, resume, cancel (final), all audited

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---
### Task 6: The pacer — `advance_run`, one first then at most 50 in flight, a stop on sign-in failures, and a full re-check before every target

**Files:**
- Modify: `backend/app/modules/agent_push/runs.py`: add the pacer.
- Modify: `backend/app/modules/agent_push/dispatch.py`: `record_install_result` settles a run target.
- Test: `backend/tests/test_push_run_pacer.py` (create)

**Interfaces:**
- Consumes:
  - `run_contract.RunTargetDispatcher`, `run_contract.RunTargetDispatch` and `run_contract.Envelope`;
  - `preflight.evaluate_targets`;
  - `credential_scope.resolve_push_credential`;
  - `dispatch.RunDispatcher` (in the tests).
- Produces:
  - `runs.RUN_TARGET_LOST_AFTER = timedelta(minutes=30)`.
  - `runs.RUN_REFUSAL_REASONS: dict[str, str]`.
  - `runs.PUSH_RUN_AUTH_FAILURE_LIMIT = 3` (spec §1.4a).
  - `runs.SIGNED_IN_OUTCOMES = frozenset({"installed", "os_mismatch"})`, and `runs._signed_in(outcome, result) -> bool` (R19).
  - `runs.auth_window(session, run) -> tuple[int, bool]`: the number of `auth_failed` results since the run last started or resumed, and whether a sign-in has succeeded since then.
  - The lockout guard (spec §1.4a):
    - at most ONE target is in flight until a target settled since `auth_window_started_at` has `signed_in`;
    - after that, up to `PUSH_RUN_CONCURRENCY`;
    - once `PUSH_RUN_AUTH_FAILURE_LIMIT` targets settle `auth_failed` in the window while targets are still waiting, the run moves to `stopped_credential`. This is audited as `push.run.stopped` (`actor_type="system"`, `on_behalf_of` the creator). The pacer never leaves that state.
  - `runs.advance_run(session, org_id, run_id, *, dispatch: RunTargetDispatcher, now=None) -> list[Envelope]`, which takes `SELECT … FOR UPDATE` on the run.
  - `runs.settle_target_for_job(session, job) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_push_run_pacer.py`:

```python
"""The pacer (design §1.4, §1.4a, §1.5; CLAUDE.md 2a(e), 2a(f) as amended
2026-09-30).

One target first, and at most 50 in flight once a sign-in has succeeded, even
under concurrent calls. Three `auth_failed` results stop the run as
`stopped_credential`, and only a person's Resume restarts it, again from one.
The whole ladder is re-run
before every target, so a withdrawn credential refuses the rest one by one
with no decrypt. The pacer never resumes and never adds a target. Every test
seeds the world on the admin engine and drives the pacer through
`org_session`, as the beat and the API do.
"""

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app as app_pkg
from app.core.tenancy import org_session
from app.models import AuditEvent, Job
from app.modules.agent_push import preflight, runs
from app.modules.agent_push.errors import PushValidationError
from app.modules.agent_push.models import PushRun, PushRunTarget
from tests.agent_push_fixtures import seed_asset
from tests.ipam_fixtures import seed_agent
from tests.push_run_fixtures import (
    FakeVault,
    RunWorld,
    job_engine,
    make_dispatcher,
    run_world,
    start_run,
)

APP = Path(next(iter(app_pkg.__path__)))


@pytest.fixture(autouse=True)
def _register_push_job_types() -> None:
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


async def _advance(app_engine: AsyncEngine, world: RunWorld, run_id, dispatcher) -> list[Any]:
    async with org_session(world.org.org_id, engine=app_engine) as session:
        return await runs.advance_run(
            session, world.org.org_id, run_id, dispatch=dispatcher.dispatch_target
        )


async def _targets(admin_engine: AsyncEngine, run_id) -> list[PushRunTarget]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(PushRunTarget)
                    .where(PushRunTarget.run_id == run_id)
                    .order_by(PushRunTarget.position)
                )
            ).scalars()
        )


async def _states(admin_engine: AsyncEngine, run_id) -> list[str]:
    return [t.state for t in await _targets(admin_engine, run_id)]


async def _jobs(admin_engine: AsyncEngine, world: RunWorld) -> int:
    async with AsyncSession(admin_engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(Job).where(Job.org_id == world.org.org_id)
                )
            ).scalar_one()
        )


async def _audits(admin_engine: AsyncEngine, world: RunWorld, action: str) -> list[AuditEvent]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == world.org.org_id, AuditEvent.action == action)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def _finish_job(
    admin_engine: AsyncEngine, job_id, status: str, result: dict | None = None
) -> None:
    assert status in ("succeeded", "failed", "refused", "expired", "cancelled")
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                f"UPDATE jobs SET status = '{status}', result = CAST(:r AS jsonb),"
                " finished_at = now() WHERE id = :j"
            ),
            {"r": json.dumps(result or {}), "j": job_id},
        )
        await session.commit()


async def _finish(
    admin_engine: AsyncEngine, target: PushRunTarget, outcome: str, **extra: Any
) -> None:
    """The site server reports `outcome` for this target's install job."""
    await _finish_job(
        admin_engine, target.job_id, "succeeded",
        {"target_address": str(target.address), "outcome": outcome, **extra},
    )


async def _in_flight(admin_engine: AsyncEngine, run_id) -> list[PushRunTarget]:
    return [t for t in await _targets(admin_engine, run_id) if t.state == "in_flight"]


async def _prove_sign_in(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, world, run_id, dispatcher
) -> None:
    """One first (spec §1.4a): dispatch the first target and have it install,
    which opens the run to full concurrency."""
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    (first,) = await _in_flight(admin_engine, run_id)
    await _finish(admin_engine, first, "installed")


async def _run(admin_engine: AsyncEngine, run_id) -> PushRun:
    async with AsyncSession(admin_engine) as session:
        return (await session.execute(select(PushRun).where(PushRun.id == run_id))).scalar_one()


# --------------------------------------------------------------------------- #
# The ceiling
# --------------------------------------------------------------------------- #


def test_the_bounds_are_the_approved_numbers() -> None:
    from app.modules.agent_push.run_list import PUSH_RUN_MAX

    assert runs.PUSH_RUN_CONCURRENCY == 50
    assert PUSH_RUN_MAX == 1024
    assert preflight.PUSH_BATCH_MAX == 25


async def test_the_pacer_fills_to_fifty_and_no_further(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-50", octet=101, count=56)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _prove_sign_in(app_engine, admin_engine, world, run_id, dispatcher)

    fill = await _advance(app_engine, world, run_id, dispatcher)
    assert len(fill) == 50
    assert all(device == world.site_server_device_id for device, _env in fill)
    states = await _states(admin_engine, run_id)
    assert states.count("in_flight") == 50 and states.count("waiting") == 5
    # Position order: the next fifty confirmed are the fifty in flight.
    assert states[:51] == ["installed"] + ["in_flight"] * 50

    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert await _jobs(admin_engine, world) == 51


async def test_concurrent_advances_never_exceed_the_ceiling(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    """The row lock is the whole mechanism. Without `FOR UPDATE`, both calls
    count zero in flight and dispatch three each."""
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 3)
    world = await run_world(admin_engine, "rp-race", octet=102, count=8)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _prove_sign_in(app_engine, admin_engine, world, run_id, dispatcher)

    first, second = await asyncio.gather(
        _advance(app_engine, world, run_id, dispatcher),
        _advance(app_engine, world, run_id, dispatcher),
    )
    assert len(first) + len(second) == 3
    assert await _jobs(admin_engine, world) == 4
    assert (await _states(admin_engine, run_id)).count("in_flight") == 3


async def test_freed_capacity_is_refilled_one_for_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 2)
    world = await run_world(admin_engine, "rp-refill", octet=103, count=4)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _prove_sign_in(app_engine, admin_engine, world, run_id, dispatcher)
    # The proven sign-in freed its slot and opened the run: both slots fill.
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 2
    assert await _states(admin_engine, run_id) == ["installed", "in_flight", "in_flight", "waiting"]


# --------------------------------------------------------------------------- #
# The full ladder, before every target, with no decrypt on a refusal
# --------------------------------------------------------------------------- #


async def test_an_unbound_credential_refuses_the_rest_one_by_one_without_a_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 1)
    world = await run_world(admin_engine, "rp-unbind", octet=104, count=3)
    run_id = await start_run(app_engine, world)
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    await _advance(app_engine, world, run_id, dispatcher)
    assert vault.calls == 1

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE push_credential_scope SET unbound_at = now(), unbound_by = :u"
                 " WHERE credential_id = :c"),
            {"u": world.org.user_id, "c": world.credential_id},
        )
        await session.commit()
    first = (await _targets(admin_engine, run_id))[0]
    await _finish_job(admin_engine, first.job_id, "succeeded",
                      {"target_address": str(first.address), "outcome": "installed"})

    assert await _advance(app_engine, world, run_id, dispatcher) == []
    targets = await _targets(admin_engine, run_id)
    assert [t.state for t in targets] == ["installed", "refused", "refused"]
    assert all("credential_not_scoped" in t.reasons for t in targets[1:])
    assert vault.calls == 1  # the refusals decrypted nothing
    refused = await _audits(admin_engine, world, "push.refused")
    assert [e.target_id for e in refused] == list(world.addresses[1:])
    for event in refused:
        assert event.actor_type == "system" and event.actor_id is None
        assert event.meta["run_id"] == str(run_id)
        assert event.meta["on_behalf_of"] == str(world.org.user_id)


async def test_a_deleted_credential_refuses_every_remaining_target_without_a_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-del", octet=105, count=2)
    run_id = await start_run(app_engine, world)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("DELETE FROM push_credential_scope WHERE credential_id = :c"),
            {"c": world.credential_id},
        )
        await session.execute(
            text("DELETE FROM credentials WHERE id = :c"), {"c": world.credential_id}
        )
        await session.commit()
    assert (await _run(admin_engine, run_id)).credential_id is None

    vault = FakeVault()
    assert await _advance(app_engine, world, run_id, make_dispatcher(tmp_path, vault=vault)) == []
    targets = await _targets(admin_engine, run_id)
    assert [t.state for t in targets] == ["refused", "refused"]
    assert all("credential_missing" in t.reasons for t in targets)
    assert vault.calls == 0 and await _jobs(admin_engine, world) == 0


async def test_a_person_who_lost_the_permission_no_longer_pushes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-perm", octet=106, count=1)
    run_id = await start_run(app_engine, world)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("DELETE FROM user_roles WHERE user_id = :u"), {"u": world.org.user_id}
        )
        await session.commit()
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    assert target.state == "refused" and "run_owner_not_permitted" in target.reasons


async def test_a_machine_that_enrolled_meanwhile_is_not_pushed_to(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-enr", octet=107, count=1)
    run_id = await start_run(app_engine, world)
    await seed_agent(admin_engine, world.org.org_id, world.device_ids[0])
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    assert target.state == "refused" and target.reasons == ["already_enrolled"]


async def test_an_address_that_now_names_another_machine_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-moved", octet=108, count=1)
    run_id = await start_run(app_engine, world)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE devices SET primary_ip = CAST('10.108.9.9' AS inet) WHERE id = :d"),
            {"d": world.device_ids[0]},
        )
        await session.commit()
    await seed_asset(admin_engine, world.org.org_id, primary_ip=world.addresses[0])
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    assert target.state == "refused" and "target_changed" in target.reasons
    assert await _jobs(admin_engine, world) == 0


# --------------------------------------------------------------------------- #
# The pacer never resumes, never adds a target, and settles outcomes
# --------------------------------------------------------------------------- #


async def test_the_pacer_never_resumes_a_paused_run_but_still_settles_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 1)
    world = await run_world(admin_engine, "rp-pause", octet=109, count=2)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _advance(app_engine, world, run_id, dispatcher)
    async with org_session(world.org.org_id, engine=app_engine) as session:
        await runs.pause_run(session, world.org.org_id, run_id=run_id, actor_id=world.org.user_id)
    first = (await _targets(admin_engine, run_id))[0]
    await _finish_job(admin_engine, first.job_id, "succeeded",
                      {"target_address": str(first.address), "outcome": "installed"})

    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert (await _run(admin_engine, run_id)).state == "paused"
    assert await _states(admin_engine, run_id) == ["installed", "waiting"]


async def test_a_cancelled_run_dispatches_nothing_and_stays_cancelled(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-cxl", octet=110, count=2)
    run_id = await start_run(app_engine, world)
    async with org_session(world.org.org_id, engine=app_engine) as session:
        await runs.cancel_run(session, world.org.org_id, run_id=run_id, actor_id=world.org.user_id)
    assert await _advance(app_engine, world, run_id, make_dispatcher(tmp_path)) == []
    assert (await _run(admin_engine, run_id)).state == "cancelled"
    assert await _jobs(admin_engine, world) == 0


def test_only_create_run_constructs_a_run_target() -> None:
    """No code path adds a target to an existing run (design §1.3). The
    database trigger refuses one; this names the only constructor."""
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name == "PushRunTarget":
                        offenders.append(f"{path.relative_to(APP)}::{func.name}")
    assert offenders == ["modules/agent_push/runs.py::create_run"]
    assert not [p for p in APP.rglob("*.py") if "INSERT INTO push_run_target" in p.read_text()]


def test_only_a_persons_act_makes_a_run_running() -> None:
    """CLAUDE.md 2a(e) as amended: the pacer cannot resume. The value
    `"running"` is written to a run's state in `create_run` and `resume_run`
    and nowhere else."""
    tree = ast.parse((APP / "modules/agent_push/runs.py").read_text())
    writers: set[str] = set()
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Attribute) and t.attr == "state" for t in node.targets)
                and isinstance(node.value, ast.Constant)
                and node.value.value == "running"
            ):
                writers.add(func.name)
            if (
                isinstance(node, ast.keyword)
                and node.arg == "state"
                and isinstance(node.value, ast.Constant)
                and node.value.value == "running"
            ):
                writers.add(func.name)
    assert writers == {"create_run", "resume_run"}


async def test_the_install_result_settles_the_target_in_the_jobs_transaction(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-res", octet=111, count=2)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    engine = job_engine(tmp_path)
    for position, outcome in ((0, "installed"), (1, "auth_failed")):
        # One first: each target is dispatched only after the one before settled.
        await _advance(app_engine, world, run_id, dispatcher)
        target = (await _targets(admin_engine, run_id))[position]
        async with org_session(world.org.org_id, engine=app_engine) as session:
            job = (await session.execute(select(Job).where(Job.id == target.job_id))).scalar_one()
            await engine.complete(
                session, job, state="succeeded",
                result={"target_address": str(target.address), "outcome": outcome,
                        "detail": "Permission denied" if outcome != "installed" else ""},
            )
    first, second = await _targets(admin_engine, run_id)
    assert (first.state, first.outcome, first.signed_in) == ("installed", "installed", True)
    assert (second.state, second.outcome, second.signed_in) == ("failed", "auth_failed", False)
    assert second.detail == "Permission denied"
    assert first.settled_at is not None and second.settled_at is not None


async def test_a_job_that_expired_is_settled_by_the_next_pass(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 1)
    world = await run_world(admin_engine, "rp-exp", octet=113, count=2)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _advance(app_engine, world, run_id, dispatcher)
    first = (await _targets(admin_engine, run_id))[0]
    await _finish_job(admin_engine, first.job_id, "expired")
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    first, second = await _targets(admin_engine, run_id)
    assert (first.state, first.outcome) == ("failed", "expired")
    assert second.state == "in_flight"


async def test_a_hung_install_is_lost_after_the_grace(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-lost", octet=114, count=1)
    run_id = await start_run(app_engine, world)
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "UPDATE jobs SET status = 'running', expires_at = now() - interval '31 minutes'"
                " WHERE id = :j"
            ),
            {"j": target.job_id},
        )
        await session.commit()
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    assert (target.state, target.outcome) == ("failed", "lost")


async def test_a_run_whose_last_target_is_terminal_completes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-done", octet=115, count=1)
    run_id = await start_run(app_engine, world)
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    await _finish_job(admin_engine, target.job_id, "succeeded",
                      {"target_address": str(target.address), "outcome": "installed"})
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    run = await _run(admin_engine, run_id)
    assert run.state == "completed" and run.finished_at is not None
    (completed,) = await _audits(admin_engine, world, "push.run.completed")
    assert completed.actor_type == "system" and completed.meta["counts"]["installed"] == 1


# --------------------------------------------------------------------------- #
# The lockout guard (spec §1.4a): one first, stop on sign-in failures
# --------------------------------------------------------------------------- #


def test_the_guard_limit_is_the_approved_number() -> None:
    assert runs.PUSH_RUN_AUTH_FAILURE_LIMIT == 3


@pytest.mark.parametrize(
    ("outcome", "result", "signed_in"),
    [
        ("installed", {}, True),
        ("os_mismatch", {"arch_detected": "arm64"}, True),
        # `failed` after sign-in carries what the target said it is ...
        ("failed", {"arch_detected": "amd64", "os_detected": "linux"}, True),
        # ... and before sign-in (WinRM artefact download) it cannot.
        ("failed", {"detail": "artefact download: 404"}, False),
        # Host-key and transport failures happen before any authentication.
        ("unreachable", {}, False),
        ("auth_failed", {}, False),
        ("something_new", {"arch_detected": "amd64"}, False),
    ],
)
def test_what_counts_as_getting_past_sign_in(
    outcome: str, result: dict, signed_in: bool
) -> None:
    assert runs._signed_in(outcome, result) is signed_in


async def test_one_target_first_until_a_sign_in_succeeds(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-one", octet=116, count=5)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)

    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    # Still one in flight: nothing more, though 49 slots are "free".
    assert await _advance(app_engine, world, run_id, dispatcher) == []
    # An unreachable machine proves nothing about the credential: still one.
    (first,) = await _in_flight(admin_engine, run_id)
    await _finish(admin_engine, first, "unreachable")
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    # A sign-in that got through opens the run to its ceiling.
    (second,) = await _in_flight(admin_engine, run_id)
    await _finish(admin_engine, second, "os_mismatch", arch_detected="arm64")
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 3
    assert await _states(admin_engine, run_id) == [
        "failed", "failed", "in_flight", "in_flight", "in_flight",
    ]


async def test_three_sign_in_failures_stop_the_run_and_only_a_person_restarts_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-stop", octet=117, count=6)
    run_id = await start_run(app_engine, world)
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    for _ in range(3):
        assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
        (target,) = await _in_flight(admin_engine, run_id)
        await _finish(admin_engine, target, "auth_failed", detail="authentication failed")

    # The third failure stops the run before anything else is presented.
    assert await _advance(app_engine, world, run_id, dispatcher) == []
    run = await _run(admin_engine, run_id)
    assert run.state == "stopped_credential" and run.stopped_at is not None
    assert vault.calls == 3
    assert (await _states(admin_engine, run_id)).count("waiting") == 3
    (stopped,) = await _audits(admin_engine, world, "push.run.stopped")
    assert stopped.actor_type == "system" and stopped.actor_id is None
    assert stopped.meta["on_behalf_of"] == str(world.org.user_id)
    assert stopped.meta["reason"] == "auth_failures"
    assert stopped.meta["auth_failures"] == 3

    # The pacer never leaves `stopped_credential` on its own.
    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert (await _run(admin_engine, run_id)).state == "stopped_credential"
    assert vault.calls == 3

    # A person's Resume restarts it -- from ONE, with a fresh count.
    async with org_session(world.org.org_id, engine=app_engine) as session:
        await runs.resume_run(session, world.org.org_id, run_id=run_id, actor_id=world.org.user_id)
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    (target,) = await _in_flight(admin_engine, run_id)
    await _finish(admin_engine, target, "auth_failed")
    # One failure in the new window is not three: the run carries on, one at a time.
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    assert (await _run(admin_engine, run_id)).state == "running"


async def test_installs_in_flight_finish_after_a_stop_and_do_not_undo_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 4)
    world = await run_world(admin_engine, "rp-stop2", octet=118, count=8)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _prove_sign_in(app_engine, admin_engine, world, run_id, dispatcher)
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 4
    in_flight = await _in_flight(admin_engine, run_id)
    for target in in_flight[:3]:
        await _finish(admin_engine, target, "auth_failed")

    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert (await _run(admin_engine, run_id)).state == "stopped_credential"
    assert [t.address for t in await _in_flight(admin_engine, run_id)] == [in_flight[3].address]

    # The last install finishes, is recorded, and a success does not unstop it.
    await _finish(admin_engine, in_flight[3], "installed")
    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert await _in_flight(admin_engine, run_id) == []
    assert (await _run(admin_engine, run_id)).state == "stopped_credential"


def test_only_the_safety_stop_writes_stopped_credential() -> None:
    """CLAUDE.md 2a(e) as amended: the pacer's one act of its own is the
    safety stop. Nothing else anywhere sets `stopped_credential`."""
    writers: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                assigns = (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(t, ast.Attribute) and t.attr == "state" for t in node.targets
                    )
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == "stopped_credential"
                )
                keyword = (
                    isinstance(node, ast.keyword)
                    and node.arg == "state"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == "stopped_credential"
                )
                if assigns or keyword:
                    writers.add(f"{path.relative_to(APP)}::{func.name}")
    assert writers == {"modules/agent_push/runs.py::_stop_if_locked_out"}


async def test_a_ticked_push_is_still_bounded_by_twenty_five_while_a_run_exists(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rp-25", octet=112, count=1)
    await start_run(app_engine, world)
    with pytest.raises(PushValidationError, match="at most 25"):
        async with org_session(world.org.org_id, engine=app_engine) as session:
            await preflight.request_push_batch(
                session,
                world.org.org_id,
                actor_id=world.org.user_id,
                credential_id=world.credential_id,
                targets=[f"10.112.1.{i}" for i in range(26)],
            )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_pacer.py`
Expected: FAIL with `AttributeError: module 'app.modules.agent_push.runs' has no attribute 'advance_run'`. The "running" AST test and the concurrency bounds test already pass. `test_the_guard_limit_is_the_approved_number`, `test_what_counts_as_getting_past_sign_in` and `test_only_the_safety_stop_writes_stopped_credential` fail too, because those names do not exist yet.

- [ ] **Step 3: Write the pacer**

In `backend/app/modules/agent_push/runs.py`:

(a) Replace the import block with:

```python
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.rbac import has_permission
from app.models import Agent, Job, JobStatus
from app.modules.agent_push.credential_scope import resolve_push_credential
from app.modules.agent_push.errors import (
    PushConflictError,
    PushError,
    PushListChangedError,
    PushNotFoundError,
    PushPermissionError,
    PushValidationError,
)
from app.modules.agent_push.models import (
    PUSH_RUN_ACTIVE_STATES,
    PUSH_RUN_TARGET_STATES,
    PushRun,
    PushRunTarget,
)
from app.modules.agent_push.net import parse_target_address
from app.modules.agent_push.preflight import TargetDecision, evaluate_targets
from app.modules.agent_push.run_contract import Envelope, RunTargetDispatcher
from app.modules.agent_push.run_list import LIST_REASONS, expand_prefix
```

and extend `__all__` with `"PUSH_RUN_AUTH_FAILURE_LIMIT"`, `"RUN_REFUSAL_REASONS"`, `"RUN_TARGET_LOST_AFTER"`, `"SIGNED_IN_OUTCOMES"`, `"advance_run"`, `"auth_window"` and `"settle_target_for_job"`. `auth_window` is public because the API's run read (Task 9) shows it.

(b) Below `PUSH_RUN_CONCURRENCY = 50`, add:

```python
#: A `running` install still unresolved this long after its job's expiry is
#: lost (R15). The kernel expires only queued and dispatched jobs, and a hung
#: install must not hold one of the fifty slots for ever.
RUN_TARGET_LOST_AFTER = timedelta(minutes=30)

#: THE LOCKOUT GUARD (spec §1.4a, decided by the operator 2026-09-30). A wrong or
#: expired domain password presented to fifty machines at once trips a typical
#: Active Directory lockout policy (5-10 bad attempts) within seconds. So a run
#: sends ONE target first, opens to `PUSH_RUN_CONCURRENCY` only once a sign-in
#: has got through, and stops as `stopped_credential` after this many
#: `auth_failed` results since it last started or resumed. The safety stop is
#: the pacer's one act of its own (CLAUDE.md 2a(e) as amended); only a person's
#: Resume leaves it.
PUSH_RUN_AUTH_FAILURE_LIMIT = 3

#: THE OUTCOMES THAT PROVE THE CREDENTIAL GOT PAST SIGN-IN (R19), read off the
#: agent (`agent/internal/capabilities/pushinstall/pushinstall.go`,
#: `winrm_capability.go`):
#: * `installed` and `os_mismatch` are produced only after an authenticated
#:   session ran on the target (SSH: after `Open`; WinRM: from the install
#:   script's exit code).
#: * `failed` is produced BOTH before sign-in (WinRM: the artefact download,
#:   the script build) and after it, so it counts only when the result carries
#:   `arch_detected`, which the agent learns only from inside a signed-in
#:   session (`_signed_in`).
#: * `unreachable` never counts: it is also the host-key and transport failure,
#:   before any authentication.
#: * `auth_failed` is the failure the guard counts.
SIGNED_IN_OUTCOMES = frozenset({"installed", "os_mismatch"})


def _signed_in(outcome: str, result: dict[str, Any]) -> bool:
    if outcome in SIGNED_IN_OUTCOMES:
        return True
    return outcome == "failed" and bool(str(result.get("arch_detected") or "").strip())


#: The refusals only a run reaches, each with the remedy it implies. They are
#: shown beside the machine's own address, so they are written in plain
#: language.
RUN_REFUSAL_REASONS: dict[str, str] = {
    "run_owner_not_permitted": (
        "the person who started this run no longer holds both agent.push.run and "
        "credentials.checkout, so nothing more is pushed on their confirmation. "
        "Start a new run"
    ),
    "credential_missing": (
        "the run's credential no longer exists, or is no longer a push credential. "
        "Nothing is presented with a secret the operator has withdrawn. Start a new "
        "run with a current credential"
    ),
    "target_changed": (
        "this address now belongs to a different machine record from the one that "
        "was confirmed, or the confirmed record is gone. Confirm it in a new run"
    ),
    "already_enrolled": "the machine is running the agent now, so there is nothing to install",
    "not_installable": LIST_REASONS["not_installable"],
    "no_release": (
        "no published agent release exists for this machine's operating system and "
        "processor. Publish one under Agent packages"
    ),
    "credential_unreadable": (
        "the run's credential could not be read from the vault in the form a push "
        "needs. Open it under Credentials and save it again"
    ),
    "site_server_awaiting_review": (
        "the site server for this machine's segment is waiting for review, so no job "
        "may be sent to it. Approve it under Devices"
    ),
}
```

(c) Append at the end of the file:

```python
# --------------------------------------------------------------------------- #
# The pacer
# --------------------------------------------------------------------------- #


Settled = tuple[str, str, str, bool]


def _terminal(job: Job | None, now: datetime) -> Settled | None:
    """`(state, outcome, detail, signed_in)` for a target whose job is over,
    or `None` while it is still live. Derived from the `jobs` row alone, so
    succeeded, failed, refused, expired and cancelled are handled in one place,
    as in `agent_update/reconcile.py`. Only a `succeeded` job carries an agent
    outcome, so only it can prove a sign-in."""
    if job is None:
        return ("failed", "job_missing", "", False)
    result = job.result if isinstance(job.result, dict) else {}
    if job.status is JobStatus.succeeded:
        outcome = str(result.get("outcome") or "unknown")[:32]
        detail = str(result.get("detail") or "")[:500]
        state = "installed" if outcome == "installed" else "failed"
        return (state, outcome, detail, _signed_in(outcome, result))
    if job.status is JobStatus.failed:
        return ("failed", "job_failed", str(result.get("error") or "")[:500], False)
    if job.status is JobStatus.refused:
        return ("failed", "agent_refused", str(result.get("refused") or "")[:500], False)
    if job.status is JobStatus.expired:
        return ("failed", "expired", "", False)
    if job.status is JobStatus.cancelled:
        return ("failed", "job_cancelled", str(job.cancel_outcome or ""), False)
    if job.expires_at + RUN_TARGET_LOST_AFTER < now:
        return ("failed", "lost", "", False)
    return None


def _apply(target: PushRunTarget, settled: Settled, now: datetime) -> None:
    target.state, target.outcome, target.detail, target.signed_in = settled
    # The lockout guard's clock: a result counts in the window it ARRIVED in.
    target.settled_at = now
    target.updated_at = now


async def _reconcile(session: AsyncSession, run: PushRun, now: datetime) -> None:
    rows = (
        await session.execute(
            select(PushRunTarget, Job)
            .outerjoin(Job, Job.id == PushRunTarget.job_id)
            .where(
                PushRunTarget.org_id == run.org_id,
                PushRunTarget.run_id == run.id,
                PushRunTarget.state == "in_flight",
            )
        )
    ).all()
    for target, job in rows:
        settled = _terminal(job, now)
        if settled is not None:
            _apply(target, settled, now)
    await session.flush()


async def settle_target_for_job(session: AsyncSession, job: Job) -> None:
    """Called by `dispatch.record_install_result`, in the gateway, inside the
    job's own transaction. It records the outcome and nothing more: the gateway
    holds no vault, so the pacer's next pass fills the freed slot (R2)."""
    target = (
        await session.execute(
            select(PushRunTarget).where(
                PushRunTarget.org_id == job.org_id,
                PushRunTarget.job_id == job.id,
                PushRunTarget.state == "in_flight",
            )
        )
    ).scalar_one_or_none()
    if target is None:
        return
    now = _utcnow()
    settled = _terminal(job, now)
    if settled is not None:
        _apply(target, settled, now)
        await session.flush()


async def _count_state(session: AsyncSession, run: PushRun, *states: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(PushRunTarget)
                .where(
                    PushRunTarget.org_id == run.org_id,
                    PushRunTarget.run_id == run.id,
                    PushRunTarget.state.in_(states),
                )
            )
        ).scalar_one()
    )


async def _run_credential(session: AsyncSession, run: PushRun) -> Any | None:
    """The run's credential, metadata only, or `None` if it is gone or no
    longer a push credential. It is resolved again on every pass, never cached."""
    if run.credential_id is None:
        return None
    try:
        return await resolve_push_credential(session, run.org_id, run.credential_id)
    except PushError:
        return None


async def _owner_may_push(session: AsyncSession, user_id: UUID) -> bool:
    """The conjunction `request_push_batch` checks first (R10)."""
    return await has_permission(session, user_id, "agent.push.run") and await has_permission(
        session, user_id, "credentials.checkout"
    )


async def _is_enrolled(session: AsyncSession, org_id: UUID, device_id: UUID) -> bool:
    row = (
        await session.execute(
            select(Agent.id).where(Agent.org_id == org_id, Agent.device_id == device_id)
        )
    ).first()
    return row is not None


async def _recheck(
    session: AsyncSession,
    run: PushRun,
    target: PushRunTarget,
    *,
    credential: Any | None,
    owner_ok: bool,
    now: datetime,
) -> tuple[TargetDecision | None, list[str]]:
    """THE WHOLE LADDER, AGAINST CURRENT DATA, FOR THIS ONE TARGET (design
    §1.4 step 1). Every failing reason is collected, never just the first."""
    reasons: list[str] = []
    if not owner_ok:
        reasons.append("run_owner_not_permitted")
    if credential is None:
        reasons.append("credential_missing")
    if target.device_id is None:
        reasons.append("target_changed")
    decision: TargetDecision | None = None
    if credential is not None:
        (decision,) = await evaluate_targets(
            session,
            run.org_id,
            addresses=[parse_target_address(str(target.address))],
            credential_id=credential.id,
            now=now,
        )
        reasons.extend(r for r in decision.refusals if r not in reasons)
        if (
            decision.device_id is not None
            and target.device_id is not None
            and decision.device_id != target.device_id
            and "target_changed" not in reasons
        ):
            reasons.append("target_changed")
    if target.device_id is not None and await _is_enrolled(session, run.org_id, target.device_id):
        reasons.append("already_enrolled")
    return decision, reasons


async def _refuse_target(
    session: AsyncSession,
    run: PushRun,
    target: PushRunTarget,
    *,
    reasons: list[str],
    extras: dict[str, str],
    credential: Any | None,
    now: datetime,
) -> None:
    """`push.refused`, one per target, exactly as a ticked push writes it, plus
    the run's context. The pacer is the actor, on behalf of the run's creator (R17)."""
    target.state = "refused"
    target.reasons = list(reasons)
    target.updated_at = now
    await audit_service.record(
        session,
        org_id=run.org_id,
        actor_type="system",
        actor_id=None,
        action="push.refused",
        target_type="push_target",
        target_id=str(target.address),
        metadata={
            "target_address": str(target.address),
            "reasons": list(reasons),
            "run_id": str(run.id),
            "on_behalf_of": str(run.created_by),
            "credential_id": None if run.credential_id is None else str(run.credential_id),
            "credential_name": getattr(credential, "name", ""),
            **extras,
        },
    )


async def auth_window(session: AsyncSession, run: PushRun) -> tuple[int, bool]:
    """`(auth_failed count, a sign-in got through)` since the run last started
    or resumed. Derived from target rows (`settled_at`, `signed_in`), so the
    gateway's settle and the pacer never race on a counter."""
    row = (
        await session.execute(
            select(
                func.count().filter(PushRunTarget.outcome == "auth_failed"),
                func.count().filter(PushRunTarget.signed_in.is_(True)),
            ).where(
                PushRunTarget.org_id == run.org_id,
                PushRunTarget.run_id == run.id,
                PushRunTarget.settled_at >= run.auth_window_started_at,
            )
        )
    ).one()
    return int(row[0]), int(row[1]) > 0


async def _stop_if_locked_out(session: AsyncSession, run: PushRun, now: datetime) -> bool:
    """THE SAFETY STOP (spec §1.4a; CLAUDE.md 2a(e) as amended): the pacer's
    one act of its own. After `PUSH_RUN_AUTH_FAILURE_LIMIT` `auth_failed`
    results in this window, while machines are still waiting, the run becomes
    `stopped_credential`: nothing further is dispatched, installs in flight
    finish, and only a person's Resume restarts it. Nothing waiting means
    nothing left to protect, so the run is left to complete."""
    failures, _proven = await auth_window(session, run)
    if failures < PUSH_RUN_AUTH_FAILURE_LIMIT or not await _count_state(session, run, "waiting"):
        return False
    run.state = "stopped_credential"
    run.stopped_at = now
    run.updated_at = now
    await session.flush()
    await audit_service.record(
        session,
        org_id=run.org_id,
        actor_type="system",
        actor_id=None,
        action="push.run.stopped",
        target_type="push_run",
        target_id=str(run.id),
        metadata={
            "reason": "auth_failures",
            "auth_failures": failures,
            "limit": PUSH_RUN_AUTH_FAILURE_LIMIT,
            "window_started_at": run.auth_window_started_at.isoformat(),
            "prefix": str(run.prefix),
            "credential_id": None if run.credential_id is None else str(run.credential_id),
            "on_behalf_of": str(run.created_by),
            "counts": await run_counts(session, run.org_id, run.id),
        },
    )
    return True


async def _fill(
    session: AsyncSession, run: PushRun, dispatch: RunTargetDispatcher, now: datetime
) -> list[Envelope]:
    """ONE JOB AT A TIME, AS CAPACITY FREES (design §1.4). A refusal consumes
    no capacity, so the next waiting target is tried in its place.

    ONE FIRST (§1.4a): until a sign-in has got through in this window, the
    ceiling is 1, not `PUSH_RUN_CONCURRENCY`, so a wrong password reaches one
    machine at a time until the guard stops the run."""
    _failures, proven = await auth_window(session, run)
    ceiling = PUSH_RUN_CONCURRENCY if proven else 1
    room = ceiling - await _count_state(session, run, "in_flight")
    if room <= 0:
        return []
    waiting = list(
        (
            await session.execute(
                select(PushRunTarget)
                .where(
                    PushRunTarget.org_id == run.org_id,
                    PushRunTarget.run_id == run.id,
                    PushRunTarget.state == "waiting",
                )
                .order_by(PushRunTarget.position)
            )
        ).scalars()
    )
    if not waiting:
        return []
    credential = await _run_credential(session, run)
    owner_ok = await _owner_may_push(session, run.created_by)
    envelopes: list[Envelope] = []
    for target in waiting:
        if room <= 0:
            break
        decision, reasons = await _recheck(
            session, run, target, credential=credential, owner_ok=owner_ok, now=now
        )
        extras: dict[str, str] = {}
        if not reasons:
            assert decision is not None
            outcome = await dispatch(
                session,
                org_id=run.org_id,
                run_id=run.id,
                on_behalf_of=run.created_by,
                credential=credential,
                decision=decision,
            )
            if outcome.dispatched:
                assert outcome.envelope is not None
                target.state = "in_flight"
                target.job_id = outcome.job_id
                target.updated_at = now
                envelopes.append((UUID(str(outcome.envelope["job"]["device"])), outcome.envelope))
                room -= 1
                continue
            reasons, extras = list(outcome.refusals), dict(outcome.extras)
        await _refuse_target(
            session, run, target, reasons=reasons, extras=extras, credential=credential, now=now
        )
    await session.flush()
    return envelopes


async def _complete_if_done(session: AsyncSession, run: PushRun, now: datetime) -> None:
    if await _count_state(session, run, "waiting", "in_flight"):
        return
    run.state = "completed"
    run.finished_at = now
    run.updated_at = now
    await session.flush()
    counts = await run_counts(session, run.org_id, run.id)
    await audit_service.record(
        session,
        org_id=run.org_id,
        actor_type="system",
        actor_id=None,
        action="push.run.completed",
        target_type="push_run",
        target_id=str(run.id),
        metadata={
            "prefix": str(run.prefix),
            "counts": counts,
            "on_behalf_of": str(run.created_by),
        },
    )


async def advance_run(
    session: AsyncSession,
    org_id: UUID,
    run_id: UUID,
    *,
    dispatch: RunTargetDispatcher,
    now: datetime | None = None,
) -> list[Envelope]:
    """Continue ONE confirmed run. Returns the envelopes to publish after the
    caller's transaction commits.

    1. `SELECT ... FOR UPDATE` on the run: two concurrent callers (the beat, a
       person's Start or Resume) serialize here, so in-flight never exceeds
       `PUSH_RUN_CONCURRENCY`.
    2. Settle every in-flight target from its job row.
    3. Only while the run is `running`: stop it as `stopped_credential` if the
       lockout guard's limit is reached (§1.4a); otherwise re-run the whole
       ladder for each next waiting target, dispatch it through `dispatch`
       (one at a time until a sign-in succeeds), or refuse and record it. A
       paused, stopped or cancelled run is never resumed here.
    4. Complete the run once no target is waiting or in flight.

    It never inserts a target, never changes the credential and never writes
    `running`. Its one act of its own is the safety stop.
    """
    stamp = now or _utcnow()
    run = (
        await session.execute(
            select(PushRun)
            .where(PushRun.org_id == org_id, PushRun.id == run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        return []
    await _reconcile(session, run, stamp)
    envelopes: list[Envelope] = []
    if run.state == "running" and not await _stop_if_locked_out(session, run, stamp):
        envelopes = await _fill(session, run, dispatch, stamp)
        await _complete_if_done(session, run, stamp)
    await session.flush()
    return envelopes
```

- [ ] **Step 4: Settle a run target from the install result handler**

In `backend/app/modules/agent_push/dispatch.py`, append to the end of `record_install_result`, after its `audit_service.record(...)` call:

```python
    # A RUN'S TARGET SETTLES HERE, in the job's own transaction (design §1.4).
    # The freed slot is NOT filled here: this runs in the gateway, which holds
    # no vault (R2). The pacer's next pass fills it. Imported at call time,
    # because `runs` -> `run_list` -> `dispatch` would otherwise be a cycle.
    from app.modules.agent_push.runs import settle_target_for_job

    await settle_target_for_job(session, job)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_pacer.py tests/test_push_run_lifecycle.py tests/test_push_run_dispatcher.py tests/test_agent_push_dispatch.py tests/test_agent_push_dispatches_nothing.py`
Expected: PASS.

- [ ] **Step 6: Lint, type-check, and commit**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: no findings.

```bash
git add backend/app/modules/agent_push/runs.py backend/app/modules/agent_push/dispatch.py backend/tests/test_push_run_pacer.py
git commit -m "feat(agent-push): the run pacer -- row-locked, one first then at most 50 in flight, stop after 3 sign-in failures, full ladder re-check before every target

A refusal decrypts nothing and is audited push.refused with the run id; the
pacer never resumes and never adds a target (AST-pinned). The install result
handler settles the target in the job's transaction; the slot is refilled by
the pacer where the vault lives.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 7: The `push.run.completed` and `push.run.stopped` notification events

**Files:**
- Modify: `backend/app/core/events.py` (`EVENT_TYPES`)
- Modify: `backend/app/modules/notification/delivery.py` (a renderer and an `_EMAIL_RENDERERS` entry)
- Modify: `backend/app/modules/agent_push/runs.py` (`_complete_if_done` and `_stop_if_locked_out` publish the events)
- Modify: `backend/tests/test_notification_delivery.py` (`_EVENT_TYPE_SAMPLE_PAYLOADS`, plus one renderer test)
- Modify: `backend/tests/test_push_run_pacer.py` (assert the event)
- Modify: `frontend/src/modules/notification/types.ts`, `frontend/src/modules/notification/labels.ts`, `frontend/src/lib/noWireNames.test.ts`

**Interfaces:**
- Consumes: `app.core.events.publish`.
- Produces:
  - the event type `push.run.completed`, with payload `{run_id, prefix, target_count, installed, failed, refused, cancelled, finished_at}`;
  - the event type `push.run.stopped` (spec §1.4a), with payload `{run_id, prefix, reason, auth_failures, credential_name, target_count, installed, waiting, stopped_at}`.
  - Neither payload carries a secret, or any credential detail beyond the name that is already audited.

- [ ] **Step 1: Write the failing tests**

(a) In `backend/tests/test_notification_delivery.py`, add this entry to `_EVENT_TYPE_SAMPLE_PAYLOADS`, directly after the `"device.removal_incomplete"` entry:

```python
    "push.run.completed": {
        "run_id": "66666666-6666-6666-6666-666666666666",
        "prefix": "10.20.4.0/24",
        "target_count": 249,
        "installed": 240,
        "failed": 5,
        "refused": 4,
        "cancelled": 0,
        "finished_at": "2026-09-30T14:02:00+00:00",
    },
    "push.run.stopped": {
        "run_id": "77777777-7777-7777-7777-777777777777",
        "prefix": "10.20.4.0/24",
        "reason": "auth_failures",
        "auth_failures": 3,
        "credential_name": "Domain push account",
        "target_count": 249,
        "installed": 0,
        "waiting": 246,
        "stopped_at": "2026-09-30T14:02:00+00:00",
    },
```

and add this test at the end of the file:

```python
def test_a_finished_push_run_says_what_happened_in_numbers() -> None:
    from app.modules.notification.delivery import _render_email

    event = DomainEvent(
        org_id=uuid7(),
        event_type="push.run.completed",
        subject_type="push_run",
        subject_id=uuid7(),
        payload=_EVENT_TYPE_SAMPLE_PAYLOADS["push.run.completed"],
    )
    subject, body = _render_email(event)
    assert subject == "[Toeshee] Push install finished: 10.20.4.0/24"
    assert "Installed: 240" in body and "Failed: 5" in body
    assert "Refused before anything was sent: 4" in body


def test_a_stopped_push_run_says_why_and_what_to_do() -> None:
    from app.modules.notification.delivery import _render_email

    event = DomainEvent(
        org_id=uuid7(),
        event_type="push.run.stopped",
        subject_type="push_run",
        subject_id=uuid7(),
        payload=_EVENT_TYPE_SAMPLE_PAYLOADS["push.run.stopped"],
    )
    subject, body = _render_email(event)
    assert subject == "[Toeshee] Push install stopped: 10.20.4.0/24"
    assert "refused by 3 machines" in body and "Domain push account" in body
    assert "Machines still waiting: 246" in body and "Resume" in body
```

(b) In `backend/tests/test_push_run_pacer.py`, in `test_a_run_whose_last_target_is_terminal_completes`, add after the audit assertion:

```python
    from app.models import DomainEvent

    async with AsyncSession(admin_engine) as session:
        (event,) = (
            await session.execute(
                select(DomainEvent).where(
                    DomainEvent.org_id == world.org.org_id,
                    DomainEvent.event_type == "push.run.completed",
                )
            )
        ).scalars().all()
    assert event.subject_id == run_id and event.actor_user_id is None
    assert event.payload["installed"] == 1 and event.payload["prefix"] == world.prefix
```

and in `test_three_sign_in_failures_stop_the_run_and_only_a_person_restarts_it`, add after the `stopped.meta["auth_failures"] == 3` assertion:

```python
    from app.models import DomainEvent

    async with AsyncSession(admin_engine) as session:
        (event,) = (
            await session.execute(
                select(DomainEvent).where(
                    DomainEvent.org_id == world.org.org_id,
                    DomainEvent.event_type == "push.run.stopped",
                )
            )
        ).scalars().all()
    assert event.subject_id == run_id and event.actor_user_id is None
    assert event.payload["auth_failures"] == 3 and event.payload["waiting"] == 3
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_notification_delivery.py tests/test_push_run_pacer.py -k "wire_format or push_run or push.run or run_whose_last or sign_in_failures"`
Expected: FAIL. The sample-payload guard reports that `set(_EVENT_TYPE_SAMPLE_PAYLOADS) != EVENT_TYPES`, and the two pacer tests find no `DomainEvent`.

- [ ] **Step 3: Register, render and emit the event**

(a) In `backend/app/core/events.py`, add inside `EVENT_TYPES`, after `"device.removal_incomplete",`:

```python
        # Published by `app.modules.agent_push.runs` when a confirmed subnet
        # push's last target reaches a terminal state (design 2026-09-30
        # §1.5). Machine-origin, no ticket and no requester: routable through
        # `channel_default` rules only. The payload carries counts and the
        # prefix, never a credential.
        "push.run.completed",
        # Published by the same pacer when the lockout guard stops a run after
        # repeated sign-in failures (design 2026-09-30 §1.4a). Someone must
        # check the credential and resume, so this one needs a person.
        # Machine-origin, `channel_default` rules only; it names the credential
        # by its label and nothing more.
        "push.run.stopped",
```

(b) In `backend/app/modules/notification/delivery.py`, add above `_render_generic`:

```python
def _render_push_run_completed(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    prefix = _strip_controls(str(payload.get("prefix") or "")) or "(unknown subnet)"
    return (
        f"[Toeshee] Push install finished: {prefix}",
        "A push install run someone confirmed has finished.\n\n"
        f"Subnet: {prefix}\n"
        f"Machines in the run: {payload.get('target_count', '(unknown)')}\n"
        f"Installed: {payload.get('installed', 0)}\n"
        f"Failed: {payload.get('failed', 0)}\n"
        f"Refused before anything was sent: {payload.get('refused', 0)}\n"
        f"Finished: {payload.get('finished_at') or '(unknown)'}\n\n"
        "Open Devices, then Push runs, to see what happened on each machine.\n",
    )


def _render_push_run_stopped(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    prefix = _strip_controls(str(payload.get("prefix") or "")) or "(unknown subnet)"
    name = _strip_controls(str(payload.get("credential_name") or "")) or "(unnamed credential)"
    return (
        f"[Toeshee] Push install stopped: {prefix}",
        "A push install run was stopped to protect its credential from lockout.\n\n"
        f"The credential {name} was refused by {payload.get('auth_failures', '(unknown)')} "
        "machines, so nothing more was presented with it.\n\n"
        f"Subnet: {prefix}\n"
        f"Machines in the run: {payload.get('target_count', '(unknown)')}\n"
        f"Installed before the stop: {payload.get('installed', 0)}\n"
        f"Machines still waiting: {payload.get('waiting', 0)}\n"
        f"Stopped: {payload.get('stopped_at') or '(unknown)'}\n\n"
        "Check the account's password and whether it is locked, then open Devices, "
        "Push runs, and Resume the run. It starts again with one machine.\n",
    )
```

and add `"push.run.completed": _render_push_run_completed,` and `"push.run.stopped": _render_push_run_stopped,` as the last two entries of `_EMAIL_RENDERERS`.

(c) In `backend/app/modules/agent_push/runs.py`, add `from app.core.events import publish as publish_event` to the imports. At the end of `_complete_if_done`, after the `audit_service.record(...)` call, add:

```python
    await publish_event(
        session,
        org_id=run.org_id,
        event_type="push.run.completed",
        subject_type="push_run",
        subject_id=run.id,
        payload={
            "run_id": str(run.id),
            "prefix": str(run.prefix),
            "target_count": run.target_count,
            "installed": counts["installed"],
            "failed": counts["failed"],
            "refused": counts["refused"],
            "cancelled": counts["cancelled"],
            "finished_at": now.isoformat(),
        },
        actor_user_id=None,
    )
```

and in `_stop_if_locked_out`, directly before `return True`, add:

```python
    counts = await run_counts(session, run.org_id, run.id)
    credential = await _run_credential(session, run)
    await publish_event(
        session,
        org_id=run.org_id,
        event_type="push.run.stopped",
        subject_type="push_run",
        subject_id=run.id,
        payload={
            "run_id": str(run.id),
            "prefix": str(run.prefix),
            "reason": "auth_failures",
            "auth_failures": failures,
            "credential_name": getattr(credential, "name", ""),
            "target_count": run.target_count,
            "installed": counts["installed"],
            "waiting": counts["waiting"],
            "stopped_at": now.isoformat(),
        },
        actor_user_id=None,
    )
```

- [ ] **Step 4: Mirror the event in the console**

(a) In `frontend/src/modules/notification/types.ts`, add to the end of `EVENT_TYPES`, before `] as const;`:

```ts
  // A confirmed subnet push finished (design 2026-09-30 §1.5).
  // Machine-origin: route with "channel_default".
  "push.run.completed",
  // A run stopped after repeated sign-in failures (design 2026-09-30 §1.4a).
  // Someone must check the credential and resume it. Machine-origin.
  "push.run.stopped",
```

(b) In `frontend/src/modules/notification/labels.ts`, add to `EVENT_TYPE_LABELS`:

```ts
  "push.run.completed": "Push install run finished",
  "push.run.stopped": "Push install run stopped after sign-in failures",
```

(c) In `frontend/src/lib/noWireNames.test.ts`, add `"push.run.completed",` and `"push.run.stopped",` to `WIRE_NAMES`, directly after `"device.removal_incomplete",`.

- [ ] **Step 5: Run everything touched**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_notification_delivery.py tests/test_events_outbox.py tests/test_push_run_pacer.py`
Expected: PASS.

Run: `cd frontend && npx tsc --noEmit && npx vitest run src/lib/noWireNames.test.ts src/modules/notification`
Expected: PASS. `Record<EventType, string>` compiles only because the label was added.

- [ ] **Step 6: Lint, type-check, and commit**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: no findings.

```bash
git add backend/app/core/events.py backend/app/modules/notification/delivery.py backend/app/modules/agent_push/runs.py backend/tests/test_notification_delivery.py backend/tests/test_push_run_pacer.py frontend/src/modules/notification/types.ts frontend/src/modules/notification/labels.ts frontend/src/lib/noWireNames.test.ts
git commit -m "feat(notification): push.run.completed and push.run.stopped, emitted by the run pacer

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---
### Task 8: The 60-second beat — `push_run_tick_all_orgs`

**Files:**
- Modify: `backend/app/modules/agent_push/runs.py` (add `TICK_INTERVAL_S`, `tick` and `org_ids_with_live_runs`)
- Modify: `backend/app/worker.py` (imports, `push_run_tick_all_orgs`, the beat entry and the Celery task)
- Test: `backend/tests/test_push_run_tick.py` (create)

**Interfaces:**
- Consumes: `runs.advance_run` and `dispatch.RunDispatcher`, plus the worker's `_OrgCycleFailures`, `get_vault`, `get_signer`, `ObjectStore` and `publish_job`.
- Produces:
  - `runs.TICK_INTERVAL_S = 60.0`.
  - `runs.tick(session, org_id, *, dispatch, now=None) -> list[Envelope]`.
  - `runs.org_ids_with_live_runs(admin_engine) -> list[UUID]`.
  - `worker.push_run_tick_all_orgs(now: datetime | None = None) -> int`.
  - The beat entry `"push-run-tick"`, which runs the Celery task `app.worker.push_run_tick`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_push_run_tick.py`:

```python
"""The push-run beat: the backstop that continues confirmed runs every 60 s
(design §1.4). It never starts, resumes or widens a run. It fills capacity
freed by jobs that ended without reaching the gateway's result handler, and
capacity the gateway cannot fill because it holds no vault.

The shared test database also holds other tests' orgs, so every assertion is
filtered to this test's own site server device.
"""

from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings, override_settings
from app.core.tenancy import org_session
from tests.conftest import PgCluster
from tests.push_run_fixtures import CONSOLE, FakeStore, FakeVault, run_world, start_run


@pytest.fixture(autouse=True)
def _register_push_job_types() -> None:
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


def _settings(pg_cluster: PgCluster, tmp_path: Path) -> Settings:
    return Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
        public_console_url=CONSOLE,
    )


@pytest.fixture()
def published(monkeypatch) -> list[tuple[Any, dict[str, Any]]]:
    from app import worker

    sent: list[tuple[Any, dict[str, Any]]] = []

    async def _publish(redis: Any, device_id: Any, envelope: dict[str, Any]) -> None:
        sent.append((device_id, envelope))

    monkeypatch.setattr(worker, "publish_job", _publish)
    monkeypatch.setattr(worker, "get_vault", lambda settings: FakeVault())
    monkeypatch.setattr(worker, "ObjectStore", lambda settings: FakeStore())
    return sent


async def test_the_beat_continues_a_running_run(
    admin_engine, app_engine, pg_cluster: PgCluster, tmp_path: Path, published
) -> None:
    from app import worker

    world = await run_world(admin_engine, "rt-go", octet=121, count=2)
    await start_run(app_engine, world)
    with override_settings(_settings(pg_cluster, tmp_path)):
        total = await worker.push_run_tick_all_orgs()
    mine = [e for device, e in published if device == world.site_server_device_id]
    # One first (spec §1.4a): the beat, like every caller, sends ONE target
    # until a sign-in has got through.
    assert len(mine) == 1 and total >= 1
    assert mine[0]["job"]["params"]["target_address"] == world.addresses[0]


async def test_a_cancelled_run_is_not_continued_by_the_beat(
    admin_engine, app_engine, pg_cluster: PgCluster, tmp_path: Path, published
) -> None:
    from app import worker
    from app.modules.agent_push import runs

    world = await run_world(admin_engine, "rt-cxl", octet=122, count=2)
    run_id = await start_run(app_engine, world)
    async with org_session(world.org.org_id, engine=app_engine) as session:
        await runs.cancel_run(session, world.org.org_id, run_id=run_id, actor_id=world.org.user_id)
    with override_settings(_settings(pg_cluster, tmp_path)):
        await worker.push_run_tick_all_orgs()
    assert [d for d, _e in published if d == world.site_server_device_id] == []


async def test_one_failing_org_does_not_stop_the_others(
    admin_engine, app_engine, pg_cluster: PgCluster, tmp_path: Path, published, monkeypatch
) -> None:
    from app import worker
    from app.modules.agent_push import runs

    broken = await run_world(admin_engine, "rt-bad", octet=123, count=1)
    healthy = await run_world(admin_engine, "rt-good", octet=124, count=1)
    await start_run(app_engine, broken)
    await start_run(app_engine, healthy)
    real_tick = runs.tick

    async def _flaky(session: Any, org_id: Any, **kw: Any) -> Any:
        if org_id == broken.org.org_id:
            raise RuntimeError("this org's pass fails")
        return await real_tick(session, org_id, **kw)

    monkeypatch.setattr(runs, "tick", _flaky)
    with override_settings(_settings(pg_cluster, tmp_path)):
        await worker.push_run_tick_all_orgs()
    assert [d for d, _e in published if d == healthy.site_server_device_id] == [
        healthy.site_server_device_id
    ]
    assert [d for d, _e in published if d == broken.site_server_device_id] == []


def test_the_beat_entry_runs_every_minute(pg_cluster: PgCluster, tmp_path: Path) -> None:
    from app import worker
    from app.modules.agent_push import runs

    with override_settings(_settings(pg_cluster, tmp_path)):
        entry = worker.create_celery().conf.beat_schedule["push-run-tick"]
    assert entry["task"] == "app.worker.push_run_tick"
    assert entry["schedule"] == runs.TICK_INTERVAL_S == 60.0
    assert entry["options"]["expires"] == 60.0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_tick.py`
Expected: FAIL with `AttributeError: module 'app.worker' has no attribute 'push_run_tick_all_orgs'`, and `KeyError: 'push-run-tick'`.

- [ ] **Step 3: Add `tick` and `org_ids_with_live_runs` to `runs.py`**

In `backend/app/modules/agent_push/runs.py`:
- change `from sqlalchemy import func, select, text, update` to `from sqlalchemy import exists, func, select, text, update`;
- change `from sqlalchemy.ext.asyncio import AsyncSession` to `from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession`;
- add `"TICK_INTERVAL_S"`, `"org_ids_with_live_runs"` and `"tick"` to `__all__`;
- below `RUN_TARGET_LOST_AFTER`, add:

```python
#: The beat's cadence (design §1.4): the backstop that fills freed capacity
#: and settles jobs that ended without a result. Read by `app/worker.py`.
TICK_INTERVAL_S = 60.0
```

and append at the end of the file:

```python
def _live_condition() -> Any:
    in_flight = exists().where(
        PushRunTarget.org_id == PushRun.org_id,
        PushRunTarget.run_id == PushRun.id,
        PushRunTarget.state == "in_flight",
    )
    return PushRun.state.in_(PUSH_RUN_ACTIVE_STATES) | in_flight


async def tick(
    session: AsyncSession,
    org_id: UUID,
    *,
    dispatch: RunTargetDispatcher,
    now: datetime | None = None,
) -> list[Envelope]:
    """One pass over this org's live runs: running, paused or
    `stopped_credential` (never resumed here, only settled), or cancelled but
    with installs still in flight to settle."""
    stamp = now or _utcnow()
    run_ids = list(
        (
            await session.execute(
                select(PushRun.id)
                .where(PushRun.org_id == org_id, _live_condition())
                .order_by(PushRun.created_at)
            )
        ).scalars()
    )
    envelopes: list[Envelope] = []
    for run_id in run_ids:
        envelopes.extend(await advance_run(session, org_id, run_id, dispatch=dispatch, now=stamp))
    return envelopes


async def org_ids_with_live_runs(admin_engine: AsyncEngine) -> list[UUID]:
    """Read on the ADMIN engine, exactly as `agent_update.reconcile._org_ids`
    is: the beat must find the orgs before it can open a tenant session for
    any. It returns ids only, and returns at once when no run is live."""
    async with AsyncSession(admin_engine) as session:
        rows = await session.execute(select(PushRun.org_id).where(_live_condition()).distinct())
        return list(rows.scalars())
```

- [ ] **Step 4: Wire the worker**

In `backend/app/worker.py`:

(a) Add these beside the other module imports near the top, for example after `from app.modules.agent_update import reconcile as agent_update_reconcile`:

```python
from app.modules.agent_push import dispatch as push_dispatch_module
from app.modules.agent_push import runs as push_runs_module
```

(b) Add this directly after `agent_update_tick_all_orgs`:

```python
async def push_run_tick_all_orgs(now: datetime | None = None) -> int:
    """The push-run pacer's backstop, every `TICK_INTERVAL_S` (design
    2026-09-30 §1.4).

    It CONTINUES runs a person confirmed and never starts, resumes or widens one
    (CLAUDE.md 2a(e) as amended 2026-09-30). It settles in-flight targets whose
    jobs ended without reaching the gateway's result handler, and fills freed
    capacity -- which the gateway cannot do, because it holds no vault. The
    vault, signer and object store are fleet-wide preconditions, resolved
    before the loop. One org per transaction, and envelopes are published only
    after that org commits. One org's failure is contained; see
    `_OrgCycleFailures`.
    """
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    job_engine = JobEngine(signer=get_signer(settings), default_ttl_s=settings.job_default_ttl_s)
    vault = get_vault(settings)
    stores: list[ObjectStore] = []

    def object_store() -> ObjectStore:
        if not stores:
            stores.append(ObjectStore(settings))
        return stores[0]

    dispatcher = push_dispatch_module.RunDispatcher(
        job_engine, object_store, vault, public_console_url=settings.public_console_url
    )
    failures = _OrgCycleFailures("push.run_tick")
    total = 0
    try:
        org_ids = await push_runs_module.org_ids_with_live_runs(admin_engine)
        for org_id in org_ids:
            try:
                async with org_session(org_id, engine=app_engine) as session:
                    envelopes = await push_runs_module.tick(
                        session, org_id, dispatch=dispatcher.dispatch_target, now=now
                    )
                # Transaction committed: safe to hand the signed jobs to agents.
                for device_id, envelope in envelopes:
                    await publish_job(redis, device_id, envelope)
                total += len(envelopes)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
    return total
```

(c) In `create_celery`'s `beat_schedule`, add this directly after the `"agent-update-tick"` entry:

```python
        # Push runs (design 2026-09-30 §1.4): the pacer's backstop. Returns at
        # once unless some org has a run running, paused, or with installs in
        # flight.
        "push-run-tick": {
            "task": "app.worker.push_run_tick",
            "schedule": push_runs_module.TICK_INTERVAL_S,
        },
```

(d) Inside the `try:` block that defines the Celery tasks, add this after the `agent_update_tick` task:

```python
    @celery_app.task(name="app.worker.push_run_tick")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def push_run_tick() -> dict[str, int]:
        return {"dispatched": asyncio.run(push_run_tick_all_orgs())}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_tick.py tests/test_worker_beat_expiry.py tests/test_worker_sweep_containment.py tests/test_push_run_pacer.py`
Expected: PASS. `test_every_beat_entry_expires_after_one_interval` covers the new entry through `_with_expiry`.

- [ ] **Step 6: Lint, type-check, and commit**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: no findings.

```bash
git add backend/app/modules/agent_push/runs.py backend/app/worker.py backend/tests/test_push_run_tick.py
git commit -m "feat(worker): push-run-tick every 60 s -- the pacer's backstop, per-org contained, publish after commit

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 9: The API — nine routes under `/agent-push/runs`

**Files:**
- Modify: `backend/app/modules/agent_push/schema.py` (the run models)
- Modify: `backend/app/modules/agent_push/api.py` (the routes, `_run_dispatcher`, `_http_error`)
- Modify: `backend/tests/test_agent_push_api.py` (`EXPECTED_ROUTES`, `EXPECTED_ROUTE_COUNT`, and the run-code set)
- Test: `backend/tests/test_push_run_api.py` (create)

**Interfaces:**
- Consumes: `run_list.authorized_prefixes`, `run_list.expand_prefix`, `run_list.PUSH_RUN_MAX`, `run_list.LIST_REASONS`, `runs.*` and `dispatch.RunDispatcher`.
- Produces the routes below. Every route requires `agent.push.run`.
  - `GET /api/v1/agent-push/runs/prefixes` → `list[RunPrefixOut]`
  - `GET /api/v1/agent-push/runs/preview?prefix=&credential_id=` → `RunPreviewOut`. Also requires `credentials.checkout`.
  - `POST /api/v1/agent-push/runs` with `RunCreate {prefix, credential_id, list_digest}` → 201 `RunOut`. Also requires `credentials.checkout`. It returns 409 `{"detail": {"code": "list_changed", "message": ...}}` when the list changed, and 409 with a string detail when a run is already active.
  - `GET /api/v1/agent-push/runs` → `list[RunOut]`
  - `GET /api/v1/agent-push/runs/{run_id}` → `RunOut`
  - `GET /api/v1/agent-push/runs/{run_id}/targets?offset=&limit=` → `RunTargetPageOut`
  - `POST /api/v1/agent-push/runs/{run_id}/pause` → `RunOut`
  - `POST /api/v1/agent-push/runs/{run_id}/resume` → `RunOut`. Also requires `credentials.checkout`.
  - `POST /api/v1/agent-push/runs/{run_id}/cancel` → `RunOut`

- [ ] **Step 1: Write the failing tests**

(a) In `backend/tests/test_agent_push_api.py`, replace `EXPECTED_ROUTES` and `EXPECTED_ROUTE_COUNT` with:

```python
EXPECTED_ROUTES: dict[tuple[str, str], str] = {
    ("GET", f"{PUSH}/authorizations"): SCOPE,
    ("POST", f"{PUSH}/authorizations"): SCOPE,
    ("DELETE", f"{PUSH}/authorizations/{{authorization_id}}"): SCOPE,
    ("GET", f"{PUSH}/credential-scopes"): SCOPE,
    ("POST", f"{PUSH}/credential-scopes"): SCOPE,
    ("DELETE", f"{PUSH}/credential-scopes/{{scope_id}}"): SCOPE,
    ("POST", f"{PUSH}/requests"): RUN,
    # Cycle B2: the host-key crux. Probing/confirming/pushing is running a push
    # (§4.4), so these are gated on `agent.push.run`, not `scope.manage`.
    ("GET", f"{PUSH}/host-keys"): RUN,
    ("POST", f"{PUSH}/host-keys/confirm"): RUN,
    # Push runs (design 2026-09-30 §1.7): "the existing push codes. Nothing new
    # is needed, because confirming a run *is* a push."
    ("GET", f"{PUSH}/runs/prefixes"): RUN,
    ("GET", f"{PUSH}/runs/preview"): RUN,
    ("POST", f"{PUSH}/runs"): RUN,
    ("GET", f"{PUSH}/runs"): RUN,
    ("GET", f"{PUSH}/runs/{{run_id}}"): RUN,
    ("GET", f"{PUSH}/runs/{{run_id}}/targets"): RUN,
    ("POST", f"{PUSH}/runs/{{run_id}}/pause"): RUN,
    ("POST", f"{PUSH}/runs/{{run_id}}/resume"): RUN,
    ("POST", f"{PUSH}/runs/{{run_id}}/cancel"): RUN,
}
EXPECTED_ROUTE_COUNT = 18
```

and in `test_the_run_code_gates_the_push_and_the_host_key_crux_and_nothing_else`, replace the asserted set with:

```python
    assert running == {
        ("POST", f"{PUSH}/requests"),
        ("GET", f"{PUSH}/host-keys"),
        ("POST", f"{PUSH}/host-keys/confirm"),
        ("GET", f"{PUSH}/runs/prefixes"),
        ("GET", f"{PUSH}/runs/preview"),
        ("POST", f"{PUSH}/runs"),
        ("GET", f"{PUSH}/runs"),
        ("GET", f"{PUSH}/runs/{{run_id}}"),
        ("GET", f"{PUSH}/runs/{{run_id}}/targets"),
        ("POST", f"{PUSH}/runs/{{run_id}}/pause"),
        ("POST", f"{PUSH}/runs/{{run_id}}/resume"),
        ("POST", f"{PUSH}/runs/{{run_id}}/cancel"),
    }
```

(b) Create `backend/tests/test_push_run_api.py`:

```python
"""Push runs through HTTP (design §1.1, §1.3, §1.5, §1.7; CLAUDE.md
invariants 4, 5, 6; 2a(e) as amended 2026-09-30).

Envelopes are published AFTER the transaction commits. The vault and the
object store are doubles, so no test needs OpenBao or S3.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Permission, Role, RolePermission, User, UserRole
from app.modules.agent_push.models import PushRun
from tests.push_run_fixtures import CONSOLE, FakeStore, FakeVault, run_world

PUSH = "/api/v1/agent-push"


@pytest.fixture(autouse=True)
def _register_push_job_types() -> None:
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


@pytest.fixture()
def published(api_env, monkeypatch) -> list[tuple[UUID, dict[str, Any]]]:
    from app.modules.agent_push import api as push_api

    sent: list[tuple[UUID, dict[str, Any]]] = []

    async def _publish(redis: Any, device_id: UUID, envelope: dict[str, Any]) -> None:
        sent.append((device_id, envelope))

    monkeypatch.setattr(push_api, "publish_job", _publish)
    monkeypatch.setattr(push_api, "_object_store", lambda request: FakeStore())
    monkeypatch.setattr(api_env.app.state, "vault", FakeVault())
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", CONSOLE)
    return sent


async def _person(api_env, org_id: UUID, codes: list[str] | None = None) -> tuple[dict, UUID]:
    """A user holding `org-admin`, or exactly `codes`; returns (headers, user id)."""
    if codes is None:
        sub = await api_env.create_user(org_id, "org-admin")
    else:
        sub = f"sub-{uuid4().hex[:10]}"
        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
            role = Role(org_id=org_id, name=f"r-{uuid4().hex[:6]}")
            session.add(role)
            await session.flush()
            for code in codes:
                permission = (
                    await session.execute(select(Permission).where(Permission.code == code))
                ).scalar_one()
                session.add(RolePermission(role_id=role.id, permission_id=permission.id))
            user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
            session.add(user)
            await session.flush()
            session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
            await session.commit()
    async with AsyncSession(api_env.admin_engine) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return await api_env.headers(org_id, sub), user_id


async def _preview(api_env, headers, world) -> dict[str, Any]:
    response = await api_env.client.get(
        f"{PUSH}/runs/preview",
        params={"prefix": world.prefix, "credential_id": str(world.credential_id)},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _start(api_env, headers, world, digest: str):
    return await api_env.client.post(
        f"{PUSH}/runs",
        json={
            "prefix": world.prefix,
            "credential_id": str(world.credential_id),
            "list_digest": digest,
        },
        headers=headers,
    )


async def test_pick_preview_confirm_and_the_first_jobs_are_published_after_commit(
    api_env, published
) -> None:
    world = await run_world(api_env, "ra-go", octet=131, count=2)
    headers, user_id = await _person(api_env, world.org.org_id)

    prefixes = (await api_env.client.get(f"{PUSH}/runs/prefixes", headers=headers)).json()
    assert [p["prefix"] for p in prefixes] == [world.prefix]
    assert prefixes[0]["address_count"] == 256

    preview = await _preview(api_env, headers, world)
    assert preview["ready_count"] == 2 and preview["blocked_count"] == 0
    assert [r["address"] for r in preview["rows"]] == list(world.addresses)
    assert preview["run_max"] == 1024 and preview["concurrency"] == 50

    response = await _start(api_env, headers, world, preview["list_digest"])
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["state"] == "running" and run["target_count"] == 2
    # One first (spec §1.4a): one target until a sign-in has got through.
    assert run["counts"]["in_flight"] == 1 and run["counts"]["waiting"] == 1
    assert run["signed_in"] is False and run["auth_failure_limit"] == 3
    assert [device for device, _e in published] == [world.site_server_device_id]

    async with AsyncSession(api_env.admin_engine) as session:
        (started,) = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == world.org.org_id,
                    AuditEvent.action == "push.run.started",
                )
            )
        ).scalars().all()
    assert started.actor_id == user_id
    assert "hunter2" not in response.text and "enroll_token" not in response.text


async def test_a_changed_list_is_a_409_the_console_can_recognise(api_env, published) -> None:
    world = await run_world(api_env, "ra-chg", octet=132, count=1)
    headers, _user = await _person(api_env, world.org.org_id)
    response = await _start(api_env, headers, world, "0" * 64)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "list_changed"
    assert published == []
    async with AsyncSession(api_env.admin_engine) as session:
        count = (
            await session.execute(
                select(func.count()).select_from(PushRun).where(PushRun.org_id == world.org.org_id)
            )
        ).scalar_one()
    assert count == 0


async def test_a_second_active_run_is_a_409(api_env, published) -> None:
    world = await run_world(api_env, "ra-two", octet=133, count=1)
    headers, _user = await _person(api_env, world.org.org_id)
    digest = (await _preview(api_env, headers, world))["list_digest"]
    assert (await _start(api_env, headers, world, digest)).status_code == 201
    second = await _start(api_env, headers, world, digest)
    assert second.status_code == 409 and "one active run" in second.json()["detail"]


async def test_preview_start_and_resume_need_credentials_checkout_but_pause_does_not(
    api_env, published
) -> None:
    world = await run_world(api_env, "ra-conj", octet=134, count=1)
    admin, _user = await _person(api_env, world.org.org_id)
    runner, _runner_id = await _person(api_env, world.org.org_id, ["agent.push.run"])

    preview = await api_env.client.get(
        f"{PUSH}/runs/preview",
        params={"prefix": world.prefix, "credential_id": str(world.credential_id)},
        headers=runner,
    )
    assert preview.status_code == 403 and "credentials.checkout" in preview.json()["detail"]
    assert (await _start(api_env, runner, world, "a" * 64)).status_code == 403

    digest = (await _preview(api_env, admin, world))["list_digest"]
    run_id = (await _start(api_env, admin, world, digest)).json()["id"]
    # Stopping is always safe: the run code alone may pause.
    paused = await api_env.client.post(f"{PUSH}/runs/{run_id}/pause", headers=runner)
    assert paused.status_code == 200
    resumed = await api_env.client.post(f"{PUSH}/runs/{run_id}/resume", headers=runner)
    assert resumed.status_code == 403


async def test_pause_resume_cancel_through_http_are_audited_as_the_person(
    api_env, published
) -> None:
    world = await run_world(api_env, "ra-ctl", octet=135, count=1)
    headers, user_id = await _person(api_env, world.org.org_id)
    digest = (await _preview(api_env, headers, world))["list_digest"]
    run_id = (await _start(api_env, headers, world, digest)).json()["id"]

    for verb, state in (("pause", "paused"), ("resume", "running"), ("cancel", "cancelled")):
        response = await api_env.client.post(f"{PUSH}/runs/{run_id}/{verb}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == state
    again = await api_env.client.post(f"{PUSH}/runs/{run_id}/resume", headers=headers)
    assert again.status_code == 409

    async with AsyncSession(api_env.admin_engine) as session:
        actions = [
            (e.action, e.actor_id)
            for e in (
                await session.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.org_id == world.org.org_id,
                        AuditEvent.action.like("push.run.%"),
                    )
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        ]
    assert actions == [
        ("push.run.started", user_id),
        ("push.run.paused", user_id),
        ("push.run.resumed", user_id),
        ("push.run.cancelled", user_id),
    ]


async def test_a_run_stopped_after_sign_in_failures_is_shown_and_a_person_resumes_it(
    api_env, published
) -> None:
    world = await run_world(api_env, "ra-stop", octet=140, count=2)
    headers, user_id = await _person(api_env, world.org.org_id)
    digest = (await _preview(api_env, headers, world))["list_digest"]
    run_id = (await _start(api_env, headers, world, digest)).json()["id"]
    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(
            text(
                "UPDATE push_run SET state = 'stopped_credential', stopped_at = now()"
                " WHERE id = :r"
            ),
            {"r": UUID(run_id)},
        )
        await session.commit()

    shown = (await api_env.client.get(f"{PUSH}/runs/{run_id}", headers=headers)).json()
    assert shown["state"] == "stopped_credential" and shown["stopped_at"]
    assert shown["auth_failure_limit"] == 3 and shown["signed_in"] is False
    # Stopped is not paused: Pause is refused, Resume is a person's way out.
    paused = await api_env.client.post(f"{PUSH}/runs/{run_id}/pause", headers=headers)
    assert paused.status_code == 409
    resumed = await api_env.client.post(f"{PUSH}/runs/{run_id}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "running" and resumed.json()["stopped_at"] is None


async def test_the_run_page_reads_counts_and_a_page_of_targets(api_env, published) -> None:
    world = await run_world(api_env, "ra-read", octet=136, count=3)
    headers, _user = await _person(api_env, world.org.org_id)
    digest = (await _preview(api_env, headers, world))["list_digest"]
    run_id = (await _start(api_env, headers, world, digest)).json()["id"]

    listed = (await api_env.client.get(f"{PUSH}/runs", headers=headers)).json()
    assert [r["id"] for r in listed] == [run_id]
    page = (
        await api_env.client.get(
            f"{PUSH}/runs/{run_id}/targets", params={"offset": 1, "limit": 1}, headers=headers
        )
    ).json()
    assert page["total"] == 3 and [t["position"] for t in page["items"]] == [1]
    # One first (spec §1.4a): position 0 is in flight, position 1 still waits.
    assert page["items"][0]["state"] == "waiting" and page["items"][0]["job_id"] is None
    assert "credential_missing" in page["reason_help"]


async def test_another_orgs_run_is_a_404_on_every_run_route(api_env, published) -> None:
    owner = await run_world(api_env, "ra-own", octet=137, count=1)
    other = await run_world(api_env, "ra-oth", octet=138, count=1)
    owner_headers, _u = await _person(api_env, owner.org.org_id)
    other_headers, _v = await _person(api_env, other.org.org_id)
    digest = (await _preview(api_env, owner_headers, owner))["list_digest"]
    run_id = (await _start(api_env, owner_headers, owner, digest)).json()["id"]

    for method, path in (
        ("GET", f"{PUSH}/runs/{run_id}"),
        ("GET", f"{PUSH}/runs/{run_id}/targets"),
        ("POST", f"{PUSH}/runs/{run_id}/pause"),
        ("POST", f"{PUSH}/runs/{run_id}/resume"),
        ("POST", f"{PUSH}/runs/{run_id}/cancel"),
    ):
        response = await api_env.client.request(method, path, headers=other_headers)
        assert response.status_code == 404, (method, path, response.text)


async def test_a_scope_manager_alone_may_not_touch_a_run(api_env, published) -> None:
    world = await run_world(api_env, "ra-scope", octet=139, count=1)
    headers, _user = await _person(api_env, world.org.org_id, ["agent.push.scope.manage"])
    for method, path in (
        ("GET", f"{PUSH}/runs/prefixes"),
        ("GET", f"{PUSH}/runs"),
        ("POST", f"{PUSH}/runs"),
        ("POST", f"{PUSH}/runs/{uuid4()}/pause"),
    ):
        response = await api_env.client.request(method, path, headers=headers, json={})
        assert response.status_code == 403, (method, path)


def test_no_ai_tool_reaches_a_push_route() -> None:
    """CLAUDE.md 2a(e): never reachable by an AI-driven actor. The AI read
    tools are an explicit registry; none may point under /agent-push."""
    from app.core.ai.tools.registry import REGISTRY

    assert not [t.name for t in REGISTRY.values() if t.path.startswith(PUSH)]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_api.py tests/test_agent_push_api.py`
Expected: FAIL. The route-table tests fail on the nine missing routes, and the run tests get 404s.

- [ ] **Step 3: Add the schema models**

In `backend/app/modules/agent_push/schema.py`, add these names to `__all__`: `"RunCountsOut"`, `"RunCreate"`, `"RunListRowOut"`, `"RunOut"`, `"RunPrefixOut"`, `"RunPreviewOut"`, `"RunTargetOut"`, `"RunTargetPageOut"`. Then append:

```python
# --------------------------------------------------------------------------- #
# Push runs (design 2026-09-30 §1.3, §1.7). Still no secret, token, vault
# reference or presigned URL in any field.
# --------------------------------------------------------------------------- #


class RunPrefixOut(_Strict):
    """A subnet a run may be built from: an ACTIVE push authorization."""

    prefix: str = Field(description="The authorized prefix, exactly as authorized.")
    address_count: int = Field(description="How many addresses it reaches.")
    expires_at: datetime | None = Field(description="When the authorization lapses, if ever.")


class RunListRowOut(_Strict):
    """One listed machine. `ready` false means shown and never installed."""

    device_id: UUID = Field(description="The vouched-for device record.")
    address: str = Field(description="Its address.")
    hostname: str = Field(description="Its name.")
    os_family: str = Field(description="Its declared operating system.")
    ready: bool = Field(description="True when every bound holds.")
    reasons: list[str] = Field(description="Every failing bound, named. Empty when ready.")


class RunPreviewOut(_Strict):
    """The list a person is asked to confirm, and the digest that binds it."""

    prefix: str = Field(description="The subnet that built the list.")
    credential_id: UUID = Field(description="The credential the run would present.")
    credential_name: str = Field(description="Its label.")
    list_digest: str = Field(description="SHA-256 of the ready (device_id, address) pairs.")
    ready_count: int = Field(description="Machines that would be installed.")
    blocked_count: int = Field(description="Machines listed and not ready.")
    unclaimed_count: int = Field(description="Sweep-only rows in the subnet, not listed.")
    run_max: int = Field(description="The most ready machines one run may hold.")
    concurrency: int = Field(description="The most installs in flight at once.")
    rows: list[RunListRowOut] = Field(description="Every listed machine, in address order.")
    reason_help: dict[str, str] = Field(description="Each reason's remedy, in plain words.")


class RunCreate(_Strict):
    """Confirm a list. There is no target field: the list is re-expanded
    server-side and must hash to `list_digest`, or nothing is started."""

    prefix: str = Field(min_length=1, max_length=64, description="The authorized subnet.")
    credential_id: UUID = Field(description="The push credential to present.")
    list_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="The `list_digest` of the preview confirmed."
    )


class RunCountsOut(_Strict):
    waiting: int
    in_flight: int
    installed: int
    failed: int
    refused: int
    cancelled: int


class RunOut(_Strict):
    """One push run."""

    id: UUID
    prefix: str
    credential_id: UUID | None = Field(description="Null once the credential is gone.")
    credential_name: str
    created_by: UUID = Field(description="The person who confirmed the list.")
    created_at: datetime
    state: str = Field(
        description="running, paused, stopped_credential, cancelled or completed."
    )
    target_count: int = Field(description="Machines confirmed. Fixed at creation.")
    concurrency: int = Field(description="The most installs in flight at once.")
    list_digest: str
    finished_at: datetime | None
    counts: RunCountsOut
    stopped_at: datetime | None = Field(
        description="When the lockout guard stopped the run (spec §1.4a); null otherwise."
    )
    auth_failures: int = Field(
        description="Sign-in failures since the run last started or resumed."
    )
    auth_failure_limit: int = Field(description="How many stop the run.")
    signed_in: bool = Field(
        description="A sign-in has got through since the run last started or resumed. "
        "False means one machine at a time."
    )


class RunTargetOut(_Strict):
    position: int
    device_id: UUID | None
    address: str
    state: str = Field(
        description="waiting, in_flight, installed, failed, refused or cancelled."
    )
    job_id: UUID | None
    outcome: str = Field(description="What the site server reported, once dispatched.")
    detail: str
    reasons: list[str] = Field(description="Named refusals, when refused.")
    updated_at: datetime


class RunTargetPageOut(_Strict):
    total: int
    items: list[RunTargetOut]
    reason_help: dict[str, str]
```

- [ ] **Step 4: Add the routes**

In `backend/app/modules/agent_push/api.py`:

(a) Add these imports:

```python
from app.modules.agent_push import run_list as run_list_service
from app.modules.agent_push import runs as runs_service
from app.modules.agent_push.errors import PushListChangedError
from app.modules.agent_push.models import PushRun, PushRunTarget
from app.modules.agent_push.run_contract import Envelope
```

and add these names to the existing `schema` import: `RunCountsOut`, `RunCreate`, `RunListRowOut`, `RunOut`, `RunPrefixOut`, `RunPreviewOut`, `RunTargetOut`, `RunTargetPageOut`.

(b) In `_http_error`, add this directly before `if isinstance(exc, PushConflictError):`:

```python
    if isinstance(exc, PushListChangedError):
        # A STRUCTURED 409, so the console tells "the list changed, show it
        # again" from "a run is already active" without parsing prose.
        return HTTPException(
            status.HTTP_409_CONFLICT, {"code": "list_changed", "message": str(exc)}
        )
```

(c) Append at the end of the file:

```python
# --------------------------------------------------------------------------- #
# Push runs -- a subnet builds the list; a person confirms it; the pacer
# continues it (design 2026-09-30; CLAUDE.md 2a(e), 2a(f) as amended).
#
# DECLARATION ORDER MATTERS: `/runs/prefixes` and `/runs/preview` come before
# `/runs/{run_id}`, or "prefixes" would be parsed as a run id.
# --------------------------------------------------------------------------- #


def _run_dispatcher(request: Request) -> dispatch_service.RunDispatcher:
    return dispatch_service.RunDispatcher(
        request.app.state.job_engine,
        lambda: _object_store(request),
        request.app.state.vault,
        public_console_url=request.app.state.settings.public_console_url,
    )


def _run_reason_help() -> dict[str, str]:
    return {
        **preflight_service.REFUSAL_REASONS,
        **dispatch_service.DISPATCH_REFUSAL_REASONS,
        **run_list_service.LIST_REASONS,
        **runs_service.RUN_REFUSAL_REASONS,
    }


async def _run_out(session: AsyncSession, org_id: UUID, run: PushRun) -> RunOut:
    counts = await runs_service.run_counts(session, org_id, run.id)
    auth_failures, signed_in = await runs_service.auth_window(session, run)
    names = (
        await _credential_names(session, org_id, {run.credential_id})
        if run.credential_id is not None
        else {}
    )
    return RunOut(
        id=run.id,
        prefix=str(run.prefix),
        credential_id=run.credential_id,
        credential_name=names.get(run.credential_id, "") if run.credential_id else "",
        created_by=run.created_by,
        created_at=run.created_at,
        state=run.state,
        target_count=run.target_count,
        concurrency=runs_service.PUSH_RUN_CONCURRENCY,
        list_digest=run.list_digest,
        finished_at=run.finished_at,
        counts=RunCountsOut(**counts),
        stopped_at=run.stopped_at,
        auth_failures=auth_failures,
        auth_failure_limit=runs_service.PUSH_RUN_AUTH_FAILURE_LIMIT,
        signed_in=signed_in,
    )


def _target_out(row: PushRunTarget) -> RunTargetOut:
    return RunTargetOut(
        position=row.position,
        device_id=row.device_id,
        address=str(row.address),
        state=row.state,
        job_id=row.job_id,
        outcome=row.outcome,
        detail=row.detail,
        reasons=list(row.reasons or []),
        updated_at=row.updated_at,
    )


async def _publish_after_commit(request: Request, envelopes: list[Envelope]) -> None:
    for device_id, envelope in envelopes:
        await publish_job(request.app.state.redis, device_id, envelope)


@router.get("/runs/prefixes", response_model=list[RunPrefixOut])
async def list_push_run_prefixes(
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> list[RunPrefixOut]:
    """The subnets a run may be built from: this org's ACTIVE push
    authorizations, and nothing else. None can be typed (design §1.1)."""
    async with org_session(ctx.org_id, engine=_engine(request)) as session:
        rows = await run_list_service.authorized_prefixes(session, ctx.org_id)
        return [
            RunPrefixOut(
                prefix=str(row.prefix),
                address_count=prefix_size(parse_prefix(str(row.prefix))),
                expires_at=row.expires_at,
            )
            for row in rows
        ]


@router.get("/runs/preview", response_model=RunPreviewOut)
async def preview_push_run(
    request: Request,
    prefix: str = Query(min_length=1, max_length=64),
    credential_id: UUID = Query(),
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunPreviewOut:
    """Expand an authorized subnet into the named list a person confirms.
    Reads only: nothing is written, decrypted or signed."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request)) as session:
            listed = await run_list_service.expand_prefix(
                session,
                ctx.org_id,
                actor_id=ctx.user_id,
                prefix=prefix,
                credential_id=credential_id,
                public_console_url=request.app.state.settings.public_console_url,
            )
    except PushError as exc:
        raise _http_error(exc) from None
    ready = len(listed.ready_rows)
    return RunPreviewOut(
        prefix=listed.prefix,
        credential_id=listed.credential_id,
        credential_name=listed.credential_name,
        list_digest=listed.list_digest,
        ready_count=ready,
        blocked_count=len(listed.rows) - ready,
        unclaimed_count=listed.unclaimed_count,
        run_max=run_list_service.PUSH_RUN_MAX,
        concurrency=runs_service.PUSH_RUN_CONCURRENCY,
        rows=[
            RunListRowOut(
                device_id=row.device_id,
                address=row.address,
                hostname=row.hostname,
                os_family=row.os_family,
                ready=row.ready,
                reasons=list(row.reasons),
            )
            for row in listed.rows
        ],
        reason_help=_run_reason_help(),
    )


@router.post("/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def start_push_run(
    body: RunCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunOut:
    """**A PERSON CONFIRMS THE LIST.** The server expands the subnet again and
    refuses (409 `list_changed`) unless the digest matches, before any row is
    written or any secret touched. The pacer then dispatches the first targets
    in this same transaction, and their envelopes are published only after it
    commits."""
    envelopes: list[Envelope] = []
    try:
        async with org_session(ctx.org_id, engine=_engine(request)) as session:
            run = await runs_service.create_run(
                session,
                ctx.org_id,
                actor_id=ctx.user_id,
                prefix=body.prefix,
                credential_id=body.credential_id,
                list_digest=body.list_digest,
                public_console_url=request.app.state.settings.public_console_url,
            )
            envelopes = await runs_service.advance_run(
                session, ctx.org_id, run.id, dispatch=_run_dispatcher(request).dispatch_target
            )
            out = await _run_out(session, ctx.org_id, run)
    except PushError as exc:
        raise _http_error(exc) from None
    await _publish_after_commit(request, envelopes)
    return out


@router.get("/runs", response_model=list[RunOut])
async def list_push_runs(
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> list[RunOut]:
    async with org_session(ctx.org_id, engine=_engine(request)) as session:
        rows = await runs_service.list_runs(session, ctx.org_id)
        return [await _run_out(session, ctx.org_id, row) for row in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_push_run(
    run_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request)) as session:
            run = await runs_service.get_run(session, ctx.org_id, run_id)
            out = await _run_out(session, ctx.org_id, run)
    except PushError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/runs/{run_id}/targets", response_model=RunTargetPageOut)
async def list_push_run_targets(
    run_id: UUID,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunTargetPageOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request)) as session:
            total, rows = await runs_service.list_targets(
                session, ctx.org_id, run_id, offset=offset, limit=limit
            )
            out = RunTargetPageOut(
                total=total,
                items=[_target_out(row) for row in rows],
                reason_help=_run_reason_help(),
            )
    except PushError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/runs/{run_id}/pause", response_model=RunOut)
async def pause_push_run(
    run_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunOut:
    """A person's act. New dispatches stop; installs in flight finish."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request)) as session:
            run = await runs_service.pause_run(
                session, ctx.org_id, run_id=run_id, actor_id=ctx.user_id
            )
            out = await _run_out(session, ctx.org_id, run)
    except PushError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/runs/{run_id}/resume", response_model=RunOut)
async def resume_push_run(
    run_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunOut:
    """A person's act, and the only way a paused run, or one the lockout guard
    stopped (`stopped_credential`, spec §1.4a), runs again. Dispatch restarts
    in this request -- from ONE target, because the guard's window restarts --
    each target re-checked as it goes."""
    envelopes: list[Envelope] = []
    try:
        async with org_session(ctx.org_id, engine=_engine(request)) as session:
            run = await runs_service.resume_run(
                session, ctx.org_id, run_id=run_id, actor_id=ctx.user_id
            )
            envelopes = await runs_service.advance_run(
                session, ctx.org_id, run.id, dispatch=_run_dispatcher(request).dispatch_target
            )
            out = await _run_out(session, ctx.org_id, run)
    except PushError as exc:
        raise _http_error(exc) from None
    await _publish_after_commit(request, envelopes)
    return out


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_push_run(
    run_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunOut:
    """A person's act, and final. Waiting targets are cancelled; installs in
    flight finish and are still recorded."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request)) as session:
            run = await runs_service.cancel_run(
                session, ctx.org_id, run_id=run_id, actor_id=ctx.user_id
            )
            out = await _run_out(session, ctx.org_id, run)
    except PushError as exc:
        raise _http_error(exc) from None
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_push_run_api.py tests/test_agent_push_api.py tests/test_route_session_discipline.py tests/test_route_gating.py tests/test_agent_push_dispatches_nothing.py`
Expected: PASS. `test_route_session_discipline.py` finds no raw session, because every route uses `org_session`.

- [ ] **Step 6: Lint, type-check, and commit**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: no findings.

```bash
git add backend/app/modules/agent_push/schema.py backend/app/modules/agent_push/api.py backend/tests/test_agent_push_api.py backend/tests/test_push_run_api.py
git commit -m "feat(agent-push): /agent-push/runs -- prefixes, preview, confirm, read, pause, resume, cancel

Every route is agent.push.run; preview, confirm and resume also need
credentials.checkout. A changed list is a structured 409; envelopes publish
after commit.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---
### Task 10: Console data layer — types, `ApiClient` methods, hooks, fixtures, presentation

**Files:**
- Modify: `frontend/src/modules/agent-push/types.ts`
- Modify: `frontend/src/lib/api.ts` (the type import at the top, and nine methods after `requestPush`)
- Modify: `frontend/src/modules/agent-push/api.ts` (the run hooks)
- Modify: `frontend/src/modules/agent-push/fixtures.ts` (the run fixtures)
- Create: `frontend/src/modules/agent-push/runState.ts`
- Test: `frontend/src/modules/agent-push/__tests__/runState.test.ts` (create)

**Interfaces:**
- Consumes: the Task 9 routes and models.
- Produces:
  - Types: `PushRunPrefixOut`, `PushRunListRowOut`, `PushRunPreviewOut`, `PushRunCreate`, `PushRunState`, `PushRunTargetState`, `PushRunCounts`, `PushRunOut`, `PushRunTargetOut`, `PushRunTargetPage`.
  - Constants: `PUSH_RUN_MAX = 1024`, `PUSH_RUN_CONCURRENCY = 50`.
  - `ApiClient` methods: `listPushRunPrefixes()`, `previewPushRun(prefix, credentialId)`, `createPushRun(body)`, `listPushRuns()`, `getPushRun(id)`, `listPushRunTargets(id, {offset, limit})`, `pausePushRun(id)`, `resumePushRun(id)`, `cancelPushRun(id)`.
  - Hooks: `usePushRunPrefixes`, `usePushRunPreview(prefix, credentialId)`, `useCreatePushRunMutation`, `usePushRuns`, `usePushRun(id)`, `usePushRunTargets(id, offset, limit, live)`, `usePushRunControl`.
  - From `runState.ts`: `RUN_STATE_LABELS`, `RUN_STATE_KIND`, `mayResume`, `mayCancel`, `TARGET_STATE_LABELS`, `TARGET_STATE_KIND`, `READINESS_CHIPS`, `readinessChip`, `outcomeLabel`, `runReasonLabel`, `runProgress`, `runIsLive`, `countsSentence` and `isListChanged`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/modules/agent-push/__tests__/runState.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { ApiError } from "../../../lib/api";
import {
  READINESS_CHIPS,
  countsSentence,
  isListChanged,
  mayCancel,
  mayResume,
  outcomeLabel,
  readinessChip,
  runIsLive,
  runProgress,
  runReasonLabel,
} from "../runState";
import type { PushRunCounts } from "../types";

const counts = (over: Partial<PushRunCounts> = {}): PushRunCounts => ({
  waiting: 0,
  in_flight: 0,
  installed: 0,
  failed: 0,
  refused: 0,
  cancelled: 0,
  ...over,
});

describe("run presentation", () => {
  it("counts everything terminal as progress, and nothing else", () => {
    expect(runProgress(counts({ installed: 2, failed: 1, refused: 1, waiting: 4, in_flight: 2 }), 10)).toBe(40);
    expect(runProgress(counts(), 0)).toBe(0);
  });

  it("is live while running, or while installs are still in flight", () => {
    expect(runIsLive({ state: "running", counts: counts() })).toBe(true);
    expect(runIsLive({ state: "cancelled", counts: counts({ in_flight: 1 }) })).toBe(true);
    expect(runIsLive({ state: "paused", counts: counts() })).toBe(false);
    expect(runIsLive({ state: "completed", counts: counts() })).toBe(false);
    // A stopped run still settles installs in flight, then goes quiet.
    expect(runIsLive({ state: "stopped_credential", counts: counts({ in_flight: 1 }) })).toBe(true);
    expect(runIsLive({ state: "stopped_credential", counts: counts() })).toBe(false);
  });

  it("lets a person resume a paused or stopped run, and cancel any active one", () => {
    expect(mayResume("paused")).toBe(true);
    expect(mayResume("stopped_credential")).toBe(true);
    expect(mayResume("running")).toBe(false);
    expect(mayResume("cancelled")).toBe(false);
    expect(mayCancel("stopped_credential")).toBe(true);
    expect(mayCancel("completed")).toBe(false);
  });

  it("gives every blocking reason the server can name a short chip", () => {
    for (const code of [
      "credential_not_scoped",
      "no_site_server",
      "site_server_too_old",
      "no_winrm_site_server",
      "no_dns_name",
      "invalid_dns_name",
      "wrong_credential_kind",
      "no_console_url",
      "outside_realm",
      "realm_not_recorded",
      "host_key_unconfirmed",
      "host_key_changed",
      "not_installable",
    ]) {
      expect(READINESS_CHIPS[code], code).toBeTruthy();
    }
    expect(readinessChip("host_key_unconfirmed")).toBe("Not probed");
    expect(readinessChip("something_new")).toBe("Not ready");
  });

  it("says a run's own refusals and outcomes in words, never as codes", () => {
    expect(runReasonLabel("already_enrolled")).not.toContain("_");
    expect(runReasonLabel("credential_missing")).not.toContain("_");
    expect(outcomeLabel("auth_failed")).toBe("The credential was refused");
    expect(outcomeLabel("never_seen")).toBe("Finished");
    expect(countsSentence(counts({ installed: 3, failed: 1, waiting: 2, in_flight: 1 }))).toBe(
      "3 installed · 1 failed · 0 refused · 3 to go",
    );
  });

  it("recognises the server's list-changed conflict and nothing else", () => {
    expect(isListChanged(new ApiError(409, JSON.stringify({ detail: { code: "list_changed", message: "x" } })))).toBe(true);
    expect(isListChanged(new ApiError(409, JSON.stringify({ detail: "one active run per organization" })))).toBe(false);
    expect(isListChanged(new ApiError(500, "boom"))).toBe(false);
    expect(isListChanged(new Error("nope"))).toBe(false);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run src/modules/agent-push/__tests__/runState.test.ts`
Expected: FAIL with `Failed to resolve import "../runState"`.

- [ ] **Step 3: Add the types**

Append to `frontend/src/modules/agent-push/types.ts`:

```ts
// --------------------------------------------------------------------------
// Push runs (design 2026-09-30 §1). Mirrors `schema.py`'s Run* models. Still
// nothing here can hold a secret.
// --------------------------------------------------------------------------

/** `run_list.PUSH_RUN_MAX`: the most READY machines one run may hold. */
export const PUSH_RUN_MAX = 1024;
/** `runs.PUSH_RUN_CONCURRENCY`: the most installs in flight at once. */
export const PUSH_RUN_CONCURRENCY = 50;

export interface PushRunPrefixOut {
  prefix: string;
  address_count: number;
  expires_at: string | null;
}

export interface PushRunListRowOut {
  device_id: string;
  address: string;
  hostname: string;
  os_family: string;
  ready: boolean;
  /** Wire codes. Shown through `readinessChip`, never raw. */
  reasons: string[];
}

export interface PushRunPreviewOut {
  prefix: string;
  credential_id: string;
  credential_name: string;
  /** Computed by the server; echoed back unchanged to confirm. */
  list_digest: string;
  ready_count: number;
  blocked_count: number;
  unclaimed_count: number;
  run_max: number;
  concurrency: number;
  rows: PushRunListRowOut[];
  reason_help: Record<string, string>;
}

export interface PushRunCreate {
  prefix: string;
  credential_id: string;
  list_digest: string;
}

/** `stopped_credential`: the lockout guard stopped the run after repeated
 * sign-in failures (spec §1.4a). Only a person's Resume restarts it. */
export type PushRunState = "running" | "paused" | "stopped_credential" | "cancelled" | "completed";
export type PushRunTargetState = "waiting" | "in_flight" | "installed" | "failed" | "refused" | "cancelled";
export type PushRunCounts = Record<PushRunTargetState, number>;

export interface PushRunOut {
  id: string;
  prefix: string;
  credential_id: string | null;
  credential_name: string;
  created_by: string;
  created_at: string;
  state: PushRunState;
  target_count: number;
  concurrency: number;
  list_digest: string;
  finished_at: string | null;
  counts: PushRunCounts;
  /** When the lockout guard stopped the run; null otherwise. */
  stopped_at: string | null;
  /** Sign-in failures since the run last started or resumed. */
  auth_failures: number;
  /** How many stop the run (`runs.PUSH_RUN_AUTH_FAILURE_LIMIT`). */
  auth_failure_limit: number;
  /** A sign-in has got through since then. False: one machine at a time. */
  signed_in: boolean;
}

export interface PushRunTargetOut {
  position: number;
  device_id: string | null;
  address: string;
  state: PushRunTargetState;
  job_id: string | null;
  outcome: string;
  detail: string;
  reasons: string[];
  updated_at: string;
}

export interface PushRunTargetPage {
  total: number;
  items: PushRunTargetOut[];
  reason_help: Record<string, string>;
}
```

- [ ] **Step 4: Add the `ApiClient` methods**

In `frontend/src/lib/api.ts`, extend the `import type { ... } from "../modules/agent-push/types";` block at the top so it also imports `PushRunCreate`, `PushRunOut`, `PushRunPrefixOut`, `PushRunPreviewOut` and `PushRunTargetPage`. Then add these directly after `requestPush(...)`:

```ts
  // Push runs (design 2026-09-30). Every route is `agent.push.run`; preview,
  // create and resume also need `credentials.checkout`, server-side. None is
  // ever retried by the caller.
  listPushRunPrefixes(): Promise<PushRunPrefixOut[]> {
    return this.request<PushRunPrefixOut[]>("/agent-push/runs/prefixes");
  }

  previewPushRun(prefix: string, credentialId: string): Promise<PushRunPreviewOut> {
    const query = new URLSearchParams({ prefix, credential_id: credentialId });
    return this.request<PushRunPreviewOut>(`/agent-push/runs/preview?${query.toString()}`);
  }

  createPushRun(body: PushRunCreate): Promise<PushRunOut> {
    return this.request<PushRunOut>("/agent-push/runs", { method: "POST", body: JSON.stringify(body) });
  }

  listPushRuns(): Promise<PushRunOut[]> {
    return this.request<PushRunOut[]>("/agent-push/runs");
  }

  getPushRun(id: string): Promise<PushRunOut> {
    return this.request<PushRunOut>(`/agent-push/runs/${id}`);
  }

  listPushRunTargets(id: string, params: { offset: number; limit: number }): Promise<PushRunTargetPage> {
    const query = new URLSearchParams({ offset: String(params.offset), limit: String(params.limit) });
    return this.request<PushRunTargetPage>(`/agent-push/runs/${id}/targets?${query.toString()}`);
  }

  pausePushRun(id: string): Promise<PushRunOut> {
    return this.request<PushRunOut>(`/agent-push/runs/${id}/pause`, { method: "POST" });
  }

  resumePushRun(id: string): Promise<PushRunOut> {
    return this.request<PushRunOut>(`/agent-push/runs/${id}/resume`, { method: "POST" });
  }

  cancelPushRun(id: string): Promise<PushRunOut> {
    return this.request<PushRunOut>(`/agent-push/runs/${id}/cancel`, { method: "POST" });
  }
```

- [ ] **Step 5: Add the hooks**

In `frontend/src/modules/agent-push/api.ts`, extend the `./types` import with `type PushRunCreate`, `type PushRunOut`, `type PushRunPrefixOut`, `type PushRunPreviewOut` and `type PushRunTargetPage`. Add `import { runIsLive } from "./runState";`, then append:

```ts
// --------------------------------------------------------------------------
// Push runs (design 2026-09-30)
// --------------------------------------------------------------------------
//
// **THE PREVIEW IS NEVER REFETCHED BEHIND THE OPERATOR'S BACK.** The list on
// screen is what they confirm, bound by its digest; a background refetch that
// swapped rows under them would make the screen and the confirmation describe
// different machines. It refetches only when asked (a changed-list 409).
//
// **A LIVE RUN'S PAGE POLLS EVERY FIVE SECONDS**, and nothing else here does. A
// run in flight is telemetry; once it is paused with nothing in flight,
// cancelled or finished, polling stops.

export const PUSH_RUNS_KEY = "agent-push-runs";

export function usePushRunPrefixes() {
  const api = useApi();
  return useQuery<PushRunPrefixOut[], ApiError>({
    queryKey: [PUSH_RUNS_KEY, "prefixes"],
    queryFn: () => api.listPushRunPrefixes(),
    ...NO_RETRY,
  });
}

export function usePushRunPreview(prefix: string, credentialId: string) {
  const api = useApi();
  return useQuery<PushRunPreviewOut, ApiError>({
    queryKey: [PUSH_RUNS_KEY, "preview", prefix, credentialId],
    queryFn: () => api.previewPushRun(prefix, credentialId),
    enabled: prefix !== "" && credentialId !== "",
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    ...NO_RETRY,
  });
}

/** Confirm a list. Deliberately NOT retried: a retry is a second confirmation. */
export function useCreatePushRunMutation() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<PushRunOut, ApiError, PushRunCreate>({
    mutationFn: (body) => api.createPushRun(body),
    retry: false,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [PUSH_RUNS_KEY] });
    },
  });
}

export function usePushRuns() {
  const api = useApi();
  return useQuery<PushRunOut[], ApiError>({
    queryKey: [PUSH_RUNS_KEY, "list"],
    queryFn: () => api.listPushRuns(),
    ...NO_RETRY,
  });
}

export function usePushRun(id: string) {
  const api = useApi();
  return useQuery<PushRunOut, ApiError>({
    queryKey: [PUSH_RUNS_KEY, "run", id],
    queryFn: () => api.getPushRun(id),
    enabled: id !== "",
    refetchInterval: (query) => (query.state.data && runIsLive(query.state.data) ? 5_000 : false),
    ...NO_RETRY,
  });
}

export function usePushRunTargets(id: string, offset: number, limit: number, live: boolean) {
  const api = useApi();
  return useQuery<PushRunTargetPage, ApiError>({
    queryKey: [PUSH_RUNS_KEY, "run", id, "targets", offset, limit],
    queryFn: () => api.listPushRunTargets(id, { offset, limit }),
    enabled: id !== "",
    refetchInterval: live ? 5_000 : false,
    ...NO_RETRY,
  });
}

/** Pause, Resume or Cancel: each a person's act, never retried. */
export function usePushRunControl() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<PushRunOut, ApiError, { runId: string; action: "pause" | "resume" | "cancel" }>({
    mutationFn: ({ runId, action }) =>
      action === "pause"
        ? api.pausePushRun(runId)
        : action === "resume"
          ? api.resumePushRun(runId)
          : api.cancelPushRun(runId),
    retry: false,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [PUSH_RUNS_KEY] });
    },
  });
}
```

- [ ] **Step 6: Write `runState.ts`**

Create `frontend/src/modules/agent-push/runState.ts`:

```ts
/**
 * Presentation for push runs (design 2026-09-30 §1.7).
 *
 * Every server code reaches the screen as words: a short chip for a readiness
 * reason (with the server's longer remedy as its title), a sentence for a
 * refusal or an outcome. A code this console has not been taught falls back
 * to a neutral word, never to the code itself (`noWireNames.test.ts`).
 */

import { ApiError } from "../../lib/api";
import type { Kind } from "../../shell/components";
import { refusalReasonLabel } from "./pushState";
import type { PushRunCounts, PushRunState, PushRunTargetState } from "./types";

export const RUN_STATE_LABELS: Readonly<Record<PushRunState, string>> = {
  running: "Running",
  paused: "Paused",
  stopped_credential: "Stopped: sign-in failures",
  cancelled: "Cancelled",
  completed: "Finished",
};

export const RUN_STATE_KIND: Readonly<Record<PushRunState, Kind>> = {
  running: "run",
  paused: "warn",
  stopped_credential: "crit",
  cancelled: "mut",
  completed: "ok",
};

/** The states a person may Resume from (spec §1.4a, §1.5). */
export function mayResume(state: PushRunState): boolean {
  return state === "paused" || state === "stopped_credential";
}

/** The states a person may Cancel from: every active one. */
export function mayCancel(state: PushRunState): boolean {
  return state === "running" || state === "paused" || state === "stopped_credential";
}

export const TARGET_STATE_LABELS: Readonly<Record<PushRunTargetState, string>> = {
  waiting: "Waiting",
  in_flight: "Installing",
  installed: "Installed",
  failed: "Failed",
  refused: "Refused",
  cancelled: "Cancelled",
};

export const TARGET_STATE_KIND: Readonly<Record<PushRunTargetState, Kind>> = {
  waiting: "mut",
  in_flight: "run",
  installed: "ok",
  failed: "crit",
  refused: "warn",
  cancelled: "mut",
};

/** Short chip text for a readiness reason; the long remedy goes in a title. */
export const READINESS_CHIPS: Readonly<Record<string, string>> = {
  no_asset: "Nobody vouched for it",
  no_authorization: "Address not authorized",
  credential_not_scoped: "Credential not allowed here",
  no_site_server: "No site server for its segment",
  site_server_too_old: "Site server too old",
  no_winrm_site_server: "Site server too old for Windows",
  no_dns_name: "No DNS name",
  invalid_dns_name: "DNS name not usable",
  wrong_credential_kind: "Credential type does not match",
  no_console_url: "Console address not set",
  outside_realm: "Outside the account's domain",
  realm_not_recorded: "Account needs saving again",
  host_key_unconfirmed: "Not probed",
  host_key_changed: "Host key changed",
  not_installable: "Operating system or processor not set",
};

export function readinessChip(code: string): string {
  return READINESS_CHIPS[code] ?? "Not ready";
}

/** What a target's job came to, once the site server answered. */
const OUTCOME_LABELS: Readonly<Record<string, string>> = {
  installed: "Installed",
  os_mismatch: "Its operating system was not the one recorded",
  unreachable: "The site server could not reach it",
  auth_failed: "The credential was refused",
  failed: "The install failed",
  job_failed: "The site server reported a failure",
  agent_refused: "The site server refused the job",
  expired: "The site server never picked the job up",
  job_cancelled: "Cancelled on the site server",
  lost: "No answer from the site server",
  job_missing: "Its job record is missing",
};

export function outcomeLabel(code: string): string {
  return OUTCOME_LABELS[code] ?? "Finished";
}

/** The refusals only a run reaches (`runs.RUN_REFUSAL_REASONS`). */
const RUN_REASON_LABELS: Readonly<Record<string, string>> = {
  run_owner_not_permitted: "The person who started this run may no longer push install.",
  credential_missing: "The run's credential was removed or changed.",
  target_changed: "This address now belongs to a different machine record.",
  already_enrolled: "It is running the agent now.",
  not_installable: "Its operating system or processor is not set.",
  no_release: "No agent package is published for its operating system and processor.",
  credential_unreadable: "The run's credential could not be read: save it again under Credentials.",
  site_server_awaiting_review: "The site server for its segment is waiting for review.",
};

export function runReasonLabel(code: string, help?: string): string {
  return RUN_REASON_LABELS[code] ?? refusalReasonLabel(code, help);
}

/** Percent of the run that is finished, whatever the outcome. */
export function runProgress(counts: PushRunCounts, total: number): number {
  if (total <= 0) return 0;
  const done = counts.installed + counts.failed + counts.refused + counts.cancelled;
  return (done / total) * 100;
}

/** Still changing: running, or with installs in flight to settle. */
export function runIsLive(run: { state: PushRunState; counts: PushRunCounts }): boolean {
  return run.state === "running" || run.counts.in_flight > 0;
}

export function countsSentence(counts: PushRunCounts): string {
  return `${counts.installed} installed · ${counts.failed} failed · ${counts.refused} refused · ${
    counts.waiting + counts.in_flight
  } to go`;
}

/** The server's structured 409 for a list that changed since it was shown. */
export function isListChanged(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  try {
    const body = JSON.parse(error.message) as { detail?: { code?: unknown } | string };
    return typeof body.detail === "object" && body.detail?.code === "list_changed";
  } catch {
    return false;
  }
}
```

- [ ] **Step 7: Add the fixtures**

In `frontend/src/modules/agent-push/fixtures.ts`, extend the type import with `PushRunOut`, `PushRunPreviewOut` and `PushRunTargetOut`. Add this above `export const fixtures`:

```ts
/** A run on the Lisbon floor, part-way through (design 2026-09-30 §1.7). */
const RUN: PushRunOut = {
  id: "run-1",
  prefix: "10.20.4.0/24",
  credential_id: "cred-push-b4",
  credential_name: "Push install · rack B4",
  created_by: "u1",
  created_at: inDays(0),
  state: "running",
  target_count: 6,
  concurrency: 50,
  list_digest: "ab".repeat(32),
  finished_at: null,
  counts: { waiting: 1, in_flight: 2, installed: 1, failed: 1, refused: 1, cancelled: 0 },
  stopped_at: null,
  auth_failures: 1,
  auth_failure_limit: 3,
  signed_in: true,
};

const RUN_TARGETS: PushRunTargetOut[] = [
  { position: 0, device_id: "dev-1", address: "10.20.4.12", state: "installed", job_id: "job-1", outcome: "installed", detail: "", reasons: [], updated_at: inDays(0) },
  { position: 1, device_id: "dev-2", address: "10.20.4.13", state: "failed", job_id: "job-2", outcome: "auth_failed", detail: "Permission denied (publickey,password).", reasons: [], updated_at: inDays(0) },
  { position: 2, device_id: "dev-3", address: "10.20.4.14", state: "refused", job_id: null, outcome: "", detail: "", reasons: ["host_key_changed"], updated_at: inDays(0) },
  { position: 3, device_id: "dev-4", address: "10.20.4.15", state: "in_flight", job_id: "job-3", outcome: "", detail: "", reasons: [], updated_at: inDays(0) },
  { position: 4, device_id: "dev-5", address: "10.20.4.16", state: "in_flight", job_id: "job-4", outcome: "", detail: "", reasons: [], updated_at: inDays(0) },
  { position: 5, device_id: "dev-6", address: "10.20.4.17", state: "waiting", job_id: null, outcome: "", detail: "", reasons: [], updated_at: inDays(0) },
];

const PREVIEW: PushRunPreviewOut = {
  prefix: "10.20.4.0/24",
  credential_id: "cred-push-b4",
  credential_name: "Push install · rack B4",
  list_digest: "cd".repeat(32),
  ready_count: 1,
  blocked_count: 2,
  unclaimed_count: 4,
  run_max: 1024,
  concurrency: 50,
  rows: [
    { device_id: "dev-1", address: "10.20.4.12", hostname: "lis-srv-12", os_family: "linux", ready: true, reasons: [] },
    { device_id: "dev-7", address: "10.20.4.31", hostname: "lis-ws-31", os_family: "windows", ready: false, reasons: ["wrong_credential_kind"] },
    { device_id: "dev-8", address: "10.20.4.40", hostname: "lis-srv-40", os_family: "linux", ready: false, reasons: ["host_key_unconfirmed"] },
  ],
  reason_help: {},
};

const listPushRunPrefixes: FixtureMap["listPushRunPrefixes"] = () =>
  GRANTS.filter((g) => g.revoked_at === null).map((g) => ({
    prefix: g.prefix,
    address_count: g.address_count,
    expires_at: g.expires_at,
  }));
const previewPushRun: FixtureMap["previewPushRun"] = (prefix) => ({ ...PREVIEW, prefix });
const createPushRun: FixtureMap["createPushRun"] = (body) => ({ ...RUN, prefix: body.prefix });
const listPushRuns: FixtureMap["listPushRuns"] = () => [
  RUN,
  { ...RUN, id: "run-0", prefix: "10.30.1.0/24", state: "completed", created_at: inDays(-3), finished_at: inDays(-3), counts: { waiting: 0, in_flight: 0, installed: 5, failed: 1, refused: 0, cancelled: 0 } },
];
const getPushRun: FixtureMap["getPushRun"] = (id) => ({ ...RUN, id });
const listPushRunTargets: FixtureMap["listPushRunTargets"] = (_id, { offset, limit }) => ({
  total: RUN_TARGETS.length,
  items: RUN_TARGETS.slice(offset, offset + limit),
  reason_help: {},
});
const pausePushRun: FixtureMap["pausePushRun"] = (id) => ({ ...RUN, id, state: "paused" });
const resumePushRun: FixtureMap["resumePushRun"] = (id) => ({ ...RUN, id, state: "running" });
const cancelPushRun: FixtureMap["cancelPushRun"] = (id) => ({ ...RUN, id, state: "cancelled" });
```

and add these to the `fixtures` object: `listPushRunPrefixes`, `previewPushRun`, `createPushRun`, `listPushRuns`, `getPushRun`, `listPushRunTargets`, `pausePushRun`, `resumePushRun`, `cancelPushRun`.

- [ ] **Step 8: Run the checks**

Run: `cd frontend && npx vitest run src/modules/agent-push/__tests__/runState.test.ts && npx tsc --noEmit`
Expected: PASS, and no type errors.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/modules/agent-push/types.ts frontend/src/lib/api.ts frontend/src/modules/agent-push/api.ts frontend/src/modules/agent-push/fixtures.ts frontend/src/modules/agent-push/runState.ts frontend/src/modules/agent-push/__tests__/runState.test.ts
git commit -m "feat(console): push run types, client, hooks, fixtures and presentation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 11: Console — the **By subnet** choice in "Push install the agent…"

**Files:**
- Create: `frontend/src/modules/agent-push/PushBySubnetFlow.tsx`
- Create: `frontend/src/modules/agent-push/PushInstallChooser.tsx`
- Modify: `frontend/src/modules/devices/DevicesPage.tsx`: the imports, the page-head actions, and the flow mount (`{pushing && selected.length > 0 ? (<PushInstallFlow …/>) : null}`)
- Test: `frontend/src/modules/agent-push/__tests__/pushBySubnet.test.tsx` (create)

**Interfaces:**
- Consumes: the Task 10 hooks and `runState` helpers, `usePushCredentials`, and the existing `PushInstallFlow`.
- Produces:
  - `PushBySubnetFlow({ onClose })`.
  - `PushInstallChooser({ machines, onRemove, onClose })`, which defaults to **Ticked machines** when any are ticked and to **By subnet** otherwise.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/modules/agent-push/__tests__/pushBySubnet.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { ApiError } from "../../../lib/api";
import { PushInstallChooser } from "../PushInstallChooser";
import type { PushRunPreviewOut } from "../types";

// **THE LAST SCREEN BEFORE A CREDENTIAL IS PRESENTED TO A WHOLE SUBNET.** Each
// assertion is a claim the screen makes to the operator: only authorized
// subnets can be chosen, every listed machine is shown with why it is or is
// not ready, the count on the button is the ready count, and a list that
// changed is shown again rather than started.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function preview(over: Partial<PushRunPreviewOut> = {}): PushRunPreviewOut {
  return {
    prefix: "10.20.4.0/24",
    credential_id: "c1",
    credential_name: "Push install · rack B4",
    list_digest: "ab".repeat(32),
    ready_count: 2,
    blocked_count: 1,
    unclaimed_count: 3,
    run_max: 1024,
    concurrency: 50,
    rows: [
      { device_id: "d1", address: "10.20.4.12", hostname: "lis-srv-12", os_family: "linux", ready: true, reasons: [] },
      { device_id: "d2", address: "10.20.4.13", hostname: "lis-srv-13", os_family: "linux", ready: true, reasons: [] },
      { device_id: "d3", address: "10.20.4.40", hostname: "lis-srv-40", os_family: "linux", ready: false, reasons: ["host_key_unconfirmed"] },
    ],
    reason_help: {},
    ...over,
  };
}

function stubApi(overrides: Partial<Record<string, ReturnType<typeof vi.fn>>> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: ["agent.push.run"] }),
    listCredentials: vi.fn().mockResolvedValue([
      { id: "c1", name: "Push install · rack B4", type: "push_install", device_group_id: null, created_at: null, last_used_at: null, has_secret: true },
      { id: "c2", name: "Remote control", type: "remote_control", device_group_id: null, created_at: null, last_used_at: null, has_secret: true },
    ]),
    listPushRunPrefixes: vi.fn().mockResolvedValue([{ prefix: "10.20.4.0/24", address_count: 256, expires_at: null }]),
    previewPushRun: vi.fn().mockResolvedValue(preview()),
    createPushRun: vi.fn().mockResolvedValue({ id: "run-9" }),
    listPushHostKeys: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderChooser(machines: never[] = []) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/devices"]}>
        <Routes>
          <Route path="/devices" element={<PushInstallChooser machines={machines} onRemove={() => {}} onClose={() => {}} />} />
          <Route path="/devices/push-runs/:runId" element={<p>run page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function choose(user: ReturnType<typeof userEvent.setup>) {
  await user.selectOptions(await screen.findByLabelText("Subnet"), "10.20.4.0/24");
  await waitFor(() => expect(screen.getByRole("option", { name: /rack B4/ })).toBeTruthy());
  await user.selectOptions(screen.getByLabelText("Credential"), "c1");
}

beforeEach(() => mockUseApi.mockReset());

describe("Push install by subnet", () => {
  it("opens on By subnet when nothing is ticked, and offers only authorized subnets, never a text box", async () => {
    stubApi();
    renderChooser();
    expect((screen.getByRole("radio", { name: /By subnet/ }) as HTMLInputElement).checked).toBe(true);
    const subnet = await screen.findByLabelText("Subnet");
    await waitFor(() => expect(within(subnet).getAllByRole("option")).toHaveLength(2));
    expect(within(subnet).getByRole("option", { name: /10\.20\.4\.0\/24/ })).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("offers only push credentials", async () => {
    stubApi();
    renderChooser();
    const credential = await screen.findByLabelText("Credential");
    await waitFor(() => expect(within(credential).getByRole("option", { name: /rack B4/ })).toBeTruthy());
    expect(within(credential).queryByRole("option", { name: /Remote control/ })).toBeNull();
  });

  it("shows every listed machine with its readiness, the unclaimed count, and the ready count on the button", async () => {
    const user = userEvent.setup();
    stubApi();
    renderChooser();
    await choose(user);
    const table = await screen.findByRole("table", { name: "Machines in this subnet" });
    for (const address of ["10.20.4.12", "10.20.4.13", "10.20.4.40"]) {
      expect(within(table).getByText(address)).toBeTruthy();
    }
    expect(within(table).getAllByText("Ready")).toHaveLength(2);
    expect(within(table).getByText("Not probed")).toBeTruthy();
    expect(screen.getByText(/3 machines the network sweep found are not listed/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Start push to 2 machines" })).toBeTruthy();
    expect(screen.queryByText(/and \d+ more/i)).toBeNull();
  });

  it("confirms exactly the digest it was shown, then opens the run page", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    renderChooser();
    await choose(user);
    await user.click(await screen.findByRole("button", { name: "Start push to 2 machines" }));
    expect(api.createPushRun).toHaveBeenCalledWith({
      prefix: "10.20.4.0/24",
      credential_id: "c1",
      list_digest: "ab".repeat(32),
    });
    expect(await screen.findByText("run page")).toBeTruthy();
  });

  it("shows a changed list again instead of starting it", async () => {
    const user = userEvent.setup();
    const api = stubApi({
      createPushRun: vi.fn().mockRejectedValue(
        new ApiError(409, JSON.stringify({ detail: { code: "list_changed", message: "changed" } })),
      ),
    });
    renderChooser();
    await choose(user);
    await user.click(await screen.findByRole("button", { name: "Start push to 2 machines" }));
    expect(await screen.findByText(/changed since the list was shown/)).toBeTruthy();
    await waitFor(() => expect(api.previewPushRun).toHaveBeenCalledTimes(2));
  });

  it("cannot start when nothing is ready", async () => {
    const user = userEvent.setup();
    stubApi({ previewPushRun: vi.fn().mockResolvedValue(preview({ ready_count: 0, rows: [] })) });
    renderChooser();
    await choose(user);
    expect(((await screen.findByRole("button", { name: "Start push to 0 machines" })) as HTMLButtonElement).disabled).toBe(true);
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && npx vitest run src/modules/agent-push/__tests__/pushBySubnet.test.tsx`
Expected: FAIL with `Failed to resolve import "../PushInstallChooser"`.

- [ ] **Step 3: Write `PushBySubnetFlow.tsx`**

Create `frontend/src/modules/agent-push/PushBySubnetFlow.tsx`:

```tsx
// "By subnet": the list a person confirms, built from a subnet (design
// 2026-09-30 §1.1, §1.7; CLAUDE.md 2a(f) as amended 2026-09-30).
//
//   1. THE SUBNET is one of this organization's active push authorizations,
//      chosen from a list. There is no text box: a subnet that is not
//      authorized cannot be chosen, and none can be typed.
//   2. THE CREDENTIAL is one push credential. One run presents one credential,
//      so machines of the other kind are listed as not ready, never
//      half-pushed.
//   3. THE LIST is every vouched-for machine the subnet holds, each with why it
//      is or is not ready. Every row is shown; there is no "and 40 more".
//   4. START sends back the list's digest, so what is confirmed is these
//      machines and not "the subnet". If the subnet changed meanwhile, the
//      server refuses, and the list is fetched and shown again.
//
// NOTHING HERE GATES ANYTHING (invariant 6): the server checks
// `agent.push.run`, `credentials.checkout`, every bound and the digest.

import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { describeSubmitError } from "../../components/form";
import { Button, Field, Td, Th } from "../../components/ui";
import { Panel, StatusPill, TableWrap } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { credentialTypeLabel } from "../credentials/credentialTypes";
import { osFamilyLabel } from "../devices/osLabel";
import { useCreatePushRunMutation, usePushCredentials, usePushRunPrefixes, usePushRunPreview } from "./api";
import { formatCount, refusalReasonLabel } from "./pushState";
import { isListChanged, readinessChip } from "./runState";

export function PushBySubnetFlow({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const prefixes = usePushRunPrefixes();
  const credentials = usePushCredentials();
  const [prefix, setPrefix] = useState("");
  const [credentialId, setCredentialId] = useState("");
  const [changed, setChanged] = useState(false);
  const preview = usePushRunPreview(prefix, credentialId);
  const start = useCreatePushRunMutation();
  const list = preview.data;

  function confirm() {
    if (!list) return;
    setChanged(false);
    start.mutate(
      { prefix: list.prefix, credential_id: list.credential_id, list_digest: list.list_digest },
      {
        onSuccess: (run) => navigate(`/devices/push-runs/${run.id}`),
        onError: (error) => {
          if (isListChanged(error)) {
            setChanged(true);
            void preview.refetch();
          }
        },
      },
    );
  }

  const noun = (n: number) => (n === 1 ? "machine" : "machines");

  return (
    <Panel
      title="Push install by subnet"
      subtitle="The machines of one authorized subnet, as a list you read and confirm."
      actions={
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      }
    >
      <div className="form-grid">
        <Field label="Subnet">
          <select
            className="select-field"
            value={prefix}
            onChange={(event) => {
              setPrefix(event.target.value);
              setChanged(false);
            }}
          >
            <option value="">Choose an authorized subnet</option>
            {(prefixes.data ?? []).map((p) => (
              <option key={p.prefix} value={p.prefix}>
                {p.prefix} ({formatCount(p.address_count)} addresses)
              </option>
            ))}
          </select>
        </Field>
        <Field label="Credential">
          <select
            className="select-field"
            value={credentialId}
            onChange={(event) => {
              setCredentialId(event.target.value);
              setChanged(false);
            }}
          >
            <option value="">Choose a push credential</option>
            {(credentials.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({credentialTypeLabel(c.type)})
              </option>
            ))}
          </select>
        </Field>
      </div>

      {prefixes.isSuccess && prefixes.data.length === 0 ? (
        <p className="note mt-3">
          No subnet is authorized for push install yet. Someone who manages push ranges authorizes
          one under Push authorizations.
        </p>
      ) : null}
      {preview.isLoading ? <p className="note mt-3">Listing the machines in this subnet…</p> : null}
      {preview.isError ? (
        <p role="alert" className="form-error mt-3">
          {describeSubmitError(preview.error)}
        </p>
      ) : null}
      {changed ? (
        <p role="alert" className="form-error mt-3">
          The machines in this subnet changed since the list was shown, so nothing was started.
          Read the list below again, then start the push.
        </p>
      ) : null}

      {list ? (
        <>
          <div className="notice gold mt-3" role="note">
            <Icon name="warning" size={17} />
            <p>
              One credential is presented to every ready machine on this list: to one machine
              first, then to at most {list.concurrency} at a time once a sign-in has succeeded. If
              the credential is refused by 3 machines the run stops itself, so a wrong domain
              password cannot lock the account out for everyone. Machines that are not ready are
              shown and will not be pushed to.
            </p>
          </div>
          <TableWrap>
            <table aria-label="Machines in this subnet">
              <thead>
                <tr>
                  <Th>Machine</Th>
                  <Th>Address</Th>
                  <Th>Operating system</Th>
                  <Th>Ready for a push</Th>
                </tr>
              </thead>
              <tbody>
                {list.rows.map((row) => (
                  <tr key={row.device_id}>
                    <Td>
                      <Link to={`/devices/${row.device_id}`}>{row.hostname || row.address}</Link>
                    </Td>
                    <Td>
                      <code>{row.address}</code>
                    </Td>
                    <Td>{row.os_family ? osFamilyLabel(row.os_family) : "Not recorded"}</Td>
                    <Td>
                      {row.ready ? (
                        <StatusPill value="ready" label="Ready" kind="ok" />
                      ) : (
                        row.reasons.map((reason) => (
                          <span key={reason} title={refusalReasonLabel(reason, list.reason_help[reason])}>
                            <StatusPill value={reason} label={readinessChip(reason)} kind="warn" />
                          </span>
                        ))
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
          {list.unclaimed_count > 0 ? (
            <p className="note mt-3">
              {list.unclaimed_count}{" "}
              {list.unclaimed_count === 1
                ? "machine the network sweep found is"
                : "machines the network sweep found are"}{" "}
              not listed, because nobody has claimed them. <Link to="/discovered">Claim them on Discovered</Link>{" "}
              to include them.
            </p>
          ) : null}
          {start.isError && !isListChanged(start.error) ? (
            <p role="alert" className="form-error mt-3">
              {describeSubmitError(start.error)}
            </p>
          ) : null}
          <div className="mt-3">
            <Button onClick={confirm} disabled={list.ready_count === 0 || start.isPending}>
              Start push to {list.ready_count} {noun(list.ready_count)}
            </Button>
          </div>
        </>
      ) : null}
    </Panel>
  );
}
```

- [ ] **Step 4: Write `PushInstallChooser.tsx`**

Create `frontend/src/modules/agent-push/PushInstallChooser.tsx`:

```tsx
// "Push install the agent…": ticked machines, or a subnet's list (design
// 2026-09-30 §1.1). Two ways to NAME machines, and each ends in a person
// confirming named machines. The first keeps the batch ceiling of 25; the
// second is a run, at most 1,024 confirmed machines, 50 at a time
// (CLAUDE.md 2a(f) as amended 2026-09-30).

import { useState } from "react";

import { Button } from "../../components/ui";
import type { Device } from "../../lib/api";
import { Panel } from "../../shell/components";
import { PushBySubnetFlow } from "./PushBySubnetFlow";
import { PushInstallFlow } from "./PushInstallFlow";
import { PUSH_BATCH_MAX, PUSH_RUN_CONCURRENCY } from "./types";

export function PushInstallChooser({
  machines,
  onRemove,
  onClose,
}: {
  machines: readonly Device[];
  onRemove: (id: string) => void;
  onClose: () => void;
}) {
  const [mode, setMode] = useState<"ticked" | "subnet">(machines.length > 0 ? "ticked" : "subnet");
  return (
    <div className="space-y-4">
      <Panel
        title="Push install: choose the machines"
        actions={
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        }
      >
        <fieldset className="field full">
          <legend className="note" style={{ marginBottom: 6, fontWeight: 500 }}>
            Which machines
          </legend>
          <div className="flex flex-col gap-1.5">
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="push-choose-machines"
                value="ticked"
                checked={mode === "ticked"}
                onChange={() => setMode("ticked")}
              />
              <span>
                Ticked machines{" "}
                <span className="note">— up to {PUSH_BATCH_MAX}, ticked one by one in the list below</span>
              </span>
            </label>
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="push-choose-machines"
                value="subnet"
                checked={mode === "subnet"}
                onChange={() => setMode("subnet")}
              />
              <span>
                By subnet{" "}
                <span className="note">
                  — an authorized subnet's machines, as a list you confirm, pushed {PUSH_RUN_CONCURRENCY} at a
                  time
                </span>
              </span>
            </label>
          </div>
        </fieldset>
      </Panel>
      {mode === "subnet" ? (
        <PushBySubnetFlow onClose={onClose} />
      ) : machines.length > 0 ? (
        <PushInstallFlow machines={machines} onRemove={onRemove} onClose={onClose} />
      ) : (
        <p className="note">Tick the machines to push to in the list below, one by one.</p>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Wire it into Devices**

In `frontend/src/modules/devices/DevicesPage.tsx`:

(a) Replace `import { PushInstallFlow } from "../agent-push/PushInstallFlow";` with `import { PushInstallChooser } from "../agent-push/PushInstallChooser";`.

(b) In `PageHead`'s `actions`, add this directly before the `<Link className="btn primary" to={ADD_COMPUTER_TO}>` element:

```tsx
            {/* PUSH INSTALL, TICKED OR BY SUBNET (design 2026-09-30 §1.1).
                Offered to a holder of `agent.push.run`; cosmetic, because the
                server checks that code and everything else on every request. */}
            {canPush ? (
              <>
                <Btn label="Push install the agent…" icon="play" onClick={() => setPushing(true)} />
                <Link className="btn" to="/devices/push-runs">
                  Push runs
                </Link>
              </>
            ) : null}
```

(c) Replace the flow mount:

```tsx
      {pushing && selected.length > 0 ? (
        <PushInstallFlow
          machines={selected}
          onRemove={unselect}
          onClose={() => setPushing(false)}
        />
      ) : null}
```

with:

```tsx
      {pushing ? (
        <PushInstallChooser
          machines={selected}
          onRemove={unselect}
          onClose={() => setPushing(false)}
        />
      ) : null}
```

- [ ] **Step 6: Run the tests**

Run: `cd frontend && npx vitest run src/modules/agent-push src/modules/devices src/lib/noWireNames.test.ts && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/modules/agent-push/PushBySubnetFlow.tsx frontend/src/modules/agent-push/PushInstallChooser.tsx frontend/src/modules/agent-push/__tests__/pushBySubnet.test.tsx frontend/src/modules/devices/DevicesPage.tsx
git commit -m "feat(console): Push install the agent -- By subnet builds a list the operator reads and confirms

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---
### Task 12: Console — Devices → Push runs → the run page

**Files:**
- Create: `frontend/src/modules/agent-push/PushRunsPage.tsx`
- Create: `frontend/src/modules/agent-push/PushRunPage.tsx`
- Modify: `frontend/src/App.tsx` (two routes, declared before `/devices/:deviceId`)
- Modify: `frontend/src/shell/nav.ts` (`NAV_UNLISTED`)
- Modify: `frontend/scripts/routes.mjs` (`PARAMS.runId`, and two `ROUTES` entries)
- Test: `frontend/src/modules/agent-push/__tests__/pushRuns.test.tsx` (create)

**Interfaces:**
- Consumes: the Task 10 hooks and `runState` (including `mayResume` and `mayCancel`), plus `usePushPermissions`.
- Produces: the routes `/devices/push-runs` and `/devices/push-runs/:runId`, rendered by `PushRunsPage` and `PushRunPage`.
  - The run page shows `stopped_credential` with its reason: how many sign-in failures, and what to do.
  - It offers Resume and Cancel, but not Pause, on a stopped run.
  - It says when a running run is still going one machine at a time.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/modules/agent-push/__tests__/pushRuns.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { PushRunPage } from "../PushRunPage";
import { PushRunsPage } from "../PushRunsPage";
import type { PushRunOut, PushRunState, PushRunTargetOut } from "../types";

// The run page is where an operator watches a credential being presented to up
// to 1,024 machines. It must say where the run stands, what happened to each
// machine and why, and offer exactly the controls the run's state allows.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function run(state: PushRunState = "running", over: Partial<PushRunOut> = {}): PushRunOut {
  return {
    id: "run-1",
    prefix: "10.20.4.0/24",
    credential_id: "c1",
    credential_name: "Push install · rack B4",
    created_by: "u1",
    created_at: "2026-09-30T09:00:00Z",
    state,
    target_count: 4,
    concurrency: 50,
    list_digest: "ab".repeat(32),
    finished_at: null,
    counts: { waiting: 1, in_flight: 1, installed: 1, failed: 0, refused: 1, cancelled: 0 },
    stopped_at: state === "stopped_credential" ? "2026-09-30T09:05:00Z" : null,
    auth_failures: state === "stopped_credential" ? 3 : 0,
    auth_failure_limit: 3,
    signed_in: state !== "stopped_credential",
    ...over,
  };
}

const TARGETS: PushRunTargetOut[] = [
  { position: 0, device_id: "d1", address: "10.20.4.12", state: "installed", job_id: "j1", outcome: "installed", detail: "", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
  { position: 1, device_id: "d2", address: "10.20.4.13", state: "refused", job_id: null, outcome: "", detail: "", reasons: ["already_enrolled"], updated_at: "2026-09-30T09:01:00Z" },
  { position: 2, device_id: "d3", address: "10.20.4.14", state: "in_flight", job_id: "j3", outcome: "", detail: "", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
  { position: 3, device_id: "d4", address: "10.20.4.15", state: "waiting", job_id: null, outcome: "", detail: "", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
];

function stubApi(state: PushRunState = "running") {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: ["agent.push.run"] }),
    listPushRuns: vi.fn().mockResolvedValue([run(state)]),
    getPushRun: vi.fn().mockResolvedValue(run(state)),
    listPushRunTargets: vi.fn().mockResolvedValue({ total: TARGETS.length, items: TARGETS, reason_help: {} }),
    pausePushRun: vi.fn().mockResolvedValue(run("paused")),
    resumePushRun: vi.fn().mockResolvedValue(run("running")),
    cancelPushRun: vi.fn().mockResolvedValue(run("cancelled")),
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/devices/push-runs" element={<PushRunsPage />} />
          <Route path="/devices/push-runs/:runId" element={<PushRunPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => mockUseApi.mockReset());

describe("Push runs", () => {
  it("lists each run with its subnet, its state and where it stands", async () => {
    stubApi();
    renderAt("/devices/push-runs");
    const table = await screen.findByRole("table", { name: "Push runs" });
    expect(within(table).getByRole("link", { name: "10.20.4.0/24" }).getAttribute("href")).toBe("/devices/push-runs/run-1");
    expect(within(table).getByText("Running")).toBeTruthy();
    expect(within(table).getByText("1 installed · 0 failed · 1 refused · 2 to go")).toBeTruthy();
  });

  it("shows progress, every machine on the page, and why a machine was refused", async () => {
    stubApi();
    renderAt("/devices/push-runs/run-1");
    expect(await screen.findByRole("heading", { name: "Push run on 10.20.4.0/24" })).toBeTruthy();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("50");
    const table = await screen.findByRole("table", { name: "Machines in this run" });
    for (const t of TARGETS) expect(within(table).getByText(t.address)).toBeTruthy();
    expect(within(table).getByText("It is running the agent now.")).toBeTruthy();
    expect(within(table).queryByText("already_enrolled")).toBeNull();
  });

  it("offers Pause and Cancel while running, and Cancel asks first", async () => {
    const user = userEvent.setup();
    const api = stubApi("running");
    renderAt("/devices/push-runs/run-1");
    await user.click(await screen.findByRole("button", { name: "Pause" }));
    expect(api.pausePushRun).toHaveBeenCalledWith("run-1");
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Cancel run" }));
    expect(api.cancelPushRun).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: "Cancel the run" }));
    expect(api.cancelPushRun).toHaveBeenCalledWith("run-1");
  });

  it("offers Resume while paused", async () => {
    const user = userEvent.setup();
    const api = stubApi("paused");
    renderAt("/devices/push-runs/run-1");
    await user.click(await screen.findByRole("button", { name: "Resume" }));
    expect(api.resumePushRun).toHaveBeenCalledWith("run-1");
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
  });

  it("shows a run the lockout guard stopped, why, and offers Resume and Cancel but not Pause", async () => {
    const user = userEvent.setup();
    const api = stubApi("stopped_credential");
    renderAt("/devices/push-runs/run-1");
    expect(await screen.findByText(/refused by 3 machines/)).toBeTruthy();
    expect(screen.getByText(/starts again with one machine/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Resume" }));
    expect(api.resumePushRun).toHaveBeenCalledWith("run-1");
  });

  it("says a run is going one machine at a time until a sign-in succeeds", async () => {
    const api = stubApi("running");
    api.getPushRun.mockResolvedValue(run("running", { signed_in: false }));
    renderAt("/devices/push-runs/run-1");
    expect(await screen.findByText(/One machine at a time until a sign-in succeeds/)).toBeTruthy();
  });

  it("offers nothing once cancelled, and says a cancelled run is final", async () => {
    stubApi("cancelled");
    renderAt("/devices/push-runs/run-1");
    expect(await screen.findByText(/A cancelled run cannot be resumed/)).toBeTruthy();
    for (const name of ["Pause", "Resume", "Cancel run"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && npx vitest run src/modules/agent-push/__tests__/pushRuns.test.tsx`
Expected: FAIL with `Failed to resolve import "../PushRunPage"`.

- [ ] **Step 3: Write `PushRunsPage.tsx`**

Create `frontend/src/modules/agent-push/PushRunsPage.tsx`:

```tsx
// Devices → Push runs (design 2026-09-30 §1.7): every subnet push a person
// confirmed, newest first, with where each stands.

import { Link } from "react-router-dom";

import { Td, Th } from "../../components/ui";
import { PageHead, Panel, Progress, QueryState, StatusPill, TableWrap } from "../../shell/components";
import { usePushRuns } from "./api";
import { RUN_STATE_KIND, RUN_STATE_LABELS, countsSentence, runProgress } from "./runState";

export function PushRunsPage() {
  const runs = usePushRuns();
  const items = runs.data ?? [];
  return (
    <>
      <PageHead
        title="Push runs"
        description="Subnet pushes someone confirmed, installed at most 50 machines at a time."
        actions={
          <Link className="btn" to="/devices">
            Back to Devices
          </Link>
        }
      />
      <Panel bare>
        <QueryState
          loading={runs.isLoading}
          error={runs.error}
          empty={items.length === 0}
          emptyLabel="No subnet push has been started."
        >
          <TableWrap>
            <table aria-label="Push runs">
              <thead>
                <tr>
                  <Th>Subnet</Th>
                  <Th>Started</Th>
                  <Th>State</Th>
                  <Th>Progress</Th>
                  <Th>So far</Th>
                </tr>
              </thead>
              <tbody>
                {items.map((run) => (
                  <tr key={run.id}>
                    <Td>
                      <Link to={`/devices/push-runs/${run.id}`}>
                        <code>{run.prefix}</code>
                      </Link>
                    </Td>
                    <Td>{new Date(run.created_at).toLocaleString()}</Td>
                    <Td>
                      <StatusPill value={run.state} label={RUN_STATE_LABELS[run.state]} kind={RUN_STATE_KIND[run.state]} />
                    </Td>
                    <Td>
                      <Progress
                        value={runProgress(run.counts, run.target_count)}
                        label={`Progress of the push run on ${run.prefix}`}
                      />
                    </Td>
                    <Td>{countsSentence(run.counts)}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        </QueryState>
      </Panel>
    </>
  );
}
```

- [ ] **Step 4: Write `PushRunPage.tsx`**

Create `frontend/src/modules/agent-push/PushRunPage.tsx`:

```tsx
// One push run (design 2026-09-30 §1.5, §1.7): its progress, what happened to
// each confirmed machine and why, and the controls its state allows. Pause,
// Resume and Cancel are a person's acts; the server checks the permission on
// each, and Cancel is final.

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { Td, Th } from "../../components/ui";
import {
  Btn,
  PageHead,
  Pager,
  Panel,
  Progress,
  QueryState,
  StatusPill,
  TableWrap,
} from "../../shell/components";
import { usePushPermissions, usePushRun, usePushRunControl, usePushRunTargets } from "./api";
import {
  TARGET_STATE_KIND,
  TARGET_STATE_LABELS,
  countsSentence,
  mayCancel,
  mayResume,
  outcomeLabel,
  runIsLive,
  runProgress,
  runReasonLabel,
} from "./runState";
import type { PushRunTargetOut } from "./types";

const PAGE_SIZE = 50;

function whatHappened(target: PushRunTargetOut, help: Record<string, string>): string {
  switch (target.state) {
    case "waiting":
      return "Waiting for a free slot.";
    case "in_flight":
      return "Installing now.";
    case "cancelled":
      return "Not pushed: the run was cancelled.";
    case "refused":
      return target.reasons.map((r) => runReasonLabel(r, help[r])).join(" ");
    default:
      return target.detail ? `${outcomeLabel(target.outcome)}: ${target.detail}` : outcomeLabel(target.outcome);
  }
}

export function PushRunPage() {
  const { runId = "" } = useParams<{ runId: string }>();
  const permissions = usePushPermissions();
  const run = usePushRun(runId);
  const [page, setPage] = useState(0);
  const data = run.data;
  const live = data ? runIsLive(data) : false;
  const targets = usePushRunTargets(runId, page * PAGE_SIZE, PAGE_SIZE, live);
  const control = usePushRunControl();
  const [confirmCancel, setConfirmCancel] = useState(false);
  const total = targets.data?.total ?? 0;

  const actions =
    data && permissions.run ? (
      <>
        {data.state === "running" ? (
          <Btn
            label="Pause"
            disabled={control.isPending}
            onClick={() => control.mutate({ runId, action: "pause" })}
          />
        ) : null}
        {mayResume(data.state) ? (
          <Btn
            label="Resume"
            variant="primary"
            disabled={control.isPending}
            onClick={() => control.mutate({ runId, action: "resume" })}
          />
        ) : null}
        {mayCancel(data.state) ? (
          <Btn label="Cancel run" variant="danger" onClick={() => setConfirmCancel(true)} />
        ) : null}
      </>
    ) : null;

  return (
    <>
      <PageHead
        title={data ? `Push run on ${data.prefix}` : "Push run"}
        description={
          <>
            A list someone confirmed, installed at most {data?.concurrency ?? 50} machines at a time.{" "}
            <Link to="/devices/push-runs">All push runs</Link>
          </>
        }
        actions={actions}
      />
      {control.isError ? (
        <p role="alert" className="form-error">
          {describeSubmitError(control.error)}
        </p>
      ) : null}
      <QueryState loading={run.isLoading} error={run.error} empty={false} emptyLabel="">
        {data ? (
          <>
            <Panel
              title="Progress"
              subtitle={`${data.target_count} machines confirmed with ${data.credential_name || "a credential that no longer exists"}.`}
            >
              <Progress value={runProgress(data.counts, data.target_count)} label="Push run progress" />
              <p className="note mt-2">
                {countsSentence(data.counts)} · {data.counts.in_flight} installing now
              </p>
              {data.state === "running" && !data.signed_in ? (
                <p className="note mt-2">
                  One machine at a time until a sign-in succeeds, so a wrong password cannot lock
                  the account out across the subnet.
                </p>
              ) : null}
              {data.state === "paused" ? (
                <p className="note mt-2">
                  Paused: nothing new is pushed until someone resumes it. Installs already in
                  progress finish.
                </p>
              ) : null}
              {data.state === "stopped_credential" ? (
                <div className="notice gold mt-2" role="alert">
                  <p>
                    Stopped to protect the credential: it was refused by {data.auth_failures}{" "}
                    machines, so nothing more was presented with it. Installs already in progress
                    finish. Check the account&rsquo;s password and whether it is locked, then
                    Resume: the run starts again with one machine.
                  </p>
                </div>
              ) : null}
              {data.state === "cancelled" ? (
                <p className="note mt-2">
                  This run was cancelled. A cancelled run cannot be resumed; installs that were
                  already in progress finished on their own.
                </p>
              ) : null}
            </Panel>
            <Panel title="Machines" bare>
              <QueryState
                loading={targets.isLoading}
                error={targets.error}
                empty={total === 0}
                emptyLabel="This run has no machines."
              >
                <TableWrap>
                  <table aria-label="Machines in this run">
                    <thead>
                      <tr>
                        <Th>#</Th>
                        <Th>Address</Th>
                        <Th>State</Th>
                        <Th>What happened</Th>
                        <Th label="Job" />
                      </tr>
                    </thead>
                    <tbody>
                      {(targets.data?.items ?? []).map((t) => (
                        <tr key={t.position}>
                          <Td>{t.position + 1}</Td>
                          <Td>
                            <code>{t.address}</code>
                          </Td>
                          <Td>
                            <StatusPill value={t.state} label={TARGET_STATE_LABELS[t.state]} kind={TARGET_STATE_KIND[t.state]} />
                          </Td>
                          <Td>{whatHappened(t, targets.data?.reason_help ?? {})}</Td>
                          <Td>
                            {t.job_id ? (
                              <Link to={`/jobs/${t.job_id}`} aria-label={`Job for ${t.address}`}>
                                Job
                              </Link>
                            ) : null}
                          </Td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableWrap>
                <Pager
                  page={page}
                  pageCount={Math.ceil(total / PAGE_SIZE)}
                  onPrevious={() => setPage((p) => Math.max(0, p - 1))}
                  onNext={() => setPage((p) => p + 1)}
                  scopeName="this run's machines"
                />
              </QueryState>
            </Panel>
            <ConfirmDialog
              open={confirmCancel}
              title="Cancel this push run"
              body="Machines still waiting will not be pushed to. Installs already in progress finish. A cancelled run cannot be resumed."
              subject={data.prefix}
              confirmLabel="Cancel the run"
              destructive
              onConfirm={() => {
                setConfirmCancel(false);
                control.mutate({ runId, action: "cancel" });
              }}
              onCancel={() => setConfirmCancel(false)}
            />
          </>
        ) : null}
      </QueryState>
    </>
  );
}
```

- [ ] **Step 5: Mount the routes, the crumb and the harmony entries**

(a) In `frontend/src/App.tsx`, add these imports beside the other agent-push imports:

```tsx
import { PushRunPage } from "./modules/agent-push/PushRunPage";
import { PushRunsPage } from "./modules/agent-push/PushRunsPage";
```

and add these routes directly after the `/devices/onboard` route and before `/devices/:deviceId`:

```tsx
            {/* Push runs (design 2026-09-30 §1.7). Static segments, so React
                Router ranks them above `/devices/:deviceId` regardless. */}
            <Route
              path="/devices/push-runs"
              element={
                <RequireAuth>
                  <PushRunsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/devices/push-runs/:runId"
              element={
                <RequireAuth>
                  <PushRunPage />
                </RequireAuth>
              }
            />
```

(b) In `frontend/src/shell/nav.ts`, add this to `NAV_UNLISTED`, directly after the `/devices/onboard` entry:

```ts
  // Subnet push runs (design 2026-09-30 §1.7): reached from Devices.
  { to: "/devices/push-runs", label: "Push runs", under: "/devices" },
```

(c) In `frontend/scripts/routes.mjs`, add `runId: "run-1",` to `PARAMS`, and add these two entries to `ROUTES` directly after `"/devices/onboard",`:

```js
  "/devices/push-runs",
  "/devices/push-runs/run-1",
```

- [ ] **Step 6: Run the tests, the build and harmony**

Run: `cd frontend && npx vitest run && npx tsc --noEmit && npm run build`
Expected: PASS. `src/test/routes-covered.test.ts` agrees with `App.tsx`; `nav.test.ts` finds `/devices` for the new entry's `under`; `noWireNames.test.ts` finds no wire name in any new copy.

Run: `cd frontend && node scripts/harmony.mjs --routes /devices,/devices/push-runs,/devices/push-runs/run-1 --strict-fixtures`
Expected: exit 0, with no stray token, no horizontal overflow, no serious or critical axe violation, and no fixture miss.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/modules/agent-push/PushRunsPage.tsx frontend/src/modules/agent-push/PushRunPage.tsx frontend/src/modules/agent-push/__tests__/pushRuns.test.tsx frontend/src/App.tsx frontend/src/shell/nav.ts frontend/scripts/routes.mjs
git commit -m "feat(console): Devices -> Push runs and the run page, with Pause, Resume and a confirmed Cancel

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 13: Docs pointer, full verification, and the operator's deploy sequence

**Files:**
- Modify: `docs/modules/agent-distribution/spec-push-install.md` (§5.2, one paragraph appended)
- No code.

**Interfaces:** none.

- [ ] **Step 1: Point the push spec at the run design**

In `docs/modules/agent-distribution/spec-push-install.md`, append this paragraph at the end of §5.2, after the paragraph beginning "Validation is **all-or-nothing, execution is per-target**":

```markdown
**Since 2026-09-30, a subnet may build a list; it is never the thing confirmed.**
CLAUDE.md 2a(e) and 2a(f) carry dated notes, approved with
`docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md`: an
authorized subnet expands to the vouched-for machines it holds, a person
confirms that named list (bound by a SHA-256 digest), and a push run installs
at most 1,024 of them, re-checking every bound above before each one. A run
sends one machine first and at most 50 in flight only after a sign-in has got
through; three sign-in failures since it last started or resumed stop it as
`stopped_credential` until a person resumes it (design §1.4a). A push that is
not a run keeps the ceiling of 25 described here. §5.3's per-credential
circuit breaker (`PUSH_AUTH_FAILURES_MAX`) is still unbuilt.
```

- [ ] **Step 2: Run the full verification**

From `backend/`:

```bash
.venv/bin/ruff check app tests
.venv/bin/mypy app
.venv/bin/python -m pytest -q tests/test_push_run_migration.py tests/test_push_run_dispatcher.py \
  tests/test_push_run_list.py tests/test_push_run_lifecycle.py tests/test_push_run_pacer.py \
  tests/test_push_run_tick.py tests/test_push_run_api.py tests/test_agent_push_api.py \
  tests/test_agent_push_dispatch.py tests/test_agent_push_winrm_dispatch.py \
  tests/test_agent_push_dispatches_nothing.py tests/test_agent_push_preflight.py \
  tests/test_notification_delivery.py tests/test_events_outbox.py tests/test_worker_beat_expiry.py \
  tests/test_worker_sweep_containment.py tests/test_schema_drift.py tests/test_rls.py \
  tests/test_migrations.py tests/test_route_session_discipline.py tests/test_route_gating.py
.venv/bin/python -m pytest -q
```

Expected: every command exits 0.

Then run the migration round trip exactly as `.github/workflows/ci.yml`'s `migrations` job does, from `backend/`, on a host with a local PostgreSQL:

```bash
sudo -u postgres psql -X -q -c "DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='its_admin') THEN
    CREATE ROLE its_admin LOGIN PASSWORD 'its_admin_test' NOSUPERUSER CREATEDB CREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='app_user') THEN
    CREATE ROLE app_user LOGIN PASSWORD 'app' NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='portal_user') THEN
    CREATE ROLE portal_user LOGIN PASSWORD 'portal' NOSUPERUSER NOBYPASSRLS;
  END IF;
END \$\$;"
sudo -u postgres psql -X -q -c "ALTER ROLE its_admin BYPASSRLS"
sudo -u postgres psql -X -q -c "DROP DATABASE IF EXISTS rt_ref"
sudo -u postgres psql -X -q -c "DROP DATABASE IF EXISTS rt_probe"
sudo -u postgres psql -X -q -c "CREATE DATABASE rt_ref   OWNER its_admin"
sudo -u postgres psql -X -q -c "CREATE DATABASE rt_probe OWNER its_admin"
ITS_ALEMBIC_URL="postgresql+asyncpg://its_admin:its_admin_test@127.0.0.1:5432/rt_ref" .venv/bin/alembic upgrade head
sudo -u postgres pg_dump --schema-only --no-owner --no-privileges -d rt_ref > /tmp/ref.sql
export ITS_ALEMBIC_URL="postgresql+asyncpg://its_admin:its_admin_test@127.0.0.1:5432/rt_probe"
.venv/bin/alembic upgrade head
.venv/bin/alembic downgrade base
sudo -u postgres psql -X -A -t -d rt_probe -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name <> 'alembic_version'"
sudo -u postgres psql -X -A -t -d rt_probe -c "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname='public' AND t.typtype='e'"
sudo -u postgres psql -X -A -t -d rt_probe -c "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname='public' AND p.proname='push_run_target_fixed'"
.venv/bin/alembic upgrade head
sudo -u postgres pg_dump --schema-only --no-owner --no-privileges -d rt_probe > /tmp/rt.sql
norm() { grep -v '^\\restrict' "$1" | grep -v '^\\unrestrict'; }
norm /tmp/ref.sql > /tmp/ref.norm
norm /tmp/rt.sql > /tmp/rt.norm
diff -u /tmp/ref.norm /tmp/rt.norm
```

Expected:
- the three counts after `downgrade base` print `0`, `0` and `0`. The third count shows that the trigger function is dropped too;
- `diff` prints nothing.

From `frontend/`:

```bash
npx tsc --noEmit
npx vitest run
npm run build
node scripts/harmony.mjs --routes /devices,/devices/push-runs,/devices/push-runs/run-1 --strict-fixtures
```

Expected: every command exits 0.

- [ ] **Step 3: Confirm that no agent or wire change was made**

Run from the repository root: `git diff --stat "$(git merge-base HEAD origin/main)" -- agent docs/kernel/wire-protocol`
Expected: empty output. The run reuses `agent.push_install` unchanged.

- [ ] **Step 4: Commit the docs pointer**

```bash
git add docs/modules/agent-distribution/spec-push-install.md
git commit -m "docs(push): point §5.2 at the 2026-09-30 run design and its lockout guard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

- [ ] **Step 5: Write the operator's deploy sequence into the final report**

The report ends with this sequence, verbatim. The operator deploys from `main` only (CLAUDE.md, "Reporting to the operator").

What this change carries:
- **Migration:** one, `0200`.
- **`.env` keys:** none new. `ITS_PUBLIC_CONSOLE_URL` is already required for any install push.
- **Beat:** one new entry, `push-run-tick`. The worker is recreated by `up -d`, because `IT_SUITE_VERSION` changes.
- **Agent:** no release is needed.
- **OpenBao:** nothing changes in OpenBao, but the unseal check is still step 2, because a restart or reboot seals it whatever changed.

On the workstation where this branch was built, merge it to `main` first:

```sh
git push -u origin HEAD
gh pr create --base main --fill
gh pr merge --merge --delete-branch
```

On the deploy host:

```sh
# 1. Where the deployment is, and a backup. PREV_VERSION is kept in this shell
#    for the rollback line at the end.
cd /opt/it-suite
PREV_VERSION=$(sed -n 's/^IT_SUITE_VERSION=//p' deploy/.env)
echo "running: $PREV_VERSION"
cd deploy && ./scripts/backup.sh ~/backups/pre-upgrade-$(date -u +%FT%H%MZ) && cd ..

# 2. OpenBao must be unsealed, or `up -d` blocks. This change did not touch it;
#    a restart or reboot seals it whatever changed. If this prints
#    "Sealed true", unseal per deploy/GO-LIVE.md §4 before going on.
cd /opt/it-suite/deploy
docker compose exec openbao bao status

# 3. Pull main.
cd /opt/it-suite
git checkout main
git pull --ff-only

# 4. Build images tagged with the new commit and select that tag. The old key
#    is deleted before the new one is appended, so only one line exists.
cd /opt/it-suite/deploy
./scripts/build-images.sh
NEW_VERSION=$(git rev-parse --short HEAD)
sed -i '/^IT_SUITE_VERSION=/d' .env
echo "IT_SUITE_VERSION=$NEW_VERSION" >> .env
grep -c '^IT_SUITE_VERSION=' .env          # prints 1

# 5. Migrate before restarting the app. This change adds 0200 (push_run,
#    push_run_target and the sealed-list trigger).
docker compose up migrate
docker compose up postgres-grants          # idempotent; covers the two new tables

# 6. Idempotent init jobs. Both are no-ops for this change, run as always.
docker compose up openbao-init
docker compose up keycloak-init

# 7. Restart. This recreates the worker, which is how beat picks up push-run-tick.
docker compose up -d

# 8. Verify.
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
docker compose run --rm migrate alembic current                 # 0200 (head)
docker compose exec worker python -c \
  "from app.worker import create_celery; print(create_celery().conf.beat_schedule['push-run-tick'])"
curl -o /dev/null -w '%{http_code}\n' https://itms.toeshee.com/api/v1/agent-push/runs   # 401
```

After step 8, check in the console:
- Devices shows **Push install the agent…** and **Push runs**.
- Choosing **By subnet** lists only authorized subnets.
- **Push runs** opens an empty list.

**Rollback.** `./scripts/build-images.sh` in step 4 prints `Recent tags:`.
- **If `$PREV_VERSION` is listed there**, then from the same shell, in `/opt/it-suite/deploy`, run:

  ```sh
  sed -i '/^IT_SUITE_VERSION=/d' .env
  echo "IT_SUITE_VERSION=$PREV_VERSION" >> .env
  docker compose up -d
  ```

  `0200` only adds two tables and a trigger that the previous image never reads, so the previous image runs against the migrated schema. A run started before rollback stops advancing, because the old worker has no beat for it, and it resumes when the deploy is re-applied.
- **If `build-images.sh` printed `(none yet)`**, or `$PREV_VERSION` is `dev`, there is no image to roll back to. The only way back is restore-from-backup, from the backup taken in step 1.

---

## Self-Review

**1. Spec coverage (Part 1, §1.1–§1.7 and the Testing section).**

§1.1, what the operator does:
- the **By subnet** choice beside **Ticked machines**: Task 11, `PushInstallChooser`;
- the subnet list is exactly the active authorizations, with none typed: Task 4 `authorized_prefixes`, Task 9 `/runs/prefixes` (R4), Task 11 (a select, no text box, tested);
- the expanded list with readiness, then a confirmation carrying the digest: Tasks 4, 9 and 11;
- a changed list is refused and shown again: Task 5 `PushListChangedError`, Task 9's structured 409, Task 11's refetch;
- the run page with counts, per-machine outcome and Pause/Resume/Cancel: Task 12.

§1.2, expansion:
- the vouched-for allowlist, sweep rows unlisted but counted, no enrolled agent, the approved review state, inside the prefix: Task 4 (R5, R6);
- blocked reasons (not scoped, no site server, no DNS name, no confirmed host key, outside realm, credential kind): Task 4, through `evaluate_targets` and `pre_dispatch_checks`, reused not recopied (Task 3);
- 1,024 ready machines counted, not the prefix size: Task 4, plus the R13 cost guard;
- one active run per org: Task 2's index and Task 5's advisory lock and check;
- `PUSH_BATCH_MAX = 25` unchanged: Tasks 4 and 6 tests.

§1.3, the confirmed list is the authority:
- the tables and forced RLS: Task 2;
- the digest over sorted pairs, refused before any write or decrypt: Tasks 4 and 5 (R7 narrows it to the ready pairs);
- a fixed list: Task 2's trigger and Task 6's AST test (R14).

§1.4, pacing:
- `advance_run` with `FOR UPDATE`, at most 50, and the whole ladder before each target: Task 6;
- a refusal is audited `push.refused` with no decrypt: Task 6;
- dispatch through the same `_dispatch_install` (token, `vault.checkout`, signed job to the site server, `push.dispatched` with the run id): Task 3;
- one job at a time as capacity frees: Task 6;
- triggers: the 60-second beat is Task 8, and the result handler is Task 6. The handler settles the target and does not dispatch (R2).

§1.4a, the lockout guard (R18, R19):
- one first, and Resume starts from one again: Task 6 (`_fill`'s ceiling, `auth_window`), Task 5 (Resume restarts the window);
- 3 `auth_failed` results give `stopped_credential`, audited `push.run.stopped` as the system acting for the creator, while installs in flight finish: Task 6;
- the `push.run.stopped` event: Task 7;
- only a person restarts it: Task 5 (Resume from `stopped_credential`), Task 6 (the pacer never leaves it; an AST test pins the only writer), Task 9 (HTTP), Task 12 (the run page shows the state, its reason, and Resume).

§1.5, controls:
- Pause, Resume and Cancel audited, Cancel final: Task 5;
- the pacer cannot resume: Task 6, behavioural and AST tests;
- completion and `push.run.completed`: Tasks 6 and 7;
- a credential deleted or re-scoped mid-run: Task 6 (R9).

§1.6, the amendment: Task 1, verbatim and additive only.

§1.7, console: Tasks 10–12. The permissions are the existing codes: Task 9's route table.

The Testing section, Part 1:
- expansion set, sweep count and blocked reasons: Task 4;
- digest mismatch before any write or decrypt: Tasks 5 and 9;
- one active run and the 1,024 bound: Tasks 2, 4, 5 and 9;
- 50 under concurrent calls: Task 6;
- a deleted credential refuses the rest: Task 6;
- controls audited, Cancel final, the pacer cannot resume: Tasks 5, 6 and 9;
- no path inserts a target: Tasks 2 and 6;
- `PUSH_BATCH_MAX` still binds: Task 6;
- one first, nothing beyond one in flight until a sign-in succeeds: Task 6 (`test_one_target_first_until_a_sign_in_succeeds`, plus the ceiling tests, which now prove a sign-in first);
- 3 `auth_failed` results stop the run as `stopped_credential`, and only a person's Resume restarts it, again from one: Task 6 (`test_three_sign_in_failures_stop_the_run_and_only_a_person_restarts_it`, `test_installs_in_flight_finish_after_a_stop_and_do_not_undo_it`, `test_only_the_safety_stop_writes_stopped_credential`), Task 5, Task 9 and Task 12;
- frontend dialog, run page and harmony: Tasks 11, 12 and 13.

**2. Placeholder scan.** No "TBD", "similar to Task N" or step without code. The deploy sequence computes every value (`PREV_VERSION`, `NEW_VERSION`) instead of asking for one to be substituted.

**3. Type consistency.** Names and signatures used across tasks:
- `pre_dispatch_checks(session, org_id, *, decisions, credential, kind, public_console_url) -> InstallChecks`: Tasks 3 and 4.
- `RunDispatcher.dispatch_target(session, *, org_id, run_id, on_behalf_of, credential, decision) -> RunTargetDispatch`: Tasks 3, 6, 8 and 9.
- `expand_prefix(..., actor_id, prefix, credential_id, public_console_url, now=None) -> RunList` with `.ready_rows`, `.list_digest`, `.credential_name` and `.unclaimed_count`: Tasks 4, 5 and 9.
- `create_run(..., list_digest, public_console_url)`, and `advance_run(session, org_id, run_id, *, dispatch, now=None) -> list[Envelope]`: Tasks 5, 6, 8 and 9.
- `settle_target_for_job(session, job)`: Task 6.
- `tick` and `org_ids_with_live_runs`: Task 8.
- `RUN_REFUSAL_REASONS` and `LIST_REASONS`: Tasks 4, 6 and 9.
- The count keys `waiting / in_flight / installed / failed / refused / cancelled` are the same in `PUSH_RUN_TARGET_STATES`, `RunCountsOut` and `PushRunCounts`.
- The run states `running / paused / stopped_credential / cancelled / completed` are the same in `PUSH_RUN_STATES`, the migration's CHECK, `RunOut.state` and `PushRunState`. `stopped_credential` is 18 characters, so the column is `varchar(24)`.
- `auth_window(session, run) -> tuple[int, bool]` is used by `_fill`, `_stop_if_locked_out` (Task 6) and `_run_out` (Task 9). `RunOut` and `PushRunOut` share `stopped_at`, `auth_failures`, `auth_failure_limit` and `signed_in`.
- The console method names in Task 10 match the fixtures and the Task 11–12 tests.
