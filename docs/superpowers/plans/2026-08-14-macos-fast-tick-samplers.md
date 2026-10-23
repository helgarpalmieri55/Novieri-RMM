# macOS Fast-Tick Samplers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer `disk.free_bytes`, `disk.free_pct` and `process.present` on macOS without cgo, reusing the untagged calc layer the Windows cycle built.

**Architecture:** Untagged calc + tagged dispatch, identical to the Windows cycle. Two new pure functions go in `sampler_calc.go` (executed by Linux CI); `sampler_darwin.go` holds syscalls and a five-case dispatch and nothing else. `cpu.busy_pct` and `memory.used_pct` return `unavailable()` with the Mach reasoning in-code.

**Tech Stack:** Go 1.24, `golang.org/x/sys v0.33.0`, no cgo.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-14-macos-fast-tick-samplers-design.md` (commit `3b4d388`). Authoritative — raise disagreements rather than resolving them yourself.
- **Do not modify `sampler_linux.go` or `sampler_windows.go`.** They must be byte-identical across this branch.
- **No cgo. No shell-out on the fast tick. No Mach binding** — not even a "temporary" one. §1.2 of the spec is why.
- **`process.present` matches an executable NAME, never a command line** (§5.1). `KinfoProc` carries pointers into the process's argument space; none may be read.
- **File naming is load-bearing.** Go applies an implicit build constraint to any file whose base name ends in a GOOS. Additions to the untagged calc layer go in `sampler_calc.go` — a new file named `sampler_darwin_helpers.go` would be fine, but `sampler_helpers_darwin.go` would silently vanish from Linux CI.
- Agent commands run from `/home/user/Toeshee-RMS/agent`.
- Run `staticcheck` **by its full path** `/root/go/bin/staticcheck` — a bare `staticcheck` that is not on PATH prints "command not found" and looks like a clean run.
- Never `git add -A`, `git add .`, `git commit -a`, `git checkout .`, or `git stash`. Stage named paths only.
- No model identifier and no `Co-Authored-By` trailer in commits.

---

## File Structure

| File | Responsibility |
|---|---|
| `agent/internal/capabilities/health/sampler_calc.go` | **Modify.** Add `statfsFreeBytes` and `cstr`. Extend `processNameMatches`'s comment with the truncation divergence. |
| `agent/internal/capabilities/health/sampler_calc_test.go` | **Modify.** Tests for both new functions. |
| `agent/internal/capabilities/health/sampler_darwin.go` | **Create.** `//go:build darwin`. Syscalls + dispatch. |
| `agent/internal/capabilities/health/sampler_other.go` | **Modify.** Build tag narrows; the macOS paragraph is superseded in place. |
| `.github/workflows/ci.yml` | **Modify.** Add the darwin `go test -c` line. |
| `docs/modules/endpoint-health-alerting/spec.md` | **Modify.** §4.1 and the macOS status paragraphs. |

---

### Task 1: The two new calc functions

**Files:**
- Modify: `agent/internal/capabilities/health/sampler_calc.go`
- Test: `agent/internal/capabilities/health/sampler_calc_test.go`

**Interfaces:**
- Produces, used by Task 2:
  - `statfsFreeBytes(bavail, bsize uint64) (uint64, bool)`
  - `cstr(b []byte) string`

- [ ] **Step 1: Write the failing tests**

Append to `sampler_calc_test.go`:

