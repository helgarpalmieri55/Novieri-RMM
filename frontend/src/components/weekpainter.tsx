/**
 * The maintenance-window painter -- v3's `week(sel)` grid, made operable.
 *
 * WHAT IT IS FOR. Three fields in the console take a cron expression (the
 * patch maintenance window, the deployment window, the backup window), and
 * each was a text box asking an operator to write `0 2-5 * * 6` from memory
 * at 2am. The design draws the week as a 7×24 grid instead and says "drag to
 * paint". This is that grid, and it emits the five-field expression the
 * server's `window_open` parses (`backend/app/core/schedule.py`).
 *
 * THE MODEL IS A WINDOW, NOT A BITMAP. One expression says "these hours on
 * these days" -- a set of hours times a set of days -- so the painted cells
 * are always that product. Painting a cell adds its hour and its day;
 * unpainting one shrinks the window by an hour when it spans one day and by
 * a day when it spans several, because those are the two shrinks an
 * expression can express. The sentence under the grid says the same thing in
 * words, so the grid is never the only carrier.
 *
 * NOTHING EXPRESSIBLE TODAY STOPS BEING EXPRESSIBLE. An expression the grid
 * cannot draw -- a minute, a step, a day of the month, a month, a name -- is
 * shown as "custom expression": the grid is disabled, the raw text stays in
 * the Advanced field, and editing it there is the same `onChange` the grid
 * drives.
 *
 * KEYBOARD. One tab stop (roving tabindex), arrows move, Space and Enter
 * toggle, Shift with an arrow extends a rectangle from where the selection
 * started. Each cell is a `<button role="gridcell">` named "Tuesday 02:00";
 * its state is `aria-selected` -- the state ARIA gives a cell in a
 * multiselectable grid -- rather than `aria-pressed`, which axe rejects on
 * that role (`aria-allowed-attr`) and the a11y pass would fail on.
 */

import { useId, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

import { useT } from "../i18n/context";
import { translate } from "../i18n/core";
import { inputClass } from "./ui";
import "../shell/viz.css";

export interface Window {
  /** 0-23, ascending, unique. */
  hours: number[];
  /** Cron day-of-week, 0 (Sunday) to 6 (Saturday), ascending, unique. */
  days: number[];
}

/** Rows top to bottom, as the design draws them: Monday first, Sunday last. */
const ROWS = [1, 2, 3, 4, 5, 6, 0] as const;
const DAY_KEYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"] as const;
const dayName = (day: number) => translate(`components.weekPainter.day.${DAY_KEYS[day] as (typeof DAY_KEYS)[number]}`);
const dayShort = (day: number) => translate(`components.weekPainter.dayShort.${DAY_KEYS[day] as (typeof DAY_KEYS)[number]}`);
const HOURS = Array.from({ length: 24 }, (_, hour) => hour);

const hh = (hour: number) => `${String(hour).padStart(2, "0")}:00`;

/**
 * One cron field as the set of values it names, or null when it uses a form
 * the grid cannot draw. `*` is every value; a list of numbers and `a-b`
 * ranges is fine; a step, a name or anything out of range is not.
 */
function field(text: string, lo: number, hi: number): number[] | null {
  if (text === "*") return Array.from({ length: hi - lo + 1 }, (_, at) => lo + at);
  const found = new Set<number>();
  for (const part of text.split(",")) {
    const range = /^(\d{1,2})(?:-(\d{1,2}))?$/.exec(part);
    if (!range) return null;
    const from = Number(range[1]);
    const to = range[2] === undefined ? from : Number(range[2]);
    if (from < lo || to > hi || to < from) return null;
    for (let value = from; value <= to; value += 1) found.add(value);
  }
  return [...found].sort((a, b) => a - b);
}

/**
 * The window a cron expression describes, or null when the grid cannot draw
 * it: the minute must be 0, day-of-month and month `*`, hours and days lists
 * or ranges. Cron's second Sunday (7) folds onto its first (0).
 */
export function cronToWindow(cron: string): Window | null {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute, hourField, dom, month, dowField] = parts as [string, string, string, string, string];
  if (!/^0+$/.test(minute) || dom !== "*" || month !== "*") return null;
  const hours = field(hourField, 0, 23);
  const raw = field(dowField, 0, 7);
  if (!hours || !raw || hours.length === 0 || raw.length === 0) return null;
  const days = [...new Set(raw.map((day) => day % 7))].sort((a, b) => a - b);
  return { hours, days };
}

