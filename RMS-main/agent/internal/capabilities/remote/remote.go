// Package remote implements `remote.screen.open`, the signed job that shares
// the endpoint's console with a technician
// (docs/modules/remote-control/spec-screen-share.md §2.3).
//
// The service (SYSTEM on Windows) does three things and nothing else: it
// dials the relay as a second outbound mTLS WebSocket and authenticates with
// the one-shot token from the signed params; it launches the helper in the
// logged-on user's session with two inherited anonymous pipes; and it moves
// rsp/1 frames between the two, checking each frame's direction. It never
// captures, encodes or injects, so no graphics DLL is ever loaded here
// (§4.2 F4), and nothing listens: the relay is dialled, the pipes are
// inherited handles (F6).
//
// Dormant at idle: registration is a map insert, and every goroutine, handle
// and process this package starts belongs to one job and is gone when the
// handler returns (§4.2 F1, F2, F3).
package remote

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"

	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/ipc"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// JobType matches app/modules/remote/jobtypes.py.
const JobType = "remote.screen.open"

// HelperArg is the subcommand that runs the agent binary as the helper.
const HelperArg = "remote-helper"

// Params implements RemoteScreenOpenParams.schema.json. Unknown members are
// ignored, so the server can add optional ones without breaking this build.
type Params struct {
	SessionID    string  `json:"session_id"`
	RelayURL     string  `json:"relay_url"`
	RelayToken   string  `json:"relay_token"`
	ConnectBy    string  `json:"connect_by"`
	ConsentMode  string  `json:"consent_mode"`
	Consent      Consent `json:"consent"`
	MaxDurationS int     `json:"max_duration_s"`
	AllowInput   *bool   `json:"allow_input"`
	Record       *bool   `json:"record"`
	Desktop      string  `json:"desktop"`
}

// Consent is ScreenConsent.
type Consent struct {
	Technician string `json:"technician"`
	Org        string `json:"org"`
	Reason     string `json:"reason"`
	TimeoutS   int    `json:"timeout_s"`
}

// Result implements RemoteScreenOpenResult.schema.json.
type Result struct {
	Outcome   string `json:"outcome"`
	DurationS int    `json:"duration_s"`
	Detail    string `json:"detail"`
}

// Bounds restated from the schema and pinned to it by schema_test.go.
const (
	minDurationS    = 60
	maxDurationS    = 86400
	minConsentS     = 15
	maxConsentS     = 120
	defaultConsentS = 30
)

// Options wires the capability. Main binds it; tests build their own.
type Options struct {
	// ClientCert is read per dial, like the agent channel's, so a rotated
	// certificate is used without a restart.
	ClientCert func() (tls.Certificate, error)
	// RootCAs pins the gateway. Nil refuses to dial.
	RootCAs  *x509.CertPool
	Launcher Launcher
	Logger   *slog.Logger
	Now      func() time.Time
	// HelperGrace is how long the helper gets to exit on its own before it
	// is killed (spec §2.3 step 7: 5 s).
	HelperGrace time.Duration
	// AuthTimeout bounds the AUTH exchange with the gateway.
	AuthTimeout time.Duration

	// limit replaces max_duration_s + consent timeout in tests, which cannot
	// wait the schema's one-minute minimum.
	limit time.Duration
	// dropped, in tests, is called when a viewer frame is refused because
	// consent has not been given, so a test can order consent after the drop.
	dropped func()
}

func (o Options) withDefaults() Options {
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	if o.Now == nil {
		o.Now = time.Now
	}
	if o.HelperGrace <= 0 {
		o.HelperGrace = 5 * time.Second
	}
	if o.AuthTimeout <= 0 {
		o.AuthTimeout = 10 * time.Second
	}
	return o
}

// Service is the capability. The zero value refuses every job until bound.
type Service struct {
	mu    sync.Mutex
	o     Options
	bound bool
}

// Process is this binary's capability, bound by main.
var Process = &Service{}

// New returns a bound Service.
func New(o Options) *Service {
	s := &Service{}
	s.Bind(o)
	return s
}

// Bind sets the wiring. It stores values and starts nothing.
func (s *Service) Bind(o Options) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.o, s.bound = o.withDefaults(), true
}

// Open is the jobs.Handler for remote.screen.open. Every outcome the
// capability can name is a successful job with that outcome; an error means
// the handler could not run at all.
func (s *Service) Open(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	s.mu.Lock()
	o, bound := s.o, s.bound
	s.mu.Unlock()
	if !bound || o.Launcher == nil || o.ClientCert == nil || o.RootCAs == nil {
		return nil, jobs.Refuse("screen sharing is not available on this agent build")
	}
	var p Params
	if err := json.Unmarshal(job.Params(), &p); err != nil {
		return nil, fmt.Errorf("remote.screen.open: params: %w", err)
	}
	res := run(ctx, p, o)
	o.Logger.Info("screen session over", "session", p.SessionID, "outcome", res.Outcome, "duration_s", res.DurationS, "detail", res.Detail)
	return res, nil
}

