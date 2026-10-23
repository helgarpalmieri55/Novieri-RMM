package inventory

import (
	"net"
	"reflect"
	"testing"
)

// §14.1's NIC address block -- the ONE place a NIC reports its addresses.
//
// This file used to also pin `attributes.ip`, a second key carrying one of the
// same addresses, emitted so that a control plane predating the block still saw
// something. OQ-2 (docs/modules/agent/open-questions.md) closed on 2026-08-05:
// the platform is not live, no control plane and no agent is deployed anywhere,
// so the mixed-version hazard the dual path insured against does not exist, and
// what is left is two implementations of one fact.
//
// What survives that removal, and is asserted below, is everything that was
// never about compatibility: the block is emitted unfiltered and the SERVER
// decides what is documentable (TestLoopbackInterfacesAreReportedAndTheServerDecides),
// and the legacy key is now provably absent (TestTheLegacyIPKeyIsNotEmitted).

// ---------------------------------------------------------------------------
// The block
// ---------------------------------------------------------------------------

func TestAnInterfaceWithTwoIPv4AddressesEmitsTwoEntriesEachWithItsPrefixLength(t *testing.T) {
	// The headline of the change: the legacy key held ONE address per NIC, so a
	// second address on an interface was invisible to IPAM, and no address had a
	// prefix length at all.
	c := componentFor(t, net.Interface{Name: "eth0", HardwareAddr: mac(t, "a4:bb:6d:11:22:33")},
		addrs(t, "10.1.2.42/24", "10.1.2.43/24"))

	got := addressBlock(t, c)
	want := []map[string]any{
		{"address": "10.1.2.42/24", "family": "ipv4", "origin": originUnknown},
		{"address": "10.1.2.43/24", "family": "ipv4", "origin": originUnknown},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("addresses = %v\nwant %v", got, want)
	}
}

func TestFamilyComesFromTheParsedAndUNMAPPEDAddress(t *testing.T) {
	// The case: `::ffff:10.1.2.42`, a v4-mapped v6 address. Parsed and unmapped
	// it is IPv4, which is what the server will make of it too --
	// `is_documentable` unmaps before judging, precisely because CPython says
	// IPv6Address("::ffff:127.0.0.1").is_loopback is False.
	//
	// FALSIFIER, MEASURED: deleting `addr = addr.Unmap()` in addressBlockFor
	// reports family "ipv6" here, and prefixBits then refuses the /120 outright
	// so the address disappears from the block entirely.
	//
	// THE PLAN'S STATED FALSIFIER DOES NOT EXIST ON THIS PATH, and saying so is
	// the point of this comment. Task 11 step 3 asks for "family derived from
	// the parsed address, not from a string search for `:`" -- but the mutation
	// `strings.Contains(a.String(), ":")` PASSES this test, because Go's
	// *net.IPNet.String() renders a v4-mapped network as dotted quad and there
	// is no colon left to find. A test that claimed to catch that would be a
	// test passing for a reason other than the one written on it.
	c := componentFor(t, net.Interface{Name: "eth0"}, addrs(t, "::ffff:10.1.2.42/120", "2001:db8::5/64"))

	got := addressBlock(t, c)
	if len(got) != 2 {
		t.Fatalf("addresses = %v; want two", got)
	}
	if got[0]["family"] != "ipv4" {
		t.Errorf("::ffff:10.1.2.42 reported as %v; it is an IPv4 address wearing a v6 coat",
			got[0]["family"])
	}
	if got[1]["family"] != "ipv6" {
		t.Errorf("2001:db8::5 reported as %v", got[1]["family"])
	}
}

