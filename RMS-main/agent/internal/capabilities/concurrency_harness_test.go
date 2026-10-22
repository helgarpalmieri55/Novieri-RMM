package capabilities_test

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/toeshee/it-suite/agent/internal/canonical"
	"github.com/toeshee/it-suite/agent/internal/capabilities"
	"github.com/toeshee/it-suite/agent/internal/chunkreply"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/transport"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// A harness that composes all three layers a deployed agent actually runs:
// transport.Run, on a REAL mutually-authenticated WebSocket, driving
// jobs.Runner.Handle, dispatching into a REAL capabilities.Registry -- exactly
// the shape cmd/agent/main.go wires them (main.go:253-265). No other test in
// this tree does that: capability tests inject their own transport seam and
// answer in-process (see integration_test.go's *harness*, which calls
// runner.Handle directly with no socket underneath); runner tests feed
// synthetic frames with no transport underneath; transport tests use a no-op
// handler. The dedup deadlock described in HANDOFF.md §2 -- a capability
// blocking on a reply that only the goroutine it occupies could read -- lives
// exactly in the gap between those seams, which is why a fully green suite
// never saw it. This file is the instrument that can.
//
// # Naming
//
// integration_test.go already declares `harness` and `newHarness` in this
// package (a lighter-weight harness that dispatches straight into
// runner.Handle, no transport, no socket -- cert_rotation_test.go depends on
// it too). This file cannot redeclare either name without breaking the build,
// and per this task's own constraints ("the only changed file must be
// concurrency_harness_test.go") the existing declarations cannot be renamed to
// make room. So this harness is `concurrencyHarness` / `newConcurrencyHarness`
// rather than the `harness` / `newHarness` named in the task brief. See
// task-1-report.md for the full note.
//
// Two names from that brief's surface are GONE, having never acquired a
// caller. `awaitLog` waited for a job_log matching a substring; no test here
// emits one. `encodeHeartbeat` built an uncorrelated chunk grant for a
// "the reader stays free" test, which
// TestACapabilityCanReceiveACorrelatedReplyWhileItRuns subsumes far more
// strongly -- it requires a reply the running job is actually WAITING on to
// arrive, not merely that an ignorable frame was consumed. Dead scaffolding in
// a harness reads as a facility the tests use, and is worse than absent.

// ---------------------------------------------------------------------------
// harnessOptions / newConcurrencyHarness
// ---------------------------------------------------------------------------

// harnessOptions configures a concurrencyHarness.
type harnessOptions struct {
	// register runs before the runner starts, against the harness's own
	// (initially empty but for "hello") registry, so a test can add its own
	// probe capability. send is the exact function the runner sends through --
	// a probe can use it to emit its own frames (a job_log, say) independently
	// of the runner's automatic running/result frames. Nil means "defaults
	// only": just `hello`, wired the way capabilities.Hello is in the real
	// registry.
	register func(reg *capabilities.Registry, send func(context.Context, any) error)

	// grantChunkQueries, when true, makes the fake gateway answer every
	// inbound backup_chunk_query it reads with a matching wire.ChunkGrant --
	// same JobID and QueryID, echoed straight off the query -- fired from the
	// gateway's own goroutine, independent of anything the agent is doing.
	// This is what lets TestACapabilityCanReceiveACorrelatedReplyWhileItRuns
	// put a real reply "in flight" without the test orchestrating the timing:
	// the only question left is whether the AGENT's read goroutine is free to
	// receive it.
	grantChunkQueries bool
}

// concurrencyHarness is `newHarness`/`harness` from the task brief; see the
// package comment above for why the names differ.
type concurrencyHarness struct {
	t        *testing.T
	gateway  *harnessGateway
	deviceID string
	priv     ed25519.PrivateKey
	seq      atomic.Uint64

	// replies is the harness's real chunkreply.Registry -- the same object
	// wired into the harness's own inbound routing above. Exposed so a
	// register hook's probe capability can call replies.Query itself, the way
	// backup.run does, rather than the harness inventing a stand-in.
	replies *chunkreply.Registry

	cancel context.CancelFunc
	done   chan error
}

// deviceID is fixed and distinct from integration_test.go's testDeviceID so a
// stray cross-harness signature could never verify by accident.
const concurrencyDeviceID = "6f1a2c34-5b6d-4e7f-8a9b-0c1d2e3f4a5d"

