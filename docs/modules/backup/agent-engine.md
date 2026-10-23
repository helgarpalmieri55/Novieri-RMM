# The backup engine on the endpoint (`backup.run`, `backup.restore`)

> Companion to `docs/modules/backup/spec.md`. That document specifies the
> control-plane slice and **pins** the repository format; this one records what
> `agent/internal/capabilities/backup/` actually implements, what it refuses,
> what it never executes, and the two places where the pinned contract does not
> yet carry something the endpoint needs.
>
> Spec §2 BK-02 said "the chunking/snapshot engine is agent-side and **not
> built**". This is the document that stops being true of.
> Date: 2026-08-06.

---

## 1. What is implemented

The whole of the pinned repository format (§5) and both capabilities:

| §5 element | Implementation |
| --- | --- |
| Content-defined chunking, Buzhash, 48-byte window, avg/min/max | `chunker.go`. Streaming: one buffer of exactly `max`, reused for the whole run. |
| Chunk id = `BLAKE3-256(plaintext)` hex | `blake3.go`, in-tree (see §7), pinned to the BLAKE3 team's 35 official vectors. |
| Chunk object `nonce(12) ‖ AES-256-GCM ‖ tag(16)` at `chunks/{id[:2]}/{id}` | `crypto.go`, `run.go`. |
| Store-once by id (first-writer-wins) | `run.go` + `dedup.go`: an in-run id set, then a BATCHED question to the control plane (`backup_chunk_query`, spec §5.2). The old per-chunk `Has()` against the bucket is gone — it needed a credential this agent must never hold. |
| Manifest = `{schema_version, streams:[{type:"filetree", entries:[…]}]}`, encrypted | `manifest.go`. Byte-compatible with `app/modules/backup/restore.py::seal_manifest` / `open_manifest`. |
| Restore, selection-scoped | `restore.go`. |
| Proven-restore (`mode=verify`) | `restore.go`: restores the whole point into a scratch directory this agent creates and deletes, re-hashing every chunk against the manifest. |

Both capabilities' Go structs are asserted field-by-field against the seven
exported schema files in `agent/internal/capabilities/backup/schema_test.go`.

### The two *undeclared* contracts inside the declared ones

`sources`, `dedup_params` and `target` are exported as
`type: object, additionalProperties: true` **with no properties at all**, so
the schema pins nothing about their contents. Their real shape was read off the
server rather than invented, and is pinned to the server's own source:

- `sources` = `{"paths": [...], "exclude": [...]}` — `paths` is what
  `backend/tests/test_backup_worker.py` and `test_backup_dispatch.py` build
  policies with. `exclude` is this agent's addition for §4's "include/exclude
  globs"; patterns are `path.Match`, so **`**` is not supported** and a pattern
  using it matches nothing.
- `dedup_params` = `app/modules/backup/repository.py::DEFAULT_DEDUP`. The
  agent's fallbacks are asserted equal to that dict by a test that parses the
  Python.
- `target` = `{"path": ...}` for a restore, `{}` for a verify
  (`api.py::restore_point` / `verify_point`).

An absent or empty `sources.paths` is **refused**, not treated as "back up
nothing". A policy that protects zero bytes and reports success is the one
failure mode that is invisible from the console until the day of a restore.

---

## 2. Processes this capability may spawn

Written down because an EDR team needs the list and because the answer is
unusual enough to be worth stating rather than leaving to be discovered.

> **This section said "None. Not one, on any platform." until the snapshot
> providers landed (§4).** That is no longer true on Linux or macOS, and the
> list below replaces it. The Windows half of the promise — the half this
> section exists for, and the reason the VSS provider is a COM binding and not
> a shell-out — is intact and unchanged.

### Windows: **none**

Not one process, on any path, including paths the code does not take.
`snapshot_windows.go` reaches the Volume Shadow Copy Service through the
`IVssBackupComponents` COM API.

- **never `powershell.exe`.** A SYSTEM-context process spawning PowerShell is
  among the loudest behavioural signals on a Windows 11 endpoint and trips
  Attack Surface Reduction rules directly.
- **never `vssadmin`,** and never `diskshadow.exe`. `vssadmin delete shadows`
  is a signature-grade ransomware indicator; a backup binary should not appear
  anywhere near that command line, including in a code path it does not take.

### Linux — `snapshot_lvm.go`, only during a `backup.run` carrying `snapshot: "lvm"`

| Binary | When |
| --- | --- |
| `findmnt` | once per source root, to resolve its device and mount point. Asked for `--json`; the whitespace-delimited form is kept only as a fallback, because it cannot represent a mount point containing a space and such a root would otherwise lose its backup entirely |
| `lvs` | once per source root, plus once to list volumes for the orphan sweep |
| `lvcreate` | once per distinct logical volume covered by the job |
| `mkdir` | once per snapshot, to create its mountpoint |
| `mount` | once per snapshot, `-o ro` |
| `umount` | **teardown and sweep only** — at the end of the job, and against orphan mountpoints before anything is created |
| `lvremove` | **teardown, sweep, and error-unwind only** — at the end of the job, against orphans before anything is created, and to remove a snapshot whose own `mkdir`/`mount` just failed |

### Linux — `snapshot_auto.go`, only during a `backup.run` carrying `snapshot: "auto"`

