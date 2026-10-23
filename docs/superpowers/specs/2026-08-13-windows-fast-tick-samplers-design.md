# Windows fast-tick samplers — design

**Date:** 2026-08-13
**Module:** `endpoint-health-alerting` (`docs/modules/endpoint-health-alerting/spec.md` §4.1, the `windows` column)
**Closes:** the gap that makes the proactive health loop a Linux-only feature.

---

## 1. The problem, stated exactly

`agent/internal/capabilities/health`'s fast-tick sampler answers five checks on
Linux — `cpu.busy_pct`, `memory.used_pct`, `disk.free_pct`, `disk.free_bytes`,
`process.present` — and answers **none of them anywhere else**.
`sampler_other.go` is a `!linux` stub returning `unavailable()` for everything.

So the threshold-based edge health loop, which is this module's whole reason to
exist, only fires on Linux endpoints. On a fleet that is mostly Windows, the
feature is mostly absent. `unavailable` is an honest state and it never opens or
clears an alert — which is exactly why nothing is currently wrong, and exactly
why nothing is currently useful.

The stub's own comment explains the deferral, and one half of it is **wrong on
the facts**: it says `cpu.busy_pct` and `memory.used_pct` on Windows "are
performance counters or WMI". They are neither. They are two plain `kernel32`
calls, reachable from a cgo-free static binary with the same
`NewLazySystemDLL` mechanism `posture_windows.go` already uses.

---

## 2. Decisions

### 2.1 Windows this cycle; macOS gets its own

Windows is the bulk of the target fleet and its primitives are ordinary
functions. macOS is the awkward one: CPU load and memory pressure conventionally
come from Mach calls (`host_statistics64`) that usually mean cgo, which
CLAUDE.md's agent conventions forbid. `sysctl` is reachable without cgo through
`x/sys/unix`, but it does not expose what these two checks need.

That is a real research problem and it is not this cycle's. macOS stays
`unavailable`, honestly.

### 2.2 All five checks, not a subset

Partial coverage means a Windows device half-answers in the console, which is
worse to operate than a clear "this platform cannot answer" — you cannot tell a
deliberate gap from a broken probe. All five are reachable with `kernel32`
alone, and the three additional calls (`GetDiskFreeSpaceExW`,
`GetLogicalDriveStringsW`/`GetDriveTypeW`, `CreateToolhelp32Snapshot`) are
simpler than the CPU one.

### 2.3 Untagged arithmetic, tagged dispatch — not a shared reader interface

**Rejected: a `hostReader` interface both platforms implement**, with one
untagged sampler owning selector handling and the `unavailable` rules. It is the
better end state and it is the wrong move now, for two reasons:

- It restructures a Linux sampler that is currently **measured**. Falsifier 2c
  pins its retained heap flat across a hundredfold tick spread, and that
  measurement is part of what keeps the idle budget from decaying back into a
  design target. Refactoring the proven path to accommodate the unproven one
  inverts the risk.
- **Two platforms is the wrong number to abstract from.** macOS has genuinely
  different primitives, so it is the platform most likely to show that an
  interface shaped around Linux files and Windows functions is the wrong shape.

**Rejected: a standalone Windows sampler with no shared layer.** It would
duplicate the selector and `unavailable` rules across platforms — two copies of
one decision that must stay in step, which is this project's most frequently
paid defect.

So: pure arithmetic in an untagged file that Linux CI tests, platform reads in a
tagged file that only compiles.

### 2.4 The verification standard, agreed up front

This environment can compile-check Windows and never execute it. Therefore:

1. **Measure what is measurable** — the arithmetic, on Linux CI, including a
   zero-allocation assertion.
2. **Also write the never-run falsifier**, `//go:build windows`, so it is ready
   the day someone runs the binary on real hardware.
3. **Label it as never-run everywhere it is mentioned.** CLAUDE.md's complaint
   about the original six falsifiers was not that they were unrun; it was that
   they were unrun *while being cited as verification*. This cycle must not earn
   the same complaint.

