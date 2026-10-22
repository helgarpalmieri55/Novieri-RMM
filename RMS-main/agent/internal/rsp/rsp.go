// Package rsp is the rsp/1 remote-screen frame format
// (docs/kernel/wire-protocol/remote-screen-protocol.md).
//
// A frame is one binary WebSocket message: type (u8) | seq (u32, big-endian)
// | body. The type's range carries its direction, which the gateway enforces:
// 0x01-0x3F agent to viewer, 0x40-0x7F viewer to agent, 0xF0-0xFF between a
// leg and the gateway only. Readers ignore types they do not know.
//
// This package is pure encoding: no goroutines, no I/O, nothing allocated at
// init, so importing it costs the idle agent nothing.
package rsp

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"unicode/utf8"
)

// Subprotocol is the WebSocket subprotocol both relay legs must negotiate.
const Subprotocol = "itms-rsp.1"

// MaxMessageBytes is the largest message either side may send.
const MaxMessageBytes = 1 << 20

// HeaderBytes is the fixed header before every body.
const HeaderBytes = 5

// Type is a frame type.
type Type byte

// Agent to viewer.
const (
	Hello       Type = 0x01
	FrameBegin  Type = 0x02
	Tile        Type = 0x03
	CopyRect    Type = 0x04
	FrameEnd    Type = 0x05
	CursorShape Type = 0x06
	CursorPos   Type = 0x07
	State       Type = 0x08
)

// Viewer to agent.
const (
	Ack           Type = 0x40
	Pointer       Type = 0x41
	Wheel         Type = 0x42
	Key           Type = 0x43
	Text          Type = 0x44
	MonitorSelect Type = 0x45
	Quality       Type = 0x46
	Refresh       Type = 0x47
	SAS           Type = 0x48
	Control       Type = 0x49
)

// Between a leg and the gateway; never forwarded.
const (
	Auth   Type = 0xF0
	AuthOK Type = 0xF1
)

// Tile and cursor codecs.
const (
	CodecFill byte = 0
	CodecJPEG byte = 1
	CodecPNG  byte = 2
)

// Limits the agent enforces on viewer input (spec §3.3).
const (
	MaxTextBytes = 256
	MaxKeyCode   = 32
)

// WebSocket close codes used on both relay legs.
const (
	CloseEnded        = 4000
	CloseProtocol     = 4400
	CloseUnauthorized = 4401
	CloseRefused      = 4403
	CloseTimeout      = 4408
	CloseConflict     = 4409
)

// ErrFrame is returned for a message that is not a well-formed frame of the
// expected kind.
var ErrFrame = errors.New("rsp: malformed frame")

// AgentToViewer reports whether t travels from the agent to the viewer.
func AgentToViewer(t Type) bool { return t >= 0x01 && t <= 0x3F }

// ViewerToAgent reports whether t travels from the viewer to the agent.
func ViewerToAgent(t Type) bool { return t >= 0x40 && t <= 0x7F }

// Frame is one decoded message. Body aliases the input to Decode.
type Frame struct {
	Type Type
	Seq  uint32
	Body []byte
}

// Encode builds a frame.
func Encode(t Type, seq uint32, body []byte) []byte {
	out := make([]byte, HeaderBytes+len(body))
	out[0] = byte(t)
	binary.BigEndian.PutUint32(out[1:5], seq)
	copy(out[HeaderBytes:], body)
	return out
}

// Decode splits a message into its header and body without copying.
func Decode(b []byte) (Frame, error) {
	if len(b) < HeaderBytes {
		return Frame{}, fmt.Errorf("%w: %d bytes", ErrFrame, len(b))
	}
	if len(b) > MaxMessageBytes {
		return Frame{}, fmt.Errorf("%w: exceeds %d bytes", ErrFrame, MaxMessageBytes)
	}
	return Frame{Type: Type(b[0]), Seq: binary.BigEndian.Uint32(b[1:5]), Body: b[HeaderBytes:]}, nil
}

