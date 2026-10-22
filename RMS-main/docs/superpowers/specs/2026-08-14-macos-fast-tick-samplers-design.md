# macOS fast-tick samplers: the three checks that are real

**Date:** 2026-08-14
**Module:** `endpoint-health-alerting` — `agent/internal/capabilities/health`
**Status:** design, approved for planning

---

## 0. What this cycle is

macOS answers `unavailable` for all five fast-tick checks today (`sampler_other.go`).
This cycle implements the three that can be implemented **correctly and without
cgo**, and leaves the other two `unavailable` with the reason recorded in code.

| check | macOS v1 | mechanism |
| --- | --- | --- |
| `disk.free_bytes` | **implemented** | `unix.Statfs` / `unix.Getfsstat` |
| `disk.free_pct` | **implemented** | as above |
| `process.present` | **implemented** | `unix.SysctlKinfoProcSlice("kern.proc.all")` |
| `cpu.busy_pct` | stays `unavailable` | needs Mach — §1 |
| `memory.used_pct` | stays `unavailable` | needs Mach — §1 |

macOS goes from 0/5 to 3/5. Every line shipped is one this project's CI can
reason about, which §1 shows is not true of the alternative.

---

## 1. Why `cpu.busy_pct` and `memory.used_pct` are not in this cycle

### 1.1 Mach is not reachable without cgo

Verified against `golang.org/x/sys v0.33.0`, the version this module pins: a
grep for `host_statistics`, `vm_statistics`, `mach_host` and
`host_processor_info` across the whole package returns **nothing**. `sysctl` is
reachable — `Sysctl`, `SysctlUint64`, `SysctlRaw`, `SysctlKinfoProcSlice` all
exist for darwin — and does not expose either figure. `hw.memsize` gives total
RAM and no notion of used.

The agent is a single static binary with no cgo (`CLAUDE.md`, agent
conventions), and a shell-out on the fast tick is the shape §0.5 falsifier 3
forbids. So these two checks need a hand-rolled binding to libSystem, or they
wait.

### 1.2 The measurement that settled it

x/sys reaches libSystem without cgo using `//go:cgo_import_dynamic` plus
`//go:linkname` to `syscall.syscall6`, and **an assembly trampoline per
symbol** — `zsyscall_darwin_arm64.s` defines `libc_getgroups_trampoline` and the
`libc_getgroups_trampoline_addr` global that the Go side calls through.

A probe was built to find out what our CI could see if that binding were wrong.
It used the same pragmas for `host_statistics64`, and **omitted the assembly
entirely — no `.s` file in the module at all**:

```
$ GOOS=darwin GOARCH=arm64 go build -o /dev/null ./...
EXIT=0
```

Clean. On a real Mac that code calls through a null function pointer.

**This is the FILETIME trap one level deeper, and the difference is what decides
the cycle.** In the Windows cycle the syscalls were unexecutable but the
*arithmetic* they fed was moved into an untagged file that Linux CI runs, so the
part most likely to be silently wrong was genuinely tested. A hand-rolled Mach
binding has no such half: the **mechanism itself** — pragma spelling, trampoline
symbol names, the `_addr` global, the argument marshalling — is what would be
wrong, and every check available here reports success on a binding that cannot
execute at all.

Shipping two checks on that basis would mean two `unavailable`-forever counters
on every Mac in the fleet, indistinguishable from the current state except that
the code would *look* finished. `unavailable` is a first-class visible state
(§0.7); a check that silently never fires is worse than one that honestly
reports it cannot answer.

### 1.3 What would unblock them

Either a macOS runner in CI — at which point a hand-rolled binding becomes
testable and is a reasonable cycle — or a deliberate decision to enable cgo for
darwin, which is a build-system change (cross-compiling cgo for darwin needs an
osxcross toolchain) and belongs to whoever owns the release pipeline, not to a
sampler cycle. Recorded in `sampler_darwin.go` so the next reader inherits the
reasoning rather than the conclusion.

---

## 2. Architecture: the Windows layout, reused

Same split, for the same reason, with one addition.

