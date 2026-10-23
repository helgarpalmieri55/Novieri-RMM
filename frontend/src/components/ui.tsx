// The shared primitives, rendering the DESIGN'S markup.
//
// WHY THIS FILE IS THE LEVER. 109 files import from here. Restyling these six
// primitives brings `docs/design/itms-prototype-v3.html` to every one of them
// at once, without editing a single page -- which is why the v3 port (spec §4)
// restyles in place first and migrates pages to the new pieces second. A
// per-page rewrite of the same six components, 109 times, would take weeks
// and drift on the way.
//
// These emit the design's class names (`.btn`, `.pill`, `.panel`,
// `.table-wrap`, `.modal`, `.field`) and nothing else. The styling lives in
// `shell/components.css`, transcribed rule for rule from the design, so there
// is one place a visual decision can be made and it is the design file.
//
// THE TAILWIND UTILITY SOUP IS GONE, INCLUDING EVERY `dark:` VARIANT. The
// design is light-only (`color-scheme: light`, no dark palette anywhere).

import { Fragment, cloneElement, isValidElement, useEffect, useId, useRef, useState } from "react";
import type {
  ButtonHTMLAttributes,
  CSSProperties,
  HTMLAttributes,
  ReactElement,
  ReactNode,
  Ref,
} from "react";
import { useT } from "../i18n/context";
import type { BadgeVariant } from "../modules/jobs/jobState";
import { Icon } from "../shell/icons";
import { KIND_ICON, type Kind } from "../shell/viz";
import "../shell/components.css";

function cx(...parts: Array<string | false | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/** `default` is v3's `.btn.pri` (the console's `.primary`), `outline` its plain `.btn`, `ghost` its borderless one. */
export function Button({
  className,
  variant = "default",
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "outline" | "ghost";
  // React 19 passes `ref` as an ordinary prop, but it has to be DECLARED to be
  // accepted. The confirmation dialog needs it: focus starts on Cancel, so a
  // stray Enter cannot be the deletion.
  ref?: Ref<HTMLButtonElement>;
}) {
  const variants = { default: "primary", outline: "", ghost: "ghost" } as const;
  // `type="button"` BY DEFAULT, AND IT IS NOT A STYLE CHOICE. HTML defaults a
  // <button> inside a form to type="submit". While the product contained six
  // <form> elements in total that was harmless -- a Button was almost never
  // inside one. The cycle-3 form kernel wraps twenty forms in a real <form>,
  // which turns every Cancel button in them into a second submit control: the
  // control whose entire purpose is to ABANDON the form would send it.
  //
  // So submitting is opt-in. `FormActions` passes type="submit" explicitly,
  // and any other control that must submit says so. Two of the fourteen agents
  // adopting the kernel hit this independently and patched their own Cancel
  // buttons; fixing it here is what stops the fifteenth from missing it.
  return <button type={type} className={cx("btn", variants[variant], className)} {...props} />;
}

// The job-state vocabulary, as v3 kinds. A badge is a pill: it carries its
// kind's glyph as well as its wash, because colour is never the only carrier.
const badgeKind: Record<BadgeVariant, Kind> = {
  muted: "mut",
  info: "info",
  success: "ok",
  destructive: "crit",
  warning: "warn",
};

export function Badge({
  variant = "muted",
  className,
  children,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { variant?: BadgeVariant }) {
  const kind = badgeKind[variant];
  return (
    <span className={cx("pill", `p-${kind}`, className)} data-kind={kind} {...props}>
      <Icon name={KIND_ICON[kind]} size={13} />
      {children}
    </span>
  );
}

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cx("panel", className)} {...props} />;
}

/**
 * THE WRAPPER IS A TAB STOP, because it is a scroll container. Below ~600px
 * the table scrolls inside it (SC 1.4.10 reflow), and a scrollable region a
 * keyboard cannot reach is an axe `scrollable-region-focusable` failure --
 * measured on the preview at 390px on a table whose cells held only pills
 * and names. A table with buttons in it happened to pass by accident. The
 * universal `:focus-visible` ring in base.css shows where the stop is.
 */
