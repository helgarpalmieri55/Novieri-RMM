package capabilities_test

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"
)

// The two waits of TestAJobSurvivesADisconnectAndItsResultArrivesAfterReconnect
// are a CONDITION, bounded by the test binary's own deadline, not a fixed 15 s
// (design 2026-11-23-vps-y-marca-novieri §6). These hold the wait to that:
// a slow answer is waited for, a missing one still fails -- with the same
// diagnosis the fixed deadline gave.

func frame(kind, id, state string) []byte {
	return []byte(fmt.Sprintf(`{"v":1,"kind":%q,"job_id":%q,"state":%q}`, kind, id, state))
}

func TestWaitResultsWaitsForASlowAnswer(t *testing.T) {
	out := make(chan []byte, 8)
	go func() {
		out <- frame("job_status", "a", "running")
		out <- frame("job_result", "a", "succeeded")
		time.Sleep(300 * time.Millisecond) // slower than nothing, well inside the bound
		out <- frame("job_result", "b", "succeeded")
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	got, err := waitResults(ctx, out, []string{"a", "b"})
	if err != nil {
		t.Fatalf("a slow answer was not waited for: %v", err)
	}
	if got["a"].State != "succeeded" || got["b"].State != "succeeded" {
		t.Fatalf("results = %+v", got)
	}
}

func TestWaitResultsStillFailsAMissingAnswerAndSaysWhatItSaw(t *testing.T) {
	out := make(chan []byte, 8)
	out <- frame("job_status", "a", "running")
	out <- frame("job_status", "b", "running")
	out <- frame("job_result", "a", "succeeded")
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	_, err := waitResults(ctx, out, []string{"a", "b", "c"})
	if err == nil {
		t.Fatal("two answers missing, and the wait reported none")
	}
	for _, want := range []string{
		"2 of 3 jobs",
		"b (a job_status WAS seen for it",
		"c (no job_status ever seen for it",
		"map[job_result:1 job_status:2]",
	} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the failure does not say %q: %v", want, err)
		}
	}
}

func TestTheWaitIsBoundedByTheTestsOwnDeadline(t *testing.T) {
	ctx, cancel := untilTestEnds(t)
	defer cancel()
	deadline, ok := ctx.Deadline()
	if !ok {
		t.Fatal("the wait has no bound at all")
	}
	if testEnd, set := t.Deadline(); set && !deadline.Before(testEnd) {
		t.Fatalf("the wait ends at %s, not before the test binary's own %s", deadline, testEnd)
	}
}
