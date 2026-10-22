package inventory

import (
	"encoding/json"
	"net/netip"
	"strings"
)

// How an address got onto an interface (`docs/modules/ipam/spec.md` §14.1).
//
// # The rule every mapping below obeys
//
// EVERY MAPPING IS EXPLICIT AND EVERY UNMAPPED CASE IS `unknown`. A confidently
// wrong `static` is worse than an honest `unknown`, for exactly the reason §4.1
// argues about `posture.antivirus_healthy`: `unknown` is a gap an operator can
// see and close, and a wrong `static` is a fact they will act on. IPAM's §3.2
// turns this into `allocation_method`, and a DHCP address recorded as a static
// reservation is a double-allocation waiting to happen.
//
// `link_local` is the one value that is derived rather than probed: fe80::/10
// and 169.254.0.0/16 are link-local BY DEFINITION, so saying so is reading the
// address, not guessing about it. Everything else needs the OS to say.
//
// # What is exercised where
//
// This file and its mappers have no build tag, so all three platforms' mappings
// are unit-tested on the linux container this repository builds in
// (origin_test.go). What is NOT exercised is the exec call in each prober --
// and on linux, not even that: `ip` is absent from this container, so the linux
// prober's own answer here is always "no data", i.e. `unknown`. Stated plainly
// rather than implied.
const (
	originStatic    = "static"
	originDHCP      = "dhcp"
	originSLAAC     = "slaac"
	originLinkLocal = "link_local"
	originUnknown   = "unknown"
)

// validOrigins is §14.1's closed set. Nothing outside it may ever be emitted.
var validOrigins = map[string]bool{
	originStatic: true, originDHCP: true, originSLAAC: true,
	originLinkLocal: true, originUnknown: true,
}

// originProber answers "how did this address get here?" for one host.
//
// It is built ONCE per collection, inside the job, and released when the job
// returns -- the shell-out it wraps happens at construction, not per address,
// so an interface with sixty aliases costs one `ip -j addr` and not sixty.
type originProber interface {
	// Origin returns one of the five values for an address on an interface.
	// It must return originUnknown rather than guessing.
	Origin(ifName string, addr netip.Addr) string
}

// noOriginProber is the answer when the OS has no way to say, and the fallback
// on any platform whose tool is missing.
type noOriginProber struct{}

func (noOriginProber) Origin(string, netip.Addr) string { return originUnknown }

// originOf is the one place an origin is decided.
//
// The address-derived case is checked FIRST and is not overridable by a prober:
// a link-local address is link-local whatever `ip` says about how it was
// configured, and letting a prober call it `static` would be the confidently
// wrong answer this file exists to refuse.
func originOf(prober originProber, ifName string, addr netip.Addr) string {
	if addr.IsLinkLocalUnicast() || addr.IsLinkLocalMulticast() {
		return originLinkLocal
	}
	if prober == nil {
		return originUnknown
	}
	origin := prober.Origin(ifName, addr)
	if !validOrigins[origin] {
		// A prober that returned something outside the enum is a bug in the
		// prober, and `unknown` is the safe reading of a bug.
		return originUnknown
	}
	return origin
}

// ---------------------------------------------------------------------------
// Linux: `ip -j addr`
// ---------------------------------------------------------------------------

// ipAddrEntry is one element of `ip -j addr show`.
type ipAddrEntry struct {
	IfName   string `json:"ifname"`
	AddrInfo []struct {
		Local string `json:"local"`
		// Dynamic is IFA_F_PERMANENT's absence and Protocol is IFA_PROTO.
		// `scope` and `prefixlen` are deliberately NOT read: the prefix comes
		// from net.Interface.Addrs() (nic.go) and link-local is decided from
		// the address itself, so reading either here would be a second source
		// for a fact that already has one.
		Dynamic  bool   `json:"dynamic"`
		Protocol string `json:"protocol"`
	} `json:"addr_info"`
}