func newConcurrencyHarness(t *testing.T, opts harnessOptions) *concurrencyHarness {
	t.Helper()

	pki := newHarnessPKI(t)
	gw := newHarnessGateway(t, pki, opts.grantChunkQueries)

	// Discarded by default: a passing test should produce silence. A failing
	// one gets the *testing.T failure message itself, which is where the
	// signal belongs -- not a slog stream a human has to scroll past to find
	// it. (Step 4's temporary probe run captures this deliberately; see
	// task-1-report.md.)
	log := slog.New(slog.NewTextHandler(io.Discard, nil))

	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("generating the job-signing key: %v", err)
	}

	// The outbound half, wired exactly as main.go wires it: a persisted
	// buffer wrapped in an Outbox, so the runner's Sender is the real
	// production type and not a test double.
	buf := jobs.OpenBuffer(filepath.Join(harnessSpoolDir(t), "outbox.jsonl"), 0, log)
	outbox := jobs.NewOutbox(buf, log)

	// The reply half of the backup dedup exchange -- the real
	// internal/chunkreply used in production, not a stand-in. Step 4 uses
	// this directly: it is the actual mechanism the deadlock in HANDOFF.md §2
	// describes, not an analogue of it.
	replies := chunkreply.New(log)

	reg := capabilities.New()
	if err := reg.Register("hello", capabilities.Hello); err != nil {
		t.Fatalf("registering hello: %v", err)
	}
	if opts.register != nil {
		opts.register(reg, outbox.Send)
	}

	// This ctx is the harness's PROCESS context: it is what transport.Run is
	// given below, and cancelling it is what "the agent stops" means here. It
	// is created before the runner because the runner needs it too, and needs
	// exactly this one -- main.go passes service.Run's ctx for the same reason.
	// The transport derives its own per-connection context from it internally
	// (transport.serve), and that one must never reach a job: see
	// jobs.DispatcherOptions.BaseContext, and
	// TestAJobSurvivesADisconnectAndItsResultArrivesAfterReconnect below.
	ctx, cancel := context.WithCancel(context.Background())

	runner, err := jobs.New(jobs.Options{
		BaseContext: ctx,
		DeviceID:    concurrencyDeviceID,
		SigningKey:  pub,
		Nonces:      freshNonces{}, // declared in integration_test.go; same package
		Registry:    reg,
		Sender:      outbox,
		Logger:      log,
	})
	if err != nil {
		cancel()
		t.Fatalf("jobs.New: %v", err)
	}

	// One inbound entry point, two destinations -- copied from main.go:260-272
	// verbatim in shape: a chunk grant answers a question a running job asked
	// and goes to the reply registry; everything else is a job and goes to
	// the runner, which verifies its signature before anything runs.
	inbound := func(c context.Context, frame []byte) {
		if msg, err := wire.Decode(frame); err == nil {
			if g, ok := msg.(*wire.ChunkGrant); ok {
				replies.Deliver(g)
				return
			}
		}
		runner.Handle(c, frame)
	}

	done := make(chan error, 1)
	go func() {
		done <- transport.Run(ctx, transport.Options{
			GatewayURL: gw.url(),
			ClientCert: pki.client,
			RootCAs:    pki.caPool,
			Logger:     log,
			OnConnected: func(c context.Context, conn *transport.Conn) error {
				// Drain-before-new, exactly as main.go's comment requires:
				// "so a job's answer never overtakes an older one."
				if err := outbox.Attach(c, conn); err != nil {
					return err
				}
				replies.Attach(conn)
				return nil
			},
			OnDisconnect: func(_ context.Context, cause error) { replies.Detach(cause) },
		}, inbound)
	}()

	h := &concurrencyHarness{
		t: t, gateway: gw, deviceID: concurrencyDeviceID, priv: priv,
		replies: replies,
		cancel:  cancel, done: done,
	}

	// Fail fast, in setup, rather than as an opaque timeout on the first
	// sendJob: if the dial itself is broken there is no deadlock to observe,
	// only a harness that never got off the ground.
	select {
	case <-gw.ready:
	case err := <-done:
		cancel()
		t.Fatalf("transport.Run exited before the agent ever connected: %v", err)
	case <-time.After(5 * time.Second):
		cancel()
		t.Fatal("the agent did not connect to the fake gateway within 5s")
	}

	return h
}

