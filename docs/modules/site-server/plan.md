# Site Servers — Implementation Plan (v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make six modules' dangling references to a "site server" real, with the smallest thing that does it: a registry row, one job type that sends a Wake-on-LAN packet, and one ORDER BY term in IPAM's existing sweeper selection.

**Architecture:** A site server **is a `Device`** (spec §0.1) that an operator has promoted by creating one row in a module-owned `site_server` table. That single choice is what makes v1 a module rather than a kernel fork: there is no new job target, no new connection identity, no new certificate profile, and no new dispatch channel. `site.wake` is an ordinary registered job type targeting the site server's own device; the machine being woken is a **parameter**, not a target, because it usually has no `devices` row at all. The sweeper preference is one term in a query that already exists.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Pydantic v2, Alembic, Postgres RLS, React + TypeScript. **No new backend dependency and no new frontend dependency.** `cidr[]` is native Postgres and `ipaddress` is stdlib.

**Nothing in this cycle listens on a socket, and nothing in this cycle is written in Go.**

---

## Global Constraints

Every task's requirements implicitly include this section. These are earned lessons from previous sub-projects of this repo, not general advice.

1. **Authority is `docs/modules/site-server/spec.md`** (written at `95e89f9`; its stale agent-repo references corrected in `9f3111f` and `b837f39`). Where this plan and the spec disagree, the spec wins — **except** at the three places this plan records the spec contradicting itself or contradicting the code (Task 5 on §12.1, Task 8 on §6.1-vs-§6.2, Task 9 on §8.2's schedule). Each is argued in its task; do not silently pick either side.

2. **Trust reality over the spec and over this plan.** If a signature, a table name, a constraint or a behaviour differs from what is written here, **the code wins, and you say so in your report** rather than implementing the brief. This plan was written against the code at `b837f39`; other agents are working concurrently and the code has already moved twice during its writing.

3. **Fixture blindness is this project's defining defect.** A guard test must make the guarded and the unguarded path produce **different observable values**. Three traps, each of which has shipped here:
   - a mutation test whose only evidence is `DID NOT RAISE` — assert *what was stored*, not that nothing exploded;
   - a parameter held constant across every fixture, so "it picked the right device" and "it picked the only device" are the same answer;
   - an empty answer that the function's default already produces — `[]`, `{}`, `None` and `0` are satisfied by a function that never ran.
   **Every task below that adds a guard states what the test would observe if the guard were deleted. Implement that observation, not a weaker one.**

4. **RLS masks application predicates.** A cross-org test driven through `org_session` passes even when the service's own `org_id` predicate has been deleted, because RLS rejects first. To pin an application-layer predicate you must exercise it on the **admin engine**, where RLS is not the thing doing the rejecting. This defect shipped four times in the SLA sub-project. Every service in this module carries an explicit `org_id` predicate **in addition to** RLS, and at least one test per service pins it on the admin engine.

5. **For an `ALL`-command RLS policy, Postgres reuses `USING` as `WITH CHECK` when the latter is omitted** — so deleting the `WITH CHECK` clause falsifies nothing. Write it explicitly anyway (it matches `0002_rls.py` and `0043_ipam.py`), but the mutations that actually falsify a tenancy test are **`WITH CHECK (true)`** for an insert test and **`USING (true)`** for a read test. Wherever a step says "watch it go red", use those two.

6. **Touching `app/core/events.py`, `app/core/rbac.py` or `app/models/__init__.py` requires running the FULL backend suite**, not just this module's tests. This module touches **`rbac.py` and `models/__init__.py`** (Task 1). It must **not** touch `events.py`: this module publishes **no domain events** — it writes audit rows only (spec §10.3). Adding an `EVENT_TYPES` entry drags in an email renderer in `app/modules/notification/delivery.py` and a pinned wire-format sample in `backend/tests/test_notification_delivery.py`, and turns two set-equality tests red. If a task appears to need a domain event, that task is wrong.

7. **A guard must match definitions, not mentions.** A test that forbids a string anywhere in a package goes red on the docstrings that describe the boundary — including the docstring explaining why the test is green. Every structural guard in this plan parses with `ast`, blanks docstrings, and `ast.unparse`s before matching. **`backend/tests/test_ipam_sweep_dispatch.py::_code_of` already does exactly that — copy it, do not re-derive it.**

8. **Nothing in this module reaches a device except through `JobEngine.create()` → `publish_job()`**, and **the publish happens only after the tenant transaction commits.** `app/modules/ipam/sweep.py`'s module docstring states the reason: a job published before the caller's transaction commits could be handed to an agent describing a run that then rolled back. `create_sweep` returns the envelope and the caller publishes; `wake_via_site_server` does the same.

9. **Nothing in this module may depend on `agents.capabilities`.** That column is written by **nothing** — `DeviceFacts` has five fields and none is a capability, `EnrollRequest` adds nothing, and no agent→server message kind carries them (spec §13.1, verified again at `b837f39`). Capability advertisement is not a degraded path, it is a dead one. **Refusal** (a real `JobStatus`, with IPAM's seven-day cooldown) is the working mechanism. `docs/modules/agent/plan.md` Task 5 proposes adding advertisement as a strictly additive optional field; that is a future, and no task here may be built on it.

10. **Nothing in this cycle binds a listening socket.** The cache (spec §7) and PXE (spec §8) are specified in full and **not built**, because both need `CLAUDE.md` invariant #2 amended — the wording is drafted in §4.4 with four testable properties, and it has not been accepted. **Do not build them, do not stub them, and do not adopt the amendment as a side effect of building v1.** If a task appears to need something to listen, it is the deferred slice and it is wrong. Task 11 states what the seam is and — just as important — what it is not.

11. **No Go in this cycle.** `site.wake`'s agent half is unbuilt (spec §5.4). The deliverable is the exported schema, not the handler. `docs/modules/agent/plan.md` does not currently plan `site.wake` at all; Task 11 records that as an obligation, and does not discharge it. **Do not report the idle budget as verified** — `CLAUDE.md` requires a benchmark that runs plus a stated falsifier, and this capability has the falsifiers and not the benchmark.

12. **Migrations are forward-only; never edit a shipped migration. Do not hardcode a migration number from this plan or from the spec.** Committed head at writing was `0046` (`0046_approved_action_fuse_ceilings.py`) and other work is in flight. Before the one migration in this plan, run `cd backend && .venv/bin/alembic heads` and `ls migrations/versions/`, take the next free number, and set `down_revision` to the bare short string of the actual head (e.g. `"0047"`), matching every other migration here.

13. **`pgrep -f "<pattern>"` matches the polling shell's own command line** and will spin forever waiting for a process that is the poll. If you must check for a process, make **one foreground call redirected to a file** and read the file. Do not background a long test run and poll for it.

14. **Resync the index before staging.** Other agents commit into this branch while you work. Before every commit: `git reset` (mixed, **no pathspec**), then `git add <the exact paths this task owns>` and `git commit`. Never `git add -A`, never `git commit -a`, never stage a path a task does not name — `agent/`, `docs/modules/reporting/`, and other in-flight work are sitting untracked in this tree right now.

15. **Tooling.** `pytest` is at `backend/.venv/bin/pytest`; bare `pytest` and `python -m pytest` are not installed. Use `cd backend && .venv/bin/pytest tests/... -v`, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`, `.venv/bin/alembic ...`. Frontend: `npx tsc --noEmit` is the only static check (`npm run lint` does not exist); `npm test` for the suite.

16. **Measure the baseline before you start.** Run the full backend suite once and record the pass count. Do **not** trust a number written in this document or in a sibling plan. Do not regress it.

17. **Postgres and Redis go down in this container during long runs.** Check `pg_isready` and `redis-cli ping` first, and again if anything hangs; restart them yourself (`service postgresql start`, `redis-server --daemonize yes`) and carry on.

18. **Naming and shape facts, verified against the code — do not guess.**
    - Kernel tables are plural: `devices`, `agents`, `jobs`, `users`, `organizations`, `audit_events`, `permissions`, `roles`, `role_permissions`. IPAM's tables are singular: `ipam_site`, `ipam_subnet`, `ipam_observation`, `ipam_vrf`. This module's table is **`site_server`**, singular, matching IPAM.
    - `permissions` is `(id, code, module, action)` — there is **no** `resource` and **no** `description` column. Descriptions live only in `app/core/rbac.py::PERMISSIONS`.
    - `AuditEvent`'s metadata attribute is **`meta`** (mapped to column `metadata`).
    - Tenancy GUC is **`app.current_org`**. There is no `app.org_id`.
    - Seeded global roles are `super-admin`, `org-admin`, `technician`, `auditor`, `read-only` (`0043_ipam.py::ROLE_GRANTS`).
    - `JobEngine.create` hard-sets `target_type="device"` (`app/jobs/engine.py:86`) and `jobs.target_type` is `varchar(10)` (`0001_core_schema.py:153`).
    - FastAPI 0.139.2: `app.routes` **is** `app.router.routes`, and **neither contains `include_router`'d routes** — only a `_IncludedRouter` with `path=None`. A route-completeness test must flatten via `_IncludedRouter.original_router` + `include_context.prefix`. `backend/tests/test_email_intake_api.py` has a working flattener — copy it.
    - Tests use `api_env` with `asyncio_mode=auto` — **do not** write `@pytest.mark.asyncio`. `tests/conftest.py` provides `admin_engine`, `app_engine`, `api_env`; `tests/ipam_fixtures.py` provides `seed_org`, `seed_device`, `seed_agent`, `seed_subnet`, `seed_vrf`, `upsert_observation_row` — Task 8 reuses them rather than writing a second set.
    - Async throughout, full type hints, business logic in services and not in route handlers, `mypy` and `ruff` clean.

---

## Verified signatures you will call

Confirmed against the code at `b837f39`. Re-verify before use (constraint 2).

```python
# app/jobs/engine.py
JobEngine.create(session, *, org_id: UUID, type: str, target_device_id: UUID,
                 params: dict[str, Any], requested_by: UUID | None = None,
                 ttl_s: int | None = None) -> Job          # target_type is hard-set to "device"
JobEngine.signed_envelope(job) -> dict[str, Any]
# app/jobs/registry.py
register(JobType(name: str, params_model: type[BaseModel], result_handler: ResultHandler | None))
registered_names() -> list[str]
# app/gateway/dispatch.py
publish_job(redis, device_id: UUID, envelope: dict[str, Any]) -> None
# app/core/audit.py
audit_service.record(session, *, org_id, actor_type: Literal["user","agent","system"],
                     actor_id: UUID | None, action: str, target_type: str = "",
                     target_id: str = "", metadata: dict[str, Any] | None = None) -> AuditEvent
# app/core/rbac.py
has_permission(session, user_id: UUID, code: str, *, device_group_id: UUID | None = None) -> bool
# app/modules/ipam/sweep.py — the function Task 8 changes by one ORDER BY term
select_sweeper(session, org_id: UUID, subnet: IpamSubnet, *, now: datetime | None = None) -> UUID | None
# app/modules/provisioning/dispatch.py — the function Task 9 changes
start_provision_run(session, object_store, vault, signer, *, org_id, profile_id,
                    target_ref: str | None, now, actor_id, actor_type="user") -> ProvisionRun
```

`ResultHandler` is `(AsyncSession, Job, dict[str, Any]) -> Awaitable[None]`, invoked by `JobEngine.complete` **inside the job's tenant transaction**; a raising handler fails the job and `complete` catches rather than rolling back, so audit rows written before the raise survive (`app/modules/ipam/handler.py`'s precedent).

---

## File structure

**Created (backend):**

| File | Responsibility |
| --- | --- |
| `backend/migrations/versions/00XX_site_server.py` | One table, RLS, three permissions, role grants (number per constraint 12) |
| `backend/app/modules/site_server/__init__.py` | `register(app)` |
| `backend/app/modules/site_server/models.py` | `SiteServer` |
| `backend/app/modules/site_server/errors.py` | `SiteServerValidationError`, `SiteServerNotFoundError`, `SiteWakeRejected` |
| `backend/app/modules/site_server/net.py` | **Pure.** Segment parsing, host-bits validation, broadcast computation. No DB. |
| `backend/app/modules/site_server/registry.py` | Promote / update / demote / list / get + audit |
| `backend/app/modules/site_server/handler.py` | `site.wake` job type + result handler |
| `backend/app/modules/site_server/dispatch.py` | `wake_via_site_server` — **the only file here that may import `JobEngine`** |
| `backend/app/modules/site_server/segments.py` | The declared-vs-observed segment check |
| `backend/app/modules/site_server/schema.py` | `SiteWakeParams`, `SiteWakeResult`, request/response models |
| `backend/app/modules/site_server/api.py` | Routes, each `require_permission`-gated |

**Modified (backend), and each is a shared file — flag every one in your report:** `app/core/rbac.py` (3 codes), `app/models/__init__.py` (1 import), `app/modules/__init__.py` (1 call), `app/gateway/main.py::register_module_job_types` (1 call), `scripts/export_wire_schema.py` (2 models), `app/modules/ipam/sweep.py` (Task 8, one ORDER BY term), `app/modules/provisioning/{schema,dispatch,api}.py` (Task 9).

**Created (frontend):** `frontend/src/modules/site-servers/` — `api.ts`, `types.ts`, `SiteServerList.tsx`, `PromoteDialog.tsx`, `SiteServerDetail.tsx`, `SegmentCheck.tsx`, `WakeAction.tsx`.

**Not created, deliberately:** no `site_server_lease` table, no `site.serve` job type, no `cache_endpoints_for` helper, no listener of any kind, no Go. See Task 11.

---

## Task 1 — Migration: `site_server`, RLS, three permissions, role grants

**Files:**
- Create: `backend/migrations/versions/00XX_site_server.py`, `backend/app/modules/site_server/__init__.py` (empty `register` for now), `backend/app/modules/site_server/models.py`
- Modify: `backend/app/core/rbac.py`, `backend/app/models/__init__.py`
- Test: `backend/tests/test_site_server_migration.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SiteServer`; permission codes `site.read` / `site.manage` / `site.wake`; the seed helpers later tasks import — `seed_site_server(admin_engine, org_id, *, device_id, segments, roles=("wake",), enabled=True) -> UUID`.

The table is spec §2.1 exactly. Constraints that must exist:

- `UNIQUE (device_id)` — **not** `(org_id, device_id)`. `devices.id` is already globally unique and the narrower key is the honest one, matching `agents.device_id`.
- `CHECK cardinality(segments) BETWEEN 1 AND 32`.
- `CHECK roles <@ ARRAY['wake','cache','pxe']::text[] AND cardinality(roles) >= 1`. **The CHECK stays permissive about `cache` and `pxe` even though v1 honours neither** — that vocabulary is the whole of the forward seam (Task 11), and keeping it here is what stops the cache slice needing a migration to widen a shipped CHECK. The *refusal* of those roles is a service rule (Task 2), not a constraint.
- Index `ix_site_server_org_enabled (org_id, enabled)`. **No GiST index on `segments`** (spec §16) — a fleet-sized `&&` scan is a handful of rows, and an index nobody's query plan needs is a thing to maintain.
- `ENABLE` + `FORCE ROW LEVEL SECURITY`, `USING (org_id = current_setting('app.current_org')::uuid)` and the same predicate spelled out again as `WITH CHECK`.

`segments` is `cidr[]` and `roles` is `text[]`, both NOT NULL. Native `cidr`, not text: containment (`<<=`, `&&`) is the operation Task 8 needs, and text comparison of prefixes is wrong in ways that pass small tests (IPAM §0.1's reason, and IPAM's `prefix` column is `CIDR` for it).

Add the three codes to `PERMISSIONS` in `app/core/rbac.py` with the descriptions from spec §10.2, and seed them plus the role grants **in the same migration**, following `0043_ipam.py`'s exact insert SQL. `super-admin`/`org-admin` get all three so `test_rbac.py::test_org_admin_has_all_permissions` keeps holding; `technician` gets `site.read` + `site.wake`; `auditor` and `read-only` get `site.read` only.

Declare the index in the model's `__table_args__` **as well as** in the migration: `test_schema_drift.py` has **two** tests, and `test_metadata_indexes_match_live_schema` deparses indexes on both sides.

- [ ] **Step 1: Write the failing tests.** `backend/tests/test_site_server_migration.py`. Cover at minimum: RLS enabled *and* forced; a row is invisible to another org through `org_session`; a row cannot be inserted under another org's id; zero segments rejected; 33 segments rejected and 32 accepted; `roles = '{}'` rejected; `roles = '{relay}'` rejected; `roles = '{cache}'` **accepted at the DB layer** (with a docstring saying the refusal is Task 2's, and why the CHECK is deliberately wider than v1); a second row for one `device_id` rejected; each of the three permission codes granted to `org-admin`; `technician` has `site.wake` but **not** `site.manage` — a negative assertion, because a test that only checks presence passes against a migration that grants everything to everyone.
- [ ] **Step 2: Run to verify failure.** Expected: `ModuleNotFoundError: No module named 'app.modules.site_server'`.
- [ ] **Step 3: Write `models.py`,** mirroring `app/modules/ipam/models.py`'s header style (`Base`, `uuid7_pk`, a local `_utcnow`).
- [ ] **Step 4: Write the migration** at the real head (constraint 12).
- [ ] **Step 5: Run** `tests/test_site_server_migration.py tests/test_schema_drift.py tests/test_rls.py tests/test_rbac.py`. If `test_rbac.py::test_permission_registry_complete` fails, it pins the exact permission-code set and adding the three codes to its literal is the correct fix.
- [ ] **Step 6: Prove the tenancy tests can fail.** Set the policy to `WITH CHECK (true)`, rebuild the test DB, run the insert test — **watch it go red**. Then `USING (true)` and watch the visibility test go red. Restore both, paste both outputs in your report. Deleting the `WITH CHECK` clause entirely proves nothing (constraint 5).
- [ ] **Step 7:** Run the **full backend suite** (constraint 6 — this touched `rbac.py` and `models/__init__.py`). Then `git reset`, stage the six paths, commit.

**Definition of done:** the table exists with all five constraints, RLS is behaviourally pinned with both mutations demonstrated, the permission set is seeded with a negative grant assertion, and the full suite is green.

**Commit:** `feat(site-server): the registry table, RLS, and three permissions`

---

## Task 2 — The registry service: promote, update, demote, read

**Files:** Create `backend/app/modules/site_server/{net.py,errors.py,registry.py}`; Test `backend/tests/test_site_server_net.py`, `backend/tests/test_site_server_registry.py`

**Interfaces:**
- Consumes: `SiteServer` (T1), `audit_service.record`.
- Produces:
  - `net.py` (**pure, no DB, no ORM import**): `parse_segment(value: str) -> IPv4Network` raising `SiteServerValidationError` when host bits are set; `broadcast_for(segment) -> str`; `containing_segment(segments, address) -> IPv4Network | None`; `normalize_mac(value: str) -> str`.
  - `registry.py`: `promote`, `update_site_server`, `demote`, `list_site_servers`, `get_site_server`.

`net.py` is a plain value module so nothing in this task needs a database to be tested exhaustively — and it is cheap to test exhaustively, so do. `parse_segment("10.20.7.4/24")` **raises**; `parse_segment("10.20.7.0/24")` returns. `broadcast_for("10.20.7.0/24") == "10.20.7.255"`; `broadcast_for("10.20.0.0/16") == "10.20.255.255"`; a `/31` and a `/32` are edge cases with a stated answer, not an accident — decide and test both. `containing_segment` returns the **most specific** match when a server declares both `10.20.0.0/16` and `10.20.7.0/24`, and a test with only one segment cannot see that rule.

`promote` validates, in this order, per spec §3.3:

1. the device exists **in this org** — with an explicit `org_id` predicate as well as RLS;
2. it has an `agents` row (a device with no agent can never receive a job);
3. it is not already promoted;
4. every segment parses as a network prefix with host bits zero;
5. **`roles` contains only `wake`.** `cache` and `pxe` are refused with an error naming spec §4.4 and the fact that the amendment has not been accepted. This is the plan reading §2.1's "v1 only `'wake'` is honoured" as *refused*, not as *accepted and ignored*. Record the reading in your report: a stored `roles=['cache']` on a machine with no listener is a row that says a service is running when none is, and the operator has no way to find that out. Refusing costs a later `PATCH`; accepting costs a lie in the registry.

Then writes `site.promoted` to the audit chain with `site_server_id`, `device_id`, `segments`, `roles`.

**Promotion does not check that the device is on the segments it claims** and does not fail if it is not (spec §3.3). That is §0.8's design: a claim earns preference, evidence earns candidacy. The mismatch surfaces on the segment check (Task 7), not as a refusal here. Ship a test that promotes a device with **no** observation at all and asserts it **succeeds** — the negative half of Task 8's rule, and the test that stops a future author "helpfully" adding the check.

`demote` deletes the row and audits `site.demoted`. There is no lease to revoke in v1 — say so in the docstring so the cache slice adds the revocation next to the delete rather than discovering it is missing.

**Guard observability.** Every one of the five validations must be pinned by a fixture where the guarded and unguarded paths differ in a stored value:
- cross-org device: promoting org B's device from org A must be a **not-found**, tested on the **admin engine** so the explicit `org_id` predicate is what rejects — through `org_session` RLS rejects first and the test passes with the predicate deleted (constraint 4);
- no `agents` row: the fixture has **two** devices, one with an agent and one without, and promotes the wrong one — a fixture with a single agent-less device cannot tell "the check works" from "promotion is broken";
- double promotion: assert the `site_server` row **count is 1** afterwards, not that an exception was raised;
- host bits: `10.20.7.4/24` refused **and** `10.20.7.0/24` accepted in the same test, so a function that refuses everything fails;
- roles: `roles=['cache']` refused **and** `roles=['wake']` accepted, likewise.

**Definition of done:** `net.py` has no import from `app.models` or `sqlalchemy`; all five validations are pinned by discriminating fixtures; the cross-org predicate is pinned on the admin engine; every mutating path writes its audit action from spec §10.3.

**Commit:** `feat(site-server): promotion, demotion, and the segment validation rules`

---

## Task 3 — `site.wake`: params, result, the job type, and registration in the two processes that need it

**Files:** Create `backend/app/modules/site_server/{schema.py,handler.py}`; Modify `backend/app/modules/site_server/__init__.py`, `backend/app/modules/__init__.py`, `backend/app/gateway/main.py`, `scripts/export_wire_schema.py`; Test `backend/tests/test_site_server_jobtype.py`

**Interfaces:**
- Produces: `SiteWakeParams`, `SiteWakeResult`, `register_job_types()`, `handle_wake_result`, `WAKE_TTL_S = 300`.

`SiteWakeParams` (`extra="forbid"`, matching every other params model), spec §5.2:

```
target_mac:  str        # canonical lowercase colon form, validated in the model
broadcast:   str        # an explicit ADDRESS, never a CIDR and never "the segment"
port:        int        # 7 or 9, default 9
repeat:      int        # 1..3, default 3
```

`SiteWakeResult`: `{sent: bool, broadcast: str, sent_at: RFC3339}`. **No `woke`, no reachability probe, no retry count** (spec §0.9). A result field that cannot carry true information is worse than a missing one, and a "did it come up?" poll is how this module becomes the monitoring product.

`handle_wake_result` **rejects a result whose `broadcast` differs from the signed params**, raising `SiteWakeRejected`. That rejection is the reason the job type must be registered in the **gateway** process: results complete in `JobEngine.complete`, which looks the handler up by name, and `app/gateway/main.py` never imports `register_all`. A handler registered only in `register(app)` is **dead in production while every in-process test passes** — the hole `register_module_job_types`' own docstring records IPAM falling into. Register from:

- `app/modules/site_server/__init__.py::register(app)`, called by `app/modules/__init__.py::register_all` (API process);
- `app/gateway/main.py::register_module_job_types` (gateway process).

**Not** from `app/worker.py::register_worker_job_types`, and this is a decision with a consequence, not an omission: no worker code path creates a `site.wake` job in v1 (the only creators are the API route in Task 6 and the provisioning route in Task 9, both in the API process). **If a later cycle dispatches a wake from a beat task, the first firing raises `UnknownJobType`.** Write that sentence into `register_worker_job_types`' vicinity in your report, not into the kernel file.

Export `SiteWakeParams` and `SiteWakeResult` to `docs/kernel/wire-protocol/` by adding them to `scripts/export_wire_schema.py` and running it. That export is a **compatibility contract**, not an internal detail, even though both sides now live in one tree (spec §5.1): a deployed agent is older than the control plane, so a change here is additive, or versioned, or it breaks agents in the field, and no test in this repository will tell you which.

Required tests:
- a MAC in any of the accepted input forms normalises to the canonical lowercase colon form, and an invalid MAC is refused — **assert the normalised value**, not that parsing succeeded;
- `port=8` is refused, `port=7` and `port=9` accepted; `repeat=0` and `repeat=4` refused;
- an unknown key is refused (`extra="forbid"`);
- **the gateway registration test**: pop `site.wake` out of `app.jobs.registry._registry`, call `register_module_job_types()`, assert it is back in `registered_names()` — mirroring `test_ipam_sweep_dispatch.py`'s precedent. Then do the same through `ipam.register(FastAPI())`'s analogue for the API path. **Two separate tests**, because one process's registration passing says nothing about the other's — that is the entire lesson;
- `handle_wake_result` with a matching `broadcast` writes nothing and does not raise; with a **differing** `broadcast` raises `SiteWakeRejected`. The discriminating fixture is the pair: the same job, two results differing only in that field, producing different job end states;
- the exported JSON Schema files exist and match the models — regenerate and assert the working tree is clean, so a model change without a re-export goes red.

**Definition of done:** the job type is registered in **both** processes with a test per process; the result handler discriminates on `broadcast`; the wire schemas are exported and pinned.

**Commit:** `feat(site-server): the site.wake job type and its rejecting result handler`

---

## Task 4 — Broadcast resolution and `wake_via_site_server`

**Files:** Create `backend/app/modules/site_server/dispatch.py`; Test `backend/tests/test_site_server_dispatch.py`

**This is the task that puts a packet on somebody else's LAN. Read spec §0.2, §0.9, §0.10 and §5.2 in full before starting.**

**Interfaces:**
- Consumes: `registry.get_site_server`, `net.containing_segment`/`broadcast_for`, `JobEngine.create`, `audit_service.record`.
- Produces: `wake_via_site_server(session, job_engine, *, org_id, site_server_id, target_mac, segment=None, actor_id) -> tuple[Job, dict[str, Any]]` — the job and its signed envelope, **not published**.

The rule that makes this safe is spec §0.2's: **the endpoint never decides what it may touch.** The control plane picks the `site_server.segments` entry containing `segment` (or the sole entry when there is exactly one), computes the broadcast **address**, and puts *that* in the signed params. The agent broadcasts to exactly that address and to nothing else. A MAC with no containing segment is a **refusal before any job is created** — never a guess, never "the first segment", never a CIDR handed down for the agent to resolve. This is the same shape as IPAM's enumerated `targets`, and for the same reason: an authorization revoked in the control plane must not depend on the endpoint re-deriving anything.

`ttl_s=WAKE_TTL_S` (300), **not** the kernel's 3600s default (spec §0.10). A job for an offline device stays `queued` and is drained when that device next connects, so a wake requested at 22:00 and drained at 04:00 broadcasts into a maintenance window that ended hours ago. The constant is a module constant so the dispatcher and the test cannot drift apart, exactly as `SWEEP_TTL_S` is.

`dispatch.py` is **the only file in this module that may import `JobEngine`** — Task 5 asserts that structurally. Nothing here publishes to Redis; the caller publishes after commit (constraint 8).

Audit `site.wake_dispatched` with `job_id`, `target_mac`, `broadcast`, and the segment chosen.

Required tests:
- **the broadcast is server-computed**: a site server declaring `10.20.0.0/16` and `10.20.7.0/24`, woken for a MAC on `10.20.7.0/24`, produces `broadcast == "10.20.7.255"` — the **more specific** segment. With only one segment declared, this test cannot fail (constraint 3);
- a `segment` naming a prefix the server does **not** declare is refused, and **the `jobs` row count is zero** afterwards — assert the count, not the exception;
- `ttl_s == WAKE_TTL_S`: assert `expires_at - issued_at == 300s` **on the stored row**, so a dispatcher that passes the kernel default is observed as 3600;
- the job's `signature` is non-NULL and its payload's `device` field is the **site server's** device id, not the woken MAC's anything;
- the envelope is returned and **nothing is published** by this function — assert the fake Redis received no publish, which is a different observable from "no exception";
- cross-org: `site_server_id` from another org is not found, pinned on the **admin engine**;
- **advertisement is not consulted**: a site server whose `agents.capabilities` is `{}` and one whose capabilities name only `patch.apply` both dispatch. There is no capability gate on this path and there must not be one (constraint 9). The discriminating observation is that **both** produce a job — a test with one fixture cannot tell "no gate" from "a gate that happens to pass".

**Definition of done:** broadcast is computed server-side from the most specific declared segment, an unresolvable MAC creates nothing, the TTL is 300 on the stored row, and the publish is the caller's.

**Commit:** `feat(site-server): server-computed broadcast and the wake dispatch path`

---

## Task 5 — The §12.1 guard: "no kernel change" asserted, not trusted

**Files:** Test only — `backend/tests/test_site_server_kernel_boundary.py`

**No production code. This task's whole output is the assertion that the previous four tasks did not do what every module of this kind is tempted to do.**

Spec §12.1 claims v1 requires **no kernel change**: `Job.target_type` stays `"device"`, `JobEngine.create` is called as-is, `_load_job_for_device` is untouched, identity resolution is untouched, the wire protocol gains no kind. It then says: *"If a builder finds themselves editing any file in `app/jobs/`, `app/gateway/`, or `app/core/` to make this module work, the design has drifted."*

**That sentence, taken literally, is false on day one, and the spec contradicts it two sections earlier.** §5.1 *requires* editing `app/gateway/main.py::register_module_job_types`, and the repo's shipped pattern requires adding three codes to `app/core/rbac.py::PERMISSIONS`. Both are under the named packages. Restate the claim precisely, because a guard built on the literal sentence would be either permanently red or quietly disabled:

> **v1 appends to four registration points and changes no kernel logic.** The four are `app/core/rbac.py::PERMISSIONS`, `app/models/__init__.py`, `app/modules/__init__.py::register_all`, and `app/gateway/main.py::register_module_job_types`. Every one is an existing extension point that every module in this repo appends to. **No other kernel file gains a line, and no kernel file gains a *branch* — no `site_server` case in dispatch, no second identity resolution, no second job-loading path, no widened column.**

Task 11 records that restatement in the spec.

Four assertions, and the map in the first is the point of the task:

**(a) The kernel does not name this module, except at the two allowlisted registration points.** Walk every `.py` under `backend/app/jobs/`, `backend/app/gateway/`, `backend/app/core/`. Parse each, blank every docstring, `ast.unparse` (**copy `_code_of` from `backend/tests/test_ipam_sweep_dispatch.py`** — constraint 7), then count matches of `site_server`, `SiteWake`, and `site\.(read|manage|wake)`. Assert the resulting `{relative_path: count}` map equals the pinned allowlist **exactly** — an equality, never a subset, and never a `>=`.

Expected shape (**pin the counts you actually observe, not the ones this plan guesses**): `app/gateway/main.py` for the import and the call inside `register_module_job_types`, and `app/core/rbac.py` for the three permission codes. Nothing else.

*Guard observability:* add a `target_type == "site_server"` branch to `_load_job_for_device` and the map gains `app/gateway/dispatch.py`, and the assertion message names that file and its count. Docstrings are blanked first, so the paragraph in `register_module_job_types` that *explains* this boundary is not counted as a definition — write this as a raw-text `grep` instead and the test goes red on the sentence describing why it is green.

**(b) `jobs.target_type` is still `varchar(10)`.** Read `information_schema.columns` on the **admin engine**; assert `character_maximum_length == 10`. In the same test assert `len("site_server") == 11`, with a failure message that says what the pair means: the rejected design in spec §0.1 begins by widening a shipped column on the `jobs` table, so a widened column is the earliest observable sign that somebody is building it. *Guard observability:* widen to `varchar(20)` and the test reports the new width by name.

**(c) Every job this module creates is a device job, and it names the right device.** Behavioural, on the admin engine: promote a device, dispatch a wake, read the `jobs` row and assert `target_type == "device"` and `target_id == the promoted device's id`. The fixture contains **at least two** devices — the site server and an ordinary endpoint — so `target_id` naming the site server is evidence and not arithmetic on a one-element set.

**(d) The module's own imports stay inside the boundary.** AST over every module under `app/modules/site_server/`: collect every `ast.Import` / `ast.ImportFrom` whose module starts `app.jobs`, `app.gateway`, `app.core.signing` or `app.core.ca`, and assert the `{file: {modules}}` map equals the pinned allowlist exactly — expected `handler.py → {app.jobs.registry}` and `dispatch.py → {app.jobs.engine}`, and nothing else. `app.core.rbac`, `app.core.audit` and `app.core.tenancy` are ordinary module dependencies and are **not** in the forbidden set. *Guard observability:* a `from app.gateway.dispatch import publish_job` added to `api.py` — the shape of a second egress path — makes the map differ and names the file.

**What this task cannot check, stated so nobody thinks it does:** a test can see *references*, not *authorship*. §12.1's "the module adds no file to `app/jobs/`, `app/gateway/`, `app/core/`" is a review obligation — check `git diff --stat` for new paths under those three trees — and the task records it as one rather than pretending otherwise.

**Definition of done:** four assertions, each with its mutation demonstrated and the output pasted in your report. If any of them is green after its mutation, the assertion is decoration — fix the test, not the assertion.

**Commit:** `test(site-server): assert the kernel is unchanged rather than reviewing for it`

---

## Task 6 — The API

**Files:** Create `backend/app/modules/site_server/api.py`, extend `schema.py`; Modify `backend/app/modules/site_server/__init__.py`, `backend/app/modules/__init__.py`; Test `backend/tests/test_site_server_api.py`

Routes exactly as spec §11 tabulates, under `/api/v1/site-servers`, literal segments before `{id}`, request models `extra="forbid"`:

| Method | Path | Permission |
|---|---|---|
| GET | `/` | `site.read` (filters: `enabled`, `ipam_site_id`, `segment` containment) |
| POST | `/` | `site.manage` |
| GET | `/{id}` | `site.read` (includes agent status and last seen) |
| PATCH | `/{id}` | `site.manage` |
| DELETE | `/{id}` | `site.manage` |
| GET | `/{id}/segment-check` | `site.read` (Task 7 fills the body) |
| POST | `/{id}/wake` | `site.wake` |

Requirements:

- Handlers stay thin: resolve principal, call service, map errors. `SiteServerNotFoundError` → 404, `SiteServerValidationError` → 400. **Let the error escape the `org_session` block** so the transaction rolls back, and map it outside; catching inside leaves partial writes committed.
- **404, never 403**, for another org's site server — it must not confirm existence. Same for a device id from another org on `POST /` (spec §14).
- `POST /{id}/wake` calls `wake_via_site_server` inside the tenant transaction and **publishes the envelope after the block exits**, never inside it (constraint 8).
- RBAC completeness derived from the router via `route_permission` — **use the flattener from `backend/tests/test_email_intake_api.py`** (constraint 18). **Pin the exact route count**; a `>=` assertion hides a missing route.
- `site.manage` and `site.wake` are separate permissions and the API is where that separation becomes real (spec §10.2): a `site.wake`-only principal must be refused `POST /`, and a `site.manage`-only principal must be refused `POST /{id}/wake`. **Both directions**, as two tests — one direction alone passes against a router that gates everything on a single code.

Required tests: one per route for the happy path and the 403; the cross-org 404s; the `site.manage`/`site.wake` pair in both directions; a wake whose MAC resolves to no declared segment is a 400 that creates no job; the envelope reaches the fake Redis **after** the response's transaction committed.

**Guard observability:** remove `require_permission` from exactly one route and the completeness test must go red **naming that route**. **Prove it and paste the output.** If it stays green, the flattener is not reaching `include_router`'d routes — fix the test, not the assertion.

**Definition of done:** all seven routes present, gated and counted; the completeness mutation demonstrated; full backend suite green (this touches `app/modules/__init__.py`).

**Commit:** `feat(site-server): the site-server API`

---

## Task 7 — The segment check

**Files:** Create `backend/app/modules/site_server/segments.py`; Modify `api.py`; Test `backend/tests/test_site_server_segments.py`

**Interfaces:**
- Produces: `segment_check(session, org_id, site_server_id) -> SegmentCheck` with `matched`, `declared_unobserved`, `observed_undeclared`, `evidence: Literal["ipam", "unavailable"]`.

This is the view that makes §3.3's deliberate non-check visible: promotion accepts an operator's claim without verifying it, and this is where the operator finds out the claim is wrong. Declared segments come from `site_server.segments`; observed addresses come from `ipam_observation` rows for this `device_id` with `source='inventory'` (the only source that carries a device identity — a sweep sees addresses and MACs, not identity).

**IPAM may not be installed.** IPAM is a pull-forward module and site servers must work in a deployment that never installs it (spec §2, §11). Detect with **`SELECT to_regclass('ipam_observation') IS NOT NULL`**, not with a `try`/`except ProgrammingError` around a query: a failed statement aborts the enclosing transaction, so the "graceful" fallback would poison every subsequent write in the request. Absence yields `evidence="unavailable"` and empty sets — **explicitly, not silently**, because "no mismatches" and "we could not look" are opposite answers and must not render the same (Task 10 pins the rendering half).

Required tests, with fixtures that discriminate:
- a device observed **inside** a declared segment appears in `matched` and in neither other list;
- a declared segment with no observation inside it appears in `declared_unobserved` — the fixture also declares a **second**, observed segment, so the test observes *which* segment landed where rather than "the list is non-empty";
- an observation outside every declared segment appears in `observed_undeclared`;
- an observation from `source='sweep'` is **ignored** — it has no `device_id` and must not become evidence about this device; the fixture pairs it with an inventory observation so the two produce different results;
- an observation older than IPAM's `observation_ttl_days` is excluded, with a fixture holding one row inside the window and one outside, so the test observes which one survived;
- **the unavailable branch**: factor the presence probe as `_observation_evidence_available(session) -> bool`, test it against the real schema (returns `True`), and test the caller with it forced `False` — asserting the body carries `evidence == "unavailable"` **and** `matched == []` where the same fixture with `True` returns a non-empty `matched`. Two fixtures, two different observable bodies; asserting only the empty list would be satisfied by a function that never ran (constraint 3).
- cross-org: another org's site server is not found, pinned on the admin engine.

**Definition of done:** the three sets are computed from inventory observations only, the unavailable branch is reachable and distinguishable from the empty-and-fine branch, and no code path lets a missing table abort a transaction.

**Commit:** `feat(site-server): declared segments versus what inventory actually saw`

---

## Task 8 — IPAM: one ORDER BY term (and one thing raised, not fixed)

**Files:** Modify `backend/app/modules/ipam/sweep.py`, `docs/modules/ipam/spec.md`; Test `backend/tests/test_ipam_site_server_preference.py`

**`app/modules/ipam/sweep.py` is another module's security-critical file — flag it in your report.** The change is additive and is one clause. Everything else in `select_sweeper` is untouched: the `EXISTS` inventory-observation predicate that defines candidacy, the `NOT EXISTS` refusal-cooldown predicate, `_CANDIDATE_LIMIT`, `_advertises_net_sweep`, the `_designated` short-circuit and the `no_agent` outcome. **There is no second selection function** (spec §0.7) — two selection functions is two answers to "who probed this segment", and they diverge on the first bug fixed in one of them.

### The spec contradicts itself here, and this plan picks the behaviour table

Spec §6.1 writes the new term **first** in the `ORDER BY`. Spec §6.2's fallback table says an offline site server "loses the preference; an ordinary endpoint sweeps", explaining that "`(a.status='online') DESC` is already the second term". **Those cannot both be true.** The `EXISTS` subquery does not consult agent status, so with the site-server term first an **offline** site server outranks an **online** ordinary endpoint, and the sweep is dispatched to a machine that cannot receive it — it sits `queued` until the 600s TTL expires it, and the segment goes unswept for the cycle.

Implement **§6.2's behaviour** and correct §6.1's ordering, for the reason IPAM's own code already establishes: `_designated`'s docstring says designation "is not liveness", and a designated device whose agent is offline **falls through to auto-selection** rather than producing a job addressed to a machine that cannot receive it. Preference is a weaker claim than designation, so it cannot outrank liveness when designation does not.

```sql
ORDER BY (a.status = 'online') DESC,
         (EXISTS (SELECT 1 FROM site_server s
                  WHERE s.org_id = :org AND s.device_id = d.id
                    AND s.enabled
                    AND EXISTS (SELECT 1 FROM unnest(s.segments) seg
                                WHERE CAST(:prefix AS cidr) <<= seg))) DESC,
         a.last_seen_at DESC NULLS LAST,
         d.id
```

Record the correction in `docs/modules/ipam/spec.md` §5.3 alongside the term itself, and in this module's spec §6.1 (Task 11). If a reviewer prefers §6.1's literal ordering, that is a legitimate decision — but it must be taken by editing §6.2's table, not by leaving both in the document.

`<<=` reads "the subnet being swept is contained in a segment this server serves", so a server declared for `10.20.0.0/16` is preferred for `10.20.7.0/24`. Containment, not equality — a branch server serves a branch, not one VLAN.

Required tests (reusing `tests/ipam_fixtures.py`, not a second fixture set):

- **preferred among equals** — two devices, both online, both with a current observation inside the prefix, **identical `last_seen_at`**, and the ids seeded so the *non*-site-server sorts first on `d.id`. Assert the site server is chosen. *If the new term is deleted this fixture returns the other device* — a different observable value, not a missing exception. This is the fixture the whole task rests on; a fixture where the site server wins on `last_seen_at` anyway proves nothing.
- **liveness outranks preference** — an **offline** site server and an **online** ordinary endpoint, both candidates. The ordinary endpoint is chosen. Under §6.1's literal ordering this test is red, which is the point.
- **declared but unobserved is not a candidate** (§0.8) — a promoted device whose only observation is *outside* the prefix, plus one ordinary eligible device. The ordinary device is returned. Preference reorders the candidate set; it never adds to it.
- **refusal applies to site servers too** — two promoted site servers, one that refused a `net.sweep` inside the seven-day cooldown and one outside it, so the test observes *which* was chosen. There is no site-server exemption from `REFUSAL_COOLDOWN_DAYS` and there must not be one.
- **advertisement is not the mechanism** (constraint 9) — a site server with `capabilities = {}` is preferred; a site server whose non-empty capabilities name only `patch.apply` is skipped exactly as any device is. Two fixtures, two different chosen devices.
- **`enabled=false` and demotion** fall back to an ordinary endpoint, as two separate tests.
- **`_designated` still wins** over a site server — the operator's explicit choice outranks an inferred preference.
- **`no_agent` still occurs** when nothing is eligible.
- **containment, not equality** — declared `10.20.0.0/16`, sweeping `10.20.7.0/24`, preferred.
- **cross-org** — a `site_server` row in org B does not influence org A's selection. The subquery carries `s.org_id = :org` explicitly; pin it on the **admin engine**, because through `org_session` RLS rejects first and the test passes with the predicate deleted (constraint 4).
- **determinism** — the same fixture picks the same device across repeated runs.

### Raised, not fixed: `_designated` has no subnet predicate

`sweep.py::_designated` checks that the designated device's agent is online, advertises the capability, and has not recently refused — and **never checks that the device is on the subnet**, though `select_sweeper`'s own docstring gives the safety argument for that check ("a machine that is not on that wire cannot sweep it, and dispatching anyway emits somebody's probe from the wrong place"). IPAM's spec §5.3 step 1 does not state the check either, so it reads as emergent rather than chosen.

**Do not fix it here.** Adding `AND EXISTS (observation inside prefix)` to `_designated` is a **behaviour change to shipped code** — a designation that works today would start falling through to auto-selection — and it is IPAM's call. Write it into `docs/modules/ipam/spec.md` §5.3 as a recorded, unfixed finding with its consequence, and say plainly in your report that this module works either way.

**Definition of done:** one clause added, every other predicate byte-identical, ten discriminating tests, the ordering correction recorded in both specs, and `_designated` untouched.

**Commit:** `feat(ipam): prefer a site server for a sweep of a segment it serves`

---

## Task 9 — Provisioning: `wake_on_lan` stops being dropped, and the discarded signature

**Files:** Modify `backend/app/modules/provisioning/{schema.py,dispatch.py,api.py}`; Test `backend/tests/test_site_server_provisioning_wake.py`, `backend/tests/test_signing_boundary.py`; Modify `backend/tests/test_provisioning_dispatch.py`

**Another module's dispatch path — flag every file in your report.** Two independent defects, both recorded in spec §13, both fixable in v1.

### 9a. `RunStartRequest.wake_on_lan` is accepted and silently discarded

`provisioning/schema.py:121` accepts `wake_on_lan: dict[str, Any] | None`. `start_provision_run` **has no such parameter**, `provisioning/api.py` never passes it, and `dispatch.py:126` hard-codes `wake_on_lan={}` into the prepare envelope. An operator can ask for a wake today and nothing happens, with no error and no log line. `site.wake` is the fix, and spec §12.3.4 marks it as the one piece of the provisioning work v1 can deliver.

Replace the untyped dict with a model (`target_mac`, optional `segment`, `extra="forbid"`), thread it through `start_provision_run`, and call `wake_via_site_server` when it is set. The route publishes the wake envelope after the `org_session` block, next to the run it already returns.

**Two decisions to take deliberately, both of which change behaviour:**

1. **No site server serves the target's segment ⇒ 400, before the run is created.** Starting the run anyway and not waking reproduces exactly the defect being fixed, one layer down. A caller who passes `wake_on_lan` today gets a run and no wake; after this change they may get a 400. That is the correct break — it is the difference between "we ignored you" and "we cannot do that" — and it must be tested in both directions: with a serving site server the run **and** the job exist; without one, **neither** does.
2. **`wake_on_lan` additionally requires `site.wake`**, checked at the service boundary with `has_permission`. The route stays gated on `provisioning.run`. Spec §10.2 separated `site.wake` from `site.manage` on purpose; letting `provisioning.run` confer LAN-wide wake for free collapses a separation the spec chose. In practice `technician` holds both, so no seeded role loses anything — the test that matters is a principal with `provisioning.run` and **not** `site.wake`, who gets a run when `wake_on_lan` is absent and a 403 when it is present. Different observable outcomes on the same route.

**`ProvisionPrepareEnvelope.wake_on_lan` stays as it is.** Retiring that envelope is spec §8.2's PXE-slice work; this task only stops the API-level drop. Removing a field from an exported wire schema is a compatibility decision (constraint 8's sibling rule) and does not belong in a task about a dropped request field.

### 9b. `provision.prepare`'s signature is computed and thrown away — decision: fix it here, by deletion

`provisioning/dispatch.py:131` is `await signer.sign(canonical_bytes(envelope.model_dump()))`. The result is not assigned, not persisted on the run, not returned and not published, because no consumer exists.

Three facts decide this:

1. **`await ....sign(...)` has exactly two call sites in `backend/app`**: `JobEngine._sign` (`app/jobs/engine.py:112`) and this line. Every signature in this system is a `Job`'s, except this one. (`app/core/ca.py`'s `.sign(key, hashes.SHA256())` is x509 issuance and is not awaited; `app/core/signing.py`'s is the signer's own implementation.)
2. **It is unguarded as well as unconsumed.** The one test that appears to cover it — `test_provisioning_dispatch.py::test_start_provision_run_prepare_envelope_signed_and_audited` — asserts only audit metadata. Delete line 131 today and **nothing goes red**. A discipline that no test can see is a comment.
3. **Spec §8.2 wants this line deleted, not preserved**: under §0.1 `provision.prepare` becomes an ordinary job type signed by the kernel, and the spec calls that "a **deletion** of a parallel signing path". §8.2 schedules the deletion with the PXE slice — but PXE is gated on an invariant amendment that may never be accepted, so scheduling it there leaves an unguarded parallel signing path in shipped code indefinitely.

