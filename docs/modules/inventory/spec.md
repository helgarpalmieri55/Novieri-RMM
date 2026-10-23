# Phase 1 — Inventory Module Specification (control plane)

Status: approved (autonomous-build delegation, 2026-07-17)
Source brief: `docs/modules/inventory.md`. Kernel contracts: `docs/kernel/spec.md`.

## 1. Scope

Control-plane side of the inventory module: result ingestion, storage, change
history, dynamic grouping, API, exports, and fleet UI. The agent-side collector
(per-OS sources, event hooks, local schedule) is **not built**; it belongs
under `agent/` in this repository and is specified by the wire contract this
module defines — the `inventory.collect`
**result document schema** is exported alongside the kernel wire protocol.

Out of scope for this phase: licence reconciliation, warranty lookups, USB
history, golden-baseline drift (brief's "later phases"); agent-side event
hooks; policy-driven sweep scheduling delivery to agents (arrives with the
agent cycle, still unbuilt — the module seeds a default `inventory` policy row
so the cadence is configurable data from day one).

## 2. Resolutions of the brief's open questions

- **Sweep cadence default:** 24h, stored as the org-wide default `Policy`
  (`module="inventory"`, `config={"interval_hours": 24}`), seeded at org
  creation-time by ingestion if missing. Event hooks are an agent-side concern, unbuilt (`agent/`).

  **Scheduled collection (2026-09-23).** The server now dispatches it; the
  agent schedules nothing. `app/modules/inventory/schedule.py`: the
  `inventory-collect-sweep` beat (hourly) sends a signed `inventory.collect` to
  each online, unrevoked device with none in flight and none finished within
  5/6 of `interval_hours` (20 h of the default 24). A pass takes at most
  `max(25, ceil(online/12))` devices, never-collected first, so a fleet's first
  refresh is spread over about half a day. The gateway also queues one on an
  agent's first connection after enrolment (`agents.last_seen_at IS NULL`),
  delivered by the connect-time drain. Both are audited
  `inventory.collect_scheduled` with `trigger` `scheduled` / `first_connect`.
- **History retention:** `inventory_changes` kept indefinitely (small rows,
  compliance value). Snapshots: keep the **latest 10 per device**; older ones
  pruned at ingest time. No rollup in v1.
- **Normalization:** summary stays on `Device` (existing columns); itemized
  hardware in `hardware_components` (typed rows + jsonb attributes); software
  fully normalized in `installed_software`; the raw document also stored on the
  snapshot for debugging/replay.

## 3. Module layout (registers into the kernel; forks nothing)

```
backend/app/modules/inventory/
├── __init__.py      # register_module(): job-type result handler registration
├── schema.py        # InventoryDocument — the agent result contract (exported)
├── ingest.py        # ingest_inventory(session, device, doc) — the core service
├── rules.py         # dynamic device-group rule evaluation
└── api.py           # /api/v1 routes (inventory detail, changes, software, exports)
frontend/src/modules/inventory/   # DeviceDetailPage tabs + fleet SoftwarePage
backend/migrations/versions/0005_inventory.py
```

**Kernel extension point (the one kernel change):** `JobType` gains an optional
`result_handler: Callable[[AsyncSession, Job, dict], Awaitable[None]]`.
`JobEngine.complete()` invokes it after a successful result, inside the same
tenant session/transaction. Modules register handlers at import; the kernel
never imports module code except via `app.modules.register_all(app)`.

## 4. Data model (migration 0005 — all tables org_id + FORCE RLS)

- `inventory_snapshots`: `id`, `org_id`, `device_id` FK, `taken_at`,
  `raw` jsonb (full document), `software_count` int, `changes_count` int.
- `installed_software`: `id`, `org_id`, `device_id` FK, `name`, `version`,
  `publisher`, `install_date` (date, nullable); unique `(device_id, name)`.
  Index `(org_id, name)` for fleet-wide search.
- `inventory_changes`: `id`, `org_id`, `device_id` FK, `ts`,
  `change_type` (`software_installed|software_removed|software_updated|os_updated|fact_changed`),
  `subject` (e.g. software name or fact key), `from_value`, `to_value` (text,
  nullable). Index `(org_id, device_id, id)`.
- `hardware_components`: `id`, `org_id`, `device_id` FK, `type`
  (`cpu|memory|disk|nic|gpu|display|battery|bios|motherboard`), `identifier`
  (stable within device+type, e.g. disk serial or NIC MAC), `attributes` jsonb;
  unique `(device_id, type, identifier)`. Replaced wholesale each sweep.

The RLS-completeness test from Phase 0 automatically requires policies on all
four tables; migration 0005 creates them with the same `tenant_isolation`
pattern as 0002.

## 5. Result document contract (`schema.py`)

`InventoryDocument` (Pydantic, `extra="ignore"` so newer agents can send more):

```
hostname: str;  fqdn: str|None;  os_family: windows|linux|macos
os_version: str;  os_build: str|None;  architecture: str;  serial: str|None
hardware: { cpu: str, ram_bytes: int, components: [ {type, identifier, attributes{}} ] }
software: [ { name, version, publisher?: str, install_date?: YYYY-MM-DD } ]
facts: { domain_joined?: bool, disk_encryption?: bool, firewall_enabled?: bool, ... free-form }
```

Exported as JSON Schema to `docs/kernel/wire-protocol/InventoryDocument.schema.json`
by the existing export script — this is what the Go collector builds against.
A malformed result document fails the job (job flips to `failed` with a
validation error in `result`) rather than half-ingesting.

**What the agent sends (2026-09-23).** `software[]` comes from the Uninstall
keys (HKLM 64/32-bit views and loaded HKU hives, minus system components and
updates) plus Store/MSIX packages on Windows; `/Applications` bundles on macOS;
dpkg or rpm on Linux. It is deduplicated by name, sorted, capped at 4,096, and
`version`/`publisher` are clamped to the column widths (100/200). Facts keys,
each absent when unknown: `last_boot` (RFC 3339 UTC), `console_user`,
`manufacturer`, `model`, `domain_joined` and `entra_joined` (Windows),
`disk_encryption` (BitLocker on the system drive, or FileVault),
`software_sources` (the databases read, e.g. `["registry","appx"]`) and
`software_truncated` (only when the cap was hit). All of it rides in the
existing free-form `facts` map, so no schema change was needed and older
agents are unaffected.

## 6. Ingestion (`ingest.py`) — runs in the job's tenant transaction

1. Validate the raw result into `InventoryDocument` (failure ⇒ raise
   `InventoryValidationError`; the result handler catches it, marks the job
   failed with the message, audits `inventory.ingest_rejected`).
2. Upsert `Device` summary: hostname, fqdn, os_version, architecture, serial,
   cpu, ram_bytes, disks/nics jsonb (from components), `last_seen_at`.
3. Diff software: previous `installed_software` vs document — insert new rows,
   delete removed, update changed versions; write one `inventory_changes` row
   per difference (`from_value`/`to_value` carry versions). A device with no
   `installed_software` rows yet treats its first list as a baseline, exactly
   like a first sweep: agents before 2026-09-23 reported no software, and
   their first upgraded sweep is not hundreds of installs.
4. Detect `os_updated` (os_version changed) and `fact_changed` (tracked facts:
   domain_joined, disk_encryption, firewall_enabled) changes; record them. The
   current facts live in the snapshot raw document (no separate facts table in
   v1).
5. Replace `hardware_components` (delete + insert; identifiers keep the diff
   cheap later, but v1 does not diff hardware).
6. Insert `inventory_snapshots` row (raw doc + counters); prune to the latest
   10 for the device.
7. Evaluate dynamic group rules (§7) against the fresh facts.
8. First successful sweep for an org seeds the default inventory `Policy` row
   if none exists.

No audit event for a routine successful sweep (jobs.succeeded already exists);
rejected ingests are audited.

## 7. Dynamic grouping (`rules.py`)

`DeviceGroup.rules` jsonb, shape: `{"all": [{"fact": <path>, "op": <op>, "value": <v>}]}`
— every condition must hold. Ops: `eq`, `ne`, `contains` (case-insensitive
substring), `gt`, `lt` (numeric). Fact paths cover the flat namespace:
`hostname`, `os_family`, `os_version`, `architecture`, `ram_bytes`, `serial`,
plus `facts.<key>` and `software:<name>` (value = installed version or absent).
`serial` was in the code and missing from this list; both are now read from
`rules.FLAT_FACTS`, which is also what builds `_fact_value`'s table.

**The document is CLOSED on write.** `GroupRules` (in `rules.py`, beside the
evaluator it is derived from) refuses on `POST`/`PATCH`/`preview` anything the
evaluator could not run: an unresolvable fact path, a `gt`/`lt` value `float()`
rejects, an empty `all`, an unknown op, an unknown key anywhere. The first of
those is the one that matters — `_fact_value` returns `_MISSING` for a typo and
`_condition_holds` answers **True** for `ne` on a missing fact, so
`{"fact": "os_familly", "op": "ne", "value": "windows"}` claims every device in
the org and reads like a reasonable rule.

`evaluate_rules(session, device, doc, org_id=...)` runs after ingest: for each
LIVE rule-bearing group in that org (deterministic order: `(precedence, name)`
— migration 0072; it was `name` alone until then, which made the alphabet the
precedence list and a rename a way to re-segment a fleet), the first matching
group claims the device (`device.device_group_id`). A device matching no
rule-bearing group keeps whatever group it was in. Malformed rules are skipped
and logged, never fatal to ingestion.

**CORRECTION, 2026-08-26.** This paragraph used to end *"rule groups never
unassign manual placements — only moves between rule groups or from NULL are
automatic"*, and that was **false against `rules.py`**. The loop assigns
`device.device_group_id = group.id` whenever a rule matches, whatever was there
before; only the NO-MATCH case preserves a placement, which is all
`test_no_match_preserves_manual_placement` ever covered. A device hand-placed
into a rule-bearing group was therefore evicted on the next collection, with no
message anywhere — and a reader of this sentence would have concluded the
opposite.

The claim is now true by CONSTRUCTION rather than by correction, which is the
only reason to leave a sentence like it in a spec at all. Since
`docs/superpowers/specs/2026-08-22-device-group-management-design.md` §10.3, a
group is hand-curated **or** rule-driven and never both: `POST
/device-groups/{id}/members` on a rule-bearing group is a 409, and `PATCH`
adding rules to a group that holds hand-placed devices is a 409 naming the
count. So there is no longer a manual placement inside a rule group for the
evaluator to overwrite.

**Membership is also re-evaluated outside ingest.** Collection is daily
(§2, scheduled collection), so a rule authored in the console would otherwise
take effect only on the next ingest, up to a day later. The
`device-group-reevaluate` beat (design §5.3, 300s) replays each active device's
latest `InventorySnapshot` through this same evaluator, per org, inside that
org's `org_session`. `POST /device-groups/preview` shows an author what a
candidate document claims before it is saved.

## 8. API (`api.py`, all `devices.read` unless noted)

- `GET /api/v1/devices/{id}/inventory` → `{summary, hardware: [components],
  software: [...], facts, snapshot_taken_at}` (404 outside org, as usual).
- `GET /api/v1/devices/{id}/inventory/changes?limit&offset` → change history,
  newest first.
- `GET /api/v1/software?name=&limit&offset` → fleet-wide: distinct
  `(name, version, publisher)` with device counts; `?name=` filters by
  substring.
- `GET /api/v1/software/{name}/devices` → devices having that software (+
  version each).
- **Exports:** `GET /api/v1/export/devices.csv` and
  `GET /api/v1/export/software.csv` — streamed CSV, Power-BI-friendly flat
  columns. Same permission; content-type `text/csv`. `.xlsx` siblings of both
  render the same rows through the same builder.

  **The device columns, in order, and the order is part of the contract:**
  `hostname, os_family, os_version, architecture, serial, agent_status,
  last_seen_at, source, provenance`.

  `source` and `provenance` were **appended** on 2026-08-26, the day a
  `net.sweep` began recording every host it finds as an asset row: in a swept
  estate most rows are machine-recorded, and an export that renders the coffee
  machine identically to the laptop somebody typed in is an export nobody can
  act on. `provenance` is the whole answer to "which of these did a person
  vouch for?"; `source` rides with it because
  `ck_devices_source_provenance` makes the two one fact in two columns.

  **New columns are appended, never inserted.** These are shipped GET routes
  and this repository cannot enumerate what reads them — a scheduled fetch, a
  CMDB import, a spreadsheet's own web query. A consumer keyed on header name
  is unaffected by an addition anywhere; one keyed on column index is
  unaffected only if the addition is at the end. Removing or reordering a
  column is a breaking change and needs a second route, not an edit.

## 9. Frontend

- **Device detail page** (`/devices/:id`): summary card (OS, hardware, agent
  status, facts badges for encryption/firewall/domain) + tabs: Hardware
  (component table), Software (searchable table), Changes (history list),
  Jobs (existing job list filtered to the device, with dispatch button).
- **Software page** (`/software`): fleet-wide searchable software table;
  clicking a row lists the devices that have it.
- Devices grid gains a link to the detail page and a CSV export button.
- Live updates: existing `useLiveEvents` invalidation covers job completion;
  an `inventory_updated` UI event is published by the result handler so the
  detail page refreshes when a sweep lands.

## 10. Testing

- Ingestion: first sweep populates everything; second sweep with
  installs/removals/updates produces exactly the expected `inventory_changes`;
  malformed document fails the job and ingests nothing (transactional);
  snapshot pruning keeps 10.
- Rules: each op; first-match assignment in `(precedence, name)` order; a
  device no rule claims keeps its group; malformed rules skipped; an ARCHIVED
  rule group claims nothing; the evaluator's `org_id` predicate holds on the
  BYPASSRLS admin engine (`tests/test_inventory_rules.py`), which is the engine
  the re-evaluation sweep's siblings run on.
- API: RBAC 403s, org isolation (404 for foreign devices — RLS-backed),
  software search, CSV exports parse and contain expected rows.
- E2E extension: the Phase 0 kernel e2e's fake agent answers
  `inventory.collect` with a realistic document; asserts device summary,
  software rows, change history, and UI event.
- Frontend: vitest for the inventory API client additions and the facts-badge
  mapping; `tsc` strict + build.
