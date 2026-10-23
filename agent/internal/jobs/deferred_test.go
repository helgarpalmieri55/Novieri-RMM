package jobs

import (
	"context"
	"fmt"
	"log/slog"
	"testing"
	"time"
)

// deferredHarness runs a capability that returns ErrDeferred under two job
// types: the one that may defer, and one that may not.
func deferredHarness(t *testing.T, s signer, h Handler) (*Runner, *fakeSender) {
	t.Helper()
	sender := &fakeSender{}
	r, err := New(Options{
		BaseContext: t.Context(),
		DeviceID:    testDeviceID,
		SigningKey:  s.pub,
		Nonces:      newMemNonces(),
		Registry:    mapRegistry{DeferrableJobType: h, "hello": h},
		Sender:      sender,
		Now:         func() time.Time { return baseNow },
		Logger:      slog.New(slog.NewTextHandler(&logBuf{}, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	return r, sender
}

func typedJob(id, jobType, nonce string) string {
	return jobText(id, jobType, testDeviceID, `{}`,
		ts(baseNow.Add(-time.Second)), ts(time.Now().Add(10*time.Minute)), nonce)
}

func TestAgentUpdateMayDeferItsResult(t *testing.T) {
	s := newSigner(t)
	deferring := func(context.Context, VerifiedJob) (any, error) {
		return nil, fmt.Errorf("staged: %w", ErrDeferred)
	}
	r, sender := deferredHarness(t, s, deferring)

	r.Handle(t.Context(), s.frame(t, typedJob("9c3f0a11-0000-4000-8000-0000000000d1", DeferrableJobType, "n-d1")))

	sender.only(t, "job_status")
	// Give a wrongly emitted result time to show up before asserting absence.
	time.Sleep(50 * time.Millisecond)
	for _, f := range sender.frames() {
		if f["kind"] == "job_result" {
			t.Fatalf("a deferred job must send no result from this process, got %v", f)
		}
	}
}

func TestErrDeferredFromAnyOtherTypeBecomesFailed(t *testing.T) {
	s := newSigner(t)
	deferring := func(context.Context, VerifiedJob) (any, error) { return nil, ErrDeferred }
	r, sender := deferredHarness(t, s, deferring)

	r.Handle(t.Context(), s.frame(t, typedJob("9c3f0a11-0000-4000-8000-0000000000d2", "hello", "n-d2")))

	res := sender.only(t, "job_result")
	if res["state"] != "failed" {
		t.Fatalf("state = %v, want failed", res["state"])
	}
}

func TestExecutingCountsRunningJobsExceptTheCaller(t *testing.T) {
	s := newSigner(t)
	h := newInterruptHarness(t, s)
	if n := h.runner.Executing(""); n != 0 {
		t.Fatalf("idle runner reports %d executing", n)
	}

	id := "9c3f0a11-0000-4000-8000-0000000000e1"
	h.runner.Handle(t.Context(), s.frame(t, jobWithID(id, "n-e1")))
	h.block.awaitStart(t, 5*time.Second)

	if n := h.runner.Executing(""); n != 1 {
		t.Fatalf("Executing(\"\") = %d, want 1", n)
	}
	if n := h.runner.Executing(id); n != 0 {
		t.Fatalf("Executing(self) = %d, want 0", n)
	}
	h.runner.Interrupt(id)
	h.block.awaitFinish(t, 5*time.Second)
}
