/**
 * WHAT THE TOP BAR'S HEALTH GLYPH IS ALLOWED TO SAY, and where each colour
 * comes from.
 *
 * THE DEFECT THIS REPLACES, in two halves that had the same root. The top bar
 * rendered `<Icon name="warning">` — an alert triangle, in one colour, on
 * every route — beside a `.counter-dot` fed from `AppShell`'s `badges` prop.
 * `grep -rn 'badges=' src` found no caller outside the tests and the preview
 * page, so `alertCount` was permanently 0: the dot had never rendered on a
 * deployed console, and neither had the rail's /health badge. A triangle that
 * cannot change and a count that is always zero are not a status indicator;
 * they are a picture of one.
 *
 * THE THREE STATES, and what each is computed from:
 *
 *   RED    a dependency the whole control plane stands on is DOWN.
 *          `GET /platform/status` -> `overall`, gated `platform.status.read`.
 *          This outranks everything: open alerts on a fleet the console cannot
 *          reliably talk to are the smaller of the two problems.
 *   AMBER  open health alerts, or a DEGRADED dependency.
 *          `GET /health/alerts/summary` -> `open`, gated `health.read`.
 *   GREEN  the count was taken, it was zero, and nothing above fired.
 *
 * AND A FOURTH, WHICH IS THE ONE THAT MATTERS MOST. `unknown` — nobody
 * measured this. A console that paints green because a query 403'd is telling
 * an operator that a fleet it never asked about is fine, which is the single
 * most dangerous sentence this chrome can produce (`AlertConsole` carries the
 * same finding about `alerts.data ?? []`). Green is a claim; it is made only
 * from a real count.
 *
 * WHY `open` IS NOT DEFINED HERE, AND NO LONGER DECIDED HERE EITHER. An
 * acknowledged alert is owned and STILL OPEN, and a muted one is silenced and
 * STILL OPEN. That rule must have exactly one home; it used to have two, and
 * this file held the browser's copy — it fetched a page of alerts and applied
 * `healthState.ts::isAlertOpen` to it. The server now applies the predicate in
 * SQL and returns the count, so the rule lives where the rows do.
 * `healthState.ts` keeps it for the alert console, which renders per-row state
 * and genuinely needs it.
 */

import { useQuery } from "@tanstack/react-query";

import type { ApiError } from "../lib/api";
import { translate } from "../i18n/core";
import { useApi } from "../lib/ApiContext";
import type { AlertSummary } from "../modules/health/types";
import type { PlatformStatus, Status } from "../modules/platform-status/types";

export type HealthSignal = "unknown" | "ok" | "alerts" | "down";

export interface HealthReading {
  /**
   * Open alerts across the whole org — or `null`, which means NOBODY COUNTED,
   * not zero.
   *
   * A TOTAL, NOT A FLOOR, and that is new. See `ALERT_SUMMARY` below for what
   * this used to be and why it had to carry an `atLeast` flag beside it.
   */
  openAlerts: number | null;
  /** The dependency rollup, or `null` when the operator cannot read it. */
  platform: Status | null;
}

/** Nothing measured. The honest default, and what `AppShell` falls back to. */
export const NOTHING_MEASURED: HealthReading = {
  openAlerts: null,
  platform: null,
};

/**
 * THE ORDER OF THESE BRANCHES IS THE RULE, and each one is a decision:
 *
 *   1. `down` wins outright, even over open alerts, and even when the alert
 *      count was never taken — a dependency being down is knowable on its own.
 *   2. Open alerts outrank `degraded`: a degraded dependency is a platform
 *      concern, and an operator's fleet is the thing they came here for.
 *   3. `degraded` is amber, not green. It is a dependency answering badly.
 *   4. No count means UNKNOWN, never `ok`. Green requires a measurement.
 */
export function healthSignal(reading: HealthReading): HealthSignal {
  if (reading.platform === "down") return "down";
  if (reading.openAlerts !== null && reading.openAlerts > 0) return "alerts";
  if (reading.platform === "degraded") return "alerts";
  if (reading.openAlerts === null) return "unknown";
  return "ok";
}

