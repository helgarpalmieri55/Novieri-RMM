# Windows Fast-Tick Samplers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `health.check` fast tick answer its five checks on Windows, so the proactive health loop stops being a Linux-only feature.

**Architecture:** All arithmetic lives in an untagged `sampler_calc.go` that the Linux CI runner actually executes and tests. A `//go:build windows` `sampler_windows.go` does only the raw reads and dispatch, routing every decision through the calc layer. `sampler_linux.go` is untouched so its measured falsifier stays measured.

**Tech Stack:** Go 1.24, `golang.org/x/sys/windows` v0.33.0 (already a dependency), no cgo.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-13-windows-fast-tick-samplers-design.md` (commit `c73074a`). Where this plan and the spec disagree, the spec governs — raise it rather than choosing.
- **No cgo.** The agent is a single static binary (CLAUDE.md agent conventions).
- **No shell-out on the fast tick.** §0.5 falsifier 3 names that as the shape that breaks the idle claim.
- **`process.present` matches an executable NAME, never a command line.** §5.1: a command line never travels on this wire. `PROCESSENTRY32.szExeFile` satisfies this; `Win32_Process.CommandLine` does not and is out of bounds regardless of convenience.
- **A failed call returns `unavailable()`, never a zero or a false.** Absent evidence is a first-class state that never opens and never clears an alert.
- **`sampler_linux.go` is not edited by any task in this plan.**
- **Never claim Windows behaviour is verified.** It is written and compile-checked. Every doc and comment that mentions it says so.
- Never `git add -A`, `git add .`, `git commit -a`, `git checkout .`, or `git stash`. Stage named paths only.
- No model identifier and no `Co-Authored-By` trailer in commit messages.
- After every task: `cd /home/user/Toeshee-RMS/agent && gofmt -l . && go vet ./... && GOOS=windows go vet ./... && GOOS=darwin go vet ./... && /root/go/bin/staticcheck ./... && go test ./internal/capabilities/health/ -count=1`. `gofmt -l` must print nothing.

### Two facts established before this plan was written, so you do not rediscover them

1. **staticcheck's U1000 counts test usage.** Measured: a function used only by a `_test.go` is NOT reported; a function used by nothing IS (`func probeUsedByNobody is unused (U1000)`). Every calc function below has an untagged test, so none will trip it. If you add a calc helper with no test, CI will fail on Linux even though Windows uses it.
2. **`x/sys/windows` already provides** `GetDiskFreeSpaceEx`, `GetDriveType`, `GetLogicalDriveStrings`, `GetWindowsDirectory`, `CreateToolhelp32Snapshot`, `Process32First`, `Process32Next`, `ProcessEntry32`, `DRIVE_FIXED`, `TH32CS_SNAPPROCESS`, `Filetime`, `UTF16PtrFromString`, `UTF16ToString`. **Only `GetSystemTimes` and `GlobalMemoryStatusEx` need hand-rolled `LazyProc`s.** Do not hand-roll anything else.

---

## File Structure

| File | Build tag | Responsibility |
|---|---|---|
| `agent/internal/capabilities/health/sampler_calc.go` | **none** | pure arithmetic and string logic: CPU counter conversion and deltas, bytes→percent, drive-string parsing, process-name matching |
| `agent/internal/capabilities/health/sampler_calc_test.go` | **none** | the tests that actually EXECUTE, on Linux CI |
| `agent/internal/capabilities/health/sampler_windows.go` | `windows` | two hand-rolled procs, the five-case dispatch, previous-CPU-counter state |
| `agent/internal/capabilities/health/sampler_windows_test.go` | `windows` | compile-checked, never-run: the `dwLength` helper and the budget falsifier |
| `agent/internal/capabilities/health/sampler_other.go` | `!linux && !windows` | narrowed; its false WMI comment rewritten |
| `.github/workflows/ci.yml` | — | one added `go test -c` line |
| `docs/modules/endpoint-health-alerting/spec.md` | — | §4.1 table's `windows` column |

---

### Task 1: The arithmetic, untagged and actually executed

**Files:**
- Create: `agent/internal/capabilities/health/sampler_calc.go`
- Create: `agent/internal/capabilities/health/sampler_calc_test.go`

**Interfaces:**
- Produces, all unexported, all used by Task 2-4:
  `windowsCPUBusyTotal(idle, kernel, user uint64) (busy, total uint64)`,
  `cpuBusyDelta(prevBusy, prevTotal, busy, total uint64, had bool) Sample`,
  `usedPctFromBytes(total, avail uint64) Sample`,
  `freeSample(free, total uint64, asPct bool) Sample`,
  `splitDriveStrings(buf []uint16) []string`,
  `normaliseProcessName(s string) string`,
  `processNameMatches(got, want string) bool`.
- Consumes: `Sample` and `unavailable()` from `checks.go`.

- [ ] **Step 1: Write the failing tests**

Create `agent/internal/capabilities/health/sampler_calc_test.go`:

```go
package health