So: **delete the discarded computation** and the now-unused `signer` parameter from `start_provision_run` and its one caller, and land the general rule this module is the natural home for — spec §0.1's whole thesis is one job path and one signing path.

Do **not** "fix" it by assigning and persisting the signature: that entrenches a second signing path over a bespoke envelope, needs a migration for a column to hold it, and is the design §8.2 says to remove. Do **not** make `provision.prepare` a registered job type in this cycle either — that is PXE-slice work and it has no consumer.

`backend/tests/test_signing_boundary.py` — a repo-level guard, not a site-server one: AST over every `.py` under `backend/app/`, collect every `ast.Await` whose value is a call to an attribute named `sign`, and assert the `{file: count}` map is exactly `{"app/jobs/engine.py": 1}`. **This test goes red before the deletion, naming `app/modules/provisioning/dispatch.py:131` — run it first and paste that output**, because a guard that is green the moment it is written has proved nothing. *Guard observability:* a second awaited signer call added anywhere in `backend/app` produces a different map and names the file.

Also rename `test_start_provision_run_prepare_envelope_signed_and_audited` to what it actually pins (the audit metadata and its exclusion of secrets), so the suite stops claiming coverage it never had. Say so in your report.

**Definition of done:** `wake_on_lan` reaches a real job or a real refusal; the permission split is enforced at the service boundary; the discarded signature is gone; `test_signing_boundary.py` is green **and its pre-deletion red output is in your report**; the full backend suite is green.