func TestAnIPv6PrefixLengthSurvivesTheV4MappedShape(t *testing.T) {
	// net.IPNet may carry a 16-byte IP with a 128-bit mask for an address that
	// is really v4. Reporting `/120` as the prefix of a v4 address would put a
	// nonsense CIDR in front of an operator; the server strips the suffix
	// today, so this is about what the block says rather than about ingestion.
	c := componentFor(t, net.Interface{Name: "eth0"}, addrs(t, "::ffff:10.1.2.42/120"))
	block := addressBlock(t, c)
	if len(block) != 1 {
		// FALSIFIER, MEASURED: deleting the `ones -= 96` adjustment makes
		// prefixBits refuse a 120 that would be out of range for IPv4, so the
		// address vanishes rather than being reported wrongly. Checked here so
		// the failure reads as a missing address rather than as an index panic.
		t.Fatalf("addresses = %v; want the one v4-mapped address, reported as IPv4", block)
	}
	if got := block[0]["address"]; got != "10.1.2.42/24" {
		t.Fatalf("address = %v; a v4-mapped /120 is a /24", got)
	}
}

func TestOriginIsOneOfTheFiveAndNeverInventsStatic(t *testing.T) {
	// §4.1's argument for `posture.antivirus_healthy`, applied here: a
	// confidently wrong `static` is worse than an honest `unknown`. With no
	// prober -- which is this container's actual situation, since `ip` is not
	// installed -- everything that is not derivable from the address itself is
	// `unknown`.
	c := componentFor(t, net.Interface{Name: "eth0"},
		addrs(t, "10.1.2.42/24", "fe80::1/64", "169.254.7.7/16", "2001:db8::5/64"))

	got := addressBlock(t, c)
	origins := []any{}
	for _, entry := range got {
		origins = append(origins, entry["origin"])
		if !validOrigins[entry["origin"].(string)] {
			t.Fatalf("origin %q is outside {static, dhcp, slaac, link_local, unknown}", entry["origin"])
		}
	}
	// link_local is derived from the ADDRESS, which is a fact and not a guess.
	// Everything else with no prober is unknown, and never static.
	want := []any{originUnknown, originLinkLocal, originLinkLocal, originUnknown}
	if !reflect.DeepEqual(origins, want) {
		t.Fatalf("origins = %v; want %v", origins, want)
	}
}

func TestLoopbackInterfacesAreReportedAndTheServerDecides(t *testing.T) {
	// One implementation of "documentable", and it is the server's
	// (`net.is_documentable`). The agent does not pre-filter the block: an agent
	// that made the discard judgement itself would be a second implementation
	// of a rule that has to be one, and it would be the one nobody could see.
	//
	// FALSIFIER: an empty or absent `addresses[]` on `lo`.
	c := componentFor(t, net.Interface{Name: "lo", Flags: net.FlagLoopback}, addrs(t, "127.0.0.1/8", "::1/128"))
	got := addressBlock(t, c)
	if len(got) != 2 {
		t.Fatalf("loopback addresses = %v; the agent reports them and the server discards them", got)
	}
}

// ---------------------------------------------------------------------------
// The removed legacy key (OQ-2, closed 2026-08-05)
// ---------------------------------------------------------------------------

func TestTheLegacyIPKeyIsNotEmitted(t *testing.T) {
	// `attributes.addresses[]` is the ONLY address key. `attributes.ip` was a
	// second one carrying a copy of one of the same addresses, kept alive for a
	// control plane that predated the block; OQ-2 recorded that dropping it was
	// a fleet decision, and the product owner has confirmed the platform is not
	// live and nothing is deployed anywhere. The decision is: one key.
	//
	// THIS INTERFACE IS THE ONE THE OLD CODE WOULD DEFINITELY HAVE LABELLED.
	// The removed `legacyIPv4` skipped loopback, link-local, multicast and
	// unspecified addresses and took the first ordinary IPv4 one, so an
	// interface whose only usable address is 169.254.x got no `ip` even before
	// this change -- asserting absence there would pass against the code that
	// still emits the key. 10.1.2.42 is the case that discriminates.
	//
	// FALSIFIER, MEASURED: restoring `attributes["ip"] = legacy` in
	// nicComponentFrom makes this print
	//   attributes.ip = 10.1.2.42; §14.1's addresses[] is the only address key
	c := componentFor(t, net.Interface{Name: "eth0", HardwareAddr: mac(t, "a4:bb:6d:11:22:33")},
		addrs(t, "169.254.7.7/16", "10.1.2.42/24", "2001:db8::5/64"))

	if got, present := c.Attributes["ip"]; present {
		t.Errorf("attributes.ip = %v; §14.1's addresses[] is the only address key. Two keys for "+
			"one fact is two implementations of it, and they drift.", got)
	}
	// Asserted in the same test on the same component, because "no `ip`" is
	// also what a NIC with no addresses at all looks like: the block must still
	// carry every address, which is what makes the absence above a removal
	// rather than a regression.
	if block, ok := c.Attributes["addresses"].([]Address); !ok || len(block) != 3 {
		t.Fatalf("attributes.addresses = %v; want all three addresses, unfiltered",
			c.Attributes["addresses"])
	}
}

