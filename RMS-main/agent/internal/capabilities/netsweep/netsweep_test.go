package netsweep

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
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

// spyEmitter counts every probe and answers from a script.
//
// The PROBE COUNTER is the observation every scope test in this file asserts
// on. docs/modules/agent/plan.md Task 13 step 2 says so explicitly -- "assert
// the probe counter, not the returned error" -- because an implementation that
// returned the right error AND emitted the packets anyway would satisfy an
// error-only assertion while having done the one thing the check exists to
// prevent.
type spyEmitter struct {
	mu      sync.Mutex
	sent    []netip.Addr
	answers map[netip.Addr]bool
	opens   int
	closes  int
	sendErr error
}

func newSpyEmitter(answers ...netip.Addr) *spyEmitter {
	e := &spyEmitter{answers: map[netip.Addr]bool{}}
	for _, a := range answers {
		e.answers[a] = true
	}
	return e
}

func (e *spyEmitter) Send(addr netip.Addr) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.sent = append(e.sent, addr)
	return e.sendErr
}

func (e *spyEmitter) Responded() map[netip.Addr]bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := map[netip.Addr]bool{}
	for _, a := range e.sent {
		if e.answers[a] {
			out[a] = true
		}
	}
	return out
}

func (e *spyEmitter) Close() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.closes++
	return nil
}

func (e *spyEmitter) probes() []netip.Addr {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]netip.Addr(nil), e.sent...)
}

func (e *spyEmitter) open(context.Context) (Emitter, error) {
	e.mu.Lock()
	e.opens++
	e.mu.Unlock()
	return e, nil
}

// fakeClock is virtual time. A rate limiter that sleeps must be testable
// without sleeping: 100 targets at 10 pps is ten seconds of wall-clock and
// nobody keeps a ten-second unit test.
type fakeClock struct {
	mu     sync.Mutex
	now    time.Time
	slept  time.Duration
	sleeps int
}

func newFakeClock() *fakeClock {
	return &fakeClock{now: time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)}
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Sleep(ctx context.Context, d time.Duration) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if d > 0 {
		c.now = c.now.Add(d)
		c.slept += d
		c.sleeps++
	}
	return nil
}

type staticNeighbours struct {
	table map[netip.Addr]string
	err   error
}

func (s staticNeighbours) Neighbours() (map[netip.Addr]string, error) {
	return s.table, s.err
}

type staticResolver struct{ names map[netip.Addr]string }

func (s staticResolver) Hostname(_ context.Context, a netip.Addr) string { return s.names[a] }

func addr(t testing.TB, s string) netip.Addr {
	t.Helper()
	a, err := netip.ParseAddr(s)
	if err != nil {
		t.Fatal(err)
	}
	return a
}

// paramsJSON builds a NetSweepParams with a CORRECT scope_digest, so a test
// that is not about the digest does not accidentally exercise the refusal path.
func paramsJSON(t testing.TB, targets []string, extra map[string]any) json.RawMessage {
	t.Helper()
	m := map[string]any{
		"subnet_id":    "3f2a1b7c-0d4e-4a19-9f8b-2c6d5e4f3a21",
		"sweep_run_id": "b8e1c4d2-77aa-4f3e-8b21-9c0d3e5f7a14",
		"targets":      targets,
		"scope_digest": Digest(targets),
	}
	for k, v := range extra {
		m[k] = v
	}
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func sweepWith(t testing.TB, o Options) *Service {
	t.Helper()
	if o.Clock == nil {
		o.Clock = newFakeClock()
	}
	if o.Logger == nil {
		o.Logger = quiet()
	}
	// An empty table unless the test brings one, so no test depends on the
	// machine it runs on (macOS and Windows have no table at all).
	if o.Neighbours == nil {
		o.Neighbours = staticNeighbours{}
	}
	return New(o)
}

func addrsOf(hosts []host) []string {
	out := make([]string, 0, len(hosts))
	for _, h := range hosts {
		out = append(out, h.Address)
	}
	sort.Strings(out)
	return out
}

// ---------------------------------------------------------------------------
// The scope check, before a single packet
// ---------------------------------------------------------------------------

func TestADigestMismatchRefusesBeforeAnyProbeIsEmitted(t *testing.T) {
	// §6.4 has the SERVER reject an out-of-scope result whole. That protects the
	// database; it does not un-send a packet. This is the agent side, and it is
	// strictly earlier.
	//
	// FALSIFYING OBSERVATION: the probe counter non-zero for a job whose digest
	// does not match. Asserted on the counter AND on the emitter never having
	// been opened -- not on the returned error.
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open})

	raw := json.RawMessage(`{"subnet_id":"s","sweep_run_id":"r",
		"targets":["10.20.30.0/30"],
		"scope_digest":"0000000000000000000000000000000000000000000000000000000000000000"}`)

	doc, err := s.run(context.Background(), raw)

	// THE PROBE COUNTER FIRST, deliberately. The plan says "assert the probe
	// counter, not the returned error", and the ordering matters when this goes
	// red: with the check deleted, the failure that gets printed should be the
	// packets, not the missing error.
	if n := len(spy.probes()); n != 0 {
		t.Fatalf("%d probes were emitted for a job whose scope_digest did not match: %v",
			n, spy.probes())
	}
	if spy.opens != 0 {
		t.Fatalf("the probe socket was opened %d times before the scope check", spy.opens)
	}
	if doc != nil {
		t.Fatalf("a document was produced for a job whose scope could not be verified: %+v", doc)
	}
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("err = %v (%T); want a jobs.Refusal", err, err)
	}
}

func TestAMatchingDigestProbesTheAuthorizedAddresses(t *testing.T) {
	// The complement, so the test above is not vacuous: with a digest that
	// matches, probes ARE emitted.
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	doc, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, nil))
	if err != nil {
		t.Fatal(err)
	}
	if n := len(spy.probes()); n == 0 {
		t.Fatal("no probes were emitted for a job whose scope_digest matched; the refusal test " +
			"above passes because this capability never probes at all")
	}
	if doc.ScopeDigest != Digest([]string{"10.20.30.0/30"}) {
		t.Fatalf("the document echoed scope_digest %q", doc.ScopeDigest)
	}
}

