/**
 * Checking a maintenance-window expression BEFORE it is stored.
 *
 * WHY THIS EXISTS. `PatchPolicy.maintenance_window` is handed to the same
 * matcher a deployment window is -- `backend/app/core/schedule.py`'s
 * `window_open`, called from `patch/apply.py:124` -- and this editor's field
 * carried the placeholder `e.g. Sat 02:00-04:00`, which that matcher CANNOT
 * PARSE. It is a 5-field numeric cron expression or it raises `ValueError`
 * inside the apply path, and the policy silently gates nothing. A field whose
 * own example is invalid is a field nobody validated.
 *
 * TIMEZONE. The window is matched against `datetime.now(UTC)`
 * (`patch/apply.py:186`), so the label says UTC rather than leaving the
 * operator to guess between UTC, server time and their own -- the guess
 * Auditor B measured as a five-hour error elsewhere in the product.
 *
 * DUPLICATED, DELIBERATELY, from `modules/deploy/cron.ts`: the two modules own
 * separate fields with the same server-side semantics, and neither may reach
 * into the other. The right home is `lib/`, which this cycle does not own --
 * recorded for the controller rather than smuggled in.
 */

import { cronToWindow } from "../../components/weekpainter";
import { translate, type MessageKey } from "../../i18n/core";

interface CronField {
  /** The catalogue key of the field as a person would say it, for the message. */
  name: MessageKey;
  lo: number;
  hi: number;
}

// Bounds copied from `_parse_cron_field`'s call sites, in field order.
const FIELDS: readonly CronField[] = [
  { name: "patch.cron.minute", lo: 0, hi: 59 },
  { name: "patch.cron.hour", lo: 0, hi: 23 },
  { name: "patch.cron.dayOfMonth", lo: 1, hi: 31 },
  { name: "patch.cron.month", lo: 1, hi: 12 },
  { name: "patch.cron.dayOfWeek", lo: 0, hi: 7 },
];

/** A valid example, unlike the one this field used to carry. Read when drawn,
 *  so it follows the language. */
export function cronPlaceholder(): string {
  return translate("patch.cron.placeholder", { example: CRON_EXAMPLE });
}

/** The expression itself is not copy: it is the same in every language. */
const CRON_EXAMPLE = "0 2 * * 6";

/** The label suffix every window field wears, so the zone is never implied. */
export const CRON_ZONE_NOTE = "UTC";

function fieldProblem(raw: string, spec: CronField): string | null {
  const name = translate(spec.name);
  for (const rawPart of raw.split(",")) {
    const part = rawPart.trim();
    if (!part) {
      return translate("patch.cron.emptyValue", { name });
    }
    const slashed = part.split("/");
    if (slashed.length > 2) {
      return translate("patch.cron.moreSlash", { name });
    }
    const base = slashed[0] ?? "";
    const step = slashed[1];
    if (step !== undefined && !/^\d+$/.test(step)) {
      return translate("patch.cron.stepWhole", { name });
    }
    if (base === "*") continue;
    const bounds = base.split("-");
    if (bounds.length > 2) {
      return translate("patch.cron.moreDash", { name });
    }
    for (const bound of bounds) {
      if (!/^\d+$/.test(bound)) {
        // The message names the placeholder's own mistake, because that is the
        // value most operators will have typed.
        return spec.name === "patch.cron.dayOfWeek"
          ? translate("patch.cron.namesNotSupported", { name })
          : translate("patch.cron.numbersOnly", { name });
      }
      const value = Number(bound);
      if (value < spec.lo || value > spec.hi) {
        return translate("patch.cron.between", { name, lo: spec.lo, hi: spec.hi });
      }
    }
  }
  return null;
}

/**
 * What is wrong with `value` as a maintenance window, or null when nothing is.
 *
 * An empty string is NOT a problem here: the window is optional on both the
 * deployment and the patch policy, and "no window" means "always open".
 * Requiredness is the caller's rule, expressed with `requiredProblems`.
 */
export function cronProblem(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parts = trimmed.split(/\s+/);
  if (parts.length !== 5) {
    return translate("patch.cron.fiveFields", { example: cronPlaceholder() });
  }
  for (const [index, field] of FIELDS.entries()) {
    const problem = fieldProblem(parts[index] ?? "", field);
    if (problem) return problem;
  }
  return null;
}

/* ------------------------------------------------------ the window in words */