---

## 3. File layout

| File | Build tag | Responsibility |
| --- | --- | --- |
| `sampler_calc.go` | **none** | pure arithmetic: counter deltas → percent, bytes → percent. No syscalls, no platform types. Tested on Linux. |
| `sampler_windows.go` | `windows` | four `kernel32` reads, the previous-CPU-counter state, and a five-case dispatch matching `sampler_linux.go`'s shape. |
| `sampler_other.go` | `!linux && !windows` | narrowed from `!linux`. Its doc comment is rewritten: the claim it currently makes about Windows becomes false, and was already wrong about WMI. |
| `sampler_linux.go` | `linux` | **untouched.** Its measured falsifier stays measured. |

**The name `sampler_calc.go` is load-bearing.** Go applies an implicit build
constraint to any file whose base name ends in a GOOS. Cycle 3 lost time to
exactly this with `posture_probe_linux.go`, and `posture_darwin_probes.go`
carries a comment recording the rule. `_calc` is not a GOOS, so the file builds
and tests everywhere.

---

## 4. The five checks

### `cpu.busy_pct` — `GetSystemTimes`

Returns idle, kernel and user as `FILETIME`s. **On Windows, kernel time already
includes idle time.** So:

```
busy  = (kernel + user) - idle
total =  kernel + user
```

Getting this wrong — computing `kernel + user` over some separate total —
produces plausible, inflated CPU on every Windows endpoint forever. A test pins
the correct form so that "simplifying" it goes red.

Deltas against the previous tick, held in the sampler exactly as the Linux one
holds its `/proc/stat` counters. The first sample after start returns
`unavailable`: a rate needs two readings, and inventing one is how a device
reports 100% CPU one second after the agent starts.

### `memory.used_pct` — `GlobalMemoryStatusEx`

`used_pct = (TotalPhys − AvailPhys) / TotalPhys × 100`.

`MEMORYSTATUSEX.dwLength` **must be set to the struct size before the call** or
it fails. This is the class of mistake that compiles cleanly and fails at
runtime, on a platform nothing here can run. It is therefore set in exactly ONE
helper that constructs the struct, so there is a single place to get it right,
and a Windows-tagged test exercises that helper. **That test compiles and never
runs** — it is insurance for the day someone runs it on hardware, not evidence
today.

### `disk.free_pct` / `disk.free_bytes` — `GetDiskFreeSpaceExW`

Use `FreeBytesAvailableToCaller`, mirroring Linux's `Bavail`.

Selector contract, matching Linux:

- **wildcard → the worst fixed drive**, where "worst" is the *lowest value of the
  metric being asked for*: the lowest free percentage for `disk.free_pct`, the
  lowest free byte count for `disk.free_bytes`. These can name different drives
  on the same machine — a small nearly-full volume versus a large one with more
  slack — and that is correct, because each check is answering its own question;
- **empty → the system drive**, resolved from `GetWindowsDirectory`'s volume
  rather than the `%SystemDrive%` environment variable, which a user process can
  set to anything. Linux's equivalent default is `/`;
- otherwise → the path given.

The wildcard case enumerates with `GetLogicalDriveStringsW` and filters on
`GetDriveTypeW == DRIVE_FIXED`, so a mounted ISO or a USB stick cannot open a
disk-free alert.

A path that does not resolve is `unavailable`, **never zero** — the rule
`statfsSample` already states on Linux, and for the same reason: reporting 0
free bytes would open a critical alert for a volume that was merely unmounted or
ejected.

**One honest asymmetry, stated rather than engineered around:** Linux's total is
the whole filesystem (`Blocks × Bsize`) while Windows'
`TotalNumberOfBytes` is quota-aware. On a machine with disk quotas the two
platforms answer subtly different questions. Recorded in a comment.

### `process.present` — `CreateToolhelp32Snapshot`

`Process32FirstW` / `Process32NextW`, with the snapshot handle closed on **every**
path including the error paths.