/**
 * A list, in a card that scrolls sideways rather than crushing its columns --
 * AND SAYS SO WHEN IT DOES.
 *
 * `.table-wrap > table` carries a 560px floor (`components.css`), so at a
 * 390px viewport every five-column table in the console has its last column
 * outside the card. That is the right trade for a data table under SC 1.4.10
 * -- the alternative is columns compressed past legibility -- and the wrapper
 * is a tab stop, so a keyboard user reaches it. A SIGHTED TOUCH USER GOT NO
 * CUE AT ALL: the card simply ended, mid-table, with nothing to say there was
 * more. Three review findings (/ipam/drift's Actions column,
 * /ipam/scan-authorizations' Revoke, /email-intake's Ticket) were that.
 *
 * The hint is measured, not assumed: it appears only while `scrollWidth`
 * exceeds `clientWidth` AND the wrapper is not already scrolled to the end,
 * so a table that fits draws nothing. `aria-hidden` because it is a
 * restatement for the eye of something the keyboard and the screen reader
 * already have.
 *
 * IT SITS UNDER THE TABLE AND VEILS NOTHING. The first spelling was a
 * right-edge gradient over the scroller, and the browser pass measured what
 * that costs: three avatars in the trailing column read 1.28:1, 1.31:1 and
 * 1.53:1, because a white veil over a person's cell is a white veil over the
 * person's initials. A cue that makes the content it points at unreadable is
 * not a cue.
 */
export function Table({ children, label }: { children: ReactNode; label?: string }) {
  const t = useT();
  const wrap = useRef<HTMLDivElement | null>(null);
  const inner = useRef<HTMLTableElement | null>(null);
  const [more, setMore] = useState(false);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const measure = () => {
      // 2px rather than 0: sub-pixel layout leaves a fractional remainder on
      // a table that is exactly as wide as its box, and a permanent hint on a
      // table that does not scroll is worse than none.
      setMore(el.scrollWidth - el.clientWidth - el.scrollLeft > 2);
    };
    measure();
    el.addEventListener("scroll", measure, { passive: true });
    // `ResizeObserver` is undefined in some test environments; the hint is an
    // enhancement, so its absence must not throw.
    //
    // BOTH BOXES ARE OBSERVED, and mounting once rather than per render is why
    // that matters. The WRAPPER changes when the viewport does; the TABLE
    // changes when the rows do -- a filter that narrows a list can change
    // `scrollWidth` while the wrapper's own box never moves, and an observer
    // on the wrapper alone would never hear about it. Re-subscribing on every
    // render (`[children]`) would also have caught it, at the cost of tearing
    // down and rebuilding two listeners on every keystroke in a filter box.
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
    observer?.observe(el);
    if (inner.current) observer?.observe(inner.current);
    return () => {
      el.removeEventListener("scroll", measure);
      observer?.disconnect();
    };
  }, []);

  return (
    <div className="table-scroll">
      <div className="table-wrap" tabIndex={0} ref={wrap}>
        {/* THE TABLE'S OWN NAME. `/site-servers` wrote `<table aria-label="Site
            servers">` before the port and lost it to a `<Table>` that took no
            such prop -- a scroller that is a tab stop, announced as nothing.
            Optional, because most tables sit under a heading that names them. */}
        <table ref={inner} aria-label={label}>
          {children}
        </table>
      </div>
      {more ? (
        <p className="table-scroll-hint" aria-hidden="true">
          {t("components.ui.scrollForMore")}
          <Icon name="chevron-right" size={13} />
        </p>
      ) : null}
    </div>
  );
}

// `th`/`td` are styled by `.table-wrap table` in the design, so these carry no
// classes of their own. They stay as components rather than being replaced by
// bare tags because 109 files import them, and because a future design
// revision may well give them modifiers.

/**
 * True when `children` would put no text in the accessibility tree.
 *
 * Hand-rolled rather than `Children.toArray`, because that helper drops
 * `null`/`false` and flattens — which answers "how many nodes" and not the
 * question here, which is "does a screen reader hear anything". `0` is a real
 * datum and deliberately NOT empty; `""` from an absent value is.
 */
function namesNothing(children: ReactNode): boolean {
  if (children == null || typeof children === "boolean") return true;
  if (typeof children === "string") return children.trim() === "";
  if (Array.isArray(children)) return children.every(namesNothing);
  return false;
}

