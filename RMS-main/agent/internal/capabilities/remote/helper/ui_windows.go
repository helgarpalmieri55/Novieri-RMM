package helper

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"runtime"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// The consent prompt and the indicator, in plain Win32 (spec §2.4). Each
// window lives on its own locked OS thread with its own message loop, for
// exactly as long as it is on screen.

const (
	wmDestroy          = 0x0002
	wmSetFont          = 0x0030
	wmClose            = 0x0010
	wmMouseActivate    = 0x0021
	wmCommand          = 0x0111
	wmTimer            = 0x0113
	wmCtlColorStatic   = 0x0138
	wmApp              = 0x8000
	wmAppCancel        = wmApp + 1
	wmAppMode          = wmApp + 2
	wmAppQuit          = wmApp + 3
	wsPopup            = 0x80000000
	wsCaption          = 0x00C00000
	wsSysMenu          = 0x00080000
	wsVisible          = 0x10000000
	wsChild            = 0x40000000
	wsTabStop          = 0x00010000
	wsExTopmost        = 0x00000008
	wsExToolWindow     = 0x00000080
	wsExNoActivate     = 0x08000000
	wsExTransparent    = 0x00000020
	wsExLayered        = 0x00080000
	wsExDlgModalFrame  = 0x00000001
	bsDefPushButton    = 0x00000001
	ssNoPrefix         = 0x00000080
	ssCenterImage      = 0x00000200
	swShowNoActivate   = 4
	swShow             = 5
	swpNoSize          = 0x0001
	swpNoMove          = 0x0002
	swpNoActivate      = 0x0010
	swpShowWindow      = 0x0040
	maNoActivate       = 3
	lwaAlpha           = 0x2
	wdaMonitor         = 0x1
	wdaExcludeCapture  = 0x11
	idcArrow           = 32512
	colorBtnFace       = 15
	idAllow            = 100
	idDecline          = 2 // IDCANCEL, so Esc declines
	idOK               = 1
	idStopControl      = 11
	idEnd              = 12
	timerCountdown     = 1
	timerTopmost       = 2
	transparentBk      = 1
	indicatorColour    = 0x00008CFF // RGB(255,140,0) as COLORREF
	indicatorTextColor = 0x00000000
)

var hwndTopmost = ^uintptr(0) // (HWND)-1

type wndClassEx struct {
	Size       uint32
	Style      uint32
	WndProc    uintptr
	ClsExtra   int32
	WndExtra   int32
	Instance   uintptr
	Icon       uintptr
	Cursor     uintptr
	Background uintptr
	MenuName   *uint16
	ClassName  *uint16
	IconSm     uintptr
}

type msg struct {
	Hwnd    uintptr
	Message uint32
	WParam  uintptr
	LParam  uintptr
	Time    uint32
	Pt      point
	Private uint32
}

// window is the Go side of one top-level window, found by handle from the
// shared window procedure.
type window struct {
	onMessage func(hwnd uintptr, m uint32, wp, lp uintptr) (uintptr, bool)
}

var (
	classOnce  sync.Once
	classErr   error
	wndProcPtr uintptr
	instance   uintptr
	barBrush   uintptr
	windowsMu  sync.Mutex
	windowsBy  = map[uintptr]*window{}
)

const (
	classConsent = "ITMSRemoteConsent"
	classBar     = "ITMSRemoteIndicator"
	classBorder  = "ITMSRemoteBorder"
)

func registerClasses() error {
	classOnce.Do(func() {
		if !has(procRegisterClassExW, procCreateWindowExW, procGetMessageW, procSetWindowDisplayAffinity) {
			classErr = errProc
			return
		}
		instance, _, _ = procGetModuleHandleW.Call(0)
		wndProcPtr = windows.NewCallback(wndProc)
		barBrush, _, _ = procCreateSolidBrush.Call(indicatorColour)
		arrow, _, _ := procLoadCursorW.Call(0, idcArrow)
		for name, bg := range map[string]uintptr{classConsent: colorBtnFace + 1, classBar: barBrush, classBorder: barBrush} {
			wc := wndClassEx{Size: uint32(unsafe.Sizeof(wndClassEx{})), WndProc: wndProcPtr, Instance: instance,
				Cursor: arrow, Background: bg, ClassName: wstr(name)}
			if r, _, err := procRegisterClassExW.Call(uintptr(unsafe.Pointer(&wc))); r == 0 {
				classErr = fmt.Errorf("RegisterClassEx %s: %w", name, err)
				return
			}
		}
	})
	return classErr
}

