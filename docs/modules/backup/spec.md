# Spec — Backup Engine (Phase 6)

> Full implementation spec, expanded from `docs/modules/backup.md` via the
> brainstorming skill. The heaviest module: a built-in, content-addressed,
> deduplicated, encrypted, retention-managed backup repository with restore.
> v1 is **file-level only**; the repository format + job model are **pinned now**
> so image-level/bare-metal and app-aware backup slot in later without a rewrite.
> Reuses the vault, object storage, shared scheduler, and signed-job engine.
> Two signed agent job types: `backup.run` and `backup.restore`.
> Date: 2026-07-21.

## 1. Purpose & scope

A real backup **engine** (original code, not third-party orchestration): capture
endpoint/server file data into a per-tenant **content-addressed, deduplicated,
encrypted** repository in object storage, manage **GFS retention**, and
**restore** selected files to the original or an alternate location. Datto/Veeam
ambition, delivered in phases.

**In scope for v1 (file-level, real engine):**
- File/folder backup on Windows/Linux/macOS using OS snapshots for consistency
  (VSS / LVM / APFS) — the snapshot + chunking machinery is **agent-side**.
- Repository engine: content-defined chunking + dedup, envelope encryption
  (per-org repository key via the vault), GFS retention, object-storage backend
  (per-tenant prefix), optional site-server LAN staging (a seam only).
- Backup windows via policy → agent scheduler; dormant outside windows.
- File-level restore: browse a backup point's manifest, restore selected paths.
- Backup status & verification: per-device last-good point, repository health,
  integrity verification, and a **proven-restore** (`restore_tested`) status that
  feeds BCP/DR (Phase 10) and SOC 2 evidence.

**Explicitly out of scope for v1 (later phases, format designed to admit them):**
- Image-level / bare-metal recovery (raw-volume/filter-driver access is the
  single heaviest agent burden and breaks the idle budget — BK-01).
- Application-aware backup (VSS writers / app quiescing).
- Replication / offsite / immutable copies; instant recovery / virtual boot.

## 2. Design decisions (resolved in brainstorming)

- **BK-01 · v1 = file-level only.** Snapshot→file backup + restore. Image-level
  and app-aware are deferred; the agent's disk layer is NOT redesigned for raw
  volume access in this phase (idle-budget critical).
- **BK-02 · Control-plane slice + pinned format; engine is agent-side.** This
  phase builds the full control-plane slice (policy, repository catalog, job
  orchestration, restore coordination, key management, verification/reporting,
  RBAC/RLS/audit, frontend) **plus the pinned repository-format + wire
  contracts**. The chunking/snapshot engine is agent-side and **not built** — it
  belongs under `agent/` in this repository (it was scoped to a separate
  `it-suite-agent` repo until `CLAUDE.md` commit `a32cead`) — and is **stubbed
  in server tests**, exactly as patch/deploy mocked their agent engines.
- **BK-03 · Repository format (the crux).** Content-defined chunking (CDC,
  Buzhash rolling hash; avg ≈1 MiB, min 512 KiB, max 4 MiB). Chunk ID =
  **BLAKE3-256 of the plaintext chunk** (dedup key). Chunks stored
  **AES-256-GCM-encrypted, store-once by ID** (first-writer-wins; readers decrypt
  with the stored nonce). A **backup point = an encrypted manifest** (a tree of
  entries → ordered chunk-ID lists) plus a repository index. The manifest models
  generic **streams** (v1 stream type `filetree`); a later `block` stream
  (image-level) or `named` stream (app-aware) slots in with **no format rewrite**.
- **BK-04 · Original, license-clean code.** Uses well-understood, non-proprietary
  techniques (CDC + content-addressed encrypted chunks + packed blobs); no forked
  GPL/BSL engine code.
- **BK-05 · Encryption.** A per-org **repository master key** managed via the
  vault's envelope scheme (wrapped under the org DEK). The agent receives only a
  **job-scoped repository key** inside the signed job (decrypt-at-use); the key is
  never logged, never returned in API responses, never stored in the app DB in
  plaintext. Chunk IDs are plaintext-hashes but the ID→ciphertext map is
  per-tenant, so IDs never cross tenants (per-tenant repositories, no cross-org
  dedup — BK-03).
- **BK-06 · Restore verification (two tiers).** (a) **Integrity check** — re-read
  a backup point's chunks, verify BLAKE3 IDs + manifest completeness (cheap,
  scheduled) → `verified`. (b) **Proven-restore** — an actual test-restore of a
  selection to a scratch target, checksummed against the manifest →
  `restore_tested`, feeding BCP/SOC 2. Both in v1; proven-restore reuses the
  restore path in a verify mode.
- **BK-07 · Data model.** `backup_repository`, `backup_policy`, `backup_point`,
  `backup_run` (job-execution record), `restore_job`. Chunk/manifest are
  engine-internal (object storage), referenced by `backup_point.manifest_ref`.
- **BK-08 · Scheduling, GFS retention, GC.** Reuse `window_open` + policy
  resolution; dormant outside windows. GFS retention computed server-side to mark
  points for pruning; **repository GC** (delete chunks no live manifest
  references) is a **control-plane worker sweep** over object storage — not an
  agent job.
- **BK-09 · Job types & idle budget.** Two signed agent job types — `backup.run`
  and `backup.restore` — dormant until a window/job, allocating snapshot+chunking
  on demand and **releasing after**. Registered in all three processes (app,
  gateway, worker) with an un-masking worker test (Phase-4 lesson).

## 3. Reused kernel seams (do not fork)

- **Vault** (`app/core/vault.py`) — per-org DEK envelope encryption (AES-256-GCM).
  The repository master key is a vault-managed secret (a new `repository-key`
  credential kind, or an added `VaultService` method that seals/opens raw bytes);
  extend the vault with a bytes-oriented `seal_bytes`/`open_bytes` sibling to the
  existing string API rather than base64-through-the-string path.
- **ObjectStore** (`app/core/storage.py`) — per-tenant `{org_id}/…` prefix. Add
  backup layout: `{org_id}/backup/{repository_id}/chunks/{aa}/{chunk_id}`,
  `…/manifests/{backup_point_id}`, `…/index`. Add `put_backup_object` /
  `get_backup_object` / `delete_backup_object` / `list_backup_chunks` mirroring
  the existing artifact methods.
