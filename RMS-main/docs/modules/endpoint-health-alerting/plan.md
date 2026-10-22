# Endpoint Health, Alerting & Auto-Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Threshold-based endpoint health evaluated at the edge, an alert lifecycle that cannot fail to clear and cannot re-fire every tick, and three alert-triggered actions — notify, open a ticket, run an approved remediation as a signed job — with the whole remediation path governed by a registry Phase 17 will share.

**Architecture:** The agent's existing timer wheel owns the tick; the threshold policy reaches it as a **signed job** (`health.policy.apply`), because no unsigned server→agent channel may exist (invariant #1). The agent emits only edges — `breach`, `recovery` — plus a periodic `summary`, on a **new agent→server wire kind** `health_report`. The server owns the entire alert lifecycle: dedup by a partial unique index, flap suppression that removes actions but never evidence, and six enumerated clear predicates. Actions are written as `pending` rows in the alert's own transaction and executed by a worker afterwards, idempotent by two unique indexes. Remediation dispatches an existing, registered job type through the **unmodified** signed-job engine.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Pydantic v2, Alembic, Celery, Postgres RLS, React + TypeScript. **No new dependency** — no metrics library, no expression evaluator, no scheduler.

---

## Global Constraints

Every task's requirements implicitly include this section. These are earned lessons from previous sub-projects of this repo, not general advice.

1. **Authority is `docs/modules/endpoint-health-alerting/spec.md` (commit `e6ccf85`).** Where this plan and the spec disagree, the spec wins. Where the **code** and either of them disagree, see constraint 6.

2. **Registering an event type in `app/core/events.py` carries notification-side obligations.** Adding a name to `EVENT_TYPES` without also adding (a) an email renderer in `_EMAIL_RENDERERS` (`app/modules/notification/delivery.py`) and (b) a pinned wire-format sample in `_EVENT_TYPE_SAMPLE_PAYLOADS` (`backend/tests/test_notification_delivery.py`) turns two existing set-equality tests red — `test_every_registered_event_type_has_an_email_renderer` and `test_every_registered_event_type_has_a_pinned_wire_format_sample`. **Touching `app/core/events.py`, `app/core/rbac.py`, or `app/models/__init__.py` requires running the FULL backend suite, not just this module's tests.** A previous build in this repo shipped with 2 failures that were only visible that way.

3. **Fixture blindness is the recurring defect class here.** A guard test must make the guarded and the unguarded path produce **different observable values**. If the fixture makes both paths agree, the assertion is decoration and the test passes against the bug. **Every task below that adds a guard states what the test would observe if the guard were deleted — implement that observation, not a weaker one.** Concretely: never assert "no exception was raised", never assert `Object.keys()`/`set()` over a collection defined in the same file, never assert that a function was called when you can assert what it stored.

4. **RLS masks application predicates.** A cross-org test driven through `org_session` passes even when the service's own `org_id` predicate has been deleted, because RLS rejects first. To pin an application-layer predicate you must exercise it on the **admin engine**, where RLS is not the thing doing the rejecting. This defect shipped four times in the SLA sub-project. Every service in this module carries an explicit `org_id` predicate **in addition to** RLS, and at least one test per service pins it on the admin engine.

5. **For an `ALL`-command RLS policy, Postgres reuses `USING` as `WITH CHECK` when the latter is omitted** — so deleting the `WITH CHECK` clause falsifies nothing and proves nothing. Write `WITH CHECK` explicitly anyway (it matches `0002_rls.py` and `0025_notification_core.py`, and it stops silently following a future divergent read predicate), but **the mutations that actually falsify a tenancy test are `WITH CHECK (true)` for an insert test and `USING (true)` for a read test.** Wherever a step says "watch it go red", use those two.

6. **Trust reality over this plan.** If a task's brief contradicts what the code actually does — a signature, a table name, a constraint, a behaviour — **the code wins, and you say so in your report rather than implementing the brief.** This plan was written against the code at commit `9fbd7df`; other agents are working concurrently and the code may have moved.

7. **Migrations are forward-only; never edit a shipped migration.** **Do not hardcode a migration number from this plan or from the spec.** Committed head at writing was `0038`; `0039_sla_breach_sweep_indexes.py` was in flight from a concurrent agent and more may have landed since. The spec's §2/§16 say "migration `0040`" — **treat that as stale**. Before each of the three migrations in this plan, run `cd backend && .venv/bin/alembic heads` (and `ls migrations/versions/`) to find the real head, take the next free number, and set `down_revision` to the bare short string of the actual head (e.g. `"0041"`), matching every other migration in this repo. This plan refers to them as **Migration A** (kernel: events, permissions, notification CHECKs), **Migration B** (`approved_action` + script exemptions), **Migration C** (the seven health tables) — chain B off A and C off B.

8. **NULLs do not collide in a Postgres unique constraint.** `UNIQUE (org_id, device_id, device_group_id)` does **not** prevent two org-default assignment rows, nor two device-level rows for one device. The spec's per-resolution-level **partial** unique indexes (§0.2) are load-bearing, not decoration. And a **partial index is not a named constraint**: `ON CONFLICT ON CONSTRAINT uq_health_assignment_device` fails at runtime with `InvalidObjectDefinition`. Every upsert against a partial index must use `index_elements=[...]` **plus** `index_where=...` on `sqlalchemy.dialects.postgresql.insert`. There must be a test that executes each such upsert against the real database — a unit test that never runs the statement cannot see this.

9. **`test_schema_drift.py` compares `Base.metadata` to the live schema and does NOT compare indexes.** Every partial unique index must therefore be declared in the model's `__table_args__` **as well as** in the migration, or a future `alembic revision --autogenerate` proposes dropping all of them — including `uq_health_alert_open_key`, which the entire alert lifecycle rests on. This is the trap `docs/modules/sla/spec.md` §11 hit and documented.

10. **Tooling.** `pytest` is at `backend/.venv/bin/pytest`; bare `pytest` and `python -m pytest` are not installed. Use `cd backend && .venv/bin/pytest tests/... -v`, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`, `.venv/bin/alembic ...`. Frontend: `npx tsc --noEmit` is the only static check (`npm run lint` does not exist); `npm test` for the suite.

11. **Measure the baseline before you start.** Run the full backend suite once and record the pass count. Do **not** trust a number written in this document or in a sibling plan — concurrent agents are adding tests. Do not regress that number.

12. **Postgres and Redis go down in this container during long runs.** Check `pg_isready` and `redis-cli ping` first, and again if anything hangs; restart them yourself (`service postgresql start`, `redis-server --daemonize yes`) and carry on. Do not background a long test run and wait on it.

13. **Naming and shape facts, verified against the code — do not guess.**
    - Kernel tables are plural: `devices`, `device_groups`, `agents`, `users`, `organizations`, `jobs`, `scripts`, `audit_events`, `domain_events`, `permissions`, `roles`, `role_permissions`, `notification_rules`. Service-desk tables are **singular**: `ticket`, `ticket_queue`, `ticket_category`, `ticket_comment`, `ticket_status_history`.
    - `permissions` is `(code, module, action)` — there is **no** `resource` and **no** `description` column.
    - `AuditEvent`'s metadata attribute is **`meta`** (mapped to column `metadata`).
    - Tenancy GUC is **`app.current_org`**, read as `current_setting('app.current_org', true)::uuid`. There is no `app.org_id`.
    - Modules register in **`app/modules/__init__.py`**, not `app/main.py`.
    - FastAPI 0.139.2: `app.routes` **is** `app.router.routes` and **neither contains `include_router`'d routes** — only a `_IncludedRouter` with `path=None`. A route-completeness test must flatten via `_IncludedRouter.original_router` + `include_context.prefix`. `backend/tests/test_email_intake_api.py` has a working flattener — copy it.
    - Tests use the `api_env` fixture with `asyncio_mode=auto` — **do not** write `@pytest.mark.asyncio`. `tests/conftest.py` provides `admin_engine`, `app_engine`, `api_env`, `a_ticket_for`, `add_comment_as_staff`.
    - Async throughout, full type hints, business logic in services and not in route handlers, `mypy` and `ruff` clean.

14. **Nothing in this module reaches a device except through `JobEngine.create()` → `publish_job()`.** No shell string in any params model, no second egress path. This is asserted structurally by a test (Task 11).

15. **Publish to Redis only after the tenant transaction commits.** `app/modules/deploy/scripts.py::run_script` states the reason in its own docstring: a job published before the caller's transaction commits could be handed to an agent with no durable server record if the commit later failed. Same for `queue_ui_event` — `handle_agent_message` drains `session.info["ui_events"]` after the `org_session` block exits.

16. **Agent-side work is out of scope for this plan — not out of this repository.** No Go code, no capability implementation, and no idle-budget benchmark lands in *this cycle*. Task 14 records those obligations; it does not implement them, and none of them has been implemented since. *(Amended 2026-08-05: this constraint used to read "belongs in `it-suite-agent`, a separate repository". `CLAUDE.md` commit `a32cead` moved the agent in-tree to `agent/`. The scope boundary this plan drew is unchanged — it is now a boundary of cycle rather than of repo — and nothing it deferred has been done.)*

---

## Verified signatures you will call

Confirmed against the code at `9fbd7df`. Re-verify before use (constraint 6).

```python
# app/jobs/engine.py
JobEngine.create(session, *, org_id: UUID, type: str, target_device_id: UUID,
                 params: dict[str, Any], requested_by: UUID | None = None,
                 ttl_s: int | None = None) -> Job
