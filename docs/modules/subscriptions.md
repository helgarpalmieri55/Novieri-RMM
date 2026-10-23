# Module — Subscriptions Management

> Scoping brief. Phase 12 by default — low-dependency (kernel only; Licenses
> Phase 11 supplies the shared `vendor` entity) and can be pulled earlier.
> Second of the financial trio. Expand into a full spec via the brainstorming
> skill before implementation.

## Purpose

A registry and control point for recurring SaaS and cloud subscriptions — Claude,
OpenAI, Canva, Microsoft 365, Adobe, hosting, and the long tail — tracking plans,
seats, costs, owners, and renewal cycles, so the org knows what it pays for,
who uses it, and **when the window to cancel or renegotiate closes**. The SaaS
management capability (Torii/Zylo category) scoped to registry, seats, renewals,
and spend visibility.

## Boundary vs Licenses

**Cloud/SaaS seat services → here; device-installed software → Licenses.**
Hybrid products (e.g. Microsoft 365) live here, with a license-side link where
install reconciliation matters. (Same rule stated in the Licenses brief.)

## Scope — v1

- **Subscription records:** service, vendor (shared `vendor` entity), plan/tier,
  category, billing cycle (monthly/annual/other), cost per cycle + currency,
  seats purchased, internal **owner** (responsible operator), start date, renewal
  date, **auto-renew flag**, **cancellation-notice period**, payment reference,
  status (active/cancelled/trial).
- **Seat assignments:** seats purchased vs assigned; v1 assigns to lightweight
  person records (name + email — the platform's `User` table is operators only,
  and seat holders are employees at large). Directory-driven reconciliation is a
  later phase.
- **Renewal & notice alerts:** alert **before the cancellation-notice cutoff**,
  not merely before the renewal date — the notice deadline is the decision point;
  after it passes, the renewal is committed.
- **Cost normalization:** monthly-equivalent and annualized cost per
  subscription; roll-ups by vendor, category, and owner.
- **Reporting:** total SaaS spend, upcoming renewals/notice deadlines, unassigned
  (paid-but-idle) seats, per-owner portfolios; CSV / Power BI export.
- **Change history:** price changes, seat changes, plan changes recorded as
  events.

## Later phases

- **Entra ID / Google Workspace reconciliation:** sync directory + sign-in data
  to detect dormant seats (assigned but unused) and offboarding leaks (departed
  employee still holding seats).
- Shadow-IT discovery hints from SSO/identity-provider app data.
- Spend-anomaly alerts (price hikes, seat creep).
- Contract/quote document parsing to prefill records.

## Agent capability

- **None — deliberately.** SaaS usage discovery via endpoint monitoring (browser
  tracking) is out of scope on principle: it is privacy-invasive and would fight
  the agent's idle budget. Utilization insight comes from identity-provider data
  (later phase), not from watching endpoints.

## Server side

- Subscription registry; seat-assignment tracking; renewal/notice scheduler and
  alerting; cost normalization and roll-ups; event history; exports.

## Data model additions

- `subscription` (org_id, vendor_id, service_name, plan, category,
  billing_cycle, cost_per_cycle, currency, seats, owner_user_id, started_at,
  renews_at, auto_renew, notice_days, payment_ref, status)
- `seat_assignment` (org_id, subscription_id, assignee_name, assignee_email,
  assigned_at, released_at)
- `subscription_event` (org_id, subscription_id, type: renewal / price_change /
  seat_change / plan_change / cancel, ts, details jsonb)

## Integration points

- **Budget** (primary consumer): each billing cycle generates an expected expense
  entry automatically; renewals ahead form the committed-spend forecast.
- **Licenses**: shared `vendor`; the boundary rule above.
- **Documentation**: contracts and vendor records linked.
- **Entra ID / AD** (later): directory + sign-in reconciliation.
- **Notifications**: renewal/notice alerts to owners.

## Open questions (resolve in spec)

- Person records for seat holders in v1: free-form name+email vs an early
  minimal employee directory synced from Entra (recommend free-form now, sync
  later — keeps the phase light).
- Category taxonomy: fixed set vs org-configurable.
- Multi-currency normalization — align with Budget's base-currency/FX decision.
- Whether payment references should link to a payment-method entity in Budget or
  stay free-text in v1.
