//go:build windows

package health

import (
	"strings"
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
//
// Delegates to joinFiletime in sampler_calc.go: the join itself is arithmetic,
// and that file is the one Linux CI actually executes. This wrapper stays
// only because the three call sites read better passing a windows.Filetime
// than unpacking it themselves.
func filetimeToUint64(ft windows.Filetime) uint64 {
	return joinFiletime(ft.LowDateTime, ft.HighDateTime)
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
	// This is a call-failure check, not a decision: each disjunct rejects one
	// way the syscall itself failed to hand back a usable buffer (an error,
	// nothing written, or the "buffer too small so I returned the required
	// length instead" case noted above) -- there is no comparison here between
	// two readings for worseSample to own. It stays beside the syscall it
	// guards rather than moving to sampler_calc.go.
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
		// worseSample (sampler_calc.go) owns the comparison, including
		// treating an unavailable got as "no reading" rather than zero, so
		// the loop body carries no decision of its own.
		worst = worseSample(worst, diskSample(root, asPct))
	}
	return worst
}

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
