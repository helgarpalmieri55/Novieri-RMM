package chunkreply_test

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"runtime"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/chunkreply"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

func quiet() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// fakeSender records what was sent and optionally answers.
type fakeSender struct {
	mu   sync.Mutex
	sent []wire.ChunkQuery
	err  error
}

func (f *fakeSender) Send(_ context.Context, msg any) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	f.sent = append(f.sent, msg.(wire.ChunkQuery))
	return nil
}

func (f *fakeSender) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.sent)
}

func TestQueryReturnsTheMatchingGrant(t *testing.T) {
	r := chunkreply.New(quiet())
	s := &fakeSender{}
	r.Attach(s)

	go func() {
		for r.Pending() == 0 {
			runtime.Gosched()
		}
		r.Deliver(&wire.ChunkGrant{JobID: "job-1", QueryID: "q1", New: []string{"ab"}, Recorded: 3})
	}()

	got, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "job-1", QueryID: "q1"})
	if err != nil {
		t.Fatalf("Query: %v", err)
	}
	state := [3]any{got.QueryID, len(got.New), got.Recorded}
	want := [3]any{"q1", 1, int64(3)}
	if state != want {
		t.Errorf("grant = %v, want %v", state, want)
	}
	if s.count() != 1 {
		t.Errorf("sent %d queries, want 1", s.count())
	}
}

// Silence IS the refusal (protocol.py:259). Without a deadline an
// unauthorized backup.run hangs forever instead of failing.
func TestQueryTimesOutOnSilence(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Deadline = 20 * time.Millisecond
	r.Attach(&fakeSender{})

	start := time.Now()
	_, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "q1"})
	if !errors.Is(err, chunkreply.ErrTimeout) {
		t.Fatalf("err = %v, want ErrTimeout", err)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("waited %v; the deadline did not bound the wait", elapsed)
	}
	if r.Pending() != 0 {
		t.Errorf("pending = %d after timeout, want 0 (the entry leaked)", r.Pending())
	}
}

// A reply cannot arrive on a new connection, so the wait must not burn the
// full deadline after a drop.
func TestDetachFailsPendingWaitsImmediately(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Deadline = time.Hour
	r.Attach(&fakeSender{})

	done := make(chan error, 1)
	go func() {
		_, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "q1"})
		done <- err
	}()
	for r.Pending() == 0 {
		runtime.Gosched()
	}
	r.Detach(errors.New("socket closed"))

	select {
	case err := <-done:
		if !errors.Is(err, chunkreply.ErrDisconnected) {
			t.Errorf("err = %v, want ErrDisconnected", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Detach did not wake the pending wait")
	}
}

// The pair is the key: a counter restarts at q1 every run.
func TestDeliverMatchesOnJobAndQueryTogether(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Deadline = 50 * time.Millisecond
	r.Attach(&fakeSender{})

	done := make(chan error, 1)
	go func() {
		_, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "job-A", QueryID: "q1"})
		done <- err
	}()
	for r.Pending() == 0 {
		runtime.Gosched()
	}
	// Same query id, different job: a late grant from a previous run.
	if r.Deliver(&wire.ChunkGrant{JobID: "job-B", QueryID: "q1"}) {
		t.Error("a grant for another job was routed to this waiter")
	}
	if err := <-done; !errors.Is(err, chunkreply.ErrTimeout) {
		t.Errorf("err = %v, want ErrTimeout", err)
	}
}

func TestDeliverDropsUnmatchedAndDuplicateGrants(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Attach(&fakeSender{})

	if r.Deliver(&wire.ChunkGrant{JobID: "j", QueryID: "nobody-waiting"}) {
		t.Error("an unmatched grant reported as delivered")
	}

	go func() {
		for r.Pending() == 0 {
			runtime.Gosched()
		}
		r.Deliver(&wire.ChunkGrant{JobID: "j", QueryID: "q1"})
		if r.Deliver(&wire.ChunkGrant{JobID: "j", QueryID: "q1"}) {
			t.Error("a duplicate grant reported as delivered")
		}
	}()
	if _, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "q1"}); err != nil {
		t.Fatalf("Query: %v", err)
	}
}