import (
	"testing"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

func TestWindowsCPUBusyTotal(t *testing.T) {
	// THE TRAP THIS TEST EXISTS FOR: on Windows, kernel time ALREADY INCLUDES
	// idle time. Computing busy as kernel+user counts every idle tick as busy
	// and reports plausible, permanently inflated CPU on every endpoint.
	// Anyone "simplifying" this turns this test red.
	cases := []struct {
		name                      string
		idle, kernel, user        uint64
		wantBusy, wantTotal       uint64
	}{
		{"half idle", 50, 100, 0, 50, 100},
		{"fully idle", 100, 100, 0, 0, 100},
		{"user work is never idle", 0, 40, 60, 100, 100},
		{"idle counted once, not twice", 30, 80, 20, 70, 100},
		{"idle exceeding total is refused", 200, 100, 0, 0, 0},
		{"all zero", 0, 0, 0, 0, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			busy, total := windowsCPUBusyTotal(c.idle, c.kernel, c.user)
			if busy != c.wantBusy || total != c.wantTotal {
				t.Fatalf("windowsCPUBusyTotal(%d,%d,%d) = (%d,%d), want (%d,%d)",
					c.idle, c.kernel, c.user, busy, total, c.wantBusy, c.wantTotal)
			}
		})
	}
}

func TestCPUBusyDelta(t *testing.T) {
	cases := []struct {
		name                                string
		prevBusy, prevTotal, busy, total    uint64
		had                                 bool
		wantAvailable                       bool
		wantNum                             float64
	}{
		{"no previous sample is unavailable", 0, 0, 50, 100, false, false, 0},
		{"half busy", 0, 0, 50, 100, true, true, 50},
		{"idle machine", 100, 1000, 100, 2000, true, true, 0},
		{"pegged machine", 100, 1000, 1100, 2000, true, true, 100},
		{"counter did not advance", 100, 1000, 100, 1000, true, false, 0},
		{"counter went backwards (wraparound)", 100, 2000, 100, 1000, true, false, 0},
		{"busy advancing faster than total is refused", 0, 0, 200, 100, true, false, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := cpuBusyDelta(c.prevBusy, c.prevTotal, c.busy, c.total, c.had)
			if got.Available != c.wantAvailable {
				t.Fatalf("Available = %v, want %v", got.Available, c.wantAvailable)
			}
			if got.Available && got.Num != c.wantNum {
				t.Fatalf("Num = %v, want %v", got.Num, c.wantNum)
			}
		})
	}
}

func TestUsedPctFromBytes(t *testing.T) {
	cases := []struct {
		name          string
		total, avail  uint64
		wantAvailable bool
		wantNum       float64
	}{
		{"quarter used", 100, 75, true, 25},
		{"nothing used", 100, 100, true, 0},
		{"everything used", 100, 0, true, 100},
		{"zero total is unavailable", 0, 0, false, 0},
		{"available exceeding total is refused", 100, 200, false, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := usedPctFromBytes(c.total, c.avail)
			if got.Available != c.wantAvailable {
				t.Fatalf("Available = %v, want %v", got.Available, c.wantAvailable)
			}
			if got.Available && got.Num != c.wantNum {
				t.Fatalf("Num = %v, want %v", got.Num, c.wantNum)
			}
		})
	}
}

func TestFreeSample(t *testing.T) {
	if got := freeSample(4096, 8192, false); !got.Available || got.Num != 4096 {
		t.Fatalf("bytes mode = %+v, want the free byte count", got)
	}
	if got := freeSample(4096, 8192, true); !got.Available || got.Num != 50 {
		t.Fatalf("percent mode = %+v, want 50", got)
	}
	// Zero total is unavailable in PERCENT mode only: a percentage of nothing
	// is undefined, but "zero bytes free" is a real and alarming answer.
	if got := freeSample(0, 0, true); got.Available {
		t.Fatalf("percent of a zero-byte volume = %+v, want unavailable", got)
	}
	if got := freeSample(0, 0, false); !got.Available || got.Num != 0 {
		t.Fatalf("bytes mode on a zero-byte volume = %+v, want an available 0", got)
	}
}

func TestSplitDriveStrings(t *testing.T) {
	// GetLogicalDriveStringsW writes NUL-separated roots with a double NUL at
	// the end. The trailing empty string is the classic off-by-one here.
	buf := utf16Of("C:\\\x00D:\\\x00\x00")
	got := splitDriveStrings(buf)
	if len(got) != 2 || got[0] != "C:\\" || got[1] != "D:\\" {
		t.Fatalf("splitDriveStrings = %q, want [C:\\ D:\\]", got)
	}
	if len(splitDriveStrings(nil)) != 0 {
		t.Fatal("splitDriveStrings(nil) returned entries")
	}
	if len(splitDriveStrings(utf16Of("\x00"))) != 0 {
		t.Fatal("an empty list returned entries")
	}
}

func utf16Of(s string) []uint16 {
	out := make([]uint16, 0, len(s))
	for _, r := range s {
		out = append(out, uint16(r))
	}
	return out
}