// AuthBody is the AUTH frame's body on the agent leg. The viewer leg sends
// "ticket" instead of "token"; the agent never does.
type AuthBody struct {
	SessionID string `json:"session_id"`
	Token     string `json:"token"`
}

// StateBody is a STATE frame's body. Unknown members are ignored by readers.
type StateBody struct {
	State    string `json:"state"`
	Reason   string `json:"reason,omitempty"`
	TimeoutS int    `json:"timeout_s,omitempty"`
}

// EncodeJSON builds a frame whose body is a JSON object (HELLO, STATE, AUTH).
func EncodeJSON(t Type, seq uint32, v any) ([]byte, error) {
	body, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return Encode(t, seq, body), nil
}

// DecodeJSON reads a JSON-object body into v.
func DecodeJSON(f Frame, v any) error {
	if len(f.Body) == 0 || f.Body[0] != '{' {
		return fmt.Errorf("%w: body is not a JSON object", ErrFrame)
	}
	if err := json.Unmarshal(f.Body, v); err != nil {
		return fmt.Errorf("%w: %v", ErrFrame, err)
	}
	return nil
}

// Rect is a TILE's placement on the selected monitor.
type Rect struct{ X, Y, W, H uint16 }

// EncodeTile builds a TILE: x, y, w, h (u16) | codec (u8) | data.
func EncodeTile(seq uint32, r Rect, codec byte, data []byte) []byte {
	body := make([]byte, 9+len(data))
	putRect(body, r)
	body[8] = codec
	copy(body[9:], data)
	return Encode(Tile, seq, body)
}

// DecodeTile reads a TILE body.
func DecodeTile(f Frame) (r Rect, codec byte, data []byte, err error) {
	if f.Type != Tile || len(f.Body) < 9 {
		return Rect{}, 0, nil, fmt.Errorf("%w: TILE", ErrFrame)
	}
	return getRect(f.Body), f.Body[8], f.Body[9:], nil
}

// EncodeCopyRect builds a COPYRECT: src_x, src_y, dst_x, dst_y, w, h (u16).
func EncodeCopyRect(seq uint32, srcX, srcY uint16, dst Rect) []byte {
	body := make([]byte, 12)
	binary.BigEndian.PutUint16(body[0:], srcX)
	binary.BigEndian.PutUint16(body[2:], srcY)
	binary.BigEndian.PutUint16(body[4:], dst.X)
	binary.BigEndian.PutUint16(body[6:], dst.Y)
	binary.BigEndian.PutUint16(body[8:], dst.W)
	binary.BigEndian.PutUint16(body[10:], dst.H)
	return Encode(CopyRect, seq, body)
}

// EncodeCursorPos builds a CURSOR_POS: x, y (u16) | visible (u8).
func EncodeCursorPos(seq uint32, x, y uint16, visible bool) []byte {
	body := make([]byte, 5)
	binary.BigEndian.PutUint16(body[0:], x)
	binary.BigEndian.PutUint16(body[2:], y)
	if visible {
		body[4] = 1
	}
	return Encode(CursorPos, seq, body)
}

// EncodeAck builds an ACK naming the seq of the last FRAME_END received.
func EncodeAck(seq, frameEndSeq uint32) []byte {
	body := make([]byte, 4)
	binary.BigEndian.PutUint32(body, frameEndSeq)
	return Encode(Ack, seq, body)
}

// DecodeAck reads an ACK.
func DecodeAck(f Frame) (uint32, error) {
	if f.Type != Ack || len(f.Body) < 4 {
		return 0, fmt.Errorf("%w: ACK", ErrFrame)
	}
	return binary.BigEndian.Uint32(f.Body), nil
}

// PointerEvent is a POINTER body: absolute position on the selected monitor
// and a button bitmask (1 left, 2 right, 4 middle, 8 back, 16 forward).
type PointerEvent struct {
	X, Y    uint16
	Buttons byte
}

// EncodePointer builds a POINTER.
func EncodePointer(seq uint32, p PointerEvent) []byte {
	body := make([]byte, 5)
	binary.BigEndian.PutUint16(body[0:], p.X)
	binary.BigEndian.PutUint16(body[2:], p.Y)
	body[4] = p.Buttons
	return Encode(Pointer, seq, body)
}