/**
 * A column header. IT CANNOT RENDER WITHOUT AN ACCESSIBLE NAME, and that is
 * the whole of the fix for a defect this repo carried in forty-seven places.
 *
 * WHAT WAS WRONG. The trailing actions column is written `<Th>{""}</Th>`
 * everywhere — a blank header, because a heading over a column of buttons
 * would be visual noise. A sighted reader infers the column from its buttons.
 * A screen-reader user hears the column header announced before each cell and
 * heard NOTHING for the column that holds Delete. axe calls it
 * `empty-table-header`; the browser pass reported it twelve times, all from
 * the three instances that happen to be in `/__preview`.
 *
 * WHY THE FIX IS HERE AND NOT AT THE CALL SITES. There were thirty-one of
 * these going through this component, in nineteen files, across fourteen
 * modules — plus sixteen more written as raw `<th>` tags. Repairing the three
 * the harness renders would have closed the finding and left the defect, and
 * the next table written the same way would have shipped the same way. A
 * component that cannot express the bad state is a guard; a convention that
 * someone has to remember is not. `table-headers.test.tsx` holds both this
 * behaviour and the raw-tag half a component can never see.
 *
 * WHY THE DEFAULT IS "Actions". Every one of the thirty-one sites was audited
 * and every one is the trailing column of controls. That is what the fallback
 * says, so it is accurate rather than merely non-empty — an unnamed column and
 * a wrongly named one are both defects. `label` is for the header that is
 * blank for some other reason (a leading selection column, a spacer), and it
 * exists because `devices/CandidatesPage.tsx` has exactly that shape.
 */
export function Th({
  children,
  label,
  width,
  className,
}: {
  children?: ReactNode;
  label?: string;
  /**
   * A measure for this column, declared once in the header rather than as an
   * inline style on every cell's content.
   *
   * `min-width`, not `width`: the table is auto-layout, so this is a floor a
   * column may exceed and never a cap -- which is the shape the review
   * findings actually asked for (a status column collapsing to one character
   * per line at 390px). A `number` is CSS pixels.
   */
  width?: number | string;
  className?: string;
}) {
  const t = useT();
  const measure = width === undefined ? undefined : { minWidth: width };
  if (namesNothing(children)) {
    // `sr-only` is clipped out of the paint and stays in the a11y tree.
    // `base.css` restates it unlayered on purpose, so a `table`-scoped element
    // rule cannot beat it on specificity and un-hide this.
    //
    // `position: relative` ON THE CELL IS LOAD-BEARING, and the browser pass
    // caught its absence within one run of adding the span. `.sr-only` is
    // `position: absolute`, and an absolutely positioned box with no
    // POSITIONED ancestor is laid out against the initial containing block —
    // which means `.table-wrap`'s `overflow-x: auto` does not clip it, because
    // a scroll container only clips descendants it is the containing block
    // for. The name was therefore placed at its static position inside a table
    // 869px wide in a 368px scroller, and `documentElement.scrollWidth` went
    // from 390 to 604 at a 390px viewport: a horizontal page scroll on every
    // narrow screen, SC 1.4.10, introduced by an accessibility fix.
    //
    // Making the cell the containing block puts the name back inside the
    // scroller. It is inline rather than a class because it is not a design
    // decision that a stylesheet should be able to overrule — `base.css`
    // already records one cascade fight `.sr-only` lost here, and this
    // declaration existing is the difference between a clean pass and a
    // 214px-too-wide document.
    return (
      <th className={className} style={{ position: "relative", ...measure }}>
        <span className="sr-only">{label ?? t("components.ui.actions")}</span>
      </th>
    );
  }
  return (
    <th className={className} style={measure}>
      {children}
    </th>
  );
}

/**
 * A cell, with v3's two row adornments available on it.
 *
 * `gutter` is the design's 3px severity edge (`.lrow.crit .gut`): a `<table>`
 * has no spare column for it, so the cell paints it as an inset edge on its
 * own left side. `lead` is the design's glyph before the row's title
 * (`.lrow .lead`). Both are optional and both are for the FIRST cell of a row;
 * a page adopts them where v3 shows them.
 */
