package health

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// The apparatus every test in this package shares. Three spies and a policy
// builder, all counting rather than describing: §0.5's falsifiers are
// observations ("the posture prober's call counter is 0"), and a test that
// asserts "no error was returned" is not one of them.

var testStart = time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// ---------------------------------------------------------------------------
// A scheduler that records rather than schedules
// ---------------------------------------------------------------------------

type fakeSched struct {
	fn      func(context.Context)
	id      string
	adds    int
	removes int
}

func (f *fakeSched) Add(id string, _ time.Duration, fn func(context.Context)) error {
	f.id, f.fn, f.adds = id, fn, f.adds+1
	return nil
}

func (f *fakeSched) Remove(id string) {
	if id == f.id {
		f.fn = nil
	}
	f.removes++
}

func (f *fakeSched) Len() int {
	if f.fn == nil {
		return 0
	}
	return 1
}

// tick fires the installed callback once, or fails: a test that "ticked" a
// wheel with nothing on it would pass for the wrong reason.
func (f *fakeSched) tick(t testing.TB) {
	t.Helper()
	if f.fn == nil {
		t.Fatal("no tick callback is installed; this test would be vacuous")
	}
	f.fn(context.Background())
}

func (f *fakeSched) ticks(t testing.TB, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		f.tick(t)
	}
}

// ---------------------------------------------------------------------------
// A sender that counts frames and keeps them
// ---------------------------------------------------------------------------

type spySender struct {
	sends    atomic.Int64
	messages []wire.HealthReport
}

func (s *spySender) Send(_ context.Context, msg any) error {
	s.sends.Add(1)
	if r, ok := msg.(wire.HealthReport); ok {
		s.messages = append(s.messages, r)
	}
	return nil
}

// lines flattens every line this sender has seen, in order.
func (s *spySender) lines() []wire.HealthReportLine {
	var out []wire.HealthReportLine
	for _, m := range s.messages {
		out = append(out, m.Reports...)
	}
	return out
}

func (s *spySender) reset() {
	s.sends.Store(0)
	s.messages = nil
}

// ---------------------------------------------------------------------------
// A sampler that counts calls and answers from a script
// ---------------------------------------------------------------------------

type spySampler struct {
	calls   atomic.Int64
	answers map[string]Sample
	// fallback answers any check type not in `answers`.
	fallback Sample
}

func newSpySampler() *spySampler {
	return &spySampler{answers: map[string]Sample{}}
}

func (s *spySampler) Sample(checkType, selector string) Sample {
	s.calls.Add(1)
	if got, ok := s.answers[checkType+"|"+selector]; ok {
		return got
	}
	if got, ok := s.answers[checkType]; ok {
		return got
	}
	return s.fallback
}

func num(v float64) Sample { return Sample{Available: true, Num: v} }

// ---------------------------------------------------------------------------
// An event-log source that counts every borrow
// ---------------------------------------------------------------------------

// spyEventLog is §0.5 falsifier 5's observable. `borrows` counts the number of
// times this package asked for an OS log subscription; the falsifier is that it
// stays at zero.
type spyEventLog struct {
	borrows atomic.Int64
	reads   atomic.Int64
}

func (s *spyEventLog) CountSince(string) (int, bool) {
	s.reads.Add(1)
	return 0, true
}

func (s *spyEventLog) borrow() EventLogCounters {
	s.borrows.Add(1)
	return s
}

// ---------------------------------------------------------------------------
// A policy builder
// ---------------------------------------------------------------------------

type checkJSON struct {
	CheckID    string `json:"check_id"`
	CheckType  string `json:"check_type"`
	Selector   string `json:"selector"`
	Comparator string `json:"comparator"`
	Threshold  string `json:"threshold"`
	ForTicks   int    `json:"for_ticks"`
	Cadence    string `json:"cadence"`
}

type policyJSON struct {
	PolicyVersion    int         `json:"policy_version"`
	TickIntervalS    int         `json:"tick_interval_s"`
	PostureIntervalS int         `json:"posture_interval_s"`
	SummaryIntervalS int         `json:"summary_interval_s"`
	Checks           []checkJSON `json:"checks"`
}

