/**
 * Dates and times, in one place, in the zone the user actually works in.
 *
 * WHY THIS EXISTS (UX remediation cycle 5). Auditor A: "Times are shifted by 5
 * hours between entry and display — what you type is not what you see."
 * Auditor B reproduced it in a third module and filed it as a BLOCKER: "every
 * datetime in the product is displayed 5 hours earlier than it was entered."
 *
 * FIVE IS THE OPERATOR'S OFFSET, and naming the mechanism matters because the
 * fix is on the side people look at least. `<input type="datetime-local">`
 * hands back a WALL CLOCK with no zone -- "2026-08-20T14:00". Sent unqualified
 * to an API that reads it as UTC, it records 14:00Z for a moment that was
 * really 19:00Z; rendered back through the browser's local zone it reads
 * 09:00. The product loses five hours on the way in and reports the loss
 * confidently on the way out.
 *
 * So there are two functions and both are needed: `toInstant` attaches the
 * zone on the way in, and `formatDateTime` is the single way out. Seventy
 * ad-hoc `new Date(x).toLocaleString()` call sites are what produced "the same
 * device reports two different last-contact times in two different date
 * formats" -- one function is the fix for that finding as well.
 *
 * THE ZONE COMES FROM THE PROFILE, not from the browser. `/me` carries
 * `timezone` (cycle 2), so a technician who sets it sees consistent times
 * across the machines they work from, and two people reading one ticket can
 * each say which zone they are reading it in.
 */

import { getLanguage, translate } from "../i18n/core";

/**
 * The `Intl` locale for the active language. English keeps `en-GB` exactly as
 * before; Spanish uses `es`. Every formatter and every locale-aware sort in
 * the console reads it from here, so one language switch moves them all.
 */
export function intlLocale(): "en-GB" | "es" {
  return getLanguage() === "es" ? "es" : "en-GB";
}

/** Shown where a date is absent. An em dash, not "Invalid Date", not "null". */
export const NO_DATE = "—";

/**
 * The offset of `zone` at `instant`, in minutes.
 *
 * DERIVED PER INSTANT rather than taken once, because an offset is not a
 * property of a zone: America/New_York is -5 in January and -4 in July. A
 * cached offset is correct for half the year, which is the kind of bug that
 * surfaces as "the scheduler runs an hour early, but only in summer".
 */
function offsetMinutes(instant: Date, zone: string): number {
  // `en-US` with an explicit `timeZone` gives the wall-clock parts in that
  // zone; treating them as UTC and differencing yields the offset.
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).formatToParts(instant);
  const at = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  const asUtc = Date.UTC(
    at("year"),
    at("month") - 1,
    at("day"),
    at("hour") % 24,
    at("minute"),
    at("second"),
  );
  return (asUtc - instant.getTime()) / 60000;
}

/**
 * A `datetime-local` field value as a real instant.
 *
 * Returns "" for an empty field: an untouched optional input must not become
 * the epoch or the string "Invalid Date", both of which are values the user
 * never entered and the server would happily store.
 */
export function toInstant(localValue: string, zone?: string | null): string {
  const value = localValue.trim();
  if (!value) return "";
  // Already qualified -- a value from the API, or a field that carries a zone.
  if (/(Z|[+-]\d{2}:?\d{2})$/.test(value)) {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? "" : parsed.toISOString();
  }

  const tz = zone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  // Read the wall clock AS IF UTC, then correct by the zone's offset at that
  // moment. Two passes: the offset can differ either side of a DST boundary,
  // and the second pass is measured at the corrected instant.
  const naive = new Date(`${value}${value.length === 16 ? ":00" : ""}Z`);
  if (Number.isNaN(naive.getTime())) return "";
  const first = new Date(naive.getTime() - offsetMinutes(naive, tz) * 60000);
  const corrected = new Date(naive.getTime() - offsetMinutes(first, tz) * 60000);
  return corrected.toISOString();
}

function resolve(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** One datetime format for the whole product. */
export function formatDateTime(iso: string | null | undefined, zone?: string | null): string {
  const at = resolve(iso);
  if (!at) return NO_DATE;
  return new Intl.DateTimeFormat(intlLocale(), {
    timeZone: zone || undefined,
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(at);
}

/**
 * "5 minutes ago", "in 3 days": an instant relative to `now`, in words.
 *
 * The console's one relative form (review 2026-09-23: timestamps were
 * absolute in some places and relative in others, sometimes on one page).
 * Past two months it falls back to the date, where "in 58 days" makes the
 * reader do arithmetic again. Read beside `formatDateTime` on hover.
 */
export function describeWhen(
  iso: string | null | undefined,
  now: number = Date.now(),
  zone?: string | null,
): string {
  const at = resolve(iso);
  if (!at) return NO_DATE;
  const delta = at.getTime() - now;
  const seconds = Math.abs(delta) / 1000;
  // Clock skew between server and browser must not read as the future.
  if (seconds < 45) return translate("time.justNow");
  const say = (count: number, unit: "minute" | "hour" | "day") =>
    translate(`time.${delta < 0 ? "ago" : "in"}.${unit}`, { count });
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return say(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (hours < 36) return say(hours, "hour");
  const days = Math.round(hours / 24);
  if (days < 60) return say(days, "day");
  return formatDate(iso, zone);
}

/** The date alone, in the same zone -- a date rendered in the wrong zone is
 *  off by a whole day, which is harder to notice than five hours. */
export function formatDate(iso: string | null | undefined, zone?: string | null): string {
  const at = resolve(iso);
  if (!at) return NO_DATE;
  return new Intl.DateTimeFormat(intlLocale(), {
    timeZone: zone || undefined,
    year: "numeric",
    month: "short",
    day: "2-digit",
  }).format(at);
}
