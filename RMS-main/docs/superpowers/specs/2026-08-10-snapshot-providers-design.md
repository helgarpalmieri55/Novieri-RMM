# Design — snapshot providers for `backup.run`

**Status:** approved in brainstorming, not yet implemented.
**Scope:** implement `Options.OpenSnapshot` for Windows (VSS), macOS (APFS) and
Linux (LVM), and wire it at the composition root.
**Out of scope:** prefix-scoped READ / `backup.restore` (spec §6.1 item 1); the
Option 4 relay; unblocking the agent CI job.

Read `CLAUDE.md` first. The agent idle budget governs this work, and the
"every job is signed and verified" invariant is why §1.2 refuses rather than
reinterprets a snapshot kind.

---

## 0. Why this cycle

`agent/cmd/agent/main.go` never sets `backup.Options.OpenSnapshot`, so
`defaultOpenSnapshot` (`backup.go:300-312`) refuses `vss`, `apfs` and `lvm`. A
stock binary therefore refuses **every** `backup.run` before a chunk query
leaves the machine, on every platform.

That is now the **only** reason `backup.run` refuses. The previous two cycles
closed the others: the dedup exchange has wire framing, a correlation registry
and a POST-grant object store with its enforcement measured against real MinIO;
and the transport deadlock — where a job waited for a grant only the goroutine
it occupied could read — is fixed by the per-capability `Dispatcher`.

`defaultOpenSnapshot` refuses deliberately, and its reasoning constrains this
design: backing up a live filesystem produces a backup point that **restores
torn files and reports success**. No provider here may fall back to the live
filesystem, and no failure here may be silent.

---

## 1. Resolved decisions

Each was a real choice with a rejected alternative, recorded so a later session
does not silently re-litigate one.

### 1.1 The verification bar is `com_windows.go`'s

**Chosen:** write the real providers; verify Windows and macOS by `GOOS`-targeted
`go vet` and a compile-only `go test -c`; ship each with a header stating it has
never executed, what verification it does have, and what would falsify it.

This is not a new convention. `internal/capabilities/patch/com_windows.go`
already ships in-process COM under exactly such a header. The precedent, and the
honesty it encodes, is the reason this bar is acceptable.

**Rejected — Linux only, others when hardware exists:** leaves two thirds of the
fleet refusing and defers the interface pressure that having three
implementations puts on the contract.

**Rejected — design the contract, implement nothing:** `backup.run` still
refuses everywhere at the end of the cycle.

### 1.2 A kind that does not match the platform is refused, not reinterpreted

**Chosen:** a Windows agent asked for `lvm` refuses, naming both what the job
asked for and what this endpoint provides.

**Why:** `p.Snapshot` arrives inside a cryptographically signed job, and
`wire.go:321` already restricts it to `vss`/`lvm`/`apfs` with no fallback. On a
platform where signing exists so the endpoint does what the server actually
said, silently substituting a different mechanism is the wrong instinct. A
refusal puts the server's misconfiguration in the job record.

**Rejected — ignore the kind and let the build decide:** always does the locally
correct thing, but discards a signed instruction and makes the wire field
decorative.

### 1.3 A Linux root that is not on an LVM logical volume is refused

**Chosen:** refuse, naming the root and what it is actually on.

**Stated plainly:** this means `backup.run` still refuses on plain
ext4-on-a-partition, which is common on cloud images and many desktops. Linux is
also the only platform this cycle can verify. The coverage this cycle delivers
is therefore narrower than "three platforms" suggests.

**Rejected — add btrfs subvolume snapshots too:** a fourth provider in a cycle
already carrying three, with a different snapshot model. A candidate for its own
cycle if the fleet needs it.

**Rejected — fall back to the live filesystem:** forbidden by §0.

### 1.4 Orphans are swept by agent-owned name tag, before a new snapshot

**Chosen:** every snapshot the agent creates carries an identifiable
agent-owned name. Before taking a new one, the provider releases any
pre-existing snapshot bearing that tag.

