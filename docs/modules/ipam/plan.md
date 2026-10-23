# IPAM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hold the organisation's address plan — VRFs, sites, VLANs, subnets, ranges, addresses — and continuously reconcile it against what the fleet actually reports, so the plan's defects are a worklist rather than an outage discovery.

**Architecture:** Ten org-scoped tables in native `inet`/`cidr`/`macaddr`. Hierarchy is *derived* from containment, never hand-set. Addresses are sparse — a row exists only once something has been said about that address; "free" is the absence of a row. Two evidence streams write `ipam_observation`: inventory NIC data (passive, no new agent work) and the `net.sweep` capability (active, deny-by-default authorized per prefix). A daily Celery beat sweep materialises `ipam_drift` with a stable identity, and publishes an event only on the second consecutive detection. The module registers into the kernel — one job type, one params model, one result handler, two beat entries, one migration, one router — and forks nothing.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Pydantic v2, Alembic, Celery, Postgres RLS + GiST, React + TypeScript. Stdlib `ipaddress` and `hashlib` only — **no new dependency, and no new Postgres extension** (`inet`, `cidr`, `macaddr`, `inet_ops` are all core).

---

## Global Constraints

Every task's requirements implicitly include this section. These are earned lessons from shipped defects in this repository, not style preferences.

### Authority

- **Authority is `docs/modules/ipam/spec.md`** (committed as `9fbd7df`). Where this plan and the spec disagree, the spec wins — **except** where the *code* disagrees with both.
- **Trust reality over the plan.** If a task's brief here contradicts the code you are looking at, **the code wins**. Say so in your report and record the deviation; do not implement a brief you have just proven wrong. This plan's author read the code but did not run it. Several "verified" claims in the spec are recorded in "Spec corrections" below precisely because they were checked and found wrong.

### Migrations

- **Do not hardcode a migration number.** The head is moving under concurrent work: at the time of writing `backend/migrations/versions/0039_sla_breach_sweep_indexes.py` exists but is **untracked**, and other agents are committing. Confirm the head at build time (`cd backend && .venv/bin/alembic heads`, and read the newest file in `migrations/versions/`), then set `down_revision` to that exact bare-string revision id. The spec's "migration expected `0040`" is an expectation, not an instruction.
- **Migrations are forward-only.** Never edit a shipped migration; add a new one.

### Tenancy (invariant #5)

- Tenancy GUC is `app.current_org`, read as `current_setting('app.current_org', true)::uuid`. Every new table gets `ENABLE ROW LEVEL SECURITY` **and** `FORCE ROW LEVEL SECURITY` and a `tenant_isolation` policy carrying **both** `USING` and `WITH CHECK`.
- **For an `ALL`-command policy Postgres reuses `USING` as `WITH CHECK` when the latter is omitted.** Deleting the `WITH CHECK` clause therefore falsifies nothing — a test that "proves" a hole by deleting it is decoration. **The mutations that actually kill these tests are `WITH CHECK (true)` for an insert test and `USING (true)` for a read test.** Wherever this plan says "watch it go red", use those two.
- **RLS masks application predicates.** A cross-org test driven through `org_session` passes even with the service's own `org_id` predicate deleted, because RLS rejects first. To pin an application-layer `org_id` predicate you must exercise it on the **admin engine**, where RLS is not the thing rejecting. This defect shipped four times in the previous sub-project. Every service in this module that filters by `org_id` needs at least one admin-engine test.

### Guard tests

- **Fixture blindness is the recurring defect class in this repo.** A guard test must make the guarded and the unguarded path produce **different observable values**. If the fixture is built so both paths agree, the assertion passes against the bug it exists to catch.
- **Every task that adds a guard must state, in its test docstring, what the test would observe if the guard were deleted** — and the builder must actually delete it once, watch the test go red, and restore it. "No exception was raised" is not an observation. Assert on stored state, on row counts, on the value in the column.

### Postgres NULL semantics

- **NULLs do not collide in a Postgres unique constraint.** `(a, NULL)` can be inserted twice. The spec's `vrf_id NOT NULL` + lazily-created `global` row (§0.7) exists precisely for this reason — **do not quietly reintroduce a nullable scoping column** anywhere in this module.
- Two of the spec's own unique keys still contain a nullable column and are therefore not unique. Both are fixed in Task 1; see "Spec corrections" S1 and S2.
- **A partial unique index is not a named constraint.** `ON CONFLICT ON CONSTRAINT <name>` fails at runtime against one. Use SQLAlchemy's `pg_insert(...).on_conflict_do_update(index_elements=[...], index_where=<clause>)` — the inference form — and test the upsert path, not just the constraint.

### Events (`app/core/events.py`)

- **Registering an event type carries notification-side obligations.** `EVENT_TYPES` is pinned by two structural tests in `backend/tests/test_notification_delivery.py`:
  - `test_every_registered_event_type_has_an_email_renderer` asserts `set(_EMAIL_RENDERERS) == EVENT_TYPES` (`app/modules/notification/delivery.py`),
  - `test_every_registered_event_type_has_a_pinned_wire_format_sample` asserts `set(_EVENT_TYPE_SAMPLE_PAYLOADS) == EVENT_TYPES`.

  Adding an event type without both is a red suite, and adding one with a lazy renderer is worse — it ships `_render_generic`'s machine notice to a human. Write a real renderer for each.
- **Touching `app/core/events.py`, `app/core/rbac.py` or `app/models/__init__.py` requires running the FULL backend suite**, not this module's tests. A previous build shipped 2 failures that only the full suite found. Do it in the same step as the edit, not at the end of the plan.

### Jobs and the inventory result path

- **A raising inventory result handler fails the job.** `JobEngine.complete` (`app/jobs/engine.py:145-180`) wraps the registered `result_handler` in `try/except Exception`, and on any exception flips the job to `failed` with `{"error": ...}`. **Every reconciliation path that reads NIC data must degrade to "no observation", never raise.** This module must not be able to break Phase 1. That is an explicit test in Task 5 — not a code comment.
- A caught exception is not enough on its own: if the IPAM path raised a *database* error, the surrounding transaction is already poisoned and the whole inventory ingest rolls back regardless of the `except`. The hook therefore runs inside `session.begin_nested()` (a SAVEPOINT). Precedent: `app/modules/notification/channels.py:325`, `app/modules/email_intake/mailboxes.py:277`, `app/modules/sla/policies.py:499`.

### Verified signatures you will call

```python
audit_service.record(session, *, org_id, actor_type, actor_id, action,
                     target_type="", target_id="", metadata=None) -> AuditEvent
publish(session, *, org_id, event_type, subject_type, subject_id, payload,
        actor_user_id) -> None                      # app.core.events
JobEngine.create(session, *, org_id, type, target_device_id, params,
                 requested_by=None, ttl_s=None) -> Job
JobEngine.signed_envelope(job) -> dict              # then publish_job(redis, device_id, envelope)
publish_job(redis, device_id, envelope)             # app.gateway.dispatch
queue_ui_event(session, org_id, {...})              # app.gateway.dispatch
register(JobType(name, params_model, result_handler=...))   # app.jobs.registry
org_session(org_id, *, engine=None)                 # app.core.tenancy
escape_formula(value)                               # app.core.csv_export
require_permission(code)                            # app.api.deps
```

### House facts