/** `[2,3,4,5,22,23]` -> `2-5,22-23`; a lone value stays a value. */
function compact(values: readonly number[]): string {
  const sorted = [...new Set(values)].sort((a, b) => a - b);
  const runs: string[] = [];
  for (let at = 0; at < sorted.length; ) {
    let end = at;
    while (end + 1 < sorted.length && sorted[end + 1] === (sorted[end] as number) + 1) end += 1;
    runs.push(end > at ? `${sorted[at]}-${sorted[end]}` : `${sorted[at]}`);
    at = end + 1;
  }
  return runs.join(",");
}

/** `0 <hours> * * <dows>`, hours as compact ranges, days as a list. */
export function windowToCron(window: Window): string {
  const days = [...new Set(window.days)].sort((a, b) => a - b);
  return `0 ${compact(window.hours)} * * ${days.join(",")}`;
}

/** The window in words: "Monday to Friday, 22:00 to 24:00". */
function describe(window: Window): string {
  const rows = ROWS.filter((day) => window.days.includes(day));
  let days: string;
  const first = ROWS.indexOf(rows[0] as (typeof ROWS)[number]);
  const consecutive = rows.every((day, at) => day === ROWS[first + at]);
  if (rows.length > 2 && consecutive) {
    days = translate("components.weekPainter.rangeTo", { from: dayName(rows[0] as number), to: dayName(rows[rows.length - 1] as number) });
  } else {
    days = rows.map((day) => dayName(day)).join(", ");
  }
  const hours = compact(window.hours)
    .split(",")
    .map((run) => {
      const [from, to] = run.split("-").map(Number) as [number, number | undefined];
      return translate("components.weekPainter.rangeTo", { from: hh(from), to: hh((to ?? from) + 1) });
    })
    .join(` ${translate("components.weekPainter.and")} `);
  return `${days}, ${hours}`;
}

const EMPTY: Window = { hours: [], days: [] };

export interface WeekPainterProps {
  /** The expression, or null for no window. */
  value: string | null;
  /** The new expression, or null when the grid is empty. */
  onChange: (cron: string | null) => void;
  /** The grid's accessible name: "Maintenance window". */
  label: string;
  /** The sentence under the grid when nothing is painted. */
  emptyHint?: string;
}