func (p Params) check(now time.Time) (Result, bool) {
	switch {
	case p.SessionID == "" || p.RelayToken == "":
		return Result{Outcome: "error", Detail: "params lack a session id or relay token"}, false
	case !strings.HasPrefix(p.RelayURL, "wss://"):
		return Result{Outcome: "relay_refused", Detail: "relay_url is not wss://"}, false
	case p.ConsentMode != "attended" && p.ConsentMode != "unattended":
		return Result{Outcome: "error", Detail: "unknown consent_mode"}, false
	case p.Desktop != "" && p.Desktop != "user":
		return Result{Outcome: "error", Detail: "this agent shares only the user desktop"}, false
	}
	connectBy, err := time.Parse(time.RFC3339, p.ConnectBy)
	if err != nil {
		return Result{Outcome: "error", Detail: "connect_by is not a timestamp"}, false
	}
	if now.After(connectBy) {
		return Result{Outcome: "expired", Detail: "connect_by has passed"}, false
	}
	return Result{}, true
}

func (p Params) config() ipc.SessionConfig {
	t := p.Consent.TimeoutS
	if t < minConsentS || t > maxConsentS {
		t = defaultConsentS
	}
	return ipc.SessionConfig{
		SessionID:   p.SessionID,
		Attended:    p.ConsentMode != "unattended",
		Technician:  p.Consent.Technician,
		Org:         p.Consent.Org,
		Reason:      p.Consent.Reason,
		TimeoutS:    t,
		AllowInput:  p.AllowInput == nil || *p.AllowInput,
		Record:      p.Record == nil || *p.Record,
		MaxDuration: p.duration(),
	}
}

func (p Params) duration() int {
	d := p.MaxDurationS
	if d < minDurationS {
		d = minDurationS
	}
	if d > maxDurationS {
		d = maxDurationS
	}
	return d
}

func run(ctx context.Context, p Params, o Options) Result {
	start := o.Now()
	res := session(ctx, p, o)
	res.DurationS = int(o.Now().Sub(start) / time.Second)
	return res
}

func session(ctx context.Context, p Params, o Options) Result {
	if res, ok := p.check(o.Now()); !ok {
		return res
	}
	conn, err := dialRelay(ctx, p, o)
	if err != nil {
		return Result{Outcome: "relay_refused", Detail: err.Error()}
	}
	defer conn.CloseNow()
	r := &relay{conn: conn, seq: 1}

	h, err := o.Launcher.Launch(ctx)
	if err != nil {
		reason, outcome := "error", "error"
		if errors.Is(err, ErrNoUserSession) {
			reason, outcome = "no_user_session", "no_user_session"
		}
		r.end(reason)
		return Result{Outcome: outcome, Detail: err.Error()}
	}
	return pump(ctx, p, o, r, h)
}

// relay is the agent leg with the service's own frame numbering.
type relay struct {
	conn *websocket.Conn
	mu   sync.Mutex
	seq  uint32
}

func (r *relay) forward(ctx context.Context, f rsp.Frame, raw []byte) error {
	r.mu.Lock()
	r.seq = f.Seq
	r.mu.Unlock()
	return r.conn.Write(ctx, websocket.MessageBinary, raw)
}

// end tells the viewer why the session ended, when the helper cannot, and
// closes the leg.
func (r *relay) end(reason string) {
	r.mu.Lock()
	r.seq++
	seq := r.seq
	r.mu.Unlock()
	if b, err := rsp.EncodeJSON(rsp.State, seq, rsp.StateBody{State: "ended", Reason: reason}); err == nil {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_ = r.conn.Write(ctx, websocket.MessageBinary, b)
		cancel()
	}
	_ = r.conn.Close(websocket.StatusCode(rsp.CloseEnded), reason)
}

// tracker is what the service learns from the helper's STATE frames.
type tracker struct {
	mu        sync.Mutex
	consented bool
	outcome   string
	ended     bool
}

func (t *tracker) observe(f rsp.Frame) {
	var s rsp.StateBody
	if rsp.DecodeJSON(f, &s) != nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	switch s.State {
	case "consent_granted":
		t.consented = true
	case "consent_denied":
		t.outcome, t.ended = "denied", true
	case "consent_timeout":
		t.outcome, t.ended = "consent_timeout", true
	case "ended":
		t.outcome, t.ended = outcomeFor(s.Reason), true
	}
}

func (t *tracker) allowed() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.consented
}

func (t *tracker) result() (string, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.outcome, t.ended
}