- **Shared scheduler** (`app/core/schedule.py`) — reuse `window_open`.
- **Signed-job engine** (`signed_envelope` + `publish_job`) — the ONLY dispatch
  path; result handlers run in the job's tenant transaction. **Publish AFTER the
  tenant transaction commits** (the fix landed in patch+deploy — do it here too).
- Device-group → org-default **policy resolution** pattern (Phase 4
  `resolve_patch_policy`).
- **Human-vs-system audit actor threading** (operator API = `user`, worker sweeps
  = `system`) — thread `actor_id`/`actor_type` through every dispatch from the
  start (Phase-4/5 lesson).
- Post-commit UI events via `queue_ui_event` (frontend refetches after mutation;
  no live-event subscription).

## 4. Data model (§4)

Tables all carry `org_id UUID NOT NULL`, RLS `ENABLE + FORCE` + `tenant_isolation`
policy `USING (org_id = current_setting('app.current_org', true)::uuid)`, and
`created_at`/`updated_at`.

- **`backup_repositories`** — `id`, `org_id`, `name`, `storage_ref` (object-store
  prefix root), `encryption_key_ref` (vault ref to the wrapped repo master key),
  `dedup_params` JSONB (chunker avg/min/max, hash algo — pinned defaults),
  `state` (active|disabled). UNIQUE `(org_id, name)`.
- **`backup_policies`** — `id`, `org_id`, `repository_id` FK, `device_group_id`
  (nullable = org-default), `sources` JSONB (path include/exclude globs),
  `window_cron` (nullable = run-on-demand only), `retention_gfs` JSONB
  (`{daily, weekly, monthly, yearly}` keep-counts), `enabled` (bool). Resolution:
  device-group → org-default → hardcoded safe defaults (Phase-4 pattern).
- **`backup_runs`** — `id`, `org_id`, `device_id`, `repository_id` FK, `job_id`
  (nullable until dispatched), `state` (pending|running|success|failed),
  `bytes_protected` (nullable), `bytes_stored` (nullable, post-dedup),
  `chunk_count` (nullable), `started_at`, `finished_at` (nullable), `error`
  (nullable), `requested_by`, `actor_type`. Index `(org_id, state)`.
- **`backup_points`** — `id`, `org_id`, `device_id`, `repository_id` FK,
  `backup_run_id` FK, `taken_at`, `manifest_ref` (object-store ref to the
  encrypted manifest), `size` (logical bytes), `dedup_stats` JSONB, `state`
  (complete|failed|pruned), `verified_at` (nullable), `restore_tested_at`
  (nullable). Index `(org_id, device_id, taken_at)`.
- **`restore_jobs`** — `id`, `org_id`, `device_id`, `backup_point_id` FK,
  `job_id` (nullable), `selection` JSONB (paths to restore), `target`
  (original|alternate + path), `mode` (restore|verify — verify = proven-restore
  to scratch), `state` (pending|running|success|failed), `restored_bytes`
  (nullable), `finished_at` (nullable), `error` (nullable), `requested_by`,
  `actor_type`. Index `(org_id, state)`.

Migration: additive Alembic revision after Phase 5's head, with the `_rls` helper
and the `backup.*` permission seed + role grants.

