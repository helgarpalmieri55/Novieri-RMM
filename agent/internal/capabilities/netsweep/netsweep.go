// Package netsweep implements the `net.sweep` capability
// (docs/modules/ipam/spec.md §5, §6).
//
// # It emits; it never accepts
//
// Invariant #2 is that the agent connects OUTBOUND ONLY and has no listening
// port. A sweep needs a raw ICMP socket, which is the one thing in this
// repository that receives packets nobody in this process dialled — and that is
// still not a listening service:
//
//   - it is opened INSIDE a verified job and closed in a defer when that job
//     ends. There is no socket between jobs (§5.5: "dormant by default");
//   - it carries no port, accepts no connection, and runs no protocol an
//     outsider can start. What it reads are ECHO REPLIES to echo requests this
//     process just sent, filtered by an identifier this process chose;
//   - an unsolicited packet arriving at it is discarded, not dispatched. There
//     is no code path from a received byte to an action.
//
// internal/transport's listen_test.go holds the source-level guard, and Task 13
// takes its ONE documented allowance there: `net.ListenPacket` in probe_icmp.go
// and nothing else in this package. That allowance was also NARROWED as part of
// this task, from "skip this whole file" to "permit this one call in this one
// file", because a file-wide exemption would have let a future `net.Listen`
// into the same file unchallenged. netsweep_test.go additionally asserts that a
// real sweep binds no TCP port.
//
// # The scope is signed, and the agent cannot widen it
//
// `targets` is an explicit enumerated list computed server-side (§6.2) and
// covered by the job signature. The agent consults no local configuration to
// decide what it may touch; there is nothing for it to consult. Before a single
// packet leaves the host it recomputes `scope_digest` over `params.targets` AS
// RECEIVED and refuses on a mismatch — see digest.go for why re-deriving that
// input is the one thing that would quietly retire the check.
//
// §6.4 has the server reject an out-of-scope result WHOLE. This package is the
// half that stops one being produced: rejecting after the fact means the probes
// already went out, and a packet on someone else's wire is not recalled by a
// database transaction rolling back.
//
// # No local schedule (§0.9)
//
// The cadence is server-side. This package adds NOTHING to the timer wheel: a
// locally scheduled sweep is a standing authorization signed once and executed
// for weeks after the scope was revoked. netsweep_test.go asserts the wheel is
// unchanged by running one.
package netsweep

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/netip"
	"sync/atomic"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// JobType matches app/modules/ipam/handler.py's registration.
const JobType = "net.sweep"

// rfc3339 matches app/jobs/canonical.py::rfc3339 — whole seconds, literal Z.
// NetSweepDocument's `started_at`/`finished_at` are `format: date-time`.
const rfc3339 = "2006-01-02T15:04:05Z"

// Bounds from NetSweepParams.schema.json, restated here.
//
// Duplicated agent-side on purpose, for the same reason the health caps are:
// a bound that lives only in the server's params model does not travel with the
// job in any form the agent can enforce. `rate_pps`, `max_targets` and
// `budget_s` are the ceiling on what a sweep can COST, and §5.5 makes the point
// that they are inside the SIGNED params so that ceiling "cannot be raised by
// anything on the endpoint" — which is only true if the endpoint checks them.
const (
	maxTargetEntries  = 4096
	digestHexLen      = 64
	minRatePPS        = 1
	maxRatePPS        = 200
	defaultRatePPS    = 50
	minProbeTimeoutMS = 100
	maxProbeTimeoutMS = 5000
	defProbeTimeoutMS = 500
	minMaxTargets     = 1
	maxMaxTargets     = 65536
	defMaxTargets     = 4096
	minBudgetS        = 10
	maxBudgetS        = 900
	defBudgetS        = 300

	// NetSweepDocument.errors is bounded 20 entries, 200 chars each.
	maxErrors   = 20
	maxErrorLen = 200
)