- **`pytest` is at `backend/.venv/bin/pytest`.** Bare `pytest` and `python -m pytest` are not installed. Use `cd backend && .venv/bin/pytest tests/... -v`, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`.
- **Baseline is ~2534 backend test functions across 200 test files** (count at plan time, on a tree with uncommitted concurrent work). Record the real baseline before Task 1 and do not regress it.
- **Postgres and Redis go down in this container during long runs.** Check `pg_isready` and `redis-cli ping` first and again if anything hangs; restart them yourself (`service postgresql start`, `redis-server --daemonize yes`).
- **`permissions` is `(id, code, module, action)`** — no `resource`, no `description`. Descriptions live only in `app/core/rbac.py::PERMISSIONS`. A dotted action is normal: `("deploy.script.run", "deploy", "script.run")`, `("patch.policy.manage", "patch", "policy.manage")`.
- **Roles are `super-admin`, `org-admin`, `technician`, `auditor`, `read-only`** (`migrations/versions/0003_rbac_seed.py`). `test_org_admin_has_all_permissions` requires org-admin (and super-admin) to hold every code.
- **Modules register in `app/modules/__init__.py::register_all`**, not `app/main.py`.
- **FastAPI 0.139.2: `app.routes` does not contain `include_router`'d routes** — only `_IncludedRouter` objects with `path=None`. A route-completeness test must flatten via `_IncludedRouter.original_router` + `include_context.prefix`. **Copy the working flattener from `backend/tests/test_email_intake_api.py`** (see its lines 77-90).
- **Module ORM models are re-exported from `app/models/__init__.py`** (see the `email_intake` and `sla` imports there). `tests/test_schema_drift.py` compares `Base.metadata` against the live schema for every modelled table — a model that is not imported there is not checked, and a model that disagrees with its migration on nullability fails.
- Tests use the `api_env` fixture with `asyncio_mode=auto` — **do not** write `@pytest.mark.asyncio`. Use `AsyncSession(api_env.admin_engine)` for setup.
- Async throughout, full type hints, business logic in services not route handlers, `mypy` and `ruff` clean. Cross-org ids return **404**, never 403.
- **No agent code in this cycle.** Nothing in this plan writes Go. Task 13 states what the inventory collector must emit and Task 9 states `net.sweep`'s idle-budget obligations. *(Corrected 2026-08-05: this pointed at "Task 12", which is the CSV import/export task and says nothing about the agent — that was wrong before the move, not because of it. Amended the same day: it also read "No agent code in this repo — `it-suite-agent` is a separate repository". `CLAUDE.md` commit `a32cead` moved the agent in-tree to `agent/`, so the exclusion is one of scope, not of location. The Go work this plan defers — Task 13's NIC address block, and the `net.sweep` capability — is now writable here and still has not been written.)*

---

## Spec corrections

Six things found by reading the live code and re-reading the spec's own reasoning. Each is implemented per this plan.

> **Status: all six are now resolved *in the spec itself*** — the spec has been amended (§0.15 covers S1 and S2 as one rule with §0.7; §0.16 covers S6; §0.17 pins the inventory hook and its SAVEPOINT; §0.18 covers S4; §11.1 carries S5; §14.4 carries S3; §14.6 records the inventory change). **The spec and this section no longer disagree**, and the spec's §2.7, §2.3, §3.1, §4.3 and §13 now state the corrected schema, ordering and tests directly. Read the paragraphs below for the *reasoning* that produced each fix; take the *instructions* from the spec, which is the authority. One resolution was made more specific than this section left it: S1's `mac_key` is a **stored generated column**, not a write-time default (§0.15 says why) — there is no longer a choice to make there.

**S1 — `ipam_observation`'s unique key contains a nullable column, so ICMP-only observations never upsert.** Spec §2.7 keys the upsert on `(org_id, vrf_id, address, source, mac)`, and `mac` is nullable — every ICMP-only sweep observation has `mac IS NULL`, NULLs do not collide, and **every weekly sweep would insert a fresh row for the same address forever.** That silently converts the "current-state table, not a time series" property (§0.10 point 4 — the single schema property that keeps this module out of the monitoring business) into an unbounded probe log. This is the exact trap §0.7 invokes to justify `vrf_id NOT NULL`. Fix: a `mac_key` column, `NOT NULL`, defaulted at write time to `coalesce(mac::text, '')`, with the unique index on `(org_id, vrf_id, address, source, mac_key)`. A generated column is acceptable if you prefer (`GENERATED ALWAYS AS (coalesce(mac::text,'')) STORED`) — state which you chose. Task 1 ships the fix and the test that an ICMP-only observation upserted twice yields **one row with `sighting_count = 2`**.

**S2 — `ipam_vlan`'s unique key has the same hole.** Unique `(org_id, site_id, vid)` with `site_id` nullable permits two org-wide VLAN 10 rows. Fix with a pair: the full unique index plus a partial `UNIQUE (org_id, vid) WHERE site_id IS NULL`. Same for any org-wide row you add later.

**S3 — `_LINK_TARGET_MODELS` is not in `documentation/api.py`.** Spec §14.4 says both changes are in `app/modules/documentation/api.py`. In fact `_LINK_TARGET_MODELS` lives in `app/modules/documentation/service.py:33`, and the `target_type` pattern is in `api.py:60` (`Field(pattern="^(device|device_group|credential|record)$")`). Task 12 edits both files.

**S4 — "once IPAM is installed" has no meaning in this codebase.** Spec §0.14/§8.3 conditions the `record_type='network'` refusal on IPAM being installed. There is no per-module install state: `app/modules/__init__.py::register_all` registers every module unconditionally in every deployment. The refusal is therefore **unconditional**, in `documentation/service.py::create_record`, raising a `ValueError` whose message names `POST /api/v1/ipam/subnets`. `network` stays in `RECORD_TYPES` (`documentation/schemas.py:13`) so existing rows keep reading, updating and archiving.

**S5 — §11.1 says "all eleven tables"; §2 defines ten.** `ipam_vrf`, `ipam_site`, `ipam_vlan`, `ipam_subnet`, `ipam_range`, `ipam_address`, `ipam_observation`, `ipam_drift`, `ipam_scan_authorization`, `ipam_sweep_run`. The list is authoritative; the count is a typo. Ten tables.

**S6 — §3.1's `status` table is ambiguous between `dormant` and `assigned`.** As written, `dormant` fires whenever `¬(Gate E ∧ Gate L)`, which includes "this segment never had any evidence at all" — making the `assigned` row (documented as exactly that case) unreachable. Pinned resolution, implemented in Task 6: the table is evaluated **strictly top to bottom, first match wins**, and the two are separated by *history*, not by the window:
- `assigned` — no current observation and the subnet has **never** had an evidence source (no successful `ipam_sweep_run` ever, no inventory observation inside it ever). Nothing has ever looked; there is nothing to be dormant about.
- `dormant` — no current observation, the subnet *has* had evidence at some point, and a gate blocks now.

This preserves §0.11's intent (silence is not information) and makes both rows reachable. Record it in the spec.

---

## File structure

**Created (backend):**

| File | Responsibility |
| --- | --- |
| `backend/migrations/versions/<head+1>_ipam.py` | Ten tables, GiST + btree indexes, RLS, six permissions |
| `backend/app/modules/ipam/__init__.py` | `register(app)` — router, job type, inventory hook |
| `backend/app/modules/ipam/models.py` | The ten ORM models |
| `backend/app/modules/ipam/errors.py` | `IpamError`, `IpamValidationError`, `IpamNotFoundError`, `IpamConflictError`, `IpamTooLargeError`, `IpamAuthorizationError`, `IpamSweepRejected` |
| `backend/app/modules/ipam/net.py` | **Pure** address arithmetic: parse, classify, minimal-CIDR set ops, `scope_digest`. No DB. |
| `backend/app/modules/ipam/registry.py` | VRF / site / VLAN CRUD + `ensure_default_vrf` |
| `backend/app/modules/ipam/subnets.py` | Subnet CRUD, derived parent, sibling-overlap under the advisory lock, ranges |
| `backend/app/modules/ipam/observations.py` | Observation upsert, currency, orphan grouping |
| `backend/app/modules/ipam/nic.py` | **Pure** NIC-attribute parsing. Total: never raises. |
| `backend/app/modules/ipam/allocation.py` | Derived `status`, utilisation, next-free, `allocate` |
| `backend/app/modules/ipam/addresses.py` | `state` transitions, claimants, release, audit-history projection |
| `backend/app/modules/ipam/authorization.py` | `ipam_scan_authorization` CRUD + `authorized_targets()` |
| `backend/app/modules/ipam/sweep.py` | Dispatch, `ipam_sweep_run`, device selection |
| `backend/app/modules/ipam/handler.py` | `net.sweep` job type + the rejecting result handler |
| `backend/app/modules/ipam/reconcile.py` | Drift classes, gates, the daily recompute, ack/suppress |
| `backend/app/modules/ipam/importer.py` | CSV preview/apply + the doc-record converter |
| `backend/app/modules/ipam/schema.py` | Pydantic request/response + `NetSweepParams` / `NetSweepDocument` |
| `backend/app/modules/ipam/api.py` | Routes, each `require_permission`-gated |

**Modified (backend):** `app/core/rbac.py` (6 codes), `app/core/events.py` (2 types), `app/models/__init__.py`, `app/modules/__init__.py`, `app/worker.py` (2 beat entries + 2 tasks), `app/modules/notification/delivery.py` (2 renderers), `app/modules/inventory/handler.py` + new `app/modules/inventory/hooks.py` (Task 5 — **inventory owns these**), `app/modules/documentation/service.py` + `api.py` (Task 12 — **documentation owns these**), `docs/kernel/wire-protocol/` (Tasks 9 and 13), `scripts/export_wire_schema.py`.

**Created (frontend):** `frontend/src/modules/ipam/` — `api.ts`, `types.ts`, `SubnetTree.tsx`, `SubnetDetail.tsx`, `AddressGrid.tsx`, `DriftPage.tsx`, `SearchBox.tsx`, `SweepPanel.tsx`, `status.ts`; plus an Addresses tab on device detail.

**Files another module owns — flag these in your report and keep the diff minimal:**

| File | Owner | Task |
| --- | --- | --- |
| `app/core/events.py`, `app/core/rbac.py`, `app/models/__init__.py` | kernel | 1 |
| `app/modules/notification/delivery.py` | notification | 1 |
| `app/modules/inventory/handler.py`, `app/modules/inventory/hooks.py` (new) | inventory | 5 |
| `app/modules/documentation/service.py`, `api.py` | documentation | 12 |
| `docs/kernel/wire-protocol/InventoryDocument.schema.json` | kernel wire contract | 13 |
| `app/worker.py` | kernel worker | 9, 10 |

**Security-critical path — read this before sequencing anything.** The deny-by-default scan-authorization surface is **Task 8**, and it is complete and falsified before **Task 9** creates the first thing in this module capable of dispatching a sweep. No task before Task 9 may call `JobEngine.create`. If you find yourself needing to dispatch earlier, stop: that is the sequencing error this plan exists to prevent.

---

## Task 1: Migration — ten tables, GiST indexes, RLS, six permissions, two event types

**Files:**
- Create: `backend/migrations/versions/<head+1>_ipam.py`, `backend/app/modules/ipam/__init__.py` (empty for now), `backend/app/modules/ipam/models.py`, `backend/app/modules/ipam/errors.py`
- Modify: `backend/app/core/rbac.py`, `backend/app/core/events.py`, `backend/app/models/__init__.py`, `backend/app/modules/notification/delivery.py`
- Test: `backend/tests/test_ipam_migration.py`, and append to `backend/tests/test_notification_delivery.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the ten ORM models; permission codes `ipam.read`, `ipam.assign`, `ipam.manage`, `ipam.sweep.run`, `ipam.sweep.manage`, `ipam.scope.manage`; event types `ipam.drift_detected`, `ipam.sweep_rejected`; the seed helpers later tasks import — `_seed_user(api_env, org_id) -> UUID`, `_seed_vrf(api_env, org_id, name="global") -> UUID`, `_seed_subnet(api_env, org_id, *, vrf_id, prefix, kind="assignable", **kw) -> UUID`, `_seed_device(api_env, org_id, *, online=True, last_seen_at=None) -> UUID`.

- [ ] **Step 1: Confirm the head**

`cd backend && .venv/bin/alembic heads` and `ls migrations/versions | sort | tail -3`. Use that revision id as `down_revision`. **Write the number you found into your report.** If it is not what this plan's filename placeholder suggests, that is expected — concurrent work moves it.

- [ ] **Step 2: Write the failing tests**

`backend/tests/test_ipam_migration.py`. At minimum:

- **RLS structural**, parametrized over all ten tables: `relrowsecurity` and `relforcerowsecurity` are both true.
- **RLS behavioural**, per table: a row written in org A is invisible in org B through `org_session`; inserting org B's `org_id` from org A's session raises `ProgrammingError`. Ten of each. Parametrize with a small per-table row factory rather than writing twenty near-identical functions.
- **Types are native**, not text: query `information_schema.columns` and assert `udt_name` is `cidr` for `ipam_subnet.prefix` and `ipam_scan_authorization.prefix`, `inet` for `ipam_address.address` / `ipam_observation.address` / `ipam_drift.address` / `ipam_range.start_address` / `ipam_range.end_address` / `ipam_subnet.gateway`, and `macaddr` for `ipam_address.mac` / `ipam_observation.mac`. A text column passes every behavioural test in this suite until the first `10.4.70.0/24` sorts wrong — pin it structurally.
- **GiST indexes exist** on `ipam_subnet.prefix`, `ipam_address.address`, `ipam_observation.address`, `ipam_scan_authorization.prefix` (query `pg_indexes` for `USING gist`).
- **CHECK constraints refuse**: `kind='router'`; a gateway outside its own prefix; `sweep_interval_hours = 0`; `vid = 0` and `vid = 4095`; `start_address > end_address`; `state='in_use'` (§0.6 — this column may never hold a derived value); an `assigned` row with **both** `device_id` and `asset_label`, and one with **neither**.
- **Uniqueness**: duplicate `(org_id, vrf_id, prefix)` refused; duplicate `(org_id, vrf_id, address)` refused; two active authorizations for the same `(org, vrf, prefix)` refused **while a revoked one plus a new one is allowed** (that pair is what proves the index is partial rather than total).
- **S1, the NULL-collision test that matters:** insert the same ICMP-only observation (`mac IS NULL`) twice through the upsert path and assert **one row** with `sighting_count = 2`. *If the guard were deleted* (`mac` used directly in the key instead of `mac_key`), this test observes **two rows and `sighting_count = 1`** — different values, not merely a different exception. Write that sentence in the docstring.
- **S2:** two org-wide `ipam_vlan` rows with the same `vid` and `site_id IS NULL` are refused.
- **Permissions**: each of the six codes is seeded and granted to `org-admin`; `technician` holds exactly `ipam.read`, `ipam.assign`, `ipam.sweep.run`; `auditor` and `read-only` hold exactly `ipam.read`; **nobody but org-admin/super-admin holds `ipam.scope.manage` or `ipam.manage`** — assert the negative explicitly, it is the §11.2 control.
- **Events**: both types are in `EVENT_TYPES`.

- [ ] **Step 3: Run to verify failure** — `ModuleNotFoundError: No module named 'app.modules.ipam'`.

- [ ] **Step 4: Write the models**

`app/modules/ipam/models.py`, mirroring `app/modules/sla/models.py`'s header style (`Base`, `uuid7_pk`, a local `_utcnow`). Use `sqlalchemy.dialects.postgresql.INET`, `CIDR`, `MACADDR`. Ten classes exactly as spec §2.1-2.10 describes, with S1 and S2 applied.

