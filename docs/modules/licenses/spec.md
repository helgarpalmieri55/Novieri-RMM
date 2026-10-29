# Module Spec — Licenses Management (Software Asset Management)

> Phase 11. First of the financial trio (licenses → subscriptions → budget).
> Depends only on Inventory (Phase 1). Introduces the shared `vendor` entity the
> other two modules reuse. Derived from `docs/modules/licenses.md` via the
> brainstorming skill; four design decisions were resolved with the user (see
> §0). Control-plane only — no new agent capability.

---

## 0. Resolved design decisions

1. **Matching engine — curated rules + a review queue.** Each product carries
   `match_rules`; installed-software names that match no product surface in a
   review queue an operator resolves (map → product, or ignore). No heuristic
   auto-matching (a wrong auto-match would corrupt the compliance position an
   audit depends on).
2. **Compliance snapshots + alerting — a light Celery-beat sweep.** Reuses the
   existing Redis/Celery; **no new compose services**. The sweep recomputes and
   persists `license_compliance_snapshot` rows (trend history). Reports also
   compute compliance **live** at read time (authoritative). Renewal/breach
   "alerts" are surfaced as computed flags in the reporting layer — there is no
   push-notification channel in v1 (deferred with the rest of notifications).
3. **Currency — per-record `cost` + ISO-4217 `currency`, no FX.** Reports group
   and sum by currency. A per-org base currency and cross-currency roll-up are
   owned by the Budget module (Phase 13), which owns FX. This module invents no
   exchange rates.
4. **License keys — sealed with the existing per-org vault envelope.** Keys are
   sealed via `app.core.vault`'s `seal_bytes`/`open_bytes` (decrypt-at-use),
   never returned in list/detail responses, never logged. A dedicated reveal
   endpoint gated by a distinct `licenses.keys.read` permission writes an audit
   event on every access.

---

## 1. Purpose & boundary

Track software licenses/entitlements and reconcile them against **actual
installations from Inventory**, so the org always knows its compliance position
(over/under-licensed), upcoming renewals, and per-entitlement cost.

**Boundary vs Subscriptions (one rule):** software installed on devices →
**Licenses**; cloud/SaaS seat services → **Subscriptions**. Hybrid products live
in Subscriptions with a license-side link; not modelled in this phase (no
Subscriptions module yet).

---

## 2. Data model

All tables are org-scoped with RLS `ENABLE + FORCE` + a `tenant_isolation`
policy (`USING (org_id = current_setting('app.current_org', true)::uuid)` and a
matching `WITH CHECK`), exactly as every prior module. The migration is the next
in sequence (expected `0018`, chained from the current head; Task 1 confirms the
head — never hard-code it elsewhere).

### `vendor` — shared entity (the trio reuses this)
`id, org_id, name, website (str|None), contacts (jsonb, default []), created_by,
created_at`. Index `(org_id, name)`. Subscriptions/Budget and Documentation
vendor-records will FK/reference this later; this module owns it.

### `software_product`
`id, org_id, name, publisher (str, default ""), match_rules (jsonb, default []),
created_by, created_at`. Index `(org_id, name)`.
`match_rules` is a list of rule objects (see §4).

### `license`
`id, org_id, product_id (FK software_product), vendor_id (FK vendor, nullable),
type (str(16)), entitlements (int), key_sealed (text|None), cost (Numeric(14,2)|
None), currency (str(3)|None), po_ref (str|None), purchased_at (date|None),
maintenance_expires_at (date|None), renews_at (date|None), agreement_ref
(str|None), notes (text, default ""), created_by, created_at`.
Indexes `(org_id, product_id)`, `(org_id, renews_at)`, `(org_id, maintenance_expires_at)`.
`type ∈ {perpetual, volume, oem, cal, per_device, per_user}`.
`key_sealed` holds the vault-sealed key string (§5) — NULL when the license has
no key. `agreement_ref` is an optional reference string to a stored agreement
(managed binary upload is deferred — see §14).

### `license_allocation`
`id, org_id, license_id (FK license), target_type (str(8): "device"|"user"|"person"),
target_id (UUID), person_id (FK person, null for devices), allocated_at (server_default
now), released_at (datetime|None), created_by`. `person` and `person_id` arrived with
`docs/modules/people/spec.md` (migration 0115). Index `(org_id, license_id)`.
An allocation is "active" while `released_at IS NULL`.

### `license_compliance_snapshot`
`id, org_id, product_id (FK software_product), entitled (int), installed (int),
delta (int), computed_at (server_default now)`. Index `(org_id, product_id,
computed_at)`. `delta = entitled - installed` (negative = breach / over-installed;
positive = headroom).

