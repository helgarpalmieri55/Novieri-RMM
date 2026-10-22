package inventory

import (
	"net/netip"
	"strings"
	"testing"
)

// The three origin mappings, all exercised on linux.
//
// WHICH PLATFORMS WERE ACTUALLY EXERCISED, stated plainly because §14.1's
// `origin` is the fact IPAM's §3.2 will build `allocation_method` on:
//
//   - linux: the container this repository builds in. `ip` is NOT INSTALLED
//     here, so the real linux prober answers `unknown` for every address on
//     this machine (TestThisHostsRealInterfacesProduceAWellFormedBlock logs
//     exactly that). The MAPPING is exercised below against captured
//     `ip -j addr show` output; the exec that feeds it is not.
//   - windows: never run. The mapping and both of PowerShell's enum
//     serialisations are exercised below; one exec.Command is not.
//   - macOS: never run. parseGetPacket is exercised below; the per-interface
//     `ipconfig getpacket` calls are not.
//
// So: three mappings tested, three exec calls unexercised, and on linux the
// exec is known to find nothing. That is the honest statement, and it is why
// none of these mappings may invent a value -- see origin.go.

func addr(t *testing.T, s string) netip.Addr {
	t.Helper()
	a, err := netip.ParseAddr(s)
	if err != nil {
		t.Fatal(err)
	}
	return a
}

// ---------------------------------------------------------------------------
// Linux: `ip -j addr show`
// ---------------------------------------------------------------------------

// ipAddrFixture is `ip -j addr show` output, in the shape iproute2 emits:
// `dynamic` present only when the address is not IFA_F_PERMANENT, `protocol`
// present only on kernels that expose IFA_PROTO.
const ipAddrFixture = `[
 {"ifindex":1,"ifname":"lo","addr_info":[
   {"family":"inet","local":"127.0.0.1","prefixlen":8,"scope":"host"}]},
 {"ifindex":2,"ifname":"eth0","addr_info":[
   {"family":"inet","local":"10.1.2.42","prefixlen":24,"scope":"global","dynamic":true},
   {"family":"inet","local":"10.9.9.9","prefixlen":24,"scope":"global"},
   {"family":"inet6","local":"2001:db8::5","prefixlen":64,"scope":"global","dynamic":true,"protocol":"kernel_ra"},
   {"family":"inet6","local":"2001:db8::99","prefixlen":128,"scope":"global","dynamic":true},
   {"family":"inet6","local":"2001:db8::7","prefixlen":64,"scope":"global"},
   {"family":"inet6","local":"fe80::1","prefixlen":64,"scope":"link","protocol":"kernel_ll"}]}]`

func TestTheLinuxOriginMappingIsExplicitCaseByCase(t *testing.T) {
	index := parseIPAddrJSON(ipAddrFixture)
	for _, tc := range []struct {
		name    string
		iface   string
		address string
		want    string
	}{
		{"IPv4 without the permanent flag is a lease", "eth0", "10.1.2.42", originDHCP},
		{"IPv4 that is permanent is static", "eth0", "10.9.9.9", originStatic},
		{"IPv6 from a router advertisement is slaac", "eth0", "2001:db8::5", originSLAAC},
		{
			// The arm that must NOT guess. A dynamic IPv6 address with no
			// IFA_PROTO is DHCPv6 or SLAAC and this output cannot say which;
			// answering `slaac` would be wrong on every DHCPv6 estate.
			"IPv6 dynamic with no protocol is unknown, not slaac",
			"eth0", "2001:db8::99", originUnknown,
		},
		{"IPv6 that is permanent is static", "eth0", "2001:db8::7", originStatic},
		{"loopback is permanent and therefore static here", "lo", "127.0.0.1", originStatic},
		{"an address the tool did not mention", "eth0", "10.4.4.4", originUnknown},
		{"an interface the tool did not mention", "wlan0", "10.1.2.42", originUnknown},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := index.Origin(tc.iface, addr(t, tc.address)); got != tc.want {
				t.Fatalf("Origin(%q, %q) = %q; want %q", tc.iface, tc.address, got, tc.want)
			}
		})
	}
}

