package capabilities_test

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/canonical"
	"github.com/toeshee/it-suite/agent/internal/capabilities"
	"github.com/toeshee/it-suite/agent/internal/capabilities/inventory"
	"github.com/toeshee/it-suite/agent/internal/capabilities/netsweep"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/readreply"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// The only place a capability in this repository is invoked with a real
// jobs.VerifiedJob.
//
// jobs.VerifiedJob is an interface with an unexported method, so the runner is
// the ONLY thing that can produce one (verified.go, construct_test.go). That is
// invariant #1 made structural, and it means a capability package's own tests
// can never hand their handler a job. This test therefore drives the whole
// path: a server-signed frame -> the five checks -> the registry -> the
// capability -> the job_result the gateway parses.
//
// It is also the only test that exercises `hello`'s parameter handling, since
// `hello` is the one capability that reads job.Params().

const testDeviceID = "6f1a2c34-5b6d-4e7f-8a9b-0c1d2e3f4a5b"

// baseNow is the harness's clock, and it tracks the REAL one on purpose.
//
// It used to be a fixed 2026-08-05T12:00:00Z. The verifier's freshness check is
// made against this value, so the fixtures verified fine -- but Runner.run
// bounds the capability's context with `context.WithDeadline(ctx,
// job.ExpiresAt())`, and THAT is compared against the wall clock. Every
// capability in this harness was therefore invoked with a context that had
// already expired, silently, for as long as the fixed date was in the past.
//
// Nothing noticed until net.sweep, which is the first capability whose work is
// bounded by its own deadline: its first end-to-end run reported
// `probed_count: 0, truncated: true` and an errors[] entry saying the job
// deadline had passed. Truncated to the second because rfc3339 here is
// whole-second (app/jobs/canonical.py::rfc3339).
var baseNow = time.Now().UTC().Truncate(time.Second)

func TestASignedInventoryCollectJobProducesTheDocumentAtTheTopLevel(t *testing.T) {
	h := newHarness(t)
	h.dispatch(t, "inventory.collect", `{}`, "9c3f0a11-0000-4000-8000-000000000001")

	// A real collection, not a fake: on Windows two PowerShell processes, CIM
	// and Get-AppxPackage -AllUsers, which on a CI VM take tens of seconds.
	// The deadline is the capability's own stated bound plus margin, so a
	// slow machine passes and a hang still fails.
	result := h.resultWithin(t, inventory.MaxCollectDuration+10*time.Second)
	if result.State != wire.StateSucceeded {
		t.Fatalf("state = %q, result = %v; want succeeded", result.State, result.Result)
	}
	// handler.py:27 does InventoryDocument(**result): hostname and os_family
	// are required at the TOP level of this dict.
	for _, key := range []string{"hostname", "os_family", "hardware"} {
		if _, ok := result.Result[key]; !ok {
			t.Fatalf("job_result.result has no %q; keys = %v", key, keys(result.Result))
		}
	}
}

func TestASignedHelloJobEchoesItsMessage(t *testing.T) {
	// scripts/demo_e2e_agent.py asserts exactly this:
	//     final["result"].get("greeting") == "hello from the control plane"
	h := newHarness(t)
	h.dispatch(t, "hello", `{"message":"hello from the control plane"}`,
		"9c3f0a11-0000-4000-8000-000000000002")

	result := h.result(t)
	if result.State != wire.StateSucceeded {
		t.Fatalf("state = %q, result = %v; want succeeded", result.State, result.Result)
	}
	if got := result.Result["greeting"]; got != "hello from the control plane" {
		t.Fatalf("greeting = %v; want the dispatched message", got)
	}
}

func TestHelloWithoutAMessageUsesTheServersDefault(t *testing.T) {
	// HelloParams.message defaults to "ping" server-side, and the params dict
	// the engine signs carries the model's dump -- but an old server that
	// dispatched `{}` must still get an answer rather than an error. Inbound is
	// loose (docs/kernel/wire-protocol/README.md).
	h := newHarness(t)
	h.dispatch(t, "hello", `{}`, "9c3f0a11-0000-4000-8000-000000000003")

	result := h.result(t)
	if result.State != wire.StateSucceeded {
		t.Fatalf("state = %q; want succeeded", result.State)
	}
	if got := result.Result["greeting"]; got != "ping" {
		t.Fatalf("greeting = %v; want %q", got, "ping")
	}
}

