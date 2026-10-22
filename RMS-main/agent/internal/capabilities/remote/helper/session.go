package helper

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode"

	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/ipc"
	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// Options tunes a session. Zero values are the production defaults.
type Options struct {
	Logger *slog.Logger
	Now    func() time.Time
	// CaptureTimeout bounds one blocking Capturer.Next call.
	CaptureTimeout time.Duration
	// MinFrameInterval caps the frame rate.
	MinFrameInterval time.Duration
	// Workers is the number of encoder goroutines, joined per frame.
	Workers int
	// StaleAck is how long unacknowledged frames may block the stream before
	// the helper assumes the viewer lost them.
	StaleAck time.Duration
}

func (o Options) withDefaults() Options {
	if o.Logger == nil {
		o.Logger = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if o.Now == nil {
		o.Now = time.Now
	}
	if o.CaptureTimeout <= 0 {
		o.CaptureTimeout = 500 * time.Millisecond
	}
	if o.MinFrameInterval <= 0 {
		o.MinFrameInterval = time.Second / 30
	}
	if o.Workers <= 0 {
		o.Workers = 4
	}
	if o.StaleAck <= 0 {
		o.StaleAck = 5 * time.Second
	}
	return o
}

// maxUnacked is rsp/1's flow-control window: no new frame while more than
// two FRAME_ENDs are unacknowledged.
const maxUnacked = 2

// Capabilities are what HELLO tells the viewer it may use.
var Capabilities = []string{"tile", "copyrect", "cursor", "control", "text", "monitor_select", "quality", "refresh"}

// ErrNoConfig means the service did not open with a Config message.
var ErrNoConfig = errors.New("helper: the first message was not the session config")

type session struct {
	cfg    ipc.SessionConfig
	p      Platform
	o      Options
	w      *ipc.Writer
	log    *slog.Logger
	prompt Prompt

	seqMu sync.Mutex
	seq   uint32

	consented atomic.Bool

	mu          sync.Mutex
	monitors    []rsp.Monitor
	selected    int
	want        int
	started     bool
	forceFull   bool
	quality     byte
	outstanding []uint32
	since       time.Time // when the oldest outstanding frame was sent
	flow        chan struct{}
}

// Run drives one session over the service's pipes: consent, the indicator,
// capture and input, until the user or the service ends it. It returns after
// everything it opened is closed and every goroutine it started has exited.
func Run(ctx context.Context, in io.Reader, out io.Writer, p Platform, o Options) error {
	o = o.withDefaults()
	r := ipc.NewReader(in)
	k, body, err := r.Read()
	if err != nil {
		return err
	}
	var cfg ipc.SessionConfig
	if k != ipc.Config || json.Unmarshal(body, &cfg) != nil {
		return ErrNoConfig
	}
	s := &session{
		cfg: cfg, p: p, o: o, w: ipc.NewWriter(out), log: o.Logger,
		prompt: promptFor(cfg), flow: make(chan struct{}, 1), want: -1,
	}

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	if cfg.MaxDuration > 0 {
		var stop context.CancelFunc
		ctx, stop = context.WithTimeout(ctx, time.Duration(cfg.MaxDuration)*time.Second)
		defer stop()
	}

	inbox := make(chan rsp.Frame, 256)
	stopped := make(chan string, 1)
	// The reader ends when the service closes the pipe or the process exits;
	// Run does not wait for it, because a blocked pipe read cannot be
	// interrupted portably and the helper exits right after Run returns.
	go func() {
		defer cancel()
		for {
			k, body, err := r.Read()
			if err != nil {
				return // the service is gone: end without a word
			}
			switch k {
			case ipc.Frame:
				f, err := rsp.Decode(body)
				// Nothing from the viewer counts before the user has allowed
				// the session, whatever the service and the gateway let through.
				if err != nil || !rsp.ViewerToAgent(f.Type) || !s.consented.Load() {
					continue
				}
				select {
				case inbox <- f:
				case <-ctx.Done():
					return
				}
			case ipc.Stop:
				var b ipc.StopBody
				_ = json.Unmarshal(body, &b)
				stopped <- b.Reason
				return
			}
		}
	}()

	reason := s.run(ctx, inbox)
	select {
	case r := <-stopped:
		reason = r
	default:
		if reason == "" && ctx.Err() == context.DeadlineExceeded {
			reason = "expired"
		}
	}
	if reason != "" {
		_ = s.state(rsp.StateBody{State: "ended", Reason: reason})
	}
	return nil
}

// run is the session after the config. It returns the ended reason to
// report, or "" when there is nobody left to tell.
func (s *session) run(ctx context.Context, inbox <-chan rsp.Frame) string {
	ui := s.p.UI()
	defer ui.Close()

	if !s.cfg.Attended {
		s.consented.Store(true)
	} else {
		if err := s.state(rsp.StateBody{State: "consent_pending", TimeoutS: int(s.prompt.Timeout / time.Second)}); err != nil {
			return ""
		}
		switch ui.Consent(ctx, s.prompt) {
		case Granted:
			if err := s.state(rsp.StateBody{State: "consent_granted"}); err != nil {
				return ""
			}
			s.consented.Store(true)
		case TimedOut:
			_ = s.state(rsp.StateBody{State: "consent_timeout"})
			return ""
		default:
			if ctx.Err() == nil {
				_ = s.state(rsp.StateBody{State: "consent_denied"})
			}
			return ""
		}
	}

	monitors, err := s.p.Monitors()
	if err != nil || len(monitors) == 0 {
		s.log.Warn("no monitors", "err", err)
		return "capture_unavailable"
	}
	sel := 0
	for i, m := range monitors {
		if m.Primary {
			sel = i
		}
	}
	s.monitors, s.selected = monitors, sel

	control := s.cfg.AllowInput
	ind, err := ui.ShowIndicator(s.prompt, monitors[sel], control)
	if err != nil {
		// The indicator is not optional: no indicator, no session.
		s.log.Warn("indicator", "err", err)
		return "error"
	}
	defer ind.Close()

	inj, err := s.p.OpenInjector(monitors[sel])
	if err != nil {
		s.log.Warn("input unavailable; view only", "err", err)
		inj, control = nil, false
		ind.SetControlling(false)
	}
	gate := newInputGate(inj, monitors[sel].Width, monitors[sel].Height, control)
	defer func() {
		gate.releaseAll()
		if gate.inj != nil {
			_ = gate.inj.Close()
		}
	}()

	pumpCtx, stopPump := context.WithCancel(ctx)
	pumpDone := make(chan error, 1)
	go func() { pumpDone <- s.pump(pumpCtx) }()
	defer func() {
		stopPump()
		<-pumpDone
	}()

	userRevoked := false
	for {
		select {
		case <-ctx.Done():
			return ""
		case err := <-pumpDone:
			pumpDone <- err // for the deferred receive
			s.log.Warn("capture ended the session", "err", err)
			return "capture_unavailable"
		case ev := <-ind.Events():
			switch ev {
			case EndSession:
				return "ended_by_user"
			case StopControl:
				if !userRevoked {
					userRevoked = true
					gate.setControl(false)
					ind.SetControlling(false)
					_ = s.state(rsp.StateBody{State: "control_revoked_by_user"})
				}
			}
		case f := <-inbox:
			s.onViewer(f, gate, ind, userRevoked)
		}
	}
}

func (s *session) onViewer(f rsp.Frame, gate *inputGate, ind Indicator, userRevoked bool) {
	switch f.Type {
	case rsp.Ack:
		n, err := rsp.DecodeAck(f)
		if err != nil {
			return
		}
		s.mu.Lock()
		keep := s.outstanding[:0]
		for _, seq := range s.outstanding {
			if int32(seq-n) > 0 {
				keep = append(keep, seq)
			}
		}
		s.outstanding = keep
		s.since = s.o.Now()
		s.mu.Unlock()
		s.poke()
	case rsp.Refresh:
		s.mu.Lock()
		s.started, s.forceFull, s.outstanding = true, true, nil
		s.mu.Unlock()
		s.poke()
	case rsp.Quality:
		if q, err := rsp.DecodeQuality(f); err == nil {
			s.mu.Lock()
			s.quality = q
			s.mu.Unlock()
		}
	case rsp.MonitorSelect:
		id, err := rsp.DecodeMonitorSelect(f)
		if err != nil {
			return
		}
		s.mu.Lock()
		i := indexOf(s.monitors, int(id))
		if i < 0 || i == s.selected {
			s.mu.Unlock()
			return
		}
		s.want = i
		m := s.monitors[i]
		s.mu.Unlock()
		inj, err := s.p.OpenInjector(m)
		if err != nil {
			inj = nil
		}
		if gate.inj != nil {
			gate.releaseAll()
			_ = gate.inj.Close()
		}
		gate.resize(inj, m.Width, m.Height)
		s.poke()
	case rsp.Control:
		want, err := rsp.DecodeControl(f)
		if err != nil || (want && (userRevoked || !s.cfg.AllowInput || gate.inj == nil)) {
			return
		}
		gate.setControl(want)
		ind.SetControlling(want)
	default:
		if isInput(f.Type) {
			gate.handle(f, s.o.Now())
		}
		// SAS (phase 2) and types this build does not know are ignored.
	}
}

func indexOf(ms []rsp.Monitor, id int) int {
	for i, m := range ms {
		if m.ID == id {
			return i
		}
	}
	return -1
}

func (s *session) poke() {
	select {
	case s.flow <- struct{}{}:
	default:
	}
}

// waitCredit blocks until the viewer has asked for frames and the flow
// window has room. It reports whether it had to wait.
func (s *session) waitCredit(ctx context.Context) (bool, error) {
	waited := false
	for {
		s.mu.Lock()
		if len(s.outstanding) > maxUnacked && s.o.Now().Sub(s.since) > s.o.StaleAck {
			s.log.Warn("no ACK for a while; assuming the viewer lost those frames", "outstanding", len(s.outstanding))
			s.outstanding = nil
		}
		ok := s.started && len(s.outstanding) <= maxUnacked
		s.mu.Unlock()
		if ok {
			return waited, nil
		}
		waited = true
		t := time.NewTimer(time.Second)
		select {
		case <-s.flow:
		case <-t.C:
		case <-ctx.Done():
			t.Stop()
			return waited, ctx.Err()
		}
		t.Stop()
	}
}

// pump owns the capturer and the tiler: it opens the selected monitor, sends
// HELLO, then captures and sends frames within the flow window until ctx
// ends. It returns an error only when capture cannot continue at all.
func (s *session) pump(ctx context.Context) error {
	// Capture objects stay on one OS thread for their whole life.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()

	tiler := NewTiler(s.o.Workers)
	var capt Capturer
	defer func() {
		if capt != nil {
			_ = capt.Close()
		}
	}()
	current := -1
	autoQ := 70
	fast := 0
	unavailable := false
	var lastPos *CursorState
	var lastSent time.Time

	open := func(i int) error {
		if capt != nil {
			_ = capt.Close()
			capt = nil
		}
		s.mu.Lock()
		m := s.monitors[i]
		s.mu.Unlock()
		c, err := s.p.OpenCapturer(m)
		if err != nil {
			return err
		}
		capt = c
		s.log.Info("capturing", "monitor", m.ID, "via", c.Kind())
		tiler.Invalidate()
		lastPos = nil
		return nil
	}

	for {
		s.mu.Lock()
		target := s.selected
		if s.want >= 0 {
			target, s.want = s.want, -1
		}
		s.mu.Unlock()
		if target != current || capt == nil {
			if err := open(target); err != nil {
				if errors.Is(err, ErrDesktopUnavailable) {
					if !unavailable {
						unavailable = true
						_ = s.state(rsp.StateBody{State: "input_desktop_unavailable"})
					}
					if sleepCtx(ctx, time.Second) != nil {
						return nil
					}
					continue
				}
				if current < 0 {
					return fmt.Errorf("open capture: %w", err)
				}
				s.log.Warn("could not switch monitor", "err", err)
				if open(current) != nil {
					return fmt.Errorf("reopen capture: %w", err)
				}
				target = current
			}
			unavailable = false
			current = target
			s.mu.Lock()
			s.selected = current
			hello := rsp.HelloBody{Version: 1, Capabilities: Capabilities, Monitors: s.monitors,
				Codecs: []string{"jpeg", "fill"}, Selected: s.monitors[current].ID}
			s.forceFull = true
			s.mu.Unlock()
			if err := s.send(func(seq uint32) ([]byte, error) { return rsp.EncodeJSON(rsp.Hello, seq, hello) }); err != nil {
				return nil
			}
		}

		waited, err := s.waitCredit(ctx)
		if err != nil {
			return nil
		}
		if d := s.o.MinFrameInterval - s.o.Now().Sub(lastSent); d > 0 {
			if sleepCtx(ctx, d) != nil {
				return nil
			}
		}

		upd, err := capt.Next(ctx, s.o.CaptureTimeout)
		if ctx.Err() != nil {
			return nil
		}
		if err != nil {
			s.log.Info("capture source lost; reopening", "err", err)
			_ = capt.Close()
			capt = nil
			continue
		}
		if upd.Shape != nil {
			hdr := rsp.CursorShapeHeader{HotX: uint16(upd.Shape.HotX), HotY: uint16(upd.Shape.HotY),
				W: uint16(upd.Shape.Image.Rect.Dx()), H: uint16(upd.Shape.Image.Rect.Dy()), Codec: rsp.CodecPNG}
			data := EncodeCursorPNG(upd.Shape.Image)
			if s.send(func(seq uint32) ([]byte, error) { return rsp.EncodeCursorShape(seq, hdr, data), nil }) != nil {
				return nil
			}
		}
		if c := upd.Cursor; c != nil && (lastPos == nil || *lastPos != *c) {
			cc := *c
			lastPos = &cc
			m := s.monitorAt(current)
			vis := c.Visible && c.X >= 0 && c.Y >= 0 && c.X < m.Width && c.Y < m.Height
			x, y := clamp(c.X, m.Width), clamp(c.Y, m.Height)
			if s.send(func(seq uint32) ([]byte, error) { return rsp.EncodeCursorPos(seq, uint16(x), uint16(y), vis), nil }) != nil {
				return nil
			}
		}

		s.mu.Lock()
		full := s.forceFull
		s.forceFull = false
		level := s.quality
		s.mu.Unlock()
		frame := upd.Frame
		if frame == nil {
			if !full {
				continue
			}
			frame = tiler.Snapshot()
			if frame == nil {
				s.mu.Lock()
				s.forceFull = true // wait for the first real frame
				s.mu.Unlock()
				continue
			}
		}

		q := qualityFor(level)
		if level == rsp.QualityAuto {
			if waited {
				autoQ, fast = max(40, autoQ-10), 0
			} else if fast++; fast > 30 && autoQ < 75 {
				autoQ, fast = autoQ+5, 0
			}
			q = autoQ
		}
		enc := tiler.Encode(frame, upd.Dirty, upd.Moves, full, q)
		if enc.Empty() {
			continue
		}
		if err := s.sendFrame(enc); err != nil {
			return nil
		}
		lastSent = s.o.Now()
	}
}

func (s *session) monitorAt(i int) rsp.Monitor {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.monitors[i]
}

func (s *session) sendFrame(enc Encoded) error {
	if err := s.send(func(seq uint32) ([]byte, error) { return rsp.Encode(rsp.FrameBegin, seq, nil), nil }); err != nil {
		return err
	}
	for _, c := range enc.Copies {
		if err := s.send(func(seq uint32) ([]byte, error) { return rsp.EncodeCopyRect(seq, c.SrcX, c.SrcY, c.Dst), nil }); err != nil {
			return err
		}
	}
	for _, t := range enc.Tiles {
		if err := s.send(func(seq uint32) ([]byte, error) { return rsp.EncodeTile(seq, t.Rect, t.Codec, t.Data), nil }); err != nil {
			return err
		}
	}
	var end uint32
	err := s.send(func(seq uint32) ([]byte, error) { end = seq; return rsp.Encode(rsp.FrameEnd, seq, nil), nil })
	if err != nil {
		return err
	}
	s.mu.Lock()
	if len(s.outstanding) == 0 {
		s.since = s.o.Now()
	}
	s.outstanding = append(s.outstanding, end)
	s.mu.Unlock()
	return nil
}

// send numbers and writes one frame. Seq and write happen under one lock so
// frames reach the pipe in seq order.
func (s *session) send(build func(seq uint32) ([]byte, error)) error {
	s.seqMu.Lock()
	defer s.seqMu.Unlock()
	s.seq++
	b, err := build(s.seq)
	if err != nil {
		return err
	}
	return s.w.Write(ipc.Frame, b)
}

func (s *session) state(b rsp.StateBody) error {
	return s.send(func(seq uint32) ([]byte, error) { return rsp.EncodeJSON(rsp.State, seq, b) })
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// promptFor builds the consent text from signed params, stripped of anything
// that could make it lie on screen: control characters and bidi overrides.
func promptFor(cfg ipc.SessionConfig) Prompt {
	t := cfg.TimeoutS
	if t < 15 || t > 120 {
		t = 30
	}
	tech := Sanitize(cfg.Technician, 80)
	if tech == "" {
		tech = "A technician"
	}
	return Prompt{
		Technician: tech,
		Org:        Sanitize(cfg.Org, 80),
		Reason:     Sanitize(cfg.Reason, 200),
		Recorded:   cfg.Record,
		Control:    cfg.AllowInput,
		Timeout:    time.Duration(t) * time.Second,
	}
}

// Sanitize removes control and bidi-formatting characters, collapses
// whitespace and caps s at max runes.
func Sanitize(s string, max int) string {
	var b strings.Builder
	n := 0
	space := false
	for _, r := range s {
		if unicode.IsControl(r) || unicode.Is(unicode.Bidi_Control, r) || r == 0x200b || r == 0xfeff {
			r = ' '
		}
		if unicode.IsSpace(r) {
			if space || b.Len() == 0 {
				continue
			}
			space = true
			r = ' '
		} else {
			space = false
		}
		if n == max {
			break
		}
		b.WriteRune(r)
		n++
	}
	return strings.TrimSpace(b.String())
}
