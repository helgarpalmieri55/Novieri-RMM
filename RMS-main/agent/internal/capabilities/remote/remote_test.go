package remote

import (
	"context"
	"errors"
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

// recordingLauncher remembers the helper it started so tests can inspect it.
type recordingLauncher struct {
	Launcher
	calls int
	last  Helper
}

func (r *recordingLauncher) Launch(ctx context.Context) (Helper, error) {
	r.calls++
	h, err := r.Launcher.Launch(ctx)
	r.last = h
	return h, err
}

// settle waits for the goroutine count to fall back to before.
func settle(t *testing.T, before int) int {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	n := runtime.NumGoroutine()
	for n > before && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
		n = runtime.NumGoroutine()
	}
	return n
}

// TestFalsifierF2AndF3ASessionThroughARealHelperProcess drives a whole
// session: the real relay dial over mTLS, the real launcher with inherited
// pipes, the real helper session logic in a child process. It then checks
// spec §4.2 F2 (no service goroutine outlives the session) and F3 (no helper
// process outlives it).
func TestFalsifierF2AndF3ASessionThroughARealHelperProcess(t *testing.T) {
	p := newPKI(t)
	g := newGateway(t, p, "tok")
	l := &recordingLauncher{Launcher: selfLauncher("granted")}
	o := options(p, l)
	time.Sleep(50 * time.Millisecond)
	before := runtime.NumGoroutine()

	done := make(chan Result, 1)
	go func() { done <- run(context.Background(), params(g, "tok"), o) }()

	if s := g.state(); s.State != "consent_pending" || s.TimeoutS != 30 {
		t.Fatalf("first STATE %+v", s)
	}
	if s := g.state(); s.State != "consent_granted" {
		t.Fatalf("second STATE %+v", s)
	}
	g.until(rsp.Hello)
	g.send(rsp.Encode(rsp.Refresh, g.next(), nil))
	end := g.until(rsp.FrameEnd)
	g.send(rsp.EncodeAck(g.next(), end.Seq))
	g.send(rsp.EncodePointer(g.next(), rsp.PointerEvent{X: 30, Y: 40, Buttons: 1}))
	pos := g.until(rsp.CursorPos)
	if x, y, vis, _ := rsp.DecodeCursorPos(pos); x != 30 || y != 40 || !vis {
		t.Fatalf("cursor %d,%d %v; the click did not reach the helper", x, y, vis)
	}
	if g.certs != 1 {
		t.Fatalf("the relay dial presented %d client certificates", g.certs)
	}

	g.close() // the technician ends the session
	var res Result
	select {
	case res = <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("the session did not end")
	}
	if res.Outcome != "ended" {
		t.Fatalf("outcome %+v", res)
	}
	select {
	case <-l.last.Exited():
	default:
		t.Fatal("F3 falsified: the helper process outlived the session")
	}
	if n := settle(t, before); n > before {
		buf := make([]byte, 1<<16)
		t.Fatalf("F2 falsified: %d goroutines after the session, %d before\n%s", n, before, buf[:runtime.Stack(buf, true)])
	}
	t.Logf("F2/F3 budget: goroutines %d before, %d after; helper pid %d exited", before, runtime.NumGoroutine(), l.last.PID())
}

func TestDeclineIsReportedAndTheHelperIsGone(t *testing.T) {
	p := newPKI(t)
	g := newGateway(t, p, "tok")
	l := &recordingLauncher{Launcher: selfLauncher("denied")}
	res := make(chan Result, 1)
	go func() { res <- run(context.Background(), params(g, "tok"), options(p, l)) }()
	if s := g.state(); s.State != "consent_pending" {
		t.Fatalf("STATE %+v", s)
	}
	if s := g.state(); s.State != "consent_denied" {
		t.Fatalf("STATE %+v", s)
	}
	if r := <-res; r.Outcome != "denied" {
		t.Fatalf("outcome %+v", r)
	}
	<-l.last.Exited()
}