export function WeekPainter({
  value,
  onChange,
  label,
  emptyHint,
}: WeekPainterProps) {
  const t = useT();
  const raw = value ?? "";
  const parsed = raw.trim() === "" ? EMPTY : cronToWindow(raw);
  const custom = parsed === null;
  const window = parsed ?? EMPTY;

  const [focus, setFocus] = useState<{ row: number; hour: number }>({ row: 0, hour: 0 });
  const anchor = useRef<{ row: number; hour: number }>({ row: 0, hour: 0 });
  const drag = useRef<boolean | null>(null);
  const suppressClick = useRef(false);
  const extending = useRef(false);
  const id = useId();

  const isOn = (day: number, hour: number) => window.days.includes(day) && window.hours.includes(hour);

  const emit = (next: Window) => {
    // An empty side empties the other: a window with days and no hours is
    // not a window, and re-painting must not resurrect days nobody clicked.
    const window = next.hours.length === 0 || next.days.length === 0 ? EMPTY : next;
    onChange(window.hours.length === 0 ? null : windowToCron(window));
  };

  const paint = (day: number, hour: number, on: boolean) => {
    if (custom) return;
    if (on) {
      if (isOn(day, hour)) return;
      emit({ hours: [...window.hours, hour], days: [...window.days, day] });
      return;
    }
    if (!isOn(day, hour)) return;
    // The two shrinks an expression can express: an hour off a one-day
    // window, a day off a multi-day one.
    if (window.days.length <= 1) {
      emit({ hours: window.hours.filter((h) => h !== hour), days: window.days });
    } else {
      emit({ hours: window.hours, days: window.days.filter((d) => d !== day) });
    }
  };

  const toggle = (day: number, hour: number) => paint(day, hour, !isOn(day, hour));

  /** Paint the rectangle between the anchor and a position: a union, never a replacement. */
  const extendTo = (to: { row: number; hour: number }) => {
    const from = anchor.current;
    const rows = ROWS.slice(Math.min(from.row, to.row), Math.max(from.row, to.row) + 1);
    const hours = HOURS.slice(Math.min(from.hour, to.hour), Math.max(from.hour, to.hour) + 1);
    emit({ hours: [...window.hours, ...hours], days: [...window.days, ...rows] });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (custom) return;
    // THE CELL THE KEY LANDED ON, read from the event rather than from state:
    // focus can arrive programmatically (a test, a screen reader, a page
    // restoring it) without passing through this component's handlers.
    const target = (event.target as HTMLElement).closest<HTMLElement>("[data-row]");
    if (!target) return;
    const at = { row: Number(target.dataset.row), hour: Number(target.dataset.hour) };
    const day = ROWS[at.row] as number;
    if (event.key === " " || event.key === "Enter") {
      event.preventDefault();
      toggle(day, at.hour);
      anchor.current = at;
      return;
    }
    const delta: Record<string, [number, number]> = {
      ArrowLeft: [0, -1],
      ArrowRight: [0, 1],
      ArrowUp: [-1, 0],
      ArrowDown: [1, 0],
    };
    const step = delta[event.key];
    if (!step) return;
    event.preventDefault();
    const next = {
      row: Math.max(0, Math.min(ROWS.length - 1, at.row + step[0])),
      hour: Math.max(0, Math.min(23, at.hour + step[1])),
    };
    if (event.shiftKey) extendTo(next);
    else anchor.current = next;
    extending.current = true;
    event.currentTarget.querySelector<HTMLButtonElement>(`[data-row="${next.row}"][data-hour="${next.hour}"]`)?.focus();
    extending.current = false;
  };

  const onFocus = (row: number, hour: number) => () => {
    setFocus({ row, hour });
    // A focus that did not come from an arrow key is a new starting point for
    // Shift+arrow; one that did is mid-extension and keeps its anchor.
    if (!extending.current) anchor.current = { row, hour };
  };

  const onPointerDown = (day: number, hour: number, row: number) => (event: PointerEvent<HTMLButtonElement>) => {
    if (custom || event.button !== 0) return;
    const target = !isOn(day, hour);
    drag.current = target;
    suppressClick.current = true;
    setFocus({ row, hour });
    anchor.current = { row, hour };
    paint(day, hour, target);
  };

  const onPointerEnter = (day: number, hour: number) => (event: PointerEvent<HTMLButtonElement>) => {
    if (drag.current === null || (event.buttons & 1) === 0) return;
    paint(day, hour, drag.current);
  };

  const onClick = (day: number, hour: number) => () => {
    // The pointer path has already painted; this is the keyboard's and an
    // assistive technology's click, which arrives without a pointerdown.
    if (suppressClick.current) {
      suppressClick.current = false;
      return;
    }
    toggle(day, hour);
  };

  const summary = custom
    ? t("components.weekPainter.custom")
    : window.hours.length === 0
      ? (emptyHint ?? t("components.weekPainter.nothingSelected"))
      : t("components.weekPainter.selected", { summary: describe(window) });

  return (
    <div className="wk-painter" onPointerUp={() => (drag.current = null)} onPointerLeave={() => (drag.current = null)}>
      <div
        className="wk"
        role="grid"
        aria-label={label}
        aria-multiselectable="true"
        aria-readonly={custom || undefined}
        aria-describedby={`${id}-summary`}
        tabIndex={custom ? 0 : undefined}
        onKeyDown={onKeyDown}
      >
        <div className="wk-row" role="row">
          <span className="wk-d" role="columnheader">
            <span className="sr-only">{t("components.weekPainter.dayColumn")}</span>
          </span>
          {HOURS.map((hour) => (
            // The design prints every sixth hour; the other headers keep
            // their name for assistive technology (axe `empty-table-header`).
            <span className="wk-h" role="columnheader" key={hour}>
              {hour % 6 ? <span className="sr-only">{hh(hour)}</span> : hour}
            </span>
          ))}
        </div>
        {ROWS.map((day, row) => (
          <div className="wk-row" role="row" key={day}>
            <span className="wk-d" role="rowheader" aria-label={dayName(day)}>
              {dayShort(day)}
            </span>
            {HOURS.map((hour) => {
              const on = isOn(day, hour);
              const focused = focus.row === row && focus.hour === hour;
              return (
                <button
                  type="button"
                  key={hour}
                  role="gridcell"
                  className={on ? "wk-c on" : "wk-c"}
                  aria-selected={on}
                  aria-label={`${dayName(day)} ${hh(hour)}`}
                  tabIndex={focused ? 0 : -1}
                  disabled={custom}
                  data-row={row}
                  data-hour={hour}
                  onFocus={onFocus(row, hour)}
                  onPointerDown={onPointerDown(day, hour, row)}
                  onPointerEnter={onPointerEnter(day, hour)}
                  onClick={onClick(day, hour)}
                />
              );
            })}
          </div>
        ))}
      </div>
      <p className="wk-help" id={`${id}-summary`}>
        {summary}
      </p>
      <details className="wk-adv" open={custom || undefined}>
        <summary>{t("components.weekPainter.advanced")}</summary>
        <input
          className={`${inputClass} mono`}
          aria-label={t("components.weekPainter.cronExpression")}
          spellCheck={false}
          autoComplete="off"
          placeholder="0 2-5 * * 6"
          value={raw}
          onChange={(event) => onChange(event.target.value.trim() === "" ? null : event.target.value)}
        />
        <p className="wk-help">{t("components.weekPainter.cronHelp")}</p>
      </details>
    </div>
  );
}
