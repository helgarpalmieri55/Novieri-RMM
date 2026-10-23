/**
 * rsp/1, the remote-screen protocol (docs/kernel/wire-protocol/remote-screen-protocol.md).
 *
 * The browser is the third party to this contract, with the agent and the
 * gateway. One binary WebSocket message is one frame: type (u8) | seq (u32,
 * big-endian) | body. `rsp.test.ts` holds this file to the shared vectors in
 * `rsp1-vectors.json`, the same bytes the agent and the server test against.
 */

export const MAX_MESSAGE_BYTES = 1 << 20;
export const MAX_TEXT_BYTES = 256;
export const MAX_KEY_CODE_BYTES = 32;
const HEADER = 5;

/** Frame types. 0x01-0x3F travel agent -> viewer, 0x40-0x7F viewer -> agent. */
export const T = {
  HELLO: 0x01,
  FRAME_BEGIN: 0x02,
  TILE: 0x03,
  COPYRECT: 0x04,
  FRAME_END: 0x05,
  CURSOR_SHAPE: 0x06,
  CURSOR_POS: 0x07,
  STATE: 0x08,
  ACK: 0x40,
  POINTER: 0x41,
  WHEEL: 0x42,
  KEY: 0x43,
  TEXT: 0x44,
  MONITOR_SELECT: 0x45,
  QUALITY: 0x46,
  REFRESH: 0x47,
  SAS: 0x48,
  CONTROL: 0x49,
} as const;

export const CODEC = { FILL: 0, JPEG: 1, PNG: 2 } as const;

/** QUALITY levels. */
export type QualityLevel = 0 | 1 | 2 | 3;

export class RspError extends Error {}