// DecodePointer reads a POINTER.
func DecodePointer(f Frame) (PointerEvent, error) {
	if f.Type != Pointer || len(f.Body) < 5 {
		return PointerEvent{}, fmt.Errorf("%w: POINTER", ErrFrame)
	}
	return PointerEvent{
		X:       binary.BigEndian.Uint16(f.Body[0:]),
		Y:       binary.BigEndian.Uint16(f.Body[2:]),
		Buttons: f.Body[4],
	}, nil
}

// DecodeWheel reads a WHEEL: dx, dy (i16).
func DecodeWheel(f Frame) (dx, dy int16, err error) {
	if f.Type != Wheel || len(f.Body) < 4 {
		return 0, 0, fmt.Errorf("%w: WHEEL", ErrFrame)
	}
	return int16(binary.BigEndian.Uint16(f.Body[0:])), int16(binary.BigEndian.Uint16(f.Body[2:])), nil
}

// DecodeKey reads a KEY: down (u8) | KeyboardEvent.code (UTF-8, at most
// MaxKeyCode bytes).
func DecodeKey(f Frame) (down bool, code string, err error) {
	if f.Type != Key || len(f.Body) < 2 || len(f.Body)-1 > MaxKeyCode || !utf8.Valid(f.Body[1:]) {
		return false, "", fmt.Errorf("%w: KEY", ErrFrame)
	}
	return f.Body[0] == 1, string(f.Body[1:]), nil
}

// DecodeText reads a TEXT: UTF-8, at most MaxTextBytes.
func DecodeText(f Frame) (string, error) {
	if f.Type != Text || len(f.Body) > MaxTextBytes || !utf8.Valid(f.Body) {
		return "", fmt.Errorf("%w: TEXT", ErrFrame)
	}
	return string(f.Body), nil
}

// DecodeControl reads a CONTROL: mode (u8), 1 = control, 0 = view only.
func DecodeControl(f Frame) (control bool, err error) {
	if f.Type != Control || len(f.Body) < 1 {
		return false, fmt.Errorf("%w: CONTROL", ErrFrame)
	}
	return f.Body[0] == 1, nil
}

func putRect(b []byte, r Rect) {
	binary.BigEndian.PutUint16(b[0:], r.X)
	binary.BigEndian.PutUint16(b[2:], r.Y)
	binary.BigEndian.PutUint16(b[4:], r.W)
	binary.BigEndian.PutUint16(b[6:], r.H)
}

func getRect(b []byte) Rect {
	return Rect{
		X: binary.BigEndian.Uint16(b[0:]),
		Y: binary.BigEndian.Uint16(b[2:]),
		W: binary.BigEndian.Uint16(b[4:]),
		H: binary.BigEndian.Uint16(b[6:]),
	}
}

// DecodeCopyRect reads a COPYRECT body.
func DecodeCopyRect(f Frame) (srcX, srcY uint16, dst Rect, err error) {
	if f.Type != CopyRect || len(f.Body) < 12 {
		return 0, 0, Rect{}, fmt.Errorf("%w: COPYRECT", ErrFrame)
	}
	return binary.BigEndian.Uint16(f.Body[0:]), binary.BigEndian.Uint16(f.Body[2:]), getRect(f.Body[4:]), nil
}

// DecodeCursorPos reads a CURSOR_POS body.
func DecodeCursorPos(f Frame) (x, y uint16, visible bool, err error) {
	if f.Type != CursorPos || len(f.Body) < 5 {
		return 0, 0, false, fmt.Errorf("%w: CURSOR_POS", ErrFrame)
	}
	return binary.BigEndian.Uint16(f.Body[0:]), binary.BigEndian.Uint16(f.Body[2:]), f.Body[4] == 1, nil
}

// CursorShapeHeader is a CURSOR_SHAPE's fixed part.
type CursorShapeHeader struct {
	HotX, HotY, W, H uint16
	Codec            byte
}

