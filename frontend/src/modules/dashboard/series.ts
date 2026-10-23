/**
 * The two derivations the dashboard's v3 pieces need and no endpoint serves.
 *
 * NOTHING HERE INVENTS A NUMBER. Both functions take rows the page already
 * holds and count them; the caller decides whether the rows are complete
 * enough for the count to mean anything (`DashboardPage` draws a sparkline
 * only over a page that is the whole list, and a heat strip only with its
 * scope stated).
 */

import type { Device } from "../../lib/api";
import type { Kind } from "../../shell/viz";

const DAY_MS = 86_400_000;

/**
 * How many of `timestamps` fall on each of the last `days` UTC days, oldest
 * day first. An unparseable or absent timestamp counts nowhere; one older
 * than the window is dropped rather than piled onto the first day.
 */
export function dailySeries(
  timestamps: readonly (string | null | undefined)[],
  days: number,
  now: number,
): number[] {
  const counts = new Array<number>(days).fill(0);
  const today = Math.floor(now / DAY_MS);
  for (const iso of timestamps) {
    if (!iso) continue;
    const at = Date.parse(iso);
    if (Number.isNaN(at)) continue;
    const back = today - Math.floor(at / DAY_MS);
    if (back < 0 || back >= days) continue;
    counts[days - 1 - back] = (counts[days - 1 - back] ?? 0) + 1;
  }
  return counts;
}

/** True when a series has anything in it worth drawing. */
export function hasSignal(series: readonly number[]): boolean {
  return series.some((value) => value > 0);
}

/**
 * The v3 "Fleet at a glance" reading of one device row: what needs a person
 * (offline, or an identity that has lapsed), what is degraded (an identity
 * about to lapse), what the platform is working on (a renewal in flight),
 * what carries no agent at all, and what is healthy.
 */
export function deviceKind(
  device: Pick<Device, "agent_status" | "cert_state" | "cert_revoked">,
): Kind {
  if (device.agent_status === null) return "mut";
  if (device.agent_status === "offline" || device.cert_revoked || device.cert_state === "expired") {
    return "crit";
  }
  if (device.cert_state === "expiring") return "warn";
  if (device.cert_state === "renewing") return "run";
  return "ok";
}
