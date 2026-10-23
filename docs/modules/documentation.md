# Module — IT Documentation

> Scoping brief. Phase 8 by default, but **low-dependency** and can be pulled
> earlier (e.g. right after Inventory) as a lighter phase between heavy ones. See
> `docs/06-roadmap.md`. Expand into a full spec via the brainstorming skill before
> implementation.

## Purpose

A structured, asset-linked IT knowledge base and runbook repository — the ITGlue /
IT Glue-style "documentation that's connected to the things it documents." Because
the platform already knows the fleet, documentation can link directly to devices,
groups, credentials (references, not secrets), and procedures.

## Scope — v1

- **Documents & runbooks:** rich-text/Markdown articles, procedures, and runbooks,
  organized by category and searchable.
- **Asset linking:** attach documentation to `Device`, `DeviceGroup`, or the org
  (e.g. "server build runbook" linked to all servers; "site network diagram"
  linked to a location).
- **Structured records:** typed documentation objects for common IT assets —
  network/IP records, service/application records, vendor/contract records,
  location records — with fields, not just prose.
- **Credential references:** link to vault credential **entries** (metadata) so a
  runbook can say "uses the X admin account" without exposing the secret; opening
  the actual credential still goes through the audited vault checkout.
- **Versioning & audit:** document history; who changed what, when.
- **Access control:** RBAC-scoped visibility (auditors read-only, technicians per
  scope).

## Later phases

- Auto-generated documentation from inventory (e.g. per-device fact sheets).
- Relationship graph across assets (this app runs on these servers, backed up by
  this job, covered by this BCP plan).
- Templates and required-field policies for standardized documentation.

## Agent capability

- **None required** for core v1 (this is control-plane data). Optional later:
  auto-population from inventory facts (already collected by the inventory
  capability — no new agent work).

## Server side

- Document store with categories, search, versioning; typed structured records;
  linking to core entities; RBAC-scoped access; audit of changes.

## Data model additions

- `doc_article` (org_id, title, body, category, created_by, updated_at)
- `doc_link` (org_id, article_id, target_type, target_id) — links to
  device/group/credential-ref/etc.
- `doc_record` (org_id, record_type, fields jsonb) for typed asset records
  (network, service, vendor, location)
- `doc_version` (article_id/record_id, version, snapshot, changed_by, ts)

## Integration points

- **Inventory** (link docs to real assets; later auto-populate).
- **Credential vault** (reference entries without exposing secrets).
- **Change management / BCP** (link procedures and DR plans to the assets they
  cover).

## Open questions (resolve in spec)

- Editor and content format (Markdown vs rich-text/WYSIWYG) and search backend.
- Which typed record types ship in v1 vs later.
- How tightly to couple to inventory auto-population in v1 (probably defer to keep
  the phase light).