// params implements NetSweepParams.schema.json.
//
// Unknown members are ignored (encoding/json's default): the inbound half of
// docs/kernel/wire-protocol/README.md's rule, so a strictly additive server
// change does not take a deployed fleet offline.
//
// The optional numbers are POINTERS because their schema defaults are not their
// zero values. `rate_pps: 0` and "no rate_pps" are different statements and
// only one of them is legal; collapsing them would let an absent member read as
// a bound of zero.
type params struct {
	SubnetID       string   `json:"subnet_id"`
	SweepRunID     string   `json:"sweep_run_id"`
	Targets        []string `json:"targets"`
	ScopeDigest    string   `json:"scope_digest"`
	Method         *string  `json:"method"`
	RatePPS        *int     `json:"rate_pps"`
	ProbeTimeoutMS *int     `json:"probe_timeout_ms"`
	MaxTargets     *int     `json:"max_targets"`
	BudgetS        *int     `json:"budget_s"`
}

// host implements NetSweepDocument $defs SweepHost.
//
// `extra="forbid"` server-side, and §6.4 rejects the WHOLE document on any
// failure — so an extra key here costs the sweep, not the field. There is no
// RTT, no per-probe timing, no retry count and no `reachable:false` entry:
// §5.4's absences are §0.10 points 2 and 5 expressed as a schema, and "adding
// any of them converts this module into the monitoring product one field at a
// time".
type host struct {
	Address      string  `json:"address"`
	MAC          *string `json:"mac,omitempty"`
	RespondedVia string  `json:"responded_via"`
	Hostname     *string `json:"hostname,omitempty"`
}

// document implements NetSweepDocument.schema.json.
type document struct {
	SubnetID    string   `json:"subnet_id"`
	SweepRunID  string   `json:"sweep_run_id"`
	ScopeDigest string   `json:"scope_digest"`
	Method      string   `json:"method"`
	StartedAt   string   `json:"started_at"`
	FinishedAt  string   `json:"finished_at"`
	ProbedCount int      `json:"probed_count"`
	Truncated   bool     `json:"truncated"`
	Hosts       []host   `json:"hosts"`
	Errors      []string `json:"errors"`
}

// Emitter is the probe half, job-scoped.
//
// Send never blocks on a reply; Responded is a snapshot of what has answered so
// far. Splitting them is what lets the sweep rate-limit its SENDS against a
// clock without also serialising a 500 ms wait per address — 4,096 addresses at
// one round trip each is over half an hour, which no `budget_s` permits.
type Emitter interface {
	Send(addr netip.Addr) error
	Responded() map[netip.Addr]bool
	Close() error
}

// Neighbours reads the kernel's neighbour table.
//
// # Why this, and not an AF_PACKET ARP implementation
//
// docs/modules/agent/plan.md Task 13 step 5 says "ARP on Linux via `AF_PACKET`
// behind a build tag". This does not do that, and the reason is recorded rather
// than left as a silent substitution:
//
//  1. sending an ICMP echo to an on-link IPv4 address makes the KERNEL emit the
//     ARP request, because it has to resolve the next hop before it can send
//     the frame. The reply populates the neighbour table. So the ARP evidence
//     is already there, produced by the probe the server authorized, and
//     reading it emits nothing extra.
//  2. it needs no privilege the raw ICMP socket does not already have, and it
//     puts no frame on the wire that is not a consequence of an authorized
//     probe. A hand-rolled AF_PACKET sender would.
//  3. it answers the question `icmp+arp` exists for — a host that answers ARP
//     and drops ICMP — which is exactly the entry that appears in the table
//     without a corresponding echo reply.
//
// The cost, stated: entries can be stale, and the table also names addresses
// this sweep never probed. Both are handled by intersecting with the probed set
// (see run), which is also where §6.4's containment guarantee is enforced —
// this is UNTRUSTED INPUT to a document the server checks against a signed
// scope.
type Neighbours interface {
	Neighbours() (map[netip.Addr]string, error)
}

// Clock is the sweep's own clock: a job-scoped rate limiter has to sleep, and
// a test must not.
//
// It is deliberately NOT scheduler.Clock. That interface has no Sleep,
// precisely so a polling loop cannot be written against it — and this is the
// one place in the agent where sleeping between sends is the correct behaviour
// rather than the forbidden one, because it is bounded by a signed `budget_s`
// inside a job that has already been verified.
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

// Resolver is best-effort reverse DNS. §5.4: `hostname` is DISPLAY-ONLY, is
// never a reconciliation predicate, and never matches an address to a device —
// identity comes from MACs and from inventory, both of which have a chain of
// custody. A failure yields nil.
type Resolver interface {
	Hostname(ctx context.Context, addr netip.Addr) string
}