/**
 * The accessible name, which STATES THE CONDITION IN WORDS.
 *
 * SC 1.4.1: colour may not be the only way information is conveyed. A glyph
 * that is green, amber or red and named "Health and alerts" in all three
 * carries its entire meaning in a hue that roughly one man in twelve cannot
 * separate reliably — and green-vs-amber-vs-red is the worst possible trio for
 * exactly that. The pip's shape (`shell.css`) is the second carrier; this is
 * the first.
 *
 * BOTH FACTS WHEN BOTH ARE KNOWN. A dependency being down does not make the
 * open alerts stop existing, so the name carries the platform clause and the
 * count, in that order.
 */
export function healthLabel(reading: HealthReading): string {
  const parts: string[] = [];
  if (reading.platform === "down") parts.push(translate("shell.health.platformDown"));
  else if (reading.platform === "degraded") parts.push(translate("shell.health.platformDegraded"));
  if (reading.openAlerts === 0) parts.push(translate("shell.health.noOpenAlerts"));
  // NO LONGER HEDGED. This used to read "at least 40 needing attention",
  // because the count was one page of the alert list filtered in the browser
  // and a full page is a floor. The server counts the whole org now, so the
  // hedge would be false modesty about an exact number — and a badge that
  // hedges a number it actually knows teaches an operator to discount it.
  else if (reading.openAlerts !== null) {
    parts.push(translate("shell.health.needingAttention", { count: reading.openAlerts }));
  }
  return parts.length > 0
    ? translate("shell.health.withParts", { parts: parts.join(", ") })
    : translate("shell.health.base");
}

/**
 * THE PLATFORM ROLLUP ON ITS OWN, for the glyph whose destination is
 * `/platform-status`.
 *
 * WHY THIS EXISTS AND `healthSignal` ABOVE IS UNCHANGED. Red comes from
 * `GET /platform/status` (`platform.status.read`); amber from open alerts
 * (`health.read`). The top bar had ONE glyph for both, a `<Link to="/health">`
 * gated `health.read` because that is what its destination requires — so an
 * operator holding platform-status and not health saw no glyph at all, and a
 * dependency being DOWN, the most severe thing this console can report, was
 * invisible to them on every page.
 *
 * THE FIX IS NOT A WIDER GATE. `health.read || platform.status.read` on that
 * link points the control at a route the second operator gets a 403 from,
 * which is a false affordance rather than a fix. Platform status earns its own
 * affordance with its own destination, and this is the rule behind it — beside
 * `healthSignal`, not instead of it, so there is still exactly one place where
 * a colour on this console's chrome is decided.
 *
 * THE SAME FOUR-STATE VOCABULARY, deliberately, so both glyphs are painted by
 * one set of rules in `shell.css` (`.top-icon-btn.health-ok` and friends) and
 * a state cannot get a colour here that means something else two icons away.
 * `degraded` folds into `alerts` for that reason: it is amber on both.
 *
 * AND `null` IS `unknown`, WHICH IS THE POINT. A refused or failed
 * `/platform/status` must never paint green — green is a claim, and a console
 * that makes it from a 403 is telling an operator that dependencies it never
 * asked about are fine.
 */
export function platformSignal(platform: Status | null): HealthSignal {
  if (platform === "down") return "down";
  if (platform === "degraded") return "alerts";
  if (platform === "ok") return "ok";
  return "unknown";
}

/**
 * The accessible name, which STATES THE CONDITION IN WORDS (SC 1.4.1).
 *
 * Colour may not be the only carrier, and red-amber-green is the worst
 * possible trio for it. The bare "Platform status" is the unmeasured state and
 * is a DIFFERENT string from every measured one, which is what keeps "nobody
 * asked" separable from "all clear" once the hue is gone — the same shape
 * `healthLabel` uses for the same reason.
 */
export function platformLabel(platform: Status | null): string {
  if (platform === "down") return translate("shell.health.platformStatusDown");
  if (platform === "degraded") return translate("shell.health.platformStatusDegraded");
  if (platform === "ok") return translate("shell.health.platformStatusOk");
  return translate("shell.health.platformStatus");
}

