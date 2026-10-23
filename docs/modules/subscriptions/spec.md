# Module Spec — Subscriptions Management (SaaS Management)

> Phase 12. Second of the financial trio (licenses → subscriptions → budget).
> Depends on the kernel and reuses the shared `vendor` entity from Licenses
> (Phase 11). Derived from `docs/modules/subscriptions.md` via the brainstorming
> skill; four design decisions were resolved with the user (see §0). Control-
> plane only — no agent capability, **no worker**.

---

## 0. Resolved design decisions

1. **Control-plane only, no worker.** Notice-deadline and upcoming-renewal
   "alerts" are computed at read time in the reporting layer (like BCP's overdue
   flag) — there is no notification channel and no persisted-snapshot table in
   v1. Renewals are recorded by an explicit operator action (no auto-renew
   sweep; that automation is a fast-follow).

   *Amended 2026-09-23 (platform review): alerts are now pushed.* A daily
   `subscription-alerts` beat (`app/modules/subscriptions/alerts.py`) publishes
   `subscription.notice_deadline_approaching` (when `renews_at − notice_days`
   is within the window and `notice_days > 0`) and
   `subscription.renewal_approaching` (when `renews_at` is) into the
   notification outbox, for active subscriptions. The window is
   `ITS_SUBSCRIPTION_ALERT_WINDOW_DAYS` (default 30). Each is sent once per
   subscription per deadline: `subscription_alert_sent` (migration 0086) is
   keyed on `(org_id, subscription_id, kind, deadline)`, so recording a renewal
   produces a new deadline and a new alert. Neither event has a ticket or a
   requester, so they route through `channel_default` rules. The read-time
   calendar is unchanged. Renewals are still recorded by hand.
2. **Seat holders are free-form person records.** `seat_assignment` stores a
   plain `assignee_name` + `assignee_email`; the platform's `User` table is
   operators only, and seat holders are employees at large. Directory-synced
   person records (Entra/Google) are a later phase.
3. **Category is a free-text string.** Operator-entered; reports group by
   whatever values exist. An org-configurable taxonomy can be layered on later
   without a migration.
4. **Per-record currency, no FX.** Same as Licenses: each subscription carries
   `cost_per_cycle` + an ISO-4217 `currency`; monthly-equivalent/annualized
   normalization is per-record math; roll-ups (vendor/category/owner) group and
   sum **by currency**. A per-org base currency and cross-currency roll-up are
   owned by the Budget module (Phase 13), which owns FX.

---

## 1. Purpose & boundary

A registry and control point for recurring SaaS/cloud subscriptions — plans,
seats, costs, owners, renewal cycles — so the org knows what it pays for, who
uses it, and **when the window to cancel or renegotiate closes**.

**Boundary vs Licenses (one rule):** cloud/SaaS seat services → **Subscriptions**;
device-installed software → **Licenses**. Hybrid products (e.g. Microsoft 365)
live here, with an optional advisory link to a license record where install
reconciliation matters (§6).

---

## 2. Data model

All tables org-scoped with RLS `ENABLE + FORCE` + a `tenant_isolation` policy
(`USING (org_id = current_setting('app.current_org', true)::uuid)` + matching
`WITH CHECK`), exactly as every prior module. Migration is the next in sequence
(expected `0019`, chained from the current head; Task 1 confirms the head).

### `subscription`
`id, org_id, vendor_id (FK vendor, nullable), service_name (str), plan (str,
default ""), category (str, default ""), billing_cycle (str(10)), cost_per_cycle
(Numeric(14,2)), currency (str(3)), seats (int, default 0), owner_user_id (FK
users, nullable), started_at (date|None), renews_at (date|None), auto_renew
(bool, default False), notice_days (int, default 0), payment_ref (str|None),
license_ref (UUID|None — advisory, see §6), status (str(10), default "active"),
created_by, created_at`.
Indexes `(org_id, vendor_id)`, `(org_id, renews_at)`, `(org_id, owner_user_id)`.
`billing_cycle ∈ {monthly, quarterly, annual}`. `status ∈ {active, trial, cancelled}`.

