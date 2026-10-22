import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  CODEC,
  RspError,
  T,
  decode,
  decodeCopyRect,
  decodeCursorPos,
  decodeCursorShape,
  decodeTile,
  encode,
  encodeAck,
  encodeControl,
  encodeKey,
  encodeMonitorSelect,
  encodePointer,
  encodeQuality,
  encodeRefresh,
  encodeText,
  encodeWheel,
  isRspRecording,
  jsonBody,
  readRecording,
  type QualityLevel,
} from "./rsp";

// The same vectors the agent (internal/rsp) and the server (rsp.py) read.
interface Vector {
  name: string;
  type: number;
  seq: number;
  hex: string;
  json?: Record<string, unknown>;
  fields?: Record<string, number | string>;
}

const vectors: Vector[] = (
  JSON.parse(
    readFileSync(
      resolve(import.meta.dirname, "../../../../docs/kernel/wire-protocol/rsp1-vectors.json"),
      "utf8",
    ),
  ) as { frames: Vector[] }
).frames;
const byName = new Map(vectors.map((v) => [v.name, v]));

function vec(name: string): Vector {
  const v = byName.get(name);
  if (!v) throw new Error(`no vector ${name}`);
  return v;
}

function bytes(hex: string): Uint8Array {
  return Uint8Array.from(hex.match(/../g) ?? [], (h) => parseInt(h, 16));
}

function hex(b: Uint8Array): string {
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

function num(v: Vector, k: string): number {
  return Number(v.fields?.[k]);
}

describe("rsp/1 against the shared vectors", () => {
  it("decodes every vector's header and JSON body", () => {
    expect(vectors.length).toBeGreaterThanOrEqual(10);
    for (const v of vectors) {
      const f = decode(bytes(v.hex));
      expect([v.name, f.type, f.seq]).toEqual([v.name, v.type, v.seq]);
      if (v.json) expect(jsonBody(f)).toEqual(v.json);
    }
  });

  it("encodes the viewer's frames byte for byte", () => {
    const check = (name: string, got: Uint8Array) => expect([name, hex(got)]).toEqual([name, vec(name).hex]);
    const ack = vec("ack");
    check("ack", encodeAck(ack.seq, num(ack, "frame_seq")));
    const p = vec("pointer");
    check("pointer", encodePointer(p.seq, num(p, "x"), num(p, "y"), num(p, "buttons")));
    const w = vec("wheel");
    check("wheel", encodeWheel(w.seq, num(w, "dx"), num(w, "dy")));
    const k = vec("key");
    check("key", encodeKey(k.seq, num(k, "down") === 1, String(k.fields?.code)));
    const t = vec("text");
    check("text", encodeText(t.seq, String(t.fields?.text)));
    check("refresh", encodeRefresh(vec("refresh").seq));
    const c = vec("control_view_only");
    check("control_view_only", encodeControl(c.seq, num(c, "mode") === 1));
    const m = vec("monitor_select");
    check("monitor_select", encodeMonitorSelect(m.seq, num(m, "monitor_id")));
    const q = vec("quality");
    check("quality", encodeQuality(q.seq, num(q, "level") as QualityLevel));
    check("frame_end", encode(T.FRAME_END, vec("frame_end").seq));
  });

  it("decodes the agent's binary frames", () => {
    const tile = decodeTile(decode(bytes(vec("tile_fill").hex)));
    expect(tile.rect).toEqual({ x: 16, y: 32, w: 64, h: 48 });
    expect(tile.codec).toBe(CODEC.FILL);
    expect(hex(tile.data)).toBe("ff8000");
    expect(decodeCopyRect(decode(bytes(vec("copyrect").hex)))).toEqual({
      srcX: 0,
      srcY: 0,
      dst: { x: 10, y: 20, w: 100, h: 50 },
    });
    expect(decodeCursorPos(decode(bytes(vec("cursor_pos").hex)))).toEqual({ x: 640, y: 360, visible: true });
    const shape = decodeCursorShape(decode(bytes(vec("cursor_shape").hex)));
    expect([shape.hotX, shape.hotY, shape.w, shape.h, shape.codec]).toEqual([1, 2, 16, 16, CODEC.PNG]);
  });
});

describe("rsp/1 limits", () => {
  it("refuses short and oversized frames and bad bodies", () => {
    expect(() => decode(new Uint8Array(4))).toThrow(RspError);
    expect(() => decode(new Uint8Array((1 << 20) + 1))).toThrow(RspError);
    expect(() => encodeText(1, "a".repeat(257))).toThrow(RspError);
    expect(() => encodeKey(1, true, "")).toThrow(RspError);
    expect(() => decodeTile(decode(encode(T.TILE, 1, new Uint8Array(3))))).toThrow(RspError);
    expect(() => jsonBody(decode(encode(T.STATE, 1, new TextEncoder().encode("[]"))))).toThrow(RspError);
  });

  it("masks pointer buttons to the five defined", () => {
    const f = decode(encodePointer(1, 1, 1, 0xff));
    expect(f.body[4]).toBe(0x1f);
  });
});

describe("recordings", () => {
  it("reads records and drops a truncated tail", () => {
    const frame = encode(T.FRAME_END, 7);
    const rec = new Uint8Array(8 + 8 + frame.length + 6);
    rec.set(new TextEncoder().encode("ITMSRSP1"));
    const v = new DataView(rec.buffer);
    v.setUint32(8, 1500);
    v.setUint32(12, frame.length);
    rec.set(frame, 16);
    v.setUint32(16 + frame.length, 2000); // a record cut short
    expect(isRspRecording(rec)).toBe(true);
    const out = readRecording(rec);
    expect(out).toHaveLength(1);
    expect(out[0]?.tMs).toBe(1500);
    expect(decode(out[0]?.frame ?? new Uint8Array()).seq).toBe(7);
    expect(isRspRecording(new TextEncoder().encode("4.size,..."))).toBe(false);
  });
});