# app/jobs/registry.py
register(JobType(name: str, params_model: type[BaseModel], result_handler: ResultHandler | None))
registered_names() -> list[str]
# app/gateway/dispatch.py
publish_job(redis, device_id: UUID, envelope: dict[str, Any]) -> None
queue_ui_event(session, org_id: UUID, event: dict[str, Any]) -> None
handle_agent_message(app_engine, job_engine, redis, msg, *, device_id: UUID, org_id: UUID) -> None
# app/core/events.py
publish(session, *, org_id, event_type, subject_type, subject_id,
        payload: dict[str, Any], actor_user_id: UUID | None) -> None
# app/core/audit.py
audit_service.record(session, *, org_id, actor_type, actor_id, action,
                     target_type="", target_id="", metadata=None) -> AuditEvent
# app/modules/service_desk/tickets.py
create_ticket(session, org_id, actor_id: UUID | None, data: TicketCreate, *,
              origin: TicketOrigin = "technician", source_alert_id: UUID | None = None) -> Ticket
# app/modules/service_desk/transitions.py
change_status(session, org_id, ticket_id, *, to_status: str, actor_id: UUID | None,
              actor_type: Literal["user","system"] = "user", reason: str = "") -> Ticket
```

Relevant column facts: `devices` has `os_family`, `device_group_id`, `status`; `device_groups` has `parent_group_id` (**no cycle constraint** — hence §0.2's depth bound); `agents` has `last_seen_at` and `status`; `scripts` has `version` under `uq_scripts_org_name_version`; `ticket` has `number` (String(24)) and `source_alert_id`.

---

## File structure

**Created (backend):**

| File | Responsibility |
| --- | --- |
| `backend/app/core/approved_actions.py` | **Kernel-owned.** `ApprovedAction`/`ApprovedActionScriptExemption` models, `is_destructive()`, `_DESTRUCTIVE_JOB_TYPES`, `DESTRUCTIVE_PARAM_KEYS`, `SUBSTITUTABLE_PARAM_KEYS`, template rendering |
| `backend/app/modules/health/__init__.py` | `register(app)` |
| `backend/app/modules/health/models.py` | The seven module ORM models |
| `backend/app/modules/health/errors.py` | `HealthValidationError`, `HealthNotFoundError` |
| `backend/app/modules/health/registry.py` | The **closed** check-type registry (§4.1) as data: `value_kind`, `cadence`, `evaluated_by`, per-OS availability |
| `backend/app/modules/health/profiles.py` | Profile / check / assignment CRUD, `policy_version` bump, audit |
| `backend/app/modules/health/settings.py` | `health_org_settings` read (effective) / write (audited) |
| `backend/app/modules/health/resolution.py` | `resolve_profile_for_device`, the depth-bounded group walk, OS filtering |
| `backend/app/modules/health/jobtypes.py` | `HealthPolicyApplyParams`, `HealthCheckSpec`, `HealthCheckNowParams`, `register_job_types()` |
| `backend/app/modules/health/policy_push.py` | Build + dispatch `health.policy.apply`; the ack result handler |
| `backend/app/modules/health/flap.py` | `health_flap_state` window/threshold/cooldown algorithm |
| `backend/app/modules/health/ingest.py` | `ingest_report()` — the open/clear predicates, `device_health_check_state` upsert |
| `backend/app/modules/health/alerts.py` | Alert state transitions: acknowledge, mute, close, clear-by-\<reason\> |
| `backend/app/modules/health/actions.py` | The action worker: remediate → open_ticket → notify, fuses, autonomy dial, approvals |
| `backend/app/modules/health/sweep.py` | The four sweep bodies + their interval constants |
| `backend/app/modules/health/approved_actions.py` | Service layer over the kernel registry: CRUD, validation, audit, exemptions |
| `backend/app/modules/health/schema.py` | Pydantic request/response models |
| `backend/app/modules/health/api.py` | Routes, each `require_permission`-gated |

**Modified (backend):** `app/core/events.py` (4 types + `STAFF_ONLY_EVENT_TYPES`), `app/core/rbac.py` (5 codes), `app/gateway/protocol.py` (`HealthReportMsg`), `app/gateway/dispatch.py` (one branch), `app/models/__init__.py`, `app/modules/__init__.py`, `app/worker.py` (5 beat entries + `register_worker_job_types`), `app/modules/notification/delivery.py` (**4 renderers — notification-owned file**), `scripts/export_wire_schema.py`.

**Modified (tests owned by another module):** `backend/tests/test_notification_delivery.py` (4 pinned samples).

**Created (frontend):** `frontend/src/modules/health/` — `api.ts`, `types.ts`, `AlertConsole.tsx`, `AlertDetail.tsx`, `ProfileList.tsx`, `ProfileEditor.tsx`, `DeviceHealthPanel.tsx`, `ApprovedActionList.tsx`.

---

# Kernel tasks

Tasks 1–3 change kernel-owned code. They land first because everything else depends on them, and because they are the tasks most likely to break suites that have nothing to do with this module. **Run the full backend suite at the end of each of them** (Global Constraint 2).

---

## Task 1 — KERNEL: four event types, five permission codes, four staff-only CHECKs

**This is a kernel task and it touches a file the notification module owns.**

**Files:**
- Modify: `backend/app/core/events.py`, `backend/app/core/rbac.py`
- Modify (**notification-owned**): `backend/app/modules/notification/delivery.py`, `backend/tests/test_notification_delivery.py`
- Create: `backend/migrations/versions/<A>_health_kernel.py` (see Global Constraint 7)
- Test: `backend/tests/test_health_kernel_registry.py`

**Interfaces:**
- Consumes: nothing.
- Produces: event types `health.alert_raised`, `health.alert_cleared`, `health.alert_escalated`, `health.remediation_requires_approval`; permission codes `health.read`, `health.alert.manage`, `health.admin`, `health.remediate`, `health.action.admin`; migration revision A.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_health_kernel_registry.py` covers:

- each of the four event types is in `EVENT_TYPES` and in `STAFF_ONLY_EVENT_TYPES`;
- each of the five permission codes is in `PERMISSIONS` and seeded to `org-admin`; `health.action.admin` and `health.admin` are **not** granted to any technician-tier role, and `health.remediate` is **not** implied by `health.admin` (assert on the actual `role_permissions` rows, per role name);
- `staff_only_rejection(t, "requester")` returns a non-`None` string for all four types, and `None` for any other `recipient_kind`;
- **four DB-layer tests**, one per type: inserting a `notification_rules` row pairing that `event_type` with `recipient_kind='requester'` raises `IntegrityError`/`ProgrammingError` **on the admin engine**.

- [ ] **Step 2: Run to verify failure**

`cd backend && .venv/bin/pytest tests/test_health_kernel_registry.py -v` — expect failures naming the missing event types.

- [ ] **Step 3: Implement**