// Options wires the capability. Every field has a production default; they
// exist so the sweep loop, the scope check and the budget arithmetic are
// testable without a socket.
type Options struct {
	OpenEmitter func(ctx context.Context) (Emitter, error)
	Neighbours  Neighbours
	Clock       Clock
	Resolver    Resolver
	Logger      *slog.Logger
}

// Service is the process-wide holder. The zero value is usable and is what
// `Process` is: it resolves its own defaults at JOB TIME, so registration stays
// a map insert and nothing here opens a socket while the agent is idle.
type Service struct {
	opts Options
	// runs counts completed invocations, so the registry's dormancy assertion
	// can observe that this capability has NOT run. A single atomic load.
	runs atomic.Int64
}

// Process is the Service the default capability registry registers.
var Process = &Service{}

// New returns a Service with explicit wiring. Tests use it; production uses
// Process.
func New(o Options) *Service { return &Service{opts: o} }

// Runs reports how many sweeps this process has completed. Exported for the
// dormancy assertion, which lives outside this package.
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

// Sweep is the `net.sweep` capability.
//
// It takes a jobs.VerifiedJob and nothing else (invariant #1): the runner has
// already checked the Ed25519 signature over the canonical bytes against the
// key pinned at enrollment, that the job targets this device, that it is fresh,
// that its nonce is unseen, and that the type is registered. There is no second
// path into this function.
func (s *Service) Sweep(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	doc, err := s.run(ctx, job.Params())
	if err != nil {
		return nil, err
	}
	s.runs.Add(1)
	return doc, nil
}

func (s *Service) run(ctx context.Context, raw json.RawMessage) (*document, error) {
	p, err := parseParams(raw)
	if err != nil {
		return nil, err
	}

	// ---------------------------------------------------------------------
	// THE SCOPE CHECK. Nothing above this line has opened a socket, and
	// nothing below it runs if this fails.
	// ---------------------------------------------------------------------
	//
	// §6.4 has the server reject an out-of-scope result whole. Relying on that
	// alone means the probes already went out: the rejection protects the
	// DATABASE, not the network. This is the earlier half — the agent must not
	// be able to accidentally produce out-of-scope traffic in the first place.
	//
	// The digest is recomputed over `params.targets` exactly as received. See
	// digest.go: re-deriving that input is how the check keeps agreeing while
	// checking nothing.
	if got := Digest(p.Targets); got != p.ScopeDigest {
		return nil, jobs.Refuse(
			"net.sweep: the scope_digest in the signed params does not match a digest recomputed " +
				"over params.targets, so what this agent would probe is not demonstrably what was " +
				"authorized; no packet was sent")
	}

	// Enumerating happens before the socket too: a target list that yields no
	// address is a sweep with nothing to do, not a socket to open.
	//
	// THE BOUND IS `maxTargets()+1`, AND THE `+1` IS LOAD-BEARING. `truncated`
	// below is derived from `budget < len(plan)` — "the plan held more than we
	// probed". Bounding enumeration at exactly maxTargets() would make that
	// comparison structurally impossible to satisfy and §5.4's flag would go
	// silently false on every sweep that was in fact partial: the operator would
	// stop being told the sweep did not cover the subnet.
	//
	// Asking for one address MORE than can be probed keeps the comparison
	// meaning exactly what it means today. enumerate only produces DISTINCT,
	// in-scope addresses, so a plan of maxTargets()+1 is proof that a
	// (maxTargets()+1)'th address really existed — not that a duplicate or a
	// skipped broadcast was passed over. One extra netip.Addr is the whole cost.
	plan, planErrors := enumerate(p.Targets, p.maxTargets()+1)
	budget := len(plan)
	if p.maxTargets() < budget {
		budget = p.maxTargets()
	}

	// `hosts` and `errors` are `list[...] = Field(default_factory=list)`
	// server-side, NOT Optional -- so a JSON `null` for either is a pydantic
	// validation error and §6.4 rejects the WHOLE document. A nil Go slice
	// marshals to `null`, so both are initialised empty rather than left nil.
	// Observed on the first end-to-end run: `errors: nil` went out as
	// `"errors": null`.
	if planErrors == nil {
		planErrors = []string{}
	}

	started := s.clock().Now().UTC()
	doc := &document{
		// ECHOED VERBATIM, byte for byte, from the signed params (§5.4, §6.4
		// step 2). Never re-parsed and re-rendered: the server compares
		// canonical UUID text, and a round trip through a UUID type is a chance
		// to differ in case or in hyphenation for no benefit at all.
		SubnetID:    p.SubnetID,
		SweepRunID:  p.SweepRunID,
		ScopeDigest: p.ScopeDigest,
		Method:      p.method(),
		StartedAt:   started.Format(rfc3339),
		Hosts:       []host{},
		Errors:      planErrors,
	}

	if budget == 0 {
		doc.FinishedAt = s.clock().Now().UTC().Format(rfc3339)
		return doc, nil
	}

	open := s.opts.OpenEmitter
	if open == nil {
		open = openICMPEmitter
	}
	emitter, err := open(ctx)
	if err != nil {
		// §5.5 and the plan: the capability REFUSES rather than falling back to
		// a TCP connect scan. A silent method substitution would put different
		// packets on someone else's wire than the ones the server authorized —
		// and the operator would read the result as an ICMP sweep.
		//
		// `refused` and not `failed` on purpose: IPAM's dispatcher marks a
		// device that refuses `net.sweep` ineligible for sweeps for 7 days
		// (app/modules/ipam/sweep.py) and picks another device on the segment.
		// For a host whose agent cannot open a raw socket that is exactly the
		// right answer, and it is the fallback §5.3 already designed for.
		return nil, jobs.Refuse("net.sweep requires raw socket privilege: %v", err)
	}
	defer func() {
		if cerr := emitter.Close(); cerr != nil {
			s.logger().Warn("net.sweep: closing the probe socket", "err", cerr)
		}
	}()

	probed, truncated := s.emit(ctx, emitter, plan[:budget], p)
	doc.ProbedCount = len(probed)
	doc.Truncated = truncated || budget < len(plan)

	// Wait out the last probe's flight time before reading what answered.
	if err := s.clock().Sleep(ctx, p.probeTimeout()); err != nil && ctx.Err() != nil {
		doc.Errors = appendError(doc.Errors, "the job deadline passed while waiting for replies")
	}

	doc.Hosts = s.collect(ctx, emitter, probed, p, &doc.Errors, &doc.Method)
	doc.FinishedAt = s.clock().Now().UTC().Format(rfc3339)
	return doc, nil
}

