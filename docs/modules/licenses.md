# Module — Licenses Management

> Scoping brief. Phase 11 by default — but like IT documentation, this is
> **low-dependency** (needs only Inventory, Phase 1) and can be pulled earlier as
> a lighter phase between heavy ones. First of the financial trio (licenses →
> subscriptions → budget); introduces the shared `vendor` entity the other two
> reuse. Expand into a full spec via the brainstorming skill before
> implementation.

## Purpose

Track software licenses and entitlements — perpetual, volume, OEM, CALs,
per-device/per-user — and reconcile them against **actual installations from
inventory**, so the org always knows its compliance position (over- or
under-licensed), what renewals are coming, and what each entitlement cost. The
SAM (software asset management) capability, scoped to what an IT team actually
uses: compliance position, renewal control, and vendor-audit readiness.

## Boundary vs Subscriptions

One rule, stated once and enforced in both modules: **software installed on
devices → Licenses; cloud/SaaS seat services → Subscriptions.** Hybrid products
(e.g. Microsoft 365 covering both desktop apps and cloud seats) live in
Subscriptions with a license-side link where install reconciliation matters.

## Scope — v1

- **License records:** product, vendor, license type (perpetual / volume / OEM /
  CAL / per-device / per-user), entitlement count, purchase data (cost, currency,
  PO ref, date), maintenance/support expiry, renewal date, notes, attachments
  (agreement PDFs → object storage).
- **License keys stored encrypted** at rest (per-org key), access **audited** —
  lighter than vault-grade credential handling, same philosophy: never plaintext
  in responses or logs.
- **Reconciliation against inventory:** matching rules map messy
  `installed_software` names to normalized products; per-product compliance
  position = entitled vs installed vs delta. This normalization engine is the
  crux of the module.
- **Allocation:** assign license seats to devices or users; track the available
  pool.
- **Alerts:** renewal/maintenance expiry approaching; compliance breach (installs
  exceed entitlements).
- **Reporting:** compliance summary, renewal calendar, true-up preparation,
  per-product cost view; CSV / Power BI export.

## Later phases

- **Usage metering** (which installed apps actually run) to drive
  harvest/reclaim of unused seats — *explicitly deferred* because it requires
  resident process sampling on the agent, which pulls against the idle-footprint
  budget; revisit with an event-driven design only.
- Downgrade/upgrade rights and license-model math (cores, CAL suites).
- Procurement workflow (request → approve → purchase → allocate).

## Agent capability

- **None new in v1.** Software presence comes from the existing
  `inventory.collect` capability. Any future metering must clear the idle-budget
  bar first (see above).

## Server side

- Product catalog + **matching-rules engine** (normalize installed-software names
  to products); compliance computation on inventory refresh; renewal scheduler
  and alerting; encrypted key storage with audited access.

## Data model additions

- `vendor` (org_id, name, website, contacts jsonb) — **shared entity**, reused by
  Subscriptions and Budget; the Documentation module's vendor records link to it.
- `software_product` (org_id, name, publisher, match_rules jsonb)
- `license` (org_id, product_id, vendor_id, type, entitlements, key_encrypted,
  cost, currency, po_ref, purchased_at, maintenance_expires_at, renews_at,
  agreement_ref, notes)
- `license_allocation` (org_id, license_id, target_type: device/user, target_id,
  allocated_at, released_at)
- `license_compliance_snapshot` (org_id, product_id, entitled, installed, delta,
  computed_at)

## Integration points

- **Inventory** (hard dependency): installed-software data drives reconciliation.
- **Budget**: purchases and maintenance renewals feed expense/committed-spend
  entries automatically.
- **Subscriptions**: shared `vendor`; the boundary rule above.
- **Documentation**: agreements/contracts linked; vendor doc-records reference
  `vendor`.
- **Audit**: key access and entitlement changes are audit events.

## Open questions (resolve in spec)

- Matching-rule mechanics: curated per-product rules vs heuristic matching with a
  review queue for unmatched software (recommend rules + review queue).
- License models to support in v1 (flat per-device/per-user first; core/CAL math
  later?).
- Whether key storage should escalate to full vault handling for high-value keys.
- Currency handling — align with the Budget module's base-currency decision so
  costs roll up consistently.