func TestProcessNameMatching(t *testing.T) {
	// Windows process names carry .exe and are case-insensitive; Linux `comm`
	// has neither property. One policy should behave sanely on both, so the
	// selector is accepted with or without the extension, in any case.
	cases := []struct {
		got, want string
		match     bool
	}{
		{"sshd.exe", "sshd", true},
		{"sshd.exe", "sshd.exe", true},
		{"SSHD.EXE", "sshd", true},
		{"sshd.exe", "SSHD", true},
		{"nginx.exe", "sshd", false},
		{"sshd.exe", "ssh", false},
		{"sshd", "sshd", true},
	}
	for _, c := range cases {
		if got := processNameMatches(c.got, normaliseProcessName(c.want)); got != c.match {
			t.Fatalf("processNameMatches(%q, %q) = %v, want %v", c.got, c.want, got, c.match)
		}
	}
	if normaliseProcessName("   ") != "" {
		t.Fatal("a blank selector must normalise to empty so the caller can refuse it")
	}
}

func TestTheCalcLayerAllocatesNothing(t *testing.T) {
	// The measurable half of this cycle's idle-budget claim. The syscalls
	// cannot run on this runner; the arithmetic they feed can, and it runs on
	// the FAST tick, so it must not allocate.
	budget.AssertNoAllocations(t, "windows sampler calc", func() func() {
		return func() {
			busy, total := windowsCPUBusyTotal(30, 80, 20)
			_ = cpuBusyDelta(10, 50, busy, total, true)
			_ = usedPctFromBytes(1 << 30, 1 << 29)
			_ = freeSample(1<<28, 1<<30, true)
			_ = processNameMatches("sshd.exe", "sshd")
		}
	})
}
```

- [ ] **Step 2: Run and watch it fail**

Run: `cd /home/user/Toeshee-RMS/agent && go test ./internal/capabilities/health/ -run 'CPUBusy|UsedPct|FreeSample|DriveStrings|ProcessName|CalcLayer' -count=1`
Expected: FAIL — `undefined: windowsCPUBusyTotal` and the rest.

- [ ] **Step 3: Write the implementation**

Create `agent/internal/capabilities/health/sampler_calc.go`:

```go
package health

import (
	"strings"
	"unicode/utf16"
)

// Pure arithmetic and string logic shared by the platform samplers.
//
// NO BUILD TAG, AND THE FILE NAME IS LOAD-BEARING. Go applies an implicit
// build constraint to any file whose base name ends in a GOOS, so naming this
// `sampler_windows_something.go` is fine but anything ending `_windows.go`
// would exclude it from every non-Windows build -- including the Linux test
// run that is the only place this code is ever EXECUTED. `_calc` is not a
// GOOS. posture_darwin_probes.go records the same rule; cycle 3 lost time to
// posture_probe_linux.go.
//
// Everything here is a pure function over integers and strings. That is the
// point: the Windows syscalls cannot run on this project's Linux CI, but the
// sums and the parsing they feed can, so this is the part that is genuinely
// tested rather than merely compiled.

// windowsCPUBusyTotal converts GetSystemTimes' three cumulative FILETIME
// totals into the (busy, total) pair cpuBusyDelta consumes.
//
// THE TRAP THIS FUNCTION EXISTS TO CONTAIN: on Windows the kernel time
// ALREADY INCLUDES idle time. Computing busy as kernel+user counts every idle
// tick as busy and reports plausible, permanently inflated CPU on every
// Windows endpoint -- a number nobody would question until an operator
// compared it with Task Manager. TestWindowsCPUBusyTotal pins the correct
// form.
//
// Counters that disagree with each other (idle greater than kernel+user)
// return a zero total, which the caller reads as unavailable. A host whose
// own counters are inconsistent is not a licence to guess.
func windowsCPUBusyTotal(idle, kernel, user uint64) (busy, total uint64) {
	total = kernel + user
	if idle > total {
		return 0, 0
	}
	return total - idle, total
}

// cpuBusyDelta turns two cumulative (busy, total) readings into a percentage.
//
// The first reading after start returns unavailable: a rate needs two
// samples, and inventing one is how a device reports 100% CPU one second
// after the agent starts. A total that has not advanced -- or has gone
// backwards, which is what a counter wraparound looks like from here -- is
// also unavailable rather than a division by zero or a negative percentage.
func cpuBusyDelta(prevBusy, prevTotal, busy, total uint64, had bool) Sample {
	if !had || total <= prevTotal {
		return unavailable()
	}
	dt := total - prevTotal
	db := busy - prevBusy
	if db > dt {
		// Busy cannot advance faster than total. Either a counter wrapped or
		// the two readings came from different epochs.
		return unavailable()
	}
	return Sample{Available: true, Num: 100 * float64(db) / float64(dt)}
}

// usedPctFromBytes answers memory.used_pct from a total and an available.
func usedPctFromBytes(total, avail uint64) Sample {
	if total == 0 || avail > total {
		return unavailable()
	}
	return Sample{Available: true, Num: 100 * float64(total-avail) / float64(total)}
}

