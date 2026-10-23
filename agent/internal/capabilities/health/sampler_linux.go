//go:build linux

package health

import (
	"bufio"
	"os"
	"strconv"
	"strings"
	"sync"
	"syscall"
)

// The Linux host sampler (§4.1's table, `linux` column).
//
// WHAT IS ANSWERED HERE AND WHAT IS NOT, stated plainly rather than discovered
// later. v1 answers the four counter-shaped fast-tick checks -- cpu.busy_pct,
// memory.used_pct, disk.free_pct, disk.free_bytes -- plus process.present.
// Everything else reports `unavailable` (§0.7), which is a REAL answer and a
// visible one: it is a first-class state in `device_health_check_state`,
// surfaced in the console and countable. It is not a failure and it never opens
// or clears an alert.
//
//   - `service.state` needs a service-manager integration (systemd over D-Bus,
//     or a `systemctl` shell-out on the FAST tick). §0.5 falsifier 3 names the
//     `systemctl` shell-out as the shape that breaks the idle claim, and §4.1
//     puts this check on the fast cadence. Answering it properly needs the
//     integration, not the shell-out, and that is a piece of work with its own
//     footprint argument. Reporting `unavailable` is the honest state until
//     then.
//   - every `posture.*` and `disk.smart_ok` check is answered by the POSTURE
//     sampler, which is a separate object for exactly the reason §0.5
//     falsifier 3 exists. (Superseded 2026-08-13: this used to end "v1 has no
//     posture probes; see postureSampler." That is no longer true on this
//     platform -- posture_linux.go wires posture.disk_encryption,
//     posture.firewall_enabled and disk.smart_ok to real probes in
//     posture_linux_probes.go, exercised against the actual
//     findmnt/lsblk/nft/iptables in posture_linux_integration_test.go. See
//     postureSampler and the per-OS posture_*.go files for what each platform
//     answers and what stays `unavailable`.)
//   - every `eventlog.*` check never reaches a sampler at all (§0.5 item 5).
//
// # Allocation
//
// This runs on the fast tick, so it reads with a REUSED buffer and a reused
// bufio.Scanner rather than os.ReadFile. It is not allocation-free -- os.Open
// allocates an *os.File per call -- and internal/budget's zero-allocation
// assertion is therefore made against the tick ENGINE with a fake sampler, and
// stated as such. What IS asserted for this sampler is falsifier 2: the
// retained heap must be flat across a hundredfold spread in tick count, which
// is the property that decides whether a device is still healthy after a week.

// hostSampler answers the fast-cadence checks. It holds the previous CPU
// counters and nothing else: no cache of samples, no history (§0.12).
type hostSampler struct {
	mu        sync.Mutex
	buf       []byte
	haveCPU   bool
	prevBusy  uint64
	prevTotal uint64
}

// NewHostSampler returns the platform's fast-tick sampler.
//
// It allocates its read buffer lazily, on first use, so constructing one during
// wiring costs nothing.
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

// scratch returns the reused read buffer.
func (h *hostSampler) scratch() []byte {
	if h.buf == nil {
		h.buf = make([]byte, 8192)
	}
	return h.buf
}

// readInto reads a small /proc file into the reused buffer.
//
// os.ReadFile would allocate a fresh slice per tick, per check, forever. The
// file is read in one Read: /proc/stat's first line and /proc/meminfo's first
// few lines are what this needs, and both are well inside 8 KiB.
func (h *hostSampler) readInto(path string) ([]byte, bool) {
	f, err := os.Open(path)
	if err != nil {
		return nil, false
	}
	defer f.Close()
	buf := h.scratch()
	n, err := f.Read(buf)
	if n <= 0 || (err != nil && n == 0) {
		return nil, false
	}
	return buf[:n], true
}

// cpuBusyPct is the busy fraction since the PREVIOUS tick.
//
// The first sample after an install has no previous counters and reports
// `unavailable` rather than a number computed against boot -- "43% since the
// machine started" is not the fact the check asks for, and reporting it would
// make the first tick after every policy push a plausible-looking lie.
func (h *hostSampler) cpuBusyPct() Sample {
	raw, ok := h.readInto("/proc/stat")
	if !ok {
		return unavailable()
	}
	line := raw
	if i := indexByte(line, '\n'); i >= 0 {
		line = line[:i]
	}
	fields := strings.Fields(string(line))
	if len(fields) < 5 || fields[0] != "cpu" {
		return unavailable()
	}
	var total, idle uint64
	for i, f := range fields[1:] {
		v, err := strconv.ParseUint(f, 10, 64)
		if err != nil {
			return unavailable()
		}
		total += v
		// Fields 4 and 5 (idle, iowait) are the not-busy halves.
		if i == 3 || i == 4 {
			idle += v
		}
	}
	busy := total - idle
	prevBusy, prevTotal, had := h.prevBusy, h.prevTotal, h.haveCPU
	h.prevBusy, h.prevTotal, h.haveCPU = busy, total, true
	if !had || total <= prevTotal {
		return unavailable()
	}
	dt := total - prevTotal
	db := busy - prevBusy
	return Sample{Available: true, Num: 100 * float64(db) / float64(dt)}
}

