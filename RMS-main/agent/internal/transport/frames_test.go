package transport

import (
	"context"
	"encoding/json"
	"errors"
	"sync/atomic"
	"testing"
	"time"
)

// SendFrame and OnConnected exist for the offline outbound buffer
// (internal/jobs). Both are additive Go API on an internal package; neither
// changes a byte on the wire.

func TestSendFrameDeliversAlreadyFramedBytesUnchanged(t *testing.T) {
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	conn, err := Dial(ctx, pki.options(g.url()))
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()

	// A frame the buffer produced at enqueue time, replayed verbatim after a
	// reconnect. If SendFrame re-encoded it, an outbound message could change
	// shape between being spooled and being sent.
	frame := []byte(`{"v":1,"kind":"job_result","job_id":"abc","state":"succeeded"}`)
	if err := conn.SendFrame(ctx, frame); err != nil {
		t.Fatalf("SendFrame: %v", err)
	}
	select {
	case got := <-g.received:
		if string(got) != string(frame) {
			t.Fatalf("gateway received %s, want the exact frame %s", got, frame)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the frame never arrived")
	}
}

func TestOnConnectedRunsWithTheLiveConnectionBeforeTheReadLoop(t *testing.T) {
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)

	var calls atomic.Int32
	opts := pki.options(g.url())
	opts.OnConnected = func(ctx context.Context, c *Conn) error {
		calls.Add(1)
		// This is the buffer's flush: frames go out before anything new is
		// produced against the connection.
		return c.SendFrame(ctx, []byte(`{"v":1,"kind":"job_result","job_id":"flushed","state":"succeeded"}`))
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = Run(ctx, opts, func(context.Context, []byte) {}) }()

	select {
	case got := <-g.received:
		var m map[string]any
		if err := json.Unmarshal(got, &m); err != nil {
			t.Fatal(err)
		}
		if m["job_id"] != "flushed" {
			t.Fatalf("first frame on the connection was %v; the buffer must drain first", m)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("OnConnected never sent anything")
	}
	if n := calls.Load(); n < 1 {
		t.Fatalf("OnConnected ran %d times", n)
	}
}

func TestAFailingOnConnectedClosesTheConnectionAndBacksOff(t *testing.T) {
	// An agent that cannot drain its answers should back off and try again, not
	// stay connected and pile new work on top of an outbox it cannot empty.
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)

	var attempts atomic.Int32
	opts := pki.options(g.url())
	opts.Backoff = Backoff{Base: time.Millisecond, Max: 2 * time.Millisecond}
	opts.Jitter = func() float64 { return 0 }
	opts.OnConnected = func(context.Context, *Conn) error {
		attempts.Add(1)
		return errors.New("flush failed")
	}

	ctx, cancel := context.WithCancel(context.Background())
	go func() { _ = Run(ctx, opts, func(context.Context, []byte) {}) }()

	deadline := time.After(10 * time.Second)
	for attempts.Load() < 2 {
		select {
		case <-deadline:
			cancel()
			t.Fatalf("only %d connect attempts; a failing OnConnected did not drive a reconnect",
				attempts.Load())
		case <-time.After(5 * time.Millisecond):
		}
	}
	cancel()
}
