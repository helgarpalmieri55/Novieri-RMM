package jobs

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// fakeFrameSender is the gateway connection as the buffer sees it: something
// that takes an already-framed message and can start failing part-way through.
type fakeFrameSender struct {
	mu       sync.Mutex
	got      [][]byte
	failFrom int // fail every send from this index on; -1 never fails
}

func newFrameSender() *fakeFrameSender { return &fakeFrameSender{failFrom: -1} }

func (f *fakeFrameSender) SendFrame(_ context.Context, b []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failFrom >= 0 && len(f.got) >= f.failFrom {
		return errors.New("connection lost")
	}
	f.got = append(f.got, append([]byte(nil), b...))
	return nil
}

func (f *fakeFrameSender) jobIDs(t *testing.T) []string {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for _, b := range f.got {
		var m map[string]any
		if err := json.Unmarshal(b, &m); err != nil {
			t.Fatalf("buffer emitted a frame that is not JSON: %s", b)
		}
		out = append(out, m["job_id"].(string)+"/"+m["kind"].(string))
	}
	return out
}

func newBuffer(t *testing.T, maxBytes int64, logs *logBuf) (*Buffer, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "outbound.log")
	if logs == nil {
		logs = &logBuf{}
	}
	return OpenBuffer(path, maxBytes, slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{
		Level: slog.LevelDebug,
	}))), path
}

func TestTheBufferPreservesOrderAcrossADisconnectAndARestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "outbound.log")
	log := slog.New(slog.NewTextHandler(&logBuf{}, nil))

	b := OpenBuffer(path, DefaultBufferBytes, log)
	for _, id := range []string{"1", "2", "3"} {
		if err := b.Enqueue(wire.JobResult{JobID: id, State: wire.StateSucceeded}); err != nil {
			t.Fatal(err)
		}
	}
	if b.Len() != 3 {
		t.Fatalf("buffer holds %d, want 3", b.Len())
	}

	// The process restarts before anything is flushed.
	reopened := OpenBuffer(path, DefaultBufferBytes, log)
	conn := newFrameSender()
	if err := reopened.Flush(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	want := []string{"1/job_result", "2/job_result", "3/job_result"}
	if got := conn.jobIDs(t); !equal(got, want) {
		t.Fatalf("flushed %v, want %v -- a job's answer arriving out of order is a job the "+
			"operator sees resolve twice", got, want)
	}
	if reopened.Len() != 0 {
		t.Fatalf("%d entries left after a complete flush", reopened.Len())
	}
	if _, err := os.Stat(path); err == nil {
		if data, _ := os.ReadFile(path); len(strings.TrimSpace(string(data))) != 0 {
			t.Fatalf("the backing file still holds %q after a complete flush", data)
		}
	}
}

