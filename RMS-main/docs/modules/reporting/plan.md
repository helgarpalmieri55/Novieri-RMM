# Reporting & Analytics — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fixed catalogue of code-defined, bounded, typed, self-describing cross-module reports, authorized by the conjunction of every source module's read permission, so that an operator can never see through a report what the module APIs would refuse them directly.

**Architecture:** A `ReportDefinition` registry compiled into the image. Each definition declares its `sources` (table names) and one bounded `Select`; `requires` is **derived** from `sources` through a module-level `TABLE_PERMISSIONS` map and is not settable by a definition author. Routes are **generated from the registry in a loop**, so the five "clean enough to be a tool" properties are supplied by the loop and cannot be omitted by a route author — there is no route author. Every route carries exactly one kernel `require_permission("reporting.read")` (so `route_permission` still recovers exactly one code by reflection) **plus** a separately-named `require_report_sources(key)` dependency that enforces the conjunction. A build-time verifier compiles each definition's statement, walks its froms, and fails the build if the SQL reads a table the definition did not declare. Reads run live inside `org_session` under `SET LOCAL statement_timeout`. Nothing is stored; no table is added; no worker, no beat entry, no job type, no agent capability.

**Tech Stack:** Python 3.12, FastAPI 0.139.2, SQLAlchemy 2.0.51 (typed), Pydantic 2.13.4, Alembic, Postgres 16 RLS, React + TypeScript. **No new dependency** — the statement walker is `sqlalchemy.sql.visitors`, the CSV writer is stdlib `csv`, the escaper is the existing `app/core/csv_export.py`.

---

## Global Constraints

Every task's requirements implicitly include this section. These are earned lessons from previous sub-projects of this repo, not general advice.

1. **Authority is `docs/modules/reporting/spec.md` (commit `9eabdf0`).** Where this plan and the spec disagree, the spec wins. Where the **code** and either of them disagree, see constraint 10.

2. **Touching `app/core/events.py`, `app/core/rbac.py`, or `app/models/__init__.py` requires running the FULL backend suite, not just this module's tests.** This module touches exactly one of them (`rbac.py`, Task 1) and neither of the others. A permission-set literal has broken this way **three times**: `tests/test_rbac.py::test_permission_registry_complete` asserts `set(PERMISSIONS) == {…}` as a literal, `tests/test_rbac.py::test_org_admin_has_all_permissions` asserts an org-admin's granted codes `== set(PERMISSIONS)` (so a code in the dict but not seeded to `org-admin` turns it red), and `tests/test_api_routes.py::test_me_returns_identity_and_permissions` asserts the **technician** role's exact code set (so seeding `reporting.read` to `technician` turns *that* red). Task 1 states the resolution; do not discover it by running one module's tests.

3. **Fixture blindness is this project's defining defect.** A guard test must make the guarded and the unguarded path produce **different observable values**. If the fixture makes both paths agree, the assertion is decoration and the test passes against the bug. Every task below that adds a guard states what the test would observe if the guard were deleted — implement that observation, not a weaker one. Three known traps, each of which has shipped here:
   - **A mutation that only prints `DID NOT RAISE` has told you nothing about which guard fired.** Assert on the *content* of the rejection — the offending table name, the missing permission code — not merely that something raised.
   - **A constant parameter across fixtures.** If every seeded org, device, window or permission set carries the same value for the thing under test, the predicate that reads it is unobservable. Vary it deliberately.
   - **An empty-answer test satisfied by the function's own default.** `assert result == []` passes against a function that returns `[]` unconditionally, and `assert set(REGISTRY) >= {…}` passes against an empty registry. Assert non-emptiness first, then assert the content.

4. **RLS masks application predicates.** A cross-org test driven through `org_session` passes even when the service's own `org_id` predicate has been deleted, because RLS rejects first. To pin an application-layer predicate you must exercise it on the **admin engine**, where RLS is not the thing doing the rejecting. This defect shipped four times in the SLA sub-project. Spec §6.4 requires an explicit `org_id ==` predicate on **every** report statement in addition to RLS; **at least one test per report pins that predicate on the admin engine.**

5. **A report is a read across tenants' data by construction.** It is a wide join over five modules' tables with no per-row route in front of it, so "provably org-scoped" is not a formality here. Every tenancy test must seed **two orgs with deliberately overlapping data** — same hostnames, same queue names, same OS families, timestamps inside the same window — so that an org predicate is observable at all. Two orgs whose rows differ in every column make a missing predicate invisible: the assertion passes because the other org's rows would not have matched the filters anyway. (This is constraint 3's "constant parameter" trap wearing a tenancy costume.)

6. **`SET LOCAL statement_timeout` is the spec's answer to unbounded queries (§0.3, §6.4), and no `statement_timeout` is set anywhere in this codebase today.** A test must prove it *applies* (a statement that exceeds it is cancelled and surfaces as the typed 504) **and** that it does not leak to the next checkout of a pooled connection (`SET LOCAL`, not `SET`). Both directions; see Task 4.

7. **413 rather than truncate, on export.** A truncated CSV is indistinguishable from a complete one once it is in a spreadsheet, so it is a wrong-number defect, not a paging inconvenience (§0.8). **The test must observe the status code and the absence of a CSV body — never the row count.** A test that asserts `len(rows) <= cap` passes against an implementation that truncates, which is the exact bug.

8. **`escape_formula` guards `= + - @` only.** Verified at `app/core/csv_export.py:57`: `_DANGEROUS_PREFIXES = ("=", "+", "-", "@")`. It does **not** guard a leading tab, a leading CR/LF, or an embedded NUL, and it is **not** to be extended in this cycle — six modules call it (`ipam`, `inventory`, `budget`, `backup`, `patch`, `security_logs`; its own docstring still says "five") and budget's Odoo export is a pinned wire contract read by an ERP importer. Re-read that file before relying on it, and normalise locally (Task 9).

9. **Do not poll with `pgrep -f "<pattern>"`.** The polling shell's own command line contains the pattern, so `pgrep` matches itself, the loop never terminates, and the session deadlocks. If you must check on a long-running process, make **one foreground call redirected to a file** and read the file.

10. **Trust reality over the spec and over this plan.** If a signature, a table name, a constraint or a behaviour in the code contradicts either document, **the code wins, you implement what the code says, and you report the divergence.** This plan was written against the code at `6d57334`; other agents are working concurrently and the code may have moved.

11. **Migrations are forward-only; never edit a shipped migration. Do not hardcode a migration number from this plan or from the spec.** The head at writing was `0046_approved_action_fuse_ceilings`, but other module cycles are in flight. Before the migration, run `cd backend && .venv/bin/alembic heads` and `ls migrations/versions/`, take the next free number, and set `down_revision` to the bare short string of the **actual** head (e.g. `"0047"`), matching every other migration in this repo.