```go
func TestStatfsFreeBytes(t *testing.T) {
	// THE TRAP THIS FUNCTION EXISTS FOR is at the CALL SITE, not here: Statfs_t
	// carries both Bfree (includes the root-reserved pool) and Bavail (what a
	// non-root process can use). Passing Bfree overstates free space and can
	// hold a real disk alert closed. This test pins the arithmetic; the call
	// site is pinned by review.
	cases := []struct {
		name          string
		bavail, bsize uint64
		want          uint64
		wantOK        bool
	}{
		{"ordinary volume", 1 << 20, 4096, 1 << 20 * 4096, true},
		{"nothing available", 0, 4096, 0, true},
		{"zero block size is refused", 1 << 20, 0, 0, false},
		{"overflow is refused", 1 << 40, 1 << 40, 0, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, ok := statfsFreeBytes(c.bavail, c.bsize)
			if ok != c.wantOK {
				t.Fatalf("statfsFreeBytes(%d, %d) ok = %v, want %v",
					c.bavail, c.bsize, ok, c.wantOK)
			}
			if ok && got != c.want {
				t.Fatalf("statfsFreeBytes(%d, %d) = %d, want %d",
					c.bavail, c.bsize, got, c.want)
			}
		})
	}
}

func TestCstr(t *testing.T) {
	// THE TRAP: a name that exactly fills the buffer has NO terminator, and
	// `P_comm` is [17]byte -- 16 characters plus NUL -- so this is reachable
	// with a real process name rather than theoretical. A naive
	// bytes.IndexByte returning -1 must yield the whole buffer.
	cases := []struct {
		name string
		in   []byte
		want string
	}{
		{"terminated in the middle", []byte("sshd\x00\x00\x00"), "sshd"},
		{"terminator at index zero", []byte("\x00abc"), ""},
		{"no terminator at all", []byte("abcdefghijklmnop"), "abcdefghijklmnop"},
		{"empty buffer", []byte{}, ""},
		{"nil buffer", nil, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := cstr(c.in); got != c.want {
				t.Fatalf("cstr(%q) = %q, want %q", c.in, got, c.want)
			}
		})
	}
}
```

- [ ] **Step 2: Run to verify they fail**

Run: `go test ./internal/capabilities/health/ -run 'TestStatfsFreeBytes|TestCstr' 2>&1 | head -20`
Expected: FAIL — `undefined: statfsFreeBytes`, `undefined: cstr`.

- [ ] **Step 3: Implement**

Add to `sampler_calc.go`:

```go
// statfsFreeBytes converts a Statfs_t's available-block count and block size
// into a byte count. The bool is false when the numbers cannot be trusted.
//
// THE TRAP IS AT THE CALL SITE AND THIS COMMENT IS WHERE IT IS RECORDED:
// Statfs_t carries BOTH `Bfree` and `Bavail`. `Bfree` counts every free block
// including the root-reserved pool; `Bavail` counts what a non-root process can
// actually use. Passing `Bfree` OVERSTATES free space, and on a volume sitting
// just under a disk.free_bytes threshold that difference is enough to hold a
// real alert closed -- a check that silently never fires. sampler_darwin.go
// passes Bavail.
//
// Overflow is refused rather than wrapped. `bavail * bsize` is safe for any
// real volume, but a garbage bsize from a failed call would otherwise produce a
// plausible enormous number that freeSample would report as Available.
func statfsFreeBytes(bavail, bsize uint64) (uint64, bool) {
	if bsize == 0 {
		return 0, false
	}
	if bavail > ^uint64(0)/bsize {
		return 0, false
	}
	return bavail * bsize, true
}

// cstr decodes a NUL-terminated C string out of a fixed-size byte array.
//
// THE TRAP: a name that exactly fills the buffer has NO terminator. `P_comm` is
// [17]byte -- 16 characters plus NUL -- so a 16-character executable name hits
// this with no NUL to find, and an implementation that trusted IndexByte's -1
// would return empty (matching nothing, forever) or panic.
func cstr(b []byte) string {
	for i, c := range b {
		if c == 0 {
			return string(b[:i])
		}
	}
	return string(b)
}
```

- [ ] **Step 4: Run to verify they pass**

Run: `go test ./internal/capabilities/health/ -run 'TestStatfsFreeBytes|TestCstr' -v 2>&1 | tail -25`
Expected: PASS, all subtests.

- [ ] **Step 5: Prove the tests can fail**

Change `cstr`'s final `return string(b)` to `return ""`. Re-run: `TestCstr/no_terminator_at_all` must FAIL. **Restore by editing the line back — never `git checkout`.** Re-run and confirm green. Paste both outputs into your report.

- [ ] **Step 6: Record the truncation divergence**