// emit sends the probes, bounded by rate_pps and budget_s.
//
// Both bounds are SIGNED (§5.5), so the ceiling on what a sweep costs is the
// server's and cannot be raised by anything on the endpoint. Hitting either
// stops the sweep and sets `truncated` rather than running longer: §5.5 again,
// and the alternative is a job that outlives its own 600-second TTL and reports
// into a job row the control plane has already written off.
func (s *Service) emit(ctx context.Context, e Emitter, plan []netip.Addr, p params) ([]netip.Addr, bool) {
	clock := s.clock()
	deadline := clock.Now().Add(time.Duration(p.budgetS()) * time.Second)
	interval := time.Second / time.Duration(p.ratePPS())

	probed := make([]netip.Addr, 0, len(plan))
	for i, addr := range plan {
		if ctx.Err() != nil || !clock.Now().Before(deadline) {
			return probed, true
		}
		if i > 0 {
			if err := clock.Sleep(ctx, interval); err != nil {
				return probed, true
			}
		}
		if err := e.Send(addr); err != nil {
			// One unreachable address is not a failed sweep. A whole segment
			// that cannot be reached shows up as zero hosts, which is a fact
			// the subnet page already renders.
			s.logger().Debug("net.sweep: probe not sent", "addr", addr.String(), "err", err)
		}
		probed = append(probed, addr)
	}
	return probed, false
}