export function Td({
  children,
  className,
  colSpan,
  lead,
  gutter,
  clip,
  title,
  style,
}: {
  // OPTIONAL, so a cell can be genuinely empty. A grouped table has spacer
  // cells under the column its group heading has already answered, and
  // `<Td>{null}</Td>` to satisfy a required prop is a worse way to say that.
  children?: ReactNode;
  className?: string;
  colSpan?: number;
  /** The glyph before the cell's content: an OS icon, a ticket, a package. */
  lead?: ReactNode;
  /** The row's severity, drawn as the 3px edge. `ok`, `mut` and `info` draw nothing, as in the design. */
  gutter?: Kind;
  /**
   * THE COLUMN'S MEASURE, AND WHY IT LIVES ON THE CELL RATHER THAN THE HEADER.
   *
   * A number (CSS pixels) or a length caps this cell's `max-width` and makes
   * its text one clipped line. In an auto-layout table that is a COMPLETE
   * column measure, not half of one: `white-space: nowrap` makes the cell's
   * min-content width equal its max-content width, `max-width` then clamps
   * both, so the column settles at exactly this measure -- a floor against
   * the starvation in the review's I1/I9, and a ceiling against a nowrap
   * column dragging the scroller out to the length of the longest string.
   * `clip` alone (boolean) clips within whatever width the column already has.
   *
   * It cannot be expressed on `Th` instead. A `max-width` on the header does
   * not clamp the column, because the column's max-content contribution is
   * computed over every cell in it and the body cells would still push it
   * wide. `Th`'s `width` remains the right tool for the OTHER column shape:
   * one that must keep wrapping (a note, an error paragraph) but must not be
   * squeezed to one word per line.
   *
   * NOTHING IS LOST TO THE ELLIPSIS. `title` carries the full value; when it
   * is not given it is read off the cell's own text, so a clipped cell is
   * always recoverable by hover and by the accessible description. Pass
   * `title` explicitly where the visible text is not the whole story.
   */
  clip?: number | string | boolean;
  /** The full value behind a clipped cell. Derived from the cell's text when omitted. */
  title?: string;
  /**
   * A measure for THIS cell. Prefer `clip` for a column measure; this is for
   * the cell that needs something else of its own (a spanning summary row, a
   * cell whose column has no header).
   */
  style?: CSSProperties;
}) {
  const edge = gutter === "crit" || gutter === "warn" || gutter === "run" ? `gut-${gutter}` : undefined;
  const clipped = clip !== undefined && clip !== false;
  const measure =
    clip === undefined || typeof clip === "boolean" ? undefined : { maxWidth: clip };
  // ONLY A CLIPPED CELL GETS A `title`: an untruncated cell repeating its own
  // text in a tooltip is noise, and a screen reader reads the description in
  // place of the content. Whitespace is collapsed once, here rather than
  // inside the walk, because the JSX that produces these cells indents its
  // children and the raw concatenation carries the source's newlines.
  const full = clipped ? (title ?? textOf(children).replace(/\s+/g, " ").trim()) : undefined;
  return (
    <td
      className={cx(className, edge, clipped && "cell-clip") || undefined}
      colSpan={colSpan}
      title={full === "" ? undefined : full}
      style={measure ? { ...measure, ...style } : style}
    >
      {lead ? (
        <span className="cell-lead">
          <span className="lead">{lead}</span>
          <span style={{ minWidth: 0 }}>{children}</span>
        </span>
      ) : (
        children
      )}
    </td>
  );
}

/**
 * The text a reader would see in this subtree, for the `title` of a clipped
 * cell.
 *
 * It walks into elements' `children` on purpose. The cells this exists for do
 * not hold bare strings -- they hold `<Link>{ticket.subject}</Link>` inside a
 * `<div className="cell-title">`, which is precisely the text that gets
 * ellipsised and precisely the text a hover has to give back. Stopping at the
 * first element would hand every one of them an empty title and quietly turn
 * "truncated but recoverable" into "truncated".
 *
 * `false`, `null` and `undefined` render nothing and contribute nothing; `0`
 * is a real datum and contributes. Anything else (a function child, a
 * portal) is not text and is skipped rather than stringified into `[object
 * Object]`.
 */
function textOf(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  // JOINED WITH A SPACE, not with nothing. A cell's children are a title block
  // and a sub-line block, and concatenating them gives `firmwareStandard` --
  // one word that is in neither of them. The collapse in `Td` removes the
  // surplus.
  if (Array.isArray(node)) return node.map(textOf).join(" ");
  if (isValidElement(node)) {
    const props = node.props as { children?: ReactNode };
    return textOf(props.children);
  }
  return "";
}


/* --------------------------------------------------------------- dialogs */