func wndProc(hwnd, m, wp, lp uintptr) uintptr {
	windowsMu.Lock()
	w := windowsBy[hwnd]
	windowsMu.Unlock()
	if w != nil {
		if r, handled := w.onMessage(hwnd, uint32(m), wp, lp); handled {
			return r
		}
	}
	r, _, _ := procDefWindowProcW.Call(hwnd, m, wp, lp)
	return r
}

func track(hwnd uintptr, w *window) {
	windowsMu.Lock()
	windowsBy[hwnd] = w
	windowsMu.Unlock()
}

func untrack(hwnd uintptr) {
	windowsMu.Lock()
	delete(windowsBy, hwnd)
	windowsMu.Unlock()
}

func createWindow(exStyle uint32, class, title string, style uint32, x, y, w, h int, parent, id uintptr) uintptr {
	r, _, _ := procCreateWindowExW.Call(uintptr(exStyle), uintptr(unsafe.Pointer(wstr(class))), uintptr(unsafe.Pointer(wstr(title))),
		uintptr(style), uintptr(x), uintptr(y), uintptr(w), uintptr(h), parent, id, instance, 0)
	return r
}

func setText(hwnd uintptr, s string) {
	_, _, _ = procSetWindowTextW.Call(hwnd, uintptr(unsafe.Pointer(wstr(s))))
}

func uiFont(dpi int) uintptr {
	height := int32(-9 * dpi / 72) // negative: character height in pixels for 9 pt
	f, _, _ := procCreateFontW.Call(uintptr(uint32(height)), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0,
		uintptr(unsafe.Pointer(wstr("Segoe UI"))))
	return f
}

// excludeFromCapture keeps the helper's own windows out of the stream, so
// the technician never sees (or clicks) the user's controls.
func excludeFromCapture(hwnd uintptr) {
	if r, _, _ := procSetWindowDisplayAffinity.Call(hwnd, wdaExcludeCapture); r == 0 {
		_, _, _ = procSetWindowDisplayAffinity.Call(hwnd, wdaMonitor) // before Windows 10 2004: shown black
	}
}

// loop pumps messages until the thread's quit message.
func loop(dialog uintptr) {
	var m msg
	for {
		r, _, _ := procGetMessageW.Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		if int32(r) <= 0 {
			return
		}
		if dialog != 0 {
			if r, _, _ := procIsDialogMessageW.Call(dialog, uintptr(unsafe.Pointer(&m))); r != 0 {
				continue
			}
		}
		_, _, _ = procTranslateMessage.Call(uintptr(unsafe.Pointer(&m)))
		_, _, _ = procDispatchMessageW.Call(uintptr(unsafe.Pointer(&m)))
	}
}

type winUI struct{ log *slog.Logger }

func (u *winUI) Close() {}

// Consent shows the prompt: technician, organisation, reason, whether it is
// recorded, a countdown, Allow and Decline. Decline is the default button,
// Esc and the close box decline, and no answer by the deadline declines.
func (u *winUI) Consent(ctx context.Context, p Prompt) Answer {
	if err := registerClasses(); err != nil {
		u.log.Warn("consent prompt unavailable", "err", err)
		return Denied
	}
	answer := make(chan Answer, 1)
	created := make(chan uintptr, 1)
	go func() {
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		answer <- runConsent(p, created)
	}()
	hwnd := <-created
	if hwnd == 0 {
		return <-answer
	}
	select {
	case a := <-answer:
		return a
	case <-ctx.Done():
		_, _, _ = procPostMessageW.Call(hwnd, wmAppCancel, 0, 0)
		<-answer
		return Denied
	}
}

func consentText(p Prompt) (who, reason, recorded string) {
	org := ""
	if p.Org != "" {
		org = " from " + p.Org
	}
	what := "view your screen"
	if p.Control {
		what = "view and control your screen"
	}
	who = fmt.Sprintf("%s%s wants to %s.", p.Technician, org, what)
	if p.Reason != "" {
		reason = "Reason: " + p.Reason
	}
	if p.Recorded {
		recorded = "This session will be recorded."
	}
	return who, reason, recorded
}

func countdownText(n int) string {
	return fmt.Sprintf("If you do nothing, the request is declined in %d seconds.", n)
}