func TestLinkLocalOutranksWhateverTheProberSays(t *testing.T) {
	// originOf decides link_local from the ADDRESS, before consulting anything.
	// fe80::1 is link-local by definition, and `ip` calling it permanent must
	// not turn it into `static`.
	//
	// FALSIFIER: `static` for fe80::1. The fixture deliberately contains an
	// entry for it, so this test is not passing merely because the prober had
	// no data -- the shape that has bitten this module twice.
	index := parseIPAddrJSON(ipAddrFixture)
	if got := originOf(index, "eth0", addr(t, "fe80::1")); got != originLinkLocal {
		t.Fatalf("originOf(fe80::1) = %q; want %q", got, originLinkLocal)
	}
	if got := originOf(index, "eth0", addr(t, "169.254.7.7")); got != originLinkLocal {
		t.Fatalf("originOf(169.254.7.7) = %q; want %q", got, originLinkLocal)
	}
	// And the fixture really does answer for that address, so the assertion
	// above is about precedence and not about absence.
	if got := index.Origin("eth0", addr(t, "fe80::1")); got != originLinkLocal {
		t.Fatalf("the fixture's own answer for fe80::1 is %q; adjust the fixture so the "+
			"precedence assertion above is not vacuous", got)
	}
}

func TestTheLinuxProberDegradesToUnknownRatherThanFailing(t *testing.T) {
	// This is the container's ACTUAL situation: `ip` is not installed, run()
	// returns "", and everything is unknown.
	//
	// FALSIFIER: any value other than unknown from an index built on nothing.
	for _, raw := range []string{
		"",
		"Command 'ip' not found",
		`{"not":"an array"}`,
		`[{"ifname":"eth0","addr_info":"not a list"}]`,
		`[{"ifname":"eth0","addr_info":[{"local":"not-an-address","dynamic":true}]}]`,
	} {
		index := parseIPAddrJSON(raw)
		if got := index.Origin("eth0", addr(t, "10.1.2.42")); got != originUnknown {
			t.Fatalf("parseIPAddrJSON(%q).Origin = %q; want %q", raw, got, originUnknown)
		}
	}
	var nilIndex *linuxOrigins
	if got := nilIndex.Origin("eth0", addr(t, "10.1.2.42")); got != originUnknown {
		t.Fatalf("a nil index answered %q", got)
	}
}

// ---------------------------------------------------------------------------
// Windows: Get-NetIPAddress
// ---------------------------------------------------------------------------

func TestTheWindowsOriginMappingCoversTheEnumerationExplicitly(t *testing.T) {
	for _, tc := range []struct{ prefix, suffix, want string }{
		{"Dhcp", "Dhcp", originDHCP},
		{"RouterAdvertisement", "Link", originSLAAC},
		{"RouterAdvertisement", "Random", originSLAAC},
		{"Manual", "Manual", originStatic},
		{"WellKnown", "Link", originLinkLocal},
		{"WellKnown", "WellKnown", originUnknown},
		{"Other", "Other", originUnknown},
		{"", "", originUnknown},
		{"SomethingMicrosoftAddsLater", "Link", originUnknown},
	} {
		if got := windowsOriginFor(tc.prefix, tc.suffix); got != tc.want {
			t.Errorf("windowsOriginFor(%q, %q) = %q; want %q", tc.prefix, tc.suffix, got, tc.want)
		}
	}
}

func TestTheWindowsProberReadsBothOfPowerShellsEnumSerialisations(t *testing.T) {
	// ConvertTo-Json renders an enum as its NAME in some PowerShell versions
	// and as its INTEGER in others. A parser that understood only one would
	// answer `unknown` for an entire Windows fleet and look perfectly healthy
	// doing it.
	//
	// FALSIFIER: `unknown` from either form for an address whose origin the
	// other form reports correctly.
	named := parseWindowsOrigins(
		`[{"iface":"Ethernet0","address":"10.1.2.42","prefix":"Dhcp","suffix":"Dhcp"}]`)
	numeric := parseWindowsOrigins(
		`[{"iface":"Ethernet0","address":"10.1.2.42","prefix":3,"suffix":3}]`)
	for name, index := range map[string]*windowsOrigins{"named": named, "numeric": numeric} {
		if got := index.Origin("Ethernet0", addr(t, "10.1.2.42")); got != originDHCP {
			t.Errorf("%s form: Origin = %q; want %q", name, got, originDHCP)
		}
	}
}