`spec-server-backup.md` §4.4. Per root: btrfs, ZFS, LVM, else a live read that
runs nothing at all.

| Binary | When |
| --- | --- |
| `findmnt` | once per source root, `-o SOURCE,TARGET,FSTYPE,FSROOT --json`, to pick the mechanism |
| `lvs` | once per root whose source is a `/dev/` device and is not btrfs or ZFS; then as in the LVM table above for the roots that are LVs |
| `lvcreate`, `mkdir`, `mount`, `umount`, `lvremove` | as in the LVM table above, for LV roots only |
| `btrfs subvolume list -o` | once per subvolume holding or nested under a btrfs root, to find nested subvolumes (snapshots are not recursive) |
| `btrfs subvolume snapshot -r` | once per such subvolume, to `<subvolume>/.itms-snap-<tag>` |
| `btrfs subvolume delete` | **teardown and sweep only** — this agent's `.itms-snap-*` snapshots, never another name |
| `zfs list` | once per dataset for the orphan sweep (`-t snapshot`), once to map child datasets (`-t filesystem`) |
| `zfs snapshot -r` | once per dataset, `<dataset>@itms-<tag>` (atomic across children) |
| `zfs destroy` | **teardown and sweep only** — `@itms-` snapshots only; a `@daily-…` or any other snapshot is never touched |

### macOS — `snapshot_apfs.go`, only during a `backup.run` carrying `snapshot: "apfs"`

| Binary | When |
| --- | --- |
| `mount` (no arguments) | once, to list mountpoints for the orphan sweep |
| `tmutil localsnapshot` | once per job |
| `mkdir` | once, to create the snapshot's mountpoint |
| `mount_apfs` | once, `-o ro` against `/System/Volumes/Data` |
| `umount` | **teardown and sweep only** |

`tmutil` is **never** invoked with `deletelocalsnapshots`, on any path: a local
snapshot on the machine may be one Time Machine itself relies on, and this
agent removes only mountpoints it created itself
(`TestSweepAPFSNeverDeletesASnapshotItDidNotCreate`).

### Properties that hold for the whole list

- **No shell.** Every one of these goes through `execSnapRunner`, which is
  `exec.LookPath` followed by `exec.CommandContext(path, args...)`. There is no
  `sh -c`, no `cmd.exe`, and no string ever handed to an interpreter.
- **Nothing is spawned outside a running `backup.run` job.** An idle agent
  spawns none of these, and neither does a `backup.restore`.
- **Any change to this list is a change to a promise.** A future provider must
  extend the table above, and on Windows must use the COM API rather than a
  shell-out for the reason §4 gives.

## 3. The other two antivirus hazards

**Controlled Folder Access** blocks writes to user directories by processes not
on Defender's allowlist, and a restore hits it as `access is denied` on the
first file under a protected folder. `restore.go` therefore does **not** abort
the job on a per-entry write failure: the path is recorded in the result's
`mismatch` list and the restore continues, so an operator sees *which* paths
were blocked instead of one opaque failure and none of the files that would
have succeeded. Allowlisting the agent binary is a deployment step and belongs
in the Windows packaging notes.

**Mass sequential reads of user documents followed by network egress** is,
behaviourally, indistinguishable from ransomware staging. Nothing in this
package can make that untrue — it is a truthful description of what a backup
does. What it can do is not add the *other* signals that turn a behavioural
score into a block, and it does not: no process spawning, no shadow-copy
deletion, no writes back over the files it reads, no renaming, and no
extension changes.

---

## 4. Gap 1: volume snapshots — implemented, and a recorded judgement was overridden to do it

> **Amended this cycle.** This section originally judged that writing a VSS
> binding from documentation alone was "not a risk worth taking", and stopped
> at stating the gap. That judgement has been **deliberately overridden** —
> see `docs/superpowers/specs/2026-08-10-snapshot-providers-design.md` §3.1a
> for the decision record. **The original reasoning is kept below, unedited,**
> because a future reader needs to know the position was held, and why it
> changed, not just that it changed. The override, what it produced, and what
> that production is and is not worth, follow after it.

`Options.OpenSnapshot` is a one-function seam. The production default
(`defaultOpenSnapshot`) **refuses**, per snapshot kind, with
"no volume shadow copy service" for `vss`.

`refused` and not `failed` is deliberate and is the module brief's own line: a
device with no snapshot provider has a standing incapacity, not a transient
error, and the reason travels into the server's hash-chained audit log.

It refuses rather than falling back to reading the live filesystem. A
crash-inconsistent backup point that *reports success* is worse than no backup
point, because it stops anyone looking.

**Why it was not written here [original reasoning, held at the time].** A
Linux container has no VSS and no APFS. An `IVssBackupComponents` binding
written against documentation alone is several hundred lines of COM vtable
calls that compile, ship to a fleet, and are first executed on a customer's
machine — where a vtable index off by one is a crash in a SYSTEM service. That
is not a risk worth taking to avoid stating a gap.