// harnessSpoolDir is a temp directory this harness removes itself, in place of
// t.TempDir().
//
// t.TempDir() asserts that NOTHING touches the directory after the test
// function returns, and since capabilities moved onto Dispatcher workers that
// assertion is no longer true of a healthy agent. A worker finishing a job
// hands its job_result frame to the writer goroutine (transport.Conn.out)
// BEFORE Flush relocks the outbound Buffer and rewrites the spool -- so the
// gateway can read the answer, and awaitResult can return, while the agent is
// still doing its own bookkeeping. Nothing here waits for that: close() waits
// for transport.Run, and the runner's Dispatcher is not reachable from this
// package to drain. Observed as `TempDir RemoveAll cleanup: directory not
// empty`, with the recreated file being the spool a Flush had just emptied.
//
// It is faithful to the shipped binary -- process shutdown does not drain
// in-flight jobs either -- so the harness stops asserting otherwise and
// removes the directory best-effort instead. Nothing leaks: RemoveAll runs,
// and a spool recreated a microsecond later sits in the OS temp directory of a
// test process that is about to exit.
func harnessSpoolDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "its-agent-harness-")
	if err != nil {
		t.Fatalf("creating the harness spool directory: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return dir
}

// close cancels the run loop and waits for transport.Run to return.
func (h *concurrencyHarness) close() {
	h.t.Helper()
	h.cancel()
	select {
	case err := <-h.done:
		if err != nil {
			h.t.Errorf("transport.Run returned %v at shutdown; want nil (ctx cancellation is not "+
				"a terminal verdict)", err)
		}
	case <-time.After(5 * time.Second):
		h.t.Errorf("transport.Run did not exit within 5s of the harness closing")
	}
}

// sendJob signs a job the way app/jobs/engine.py does -- Ed25519 over
// canonical_bytes of the job payload, base64 standard encoding -- and pushes
// it to the agent as the gateway would. Returns the job id.
func (h *concurrencyHarness) sendJob(jobType string, payload map[string]any) string {
	h.t.Helper()

	params := []byte("{}")
	if payload != nil {
		b, err := json.Marshal(payload)
		if err != nil {
			h.t.Fatalf("marshalling job params: %v", err)
		}
		params = b
	}

	id := fmt.Sprintf("9c3f0a11-conc-4000-8000-%012d", h.seq.Add(1))
	now := time.Now().UTC()
	job := fmt.Sprintf(
		`{"id":%q,"type":%q,"device":%q,"params":%s,"issued_at":%q,"expires_at":%q,"nonce":%q}`,
		id, jobType, h.deviceID, params,
		now.Add(-time.Second).Format("2006-01-02T15:04:05Z"),
		now.Add(5*time.Minute).Format("2006-01-02T15:04:05Z"),
		id, // nonce: unique per call, same as the id
	)
	msg, err := canonical.BytesFromRaw(json.RawMessage(job))
	if err != nil {
		h.t.Fatalf("canonicalising the job fixture: %v", err)
	}
	sig := base64.StdEncoding.EncodeToString(ed25519.Sign(h.priv, msg))
	frame := []byte(fmt.Sprintf(`{"v":1,"kind":"job","job":%s,"sig":%q}`, job, sig))

	h.gateway.send(h.t, frame)
	return id
}

// awaitResult blocks until the agent's job_result for jobID crosses the wire,
// or fails the test after d.
func (h *concurrencyHarness) awaitResult(t *testing.T, jobID string, d time.Duration) wire.JobResult {
	t.Helper()
	deadline := time.NewTimer(d)
	defer deadline.Stop()
	for {
		select {
		case frame := <-h.gateway.outbound:
			if kindOf(t, frame) != wire.KindJobResult {
				continue
			}
			var r wire.JobResult
			if err := json.Unmarshal(frame, &r); err != nil {
				t.Fatalf("undecodable job_result frame: %v (%s)", err, frame)
			}
			if r.JobID == jobID {
				return r
			}
		case <-deadline.C:
			t.Fatalf("no job_result for job %s arrived within %s", jobID, d)
		}
	}
}

// awaitResults blocks until a job_result has arrived for every id in ids, and
// returns them keyed by job id.
//
// It exists because awaitResult DISCARDS every job_result that is not the one
// it was asked for: calling it in a loop over several ids only works if the
// answers happen to cross the wire in exactly that order, and silently loses
// one when they do not. That is not a hypothetical -- it produced a 15s
// timeout naming the wrong job while this file's disconnect test was being
// written, because the third job's answer arrived while the second was still
// being waited for. Anything expecting more than one answer must use this.
//
// On timeout, the failure message reports what a reproduction would otherwise
// be needed to find out: which ids never got a job_result, whether each of
// those ids ever produced a job_status (i.e. started running, as opposed to
// never having been dispatched at all), and how many frames of each kind were
// drained off the wire while waiting. This exists because
// TestAJobSurvivesADisconnectAndItsResultArrivesAfterReconnect failed once,
// with this loop's plain "no job_result ... for 1 of 3 jobs" message, and has
// not reproduced since (see that test's comment) -- so the next occurrence,
// whenever it comes, is the only chance to see why, and a bare list of
// missing ids does not say whether the job ever started.
func (h *concurrencyHarness) awaitResults(t *testing.T, ids []string, d time.Duration) map[string]wire.JobResult {
	t.Helper()
	want := make(map[string]bool, len(ids))
	for _, id := range ids {
		want[id] = true
	}
	got := make(map[string]wire.JobResult, len(ids))
	started := make(map[string]bool, len(ids)) // ids for which a job_status was seen
	frameCounts := make(map[string]int)        // every frame kind drained, including ones not addressed to `ids`
	deadline := time.NewTimer(d)
	defer deadline.Stop()
	for len(got) < len(want) {
		select {
		case frame := <-h.gateway.outbound:
			kind := kindOf(t, frame)
			frameCounts[kind]++
			switch kind {
			case wire.KindJobStatus:
				var s wire.JobStatus
				if err := json.Unmarshal(frame, &s); err == nil && want[s.JobID] {
					started[s.JobID] = true
				}
			case wire.KindJobResult:
				var r wire.JobResult
				if err := json.Unmarshal(frame, &r); err != nil {
					t.Fatalf("undecodable job_result frame: %v (%s)", err, frame)
				}
				if want[r.JobID] {
					got[r.JobID] = r
				}
			}
		case <-deadline.C:
			var missing []string
			total := 0
			for _, n := range frameCounts {
				total += n
			}
			for _, id := range ids {
				if _, ok := got[id]; ok {
					continue
				}
				status := "no job_status ever seen for it (it may never have started)"
				if started[id] {
					status = "a job_status WAS seen for it (it started, but never reported a result)"
				}
				missing = append(missing, fmt.Sprintf("%s (%s)", id, status))
			}
			t.Fatalf("no job_result arrived within %s for %d of %d jobs: %v; drained %d frames total "+
				"while waiting, by kind: %v", d, len(missing), len(ids), missing, total, frameCounts)
		}
	}
	return got
}

// kindOf peeks an outbound frame's `kind` without committing to a full
// decode -- wire.Decode itself only recognises the two INBOUND kinds (job,
// backup_chunk_grant) and would reject every frame this harness's gateway
// actually receives, since everything the agent sends is one of the seven
// OUTBOUND kinds (job_status, job_log, job_result, heartbeat, ...). This is
// the reader for the other direction.
func kindOf(t *testing.T, frame []byte) string {
	t.Helper()
	var envelope struct {
		Kind string `json:"kind"`
	}
	if err := json.Unmarshal(frame, &envelope); err != nil {
		t.Fatalf("outbound frame is not JSON: %v (%s)", err, frame)
	}
	return envelope.Kind
}

// chunkQueryFrom reports the job_id/query_id of an outbound
// backup_chunk_query frame. Like kindOf, this reads a frame the AGENT sent --
// wire.Decode only recognises the two INBOUND kinds and would reject this one
// outright, so the gateway peeks the same way kindOf does.
func chunkQueryFrom(frame []byte) (jobID, queryID string, ok bool) {
	var q struct {
		Kind    string `json:"kind"`
		JobID   string `json:"job_id"`
		QueryID string `json:"query_id"`
	}
	if err := json.Unmarshal(frame, &q); err != nil || q.Kind != wire.KindBackupChunkQuery {
		return "", "", false
	}
	return q.JobID, q.QueryID, true
}

// encodeChunkGrant builds a minimal, well-formed wire.ChunkGrant frame that
// answers exactly the query named by jobID/queryID. Matching those two
// fields is what lets chunkreply.Registry.Deliver correlate the reply to a
// specific waiter (see internal/chunkreply's key type) -- get either one
// wrong and the wait times out regardless of anything under test. The other
// fields (Recorded, Upload, ExpiresAt) are left at their zero value: nothing
// in this file's tests reads them.
func encodeChunkGrant(jobID, queryID string) []byte {
	return []byte(fmt.Sprintf(
		`{"v":1,"kind":%q,"job_id":%q,"query_id":%q,"recorded":0}`,
		wire.KindBackupChunkGrant, jobID, queryID))
}

// must panics on a setup error raised from inside a register hook, where
// there is no *testing.T in scope. register runs synchronously on the test's
// own goroutine (inside newConcurrencyHarness, before it returns), so the
// panic surfaces as a normal, attributable test failure rather than being
// swallowed on some other goroutine.
func must(err error) {
	if err != nil {
		panic(err)
	}
}

// ---------------------------------------------------------------------------
// The fake gateway: mutual TLS 1.3 + a coder/websocket accept handler,
// modelled on internal/transport/transport_test.go:38-206. Duplicated rather
// than shared, per the convention netsweep_test.go:1240 states for exactly
// this situation ("moving it into non-test code to share it would put a
// [transport-test-only shape] in the shipped binary for the sake of a test").
// newFakeGateway itself is unexported in package transport and cannot be
// imported at all.
// ---------------------------------------------------------------------------

type harnessPKI struct {
	caPool *x509.CertPool
	client tls.Certificate
	server tls.Certificate
}

func newHarnessPKI(t *testing.T) *harnessPKI {
	t.Helper()
	caCert, caKey := harnessSelfSignedCA(t, "its-concurrency-test-ca")
	pool := x509.NewCertPool()
	pool.AddCert(caCert)
	notAfter := time.Now().Add(24 * time.Hour)
	return &harnessPKI{
		caPool: pool,
		server: harnessIssueCert(t, caCert, caKey, "gateway", []net.IP{net.ParseIP("127.0.0.1")},
			x509.ExtKeyUsageServerAuth, notAfter),
		client: harnessIssueCert(t, caCert, caKey, "device", nil,
			x509.ExtKeyUsageClientAuth, notAfter),
	}
}

func harnessSelfSignedCA(t *testing.T, cn string) (*x509.Certificate, ed25519.PrivateKey) {
	t.Helper()
	pub, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: cn},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, pub, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return cert, key
}

