// Package site implements the `site.wake` capability
// (docs/modules/site-server/spec.md §5, docs/kernel/wire-protocol/README.md).
//
// # A site server is a RELAY, not an authority
//
// This is the second capability that acts on something other than the device
// its job is signed for (`net.sweep` is the first), and it is the one where
// that distinction is easiest to erode. A site server does not sign, does not
// verify on another device's behalf, does not enqueue or modify another
// device's job, and does not hold any authorization of its own. It receives a
// signed job addressed to ITSELF and emits one datagram.
//
// The datagram carries NO INSTRUCTION. That is a structural property here
// rather than a promise, and it rests on three things this package enforces:
//
//   - the payload is a pure function of six bytes of MAC address --
//     6×0xFF followed by 16 copies of the target MAC, 102 bytes, and nothing
//     else. No field of the params other than those six bytes can reach the
//     wire. magicPacket is that function and site_test.go asserts it byte for
//     byte against params that vary everything else;
//   - the destination port is 7 or 9 and nothing else. A free port would turn
//     this into a general "send a UDP datagram of my choosing to a broadcast
//     address" primitive, which is a command channel wearing a wake's clothes;
//   - the destination address is the `broadcast` in the signed params,
//     verbatim. Never re-derived, never widened, never read from local
//     configuration -- there is no local configuration to read.
//
// # Invariant #2 is untouched, and this package needed no allowance
//
// The socket is DIALLED, not bound: `net.Dialer.DialContext` with a Control
// hook that sets `SO_BROADCAST` before connect. Nothing accepts, nothing
// listens, and there is no code path from a received byte to an action --
// nothing is ever read from this socket at all.
//
// Note what that means for internal/transport/listen_test.go: `netsweep`
// needed one narrowly scoped allowance there for its raw ICMP socket. THIS
// PACKAGE NEEDED NONE, and the absence of an entry is the assertion. If a
// future change to this package requires adding one, that change is turning a
// send-only capability into a listener and belongs in review, not in a commit.
//
// # The agent never re-derives the scope (§0.2)
//
// `broadcast` is an explicit ADDRESS computed by the control plane from the
// operator-declared `site_server.segments` entry that contains the target. The
// agent refuses a value carrying a prefix length rather than resolving it: an
// authorization revoked in the control plane must never depend on the
// endpoint's arithmetic. Same principle as IPAM's enumerated `targets`.
//
// # Dormant, and no local schedule
//
// Registration is a map insert. Nothing here opens a socket, starts a
// goroutine or takes a timer-wheel entry until a verified job arrives; the
// socket is opened inside the job and closed in a defer when it ends. The
// cadence is server-side for the same reason `net.sweep`'s is -- a locally
// scheduled wake is a standing authorization signed once and replayed for
// weeks. budget_test.go measures all of this rather than describing it.
package site

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/netip"
	"strings"
	"sync/atomic"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// JobType matches app/modules/site_server/handler.py's registration.
const JobType = "site.wake"

// rfc3339 matches app/jobs/canonical.py::rfc3339 -- whole seconds, literal Z.
// SiteWakeResult's `sent_at` is `format: date-time`.
const rfc3339 = "2006-01-02T15:04:05Z"

// Bounds from SiteWakeParams.schema.json, restated here and pinned to that
// file by TestTheParamsBoundsAreTheExportedSchemasBounds.
//
// Duplicated agent-side on purpose, for the same reason netsweep's are: a bound
// that lives only in the server's params model does not travel with the job in
// any form the endpoint can enforce. `port` in particular is the difference
// between a wake capability and an arbitrary-UDP-datagram capability, and it is
// only a bound if the endpoint checks it.
const (
	minRepeat     = 1
	maxRepeat     = 3
	defaultRepeat = 3
	defaultPort   = 9

	// macLen, syncLen and macRepeats are the magic packet, and they are not
	// tunable. 6 bytes of 0xFF followed by the 6-byte MAC sixteen times.
	macLen         = 6
	syncLen        = 6
	macRepeats     = 16
	magicPacketLen = syncLen + macRepeats*macLen // 102

	// repeatInterval is §5.4's "~200 ms apart". Magic packets are lossy UDP with
	// no acknowledgement, so a small fixed repeat is the only reliability
	// available; the SPACING is what makes the repeat worth anything, since
	// three datagrams emitted back to back are lost to the same buffer overrun.
	repeatInterval = 200 * time.Millisecond
)

// wakePorts is the whole permitted destination set. Written as a function of
// the value rather than a map so it allocates nothing on the job path.
func portAllowed(p int) bool { return p == 7 || p == 9 }

