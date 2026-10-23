package helper

import (
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"sync"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// winPlatform is the real desktop of the session the helper runs in.
type winPlatform struct {
	log *slog.Logger
	// forceGDI skips DXGI (tests exercise each path on its own).
	forceGDI bool
}

// NewPlatform returns the Windows platform.
func NewPlatform(log *slog.Logger) Platform { return &winPlatform{log: log} }

// setDPIAware makes coordinates physical pixels on every monitor; without it
// capture and SendInput disagree on scaled displays and DXGI duplication can
// refuse to start.
func setDPIAware() {
	const perMonitorAwareV2 = ^uintptr(3) // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (-4)
	if has(procSetProcessDpiAwarenessContext) {
		if r, _, _ := procSetProcessDpiAwarenessContext.Call(perMonitorAwareV2); r != 0 {
			return
		}
	}
	if has(procSetProcessDPIAware) {
		_, _, _ = procSetProcessDPIAware.Call()
	}
}

type monitorInfoEx struct {
	Size    uint32
	Monitor rect
	Work    rect
	Flags   uint32
	Device  [32]uint16
}

const monitorInfoPrimary = 1

// Monitors implements Platform. IDs are positions in a stable order (left to
// right, then top to bottom), so a viewer's MONITOR_SELECT names the same
// screen for the whole session.
func (p *winPlatform) Monitors() ([]rsp.Monitor, error) {
	if !has(procEnumDisplayMonitors, procGetMonitorInfoW) {
		return nil, errProc
	}
	enumMu.Lock()
	defer enumMu.Unlock()
	enumOut = nil
	enumOnce.Do(func() { enumCB = windows.NewCallback(enumMonitor) })
	if r, _, err := procEnumDisplayMonitors.Call(0, 0, enumCB, 0); r == 0 {
		return nil, fmt.Errorf("EnumDisplayMonitors: %w", err)
	}
	out := enumOut
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].X != out[j].X {
			return out[i].X < out[j].X
		}
		return out[i].Y < out[j].Y
	})
	for i := range out {
		out[i].ID = i
	}
	return out, nil
}

// The callback is created once: Windows callbacks are a finite per-process
// resource, and EnumDisplayMonitors calls back on the calling thread.
var (
	enumMu   sync.Mutex
	enumOnce sync.Once
	enumCB   uintptr
	enumOut  []rsp.Monitor
)

func enumMonitor(hmon, _, _, _ uintptr) uintptr {
	mi := monitorInfoEx{Size: uint32(unsafe.Sizeof(monitorInfoEx{}))}
	if r, _, _ := procGetMonitorInfoW.Call(hmon, uintptr(unsafe.Pointer(&mi))); r == 0 {
		return 1
	}
	enumOut = append(enumOut, rsp.Monitor{
		X: int(mi.Monitor.Left), Y: int(mi.Monitor.Top),
		Width: int(mi.Monitor.Right - mi.Monitor.Left), Height: int(mi.Monitor.Bottom - mi.Monitor.Top),
		DPI: monitorDPI(hmon), Primary: mi.Flags&monitorInfoPrimary != 0,
	})
	return 1
}

func monitorDPI(hmon uintptr) int {
	if has(procGetDpiForMonitor) {
		var x, y uint32
		if r, _, _ := procGetDpiForMonitor.Call(hmon, 0, uintptr(unsafe.Pointer(&x)), uintptr(unsafe.Pointer(&y))); !failed(r) && x > 0 {
			return int(x)
		}
	}
	return 96
}

func systemDPI() int {
	if has(procGetDpiForSystem) {
		if r, _, _ := procGetDpiForSystem.Call(); r > 0 {
			return int(r)
		}
	}
	return 96
}

func virtualScreen() VirtualScreen {
	m := func(i uintptr) int {
		r, _, _ := procGetSystemMetrics.Call(i)
		return int(int32(r))
	}
	return VirtualScreen{X: m(76), Y: m(77), W: m(78), H: m(79)}
}

// OpenCapturer implements Platform: DXGI Desktop Duplication, falling back to
// GDI when duplication is unavailable (no GPU driver support, a rotated
// output, an RDP session).
func (p *winPlatform) OpenCapturer(m rsp.Monitor) (Capturer, error) {
	if !p.forceGDI {
		c, err := openDXGI(m)
		if err == nil {
			return c, nil
		}
		if errors.Is(err, ErrDesktopUnavailable) {
			return nil, err
		}
		p.log.Info("desktop duplication unavailable; using GDI", "err", err)
	}
	return openGDI(m)
}

// OpenInjector implements Platform.
func (p *winPlatform) OpenInjector(m rsp.Monitor) (Injector, error) {
	if !has(procSendInput, procGetSystemMetrics) {
		return nil, errProc
	}
	return &sendInput{m: m, vs: virtualScreen()}, nil
}

// UI implements Platform.
func (p *winPlatform) UI() UI { return &winUI{log: p.log} }

// sendInput injects through SendInput. It runs as the logged-on user, so
// UIPI keeps it out of elevated windows and the secure desktop (spec §2.4).
type sendInput struct {
	m       rsp.Monitor
	vs      VirtualScreen
	buttons byte
}

func (s *sendInput) send(recs []winInput) error {
	if len(recs) == 0 {
		return nil
	}
	n, _, err := procSendInput.Call(uintptr(len(recs)), uintptr(unsafe.Pointer(&recs[0])), unsafe.Sizeof(recs[0]))
	if int(n) != len(recs) {
		return fmt.Errorf("SendInput injected %d of %d: %w", n, len(recs), err)
	}
	return nil
}

func (s *sendInput) Pointer(x, y int, buttons byte) error {
	err := s.send(pointerRecords(s.vs, s.m, x, y, s.buttons, buttons))
	s.buttons = buttons
	return err
}

func (s *sendInput) Wheel(dx, dy int) error      { return s.send(wheelRecords(dx, dy)) }
func (s *sendInput) Key(k Scan, down bool) error { return s.send(keyRecords(k, down)) }
func (s *sendInput) Text(t string) error         { return s.send(textRecords(t)) }
func (s *sendInput) Close() error                { return nil }
