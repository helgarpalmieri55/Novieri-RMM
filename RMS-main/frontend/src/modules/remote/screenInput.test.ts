import { describe, expect, it } from "vitest";
import { HeldKeys, SHORTCUTS, buttonsMask, sendableCode, textChunks, toRemote, wheelUnits } from "./screenInput";

describe("toRemote", () => {
  const box = { left: 100, top: 50, width: 960, height: 540 };
  const monitor = { width: 1920, height: 1080 };

  it("scales from the displayed canvas to the monitor's pixels", () => {
    expect(toRemote(100, 50, box, monitor)).toEqual({ x: 0, y: 0 });
    expect(toRemote(580, 320, box, monitor)).toEqual({ x: 960, y: 540 });
  });

  it("clamps to the monitor", () => {
    expect(toRemote(5000, -20, box, monitor)).toEqual({ x: 1919, y: 0 });
  });

  it("answers the origin for a canvas with no layout", () => {
    expect(toRemote(10, 10, { left: 0, top: 0, width: 0, height: 0 }, monitor)).toEqual({ x: 0, y: 0 });
  });
});

describe("buttons, wheel and keys", () => {
  it("keeps the five DOM buttons rsp/1 defines", () => {
    expect(buttonsMask(1 | 2 | 32)).toBe(3);
  });

  it("turns a notch into 120 units whatever the browser reports", () => {
    expect(wheelUnits(100, 0)).toBe(120); // Chromium, pixels
    expect(wheelUnits(3, 1)).toBe(120); // Firefox, lines
    expect(wheelUnits(-1, 2)).toBe(-360);
    expect(wheelUnits(1e9, 0)).toBe(32767);
  });

  it("sends only real key codes", () => {
    expect(sendableCode("KeyA")).toBe(true);
    expect(sendableCode("")).toBe(false);
    expect(sendableCode("Unidentified")).toBe(false);
    expect(sendableCode("K".repeat(33))).toBe(false);
  });

  it("presses and releases every key of a shortcut", () => {
    for (const s of Object.values(SHORTCUTS)) {
      const downs = s.keys.filter(([, d]) => d).map(([c]) => c);
      const ups = s.keys.filter(([, d]) => !d).map(([c]) => c);
      expect(ups.sort()).toEqual(downs.sort());
    }
  });

  it("remembers held keys until released", () => {
    const held = new HeldKeys();
    held.down("ShiftLeft");
    held.down("KeyA");
    expect(held.up("KeyA")).toBe(true);
    expect(held.up("KeyA")).toBe(false);
    expect(held.releaseAll()).toEqual(["ShiftLeft"]);
    expect(held.releaseAll()).toEqual([]);
  });
});

describe("textChunks", () => {
  it("splits at 256 bytes without cutting a character", () => {
    const chunks = textChunks("é".repeat(200)); // 400 bytes
    expect(chunks.map((c) => new TextEncoder().encode(c).length)).toEqual([256, 144]);
    expect(chunks.join("")).toBe("é".repeat(200));
    expect(textChunks("")).toEqual([]);
  });
});
