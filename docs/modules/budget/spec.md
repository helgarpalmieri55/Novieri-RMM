# Module Spec — IT Budget & Expense Management

> Phase 13. Last of the financial trio (licenses → subscriptions → **budget**).
> Depends on the kernel, reuses the shared `vendor` entity from Licenses
> (Phase 11), and **consumes Licenses + Subscriptions as auto-feeds**. Derived
> from `docs/modules/budget.md` via the brainstorming skill; six design
> decisions were resolved with the user (see §0). Control-plane only — no agent
> capability, **no worker**.

---

## 0. Resolved design decisions

1. **Budget owns FX; rates live in an org-maintained table.** A per-org base
   currency plus an `fx_rate` table (`from_currency → base`, `effective_date`,
   `rate`). Every expense stores both the `fx_rate` it was converted at **and**
   the resulting `amount_base`, so editing or adding a rate later never
   re-values history. No external rate feed and no sweep — consistent with the
   on-prem-first posture and with Subscriptions' no-worker precedent. Where no
   rate covers a date, the row is still created with `amount_base = NULL` and
   is reported as unconvertible; **the module never guesses a rate.** This is
   the convention `licenses/spec.md` §24-27 and `subscriptions/spec.md` §4 both
   explicitly deferred to this module.
2. **Cost centers are free-standing**, not modelled on Odoo analytic accounts.
   They carry a nullable `external_ref` string from day one so a later Odoo
   mapping is a data change, not a migration.
3. **Expected entries never auto-confirm.** A human always confirms, and the
   confirmed amount may differ from the projected one. Auto-confirming would
   make the module assert a figure nobody verified, which defeats the purpose
   of tracking actuals.

   **AMENDED 2026-08-16, on approval of `odoo-sync-spec.md`.** A row pulled
   from Odoo may land as `confirmed`, and the rule above is unchanged for
   every other source. The reason this is not the thing the rule forbids: a
   posted vendor bill *was* verified — by a human, in the accounting system
   of record — so the module is not asserting a figure nobody checked; it is
   mirroring one somebody already did. Landing such rows as `expected`
   instead would also double-count them against the materialized subscription
   rows already in the `committed` column. What still holds absolutely: an
   Odoo row that a human has since `confirmed` is **never** silently
   overwritten by a later sync — a changed upstream amount becomes `drifted`
   and goes to the review queue for a person to accept or dismiss.
4. **Odoo export is CSV in v1**, with a documented and audited column contract.
   Same posture change-mgmt took with its null adapter: the shape is pinned, the
   API push is deferred.
5. **Four permissions, and technician gets none of them.** `budget.read` /
   `budget.manage` / `expense.record` / `expense.confirm`, with record and
   confirm split the way change-mgmt split `change.request` from
   `change.approve`. Every prior module granted technician read; this is the
   first that must not, because budget and expense data is sensitive even
   inside IT. No per-cost-center scoping in v1 — visibility is org-wide to
   permission-holders.
6. **No worker: the forecast is read-time, materialization is explicit.** The
   committed-spend forecast is a pure read-time projection over
   `subscription.renews_at`/`billing_cycle` and
   `license.renews_at`/`maintenance_expires_at` — no rows, no drift, always
   current. Expected **expense rows** materialize only on an explicit, audited
   operator action, idempotent under a unique constraint so re-running is a
   no-op. This delivers the auto-feed advantage without a nightly sweep
   silently writing financial records.

**Deviation from the brief, recorded deliberately:** the brief puts
`base_currency` on `budget`. Two budgets with different base currencies would
give an expense two possible base values, so base currency is instead a single
**org-level setting** (§3). Changing it is audited and does not retro-convert
history.

---

## 1. Purpose & boundary

Plan IT budgets, record and categorize IT spend, and track actuals against plan
— with the platform's unfair advantage: the license renewals and subscription
billing cycles it already knows about become expected expense entries and a
committed-spend forecast, so the picture stays current with minimal data entry,
and every cost line can link to the asset, license, subscription, or change
that explains it.