**Commit:** `fix(provisioning): a requested wake did nothing, and a signature was computed and dropped`

---

## Task 10 — Frontend

**Files:** Create `frontend/src/modules/site-servers/{api.ts,types.ts,SiteServerList.tsx,PromoteDialog.tsx,SiteServerDetail.tsx,SegmentCheck.tsx,WakeAction.tsx}` and tests

Per spec §11: a list with segment badges and agent status, a promote dialog picking from devices, a detail pane with the segment check and a wake action. TanStack Query, strict TypeScript, **no `any`**, matching `frontend/src/modules/ipam/`'s conventions.

Five behaviours must be tested **on rendered output**, each because the wrong rendering is a specific operational lie:

1. **A declared-but-unobserved segment renders as its own state** — never folded into "matched" and never rendered as an error. It is §0.8's designed condition: the operator declared a segment and inventory has not yet placed the machine on it, which means the server has no sweep preference there yet.
2. **`evidence: "unavailable"` renders as "cannot verify", not as "no mismatches".** Those are opposite answers and the whole point of Task 7's explicit marker.
3. **`enabled: false` renders distinctly from an absent row.** A demoted-without-delete site server is a deliberate state, and a page where it looks like a live one is a page that gets trusted.
4. **The promote dialog offers only `wake`.** `cache` and `pxe` are refused by the service (Task 2); an option the server rejects is a bug report waiting to be filed.
5. **A device with no `agents` row is not offerable for promotion**, with the reason shown — not silently missing from the list.

