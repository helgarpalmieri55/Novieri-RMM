//go:build linux

package inventory

import "context"

// newOriginProber runs `ip -j addr show` ONCE and indexes it.
//
// `ip` is not installed everywhere -- it is absent from this repository's own
// build container, and from minimal images generally. run() returns "" for a
// tool that is not on PATH, parseIPAddrJSON turns "" into an empty index, and
// an empty index answers `unknown` for every address. That is the degradation,
// and it is the correct one: nothing is guessed.
//
// The alternative -- AF_NETLINK RTM_GETADDR -- would remove the dependency on
// iproute2 and give IFA_F_PERMANENT and IFA_PROTO directly. It is a few hundred
// lines of message packing with no stdlib support, so it is a deliberate
// follow-on rather than something improvised inside this task.
func newOriginProber(ctx context.Context) originProber {
	return parseIPAddrJSON(run(ctx, "ip", "-j", "addr", "show"))
}
