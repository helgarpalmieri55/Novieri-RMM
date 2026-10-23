# Health posture probes — design

**Date:** 2026-08-12
**Module:** `endpoint-health-alerting` (`docs/modules/endpoint-health-alerting/spec.md`)
**Scope:** `agent/internal/capabilities/health/` only. No server change, no wire
change, no schema change.

---

## 1. What this adds, and what it does not

`posture.go` currently answers `unavailable` for every posture check on every
platform. That is a deliberate, argued refusal — not an omission — and this
design replaces it with real probes for the four posture-cadence check types
already in the closed registry of module spec §4.1:

`posture.disk_encryption`, `posture.firewall_enabled`,
`posture.antivirus_healthy`, `disk.smart_ok`.

**Nothing else changes.** All four check types already exist in the agent's
registry (`checks.go`) and in the server's. No new `check_type` is introduced,
so §0.1's "any registry change is a schema change" is not triggered. No wire
kind, no field, no migration. This is an agent-only cycle whose entire
observable effect is that four checks stop reporting `unavailable` on platforms
where they can now be answered.

**Not in scope:** fast-tick sampling on Windows and macOS (`sampler_other.go`
still answers `unavailable` for `cpu.busy_pct` and friends). That is a separate
footprint argument and a separate cycle.

---

## 2. Decisions

Each was taken deliberately; the rejected alternative is recorded because a
later reader will otherwise re-litigate it.

### 2.1 Windows probes are in-process COM, never a shell-out

**Rejected:** `manage-bde`, `netsh advfirewall`, PowerShell — which module spec
§0.5 falsifier 3 itself calls "the WMI / powershell / sc / systemctl
shell-outs", so the spec anticipated them.

**Why rejected anyway:** a SYSTEM-privileged process spawning a shell is among
the loudest behavioural signals an endpoint can emit, and posture probes fire on
a schedule, on every endpoint in the fleet, forever.

*Corrected 2026-08-13.* This cited `docs/modules/backup/agent-engine.md` §2 as
"a promise to an EDR team that **this agent** spawns no processes on Windows, on
any path". It is not that. §2 is titled "Processes **this capability** may
spawn" and is scoped to backup; `deploy` runs scripts on Windows by design, so
no such fleet-wide promise exists to retract. What this decision honours is §2's
REASONING — and the fact that, unlike `deploy`, a posture probe has an
in-process alternative that costs only bindings.

**Cost, stated:** four COM bindings that have never executed. This is the same
bargain the VSS provider struck, and that provider's review found a false claim
inside it — so each binding here carries a `com_windows.go`-style header listing
what could not be confirmed.

### 2.2 Linux firewall: front-end state first, kernel ruleset as fallback

Ask `ufw`, then `firewall-cmd`, for their own enabled state. If neither is
installed, ask `nft`/`iptables` whether inbound is actually filtered.

**Rejected — front-end only:** reports `unavailable` on every server filtering
with raw nftables, which is a large and correctly-configured class of machine.

**Rejected — kernel ruleset only:** a `ufw`-enabled box expresses its policy
through the wrapper, and reading the raw table can read `false` on a machine
that is correctly firewalled. A false alarm on a healthy machine is the
expensive error.

The fallback never calls an empty `ACCEPT`-policy table "enabled". "iptables is
installed" is not a firewall.

### 2.3 `posture.disk_encryption` answers for the system/boot volume only

The check takes no selector (§4.1), so it must answer one question about the
machine. It answers for the volume the OS boots from: root's backing device on
Linux, `C:` on Windows, the data volume on macOS.

**Known limit, stated rather than discovered later:** a machine with an
encrypted root and an unencrypted `/data` or `D:` reads as healthy. Per-volume
coverage needs its own check type with a selector, which §0.1 makes a schema
change. It is not smuggled into this one.

**Rejected — every fixed local volume:** stricter and better for audit, but it
redefines a shipped check type's meaning without a registry change, and it
reports `false` on machines many operators consider fine.

### 2.4 Probes are bounded on three axes

