// Package synthetic is a fake desktop for the screen-share helper: a drawn
// screen, a pointer that follows injected input, and a scripted consent
// answer. Tests on every OS and the backend's end-to-end double
// (remote/relayagent) use it in place of DXGI, SendInput and Win32 windows.
// It captures nothing and injects nothing into a real session.
package synthetic

import (
	"context"
	"image"
	"image/color"
	"sync"
	"time"

	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/helper"
	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// Event is one injected input, as the fake desktop received it.
type Event struct {
	Kind    string // pointer | wheel | key | text
	X, Y    int
	Buttons byte
	Scan    helper.Scan
	Down    bool
	Text    string
}

// Options scripts the fake desktop.
type Options struct {
	Width, Height int
	// Monitors is how many side-by-side monitors there are (default 1).
	Monitors int
	Answer   helper.Answer
	// AnswerAfter delays the consent answer.
	AnswerAfter time.Duration
	// Frozen: the screen never changes, whatever is injected.
	Frozen bool
	// AlwaysChanged reports a frame on every Next even when nothing changed,
	// as a polling (GDI) capturer does.
	AlwaysChanged bool
	// NoInjector makes OpenInjector fail (view only).
	NoInjector bool
}

// Desktop is the fake platform.
type Desktop struct {
	o Options

	mu       sync.Mutex
	img      *image.RGBA // the whole virtual screen, monitors side by side
	events   []Event
	cursor   image.Point
	changed  chan struct{}
	prompts  []helper.Prompt
	shown    int
	ind      *indicator
	indReady chan struct{}
}

// New builds a fake desktop.
func New(o Options) *Desktop {
	if o.Width == 0 {
		o.Width, o.Height = 640, 360
	}
	if o.Monitors == 0 {
		o.Monitors = 1
	}
	d := &Desktop{o: o, changed: make(chan struct{}, 1), indReady: make(chan struct{})}
	d.img = image.NewRGBA(image.Rect(0, 0, o.Width*o.Monitors, o.Height))
	for y := 0; y < o.Height; y++ {
		for x := 0; x < o.Width*o.Monitors; x++ {
			c := color.RGBA{245, 245, 245, 255}
			if y < 24 {
				c = color.RGBA{30, 60, 120, 255}
			} else if (y/12)%2 == 0 && (x/7)%5 != 0 && x%(o.Width) > 20 && x%(o.Width) < o.Width-40 {
				c = color.RGBA{uint8(40 + x%9), 40, 40, 255} // a line of "text"
			}
			d.img.SetRGBA(x, y, c)
		}
	}
	return d
}

// Events returns what was injected so far.
func (d *Desktop) Events() []Event {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]Event(nil), d.events...)
}

// Prompts returns the consent prompts shown so far.
func (d *Desktop) Prompts() []helper.Prompt {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]helper.Prompt(nil), d.prompts...)
}

// Press presses an indicator button, once the indicator is up.
func (d *Desktop) Press(ctx context.Context, ev helper.IndicatorEvent) bool {
	select {
	case <-d.indReady:
	case <-ctx.Done():
		return false
	}
	d.mu.Lock()
	ind := d.ind
	d.mu.Unlock()
	select {
	case ind.events <- ev:
		return true
	case <-ctx.Done():
		return false
	}
}

// Controlling reports what the indicator last said.
func (d *Desktop) Controlling() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.ind != nil && d.ind.controlling
}

// IndicatorShown reports how many indicators were shown.
func (d *Desktop) IndicatorShown() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.shown
}

func (d *Desktop) poke() {
	select {
	case d.changed <- struct{}{}:
	default:
	}
}

// Monitors implements helper.Platform.
func (d *Desktop) Monitors() ([]rsp.Monitor, error) {
	var ms []rsp.Monitor
	for i := 0; i < d.o.Monitors; i++ {
		ms = append(ms, rsp.Monitor{ID: i, X: i * d.o.Width, Width: d.o.Width, Height: d.o.Height, DPI: 96, Primary: i == 0})
	}
	return ms, nil
}

// OpenCapturer implements helper.Platform.
func (d *Desktop) OpenCapturer(m rsp.Monitor) (helper.Capturer, error) {
	return &capturer{d: d, m: m}, nil
}

// OpenInjector implements helper.Platform.
func (d *Desktop) OpenInjector(m rsp.Monitor) (helper.Injector, error) {
	if d.o.NoInjector {
		return nil, errNoInput
	}
	return &injector{d: d, m: m}, nil
}

type errString string

func (e errString) Error() string { return string(e) }

const errNoInput = errString("synthetic: no input on this desktop")