### `seat_assignment`
`id, org_id, subscription_id (FK subscription), assignee_name (str), assignee_email
(str), assigned_at (server_default now), released_at (datetime|None), created_by`.
Index `(org_id, subscription_id)`. Active while `released_at IS NULL`.

### `subscription_event`
`id, org_id, subscription_id (FK subscription), type (str(16)), details (jsonb,
default {}), created_by, created_at (server_default now)`.
Index `(org_id, subscription_id, created_at)`.
`type ∈ {created, renewal, price_change, seat_change, plan_change, cancel}`.
The immutable change-history trail (distinct from the audit log: this is
business history surfaced in the UI; the audit log is the tamper-evident
security record).

---

## 3. Subscription lifecycle & change history

- `create_subscription` — validate `vendor_id`/`owner_user_id`/`license_ref` (if
  set) org-scoped (§6/§8); insert `status="active"` (or `"trial"` if requested);
  record a `subscription_event` type `created`; audit `subscription.created`.
- `update_subscription` — org-scoped load; apply an allow-list of fields; **detect
  material changes and record events**: `cost_per_cycle` change → `price_change`
  (details `{from, to, currency}`); `seats` change → `seat_change` (details
  `{from, to}`); `plan` change → `plan_change` (details `{from, to}`). Validate
  `status`/`billing_cycle` against their allow-lists. Audit `subscription.updated`.
- `record_renewal` — an explicit manual action: advance `renews_at` by one
  `billing_cycle` (monthly→+1mo, quarterly→+3mo, annual→+1yr; date arithmetic
  clamps month-end), record a `renewal` event, audit `subscription.renewed`.
- `cancel_subscription` — set `status="cancelled"`, record a `cancel` event,
  audit `subscription.cancelled`. (Idempotent: cancelling an already-cancelled
  subscription is a `SubscriptionValidationError`.)
- `get_subscription` / `list_subscriptions` / `list_events` — org-scoped, NO audit.

---

## 4. Seat assignments

- `assign_seat(sub_id, assignee_name, assignee_email)` — org-scoped-load the
  subscription; enforce the pool: `active_assignments < subscription.seats` else
  `SubscriptionValidationError("no seats available")`; insert; record a
  `seat_change` event (details `{action: "assigned", email}`); audit
  `subscription.seat_assigned`.
- `release_seat(assignment_id)` — org-scoped load; already-released →
  `SubscriptionValidationError`; stamp `released_at = now`; record a `seat_change`
  event (details `{action: "released", email}`); audit `subscription.seat_released`.
- `available_seats(subscription) = seats - active_assignment_count` (org+sub
  scoped). NO audit.

---

## 5. Cost normalization

