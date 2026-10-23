//go:build linux

package inventory

import (
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// The linux /proc parsers -- these DO run on the target platform here
// ---------------------------------------------------------------------------

func TestLinuxProcParsers(t *testing.T) {
	t.Run("os-release prefers PRETTY_NAME", func(t *testing.T) {
		name, version := osRelease("NAME=\"Ubuntu\"\nVERSION=\"24.04.4 LTS\"\nPRETTY_NAME=\"Ubuntu 24.04.4 LTS\"\n")
		if name != "Ubuntu 24.04.4 LTS" || version != "" {
			t.Fatalf("osRelease = %q, %q", name, version)
		}
	})
	t.Run("os-release falls back to NAME and VERSION", func(t *testing.T) {
		name, version := osRelease("NAME=\"Alpine Linux\"\nVERSION_ID=3.20\nVERSION=\"3.20\"\n")
		if name != "Alpine Linux" || version != "3.20" {
			t.Fatalf("osRelease = %q, %q", name, version)
		}
	})
	t.Run("os-release degrades", func(t *testing.T) {
		name, version := osRelease("")
		if name != "" || version != "" {
			t.Fatalf("osRelease(\"\") = %q, %q", name, version)
		}
	})
	t.Run("cpuinfo model name", func(t *testing.T) {
		if got := cpuModel("processor\t: 0\nmodel name\t: 12th Gen Intel(R) Core(TM) i7-1265U\n"); got != "12th Gen Intel(R) Core(TM) i7-1265U" {
			t.Fatalf("cpuModel = %q", got)
		}
	})
	t.Run("cpuinfo on arm has no model name", func(t *testing.T) {
		// aarch64 /proc/cpuinfo carries CPU implementer/part, not a model name.
		// An empty CPU string is expected there, not a bug -- and it must not
		// become a garbage identifier.
		if got := cpuModel("processor\t: 0\nBogoMIPS\t: 50.00\nCPU implementer\t: 0x41\n"); got != "" {
			t.Fatalf("cpuModel = %q; want empty on a cpuinfo with no model line", got)
		}
	})
	t.Run("meminfo is kB", func(t *testing.T) {
		if got := memTotalBytes("MemTotal:       16460996 kB\nMemFree: 1 kB\n"); got != 16460996*1024 {
			t.Fatalf("memTotalBytes = %d", got)
		}
	})
	t.Run("meminfo degrades", func(t *testing.T) {
		for _, raw := range []string{"", "MemTotal:\n", "MemTotal: lots kB\n", "MemFree: 4 kB\n"} {
			if got := memTotalBytes(raw); got != 0 {
				t.Fatalf("memTotalBytes(%q) = %d; want 0", raw, got)
			}
		}
	})
	t.Run("fqdn is never invented", func(t *testing.T) {
		// FALSIFIER: a non-empty answer for a short hostname. A reverse DNS
		// lookup here would make the device's own name a property of whatever
		// resolver the network hands it.
		if got := fqdnOf("ws-0042"); got != "" {
			t.Fatalf("fqdnOf(%q) = %q; a short hostname is not a fully-qualified one", "ws-0042", got)
		}
		if got := fqdnOf("ws-0042.corp.example"); got != "ws-0042.corp.example" {
			t.Fatalf("fqdnOf = %q", got)
		}
	})
	t.Run("readFile treats absence as ordinary", func(t *testing.T) {
		if got := readFile("/proc/definitely-not-a-file"); got != "" {
			t.Fatalf("readFile = %q", got)
		}
		if got := readFile("/proc/sys/kernel/osrelease"); strings.TrimSpace(got) == "" {
			t.Fatal("readFile could not read /proc/sys/kernel/osrelease; this suite's platform claim is wrong")
		}
	})
}