12. **Tooling.** `pytest` is at `backend/.venv/bin/pytest`; bare `pytest` and `python -m pytest` are not installed. Use `cd backend && .venv/bin/pytest tests/... -v`, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`, `.venv/bin/alembic ...`. Frontend: `npx tsc --noEmit` is the only static check (`npm run lint` does not exist); `npm test` for the suite.

13. **Measure the baseline before you start.** Run the full backend suite once and record the pass count. Do **not** trust a number written in this document or in a sibling plan — concurrent agents are adding tests. Do not regress that number.

14. **Postgres and Redis go down in this container during long runs.** Check `pg_isready` and `redis-cli ping` first, and again if anything hangs; restart them yourself (`service postgresql start`, `redis-server --daemonize yes`) and carry on. Do not background a long test run and wait on it.

15. **Naming and shape facts, verified against the code — do not guess.**
    - Kernel tables are plural: `devices`, `device_groups`, `agents`, `users`, `organizations`, `audit_events`, `domain_events`, `permissions`, `roles`, `role_permissions`, `user_roles`, `credentials`, `enrollment_tokens`, `org_vault_keys`, `notification_channels`, `webhook_allowlist_entries`. Patch tables: `patches`, `device_patch_status`, `patch_policies`, `patch_approvals`, `patch_runs`. Backup tables: `backup_repositories`, `backup_policies`, `backup_runs`, `backup_points`, `restore_jobs`. Health tables: `health_alert`, `health_profile`, `health_check`, `device_health_check_state` (**singular**). Service-desk tables are **singular**: `ticket`, `ticket_queue`, `ticket_category`, `ticket_comment`. SLA: `ticket_sla`. Email intake: `email_mailbox`, `inbound_message`. Grep `__tablename__`; do not guess.
    - `permissions` is `(id, code, module, action)` — there is **no** `resource` and **no** `description` column. Descriptions live only in `app/core/rbac.py::PERMISSIONS`.
    - `AuditEvent`'s metadata attribute is **`meta`** (mapped to column `metadata`).
    - Tenancy GUC is **`app.current_org`**, set by `org_session` with `set_config(..., true)` (transaction-local). There is no role or scope GUC — spec §0.4 rests on this.
    - Modules register in **`app/modules/__init__.py::register_all`**, not `app/main.py`.
    - FastAPI 0.139.2: `app.routes` **is** `app.router.routes` and **neither contains `include_router`'d routes** — only a `_IncludedRouter` whose own `.path` is `None`. A route-completeness test must flatten via `_IncludedRouter.original_router` + `include_context.prefix`. `backend/tests/test_ai_admin_registry.py::_flatten` is the working flattener this module reuses — **copy it; a naive `app.routes` filter finds nothing and the test passes vacuously** (constraint 3's empty-answer trap).
    - Tests use the `api_env` fixture with `asyncio_mode=auto` — **do not** write `@pytest.mark.asyncio`. `tests/conftest.py` provides `admin_engine`, `app_engine`, `api_env`, `a_ticket_for`, `add_comment_as_staff`.
    - Async throughout, full type hints, business logic in services and not in route handlers, `mypy` and `ruff` clean.

16. **Resync the index before staging.** Other agents commit into this working tree concurrently. Before every commit step: `git reset` (mixed, **no pathspec**) to drop anything another agent left staged, then `git add <your explicit paths> && git commit`. Never `git add -A`, never `git add .`, never `git commit -a`. Do not push; do not open a pull request.

---

## Verified facts you will build on

Confirmed against the code at `6d57334`. Re-verify before use (constraint 10).

```python
# app/api/deps.py
def require_permission(code: str) -> Callable[..., Coroutine[Any, Any, AuthContext]]
#   returns a closure named `dependency`, qualname "require_permission.<locals>.dependency",
#   with `code` as a genuine free variable. route_permission reflects on exactly this.

# app/core/rbac.py
PERMISSIONS: dict[str, str]                      # code -> human description
async def user_permission_codes(session, user_id, *, device_group_id: UUID | None = None) -> set[str]
async def has_permission(session, user_id, code, *, device_group_id=None) -> bool   # raises ValueError on unknown code
def route_permission(route: APIRoute) -> str | None
_PERMISSION_DEPENDENCY_QUALNAMES = frozenset({
    "require_permission.<locals>.dependency",
    "require_portal_permission.<locals>.dependency",
})

# app/core/tenancy.py
org_session(org_id, *, engine) -> AsyncSession    # SET LOCAL app.current_org, inside session.begin()
admin_session(*, engine) -> AsyncSession          # RLS-bypassing; NO report uses this

# app/core/audit.py
audit_service.record(session, *, org_id, actor_type, actor_id, action,
                     target_type="", target_id="", metadata=None) -> AuditEvent

# app/core/csv_export.py
escape_formula(value: Any) -> Any                 # prefixes "'" iff str startswith ("=", "+", "-", "@")
```

Facts that shape specific tasks:

- **There is no `inventory.*` permission code.** `app/modules/inventory/api.py` gates all six of its routes — including both CSV exports — on **`devices.read`**. The commissioning brief named `inventory` as a v1 source module; the spec's four reports do not read inventory tables, and if a later report does, `TABLE_PERMISSIONS` maps `inventory_snapshots` / `installed_software` / `hardware_components` / `inventory_changes` to `devices.read`, not to an invented code.
- **`app/modules/budget/api.py::_csv_response(rows, columns, escaped_columns)`** is the per-column escaping shape to copy (spec §0.8): escape only the named columns, render `None` as `""`, everything else untouched.
- **No `statement_timeout` appears anywhere in `app/` or `tests/`.** This module introduces the pattern.
- **`user_permission_codes` counts a group-scoped `user_roles` row only when asked about that same group**, and this module always asks with `device_group_id=None`. Spec §0.6's "a group-scoped grant does not run a report" is therefore already true — the task is to *pin* it, not to build it.

---

## File structure

**Created (backend):**

| File | Responsibility |
| --- | --- |
| `backend/migrations/versions/00NN_reporting.py` | Seed `reporting.read`. **No tables, no DDL, no RLS policy.** |
| `backend/app/modules/reporting/__init__.py` | `register(app)` |
| `backend/app/modules/reporting/errors.py` | `ReportNotFound`, `ReportPermissionDenied`, `ReportExportTooLarge`, `ReportTimeout`, `UndeclaredSource`, `DenylistedSource` |
| `backend/app/modules/reporting/registry.py` | `ReportDefinition`, `TABLE_PERMISSIONS`, `_NEVER_A_SOURCE`, `_NEVER_A_TOOL`, `REGISTRY` (a `MappingProxyType`), the `requires` derivation |
| `backend/app/modules/reporting/sources.py` | `tables_read(stmt) -> frozenset[str]`, `verify_definition(defn) -> None`. **The load-bearing module.** |
| `backend/app/modules/reporting/schema.py` | `ReportSource`, `ReportMeta`, `ReportPage[RowT]`, `ReportCatalogue`, `CatalogueEntry` |
| `backend/app/modules/reporting/service.py` | `run_bounded`, `execute_report` — count, page, totals, meta |
| `backend/app/modules/reporting/deps.py` | `require_report_sources(key)` |
| `backend/app/modules/reporting/export.py` | CSV writer, free-text normalisation, the cap |
| `backend/app/modules/reporting/api.py` | The generation loop, the catalogue route, the export routes |
| `backend/app/modules/reporting/definitions/__init__.py` | Imports the four definition modules and builds `REGISTRY` |
| `backend/app/modules/reporting/definitions/fleet_posture.py` | §5.1 |
| `backend/app/modules/reporting/definitions/patch_compliance.py` | §5.2 |
| `backend/app/modules/reporting/definitions/backup_coverage.py` | §5.3 |
| `backend/app/modules/reporting/definitions/sla_attainment.py` | §5.4 |

**Modified (backend):** `app/core/rbac.py` (**one** code — Global Constraint 2 applies), `app/modules/__init__.py` (`register_all`), `tests/test_rbac.py` (the `PERMISSIONS` literal).

**Not created, deliberately:** no `models.py`, no entry in `app/models/__init__.py`, no worker task, no beat entry, no job type, no event type, no notification renderer, no wire sample. Reporting owns no facts (spec preamble). If you find yourself adding any of these, stop and re-read §0.2/§0.3/§0.11 — you are building the thing the spec refused.

**Created (frontend):** `frontend/src/modules/reporting/` — `api.ts`, `types.ts`, `ReportCatalogue.tsx`, `ReportView.tsx`, and tests.

---

## Task 1: The permission code, the migration, and the empty module

**Files:**
- Create: `backend/migrations/versions/00NN_reporting.py`, `backend/app/modules/reporting/__init__.py`, `backend/app/modules/reporting/errors.py`
- Modify: `backend/app/core/rbac.py`, `backend/tests/test_rbac.py`
- Test: `backend/tests/test_reporting_migration.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the permission code `reporting.read`; the `app.modules.reporting` package; the error hierarchy every later task raises.

