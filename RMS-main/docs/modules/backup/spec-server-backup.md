# Spec: Server backup (files, databases, AD; local and off-site)

> Companion to `docs/modules/backup/spec.md` (control plane, pinned format) and
> `docs/modules/backup/agent-engine.md` (what the endpoint engine implements).
> Those two remain authoritative for everything this document does not change.
> This one defines what "sellable server backup" means, per workload, and the
> buildable path from "`backup.run` has never completed" (`HANDOFF.md` §5) to it.
> Date: 2026-09-23. Sale from January 2027; field testing until December 2026.

---

## 0. Decisions at a glance

| Workload | Default mechanism | Consistency claim |
| --- | --- | --- |
| Windows files (shares, user folders) | VSS snapshot, `VSS_BT_COPY`, writer status checked after `PrepareForBackup`, after `DoSnapshotSet` and after `BackupComplete`; read with backup semantics; NTFS security descriptors captured | `snapshot` (writer-consistent) or the job fails |
| SQL Server | Native `BACKUP DATABASE … WITH COPY_ONLY, CHECKSUM` per database to a local staging directory, driven in-process through `odbc32.dll`; the `.bak` is chunked and staging is deleted | `application` |
| AD domain controller | VSS in component mode, `VSS_BT_FULL`, NTDS + DFSR (SYSVOL) + Registry writers selected; NTDS writer records the backup | `application`; restore is non-authoritative, to staging plus a runbook |
| Linux files | LVM snapshot, else btrfs read-only snapshot, else ZFS snapshot, else **live read with change detection** (opt-out per policy) | `snapshot`, or `live` (crash-consistent: no), shown everywhere |
| PostgreSQL | `pg_dump -Fc -Z0` per database streamed into the chunker, plus `pg_dumpall --globals-only`; peer auth as `postgres` by default | `application` (per database) |
| MySQL / MariaDB | `mariadb-dump`/`mysqldump --single-transaction` per database, streamed; unix-socket auth by default; non-InnoDB tables flagged | `application`, or `partial` when non-transactional tables exist |

**Off-site copy:** a server-side replication worker copies already-encrypted
objects from the local MinIO repository to the off-site S3 target, using an
off-site credential decrypted from the vault inside the worker. Endpoints never
see the off-site target, its credential or its existence.

**Loss of the ITMS host:** every repository key is additionally wrapped to an
operator-held X25519 recovery key and written to the off-site copy with an
encrypted point catalog. `itms-recover` (a standalone Go CLI) restores from the
off-site bucket with only the bucket credential and the recovery private key.

---

## 1. Scope

### 1.1 In scope

Windows Server 2016+ and Linux (Debian/Ubuntu, RHEL family) servers running the
agent, with the six workloads in §0; a local copy on the ITMS host's MinIO and a
second copy on one S3-compatible off-site target per repository (NAS or second
server running MinIO, Wasabi, Backblaze B2, AWS S3); file-level, database-level
and AD-staging restores, to the source server or another server; scheduled test
restores with recorded evidence; health alerts and tickets for failed, missed,
unreplicated and untested backups.

### 1.2 Out of scope for v1 (stated so nobody sells it)

Bare-metal and image-level recovery; hypervisor-level (Hyper-V, VMware)
backup; failover clusters and Cluster Shared Volumes; SQL Server Always On
secondaries (skipped with a warning), log backups and point-in-time restore;
Exchange, SharePoint, Oracle, MongoDB; PostgreSQL PITR (`pg_basebackup` + WAL);
physical MySQL backup (`mariabackup`, XtraBackup); authoritative AD restore and
automated in-DSRM restore; NTFS alternate data streams, EFS-encrypted files,
Linux hard-link identity and sparseness (each is counted and reported, not
silently dropped); macOS servers; application pre/post scripts.

### 1.3 What "done" means

A workload is done when a CI job on the real OS (§12) backs it up with the
production agent binary, against a real MinIO, under a job signed by a real
key, replicates the point off-site, restores it through the production restore
path, and compares the restored bytes or rows with the source. Nothing in §13
is closed by unit tests alone; unit tests are necessary, not sufficient.

---

## 2. Defects in the existing slice that this spec fixes

Found reading the code for this design. Each is a precondition for Phase 1.

| # | Defect | Where | Fix (section) |
| --- | --- | --- | --- |
| D1 | The window sweep runs every 900 s (`worker.py` beat `backup-window-sweep`), but `window_open` matches the cron **to the minute**, so `0 2 * * *` fires only if a sweep happens to land on 02:00. A `* 2 * * *` window instead dispatches four runs a night. There is no "already ran since the fire time" and no "already running" check, and `window_cron = NULL` means *always open*, i.e. a run every 15 minutes (spec.md §4 says NULL means on-demand only). | `dispatch.py::dispatch_backup`, `worker.py::backup_window_sweep_all_orgs` | Due-based scheduling, §3.3 |
| D2 | `repo_key` is in `jobs.params` in plaintext (base64) for the retention period, and `GET /api/v1/jobs/{id}` returns it to any `jobs.read` holder: neither `backup.run` nor `backup.restore` declares a `params_redactor` or `terminal_params_scrubber` (only `app.deploy` and `agent.push_install` do). Invariant 3. | `handler.py::register_job_types` | Redactor + scrubber now; the key leaves params entirely in Phase 2, §5.4 |
| D3 | Jobs expire after `job_default_ttl_s = 3600`, and the dedup exchange silently refuses an expired job (`dedup.py`, `job_expired`). Any backup longer than an hour, meaning every first full of a real file server, fails mid-run. | `dedup.py`, `dispatch.py` | Per-workload `max_runtime`, §3.3 |
| D4 | The result handler runs only for a `succeeded` job. A refused, expired or failed `backup.run` leaves its `backup_runs` row on `running` forever, invisible to reporting and alerting. Same for `restore_jobs`. | `app/jobs/engine.py::complete` | Reconcile sweep, §3.3 |
| D5 | The VSS provider calls `SetBackupState(false, false, VSS_BT_FULL)` in non-component mode. Every writer with data on the snapshotted volumes then treats it as a full backup: SQL Server records a non-copy-only full backup in `msdb` and resets the differential base of every database on the volume, breaking the customer's own backup chain. | `snapshot_windows.go::takeSnapshotSet` | `VSS_BT_COPY` for file workloads, §4.1 |
| D6 | The whole manifest is held in memory (`writer.entries`) and a manifest over 256 MiB is refused on restore (`maxManifestBytes`), about one million files. File servers exceed both. | `run.go`, `manifest.go` | Paged manifest v2, §5.2 |
| D7 | `verify_point` re-reads and decrypts **every** chunk a point references, and every new daily point is unverified, so a 2 TB server costs 2 TB of reads per day. | `gc.py::verify_point`, `worker.py` | Chunk-level verification, §7.5 |
| D8 | The manifest carries no owner, ACL or xattr. Restoring a file share makes every file inherit the restore directory's ACL. | `wire.go::manifestEntry` | Security metadata in v2 entries, §4.1, §4.4 |
| D9 | Restore opens the destination with `O_TRUNC`; a failure part-way through deletes the partial file, so an in-place restore that fails destroys the original. | `restore.go::(*restorer).file` | Write-then-rename, §7.1 |
| D10 | Windows reads use `os.Open`, so files whose DACL excludes SYSTEM are counted unreadable. | `run.go` | `SeBackupPrivilege` + `FILE_FLAG_BACKUP_SEMANTICS`, §4.1 |
| D11 | Exclude patterns use `path.Match`; `**` silently matches nothing. | `wire.go`, `run.go` | In-tree `**` matcher, §4.1 |
| D12 | Fleet coverage counts a device as covered if it ever had a complete point. | `reporting.py::fleet_backup_report` | Per-workload RPO coverage, §8.3 |

---

## 3. Data model and scheduling

All new tables carry `org_id`, RLS `ENABLE + FORCE` with the standard
`tenant_isolation` policy, and an explicit `org_id` predicate on every query
(invariant 5). One forward migration after the current head (0082 today) adds
them; a second seeds the health checks of §8.1.

### 3.1 Workloads

A policy stays the unit of assignment (device group, then org default), but it
now protects a list of **workloads**, each with its own schedule, retention and
alerting. One `backup.run` protects one workload on one device and produces one
backup point.

