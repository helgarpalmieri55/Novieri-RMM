# Module — Inventory Management

> Scoping brief. Phase 1. This is the first real module and the proving ground
> for the agent → signed-job → result → UI loop. Expand into a full spec via the
> brainstorming skill before implementation (`docs/06-roadmap.md`).

## Purpose

Automatic, continuously refreshed hardware and software inventory of every
managed endpoint, forming the fleet-of-record that every other module builds on
(patch targets, deployment targets, backup scope, provisioning baselines).

## Scope — v1

- **Hardware inventory:** CPU, RAM, disks (model/size/health where available),
  NICs/MAC/IP, motherboard/serial/BIOS, GPU, connected displays, battery (mobile).
- **Software inventory:** installed applications and versions per OS, OS build and
  patch level, running services, key configuration facts (domain membership, disk
  encryption status, firewall on/off).
- **Change detection:** diff each sweep against the last; record installs,
  removals, and version changes as inventory history.
- **Dynamic grouping input:** expose inventory facts for rule-based
  `DeviceGroup` membership and tag rules (e.g. "all Windows Server 2019", "all
  machines with < 20 GB free").
- **Fleet UI:** searchable, filterable, sortable device grid (virtualized for
  large fleets); per-device detail view; export (CSV / API for Power BI).

## Later phases

- Software licence/usage reconciliation.
- Warranty lookups via vendor APIs.
- Peripheral and USB history.
- Baseline/drift comparison against a "golden" configuration.

## Agent capability

- Job type `inventory.collect` (on schedule and on-demand).
- **Event-driven refresh hooks** where cheap (e.g. software-install events)
  supplement the scheduled sweep; otherwise a periodic sweep per policy.
- Dormant between sweeps — no resident collector (honors the idle budget).
- Per-OS sources: WMI/registry/`Get-*` (Windows), `/proc`, `dmidecode`, package
  DBs (Linux), `system_profiler`/IORegistry (macOS).

## Server side

- Ingests inventory results; upserts summary fields on `Device`; writes detailed
  and volatile data to inventory tables keyed by `device_id`.
- Computes and stores inventory diffs (history).
- Evaluates dynamic group/tag rules on new data.

## Data model additions

- `inventory_snapshot` (device_id, org_id, taken_at, raw jsonb summary)
- `installed_software` (device_id, org_id, name, version, publisher, install_date)
- `inventory_change` (device_id, org_id, ts, change_type, subject, from, to)
- Hardware detail either on `Device` (summary) or `hardware_component`
  (device_id, org_id, type, attributes jsonb) for itemized parts.

## Integration points

- Feeds **patch**, **deployment**, **backup**, **provisioning** targeting.
- Exports to **Power BI** and CSV.
- Disk-encryption / firewall facts feed **security-log / evidence** reporting.

## Open questions (resolve in spec)

- Sweep cadence defaults per device type; how aggressively to use event hooks vs
  periodic sweeps given the idle budget.
- Retention/rollup policy for inventory history.
- How much hardware detail to normalize into columns vs keep as jsonb.