A per-command context deadline, a **whole-pass** budget, and a non-overlap
guard. See §6.

*Amended 2026-08-13.* This originally read "two axes" and named only the first
and third. Neither bounds a PASS, and a pass is what holds the agent's wheel —
see §6.

### 2.5 All four Windows COM bindings land in this cycle

Including `root/WMI` for `disk.smart_ok`, a fourth namespace beyond the three
posture ones.

**Flagged and overridden deliberately:** `SecurityCenter2`'s `productState` is a
bitfield Microsoft has never formally documented, and §4.1 warns in as many
words against a check that is "confidently wrong". The mitigation is in §5: only
well-attested bit patterns are read, and anything else is `unavailable` — never
a confident `true` or `false`.

---

## 3. File layout and the seam

```
posture.go                 (untagged)  shared core: runner seam, bounded exec,
                                       deadline, non-overlap guard, dispatch
posture_linux_probes.go     (untagged)  LUKS / firewall / SMART logic
posture_darwin_probes.go    (untagged)  FileVault / socketfilterfw logic
posture_linux.go           //go:build linux
posture_darwin.go          //go:build darwin
posture_windows.go         //go:build windows   four COM bindings + dispatch
posture_other.go           //go:build !linux && !darwin && !windows
```

**The names matter, and the first draft of this design got them wrong.** These
files are `posture_linux_probes.go` and `posture_darwin_probes.go`, NOT
`posture_probe_linux.go` / `posture_probe_darwin.go`. Go applies an IMPLICIT
build constraint to any file whose base name ends in `_GOOS`, tag or no tag —
measured on this project:

```
GOOS=linux   go list → posture.go posture_linux.go posture_probe_linux.go
GOOS=windows go list → posture.go posture_windows.go        (probe file EXCLUDED)
```

An OS-suffixed name would therefore have made the macOS probe logic invisible to
Linux CI, destroying the entire reason for keeping it untagged, while looking
correct in review. The snapshot providers escaped this only by naming files after
MECHANISMS — `snapshot_lvm.go`, `snapshot_apfs.go`. A base name ending in
`_probes` carries no constraint.

The probe files carry **no build tag** on purpose: they shell out only
through an injected runner, so every decision they make — front-end detection,
ruleset parsing, root-device resolution, and every `unavailable` branch — is
exercised by a real `go test` on this project's Linux CI, including for macOS.
Only the dispatch that selects a sampler is tagged. This is the shape the
snapshot providers established (`snapshot_lvm.go`, `snapshot_apfs.go`), and the
reason their logic is tested on a machine with neither LVM nor APFS.

Windows cannot use it: `x/sys/windows` builds nowhere else, and there is no seam
a fake could stand behind. Both its logic and its dispatch are in one tagged
file, and the compile checks are its entire verification.

`NewPostureSampler()` keeps its signature and returns the platform sampler, so
`main.go` and §0.5 falsifier 3 are untouched.

**The runner seam is duplicated into `health`, not imported from `patch`**, per
the convention stated at `internal/capabilities/netsweep/netsweep_test.go:1240`.
It carries the two disciplines `patch/exec.go` and `inventory/exec.go`
independently record: `cmd.WaitDelay` — cancellation kills only the direct
child, and a grandchild holding the stdout pipe made a 200ms deadline take
30.003s on this project, measured — and a bounded output buffer, so a tool that
prints forever costs a fixed cap rather than the agent's residency claim.

---

## 4. What each probe reads

