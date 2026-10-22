package helper_test

import (
	"context"
	"io"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/helper"
	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/helper/synthetic"
	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/ipc"
	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// harness plays the service's side of the pipes against an in-process helper.
type harness struct {
	t      *testing.T
	desk   *synthetic.Desktop
	in     *io.PipeWriter
	w      *ipc.Writer
	frames chan rsp.Frame
	done   chan error
	seq    uint32
	canvas synthetic.Canvas
}

func attended() ipc.SessionConfig {
	return ipc.SessionConfig{SessionID: "s1", Attended: true, Technician: "Ann Tech", Org: "Toeshee",
		Reason: "INC-42 printer", TimeoutS: 30, AllowInput: true, Record: true}
}

func start(t *testing.T, cfg ipc.SessionConfig, o synthetic.Options) *harness {
	t.Helper()
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	h := &harness{t: t, desk: synthetic.New(o), in: inW, w: ipc.NewWriter(inW),
		frames: make(chan rsp.Frame, 1024), done: make(chan error, 1)}
	go func() {
		err := helper.Run(context.Background(), inR, outW, h.desk, helper.Options{
			CaptureTimeout: 20 * time.Millisecond, MinFrameInterval: time.Millisecond, Workers: 2,
		})
		_ = outW.Close()
		_ = inR.Close()
		h.done <- err
	}()
	go func() {
		defer close(h.frames)
		r := ipc.NewReader(outR)
		for {
			k, body, err := r.Read()
			if err != nil {
				return
			}
			if k != ipc.Frame {
				t.Errorf("helper sent ipc kind %d", k)
				continue
			}
			f, err := rsp.Decode(body)
			if err != nil || !rsp.AgentToViewer(f.Type) {
				t.Errorf("helper sent %x", body)
				continue
			}
			h.frames <- f
		}
	}()
	if err := h.w.WriteJSON(ipc.Config, cfg); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = inW.Close() })
	return h
}

func (h *harness) send(b []byte) {
	h.t.Helper()
	if err := h.w.Write(ipc.Frame, b); err != nil {
		h.t.Fatal(err)
	}
}

func (h *harness) next() uint32 { h.seq++; return h.seq }

// recv returns the next frame, applying it to the canvas.
func (h *harness) recv() rsp.Frame {
	h.t.Helper()
	select {
	case f, ok := <-h.frames:
		if !ok {
			h.t.Fatal("helper closed its output")
		}
		if err := h.canvas.Apply(f); err != nil {
			h.t.Fatal(err)
		}
		return f
	case <-time.After(5 * time.Second):
		h.t.Fatal("no frame from the helper")
	}
	return rsp.Frame{}
}

func (h *harness) expectState(name string) rsp.StateBody {
	h.t.Helper()
	for {
		f := h.recv()
		if f.Type == rsp.State {
			var s rsp.StateBody
			_ = rsp.DecodeJSON(f, &s)
			if s.State != name {
				h.t.Fatalf("STATE %q; want %q", s.State, name)
			}
			return s
		}
		if f.Type == rsp.FrameBegin || f.Type == rsp.Tile || f.Type == rsp.FrameEnd || f.Type == rsp.CursorPos || f.Type == rsp.CopyRect {
			continue
		}
		h.t.Fatalf("frame %#x while waiting for STATE %q", f.Type, name)
	}
}

func (h *harness) expect(t rsp.Type) rsp.Frame {
	h.t.Helper()
	f := h.recv()
	if f.Type != t {
		h.t.Fatalf("frame %#x; want %#x", f.Type, t)
	}
	return f
}

// frameEnd reads until the next FRAME_END and returns its seq.
func (h *harness) frameEnd() uint32 {
	h.t.Helper()
	for {
		if f := h.recv(); f.Type == rsp.FrameEnd {
			return f.Seq
		}
	}
}

// quiet asserts that nothing arrives for d.
func (h *harness) quiet(d time.Duration) {
	h.t.Helper()
	select {
	case f := <-h.frames:
		h.t.Fatalf("unexpected frame %#x", f.Type)
	case <-time.After(d):
	}
}

func (h *harness) finished() {
	h.t.Helper()
	select {
	case err := <-h.done:
		if err != nil {
			h.t.Fatalf("Run: %v", err)
		}
	case <-time.After(5 * time.Second):
		h.t.Fatal("helper did not end")
	}
}

func (h *harness) screen(t *testing.T) []byte {
	t.Helper()
	// The desktop as the capturer sees it, via a fresh capturer.
	ms, _ := h.desk.Monitors()
	c, _ := h.desk.OpenCapturer(ms[h.canvas.Hello.Selected])
	u, err := c.Next(context.Background(), time.Millisecond)
	if err != nil || u.Frame == nil {
		t.Fatalf("capture: %v", err)
	}
	return u.Frame.Pix
}