// UI implements helper.Platform.
func (d *Desktop) UI() helper.UI { return (*ui)(d) }

type capturer struct {
	d     *Desktop
	m     rsp.Monitor
	first bool
	last  image.Point
}

func (c *capturer) Kind() string { return "synthetic" }
func (c *capturer) Close() error { return nil }

func (c *capturer) Next(ctx context.Context, timeout time.Duration) (helper.Update, error) {
	if c.first {
		t := time.NewTimer(timeout)
		defer t.Stop()
		select {
		case <-c.d.changed:
		case <-t.C:
			if !c.d.o.AlwaysChanged {
				return helper.Update{}, nil
			}
		case <-ctx.Done():
			return helper.Update{}, ctx.Err()
		}
	}
	c.first = true
	c.d.mu.Lock()
	defer c.d.mu.Unlock()
	f := &helper.Frame{Width: c.m.Width, Height: c.m.Height, Stride: c.m.Width * 4, Pix: make([]byte, c.m.Width*c.m.Height*4)}
	for y := 0; y < c.m.Height; y++ {
		for x := 0; x < c.m.Width; x++ {
			p := c.d.img.RGBAAt(c.m.X+x, y)
			i := y*f.Stride + x*4
			f.Pix[i], f.Pix[i+1], f.Pix[i+2], f.Pix[i+3] = p.B, p.G, p.R, 255
		}
	}
	u := helper.Update{Frame: f}
	if c.d.cursor != c.last {
		c.last = c.d.cursor
		u.Cursor = &helper.CursorState{X: c.d.cursor.X - c.m.X, Y: c.d.cursor.Y, Visible: true}
	}
	return u, nil
}

type injector struct {
	d *Desktop
	m rsp.Monitor
}

func (i *injector) Close() error { return nil }

func (i *injector) record(e Event, draw func()) error {
	i.d.mu.Lock()
	i.d.events = append(i.d.events, e)
	if !i.d.o.Frozen && draw != nil {
		draw()
	}
	i.d.mu.Unlock()
	i.d.poke()
	return nil
}

func (i *injector) Pointer(x, y int, buttons byte) error {
	return i.record(Event{Kind: "pointer", X: x, Y: y, Buttons: buttons}, func() {
		i.d.cursor = image.Pt(i.m.X+x, y)
		if buttons&1 != 0 {
			// A click leaves a red square where it landed.
			for dy := 0; dy < 16; dy++ {
				for dx := 0; dx < 16; dx++ {
					i.d.img.SetRGBA(i.m.X+x+dx, y+dy, color.RGBA{220, 30, 30, 255})
				}
			}
		}
	})
}

func (i *injector) Wheel(dx, dy int) error {
	return i.record(Event{Kind: "wheel", X: dx, Y: dy}, nil)
}

func (i *injector) Key(s helper.Scan, down bool) error {
	return i.record(Event{Kind: "key", Scan: s, Down: down}, nil)
}

func (i *injector) Text(s string) error {
	return i.record(Event{Kind: "text", Text: s}, func() {
		for dx := 0; dx < 8*len(s) && dx < i.m.Width; dx++ {
			i.d.img.SetRGBA(i.m.X+dx, i.m.Height-10, color.RGBA{0, 120, 0, 255})
		}
	})
}

type ui Desktop

func (u *ui) Close() {}

func (u *ui) Consent(ctx context.Context, p helper.Prompt) helper.Answer {
	d := (*Desktop)(u)
	d.mu.Lock()
	d.prompts = append(d.prompts, p)
	d.mu.Unlock()
	t := time.NewTimer(d.o.AnswerAfter)
	defer t.Stop()
	select {
	case <-t.C:
		return d.o.Answer
	case <-ctx.Done():
		return helper.Denied
	}
}

func (u *ui) ShowIndicator(p helper.Prompt, m rsp.Monitor, controlling bool) (helper.Indicator, error) {
	d := (*Desktop)(u)
	d.mu.Lock()
	defer d.mu.Unlock()
	d.shown++
	d.ind = &indicator{d: d, events: make(chan helper.IndicatorEvent), controlling: controlling}
	if d.shown == 1 {
		close(d.indReady)
	}
	return d.ind, nil
}

type indicator struct {
	d           *Desktop
	events      chan helper.IndicatorEvent
	controlling bool
}

func (i *indicator) SetControlling(on bool) {
	i.d.mu.Lock()
	i.controlling = on
	i.d.mu.Unlock()
}

func (i *indicator) Events() <-chan helper.IndicatorEvent { return i.events }
func (i *indicator) Close()                               {}