**The tag is the literal prefix `toeshee-snap-`**, followed by the job id. So an
LVM snapshot LV is named `toeshee-snap-<job_id>`, and the sweep removes any LV
whose name starts with `toeshee-snap-`. Mountpoints the agent creates live under
`/var/lib/toeshee-agent/snap/` on Linux and macOS, and the sweep unmounts and
removes any directory there. Both are exact strings, not conventions to be
re-derived: a sweep matching a wider pattern than it created is §4's
`Sweep filter` falsifier.

**Why:** `Release()` runs from a `defer`, and a crashed or killed process never
runs defers, so orphans are guaranteed eventually. This is self-healing, needs
no persistent state, and only ever touches snapshots the agent itself created.

**The rule is not uniform across platforms, and must not be** — see §3.2 for
macOS, where the agent cannot name the snapshot and deleting the wrong one would
interfere with the user's Time Machine.

**Rejected — persist a record and reconcile at agent start:** cleans up sooner,
but adds persistent state the agent must keep consistent, and a stale record is
its own failure mode.

### 1.5 A snapshot is verified to present the data before it is used

**Chosen:** after opening, count the immediate children of each root, live
versus in the snapshot. A root that is populated live and empty in the snapshot
refuses.

**Why:** on macOS without a Full Disk Access grant, `mount_apfs` can succeed
while the mounted tree presents user directories as empty. The backup then
completes, uploads almost nothing, and **reports success** — §0's hazard wearing
a green result. An existence-and-readability check does not catch this: the
directory exists and opens; it is empty.

**The exact rule**, so it is not re-derived: for each root, count immediate
children (one `readdir`, not a recursive walk) of the live path and of the
translated path. **Refuse when the live count is greater than zero and the
snapshot count is zero.** Nothing else refuses — not a smaller count, not a
larger one. A root that is genuinely empty live is fine; a root that is
populated live and empty in the snapshot is the TCC-denial and bad-mount
signature.

**Cost, stated:** one `readdir` per root. Small divergence between live and
snapshot is normal on a running system, which is why only the stark case
refuses.

### 1.6 Providers live in the capability package, wired at the composition root

**Chosen:** build-tagged files in `agent/internal/capabilities/backup/`, and
`main.go` sets `Options.OpenSnapshot` explicitly.

`patch` already uses exactly this layout (`engine_windows.go`,
`engine_darwin.go`, `engine_linux.go`, `engine_other.go`, plus
`com_windows.go`). The `Snapshot` interface already lives in `backup`, so
nothing moves and no import direction becomes strange.

Wiring explicitly at the composition root keeps `defaultOpenSnapshot`'s
refuse-on-nil safety for every test and for any build that forgets to wire it,
and puts the production decision where `OpenStore` and `QueryChunks` already are.

**Rejected — a separate `internal/snapshot` package:** cleaner in the abstract,
but `Snapshot` is defined in `backup`, so the interface would have to move or
the new package would import the capability. A new package for one consumer,
against the tree's own precedent.

**Rejected — make `defaultOpenSnapshot` platform-aware in place:** smallest
diff, but it silently changes nil from "refuse" to "take a real snapshot", so
every existing test and every unwired build would start snapshotting. That
default refuses for a reason.

---

## 2. Architecture

Four new files in `agent/internal/capabilities/backup/`:

| File | Build tag | Provider |
|---|---|---|
| `snapshot_windows.go` | `windows` | VSS via in-process COM |
| `snapshot_darwin.go` | `darwin` | APFS via `tmutil` + `mount_apfs` |
| `snapshot_linux.go` | `linux` | LVM via `lvcreate -s` |
| `snapshot_other.go` | `!windows && !darwin && !linux` | refuses, naming the platform |

Each supplies one function matching the existing signature
`func(ctx context.Context, kind string, roots []string) (Snapshot, error)`, so
`Snapshot` and `Options.OpenSnapshot` are unchanged.

**No new dependency.** VSS uses `golang.org/x/sys/windows`, already a direct
requirement at `agent/go.mod:7`. The shell-out providers use the existing
`runner` seam pattern from `patch/exec.go`.

**Every provider performs the same four steps, in order:**

1. **Check the kind matches the platform** (§1.2). Mismatch refuses.
2. **Sweep orphans** (§1.4) — release any pre-existing agent-tagged snapshot.
3. **Take the snapshot** and build the path translation.
4. **Verify it presents the data** (§1.5). Divergence refuses.