- **`sampler_calc.go`** — no build tag, executed by Linux CI. Already holds
  `freeSample`, `worseSample`, `usedPctFromBytes`, `normaliseProcessName`,
  `processNameMatches` and `unavailable()`. This cycle **reuses all of them**
  and adds only what darwin needs and Windows did not (§3).
- **`sampler_darwin.go`** (`//go:build darwin`) — syscalls and the five-case
  dispatch. No arithmetic, no parsing.
- **`sampler_other.go`** — build tag narrows to
  `//go:build !linux && !windows && !darwin`.

The reuse is the point. `freeSample`'s asymmetry (a zero total is `unavailable`
in percent mode but "zero bytes free" is a real answer in bytes mode) and
`worseSample`'s "worse, not better" comparison are already pinned by tests, and
a second platform getting them for free is what the untagged layer was built
for. `sampler_linux.go` and `sampler_windows.go` are **not modified**.

---

## 3. New calc-layer functions

Two, both in `sampler_calc.go`, both tested on Linux.

### 3.1 `statfsFreeBytes(bavail, bsize uint64) (uint64, bool)`

Free bytes from a `Statfs_t`. Returns `false` on overflow.

**THE TRAP: `Bavail`, NOT `Bfree`.** `Statfs_t` carries both. `Bfree` counts
every free block including the root-reserved pool; `Bavail` counts what a
non-root process can actually use. The agent does not run as root for this
purpose, and reporting `Bfree` **overstates** free space — on a volume sitting
just under a `disk.free_bytes` threshold, the difference is enough to hold a
real alert closed. The function takes the two numbers it needs so the choice is
made at the call site in `sampler_darwin.go` and pinned by a test here.

Overflow is refused rather than wrapped: `blocks * bsize` on a `uint64` is
safe for any real volume, but a garbage `Bsize` from a failed call would
otherwise produce a plausible enormous number, and `freeSample` would report it
as `Available`.

### 3.2 `cstr(b []byte) string`

Decode a NUL-terminated C string out of a fixed-size array.

**The trap: a name that exactly fills the buffer has no NUL**, so a naive
`IndexByte` returning -1 must yield the whole buffer rather than panicking or
returning empty. `P_comm` is `[17]byte` — 16 characters plus terminator — so
this is reachable with a real process name, not a theoretical case.

---

## 4. `sampler_darwin.go`

### 4.1 Disk

- **Named selector** (`/`, `/Volumes/Data`): `unix.Statfs(path, &st)`, then
  `statfsFreeBytes(st.Bavail, uint64(st.Bsize))` and total from
  `st.Blocks * st.Bsize`, into `freeSample`.
- **Wildcard `*`**: `unix.Getfsstat(nil, unix.MNT_NOWAIT)` to size, then fill,
  filter, and fold with `worseSample`.

`MNT_NOWAIT` and not `MNT_WAIT`: the wait variant asks every filesystem to
update its statistics synchronously, which on a machine with a stalled network
mount blocks the fast tick. This is the same class of decision as §0.5's
no-shell-out rule.

**Filter: `Flags&MNT_LOCAL != 0` and `Flags&MNT_RDONLY == 0`.** Local excludes
network mounts, whose fullness is not this endpoint's problem and whose
availability would make the check flap. Read-only excludes the sealed system
volume and mounted disk images — a read-only volume is *always* ~0% free and
would win `worseSample` on every Mac, permanently pinning the wildcard to a
meaningless 0.

### 4.2 `process.present`

`unix.SysctlKinfoProcSlice("kern.proc.all")`, matching
`processNameMatches(cstr(p.Proc.P_comm[:]), normaliseProcessName(selector))`.

Matches an executable **name**, never a command line (§5.1) — `KinfoProc` does
carry pointers into the process's argument space, and none of them are read.

### 4.3 CPU and memory

`return unavailable()`, with §1's reasoning in a comment at the dispatch site
rather than only in this document.

---

## 5. Cross-platform divergences to record

In the register `sampler_calc.go` already established, because a policy author
writing one rule for a mixed fleet needs these and will not find them by
reading three platform files.

