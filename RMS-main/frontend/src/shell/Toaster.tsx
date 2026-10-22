/**
 * The toast stack -- TRANSCRIBED from the prototype's `#toasts` markup and
 * its `toast()` helper in `docs/design/itms-prototype.html`.
 *
 * The prototype's `toast(title, copy)` appends one `<div class="toast">` per
 * call, fades it (`style.opacity='0'`) at 3600ms and removes it at 3950ms.
 * There is no CSS `transition` on `.toast` for `opacity`, so that fade never
 * actually animates in the browser -- the node just becomes invisible for
 * 350ms before it is removed. This component keeps the same total lifetime
 * (3950ms) as one timer rather than reproducing an intermediate state that
 * has no visible effect in the source either.
 *
 * TONES THE DESIGN DOES NOT HAVE. Every one of the prototype's 15 `toast()`
 * call sites uses the same markup: a gold check icon (`.toast-icon`'s literal
 * colour, `#e1c36d` on `rgba(209,154,24,.18)`) for what is, in every case, a
 * success confirmation. The design never renders an error or informational
 * toast, so there is nothing to transcribe for those two tones -- this task
 * requires them regardless (see the task's REQUIREMENTS). Rather than invent
 * new colours, `error` and `info` reuse tokens the design's own `:root`
 * already assigns to those meanings elsewhere in the same file (`.notice`'s
 * base state is the purple "info" tone at `--primary`/`--primary-soft`; `.red`
 * is the design's established danger modifier, used on both `.notice.red`
 * and `.mini-icon.red`). `success` needs no modifier class: the unmodified
 * `.toast-icon` rule already **is** the design's gold success look, verbatim.
 * See `overlays.css` for the two added modifier rules, kept in a clearly
 * separate, commented block from the transcribed rules above them.
 *
 * CONTROLLED, like `Drawer` and `CommandPalette`: this component owns no
 * queue of its own. The caller supplies `toasts` and `onDismiss`; a toast
 * leaves the stack when its timer fires or its dismiss button is pressed,
 * either way by the caller removing it from the array it passed in.
 *
 * ACCESSIBILITY THE PROTOTYPE DOES NOT HAVE. The source markup has no ARIA
 * role at all -- a toast is just a `<div>` an assistive technology has no
 * reason to announce. This transcribes the design's APPEARANCE (class names,
 * structure, the icon/title/copy layout), not its accessibility gap: each
 * toast carries `role="status"` so it is announced without stealing focus,
 * plus a visible, focusable dismiss button the design's mock has no need of
 * because nothing in a static prototype ever needs dismissing early.
 */

import { useEffect } from "react";

import { useT } from "../i18n/context";
import { Icon } from "./icons";
import { KIND_ICON } from "./viz";
import "./overlays.css";

export type ToastTone = "success" | "error" | "info";

export interface ToastItem {
  id: string;
  title: string;
  /** `copy` in the prototype's `toast(title, copy)`. Optional there too. */
  copy?: string;
  /** Defaults to `success` -- the only tone the design itself ever renders. */
  tone?: ToastTone;
}

export interface ToasterProps {
  toasts: readonly ToastItem[];
  onDismiss: (id: string) => void;
  /** Total lifetime before auto-dismissal. Defaults to the prototype's own
   * `toast()` timing (3950ms from mount to removal). */
  durationMs?: number;
}

/* The severity glyphs the whole console uses (`KIND_ICON` in viz.tsx: ok
 * circle-check, crit circle-x, info info-circle), so a toast's tone reads the
 * same as a pill's. Colour is never the only carrier. */
const TONE_ICON: Record<ToastTone, string> = {
  success: KIND_ICON.ok,
  error: KIND_ICON.crit,
  info: KIND_ICON.info,
};

/** `.toast-icon` alone (no modifier class) is the design's literal, gold
 * success rule -- so success gets no modifier, matching the transcription. */
const TONE_CLASS: Record<ToastTone, string> = {
  success: "",
  error: "red",
  info: "info",
};

function Toast({ item, onDismiss, durationMs }: { item: ToastItem; onDismiss: (id: string) => void; durationMs: number }) {
  useEffect(() => {
    const timer = window.setTimeout(() => onDismiss(item.id), durationMs);
    return () => window.clearTimeout(timer);
    // Deliberately keyed on the item's identity, not on `onDismiss` (an
    // inline caller callback would otherwise re-arm the timer every render).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.id, durationMs]);

  const t = useT();
  const tone = item.tone ?? "success";

  return (
    <div className="toast" role="status">
      <span className={`toast-icon${TONE_CLASS[tone] ? ` ${TONE_CLASS[tone]}` : ""}`}>
        <Icon name={TONE_ICON[tone]} size={14} />
      </span>
      <span>
        <div className="toast-title">{item.title}</div>
        {item.copy ? <div className="toast-copy">{item.copy}</div> : null}
      </span>
      <button className="toast-dismiss" onClick={() => onDismiss(item.id)} aria-label={t("shell.toaster.dismiss")}>
        <Icon name="close" size={12} />
      </button>
    </div>
  );
}

export function Toaster({ toasts, onDismiss, durationMs = 3950 }: ToasterProps) {
  return (
    <div className="toast-stack">
      {toasts.map((item) => (
        <Toast key={item.id} item={item} onDismiss={onDismiss} durationMs={durationMs} />
      ))}
    </div>
  );
}