func TestAnAttendedSessionAsksThenStreamsTakesInputAndEndsWhenTheUserSaysSo(t *testing.T) {
	h := start(t, attended(), synthetic.Options{Answer: helper.Granted})
	if s := h.expectState("consent_pending"); s.TimeoutS != 30 {
		t.Fatalf("consent_pending timeout_s = %d", s.TimeoutS)
	}
	h.expectState("consent_granted")
	h.expect(rsp.Hello)
	if hl := h.canvas.Hello; hl.Version != 1 || len(hl.Monitors) != 1 || hl.Monitors[0].Width != 640 || hl.Selected != 0 {
		t.Fatalf("HELLO %+v", hl)
	}
	// Nothing is drawn until the viewer asks: it may not be attached yet.
	h.quiet(150 * time.Millisecond)

	h.send(rsp.Encode(rsp.Refresh, h.next(), nil))
	end := h.frameEnd()
	if e := h.canvas.MeanError(h.screen(t), 640*4); e > 3 {
		t.Fatalf("first frame differs from the desktop by %.2f", e)
	}
	h.send(rsp.EncodeAck(h.next(), end))

	// A click moves the pointer and leaves a mark the next frame carries.
	h.send(rsp.EncodePointer(h.next(), rsp.PointerEvent{X: 100, Y: 50, Buttons: 1}))
	h.send(rsp.EncodePointer(h.next(), rsp.PointerEvent{X: 100, Y: 50, Buttons: 0}))
	end = h.frameEnd()
	h.send(rsp.EncodeAck(h.next(), end))
	if h.canvas.CursorX != 100 || h.canvas.CursorY != 50 || !h.canvas.CursorVis {
		t.Fatalf("cursor at %d,%d visible=%v", h.canvas.CursorX, h.canvas.CursorY, h.canvas.CursorVis)
	}
	if p := h.canvas.Img.RGBAAt(104, 54); p.R < 180 || p.G > 70 {
		t.Fatalf("the click mark is not on the viewer's canvas: %+v", p)
	}
	h.send(rsp.EncodeKey(h.next(), true, "KeyA"))
	h.send(rsp.EncodeKey(h.next(), false, "KeyA"))
	h.send(rsp.EncodeText(h.next(), "héllo"))

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	time.Sleep(50 * time.Millisecond)
	if !h.desk.Press(ctx, helper.EndSession) {
		t.Fatal("indicator never shown")
	}
	if s := h.expectState("ended"); s.Reason != "ended_by_user" {
		t.Fatalf("ended reason %q", s.Reason)
	}
	h.finished()

	var kinds []string
	for _, e := range h.desk.Events() {
		kinds = append(kinds, e.Kind)
	}
	if got := strings.Join(kinds, ","); got != "pointer,pointer,key,key,text" {
		t.Fatalf("injected %s", got)
	}
	p := h.desk.Prompts()
	if len(p) != 1 || p[0].Technician != "Ann Tech" || p[0].Org != "Toeshee" || !p[0].Recorded || p[0].Timeout != 30*time.Second {
		t.Fatalf("prompt %+v", p)
	}
}

func TestDeclineAndTimeoutEndBeforeAnyPixel(t *testing.T) {
	for _, tc := range []struct {
		answer helper.Answer
		state  string
	}{{helper.Denied, "consent_denied"}, {helper.TimedOut, "consent_timeout"}} {
		h := start(t, attended(), synthetic.Options{Answer: tc.answer})
		h.expectState("consent_pending")
		h.expectState(tc.state)
		h.finished()
		if f, ok := <-h.frames; ok {
			t.Fatalf("%s: frame %#x after the answer", tc.state, f.Type)
		}
		if h.desk.IndicatorShown() != 0 {
			t.Fatalf("%s: indicator shown", tc.state)
		}
	}
}

func TestInputBeforeConsentIsNeverInjected(t *testing.T) {
	h := start(t, attended(), synthetic.Options{Answer: helper.Granted, AnswerAfter: 200 * time.Millisecond})
	h.expectState("consent_pending")
	h.send(rsp.EncodePointer(h.next(), rsp.PointerEvent{X: 1, Y: 1, Buttons: 1}))
	h.send(rsp.EncodeKey(h.next(), true, "KeyA"))
	h.expectState("consent_granted")
	h.expect(rsp.Hello)
	time.Sleep(50 * time.Millisecond)
	if ev := h.desk.Events(); len(ev) != 0 {
		t.Fatalf("injected before consent: %+v", ev)
	}
}