// freeSample answers disk.free_bytes (asPct false) or disk.free_pct (true).
//
// Note the asymmetry, which is deliberate: a zero total is unavailable in
// PERCENT mode because a percentage of nothing is undefined, but "zero bytes
// free" is a real and alarming answer that must reach the operator.
func freeSample(free, total uint64, asPct bool) Sample {
	if !asPct {
		return Sample{Available: true, Num: float64(free)}
	}
	if total == 0 || free > total {
		return unavailable()
	}
	return Sample{Available: true, Num: 100 * float64(free) / float64(total)}
}

// splitDriveStrings parses GetLogicalDriveStringsW's buffer: NUL-separated
// root paths ("C:\", "D:\") terminated by a second NUL.
//
// The trailing empty string is the classic off-by-one here, and it matters:
// an empty root passed to GetDriveTypeW answers about the CURRENT DIRECTORY's
// drive, so a bug here would silently sample one volume twice and miss
// another.
func splitDriveStrings(buf []uint16) []string {
	var out []string
	start := 0
	for i, c := range buf {
		if c != 0 {
			continue
		}
		if i > start {
			out = append(out, string(utf16.Decode(buf[start:i])))
		}
		start = i + 1
	}
	return out
}

// normaliseProcessName prepares a policy selector for matching. Empty means
// the caller must refuse: process.present with no name is unanswerable, not
// false.
func normaliseProcessName(s string) string {
	return strings.ToLower(strings.TrimSpace(s))
}

// processNameMatches compares a running process's executable name against an
// already-normalised selector.
//
// Windows names carry `.exe` and are case-insensitive; Linux `comm` has
// neither property. Accepting the selector with or without the extension is
// what lets one policy be written once and behave sanely on both platforms --
// exact matching would make a cross-platform policy impossible to express.
//
// This matches a NAME and never a command line. §5.1: a command line never
// travels on this wire, because a check that matched one could be used to ask
// "is anyone running this argument" across the fleet.
func processNameMatches(got, want string) bool {
	if want == "" {
		return false
	}
	g := strings.ToLower(strings.TrimSpace(got))
	if g == want {
		return true
	}
	return strings.TrimSuffix(g, ".exe") == strings.TrimSuffix(want, ".exe")
}
```

- [ ] **Step 4: Run the tests**

Run: `cd /home/user/Toeshee-RMS/agent && go test ./internal/capabilities/health/ -run 'CPUBusy|UsedPct|FreeSample|DriveStrings|ProcessName|CalcLayer' -count=1 -v 2>&1 | tail -20`
Expected: PASS, every subtest.

- [ ] **Step 5: Prove the CPU test bites**

Change `windowsCPUBusyTotal`'s body to the wrong-but-plausible form — `return kernel + user, kernel + user` — run the test, capture the failure verbatim, then restore by editing and confirm `sha256sum agent/internal/capabilities/health/sampler_calc.go` matches its pre-plant value with `git status --porcelain` clean. Paste both into your report. This is the single most valuable test in the plan: it is the one defect that would ship silently and look right.

- [ ] **Step 6: Commit**

```bash
git add agent/internal/capabilities/health/sampler_calc.go \
        agent/internal/capabilities/health/sampler_calc_test.go
git commit -m "feat(agent/health): the arithmetic the Windows sampler will need, tested on Linux"
```

---

### Task 2: The Windows sampler — CPU and memory

**Files:**
- Create: `agent/internal/capabilities/health/sampler_windows.go`
- Modify: `agent/internal/capabilities/health/sampler_other.go`

**Interfaces:**
- Consumes: `windowsCPUBusyTotal`, `cpuBusyDelta`, `usedPctFromBytes` (Task 1); `Sample`, `unavailable()`, `Sampler`, `selectorIsWildcard` from the package.
- Produces: `NewHostSampler() Sampler` on Windows; `hostSampler` with `diskFree(selector string, asPct bool) Sample` and `processPresent(selector string) Sample` — both stubbed to `unavailable()` here and filled in by Tasks 3 and 4.

**Read first:** `sampler_linux.go`'s `Sample` method. Yours has the same five cases in the same order under the same mutex. Do not invent a different shape.

- [ ] **Step 1: Write `sampler_windows.go`**

```go
//go:build windows

package health

import (
	"sync"
	"unsafe"

	"golang.org/x/sys/windows"
)

