//go:build windows

package helper

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"
	"unsafe"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// These run on the windows-latest CI runner against its real desktop. Each
// one skips, saying why, when the runner has no interactive desktop; the log
// names which capture path ran.

var (
	procGetCursorPos     = user32.NewProc("GetCursorPos")
	procGetAsyncKeyState = user32.NewProc("GetAsyncKeyState")
)

func discard() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func primaryMonitor(t *testing.T) rsp.Monitor {
	t.Helper()
	setDPIAware()
	ms, err := (&winPlatform{}).Monitors()
	if err != nil || len(ms) == 0 {
		t.Skipf("no monitors on this runner: %v", err)
	}
	for _, m := range ms {
		if m.Primary {
			t.Logf("primary monitor %+v of %d", m, len(ms))
			return m
		}
	}
	return ms[0]
}

func nonBlack(f *Frame) int {
	n := 0
	for i := 0; i+3 < len(f.Pix); i += 4 {
		if f.Pix[i]|f.Pix[i+1]|f.Pix[i+2] != 0 {
			n++
		}
	}
	return n
}

func TestGDICapturesTheDesktop(t *testing.T) {
	m := primaryMonitor(t)
	c, err := openGDI(m)
	if err != nil {
		t.Skipf("GDI capture unavailable: %v", err)
	}
	defer c.Close()
	u, err := c.Next(context.Background(), time.Second)
	if errors.Is(err, ErrReopen) {
		t.Skip("BitBlt refused: no interactive desktop on this runner")
	}
	if err != nil || u.Frame == nil {
		t.Fatalf("Next: %+v, %v", u, err)
	}
	if u.Frame.Width != m.Width || u.Frame.Height != m.Height || len(u.Frame.Pix) != m.Width*m.Height*4 {
		t.Fatalf("frame %dx%d (%d bytes) for monitor %+v", u.Frame.Width, u.Frame.Height, len(u.Frame.Pix), m)
	}
	t.Logf("gdi: %dx%d, %d non-black pixels", u.Frame.Width, u.Frame.Height, nonBlack(u.Frame))
	// The second poll waits out the 15 fps cap. The cap spaces the polls'
	// copies, so measure between the two copies, never from here: the first
	// frame's BitBlt and the pixel count above already used part of the
	// interval, and on a slow runner that left a timer started here under half
	// of it (main, 2026-10-02: 33.2ms against a bound of 33.3ms) while the cap
	// held exactly.
	first := c.last
	if _, err := c.Next(context.Background(), time.Second); err != nil {
		t.Fatal(err)
	}
	if d := c.last.Sub(first); d < gdiInterval {
		t.Fatalf("second GDI poll %v after the first; the cap is %v", d, gdiInterval)
	}
}

func TestDXGICapturesTheDesktop(t *testing.T) {
	m := primaryMonitor(t)
	c, err := openDXGI(m)
	if err != nil {
		t.Skipf("desktop duplication unavailable on this runner (the GDI fallback covers it): %v", err)
	}
	defer c.Close()
	var u Update
	deadline := time.Now().Add(5 * time.Second)
	for u.Frame == nil && time.Now().Before(deadline) {
		if u, err = c.Next(context.Background(), 500*time.Millisecond); err != nil {
			t.Fatalf("Next: %v", err)
		}
	}
	if u.Frame == nil {
		t.Skip("duplication produced no frame in 5 s (nothing presented on this runner)")
	}
	if u.Frame.Width != m.Width || u.Frame.Height != m.Height {
		t.Fatalf("frame %dx%d for monitor %+v", u.Frame.Width, u.Frame.Height, m)
	}
	t.Logf("dxgi: %dx%d, %d non-black pixels, %d dirty rects, %d moves", u.Frame.Width, u.Frame.Height,
		nonBlack(u.Frame), len(u.Dirty), len(u.Moves))
	// A frame through the real tiler reconstructs.
	enc := NewTiler(4).Encode(u.Frame, nil, nil, true, 70)
	if len(enc.Tiles) == 0 {
		t.Fatal("no tiles from a full frame")
	}
}

func TestSendInputMovesThePointer(t *testing.T) {
	m := primaryMonitor(t)
	inj, err := (&winPlatform{}).OpenInjector(m)
	if err != nil {
		t.Fatal(err)
	}
	if err := inj.Pointer(123, 77, 0); err != nil {
		t.Skipf("SendInput refused (no interactive input on this runner): %v", err)
	}
	time.Sleep(100 * time.Millisecond)
	var p point
	if r, _, _ := procGetCursorPos.Call(uintptr(unsafe.Pointer(&p))); r == 0 {
		t.Skip("GetCursorPos failed: no interactive desktop")
	}
	dx, dy := int(p.X)-(m.X+123), int(p.Y)-(m.Y+77)
	if dx < -1 || dx > 1 || dy < -1 || dy > 1 {
		t.Fatalf("pointer at %d,%d; want %d,%d", p.X, p.Y, m.X+123, m.Y+77)
	}
}

func TestSendInputPressesAndReleasesAKey(t *testing.T) {
	m := primaryMonitor(t)
	inj, _ := (&winPlatform{}).OpenInjector(m)
	shift, _ := LookupKey("ShiftLeft")
	const vkLShift = 0xA0
	down := func() bool {
		r, _, _ := procGetAsyncKeyState.Call(vkLShift)
		return r&0x8000 != 0
	}
	if err := inj.Key(shift, true); err != nil {
		t.Skipf("SendInput refused: %v", err)
	}
	time.Sleep(50 * time.Millisecond)
	wasDown := down()
	if err := inj.Key(shift, false); err != nil {
		t.Fatal(err)
	}
	time.Sleep(50 * time.Millisecond)
	if !wasDown {
		t.Skip("the key state did not change: input is not delivered to this runner's desktop")
	}
	if down() {
		t.Fatal("ShiftLeft still down after its release")
	}
}

func TestTheConsentPromptTimesOutAsDeclined(t *testing.T) {
	ui := &winUI{log: discard()}
	start := time.Now()
	a := ui.Consent(context.Background(), Prompt{Technician: "CI", Org: "Test", Reason: "timeout test", Timeout: time.Second})
	if a != TimedOut {
		t.Fatalf("answer %v; want TimedOut", a)
	}
	if d := time.Since(start); d > 5*time.Second {
		t.Fatalf("timeout took %v", d)
	}
}

func TestACancelledConsentPromptIsADenial(t *testing.T) {
	ui := &winUI{log: discard()}
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	if a := ui.Consent(ctx, Prompt{Technician: "CI", Timeout: time.Minute}); a != Denied {
		t.Fatalf("answer %v; want Denied", a)
	}
}

func TestTheIndicatorOpensAndCloses(t *testing.T) {
	m := primaryMonitor(t)
	ui := &winUI{log: discard()}
	ind, err := ui.ShowIndicator(Prompt{Technician: "CI", Org: "Test", Recorded: true}, m, true)
	if err != nil {
		t.Fatal(err)
	}
	ind.SetControlling(false)
	time.Sleep(100 * time.Millisecond)
	done := make(chan struct{})
	go func() { ind.Close(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the indicator did not close")
	}
}

func TestTheHelperRefusesToStartFromAConsole(t *testing.T) {
	if err := checkLaunch(); err == nil {
		t.Fatal("checkLaunch accepted a process the agent did not start")
	}
}