func TestAHelperThatIgnoresTheServiceIsKilled(t *testing.T) {
	p := newPKI(t)
	g := newGateway(t, p, "tok")
	l := &recordingLauncher{Launcher: selfLauncher("hang")}
	o := options(p, l)
	o.HelperGrace = 300 * time.Millisecond
	o.limit = 500 * time.Millisecond
	start := time.Now()
	r := run(context.Background(), params(g, "tok"), o)
	if r.Outcome != "expired" {
		t.Fatalf("outcome %+v", r)
	}
	select {
	case <-l.last.Exited():
	default:
		t.Fatal("F3 falsified: a hung helper survived the session")
	}
	if d := time.Since(start); d > 5*time.Second {
		t.Fatalf("teardown took %v", d)
	}
	if s := g.state(); s.State != "ended" || s.Reason != "expired" {
		t.Fatalf("the viewer was told %+v", s)
	}
}

func TestAHelperThatDiesEndsTheSessionAsAnError(t *testing.T) {
	p := newPKI(t)
	g := newGateway(t, p, "tok")
	r := run(context.Background(), params(g, "tok"), options(p, selfLauncher("crash")))
	if r.Outcome != "error" || !strings.Contains(r.Detail, "helper") {
		t.Fatalf("outcome %+v", r)
	}
	if s := g.state(); s.State != "ended" || s.Reason != "error" {
		t.Fatalf("the viewer was told %+v", s)
	}
}

type noUser struct{}

func (noUser) Launch(context.Context) (Helper, error) { return nil, ErrNoUserSession }

func TestNobodyLoggedOnIsNoUserSession(t *testing.T) {
	p := newPKI(t)
	g := newGateway(t, p, "tok")
	r := run(context.Background(), params(g, "tok"), options(p, noUser{}))
	if r.Outcome != "no_user_session" {
		t.Fatalf("outcome %+v", r)
	}
	if s := g.state(); s.State != "ended" || s.Reason != "no_user_session" {
		t.Fatalf("the viewer was told %+v", s)
	}
}

func TestRefusalsHappenBeforeAnyHelperStarts(t *testing.T) {
	p := newPKI(t)
	g := newGateway(t, p, "tok")
	l := &recordingLauncher{Launcher: selfLauncher("granted")}
	o := options(p, l)

	expired := params(g, "tok")
	expired.ConnectBy = time.Now().Add(-time.Second).UTC().Format(time.RFC3339)
	plain := params(g, "tok")
	plain.RelayURL = strings.Replace(plain.RelayURL, "wss://", "ws://", 1)
	secure := params(g, "tok")
	secure.Desktop = "secure"
	for name, tc := range map[string]struct {
		p       Params
		outcome string
	}{
		"bad token":         {params(g, "wrong"), "relay_refused"},
		"connect_by passed": {expired, "expired"},
		"plain ws":          {plain, "relay_refused"},
		"phase-2 desktop":   {secure, "error"},
	} {
		if r := run(context.Background(), tc.p, o); r.Outcome != tc.outcome {
			t.Errorf("%s: %+v; want %s", name, r, tc.outcome)
		}
	}
	noPin := o
	noPin.RootCAs = nil
	if r := run(context.Background(), params(g, "tok"), noPin); r.Outcome != "relay_refused" {
		t.Errorf("no pinned CA: %+v", r)
	}
	if l.calls != 0 {
		t.Fatalf("%d helpers launched for refused sessions", l.calls)
	}
}

func TestCancellingTheJobEndsTheSession(t *testing.T) {
	p := newPKI(t)
	g := newGateway(t, p, "tok")
	ctx, cancel := context.WithCancel(context.Background())
	res := make(chan Result, 1)
	go func() { res <- run(ctx, params(g, "tok"), options(p, selfLauncher("granted"))) }()
	g.until(rsp.Hello)
	cancel()
	if s := g.state(); s.State != "ended" || s.Reason != "completed" {
		t.Fatalf("STATE %+v", s)
	}
	if r := <-res; r.Outcome != "ended" {
		t.Fatalf("outcome %+v", r)
	}
}