// The Windows host sampler (§4.1's table, `windows` column).
//
// NOTHING IN THIS FILE HAS EVER EXECUTED. It compiles under
// `GOOS=windows go vet ./...` and `GOOS=windows go test -c`, and no machine on
// this project can run it. Every DECISION it makes is made in
// sampler_calc.go, which is untagged and is exercised by real tests on Linux;
// what lives here is the raw reads and the dispatch. That split is the whole
// design: it keeps the unverifiable surface as small as the platform allows.
//
// # Why LazyProc for two calls and x/sys for the rest
//
// golang.org/x/sys/windows already wraps GetDiskFreeSpaceEx, GetDriveType,
// GetLogicalDriveStrings, GetWindowsDirectory and the whole Toolhelp API with
// checked Go signatures, so those are used directly. It does NOT wrap
// GetSystemTimes or GlobalMemoryStatusEx, so exactly two procs are declared
// here -- the same NewLazySystemDLL mechanism posture_windows.go uses for
// ole32/oleaut32. No cgo, no WMI, no performance counters, no shell-out.
var (
	modkernel32              = windows.NewLazySystemDLL("kernel32.dll")
	procGetSystemTimes       = modkernel32.NewProc("GetSystemTimes")
	procGlobalMemoryStatusEx = modkernel32.NewProc("GlobalMemoryStatusEx")
)

// memoryStatusEx mirrors MEMORYSTATUSEX. Field order and widths are the ABI;
// do not reorder them.
type memoryStatusEx struct {
	Length               uint32
	MemoryLoad           uint32
	TotalPhys            uint64
	AvailPhys            uint64
	TotalPageFile        uint64
	AvailPageFile        uint64
	TotalVirtual         uint64
	AvailVirtual         uint64
	AvailExtendedVirtual uint64
}

// newMemoryStatusEx is the ONE place Length is set.
//
// GlobalMemoryStatusEx FAILS unless dwLength is the struct's own size. That is
// the class of mistake that compiles cleanly and returns garbage at runtime,
// on a platform nothing in this project can run -- so it is confined to a
// single constructor rather than repeated at each call site.
func newMemoryStatusEx() memoryStatusEx {
	var m memoryStatusEx
	m.Length = uint32(unsafe.Sizeof(m))
	return m
}

// hostSampler answers the fast-cadence checks. It holds the previous CPU
// counters and nothing else: no cache of samples, no history (§0.12).
type hostSampler struct {
	mu        sync.Mutex
	haveCPU   bool
	prevBusy  uint64
	prevTotal uint64
}

// NewHostSampler returns the platform's fast-tick sampler.
func NewHostSampler() Sampler { return &hostSampler{} }

func (h *hostSampler) Sample(checkType, selector string) Sample {
	h.mu.Lock()
	defer h.mu.Unlock()
	switch checkType {
	case "cpu.busy_pct":
		return h.cpuBusyPct()
	case "memory.used_pct":
		return h.memoryUsedPct()
	case "disk.free_pct":
		return h.diskFree(selector, true)
	case "disk.free_bytes":
		return h.diskFree(selector, false)
	case "process.present":
		return h.processPresent(selector)
	}
	return unavailable()
}

// filetimeToUint64 joins a FILETIME's two halves into the 100ns tick count.
func filetimeToUint64(ft windows.Filetime) uint64 {
	return uint64(ft.HighDateTime)<<32 | uint64(ft.LowDateTime)
}

func (h *hostSampler) cpuBusyPct() Sample {
	var idle, kernel, user windows.Filetime
	r1, _, _ := procGetSystemTimes.Call(
		uintptr(unsafe.Pointer(&idle)),
		uintptr(unsafe.Pointer(&kernel)),
		uintptr(unsafe.Pointer(&user)),
	)
	if r1 == 0 {
		return unavailable()
	}
	busy, total := windowsCPUBusyTotal(
		filetimeToUint64(idle), filetimeToUint64(kernel), filetimeToUint64(user))
	if total == 0 {
		return unavailable()
	}
	prevBusy, prevTotal, had := h.prevBusy, h.prevTotal, h.haveCPU
	h.prevBusy, h.prevTotal, h.haveCPU = busy, total, true
	return cpuBusyDelta(prevBusy, prevTotal, busy, total, had)
}

func (h *hostSampler) memoryUsedPct() Sample {
	m := newMemoryStatusEx()
	r1, _, _ := procGlobalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&m)))
	if r1 == 0 {
		return unavailable()
	}
	return usedPctFromBytes(m.TotalPhys, m.AvailPhys)
}

// diskFree is filled in by the disk task; see sampler_windows.go's history.
func (h *hostSampler) diskFree(string, bool) Sample { return unavailable() }

// processPresent is filled in by the process task.
func (h *hostSampler) processPresent(string) Sample { return unavailable() }
```

**Note on unused helpers, so you do not add a blank-reference block you do not
need.** `sampler_other.go` carries a `var _ = selectorIsWildcard` to keep a
Linux-only helper vetted everywhere. You do **not** need the equivalent for the
calc functions: staticcheck's U1000 counts usage from `_test.go` files (measured
before this plan was written), and every calc function has an untagged test from
Task 1. Adding blank references here would be noise that Tasks 3 and 4 then have
to unpick.

- [ ] **Step 2: Narrow and correct `sampler_other.go`**

Change its build tag from `//go:build !linux` to `//go:build !linux && !windows`, and replace the FIRST bullet of its doc comment with this. Leave the AV bullet and the `unavailable`-is-first-class paragraph exactly as they are.