func TestTheDigestIsRecomputedOverTheTargetsAsReceived(t *testing.T) {
	// C6. An agent that normalised, sorted or re-rendered `params.targets`
	// before hashing would agree with the server on every canonical list -- all
	// of them, in practice -- and would silently stop checking anything on a
	// list that was not canonical.
	//
	// This job carries an UNSORTED target list and the digest of that order. It
	// must be accepted.
	unsorted := []string{"10.20.31.0/30", "10.20.30.0/30"}
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	if _, err := s.run(context.Background(), paramsJSON(t, unsorted, nil)); err != nil {
		t.Fatalf("a legitimate job with an unsorted target list was refused: %v", err)
	}

	// And the digest of the SORTED order must be rejected for the same targets:
	// the two are different authorizations.
	sorted := []string{"10.20.30.0/30", "10.20.31.0/30"}
	raw, err := json.Marshal(map[string]any{
		"subnet_id": "s", "sweep_run_id": "r",
		"targets": unsorted, "scope_digest": Digest(sorted),
	})
	if err != nil {
		t.Fatal(err)
	}
	spy2 := newSpyEmitter()
	s2 := sweepWith(t, Options{OpenEmitter: spy2.open})
	if _, err := s2.run(context.Background(), raw); err == nil {
		t.Fatal("a job whose scope_digest is of a DIFFERENT ordering of the same targets was " +
			"accepted; the agent is normalising its input before hashing")
	}
	if n := len(spy2.probes()); n != 0 {
		t.Fatalf("%d probes emitted for it", n)
	}
}

// ---------------------------------------------------------------------------
// Containment: every probed address, and every reported one, is in scope
// ---------------------------------------------------------------------------

func TestEveryProbedAddressIsContainedByTheSignedTargets(t *testing.T) {
	// The property §6.4 step 4 checks server-side, held here. Generated target
	// lists rather than one hand-picked case: a containment bug is an
	// off-by-one at a prefix boundary, and one example finds one boundary.
	//
	// FALSIFYING OBSERVATION: a probed address outside the union, PRINTED with
	// the target list that authorized it.
	rng := rand.New(rand.NewPCG(20260805, 13))
	for i := 0; i < 200; i++ {
		var targets []string
		var nets []netip.Prefix
		for n := 0; n < 1+rng.IntN(4); n++ {
			bits := 24 + rng.IntN(9) // /24 .. /32
			a := netip.AddrFrom4([4]byte{10, byte(rng.IntN(256)), byte(rng.IntN(256)), byte(rng.IntN(256))})
			p := netip.PrefixFrom(a, bits).Masked()
			targets = append(targets, p.String())
			nets = append(nets, p)
		}
		spy := newSpyEmitter()
		s := sweepWith(t, Options{OpenEmitter: spy.open})
		if _, err := s.run(context.Background(), paramsJSON(t, targets, nil)); err != nil {
			t.Fatalf("targets %v: %v", targets, err)
		}
		for _, probed := range spy.probes() {
			contained := false
			for _, n := range nets {
				if n.Contains(probed) {
					contained = true
					break
				}
			}
			if !contained {
				t.Fatalf("probed %s, which no signed target contains: targets = %v",
					probed, targets)
			}
		}
	}
}

func TestANeighbourTableEntryOutsideTheScopeIsNeverReported(t *testing.T) {
	// THE CONTAINMENT FILTER, and the reason it is not redundant with "we only
	// probe what we enumerated". The kernel's neighbour table is UNTRUSTED
	// INPUT: it names the default gateway, other segments, and whatever was
	// resolved last week. A sweep that reported what it found there would emit
	// observations for addresses it never probed and was never authorized to
	// touch -- and §6.4 would reject the whole document, losing the legitimate
	// hosts with it.
	//
	// FALSIFYING OBSERVATION: an out-of-scope address in `hosts`, printed.
	inScope := addr(t, "10.20.30.1")
	outOfScope := addr(t, "192.168.1.1")
	spy := newSpyEmitter(inScope)
	s := sweepWith(t, Options{
		OpenEmitter: spy.open,
		Neighbours: staticNeighbours{table: map[netip.Addr]string{
			inScope:    "02:00:00:00:00:01",
			outOfScope: "02:00:00:00:00:99",
		}},
	})
	doc, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, nil))
	if err != nil {
		t.Fatal(err)
	}
	for _, h := range doc.Hosts {
		if h.Address == outOfScope.String() {
			t.Fatalf("the document reports %s, which is outside the signed scope and was never "+
				"probed: %v", h.Address, addrsOf(doc.Hosts))
		}
	}
	if len(doc.Hosts) == 0 {
		t.Fatal("no hosts at all; the assertion above is vacuous")
	}
}

func TestIPv6TargetsAreRecordedAndSkippedRatherThanEnumerated(t *testing.T) {
	// §0.5: IPv6 is stored, documented and reconciled but never ENUMERATED --
	// "an ICMP sweep of an IPv6 prefix is not a hard query, it is a category
	// error". `create_sweep` raises before signing, so one cannot legitimately
	// arrive; one that does is recorded and skipped.
	//
	// FALSIFYING OBSERVATION, and the honest version of it: the FIRST draft of
	// this test asserted that no probed address rendered with a colon. That
	// assertion is impossible to fail -- Go renders a v4-mapped address as a
	// dotted quad, so a colon search finds nothing whatever the code does. It
	// is replaced by the observation that actually distinguishes the two
	// behaviours: the probe COUNT, which would be astronomically larger if a /64
	// were enumerated, and an `errors[]` entry naming the skip.
	targets := []string{"2001:db8::/64", "10.20.30.0/30"}
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	doc, err := s.run(context.Background(), paramsJSON(t, targets, nil))
	if err != nil {
		t.Fatal(err)
	}
	if n := len(spy.probes()); n != 2 {
		t.Fatalf("%d probes; the /30 contributes 2 addresses and the /64 must contribute none", n)
	}
	for _, p := range spy.probes() {
		if !p.Is4() {
			t.Fatalf("probed a non-IPv4 address: %s", p)
		}
	}
	var named bool
	for _, e := range doc.Errors {
		if strings.Contains(e, "2001:db8::/64") {
			named = true
		}
	}
	if !named {
		t.Fatalf("the skipped IPv6 target is not in errors[]: %v", doc.Errors)
	}
}