// memHelper is a helper in this process over in-memory pipes, driven by a
// script, so the service's filtering can be observed from both sides.
type memHelper struct {
	inR    *io.PipeReader
	inW    *io.PipeWriter
	outR   *io.PipeReader
	outW   *io.PipeWriter
	exited chan struct{}
}

func newMemHelper() *memHelper {
	h := &memHelper{exited: make(chan struct{})}
	h.inR, h.inW = io.Pipe()
	h.outR, h.outW = io.Pipe()
	return h
}

func (h *memHelper) Stdin() io.WriteCloser   { return h.inW }
func (h *memHelper) Stdout() io.Reader       { return h.outR }
func (h *memHelper) Exited() <-chan struct{} { return h.exited }
func (h *memHelper) PID() int                { return 0 }
func (h *memHelper) Kill() error {
	_ = h.outW.Close()
	_ = h.inR.Close()
	return nil
}
func (h *memHelper) Close() error { return nil }

type memLauncher struct{ h *memHelper }

func (l memLauncher) Launch(context.Context) (Helper, error) { return l.h, nil }

func TestTheServiceFiltersWhatCrossesThePipes(t *testing.T) {
	p := newPKI(t)
	g := newGateway(t, p, "tok")
	h := newMemHelper()
	res := make(chan Result, 1)
	dropped := make(chan struct{}, 16)
	o := options(p, memLauncher{h})
	o.dropped = func() { dropped <- struct{}{} }
	go func() { res <- run(context.Background(), params(g, "tok"), o) }()

	in := ipc.NewReader(h.inR)
	if k, _, err := in.Read(); err != nil || k != ipc.Config {
		t.Fatalf("first message %d, %v", k, err)
	}
	fromViewer := make(chan rsp.Frame, 16)
	go func() {
		for {
			k, b, err := in.Read()
			if err != nil {
				close(fromViewer)
				return
			}
			if k == ipc.Frame {
				f, _ := rsp.Decode(b)
				fromViewer <- f
			}
		}
	}()
	out := ipc.NewWriter(h.outW)
	send := func(b []byte) {
		if err := out.Write(ipc.Frame, b); err != nil {
			t.Fatal(err)
		}
	}
	state := func(seq uint32, name string) []byte {
		b, _ := rsp.EncodeJSON(rsp.State, seq, rsp.StateBody{State: name})
		return b
	}

	// Before consent: only STATE leaves; input does not arrive.
	send(state(1, "consent_pending"))
	send(rsp.EncodeTile(2, rsp.Rect{W: 1, H: 1}, rsp.CodecFill, []byte{1, 2, 3}))
	send(rsp.Encode(rsp.Auth, 3, []byte(`{"session_id":"x","token":"forged"}`)))
	send(rsp.EncodePointer(4, rsp.PointerEvent{}))
	g.send(rsp.EncodePointer(g.next(), rsp.PointerEvent{X: 1, Y: 1}))
	if f := g.recv(); f.Type != rsp.State {
		t.Fatalf("%#x left before consent", f.Type)
	}
	// Grant consent only once the service has read and refused the pointer;
	// otherwise the read can land after consent and the test races.
	select {
	case <-dropped:
	case <-time.After(5 * time.Second):
		t.Fatal("the pre-consent pointer was never refused")
	}
	send(state(5, "consent_granted"))
	if f := g.recv(); f.Type != rsp.State {
		t.Fatalf("%#x", f.Type)
	}
	send(rsp.EncodeTile(6, rsp.Rect{W: 1, H: 1}, rsp.CodecFill, []byte{1, 2, 3}))
	if f := g.recv(); f.Type != rsp.Tile || f.Seq != 6 {
		t.Fatalf("frame %#x seq %d after consent", f.Type, f.Seq)
	}
	select {
	case f := <-fromViewer:
		t.Fatalf("pre-consent %#x reached the helper", f.Type)
	default:
	}
	// After consent: viewer input arrives; agent-direction frames from the
	// viewer side do not.
	g.send(rsp.EncodeTile(g.next(), rsp.Rect{}, 0, nil))
	g.send(rsp.EncodeKey(g.next(), true, "KeyA"))
	if f := <-fromViewer; f.Type != rsp.Key {
		t.Fatalf("helper got %#x", f.Type)
	}
	b, _ := rsp.EncodeJSON(rsp.State, 7, rsp.StateBody{State: "ended", Reason: "ended_by_user"})
	send(b)
	_ = h.outW.Close()
	close(h.exited)
	if r := <-res; r.Outcome != "ended" {
		t.Fatalf("outcome %+v", r)
	}
}