func harnessIssueCert(
	t *testing.T,
	caCert *x509.Certificate,
	caKey ed25519.PrivateKey,
	cn string,
	ips []net.IP,
	eku x509.ExtKeyUsage,
	notAfter time.Time,
) tls.Certificate {
	t.Helper()
	pub, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: cn},
		NotBefore:    time.Now().Add(-5 * time.Minute),
		NotAfter:     notAfter,
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{eku},
		IPAddresses:  ips,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, caCert, pub, caKey)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
	pair, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		t.Fatal(err)
	}
	pair.Leaf = leaf
	return pair
}

// harnessGateway is the fake control plane: it accepts exactly the mutual-TLS
// WebSocket handshake the real gateway would, records every outbound frame
// the agent sends, and can push an inbound frame at a moment the test chooses.
//
// It accepts REPEATEDLY, not once. It used to keep a single connection for the
// life of a test, which was enough while every test here held one -- but the
// event that matters most to a job running off the read goroutine is the
// connection going AWAY and coming back, and a gateway that cannot drop a
// connection cannot stage it. `generation` counts accepted connections so
// awaitReconnect can wait for a genuinely new one rather than for a duration.
type harnessGateway struct {
	srv      *httptest.Server
	outbound chan []byte // every frame read from the agent, in arrival order

	mu         sync.Mutex
	cur        *websocket.Conn // the connection currently accepted, or nil
	generation int             // how many connections have been accepted
	readyOnce  sync.Once
	ready      chan struct{} // closed the instant the FIRST connection is accepted
}

// gen reports how many connections have been accepted so far.
func (g *harnessGateway) gen() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.generation
}

// drop kills the current connection the way a gateway restart or a network
// partition does: no close frame, no goodbye -- the agent's blocking read
// simply fails and transport.Run's backoff loop redials.
func (g *harnessGateway) drop(t *testing.T) {
	t.Helper()
	g.mu.Lock()
	conn := g.cur
	g.cur = nil
	g.mu.Unlock()
	if conn == nil {
		t.Fatal("harness gateway: drop called with no connection accepted")
	}
	_ = conn.CloseNow()
}

