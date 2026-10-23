package helper

import (
	"unicode/utf16"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// This file turns validated input into SendInput records. It is portable so
// the mapping is tested on every OS; only the SendInput call is Windows-only.

// SendInput constants (winuser.h).
const (
	inputMouse    = 0
	inputKeyboard = 1

	mouseMove        = 0x0001
	mouseLeftDown    = 0x0002
	mouseLeftUp      = 0x0004
	mouseRightDown   = 0x0008
	mouseRightUp     = 0x0010
	mouseMiddleDown  = 0x0020
	mouseMiddleUp    = 0x0040
	mouseXDown       = 0x0080
	mouseXUp         = 0x0100
	mouseWheel       = 0x0800
	mouseHWheel      = 0x1000
	mouseVirtualDesk = 0x4000
	mouseAbsolute    = 0x8000

	keyExtended = 0x0001
	keyUp       = 0x0002
	keyUnicode  = 0x0004
	keyScancode = 0x0008

	xButton1 = 1
	xButton2 = 2

	// maxWheel bounds one WHEEL frame to ten notches.
	maxWheel = 1200
)

// mouseInput is MOUSEINPUT. A KEYBDINPUT is written into the same bytes (see
// keyRecord), so one record type serves both and matches sizeof(INPUT) on
// 32- and 64-bit Windows: the union is aligned like the uintptr it holds.
type mouseInput struct {
	Dx, Dy    int32
	MouseData uint32
	Flags     uint32
	Time      uint32
	ExtraInfo uintptr
}

// winInput is INPUT.
type winInput struct {
	Type uint32
	Mi   mouseInput
}

// keyRecord builds a keyboard INPUT: KEYBDINPUT's wVk and wScan share the
// first 32 bits and dwFlags the next, which are Dx and Dy in MOUSEINPUT.
func keyRecord(vk, scan uint16, flags uint32) winInput {
	return winInput{Type: inputKeyboard, Mi: mouseInput{Dx: int32(uint32(vk) | uint32(scan)<<16), Dy: int32(flags)}}
}

// VirtualScreen is the bounding box of all monitors, in the coordinates
// SendInput's MOUSEEVENTF_VIRTUALDESK normalises against.
type VirtualScreen struct{ X, Y, W, H int }

// absolute maps a monitor-relative point to SendInput's 0..65535 range.
func absolute(vs VirtualScreen, m rsp.Monitor, x, y int) (int32, int32) {
	norm := func(v, origin, size int) int32 {
		if size <= 1 {
			return 0
		}
		return int32(((v - origin) * 65535) / (size - 1))
	}
	return norm(m.X+x, vs.X, vs.W), norm(m.Y+y, vs.Y, vs.H)
}

var buttonFlags = [5]struct {
	down, up, data uint32
}{
	{mouseLeftDown, mouseLeftUp, 0},
	{mouseRightDown, mouseRightUp, 0},
	{mouseMiddleDown, mouseMiddleUp, 0},
	{mouseXDown, mouseXUp, xButton1},
	{mouseXDown, mouseXUp, xButton2},
}

// pointerRecords moves the pointer, then presses or releases each button
// whose bit changed since prev.
func pointerRecords(vs VirtualScreen, m rsp.Monitor, x, y int, prev, now byte) []winInput {
	dx, dy := absolute(vs, m, x, y)
	out := []winInput{{Type: inputMouse, Mi: mouseInput{Dx: dx, Dy: dy, Flags: mouseMove | mouseAbsolute | mouseVirtualDesk}}}
	for i, b := range buttonFlags {
		bit := byte(1) << i
		if prev&bit == now&bit {
			continue
		}
		f := b.up
		if now&bit != 0 {
			f = b.down
		}
		out = append(out, winInput{Type: inputMouse, Mi: mouseInput{Dx: dx, Dy: dy, MouseData: b.data, Flags: f | mouseAbsolute | mouseVirtualDesk}})
	}
	return out
}

// wheelRecords converts rsp/1 wheel deltas (DOM sense: positive dy scrolls
// down) to Windows' (positive WHEEL scrolls up, positive HWHEEL right).
func wheelRecords(dx, dy int) []winInput {
	var out []winInput
	if dy = clampWheel(dy); dy != 0 {
		out = append(out, winInput{Type: inputMouse, Mi: mouseInput{MouseData: uint32(int32(-dy)), Flags: mouseWheel}})
	}
	if dx = clampWheel(dx); dx != 0 {
		out = append(out, winInput{Type: inputMouse, Mi: mouseInput{MouseData: uint32(int32(dx)), Flags: mouseHWheel}})
	}
	return out
}

func clampWheel(v int) int {
	if v > maxWheel {
		return maxWheel
	}
	if v < -maxWheel {
		return -maxWheel
	}
	return v
}

// keyRecords sends a physical key by scan code; the endpoint's layout decides
// the character.
func keyRecords(s Scan, down bool) []winInput {
	f := uint32(keyScancode)
	if s.Extended {
		f |= keyExtended
	}
	if !down {
		f |= keyUp
	}
	return []winInput{keyRecord(0, s.Code, f)}
}

// textRecords types s as Unicode characters, independent of the layout.
func textRecords(s string) []winInput {
	var out []winInput
	for _, r := range s {
		if r == '\n' {
			r = '\r'
		}
		if r < 0x20 && r != '\r' && r != '\t' {
			continue
		}
		for _, u := range utf16.Encode([]rune{r}) {
			out = append(out, keyRecord(0, u, keyUnicode), keyRecord(0, u, keyUnicode|keyUp))
		}
	}
	return out
}
