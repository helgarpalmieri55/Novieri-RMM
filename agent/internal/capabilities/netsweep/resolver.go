package netsweep

import (
	"context"
	"net"
	"net/netip"
	"time"
)

// Reverse DNS, best-effort (§5.4).
//
// `hostname` is DISPLAY-ONLY. It is never a reconciliation predicate and never
// matches an address to a device: identity comes from MACs and from inventory,
// both of which have a chain of custody, and a PTR record is a string the owner
// of the address chose. A failure yields "" and the member is omitted, which is
// the schema's `null`.
//
// The timeout is short and per-address on purpose. A segment with a broken
// resolver is common, and a sweep that spent its `budget_s` waiting on PTR
// lookups would return `truncated: true` for a reason that has nothing to do
// with the network it was asked to probe.
const reverseTimeout = 300 * time.Millisecond

type dnsResolver struct{ r net.Resolver }

// defaultResolver is what the registered capability uses.
//
// It is constructed per job (see Service.reverse) rather than held: a resolver
// holds no state worth keeping between sweeps, and §5.5's "no cache" is easier
// to keep true than to audit.
func defaultResolver() Resolver { return &dnsResolver{} }

func (d *dnsResolver) Hostname(ctx context.Context, addr netip.Addr) string {
	ctx, cancel := context.WithTimeout(ctx, reverseTimeout)
	defer cancel()
	names, err := d.r.LookupAddr(ctx, addr.String())
	if err != nil || len(names) == 0 {
		return ""
	}
	return d.clampName(names[0])
}

// clampName strips the trailing dot a PTR record carries and refuses one above
// the schema's 255-character bound.
//
// Refuses rather than truncates: a truncated hostname is a WRONG hostname, and
// this field exists only to be read by a human. An absent one says "we do not
// know"; a truncated one says something false. The document's other fields are
// worth more than this one, and an over-length value costs all of them.
func (d *dnsResolver) clampName(name string) string {
	if n := len(name); n > 0 && name[n-1] == '.' {
		name = name[:n-1]
	}
	if len(name) > 255 {
		return ""
	}
	return name
}
