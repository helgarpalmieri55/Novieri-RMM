/**
 * What the viewer does with agent frames, without a canvas or a socket: the
 * frame assembler (a frame lands atomically at FRAME_END) and the session
 * status in plain words. `ScreenViewer` and the recording player share it.
 */
import { translate, type MessageKey } from "../../i18n/core";
import {
  CODEC,
  T,
  decodeCopyRect,
  decodeCursorPos,
  decodeCursorShape,
  decodeTile,
  jsonBody,
  type Frame,
  type Hello,
  type Rect,
  type StateBody,
} from "./rsp";

export type Op =
  | { kind: "fill"; rect: Rect; rgb: [number, number, number] }
  | { kind: "jpeg"; rect: Rect; data: Uint8Array }
  | { kind: "copy"; srcX: number; srcY: number; dst: Rect };

export type ScreenEvent =
  | { kind: "hello"; hello: Hello }
  | { kind: "state"; state: StateBody }
  | { kind: "frame"; seq: number; ops: Op[] }
  | { kind: "cursor"; x: number; y: number; visible: boolean }
  | { kind: "cursor-shape"; hotX: number; hotY: number; png: Uint8Array };

/** Collects TILE and COPYRECT between FRAME_BEGIN and FRAME_END. */
export class Assembler {
  private pending: Op[] | null = null;

  /** One agent frame in; at most one event out. Unknown types are ignored. */
  push(f: Frame): ScreenEvent | null {
    switch (f.type) {
      case T.HELLO:
        return { kind: "hello", hello: jsonBody<Hello>(f) };
      case T.STATE:
        return { kind: "state", state: jsonBody<StateBody>(f) };
      case T.FRAME_BEGIN:
        this.pending = [];
        return null;
      case T.TILE: {
        if (!this.pending) return null;
        const t = decodeTile(f);
        if (t.codec === CODEC.FILL && t.data.length >= 3) {
          this.pending.push({ kind: "fill", rect: t.rect, rgb: [t.data[0] ?? 0, t.data[1] ?? 0, t.data[2] ?? 0] });
        } else if (t.codec === CODEC.JPEG) {
          // A copy: the frame buffer is not ours to keep.
          this.pending.push({ kind: "jpeg", rect: t.rect, data: t.data.slice() });
        }
        return null;
      }
      case T.COPYRECT: {
        if (!this.pending) return null;
        const c = decodeCopyRect(f);
        this.pending.push({ kind: "copy", srcX: c.srcX, srcY: c.srcY, dst: c.dst });
        return null;
      }
      case T.FRAME_END: {
        const ops = this.pending ?? [];
        this.pending = null;
        return { kind: "frame", seq: f.seq, ops };
      }
      case T.CURSOR_POS:
        return { kind: "cursor", ...decodeCursorPos(f) };
      case T.CURSOR_SHAPE: {
        const s = decodeCursorShape(f);
        return s.codec === CODEC.PNG ? { kind: "cursor-shape", hotX: s.hotX, hotY: s.hotY, png: s.data.slice() } : null;
      }
      default:
        return null;
    }
  }
}

// ------------------------------------------------------------------------- //
// Status, in the words the technician reads
// ------------------------------------------------------------------------- //

export type Phase = "connecting" | "waiting" | "consent" | "live" | "ended" | "failed";

export interface ScreenStatus {
  phase: Phase;
  /** Epoch ms when the consent prompt times out, while phase is "consent". */
  consentDeadline: number | null;
  /** The endpoint user is on a screen this helper cannot see (UAC, lock). */
  desktopHidden: boolean;
  /** The endpoint user pressed Stop control; final for the session. */
  controlTaken: boolean;
  /** Why the session ended, or why it could not start, as a sentence worded
   *  when the status changed. A snapshot for readers that want a string; the
   *  page words `messageKey` at render, so a language switch re-words it. */
  message: string | null;
  /** The catalogue entry for that sentence. */
  messageKey: MessageKey | null;
}