func TestTheIdentifierIsTheMacWhenTheOSExposesOneAndTheNameOtherwise(t *testing.T) {
	// nic.py treats `identifier` as a MAC only when it PARSES as one and records
	// mac = NULL otherwise, so both shapes are safe -- and §14.1's
	// `attributes.mac` is what lets the server find the MAC even in the second
	// case, which is what makes §4.3's mac_mismatch able to fire on Windows.
	withMAC := componentFor(t, net.Interface{Name: "eth0", HardwareAddr: mac(t, "a4:bb:6d:11:22:33")}, nil)
	if withMAC.Identifier != "a4:bb:6d:11:22:33" {
		t.Errorf("identifier = %q; want the MAC", withMAC.Identifier)
	}
	if withMAC.Attributes["mac"] != "a4:bb:6d:11:22:33" {
		t.Errorf("attributes.mac = %v", withMAC.Attributes["mac"])
	}

	without := componentFor(t, net.Interface{Name: "tun0"}, nil)
	if without.Identifier != "tun0" {
		t.Errorf("identifier = %q; want the interface name when there is no MAC", without.Identifier)
	}
	if _, present := without.Attributes["mac"]; present {
		t.Errorf("attributes.mac = %v on an interface with no hardware address; absence is the "+
			"honest answer and nic.py records mac = NULL for it", without.Attributes["mac"])
	}
}

// ---------------------------------------------------------------------------
// Bounds and degradation
// ---------------------------------------------------------------------------

func TestTheAddressListIsBoundedPerInterface(t *testing.T) {
	// The server bounds what it will READ (nic.py's _MAX_ADDRESSES); this bounds
	// what the agent SENDS, so a machine with thousands of aliases does not push
	// a document neither side wants through the gateway's size limits.
	var many []net.Addr
	for i := 0; i < 500; i++ {
		many = append(many, &net.IPNet{
			IP:   net.IPv4(10, byte(i/256), byte(i%256), 1),
			Mask: net.CIDRMask(24, 32),
		})
	}
	c := componentFor(t, net.Interface{Name: "eth0"}, many)
	if got := len(addressBlock(t, c)); got != maxNICAddresses {
		t.Fatalf("emitted %d addresses; the cap is %d", got, maxNICAddresses)
	}
}

func TestAnAddressTypeTheStdlibDoesNotGiveUsIsSkippedNotGuessedAt(t *testing.T) {
	// Interface.Addrs() returns []net.Addr, and only *net.IPNet carries a
	// prefix. Anything else (a *net.IPAddr from an odd platform) has no prefix
	// length to report, and inventing /32 would be a fact nobody measured.
	c := componentFor(t, net.Interface{Name: "eth0"},
		[]net.Addr{&net.IPAddr{IP: net.ParseIP("10.1.2.42")}, addrs(t, "10.9.9.9/24")[0]})
	got := addressBlock(t, c)
	if len(got) != 1 || got[0]["address"] != "10.9.9.9/24" {
		t.Fatalf("addresses = %v; want only the one that carried a prefix", got)
	}
}