func outcomeFor(reason string) string {
	switch reason {
	case "completed", "ended_by_user":
		return "ended"
	case "no_user_session", "capture_unavailable", "permission_missing", "unsupported_display", "expired":
		return reason
	default:
		return "error"
	}
}

// pump moves frames until either side ends, then releases everything: the
// helper exits (or is killed after the grace period), both pipes and the
// relay are closed, and every goroutine started here has returned.
func pump(ctx context.Context, p Params, o Options, r *relay, h Helper) Result {
	defer h.Close()
	cfg := p.config()
	track := &tracker{consented: !cfg.Attended}
	hw := ipc.NewWriter(h.Stdin())

	// The pipes and the relay outlive a cancelled job long enough to tell the
	// viewer why: coder/websocket closes the connection when a Read's context
	// ends, so I/O runs on its own context, cancelled only at teardown.
	job := ctx
	ctx, cancel := context.WithCancel(context.WithoutCancel(job))
	defer cancel()
	var wg sync.WaitGroup
	helperDone := make(chan struct{})
	relayDone := make(chan struct{})

	if err := hw.WriteJSON(ipc.Config, cfg); err != nil {
		_ = h.Kill()
		<-h.Exited()
		r.end("error")
		return Result{Outcome: "error", Detail: "the helper did not start"}
	}

	wg.Add(2)
	go func() { // helper -> relay
		defer wg.Done()
		defer close(helperDone)
		rd := ipc.NewReader(h.Stdout())
		for {
			k, body, err := rd.Read()
			if err != nil {
				return
			}
			f, err := rsp.Decode(body)
			// The helper runs as the user: only agent->viewer frames of rsp/1
			// leave through the relay, and only STATE before consent.
			if k != ipc.Frame || err != nil || !rsp.AgentToViewer(f.Type) {
				continue
			}
			if f.Type == rsp.State {
				track.observe(f)
			} else if !track.allowed() {
				continue
			}
			_ = r.forward(ctx, f, body)
		}
	}()
	go func() { // relay -> helper
		defer wg.Done()
		defer close(relayDone)
		for {
			typ, b, err := r.conn.Read(ctx)
			if err != nil {
				return
			}
			f, err := rsp.Decode(b)
			if typ != websocket.MessageBinary || err != nil || !rsp.ViewerToAgent(f.Type) {
				continue
			}
			if !track.allowed() {
				if o.dropped != nil {
					o.dropped()
				}
				continue
			}
			if hw.Write(ipc.Frame, b) != nil {
				return
			}
		}
	}()

	d := time.Duration(cfg.MaxDuration+cfg.TimeoutS) * time.Second
	if o.limit > 0 {
		d = o.limit
	}
	limit := time.NewTimer(d)
	defer limit.Stop()
	stop, cause := "", ""
	select {
	case <-helperDone:
		cause = "helper"
	case <-relayDone:
		cause = "relay"
	case <-limit.C:
		stop, cause = "expired", "limit"
	case <-job.Done():
		stop, cause = "completed", "cancelled"
		if errors.Is(job.Err(), context.DeadlineExceeded) {
			stop = "expired"
		}
	}

	// Ask the helper to finish; it reports STATE ended itself when it can.
	if cause != "helper" {
		sent := make(chan struct{})
		go func() {
			_ = hw.WriteJSON(ipc.Stop, ipc.StopBody{Reason: stop})
			close(sent)
		}()
		wait(sent, time.Second)
		wait(helperDone, o.HelperGrace)
	}
	_ = h.Stdin().Close()
	if !wait(h.Exited(), o.HelperGrace) {
		o.Logger.Warn("screen-share helper did not exit; terminating it", "pid", h.PID())
		_ = h.Kill()
		<-h.Exited()
	}
	// A relay that stopped reading must not hold the last frames forever.
	if !wait(helperDone, o.HelperGrace) {
		cancel()
		<-helperDone
	}

	outcome, ended := track.result()
	detail := ""
	switch {
	case ended:
	case cause == "relay":
		outcome = "ended"
	case cause == "limit" || stop == "expired":
		outcome = "expired"
		r.end("expired")
	case cause == "cancelled":
		outcome = "ended"
		r.end("completed")
	default:
		outcome, detail = "error", "the helper exited without ending the session"
		r.end("error")
	}
	_ = r.conn.Close(websocket.StatusCode(rsp.CloseEnded), outcome)
	cancel()
	wg.Wait()
	if s, ok := h.(interface{ Stderr() string }); ok && outcome != "ended" && outcome != "denied" {
		o.Logger.Warn("screen-share helper log", "outcome", outcome, "stderr", s.Stderr())
	}
	return Result{Outcome: outcome, Detail: detail}
}

// wait reports whether ch closed within d.
func wait(ch <-chan struct{}, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ch:
		return true
	case <-t.C:
		return false
	}
}
