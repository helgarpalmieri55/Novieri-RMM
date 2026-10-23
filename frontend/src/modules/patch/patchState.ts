import { translate, type MessageKey } from "../../i18n/core";
import type { BadgeVariant } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";

/**
 * SEVERITY AS A RAMP, AND ONLY ONE RUNG IS RED.
 *
 * `critical` and `important` both painted `destructive`, so the two severities
 * this whole page is built to separate — the default filter is literally
 * "critical & important" — rendered as the same red pill, in the Updates
 * table, in the Worst severity column and on the device drill-down. DESIGN.md's
 * Rationed Hue Rule: a colour on this page must be carrying information, and a
 * hue covering half the ramp is not.
 *
 * IT IS ALSO THE ONLY MODULE THAT DID THIS. `health` maps critical ->
 * destructive, warning -> warning, everything else -> info; `bcp` maps
 * critical/high/medium/low onto destructive/warning/info/muted; `ipam` the
 * same shape. Patch was the outlier, so this is not a new scheme, it is the
 * console's existing one applied to the module that had drifted off it.
 *
 * `unknown` FALLS THROUGH TO GREY DELIBERATELY. Windows Update leaves most
 * cumulative updates unrated (`winupdate.go`), so `unknown` is the single
 * commonest value on a Windows fleet — and it means "nobody said", which is
 * exactly what grey means everywhere else in this product.
 */
const SEVERITY_VARIANTS: Record<string, BadgeVariant> = {
  critical: "destructive",
  important: "warning",
  high: "warning",
  moderate: "info",
  medium: "info",
  low: "muted",
  informational: "muted",
};

export function severityVariant(severity: string): BadgeVariant {
  return SEVERITY_VARIANTS[severity.toLowerCase()] ?? "muted";
}

// DevicePatchStatus.state vocabulary exactly as written by the backend:
// missing (approve.py:88), approved (approve.py:93), installed
// (apply.py:292-293, ingest.py:187), failed (apply.py:298), not_applicable
// (ingest.py:235). No other state values are ever persisted.
const STATE_VARIANTS: Record<string, BadgeVariant> = {
  missing: "warning",
  approved: "info",
  installed: "success",
  failed: "destructive",
  not_applicable: "muted",
};

export function patchStateVariant(state: string): BadgeVariant {
  return STATE_VARIANTS[state] ?? "muted";
}

/**
 * A patch state as a person reads it.
 *
 * Auditor A, [MEDIUM], /devices/:id → Patch tab: "exposes the raw enum value
 * `not_applicable` as a user-facing badge label". The vocabulary above is the
 * backend's, and it stays the backend's -- this maps it for display only, at
 * the two places a state reaches a screen (the device tab's "By state" card
 * and the fleet exposures table).
 *
 * An UNKNOWN state falls back to the raw value rather than to "—": a state the
 * server started writing and this table has not learned yet is information,
 * and hiding it would make a new backend state invisible in the console.
 */
const STATE_LABELS: Record<string, MessageKey> = {
  missing: "patch.state.missing",
  approved: "patch.state.approved",
  installed: "patch.state.installed",
  failed: "patch.state.failed",
  not_applicable: "patch.state.not_applicable",
};

export function patchStateLabel(state: string): string {
  const key = STATE_LABELS[state];
  return key ? translate(key) : state;
}

/**
 * THE SAME TWO RAMPS, AS v3 KINDS.
 *
 * The badge variants above are the previous design's vocabulary and stay for
 * the tests and the device tab that read them. The v3 port draws a pill with
 * its kind's glyph (`StatusPill`, `Td gutter`), and the kind is the same
 * decision the variant was: critical is the one red rung, important the
 * amber one, moderate plain information, low and unrated grey. One table,
 * two spellings, so the two can never disagree about which rung is red.
 */
const VARIANT_KIND: Record<BadgeVariant, Kind> = {
  destructive: "crit",
  warning: "warn",
  info: "info",
  success: "ok",
  muted: "mut",
};

export function severityKind(severity: string): Kind {
  return VARIANT_KIND[severityVariant(severity)];
}

export function patchStateKind(state: string): Kind {
  return VARIANT_KIND[patchStateVariant(state)];
}

/**
 * The severity as a person reads it in a pill: "Critical", "Important". The
 * wire value is lowercase and the test suite reads it that way from the
 * rows' data, so this is display only, like `patchStateLabel`.
 */
const SEVERITY_LABELS: Record<string, MessageKey> = {
  critical: "patch.severity.critical",
  important: "patch.severity.important",
  moderate: "patch.severity.moderate",
  low: "patch.severity.low",
  unknown: "patch.severity.unknown",
  high: "patch.severity.high",
  medium: "patch.severity.medium",
  informational: "patch.severity.informational",
};

export function severityLabel(severity: string): string {
  const key = SEVERITY_LABELS[severity];
  if (key) return translate(key);
  // A severity this console has not been taught reads as sent, capitalised.
  return severity ? severity.charAt(0).toUpperCase() + severity.slice(1) : severity;
}