> **Amended 2026-10-29 (MSP clients plan 2026-10-26, Task 13 and its
> reviews): a backup repository serves one client** (migration 0242; plan
> decision C1). The reason is the key, not the row filter: a repository's
> master key travels in the signed parameters of every device that backs up
> into it, and chunks are deduplicated per repository (§5.2), so a repository
> shared by two clients would hand one client's devices the other's key.
> `backup_repositories` is C (existing repositories went to the internal
> client; a name is unique per client), and `backup_chunks` and
> `backup_chunk_copies` inherit it (H); all three carry the client policy of
> `CLAUDE.md` invariant 5a. **A policy names only a repository of its own
> client** (`its_backup_policy_repository_guard`): a group policy is its
> group's client's, and a group-less policy is one client's default. **There
> is no policy for every client**: `repository_id` is NOT NULL (0014), the
> guard refuses a client-less policy naming one, and 0242 filed the
> client-less defaults under the internal client. Resolution is device group
> → the device's client's default. A run and a
> point name a repository of their device's client (composite keys, `NOT
> VALID`, so rows from before 0242 are history and are not re-judged); the
> window refuses a device whose policy names another client's repository,
> opens no key, and records `backup.dispatch_refused` **once per device and
> policy version**. `window_cron` is validated on write (five cron fields,
> or no window: a 422 otherwise), and a stored window the sweep cannot read
> stops only its own policy's devices, recorded once per policy version, not
> the organisation's whole sweep. Points taken before the upgrade by a
> customer client's devices stay in the internal client's repository, where
> a person with every client (or with the internal client) can still browse
> and restore them; a technician narrowed to the customer client cannot see
> that repository, so for them browsing is a 404 and a restore dispatches
> nothing, and the automatic test restore passes them over (§7.4 of
> `spec-server-backup.md`). Once that client's policy names its own
> repository, retention no longer prunes them. The plan is
> `docs/superpowers/plans/2026-10-26-msp-clientes-fase2-modulos.md`.

> **Amended 2026-10-31 (phase 3 design §5.5, operator decision Q5): a
> repository can be retired.** `POST /backup/repositories/{id}/retire`
> (`backup.policy.manage` on the repository's client; another client's
> repository is a 404) sets `state = 'retired'` and is audited
> `backup.repository_retired` in that client. It is a 409 while an enabled
> policy names the repository, and a 409 on one already retired; there is no
> way back to `active`. A retired repository takes no new backup: the dispatch
> refuses it — no job, no key — and records `backup.dispatch_refused`, reason
> `repository_retired` (once per device and policy version for the window,
> every time for a person's Run now); the dedup exchange grants it no writes
> (`repository_not_active`); and no policy write may name it (`PUT /policies`,
> 409), except to switch a stored policy off as it stands. Its points stay
> where they are, still occupying their space, and stay restorable: the read
> exchange grants reads on `active` and `retired` repositories, and
> verification and the off-site copy continue. Retention and GC do not visit
> it: they visit only the repository of an enabled policy, which a retired
> one never is, so its points are never pruned. A run already in flight when
> the repository is retired can still land its point — it is the same client
> and the same key — while the dedup exchange refuses its later chunk writes.
> The `state` vocabulary is therefore `active|retired` (no CHECK constraint
> holds it, 0014).
>
> **Every backup audit that has its record's client at hand names it** (same
> date, §5.5): the restore and its result, the automatic test restore, the
> verification, the off-site copy and its failure, and the two
> reconciliations, each in its device's client. `backup.dedup_refused` and
> `backup.read_refused` name the client when the agent named a job of its own
> device, and none otherwise. `backup.offsite_gc` names none, deliberately: it
> is one summary of every target, repository and client of the organisation.

## 5. Repository format (§5) — pinned now

Engine-internal, produced by the agent, consumed on restore/verify. Specified
here and exported as wire schema so both sides agree and later stream types slot
in.

- **Chunking:** content-defined via a Buzhash rolling hash over a 48-byte window;
  a chunk boundary is emitted when the low `log2(avg)` hash bits are zero, bounded
  by min 512 KiB / max 4 MiB. Defaults live in `backup_repositories.dedup_params`
  and are pinned for a repository at creation (changing them starts a new chunk
  domain).
- **Chunk ID:** `BLAKE3-256(plaintext_chunk)` hex. Dedup is store-once by ID.
- **Chunk object:** `nonce(12) || AES-256-GCM(repo_key, plaintext) || tag(16)`,
  stored at `{storage_ref}/chunks/{id[:2]}/{id}`. First-writer-wins; a chunk whose
  ID already exists is not re-uploaded (dedup). Readers decrypt with the repo key
  and the stored nonce (nonce need not be deterministic — store-once by plaintext
  ID makes convergent encryption unnecessary and avoids its cross-file leak).
- **Manifest:** an encrypted object at `{storage_ref}/manifests/{backup_point_id}`
  containing `{ schema_version, streams: [ Stream ] }`. A **Stream** is
  `{ type, ... }`. v1 defines `type="filetree"` with `entries: [ Entry ]`, where
  `Entry = { path, kind (file|dir|symlink), mode, size, mtime, chunk_ids: [id…]
  (files only), target (symlinks only) }`. Later: `type="block"` (image: ordered
  fixed-block chunk list + volume metadata) and `type="named"` (app-aware stream).
  The `streams` list + `schema_version` are the forward-compatibility hinge.
- **Repository index:** ~~`{storage_ref}/index`~~ — **superseded by §5.2.** The
  index is a database table (`backup_chunks`), not an object, because the party
  that reads it is the control plane and not the endpoint.

### 5.2 Dedup is answered by the control plane, not by the endpoint

**This closes the first of §6.1's five gaps and is what makes `backup.run`
buildable at all.** The first-writer-wins check used to be
`ObjectStore.Has(chunks/{id[:2]}/{id})` on the agent: one HEAD per chunk,
against a bucket the endpoint has no credential for and must never be given
(invariant #3). It does not need one. The server already reasons about chunk
references — §8's GC reads manifests to decide what nothing points at — so it
can answer the question directly, and answering it is also the moment it knows
which keys the run is about to write.

**The exchange.** Two additive message kinds on the agent's existing mTLS
WebSocket (`BackupChunkQueryMsg` / `BackupChunkGrantMsg`):

- agent → server: `{job_id, query_id, check[], stored[]}`. `check` is a batch of
  content ids to partition; `stored` reports ids this agent has finished
  uploading since its last query.
- server → agent: `{job_id, query_id, new[], recorded, upload, expires_at}`.
  `new` is the subset of `check` the repository does not hold; `upload` is a
  presigned S3 POST policy scoped to `{storage_ref}/` and bounded by
  `content-length-range`.

**The query names ONE thing: a job id.** No repository id, no prefix, no org.
The server resolves org, device, repository, storage prefix and chunk bounds
from the job IT signed and the `backup_runs` row it wrote at dispatch. An
exchange that accepted a `repository_id` would answer "does repository R hold
chunk X" for whatever R an endpoint named — in a deduplicating store that is a
cross-tenant existence oracle, and there is deliberately no cross-org (indeed no
cross-REPOSITORY) dedup: `backup_chunks` is unique on `(repository_id,
chunk_id)`, so the same content id in two repositories is two rows.

**Every refusal is silent.** Unknown job, another device's job, a job that is
not a `backup.run`, an expired job, a finished run, an inactive repository —
all produce no reply and one `backup.dedup_refused` audit record. Refusals that
differed would be an oracle a compromised endpoint could walk. Grants
themselves are NOT audited per query: the authorization is already audited once
at `backup.dispatched`, and a hash-chained log whose value is that a human can
read it must not carry ~1000 records per terabyte protected (the reasoning
`_handle_health_report` records for health).

**The batching bound is 1024 ids per direction per message**, enforced at the
parse layer on the server and by the agent's own flush. A run producing six
figures of chunks therefore costs ~100 round trips per 100k chunks instead of
100k. What a pending batch HOLDS is a reference — id, path, offset, length,
about 100 bytes — never the chunk plaintext, so the agent's footprint does not
scale with the data it protects (the property `budget_test.go`'s falsifier 3
gates). The cost is that a chunk the server calls NEW is read a second time to
be sealed; that second read is re-hashed and the run FAILS on a mismatch,
because storing different bytes under a content-addressed id would corrupt
every backup point in the repository that references it.

**What the index takes on trust, and what repairs it.** A `backup_chunks` row
is created because an agent said it wrote the object. The server does not
re-HEAD to confirm — that would restore the per-chunk round trip this exchange
exists to remove, merely moved from endpoint→server to server→bucket. The
residual risk is one tenant poisoning its own repository's dedup so a later
manifest references an object nobody wrote; it is bounded to that tenant, it is
what `verify_point` detects, and §8's GC repairs it for free by reconciling the
index against the chunk objects it already enumerates.

## 6. Wire contracts (§6)

Pydantic v2, `ConfigDict(extra="forbid")`, exported via
`scripts/export_wire_schema.py`. The chunk/manifest **format** models above are
also exported (documentation + agent contract).

- **`BackupRunParams`** (job params for `backup.run`) — `repository_id`,
  `storage_ref`, `sources` (include/exclude globs), `dedup_params`,
  `repo_key` (the job-scoped repository key — travels inside the signed
  envelope), `snapshot` (vss|lvm|apfs, chosen by os_family).
- **`BackupRunResult`** — `state` (success|failed), `manifest_ref`,
  `bytes_protected`, `bytes_stored`, `chunk_count`, `dedup_stats`, `error`
  (nullable). One backup point per successful run.
- **`BackupRestoreParams`** (job params for `backup.restore`) — `repository_id`,
  `storage_ref`, `manifest_ref`, `selection` (paths), `target`, `mode`
  (restore|verify), `repo_key`.
- **`BackupRestoreResult`** — `state` (success|failed), `restored_bytes`,
  `verified` (bool — for `mode=verify`), `mismatch` (nullable list of paths that
  failed checksum), `error` (nullable).

The `repo_key` is a secret: it is present only inside the signed job params
(delivered encrypted end-to-end via the signed envelope), never in API responses,
never in audit metadata, never logged (invariant #3).

### 6.1 `storage_ref` is a KEY PREFIX — THE GRANT IS PROVEN, THE RUN IS NOT

> **Amended five times. Read the fifth amendment first.**
>
> **Amendment 5 (this cycle: prefix-scoped READ, item 1, is built).** Item 1
> under "Still open after §5.2" below — the last of the five original rows,
> and the one every earlier amendment left open — is now **CLOSED**. A new
> wire kind pair, `backup_read_query` / `backup_read_grant`
> (`agent/internal/wire/wire.go`, `backend/app/gateway/protocol.py`), lets the
> agent ask for batches of up to 256 repository-relative SUFFIXES at a time;
> `answer_read_query` (`backend/app/modules/backup/read.py`) resolves the
> live `backup.restore` job, checks the device binding and that the job's
> stored `storage_ref` matches the prefix derived from it, and signs each
> suffix with `ObjectStore.presign_backup_read` — never a suffix the agent
> could turn into a key outside its own repository, because the server joins
> it onto a prefix it derived itself. `backupstore.Get`/`GetRef` spend the
> grant; see item 1's full text below for what the capability does now and
> what its own known limits are. **This is not the same claim as "restore is
> proven."** Every branch of the exchange is unit-tested. Note that the
> enforcement question does NOT transfer from writes: a write grant is a POST
> policy carrying `starts-with $key`, which the store itself enforces (and
> `moto` fails to model — Amendment 2), whereas a presigned GET is signed for
> exactly one key and the bucket carries no read policy, so a validly signed
> out-of-prefix GET would be answered **200**. For reads the prefix boundary
> is application-level — the control plane refuses to *sign* outside the
> prefix it derives — and the unit tests are the right place for it. What
> remains unmeasured is end-to-end byte fidelity and batching, exercised by a
> restore leg `scripts/verify_backup_run.py` gained this cycle that **has
> never run**, in any environment: this container has no Docker daemon, so the
> compose stack that would host real MinIO cannot come up. See item 1 for the exact scope
> of "unit-tested" versus "unmeasured."
>
> **Amendment 4 (this cycle: snapshot providers wired).** Amendment 2 below
> names two reasons the live measurement found `backup.run` still not
> completing; the first, no snapshot provider (item 4), is no longer accurate
> as stated. `main.go` now binds `Options.OpenSnapshot` to
> `backup.PlatformSnapshot`, and
> a provider exists for all three platforms; see item 4 under "Still open
> after §5.2" for what each provider is, what it does and does not have
> verification for, and why a Linux root not on LVM still refuses. **This is
> not `backup.run` completing.** No provider has been observed taking a real
> snapshot in this project — the one end-to-end LVM test skips everywhere
> this project runs, and the Windows and macOS providers have never
> executed — and the live measurement in Amendment 2 below predates this
> change and does not cover it.
>
> **Amendment 3 (the deadlock is fixed).** Reason 2 in Amendment 2
> below — `jobs.Runner.Handle` blocking the transport's read goroutine — is
> fixed; see item 5 under "Still open after §5.2" for what changed and what the
> fix does and does not prove.
>
> **Amendment 2 (the live measurement).** `scripts/verify_backup_run.py` was
> run against real MinIO behind real Traefik on `:443`, on Linux, with a real
> compiled agent binary and a real server-signed `backup.run`. It closed items
> 2 and 3 below and it also established that **`backup.run` still does not
> complete end to end** — for two reasons neither of which is the storage
> transport, and neither of which any test in this tree could see. Both are
> written out under "Still open after §5.2".
>
> What the measurement DID prove, against a real object store rather than
> `moto`:
>
> * A POST whose `key` is outside `{storage_ref}/` is refused **403**.
> * A POST whose body exceeds the signed `content-length-range` is refused
>   **400**.
> * A POST that complies with both, under the same grant, is accepted
>   **204** — without which the two rejections above are equally consistent
>   with a route that refuses everything.
>
> These are the enforcement claims this codebase could not previously test:
> `moto` accepts a policy-violating key and answers `204` (measured, not
> assumed), so every unit test of the grant asserts the signed policy
> DOCUMENT and nothing else. That gap is now closed by measurement.
>
> **What that measurement does NOT prove**, stated so a later reader inherits
> it rather than assuming otherwise: it is **Linux only** — no Windows and no
> macOS endpoint has ever run this agent; **nothing here runs in CI** — CI's
> `agent` job (`.github/workflows/ci.yml`) does run `agent/`'s unit, schema
> and idle-budget suites, with a real Go toolchain, on every push, but
> `scripts/verify_backup_run.py` itself — the live measurement against real
> MinIO — is manual and Linux-only and has no CI job of its own, so a
> regression in what IT checks returns silently until somebody runs the
> script by hand; the `:443` certificate was a locally-issued one installed
> into the host trust store, so `backupstore`'s system-trust-roots decision is
> **exercised, not proven** against a publicly-issued chain; and the control
> plane ran in-process (`demo_e2e_agent.py`'s precedent), not as the
> `api`/`gateway` containers.
>
> **Amendment 1 (when the exchange landed).** Three of the five rows below are
> addressed by §5.2's exchange: the dedup `Has`, the chunk `Put` and the
> manifest `Put` all travel under the prefix-scoped POST grant the dedup
> answer carries, with no credential on the endpoint. What remains open is the
> RESTORE side — prefix-scoped READ. The original analysis is kept below
> because it is what the solution was built against; read it as the problem
> statement, not as the current state.

**This is an open gap, stated rather than papered over.** `storage_ref` is
`{org_id}/backup/{repository_id}` — an object-store key prefix. No endpoint, no
bucket, no credential and no expiring token travels with the job, and the agent
wire protocol has no message kind that carries object bytes, so
`agent/internal/capabilities/backup` runs with `Options.OpenStore == nil` in
production and REFUSES both `backup.run` and `backup.restore` with that reason.
Everything above the transport is built and is exercised end to end against a
fake store.

`app.deploy` had the same shape of gap and it was closed with a presigned URL
(`docs/modules/app-deploy/spec.md` §5.2). **Backup cannot be closed the same
way, and the reason is structural rather than a matter of effort.**

A presigned SigV4 URL authorises **one operation on one key**. That fits
`app.deploy` exactly: one package, one object, key known at dispatch time. It
does not fit backup, whose object keys are content-derived and therefore unknown
until the job is already running:

| Operation | Keys | Presignable at dispatch? |
|---|---|---|
| `Put` chunk (`chunks/{id[:2]}/{id}`) | BLAKE3 of plaintext, computed on the endpoint | **No** — unknowable |
| `Put` manifest (`manifests/{name}`) | random hex minted on the endpoint | **No** — unknowable |
| `Has` chunk (dedup's first-writer-wins check) | as above | **No** — unknowable |
| `Get` chunk (restore) | from the manifest; server CAN decrypt it, but a restore's selection can run to six figures of chunks | **No** — a URL each is tens of MB of job params |
| `Get` manifest (`manifest_ref`) | already in `BackupRestoreParams` | **Yes** |

So exactly one of the five is expressible as a dispatch-time presigned URL.

**What IS expressible, and is implemented and tested** (`app/core/storage.py`,
`backend/tests/test_storage_grants.py`):

- `presign_backup_write` — an S3 **POST policy**, the only S3 signing form whose
  signed document can carry a `starts-with $key` condition. It authorises writes
  anywhere under `{storage_ref}/` and nowhere else, bounded by a
  `content-length-range` so a compromised endpoint holding it cannot fill the
  tenant's storage with one object. This covers every WRITE a run performs.
- `presign_backup_object` — a single-object GET/HEAD whose key is DERIVED from
  `org_id` + `repository_id` and whose relative suffix is rejected outright if it
  contains `..`, an absolute path, a backslash or an empty segment. This covers
  the manifest read.

**What is still missing is prefix-scoped READ (and HEAD).** SigV4 query signing
cannot express it. The two designs that can, neither of which is a
control-plane-only change:

1. **On-demand grants over the existing gateway.** The agent asks for a URL for
   the suffix it needs; the server checks the suffix is under the job's own
   `storage_ref` and signs one object. Keeps zero standing credential on the
   endpoint and is enforceable server-side per request. Costs a new wire message
   kind and a round trip per chunk — acceptable for a manifest, painful for
   dedup's per-chunk `Has`.
2. **STS session credentials scoped by session policy to the prefix.** One
   short-lived credential covers all five operations. It is still a CREDENTIAL on
   the endpoint, which is the thing the agent's `openStore` refusal names as
   unacceptable, and it needs an IAM/MinIO role to exist in every deployment.

**The choice was made: option 1, batched.** §5.2 is design 1 with the round-trip
objection answered — the exchange is batched at 1024 ids and it returns the
WRITE grant in the same message, so the dedup check costs ~1/1024 of a round
trip per chunk and the writes it authorises need no second exchange. Option 2
(STS session credentials) stays rejected: it is a credential on the endpoint,
which is the thing the agent's `openStore` refusal names as unacceptable.

**Still open after §5.2:**

1. **Prefix-scoped READ**, for `backup.restore`. **CLOSED, this cycle
   (2026-08-13).**

   > This item read **STILL OPEN** — "Unchanged by §5.2 and unchanged by the
   > live measurement — a restore's chunk selection can run to six figures
   > and SigV4 cannot express a prefix GET. `backupstore`'s `Get`/`GetRef`
   > refuse, naming this. The natural extension is a third message kind that
   > returns single-object READ grants in batches, mirroring the write side;
   > it is not built. Option 4 (relay object bytes through the gateway) is
   > the strongest candidate. It would also have dissolved item 5 below as a
   > side effect, since the bytes would have travelled on a path the agent is
   > already reading — moot now that item 5 is fixed a different way" — from
   > when this row was written until the prefix-scoped-read cycle. That text
   > is kept above (Amendment 1) as the problem statement; what follows is
   > what was built against it.

   **What was built.** A new wire kind pair, `backup_read_query` /
   `backup_read_grant` (`agent/internal/wire/wire.go`,
   `backend/app/gateway/protocol.py`). The query carries repository-relative
   **suffixes**, never full keys, up to `READ_BATCH_MAX`/`readBatchMax`
   (256) per batch; the server joins each suffix onto the prefix it derives
   from the job's own resolved repository, so an agent cannot express a
   location outside its own repository — the boundary is a property of the
   construction, checked a second time by the suffix validator
   (`_safe_object_suffix` / `ObjectSuffix`) and a third time by the object
   store's own key handling. `answer_read_query`
   (`backend/app/modules/backup/read.py`) resolves the live `backup.restore`
   job — a `RestoreJob` in state `running`, targeting the requesting device,
   not expired — then checks the repository's stored `storage_ref` against
   the prefix the server derives itself before signing anything via
   `ObjectStore.presign_backup_read`. As with the write side's
   `BackupChunkGrantMsg`, there is deliberately **no error variant on the
   wire**: every refusal — unknown job, wrong device, wrong job type, an
   expired job, a restore that is not running, a `storage_ref` that fails
   the derived-prefix check, an unsafe suffix — is silence plus one
   `backup.read_refused` audit record, never a distinguishable reply, because
   a reply that said "no such job" and one that said "not your job" would
   together be an oracle a compromised endpoint could walk. A grant's
   lifetime is `min(900s, the job's remaining lifetime)` — short so a leaked
   URL is worth little, clamped so a generously-lived job cannot silently
   widen the signing window.

   On the agent, `readreply` (`agent/internal/readreply`) is a correlation
   registry for inbound grants — deliberately **duplicated from
   `chunkreply` rather than generalised with it**, so a regression in a
   shared registry could not take down `backup.run` along with
   `backup.restore`. `backupstore.Get`/`GetRef` spend the grant:
   `restore.go` announces its fetch order up front via `Expect(suffixes)`,
   and `Get` fetches through a window of up to 256 grants at a time,
   sequentially, retrying a failed fetch up to 3 attempts total — because a
   read is idempotent, an expired URL inside a window costs a re-request,
   not a failed restore.

   **Relay through the gateway (Option 4) was deliberately overridden, not
   merely passed over.** This section's own text above named it "the
   strongest candidate" and credited it with dissolving item 5 (the
   dedup-exchange deadlock) as a side effect — but that side-benefit is
   recorded in that same paragraph as moot, since item 5 was fixed a
   different way (see item 5 below), and the remaining case for relay does
   not survive on its own: it would put every restored byte across a
   WebSocket sized for job messages, making the control plane a bandwidth
   bottleneck and a single point of failure for every restore in the fleet,
   when the agent already reaches the object store directly for writes — a
   path already proven against real MinIO (Amendment 2). Full design
   record: `docs/superpowers/specs/2026-08-13-prefix-scoped-read-design.md`
   §2.1.

   **Two known limits, carried forward rather than discovered later**
   (design doc §7, restated unsoftened in `agent-engine.md` §5): prefix
   scoping stops **cross-tenant** access but does **not** prevent
   **intra-org, cross-point** reads — a compromised agent could request
   chunks belonging to a different backup point of its own organisation if
   it knew their ids, which prefix scoping alone cannot see; and restore
   fetches **sequentially**, by design, so it will be slower than the
   network allows.

   **What is proven and what is not.** Every branch of the exchange is
   unit-tested on both sides — an expired grant, a refused batch, a dropped
   correlation, a suffix outside the prefix, a batch boundary landing
   exactly on 256, and (server-side) that an out-of-prefix key refuses the
   *whole* batch without echoing the offending value into the audit log.
   Amendment 2's enforcement claim does **not** carry over from writes, and
   this is a property of S3 rather than a gap to be closed later. A write
   grant is a POST policy carrying `starts-with $key`, enforced by the store
   itself (which `moto` does not model). A presigned GET is signed for
   exactly one key and the bucket carries no read policy — so a *validly
   signed* out-of-prefix GET would be answered **200**; there is nothing for
   it to violate. For reads the prefix boundary is entirely application-level:
   the control plane derives the key from ids it is already authorized for and
   refuses to sign anything else, which is what the unit tests above cover.
   What is genuinely unmeasured is end-to-end byte fidelity and batching.
   `scripts/verify_backup_run.py` gained a restore leg this cycle for exactly
   those (write a point, restore it, compare bytes; count `backup_read_grant`
   frames to confirm the exchange batched rather than falling back to one
   grant per object; and retarget a signed GET at another key, which proves
   grant REUSE resistance — SigV4 binds the resource path into the signature —
   not prefix enforcement) — and that leg **has never run, in any
   environment**: this container has no Docker daemon, so the MinIO/Traefik
   compose stack it needs cannot come up. Read "the READ half is built" and
   "the READ half is measured against a real store" as different claims
   until that leg is actually run.
