/**
 * The detail drawer -- TRANSCRIBED from the prototype's `#drawer` markup and
 * its `openDrawer()` / `closeDrawer()` helpers in
 * `docs/design/itms-prototype.html`.
 *
 * The prototype drives one drawer element through direct DOM writes
 * (`drawerTitle.textContent = title`, `drawerBody.innerHTML = body`) toggled
 * open with a class. This is that same drawer as a controlled component:
 * `open` replaces the `.open` class toggle, `onClose` replaces
 * `closeDrawer()`, and `title` / `subtitle` / `children` / `footer` replace
 * the four DOM slots the prototype writes into by id.
 *
 * ACCESSIBILITY THE PROTOTYPE DOES NOT HAVE. The source markup is a bare
 * `<aside class="drawer">` with a `<div class="drawer-title">` -- no
 * `role="dialog"`, no `aria-modal`, no heading element, and Escape is wired
 * only through a single document-level keydown switch that also happens to
 * close the modal and the palette. This component transcribes the design's
 * APPEARANCE (class names, structure, the close-icon button), not its
 * accessibility gaps: `role="dialog"` + `aria-modal="true"` +
 * `aria-labelledby` pointing at a real `<h2>`, and Escape is scoped to this
 * component rather than borrowed from a shared global handler. See the UI
 * port spec §4 and the task note that established this rule for the shell.
 *
 * MOUNTING, NOT CLASS-TOGGLING. The prototype keeps `#drawer` permanently in
 * the DOM and toggles a `.open` class so the `transform` transition can slide
 * it in. This component instead renders nothing when `open` is false. A
 * closed-but-mounted dialog with a focusable close button inside it is an
 * `aria-hidden`-on-focusable-content violation the prototype's static HTML
 * never has to answer for and a controlled React component should not
 * introduce; the trade is the slide-*out* transition, which no animation
 * library is available to add without a new dependency. The slide-*in*
 * transition (`.drawer` to `.drawer.open`) still plays, because the class is
 * applied in the same render that mounts the node.
 *
 * The `.overlay` div is rendered here too, and only while open -- the
 * prototype has exactly one overlay element shared by drawer/modal/palette,
 * but nothing outside this task builds Modal yet, so an overlay that only
 * Drawer or CommandPalette ever shows is correct for what exists today.
 */

import { useEffect, useRef, type ReactNode } from "react";

import { useDialogSurface } from "../components/ui";
import { useT } from "../i18n/context";
import { Icon } from "./icons";
import "./overlays.css";

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  /** `drawerTitle` in the prototype. Rendered as a real heading, not a div. */
  title: string;
  /** `drawerSubtitle` in the prototype. Absent renders nothing, matching the
   * prototype's `subtitle || ''` -- an empty subtitle line is not shown. */
  subtitle?: string;
  /** `drawerBody` in the prototype (`innerHTML = body`). */
  children: ReactNode;
  /**
   * `drawerFoot` in the prototype. The prototype defaults this to a single
   * `<button class="btn" data-close="drawer">Close</button>` when the caller
   * passes no footer (`footer || defaultCloseButton`). That default is not
   * reproduced here: a caller-supplied close action risks acting on stale
   * props, so an absent footer renders no `.drawer-foot` at all rather than
   * guessing at one.
   */
  footer?: ReactNode;
}

export function Drawer({ open, onClose, title, subtitle, children, footer }: DrawerProps) {
  const t = useT();
  const panel = useRef<HTMLElement>(null);
  // Initial focus, the focus trap, scroll lock and focus return are the
  // shared modal's. Escape is handled below instead, because a drawer hosts
  // confirmations whose own Escape it must not also answer.
  useDialogSurface({ open, surface: panel, onClose, dismissible: false });

  // Escape closes the drawer, unless the key was pressed inside another
  // modal surface stacked above it -- that surface closes itself.
  useEffect(() => {
    if (!open) return;
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      const owner = event.target instanceof Element ? event.target.closest('[aria-modal="true"]') : null;
      if (owner && owner !== panel.current) return;
      onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <>
      <div className="overlay open" onClick={onClose} />
      <aside
        className="drawer open"
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
        ref={panel}
        tabIndex={-1}
      >
        <div className="drawer-head">
          <div>
            <h2 className="drawer-title" id="drawer-title">
              {title}
            </h2>
            {subtitle ? <div className="panel-subtitle">{subtitle}</div> : null}
          </div>
          <button className="close-btn" onClick={onClose} aria-label={t("common.actions.close")}>
            <Icon name="close" size={16} />
          </button>
        </div>
        <div className="drawer-body">{children}</div>
        {footer ? <div className="drawer-foot">{footer}</div> : null}
      </aside>
    </>
  );
}