/**
 * A read-only view of a ref, so a `RefObject<HTMLButtonElement | null>` can be
 * handed to something that only ever reads an `HTMLElement | null`. React's
 * own `RefObject` has a mutable `current` and is therefore invariant, which
 * would reject every concrete element type at the call sites below.
 */
type FocusTarget = { readonly current: HTMLElement | null };

/**
 * What Tab can land on inside a surface, in document order.
 *
 * NO VISIBILITY FILTER, DELIBERATELY. jsdom implements no layout, so
 * `offsetParent`, `getClientRects()` and a computed `display` are all useless
 * in the suite that has to keep this honest — a filter written against them
 * would return an empty list under test and the whole trap would be a no-op
 * nothing could see. `disabled`, `hidden` and `tabindex="-1"` are structural,
 * are what actually removes a control from the tab order here, and are the
 * same in both engines. The one this buys directly: the type-to-confirm
 * dialog's blocked confirm button is `disabled`, so the cycle runs between
 * the field and Cancel rather than parking on a control that cannot be used.
 */
const FOCUSABLE = [
  "a[href]",
  "area[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "button:not([disabled])",
  "iframe",
  "object",
  "embed",
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function tabStops(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => !el.hasAttribute("hidden") && el.getAttribute("aria-hidden") !== "true",
  );
}

/**
 * Scroll lock for the page behind a modal surface, REFERENCE-COUNTED.
 *
 * The page scrolling under the wheel is what makes a modal read as an overlay
 * rather than as a task, so every surface below locks it. Counting is what
 * makes stacking safe, and save-and-restore-per-surface is not: a
 * `ConfirmDialog` raised over an open `Dialog` saves "hidden" as its previous
 * value, and if the two then unmount in the same commit the order of the two
 * cleanups decides whether the page is left locked forever. That is not
 * hypothetical -- it is what three tests in the deploy module caught the
 * moment ConfirmDialog started locking at all, including one asserting merely
 * that no dialog was open.
 *
 * The saved value is taken once, when the count goes 0 -> 1, and put back once
 * when it returns to 0 -- so a host page that had already set `overflow`
 * itself gets its own value back rather than a blanked one.
 */
let scrollLocks = 0;
let lockedOverflow = "";
let lockedPadding = "";

function lockScroll(): () => void {
  if (scrollLocks === 0) {
    lockedOverflow = document.body.style.overflow;
    lockedPadding = document.body.style.paddingRight;
    // SCROLLBAR-GUTTER COMPENSATION. Hiding the overflow removes the
    // scrollbar, and without this the whole page jumps sideways as the dialog
    // opens. Zero on overlay-scrollbar platforms and in jsdom, so it adds
    // nothing where nothing is needed.
    const gutter = window.innerWidth - document.documentElement.clientWidth;
    document.body.style.overflow = "hidden";
    if (gutter > 0) document.body.style.paddingRight = `${gutter}px`;
  }
  scrollLocks += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    scrollLocks -= 1;
    if (scrollLocks === 0) {
      document.body.style.overflow = lockedOverflow;
      document.body.style.paddingRight = lockedPadding;
    }
  };
}

/**
 * Everything a modal surface owes the keyboard: initial focus, a focus trap,
 * document-level Escape, scroll lock, and focus given back on close.
 *
 * ONE IMPLEMENTATION, THREE SURFACES, and the third is why this was extracted.
 * `Dialog` grew focus-in, Escape and scroll lock earlier in this programme;
 * the browser pass then found `ConfirmDialog` and `CommandPalette` each
 * missing a different subset — four Tabs from the open destructive
 * confirmation reached the rail's "Dashboard" link with the dialog still up
 * and `aria-modal="true"` telling assistive technology the opposite, and the
 * palette's Escape was bound to its input, so one Tab out of it left a surface
 * no keypress could dismiss. Writing a third focus implementation is how those
 * drift apart again.
 *
 * TWO TRAPS ARE ALREADY PAID FOR AND ARE PRESERVED BELOW. The effect must not
 * depend on `onClose`: every call site passes an inline arrow, so an effect
 * keyed on it re-runs on every keystroke and `focus()` inside it steals focus
 * back from the field being typed into (that one cost 60 tests). And React
 * clears refs BEFORE effect cleanup runs, so "is focus still inside the
 * panel?" cannot be asked on the way out; only orphaned focus can be restored.
 *
 * THE TRAP IS KEYED ON CONTAINMENT, not on a stack of open surfaces. A
 * `ConfirmDialog` raised over a `Dialog` puts two of these handlers on the
 * document at once; each acts only while the focus is inside its own root, and
 * the inner one is bound later so it runs last and wins the overlap. A
 * module-level stack would instead depend on effect ordering, which React runs
 * child-first — exactly backwards for a confirmation nested inside the form it
 * is confirming.
 */