**A SECURITY PROPERTY THAT MUST CARRY ACROSS, and which a Windows implementer
could easily breach by reaching for the obvious API.** Linux matches against
`/proc/<pid>/comm` — the kernel's own process name — and never against a command
line. Its comment records why, citing §5.1: a command line never travels on this
wire, and *"a check that matched one would be a check whose selector could be
used to ask 'is anyone running this argument'"*. That turns a health check into
a fleet-wide interrogation primitive.

`PROCESSENTRY32.szExeFile` is the executable name, so the Toolhelp route
satisfies this by construction. **WMI's `Win32_Process.CommandLine` does not**,
and is the natural thing to reach for if the Toolhelp path proves awkward. It is
out of bounds here regardless of convenience.

Two deliberate divergences from Linux:

- **Matching is case-insensitive and `.exe`-tolerant.** Windows process names
  carry an extension and are case-insensitive; Linux `comm` has neither
  property. A policy written `sshd` or `nginx.exe` should behave sanely on both.
  Exact matching would make a cross-platform policy impossible to write.
- **Linux `comm` is truncated to 15 characters by the kernel**, so a long
  process name already cannot be matched exactly there. Windows has no such
  limit. This cycle does not fix the Linux quirk; it declines to replicate it.

An empty selector is `unavailable` on both platforms. A process that is genuinely
absent is `Available: true, Bool: false` — a real answer, not a missing one.

---

## 5. Verification

### Measured, on Linux CI

`sampler_calc.go`, table-driven: no-previous-sample, counter wraparound,
`total == 0`, the 0% and 100% boundaries, and a case pinning
`busy = (kernel + user) − idle`. Plus a zero-allocation assertion on the calc
layer through `internal/budget` — the part of the idle-budget claim this
environment can actually establish.

### Compile-checked only

`sampler_windows.go` and the Windows-tagged tests, via the existing
`GOOS=windows go vet ./...` step, which type-checks `_test.go` files as well as
the package (the darwin step's own comment records this). Add
`GOOS=windows go test -c -o /dev/null ./internal/capabilities/health` beside the
existing `./internal/service` line: `go test -c` takes a single package, so it
must be named, and this cycle adds enough Windows-only code to earn it.

### Never executed

Every `kernel32` call, every struct layout, every number. The Windows-tagged
falsifier mirroring 2c compiles and waits.

### One test that changes meaning without changing a line

`TestFalsifier2_TheRealHostSamplerAccumulatesNothingEither` is untagged and
calls `NewHostSampler()`. On Windows today it exercises the **stub** and passes
trivially, so its own framing ("the real host sampler is NOT allocation-free")
is already false there. Giving Windows a real sampler makes it meaningful — and
it still cannot run. Its comment must say so, or it becomes a test that looks
like Windows coverage and is not.

### Falsifiers for this design's own claims

| claim | falsifying observation |
| --- | --- |
| the CPU arithmetic is right | a Windows endpoint reporting `cpu.busy_pct` above 100, or pinned near-constant across real load |
| the memory arithmetic is right | a Windows box reporting ~100% used while Task Manager disagrees |
| the disk selector filters correctly | a mounted ISO or USB stick opening a disk-free alert |
| absent evidence stays absent | any Windows check reporting a number after the underlying call failed |
| the first sample is not invented | a device reporting a CPU percentage within one tick of agent start |

---

## 6. Out of scope, stated so it is inherited rather than rediscovered

- **macOS.** Still `unavailable` on all five. Needs a cgo-free route to Mach
  statistics, which is a research question of its own.
- **`service.state`.** Still `unavailable` on every platform. §0.5 falsifier 3
  names the shell-out as the shape that breaks the idle claim, so it needs a
  real service-manager integration on both platforms.
- **The Linux `comm` 15-character truncation.** A known quirk, not touched here.
- **Running any of this on real Windows hardware.** Until that happens, this
  module's Windows column is *written*, not *verified*, and every document that
  mentions it must use the former word.