// linuxOrigins indexes `ip -j addr show`'s output by (interface, address).
//
// The mapping, and the evidence for each arm:
//
//	protocol == "kernel_ra"   -> slaac      IFA_PROTO_KERNEL_RA: the kernel
//	                                        says this address came from a
//	                                        router advertisement.
//	protocol == "kernel_ll"   -> link_local (already decided by originOf)
//	dynamic && IPv4           -> dhcp       an IPv4 address that is not
//	                                        IFA_F_PERMANENT was leased.
//	dynamic && IPv6           -> unknown    DHCPv6 and SLAAC are BOTH dynamic
//	                                        and, without IFA_PROTO, are not
//	                                        distinguishable. Guessing `slaac`
//	                                        here would be wrong on every
//	                                        DHCPv6 estate.
//	not dynamic               -> static     IFA_F_PERMANENT. `ip -j` exists
//	                                        only in iproute2 >= 4.13, which
//	                                        has always emitted `dynamic`, so
//	                                        the absence of the flag is the
//	                                        kernel saying "permanent" rather
//	                                        than the tool being too old to say.
//	no entry for the address  -> unknown
type linuxOrigins struct {
	byKey map[string]string
}

// parseIPAddrJSON builds the index. Unparseable output yields an empty index,
// which answers `unknown` for everything -- the same as having no `ip` at all.
func parseIPAddrJSON(raw string) *linuxOrigins {
	var entries []ipAddrEntry
	if err := json.Unmarshal([]byte(strings.TrimSpace(raw)), &entries); err != nil {
		return &linuxOrigins{}
	}
	index := &linuxOrigins{byKey: make(map[string]string, len(entries))}
	for _, entry := range entries {
		for _, info := range entry.AddrInfo {
			addr, err := netip.ParseAddr(info.Local)
			if err != nil {
				continue
			}
			origin := originUnknown
			switch {
			case info.Protocol == "kernel_ra":
				origin = originSLAAC
			case info.Protocol == "kernel_ll":
				origin = originLinkLocal
			case info.Dynamic && addr.Unmap().Is4():
				origin = originDHCP
			case info.Dynamic:
				// IPv6 and dynamic: DHCPv6 or SLAAC, and this output cannot
				// tell them apart. `unknown` is the honest answer.
				origin = originUnknown
			default:
				origin = originStatic
			}
			index.byKey[originKey(entry.IfName, addr)] = origin
		}
	}
	return index
}

func (l *linuxOrigins) Origin(ifName string, addr netip.Addr) string {
	if l == nil || l.byKey == nil {
		return originUnknown
	}
	if origin, ok := l.byKey[originKey(ifName, addr)]; ok {
		return origin
	}
	return originUnknown
}

func originKey(ifName string, addr netip.Addr) string {
	return ifName + "|" + addr.Unmap().WithZone("").String()
}

// ---------------------------------------------------------------------------
// Windows: Get-NetIPAddress PrefixOrigin / SuffixOrigin
// ---------------------------------------------------------------------------

// windowsOriginRows is the Get-NetIPAddress pipeline. It runs inside
// windowsFactsScript's one PowerShell process, as its `origins` member, rather
// than in a process of its own: a Windows PowerShell start is the dominant
// cost of a collection. A compile-time constant with nothing interpolated
// (rule 1 in exec.go).
//
// PowerShell serialises these enums as integers in some versions and as names
// in others, and a mapping that only understood one of the two would silently
// answer `unknown` for a whole Windows fleet. The parser accepts both.
const windowsOriginRows = `Get-NetIPAddress | ForEach-Object { [pscustomobject]@{` +
	`iface=$_.InterfaceAlias;address=[string]$_.IPAddress;` +
	`prefix=[string]$_.PrefixOrigin;suffix=[string]$_.SuffixOrigin } }`

// windowsOrigins indexes Get-NetIPAddress by (interface alias, address).
//
// The mapping, from the NL_PREFIX_ORIGIN / NL_SUFFIX_ORIGIN enumerations:
//
//	PrefixOrigin Dhcp                     -> dhcp
//	PrefixOrigin RouterAdvertisement      -> slaac
//	PrefixOrigin Manual                   -> static
//	PrefixOrigin WellKnown + Suffix Link  -> link_local (also address-derived)
//	anything else, including Other        -> unknown
type windowsOrigins struct {
	byKey map[string]string
}

func parseWindowsOrigins(raw string) *windowsOrigins {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return &windowsOrigins{}
	}
	// ConvertTo-Json emits a bare object rather than an array when there is
	// exactly one result. A machine with one address is not a machine this
	// prober may fail on.
	if strings.HasPrefix(trimmed, "{") {
		trimmed = "[" + trimmed + "]"
	}
	var rows []struct {
		Iface   string          `json:"iface"`
		Address string          `json:"address"`
		Prefix  json.RawMessage `json:"prefix"`
		Suffix  json.RawMessage `json:"suffix"`
	}
	if err := json.Unmarshal([]byte(trimmed), &rows); err != nil {
		return &windowsOrigins{}
	}
	index := &windowsOrigins{byKey: make(map[string]string, len(rows))}
	for _, row := range rows {
		addr, err := netip.ParseAddr(strings.TrimSpace(strings.Split(row.Address, "%")[0]))
		if err != nil {
			continue
		}
		index.byKey[originKey(row.Iface, addr)] =
			windowsOriginFor(enumName(row.Prefix, prefixOriginNames), enumName(row.Suffix, suffixOriginNames))
	}
	return index
}