**Multiple roots may span multiple volumes**, so a provider may take several
snapshots and return one `Snapshot` holding a volume-to-prefix map. `Translate`
resolves through the longest matching prefix. `Release()` tears down all of
them, and **continues past a failure**, reporting the first — an early return
leaks the remainder.

---

## 3. Per-platform mechanics

**This section is derived from platform documentation, not from observation.**
Nothing in §3.1 or §3.2 has executed. The files' headers say so, and this spec
must not be read as though these sequences were verified.

### 3.1 Windows — VSS

**The sequence is `docs/modules/backup/agent-engine.md` §4's, verbatim — that
document is authoritative and this section defers to it:**

`CoInitializeEx` → `CreateVssBackupComponents` → `InitializeForBackup` →
`SetContext(VSS_CTX_BACKUP)` → `GatherWriterMetadata` → `StartSnapshotSet` →
`AddToSnapshotSet` per volume → `SetBackupState(false, false, VSS_BT_FULL)` →
`PrepareForBackup` → `DoSnapshotSet` → `GetSnapshotProperties` for the device
path, yielding `\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopyN\...`.

**On the way out: `BackupComplete`, then `DeleteSnapshots` for this snapshot
set's id only.** Release is explicit, not implicit. An earlier draft of this
spec asserted that `VSS_CTX_BACKUP` copies are non-persistent and therefore
self-releasing, and used that to argue Windows largely self-heals the orphan
problem. That claim is withdrawn: it was documentation-derived, unverified, and
it contradicted `agent-engine.md`'s explicit teardown. §1.4's sweep applies on
Windows as elsewhere, scoped to snapshot sets this agent created.

In-process vtable dispatch through `x/sys/windows`, following `com_windows.go`
— **not** `diskshadow.exe`. That file already records why: a SYSTEM process
spawning a shell is among the loudest behavioural signals an endpoint can emit,
and is what EDR behavioural chains are built to catch.

**One hazard to expect during implementation.**
`CreateVssBackupComponents` is a C++ export and is **name-mangled** in
`vssapi.dll`, so resolving it is not the plain `NewProc(...)` the existing COM
work uses.

Requires SYSTEM, which the agent has as a Windows service.

### 3.1a This section overrides a recorded decision

`agent-engine.md` §4 states that writing an `IVssBackupComponents` binding from
documentation alone is *"not a risk worth taking"*, because it is hundreds of
lines of vtable calls first executed on a customer's machine, where **an index
off by one is a crash in a SYSTEM service**.

That judgement is overridden here, deliberately and on the record, on **two**
grounds — it was three when this section was written, and ground 2 has since
been withdrawn as false; it is struck rather than deleted so the weakening of
the case stays visible:

1. **The project already took this trade once.** `patch/com_windows.go` ships
   exactly such a binding — COM vtable dispatch, in the same SYSTEM service,
   never executed, under an honest header.