// collect turns what answered into `hosts`.
//
// EVERY host that leaves this function is in `probed`, and `probed` is built
// only from addresses enumerated out of `params.targets`. That is the
// containment guarantee §6.4 step 4 checks server-side, held here as well —
// and it is not redundant, because the neighbour table is UNTRUSTED INPUT that
// names addresses this sweep never touched.
func (s *Service) collect(ctx context.Context, e Emitter, probed []netip.Addr, p params, errs *[]string, method *string) []host {
	inScope := make(map[netip.Addr]bool, len(probed))
	for _, a := range probed {
		inScope[a] = true
	}

	answered := e.Responded()
	macs := map[netip.Addr]string{}
	if p.wantARP() {
		src := s.opts.Neighbours
		if src == nil {
			src = defaultNeighbours()
		}
		if src == nil {
			*errs = appendError(*errs, "arp: no neighbour table on this platform; the sweep was "+
				"icmp-only and `method` records what was actually done")
			// Say so in `method` too: the server reads it to decide whether a
			// missing MAC means "none seen" or "never looked".
			*method = "icmp"
		} else if table, err := src.Neighbours(); err != nil {
			*errs = appendError(*errs, "arp: "+err.Error())
		} else {
			macs = table
		}
	}

	// One record per address, at most. handle_sweep_result rejects the WHOLE
	// document if an address repeats (handler.py's `repeated` branch): §2.7 is
	// a current-state table, not a probe log, and a document naming one address
	// twice is describing a time series. Deduping here means a legitimate sweep
	// is never thrown away for it.
	seen := make(map[netip.Addr]bool, len(probed))
	out := make([]host, 0, len(probed))
	for _, addr := range probed {
		if seen[addr] {
			continue
		}
		viaICMP := answered[addr]
		mac, viaARP := macs[addr]
		if !viaICMP && !viaARP {
			// A host that did not answer simply is not in `hosts`. There is no
			// `reachable:false` entry (§5.4).
			continue
		}
		// THE CONTAINMENT FILTER. `answered` comes off the wire and `macs`
		// comes from the kernel's table; neither is this agent's own list.
		if !inScope[addr] {
			continue
		}
		seen[addr] = true
		h := host{Address: addr.String(), RespondedVia: respondedVia(viaICMP, viaARP)}
		if viaARP && mac != "" {
			m := mac
			h.MAC = &m
		}
		if name := s.reverse(ctx, addr); name != "" {
			n := name
			h.Hostname = &n
		}
		out = append(out, h)
	}

	// NOTE what this loop does NOT do: it iterates `probed`, never `macs`. The
	// neighbour table names addresses this sweep never touched -- other
	// segments, the default gateway, whatever the kernel happened to resolve
	// last week -- and an implementation that walked the table and reported
	// what it found would emit observations for addresses outside the signed
	// scope while having sent no packet at all. §0.10: `last_seen_at` is
	// documentation currency, not a cache of what the kernel remembers.
	return out
}

func (s *Service) reverse(ctx context.Context, addr netip.Addr) string {
	r := s.opts.Resolver
	if r == nil {
		r = defaultResolver()
	}
	return r.Hostname(ctx, addr)
}

func respondedVia(icmp, arp bool) string {
	switch {
	case icmp && arp:
		return "both"
	case arp:
		return "arp"
	default:
		return "icmp"
	}
}

// ---------------------------------------------------------------------------
// Params
// ---------------------------------------------------------------------------

func parseParams(raw json.RawMessage) (params, error) {
	var p params
	if err := json.Unmarshal(raw, &p); err != nil {
		return p, fmt.Errorf("net.sweep: params are not a NetSweepParams: %w", err)
	}
	if p.SubnetID == "" || p.SweepRunID == "" {
		return p, fmt.Errorf("net.sweep: params carry no subnet_id/sweep_run_id; the result could " +
			"not be bound to the run that asked for it")
	}
	if len(p.Targets) == 0 {
		return p, fmt.Errorf("net.sweep: params carry no targets; an empty target set is never " +
			"dispatched (§6.2) and probing 'the subnet' is exactly what the agent is not asked to do")
	}
	if len(p.Targets) > maxTargetEntries {
		return p, fmt.Errorf("net.sweep: %d target entries, above the schema's %d",
			len(p.Targets), maxTargetEntries)
	}
	if len(p.ScopeDigest) != digestHexLen {
		return p, fmt.Errorf("net.sweep: scope_digest is %d characters, not %d",
			len(p.ScopeDigest), digestHexLen)
	}
	if m := p.method(); m != "icmp" && m != "icmp+arp" {
		return p, fmt.Errorf("net.sweep: method %q is not one this agent will perform; "+
			"substituting another is putting different packets on the wire than the ones the "+
			"server authorized", m)
	}
	if err := inRange("rate_pps", p.RatePPS, minRatePPS, maxRatePPS); err != nil {
		return p, err
	}
	if err := inRange("probe_timeout_ms", p.ProbeTimeoutMS, minProbeTimeoutMS, maxProbeTimeoutMS); err != nil {
		return p, err
	}
	if err := inRange("max_targets", p.MaxTargets, minMaxTargets, maxMaxTargets); err != nil {
		return p, err
	}
	if err := inRange("budget_s", p.BudgetS, minBudgetS, maxBudgetS); err != nil {
		return p, err
	}
	return p, nil
}

