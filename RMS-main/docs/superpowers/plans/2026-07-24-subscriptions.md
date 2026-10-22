# Subscriptions (SaaS Management) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Subscriptions / SaaS-management module — a registry of recurring subscriptions with seat assignment, cost normalization, change-history events, and notice-deadline reporting.

**Architecture:** Control-plane-only module mirroring `app.modules.change_mgmt` / `app.modules.licenses` (models → schema → errors → service → reporting → api → registration). **No worker.** Reuses the shared `vendor` entity and links advisorily to `license` (both from Phase 11).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async/asyncpg), Alembic, Pydantic v2; React+TS+Vite+vitest frontend.

## Global Constraints

- **Spec:** `docs/modules/subscriptions/spec.md` is authoritative. Read the relevant section before each task.
- **Control-plane only, NO worker:** no agent capability, no job types, no gateway edit, no Celery task, no beat entry, no new compose service, no new env var. `register(app)` mounts the router only.
- **Migration** is the next in sequence — resolve the head with `alembic heads` in Task 1 (expected `0019`); never hard-code a revision elsewhere. Downgrade uses valid Postgres only (`DROP TABLE IF EXISTS ... CASCADE`; `DROP INDEX IF EXISTS <name>` — no MySQL `... ON table`).
- **Tenancy (invariant #5):** every table RLS `ENABLE + FORCE` + `tenant_isolation` (USING + WITH CHECK, `org_id = current_setting('app.current_org', true)::uuid`); every query carries an explicit `org_id` predicate; every cross-reference (`vendor_id`, `owner_user_id`, `license_ref`) resolved `WHERE org_id AND id`, cross-org rejected as `SubscriptionValidationError` (indistinguishable from nonexistent). At least one RLS-bypassing admin-session load-bearing test, mutation-verified (Phase-10/11 lesson).
- **RBAC (invariant #6):** two perms `subscriptions.read` / `subscriptions.manage`. Migration grants: super + org-admin = both; **technician = read + manage**; auditor + read-only = read. `app/core/rbac.py` `PERMISSIONS` gains both AND `tests/test_rbac.py` + `tests/test_api_routes.py` perm-sets updated in Task 9 (Phase-7 lesson).
- **Audit (invariant #4):** subscription create/update/cancel/renew, seat assign/release, CSV export audited (`actor_type="user"`, `target_type` `subscription`/`seat_assignment`). **No-audit-on-view** for lists/detail/events/spend/renewal/idle reports. `subscription_event` is business change-history (written on mutation), NOT the audit log; reading it writes nothing.
- **Errors:** `SubscriptionNotFoundError(SubscriptionValidationError)`; API maps NotFound→404, other validation→400 by `isinstance`, never message-string, never 500 (Phase-9 lesson). `currency` length-constrained to 3 at the schema boundary (Phase-11 lesson).
- **Currency:** per-record `cost_per_cycle` + `currency` (ISO-4217); no FX; roll-ups group/sum by currency.
- **Frontend:** vocab strings sourced verbatim from backend (Phase-5 lesson); strict TS, no `any`. **Wire seat assign/release actions** — do NOT ship a read-only seats panel (Phase-11 AllocationsPanel lesson).
- **Verification:** every task ends `.venv/bin/python -m pytest` (task tests) + `.venv/bin/mypy <changed>` + `.venv/bin/ruff check <changed>` clean; Task 9 runs the FULL backend suite; Task 10 runs `npm test`/`typecheck`/`build`. One commit per task.

---

### Task 1: Migration — three tables, RLS, RBAC

**Files:** Create `backend/migrations/versions/0019_subscriptions.py`; Test `backend/tests/test_subscriptions_migration.py`.

**Interfaces:** Produces tables `subscription`, `seat_assignment`, `subscription_event` (columns per spec §2), each RLS ENABLE+FORCE + `tenant_isolation` (copy `_rls` from `0018_licenses.py`); seeds perms `subscriptions.read`/`subscriptions.manage` + grants (super+org-admin=both; technician=read,manage; auditor+read-only=read) — mirror `0018_licenses.py` exactly.

- [ ] **Step 1: Write the failing test** — mirror `test_licenses_migration.py`: assert the 3 tables exist, each has `relforcerowsecurity` + `tenant_isolation` policy; the 2 perms exist; grants match (technician read+manage). Assert head `>= "0019"` lexically (not pinned).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** `0019_subscriptions.py`. `down_revision` = current head (`.venv/bin/alembic heads`; expected `0018`). `Numeric(14,2)` cost; `JSONB` `details`; FK `vendor_id`→vendor (nullable), `owner_user_id`→users (nullable), `license_ref` plain UUID nullable (NO FK — advisory, org-validated in service). `_rls` each table. Seed perms+grants. Downgrade drops in FK-safe order with `IF EXISTS`.
- [ ] **Step 4:** Run → PASS; `alembic upgrade head && downgrade -1 && upgrade head` clean.
- [ ] **Step 5: Commit** `feat(subscriptions): migration 0019 — subscription/seat/event + RLS + RBAC`.

---

### Task 2: ORM models

**Files:** Create `backend/app/modules/subscriptions/__init__.py` (minimal, no register yet), `backend/app/modules/subscriptions/models.py`; Modify `backend/app/models/__init__.py` (re-export); Test `backend/tests/test_subscriptions_models.py`.

**Interfaces:** Produces `Subscription`, `SeatAssignment`, `SubscriptionEvent` (typed `Mapped[...]`, columns exactly matching migration 0019). `Subscription.cost_per_cycle: Mapped[Decimal]`; `currency: Mapped[str]`; `license_ref: Mapped[UUID | None]` (plain, no FK); `details: Mapped[dict[str, Any]]` JSONB default dict. Re-exported from `app.models`.

- [ ] **Step 1: Write the failing test** — insert one row of each, read back, assert column parity (Decimal cost roundtrip, `license_ref=None` default, `auto_renew=False`, `details={}`). Mirror `test_licenses_models.py`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** mirroring `app/modules/licenses/models.py`. Re-export all 3 names in `app/models/__init__.py`.
- [ ] **Step 4:** PASS; mypy + ruff clean.
- [ ] **Step 5: Commit** `feat(subscriptions): ORM models`.

---

### Task 3: Schemas + typed errors

**Files:** Create `backend/app/modules/subscriptions/errors.py`, `backend/app/modules/subscriptions/schema.py`; Test `backend/tests/test_subscriptions_schema.py`.

**Interfaces:**
- `errors.py`: `SubscriptionValidationError(Exception)`, `SubscriptionNotFoundError(SubscriptionValidationError)`.
- `schema.py` (mirror `licenses/schema.py`): module-level `BillingCycle = Literal["monthly","quarterly","annual"]`, `SubStatus = Literal["active","trial","cancelled"]`; `_Currency = Field(default=None, min_length=3, max_length=3)` for optional currency, or `Field(min_length=3, max_length=3)` where required.
  - `SubscriptionCreate` (vendor_id: UUID|None, service_name: str, plan: str="", category: str="", billing_cycle: BillingCycle, cost_per_cycle: Decimal, currency: str [len 3], seats: int=0, owner_user_id: UUID|None=None, started_at: date|None=None, renews_at: date|None=None, auto_renew: bool=False, notice_days: int=0, payment_ref: str|None=None, license_ref: UUID|None=None, status: SubStatus="active"), `SubscriptionUpdate` (all optional, same Literals/constraints), `SubscriptionOut` (all fields).
  - `SeatAssignCreate` (assignee_name: str, assignee_email: str), `SeatAssignmentOut`.
  - `EventOut` (id, subscription_id, type, details, created_at).
  - Report outs: `SpendRow` (currency, monthly_total: Decimal, annual_total: Decimal, plus a `group_key`/`group_type` for vendor/category/owner roll-ups — or separate `SpendByGroup`), `RenewalRow` (subscription_id, service_name, renews_at, notice_deadline, days_to_notice, days_to_renewal, action_required: bool), `IdleSeatRow` (subscription_id, service_name, seats, assigned, idle, currency, idle_monthly_cost: Decimal).

- [ ] **Step 1: Write the failing test** — each Literal (billing_cycle, status) rejects out-of-domain; `currency` rejects a >3-char value (422); `SubscriptionNotFoundError` subclasses `SubscriptionValidationError`; a valid `SubscriptionCreate` constructs with `cost_per_cycle=Decimal("9.99")`, `currency="USD"`. Add `__test__=False` to any `Test*`-named class (none expected).
- [ ] **Step 2:** FAIL → **Step 3:** Implement → **Step 4:** PASS; mypy+ruff clean.
- [ ] **Step 5: Commit** `feat(subscriptions): API schemas + typed errors`.

---

### Task 4: Cost normalization helpers

**Files:** Create `backend/app/modules/subscriptions/costs.py`; Test `backend/tests/test_subscriptions_costs.py`.

**Interfaces:** Pure, currency-agnostic (no DB):
- `_CYCLE_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}`.
- `def monthly_equivalent(cost_per_cycle: Decimal, billing_cycle: str) -> Decimal` = `(cost / _CYCLE_MONTHS[cycle]).quantize(Decimal("0.01"), ROUND_HALF_UP)`; unknown cycle → `SubscriptionValidationError`.
- `def annualized(cost_per_cycle: Decimal, billing_cycle: str) -> Decimal` = `(cost * (12 // _CYCLE_MONTHS[cycle]))` quantized to 2dp.
- `def advance_renewal(current: date, billing_cycle: str) -> date` — advance by the cycle's months with month-end clamp (e.g. Jan 31 + 1mo → Feb 28/29). Use a small manual month-add (no external dateutil — check if the repo already vendors one; if not, implement clamp inline).

- [ ] **Step 1: Write the failing test** — `monthly_equivalent(Decimal("120"),"annual")==Decimal("10.00")`, `(Decimal("30"),"quarterly")==Decimal("10.00")`, `(Decimal("10"),"monthly")==Decimal("10.00")`; `annualized(Decimal("10"),"monthly")==Decimal("120")`, `(Decimal("30"),"quarterly")==Decimal("120")`; unknown cycle raises; `advance_renewal(date(2026,1,31),"monthly")==date(2026,2,28)`, `advance_renewal(date(2026,1,15),"annual")==date(2027,1,15)`, `advance_renewal(date(2026,1,15),"quarterly")==date(2026,4,15)`.
- [ ] **Step 2:** FAIL → **Step 3:** Implement → **Step 4:** PASS; mypy+ruff clean.
- [ ] **Step 5: Commit** `feat(subscriptions): cost normalization + renewal-date helpers`.

---

### Task 5: Subscription service — CRUD + change-history events

**Files:** Create `backend/app/modules/subscriptions/service.py`; Test `backend/tests/test_subscriptions_service.py`.

**Interfaces:** Consumes `app.models` (`Subscription`, `SubscriptionEvent`, `Vendor`, `User`, `License`), `errors`. Produces (mirror `licenses/service.py` org-scoped-load + audit + flush style):
- `_load_subscription(session, org_id, sub_id) -> Subscription` (None → `SubscriptionNotFoundError`).
- `_validate_refs(session, org_id, *, vendor_id, owner_user_id, license_ref)` — each non-None resolved `WHERE org_id AND id` against `Vendor`/`User`/`License`; cross-org/unknown → `SubscriptionValidationError`.
- `_record_event(session, org_id, actor_id, sub_id, type_, details)` — insert a `SubscriptionEvent` (helper; NO audit — events are business history).
- `create_subscription(session, *, org_id, actor_id, **fields) -> Subscription` — validate refs; validate `billing_cycle`/`status` allow-lists; insert; `_record_event(..., "created", {...})`; audit `subscription.created`.
- `update_subscription(session, *, org_id, actor_id, sub_id, **fields) -> Subscription` — `_load`; re-validate refs if provided; apply allow-list; **detect changes**: `cost_per_cycle` → event `price_change` `{from,to,currency}`; `seats` → `seat_change` `{from,to}`; `plan` → `plan_change` `{from,to}`; audit `subscription.updated`.
- `get_subscription`/`list_subscriptions`/`list_events(session, org_id, sub_id)` — org-scoped, NO audit.

- [ ] **Step 1: Write the failing test** — create → `created` event recorded + `subscription.created` audit; update cost → `price_change` event with `{from,to,currency}` + no event for unchanged fields; update seats → `seat_change`; update plan → `plan_change`; cross-org `vendor_id`/`owner_user_id`/`license_ref` → `SubscriptionValidationError` (+ RLS-bypassing admin-session load-bearing variant on one ref, mutation-verified); missing sub → `SubscriptionNotFoundError`; list/get/list_events no audit; tenancy.
- [ ] **Step 2:** FAIL → **Step 3:** Implement → **Step 4:** PASS; mypy+ruff clean.
- [ ] **Step 5: Commit** `feat(subscriptions): subscription service — CRUD + change-history events`.

---

### Task 6: Lifecycle — cancel + record_renewal

**Files:** Modify `backend/app/modules/subscriptions/service.py`; Test `backend/tests/test_subscriptions_lifecycle.py`.

**Interfaces:** Consumes `costs.advance_renewal`. Produces:
- `cancel_subscription(session, *, org_id, actor_id, sub_id) -> Subscription` — `_load`; if already `cancelled` → `SubscriptionValidationError`; set `status="cancelled"`; `_record_event(..., "cancel", {})`; audit `subscription.cancelled`.
- `record_renewal(session, *, org_id, actor_id, sub_id) -> Subscription` — `_load`; if `renews_at` is None → `SubscriptionValidationError("no renewal date")`; `sub.renews_at = advance_renewal(sub.renews_at, sub.billing_cycle)`; `_record_event(..., "renewal", {"renewed_to": <iso>})`; audit `subscription.renewed`.

- [ ] **Step 1: Write the failing test** — cancel sets status + `cancel` event + audit; double-cancel → `SubscriptionValidationError`; `record_renewal` advances `renews_at` by the cycle (assert exact date incl. month-end clamp) + `renewal` event + audit; renewal on a None-date sub → `SubscriptionValidationError`; tenancy (cross-org → `SubscriptionNotFoundError`).
- [ ] **Step 2:** FAIL → **Step 3:** Implement → **Step 4:** PASS; mypy+ruff clean.
- [ ] **Step 5: Commit** `feat(subscriptions): cancel + record-renewal lifecycle actions`.

---

### Task 7: Seat assignment service

**Files:** Modify `backend/app/modules/subscriptions/service.py`; Test `backend/tests/test_subscriptions_seats.py`.

**Interfaces:** Produces:
- `assign_seat(session, *, org_id, actor_id, sub_id, assignee_name, assignee_email) -> SeatAssignment` — `_load`; enforce pool `active_count < sub.seats` else `SubscriptionValidationError("no seats available")`; insert; `_record_event(..., "seat_change", {"action":"assigned","email":email})`; audit `subscription.seat_assigned`.
- `release_seat(session, *, org_id, actor_id, assignment_id) -> SeatAssignment` — org-scoped load (None → `SubscriptionNotFoundError`); already released → `SubscriptionValidationError`; `released_at = now`; `_record_event(..., "seat_change", {"action":"released","email":email})`; audit `subscription.seat_released`.
- `list_seats(session, org_id, sub_id) -> list[SeatAssignment]`; `available_seats(session, org_id, subscription) -> int` (seats − active) — NO audit.

- [ ] **Step 1: Write the failing test** — assign to the cap (seats=2) then the 3rd → `SubscriptionValidationError`; `available_seats` 2→1→0; release frees a slot (can assign again); double-release → `SubscriptionValidationError`; assign/release record `seat_change` events + audit; list/available no audit; tenancy.
- [ ] **Step 2:** FAIL → **Step 3:** Implement → **Step 4:** PASS; mypy+ruff clean.
- [ ] **Step 5: Commit** `feat(subscriptions): seat assignment — pool enforcement + release`.

---

### Task 8: Reporting

**Files:** Create `backend/app/modules/subscriptions/reporting.py`; Test `backend/tests/test_subscriptions_reporting.py`.

**Interfaces:** Read-only, org-scoped, NO audit (mirror `licenses/reporting.py`). Consumes `costs`. Produces:
- `spend_summary(session, org_id) -> dict` — for active subs: total `monthly_equivalent` and `annualized` grouped **by currency**, plus per-`vendor_id`, per-`category`, per-`owner_user_id` roll-ups (each grouped by currency). Return a dict like `{"by_currency": [...], "by_vendor": [...], "by_category": [...], "by_owner": [...]}`.
- `renewal_calendar(session, org_id, *, now: datetime, within_days=30) -> list[dict]` — active subs whose `notice_deadline = renews_at - notice_days` OR `renews_at` ∈ `[now.date(), now.date()+within_days]`; emit `{subscription_id, service_name, renews_at, notice_deadline, days_to_notice, days_to_renewal, action_required}` where `action_required = auto_renew and now.date() >= notice_deadline`. Sort by `notice_deadline`.
- `idle_seats(session, org_id) -> list[dict]` — per active sub: `{subscription_id, service_name, seats, assigned (active count), idle, currency, idle_monthly_cost}` where per-seat cost = `monthly_equivalent(cost_per_cycle, billing_cycle) / seats` (guard seats=0) and `idle_monthly_cost = per_seat * idle`.
- `export_rows(session, org_id) -> list[dict]` — flat subscription rows for CSV (all scalar fields; `payment_ref` included as a reference, no vault secrets exist).

- [ ] **Step 1: Write the failing test** — hand-computed: spend by currency (2 monthly USD subs 10+20 → monthly_total 30, annual 360; a EUR sub separate); renewal with fixed `now` — a sub renewing in 40 days with notice_days=14 whose notice_deadline (26 days out) is within a 30-day window is included, `action_required` true only when `now>=notice_deadline and auto_renew`; idle-seats math (seats 5, 2 assigned → idle 3, idle cost = 3 × per-seat); export carries expected fields; tenancy — second org excluded from all four (RLS-bypassing admin-session load-bearing variant on `spend_summary`).
- [ ] **Step 2:** FAIL → **Step 3:** Implement → **Step 4:** PASS; mypy+ruff clean.
- [ ] **Step 5: Commit** `feat(subscriptions): spend/renewal/idle-seat reports + CSV export`.

---

### Task 9: API routes + registration + RBAC

**Files:** Create `backend/app/modules/subscriptions/api.py`; Modify `backend/app/modules/subscriptions/__init__.py` (`register`), `backend/app/modules/__init__.py` (`register_all`), `backend/app/core/rbac.py` (`PERMISSIONS`), `backend/tests/test_rbac.py` + `backend/tests/test_api_routes.py` (perm-sets); Test `backend/tests/test_subscriptions_api.py`.

**Interfaces:** Routes per spec §9, each `Depends(require_permission("<code>"))`; `org_id`/`actor_id` from `AuthContext`. Shared `_http_error(exc)` → `isinstance(exc, SubscriptionNotFoundError)`→404 else 400 (mirror `licenses/api.py`). CSV export writes `subscription.export` audit; plain reads write nothing. `register(app)` mounts router only; `register_all` adds `subscriptions.register(app)` after `licenses`. Route ordering: static `/subscriptions/{spend,renewals,idle-seats,export}` BEFORE `/subscriptions/{id}`.
PROACTIVE: `rbac.py` `PERMISSIONS` += `subscriptions.read`/`subscriptions.manage`; `test_rbac.py::test_permission_registry_complete` += 2; `test_api_routes.py::test_me_returns_identity_and_permissions` technician set += both.

- [ ] **Step 1: Write the failing test** (mirror `test_licenses_api.py` `api_env`): POST /subscriptions OK technician(manage)/403 read-only; POST seat assign + release; POST cancel + renew; GET spend/renewals/idle-seats OK read; cross-org `vendor_id` on POST → 400 (not 500); missing ids → 404; `GET /subscriptions/export` audits `subscription.export` while `GET /subscriptions` list does not; `/subscriptions/spend` resolves to the report route not `/subscriptions/{id}`; currency >3 chars → 422; no route 500s on a `SubscriptionValidationError`.
- [ ] **Step 2:** FAIL → **Step 3:** Implement api.py + wiring + rbac.py + the two perm-set-test updates.
- [ ] **Step 4:** `test_subscriptions_api.py` PASS; then FULL suite `.venv/bin/python -m pytest -q` green; `.venv/bin/mypy app/modules/subscriptions/ app/modules/__init__.py app/core/rbac.py` + `.venv/bin/ruff check app/modules/subscriptions/ tests/test_subscriptions_api.py` clean.
- [ ] **Step 5: Commit** `feat(subscriptions): API routes + module registration`.

---

### Task 10: Frontend feature folder

**Files:** Create `frontend/src/modules/subscriptions/{types.ts,subscriptionState.ts,api.ts,SubscriptionsPage.tsx,SubscriptionDetail.tsx,SpendDashboard.tsx,RenewalCalendar.tsx}`; Modify `frontend/src/lib/api.ts`, `frontend/src/App.tsx`; Test `frontend/src/modules/subscriptions/__tests__/subscriptions.test.tsx`.

**Interfaces:** `types.ts` mirrors backend outs exactly (`cost_per_cycle` + normalized costs as `string|null` — Decimal over wire). `subscriptionState.ts` maps backend vocab → `BadgeVariant` (import from `../jobs/jobState`), `?? "muted"` fallback: `status` {active→success, trial→info, cancelled→muted}; `billing_cycle`; renewal `actionVariant(action_required: boolean)` → destructive/muted — REAL backend values only, cite source. `api.ts` hooks with query-key invalidation. Pages: SubscriptionsPage (list + New gated `subscriptions.manage`); SubscriptionDetail (fields; **seats panel with Assign/Release wired** gated `manage` — NOT read-only; event-history timeline; Cancel + Record-renewal gated `manage`); SpendDashboard (roll-ups by currency); RenewalCalendar (notice-deadline + action-required badges). Nav `{ to:"/subscriptions", label:"Subscriptions" }` gated `permissions.includes("subscriptions.read")` + routes. Strict TS, no `any`.

- [ ] **Step 1: Write the failing test** (mirror `modules/licenses/__tests__/licenses.test.tsx`): SubscriptionsPage lists subs; "New subscription"/manage controls hidden without `subscriptions.manage`; SubscriptionDetail Assign-seat AND Cancel controls hidden without `manage`, shown with it (prove BOTH directions — this is the Phase-11 lesson); `statusVariant("active")` non-fallback and unknown → `"muted"`; `actionVariant(true)` → destructive.
- [ ] **Step 2:** FAIL → **Step 3:** Implement pages + nav/route + ApiClient methods (`listSubscriptions`/`createSubscription`/`getSubscription`/`updateSubscription`/`cancelSubscription`/`renewSubscription`/`listEvents`/`listSeats`/`assignSeat`/`releaseSeat`/`spend`/`renewals`/`idleSeats`).
- [ ] **Step 4:** `npm test`, `npm run typecheck`, `npm run build` all green.
- [ ] **Step 5: Commit** `feat(subscriptions): registry/seats/spend UI + renewal calendar`.

---

### Task 11: Deploy docs

**Files:** Modify `deploy/README.md`, `deploy/.env.example`; Test `cd deploy && cp .env.example .env && docker compose config >/dev/null && echo OK && rm -f .env`.

- [ ] **Step 1:** Add a "Subscriptions (SaaS management)" section to `deploy/README.md`: control-plane only (no agent, **no worker**, no new compose services, no new env vars); reuses Postgres, the audit log, RBAC, and the shared `vendor` entity; notice/renewal alerts computed at read time; per-record currency (no FX — Budget owns roll-up); advisory `license_ref` for hybrids. One-line `.env.example` note.
- [ ] **Step 2: Validate** `docker compose config` prints OK.
- [ ] **Step 3: Commit** `docs(subscriptions): deploy notes — control-plane-only, no worker`.

---

## Self-Review

**Spec coverage:** §1 boundary→docs(T11); §2 model→T1/T2; §3 lifecycle+events→T5(create/update events)+T6(cancel/renew); §4 seats→T7; §5 normalization→T4; §6 license_ref→T5(`_validate_refs`); §7 reporting→T8; §8 invariants→ tenancy(all + admin-session load-bearing T5/T8), RBAC(T1 seed + T9 enforce + rbac.py + perm-set tests), audit(all service tasks; no-audit-on-view T5/T7/T8; export T9), errors(T3 typed + T9 isinstance + currency 422), secrets(payment_ref non-vault); §9 API→T9; §10 no worker→ correctly omitted (no worker task); §11 frontend→T10 (seats wired); §12 testing→per-task; §13 deploy→T11; §14 deferred→not built.

**Placeholder scan:** every task names concrete files, signatures, and hand-computed test values; no TBD/TODO. Migration revision resolved live in T1.

**Type consistency:** `SubscriptionValidationError`/`SubscriptionNotFoundError`, `Subscription`/`SeatAssignment`/`SubscriptionEvent`, `monthly_equivalent`/`annualized`/`advance_renewal`, `create_subscription`/`update_subscription`/`cancel_subscription`/`record_renewal`/`assign_seat`/`release_seat`/`spend_summary`/`renewal_calendar`/`idle_seats` used consistently. `BillingCycle`/`SubStatus` Literals + event `type` set {created,renewal,price_change,seat_change,plan_change,cancel} consistent T3/T5/T6/T7. The 2 perm codes consistent T1/T9/T10. `license_ref` advisory (no FK) consistent T1/T2/T5.