2. **The `v`/`kind` framing for the two new kinds** in `agent/internal/wire`.
   **CLOSED.** `Decode` returns a `*wire.ChunkGrant` for an inbound
   `backup_chunk_grant` and `Encode` frames an outbound `backup_chunk_query`;
   `main.go` routes grants to `internal/chunkreply` and everything else to the
   runner. Measured on the wire, not only in unit tests: a real agent sent a
   real query and the control plane published a real grant for it.
3. **An `ObjectStore` implementation that spends a POST grant.** **CLOSED.**
   `agent/internal/backupstore` posts the signed form over https with system
   trust roots, refusing plaintext and refusing redirects, and `main.go` binds
   it to `Options.OpenStore`. The grant it spends is the one measured above:
   403 outside the prefix, 400 over the cap, 204 when compliant.
4. **Snapshot providers now exist for all three platforms and `main.go` wires
   them; a stock binary still does not complete `backup.run` on any
   platform.** **CHANGED, this cycle.** `main.go` now binds
   `Options.OpenSnapshot` to `backup.PlatformSnapshot`, so `run.go`'s
   pre-storage snapshot step no longer refuses unconditionally. What that
   step actually does depends on platform and, on Linux, on the root:

   - **Linux (LVM).** A root that resolves to an LVM logical volume gets a
     real copy-on-write snapshot via `openLVM`
     (`snapshot_linux.go`/`snapshot_lvm.go`). The dispatch/refusal/sweep logic
     is unit-tested through an injected command runner — as part of `go test
     ./...`, which passes locally, but **not currently as part of CI**: the
     `agent` job runs `staticcheck` before `test`
     (`.github/workflows/ci.yml`), staticcheck fails on ten pre-existing
     findings unrelated to this work, and Actions does not run a later step
     after an earlier one fails, so `test` — and this coverage with it — does
     not currently execute in CI (`HANDOFF.md` §6). The one test that takes a
     **real** snapshot end to end —
     `TestARealLVMSnapshotIsTakenAndReleased`
     (`snapshot_lvm_integration_test.go`), building a real loopback volume
     group — **skips** in every environment this project has (it needs root
     and the LVM tool chain), so even the Linux path has never been *watched*
     to succeed; a skip is not a pass. A root that is not on an LVM logical
     volume — plain ext4-on-a-partition, common on cloud images and many
     desktops — still refuses, before anything is created.
   - **macOS (APFS).** `snapshot_apfs.go`'s logic (including the
     Full-Disk-Access-denial case, where the mount succeeds and the tree reads
     empty) is unit-tested the same way as Linux's — same caveat: passes
     locally, not currently reached by CI. The `snapshot_darwin.go`
     dispatch that selects it has never executed; its only verification is
     `GOOS=darwin go vet` and a compile-only `go test -c`.
   - **Windows (VSS).** `snapshot_windows.go` is in-process COM, ~1500 lines,
     with **no unit coverage of its logic at all** — `golang.org/x/sys/windows`
     builds nowhere but Windows, so there is no seam a fake can stand behind.
     Verification is the same compile-only `go vet`/`go test -c` pair. Its own
     header lists nine specific unconfirmed items; the worst is that writer
     status is never checked, so a writer that fails to quiesce without
     failing the requester's call can yield `status=succeeded` over mutually
     inconsistent files, with no error anywhere. This gap is the reason
     `agent-engine.md` §4's original judgement — that writing this binding
     from documentation alone was "not a risk worth taking" — was overridden
     rather than silently reversed; see §4 there and
     `docs/superpowers/specs/2026-08-10-snapshot-providers-design.md` §3.1a for
     the decision record, including a ground that was withdrawn after
     implementation disproved it.

   **None of this has been observed completing a `backup.run` end to end, on
   any platform.** The live measurement in Amendment 2 above predates this
   change and does not cover it. This is the same gap BK-01 always named,
   narrowed rather than closed: "the storage transport is built" and "backups
   happen" remain different sentences, and on Linux "LVM has a tested
   provider" and "a Linux backup happens" remain different sentences too.