### `software_ignore`
`id, org_id, name (str(300)), created_by, created_at`. Unique `(org_id, name)`.
The review-queue "ignore" sink so OS components / free tools stop reappearing
(§4). Kept deliberately small (exact-name ignore only).

> **Amended 2026-10-29 (MSP clients plan 2026-10-26, Task 6): licenses belong
> to a client, and compliance is counted per client** (migration 0236).
> `license` and `license_compliance_snapshot` are C, and `license_allocation`
> inherits the license's client (H); the client policy of `CLAUDE.md`
> invariant 5a is on all three. `vendor` and `software_product` stay the
> organisation's (G). `POST /licenses` names its client (404 not visible, 409
> archived, 422 Unassigned). An allocation's target — device, person or
> account — is the license's client's (`its_license_allocation_target_guard`;
> a technician's account is the internal client's, plan decision C4), and
> another client's is a 422. **Compliance (§4) is one row per client and
> product, and costs and renewals one row per client, never summed across
> clients**, in the route and in the daily sweep (§7): one client's
> entitlements are matched against that client's installs. `software_ignore` is O: a name ignored for one client
> stays in the other clients' review queues, and a global ignore still needs
> every client. Every license and allocation audit event carries the
> license's client. The plan is
> `docs/superpowers/plans/2026-10-26-msp-clientes-fase2-modulos.md`.

---

## 3. Vendor management

Plain org-scoped CRUD (`create_vendor`, `update_vendor`, `get_vendor`,
`list_vendors`), audited on create/update (`license.vendor_created` /
`license.vendor_updated`). No secrets. This is the shared entity; the code lives
in this module's `service.py` (or a `vendors.py` submodule) and later modules
import it rather than re-defining the table.

---

## 4. Matching engine (the crux)

### Rule format
Each `software_product.match_rules` entry:
```json
{ "type": "exact" | "prefix" | "regex", "pattern": "<str>", "publisher": "<str|null>" }
```
An `InstalledSoftware` row matches a rule when:
- `type=exact`: `name == pattern` (case-insensitive);
- `type=prefix`: `name` starts with `pattern` (case-insensitive);
- `type=regex`: `pattern` fully matches `name` (anchored; compiled safely, see
  hardening below);
- and, if `rule.publisher` is non-null, `installed.publisher == rule.publisher`
  (case-insensitive).
A row matches a **product** if it matches **any** of that product's rules.

**Regex safety:** rules are operator-authored (RBAC `licenses.manage`), but a
pathological pattern must not hang the compliance sweep. Compile with a length
cap on the pattern and match with a per-row guard; an invalid/oversized regex
rule is rejected at write time (`LicenseValidationError`) so it never enters the
engine. Matching is done in Python over the org's installed-software rows (not a
DB regex), keeping the tenant boundary in one place.

### Compliance computation (per product, org-scoped)
- `entitled(product) = Σ license.entitlements` over that product's licenses.
- `installed(product) =` count of **distinct devices** having ≥1
  `installed_software` row matching any of the product's rules.
- `delta = entitled - installed`. `installed > entitled` is a **breach**.
Computed live at read time from current inventory (authoritative); the sweep
(§7) persists periodic snapshots for history. Per-user license models still
reconcile against devices in v1 (installed = distinct devices); user-based
reconciliation is a later refinement (noted §14).

### Review queue
`GET .../review-queue` returns the org's distinct installed-software
`(name, publisher, device_count)` rows that match **no** product's rules **and**
are not in `software_ignore`, ordered by `device_count` desc (biggest exposure
first). Operator actions (both `licenses.manage`, both audited):
- **map**: `POST .../review-queue/map {name, product_id}` appends an `exact`
  rule for `name` to that product's `match_rules` (org-scoped product resolve —
  a cross-org `product_id` is rejected as not-found).
- **ignore**: `POST .../review-queue/ignore {name}` inserts a `software_ignore`
  row.

---

## 5. License-key encryption (invariant #3, lighter-weight variant)

- On license create/update **with** a key: seal via
  `vault.seal_bytes(session, org_id, key.encode())`; store the returned string in
  `key_sealed`. The plaintext key is never persisted, never logged.
- List/detail responses **never** include the key or `key_sealed`. `LicenseOut`
  carries a boolean `has_key` only.