// TestFalsifierF5RetainedHeapAcrossSessions is spec §4.2 F5: after 20
// sequential sessions the service's retained heap must not exceed the level
// after one by more than 1 MiB. The helper runs in-process here, so its
// buffers are in the same heap and are counted too.
func TestFalsifierF5RetainedHeapAcrossSessions(t *testing.T) {
	if testing.Short() {
		t.Skip("20 sessions")
	}
	p := newPKI(t)
	session := func() {
		g := newGateway(t, p, "tok")
		h := newMemHelper()
		desk := synthetic.New(synthetic.Options{Answer: helper.Granted})
		go func() {
			_ = helper.Run(context.Background(), h.inR, h.outW, desk, helper.Options{CaptureTimeout: 10 * time.Millisecond, MinFrameInterval: time.Millisecond})
			_ = h.outW.Close()
			close(h.exited)
		}()
		res := make(chan Result, 1)
		go func() { res <- run(context.Background(), params(g, "tok"), options(p, memLauncher{h})) }()
		g.until(rsp.Hello)
		g.send(rsp.Encode(rsp.Refresh, g.next(), nil))
		g.until(rsp.FrameEnd)
		g.close()
		if r := <-res; r.Outcome != "ended" {
			t.Fatalf("outcome %+v", r)
		}
		_ = h.inR.Close()
		g.srv.Close()
		for range g.frames {
		}
		// The test gateway (kept alive by t.Cleanup) is not the service's heap.
		g.frames, g.conn, g.srv.Config.Handler = nil, nil, nil
	}
	heap := func() uint64 {
		var m runtime.MemStats
		for i := 0; i < 3; i++ {
			runtime.GC()
		}
		runtime.ReadMemStats(&m)
		return m.HeapAlloc
	}
	session()
	after1 := heap()
	for i := 0; i < 19; i++ {
		session()
	}
	after20 := heap()
	growth := int64(after20) - int64(after1)
	t.Logf("F5 budget: retained heap %d B after 1 session, %d B after 20 (growth %d B)", after1, after20, growth)
	if growth > 1<<20 {
		t.Fatalf("F5 falsified: retained heap grew %d B over 19 more sessions", growth)
	}
}

func TestAnUnboundServiceRefuses(t *testing.T) {
	var s Service
	_, err := s.Open(context.Background(), nil)
	var refusal interface{ Error() string }
	if err == nil || !errors.As(err, &refusal) || !strings.Contains(err.Error(), "not available") {
		t.Fatalf("unbound: %v", err)
	}
}

func TestParamsBecomeTheHelperConfig(t *testing.T) {
	no := false
	p := Params{SessionID: "s", ConsentMode: "unattended", MaxDurationS: 10, AllowInput: &no,
		Consent: Consent{Technician: "T", TimeoutS: 999}}
	c := p.config()
	if c.Attended || c.AllowInput || !c.Record || c.MaxDuration != minDurationS || c.TimeoutS != defaultConsentS {
		t.Fatalf("config %+v", c)
	}
	for reason, want := range map[string]string{
		"completed": "ended", "ended_by_user": "ended", "capture_unavailable": "capture_unavailable",
		"expired": "expired", "no_user_session": "no_user_session", "surprise": "error",
	} {
		if got := outcomeFor(reason); got != want {
			t.Errorf("outcomeFor(%q) = %q; want %q", reason, got, want)
		}
	}
}