Pure helpers (unit-testable, currency-agnostic — they operate on the
subscription's own `cost_per_cycle`/`currency`):
- `monthly_equivalent(cost_per_cycle, billing_cycle) -> Decimal` =
  `cost / {monthly: 1, quarterly: 3, annual: 12}` (rounded to 2dp, `ROUND_HALF_UP`).
- `annualized(cost_per_cycle, billing_cycle) -> Decimal` =
  `cost * {monthly: 12, quarterly: 4, annual: 1}`.
Arbitrary/custom billing periods are deferred (v1 supports the three exact
cycles). These feed the roll-ups in §7.

---

## 6. Hybrid license link (advisory)

`subscription.license_ref` is an optional `UUID` referencing a `license` row
(Phase 11). When set on create/update it is resolved org-scoped
(`select(License.id).where(License.org_id == org_id, License.id == license_ref)`)
— a cross-org or nonexistent id raises `SubscriptionValidationError` (invariant
#5). It is **advisory**: the `license` row is only SELECTed to validate, never
mutated; it records the hybrid-product relationship (e.g. an M365 subscription
whose desktop apps are install-reconciled on the license side). No gating.

---

## 7. Reporting (read-only, org-scoped, no audit-on-view)

- **Spend summary** `GET /subscriptions/spend`: total monthly-equivalent and
  annualized spend grouped **by currency** (no cross-currency sum); plus per-
  `vendor`, per-`category`, per-`owner` roll-ups (each grouped by currency).
- **Renewals / notice deadlines** `GET /subscriptions/renewals?within_days=N`
  (default 30): active subscriptions whose **notice cutoff** (`renews_at −
  notice_days`) OR `renews_at` falls within `[today, today+within_days]`, with a
  computed `notice_deadline` (date), `days_to_notice`, `days_to_renewal`, and an
  `action_required` flag (true when today ≥ notice_deadline and `auto_renew`).
  The notice cutoff is the decision point — after it passes, the renewal is
  committed.
- **Unassigned (paid-but-idle) seats** `GET /subscriptions/idle-seats`: per
  subscription, `seats`, `assigned` (active), `idle = seats − assigned`, and the
  idle cost (idle × per-seat monthly-equivalent), grouped by currency.
- **CSV export** `GET /subscriptions/export`: flat subscription rows for Power
  BI (no secrets — `payment_ref` is a reference string, included; see §8). This
  export is **audited** (`subscription.export`); plain list/detail/spend/renewal
  reads are NOT.

---

## 8. Security invariants

- **#5 Tenancy:** RLS on all three tables; every query carries an explicit
  `org_id` predicate. Every cross-reference — `vendor_id`, `owner_user_id`,
  `license_ref` — resolved `WHERE org_id AND id`, rejecting another org's id as
  indistinguishable-from-nonexistent (`SubscriptionValidationError`). Include an
  RLS-bypassing admin-session load-bearing test proving the app-level `org_id`
  predicate is the sole guard on the cross-ref resolves (the Phase-10/11 lesson).
- **#6 RBAC (server-side):** two permissions — `subscriptions.read` (view
  registry, seats, reports) and `subscriptions.manage` (create/edit/cancel
  subscriptions, assign/release seats, record renewals). Migration-seeded grants:
  super-admin & org-admin = both; **technician = read + manage**; auditor &
  read-only = read only. `PERMISSIONS` in `app/core/rbac.py` gains both, and the
  perm-set tests (`test_rbac`, `test_api_routes`) are updated in the same task
  that adds the routes (the Phase-7 lesson). `payment_ref` is a low-sensitivity
  reference (invoice/PO/last-4-style), NOT a vault secret — no dedicated reveal
  permission; do not store full card/credential data in it (documented).
- **#4 Audit (hash-chained):** subscription create/update/cancel/renew, seat
  assign/release, and CSV export are audited with `target_type` (`subscription`
  / `seat_assignment`) and `actor_type="user"`. **No-audit-on-view:** lists,
  detail, event history, and spend/renewal/idle reports write nothing. Note the
  `subscription_event` table is business change-history, written on mutation —
  it is NOT a substitute for the audit log, and reading it writes nothing.
- **#3 Secrets:** none of vault grade; `payment_ref` handled as above.
- Errors: typed `SubscriptionNotFoundError(SubscriptionValidationError)`; the API
  maps NotFound → 404, other validation → 400 **by isinstance**, never message-
  string, never 500 (the Phase-9 lesson). `currency` is length-constrained to 3
  at the schema boundary (the Phase-11 lesson — avoid a Postgres truncation 500).

---

## 9. API (routes under `/api/v1`)

Subscriptions: `GET/POST /subscriptions`, `GET/PATCH /subscriptions/{id}`,
`POST /subscriptions/{id}/cancel`, `POST /subscriptions/{id}/renew`,
`GET /subscriptions/{id}/events`.
Seats: `GET/POST /subscriptions/{id}/seats`, `POST /seats/{id}/release`.
Reports: `GET /subscriptions/spend`, `GET /subscriptions/renewals`,
`GET /subscriptions/idle-seats`, `GET /subscriptions/export`.
Read routes → `subscriptions.read`; writes → `subscriptions.manage`. `org_id`/
`actor_id` from `AuthContext`, never body/path. `register(app)` mounts the
router ONLY (no job types, no gateway, no worker). Route-ordering caution:
declare the static `/subscriptions/{spend,renewals,idle-seats,export}` **before**
`/subscriptions/{id}`.

---

## 10. Worker

**None.** Control-plane only — no Celery task, no beat entry, no new compose
service, no new required env var. Notice/renewal alerts are computed at read
time (§7); renewals are recorded by the explicit `record_renewal` action (§3).

---

## 11. Frontend (`frontend/src/modules/subscriptions/`)

`types.ts` mirrors the backend response models exactly (`cost_per_cycle`/
normalized costs as `string|null` — Decimal over the wire). `subscriptionState.ts`
maps backend vocab → `BadgeVariant` with `?? "muted"` fallback (subscription
`status` {active→success, trial→info, cancelled→muted}; `billing_cycle`; a
renewal `action_required`→destructive / due-soon→warning / ok→muted) — real
backend values only (the Phase-5 lesson). Pages: SubscriptionsPage (list + New,
gated `subscriptions.manage`); SubscriptionDetail (fields, seats panel with
assign/release gated `manage`, event-history timeline, Cancel + Record-renewal
gated `manage`); SpendDashboard (roll-ups by currency); RenewalCalendar (notice
deadlines + action-required badges). Nav `{ to: "/subscriptions", label:
"Subscriptions" }` gated `permissions.includes("subscriptions.read")`. Strict
TS, no `any`. (Learn from Phase 11: wire the seat assign/release actions — do not
ship a read-only seats panel.)

---

## 12. Testing (test-first, security paths explicit)

- Lifecycle: create records a `created` event; update detects price/seat/plan
  changes and records the right events with correct `{from,to}` details; cancel
  sets status + records `cancel`, double-cancel rejected; `record_renewal`
  advances `renews_at` by the cycle (incl. month-end clamp) + records `renewal`.
- Seats: assign to the cap then 400; release frees a slot; available_seats math;
  double-release rejected.
- Cost normalization: hand-computed literals for monthly/quarterly/annual
  monthly-equivalent and annualized.
- Cross-refs: cross-org `vendor_id`/`owner_user_id`/`license_ref` → validation
  error; RLS-bypassing admin-session load-bearing test (mutation-verified) on at
  least one cross-ref resolve.
- Reporting: spend grouped by currency (no cross-currency sum); renewal notice-
  deadline + action_required flag against a fixed `now`; idle-seat math; export
  carries no unexpected secret and is audited while plain reads are not.
- API: every route RBAC-gated with the correct code; NotFound→404 / validation→
  400 never 500; route ordering; currency length rejected as 422.
- Tenancy on every query; full suite green; mypy (strict) + ruff + frontend
  typecheck/build clean.

---

## 13. Deploy

`deploy/README.md` gains a "Subscriptions (SaaS management)" section: control-
plane only (no agent, no worker, no new compose services, no new env vars);
reuses Postgres, the audit log, RBAC, and the shared `vendor` entity;
notice/renewal alerts computed at read time; per-record currency (no FX — Budget
owns roll-up). `.env.example` gets a one-line "adds no vars" note. `docker
compose config` must validate.

---

## 14. Deferred (explicitly not in v1)

- Entra ID / Google Workspace directory + sign-in reconciliation (dormant-seat /
  offboarding-leak detection); shadow-IT discovery from SSO app data.
- Auto-renew background sweep (auto-advancing renewals); spend-anomaly alerts;
  push/email notifications to owners.
- Contract/quote document parsing to prefill records.
- Budget integration (auto-generated expense/committed-spend entries), per-org
  base currency + FX roll-up, and a payment-method entity link (Phase 13, Budget).
- Org-configurable category taxonomy; arbitrary/custom billing periods.
