import { describe, expect, it } from "vitest";
import { T, decode, encode } from "./rsp";
import { Assembler, INITIAL_STATUS, describeClose, nextStatus, type ScreenStatus, type StatusEvent } from "./screenModel";

function tile(x: number, codec: number, data: number[]): Uint8Array {
  return encode(T.TILE, 1, Uint8Array.of(0, x, 0, 0, 0, 64, 0, 64, codec, ...data));
}

describe("Assembler", () => {
  it("lands a frame's tiles and copies together at FRAME_END", () => {
    const a = new Assembler();
    expect(a.push(decode(encode(T.FRAME_BEGIN, 1)))).toBeNull();
    expect(a.push(decode(tile(0, 0, [1, 2, 3])))).toBeNull();
    expect(a.push(decode(tile(64, 1, [0xff, 0xd8])))).toBeNull();
    expect(a.push(decode(encode(T.COPYRECT, 4, Uint8Array.of(0, 0, 0, 0, 0, 10, 0, 10, 0, 5, 0, 5))))).toBeNull();
    const done = a.push(decode(encode(T.FRAME_END, 5)));
    expect(done).toEqual({
      kind: "frame",
      seq: 5,
      ops: [
        { kind: "fill", rect: { x: 0, y: 0, w: 64, h: 64 }, rgb: [1, 2, 3] },
        { kind: "jpeg", rect: { x: 64, y: 0, w: 64, h: 64 }, data: Uint8Array.of(0xff, 0xd8) },
        { kind: "copy", srcX: 0, srcY: 0, dst: { x: 10, y: 10, w: 5, h: 5 } },
      ],
    });
  });

  it("ignores tiles outside a frame and types it does not know", () => {
    const a = new Assembler();
    expect(a.push(decode(tile(0, 0, [1, 2, 3])))).toBeNull();
    expect(a.push(decode(encode(0x3e, 1, Uint8Array.of(1))))).toBeNull();
  });
});

function run(events: StatusEvent[], from: ScreenStatus = INITIAL_STATUS): ScreenStatus {
  return events.reduce(nextStatus, from);
}

describe("nextStatus", () => {
  it("walks connect, consent and live", () => {
    let s = run([{ kind: "open" }]);
    expect(s.phase).toBe("waiting");
    s = nextStatus(s, { kind: "state", state: { state: "consent_pending", timeout_s: 30 }, now: 1000 });
    expect([s.phase, s.consentDeadline]).toEqual(["consent", 31_000]);
    s = run([{ kind: "state", state: { state: "consent_granted" }, now: 2000 }, { kind: "hello" }], s);
    expect([s.phase, s.consentDeadline]).toEqual(["live", null]);
  });

  it("keeps the first reason it was given for the end", () => {
    const s = run([
      { kind: "open" },
      { kind: "hello" },
      { kind: "state", state: { state: "ended", reason: "ended_by_user" }, now: 0 },
      { kind: "closed", code: 4000, reason: "completed" },
    ]);
    expect([s.phase, s.message]).toEqual(["ended", "The person at the device ended the session."]);
  });

  it("marks control taken back and a hidden desktop, and clears the latter on the next frame", () => {
    let s = run([
      { kind: "hello" },
      { kind: "state", state: { state: "control_revoked_by_user" }, now: 0 },
      { kind: "state", state: { state: "input_desktop_unavailable" }, now: 0 },
    ]);
    expect([s.controlTaken, s.desktopHidden]).toEqual([true, true]);
    s = nextStatus(s, { kind: "frame" });
    expect([s.controlTaken, s.desktopHidden]).toEqual([true, false]);
  });

  it("treats an unexplained close as a failure, and an unknown end reason as an error", () => {
    expect(run([{ kind: "open" }, { kind: "closed", code: 1006, reason: "" }]).phase).toBe("failed");
    expect(run([{ kind: "state", state: { state: "ended", reason: "later" }, now: 0 }]).message).toMatch(/error/);
    expect(describeClose(4403, "")).toMatch(/not allowed/);
  });
});