func TestHelloIgnoresParametersItDoesNotKnow(t *testing.T) {
	// A newer control plane that adds an optional field to HelloParams must not
	// take an old fleet offline. Inbound is loose: unknown members are ignored,
	// not refused.
	h := newHarness(t)
	h.dispatch(t, "hello", `{"message":"hi","loudness":11,"nested":{"a":[1,2]}}`,
		"9c3f0a11-0000-4000-8000-000000000004")

	result := h.result(t)
	if result.State != wire.StateSucceeded {
		t.Fatalf("state = %q, result = %v; want succeeded -- an unknown params member must be "+
			"ignored, not refused", result.State, result.Result)
	}
	if got := result.Result["greeting"]; got != "hi" {
		t.Fatalf("greeting = %v; want %q", got, "hi")
	}
}

func TestAJobTypeThisBuildDoesNotHaveIsRefusedAndNothingRuns(t *testing.T) {
	// The registry's `false` is what produces this refusal, and IPAM's
	// dispatcher reads a refused job as "this agent cannot do that" (§5.3).
	//
	// FALSIFIER: a `succeeded` or `failed` state, or a running device count
	// above zero. "An error came back" is not the observation -- the observation
	// is that the refusal is the terminal state and no capability was reached.
	h := newHarness(t)
	h.dispatch(t, "script.run", `{"script":"rm -rf /"}`, "9c3f0a11-0000-4000-8000-000000000005")

	result := h.result(t)
	if result.State != wire.StateRefused {
		t.Fatalf("state = %q; want refused for a type this build has no capability for", result.State)
	}
	if reason, _ := result.Result["reason"].(string); reason == "" {
		t.Fatalf("refusal carried no reason; dispatch.py reads result[\"reason\"]")
	}
	if before, after := h.runsBefore, capabilities.Names(); len(after) != len(before) {
		t.Fatalf("the registry changed under the runner: %v -> %v", before, after)
	}
}

func TestBackupRunIsRegisteredButUnboundRefusesNamingTheMissingTransport(t *testing.T) {
	// backup.Process.Bind is cmd/agent/main.go's job, not any test's -- so in
	// this binary the capability is registered (registry.go) but unbound.
	// registry.go's comment records that `backup.run` does not complete even
	// once Bind has run -- OpenSnapshot is still nil and the dedup exchange
	// still deadlocks (spec §6.1 items 4-5) -- but neither of those is what
	// this test is about: this harness never calls Bind at all, so the
	// refusal here must still name the missing TRANSPORT rather than read
	// like backup.run does not exist -- `unknown_type` and "no transport
	// wired" are both `refused` on the wire, and IPAM's dispatcher must be
	// able to tell them apart from the reason.
	h := newHarness(t)
	repoKey := base64.StdEncoding.EncodeToString(make([]byte, 32))
	params := fmt.Sprintf(
		`{"repository_id":"22222222-2222-2222-2222-222222222222",`+
			`"storage_ref":"11111111-1111-1111-1111-111111111111/backup/22222222-2222-2222-2222-222222222222",`+
			`"repo_key":%q,"snapshot":"vss","sources":{"paths":["/tmp"]}}`, repoKey)
	h.dispatch(t, "backup.run", params, "9c3f0a11-0000-4000-8000-000000000006")

	result := h.result(t)
	if result.State != wire.StateRefused {
		t.Fatalf("state = %q, result = %v; want refused -- this build has backup.run "+
			"registered, so an unbound Process must refuse rather than run or crash",
			result.State, result.Result)
	}
	reason, ok := result.Result["reason"].(string)
	if !ok || reason == "" {
		t.Fatalf("the refusal carried no reason: %v", result.Result)
	}
	if !strings.Contains(reason, "no way to ask the control plane") {
		t.Fatalf("reason = %q; want the QueryChunks-is-nil refusal that names the missing "+
			"dedup exchange, so an operator can tell this apart from an unregistered type",
			reason)
	}
}

// ---------------------------------------------------------------------------
// Inbound routing: a backup_read_grant must reach the read registry and
// NEVER the runner (cmd/agent/main.go's `inbound` closure, Task 8). Modelled
// on the same routing shape concurrency_harness_test.go copies from main.go
// for the chunk-grant half of this exchange.
//
// main.go's `inbound` is unexported inside package main and cannot be
// imported here, so this test builds the identical three-way shape locally --
// the same thing this package's own concurrency harness does for the chunk
// grant -- rather than exercising main.go itself.
// ---------------------------------------------------------------------------