Add the four types to `EVENT_TYPES` with a comment naming this module as publisher (follow the `ticket.sla_breached` comment's shape). Add all four to `STAFF_ONLY_EVENT_TYPES` with a `StaffOnlyEvent(noun, spec_ref)`.

**Reproduce spec §9.2's correction in the code comment.** `app/core/events.py` lines 79–83 currently claim `recipient_kind="requester"` "resolves the recipient from the TICKET, not from the event payload". `routing.py::_expand_rule` does **not** do that — it resolves from `payload["requester_id"]` via `_uuid_or_none` → `_resolve_single_user`. The guard's conclusion is right; its stated mechanism is wrong. Correct the comment while you are in the file; do not delete the guard.

Add the five codes to `PERMISSIONS` with the descriptions in spec §11.1, including the HIGH PRIVILEGE wording for `health.admin`, `health.remediate` and `health.action.admin`.

Add the four renderers to `_EMAIL_RENDERERS` in `delivery.py`, modelled on `_render_ticket_sla_breached`. They are **staff-facing** text. They must render hostname, check type, selector, severity, observed value and threshold, and — for `health.remediation_requires_approval` — the action key and the pinned script name/version if any. **No raw event-log text, no script body, no credential** (spec §9.1).

Add the four pinned samples to `_EVENT_TYPE_SAMPLE_PAYLOADS` in `tests/test_notification_delivery.py`, each carrying **every** key of spec §9.1's payload contract, with `queue_id` non-NULL and `assignee_id`/`requester_id` present-and-`null`.

Migration A: seed the five permission codes and their role grants (copy the exact insert SQL out of `0033_sla.py` rather than trusting this plan), and add four `ck_rule_no_health_<type>_to_requester` CHECK constraints on `notification_rules`, following `ck_rule_no_internal_to_requester` (0025) and `ck_rule_no_sla_breach_to_requester` (0034).

- [ ] **Step 4: Run the FULL backend suite**

`cd backend && .venv/bin/pytest -q`. `test_rbac.py::test_permission_registry_complete` pins the exact permission-code set — adding the five codes to its literal is the correct fix, not weakening the assertion. The two notification set-equality tests must pass **because you added the renderers and samples**, not because you relaxed them.

- [ ] **Step 5: Prove the guards can fail**

- Delete one of the four CHECK constraints from the migration, rebuild the test DB, re-run the four DB-layer tests. **If the guard is deleted, the insert succeeds and the row is readable** — the test observes a returned `notification_rules` row instead of an exception. If it stays green, the test is asserting against the application-layer guard rather than the constraint; move it to the admin engine.
- Remove one renderer from `_EMAIL_RENDERERS`. **If the obligation is deleted, `_render_email` falls through to `_render_generic` and delivers a machine notice** — `test_every_registered_event_type_has_an_email_renderer` goes red. Restore.
- Paste both outputs in your report.

**Definition of done:** four event types registered, staff-only, and CHECK-guarded at the database layer; five permission codes registered and seeded; four renderers and four pinned samples present; **full backend suite green**; `mypy app` and `ruff check app tests` clean.

**Commit:** `feat(health): kernel event types, permissions, and staff-only rule guards`

---

## Task 2 — KERNEL: the `health_report` agent→server wire kind

**This is a kernel task: it changes the agent wire contract.**

**Files:**
- Modify: `backend/app/gateway/protocol.py`, `backend/app/gateway/dispatch.py`, `scripts/export_wire_schema.py`
- Create: `docs/kernel/wire-protocol/HealthReportMsg.schema.json` (generated, committed)
- Test: `backend/tests/test_health_wire.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `HealthReportMsg`, `HealthReportLine` in `app/gateway/protocol.py`; `HealthReportMsg` added to the `AgentMessage` discriminated union.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_health_wire.py` — pure parse-layer tests against `parse_agent_message`, no database:

- a well-formed `health_report` with one breach line, one recovery line and one summary line parses, and `msg.kind == "health_report"`;
- `reports: []` is rejected (`min_length=1`);
- 65 report lines are rejected (`max_length=64`);
- `kind: "flatline"` on a line is rejected;
- `state: "degraded"` is rejected;
- an extra top-level field and an extra per-line field are each rejected (`extra="forbid"`);
- a 65-character `value` is rejected (`max_length=64`);
- **no field in the payload names a device** — assert `"device" not in HealthReportMsg.model_json_schema()["properties"]` and likewise for the line model. This is the wire-rule form of "identity comes from the certificate binding".

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`/`ValidationError` mismatch.

- [ ] **Step 3: Implement**

In `protocol.py`, modelled directly on `SecurityEventsMsg`:

```python
class HealthReportLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_id: str
    selector: str = Field(default="", max_length=200)
    kind: Literal["breach", "recovery", "summary"]
    state: Literal["ok", "breaching", "unavailable"]
    value: str = Field(default="", max_length=64)
    observed_at: str  # RFC3339


class HealthReportMsg(_Msg):
    """agent -> server: threshold edges and periodic summaries (at-least-once).

    Carries no device identity: the binding comes from the mTLS connection.
    Carries no sample series -- see spec §0.12 and §5.1's wire rule.
    """
    kind: Literal["health_report"]
    policy_version: int = Field(ge=0)
    reports: list[HealthReportLine] = Field(min_length=1, max_length=64)
```

Add it to the `AgentMessage` union.

**`handle_agent_message` must be updated in this task**, because adding a non-job-bound member to the union makes `msg.job_id` a mypy error on the union. Add, next to the `SecurityEventsMsg` branch:

```python
if isinstance(msg, HealthReportMsg):
    # Ingestion lands in Task 9. Until then this branch deliberately drops the
    # report rather than falling through to the job-bound path, which would
    # raise AttributeError on `msg.job_id`.
    return
```

Ship a test pinning that interim behaviour (`a health report neither raises nor touches jobs`). **Task 9's definition of done includes deleting that test and that comment.** Do not leave a stub that pretends to ingest.

Add `HealthReportMsg` to `scripts/export_wire_schema.py` and regenerate: `backend/.venv/bin/python scripts/export_wire_schema.py`. Commit the generated JSON — it is the contract the agent builds Go structs against, and it stays a compatibility contract now that the agent shares this repo (`docs/kernel/wire-protocol/README.md`).

- [ ] **Step 4: Run tests**

`cd backend && .venv/bin/pytest tests/test_health_wire.py tests/test_gateway*.py -v`, then the **full** suite (this touches the kernel).

- [ ] **Step 5: Prove the parse layer can fail**

Change `reports` to `list[dict[str, Any]]`. **If the typed model is deleted, the unknown-`kind` and unknown-`state` tests observe a successfully parsed message carrying `{"kind": "flatline"}` into business logic** instead of a `ValidationError`. Watch those two go red; restore.

**Definition of done:** the union accepts `health_report` and rejects every malformation above; `handle_agent_message` has an explicit, tested, temporary branch; the JSON schema is regenerated and committed; full backend suite green; `mypy`/`ruff` clean.

**Commit:** `feat(health): the health_report agent wire message`

---

## Task 3 — KERNEL: the `approved_action` registry and `is_destructive`

**This is a kernel task. The registry lives in `app/core/`, not in this module — Phase 17 and the AI-automation layer share it (spec §2.9, §8.5). A registry owned by the health module is a registry the AI layer will fork.**

**Files:**
- Create: `backend/app/core/approved_actions.py`, `backend/migrations/versions/<B>_approved_actions.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/test_approved_actions_registry.py`, `backend/tests/test_approved_actions_destructive.py`

**Interfaces:**
- Consumes: `app.jobs.registry.registered_names()`, Migration A.
- Produces: `ApprovedAction`, `ApprovedActionScriptExemption` models; `is_destructive()`; `_DESTRUCTIVE_JOB_TYPES`; `DESTRUCTIVE_PARAM_KEYS`; `SUBSTITUTABLE_PARAM_KEYS`; `render_params_template()`.

**Spec gap you must close in this task (flagged, decide and record).** Spec §8.4 defines `is_destructive(job_type, params_template, script_exemption) -> bool` and §10 exposes `POST /approved-actions/{id}/exempt-script`, but **§2 defines no table for the exemption**. §8.4's wording — "granted per exact version … Editing the script mints a new version and the exemption does not follow it" — is a property of the `(script_id, script_version)` pair, not of one approved action. **Recommended resolution:** a second kernel table `approved_action_script_exemption (id, org_id FK, script_id FK, script_version int, granted_by FK, granted_at, reason str(500))` with `UNIQUE (org_id, script_id, script_version)`, RLS `ENABLE + FORCE`. The API route then grants the exemption for the action's pinned pair. **Record the decision and its reason in your report and in spec §8.4.**

- [ ] **Step 1: Write the failing tests**

`test_approved_actions_destructive.py` — pure, no database:

- `is_destructive("backup.restore", {}, None)` is `True`; likewise `provision.enroll`, `app.deploy`, `patch.apply`;
- `is_destructive("inventory.collect", {}, None)` is `False`;
- `is_destructive("script.run", {...}, None)` is **`True`** — arbitrary code is destructive by default;
- `is_destructive("script.run", {...}, exemption_for(script_id, 3))` is `False` **only** when the exemption's `(script_id, script_version)` matches exactly; an exemption for version 2 against a version-3 pin returns `True`;
- `is_destructive("patch.apply", {"reboot": True}, None)` is `True` via `DESTRUCTIVE_PARAM_KEYS`;
- `_DESTRUCTIVE_JOB_TYPES ⊆ set(registered_names())` — **assert against the live registry**, so a renamed job type breaks this test rather than silently disarming the gate. (Import the modules that register job types first, or call `register_worker_job_types()`.)

`test_approved_actions_registry.py` — database:

- RLS enabled **and forced** on both tables; a cross-org read test and a cross-org insert test each;
- an app-layer `org_id` predicate test **on the admin engine** (constraint 4);
- **the autonomy CHECK**: inserting `autonomy='autonomous'` with `destructive=True` raises; likewise with `risk_class='high'`; likewise with `max_target_scope != 'device'`. Run these **on the admin engine** so the database is demonstrably the thing rejecting, not a Pydantic validator;
- `max_target_scope` CHECK rejects `'group'` in v1;
- `UNIQUE (org_id, key)`; **no global rows** — `org_id` is `NOT NULL` (assert `is_nullable == 'NO'` from `information_schema`);
- `script_id`/`script_version` CHECK: required when `job_type='script.run'`, forbidden otherwise.

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`app/core/approved_actions.py`: the two models (columns exactly per spec §2.9 plus the exemption table above), `is_destructive` as a **pure function** — it takes the exemption row (or `None`), it does not query — and:

```python
# Which params keys may receive a substitution, per job type. NOT a template
# language: exactly two substitutions exist ({device_id}, {selector}) and this
# map bounds where each may appear. Adding an entry is a reviewed code change.
SUBSTITUTABLE_PARAM_KEYS: dict[str, frozenset[str]] = {...}
```

`render_params_template(job_type, template, *, device_id, selector) -> dict` substitutes only `{device_id}` and `{selector}`, only into keys the map allows, and **raises** on any other brace token. Do not use `str.format` on the whole structure — an unexpected `{` in a customer-authored selector must be an error, not an exception from the formatter, and `format` would expose every attribute of every value.

`destructive` is **derived and stored** at write time, never read from a request body — that is Task 13's job to enforce at the API, but the column exists here and the CHECK depends on it.

Migration B: both tables, RLS `ENABLE + FORCE` with a `tenant_isolation` policy carrying **both** `USING` and `WITH CHECK`, all CHECKs including the autonomy gate:

```sql
CHECK (NOT (autonomy = 'autonomous'
            AND (destructive OR risk_class = 'high' OR max_target_scope <> 'device')))
```

Register both models in `app/models/__init__.py`.

- [ ] **Step 4: Run tests + FULL suite** (this touches `app/models/__init__.py` — constraint 2). Include `tests/test_schema_drift.py` and `tests/test_rls.py`.

- [ ] **Step 5: Prove the guards can fail**

- **Autonomy gate:** delete the CHECK from the migration, rebuild, re-run. **If the gate is deleted, the insert succeeds and the test observes a stored row with `autonomy='autonomous', destructive=true`** — the row you can then SELECT back. Watch it go red; restore.
- **Destructive-by-default:** change clause 2 so `script.run` is destructive only when an exemption is *absent for a different version*. **If the default flips, `is_destructive("script.run", {}, None)` returns `False`** and the un-exempted test observes `False` where it expects `True`. Watch it go red; restore.
- **Tenancy:** `WITH CHECK (true)` → insert test red; `USING (true)` → read test red. Paste both.

**Definition of done:** the registry exists in `app/core/` with the gate enforced by Postgres; `is_destructive` is pure and pinned against the live job-type registry; the exemption storage decision is recorded; full backend suite green.

**Commit:** `feat(core): the shared approved-action registry and the autonomy gate`

---

# Module tasks

---

## Task 4 — Migration C: seven health tables, the partial indexes, the check-count trigger

**Files:**
- Create: `backend/migrations/versions/<C>_health.py`, `backend/app/modules/health/__init__.py` (empty for now), `backend/app/modules/health/models.py`, `backend/app/modules/health/errors.py`, `backend/app/modules/health/registry.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/test_health_migration.py`, `backend/tests/test_health_check_registry.py`

**Interfaces:**
- Consumes: Migration B.
- Produces: `HealthProfile`, `HealthCheck`, `HealthProfileAssignment`, `HealthAlert`, `HealthAlertAction`, `DeviceHealthCheckState`, `HealthFlapState`, `HealthOrgSettings`, `DeviceHealthPolicyAck` (see gap below); `CHECK_TYPES` registry; the seed helpers later tasks import — `_seed_profile(api_env, org_id, **kw) -> UUID`, `_seed_check(api_env, profile_id, **kw) -> UUID`, `_seed_device(api_env, org_id, **kw) -> UUID`, `_seed_assignment(...) -> UUID`.

**Two spec gaps you must close in this task (flagged; decide, implement, record).**

- **`device_health_policy_ack` has no definition.** Spec §5.2 says the `health.policy.apply` result handler "stamp[s] `device_health_policy_ack` promptly", and §0.8 clause 2 and §12's reconciliation sweep both compare against "the device's last **acknowledged** version" — but §2 lists no such table or column. **Recommended:** an eighth table `device_health_policy_ack (org_id FK, device_id FK PK, profile_id FK NULL, policy_version int, acknowledged_at)`, one row per device, upserted. It is a per-device state row, not a history — same shape and same bound as §2.6, so it does not breach §0.12.
- **`health_ingest_stats` does not exist.** Spec §0.8 clause 1 says a discarded report line is "counted in `health_ingest_stats`, not silently dropped", but §2 defines no such table and §0.12 states that **exactly three** tables retain anything. **Recommended:** do not add a table. Return the discard count from `ingest_report()`, include it in the `queue_ui_event` payload, and log it at WARNING with the device id. A per-batch counter table would be a time series by another name. **Record the decision.**

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_health_migration.py`, opening with a docstring that says the RLS tests are **behavioural** — a test asserting `relrowsecurity` is true passes against a policy whose predicate was deleted.

Minimum coverage:

```python
@pytest.mark.parametrize("table", [
    "health_profile", "health_check", "health_profile_assignment",
    "health_alert", "health_alert_action", "device_health_check_state",
    "health_flap_state", "health_org_settings", "device_health_policy_ack",
])
async def test_rls_is_enabled_and_forced(admin_engine, table): ...
```

plus, per table, a cross-org **read** test and a cross-org **insert** test through `org_session` on `api_env.app_engine`.

Then the structural constraints, each as its own named test:

- **`uq_health_alert_open_key`** — two `active` alerts for the same `(org_id, device_id, check_id, selector)` raise; after clearing the first, a second **succeeds**. *(Both halves are required: the first alone passes against a non-partial index, which would forbid the legitimate re-open.)*
- **The three assignment partial indexes** — two device-level rows for one device raise; two group-level rows for one group raise; two org-default rows raise; and a device-level row **plus** a group-level row **plus** an org-default row all coexist.
- **The mutual-exclusion CHECK** — a row with both `device_id` and `device_group_id` raises.
- **`UNIQUE (alert_id, kind)`** on `health_alert_action`.
- **`UNIQUE (org_id, device_id, check_id, selector)`** on `device_health_check_state` and on `health_flap_state`.
- **The 32-check trigger** — inserting a 33rd `health_check` for one profile raises; 32 succeed; **and a single multi-row `INSERT ... VALUES` of 33 rows also raises** (a row-level trigger would pass the first assertion and fail this one).
- **CHECK ranges**: `tick_interval_s` 59 raises, 3601 raises; `posture_interval_s` 899 raises; `posture_interval_s < tick_interval_s` raises; `for_ticks` 0 and 61 raise; `summary_interval_s` outside 300–86400 raises.
- **Alert CHECKs**: `(state='cleared') = (cleared_at IS NOT NULL)`; `(state='muted') = (muted_until IS NOT NULL)`; `muted_until > opened_at + 30 days` raises; `cleared_reason` outside the six-value enum raises.
- **`health_alert.observed_value` is a string column** — assert `data_type` from `information_schema`, because "someone later adds a numeric column and aggregates it over time" is the failure §0.12 exists to prevent.

`backend/tests/test_health_check_registry.py` — the closed registry (§4.1) as data:

- every `check_type` in `CHECK_TYPES` appears in the `health_check.check_type` CHECK constraint's definition (read `pg_get_constraintdef`) **and vice versa** — the SQL enum and the Python registry cannot drift;
- `agent.offline` is the **only** entry with `evaluated_by == "server"`;
- `posture.antivirus_healthy` is available on `windows` only; `disk.smart_ok` is unavailable on `macos`;
- every entry's `value_kind` is one of `numeric`/`boolean`/`enum`, and every `cadence` is `tick`/`posture`.

**Do not write a test that iterates `CHECK_TYPES` and asserts something about `CHECK_TYPES`** (constraint 3). The registry test earns its keep only by comparing the Python registry against the **database** constraint.

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError: No module named 'app.modules.health'`.

- [ ] **Step 3: Write the models**

`app/modules/health/models.py`, header modelled on `app/modules/sla/models.py` (which states the schema-drift contract explicitly). Every column exactly per spec §2.1–2.8, **plus** `escalated_at` on `health_alert` — spec §6 requires it ("stamped by `escalated_at`") and §2.4's column list omits it. That omission is a spec bug, not a design decision; add the column and record it.

**Every partial unique index must be declared in `__table_args__` as well as in the migration** (Global Constraint 9):

```python
Index("uq_health_alert_open_key", "org_id", "device_id", "check_id", "selector",
      unique=True, postgresql_where=text("state <> 'cleared'")),
```

- [ ] **Step 4: Write the migration**

Migration C. Determine the head first (Global Constraint 7). Copy the standard RLS block out of the most recent module migration rather than trusting this plan.

The 32-check cap is a **statement-level** trigger using a transition table, because a CHECK cannot count siblings and a row-level trigger misses multi-row inserts:

```sql
CREATE FUNCTION health_check_cap() RETURNS trigger AS $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM (SELECT DISTINCT profile_id FROM new_rows) t
    WHERE (SELECT count(*) FROM health_check c WHERE c.profile_id = t.profile_id) > 32
  ) THEN
    RAISE EXCEPTION 'a health profile may hold at most 32 checks (spec §0.11)';
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_health_check_cap AFTER INSERT ON health_check
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION health_check_cap();
```

**Verify this executes** — write the test first and watch it fail, then pass. A trigger that is syntactically valid and semantically wrong is exactly the shape of bug constraint 3 warns about.

**`health_alert.check_id` FK.** Spec §10 says deleting a check leaves existing `health_alert` rows intact, but §2.4 makes `check_id` a non-nullable FK to `health_check`. Those cannot both hold. **Resolution: `health_check` rows are never hard-deleted while alerts reference them** — the API's "replace the check set" path (Task 5) must clear the affected alerts with `cleared_reason='policy_removed'` and then delete, or the FK must be `ON DELETE RESTRICT` with the error naming the alerts. Pick one, implement it, test it, and record it. Do not make `check_id` nullable: it is part of the dedup key.

- [ ] **Step 5: Run tests**

`cd backend && .venv/bin/pytest tests/test_health_migration.py tests/test_health_check_registry.py tests/test_schema_drift.py tests/test_rls.py -v`, then the **full** suite (this touches `app/models/__init__.py`).

- [ ] **Step 6: Prove the tenancy and index tests can fail**

- `WITH CHECK (true)` on one policy → its insert test red. `USING (true)` → its read test red. (Deleting `WITH CHECK` entirely proves nothing — constraint 5.)
- **`uq_health_alert_open_key`:** drop the `WHERE state <> 'cleared'` predicate. **If the predicate is deleted, the re-open-after-clear test observes an `IntegrityError` where it expects a second alert row** — a legitimate re-open becomes impossible. Watch it go red; restore.
- **The assignment partial indexes:** replace all three with a single plain `UNIQUE (org_id, device_id, device_group_id)`. **If they are replaced, the two-device-level-rows test observes two stored rows instead of an `IntegrityError`, because `(org, dev, NULL)` does not collide with itself.** Watch all three go red; restore. This is Global Constraint 8 made observable.
- Paste every output.

**Definition of done:** all eight (or nine, with the ack table) tables exist with RLS forced and behavioural cross-org tests; every partial index is in both the migration and `__table_args__`; the trigger rejects a multi-row over-cap insert; the two spec gaps and the FK resolution are recorded; full backend suite green.

**Commit:** `feat(health): schema for profiles, checks, alerts, and per-device state`

---

## Task 5 — Profiles, checks, assignments, and org settings

**Files:** Create `backend/app/modules/health/profiles.py`, `backend/app/modules/health/settings.py`; Test `backend/tests/test_health_profiles.py`, `backend/tests/test_health_settings.py`

**Interfaces:**
- Consumes: models (T4), `CHECK_TYPES` (T4), `audit_service`.
- Produces: `create_profile`, `update_profile`, `delete_profile`, `get_profile`, `list_profiles`, `clone_profile`, `replace_checks`, `create_assignment`, `delete_assignment`, `list_assignments`; `get_settings` (effective), `update_settings`; `bump_policy_version(session, org_id, profile_id) -> int`.

Requirements:

- **`policy_version` is server-owned.** It increments on **any** write to the profile or to its checks, and **no API accepts it** (spec §2.1). Assert that with a test that PATCHes a profile's `name` and observes `policy_version` incremented — a test that only writes checks would pass against an implementation that bumps in one place.
- **`replace_checks` is a set replacement in one transaction** (spec §10), validated against `CHECK_TYPES`: `comparator` legal for the type, exactly one of `threshold_num`/`threshold_bool` set and **matched to the type's `value_kind`** (SQL cannot consult the registry — this validation is the only thing standing between a boolean check and a numeric threshold), `os_family` legal, `for_ticks` in range.
- **`for_seconds` conversion** (spec §0.3): the service accepts `for_seconds` as an alternative and converts with `ceil(for_seconds / profile.tick_interval_s)` **against the profile's own tick interval**, stores the tick count, and returns both. Test that a 900 s input on a 300 s profile stores `3`, and that the same input on a 60 s profile stores `15`. A test with one tick interval cannot see a hardcoded divisor.
- **`clone_profile`** is a first-class action (spec §0.2's stated mitigation): deep-copies the checks, mints a new name, and creates **no** assignment.
- **Assignment mutual exclusion** and the three-level uniqueness are enforced by the database (T4); the service maps the `IntegrityError` to a typed `HealthValidationError` naming the existing assignment. **Create is `ON CONFLICT` + `RETURNING`, not check-then-insert** — this project has run two sweeps to remove that bug class. Because the targets are **partial** indexes, the upsert must use `index_elements=[...]` + `index_where=...`; `ON CONFLICT ON CONSTRAINT` fails at runtime (Global Constraint 8). **Ship a test that executes each of the three upserts against the real database.**
- A profile referenced by any assignment cannot be deleted; the error names the assignments (spec §10).
- Deleting a check clears the affected alerts with `cleared_reason='policy_removed'` first (the T4 resolution).
- `settings.py` follows `app/modules/sla/settings.py`: **`get_settings` returns effective values and writes nothing** — a `GET` that lazily INSERTs makes a `health.read` request a write. Only the PUT creates the row, and it audits the **old and new** values.
- Audit create/update/delete for profiles, checks, assignments and settings (spec §11.3).
- Every query carries an explicit `org_id` predicate; **at least one test pins that predicate on the admin engine** (constraint 4).

**Guard observability:** if the `value_kind` validation is deleted, the "boolean check with a numeric threshold" test observes a **stored row** with `threshold_num=95` on `posture.disk_encryption` instead of a `HealthValidationError`. If the `org_id` predicate is deleted, the admin-engine test observes org B's profile in org A's `list_profiles` result.

**Definition of done:** CRUD complete and audited, `policy_version` bumps on every write path, the three upserts execute against real partial indexes, the admin-engine predicate test passes and fails on mutation.

**Commit:** `feat(health): profile, check, and assignment configuration`

---

## Task 6 — Effective-profile resolution

**Files:** Create `backend/app/modules/health/resolution.py`; Test `backend/tests/test_health_resolution.py`

**Interfaces:**
- Consumes: models (T4), `CHECK_TYPES`.
- Produces: `resolve_profile_for_device(session, org_id, device) -> ResolvedProfile | None`, `MAX_GROUP_DEPTH = 16`, and `ResolvedProfile` (a frozen dataclass carrying the profile, the **assignment that produced it**, and the OS-filtered check list).

Spec §3, exactly:

1. `health_profile_assignment` where `device_id = device.id`;
2. the nearest ancestor walking `device_groups.parent_group_id` upward, **self first**, depth-bounded at 16;
3. the org default (`device_id IS NULL AND device_group_id IS NULL`).

Only `active` profiles are considered. **Returns `None` when nothing matches** — no fabricated default. The console shows "no health profile", never "healthy".

A check applies iff `check.enabled AND (check.os_family IS NULL OR check.os_family == device.os_family)`, filtered **server-side at resolution time** so an agent is never shipped a check it cannot answer. `agent.offline` (`evaluated_by='server'`) is **excluded** from the pushed set.

`ResolvedProfile` carries the assignment because spec §0.2 requires the device view to show *which assignment produced it* — "a resolution nobody can see is a resolution nobody trusts".

Required tests:

- all three levels resolve;
- **precedence matters**: with a device-level, a group-level and an org-default assignment all present, the device-level wins; remove it and the group-level wins; remove that and the org default wins. *A test with a single candidate cannot detect a reversed order.*
- **the nearest ancestor wins**: device in group C, C's parent B has an assignment, B's parent A also has one → B's is chosen;
- **cycle safety**: build `A.parent = B, B.parent = A`, assign a profile at the org default, and assert resolution returns the **org default** and logs at ERROR with the device id — not a hang, not an exception (spec §0.2's degrade-don't-fail posture);
- no match → `None`, and **no** `health.policy.apply` is queued;
- an inactive profile is skipped even when a device-level assignment points at it (and the walk then falls through — assert the org default is returned, not `None`);
- OS filtering: a `posture.antivirus_healthy` check is absent from a Linux device's resolved set and present for Windows;
- `agent.offline` is never in the resolved *push* set but **is** in the evaluated set the offline sweep uses.

**Guard observability:** if `MAX_GROUP_DEPTH` is deleted, the cycle test **hangs** rather than failing — so give it a `pytest.mark.timeout` or assert on the ERROR log record, and say in the test docstring which it is. If the `active` filter is deleted, the inactive-profile test observes the inactive profile returned instead of the org default.

**Definition of done:** all three levels, precedence, nearest-ancestor, cycle bound, OS filter and the `None` case are tested and the mutations above are demonstrated.

**Commit:** `feat(health): effective-profile resolution with a depth-bounded group walk`

---

## Task 7 — Job types and the policy push

**Files:** Create `backend/app/modules/health/jobtypes.py`, `backend/app/modules/health/policy_push.py`; Modify `scripts/export_wire_schema.py`; Test `backend/tests/test_health_jobtypes.py`, `backend/tests/test_health_policy_push.py`

**Interfaces:**
- Consumes: `resolve_profile_for_device` (T6), `JobEngine.create`, `app.jobs.registry.register`.
- Produces: `HealthCheckSpec`, `HealthPolicyApplyParams`, `HealthCheckNowParams`, `register_job_types()`; `build_policy_params(resolved) -> HealthPolicyApplyParams`, `push_policy(session, job_engine, org_id, device) -> tuple[UUID, dict] | None`, `record_policy_ack(session, job, result) -> None`.

Params models exactly as spec §5.2 writes them — `extra="forbid"`, `threshold` a **string**, `checks` bounded at 32, the interval bounds matching the DB CHECKs. Register both types through `app/jobs/registry.py`; register a `result_handler` for `health.policy.apply` that upserts `device_health_policy_ack` so the reconciliation sweep does not re-push for a whole summary interval.

**`push_policy` does not publish.** It returns `(job_id, envelope)` for the caller to publish **after** the `org_session` block exits — the `deploy/scripts.py` contract (Global Constraint 15). Returning `None` when resolution yields no profile is the "no checks pushed" case.

**An empty `checks` list is a legal policy and means "run nothing"** (spec §5.2). Test it explicitly: unassigning a device's profile pushes a policy with `checks=[]` — a **disarm must be an explicit instruction, not the absence of one**. An implementation that skips the push on an empty set leaves the device running its last policy forever, and the test that catches that is the one asserting a job **was** created.

Required tests:

- both job types are registered and `registered_names()` contains them;
- `build_policy_params` emits `cadence` from `CHECK_TYPES`, not from the row, and never emits `agent.offline`;
- 33 checks cannot be built (the DB caps at 32; assert the Pydantic bound too);
- the created job's `type`, `target_type='device'`, `target_id` and `params` are correct, and its `signature` is **non-NULL** (the signed path);
- the ack handler stamps `device_health_policy_ack.policy_version` and is idempotent under redelivery;
- `push_policy` returns `None` and creates **no** job when the device has no profile;
- the unassign case pushes `checks=[]`.

Add `HealthPolicyApplyParams` and `HealthCheckNowParams` to `scripts/export_wire_schema.py` and regenerate.

**Definition of done:** both types registered, the policy build is driven by the registry, the disarm case is tested, schemas regenerated and committed.

**Commit:** `feat(health): the signed policy-apply and check-now job types`

---

## Task 8 — Flap suppression

**Files:** Create `backend/app/modules/health/flap.py`; Test `backend/tests/test_health_flap.py`

**Interfaces:**
- Consumes: `HealthFlapState` (T4), `get_settings` (T5).
- Produces: `note_open(session, org_id, key, *, now) -> FlapDecision` where `FlapDecision` is a frozen dataclass `(suppressed: bool, flap_count: int, suppressed_until: datetime | None)`.

Spec §0.9's algorithm, verbatim:

```
if now - window_start > flap_window_s:   window_start = now; open_count = 0
open_count += 1
if open_count > flap_threshold:
    suppressed_until = now + flap_cooldown_s
    -> suppressed
```

**The alert record is still written.** Suppression removes the *actions*, never the evidence. `flapping` and `flap_count` are columns on the alert precisely so the console can sort by them — a flapping device is a finding, often more important than any single breach.

Required tests:

- **the boundary, both sides**: `open_count` reaching exactly `flap_threshold` does **not** suppress; one more does. *(A test with only one of these passes against `>=` or `>` indifferently.)*
- a window older than `flap_window_s` resets `window_start` and `open_count`;
- while inside `suppressed_until`, further opens stay suppressed;
- one row per key, upserted — 50 opens produce **one** `health_flap_state` row (this is also part of §0.12's boundary);
- the org tunables are read from `health_org_settings` and the CHECK ranges are enforced by the database (`flap_window_s` 299 raises, 86401 raises; `flap_threshold` 1 and 51 raise; `flap_cooldown_s` likewise) — **on the admin engine**;
- cross-org: two orgs with the same `(device_id, check_id, selector)` shape do not share a flap window. Pin the app-layer `org_id` predicate on the **admin engine**.

**Guard observability:** if the `open_count > flap_threshold` comparison is weakened to `>=`, the at-threshold test observes `suppressed=True` where it expects `False`. If the window reset is deleted, the reset test observes `open_count=5` where it expects `1`.

**Definition of done:** the algorithm's boundary is pinned from both sides; one row per key is asserted by count; the tunable ranges are DB-enforced.

**Commit:** `feat(health): flap suppression that bounds actions, not evidence`

---

## Task 9 — Ingestion and the open/clear predicates

**This is the heart of the module. Read spec §0.7, §0.8 and §6 in full before writing anything.**

**Files:** Create `backend/app/modules/health/ingest.py`; Modify `backend/app/gateway/dispatch.py` (replace Task 2's interim branch); Test `backend/tests/test_health_ingest.py`, `backend/tests/test_health_clear_predicate.py`

**Interfaces:**
- Consumes: `HealthReportMsg` (T2), `resolve_profile_for_device` (T6), `note_open` (T8), `publish` (kernel), `queue_ui_event` (kernel).
- Produces: `ingest_report(session, device, msg) -> IngestOutcome`, `HealthIngestError`.

Per report line, in **one** transaction (spec §6):

1. Resolve `(check_id, selector)` against the device's **currently effective** profile. Unknown or removed → discard, increment the outcome's counter, **continue**. **Never raise** — one stale line must not reject a whole batch that also carries a recovery.
2. UPSERT `device_health_check_state` (§2.6) — `index_elements=["org_id","device_id","check_id","selector"]`.
3. `kind='breach'` → evaluate the OPEN predicate; on success write the alert **plus** one `health_alert_action` row per configured action in state `pending`, **and** publish `health.alert_raised` — all in the same transaction.
4. `kind='recovery'` → CLEAR clause 1, `cleared_reason='recovery'`.
5. `kind='summary'`, `state='ok'` → clause 2, `cleared_reason='summary_ok'`.
6. `kind='summary'`, `state='unavailable'` **and** the alert is older than `unavailable_clear_after_s` → clause 3, `cleared_reason='unavailable_timeout'`.

The gateway branch replaces Task 2's early return: resolve the `Device` from the certificate binding, call `ingest_report`, audit `health.ingest_rejected` for a **rejected batch** (never per line), and `queue_ui_event` so the console refreshes only after commit. Follow `_handle_security_events` exactly. **Delete Task 2's interim test and its comment.**

Required tests — one per numbered clause, plus the negatives that carry the design:

**OPEN predicate**
- a breach for a check **not** in the currently effective profile is discarded, counted, and the rest of the batch still applies (put a valid recovery in the same batch and assert it cleared);
- `policy_version` older than `acknowledged - 1` is discarded; exactly `acknowledged - 1` is **accepted** (one version of tolerance so a breach in flight across a policy push is not lost);
- **five identical breach reports produce ONE alert, ONE ticket row, ONE remediate action row** — assert on counts, not on absence of error;
- a suppressed key still **writes** the alert with `flapping=true`, and its action rows are `blocked_flapping`;
- `unavailable` **never** opens an alert.

**CLEAR predicate — six tests, one per `cleared_reason`, plus the exclusion**
- `recovery`, `summary_ok`, `unavailable_timeout` (and its negative: **not** cleared before the timeout), `policy_removed` (T5's path), `device_inactive` (T10's path), `manual` (T10's path);
- **the clause deliberately NOT present**: `test_an_offline_device_does_not_clear_its_open_alerts`. Take a device with two open alerts, mark the agent offline past `offline_after_s`, run the offline sweep, and assert both alerts are **still open** and a **third** alert (`agent.offline`) has opened alongside them. *Without this test, the implementation that "recovers" everything at the instant a machine dies passes.*

**The monitoring boundary**
- `test_no_health_table_grows_with_time_for_a_stable_device`: run **100** summaries for one stable device and assert every row count in every health table is unchanged (spec §0.12, §14). This is the property test for "what was this device's CPU last Tuesday?" being unanswerable.

**Tenancy**
- a report arriving on org A's certificate binding for a device belonging to org B is rejected and audited `gateway.identity_mismatch` — the existing `_handle_security_events` path already does this; assert it, do not re-implement it;
- the app-layer `org_id` predicate inside `ingest_report` is pinned on the **admin engine** (constraint 4).

**Guard observability, stated per guard:**
- delete step 1's profile check → the stale-check test observes a **stored alert row** for a removed check instead of a discard counter of 1;
- delete the `policy_version` tolerance → the `acknowledged - 1` test observes zero alerts where it expects one;
- delete the in-code duplicate check (leaving only the index) → the five-identical-reports test still passes, so **it does not falsify anything on its own**; the test that falsifies it asserts `occurrence_count == 5` and `last_seen_at` advanced, which an `IntegrityError`-catching path would not produce;
- delete the `unavailable` exclusion → the `unavailable`-never-opens test observes an alert row.

**Definition of done:** every OPEN clause and all six CLEAR clauses have a test; the offline-does-not-clear negative passes; the 100-summary boundary test passes; the gateway branch is live and Task 2's interim test is gone; full backend suite green (this touches `app/gateway/dispatch.py`).

**Commit:** `feat(health): report ingestion, alert dedup, and the six clear predicates`

---

## Task 10 — Alert state transitions

**Files:** Create `backend/app/modules/health/alerts.py`; Test `backend/tests/test_health_alerts.py`

**Interfaces:**
- Consumes: models (T4), `publish`, `audit_service`.
- Produces: `acknowledge`, `mute`, `close` (manual), `clear_for_policy_change`, `clear_for_device_inactive`, `list_alerts`, `get_alert`, `link_previous_alert`.

Per spec §0.8:

- **Acknowledge** (`active → acknowledged`): a named user takes ownership. Suppresses re-notification and escalation. Does **not** stop the check, does **not** prevent auto-clear, and does **not** cancel a remediation already in flight. Test all three negatives — they are the whole content of the state.
- **Mute** (`active|acknowledged → muted`) **requires** `muted_until`, bounded to 30 days by the DB CHECK. On expiry the sweep (T12) returns the alert to `active` and re-notifies **once**.
- **Manual close** requires `health.alert.manage`, sets `cleared_reason='manual'` and `cleared_by`.
- `previous_alert_id` links to the last cleared alert for the same key within `flap_window_s`, so a re-opened alert's ticket can name its predecessor.
- Every transition is audited (`health.alert_acknowledged` / `_muted` / `_closed`) and publishes `health.alert_cleared` where it clears.

Required tests: each transition, each illegal transition (`cleared → acknowledged` raises), the mute bound at the DB layer, `previous_alert_id` set on a re-open inside the window and **NULL** on a re-open outside it, and cross-org 404 (**404, never 403**, for another org's alert).

**Guard observability:** if acknowledge is allowed to suppress auto-clear, the "acknowledged alert still auto-clears on recovery" test observes `state='acknowledged'` where it expects `'cleared'`. If the mute bound is deleted, the 31-day mute test observes a stored row.

**Definition of done:** every state edge tested including the illegal ones; `previous_alert_id` behaviour pinned on both sides of the window.

**Commit:** `feat(health): alert acknowledge, mute, and manual close`

---

## Task 11 — The action worker: remediate → open_ticket → notify

**Read spec §0.10, §7 and §8 in full. This is the task with the largest blast radius: it puts signed jobs on endpoints.**

**Files:** Create `backend/app/modules/health/actions.py`, `backend/app/modules/health/approved_actions.py`; Test `backend/tests/test_health_actions.py`, `backend/tests/test_health_approved_actions.py`, `backend/tests/test_health_egress.py`

**Interfaces:**
- Consumes: `ApprovedAction`/`is_destructive`/`render_params_template` (T3), `JobEngine.create`, `tickets.create_ticket`, `transitions.change_status`, `publish`.
- Produces: `execute_pending_actions(session, org_id, *, now) -> dict[str,int]`, `approve_action`, `refuse_action`, `dispatch_manual_remediation`; and the CRUD/validation service over the kernel registry.

**Order is fixed, not configurable: `remediate` → `open_ticket` → `notify`** (spec §0.10). Remediation's job id is context the other two carry; the ticket must exist before the mail can name a number that resolves; notify is last because it is the only irreversible external side effect.

**`remediate`**, per spec §7.1:
1. load the `approved_action`; refuse if inactive, if its `job_type` is no longer in `registered_names()`, or if `"health" not in personas`;
2. evaluate the fuses **against `jobs`** (spec §2.9 — `jobs` is already the durable audited record; a second counter would be a second truth): `cooldown_s` since the last dispatch of this action to **this device**, `max_per_device_per_day`, `max_per_org_per_hour`. Any breach → `blocked_rate_limit` with the reason in `last_error`. **Blocked, not queued** — a remediation deferred an hour is a remediation applied to a situation that has changed;
3. apply the autonomy dial (§8.3): `suggest` → nothing dispatched; `require_approval` → `awaiting_approval` + publish `health.remediation_requires_approval`, expiring to `expired` after `approval_ttl_s`; `autonomous` → dispatch;
4. `JobEngine.create(...)` with `target_device_id: UUID` — **a single UUID, no target union, no group path** — then publish the envelope **after commit**.

A **failed** remediation does **not** clear the alert and does **not** re-fire (`UNIQUE (alert_id, kind)`); it posts a comment on the ticket if one exists and leaves the alert open for a human. *Auto-remediation that retries itself is how a service-restart loop becomes an outage.*

**`open_ticket`**, per spec §7.2: `create_ticket(..., origin="alert", source_alert_id=alert.id)` with `type="incident"`. **Nothing here needs building** — `create_ticket` already enforces `origin='alert' ⇒ type='incident'`, already forces `requester_id`/`created_by` to NULL for alert origin, and already refuses `source_alert_id` for other origins, with `ck_ticket_human_attribution` behind it. Assert those behaviours; do not re-implement them. The description carries hostname, check type, selector, comparator, threshold, observed value, opened-at, the remediation action and job id if dispatched, and the previous alert's ticket number when `previous_alert_id` is set. **No raw log text, no script body.**

**`notify`** publishes nothing itself (§7.3) — the `health.alert_raised` event is already in the outbox from ingestion. **Spec gap, flagged:** §7.3 says a check *without* a notify action publishes the event with `"notify": false` and "the org's rule set is expected to scope on it", but `notification_rules` scope only on `event_type` and `queue_id` — **`routing.resolve_recipients` cannot read a payload flag**, so `notify: false` currently changes nothing and the "silent by design" check is not silent. Decide and record: either (a) suppress the outbox publish entirely when no `notify` action is configured — simple, and the alert is still visible in the console, which is what "console-only" means — or (b) extend `notification_rules` with a scoping column, which is a notification-module change and a bigger commitment. **(a) is recommended.** Whichever you pick, ship a test that observes a *delivered recipient count*, not the presence of the flag.

**`auto_resolve_alert_tickets`** (§7.4): on clear, if the ticket has **not been touched by a human** — no `user`-actor `ticket_status_history` row, no `user`-authored comment, no assignee — drive it to **`resolved`** (never `closed`) via `change_status(..., actor_type="system", actor_id=None)` and post a system comment naming the recovery and its `cleared_reason`. If it **has** been touched, post the comment and change nothing else. Test both branches and each of the three "touched" signals independently — a test with only one signal passes against an implementation that checks only that one.

**Approved-action service validation** (spec §8.2): `job_type` validated against `registered_names()` at **write** time (a 400 at the API, never an `UnknownJobType` at 03:00 with an alert already open); `script_id` + `script_version` **required** for `script.run` and forbidden otherwise; `destructive` **derived and stored** via `is_destructive`, never accepted from the request body; `autonomous` refused at write time when destructive/high/non-device — API validation **and** the T3 CHECK. Every autonomy-dial change is audited with **old and new** values (`health.action_autonomy_changed`); every script exemption is audited with `(script_id, script_version)` (`health.action.destructive_exempted`). Every remediation dispatch is audited `health.remediation_dispatched` with `alert_id`, `action_key`, `job_id`, `autonomy`, `approved_by` — **in addition to** the kernel's `jobs.dispatched`, because the kernel record says a job was sent and this record says *why a machine decided to send it*.

**`test_health_egress.py` — the single-egress-path test** (spec §14). Grep every source file under `app/modules/health/` for `publish_job`, `send_text`, `httpx`, `requests`, `subprocess` and assert the only call sites are the two `publish_job` calls the design permits (policy push, remediation dispatch), each in a function that takes `device_id: UUID`. Pin the notification module's single-egress precedent. **Guard observability:** if a second egress path is added — say a direct `send_text` in `actions.py` — this test observes **three** call sites where it expects two, and names the file.

Required tests beyond the above:
- **idempotency**: a sweep re-run after a crash mid-action produces **no second ticket and no second job** — assert against `UNIQUE (alert_id, kind)` by counting rows, not by catching an exception;
- **fuses**: 21 alerts in one hour with `max_per_org_per_hour=20` produce **20 dispatched** and **1 `blocked_rate_limit`** — this is §8.4's real fleet-wide risk (4,000 device-scoped jobs in one minute because a DNS outage tripped one check estate-wide), so the test must run the *batch*, not a single action;
- **the signed path**: the dispatched job's `signature` is non-NULL over the canonical bytes;
- **RBAC**: a `health.admin`-only user is refused `POST /alerts/{id}/remediate`; a `health.remediate` user is refused the `approved-actions` write routes (assert at the service/permission layer here; the route-level assertion is Task 13);
- a `dead` remediation does **not** block the ticket or the notification — the three actions are independently retryable and independently terminal.

**Known semantic issue to record (do not paper over):** the fuses count rows in `jobs` filtered by `(org_id, type, target_id, created_at)`. Two approved actions sharing a `job_type` (two different `script.run` actions) therefore share a fuse, and an operator running scripts manually consumes the remediation budget. Spec §2.9 chose `jobs` deliberately over a second counter table; note the consequence in your report and in the spec, and consider narrowing the predicate with `jobs.requested_by IS NULL` if that is what the code actually distinguishes — **verify before assuming**.

**Definition of done:** the three actions execute in the fixed order, the fuses bound a batch, the autonomy dial is enforced at both layers, the egress test names exactly two call sites, and every audit action in spec §11.3 is written.

**Commit:** `feat(health): the alert action worker, fuses, and the autonomy dial`

---

## Task 12 — The five sweeps and the beat entries

**Files:** Create `backend/app/modules/health/sweep.py`; Modify `backend/app/worker.py`; Test `backend/tests/test_health_sweep.py`

**`app/worker.py` is shared — flag it in your report.**

**Interfaces:**
- Produces: `run_alert_actions`, `run_alert_sweep`, `run_offline_sweep`, `run_policy_reconcile`, `run_retention`, each with a `sweep_all_orgs`-shaped wrapper; interval constants `ACTION_INTERVAL_S = 30.0`, `ALERT_SWEEP_INTERVAL_S = 60.0`, `OFFLINE_SWEEP_INTERVAL_S = 60.0`, `POLICY_RECONCILE_INTERVAL_S = 900.0`, `RETENTION_INTERVAL_S = 86400.0`.

Spec §12's five entries, following `app/worker.py`'s established per-org shape — a thin scheduling shell, with the logic in `app/modules/health/` and tested directly:

| task | interval | does |
|---|---|---|
| `health-alert-actions` | 30 s | execute `pending`/approved action rows (T11), with **claim-before-execute** and a stuck-claim sweep on the notification module's 10-minute precedent |
| `health-alert-sweep` | 60 s | mute expiry → `active` + **one** re-notify; escalation once per alert (`escalate_after_s`, stamped by `escalated_at`); `unavailable_timeout` clears; approval expiry |
| `health-offline-sweep` | 60 s | the server-side `agent.offline` check against `agents.last_seen_at`/`agents.status` |
| `health-policy-reconcile` | 900 s | dispatch `health.policy.apply` where the acknowledged `policy_version` is stale |
| `health-retention` | 86400 s | delete `health_alert` past `alert_retention_days`; prune `health_flap_state` for keys with no alert in a flap window |

**Intervals are project constants, not per-org configuration** — the SLA sweep's reasoning applies unchanged: detection granularity is an operational property, and a per-org interval lets one org's setting delay another's escalation.

`register_worker_job_types()` gains this module's registration call, for the reason its docstring already gives: the worker process dispatches jobs (`health.policy.apply`, remediations) without importing the API's registration path, so without it the **first sweep would raise `UnknownJobType`**. Ship a test that calls `create_celery()` and asserts `health.policy.apply` is in `registered_names()` — the `test_create_celery_registers_*` precedent.

Required tests:
- **idempotency**: running each sweep twice produces one escalation, one re-notify, one offline alert. Assert on **counts**;
- **escalation fires once**: `escalated_at` stamped; a second pass publishes nothing. *A sweep that re-fires every pass turns one problem into an hourly mail.*
- escalation **skips** `acknowledged` and `muted` alerts (both negatives, separately);
- mute expiry returns to `active` and re-notifies **exactly once** — assert the outbox row count;
- the offline sweep opens `agent.offline` and, per T9's negative, **leaves the device's other alerts open**;
- the reconcile sweep pushes only where the ack is stale — a device whose ack matches gets **no** job (assert zero rows created, not "no exception");
- retention deletes past the boundary and **not** at it, and cascades `health_alert_action` rows;
- one org's sweep does not touch another's — pinned on the **admin engine**;
- the five beat entries are registered with the right task names and intervals.

**Guard observability:** if `escalated_at` is not stamped, the twice-run test observes **two** `health.alert_escalated` rows in `domain_events` where it expects one. If the acknowledged/muted skip is deleted, the negative tests observe an escalation event for an acknowledged alert.

**Definition of done:** all five sweeps idempotent under a second run, `register_worker_job_types` wired and tested, beat entries asserted; full backend suite green (this touches `app/worker.py`).

**Commit:** `feat(health): the alert, offline, reconcile, and retention sweeps`

---

## Task 13 — API

**Files:** Create `backend/app/modules/health/api.py`, `backend/app/modules/health/schema.py`; Modify `backend/app/modules/health/__init__.py`, `backend/app/modules/__init__.py`; Test `backend/tests/test_health_api.py`

Routes exactly as spec §10 tabulates, under `/api/v1`, literal segments before `{id}`.

Requirements:

- Handlers stay thin: resolve principal, call service, map errors. `HealthNotFoundError` → 404, `HealthValidationError` → 400. **Let the error escape the `org_session` block** so the transaction rolls back; map it outside. Catching inside leaves partial writes committed.
- **404, never 403**, for another org's profile, alert or approved action.
- Checks are replaced as a **set** (`PUT /health/profiles/{id}/checks`), mirroring the SLA spec's window handling: a profile's checks only make sense together, and a partial edit can leave a profile that no longer matches the `policy_version` an agent acknowledged.
- `GET /devices/{device_id}/health` is gated on **`devices.read`**, not `health.read` — a technician looking at a device needs its health without holding health administration rights. Ship a test with a `devices.read`-only user.
- `POST /devices/{device_id}/health/check-now` dispatches `health.check.now` and publishes **after commit**.
- Every write route that changes a profile, check or assignment queues a `health.policy.apply` push for the affected devices, published after commit.
- RBAC completeness derived from the router via `route_permission` — **use the flattener from `backend/tests/test_email_intake_api.py`** (Global Constraint 13). **Pin the exact route count**; a `>=` assertion hides a missing route.
- Register in `app/modules/__init__.py` next to the other modules.

Required tests: one per route for the happy path and the 403; the cross-org 404s; the `devices.read` route with a non-health user; the `health.admin`-cannot-remediate and `health.remediate`-cannot-write-approved-actions pairs at the route layer; `destructive` supplied in a request body is **ignored** and the stored value is the derived one; an unregistered `job_type` on an approved action is a **400**.

**Guard observability:** remove `require_permission` from exactly one route and the completeness test must go red naming that route. **Prove it and paste the output.** If it stays green, the flattener is not reaching `include_router`'d routes — fix the test, not the assertion.

**Definition of done:** every spec §10 route present, gated, and counted; the completeness mutation demonstrated; full backend suite green (this touches `app/modules/__init__.py`).

**Commit:** `feat(health): the health, alert, and approved-action API`

---

## Task 14 — Frontend, documentation, and the live smoke test

**Files:** Create `frontend/src/modules/health/{api.ts,types.ts,AlertConsole.tsx,AlertDetail.tsx,ProfileList.tsx,ProfileEditor.tsx,DeviceHealthPanel.tsx,ApprovedActionList.tsx}` and tests; Modify `docs/modules/endpoint-health-alerting/spec.md`, `docs/modules/service-desk/spec.md`, `docs/05-agent-spec.md`

**Frontend** per spec §13 — TanStack Query, strict TypeScript, **no `any`**, matching `frontend/src/modules/notification/`'s conventions. Six behaviours must be tested **on rendered output**:

1. `unavailable` counts render as a **distinct band**, never folded into "healthy" (§0.7). A device that cannot answer a check is not a healthy device.
2. A device with **no** effective profile renders "no health profile", **not** "healthy" (§3). This is the single most dangerous default a monitoring module could pick.
3. The device view names **which assignment produced** the effective profile (§0.2) — a resolution nobody can see is a resolution nobody trusts.
4. A check's duration renders in **both** ticks and minutes, with the profile's tick interval named (§0.3) — "3 ticks (≈15 min at this profile's 5-minute tick)".
5. `autonomy='autonomous'` renders **distinctly** in the approved-action list (§13) — a page where "runs itself on 400 machines" looks like every other row is a page that gets clicked past.
6. A `require_approval` action renders the pinned script's **name and version** — nobody approves a script they cannot identify.

**Reporting** (§7.4's obligation, §13): alert volume, MTTR and auto-remediation success rate **must** separate machine-resolved (`ticket_status_history.actor_type='system'` on an `origin='alert'` ticket) from human-resolved, and **must exclude `cleared_reason='unavailable_timeout'`** from any resolution count — an alert cleared because the sensor was removed is not a fixed problem. A dashboard that conflates them is a defect in the report, not in the rule.

**Do not write a test that asserts `Object.keys()` over a fixture defined in the same file.** That test cannot fail; this project has shipped one.

**Documentation.** Record every decision the build forced, with its reason:
- in **this module's spec**: the `device_health_policy_ack` table (T4), the `health_ingest_stats` resolution (T4), `escalated_at` on `health_alert` (T4), the `check_id` FK resolution (T4), the script-exemption storage (T3), the `notify: false` resolution (T11), the fuse-sharing consequence (T11), and the **real** migration numbers in place of §2/§16's "0040";
- `docs/modules/service-desk/spec.md` §7.2 — retitle "Phase 15" → "Phase 16" and replace "reserved, not built" with a pointer at this spec's §7.2, since the contract **is** implemented;
- `docs/05-agent-spec.md` — add `health.check` to the capability table ("agent-local timer per signed policy; dormant between ticks; emits breach/recovery/summary only"), and correct the enrollment step that claims a resolved policy is returned in the enrollment response (`EnrollResponse` has no policy field).

**Agent-side obligations, recorded not implemented** (Global Constraint 16): the `health.check` capability, its reuse of the security-logs OS event subscription for the `eventlog.*` counters (**it opens no subscription of its own**), and the idle-budget benchmark asserting §0.5's falsifiers — RSS delta ≤ 2 MB, zero per-tick allocation proportional to elapsed ticks, posture probes off the fast tick, no network write on a quiet tick. State plainly in your report that none of these is built and that **nothing in this repo's CI covers any of them**. *(Amended 2026-08-05: these were recorded as "agent-repo" work. They now belong under `agent/` here, which makes them reachable rather than deferred to somebody else's tree — and leaves their status exactly where this task left it: unbuilt, unmeasured.)*

**The live smoke test** — a script under the scratchpad directory driving real services against a real Postgres and Redis, not mocks:

1. Create an org, a device (Windows), a profile with a `disk.free_pct < 10, for_ticks=3` check carrying a `remediate` action pointing at a `require_approval` approved action, and an org-default assignment.
2. Assert a `health.policy.apply` job was created, is signed, and carries exactly the OS-applicable checks.
3. Feed three `breach` reports through `handle_agent_message` on the device's binding → assert **one** alert, **one** `health.alert_raised` event, action rows `awaiting_approval` + `pending`.
4. Feed two more identical breaches → assert still **one** alert, `occurrence_count` advanced, **no** second ticket.
5. Approve the remediation → run the action sweep → assert **one** signed `jobs` row targeted at that device and a `health.remediation_dispatched` audit record.
6. Feed a `recovery` → assert the alert is `cleared` with `cleared_reason='recovery'` and the untouched ticket auto-**resolved** (not closed) by a `system` actor.
7. Take the device offline past `offline_after_s`, run the offline sweep → assert an `agent.offline` alert opened and **any other open alert is still open**.
8. Run every sweep **again** → assert nothing changed.

Steps 4, 7 and 8 matter most: they are the only end-to-end checks that dedup, the offline exclusion, and sweep idempotency hold against the real database rather than against a fixture. **Report plainly what the script did and did not cover** — if a step could not be driven end to end, say so rather than substituting a mock.

**Verify:** full backend suite, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`, frontend `npm test` and `npx tsc --noEmit`. Record the counts against the baseline from Global Constraint 11.

Commit; do **not** push and do **not** open a pull request.

---

## Self-review

**Spec coverage.** §0.1 closed registry → T4 (`registry.py` + the DB enum, cross-pinned). §0.2 profiles/assignments and the partial indexes → T4, T5, T6. §0.3 ticks not wall-clock, `for_seconds` conversion → T5. §0.4 policy-as-signed-job → T7. §0.5 idle budget → **agent-side, unbuilt**, recorded in T14. §0.6 `agent.offline` server-side → T6 (excluded from push), T12 (the sweep). §0.7 `unavailable` → T9, T14. §0.8 open/clear predicates → T9, T10. §0.9 flap suppression → T8. §0.10 three actions, order, idempotency → T11. §0.11 the caps as constraints → T4. §0.12 the monitoring boundary → T4 (schema), T9 (the 100-summary test). §2 data model → T3 (`approved_action`), T4 (the rest). §3 resolution → T6. §4 check registry → T4; §4.2/4.3 tick behaviour → agent-side, unbuilt. §5.1 wire → T2; §5.2/5.3 job types → T7. §6 ingestion/escalation → T9, T12. §7 the three actions → T11. §8 governance → T3, T11. §9 events and their four-part obligation → T1. §10 API → T13. §11 RBAC/tenancy/audit → T1, T3, T4, T5, T11, T13. §12 worker → T12. §13 frontend → T14. §14 testing → distributed, each named in its task. §16 migration notes → T1, T3, T4 (with Global Constraint 7 overriding the numbers). §17 deferrals → nothing built. §18 doc changes → T14.

**Kernel tasks:** T1 (events, permissions, notification CHECKs), T2 (the wire kind), T3 (the `approved_action` registry). T12 additionally edits `app/worker.py` and T13 edits `app/modules/__init__.py`; both are shared, neither is a kernel *contract* change.

**Files owned by another module or by the kernel, flagged:** `app/core/events.py`, `app/core/rbac.py`, `app/core/approved_actions.py`, `app/models/__init__.py`, `app/gateway/protocol.py`, `app/gateway/dispatch.py`, `app/jobs/registry.py`, `app/worker.py`, `app/modules/__init__.py`, `scripts/export_wire_schema.py`, `docs/kernel/wire-protocol/` (kernel); `app/modules/notification/delivery.py` and `backend/tests/test_notification_delivery.py` (notification); `docs/modules/service-desk/spec.md` and `docs/05-agent-spec.md` (other modules' docs). This module **calls** `service_desk/tickets.py` and `transitions.py` but edits neither — spec §15.2 verified both entry points already implement the alert→ticket contract.

**Gaps this plan found in the spec and assigns to a task rather than leaving to a guess.** `device_health_policy_ack` is referenced by §5.2, §0.8 and §12 but defined nowhere (T4). `health_ingest_stats` is referenced by §0.8 but contradicted by §0.12's "exactly three tables retain anything" (T4). `escalated_at` is required by §6 but missing from §2.4's column list (T4). `health_alert.check_id` is a non-nullable FK while §10 promises alerts survive check deletion (T4). The script destructive-exemption has a function parameter and an API route but no storage (T3). §8.2's "per-job-type allowlist of which keys may receive a substitution" is named but not enumerated (T3). §7.3's `notify: false` marker cannot be acted on by `routing.resolve_recipients`, which scopes only on `event_type` and `queue_id` (T11). §2.9's fuses are evaluated against `jobs` filtered by `type`, so two approved actions sharing a job type share a fuse and manual operator jobs consume it (T11).

**Known risk this plan does not remove.** Tasks 5–12 each build fixtures over a nine-table model, and the module's most important properties — dedup, the offline exclusion, sweep idempotency — are exactly the ones a permissive fixture hides. Global Constraint 3 and Task 14's smoke test exist for that, and the smoke test is not optional.