// params implements SiteWakeParams.schema.json.
//
// Unknown members are ignored (encoding/json's default): the inbound half of
// docs/kernel/wire-protocol/README.md's rule, so a strictly additive server
// change does not take a deployed fleet offline.
//
// `port` and `repeat` are POINTERS because their schema defaults are not their
// zero values. `port: 0` and "no port" are different statements and only one of
// them is legal; collapsing them would make an absent member read as port 0,
// which portAllowed would then refuse -- turning every wake from a slightly
// older control plane into a refusal.
type params struct {
	TargetMAC string `json:"target_mac"`
	Broadcast string `json:"broadcast"`
	Port      *int   `json:"port"`
	Repeat    *int   `json:"repeat"`
}

// result implements SiteWakeResult.schema.json.
//
// THREE FIELDS, AND THE ABSENCES ARE THE POINT (§0.9). There is no `woke`, no
// reachability probe, no retry count and no per-datagram timing. A magic packet
// is unauthenticated and unacknowledged: there is no reply to wait for, so a
// field claiming the machine came up could not carry true information, and a
// "did it come up?" poll is how this module becomes the monitoring product.
// `SiteWakeResult` is extra="forbid" server-side, so an added field does not
// get ignored -- it fails the whole job at handle_wake_result.
type result struct {
	Sent      bool   `json:"sent"`
	Broadcast string `json:"broadcast"`
	SentAt    string `json:"sent_at"`
}

// Sender is the emit half, job-scoped. One implementation dials a UDP socket;
// tests supply another so the packet, the destination and the repeat arithmetic
// are assertable without putting a datagram on anyone's wire.
//
// It has no Receive. There is nothing to receive.
type Sender interface {
	// Send hands one datagram to the kernel. It reports whether the kernel
	// accepted it -- never whether anything received it, which is not knowable.
	Send(payload []byte) error
	Close() error
}

// Clock is the wake's own clock: spacing the repeats has to sleep, and a test
// must not.
//
// Deliberately NOT scheduler.Clock, which has no Sleep precisely so a polling
// loop cannot be written against it. Sleeping here is bounded by `repeat` (at
// most 3) inside a job that has already been verified, so the total is under
// half a second and cannot be extended by anything on the endpoint.
type Clock interface {
	Now() time.Time
	Sleep(ctx context.Context, d time.Duration) error
}

type realClock struct{}

func (realClock) Now() time.Time { return time.Now() }

func (realClock) Sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// Options wires the capability. Every field has a production default; they
// exist so the packet, the scope check and the repeat arithmetic are testable
// without a socket.
type Options struct {
	OpenSender func(ctx context.Context, dst netip.AddrPort) (Sender, error)
	Clock      Clock
	Logger     *slog.Logger
}

// Service is the process-wide holder. The zero value is usable and is what
// `Process` is: it resolves its own defaults at JOB TIME, so registration stays
// a map insert and nothing here opens a socket while the agent is idle.
type Service struct {
	opts Options
	// runs counts completed wakes, so a dormancy assertion outside this package
	// can observe that this capability has NOT run. A single atomic load.
	runs atomic.Int64
}

// Process is the Service the default capability registry registers.
var Process = &Service{}

// New returns a Service with explicit wiring. Tests use it; production uses
// Process.
func New(o Options) *Service { return &Service{opts: o} }

// Runs reports how many wakes this process has completed.
func (s *Service) Runs() int64 { return s.runs.Load() }

func (s *Service) logger() *slog.Logger {
	if s.opts.Logger != nil {
		return s.opts.Logger
	}
	return slog.Default()
}

func (s *Service) clock() Clock {
	if s.opts.Clock != nil {
		return s.opts.Clock
	}
	return realClock{}
}

// Wake is the `site.wake` capability.
//
// It takes a jobs.VerifiedJob and nothing else (invariant #1): the runner has
// already checked the Ed25519 signature over the canonical bytes against the
// key pinned at enrollment, that the job targets THIS device -- which is the
// site server, never the machine being woken -- that it is fresh, that its
// nonce is unseen, and that the type is registered. There is no second path
// into this function, and this capability adds none: it neither signs nor
// verifies anything on another device's behalf.
func (s *Service) Wake(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	res, err := s.run(ctx, job.Params())
	if err != nil {
		return nil, err
	}
	s.runs.Add(1)
	return res, nil
}