func TestTheWindowsProberHandlesTheSingleResultObjectForm(t *testing.T) {
	// ConvertTo-Json emits a bare object rather than a one-element array when
	// there is exactly one result. A machine with one address is not a machine
	// this prober may fail on.
	index := parseWindowsOrigins(
		`{"iface":"Ethernet0","address":"10.1.2.42","prefix":"Manual","suffix":"Manual"}`)
	if got := index.Origin("Ethernet0", addr(t, "10.1.2.42")); got != originStatic {
		t.Fatalf("Origin = %q; want %q", got, originStatic)
	}
}

func TestTheWindowsProberStripsTheZoneFromALinkLocalAddress(t *testing.T) {
	// Get-NetIPAddress reports "fe80::1%12". The index key must match what
	// nic.go looks up, which is the zoneless address.
	index := parseWindowsOrigins(
		`[{"iface":"Ethernet0","address":"fe80::1%12","prefix":"WellKnown","suffix":"Link"}]`)
	if got := index.Origin("Ethernet0", addr(t, "fe80::1")); got != originLinkLocal {
		t.Fatalf("Origin = %q; want %q", got, originLinkLocal)
	}
}

func TestTheWindowsProberDegradesToUnknown(t *testing.T) {
	for _, raw := range []string{"", "Get-NetIPAddress : The term is not recognized", "[", "[[]]"} {
		index := parseWindowsOrigins(raw)
		if got := index.Origin("Ethernet0", addr(t, "10.1.2.42")); got != originUnknown {
			t.Fatalf("parseWindowsOrigins(%q).Origin = %q", raw, got)
		}
	}
	var nilIndex *windowsOrigins
	if got := nilIndex.Origin("Ethernet0", addr(t, "10.1.2.42")); got != originUnknown {
		t.Fatalf("a nil index answered %q", got)
	}
}

// ---------------------------------------------------------------------------
// macOS: `ipconfig getpacket <if>`
// ---------------------------------------------------------------------------

func TestParseGetPacketFindsTheLeasedAddress(t *testing.T) {
	out := `op = BOOTREPLY
htype = 1
yiaddr = 10.1.2.42
siaddr = 10.1.2.1
options:
Options count is 6
dhcp_message_type (uint8): ACK 0x5`
	if got := parseGetPacket(out); got != "10.1.2.42" {
		t.Fatalf("parseGetPacket = %q", got)
	}
}

func TestAnInterfaceWithNoDhcpPacketIsUnknownAndNeverStatic(t *testing.T) {
	// `ipconfig getpacket` prints nothing for a manually configured interface
	// -- AND for a permission failure, AND for an interface it does not know.
	// Reading absence as `static` would be a confidently wrong answer on two of
	// those three, so macOS reports `unknown`.
	//
	// FALSIFIER: `static` from an empty packet dump.
	index := &darwinOrigins{dhcpByInterface: map[string]map[string]bool{
		"en0": {"10.1.2.42": true},
	}}
	if got := index.Origin("en0", addr(t, "10.1.2.42")); got != originDHCP {
		t.Fatalf("a leased address answered %q", got)
	}
	if got := index.Origin("en0", addr(t, "10.9.9.9")); got != originUnknown {
		t.Fatalf("a manually configured address answered %q; macOS cannot distinguish "+
			"manual from unreadable, so it must not claim static", got)
	}
	if got := index.Origin("en1", addr(t, "10.1.2.42")); got != originUnknown {
		t.Fatalf("an interface with no packet answered %q", got)
	}
	var nilIndex *darwinOrigins
	if got := nilIndex.Origin("en0", addr(t, "10.1.2.42")); got != originUnknown {
		t.Fatalf("a nil index answered %q", got)
	}
}

func TestParseGetPacketDegrades(t *testing.T) {
	for _, raw := range []string{
		"",
		"ipconfig: getpacket: not a DHCP interface",
		"yiaddr =\n",
		"yiaddr = not-an-address\n",
		"siaddr = 10.1.2.1\n",
	} {
		if got := parseGetPacket(raw); got != "" {
			t.Fatalf("parseGetPacket(%q) = %q; want the empty string", raw, got)
		}
	}
}

