package logs

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

// fakeLog is a synthetic Windows Security channel.
//
// EVERY RECORD IS DIFFERENT, AND THAT IS THE POINT OF THIS TYPE. A fixture
// where every batch is the same size and every event carries the same
// timestamp cannot tell "resumed at the cursor" from "started over": both
// produce 500 identical-looking events. So each record here has its own record
// id, its own timestamp (advancing by a varying interval), its own username and
// its own event id -- drawn from a rota that deliberately includes ids OUTSIDE
// the taxonomy, so the drop path and the cursor's advance past dropped records
// are exercised by the same stream.
type fakeLog struct {
	records []fakeRecord
	// opens counts Open calls, so a test can see a re-read that should not have
	// happened.
	opens int
	// maxOutstanding is the largest number of records this source has handed
	// out that the consumer had not yet finished with. The streaming assertion
	// reads it.
	rewound   bool
	rewindErr error
	failAt    int // index at which Next returns an error; -1 for never
}

type fakeRecord struct {
	id     int
	ts     time.Time
	user   string
	eventD int
}

// taxonomyRota is the event-id sequence the synthetic log cycles through. Seven
// of the ten are collected; 4688 and 4663 are not, and 5379 is not -- so
// roughly a third of the stream is dropped, as it would be on a real endpoint.
var taxonomyRota = []int{4624, 4688, 4625, 4672, 4663, 4732, 4634, 5379, 4740, 4719}

func newFakeLog(n int) *fakeLog {
	f := &fakeLog{failAt: -1}
	base := time.Date(2026, 7, 18, 9, 0, 0, 0, time.UTC)
	for i := 0; i < n; i++ {
		f.records = append(f.records, fakeRecord{
			// Record ids start at 1000 and advance by a VARYING amount (7, 8, 9
			// or 4): a real channel is not contiguous, because the query filters
			// ids out server-side, and a cursor test against a contiguous stream
			// cannot catch an off-by-one that only shows on a gap. Strictly
			// increasing, because EventRecordID is -- an earlier version of this
			// generator used a step of 3 with the same jitter and produced
			// DUPLICATE ids, which made the resume assertion fail against a
			// cursor that was in fact correct.
			id: 1000 + i*7 + i%4,
			// Timestamps advance by a varying interval, so two records never
			// share one and "which record is this?" is answerable from the
			// event alone.
			ts:     base.Add(time.Duration(i)*1700*time.Millisecond + time.Duration(i%7)*time.Second),
			user:   fmt.Sprintf("user%04d", i),
			eventD: taxonomyRota[i%len(taxonomyRota)],
		})
	}
	return f
}

func (f *fakeLog) Name() string { return "fake-eventlog" }

func (f *fakeLog) Normalize(rec Record) (wire.SecurityEvent, bool, error) {
	return parseWinEvent(rec.Payload, rec.Position)
}

func (f *fakeLog) Rewound(context.Context, string) (bool, error) {
	return f.rewound, f.rewindErr
}

func (f *fakeLog) Open(_ context.Context, after string) (Reader, error) {
	f.opens++
	start := 0
	if after != "" {
		n, err := strconv.Atoi(after)
		if err != nil {
			return nil, fmt.Errorf("bad cursor %q", after)
		}
		for start < len(f.records) && f.records[start].id <= n {
			start++
		}
	}
	return &fakeReader{log: f, i: start}, nil
}

type fakeReader struct {
	log    *fakeLog
	i      int
	served int
	closed bool
}

func (r *fakeReader) Next() (Record, error) {
	if r.log.failAt >= 0 && r.served == r.log.failAt {
		return Record{}, errors.New("the event log service went away")
	}
	if r.i >= len(r.log.records) {
		return Record{}, io.EOF
	}
	rec := r.log.records[r.i]
	r.i++
	r.served++
	return Record{Position: strconv.Itoa(rec.id), Payload: renderWinXML(rec)}, nil
}

func (r *fakeReader) Close() error { r.closed = true; return nil }