| check | Linux | macOS | Windows (COM) |
|---|---|---|---|
| `disk_encryption` | `findmnt -n -o SOURCE --target /` → device; `lsblk -nso NAME,TYPE <dev>` → `TYPE=crypt` anywhere in the ancestry | `fdesetup status` → `FileVault is On.` | `Win32_EncryptableVolume` in `root/CIMV2/Security/MicrosoftVolumeEncryption`, `GetProtectionStatus()==1` for the system drive |
| `firewall_enabled` | `ufw status` → `Status: active`; else `firewall-cmd --state` → `running`; else `nft list ruleset` → an `input` hook chain whose policy is `drop`, or `iptables -S INPUT` → `-P INPUT DROP`/`REJECT` | `/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate` → state 1 or 2 | `INetFwPolicy2.FirewallEnabled`, **all three profiles** must be enabled |
| `antivirus_healthy` | `unavailable` (§4.1: Windows only) | `unavailable` | `root/SecurityCenter2` → `AntiVirusProduct.productState` |
| `disk.smart_ok` | `smartctl -H <dev>`; selector = device path, `*` = all fixed devices | `unavailable` (`—` in §4.1) | `MSStorageDriver_FailurePredictStatus` in `root/WMI` |

`lsblk -s` walks *parents*, which is what makes the crypt test correct for a
root on LVM-on-LUKS as well as a plain LUKS partition.

**`disk.smart_ok` with selector `*` aggregates as follows**, because "all
devices" needs a stated rule and the obvious one is wrong: `false` if **any**
readable device reports failing; `true` only if **at least one** device was read
and none reported failing; `unavailable` if **no** device could be read at all.
The tempting "true if no failures seen" would report a healthy fleet on a
machine where `smartctl` cannot read a single disk — absence of evidence
becoming `true`, which §5 forbids.

**The firewall fallback is a two-step, not a scan.** It runs only when neither
front-end is installed, and it asks one question: is inbound filtered by
default? An `ACCEPT` policy with rules present is **not** "enabled" — those
rules may all be permissive, and "iptables is installed" is not a firewall.

---

## 5. The `unavailable` / `false` / `true` rules

Asymmetric in **both** directions, and that is the point:

- **`true` requires a positive parsed statement that protection is ON.** Never
  inferred from absence of evidence.
- **`false` requires a positive parsed statement that protection is OFF.**
- **Everything else is `unavailable`:** tool absent (`LookPath` fails), non-zero
  exit, output that does not match a known shape, permission denied, deadline
  exceeded, overlap-guard rejection, or a `productState` bit pattern outside the
  well-attested set.

Why the extra branches are worth it, per §0.7: `unavailable` is stored,
surfaced and countable, and it never opens or clears an alert. So:

| wrong answer | consequence |
| --- | --- |
| wrong `false` | a fleet-wide false alarm, and alert fatigue is how real alerts get ignored |
| wrong `true` | a silently unencrypted laptop — and because `true` also CLEARS, it can close a real alert |
| `unavailable` | "37 devices cannot answer this check" — visible, countable, actionable |

Only the third fails without lying.

---

## 6. Bounding

**Per command:** a 5-second context deadline and `cmd.WaitDelay = 2s`. The
posture cadence is hourly by default; a probe that cannot answer in five seconds
reports `unavailable` rather than keep the pass waiting on it.

**Per pass (`posturePassBudget`, 20s):** one context budget shared by every
command in a single posture pass. When it expires, the checks remaining in that
pass report `unavailable` and run no command — never a definite answer.

*Amended 2026-08-13 — this is order-dependent, and it is permanent, not just
a slow tick.* `pol.checks` is server-supplied and `Engine.onTick` iterates it
by index, in the same order, every tick. A check that wedges consumes the pass
budget itself, and every check AFTER it **in that policy order** reports
`unavailable` having run no command — not only on the pass where it wedged,
but on every subsequent pass, because nothing rotates the order. One wedged
check therefore permanently starves the checks below it in policy until it is
fixed or dropped. This is inherent to a whole-pass budget bounding the wheel,
not a bug in it: the alternative is the unbounded stall the budget replaced.

*Amended 2026-08-13, and the reason is a real regression this design shipped.*
A per-command deadline does not bound a pass; it MULTIPLIES. `Engine.onTick`
holds `e.mu` across its whole sample loop and `internal/scheduler` invokes wheel
callbacks synchronously, so the worst case must be stated as what it is:

> **A posture pass CAN hold the agent's scheduler wheel** — every scheduled
> entry on the device, not just health — for up to the pass budget PLUS one
> `WaitDelay` (22s, not 20s), once per posture interval (≥ 900s).
> `health.check.now` queues behind `e.mu` for the same span. That is bounded,
> not eliminated.