**This task touches `app/core/rbac.py`. Global Constraint 2 is in force: run the FULL backend suite before committing, not `tests/test_reporting_migration.py`.**

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_reporting_migration.py`:

```python
"""The one kernel change this module makes (spec §4.3, §13, §16.10).

There is no schema here -- reporting owns no tables -- so these tests are
entirely about the permission code and the roles it reaches.
"""
```

Cover:
- `"reporting.read" in PERMISSIONS`, and its description contains the "grants NOTHING on its own" phrasing spec §4.3 requires. Assert on the substring, not on presence of a key: a description nobody wrote is the failure mode.
- `reporting.read` is seeded into `permissions` (query the live table on the admin engine, not `PERMISSIONS`).
- `reporting.read` is granted to `super-admin` and `org-admin` (join `role_permissions`).
- `reporting.read` is **not** granted to `technician` — a positive assertion, because it is the fact `tests/test_api_routes.py`'s literal depends on and a silent broadening is exactly the kind of change that turns another module's test red for reasons nobody can trace.
- **No** `reporting.admin` and **no** `reporting.export` code exists (§4.3 rejects both). `assert not [c for c in PERMISSIONS if c.startswith("reporting.") and c != "reporting.read"]`.
- The migration adds no table: `assert not [t for t in Base.metadata.tables if t.startswith("report")]`.

- [ ] **Step 2: Run to verify failure** — `cd backend && .venv/bin/pytest tests/test_reporting_migration.py -v`. Expected: `KeyError`/assertion on the missing code.

- [ ] **Step 3: Add the code and the migration**

Add to `app/core/rbac.py::PERMISSIONS`, with the spec §4.3 wording:

```python
"reporting.read": (
    "View cross-module reports. Grants NOTHING on its own: every report "
    "additionally requires the read permission of every module it draws "
    "from, so a user holding this alone gets a 403 from every report."
),
```

**Role seeding — the resolution, recorded because the spec does not name roles.** Seed to `super-admin` and `org-admin` **only**, following `0045_ai_admin.py`'s `ai.use` precedent and for the same stated reason. `org-admin` is **mandatory**, not a choice: `tests/test_rbac.py::test_org_admin_has_all_permissions` asserts an org-admin's granted codes `== set(PERMISSIONS)`, so a code in the dict and not seeded to `org-admin` turns it red. `technician` is **excluded**, and that is also not a choice: `tests/test_api_routes.py::test_me_returns_identity_and_permissions` pins the technician role's exact set, and widening it there would be a change to another module's test on this module's behalf. An org that wants technicians to hold `reporting.read` grants it deliberately — and, per §4.3, gains nothing by it until they also hold the source codes.

Migration: **run `alembic heads` first** (constraint 11). Copy the permission-insert and role-grant SQL verbatim from the current head's neighbour `0045_ai_admin.py:225-241`; do not retype it. `downgrade()` deletes the `role_permissions` rows then the `permissions` row.

Update `tests/test_rbac.py::test_permission_registry_complete`'s literal — that is the correct fix, not a workaround.

`errors.py` defines the hierarchy now so later tasks import rather than invent: `ReportingError(Exception)` with `ReportNotFound`, `ReportPermissionDenied(missing: frozenset[str])`, `ReportExportTooLarge(cap: int, matched: int)`, `ReportTimeout(report_key: str)`, `UndeclaredSource(report_key: str, tables: frozenset[str])`, `DenylistedSource(report_key: str, name: str)`. Each carries the data its typed error body needs (§6.5) as an attribute, not only in the message string.

- [ ] **Step 4: Run the FULL backend suite** — `cd backend && .venv/bin/pytest -q`. Expected: baseline + the new tests, zero failures. If `test_api_routes.py::test_me_returns_identity_and_permissions` is red, you seeded `technician`; undo it rather than editing that literal.

- [ ] **Step 5: Commit**

```bash
git reset
git add backend/migrations/versions/00NN_reporting.py backend/app/modules/reporting/ \
        backend/app/core/rbac.py backend/tests/test_rbac.py \
        backend/tests/test_reporting_migration.py
