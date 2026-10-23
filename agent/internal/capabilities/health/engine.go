package health

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// The four values `state` takes.
//
// There are four rather than §0.7's three because "unavailable" has to remember
// whether an alert is open on the server. §0.7: `unavailable` "never opens an
// alert and NEVER CLEARS ONE" -- so a check that was breaching and becomes
// unanswerable must emit no recovery, and must still emit one later if it comes
// back answerable and healthy. Losing that bit would either clear a real
// finding silently or leave an alert open forever.
//
// Both unavailable values REPORT as "unavailable"; the distinction never
// reaches the wire.
const (
	stateOK uint8 = iota
	stateBreaching
	stateUnavailable
	stateUnavailableWasBreaching
)

// checkState is §0.5's struct, exactly: `consecutive int32`, `state uint8`,
// `last float32`. Twelve bytes, and NO SAMPLE HISTORY AT ALL -- no ring buffer,
// no rolling average, no "last 10 values". §0.12: a moving average is a time
// series, and a time series on the endpoint is the first step to becoming the
// monitoring product.
//
// `last` carries the most recent observation for the SUMMARY to render:
// the value itself for a numeric check, 1/0 for a boolean, and the registry
// index of the member for an enum. A byte count above float32's 24-bit mantissa
// is therefore rounded in a summary line -- the edge lines render from the live
// sample and are exact. That rounding is the price of §0.5's field list, and it
// is recorded rather than quietly widened to float64.
type checkState struct {
	consecutive int32
	state       uint8
	last        float32
}

func breaching(state uint8) bool {
	return state == stateBreaching || state == stateUnavailableWasBreaching
}

// reportedState maps the four internal values onto §5.1's three wire values.
func reportedState(state uint8) string {
	switch state {
	case stateBreaching:
		return "breaching"
	case stateUnavailable, stateUnavailableWasBreaching:
		return "unavailable"
	}
	return "ok"
}

// Scheduler is the timer wheel, narrowed to what this module uses.
//
// It is an interface so the engine can be driven by a fake in tests, and so
// this package does not reach past Add/Remove/Len into the wheel's internals.
// *scheduler.Wheel satisfies it.
type Scheduler interface {
	Add(id string, every time.Duration, fn func(context.Context)) error
	Remove(id string)
	Len() int
}

// Sender is the outbound half of the gateway connection. jobs.Outbox satisfies
// it, so a report produced while the uplink is down is still owed and still
// delivered (§5.1: delivery is at-least-once).
type Sender interface {
	Send(ctx context.Context, msg any) error
}

// EventLogCounters is the security-logs capability's EXISTING OS subscription,
// borrowed (§0.5 item 5, §4.1†).
//
// It exists as a type so the borrowing is explicit and so the falsifier can be
// observed: `Options.BorrowEventLog` is the ONLY way this package could obtain
// one, and the tick engine never calls it. §0.5 falsifier 5 is "an event-log
// check type opening its OWN OS log subscription", and the observation is that
// counter staying at zero -- not a promise in a comment.
//
// There is no security-logs capability in this build, so every `eventlog.*`
// check reports `unavailable` (§0.7). It does NOT open a second subscription to
// make itself work: two subscriptions to one log for two modules is the
// always-on collector the brief forbids.
type EventLogCounters interface {
	// CountSince returns the number of matching events since the previous read.
	CountSince(condition string) (int, bool)
}

// tickEntryID is the wheel entry. There is exactly ONE, whatever the check
// count -- §0.5's mechanism, and the reason a 32-check policy is not 32 tickers.
const tickEntryID = "health.tick"

// rfc3339 matches app/jobs/canonical.py::rfc3339 -- whole seconds, literal Z.
const rfc3339 = "2006-01-02T15:04:05Z"

