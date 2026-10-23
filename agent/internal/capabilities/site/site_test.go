package site

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/netip"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/scheduler"
)

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// spySender records every datagram AND every destination.
//
// The DESTINATION is as much the subject of this file as the payload. §0.2's
// rule is about where the packet lands, and an implementation that built a
// perfect magic packet and sent it to a re-derived address would satisfy every
// payload assertion while doing the one thing the rule forbids.
type spySender struct {
	mu      sync.Mutex
	dst     netip.AddrPort
	sent    [][]byte
	opens   int
	closes  int
	sendErr error
}

func (s *spySender) open(_ context.Context, dst netip.AddrPort) (Sender, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.opens++
	s.dst = dst
	return s, nil
}

func (s *spySender) Send(payload []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.sendErr != nil {
		return s.sendErr
	}
	s.sent = append(s.sent, append([]byte(nil), payload...))
	return nil
}

func (s *spySender) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closes++
	return nil
}

func (s *spySender) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.sent)
}

// fakeClock never really sleeps and records what it was asked to sleep for.
type fakeClock struct {
	mu     sync.Mutex
	now    time.Time
	slept  []time.Duration
	failAt int // 1-based: the nth Sleep returns an error (a deadline passing)
	calls  int
}

func newFakeClock() *fakeClock {
	return &fakeClock{now: time.Date(2026, 8, 6, 12, 0, 0, 0, time.UTC)}
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Sleep(_ context.Context, d time.Duration) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.calls++
	if c.failAt != 0 && c.calls >= c.failAt {
		return context.DeadlineExceeded
	}
	c.slept = append(c.slept, d)
	c.now = c.now.Add(d)
	return nil
}

// wakeWith builds a Service with a spy sender and a fake clock.
func wakeWith(t *testing.T, spy *spySender) (*Service, *fakeClock) {
	t.Helper()
	clk := newFakeClock()
	return New(Options{OpenSender: spy.open, Clock: clk, Logger: quiet()}), clk
}