func runConsent(p Prompt, created chan<- uintptr) Answer {
	dpi := systemDPI()
	s := func(v int) int { return v * dpi / 96 }
	w, h := s(480), s(250)
	sx, _, _ := procGetSystemMetrics.Call(0)
	sy, _, _ := procGetSystemMetrics.Call(1)
	hwnd := createWindow(wsExTopmost|wsExDlgModalFrame, classConsent, "Remote assistance request",
		wsPopup|wsCaption|wsSysMenu, (int(sx)-w)/2, (int(sy)-h)/2, w, h, 0, 0)
	if hwnd == 0 {
		created <- 0
		return Denied
	}
	font := uiFont(dpi)
	defer func() { _, _, _ = procDeleteObject.Call(font) }()
	who, reason, recorded := consentText(p)
	m := s(16)
	cw := w - 2*m - s(8)
	child := func(class, text string, style uint32, x, y, cw, ch int, id uintptr) uintptr {
		c := createWindow(0, class, text, wsChild|wsVisible|style, x, y, cw, ch, hwnd, id)
		_, _, _ = procSendMessageW.Call(c, wmSetFont, font, 1)
		return c
	}
	child("STATIC", who, ssNoPrefix, m, m, cw, s(40), 0)
	child("STATIC", reason, ssNoPrefix, m, s(60), cw, s(40), 0)
	child("STATIC", recorded, ssNoPrefix, m, s(104), cw, s(20), 0)
	remaining := int(p.Timeout / time.Second)
	countdown := child("STATIC", countdownText(remaining), ssNoPrefix, m, s(128), cw, s(20), 0)
	bw, bh := s(96), s(30)
	child("BUTTON", "Allow", wsTabStop, w-2*bw-2*m-s(8), s(164), bw, bh, idAllow)
	decline := child("BUTTON", "Decline", wsTabStop|bsDefPushButton, w-bw-m-s(8), s(164), bw, bh, idDecline)

	answer := Denied
	finish := func(a Answer) {
		answer = a
		_, _, _ = procKillTimer.Call(hwnd, timerCountdown)
		_, _, _ = procDestroyWindow.Call(hwnd)
	}
	track(hwnd, &window{onMessage: func(hw uintptr, m uint32, wp, _ uintptr) (uintptr, bool) {
		switch m {
		case wmCommand:
			switch wp & 0xFFFF {
			case idAllow:
				finish(Granted)
			case idDecline, idOK:
				finish(Denied) // Enter with no button focused lands on IDOK
			}
			return 0, true
		case wmTimer:
			remaining--
			if remaining <= 0 {
				finish(TimedOut)
			} else {
				setText(countdown, countdownText(remaining))
			}
			return 0, true
		case wmClose, wmAppCancel:
			finish(Denied)
			return 0, true
		case wmDestroy:
			untrack(hw)
			_, _, _ = procPostQuitMessage.Call(0)
			return 0, true
		}
		return 0, false
	}})
	_, _, _ = procSetTimer.Call(hwnd, timerCountdown, 1000, 0)
	_, _, _ = procShowWindow.Call(hwnd, swShow)
	_, _, _ = procSetForegroundWindow.Call(hwnd)
	_, _, _ = procFlashWindow.Call(hwnd, 1)
	_, _, _ = procSetFocus.Call(decline)
	created <- hwnd
	loop(hwnd)
	return answer
}

// winIndicator is the bar and the border on the shared monitor.
type winIndicator struct {
	events chan IndicatorEvent
	bar    uintptr
	done   chan struct{}
}

func (i *winIndicator) Events() <-chan IndicatorEvent { return i.events }

func (i *winIndicator) SetControlling(on bool) {
	v := uintptr(0)
	if on {
		v = 1
	}
	_, _, _ = procPostMessageW.Call(i.bar, wmAppMode, v, 0)
}

func (i *winIndicator) Close() {
	_, _, _ = procPostMessageW.Call(i.bar, wmAppQuit, 0, 0)
	select {
	case <-i.done:
	case <-time.After(2 * time.Second):
	}
}

func indicatorText(p Prompt, controlling bool) string {
	verb := "viewing"
	if controlling {
		verb = "controlling"
	}
	org := ""
	if p.Org != "" {
		org = " from " + p.Org
	}
	t := fmt.Sprintf("%s%s is %s your screen", p.Technician, org, verb)
	if p.Recorded {
		t += " (recorded)"
	}
	return t
}

// ShowIndicator implements UI. The indicator cannot be closed from the
// keyboard, is never activated by a click, and is excluded from capture.
func (u *winUI) ShowIndicator(p Prompt, m rsp.Monitor, controlling bool) (Indicator, error) {
	if err := registerClasses(); err != nil {
		return nil, err
	}
	ind := &winIndicator{events: make(chan IndicatorEvent, 4), done: make(chan struct{})}
	ready := make(chan error, 1)
	go func() {
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		defer close(ind.done)
		ind.run(p, m, controlling, ready)
	}()
	if err := <-ready; err != nil {
		return nil, err
	}
	return ind, nil
}