`backup_workloads`: `id`, `org_id`, `policy_id` FK, `name`, `kind`
(`files | mssql | ad_system_state | postgres | mysql`), `config` JSONB
(validated by a per-kind Pydantic model, §4), `credential_id` FK `credentials`
nullable, `schedule_cron` nullable (NULL = on demand only), `start_deadline_min`
(default 360), `max_runtime_min` (default 1440, cap 4320), `retention_gfs`
JSONB, `test_restore_every_days` nullable, `restore_test_device_id` nullable,
`enabled`. UNIQUE `(policy_id, name)`.

Existing rows migrate losslessly: each `backup_policies` row with a non-empty
`sources` becomes one `files` workload carrying its `sources`, `window_cron` and
`retention_gfs`. The policy columns stay for one release, read-only, then go in a
later migration.

`backup_runs` and `backup_points` gain `workload_id` (nullable for legacy rows),
`workload_kind`, and `consistency` JSONB (§5.3). `backup_points` also gains
`consistency_level` (indexed, for reports), `restore_test_level`,
`restore_test_scope` and `rehydrated_until`. `backup_runs.state` gains `missed`.

`backup_discovery`: `org_id`, `device_id`, `collected_at`, `facts` JSONB, the
result of the `backup.discover` job (§5.1): volumes and their snapshot
capability, VSS writers present, SQL instances and databases, DC role, Postgres
clusters, MySQL/MariaDB servers. Feeds the policy editor's pickers only.

### 3.2 Credentials

Database and off-site credentials are ordinary `credentials` rows (vault-sealed,
`docs/kernel/vault.md`) with new `type` values `db_mssql`, `db_postgres`,
`db_mysql`, `s3_offsite`. A workload may reference only a credential of the
matching type; if the credential has a `device_group_id`, dispatch refuses a
device outside that group. `last_used_at` is stamped and `credential.accessed`
audited on every release (§5.4).

### 3.3 Scheduling (D1, D3, D4)

`window_open` is replaced for backup by `last_fire(cron, now)`: the latest minute
at or before `now` that matches the cron, looking back at most eight days. The
sweep (still every 15 minutes) dispatches for a `(device, workload)` when:

1. `last_fire` exists and `now - last_fire <= start_deadline_min`;
2. no `backup_runs` row for the pair has `started_at >= last_fire`;
3. no row for the pair is `running`.

When `now - last_fire > start_deadline_min` and no run exists since
`last_fire`, the sweep writes one `backup_runs` row with `state = missed` (agent
offline, maintenance, sweep outage). A missed run is what the age alert in §8.1
reads; it is never retried automatically.

The job is created with `ttl_s = start_deadline + max_runtime`, and the params
carry `not_after_start` (the deadline). A current agent refuses to *start* after
it; an old agent ignores the field and starts late, which is harmless. The dedup
and read exchanges keep refusing expired jobs; the TTL now covers the run.

A new `backup_reconcile_sweep` (every 5 minutes) moves any `running`
`backup_runs`/`restore_jobs` row whose job is terminal and not `succeeded` to
`failed`, copying the job's refusal reason or terminal status into `error`.

---

## 4. Workloads

Every workload's `config` model is `extra="forbid"`. Every refusal reason and
failure text keeps the existing rule: no customer path, database name or host
name in audit metadata or `backup_runs.error`; roots and databases are named by
index or id (`agent-engine.md` §9).

### 4.1 Windows files

**Config:** `paths` (absolute, or the token `@shares` which the agent expands to
the local paths of non-administrative SMB shares via `NetShareEnum`), `exclude`
(globs, `**` supported, case-insensitive), `restore_acls` (default true).
Templates in the editor: *File server* (`@shares`, plus `D:\Users` style user
folder roots) and *All data volumes*.

**Snapshot sequence** (MSDN requester order; replaces the order in
`agent-engine.md` §4, which put `SetBackupState` after `AddToSnapshotSet`):

`CoInitializeEx(MTA)` on a locked OS thread → `CreateVssBackupComponents` →
`InitializeForBackup` → `SetContext(VSS_CTX_BACKUP)` → `GatherWriterMetadata`
(wait) → `SetBackupState(selectComponents=false, bootable=false, VSS_BT_COPY,
partial=false)` → `StartSnapshotSet` → `AddToSnapshotSet` per volume →
`PrepareForBackup` (wait) → **writer check 1** → `DoSnapshotSet` (wait) →
**writer check 2** → read → `BackupComplete` (wait) → **writer check 3** →
`DeleteSnapshots(this set only)` → release.

**Writer check** = `GatherWriterStatus` (wait), `GetWriterStatusCount`,
`GetWriterStatus` for each (vtable slots 16–19, already declared and never
called), `SysFreeString` on each writer name, `FreeWriterStatus`. A writer in any
`VSS_WS_FAILED_AT_*` state, or with a non-`S_OK` failure HRESULT, at check 1 or 2
fails the job: `AbortBackup`, delete the set, return a `failed` result naming
the writer, its state and the HRESULT (writer names are Microsoft component
names, not customer data). There is no ignore list in v1. A failure at check 3
(after the data is read) produces the point with a `writer_post_backup_failed`
warning, because the data was captured while every writer was stable. All three
checks' results go into `consistency.writers`.

**Why `VSS_BT_COPY` (D5).** A copy backup is, by VSS definition, one that does
not affect the writers' own backup history: SQL Server records it
`is_copy_only = 1` and leaves the differential base alone; Exchange does not
truncate logs. A file-level product must not change the customer's database
backup chain as a side effect. Only the AD workload (§4.3) uses `VSS_BT_FULL`,
because there recording the backup is the point.

**`CoInitializeSecurity`** moves to `main.go`, called once before any
capability touches COM (`PKT_PRIVACY`, `IMP_LEVEL_IDENTIFY`): it is process-wide,
so it belongs to the process owner. W8 (§12) proves `patch.scan` survives it.

**Reading.** For the job's duration the agent enables `SeBackupPrivilege` and
`SeSecurityPrivilege` on its token and opens files under the shadow path with
`CreateFile(…, FILE_FLAG_BACKUP_SEMANTICS)` (D10); both privileges are disabled
again on every exit path. Per entry, the owner, group and DACL are read with
`GetSecurityInfo` and stored as SDDL in the v2 entry's `sd` field (SACL only when
`SeSecurityPrivilege` is held; its absence is recorded, not fatal).

**Default exclusions:** `pagefile.sys`, `hiberfil.sys`, `swapfile.sys`,
`System Volume Information`, `$Recycle.Bin`, the agent's own data directory, the
SQL staging directory of §4.2, and every entry of the registry key
`HKLM\SYSTEM\CurrentControlSet\Control\BackupRestore\FilesNotToBackup` (the
Windows convention every backup product honours). On a server that also has an
`mssql` workload, database files (`*.mdf`, `*.ndf`, `*.ldf` under the instance
data paths from discovery) are excluded from the files workload by default:
they are covered better by §4.2.

ADS, EFS files and unsupported reparse points are counted and warned, not
silently dropped. **Rejected:** `vssadmin`/`diskshadow`/PowerShell (EDR signal,
`agent-engine.md` §2); `VSS_BT_FULL` for files (D5); an ignore list for failed
writers (reconsider with field data).

### 4.2 SQL Server

**Config:** `instances` (`"*"` or names from discovery), `databases` (`"*"` or
names), `exclude_databases`, `staging_path` (default: the agent data directory
on the local fixed volume with the most free space that is not the system
volume, else the system volume), `compression` (default false, §below),
`auth` (`integrated` default, or `credential`).

**Mechanism.** For each selected online database (tempdb always excluded;
master, model, msdb included), one at a time:

1. Pre-flight: free space on the staging volume ≥ 1.2 × allocated data size
   (`sys.master_files`), or the database fails with a clear reason and the run
   continues with the next.
2. `BACKUP DATABASE [<quoted>] TO DISK = N'<staging>\db-<database_id>.bak'
   WITH COPY_ONLY, CHECKSUM, INIT, FORMAT[, COMPRESSION]`. The statement is
   executed with `SQLExecDirectW` and then drained with `SQLMoreResults` until
   `SQL_NO_DATA`; a `BACKUP` whose informational result sets are not drained can
   be cancelled by the driver before it finishes. Error messages are read with
   `SQLGetDiagRecW`. The database name is quoted by doubling `]`; file names use
   the numeric `database_id` only.