// ---------------------------------------------------------------------------
// The enum itself
// ---------------------------------------------------------------------------

func TestNothingOutsideTheEnumCanEscape(t *testing.T) {
	// §14.1's set is closed, and a value outside it is a value IPAM's §3.2 will
	// not know what to do with. A prober that returned one is a bug in the
	// prober, and `unknown` is the safe reading of a bug.
	//
	// FALSIFIER: the bogus value coming back out of originOf.
	if got := originOf(bogusProber{}, "eth0", addr(t, "10.1.2.42")); got != originUnknown {
		t.Fatalf("originOf let %q through; the enum is closed", got)
	}
	if got := originOf(nil, "eth0", addr(t, "10.1.2.42")); got != originUnknown {
		t.Fatalf("originOf with no prober = %q", got)
	}
	want := map[string]bool{
		"static": true, "dhcp": true, "slaac": true, "link_local": true, "unknown": true,
	}
	if len(validOrigins) != len(want) {
		t.Fatalf("validOrigins = %v; §14.1's set is exactly %v", validOrigins, want)
	}
	for name := range want {
		if !validOrigins[name] {
			t.Fatalf("validOrigins is missing %q", name)
		}
	}
}

type bogusProber struct{}

func (bogusProber) Origin(string, netip.Addr) string { return "dhcpv6" }

func TestTheWindowsOriginScriptInterpolatesNothing(t *testing.T) {
	// Rule 1 in exec.go, applied to the second PowerShell constant. Nothing
	// job-supplied may reach an argument, and the source walker in
	// inventory_test.go already proves this package never reads job.Params() --
	// this proves the string handed to the interpreter has nowhere to put
	// anything even if it did.
	for _, forbidden := range []string{"%s", "%v", "%d", "%q"} {
		if strings.Contains(windowsFactsScript, forbidden) {
			t.Fatalf("windowsFactsScript contains %q", forbidden)
		}
	}
	for _, expected := range []string{"Get-NetIPAddress", "PrefixOrigin", "SuffixOrigin"} {
		if !strings.Contains(windowsOriginRows, expected) {
			t.Fatalf("windowsOriginRows no longer mentions %q; the mapping tested above would "+
				"then be fed by a command that does not report an origin at all", expected)
		}
	}
	// And the facts script carries it, as the member windowsOriginsFrom reads.
	if !strings.Contains(windowsFactsScript, "origins=@("+windowsOriginRows+")") {
		t.Fatal("windowsFactsScript no longer carries the origin rows as `origins`")
	}
}

func TestTheOriginsRideInTheFactsDocument(t *testing.T) {
	// The shape windowsFactsScript produces, trimmed to what matters here:
	// one address as names, one as integers, and the facts beside them.
	raw := `{"caption":"Microsoft Windows 11 Pro","origins":[` +
		`{"iface":"Ethernet0","address":"10.1.2.42","prefix":"Dhcp","suffix":"Dhcp"},` +
		`{"iface":"Ethernet0","address":"10.1.2.43","prefix":1,"suffix":1}]}`
	p := windowsOriginsFrom(raw)
	if got := p.Origin("Ethernet0", netip.MustParseAddr("10.1.2.42")); got != originDHCP {
		t.Errorf("10.1.2.42 origin = %q, want %q", got, originDHCP)
	}
	if got := p.Origin("Ethernet0", netip.MustParseAddr("10.1.2.43")); got != originStatic {
		t.Errorf("10.1.2.43 origin = %q, want %q", got, originStatic)
	}
	if parseWindowsFacts(raw).OSVersion == "" {
		t.Error("the origins member broke the facts parse")
	}
	// A facts run that failed is an empty index, not a panic or a guess.
	for _, bad := range []string{"", "not json", `{"caption":"x"}`} {
		if got := windowsOriginsFrom(bad).Origin("Ethernet0", netip.MustParseAddr("10.1.2.42")); got != originUnknown {
			t.Errorf("windowsOriginsFrom(%q) answered %q, want unknown", bad, got)
		}
	}
}