```go
//   - `cpu.busy_pct` and `memory.used_pct` on macOS come from Mach calls
//     (`host_statistics64`) that conventionally require cgo. `sysctl` IS
//     reachable without cgo through x/sys/unix and does not expose them. The
//     agent is a single static binary with no cgo (CLAUDE.md's agent
//     conventions), and a shell-out on the fast tick is the shape §0.5
//     falsifier 3 forbids -- so this needs a real implementation, and has not
//     had one yet.
//
//     (Superseded 2026-08-14, quoted rather than deleted so the correction is
//     legible: this bullet used to say the same of WINDOWS, and described its
//     counters as "performance counters or WMI". Both halves were wrong.
//     Windows is answered by sampler_windows.go as of the Windows fast-tick
//     samplers cycle, and it needed neither performance counters nor WMI:
//     GetSystemTimes and GlobalMemoryStatusEx are plain kernel32 calls,
//     reachable from a cgo-free binary.)
```

- [ ] **Step 3: Verify it compiles for every target**

Run: `cd /home/user/Toeshee-RMS/agent && gofmt -l . && go vet ./... && GOOS=windows go vet ./... && GOOS=darwin go vet ./... && /root/go/bin/staticcheck ./... && GOOS=windows go test -c -o /dev/null ./internal/capabilities/health/`
Expected: all silent/clean. `GOOS=windows go vet` is what type-checks this file at all.

- [ ] **Step 4: Confirm Linux is unchanged and still green**

Run: `git diff --stat agent/internal/capabilities/health/sampler_linux.go && go test ./internal/capabilities/health/ -count=1`
Expected: no diff for `sampler_linux.go`; tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent/internal/capabilities/health/sampler_windows.go \
        agent/internal/capabilities/health/sampler_other.go
git commit -m "feat(agent/health): Windows fast-tick sampler, cpu and memory"
```

---

### Task 3: Disk free, including the fixed-drive filter

**Files:**
- Modify: `agent/internal/capabilities/health/sampler_windows.go`

**Interfaces:**
- Consumes: `freeSample`, `splitDriveStrings` (Task 1); `selectorIsWildcard`.
- Produces: a real `(*hostSampler).diskFree(selector string, asPct bool) Sample`.

**Read first:** `sampler_linux.go`'s `diskFree`, `statfsSample` and `worstMount`. Match their semantics exactly: wildcard → worst, empty → the platform default root, missing path → `unavailable` and never zero.

- [ ] **Step 1: Replace the `diskFree` stub**

Add `"strings"` to the file's imports — this is the first code in it that needs
`strings.TrimSpace`.

```go
// diskFree answers disk.free_pct and disk.free_bytes.
//
// Selector contract, matching sampler_linux.go: a wildcard means the worst
// drive, empty means the system drive (Linux's equivalent default is "/"),
// and anything else is the path as given.
func (h *hostSampler) diskFree(selector string, asPct bool) Sample {
	if selectorIsWildcard(selector) {
		return worstFixedDrive(asPct)
	}
	path := strings.TrimSpace(selector)
	if path == "" {
		// Resolved from the API rather than %SystemDrive%, which is an
		// ordinary environment variable a user process can set to anything.
		dir, err := windows.GetWindowsDirectory()
		if err != nil {
			return unavailable()
		}
		path = dir
	}
	return diskSample(path, asPct)
}

// diskSample reads one path.
//
// FreeBytesAvailableToCaller mirrors Linux's statfs Bavail -- what an
// unprivileged writer can actually use, not the raw free count.
//
// A path that does not resolve is `unavailable`, NEVER zero. Reporting 0 free
// bytes would open a critical alert for a volume that was merely unmounted or
// ejected, which is the rule statfsSample already states on Linux.
//
// One honest asymmetry with Linux, recorded rather than engineered around:
// Linux's total is the whole filesystem (Blocks x Bsize) while
// TotalNumberOfBytes here is quota-aware, so on a machine with disk quotas
// the two platforms answer subtly different questions.
func diskSample(path string, asPct bool) Sample {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return unavailable()
	}
	var freeToCaller, total, totalFree uint64
	if err := windows.GetDiskFreeSpaceEx(p, &freeToCaller, &total, &totalFree); err != nil {
		return unavailable()
	}
	return freeSample(freeToCaller, total, asPct)
}