3. The `.bak` is chunked into a `named` stream (§5.2) and deleted, before the
   next database starts. Staging therefore needs the size of the largest
   database, not the sum.

The staging directory's DACL grants only SYSTEM, Administrators and the
instance's service SID (`NT SERVICE\MSSQL$<name>`), which writes the file.

**Why native `BACKUP`, not the VSS SQL writer, as default.** (a) A standard
`.bak` restores on any same-or-later SQL Server, so single-database restore and
restore to another server are one `RESTORE`; VSS output must be attached or
replayed through the writer's restore protocol. (b) `CHECKSUM` validates every
page and `RESTORE VERIFYONLY` is the §7.4 test restore. (c) Component-mode VSS
needs two more unverified COM interfaces (`IVssExamineWriterMetadata`,
`IVssWMComponent`); ODBC is a flat C API with no vtable to mis-order. Accepted
costs: a login with backup rights, and staging space. VSS stays the fallback if
the permission requirement proves unacceptable in the field.

**No compression by default:** uncompressed consecutive fulls are mostly
identical extents, which CDC deduplicates; compressed ones are not.
`compression: true` exists for staging-constrained servers.

**Driver and authentication.** `odbc32.dll` is loaded with `LoadLibraryEx` at job
start and freed at job end; driver preference is ODBC Driver 18, 17, then the
in-box `SQL Server` driver, over shared memory to the local instance. No Go SQL
driver enters the module graph. `integrated` (default) connects as
`NT AUTHORITY\SYSTEM`, which has no SQL rights by default; the setup page prints
the one-time grant (Q2). `credential` uses a `db_mssql` vault credential from the
secret exchange (§5.4), passed to `SQLDriverConnectW` in memory and zeroed.

**Transaction logs.** `COPY_ONLY` does not truncate the log. v1 never takes log
backups and never changes a recovery model. Discovery reads each database's
recovery model and the newest log backup in `msdb.dbo.backupset`; a `FULL` or
`BULK_LOGGED` database with no log backup in 24 hours raises the
`backup.mssql_log_unmanaged` warning (§8.1), because its log will grow until
someone backs it up. Q3 decides whether a later phase lets ITMS own the log
chain.

TDE databases (warned: restorable only where the certificate exists), Always
On secondaries and non-`ONLINE` databases are flagged or skipped and counted.
**Rejected:** `sqlcmd.exe` (a Windows process spawn); `BACKUP TO URL`
(object-store credentials inside SQL Server); VDI streaming (another COM
surface; revisit after v1 to remove staging).

### 4.3 Active Directory domain controller

**Config:** none beyond the schedule; the agent refuses the workload on a machine
that is not a DC (no `NTDS\Parameters` key).

**Mechanism.** VSS in component mode: `SetBackupState(selectComponents=true,
bootable=false, VSS_BT_FULL)`; `AddComponent` for the NTDS writer's database
component, the DFS Replication writer's SYSVOL component and the Registry
writer; writer checks as §4.1 with failure of any *selected* writer fatal at
every check including 3 (for AD, `BackupComplete` is where the NTDS writer
records the backup). `SetBackupSucceeded` per component before `BackupComplete`.
Files are read from the shadow copy by the paths the registry names:
`DSA Database file`, `Database log files path`, the Netlogon `SysVol` path, and
`System32\config\{SYSTEM,SOFTWARE,SECURITY,SAM,DEFAULT}`. They go into three
named filetree streams: `ntds`, `sysvol`, `registry`.

Stream metadata records the domain DNS name, DC name, DSA invocation ID and the
forest `tombstoneLifetime`, read in process through `wldap32.dll` (bind as the
machine account with Negotiate); if the read fails, 60 days is assumed and
recorded as assumed.

**What restore means in v1.** Non-authoritative. ITMS restores the three streams
to a staging directory on the DC (or any Windows device) and generates a runbook
with this DC's actual paths: boot into DSRM, copy NTDS files into place, set
`HKLM\SYSTEM\CurrentControlSet\Services\NTDS\Parameters\Database restored from
backup = 1` (so the DC takes a new invocation ID and USN rollback cannot occur),
restore SYSVOL as non-authoritative for DFSR, reboot normally, confirm
replication. The server refuses to restore an AD point older than
`tombstoneLifetime - 7 days`. Out of scope: authoritative restore, bare-metal
restore, automated in-DSRM restore, object-level recovery (the AD Recycle Bin
covers most of that need; mounting a restored `ntds.dit` with `dsamain` is a
candidate for a later phase).

**Rejected:** bootable system state (the System writer's whole OS file set,
10–15 GB, for a restore v1 cannot perform); `wbadmin` (a process spawn).

### 4.4 Linux files

**Config:** `paths`, `exclude` (`**` supported), `one_filesystem` (default
false), `allow_live` (default per Q1), `restore_ownership` (default true).
Template *Linux server*: `/` with default exclusions `/proc`, `/sys`, `/dev`,
`/run`, `/tmp`, `/var/tmp`, `/var/cache`, swap files, and every mount of a
pseudo or network filesystem (`proc`, `sysfs`, `tmpfs`, `devtmpfs`, `cgroup*`,
`overlay`, `nfs*`, `cifs`, `fuse.*`), judged by fstype from `findmnt --json`.
If the device has a `postgres` or `mysql` workload, the server's data directory
is excluded from the files workload by default; if it has none and a root covers
a data directory discovery found, the policy editor warns that a live copy of a
database data directory is not a backup.

**Snapshot selection, per filesystem a root lives on, in this order:**

| Filesystem | Mechanism | Commands (only during a `backup.run`) |
| --- | --- | --- |
| LVM logical volume | existing `openLVM` | as `agent-engine.md` §2 |
| btrfs | `btrfs subvolume snapshot -r <subvol> <subvol>/.itms-snap-<tag>` for the subvolume containing the root and each nested subvolume under it (snapshots are not recursive); read via the snapshot path; `btrfs subvolume delete` of this agent's tagged snapshots only, including in the orphan sweep | `btrfs` |
| ZFS dataset | `zfs snapshot <ds>@itms-<tag>` for the dataset and each child dataset under the root; read via `<mountpoint>/.zfs/snapshot/itms-<tag>/`; `zfs destroy` of this agent's `@itms-` snapshots only | `zfs` |
| anything else (ext4/XFS on a partition, cloud images) | live read with change detection, if `allow_live`; otherwise the existing refusal | none |

The params' `snapshot` value becomes `auto` for Linux under format 2 (§5.1);
an old agent refuses `auto` through `requireKind`, which is the correct failure.

**Live read with change detection.** Per regular file: `lstat`, open, read and
chunk, `fstat`; if size, mtime, ctime or inode changed, re-read up to two more
times. A file still changing after three reads is stored as last read and
marked `changed_during_read`. The point records `consistency.level = live`,
`crash_consistent = false`, and the count. Nothing freezes the filesystem.
The console shows *Not snapshot-consistent* on every such point, the coverage
report shows the level, and §8.1's `backup.consistency_degraded` check alerts at
`info` severity so the state is visible without paging anyone.

**Security metadata (D8):** uid, gid, user and group names, mode, and xattrs in
the `security.*`, `system.posix_acl_*` and `trusted.*` namespaces (capabilities,
SELinux labels, POSIX ACLs). Restore applies them by name first, then by number.
Device nodes, FIFOs and sockets are skipped and counted; hard links are restored
as independent files; sparse files are restored dense. All three are counted.

**Rejected:** `fsfreeze` (blocks every writer including journald; a crash
while frozen hangs the server); refusing every non-LVM root (most cloud-image
servers are plain ext4, and a flagged live backup is not silent).

### 4.5 PostgreSQL

**Config:** `host` (default: the local socket directory), `port` (5432),
`databases` (`"*"`), `exclude_databases`, `auth` (`peer` default, or
`credential`).

**Mechanism.** List databases with `psql -AtX -c "SELECT datname FROM pg_database
WHERE datallowconn AND NOT datistemplate"`. For each:
`pg_dump -Fc -Z0 --no-password -d <name>`, stdout read directly by the chunker
into a `named` stream `postgres/<port>/<datoid>`: no staging, no temporary file.
Then `pg_dumpall --globals-only` into `postgres/<port>/globals`. `-Z0` for the
same dedup reason as §4.2; custom format keeps per-object selective restore.
Each dump is one `REPEATABLE READ` snapshot, so each database is consistent;
databases are not consistent with each other, which is stated in the point.

