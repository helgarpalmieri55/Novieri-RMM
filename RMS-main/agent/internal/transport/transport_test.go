package transport

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"runtime/pprof"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// ---------------------------------------------------------------------------
// A CA, a server cert and a client cert, so the test exercises REAL mutual TLS.
//
// The gateway's whole admission model is "the certificate is the identity"
// (app/gateway/main.py:174-186). A transport test that stubs TLS out would pass
// against a transport that never presented a certificate at all.
// ---------------------------------------------------------------------------

type testPKI struct {
	caCert   *x509.Certificate
	caKey    ed25519.PrivateKey
	caPool   *x509.CertPool
	server   tls.Certificate
	client   tls.Certificate
	otherCA  *x509.CertPool
	notAfter time.Time
}

func newTestPKI(t *testing.T) *testPKI {
	t.Helper()
	caCert, caKey := selfSignedCA(t, "its-test-ca")
	pool := x509.NewCertPool()
	pool.AddCert(caCert)

	otherCert, _ := selfSignedCA(t, "some-other-ca")
	other := x509.NewCertPool()
	other.AddCert(otherCert)

	notAfter := time.Now().Add(30 * 24 * time.Hour)
	return &testPKI{
		caCert:   caCert,
		caKey:    caKey,
		caPool:   pool,
		server:   issue(t, caCert, caKey, "gateway", []net.IP{net.ParseIP("127.0.0.1")}, x509.ExtKeyUsageServerAuth, notAfter),
		client:   issue(t, caCert, caKey, "device", nil, x509.ExtKeyUsageClientAuth, notAfter),
		otherCA:  other,
		notAfter: notAfter,
	}
}