5. **The dedup exchange deadlocked against the agent's own transport.**
   **FIXED, this cycle.** `jobs.Runner.Handle` used to be called on the
   transport's READ goroutine and block for the whole capability — stated in
   its own doc comment and deliberate, so that job ordering was the wire's
   ordering. §5.2's exchange needed the opposite: `backup.run` sends a
   `backup_chunk_query` and blocks waiting for the `backup_chunk_grant` on the
   same connection, whose only reader was the goroutine the run was
   occupying. This was measured with a build that got past item 4: the server
   answered and published the grant, the grant sat unread for the full 60s
   `chunkreply` deadline, the run failed naming the unanswered query, and only
   after `Handle` returned did the agent read it and log `dropped a backup
   chunk grant that nothing was waiting for`. The blocked reader also sent no
   WebSocket pong, so the gateway closed the connection at the same moment.

   **What changed.** `Handle` still decodes, verifies and looks up the job
   inline, in wire order — that part of the contract is unchanged — but now
   submits the verified job to a `Dispatcher` (`agent/internal/jobs/
   dispatcher.go`) and returns immediately instead of running the capability
   itself. The dispatcher runs one worker goroutine per capability key,
   spawned on first use and exited when its queue drains: concurrent across
   capabilities, strictly serial within one (so `backup.run` and
   `backup.restore` still never overlap), a heavy-capability budget of 2 for
   `{backup, patch, app, script}`, and a bounded queue (depth 8) per
   capability that refuses a job visibly, through the existing `jobs.Refuse`
   path, rather than blocking or growing without bound. Both numbers are
   reasoned defaults, not measured ones. The read goroutine is
   free the instant `Submit` returns, so it is there to read the grant when
   the server sends it. Full design:
   `docs/superpowers/specs/2026-08-09-agent-job-concurrency-design.md`.

   **Proven, not asserted.** Every test of this exchange had injected
   `Options.QueryChunks` and answered it in-process — the seam that could not
   see this deadlock, because it never connected the capability to the real
   read loop. A new harness does
   (`agent/internal/capabilities/concurrency_harness_test.go`): it composes
   the real read loop, `Handle` and a real capability over a fake gateway. On
   that harness the deadlock test failed against the pre-fix code with the
   same symptom the live run measured — a genuine ~5s timeout,
   `chunkreply: no grant returned before the deadline` — and passes in
   0.02–0.05s after the fix (measured on this run, on Linux, against the fake
   gateway). Also falsified rather than assumed: no job lost across a worker
   restart, a job survives the connection being dropped under it and answers
   after the reconnect, no worker goroutine survives an idle queue, the heavy
   budget holds while light capabilities are exempt, and the replay-defence
   tests no longer depend on goroutine scheduling to pass.

   **These are gated in CI.** They are ordinary Go tests under
   `agent/internal/`, and the `agent` job in `.github/workflows/ci.yml` runs
   `go test ./... -count=1` on every push. **What is not gated is `-race`** —
   the CI test step omits it deliberately, because the race detector's shadow
   memory would corrupt the idle-budget RSS thresholds measured in the same
   job — so the `-race` verification above is local only.

   **One caveat, verified on the real run.** The `agent` job runs
   `staticcheck` BEFORE `test`, and staticcheck currently fails on ten
   pre-existing findings, so Actions skips the test step: run 31333534459 has
   staticcheck `failure`, test `skipped`. The gate is configured and correct
   and is blocked by an unrelated red step above it.

   **What this does not prove.** The concurrency model is verified on Linux,
   against a fake gateway — not against a real gateway under real load, which
   is the operator's deploy-validation step, not something this suite can
   claim. And fixing this deadlock does **not** make `backup.run` complete:
   item 4 above now has a wired provider on every platform, but none has been
   observed completing a real `backup.run`, and a Linux root off LVM still
   refuses outright.