// renderWinXML builds a rendered EventXml document for one synthetic record.
//
// Built from a template rather than read from testdata because the point of
// this stream is VARIATION -- ten thousand distinct records -- and ten thousand
// files would be a fixture set nobody reads. The SHAPE is the one testdata/'s
// hand-written documents encode, and winevent_test.go is what checks the shape
// against those. testdata/README.md records that neither is captured output.
func renderWinXML(rec fakeRecord) []byte {
	return []byte(fmt.Sprintf(`<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
  <System>
    <EventID>%d</EventID>
    <TimeCreated SystemTime="%s"/>
    <EventRecordID>%d</EventRecordID>
    <Channel>Security</Channel>
    <Computer>WS-0142.corp.toeshee.local</Computer>
  </System>
  <EventData>
    <Data Name="TargetUserName">%s</Data>
    <Data Name="TargetDomainName">TOESHEE</Data>
    <Data Name="SubjectUserName">%s</Data>
    <Data Name="SubjectDomainName">TOESHEE</Data>
    <Data Name="MemberName">CN=%s,OU=Users,DC=corp</Data>
    <Data Name="LogonType">3</Data>
    <Data Name="IpAddress">10.20.7.%d</Data>
  </EventData>
</Event>`, rec.eventD, rec.ts.Format("2006-01-02T15:04:05.0000000Z"), rec.id,
		rec.user, rec.user, rec.user, rec.id%254))
}

// spySender records every batch it is handed.
type spySender struct {
	batches [][]wire.SecurityEvent
	failOn  int // 1-based batch number to fail on; 0 for never
	sent    int
}

func (s *spySender) Send(_ context.Context, msg any) error {
	ev, ok := msg.(wire.SecurityEvents)
	if !ok {
		return fmt.Errorf("logs.ship sent a %T; the only outbound kind it may use is "+
			"wire.SecurityEvents", msg)
	}
	s.sent++
	if s.failOn == s.sent {
		return errors.New("the uplink went away")
	}
	s.batches = append(s.batches, ev.Events)
	return nil
}

func (s *spySender) events() []wire.SecurityEvent {
	var all []wire.SecurityEvent
	for _, b := range s.batches {
		all = append(all, b...)
	}
	return all
}

// countingSender counts and DISCARDS. Used wherever the number of events is
// large: a spy that retained 100,000 events would report the test's own
// apparatus as this capability's footprint, which is a mistake this module has
// made before (health's falsifier file read 53 KB of engine growth from a spy's
// message log).
type countingSender struct {
	batches int
	events  int
	last    wire.SecurityEvent
	first   wire.SecurityEvent
}

func (c *countingSender) Send(_ context.Context, msg any) error {
	ev := msg.(wire.SecurityEvents)
	if c.batches == 0 && len(ev.Events) > 0 {
		c.first = ev.Events[0]
	}
	c.batches++
	c.events += len(ev.Events)
	if n := len(ev.Events); n > 0 {
		c.last = ev.Events[n-1]
	}
	return nil
}

// memCursor is an in-memory CursorStore.
type memCursor struct {
	cur    Cursor
	saves  int
	loadEr error
	saveEr error
}

func (m *memCursor) Load() (Cursor, error) {
	if m.loadEr != nil {
		return Cursor{}, m.loadEr
	}
	return m.cur, nil
}

func (m *memCursor) Save(c Cursor) error {
	if m.saveEr != nil {
		return m.saveEr
	}
	m.saves++
	m.cur = c
	return nil
}

func quiet() *slog.Logger {
	return slog.New(slog.NewTextHandler(discardWriter{}, &slog.HandlerOptions{Level: slog.LevelError}))
}

type discardWriter struct{}

func (discardWriter) Write(p []byte) (int, error) { return len(p), nil }

func fixedNow() time.Time { return time.Date(2026, 8, 6, 12, 0, 0, 0, time.UTC) }

func shipOnce(t *testing.T, s *Service, opts Options, src Source, store CursorStore) *result {
	t.Helper()
	res, err := s.ship(context.Background(), opts, src, store)
	if err != nil {
		t.Fatalf("ship: %v", err)
	}
	return res
}

// ---------------------------------------------------------------------------
// Invariant #1
// ---------------------------------------------------------------------------

