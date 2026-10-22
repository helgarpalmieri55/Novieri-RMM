/**
 * Checking a maintenance-window expression BEFORE it is stored.
 *
 * WHY THIS EXISTS. Auditor A, [MEDIUM], /deploy → New deployment: "Maintenance
 * window is a raw cron expression with no timezone stated ... no validation of
 * the expression."
 *
 * THE FIELD'S OWN EXAMPLE WAS INVALID, which is how this got past review. The
 * placeholder read `e.g. 0 2 * * SAT`, and the matcher this string is handed to
 * — `backend/app/core/schedule.py`'s `_parse_cron_field` — is a pure-stdlib
 * parser that calls `int()` on every term. `int("SAT")` raises, so a deployment
 * created by following the placeholder does not dispatch late: it raises
 * `ValueError` inside `execute_deployment` and returns a 500. Names are not
 * supported on ANY field; only `*`, `a`, `a-b`, and a `/n` step are.
 *
 * TIMEZONE. The window is matched against `datetime.now(UTC)`
 * (`deploy/api.py:494`, `patch/apply.py:186`), so the answer to "2 AM where?"
 * is UTC, and the label says so rather than leaving the operator to guess
 * between UTC, server time and their own — the guess Auditor B measured as a
 * five-hour error elsewhere in the product.
 */

import { cronToWindow } from "../../components/weekpainter";
import { translate, type MessageKey } from "../../i18n/core";

interface CronField {
  /** The catalogue entry of the field as a person would say it, for the message. */
  name: MessageKey;
  lo: number;
  hi: number;
}

// Bounds copied from `_parse_cron_field`'s call sites, in field order.
const FIELDS: readonly CronField[] = [
  { name: "deploy.cron.minute", lo: 0, hi: 59 },
  { name: "deploy.cron.hour", lo: 0, hi: 23 },
  { name: "deploy.cron.dayOfMonth", lo: 1, hi: 31 },
  { name: "deploy.cron.month", lo: 1, hi: 12 },
  { name: "deploy.cron.dayOfWeek", lo: 0, hi: 7 },
];

/** The expression itself is not copy: it is the same in every language. */
const CRON_EXAMPLE = "0 2 * * 6";

/** A valid example, unlike the one this field used to carry. Read when it is
 *  drawn, so it follows the language. */
export function cronPlaceholder(): string {
  return translate("deploy.cron.placeholder", { example: CRON_EXAMPLE });
}

/** The label suffix every window field wears, so the zone is never implied. */
export const CRON_ZONE_NOTE = "UTC";

function fieldProblem(raw: string, spec: CronField): string | null {
  const name = translate(spec.name);
  for (const rawPart of raw.split(",")) {
    const part = rawPart.trim();
    if (!part) {
      return translate("deploy.cron.emptyValue", { name });
    }
    const slashed = part.split("/");
    if (slashed.length > 2) {
      return translate("deploy.cron.moreSlash", { name });
    }
    const base = slashed[0] ?? "";
    const step = slashed[1];
    if (step !== undefined && !/^\d+$/.test(step)) {
      return translate("deploy.cron.stepWhole", { name });
    }
    if (base === "*") continue;
    const bounds = base.split("-");
    if (bounds.length > 2) {
      return translate("deploy.cron.moreDash", { name });
    }
    for (const bound of bounds) {
      if (!/^\d+$/.test(bound)) {
        // The message names the placeholder's own mistake, because that is the
        // value most operators will have typed.
        return spec.name === "deploy.cron.dayOfWeek"
          ? translate("deploy.cron.namesNotSupported", { name })
          : translate("deploy.cron.numbersOnly", { name });
      }
      const value = Number(bound);
      if (value < spec.lo || value > spec.hi) {
        return translate("deploy.cron.between", { name, lo: spec.lo, hi: spec.hi });
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
    return translate("deploy.cron.fiveFields", { example: cronPlaceholder() });
  }
  for (const [index, field] of FIELDS.entries()) {
    const problem = fieldProblem(parts[index] ?? "", field);
    if (problem) return problem;
  }
  return null;
}

/* ------------------------------------------------------------------ reading
 *
 * THE OTHER HALF OF THE FIELD: printing a window the operator did not type.
 *
 * The window is PAINTED on a 7x24 grid in every editor that sets one
 * (`WeekPainter`), and it was printed as raw crontab in every list that shows
 * one -- so the only place in the console a reader met `0 2-4 * * 6` was the
 * column that was supposed to tell them when the deployment runs. The review
 * wave filed it on /deploy and /backup/policies as one finding in two places.
 *
 * `cronToWindow` is the painter's own parser, so a window this function can
 * put into words is exactly a window the grid can draw, and an expression the
 * grid shows as "custom expression" returns null here: the caller prints the
 * expression itself rather than a sentence that would have to guess.
 */

const DAY_SHORT: readonly MessageKey[] = [
  "deploy.cron.sun",
  "deploy.cron.mon",
  "deploy.cron.tue",
  "deploy.cron.wed",
  "deploy.cron.thu",
  "deploy.cron.fri",
  "deploy.cron.sat",
];
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
 * The window in words -- "Sat, 02:00 to 05:00", "Weekdays, 22:00 to 24:00",
 * "Every day, 01:00 to 05:00" -- or null when the expression is not one the
 * painter can draw.
 *
 * The hour a window ends is the hour AFTER the last one it holds: `2-4` opens
 * at 02:00 and closes at 05:00, because the matcher lets a job start at any
 * minute of hour 4. The same reading as `WeekPainter`'s own sentence, so the
 * list and the editor say the same thing about the same value.
 */
export function windowLabel(cron: string): string | null {
  const window = cronToWindow(cron);
  if (!window || window.hours.length === 0 || window.days.length === 0) return null;

  const rows = WEEK.filter((day) => window.days.includes(day));
  let days: string;
  if (rows.length === 7) {
    days = translate("deploy.cron.everyDay");
  } else if (rows.length === 5 && [1, 2, 3, 4, 5].every((day) => window.days.includes(day))) {
    days = translate("deploy.cron.weekdays");
  } else if (rows.length === 2 && window.days.includes(0) && window.days.includes(6)) {
    days = translate("deploy.cron.weekends");
  } else {
    const first = WEEK.indexOf(rows[0] as (typeof WEEK)[number]);
    const consecutive = rows.every((day, at) => day === WEEK[first + at]);
    days =
      rows.length > 2 && consecutive
        ? translate("deploy.cron.dayRange", {
            from: dayShort(rows[0] as number),
            to: dayShort(rows[rows.length - 1] as number),
          })
        : rows.map((day) => dayShort(day)).join(", ");
  }

  const hours =
    window.hours.length === 24
      ? translate("deploy.cron.allDay")
      : runs(window.hours)
          .map(([from, to]) => translate("deploy.cron.hourRange", { from: hh(from), to: hh(to + 1) }))
          .join(translate("deploy.cron.and"));

  // Day names are lower case mid-sentence in Spanish; the label opens a cell,
  // so its first letter is capitalised (English already is).
  const sentence = translate("deploy.cron.sentence", { days, hours });
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}
