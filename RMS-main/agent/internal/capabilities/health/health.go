// Package health implements the `health.check` edge tick
// (docs/modules/endpoint-health-alerting/spec.md §0.4, §0.5, §4, §5) as two
// signed job types: `health.policy.apply` and `health.check.now`.
//
// # Why a policy arrives as a job
//
// app/gateway/protocol.py defines exactly one server -> agent message,
// JobEnvelope. There is no policy channel, and inventing one would be a new
// instruction path into a root/SYSTEM process that the signed-job runner does
// not verify -- invariant #1, straightforwardly (§0.4). So the threshold policy
// is a signed job, and installing one inherits the whole signed-job safety
// story: signature over canonical bytes against the pinned key, target-device
// match, freshness, non-replay, known type -- all before Apply is reached.
//
// # What is evaluated here, and what is not
//
// Evaluated at the edge: every per-tick sample, its comparator, and its
// consecutive-tick counter. Evaluated on the server: `agent.offline` (§0.6, and
// an agent that is offline cannot report that it is offline), plus the entire
// alert lifecycle, dedup, flap suppression and actions. This package emits
// EDGES and SUMMARIES. It opens no alert, retains no series, and holds twelve
// bytes per check (§0.5, §0.12).
//
// # The idle budget, measured
//
// §0.5's claim -- "< 2 MB resident, < 0.1% of one core, ZERO network writes on
// a tick where nothing changed" -- shipped with six named falsifiers and no
// coverage, for an entire phase, because the code they measure lived in a
// repository this CI could not see. budget_test.go in this package is where
// each of the six becomes an assertion that fails CI, wired one-for-one to
// internal/budget's harness. Each is an ordinary Test calling
// testing.Benchmark, because `go test -bench` exits 0 on a regression -- that
// was measured on this project, not assumed (see internal/budget's package
// comment).
//
// The CPU half of the claim is NOT asserted as wall-clock. A shared runner's
// variance exceeds the signal, a threshold loose enough not to flake is loose
// enough to pass a real regression, and a flaky budget test gets disabled --
// which reads as coverage while measuring nothing. It is asserted through the
// machine-independent proxies internal/budget established: allocation counts,
// goroutine counts, network writes, and retained heap across a hundredfold tick
// spread.
//
// # Dormancy
//
// Registration is a map insert. Process's engine is built on FIRST USE, from
// Bind's wiring, and nothing here reads /proc, opens a handle or starts a
// goroutine until a verified job arrives. An engine with no policy holds no
// wheel entry; a disarmed policy REMOVES the one it had (§5.2 -- an empty
// `checks` list is a legal policy meaning "run nothing").
//
// # Compatibility (docs/kernel/wire-protocol/README.md)
//
// This package adds NO wire kind and NO field. `health_report` and both params
// schemas already ship. Inbound is loose: an unknown member of
// HealthPolicyApplyParams is ignored, and an unknown `check_type` is installed
// and reports `unavailable` (§0.7) rather than costing the device its whole
// policy -- so a new agent meeting an old server, and an old agent meeting a
// new server, both keep evaluating what they can. Outbound is exact:
// wire.Encode writes `v` and `kind`, and HealthReportMsg is extra="forbid".
package health

import (
	"context"
	"log/slog"
	"sync"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Job types. They match app/modules/health/jobtypes.py; a mismatch would be
// refused `unknown_type` by this agent's own runner -- the correct answer, and
// a confusing one, so the strings live in one place per side.
const (
	JobTypePolicyApply = "health.policy.apply"
	JobTypeCheckNow    = "health.check.now"
)

// Service is the process-wide holder of the tick engine.
//
// The zero value is usable and inert: it is what `Process` is. It becomes able
// to install a policy when main.go calls Bind with the process's wheel and
// outbox. Until then a `health.policy.apply` job FAILS with a named error
// rather than installing a policy nothing will ever tick -- an agent that
// accepted a policy it cannot run would acknowledge a `policy_version` it is
// not evaluating, and §12's reconciliation sweep would then never re-push it.
type Service struct {
	mu     sync.Mutex
	engine *Engine
}

// Process is the Service the default capability registry registers.
var Process = &Service{}

// Bind wires the engine to the process's scheduler and outbound sender.
//
// Called once, from main, after the wheel and the outbox exist. It builds the
// engine but installs no policy and adds no wheel entry: §0.9's argument
// against a locally scheduled anything applies here too -- the cadence arrives
// signed, or it does not exist.
func (s *Service) Bind(o Options) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.engine = NewEngine(o)
}

// engineOrNil returns the bound engine.
func (s *Service) engineOrNil() *Engine {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.engine
}

// ErrUnbound is returned when a health job arrives before Bind.
var ErrUnbound = errUnbound{}

type errUnbound struct{}

func (errUnbound) Error() string {
	return "health: no tick engine is bound in this process, so a policy installed here would " +
		"never tick; refusing rather than acknowledging a policy_version this agent is not evaluating"
}

// Apply is the `health.policy.apply` capability.
//
// The job is a jobs.VerifiedJob and nothing else (invariant #1): this function
// is unreachable except through the signed-job runner, because that type has no
// constructor outside internal/jobs.
func (s *Service) Apply(_ context.Context, job jobs.VerifiedJob) (any, error) {
	e := s.engineOrNil()
	if e == nil {
		return nil, ErrUnbound
	}
	pol, dropped, err := parsePolicy(job.Params())
	if err != nil {
		// REFUSED BEFORE INSTALL. §0.5 falsifier 6's observation is that the
		// tick engine ends up armed with an over-cap policy, so the ordering
		// here is the guarantee and the error is only its report.
		return nil, err
	}
	for _, id := range dropped {
		// §0.6: the server filters `agent.offline` out of every push. One
		// arriving is a control-plane bug, and inventing an evaluation for it
		// on the endpoint would be inventing a fact.
		e.log.Warn("dropped a server-evaluated check from a pushed policy",
			"check_id", id, "check_type", "agent.offline", "policy_version", pol.version)
	}
	if err := e.install(pol); err != nil {
		return nil, err
	}
	e.log.Info("health policy installed",
		"policy_version", pol.version, "checks", len(pol.checks),
		"tick_s", int(pol.tick.Seconds()), "wheel_entries", e.sched.Len())
	// The result stamps device_health_policy_ack promptly (§5.2). Correctness
	// does not depend on it -- the version also arrives in the next summary --
	// so the shape is deliberately minimal.
	return map[string]any{"policy_version": pol.version, "checks": len(pol.checks)}, nil
}

// CheckNow is the `health.check.now` capability (§5.3): sample every check now
// and emit a full summary.
//
// HealthCheckNowParams is `{}` and this reads nothing from the job. A parameter
// would be a second, unversioned way to configure the tick.
func (s *Service) CheckNow(ctx context.Context, _ jobs.VerifiedJob) (any, error) {
	e := s.engineOrNil()
	if e == nil {
		return nil, ErrUnbound
	}
	if err := e.CheckNow(ctx); err != nil {
		return nil, err
	}
	return map[string]any{"policy_version": e.PolicyVersion(), "checks": e.Checks()}, nil
}

// Logger is the engine's logger, or the default. Small helper so Apply can log
// without reaching into Engine's fields from another file.
func (e *Engine) Logger() *slog.Logger { return e.log }