func TestQueryWithoutAConnectionFailsFast(t *testing.T) {
	r := chunkreply.New(quiet())
	_, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "q1"})
	if !errors.Is(err, chunkreply.ErrNoConnection) {
		t.Errorf("err = %v, want ErrNoConnection", err)
	}
}

// maxPendingForTest mirrors the package's unexported maxPending (16). One run
// flushes serially so the realistic count is 1; this pins the cap that turns
// a bug spawning unbounded flushes into a named failure.
const maxPendingForTest = 16

// A registry that already holds maxPending queries must refuse a 17th rather
// than grow without bound.
func TestQueryRefusesOnceMaxPendingIsInFlight(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Deadline = time.Hour // long enough that none of these time out mid-test
	r.Attach(&fakeSender{})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel() // unblocks the maxPending goroutines via ctx.Done() at exit

	for i := 0; i < maxPendingForTest; i++ {
		go func(i int) {
			_, _ = r.Query(ctx, wire.ChunkQuery{JobID: "j", QueryID: fmt.Sprintf("q%d", i)})
		}(i)
	}
	for r.Pending() < maxPendingForTest {
		runtime.Gosched()
	}

	_, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "overflow"})
	if !errors.Is(err, chunkreply.ErrTooManyPending) {
		t.Fatalf("err = %v, want ErrTooManyPending", err)
	}
}

// A query_id already in flight must be refused with an errors.Is-able
// sentinel, not silently overwrite the first waiter's channel.
func TestQueryRefusesADuplicateQueryIDAlreadyInFlight(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Deadline = time.Hour
	r.Attach(&fakeSender{})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		_, _ = r.Query(ctx, wire.ChunkQuery{JobID: "j", QueryID: "q1"})
	}()
	for r.Pending() == 0 {
		runtime.Gosched()
	}

	_, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "q1"})
	if !errors.Is(err, chunkreply.ErrDuplicateQuery) {
		t.Fatalf("err = %v, want ErrDuplicateQuery", err)
	}
}

// The caller's context is one of three branches in Query's select -- the
// package exists to get this right independently of the server-side
// deadline.
func TestQueryReturnsWhenTheCallersContextIsCancelled(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Deadline = time.Hour // must not be what ends this wait
	r.Attach(&fakeSender{})

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		for r.Pending() == 0 {
			runtime.Gosched()
		}
		cancel()
	}()

	_, err := r.Query(ctx, wire.ChunkQuery{JobID: "j", QueryID: "q1"})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	if r.Pending() != 0 {
		t.Errorf("pending = %d after cancellation, want 0 (the entry leaked)", r.Pending())
	}
}

// A failed conn.Send must forget the pending slot it just reserved. Left
// unhandled, every failed send permanently burns one of maxPending against a
// query that never actually went out -- fakeSender.err exists for this.
func TestQuerySendFailureForgetsThePendingSlot(t *testing.T) {
	r := chunkreply.New(quiet())
	r.Attach(&fakeSender{err: errors.New("write: broken pipe")})

	if _, err := r.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "q1"}); err == nil {
		t.Fatal("Query succeeded despite Send failing")
	}
	if r.Pending() != 0 {
		t.Errorf("pending = %d after a failed Send, want 0 -- forget(k) must run on this path "+
			"or a send failure permanently leaks a slot against maxPending", r.Pending())
	}
}

// The idle budget: a registry that has never been queried holds nothing and
// runs nothing.
func TestIdleRegistryHoldsNothing(t *testing.T) {
	before := runtime.NumGoroutine()
	r := chunkreply.New(quiet())
	r.Attach(&fakeSender{})
	runtime.Gosched()
	if got := runtime.NumGoroutine(); got > before {
		t.Errorf("goroutines %d -> %d; the registry started one at rest", before, got)
	}
	if r.Pending() != 0 {
		t.Errorf("pending = %d at rest, want 0", r.Pending())
	}
}
