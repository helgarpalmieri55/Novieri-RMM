// Package transport is the agent's one connection to the control plane: a
// long-lived, OUTBOUND, mutually-authenticated TLS 1.3 WebSocket, parked on a
// blocking read.
//
// Implements docs/05-agent-spec.md §"Transport" and the frame contract in
// docs/kernel/wire-protocol/. The server side is app/gateway/main.py.
//
// # Invariant #2: outbound only
//
// Nothing here binds a port. There is no http.Server, no net.Listen, and no
// inbound upgrade: the agent dials the gateway_url it was given at enrollment
// and never accepts a connection. listen_test.go asserts that twice -- once
// against /proc/net/tcp for the running process, once with a go/ast walk over
// every non-test file in the module, so a listener added to a capability that
// no test exercises still goes red.
//
// # Invariant #1: no unsigned command path
//
// This package moves BYTES. It decodes nothing and dispatches nothing: Recv
// hands the raw frame to its caller, and the only legitimate caller is the
// signed-job runner (Task 7). A transport that "helpfully" parsed a job and
// called a capability would be the unsigned command path, so it does not.
//
// # Idle budget
//
// Two goroutines while connected: one parked in a blocking websocket read, one
// parked on a channel receive waiting to write. Everything time-based goes
// through Clock.Sleep -- a timer, never a poll. transport_test.go asserts zero
// socket reads return across an idle window, which is the observable
// difference between a blocking read and a polling loop.
package transport

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"sync"
	"time"

	"github.com/coder/websocket"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// Gateway close codes. The gateway closes 4401 for an unknown, expired, or
// revoked certificate at connect (app/gateway/main.py:42,183-186) and 4403
// when a live connection's certificate is revoked
// (app/gateway/dispatch.py:120). Both are verdicts about this agent's
// identity, and no amount of reconnecting changes one.
const (
	CloseUnauthorized = 4401
	CloseRevoked      = 4403
)

// rfc3339 matches app/jobs/canonical.py::rfc3339 -- whole seconds, literal Z.
// Not time.RFC3339: that layout renders a numeric offset for a non-UTC time,
// and a heartbeat carrying "+00:00" is a different string to the server.
const rfc3339 = "2006-01-02T15:04:05Z"

var (
	// ErrUnauthorized is terminal: the gateway does not recognise this
	// certificate, or it has expired. Reconnecting cannot fix it; only
	// re-enrollment (or, once it exists, renewal -- see OQ-1) can.
	ErrUnauthorized = errors.New("gateway closed 4401: certificate unrecognized, expired, or revoked")
	// ErrRevoked is terminal: an operator revoked this agent while it was
	// connected. An agent that kept reconnecting would be a denial-of-service
	// against its own control plane.
	ErrRevoked = errors.New("gateway closed 4403: certificate revoked")
	// ErrInsecureGatewayURL is terminal: mutual TLS is not optional. A ws:// or
	// http:// gateway URL would drop both server pinning and the client
	// certificate, and an unauthenticated socket looks exactly like an
	// authenticated one once it is open.
	ErrInsecureGatewayURL = errors.New("gateway URL must be wss:// (mutual TLS is invariant #2)")
	// ErrClosed is returned by Send after the connection has been closed.
	ErrClosed = errors.New("transport: connection closed")
)

// Clock is the agent's only source of time. Injected so backoff and heartbeat
// behaviour is asserted on observed durations rather than by sleeping in a
// test, and so nothing in this package can reach for time.Sleep directly.
type Clock interface {
	Now() time.Time
	// Sleep parks until d has elapsed or ctx is done. It returns ctx.Err() if
	// the context ended first. Implementations must PARK, never spin.
	Sleep(ctx context.Context, d time.Duration) error
}

type realClock struct{}

func (realClock) Now() time.Time { return time.Now() }

func (realClock) Sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Backoff is the reconnect schedule: min(Max, Base<<attempt), jittered.
type Backoff struct {
	Base time.Duration
	Max  time.Duration
}