**Binaries.** Resolved only from fixed directories, never `$PATH`:
`/usr/lib/postgresql/<major>/bin` (Debian) and `/usr/pgsql-<major>/bin` (PGDG
RPM), with `<major>` equal to the server's `server_version_num` major, because
`pg_dump` must not be older than the server. Missing binaries fail the run with
the version they needed.

**Authentication.** `peer` (default): the child runs as the `postgres` OS user
(`SysProcAttr.Credential`); no secret exists. `credential`: a `db_postgres`
vault credential from the secret exchange (§5.4), set only in the child's
environment as `PGPASSWORD`, never in argv (world-readable in `ps`) and never in
the agent's own environment; `/proc/<pid>/environ` is readable only by the uid
and root.

**Rejected:** `pg_basebackup` as default (whole cluster only, no single-database
restore, needs WAL archiving to be useful; it is the PITR phase's mechanism);
a password file (`libpq` rejects a non-regular file, so it would have to be
written to disk).

### 4.6 MySQL and MariaDB

**Config:** `socket` (default from discovery), `databases`, `exclude_databases`,
`auth` (`socket` default, or `credential`).

**Mechanism.** Probe `/usr/bin/mariadb-dump` then `/usr/bin/mysqldump` (fixed
paths). Per database, without `--databases` so the dump can be loaded into any
name: `--single-transaction --quick --routines --events --triggers --hex-blob
--default-character-set=utf8mb4 --no-tablespaces <db>`, streamed into a named
stream. Users and grants: `mariadb-dump --system=users` on MariaDB ≥ 10.5; on
MySQL 8, `SHOW CREATE USER` and `SHOW GRANTS` output collected through the
`mysql` client. Discovery counts tables whose engine is not InnoDB in each
selected schema; any non-zero count makes the point `consistency.level =
partial` with the count, because `--single-transaction` does not protect them.

**Authentication.** `socket`: root through `unix_socket`/`auth_socket` (the
Debian/Ubuntu MariaDB default), no secret. `credential`: the client reads
`--defaults-extra-file=/dev/fd/3`, where fd 3 is a pipe the agent writes a
`[client]` section into and closes; the secret touches no disk, no argv and no
environment (`MYSQL_PWD` is deprecated). The Linux CI job must prove both client
binaries accept a pipe here (falsifier L6); if one does not, the fallback is a
0600 file in a 0700 directory on `/run` (tmpfs), unlinked once the child has
opened it.

**Rejected:** `--lock-all-tables` by default (blocks writers for the whole dump);
physical backup tools (a later phase, for databases too large to dump nightly).

---

## 5. Wire and format changes

Every change is additive or versioned. The server model is loosened in the same
release that first dispatches the new shape, and the operator upgrades the
control plane before agents. Nothing here makes a field required that an old
agent does not send.

### 5.1 Job params

`BackupRunParams` gains optional members; agents ignore unknown members today.

| Member | Meaning | What an old agent does |
| --- | --- | --- |
| `format: int = 1` | 2 = write manifest v2, emit the §5.3 result members, fetch secrets by exchange | ignores; writes v1 |
| `workload: {id, kind, config}` | the workload of §4 | ignores it; for non-file kinds `sources` is sent as `{}`, so the old agent **refuses** (`sources.paths` absent) |
| `snapshot: "auto"` (enum widened) | Linux selection of §4.4 | refuses via `requireKind` |
| `vss: {backup_type, fail_on_writer}` | §4.1/§4.3 | ignores; uses its compiled `VSS_BT_FULL` (D5 persists on old agents; §14 R7) |
| `not_after_start` | §3.3 | ignores |
| `repo_key: ""` under format 2 | key by exchange (§5.4) | refuses (not 32 bytes) |

The server sends `format: 2` only to agents whose `agents.agent_version` is at
least the release that implements it, and dispatches non-file workloads only
under format 2. `agent_version` is advisory; the table's right-hand column is
why a wrong guess fails closed.

`BackupRestoreParams` gains `format`, `restore_kind`
(`files | database | ad_staging`), `database` (`{engine, stream, target_name,
overwrite}`), `target.conflict` (`rename | skip | overwrite`, inside the existing
untyped `target` object) and `target_device_id` is **not** a param: the job is
simply addressed to the target device (§7.2).

A new light job type, `backup.discover` (params: none; result: the §3.1
`facts`), is registered on both sides. Old agents refuse it `unknown_type`,
which the editor shows as "agent too old for discovery".

### 5.2 Manifest v2

`schema_version: 2`. Old agents refuse any version they do not implement
(`manifest.go::openManifest`), so a v2 point can never be half-restored by an old
agent: the failure is the refusal, with its reason, in the audit log.

```
{ "schema_version": 2,
  "header": { "workload_kind", "consistency", "host_os", "page_index": [...] },
  "streams": [
    { "type": "filetree", "name": "files" | "ntds" | "sysvol" | "registry",
      "entry_count": N, "pages": [chunk_id, ...] },
    { "type": "named", "name": "mssql/<instance>/<database_id>",
      "format": "mssql-bak" | "pg-custom" | "pg-globals" | "mysql-sql" | "mysql-users",
      "meta": { "server_version", "db_name", "recovery_model", ... },
      "size": bytes, "pages": [chunk_id, ...] } ] }
```

**Paging (D6).** Entries are never held in memory. The walk visits children in
sorted order, so entries are produced already ordered; they are serialised as
NDJSON and fed to the same chunker, encryption and dedup path as file data. A
page is an ordinary chunk. Unchanged directories produce identical pages, so
the manifest itself deduplicates day to day. A named stream's pages hold its
ordered chunk id list the same way, so a 1 TB database's 1M chunk ids are not
one 65 MB object. `page_index` records each page's first and last path so the
console's browse fetches only the pages under the directory being viewed.

v2 entries add optional `sd` (Windows SDDL), `uid`, `gid`, `user`, `group`,
`xattrs` (base64 values), `flags` (`changed_during_read`, `sparse`,
`hardlink`). The server's `BackupManifest` becomes a discriminated union over
schema versions and stream types; `gc.py` and verification read pages through
one iterator used by both, so a v2 point's chunks are referenced for GC exactly
as v1's are.

### 5.3 Result

`BackupRunResult` is `extra="forbid"` server-side. The server release that
dispatches format 2 first adds optional `workload_id`, `consistency` and
`warnings`; agents emit them only when the job said `format: 2`, so an old
server never sees them. `consistency` = `{level: application | snapshot | live |
partial, crash_consistent: bool, snapshot: vss | lvm | btrfs | zfs | none,
writers: [{name, state, hr, at}], files_changed_during_read,
non_transactional_tables, databases: [{id, state, bytes}]}`.

### 5.4 Secret exchange (D2, invariant 3)

Two new message kinds on the existing mTLS WebSocket, shaped like the read
exchange: `backup_secret_query {job_id, query_id}` and `backup_secret_grant
{job_id, query_id, repo_key, credential?: {username, secret}}`. The server
resolves everything from the job it signed: a `backup.run`/`backup.restore`
that is live, addressed to the requesting device, with a `running` run row; the
repository's key from the vault; the workload's credential if any. Every refusal
is silence plus a `backup.secret_refused` audit record. Each release is audited
as `credential.accessed` (credential id, job id, never the secret), and a job may
release at most twice (one reconnect). The agent zeroes both after use.

This takes the repository key and every database credential out of
`jobs.params` for format-2 agents. Until the fleet is on format 2, legacy
dispatches keep `repo_key` in params, now covered by a `params_redactor` (every
API read) and a `terminal_params_scrubber` (overwritten at any terminal state),
both shipped in Phase 1 before anything else.

Compatibility: an old agent never sends the query (it is never given an empty
key it would need to fill, because the server only omits the key under
format 2); an old control plane never receives it (a format-2 job comes only
from a new one).

---

## 6. Storage: local and off-site

### 6.1 Local copy

Unchanged: the agent writes chunks and manifests to MinIO on the ITMS host under
`{org_id}/backup/{repository_id}/` through the POST grant of the dedup exchange.
Local retention is the workload's `retention_gfs`, applied by the existing GFS
sweep, now per `(device, workload)` rather than per device, with one new rule: a
point whose off-site copy is not `complete` is never pruned locally.

### 6.2 Off-site target

`backup_offsite_targets`: `id`, `org_id`, `name`, `endpoint`, `region`,
`bucket`, `prefix`, `addressing` (`path | virtual`), `credential_id`
(`s3_offsite`), `lock_mode` (`none | governance | compliance`), `immutable_days`
(default 30, cap 90), `retention_gfs`, `max_mbps`, `state`, `last_ok_at`.
`backup_repositories.offsite_target_id` attaches one target to a repository.
`POST /offsite-targets/{id}/test` has the worker write, read, lock-check and
(where the lock allows) delete a probe object, and reports each step.

The off-site credential needs `PutObject`, `GetObject`, `ListBucket`,
`PutObjectRetention` and `DeleteObjectVersion`, and must not have
`BypassGovernanceRetention` or bucket-configuration rights. The setup page
prints the policy for AWS, Wasabi and B2.

### 6.3 Replication worker

**Server-side replication, not the agent writing twice.** A Celery task,
`backup_replication_sweep`, runs every 5 minutes on a dedicated queue served by
a new compose service `backup-replicator` (same image), so a multi-hour initial
seed never starves the beat sweeps. Per target, under a Redis lock, oldest point
first:

1. Open the repository key from the vault; decrypt the point's manifest; iterate
   its pages to the referenced chunk ids.
2. For each chunk id with no `backup_chunk_copies` row for this target: GET
   from local MinIO, PUT to the target under the off-site name (§6.4) with the
   lock headers and a checksum header (Object Lock buckets require one), record
   the returned version id. Ciphertext only: the worker never decrypts a data
   chunk.
3. PUT the manifest and its pages, then the catalog object (§6.5).
4. Mark `backup_point_copies.state = complete`, stamp `copied_at`, audit
   `backup.replicated` (counts and bytes only).

Rows make it resumable at chunk granularity. `backup_point_copies`: `point_id`,
`target_id`, `state` (`pending | copying | complete | failed | expired`),
`copied_at`, `bytes`, `locked_until`, `last_error`. `backup_chunk_copies`:
`repository_id`, `target_id`, `chunk_id`, `object_name`, `version_id`,
`locked_until`, `verified_at`; UNIQUE `(target_id, repository_id, chunk_id)`.

**Rejected: the agent writes both copies.** It doubles endpoint upload
bandwidth; it requires every server, including those in a DMZ, to reach the
internet; it needs a POST-policy grant from a third-party provider, and not every
S3-compatible provider implements POST-policy uploads; and it would put a credential-derived
grant for the off-site bucket, the ransomware-recovery copy, on the machines most
likely to be compromised.

### 6.4 Immutability and naming

With `lock_mode` set, the target bucket must have Object Lock enabled (the test
endpoint checks). Every object is PUT with retain-until = now +
`immutable_days`. A chunk is shared by many points, so when a newly replicated
point references a chunk whose `locked_until` is earlier than the point's own,
the worker extends it with `PutObjectRetention` to now + 2 × `immutable_days`.
Extending by twice the window means a chunk is extended at most once per window:
about 33,000 calls per day per TB at the default 1 MiB chunk size, which is the
cost of the property and is stated on the setup page.

`compliance` means nobody, including the account root and a thief holding the
ITMS host's off-site credential, can delete or shorten a lock before it expires.
That is the ransomware property. The local MinIO copy is not immutable in v1:
an attacker with root on the ITMS host can destroy it, and the off-site copy is
the answer.

Off-site chunk objects are named `chunks/{n[:2]}/{n}` with `n =
HMAC-SHA256(K_name, chunk_id)` and `K_name = HKDF(repo_key, "itms offsite
chunk names v1")`. The plain chunk id is BLAKE3 of the plaintext; on a
third-party provider it would let the provider confirm whether a specific known
file is in the backup. The keyed name removes that for the cost of one HMAC.

### 6.5 Key custody and recovery without the ITMS host

The repository key is sealed in the vault under the org DEK, whose wrapping key
lives in OpenBao (or the local root key). If the ITMS host and its own platform
backup (`docs/07-backup-restore.md`) are both lost, the off-site copy would be
unreadable. So:

- **Recovery key.** Off-site replication cannot be enabled until the org has a
  registered recovery key. The operator generates an X25519 keypair offline with
  `itms-recover keygen`, pastes the public key, and proves possession: the server
  encrypts a random challenge to it, `itms-recover prove` decrypts it, the
  operator pastes the answer. ITMS never sees the private key.
  `backup_recovery_keys`: `id`, `org_id`, `public_key`, `fingerprint`,
  `proven_at`, `created_by`.
- **Key object.** For each repository and each proven recovery key the worker
  writes `keys/repository-key.<fingerprint>.json` to the target:
  `{v: 1, alg: "X25519-HKDF-SHA256-AES-256-GCM", recipient, epk, nonce, ct}`
  with AAD = repository id. Python `cryptography` on the server; Go standard
  library (`crypto/ecdh`, `crypto/hkdf`, `crypto/aes`) in the tool, no new
  dependency.
- **Catalog.** `catalog/repository.json` (format version, repository id, dedup
  params, key fingerprints; no names) and `catalog/points/<point_id>` sealed under
  the repository key (device id, hostname, workload kind and name, taken_at,
  manifest object name, consistency, size).
- **`itms-recover`.** A standalone Go binary built from `agent/cmd/itms-recover`,
  reusing the agent's chunk, manifest and restore packages, shipped with every
  release for Windows and Linux. Given the target endpoint, bucket, a read
  credential and the recovery private key, it lists repositories and points,
  restores a point or a selection of files to a local directory, and extracts a
  database dump to a file (the operator then runs `RESTORE`, `pg_restore` or
  `mysql` themselves). It holds no agent identity and cannot talk to a gateway.
  `deploy/GO-LIVE.md` gains a "restore with the ITMS host gone" page, and the
  CI proof is L9 in §12.

**Rejected:** storing the repository key off-site in the clear (the provider
reads every backup); relying on the platform backup alone (it is on the same
host unless the operator moves it, and it restores the whole platform, not one
server's files, at 3 a.m.); generating the recovery key in the browser (it would
exist, however briefly, inside the application that it is meant to survive).

### 6.6 Per-copy retention and GC

Off-site retention is the target's `retention_gfs`, independent of local
retention and usually longer. `backup_offsite_gc` (daily) runs `select_gfs` over
each `(device, workload)`'s complete copies, marks losers `expired`, then deletes
object versions of chunks no live copy references and whose `locked_until` has
passed, by version id. Local GC is unchanged except for the §6.1 hold and the
rehydration rule in §7.3.

---

## 7. Restore

### 7.1 File-level

Browse is page-lazy (§5.2). The operator selects paths and a target:

- **Alternate path** (default): `<system drive>\ITMS-Restore\<point time>\` or
  `/var/lib/itms/restore/<point time>/` on the chosen device.
- **Original location**: `target.path` is the original root with `conflict`
  `rename` (default: the restored file gets `.restored-<time>` if the name
  exists), `skip`, or `overwrite`. `overwrite` requires `backup.restore.overwrite`.

Every file is written to a temporary name in the destination directory and
renamed over the target only after its length and every chunk hash verify
(`MoveFileExW(MOVEFILE_REPLACE_EXISTING)` / `rename(2)`), so a failed restore
leaves the original untouched (D9). Security descriptors are applied with
`SetNamedSecurityInfo` under `SeRestorePrivilege` and `SeTakeOwnershipPrivilege`
on Windows; ownership and xattrs by name then number on Linux. A SID or user
that does not exist on the target is applied raw and reported. The existing
refusals of `agent-engine.md` §8 (symlink escape, `..`, UNC, reserved names)
stand unchanged.

### 7.2 Database-level and to another server

`restore_kind = database`. The target defaults to a **new** database named
`<name>_restored_<yyyymmddHHMM>` on the chosen device. Overwriting the source
database requires `backup.restore.overwrite` and a request field `confirm_name`
equal to the database name (checked server-side).

- **SQL Server:** chunks → staging `.bak` (hash-verified) → `RESTORE DATABASE
  [new] FROM DISK = … WITH CHECKSUM, RECOVERY, MOVE` each logical file to
  `<instance default data/log path>\<new>_<n>.{mdf,ldf}`; overwrite uses
  `REPLACE`. Staging deleted.
- **PostgreSQL:** `CREATE DATABASE` through `psql`, then chunks streamed into
  `pg_restore --exit-on-error -d <new>` stdin. Overwrite: `--clean --if-exists`
  into the existing database. Globals are restored as a file to the alternate
  path, never applied automatically (role changes are an operator's call).
- **MySQL/MariaDB:** `CREATE DATABASE`, then chunks streamed into `mysql <new>`.
  Users and grants as a file, as above.

**Restore to another server** is the same request with a different target
device in the same org. `restore_jobs` gains `source_device_id`; its
`device_id` is the target, which is exactly what the read exchange already
authorizes against (`read.py` resolves the running `RestoreJob` for the
requesting device), so no exchange change is needed. The target must have a
matching OS family for file ACLs, and for databases an engine of the same or a
later major version (checked against its discovery facts). Both devices are named
in the `backup.restore_dispatched` audit record.

### 7.3 Restoring a point that exists only off-site

`POST /points/{id}/rehydrate` has the worker copy the point's missing chunks,
pages and manifest back from the target into the local repository and inserts
their `backup_chunks` rows; the point becomes `rehydrated` with
`rehydrated_until = now + 7 days`, which local GC treats as live. Then the
ordinary restore runs. Endpoints never read from the off-site target, need no
internet route, and no read grant is ever signed against the off-site bucket.

### 7.4 Scheduled test restores

`backup_test_restore_sweep` (daily) dispatches, per workload whose
`test_restore_every_days` has elapsed, a `mode = verify` restore of the newest
complete point on the source device (or on the policy's
`restore_test_device_id`), system-attributed:

| Workload | Level | What runs |
| --- | --- | --- |
| files | `read`, scope `sampled` | 200 random files, capped at 5 GiB, restored into the agent's scratch directory, every chunk hash-checked, ACLs applied and read back, then deleted |
| mssql | `archive` | staging `.bak` rebuilt from chunks, `RESTORE VERIFYONLY … WITH CHECKSUM`, deleted |
| mssql, postgres, mysql with a restore-test device | `load` | full restore into a scratch database `itms_verify_<id>`, a row-count query per table compared with counts recorded at backup, then dropped |
| postgres | `archive` | stream into `pg_restore --list` (TOC parses) with every chunk hash-checked |
| mysql | `archive` | every chunk hash-checked and the dump's completion trailer present |
| ad_system_state | `archive` | restore to scratch, `esentutl /g` integrity check of `ntds.dit` |

`restore_tested_at`, `restore_test_level` and `restore_test_scope` are stamped
from the result. Evidence and reports show the level; "sampled read" is never
presented as "full restore".

### 7.5 Integrity verification (D7)

`backup_chunks` gains `verified_at`. The verification sweep reads a bounded
budget per day (default 200 GiB per repository): first chunks never verified,
then the oldest-verified, so every chunk is re-read at least every 90 days. A
point is `verified` when its manifest opens, its pages parse, and every chunk it
references has a `verified_at`. The same sweep samples off-site objects
(`backup_chunk_copies.verified_at`, 1 % per day) by GET, decrypt and hash.

---

## 8. Monitoring and evidence

### 8.1 Health checks and tickets

Backup reuses the health module's alert lifecycle, dedup key and action
pipeline (notify, open ticket) rather than building its own. New server-evaluated
entries in `app/modules/health/registry.py`, `evaluated_by="server"`, never in a
policy push, selector = workload id:

| Check | Kind | Opens when | Clears when |
| --- | --- | --- | --- |
| `backup.success_age_h` | numeric | hours since the newest complete point exceed the threshold (seeded: warning 26, critical 50) | a newer point lands |
| `backup.last_run_failed` | boolean | the newest run is `failed` or `missed` | a later run succeeds |
| `backup.offsite_lag_h` | numeric | the newest complete point is not off-site after N hours (seeded 24) | its copy completes |
| `backup.restore_test_age_d` | numeric | no test restore within N days (seeded 35) | a test restore succeeds |
| `backup.consistency_degraded` | boolean | the newest point is `live` or `partial`, or carries a writer warning | a point without it lands |
| `backup.mssql_log_unmanaged` | boolean | §4.2 | a log backup appears |

A `backup_health_sweep` (every 15 minutes, per org) evaluates them with the same
`open_alert_for_key` / `clear_alert` calls `run_offline_sweep` uses. The seeding
migration adds the checks to each org's default health profile with
`open_ticket` on critical, so a failed or missed backup produces a ticket through
the existing `service_desk.create_ticket` path with no new ticket code. A device
with no backup workload is never evaluated, so the checks cannot page about
machines nobody asked to protect.

### 8.2 Console

Per-workload status on the device tab; point badges for consistency level,
off-site state and lock expiry; the run's warnings and writer list on the run
detail.

### 8.3 Coverage and evidence (D12)

`backup-coverage` rows become per `(device, workload)` and gain `workload_kind`,
`consistency_level`, `offsite_state`, `last_offsite_at`, `restore_test_level`.
`covered` requires a complete point within the workload's RPO (derived from its
cron, default 26 hours) and, when a target is attached, an off-site copy within
`offsite_lag_h`; a new verdict `degraded` covers `live`/`partial` points.
`fleet_backup_report` counts a device covered only when every enabled workload
is. `backup_evidence_rows` carries the same columns, so the SOC 2 / BCP export
says which workloads have an off-site copy and what level of restore was proven.

---

## 9. Security invariants

(1) `backup.run`, `backup.restore` and `backup.discover` are signed jobs; the
secret and grant exchanges carry data for a signed job, never an instruction;
the CI harness signs with a test key the agent trusts, exercising verification
rather than bypassing it. (2) No inbound path: replication is ITMS host to
off-site. (3) Repository keys and database credentials reach format-2 agents
only through §5.4; legacy dispatches are redacted and scrubbed (D2); the
off-site credential is decrypted only inside the replication worker; no secret
in argv, logs, audit, API responses or `backup_runs.error`. (4) New audit
actions: `backup.workload_*`, `backup.offsite_target_*`,
`backup.recovery_key_*`, `backup.replicated`, `backup.replication_failed`,
`backup.rehydrated`, `backup.secret_refused`, `credential.accessed`,
`backup.test_restore_dispatched`; ids and counts only. (5) Every new table
RLS-forced; the worker runs per org in `org_session`. (6) New permissions
`backup.offsite.manage`, `backup.recovery.manage`, `backup.restore.overwrite`:
org admin holds all three, technician none (keeps read, run, restore), and no
automated actor ever holds `backup.restore.overwrite`.

---

## 10. Idle budget

The engine is dormant between runs. Registration stays a map insert; nothing in
this spec adds a timer, goroutine, open handle, loaded DLL or child process to
an idle agent. Every new component allocates at job start and releases at job
end. The existing falsifiers 1–4 in `agent-engine.md` §10 stay; these are added,
each gated by an ordinary `Test`, not a benchmark log line:

| # | Falsifier | Gate |
| --- | --- | --- |
| B5 | goroutines, file descriptors or wheel entries left after a Linux live-mode run, a Postgres dump or a MySQL dump | any growth fails |
| B6 | a child process (`pg_dump`, `btrfs`, `zfs`, …) still alive after the job returns | any child fails (Linux: `/proc/self/task/*/children`) |
| B7 | a 2,000,000-entry synthetic tree backed up with peak live heap (sampled every 10 ms through `runtime/metrics`) above 96 MiB | fails; proves manifest paging (D6) |
| B8 | a 1 GiB `pg_dump` stream costing more than 64 MiB cumulative allocation | fails; proves streaming, no staging |
| B9 (Windows) | private bytes after an MSSQL or VSS job more than 4 MiB above the pre-job figure, or `odbc32.dll` still in the module list | fails |
| B10 | peak RSS of the agent process above 256 MiB during any E2E workload run (sampled by the CI harness, not the agent) | fails the E2E job |

`~0% CPU at idle` remains a design target, not a measured claim, for the reason
`agent-engine.md` §10 gives. Upload throughput is not part of the budget; it is
measured and reported in Phase 1's definition of done.

---

## 11. Server and UI changes

**Backend** (`backend/app/modules/backup/`): `workloads.py` (models, per-kind
config validation, CRUD), `schedule.py` (`last_fire`, due logic, missed rows),
`reconcile.py` (D4), `secrets.py` (§5.4 handler, registered in the gateway
beside `dedup.py` and `read.py`), `offsite.py` (targets, test, replication,
off-site GC, rehydrate), `recovery.py` (recovery keys, challenge, key objects,
catalog), `verify.py` (chunk-level verification, replacing `verify_point`'s
full read), `discover.py` (`backup.discover` job type and facts), and the
redactor and scrubber in `handler.py`. `gateway/protocol.py` gains the two
secret message kinds; `scripts/export_wire_schema.py` exports them and the
widened models.

**API** (`/api/v1/backup`, all RBAC-gated, no secret in any response):
`GET|POST|PUT|DELETE /policies/{id}/workloads[/{wid}]`; `POST
/devices/{id}/discover`, `GET /devices/{id}/discovery`; `GET|POST|PUT
/offsite-targets`, `POST /offsite-targets/{id}/test`; `GET|POST
/recovery-keys`, `POST /recovery-keys/{id}/prove`; `POST /points/{id}/restore`
(body gains `target_device_id`, `restore_kind`, `database`, `conflict`,
`confirm_name`); `POST /points/{id}/verify` (gains `level`); `POST
/points/{id}/rehydrate`; `GET /points/{id}/copies`; `POST
/workloads/{id}/run`.

**Worker beat:** `backup-reconcile` (300 s), `backup-replication` (300 s,
own queue), `backup-offsite-gc` (daily), `backup-test-restore` (daily),
`backup-health` (900 s); `backup-verification` keeps its slot with the new
budgeted body.

**Deploy:** new compose service `backup-replicator` (same image, Celery on the
`backup_replication` queue); no new `.env` key (targets and credentials live in
the database and vault). The platform backup in `docs/07-backup-restore.md`
still includes the MinIO volume; whether to exclude fleet repositories once
off-site copies exist is Q7.

**Frontend** (`frontend/src/modules/backup/`): workloads list and per-kind forms
in `BackupPolicyEditor` (pickers fed by discovery, credential picker filtered by
type, templates of §4); `OffsiteTargetsPage` with the test result per step;
`RecoveryKeyWizard` (paste public key, paste proof); `RestoreDialog` gains
Files / Database / AD tabs, target device picker, conflict mode, typed
confirmation for overwrite; point and run badges from §8.2; coverage report
columns from §8.3. Strict TS, state vocabularies from the backend models.

**Agent:** `agent/internal/capabilities/backup/` gains `workload_mssql_windows.go`
+ `odbc_windows.go`, `workload_ad_windows.go`, `workload_pg.go`,
`workload_mysql.go`, `snapshot_btrfs.go`, `snapshot_zfs.go`, `live.go`,
`secmeta_{windows,linux}.go`, `pages.go` (manifest v2), `match.go` (`**` globs),
`discover.go`; `snapshot_windows.go` gains writer checks, component mode and the
`VSS_BT_COPY` switch; `internal/secretreply` mirrors `readreply` (duplicated, not
shared, for the reason `agent-engine.md` §5 gives). `agent-engine.md` §2's
process table is extended with every new binary; Windows stays at zero spawned
processes. New binary `agent/cmd/itms-recover`.

---

## 12. Tests and CI

Unit tests first for every piece (Go through injected runners and seams as
today; Python for scheduling math, redaction, scrubbing, exchanges, replication
bookkeeping, per-copy GFS, coverage verdicts). Then three new CI jobs, run on
pushes touching backup code, on `main`, and nightly:

**`backup-e2e-linux`** (`ubuntu-latest`, steps under `sudo`). Two MinIO
containers started with `docker run` (service containers cannot pass MinIO its
`server` argument): `local`, and `offsite` with a lock-enabled bucket. The
runner image's preinstalled PostgreSQL and MySQL started natively (peer and
socket auth need a local server), MariaDB in a container. Go E2E tests (build tag
`e2e`) drive the production capability through the real read loop and dispatcher
over a fake gateway that signs jobs and mints real SigV4 POST and GET grants:

- L1 LVM root on a loopback VG (`TestARealLVMSnapshotIsTakenAndReleased` finally
  runs); L2 btrfs with a nested subvolume; L3 ZFS if the module loads, otherwise
  reported **skipped** and counted as not done; L4 ext4 live mode with a file
  rewritten during the read (asserts `changed_during_read` and `live`);
- L5 ownership, mode and xattr round trip; L6 Postgres peer and password,
  MySQL socket and `/dev/fd/3`, MariaDB, each restored under a new name and
  compared by per-table row count and checksum; L7 falsifiers B5–B8 and B10;
- L8 full stack: `docker compose` up, `scripts/verify_backup_run.py` write and
  restore legs (the restore leg runs for the first time), then replication to
  `offsite`; L9 `itms-recover` restores from `offsite` alone, with `local`
  stopped and the database dropped, and the bytes compare.

**`backup-e2e-windows`** (`windows-latest`, elevated). `minio.exe` started in the
background (service containers are Linux-only); SQL Server 2022 Express
installed by package manager. Go E2E tests as above:

- W1 VSS file backup of a test tree: writers enumerated at all three checks and
  all stable, `consistency.writers` non-empty; W2 the set is deleted and no
  shadow copy this job did not create disappeared (harness compares
  `vssadmin list shadows` before and after; the harness may spawn, the agent may
  not); W3 a file whose DACL denies SYSTEM is backed up; W4 SDDL round trip to an
  alternate path; W5 in-place overwrite with a fault injected mid-file leaves the
  original intact;
- W6 MSSQL: database with rows backed up, `msdb.dbo.backupset.is_copy_only = 1`,
  `differential_base_lsn` unchanged; restored as a new name, rows compared;
  `RESTORE VERIFYONLY` test restore passes; W7 a VSS **file** backup of the same
  volume leaves `differential_base_lsn` unchanged (D5 falsifier); W8
  `patch.scan` still succeeds after `CoInitializeSecurity` moved to `main.go`;
  W9 falsifier B9.

The writer-failure branch cannot be provoked reliably on a runner, so its
decision logic is unit-tested through an injected status source; W1 proves the
status is actually read from real writers. The first run of W1 is also the first
execution of `snapshot_windows.go` anywhere, which is why it happens on a
disposable VM and not a customer server.

**`backup-e2e-ad`** (self-hosted runner, label `windows-dc`, see Q6). A
checkpointed Windows Server VM promoted to a DC (promotion needs a reboot, which
GitHub-hosted runners cannot survive), reverted before each run: AD backup, the
NTDS writer's backup timestamp advances (`repadmin /showbackup`), restore to
staging, `esentutl /g` passes on the restored `ntds.dit`. The full DSRM
non-authoritative restore is executed once by hand in the operator's lab
following the generated runbook, and the record is attached to the Phase 3
sign-off; it is not claimed as CI coverage.

---

## 13. Phasing

Dates are targets from 2026-09-23; each phase closes on its CI jobs, not on a
date.

**Phase 1: files, local and off-site, real restore (target 2026-11-06).**
Redactor and scrubber for `repo_key` (first, D2); scheduling, TTL and reconcile
(D1, D3, D4); workloads table and migration; VSS writer checks, `VSS_BT_COPY`,
`CoInitializeSecurity` in `main.go`, backup semantics, SDDL (D5, D8, D10);
Linux btrfs, ZFS and live mode, ownership and xattrs; manifest v2 with paging
(D6); `**` matcher (D11); write-then-rename restore, conflict modes, restore to
another server (D9); off-site targets, replication, Object Lock, keyed names,
per-copy GFS, off-site GC, rehydrate; recovery keys, catalog, `itms-recover`;
chunk-level verification (D7); sampled file test restores; health checks and
tickets; coverage per workload (D12). **Done when** `backup-e2e-linux` L1–L5,
L7–L9 and `backup-e2e-windows` W1–W5, W7–W9 are green on `main`, and the report
records measured upload throughput on each runner.

**Phase 2: databases (target 2026-11-27).** Secret exchange (§5.4, repo key
leaves params for format-2 agents); `backup.discover`; SQL Server (ODBC,
staging, `COPY_ONLY`, log warning); PostgreSQL; MySQL/MariaDB; database restore
to new name, overwrite, another server; `archive` and `load` test restores.
**Done when** L6 and W6 are green, a restore-to-another-server of each engine
passes, and a 50 GB SQL Server database and a 50 GB Postgres database complete
backup, off-site replication and restore on the runners with timings recorded.

**Phase 3: AD system state (target 2026-12-18).** Component-mode VSS, NTDS /
DFSR / Registry writers, `VSS_BT_FULL`, tombstone guard, staging restore,
runbook generator, `esentutl` test restore. **Done when** `backup-e2e-ad` is
green and the manual DSRM restore record is attached. If the self-hosted runner
does not exist by 2026-12-01, AD is sold in January as preview or not at all
(Q8).

December: the operator runs Phases 1–2 on Toeshee's own servers, including one
restore of each workload from the off-site copy with the ITMS host disconnected.

---

## 14. Risks

- **R1** `iVssBackupComponentsVtbl` order and the VSS constants are still
  unconfirmed; the first Windows run may crash the agent. W1 on a disposable VM
  is where that happens.
- **R2** The ODBC path is new code against a C API driven by syscalls; driver
  quirks (message draining, `SQL_SUCCESS_WITH_INFO`) are the likely failures.
- **R3** Throughput: uploads and restores are sequential per job. A 2 TB first
  full at 100 MB/s is about six hours; over a WAN, days. Measured in Phase 1;
  parallel upload is a separate, budget-measured change. Initial seeding by
  shipped disk is out of scope.
- **R4** Off-site egress from the ITMS host and lock-extension API calls cost
  money and bandwidth; `max_mbps` bounds the first, §6.4 states the second.
- **R5** A lost recovery private key plus a lost ITMS host means the off-site
  copy is unrecoverable. By design; the possession proof and a periodic
  re-prove prompt (yearly) are the mitigation.
- **R6** Logical dumps stop being practical beyond a few hundred GB per
  database (dump window, restore time). Stated in the sales material; physical
  backups are a later phase.
- **R7** Agents older than format 2 still use `VSS_BT_FULL` for files (D5). The
  server refuses to dispatch Windows file workloads to them once the fleet
  release is available, with a visible "agent too old" run row.
- **R8** Intra-org cross-point reads through the read exchange remain possible
  (`agent-engine.md` §5, known limit); unchanged by this spec.
- **R9** AD v1 restore depends on an operator following a runbook in DSRM; a
  restore done without the `Database restored from backup` flag causes USN
  rollback. The runbook puts that step first and the tombstone guard is
  server-side.
- **R10** GitHub runner images move (`windows-latest` is moving to Server 2025);
  pin the image in the workflow and update deliberately.
- **R11** `backup_chunks` rows are still created on the agent's word
  (spec.md §5.2); chunk-level verification (§7.5) now finds a missing object
  within the verification budget rather than at restore.

---

## 15. Operator decisions still open

- **Q1 Linux without LVM/btrfs/ZFS:** (a) live mode on by default, flagged
  *Not snapshot-consistent* everywhere (recommended); (b) opt-in per policy;
  (c) refuse, as today.
- **Q2 SQL Server rights for the agent:** (a) setup snippet grants `sysadmin`
  to `NT AUTHORITY\SYSTEM` per instance (recommended; covers new databases and
  restores); (b) a vault SQL login per instance; (c) per-database
  `db_backupoperator` (new databases unprotected until granted; restore still
  needs more).
- **Q3 SQL transaction logs:** (a) never take log backups, alert on unmanaged
  logs (recommended for v1); (b) a later phase where ITMS owns log backups and
  point-in-time restore; (c) advise switching non-critical databases to SIMPLE
  in the setup guide, in addition to (a).
- **Q4 Off-site immutability default:** (a) compliance, 30 days (recommended);
  (b) governance, 30 days; (c) none.
- **Q5 Recovery key:** (a) mandatory before off-site can be enabled, generated
  offline (recommended); (b) optional with a standing warning.
- **Q6 AD CI:** (a) a self-hosted Windows Server VM runner with checkpoint
  revert on hardware the operator provides (recommended); (b) a cloud VM
  created per run (cost, cloud credentials in CI); (c) manual lab validation
  only.
- **Q7 Platform backup:** once off-site copies exist, (a) exclude fleet
  repositories from the platform backup of `docs/07-backup-restore.md`
  (smaller, faster; off-site is the copy); (b) keep including them.
- **Q8 If AD misses 2026-12-18:** (a) sell January with AD as preview;
  (b) sell without AD; (c) move the launch.
- **Q9 Test-restore defaults:** (a) files monthly sampled, databases weekly
  `archive` (recommended); (b) weekly for both; (c) off until configured.

## Operator decisions (2026-09-23)

| Question | Decision |
|---|---|
| Workloads | Windows file servers, SQL Server, AD domain controllers, Linux app/web servers, PostgreSQL and MySQL/MariaDB. |
| Storage | Local MinIO **and** an off-site S3-compatible copy. |
| SQL Server rights | Grant `sysadmin` to `NT AUTHORITY\SYSTEM` per instance (setup guide gives the command). |
| Off-site immutability | Object Lock **compliance mode, 30 days**. |
| Recovery key | **Mandatory** before off-site copying can be enabled. |
| If AD misses 2026-12-18 | Sell in January with AD backup labelled **Preview**. |
| Linux without LVM/btrfs/ZFS | Live file backup on by default, flagged "not crash-consistent". |
| SQL transaction logs | v1 takes no log backups; FULL-recovery databases without log backups raise an alert. |
| AD testing in CI | A self-hosted Windows Server VM runner that reverts to a checkpoint (the operator provides the VM). |
| Platform backup scope | The ITMS host's own backup leaves out fleet backup data once off-site copies exist. |
| Test-restore defaults | Files monthly on a sample; databases weekly with an integrity check. |

## Phase 1 server build notes (2026-09-23)

What the server side of Phase 1 built differently from the text above, and
why. Each is a deliberate choice, not an omission.

- **No workloads table yet.** Scheduling, health checks, test restores and
  coverage work per `(device, effective policy)`. `start_deadline_min` (360)
  and `max_runtime_min` (1440) are constants in `backup/schedule.py`, and the
  health alerts use selector `""`. Workloads (§3.1) are still to build; they
  replace these constants with columns.
- **`backup.restore` carries no `not_after_start`.** §5.1 defines it for
  `backup.run` only, and the agent's schema test pins the params to exactly the
  members it reads. The read exchange derives the restore start deadline
  (one hour) from the job's `issued_at` instead; the restore job lives one hour
  plus `max_runtime`.
- **Test-restore sampling waits for the agent.** A released agent verifies the
  whole point whatever `selection` says, so the monthly sampled test restore
  (§7.4) is dispatched only for points no larger than the 5 GiB sample cap until
  agents honour the selection in verify mode. Larger devices stay visible
  through `backup.restore_test_age_d`.
- **`itms-recover` is a Python reference first** (`scripts/itms_recover.py`:
  keygen, prove, list, restore, manifest v1 and v2), because the agent tree was
  another build's. `agent/cmd/itms-recover` (§6.5) reads the same layout.
- **The target test runs in the worker** (`backup-offsite-test`, every 60 s on
  the replication queue), because only the worker may decrypt the off-site
  credential; the API records the request and the console polls. The
  credentials API refuses to check out an `s3_offsite` secret.
- **`lock_mode` has no `none`.** The operator chose compliance, 30 days; a
  bucket without Object Lock is refused, not accepted with a warning.
- **Coverage.** With off-site on, a device whose newest recent point has not
  reached the off-site copy is `stale` (no new verdict value); `failing`
  includes `missed`; the report gains `offsite`, `last_offsite_at`,
  `consistency_level` and `crash_consistent`.
- **Not yet built in Phase 1:** rehydrate (§7.3), chunk-level budgeted
  verification (§7.5, D7; verification still re-reads whole points),
  `backup.consistency_degraded` and `backup.mssql_log_unmanaged` checks,
  restore to another server and conflict modes on the server (§7.1, §7.2),
  `backup.restore.overwrite`, and dispatching `format: 2` (it needs the agent
  release version to gate on; the server already reads everything format 2
  produces).