// Options configures an Engine.
type Options struct {
	// Scheduler is the process's one timer wheel. Required.
	Scheduler Scheduler
	// Sender carries health_report frames. Required.
	Sender Sender
	// Tick samples fast-cadence checks. Required.
	Tick Sampler
	// Posture samples posture-cadence checks. It is a SEPARATE field from Tick
	// so §0.5 falsifier 3 has something to count: the expensive shell-outs
	// (BitLocker/FileVault/LUKS, AV health, firewall state) live behind this
	// one, and a test asserts its call counter is zero across fast ticks.
	Posture Sampler
	// BorrowEventLog hands back the security-logs capability's subscription.
	// Nil in this build. See EventLogCounters.
	BorrowEventLog func() EventLogCounters
	Now            func() time.Time
	Logger         *slog.Logger
}

// Engine is the tick engine: one wheel entry, one fixed-size state array, and
// nothing else (§4.3 -- "between ticks: nothing").
type Engine struct {
	sched          Scheduler
	sender         Sender
	tickSampler    Sampler
	postureSampler Sampler
	borrowEventLog func() EventLogCounters
	now            func() time.Time
	log            *slog.Logger

	mu     sync.Mutex
	pol    *policy
	states []checkState
	ticks  int
	// lines is the reused report scratch. Reused, never appended to across
	// ticks -- §0.5's mechanism, and what makes AllocsPerOp zero on a quiet
	// tick.
	lines []wire.HealthReportLine
}

// NewEngine builds an engine. It installs nothing and schedules nothing: an
// engine with no policy is inert, which is the state a freshly enrolled device
// is in until the first `health.policy.apply` arrives.
func NewEngine(o Options) *Engine {
	e := &Engine{
		sched:          o.Scheduler,
		sender:         o.Sender,
		tickSampler:    o.Tick,
		postureSampler: o.Posture,
		borrowEventLog: o.BorrowEventLog,
		now:            o.Now,
		log:            o.Logger,
	}
	if e.now == nil {
		e.now = time.Now
	}
	if e.log == nil {
		e.log = slog.Default()
	}
	return e
}

// PolicyVersion is the version this agent believes it is running.
//
// It is load-bearing: app/modules/health/ingest.py:552 DISCARDS a report whose
// `policy_version < acknowledged - 1`, so an agent that lies or lags by two
// versions has its breaches silently dropped. A disarmed agent reports 0.
func (e *Engine) PolicyVersion() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.pol == nil {
		return 0
	}
	return e.pol.version
}

// Checks is how many checks are installed. Exported for tests and for the
// dormancy assertions; it is a slice length, not a scan.
func (e *Engine) Checks() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.pol == nil {
		return 0
	}
	return len(e.pol.checks)
}

// install replaces the whole policy and re-arms the wheel.
//
// WHOLESALE, never merged (§5.2). Two live copies of one check would double
// every report the endpoint sends, and scheduler.Wheel.Add replaces an entry
// with the same id for the same reason.
//
// An EMPTY check list is a legal policy meaning "run nothing" and DISARMS the
// device: the wheel entry is removed, so a disarmed agent has no health work on
// the wheel at all rather than a callback that wakes up to do nothing.
func (e *Engine) install(pol *policy) error {
	e.mu.Lock()
	e.pol = pol
	// One allocation per install, sized to the policy. Not per tick.
	//
	// Every check starts UNAVAILABLE, not `ok`. §0.7: absent is not the same as
	// healthy. A posture check whose sub-tick has not come round yet, or any
	// check on a device that has not ticked since the policy landed, has not
	// been observed -- and a summary that reported `ok` for it would be
	// asserting a fact nobody measured. This was caught by a test rather than
	// designed in: the zero value of `state` is stateOK, and the first summary
	// after an install reported every unsampled check healthy.
	e.states = make([]checkState, len(pol.checks))
	for i := range e.states {
		e.states[i].state = stateUnavailable
	}
	e.ticks = 0
	e.mu.Unlock()

	if len(pol.checks) == 0 {
		e.sched.Remove(tickEntryID)
		return nil
	}
	return e.sched.Add(tickEntryID, pol.tick, e.onTick)
}

