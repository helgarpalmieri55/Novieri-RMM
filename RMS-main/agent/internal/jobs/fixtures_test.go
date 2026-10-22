package jobs

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/canonical"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// The fixtures every verification test is built from.
//
// A frame is assembled from RAW TEXT, not from a marshalled struct, for the
// same reason wire.Envelope keeps RawJob: the signature covers the bytes the
// server produced, and a test that re-marshals its own struct proves the agent
// agrees with itself rather than with the control plane. Tampering is done by
// byte substitution on an otherwise-valid frame, so exactly one property
// differs per case.

const (
	testDeviceID = "6f1a2c34-5b6d-4e7f-8a9b-0c1d2e3f4a5b"
	otherDevice  = "11111111-2222-4333-8444-555555555555"
)

type signer struct {
	pub  ed25519.PublicKey
	priv ed25519.PrivateKey
}

func newSigner(t *testing.T) signer {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return signer{pub: pub, priv: priv}
}

// jobText renders the `job` member the way the gateway does: json.dumps of a
// dict, insertion-ordered, no spaces. The exact spelling matters only in that
// it is what gets canonicalised and what gets signed.
func jobText(id, jobType, device, params, issuedAt, expiresAt, nonce string) string {
	return fmt.Sprintf(
		`{"id":%q,"type":%q,"device":%q,"params":%s,"issued_at":%q,"expires_at":%q,"nonce":%q}`,
		id, jobType, device, params, issuedAt, expiresAt, nonce)
}

// frame signs jobText verbatim and wraps it in a v1 job envelope.
func (s signer) frame(t *testing.T, job string) []byte {
	t.Helper()
	msg, err := canonical.BytesFromRaw(json.RawMessage(job))
	if err != nil {
		t.Fatalf("canonicalising the fixture failed: %v", err)
	}
	sig := base64.StdEncoding.EncodeToString(ed25519.Sign(s.priv, msg))
	return []byte(fmt.Sprintf(`{"v":1,"kind":"job","job":%s,"sig":%q}`, job, sig))
}

// ts renders app/jobs/canonical.py::rfc3339 -- whole seconds, literal Z.
func ts(t time.Time) string { return t.UTC().Format("2006-01-02T15:04:05Z") }

// baseNow is the fixed "now" every table case is evaluated at.
var baseNow = time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)

// validJob is a well-formed, in-window, correctly-addressed hello job.
func validJob(nonce string) string {
	return jobText(
		"9c3f0a11-0000-4000-8000-000000000001",
		"hello",
		testDeviceID,
		`{"message":"hello from the control plane","rate_pps":50}`,
		ts(baseNow.Add(-time.Second)),
		ts(baseNow.Add(10*time.Minute)),
		nonce,
	)
}

// ---------------------------------------------------------------------------
// The spy capability: the falsifying observation for every negative case
// ---------------------------------------------------------------------------

// spyCapability counts invocations and writes a marker file.
//
// "An error was returned" is NOT an observation of invariant #1: a runner that
// invokes a capability and then returns an error passes an error-shaped
// assertion while having already run attacker-chosen code on the endpoint. The
// counter and the marker file are the observation, and every negative case
// asserts both.
type spyCapability struct {
	mu         sync.Mutex
	calls      int
	seen       []VerifiedJob
	markerPath string
	result     any
	err        error
	panicWith  any
}

func newSpy(t *testing.T) *spyCapability {
	t.Helper()
	return &spyCapability{
		markerPath: filepath.Join(t.TempDir(), "capability-ran"),
		result:     map[string]any{"greeting": "hello from the control plane"},
	}
}

func (s *spyCapability) handle(_ context.Context, j VerifiedJob) (any, error) {
	s.mu.Lock()
	s.calls++
	s.seen = append(s.seen, j)
	s.mu.Unlock()
	if err := os.WriteFile(s.markerPath, []byte(j.ID()), 0o600); err != nil {
		panic(err)
	}
	if s.panicWith != nil {
		panic(s.panicWith)
	}
	return s.result, s.err
}

func (s *spyCapability) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.calls
}

// awaitCount blocks, bounded, until the spy has been called at least `want`
// times, then asserts the count is EXACTLY want.
//
// Needed since Task 6 wired jobs.Dispatcher into Runner.Handle: Handle now
// verifies and submits a job on the caller's goroutine but the capability
// itself runs on a Dispatcher worker goroutine, asynchronously -- so a test
// calling h.runner.Handle(...) and immediately checking s.count() races
// against the worker actually having run yet. This is that wait, bounded so a
// genuine defect (the capability never runs at all) still fails within d
// rather than hanging the test.
func (s *spyCapability) awaitCount(t *testing.T, want int, d time.Duration) {
	t.Helper()
	deadline := time.Now().Add(d)
	for {
		if n := s.count(); n >= want {
			if n != want {
				t.Fatalf("capability ran %d times, want exactly %d", n, want)
			}
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("capability ran %d times after %s, want %d", s.count(), d, want)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// assertDidNotRun is the invariant #1 assertion. Both halves are required: the
// counter catches an invocation, the marker catches a side effect that reached
// the filesystem even if the counter were somehow reset.
func (s *spyCapability) assertDidNotRun(t *testing.T, what string) {
	t.Helper()
	if n := s.count(); n != 0 {
		t.Fatalf("capability RAN %d time(s) for %s: invariant #1 breached", n, what)
	}
	if _, err := os.Stat(s.markerPath); err == nil {
		t.Fatalf("capability produced its side effect (%s exists) for %s", s.markerPath, what)
	}
}

// ---------------------------------------------------------------------------
// Test doubles for the two collaborators Tasks 8 and 10 own
// ---------------------------------------------------------------------------

type mapRegistry map[string]Handler

func (m mapRegistry) Lookup(jobType string) (Handler, bool) {
	h, ok := m[jobType]
	return h, ok
}

// fakeSender records the frames the runner emitted, decoded back into a shape a
// test can assert on. It is the stand-in for the gateway connection.
type fakeSender struct {
	mu   sync.Mutex
	sent []map[string]any
	err  error
}

func (f *fakeSender) Send(_ context.Context, msg any) error {
	if f.err != nil {
		return f.err
	}
	b, err := wire.Encode(msg)
	if err != nil {
		return err
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return err
	}
	f.mu.Lock()
	f.sent = append(f.sent, m)
	f.mu.Unlock()
	return nil
}

func (f *fakeSender) frames() []map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]map[string]any, len(f.sent))
	copy(out, f.sent)
	return out
}