// Delay returns the wait before attempt n. jitter is in [0,1) and spreads the
// result across [0.5x, 1.5x] of the nominal delay, so a control plane that
// restarts does not get the whole fleet back in the same millisecond.
func (b Backoff) Delay(attempt int, jitter float64) time.Duration {
	base, maximum := b.Base, b.Max
	if base <= 0 {
		base = DefaultBackoff.Base
	}
	if maximum <= 0 {
		maximum = DefaultBackoff.Max
	}
	d := maximum
	// Shift only while it cannot overflow int64; anything beyond that is
	// already past Max. A naive base<<attempt goes NEGATIVE at 63 and a
	// negative delay is an instant reconnect loop.
	if attempt >= 0 && attempt < 62 {
		if shifted := base << uint(attempt); shifted > 0 && shifted < maximum {
			d = shifted
		}
	}
	if jitter < 0 {
		jitter = 0
	}
	if jitter >= 1 {
		jitter = math.Nextafter(1, 0)
	}
	return time.Duration(float64(d) * (0.5 + jitter))
}

// DefaultBackoff: quick enough that a gateway restart is invisible, slow enough
// that a fleet-wide outage does not become a fleet-wide retry storm.
var DefaultBackoff = Backoff{Base: time.Second, Max: 5 * time.Minute}

// DefaultHeartbeat is 60s.
//
// It is a number with a server-side counterpart: app/gateway/dispatch.py
// refreshes agents.last_seen_at from a heartbeat at most once per 60s per
// device, and health's `agent.offline` check fires at offline_after_s, whose
// default is 900s and whose floor is 300s. 60s leaves four missed beats before
// the tightest configurable offline threshold.
const DefaultHeartbeat = 60 * time.Second

// DefaultResetAfter is how long a connection must live before the reconnect
// attempt counter goes back to zero. Resetting on a bare successful DIAL would
// mean a gateway that accepts and instantly drops gets redialled every Base
// forever -- backoff that never backs off.
const DefaultResetAfter = 60 * time.Second

// Options configures a dial and the reconnect loop around it.
type Options struct {
	// GatewayURL is the wss:// URL from the enrollment response. It is not
	// configurable from the environment: see internal/config.
	GatewayURL string
	// ClientCert is the device identity. The gateway resolves the agent by
	// sha256(DER(leaf)), so this certificate IS the authentication.
	//
	// Used when ClientCertFn is nil.
	ClientCert tls.Certificate
	// ClientCertFn, when non-nil, supplies the client certificate for EACH
	// dial, and ClientCert is ignored.
	//
	// It exists for certificate rotation
	// (docs/modules/agent/cert-renewal.md §4.3): `agent.cert.install` swaps the
	// identity ON DISK while a connection is up, and the live connection is not
	// re-keyed -- it was authenticated at handshake time and the gateway does
	// not re-check. Re-reading the identity per dial is what makes the new
	// certificate take effect on the next reconnect without a process restart.
	//
	// COMPATIBILITY: additive Go field, not a wire change. A nil hook is the
	// previous behaviour exactly.
	ClientCertFn func() (tls.Certificate, error)
	// RootCAs pins the control plane. Nil means "the host trust store", which
	// is not pinning, so Dial refuses it.
	RootCAs *x509.CertPool
	// HeartbeatEvery is the liveness interval; 0 disables it.
	HeartbeatEvery time.Duration
	// OnConnect runs once per successful dial, before the read loop starts.
	//
	// It exists so per-connection checks -- Task 6's certificate expiry
	// warning is the first -- can run without a timer of their own. It is not
	// a message hook: it receives nothing from the wire and can send nothing,
	// so it cannot become a second inbound path.
	OnConnect func(context.Context)
	// OnConnected runs once per successful dial, after OnConnect and before
	// the read loop starts, with the live connection.
	//
	// It exists for one caller: the offline outbound buffer, which must drain
	// what it holds -- in order -- BEFORE the runner produces anything new
	// against this connection. Handing out the *Conn is a write capability and
	// nothing else: Recv is not safe to call from here (one reader only), and
	// this hook is never given a frame, so it cannot become a second INBOUND
	// path. If it returns an error the connection is closed and the reconnect
	// loop backs off, because an agent that could not flush its answers should
	// try again rather than pile more on top.
	//
	// COMPATIBILITY: additive Go field, not a wire change. A nil hook is the
	// previous behaviour exactly.
	OnConnected func(context.Context, *Conn) error
	// OnDisconnect runs once per connection loss, after the connection is
	// closed and before the reconnect backoff.
	//
	// It exists for callers holding state that is only meaningful on ONE
	// connection -- the backup dedup exchange is the first: a query's answer
	// comes back on the connection it went out on, so a pending wait must fail
	// when that connection dies rather than burning its full deadline waiting
	// for a reply that cannot arrive.
	//
	// It receives the cause and can send nothing: like OnConnect, it is not a
	// message hook and cannot become a second inbound path.
	//
	// COMPATIBILITY: additive Go field, not a wire change. A nil hook is the
	// previous behaviour exactly.
	OnDisconnect func(context.Context, error)
	// OnUnauthorized runs when the gateway closes 4401, and its bool decides
	// whether that verdict is terminal.
	//
	// 4401 is a verdict about this agent's identity and reconnecting cannot
	// normally change one -- which is what stops a fleet whose certificates all
	// expire on the same day becoming a retry storm. There is exactly one case
	// where it CAN change: a rotation the endpoint applied and the control plane
	// never recorded (the acknowledgement was lost). The endpoint then holds a
	// certificate the gateway has never heard of, while the gateway's gate still
	// names the one the endpoint just replaced.
	//
	// Returning true means "I put myself back on the previous certificate; try
	// again". The agent's implementation can only do that while it still HAS a
	// previous certificate, and reverting consumes it -- so a second 4401 in the
	// same situation is terminal exactly as before, and the anti-storm property
	// survives.
	//
	// COMPATIBILITY: additive Go field. A nil hook is the previous behaviour
	// exactly: 4401 is always terminal.
	OnUnauthorized func(context.Context) bool
	// Header is added to every upgrade request (IdentityHeader). Additive: a
	// nil Header is the previous behaviour exactly.
	Header http.Header
	// Backoff, Jitter, ResetAfter, Clock, Logger, DialTimeout have defaults.
	Backoff     Backoff
	Jitter      func() float64
	ResetAfter  time.Duration
	Clock       Clock
	Logger      *slog.Logger
	DialTimeout time.Duration

	// dialTLS is a test seam for observing the socket underneath the
	// WebSocket. Unexported on purpose: no build tag, no environment variable,
	// nothing an operator or a deployment can reach.
	dialTLS func(ctx context.Context, cfg *tls.Config, network, addr string) (net.Conn, error)
}