// onTick is the wheel callback: ONE entry, whatever the check count.
//
// The posture sub-tick is a COUNTER on this tick, not a second wheel entry
// (§0.5's mechanism). So is the summary.
func (e *Engine) onTick(ctx context.Context) {
	e.mu.Lock()
	pol := e.pol
	if pol == nil || len(pol.checks) == 0 {
		e.mu.Unlock()
		return
	}
	e.ticks++
	posture := e.ticks%pol.posturePer == 0
	summary := e.ticks%pol.summaryPer == 0
	version := pol.version

	e.lines = e.lines[:0]
	stamp := "" // rendered only if something is going to be said

	for i := range pol.checks {
		c := &pol.checks[i]
		st := &e.states[i]

		if c.posture && !posture {
			// The expensive probes do not run on the fast tick. §0.5 falsifier
			// 3, and the reason the posture sampler is a separate field.
			continue
		}
		s := e.sample(c, c.posture)
		kind := e.advance(c, st, s)
		if kind != "" {
			if stamp == "" {
				stamp = e.now().UTC().Format(rfc3339)
			}
			e.lines = append(e.lines, line(c, st, s, kind, stamp))
		}
	}

	if summary {
		if stamp == "" {
			stamp = e.now().UTC().Format(rfc3339)
		}
		for i := range pol.checks {
			c := &pol.checks[i]
			st := &e.states[i]
			// A summary reports STORED state; it does not re-sample. Sampling
			// again here would run every posture probe on the summary tick,
			// which is falsifier 3 wearing a different hat.
			e.lines = append(e.lines, line(c, st, c.stored(st), "summary", stamp))
		}
	}

	out := e.take()
	e.mu.Unlock()

	// A tick with no transition and no summary due says NOTHING. §0.5
	// falsifier 4: an endpoint that reports on every tick spends the CONTROL
	// PLANE's budget instead of its own -- invisible on the device, and ingest
	// load nobody can attribute.
	e.emit(ctx, version, out)
}

// sample reads one check.
//
// `eventlog.*` never reaches a sampler: §4.1† and §0.5 item 5. Those types are
// counters read from the security-logs capability's EXISTING subscription, that
// capability is not built, and this module does not open one of its own -- so
// they report `unavailable`, which is a real answer (§0.7) and not a failure.
func (e *Engine) sample(c *checkSpec, posture bool) Sample {
	if !c.known {
		// A check type a newer control plane knows and this build does not.
		return unavailable()
	}
	if len(c.checkType) >= len(eventlogPrefix) && c.checkType[:len(eventlogPrefix)] == eventlogPrefix {
		return unavailable()
	}
	s := e.postureSampler
	if !posture {
		s = e.tickSampler
	}
	if s == nil {
		return unavailable()
	}
	return s.Sample(c.checkType, c.selector)
}

// advance applies §4.2 to one check and returns the line kind to emit, or "".
//
// Breach is duration-gated; recovery is a SINGLE sample. The asymmetry is
// deliberate (§4.2): a slow-to-open, fast-to-close alert errs toward silence.
func (e *Engine) advance(c *checkSpec, st *checkState, s Sample) string {
	if !s.Available {
		// §0.7: unavailable never opens an alert and never clears one. No edge
		// is emitted, and the consecutive counter is left where it was rather
		// than advanced -- an unanswerable tick is not evidence either way.
		if breaching(st.state) {
			st.state = stateUnavailableWasBreaching
		} else {
			st.state = stateUnavailable
		}
		return ""
	}
	st.last = c.storeValue(s)

	if s.matches(c) {
		if breaching(st.state) {
			// Already open server-side. Coming back from `unavailable` to
			// `breaching` is not a new edge.
			st.state = stateBreaching
			return ""
		}
		st.consecutive++
		if st.consecutive >= c.forTicks {
			st.state = stateBreaching
			return "breach"
		}
		st.state = stateOK
		return ""
	}

	st.consecutive = 0
	if breaching(st.state) {
		st.state = stateOK
		return "recovery"
	}
	st.state = stateOK
	return ""
}