func selfSignedCA(t *testing.T, cn string) (*x509.Certificate, ed25519.PrivateKey) {
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

func issue(
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

// ---------------------------------------------------------------------------
// A fake gateway: mutual TLS 1.3 + a coder/websocket accept handler, exactly
// the shape app/gateway/main.py::GatewayServer.start builds.
// ---------------------------------------------------------------------------

type fakeGateway struct {
	srv      *httptest.Server
	accepted atomic.Int64 // handler invocations: 0 proves the dial failed BEFORE any frame
	received chan []byte
	// onAccept runs for each accepted connection. Return a close code to close
	// with it; 0 means "serve until the client goes away".
	onAccept func(t *testing.T, conn *websocket.Conn) websocket.StatusCode
}

func newFakeGateway(t *testing.T, pki *testPKI) *fakeGateway {
	t.Helper()
	g := &fakeGateway{received: make(chan []byte, 64)}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		g.accepted.Add(1)
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		defer conn.CloseNow() //nolint:errcheck
		if g.onAccept != nil {
			if code := g.onAccept(t, conn); code != 0 {
				_ = conn.Close(code, "test")
				return
			}
		}
		for {
			_, b, err := conn.Read(r.Context())
			if err != nil {
				return
			}
			select {
			case g.received <- b:
			default:
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

func (g *fakeGateway) url() string { return "wss" + strings.TrimPrefix(g.srv.URL, "https") + "/agent" }

func (pki *testPKI) options(url string) Options {
	return Options{
		GatewayURL: url,
		ClientCert: pki.client,
		RootCAs:    pki.caPool,
	}
}

// ---------------------------------------------------------------------------
// Clocks
// ---------------------------------------------------------------------------

// recordClock returns from Sleep immediately and records what it was asked to
// wait for. The backoff assertions are therefore on OBSERVED DURATIONS, not on
// wall-clock elapsed time, and the test runs in milliseconds.
type recordClock struct {
	mu     sync.Mutex
	now    time.Time
	slept  []time.Duration
	onWake func(n int) error // optional: stop the loop after n sleeps
}

func (c *recordClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *recordClock) Sleep(_ context.Context, d time.Duration) error {
	c.mu.Lock()
	c.slept = append(c.slept, d)
	c.now = c.now.Add(d)
	n := len(c.slept)
	wake := c.onWake
	c.mu.Unlock()
	if wake != nil {
		return wake(n)
	}
	return nil
}

func (c *recordClock) delays() []time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]time.Duration(nil), c.slept...)
}

// gateClock blocks in Sleep until the test releases it, which is how the
// heartbeat test fires a timer without waiting a minute.
type gateClock struct {
	mu   sync.Mutex
	now  time.Time
	gate chan time.Duration
}

func newGateClock() *gateClock {
	return &gateClock{now: time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC), gate: make(chan time.Duration)}
}

func (c *gateClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *gateClock) Sleep(ctx context.Context, d time.Duration) error {
	select {
	case c.gate <- d:
		c.mu.Lock()
		c.now = c.now.Add(d)
		c.mu.Unlock()
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// advance releases exactly one pending Sleep and reports what it asked for.
func (c *gateClock) advance(t *testing.T) time.Duration {
	t.Helper()
	select {
	case d := <-c.gate:
		return d
	case <-time.After(2 * time.Second):
		t.Fatal("no goroutine was waiting on the clock; a timer-driven agent should be parked in Sleep")
		return 0
	}
}

// ---------------------------------------------------------------------------
// Dial
// ---------------------------------------------------------------------------

func TestDialWithTheClientCertificateReceivesAPushedFrame(t *testing.T) {
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)
	g.onAccept = func(t *testing.T, conn *websocket.Conn) websocket.StatusCode {
		_ = conn.Write(context.Background(), websocket.MessageText,
			[]byte(`{"v":1,"kind":"job","job":{"id":"j1","type":"hello","device":"d1","params":{},`+
				`"issued_at":"2026-08-05T12:00:00Z","expires_at":"2026-08-05T12:05:00Z","nonce":"n1"},"sig":"AA=="}`))
		return 0
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	conn, err := Dial(ctx, pki.options(g.url()))
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()

	raw, err := conn.Recv(ctx)
	if err != nil {
		t.Fatalf("Recv: %v", err)
	}
	msg, err := wire.Decode(raw)
	if err != nil {
		t.Fatalf("the frame the gateway pushed did not decode: %v", err)
	}
	env, ok := msg.(*wire.Envelope)
	if !ok {
		t.Fatalf("got %T, want *wire.Envelope", msg)
	}
	if env.Job.ID != "j1" {
		t.Fatalf("job id = %q, want j1", env.Job.ID)
	}
}

func TestDialWithoutAClientCertificateFails(t *testing.T) {
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)

	opts := pki.options(g.url())
	opts.ClientCert = tls.Certificate{} // no identity at all

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	conn, err := Dial(ctx, opts)
	if err == nil {
		conn.Close()
		t.Fatal("dialled a RequireAndVerifyClientCert gateway with no client certificate")
	}
}

func TestDialRejectsAGatewayCertificateOutsideThePinnedPool(t *testing.T) {
	// Pinning is not advisory: the failure must happen in the TLS handshake,
	// so the gateway's HTTP handler never runs and no frame can have been
	// exchanged. `g.accepted == 0` is that observation; "an error was returned"
	// would also be satisfied by an agent that read a frame and then complained.
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)

	opts := pki.options(g.url())
	opts.RootCAs = pki.otherCA

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	conn, err := Dial(ctx, opts)
	if err == nil {
		conn.Close()
		t.Fatal("dialled a gateway whose certificate is not in the pinned pool")
	}
	if n := g.accepted.Load(); n != 0 {
		t.Fatalf("the gateway handler ran %d times; the dial must fail in the TLS handshake, before any frame", n)
	}
}

func TestDialRefusesAPlaintextGatewayURL(t *testing.T) {
	// docs/03-security-model.md: agents connect outbound over MUTUAL TLS. A
	// gateway_url that arrived as ws:// or http:// would silently drop both the
	// server pinning and the client certificate, and nothing downstream would
	// notice: an unauthenticated socket looks exactly like an authenticated one
	// once it is open.
	for _, u := range []string{"ws://127.0.0.1:8443/agent", "http://127.0.0.1:8443/agent", "127.0.0.1:8443"} {
		t.Run(u, func(t *testing.T) {
			pki := newTestPKI(t)
			opts := pki.options(u)
			if _, err := Dial(context.Background(), opts); !errors.Is(err, ErrInsecureGatewayURL) {
				t.Fatalf("Dial(%q) error = %v, want ErrInsecureGatewayURL", u, err)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Backoff
// ---------------------------------------------------------------------------

func TestBackoffDelayIsMinCapBaseTimesTwoToTheN(t *testing.T) {
	b := Backoff{Base: time.Second, Max: 30 * time.Second}
	cases := []struct {
		attempt int
		want    time.Duration
	}{
		{0, time.Second},
		{1, 2 * time.Second},
		{2, 4 * time.Second},
		{3, 8 * time.Second},
		{4, 16 * time.Second},
		{5, 30 * time.Second}, // capped
		{6, 30 * time.Second},
		{62, 30 * time.Second}, // shift overflow must cap, not wrap negative
		{200, 30 * time.Second},
	}
	for _, tc := range cases {
		// jitter 0.5 is the identity factor: 0.5 + 0.5 == 1.0.
		if got := b.Delay(tc.attempt, 0.5); got != tc.want {
			t.Errorf("Delay(%d, 0.5) = %v, want %v", tc.attempt, got, tc.want)
		}
	}
}

func TestBackoffJitterStaysInsideHalfToOneAndAHalf(t *testing.T) {
	b := Backoff{Base: time.Second, Max: 30 * time.Second}
	for attempt := range 8 {
		base := b.Delay(attempt, 0.5)
		for _, j := range []float64{0, 0.001, 0.25, 0.5, 0.75, 0.999} {
			got := b.Delay(attempt, j)
			lo := time.Duration(float64(base) * 0.5)
			hi := time.Duration(float64(base) * 1.5)
			if got < lo || got > hi {
				t.Fatalf("Delay(%d, %v) = %v, outside [%v, %v]", attempt, j, got, lo, hi)
			}
		}
	}
}

func TestRunReconnectsWithExponentialBackoffOnTheInjectedClock(t *testing.T) {
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)
	// Every connection is dropped the moment it is accepted, so no connection
	// ever reaches ResetAfter and the attempt counter keeps climbing.
	g.onAccept = func(_ *testing.T, _ *websocket.Conn) websocket.StatusCode {
		return websocket.StatusNormalClosure
	}

	stop := errors.New("enough")
	clock := &recordClock{now: time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	clock.onWake = func(n int) error {
		if n >= 6 {
			cancel()
			return stop
		}
		return nil
	}

	opts := pki.options(g.url())
	opts.Clock = clock
	opts.Jitter = func() float64 { return 0.5 }
	opts.Backoff = Backoff{Base: time.Second, Max: 30 * time.Second}
	opts.HeartbeatEvery = 0

	_ = Run(ctx, opts, func(context.Context, []byte) {})

	want := []time.Duration{
		time.Second, 2 * time.Second, 4 * time.Second,
		8 * time.Second, 16 * time.Second, 30 * time.Second,
	}
	got := clock.delays()
	if len(got) < len(want) {
		t.Fatalf("recorded %v, want at least %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("backoff delays = %v, want prefix %v", got, want)
		}
	}
}

func TestRunResetsBackoffAfterAConnectionThatLived(t *testing.T) {
	// The reason the counter is NOT reset on a bare successful dial: a gateway
	// that accepts and immediately drops would then be redialled every Base
	// forever. Reset is earned by staying up for ResetAfter.
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)

	clock := &recordClock{now: time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)}
	var conns atomic.Int64
	g.onAccept = func(_ *testing.T, _ *websocket.Conn) websocket.StatusCode {
		// Second and later connections "lived" a long time: push the injected
		// clock forward past ResetAfter before the drop.
		if conns.Add(1) >= 2 {
			clock.mu.Lock()
			clock.now = clock.now.Add(10 * time.Minute)
			clock.mu.Unlock()
		}
		return websocket.StatusNormalClosure
	}

	stop := errors.New("enough")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	clock.onWake = func(n int) error {
		if n >= 3 {
			cancel()
			return stop
		}
		return nil
	}

	opts := pki.options(g.url())
	opts.Clock = clock
	opts.Jitter = func() float64 { return 0.5 }
	opts.Backoff = Backoff{Base: time.Second, Max: 30 * time.Second}
	opts.ResetAfter = time.Minute
	opts.HeartbeatEvery = 0

	_ = Run(ctx, opts, func(context.Context, []byte) {})

	got := clock.delays()
	// attempt 0 -> 1s; then a long-lived connection resets, so the next two are
	// 1s again rather than 2s and 4s.
	want := []time.Duration{time.Second, time.Second, time.Second}
	if len(got) < 3 {
		t.Fatalf("recorded %v, want at least 3 delays", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("delays = %v, want prefix %v (the reset did not happen)", got, want)
		}
	}
}

func TestRevocationCloseCodesAreTerminal(t *testing.T) {
	// A revoked agent that reconnects forever is a denial-of-service against
	// its own control plane, and it is the exact behaviour a naive reconnect
	// loop produces. The observation is the DIAL COUNT, not the returned error:
	// an agent that returns the right error and reconnects anyway would pass an
	// error-only assertion.
	cases := []struct {
		name string
		code websocket.StatusCode
		want error
	}{
		{"4401 unauthorized", 4401, ErrUnauthorized},
		{"4403 revoked", 4403, ErrRevoked},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			pki := newTestPKI(t)
			g := newFakeGateway(t, pki)
			g.onAccept = func(_ *testing.T, _ *websocket.Conn) websocket.StatusCode { return tc.code }

			clock := &recordClock{now: time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)}
			opts := pki.options(g.url())
			opts.Clock = clock
			opts.Jitter = func() float64 { return 0.5 }
			opts.HeartbeatEvery = 0

			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			err := Run(ctx, opts, func(context.Context, []byte) {})
			if !errors.Is(err, tc.want) {
				t.Fatalf("Run error = %v, want %v", err, tc.want)
			}
			if n := g.accepted.Load(); n != 1 {
				t.Fatalf("the gateway was dialled %d times after a %d close; %d must be terminal", n, tc.code, tc.code)
			}
			if d := clock.delays(); len(d) != 0 {
				t.Fatalf("slept %v before giving up; a terminal close must not back off and retry", d)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Heartbeat
// ---------------------------------------------------------------------------

func TestHeartbeatIsTimerDrivenAndCarriesWholeSecondRFC3339(t *testing.T) {
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)

	clock := newGateClock()
	opts := pki.options(g.url())
	opts.Clock = clock
	opts.HeartbeatEvery = 60 * time.Second

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(ctx, opts, func(context.Context, []byte) {}) }()

	// The agent must be PARKED in Sleep, not spinning: advance() fails if
	// nothing is waiting on the clock.
	if d := clock.advance(t); d != 60*time.Second {
		t.Fatalf("heartbeat timer asked for %v, want 60s", d)
	}

	select {
	case raw := <-g.received:
		var got map[string]any
		if err := json.Unmarshal(raw, &got); err != nil {
			t.Fatalf("heartbeat is not JSON: %v (%s)", err, raw)
		}
		want := map[string]any{"v": float64(1), "kind": "heartbeat", "ts": "2026-08-05T12:01:00Z"}
		if len(got) != len(want) {
			t.Fatalf("heartbeat key set = %v, want exactly %v (the server is extra=\"forbid\")", got, want)
		}
		for k, v := range want {
			if got[k] != v {
				t.Fatalf("heartbeat[%q] = %v, want %v (raw %s)", k, got[k], v, raw)
			}
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no heartbeat arrived after the timer fired")
	}
	cancel()
	<-done
}

// ---------------------------------------------------------------------------
// Idle: one parked read, no polling
// ---------------------------------------------------------------------------

// countingDialer wraps the TLS connection so the test can count Read RETURNS.
// A blocking read issues one Read that never returns while idle; a polling
// loop returns from Read continuously. That difference is the observation.
type countingConn struct {
	net.Conn
	returns *atomic.Int64
}

func (c *countingConn) Read(b []byte) (int, error) {
	n, err := c.Conn.Read(b)
	c.returns.Add(1)
	return n, err
}

func TestIdleTransportParksOnOneReadAndDoesNotPoll(t *testing.T) {
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)

	var reads atomic.Int64
	opts := pki.options(g.url())
	opts.HeartbeatEvery = 0
	opts.dialTLS = func(ctx context.Context, cfg *tls.Config, network, addr string) (net.Conn, error) {
		d := &tls.Dialer{Config: cfg}
		c, err := d.DialContext(ctx, network, addr)
		if err != nil {
			return nil, err
		}
		return &countingConn{Conn: c, returns: &reads}, nil
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	conn, err := Dial(ctx, opts)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()

	recvDone := make(chan struct{})
	go func() {
		defer close(recvDone)
		_, _ = conn.Recv(ctx)
	}()

	time.Sleep(100 * time.Millisecond) // let the handshake reads settle
	readsBefore := reads.Load()
	goroutinesBefore := transportGoroutines(t)

	time.Sleep(200 * time.Millisecond) // the idle window

	if delta := reads.Load() - readsBefore; delta != 0 {
		t.Fatalf("%d socket reads returned during a 200ms idle window; the read must be BLOCKING, not polled", delta)
	}
	if after := transportGoroutines(t); after != goroutinesBefore {
		t.Fatalf("transport goroutines went %d -> %d while idle; nothing may accumulate on an idle agent",
			goroutinesBefore, after)
	}
	if goroutinesBefore == 0 {
		t.Fatal("no transport goroutine was parked; the profile marker is wrong and this test proves nothing")
	}

	cancel()
	conn.Close()
	<-recvDone
}

// transportGoroutines counts goroutine stacks that mention this package.
// runtime.NumGoroutine() would also count the httptest server's, which is
// noise the assertion cannot afford.
func transportGoroutines(t *testing.T) int {
	t.Helper()
	var buf bytes.Buffer
	if err := pprof.Lookup("goroutine").WriteTo(&buf, 2); err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, stack := range strings.Split(buf.String(), "\n\n") {
		if strings.Contains(stack, "it-suite/agent/internal/transport.") {
			n++
		}
	}
	return n
}

// ---------------------------------------------------------------------------
// Send
// ---------------------------------------------------------------------------

func TestSendFramesThroughWireEncodeAndRefusesInboundTypes(t *testing.T) {
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	conn, err := Dial(ctx, pki.options(g.url()))
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()

	if err := conn.Send(ctx, wire.JobStatus{JobID: "j1"}); err != nil {
		t.Fatalf("Send: %v", err)
	}
	select {
	case raw := <-g.received:
		if want := `{"v":1,"kind":"job_status","job_id":"j1","state":"running"}`; string(raw) != want {
			t.Fatalf("sent %s, want %s", raw, want)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("nothing arrived at the gateway")
	}

	// A type wire.Encode does not recognise must be refused here, not framed
	// as something the server will silently drop at parse_agent_message.
	if err := conn.Send(ctx, struct{ X int }{1}); err == nil {
		t.Fatal("Send framed a type that is not a declared outbound message")
	}
	if err := conn.Send(ctx, &wire.Envelope{}); err == nil {
		t.Fatal("Send framed a SERVER->AGENT envelope; that direction does not exist")
	}
}

func TestConcurrentSendsDoNotRaceTheWebsocketWriter(t *testing.T) {
	// coder/websocket forbids concurrent writers. The transport owns a single
	// writer goroutine for exactly that reason; this test is what makes a
	// regression show up under -race instead of in the field.
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	conn, err := Dial(ctx, pki.options(g.url()))
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()

	var wg sync.WaitGroup
	for i := range 16 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_ = conn.Send(ctx, wire.JobLog{JobID: "j1", Msg: "x", Level: wire.LevelInfo})
			_ = i
		}()
	}
	wg.Wait()
	for range 16 {
		select {
		case <-g.received:
		case <-time.After(5 * time.Second):
			t.Fatal("a concurrently queued message was lost")
		}
	}
}

func TestOnConnectRunsOncePerConnectionAndNotPerFrame(t *testing.T) {
	// Task 6 hangs the certificate-expiry check on this, so "once per connect"
	// has to be a fact rather than an assumption: a hook that fired per frame
	// would put a WARN in the log for every job on an expiring endpoint, and a
	// hook that fired once per process would never warn at all on a
	// long-running agent whose certificate ages out under it.
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)
	g.onAccept = func(_ *testing.T, conn *websocket.Conn) websocket.StatusCode {
		for range 3 {
			_ = conn.Write(context.Background(), websocket.MessageText, []byte(`{"v":1,"kind":"nope"}`))
		}
		return websocket.StatusNormalClosure
	}

	var hooks atomic.Int64
	clock := &recordClock{now: time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	stop := errors.New("enough")
	clock.onWake = func(n int) error {
		if n >= 3 {
			cancel()
			return stop
		}
		return nil
	}

	opts := pki.options(g.url())
	opts.Clock = clock
	opts.Jitter = func() float64 { return 0.5 }
	opts.HeartbeatEvery = 0
	opts.OnConnect = func(context.Context) { hooks.Add(1) }

	_ = Run(ctx, opts, func(context.Context, []byte) {})

	connects := g.accepted.Load()
	if connects < 3 {
		t.Fatalf("only %d connections; the test proves nothing", connects)
	}
	if got := hooks.Load(); got != connects {
		t.Fatalf("OnConnect ran %d times across %d connections (and 3 frames each)", got, connects)
	}
}

// ---------------------------------------------------------------------------
// Certificate rotation hooks (docs/modules/agent/cert-renewal.md §4.3, §4.4)
// ---------------------------------------------------------------------------

func TestTheClientCertificateIsReReadOnEveryDial(t *testing.T) {
	// §4.3. `agent.cert.install` swaps the identity ON DISK while a connection
	// is up, and the live connection is deliberately not re-keyed. If Dial kept
	// using the certificate captured when Run started, a rotated endpoint would
	// keep presenting the retired certificate forever -- and once the control
	// plane flipped its gate, that is a device that never comes back.
	//
	// FALSIFIER: the call count. A hook consulted once would read 1 here.
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)
	var calls atomic.Int64
	g.onAccept = func(_ *testing.T, _ *websocket.Conn) websocket.StatusCode {
		return websocket.StatusNormalClosure
	}

	clock := &recordClock{now: time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)}
	clock.onWake = func(n int) error {
		if n >= 3 {
			return context.Canceled
		}
		return nil
	}
	opts := pki.options(g.url())
	opts.ClientCert = tls.Certificate{} // must NOT be used
	opts.ClientCertFn = func() (tls.Certificate, error) {
		calls.Add(1)
		return pki.client, nil
	}
	opts.Clock = clock
	opts.Jitter = func() float64 { return 0.5 }
	opts.HeartbeatEvery = 0

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = Run(ctx, opts, func(context.Context, []byte) {})

	if n := calls.Load(); n < 3 {
		t.Fatalf("ClientCertFn was consulted %d times across %d dials; it must be read per dial",
			n, g.accepted.Load())
	}
	if g.accepted.Load() == 0 {
		t.Fatal("the certificate the hook returned did not produce a mutual-TLS connection")
	}
}

func TestADialWhoseCertificateCannotBeReadFailsRatherThanFallingBack(t *testing.T) {
	// Silently presenting the previously captured certificate would be the
	// worst outcome: it can succeed, so the operator sees a working agent while
	// the identity on disk is unreadable. FALSIFIER: a dial that is accepted.
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)
	opts := pki.options(g.url())
	opts.ClientCertFn = func() (tls.Certificate, error) {
		return tls.Certificate{}, errors.New("identity.json is gone")
	}
	if _, err := Dial(context.Background(), opts); err == nil {
		t.Fatal("Dial succeeded with no readable client certificate")
	} else if !strings.Contains(err.Error(), "identity.json is gone") {
		t.Fatalf("the underlying reason was swallowed: %v", err)
	}
	if n := g.accepted.Load(); n != 0 {
		t.Fatalf("the gateway accepted %d connections; the dial must not fall back", n)
	}
}

func TestAFourFourZeroOneIsTerminalWhenNothingCanRecoverIt(t *testing.T) {
	// The default is unchanged and must stay unchanged: a nil hook, or a hook
	// that reports it could not recover, means 4401 ends the process exactly as
	// it always did. That is what stops a fleet whose certificates have expired
	// becoming a retry storm.
	for _, tc := range []struct {
		name string
		hook func(context.Context) bool
	}{
		{"no hook at all", nil},
		{"hook with nothing to revert to", func(context.Context) bool { return false }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			pki := newTestPKI(t)
			g := newFakeGateway(t, pki)
			g.onAccept = func(_ *testing.T, _ *websocket.Conn) websocket.StatusCode { return 4401 }

			clock := &recordClock{now: time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)}
			opts := pki.options(g.url())
			opts.OnUnauthorized = tc.hook
			opts.Clock = clock
			opts.Jitter = func() float64 { return 0.5 }
			opts.HeartbeatEvery = 0

			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			if err := Run(ctx, opts, func(context.Context, []byte) {}); !errors.Is(err, ErrUnauthorized) {
				t.Fatalf("Run error = %v, want ErrUnauthorized", err)
			}
			if n := g.accepted.Load(); n != 1 {
				t.Fatalf("the gateway was dialled %d times after a 4401; it must be terminal", n)
			}
		})
	}
}

