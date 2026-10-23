package rsp

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"testing"
)

// The same vectors the server's tests read: one contract, three readers.
const vectorsPath = "../../../docs/kernel/wire-protocol/rsp1-vectors.json"

type vector struct {
	Name   string         `json:"name"`
	Type   int            `json:"type"`
	Seq    uint32         `json:"seq"`
	Hex    string         `json:"hex"`
	JSON   map[string]any `json:"json"`
	Fields map[string]any `json:"fields"`
}

func loadVectors(t *testing.T) map[string]vector {
	t.Helper()
	raw, err := os.ReadFile(vectorsPath)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Frames []vector `json:"frames"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	out := map[string]vector{}
	for _, v := range doc.Frames {
		out[v.Name] = v
	}
	if len(out) < 10 {
		t.Fatalf("only %d vectors in %s", len(out), vectorsPath)
	}
	return out
}

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func num(v vector, k string) int { return int(v.Fields[k].(float64)) }

func TestEveryVectorDecodesToItsHeader(t *testing.T) {
	for name, v := range loadVectors(t) {
		f, err := Decode(mustHex(t, v.Hex))
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if int(f.Type) != v.Type || f.Seq != v.Seq {
			t.Errorf("%s: got type %#x seq %d", name, f.Type, f.Seq)
		}
		if v.JSON != nil {
			var got map[string]any
			if err := DecodeJSON(f, &got); err != nil {
				t.Fatalf("%s: %v", name, err)
			}
			if !reflect.DeepEqual(got, v.JSON) {
				t.Errorf("%s: body %v, want %v", name, got, v.JSON)
			}
		}
	}
}

func TestBinaryLayoutsEncodeExactly(t *testing.T) {
	vs := loadVectors(t)
	check := func(name string, got []byte) {
		t.Helper()
		if want := mustHex(t, vs[name].Hex); !bytes.Equal(got, want) {
			t.Errorf("%s:\n got %x\nwant %x", name, got, want)
		}
	}
	tile := vs["tile_fill"]
	check("tile_fill", EncodeTile(tile.Seq,
		Rect{uint16(num(tile, "x")), uint16(num(tile, "y")), uint16(num(tile, "w")), uint16(num(tile, "h"))},
		byte(num(tile, "codec")), mustHex(t, tile.Fields["data_hex"].(string))))
	cr := vs["copyrect"]
	check("copyrect", EncodeCopyRect(cr.Seq, uint16(num(cr, "src_x")), uint16(num(cr, "src_y")),
		Rect{uint16(num(cr, "x")), uint16(num(cr, "y")), uint16(num(cr, "w")), uint16(num(cr, "h"))}))
	cp := vs["cursor_pos"]
	check("cursor_pos", EncodeCursorPos(cp.Seq, uint16(num(cp, "x")), uint16(num(cp, "y")), num(cp, "visible") == 1))
	ack := vs["ack"]
	check("ack", EncodeAck(ack.Seq, uint32(num(ack, "frame_seq"))))
	p := vs["pointer"]
	check("pointer", EncodePointer(p.Seq, PointerEvent{uint16(num(p, "x")), uint16(num(p, "y")), byte(num(p, "buttons"))}))
	check("frame_begin", Encode(FrameBegin, vs["frame_begin"].Seq, nil))
	check("frame_end", Encode(FrameEnd, vs["frame_end"].Seq, nil))
	check("refresh", Encode(Refresh, vs["refresh"].Seq, nil))
}

func TestViewerInputDecodes(t *testing.T) {
	vs := loadVectors(t)
	dec := func(name string) Frame {
		f, err := Decode(mustHex(t, vs[name].Hex))
		if err != nil {
			t.Fatal(err)
		}
		return f
	}
	if n, err := DecodeAck(dec("ack")); err != nil || n != 5 {
		t.Errorf("ack = %d, %v", n, err)
	}
	if p, err := DecodePointer(dec("pointer")); err != nil || p != (PointerEvent{100, 200, 1}) {
		t.Errorf("pointer = %+v, %v", p, err)
	}
	if dx, dy, err := DecodeWheel(dec("wheel")); err != nil || dx != 0 || dy != -120 {
		t.Errorf("wheel = %d %d, %v", dx, dy, err)
	}
	if down, code, err := DecodeKey(dec("key")); err != nil || !down || code != "KeyA" {
		t.Errorf("key = %v %q, %v", down, code, err)
	}
	if s, err := DecodeText(dec("text")); err != nil || s != "h\u00e9llo" {
		t.Errorf("text = %q, %v", s, err)
	}
	if c, err := DecodeControl(dec("control_view_only")); err != nil || c {
		t.Errorf("control = %v, %v", c, err)
	}
	r, codec, data, err := DecodeTile(dec("tile_fill"))
	if err != nil || r != (Rect{16, 32, 64, 48}) || codec != CodecFill || !bytes.Equal(data, []byte{0xff, 0x80, 0}) {
		t.Errorf("tile = %+v %d %x, %v", r, codec, data, err)
	}
}

func TestDirectionIsByRange(t *testing.T) {
	for _, tt := range []struct {
		t        Type
		up, down bool
		name     string
	}{
		{Hello, true, false, "HELLO"},
		{State, true, false, "STATE"},
		{0x3F, true, false, "unassigned agent type"},
		{Pointer, false, true, "POINTER"},
		{0x7F, false, true, "unassigned viewer type"},
		{Auth, false, false, "AUTH"},
		{AuthOK, false, false, "AUTH_OK"},
		{0x00, false, false, "zero"},
		{0x80, false, false, "reserved"},
	} {
		if AgentToViewer(tt.t) != tt.up || ViewerToAgent(tt.t) != tt.down {
			t.Errorf("%s: agent->viewer %v viewer->agent %v", tt.name, AgentToViewer(tt.t), ViewerToAgent(tt.t))
		}
	}
}

func TestMalformedInputIsRefused(t *testing.T) {
	for name, b := range map[string][]byte{
		"empty":    nil,
		"short":    {byte(Tile), 0, 0, 0},
		"oversize": make([]byte, MaxMessageBytes+1),
	} {
		if _, err := Decode(b); !errors.Is(err, ErrFrame) {
			t.Errorf("%s: err = %v", name, err)
		}
	}
	long := Encode(Text, 1, bytes.Repeat([]byte("a"), MaxTextBytes+1))
	f, _ := Decode(long)
	if _, err := DecodeText(f); !errors.Is(err, ErrFrame) {
		t.Errorf("TEXT over %d bytes accepted", MaxTextBytes)
	}
	bad := Encode(Text, 1, []byte{0xff, 0xfe})
	f, _ = Decode(bad)
	if _, err := DecodeText(f); !errors.Is(err, ErrFrame) {
		t.Error("invalid UTF-8 TEXT accepted")
	}
	f, _ = Decode(Encode(Key, 1, append([]byte{1}, bytes.Repeat([]byte("K"), MaxKeyCode+1)...)))
	if _, _, err := DecodeKey(f); !errors.Is(err, ErrFrame) {
		t.Error("oversized KEY code accepted")
	}
	f, _ = Decode(Encode(Pointer, 1, []byte{0, 1}))
	if _, err := DecodePointer(f); !errors.Is(err, ErrFrame) {
		t.Error("short POINTER accepted")
	}
	f, _ = Decode(Encode(State, 1, []byte("[]")))
	var s StateBody
	if err := DecodeJSON(f, &s); !errors.Is(err, ErrFrame) {
		t.Error("non-object STATE accepted")
	}
}

func TestStateRoundTripIgnoresUnknownMembers(t *testing.T) {
	f, _ := Decode(Encode(State, 9, []byte(`{"state":"ended","reason":"ended_by_user","later":1}`)))
	var s StateBody
	if err := DecodeJSON(f, &s); err != nil || s.State != "ended" || s.Reason != "ended_by_user" {
		t.Fatalf("state = %+v, %v", s, err)
	}
	raw, err := EncodeJSON(State, 10, StateBody{State: "consent_pending", TimeoutS: 30})
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(raw, []byte(`"timeout_s":30`)) || bytes.Contains(raw, []byte("reason")) {
		t.Errorf("encoded %s", raw)
	}
}

func TestEncodeAllocatesOnlyTheFrame(t *testing.T) {
	// A pure codec: the returned frame is the only allocation.
	allocs := testing.AllocsPerRun(100, func() { _ = Encode(FrameEnd, 1, nil) })
	if allocs > 1 {
		t.Errorf("Encode allocates %.0f times", allocs)
	}
}

func TestViewerInputEncodesExactly(t *testing.T) {
	vs := loadVectors(t)
	check := func(name string, got []byte) {
		t.Helper()
		if want := mustHex(t, vs[name].Hex); !bytes.Equal(got, want) {
			t.Errorf("%s:\n got %x\nwant %x", name, got, want)
		}
	}
	w := vs["wheel"]
	check("wheel", EncodeWheel(w.Seq, int16(num(w, "dx")), int16(num(w, "dy"))))
	k := vs["key"]
	check("key", EncodeKey(k.Seq, num(k, "down") == 1, k.Fields["code"].(string)))
	tx := vs["text"]
	check("text", EncodeText(tx.Seq, tx.Fields["text"].(string)))
	c := vs["control_view_only"]
	check("control_view_only", EncodeControl(c.Seq, num(c, "mode") == 1))
	ms := vs["monitor_select"]
	check("monitor_select", EncodeMonitorSelect(ms.Seq, byte(num(ms, "monitor_id"))))
	q := vs["quality"]
	check("quality", EncodeQuality(q.Seq, byte(num(q, "level"))))
	cs := vs["cursor_shape"]
	check("cursor_shape", EncodeCursorShape(cs.Seq, CursorShapeHeader{
		HotX: uint16(num(cs, "hot_x")), HotY: uint16(num(cs, "hot_y")),
		W: uint16(num(cs, "w")), H: uint16(num(cs, "h")), Codec: byte(num(cs, "codec")),
	}, mustHex(t, cs.Fields["data_hex"].(string))))
}

func TestAgentFramesDecode(t *testing.T) {
	vs := loadVectors(t)
	dec := func(name string) Frame {
		f, err := Decode(mustHex(t, vs[name].Hex))
		if err != nil {
			t.Fatal(err)
		}
		return f
	}
	if sx, sy, r, err := DecodeCopyRect(dec("copyrect")); err != nil || sx != 0 || sy != 0 || r != (Rect{10, 20, 100, 50}) {
		t.Errorf("copyrect = %d %d %+v, %v", sx, sy, r, err)
	}
	if x, y, vis, err := DecodeCursorPos(dec("cursor_pos")); err != nil || x != 640 || y != 360 || !vis {
		t.Errorf("cursor_pos = %d %d %v, %v", x, y, vis, err)
	}
	h, data, err := DecodeCursorShape(dec("cursor_shape"))
	if err != nil || h != (CursorShapeHeader{HotX: 1, HotY: 2, W: 16, H: 16, Codec: CodecPNG}) || len(data) != 4 {
		t.Errorf("cursor_shape = %+v %x, %v", h, data, err)
	}
	if id, err := DecodeMonitorSelect(dec("monitor_select")); err != nil || id != 1 {
		t.Errorf("monitor_select = %d, %v", id, err)
	}
	if l, err := DecodeQuality(dec("quality")); err != nil || l != QualityHigh {
		t.Errorf("quality = %d, %v", l, err)
	}
	f, _ := Decode(EncodeQuality(1, 9))
	if _, err := DecodeQuality(f); !errors.Is(err, ErrFrame) {
		t.Error("QUALITY level 9 accepted")
	}
}