// awaitReconnect blocks until a connection NEWER than after has been accepted.
// Polling rather than a channel because the accept happens inside net/http's
// handler goroutine, which this harness does not own.
func (g *harnessGateway) awaitReconnect(t *testing.T, after int, d time.Duration) {
	t.Helper()
	deadline := time.Now().Add(d)
	for {
		if g.gen() > after {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("the agent did not reconnect within %s (still on connection %d)", d, g.gen())
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func newHarnessGateway(t *testing.T, pki *harnessPKI, grantChunkQueries bool) *harnessGateway {
	t.Helper()
	g := &harnessGateway{
		outbound: make(chan []byte, 256),
		ready:    make(chan struct{}),
	}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		defer conn.CloseNow() //nolint:errcheck
		g.mu.Lock()
		g.cur = conn
		g.generation++
		g.mu.Unlock()
		// Once: a reconnect must not close an already-closed channel, and
		// `ready` means "the agent got off the ground", which happens once.
		g.readyOnce.Do(func() { close(g.ready) })
		defer func() {
			g.mu.Lock()
			if g.cur == conn {
				g.cur = nil
			}
			g.mu.Unlock()
		}()
		for {
			_, b, err := conn.Read(r.Context())
			if err != nil {
				return
			}
			select {
			case g.outbound <- b:
			default:
				t.Errorf("harness gateway: outbound buffer full (256); a frame was dropped")
			}
			// Answered from THIS goroutine -- the gateway's own connection
			// handler -- never the test goroutine. The reply reaching the
			// agent therefore depends only on the agent's read goroutine being
			// free to receive it, which is exactly the thing under test in
			// TestACapabilityCanReceiveACorrelatedReplyWhileItRuns.
			if grantChunkQueries {
				if jobID, queryID, ok := chunkQueryFrom(b); ok {
					wctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
					werr := conn.Write(wctx, websocket.MessageText, encodeChunkGrant(jobID, queryID))
					cancel()
					if werr != nil {
						t.Errorf("harness gateway: replying to a backup_chunk_query: %v", werr)
					}
				}
			}
		}
	}))
	srv.TLS = &tls.Config{
		Certificates: []tls.Certificate{pki.server},
		ClientCAs:    pki.caPool,
		ClientAuth:   tls.RequireAndVerifyClientCert,
		MinVersion:   tls.VersionTLS13,
	}
	srv.StartTLS()
	t.Cleanup(srv.Close)
	g.srv = srv
	return g
}

func (g *harnessGateway) url() string {
	return "wss" + strings.TrimPrefix(g.srv.URL, "https") + "/agent"
}

// send pushes an already-framed inbound message to the connected agent. It
// blocks until a connection has been accepted (bounded, so a harness that
// never connected fails with a clear message rather than hanging).
func (g *harnessGateway) send(t *testing.T, frame []byte) {
	t.Helper()
	var conn *websocket.Conn
	deadline := time.Now().Add(5 * time.Second)
	for {
		g.mu.Lock()
		conn = g.cur
		g.mu.Unlock()
		if conn != nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("harness gateway: send called but no agent is connected")
		}
		time.Sleep(5 * time.Millisecond)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := conn.Write(ctx, websocket.MessageText, frame); err != nil {
		t.Fatalf("harness gateway: writing an inbound frame: %v", err)
	}
}

// ---------------------------------------------------------------------------
// The end-to-end proof: a job runs through the real transport, not a seam.
// ---------------------------------------------------------------------------

func TestAJobRunsEndToEndThroughTheRealTransport(t *testing.T) {
	h := newConcurrencyHarness(t, harnessOptions{})
	defer h.close()

	id := h.sendJob("hello", nil)

	res := h.awaitResult(t, id, 5*time.Second)
	if got := (struct {
		state string
		id    string
	}{res.State, res.JobID}); got.state != wire.StateSucceeded || got.id != id {
		t.Fatalf("got %+v, want state=%s id=%s", got, wire.StateSucceeded, id)
	}
}

// ---------------------------------------------------------------------------
// The deadlock, reproduced deterministically and made a permanent test.
//
// This promotes Task 1 Step 4's temporary probe (task-1-report.md) into a
// real, always-run test. An earlier draft had the probe block on a channel
// the TEST goroutine closed -- that version would pass with the deadlock
// fully present, because Go unblocks a select on channel close regardless of
// what the read goroutine is doing, so the "reply" would never actually have
// to cross the wire. This version doesn't have that hole: the only path from
// the gateway's reply to probe.query is through this harness's own inbound
// routing (newConcurrencyHarness's `inbound` closure, at the top of this
// file), which runs on the transport's one read goroutine -- the same
// goroutine jobs.Runner.Handle occupies for as long as it blocks.
// ---------------------------------------------------------------------------