/**
 * READING A WINDOW BACK OUT, because the policy list was showing the operator
 * the machine's copy of it.
 *
 * `0 2-5 * * 6` in a table cell is a value nobody reads at a glance and
 * several people read wrong: cron's day-of-week is the field with two
 * Sundays, its ranges are inclusive at both ends, and `2-5` covers four hours
 * rather than three. The editor behind Edit has not asked anyone to parse
 * that since `WeekPainter` landed -- it paints the same value on a grid -- so
 * the list was the last surface in this module still speaking cron at a
 * person, and the row beneath it saying "always open" in plain words made the
 * inconsistency legible.
 *
 * THE EXPRESSION STAYS ON SCREEN, under the sentence rather than instead of
 * it. It is the stored value, it is what an operator pastes into a ticket or
 * compares against `patch/apply.py`'s matcher, and a reading that replaced it
 * would make the two impossible to check against each other.
 *
 * IT PARSES WITH `cronToWindow`, THE GRID'S OWN PARSER, and that is the point
 * of the import rather than a second parser here: the list and the editor
 * cannot then disagree about what a stored expression means. Anything the
 * grid cannot draw -- a step, a named day, a day-of-month -- returns null
 * here too, and the caller says so instead of inventing a reading.
 *
 * The hours are given as boundaries, `02:00 to 06:00`, not as the inclusive
 * cron hours `2 to 5`: the window is open for the whole of hour 5, and an
 * operator who reads "to 05:00" and schedules against it has been misled by
 * an hour.
 */

/** Rows as the painter draws them: Monday first, Sunday last. */
const DESCRIBE_ROWS = [1, 2, 3, 4, 5, 6, 0] as const;
const DESCRIBE_DAY: readonly MessageKey[] = [
  "patch.cron.sunday",
  "patch.cron.monday",
  "patch.cron.tuesday",
  "patch.cron.wednesday",
  "patch.cron.thursday",
  "patch.cron.friday",
  "patch.cron.saturday",
];

const dayName = (day: number): string => translate(DESCRIBE_DAY[day] as MessageKey);

const clock = (hour: number) => `${String(hour).padStart(2, "0")}:00`;

/** `[2,3,4,5,22,23]` -> `[[2,5],[22,23]]`; a lone value is its own run. */
function runs(values: readonly number[]): [number, number][] {
  const sorted = [...new Set(values)].sort((a, b) => a - b);
  const out: [number, number][] = [];
  for (let at = 0; at < sorted.length; ) {
    let end = at;
    while (end + 1 < sorted.length && sorted[end + 1] === (sorted[end] as number) + 1) end += 1;
    out.push([sorted[at] as number, sorted[end] as number]);
    at = end + 1;
  }
  return out;
}

/**
 * `value` as a sentence -- "Saturday, 02:00 to 06:00" -- or null when it uses
 * a form the painter cannot draw and this cannot honestly read.
 *
 * An empty or whitespace value is null too: "no window" means "always open",
 * and that sentence belongs to the caller, which is the only place that knows
 * whether an absent window is a permission or an omission.
 */
export function describeCron(value: string): string | null {
  const window = cronToWindow(value.trim());
  if (!window || window.days.length === 0 || window.hours.length === 0) return null;

  const ordered = DESCRIBE_ROWS.filter((day) => window.days.includes(day));
  const first = DESCRIBE_ROWS.indexOf(ordered[0] as (typeof DESCRIBE_ROWS)[number]);
  // Three or more consecutive rows read as a span; two read as a pair, because
  // "Monday to Tuesday" is a longer way of saying "Monday, Tuesday".
  const consecutive = ordered.every((day, at) => day === DESCRIBE_ROWS[first + at]);
  const days =
    ordered.length > 2 && consecutive
      ? translate("patch.cron.dayRange", {
          from: dayName(ordered[0] as number),
          to: dayName(ordered[ordered.length - 1] as number),
        })
      : ordered.map((day) => dayName(day)).join(", ");

  const hours = runs(window.hours)
    .map(([from, to]) => translate("patch.cron.hourRange", { from: clock(from), to: clock(to + 1) }))
    .join(translate("patch.cron.and"));

  // Day names are lower case mid-sentence in Spanish; the sentence is a cell
  // title, so its first letter is capitalised (English already is).
  const sentence = translate("patch.cron.sentence", { days, hours });
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}