func (o Options) withDefaults() Options {
	if o.Clock == nil {
		o.Clock = realClock{}
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	if o.Jitter == nil {
		o.Jitter = rand.Float64
	}
	if o.Backoff.Base <= 0 {
		o.Backoff.Base = DefaultBackoff.Base
	}
	if o.Backoff.Max <= 0 {
		o.Backoff.Max = DefaultBackoff.Max
	}
	if o.ResetAfter <= 0 {
		o.ResetAfter = DefaultResetAfter
	}
	if o.DialTimeout <= 0 {
		o.DialTimeout = 30 * time.Second
	}
	return o
}

// Conn is one live gateway connection.
//
// Exactly one goroutine may call Recv (the read is blocking and
// coder/websocket allows one reader). Send is safe from any goroutine: it
// enqueues, and a single owned writer goroutine drains the queue, because
// coder/websocket forbids concurrent writers and "we only write from one
// place" is a comment, not a guarantee.
type Conn struct {
	ws     *websocket.Conn
	out    chan []byte
	closed chan struct{}
	once   sync.Once
	writer chan struct{} // closed when the writer goroutine has exited
	log    *slog.Logger
}

// Dial opens one mutually-authenticated WebSocket to the gateway.
func Dial(ctx context.Context, opts Options) (*Conn, error) {
	opts = opts.withDefaults()

	u, err := url.Parse(opts.GatewayURL)
	if err != nil {
		return nil, fmt.Errorf("transport: %w: %q is not a URL", ErrInsecureGatewayURL, opts.GatewayURL)
	}
	if u.Scheme != "wss" && u.Scheme != "https" {
		return nil, fmt.Errorf("transport: %w: scheme %q", ErrInsecureGatewayURL, u.Scheme)
	}
	if opts.RootCAs == nil {
		// The host trust store would accept any publicly-trusted certificate
		// for the gateway's hostname. The CA bundle from enrollment is the
		// pin; refusing to dial without it is the only way "pinned" is a fact
		// rather than a claim.
		return nil, fmt.Errorf("transport: %w: no pinned CA bundle", ErrInsecureGatewayURL)
	}

	cfg := &tls.Config{
		MinVersion: tls.VersionTLS13,
		RootCAs:    opts.RootCAs,
	}
	clientCert := opts.ClientCert
	if opts.ClientCertFn != nil {
		// Fail the dial rather than falling back to opts.ClientCert: a stale
		// certificate silently presented after a rotation is how an endpoint
		// ends up locked out for a reason nobody can see.
		clientCert, err = opts.ClientCertFn()
		if err != nil {
			return nil, fmt.Errorf("transport: client certificate: %w", err)
		}
	}
	if len(clientCert.Certificate) > 0 {
		cfg.Certificates = []tls.Certificate{clientCert}
	}

	dialTLS := opts.dialTLS
	if dialTLS == nil {
		dialTLS = func(ctx context.Context, cfg *tls.Config, network, addr string) (net.Conn, error) {
			d := &tls.Dialer{NetDialer: &net.Dialer{Timeout: opts.DialTimeout}, Config: cfg}
			return d.DialContext(ctx, network, addr)
		}
	}

	client := &http.Client{
		Transport: &http.Transport{
			// HTTP/1.1 only: the WebSocket upgrade does not exist over h2.
			ForceAttemptHTTP2: false,
			DisableKeepAlives: true,
			DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
				return dialTLS(ctx, cfg, network, addr)
			},
		},
	}

	dialCtx, cancel := context.WithTimeout(ctx, opts.DialTimeout)
	defer cancel()
	ws, resp, err := websocket.Dial(dialCtx, u.String(), &websocket.DialOptions{
		HTTPClient: client,
		HTTPHeader: opts.Header.Clone(),
	})
	if resp != nil && resp.Body != nil {
		_ = resp.Body.Close()
	}
	if err != nil {
		return nil, fmt.Errorf("transport: dial %s: %w", u.Redacted(), err)
	}
	// The gateway pushes signed job envelopes; the default 32KiB read limit is
	// generous for a job payload and is a bound on what a compromised control
	// plane can make an endpoint allocate.
	ws.SetReadLimit(1 << 20)

	c := &Conn{
		ws:     ws,
		out:    make(chan []byte, 64),
		closed: make(chan struct{}),
		writer: make(chan struct{}),
		log:    opts.Logger,
	}
	go c.writeLoop()
	return c, nil
}