**What a real implementation owes.** On Windows, through the COM API:
`CoInitializeEx` → `CreateVssBackupComponents` → `InitializeForBackup` →
`SetContext(VSS_CTX_BACKUP)` → `GatherWriterMetadata` → `StartSnapshotSet` →
`AddToSnapshotSet` per volume → `SetBackupState(false, false, VSS_BT_FULL)` →
`PrepareForBackup` → `DoSnapshotSet` → `GetSnapshotProperties` for the device
path, and on the way out `BackupComplete` then `DeleteSnapshots` **for this
snapshot set's id only**. `Snapshot.Translate` maps an original path onto the
shadow device path; the manifest keeps recording the **original**, because a
path containing a shadow device name is meaningless to an operator browsing a
backup point and unrestorable afterwards. On macOS the equivalent is an APFS
local snapshot mounted read-only.

### 4.1 The judgement was overridden — what changed, and on what grounds

The risk above is real and was not dismissed; it was outweighed, on the
record, in `docs/superpowers/specs/2026-08-10-snapshot-providers-design.md`
§3.1a. That section originally gave **three** grounds. It gives **two** now:

1. `patch/com_windows.go` already ships exactly this trade — in-process COM
   vtable dispatch, in the same SYSTEM service, never executed, under an
   honest header. This project had already accepted the risk class once.
2. ~~Compile-time verification catches a wrong vtable index, so the stated
   failure mode is mitigated, not just risked.~~ **WITHDRAWN — disproved
   while implementing it.** `snapshot_windows.go` dispatches through a struct
   of named `uintptr` fields (`iVssBackupComponentsVtbl`), not an index
   literal. A struct whose fields are declared in the **wrong order**
   relative to the real COM interface compiles cleanly and calls the wrong
   function pointer at runtime — `GOOS=windows go vet` and `go test -c` have
   no way to catch it. This was precisely the hazard the original §4 named
   ("an index off by one is a crash in a SYSTEM service"), and the ground
   claiming it was covered turned out to be false. It is struck rather than
   deleted from §3.1a so the weakening of the case stays visible there.
3. The alternative leaves `backup.run` refusing on the entire Windows fleet
   indefinitely — no Windows endpoint has ever run this agent and none is
   scheduled.

**The residual risk is therefore larger than the override originally
claimed.** Two grounds, not three, and the one that was withdrawn was the only
one that spoke directly to the vtable-ordering hazard. Ground 1 is precedent,
not mitigation; ground 3 is a cost of *not* building, not a reduction of the
risk in what *was* built.

**What was built, and what verification it actually has.**

- **Linux (LVM), `snapshot_linux.go` + `snapshot_lvm.go`.** The dispatch file
  carries the `//go:build linux` tag; all the logic — device resolution, LV
  detection, refusals, the orphan sweep, path translation — lives in the
  untagged `snapshot_lvm.go` and is unit-tested through an injected command
  runner, on any machine including one with no LVM at all. *Updated
  2026-08-12: this coverage now runs in CI.* The paragraph here said
  "**\"Unit-tested\" means `go test ./...` passes locally, run by hand, not
  that CI has run it**", because the `agent` job runs `staticcheck` before
  `test`, staticcheck failed on ten pre-existing findings, and Actions does not
  continue past a failed step — so `test` never executed. Those ten findings
  are cleared and staticcheck reports zero, so `test` is reached and this
  coverage runs on every push.
  One additional test, `TestARealLVMSnapshotIsTakenAndReleased`
  (`snapshot_lvm_integration_test.go`), builds a real loopback volume group
  and takes a real snapshot through `openLVM` — **and skips loudly** whenever
  the LVM tooling is missing or the process is not root, which is every
  environment this project's CI or sandbox provides. A skip is not a pass:
  this is the only place any of the three providers has been asked to run for
  real, and it has never actually run. A root that does not resolve to an LVM
  logical volume — plain ext4-on-a-partition, common on cloud images and many
  desktops — is refused before anything is created; "Linux is done" does not
  follow from this work, only "Linux-on-LVM has a tested path".
- **macOS (APFS), `snapshot_darwin.go` + `snapshot_apfs.go`.** Same split:
  `snapshot_apfs.go` carries no build tag and is unit-tested, on Linux,
  through the injected runner, including the Full-Disk-Access-denial case
  (mount succeeds, tree reads empty) — same caveat as LVM above: run by hand,
  not currently executed by CI, which stops at `staticcheck` before it
  reaches `test`. `snapshot_darwin.go` is the `//go:build
  darwin` dispatch and has never executed — its only verification is
  `GOOS=darwin go vet` and a compile-only `go test -c`, which is a real but
  small check: it catches a call to something that does not exist, not
  whether `tmutil`'s output format or `mount_apfs`'s flags are actually what
  the code assumes.
- **Windows (VSS), `snapshot_windows.go`.** In-process COM, ~1500 lines,
  compile-only verified (`GOOS=windows go vet` and `go test -c`) — no test
  double is possible because `golang.org/x/sys/windows` builds nowhere else,
  so unlike Linux and macOS this provider's logic has **no unit coverage at
  all**, only type-checking. The file's own header carries a nine-item list of
  things nobody could confirm — vtable declaration order, the mangled export
  name, VSS constants, struct field offsets, the MTA-vs-STA question, and
  more — plus the withdrawn-ground note above. One of the nine is worse than
  the rest: **writer status is never checked**, and a writer that fails to
  quiesce without failing the requester's call produces **no error anywhere**
  — a `backup.run` that reports `succeeded` over mutually inconsistent files.
  That gap is this file's own admission, not something review found.

### 4.2 What the final review of this cycle found, and what it says about the above