func (s *Service) run(ctx context.Context, raw json.RawMessage) (*result, error) {
	// ---------------------------------------------------------------------
	// Everything above the socket. Nothing below this block runs if any of it
	// fails, and no datagram exists until it has all passed.
	// ---------------------------------------------------------------------
	p, mac, dst, err := parseParams(raw)
	if err != nil {
		return nil, err
	}

	// The payload is built from the MAC and from nothing else. Built BEFORE the
	// socket, so a malformed target cannot get as far as an open socket.
	packet := magicPacket(mac)

	open := s.opts.OpenSender
	if open == nil {
		open = openUDPSender
	}
	sender, err := open(ctx, dst)
	if err != nil {
		// A plain error, so the job is `failed` and not `refused`. The
		// distinction matters: `net.sweep` refuses when it cannot open a raw
		// socket because IPAM takes a refusing device out of sweep rotation for
		// seven days and picks another on the segment. There is no equivalent
		// re-selection for a wake -- §6.3 is explicit that a wake must originate
		// on the site server that serves the segment -- so asking to be taken
		// out of rotation would ask for something nothing implements, and a
		// transient routing failure would read as a permanent incapacity.
		return nil, fmt.Errorf("site.wake: no UDP socket to %s: %w", dst, err)
	}
	defer func() {
		if cerr := sender.Close(); cerr != nil {
			s.logger().Warn("site.wake: closing the broadcast socket", "err", cerr)
		}
	}()

	sentAt, sent := s.emit(ctx, sender, packet, p.repeat())
	if sent == 0 {
		return nil, fmt.Errorf("site.wake: none of %d magic packets to %s were accepted by the "+
			"kernel; reporting sent=true here would be a claim about a datagram that does not "+
			"exist", p.repeat(), dst)
	}

	return &result{
		Sent: true,
		// ECHOED VERBATIM from the signed params, never re-rendered from the
		// parsed address. handle_wake_result compares this against
		// `SiteWakeParams(**job.params).broadcast` and fails the job on any
		// difference; a round trip through netip.Addr is a chance to differ in
		// spelling for no benefit at all.
		Broadcast: p.Broadcast,
		SentAt:    sentAt.UTC().Format(rfc3339),
	}, nil
}

// emit sends the packet `repeat` times, ~200 ms apart, and reports when the
// FIRST datagram was accepted.
//
// `sent_at` is the first datagram's time and not the last: it is the instant
// the wake reached the wire, which is what a maintenance window is measured
// against. The repeats are redundancy for a lossy medium, not separate events.
func (s *Service) emit(ctx context.Context, sender Sender, packet []byte, repeat int) (time.Time, int) {
	clock := s.clock()
	var first time.Time
	sent := 0
	for i := 0; i < repeat; i++ {
		if i > 0 {
			if err := clock.Sleep(ctx, repeatInterval); err != nil {
				// The job's deadline passed between repeats. Whatever already
				// left the host still left it; stopping here is right and
				// reporting what was actually sent is right.
				break
			}
		}
		if err := sender.Send(packet); err != nil {
			// One lost datagram is not a failed wake -- that is what the repeat
			// is for. All of them lost IS, and run() checks that.
			s.logger().Debug("site.wake: datagram not accepted", "attempt", i+1, "err", err)
			continue
		}
		if sent == 0 {
			first = clock.Now()
		}
		sent++
	}
	return first, sent
}

// ---------------------------------------------------------------------------
// The magic packet
// ---------------------------------------------------------------------------

// magicPacket is the whole payload: 6 bytes of 0xFF, then the MAC sixteen
// times. 102 bytes, fixed.
//
// THIS FUNCTION IS THE REASON THE WAKE CARRIES NO INSTRUCTION. Its only input
// is six bytes, so no `broadcast`, no `port`, no `repeat`, no job id, no nonce
// and no unknown params member can appear on the wire. If a future change gives
// it a second parameter, that change has widened what an operator with
// `site.wake` can put on a segment, and it is a security decision.
func magicPacket(mac [macLen]byte) []byte {
	packet := make([]byte, 0, magicPacketLen)
	for i := 0; i < syncLen; i++ {
		packet = append(packet, 0xFF)
	}
	for i := 0; i < macRepeats; i++ {
		packet = append(packet, mac[:]...)
	}
	return packet
}

// ---------------------------------------------------------------------------
// Params
// ---------------------------------------------------------------------------

