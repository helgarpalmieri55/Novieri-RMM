//go:build darwin

package inventory

import (
	"context"
	"net"
)

// newOriginProber runs `ipconfig getpacket <if>` once per interface.
//
// One process per interface is more than this file would like, but there is no
// bulk form: `ipconfig` answers about one interface at a time. The cost is
// bounded by the interface count, it happens inside the job, and it is released
// when the job returns.
//
// It answers DHCP-or-nothing. See darwinOrigins for why the absence of a packet
// is NOT read as `static`.
//
// Unexercised: these exec calls. parseGetPacket is unit-tested on linux.
func newOriginProber(ctx context.Context) originProber {
	ifaces, err := net.Interfaces()
	if err != nil {
		return noOriginProber{}
	}
	index := &darwinOrigins{dhcpByInterface: map[string]map[string]bool{}}
	for _, iface := range ifaces {
		if iface.Flags&net.FlagLoopback != 0 {
			continue // ipconfig has nothing to say about lo0
		}
		leased := parseGetPacket(run(ctx, "ipconfig", "getpacket", iface.Name))
		if leased == "" {
			continue
		}
		index.dhcpByInterface[iface.Name] = map[string]bool{leased: true}
	}
	return index
}
