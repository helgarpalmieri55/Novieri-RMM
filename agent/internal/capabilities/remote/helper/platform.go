// Package helper is the screen-share helper: the agent binary re-executed as
// `remote-helper` inside the logged-on user's session
// (spec-screen-share.md §2.4). It captures, encodes, injects input and shows
// the consent prompt and the indicator; the SYSTEM service never does any of
// that, it only moves bytes between the relay and this process's pipes.
//
// Everything here lives for one session: the process exits when the session
// ends, which is what returns the capture device and frame buffers to the OS
// (§4.1).
//
// The session logic is portable and tested on every OS against a synthetic
// platform; the Windows platform (DXGI/GDI capture, SendInput, Win32 UI) is in
// the _windows.go files.
package helper

import (
	"context"
	"errors"
	"image"
	"time"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// Frame is one captured image of the selected monitor: 32-bit BGRA, top-down.
type Frame struct {
	Width, Height, Stride int
	Pix                   []byte
}

// Move is a region the OS reports as moved (a scrolled or dragged window).
type Move struct {
	Src image.Point
	Dst image.Rectangle
}

// CursorShape is a pointer image with its hot spot.
type CursorShape struct {
	HotX, HotY int
	Image      *image.NRGBA
}

// CursorState is where the pointer is, relative to the selected monitor.
type CursorState struct {
	X, Y    int
	Visible bool
}

// Update is what one Capturer.Next call observed.
type Update struct {
	// Frame is the whole monitor; nil when nothing on screen changed. It is
	// owned by the capturer and valid until the next Next call.
	Frame *Frame
	// Dirty limits where Frame may differ from the previous one; nil means
	// anywhere. Moves are applied before Dirty.
	Dirty []image.Rectangle
	Moves []Move
	// Cursor is non-nil when the pointer moved or changed visibility; Shape
	// when its image changed.
	Cursor *CursorState
	Shape  *CursorShape
}

// ErrReopen means the capture source is gone (display mode change, desktop
// switch) and must be reopened; the next frame is sent in full.
var ErrReopen = errors.New("helper: capture source must be reopened")

// ErrDesktopUnavailable means the input desktop cannot be captured right now
// (a UAC prompt or the lock screen, which a user-token helper cannot see).
var ErrDesktopUnavailable = errors.New("helper: input desktop unavailable")

// Capturer produces frames of one monitor.
type Capturer interface {
	// Next blocks up to timeout for something to change. It returns an empty
	// Update on timeout.
	Next(ctx context.Context, timeout time.Duration) (Update, error)
	// Kind names the mechanism ("dxgi", "gdi", ...) for logs and tests.
	Kind() string
	Close() error
}

// Injector delivers validated input to the desktop. Coordinates are relative
// to the monitor it was opened for and already clamped.
type Injector interface {
	Pointer(x, y int, buttons byte) error
	Wheel(dx, dy int) error
	Key(s Scan, down bool) error
	Text(s string) error
	Close() error
}

// Answer is the endpoint user's reply to the consent prompt.
type Answer int

// Consent answers.
const (
	Denied Answer = iota
	Granted
	TimedOut
)

// Prompt is what the user is shown, already sanitised.
type Prompt struct {
	Technician string
	Org        string
	Reason     string
	Recorded   bool
	// Control is whether the technician may take control, not only view.
	Control bool
	Timeout time.Duration
}

// IndicatorEvent is a button the endpoint user pressed on the indicator.
type IndicatorEvent int

// Indicator events.
const (
	StopControl IndicatorEvent = iota + 1
	EndSession
)

// Indicator is the always-visible "being assisted" bar.
type Indicator interface {
	// SetControlling switches the text between viewing and controlling.
	SetControlling(bool)
	Events() <-chan IndicatorEvent
	Close()
}

// UI is the helper's user interface.
type UI interface {
	// Consent shows the prompt and waits for an answer; a cancelled ctx is a
	// denial.
	Consent(ctx context.Context, p Prompt) Answer
	// ShowIndicator puts the indicator on monitor m.
	ShowIndicator(p Prompt, m rsp.Monitor, controlling bool) (Indicator, error)
	Close()
}

// Platform is what the session needs from the OS.
type Platform interface {
	Monitors() ([]rsp.Monitor, error)
	OpenCapturer(m rsp.Monitor) (Capturer, error)
	OpenInjector(m rsp.Monitor) (Injector, error)
	UI() UI
}