// writeLoop is the only goroutine that writes to the socket. It parks on a
// channel receive; an idle agent costs one blocked goroutine, not a tick.
func (c *Conn) writeLoop() {
	defer close(c.writer)
	for {
		select {
		case <-c.closed:
			return
		case b := <-c.out:
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			err := c.ws.Write(ctx, websocket.MessageText, b)
			cancel()
			if err != nil {
				// The read side will see the same failure and drive the
				// reconnect; there is nothing useful to do from here.
				c.log.Debug("gateway write failed", "err", err)
				return
			}
		}
	}
}

// Recv blocks until one frame arrives. The bytes are returned unparsed: this
// package does not decide what a frame means (invariant #1).
func (c *Conn) Recv(ctx context.Context) ([]byte, error) {
	_, b, err := c.ws.Read(ctx)
	if err != nil {
		return nil, err
	}
	return b, nil
}

// Send frames msg with wire.Encode and queues it for the writer goroutine.
//
// wire.Encode refuses anything that is not one of the seven declared agent to
// server kinds, so a caller cannot invent a message the server would drop
// silently at parse_agent_message.
func (c *Conn) Send(ctx context.Context, msg any) error {
	b, err := wire.Encode(msg)
	if err != nil {
		return err
	}
	return c.SendFrame(ctx, b)
}

