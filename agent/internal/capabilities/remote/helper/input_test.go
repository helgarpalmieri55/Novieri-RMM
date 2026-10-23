package helper

import (
	"fmt"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

type recorder struct{ events []string }

func (r *recorder) Pointer(x, y int, b byte) error {
	r.events = append(r.events, fmt.Sprintf("pointer %d,%d %d", x, y, b))
	return nil
}
func (r *recorder) Wheel(dx, dy int) error {
	r.events = append(r.events, fmt.Sprintf("wheel %d,%d", dx, dy))
	return nil
}
func (r *recorder) Key(s Scan, down bool) error {
	r.events = append(r.events, fmt.Sprintf("key %#x ext=%v down=%v", s.Code, s.Extended, down))
	return nil
}
func (r *recorder) Text(s string) error {
	r.events = append(r.events, "text "+s)
	return nil
}
func (r *recorder) Close() error { return nil }

func frame(b []byte) rsp.Frame {
	f, err := rsp.Decode(b)
	if err != nil {
		panic(err)
	}
	return f
}

func TestTheKeyTableIsAFixedAllowlist(t *testing.T) {
	seen := map[Scan]string{}
	for code, s := range keyTable {
		if len(code) > rsp.MaxKeyCode {
			t.Errorf("%q is longer than a KEY frame can carry", code)
		}
		if other, dup := seen[s]; dup {
			t.Errorf("%q and %q share scan code %+v", code, other, s)
		}
		seen[s] = code
		if s.Code == 0 || s.Code > 0x7F {
			t.Errorf("%q maps to %#x, outside set 1", code, s.Code)
		}
	}
	for code, want := range map[string]Scan{
		"KeyA": {0x1E, false}, "Enter": {0x1C, false}, "NumpadEnter": {0x1C, true},
		"ArrowLeft": {0x4B, true}, "MetaLeft": {0x5B, true}, "IntlBackslash": {0x56, false},
		"ControlRight": {0x1D, true}, "AltRight": {0x38, true},
	} {
		if got, ok := LookupKey(code); !ok || got != want {
			t.Errorf("%s = %+v %v; want %+v", code, got, ok, want)
		}
	}
	for _, code := range []string{"", "Fn", "Power", "KeyA ", "keya", "Unidentified"} {
		if _, ok := LookupKey(code); ok {
			t.Errorf("%q is injectable; the table is an allowlist", code)
		}
	}
}

func TestViewOnlyInjectsNothing(t *testing.T) {
	r := &recorder{}
	g := newInputGate(r, 100, 100, false)
	now := time.Unix(0, 0)
	for _, b := range [][]byte{
		rsp.EncodePointer(1, rsp.PointerEvent{X: 1, Y: 1, Buttons: 1}),
		rsp.EncodeWheel(2, 0, 120),
		rsp.EncodeKey(3, true, "KeyA"),
		rsp.EncodeText(4, "hi"),
	} {
		if g.handle(frame(b), now) {
			t.Fatalf("%x injected while view only", b[0])
		}
	}
	if len(r.events) != 0 {
		t.Fatalf("events %v", r.events)
	}
}

func TestCoordinatesAreClampedToTheMonitor(t *testing.T) {
	r := &recorder{}
	g := newInputGate(r, 1920, 1080, true)
	g.handle(frame(rsp.EncodePointer(1, rsp.PointerEvent{X: 65535, Y: 5000, Buttons: 0xFF})), time.Unix(0, 0))
	if want := "pointer 1919,1079 31"; r.events[0] != want {
		t.Fatalf("got %q; want %q (clamped, buttons masked to the five defined)", r.events[0], want)
	}
}

func TestUnknownKeysAndStrayReleasesAreDropped(t *testing.T) {
	r := &recorder{}
	g := newInputGate(r, 10, 10, true)
	now := time.Unix(0, 0)
	g.handle(frame(rsp.EncodeKey(1, true, "Power")), now)
	g.handle(frame(rsp.EncodeKey(2, false, "KeyB")), now)
	g.handle(frame(rsp.EncodeKey(3, true, "KeyB")), now)
	g.handle(frame(rsp.EncodeKey(4, false, "KeyB")), now)
	want := []string{"key 0x30 ext=false down=true", "key 0x30 ext=false down=false"}
	if fmt.Sprint(r.events) != fmt.Sprint(want) {
		t.Fatalf("events %v; want %v", r.events, want)
	}
}

func TestLeavingControlReleasesHeldKeysAndButtons(t *testing.T) {
	r := &recorder{}
	g := newInputGate(r, 100, 100, true)
	now := time.Unix(0, 0)
	g.handle(frame(rsp.EncodeKey(1, true, "ControlLeft")), now)
	g.handle(frame(rsp.EncodeKey(2, true, "AltLeft")), now)
	g.handle(frame(rsp.EncodePointer(3, rsp.PointerEvent{X: 5, Y: 6, Buttons: 1})), now)
	r.events = nil
	g.setControl(false)
	if len(r.events) != 3 {
		t.Fatalf("release events %v; want two key-ups and a button release", r.events)
	}
	if r.events[2] != "pointer 5,6 0" {
		t.Fatalf("button release %q", r.events[2])
	}
	r.events = nil
	g.setControl(false)
	if len(r.events) != 0 {
		t.Fatalf("second release %v", r.events)
	}
}

func TestInputIsRateLimited(t *testing.T) {
	r := &recorder{}
	g := newInputGate(r, 100, 100, true)
	now := time.Unix(100, 0)
	n := 0
	for i := 0; i < 1000; i++ {
		if g.handle(frame(rsp.EncodePointer(uint32(i), rsp.PointerEvent{X: 1, Y: 1})), now) {
			n++
		}
	}
	if n != inputBurst {
		t.Fatalf("%d of 1000 simultaneous events injected; want the burst of %d", n, inputBurst)
	}
	// One second later the bucket has refilled at the cap, never beyond the burst.
	n = 0
	for i := 0; i < 1000; i++ {
		if g.handle(frame(rsp.EncodePointer(uint32(i), rsp.PointerEvent{X: 1, Y: 1})), now.Add(time.Second)) {
			n++
		}
	}
	if n != inputBurst {
		t.Fatalf("after a second: %d", n)
	}
	// Sustained: 1000 events spread over one second get at most 500 + burst.
	n = 0
	start := now.Add(10 * time.Second)
	for i := 0; i < 1000; i++ {
		if g.handle(frame(rsp.EncodePointer(uint32(i), rsp.PointerEvent{X: 1, Y: 1})), start.Add(time.Duration(i)*time.Millisecond)) {
			n++
		}
	}
	if n > MaxInputPerSecond+inputBurst {
		t.Fatalf("%d events in one second", n)
	}
}

func TestNonInputFramesNeverReachTheInjector(t *testing.T) {
	r := &recorder{}
	g := newInputGate(r, 100, 100, true)
	for _, b := range [][]byte{rsp.EncodeAck(1, 1), rsp.Encode(rsp.SAS, 2, nil), rsp.EncodeControl(3, true), rsp.Encode(0x7E, 4, []byte{1})} {
		if g.handle(frame(b), time.Unix(0, 0)) {
			t.Fatalf("%#x injected", b[0])
		}
	}
}

func TestSanitizeStripsWhatCouldMakeThePromptLie(t *testing.T) {
	rlo, zwsp, bom := string(rune(0x202e)), string(rune(0x200b)), string(rune(0xfeff))
	for in, want := range map[string]string{
		"Ann" + rlo + "evil":          "Ann evil",
		"  Bob \n\t Smith  ":          "Bob Smith",
		"a\x00b\x1bc":                 "a b c",
		"zero" + zwsp + "width" + bom: "zero width",
	} {
		if got := Sanitize(in, 80); got != want {
			t.Errorf("Sanitize(%q) = %q; want %q", in, got, want)
		}
	}
	if got := Sanitize("ééééé", 3); got != "ééé" {
		t.Errorf("cap by runes: %q", got)
	}
}

func TestPointerShapes(t *testing.T) {
	// Monochrome 8x2: AND mask rows then XOR mask rows, pitch 1 byte.
	mono := []byte{
		0b11110000, 0b00001111, // AND
		0b00001100, 0b11000011, // XOR
	}
	img, ok := DecodePointerShape(ShapeMonochrome, 8, 2, 1, mono)
	if !ok {
		t.Fatal("monochrome refused")
	}
	for _, c := range []struct {
		x, y       int
		a, r, g, b uint8
	}{
		{0, 0, 0, 0, 0, 0},         // AND=1 XOR=0: transparent
		{4, 0, 255, 255, 255, 255}, // AND=0 XOR=1: white
		{6, 0, 255, 0, 0, 0},       // AND=0 XOR=0: black
		{0, 1, 255, 255, 255, 255}, // AND=0 XOR=1: white
		{6, 1, 255, 0, 0, 0},       // AND=1 XOR=1: inverted, drawn black
	} {
		p := img.NRGBAAt(c.x, c.y)
		if p.A != c.a || (c.a != 0 && (p.R != c.r || p.G != c.g || p.B != c.b)) {
			t.Errorf("(%d,%d) = %+v", c.x, c.y, p)
		}
	}
	color := []byte{1, 2, 3, 128, 9, 9, 9, 0}
	img, ok = DecodePointerShape(ShapeColor, 2, 1, 8, color)
	if !ok || img.NRGBAAt(0, 0).R != 3 || img.NRGBAAt(0, 0).B != 1 || img.NRGBAAt(0, 0).A != 128 {
		t.Fatalf("colour pixel %+v", img.NRGBAAt(0, 0))
	}
	masked := []byte{10, 20, 30, 0, 0, 0, 0, 0xFF}
	img, _ = DecodePointerShape(ShapeMaskedColor, 2, 1, 8, masked)
	if img.NRGBAAt(0, 0).A != 255 || img.NRGBAAt(1, 0).A != 0 {
		t.Fatalf("masked %+v %+v", img.NRGBAAt(0, 0), img.NRGBAAt(1, 0))
	}
	for name, args := range map[string][]int{
		"too big": {ShapeColor, 1000, 1, 4000}, "short buffer": {ShapeColor, 32, 32, 128},
		"unknown kind": {7, 2, 1, 8},
	} {
		if _, ok := DecodePointerShape(args[0], args[1], args[2], args[3], color); ok {
			t.Errorf("%s accepted", name)
		}
	}
	if png := EncodeCursorPNG(img); len(png) < 8 || string(png[1:4]) != "PNG" {
		t.Fatal("not a PNG")
	}
}
