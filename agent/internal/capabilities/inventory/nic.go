package inventory

import (
	"net"
	"net/netip"
)

// NIC components, and §14.1's address block.
//
// Enumeration is cross-platform through net.Interfaces()/Interface.Addrs():
// the stdlib already asks each OS the right question and hands back a
// *net.IPNet, which carries the prefix length. Nothing here shells out -- the
// only per-OS code is the origin prober (origin.go).
//
// # The shape (docs/modules/ipam/spec.md §14.1)
//
//	{ "type": "nic", "identifier": "a4:bb:6d:11:22:33",
//	  "attributes": {
//	    "name": "Ethernet0",
//	    "mac":  "a4:bb:6d:11:22:33",
//	    "addresses": [
//	      {"address": "10.1.2.42/24", "family": "ipv4", "origin": "dhcp"},
//	      {"address": "fe80::1%eth0/64", "family": "ipv6", "origin": "link_local"}
//	    ] } }
//
// # `addresses[]` is the ONLY address key. There is no `attributes.ip`.
//
// There used to be one, emitted alongside the block so that a control plane
// predating the block still saw something: that parser reads ONLY `ip`, and it
// is TOTAL BY DESIGN -- it returns [] rather than raising -- so an agent that
// emitted the block alone would have silenced its entire passive reconciliation
// stream with nothing going red on either side. That was a real hazard for a
// DEPLOYED control plane, and there is none: the platform is not live anywhere
// (docs/modules/agent/open-questions.md OQ-2, closed 2026-08-05), so both
// conditions the dual path was waiting on are vacuously satisfied.
//
// DO NOT REINTRODUCE `ip` AS A CONVENIENCE. Two keys for one fact is two
// implementations of one thing, and they drift; the address grid and the drift
// page computing `conflicting` differently is the same shape this repository
// has already paid for once. If a future deployment ever needs a compatibility
// key, that is a wire-protocol decision (docs/kernel/wire-protocol/README.md),
// versioned and reviewed -- not a line added back here.
// TestTheLegacyIPKeyIsNotEmitted pins its absence.
//
// # What is NOT filtered here
//
// The block is emitted UNFILTERED -- loopback, link-local and all. §4.1's
// discard rule lives in the server's `net.is_documentable`, and one
// implementation of "documentable" is the point. Nothing on this side makes a
// discard judgement, so there is no second implementation of it to drift.

// maxNICAddresses bounds what one interface may contribute. An interface with
// hundreds of aliases is not a reason to push an unbounded document through the
// gateway. The server bounds what it will READ at 64 as well
// (app/modules/ipam/nic.py::_MAX_ADDRESSES).
const maxNICAddresses = 64

// Address is one entry of §14.1's `addresses[]`.
type Address struct {
	// Address is CIDR text: the interface's own address with its prefix
	// length, and a zone for a link-local IPv6 address ("fe80::1%eth0/64").
	// The server's parse_address strips both the prefix and the zone, in that
	// order, so this text parses there as well as reading correctly here.
	Address string `json:"address"`
	// Family is "ipv4" or "ipv6", derived from the PARSED address rather than
	// from looking for a colon: `::ffff:10.1.2.42` has colons and is IPv4.
	Family string `json:"family"`
	// Origin is one of {static, dhcp, slaac, link_local, unknown}. See
	// origin.go: every mapping is explicit and every unmapped case is unknown.
	Origin string `json:"origin"`
}

// nicComponents describes every interface on this host.
//
// The origin prober is built ONCE by the caller, inside the job, and released
// when the collection returns -- so a machine with sixty interfaces costs one
// `ip -j addr`, not sixty.
//
// Interfaces the OS will not describe are skipped rather than failing the
// collection: one interface disappearing mid-enumeration (a VPN going down) is
// ordinary, and it is not a reason for a device to have no inventory.
func nicComponents(prober originProber) []Component {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil
	}
	var out []Component
	for _, iface := range ifaces {
		addrs, err := iface.Addrs()
		if err != nil {
			// The interface vanished between the enumeration and the query. It
			// is still worth reporting that it exists.
			addrs = nil
		}
		if c, ok := nicComponentFrom(iface, addrs, prober); ok {
			out = append(out, c)
		}
	}
	return out
}

// nicComponentFrom builds one NIC component. Split from nicComponents so the
// tests can describe interfaces this container does not have.
func nicComponentFrom(iface net.Interface, addrs []net.Addr, prober originProber) (Component, bool) {
	mac := iface.HardwareAddr.String()

	attributes := map[string]any{"name": iface.Name}
	if mac != "" {
		// §14.1's `mac`. This is what lets the server find a MAC for an
		// interface whose `identifier` is a friendly name -- which is what
		// Windows reports, and what kept §4.3's mac_mismatch from firing there.
		attributes["mac"] = mac
	}
	if block := addressBlockFor(iface, addrs, prober); len(block) > 0 {
		attributes["addresses"] = block
	}

	identifier := mac
	if identifier == "" {
		identifier = iface.Name
	}
	if identifier == "" {
		return Component{}, false
	}
	return Component{Type: "nic", Identifier: identifier, Attributes: attributes}, true
}

// addressBlockFor renders §14.1's `addresses[]` for one interface.
func addressBlockFor(iface net.Interface, addrs []net.Addr, prober originProber) []Address {
	var out []Address
	for _, a := range addrs {
		if len(out) == maxNICAddresses {
			break
		}
		ipnet, ok := a.(*net.IPNet)
		if !ok {
			// Only *net.IPNet carries a prefix length. Reporting a /32 for an
			// address that did not come with one would be a fact nobody
			// measured, so the address is skipped instead.
			continue
		}
		addr, ok := netip.AddrFromSlice(ipnet.IP)
		if !ok {
			continue
		}
		addr = addr.Unmap()
		family := "ipv6"
		if addr.Is4() {
			family = "ipv4"
		}
		bits, ok := prefixBits(addr, ipnet.Mask)
		if !ok {
			continue
		}
		text := addr.String()
		if addr.Is6() && addr.IsLinkLocalUnicast() {
			// §14.1's example carries a zone ("fe80::1%4/64"). Go's convention
			// is the interface NAME rather than the index; the server strips
			// the zone before parsing either way, and without it two hosts'
			// fe80::1 read as one address.
			text = addr.WithZone(iface.Name).String()
		}
		out = append(out, Address{
			Address: text + "/" + itoa(bits),
			Family:  family,
			Origin:  originOf(prober, iface.Name, addr),
		})
	}
	return out
}

// prefixBits is the prefix length in the address's OWN family.
//
// net.IPNet may hold a 16-byte IP with a 128-bit mask for an address that is
// really IPv4, in which case the v4 prefix is the last 32 bits of the mask:
// ::ffff:10.1.2.42/120 is 10.1.2.42/24. Emitting "/120" beside family "ipv4"
// would put a CIDR in front of an operator that means nothing.
func prefixBits(addr netip.Addr, mask net.IPMask) (int, bool) {
	ones, bits := mask.Size()
	if bits == 0 {
		return 0, false // a non-contiguous mask; Size() gives up and so do we
	}
	if addr.Is4() && bits == 128 {
		ones -= 96
	}
	if ones < 0 || (addr.Is4() && ones > 32) || ones > 128 {
		return 0, false
	}
	return ones, true
}

// itoa is strconv.Itoa for small non-negative values, kept local so this file
// does not import strconv for one call.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [4]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