The bullets above claimed "path translation … is unit-tested" for both
shell-out providers. **The translation was wrong and the tests did not look at
it.** LVM and APFS mapped every root to the snapshot's *mount root*, discarding
the offset between the root and the filesystem it lives on, so any root that
was not itself a mount point backed up the whole snapshotted filesystem under
the name of one subdirectory — and reported success. Windows never had the
defect; its `volumeRelative`/`shadowPath` model was correct from the start.

Nothing caught it because **every guard and every fixture in this cycle checked
existence and non-emptiness, never correspondence** — and a snapshot presenting
the wrong tree is real, readable and populated. `verifyPresents` passes on it
by construction. The fixtures compounded it: the loopback integration test
backed up the mount point itself, where the right answer and the wrong answer
are the same path.

The closure is a fixture that can tell the difference: back up a strict
subdirectory of the mount point, read a canary back *at its own relative path*
through `Translate`, and assert a sibling directory outside the root does not
appear in the walk. Read "unit-tested" in the bullets above as scoped to what
the tests actually assert, which is now stated per test rather than implied by
the file's existence.

The same review corrected four other things worth carrying here: one snapshot
is now taken per logical **volume** rather than per root (two roots on one
filesystem were two snapshots at two different instants — the very
inconsistency the mechanism exists to prevent); relative and `-`-prefixed roots
are refused in the shared core so all three providers agree; refusal reasons
and failure texts no longer carry source paths or device names into the
hash-chained audit log or `backup_runs.error`; and transient provider failures
are `state:"failed"` result documents rather than refusals, per §9's table.
`snapshot_linux.go`'s dispatch, previously covered by nothing, now has a
Linux-tagged test; `snapshot_darwin.go`'s still has none and cannot until a Mac
exists.

Everything above the seam (chunking, dedup, manifest, storage) is complete and
is exercised end to end. The seam itself now has a provider on every platform
this agent builds for, and **none of the three has completed a real
`backup.run`** — see §11.

## 5. Gap 2 — WRITES and READS both travel under short-lived object-store grants

> **This section read "Gap 2: WRITES are solved; READS are not" until the
> prefix-scoped-read cycle (2026-08-13).** The amendment immediately below it
> used to end: *"`backup.restore` remains blocked outright: a prefix-scoped
> READ grant does not exist and cannot be expressed in SigV4."* That is no
> longer true. `backupstore.Get`/`GetRef` used to refuse unconditionally,
> naming exactly that reason; they now spend a read grant the control plane
> signs on request. What follows describes what `backup.restore` does today,
> not the gap that used to block it — the original analysis stays below,
> under its own amendment, as the record of the problem the solution was
> built against.

> **Amended by spec §5.2, then again the same cycle, then again this
> cycle.** The dedup check, the chunk `Put` and the manifest `Put` travel
> under a short-lived, prefix-scoped S3 POST policy the control plane
> returns on the dedup exchange, so no credential is required on the
> endpoint and none is held. `Options.QueryChunks` is the seam that carries
> the exchange and `Options.OpenStore` is the seam that spends the grant.
> **Both are bound in production as of commit `6fea619`** (`main.go` sets
> `QueryChunks: replies.Query` and `OpenStore: backupstore.Open`). This is
> not the same as `backup.run` completing: `Options.OpenSnapshot` was nil in
> production until a later cycle too (§4), and the object-store transport
> itself has only been measured against real MinIO on Linux, for the WRITE
> half (spec §6.1 Amendment 2). The original analysis follows as the problem
> statement — read it as the state of things before `OpenStore`/`QueryChunks`
> were wired, not as current.

**What `backup.restore` does now.** A restore reads exactly two things: the
manifest once, via `GetRef`, and then chunks in a loop, via `Get`. Both are
now served by a second wire exchange, `backup_read_query` /
`backup_read_grant` (`docs/kernel/wire-protocol/README.md`), that is the READ
counterpart to §5.2's write exchange: the agent sends repository-relative
**suffixes**, never keys, and the control plane signs one GET per suffix
after checking the live `backup.restore` job's device binding and derived
`storage_ref` — never a suffix the agent could turn into a location outside
its own repository.

- **Batches of up to 256 suffixes.** `restore.go` announces its fetch order
  up front via `store.Expect(order)`; `Get` requests a window of at most 256
  suffixes at a time starting from the one currently needed
  (`readBatchMax`) — a restore's selection can run to six figures, and a URL
  per object would be a gateway round trip per chunk. A chunk shared by two
  files is announced once but fetched once per reference; the store keeps a
  bounded cache spanning the current window and the one before it
  (`maxCachedURLs`, `backupstore.go`) so a repeat that lands inside either
  still costs nothing, and a repeat further behind than that costs one more
  (small) grant for itself rather than a full window — not the collapse to
  one grant per chunk that a naive per-window cache produces on a repeat
  reference, and not unbounded memory growth over a restore of millions of
  chunks either.
- **A grant lives `min(900s, the job's remaining lifetime)`.** Short, so a
  leaked URL is worth little; clamped, so a job with a generous lifetime
  cannot silently widen the signing window. `readreply`
  (`agent/internal/readreply`) is the agent-side correlation registry for
  these grants — deliberately duplicated from `chunkreply` rather than
  shared with it, so a regression in a shared registry could not take
  `backup.run` down along with `backup.restore`.
