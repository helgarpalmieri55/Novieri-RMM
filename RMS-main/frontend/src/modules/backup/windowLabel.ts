/**
 * A backup window in words, not in crontab.
 *
 * WHY THIS EXISTS. The policy editor PAINTS the window on a 7x24 grid
 * (`WeekPainter`) and writes a sentence under it, and the policy list printed
 * the stored expression: one column carrying "Always open" beside
 * "0 1-4 * * *", so a reader met crontab syntax in exactly one place in the
 * console -- the table that is supposed to tell them when the backup runs.
 *
 * DUPLICATED, DELIBERATELY, from `modules/deploy/cron.ts`, on the same
 * reasoning `modules/patch/cron.ts` records for the validator beside it: the
 * three modules own separate fields with the same server-side semantics, and
 * neither may reach into another's package. The right home is `lib/`, which
 * this cycle does not own -- recorded for the controller rather than smuggled
 * in.
 *
 * `cronToWindow` is the painter's own parser, so a window this can put into
 * words is exactly a window the grid can draw; anything else returns null and
 * the caller prints the expression itself rather than guessing at it.
 */

import { cronToWindow } from "../../components/weekpainter";
import { translate, type MessageKey } from "../../i18n/core";

// Catalogue keys, not words: the words are read when a label is built.
const DAY_SHORT = [
  "backup.window.sun",
  "backup.window.mon",
  "backup.window.tue",
  "backup.window.wed",
  "backup.window.thu",
  "backup.window.fri",
  "backup.window.sat",
] as const satisfies readonly MessageKey[];
const dayShort = (day: number): string => translate(DAY_SHORT[day] as MessageKey);
/** Monday first, Sunday last -- the order the painter draws its rows in. */
const WEEK = [1, 2, 3, 4, 5, 6, 0] as const;

const hh = (hour: number): string => `${String(hour).padStart(2, "0")}:00`;

/** `[2,3,4,22]` -> `[[2,4],[22,22]]`. */
function runs(values: readonly number[]): Array<[number, number]> {
  const sorted = [...new Set(values)].sort((a, b) => a - b);
  const found: Array<[number, number]> = [];
  for (let at = 0; at < sorted.length; ) {
    let end = at;
    while (end + 1 < sorted.length && sorted[end + 1] === (sorted[end] as number) + 1) end += 1;
    found.push([sorted[at] as number, sorted[end] as number]);
    at = end + 1;
  }
  return found;
}

/**
 * The window in words -- "Every day, 01:00 to 05:00", "Weekdays, 02:00 to
 * 04:00" -- or null when the expression is not one the painter can draw.
 *
 * The hour a window ends is the hour AFTER the last one it holds: `1-4` opens
 * at 01:00 and closes at 05:00, because the matcher lets a run start at any
 * minute of hour 4.
 */
export function windowLabel(cron: string): string | null {
  const window = cronToWindow(cron);
  if (!window || window.hours.length === 0 || window.days.length === 0) return null;

  const rows = WEEK.filter((day) => window.days.includes(day));
  let days: string;
  if (rows.length === 7) {
    days = translate("backup.window.everyDay");
  } else if (rows.length === 5 && [1, 2, 3, 4, 5].every((day) => window.days.includes(day))) {
    days = translate("backup.window.weekdays");
  } else if (rows.length === 2 && window.days.includes(0) && window.days.includes(6)) {
    days = translate("backup.window.weekends");
  } else {
    const first = WEEK.indexOf(rows[0] as (typeof WEEK)[number]);
    const consecutive = rows.every((day, at) => day === WEEK[first + at]);
    days =
      rows.length > 2 && consecutive
        ? translate("backup.window.dayRange", {
            from: dayShort(rows[0] as number),
            to: dayShort(rows[rows.length - 1] as number),
          })
        : rows.map((day) => dayShort(day)).join(", ");
  }

  const hours =
    window.hours.length === 24
      ? translate("backup.window.allDay")
      : runs(window.hours)
          .map(([from, to]) => translate("backup.window.hourRange", { from: hh(from), to: hh(to + 1) }))
          .join(translate("backup.window.and"));

  return translate("backup.window.sentence", { days, hours });
}