func TestTheNetworkAndBroadcastAddressesAreNotProbed(t *testing.T) {
	// Strictly inside the authorized scope: probing fewer addresses than were
	// authorized is always permitted. A directed-broadcast echo elicits a reply
	// from every host on the segment at once, and every one of those hosts is
	// probed individually anyway.
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	if _, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/29"}, nil)); err != nil {
		t.Fatal(err)
	}
	got := spy.probes()
	if len(got) != 6 {
		t.Fatalf("%d probes for a /29; want 6 (8 addresses less network and broadcast): %v",
			len(got), got)
	}
	for _, p := range got {
		if p.String() == "10.20.30.0" || p.String() == "10.20.30.7" {
			t.Fatalf("probed %s", p)
		}
	}
	// A /31 and a /32 have no network/broadcast pair to exclude: excluding them
	// there would leave nothing to probe at all.
	spy31 := newSpyEmitter()
	s31 := sweepWith(t, Options{OpenEmitter: spy31.open})
	if _, err := s31.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/31", "10.20.31.9/32"}, nil)); err != nil {
		t.Fatal(err)
	}
	if n := len(spy31.probes()); n != 3 {
		t.Fatalf("%d probes for a /31 plus a /32; want 3: %v", n, spy31.probes())
	}
}

// ---------------------------------------------------------------------------
// The result document
// ---------------------------------------------------------------------------

func TestTheDocumentEchoesTheSignedIdentifiersByteForByte(t *testing.T) {
	// §5.4/§6.4 step 2. The server compares canonical UUID text; a re-parse and
	// re-render is a chance to differ in case or hyphenation for no benefit.
	targets := []string{"10.20.30.0/30"}
	raw, err := json.Marshal(map[string]any{
		"subnet_id":    "3F2A1B7C-0D4E-4A19-9F8B-2C6D5E4F3A21",
		"sweep_run_id": "b8e1c4d2-77aa-4f3e-8b21-9c0d3e5f7a14",
		"targets":      targets,
		"scope_digest": Digest(targets),
	})
	if err != nil {
		t.Fatal(err)
	}
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	doc, err := s.run(context.Background(), raw)
	if err != nil {
		t.Fatal(err)
	}
	if doc.SubnetID != "3F2A1B7C-0D4E-4A19-9F8B-2C6D5E4F3A21" {
		t.Fatalf("subnet_id came back %q; it must be the received bytes, uppercase and all",
			doc.SubnetID)
	}
	if doc.SweepRunID != "b8e1c4d2-77aa-4f3e-8b21-9c0d3e5f7a14" {
		t.Fatalf("sweep_run_id came back %q", doc.SweepRunID)
	}
	if doc.ScopeDigest != Digest(targets) {
		t.Fatalf("scope_digest came back %q", doc.ScopeDigest)
	}
}

func TestTheEncodedHostKeySetIsExactlyWhatSweepHostAllows(t *testing.T) {
	// `SweepHost` is extra="forbid" and §6.4 rejects the WHOLE document on any
	// failure, so one extra key costs the sweep. There is no RTT, no per-probe
	// timing, no retry count and no `reachable:false` entry (§5.4) -- "adding
	// any of them converts this module into the monitoring product one field at
	// a time".
	responder := addr(t, "10.20.30.1")
	for _, tc := range []struct {
		name string
		h    host
		want string
	}{
		{"icmp only", host{Address: "10.20.30.1", RespondedVia: "icmp"}, "address,responded_via"},
		{"with mac", host{Address: "10.20.30.1", RespondedVia: "arp", MAC: strptr("02:00:00:00:00:01")},
			"address,mac,responded_via"},
		{"with hostname", host{Address: "10.20.30.1", RespondedVia: "both",
			MAC: strptr("02:00:00:00:00:01"), Hostname: strptr("printer.local")},
			"address,hostname,mac,responded_via"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b, err := json.Marshal(tc.h)
			if err != nil {
				t.Fatal(err)
			}
			var m map[string]any
			if err := json.Unmarshal(b, &m); err != nil {
				t.Fatal(err)
			}
			keys := make([]string, 0, len(m))
			for k := range m {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			if strings.Join(keys, ",") != tc.want {
				t.Fatalf("encoded keys = %v, want %s", keys, tc.want)
			}
		})
	}

	// And the same assertion on a host the capability actually produced, so the
	// struct above is not a fiction.
	spy := newSpyEmitter(responder)
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	doc, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, nil))
	if err != nil {
		t.Fatal(err)
	}
	if len(doc.Hosts) != 1 {
		t.Fatalf("%d hosts, want 1", len(doc.Hosts))
	}
	b, err := json.Marshal(doc.Hosts[0])
	if err != nil {
		t.Fatal(err)
	}
	if got := string(b); got != `{"address":"10.20.30.1","responded_via":"icmp"}` {
		t.Fatalf("encoded host = %s", got)
	}
}

func TestRespondedViaDistinguishesICMPFromARPFromBoth(t *testing.T) {
	icmpOnly := addr(t, "10.20.30.1")
	arpOnly := addr(t, "10.20.30.2")
	both := addr(t, "10.20.30.3")
	spy := newSpyEmitter(icmpOnly, both)
	s := sweepWith(t, Options{
		OpenEmitter: spy.open,
		Neighbours: staticNeighbours{table: map[netip.Addr]string{
			arpOnly: "02:00:00:00:00:02",
			both:    "02:00:00:00:00:03",
		}},
	})
	doc, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/24"}, nil))
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	macs := map[string]bool{}
	for _, h := range doc.Hosts {
		got[h.Address] = h.RespondedVia
		macs[h.Address] = h.MAC != nil
	}
	for _, tc := range []struct{ address, want string }{
		{"10.20.30.1", "icmp"},
		{"10.20.30.2", "arp"},
		{"10.20.30.3", "both"},
	} {
		if got[tc.address] != tc.want {
			t.Fatalf("%s responded_via = %q, want %q (all: %v)", tc.address, got[tc.address], tc.want, got)
		}
	}
	// A host that answered only ICMP has no MAC: `mac` is null for icmp-only
	// (§5.4), and inventing one would be inventing identity evidence.
	if macs["10.20.30.1"] {
		t.Fatal("an icmp-only host carries a MAC")
	}
	if !macs["10.20.30.2"] || !macs["10.20.30.3"] {
		t.Fatal("an ARP responder carries no MAC")
	}
}

