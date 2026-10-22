/**
 * Number formatting for values that arrive from the wire as raw quantities.
 *
 * WHY THIS IS SHARED. The audit found the same byte count rendered two ways on
 * ONE page: the Memory stat card read "15.0 GiB" while the hardware table
 * printed `{"size_bytes":256022937600}` verbatim, in the same column as 34
 * other unparsed JSON cells. Its note is the argument for this file --
 * "proving the formatter exists and simply is not applied here". A formatter
 * that lives inside the component that first needed it is a formatter the next
 * component will not find.
 */

import { translate } from "../i18n/core";
import { intlLocale } from "./datetime";

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] as const;

/**
 * A byte count as a person reads it: `256022937600` -> "238 GiB",
 * `16093573120` -> "15.0 GiB".
 *
 * One decimal below 100 and none at or above it. That boundary is not taste:
 * the audit quotes the Memory stat card already rendering 16093573120 as
 * "15.0 GiB" and asks the hardware table beside it to match, and it quotes
 * 256022937600 as "238 GiB". Any rule that keeps a decimal at 15 and drops it
 * at 238 satisfies both; this is the simplest one. A rule that drops the
 * decimal at 10 -- which the backup module briefly shipped -- renders "15 GiB"
 * and contradicts the card it was meant to agree with.
 *
 * Binary units (KiB, not kB) because every source feeding this --
 * `/proc/meminfo`, `lsblk`, WMI, `statfs` -- reports powers of two, and
 * relabelling them as decimal would be a 7% lie at TiB scale.
 *
 * NULL IS NOT ZERO, and neither is a negative or a NaN. An unmeasured size and
 * an empty backup point are different facts, so anything unmeasurable returns
 * the empty string and the CALLER renders `<NoValue>` with a sentence naming
 * what is missing. A formatter that returns an em dash has decided how the
 * absence looks, in a file that cannot see the column.
 */
export function formatBytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return "";
  const negative = value < 0;
  let size = Math.abs(value);
  let unit = 0;
  while (size >= 1024 && unit < BYTE_UNITS.length - 1) {
    size /= 1024;
    unit += 1;
  }
  // Bytes are whole things; a third of a byte is not a measurement.
  const decimals = unit === 0 || size >= 100 ? 0 : 1;
  return `${negative ? "-" : ""}${size.toFixed(decimals)} ${BYTE_UNITS[unit]}`;
}

/**
 * A count with thousands separators, in the viewer's locale.
 *
 * Deliberately locale-aware where `formatBytes` is not: a European operator
 * expects `1.234`, and a byte unit is a unit rather than a number.
 */
export function formatCount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "";
  return value.toLocaleString(intlLocale());
}

/** "90 seconds", "15 minutes", "2 hours", "1 day": the largest unit that divides it. */
export function describeSeconds(seconds: number): string {
  const say = (count: number, unit: "second" | "minute" | "hour" | "day") =>
    translate(`time.units.${unit}`, { count });
  if (seconds >= 86400 && seconds % 86400 === 0) return say(seconds / 86400, "day");
  if (seconds >= 3600 && seconds % 3600 === 0) return say(seconds / 3600, "hour");
  if (seconds >= 60 && seconds % 60 === 0) return say(seconds / 60, "minute");
  return say(seconds, "second");
}
