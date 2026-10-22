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

// joinFiletime joins a FILETIME's two 32-bit halves into the 64-bit count of
// 100-nanosecond ticks it represents.
//
// This is arithmetic, so it lives HERE rather than beside the syscall that
// produces it: swapping the halves is a one-character mistake that no
// compiler, vet, or cross-compile check can see, and on Windows it would make
// every CPU counter land about 2^32 out. `idle > kernel+user` then becomes
// routine, windowsCPUBusyTotal answers (0, 0), and cpu.busy_pct reports
// `unavailable` forever on every Windows endpoint -- a check that never fires
// rather than one that fires wrongly, which is the harder kind to notice.
func joinFiletime(low, high uint32) uint64 {
	return uint64(high)<<32 | uint64(low)
}

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
		//
		// A DELIBERATE DIVERGENCE FROM sampler_linux.go, recorded here so a
		// future reader comparing the two platforms does not read it as an
		// accident. Linux's cpuBusyPct has no equivalent guard: without one, a
		// busy counter that regresses between ticks underflows the uint64
		// subtraction above and reports a nonsense percentage in the billions.
		// Windows gets the guard because its counters come from a different
		// source and this file is the only place either platform's arithmetic
		// is executed. Adding it to Linux would mean editing a sampler whose
		// retained-heap falsifier is currently measured, which this cycle
		// deliberately does not do.
		return unavailable()
	}
	return Sample{Available: true, Num: 100 * float64(db) / float64(dt)}
}

// usedPctFromBytes answers memory.used_pct from a total and an available.
//
// A DOCUMENTED CROSS-PLATFORM DIVERGENCE, in the same register as
// cpuBusyDelta's `db > dt` comment above: sampler_linux.go's memoryUsedPct
// CLAMPS an `available > total` reading down to total and answers an
// available 0% used -- so an inconsistent /proc/meminfo read can CLEAR a live
// memory alert. This function instead refuses the same input as
// `unavailable`, leaving any existing alert exactly where it was. Windows'
// behaviour here is the better one -- an inconsistent reading should not by
// itself clear an alert -- and this comment exists so the difference is
// recorded rather than left for someone to discover by diffing the two files.
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
//
// `free > total` is refused in BOTH modes, and that symmetry is not cosmetic.
// Both numbers come from one GetDiskFreeSpaceEx call, so a free count
// exceeding the total means the read is internally inconsistent -- and a
// garbage byte count reported as Available is worse in bytes mode than in
// percent mode, because a threshold on free_bytes compares against it
// directly and could CLEAR a real alert.
func freeSample(free, total uint64, asPct bool) Sample {
	if free > total {
		return unavailable()
	}
	if !asPct {
		return Sample{Available: true, Num: float64(free)}
	}
	if total == 0 {
		return unavailable()
	}
	return Sample{Available: true, Num: 100 * float64(free) / float64(total)}
}

// worseSample returns whichever of two samples is the worse reading, treating
// an unavailable sample as "no reading yet" rather than as zero.
//
// It exists so the wildcard disk selector's choice is made in a file that
// Linux CI executes. In sampler_windows.go the same comparison is one
// character away from selecting the BEST drive and reporting a healthy fleet,
// with no test anywhere able to notice.
//
// TWO MORE DOCUMENTED CROSS-PLATFORM DIVERGENCES in what the wildcard `*`
// selector actually covers, same register as cpuBusyDelta's `db > dt`
// comment: neither is a bug, both are worth a reader knowing before they
// author a wildcard disk policy and assume it means the same thing on both
// platforms.
//
//   - `*` means different filesystem types. sampler_linux.go's
//     realFilesystems allow-list includes vfat/exfat/ntfs, so a nearly-full
//     USB stick is a live candidate for disk.free_pct on Linux's `*`.
//     worstFixedDrive's DRIVE_FIXED filter on Windows deliberately excludes
//     exactly that removable-media case. One selector, opposite behaviour.
//   - `*` on Windows enumerates drive letters only. worstFixedDrive walks
//     GetLogicalDriveStrings, which cannot see a volume mounted at a folder
//     path (e.g. C:\Data, ordinary on servers) -- such a volume can fill to
//     zero without ever alerting. Linux's `*` walks /proc/mounts and sees
//     every mount, folder-mounted or not. This is the divergence most likely
//     to bite in practice, because a wildcard disk policy is the one most
//     fleets actually author.
//   - `*` ON macOS SEES FOLDER-MOUNTED VOLUMES BUT DOUBLE-COUNTS FIRMLINKS.
//     worstLocalVolume walks getfsstat, which reports `/` and
//     `/System/Volumes/Data` as separate entries backed by ONE physical store,
//     so a Mac's wildcard legitimately reports the same free space twice.
//     Harmless here -- worseSample takes a minimum, and the same number twice
//     yields that number -- and recorded so it is not read as a bug by someone
//     comparing volume counts against `df`. Note this is the exact OPPOSITE
//     blind spot to Windows: macOS sees folder-mounted volumes that Windows
//     cannot, and pays for it by seeing some stores more than once.
func worseSample(worst, got Sample) Sample {
	if !got.Available {
		return worst
	}
	if !worst.Available || got.Num < worst.Num {
		return got
	}
	return worst
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
// PROCESS NAMES TRUNCATE AT DIFFERENT LENGTHS, and this is the divergence most
// likely to produce a silent never-fires. macOS `P_comm` gives 16 characters,
// Linux `comm` 15, Windows `szExeFile` the full name with `.exe`. A policy
// naming a longer executable matches on Windows and NEVER on macOS or Linux --
// and the symptom is an alert that simply never opens, which nobody
// investigates, rather than an error anyone sees. Choose selectors that fit in
// 15 characters for a mixed fleet.
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