func TestARecoverableFourFourZeroOneIsRetriedExactlyAsOftenAsTheHookAllows(t *testing.T) {
	// §4.4, the lost-acknowledgement case. The hook reports a successful revert
	// ONCE -- which is all the agent's implementation can ever do, because a
	// revert consumes previous.json -- and the loop must dial again and then
	// stop.
	//
	// FALSIFIER: the DIAL COUNT. A `return true` that did not actually retry
	// would read 1; a retry that ignored the hook's second `false` would read
	// more than 2 and would be the retry storm this design must not have.
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)
	g.onAccept = func(_ *testing.T, _ *websocket.Conn) websocket.StatusCode { return 4401 }

	var reverts atomic.Int64
	clock := &recordClock{now: time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)}
	opts := pki.options(g.url())
	opts.OnUnauthorized = func(context.Context) bool { return reverts.Add(1) == 1 }
	opts.Clock = clock
	opts.Jitter = func() float64 { return 0.5 }
	opts.HeartbeatEvery = 0

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := Run(ctx, opts, func(context.Context, []byte) {}); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("Run error = %v, want ErrUnauthorized", err)
	}
	if n := g.accepted.Load(); n != 2 {
		t.Fatalf("the gateway was dialled %d times; want exactly 2 (one revert, one retry)", n)
	}
	if n := reverts.Load(); n != 2 {
		t.Fatalf("the hook was consulted %d times; want 2", n)
	}
}