// TestACapabilityCanReceiveACorrelatedReplyWhileItRuns is the RED this whole
// cycle exists to turn GREEN.
//
// probe.query calls the real chunkreply.Registry.Query -- the exact
// mechanism backup.run uses to ask the control plane which chunks it already
// has -- and the harness gateway (grantChunkQueries: true) answers the
// resulting backup_chunk_query on its own goroutine, unprompted by the test,
// with a matching wire.ChunkGrant.
//
// That grant can only reach probe.query by crossing the transport's ONE read
// goroutine. Before this task's change to runner.go, Handle occupied that
// goroutine for the whole capability call, so the grant sat unread until the
// deadline -- exactly the live-MinIO failure the task brief describes
// (`dropped a backup chunk grant that nothing was waiting for`). There is no
// way for this test to pass while that is true.
func TestACapabilityCanReceiveACorrelatedReplyWhileItRuns(t *testing.T) {
	var h *concurrencyHarness
	h = newConcurrencyHarness(t, harnessOptions{
		grantChunkQueries: true,
		register: func(reg *capabilities.Registry, _ func(context.Context, any) error) {
			must(reg.Register("probe.query", func(ctx context.Context, j jobs.VerifiedJob) (any, error) {
				g, err := h.replies.Query(ctx, wire.ChunkQuery{JobID: j.ID(), QueryID: "q1"})
				if err != nil {
					return nil, err
				}
				if g.JobID != j.ID() || g.QueryID != "q1" {
					// A correlation mismatch, not a runner defect: the gateway
					// answered the wrong query. Distinguished from a timeout so a
					// future reader can tell the two failure modes apart.
					return nil, fmt.Errorf(
						"probe.query: grant is for job=%s query=%s, want job=%s query=q1",
						g.JobID, g.QueryID, j.ID())
				}
				return map[string]any{"ok": true}, nil
			}))
		},
	})
	defer h.close()
	// Well under chunkreply.DefaultDeadline (60s), and under awaitResult's own
	// bound below, so a genuine failure is reported in seconds, not a minute.
	h.replies.Deadline = 5 * time.Second

	id := h.sendJob("probe.query", nil)

	res := h.awaitResult(t, id, 10*time.Second)
	if res.State != wire.StateSucceeded {
		t.Fatalf("state = %s, want %s: a capability must be able to receive a correlated "+
			"reply while it runs; the read goroutine is blocked inside Handle (result=%v)",
			res.State, wire.StateSucceeded, res.Result)
	}
}

// TestAFullQueueRefusesToTheServerRatherThanTimingOut proves the claim Task 5
// did NOT prove: that a full capability queue's refusal reaches the SERVER as
// a job_result{state: refused, result.reason: ...}, not merely that Submit
// returns ErrQueueFull to its caller. If the overflow job is instead
// accepted, the queue is not bounded where this test assumes it is.
func TestAFullQueueRefusesToTheServerRatherThanTimingOut(t *testing.T) {
	release := make(chan struct{})
	h := newConcurrencyHarness(t, harnessOptions{
		register: func(reg *capabilities.Registry, _ func(context.Context, any) error) {
			must(reg.Register("probe.slow", func(ctx context.Context, _ jobs.VerifiedJob) (any, error) {
				select {
				case <-release:
					return map[string]any{"ok": true}, nil
				case <-ctx.Done():
					return nil, ctx.Err()
				}
			}))
		},
	})
	defer h.close()

	// A queue of depth 8 plus the one job the worker can have in flight means
	// AT MOST NINE of these ten can be accepted, so at least one refusal is
	// guaranteed. WHICH one is refused is not guaranteed, and this test used to
	// assert it was the tenth.
	//
	// It is not, because the worker dequeues concurrently with these
	// submissions: if a slot frees between job nine's arrival and job ten's,
	// nine overflows and ten is accepted into the freed slot. Under CPU
	// contention that happened in 9 of 40 runs, and the old assertion did not
	// merely fail -- it HUNG. Job ten, once accepted, sits behind a capability
	// blocked on `release`, and the old code did not close `release` until
	// after the await returned, so the result it was waiting for could never
	// arrive. A five-second failure that is really a deadlock reads as a slow
	// machine, which is how it survived as a "flake".
	var ids []string
	for i := 0; i < 10; i++ {
		ids = append(ids, h.sendJob("probe.slow", nil))
	}

	want := make(map[string]bool, len(ids))
	for _, id := range ids {
		want[id] = true
	}
	results := make(map[string]wire.JobResult, len(ids))
	collect := func(frame []byte) {
		if kindOf(t, frame) != wire.KindJobResult {
			return
		}
		var r wire.JobResult
		if err := json.Unmarshal(frame, &r); err != nil {
			t.Fatalf("undecodable job_result frame: %v (%s)", err, frame)
		}
		if want[r.JobID] {
			results[r.JobID] = r
		}
	}
	refusedCount := func() int {
		n := 0
		for _, r := range results {
			if r.State == wire.StateRefused {
				n++
			}
		}
		return n
	}

	// Wait for the refusal while every accepted job is STILL BLOCKED. Releasing
	// first would let the worker drain the queue before the last submissions
	// land, so nothing would overflow and this test would pass without ever
	// exercising a full queue.
	first := time.NewTimer(30 * time.Second)
	defer first.Stop()
	for refusedCount() == 0 {
		select {
		case frame := <-h.gateway.outbound:
			collect(frame)
		case <-first.C:
			t.Fatalf("no job was refused within 30s. At most nine of these ten can be accepted "+
				"(one in flight plus a queue of eight), so a refusal is guaranteed; got %d results",
				len(results))
		}
	}

	// Only now: whatever WAS accepted has to be able to finish, or the drain
	// below blocks behind it forever -- the deadlock this test used to hit.
	close(release)

	rest := time.NewTimer(30 * time.Second)
	defer rest.Stop()
	for len(results) < len(ids) {
		select {
		case frame := <-h.gateway.outbound:
			collect(frame)
		case <-rest.C:
			var missing []string
			for _, id := range ids {
				if _, ok := results[id]; !ok {
					missing = append(missing, id)
				}
			}
			t.Fatalf("no job_result arrived within 30s for %d of %d jobs: %v. Every submitted job "+
				"must be answered -- accepted or refused -- rather than left to time out",
				len(missing), len(ids), missing)
		}
	}

	// The COUNT is bounded on both sides, not just "at least one".
	//
	// "At least one was refused" would also pass if all ten were, and a queue
	// that refuses everything is a broken queue that this test would have
	// called healthy. The real arithmetic: a refusal happens only when the
	// queue (8) is full, so a refusal implies at least 8 accepted; and at most
	// 9 can be accepted (8 queued plus 1 in flight). With ten submissions that
	// is one or two refusals -- two when the worker has not yet dequeued its
	// first job by the time submissions 9 and 10 arrive, one when it has.
	if n := refusedCount(); n < 1 || n > 2 {
		t.Errorf("%d of %d jobs were refused, want 1 or 2. A queue of 8 plus one job in flight "+
			"accepts between 8 and 9 of these ten; anything else means the bound is not where "+
			"this test assumes it is.", n, len(ids))
	}

	// The refusal must carry a reason: a `refused` with an empty reason tells
	// the operator a job was rejected and nothing about why.
	for _, id := range ids {
		r := results[id]
		if r.State != wire.StateRefused {
			continue
		}
		if reason, _ := r.Result["reason"].(string); reason == "" {
			t.Errorf("job %s = state %s with an empty reason, want a stated one", id, r.State)
		}
	}
}