// worstFixedDrive is the wildcard case: the lowest value of the metric being
// asked for, across fixed drives only.
//
// "Worst" is per-metric, so disk.free_pct and disk.free_bytes can legitimately
// name DIFFERENT drives on the same machine -- a small nearly-full volume
// versus a large one with more slack. Each check answers its own question.
//
// DRIVE_FIXED is what stops a mounted ISO, a network share or a USB stick
// from opening a disk-free alert on a healthy machine.
func worstFixedDrive(asPct bool) Sample {
	// 26 drive letters at 4 UTF-16 units each, plus the terminator, fits far
	// inside this. A buffer too small makes GetLogicalDriveStrings return the
	// REQUIRED length instead of writing, which the length check below reads
	// as unavailable rather than as an empty drive list.
	var buf [512]uint16
	n, err := windows.GetLogicalDriveStrings(uint32(len(buf)), &buf[0])
	if err != nil || n == 0 || int(n) > len(buf) {
		return unavailable()
	}
	worst := unavailable()
	for _, root := range splitDriveStrings(buf[:n]) {
		p, err := windows.UTF16PtrFromString(root)
		if err != nil {
			continue
		}
		if windows.GetDriveType(p) != windows.DRIVE_FIXED {
			continue
		}
		got := diskSample(root, asPct)
		if !got.Available {
			// An unreadable fixed drive is skipped, not treated as zero free.
			continue
		}
		if !worst.Available || got.Num < worst.Num {
			worst = got
		}
	}
	return worst
}
```

- [ ] **Step 2: Verify**

Run: `cd /home/user/Toeshee-RMS/agent && gofmt -l . && GOOS=windows go vet ./... && /root/go/bin/staticcheck ./... && GOOS=windows go test -c -o /dev/null ./internal/capabilities/health/ && go test ./internal/capabilities/health/ -count=1`
Expected: all clean.

- [ ] **Step 3: Commit**

```bash
git add agent/internal/capabilities/health/sampler_windows.go
git commit -m "feat(agent/health): Windows disk.free_pct and disk.free_bytes"
```

---

### Task 4: Process presence

**Files:**
- Modify: `agent/internal/capabilities/health/sampler_windows.go`

**Interfaces:**
- Consumes: `normaliseProcessName`, `processNameMatches` (Task 1).
- Produces: a real `(*hostSampler).processPresent(selector string) Sample`.

- [ ] **Step 1: Replace the `processPresent` stub**

```go
// processPresent reports whether a process with that name is running.
//
// THE SELECTOR IS MATCHED AGAINST AN EXECUTABLE NAME AND NEVER A COMMAND
// LINE. §5.1: a command line never travels on this wire, and sampler_linux.go
// records why -- "a check that matched one would be a check whose selector
// could be used to ask 'is anyone running this argument'", which turns a
// health check into a fleet-wide interrogation primitive.
// PROCESSENTRY32.szExeFile is a name, so the Toolhelp route satisfies that by
// construction. WMI's Win32_Process.CommandLine does NOT, and is out of
// bounds here however convenient it looks.
//
// A process that is genuinely absent is a real answer (Available, false), not
// a missing one. Only a failed enumeration or an empty selector is
// `unavailable`.
func (h *hostSampler) processPresent(selector string) Sample {
	want := normaliseProcessName(selector)
	if want == "" {
		return unavailable()
	}
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return unavailable()
	}
	// Closed on EVERY path, including the error returns below: a leaked
	// snapshot handle per tick is a handle leak on a process that is supposed
	// to be invisible when nothing is happening.
	defer func() { _ = windows.CloseHandle(snap) }()

	var e windows.ProcessEntry32
	// Same class of trap as MEMORYSTATUSEX.dwLength: Process32First fails
	// unless Size is the struct's own size.
	e.Size = uint32(unsafe.Sizeof(e))
	if err := windows.Process32First(snap, &e); err != nil {
		return unavailable()
	}
	for {
		if processNameMatches(windows.UTF16ToString(e.ExeFile[:]), want) {
			return Sample{Available: true, Bool: true}
		}
		if err := windows.Process32Next(snap, &e); err != nil {
			break
		}
	}
	return Sample{Available: true, Bool: false}
}
```

- [ ] **Step 2: Verify**

Run: `cd /home/user/Toeshee-RMS/agent && gofmt -l . && go vet ./... && GOOS=windows go vet ./... && GOOS=darwin go vet ./... && /root/go/bin/staticcheck ./... && GOOS=windows go test -c -o /dev/null ./internal/capabilities/health/ && go test ./internal/capabilities/health/ -count=1`
Expected: all clean.

- [ ] **Step 3: Commit**

```bash
git add agent/internal/capabilities/health/sampler_windows.go
git commit -m "feat(agent/health): Windows process.present via Toolhelp, never a command line"
```

---

### Task 5: The never-run falsifier, and one test that quietly changed meaning

**Files:**
- Create: `agent/internal/capabilities/health/sampler_windows_test.go`
- Modify: `agent/internal/capabilities/health/budget_test.go` (comment only)

- [ ] **Step 1: Write the Windows-tagged tests**

```go
//go:build windows

package health

