/**
 * Asking before doing something that cannot be undone.
 *
 * WHY THIS EXISTS (UX remediation cycle 8). The audits found deletion firing
 * on a single click with no confirmation and no undo, styled exactly like the
 * safe actions beside it; a one-click irreversible "Resolve" with no way to
 * reopen; an enrollment credential minted by a button with no form; and -- as
 * a BLOCKER -- a security control mutated and persisted by one dropdown change
 * with no save, no confirmation and no feedback at all.
 *
 * WHAT A CONFIRMATION HAS TO DO, because it is easy to build one that only
 * adds a click and trains people to click through it: say WHAT will happen, to
 * WHICH thing, and whether it can be undone. "Are you sure?" answers none of
 * those. The subject is the load-bearing part -- it is what lets somebody
 * notice they have the wrong row selected, which is the mistake this is for.
 */

import { useEffect, useRef, useState } from "react";

import { useT } from "../i18n/context";
import { Trans } from "../i18n/Trans";
import { Button, inputClass, useDialogSurface } from "./ui";
/* THIS COMPONENT PAINTS `.overlay` AND `.modal`, SO IT HAS TO ASK FOR THEM.
 *
 * Until now it did not, and the rules reached it only because `overlays.css`
 * is imported by `CommandPalette`, `Drawer` and `Toaster` -- all of which
 * `AppShell` happens to mount. So every confirmation in the product was
 * styled by a side effect of three unrelated components being on screen.
 *
 * Rendered outside that shell it drew as an unstyled static block below the
 * fold: not a dialog, not on top of anything, and dismissible only by finding
 * it. That is the exact defect this cycle already found once, when `.modal`
 * had no rule at all and the confirm dialog had never rendered as a dialog.
 * It is shipped-correct today and one import away from shipped-broken, and
 * nothing tested the difference. A component owns its own stylesheet. */
import "../shell/overlays.css";

export interface ConfirmDialogProps {
  open: boolean;
  /** The action, as a sentence: "Delete health profile". */
  title: string;
  /** What follows from it. Consequences, not reassurance. */
  body: string;
  /** WHICH thing — the name the user recognises, not an id. */
  subject: string;
  confirmLabel: string;
  /** Irreversible, or destroys data. Drives the styling and nothing else. */
  destructive?: boolean;
  /**
   * Require the subject to be typed before confirming.
   *
   * For the small set where a misclick is unrecoverable AND the blast radius
   * is wide -- erasing a device, deleting an organisation. Everywhere else it
   * is friction that buys nothing: a person who will click through a
   * confirmation will also copy the word in front of them.
   */
  requireTyping?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  body,
  subject,
  confirmLabel,
  destructive = false,
  requireTyping = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const t = useT();
  const [typed, setTyped] = useState("");
  const panel = useRef<HTMLElement>(null);
  // FOCUS STARTS ON CANCEL. A dialog that opens with the destructive control
  // focused turns a stray Enter -- often the very keypress that opened it --
  // into the deletion.
  const cancel = useRef<HTMLButtonElement>(null);

  // Escape, the focus trap, scroll lock and giving focus back are the shared
  // modal contract; see `useDialogSurface`. This dialog had Escape and nothing
  // else: the browser pass tabbed four times out of an open delete
  // confirmation and landed on the rail's "Dashboard" link, with
  // `aria-modal="true"` on the section claiming that was impossible, and
  // Escape then dropped focus on <body> rather than back on the control that
  // opened it.
  useDialogSurface({ open, surface: panel, onClose: onCancel, initialFocus: cancel });

  useEffect(() => {
    if (open) setTyped("");
  }, [open]);

  if (!open) return null;

  const blocked = requireTyping && typed.trim() !== subject.trim();

  return (
    <>
      <div className="overlay open" onClick={onCancel} />
      <section
        className="modal open confirm"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        ref={panel}
        tabIndex={-1}
      >
        <div className="modal-head">
          <h2 className="modal-title">{title}</h2>
        </div>
        <div className="modal-body">
          <p className="confirm-subject">{subject}</p>
          <p className="confirm-body">{body}</p>

          {requireTyping ? (
            <label className="confirm-typing">
              <span>
                <Trans k="components.confirm.typeToConfirm" vars={{ subject }} components={{ strong: <strong /> }} />
              </span>
              <input
                className={inputClass}
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoComplete="off"
              />
            </label>
          ) : null}

          <div className="confirm-actions">
            <Button ref={cancel} variant="outline" onClick={onCancel}>
              {t("common.actions.cancel")}
            </Button>
            <Button
              className={destructive ? "danger" : undefined}
              disabled={blocked}
              onClick={onConfirm}
            >
              {confirmLabel}
            </Button>
          </div>
        </div>
      </section>
    </>
  );
}