// TestABackupReadGrantReachesTheReadRegistryNotTheRunner is the routing test
// Task 8's brief calls for. It asserts BOTH halves, deliberately: that the
// registry actually receives the grant, and that the runner is never invoked
// for it. Either assertion alone would pass on a bug the other catches -- a
// grant that is silently dropped (reaching neither) would still leave
// runnerCalls at 0, and a grant that reaches both would still let the Query
// below succeed.
//
// FALSIFIER for the "not the runner" half: route the read-grant frame through
// the runner branch instead of (or in addition to) readGrants.Deliver.
// runnerCalls would then be >0 and this test fails.
//
// FALSIFIER for the "reaches the registry" half: drop the frame instead of
// delivering it (e.g. an inbound that returns before calling Deliver on a
// decoded *wire.ReadGrant). The pending Query would then time out instead of
// returning, and this test fails on that wait.
func TestABackupReadGrantReachesTheReadRegistryNotTheRunner(t *testing.T) {
	h := newHarness(t)

	readGrants := readreply.New(slog.New(slog.NewTextHandler(io.Discard, nil)))
	readGrants.Attach(&recorder{})

	var runnerCalls int32
	// inbound mirrors cmd/agent/main.go's routing shape: a read grant answers
	// a question a running job asked and goes to the read registry; anything
	// else -- including an ordinary job -- reaches the runner unchanged.
	inbound := func(c context.Context, frame []byte) {
		if msg, err := wire.Decode(frame); err == nil {
			if g, ok := msg.(*wire.ReadGrant); ok {
				readGrants.Deliver(g)
				return
			}
		}
		atomic.AddInt32(&runnerCalls, 1)
		h.runner.Handle(c, frame)
	}

	// A fetch waiting on this exact (job, query) pair, the way
	// backupstore.Get's read loop does through readreply.Registry.Query.
	const readJobID, readQueryID = "9c3f0a11-0000-4000-8000-00000000d100", "r1"
	done := make(chan wire.ReadGrant, 1)
	errc := make(chan error, 1)
	go func() {
		g, err := readGrants.Query(context.Background(), wire.ReadQuery{
			JobID: readJobID, QueryID: readQueryID, Suffixes: []string{"ab/abcdef"},
		})
		if err != nil {
			errc <- err
			return
		}
		done <- g
	}()
	waitForReadPending(t, readGrants, 1)

	inbound(context.Background(), encodeReadGrant(readJobID, readQueryID))

	select {
	case g := <-done:
		if g.JobID != readJobID || g.QueryID != readQueryID {
			t.Fatalf("delivered grant = %+v, want job=%s query=%s", g, readJobID, readQueryID)
		}
	case err := <-errc:
		t.Fatalf("Query: %v", err)
	case <-time.After(2 * time.Second):
		t.Fatal("the read registry never received the grant routed through inbound")
	}

	if n := atomic.LoadInt32(&runnerCalls); n != 0 {
		t.Fatalf("a backup_read_grant frame reached the runner %d time(s) instead of only the "+
			"read registry -- misrouted there it would be refused as an unknown job type, a "+
			"confusing way to discover a routing bug", n)
	}

	// Control: an ORDINARY job, through the SAME inbound function, must still
	// reach the runner. Without this, runnerCalls staying at 0 above could
	// just as easily mean the counter never increments at all -- this proves
	// it does, so the assertion above is a real check and not dead code.
	sentBefore := len(h.sender.sent())
	inbound(context.Background(),
		h.signedJobFrame(t, "hello", `{"message":"control"}`, "9c3f0a11-0000-4000-8000-00000000d101"))
	if n := atomic.LoadInt32(&runnerCalls); n != 1 {
		t.Fatalf("runnerCalls = %d after routing an ordinary job through inbound, want 1: "+
			"everything that is not a chunk or read grant must still reach the runner unchanged", n)
	}
	result := h.result(t)
	if result.State != wire.StateSucceeded {
		t.Fatalf("control job state = %q, want succeeded: it must have actually run", result.State)
	}
	if len(h.sender.sent()) <= sentBefore {
		t.Fatal("the control job produced no new frames; the runner did not actually run it")
	}
}

// encodeReadGrant builds a minimal, well-formed wire.ReadGrant frame
// answering exactly the query named by jobID/queryID -- the two fields
// readreply.Registry.Deliver correlates on (see internal/readreply's key
// type). Grants and ExpiresAt are left at their zero value: this test does
// not read them.
func encodeReadGrant(jobID, queryID string) []byte {
	return []byte(fmt.Sprintf(
		`{"v":1,"kind":%q,"job_id":%q,"query_id":%q,"grants":[]}`,
		wire.KindBackupReadGrant, jobID, queryID))
}