import (
	"testing"
	"unsafe"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// THESE TESTS COMPILE AND HAVE NEVER RUN.
//
// `GOOS=windows go vet ./...` and `GOOS=windows go test -c` type-check and
// link them on this project's Linux CI; no machine here can execute them.
// They exist so that the day someone runs this binary on real Windows
// hardware, the falsifier is already sitting there to run -- NOT so that
// anything may be described as verified today.
//
// CLAUDE.md's complaint about this module's original six falsifiers was not
// that they were unrun. It was that they were unrun WHILE BEING CITED AS
// VERIFICATION. Do not cite these.

func TestNewMemoryStatusExSetsItsOwnLength(t *testing.T) {
	// GlobalMemoryStatusEx fails unless dwLength is the struct size. Confining
	// that to one constructor is the mitigation; this is the check on the
	// constructor.
	m := newMemoryStatusEx()
	if got, want := m.Length, uint32(unsafe.Sizeof(m)); got != want {
		t.Fatalf("newMemoryStatusEx().Length = %d, want %d", got, want)
	}
}

func TestWindowsFalsifier_TheRealSamplerAccumulatesNothing(t *testing.T) {
	// The Windows twin of falsifier 2c. Retained heap must stay flat across a
	// hundredfold spread in tick count -- the property that decides whether a
	// device is still healthy after a week.
	//
	// FALSIFYING OBSERVATION: retained bytes scaling with the tick count.
	const few, many = 100, 10_000
	tick := func() func() {
		s := NewHostSampler()
		return func() {
			_ = s.Sample("cpu.busy_pct", "")
			_ = s.Sample("memory.used_pct", "")
		}
	}
	small := budget.Retained(few, tick)
	large := budget.Retained(many, tick)
	const allowance = 32 << 10
	if delta := large - small; delta > allowance {
		t.Fatalf("windows sampler: %d ticks retained %d bytes more than %d (allowance %d)",
			many, delta, few, allowance)
	}
	t.Logf("windows sampler: %d ticks = %d bytes, %d ticks = %d bytes, delta = %d",
		few, small, many, large, large-small)
}
```

- [ ] **Step 2: Correct the untagged falsifier's comment**

`TestFalsifier2_TheRealHostSamplerAccumulatesNothingEither` in `budget_test.go` is untagged and calls `NewHostSampler()`. Before this cycle it exercised a do-nothing stub on every non-Linux platform, so its own framing — "The real host sampler is NOT allocation-free" — was false there and it passed trivially. It is now meaningful on Windows and still cannot run there.

Add this paragraph to that test's comment, after the existing text. **Do not change its assertions.**

```go
	// WHAT THIS MEASURES DEPENDS ON THE PLATFORM, and that changed under this
	// test without a line of it being edited. On Linux it drives the real
	// /proc-reading sampler, and is genuine evidence. Before the Windows
	// sampler existed it drove a do-nothing stub everywhere else -- so the
	// sentence above ("The real host sampler is NOT allocation-free") was
	// false there and this assertion passed trivially. Windows now has a real
	// sampler, which makes this meaningful there and still unable to run: no
	// machine on this project executes Windows. Its twin -- which also
	// compiles and has also never run -- is
	// TestWindowsFalsifier_TheRealSamplerAccumulatesNothing in
	// sampler_windows_test.go.
```

- [ ] **Step 3: Verify**

Run: `cd /home/user/Toeshee-RMS/agent && gofmt -l . && GOOS=windows go vet ./... && GOOS=windows go test -c -o /dev/null ./internal/capabilities/health/ && go test ./internal/capabilities/health/ -count=1`
Expected: clean; the Linux run is unaffected.

- [ ] **Step 4: Commit**

```bash
git add agent/internal/capabilities/health/sampler_windows_test.go \
        agent/internal/capabilities/health/budget_test.go
git commit -m "test(agent/health): the Windows budget falsifier, compiled and never run"
```

---

### Task 6: CI, and the docs that still say Windows answers nothing

**Files:**
- Modify: `.github/workflows/ci.yml` (the `windows service code compiles` step, ~line 323)
- Modify: `docs/modules/endpoint-health-alerting/spec.md` (§4.1's table and §0.7)

- [ ] **Step 1: Link the health test binary for Windows in CI**

The existing step runs `GOOS=windows go vet ./...` and `GOOS=windows go test -c -o /dev/null ./internal/service`. `go test -c` takes ONE package, so add a second line for health beside it, and extend the step's name and comment to say it now covers the health sampler too:

```yaml
          GOOS=windows go vet ./...
          GOOS=windows go test -c -o /dev/null ./internal/service
          GOOS=windows go test -c -o /dev/null ./internal/capabilities/health
```

- [ ] **Step 2: Update §4.1's table**

The `windows` column for `cpu.busy_pct`, `memory.used_pct`, `disk.free_pct`, `disk.free_bytes` and `process.present` currently says these are unanswered. Change them to answered, and add a note directly under the table stating in plain words that the Windows column is **written and compile-checked, never executed** — no machine on this project can run it — and that the numbers it produces have never been compared against Task Manager or `wmic`. Follow the file's existing correction convention: keep the superseded text quoted and dated, matching a neighbouring correction's format exactly.

- [ ] **Step 3: Check nothing still claims Windows is silent**

Run: `grep -rn "unavailable" docs/modules/endpoint-health-alerting/spec.md | grep -i windows`
Read each hit and correct any that is now false. Report what you found and what you changed, including any you judged still correct and why.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml docs/modules/endpoint-health-alerting/spec.md
git commit -m "ci+docs(health): link the Windows health test binary; §4.1 answers five checks"
```