// EncodeCursorShape builds a CURSOR_SHAPE: hot_x, hot_y, w, h (u16) | codec
// (u8) | data.
func EncodeCursorShape(seq uint32, h CursorShapeHeader, data []byte) []byte {
	body := make([]byte, 9+len(data))
	putRect(body, Rect{h.HotX, h.HotY, h.W, h.H})
	body[8] = h.Codec
	copy(body[9:], data)
	return Encode(CursorShape, seq, body)
}

// DecodeCursorShape reads a CURSOR_SHAPE body.
func DecodeCursorShape(f Frame) (CursorShapeHeader, []byte, error) {
	if f.Type != CursorShape || len(f.Body) < 9 {
		return CursorShapeHeader{}, nil, fmt.Errorf("%w: CURSOR_SHAPE", ErrFrame)
	}
	r := getRect(f.Body)
	return CursorShapeHeader{HotX: r.X, HotY: r.Y, W: r.W, H: r.H, Codec: f.Body[8]}, f.Body[9:], nil
}

// EncodeWheel builds a WHEEL: dx, dy (i16), in 1/120ths of a notch; positive
// dy scrolls down and positive dx scrolls right (DOM WheelEvent's sense).
func EncodeWheel(seq uint32, dx, dy int16) []byte {
	body := make([]byte, 4)
	binary.BigEndian.PutUint16(body[0:], uint16(dx))
	binary.BigEndian.PutUint16(body[2:], uint16(dy))
	return Encode(Wheel, seq, body)
}

// EncodeKey builds a KEY.
func EncodeKey(seq uint32, down bool, code string) []byte {
	body := make([]byte, 1+len(code))
	if down {
		body[0] = 1
	}
	copy(body[1:], code)
	return Encode(Key, seq, body)
}

// EncodeText builds a TEXT.
func EncodeText(seq uint32, text string) []byte { return Encode(Text, seq, []byte(text)) }

// EncodeControl builds a CONTROL: 1 = control, 0 = view only.
func EncodeControl(seq uint32, control bool) []byte {
	body := []byte{0}
	if control {
		body[0] = 1
	}
	return Encode(Control, seq, body)
}

// EncodeMonitorSelect builds a MONITOR_SELECT.
func EncodeMonitorSelect(seq uint32, id byte) []byte { return Encode(MonitorSelect, seq, []byte{id}) }

// DecodeMonitorSelect reads a MONITOR_SELECT.
func DecodeMonitorSelect(f Frame) (byte, error) {
	if f.Type != MonitorSelect || len(f.Body) < 1 {
		return 0, fmt.Errorf("%w: MONITOR_SELECT", ErrFrame)
	}
	return f.Body[0], nil
}

// Quality levels carried by QUALITY.
const (
	QualityAuto   byte = 0
	QualityLow    byte = 1
	QualityMedium byte = 2
	QualityHigh   byte = 3
)

// EncodeQuality builds a QUALITY.
func EncodeQuality(seq uint32, level byte) []byte { return Encode(Quality, seq, []byte{level}) }

// DecodeQuality reads a QUALITY; levels above High are refused.
func DecodeQuality(f Frame) (byte, error) {
	if f.Type != Quality || len(f.Body) < 1 || f.Body[0] > QualityHigh {
		return 0, fmt.Errorf("%w: QUALITY", ErrFrame)
	}
	return f.Body[0], nil
}

// Monitor is one entry of HELLO's monitors list.
type Monitor struct {
	ID      int  `json:"id"`
	X       int  `json:"x"`
	Y       int  `json:"y"`
	Width   int  `json:"width"`
	Height  int  `json:"height"`
	DPI     int  `json:"dpi"`
	Primary bool `json:"primary"`
}

// HelloBody is HELLO's body. Selected (additive) names the monitor that
// following frames describe; HELLO is resent when it or the geometry changes.
type HelloBody struct {
	Version      int       `json:"version"`
	Capabilities []string  `json:"capabilities"`
	Monitors     []Monitor `json:"monitors"`
	Codecs       []string  `json:"codecs"`
	Selected     int       `json:"selected"`
}