The wake action may be hidden without `site.wake`, but **the frontend never gates security** (invariant #6): the server-side 403 is Task 6's test, and the UI test asserts rendering only.

**Do not write a test that asserts `Object.keys()` over a fixture defined in the same file.** That test cannot fail; this project has shipped one.

**Verify:** `npm test` and `npx tsc --noEmit`.

**Commit:** `feat(site-server): the site-server console`

---

## Task 11 — Documentation, the deferred seam, and the live smoke test

**Files:** Modify `docs/modules/site-server/spec.md`, `docs/kernel/spec.md`, `docs/03-security-model.md`, `docs/modules/provisioning/spec.md`, `docs/modules/ipam/spec.md`, `docs/01-architecture.md`, `deploy/README.md`

### The amendments spec §12.4 requires

| Document | Amendment |
|---|---|
| `docs/kernel/spec.md` §1 | Site servers move out of "out of scope"; annotate that the kernel needed no change to support them, and point at Task 5's guard as where that is asserted |
| `docs/03-security-model.md` (trust-boundary list, the "Site servers are authenticated extensions of the control plane (mTLS), scoped to caching/PXE/relay" bullet) | A site server is an **ordinary agent** with an ordinary device-bound certificate; the scoping is the registry row plus (later) the signed lease, **not** a second certificate profile (§3.2). "Not implicitly trusted to command endpoints" stays true and is now enforced by `_load_job_for_device`, which is named |
| `docs/modules/provisioning/spec.md` §8 | Same correction; plus the `wake_on_lan` fix as delivered (Task 9a) and the discarded prepare signature as deleted (Task 9b) — noting that `provision.prepare` becoming a job type remains PXE-slice work |
| `docs/modules/ipam/spec.md` §5.3 | Replace the "no site-server entity exists" note with the ORDER BY term **as built** (Task 8's ordering, not §6.1's), plus the `_designated` finding raised and unfixed |
| `docs/01-architecture.md` | Site server is a promoted `Device`; relay is opaque-TCP-only if ever |
| `deploy/README.md` | New site-server section; correct the cache seam to name **both** its prerequisites — the invariant amendment *and* the absent artefact origin-fetch path (§7.4) |
| **This module's spec** | §6.1's ordering (Task 8), §12.1's precise restatement (Task 5), the roles-refusal reading (Task 2), and the real migration number in place of §2/§15's `0046` |
| `CLAUDE.md` invariant #2 | **Not amended. Not now.** It is required only for the cache and PXE, and adopting it as a side effect of shipping a registry and a UDP broadcast is exactly what spec §0.3 forbids |

### The seam, and what it is not

The deferred slices (cache §7, PXE §8, lease §7.3, relay §16) must attach later **without rework**. The seam that achieves that is small and already built:

- **the `roles` vocabulary** — the DB CHECK already admits `cache` and `pxe` (Task 1), so the cache slice needs no migration to widen a shipped constraint. The service refuses them until §4.4 is accepted (Task 2), so the refusal is one predicate to relax, in one place, with the gate named in its own error message;
- **the registry row itself** — `site_server` is what a `site_server_lease` will hang an FK on;
- **the spec** — §7.3 pins the lease's shape, §7.2 pins `cache_endpoints`, §4.4 pins the amendment's four testable properties.

**And what the seam is not, stated so nobody builds it:** no `site_server_lease` table, no `site.serve` job type, no `cache_endpoints` field on any params model, and **no `cache_endpoints_for()` helper returning `[]`**. An empty-list helper is not a seam, it is the third fixture-blindness trap (constraint 3) wearing a design pattern: every consumer test would pass against a function that never ran, and the day the cache lands nobody can tell which call sites were ever exercised. Spec §14's "reserved for the cache/PXE slice" tests are **not** written in this cycle.

### Agent-side obligations, recorded not discharged

`site.wake`'s Go capability does not exist, and **`docs/modules/agent/plan.md` does not currently plan it** — it plans `inventory.collect`, `health.check` and `net.sweep`. Record the obligation there or in this spec, and state plainly in your report that it is unbuilt and unmeasured: `CLAUDE.md` requires a benchmark that runs plus a stated falsifier before a footprint number counts as verified, and spec §5.4 now names the falsifiers (a socket or timer-wheel entry resident with no wake job in flight; RSS that does not return to its pre-job baseline) while the benchmark is still missing. The `agent` CI job added in `0203612` compiles and tests what exists, which for this capability is nothing.

### The live smoke test

A script under the scratchpad directory driving real services against a real Postgres and Redis, not mocks:

1. Create an org, two devices with agents, and an IPAM subnet `10.20.7.0/24` with an inventory observation placing **both** devices on it.
2. Promote one, declaring `10.20.0.0/16`. Assert `site.promoted` is on the audit chain and the chain still verifies.
3. `POST /{id}/wake` for a MAC on that segment → assert **one** `jobs` row, `target_type='device'`, `target_id` = the promoted device, `signature` non-NULL, `expires_at - issued_at == 300s`, `params.broadcast == '10.20.255.255'`, and the envelope on Redis **after** the commit.
4. Complete that job with a **matching** `broadcast` → `succeeded`. Complete a second with a **differing** one → `failed`, audited.
5. Run `select_sweeper` for `10.20.7.0/24` → the promoted device. Take its agent **offline** → the other device. Bring it back and mark it `enabled=false` → the other device.
6. `GET /{id}/segment-check` → `10.20.0.0/16` in `matched`.
7. Start a provisioning run with `wake_on_lan` set → the run **and** a second `site.wake` job. Repeat with the site server demoted → **400, and no run**.
8. Re-run steps 3 and 5 → the same answers, and no extra rows.

Steps 4, 5 and 8 matter most: they are the only end-to-end checks that the result handler discriminates, that liveness outranks preference against the real query planner, and that nothing here is order-dependent. **Report plainly what the script did and did not cover** — if a step could not be driven end to end, say so rather than substituting a mock.

**Verify:** full backend suite, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`, frontend `npm test` and `npx tsc --noEmit`. Record the counts against the baseline from constraint 16.

Commit; do **not** push and do **not** open a pull request.

**Commit:** `docs(site-server): the amendments v1 earned, and the four slices it did not build`

---

## Self-review

**Spec coverage.** §0.1 a site server is a `Device` → T1 (the table hangs off `devices`), T5 (asserted). §0.2 promotion is a control-plane act → T2. §0.3 nothing listens → constraint 10, T11. §0.4–§0.6 cache integrity, confidentiality, no relay → **not built**; recorded in T11. §0.7 one ORDER BY term, no second function → T8. §0.8 claim vs evidence → T2 (promotion accepts an unverified claim), T7 (surfaces it), T8 (preference reorders, never adds). §0.9 `sent`, not `woke` → T3. §0.10 the 300s TTL → T4. §1 scope → the task list. §2 data model → T1. §3 enrollment untouched, no certificate profile → T5 (a) and (d) assert it structurally; T11 corrects `docs/03-security-model.md`. §4 the listening question → not built; §4.4 **not adopted**. §5.1 registration in both processes → T3. §5.2/§5.3 params and result → T3. §5.4 idle budget → agent-side, unbuilt, recorded T11. §5.5 `net.sweep` unchanged → T8. §6 selection and fallback → T8, **with §6.1's ordering corrected to §6.2's behaviour**. §7/§8 cache and PXE → not built. §9 trust → nothing here weakens it. §10 tenancy/RBAC/audit → T1, T2, T4, T6. §11 API → T6, T7. §12.1 no kernel change → **T5, restated precisely and asserted four ways**. §12.3 cross-module requests → T8 (IPAM), T9 (provisioning); app-deploy's `cache_endpoints` is cache-slice and untouched. §12.4 doc amendments → T11. §13 code-vs-doc findings → §13.1 constraint 9, §13.2 T9b, §13.3/§13.4 not built, §13.5 T9a, §13.6 T5 (b), §13.7 nothing adds a third `target_type`. §14 testing → distributed, each named in its task. §15 migration notes → T1 with constraint 12 overriding the number. §16 deferrals → nothing built.

**Kernel touch points, all four of them append-only:** `app/core/rbac.py` (T1), `app/models/__init__.py` (T1), `app/modules/__init__.py` (T3, T6), `app/gateway/main.py::register_module_job_types` (T3). No kernel *logic* changes, and T5 (a) asserts it as an exact map rather than a subset.

**Files owned by another module or by the kernel, flagged:** `app/core/rbac.py`, `app/models/__init__.py`, `app/modules/__init__.py`, `app/gateway/main.py`, `scripts/export_wire_schema.py`, `docs/kernel/wire-protocol/` (kernel); `app/modules/ipam/sweep.py` and `docs/modules/ipam/spec.md` (IPAM); `app/modules/provisioning/{schema,dispatch,api}.py`, `backend/tests/test_provisioning_dispatch.py` and `docs/modules/provisioning/spec.md` (provisioning); `docs/03-security-model.md`, `docs/01-architecture.md`, `docs/kernel/spec.md`, `deploy/README.md` (platform docs).

**Where this plan overrides the spec, and why — three places, each argued in its task, none of them silent.** (1) §12.1's literal "no file under `app/jobs/`, `app/gateway/`, `app/core/`" is contradicted by §5.1 and by the repo's permission pattern; T5 restates it as four append-only registration points and no kernel branch, and asserts that instead. (2) §6.1's ORDER BY puts preference above liveness, which contradicts §6.2's fallback table and IPAM's own `_designated` rule; T8 implements §6.2 and corrects §6.1. (3) §8.2 schedules the deletion of the discarded `provision.prepare` signature with the PXE slice, which is gated on an amendment that may never be accepted; T9b deletes it now and lands the general guard, while leaving the *replacement* — `provision.prepare` as a registered job type — where §8.2 put it.

**Things this plan deliberately does not build, so that "it isn't done yet" is never mistaken for "it was forgotten":** the cache, PXE, the `site.serve` lease and its table, the relay, backup LAN staging, `cache_endpoints` on any params model, `cache_endpoints_for()`, a GiST index on `segments`, a second `Job.target_type`, a site-server certificate profile, a `site_server` enrollment token kind, worker-process registration of `site.wake`, any Go, and any amendment to `CLAUDE.md`.

**Known risk this plan does not remove.** Four of the eleven tasks turn on a single ordering decision inside one SQL statement (T8), and an ordering bug there is invisible in any fixture where one candidate is obviously better than the others. Constraint 3 and T8's "preferred among equals" fixture — identical `last_seen_at`, ids seeded so the wrong answer sorts first — exist for exactly that, and the smoke test's step 5 is the only place the real planner is asked the question. Neither is optional.