func inRange(name string, v *int, lo, hi int) error {
	if v == nil {
		return nil
	}
	if *v < lo || *v > hi {
		return fmt.Errorf("net.sweep: %s is %d, outside %d..%d — these bounds are the ceiling on "+
			"what a sweep may cost and they are covered by the signature (§5.5)", name, *v, lo, hi)
	}
	return nil
}

func (p params) method() string {
	if p.Method == nil {
		return "icmp+arp"
	}
	return *p.Method
}

func (p params) wantARP() bool { return p.method() == "icmp+arp" }

func (p params) ratePPS() int { return orDefault(p.RatePPS, defaultRatePPS) }

func (p params) probeTimeout() time.Duration {
	return time.Duration(orDefault(p.ProbeTimeoutMS, defProbeTimeoutMS)) * time.Millisecond
}

func (p params) maxTargets() int { return orDefault(p.MaxTargets, defMaxTargets) }

func (p params) budgetS() int { return orDefault(p.BudgetS, defBudgetS) }

func orDefault(v *int, def int) int {
	if v == nil {
		return def
	}
	return *v
}

// ---------------------------------------------------------------------------
// Enumeration
// ---------------------------------------------------------------------------

// enumerate expands the signed targets into AT MOST `limit` addresses to probe,
// in the order the targets were given, deduplicated.
//
// # The bound is on the work, not only on the answer
//
// This function used to take no limit. It expanded EVERY address of EVERY
// signed prefix into a slice and a dedup map, and `run` applied `max_targets`
// afterwards with `plan[:budget]` — so the bound sized the ANSWER while the
// thing that produced it was unbounded. Measured on this repository before the
// change: a /12 cost 295,863,936 bytes to probe at most 65,536 addresses, and a
// /8 cost 5,035,381,632 bytes — five gigabytes of peak allocation, against a
// process whose stated budget in CLAUDE.md is "low-tens-of-MB resident memory".
// That is two orders of magnitude over, and it was invisible to a
// resident-memory reading because the peak is transient: the slice and the map
// are both freed before the handler returns.
//
// Stopping at `limit` DOES NOT CHANGE WHICH ADDRESSES ARE PROBED. Truncation
// kept the first N addresses in enumeration order; stopping early keeps the
// same first N in the same order. Enumeration order is fully determined by the
// order of `targets` and by ascending address within each prefix — `seen` is
// consulted for membership and never iterated — so the probed set is
// byte-identical to what the unbounded version produced. This is a footprint
// change, not a scope change: the agent probes neither more nor fewer
// addresses than before.
//
// # The one thing that DID change: errors[] for targets past the bound
//
// Every target used to be parsed, because truncation happened after the whole
// list had been expanded. So a malformed or IPv6 target sitting AFTER the
// truncation point still contributed an `errors[]` entry describing a target
// this agent was never going to probe. It no longer does: enumeration stops,
// and targets past the stopping point are neither parsed nor reported.
//
// This is deliberate and it is the more honest report — a parse error for an
// address that was never in the probed set describes work that did not happen.
// It is safe to change because `errors[]` is ADVISORY: the server's
// `handle_sweep_result` validates it against NetSweepDocument's 20 x 200
// bounds and then never reads it. No reconciliation, no state, and no
// acceptance decision depends on an entry for an un-probed target. Recorded in
// docs/modules/ipam/spec.md §5.4.
//
// A non-positive `limit` enumerates nothing at all, including no errors.
//
// # What remains unbounded, stated rather than left to be discovered
//
// CPU, not memory. A target whose addresses have all been seen already still
// costs one iteration per address before the loop moves on, so a list of
// overlapping prefixes can spin without producing anything. It is now bounded
// by the limit rather than by the prefix — `seen` can never hold more than
// `limit` entries, so at most `limit` duplicates can be skipped per target
// before the loop starts appending and hits the bound — which is 4,096 targets
// x ~2 x 65,537 iterations in the worst case instead of the 4,096 x 16,777,216
// it was. Bounded and finite, but it is a bound on a CPU cost and not a small
// one; if it ever matters, the fix is to advance past an already-seen RUN of
// addresses rather than one at a time.
//
// Two deliberate exclusions, both strictly INSIDE the authorized scope (probing
// fewer addresses than were authorized is always permitted; probing more never
// is):
//
//   - IPv6 is never enumerated. `app/modules/ipam/net.py` refuses it and
//     `create_sweep` raises `IpamTooLargeError` before signing, so an IPv6
//     target cannot legitimately arrive; §0.5 calls an ICMP sweep of an IPv6
//     prefix "not a hard query, a category error". One that does arrive is
//     recorded in `errors[]` and skipped, rather than turned into an
//     enumeration this host would never finish.
//   - the network and broadcast addresses of an IPv4 prefix of /30 or shorter.
//     A directed-broadcast echo elicits a reply from every host on the segment
//     at once, which is both noisy and the classic amplification shape. §6.4's
//     containment check would permit it; not emitting it is a smaller footprint
//     on someone else's wire for no lost information, because every host that
//     would have answered the broadcast is probed individually anyway.
func enumerate(targets []string, limit int) ([]netip.Addr, []string) {
	var out []netip.Addr
	var errs []string
	seen := map[netip.Addr]bool{}

	for _, t := range targets {
		// The bound is checked HERE as well as in the inner loop, and both
		// checks are needed: the inner one stops expanding the prefix that
		// filled the plan, this one stops the walk moving on to the next
		// target. Without it every remaining target would still be parsed,
		// which is the cost this function exists to stop paying.
		if len(out) >= limit {
			break
		}
		prefix, err := parseTarget(t)
		if err != nil {
			errs = appendError(errs, "target "+clampError(t)+": "+err.Error())
			continue
		}
		if prefix.Addr().Is6() && !prefix.Addr().Is4In6() {
			errs = appendError(errs, "target "+clampError(t)+": IPv6 is stored, documented and "+
				"reconciled but never enumerated (spec §0.5); skipped")
			continue
		}
		addr := prefix.Masked().Addr()
		bits := prefix.Bits()
		total := uint64(1) << uint(32-bits)
		skipEnds := bits <= 30
		for i := uint64(0); i < total; i++ {
			// `out` and `seen` grow together and only together, so bounding
			// the slice bounds the map with it. This is the line that turns a
			// /8 from five gigabytes into the cost of the addresses actually
			// planned.
			if len(out) >= limit {
				break
			}
			if skipEnds && (i == 0 || i == total-1) {
				addr = addr.Next()
				continue
			}
			if !seen[addr] {
				seen[addr] = true
				out = append(out, addr)
			}
			addr = addr.Next()
		}
	}
	return out, errs
}

