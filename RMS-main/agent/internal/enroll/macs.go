package enroll

import (
	"net"
	"sort"
	"strings"
)

// physicalMACs returns the hardware addresses that identify THIS MACHINE, in a
// stable order, without duplicates.
//
// WHAT THE SERVER DOES WITH THEM, so the filter's stakes are visible from
// here: it AUTO-LINKS an enrolling agent to an existing inventory record
// carrying one of these addresses (docs/modules/asset-inventory/spec.md §4
// rule 2). A wrong entry therefore does not produce a wrong display value — it
// merges two records, and the spec is explicit that a bad merge is data loss
// discovered months later.
//
// FILTERED ON THE LOCALLY-ADMINISTERED BIT, NOT ON INTERFACE NAME. Matching
// `veth*`, `docker*` and `br-*` is fragile across three operating systems and
// misses the case that actually collides: Docker, KVM, most hypervisor
// defaults and phone MAC randomisation all set that bit, and two containers on
// different hosts can carry the SAME address. The bit is defined by the
// standard, is the same on every platform, and needs no list to maintain.
// (This project's own reference host reported nine `veth*` interfaces.)
//
// Loopback and multicast addresses are excluded for the same reason: neither
// identifies a machine, and a multicast bit set on a "hardware" address means
// whatever reported it was not reporting an interface identity.
//
// A DOWN INTERFACE IS STILL INCLUDED. A laptop enrolling over Wi-Fi with its
// Ethernet unplugged still owns that Ethernet MAC, and it is the address a
// discovery sweep most likely saw when the machine was at a desk.
func physicalMACs() []string {
	interfaces, err := net.Interfaces()
	if err != nil {
		// Best effort by design: enrollment must not fail because an interface
		// enumeration did. The server treats an empty list exactly as it
		// treats an older agent that cannot send one -- it falls through to
		// the suggestion path.
		return nil
	}
	seen := make(map[string]struct{}, len(interfaces))
	out := make([]string, 0, len(interfaces))
	for _, iface := range interfaces {
		if iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		if !isPhysicalMAC(iface.HardwareAddr) {
			continue
		}
		address := strings.ToLower(iface.HardwareAddr.String())
		if _, dup := seen[address]; dup {
			// Bonded and VLAN interfaces legitimately share their parent's
			// address; sending it twice would say nothing extra.
			continue
		}
		seen[address] = struct{}{}
		out = append(out, address)
	}
	// Sorted so the same machine sends the same list every time, which is what
	// makes a difference between two enrollments mean something.
	sort.Strings(out)
	return out
}

// isPhysicalMAC reports whether addr is a manufacturer-assigned unicast
// address. See physicalMACs for why these two bits and not an interface-name
// list.
func isPhysicalMAC(addr net.HardwareAddr) bool {
	if len(addr) != 6 {
		// EUI-64, InfiniBand and tunnel pseudo-addresses are not the six-octet
		// Ethernet address the server matches on.
		return false
	}
	if addr[0]&0b1 != 0 {
		return false // multicast: never an interface's own identity
	}
	if addr[0]&0b10 != 0 {
		return false // locally administered: assigned by software, can collide
	}
	if allZero(addr) {
		return false
	}
	return true
}

func allZero(addr net.HardwareAddr) bool {
	for _, b := range addr {
		if b != 0 {
			return false
		}
	}
	return true
}