**What is now measured, and what still is not.** The POST policy's conditions
are enforced by S3/MinIO, not by this process, and `moto` does not model that
enforcement — it accepts an upload whose `key` violates the signed policy and
answers `204`, measured rather than assumed. The unit tests therefore assert
the signed policy DOCUMENT, which is the part this codebase can get wrong; the
ENFORCEMENT is now measured by `scripts/verify_backup_run.py` against real
MinIO. That script is **manual and Linux-only, and no CI job runs it**, so
this is a measurement taken once, not a guard that holds — unlike `agent/`'s
own unit, schema and idle-budget suites, which the `agent` CI job does run on
every push.

## 7. Encryption & key management (§7)

- On repository creation, generate a random 32-byte **repository master key**,
  seal it via the vault (`encryption_key_ref` = the vault reference), and store
  only the ref. The plaintext key exists transiently at point of use.
- On `backup.run`/`backup.restore` dispatch, the control plane opens the repo key
  from the vault and places it in the **signed job params** (`repo_key`). Because
  the job envelope is Ed25519-signed and delivered over mTLS to one device, the
  key is bound to that job. The agent holds it only for the job's lifetime.
- Chunks/manifests/index are AES-256-GCM under the repo key. No plaintext backup
  data or key is ever written to the app DB or logs.