// parseTarget accepts a CIDR, and a bare address as a host route.
//
// The server always sends `str(ip_network(...))`, which always carries a prefix
// length — but accepting a bare address costs nothing and refusing one would
// turn a harmless server-side rendering change into a fleet-wide sweep outage.
// Note that this parse is for ENUMERATION ONLY: the digest is computed over the
// original strings, never over anything this function produced.
func parseTarget(t string) (netip.Prefix, error) {
	if p, err := netip.ParsePrefix(t); err == nil {
		if p.Addr().Is4In6() {
			return netip.Prefix{}, fmt.Errorf("v4-mapped IPv6 form is ambiguous here")
		}
		return p, nil
	}
	a, err := netip.ParseAddr(t)
	if err != nil {
		return netip.Prefix{}, fmt.Errorf("not a CIDR or an address")
	}
	return netip.PrefixFrom(a, a.BitLen()), nil
}

// appendError adds one bounded entry. `errors` is 20 entries of 200 characters
// server-side, and a document that exceeds either is rejected WHOLE — so the
// bound is enforced here rather than discovered as a lost sweep.
func appendError(errs []string, msg string) []string {
	if len(errs) >= maxErrors {
		return errs
	}
	return append(errs, clampError(msg))
}

func clampError(s string) string {
	if len(s) <= maxErrorLen {
		return s
	}
	n := maxErrorLen
	for n > 0 && s[n]&0xC0 == 0x80 {
		n--
	}
	return s[:n]
}