git commit -m "feat(reporting): seed reporting.read, the module's one kernel change"
```

---

## Task 2: The definition registry and the derivation of `requires`

**Files:** Create `backend/app/modules/reporting/registry.py`; Test `backend/tests/test_reporting_registry.py`

**Interfaces:**
- Consumes: `errors.py` (T1).
- Produces:
  - `@dataclass(frozen=True) ReportDefinition` exactly as spec §3 shapes it, **with `requires` computed in `__post_init__`**, never accepted from the caller.
  - `TABLE_PERMISSIONS: Mapping[str, str]`
  - `_NEVER_A_SOURCE: frozenset[str]` (tables) and `_NEVER_A_COLUMN: frozenset[str]` (the `table.column` tail, §4.2)
  - `_NEVER_A_TOOL: frozenset[tuple[str, str]]` (method, path) — reporting's own never-a-tool list (§10.3), empty until Task 9 fills it
  - `REGISTRY: Mapping[str, ReportDefinition]` as a `MappingProxyType`
  - `register(definition) -> None`, used only by `definitions/__init__.py` at import time

No report definitions ship in this task. The registry is empty and that is fine — Task 3 is what makes an empty registry impossible to hide behind.

`requires` derivation: `frozenset(TABLE_PERMISSIONS[t] for t in sources)`. A table absent from the map raises at **import time**, naming the table — the module fails to import rather than defaulting to anything (§3). `requires` must not be a constructor parameter; make it a field with `init=False` set via `object.__setattr__` in `__post_init__`, so a definition author cannot hand-write (and therefore understate) it.

`TABLE_PERMISSIONS`, from §3 and §4.2, with the codes verified against `PERMISSIONS`:

| table | code | | table | code |
|---|---|---|---|---|
| `devices` | `devices.read` | | `backup_repositories` | `backup.read` |
| `agents` | `devices.read` | | `backup_policies` | `backup.read` |
| `device_groups` | `devices.read` | | `backup_points` | `backup.read` |
| `health_alert` | `health.read` | | `backup_runs` | `backup.read` |
| `device_patch_status` | `patch.read` | | `ticket` | `ticket.read` |
| `patches` | `patch.read` | | `ticket_queue` | `ticket.read` |
| `patch_approvals` | `patch.read` | | `ticket_sla` | `sla.read` |
| `patch_policies` | `patch.read` | | | |

**`TABLE_PERMISSIONS` names the *weakest* code that governs *any* column of the table** (§4.2) — `licenses` is the shape that forces this rule, and the secret columns live on `_NEVER_A_COLUMN` instead.

`_NEVER_A_SOURCE` (§3, §8): `credentials`, `org_vault_keys`, `enrollment_tokens`, `notification_channels`, `webhook_allowlist_entries`, `email_mailbox`, `inbound_message`. `_NEVER_A_COLUMN`: the agent certificate material on `agents`, `audit_events.hash`, `audit_events.prev_hash`, and the licence-key ciphertext column. **Read the models and use the real column names** — do not copy a guess out of this table.

Required tests:
1. Every key of `TABLE_PERMISSIONS` resolves to a real table in `Base.metadata.tables`, and every value is a key of `PERMISSIONS`. Both directions are a liveness check: a map entry naming a renamed table maps nothing.
2. `test_denylisted_sources_are_real_tables` (§12.3): every `_NEVER_A_SOURCE` entry resolves to a live table, and every `_NEVER_A_COLUMN` entry resolves to a live `table.column`. **This is the test that stops the tripwire rotting into pointing at nothing.** Assert the resolved set is non-empty first (constraint 3's empty-answer trap: an all-typo denylist and an empty denylist both pass a naive `all(...)`).
3. `_NEVER_A_SOURCE ∩ set(TABLE_PERMISSIONS) == ∅` — a table cannot be both readable and forbidden.
4. `requires` cannot be passed to the constructor: `pytest.raises(TypeError)` on `ReportDefinition(..., requires=frozenset())`.
5. A definition naming an unmapped table raises at construction, and the message names **that table**. (Constraint 3: assert the table name is in `str(exc.value)`, not merely that it raised.)
6. `REGISTRY` is a `MappingProxyType` and rejects `__setitem__` (§3 — "a mapping any code in the process can widen at runtime is not compiled into the image").

Commit: `feat(reporting): the definition registry and the derived permission requirement`

---

## Task 3: The source-declaration verifier — **the load-bearing task**

**Files:** Create `backend/app/modules/reporting/sources.py`; Test `backend/tests/test_reporting_sources.py`

**This task ships before any report exists, and every later report task is blocked on it.** Spec §0.4's guarantee — *"an operator never sees through a report what the API would refuse them directly"* — is a checkable property only because of this verifier. Without it, `requires` is a comment: a definition could declare `sources={"devices"}`, join `ticket` in its `build()`, derive `requires={"devices.read"}`, and serve ticket data to a caller the ticket API refuses. Nothing else in the design catches that. Sequencing it after the first report would mean shipping at least one report on an unverified declaration, which is the whole hole.

**Interfaces:**
- Consumes: `ReportDefinition`, `TABLE_PERMISSIONS`, `_NEVER_A_SOURCE`, `_NEVER_A_COLUMN` (T2); `UndeclaredSource`, `DenylistedSource` (T1).
- Produces:
  - `def tables_read(stmt: Select[Any]) -> frozenset[str]` — every table name the compiled statement reads, including through joins, subqueries, CTEs, `EXISTS` and scalar sub-selects.
  - `def verify_definition(defn: ReportDefinition) -> None` — raises `UndeclaredSource` / `DenylistedSource`, naming the offending table.
  - `def verify_registry() -> None` — every definition in `REGISTRY`.

**Implementation.** Build the statement with the definition's own default params, then walk it with `sqlalchemy.sql.visitors.iterate(stmt)` collecting `.name` from every `sqlalchemy.Table` element. `iterate` descends the whole clause tree, so a table reachable only inside a correlated subquery or an `EXISTS` is still found — **but prove that rather than assume it** (probe 3 below). Add a cheap independent tripwire so a traversal gap is loud rather than silent: compile the statement under the PostgreSQL dialect, regex every identifier following `FROM`/`JOIN`, and assert that set is a subset of what `iterate` found. If the regex finds a name `iterate` did not, `tables_read` has a hole and the build fails — the regex is not the guarantee, it is the alarm on the guarantee.

**Falsification — permanent, not a one-time manual mutation.** The test module defines its own deliberately-wrong probe definitions (not registered in `REGISTRY`) and asserts the verifier rejects each, naming the offending table:

1. **The undeclared join.** `sources={"devices"}`, `build()` returns `select(Device.id, Ticket.subject).join(Ticket, ...)`. `verify_definition` must raise `UndeclaredSource` and `"ticket"` must appear in the message. *This is the mutation the brief names: a report whose SQL joins a table it did not declare must fail the build.*
2. **The denylisted source.** `sources={"devices", "credentials"}` — must raise `DenylistedSource` naming `credentials`, and it must raise **at the denylist check, before** the undeclared-source check, so a forbidden table declared honestly is still refused.
3. **The subquery hider.** `sources={"devices"}`, with `ticket` reachable only through a `.where(exists().where(...))` — must raise exactly as probe 1 does. This is the probe that proves the traversal is deep; delete it and a walker that only inspects `stmt.get_final_froms()` ships green.
4. **The over-declaration.** `sources={"devices", "ticket"}` with a statement that reads only `devices` — this **passes** (the check is `read ⊆ declared`), and the test says so explicitly, because a reader will otherwise assume equality and "fix" it into a rule that punishes a definition for being conservative.

Then, over the real registry:

5. `assert REGISTRY, "the registry is empty; every test below passes vacuously"` — **first**, before the parametrized run. Constraint 3's empty-answer trap: `@pytest.mark.parametrize("key", list(REGISTRY))` over an empty registry collects zero cases and reports success.
6. `verify_registry()` runs clean.
7. §12.1: for every definition, `requires == frozenset(TABLE_PERMISSIONS[t] for t in sources)` — recomputed in the test from the map, not read back off the object, so a derivation that silently became a hand-written field is visible.
8. §12.3 second half: no `export_columns` entry names a `_NEVER_A_COLUMN` column.

**Wire the verifier into import.** `definitions/__init__.py` calls `verify_registry()` at module import, so a bad definition fails the application's import, not only the test suite. State in the report whether you did this at import or only in the test, and why.

Commit: `feat(reporting): verify every report's SQL against its declared sources`

---

## Task 4: The runner — envelope, bounded execution, the conjunction dependency, the generated routes, and `fleet-posture`

**Files:** Create `backend/app/modules/reporting/schema.py`, `service.py`, `deps.py`, `api.py`, `definitions/__init__.py`, `definitions/fleet_posture.py`; Modify `backend/app/modules/reporting/__init__.py`, `backend/app/modules/__init__.py`; Test `backend/tests/test_reporting_runner.py`, `backend/tests/test_reporting_fleet_posture.py`

These pieces ship together because none of them is observable without the others: a generation loop with no definition generates nothing, and a conjunction dependency with no route cannot 403. `fleet-posture` (§5.1) is the first definition because it is the simplest — three tables, no window parameter.

**Interfaces:**
- Consumes: T2's registry, T3's verifier, `require_permission`, `org_session`, `user_permission_codes`.
- Produces:
  - `ReportSource` / `ReportMeta` / `ReportPage[RowT]` exactly as spec §6.3 declares them, **every field carrying `Field(description=...)`**.
  - `async def run_bounded(session, stmt, *, timeout_ms: int) -> Result` — issues `SET LOCAL statement_timeout` then executes; maps the cancellation to `ReportTimeout`.
  - `async def execute_report(session, defn, params, *, limit, offset) -> ReportPage[Any]`
  - `def require_report_sources(key: str) -> Callable[..., Coroutine[Any, Any, AuthContext]]`
  - `REPORT_STATEMENT_TIMEOUT_MS: int`, `DEFAULT_MAX_LIMIT: int`

