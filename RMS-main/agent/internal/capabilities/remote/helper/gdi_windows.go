package helper

import (
	"context"
	"errors"
	"time"
	"unsafe"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// gdiInterval caps the GDI fallback at 15 frames per second (spec §2.4). GDI
// cannot say what changed, so it polls; the tiler's diff turns an unchanged
// poll into nothing sent.
const gdiInterval = time.Second / 15

const (
	srcCopy     = 0x00CC0020
	captureBlt  = 0x40000000
	cursorShown = 0x00000001
)

type bitmapInfoHeader struct {
	Size          uint32
	Width, Height int32
	Planes        uint16
	BitCount      uint16
	Compression   uint32
	SizeImage     uint32
	XPelsPerMeter int32
	YPelsPerMeter int32
	ClrUsed       uint32
	ClrImportant  uint32
}

type cursorInfo struct {
	Size   uint32
	Flags  uint32
	Cursor uintptr
	Pos    point
}

type gdiCapturer struct {
	m                     rsp.Monitor
	screen, mem, bmp, old uintptr
	frame                 Frame
	last                  time.Time
}

func openGDI(m rsp.Monitor) (*gdiCapturer, error) {
	if !has(procGetDC, procCreateCompatibleDC, procCreateDIBSection, procBitBlt, procSelectObject) {
		return nil, errProc
	}
	c := &gdiCapturer{m: m}
	c.screen, _, _ = procGetDC.Call(0)
	if c.screen == 0 {
		return nil, ErrDesktopUnavailable
	}
	c.mem, _, _ = procCreateCompatibleDC.Call(c.screen)
	bi := bitmapInfoHeader{Size: uint32(unsafe.Sizeof(bitmapInfoHeader{})), Width: int32(m.Width),
		Height: -int32(m.Height), Planes: 1, BitCount: 32} // negative height: top-down rows
	var bits unsafe.Pointer
	c.bmp, _, _ = procCreateDIBSection.Call(c.mem, uintptr(unsafe.Pointer(&bi)), 0, uintptr(unsafe.Pointer(&bits)), 0, 0)
	if c.mem == 0 || c.bmp == 0 || bits == nil {
		_ = c.Close()
		return nil, errors.New("GDI: could not create the capture bitmap")
	}
	c.old, _, _ = procSelectObject.Call(c.mem, c.bmp)
	c.frame = Frame{Width: m.Width, Height: m.Height, Stride: m.Width * 4, Pix: unsafe.Slice((*byte)(bits), m.Width*m.Height*4)}
	return c, nil
}

func (c *gdiCapturer) Kind() string { return "gdi" }

func (c *gdiCapturer) Close() error {
	if c.old != 0 {
		_, _, _ = procSelectObject.Call(c.mem, c.old)
		c.old = 0
	}
	if c.bmp != 0 {
		_, _, _ = procDeleteObject.Call(c.bmp)
		c.bmp = 0
	}
	if c.mem != 0 {
		_, _, _ = procDeleteDC.Call(c.mem)
		c.mem = 0
	}
	if c.screen != 0 {
		_, _, _ = procReleaseDC.Call(0, c.screen)
		c.screen = 0
	}
	c.frame.Pix = nil
	return nil
}

// Next waits out the frame interval, then copies the monitor. The first call
// returns immediately.
func (c *gdiCapturer) Next(ctx context.Context, timeout time.Duration) (Update, error) {
	if wait := gdiInterval - time.Since(c.last); wait > 0 && !c.last.IsZero() {
		if wait > timeout {
			wait = timeout
		}
		if err := sleepCtx(ctx, wait); err != nil {
			return Update{}, err
		}
	}
	c.last = time.Now()
	if r, _, _ := procBitBlt.Call(c.mem, 0, 0, uintptr(c.m.Width), uintptr(c.m.Height),
		c.screen, uintptr(c.m.X), uintptr(c.m.Y), srcCopy|captureBlt); r == 0 {
		// BitBlt fails while the secure desktop has the input.
		return Update{}, ErrReopen
	}
	u := Update{Frame: &c.frame}
	ci := cursorInfo{Size: uint32(unsafe.Sizeof(cursorInfo{}))}
	if r, _, _ := procGetCursorInfo.Call(uintptr(unsafe.Pointer(&ci))); r != 0 {
		u.Cursor = &CursorState{X: int(ci.Pos.X) - c.m.X, Y: int(ci.Pos.Y) - c.m.Y, Visible: ci.Flags&cursorShown != 0}
	}
	return u, nil
}