## 8. Scheduling, retention & GC (§8)

- **Windows:** `backup.run` dispatched when `window_open(policy.window_cron, now)`
  is true (or on-demand via API). Dormant otherwise.
- **GFS retention:** on each new `backup_point`, compute which points to keep per
  `retention_gfs` (keep N daily, N weekly, N monthly, N yearly per device/repo);
  mark superseded points `state=pruned` (metadata) — this does NOT yet delete
  chunks.
- **Repository GC (control-plane worker sweep):** periodically, per repository,
  compute the set of chunk IDs referenced by all live (non-pruned) manifests and
  delete unreferenced chunk objects from storage (mark-and-sweep). Runs in the
  worker, org-scoped, operating on object storage directly — never an agent job.
  Audited `backup.gc`.
- **Verification sweep (worker):** periodically dispatch integrity checks for
  points past a staleness threshold → set `verified_at`.

## 9. Restore & verification (§9)

- **Restore:** operator browses a backup point's manifest (server decrypts +
  returns the filetree listing — metadata only, no file contents through the API),
  selects paths, and dispatches `backup.restore` (mode=restore) to the device.
- **Proven-restore:** `restore_jobs.mode = verify` dispatches `backup.restore`
  with a scratch target; the agent restores + checksums against the manifest and
  reports `verified` + any `mismatch`. Success sets
  `backup_points.restore_tested_at` → feeds BCP/SOC 2.
