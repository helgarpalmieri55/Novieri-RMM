# Module — Change Management

> Scoping brief. Phase 9. Depends on the modules whose actions it governs (patch,
> deployment, provisioning) and integrates Odoo Helpdesk. Expand into a full spec
> via the brainstorming skill before implementation.

## Purpose

An IT change-management workflow that records, reviews, approves, and tracks
changes to the environment — and ties the platform's own actions (patch
campaigns, deployments, provisioning) into formal change records. Scoped to IT
change, integrating with **Odoo Helpdesk** rather than reimplementing a full
service desk. This is a **SOC 2 evidence** engine.

## Scope — v1

- **Change requests:** create a change with type (standard/normal/emergency),
  description, risk, affected assets (linked to `Device`/`DeviceGroup`), planned
  window, and rollback plan.
- **Approval workflow:** RBAC-gated review/approval; approval states and history;
  emergency-change fast path with retrospective approval.
- **Change calendar:** scheduled changes and maintenance windows in a fleet
  calendar; conflict awareness.
- **Link to platform actions:** associate a change with the patch campaigns,
  deployments, or provisioning runs that implement it, so the record reflects what
  actually happened (with audit references).
- **Odoo Helpdesk integration:** sync change records/tickets with Odoo (create/
  update/close), consistent with existing Toeshee ticketing.
- **Records & evidence:** immutable change history suitable for SOC 2 (what
  changed, who approved, when, with what result).

## Later phases

- CAB (change advisory board) workflows and scheduled review meetings.
- Automated change creation from risky operations (e.g. a patch ring auto-opens a
  change).
- Risk scoring from inventory/Sentinel context.

## Agent capability

- **None directly.** Change management governs and records; the *actions* it
  tracks are executed by other modules' agent capabilities (patch, deploy,
  provision), which already flow through the signed-job runner and audit log.

## Server side

- Change workflow engine (states, approvals, calendar); linking to module actions
  and audit events; Odoo Helpdesk sync; evidence export.

## Data model additions

- `change_request` (org_id, type, title, description, risk, affected jsonb,
  window_ref, rollback_plan, state, created_by)
- `change_approval` (org_id, change_id, approver, decision, ts, comment)
- `change_link` (org_id, change_id, action_type, action_id) — ties to
  patch_run/deployment/provision_run
- `change_external_ref` (org_id, change_id, system: odoo, external_id)

## Integration points

- **Patch / deployment / provisioning** (the actions changes govern; link via
  `change_link` + audit).
- **Odoo Helpdesk** (ticket sync — dependency for the integration piece).
- **Documentation** (link change procedures/runbooks).
- **BCP** (major changes may require BCP re-test).
- **SOC 2 evidence** (change records are core audit evidence).

## Open questions (resolve in spec)

- Depth of Odoo integration in v1 (one-way push vs two-way sync).
- Which platform actions must be change-gated vs merely change-linked, and whether
  gating is enforced (block the action without an approved change) or advisory.
- Change types and approval matrices for the internal deployment first.
