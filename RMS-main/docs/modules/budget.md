# Module — IT Budget & Expense Management

> Scoping brief. Phase 13 by default — last of the financial trio because it
> **consumes** Licenses (11) and Subscriptions (12) as auto-feeds, though it can
> run standalone on manual entries if pulled earlier. Expand into a full spec via
> the brainstorming skill before implementation.

## Purpose

Plan IT budgets, record and categorize IT spend, and track actuals against plan —
with the platform's unfair advantage: license renewals and subscription billing
cycles it already knows about become **auto-generated expense entries and a
committed-spend forecast**, so the budget picture stays current with minimal data
entry, and every cost line can link to the asset, license, subscription, or
change that explains it.

## Boundary vs the ERP (important)

**This is not an accounting system.** Odoo (or the customer's ERP) remains the
system of record for invoices, payments, and the general ledger. This module is
the **IT lens**: category-level budget vs actuals, renewal-driven forecasting,
and cost lines linked to the technical objects this platform manages — exported
or synced *to* the ERP, never replacing it.

## Scope — v1

- **Budget definition:** fiscal year with period granularity (month/quarter);
  categories (hardware, software licenses, SaaS subscriptions, cloud/hosting,
  telecom, services, maintenance — org-configurable); optional cost centers;
  planned amount per category × period.
- **Expense records:** date, amount, original currency + base-currency conversion
  (FX rate captured at entry), vendor (shared `vendor` entity), category, cost
  center, description, **optional link** to license / subscription / device /
  change record, invoice attachment (object storage), entered-by, status.
- **Auto-fed entries:** subscription billing cycles and license
  maintenance/renewals generate **expected** expense entries automatically; a
  human confirms them (amount may differ) → status flows expected → confirmed.
  This is what keeps actuals honest without manual bookkeeping.
- **Committed-spend forecast:** the forward calendar of known renewals and
  billing cycles projected per category × period — "what is already committed
  for the next N months" — alongside plan and actuals.
- **Dashboards & variance:** actual vs planned vs committed per category, period,
  and cost center; burn rate; year-to-date position.
- **RBAC scoping:** budget and expense visibility restricted to finance-relevant
  roles (this is sensitive data even inside IT).
- **Exports:** CSV / Power BI; **Odoo export** of categorized expense lines
  (one-way in v1).

## Later phases

- Two-way Odoo sync (pull confirmed invoice amounts back; map cost centers to
  Odoo analytic accounts).
- Purchase-request / approval workflow feeding planned spend.
- Hardware depreciation view and per-device TCO (links `Device` purchase data +
  attributed licenses/subscriptions/support).
- Multi-year planning and scenario comparison.

## Agent capability

- **None.** Entirely control-plane.

## Server side

- Budget/period/category modeling; expense CRUD with linking and attachments;
  the auto-feed generators (subscription cycles, license renewals) and the
  expected→confirmed flow; FX conversion at entry; forecast computation;
  dashboards; exports.

## Data model additions

- `budget` (org_id, fiscal_year, name, base_currency)
- `budget_line` (org_id, budget_id, period, category, cost_center_id,
  planned_amount)
- `cost_center` (org_id, code, name)
- `expense` (org_id, date, amount, currency, fx_rate, amount_base, vendor_id,
  category, cost_center_id, description, source: manual / subscription /
  license, link_type, link_id, invoice_ref, entered_by, status:
  expected / confirmed / void)
- Category set stored per org (configurable), seeded with the defaults above.

## Integration points

- **Subscriptions** and **Licenses** (primary auto-feeds for expected entries and
  the committed forecast).
- **Vendor** shared entity (introduced in Licenses).
- **Odoo** (v1 export; later sync — consistent with the change-management
  module's Odoo posture).
- **Inventory / Device** (hardware purchase linkage now; TCO later).
- **Change management** (an expense can reference the change that caused it).
- **Power BI** / CSV for finance reporting.
- **Audit** for budget edits and expense confirmation.

## Open questions (resolve in spec)

- Base currency and FX-rate source (manual rate at entry vs a rate feed) — the
  COP/USD reality makes this a v1 decision, and Licenses/Subscriptions must use
  the same convention.
- Cost-center model: free-standing vs mirroring Odoo analytic accounts from the
  start.
- Whether expected entries auto-confirm after N days or always require a human.
- Odoo export format/mechanism v1 (CSV drop vs API push).
- How far RBAC granularity goes (own-cost-center visibility vs org-wide).