func TestHostsNeverRepeatAnAddress(t *testing.T) {
	// handle_sweep_result rejects the WHOLE document if an address repeats
	// (handler.py's `repeated` branch): §2.7 is a current-state table, not a
	// probe log. Overlapping targets are the way a duplicate arises, and they
	// are legal input -- the digest is over the list AS GIVEN, duplicates and
	// all.
	responder := addr(t, "10.20.30.1")
	spy := newSpyEmitter(responder)
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	targets := []string{"10.20.30.0/30", "10.20.30.0/30", "10.20.30.0/31"}
	doc, err := s.run(context.Background(), paramsJSON(t, targets, nil))
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]int{}
	for _, h := range doc.Hosts {
		seen[h.Address]++
	}
	for a, n := range seen {
		if n > 1 {
			t.Fatalf("%s appears %d times; the server rejects the whole document for it", a, n)
		}
	}
	// The probes are deduplicated too: three overlapping targets are not three
	// packets at one host.
	probeCount := map[netip.Addr]int{}
	for _, p := range spy.probes() {
		probeCount[p]++
	}
	for a, n := range probeCount {
		if n > 1 {
			t.Fatalf("probed %s %d times for overlapping targets", a, n)
		}
	}
}

func TestTheHostBudgetIsTheSameArithmeticTheServerUses(t *testing.T) {
	// handler.py:308-311: `budget = min(params.max_targets, scope_addresses)`
	// and a document above it is rejected WHOLE. Computing it the same way here
	// means the agent stops rather than produces a document the server
	// discards.
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	doc, err := s.run(context.Background(),
		paramsJSON(t, []string{"10.20.30.0/24"}, map[string]any{"max_targets": 5}))
	if err != nil {
		t.Fatal(err)
	}
	if n := len(spy.probes()); n != 5 {
		t.Fatalf("%d probes with max_targets 5", n)
	}
	if !doc.Truncated {
		t.Fatal("truncated is false after stopping at max_targets; §5.4's flag is how the " +
			"operator learns the sweep was partial")
	}
	if doc.ProbedCount != 5 {
		t.Fatalf("probed_count = %d, want 5", doc.ProbedCount)
	}
}

// ---------------------------------------------------------------------------
// Bounding the enumeration itself, and the one thing it changed
// ---------------------------------------------------------------------------

func TestBoundedPlanningProbesExactlyWhatUnboundedTruncationProbed(t *testing.T) {
	// `enumerate` now stops at a limit instead of expanding every signed prefix
	// and letting `plan[:budget]` truncate afterwards. THAT IS A FOOTPRINT
	// CHANGE AND MUST NOT BE A SCOPE CHANGE, so this is the test that says so.
	//
	// Truncation kept the first N addresses in enumeration order. Stopping early
	// keeps the same first N in the same order — enumeration order is fixed by
	// the order of `targets` and by ascending address within each prefix, and
	// `seen` is only ever consulted for membership, never iterated. So a bounded
	// plan must be a byte-identical PREFIX of the unbounded one.
	//
	// FALSIFYING OBSERVATION: any position at which the two disagree, in value
	// or in count. That is the observation that would mean this change altered
	// which addresses the agent puts packets on — the thing enumerate's comment
	// about authorized scope is protecting.
	const target = "10.0.0.0/16" // 65,534 addresses
	unbounded, _ := enumerate([]string{target}, 1<<20)

	for _, bound := range []int{1, 2, 255, 256, 257, 4096, 4097, 65534} {
		got, _ := enumerate([]string{target}, bound)
		want := unbounded
		if bound < len(want) {
			want = want[:bound]
		}
		if len(got) != len(want) {
			t.Fatalf("bound %d planned %d addresses; truncating the unbounded plan gives %d",
				bound, len(got), len(want))
		}
		for i := range got {
			if got[i] != want[i] {
				t.Fatalf("bound %d: address %d is %s, but unbounded truncation gives %s — "+
					"bounding the enumeration changed WHICH addresses are probed, not just "+
					"what it costs to decide", bound, i, got[i], want[i])
			}
		}
	}

	// The same property across several targets, where the bound falls INSIDE the
	// second one — the case a single-prefix fixture cannot reach.
	targets := []string{"10.20.30.0/29", "10.20.31.0/24", "10.20.32.0/24"}
	full, _ := enumerate(targets, 1<<20)
	for bound := 1; bound <= len(full); bound++ {
		got, _ := enumerate(targets, bound)
		if len(got) != bound {
			t.Fatalf("bound %d over %v planned %d addresses", bound, targets, len(got))
		}
		for i := range got {
			if got[i] != full[i] {
				t.Fatalf("bound %d over %v: address %d is %s, want %s",
					bound, targets, i, got[i], full[i])
			}
		}
	}
}