*Amended 2026-08-13 — the stated worst case was short by one `WaitDelay`.* A
command started just before the pass deadline has its context expire exactly
at the deadline, and `cmd.WaitDelay = 2s` (`posture.go`) then still extends
`cmd.Wait` by up to that long while the killed process's pipes close. The real
bound is 22s, not 20s. It is one tail, not one per command: Go's
`exec.Cmd.Start` returns `ctx.Err()` immediately for an already-done context,
so every command later in a ladder after the deadline has passed costs
nothing more. No falsifier here catches this — falsifier E's fake runner
honours `ctx.Done()` instantly and never exercises `WaitDelay` — so it is
recorded here as read from the code rather than measured by a test.

Before the pass budget existed the bound was the sum of every command in the
pass: on Linux, `disk_encryption` (2 commands) + the firewall ladder (up to 4) +
`disk.smart_ok` on `*` (`lsblk` plus one `smartctl` per disk), each at 5s + 2s
`WaitDelay` — about a minute on a machine with two wedged disks.

The pass boundary is derived from the sampler's own state (the gap since the
previous `Sample` call returned, `posturePassGap`) rather than from a context
parameter, because `Sampler.Sample(checkType, selector string)` takes no context
and three call sites plus §0.5 falsifier 3 are written against that signature.
Intra-pass gaps are microseconds and inter-pass gaps are ≥ 900s, so the two are
six orders of magnitude apart; `posture.go` states both failure directions.

**Non-overlap:** the sampler holds a `TryLock`. A caller that finds a pass in
flight is answered `unavailable` for the checks it overlaps, rather than
stacking shell-outs.

*Amended 2026-08-13 — three things here were wrong.*

- **The reason was wrong.** This said `health.check.now` and the posture
  sub-tick "can overlap, and the dispatcher's per-capability serialisation does
  not prevent it". They cannot overlap: `Engine.onTick` and `Engine.CheckNow`
  both hold `e.mu` across their whole sample loop, so the guard never fires by
  that route. What serialises those two is the engine mutex — which is also
  what makes a slow pass hold the wheel, hence the pass budget above. The guard
  is kept for any OTHER caller of a shared sampler (a second engine over one
  `NewPostureSampler()`, a non-engine sampling path, a test), because it is one
  atomic operation uncontended and the failure it prevents is expensive and
  silent.
- **"for every posture check" was wrong.** The `TryLock` is per `Sample` call,
  i.e. per CHECK. A losing caller can be refused check 1 and win check 2, and
  report a mixture. Documented rather than fixed: every value in such a mixture
  was measured by the caller that got it, and a pass identity is exactly what
  `Sampler.Sample` cannot carry.
- **"and one log line" was wrong.** `postureSampler` has no logger and emits
  nothing. Removed rather than implemented: a log line per refused check, on a
  path that fires only under a contention this design does not create, is noise
  with a footprint.

Returning `unavailable` rather than waiting is the visible refusal this project
prefers, and it cannot raise a false alarm because `unavailable` never opens an
alert.

**No caching between passes** (§4.3: "Between ticks: nothing"). Every pass
re-probes. The overlap guard is not a cache — it returns `unavailable`, not a
stale value.

---

## 7. Verification

### Measured here, on Linux

- Table-driven seam tests over every branch of both `_probe_` files: tool
  absent, each firewall front-end, `DROP` vs `ACCEPT` policy, crypt present vs
  absent in the ancestry, unparseable output, non-zero exit, deadline exceeded,
  permission denied. macOS logic included — it is untagged.
- A real-tool integration test using what this container actually has
  (`findmnt`, `lsblk`, `nft`, `iptables`), **skipping loudly per missing tool**
  the way the loopback-LVM test does, asserting the probe's answer matches an
  independently derived expectation. `cryptsetup`, `ufw`, `firewall-cmd` and
  `smartctl` are absent here and those paths stay seam-only.