// prefixOriginNames and suffixOriginNames are the NL_PREFIX_ORIGIN and
// NL_SUFFIX_ORIGIN numeric values, so integer-serialised output maps as well as
// name-serialised output.
var prefixOriginNames = map[int]string{
	0: "Other", 1: "Manual", 2: "WellKnown", 3: "Dhcp", 4: "RouterAdvertisement",
}

var suffixOriginNames = map[int]string{
	0: "Other", 1: "Manual", 2: "WellKnown", 3: "Dhcp", 4: "Link", 5: "Random",
}

// enumName reads a value PowerShell may have written as a name or a number.
func enumName(raw json.RawMessage, names map[int]string) string {
	var asString string
	if err := json.Unmarshal(raw, &asString); err == nil {
		return strings.TrimSpace(asString)
	}
	var asInt int
	if err := json.Unmarshal(raw, &asInt); err == nil {
		return names[asInt]
	}
	return ""
}

func windowsOriginFor(prefix, suffix string) string {
	switch prefix {
	case "Dhcp":
		return originDHCP
	case "RouterAdvertisement":
		return originSLAAC
	case "Manual":
		return originStatic
	case "WellKnown":
		if suffix == "Link" {
			return originLinkLocal
		}
		return originUnknown
	default:
		// "Other" included. Windows uses it for addresses whose origin it does
		// not track, which is precisely the case for `unknown`.
		return originUnknown
	}
}

func (w *windowsOrigins) Origin(ifName string, addr netip.Addr) string {
	if w == nil || w.byKey == nil {
		return originUnknown
	}
	if origin, ok := w.byKey[originKey(ifName, addr)]; ok {
		return origin
	}
	return originUnknown
}

// ---------------------------------------------------------------------------
// macOS: `ipconfig getpacket <if>`
// ---------------------------------------------------------------------------

// darwinOrigins answers DHCP-or-nothing.
//
// `ipconfig getpacket <if>` prints the DHCP packet the interface was configured
// with, and prints nothing at all for an interface that was not. That
// distinguishes `dhcp`, and NOTHING ELSE:
//
//   - A manually configured address produces no packet -- and so does a
//     permission failure, and so does an interface `ipconfig` does not know.
//     `static` is therefore NOT inferred from the absence of a packet. This is
//     the plan's own instruction ("`ipconfig getpacket <if>` for DHCP, else
//     `unknown`") and it is the right one.
//   - IPv6 is not covered at all: SLAAC would need the RA state, and
//     `ipconfig getpacket` says nothing about v6. IPv6 on macOS is `unknown`
//     unless it is link-local, which is address-derived.
//
// So on macOS the honest outcome is: DHCP v4 addresses are `dhcp`, link-local
// is `link_local`, and everything else is `unknown`.
type darwinOrigins struct {
	dhcpByInterface map[string]map[string]bool
}

// parseGetPacket extracts the leased address (`yiaddr = 10.1.2.42`) from one
// interface's packet dump. "" when there is no packet or no yiaddr.
func parseGetPacket(out string) string {
	for _, line := range strings.Split(out, "\n") {
		key, value, ok := strings.Cut(line, "=")
		if !ok || strings.TrimSpace(key) != "yiaddr" {
			continue
		}
		if addr, err := netip.ParseAddr(strings.TrimSpace(value)); err == nil {
			return addr.String()
		}
	}
	return ""
}

func (d *darwinOrigins) Origin(ifName string, addr netip.Addr) string {
	if d == nil || d.dhcpByInterface == nil {
		return originUnknown
	}
	if d.dhcpByInterface[ifName][addr.Unmap().WithZone("").String()] {
		return originDHCP
	}
	return originUnknown
}

// newOriginProber is defined per GOOS (origin_probe_linux.go,
// origin_probe_darwin.go, origin_probe_other.go). It is called once per
// collection, from inside the job, and the process it starts is gone before
// the capability returns. Windows has none: its origins arrive with the facts,
// from the same PowerShell process (windowsOriginRows).
