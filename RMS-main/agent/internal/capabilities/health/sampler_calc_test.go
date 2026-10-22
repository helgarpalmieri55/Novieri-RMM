package health

import (
	"testing"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

func TestJoinFiletime(t *testing.T) {
	// THE TRAP THIS TEST EXISTS FOR: swapping the two arguments (low, high)
	// compiles cleanly and would land every Windows CPU/time counter about
	// 2^32 out. Cases are chosen so a swap is unambiguous: one where high and
	// low differ, and one where only low is set (a swap there would silently
	// produce zero for a genuinely populated low half).
	cases := []struct {
		name      string
		low, high uint32
		want      uint64
	}{
		{"high and low differ", 0x00000001, 0x00000002, 0x0000000200000001},
		{"only low is set", 0xFFFFFFFF, 0, 0x00000000FFFFFFFF},
		{"only high is set", 0, 0x00000001, 0x0000000100000000},
		{"both zero", 0, 0, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := joinFiletime(c.low, c.high); got != c.want {
				t.Fatalf("joinFiletime(low=%#x, high=%#x) = %#x, want %#x",
					c.low, c.high, got, c.want)
			}
		})
	}
}

func TestWorseSample(t *testing.T) {
	avail := func(n float64) Sample { return Sample{Available: true, Num: n} }
	na := unavailable()
	cases := []struct {
		name       string
		worst, got Sample
		want       Sample
	}{
		{"both unavailable", na, na, na},
		{"only got available", na, avail(10), avail(10)},
		{"only worst available", avail(10), na, avail(10)},
		{"got worse (lower)", avail(50), avail(10), avail(10)},
		{"got better (higher)", avail(10), avail(50), avail(10)},
		{"equal values keep worst", avail(10), avail(10), avail(10)},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := worseSample(c.worst, c.got)
			if got.Available != c.want.Available {
				t.Fatalf("worseSample(%+v, %+v).Available = %v, want %v",
					c.worst, c.got, got.Available, c.want.Available)
			}
			if got.Available && got.Num != c.want.Num {
				t.Fatalf("worseSample(%+v, %+v).Num = %v, want %v",
					c.worst, c.got, got.Num, c.want.Num)
			}
		})
	}
}

func TestWindowsCPUBusyTotal(t *testing.T) {
	// THE TRAP THIS TEST EXISTS FOR: on Windows, kernel time ALREADY INCLUDES
	// idle time. Computing busy as kernel+user counts every idle tick as busy
	// and reports plausible, permanently inflated CPU on every endpoint.
	// Anyone "simplifying" this turns this test red.
	cases := []struct {
		name                string
		idle, kernel, user  uint64
		wantBusy, wantTotal uint64
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
		name                             string
		prevBusy, prevTotal, busy, total uint64
		had                              bool
		wantAvailable                    bool
		wantNum                          float64
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
	// An internally inconsistent read is refused in BOTH modes. Both numbers
	// come from one GetDiskFreeSpaceEx call, so free exceeding total means the
	// read is garbage -- and in bytes mode a threshold would compare against
	// that number directly, so reporting it could CLEAR a real alert.
	if got := freeSample(8192, 4096, false); got.Available {
		t.Fatalf("bytes mode with free > total = %+v, want unavailable", got)
	}
	if got := freeSample(8192, 4096, true); got.Available {
		t.Fatalf("percent mode with free > total = %+v, want unavailable", got)
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

// TestTheAllocationFreeCalcFunctionsAllocateNothing is the measurable half of
// this cycle's idle-budget claim. The syscalls cannot run on this runner; the
// arithmetic they feed can, and it runs on the FAST tick, so it must not
// allocate.
//
// This covers 5 of the 7 functions in sampler_calc.go. It deliberately
// EXCLUDES two that allocate by nature and are not claimed to be
// allocation-free:
//   - splitDriveStrings allocates a []string plus a string per drive, on
//     every wildcard disk sample.
//   - normaliseProcessName's strings.ToLower allocates whenever the selector
//     is not already lowercase.
//
// A green run here is a claim about the five functions actually exercised
// below, not about "the calc layer" as a whole -- do not read it that way.
func TestTheAllocationFreeCalcFunctionsAllocateNothing(t *testing.T) {
	budget.AssertNoAllocations(t, "windows sampler calc", func() func() {
		return func() {
			busy, total := windowsCPUBusyTotal(30, 80, 20)
			_ = cpuBusyDelta(10, 50, busy, total, true)
			_ = usedPctFromBytes(1<<30, 1<<29)
			_ = freeSample(1<<28, 1<<30, true)
			_ = processNameMatches("sshd.exe", "sshd")
		}
	})
}

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