### Five new budget falsifiers

These probes spawn processes; the existing six falsifiers do not cover that.
Falsifier 3 drives a *stand-in* sampler — it measures the engine's cadence, not
these probes — which is exactly why these are owed. Per CLAUDE.md: measured, or
described as a design target.

1. **Retention:** a posture pass retains nothing across 100 passes, and
   allocates a bounded amount per pass.
2. **Goroutines:** zero growth across passes — measured over real spawns
   through `execPostureRunner`, since `exec.Cmd`'s `io.Copy` writer is a pass's
   only goroutine source and a fake runner never reaches it. *(Corrected
   2026-08-13: driven through the fake sampler, this asserted a property true
   by construction.)*
3. **A hung command cannot hold the pass open** — the `WaitDelay` discipline
   asserted rather than assumed, by elapsed time. *(Corrected 2026-08-13: this
   was written as "no surviving child process", which is both unfalsifiable
   here and untrue — a grandchild reparents to init and DOES outlive the pass.
   What the agent gets is that the pass stops waiting for it.)*
4. **The overlap guard executes ZERO commands** on the second concurrent pass,
   counted through the seam.
5. **A wedged pass is bounded by the pass budget** and answers `unavailable`,
   never a verdict, for the checks it skipped. *(Added 2026-08-13 — this is the
   §9 row "a probe cannot hang the tick", which shipped with no test at all.)*

Falsifiers A and B are ordinary Tests that call `testing.Benchmark` through
`internal/budget` (`Retained`, `Measure`); neither is a `go test -bench`
benchmark, for the reason `internal/budget` records: `-bench` exits 0 on a
regression. C, D and E are plain tests that measure elapsed time and command
count directly, with no `testing.Benchmark` involved.

### Ships unverified, and says so in its own header

- **All four Windows COM bindings.** Never executed. Header lists what could not
  be confirmed, as `snapshot_windows.go` does.
- **macOS command formats.** `fdesetup status` and `socketfilterfw
  --getglobalstate` output shapes are reasoned from documentation, never
  observed. The *logic* around them is tested through the seam; the strings are
  not.

Compile-only checks for both, stated exactly, because this originally claimed
CI coverage that did not exist:

- `GOOS=windows go vet ./...` — in CI since before this cycle. It type-checks
  the windows-tagged files including `_test.go`.
- `GOOS=darwin go vet ./...` — **added to `ci.yml` on 2026-08-13, by this
  review.** Before that there was no `GOOS=darwin` step of any kind; darwin got
  only `go build ./cmd/agent`, which does not compile test files, so a
  darwin-tagged test regression was invisible. That is not hypothetical: it is
  how `budget_test.go`'s `readInto` break survived.
- `GOOS=windows go test -c` — CI runs this for `./internal/service` only, not
  for `./internal/capabilities/health/`. The health one is a local command.
  `go vet` is what covers these files on every push.

---

## 8. Compatibility

No wire kind, no field, no schema, no migration, no server change. An older
server pushing a policy without these check types is unaffected; a newer server
pushing them to an older agent gets `unavailable`, which is what §0.7 already
specifies for an unknown or unanswerable check. The change is invisible to the
control plane except that four checks begin returning real values.

---

## 9. Falsifiers for this design's own claims

| claim | falsifying observation |
| --- | --- |
| Probes never run on the fast tick | existing falsifier 3 — the posture stand-in's call counter above zero across 11 fast ticks |
| A pass retains nothing | retained heap growing across 100 passes |
| A pass cannot hold the wheel for longer than the pass budget | a pass of wedged checks taking longer than `posturePassBudget`, or answering `true`/`false` for a check whose command never completed — `TestPostureFalsifierE_AWedgedPassIsBoundedByThePassBudget` |
| Overlap cannot stack shell-outs | any command executed by the second concurrent pass |
| Absence of evidence is never `true` | a probe returning `Available:true, Bool:true` on a tool-absent or unparseable path |
| Windows spawns no process | any `exec.Command` reachable from `posture_windows.go` |