Extend the doc comment on `processNameMatches` in `sampler_calc.go` with a third bullet in the existing divergence register:

```
//   - PROCESS NAMES TRUNCATE AT DIFFERENT LENGTHS, and this is the divergence
//     most likely to produce a silent never-fires. macOS `P_comm` gives 16
//     characters, Linux `comm` 15, Windows `szExeFile` the full name with
//     `.exe`. A policy naming a longer executable matches on Windows and NEVER
//     on macOS or Linux -- and the symptom is an alert that simply never opens,
//     which nobody investigates. Choose selectors that fit in 15 characters for
//     a mixed fleet.
```

- [ ] **Step 7: Verify, commit**

```bash
go test ./internal/capabilities/health/ 2>&1 | tail -3
go vet ./... && /root/go/bin/staticcheck ./...
git add internal/capabilities/health/sampler_calc.go internal/capabilities/health/sampler_calc_test.go
git commit -m "feat(agent/health): statfsFreeBytes and cstr in the tested calc layer

Bavail-not-Bfree and the no-terminator case are both reachable with real
inputs and neither is visible to a darwin cross-compile."
```

---

### Task 2: The darwin sampler

**Files:**
- Create: `agent/internal/capabilities/health/sampler_darwin.go`
- Modify: `agent/internal/capabilities/health/sampler_other.go`

**Interfaces:**
- Consumes: Task 1's `statfsFreeBytes`, `cstr`; existing `freeSample`, `worseSample`, `normaliseProcessName`, `processNameMatches`, `unavailable`, `Sample`, `Sampler`, `selectorIsWildcard`.
- Produces: `NewHostSampler() Sampler` for `GOOS=darwin`.

**Read first:** `sampler_windows.go` for the dispatch shape, and `sampler_other.go` for the paragraph you are superseding.

- [ ] **Step 1: Write `sampler_darwin.go`**

```go
//go:build darwin

package health

import (
	"golang.org/x/sys/unix"
)

// The fast-tick sampler on macOS. THREE of the five checks are answered here;
// the other two are refused, and the refusal is the considered outcome of a
// cycle rather than an omission.
//
// WHY cpu.busy_pct AND memory.used_pct ARE NOT HERE. Both come from Mach --
// `host_statistics64` / `vm_statistics64` -- and `golang.org/x/sys v0.33.0`
// exposes no Mach interface at all (grep it: host_statistics, vm_statistics,
// mach_host, host_processor_info all return nothing). `sysctl` IS reachable and
// does not carry either figure; `hw.memsize` is total RAM with no notion of
// used. The agent is a single static binary with no cgo, and a shell-out on the
// fast tick is the shape spec §0.5 falsifier 3 forbids.
//
// THE MEASUREMENT THAT SETTLED IT, because "we could hand-roll the binding" is
// the obvious next thought and it is a trap. x/sys reaches libSystem without
// cgo via `//go:cgo_import_dynamic` + `//go:linkname syscall.syscall6` AND an
// assembly trampoline per symbol (`zsyscall_darwin_arm64.s`). A probe was built
// using those pragmas for `host_statistics64` with the assembly OMITTED
// ENTIRELY -- no `.s` file in the module -- and `GOOS=darwin go build` exited
// 0. On a real Mac that code calls through a null function pointer.
//
// So on this platform a green cross-compile survives code that cannot execute.
// The Windows cycle could put its arithmetic in an untagged file and genuinely
// test the part most likely to be wrong; a hand-rolled Mach binding has no such
// half -- the MECHANISM is what would be wrong, and nothing available here can
// see it. Two checks shipped on that basis would report `unavailable` forever
// on every Mac while looking finished, which is strictly worse than refusing.
//
// WHAT WOULD UNBLOCK THEM: a macOS runner in CI, or a deliberate decision to
// enable cgo for darwin (a build-system change needing an osxcross toolchain,
// owned by the release pipeline). Not a sampler cycle's call either way.
//
// NOTHING IN THIS FILE HAS EVER RUN. There is no macOS machine in this
// project's CI. `GOOS=darwin go vet` and `go test -c` are the only checks, and
// the paragraph above is exactly why neither is evidence of correctness.