func TestTheCapabilityTakesAVerifiedJobAndNothingElse(t *testing.T) {
	// A compile-time fact written as a test so its failure names the reason:
	// jobs.VerifiedJob is an interface with an unexported method and no
	// exported implementation, so Ship is unreachable except through the
	// signed-job runner. That matters more here than for most capabilities: the
	// thing on the other side of this function is the Security event log.
	var _ jobs.Handler = Process.Ship
}

// ---------------------------------------------------------------------------
// The cursor: resumed, not restarted
// ---------------------------------------------------------------------------

func TestASecondJobResumesAtTheCursorAndDoesNotStartOver(t *testing.T) {
	// THE TEST THIS PACKAGE EXISTS TO GET RIGHT, and the one a bland fixture
	// cannot express. With a stream where every record is identical, "resumed"
	// and "started over" produce the same 500 events and the same counts. Here,
	// every record has its own id, timestamp and username -- so the assertion is
	// that the FIRST event of the second job is a NAMED, DIFFERENT record from
	// the first event of the first job, and that no event appears twice.
	//
	// FALSIFYING OBSERVATION: job 2's first event_uid equalling job 1's, or any
	// event_uid appearing in both.
	log := newFakeLog(1200)
	store := &memCursor{}
	opts := Options{Now: fixedNow, Logger: quiet()}

	first := &spySender{}
	opts.Sender = first
	_ = shipOnce(t, New(Options{}), opts, log, store)

	second := &spySender{}
	opts.Sender = second
	r2 := shipOnce(t, New(Options{}), opts, log, store)

	e1, e2 := first.events(), second.events()
	if len(e1) == 0 {
		t.Fatal("the first job shipped nothing; this test would be vacuous")
	}
	if len(e2) != 0 {
		t.Fatalf("the second job shipped %d events from a log that had not grown. The first "+
			"event it sent was %q (%s) -- the first job had already shipped up to %q",
			len(e2), e2[0].EventUID, e2[0].Username, e1[len(e1)-1].EventUID)
	}
	if r2.Shipped != 0 || r2.Batches != 0 {
		t.Fatalf("second job: shipped=%d batches=%d over a log that had not grown",
			r2.Shipped, r2.Batches)
	}

	// Now the log grows. The second-pass events must be the NEW ones, named.
	grown := newFakeLog(1260)
	third := &spySender{}
	opts.Sender = third
	r3 := shipOnce(t, New(Options{}), opts, grown, store)
	e3 := third.events()
	if len(e3) == 0 {
		t.Fatalf("nothing was shipped after 60 new records arrived (result %+v)", r3)
	}
	seen := map[string]bool{}
	for _, ev := range e1 {
		seen[ev.EventUID] = true
	}
	for _, ev := range e3 {
		if seen[ev.EventUID] {
			t.Fatalf("event %q (%s, %s) was shipped by BOTH jobs; the cursor did not advance",
				ev.EventUID, ev.Username, ev.TS)
		}
	}
	// And the specific record: the first new event must belong to record 1200 of
	// the synthetic log, whose username is "user1200" -- not "user0000".
	if e3[0].Username == e1[0].Username {
		t.Fatalf("the second pass began at %q, which is where the FIRST pass began; the cursor "+
			"was ignored", e3[0].Username)
	}
	wantUser := grown.records[1200].user
	if e3[0].Username != `TOESHEE\`+wantUser && e3[0].Username != wantUser {
		t.Fatalf("the second pass began at %q; the record after the cursor belongs to %q",
			e3[0].Username, wantUser)
	}
}

func TestACursorAdvancesPastRecordsTheTaxonomyDrops(t *testing.T) {
	// A tail of records outside the taxonomy must not be re-read on every job
	// forever. The synthetic log ends on a 4719 followed by nothing, so the
	// last records of a pass include dropped ones; the cursor has to move past
	// them even though no batch carried them.
	//
	// FALSIFYING OBSERVATION: a second pass over an unchanged log re-reading
	// records -- observable as `opens` rising with `Skipped` rising too.
	// 38 RECORDS, AND THE NUMBER IS CHOSEN. The rota's dropped ids sit at
	// indices 1, 4 and 7 of every ten, so a log of 38 records ENDS ON A DROPPED
	// ONE (index 37 -> 5379). That is the whole point: with 37 records the last
	// record is collected, the cursor lands on it for the trivial reason that it
	// was shipped, and this test passes against an implementation that never
	// advances past a drop at all. Measured, not reasoned about -- a mutation
	// that moved `pending` inside the `if ok` branch left the 37-record version
	// GREEN.
	log := newFakeLog(38)
	store := &memCursor{}
	opts := Options{Sender: &countingSender{}, Now: fixedNow, Logger: quiet()}

	r1 := shipOnce(t, New(Options{}), opts, log, store)
	if r1.Skipped == 0 {
		t.Fatal("no records were dropped by the taxonomy filter; the rota must include ids " +
			"outside it or this test is vacuous")
	}
	afterFirst := store.cur.Position

	r2 := shipOnce(t, New(Options{}), opts, log, store)
	if r2.Skipped != 0 || r2.Shipped != 0 {
		t.Fatalf("a second pass over an unchanged log re-read %d records (%d shipped, %d "+
			"skipped); the cursor stopped at %q instead of the end",
			r2.Shipped+r2.Skipped, r2.Shipped, r2.Skipped, afterFirst)
	}
	// The cursor must be at the LAST record of the log, not at the last
	// SHIPPED one.
	want := strconv.Itoa(log.records[len(log.records)-1].id)
	if store.cur.Position != want {
		t.Fatalf("the cursor is at %q; the last record in the log is %q, and the records between "+
			"them were dropped by the taxonomy filter and would be re-read forever",
			store.cur.Position, want)
	}
}

func TestACursorFromAnotherSourceIsDiscardedRatherThanMisread(t *testing.T) {
	// A Windows EventRecordID handed to the macOS source as a `--start`
	// timestamp collects NOTHING and reports success: a device that has quietly
	// stopped producing evidence, with a green job history. This happens on a
	// re-imaged machine whose data directory survived, or a cross-platform
	// restore.
	//
	// FALSIFYING OBSERVATION: the stored position being used, observable as zero
	// events from a log that has plenty.
	log := newFakeLog(40)
	store := &memCursor{cur: Cursor{V: cursorVersion, Source: "macos-unified-log", Position: "999999"}}
	sender := &countingSender{}
	res := shipOnce(t, New(Options{}), Options{Sender: sender, Now: fixedNow, Logger: quiet()}, log, store)

	if res.Shipped == 0 {
		t.Fatalf("nothing shipped: the foreign cursor %q was used as a position for %s",
			"999999", log.Name())
	}
	if !res.CursorReset {
		t.Error("cursor_reset was not reported, so an operator cannot see why this device " +
			"re-shipped its history")
	}
	if store.cur.Source != log.Name() {
		t.Errorf("the saved cursor still claims source %q", store.cur.Source)
	}
}

func TestAnUnreadableCursorRestartsCollectionRatherThanStoppingIt(t *testing.T) {
	// This is the evidence path. An endpoint that goes silent because a
	// 200-byte state file was truncated by a power cut is precisely the failure
	// the SOC 2 control exists to prevent. Restarting costs a bounded re-read
	// that the server dedupes away.
	//
	// FALSIFYING OBSERVATION: ship returning an error, or shipping nothing.
	log := newFakeLog(40)
	store := &memCursor{loadEr: errors.New("unexpected end of JSON input")}
	sender := &countingSender{}
	res := shipOnce(t, New(Options{}), Options{Sender: sender, Now: fixedNow, Logger: quiet()}, log, store)
	if res.Shipped == 0 {
		t.Fatal("a corrupt cursor stopped collection")
	}
	if !res.CursorReset {
		t.Error("cursor_reset was not reported")
	}
}

func TestAClearedSecurityLogIsDetectedInsteadOfSilentlyEndingCollection(t *testing.T) {
	// `wevtutil cl Security` resets EventRecordID to 1. A cursor left at 184,392
	// then matches nothing FOREVER: the device reports zero events, the console
	// shows a machine that has gone quiet, and the evidence for it ends on the
	// day the log was cleared -- which is the day that matters.
	//
	// FALSIFYING OBSERVATION: zero events shipped from a log that is full,
	// after a rewind.
	log := newFakeLog(40)
	log.rewound = true
	// A cursor far beyond anything in the (cleared and refilled) log.
	store := &memCursor{cur: Cursor{V: cursorVersion, Source: log.Name(), Position: "999999"}}
	sender := &countingSender{}
	res := shipOnce(t, New(Options{}), Options{Sender: sender, Now: fixedNow, Logger: quiet()}, log, store)

	if res.Shipped == 0 {
		t.Fatal("the log had been cleared and refilled, and this agent shipped nothing. It would " +
			"go on shipping nothing forever")
	}
	if !res.CursorReset {
		t.Error("cursor_reset was not reported; an operator has no way to see that a device's " +
			"security log was cleared")
	}
	if store.cur.Position == "999999" {
		t.Error("the cursor was not reset")
	}
}

func TestAQuietMachineIsNotMistakenForAClearedLog(t *testing.T) {
	// The other side of the previous test, and the reason Rewound is asked
	// rather than assumed. A machine where nobody logged in since the last job
	// ships nothing -- and must NOT have its cursor reset, or it re-ships its
	// whole history every time it is quiet.
	//
	// FALSIFYING OBSERVATION: cursor_reset=true, or the cursor moving.
	log := newFakeLog(40)
	log.rewound = false
	store := &memCursor{}
	opts := Options{Sender: &countingSender{}, Now: fixedNow, Logger: quiet()}
	shipOnce(t, New(Options{}), opts, log, store)
	at := store.cur.Position

	res := shipOnce(t, New(Options{}), opts, log, store)
	if res.CursorReset {
		t.Fatal("a quiet machine was treated as a cleared log; it would re-ship its entire " +
			"history on every job it had nothing to say")
	}
	if store.cur.Position != at {
		t.Fatalf("the cursor moved from %q to %q on a job that shipped nothing", at, store.cur.Position)
	}
}

// ---------------------------------------------------------------------------
// Bounds
// ---------------------------------------------------------------------------

func TestNoBatchExceedsTheFiveHundredTheServerAcceptsAtParse(t *testing.T) {
	// SecurityEventsMsg.events has maxItems 500 and the server rejects a longer
	// message AT PARSE -- so a 501-event batch does not lose one event, it loses
	// all 501.
	//
	// FALSIFYING OBSERVATION: a batch of any other size than <=500, reported
	// with its index and length.
	log := newFakeLog(2600)
	sender := &spySender{}
	shipOnce(t, New(Options{}), Options{Sender: sender, Now: fixedNow, Logger: quiet()},
		log, &memCursor{})
	if len(sender.batches) < 2 {
		t.Fatalf("only %d batch(es); this test needs the flush path", len(sender.batches))
	}
	for i, b := range sender.batches {
		if len(b) > maxBatch {
			t.Errorf("batch %d carries %d events; the server rejects the whole message above %d",
				i, len(b), maxBatch)
		}
	}
}

func TestOneJobShipsAtMostMaxEventsPerJobAndSaysItTruncated(t *testing.T) {
	// An endpoint with a year of backlog must not try to send it all at once:
	// a multi-hundred-megabyte read, ~2,000 frames into an outbox with a byte
	// cap that would then start DROPPING them, and a job that outlives its own
	// expires_at.
	//
	// FALSIFYING OBSERVATION: more than maxEventsPerJob events shipped, or
	// truncated=false when the log had more to give.
	log := newFakeLog(3 * maxEventsPerJob)
	sender := &countingSender{}
	store := &memCursor{}
	res := shipOnce(t, New(Options{}), Options{Sender: sender, Now: fixedNow, Logger: quiet()},
		log, store)

	if !res.Truncated {
		t.Fatalf("truncated=false with %d records still unread", 3*maxEventsPerJob-res.Shipped-res.Skipped)
	}
	if res.Shipped+res.Skipped > maxEventsPerJob {
		t.Fatalf("one job consumed %d records (limit %d)", res.Shipped+res.Skipped, maxEventsPerJob)
	}
	if sender.events != res.Shipped {
		t.Fatalf("the result claims %d shipped and the sender saw %d", res.Shipped, sender.events)
	}
	// And the next job continues rather than restarting.
	next := &countingSender{}
	res2 := shipOnce(t, New(Options{}), Options{Sender: next, Now: fixedNow, Logger: quiet()},
		log, store)
	if res2.Shipped == 0 {
		t.Fatal("the catch-up did not continue")
	}
	if next.first.EventUID == sender.first.EventUID {
		t.Fatalf("the catch-up restarted at %q instead of continuing", next.first.EventUID)
	}
}

func TestOverLongValuesAreClampedRatherThanFailingFiveHundredGoodEvents(t *testing.T) {
	// A username over 200 characters, a source over 100, or a uid over 64 fails
	// PYDANTIC VALIDATION FOR THE WHOLE MESSAGE. Clamping one field is strictly
	// better than losing 499 unrelated records of evidence.
	//
	// FALSIFYING OBSERVATION: a field longer than the server's own limit.
	ev := clampEvent(wire.SecurityEvent{
		EventUID:  strings.Repeat("u", 200),
		Username:  `TOESHEE\` + strings.Repeat("n", 500),
		Source:    strings.Repeat("s", 300),
		EventType: "logon_success",
		Raw:       map[string]any{"blob": strings.Repeat("x", 40000), "LogonType": "3"},
	})
	if len(ev.EventUID) > maxEventUIDLen {
		t.Errorf("event_uid is %d chars (limit %d)", len(ev.EventUID), maxEventUIDLen)
	}
	if len(ev.Username) > maxUsernameLen {
		t.Errorf("username is %d chars (limit %d)", len(ev.Username), maxUsernameLen)
	}
	if len(ev.Source) > maxSourceLen {
		t.Errorf("source is %d chars (limit %d)", len(ev.Source), maxSourceLen)
	}
	if ev.Raw[rawTruncatedKey] != true {
		t.Error("raw was clamped and does not say so; a silently truncated evidence record is " +
			"worse than a visibly truncated one")
	}
	if ev.Raw["LogonType"] != "3" {
		t.Errorf("clamping a huge value discarded a small one: LogonType = %v", ev.Raw["LogonType"])
	}
	total := 0
	for k, v := range ev.Raw {
		if s, ok := v.(string); ok {
			total += len(k) + len(s)
		}
	}
	if total > maxRawBytes {
		t.Errorf("raw is %d bytes after clamping (limit %d)", total, maxRawBytes)
	}
}

// ---------------------------------------------------------------------------
// Failure classification, and what a failure does to the cursor
// ---------------------------------------------------------------------------

func TestAFailedUploadLeavesTheCursorWhereItWasSoTheEventsAreReshipped(t *testing.T) {
	// Delivery is at-least-once and the server dedupes on
	// (device_id, event_uid). Advancing the cursor over a batch the sender
	// rejected would turn that into at-MOST-once and lose the events for good.
	//
	// FALSIFYING OBSERVATION: the cursor at a position past the failed batch,
	// or the events not reappearing on the retry.
	log := newFakeLog(1200)
	store := &memCursor{}
	failing := &spySender{failOn: 2}
	s := New(Options{})
	_, err := s.ship(context.Background(),
		Options{Sender: failing, Now: fixedNow, Logger: quiet()}, log, store)
	if err == nil {
		t.Fatal("a failed upload was reported as success")
	}
	var refusal *jobs.Refusal
	if errors.As(err, &refusal) {
		t.Fatalf("an upload failure was REFUSED (%q). `refused` takes a device out of rotation "+
			"for some modules; an uplink that went away is the textbook transient", refusal.Reason)
	}
	afterFail := store.cur.Position

	// The retry must re-ship the batch that failed.
	good := &spySender{}
	shipOnce(t, s, Options{Sender: good, Now: fixedNow, Logger: quiet()}, log, store)
	firstBatchOfFailedRun := failing.batches[0]
	reshipped := map[string]bool{}
	for _, ev := range good.events() {
		reshipped[ev.EventUID] = true
	}
	// The events of the FAILED batch were never handed to the spy, so what is
	// checked is that collection resumed from the last SUCCESSFUL flush.
	if len(firstBatchOfFailedRun) == 0 {
		t.Fatal("the first batch never went; this test would be vacuous")
	}
	lastGood := firstBatchOfFailedRun[len(firstBatchOfFailedRun)-1]
	if reshipped[lastGood.EventUID] {
		t.Errorf("the retry re-shipped %q, which had already been accepted; the cursor did not "+
			"advance over a SUCCESSFUL batch", lastGood.EventUID)
	}
	if afterFail == "" {
		t.Error("the cursor never advanced at all, so even the accepted batch would be re-sent")
	}
	if len(good.events()) == 0 {
		t.Error("the retry shipped nothing; the events in the failed batch are lost")
	}
}

func TestAStreamThatBreaksMidwayStillShipsWhatWasAlreadyRead(t *testing.T) {
	// Evidence already read is owed to the server whatever went wrong after it.
	// Dropping a normalised batch on the floor because the NEXT read failed
	// would lose records that were successfully collected.
	//
	// FALSIFYING OBSERVATION: zero batches sent when the failure came after
	// several hundred good records.
	log := newFakeLog(2000)
	log.failAt = 700
	sender := &spySender{}
	s := New(Options{})
	_, err := s.ship(context.Background(),
		Options{Sender: sender, Now: fixedNow, Logger: quiet()}, log, &memCursor{})
	if err == nil {
		t.Fatal("a broken stream was reported as success")
	}
	if len(sender.batches) == 0 {
		t.Fatal("the stream broke after 700 records and none of them were shipped")
	}
	if !strings.Contains(err.Error(), "fake-eventlog") {
		t.Errorf("the error %q does not name the source that failed", err)
	}
}

func TestAPlatformWithNoSecurityLogIsRefusedAndNotFailed(t *testing.T) {
	// "No Windows Event Log here" is permanently true, so `refused` is the
	// honest answer -- and it is the one that lets an operator see WHY a box
	// contributes no evidence instead of watching a job fail on a timer forever.
	//
	// FALSIFYING OBSERVATION: on linux, a plain error rather than a
	// jobs.Refusal. (On windows/darwin platformSource succeeds and there is
	// nothing to test.)
	src, err := platformSource()
	if err == nil {
		t.Skipf("this platform has a source (%s); the refusal path is compiled elsewhere", src.Name())
	}
	s := New(Options{Sender: &countingSender{}, Logger: quiet()})
	_, _, _, jerr := s.resolve()
	var refusal *jobs.Refusal
	if !errors.As(jerr, &refusal) {
		t.Fatalf("err = %v (%T), want a jobs.Refusal", jerr, jerr)
	}
	if !strings.Contains(refusal.Reason, "No security log was read") &&
		!strings.Contains(refusal.Reason, "no security log was read") {
		t.Errorf("the refusal %q does not say that nothing was read", refusal.Reason)
	}
}

func TestAJobArrivingBeforeBindFailsRatherThanReadingTheSecurityLog(t *testing.T) {
	// An agent that read the Security event log and then discarded it would be
	// paying the loudest EDR cost this capability has for no benefit at all.
	//
	// FALSIFYING OBSERVATION: any call to the source when no sender is bound.
	log := newFakeLog(10)
	s := &Service{}
	s.Bind(Options{Source: log, Logger: quiet()}) // no Sender
	_, _, _, err := s.resolve()
	if !errors.Is(err, ErrUnbound) {
		t.Fatalf("err = %v, want ErrUnbound", err)
	}
	if log.opens != 0 {
		t.Fatalf("the security log was opened %d times with nowhere to send what it contains",
			log.opens)
	}
}

// ---------------------------------------------------------------------------
// Personal data does not reach a log line or the job result
// ---------------------------------------------------------------------------

func TestNoUsernameOrAddressReachesAnAgentLogLineAtAnyLevel(t *testing.T) {
	// `logs.ship` carries usernames, source addresses and logon types. Those are
	// personal data on their way to a retention-governed, access-controlled
	// table; the agent's own log file is neither, is collected wholesale by
	// support bundles, and on Windows is readable by anything that can read the
	// ProgramData directory.
	//
	// FALSIFYING OBSERVATION: a username or an IP address from the stream
	// appearing anywhere in a Debug-level capture.
	var buf bytes.Buffer
	log := slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}))
	stream := newFakeLog(1200)
	// Make one record's failure mode interesting: a broken document, so the
	// error path logs too.
	sender := &countingSender{}
	res := shipOnce(t, New(Options{}),
		Options{Sender: sender, Now: fixedNow, Logger: log}, stream, &memCursor{})

	out := buf.String()
	if out == "" {
		t.Fatal("nothing was logged at all; this test would pass on a capability that never runs")
	}
	for _, needle := range []string{"user0000", "user0500", "10.20.7.", "TOESHEE\\", "4624",
		"WS-0142", "logon_success"} {
		if strings.Contains(out, needle) {
			t.Errorf("%q reached the agent log:\n%s", needle, out)
		}
	}
	// The counts, which are what an operator actually needs, ARE there.
	if !strings.Contains(out, strconv.Itoa(res.Shipped)) {
		t.Errorf("the shipped count is not in the log line, so an operator has neither the "+
			"content (correctly) nor the counts (incorrectly):\n%s", out)
	}
}

func TestTheJobResultCarriesCountsAndNoEventContent(t *testing.T) {
	// `logs.ship` has NO result handler server-side and no exported result
	// schema, so nothing validates this and nothing acts on it: it is stored on
	// the job row and read by a human. A job row is not the retention-governed,
	// access-controlled place security events belong.
	//
	// FALSIFYING OBSERVATION: any username, address or event type in the
	// marshalled result.
	stream := newFakeLog(600)
	res := shipOnce(t, New(Options{}),
		Options{Sender: &countingSender{}, Now: fixedNow, Logger: quiet()}, stream, &memCursor{})
	raw, err := json.Marshal(res)
	if err != nil {
		t.Fatal(err)
	}
	for _, needle := range []string{"user", "10.20.7", "TOESHEE", "logon", "4624", "WS-0142"} {
		if strings.Contains(string(raw), needle) {
			t.Fatalf("the job result carries %q: %s", needle, raw)
		}
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"source", "shipped", "skipped", "batches", "truncated", "cursor_reset"} {
		if _, ok := got[key]; !ok {
			t.Errorf("the result has no %q key: %s", key, raw)
		}
	}
}

// ---------------------------------------------------------------------------
// Params
// ---------------------------------------------------------------------------

func TestNoParamsMemberCanWidenWhatIsCollected(t *testing.T) {
	// LogsShipParams is EMPTY and extra="forbid": "dispatching logs.ship tells
	// the agent to flush its buffer now; the scheduled cadence comes from the
	// org's policy". A params member that named a channel, a filter or a path
	// would be a second, unversioned way to configure what this capability
	// reads -- and would make `logs.ship` a general "read me this log file"
	// primitive on a domain-admin-equivalent agent.
	//
	// FALSIFYING OBSERVATION: this package reading any params member. It is
	// asserted structurally: checkNoParams is the ONLY function given
	// job.Params(), and it returns no value.
	for _, raw := range []string{`{}`, ``, `{"channel":"Application"}`, `{"path":"C:\\secrets"}`} {
		if err := checkNoParams(json.RawMessage(raw)); err != nil {
			t.Errorf("checkNoParams(%s) = %v; unknown members are IGNORED, not refused, so a "+
				"strictly additive server change does not take a deployed fleet offline", raw, err)
		}
	}
	if err := checkNoParams(json.RawMessage(`["Security"]`)); err == nil {
		t.Error("params that are not an object were accepted")
	}
}

// NOTE ON WHAT IS DRIVEN DIRECTLY, AND WHY. jobs.VerifiedJob is an interface
// with an unexported method and no exported implementation, so no test outside
// internal/jobs can construct one -- that is invariant #1 made structural, and
// it is deliberate. So these tests drive ship() and resolve() directly, exactly
// as the site and netsweep capability tests drive their run(). The wiring
// between Ship and those two is one compile-time assertion
// (TestTheCapabilityTakesAVerifiedJobAndNothingElse) plus
// internal/capabilities/integration_test.go, which builds REAL verified jobs
// and is where the end-to-end path is checked.