func TestAnInterfaceWithNoAddressesIsStillReported(t *testing.T) {
	// A NIC that exists is a hardware fact whether or not it is configured, and
	// the hardware view lists components. FALSIFIER: the component vanishing.
	c := componentFor(t, net.Interface{Name: "eth1", HardwareAddr: mac(t, "a4:bb:6d:11:22:34")}, nil)
	if c.Type != "nic" || c.Identifier != "a4:bb:6d:11:22:34" {
		t.Fatalf("component = %+v", c)
	}
	if block, ok := c.Attributes["addresses"]; ok {
		if entries, _ := block.([]Address); len(entries) != 0 {
			t.Fatalf("addresses = %v on an unconfigured interface", block)
		}
	}
}

// ---------------------------------------------------------------------------
// This host, for real
// ---------------------------------------------------------------------------

func TestThisHostsRealInterfacesProduceAWellFormedBlock(t *testing.T) {
	// Through gather, because that is where each OS's origins come from: a
	// prober of its own on Unix, the facts PowerShell on Windows.
	var components []Component
	for _, c := range gather(t.Context()).Components {
		if c.Type == "nic" {
			components = append(components, c)
		}
	}
	if len(components) == 0 {
		t.Skip("no network interfaces in this container; the assertions below would be vacuous")
	}
	seen := 0
	for _, c := range components {
		for _, entry := range addressBlock(t, c) {
			seen++
			if !validOrigins[entry["origin"].(string)] {
				t.Errorf("%s: origin %q is outside the enum", c.Identifier, entry["origin"])
			}
			if family := entry["family"]; family != "ipv4" && family != "ipv6" {
				t.Errorf("%s: family %q", c.Identifier, family)
			}
		}
		t.Logf("%s (%v): addresses=%v",
			c.Identifier, c.Attributes["name"], c.Attributes["addresses"])
	}
	if seen == 0 {
		t.Skip("no addresses on any interface here")
	}
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// componentFor builds the NIC component for one interface with a fixed address
// list, bypassing net.Interface.Addrs() so the table above can describe
// interfaces this container does not have.
func componentFor(t *testing.T, iface net.Interface, list []net.Addr) Component {
	t.Helper()
	c, ok := nicComponentFrom(iface, list, noOriginProber{})
	if !ok {
		t.Fatalf("nicComponentFrom(%v) returned no component", iface.Name)
	}
	return c
}

// addressBlock re-reads the emitted block as the JSON the server will see.
func addressBlock(t *testing.T, c Component) []map[string]any {
	t.Helper()
	raw, ok := c.Attributes["addresses"]
	if !ok {
		return nil
	}
	entries, ok := raw.([]Address)
	if !ok {
		t.Fatalf("attributes.addresses is %T, not []Address", raw)
	}
	out := make([]map[string]any, 0, len(entries))
	for _, e := range entries {
		out = append(out, map[string]any{"address": e.Address, "family": e.Family, "origin": e.Origin})
	}
	return out
}

func addrs(t *testing.T, cidrs ...string) []net.Addr {
	t.Helper()
	var out []net.Addr
	for _, cidr := range cidrs {
		ip, ipnet, err := net.ParseCIDR(cidr)
		if err != nil {
			t.Fatalf("ParseCIDR(%q): %v", cidr, err)
		}
		// Interface.Addrs() returns the INTERFACE's address with the prefix's
		// mask, not the network address -- ParseCIDR gives the network in its
		// second return, so the two are recombined here the way the OS reports
		// them.
		out = append(out, &net.IPNet{IP: ip, Mask: ipnet.Mask})
	}
	return out
}

func mac(t *testing.T, s string) net.HardwareAddr {
	t.Helper()
	hw, err := net.ParseMAC(s)
	if err != nil {
		t.Fatal(err)
	}
	return hw
}