/**
 * WHERE THE COUNT COMES FROM, AND WHAT IT USED TO COME FROM.
 *
 * THE DEFECT. The health module exposed the alert LIST and nothing that
 * summarised it, and the list's `state` filter is single-valued — so "open"
 * (active OR acknowledged OR muted, spec §0.8) could not be expressed
 * server-side at all. This hook therefore fetched ONE PAGE of
 * `GET /health/alerts` — the route's own default, `limit: int =
 * Query(default=200, ge=1, le=500)` — and filtered it here through the health
 * module's `isAlertOpen`. On a fleet with more than a page of open alerts that
 * count is a FLOOR: the page may not contain every open one, and nothing in
 * the response says so. The old code was honest about it and rendered "at
 * least 40 needing attention", which is the right thing to say about a floor
 * and the wrong thing for a badge to say at all — an operator reads a badge as
 * a total.
 *
 * WHAT REPLACED IT. `GET /health/alerts/summary`, gated `health.read` like the
 * list, counting the whole org with one `GROUP BY (state, severity)` in
 * Postgres. Nothing about "open" is decided in the browser any more: the
 * server owns the predicate, which is where a rule with one home belongs.
 *
 * WHAT ELSE IT RETURNS, AND WHY THE GLYPH DOES NOT SPEND IT. The summary also
 * carries `by_state`, `by_severity` and `worst_severity`. The hue rule above
 * deliberately does not read them: RED means a dependency the whole control
 * plane stands on is DOWN, and promoting a critical alert to red would be a
 * change to what red means on every page of the console rather than a
 * refinement of this hook. That is a product decision, and it is not made
 * here as a side effect of the count getting better.
 */
export const ALERT_SUMMARY = "/health/alerts/summary";

/**
 * The shell's poll, deliberately slower than the alert console's 30 s.
 *
 * The alert sweep runs every 60 s server-side, so nothing faster adds
 * freshness — and this one runs on EVERY page for every session, where the
 * console's runs on one page while somebody is looking at it.
 */
export const SHELL_POLL_MS = 60_000;

/**
 * The two reads behind the glyph, each GATED ON THE PERMISSION ITS ROUTE
 * REQUIRES.
 *
 * `enabled` is not an optimisation. Without it a technician holding neither
 * code fires two 403s on every page load, forever, from chrome that renders on
 * every route — the console generating its own audit noise to draw an icon it
 * is not going to show them.
 *
 * `["health-alerts", ...]` DELIBERATELY SHARES THE MODULE'S KEY PREFIX, so
 * that acknowledging, muting or closing an alert — every one of which
 * invalidates `["health-alerts"]` (`modules/health/api.ts`) — refreshes this
 * badge too. The glyph cannot sit on a count the operator has just changed.
 * `["platform-status"]` is the SAME key `PlatformStatusPage` uses, so a
 * session on that page shares one cached answer rather than issuing two.
 */
export function useHealthReading(permissions: readonly string[]): HealthReading {
  const api = useApi();

  const alerts = useQuery<AlertSummary, ApiError>({
    queryKey: ["health-alerts", "shell-signal"],
    queryFn: () => api.getHealthAlertSummary(),
    enabled: permissions.includes("health.read"),
    refetchInterval: SHELL_POLL_MS,
    // A 403 settles rather than retrying against the same permission wall.
    retry: false,
  });

  const platform = useQuery<PlatformStatus, ApiError>({
    queryKey: ["platform-status"],
    queryFn: () => api.getPlatformStatus(),
    enabled: permissions.includes("platform.status.read"),
    refetchInterval: SHELL_POLL_MS,
    retry: false,
  });

  return {
    // `?? null` AND NOT `|| null`. A summary that answers `open: 0` is a
    // MEASUREMENT and must arrive as 0 so the glyph goes green; `undefined` --
    // not yet answered, or refused -- must arrive as null so it stays grey.
    // `||` collapses those two into the same value and turns a healthy fleet
    // into "nobody measured this".
    openAlerts: alerts.data?.open ?? null,
    platform: platform.data?.overall ?? null,
  };
}