// waitForReadPending polls until r has exactly n queries in flight, or fails
// the test. Mirrors internal/readreply's own test helper of the same shape;
// duplicated rather than exported since it exists only to synchronise a test
// goroutine with the one that just called Query.
func waitForReadPending(t *testing.T, r *readreply.Registry, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if r.Pending() == n {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("waited 2s for %d pending read queries, have %d", n, r.Pending())
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

type harness struct {
	runner     *jobs.Runner
	sender     *recorder
	priv       ed25519.PrivateKey
	runsBefore []string
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	sender := &recorder{}
	runner, err := jobs.New(jobs.Options{
		// The test's own lifetime stands in for main.go's process context. It
		// is required (jobs.DispatcherOptions.BaseContext) so that no caller
		// can silently run jobs under a per-connection context.
		BaseContext: t.Context(),
		DeviceID:    testDeviceID,
		SigningKey:  pub,
		Nonces:      freshNonces{},
		Registry:    capabilities.Default(),
		Sender:      sender,
		Now:         func() time.Time { return baseNow },
		Logger:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	return &harness{runner: runner, sender: sender, priv: priv, runsBefore: capabilities.Names()}
}

// dispatch signs a job the way app/jobs/engine.py does -- Ed25519 over
// canonical_bytes of the job payload, base64 std -- and feeds the frame to the
// runner.
func (h *harness) dispatch(t *testing.T, jobType, params, id string) {
	t.Helper()
	h.runner.Handle(context.Background(), h.signedJobFrame(t, jobType, params, id))
}

// signedJobFrame builds the same signed frame dispatch feeds to the runner,
// without submitting it -- for a test that needs to route the frame through
// something other than h.runner.Handle directly (e.g. an inbound closure that
// decides where a frame goes before the runner ever sees it).
func (h *harness) signedJobFrame(t *testing.T, jobType, params, id string) []byte {
	t.Helper()
	job := fmt.Sprintf(
		`{"id":%q,"type":%q,"device":%q,"params":%s,"issued_at":%q,"expires_at":%q,"nonce":%q}`,
		id, jobType, testDeviceID, params,
		baseNow.Add(-time.Second).UTC().Format("2006-01-02T15:04:05Z"),
		baseNow.Add(10*time.Minute).UTC().Format("2006-01-02T15:04:05Z"),
		id)
	msg, err := canonical.BytesFromRaw(json.RawMessage(job))
	if err != nil {
		t.Fatalf("canonicalising the fixture: %v", err)
	}
	sig := base64.StdEncoding.EncodeToString(ed25519.Sign(h.priv, msg))
	return []byte(fmt.Sprintf(`{"v":1,"kind":"job","job":%s,"sig":%q}`, job, sig))
}

// result returns the one job_result the runner sent.
//
// Bounded-polled rather than a single synchronous read: since Task 6 wired
// jobs.Dispatcher into Runner.Handle, Handle verifies and submits on this
// call's own goroutine but the capability itself runs on a Dispatcher worker
// goroutine, asynchronously. dispatch(t, ...) can therefore return before the
// job_result exists yet -- true for every capability this file actually
// dispatches (even "unregistered type" tests here name types that ARE
// registered by another package, e.g. script.run/deploy -- see
// TestAJobTypeThisBuildDoesNotHaveIsRefusedAndNothingRuns's own comment).
func (h *harness) result(t *testing.T) wire.JobResult {
	t.Helper()
	return h.resultWithin(t, 10*time.Second)
}

// resultWithin is result with the wait stated by the caller, for a capability
// whose honest worst case is longer than the default.
func (h *harness) resultWithin(t *testing.T, wait time.Duration) wire.JobResult {
	t.Helper()
	deadline := time.Now().Add(wait)
	for {
		for _, msg := range h.sender.sent() {
			if r, ok := msg.(wire.JobResult); ok {
				return r
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("no job_result was sent; the operator watches this job hang. sent: %v", h.sender.sent())
		}
		time.Sleep(2 * time.Millisecond)
	}
}

type recorder struct {
	mu   sync.Mutex
	msgs []any
}

func (r *recorder) Send(_ context.Context, msg any) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.msgs = append(r.msgs, msg)
	return nil
}

func (r *recorder) sent() []any {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]any(nil), r.msgs...)
}

// freshNonces accepts every nonce once; replay is internal/jobs' subject, not
// this file's.
type freshNonces struct{}

func (freshNonces) SeenOrRecord(string, time.Time) bool { return false }

func keys(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// ---------------------------------------------------------------------------
// net.sweep, end to end (docs/modules/ipam/spec.md §5, §6)
// ---------------------------------------------------------------------------

func TestASignedNetSweepJobProbesLoopbackAndReturnsTheDocumentAtTheTopLevel(t *testing.T) {
	// The whole path for the one capability that puts packets on a wire: a
	// server-signed frame, the five checks, the registry, a raw ICMP socket
	// opened inside the job, and a NetSweepDocument the result handler can
	// consume.
	//
	// It sweeps 127.0.0.1/32 -- the one address this container can legitimately
	// probe. Where the process lacks CAP_NET_RAW the capability REFUSES, and
	// that branch is asserted rather than skipped.
	if runtime.GOOS != "linux" {
		t.Skip("raw ICMP sockets; CI is linux")
	}
	if netsweep.Process.Runs() != 0 {
		t.Fatalf("the sweeper had already run %d times before any job arrived", netsweep.Process.Runs())
	}
	targets := []string{"127.0.0.1/32"}
	params := fmt.Sprintf(
		`{"subnet_id":"3f2a1b7c-0d4e-4a19-9f8b-2c6d5e4f3a21",`+
			`"sweep_run_id":"b8e1c4d2-77aa-4f3e-8b21-9c0d3e5f7a14",`+
			`"targets":["127.0.0.1/32"],"scope_digest":"%s",`+
			`"method":"icmp","probe_timeout_ms":200,"budget_s":10}`,
		netsweep.Digest(targets))

	h := newHarness(t)
	h.dispatch(t, "net.sweep", params, "9c3f0a11-0000-4000-8000-00000000d001")
	result := h.result(t)

	if result.State == wire.StateRefused {
		t.Logf("no raw socket privilege here; the capability refused: %v", result.Result["reason"])
		return
	}
	if result.State != wire.StateSucceeded {
		t.Fatalf("state = %q, result = %v", result.State, result.Result)
	}
	// handle_sweep_result parses the TOP-LEVEL dict as a NetSweepDocument, so
	// nothing may be nested under a wrapper key.
	for _, key := range []string{"subnet_id", "sweep_run_id", "scope_digest", "method",
		"started_at", "finished_at", "probed_count"} {
		if _, ok := result.Result[key]; !ok {
			t.Fatalf("job_result.result has no %q; keys = %v", key, keys(result.Result))
		}
	}
	if got := result.Result["scope_digest"]; got != netsweep.Digest(targets) {
		t.Fatalf("scope_digest = %v; the agent must echo the signed value", got)
	}
	if netsweep.Process.Runs() != 1 {
		t.Fatalf("the sweeper's run counter is %d after one job", netsweep.Process.Runs())
	}
	t.Logf("net.sweep over the signed-job path: %v", result.Result)
}

func TestASignedNetSweepWhoseDigestDoesNotMatchComesBackRefusedNotFailed(t *testing.T) {
	// The refusal path, through the RUNNER rather than through the capability's
	// own tests -- because `refused` versus `failed` is a wire-visible
	// distinction the runner makes, and IPAM's dispatcher acts on it (it takes a
	// refusing device out of the sweep rotation for 7 days and picks another on
	// the segment, spec §5.3).
	//
	// FALSIFYING OBSERVATION: `failed`, or a `succeeded` document.
	params := `{"subnet_id":"3f2a1b7c-0d4e-4a19-9f8b-2c6d5e4f3a21",` +
		`"sweep_run_id":"b8e1c4d2-77aa-4f3e-8b21-9c0d3e5f7a14",` +
		`"targets":["127.0.0.1/32"],` +
		`"scope_digest":"0000000000000000000000000000000000000000000000000000000000000000"}`

	h := newHarness(t)
	h.dispatch(t, "net.sweep", params, "9c3f0a11-0000-4000-8000-00000000d002")
	result := h.result(t)

	if result.State != wire.StateRefused {
		t.Fatalf("state = %q, result = %v; want refused", result.State, result.Result)
	}
	// gateway/dispatch.py:334-337 reads result["reason"], defaulting to "agent
	// refused" -- a refusal without one tells the operator nothing.
	reason, ok := result.Result["reason"].(string)
	if !ok || reason == "" {
		t.Fatalf("the refusal carries no reason: %v", result.Result)
	}
	if !strings.Contains(reason, "scope_digest") {
		t.Fatalf("reason = %q; it must name what was checked", reason)
	}
	t.Logf("refusal reason on the wire: %q", reason)
}