- **Reveal:** `GET /licenses/{id}/key`, gated `licenses.keys.read`. Loads the
  license org-scoped, `vault.open_bytes` to recover the plaintext, writes a
  `license.key_revealed` audit event (invariant #4), returns the plaintext once
  in the response body. This is the ONLY path that returns a key.

---

## 6. Allocation

- `POST /licenses/{id}/allocations {target_type, target_id}`: validate the target
  is an in-org `device` or `user` (org-scoped resolve → 400 on cross-org/unknown);
  enforce the available pool — `active_allocations < entitlements`, else 400
  ("license pool exhausted"). Create the allocation; audit `license.allocated`.
- `POST /allocations/{id}/release`: stamp `released_at = now`; audit
  `license.allocation_released`. Idempotent-ish (releasing an already-released
  allocation is a 400).
- `available_pool(license) = entitlements - count(active allocations)`.
Allocation (assignment intent) and installation-based compliance (§4) are
distinct axes; both are surfaced in reporting.

---

## 7. Worker — compliance sweep (Celery beat)

- A beat task `licenses.compliance_sweep` runs on a schedule (default **daily**,
  `LICENSES_COMPLIANCE_SWEEP_CRON` / interval documented in `.env.example`). For
  each org, for each product: compute `entitled/installed/delta` (§4) and insert a
  `license_compliance_snapshot` row. Reuses the existing Celery worker + Redis;
  **no new compose service, no new required env var** beyond the optional cadence
  override.
- The sweep is org-scoped per iteration (sets the tenant GUC per org, like the
  other sweeps). It writes snapshots only; it raises no push notifications.
- **Alerts are computed at read time**, not by the sweep: the renewal calendar
  (§8) and the breach flag (latest snapshot `delta < 0`, or the live compute)
  are surfaced in reports/dashboard. No audit spam from re-alerting.

---

## 8. Reporting (read-only, org-scoped, no audit-on-view)

- **Compliance summary** `GET /licenses/compliance`: per product
  `{product_id, name, entitled, installed, delta, breach: bool}` computed live.
- **Renewal calendar** `GET /licenses/renewals?within_days=N` (default 30):
  licenses whose `renews_at` or `maintenance_expires_at` falls within the window,
  with a computed `due_in_days` and which date triggered it.
- **Per-product cost view** `GET /licenses/costs`: cost grouped by product and by
  currency (no cross-currency sum — §0.3).
- **CSV export** `GET /licenses/export`: flat license rows (no key material) for
  true-up / Power BI. This is an explicit **audited** export
  (`license.export`), mirroring the change-mgmt evidence-export precedent; plain
  list/detail/summary reads are NOT audited.

---

## 9. Security invariants

- **#5 Tenancy:** RLS on all six tables; every query carries an explicit
  `org_id` predicate (defense in depth). Every cross-reference — `product_id`,
  `vendor_id`, allocation `target_id`, review-queue `product_id` — is resolved
  `WHERE org_id AND id`, rejecting another org's id as indistinguishable from
  nonexistent (`LicenseValidationError`). The matching engine reads only the
  caller org's `installed_software`.
- **#6 RBAC (server-side):** three permissions —
  `licenses.read` (view catalog, licenses, allocations, compliance, reports),
  `licenses.manage` (create/edit vendors, products, licenses, allocations,
  review-queue actions),
  `licenses.keys.read` (reveal a decrypted key — the sensitive, audited action).
  Migration-seeded grants: super-admin & org-admin = all three; **technician =
  read + manage (NOT keys.read)**; auditor & read-only = read only. Key reveal is
  least-privilege (org-admin/super only) like credential checkout. The `PERMISSIONS`
  registry in `app/core/rbac.py` gains all three, and the perm-set tests
  (`test_rbac`, `test_api_routes`) are updated in the same task that adds the
  routes (the Phase-7 lesson).
- **#4 Audit (hash-chained):** entitlement changes (license create/update/delete),
  allocation create/release, key reveal, vendor/product create/update, review-queue
  map/ignore, and CSV export are audited with the correct `target_type` and
  `actor_type="user"` (sweep writes snapshots as data, not audit). **No-audit-on-
  view:** lists, detail, compliance/renewal/cost reports write nothing.
- **#3 Secrets:** only license keys are sensitive; handled per §5. No plaintext key
  in DB, responses, or logs.
- Errors: typed `LicenseNotFoundError(LicenseValidationError)`; the API maps
  NotFound → 404, other validation → 400 **by isinstance**, never by message
  string, never 500 (the Phase-9 lesson).

---

## 10. API (routes under `/api/v1`)

Vendors: `GET/POST /vendors`, `GET/PATCH /vendors/{id}`.
Products: `GET/POST /licenses/products`, `GET/PATCH /licenses/products/{id}`.
Licenses: `GET/POST /licenses`, `GET/PATCH/DELETE /licenses/{id}`,
`GET /licenses/{id}/key` (reveal, `licenses.keys.read`).
Allocations: `POST /licenses/{id}/allocations`, `GET /licenses/{id}/allocations`,
`POST /allocations/{id}/release`.
Reconciliation: `GET /licenses/compliance`, `GET /licenses/review-queue`,
`POST /licenses/review-queue/map`, `POST /licenses/review-queue/ignore`.
Reports: `GET /licenses/renewals`, `GET /licenses/costs`, `GET /licenses/export`.
Read routes → `licenses.read`; writes → `licenses.manage`; key reveal →
`licenses.keys.read`. `register(app)` mounts the router ONLY (no job types, no
gateway edit). Route-ordering caution: declare static segments
(`/licenses/products`, `/licenses/compliance`, `/licenses/review-queue`,
`/licenses/renewals`, `/licenses/costs`, `/licenses/export`) **before**
`/licenses/{id}` so they aren't captured as an id.

---

## 11. Frontend (`frontend/src/modules/licenses/`)

`types.ts` mirrors the backend response models exactly (no key field; `has_key`
boolean). `licenseState.ts` maps backend vocab → `BadgeVariant` with `?? "muted"`
fallback (license `type`; a compliance `breach`/`ok` badge; renewal
`overdue`/`due-soon`/`ok`) — real backend values only (the Phase-5 lesson).
Pages: VendorsPage; ProductsPage + a **match-rule editor**; LicensesPage +
LicenseDetail (key reveal button gated on `licenses.keys.read`, hidden without
it; the revealed key shown once, never stored in query cache); AllocationsPanel;
ComplianceDashboard (entitled/installed/delta, breach badges); ReviewQueue (map /
ignore actions, gated `licenses.manage`); RenewalCalendar. Nav item
`{ to: "/licenses", label: "Licenses" }` gated on
`permissions.includes("licenses.read")`. Strict TS, no `any`.

---

## 12. Testing (test-first, security paths explicit)

- Matching engine: exact/prefix/regex + publisher constraint; a name matching
  multiple rules counts its device once; regex rejection at write time; the
  review queue surfaces only unmatched, non-ignored names with correct
  device_count; map appends a rule; ignore hides a name.
- Compliance: entitled/installed/delta with hand-computed literals; breach when
  installed > entitled; multi-device dedup; per-org isolation (another org's
  installs/licenses never affect the count) — with an RLS-bypassing admin-session
  test proving the app-level `org_id` predicate is load-bearing (the Phase-10
  lesson).
- Keys: seal on write; list/detail never expose key or `key_sealed` (only
  `has_key`); reveal returns plaintext and writes exactly one `license.key_revealed`
  audit; reveal is 403 without `licenses.keys.read`.
- Allocation: pool enforcement (allocate to the cap, then 400); release; released
  allocations free the pool; cross-org target rejected.
- Sweep: writes one snapshot per product per run; org-scoped; idempotent shape.
- API: every route RBAC-gated with the correct code; NotFound→404 / validation→400
  never 500; export audited, plain reads not; route ordering.
- Full suite green; mypy (strict) + ruff + frontend typecheck/build clean.

---

## 13. Deploy

`deploy/README.md` gains a "Licenses (SAM)" section: control-plane only (no agent
capability, no new compose services); reuses Postgres, the vault (per-org
envelope for keys), the audit log, and the existing Celery/Redis worker for the
daily compliance sweep. `.env.example` documents the optional
`LICENSES_COMPLIANCE_SWEEP_*` cadence override and states no other new vars.
`docker compose config` must validate.

---

## 14. Deferred (explicitly not in v1)

- **Usage metering** (which installed apps actually run → harvest/reclaim unused
  seats) — requires resident process sampling on the agent; blocked on the idle-
  footprint budget; revisit only with an event-driven design.
- **Managed agreement-file upload** to object storage — `agreement_ref` is an
  optional reference string in v1; binary upload/download is a fast-follow
  (reuse the existing ObjectStore when added).
- Core/CAL/suite license math; downgrade/upgrade rights.
- Procurement workflow (request → approve → purchase → allocate).
- Push/email alerting; per-org base currency + FX roll-up (Budget, Phase 13);
  user-based (not device-based) reconciliation for per-user models;
  Subscriptions hybrid-product linkage (Phase 12).