// SendFrame queues an ALREADY-FRAMED message.
//
// The offline buffer stores frames rather than message values -- a frame
// validated by wire.Encode at enqueue time cannot become invalid while it
// waits, and re-encoding a decoded message would be a second chance to change
// bytes. This is the only way such a frame reaches the socket.
//
// It does NOT re-validate: framing is wire.Encode's job and it already ran.
// Nothing outside internal/jobs' buffer should call this; use Send.
func (c *Conn) SendFrame(ctx context.Context, frame []byte) error {
	select {
	case c.out <- frame:
		return nil
	case <-c.closed:
		return ErrClosed
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Close is idempotent and safe from any goroutine.
func (c *Conn) Close() error {
	var err error
	c.once.Do(func() {
		close(c.closed)
		err = c.ws.Close(websocket.StatusNormalClosure, "")
		if err != nil {
			err = c.ws.CloseNow()
		}
		<-c.writer
	})
	return err
}

// Run dials, serves, and reconnects until ctx ends or a terminal verdict
// arrives.
//
// handler is called with each raw inbound frame, on the read goroutine, in
// order. A handler that blocks blocks the connection: no further inbound
// frame is read -- including the pongs that keep it alive -- until handler
// returns.
//
// That is a constraint on handler, not a design this package relies on for
// job ordering. jobs.Runner.Handle used to block for the whole capability, on
// the theory that job ordering could just be the wire's ordering; that is
// what deadlocked the backup dedup exchange (a capability blocking on a reply
// only this read goroutine could deliver) and what starved the pongs above.
// Handle now decodes, verifies and hands off to a per-capability dispatcher
// before returning, in this package's normal call time -- see
// jobs.Runner.Handle's doc comment. Any handler passed here still MUST NOT
// BLOCK for the reason stated above; this package just no longer assumes one
// that does.
func Run(ctx context.Context, opts Options, handler func(context.Context, []byte)) error {
	opts = opts.withDefaults()
	attempt := 0
	for {
		if err := ctx.Err(); err != nil {
			return nil
		}

		start := opts.Clock.Now()
		conn, err := Dial(ctx, opts)
		if err == nil {
			opts.Logger.Info("gateway connected", "url", opts.GatewayURL)
			if opts.OnConnect != nil {
				opts.OnConnect(ctx)
			}
			if opts.OnConnected != nil {
				err = opts.OnConnected(ctx, conn)
			}
			if err == nil {
				err = conn.serve(ctx, opts, handler)
			}
			_ = conn.Close()
			if opts.OnDisconnect != nil {
				opts.OnDisconnect(ctx, err)
			}
		}

		if ctx.Err() != nil {
			return nil
		}
		if terminal := terminalError(err); terminal != nil {
			// The ONE recoverable 4401: an identity rotation this endpoint
			// applied and the control plane never recorded. The hook puts the
			// previous certificate back and consumes it, so this cannot loop.
			if errors.Is(terminal, ErrUnauthorized) && opts.OnUnauthorized != nil &&
				opts.OnUnauthorized(ctx) {
				opts.Logger.Warn("gateway refused the current certificate; reverted and retrying",
					"err", terminal)
				attempt = 0
				if serr := opts.Clock.Sleep(ctx, opts.Backoff.Delay(0, opts.Jitter())); serr != nil {
					return nil
				}
				continue
			}
			opts.Logger.Error("gateway connection terminally refused; not reconnecting", "err", terminal)
			return terminal
		}
		if opts.Clock.Now().Sub(start) >= opts.ResetAfter {
			attempt = 0
		}

		d := opts.Backoff.Delay(attempt, opts.Jitter())
		opts.Logger.Warn("gateway disconnected; reconnecting", "err", err, "attempt", attempt, "in", d)
		if serr := opts.Clock.Sleep(ctx, d); serr != nil {
			return nil
		}
		attempt++
	}
}

// serve reads until the connection fails, heartbeating on a timer.
func (c *Conn) serve(ctx context.Context, opts Options, handler func(context.Context, []byte)) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	if opts.HeartbeatEvery > 0 {
		go c.heartbeat(ctx, opts)
	}
	for {
		raw, err := c.Recv(ctx)
		if err != nil {
			return err
		}
		handler(ctx, raw)
	}
}

// heartbeat is timer-driven: it parks in Clock.Sleep and wakes once per
// interval. Its only job is to keep agents.last_seen_at fresh, which is what
// health's server-evaluated `agent.offline` check reads.
func (c *Conn) heartbeat(ctx context.Context, opts Options) {
	for {
		if err := opts.Clock.Sleep(ctx, opts.HeartbeatEvery); err != nil {
			return
		}
		ts := opts.Clock.Now().UTC().Format(rfc3339)
		if err := c.Send(ctx, wire.Heartbeat{TS: ts}); err != nil {
			return
		}
	}
}

// terminalError maps a close code to a verdict this agent must not retry
// against. Anything else -- a dropped TCP connection, a gateway restart, a
// network partition -- is transient and gets the backoff.
func terminalError(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, ErrInsecureGatewayURL) {
		return err
	}
	switch websocket.CloseStatus(err) {
	case CloseUnauthorized:
		return fmt.Errorf("%w (%v)", ErrUnauthorized, err)
	case CloseRevoked:
		return fmt.Errorf("%w (%v)", ErrRevoked, err)
	}
	return nil
}