`ipam_observation` gains `mac_key` (S1). **Amended after this plan was written:** spec §0.15/§2.7 now pins it as a *stored generated* column, not a write-time default — `mac_key: Mapped[str] = mapped_column(Text, Computed("coalesce(mac::text, '')", persisted=True), nullable=False)`, and the migration creates it as `GENERATED ALWAYS AS (coalesce(mac::text, '')) STORED`. **No write path may set it**: passing `mac_key` in an INSERT (including in `pg_insert(...).values(...)`) is a Postgres error against a generated column. It appears only in `index_elements` for conflict inference. `test_schema_drift.py` compares the model against the live schema, so the `Computed(...)` in the model and the DDL in the migration must say the same thing.

Import all ten into `app/models/__init__.py`. `tests/test_schema_drift.py` compares nullability column-by-column against the live schema — get every `nullable=` right the first time or that test tells you which one you got wrong.

- [ ] **Step 5: Write the migration**

Read the newest committed module migration for the exact permission-insert SQL and RLS block and **copy them** rather than trusting this plan's recollection. Structure:

```python
_TABLES = ["ipam_vrf", "ipam_site", "ipam_vlan", "ipam_subnet", "ipam_range",
           "ipam_address", "ipam_observation", "ipam_drift",
           "ipam_scan_authorization", "ipam_sweep_run"]
PERMISSIONS = [
    ("ipam.read", "ipam", "read"),
    ("ipam.assign", "ipam", "assign"),
    ("ipam.manage", "ipam", "manage"),
    ("ipam.sweep.run", "ipam", "sweep.run"),
    ("ipam.sweep.manage", "ipam", "sweep.manage"),
    ("ipam.scope.manage", "ipam", "scope.manage"),
]
ROLE_GRANTS = {
    "super-admin": [c for c, _, _ in PERMISSIONS],
    "org-admin": [c for c, _, _ in PERMISSIONS],
    "technician": ["ipam.read", "ipam.assign", "ipam.sweep.run"],
    "auditor": ["ipam.read"],
    "read-only": ["ipam.read"],
}
```

Indexes, all created with `op.execute` so the GiST opclass is explicit:

```
CREATE INDEX ix_ipam_subnet_prefix_gist ON ipam_subnet USING gist (prefix inet_ops);
CREATE UNIQUE INDEX uq_ipam_subnet_org_vrf_prefix ON ipam_subnet (org_id, vrf_id, prefix);
CREATE INDEX ix_ipam_subnet_org_site ON ipam_subnet (org_id, site_id);
CREATE INDEX ix_ipam_subnet_org_parent ON ipam_subnet (org_id, parent_id);
CREATE INDEX ix_ipam_subnet_sweep_due ON ipam_subnet (org_id, sweep_enabled, last_swept_at)
    WHERE sweep_enabled;
CREATE INDEX ix_ipam_address_gist ON ipam_address USING gist (address inet_ops);
CREATE UNIQUE INDEX uq_ipam_address_org_vrf_addr ON ipam_address (org_id, vrf_id, address);
CREATE INDEX ix_ipam_address_grid ON ipam_address (org_id, subnet_id, address);
CREATE INDEX ix_ipam_address_device ON ipam_address (org_id, device_id) WHERE device_id IS NOT NULL;
CREATE INDEX ix_ipam_address_mac ON ipam_address (org_id, mac) WHERE mac IS NOT NULL;
CREATE UNIQUE INDEX uq_ipam_obs_key ON ipam_observation (org_id, vrf_id, address, source, mac_key);
CREATE INDEX ix_ipam_obs_age ON ipam_observation (org_id, last_seen_at);
CREATE INDEX ix_ipam_obs_device ON ipam_observation (org_id, device_id);
CREATE INDEX ix_ipam_obs_gist ON ipam_observation USING gist (address inet_ops);
CREATE UNIQUE INDEX uq_ipam_drift_key ON ipam_drift (org_id, vrf_id, address, drift_class);
CREATE INDEX ix_ipam_drift_open ON ipam_drift (org_id, drift_class, severity) WHERE resolved_at IS NULL;
CREATE INDEX ix_ipam_drift_subnet ON ipam_drift (org_id, subnet_id) WHERE resolved_at IS NULL;
CREATE UNIQUE INDEX uq_ipam_scope_active ON ipam_scan_authorization (org_id, vrf_id, prefix)
    WHERE revoked_at IS NULL;
CREATE INDEX ix_ipam_scope_gist ON ipam_scan_authorization USING gist (prefix inet_ops);
CREATE INDEX ix_ipam_range_lookup ON ipam_range (org_id, subnet_id, start_address);
CREATE INDEX ix_ipam_sweep_run_recent ON ipam_sweep_run (org_id, subnet_id, started_at DESC);
CREATE UNIQUE INDEX uq_ipam_vlan_org_site_vid ON ipam_vlan (org_id, site_id, vid)
    WHERE site_id IS NOT NULL;
CREATE UNIQUE INDEX uq_ipam_vlan_org_vid_global ON ipam_vlan (org_id, vid) WHERE site_id IS NULL;
```

(The site-scoped index is **partial** per amended spec §2.3/§0.15 — the pair is total between them, and both need `index_where=` on any upsert. A total index there would enforce the same rule but would carry rows the NULL case can never match, which is the shape that hid S2 in the first place.)

**No composite `(org_id, prefix)` GiST** — that needs `btree_gist`, and §2.4 rules it out. Note which indexes are partial: Tasks 4, 7 and 9 must use `index_elements=` + `index_where=` against them, never `ON CONFLICT ON CONSTRAINT`.

- [ ] **Step 6: Register the permissions, event types, and renderers**

`app/core/rbac.py::PERMISSIONS` — six entries. `ipam.scope.manage`'s description says HIGH PRIVILEGE in the same terms `notification.admin` uses, because it is the same class of grant: it decides where this platform emits network traffic.