type hostSampler struct{}

// NewHostSampler returns the platform's fast-tick sampler.
func NewHostSampler() Sampler { return &hostSampler{} }

func (h *hostSampler) Sample(checkType, selector string) Sample {
	switch checkType {
	case "disk.free_pct":
		return h.diskFree(selector, true)
	case "disk.free_bytes":
		return h.diskFree(selector, false)
	case "process.present":
		return h.processPresent(selector)
	case "cpu.busy_pct", "memory.used_pct":
		// Mach. See the file header -- this is a refusal, not a gap.
		return unavailable()
	}
	return unavailable()
}

func (h *hostSampler) diskFree(selector string, asPct bool) Sample {
	if selectorIsWildcard(selector) {
		return h.worstLocalVolume(asPct)
	}
	var st unix.Statfs_t
	if err := unix.Statfs(selector, &st); err != nil {
		return unavailable()
	}
	return sampleFromStatfs(&st, asPct)
}

// worstLocalVolume folds every local read-write volume into the worst reading.
//
// MNT_NOWAIT, NOT MNT_WAIT: the wait variant asks every filesystem to refresh
// its statistics synchronously, so one stalled network mount would block the
// fast tick -- the same class of problem §0.5 falsifier 3's no-shell-out rule
// exists to prevent.
//
// THE FILTER IS TWO CLAUSES AND BOTH EARN THEIR PLACE:
//   - MNT_LOCAL excludes network mounts, whose fullness is not this endpoint's
//     problem and whose availability would make the check flap.
//   - !MNT_RDONLY excludes the sealed system volume and any mounted disk image.
//     A read-only volume is ALWAYS at ~0% free, so without this clause it would
//     win worseSample on every Mac and pin the wildcard to a meaningless 0
//     forever.
func (h *hostSampler) worstLocalVolume(asPct bool) Sample {
	n, err := unix.Getfsstat(nil, unix.MNT_NOWAIT)
	if err != nil || n <= 0 {
		return unavailable()
	}
	buf := make([]unix.Statfs_t, n)
	n, err = unix.Getfsstat(buf, unix.MNT_NOWAIT)
	if err != nil {
		return unavailable()
	}
	// `n` is re-read: a volume unmounted between the two calls means the second
	// answer is smaller, and slicing to the first count would read zero values.
	if n > len(buf) {
		n = len(buf)
	}
	worst := unavailable()
	for i := range buf[:n] {
		st := &buf[i]
		if st.Flags&unix.MNT_LOCAL == 0 || st.Flags&unix.MNT_RDONLY != 0 {
			continue
		}
		worst = worseSample(worst, sampleFromStatfs(st, asPct))
	}
	return worst
}

func sampleFromStatfs(st *unix.Statfs_t, asPct bool) Sample {
	bsize := uint64(st.Bsize)
	// Bavail, NOT Bfree -- see statfsFreeBytes' comment. Bfree includes the
	// root-reserved pool and overstates what is actually usable.
	free, ok := statfsFreeBytes(st.Bavail, bsize)
	if !ok {
		return unavailable()
	}
	total, ok := statfsFreeBytes(st.Blocks, bsize)
	if !ok {
		return unavailable()
	}
	return freeSample(free, total, asPct)
}