func TestAnOverflowingBufferDropsCommentaryBeforeAnswers(t *testing.T) {
	// A job_result is the ANSWER to a signed job: dropping it leaves the
	// control plane waiting until the job expires and tells the operator
	// nothing. A job_log is commentary. The drop rule is therefore by priority
	// first, oldest-within-priority second -- not oldest overall.
	//
	// FALSIFYING OBSERVATION: the surviving job_ids. With a plain
	// oldest-first rule the result enqueued last would survive too, so the test
	// enqueues the result FIRST and then floods with logs.
	logs := &logBuf{}
	b, _ := newBuffer(t, 2048, logs)

	if err := b.Enqueue(wire.JobResult{JobID: "answer", State: wire.StateSucceeded}); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 200; i++ {
		if err := b.Enqueue(wire.JobLog{JobID: "chatter", Msg: strings.Repeat("x", 64)}); err != nil {
			t.Fatal(err)
		}
	}
	if b.Bytes() > 2048 {
		t.Fatalf("buffer holds %d bytes, cap is 2048", b.Bytes())
	}

	conn := newFrameSender()
	if err := b.Flush(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	var sawAnswer bool
	for _, id := range conn.jobIDs(t) {
		if id == "answer/job_result" {
			sawAnswer = true
		}
	}
	if !sawAnswer {
		t.Fatalf("the job_result was dropped in favour of job_log commentary; flushed %v",
			conn.jobIDs(t))
	}
	if !strings.Contains(logs.String(), "dropped") {
		t.Fatalf("frames were dropped with no trace in the log:\n%s", logs.String())
	}
}

func TestWhenOnlyAnswersRemainTheOldestAnswerIsTheOneDropped(t *testing.T) {
	b, _ := newBuffer(t, 1024, nil)
	for i := 0; i < 100; i++ {
		if err := b.Enqueue(wire.JobResult{
			JobID:  string(rune('a' + i%26)),
			State:  wire.StateSucceeded,
			Result: map[string]any{"n": i, "pad": strings.Repeat("y", 32)},
		}); err != nil {
			t.Fatal(err)
		}
	}
	if b.Bytes() > 1024 {
		t.Fatalf("buffer holds %d bytes over a 1024 cap", b.Bytes())
	}
	if b.Len() == 0 {
		t.Fatal("the buffer dropped everything; a cap that admits nothing is not a cap")
	}
	conn := newFrameSender()
	if err := b.Flush(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	// The survivors must be the NEWEST, in order.
	ids := conn.jobIDs(t)
	if len(ids) != b.flushed {
		t.Fatalf("flushed %d frames but the buffer counted %d", len(ids), b.flushed)
	}
}

func TestAMidFlushDisconnectLeavesTheUnsentEntriesInOrder(t *testing.T) {
	// At-least-once, and it is safe. VERIFIED AGAINST app/jobs/engine.py, not
	// recalled: _ALLOWED[JobStatus.succeeded] == set() (engine.py:50), so a
	// duplicate job_result raises InvalidTransition inside _transition
	// (engine.py:119-120); gateway/main.py:212's `except Exception` around
	// handle_agent_message logs it and keeps the connection. A re-sent result
	// therefore cannot corrupt a terminal job, and health_report /
	// security_events are documented at-least-once and deduped server-side.
	b, path := newBuffer(t, DefaultBufferBytes, nil)
	for _, id := range []string{"1", "2", "3", "4"} {
		if err := b.Enqueue(wire.JobResult{JobID: id, State: wire.StateSucceeded}); err != nil {
			t.Fatal(err)
		}
	}
	conn := newFrameSender()
	conn.failFrom = 2 // the third send fails

	if err := b.Flush(context.Background(), conn); err == nil {
		t.Fatal("Flush hid a connection failure")
	}
	if got, want := conn.jobIDs(t), []string{"1/job_result", "2/job_result"}; !equal(got, want) {
		t.Fatalf("sent %v, want %v", got, want)
	}
	if b.Len() != 2 {
		t.Fatalf("%d entries remain, want 2 (3 and 4)", b.Len())
	}

	// The remainder is durable: the loss window is a crash between the send and
	// the rewrite, and re-sending is the safe side of that window.
	reopened := OpenBuffer(path, DefaultBufferBytes, slog.New(slog.NewTextHandler(&logBuf{}, nil)))
	conn2 := newFrameSender()
	if err := reopened.Flush(context.Background(), conn2); err != nil {
		t.Fatal(err)
	}
	if got, want := conn2.jobIDs(t), []string{"3/job_result", "4/job_result"}; !equal(got, want) {
		t.Fatalf("after reconnect sent %v, want %v", got, want)
	}
}

func TestTheBufferRefusesAnythingThatIsNotADeclaredOutboundMessage(t *testing.T) {
	// wire.Encode closes the set of kinds. Enqueue goes through it so a caller
	// cannot persist a frame the server would drop at parse_agent_message --
	// which would be an entry that can never be flushed and never expires.
	b, _ := newBuffer(t, DefaultBufferBytes, nil)
	if err := b.Enqueue(struct{ X int }{1}); err == nil {
		t.Fatal("the buffer accepted a message that is not an outbound kind")
	}
	if b.Len() != 0 {
		t.Fatal("the rejected message consumed a slot")
	}
}

func TestAHeartbeatIsNotBuffered(t *testing.T) {
	// A heartbeat means "I am here NOW". Replaying a stale one after a
	// reconnect asserts a liveness the reconnect itself already asserts, and
	// _touch_agent writes the SERVER's clock anyway (dispatch.py), so the
	// replayed frame carries no information at all. Buffering it would spend
	// the bounded budget on the one message with no value when late.
	b, _ := newBuffer(t, DefaultBufferBytes, nil)
	err := b.Enqueue(wire.Heartbeat{TS: "2026-08-05T12:00:00Z"})
	if !errors.Is(err, ErrNotBuffered) {
		t.Fatalf("Enqueue(heartbeat) returned %v, want ErrNotBuffered", err)
	}
	if b.Len() != 0 {
		t.Fatalf("a heartbeat consumed a buffer slot")
	}
}

func TestACorruptTrailingLineIsDiscardedNotFatal(t *testing.T) {
	// Enqueue appends. A crash mid-append leaves a half-written last line, and
	// an agent that refuses to start because of it is an endpoint that never
	// comes back.
	path := filepath.Join(t.TempDir(), "outbound.log")
	logs := &logBuf{}
	log := slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	b := OpenBuffer(path, DefaultBufferBytes, log)
	if err := b.Enqueue(wire.JobResult{JobID: "good", State: wire.StateSucceeded}); err != nil {
		t.Fatal(err)
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString(`{"p":2,"f":{"v":1,"kind":"job_res`); err != nil {
		t.Fatal(err)
	}
	f.Close()

	reopened := OpenBuffer(path, DefaultBufferBytes, log)
	if reopened.Len() != 1 {
		t.Fatalf("reopened buffer holds %d entries, want 1", reopened.Len())
	}
	if !strings.Contains(logs.String(), "discarded") {
		t.Fatalf("the discarded line left no trace:\n%s", logs.String())
	}
}

// ---------------------------------------------------------------------------
// The Outbox: what the runner actually sends through
// ---------------------------------------------------------------------------

func TestTheOutboxBuffersWhileDisconnectedAndFlushesInOrderOnConnect(t *testing.T) {
	b, _ := newBuffer(t, DefaultBufferBytes, nil)
	out := NewOutbox(b, slog.New(slog.NewTextHandler(&logBuf{}, nil)))
	ctx := context.Background()

	for _, id := range []string{"1", "2"} {
		if err := out.Send(ctx, wire.JobResult{JobID: id, State: wire.StateSucceeded}); err != nil {
			t.Fatalf("Send while disconnected returned %v; it must buffer, not fail", err)
		}
	}
	conn := newFrameSender()
	if err := out.Attach(ctx, conn); err != nil {
		t.Fatal(err)
	}
	if err := out.Send(ctx, wire.JobResult{JobID: "3", State: wire.StateSucceeded}); err != nil {
		t.Fatal(err)
	}
	want := []string{"1/job_result", "2/job_result", "3/job_result"}
	if got := conn.jobIDs(t); !equal(got, want) {
		t.Fatalf("gateway saw %v, want %v", got, want)
	}
}

func TestTheOutboxKeepsAMessageWhenTheConnectionFailsUnderIt(t *testing.T) {
	b, _ := newBuffer(t, DefaultBufferBytes, nil)
	out := NewOutbox(b, slog.New(slog.NewTextHandler(&logBuf{}, nil)))
	ctx := context.Background()
	conn := newFrameSender()
	conn.failFrom = 0
	if err := out.Attach(ctx, conn); err != nil {
		t.Fatal(err)
	}

	if err := out.Send(ctx, wire.JobResult{JobID: "kept", State: wire.StateSucceeded}); err != nil {
		t.Fatalf("Send returned %v; a send that failed on the wire must be retained, not surfaced", err)
	}
	if b.Len() != 1 {
		t.Fatalf("buffer holds %d, want the retained result", b.Len())
	}
	out.Detach()

	good := newFrameSender()
	if err := out.Attach(ctx, good); err != nil {
		t.Fatal(err)
	}
	if got := good.jobIDs(t); !equal(got, []string{"kept/job_result"}) {
		t.Fatalf("after reconnect the gateway saw %v", got)
	}
}

func TestTheOutboxSatisfiesTheRunnersSenderInterface(t *testing.T) {
	// Compile-time: the runner must be able to send through the buffer without
	// knowing a buffer exists.
	b, _ := newBuffer(t, DefaultBufferBytes, nil)
	var _ Sender = NewOutbox(b, nil)
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// ---------------------------------------------------------------------------
// Concurrency: N senders and N flushers, and not one answer lost
// ---------------------------------------------------------------------------

// TestConcurrentSendersAndFlushesNeverLoseAnEntry is the test buffer_test.go
// did not have, for a property this branch made load-bearing.
//
// On a stock agent before the Dispatcher, Outbox.Send ran effectively
// single-threaded off the transport's one read goroutine, so Flush had one
// caller and the positional removal it did was safe by accident. Capability
// workers now call Send concurrently in the DEFAULT configuration, and every
// job sends at least twice (`running`, then its result) with a flush on each.
//
// The defect that made this necessary: Flush snapshotted under the lock,
// released it to send, then removed the first `sent` entries BY POSITION.
// Interleave two flushes and an enqueue and the removal deletes an entry that
// was never sent -- a job_result, the answer to a signed job, gone from the
// buffer whose entire purpose is not to lose it, and counted in `flushed`.
//
// The falsifier is exact and does not depend on timing luck: the connection
// records every frame it was handed, so any enqueued job id that is neither
// still buffered nor recorded as sent was dropped. Duplicates are ACCEPTED --
// the package documents at-least-once and the server rejects a repeated
// job_result with a log line. Losing one is what this forbids.
//
// Run under -race, where it also covers the removal path itself.
func TestConcurrentSendersAndFlushesNeverLoseAnEntry(t *testing.T) {
	const senders, perSender, flushers = 8, 40, 4

	b, _ := newBuffer(t, DefaultBufferBytes, nil)
	conn := newFrameSender()

	// Two WaitGroups on purpose: the flushers only stop when told, so waiting
	// on one group for both would wait for goroutines that are waiting to be
	// released by the wait itself.
	var sending, flushing sync.WaitGroup
	stop := make(chan struct{})

	for f := 0; f < flushers; f++ {
		flushing.Add(1)
		go func() {
			defer flushing.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				_ = b.Flush(context.Background(), conn)
				runtime.Gosched()
			}
		}()
	}

	for s := 0; s < senders; s++ {
		sending.Add(1)
		go func(s int) {
			defer sending.Done()
			for i := 0; i < perSender; i++ {
				id := fmt.Sprintf("%d-%d", s, i)
				if err := b.Enqueue(wire.JobResult{JobID: id, State: wire.StateSucceeded}); err != nil {
					t.Errorf("Enqueue(%s): %v", id, err)
					return
				}
			}
		}(s)
	}

	sending.Wait()
	close(stop)
	flushing.Wait()

	// One final flush, single-threaded, so anything left is left for a reason
	// other than nobody having tried.
	if err := b.Flush(context.Background(), conn); err != nil {
		t.Fatalf("final flush: %v", err)
	}

	sentIDs := map[string]int{}
	for _, entry := range conn.jobIDs(t) {
		sentIDs[strings.TrimSuffix(entry, "/job_result")]++
	}
	if n := b.Len(); n != 0 {
		t.Fatalf("%d entries still buffered after a final, uncontended flush", n)
	}

	var lost []string
	for s := 0; s < senders; s++ {
		for i := 0; i < perSender; i++ {
			id := fmt.Sprintf("%d-%d", s, i)
			if sentIDs[id] == 0 {
				lost = append(lost, id)
			}
		}
	}
	if len(lost) > 0 {
		t.Fatalf("%d of %d enqueued job_results were never sent and are no longer buffered: %v -- "+
			"a duplicate is documented and harmless, a lost answer is the failure this buffer exists "+
			"to prevent", len(lost), senders*perSender, lost)
	}
}

// TestAnEntryEnqueuedDuringAFlushSurvivesTheCapDroppingAnother is the second
// half of the removal bug, and it needs no concurrency at all to state: it is
// about WHICH entry a flush removes when the cap moved things underneath it.
//
// enforceCapLocked drops the lowest-priority entry, which can be anywhere in
// the slice, and it runs from Enqueue under mu alone -- a flush mutex cannot
// see it. With positional removal, a flush that sent 2 then removed
// entries[:2] deleted whatever had shuffled into those slots, which is how a
// job_result enqueued mid-flush disappeared unsent.
//
// Staged deterministically: the sender blocks inside SendFrame on the first
// frame, the test enqueues an oversized answer that forces the cap to drop the
// commentary already sent, then releases. Removal by seq must take exactly the
// two entries the flush actually delivered.
func TestAnEntryEnqueuedDuringAFlushSurvivesTheCapDroppingAnother(t *testing.T) {
	// Small cap: two modest frames fit, a third forces a drop.
	b, _ := newBuffer(t, 400, nil)

	if err := b.Enqueue(wire.JobLog{JobID: "log-1", Msg: "commentary"}); err != nil {
		t.Fatal(err)
	}
	if err := b.Enqueue(wire.JobResult{JobID: "answer-1", State: wire.StateSucceeded}); err != nil {
		t.Fatal(err)
	}

	released := make(chan struct{})
	blocked := make(chan struct{}, 1)
	conn := &blockingFrameSender{fakeFrameSender: newFrameSender(), on: 2, blocked: blocked, release: released}

	done := make(chan error, 1)
	go func() { done <- b.Flush(context.Background(), conn) }()

	select {
	case <-blocked:
	case <-time.After(5 * time.Second):
		t.Fatal("the flush never reached the second frame; nothing was staged")
	}

	// Enqueued mid-flush, and big enough to push the cap over: enforceCapLocked
	// drops the LOWEST priority present, which is log-1 -- an entry this flush
	// has already sent and sits at position 0.
	if err := b.Enqueue(wire.JobResult{JobID: "answer-2", State: wire.StateSucceeded,
		Result: map[string]any{"payload": strings.Repeat("x", 300)}}); err != nil {
		t.Fatal(err)
	}
	close(released)

	if err := <-done; err != nil {
		t.Fatalf("flush: %v", err)
	}

	// Both delivered entries are gone; answer-2, which this flush never saw,
	// is still owed and still there.
	left := map[string]bool{}
	b.mu.Lock()
	for _, e := range b.entries {
		var m map[string]any
		if err := json.Unmarshal(e.Frame, &m); err != nil {
			b.mu.Unlock()
			t.Fatalf("buffered frame is not JSON: %s", e.Frame)
		}
		left[m["job_id"].(string)] = true
	}
	b.mu.Unlock()

	if !left["answer-2"] {
		t.Fatalf("answer-2 was enqueued during a flush that never sent it and is gone; buffer holds %v",
			left)
	}
	if left["answer-1"] {
		t.Fatalf("answer-1 was delivered but is still buffered; buffer holds %v", left)
	}
}

// blockingFrameSender parks inside the nth SendFrame until released, so a test
// can act while a flush is provably mid-flight rather than sleeping and hoping.
type blockingFrameSender struct {
	*fakeFrameSender
	on      int // block on this many-th frame (1-based)
	n       int
	blocked chan struct{}
	release chan struct{}
}

func (f *blockingFrameSender) SendFrame(ctx context.Context, b []byte) error {
	f.n++
	if f.n == f.on {
		f.blocked <- struct{}{}
		<-f.release
	}
	return f.fakeFrameSender.SendFrame(ctx, b)
}
