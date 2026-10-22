//go:build linux

package netsweep

import (
	"bufio"
	"net/netip"
	"os"
	"strconv"
	"strings"
)

// The Linux neighbour table, read from /proc/net/arp.
//
// See the Neighbours interface for why this rather than an AF_PACKET ARP
// sender: sending an ICMP echo to an on-link IPv4 address makes the KERNEL
// resolve the next hop, so the ARP request and its reply have already happened
// as a consequence of the probe the server authorized. Reading the result emits
// nothing.
//
// This file is deliberately small and total: every parse failure skips a line
// rather than failing the sweep. It sits on the path of a document the server
// rejects WHOLE on any malformation, and a table with one odd row must not cost
// the sweep.

// procNetARP is the file. Named so a test can point at a fixture.
type procNetARP struct{ path string }

func defaultNeighbours() Neighbours { return procNetARP{path: "/proc/net/arp"} }

// Flag bits from linux/if_arp.h. Only ATF_COM (0x02, "complete") means the
// entry carries a MAC that was actually resolved -- an incomplete entry has
// 00:00:00:00:00:00 and means the ARP request went unanswered, which is the
// opposite of the fact this sweep is looking for.
const atfCom = 0x02

func (p procNetARP) Neighbours() (map[netip.Addr]string, error) {
	f, err := os.Open(p.path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	out := map[netip.Addr]string{}
	s := bufio.NewScanner(f)
	s.Scan() // header
	for s.Scan() {
		// IP address | HW type | Flags | HW address | Mask | Device
		fields := strings.Fields(s.Text())
		if len(fields) < 4 {
			continue
		}
		addr, err := netip.ParseAddr(fields[0])
		if err != nil || !addr.Is4() {
			continue
		}
		flags, err := strconv.ParseUint(strings.TrimPrefix(fields[2], "0x"), 16, 32)
		if err != nil || flags&atfCom == 0 {
			continue
		}
		mac := strings.ToLower(fields[3])
		if mac == "" || mac == "00:00:00:00:00:00" {
			continue
		}
		out[addr] = mac
	}
	if err := s.Err(); err != nil {
		return nil, err
	}
	return out, nil
}