func TestTruncatedSurvivesTheBoundInBothDirections(t *testing.T) {
	// THE ONE WAY THIS FIX COULD HAVE BROKEN THE RESULT DOCUMENT, gated.
	//
	// `run` derives §5.4's flag from `budget < len(plan)` — "the plan held more
	// than we probed". Bounding enumeration at exactly `max_targets` would make
	// that comparison structurally impossible to satisfy: `len(plan)` could
	// never exceed the budget, `truncated` would read false on every partial
	// sweep, and the operator would silently stop being told the sweep did not
	// cover the subnet. Nothing else in the suite would have gone red.
	//
	// `run` therefore asks enumerate for `max_targets+1`. This test pins both
	// directions, because only asserting the true case would pass an
	// implementation that hardcoded `truncated = true`.
	//
	// FALSIFYING OBSERVATION: a partial sweep reporting truncated:false, or a
	// complete one reporting truncated:true.
	cases := []struct {
		name       string
		target     string
		maxTargets int
		probes     int
		truncated  bool
	}{
		// A /24 is 254 probeable addresses.
		{"far more addresses than the cap", "10.20.30.0/24", 5, 5, true},
		{"one more address than the cap", "10.20.30.0/24", 253, 253, true},
		// EXACTLY the cap: the plan is complete, so nothing was left out.
		{"exactly as many addresses as the cap", "10.20.30.0/24", 254, 254, false},
		{"a cap larger than the prefix", "10.20.30.0/24", 4096, 254, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			spy := newSpyEmitter()
			s := sweepWith(t, Options{OpenEmitter: spy.open})
			doc, err := s.run(context.Background(),
				paramsJSON(t, []string{c.target}, map[string]any{"max_targets": c.maxTargets}))
			if err != nil {
				t.Fatal(err)
			}
			if n := len(spy.probes()); n != c.probes {
				t.Fatalf("%d probes with max_targets %d, want %d", n, c.maxTargets, c.probes)
			}
			if doc.ProbedCount != c.probes {
				t.Fatalf("probed_count = %d, want %d", doc.ProbedCount, c.probes)
			}
			if doc.Truncated != c.truncated {
				t.Fatalf("truncated = %v, want %v (max_targets %d, %d probes of a %s)",
					doc.Truncated, c.truncated, c.maxTargets, c.probes, c.target)
			}
		})
	}
}

func TestATargetPastTheBoundIsNeitherParsedNorReported(t *testing.T) {
	// THE ONE DELIBERATE BEHAVIOUR CHANGE, asserted so it is a decision on the
	// record rather than something discovered in the field.
	//
	// Before the bound, every target was parsed — truncation happened after the
	// whole list had been expanded — so a malformed or IPv6 target sitting AFTER
	// the truncation point still produced an `errors[]` entry about a target
	// this agent was never going to probe. It no longer does.
	//
	// This is the more honest report: a parse error for an address that was
	// never in the probed set describes work that did not happen. It is safe
	// because `errors[]` is ADVISORY — `handle_sweep_result` validates it
	// against NetSweepDocument's 20 x 200 bounds and then never reads it; no
	// reconciliation, no state and no acceptance decision depends on it.
	// Recorded in docs/modules/ipam/spec.md §5.4.
	//
	// FALSIFYING OBSERVATION for the safety claim, and it is NOT this test:
	// backend/app/modules/ipam/handler.py reading doc.errors for anything. If it
	// ever does, this change stops being free and the bound needs to keep
	// parsing the tail.
	//
	// The /24 fills the 5-address cap on its own, so the two broken targets
	// behind it are never reached.
	targets := []string{"10.20.30.0/24", "not-an-address", "2001:db8::/64"}
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	doc, err := s.run(context.Background(),
		paramsJSON(t, targets, map[string]any{"max_targets": 5}))
	if err != nil {
		t.Fatal(err)
	}
	if n := len(spy.probes()); n != 5 {
		t.Fatalf("%d probes with max_targets 5", n)
	}
	if len(doc.Errors) != 0 {
		t.Fatalf("errors[] is %v; targets past the bound were never going to be probed and "+
			"must not be reported", doc.Errors)
	}
	if !doc.Truncated {
		t.Fatal("truncated is false; the sweep stopped short of its targets")
	}

	// AND THE CONVERSE, which is the half that matters: a broken target the
	// enumeration DOES reach is still reported exactly as before. Without this,
	// an implementation that dropped errors[] entirely would pass the above.
	reachable := []string{"not-an-address", "2001:db8::/64", "10.20.30.0/30"}
	spy2 := newSpyEmitter()
	s2 := sweepWith(t, Options{OpenEmitter: spy2.open})
	doc2, err := s2.run(context.Background(),
		paramsJSON(t, reachable, map[string]any{"max_targets": 5}))
	if err != nil {
		t.Fatal(err)
	}
	if len(doc2.Errors) != 2 {
		t.Fatalf("errors[] is %v; both broken targets precede the bound and must be reported",
			doc2.Errors)
	}
	if n := len(spy2.probes()); n != 2 {
		t.Fatalf("%d probes; the /30 contributes 2 addresses", n)
	}
}

func TestAnErrorsEntryIsBoundedAtTwentyEntriesOfTwoHundredCharacters(t *testing.T) {
	// NetSweepDocument.errors is `maxItems: 20`, `maxLength: 200`. A document
	// that exceeds either is rejected whole, so the bound is enforced here
	// rather than discovered as a lost sweep.
	var errs []string
	for i := 0; i < 50; i++ {
		errs = appendError(errs, strings.Repeat("x", 500))
	}
	if len(errs) != maxErrors {
		t.Fatalf("%d entries, want the cap of %d", len(errs), maxErrors)
	}
	for _, e := range errs {
		if len(e) > maxErrorLen {
			t.Fatalf("entry is %d bytes, above %d", len(e), maxErrorLen)
		}
	}
}

// ---------------------------------------------------------------------------
// Rate, budget, truncation
// ---------------------------------------------------------------------------

func TestTheRateLimitIsHonouredAgainstTheClock(t *testing.T) {
	// §5.5: `rate_pps` is inside the SIGNED params so the ceiling on what a
	// sweep costs is the server's and "cannot be raised by anything on the
	// endpoint" -- which is only true if the endpoint applies it.
	//
	// FALSIFYING OBSERVATION: more than rate_pps probes per simulated second.
	clock := newFakeClock()
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open, Clock: clock})
	start := clock.Now()
	if _, err := s.run(context.Background(),
		paramsJSON(t, []string{"10.20.30.0/25"}, map[string]any{"rate_pps": 10, "max_targets": 100})); err != nil {
		t.Fatal(err)
	}
	probes := len(spy.probes())
	if probes != 100 {
		t.Fatalf("%d probes, want 100", probes)
	}
	// 100 probes at 10/s is 99 intervals of 100 ms: the first is free.
	elapsed := clock.Now().Sub(start)
	wantSend := 99 * 100 * time.Millisecond
	if elapsed < wantSend {
		t.Fatalf("100 probes took %v of simulated time at rate_pps 10; at most 10 may be sent "+
			"per second, so the send phase alone is %v", elapsed, wantSend)
	}
}