**Boundary vs the ERP (binding).** This is **not an accounting system**. Odoo
(or the customer's ERP) remains the system of record for invoices, payments,
and the general ledger. This module is the **IT lens**: category-level budget vs
actuals, renewal-driven forecasting, and cost lines linked to the technical
objects this platform manages — exported *to* the ERP, never replacing it. No
double-entry, no journal, no payment state, no tax handling.

---

## 2. Data model

All tables org-scoped with RLS `ENABLE + FORCE` + a `tenant_isolation` policy
(`USING (org_id = current_setting('app.current_org', true)::uuid)` + matching
`WITH CHECK`), exactly as every prior module. Migration is the next in sequence
(expected `0020`, chained from the current head; Task 1 confirms the head).

### `budget_settings` (one row per org)
`org_id (PK), base_currency (str(3)), fiscal_year_start_month (int, 1-12,
default 1), updated_by (|None), updated_at`.

Created lazily on first read with `base_currency = "USD"` and
`fiscal_year_start_month = 1`, `updated_by = NULL` to mark it as never
operator-set. The UI prompts for an explicit base currency before the first
expense is recorded; the seeded default is a starting value, not a claim about
the org. There is no cross-org "global" row. Base currency is an ISO-4217 code,
validated at the schema boundary.

### `cost_center`
`id, org_id, code (str(32)), name (str(255)), external_ref (str(64)|None),
active (bool, default True), created_by, created_at`.
Unique `(org_id, code)`.

### `expense_category`
`id, org_id, key (str(64)), name (str(255)), system_key (str(64)|None),
active (bool, default True), sort_order (int, default 0), created_by,
created_at`.
Unique `(org_id, key)`; unique `(org_id, system_key)` where `system_key IS NOT
NULL`.

Seeded per org with the brief's defaults — `hardware`, `software_licenses`,
`saas_subscriptions`, `cloud_hosting`, `telecom`, `services`, `maintenance` —
each carrying a `system_key` equal to its key. Orgs may add, rename, and
deactivate categories; `system_key` is the **stable identifier the auto-feeds
resolve against**, so renaming a category never breaks materialization.

> Departure from Subscriptions, deliberate: Subscriptions models category as
> free text (§0.3 there). Budget cannot, because variance math joins plan to
> actual on the category — a typo would silently split a category in two.

### `budget`
`id, org_id, fiscal_year (int), name (str(255)), granularity (str(10):
"month" | "quarter"), status (str(10): "draft" | "active" | "closed", default
"draft"), created_by, created_at`.
Unique `(org_id, fiscal_year, name)`.

Base currency is **not** on this table (see §0 deviation). Period boundaries are
derived from `fiscal_year` + `budget_settings.fiscal_year_start_month` +
`granularity` by a pure helper (§4) — never stored, never hand-computed at a
call site.

### `budget_line`
`id, org_id, budget_id (FK budget), category_id (FK expense_category),
cost_center_id (FK cost_center|None), period_index (int), planned_amount
(Numeric(14,2)), created_by, created_at`.

`period_index` is 1-12 for monthly and 1-4 for quarterly granularity, validated
against the parent budget's granularity.

**Uniqueness needs two indexes, not one.** Postgres treats NULLs as distinct, so
a single unique index over a nullable `cost_center_id` would permit unlimited
duplicate org-wide lines:

- `UNIQUE (org_id, budget_id, category_id, cost_center_id, period_index)`
  `WHERE cost_center_id IS NOT NULL`
- `UNIQUE (org_id, budget_id, category_id, period_index)`
  `WHERE cost_center_id IS NULL`

### `fx_rate`
`id, org_id, from_currency (str(3)), to_currency (str(3)), rate
(Numeric(18,8)), effective_date (date), created_by, created_at`.
Unique `(org_id, from_currency, to_currency, effective_date)`.
Index `(org_id, from_currency, to_currency, effective_date DESC)`.

`rate` must be `> 0`, enforced at the schema boundary and by a check
constraint. `to_currency` must equal the org's base currency at write time.

### `expense`
`id, org_id, expense_date (date), amount (Numeric(14,2)), currency (str(3)),
fx_rate (Numeric(18,8)|None), amount_base (Numeric(14,2)|None), vendor_id (FK
vendor|None), category_id (FK expense_category), cost_center_id (FK
cost_center|None), description (str, default ""), source (str(16): "manual" |
"subscription" | "license"), link_type (str(16)|None), link_id (UUID|None),
period_key (str(16)|None), invoice_ref (str(128)|None), invoice_object_key
(str|None), status (str(10): "expected" | "confirmed" | "void", DB default
"expected"), entered_by, created_at,
confirmed_by (|None), confirmed_at (|None), voided_by (|None), voided_at
(|None)`.

Indexes `(org_id, expense_date)`, `(org_id, status)`,
`(org_id, category_id, expense_date)`.

**The idempotency guard:**

- `UNIQUE (org_id, source, link_id, period_key)` `WHERE source <> 'manual'`

Voided rows deliberately remain under the constraint: if an operator voided a
materialized entry, re-running materialization must **not** resurrect it.

`link_type` ∈ `{subscription, license, device, change}` and is advisory — a
plain org-scoped id reference with **no foreign key**, matching the posture
Subscriptions took for `license_ref`. A link that no longer resolves is
reported as unresolved, never an error.

> **Amended 2026-10-29 (MSP clients plan 2026-10-26, Task 8): budget is per
> client, with its own currency and fiscal year** (migration 0238).
> `budget_settings`, `cost_center`, `expense_category`, `budget` and
> `expense` are C (existing rows went to the internal client), and
> `budget_line` inherits its budget's client (H); all six carry the client
> policy of `CLAUDE.md` invariant 5a. **`budget_settings` is one row per
> client**, its primary key the client: each client has its own base currency
> and fiscal-year start month, and §3's base-currency guard counts that
> client's expenses only. A client's settings row is made by its first
> change, never by a read. Category keys, cost-center codes, budget names and
> the renewal-feed slot are unique per client, and a line's or an expense's
> category and cost center are the row's own client's (another client's is a
> 422, as is a linked subscription, license, device or change of another
> client). `fx_rate` stays the organisation's (G): a rate names the currency
> it converts into, by default the internal client's base currency. Budget is
> an optional module: every write names its client and is a 404 with the
> module's sentence for a client without it; creates are 404 not visible,
> 409 archived, 422 Unassigned. **A client that turns budget off can still
> export what it recorded**: the two exports check visibility, not the
> module. Settings, the forecast and the exports name a client in the query,
> and the renewal feed projects one client's subscriptions and licenses into
> that client's categories and currency. The plan is
> `docs/superpowers/plans/2026-10-26-msp-clientes-fase2-modulos.md`.

---

## 3. Base currency & FX conversion

A pure helper module (`fx.py`), mirroring how `costs.py` isolates Subscriptions'
cost math. Nothing outside it does currency arithmetic.

- `resolve_rate(session, org_id, from_currency, on_date) -> Decimal | None` —
  the rate with the greatest `effective_date <= on_date` for
  `(from_currency → base)`. Returns `None` when no such row exists. A rate is
  **never** extrapolated backwards from a later date, and never inverted from
  the opposite pair: if only `USD→COP` exists, `COP→USD` is unresolvable.
- `convert(amount, rate) -> Decimal` — `(amount * rate).quantize(0.01,
  ROUND_HALF_UP)`. Rounding is applied once, at the end.
- When `from_currency == base`, the rate is exactly `1` and no table lookup
  occurs.

**Conversion is captured, not computed on read.** An expense stores `fx_rate`
and `amount_base` at write time. Adding a corrective rate afterwards changes
nothing already recorded; re-valuing a historical row is an explicit edit that
re-runs conversion and is audited like any other change.

**Unconvertible rows are surfaced, never silently dropped.** Every aggregate
that sums `amount_base` also returns `unconvertible_count` and a per-currency
breakdown of the amounts it could not convert. A total is never quietly
understated — the same honesty rule Licenses and Subscriptions apply by
refusing to sum across currencies at all.

Changing `base_currency` is audited and does **not** retro-convert stored rows.
The rule is one sentence: **once any non-void expense carries an
`amount_base`, changing the base currency requires an explicit
`acknowledge_mixed_basis=true`**, and is rejected without it. Existing rows keep
the basis they were converted at, so any report spanning the change mixes two
bases; the UI states this plainly at the point of change rather than burying it.

---

## 4. Budget definition & periods

`periods.py`, pure and fully unit-tested:

- `period_bounds(fiscal_year, start_month, granularity, period_index) ->
  (date, date)` — inclusive start, inclusive end.
- `period_for_date(fiscal_year, start_month, granularity, d) -> int | None`.
- `period_count(granularity) -> int` (12 or 4).

A fiscal year starting in month `m` labelled `fiscal_year = Y` runs from
`Y-m-01` to the day before `(Y+1)-m-01`. Period 1 is always the period
containing the fiscal-year start. Month-end arithmetic uses calendar-correct
bounds (no 30-day approximations), and February/leap years are explicit test
cases.

Budget lifecycle: `draft → active → closed`. Lines are editable in `draft` and
`active`; a `closed` budget rejects all line mutations (400) so a reported
year cannot shift under a reader. Closing is audited.

---

## 5. Expense lifecycle

- **Manual entry** — the caller chooses `confirmed` (recording a real cost) or
  `expected` (pre-recording a known upcoming one). The service sets it
  explicitly; the DB default of `"expected"` is a safety floor, never the
  mechanism — a row is never `confirmed` by omission.
- **Auto-fed entry** — created by materialization (§6) as `expected`.
- **expected → confirmed** — an explicit action carrying the actual amount,
  which may differ from the projected figure. Confirming re-runs FX conversion
  against `expense_date`, stamps `confirmed_by`/`confirmed_at`, and is audited.
  There is no path that confirms without a user (§0.3).
- **→ void** — reversible only by creating a new entry. Voiding stamps
  `voided_by`/`voided_at`, is audited, and permanently suppresses
  re-materialization of that `(source, link_id, period_key)`.

`amount`, `currency`, `expense_date`, and `category_id` are editable while
`expected`; on a `confirmed` row an edit is permitted but re-runs conversion and
records the changed field names in the audit metadata (names only, never
values — the Phase-12 convention).

**Invoice attachment.** Optional, stored in object storage under
`{org_id}/budget/expenses/{expense_id}/attachment`, matching the per-tenant
layout Backup and Provisioning established. The key carries **no
client-supplied segment** — it is derived entirely server-side from the org
and expense ids, following the precedent in `deploy`'s package-artifact
upload. A filename that reaches the object key is a filename that can escape
the per-tenant prefix or poison a `Content-Disposition` header.

The client's display filename is stored separately in
`expense.invoice_filename`, sanitized on the way in: rejected outright (400)
unless it survives a basename and an ASCII-only allow-list anchored with
`fullmatch`. Both details are load-bearing — a Unicode-wide class makes an
ordinary non-Latin filename un-downloadable (latin-1 headers), and `$`
instead of `fullmatch` admits a trailing newline, which is the same header
poisoning the allow-list exists to prevent.

The upload body is bounded (10 MiB) on both the declared `Content-Length` and
the bytes actually read, since the header is client-supplied. Attachment bytes
are never returned inline in a list response — the list exposes
`has_attachment` and `invoice_filename`, never the object key, which contains
the tenant id and bucket name. Upload is `expense.record`-gated and rejected
on a void expense; download is `budget.read`-gated. Both are audited.

---

## 6. Auto-feeds: forecast (read-time) and materialization (explicit)

### 6.1 Projection source

A pure projector over the two upstream modules, org-scoped:

- **Subscriptions** — for each `active` subscription with a `renews_at`,
  project forward by `billing_cycle` using the existing normalization helpers.
  `cancelled` and `trial` subscriptions are excluded; the trial exclusion
  mirrors `subscriptions/spec.md` §7 and is flagged there as a live product
  question.
- **Licenses** — project `renews_at` and `maintenance_expires_at` as one-off
  expected costs using `license.cost` / `license.currency`.

Category assignment resolves by `system_key`: subscriptions →
`saas_subscriptions`, licenses → `software_licenses`. If the org has no active
category with that `system_key`, materialization fails with a clear validation
error naming the missing key — it does not fall back to a guess. The free-text
`subscription.category` is **not** used for budget categorization in v1.

`period_key` is the canonical `YYYY-MM` of the projected date (monthly
granularity is the materialization unit regardless of budget granularity, so a
quarterly budget still gets month-accurate rows).

### 6.2 Forecast (no rows written)

`GET /budget/forecast?months=N` returns the projection per category × period,
converted to base where a rate resolves. It is computed entirely at read time —
nothing is persisted, so it can never drift from the upstream registries.

Rows already materialized are reported in a separate bucket from rows that are
still pure projection, so the two are never summed into one ambiguous figure.

### 6.3 Materialization (explicit, idempotent, audited)

`POST /budget/expenses/materialize {from_date, to_date}` writes `expected`
expense rows for every projected cost in the window that does not already have
one, and returns `{created, skipped_existing, skipped_unconvertible}`.

- Idempotent by the §2 unique index — running it twice creates nothing the
  second time. This is an explicit test, not an assumption.
- A projected cost with no resolvable FX rate is still created, with
  `amount_base = NULL`, and counted in `skipped_unconvertible` for visibility.
- The window is bounded (max 24 months) to keep one call from generating an
  unbounded write.
- Audited once per call with the counts, plus per-row audit on creation.

---

## 7. Reporting (read-only, org-scoped, no audit-on-view)

All figures in base currency from `amount_base`, with the unconvertible
disclosure of §3 attached to every aggregate.

- **Variance** (`GET /budget/{id}/variance`) — per category × period × optional
  cost center, four distinct columns that are never collapsed into one:
  `planned` (from `budget_line`), `actual` (`status='confirmed'`), `committed`
  (`status='expected'`, i.e. materialized), and `projected` (read-time
  projection for periods with no materialized row). Plus `variance = planned -
  actual` and `variance_pct`.
- **Burn rate & YTD** (`GET /budget/{id}/position`) — year-to-date actual,
  percentage of plan consumed, average monthly burn over elapsed periods, and a
  straight-line projection to year end. Elapsed periods are computed from the
  fiscal calendar (§4), not from wall-clock month count.
- **Expense list** (`GET /budget/expenses`) — filterable by date range, status,
  category, cost center, vendor, source.
- **Confirmation queue** (`GET /budget/expenses?status=expected`) — the operator
  worklist that makes §0.3 workable.
- **Exports** (`GET /budget/expenses/export`, `GET /budget/expenses/export/odoo`)
  — CSV. The Odoo variant emits the pinned column contract
  (`date, reference, vendor, category, cost_center, description, amount,
  currency, amount_base, base_currency, link_type, link_id`) documented in
  `deploy/README.md`. Both exports are audited (`budget.export`); every other read
  above writes **no** audit event.

Division-by-zero is impossible by construction: `variance_pct` and
"percentage of plan" return `None` when the denominator is zero rather than
raising or emitting `Infinity`.

---

## 8. Security invariants

- **#5 Tenancy:** RLS on all six new tables; every query carries an explicit
  `org_id` predicate. Every cross-reference — `vendor_id`, `category_id`,
  `cost_center_id`, `budget_id`, and the advisory `link_id` — is resolved
  `WHERE org_id AND id`, rejecting another org's id as
  indistinguishable-from-nonexistent (`BudgetValidationError`). Include
  RLS-bypassing admin-session **load-bearing** tests proving the app-level
  `org_id` predicate is the sole guard on every cross-ref resolve and on the
  FX-rate lookup (the Phase-10/11/12 lesson — Phase 12 had correct tenancy
  tests written and then deleted before commit, caught only by mutation
  testing).
- **#6 RBAC (server-side):** four permissions —
  `budget.read` (budgets, expenses, dashboards, exports, attachment download),
  `budget.manage` (budgets, lines, categories, cost centers, FX rates, base
  currency), `expense.record` (create/edit manual expenses, run
  materialization), `expense.confirm` (expected → confirmed, void).
  Migration-seeded grants: super-admin & org-admin = all four; **technician =
  none**; auditor = `budget.read`; read-only = none. `PERMISSIONS` in
  `app/core/rbac.py` gains all four, and the perm-set tests (`test_rbac`,
  `test_api_routes`) are updated **in the same task** that adds the routes (the
  Phase-7 lesson).
- **#4 Audit (hash-chained):** budget create/update/close, line create/update/
  delete, category and cost-center mutations, FX-rate create/update, base-
  currency change, expense create/update/confirm/void, attachment upload and
  download, materialization, and both exports — with `target_type`
  (`budget` / `budget_line` / `expense` / `fx_rate` / `cost_center` /
  `expense_category`) and `actor_type="user"`. **No-audit-on-view:** lists,
  detail, variance, position, forecast, and the confirmation queue write
  nothing **as a user-attributed event.** One narrow, deliberate exception:
  `list_categories`/`variance`/`forecast` (and `feeds.materialize`) all
  lazily bootstrap an org's seven default expense categories on first use
  (no org-bootstrap hook exists elsewhere in the platform). That path is
  reachable by a caller holding only `budget.read` (e.g. the
  migration-seeded `auditor` grant), so the bootstrap is never attributed to
  the reader as `budget.category_created` — category management is
  `budget.manage`'s action, not a read's. Instead, a seed call that actually
  inserts rows writes exactly ONE `budget.categories_seeded` event,
  `actor_type="system"`, `actor_id=None`, naming the keys it inserted; a
  seed call that inserts nothing (the common case after an org's first-ever
  read) writes nothing at all. `created_by` on the seeded rows still records
  the acting user — only the audit event's actor is system, not the row's
  ownership. Expense edit audits **changed field names only, never values** —
  amounts are the sensitive payload here.
- **#3 Secrets:** none of vault grade. `invoice_ref` is a reference string
  (invoice/PO number), not a credential; documented as such in deploy notes.
- **#1/#2:** no agent surface — no job types, no gateway registration, no
  capability, so no signed-job path and no idle-budget impact.
- **Errors:** typed `BudgetNotFoundError(BudgetValidationError)`; the API maps
  NotFound → 404 and other validation → 400 **by isinstance**, checking the
  subclass first, never by message string, never 500 (the Phase-9 lesson).
- **Schema-boundary bounds (the Phase-11/12 lesson):** every string bounded
  (`max_length`), every money field `max_digits=14, decimal_places=2`, `rate`
  `max_digits=18, decimal_places=8, gt=0`, every integer bounded on **both**
  ends (`fiscal_year` 2000-2100, `period_index` against granularity, `months`
  1-60) so nothing reaches Postgres as an out-of-range `DataError` → 500.
- **Money handling:** `Decimal` throughout, `Numeric(14,2)` at rest, JSON
  **strings** on the wire, `string` in TypeScript. No float arithmetic anywhere,
  and no summing across currencies without conversion through §3.

---

## 9. API (routes under `/api/v1`)

Settings: `GET/PUT /budget/settings`.
Categories: `GET/POST /budget/categories`, `PATCH /budget/categories/{id}`.
Cost centers: `GET/POST /budget/cost-centers`, `PATCH /budget/cost-centers/{id}`.
FX: `GET/POST /budget/fx-rates`, `PATCH /budget/fx-rates/{id}`.
Budgets: `GET/POST /budget/budgets`, `GET/PATCH /budget/budgets/{id}`,
`POST /budget/budgets/{id}/close`.
Lines: `GET/PUT /budget/budgets/{id}/lines` (bulk upsert for grid editing),
`DELETE /budget/budgets/{id}/lines/{line_id}`.
Expenses: `GET/POST /budget/expenses`, `GET/PATCH /budget/expenses/{id}`,
`POST /budget/expenses/{id}/confirm`, `POST /budget/expenses/{id}/void`,
`POST /budget/expenses/materialize`,
`POST/GET /budget/expenses/{id}/attachment`.
Reports: `GET /budget/budgets/{id}/variance`, `GET /budget/budgets/{id}/position`,
`GET /budget/forecast`, `GET /budget/expenses/export`,
`GET /budget/expenses/export/odoo`.

Reads → `budget.read`; budget/category/cost-center/FX/settings writes →
`budget.manage`; expense create/edit + materialize → `expense.record`;
confirm/void → `expense.confirm`. `org_id`/`actor_id` come from `AuthContext`,
never from body or path. `register(app)` mounts the router **only** — no job
types, no gateway, no worker.

**Route-ordering caution (the Phase-11/12 lesson):** declare
`/budget/expenses/export`, `/budget/expenses/export/odoo`, and
`/budget/expenses/materialize` **before** `/budget/expenses/{id}`; declare
`/budget/forecast` before any `/budget/{id}`-shaped route.

---

## 10. Worker

**None.** Control-plane only — no Celery task, no beat entry, no new compose
service, no new required env var (§0.6). The forecast is read-time;
materialization is an explicit operator action.

---

## 11. Frontend (`frontend/src/modules/budget/`)

- **Budget planner** — category × period grid with inline planned-amount
  editing, bulk-saved through `PUT .../lines`.
- **Expense list + entry form** — with vendor, category, cost center, link
  picker, and attachment upload.
- **Confirmation queue** — the `status=expected` worklist; confirm accepts a
  corrected amount.
- **Dashboard** — plan vs actual vs committed vs projected per category and
  period, burn rate, YTD position, and the unconvertible disclosure rendered
  explicitly rather than hidden.
- **Admin panels** — categories, cost centers, FX rates, base currency.

Conventions carried forward: money typed `string`, never `Number()`; badge
variant maps use only vocabulary the backend emits with a `?? "muted"` fallback
and a comment citing the backend source; every mutation hook's
`invalidateQueries` keys match the registered query keys; privileged controls
are **hidden** without permission, and the gating is presentational only — the
server is the authority. All four report surfaces must be **reachable from
navigation** (the Phase-12 lesson: two report pages shipped routed but
unlinked).

---

## 12. Testing (test-first, security paths explicit)

Beyond per-endpoint coverage:

- **FX** — rate resolution picks the greatest `effective_date <= d`; no
  backward extrapolation; no pair inversion; base→base is exactly 1 with no
  lookup; rounding is `ROUND_HALF_UP` applied once; unresolvable returns `None`
  and yields `amount_base = NULL`.
- **Idempotent materialization** — running it twice creates nothing the second
  time; a voided row is not resurrected; an unconvertible projection is created
  and counted.
- **Periods** — non-calendar fiscal years, quarter boundaries, February and
  leap years, `period_for_date` at both inclusive edges.
- **Uniqueness** — the NULL-`cost_center_id` partial index actually blocks a
  duplicate org-wide line (the constraint that a single naive unique index
  would silently fail to enforce).
- **Aggregates** — unconvertible rows are excluded from base totals **and**
  disclosed; `variance_pct` with a zero denominator returns `None`.
- **Tenancy** — RLS-bypassing admin-session load-bearing tests on every
  cross-ref resolve and the FX lookup, each mutation-verified (remove the
  predicate → the test must fail).
- **RBAC** — technician is denied every route in this module; `expense.record`
  cannot confirm; `expense.confirm` cannot create.
- **Audit** — every mutation writes; every view writes nothing; edit metadata
  carries field names and no amounts.

---

## 13. Deploy

Control-plane only. Notes to add to `deploy/README.md`: the object-storage
prefix for invoice attachments, the Odoo CSV column contract, the statement
that `invoice_ref` is a reference string and not a credential, and the
operational meaning of base-currency changes (not retroactive).

---

## 14. Deferred (explicitly not in v1)

Two-way Odoo sync (pull confirmed invoice amounts back; map cost centers to
analytic accounts); purchase-request/approval workflow feeding planned spend;
hardware depreciation and per-device TCO; multi-year planning and scenario
comparison; per-cost-center RBAC scoping; an external FX rate feed; automatic
confirmation after N days.

**Carried in from Phase 12, affecting this module:** the seat-pool TOCTOU in
`subscriptions.assign_seat` (mirrored from `licenses.allocate`) means
`available_seats` / `idle_seats` can transiently over-count. Budget therefore
treats seat counts as **advisory for cost attribution** and never as an
authoritative quantity in a financial figure. Fixing that race across both
modules in one pass remains the recommended follow-up.
