package enroll

import (
	"net"
	"testing"
)

// TestIsPhysicalMAC covers the filter that decides whether the server may
// AUTO-LINK on an address. Each rejected case is a real source of collisions,
// not a hypothetical: a wrong entry here merges two inventory records.
func TestIsPhysicalMAC(t *testing.T) {
	cases := []struct {
		name string
		addr string
		want bool
	}{
		{"a manufacturer-assigned unicast address", "00:1b:21:aa:bb:cc", true},
		// 0x02 has bit 1 set. This is Docker's default range, and two
		// containers on different hosts can carry the same one.
		{"docker's locally administered address", "02:42:ac:11:00:02", false},
		// QEMU picks 52:54:00 by convention, and it is locally administered
		// for exactly that reason.
		{"qemu's locally administered address", "52:54:00:12:34:56", false},
		{"a multicast address", "01:00:5e:00:00:fb", false},
		{"all zeroes", "00:00:00:00:00:00", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			addr, err := net.ParseMAC(tc.addr)
			if err != nil {
				t.Fatalf("ParseMAC(%q): %v", tc.addr, err)
			}
			if got := isPhysicalMAC(addr); got != tc.want {
				t.Fatalf("isPhysicalMAC(%s) = %v, want %v", tc.addr, got, tc.want)
			}
		})
	}
}

func TestIsPhysicalMACRejectsNonEthernetLengths(t *testing.T) {
	// EUI-64 and InfiniBand addresses are not the six-octet address the server
	// matches on, and truncating one to six octets would invent an identity.
	for _, addr := range []net.HardwareAddr{
		nil,
		{},
		{0x00, 0x1b, 0x21},
		make(net.HardwareAddr, 8),
		make(net.HardwareAddr, 20),
	} {
		if isPhysicalMAC(addr) {
			t.Fatalf("isPhysicalMAC(%v) = true, want false", addr)
		}
	}
}

// TestPhysicalMACsOnThisHost is deliberately weak, and says so. It runs on
// whatever interfaces the test machine happens to have -- in CI, a container
// whose only non-loopback interface is a veth with a locally administered
// address, which is precisely the case the filter removes. So it asserts the
// PROPERTIES that must hold for any result rather than a value.
func TestPhysicalMACsOnThisHost(t *testing.T) {
	got := physicalMACs()
	seen := map[string]bool{}
	for i, address := range got {
		addr, err := net.ParseMAC(address)
		if err != nil {
			t.Fatalf("physicalMACs()[%d] = %q, which does not parse: %v", i, address, err)
		}
		if !isPhysicalMAC(addr) {
			t.Fatalf("physicalMACs() returned %q, which its own filter rejects", address)
		}
		if seen[address] {
			t.Fatalf("physicalMACs() returned %q twice", address)
		}
		seen[address] = true
		if i > 0 && got[i-1] > address {
			t.Fatalf("physicalMACs() is not sorted: %q before %q", got[i-1], address)
		}
	}
}
