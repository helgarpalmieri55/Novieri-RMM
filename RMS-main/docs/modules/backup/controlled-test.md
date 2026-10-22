# Backup — the controlled test

**Status of the capability being tested:** `backup.run` has **never completed
end to end, on any platform**, and the prefix-scoped READ leg (`backup.restore`)
has **never run against a real object store in any environment**. Those are the
project's own words — `docs/modules/backup/spec.md` §6.1 items 1 and 4, and
`HANDOFF.md` §5. The storage transport *is* proven: a POST grant was measured
against real MinIO behind real Traefik (403 outside the prefix, 400 over the
size cap, 204 compliant). Everything between "the transport works" and "a backup
happens" is what this test is for.

**The operator has decided backup ships enabled** (2026-08-16) and will exercise
it in a controlled environment. This document exists so that run produces
something diagnosable instead of the word `failed`.

---

## 1. Before the run

Backup is enabled by default; nothing needs turning on. What needs *checking* is
that the run can get past its early stages at all, because each of these
produces a **refusal** — a wire-level refusal, not a failed run — and refusals
carry less context than failures do.

| Check | Why it matters |
|---|---|
| The endpoint's root is on **LVM** (Linux), or you accept the platform's provider | Linux takes a real copy-on-write snapshot only for a root on an LVM logical volume. A Linux root **not** on LVM still refuses, before anything is read |
| `sudo vgs` shows **`VFree` ≥ 10% of the origin LV** | `lvcreate -s -l 10%ORIGIN` needs unallocated extents equal to a tenth of the *volume*, however small a source root you name. A 236G root LV needs ~24G free in the volume group to back up one directory. **`df` cannot answer this question** — it reports space inside the filesystem, and lvcreate needs space outside it, so a host showing 164G free can still have `VFree` of 0. An installer handing the whole disk to one LV produces exactly that, and it is the state the first target host was found in |
| Windows / macOS endpoints | VSS and APFS providers exist, are compile-checked, and **have never executed**. VSS in particular ships a header listing nine unconfirmed items — one of which is that writer status is never checked, so it can return `succeeded` over torn data with **no error at all**. Treat a Windows success as unverified until you have inspected restored bytes |
| The agent can reach object storage over `:443` | The whole chunk path travels under presigned POST grants to the operator hostname. `deploy/README.md`'s object-storage route section is the reference |
| A repository and a policy exist for the target device | Otherwise the job is never dispatched and there is nothing to read |

---

## 2. What to run

Dispatch a `backup.run` job at **one** device, from the console or the API,
against a **small** source root — a few hundred files, not a home directory. The
first run should fail fast if it is going to fail; a three-hour walk that dies
at the manifest stage wastes the run.

Then, and only if that succeeded, dispatch `backup.restore` for the resulting
backup point into an **empty** destination directory. This is the leg that has
never run anywhere.

---

## 3. What to collect

Four things. The first two are the ones that actually diagnose it.

1. **The agent log from the endpoint.** Every failure site in `run.go` logs a
   `Warn` naming its stage before returning. This is the only place the
   underlying error text survives — the result document is deliberately
   redacted (see §5).
2. **The job result row** — `backup_runs.error` and the job's result document,
   from the console or `GET /api/v1/jobs/{id}`.
3. **The control-plane log** for the same `request_id` / `job_id`. Structured
   JSON, one object per line; `docs/08-observability.md` §1 describes the
   correlation ids.
4. **The platform error monitor** (`/error-monitor`), in case the failure was
   an unhandled server-side exception rather than an agent-reported failure.

---

## 4. Reading the outcome

Every failure names its stage. Match the message to this table:

| Message in `backup_runs.error` | Stage that failed | What it usually means |
|---|---|---|
| *(a wire refusal, no failed run)* | Params, roots, repo key, or grant resolution | The job never started work. A refusal before the snapshot means bad parameters or an agent with no route to ask the control plane which chunks exist |
| `the volume snapshot could not be taken (…)` | **Snapshot** | The platform provider refused or errored. On Linux, a root not on LVM lands here. This is the stage that refused *unconditionally* until the provider cycle |
| `the volume group holding … has N of unallocated space but this snapshot needs M …` | **Snapshot**, pre-check | Measured, not guessed: the volume group has no room for the copy-on-write area. Extend the VG (`vgextend`) or run the test on a host with unallocated extents. The message says what `df` will not: this is space outside any filesystem |
| `an LVM snapshot could not be created for the volume holding …` | **Snapshot**, `lvcreate` | The pre-check above could not read `vgs`/`lvs` — deliberately, it stands aside rather than failing the job on an unreadable report — so lvcreate decided. Free extents are still the first thing to check, by hand this time |
| `a source root could not be read (…)` | **Walk** | The snapshot was taken but the tree could not be read. Note the message names **no path** — source paths are customer data |
| `the repository could not be reached (…)` | **Closing dedup exchange** | The agent could not complete the chunk-hash exchange with the control plane. Historically this is where the transport deadlock lived; it is fixed, but this is the stage to suspect for anything connection-shaped |
| `the manifest could not be sealed (…)` | **Seal** | Encryption of the manifest failed. Every chunk has already been uploaded at this point |
| `the manifest name could not be generated (…)` | **Name** | The agent could not mint a 128-bit name. Rare; everything before it succeeded |
| `the manifest could not be stored (…)` | **Manifest upload** | The chunks are in the repository but the manifest is not, so no backup point is created. The bytes are there and unreachable |
| `the repository transport returned an empty manifest reference` | **Manifest upload** | The store returned success with no reference. The server treats a falsy `manifest_ref` as a failure and creates no backup point |

---

## 5. Two things the messages deliberately do not tell you

- **No source path appears in any failure message.** `backup_runs.error` flows
  into reports, and a source path is customer data. The path is in the agent
  log on the endpoint, not in the control plane.
- **Errors are redacted** on the way into the result document. If the redacted
  text is not enough, the agent log has the unredacted original.

---

## 6. What a green run would and would not prove

**Would prove:** that a snapshot was taken on that platform, that the tree was
walked, that the dedup exchange completed, that chunks reached object storage
under a prefix-scoped grant, and that a manifest was sealed and stored. That is
the first end-to-end `backup.run` in this project's history.

**Would not prove:** that the data is restorable. Restore is a separate leg with
its own never-run code path, and the only way to know a backup is good is to
restore it and compare bytes. A `backup.run` that reports success and a restore
nobody has tried is exactly the state every backup product fails in.

**Would not prove anything about other platforms.** A green Linux/LVM run says
nothing about VSS or APFS.

---

## 7. Reporting back

Include the four artefacts from §3, the platform and root filesystem type, and
the stage from §4. If the run **succeeded**, say so with the restore result
alongside it — a success reported without a restore attempt is the one outcome
that would be misleading to record as "backup works".