// paramsJSON builds a SiteWakeParams document. `extra` members go in verbatim,
// so a test can send an unknown member or an out-of-range number.
func paramsJSON(t *testing.T, mac, broadcast string, extra map[string]any) json.RawMessage {
	t.Helper()
	doc := map[string]any{"target_mac": mac, "broadcast": broadcast}
	for k, v := range extra {
		if v == nil {
			delete(doc, k)
			continue
		}
		doc[k] = v
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

const (
	testMAC       = "a4:bb:6d:11:22:33"
	testBroadcast = "10.20.7.255"
)

func wantMAC() [macLen]byte { return [macLen]byte{0xa4, 0xbb, 0x6d, 0x11, 0x22, 0x33} }

// ---------------------------------------------------------------------------
// Invariant #1: the capability is reachable only through the signed-job runner
// ---------------------------------------------------------------------------

func TestTheCapabilityTakesAVerifiedJobAndNothingElse(t *testing.T) {
	// A compile-time fact written as a test so its failure names the reason:
	// jobs.VerifiedJob is an interface with an unexported method and no exported
	// implementation, so Wake is unreachable except through the signed-job
	// runner. A site server is a RELAY -- it never signs and never verifies on
	// another device's behalf -- and this is the structural half of that claim.
	var _ jobs.Handler = Process.Wake
}

// ---------------------------------------------------------------------------
// The packet carries no instruction
// ---------------------------------------------------------------------------

func TestTheMagicPacketIsSixSyncBytesAndSixteenCopiesOfTheMAC(t *testing.T) {
	// FALSIFYING OBSERVATION: the bytes. Not a length, not "no error".
	packet := magicPacket(wantMAC())
	if len(packet) != magicPacketLen {
		t.Fatalf("packet is %d bytes, want %d", len(packet), magicPacketLen)
	}
	want := bytes.Repeat([]byte{0xFF}, syncLen)
	want = append(want, bytes.Repeat([]byte{0xa4, 0xbb, 0x6d, 0x11, 0x22, 0x33}, macRepeats)...)
	if !bytes.Equal(packet, want) {
		t.Fatalf("packet =\n %x\nwant\n %x", packet, want)
	}
}

func TestNothingButTheMACCanReachTheWire(t *testing.T) {
	// THE CENTRAL SECURITY ASSERTION OF THIS PACKAGE. The magic packet is a wake
	// SIGNAL, not a command channel, and that is true only if no attacker- or
	// operator-chosen byte other than the six MAC bytes can appear in it.
	//
	// FALSIFYING OBSERVATION: two wakes with the same MAC and everything else
	// different producing different bytes, or any params value appearing as a
	// substring of the payload.
	base := paramsJSON(t, testMAC, "10.20.7.255", map[string]any{"port": 9, "repeat": 1})
	other := paramsJSON(t, "A4-BB-6D-11-22-33", "192.168.44.255", map[string]any{
		"port": 7, "repeat": 3,
		// An unknown member carrying a payload an attacker would love to place
		// on a broadcast segment. It must be ignored, not forwarded.
		"payload": "GET /admin HTTP/1.1\r\n\r\n",
	})

	var packets [][]byte
	for _, raw := range []json.RawMessage{base, other} {
		spy := &spySender{}
		s, _ := wakeWith(t, spy)
		if _, err := s.run(context.Background(), raw); err != nil {
			t.Fatal(err)
		}
		if spy.count() == 0 {
			t.Fatal("nothing was sent; this test would be vacuous")
		}
		packets = append(packets, spy.sent[0])
	}
	if !bytes.Equal(packets[0], packets[1]) {
		t.Fatalf("two wakes for the same MAC produced different payloads:\n %x\n %x",
			packets[0], packets[1])
	}
	for _, needle := range []string{"192.168.44.255", "10.20.7.255", "GET /admin", "HTTP"} {
		if bytes.Contains(packets[1], []byte(needle)) {
			t.Fatalf("the payload carries %q; the magic packet must be a pure function of the "+
				"six MAC bytes", needle)
		}
	}
	// And every repeat is the same bytes -- a repeat that varied would be a
	// channel with a per-datagram degree of freedom.
	spy := &spySender{}
	s, _ := wakeWith(t, spy)
	if _, err := s.run(context.Background(), paramsJSON(t, testMAC, testBroadcast,
		map[string]any{"repeat": 3})); err != nil {
		t.Fatal(err)
	}
	for i, p := range spy.sent {
		if !bytes.Equal(p, packets[0]) {
			t.Fatalf("repeat %d differs from the first datagram:\n %x", i, p)
		}
	}
}

// ---------------------------------------------------------------------------
// §0.2: the endpoint never decides what it may touch
// ---------------------------------------------------------------------------

func TestTheDatagramGoesToExactlyTheSignedBroadcastAndPort(t *testing.T) {
	for _, tc := range []struct {
		broadcast string
		port      any
		want      string
	}{
		{"10.20.7.255", nil, "10.20.7.255:9"},
		{"192.168.44.255", 7, "192.168.44.255:7"},
		{"255.255.255.255", 9, "255.255.255.255:9"},
		// A /32 segment's "broadcast" is the host's own address. The server
		// computes it and says so (net.py::broadcast_for); the agent sends there
		// and does not widen it to some enclosing prefix.
		{"10.20.7.44", nil, "10.20.7.44:9"},
	} {
		t.Run(tc.broadcast, func(t *testing.T) {
			spy := &spySender{}
			s, _ := wakeWith(t, spy)
			if _, err := s.run(context.Background(),
				paramsJSON(t, testMAC, tc.broadcast, map[string]any{"port": tc.port})); err != nil {
				t.Fatal(err)
			}
			if got := spy.dst.String(); got != tc.want {
				t.Fatalf("dialled %s, want %s", got, tc.want)
			}
		})
	}
}

func TestABroadcastCarryingAPrefixLengthIsRefusedBeforeAnyDatagramIsSent(t *testing.T) {
	// §0.2. Accepting a CIDR here would mean the AGENT computes the broadcast
	// address, and at that point what the endpoint may touch is decided by the
	// endpoint's arithmetic rather than the control plane's.
	//
	// FALSIFYING OBSERVATION: the send counter, not the returned error. An
	// implementation that returned the right error and emitted the datagram
	// anyway would satisfy an error-only assertion while having done the one
	// thing the rule forbids.
	for _, bad := range []string{"10.20.7.0/24", "10.20.7.255/32", "10.20.7.0/0"} {
		t.Run(bad, func(t *testing.T) {
			spy := &spySender{}
			s, _ := wakeWith(t, spy)
			_, err := s.run(context.Background(), paramsJSON(t, testMAC, bad, nil))
			assertRefused(t, err, "prefix length")
			if spy.opens != 0 {
				t.Fatalf("a socket was opened %d times for a CIDR broadcast", spy.opens)
			}
			if n := spy.count(); n != 0 {
				t.Fatalf("%d datagrams were sent for a CIDR broadcast", n)
			}
		})
	}
}

func TestANonIPv4BroadcastIsRefusedBeforeAnyDatagramIsSent(t *testing.T) {
	// A magic packet is carried by an IPv4 UDP broadcast; IPv6 has no broadcast
	// address at all. The server's parse_segment refuses an IPv6 prefix by name
	// rather than producing an address that cannot carry the packet, and the
	// agent refuses the same input for the same reason.
	for _, bad := range []string{"ff02::1", "::1", "::ffff:10.20.7.255", "0.0.0.0", "not-an-address", ""} {
		t.Run(bad, func(t *testing.T) {
			spy := &spySender{}
			s, _ := wakeWith(t, spy)
			_, err := s.run(context.Background(), paramsJSON(t, testMAC, bad, nil))
			assertRefused(t, err, "")
			if spy.opens != 0 || spy.count() != 0 {
				t.Fatalf("opened %d sockets and sent %d datagrams for broadcast %q",
					spy.opens, spy.count(), bad)
			}
		})
	}
}

func TestAPortOutsideSevenAndNineIsRefusedBeforeAnyDatagramIsSent(t *testing.T) {
	// The port bound is the difference between a wake capability and a general
	// "send a UDP datagram of my choosing to a broadcast address" primitive, and
	// it is only a bound if the ENDPOINT checks it: the server's Literal[7, 9]
	// does not travel with the job in any form the agent can enforce.
	//
	// FALSIFYING OBSERVATION: the send counter. 4011 and 69 are in the list on
	// purpose -- they are proxyDHCP and TFTP, the two ports a PXE responder
	// would use, and a wake that could reach them is the deferred PXE slice
	// arriving through the back door.
	for _, bad := range []int{0, 1, 8, 22, 69, 4011, 65535, -1} {
		t.Run(fmt.Sprint(bad), func(t *testing.T) {
			spy := &spySender{}
			s, _ := wakeWith(t, spy)
			_, err := s.run(context.Background(),
				paramsJSON(t, testMAC, testBroadcast, map[string]any{"port": bad}))
			assertRefused(t, err, "not 7 or 9")
			if spy.opens != 0 || spy.count() != 0 {
				t.Fatalf("opened %d sockets and sent %d datagrams for port %d",
					spy.opens, spy.count(), bad)
			}
		})
	}
}

func TestRepeatOutsideItsSignedBoundsIsRefusedBeforeAnyDatagramIsSent(t *testing.T) {
	for _, bad := range []int{0, -1, 4, 100, 1000000} {
		t.Run(fmt.Sprint(bad), func(t *testing.T) {
			spy := &spySender{}
			s, _ := wakeWith(t, spy)
			_, err := s.run(context.Background(),
				paramsJSON(t, testMAC, testBroadcast, map[string]any{"repeat": bad}))
			assertRefused(t, err, "repeat")
			if spy.count() != 0 {
				t.Fatalf("%d datagrams sent for repeat %d", spy.count(), bad)
			}
		})
	}
}

func TestAMalformedTargetMACIsRefusedBeforeAnyDatagramIsSent(t *testing.T) {
	for _, bad := range []string{"", "a4:bb:6d:11:22", "a4:bb:6d:11:22:33:44", "zz:bb:6d:11:22:33",
		"a4bb6d1122", "a4:bb:6d:11:22:3g", "10.20.7.255", "a4:bb:6d:11:22:33:44:55"} {
		t.Run(bad, func(t *testing.T) {
			spy := &spySender{}
			s, _ := wakeWith(t, spy)
			_, err := s.run(context.Background(), paramsJSON(t, bad, testBroadcast, nil))
			assertRefused(t, err, "target_mac")
			if spy.opens != 0 || spy.count() != 0 {
				t.Fatalf("opened %d sockets and sent %d datagrams for MAC %q",
					spy.opens, spy.count(), bad)
			}
		})
	}
}

func TestEveryMACSpellingTheServerAcceptsProducesTheSameBytes(t *testing.T) {
	// "Loosen inbound before you tighten outbound"
	// (docs/kernel/wire-protocol/README.md). The server canonicalises to
	// lowercase colon form before signing, so that is what arrives TODAY.
	// Refusing anything else would turn a harmless server-side rendering change
	// into a fleet-wide wake outage that no test in this repository could show,
	// because the suite only ever runs one version of each side.
	want := magicPacket(wantMAC())
	for _, spelling := range []string{
		"a4:bb:6d:11:22:33", "A4-BB-6D-11-22-33", "a4bb.6d11.2233", "A4BB6D112233",
		"  a4:bb:6d:11:22:33  ",
	} {
		t.Run(spelling, func(t *testing.T) {
			spy := &spySender{}
			s, _ := wakeWith(t, spy)
			if _, err := s.run(context.Background(),
				paramsJSON(t, spelling, testBroadcast, map[string]any{"repeat": 1})); err != nil {
				t.Fatal(err)
			}
			if spy.count() != 1 || !bytes.Equal(spy.sent[0], want) {
				t.Fatalf("spelling %q produced %d datagrams; first = %x", spelling, spy.count(),
					firstOrNil(spy.sent))
			}
		})
	}
}

func TestTheAcceptedMACSetIsTheServersAcceptedMACSetIncludingItsOneQUIRK(t *testing.T) {
	// FOUND BY THIS TEST FILE, NOT REASONED ABOUT, and recorded rather than
	// quietly fixed on one side.
	//
	// `normalize_mac` strips ':', '-', '.' and ' ' and then requires 12 hex
	// digits. `255.255.255.255` survives that: it strips to `255255255255`,
	// which is twelve characters and every one of them a legal hex digit. The
	// SERVER canonicalises it to `25:52:55:25:52:55` and signs that. This agent
	// accepts the same input and builds a magic packet for the same bogus MAC.
	//
	// It is left matching, deliberately. Refusing it here would be TIGHTENING
	// INBOUND -- the agent rejecting a params document the control plane
	// considers valid and has already signed and audited -- and the divergence
	// would be a silent behavioural difference between the two halves that no
	// test in this repository could show, because the suite only ever runs one
	// version of each side. The harm of accepting it is nil: a magic packet
	// addressed to a MAC no NIC has wakes nothing. The harm of the two halves
	// disagreeing about what a valid MAC is does not stay nil.
	//
	// If this is ever narrowed, narrow the SERVER's normalize_mac first and the
	// agent second, in that order.
	spy := &spySender{}
	s, _ := wakeWith(t, spy)
	if _, err := s.run(context.Background(),
		paramsJSON(t, "255.255.255.255", testBroadcast, map[string]any{"repeat": 1})); err != nil {
		t.Fatalf("the agent refuses a MAC the server's normalize_mac accepts and signs: %v", err)
	}
	want := magicPacket([macLen]byte{0x25, 0x52, 0x55, 0x25, 0x52, 0x55})
	if spy.count() != 1 || !bytes.Equal(spy.sent[0], want) {
		t.Fatalf("the agent and the server disagree about how to canonicalise %q", "255.255.255.255")
	}
}

// ---------------------------------------------------------------------------
// Defaults, unknown members, repeats
// ---------------------------------------------------------------------------

func TestAnAbsentPortOrRepeatIsItsSchemaDefaultAndNotZero(t *testing.T) {
	// `port: 0` and "no port" are different statements and only one is legal.
	// Collapsing them (a plain int rather than a pointer) would make an absent
	// member read as port 0, which portAllowed refuses -- so every wake from a
	// control plane that omits the optional member would become a refusal.
	spy := &spySender{}
	s, _ := wakeWith(t, spy)
	if _, err := s.run(context.Background(), paramsJSON(t, testMAC, testBroadcast, nil)); err != nil {
		t.Fatal(err)
	}
	if got := spy.dst.Port(); got != defaultPort {
		t.Fatalf("absent port dialled :%d, want :%d", got, defaultPort)
	}
	if got := spy.count(); got != defaultRepeat {
		t.Fatalf("absent repeat sent %d datagrams, want %d", got, defaultRepeat)
	}
}

func TestUnknownParamsMembersAreIgnoredRatherThanRefused(t *testing.T) {
	// The inbound half of the wire-protocol rule: a strictly ADDITIVE server
	// change must not take a deployed fleet offline. An agent that refused an
	// unknown member would make every additive params change a fleet outage.
	spy := &spySender{}
	s, _ := wakeWith(t, spy)
	res, err := s.run(context.Background(), paramsJSON(t, testMAC, testBroadcast,
		map[string]any{"repeat": 1, "interface": "eth0", "vlan": 7, "future": []int{1, 2}}))
	if err != nil {
		t.Fatalf("an unknown params member was refused: %v", err)
	}
	if !res.Sent || spy.count() != 1 {
		t.Fatalf("res = %+v, %d datagrams", res, spy.count())
	}
}

func TestRepeatSendsThatManyDatagramsSpacedTwoHundredMillisApart(t *testing.T) {
	for repeat := minRepeat; repeat <= maxRepeat; repeat++ {
		t.Run(fmt.Sprint(repeat), func(t *testing.T) {
			spy := &spySender{}
			s, clk := wakeWith(t, spy)
			if _, err := s.run(context.Background(),
				paramsJSON(t, testMAC, testBroadcast, map[string]any{"repeat": repeat})); err != nil {
				t.Fatal(err)
			}
			if spy.count() != repeat {
				t.Fatalf("repeat %d sent %d datagrams", repeat, spy.count())
			}
			// N datagrams, N-1 gaps. Three back-to-back datagrams are lost to
			// the same buffer overrun, so the SPACING is what the repeat is
			// worth anything for.
			if len(clk.slept) != repeat-1 {
				t.Fatalf("repeat %d slept %d times, want %d", repeat, len(clk.slept), repeat-1)
			}
			for _, d := range clk.slept {
				if d != repeatInterval {
					t.Fatalf("slept %v between datagrams, want %v", d, repeatInterval)
				}
			}
		})
	}
}

func TestATotalWakeIsBoundedByTheSignedRepeat(t *testing.T) {
	// The only sleeping this capability does is between repeats, so the whole
	// job is bounded by (repeat-1) × 200 ms and cannot be extended by anything
	// on the endpoint. That bound is what keeps this well inside the 300 s TTL
	// §0.10 pins.
	spy := &spySender{}
	s, clk := wakeWith(t, spy)
	if _, err := s.run(context.Background(),
		paramsJSON(t, testMAC, testBroadcast, map[string]any{"repeat": maxRepeat})); err != nil {
		t.Fatal(err)
	}
	var total time.Duration
	for _, d := range clk.slept {
		total += d
	}
	if max := time.Duration(maxRepeat-1) * repeatInterval; total > max {
		t.Fatalf("the wake slept %v in total, above the %v the signed bounds permit", total, max)
	}
}

// ---------------------------------------------------------------------------
// The result (§0.9)
// ---------------------------------------------------------------------------

func TestTheResultCarriesExactlySentBroadcastAndSentAt(t *testing.T) {
	// `SiteWakeResult` is extra="forbid" server-side and handle_wake_result
	// raises SiteWakeRejected on a ValidationError, which fails the WHOLE job.
	// One extra key costs the wake.
	//
	// FALSIFYING OBSERVATION: the encoded key set. In particular there is no
	// `woke` -- a magic packet is unacknowledged, so the field could not carry
	// true information -- and no probe, retry count or per-datagram timing.
	spy := &spySender{}
	s, clk := wakeWith(t, spy)
	res, err := s.run(context.Background(), paramsJSON(t, testMAC, testBroadcast, nil))
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(res)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]json.RawMessage
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	keys := make([]string, 0, len(got))
	for k := range got {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	if strings.Join(keys, ",") != "broadcast,sent,sent_at" {
		t.Fatalf("encoded result keys = %v; want [broadcast sent sent_at]", keys)
	}
	for _, forbidden := range []string{"woke", "reachable", "retries", "rtt_ms", "target_mac"} {
		if _, ok := got[forbidden]; ok {
			t.Fatalf("the result carries %q (§0.9); %s", forbidden, raw)
		}
	}
	if want := clk.now.UTC().Format(rfc3339); res.SentAt != want {
		t.Fatalf("sent_at = %q, want %q (the FIRST datagram's time)", res.SentAt, want)
	}
	if !strings.HasSuffix(res.SentAt, "Z") || strings.Contains(res.SentAt, ".") {
		t.Fatalf("sent_at = %q; whole seconds, literal Z (app/jobs/canonical.py::rfc3339)", res.SentAt)
	}
}

func TestTheResultEchoesTheSignedBroadcastVerbatim(t *testing.T) {
	// handle_wake_result compares this against
	// `SiteWakeParams(**job.params).broadcast` and fails the job on any
	// difference. Echoing the received string rather than re-rendering the
	// parsed address removes the whole class of spelling drift.
	//
	// FALSIFYING OBSERVATION: a result whose broadcast differs by one byte from
	// what was signed.
	for _, addr := range []string{"10.20.7.255", "192.168.44.255", "255.255.255.255", "10.20.7.44"} {
		spy := &spySender{}
		s, _ := wakeWith(t, spy)
		res, err := s.run(context.Background(), paramsJSON(t, testMAC, addr, nil))
		if err != nil {
			t.Fatal(err)
		}
		if res.Broadcast != addr {
			t.Fatalf("signed broadcast %q came back as %q; handle_wake_result would fail the job",
				addr, res.Broadcast)
		}
	}
}

func TestAWakeThatSentNothingFailsRatherThanReportingSent(t *testing.T) {
	// `sent: true` is a claim that a datagram left this host. If the kernel
	// accepted none of them, a succeeded job carrying sent=true is a lie the
	// server has no way to check -- there is no acknowledgement to compare it
	// against. Failing is the only honest answer.
	//
	// FALSIFYING OBSERVATION: a non-nil result.
	spy := &spySender{sendErr: errors.New("network is unreachable")}
	s, _ := wakeWith(t, spy)
	res, err := s.run(context.Background(), paramsJSON(t, testMAC, testBroadcast, nil))
	if err == nil {
		t.Fatalf("res = %+v; a wake that sent nothing must not succeed", res)
	}
	if res != nil {
		t.Fatalf("res = %+v alongside err = %v", res, err)
	}
	// `failed`, not `refused`: there is no re-selection onto another site server
	// for a wake (§6.3 -- a wake must originate on the segment), so asking to be
	// taken out of rotation asks for something nothing implements.
	var refusal *jobs.Refusal
	if errors.As(err, &refusal) {
		t.Fatalf("a send failure refused rather than failed: %v", err)
	}
	if spy.closes != 1 {
		t.Fatalf("the socket was closed %d times after a failed wake; want 1", spy.closes)
	}
}

func TestOneLostDatagramIsNotAFailedWake(t *testing.T) {
	// The repeat exists because UDP broadcast is lossy. If a single failed
	// sendto() failed the job, the redundancy would be worse than useless.
	spy := &spySender{}
	failing := &flakySender{inner: spy, failOn: map[int]bool{1: true, 3: true}}
	clk := newFakeClock()
	s := New(Options{
		OpenSender: func(_ context.Context, dst netip.AddrPort) (Sender, error) {
			return failing, nil
		},
		Clock: clk, Logger: quiet(),
	})
	res, err := s.run(context.Background(),
		paramsJSON(t, testMAC, testBroadcast, map[string]any{"repeat": 3}))
	if err != nil {
		t.Fatalf("two of three datagrams failed and the wake did not survive it: %v", err)
	}
	if !res.Sent {
		t.Fatal("res.Sent = false after one datagram was accepted")
	}
	if spy.count() != 1 {
		t.Fatalf("%d datagrams reached the spy, want 1", spy.count())
	}
}

func TestADeadlineBetweenRepeatsReportsWhatWasActuallySent(t *testing.T) {
	// A job whose context expires between repeats has still put datagrams on the
	// wire. Stopping is right; claiming nothing happened would be wrong.
	spy := &spySender{}
	clk := newFakeClock()
	clk.failAt = 1 // the first inter-datagram sleep fails
	s := New(Options{OpenSender: spy.open, Clock: clk, Logger: quiet()})
	res, err := s.run(context.Background(),
		paramsJSON(t, testMAC, testBroadcast, map[string]any{"repeat": 3}))
	if err != nil {
		t.Fatal(err)
	}
	if spy.count() != 1 {
		t.Fatalf("%d datagrams sent after the deadline passed between repeats, want 1", spy.count())
	}
	if !res.Sent {
		t.Fatal("res.Sent = false although one datagram was accepted")
	}
	if spy.closes != 1 {
		t.Fatalf("socket closed %d times, want 1", spy.closes)
	}
}

// ---------------------------------------------------------------------------
// Dormancy and invariant #2
// ---------------------------------------------------------------------------

func TestTheSocketIsOpenedInsideTheJobAndClosedWhenItEnds(t *testing.T) {
	spy := &spySender{}
	s, _ := wakeWith(t, spy)
	if spy.opens != 0 {
		t.Fatalf("constructing the capability opened %d sockets", spy.opens)
	}
	for i := 1; i <= 3; i++ {
		if _, err := s.run(context.Background(), paramsJSON(t, testMAC, testBroadcast, nil)); err != nil {
			t.Fatal(err)
		}
		if spy.opens != i || spy.closes != i {
			t.Fatalf("after %d wakes: %d opens, %d closes; one socket per job, closed in a defer",
				i, spy.opens, spy.closes)
		}
	}
}

func TestTheCapabilityAddsNothingToTheTimerWheel(t *testing.T) {
	// §5.4's first falsifier: "a socket or timer-wheel entry resident on an
	// agent with no wake job in flight". The cadence is server-side; a locally
	// scheduled wake is a standing authorization signed once and replayed for
	// weeks.
	//
	// FALSIFYING OBSERVATION: a wheel entry after building the capability, or
	// after running a wake.
	w := scheduler.New(scheduler.NewFake(time.Now()), quiet())
	before := w.Len()
	spy := &spySender{}
	s, _ := wakeWith(t, spy)
	if w.Len() != before {
		t.Fatalf("building the capability put %d entries on the wheel", w.Len()-before)
	}
	if _, err := s.run(context.Background(), paramsJSON(t, testMAC, testBroadcast, nil)); err != nil {
		t.Fatal(err)
	}
	if w.Len() != before {
		t.Fatalf("running a wake put %d entries on the wheel", w.Len()-before)
	}
}

func TestNothingIsOpenedBeforeAJobArrivesAndEverythingIsReleasedAfter(t *testing.T) {
	// §5.4's two falsifiers, against the REAL socket rather than a spy.
	//
	// FALSIFYING OBSERVATION: a leaked file descriptor, NAMED, or a goroutine
	// that outlives the job.
	//
	// The destination is 127.255.255.255 -- the broadcast address of the
	// loopback network. It exercises the real dial, the real SO_BROADCAST
	// setsockopt and a real sendto() without putting a single byte on any
	// segment outside this container.
	if runtime.GOOS != "linux" {
		t.Skip("reads /proc/self/fd")
	}
	settle()
	fdsBefore := openFDs(t)
	goBefore := runtime.NumGoroutine()

	s := New(Options{Logger: quiet()})
	settle()
	if extra := newFDs(fdsBefore, openFDs(t)); len(extra) != 0 {
		t.Fatalf("constructing the capability opened %v", extra)
	}
	if s.Runs() != 0 {
		t.Fatalf("the capability had already run %d times", s.Runs())
	}

	res, err := s.run(context.Background(),
		paramsJSON(t, testMAC, "127.255.255.255", map[string]any{"repeat": 1}))
	if err != nil {
		// A container without a route to the loopback broadcast is a real
		// possibility. Say so rather than skipping silently: the fd assertions
		// below still apply, to the failure path instead.
		t.Logf("the real wake failed here (the fd assertions below now cover the failure "+
			"path): %v", err)
	} else {
		t.Logf("real magic packet to 127.255.255.255:9 -> sent=%v broadcast=%s sent_at=%s",
			res.Sent, res.Broadcast, res.SentAt)
	}

	settle()
	if extra := newFDs(fdsBefore, openFDs(t)); len(extra) != 0 {
		t.Fatalf("the wake leaked file descriptor(s): %v", extra)
	}
	if after := runtime.NumGoroutine(); after > goBefore {
		t.Fatalf("the wake left %d goroutine(s) running; this capability starts none",
			after-goBefore)
	}
}

func TestARealWakeBindsNoListeningPort(t *testing.T) {
	// Invariant #2 at the running-process level. The source-level guard is
	// internal/transport/listen_test.go's walker, and THIS PACKAGE HAS NO
	// ALLOWANCE THERE -- the socket is dialled, not bound, so no exemption was
	// needed and none was taken.
	if runtime.GOOS != "linux" {
		t.Skip("reads /proc/net/tcp")
	}
	before := listeningPorts(t)
	s := New(Options{Logger: quiet()})
	if _, err := s.run(context.Background(),
		paramsJSON(t, testMAC, "127.255.255.255", map[string]any{"repeat": 1})); err != nil {
		t.Logf("real wake failed (the listener assertion below still applies): %v", err)
	}
	if extra := newFDs(before, listeningPorts(t)); len(extra) != 0 {
		t.Fatalf("a wake bound %v; invariant #2 forbids any listening port", extra)
	}
}

func TestTheWakeSocketIsConnectedAndReadsNothing(t *testing.T) {
	// The reason this package needed no allowance in listen_test.go: a DIALLED
	// socket is connected, so the kernel drops any datagram whose source is not
	// the address we connected to -- and nothing in this package ever reads from
	// it in any case.
	//
	// FALSIFYING OBSERVATION: a Sender implementation with a Read method, or a
	// site.go that calls one. Asserted structurally: the Sender interface a
	// capability can use has exactly Send and Close.
	typ := fmt.Sprintf("%T", (*Sender)(nil))
	if typ == "" {
		t.Fatal("unreachable")
	}
	var s Sender = &spySender{}
	if _, ok := s.(interface{ Read([]byte) (int, error) }); ok {
		t.Fatal("a Sender exposes Read; there must be no code path from a received byte to an action")
	}
	if _, ok := s.(interface {
		ReadFrom([]byte) (int, netip.AddrPort, error)
	}); ok {
		t.Fatal("a Sender exposes ReadFrom")
	}
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// flakySender fails the nth Send and forwards the rest.
type flakySender struct {
	inner  *spySender
	failOn map[int]bool
	n      int
}

func (f *flakySender) Send(payload []byte) error {
	f.n++
	if f.failOn[f.n] {
		return errors.New("sendto: no buffer space available")
	}
	return f.inner.Send(payload)
}

func (f *flakySender) Close() error { return f.inner.Close() }

func assertRefused(t *testing.T, err error, contains string) {
	t.Helper()
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("err = %v (%T); want a jobs.Refusal so the reason reaches the audit log", err, err)
	}
	if contains != "" && !strings.Contains(refusal.Reason, contains) {
		t.Fatalf("refusal reason = %q; want it to name %q", refusal.Reason, contains)
	}
}

func firstOrNil(b [][]byte) []byte {
	if len(b) == 0 {
		return nil
	}
	return b[0]
}

func settle() {
	for i := 0; i < 50; i++ {
		runtime.Gosched()
	}
	time.Sleep(50 * time.Millisecond)
}

func openFDs(t *testing.T) map[string]bool {
	t.Helper()
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatalf("cannot read /proc/self/fd: %v", err)
	}
	out := map[string]bool{}
	for _, e := range entries {
		target, err := os.Readlink(filepath.Join("/proc/self/fd", e.Name()))
		if err != nil {
			continue
		}
		// Keyed by TARGET, not by descriptor number: numbers are reused, and a
		// leak that happens to land on a recycled number would be invisible.
		out[target] = true
	}
	return out
}

func newFDs(before, after map[string]bool) []string {
	var extra []string
	for k := range after {
		if !before[k] {
			extra = append(extra, k)
		}
	}
	sort.Strings(extra)
	return extra
}

// listeningPorts is the inode-scoped /proc/net/tcp reader, mirrored from
// internal/transport/listen_test.go for the same reason netsweep's copy is:
// moving it into non-test code to share it would put a /proc reader in the
// shipped binary for the sake of a test.
func listeningPorts(t *testing.T) map[string]bool {
	t.Helper()
	mine := map[string]bool{}
	for target := range openFDs(t) {
		if rest, ok := strings.CutPrefix(target, "socket:["); ok {
			mine[strings.TrimSuffix(rest, "]")] = true
		}
	}
	out := map[string]bool{}
	for _, path := range []string{"/proc/net/tcp", "/proc/net/tcp6"} {
		f, err := os.Open(path)
		if err != nil {
			continue
		}
		sc := bufio.NewScanner(f)
		sc.Scan() // header
		for sc.Scan() {
			fields := strings.Fields(sc.Text())
			if len(fields) < 10 || fields[3] != "0A" || !mine[fields[9]] {
				continue
			}
			local := fields[1]
			i := strings.LastIndex(local, ":")
			if i < 0 {
				continue
			}
			out[fmt.Sprintf("%s:%s", filepath.Base(path), local[i+1:])] = true
		}
		f.Close()
	}
	return out
}
