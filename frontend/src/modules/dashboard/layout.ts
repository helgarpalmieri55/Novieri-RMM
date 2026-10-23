/**
 * Resolving a stored dashboard arrangement against the widgets this build has.
 *
 * The operator, 2026-09-01: "main dashboard should be customizable with tiles
 * also give me the ability to resize and order in whichever way i want."
 *
 * THE WHOLE PROBLEM IS THAT THE STORED LIST AND THE BUILD'S LIST DISAGREE, and
 * they disagree in BOTH directions on the first release after somebody saves a
 * layout. The server deliberately does not know the widget list — the set of
 * widgets is a property of the console build, so a release that adds a tile
 * must not need a server change, and one that removes a tile must not strand
 * every stored row that names it. That decision moves the reconciliation here,
 * and makes it the thing worth testing.
 *
 *   · **An id the build does not have** is a widget a later release removed,
 *     or one the operator's browser is a version behind on. DROPPED from the
 *     rendering, and it is not written back — a save from an old tab must not
 *     delete a widget a newer tab arranged. (It survives in the stored row
 *     until the next save, which is the correct direction to be wrong in.)
 *   · **A widget the stored list does not mention** is one this release ADDED
 *     since the operator arranged their dashboard. APPENDED, in the default
 *     order, and visible. The alternative — hiding what the row does not name
 *     — makes every new dashboard feature invisible to exactly the operators
 *     who use the product enough to have customized it.
 *
 * PERMISSION IS NOT PART OF THIS FILE. A widget the caller may not see is
 * filtered by the page before it gets here, because the layout is a
 * preference and permission is a fact about the session: a layout must not be
 * rewritten (and a widget's saved position lost) because somebody's role
 * changed for an afternoon.
 */

/** How wide a widget sits in the six-column dashboard grid. */
import type { MessageKey } from "../../i18n/core";

export type WidgetSize = "small" | "medium" | "wide";

/** Mirrors `WidgetPlacement` in `backend/app/api/v1/dashboard.py`. */
export interface WidgetPlacement {
  id: string;
  size: WidgetSize;
  hidden: boolean;
}

/**
 * `GET`/`PUT /dashboard/layout`. `widgets: null` is "this person has never
 * arranged the dashboard" — never `[]`, which the server refuses to store so
 * that absent keeps its single meaning.
 */
export interface DashboardLayoutResponse {
  widgets: WidgetPlacement[] | null;
}

export const SIZE_COLUMNS: Record<WidgetSize, number> = {
  small: 2,
  medium: 3,
  wide: 6,
};

export const SIZE_LABELS: Record<WidgetSize, MessageKey> = {
  small: "dashboard.frame.size.small",
  medium: "dashboard.frame.size.medium",
  wide: "dashboard.frame.size.wide",
};

/** The cycle the one Size button walks. Three sizes, so a single button is
 *  honest — a "grow" and a "shrink" pair would each be disabled a third of
 *  the time, which reads as broken rather than as at-the-limit. */
export const SIZE_ORDER: WidgetSize[] = ["small", "medium", "wide"];

export function nextSize(size: WidgetSize): WidgetSize {
  // `?? "small"` is unreachable and is there for `noUncheckedIndexedAccess`:
  // the modulo keeps the index in range, and the compiler cannot know it.
  return SIZE_ORDER[(SIZE_ORDER.indexOf(size) + 1) % SIZE_ORDER.length] ?? "small";
}

/** What a widget is, from the layout's point of view: an id and a default. */
export interface WidgetDefault {
  id: string;
  defaultSize: WidgetSize;
}

/**
 * The arrangement to render: the stored order, minus what this build has no
 * widget for, plus what the stored order never heard of.
 *
 * `stored === null` means this person has never arranged the dashboard, and
 * the answer is the defaults in their declared order. It is NOT the same as an
 * empty list, which the server refuses to store for exactly that reason.
 */
export function resolveLayout(
  stored: WidgetPlacement[] | null | undefined,
  available: WidgetDefault[],
): WidgetPlacement[] {
  const defaults = available.map(
    (widget): WidgetPlacement => ({ id: widget.id, size: widget.defaultSize, hidden: false }),
  );
  if (stored == null) return defaults;

  const known = new Map(available.map((widget) => [widget.id, widget]));
  const placed = new Set<string>();
  const resolved: WidgetPlacement[] = [];
  for (const entry of stored) {
    // A duplicate id would render one widget twice and give React two nodes
    // with one key. The stored document is validated for shape by the server,
    // not for uniqueness, so it is enforced here where the consequence is.
    if (!known.has(entry.id) || placed.has(entry.id)) continue;
    placed.add(entry.id);
    resolved.push({ ...entry });
  }
  for (const widget of defaults) {
    if (!placed.has(widget.id)) resolved.push(widget);
  }
  return resolved;
}

/**
 * Move one widget one place earlier or later in the order.
 *
 * THE KEYBOARD PATH IS THE PRIMARY ONE, not a fallback bolted onto a drag.
 * WCAG 2.2 SC 2.5.7 (Dragging Movements) requires a single-pointer alternative
 * to any drag, and SC 2.1.1 requires a keyboard one; a pair of Move buttons
 * satisfies both by construction, and a drag layered on top is then a
 * convenience rather than the only way in. `docs/product.md` §11 makes AA
 * binding for this console.
 *
 * A move past either end is a no-op returning the SAME array, so a caller can
 * skip a save it does not need — and the button that would do nothing is
 * disabled rather than silently inert.
 */
export function moveWidget(
  layout: WidgetPlacement[],
  id: string,
  delta: -1 | 1,
): WidgetPlacement[] {
  const from = layout.findIndex((entry) => entry.id === id);
  if (from < 0) return layout;
  const to = from + delta;
  if (to < 0 || to >= layout.length) return layout;
  const next = [...layout];
  // Written out rather than destructured: both bounds are checked above, and
  // `noUncheckedIndexedAccess` types the destructured swap as possibly
  // undefined, which a non-null assertion would only paper over.
  const moving = layout[from] as WidgetPlacement;
  const displaced = layout[to] as WidgetPlacement;
  next[from] = displaced;
  next[to] = moving;
  return next;
}

/** Move a widget to an arbitrary index — the drop half of drag-and-drop. */
export function moveWidgetTo(
  layout: WidgetPlacement[],
  id: string,
  index: number,
): WidgetPlacement[] {
  const from = layout.findIndex((entry) => entry.id === id);
  if (from < 0 || index < 0 || index >= layout.length || from === index) return layout;
  const next = [...layout];
  const moved = next.splice(from, 1)[0] as WidgetPlacement;
  next.splice(index, 0, moved);
  return next;
}

export function setWidgetSize(
  layout: WidgetPlacement[],
  id: string,
  size: WidgetSize,
): WidgetPlacement[] {
  return layout.map((entry) => (entry.id === id ? { ...entry, size } : entry));
}

/**
 * Hide or show a widget IN PLACE.
 *
 * The entry keeps its position and its size, so putting a widget back restores
 * where it was rather than appending it to the end. A layout that dropped
 * hidden widgets entirely would make "hide this" and "forget where this was"
 * the same gesture.
 */
export function setWidgetHidden(
  layout: WidgetPlacement[],
  id: string,
  hidden: boolean,
): WidgetPlacement[] {
  return layout.map((entry) => (entry.id === id ? { ...entry, hidden } : entry));
}
