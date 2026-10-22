/**
 * Browser input -> rsp/1 input. Physical key codes are sent, so the device's
 * own keyboard layout decides the character (spec §5.2); "Type text" is the
 * way to send the technician's own characters.
 */
import { MAX_TEXT_BYTES } from "./rsp";

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** A point on the displayed canvas, as a pixel on the remote monitor. */
export function toRemote(
  clientX: number,
  clientY: number,
  box: Box,
  monitor: { width: number; height: number },
): { x: number; y: number } {
  const clamp = (v: number, size: number) => Math.min(Math.max(Math.floor(v), 0), Math.max(size - 1, 0));
  if (box.width <= 0 || box.height <= 0) return { x: 0, y: 0 };
  return {
    x: clamp(((clientX - box.left) * monitor.width) / box.width, monitor.width),
    y: clamp(((clientY - box.top) * monitor.height) / box.height, monitor.height),
  };
}

/** DOM MouseEvent.buttons uses rsp/1's bits: 1 left, 2 right, 4 middle, 8 back, 16 forward. */
export function buttonsMask(buttons: number): number {
  return buttons & 0x1f;
}

const DOM_DELTA_LINE = 1;
const DOM_DELTA_PAGE = 2;

/** A WheelEvent delta as 1/120ths of a notch, clamped to i16. */
export function wheelUnits(delta: number, deltaMode: number): number {
  let units: number;
  if (deltaMode === DOM_DELTA_LINE) units = delta * 40; // three lines to a notch
  else if (deltaMode === DOM_DELTA_PAGE) units = delta * 360;
  else units = delta * 1.2; // Chromium reports 100 px per notch
  return Math.max(-32768, Math.min(32767, Math.round(units)));
}

/** Whether a KeyboardEvent.code can travel as a KEY frame. */
export function sendableCode(code: string): boolean {
  return code !== "" && code !== "Unidentified" && new TextEncoder().encode(code).length <= 32;
}

/** Text split into TEXT-sized pieces, never inside a character. */
export function textChunks(text: string): string[] {
  const out: string[] = [];
  const enc = new TextEncoder();
  let chunk = "";
  let size = 0;
  for (const ch of text) {
    const n = enc.encode(ch).length;
    if (size + n > MAX_TEXT_BYTES) {
      out.push(chunk);
      chunk = "";
      size = 0;
    }
    chunk += ch;
    size += n;
  }
  if (chunk) out.push(chunk);
  return out;
}

/** Keys a browser will not deliver, sent as down/up sequences. */
export const SHORTCUTS: Record<string, { label: string; keys: [string, boolean][] }> = {
  win: { label: "Win", keys: [["MetaLeft", true], ["MetaLeft", false]] },
  altTab: {
    label: "Alt+Tab",
    keys: [["AltLeft", true], ["Tab", true], ["Tab", false], ["AltLeft", false]],
  },
  ctrlEsc: {
    label: "Ctrl+Esc",
    keys: [["ControlLeft", true], ["Escape", true], ["Escape", false], ["ControlLeft", false]],
  },
};

/** Tracks held keys so leaving the canvas releases them on the device. */
export class HeldKeys {
  private held = new Set<string>();

  down(code: string): void {
    this.held.add(code);
  }

  up(code: string): boolean {
    return this.held.delete(code);
  }

  /** Every code still held, emptied. */
  releaseAll(): string[] {
    const out = [...this.held];
    this.held.clear();
    return out;
  }
}