export function useDialogSurface({
  open,
  surface,
  onClose,
  dismissible = true,
  initialFocus,
}: {
  open: boolean;
  /** The panel itself. It needs `tabIndex={-1}` if nothing inside it takes focus first. */
  surface: FocusTarget;
  onClose: () => void;
  /**
   * False while another surface is stacked above this one — a ConfirmDialog,
   * typically. The surface above binds its own document-level Escape handler
   * and does not stop propagation, so without this switch one keypress answers
   * BOTH: it cancels the confirmation and discards the half-filled form behind
   * it. Scroll stays locked either way; the page behind a stack of two dialogs
   * is no more scrollable than behind one.
   */
  dismissible?: boolean;
  /** Where focus lands on open. Defaults to the surface itself. */
  initialFocus?: FocusTarget;
}): void {
  const opener = useRef<Element | null>(null);
  const close = useRef(onClose);
  close.current = onClose;
  const dismiss = useRef(dismissible);
  dismiss.current = dismissible;
  const landing = useRef(initialFocus);
  landing.current = initialFocus;

  useEffect(() => {
    if (!open) return;
    // WHAT TO GIVE FOCUS BACK TO, READ BEFORE ANYTHING INSIDE TAKES IT. The
    // guard is not defensive padding: React applies `autoFocus` during the
    // commit, which runs before this passive effect, so a field marked
    // `autoFocus` inside the surface would already BE the active element and
    // would be recorded as its own opener. Escape then restores focus to a
    // node React has just detached and the page is left with focus on <body>,
    // which is exactly what the browser pass measured on the command palette.
    // Initial focus is this hook's job precisely so the two cannot race.
    const active = document.activeElement;
    opener.current = active && surface.current?.contains(active) ? null : active;
    (landing.current?.current ?? surface.current)?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (dismiss.current) close.current();
        return;
      }
      if (event.key !== "Tab") return;
      const root = surface.current;
      if (!root || !root.contains(document.activeElement)) return;
      const stops = tabStops(root);
      if (stops.length === 0) {
        // Nothing inside can take focus, so the only correct destination is
        // the panel. Leaving it to the browser walks straight out to the page.
        event.preventDefault();
        root.focus();
        return;
      }
      const first = stops[0];
      const last = stops[stops.length - 1];
      const active = document.activeElement;
      // The panel itself counts as both ends: `Dialog` lands focus on it
      // rather than on its first field, so it is where a forward Tab starts
      // and where a backward one arrives.
      if (event.shiftKey && (active === first || active === root)) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && (active === last || active === root)) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", onKey);

    const unlock = lockScroll();

    return () => {
      document.removeEventListener("keydown", onKey);
      unlock();
      // RESTORE ONLY ORPHANED FOCUS. When the surface unmounts, focus falls to
      // <body> — that is the case worth rescuing, and it is what the browser
      // pass measured after Escape on both of these. If focus is anywhere else
      // the user has already moved on (they clicked a different control, which
      // is often what closed it), and yanking them back would be worse than
      // the problem.
      //
      // The obvious test — "is focus still inside the panel?" — cannot work
      // here: React clears the ref when it unmounts the section, so by the
      // time this cleanup runs `surface.current` is already null. Found by a
      // test failing, not by reading the docs.
      const orphaned = document.activeElement === null || document.activeElement === document.body;
      if (orphaned) (opener.current as HTMLElement | null)?.focus?.();
    };
    // `surface` is a ref object and is stable for the life of the component.
  }, [open, surface]);
}