func TestUnattendedShowsNoPromptButStillShowsTheIndicator(t *testing.T) {
	cfg := attended()
	cfg.Attended = false
	h := start(t, cfg, synthetic.Options{})
	h.expect(rsp.Hello)
	if len(h.desk.Prompts()) != 0 {
		t.Fatal("unattended showed a prompt")
	}
	if h.desk.IndicatorShown() != 1 {
		t.Fatal("no indicator in an unattended session")
	}
}

func TestStopControlIsFinalForTheSession(t *testing.T) {
	h := start(t, attended(), synthetic.Options{Answer: helper.Granted})
	h.expectState("consent_pending")
	h.expectState("consent_granted")
	h.expect(rsp.Hello)
	if !h.desk.Controlling() {
		t.Fatal("indicator says viewing in a session that allows input")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	h.desk.Press(ctx, helper.StopControl)
	h.expectState("control_revoked_by_user")
	h.send(rsp.EncodeControl(h.next(), true)) // the technician tries to take it back
	h.send(rsp.EncodePointer(h.next(), rsp.PointerEvent{X: 5, Y: 5, Buttons: 1}))
	time.Sleep(100 * time.Millisecond)
	if ev := h.desk.Events(); len(ev) != 0 {
		t.Fatalf("input after Stop control: %+v", ev)
	}
	if h.desk.Controlling() {
		t.Fatal("indicator still says controlling")
	}
}

func TestTheTechnicianCanSwitchBetweenViewingAndControl(t *testing.T) {
	h := start(t, attended(), synthetic.Options{Answer: helper.Granted})
	h.expectState("consent_pending")
	h.expectState("consent_granted")
	h.expect(rsp.Hello)
	h.send(rsp.EncodeControl(h.next(), false))
	h.send(rsp.EncodeKey(h.next(), true, "KeyQ"))
	h.send(rsp.EncodeControl(h.next(), true))
	h.send(rsp.EncodeKey(h.next(), true, "KeyW"))
	time.Sleep(100 * time.Millisecond)
	ev := h.desk.Events()
	if len(ev) != 1 || ev[0].Scan.Code != 0x11 {
		t.Fatalf("events %+v; want only KeyW", ev)
	}
}

func TestPolicyWithoutInputIsViewOnly(t *testing.T) {
	cfg := attended()
	cfg.AllowInput = false
	h := start(t, cfg, synthetic.Options{Answer: helper.Granted})
	h.expectState("consent_pending")
	h.expectState("consent_granted")
	h.expect(rsp.Hello)
	h.send(rsp.EncodeControl(h.next(), true))
	h.send(rsp.EncodeKey(h.next(), true, "KeyQ"))
	time.Sleep(100 * time.Millisecond)
	if ev := h.desk.Events(); len(ev) != 0 {
		t.Fatalf("injected with allow_input false: %+v", ev)
	}
}

func TestFlowControlHoldsFramesUntilAcknowledged(t *testing.T) {
	cfg := attended()
	cfg.Attended = false
	h := start(t, cfg, synthetic.Options{})
	h.expect(rsp.Hello)
	h.send(rsp.Encode(rsp.Refresh, h.next(), nil))
	// Keep the screen changing and never acknowledge.
	var ends []uint32
	deadline := time.After(600 * time.Millisecond)
	for i := 0; ; i++ {
		h.send(rsp.EncodePointer(h.next(), rsp.PointerEvent{X: uint16(20 * (i % 30)), Y: 100, Buttons: 1}))
		select {
		case f := <-h.frames:
			if f.Type == rsp.FrameEnd {
				ends = append(ends, f.Seq)
			}
			continue
		case <-deadline:
		case <-time.After(10 * time.Millisecond):
			continue
		}
		break
	}
	if len(ends) != 3 {
		t.Fatalf("%d frames without an ACK; the window is three", len(ends))
	}
	h.send(rsp.EncodeAck(h.next(), ends[2]))
	h.frameEnd() // the stream resumes
}

func TestAStopFromTheServiceReportsItsReason(t *testing.T) {
	h := start(t, attended(), synthetic.Options{Answer: helper.Granted})
	h.expectState("consent_pending")
	h.expectState("consent_granted")
	h.expect(rsp.Hello)
	if err := h.w.WriteJSON(ipc.Stop, ipc.StopBody{Reason: "expired"}); err != nil {
		t.Fatal(err)
	}
	if s := h.expectState("ended"); s.Reason != "expired" {
		t.Fatalf("reason %q", s.Reason)
	}
	h.finished()
}

func TestTheServiceGoingAwayEndsTheHelperSilently(t *testing.T) {
	h := start(t, attended(), synthetic.Options{Answer: helper.Granted})
	h.expectState("consent_pending")
	h.expectState("consent_granted")
	h.expect(rsp.Hello)
	_ = h.in.Close()
	h.finished()
	for f := range h.frames {
		if f.Type == rsp.State {
			t.Fatal("STATE after the service closed the pipe")
		}
	}
}

func TestMaxDurationEndsTheSessionAsExpired(t *testing.T) {
	cfg := attended()
	cfg.Attended = false
	cfg.MaxDuration = 1
	h := start(t, cfg, synthetic.Options{})
	h.expect(rsp.Hello)
	if s := h.expectState("ended"); s.Reason != "expired" {
		t.Fatalf("reason %q", s.Reason)
	}
	h.finished()
}

func TestMonitorSelectResendsHelloAndAFullFrame(t *testing.T) {
	cfg := attended()
	cfg.Attended = false
	h := start(t, cfg, synthetic.Options{Monitors: 2, Width: 320, Height: 200})
	h.expect(rsp.Hello)
	if len(h.canvas.Hello.Monitors) != 2 || h.canvas.Hello.Selected != 0 {
		t.Fatalf("HELLO %+v", h.canvas.Hello)
	}
	h.send(rsp.Encode(rsp.Refresh, h.next(), nil))
	h.send(rsp.EncodeAck(h.next(), h.frameEnd()))
	h.send(rsp.EncodeMonitorSelect(h.next(), 1))
	for {
		if f := h.recv(); f.Type == rsp.Hello {
			break
		}
	}
	if h.canvas.Hello.Selected != 1 {
		t.Fatalf("selected %d", h.canvas.Hello.Selected)
	}
	h.frameEnd()
	if e := h.canvas.MeanError(h.screen(t), 320*4); e > 3 {
		t.Fatalf("second monitor differs by %.2f", e)
	}
	// Input now lands on the second monitor.
	h.send(rsp.EncodePointer(h.next(), rsp.PointerEvent{X: 10, Y: 10}))
	time.Sleep(50 * time.Millisecond)
	ev := h.desk.Events()
	if len(ev) == 0 || ev[len(ev)-1].X != 10 {
		t.Fatalf("events %+v", ev)
	}
}

// TestFalsifierF7AStaticScreenSendsNothing is spec §4.2 F7: a static screen
// must not produce more than one frame per 10 s. The helper sends none:
// neither when the capturer blocks until something changes (DXGI) nor when
// it reports every poll as a frame (GDI).
func TestFalsifierF7AStaticScreenSendsNothing(t *testing.T) {
	for _, always := range []bool{false, true} {
		cfg := attended()
		cfg.Attended = false
		h := start(t, cfg, synthetic.Options{Frozen: true, AlwaysChanged: always})
		h.expect(rsp.Hello)
		h.send(rsp.Encode(rsp.Refresh, h.next(), nil))
		h.send(rsp.EncodeAck(h.next(), h.frameEnd()))
		// 1 s at a 20 ms capture timeout is ~50 polls of an unchanged screen.
		frames := 0
		timeout := time.After(time.Second)
	loop:
		for {
			select {
			case f := <-h.frames:
				frames++
				t.Logf("frame %#x", f.Type)
			case <-timeout:
				break loop
			}
		}
		t.Logf("F7 budget: polling=%v, %d frames in 1 s of a static screen", always, frames)
		if frames != 0 {
			t.Fatalf("F7 falsified: %d frames from a static screen", frames)
		}
	}
}

func TestRunLeavesNoGoroutineBehind(t *testing.T) {
	before := runtime.NumGoroutine()
	for i := 0; i < 3; i++ {
		h := start(t, attended(), synthetic.Options{Answer: helper.Granted})
		h.expectState("consent_pending")
		h.expectState("consent_granted")
		h.expect(rsp.Hello)
		h.send(rsp.Encode(rsp.Refresh, h.next(), nil))
		h.frameEnd()
		_ = h.w.WriteJSON(ipc.Stop, ipc.StopBody{Reason: "completed"})
		h.finished()
		_ = h.in.Close()
		for range h.frames {
		}
	}
	deadline := time.Now().Add(3 * time.Second)
	for runtime.NumGoroutine() > before && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if n := runtime.NumGoroutine(); n > before {
		buf := make([]byte, 1<<16)
		t.Fatalf("%d goroutines after three sessions, %d before\n%s", n, before, buf[:runtime.Stack(buf, true)])
	}
}