// take copies the scratch lines out. The copy is deliberate: `lines` is reused
// on the next tick, and a Sender that holds its argument (a buffered outbox
// does, until it has written it) would otherwise be handed a slice that changes
// underneath it.
func (e *Engine) take() []wire.HealthReportLine {
	if len(e.lines) == 0 {
		return nil
	}
	out := make([]wire.HealthReportLine, len(e.lines))
	copy(out, e.lines)
	return out
}

// line builds one HealthReportLine.
func line(c *checkSpec, st *checkState, s Sample, kind, stamp string) wire.HealthReportLine {
	return wire.HealthReportLine{
		CheckID:    c.checkID,
		Selector:   c.selector,
		Kind:       kind,
		State:      reportedState(st.state),
		Value:      renderValue(s, c),
		ObservedAt: stamp,
	}
}

// stored reconstructs the last observation from the 12-byte state, for a
// summary line. See checkState's comment on the float32.
func (c *checkSpec) stored(st *checkState) Sample {
	if st.state == stateUnavailable || st.state == stateUnavailableWasBreaching {
		return unavailable()
	}
	switch c.kind {
	case kindBoolean:
		return Sample{Available: true, Bool: st.last != 0}
	case kindEnum:
		idx := 0
		if st.last != 0 {
			idx = 1
		}
		return Sample{Available: true, Enum: c.enum[idx]}
	}
	return Sample{Available: true, Num: float64(st.last)}
}

// storeValue folds an observation into the single float32 §0.5 allows.
func (c *checkSpec) storeValue(s Sample) float32 {
	switch c.kind {
	case kindBoolean:
		if s.Bool {
			return 1
		}
		return 0
	case kindEnum:
		if s.Enum == c.enum[1] {
			return 1
		}
		return 0
	}
	return float32(s.Num)
}

// CheckNow samples every check, including posture ones, and emits a full
// summary (§5.3).
//
// It is the `health.check.now` job's body. Dispatching a job is not a fast
// tick, so running the posture probes here is correct and is not what §0.5
// falsifier 3 forbids -- the falsifier is about the SCHEDULED fast tick.
func (e *Engine) CheckNow(ctx context.Context) error {
	e.mu.Lock()
	pol := e.pol
	if pol == nil || len(pol.checks) == 0 {
		e.mu.Unlock()
		// Nothing installed, or a disarmed device. `reports` is min_length=1
		// server-side: an empty array is rejected WHOLE by parse_agent_message
		// and the only trace is a logger.warning. So nothing is sent.
		return nil
	}
	version := pol.version
	stamp := e.now().UTC().Format(rfc3339)
	e.lines = e.lines[:0]
	for i := range pol.checks {
		c := &pol.checks[i]
		st := &e.states[i]
		s := e.sample(c, c.posture)
		// The counters advance exactly as on a tick, and an edge crossed here
		// is emitted as an edge: an operator pressing "refresh now" must not
		// see a state the server never learned about.
		if kind := e.advance(c, st, s); kind != "" {
			e.lines = append(e.lines, line(c, st, s, kind, stamp))
		}
		e.lines = append(e.lines, line(c, st, s, "summary", stamp))
	}
	out := e.take()
	e.mu.Unlock()
	return e.emit(ctx, version, out)
}

// emit chunks and sends. It is the only place this package writes to the wire.
func (e *Engine) emit(ctx context.Context, version int, lines []wire.HealthReportLine) error {
	var firstErr error
	for _, batch := range chunk(lines, maxReportsPerMessage) {
		msg := wire.HealthReport{PolicyVersion: version, Reports: batch}
		if err := e.sender.Send(ctx, msg); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}
