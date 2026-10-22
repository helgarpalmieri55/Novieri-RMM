package readreply

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

type fakeSender struct{ sent []wire.ReadQuery }

func (f *fakeSender) Send(_ context.Context, msg any) error {
	q, ok := msg.(wire.ReadQuery)
	if !ok {
		return errors.New("unexpected message type")
	}
	f.sent = append(f.sent, q)
	return nil
}

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestAQueryIsAnsweredByItsOwnGrant(t *testing.T) {
	r := New(quiet())
	s := &fakeSender{}
	r.Attach(s)

	done := make(chan wire.ReadGrant, 1)
	go func() {
		g, err := r.Query(context.Background(), wire.ReadQuery{
			JobID: "j1", QueryID: "q1", Suffixes: []string{"chunks/ab/abcd"},
		})
		if err != nil {
			t.Errorf("Query: %v", err)
		}
		done <- g
	}()

	waitForPending(t, r, 1)
	if !r.Deliver(&wire.ReadGrant{JobID: "j1", QueryID: "q1",
		Grants: []wire.ReadGrantItem{{Suffix: "chunks/ab/abcd", URL: "https://x"}}}) {
		t.Fatal("Deliver reported no waiter for a query that is pending")
	}
	if g := <-done; len(g.Grants) != 1 {
		t.Fatalf("answered with %+v, want one grant", g)
	}
}

func TestAGrantForAnotherJobDoesNotAnswerThisOne(t *testing.T) {
	// The key is (job_id, query_id): query_id restarts at "r1" for every job,
	// so a late grant from a finished job must not satisfy a live one.
	r := New(quiet())
	r.Attach(&fakeSender{})

	go func() {
		_, _ = r.Query(context.Background(), wire.ReadQuery{JobID: "j2", QueryID: "q1"})
	}()
	waitForPending(t, r, 1)

	if r.Deliver(&wire.ReadGrant{JobID: "j1", QueryID: "q1"}) {
		t.Fatal("a grant for job j1 answered a query from job j2")
	}
}

func TestADisconnectFailsEveryPendingWait(t *testing.T) {
	// A restore can run for hours; a reconnect must not leave a fetch blocked
	// until its deadline waiting for a reply that can no longer arrive.
	r := New(quiet())
	r.Attach(&fakeSender{})

	errc := make(chan error, 1)
	go func() {
		_, err := r.Query(context.Background(), wire.ReadQuery{JobID: "j1", QueryID: "q1"})
		errc <- err
	}()
	waitForPending(t, r, 1)

	r.Detach(errors.New("gateway disconnected"))
	select {
	case err := <-errc:
		if !errors.Is(err, ErrDisconnected) {
			t.Fatalf("pending Query after Detach = %v, want ErrDisconnected", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("a pending Query did not return within 2s of Detach")
	}
}

func TestQueryingWithNoGatewayAttachedFailsRatherThanBlocking(t *testing.T) {
	r := New(quiet())
	_, err := r.Query(context.Background(), wire.ReadQuery{JobID: "j1", QueryID: "q1"})
	if !errors.Is(err, ErrNoConnection) {
		t.Fatalf("Query with no sender attached = %v, want ErrNoConnection", err)
	}
}

func TestASilentControlPlaneEndsTheWaitWithANamedTimeout(t *testing.T) {
	// Every authorization refusal is silence (wire.ReadGrant's comment). If the
	// wait were unbounded, a refused restore would look hung forever.
	r := New(quiet())
	r.Deadline = 50 * time.Millisecond
	r.Attach(&fakeSender{})

	start := time.Now()
	_, err := r.Query(context.Background(), wire.ReadQuery{JobID: "j1", QueryID: "q1"})
	if !errors.Is(err, ErrTimeout) {
		t.Fatalf("Query against a silent server = %v, want ErrTimeout", err)
	}
	if d := time.Since(start); d > 2*time.Second {
		t.Fatalf("the wait took %s; it must be bounded by Deadline", d)
	}
}

func waitForPending(t *testing.T, r *Registry, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if r.Pending() == n {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("waited 2s for %d pending queries, have %d", n, r.Pending())
}