1. **`*` means a third thing again.** Linux walks `/proc/mounts` with a
   filesystem-type allow-list that includes removable media. Windows enumerates
   fixed drive letters, excluding removable and invisible to folder-mounted
   volumes. macOS takes local read-write volumes from `getfsstat`, which *does*
   see folder-mounted volumes but also surfaces APFS firmlinks — `/` and
   `/System/Volumes/Data` are separate entries backed by one physical store, so
   a Mac's wildcard legitimately reports the same free space twice. Harmless for
   `worseSample`, which takes a minimum, and stated so nobody reads it as a bug.
2. **Process names truncate at different lengths.** macOS `P_comm` gives 16
   characters, Linux `comm` 15, Windows `szExeFile` the full name with `.exe`.
   A policy naming a long executable matches on Windows and **never** on macOS
   or Linux. This is the divergence most likely to produce a silent
   never-fires, so it is documented at `processNameMatches` where a reader
   choosing a selector will see it.

---

## 6. Honesty requirements

**No macOS machine exists in this project's CI, and none of this code will have
executed when it ships.** `GOOS=darwin go vet ./...` already runs; this cycle
adds `GOOS=darwin go test -c -o /dev/null ./internal/capabilities/health`,
matching the Windows line. Neither runs anything.

§1.2 is the reason that caveat must be stated at full strength rather than as
boilerplate: on this platform a green cross-compile has already been
demonstrated to survive code that cannot run. What *is* verified is the calc
layer — `statfsFreeBytes`, `cstr`, and the five existing functions macOS now
shares — which Linux CI executes.

The spec's §0.5 idle budget is **not** re-measured for darwin and must not be
described as verified there. The existing budget falsifiers run on Linux.

---

## 7. Testing requirements

Test-first per `CLAUDE.md`.

1. **`statfsFreeBytes` uses `Bavail` semantics and refuses overflow.** Cases:
   ordinary volume; zero available; `bsize` of 0; a `bsize`/`bavail` pair that
   overflows `uint64`. Falsifier: the overflow case is what catches a naive
   multiply.
2. **`cstr` handles the no-NUL case.** Cases: NUL in the middle, NUL at index 0
   (empty), **no NUL at all** (full buffer), and a buffer of length 0.
   Falsifier: the no-NUL case is the one a naive `IndexByte` implementation
   fails, and `P_comm[17]` makes it reachable.
3. **The darwin sampler compiles and vets** under `GOOS=darwin`, and the health
   package links as a test binary. States nothing about behaviour.
4. **`sampler_linux.go` and `sampler_windows.go` are byte-identical** across the
   branch — asserted by review, not by a test, and stated here so the reviewer
   knows to check.
5. **The existing calc tests stay green**, since macOS now depends on five
   functions Windows already relies on.

---

## 8. What would falsify this design

- **If `Getfsstat`'s two-call sizing races a mount appearing between calls**, the
  second call truncates. Not fatal — the wildcard samples one fewer volume for
  one tick — but if it turns out to error rather than truncate, the dispatch
  needs a retry and this section is wrong.
- **If `MNT_LOCAL` excludes something operators expect**, e.g. a Thunderbolt
  external that reports as non-local, the wildcard under-reports. The observable
  is an operator saying a full external drive never alerted.
- **If `P_comm` proves to hold something other than the executable's base name**
  on modern macOS — a bundle identifier, say — then `process.present` matches
  nothing and §5.2's truncation note is beside the point. Unfalsifiable here;
  first macOS endpoint to run this settles it.

---

## 9. Sources

- `docs/modules/endpoint-health-alerting/spec.md` §0.5, §0.7, §4.1, §5.1
- `agent/internal/capabilities/health/sampler_calc.go`, `sampler_linux.go`,
  `sampler_windows.go`, `sampler_other.go`
- `golang.org/x/sys v0.33.0` — `syscall_darwin.go`, `ztypes_darwin_arm64.go`,
  `zsyscall_darwin_arm64.s`, `syscall_darwin_libSystem.go`
- The trampoline-omission probe, run 2026-08-14 (§1.2).
