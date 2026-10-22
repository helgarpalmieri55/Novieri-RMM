package helper

import (
	"encoding/binary"
	"testing"
	"unsafe"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

func TestTheInputRecordIsSizedLikeWindowsINPUT(t *testing.T) {
	want := uintptr(28) // 32-bit
	if unsafe.Sizeof(uintptr(0)) == 8 {
		want = 40
	}
	if got := unsafe.Sizeof(winInput{}); got != want {
		t.Fatalf("sizeof(INPUT) = %d; Windows expects %d", got, want)
	}
	if off := unsafe.Offsetof(winInput{}.Mi); off != unsafe.Alignof(uintptr(0)) {
		t.Fatalf("union at offset %d", off)
	}
}

func TestAKeyRecordLaysOutKEYBDINPUT(t *testing.T) {
	r := keyRecord(0x41, 0x1E, keyScancode|keyUp)
	b := unsafe.Slice((*byte)(unsafe.Pointer(&r.Mi)), unsafe.Sizeof(r.Mi))
	// KEYBDINPUT: WORD wVk, WORD wScan, DWORD dwFlags, DWORD time, ULONG_PTR extra.
	if vk := binary.LittleEndian.Uint16(b[0:]); vk != 0x41 {
		t.Errorf("wVk %#x", vk)
	}
	if sc := binary.LittleEndian.Uint16(b[2:]); sc != 0x1E {
		t.Errorf("wScan %#x", sc)
	}
	if f := binary.LittleEndian.Uint32(b[4:]); f != keyScancode|keyUp {
		t.Errorf("dwFlags %#x", f)
	}
	if r.Type != inputKeyboard {
		t.Errorf("type %d", r.Type)
	}
}

func TestPointerMapsToTheVirtualDesktop(t *testing.T) {
	// Two monitors: 1920x1080 at the origin and 1280x1024 to its left.
	vs := VirtualScreen{X: -1280, Y: 0, W: 3200, H: 1080}
	primary := rsp.Monitor{X: 0, Y: 0, Width: 1920, Height: 1080}
	left := rsp.Monitor{X: -1280, Y: 0, Width: 1280, Height: 1024}
	if dx, dy := absolute(vs, left, 0, 0); dx != 0 || dy != 0 {
		t.Errorf("left monitor origin -> %d,%d", dx, dy)
	}
	if dx, dy := absolute(vs, primary, 1919, 1079); dx != 65535 || dy != 65535 {
		t.Errorf("primary bottom-right -> %d,%d", dx, dy)
	}
	if dx, _ := absolute(vs, primary, 0, 0); dx != int32(1280*65535/3199) {
		t.Errorf("primary origin -> %d", dx)
	}
}

func TestButtonTransitionsBecomeDownsAndUps(t *testing.T) {
	vs := VirtualScreen{W: 100, H: 100}
	m := rsp.Monitor{Width: 100, Height: 100}
	recs := pointerRecords(vs, m, 10, 10, 0, 1|16)
	if len(recs) != 3 {
		t.Fatalf("%d records", len(recs))
	}
	if recs[0].Mi.Flags != mouseMove|mouseAbsolute|mouseVirtualDesk {
		t.Errorf("move flags %#x", recs[0].Mi.Flags)
	}
	if recs[1].Mi.Flags&mouseLeftDown == 0 || recs[2].Mi.Flags&mouseXDown == 0 || recs[2].Mi.MouseData != xButton2 {
		t.Errorf("downs %+v %+v", recs[1].Mi, recs[2].Mi)
	}
	recs = pointerRecords(vs, m, 10, 10, 1|16, 0)
	if recs[1].Mi.Flags&mouseLeftUp == 0 || recs[2].Mi.Flags&mouseXUp == 0 {
		t.Errorf("ups %+v %+v", recs[1].Mi, recs[2].Mi)
	}
	if n := len(pointerRecords(vs, m, 11, 10, 1, 1)); n != 1 {
		t.Errorf("a drag with the button held: %d records; want the move only", n)
	}
}

func TestWheelDirectionAndBounds(t *testing.T) {
	recs := wheelRecords(0, 120) // DOM: scroll down
	if len(recs) != 1 || recs[0].Mi.Flags != mouseWheel || int32(recs[0].Mi.MouseData) != -120 {
		t.Fatalf("down: %+v", recs)
	}
	recs = wheelRecords(-30000, 0)
	if len(recs) != 1 || recs[0].Mi.Flags != mouseHWheel || int32(recs[0].Mi.MouseData) != -maxWheel {
		t.Fatalf("left, clamped: %+v", recs)
	}
	if len(wheelRecords(0, 0)) != 0 {
		t.Fatal("empty wheel produced input")
	}
}

func TestKeysAndText(t *testing.T) {
	r := keyRecords(Scan{0x4B, true}, false)[0]
	if f := uint32(r.Mi.Dy); f != keyScancode|keyExtended|keyUp {
		t.Errorf("ArrowLeft up flags %#x", f)
	}
	if sc := uint16(uint32(r.Mi.Dx) >> 16); sc != 0x4B {
		t.Errorf("scan %#x", sc)
	}
	recs := textRecords("a\n\x07😀")
	// a (down, up), \r (down, up), 😀 as a surrogate pair (two units, down and up each).
	if len(recs) != 8 {
		t.Fatalf("%d records", len(recs))
	}
	if uint16(uint32(recs[2].Mi.Dx)>>16) != '\r' {
		t.Error("newline is not sent as Enter's character")
	}
	if u := uint16(uint32(recs[4].Mi.Dx) >> 16); u != 0xD83D {
		t.Errorf("high surrogate %#x", u)
	}
	for _, r := range recs {
		if uint32(r.Mi.Dy)&keyUnicode == 0 {
			t.Fatal("text sent without KEYEVENTF_UNICODE")
		}
	}
}