func TestHittingTheSignedBudgetTruncatesRatherThanRunningLonger(t *testing.T) {
	// §5.5: "A sweep that hits its budget returns `truncated: true` rather than
	// running longer." Running longer means outliving the job's own 600-second
	// TTL (§5.2) and reporting into a row the control plane has written off.
	clock := newFakeClock()
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open, Clock: clock})
	doc, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.0.0/16"},
		map[string]any{"rate_pps": 1, "budget_s": 10, "max_targets": 65536}))
	if err != nil {
		t.Fatal(err)
	}
	if !doc.Truncated {
		t.Fatal("truncated is false after the budget was exhausted")
	}
	// One probe per second for ten seconds: eleven at the very most, and
	// nothing like the 65,534 the scope holds.
	if n := len(spy.probes()); n > 12 {
		t.Fatalf("%d probes inside a 10-second budget at 1 pps", n)
	}
	if n := len(spy.probes()); n == 0 {
		t.Fatal("no probes at all; the truncation is because nothing ran")
	}
}

func TestParamsOutsideTheSignedBoundsAreRefusedBeforeProbing(t *testing.T) {
	for _, tc := range []struct {
		name  string
		extra map[string]any
		want  string
	}{
		{"rate above 200", map[string]any{"rate_pps": 201}, "rate_pps"},
		{"rate below 1", map[string]any{"rate_pps": 0}, "rate_pps"},
		{"probe timeout above 5000", map[string]any{"probe_timeout_ms": 5001}, "probe_timeout_ms"},
		{"max_targets above 65536", map[string]any{"max_targets": 65537}, "max_targets"},
		{"max_targets below 1", map[string]any{"max_targets": 0}, "max_targets"},
		{"budget above 900", map[string]any{"budget_s": 901}, "budget_s"},
		{"budget below 10", map[string]any{"budget_s": 9}, "budget_s"},
		{"a method this agent will not perform", map[string]any{"method": "tcp-connect"}, "method"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			spy := newSpyEmitter()
			s := sweepWith(t, Options{OpenEmitter: spy.open})
			_, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, tc.extra))
			if err == nil {
				t.Fatal("accepted")
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("err = %q; want it to name %q", err, tc.want)
			}
			if n := len(spy.probes()); n != 0 {
				t.Fatalf("%d probes emitted for out-of-bounds params", n)
			}
		})
	}
}

func TestAnAbsentOptionalNumberIsItsSchemaDefaultAndNotZero(t *testing.T) {
	// `rate_pps: 0` is illegal and "no rate_pps" means 50. Collapsing them --
	// which a non-pointer field would -- makes an absent member read as a bound
	// of zero, which is either a division by zero or an unlimited sweep
	// depending on which way the arithmetic falls.
	p, err := parseParams(paramsJSON(t, []string{"10.20.30.0/30"}, nil))
	if err != nil {
		t.Fatal(err)
	}
	if p.ratePPS() != defaultRatePPS || p.maxTargets() != defMaxTargets ||
		p.budgetS() != defBudgetS || p.probeTimeout() != defProbeTimeoutMS*time.Millisecond ||
		p.method() != "icmp+arp" {
		t.Fatalf("defaults not applied: rate=%d max=%d budget=%d timeout=%v method=%q",
			p.ratePPS(), p.maxTargets(), p.budgetS(), p.probeTimeout(), p.method())
	}
}

func TestUnknownParamsMembersAreIgnored(t *testing.T) {
	// Inbound is loose: a strictly additive server change must not take a
	// deployed fleet offline.
	targets := []string{"10.20.30.0/30"}
	raw, err := json.Marshal(map[string]any{
		"subnet_id": "s", "sweep_run_id": "r", "targets": targets,
		"scope_digest": Digest(targets), "jitter_ms": 25, "vrf": "default",
	})
	if err != nil {
		t.Fatal(err)
	}
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	if _, err := s.run(context.Background(), raw); err != nil {
		t.Fatalf("params carrying two members this build does not know were refused: %v", err)
	}
}

// ---------------------------------------------------------------------------
// The ARP half, and the platform degradation
// ---------------------------------------------------------------------------

func TestAMethodOfICMPNeverReadsTheNeighbourTable(t *testing.T) {
	// `method` is signed. `icmp` means ICMP, and reading the ARP table anyway
	// would put MACs in the document for a sweep the operator authorized as
	// ICMP-only.
	called := 0
	responder := addr(t, "10.20.30.1")
	spy := newSpyEmitter(responder)
	s := sweepWith(t, Options{
		OpenEmitter: spy.open,
		Neighbours: neighbourFunc(func() (map[netip.Addr]string, error) {
			called++
			return map[netip.Addr]string{responder: "02:00:00:00:00:01"}, nil
		}),
	})
	doc, err := s.run(context.Background(),
		paramsJSON(t, []string{"10.20.30.0/30"}, map[string]any{"method": "icmp"}))
	if err != nil {
		t.Fatal(err)
	}
	if called != 0 {
		t.Fatalf("the neighbour table was read %d times for a method of icmp", called)
	}
	if len(doc.Hosts) != 1 || doc.Hosts[0].MAC != nil {
		t.Fatalf("hosts = %+v; an icmp-only sweep reports no MAC", doc.Hosts)
	}
	if doc.Method != "icmp" {
		t.Fatalf("method = %q; the document records what was actually done", doc.Method)
	}
}

func TestAPlatformWithNoNeighbourTableDegradesAndSaysSo(t *testing.T) {
	// The plan's rule: on Windows and macOS `icmp+arp` degrades to ICMP-only and
	// the ARP half is reported in `errors[]` rather than failing the job. A
	// sweep that found hosts is worth more than one that refused because it
	// could not also learn their MACs.
	//
	// FALSIFYING OBSERVATION: an empty errors[] -- which is what silently
	// substituting an empty table would produce, and it is indistinguishable
	// from "no host on this segment has a MAC".
	responder := addr(t, "10.20.30.1")
	spy := newSpyEmitter(responder)
	s := sweepWith(t, Options{
		OpenEmitter: spy.open,
		Neighbours:  neighbourFunc(func() (map[netip.Addr]string, error) { return nil, errNoTable }),
	})
	doc, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, nil))
	if err != nil {
		t.Fatalf("the sweep failed because the ARP half could not be done: %v", err)
	}
	if len(doc.Hosts) != 1 {
		t.Fatalf("%d hosts; the ICMP half must still be reported", len(doc.Hosts))
	}
	if len(doc.Errors) == 0 {
		t.Fatal("errors[] is empty; the missing ARP half is not recorded and an empty table " +
			"reads as 'no host here has a MAC'")
	}
}