### 4a. The conjunction dependency (spec §0.5, §4.1)

```python
def require_report_sources(key: str) -> Callable[..., Coroutine[Any, Any, AuthContext]]:
    async def dependency(request: Request, ctx: AuthContext = Depends(auth_context)) -> AuthContext:
        ...
```

The inner function's qualname is `require_report_sources.<locals>.dependency`, which is **deliberately not** in `_PERMISSION_DEPENDENCY_QUALNAMES`. Do not add it. Do not name the free variable `code`.

It resolves `granted = await user_permission_codes(session, ctx.user_id)` — org-wide only, `device_group_id` left at its `None` default (§0.6) — and raises `403 report_permission_denied` with `missing = sorted(defn.requires - granted)` in the body. It runs **before the handler opens a data session** (§4.1): FastAPI resolves route-level `dependencies=[...]` before the endpoint body, so no query runs for an unauthorized caller.

### 4b. Bounded execution (spec §6.4, Global Constraint 6)

```python
await session.execute(text("SET LOCAL statement_timeout = :ms"), {"ms": timeout_ms})
```

`SET LOCAL`, inside the `org_session` transaction, exactly as `org_session` sets `app.current_org`. Postgres does not accept a bind parameter in `SET`; you will need `SET LOCAL statement_timeout = '<n>ms'` with `n` an int the module itself produced (never a request value) — verify the exact form against the server and record what you used. Cancellation surfaces as `sqlalchemy.exc.OperationalError` wrapping `psycopg.errors.QueryCanceled`; catch narrowly on the psycopg class via `exc.orig`, never on bare `OperationalError`, and re-raise `ReportTimeout(key)` → `504 report_timeout`. **Never a partial page** (§0.3 cost 1).

### 4c. The generation loop (spec §6.1, §0.10)

Exactly the shape §6.1 prints. Two dependencies, in that order. `response_model=ReportPage[definition.row_model]`, `summary=definition.summary`, `description=definition.description`.

**Under-determined in the spec, resolved here:** §6.2's route table shows the export path as `/{key}/export.csv`, but §6.1's loop generates one route per definition. Generate a **literal** path per definition (`/fleet-posture/export.csv`), not a `{key}` template — a templated path cannot carry a per-definition `summary`, cannot bind `require_report_sources(key)` at route-build time, and would let an unknown key reach the handler. The catalogue route `/api/v1/reports` is declared **before** the loop so no definition key can shadow it, and a test asserts no definition key is the empty string or contains `/`.

### 4d. `fleet-posture` (spec §5.1)

`sources = {"devices", "agents", "health_alert"}` → `requires = {"devices.read", "health.read"}`. Row and totals fields exactly as §5.1 lists. Params: `os_family`, `device_group_id`, `stale_after_days` (default 7), `severity_at_least`, `limit`, `offset`; params model carries `ConfigDict(extra="forbid")` so an unknown query parameter is a `422` (§6.5).