- **Integrity check:** a lighter server/worker-initiated verification re-reads
  chunk objects and checks BLAKE3 IDs + manifest completeness → `verified_at`.

## 10. Reporting (§10)

Read-only, org-scoped: `device_backup_status` (last-good point, last run state,
verified/restore-tested timestamps, repository), `fleet_backup_report` (coverage
%, success rates, points needing verification, dedup savings), and a
`backup_evidence_rows` export (per-device backup coverage + tested-restore status
for SOC 2 / BCP). No writes.

## 11. RBAC (§11)

Permissions (seeded in migration + `app/core/rbac.py`): `backup.read` (view
repositories/policies/points/status), `backup.policy.manage` (create/edit
repositories + policies), `backup.run` (trigger an on-demand backup),
`backup.restore` (browse + restore/verify). Role grants (mirror prior modules):
super/org-admin → all; technician → read + run + restore (NOT policy.manage);
auditor → read; read-only → read. **Restore is privileged** (it moves data onto
endpoints) — gate it carefully and audit every dispatch.

## 12. API (§12)

Routes under `/api/v1/backup`, each RBAC-gated server-side; Pydantic response
models with **no secret fields** (never return `repo_key`/`encryption_key_ref`):
- `GET/POST /repositories` (`read` / `policy.manage`), `GET/PUT /policies`
  (`read` / `policy.manage`).
- `POST /repositories/{id}/retire` (`policy.manage` on the repository's client;
  409 while an enabled policy names it; see the 2026-10-31 amendment in §4).
- `POST /devices/{id}/run` (`backup.run`, computes `now` in-handler, `actor=user`).
- `GET /points` + `GET /points/{id}/manifest` (`backup.read`; manifest listing is
  metadata only), `GET /devices/{id}/backup` (device status tab data).
- `POST /points/{id}/restore` and `POST /points/{id}/verify` (`backup.restore`).
- `GET /report` + `GET /report/export` (`backup.read`; export audited
  `backup.evidence_exported`).
`register(app)` calls `register_job_types()` then mounts the router; `org_id`/
`actor_id` from `AuthContext`, never the request body.

## 13. Worker (§13)

- `backup_window_sweep_all_orgs(now)` — per org, dispatch `backup.run` for policies
  whose window is open and whose device is due.
- `backup_retention_gc_all_orgs(now)` — GFS marking + repository mark-and-sweep GC.
- `backup_verification_sweep_all_orgs(now)` — dispatch integrity checks for stale
  points.
- Beat entries; task shells compute `datetime.now(UTC)` (only there).
- **`register_worker_job_types()` MUST register `backup.run` + `backup.restore`**,
  with the un-masking test that drives the worker bootstrap (the Phase-4 Critical
  lesson — do it from the start).

## 14. Frontend (§14)

`frontend/src/modules/backup/`: `RepositoriesPage` + `BackupPolicyEditor`
(`backup.policy.manage`-gated), `BackupPointsPage` (browse points, per-device
status, verified/restore-tested badges), `RestoreDialog` (browse manifest tree →
select paths → restore/verify, `backup.restore`-gated), `DeviceBackupTab` in the
device-detail page. ApiClient methods on the central client; module hooks with
query-key invalidation (no live event). Strict TS, no `any`. State vocabularies
sourced from the backend response models (no invented names — Phase-5 lesson).

## 15. Security invariants & idle budget (§15)

1. **Signed jobs only** — `backup.run`/`backup.restore` dispatch exclusively via
   the kernel engine; the `repo_key` rides inside the signed envelope. No unsigned
   path.
2. Agents outbound-only mTLS (kernel; unchanged).
3. **Repository keys are vault-sealed, decrypt-at-use, job-scoped** — never
   logged, never in API responses, never in the app DB in plaintext; backup data
   is encrypted at rest under the org's repository key.
4. **Audit** every privileged action: repository/policy create-edit, backup
   dispatch + result, restore/verify dispatch + result, GC, evidence export —
   `actor_type` user-vs-system correct.
5. **Tenancy** — every query over the 5 tables + Device carries an explicit
   `org_id` predicate on top of RLS; repositories are per-tenant (object-store
   prefix `{org_id}/backup/…`); no cross-org dedup or chunk sharing.
6. **RBAC** — server-side on every route and dispatch; `backup.*` matrix seeded
   and matrix-tested.
- **Agent idle budget** — `backup.run`/`backup.restore` capabilities are dormant
  until a job; snapshot + chunking allocate on demand and **release after**. v1
  needs snapshot APIs + file reads only, NOT raw-volume/filter-driver access.

## 16. Testing (§16)

Test-first. The agent engine is stubbed by a deterministic **fake chunker** in
server tests (produces reproducible chunk IDs + manifest from fixed input) so the
control-plane slice is fully exercised without the real Go engine. Security paths
get explicit tests: signed dispatch (no unsigned path), `repo_key` carried only in
signed params and never in API responses/audit/logs, RBAC matrix (positive +
negative per route), tenancy (cross-org points/repos never visible; per-tenant
object prefixes), GFS retention selection math, GC mark-and-sweep (unreferenced
chunk deleted, referenced chunk kept — the load-bearing correctness test),
manifest round-trip encrypt/decrypt, restore selection + proven-restore verify
status, actor attribution (API=user, sweep=system), and the worker-registration
un-masking test. Frontend: vitest render + RBAC-gating + mutation invalidation +
real state vocabulary.

## 17. Deploy (§17)

Docs-only unless a config knob is needed (prefer none — repository/dedup params
live in the DB). Document: object-storage capacity/retention implications, the
per-tenant repository layout, envelope key management via the vault, the three
beat sweeps (window / retention-GC / verification), and the site-server LAN-cache
seam (not built in v1). Validate `docker compose config`.

## 18. Spec coverage map

§4 data model (BK-07), §5 repository format (BK-03/BK-04), §6 wire contracts,
§7 encryption/keys (BK-05), §8 scheduling/GFS/GC (BK-08), §9 restore/verify
(BK-06), §10 reporting, §11 RBAC, §12 API, §13 worker (BK-09 + registration),
§14 frontend, §15 invariants + idle budget (BK-01), §16 testing (BK-02 fake
chunker), §17 deploy. The agent engine is unbuilt and mocked in server tests (BK-02); the format
is pinned for image-level/app-aware to slot in via the `streams` abstraction
(BK-03).