export interface Frame {
  type: number;
  seq: number;
  body: Uint8Array;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Monitor {
  id: number;
  x: number;
  y: number;
  width: number;
  height: number;
  dpi: number;
  primary: boolean;
}

export interface Hello {
  version: number;
  capabilities: string[];
  monitors: Monitor[];
  codecs: string[];
  /** Additive: the monitor following frames describe. Absent means the primary. */
  selected?: number;
}

export interface StateBody {
  state: string;
  reason?: string;
  timeout_s?: number;
}

export function agentToViewer(type: number): boolean {
  return type >= 0x01 && type <= 0x3f;
}

/** An encoded frame, backed by a plain ArrayBuffer so it can be sent or wrapped in a Blob. */
export type Bytes = Uint8Array<ArrayBuffer>;

export function encode(type: number, seq: number, body: Uint8Array = new Uint8Array()): Bytes {
  const out = new Uint8Array(HEADER + body.length);
  out[0] = type;
  new DataView(out.buffer).setUint32(1, seq >>> 0);
  out.set(body, HEADER);
  return out;
}

export function decode(input: ArrayBuffer | Uint8Array): Frame {
  const b = input instanceof Uint8Array ? input : new Uint8Array(input);
  if (b.length < HEADER) throw new RspError(`short frame (${b.length} bytes)`);
  if (b.length > MAX_MESSAGE_BYTES) throw new RspError("frame over 1 MiB");
  const view = new DataView(b.buffer, b.byteOffset, b.byteLength);
  return { type: b[0] ?? 0, seq: view.getUint32(1), body: b.subarray(HEADER) };
}

function view(f: Frame): DataView {
  return new DataView(f.body.buffer, f.body.byteOffset, f.body.byteLength);
}

function need(f: Frame, type: number, bytes: number, name: string): DataView {
  if (f.type !== type || f.body.length < bytes) throw new RspError(`malformed ${name}`);
  return view(f);
}

/** HELLO, STATE and AUTH bodies are JSON objects; unknown members are kept. */
export function jsonBody<V>(f: Frame): V {
  const text = new TextDecoder().decode(f.body);
  if (!text.startsWith("{")) throw new RspError("body is not a JSON object");
  return JSON.parse(text) as V;
}

export function decodeTile(f: Frame): { rect: Rect; codec: number; data: Uint8Array } {
  const v = need(f, T.TILE, 9, "TILE");
  return {
    rect: { x: v.getUint16(0), y: v.getUint16(2), w: v.getUint16(4), h: v.getUint16(6) },
    codec: v.getUint8(8),
    data: f.body.subarray(9),
  };
}

export function decodeCopyRect(f: Frame): { srcX: number; srcY: number; dst: Rect } {
  const v = need(f, T.COPYRECT, 12, "COPYRECT");
  return {
    srcX: v.getUint16(0),
    srcY: v.getUint16(2),
    dst: { x: v.getUint16(4), y: v.getUint16(6), w: v.getUint16(8), h: v.getUint16(10) },
  };
}

export function decodeCursorPos(f: Frame): { x: number; y: number; visible: boolean } {
  const v = need(f, T.CURSOR_POS, 5, "CURSOR_POS");
  return { x: v.getUint16(0), y: v.getUint16(2), visible: v.getUint8(4) === 1 };
}

export function decodeCursorShape(f: Frame): {
  hotX: number;
  hotY: number;
  w: number;
  h: number;
  codec: number;
  data: Uint8Array;
} {
  const v = need(f, T.CURSOR_SHAPE, 9, "CURSOR_SHAPE");
  return {
    hotX: v.getUint16(0),
    hotY: v.getUint16(2),
    w: v.getUint16(4),
    h: v.getUint16(6),
    codec: v.getUint8(8),
    data: f.body.subarray(9),
  };
}

function withBody(type: number, seq: number, size: number, fill: (v: DataView) => void): Bytes {
  const body = new Uint8Array(size);
  fill(new DataView(body.buffer));
  return encode(type, seq, body);
}

export function encodeAck(seq: number, frameEndSeq: number): Bytes {
  return withBody(T.ACK, seq, 4, (v) => v.setUint32(0, frameEndSeq >>> 0));
}

export function encodePointer(seq: number, x: number, y: number, buttons: number): Bytes {
  return withBody(T.POINTER, seq, 5, (v) => {
    v.setUint16(0, x);
    v.setUint16(2, y);
    v.setUint8(4, buttons & 0x1f);
  });
}

/** WHEEL deltas in 1/120ths of a notch; positive dy scrolls down (DOM sense). */
export function encodeWheel(seq: number, dx: number, dy: number): Bytes {
  return withBody(T.WHEEL, seq, 4, (v) => {
    v.setInt16(0, dx);
    v.setInt16(2, dy);
  });
}

export function encodeKey(seq: number, down: boolean, code: string): Bytes {
  const bytes = new TextEncoder().encode(code);
  if (bytes.length === 0 || bytes.length > MAX_KEY_CODE_BYTES) throw new RspError("bad key code");
  const body = new Uint8Array(1 + bytes.length);
  body[0] = down ? 1 : 0;
  body.set(bytes, 1);
  return encode(T.KEY, seq, body);
}

export function encodeText(seq: number, text: string): Bytes {
  const bytes = new TextEncoder().encode(text);
  if (bytes.length > MAX_TEXT_BYTES) throw new RspError("TEXT over 256 bytes");
  return encode(T.TEXT, seq, bytes);
}

export function encodeControl(seq: number, control: boolean): Bytes {
  return encode(T.CONTROL, seq, Uint8Array.of(control ? 1 : 0));
}

export function encodeMonitorSelect(seq: number, id: number): Bytes {
  return encode(T.MONITOR_SELECT, seq, Uint8Array.of(id));
}

export function encodeQuality(seq: number, level: QualityLevel): Bytes {
  return encode(T.QUALITY, seq, Uint8Array.of(level));
}

export function encodeRefresh(seq: number): Bytes {
  return encode(T.REFRESH, seq);
}

// ------------------------------------------------------------------------- //
// Recordings: "ITMSRSP1" | repeat(t_ms u32 | len u32 | frame)
// ------------------------------------------------------------------------- //

const MAGIC = "ITMSRSP1";

export function isRspRecording(bytes: Uint8Array): boolean {
  return bytes.length >= 8 && new TextDecoder().decode(bytes.subarray(0, 8)) === MAGIC;
}

export interface RecordedFrame {
  tMs: number;
  frame: Uint8Array;
}

/** Every record in a recording; a truncated tail is dropped, not thrown. */
export function readRecording(bytes: Uint8Array): RecordedFrame[] {
  if (!isRspRecording(bytes)) throw new RspError("not an rsp/1 recording");
  const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const out: RecordedFrame[] = [];
  let at = 8;
  while (at + 8 <= bytes.length) {
    const tMs = v.getUint32(at);
    const len = v.getUint32(at + 4);
    if (at + 8 + len > bytes.length) break;
    out.push({ tMs, frame: bytes.subarray(at + 8, at + 8 + len) });
    at += 8 + len;
  }
  return out;
}