- **Sequential fetch.** One object at a time, in the announced order.
  Parallel GETs would restore faster, but concurrency is a footprint claim
  this module measures deliberately, and parallelism was left for its own
  cycle rather than smuggled in here (see the known limit below).
- **Bounded retry: 3 attempts per object** (`readAttempts`) — one try plus
  two retries. Because a read is idempotent, a retry after a failure (an
  expired window, a transient store error) simply re-requests; a fetch that
  still fails after 3 attempts fails the restore with the reason rather than
  looping.

**Two known limits, stated plainly rather than left to be discovered**
(`docs/superpowers/specs/2026-08-13-prefix-scoped-read-design.md` §7):

- **Intra-org, cross-point reads are not prevented.** Prefix scoping stops
  **cross-tenant** access — a chunk in another organisation's repository does
  not exist under this job's prefix, so no signature can reach it — but it
  does **not** stop a compromised agent from requesting chunks belonging to a
  **different backup point of its own organisation**, if it already knew
  their content-addressed ids. Closing that needs a chunk index or a
  manifest-membership check per batch, which this cycle does not build.
- **Restore is single-threaded**, deliberately, and will be slower than the
  network allows. This is a stated design choice, not an oversight — see
  "Sequential fetch" above.

**What is proven and what is not.** Every branch of the read exchange is
unit-tested on both sides (`agent/internal/backupstore/backupstore_read_test.go`,
`agent/internal/readreply/readreply_test.go`,
`backend/tests/test_backup_read_query.py`,
`backend/tests/test_storage_presign_read.py`): an expired grant, a refused
batch, a dropped correlation, a suffix outside the prefix, a short or
truncated object, a batch boundary landing exactly on 256, and — server-side —
that an out-of-prefix key refuses the whole batch without the offending value
reaching the audit log. **None of that is the same claim as "restore is
measured against a real object store."**

And one thing here is not merely unmeasured but unmeasurable by this route, so
it must not be described as a pending measurement: **the read side has no
object-store-enforced prefix boundary to test.** The WRITE grant has one — an
S3 POST policy carries `starts-with $key`, and the store itself refuses a
violating key, which is what `moto` fails to model (spec §6.1 Amendment 2).
There is no read equivalent. The bucket carries no policy, and a presigned GET
is signed for exactly one key, so a *validly signed* URL for an out-of-prefix
key would be answered **200** — there is nothing on the store side for it to
violate. For reads the prefix boundary lives entirely in the control plane,
which derives the key from ids it is already authorized for and refuses to
SIGN anything else; that is application logic, and the unit tests above are
the right and sufficient place for it.

`scripts/verify_backup_run.py` gained a restore leg this cycle that closes a
different, real gap: write a point, restore it, compare bytes, and confirm the
exchange BATCHED rather than falling back to one grant per object. It also
retargets a signed GET at another key and expects 403 — which proves grant
REUSE resistance (SigV4 binds the resource path into the signature), not
prefix enforcement. **That leg has never run, in any environment**: this
container has no Docker daemon, so the MinIO/Traefik compose stack it needs
cannot come up. Treat the restore path as unit-tested, with its end-to-end
byte fidelity and its batching behaviour not yet measured against a real
store, and do not read a clean test run or a clean `git log` entry as evidence
otherwise.

The obvious alternative fix that was NOT taken — putting S3 credentials on the
endpoint — is the one thing this module must not do. It would be a long-lived
credential with write access to every tenant object under the configured
bucket, resident on a machine whose threat model is "may be compromised".
That is invariant #3 inverted, and it would also give a compromised endpoint
the ability to delete every other tenant's chunks. A second alternative —
relaying object bytes through the gateway, named "the strongest candidate" in
`spec.md` §6.1's original analysis — was also deliberately overridden: that
analysis credited relay with dissolving the dedup-exchange deadlock (item 5)
as a side effect, but records that same side-benefit as moot once item 5 was
fixed a different way, and the remaining case does not survive on its own —
it would put every restored byte across a WebSocket sized for job messages,
making the control plane a bandwidth bottleneck and a single point of failure
for every restore in the fleet, when this agent already reaches the object
store directly today for writes.

## 6. Two smaller deviations from §5, both deliberate

- **Manifest object name.** §5 says `{storage_ref}/manifests/{backup_point_id}`.
  The backup point does not exist when the agent writes the manifest —
  `ingest_backup_result` creates it *from* this run's result — so the agent
  mints a random 128-bit name and reports whatever reference the transport
  returns as `manifest_ref`. The server stores that reference verbatim and
  fetches with it, so the layout is honoured and only the leaf's provenance
  differs.
- **The repository index (`{storage_ref}/index`) is not written.** §5 offered it
  for agent-side dedup and server-side GC, and §5.2 superseded it: the index is
  now a control-plane TABLE (`backup_chunks`), because the party that reads it
  is the server. Dedup is served by the in-run id set plus that table, which is
  exact rather than approximate; GC is §8's
  control-plane mark-and-sweep over live manifests, which does not read the
  index. Writing a shared, mutable index object from every endpoint in a fleet
  is a lost-update problem with no locking primitive available, and it buys a
  round-trip saving. If a future phase wants it, it should be written by the
  control plane, not by agents racing each other.

---

## 7. BLAKE3 is in-tree

