# AI Automation Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One AI orchestration service in the kernel, shared by three personas — the platform-ops admin (act mode, at last), the technician copilot, and the end-user tier-1 resolver — plus a preview→confirm→execute path whose every act is an existing job type, from an `approved_action` row an org admin already authored, dispatched through the unmodified `JobEngine` after a human with the owning module's own dispatch permission has confirmed a server-rendered preview.

**Architecture:** The machinery that today lives in `app/modules/ai_admin/` (turn loop, providers, envelope, scrubber, injection heuristic, staff loopback executor, transcript store, retention, settings) **moves to `app/core/ai/`** and stops knowing any persona's name. A persona becomes three things: a frozen `PersonaProfile` compiled into the image, a router on its own surface, and a frontend folder. Act mode adds one tool — `actions.propose(action_key, device_id, rationale)` — whose entire effect comes from the catalogue row it names. A proposal is persisted as an `ai_action` row with a server-computed `preview_digest`; confirmation carries an id and a digest and **contributes no job type, no params and no target**. A new kernel table, `ai_autonomy_ceiling`, can only ever lower what the catalogue already permits, and its vocabulary stops one value short of `autonomous`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Pydantic v2, Alembic, Celery, Postgres RLS, React + TypeScript. **No new dependency**, backend or frontend.

**Zero endpoint code.** No agent capability, no job type, no signed-job path, no wire-protocol change, no change to the agent's idle budget. `agent/` is not touched. If a task appears to need agent work, that task is wrong.

**Authority:** `docs/modules/ai-automation.md` (commit `653ec3c`). This plan implements it; it does not relitigate it.

---

## Global Constraints

Every task's requirements implicitly include this section. These are earned lessons from previous sub-projects of this repo, not general advice.

1. **Authority is the spec (`docs/modules/ai-automation.md`, `653ec3c`).** Where this plan and the spec disagree, the spec wins. Where the **code** and either of them disagree, constraint 2 applies. §0.1's relocation, §0.4's "the dial can only lower", §0.7's "the model names a key, never an effect", §0.9's inversion (the resolver never proposes) and §0.11's two-part confirm are settled — a task that finds one inconvenient **reports that**, it does not route around it.

2. **Trust reality over the spec and over this plan.** If a signature, a table name, a permission code, a route path or a behaviour differs from what is written here, **the code wins, and you say so in your report rather than implementing the brief.** This plan was written against the code at `6d57334` on branch `claude/platform-superpower-skills-7fxkgr`; other agents are working concurrently and the code may have moved. Every fact in "Verified facts" below was read out of the code on that commit — re-verify before calling it.

3. **Touching `app/core/events.py`, `app/core/rbac.py` or `app/models/__init__.py` requires the FULL backend suite, not this layer's own tests.** Not "should" — a permission-set literal in `backend/tests/test_api_routes.py::test_me_returns_identity_and_permissions` has broken this way **three times**, and it is invisible to every AI test file. `backend/tests/test_rbac.py::test_permission_registry_complete` pins the exact permission-code set as a literal too. **This layer registers NO event type** — the `ai.*` names are audit actions through `audit_service.record`, which is a free-form string field with no registry obligation. A task that finds itself editing `app/core/events.py` has misread §8 — stop and re-read it. Tasks 1, 3, 4 and 6 touch `rbac.py` and/or `models/__init__.py` and run the full suite.

4. **Fixture blindness is this project's defining defect.** A guard test must make the guarded and the unguarded path produce **different observable values**. If the fixture makes both paths agree, the assertion is decoration and the test passes against the bug. Every task below that adds a guard states what the test would observe if the guard were deleted — implement that observation, not a weaker one. Three traps have actually shipped here and every one recurs in this layer:
   - **A mutation whose only evidence is `DID NOT RAISE`.** `pytest.raises(...)` around an insert reports nothing about what the database now holds. **Assert the stored row first** — SELECT it back and assert on its column values — then the raise.
   - **A constant parameter across all fixtures.** A `@pytest.mark.parametrize` whose every case expects the same answer proves nothing about the function that produced it. This bites hardest on the dial (Task 5): if every `(ceiling, autonomy)` pair expects `require_approval`, deleting the `min()` passes.
   - **An empty answer satisfied by the function's own default.** "`off` mints no `ai_action` row" is satisfied by a query that was always going to return `[]`. **Seed a nearly-matching row** — same org, same session, different key — so `[]` is evidence that the filter ran rather than evidence that nothing exists.

5. **RLS masks application predicates.** A cross-org test driven through `org_session` passes even when the service's own `org_id` predicate has been deleted, because RLS rejects first. To pin an application-layer predicate you must exercise it on the **admin engine**, where RLS is not the thing doing the rejecting. This defect shipped four times in the SLA sub-project. Every read this layer adds carries an explicit `org_id` predicate **in addition to** RLS (§9.1), and at least one test per service pins it on the admin engine.

6. **For an `ALL`-command RLS policy Postgres reuses `USING` as `WITH CHECK` when the latter is omitted**, so deleting `WITH CHECK` falsifies nothing. Write it explicitly anyway; the mutations that actually falsify a tenancy test are **`WITH CHECK (true)`** for an insert test and **`USING (true)`** for a read test. And on a table carrying a **generated column**, `WITH CHECK (true)` alone is insufficient — the ORM's `RETURNING` is caught by the SELECT policy, so the insert still fails and the mutation looks like it was refused by the thing you deleted. **Run both mutations on every RLS test.**

7. **NULLs do not collide in a Postgres unique constraint**, and **a partial index is not a named constraint**: `ON CONFLICT ON CONSTRAINT <partial index>` fails at runtime with `InvalidObjectDefinition`. Any upsert against a partial index uses `index_elements=[...]` **plus** `index_where=...` on `sqlalchemy.dialects.postgresql.insert`, and there must be a test that executes the statement against the real database. In this layer the exposure is `ai_autonomy_ceiling` (upserted per `(org_id, persona, risk_class)` — a real PK, so `ON CONFLICT` on the columns is correct) and any "dedupe" a builder is tempted to add to `ai_action` (§0.5 rejects one deliberately — see Task 6).