var errNoTable = errors.New("no neighbour table on this platform")

type neighbourFunc func() (map[netip.Addr]string, error)

func (f neighbourFunc) Neighbours() (map[netip.Addr]string, error) { return f() }

func TestTheRealNeighbourTableIsReadableOnThisHost(t *testing.T) {
	// Not a unit test: a check that the file this capability depends on exists
	// and parses on a real Linux host. A parser tested only against a fixture is
	// a parser tested against its author's memory of the format.
	if runtime.GOOS != "linux" {
		t.Skip("linux only")
	}
	f, err := os.Open("/proc/net/arp")
	if err != nil {
		t.Skipf("no /proc/net/arp here: %v", err)
	}
	lines := 0
	s := bufio.NewScanner(f)
	for s.Scan() {
		lines++
	}
	f.Close()
	table, err := defaultNeighbours().Neighbours()
	if err != nil {
		t.Fatalf("reading the real /proc/net/arp: %v", err)
	}
	t.Logf("/proc/net/arp: %d lines, %d resolved IPv4 neighbours", lines, len(table))
}

// ---------------------------------------------------------------------------
// Dormancy (§5.5)
// ---------------------------------------------------------------------------

func TestTheCapabilityAddsNothingToTheTimerWheel(t *testing.T) {
	// §0.9: the cadence is server-side. "A locally scheduled sweep is a standing
	// authorization signed once and executed for weeks after the scope was
	// revoked."
	//
	// FALSIFYING OBSERVATION: a wheel entry after registering the capability, or
	// after running a sweep.
	w := scheduler.New(scheduler.NewFake(time.Now()), quiet())
	before := w.Len()
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	if w.Len() != before {
		t.Fatalf("building the capability put %d entries on the wheel", w.Len()-before)
	}
	if _, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, nil)); err != nil {
		t.Fatal(err)
	}
	if w.Len() != before {
		t.Fatalf("running a sweep put %d entries on the wheel", w.Len()-before)
	}
}

func TestNothingIsOpenedBeforeAJobArrivesAndEverythingIsReleasedAfter(t *testing.T) {
	// §5.5: "Dormant by default. The capability allocates nothing until a
	// `net.sweep` job arrives, and releases its socket, worker pool and result
	// buffer when the job completes. No resident scanner, no background thread,
	// no cache."
	//
	// FALSIFYING OBSERVATION: a leaked file descriptor, NAMED, or a goroutine
	// that outlives the job.
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

	// A REAL sweep, with the real ICMP emitter, against loopback. This container
	// has CAP_NET_RAW; where it does not, the capability refuses and the fd
	// assertion below is about the refusal path instead -- both are worth
	// asserting and neither is skipped silently.
	targets := []string{"127.0.0.1/32"}
	doc, err := s.run(context.Background(), paramsJSON(t, targets,
		map[string]any{"probe_timeout_ms": 200, "budget_s": 10}))
	var refusal *jobs.Refusal
	switch {
	case errors.As(err, &refusal):
		t.Logf("no raw socket privilege here; the capability refused: %v", refusal)
	case err != nil:
		t.Fatal(err)
	default:
		t.Logf("real ICMP sweep of 127.0.0.1: probed=%d hosts=%v errors=%v",
			doc.ProbedCount, addrsOf(doc.Hosts), doc.Errors)
		if len(doc.Hosts) != 1 || doc.Hosts[0].Address != "127.0.0.1" {
			t.Fatalf("loopback did not answer its own echo request: %+v", doc.Hosts)
		}
	}

	settle()
	if extra := newFDs(fdsBefore, openFDs(t)); len(extra) != 0 {
		t.Fatalf("the sweep leaked file descriptor(s): %v", extra)
	}
	if after := runtime.NumGoroutine(); after > goBefore {
		t.Fatalf("the sweep left %d goroutine(s) running; the reader is job-scoped",
			after-goBefore)
	}
}

func TestARealSweepBindsNoTCPPort(t *testing.T) {
	// Invariant #2 at the running-process level, for the one capability that
	// opens a socket at all. The source-level guard lives in
	// internal/transport/listen_test.go (and is where the ONE allowance for the
	// raw ICMP socket is declared); this is the runtime half.
	//
	// The reader is scoped by socket inode for the same reason transport's is:
	// `go test ./...` runs package binaries CONCURRENTLY, and an unscoped read
	// of /proc/net/tcp reports another test binary's httptest server as a port
	// this one bound.
	if runtime.GOOS != "linux" {
		t.Skip("reads /proc/net/tcp")
	}
	before := listeningPorts(t)
	s := New(Options{Logger: quiet()})
	targets := []string{"127.0.0.1/32"}
	if _, err := s.run(context.Background(), paramsJSON(t, targets,
		map[string]any{"probe_timeout_ms": 200, "budget_s": 10})); err != nil {
		var refusal *jobs.Refusal
		if !errors.As(err, &refusal) {
			t.Fatal(err)
		}
	}
	if extra := newFDs(before, listeningPorts(t)); len(extra) != 0 {
		t.Fatalf("a sweep bound %v; invariant #2 forbids any listening port", extra)
	}
}

func TestTheSocketIsClosedEvenWhenTheSweepFindsNothing(t *testing.T) {
	spy := newSpyEmitter()
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	if _, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, nil)); err != nil {
		t.Fatal(err)
	}
	if spy.opens != 1 || spy.closes != 1 {
		t.Fatalf("opened %d times, closed %d; the socket is opened at job start and closed at "+
			"job end, in a defer", spy.opens, spy.closes)
	}
}

