# Module — Backup

> Scoping brief. Phase 6. The heaviest module — a real backup **engine**, built
> in-house. Build after the platform and agent are proven. v1 is deliberately
> file-level, but the **repository format and job model are pinned now** so later
> phases (bare-metal, app-aware) slot in without a rewrite. This phasing also
> protects the agent's **idle-footprint budget** (image-level disk access is the
> heaviest thing you can put on an endpoint). Expand into a full spec via the
> brainstorming skill before implementation.

## Purpose

A built-in backup engine (not mere orchestration of third-party tools) that
captures endpoint and server data into a **deduplicated, encrypted,
retention-managed repository** and can **restore** it. Datto/Veeam-category
ambition, delivered in phases.

## Scope — v1 (file-level, real engine)

- **File/folder backup** across Windows, Linux, macOS, using OS snapshots for
  consistency:
  - Windows: **VSS** volume snapshots.
  - Linux: **LVM** snapshots.
  - macOS: **APFS** snapshots.
- **Repository engine (original code):**
  - **Content-addressed, deduplicated** storage (chunk + hash) so repeated data
    across backups and machines isn't re-stored.
  - **Encrypted at rest** (per-org keys via the vault; envelope encryption).
  - **Retention** with **GFS** (grandfather-father-son) policies.
  - Stored in **object storage** (per-tenant prefix), optionally staged via a
    **site server** for LAN-local backup traffic.
- **Scheduling & windows:** backup windows via policy → agent scheduler; dormant
  outside windows.
- **File-level restore:** browse a backup point and restore selected files/folders
  to the original or an alternate location.
- **Backup status & verification:** per-device success/failure, last-good point,
  repository health; **restore-tested** status feeds BCP/DR reporting.

## Later phases (repository format designed to accommodate now)

- **Image-level / bare-metal recovery:** full-volume/image capture and bootable
  recovery media. *Explicitly deferred from v1* because raw volume access + filter
  drivers are the single heaviest agent burden and pull against the idle budget;
  the agent's disk layer would be redesigned around it. If this becomes v1-
  critical, revisit the agent disk-access design first.
- **Application-aware backup:** consistent capture of SQL Server, Exchange, etc.
  (VSS writers / app quiescing).
- **Replication / offsite copy** of the repository; immutable/air-gapped copies.
- **Instant recovery / virtual boot** of a backup.

## Agent capability

- `backup.run` and `backup.restore` — **dormant** until a window/job; allocate the
  snapshot + chunking machinery on demand and **release after** (idle-budget
  critical).
- v1 needs snapshot APIs (VSS/LVM/APFS) and file reads; it does **not** need raw
  volume/filter-driver access (that arrives with image-level in a later phase).

## Server side / site server

- Control plane: backup policies, schedules, job orchestration, repository
  metadata/catalog, restore coordination, status/verification reporting, key
  management via vault.
- Site server: optional LAN-local repository staging and dedup cache to keep
  backup traffic off the WAN.

## Data model additions

- `backup_repository` (org_id, name, storage_ref, encryption_key_ref,
  dedup_params)
- `backup_policy` (org_id, scope, sources jsonb, schedule, retention_gfs jsonb)
- `backup_point` (org_id, device_id, repository_id, taken_at, size, dedup_stats,
  state, verified_at)
- `restore_job` (job_id, org_id, device_id, backup_point_id, selection jsonb,
  target, state)
- Repository **chunk/manifest** structures are engine-internal (content-addressed
  store), referenced by `backup_point`.

## Integration points

- **Vault** for repository encryption keys.
- **Object storage** for the repository; **site servers** for LAN staging.
- **Patch** (later) triggers a pre-patch backup.
- **BCP** consumes backup + **restore-test** status as DR evidence.
- **SOC 2 evidence** (backup coverage and tested-restore reporting).

## Open questions (resolve in spec)

- Chunking/dedup algorithm and repository format specifics (this is the crux —
  design it to admit image-level blocks and app-aware streams later).
- Whether to build on a proven engine's *format ideas* while keeping the code
  original and license-clean, or design the format ground-up.
- Restore-verification approach (checksum vs test-mount) for "proven restore"
  status.
- Confirm the v1 file-level boundary still holds when this phase begins (the one
  decision flagged as worth re-confirming before building).