func (i *winIndicator) run(p Prompt, m rsp.Monitor, controlling bool, ready chan<- error) {
	dpi := m.DPI
	if dpi <= 0 {
		dpi = 96
	}
	s := func(v int) int { return v * dpi / 96 }
	w, h := s(620), s(36)
	ex := uint32(wsExTopmost | wsExToolWindow | wsExNoActivate)
	bar := createWindow(ex, classBar, "Remote assistance", wsPopup, m.X+(m.Width-w)/2, m.Y+s(6), w, h, 0, 0)
	if bar == 0 {
		ready <- errors.New("could not create the indicator")
		return
	}
	i.bar = bar
	font := uiFont(dpi)
	defer func() { _, _, _ = procDeleteObject.Call(font) }()
	child := func(class, text string, style uint32, x, y, cw, ch int, id uintptr) uintptr {
		c := createWindow(0, class, text, wsChild|wsVisible|style, x, y, cw, ch, bar, id)
		_, _, _ = procSendMessageW.Call(c, wmSetFont, font, 1)
		return c
	}
	bw, bh := s(96), s(26)
	label := child("STATIC", indicatorText(p, controlling), ssNoPrefix|ssCenterImage, s(10), s(5), w-2*bw-s(40), bh, 0)
	stop := child("BUTTON", "Stop control", 0, w-2*bw-s(18), s(5), bw, bh, idStopControl)
	child("BUTTON", "End", 0, w-bw-s(8), s(5), bw, bh, idEnd)
	if !controlling {
		_, _, _ = procEnableWindow.Call(stop, 0)
	}

	t := s(4)
	var borders []uintptr
	for _, r := range [4][4]int{
		{m.X, m.Y, m.Width, t}, {m.X, m.Y + m.Height - t, m.Width, t},
		{m.X, m.Y, t, m.Height}, {m.X + m.Width - t, m.Y, t, m.Height},
	} {
		b := createWindow(ex|wsExLayered|wsExTransparent, classBorder, "", wsPopup, r[0], r[1], r[2], r[3], 0, 0)
		if b != 0 {
			_, _, _ = procSetLayeredWindowAttributes.Call(b, 0, 220, lwaAlpha)
			borders = append(borders, b)
		}
	}
	emit := func(e IndicatorEvent) {
		select {
		case i.events <- e:
		default:
		}
	}
	track(bar, &window{onMessage: func(hw uintptr, msgID uint32, wp, lp uintptr) (uintptr, bool) {
		switch msgID {
		case wmCommand:
			switch wp & 0xFFFF {
			case idStopControl:
				_, _, _ = procEnableWindow.Call(stop, 0)
				emit(StopControl)
			case idEnd:
				emit(EndSession)
			}
			return 0, true
		case wmMouseActivate:
			return maNoActivate, true
		case wmClose:
			return 0, true // only End ends the session
		case wmCtlColorStatic:
			_, _, _ = procSetBkMode.Call(wp, transparentBk)
			_, _, _ = procSetTextColor.Call(wp, indicatorTextColor)
			return barBrush, true
		case wmTimer:
			for _, x := range append([]uintptr{bar}, borders...) {
				_, _, _ = procSetWindowPos.Call(x, hwndTopmost, 0, 0, 0, 0, swpNoMove|swpNoSize|swpNoActivate)
			}
			return 0, true
		case wmAppMode:
			setText(label, indicatorText(p, wp == 1))
			if wp != 1 {
				_, _, _ = procEnableWindow.Call(stop, 0)
			}
			return 0, true
		case wmAppQuit:
			_, _, _ = procKillTimer.Call(hw, timerTopmost)
			for _, b := range borders {
				_, _, _ = procDestroyWindow.Call(b)
			}
			_, _, _ = procDestroyWindow.Call(hw)
			return 0, true
		case wmDestroy:
			untrack(hw)
			_, _, _ = procPostQuitMessage.Call(0)
			return 0, true
		}
		return 0, false
	}})
	for _, x := range append([]uintptr{bar}, borders...) {
		excludeFromCapture(x)
		_, _, _ = procShowWindow.Call(x, swShowNoActivate)
		_, _, _ = procSetWindowPos.Call(x, hwndTopmost, 0, 0, 0, 0, swpNoMove|swpNoSize|swpNoActivate|swpShowWindow)
	}
	// Other topmost windows can cover the bar; it reasserts itself.
	_, _, _ = procSetTimer.Call(bar, timerTopmost, 2000, 0)
	ready <- nil
	loop(0)
}
