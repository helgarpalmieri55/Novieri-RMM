package helper

import (
	"time"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// MaxInputPerSecond is spec §3.3's cap on injected input events.
const MaxInputPerSecond = 500

const inputBurst = 100

// rateLimiter is a token bucket; the clock is passed in so tests need no sleep.
type rateLimiter struct {
	tokens float64
	last   time.Time
}

func (r *rateLimiter) allow(now time.Time) bool {
	if r.last.IsZero() {
		r.tokens = inputBurst
	} else if d := now.Sub(r.last); d > 0 {
		r.tokens += d.Seconds() * MaxInputPerSecond
		if r.tokens > inputBurst {
			r.tokens = inputBurst
		}
	}
	r.last = now
	if r.tokens < 1 {
		return false
	}
	r.tokens--
	return true
}

// inputGate is the endpoint's own validation of viewer input (spec §3.3):
// allowlisted types, coordinates clamped to the monitor, keys from the fixed
// table, TEXT capped by the codec, a rate limit, and nothing while view-only.
// The gateway filters too; this is the line that does not depend on it.
type inputGate struct {
	inj     Injector
	w, h    int
	control bool
	limit   rateLimiter
	held    map[Scan]bool
	buttons byte
	x, y    int
	dropped int
}

func newInputGate(inj Injector, w, h int, control bool) *inputGate {
	return &inputGate{inj: inj, w: w, h: h, control: control, held: map[Scan]bool{}}
}

// isInput reports whether t is a frame that acts on the desktop.
func isInput(t rsp.Type) bool {
	return t == rsp.Pointer || t == rsp.Wheel || t == rsp.Key || t == rsp.Text
}

// handle applies one input frame and reports whether anything was injected.
func (g *inputGate) handle(f rsp.Frame, now time.Time) bool {
	if !isInput(f.Type) || !g.control || g.inj == nil {
		g.dropped++
		return false
	}
	if !g.limit.allow(now) {
		g.dropped++
		return false
	}
	var err error
	switch f.Type {
	case rsp.Pointer:
		p, derr := rsp.DecodePointer(f)
		if derr != nil {
			g.dropped++
			return false
		}
		g.x, g.y = clamp(int(p.X), g.w), clamp(int(p.Y), g.h)
		g.buttons = p.Buttons & 0x1F
		err = g.inj.Pointer(g.x, g.y, g.buttons)
	case rsp.Wheel:
		dx, dy, derr := rsp.DecodeWheel(f)
		if derr != nil {
			g.dropped++
			return false
		}
		err = g.inj.Wheel(int(dx), int(dy))
	case rsp.Key:
		down, code, derr := rsp.DecodeKey(f)
		s, ok := LookupKey(code)
		if derr != nil || !ok {
			g.dropped++
			return false
		}
		if !down && !g.held[s] {
			return false // a release for a key this session never pressed
		}
		if down {
			g.held[s] = true
		} else {
			delete(g.held, s)
		}
		err = g.inj.Key(s, down)
	case rsp.Text:
		s, derr := rsp.DecodeText(f)
		if derr != nil || s == "" {
			g.dropped++
			return false
		}
		err = g.inj.Text(s)
	}
	return err == nil
}

// setControl switches between control and view only. Leaving control
// releases everything the technician was holding, so no modifier or button
// stays down on the user's desktop.
func (g *inputGate) setControl(on bool) {
	if !on && g.control {
		g.releaseAll()
	}
	g.control = on
}

// resize changes the monitor the coordinates are clamped to.
func (g *inputGate) resize(inj Injector, w, h int) {
	g.releaseAll()
	g.inj, g.w, g.h = inj, w, h
}

func (g *inputGate) releaseAll() {
	if g.inj == nil {
		return
	}
	for s := range g.held {
		_ = g.inj.Key(s, false)
	}
	g.held = map[Scan]bool{}
	if g.buttons != 0 {
		g.buttons = 0
		_ = g.inj.Pointer(g.x, g.y, 0)
	}
}

func clamp(v, size int) int {
	if v < 0 {
		return 0
	}
	if size > 0 && v >= size {
		return size - 1
	}
	return v
}