func TestNoRawSocketPrivilegeRefusesRatherThanSubstitutingAnotherMethod(t *testing.T) {
	// §5.5 and the plan: a silent method substitution would put different
	// packets on someone else's wire than the ones the server authorized -- and
	// the operator would read the result as the ICMP sweep they asked for.
	//
	// `refused` and not `failed` on purpose: IPAM's dispatcher takes a device
	// that refuses `net.sweep` out of rotation for 7 days and picks another on
	// the segment, which is exactly right for a host whose agent cannot open a
	// raw socket.
	s := sweepWith(t, Options{
		OpenEmitter: func(context.Context) (Emitter, error) {
			return nil, errors.New("operation not permitted")
		},
	})
	_, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, nil))
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("err = %v (%T); want a jobs.Refusal so the dispatcher can pick another sweeper",
			err, err)
	}
	if !strings.Contains(refusal.Reason, "raw socket privilege") {
		t.Fatalf("reason = %q", refusal.Reason)
	}
}

// ---------------------------------------------------------------------------
// Invariant #1
// ---------------------------------------------------------------------------

func TestTheCapabilityTakesAVerifiedJobAndNothingElse(t *testing.T) {
	// A compile-time fact written as a test so its failure names the reason:
	// jobs.VerifiedJob is an interface with an unexported method and no exported
	// implementation, so this function is unreachable except through the
	// signed-job runner.
	var _ jobs.Handler = Process.Sweep
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func strptr(s string) *string { return &s }

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
// internal/transport/listen_test.go. It is duplicated rather than shared
// because moving it into non-test code to share it would put a /proc reader in
// the shipped binary for the sake of a test.
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
		s := bufio.NewScanner(f)
		s.Scan() // header
		for s.Scan() {
			fields := strings.Fields(s.Text())
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

func TestAResolvedHostnameIsDisplayOnlyAndBounded(t *testing.T) {
	// §5.4: `hostname` is agent-side reverse DNS, it is never a reconciliation
	// predicate, and it never matches an address to a device. It is carried
	// because an operator reading a subnet page wants it, and nothing else reads
	// it.
	//
	// FALSIFIER: an over-length name reaching the document. `hostname`'s
	// maxLength is 255 and an over-length value costs the WHOLE document, so a
	// segment whose PTR records are junk must not lose the sweep.
	responder := addr(t, "10.20.30.1")
	long := strings.Repeat("a", 300) + ".example"
	spy := newSpyEmitter(responder)
	s := sweepWith(t, Options{
		OpenEmitter: spy.open,
		Resolver:    staticResolver{names: map[netip.Addr]string{responder: "printer.local"}},
	})
	doc, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, nil))
	if err != nil {
		t.Fatal(err)
	}
	if len(doc.Hosts) != 1 || doc.Hosts[0].Hostname == nil || *doc.Hosts[0].Hostname != "printer.local" {
		t.Fatalf("hosts = %+v; want the resolved name carried through", doc.Hosts)
	}

	// A resolver that cannot answer yields no member at all -- the schema's
	// null -- rather than an empty string, which would read as a host named "".
	spy2 := newSpyEmitter(responder)
	s2 := sweepWith(t, Options{OpenEmitter: spy2.open, Resolver: staticResolver{}})
	doc2, err := s2.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, nil))
	if err != nil {
		t.Fatal(err)
	}
	if doc2.Hosts[0].Hostname != nil {
		t.Fatalf("hostname = %q for an address with no PTR record", *doc2.Hosts[0].Hostname)
	}

	// The real resolver's own bound, since the fake cannot exercise it.
	if got := (&dnsResolver{}).clampName(long); got != "" {
		t.Fatalf("a %d-character PTR record was carried through as %q", len(long), got)
	}
	if got := (&dnsResolver{}).clampName("printer.local."); got != "printer.local" {
		t.Fatalf("the trailing dot of a PTR record survived: %q", got)
	}
}

func TestTheEncodedDocumentNeverCarriesNullForHostsOrErrors(t *testing.T) {
	// `hosts` and `errors` are `list[...] = Field(default_factory=list)` in
	// NetSweepDocument -- not Optional. A JSON `null` for either is a pydantic
	// validation error, and §6.4 rejects the WHOLE document on any failure: a
	// sweep that found three hosts would be thrown away because it found no
	// errors.
	//
	// A nil Go slice marshals to `null`, and this is exactly what the first
	// end-to-end run produced.
	//
	// FALSIFYING OBSERVATION: `"errors":null` or `"hosts":null` in the encoded
	// document, printed.
	spy := newSpyEmitter() // nothing answers, and no target fails to parse
	s := sweepWith(t, Options{OpenEmitter: spy.open})
	doc, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, nil))
	if err != nil {
		t.Fatal(err)
	}
	b, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	for _, member := range []string{`"hosts":null`, `"errors":null`} {
		if strings.Contains(string(b), member) {
			t.Fatalf("the document carries %s: %s", member, b)
		}
	}
	if !strings.Contains(string(b), `"hosts":[]`) || !strings.Contains(string(b), `"errors":[]`) {
		t.Fatalf("an empty sweep must encode both as empty arrays: %s", b)
	}
}

// On a platform with no neighbour table, the document must not claim an ARP
// half it never performed. Runs on the macOS and Windows CI runners.
func TestWithoutANeighbourTableTheMethodSaysICMP(t *testing.T) {
	if runtime.GOOS == "linux" {
		t.Skip("linux has a neighbour table")
	}
	spy := newSpyEmitter()
	s := New(Options{Clock: newFakeClock(), Logger: quiet(), OpenEmitter: spy.open})
	doc, err := s.run(context.Background(), paramsJSON(t, []string{"10.20.30.0/30"}, nil))
	if err != nil {
		t.Fatal(err)
	}
	if doc.Method != "icmp" {
		t.Fatalf("method is %q; ARP was never performed on this platform", doc.Method)
	}
	if len(doc.Errors) != 1 || !strings.Contains(doc.Errors[0], "no neighbour table") {
		t.Fatalf("errors[] is %v; the missing ARP half must be reported", doc.Errors)
	}
}