// parseParams validates everything before anything is opened, and REFUSES
// rather than failing on every violation.
//
// `refused` and not `failed` is deliberate for this class: a params document
// this agent will not act on is not a transient condition, and the reason
// travels into the server's hash-chained audit log through JobEngine.refuse. It
// is the agent's own sentence -- no job-supplied string is formatted into it,
// with the single exception of bounded, already-validated numbers.
func parseParams(raw json.RawMessage) (params, [macLen]byte, netip.AddrPort, error) {
	var (
		p    params
		mac  [macLen]byte
		none netip.AddrPort
	)
	if err := json.Unmarshal(raw, &p); err != nil {
		return p, mac, none, jobs.Refuse("site.wake: params are not a SiteWakeParams")
	}

	mac, err := parseMAC(p.TargetMAC)
	if err != nil {
		return p, mac, none, err
	}

	addr, err := parseBroadcast(p.Broadcast)
	if err != nil {
		return p, mac, none, err
	}

	port := p.port()
	if !portAllowed(port) {
		return p, mac, none, jobs.Refuse("site.wake: destination port %d is not 7 or 9. A free "+
			"destination port turns a wake into a general 'send a UDP datagram of my choosing to "+
			"a broadcast address' primitive; no datagram was sent", port)
	}

	if r := p.repeat(); r < minRepeat || r > maxRepeat {
		return p, mac, none, jobs.Refuse("site.wake: repeat is %d, outside %d..%d — this is a "+
			"wake, not a traffic generator; no datagram was sent", r, minRepeat, maxRepeat)
	}

	return p, mac, netip.AddrPortFrom(addr, uint16(port)), nil
}

// parseMAC accepts every spelling app/modules/site_server/net.py::normalize_mac
// accepts, which is a SUPERSET of the canonical form the server actually sends.
//
// That is deliberate, and it is docs/kernel/wire-protocol/README.md's "loosen
// inbound before you tighten outbound". The server canonicalises to lowercase
// colon form before signing, so the canonical form is what arrives today;
// refusing anything else would turn a harmless server-side rendering change
// into a fleet-wide wake outage that no test in this repository would show,
// because the suite only ever runs one version of each side.
func parseMAC(text string) ([macLen]byte, error) {
	var mac [macLen]byte
	digits := strings.Map(func(r rune) rune {
		switch r {
		case ':', '-', '.', ' ':
			return -1
		}
		return r
	}, strings.ToLower(strings.TrimSpace(text)))

	if len(digits) != macLen*2 {
		return mac, jobs.Refuse("site.wake: target_mac is not 12 hex digits; no datagram was sent")
	}
	for i := 0; i < macLen; i++ {
		hi, ok1 := hexNibble(digits[i*2])
		lo, ok2 := hexNibble(digits[i*2+1])
		if !ok1 || !ok2 {
			return mac, jobs.Refuse("site.wake: target_mac is not 12 hex digits; no datagram was sent")
		}
		mac[i] = hi<<4 | lo
	}
	return mac, nil
}

func hexNibble(c byte) (byte, bool) {
	switch {
	case c >= '0' && c <= '9':
		return c - '0', true
	case c >= 'a' && c <= 'f':
		return c - 'a' + 10, true
	}
	return 0, false
}

// parseBroadcast enforces §0.2 on the endpoint: an explicit ADDRESS, never a
// CIDR and never "the segment".
//
// The refusal on a prefix length is not pedantry about formatting. Accepting
// `10.20.7.0/24` here would mean the agent computes the broadcast address, and
// at that point what the endpoint may touch is decided by the endpoint's
// arithmetic rather than by the control plane's — so an authorization narrowed
// or revoked server-side would not narrow what the datagram reaches. The server
// model refuses it too; both halves refuse it because either half alone is a
// rule that holds only while the other is correct.
func parseBroadcast(text string) (netip.Addr, error) {
	if strings.Contains(text, "/") {
		return netip.Addr{}, jobs.Refuse("site.wake: broadcast carries a prefix length. It must be " +
			"an explicit address the control plane computed from the declared segment; this agent " +
			"does not resolve a segment into an address, because an authorization revoked in the " +
			"control plane must not depend on the endpoint's arithmetic. No datagram was sent")
	}
	addr, err := netip.ParseAddr(text)
	if err != nil {
		return netip.Addr{}, jobs.Refuse("site.wake: broadcast is not an IP address; no datagram was sent")
	}
	if !addr.Is4() {
		// Is4, not Is4In6: a v4-mapped IPv6 spelling is ambiguous and the server
		// never produces one.
		return netip.Addr{}, jobs.Refuse("site.wake: broadcast is not IPv4. A magic packet is " +
			"carried by an IPv4 UDP broadcast and IPv6 has no broadcast address at all — the " +
			"nearest equivalent is a multicast group, which is a different protocol and a " +
			"different authorization question. No datagram was sent")
	}
	if addr.IsUnspecified() {
		return netip.Addr{}, jobs.Refuse("site.wake: broadcast is 0.0.0.0, which is not a " +
			"destination; no datagram was sent")
	}
	return addr, nil
}

func (p params) port() int {
	if p.Port == nil {
		return defaultPort
	}
	return *p.Port
}

func (p params) repeat() int {
	if p.Repeat == nil {
		return defaultRepeat
	}
	return *p.Repeat
}