/**
 * The shared modal.
 *
 * ONE FIX SITE, SIX PAGES. Both auditors filed the same four gaps against
 * every modal in the product and one of them counted them out: "Add channel"
 * on /notifications, "Add mailbox" on /email-intake, "Provision a device" on
 * /provisioning, "Promote a device" on /site-servers, "New license" on
 * /licenses, "New subscription" on /subscriptions — the same component, the
 * same four gaps, six times. Their own conclusion was that "one fix to the
 * shared modal component repairs at least six dialogs", which is what this is.
 *
 * The four: a real <form> (the pages' own concern, and now done), a labelled
 * close control (added earlier), Escape-to-close, and initial focus. Scroll
 * lock is the fifth the audit asked for on the license modal.
 *
 * FOCUS LANDS ON THE MODAL ITSELF, not on its first field. A dialog that
 * focuses its first input skips its own heading, so a screen-reader user hears
 * "Queue name, edit text" with no idea what dialog they are in; the panel
 * carries `tabIndex={-1}` so it can take focus and be announced by its
 * `aria-label`. This deliberately differs from ConfirmDialog, which focuses
 * Cancel — there the point is that a stray Enter must not confirm, and there
 * is no form to fill.
 *
 * Escape, scroll lock, the focus trap and returning focus to whatever opened
 * this all live in `useDialogSurface` above, which ConfirmDialog and the
 * command palette share. The reasoning for each is there.
 */
export function Dialog({
  open,
  onClose,
  title,
  dismissible = true,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  /**
   * False while another surface is stacked above this one — a ConfirmDialog,
   * typically. ConfirmDialog binds its own document-level Escape handler and
   * does not stop propagation, so without this switch one keypress answers
   * BOTH: it cancels the confirmation and discards the half-filled form
   * behind it. Scroll stays locked either way; the page behind a stack of two
   * dialogs is no more scrollable than behind one.
   */
  dismissible?: boolean;
  children: ReactNode;
}) {
  const t = useT();
  const panel = useRef<HTMLElement>(null);
  useDialogSurface({ open, surface: panel, onClose, dismissible });

  if (!open) return null;
  return (
    <>
      <div className="overlay open" onClick={onClose} />
      <section
        className="modal open"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        ref={panel}
        tabIndex={-1}
      >
        <div className="modal-head">
          <div>
            {/* `h2`, not the design's `div`. The prototype styles
             * `.modal-title` by CLASS, so this renders identically -- but a
             * dialog whose title is a div has no heading for a screen reader
             * to land on, and `SessionsPage.test.tsx` looks for exactly that
             * role. Transcribe the design's APPEARANCE, not its a11y gaps. */}
            <h2 className="modal-title">{title}</h2>
          </div>
          <button className="close-btn" onClick={onClose} aria-label={t("common.actions.close")}>
            <Icon name="close" size={16} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </section>
    </>
  );
}

/**
 * One dialog field: a caption above its own control.
 *
 * `full` IS THE DESIGN'S MODIFIER, NOT A NEW ONE. `.field.full` declares
 * `grid-column: 1 / -1` and nothing else -- it exists purely to make a field
 * span the `.form-grid` it sits in, which is what the design file's
 * `formField(label, control, full)` third argument switches on. Use it for
 * the fields the design uses it for: a textarea, a JSON editor, a long
 * summary or description, anything whose value is a sentence rather than a
 * token. Two short fields side by side is what the other column is for.
 *
 * Outside a `.form-grid` it is inert -- `grid-column` on a block-flow child
 * means nothing -- which is exactly the silent no-op that made `.form-grid`'s
 * absence invisible for a whole port. The grid is the container; this is the
 * cell.
 */
