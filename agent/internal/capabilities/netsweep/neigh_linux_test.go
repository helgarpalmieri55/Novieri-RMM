//go:build linux

package netsweep

import (
	"os"
	"path/filepath"
	"testing"
)

// The /proc/net/arp parser's own tests, in a LINUX-TAGGED FILE.
//
// They used to live in netsweep_test.go behind a `runtime.GOOS != "linux"`
// skip, which skips at RUN time and does nothing at COMPILE time: `procNetARP`
// is declared in neigh_linux.go, so `GOOS=windows go vet ./...` failed with
// `undefined: procNetARP` -- and that is a step the agent CI job runs on every
// push, precisely so the windows build cannot rot unnoticed. A runtime skip
// cannot express "this type does not exist on that platform"; a build tag can.
//
// Found while adding the site.wake capability, and fixed here rather than
// carried, because a CI step that is already red stops being read.

func TestTheProcNetARPParserSkipsIncompleteEntriesAndKeepsResolvedOnes(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "arp")
	// Column layout copied from a real /proc/net/arp on this container.
	body := "IP address       HW type     Flags       HW address            Mask     Device\n" +
		"192.0.2.1        0x1         0x2         02:FC:00:00:00:05     *        eth0\n" +
		"192.0.2.9        0x1         0x0         00:00:00:00:00:00     *        eth0\n" +
		"192.0.2.20       0x1         0x2         00:00:00:00:00:00     *        eth0\n" +
		"garbage\n" +
		"2001:db8::1      0x1         0x2         02:00:00:00:00:07     *        eth0\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	table, err := procNetARP{path: path}.Neighbours()
	if err != nil {
		t.Fatal(err)
	}
	if got, want := len(table), 1; got != want {
		t.Fatalf("%d entries, want %d: %v", got, want, table)
	}
	// ATF_COM (0x02) is the only flag that means the MAC was actually
	// RESOLVED. An incomplete entry means the ARP request went unanswered,
	// which is the opposite of the fact this sweep is looking for.
	if got := table[addr(t, "192.0.2.1")]; got != "02:fc:00:00:00:05" {
		t.Fatalf("resolved entry = %q; MACs are lowercased so parse_mac sees one form", got)
	}
}