// ---------------------------------------------------------------------------
// Panic containment: the recover that used to sit on the transport's read
// goroutine now has to survive the move onto a Dispatcher worker.
// ---------------------------------------------------------------------------

// TestAPanickingCapabilityFailsItsJobAndTheAgentKeepsServing is the guard for
// what Task 6's move put at risk. Runner.invoke's recover reaches the worker
// goroutine because the Dispatcher's Exec is r.run, which calls invoke, and
// it is what turns a panic into a `failed` job_result. If a future change
// routes execution around run, invoke's recover no longer runs, and the
// capability panics with only Dispatcher.exec1's own, independent recover
// left to catch it (dispatcher.go) -- which logs and returns, sending NO
// job_result at all. The failure mode is not a crash (exec1's recover
// already prevents that); it is a SILENT HANG: the job sits `running` until
// the control plane expires it, with no local signal beyond a log line an
// operator was not watching. That is worse to diagnose than an outright
// crash would be, which is why this guards it explicitly rather than relying
// on exec1's backstop alone.
//
// This is not a hypothetical: Step 5 of this test's own implementation
// (task-7-report.md) falsified it by removing invoke's recover, and observed
// exactly this -- the first awaitResult below timed out after 5s instead of
// seeing `failed`, with no process crash.
//
// In the healthy case, both assertions below run: the first confirms the
// panicking job reports `failed` (invoke's recover did its job); the second
// -- sent through the SAME runner and the SAME transport connection, not a
// fresh one -- confirms the agent is still serving afterward. Sending the
// second job is what makes this a containment test rather than only a
// failed-job test. But under the falsified configuration described above,
// the first assertion is what goes red, via t.Fatalf, which halts the test
// before the second job is ever sent -- so the falsification run confirmed
// property 1 (the job reports failed) regresses; it observed nothing
// directly about property 2 (the agent keeps serving) under that same
// regression. Property 2's behaviour there -- the agent survives, because
// exec1's recover is a second, independent barrier -- is established by
// reading dispatcher.go, not by anything this test saw fail.
func TestAPanickingCapabilityFailsItsJobAndTheAgentKeepsServing(t *testing.T) {
	h := newConcurrencyHarness(t, harnessOptions{
		register: func(reg *capabilities.Registry, _ func(context.Context, any) error) {
			must(reg.Register("probe.panic", func(context.Context, jobs.VerifiedJob) (any, error) {
				panic("deliberate")
			}))
		},
	})
	defer h.close()

	bad := h.sendJob("probe.panic", nil)
	if res := h.awaitResult(t, bad, 5*time.Second); res.State != wire.StateFailed {
		t.Fatalf("panicking job state = %s, want %s", res.State, wire.StateFailed)
	}

	// The agent must still be alive and serving.
	good := h.sendJob("hello", nil)
	if res := h.awaitResult(t, good, 5*time.Second); res.State != wire.StateSucceeded {
		t.Fatalf("next job after a panic = %s, want %s", res.State, wire.StateSucceeded)
	}
}

// ---------------------------------------------------------------------------
// A disconnect must not cancel work that is already running.
//
// This is the test the concurrency change owed and did not have. While
// Runner.Handle blocked for the whole capability, the context a job ran under
// could safely be the transport's per-connection one: transport.serve could
// not return -- and so could not cancel it -- until the job was finished.
// Moving execution onto a Dispatcher worker broke that coupling silently. The
// job outlived Handle, the connection context did not, and every running
// capability was cancelled by the next reconnect: a ten-second blip aborting a
// forty-minute patch.apply.
//
// The design doc's error table asserts the opposite -- "Disconnect | Running
// and queued jobs continue; results buffer via the outbox"
// (docs/superpowers/specs/2026-08-09-agent-job-concurrency-design.md) -- and
// nothing measured it. This does.
// ---------------------------------------------------------------------------