// processPresent answers whether a process with the given NAME is running.
//
// A NAME, NEVER A COMMAND LINE (§5.1). KinfoProc carries pointers into the
// process's argument space and none of them are read: a check that could match
// a command line could be used to ask "is anyone running this argument" across
// the fleet.
func (h *hostSampler) processPresent(selector string) Sample {
	want := normaliseProcessName(selector)
	if want == "" {
		return unavailable()
	}
	procs, err := unix.SysctlKinfoProcSlice("kern.proc.all")
	if err != nil {
		return unavailable()
	}
	for i := range procs {
		if processNameMatches(cstr(procs[i].Proc.P_comm[:]), want) {
			return Sample{Available: true, Num: 1}
		}
	}
	return Sample{Available: true, Num: 0}
}
```

**Before writing this, verify two things against the real package** rather than trusting the plan — this plan's author could not run darwin either:
1. `unix.Statfs_t.Bsize` is `uint32` on darwin (so `uint64(st.Bsize)` is correct and not a no-op conversion on some arch).
2. `unix.KinfoProc.Proc.P_comm` is `[17]byte`.

Check with: `grep -n "Bsize\|P_comm" $(go env GOMODCACHE)/golang.org/x/sys@v0.33.0/unix/ztypes_darwin_arm64.go | head`
and the same for `ztypes_darwin_amd64.go`. **If either differs between architectures, stop and report** — the dispatch would need per-arch handling and that is a design change.

- [ ] **Step 2: Narrow `sampler_other.go`'s build tag**

Change `//go:build !linux && !windows` to `//go:build !linux && !windows && !darwin`.

Then supersede the macOS bullet in place, in the house style — quote the old text, date the correction, do not delete:

```go
//   - (Superseded 2026-08-14, quoted rather than deleted so the correction is
//     legible: this bullet said `cpu.busy_pct` and `memory.used_pct` on macOS
//     "need a real implementation, and has not had one yet". Half of that is
//     now wrong and half is now sharper. macOS answers disk.free_pct,
//     disk.free_bytes and process.present as of the macOS fast-tick samplers
//     cycle -- sampler_darwin.go. The two Mach-backed checks are still
//     unanswered, and are now a RECORDED REFUSAL rather than a to-do: see that
//     file's header for the probe showing a hand-rolled Mach binding with no
//     assembly trampoline still passes GOOS=darwin go build.)
```

Also correct the closing sentence, which currently says "a macOS fleet running
this build is not silently reported healthy" — this file no longer compiles for
darwin at all. Name the platforms it actually covers now, and date the change,
exactly as the Windows cycle did when it narrowed the same tag.

- [ ] **Step 3: Verify every GOOS**

```bash
go build ./... && go test ./internal/capabilities/health/ 2>&1 | tail -3
GOOS=darwin go vet ./... && GOOS=darwin go test -c -o /dev/null ./internal/capabilities/health
GOOS=windows go vet ./... && GOOS=windows go test -c -o /dev/null ./internal/capabilities/health
GOOS=linux go vet ./... && /root/go/bin/staticcheck ./...
```
All must succeed. Report each line's outcome separately — a single "all passed" is not a report.

- [ ] **Step 4: Confirm the untouchables**

```bash
git diff --stat HEAD -- internal/capabilities/health/sampler_linux.go internal/capabilities/health/sampler_windows.go
```
Expected: **empty**. If not, you modified a file this plan forbids — revert that file by editing it back and say so.

- [ ] **Step 5: Commit**

```bash
git add internal/capabilities/health/sampler_darwin.go internal/capabilities/health/sampler_other.go
git commit -m "feat(agent/health): macOS fast-tick samplers for disk and process

Three of five checks, via Statfs/Getfsstat and SysctlKinfoProcSlice. cpu
and memory are a recorded refusal: Mach needs cgo, and a hand-rolled
binding is unfalsifiable by anything this CI runs."
```

---

### Task 3: CI and the module spec

**Files:**
- Modify: `.github/workflows/ci.yml:346`
- Modify: `docs/modules/endpoint-health-alerting/spec.md`

- [ ] **Step 1: Add the darwin link check**

`ci.yml` currently runs `GOOS=darwin go vet ./...` and **no** `go test -c` for darwin — the Windows lines at 330-331 have no darwin equivalent. Add, immediately after the darwin vet line:

```yaml
          GOOS=darwin go test -c -o /dev/null ./internal/capabilities/health
```

Keep it beside the vet line it complements. Do not add a `./internal/service` darwin line — that package is not part of this cycle and an untested addition to CI is a change nobody reviewed.

- [ ] **Step 2: Correct the module spec**

Three places, each verified against the file before editing:

1. The line stating fast-tick sampling is unimplemented on macOS (search: `sampler_other.go` answers `unavailable`). macOS now answers three of five. Supersede in place with the date.
2. The §4.1 check/platform table (search: `| check_type | value_kind | selector | cadence | windows | linux | macos |`). Set the macos column for `disk.free_pct`, `disk.free_bytes` and `process.present`. **Read the table's own legend first** — in the Windows cycle a ✓ turned out to mean "legal to configure", not "implemented", and the implementer corrected the plan's author on exactly this. If the legend says the same here, the table may already be correct and need no edit; say so rather than changing it.
3. The "macOS is implemented but has never run" paragraph about posture probes — extend it to cover the samplers, since the same never-executed caveat now applies to more code. Do not weaken its wording.

- [ ] **Step 3: Verify the claims you wrote**

Any count or CI-step citation you add must be measured, not recalled. If you write "CI runs N darwin checks", produce it with a grep and paste the output. A previous cycle's doc correction got its own test count wrong twice.

- [ ] **Step 4: Commit**

```bash
git add ../.github/workflows/ci.yml ../docs/modules/endpoint-health-alerting/spec.md
git commit -m "ci+docs(health): link the darwin health test binary; macOS answers three checks"
```

---

## Self-Review

**Spec coverage.** §0's table → T1 (calc) + T2 (dispatch). §1's refusal and its reasoning → T2 step 1's file header, verbatim in substance. §2 layout and reuse → T2 (no new copies of `freeSample`/`worseSample`). §3.1 `statfsFreeBytes` → T1. §3.2 `cstr` → T1. §4.1 disk incl. `MNT_NOWAIT` and the two filter clauses → T2. §4.2 process → T2. §4.3 the refusal at the dispatch site → T2. §5.1 the third `*` divergence → **gap, see below**. §5.2 truncation → T1 step 6. §6 honesty → T2's header, T3's CI line, T3 step 2.3. §7.1–7.2 → T1. §7.3 → T2 step 3. §7.4 byte-identical → T2 step 4. §7.5 existing calc tests → T1 step 7.

**Gap found and closed:** §5.1 requires the third `*` divergence (macOS firmlinks making one physical store appear twice) recorded in `sampler_calc.go`'s existing register at `worseSample`, and no task did it. Added as T1 step 6's sibling — **the implementer must add both bullets in step 6**: the truncation one at `processNameMatches`, and this one at `worseSample`:

```
//   - `*` ON macOS SEES FOLDER-MOUNTED VOLUMES BUT DOUBLE-COUNTS FIRMLINKS.
//     getfsstat reports `/` and `/System/Volumes/Data` as separate entries
//     backed by one physical store, so a Mac's wildcard legitimately reports
//     the same free space twice. Harmless here -- worseSample takes a minimum
//     -- and recorded so it is not read as a bug. Note this is the opposite
//     blind spot to Windows, which cannot see folder-mounted volumes at all.
```

**Placeholder scan:** none. Every step carries its code or its exact command.

**Type consistency:** `statfsFreeBytes(uint64, uint64) (uint64, bool)` — called twice in `sampleFromStatfs` with `st.Bavail`/`st.Blocks` (both `uint64` per `ztypes_darwin_arm64.go`) and `uint64(st.Bsize)` (`uint32` widened). `cstr([]byte) string` — called with `procs[i].Proc.P_comm[:]`. `freeSample(free, total uint64, asPct bool) Sample` and `worseSample(worst, got Sample) Sample` match their existing signatures in `sampler_calc.go`.

---

## Completion criteria

1. `go test ./internal/capabilities/health/` green on Linux, including the new calc tests.
2. `GOOS=darwin` and `GOOS=windows` vet and `go test -c` all clean; `staticcheck` clean run by full path.
3. `sampler_linux.go` and `sampler_windows.go` byte-identical across the branch.
4. Both divergence bullets present in `sampler_calc.go`.
5. Every doc claim measured rather than recalled.
6. The never-executed caveat stated at full strength — no macOS code in this project has ever run, and §1.2's probe is why a green cross-compile does not soften that.