2. ~~**The stated risk is mitigated, not dismissed.** Compile-time verification is
   real for this failure mode: a wrong vtable index or argument type fails
   `GOOS=windows go vet` and `go test -c`.~~

   **WITHDRAWN — this ground was false.** Task 5's implementer disproved it
   while writing the binding, and the controller verified it: the vtable is a
   struct of *named* `uintptr` fields, dispatched as
   `syscall.SyscallN(b.vtbl.InitializeForBackup, …)`. A struct whose fields are
   declared in the **wrong order** relative to the real COM interface compiles
   cleanly and calls the wrong function pointer at runtime. Compile-time
   verification catches essentially nothing about vtable ordering — which is
   precisely the hazard `agent-engine.md` §4 named ("an index off by one is a
   crash in a SYSTEM service").

   The override therefore rests on grounds 1 and 3 only. Ground 1 (the project
   already ships such a binding in `patch/com_windows.go`) and ground 3 (the
   alternative leaves the Windows fleet refusing indefinitely) both stand
   unchanged, but they are a weaker case than three grounds, and the residual
   risk is larger than this section originally claimed.
3. **The alternative leaves `backup.run` refusing on the whole Windows fleet
   indefinitely**, since no Windows endpoint has ever run this agent and none is
   scheduled.

`agent-engine.md` §4 must be amended in the same cycle to record that this
decision changed and why — per `CLAUDE.md`, a decision in `docs/` is changed
deliberately, not bypassed. Leaving §4 stating the opposite would leave the next
reader with two contradictory authorities.

### 3.2 macOS — APFS

`tmutil localsnapshot`, then `mount_apfs -s <name> /System/Volumes/Data
<mountpoint>` read-only. Requires root **and** a Full Disk Access (TCC) grant,
which on a managed fleet must be pushed by MDM.

**The orphan rule differs here and must.** `tmutil` names snapshots in Apple's
own format, so the agent cannot tag them, and a local snapshot may be one Time
Machine itself relies on. The sweep therefore reclaims **the agent's own
mountpoints**, under an agent-owned directory, and does **not** delete snapshots
it did not create. Deleting a user's Time Machine snapshot to tidy up would be a
worse bug than the leak.

Without the TCC grant the mount succeeds and the tree reads empty — the case
§1.5 exists to catch.

### 3.3 Linux — LVM

Resolve each root's backing device (`findmnt -n -o SOURCE --target`), confirm it
is a logical volume, `lvcreate -s -n <agent-tagged-name> -l 10%ORIGIN`, mount
read-only. Not an LV → refuse (§1.3).

The name tag is ours here, so the sweep is `lvs` filtered by prefix then
`lvremove -f`.

**COW exhaustion is the real operational risk.** A snapshot whose
copy-on-write space fills is silently invalidated and reads from it fail
thereafter. `10%ORIGIN` is a reasoned starting point, **not a measured one**.

---

## 4. Testing

**Runs everywhere, every time.** Provider logic through an injected `runner`
(the seam at `patch/exec.go:51-62`): device resolution, LV detection,
kind-mismatch refusal, non-LVM refusal, orphan-sweep filtering, translation
across multiple volumes, and `Release()` continuing past a partial failure.

**Runs where LVM exists.** One integration test that builds a loopback volume
group (`losetup`, `pvcreate`, `vgcreate`), takes a real `lvcreate -s` snapshot,
verifies translation and entry counts against it, and removes it. It must
`t.Skip` **loudly**, naming the missing tool, when `lvcreate` is absent — as it
is in the current development container. A skip that reads as a pass is how the
previous two cycles' worst defects survived.

**Falsifiers — each must be shown failing before its guard is trusted:**

| Guard | Falsified by | Expected |
|---|---|---|
| Entry-count check (§1.5) | remove it | the empty-snapshot simulation passes |
| Kind check (§1.2) | remove it | a `vss` job opens an LVM snapshot |
| Sweep filter (§1.4) | widen the prefix | it removes an LV the agent does not own |
| `Release()` continuation | early-return on first error | the second volume leaks |

**Compile-only.** `snapshot_windows.go` and `snapshot_darwin.go` get
`GOOS`-targeted `go vet` and `go test -c`. That is a real check — a wrong vtable
index or a bad argv type fails to compile — and it is the whole of their
verification.

---

## 5. What this cycle will not prove

Stated so the next reader inherits the limits rather than assuming coverage.

- **No VSS call has ever executed.** The COM sequence, the mangled export, and
  the `VSS_CTX_BACKUP` auto-release behaviour are all from documentation.
- **No APFS snapshot has ever been taken.** The TCC failure mode §1.5 targets is
  inferred, not observed.
- **`10%ORIGIN` is reasoned, not measured.** A fill invalidates the snapshot
  silently.
- **`backup.run` end to end is still unproven on any platform.**
  `scripts/verify_backup_run.py` can drive it on Linux once LVM is present; that
  run has not happened.
- **CI will not run any of this** until the ten pre-existing `staticcheck`
  findings are cleared. The `agent` job orders `staticcheck` before `test`, so
  Actions stops before the test step (`HANDOFF.md` §6). This cycle ships three
  providers, two of which have never run, into a repository whose agent test
  gate is currently dead.
- **Linux coverage is narrower than "three platforms" suggests** (§1.3): plain
  ext4-on-a-partition still refuses.