// TestAJobSurvivesADisconnectAndItsResultArrivesAfterReconnect drops the
// gateway connection out from under running capabilities and requires that
// they finish normally, that a job waiting on the heavy budget still runs, and
// that all three answers reach the control plane over the reconnected socket.
//
// The three job types key to three DIFFERENT members of jobs.DefaultHeavyKeys
// (patch, backup, app), which is load-bearing rather than decorative. With a
// heavy budget of 2, two of them execute and the third's worker parks on the
// budget semaphore inside Dispatcher.exec1 -- and that is where the worse of
// the two failure modes lived. Under a connection-scoped context, exec1's
// `<-ctx.Done()` arm fired the moment the socket dropped and returned having
// sent NOTHING: no result, no refusal, no status, so the control plane saw
// only the job's own expiry with no stated cause. A test that watched only a
// running job would never reach it.
//
// Releasing only after the reconnect is what proves survival: a job that
// answers here has outlived the connection it arrived on.
//
// FALSIFIED, not assumed: reverting Submit/exec1 to the connection context
// (the pre-fix shape) fails this test -- see the report for the observed
// output, including the queued heavy job that produced no job_result at all.
//
// SECOND OCCURRENCE, 2026-09-08, AND THIS IS THE DATA THE INSTRUMENTATION WAS
// ADDED FOR. `awaitResults`'s comment says the next occurrence is the only
// chance to see why; it came, on a GitHub runner, on a commit touching no Go
// at all (a frontend-only change on the v3 port branch), and it printed:
//
//	reconnect completed in 1.231713341s (connection generation 1 -> 2)
//	no job_result arrived within 15s for 1 of 3 jobs:
//	  [9c3f0a11-conc-4000-8000-000000000001
//	   (a job_status WAS seen for it (it started, but never reported a result))];
//	  drained 5 frames total while waiting, by kind:
//	  map[job_result:2 job_status:3]
//
// So: all three started, two answered, and the third was still running when
// the 15 s ran out. That is NOT the failure mode this test was built to catch
// -- exec1's `<-ctx.Done()` arm returning silently produced no job_status
// either, and here there were three. The reconnect itself took 1.2 s of the
// budget.
//
// NOT RESOLVED BY WIDENING THE DEADLINE, deliberately. That run's other
// packages took five to ten times their usual wall clock (backup 16 s against
// 1.7 s, packaging 44 s), so a loaded runner is the obvious reading -- but
// "obvious" is not "measured", and 20 local repetitions, 8 of them under
// 4-way CPU contention on a 4-core box, did not reproduce it. Raising 15 s to
// a number that would have passed THAT run would destroy the only signal this
// test has ever produced about the property it guards, which is that a
// dropped connection must not strand a running job. The deadline stays; a
// third occurrence with this data is what would separate "slow" from "stuck".
func TestAJobSurvivesADisconnectAndItsResultArrivesAfterReconnect(t *testing.T) {
	started := make(chan struct{}, 3)
	release := make(chan struct{})
	closeRelease := sync.OnceFunc(func() { close(release) })
	defer closeRelease()

	// One handler, three heavy job types. jobs.DefaultHeavyKeys is
	// {backup, patch, app, script}; capabilityKey takes everything before the
	// first '.', so these are three distinct heavy keys and therefore three
	// distinct Dispatcher workers competing for a budget of 2.
	heavy := []string{"patch.probe", "backup.probe", "app.probe"}
	h := newConcurrencyHarness(t, harnessOptions{
		register: func(reg *capabilities.Registry, _ func(context.Context, any) error) {
			for _, jt := range heavy {
				must(reg.Register(jt, func(ctx context.Context, _ jobs.VerifiedJob) (any, error) {
					started <- struct{}{}
					select {
					case <-release:
						return map[string]any{"ok": true}, nil
					case <-ctx.Done():
						// The whole point: a disconnect must not land here.
						// Named so a failure says WHICH context ended the job,
						// rather than only that the job did not succeed.
						return nil, fmt.Errorf("the job's context was cancelled while it ran: %w", ctx.Err())
					}
				}))
			}
		},
	})
	defer h.close()

	var ids []string
	for _, jt := range heavy {
		ids = append(ids, h.sendJob(jt, nil))
	}

	// Two must actually be INSIDE the capability before the drop -- otherwise
	// this would be testing a disconnect against an idle agent, and the third
	// would not yet be parked on the budget semaphore.
	for i := 0; i < 2; i++ {
		select {
		case <-started:
		case <-time.After(5 * time.Second):
			t.Fatalf("only %d of 2 heavy jobs started; there was nothing running to disconnect", i)
		}
	}

	gen := h.gateway.gen()
	h.gateway.drop(t)
	dropTime := time.Now()
	h.gateway.awaitReconnect(t, gen, 15*time.Second)
	// awaitReconnect above would have called t.Fatalf if the reconnect did not
	// complete within 15s, so reaching this line already proves it completed;
	// this records how long it took. Logged rather than asserted -- t.Logf
	// output is captured and printed alongside a later failure in this same
	// test (including one from awaitResults below), so if the eventual missing
	// job_result correlates with a slow reconnect, that shows up here instead
	// of requiring a reproduction to find out.
	t.Logf("reconnect completed in %s (connection generation %d -> %d)", time.Since(dropTime), gen, h.gateway.gen())

	// Released only after the reconnect, so a job that finishes has provably
	// outlived the connection it arrived on. The third job cannot even start
	// until one of the first two frees its budget slot.
	closeRelease()

	results := h.awaitResults(t, ids, 15*time.Second)
	for i, id := range ids {
		if res := results[id]; res.State != wire.StateSucceeded {
			t.Fatalf("job %d (%s, %s) = %s, want %s: a dropped connection must not cancel a running "+
				"or budget-queued job (result=%v)", i, heavy[i], id, res.State, wire.StateSucceeded, res.Result)
		}
	}
}