The agent's module graph is one WebSocket library and `golang.org/x/sys`.
Adding a third-party hash for one 200-line function, in a binary that runs as
SYSTEM on every endpoint in the fleet, buys a supply-chain surface larger than
the code it saves. `blake3.go` is the sequential reference construction — no
SIMD, no threading, no XOF, because 32 bytes is the only output width this
repository needs.

It is pinned to the **BLAKE3 team's own** `test_vectors.json` (35 cases,
lengths 0 … 102,400), not to a golden file of our own. An implementation that
is consistently wrong hashes, dedups and verifies perfectly against itself and
produces a repository that no other BLAKE3 implementation — including a
forensic tool during an incident — can read.

---

## 8. Restore: one root, and how it is held

Every byte a restore writes goes under **one root**: `target.path` for a
restore, an agent-created scratch directory for a verify. There is no
restore-to-original path in the pinned contract and this agent does not invent
one. That is what makes "nothing was written outside the target" a property
that can be asserted rather than argued.

A manifest path is normally **absolute** (the writer records `/data/x`, and the
server's own tests select on that spelling), so absolute is *confined*, not
refused: the leading separator and any drive letter are dropped and the rest
lands under the root. What is **refused** — because this package's writer
cannot produce it, so its presence means the manifest or the object store is
under someone else's control — is a `..` component, a backslash, a UNC prefix,
a NUL, or a colon outside a leading drive letter.

The symlink escape, which no lexical check sees, is held twice: symlinks are
created in a **second pass** after every file and directory, so a symlinked
parent never exists while children are being written; and every path component
is `Lstat`-ed immediately before use, so a symlink planted in the target by
something else on the machine is refused rather than followed. A destination
that is already a symlink is refused rather than truncated through.

Windows reserved device names (`CON`, `LPT1`, …) and names with a trailing dot
or space are **skipped and reported in `mismatch`**, not refused: a macOS
source tree can legitimately contain one, and refusing the whole job over a
single oddly-named file would cost a customer their entire restore.

## 9. `refused` versus `failed`

| Condition | Answer | Why |
| --- | --- | --- |
| No VSS / APFS / LVM provider in this build | `refused` | Standing incapacity of the device. |
| A `snapshot` kind this platform does not provide (`requireKind`) | `refused` | The kind arrives in a **signed** job; substituting the locally correct mechanism would discard a signed instruction. |
| A source root that is not on a logical volume (LVM) | `refused` | A standing property of how the machine is provisioned. |
| A source root that is relative, or begins with `-` | `refused` | Params this agent will not act on; a relative root resolves against the service's working directory. The leading-dash check is a **guard for a future positional call site**, not a live parsing hazard — see `validateRoots`. |
| `sources.paths` normalizes to zero roots | `refused` | Nothing to snapshot. Checked in `validateRoots` so all three providers agree; without it `openAPFS` mounted and never unmounted. |
| `lvcreate` / `mkdir` / `mount` / `tmutil localsnapshot` / `mount_apfs` failed | **`failed` result document** | Operational — a full volume group, a busy mount. |
| The orphan sweep could not reclaim a prior snapshot | **`failed` result document** | Operational. |
| The snapshot mounted but presents nothing (`verifyPresents`) | **`failed` result document** | Operational. On macOS this has **two** causes the agent cannot tell apart: a missing Full Disk Access grant, or a root that is not on the APFS data volume the snapshot covers. |
| A source root that is a **symlink onto another filesystem** (LVM) | **`failed` result document** | Configuration. `findmnt --target` resolves the symlink; the offset is computed lexically, so the root is not under the mount point reported for it. **New in this cycle** — see the note below. |
| No repository transport (§5 gap) | `refused` | Standing; names the missing contract field. |
| `repo_key` missing, not base64, or not 32 bytes | `refused` | There is no unencrypted repository format. |
| `snapshot`/`mode` outside its enum, `dedup_params` this build will not chunk with | `refused` | Params this agent will not act on. |
| `sources.paths` absent or empty | `refused` | Success over zero bytes is the worst failure a backup has. |
| A verify carrying `target.path` | `refused` | `verify_point` "never lands files at any caller-chosen destination". |
| A relative `target.path` | `refused` | Resolves against the service's working directory. |
| A manifest entry with `..`, a backslash, a UNC prefix, a NUL, a colon | `refused` | Evidence the manifest or store is compromised. |
| Object store timed out / unreachable | **`failed` result document** | Transient. |
| A source root that does not exist | **`failed` result document** | Configuration, changes day to day. |
| One unreadable file, one blocked write | recorded, job continues | A live profile always contains something locked. |

**A failed backup is reported as a SUCCEEDED job carrying `state:"failed"`.**
This looks wrong and is not: `app/jobs/engine.py::complete` runs a job type's
result handler **only** for a succeeded job, and `ingest_backup_result` is what
moves the `backup_runs` row off `running`. An agent returning a Go error for
"the object store timed out" would produce a failed job and a run row stuck on
`running` forever, with no error text anywhere an operator looks.

### What the root INDEX in a refusal or failure means

Every snapshot provider names a rejected root **by its index, never by its
path**: a refusal reason travels into the server's hash-chained audit log and a
failure's text into `backup_runs.error`, and a source path is customer data.

That index counts into the source paths **after they are trimmed,
de-duplicated and sorted** — `sourcesSpec.roots()` — which is **not the
policy's own ordering**. A policy naming

```
["/var/log", "/home/alice", "/home/alice"]
```

is normalized to `["/home/alice", "/var/log"]`, so a message about `index 1`
concerns `/var/log`. An operator counting into the policy would investigate
`/home/alice`, which was never the problem.

The `failed` result document says this in its own text as well, because that
column is what an operator actually reads; this section is for whoever reads
the doc instead.

### A symlinked source root is now a failed run

`findmnt --target` **resolves symlinks**; `rootRelative` compares **lexically**.
A policy naming `/var/www` where `/var/www -> /mnt/data/www` therefore gets
mount point `/mnt/data`, which `/var/www` is not lexically under, and the run
fails with "not under the mount point findmnt reports for it".

**This is a behaviour change for existing policies, and the right direction.**
Before C1's fix such a root computed an offset against the wrong mount point,
backed up a tree that was not the one the policy named, and **reported
success**. A loud failure replaces a silent wrong backup.

A symlink whose target is on the **same** filesystem does not fail: the offset
becomes the symlink's own lexical path inside the snapshot. What that path
resolves to inside a read-only snapshot mount is **not covered by any test in
this project** and is not claimed here either way.

---

## 10. The idle budget, measured

`agent/internal/capabilities/backup/budget_test.go`. Every number below is
produced by `testing.Benchmark` called from an **ordinary `Test`** which then
compares — `go test -bench` exits 0 on a regression, so a budget stated as a
benchmark is a log line and not a gate.

| Falsifier | Measured (this runner) | Gate |
| --- | --- | --- |
| 1 · anything resident with no job in flight | 0 allocs/op, 0 B/op, goroutine delta 0 over 576,301,626 iterations | fails on any allocation |
| 1b · any file descriptor held | 4 before, 4 after a full run **and** restore of 14 files / 28 manifest entries | fails on any new descriptor |
| 2 · RSS after a 64 MiB backup | RSS delta **−65,536 bytes**; 307 allocs; **8,476,392 bytes** allocated in total | RSS < 24 MiB; total allocation < the file's own size (67,108,864) |
| 3 · footprint scaling with file size | 1 MiB run 8,443,696 B allocated; 64 MiB run 8,476,456 B — a difference of **32,760 bytes** for 63 MiB more file | difference < 8 MiB |
| 4 · anything retained across jobs | 1 run 0 B retained, 100 runs 1,072 B | difference < 4 MiB |
| chunker inner loop | 1 alloc/op, 32 B/op per **1 MiB stream** | < 4 KiB per stream |
| goroutines across a run + restore | 2 before, 2 after | fails on any growth |

The absolute figures move a little between runs (allocation counts are stable;
RSS deltas and iteration counts are not) — the row above is one verified run,
and it is the **gate** column that is the contract.

**The RSS gate alone did not work, and that is recorded because it is the
interesting part.** With `io.ReadAll(f)` planted in `run.go`'s `file()` and
kept alive for the file's duration, the RSS-after-the-job delta read
**−131,072 bytes and the test passed**: the buffer is freed before the job
returns, `releaseMemory()` hands the pages back, and `budget.Fixed` calls
`debug.FreeOSMemory` again before reading. An after-the-fact RSS reading cannot
see a peak. Both falsifiers therefore also gate on **cumulative allocation**,
which is deterministic and machine-independent; re-planted, they fail naming
the wrong value — *401,968,344 bytes allocated to back up a 67,108,864-byte
file*.

Peak RSS (`/proc/self/status` `VmHWM`) is **logged and not gated**: it is the
whole test binary's high-water mark since start, `go test` does not order the
tests that set it, and a gate on it would pass vacuously about half the time.

**`~0% CPU at idle` remains a design target** and is described that way, for
the reason `internal/budget`'s package comment gives: a shared runner's
variance exceeds the signal, and a flaky budget test gets skipped — which reads
as coverage while measuring nothing.

---

## 11. What never executed

Stated plainly, per CLAUDE.md.

- **No real volume snapshot has ever been observed being taken, on any
  platform, though the code to take one now exists on all three.** §4 has the
  full account. Summary: `main.go` now binds `Options.OpenSnapshot` to
  `backup.PlatformSnapshot`, so a stock binary no longer refuses at this seam
  by default. On Linux, roots on an LVM logical volume get a real
  copy-on-write snapshot through `openLVM`, whose logic is unit-tested here
  via an injected command runner; the one test that takes a **real** LVM
  snapshot end to end, `TestARealLVMSnapshotIsTakenAndReleased`, **skips** in
  every environment this project has (no root, no loopback LVM tooling), so
  even that path has never been watched to actually succeed. A Linux root not
  on LVM still refuses. The macOS (APFS) provider's logic is unit-tested the
  same way, including the Full-Disk-Access-denial case, but its `darwin`
  dispatch has never executed at all. The Windows (VSS) provider has **no**
  unit coverage of its logic — only `GOOS=windows go vet` and a compile-only
  `go test -c` — and has never executed either; its header lists nine
  specific things nobody has confirmed, one of which (writer status is never
  checked) can produce a `succeeded` result over torn data with no error
  anywhere. None of this closes with "backups happen" — only with "the
  refusal at this seam is no longer unconditional".
- **No Windows or macOS endpoint ran any of this.** Every measurement and every
  round trip is Linux. `go vet` and `go build` pass for `GOOS=windows` and
  `GOOS=darwin`; nothing was executed there. In particular the following are
  compiled-and-reasoned-about rather than observed: `os.Chmod` semantics on
  NTFS (only the read-only bit moves), `os.Symlink` requiring
  `SeCreateSymbolicLinkPrivilege` or Developer Mode, Controlled Folder Access
  denials, and reserved-device-name resolution — plus, now, everything VSS's
  own header (`snapshot_windows.go`) lists as unconfirmed, and the APFS
  dispatch in `snapshot_darwin.go`.
- **No real object store — true of the Go test suite, not true overall.**
  > *Superseded 2026-08-13, quoted rather than deleted so the correction is
  > legible:* "Every test uses an in-memory fake. The first-writer-wins,
  > reference-round-trip and failure paths are exercised against it;
  > S3/MinIO semantics (eventual consistency on HEAD, multipart thresholds,
  > 5xx retry) are not." That is accurate for `go test ./...` in isolation but
  > reads as the whole picture, which it is not: it predates
  > `scripts/verify_backup_run.py`.

  Three separate claims, not one:
  - **The Go test suite** (`agent/internal/backupstore`'s unit tests) uses an
    in-memory fake exclusively. First-writer-wins, reference-round-trip and
    failure paths are exercised against it; S3/MinIO semantics (eventual
    consistency on HEAD, multipart thresholds, 5xx retry) are not — that part
    of the original sentence still holds.
  - **The WRITE path has been measured against real MinIO.**
    `scripts/verify_backup_run.py`, run behind real Traefik on `:443` on
    Linux with a real compiled agent binary and a real server-signed
    `backup.run`, proved three things `moto` could not model (it accepts a
    policy-violating key and answers 204): a POST outside `{storage_ref}/` is
    refused 403, a POST over the signed `content-length-range` is refused
    400, and a compliant POST under the same grant is accepted 204. Full
    account: `docs/modules/backup/spec.md` §6.1 Amendment 2. Multipart
    thresholds, HEAD eventual consistency and 5xx retry were not exercised by
    that run either — the amendment only claims POST-policy enforcement.
  - **The RESTORE leg has never run, in any environment.** Task 9 added a
    restore leg to `scripts/verify_backup_run.py` this cycle (byte-for-byte
    comparison against the seeded tree, a retargeted signed GET expecting
    403, and a frame count confirming the read exchange batched rather than
    falling back to one grant per object) — per the script's own docstring,
    this container has no Docker daemon, so the MinIO/Traefik compose stack
    it needs cannot come up, and "NOTHING in this script, restore leg
    included, has run in this environment." Do not read a passing CI job or a
    clean `git log` entry as evidence it has ever produced a PASS or a FAIL —
    it has produced neither.
- **The exported `Run`/`Restore` wrappers are not directly tested.**
  `jobs.VerifiedJob` is an interface with an unexported method, deliberately
  unimplementable outside `internal/jobs`, so no test can construct one. Every
  test drives the unexported `run`/`restore`, exactly as
  `internal/capabilities/site`'s do. The wrappers are three lines each —
  unmarshal-free delegation plus an `atomic.Int64` increment — and what is
  actually unexercised is that delegation, not the engine beneath it.

  An earlier revision of this section said they "are covered by the signed-job
  path in `internal/capabilities/integration_test.go` once the capability is
  registered". **That is now registered and it is still not true**:
  `integration_test.go` drives `inventory.collect`, `hello` and `net.sweep`,
  and no case was added for either backup type. It is recorded here as an
  absence rather than left as a forward-looking promise, because a promise in
  the past tense is how a gap becomes invisible.

  A further sentence here read "such a test could today only assert the §5
  refusal, since `Options.OpenStore` is nil in production". **That was stale
  and is removed:** `main.go` binds `OpenStore: backupstore.Open` and
  `QueryChunks: replies.Query`, which §5's own blockquote already says. What
  such a test would assert today is not recorded, because nobody has written
  one.
- **The registration is exercised only as a name.** `registry.go` (owned by
  another workstream; §12 has the two lines) now carries both types, and
  `capabilities.registry_test.go`'s exact-list test asserts they are present
  and resolvable. No signed `backup.run` frame has been run end to end through
  the runner.

---

## 12. Registration

Landed in `mustBuild()` in `agent/internal/capabilities/registry.go` (a file
this workstream does not own), as two lines plus the import:

```go
must(r.Register(backup.JobTypeRun, backup.Process.Run))
must(r.Register(backup.JobTypeRestore, backup.Process.Restore))
```

```go
"github.com/toeshee/it-suite/agent/internal/capabilities/backup"
```

`backup.Process` is a zero-value `Service`. Registration is a map insert:
nothing opens a file, dials, or starts a goroutine, and `Process` resolves its
own wiring at job time.

Register both **unconditionally**. An agent that advertised `backup.run` only
where a snapshot provider existed would be an agent whose backups silently stop
being scheduled, with the incapacity invisible in `agents.capabilities`; a
refusal, by contrast, arrives at the control plane with a reason and lands in
the audit log. Whether this device should be backing anything up is the control
plane's decision — it dispatches a signed job or it does not.