`stale` is computed from `agents.last_seen_at` against `stale_after_days`; a device with no agent row is `stale`, never "fresh by absence" (§2's no-inference rule).

`totals` are computed over the **whole match**, not the page (§6.3). Ordering is `(sort_key, devices.id)` — stable and total, so `offset` paging cannot skip or duplicate (§6.3).

Every statement carries an explicit `Device.org_id == org_id` predicate **in addition to** RLS (§6.4).

### Required tests

- **The conjunction denies, per code** (§12.4). Parametrized over each code in `requires`: a user holding `reporting.read` + every source code gets `200`; the same user missing **exactly one** source code gets `403`, and the response body's `missing` list equals `[that code]`. *This is the runtime half of the brief's falsifying mutation: a report missing a permission must 403 for a caller who holds `reporting.read` but not the source code.* **Fixture-blindness guard:** the two callers must differ only in that one grant, and the 200 case must assert a non-empty `rows` — otherwise a report that returns nothing for everyone passes both halves.
- **`reporting.read` alone is a 403** — the §4.3 claim, asserted rather than assumed.
- **A group-scoped grant is a 403** (§12.4, §0.6): seed the source codes through a `user_roles` row carrying a `device_group_id`, and the *same* codes org-wide for the control user, so the observable difference is the scoping and nothing else.
- **`route_permission` returns `reporting.read` for every report route** (§12.7), **and** the route's dependant tree contains **exactly one** `require_permission`-shaped dependency. The second assertion is the one that goes red if someone expresses the conjunction as a second `require_permission` — the first alone would keep passing, order-dependently, which is precisely the failure §0.5 exists to prevent.
- **Tenancy** (§12.5, Global Constraint 5): org A and org B seeded with **overlapping** data — same hostnames, same `os_family`, alerts of the same severity opened in the same window — and org A's report returns only org A's device ids. Then pin the application `org_id` predicate **on the admin engine** (Global Constraint 4): call `execute_report` under `admin_session`, where RLS is not doing the rejecting, and assert org B's rows are still absent. Falsify by deleting the `org_id ==` predicate and watching the admin-engine test go red while the `org_session` one stays green — **paste both outputs in your report**; that contrast is the whole point.
- **No params model declares `org_id` / `org` / `organization_id`** (§12.5), asserted over `REGISTRY`, after the non-empty assertion.
- **The statement timeout applies** (§12.12, Global Constraint 6), two directions:
  - *Applies:* call `run_bounded(session, select(func.pg_sleep(2)), timeout_ms=50)` and expect `ReportTimeout`. Falsify by deleting the `SET LOCAL` line — the sleep completes and the test fails on "expected ReportTimeout". Different observable values, not a `DID NOT RAISE` with no diagnosis.
  - *Does not leak:* after a report request completes, `SHOW statement_timeout` on a fresh checkout of the **same engine** returns the server default. Force the reuse deterministically (an engine with `pool_size=1, max_overflow=0` for this test) — otherwise the pool may hand back a different connection and the test passes against a plain `SET`. Falsify by changing `SET LOCAL` to `SET`.
- **`totals` are page-independent** (§12.10): identical `totals` across `offset=0` and `offset=1` on a seeded set of at least 3 rows. Seed rows whose values differ, so a `totals` accidentally computed over the page produces a *different number*, not the same one by coincidence.
- **Ordering is stable** (§12.11): seed a deliberate tie on the sort key, page through in size-1 pages, assert each id appears exactly once. A tie is required — without one, an unstable sort is unobservable.
- **`meta`**: `report_key`, `generated_at`, `total`, `limit`, `offset`, `sources` populated; `reproducible` is `True` for `fleet-posture` (its sources are current-state, not a mutable historical record) and the value is asserted, not defaulted.

Commit: `feat(reporting): the bounded runner, the source conjunction, and fleet-posture`

---

## Task 5: `patch-compliance`

**Files:** Create `backend/app/modules/reporting/definitions/patch_compliance.py`; Test `backend/tests/test_reporting_patch_compliance.py`

Spec §5.2. `sources = {"devices", "device_groups", "device_patch_status", "patches", "patch_approvals", "patch_policies"}` → `requires = {"devices.read", "patch.read"}`. Rows and totals exactly as §5.2 lists.

The hard part is joining **approval state** (per ring, per group) to device state — read `app/modules/patch/` before writing a line of SQL and match the module's own definition of "approved for this device", rather than inventing a second one. If the module computes it in a service you cannot reuse (reporting reads tables, not services — §1.3), reproduce the *predicate* and say so in your report; a divergence here is a wrong compliance number, which is the worst defect this module can ship.

**The load-bearing test (§12.13): an unscanned device is never counted as compliant.** `scan_state` is `fresh` | `stale` | `never`, and `never` is its own bucket. Seed three devices — one compliant, one with missing patches, one with **no `device_patch_status` row at all** — and assert `devices_with_zero_missing` counts exactly the first. Falsify by changing the join to an inner join (which drops the never-scanned device entirely) or by treating "no rows" as "nothing missing": both make the total read 2 instead of 1. That is an observable difference; "assert the query ran" is not.

Plus, per Task 4's pattern: the two conjunction cases (`devices.read` missing → 403 naming it; `patch.read` missing → 403 naming it), the two-org overlapping-data tenancy test with the admin-engine predicate pin, and `verify_definition` clean (already covered by T3's registry sweep, but confirm it turns red if you forget a `sources` entry — add the join, run T3's test, watch it fail, revert; paste the output).

Commit: `feat(reporting): patch-compliance, with unscanned devices in their own bucket`

---

## Task 6: `backup-coverage`

**Files:** Create `backend/app/modules/reporting/definitions/backup_coverage.py`; Test `backend/tests/test_reporting_backup_coverage.py`

Spec §5.3. `sources = {"devices", "device_groups", "backup_policies", "backup_repositories", "backup_points", "backup_runs"}` → `requires = {"devices.read", "backup.read"}`. Rows and per-`coverage` totals exactly as §5.3 lists.

**Excluded on purpose** (§5.3, §0.8): `backup_repositories.encryption_key_ref` and `storage_ref` are never in `export_columns` and never in the row model. Add them to `_NEVER_A_COLUMN` in `registry.py` so the §12.3 test enforces it rather than a reviewer.

**The load-bearing test (§12.13): a device with no backup policy is never `covered`.** Seed four devices, one per `coverage` value, including one with **no policy row**, and assert the totals map has `unprotected: 1` and that this device is not in any other bucket. Falsify by making the policy join inner: the device vanishes and `unprotected` reads 0 while `covered` is unchanged — an observably different number.

Also required: `last_restore_tested_at` is surfaced (it is the evidence for the roadmap's "proven restores" gate, §5.3) and a device whose policy exists but has never had a verified restore reads as such rather than as `covered`.

Plus the two conjunction cases, the two-org overlapping tenancy test with the admin-engine pin.

Commit: `feat(reporting): backup-coverage, including the never-restored evidence`

---

## Task 7: `sla-attainment`

**Files:** Create `backend/app/modules/reporting/definitions/sla_attainment.py`; Test `backend/tests/test_reporting_sla_attainment.py`

Spec §5.4. `sources = {"ticket", "ticket_queue", "ticket_sla"}` → `requires = {"ticket.read", "sla.read"}`. **Note this is the one v1 report that does not require `devices.read`** — it is what makes the four `requires` sets genuinely distinct (§5), so a test that only ever exercises `devices.read` would miss a whole class of bug.

Grouped by `(queue, priority)` over a **closed window**: `from` and `to` are required, and the window is capped at 366 days by the params model (a `422`, not a silent clamp).

`breached_after_retriage` counts rows whose `ticket_sla.policy_source` is a human re-triage — SLA spec §0.3's mitigation, and §5.4 says nothing currently reads `policy_source`. **Read `app/modules/sla/models.py` for the real literal** (`"retriage"` at the time of writing) rather than trusting this sentence.

**No ticket bodies, no subjects, no requester identity** (§5.4, §0.8). A structural test: assert the row model's field names ∩ `{"subject", "description", "body", "requester_id", "requester_email"}` is empty, and that `ticket_comment` is not in `sources` (and, via T3, not in the compiled SQL).

**`meta.reproducible` is `False`** (§0.3 cost 2, §5.4). Assert the literal value on a real response. This is the one report where it differs from the others, so a `reproducible` hardcoded `True` or `False` anywhere would be caught only by having both cases in the suite — which is why this task must assert it and Task 4 must assert the opposite.

Plus the two conjunction cases, and the two-org tenancy test seeded with **queues of the same name and tickets in the same window in both orgs** (Global Constraint 5 — a queue named differently per org makes the org predicate unobservable), with the admin-engine pin.

Commit: `feat(reporting): sla-attainment, and the re-triage column nothing else reads`

---

## Task 8: The catalogue route

**Files:** Modify `backend/app/modules/reporting/api.py`, `schema.py`; Test `backend/tests/test_reporting_catalogue.py`

`GET /api/v1/reports`, gated on `reporting.read` **alone** (§6.2). Returns one entry per definition: `key`, `summary`, `description`, `requires` (sorted), `entitled: bool`, `missing_permissions: list[str]` — **for the calling user**. It exposes no org data; the catalogue is platform code, and it turns an opaque 403 into a permissions conversation (§4.1).

Tests:
- A user with `reporting.read` and nothing else sees **every** report, all with `entitled: false` and a non-empty `missing_permissions`. Nothing is hidden (§11).
- A user with `reporting.read` + `{devices.read, health.read}` sees `fleet-posture` entitled and the other three not. **The two users must differ only in those grants** — a fixture that also changes the org or the seeded data makes the entitlement computation unobservable.
- `missing_permissions` for each entry equals `sorted(defn.requires - granted)`, recomputed in the test from `REGISTRY`, not read back off the response.
- The catalogue lists exactly `set(REGISTRY)` — and the test asserts `len(REGISTRY) == 4` explicitly, so a definition that failed to import is a red test rather than a shorter list nobody notices.
- `route_permission` for the catalogue route is `reporting.read`, and it carries **no** `require_report_sources` dependency.

Commit: `feat(reporting): the catalogue route, so a 403 is explicable`

---

## Task 9: Export — CSV, the cap, the escaping, and the audit event

**Files:** Create `backend/app/modules/reporting/export.py`; Modify `backend/app/modules/reporting/api.py`, `registry.py` (`_NEVER_A_TOOL`); Test `backend/tests/test_reporting_export.py`

Spec §7, §0.7, §0.8. One export route per definition, generated by the same loop, carrying the **same two dependencies** — an export never has a weaker gate than the report it exports.

- `PlainTextResponse(media_type="text/csv")`, matching the six existing exporters.
- Header row **equals** `definition.export_columns`, in order. §12.8's first assertion: a column cannot appear in a file without appearing in the definition first.
- `None` renders as `""`, never `"None"`.
- **Free-text normalisation, then `escape_formula`.** Re-read `app/core/csv_export.py` first (Global Constraint 8) and confirm `_DANGEROUS_PREFIXES` is still `("=", "+", "-", "@")`. Reporting strips leading `\t`, `\r`, `\n` and removes any NUL from its own `export_free_text` columns *before* calling the shared helper. **Do not extend `escape_formula`** — six modules call it and budget's Odoo export is a pinned ERP wire contract. §15 files that as a separate reviewed change.
- Applied **per column**, only to `definition.export_free_text` — never blanket over a row, which would corrupt signed amounts and dates for machine readers (`csv_export.py`'s own docstring says why at length).
- **Over `export_row_cap` → `413 report_export_too_large`** with `cap` and `matched`, and **no CSV body at all**.
- One `report.exported` audit event per export, in the same transaction as the read: `action="report.exported"`, `target_type="report"`, `target_id=key`, `metadata={"params": ..., "row_count": ..., "format": "csv"}`. **The metadata never contains rows.**
- Add each export route to `_NEVER_A_TOOL` (§10.3).

Required tests (§12.8, §12.9):
- Header equals `export_columns` exactly, per definition, parametrized over `REGISTRY` (with the non-empty assertion first).
- A free-text value beginning `=`, `+`, `-`, `@`, a tab and a CR each round-trip neutralised: the `= + - @` cases come back prefixed `'`, and the tab/CR cases come back with no leading whitespace. **Six parametrized cases, not one** — a single `=` case passes against an implementation that handles only the shared helper's four and drops reporting's own normalisation, which is exactly the gap §0.8 exists to close.
- A **numeric** column with a negative value is **not** escaped — assert the cell is `-5`, not `'-5`. Without this, "escape everything" passes every other assertion in this task and corrupts machine readers.
- **The cap returns 413 and observes the status** (Global Constraint 7): seed `cap + 1` matching rows, assert `response.status_code == 413`, assert `response.headers["content-type"]` is **not** `text/csv`, and assert the body is the typed error with `cap` and `matched`. **Do not assert on a row count** — that assertion is satisfied by a truncating implementation.
- A response at exactly `cap` succeeds — the boundary, so the cap is `>` and not `>=` by accident.
- **`report.exported` is written on export, and nothing is written on a run** (§12.9), both directions: count `audit_events` for the org before and after a `GET /api/v1/reports/fleet-posture` (delta 0) and before and after its export (delta exactly 1). §0.7 is a decision that is easy to reverse by accident, and the "nothing on a run" half is the one that silently regresses.
- The audit metadata contains no row data: assert the recorded `meta` keys are exactly `{"params", "row_count", "format"}`.
- A caller missing a source code gets `403` from the **export** route too, not just the report route.

**Under-determined in the spec, resolved here:** the spec names no numbers for `max_limit`, `export_row_cap` or the statement timeout. Proposed: `max_limit = 1000` (the `le=` on every report's `limit`), `export_row_cap = 50_000`, `REPORT_STATEMENT_TIMEOUT_MS = 15_000`. Record whatever you ship in the spec amendment (Task 12) with the reasoning, so the next reader does not think a number was measured when it was chosen.

Commit: `feat(reporting): capped CSV export that refuses rather than truncates`

---

## Task 10: The five properties, enforced over the whole reporting surface

**Files:** Test `backend/tests/test_reporting_properties.py`

Spec §0.10, §12.6, §12.7. This task runs after every route exists, because it is a sweep over the built app, and it is the task that makes §0.10's claim — *"a report that reaches the router has the five properties because there is no code path by which it could not"* — checkable.

**Copy `_flatten` from `backend/tests/test_ai_admin_registry.py`.** FastAPI 0.139.2's `include_router` appends a lazy `_IncludedRouter` whose own `.path` is `None`; a naive `app.routes` filter finds nothing and **the whole test file passes vacuously** (§12.6 says this explicitly, and it is constraint 3's empty-answer trap at file scope). Guard it: assert the flattened reporting routes number exactly `2 * len(REGISTRY) + 1` (a report route and an export route per definition, plus the catalogue) before asserting anything about them. A `>=` assertion hides a missing route.

For each report route, assert all five, **stricter than the ai-admin version on two counts** (§12.6):
1. Method-honest: `GET`, and — for the report routes, not the exports — no write.
2. Gated: `route_permission(route) == "reporting.read"` (not merely "not None").
3. Typed: `response_model` is `ReportPage[SomeRow]`, never `dict[str, Any]`, `Response`, `StreamingResponse` or `PlainTextResponse`.
4. Bounded: a `limit` query param **with an `le=`**, regardless of response shape — the ai-admin boundedness test only asks this of `list[Model]` responses and reporting's envelope is an object, so the existing test would not ask (§10.2). Assert the `le` equals the definition's `max_limit`.
5. Self-describing: a real `summary=` on the route (**not** the docstring fallback), and `Field(description=...)` on **every** field of the row model **and** of `ReportMeta`, `ReportSource` and `ReportPage` (**not** a class-docstring fallback).

Plus:
- Export routes are on `_NEVER_A_TOOL` and are **excluded** from properties 1 and 3 by design (§10.3) — assert they are on the list rather than asserting they pass.
- `_NEVER_A_TOOL` entries resolve to **real routes** on the built app, following `test_denylist_entries_resolve_to_real_routes`'s liveness discipline (§10.3). Assert the resolved set is non-empty first.

**On the remote-module finding (§0.5, §16.3): do not close it here, and do not write this test as a global sweep.** `app/modules/remote/api.py` forks a local `require_any_permission`, so `GET /api/v1/remote/sessions` and `GET /api/v1/remote/sessions/{session_id}` return `None` from `route_permission` and read as ungated to every router-derived RBAC check, though they are genuinely gated. Reasons this plan files it rather than fixing it, stated so the decision is reviewable:
- **The kernel's reflection cannot express an OR-gate.** `route_permission` returns **one** code and reads it from a free variable literally named `code`; `require_any_permission` closes over `codes`, a tuple. Adding its qualname to `_PERMISSION_DEPENDENCY_QUALNAMES` would make the reflection return `None` anyway, or force `route_permission` to return a set — which is the exact kernel change §0.5 declines to make, to the one function every module's completeness test and the AI registry's permission-reflection test depend on. (The spec calls that test `test_every_recorded_permission_is_correct`; its real name in the code is **`test_every_recorded_permission_matches_the_router`**, `tests/test_ai_admin_registry.py:107` — constraint 10, the code wins.)
- **The alternative fix changes remote's authorization semantics.** Splitting the two routes so each carries a single `require_permission` means either an auditor holding only `remote.recordings.read` loses session listing, or an operator holding only `remote.control` does. That is a product decision for the remote module's owner, not a side effect of a reporting cycle.
- **Fixing it here would put this plan on the critical path of an `app/core/rbac.py` change** (Global Constraint 2 — full suite, three prior breakages) for a defect reporting does not cause and does not inherit.

What this plan does instead: (a) reporting never forks the gate and never hides one from the router — §0.5's rule, enforced by Task 4's "exactly one `require_permission`-shaped dependency" test; (b) **this test file scopes its completeness sweep to reporting's own routes.** Do not write it as "every route in the app is gated" — that assertion goes red today on remote's two routes and `GET /api/v1/me`, and whoever hits it will add an allowlist, which converts a visible finding into a permanently suppressed one. (c) The finding is restated in the spec amendment (Task 12) as a filing against the remote module's own cycle.

Commit: `test(reporting): the five properties, enforced over the generated surface`

---

## Task 11: Frontend

**Files:** Create `frontend/src/modules/reporting/{api.ts,types.ts,ReportCatalogue.tsx,ReportView.tsx}` and their tests

Spec §11, matching `frontend/src/modules/notification/`'s conventions. TanStack Query, strict TypeScript, no `any`.

Three behaviours must be tested on **rendered output**, not on props:

1. **A report the user cannot run is visible and explained.** The catalogue card is greyed and **names the missing permission codes**. Nothing is hidden — a report you cannot run is a permissions conversation, not a secret (§11). Falsify by rendering with `entitled: false` and asserting the codes appear; a test that only asserts the card exists passes against a card that hides them.
2. **"As of `generated_at`" is always rendered, and a `reproducible: false` report additionally renders the staleness note.** Mandatory, not optional (§11, §0.3 cost 2). Two rendered cases, `reproducible` true and false, asserting a **different** visible string in each — one case cannot distinguish "always shows the note" from "shows it correctly".
3. **No client-side aggregation.** `totals` and every percentage come from the response. A structural test over `frontend/src/modules/reporting/` asserting no `reduce(`/`+=` accumulation over `rows`; if the browser computes a percentage there are two implementations and the browser's is the one someone screenshots (`sla/views.py` gives the same reasoning server-side).

CSV download links point at the export route and carry the auth header the rest of the app uses.

**Do not write a test that asserts `Object.keys()` over a fixture defined in the same file.** That test cannot fail; this project has shipped one.

Commit: `feat(reporting): catalogue and report pages`

---

## Task 12: Verification, spec amendments, and the live smoke test

**Files:** Modify `docs/modules/reporting/spec.md`

**Spec amendments.** Record every decision the build forced, with its reason. At minimum:
- The roles `reporting.read` was seeded to, and why `technician` was excluded (Task 1).
- The three numbers the spec left unset: `max_limit`, `export_row_cap`, `REPORT_STATEMENT_TIMEOUT_MS`, with the reasoning and a note that they were chosen, not measured (Task 9).
- Literal export paths rather than a `{key}` template, and why (Task 4c).
- The exact `SET LOCAL statement_timeout` syntax that worked, and the exception class the cancellation actually surfaced as (Task 4b).
- **§0.3's open build task:** confirm against the deployed PostgreSQL 16 whether RLS can be attached to a materialised view. §16.8 flags this as asserted from semantics, not measured. Record the answer either way; if a future PostgreSQL supports it, §0.3's rejection is re-openable and nothing else in the spec changes.
- Whether `verify_registry()` runs at import or only in the suite (Task 3).
- Any divergence between the spec's column names and the real models (constraint 10).
- The remote-module finding, restated as a filing against that module's cycle (Task 10).

**The live smoke test** — a script under the scratchpad directory driving the real app against a real Postgres, not mocks. **One foreground run redirected to a file** (Global Constraint 9 — do not poll it with `pgrep -f`, which matches the polling shell itself and deadlocks).

1. Create two orgs with **overlapping** data: same hostnames, same OS families, alerts of the same severity, a queue of the same name, tickets in the same window. Create a user in org A holding `reporting.read` + all four modules' read codes, and a second holding `reporting.read` only.
2. `GET /api/v1/reports` as each → the first sees four entitled entries, the second sees four entries all with `missing_permissions`.
3. Run all four reports as the entitled user → assert no org B row appears in any of them, by id.
4. Revoke one source code and re-run that report → `403`, with the revoked code in `missing`.
5. Export one report → a CSV whose header equals `export_columns` and exactly one new `report.exported` audit row. Run the report (not the export) again → **no** new audit row.
6. Drive an export past `export_row_cap` → `413`, no CSV body.
7. Verify the audit chain still verifies afterwards (`GET /api/v1/audit/verify`) — reporting appends through `audit_service.record` like every other module (§0.9) and must not have forked the chain.

Steps 3 and 5 matter most: step 3 is the only end-to-end check that the org predicate and RLS agree on real seeded data, and step 5's negative half is the only check that a dashboard refresh does not land on the audit chain's advisory lock (§0.7). **Report plainly what the smoke test did and did not cover** — if a step could not be driven end to end, say so rather than substituting a mock.

**Verify:** full backend suite, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`, frontend `npm test` + `npx tsc --noEmit`. Record the counts against the baseline from Global Constraint 13.

Commit (Global Constraint 16: `git reset` first, stage explicit paths only). **Do not push. Do not open a pull request.**

---

## Self-review

**Spec coverage.** §0.1 code-defined catalogue → T2. §0.2 no time series → nothing built, and T2's `TABLE_PERMISSIONS` has no metric table to map. §0.3 live, no snapshot → T4 (`run_bounded`, the 504); the matview question → T12. §0.4 the conjunction → T3 (derivation verified) + T4 (enforced). §0.5 one code to the router → T4's `route_permission` + single-`require_permission` test; the remote finding → T10. §0.6 org-wide grants only → T4's group-scoped 403. §0.7 export audited, runs not → T9, both directions. §0.8 CSV, capped, escaped → T9. §0.9 no audit source → T2's denylist + T3's verifier. §0.10 five properties by construction → T4's loop, T10's sweep. §0.11 nothing scheduled → nothing built; §9 worker → nothing built. §0.12 four cross-module reports → T4–T7. §1 boundary → T2's denylist. §2 recency and no inference → T5/T6's `never`/`unprotected` buckets, T4's `meta.sources`. §3 registry → T2. §4 authorization → T3, T4. §5.1–5.4 → T4, T5, T6, T7; §5.5 not built. §6 API → T4, T8; §6.3 envelope → T4; §6.4 execution → T4; §6.5 errors → T1's error classes, mapped in T4/T9. §7 export → T9. §8 tenancy/RBAC/audit → T4, T9. §10 the AI seam → T9's `_NEVER_A_TOOL` and T10's liveness test; the `ReadTool` entries themselves are **not** in this plan (see below). §11 frontend → T11. §12's thirteen tests: 1→T3, 2→T3, 3→T2/T3, 4→T4, 5→T4, 6→T10, 7→T4, 8→T9, 9→T9, 10→T4, 11→T4, 12→T4, 13→T5/T6. §13 migration → T1. §15 deferrals → nothing built. §16 → T12's amendments.

**Deliberately out of this plan.** §10.1's `ReadTool` entries in `app/modules/ai_admin/tools/registry.py`. Reporting does not import `ai_admin` and nothing here auto-registers anything (§10.1); adding four entries to another module's allowlist is that module's reviewed change, and `test_every_recorded_permission_matches_the_router` will assert `ReadTool.permission == route_permission(route) == "reporting.read"` when it happens. What this plan owes that cycle is delivered: routes that pass the five properties, a stable row model, and a `_NEVER_A_TOOL` list that resolves to real routes. §10.2's caveat — the recorded permission is the route gate, an understatement of the conjunction, and the agent is therefore automatically narrower for a narrower admin — is already written down in the spec and needs no code.

**Sequencing rationale.** T3 before T4–T7 is the only ordering constraint that is a security property rather than a convenience: it is the difference between `sources` being a verified declaration and being a comment. T4 bundles the runner with the first report because a generation loop, a conjunction dependency and a timeout are each unobservable without a definition to exercise them, and the alternative — ship them with a test-only definition, then delete it — would leave the first real report as the first unverified one.

**Known risk this plan does not remove.** Tasks 5–7 each write a multi-table join reproducing a predicate a source module owns (patch approval state, backup coverage classification, SLA attainment). Reporting reads tables and not services by design (§1.3), so those predicates are second implementations, and a second implementation drifts. T3's verifier catches a report reading an *undeclared* table; it cannot catch a report reading the *right* tables and computing the wrong answer. That is what §12.13's two honesty tests and T12's smoke test exist for, and it is the reason §0.12 forbids wrapping the single-module reporters: the four v1 reports were chosen so that no module already computes them, which bounds the drift surface to the joins themselves.