// A caller holding per-connection state must learn when that connection dies.
func TestOnDisconnectFiresWhenTheConnectionDrops(t *testing.T) {
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)
	// Same pattern as the reconnect/backoff tests above: the gateway accepts
	// and immediately closes, which is a real connection loss from the
	// client's point of view -- CloseClientConnections is not reliable here
	// because coder/websocket hijacks the underlying net.Conn on accept, and
	// httptest.Server stops tracking (and so cannot forcibly close) a
	// hijacked connection.
	g.onAccept = func(_ *testing.T, _ *websocket.Conn) websocket.StatusCode {
		return websocket.StatusNormalClosure
	}

	var mu sync.Mutex
	calls := 0

	opts := pki.options(g.url())
	opts.HeartbeatEvery = 0
	opts.OnDisconnect = func(context.Context, error) {
		mu.Lock()
		calls++
		mu.Unlock()
	}

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		_ = Run(ctx, opts, func(context.Context, []byte) {})
	}()

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := calls
		mu.Unlock()
		if n > 0 {
			cancel()
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	t.Fatal("OnDisconnect never fired")
}

func TestAFourFourZeroThreeIsNeverRecoverable(t *testing.T) {
	// Revocation is not a rotation, and the revert must not be reachable from
	// it. FALSIFIER: the hook being consulted at all.
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)
	g.onAccept = func(_ *testing.T, _ *websocket.Conn) websocket.StatusCode { return 4403 }

	var consulted atomic.Int64
	clock := &recordClock{now: time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)}
	opts := pki.options(g.url())
	opts.OnUnauthorized = func(context.Context) bool { consulted.Add(1); return true }
	opts.Clock = clock
	opts.Jitter = func() float64 { return 0.5 }
	opts.HeartbeatEvery = 0

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := Run(ctx, opts, func(context.Context, []byte) {}); !errors.Is(err, ErrRevoked) {
		t.Fatalf("Run error = %v, want ErrRevoked", err)
	}
	if n := consulted.Load(); n != 0 {
		t.Fatalf("the rotation revert was consulted %d times on a revocation close", n)
	}
	if n := g.accepted.Load(); n != 1 {
		t.Fatalf("the gateway was dialled %d times after a 4403", n)
	}
}
