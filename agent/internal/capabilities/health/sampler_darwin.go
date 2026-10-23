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
// assembly trampoline per symbol (`zsyscall_darwin_arm64.s` defines
// `libc_getgroups_trampoline` and the `_addr` global the Go side calls
// through). A probe was built using those pragmas for `host_statistics64` with
// the assembly OMITTED ENTIRELY -- no `.s` file in the module -- and
// `GOOS=darwin go build` exited 0. On a real Mac that code calls through a null
// function pointer.
//
// So on this platform a green cross-compile survives code that cannot execute.
// The Windows cycle could put its arithmetic in an untagged file and genuinely
// test the part most likely to be wrong; a hand-rolled Mach binding has no such
// half -- the MECHANISM is what would be wrong (pragma spelling, trampoline
// symbol names, the `_addr` global, argument marshalling), and nothing
// available here can see it. Two checks shipped on that basis would report
// `unavailable` forever on every Mac while LOOKING finished, which is strictly
// worse than refusing: `unavailable` is a first-class visible state (§0.7), and
// a check that silently never fires is the harder kind to notice.
//
// WHAT WOULD UNBLOCK THEM: a macOS runner in CI, at which point a hand-rolled
// binding becomes testable and is a reasonable cycle; or a deliberate decision
// to enable cgo for darwin, which is a build-system change (cross-compiling cgo
// for darwin needs an osxcross toolchain) owned by whoever owns the release
// pipeline. Not a sampler cycle's call either way.
//
// NOTHING IN THIS FILE HAS EVER RUN. There is no macOS machine in this
// project's CI. `GOOS=darwin go vet` and `GOOS=darwin go test -c` are the only
// checks, and the probe above is exactly why neither is evidence of
// correctness. What IS verified is the calc layer this file calls into --
// statfsFreeBytes, cstr, freeSample, worseSample, processNameMatches -- which
// Linux CI executes.

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
		// Mach. See the file header -- this is a RECORDED REFUSAL, not a gap
		// somebody forgot to fill, and the two cases are named explicitly
		// rather than falling through so that a reader can tell the difference
		// between "not implemented" and "not a check this platform knows".
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
//     forever -- an alert that fires on every endpoint says nothing about any
//     of them.
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
	// `n` is re-read from the second call and clamped: a volume MOUNTED between
	// the two calls means the kernel has more to report than the buffer holds,
	// and one UNMOUNTED means the second answer is smaller. Slicing to the
	// first count would read zero-valued entries and feed a bogus 0 into
	// worseSample, which takes a minimum -- so the wildcard would report 0%
	// free on any machine that happened to unmount something mid-tick.
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
	// root-reserved pool and overstates what is actually usable, which on a
	// volume just under a threshold is enough to hold a real alert closed.
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
//
// An empty selector is unanswerable rather than false -- the same contract the
// other platforms keep, so a misconfigured policy reads as `unavailable` in the
// console instead of quietly asserting the process is absent everywhere.
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