export function Field({
  label,
  help,
  full = false,
  children,
}: {
  label: string;
  /**
   * One clause, rendered OUTSIDE the label and pointed at by the control's
   * `aria-describedby`. See the header comment for why it cannot live inside.
   *
   * IT CONSTRAINS `children`, AND THE TYPE SYSTEM CANNOT STATE THE
   * CONSTRAINT. To be described, the control has to RECEIVE the attribute:
   * `children` must be a single element that puts the props it is handed onto
   * the rendered control -- an intrinsic (`<input>`, `<select>`,
   * `<textarea>`) or a component that spreads its rest props. A fragment, an
   * array of children, a bare string, or a component that drops what it does
   * not recognise all leave the paragraph on screen and pointed at by
   * nothing, which is worse than no help at all: it reads as wired to anyone
   * looking at the markup. `children` is `ReactNode` for the ~300 callers
   * that pass no help, so this is enforced after render instead -- see
   * `useEffect` below.
   */
  help?: ReactNode;
  full?: boolean;
  children: ReactNode;
}) {
  const helpId = useId();
  const wrap = useRef<HTMLDivElement>(null);
  // THE EFFECT BELOW HAS NO DEPENDENCY ARRAY -- it has to re-check on every
  // render, since `children` can change shape without `help` or `helpId`
  // changing. Undeduped that re-warns on every re-render a broken field
  // survives, not once; this latch (the same shape `icons.tsx`'s
  // `warnedOnce` uses) fires it once per failure and clears the moment the
  // wiring is fixed, so a later regression is still reported.
  const warnedRef = useRef(false);

  // HELP IS A DESCRIPTION, NOT A NAME. `Field` wraps its control INSIDE the
  // <label>, so anything rendered there joins the accessible NAME --
  // /agent-push/authorizations shipped a field announcing its whole help
  // sentence as its name. The help sits outside the label and is wired with
  // `aria-describedby`; an existing describedby is preserved, not clobbered.
  //
  // A FRAGMENT IS EXCLUDED FROM THE CLONE ON PURPOSE. `isValidElement` says
  // yes to `<>...</>`, and cloning one puts `aria-describedby` on the
  // Fragment, where it lands on no DOM node and React logs a prop warning of
  // its own. Leaving it uncloned lets the check below be the voice that
  // speaks, and say something useful.
  const described =
    help && isValidElement(children) && children.type !== Fragment
      ? cloneElement(children as ReactElement<{ "aria-describedby"?: string }>, {
          "aria-describedby": [
            (children.props as { "aria-describedby"?: string })["aria-describedby"],
            helpId,
          ]
            .filter(Boolean)
            .join(" "),
        })
      : children;

  // LOUD, BECAUSE THE FAILURE IS OTHERWISE PERFECTLY INVISIBLE. Every way the
  // wiring can fail -- a fragment, an array of children (three call sites
  // pass one today), a bare string, a component that does not spread its
  // props -- renders a paragraph that LOOKS like help, in the right place,
  // in the right type, describing nothing. jsdom is happy, `tsc` is clean,
  // and the only witness is a screen reader.
  //
  // So the committed DOM is asked, rather than the children guessed at: did
  // anything in this wrapper actually come to carry the id? The last of the
  // four cases above cannot be detected any other way -- a component that
  // drops unknown props is indistinguishable from one that keeps them until
  // it has rendered. `console.error` and degrade, which is the shape
  // `Icon`'s unknown-name path and `RecordingPlayer`'s already use: a hint
  // that is not announced is a defect to fix on the next render, not a reason
  // to take a settings page down in front of a customer.
  useEffect(() => {
    if (!help || !wrap.current) return;
    if (wrap.current.querySelector(`[aria-describedby~="${helpId}"]`)) {
      warnedRef.current = false;
      return;
    }
    if (warnedRef.current) return;
    warnedRef.current = true;
    console.error(
      `Field: the help for "${label}" is not announced -- nothing in the field ` +
        `carries aria-describedby="${helpId}". \`help\` needs a single child ` +
        `element that spreads the props it is given onto the control; a fragment, ` +
        `an array of children, a bare string, or a component that drops unknown ` +
        `props cannot receive it.`,
    );
  });

  const field = (
    <label className={full && !help ? "field full" : "field"}>
      <span>{label}</span>
      {described}
    </label>
  );

  // STRICTLY ADDITIVE. ~300 call sites pass no `help`, and every one of them
  // must keep emitting the label alone, `field full` included -- the wrapper
  // is what carries the span modifier once there IS a wrapper to carry it.
  if (!help) return field;
  return (
    <div className={full ? "field-wrap full" : "field-wrap"} ref={wrap}>
      {field}
      <p className="help" id={helpId}>
        {help}
      </p>
    </div>
  );
}

/**
 * The class every form field in the application carries.
 *
 * `.input-field`, NOT `.search-field`. The design defines three input classes
 * (`shell/components.css`) that are identical except that `.search-field` adds
 * `min-width: 230px; flex: 1` — geometry for a search box in a flex filter row,
 * and wrong for a field stacked in a dialog. This constant pointed at the
 * search class and is used by ~300 fields, so every dialog laid its inputs out
 * as search bars. Reported from the first live deployment as the job dispatch
 * dialog "not matching the page style".
 *
 * Real search inputs are unaffected — they carry `className="search-field"`
 * literally, never through this constant. `ui.test.ts` pins both halves.
 */
export const inputClass = "input-field";