export const INITIAL_STATUS: ScreenStatus = {
  phase: "connecting",
  consentDeadline: null,
  desktopHidden: false,
  controlTaken: false,
  message: null,
  messageKey: null,
};

const END_REASONS: Record<string, MessageKey> = {
  completed: "remote.end.completed",
  ended_by_user: "remote.end.ended_by_user",
  denied_consent: "remote.end.denied_consent",
  consent_timeout: "remote.end.consent_timeout",
  no_user_session: "remote.end.no_user_session",
  capture_unavailable: "remote.end.capture_unavailable",
  permission_missing: "remote.end.permission_missing",
  unsupported_display: "remote.end.unsupported_display",
  expired: "remote.end.expired",
  relay_refused: "remote.end.relay_refused",
  error: "remote.end.error",
};

const UNKNOWN_END: MessageKey = "remote.end.error";

/** The catalogue entry that words an end reason. */
export function endKey(reason: string | null | undefined): MessageKey {
  return END_REASONS[reason ?? ""] ?? UNKNOWN_END;
}

export function describeEnd(reason: string | null | undefined): string {
  return translate(endKey(reason));
}

/** The catalogue entry for a close code from the api's /screen route and the relay behind it. */
export function closeKey(code: number, reason: string): MessageKey {
  switch (code) {
    case 4000:
      return endKey(reason || "completed");
    case 4401:
    case 4403:
      return "remote.close.notAllowed";
    case 4404:
      return "remote.close.notFound";
    case 4408:
      return "remote.close.deviceTimeout";
    case 4409:
      return "remote.close.notLive";
    default:
      return "remote.close.lost";
  }
}

/** Close codes from the api's /screen route and the relay behind it. */
export function describeClose(code: number, reason: string): string {
  return translate(closeKey(code, reason));
}

function ended(s: ScreenStatus, reason: string | null | undefined): ScreenStatus {
  return { ...s, phase: "ended", consentDeadline: null, message: describeEnd(reason), messageKey: endKey(reason) };
}

export type StatusEvent =
  | { kind: "open" }
  | { kind: "state"; state: StateBody; now: number }
  | { kind: "hello" }
  | { kind: "frame" }
  | { kind: "closed"; code: number; reason: string }
  | { kind: "failed"; messageKey: MessageKey };

export function nextStatus(s: ScreenStatus, e: StatusEvent): ScreenStatus {
  if (s.phase === "ended" || s.phase === "failed") return s;
  switch (e.kind) {
    case "open":
      return { ...s, phase: "waiting" };
    case "hello":
      return { ...s, phase: "live", consentDeadline: null, desktopHidden: false };
    case "frame":
      return s.desktopHidden ? { ...s, desktopHidden: false } : s;
    case "closed": {
      const messageKey = s.messageKey ?? closeKey(e.code, e.reason);
      const message = s.message ?? translate(messageKey);
      return { ...s, phase: e.code === 4000 || s.messageKey !== null ? "ended" : "failed", message, messageKey };
    }
    case "failed":
      return { ...s, phase: "failed", message: translate(e.messageKey), messageKey: e.messageKey };
    case "state": {
      const st = e.state;
      switch (st.state) {
        case "consent_pending":
          return { ...s, phase: "consent", consentDeadline: e.now + (st.timeout_s ?? 30) * 1000 };
        case "consent_granted":
          return { ...s, phase: "waiting", consentDeadline: null };
        case "consent_denied":
          return ended(s, "denied_consent");
        case "consent_timeout":
          return ended(s, "consent_timeout");
        case "control_revoked_by_user":
          return { ...s, controlTaken: true };
        case "input_desktop_unavailable":
          return { ...s, desktopHidden: true };
        case "ended":
          return ended(s, st.reason);
        default:
          return s;
      }
    }
  }
}