// defaultPolicy is §0.5's default profile: a 300 s fast tick, a 3600 s posture
// sub-tick, and a summary interval far enough out that a test has to ask for a
// summary rather than trip over one.
func defaultPolicy(version int, checks ...checkJSON) policyJSON {
	return policyJSON{
		PolicyVersion:    version,
		TickIntervalS:    300,
		PostureIntervalS: 3600,
		SummaryIntervalS: 86400,
		Checks:           checks,
	}
}

// summaryEveryTick is the default profile with the summary interval pinned to
// the tick interval, so a test that is about summaries does not have to run 288
// ticks to see one. Both numbers are inside the schema's bounds (tick 60..3600,
// summary 300..86400), so this is a legal policy and not a test-only shape.
func summaryEveryTick(version int, checks ...checkJSON) policyJSON {
	return policyJSON{
		PolicyVersion:    version,
		TickIntervalS:    300,
		PostureIntervalS: 3600,
		SummaryIntervalS: 300,
		Checks:           checks,
	}
}

func (p policyJSON) raw(t testing.TB) json.RawMessage {
	t.Helper()
	b, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// numericCheck is the shape most tests want: a fast-tick numeric threshold.
func numericCheck(id, checkType, comparator, threshold string, forTicks int) checkJSON {
	return checkJSON{
		CheckID: id, CheckType: checkType, Comparator: comparator,
		Threshold: threshold, ForTicks: forTicks, Cadence: "tick",
	}
}

func postureCheck(id, checkType, threshold string) checkJSON {
	return checkJSON{
		CheckID: id, CheckType: checkType, Comparator: "is",
		Threshold: threshold, ForTicks: 1, Cadence: "posture",
	}
}

// twelveChecks is §0.5's "default profile (<= 12 checks)".
func twelveChecks() []checkJSON {
	out := make([]checkJSON, 0, 12)
	for i := 0; i < 8; i++ {
		out = append(out, numericCheck(fmt.Sprintf("check-%02d", i), "cpu.busy_pct", "gt", "95", 3))
	}
	for i := 8; i < 12; i++ {
		out = append(out, postureCheck(fmt.Sprintf("check-%02d", i), "posture.disk_encryption", "true"))
	}
	return out
}

// ---------------------------------------------------------------------------
// A wired engine
// ---------------------------------------------------------------------------

type rig struct {
	engine  *Engine
	sched   *fakeSched
	sender  *spySender
	tick    *spySampler
	posture *spySampler
	log     *spyEventLog
	now     time.Time
}

func newRig(t testing.TB) *rig {
	t.Helper()
	r := &rig{
		sched:   &fakeSched{},
		sender:  &spySender{},
		tick:    newSpySampler(),
		posture: newSpySampler(),
		log:     &spyEventLog{},
		now:     testStart,
	}
	r.engine = NewEngine(Options{
		Scheduler:      r.sched,
		Sender:         r.sender,
		Tick:           r.tick,
		Posture:        r.posture,
		BorrowEventLog: r.log.borrow,
		Now:            func() time.Time { return r.now },
		Logger:         quiet(),
	})
	return r
}

// apply installs a policy through the same function the signed job does.
func (r *rig) apply(t testing.TB, p policyJSON) {
	t.Helper()
	pol, _, err := parsePolicy(p.raw(t))
	if err != nil {
		t.Fatalf("parsePolicy: %v", err)
	}
	if err := r.engine.install(pol); err != nil {
		t.Fatalf("install: %v", err)
	}
}

// processCheck builds a `process.present` check. Boolean kind, so `is` is the
// only legal comparator (§4.1) -- numericCheck's gt/lt would be rejected.
func processCheck(id, selector string) checkJSON {
	return checkJSON{
		CheckID: id, CheckType: "process.present", Comparator: "is",
		Threshold: "true", Selector: selector, ForTicks: 1, Cadence: "tick",
	}
}