8. **The index-drift test is live and bidirectional.** `tests/test_schema_drift.py::test_metadata_indexes_match_live_schema` compares `Base.metadata`'s indexes to the migration-built schema **including partial-index predicates**, and fails on an unmodelled index as well as a missing one. Every index in a migration must be declared in the model's `__table_args__`, predicate included. (Note the ai-admin plan's constraint 9 said the drift test does *not* compare indexes; that is stale — it does now.)

9. **`app/core/audit.py::canonical_bytes` raises `TypeError` on a UUID.** It is `app/jobs/canonical.py::canonical_bytes` re-exported, and its `default=` handler rejects anything `json` cannot serialize by design ("passing a datetime raises so a formatting ambiguity can never silently change signed bytes"). The IPAM builder hit this. Every UUID going into `preview_digest` or into audit metadata is `str(...)`; every datetime is `rfc3339(...)`. This is not a style note — it is the failure mode of Task 7's digest.

10. **A guard must be matched against definitions, not mentions.** A test forbidding a *string* anywhere in a package goes red on the docstrings that describe the boundary, and pushes the next reader to delete the description instead of respecting it. That mistake was made in this repo and corrected: `tests/test_ai_admin_boundaries.py::test_act_mode_is_not_grown_inside_this_module` now matches `^\s*class ActTool\b`, `\bmutating\s*=\s*True\b` and friends against source **with a comment saying why the prose is allowed to name them**. Every source-scanning guard this plan adds uses AST nodes or anchored regexes over definitions. Tasks 11–13 change these guards; read the current file before touching it.

11. **`pgrep -f "<pattern>"` matches the polling shell's own command line** and deadlocks the wait loop. Do not background a long test run and poll for it. Run suites in **one foreground call redirected to a file** (`cd backend && .venv/bin/pytest -q > /tmp/run.txt 2>&1; tail -40 /tmp/run.txt`).

12. **Resync the index before staging.** `git reset` (mixed, no pathspec), then `git add <explicit paths> && git commit`. A stale index caused a 48-file revert on this branch. Other agents work concurrently: stage only the files your task names.

13. **Migrations are forward-only; never edit a shipped one. This plan names no migration number.** Head at writing was `0046` (`0046_approved_action_fuse_ceilings.py`) — **treat that as stale**. Before each migration run `cd backend && .venv/bin/alembic heads` **and** `ls migrations/versions/`, take the next free number, and set `down_revision` to the bare short string of the actual head (e.g. `"0047"`), matching every other migration in this repo. This plan refers to them as **Migration A** (Task 1: `ai_autonomy_ceiling`, the two CHECK widenings, two permission codes) and **Migration B** (Task 6: `ai_action`, two `ai_settings` tunables); chain B off A.

14. **Tooling.** `pytest` is at `backend/.venv/bin/pytest`; bare `pytest` and `python -m pytest` are not installed. Use `cd backend && .venv/bin/pytest tests/... -v`, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests`, `.venv/bin/alembic ...`. Frontend: `npx tsc --noEmit` is the only static check (`npm run lint` does not exist); `npm test` for the suite.

15. **Measure the baseline before you start.** Run the full backend suite once and record the pass count. Do not trust a number in this document or a sibling plan — concurrent agents are adding tests. Do not regress it.

16. **Postgres and Redis go down in this container during long runs.** Check `pg_isready` and `redis-cli ping` first, and again if anything hangs; restart them yourself (`service postgresql start`, `redis-server --daemonize yes`) and carry on.

17. **Naming and shape facts, verified against the code — do not guess.**
    - AI tables are **singular**: `ai_session`, `ai_message`, `ai_injection_flag`, `ai_settings`, and this layer adds `ai_action`, `ai_autonomy_ceiling`. Kernel tables are plural (`users`, `organizations`, `devices`, `jobs`, `audit_events`, `permissions`, `roles`, `role_permissions`); `approved_action` and `approved_action_script_exemption` are singular; service-desk tables are singular (`ticket`, `ticket_comment`).
    - `permissions` is `(code, module, action)` — there is **no** `resource` and **no** `description` column.
    - `AuditEvent`'s metadata attribute is **`meta`** (mapped to column `metadata`).
    - Tenancy GUCs are **`app.current_org`** and, for the portal, **`app.current_requester`**. `portal_session(org_id, requester_id, *, engine)` pins both and is fail-closed if either is unset.
    - Modules register in **`app/modules/__init__.py`**, not `app/main.py`.
    - FastAPI 0.139.2: `app.routes` **is** `app.router.routes` and **neither contains `include_router`'d routes** — only a `_IncludedRouter` with `path=None`. Any route-walking test (Tasks 2, 11, 13, 14) must flatten via `_IncludedRouter.original_router` + `include_context.prefix`. `backend/tests/test_email_intake_api.py` has a working flattener — copy it. A flattener that silently misses routes makes Task 13's "the portal AI surface registers no action route" **vacuous**, which is the one test standing between §0.9 and a resolver that can dispatch.
    - Tests use `api_env` with `asyncio_mode=auto` — **do not** write `@pytest.mark.asyncio`. `tests/conftest.py` provides `admin_engine`, `app_engine`, `api_env` (`.create_org`, `.create_user`, `.headers`, `.client`, `.app`, `.admin_engine`, `.app_engine`), `a_ticket_for`, `add_comment_as_staff`. `tests/ai_admin_fakes.py` provides `RecordingTransport`, `ScriptedProvider`, `ai_user`, `install_spy`.
    - Async throughout, full type hints, business logic in services not route handlers, `mypy` and `ruff` clean.

18. **`app/api/deps.py::require_permission`'s SHAPE is load-bearing and must not be refactored.** `app/core/rbac.py::route_permission` recovers the permission code by reflection over the closure's `__qualname__` and `co_freevars`, and that reflection is what makes RBAC completeness derivable from the router. This layer reads it in three places (Tasks 2, 11, 13). Do not "helpfully" add a registry attribute to it.

---

## Verified facts you will build against

Read out of the code at `6d57334`. Re-verify before use (constraint 2).

```python
# app/core/approved_actions.py  -- KERNEL. APPROVABLE_JOB_TYPES lives HERE, not in health.
APPROVABLE_JOB_TYPES: frozenset[str]          # hello, inventory.collect, logs.ship, patch.scan,
                                              # health.check.now, script.run, patch.apply, app.deploy
NON_APPROVABLE_REASONS: dict[str, str]
non_approvable_reason(job_type) -> str | None                     # deny by default
is_destructive(job_type, params_template, script_exemption, *,
               script_id=None, script_version=None) -> bool       # PURE; never queries
render_params_template(job_type, template, *, device_id: UUID, selector: str) -> dict
redact_secret_values(template) -> tuple[dict, list[str]]
get_approved_action(session, *, org_id: UUID, key: str) -> ApprovedAction | None
get_script_exemption(session, *, org_id, script_id, script_version) -> ...ScriptExemption | None
AUTONOMY_LEVELS = ("suggest", "require_approval", "autonomous")   # app/models/approved_actions.py
RISK_CLASSES = ("low", "medium", "high")
MAX_TARGET_SCOPES = ("device",)

# app/jobs/engine.py
JobEngine.create(session, *, org_id, type: str, target_device_id: UUID,
                 params: dict, requested_by: UUID | None = None, ttl_s: int | None = None) -> Job
# app/jobs/registry.py
registered_names() -> list[str]
# app/jobs/canonical.py  (re-exported by app/core/audit.py)
canonical_bytes(obj: Mapping) -> bytes        # raises TypeError on UUID / datetime
rfc3339(dt: datetime) -> str
# app/core/audit.py
audit_service.record(session, *, org_id, actor_type: Literal["user","agent","system"],
                     actor_id, action: str, target_type="", target_id="", metadata=None)
# app/core/tenancy.py
org_session(org_id, *, engine) -> AsyncContextManager[AsyncSession]
portal_session(org_id, requester_id, *, engine) -> AsyncContextManager[AsyncSession]
# app/modules/portal/principal.py
resolve_portal_principal(request) -> PortalContext        # PortalContext is NOT an AuthContext
require_portal_permission(code) -> ...                    # recoverable by route_permission
# app/modules/health/actions.py
_fuse_breach(session, org_id, action, *, device_id, now) -> str | None   # Task 3 lifts this
```

- **`ai_session` CHECKs today are `persona IN ('admin')` and `mode IN ('read')`**, declared in `app/modules/ai_admin/models.py::AiSession.__table_args__` and pinned by `tests/test_ai_admin_migration.py::test_ai_session_persona_and_mode_checks` (a parametrized table of `(column, value, allowed)`).
- **`app/models/__init__.py` currently imports `AiSession`/`AiMessage`/`AiInjectionFlag`/`AiSettings` from `app.modules.ai_admin.models`** — a kernel→module import that Task 4 reverses.
- **`ai_settings` already carries four CHECK-bounded tunables**; Task 6 adds two more in the same style.
- **`approved_action.personas` is JSONB defaulting `["health"]`**, and `ck_approved_action_autonomous_is_bounded` is a live database CHECK. Neither is modified by this plan.
- **`MODULE_GOVERNED_REASONS`' messages end with the owning permission in parentheses** for 10 of their 11 entries (`... (deploy.script.run)`, `... (patch.apply)`, …). `health.policy.apply`'s does not, because it has no route by design. Task 2 exploits this.
- **`tests/test_direct_dispatch.py`** already asserts a partition over `registered_names()`; copy its app-building fixture — `registered_names()` is only complete after modules have registered.
- **`app/worker.py` has an `_OrgCycleFailures` shell** used by every `*_all_orgs` beat task after the correction in `3d3ac07` (one org's failure must not abort the rest). Task 9's sweep uses it.

---

## Two things the spec verified do not exist, and what this plan does about them

Both are **explicit scope cuts**, recorded here so no builder discovers them mid-task and invents a workaround.

**1. `devices` has no owner, primary-user or assigned-to column (§11.1).** Verified again for this plan against `app/models/core.py::Device`: hostname, fqdn, os_*, serial, asset_tag, cpu, ram, disks, nics, `device_group_id`, status, timestamps. `device_tags` is free key/value with no semantics. The only device↔person link is `ticket.device_id`, nullable and asserted by whoever created the ticket — including, for an email-created ticket, by a parser reading text an unauthenticated sender wrote.

**What this plan does:** nothing, and that costs nothing, because **the resolver as specified does not need it.** §0.9 gives `end_user` no act at any dial position, and §3.1 scopes its reads to the requester's own tickets. Device ownership blocks *end-user-initiated acts*, which §14 already defers. **Task 13 makes the absence a tested property**: the `end_user` profile's tool set contains no device tool and `may_propose is False`, asserted off the registered profile, so a builder who "just adds" a device lookup fails a test instead of resolving a device from a ticket field an unauthenticated email can set. **A builder who believes they need device ownership has hit §11.1 and must stop and report, not derive ownership from `ticket.device_id`.**

**2. `doc_articles` has no visibility/audience/published flag, and every documentation route is gated on `docs.read` or `docs.manage` (§11.2).** Verified: `app/models/documentation.py::DocArticle` is `(title, body, category, archived, current_version, …)`, and all 15 gated routes in `app/modules/documentation/api.py` use those two staff codes. The corpus is an internal runbook store — precisely what must not be readable by a requester.

**What this plan does:** cuts the knowledge-base answering path from v1 and says so. The resolver ships able to answer about the requester's own tickets and to route; it cannot say "here is how to reconnect the VPN". Task 13's definition of done includes a guard that the `end_user` tool set contains **no documentation tool**, and Task 16 records the precondition — an audience dimension on `doc_articles` plus a portal-facing read route — as **documentation-module work**, not this layer's. **This layer must not invent a second article store to route around it** (§11.2), and must not widen a `docs.read` route to portal principals.

**A third, smaller cut:** §11.3 — the portal has no "search before you file" step, so the resolver's highest-value placement (pre-filing deflection) has nowhere to stand. Task 15 places the resolver **in-ticket only**. Not blocking; recorded so the frontend task does not invent a deflection surface.

---

## The health finding, and why it is not fixed here

§0.11 records that `POST /health/alerts/{id}/remediate` is gated on `health.remediate` alone, so its holder can approve a `patch.apply` remediation without holding `patch.apply` — the exact shape §0.11 refuses for this layer. Verified: `app/modules/health/api.py::remediate_alert_route` takes `require_permission("health.remediate")` and dispatches by `body.action_key`.

**Decision: this plan does not fix it, and ships the thing that would.** Task 2 builds `app/jobs/dispatch_permissions.py`, which is the machine-readable map health needs; Task 16 writes the finding and the one-paragraph fix into the health spec as a proposal for that module's own cycle.

**Why not fix it here.** The check cannot be a route dependency: the required permission depends on `body.action_key`'s resolved `job_type`, so it is a service-layer check inside the handler. That is a **behaviour change to another module's shipped authorization** — it turns a currently-succeeding call into a 403, so it edits health's test suite. Landing it inside a sixteen-task refactor means a security fix that nobody reviews on its own, in a diff nobody reads to the end. **Rejected alternative: fix it in Task 2** "while we are in there" — that is how the fix arrives unreviewed, and it is the same argument §0.11 makes about why the map should be data rather than prose.

**What this plan does guarantee:** this layer does not inherit the gap. §9.2's confirm is two-part and Task 8 tests it (§13 test 1: `ai.act.confirm` without the job type's dispatch permission is a 403 and no job is created). Nothing built here widens health's hole.

---

## File structure

**Created (backend, kernel):**

| File | Responsibility |
| --- | --- |
| `backend/app/models/ai.py` | `AiSession`, `AiMessage`, `AiInjectionFlag`, `AiSettings` (moved), `AiAction`, `AiAutonomyCeiling` — declarative classes beside the other kernel models |
| `backend/app/core/ai/__init__.py` | Re-exports the concept, the way `app/core/approved_actions.py` does |
| `backend/app/core/ai/types.py` | `Tool` protocol, `ToolSpec`, `ToolResult`, `TurnContext`, `ReadTool` (moved) |
| `backend/app/core/ai/personas.py` | `PersonaProfile`, `register_persona`, `PERSONAS: Mapping[str, PersonaProfile]` (MappingProxyType) |
| `backend/app/core/ai/orchestrator.py` | Turn loop, budgets, stop reasons (moved) |
| `backend/app/core/ai/providers/` | `__init__.py`, `_http.py`, `openai_compatible.py`, `anthropic.py` (moved) |
| `backend/app/core/ai/envelope.py`, `scrub.py`, `detect.py`, `describe.py` | Moved unchanged |
| `backend/app/core/ai/executor_staff.py` | The staff loopback executor (moved from `executor.py`) |
| `backend/app/core/ai/executor_portal.py` | **New** — the portal loopback executor (§0.2, Task 13) |
| `backend/app/core/ai/store.py`, `retention.py`, `settings.py`, `errors.py` | Moved |
| `backend/app/core/ai/autonomy.py` | **New** — the dial (§4) |
| `backend/app/core/ai/act.py` | **New** — `ActTool`, propose, preview, digest, confirm, dispatch, reject (§6) |
| `backend/app/core/ai/sweep.py` | **New** — expiry sweep, settle hook, `rationale` blanking |
| `backend/app/jobs/dispatch_permissions.py` | **New kernel** — `job_type → permission code` (§10.2) |

**Created (backend, personas):** `app/modules/ai_admin/profile.py`; `app/modules/ai_copilot/` (`__init__.py`, `profile.py`, `tools.py`, `api.py`, `schema.py`); `app/modules/ai_portal/` (same five).

**Modified (backend):** `app/core/rbac.py` (**kernel**, 2 codes), `app/models/__init__.py` (**kernel**), `app/core/approved_actions.py` (**kernel**, Task 3's `fuse_breach`), `app/modules/health/actions.py` (delegation only), `app/modules/__init__.py` (**shared**), `app/worker.py` (**shared**, one beat entry), `app/modules/ai_admin/{__init__,api,schema,prompt,tools/*}.py`.

**Created (frontend):** `frontend/src/modules/ai-automation/` — `api.ts`, `types.ts`, `ActionPreview.tsx`, `ConfirmDialog.tsx`, `AutonomyDialPage.tsx`; plus `frontend/src/modules/service-desk/TicketCopilotPanel.tsx` and `frontend/src/modules/portal/PortalAssistantPanel.tsx`.

**Not created, deliberately:** no agent code; no new job type; no `ai_action` idempotency key (§0.5); no second article store (§11.2); no device-ownership column (§11.1); no `actor_type='ai'` (§8.2); no `autonomous` level anywhere (§0.4).

---

## Sequencing, and why this order

Three kernel changes land first because everything depends on them **and** because they are the tasks most likely to break suites that have nothing to do with this layer (constraint 3). The extraction lands next, as **one atomic task**, before any act code exists — so the move is mechanically verifiable ("the full suite is green and nothing changed but import paths") rather than tangled with new behaviour. Act machinery is built on the moved code, persona by persona, with the admin persona (which already has a UI and a test suite) before the two new ones. The cross-cutting attack suite lands after all three personas exist, because two of its tests are about a persona the earlier tasks do not have.

**What breaks under the obvious alternative orders.** Extraction first, kernel second: Task 1 then edits `app/models/ai.py` and `tests/` files that have just moved, and a failure is ambiguous between "the move was wrong" and "the CHECK was wrong". Act code before extraction: act machinery grows inside `app/modules/ai_admin/`, which is the exact failure `tests/test_ai_admin_boundaries.py` was rewritten in `6d57334` to prevent, and unwinding it later is not mechanical.

---

# Phase A — kernel (Tasks 1–3)

**Run the full backend suite at the end of each of these** (constraint 3).

---

## Task 1 — KERNEL: Migration A — `ai_autonomy_ceiling`, the two CHECK widenings, two permission codes

**Files:**
- Create: `backend/migrations/versions/<A>_ai_autonomy_ceiling.py` (number per constraint 13), `backend/app/models/ai_automation.py`
- Modify (**kernel**): `backend/app/core/rbac.py`, `backend/app/models/__init__.py`; `backend/app/modules/ai_admin/models.py` (the two CHECKs)
- Test: create `backend/tests/test_ai_autonomy_ceiling.py`; modify `backend/tests/test_ai_admin_migration.py`, `backend/tests/test_rbac.py`, `backend/tests/test_api_routes.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `AiAutonomyCeiling`; permission codes `ai.act.propose`, `ai.act.confirm`; widened `ai_session` CHECKs; the seed helper later tasks import — `_seed_ceiling(api_env, org_id, *, persona, risk_class, level) -> None`.

- [ ] **Step 1: Confirm the migration head** — `cd backend && .venv/bin/alembic heads && ls migrations/versions/`. Record the number you took.

- [ ] **Step 2: Write the failing tests**

`tests/test_ai_autonomy_ceiling.py`, opening with a docstring stating the RLS tests are **behavioural** (a test asserting `relrowsecurity` passes against a policy whose predicate was deleted). Minimum coverage:

- `relrowsecurity` **and** `relforcerowsecurity` true;
- cross-org **read** and cross-org **insert** through `org_session` on `api_env.app_engine`;
- **`'autonomous'` is unstorable** (§13 test 11) — and this is the one most exposed to constraint 4's first trap. Insert `level='autonomous'` on the admin engine, and **assert first that a SELECT for that `(org_id, persona, risk_class)` returns no row**, then assert the raise. A bare `pytest.raises` reporting `DID NOT RAISE` tells a reader nothing about what the table now holds;
- **`end_user` cannot be raised above `off`** — see the spec gap below;
- the PK `(org_id, persona, risk_class)` rejects a duplicate;
- `persona` CHECK rejects `'requester'`; `risk_class` CHECK rejects `'critical'`;
- a missing row is **not** an error and means `off` — asserted in Task 5 against the resolver, named here so nobody adds a NOT-FOUND path.

In `tests/test_ai_admin_migration.py`, the parametrized `test_ai_session_persona_and_mode_checks` table flips. **Do not let every case become `True`** (constraint 4's second trap): the widened table must keep live negatives —

```
("persona", "admin", True), ("persona", "technician", True), ("persona", "end_user", True),
("persona", "requester", False),          # still refused: not a persona of this layer
("mode", "read", True), ("mode", "act", True),
("mode", "read_act", False),              # the old value; still refused
```

- [ ] **Step 3: Run to verify failure.**

- [ ] **Step 4: Write the model and the migration**

`app/models/ai_automation.py`, columns exactly per §4.1: `org_id` (FK `organizations.id`), `persona` `String(16)`, `risk_class` `String(8)`, `level` `String(20)`, `updated_by` (FK `users.id`, nullable), `updated_at`. PK `(org_id, persona, risk_class)`. CHECKs:

- `persona IN ('end_user','technician','admin')`
- `risk_class IN ('low','medium','high')`
- `level IN ('off','suggest','require_approval')` — **no `'autonomous'` value exists at all** (§0.4). Widening it is a migration a reviewer sees, which is the same argument `ck_approved_action_autonomous_is_bounded` makes for itself.
- **`NOT (persona = 'end_user' AND level <> 'off')`** — see the spec gap below.

Migration A additionally: `ALTER` the two `ai_session` CHECKs (drop + recreate — a CHECK cannot be widened in place), and seed the two permission codes. Copy the exact permission-insert SQL and the standard RLS block (`ENABLE + FORCE ROW LEVEL SECURITY`, `tenant_isolation` with **both** `USING` and `WITH CHECK`) out of the most recent module migration rather than trusting this plan.

`app/core/rbac.py` gains, with §9.2's descriptions and HIGH-PRIVILEGE wording on `ai.act.confirm`:

```
"ai.act.propose": "AI: open an act-capable session and mint action proposals. Grants NO ability to confirm or dispatch",
"ai.act.confirm": "AI: confirm a previewed AI action. HIGH PRIVILEGE, and NOT sufficient on its own -- the confirming user must also hold the permission the owning module's own dispatch route requires",
```

**Grants (§16, and the thing that breaks the unrelated suite).** §16 says `ai.act.propose` goes to the **technician tier** and `ai.act.confirm` to the **admin tier**, both to super-admin/org-admin so `test_org_admin_has_all_permissions` holds. Granting `ai.act.propose` to the seeded `technician` role **turns `tests/test_api_routes.py::test_me_returns_identity_and_permissions` red** — it pins the technician's exact permission set as a literal. **Adding the code to that literal is the correct fix, not weakening the assertion.** If you decide instead to follow the ai-admin precedent (`ai.use` was seeded to org-admin only, so the surface ships inert), that is a **deviation from §16** — implement it only with a recorded reason in your report and an amendment to the spec, never silently.

- [ ] **Step 5: Run tests, then the FULL backend suite**

`cd backend && .venv/bin/pytest tests/test_ai_autonomy_ceiling.py tests/test_ai_admin_migration.py tests/test_rbac.py tests/test_api_routes.py tests/test_schema_drift.py tests/test_rls.py -v`, then `.venv/bin/pytest -q > /tmp/full.txt 2>&1; tail -40 /tmp/full.txt` (constraint 11 — one foreground call).

- [ ] **Step 6: Prove the guards can fail**

- **Tenancy:** `WITH CHECK (true)` → the insert test red; `USING (true)` → the read test red. Run **both** (constraint 6). Paste both.
- **The `level` CHECK:** add `'autonomous'` to it, rebuild. **The test observes a stored `ai_autonomy_ceiling` row at `level='autonomous'` that you can SELECT back**, not a missing exception. Red; restore. *This is the most important mutation in the task: §0.4's whole claim is that the AI personas' vocabulary stops one short, enforced by the database.*
- **The `end_user` CHECK:** delete it. **The test observes a stored `end_user` row at `require_approval`.** Red; restore.
- **The `mode` widening:** revert `mode IN ('read')`. The parametrized `("mode","act",True)` case goes red — which is what proves act sessions became representable.

**Spec gap you must close in this task (flagged; implement, record).** §3.1 says the `end_user` persona may never "be raised above `off` in `ai_autonomy_ceiling` for any risk class", but §4.1's table gives no CHECK for it — the only stated CHECK is on `level`'s vocabulary. Application code enforcing it would be exactly what §0.4 refuses for `autonomous`. **Resolution: a fourth CHECK, `NOT (persona = 'end_user' AND level <> 'off')`.** Record it into spec §4.1.

**Definition of done:** `ai_autonomy_ceiling` with RLS forced and behavioural cross-org tests both directions; four CHECK classes enforced by Postgres and each independently falsified; `ai_session.mode`/`persona` widened with live negatives retained; two permission codes registered and seeded with the grant decision recorded; **full backend suite green**; `mypy`/`ruff` clean.

**Commit:** `feat(ai): the autonomy ceiling, and the session widening act mode needs`

---

## Task 2 — KERNEL: `app/jobs/dispatch_permissions.py`

**Read §0.11 and §10.2 in full.** The property: **which permission the owning module's own dispatch route requires is data, not prose** — so §9.2's second confirm check can be evaluated, and so a newly registered job type breaks a test instead of picking a side by accident.

**Files:**
- Create: `backend/app/jobs/dispatch_permissions.py`
- Test: `backend/tests/test_dispatch_permissions.py`

**Interfaces:**
- Consumes: `app.jobs.registry.registered_names`, `app.jobs.direct_dispatch.MODULE_GOVERNED_REASONS`, `app.core.rbac.PERMISSIONS`, `route_permission`.
- Produces: `DISPATCH_PERMISSIONS: Mapping[str, str]`, `NO_HUMAN_DISPATCH_PATH: Mapping[str, str]` (job type → stated reason), `dispatch_permission_for(job_type) -> str | None`.

### What to build

A module-level dict, compiled into the image, no database. **Deny by default:** `dispatch_permission_for` returns `None` for anything in neither map, and Task 8 treats `None` as a refusal — a job type nobody has classified is not confirmable through this layer.

Derive the values **from the code, not from this plan**: `MODULE_GOVERNED_REASONS`' messages end with the owning permission in parentheses for every entry that has a route. Read them; do not retype them from memory. At writing they gave:

```
net.sweep → ipam.sweep.run          health.check.now → health.alert.manage
patch.scan → patch.scan             patch.apply → patch.apply
app.deploy → deploy.execute         script.run → deploy.script.run
backup.run → backup.run             backup.restore → backup.restore
provision.enroll → provisioning.run remote.session.open → remote.control
hello / inventory.collect / logs.ship → jobs.dispatch   (the kernel route, DIRECTLY_DISPATCHABLE)
```

`NO_HUMAN_DISPATCH_PATH` holds `health.policy.apply` with its reason ("owned by health's policy push and reconciliation; there is no route for it by design — assign a profile and the push is queued from the effective policy").

### Tests shipped

1. **The partition, against the live registry.** `set(DISPATCH_PERMISSIONS) | set(NO_HUMAN_DISPATCH_PATH) == set(registered_names())`, and the two are disjoint. Build the app first (copy `tests/test_direct_dispatch.py`'s fixture) — `registered_names()` is incomplete until modules register.
2. **Every value is a real permission code:** `set(DISPATCH_PERMISSIONS.values()) <= set(PERMISSIONS)`. *A renamed permission otherwise turns the map into a lie that reads fine.*
3. **Every value is a real route's gate.** Flatten the router; for each mapped permission, assert at least one `APIRoute` exists whose `route_permission(route)` equals it. *Without this, a plausible-looking code that gates nothing passes tests 1 and 2.*
4. **The map agrees with the prose it replaces.** For each `MODULE_GOVERNED_REASONS` message ending in `(<code>)`, parse that code and assert it equals `DISPATCH_PERMISSIONS[job_type]`. Ten of the eleven entries parse; `health.policy.apply` does not and must be the `NO_HUMAN_DISPATCH_PATH` entry — assert that explicitly, so "the parse found nothing" cannot masquerade as agreement. *This is the highest-value test in the file: it binds the new data to the English it is derived from, and the failure message names both.*
5. `dispatch_permission_for` returns `None` for an unregistered name and for every `NO_HUMAN_DISPATCH_PATH` entry.
6. **Non-vacuity:** `assert len(DISPATCH_PERMISSIONS) >= 10` and `assert NO_HUMAN_DISPATCH_PATH`, so an empty map cannot satisfy tests 1–5 the moment the registry fixture breaks.

### Falsifying mutations (run each, paste each)

- **Test 4:** repoint `script.run` at `jobs.dispatch` (a real code, gating a real route — so tests 2 and 3 stay green). **The test observes the map claiming `jobs.dispatch` where `direct_dispatch`'s own message names `deploy.script.run`.** Red; restore. *This is the mutation that shows the prose cross-check earns its place.*
- **Test 1:** delete one entry. **The test observes a registered job type in neither map.** Red; restore.
- **Test 3:** change one value to a plausible non-existent code (`patch.dispatch`). Red on 2 and 3; restore.

**Definition of done:** the map compiled into the image; the partition asserted against the live registry; every value proved to be both a registered permission and a live route gate; the prose cross-check green with `health.policy.apply` explicitly accounted for; three mutations pasted; `mypy`/`ruff` clean. **Nothing consumes this map yet — that is deliberate** (Task 8 does).

**Commit:** `feat(jobs): the dispatch-permission map direct_dispatch only had in prose`

---

## Task 3 — KERNEL: lift the rate-fuse evaluation into `app/core/approved_actions.py`

**A fourth kernel change the spec's §10 does not name. Record it into §10 in Task 16.**

**Why it is required, not convenient.** §6.1 requires the preview to *read* the three rate fuses and §6.5 step 3 requires the dispatch to *gate* on them. The only implementation is `app/modules/health/actions.py::_fuse_breach` — a private function in a business module. §2.3 property 1 makes "`app/core/ai/` imports no `app.modules.*` package" a **test**, so `core/ai/act.py` cannot call it. Reimplementing it in `core/ai` would produce two derivations of "is this fused", which is precisely the defect `is_destructive`'s docstring exists to prevent ("two forks means two answers to 'may this run without a human'").

**Files:**
- Modify (**kernel**): `backend/app/core/approved_actions.py`; `backend/app/modules/health/actions.py`
- Test: create `backend/tests/test_approved_actions_fuses.py`

**Interfaces:**
- Produces: `fuse_breach(session, org_id, action, *, device_id: UUID, now: datetime) -> str | None`.

### What to build

Move the body verbatim. `health.actions._fuse_breach` becomes a one-line delegation (or its call sites import the kernel function directly — either is fine; the constraint is that there is one body). **The three refusal message strings must stay byte-identical**: health's tests assert that each branch names the job ids it counted against the budget, and that assertion was itself the fix for a test that could not fail. **The definition of done includes: no test file under `tests/test_health_*` is edited.** If one has to be, the move was not mechanical — stop and report.

Keep it where the concept already lives: this module is documented as "the closed maps, the pure functions over them, and two org-scoped reads". `fuse_breach` is a third org-scoped read and belongs beside them; it is not pure and its docstring must say so, unlike `is_destructive` directly above it.

### Tests shipped

`tests/test_approved_actions_fuses.py` — the three fuses, pinned in the kernel for the first time:

1. Cooldown: a job of the same type on the same device inside `cooldown_s` → a reason naming the job id. Then **one just outside the window → `None`**. Both halves, because the second is what proves the window is read rather than the branch always firing.
2. `max_per_device_per_day`: at the limit → refused; one below → `None`.
3. `max_per_org_per_hour`: same shape.
4. **The org predicate, on the admin engine** (constraint 5). Seed org B's jobs of the same type, same job type, same device-id shape; assert org A's fuse verdict is `None`. Through `org_session` this passes with the predicate deleted — that is the point, and the test docstring says so.
5. **Non-vacuity for the `None` cases** (constraint 4's third trap): every "not fused" assertion seeds a **nearly matching** row — right job type, wrong device, or right device just outside the window — so `None` is evidence the predicate discriminated, not evidence the table was empty.

### Falsifying mutations

- **Delete `Job.org_id == org_id`.** **Test 4 observes org B's jobs counted against org A's fuse** on the admin engine, and stays green through `org_session`. Paste both runs.
- **Change `>` to `>=` on the cooldown window boundary.** Test 1's "just outside" case observes a refusal. Red; restore.
- **Delete the per-device branch.** Test 2 observes `None` where it expects a reason. Red; restore.

**Definition of done:** one body, called from both health and (later) `core/ai/act.py`; health's own suite green **with no edits**; the three fuses pinned in a kernel test with the org predicate pinned on the admin engine; **full backend suite green**.

**Commit:** `refactor(approved-actions): the rate fuses move to the kernel that owns the registry`

---

# Phase B — the extraction (Task 4)

---

## Task 4 — THE EXTRACTION: `app/core/ai/`, `app/models/ai.py`, and `PersonaProfile`

**This task changes no behaviour. Its definition of done is that the full suite is green and the only diff is import paths, file locations, and the boundary suites that follow them.** Read §0.1, §2.1, §2.2 and §2.3 in full first.

**This task touches `app/models/__init__.py` (kernel) — full backend suite.**

**Files:**
- Create: `backend/app/models/ai.py`; `backend/app/core/ai/` per the file-structure table (`__init__.py`, `types.py`, `personas.py`, `orchestrator.py`, `providers/{__init__,_http,openai_compatible,anthropic}.py`, `envelope.py`, `scrub.py`, `detect.py`, `describe.py`, `executor_staff.py`, `store.py`, `retention.py`, `settings.py`, `errors.py`); `backend/app/modules/ai_admin/profile.py`
- Delete (by `git mv`): the corresponding files under `backend/app/modules/ai_admin/`
- Modify: `backend/app/models/__init__.py` (**kernel**), `backend/app/worker.py`, `backend/app/modules/ai_admin/{__init__,api,schema,prompt}.py`, `backend/app/modules/ai_admin/tools/{__init__,registry,args}.py`
- Test: create `backend/tests/test_ai_boundaries.py`; modify every `backend/tests/test_ai_admin_*.py` (≈20 files) and `backend/tests/ai_admin_fakes.py`

**Interfaces:**
- Produces: `app.core.ai` as the import path for everything shared; `PersonaProfile`, `register_persona(profile)`, `PERSONAS: Mapping[str, PersonaProfile]`; `app.modules.ai_admin.profile.ADMIN_PROFILE`.

### What to build

**Move with `git mv`** so history follows; a delete-plus-create makes the "nothing changed but locations" claim unverifiable at review.

**What moves** (§2.1's table, plus two the table omits and this plan decides):

- The four **declarative models** move from `app/modules/ai_admin/models.py` to `app/models/ai.py`. §2.1 does not list them, but it must: `core/ai/store.py` reads them, and a kernel module importing `app.modules.ai_admin.models` fails §2.3 property 1. Table names do not change, so **no migration**. This mirrors the forced split `app/models/approved_actions.py` documents (a declarative model under `app/core/` cannot be registered in `app/models/__init__.py` without a verified import cycle). `app/models/__init__.py`'s import flips from `app.modules.ai_admin.models` to `app.models.ai`.
- `errors.py` moves: the exception types are raised by moved code.

**What stays** in `app/modules/ai_admin/` (the admin persona package): `__init__.py` (`register(app)`), `api.py`, `schema.py`, `prompt.py` (§2.2: the system prompt is per-persona and is a module-level constant, never assembled from data), `tools/registry.py` + `tools/args.py` (the admin persona's read-tool set), and the new `profile.py`.

**`PersonaProfile`** in `core/ai/personas.py`, exactly §2.2's frozen dataclass (`name`, `system_prompt`, `executor`, `tools`, `may_propose`, `replay_policy`), registered into a `MappingProxyType` at import time — the same immutability posture `tools/registry.py` already takes, for the same reason (whatever compromises the database must not choose the agent's capabilities). `ADMIN_PROFILE` ships with `may_propose=False` and `replay_policy="verbatim"`; Task 11 flips them.

**No shim, no re-export, left behind in `app/modules/ai_admin/`.** A compatibility shim would let a persona package import through the admin module, which inverts the dependency direction §0.1 exists to fix, and it would make §2.3 property 1 pass while the thing it forbids happens one hop away.

### The boundary suites, relocated and widened (§0.1's stated cost)

This is the half of the task that is not mechanical, and getting it wrong is how "the first act-mode task is also the task that quietly deleted read mode's proof."

**`tests/test_ai_boundaries.py` (new, kernel-scoped):**

1. **`app/core/ai/` imports no `app.modules.*` package** (§13 test 19) — AST over `Import`/`ImportFrom`, with a non-vacuity floor on the file count.
2. **Exactly two HTTP-client construction sites in `app/core/ai/`** — the existing `_CLIENT_MARKERS` scan, with the equality set becoming `{"executor_staff.py", "providers/_http.py"}`. **Task 13 changes this to three** (`executor_portal.py`) and the equality is what makes that a decision somebody makes rather than a thing that happens.
3. **Zero construction sites in any persona package** (§13 test 20's "now across every persona package"). **Derive the package list from `PERSONAS`' registered profiles, never from a literal** — a hardcoded list means the fourth persona is unscanned, which is the same defect as a denylist.
4. The allowlist and both denylists are still module constants (the AST scan over `registry.py`, re-pointed), `_NEVER_A_TOOL`'s four pairs still resolve to real routes, and every `_FORBIDDEN_GATES` code is still in `PERMISSIONS`.
5. Every registered `PersonaProfile`'s `system_prompt` is a module-level constant in its own package with **one** assignment site and no `{`/`%` interpolation.

**`tests/test_ai_admin_boundaries.py` (kept, re-pointed):** it stays organised by *guarantee*, and its `PACKAGE_ROOT` stays `app/modules/ai_admin/`. Two things must change and neither is optional:

- **`_source_files()`'s `assert len(files) >= 10` vacuity floor is now false** — the package is about half its old size. **Re-pin it to the real count** (measure it) and keep it a floor with its comment intact. A scan that finds nothing passes every assertion below it.
- **`test_act_mode_is_not_grown_inside_this_module`** imports `ReadTool` from `app.modules.ai_admin.tools.types`, which no longer exists. Re-point it to `app.core.ai.types`. Its anchored-regex guards (`^\s*class ActTool\b`, `\bmutating\s*=\s*True\b`, `^\s*autonomy\s*[:=]`, …) stay **exactly as written** — they are matched against definitions, not mentions, deliberately (constraint 10), and the comment saying so must survive the move. Task 11 revisits this test; **this task does not weaken it.**

### Tests shipped

Everything that exists today, passing unchanged in substance. Plus the two boundary files above. **No new behavioural test belongs in this task** — a behavioural test written here is a behaviour change hiding in a move.

### Falsifying mutations (run each, paste each)

- Add `from app.modules.health import actions` to `core/ai/act`-adjacent file (use `store.py`). **Test 1 observes a kernel module importing a business module.** Red; restore.
- Add `httpx.AsyncClient(` to `core/ai/orchestrator.py`. **Test 2 observes three sites where it names two.** Red; restore.
- Add it to `app/modules/ai_admin/api.py` instead. **Test 3 observes a persona package constructing an egress client.** Red; restore. *Run this one: it is the assertion §0.1 says goes silently un-asserted if the suite does not follow the code.*
- Delete `test_ai_admin_boundaries.py`'s re-pinned vacuity floor and point `PACKAGE_ROOT` at a non-existent directory. **Every assertion in the file passes.** Restore both, and paste the run — it is the demonstration that the floor is load-bearing.

**Definition of done:** every shared component under `app/core/ai/`; the four models under `app/models/ai.py` with `app/models/__init__.py` no longer importing from `app.modules.ai_admin`; `ADMIN_PROFILE` registered; **no shim left behind**; both boundary suites green with the widened assertions and a re-pinned vacuity floor; **full backend suite green at the same pass count as the baseline**; `git log --follow` finds the history of a moved file; `mypy`/`ruff` clean.

**Commit:** `refactor(ai): the orchestration machinery becomes kernel, and ai_admin becomes a persona`

---

# Phase C — the act machinery (Tasks 5–10)

---

## Task 5 — The dial: `app/core/ai/autonomy.py` and `/ai/autonomy`

**Files:**
- Create: `backend/app/core/ai/autonomy.py`
- Modify: `backend/app/modules/ai_admin/{api,schema}.py`
- Test: `backend/tests/test_ai_autonomy.py`

**Interfaces:**
- Consumes: `AiAutonomyCeiling` (T1), `audit_service.record`.
- Produces: `LEVELS: tuple[str, ...]`, `min_level(*levels) -> str`, `effective_autonomy(*, ceiling: str, action_autonomy: str) -> str`, `get_ceilings(session, *, org_id) -> dict[tuple[str,str], str]`, `set_ceiling(session, *, org_id, persona, risk_class, level, actor_id) -> None`; routes `GET`/`PUT /api/v1/ai/autonomy` gated `ai.admin`.

### What to build

`LEVELS = ("off", "suggest", "require_approval", "autonomous")` — ordered, and `autonomous` exists **only** as an input value that can be reduced away (an `approved_action.autonomy` may be `autonomous`; a ceiling may not). `effective_autonomy` is §4.2 exactly:

```
min_level(ceiling(org, persona, risk_class), action.autonomy, "require_approval")
```

with the third argument the v1 clamp, and `off` returned **before the dial is consulted** when `persona not in action.personas`. A missing ceiling row is `off` (deny by default; the layer ships inert with no rows, the way `ai_settings.enabled` ships `false`).

Reads carry an **explicit `org_id` predicate in addition to RLS** (§9.1). `set_ceiling` is an upsert on the real PK (not a partial index — constraint 7 does not bite here, and the comment should say why it does not, so nobody "fixes" it) and writes `ai.autonomy_changed` with `persona`, `risk_class`, old level, new level. Old level is read inside the same transaction; a write that cannot say what it changed from is an audit record that answers nothing.

Routes live on the admin persona's router (§12) and are gated `ai.admin` — **not** `health.action.admin`: that grant governs what may run automatically as SYSTEM/root (the catalogue); this one governs which AI persona may reach it (§0.4).

### Tests shipped

1. **`effective_autonomy`, parametrized so no two cases share an answer** (constraint 4's second trap). At minimum: the ceiling wins (`ceiling='suggest'`, `autonomy='require_approval'` → `suggest`); the catalogue wins (`ceiling='require_approval'`, `autonomy='suggest'` → `suggest`); the **v1 clamp** wins (`ceiling='require_approval'`, `autonomy='autonomous'` → `require_approval`); `off` beats everything; a missing row → `off`.
2. `persona not in action.personas` → `off`, **without any ceiling row existing** (§4.2's last line).
3. The org predicate, pinned on the **admin engine** (constraint 5): org B's ceilings do not answer org A's question.
4. `PUT` writes `ai.autonomy_changed` carrying both levels; the metadata contains no free text.
5. `PUT` with `level='autonomous'` → 400 (Pydantic), and **no row is written** — assert the SELECT, not the status alone.
6. Route RBAC: a holder of `ai.use` but not `ai.admin` gets 403 on `GET` and `PUT`.

### Falsifying mutations

- **Delete the `"require_approval"` argument from `min_level`.** Case 1's clamp case observes `autonomous`. Red; restore. *If your parametrization made every case expect `require_approval`, this mutation passes — that is the check on the check.*
- **Delete the org predicate.** Test 3 observes org B's level on the admin engine; green through `org_session`. Paste both.
- **Make a missing row default to `suggest`.** Test 1's missing-row case observes `suggest`. Red; restore. *A dial that fails open is the whole layer failing open.*

**Definition of done:** the resolver implemented as §4.2 writes it and independently falsified; deny-by-default on a missing row; the org predicate pinned on the admin engine; both routes gated and audited; `mypy`/`ruff` clean.

**Commit:** `feat(ai): the autonomy dial, which can only ever lower`

---

## Task 6 — Migration B: `ai_action`, and two `ai_settings` tunables

**Touches `app/models/__init__.py` (kernel) — full backend suite.**

**Files:**
- Create: `backend/migrations/versions/<B>_ai_action.py`
- Modify: `backend/app/models/ai.py`, `backend/app/models/__init__.py` (**kernel**)
- Test: `backend/tests/test_ai_action_migration.py`

**Interfaces:**
- Produces: `AiAction`; `ai_settings.action_expiry_s`, `ai_settings.max_proposals_per_turn`; the seed helper later tasks import — `_seed_ai_action(api_env, org_id, **kw) -> UUID`.

### What to build

`ai_action` exactly per §5.1: `id` (uuid7), `org_id`, `session_id` (FK `ai_session.id` **`ON DELETE RESTRICT`**), `message_seq`, `persona`, `approved_action_id` (FK), `approved_action_key`, `job_type`, `target_device_id` (FK `devices.id`, **NOT NULL**, single device — §0.12), `rendered_params` (jsonb), `script_id`/`script_version` (nullable), `risk_class`, `destructive`, `effective_autonomy`, `tainted` (default false), `preview_digest` `char(64)`, `rationale` (text, default `''`), `state`, `expires_at`, `confirmed_by`/`confirmed_at`, `rejected_reason`, `job_id` (FK `jobs.id`, nullable), `created_at`/`updated_at`.

CHECKs: `persona` and `risk_class` as §4.1; `state IN ('proposed','previewed','confirmed','dispatched','settled','rejected','expired','failed')`; `effective_autonomy IN ('suggest','require_approval')` — a stored `off` would be a row that should never have been minted, and `autonomous` is unreachable in v1 (§0.4).

Indexes, **declared in `__table_args__` as well as in the migration** (constraint 8): `(org_id, state, expires_at)`, `(org_id, session_id)`, `(org_id, target_device_id)`.

`ai_settings` gains `action_expiry_s` (default 900, CHECK `BETWEEN 60 AND 86400` — §6.4's 60 s–24 h) and `max_proposals_per_turn` (default 3, CHECK `BETWEEN 1 AND 10` — §6.5). **Recorded spec gap:** §6.4 and §6.5 say both are "org-configurable, CHECK-bounded" without saying where they live; this plan puts them on `ai_settings` beside the four tunables that already carry ceilings there. Record into the spec.

**No unique constraint and no idempotency key on `ai_action`, deliberately** (§0.5 rejects reusing `health_alert_action`'s `UNIQUE (alert_id, kind)` because an AI proposal has no alert). Write that into the model docstring, with constraint 7's reason attached: a builder adding one "for dedupe" will reach for a partial index over nullable columns, NULLs will not collide, and `ON CONFLICT ON CONSTRAINT` against it fails at runtime.

### Tests shipped

1. RLS forced; cross-org read and cross-org insert, both directions.
2. The `state` CHECK rejects `'previewing'`; the `effective_autonomy` CHECK rejects `'autonomous'` and `'off'` — **SELECT back first, then assert the raise** (constraint 4).
3. **`ON DELETE RESTRICT`:** deleting an `ai_session` that has a `dispatched` `ai_action` raises **and the `ai_action` row is still there afterwards** — assert the row. Then: deleting a session whose actions are all `rejected` also raises (RESTRICT is not conditional), which is the honest cost and Task 9's retention design depends on knowing it.
4. `ai_message` still CASCADEs from `ai_session` — the asymmetry §5.1 argues for, asserted side by side so a reader sees both.
5. `target_device_id` NOT NULL; `expires_at` NOT NULL.
6. The two new `ai_settings` CHECKs at both bounds and one past each.
7. `tests/test_schema_drift.py` green, including the three indexes (constraint 8).

### Falsifying mutations

- `ondelete="RESTRICT"` → `"CASCADE"`. **Test 3 observes the `ai_action` row gone**, not a missing exception. Red; restore. *§5.1: "an `ai_action` that reached `dispatched` names a signed job that ran on somebody's machine."*
- `WITH CHECK (true)` and `USING (true)` in turn (constraint 6). Paste both.
- Drop one index from the migration but keep it on the model — **the drift test names it**. Then the reverse. Paste both directions; constraint 8's test is bidirectional and few people know it.

**Definition of done:** `ai_action` created with RLS, the CHECK vocabulary, RESTRICT proved by an assertion on the surviving row, three indexes declared on both sides, the two `ai_settings` tunables bounded; **full backend suite green**; spec gap recorded.

**Commit:** `feat(ai): the ai_action instance table and its preview clock`

---

## Task 7 — SECURITY GATE: propose and preview (`app/core/ai/act.py`)

**Read §0.6, §0.7, §6.1, §6.2, §6.4, §7.1 and §7.3 in full. The property this task establishes: there is no path from a request body, or from model output, to an effect.**

**Files:**
- Create: `backend/app/core/ai/act.py`
- Modify: `backend/app/core/ai/types.py` (nothing structural — `ToolResult.mutating` becomes reachable as `True` for the first time), `backend/app/core/ai/personas.py`
- Test: `backend/tests/test_ai_act_propose.py`, `backend/tests/test_ai_act_digest.py`

**Interfaces:**
- Consumes: `get_approved_action`, `non_approvable_reason`, `is_destructive`, `get_script_exemption`, `render_params_template`, `redact_secret_values`, `fuse_breach` (T3), `effective_autonomy` (T5), `AiAction` (T6), `canonical_bytes`/`rfc3339`.
- Produces: `ActTool` (implements the existing `Tool` protocol), `propose(...) -> AiAction | ToolError`, `build_preview(session, *, org_id, action_row) -> PreviewOut`, `compute_preview_digest(row, catalogue_row) -> str`, `MAX_PROPOSALS_PER_TURN` resolution.

### What to build

**One act tool, and its signature is the security design** (§0.7):

```python
actions.propose(action_key: str, device_id: UUID, rationale: str)
```

The model contributes a **selection** and prose. Job type, params, script pin, risk class, destructiveness and fuses all come from the catalogue row. **Do not build a tool per job type with typed params** — that is model output reaching `JobEngine.create`'s params, which `SUBSTITUTABLE_PARAM_KEYS`' own comment already refuses for operator templates.

`propose` performs §6.1's seven steps **in order, none skippable**:

1. `get_approved_action(session, org_id=…, key=…)`; unknown → `ToolError`, the turn continues (exactly as an unknown read tool does today).
2. Refuse if `persona not in action.personas`, or `action.active is False`.
3. Refuse if `non_approvable_reason(action.job_type)` is not `None` — re-checked here because the row may predate the allowlist; the kernel comment anticipates exactly this re-derivation.
4. Resolve `effective_autonomy`; `off` or `suggest` → **no row is created**, and the model is told which, truthfully.
5. Verify the target device is visible to the proposing principal **through a loopback read**, not a query — so `require_permission` and RLS decide, not this layer.
6. For `technician`, verify `device_id == ticket.device_id` (Task 12 supplies the ticket context; the check lives here).
7. Write the `ai_action` row and produce the preview.

**The preview performs no part of the act** (§0.6): no signing, no agent, `JobEngine.create` is not called. `render_params_template` is called (it raises at write time on an illegal token, and every `SUBSTITUTABLE_PARAM_KEYS` entry is empty today, so v1 params are constants). For `script.run`, resolve the pin and **re-derive `is_destructive` from it**, never from the row's stored boolean alone. Read the fuses via `fuse_breach` and report the verdict — **informational at preview; Task 8's read is the gate**. Where a read-only probe exists (`patch.scan` before `patch.apply`), the preview may **cite the most recent existing scan result and must not dispatch a fresh one** — dispatching a scan to build a preview is performing an act to describe an act.

**The digest** (§6.4) is computed **server-side from the stored row**, over exactly the ten named fields including `approved_action_updated_at`. **Constraint 9 lands here:** `canonical_bytes` raises `TypeError` on a UUID and on a datetime. Every uuid is `str(...)`, `approved_action_updated_at` is `rfc3339(...)`, `rendered_params` is canonicalized by the same function. A test must prove the digest is stable across two computations in different processes-worth of state, and that it **changes** when the catalogue row's `updated_at` moves.

**Taint** (§7.3): `tainted = true` when `detect()` fired on any retrieved content this turn, or any read touched a record whose provenance is unauthenticated ingress (`ticket.origin='email'`, or a comment created by `email_intake`). It caps `effective_autonomy` at `require_approval` — which is **already the v1 global clamp**, so taint's real work today is visibility. Write that into the docstring: a control whose effect is currently a no-op gets deleted by someone tidying up unless the reason it exists is written down.

**The per-turn cap** (§6.5): at most `ai_settings.max_proposals_per_turn` rows per turn; beyond it the tool returns an error the model can act on and the turn continues.

`ActTool` implements the same `Tool` protocol as `ReadTool` and returns `ToolResult(..., mutating=True)` — the first time that field is reachable as `True`. The audit writer and the UI already branch on it (§3.3), so nothing else changes.

### Tests shipped

`test_ai_act_propose.py`:

1. §13 test 3 — a catalogue row lacking the persona in `personas` → refused, **no row minted**.
2. §13 test 4 — a job type outside `APPROVABLE_JOB_TYPES` → refused **even though the legacy row names it**. Seed the row directly on the admin engine (the write path would refuse it), which is the whole point of step 3.
3. §13 test 10 — `off` mints no row; `suggest` mints no row; `require_approval` mints exactly one. **Seed a nearly-matching `ai_action` row first** (same org, same session, different `approved_action_key`) so `[]` is evidence the filter ran, not evidence the table is empty (constraint 4's third trap).
4. §13 test 12 — a `destructive` row and a `risk_class='high'` row produce no auto-dispatch at any dial position: parametrize over all three settable levels and assert `state == 'previewed'` and `job_id is None` in every case.
5. §13 test 16 — model output naming a `job_type`, a `params` blob, or a device the caller cannot see reaches no dispatch path: drive `ActTool.invoke` with adversarial args (`{"job_type": "script.run"}`, `{"params": {...}}`, `{"device_id": <another org's device>}`) and assert (a) `extra="forbid"` rejects the first two by field name, (b) the third produces a refusal and **the transport spy shows the loopback read returning 404**, never another org's row.
6. The device visibility check is the **loopback**, not a filter: a caller without `devices.read` gets a refusal even when the device exists in their org, asserted on the spy's recorded request.
7. The per-turn cap: the 4th proposal in one turn errors and the turn continues; exactly 3 rows exist.
8. `patch.apply` preview **dispatches nothing**: assert `select(count()).select_from(Job)` is unchanged across the preview.

`test_ai_act_digest.py`:

9. Determinism: two computations over the same stored row are equal.
10. A catalogue edit (touch `updated_at`, or change `params_template`) makes the digest **different** — assert inequality, never just "it raised".
11. Every UUID and datetime going into the digest is stringified: a direct call with a raw `UUID` in the field map raises `TypeError`, proving the guard is `canonical_bytes` and not a `str()` somewhere incidental.
12. `redact_secret_values` is applied on the preview read path: a legacy row with a `repo_key`-style template renders `[redacted: stored credential]` in the preview and the raw value appears nowhere in the response.

### Falsifying mutations (run each, paste each)

- **Accept `job_type` from the tool args** and use it instead of the catalogue row's. **Test 5 observes a proposal whose `job_type` came from model output.** Red; restore. *This is the mutation that proves §0.7.*
- **Skip step 3** (`non_approvable_reason`). Test 2 observes a previewed `ai_action` naming a non-approvable job type. Red; restore.
- **Trust `approved_action.destructive` instead of re-deriving from the pin.** Add a `script.run` row stored as non-destructive whose exemption does not match the live version; test 4 observes `destructive is False` on the preview. Red; restore.
- **Drop `approved_action_updated_at` from the digest.** Test 10 observes two equal digests across a catalogue edit. Red; restore. *Without it, §6.4's "editing the catalogue invalidates the confirmation" is not true.*
- **Return `[]` instead of a refusal for an `off` persona.** Test 3 observes an empty list where it expects a truthful "not permitted for this persona" — and note in your report that a silent empty result is the worse failure, because it reads as "nothing to do".

**Definition of done:** §6.1's seven steps in order; the preview persisted with a server-computed digest; nothing signed, no job created, no scan dispatched; taint recorded with its reason documented; the per-turn cap enforced; all twelve tests and all five mutations pasted; `mypy`/`ruff` clean.

**Commit:** `feat(ai): actions.propose, and a preview that is a persisted server-side resolution`

---

## Task 8 — SECURITY GATE: confirm → dispatch, and reject

**Read §0.11, §6.4, §6.5 and §9.2. This task makes "the confirming human must hold the permission the owning module's own dispatch route requires" a tested property.**

**Files:**
- Modify: `backend/app/core/ai/act.py`
- Test: `backend/tests/test_ai_act_confirm.py`, `backend/tests/test_ai_act_confirm_authz.py`

**Interfaces:**
- Consumes: `dispatch_permission_for` (T2), `fuse_breach` (T3), `effective_autonomy` (T5), `JobEngine.create`.
- Produces: `confirm_action(session, *, org_id, ai_action_id, digest, actor, permissions) -> tuple[UUID, Envelope]`, `reject_action(...)`, `ActConflict`/`ActExpired` errors.

### What to build

**The confirm request carries `ai_action_id` and the digest the human was shown, and nothing else.** No job type, no params, no target. The server recomputes the digest from the stored row and compares.

- digest mismatch → **409**, with a message naming what changed (catalogue edited, dial lowered, script version bumped). Not a 400: the request is well-formed; the world moved.
- `state <> 'previewed'` → **409**. A second confirm is a 409 by the state machine, not by an idempotency key.
- `now > expires_at` → **410**, and the row becomes `expired`.

Then, in **one transaction** (§6.5): re-resolve the catalogue row and re-derive `is_destructive` from the pin; re-evaluate the dial (org state may have moved and a confirmation is a decision about *now*); re-evaluate the fuses — **this read is the gate**; re-check the confirming principal's permissions; `JobEngine.create(session, org_id=…, type=action.job_type, target_device_id=ai_action.target_device_id, params=ai_action.rendered_params, requested_by=<confirming user>)` — the **unmodified** engine, `requested_by` set to the human because a human authorized it; set `state='dispatched'`, `job_id`; write `ai.action_dispatched`.

**Publish to Redis only after the tenant transaction commits** — `app/modules/deploy/scripts.py::run_script` states the reason and health's routes follow it: a job published before the caller's transaction commits could be handed to an agent with no durable server record. Build the envelope inside the block; publish after it exits.

**§9.2's two-part check, and the second part is the load-bearing one:**

```
require ai.act.confirm
AND     require dispatch_permission_for(job_type)      # None => refuse
AND     the confirming principal can read the target device (loopback GET)
```

`dispatch_permission_for` returning `None` is a **refusal**, not a pass — an unclassified job type is not confirmable here.

`reject_action` sets `rejected` with a **bounded enum** `rejected_reason` (§8.2 says bounded, not free text — model-adjacent free text on a durable row is what §8.3 spends a paragraph refusing).

### Tests shipped

`test_ai_act_confirm_authz.py`:

1. **§13 test 1, the governing principle:** a user holding `ai.act.confirm` but **not** `patch.apply` gets 403 confirming a `patch.apply` action, **and `select(count()).select_from(Job)` is unchanged**. Assert the job count, not just the status — "403" and "403 but it also dispatched" are different facts.
2. The same user holding both confirms successfully and exactly one `Job` row appears with `requested_by == <the human>`.
3. A job type in `NO_HUMAN_DISPATCH_PATH` (or unclassified) → refused, no job.
4. The confirming principal cannot read the target device → refused, asserted on the loopback spy.

`test_ai_act_confirm.py`:

5. §13 test 5 — a digest that does not match → **409**, no job.
6. §13 test 6 — editing the `approved_action` row between preview and confirm → **409**, and the message names the catalogue.
7. §13 test 7 — lowering `ai_autonomy_ceiling` between preview and confirm → refused, no job.
8. §13 test 9 — `expires_at` passed → **410**, `state == 'expired'`, no job.
9. A second confirm of a `dispatched` row → 409; **the first job is not duplicated** (assert the count).
10. A fuse that passed at preview and is breached at confirm → refused, no job. *The preview's fuse read is informational; this is the test that says so.*
11. §13 test 17 — a cross-org `ai_action` read is a **404**, never another org's row; and the same on confirm.
12. §13 test 21 — a dispatch writes `ai.action_dispatched` **and** the kernel's own job record; assert both.

### Falsifying mutations (run each, paste each)

- **§13 test 8, required by the spec:** change the handler to take `job_type` and `params` **from the request body** instead of the stored row. **A test must fail. If none does, the suite does not test the property and the task is not done.** Paste the red run. *This is the mutation the spec names by number; it is not optional and it is not satisfied by reasoning.*
- **Compare the client's digest to itself** (skip the recomputation). Test 5 or 6 observes a confirm succeeding against an edited catalogue row. Red; restore.
- **Delete the `dispatch_permission_for` check.** Test 1 observes a `patch.apply` job created by a caller who cannot run one by hand — which is invariant #6 violated. Red; restore.
- **Move `publish_job` inside the `org_session` block.** The structural test (Task 14) observes it; note the finding in your report even though this task's own tests stay green — that is the point of the structural sweep.

**Definition of done:** confirm reads the stored row and nothing else; two-part RBAC enforced with `None` meaning refuse; 409/409/410 distinguished; the dial and the fuses re-evaluated at dispatch; publish-after-commit; §13 tests 1, 5–9, 17, 21 green; **the spec's named mutation test demonstrated and pasted**; `mypy`/`ruff` clean.

**Commit:** `feat(ai): confirm dispatches the stored preview, or refuses it`

---

## Task 9 — The sweeps: expire, settle, and blank the rationale

**Files:**
- Create: `backend/app/core/ai/sweep.py`
- Modify: `backend/app/worker.py` (**shared** — one beat entry), `backend/app/core/ai/retention.py`
- Test: `backend/tests/test_ai_act_sweep.py`

**Interfaces:**
- Produces: `expire_stale_actions(session, *, org_id, now) -> int`, `settle_action_for_job(session, *, org_id, job_id, terminal_state) -> None`, `blank_rationales(session, *, org_id, cutoff) -> int`; a beat entry `ai_action_expiry_all_orgs`.

### What to build

**Expiry** (§5.2): rows in `previewed` (and the transient `proposed`, which means a crash between insert and digest) past `expires_at` become `expired`, one `ai.action_expired` each carrying `age_s`. **Per-org, with one org's failure not aborting the others** — use `app/worker.py`'s existing `_OrgCycleFailures` shell, which exists because that exact defect shipped (`3d3ac07`).

**Settle** (§5.2): `dispatched` → `settled` when the job reaches a terminal state, **set by the same result path that already updates `jobs`** — this layer runs no poller. Find that path and hook it; if it turns out there is no single one, report that rather than adding a poller.

**Retention** (§8.3): the transcript sweep **blanks `rationale` and nothing else** on `ai_action`, on the transcript's clock — every other column survives, **including on `dispatched` rows**, because a dispatched action names a signed job that ran on somebody's machine. `ai_action` is never deleted by retention, and §5.1's `ON DELETE RESTRICT` means a session with a dispatched action cannot be hard-deleted out from under it — so the **existing** `ai_admin` retention sweep, which hard-deletes `ai_session` rows, will now raise on such a session. **Handle it explicitly**: skip (and count) sessions with surviving `ai_action` rows, blanking their rationales instead. A retention sweep that starts raising in production because of a foreign key added six tasks earlier is this plan's most likely un-caught regression.

### Tests shipped

1. A `previewed` row past `expires_at` becomes `expired` with one audit event; a row **one second inside** the window is untouched. Both halves.
2. §13 test 23 — the sweep blanks `rationale` and leaves every other column intact **including on a `dispatched` row**: assert `rationale == ""` **and** assert `job_type`, `target_device_id`, `preview_digest`, `confirmed_by`, `job_id` are unchanged, by value. *Asserting only that `rationale` is empty passes against a sweep that deleted the row.*
3. The retention sweep over a session with a dispatched action **does not raise**, deletes the transcript messages, and leaves the `ai_action` row — with its `rationale` blanked. Assert all three.
4. A failure in org A's expiry pass does not prevent org B's — monkeypatch a raise for one org and assert the other's rows moved.
5. Settle: a job reaching a terminal state moves its `ai_action` to `settled`; a job reaching it twice does not double-write.

### Falsifying mutations

- **Make the sweep `DELETE` instead of blanking.** Test 2 observes a missing row where it expects a row with four unchanged columns. Red; restore.
- **Blank `rationale` on `previewed` rows before they expire.** Test 2's dispatched case stays green; add the case that catches it — a `previewed` row inside the retention window keeps its rationale. Red; restore.
- **Remove the `_OrgCycleFailures` shell.** Test 4 observes org B's rows unswept. Red; restore.

**Definition of done:** expiry, settle and rationale-blanking implemented per-org with failure isolation; the existing transcript sweep's new `RESTRICT` interaction handled and tested; §13 test 23 asserted on values rather than on absence; one beat entry with its interval constant beside the sweep, not a literal in `worker.py`.

**Commit:** `feat(ai): the expiry sweep, and a rationale that lives on the transcript's clock`

---

## Task 10 — SECURITY GATE: act-mode replay neutralization, and a mode that cannot be promoted

**Read §0.8 and §7.4. `docs/modules/ai-admin/spec.md` §17.11 says its reasoning "expires with read mode". It has expired; this task discharges it.**

**Files:**
- Modify: `backend/app/core/ai/orchestrator.py` (or wherever `_history` lands after Task 4 — it is in `ai_admin/api.py` today; **it moves to core in Task 4 or in this task, and this task is where it must end up**), `backend/app/core/ai/personas.py`
- Test: `backend/tests/test_ai_act_replay.py`

### What to build

For a session whose profile has `replay_policy="neutralize_flagged"` (act-capable), any prior **assistant** row on which `detect()` fires is replaced by a server-authored stub:

```
[A previous answer in this conversation quoted content that matched the
 injection heuristic (rule: <rule>, source: <tool>/<source_id>). Its text has
 been withheld from this turn. Ask again if you need it restated.]
```

**User rows are never touched** — they are the administrator's own words, and flagging them reports the caller to themselves. `_replayed_findings`' existing flagging via the `ai.history` pseudo-tool **continues unchanged for both modes**: the flag is the record that it happened, the stub is the neutralization; the stub is *in addition to* the flag, not instead of it.

**`ai_session.mode` is fixed at creation** (§0.8): there is no promotion path, because promoting a session imports a whole read-mode transcript — assembled under read-mode replay rules — into a context that can now propose. Creating `mode='act'` requires `ai.act.propose`.

**Do not wrap replayed answers in the untrusted envelope** (§0.8's rejected alternative, and §17.11's objection stands): wrapping a model's own prior answer contradicts the role it is replayed in and teaches the model the tags are noise. **Do not summarize prior turns with a second model call.**

### Tests shipped

1. §13 test 14, **both halves in one file with different observable values** (constraint 4): a prior assistant answer containing flagged text is **replaced by the stub** in an act-capable session, and **replayed verbatim** in a read session. Assert on the messages handed to the provider (the `ScriptedProvider` records them), not on the answer.
2. A prior **user** row containing the same text is replayed verbatim in **both** modes.
3. The `ai.history` flag still fires in both modes — the stub did not swallow the record.
4. §13 test 15 — a session created `mode='read'` cannot be promoted: no route accepts a mode change, and a direct `UPDATE` through the service raises. Assert the stored `mode` afterwards.
5. Creating `mode='act'` without `ai.act.propose` → 403, and **no session row is written**.

### Falsifying mutations

- **Apply the stub in both modes.** Test 1's read half observes the stub where it expects the original text. Red; restore.
- **Apply it to user rows too.** Test 2 observes the administrator's own words withheld. Red; restore.
- **Drop the `ai.history` flag when the stub fires.** Test 3 observes no flag — the record that it happened is gone while the mitigation looks fine, which is the worse of the two failures. Red; restore.

**Definition of done:** flagged assistant history neutralized in act-capable sessions and verbatim in read sessions, proved by different observed values; user rows untouched; flagging unchanged; mode fixed at creation and gated; `docs/modules/ai-admin/spec.md` §17.11 amended in Task 16 to point here.

**Commit:** `feat(ai): laundered history is neutralized once a turn can propose`

---

# Phase D — the three personas (Tasks 11–13)

---

## Task 11 — The admin persona gains act mode, and the boundary guard is re-aimed

**Files:**
- Modify: `backend/app/modules/ai_admin/{profile,api,schema}.py`
- Test: modify `backend/tests/test_ai_admin_boundaries.py`; create `backend/tests/test_ai_act_api.py`

### What to build

`ADMIN_PROFILE` gains `may_propose=True` and `replay_policy="neutralize_flagged"`, and its tool set gains `ActTool` over rows carrying `'admin'` in `personas`. §12's routes:

| Method | Path | Permission |
|---|---|---|
| `POST` | `/ai/sessions` (gains `mode`, `persona`) | `ai.use` (+ `ai.act.propose` for `mode='act'`) |
| `GET` | `/ai/actions`, `/ai/actions/{id}` | `ai.use` |
| `POST` | `/ai/actions/{id}/confirm` | `ai.act.confirm` + §9.2's second part |
| `POST` | `/ai/actions/{id}/reject` | `ai.act.propose` |

Route handlers **delegate**: they resolve the principal, call `app.core.ai.act`, and map errors to status codes. No proposal logic, no digest arithmetic, no `JobEngine` call in `api.py`.

### The §15.1 obligation — the boundary guard changes shape, and does not shrink

`tests/test_ai_admin_boundaries.py` was rewritten in `6d57334` to ask a question that is still open: **act mode belongs to the shared layer, not to this module.** That guarantee survives this task; what changes is what "not grown here" can mean now that the module legitimately has act *routes*.

- **`test_no_source_file_defines_an_act_mode_name` will go red**, and correctly: an `ai_action_id: UUID` route parameter is an `ast.arg` containing `ai_action`. **Narrow it, do not delete it.** The names that must still not be *defined* in a persona package: `ActTool`, `AiAction`, `AiAutonomyCeiling`, `preview_digest`, `effective_autonomy`, `fuse_breach`. The names that are now legitimate as *route parameters and response fields*: `ai_action_id`, `action_key`. Match on definitions (`ClassDef`/`FunctionDef`/module-level `Store`), **not** on `ast.arg`, and say in the docstring why the exemption is exactly that shape.
- **`test_act_mode_is_not_grown_inside_this_module`** keeps its name and its argument. Its `ReadTool.method` assertion re-points to `app.core.ai.types` (done in Task 4). Its `\bmutating\s*=\s*True\b` regex must now be scoped to **the persona package**, where it is still forbidden — `ActTool` sets it, and `ActTool` lives in `app/core/ai/act.py`. Add the new clause that carries the real guarantee: **the persona package calls no provider, runs no turn loop, and writes no `ai.*` audit event** — assert on imports and on call sites (`audit_service.record` appears nowhere under the package), which is the boundary §0.1 draws in one sentence.
- **`test_the_only_persona_read_mode_writes_is_admin`**'s premise is gone (personas exist now). Keep the file's coverage by rewriting it as: **this package writes only `persona="admin"`**, and the other two personas' packages write only their own. Derive the package list from `PERSONAS`.

### Tests shipped

1. Every route's RBAC, derived from the router via `route_permission` (constraint 18), including that `/ai/actions/{id}/confirm` is gated `ai.act.confirm`.
2. `POST /ai/sessions` with `mode='act'` requires `ai.act.propose` (Task 10 test 5 asserts the negative; assert the positive here).
3. A full propose→preview→confirm→dispatch round trip through the HTTP surface, asserting one `Job`, one `ai.action_proposed`, one `ai.action_confirmed`, one `ai.action_dispatched`.
4. Reject: `POST /reject` with a bounded reason → `state='rejected'`, `ai.action_rejected`, no job.
5. The three amended boundary tests, each with its own falsifying mutation below.

### Falsifying mutations

- **Build the job params inside `api.py`** instead of calling `core.ai.act`. **The amended boundary test observes the persona package constructing an effect.** Red; restore. *This is the mutation §0.1's whole relocation exists to make catchable.*
- **Add `audit_service.record("ai.action_dispatched", ...)` to `api.py`.** The new clause observes an `ai.*` audit write in a persona package. Red; restore.
- **Restore the old `_ACT_MODE_NAMES` list unchanged.** It goes red on `ai_action_id` — paste it, and note in your report that this is the constraint-10 failure mode: a guard matched against mentions pushes the next reader to delete the guarantee rather than respect it.

**Definition of done:** the admin persona proposes, previews, confirms and rejects over HTTP; every route's gate derived from the router; the three boundary tests amended in shape with their guarantees intact and each independently falsified; the §15.1 obligation discharged and recorded.

**Commit:** `feat(ai-admin): the admin persona gets act mode, from the shared layer`

---

## Task 12 — The technician copilot

**Read §0.3, §3.2, §9.2.**

**Files:**
- Create: `backend/app/modules/ai_copilot/{__init__,profile,tools,api,schema}.py`
- Modify: `backend/app/modules/__init__.py`
- Test: `backend/tests/test_ai_copilot.py`

### What to build

A **new persona package**, not an addition to `service_desk`. Reason: `service_desk` is the ticket core, and making it import `app.core.ai` puts an AI dependency in the module every other service-desk feature builds on. It mounts `/api/v1/service-desk/tickets/{ticket_id}/ai` (session create + turn), gated on `ticket.read` **plus** `ai.use`; the profile does the rest.

**Principal:** the technician's own `AuthContext`, through the staff loopback executor with their own bearer token. `require_permission` and RLS run verbatim; the copilot has no power the technician does not.

**May read:** the admin tool set intersected with what the technician's grants actually permit, plus the ticket, its full thread including internal notes (they hold `ticket.read`), and the linked device's state where `ticket.device_id` is set and `devices.read` is held.

**May propose:** an `ai_action` **bounded to `ticket.device_id`**. No linked device → the copilot cannot propose and says so. Letting the model choose an arbitrary device would make "the ticket" decorative.

**May never** (§0.3): post a comment, set an assignment, change a status or priority, resolve or close. **Drafts** — a suggested public reply, internal note, category/queue/priority — are returned to the technician's editor and never written. Do **not** invent an `author_type='ai'` comment kind: that is this layer defining shared ticket structure, which the ticket core's modules are forbidden from doing, and it re-opens `sla` §0.6.1 for a third author type.

### Tests shipped

1. §13 test 2 — proposing against a device the ticket does not link → refused; proposing against `ticket.device_id` → previewed. **Different observable values in one test.**
2. A ticket with `device_id IS NULL` → the copilot cannot propose, and the refusal names the reason.
3. A catalogue row lacking `'technician'` in `personas` → refused, even when the same row would be proposable by the admin persona (assert both in one test — otherwise the persona filter is untested).
4. **The no-write guarantee, structurally:** the package's router registers no route that mutates a ticket, and the package imports no ticket write service (`add_comment`, `change_status`, `update_ticket`) — asserted by AST over imports and by walking the flattened router for non-GET routes other than the two AI ones. *Matched against imports and route methods, not against a string ban (constraint 10) — the docstrings explaining why the copilot does not post must survive.*
5. A draft response carries the suggested text and **no `ticket_comment` row is created** — assert the count before and after.
6. The tool set is a subset of the admin set: `set(COPILOT_TOOLS) <= set(ADMIN_TOOLS)`, and it is non-empty (a vacuous subset assertion passes on `{}`).

### Falsifying mutations

- **Drop the `device_id == ticket.device_id` check.** Test 1 observes a preview against an unrelated device. Red; restore.
- **Import `add_comment` and post the draft.** Test 4 observes the import and test 5 observes the row. Red; restore.
- **Widen the tool set with a tool the admin set lacks.** Test 6 red; restore.

**Definition of done:** the copilot answers and drafts inside a ticket, proposes only against the ticket's linked device, and cannot write to a ticket by construction; three mutations pasted.

**Commit:** `feat(ai-copilot): the technician persona drafts, and never posts`

---

## Task 13 — The end-user resolver, and its own executor

**Read §0.2, §0.9, §3.1, §11.1, §11.2. This is the persona that reads the most attacker-controlled text on the platform and has the least reach; the inversion is the design.**

**Files:**
- Create: `backend/app/core/ai/executor_portal.py`, `backend/app/modules/ai_portal/{__init__,profile,tools,api,schema}.py`
- Modify: `backend/app/modules/__init__.py`, `backend/tests/test_ai_boundaries.py` (the client-site equality goes to **three**)
- Test: `backend/tests/test_ai_portal.py`

### What to build

**A second executor, not a branch** (§0.2). The resolver serves a `PortalContext`, which is deliberately **not** an `AuthContext`, and portal reads run under `portal_session` with both GUCs pinned. Handing the staff executor a portal principal's token means every staff route 403s — and the "fix" for that is a service token, which is the privilege-escalation primitive the staff executor's own docstring warns about. **Two executors, two types, no branch:** the portal executor cannot construct a staff request because it has no `AuthContext` to build one from.

**A decision the spec leaves under-determined, made here and reported.** §0.2 says the resolver's tools are "the portal's existing routes, invoked in-process under `portal_session` with the requester pinned", while §12 gates the surface with `require_portal_permission`. Those describe two different mechanisms. **This plan chooses the loopback**, matching the staff executor: `httpx.ASGITransport` against the portal's own routes carrying the portal principal's own bearer token, so `resolve_portal_principal` and `require_portal_permission` **re-run verbatim** and `portal_session` is opened by the route, not by the AI layer. Direct service calls would make this layer decide which GUCs get pinned, and `portal_session`'s fail-closed property is only fail-closed if both are.

**Verify the premise before you build on it** (constraint 2): v1 portal principals authenticate through `auth_context`, so a bearer token exists. `app/modules/portal/principal.py`'s own docstring says external end users will need a **second implementation** of `resolve_portal_principal` — if that implementation lands without a bearer token, the loopback stops working and the correct move is to report it, not to mint a token for the AI layer.

**Tool set** (§3.1): the requester's own ticket list, one ticket's detail, that ticket's **public** thread (the portal's routes already exclude internal notes and this persona gets no route the portal UI does not have), and the org's ticket categories. Nothing else.

**`may_propose=False`, always** (§0.9) — and structurally, not by a flag alone: the profile carries no `ActTool`, and `ai_autonomy_ceiling`'s Task-1 CHECK makes an `end_user` row above `off` unstorable.

**The two scope cuts, made testable.** The profile's tool set contains **no device tool and no documentation tool**. See "Two things the spec verified do not exist" above: a builder who wants either has hit §11.1 or §11.2 and must stop and report.

### Tests shipped

1. §13 test 18 — **the portal AI surface registers no action route**, derived from the flattened router (constraint 17 — a flattener that misses `include_router`'d routes makes this vacuous, so assert first that the flattener found the two routes that *do* exist).
2. `app/modules/ai_portal/` imports neither `executor_staff` nor anything under `app/api/deps.py` — AST.
3. A requester cannot read another requester's ticket through the resolver: seed two requesters' tickets, drive a turn, assert the loopback returned only the caller's. **Structurally impossible via RLS, so assert it on the returned payload**, and add an admin-engine test that the portal executor pins **both** GUCs (drop `app.current_requester` and assert the read returns nothing — fail-closed, not fail-open).
4. The tool set contains no device, alert, job, inventory, patch, credential, audit or documentation tool — derived by matching each tool's `path` against those prefixes, with a non-vacuity assertion that the tool set is non-empty.
5. `may_propose is False` on the registered profile, **and** an attempt to invoke `actions.propose` through the resolver's executor is an `unknown_tool` error with **zero HTTP requests on the spy**.
6. An `ai_autonomy_ceiling` row for `end_user` above `off` is unstorable (Task 1's CHECK, re-asserted here where the reader is looking).
7. The client-site equality in `tests/test_ai_boundaries.py` is now exactly three, named.

### Falsifying mutations

- **Give the profile the staff executor.** Test 2 observes the import; test 3 observes staff rows. Red; restore.
- **Add a `devices.list` tool to the profile.** Test 4 red; restore. *This is the §11.1 guard: without it, the next builder resolves "the requester's laptop" from `ticket.device_id`, which an unauthenticated email can set.*
- **Add an `/ai/actions/{id}/confirm` route to the portal router.** Test 1 red; restore.
- **Pin only `app.current_org` in the portal executor's session.** Test 3's fail-closed half observes another requester's rows. Red; restore.

**Definition of done:** the resolver answers about the requester's own tickets and nothing else; a second executor with no branch and no staff type in reach; no action route on the surface, router-derived; both scope cuts asserted off the profile; the client-site equality moved to three deliberately; the loopback decision and its premise recorded in your report.

**Commit:** `feat(ai-portal): the tier-1 resolver, weak on purpose`

---

# Phase E — proof and surface (Tasks 14–16)

---

## Task 14 — The act-mode attack suite and the structural sweep

**Files:**
- Test: `backend/tests/test_ai_act_injection.py`, `backend/tests/test_ai_act_structure.py`

### What to build

Nothing. This task ships only tests, and it lands last among the backend tasks because two of them need a persona that did not exist before Task 12.

**`test_ai_act_injection.py`:**

1. **§13 test 13 — the named attack, extended.** The hostile line arrives through the **real unauthenticated email-intake ingress**, by borrowing that suite's own fixture exactly as `test_ai_admin_boundaries.py` does today (`from tests import test_email_intake_intake as _email_intake_suite`; `email_intake_env = _email_intake_suite.email_env`). A copilot turn reads it. A **completely persuaded** `ScriptedProvider` emits `actions.propose` with a plausible key. Assertions: **no dispatched job** (`select(count()).select_from(Job)` unchanged), at most a **flagged, tainted** `ai_action` in `previewed`, and the turn still **answers** — refusing would hand the attacker a denial of service, which §8.4 forbids as firmly as it forbids the fetch. *A fake that only ever emits legal calls cannot see a missing guard; the model here must obey the injection.*
2. The same attack against the **resolver**: zero `ai_action` rows, zero jobs, `unknown_tool` on the spy.
3. A laundered instruction that reaches the model through **replayed history** (Task 10's path) in an act session produces no proposal, because the text was replaced by the stub — assert the provider's received messages, then the row count.

**`test_ai_act_structure.py`:**

4. §13 test 19 — `app/core/ai/` imports no `app.modules.*` (re-asserted here so deleting the boundary file does not delete the property).
5. §13 test 20 — the client-site count, across core and every registered persona package.
6. **§13 test 22 — no `ai.*` audit metadata anywhere contains `rationale`, question text or answer text, asserted over the metadata rather than over one call site.** The shape that is not vacuous: drive a real turn and a real proposal whose question, answer and rationale each carry a **distinctive sentinel string**, then `SELECT` every `audit_events` row for the org and assert no sentinel appears in any `meta`. A grep over call sites is a mention test and proves nothing about a metadata dict assembled elsewhere.
7. **Nothing in this layer reaches a device except through `JobEngine.create()`**: `app/core/ai/` and every persona package name `publish_job` in at most one file, and no `publish_job` call sits inside an `async with org_session` block (health's `tests/test_health_egress.py` pins both halves this way — copy its shape).
8. Non-vacuity guards on every scan in the file (file count floors, non-empty persona list).

### Falsifying mutations

- **Write `rationale` into `ai.action_proposed`'s metadata.** Test 6 observes the sentinel in a stored audit row. Red; restore. *§8.3: the audit chain is where an attacker who can write a ticket body would most like their text to end up permanently.*
- **Let the copilot dispatch on propose** (call `confirm_action` from `propose`). Test 1 observes a job. Red; restore.
- **Move a `publish_job` call inside `org_session`.** Test 7 red; restore.

**Definition of done:** §13 tests 13, 16, 19, 20, 21, 22 green, each with a stated observation-if-deleted; the sentinel-based audit test demonstrated by mutation; the egress structure pinned.

**Commit:** `test(ai): the act-mode attack, through the ingress an internet sender can reach`

---

## Task 15 — Frontend

**Files:**
- Create: `frontend/src/modules/ai-automation/{api,types,ActionPreview,ConfirmDialog,AutonomyDialPage}.{ts,tsx}`; `frontend/src/modules/service-desk/TicketCopilotPanel.tsx`; `frontend/src/modules/portal/PortalAssistantPanel.tsx`
- Modify: `frontend/src/modules/ai-admin/ChatPanel.tsx` (render proposals), the two nav files
- Test: `frontend/src/modules/ai-automation/__tests__/`

### What to build

**§6.3's layout is a security property, specified here rather than left to the frontend.** The confirmation screen has two regions and they may not merge:

- **What will happen** — rendered **only** from the confirm payload's catalogue-derived fields (`name`, `description`, `key`, `job_type`, redacted `rendered_params`, script name+version, target hostname+id, `risk_class`, `destructive`, `effective_autonomy`, `tainted`, the fuse verdict).
- **Why it was suggested** — the model's `rationale`, in a visually distinct, clearly-labelled region, rendered as **text, never as markup**, and never used to compose the first region.

A UI that lets the second region describe the first re-introduces the whole injection problem at the confirmation step: a laundered instruction rendering as an authoritative action description is a confirmation the human did not really give.

Also: the taint badge, the expiry countdown, a 409 that names what changed and offers "re-preview", a 410 that says the preview expired. The autonomy dial page (`ai.admin`) renders the persona × risk-class grid with `off` as the default for a missing row and **no `autonomous` option in the control at all**.

The resolver is placed **in-ticket only** (§11.3: the portal has no "search before you file" step, so there is nowhere to stand for pre-filing deflection — do not invent one).

### Tests shipped (vitest)

1. A `rationale` containing an authoritative-sounding action description (`"This will safely clear the print queue."`) and markup does **not** appear in the action-description region, and appears escaped in the rationale region. **Assert the two regions' text separately** — asserting only "the rationale is displayed" passes against a UI that renders it as the description.
2. The action description region's text is a function of the `action.*` fields only: change a field, the region changes; change `rationale`, it does not.
3. `tainted: true` renders the badge; `false` does not.
4. The dial control offers exactly `off | suggest | require_approval`.
5. `npx tsc --noEmit` clean; existing frontend suite not regressed.

**Definition of done:** preview/confirm/reject and the dial usable end to end; the two-region separation asserted by a test that would fail if they merged; `npx tsc --noEmit` clean.

**Commit:** `feat(ai): the confirmation screen, whose layout is a security property`

---

## Task 16 — Documentation, spec amendments, and the live smoke test

**Files:** `docs/modules/ai-automation.md`, `docs/modules/ai-admin/spec.md`, `docs/06-roadmap.md`, `docs/modules/endpoint-health-alerting/spec.md`, `README.md` if it enumerates modules.

- [ ] **§15's three consequences, discharged**
  1. `tests/test_ai_admin_boundaries.py`'s act-mode guard — **already replaced** in `6d57334` and **re-aimed in Task 11**. Record in the spec what it now asserts, so the next reader does not re-derive it.
  2. `docs/modules/ai-admin/spec.md` §12 points here for the substrate rather than describing it as unphased; §17.11's "this reasoning expires with read mode" is discharged by §0.8/§7.4 and says so, naming Task 10.
  3. `docs/06-roadmap.md`'s Phase 17 status note is rewritten from "act mode is blocked on a document that does not exist" to what is now true, and the AI Automation layer is **given a phase number**.

- [ ] **Record every decision and deviation this plan made.** Each one is a place where the spec was silent or where the code disagreed, and each must land in `docs/modules/ai-automation.md` rather than in a commit message:
  - the **fourth kernel change** — `fuse_breach` lifted into `app/core/approved_actions.py` (Task 3), added to §10;
  - the **`end_user` CHECK** on `ai_autonomy_ceiling` (Task 1), added to §4.1;
  - `action_expiry_s` and `max_proposals_per_turn` **on `ai_settings`** (Task 6), added to §6.4/§6.5;
  - **two migrations, not one** (§16 says one): kernel state and the instance table land separately so each task can be green on its own, and forward-only means a later column need is a new migration either way;
  - the **four AI models moving to `app/models/ai.py`** (Task 4), added to §2.1's table, which omits them;
  - the **portal executor's loopback** choice and its dependence on portal principals carrying a bearer token (Task 13), added to §0.2;
  - the **permission grant decision** for `ai.act.propose` (Task 1);
  - the **two scope cuts** (§11.1, §11.2) as tested properties rather than intentions.

- [ ] **The health finding.** Write §0.11's finding into `docs/modules/endpoint-health-alerting/spec.md` as a recorded, unfixed defect with the fix named: `POST /health/alerts/{id}/remediate` should require `dispatch_permission_for(resolved job_type)` in addition to `health.remediate`, now that `app/jobs/dispatch_permissions.py` exists; it is a service-layer check because the required permission depends on the request body; it changes health's tests, so it is that module's own cycle. **Do not implement it here.**

- [ ] **The live smoke test.** With a real device and a real `approved_action` row carrying `'admin'` in `personas`: open an act session, ask a question that leads to a proposal, read the preview, confirm it, watch the job dispatch and settle, and check the five audit events. Then lower the dial to `off` and watch the same request refuse. Paste both.

- [ ] **Record what is still deferred** (§14) and what remains blocked (§11), so the next reader does not have to re-derive them.

**Commit:** `docs(ai): what the automation layer changed elsewhere`

---

## Self-review

Before reporting the plan complete, confirm each:

- [ ] The full backend suite is green and no lower than the recorded baseline.
- [ ] Every task's falsifying mutations were **run** and their output **pasted** — not reasoned about.
- [ ] No guard test asserts "no exception was raised", a set defined in its own file, or an empty collection that was always going to be empty (constraint 4).
- [ ] Every new table's tenancy was falsified with **both** `USING (true)` and `WITH CHECK (true)` (constraint 6).
- [ ] Every new index is declared on the model **and** in the migration, predicate included (constraint 8).
- [ ] `app/core/ai/` imports no `app.modules.*` package, and no persona package constructs an HTTP client (§2.3).
- [ ] The confirm handler reads the stored row and nothing else, and the spec's §13 test 8 mutation was demonstrated.
- [ ] No `ai.*` audit metadata carries prose — proved by sentinel, not by grep.
- [ ] `ai_autonomy_ceiling` has no `'autonomous'` value, and `end_user` cannot be raised above `off` — both by database CHECK.
- [ ] Nothing in this layer reaches a device except through `JobEngine.create()`, and nothing publishes before its transaction commits.
- [ ] The agent was not touched, no job type was registered, no `approved_action` column changed, and no migration seeded a `personas` value.