`app/core/events.py::EVENT_TYPES` — `ipam.drift_detected` and `ipam.sweep_rejected`, each with a comment naming the publisher and the spec section. **Neither goes in `STAFF_ONLY_EVENT_TYPES`** (§10: that set is about not mailing a ticket's external requester; these have no requester) — so **no new notification CHECK constraint and no notification migration** are needed.

`app/modules/notification/delivery.py` — a real renderer per type in `_EMAIL_RENDERERS`, reading only `event.payload` (never `event.actor_user_id`: both are machine-origin with no actor). Then add a sample payload per type to `_EVENT_TYPE_SAMPLE_PAYLOADS` in `tests/test_notification_delivery.py`, matching the §10 payload shapes exactly. Both structural tests must pass.

- [ ] **Step 7: Run the FULL backend suite**

`cd backend && .venv/bin/pytest -q`. Not this module's tests — the full suite. You touched `events.py`, `rbac.py`, `models/__init__.py` and a notification file; the last build that trusted a subset shipped 2 failures. Expect `test_rbac.py`'s permission-registry test to pin the exact code set — adding the six codes to its literal is the correct fix. Record the before/after counts.

- [ ] **Step 8: Prove the tenancy tests can fail**

Set the policy on one table to `WITH CHECK (true)`, rebuild the test DB, run the insert test — watch it go red. Then `USING (true)` and watch the read test go red. Restore both. Paste both outputs. Deleting `WITH CHECK` proves nothing.

- [ ] **Step 9: Prove the S1 test can fail**

Change the observation upsert's `index_elements` to use `mac` instead of `mac_key`. Watch the ICMP-upsert test report **two rows**. Restore.

**Definition of done:** migration applies from the confirmed head; ten tables with forced RLS; every CHECK and unique test passes; the six permissions seeded with the exact role split including the negative assertions; both event types registered with renderers and pinned samples; full suite green at or above baseline; both falsification proofs pasted.

Commit: `feat(ipam): migration -- address plan tables, RLS, permissions, events`

---

## Task 2: Pure address arithmetic

**Files:** Create `backend/app/modules/ipam/net.py`; Test `backend/tests/test_ipam_net.py`

**This task has no database and is cheap to test exhaustively — so test it exhaustively.** Everything downstream (containment, the allocator, the authorized target set, the result-scope check) is built on these functions, and a boundary bug here is a security bug in Task 8.

**Interfaces:**
- Consumes: stdlib `ipaddress`, `hashlib`, `json` only.
- Produces:
  - `parse_address(value: str) -> IPv4Address | IPv6Address | None` — returns `None`, never raises, on anything unparseable. Strips a zone id (`fe80::1%4`) and a prefix length (`10.1.2.42/24`) before parsing.
  - `parse_mac(value: str) -> str | None` — normalises `A4-BB-6D-11-22-33`, `a4:bb:6d:11:22:33`, `a4bb.6d11.2233` to the colon form; `None` otherwise.
  - `is_documentable(addr) -> bool` — False for loopback, link-local, multicast, unspecified. §4.1's discard rule in one place.
  - `usable_hosts(prefix) -> Iterator[address]` — RFC 3021: both hosts in a `/31`, the single address in a `/32`, network and broadcast excluded for `masklen <= 30`. IPv6 yields nothing (§0.5 — never enumerated).
  - `subtract(prefixes, holes) -> list[network]` — the minimal canonical CIDR list. Used by §6.2 for `S.prefix ∩ authorizations − exclusions`.
  - `intersect(a, b) -> list[network]`
  - `range_to_cidrs(start, end) -> list[network]` — for turning an `ipam_range` into holes.
  - `canonical_targets(nets) -> list[str]` — sorted, deduplicated, minimal.
  - `scope_digest(targets: list[str]) -> str` — `sha256(json.dumps(targets, separators=(",",":")).encode()).hexdigest()`. **One function, called by both the dispatcher and the result handler.** Two implementations of a digest is how a scope check passes on both sides while checking nothing.

Required tests — the boundary cases, because these are where a string implementation would have been wrong:
- `10.4.7.9 < 10.4.7.10` orders correctly; `10.4.70.0/24` is **not** contained by `10.4.7.0/16`-style near-misses.
- `usable_hosts` on `/24` yields 254; on `/30` yields 2; on `/31` yields 2; on `/32` yields 1; on an IPv6 `/64` yields nothing.
- `subtract([10.0.0.0/24], [10.0.0.128/25])` → `[10.0.0.0/25]`; subtracting a single host produces the minimal cover, not 254 `/32`s.
- `intersect` of a supernet authorization with a subnet returns the subnet, **never the supernet** — the §0.12 "an authorization can never widen a sweep" property, tested as arithmetic before it is tested as a service.
- `range_to_cidrs(10.0.0.100, 10.0.0.200)` returns the minimal list and covers exactly 101 addresses.
- `scope_digest` is stable across input ordering (because `canonical_targets` sorts first) and changes when any single target changes.
- `parse_address` returns `None` for `""`, `"dhcp"`, `"10.1.2.999"`, `"not an ip"`, `None`-ish input coerced to str, a 10 KB string. It **never raises** — parametrize a hostile list.
- `is_documentable` is False for `127.0.0.1`, `::1`, `169.254.3.4`, `fe80::1`, `224.0.0.1`, `0.0.0.0`, `::`.

**Definition of done:** `net.py` has no import from `app.models`, `sqlalchemy` or `app.core`; every function above is covered; `parse_address` and `parse_mac` are proven total against a hostile input list.

Commit: `feat(ipam): pure address arithmetic -- containment, minimal CIDR sets, scope digest`

---

## Task 3: Registry — VRFs, sites, VLANs, and the lazy `global` VRF

**Files:** Create `backend/app/modules/ipam/registry.py`; Test `backend/tests/test_ipam_registry.py`

**Interfaces:**
- Consumes: the models (T1).
- Produces: `create_vrf`, `update_vrf`, `delete_vrf`, `list_vrfs`, `create_site`/`update_site`/`delete_site`/`list_sites`, `create_vlan`/`update_vlan`/`delete_vlan`/`list_vlans`, and **`ensure_default_vrf(session, org_id) -> UUID`**.

Requirements:
- `ensure_default_vrf` creates the `global` row **lazily on first use** (§0.7) via `pg_insert(...).on_conflict_do_nothing(index_elements=["org_id", "name"])` followed by a select — **not** check-then-insert. Two concurrent first subnet writes must not produce a duplicate or a 500. This project has run two sweeps to remove the check-then-insert bug class.
- Every create/update/delete writes an audit event (§11.3): `ipam.vrf_created` / `_updated` / `_deleted`, and the site/vlan equivalents.
- Deleting a VRF, site or VLAN that is referenced by a subnet raises `IpamConflictError` naming the referents. Do not rely on the FK error text.
- `ipam_site.doc_record_id` is validated to resolve to a `doc_records` row **in the same org** when supplied.
- Every query carries an explicit `org_id` predicate, and **at least one test pins that predicate on the admin engine** — through `org_session` RLS rejects first and the test passes with the predicate deleted.

Tests: the concurrency test for `ensure_default_vrf` (two tasks, one row); S2's org-wide-VLAN uniqueness through the service; the referenced-delete refusals; the audit assertions; the admin-engine predicate pin.

Commit: `feat(ipam): VRF, site and VLAN registry with the lazily-created global VRF`

---

## Task 4: Subnets — containment, the derived parent, and sibling overlap under the advisory lock

**Files:** Create `backend/app/modules/ipam/subnets.py`; Test `backend/tests/test_ipam_subnets.py`

**Interfaces:**
- Consumes: `net.py` (T2), `ensure_default_vrf` (T3).
- Produces: `create_subnet`, `update_subnet`, `delete_subnet`, `get_subnet`, `list_subnets`, `subnet_tree`, `containing_subnet(session, org_id, vrf_id, address)`, `create_range`, `delete_range`, `list_ranges`, and `recompute_parents(session, org_id, vrf_id, prefix)`.

Requirements:
- `parent_id` is **derived, never supplied**: "the smallest other prefix in the same VRF that contains this one". Recomputed on every subnet write. Inserting a supernet **between** existing rows re-parents its new children — a subnet write is not a single-row write. The API rejects a client-supplied `parent_id` outright.
- **Sibling overlap** is a service-layer rule, not an `EXCLUDE` constraint (§0.3 explains why the constraint cannot express it). The check and the insert run together under
  `SELECT pg_advisory_xact_lock(hashtext('ipam:' || :org || ':' || :vrf))`, the same serialisation technique `app/core/audit.py:121` uses for the hash chain. Two concurrent overlapping inserts serialise; the second sees the first and is refused with `IpamConflictError`.
- Uniqueness of `(org_id, vrf_id, prefix)` stays the DB's job.
- `create_range` validates in the service that both endpoints fall inside `subnet.prefix` (a CHECK cannot reach the parent row) and that `start <= end`, and that the range does not overlap another range of the same subnet.
- `kind='container'` subnets have no utilisation, no next-free and no sweeps — enforced where those are computed (T6, T9), asserted here for the flag itself.
- Audit `ipam.subnet_created` / `_updated` / `_deleted` and `ipam.range_created` / `_deleted`, each carrying prefix and vrf.
- Deleting a subnet with addresses: refuse (`IpamConflictError`) unless `?cascade` is explicitly requested; on cascade, addresses keep their rows with `subnet_id` set to NULL — sparse means "no row until first mentioned", not "rows evaporate" (§0.4).

Required tests:
- Containment across IPv4 **and** IPv6; `containing_subnet` returns the **most specific** match, with a fixture holding both a /8 and a /24 that contain the address. A fixture with one candidate cannot detect a "smallest" bug.
- **`EXPLAIN` asserts the GiST index is used** for the containment query (spec §13). Pin on the plan text containing the index name; if the planner refuses on a tiny table, seed enough rows to make the index attractive and say so.
- Inserting a supernet between existing rows re-parents **exactly** the contained children and nothing else — assert the untouched rows' `parent_id` too, not just the changed ones.
- Two concurrent overlapping sibling inserts: the second is refused. Drive it with two real sessions and a barrier. **Falsifying mutation:** remove the advisory lock and watch both succeed, leaving two overlapping siblings.
- A gateway outside its prefix is refused (belt and braces with T1's CHECK — the service should produce a readable error, not a `ProgrammingError`).
- A range that spills outside its subnet is refused; two overlapping ranges in one subnet are refused.
- Cross-org: a subnet id from another org 404s; the `org_id` predicate is pinned on the admin engine.

Commit: `feat(ipam): subnet registry with derived hierarchy and serialised overlap checks`

---

## Task 5: Observations from inventory — the legacy fallback, and the never-raise guarantee

**Files:** Create `backend/app/modules/ipam/nic.py`, `backend/app/modules/ipam/observations.py`; Create `backend/app/modules/inventory/hooks.py`; Modify `backend/app/modules/inventory/handler.py`; Test `backend/tests/test_ipam_nic.py`, `backend/tests/test_ipam_observations.py`, and append to `backend/tests/test_inventory_*.py`

**This task touches a file inventory owns.** Keep the diff to inventory as small as it can be, and flag it.

**This is the task that makes the module useful before the wire contract lands.** It reads the **legacy `attributes.ip` key only** (§4.1: the single IP-bearing NIC shape that exists in this repository today, `backend/tests/test_inventory_schema.py:25`). Task 13 adds the richer shape. Nothing here is blocked.

**Interfaces:**
- Consumes: `net.py` (T2), `subnets.py` (T4), `HardwareComponent`, `Device`, `Agent`.
- Produces:
  - `nic.observations_from_components(components) -> list[NicObservation]` — **pure, total, never raises.** `NicObservation` is a frozen dataclass `(address, mac, name)`.
  - `observations.record_inventory_observations(session, device) -> int`
  - `observations.upsert_observation(session, *, org_id, vrf_id, address, mac, hostname, source, method, device_id, confidence, sweep_run_id=None) -> None`
  - `observations.current_observations(session, org_id, addresses) -> dict[address, list[Observation]]`
  - `observations.observation_ttl_days(session, org_id) -> int` — reads `Policy(module="ipam").config["observation_ttl_days"]`, default **14**
  - `observations.orphan_groups(session, org_id) -> list[OrphanGroup]`
  - `inventory.hooks.register_post_ingest_hook(fn)` / `run_post_ingest_hooks(session, device, doc)`

**The hook, and why it is shaped this way.** IPAM must run after `ingest_inventory`, and the dependency arrow must point **ipam → inventory**, never the reverse. Re-registering `inventory.collect` from IPAM (the registry's `register()` does overwrite by name) is rejected: it would make correctness depend on `register_all` ordering and would fork inventory's ingest. So inventory gains a two-function hook module, and `handler.py` calls `run_post_ingest_hooks` after `ingest_inventory` and before `queue_ui_event`. IPAM registers its hook in `ipam.register(app)`.

`run_post_ingest_hooks` runs **each hook inside `async with session.begin_nested()`**, catching `Exception` and logging. Two layers, and they are different guarantees:
1. The SAVEPOINT + `except` means no hook — IPAM's or a future module's — can fail an `inventory.collect` job or poison its transaction. A bare `try/except` is **not** sufficient: a database error inside the hook aborts the enclosing transaction whether or not you catch the Python exception.
2. IPAM's own parser is **total** — it returns `[]` for garbage rather than relying on layer 1.

Both are tested, separately, because a test of layer 2 alone would pass against an implementation with no layer 1 and vice versa.

Requirements for `nic.observations_from_components`:
- Reads `attributes["ip"]` (legacy). Treats `identifier` as a MAC **only when `parse_mac` accepts it** (§4.1 point 4); otherwise `mac=None`.
- Discards loopback, link-local, multicast, unspecified via `is_documentable` — a NIC reporting `169.254.x` is reporting that DHCP failed, which is not an IPAM fact.
- Accepts `attributes` that is not a dict, `ip` that is not a string, a component with no `attributes` at all, a 10 KB `ip` value, a list where a dict was expected. Returns `[]`. **Never raises.**

Requirements for `record_inventory_observations`:
- Reads `hardware_components` with `type='nic'` for the device — **not** `Device.nics`, which is a flattened jsonb convenience copy (§4.1).
- `vrf_id` is the org's default VRF (T3). `subnet_id` is derived by containment (`address <<= prefix`) against `ipam_subnet` in that VRF, and is **NULL** when nothing contains it (§4.2 orphans) — not an error.
- `source='inventory'`, `method='nic'`, `confidence='high'`, `device_id=device.id`.
- Upserts on the S1 key, bumping `last_seen_at` and `sighting_count`, using `index_elements=[...] , index_where=None` against `uq_ipam_obs_key` (a total unique index, so plain `index_elements` inference works).
- **Writes `ipam_observation` rows only.** It cannot write `ipam_address.state`, `device_id` or `assigned_by` — §0.6's split is the control.
- **`Agent.last_ip` is never read.** It is the TCP peer address seen by the gateway (`app/gateway/main.py:243`), which behind NAT belongs to a firewall.

Required tests, with the falsifying mutation named for each guard:

| Test | If the guard were deleted, the test observes |
| --- | --- |
| A legacy `{"ip": "10.1.2.42"}` NIC produces one high-confidence observation | — (positive path) |
| A NIC whose `identifier` is `"Ethernet0"` produces an observation with `mac IS NULL` | `mac = 'Ethernet0'` → an insert error or a garbage MAC, not merely a different code path |
| Loopback / link-local / multicast NICs produce **zero** observations | 3 rows instead of 0 |
| A malformed NIC block (`attributes` a list, `ip` an int, absent key) produces zero observations **and the parser does not raise** | `pytest.raises` catches a `TypeError` — assert `== []`, not "no error" |
| **A full `inventory.collect` completing with a malformed NIC leaves the job `succeeded`, writes the device's software and `hardware_components` rows, and writes zero observations** | Delete the SAVEPOINT+`except` in `run_post_ingest_hooks` and register a deliberately raising hook: the job goes to `failed` with `{"error": ...}` and the inventory rows are **absent**. That is the different observable value. |
| An address inside no registered subnet lands with `subnet_id IS NULL` and appears in `orphan_groups` | Making it an error would raise; making it skip would give 0 rows |
| The same NIC ingested twice yields **one** observation row with `sighting_count = 2` | 2 rows (S1) |
| `Agent.last_ip` is never ingested: a device whose agent has `last_ip='203.0.113.9'` and no NIC data produces zero observations | 1 observation at the NAT address |

**Definition of done:** the layer-1 proof is executed and pasted (a raising hook does not fail the job, and deleting the guard does); `nic.py` imports nothing from `sqlalchemy`; the inventory diff is the hook call plus the new `hooks.py`.

Commit: `feat(ipam): inventory-derived observations that can never fail a collect job`

---

## Task 6: Derived `status`, utilisation, next-free, and atomic allocation

**Files:** Create `backend/app/modules/ipam/allocation.py`; Test `backend/tests/test_ipam_allocation.py`

**Interfaces:**
- Consumes: `net.py` (T2), `subnets.py` (T4), `observations.py` (T5).
- Produces:
  - `derive_status(...) -> str` — **pure**, given `(state, in_dhcp_pool, has_current_observation, gates, subnet_has_ever_had_evidence, conflicting)`. No DB, no session.
  - `enumeration_max_hosts(session, org_id) -> int` — `Policy(module="ipam").config`, default **65_536**
  - `utilization(session, org_id, subnet_id) -> Utilization`
  - `next_free(session, org_id, subnet_id, count) -> list[str]`
  - `allocate(session, org_id, subnet_id, *, actor_id, claimant, ...) -> IpamAddress`

Requirements:
- `derive_status` implements §3.1 **strictly top to bottom, first match wins**, with S6's separation of `dormant` (evidence existed once, a gate blocks now) from `assigned` (nothing has ever looked). It is pure so it can be exhaustively table-tested — and so the frontend's `status.ts` (T13) can be tested against the same table.
- **The ceiling is one rule with two applications** (§0.4, §0.5): above `enumeration_max_hosts`, and for **any** IPv6 prefix, `next_free` and the address grid raise `IpamTooLargeError` → **422** with the message `subnet too large to enumerate`; `utilization` returns counts (`assigned`, `reserved`, `observed`) with a **null** percentage. Not zero. Not an estimate. Null.
- `next_free` is a SQL anti-join over `generate_series` added to `network(prefix)`, short-circuited by `LIMIT`, excluding: network/broadcast (RFC 3021 special cases for `/31` and `/32`), every `dhcp_pool` / `reserved` / `excluded` range, every `ipam_address` row in state `reserved`/`assigned`/`deprecated`, **and every address with a current observation.**
- **That last exclusion is the reconciliation paying for itself.** An address that answers on the wire is not free whatever the spreadsheet says, and handing it out is an outage.
- `next_free` is **advisory**. `allocate` picks and assigns in **one** transaction under the same advisory lock as T4 and returns the address it took.

Required tests:
- `derive_status` table test: one case per row of §3.1 including both S6 rows, plus the precedence proofs — a `dhcp` address that is also observed renders `dhcp`; a `conflicting` address that is also assigned-and-observed renders `conflicting`, not `in_use`. **A test that only ever satisfies one predicate cannot detect a reordered table.**
- A /24 enumerates; a /12 raises 422; utilisation on the /12 returns counts and `percentage is None`; an IPv6 /64 refuses `next_free` but **accepts address records and produces observations** (§0.5's "no enumeration", not "no IPv6").
- `next_free` skips network and broadcast on a `/24`; yields both hosts on a `/31`; yields the single address on a `/32`.
- `next_free` skips a DHCP pool, a reserved range, an excluded range, an existing row, **and a currently observed address with no row at all**. That last one is its own test: *if the observation clause were deleted*, the function returns that address and the test observes it in the list.
- An observation that has **aged out** (older than `observation_ttl_days`) does **not** block the address — the currency window is load-bearing, and a test that only uses fresh observations cannot see a missing `last_seen_at` filter.
- Two concurrent `allocate` calls return **different** addresses. Falsifying mutation: remove the advisory lock and watch them collide.
- `kind='container'` refuses utilisation, next-free and allocate.

Commit: `feat(ipam): sparse utilisation, the reconciling allocator, and the enumeration ceiling`

---

## Task 7: Address assignment workflow, claimants, and the audit-history projection

**Files:** Create `backend/app/modules/ipam/addresses.py`; Test `backend/tests/test_ipam_addresses.py`

**Interfaces:**
- Consumes: T4, T6.
- Produces: `create_address`, `update_address`, `assign`, `reserve`, `release`, `deprecate`, `get_address`, `search`, `address_history`.

Requirements:
- Every transition in §3.2's table, with its predicates: contained by an `assignable` subnet in-org; not in a `dhcp_pool`; not the network or broadcast address (IPv4, masklen ≤ 30); exactly one claimant (`device_id` XOR `asset_label`); no unresolved `conflict` drift on the address; a supplied `device_id` resolves in-org.
- **Assignment inside a `dhcp_pool` is refused with 409** and guidance to shrink the pool or carve a `reserved` range. Modelling a DHCP reservation as a static row inside a dynamic pool creates a record that is true in the IPAM and false on the DHCP server.
- Release sets `state='free'`, clears claimant and `mac`, stamps `released_at`, and **keeps the row**.
- `allocation_method` is operator-set (`static` by default) for manual assignments; for everything else it is DERIVED at read time from the endpoint's reported `origin` (§3.2, migration 0051), with `allocation_method_source` naming which of the two answered.
- **The actor is never an agent.** No ingestion path writes `state`. Assert it directly.
- Audit per §11.3: `ipam.address_assigned` (from-state, to-state, claimant, method), `ipam.address_released` (previous claimant), `ipam.address_reserved`, `ipam.address_state_changed` (from/to).
- `address_history(session, org_id, address_id)` is a **narrow projection of `audit_events`** filtered to `target_type='ipam_address'` and this id, returning timestamp, actor, action, from/to. Gated on `ipam.read` (§0.13) — the general audit API keeps requiring `audit.read`. There is no `ipam_address_history` table.
- `search(q)` accepts a bare IP (containment: which subnet, which record, which observations), a CIDR (everything inside it), a MAC (every address that MAC has been seen at), a hostname, and a device name — dispatching on what `q` parses as.

Required tests:
- Each row of §3.2, positive **and** negative.
- Static assignment inside a `dhcp_pool` → 409.
- **No ingestion path can change `state`:** take an address in `state='assigned'` with a documented claimant, run (a) an inventory ingest and (b) a sweep result that both report that address, and assert `state`, `device_id` and `assigned_by` are **byte-for-byte unchanged** while an `ipam_observation` row appeared. *If §0.6's split were deleted*, the test observes `state` flipped or `device_id` re-pointed — a different value, not a different exception. This is the test that stops "the IPAM changed my reservation by itself".
- `address_history` returns **only** that address's events (seed a second address's events and assert they are absent) and is reachable with `ipam.read` alone (assert with a principal that does **not** hold `audit.read`).
- Both-claimants and neither-claimant are refused at the service, not only by the CHECK.
- `search` on a MAC returns every address that MAC has been observed at across subnets — the "where did this printer go" query.
- Admin-engine pin on the `org_id` predicate of `search` and `address_history`.

Commit: `feat(ipam): the assignment workflow and the narrow audit-history projection`

---

## Task 8: Scan authorization — deny by default, and the authorized target set

> **SECURITY-CRITICAL. This is the module's control surface, and it is complete and tested before anything in this repository can dispatch a sweep.** Nothing before this task calls `JobEngine.create`; nothing after it dispatches without going through `authorized_targets`.

**Files:** Create `backend/app/modules/ipam/authorization.py`; Test `backend/tests/test_ipam_authorization.py`

**Interfaces:**
- Consumes: `net.py` (T2), `subnets.py` (T4).
- Produces:
  - `create_authorization(session, org_id, *, actor_id, vrf_id, prefix, note, expires_at) -> ScanAuthorization`
  - `revoke_authorization(session, org_id, *, actor_id, authorization_id) -> None`
  - `list_authorizations(session, org_id, *, include_revoked=False)`
  - **`authorized_targets(session, org_id, subnet, *, now) -> list[str]`** — the §6.2 set
  - `IpamAuthorizationError`

Requirements:
- **Deny by default.** No row covering a target ⇒ that target is never swept. There is no org-wide "allow everything" switch, and documenting a subnet grants nothing — filing and authorizing are different decisions by different people, and conflating them is how a customer's IPAM ends up scanning a segment their contract does not cover.
- The convention followed is the notification module's webhook allowlist (`docs/modules/notification/spec.md` §2.6): explicit entries, deny by default, an admin-tier permission (`ipam.scope.manage`), every add and remove audited.
- `authorized_by` is **NOT NULL**: there is no system-authorized scope. A human put their name on every packet this module emits.
- Rows are never hard-deleted. Revocation stamps `revoked_at` / `revoked_by`, so the record of what was authorized when survives as evidence.
- An authorization is **active** iff `revoked_at IS NULL` and (`expires_at IS NULL` or `expires_at > now`).
- `authorized_targets` computes, using only `net.py`:

  ```
  targets = ( subnet.prefix ∩ ⋃{ a.prefix : a active in subnet.vrf } )
            − ⋃{ range_to_cidrs(r) : r in subnet's `excluded` ranges }
  ```

  canonically sorted, minimal, capped at `max_targets`. **The intersection with `subnet.prefix` is taken first**, so an authorization broader than the subnet cannot widen anything.
- An **empty** target set is a first-class outcome, returned as `[]`. It is Task 9's job to refuse to dispatch on it; this function does not raise for it.
- Audit `ipam.scope_authorized` / `ipam.scope_revoked` with prefix, actor and note — the high-privilege pair.

Required tests, each naming its falsifying mutation:

| Test | Falsifying mutation and what the test then observes |
| --- | --- |
| A subnet with **no** authorization yields `[]` | Default the function to `[subnet.prefix]`: the test observes `['10.0.0.0/24']` instead of `[]` |
| An authorization for a **supernet** (`10.0.0.0/8`) of a `/24` subnet yields exactly the `/24`, never the `/8` | Drop the `∩ subnet.prefix` step: the test observes `['10.0.0.0/8']` — a 16-million-address sweep authorized by a `/24`'s existence |
| An authorization for a **sibling** prefix yields `[]` | Match on org instead of containment: observes the sibling's prefix |
| A **revoked** authorization yields `[]`; an **expired** one yields `[]` | Drop the `revoked_at IS NULL` / `expires_at` predicate: observes the full prefix |
| An authorization in a **different VRF** yields `[]` | Drop the vrf predicate: observes the prefix |
| An `excluded` range is subtracted: `10.0.0.0/24` minus `.100-.200` yields the minimal cover and **does not contain `.150`** | Drop the subtraction: observes `.150` inside the target set |
| An authorization from **another org** yields `[]`, pinned on the **admin engine** | Delete the service's `org_id` predicate: through `org_session` the test still passes (RLS rejects first) — on the admin engine it observes the other org's prefix. This is the Global-Constraints §RLS lesson applied to the module's most dangerous query. |
| `create_authorization` requires `ipam.scope.manage`; a technician gets 403 | Covered again at the API layer in Task 11; assert the service-layer audit here |
| Two active authorizations for the same `(org, vrf, prefix)` are refused; a revoked one plus a new one is allowed | Make the index total instead of partial: the second observes an `IntegrityError` where it should succeed |

**Definition of done:** every row above green with its mutation executed and pasted; `authorized_targets` has no branch that returns a non-empty set without at least one active row; no call to `JobEngine.create` exists anywhere in the module yet.

Commit: `feat(ipam): deny-by-default scan authorization and the intersected target set`

---

## Task 9: `net.sweep` — job type, dispatch, and the whole-result rejection

> **SECURITY-CRITICAL. Depends on Task 8 being complete.** This is the first code in the module that can put packets on someone else's wire.

**Files:** Create `backend/app/modules/ipam/sweep.py`, `backend/app/modules/ipam/handler.py`; Modify `backend/app/modules/ipam/schema.py`, `backend/app/worker.py`, `scripts/export_wire_schema.py`; Test `backend/tests/test_ipam_sweep_dispatch.py`, `backend/tests/test_ipam_sweep_result.py`

**Interfaces:**
- Consumes: `authorized_targets` (T8), `observations.upsert_observation` (T5), `JobEngine`, `publish_job`.
- Produces:
  - `NetSweepParams` / `NetSweepDocument` (schema.py, both `extra="forbid"` on params)
  - `create_sweep(session, job_engine, org_id, *, subnet_id, actor_id, trigger) -> tuple[SweepRun, envelope | None]`
  - `select_sweeper(session, org_id, subnet) -> UUID | None`
  - `handle_sweep_result(session, job, result)`
  - `sweep_dispatch_sweep_all_orgs(now)` + beat entry `ipam-sweep-dispatch`
  - `SWEEP_TTL_S = 600`

**Params** (§5.2) — `subnet_id`, `sweep_run_id`, `targets: list[str]`, `scope_digest`, `method: "icmp"|"icmp+arp"`, `rate_pps 1..200 = 50`, `probe_timeout_ms 100..5000 = 500`, `max_targets 1..65536 = 4096`, `budget_s 10..900 = 300`.

- `targets` is an **explicit enumerated list**, never "the subnet". The agent is never asked to decide what it may touch, and has no local configuration to consult.
- `expires_at` is pinned to **600s** via `JobEngine.create(..., ttl_s=SWEEP_TTL_S)` rather than the kernel's 3600s default, because it bounds how long a revoked authorization can still be executed by an in-flight signed job.
- `rate_pps`, `max_targets` and `budget_s` are inside the **signed** params, so the ceiling on what a sweep can cost is covered by the signature.

**Result document** (§5.4) — `subnet_id`, `sweep_run_id`, `scope_digest`, `method`, `started_at`, `finished_at`, `probed_count`, `truncated`, `hosts: [{address, mac|null, responded_via, hostname|null}]`, `errors: [str]` bounded to 20 × 200 chars.

- **There is no RTT field, no per-probe timing, no retry count, and no `reachable: false` entry.** A host that did not answer is simply absent. Add any of them and this becomes the monitoring product one field at a time. **Write a test that asserts `NetSweepDocument` rejects a `hosts[]` entry carrying an `rtt_ms` key** — an `extra="forbid"` on the host model makes §0.10 point 5 a schema property rather than a matter of restraint.
- `hostname` is display-only agent-side reverse DNS: never a reconciliation predicate, never matches an address to a device.

**Dispatch** (§5.3), in order:
1. `subnet.sweep_device_id` if set, its agent `online`, and `agents.capabilities` advertises `net.sweep`.
2. Otherwise auto-select deterministically among devices whose **current** inventory observations include an address inside the subnet, ordered by (`Agent.status='online'` desc, `Agent.last_seen_at` desc, `Device.id`). Write the choice to `ipam_sweep_run.device_id` so "who probed this segment" is answerable afterwards.
3. Otherwise record `state='no_agent'` and **dispatch nothing**. This surfaces on the subnet page and feeds Task 10's Gate E.

`agents.capabilities` is a shipped jsonb column that nothing in this repository populates (the agent writes it at enrollment), so dispatch **tolerates an empty capability map**: it attempts dispatch, and a job that comes back `refused` (`JobStatus.refused` already exists in the engine's transition table) marks that device ineligible for sweeps for 7 days. Capability advertisement is the fast path; refusal is the correct fallback.

**Site servers:** there is no site-server entity in this codebase, and `JobEngine.create` hard-sets `Job.target_type="device"`, so a non-`Device` cannot be a job target. The preference order above is written so a site server slots in at position 0 when that workstream delivers. **No kernel change is requested.**

**The refusal is on the near side of the signing boundary.** `create_sweep` calls `authorized_targets`; on an empty set it records the run `state='refused'`, `error='not authorized'`, and **raises `IpamAuthorizationError` before calling `JobEngine.create`** — so no job row is created, no canonical payload is built, and **no signature is ever produced**.

**The result handler** (§6.4) runs inside the job's tenant transaction (`JobEngine.complete`'s extension point) and, in this order:
1. Recomputes the permitted set **from the job's own signed `params.targets`**, not from the current database — recomputing from live authorizations would let a scope granted *after* dispatch retroactively bless results.
2. Verifies `scope_digest`, `subnet_id` and `sweep_run_id` echo the params.
3. Verifies `len(hosts) <= params.max_targets`, that every `address` parses, that its family matches the subnet, and that every non-null `mac` parses.
4. Verifies **every** `hosts[].address` is contained by `params.targets`.

Any failure raises `IpamSweepRejected` → the job flips to `failed` and, because the handler runs in the job's transaction, **zero** observations are written. Two audit events: `ipam.sweep_rejected`, and for a scope violation specifically `ipam.sweep_out_of_scope` with `actor_type='agent'`, the device id, the offending count and the first 10 offending addresses. The `ipam.sweep_rejected` domain event is published (`subject_type="ipam_sweep_run"`, `subject_id=run.id`).

**Whole result, not the bad rows** — following `app/modules/security_logs/ingest.py`, which rejects an entire shipped batch on a single out-of-taxonomy event. Filtering optimises for keeping data; rejecting optimises for detecting the compromise. An agent returning addresses it was not authorized to probe is either buggy or controlled by someone else, and dropping the bad rows keeps its good rows flowing while hiding the tell. Cost accepted: one malformed address wastes one sweep; the next scheduled run recovers it.

On success: `source='sweep'`, `method='arp'|'icmp'` from `responded_via`, `confidence='medium'` for ARP and `'low'` for ICMP, **`device_id` always NULL** (a sweep sees addresses and MACs, not identity — letting it assert one would be a compromised agent's cheapest way to re-point an assignment), and `org_id` **from the job**, never from the payload.

**The beat entry:** `"ipam-sweep-dispatch": {"task": "app.worker.ipam_sweep_dispatch", "schedule": 3600.0}` alongside the others near `app/worker.py:1069`, following `patch_scan_sweep_all_orgs`'s shape exactly — enumerate orgs on the admin engine, one `org_session` per org, collect envelopes, and `publish_job` **only after the transaction commits**. A subnet is due when `sweep_enabled`, `sweep_interval_hours IS NOT NULL`, and `last_swept_at` is older than that many hours. **The interval is clamped server-side to ≥ 1 hour** (default 168h): a one-minute sweep interval is the exact mechanism by which an IPAM becomes a ping monitor, and the clamp is the thing that prevents it. There is **no local agent schedule** — §0.9: a locally scheduled sweep would be a standing authorization signed once and executed for weeks.

Required tests:

| Test | Falsifying mutation and observation |
| --- | --- |
| Dispatch to an unauthorized subnet raises **and creates no `jobs` row** — assert `SELECT count(*) FROM jobs` is unchanged | Move the check after `JobEngine.create`: the test observes a signed job row |
| The signed params contain the **intersected** target list and a `scope_digest` matching `net.scope_digest(targets)` | Pass `[str(subnet.prefix)]` instead: observes the un-intersected prefix in `job.params` |
| A result with **one** out-of-scope address fails the job, writes **zero** observations, and records both audit actions | Filter the bad rows instead of rejecting: observes `succeeded` and N-1 observation rows |
| A mismatched `scope_digest` is rejected the same way | Skip the digest check: observes `succeeded` |
| `len(hosts) > max_targets` is rejected | — |
| An unparseable address, and an IPv6 address in an IPv4 subnet, are each rejected | — |
| A sweep observation has `device_id IS NULL` even when the payload carries a device-shaped field | Trust the payload: observes the device id written |
| `org_id` comes from the job even when the payload supplies a different one | Read from payload: observes the foreign org id, which RLS then rejects — assert the **written row's** `org_id`, not the absence of an error |
| `NetSweepDocument` rejects `rtt_ms` inside a `hosts[]` entry | Set `extra="allow"`: observes acceptance |
| `select_sweeper` returns `None` for a subnet with no observing device, and the run records `state='no_agent'` with no job created | Fall back to "any device in the org": observes a job dispatched to an unrelated machine |
| A designated `sweep_device_id` whose agent is offline falls through to auto-select | — |
| A device whose job came back `refused` is skipped for 7 days | — |
| `ttl_s` is 600, not 3600 — assert `job.expires_at - job.issued_at` | Drop `ttl_s=`: observes 3600 |
| The beat entry is registered — follow the `test_create_celery_registers_*` precedent | — |

Also: export `NetSweepParams.schema.json` and `NetSweepDocument.schema.json` via `scripts/export_wire_schema.py` and commit them under `docs/kernel/wire-protocol/`. **That export is the contract the Go `net.sweep` capability is written against — the idle-budget requirements in §5.5 (dormant by default, no local schedule, bounded by signed params, sockets opened at job start and closed at job end) are that capability's obligation, restated in the exported README, and not implemented by this task.** The capability is unwritten; since `CLAUDE.md` `a32cead` it belongs under `agent/` here rather than in a separate repository, which changes who can write it and nothing about whether it exists.

Commit: `feat(ipam): the net.sweep capability, authorized dispatch, and whole-result rejection`

---

## Task 10: Reconciliation — drift classes, the two gates, and the daily sweep

**Files:** Create `backend/app/modules/ipam/reconcile.py`; Modify `backend/app/worker.py`; Test `backend/tests/test_ipam_reconcile.py`, `backend/tests/test_ipam_reconcile_sweep.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `evaluate_org(session, org_id, *, now) -> ReconcileStats`, `reconcile_sweep_all_orgs(now) -> int`, `acknowledge_drift`, `suppress_drift`, `list_drift`, `DRIFT_CLASSES`, `RECONCILE_INTERVAL_SECONDS = 86400.0`.

**The two gates are the module's whole thesis and the reason IPAM products get abandoned** — every one that got ignored got ignored because its drift list was mostly sleeping laptops.

- **Gate E (evidence available for subnet S):** a `net.sweep` run for S succeeded within `W_obs`, **or** some device with a current inventory observation inside S was ingested within `W_obs`. No evidence source ran ⇒ **no absence drift for the whole subnet**. Absence of evidence is not evidence of absence.
- **Gate L (claimant liveness):** `dev(D) IS NULL` **or** `live(dev(D))`, where `live` means `Agent.status='online'` **or** `Agent.last_seen_at >= now - W_obs`.
- **`live` reads `Agent`, never `Device`.** `Device.last_seen_at` is written only by inventory ingestion (`app/modules/inventory/ingest.py:85`) and by enrollment, so it means "last inventory sweep", not "last contact". Reading it would mark every connected machine that has not swept recently as dormant. **Write this as a test, not a comment:** a device whose `Agent.status='online'` but whose `Device.last_seen_at` is 60 days old must produce `documented_absent`, not `dormant`. *If the implementation read `Device`*, the test observes `dormant` — a different stored `drift_class`, in a different tab, excluded from counts.
- Note the asymmetry, because it is the idea: **silence is not information, but a live device that disagrees with the plan is.**

Implement §4.3's seven classes exactly as predicates: `documented_absent`, `present_undocumented`, `mac_mismatch`, `claimant_mismatch`, `device_moved`, `conflict`, and the `dormant` bucket. `dormant` is **not** a drift class: excluded from counts, excluded from "drift" exports, never published as an event, its own tab.

**`documented_absent` is a reclaim candidate, never an automatic reclaim.** Nothing in this module ever changes `state` by itself.

The sweep (§4.4), Celery beat `ipam-reconcile-sweep` at 86400s, following the `*_sweep_all_orgs` shape: enumerate orgs on the admin engine, one `org_session` per org. Per org: recompute every class, upsert `ipam_drift` on `(org_id, vrf_id, address, drift_class)` — a **total** unique index, so plain `index_elements` inference works — incrementing `consecutive_detections` for items detected again, stamping `resolved_at` and resetting `consecutive_detections` to 0 on items no longer detected. **Recomputation preserves `first_detected_at`, `acknowledged_by/at` and `suppressed_until`.** A resolved item is stamped, not deleted.

Publish `ipam.drift_detected` **only** for items whose `consecutive_detections` just reached 2 and which are neither acknowledged nor within `suppressed_until`. One sweep that ran while a switch was rebooting is not news. One event per item, no aggregate, so a rule can route `conflict` differently from `documented_absent`.

Acknowledgement and suppression require `ipam.assign` and are audited (`ipam.drift_acknowledged`, `ipam.drift_suppressed`, with the note).

Required tests — one per class from a real documented row plus a real observation, **and the negatives, which matter more**:

| Test | Falsifying mutation and observation |
| --- | --- |
| A device whose **agent** has been offline past `W_obs` produces `dormant`, not `documented_absent` | Delete Gate L: observes `documented_absent` in the drift list and in the counts |
| A device whose agent is **online** but whose `Device.last_seen_at` is ancient produces `documented_absent` | Read `Device` instead of `Agent`: observes `dormant` |
| A subnet with **no evidence** in the window produces **no absence drift at all** | Delete Gate E: observes N `documented_absent` rows for a segment nobody looked at |
| An **ICMP-only** observation can never produce `mac_mismatch` | Drop the confidence/mac predicate: observes a `mac_mismatch` row from an observation carrying no MAC |
| A **single** detection publishes no event; the **second** does | Publish on the first: observes 2 `domain_events` rows where 1 is expected |
| Acknowledgement and `first_detected_at` **survive** recomputation | Delete-and-reinsert instead of upsert: observes `acknowledged_at IS NULL` and a moved `first_detected_at` |
| A resolved item is **stamped, not deleted** | Observes 0 rows instead of 1 with `resolved_at` set |
| An acknowledged item that reaches its second detection publishes **no** event | — |
| `device_moved` fires (and `documented_absent` + `present_undocumented` do **not** separately) when a laptop's assignment is at `.42` and its current inventory observation is at `.57` | Without the class: observes two unrelated findings the operator has to correlate by hand |
| `conflict` fires on two distinct MACs at one address | — |
| `dormant` items are absent from the drift count and from the drift CSV export | — |
| Cross-org: org A's sweep touches nothing in org B — pinned on the **admin engine** | — |
| The beat entry is registered | — |

Commit: `feat(ipam): drift reconciliation, the two gates, and the two-detection rule`

---

## Task 11: API surface and RBAC completeness

**Files:** Create `backend/app/modules/ipam/api.py`; Modify `backend/app/modules/ipam/schema.py`, `backend/app/modules/ipam/__init__.py`, `backend/app/modules/__init__.py`; Test `backend/tests/test_ipam_api.py`

Routes exactly as spec §9 tabulates, under `/api/v1/ipam/...`, each carrying a `require_permission(...)` dependency so completeness is derivable from the router (`app/core/rbac.py::route_permission`) rather than from a list someone has to remember to update.

Requirements:
- Handlers stay thin: resolve principal, call service, map errors. `IpamNotFoundError` → 404, `IpamValidationError`/`IpamConflictError` → 400/409, `IpamTooLargeError` → **422** with `subnet too large to enumerate`, `IpamAuthorizationError` → **403**. **Let the error escape the `org_session` block** so the transaction rolls back; map it outside. Catching inside leaves partial writes committed.
- **404, never 403**, for another org's id. RLS makes that the natural outcome.
- `PUT /subnets/{id}/sweep-settings` clamps `sweep_interval_hours` to ≥ 1 and **refuses anything shorter with a 400** (§0.10 point 6). Audited as `ipam.sweep_settings_changed`.
- `POST /subnets/{id}/sweep` is gated on `ipam.sweep.run` and dispatches **within already-authorized scope only** — it calls Task 9's `create_sweep`, which calls Task 8's `authorized_targets`. It cannot create or widen an authorization.
- `POST/DELETE /scan-authorizations` is gated on **`ipam.scope.manage`** and nothing else.
- Register in `app/modules/__init__.py::register_all`. `ipam.register(app)` also calls `register_job_types()` (T9) and `register_post_ingest_hook(...)` (T5).

Required tests:
- **RBAC completeness derived from the router**, using the flattener copied from `tests/test_email_intake_api.py`. **Pin the exact route count**; a `>=` assertion hides a missing route. **Removing `require_permission` from one route must turn the completeness test red — prove it and paste the output.**
- The permission split, asserted as behaviour: a **technician can dispatch a sweep** inside an authorized scope (200) but **cannot create a scan authorization** (403). *If `ipam.sweep.run` and `ipam.scope.manage` were collapsed*, the second call observes 200 — anyone who can run a scan can decide what may be scanned, which is the whole control.
- A read-only user cannot assign (403). An `ipam.read` holder without `audit.read` **can** read `/addresses/{id}/history` (200).
- Cross-org ids 404 on every `{id}` route — parametrize.
- `422` on next-free above the ceiling and on an IPv6 subnet; `409` on assignment inside a DHCP pool; `400` on `sweep_interval_hours=0`.

Commit: `feat(ipam): the API surface with router-derived RBAC completeness`

---

## Task 12: Import, export, and absorbing documentation's `network` records

**Files:** Create `backend/app/modules/ipam/importer.py`; Modify `backend/app/modules/ipam/api.py`, `backend/app/modules/documentation/service.py`, `backend/app/modules/documentation/api.py`; Test `backend/tests/test_ipam_import.py`, `backend/tests/test_ipam_export.py`, and append to the documentation tests

**This task touches two files documentation owns.** Flag both. See spec correction **S3** (the file locations in §14.4 are wrong) and **S4** (there is no "once IPAM is installed" state — the refusal is unconditional).

**CSV import, two steps**, because a one-shot import into a plan of record is how a plan of record gets destroyed:
- `POST /ipam/import/preview` (`ipam.manage`) — multipart CSV (≤10 MB, ≤50,000 rows) plus a `{csv_column: ipam_field}` mapping. Returns per-row parse results, per-row validation errors, and a **diff** classified as create / update / conflict. **Writes nothing.**
- `POST /ipam/import/apply` (`ipam.manage`) — same file digest, same mapping, applied in **one** transaction. Audited `ipam.import_applied` with row counts and the file digest, so the import is reproducible from the audit log.
- An imported address with a claimant lands `state='assigned'`, `origin='imported'`, `allocation_method` from the mapping or `unknown`.
- **Unparseable rows are reported, never silently skipped** — silent skipping is how an import "succeeds" and loses a third of the sheet.
- Import **never** creates authorizations and **never** enables sweeps. Test both negatives explicitly: *if it did*, an import would become a way to authorize scanning without holding `ipam.scope.manage`.

**Export:** `GET /ipam/export/{subnets,addresses,drift}.csv`, `ipam.read`, streamed flat columns, `text/csv`, following `app/modules/inventory/api.py`'s device/software exports — and **every operator- or agent-supplied text column goes through `escape_formula`** (`app/core/csv_export.py`): subnet and site names, descriptions, hostnames, asset labels, drift notes, sweep `error` strings. Not the whole row — per column, as that module documents. `dormant` items are excluded from `drift.csv`.

**The documentation absorption:**
- `documentation/service.py::create_record` refuses `record_type='network'` unconditionally with a `ValueError` naming `POST /api/v1/ipam/subnets`. `network` **stays** in `RECORD_TYPES`; existing rows stay readable, editable and archivable — migrations are forward-only and existing rows must keep working. Test all three: create refused, existing row still updatable, existing row still archivable.
- `documentation/service.py::_LINK_TARGET_MODELS` gains `ipam_subnet` and `ipam_address`; `documentation/api.py:60`'s `target_type` pattern gains both (`doc_links.target_type` is `String(20)` — both values fit).
- `POST /ipam/import/doc-records` (`ipam.manage`, **`?dry_run=true` by default**) converts each non-archived `network` record whose `fields.cidr` parses: `name ← record.name`, `prefix ← ip_network(fields.cidr, strict=False)` (the same call `NetworkFields._valid_cidr` already makes), `vlan_id ←` the org's VLAN with `vid = fields.vlan` if one exists (else null **and reported**), `gateway ← fields.gateway` if it parses and falls inside the prefix, `dns_servers ← fields.dns` split on commas, `kind='assignable'`, `doc_record_id ←` the source row. The source record is then **archived, not deleted**, so `doc_links` and `doc_versions` survive (`doc_versions.record_id` is a real FK).
- Rows that do not convert — unparseable CIDR, a prefix already registered, a gateway outside its prefix — are returned in a `skipped` list **with reasons**. Nothing is guessed. This is deliberately **not** a data migration: a silent conversion would hit exactly those cases with no operator to ask.
- Audited `ipam.import_previewed` / `ipam.import_applied`.

Tests: preview writes nothing (assert row counts before and after); apply is transactional (force a failure on the last row and assert **zero** rows written); unparseable rows reported not skipped (assert the count in the response equals the count in the file); the converter archives sources and sets `doc_record_id`; each skip reason is produced by a real case; the dry-run default (calling apply with no query param must **not** write — *if the default were inverted*, the test observes converted subnets).

Commit: `feat(ipam): CSV import/export and the documentation network-record absorption`

---

## Task 13: The NIC address-block contract — half of it needs a Go collector nobody has written

> **This is the only task in the plan whose full value depends on agent-side code that does not exist.** Everything above ships and is useful without it. Build this task last, and be explicit in your report about what it does and does not deliver.
>
> *Retitled 2026-08-05.* This task was headed "BLOCKED on `it-suite-agent`", which named a *blocker* — another repository's backlog — rather than a *gap*. `CLAUDE.md` commit `a32cead` moved the agent in-tree to `agent/`, so there is no longer anything blocking it: the Go collector change described below is writable in this repository, by whoever picks it up. It is still **not written**, the in-repo half below is still the only thing this task delivers, and every row of the degradation table is still accurate.

**Files:** Modify `backend/app/modules/ipam/nic.py`, `backend/app/modules/ipam/addresses.py`, `docs/kernel/wire-protocol/InventoryDocument.schema.json`, `docs/modules/inventory/spec.md` (contract note); Test extend `backend/tests/test_ipam_nic.py`, `backend/tests/test_ipam_observations.py`

**What is missing, precisely.** `ComponentDoc` is `{type, identifier (str ≤200), attributes: dict[str, Any]}` and the exported wire contract declares `attributes` as `"additionalProperties": true` — **there is no NIC attribute contract at all.** The only IP-bearing NIC shape anywhere in this repository is one test fixture, `{"type":"nic","identifier":"a4:bb:6d:11:22:33","attributes":{"ip":"10.1.2.42"}}` (`backend/tests/test_inventory_schema.py:25`). The richer shape (§14.1) is:

```jsonc
{ "type": "nic",
  "identifier": "a4:bb:6d:11:22:33",
  "attributes": {
    "name": "Ethernet0",
    "mac": "a4:bb:6d:11:22:33",
    "addresses": [
      { "address": "10.1.2.42/24", "family": "ipv4", "origin": "dhcp" },
      { "address": "fe80::1%4/64", "family": "ipv6", "origin": "link_local" }
    ] } }
```

**What must happen agent-side (under `agent/`, unwritten, out of scope for *this* task):** the Go inventory collector must emit that `attributes` block for every NIC, with `origin ∈ {static, dhcp, slaac, link_local, unknown}` derived from the OS (Windows: `Get-NetIPAddress`'s `PrefixOrigin`/`SuffixOrigin`; Linux: `ip -j addr` `dynamic` flag; macOS: `ipconfig getpacket`). Track it against this task and the exported `InventoryDocument.schema.json`. Because `InventoryDocument` is `extra="ignore"`, an **older agent that omits the block still validates** — there is no flag day, and mixed fleets are the normal state. That leniency is exactly the compatibility rule in `docs/kernel/wire-protocol/README.md`, and it matters *more* now that both sides can be changed in one commit: the schema addition in step 4 below is additive documentation precisely so a fleet of older agents keeps ingesting.

**What is buildable in this repo today, and is this task's actual deliverable:**
1. Extend `nic.observations_from_components` to read `attributes["addresses"]` when present and fall back to `attributes["ip"]` when it is not. Parsed with IPAM's **own lenient model** (`extra="allow"`), so a malformed block degrades to "no observation" and can never fail an `inventory.collect` job. One observation **per address**, not per NIC.
2. Prefer `attributes["mac"]` over `identifier` for the MAC when both are present and both parse.
3. Map `origin` → `ipam_address.allocation_method` for `origin='discovered'` rows, replacing the permanent `'unknown'` from Task 7.
4. Update `docs/kernel/wire-protocol/InventoryDocument.schema.json` to **document** the NIC attribute block (still `additionalProperties: true` — this is additive documentation of a shape, not a tightening that would reject older agents). Re-run `scripts/export_wire_schema.py` and confirm the diff is only the addition.

**What degrades until the agent change ships, stated plainly so nobody reports this module as broken:**

| Capability | Without the agent change | With it |
| --- | --- | --- |
| Inventory-derived observations | One per NIC, from the legacy `ip` key — **works today** | One per address, IPv4 + IPv6 + secondaries |
| IPv6 reconciliation from inventory | **Absent** — the legacy key carries one address and it is the v4 one | Works |
| Secondary / multi-homed addresses | Invisible | Visible |
| `allocation_method` for observed addresses | Permanently `unknown` (§3.2) | Real (`static` / `dhcp` / `slaac`) — **shipped**, derived at read time |
| Interface name on an observation | Absent | Present |
| MAC when `identifier` is a friendly name | `NULL`, so `mac_mismatch` cannot fire for that NIC | From `attributes.mac` |
| Everything else — subnets, ranges, allocation, sweeps, drift, import/export, API | **Unaffected** | Unaffected |

Nothing in Tasks 1-12 reads the new shape, so none of them regress when it is absent.

Required tests: the §14.1 shape produces **one observation per address**; the legacy shape still produces one; a NIC carrying **both** produces the addresses block's set (not both sets doubled); `origin` maps to `allocation_method`; link-local and loopback entries inside the block are still discarded; a malformed `addresses` entry (a string where an object was expected, a missing `address` key, an `addresses` value that is not a list) yields the **other** entries and no exception — assert the surviving list, not the absence of an error.

Commit: `feat(ipam): read the NIC address block, with the legacy ip key as fallback`

> **SUPERSEDED 2026-08-05 — do not follow this task's fallback instructions.**
> Step 1's "fall back to `attributes['ip']`" and the required test "the legacy
> shape still produces one" describe a transitional dual path that has since
> been removed from **both** sides: the agent no longer emits `attributes.ip`
> and `nic.py` no longer reads it. The reason was compatibility with a deployed
> control plane, and there is none — the platform is not live. See
> `docs/modules/agent/open-questions.md` OQ-2 (closed, with the argument kept)
> and `docs/modules/ipam/spec.md` §4.1/§14.1, which are authoritative.
>
> Step 3 (`origin` → `allocation_method`) was never built by this task and is
> now **CLOSED** by its own change: `nic.py` reads `origin` into
> `NicObservation.allocation_origin`, migration **0051** adds
> `ipam_observation.allocation_origin`, and §3.2's method is derived at read time
> by `allocation.derive_allocation_method`. Two details of step 3's wording were
> wrong and are recorded rather than quietly followed:
>
> * "for `origin='discovered'` rows" describes rows that do not exist. No code
>   path in this module has ever created an `ipam_address` row with
>   `origin='discovered'` — `create_address`/`reserve` write `manual` and the
>   importer writes `imported` — so there was nothing to map onto.
> * Writing the reported method onto `ipam_address.allocation_method` at all is
>   refused by §0.6, which is the decision this module is built around. The
>   fleet's report lives on the observation; the answer is derived. See §3.2.

---

## Task 14: Frontend, spec amendments, and verification

**Files:** Create `frontend/src/modules/ipam/{api.ts,types.ts,status.ts,SubnetTree.tsx,SubnetDetail.tsx,AddressGrid.tsx,DriftPage.tsx,SweepPanel.tsx,SearchBox.tsx}` and tests; Modify the device-detail page; Modify `docs/modules/ipam/spec.md`

**Frontend** per spec §12, matching `frontend/src/modules/notification/`'s conventions. Behaviours that must be tested on **rendered output**, not on a fixture defined in the same file:

1. **Last-seen is rendered as an age, never as a timestamp presented as liveness** (§0.10). A grid showing `2026-08-01 14:03` in a column a reader will interpret as "up" is the monitoring product arriving through the UI.
2. **`dormant` and `orphans` are tabs *outside* the drift count.** Assert the badge number equals the drift rows and **excludes** the dormant ones. §0.11's whole point is that they are not the same information; a count that merges them is the bug this module exists to avoid.
3. **A container prefix renders no utilisation bar**, and a subnet above the ceiling renders the counts panel with **no percentage** — not `0%`, not a spinner.
4. **Mutation UI is hidden without the permission, and the server enforces regardless.** Test the hiding; do not test it *instead of* the server check.
5. `status.ts` mirrors `derive_status` (T6) and is table-tested against the **same** cases, so the two cannot drift.

**Do not write a test that asserts `Object.keys()` over a fixture defined in the same file.** That test cannot fail; this project has shipped one.

**Spec amendments.** ~~Fold S1-S6 into `docs/modules/ipam/spec.md`.~~ **Already done** — S1-S6 and the inventory hook's shape and SAVEPOINT are folded into the spec as §0.15-§0.18, §14.6, and corrections in place at §2.3, §2.7, §3.1, §4.3, §8.3, §11.1, §13 and §14.4. Do **not** redo it. What remains for this step is only what the *build* forces: every deviation you took from the spec as amended, and anything the code proved wrong that neither the spec nor this plan anticipated. If you deviated, amend the spec section that is now wrong rather than appending a list of exceptions.

**Verify and record the counts:** `cd backend && .venv/bin/pytest -q`, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`; frontend `npm test` and `npx tsc --noEmit` (`npm run lint` does not exist in this repo).

**The live smoke test** — a script under the scratchpad directory driving real services against a real Postgres, not mocks:

1. Create an org, a site, a `/24` subnet in the lazily-created `global` VRF, a DHCP pool `.100-.200`, and two devices with agents.
2. Ingest inventory for one device carrying a NIC at `.42` → assert one high-confidence observation, `subnet_id` resolved by containment.
3. `next-free` → assert it skips `.0`, `.255`, the pool, **and `.42`** (which has no `ipam_address` row at all — only an observation).
4. `allocate` → assert it takes the next legal address and audits `ipam.address_assigned`.
5. Attempt `POST /subnets/{id}/sweep` with **no** authorization → assert 403, a `refused` run, and **zero rows in `jobs`**.
6. Create an authorization for a **supernet**, dispatch again → assert one job whose signed `params.targets` is the `/24` minus the excluded range, with a matching `scope_digest`, and `expires_at - issued_at == 600`.
7. Complete that job with a result containing one address **outside** the targets → assert the job is `failed`, **zero** observations were written, and both audit actions plus the `ipam.sweep_rejected` domain event exist.
8. Complete a clean result → assert observations written with `device_id IS NULL`.
9. Take the assigned device's agent offline past `W_obs`, run the reconcile sweep twice → assert `dormant`, **not** `documented_absent`, and **no** `ipam.drift_detected` event.
10. Bring the agent online with a NIC set that no longer contains the documented address, run the sweep twice → assert `documented_absent` and exactly **one** `ipam.drift_detected` event (on the second run, not the first).

Steps 5-7 and 9-10 matter most: they are the only end-to-end proof that the authorization the API enforces and the scope the result handler checks are the same scope, and that the gates behave on real agent state rather than on a fixture that agrees with them. **Report plainly what it did and did not cover** — if a step could not be driven end to end, say so rather than substituting a mock.

Commit and push; do **not** open a pull request.

---

## Self-review

**Spec coverage.** §0.1 native types → T1. §0.2 derived hierarchy → T4. §0.3 advisory-lock overlap → T4. §0.4 sparseness + ceiling → T6. §0.5 IPv6 stored not enumerated → T1, T6. §0.6 state vs observation vs status → T6, T7 (the "no ingestion path writes state" test). §0.7 default VRF → T3. §0.8 materialised drift → T10. §0.9 server-side schedule → T9. §0.10 the monitoring boundary → T9 (`rtt_ms` rejection, the interval clamp), T14 (age rendering). §0.11 the two gates → T10. §0.12 deny-by-default → T8. §0.13 audit-history projection → T7. §0.14 documentation absorption → T12. §2 data model → T1. §3 assignment → T6, T7. §4 reconciliation → T5, T10. §5 `net.sweep` → T9. §6 governance → T8, T9. §7 boundary → T9, T14. §8 import/export → T12. §9 API → T11. §10 events → T1 (registration + renderers), T9 + T10 (publication). §11 tenancy/RBAC/audit → T1 and every service task. §12 frontend → T14. §13 testing → distributed, with the falsifying mutation named per assertion. §14.1 → T13 (server half only; the collector change is unwritten). §14.2 kernel: none. §14.3 events → T1. §14.4 documentation → T12. §14.5 deployment: none. §15 deferrals → nothing built.

**Sequencing claim.** Tasks 1-8 and 10-12 are buildable today against the legacy `attributes.ip` key. Task 13 is the only one whose full value depends on an agent-side collector change that has not been written, and its server half (lenient parsing of both shapes, schema documentation) is still buildable and testable here with synthetic payloads. The deny-by-default authorization surface (T8) is complete and falsified before T9 introduces the module's first `JobEngine.create` call.

**Gaps found and closed while reviewing.** S1 is the serious one: the spec's own observation upsert key contains a nullable `mac`, so ICMP-only observations would never conflict and every weekly sweep would append a row — silently converting the current-state table into the probe time-series that §0.10 point 4 exists to forbid. S2 is the same defect in `ipam_vlan`. S6 resolves a genuine ambiguity between `dormant` and `assigned` that would otherwise be settled by whichever order a builder happened to write the branches in.

**Known risk this plan does not remove.** The two gates (T10) are the module's differentiator and are also the hardest thing here to test honestly: every gate test needs a fixture where the gated and ungated paths produce *different* `drift_class` values, and it is easy to build one where they happen to agree. The table in T10 names the falsifying mutation for each, and the smoke test's steps 9-10 exist because a fixture that agrees with the implementation is exactly the failure mode a unit test cannot see.