func (h *hostSampler) memoryUsedPct() Sample {
	raw, ok := h.readInto("/proc/meminfo")
	if !ok {
		return unavailable()
	}
	var total, available uint64
	var haveTotal, haveAvail bool
	s := bufio.NewScanner(strings.NewReader(string(raw)))
	for s.Scan() {
		fields := strings.Fields(s.Text())
		if len(fields) < 2 {
			continue
		}
		v, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil {
			continue
		}
		switch fields[0] {
		case "MemTotal:":
			total, haveTotal = v, true
		case "MemAvailable:":
			available, haveAvail = v, true
		}
		if haveTotal && haveAvail {
			break
		}
	}
	if !haveTotal || !haveAvail || total == 0 {
		return unavailable()
	}
	if available > total {
		available = total
	}
	return Sample{Available: true, Num: 100 * float64(total-available) / float64(total)}
}

// diskFree answers disk.free_pct and disk.free_bytes.
//
// `*` means "worst" (§4.1's selector column): the mount with the least free
// space by whichever measure is being asked for. An empty selector is treated
// as `/`, because a check with no selector still has to mean something and the
// root filesystem is the one every Linux host has.
func (h *hostSampler) diskFree(selector string, asPct bool) Sample {
	if selectorIsWildcard(selector) {
		return h.worstMount(asPct)
	}
	path := selector
	if strings.TrimSpace(path) == "" {
		path = "/"
	}
	return statfsSample(path, asPct)
}

func statfsSample(path string, asPct bool) Sample {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		// A mount point that is not there is `unavailable`, not zero free
		// bytes. Reporting 0 would open a critical alert for a volume that was
		// simply unmounted.
		return unavailable()
	}
	free := uint64(st.Bavail) * uint64(st.Bsize)
	if !asPct {
		return Sample{Available: true, Num: float64(free)}
	}
	total := uint64(st.Blocks) * uint64(st.Bsize)
	if total == 0 {
		return unavailable()
	}
	return Sample{Available: true, Num: 100 * float64(free) / float64(total)}
}

// realFilesystems is an ALLOW list, not a deny list.
//
// A deny list of pseudo-filesystems is a list that goes stale every kernel
// release, and the failure is a `disk.free_pct` check on `*` that reports
// tmpfs's 0% and opens a critical alert on every device in the fleet. An
// unknown filesystem is skipped, which costs a check on an exotic mount and
// never costs a false alert.
var realFilesystems = map[string]bool{
	"ext2": true, "ext3": true, "ext4": true, "xfs": true, "btrfs": true,
	"zfs": true, "f2fs": true, "jfs": true, "reiserfs": true,
	"vfat": true, "exfat": true, "ntfs": true, "ntfs3": true,
}

func (h *hostSampler) worstMount(asPct bool) Sample {
	f, err := os.Open("/proc/mounts")
	if err != nil {
		return unavailable()
	}
	defer f.Close()
	worst := Sample{}
	s := bufio.NewScanner(f)
	for s.Scan() {
		fields := strings.Fields(s.Text())
		if len(fields) < 3 || !realFilesystems[fields[2]] {
			continue
		}
		got := statfsSample(fields[1], asPct)
		if !got.Available {
			continue
		}
		if !worst.Available || got.Num < worst.Num {
			worst = got
		}
	}
	return worst
}

// processPresent reports whether a process with that name is running.
//
// The selector is matched against /proc/<pid>/comm, which is the kernel's own
// 15-character name for the process -- never against a command line. §5.1: a
// command line never travels on this wire, and a check that matched one would
// be a check whose selector could be used to ask "is anyone running this
// argument".
func (h *hostSampler) processPresent(selector string) Sample {
	name := strings.TrimSpace(selector)
	if name == "" {
		return unavailable()
	}
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return unavailable()
	}
	for _, e := range entries {
		if !e.IsDir() || e.Name()[0] < '0' || e.Name()[0] > '9' {
			continue
		}
		comm, ok := h.readInto("/proc/" + e.Name() + "/comm")
		if !ok {
			continue
		}
		if strings.TrimSpace(string(comm)) == name {
			return Sample{Available: true, Bool: true}
		}
	}
	return Sample{Available: true, Bool: false}
}

func indexByte(b []byte, c byte) int {
	for i := range b {
		if b[i] == c {
			return i
		}
	}
	return -1
}
