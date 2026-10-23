package backup

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/canonical"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// These two tests drive backup.restore through the SAME path the control
// plane's frame ultimately takes -- a signed envelope, jobs.Runner.Handle,
// the registry, the capability, and back out as a wire.JobResult -- rather
// than calling s.restore directly the way every other test in this package
// does. That is deliberate: the defect this guards against (a *jobs.Refusal
// from the manifest fetch silently becoming a wire.StateSucceeded job with a
// `state=failed` body buried inside it) is only visible at the
// wire.JobResult.State level. Asserting on the INNER result field, the way
// s.restore's own tests do, would have passed the entire time this bug
// existed -- that is exactly how it got through review once already.
//
// This does not go through capabilities.Default() or backup.Process: both
// are process-wide singletons shared with every other test in this binary
// (or, for Process, the production binary), and mutating them here would
// make this file's outcome depend on test execution order. Instead each test
// builds its own Service via New(Options{...}) and its own single-entry
// jobs.Registry, which is enough to exercise jobs.Runner's real
// verify-then-dispatch path without touching any shared state.

const dispatchTestDeviceID = "6f1a2c34-5b6d-4e7f-8a9b-0c1d2e3f4a5b"

// singleHandlerRegistry maps exactly one job type to one handler.
type singleHandlerRegistry struct {
	jobType string
	handler jobs.Handler
}

func (r singleHandlerRegistry) Lookup(t string) (jobs.Handler, bool) {
	if t == r.jobType {
		return r.handler, true
	}
	return nil, false
}

// dispatchNonces accepts every nonce once; replay is internal/jobs' subject,
// not this file's.
type dispatchNonces struct{}

func (dispatchNonces) SeenOrRecord(string, time.Time) bool { return false }

// dispatchRecorder captures every message a Runner sent, the way the gateway
// would receive it over the wire.
type dispatchRecorder struct {
	mu   sync.Mutex
	msgs []any
}

func (r *dispatchRecorder) Send(_ context.Context, msg any) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.msgs = append(r.msgs, msg)
	return nil
}

// result waits, bounded, for the one job_result the runner sent.
//
// Bounded-polled rather than a single synchronous read: since Task 6 wired
// jobs.Dispatcher into Runner.Handle, dispatchRestore's call to
// runner.Handle verifies and submits the job but returns before
// svc.Restore has necessarily run -- capability execution now happens on a
// Dispatcher worker goroutine.
func (r *dispatchRecorder) result(t *testing.T) wire.JobResult {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		r.mu.Lock()
		msgs := append([]any(nil), r.msgs...)
		r.mu.Unlock()
		for _, msg := range msgs {
			if res, ok := msg.(wire.JobResult); ok {
				return res
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("no job_result was sent; the operator watches this job hang. sent: %v", msgs)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// dispatchRestore signs a backup.restore job the way app/jobs/engine.py does
// -- Ed25519 over canonical_bytes, base64 std -- and feeds the frame to a
// real jobs.Runner bound to svc via a one-entry registry, exactly the shape
// capabilities/integration_test.go uses for `hello` and `net.sweep`.
func dispatchRestore(t *testing.T, svc *Service, params json.RawMessage, jobID string) wire.JobResult {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	sender := &dispatchRecorder{}
	now := time.Now().UTC().Truncate(time.Second)
	runner, err := jobs.New(jobs.Options{
		BaseContext: t.Context(),
		DeviceID:    dispatchTestDeviceID,
		SigningKey:  pub,
		Nonces:      dispatchNonces{},
		Registry:    singleHandlerRegistry{jobType: JobTypeRestore, handler: svc.Restore},
		Sender:      sender,
		Now:         func() time.Time { return now },
		Logger:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatalf("jobs.New: %v", err)
	}
	job := fmt.Sprintf(
		`{"id":%q,"type":%q,"device":%q,"params":%s,"issued_at":%q,"expires_at":%q,"nonce":%q}`,
		jobID, JobTypeRestore, dispatchTestDeviceID, params,
		now.Add(-time.Second).Format("2006-01-02T15:04:05Z"),
		now.Add(10*time.Minute).Format("2006-01-02T15:04:05Z"),
		jobID)
	msg, err := canonical.BytesFromRaw(json.RawMessage(job))
	if err != nil {
		t.Fatalf("canonicalising the fixture: %v", err)
	}
	sig := base64.StdEncoding.EncodeToString(ed25519.Sign(priv, msg))
	frame := []byte(fmt.Sprintf(`{"v":1,"kind":"job","job":%s,"sig":%q}`, job, sig))
	runner.Handle(context.Background(), frame)
	return sender.result(t)
}

func TestARestoreWhoseManifestFetchIsRefusedComesBackRefusedOnTheWire(t *testing.T) {
	store := newFakeStore(testStorageRef)
	store.failGet = func(string) error {
		return jobs.Refuse("backup: this agent cannot read from the repository (test refusal)")
	}
	svc := New(Options{
		OpenStore: func(context.Context, Repository) (ObjectStore, error) { return store, nil },
		Logger:    quiet(),
	})

	manifestRef := store.prefix + "manifests/deadbeef"
	params := restoreParamsJSON(t, manifestRef, nil, "", modeVerify)

	result := dispatchRestore(t, svc, params, testJobID)
	if result.State != wire.StateRefused {
		t.Fatalf("state = %q, result = %v; want refused -- a *jobs.Refusal from GetRef must "+
			"reach the runner as an error, not a state=failed body inside a succeeded job",
			result.State, result.Result)
	}
	reason, ok := result.Result["reason"].(string)
	if !ok || reason == "" {
		t.Fatalf("the refusal carried no reason: %v", result.Result)
	}
	if svc.Restores() != 0 {
		t.Errorf("restores = %d after a refused job, want 0 -- a refusal means nothing ran",
			svc.Restores())
	}
}

func TestARestoreWhoseManifestFetchFailsTransientlyStillSucceedsWithAFailedBody(t *testing.T) {
	store := newFakeStore(testStorageRef)
	store.failGet = func(string) error { return errors.New("connection reset by peer") }
	svc := New(Options{
		OpenStore: func(context.Context, Repository) (ObjectStore, error) { return store, nil },
		Logger:    quiet(),
	})

	manifestRef := store.prefix + "manifests/deadbeef"
	params := restoreParamsJSON(t, manifestRef, nil, "", modeVerify)

	result := dispatchRestore(t, svc, params, testJobID)
	// The old, still-correct shape: a plain transport failure means the
	// device is capable and the network was not, so the JOB succeeds and the
	// failure is reported inside its result document, not on the wire state.
	if result.State != wire.StateSucceeded {
		t.Fatalf("state = %q, result = %v; want succeeded (with an inner failed body) for a "+
			"non-refusal store error -- this shape must not change", result.State, result.Result)
	}
	if got := result.Result["state"]; got != stateFailed {
		t.Fatalf("result[state] = %v; want %q", got, stateFailed)
	}
	if errMsg, _ := result.Result["error"].(string); errMsg == "" {
		t.Fatalf("result[error] is empty; want the redacted fetch failure")
	}
	if svc.Restores() != 1 {
		t.Errorf("restores = %d after one completed (if unsuccessful) restore job, want 1",
			svc.Restores())
	}
}