// only returns the single frame of the given kind, failing if there is not
// exactly one.
//
// Bounded-polled rather than a single synchronous read, for the same reason
// spyCapability.awaitCount is: the frame `only` wants may not have been sent
// yet the instant Handle returns, since capability execution (and the
// job_status/job_result sends around it) now happens on a Dispatcher worker
// goroutine. A genuinely wrong count (0, or more than 1) still fails within
// the bound rather than hanging.
func (f *fakeSender) only(t *testing.T, kind string) map[string]any {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		var found []map[string]any
		for _, m := range f.frames() {
			if m["kind"] == kind {
				found = append(found, m)
			}
		}
		if len(found) == 1 {
			return found[0]
		}
		if len(found) > 1 || time.Now().After(deadline) {
			t.Fatalf("want exactly one %s frame, got %d (all frames: %v)", kind, len(found), f.frames())
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// awaitAtLeast blocks, bounded, until at least n frames have been recorded.
// Used ahead of an ordering assertion (e.g. via kinds()) that needs more than
// one frame to have arrived, where only() (which waits for exactly one frame
// OF A GIVEN KIND) does not fit.
func (f *fakeSender) awaitAtLeast(t *testing.T, n int, d time.Duration) {
	t.Helper()
	deadline := time.Now().Add(d)
	for {
		if len(f.frames()) >= n {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("only %d frame(s) after %s, want at least %d: %v", len(f.frames()), d, n, f.frames())
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// awaitKindCount blocks, bounded, until at least n frames of the given kind
// have been recorded, then returns every matching frame observed at that
// point.
//
// This exists for a narrower and more security-sensitive case than
// awaitAtLeast or spyCapability.awaitCount cover on their own: a test that
// delivers the SAME nonce twice and must tell "both refused" apart from "a
// broken replay defence let both run" cannot sample the spy's counter the
// moment it first reaches the expected value -- if the defence is broken,
// BOTH deliveries dispatch, and a counter read the instant it hits 1 can miss
// that a second run is seconds away from making it 2, which is a false PASS
// on the exact security property the test exists to falsify. Waiting for
// TWO job_result frames -- one per Handle call, produced whether that
// delivery was refused synchronously or dispatched and reported later --
// means both deliveries have reached a terminal state before anything else
// is read, regardless of which path either one took.
func (f *fakeSender) awaitKindCount(t *testing.T, kind string, n int, d time.Duration) []map[string]any {
	t.Helper()
	deadline := time.Now().Add(d)
	for {
		var found []map[string]any
		for _, m := range f.frames() {
			if m["kind"] == kind {
				found = append(found, m)
			}
		}
		if len(found) >= n {
			return found
		}
		if time.Now().After(deadline) {
			t.Fatalf("only %d %s frame(s) after %s, want at least %d: %v", len(found), kind, d, n, f.frames())
		}
		time.Sleep(2 * time.Millisecond)
	}
}

func (f *fakeSender) kinds() []string {
	var out []string
	for _, m := range f.frames() {
		out = append(out, fmt.Sprint(m["kind"]))
	}
	return out
}

// reasonOf pulls result.reason out of a job_result frame. gateway/dispatch.py
// reads exactly this key.
func reasonOf(t *testing.T, frame map[string]any) string {
	t.Helper()
	res, ok := frame["result"].(map[string]any)
	if !ok {
		t.Fatalf("job_result carries no result object: %v", frame)
	}
	s, _ := res["reason"].(string)
	return s
}

// logBuf captures the runner's log so a test can assert on what an operator
// would see for a frame too broken to answer.
type logBuf struct {
	mu sync.Mutex
	b  strings.Builder
}

func (l *logBuf) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}

func (l *logBuf) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.String()
}

// awaitContains blocks, bounded, until the captured log text contains substr.
// Needed for the same reason spyCapability.awaitCount is (Task 6: capability
// execution, and the logging around it, now happens on a Dispatcher worker
// goroutine): a test checking the log immediately after Handle returns would
// otherwise race against the worker actually having written to it.
func (l *logBuf) awaitContains(t *testing.T, substr string, d time.Duration) {
	t.Helper()
	deadline := time.Now().Add(d)
	for {
		if s := l.String(); strings.Contains(s, substr) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("log does not contain %q within %s:\n%s", substr, d, l.String())
		}
		time.Sleep(2 * time.Millisecond)
	}
}
