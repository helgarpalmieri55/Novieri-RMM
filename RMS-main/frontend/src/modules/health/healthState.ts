// Pure presentation logic for the health module. Everything here is a DISPLAY
// decision over values the server already decided; nothing here decides whether
// a device is healthy, whether an alert is open, or whether a remediation may
// run.
//
// THE FOUR RULES THIS FILE EXISTS TO KEEP. Each is a distinction that a
// plausible renderer collapses, and each collapse is a wrong answer of the most
// dangerous kind -- one that reads as good news:
//
//   1. AN ACKNOWLEDGED ALERT IS NOT A CLEARED ONE (spec §0.8). Acknowledging
//      does not stop the check, does not prevent auto-clear and does not cancel
//      a remediation in flight; it suppresses re-notification and escalation
//      and nothing else. It is OPEN.
//   2. A MUTED ALERT IS NOT A RESOLVED ONE. It is open, silenced, and
//      time-boxed -- and the box matters, because unbounded mutes are how a
//      fleet accumulates permanently silent alarms everyone believes are armed.
//   3. `unavailable` IS NEITHER `ok` NOR `breaching` (spec §0.7). A check the
//      device could not answer opened nothing and cleared nothing. Neither is
//      `unknown`, which is a check the device has never answered at all.
//   4. AN OFFLINE DEVICE IS NOT A HEALTHY DEVICE (spec §0.6). Its alerts do not
//      clear when it goes quiet, so "no new alerts" from a silent machine is
//      not evidence of anything.
//
// `openAlertBands` and the four `*Presentation` maps below are the single place
// each rule is expressed, and `__tests__/healthState.test.ts` asserts the
// distinctions rather than the labels -- an assertion over the keys of a map
// defined in the same file cannot fail.

import { translate, type MessageKey } from "../../i18n/core";
import { lazyFields, lazyLabels } from "../../i18n/lazy";
import { intlLocale } from "../../lib/datetime";
import type { BadgeVariant } from "../jobs/jobState";
import type {
  ActionKind,
  ActionState,
  AlertActionOut,
  AlertOut,
  AlertState,
  Autonomy,
  CheckOut,
  ClearedReason,
  Comparator,
  DeviceCheckState,
  DeviceHealthOut,
  HealthSeverity,
} from "./types";

// --------------------------------------------------------------------------
// Alert state
// --------------------------------------------------------------------------

export interface StatePresentation {
  label: string;
  variant: BadgeVariant;
  /** Is this alert still an open incident? */
  open: boolean;
  /** One line explaining what the state does and does not mean. */
  meaning: string;
}

// The worded fields (`label`, `meaning`) are accessors over the catalogue
// (`lazyFields`): read when shown, so a language switch re-words them; the
// variant and the flags are plain data.
export const ALERT_STATE_PRESENTATION: Readonly<Record<AlertState, StatePresentation>> = {
  active: lazyFields(
    { variant: "destructive" as const, open: true },
    { label: "health.alertState.active.label", meaning: "health.alertState.active.meaning" },
  ),
  acknowledged: lazyFields(
    { variant: "warning" as const, open: true },
    { label: "health.alertState.acknowledged.label", meaning: "health.alertState.acknowledged.meaning" },
  ),
  muted: lazyFields(
    { variant: "warning" as const, open: true },
    { label: "health.alertState.muted.label", meaning: "health.alertState.muted.meaning" },
  ),
  cleared: lazyFields(
    { variant: "muted" as const, open: false },
    { label: "health.alertState.cleared.label", meaning: "health.alertState.cleared.meaning" },
  ),
};

export interface ClearedReasonPresentation {
  label: string;
  /**
   * Does this clear belong in a RESOLUTION figure at all?
   *
   * `false` for the three non-events spec §7.4 excludes: the sensor went away,
   * the check was deleted, the device left the fleet. Counting any of them is
   * how "we fixed 40 things this week" comes to mean "40 sensors disappeared".
   */
  countsAsResolution: boolean;
  /**
   * Did a MACHINE resolve it, as opposed to a person deciding to stop looking?
   * Only meaningful when `countsAsResolution` is true.
   */
  machineResolved: boolean;
  meaning: string;
}

export const CLEARED_REASON_PRESENTATION: Readonly<
  Record<ClearedReason, ClearedReasonPresentation>
> = {
  recovery: lazyFields(
    { countsAsResolution: true, machineResolved: true },
    { label: "health.cleared.recovery.label", meaning: "health.cleared.recovery.meaning" },
  ),
  summary_ok: lazyFields(
    { countsAsResolution: true, machineResolved: true },
    { label: "health.cleared.summary_ok.label", meaning: "health.cleared.summary_ok.meaning" },
  ),
  unavailable_timeout: lazyFields(
    // NOT a resolution of any kind (spec §7.4): an alert cleared because the
    // sensor was removed is not a fixed problem.
    { countsAsResolution: false, machineResolved: false },
    {
      label: "health.cleared.unavailable_timeout.label",
      meaning: "health.cleared.unavailable_timeout.meaning",
    },
  ),
  policy_removed: lazyFields(
    { countsAsResolution: false, machineResolved: false },
    { label: "health.cleared.policy_removed.label", meaning: "health.cleared.policy_removed.meaning" },
  ),
  device_inactive: lazyFields(
    { countsAsResolution: false, machineResolved: false },
    { label: "health.cleared.device_inactive.label", meaning: "health.cleared.device_inactive.meaning" },
  ),
  manual: lazyFields(
    // A human close IS a resolution of the incident, but it is not evidence
    // that a machine fixed anything -- which is exactly why the two are
    // counted apart.
    { countsAsResolution: true, machineResolved: false },
    { label: "health.cleared.manual.label", meaning: "health.cleared.manual.meaning" },
  ),
};

export function isAlertOpen(alert: Pick<AlertOut, "state">): boolean {
  return ALERT_STATE_PRESENTATION[alert.state].open;
}

/**
 * The one-line status a console row shows, with `state` and `cleared_reason`
 * kept APART.
 *
 * A renderer that showed only the state would make "cleared because it
 * recovered" and "cleared because someone deleted the check" identical; one
 * that showed only the reason would make an acknowledged alert look unhandled.
 */
export function describeAlertStatus(alert: Pick<AlertOut, "state" | "cleared_reason">): string {
  const state = ALERT_STATE_PRESENTATION[alert.state];
  if (alert.state !== "cleared") return state.label;
  const reason = alert.cleared_reason ? CLEARED_REASON_PRESENTATION[alert.cleared_reason] : null;
  return reason ? translate("health.statusCleared", { state: state.label, reason: reason.label }) : state.label;
}

/**
 * Does this cleared alert belong in a RESOLUTION count?
 *
 * Spec §7.4's reporting obligation lives on this predicate: MTTR and
 * auto-remediation success rate must exclude `unavailable_timeout`, and the
 * same reasoning excludes `policy_removed` and `device_inactive` -- in all
 * three the alert went away because the QUESTION went away. Returns false for
 * an alert that is still open, because an open alert has resolved nothing.
 */
export function countsAsResolved(alert: Pick<AlertOut, "state" | "cleared_reason">): boolean {
  if (alert.state !== "cleared" || alert.cleared_reason === null) return false;
  return CLEARED_REASON_PRESENTATION[alert.cleared_reason].countsAsResolution;
}

/** Was it a MACHINE that resolved it? False for a human close, and for
 * anything that is not a resolution at all. */
export function isMachineResolved(alert: Pick<AlertOut, "state" | "cleared_reason">): boolean {
  if (!countsAsResolved(alert) || alert.cleared_reason === null) return false;
  return CLEARED_REASON_PRESENTATION[alert.cleared_reason].machineResolved;
}

// --------------------------------------------------------------------------
// Severity
// --------------------------------------------------------------------------

/**
 * A severity's LABEL as well as its variant, in one place.
 *
 * The variant was here and the label was not, so every call site rendered
 * `{alert.severity}` -- five of them -- and the console drew a lowercase wire
 * word ("critical", "warning", "info") in a pill sitting beside title-case
 * state pills ("Active", "Acknowledged"). It is the same defect
 * `lib/labels#humaniseEnum` exists for, and a severity cannot go through that
 * helper alone: "Info" is right, but so is the pair with it, and the label a
 * severity filter offers must be the label its rows carry.
 */
export interface SeverityPresentation {
  label: string;
  variant: BadgeVariant;
}

export const SEVERITY_PRESENTATION: Readonly<Record<HealthSeverity, SeverityPresentation>> = {
  critical: lazyFields({ variant: "destructive" as const }, { label: "health.severity.critical" }),
  warning: lazyFields({ variant: "warning" as const }, { label: "health.severity.warning" }),
  info: lazyFields({ variant: "info" as const }, { label: "health.severity.info" }),
};

export function severityVariant(severity: HealthSeverity): BadgeVariant {
  return SEVERITY_PRESENTATION[severity].variant;
}

export function severityLabel(severity: HealthSeverity): string {
  return SEVERITY_PRESENTATION[severity].label;
}

// --------------------------------------------------------------------------
// Thresholds and observations
// --------------------------------------------------------------------------

/**
 * A comparator as the words a person reads.
 *
 * `gt`, `lte` and `is` are the AGENT's alphabet (spec §5.2) and they were
 * reaching the screen verbatim: an alert row read "96 vs gt 95", a check row
 * read "gt 90", and an alert with no observed value read "vs is true" -- a
 * sentence beginning with its own operator. The comparator is still the thing
 * being said; only the alphabet changes here.
 */
const COMPARATOR_PHRASE: Readonly<Record<Comparator, string>> = lazyLabels<Comparator>({
  gt: "health.comparator.gt",
  gte: "health.comparator.gte",
  lt: "health.comparator.lt",
  lte: "health.comparator.lte",
  eq: "health.comparator.eq",
  neq: "health.comparator.neq",
  is: "health.comparator.is",
});

/**
 * The comparators whose threshold names WHAT WAS EXPECTED rather than a limit
 * the observation crossed. "96 over 95" is one clause; "stopped, expected
 * running" is two, and running them together ("stopped expected running")
 * reads as a typo.
 */
const EXPECTATION: ReadonlySet<Comparator> = new Set<Comparator>(["is", "eq", "neq"]);

/** A check's rule as a phrase: `gt` + `90` -> "over 90", `is` + `running` ->
 *  "expected running". */
export function describeThreshold(comparator: Comparator, display: string): string {
  const phrase = COMPARATOR_PHRASE[comparator];
  return display ? `${phrase} ${display}` : phrase;
}

const COMPARATORS: ReadonlySet<string> = new Set(Object.keys(COMPARATOR_PHRASE));

/**
 * What an alert measured, and against what, as one sentence.
 *
 * AN ALERT'S `threshold_display` CARRIES THE COMPARATOR IN IT -- the server
 * builds it as `f"{check.comparator} {rendered}"` (`health/ingest.py`), which
 * is why this parses rather than taking a comparator argument. A leading token
 * this file does not recognise is left alone and read as prose, so a server
 * that changes the format degrades to "96 against 90" rather than to nonsense.
 *
 * AN ALERT CAN HAVE NO OBSERVED VALUE AT ALL: an alert cleared by
 * `unavailable_timeout` never got one, and that reads "Nothing observed",
 * never as an empty half-sentence.
 */
export function describeObservation(
  alert: Pick<AlertOut, "observed_value" | "threshold_display">,
): string {
  const observed = alert.observed_value.trim();
  const threshold = alert.threshold_display.trim();
  if (threshold === "") return observed === "" ? translate("health.observation.nothing") : observed;

  const parts = threshold.split(/\s+/);
  const head = parts[0] ?? "";
  const comparator = COMPARATORS.has(head) ? (head as Comparator) : null;
  const phrase =
    comparator === null
      ? translate("health.observation.against", { threshold })
      : describeThreshold(comparator, parts.slice(1).join(" "));

  if (observed === "") return translate("health.observation.nothingWith", { phrase });
  return comparator !== null && EXPECTATION.has(comparator)
    ? `${observed}, ${phrase}`
    : `${observed} ${phrase}`;
}

// --------------------------------------------------------------------------
// Check state on a device
// --------------------------------------------------------------------------

export interface CheckStatePresentation {
  label: string;
  variant: BadgeVariant;
  meaning: string;
}

export const CHECK_STATE_PRESENTATION: Readonly<
  Record<DeviceCheckState, CheckStatePresentation>
> = {
  ok: lazyFields(
    { variant: "success" as const },
    { label: "health.checkState.ok.label", meaning: "health.checkState.ok.meaning" },
  ),
  breaching: lazyFields(
    { variant: "destructive" as const },
    { label: "health.checkState.breaching.label", meaning: "health.checkState.breaching.meaning" },
  ),
  unavailable: lazyFields(
    // Deliberately NOT `success` and NOT `destructive`. The check opened
    // nothing and cleared nothing; the device could not answer it (spec §0.7).
    { variant: "warning" as const },
    { label: "health.checkState.unavailable.label", meaning: "health.checkState.unavailable.meaning" },
  ),
  unknown: lazyFields(
    { variant: "muted" as const },
    { label: "health.checkState.unknown.label", meaning: "health.checkState.unknown.meaning" },
  ),
};

/**
 * The device-view header counts, as FOUR bands.
 *
 * Spec §13: "`unavailable` counts are shown as a distinct band, never folded
 * into 'healthy'". `unknown` gets its own band for the same reason -- a check
 * nobody has ever answered is not a passing check.
 */
export function checkStateBands(
  checks: ReadonlyArray<{ state: DeviceCheckState }>,
): Record<DeviceCheckState, number> {
  const bands: Record<DeviceCheckState, number> = {
    ok: 0,
    breaching: 0,
    unavailable: 0,
    unknown: 0,
  };
  for (const check of checks) bands[check.state] += 1;
  return bands;
}

/**
 * The device-level headline, which must never read "healthy" for a device that
 * is merely quiet or merely unmonitored.
 *
 * The order of the branches IS the rule: no profile outranks everything (a
 * device monitoring nothing cannot be healthy), an open alert outranks being
 * offline, and being offline outranks every remaining "all clear".
 */
export function describeDeviceHealth(device: DeviceHealthOut): {
  label: string;
  variant: BadgeVariant;
  detail: string;
} {
  if (!device.has_profile) {
    return {
      label: translate("health.device.noProfile.label"),
      variant: "warning",
      detail: translate("health.device.noProfile.detail"),
    };
  }
  const open = device.open_alerts.filter(isAlertOpen);
  if (open.length > 0) {
    return {
      label: translate("health.device.openAlerts", { count: open.length }),
      variant: open.some((alert) => alert.severity === "critical") ? "destructive" : "warning",
      detail: translate(device.agent_online ? "health.device.openOnline" : "health.device.openQuiet"),
    };
  }
  if (!device.agent_online) {
    return {
      label: translate("health.device.offline.label"),
      variant: "warning",
      detail: translate("health.device.offline.detail"),
    };
  }
  const bands = checkStateBands(device.checks);
  if (bands.unavailable > 0 || bands.unknown > 0) {
    return {
      label: translate("health.device.partial"),
      variant: "warning",
      detail: translate("health.device.partialDetail", {
        unavailable: bands.unavailable,
        unknown: bands.unknown,
        count: bands.unavailable,
      }),
    };
  }
  if (device.checks.length === 0) {
    return {
      label: translate("health.device.noChecks.label"),
      variant: "warning",
      detail: translate("health.device.noChecks.detail"),
    };
  }
  return {
    label: translate("health.device.healthy.label"),
    variant: "success",
    detail: translate("health.device.healthy.detail"),
  };
}

// --------------------------------------------------------------------------
// Actions and remediation
// --------------------------------------------------------------------------

/** What each action kind does, as its heading. "remediate" is the engine's word; "fix" is the operator's. */
export const ACTION_KIND_LABEL: Readonly<Record<ActionKind, string>> = lazyLabels<ActionKind>({
  remediate: "health.actionKind.remediate",
  open_ticket: "health.actionKind.open_ticket",
  notify: "health.actionKind.notify",
});

/**
 * Why an action is paused because its alert keeps repeating, in words. The
 * server's own sentence ("actions suppressed: this key opened 4 times inside
 * the flap window...") is the engine talking.
 */
export function describeRepeatPause(flapCount: number): string {
  return flapCount > 1
    ? translate("health.repeatPause.many", { count: flapCount })
    : translate("health.repeatPause.several");
}

export const ACTION_STATE_PRESENTATION: Readonly<
  Record<ActionState, { label: string; variant: BadgeVariant; meaning: string }>
> = {
  pending: lazyFields(
    { variant: "info" as const },
    { label: "health.actionState.pending.label", meaning: "health.actionState.pending.meaning" },
  ),
  awaiting_approval: lazyFields(
    { variant: "warning" as const },
    {
      label: "health.actionState.awaiting_approval.label",
      meaning: "health.actionState.awaiting_approval.meaning",
    },
  ),
  dispatched: lazyFields(
    { variant: "info" as const },
    { label: "health.actionState.dispatched.label", meaning: "health.actionState.dispatched.meaning" },
  ),
  done: lazyFields(
    { variant: "success" as const },
    { label: "health.actionState.done.label", meaning: "health.actionState.done.meaning" },
  ),
  skipped: lazyFields(
    { variant: "muted" as const },
    { label: "health.actionState.skipped.label", meaning: "health.actionState.skipped.meaning" },
  ),
  blocked_rate_limit: lazyFields(
    { variant: "destructive" as const },
    {
      label: "health.actionState.blocked_rate_limit.label",
      meaning: "health.actionState.blocked_rate_limit.meaning",
    },
  ),
  blocked_flapping: lazyFields(
    { variant: "destructive" as const },
    {
      label: "health.actionState.blocked_flapping.label",
      meaning: "health.actionState.blocked_flapping.meaning",
    },
  ),
  expired: lazyFields(
    { variant: "muted" as const },
    { label: "health.actionState.expired.label", meaning: "health.actionState.expired.meaning" },
  ),
  dead: lazyFields(
    { variant: "destructive" as const },
    { label: "health.actionState.dead.label", meaning: "health.actionState.dead.meaning" },
  ),
};

export function isActionBlocked(action: Pick<AlertActionOut, "state">): boolean {
  return action.state === "blocked_rate_limit" || action.state === "blocked_flapping";
}

/**
 * WHY a blocked action was blocked, verbatim from the server.
 *
 * `last_error` is the only place the fuse, its window and the job ids counted
 * against it appear (`actions._fuse_breach`). Summarising it into a badge is
 * what makes a blocked remediation unexplainable, so this returns the stored
 * text and invents nothing; `null` means the server recorded no reason, which
 * the UI must say rather than paper over.
 */
export function blockedReason(action: Pick<AlertActionOut, "state" | "last_error">): string | null {
  if (!isActionBlocked(action)) return null;
  return action.last_error ?? null;
}

export const AUTONOMY_PRESENTATION: Readonly<
  Record<Autonomy, { label: string; variant: BadgeVariant; meaning: string }>
> = {
  suggest: lazyFields(
    { variant: "muted" as const },
    { label: "health.autonomy.suggest.label", meaning: "health.autonomy.suggest.meaning" },
  ),
  require_approval: lazyFields(
    { variant: "info" as const },
    { label: "health.autonomy.require_approval.label", meaning: "health.autonomy.require_approval.meaning" },
  ),
  // SENTENCE CASE, AND THE WEIGHT CARRIED BY THE ROW. This read "RUNS
  // AUTOMATICALLY" as a literal all-caps string, which the port's copy rule
  // reserves for `.k`/`.grp` captions (they are CSS-uppercased, not typed) --
  // and every other pill on the page is sentence case, so the shout was also
  // the only one of its kind. Spec §13's requirement is that the row does not
  // look like every other row, and that is carried by what `ApprovedActionList`
  // actually draws: the crit gutter, the wash on the `<tr>`, the robot glyph,
  // the destructive pill's colour and icon, and the red notice above the
  // table. None of those is typography.
  autonomous: lazyFields(
    // Spec §13: "a page where 'runs itself on 400 machines' looks like every
    // other row is a page that gets clicked past."
    { variant: "destructive" as const },
    { label: "health.autonomy.autonomous.label", meaning: "health.autonomy.autonomous.meaning" },
  ),
};

export function isAutonomous(action: { autonomy: Autonomy }): boolean {
  return action.autonomy === "autonomous";
}

// --------------------------------------------------------------------------
// Durations (spec §0.3)
// --------------------------------------------------------------------------

/**
 * "3 ticks (≈15 min at this profile's 5-minute tick)".
 *
 * BOTH numbers, always, and the tick interval NAMED. Ticks alone are
 * unreadable; minutes alone hide that changing the profile's tick interval
 * silently changes every check's effective duration. The seconds come from the
 * server (`profiles.effective_for_seconds`) -- this function formats, it does
 * not multiply.
 */
export function describeDuration(check: Pick<CheckOut, "for_ticks" | "for_seconds" | "tick_interval_s">): string {
  return translate("health.duration.sentence", {
    times: translate("health.duration.times", { count: check.for_ticks }),
    approx: formatSeconds(check.for_seconds),
    tick: formatSeconds(check.tick_interval_s),
  });
}

/** One decimal, in the reader's number format ("1.5" in English, "1,5" in Spanish). */
function oneDecimal(value: number): string {
  return Number.isInteger(value)
    ? String(value)
    : new Intl.NumberFormat(intlLocale(), {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
        useGrouping: false,
      }).format(value);
}

// The unit symbols (s, min, h, d) are the standard ones in both languages.
export function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${oneDecimal(seconds / 60)} min`;
  if (seconds < 86400) return `${oneDecimal(seconds / 3600)} h`;
  return `${oneDecimal(seconds / 86400)} d`;
}

/**
 * Which OS families can ANSWER this check, and which of them the operator has
 * pinned it to.
 *
 * Spec §13 wants the availability matrix inline "so an operator sees before
 * saving that a BitLocker check will be `unavailable` on their Linux estate".
 * `os_family: null` means "any OS" -- which is not the same as "every OS can
 * answer it", and that gap is exactly what this returns.
 */
export function unansweredFamilies(check: Pick<CheckOut, "os_family" | "available_os_families">): string[] {
  if (check.os_family !== null) {
    return check.available_os_families.includes(check.os_family) ? [] : [check.os_family];
  }
  return (["windows", "linux", "macos"] as const).filter(
    (family) => !check.available_os_families.includes(family),
  );
}

// --------------------------------------------------------------------------
// Reporting (spec §7.4's obligation, §13)
// --------------------------------------------------------------------------

export interface AlertSummary {
  total: number;
  open: number;
  /** Cleared alerts that belong in a resolution count at all. */
  resolved: number;
  /** Of those, the ones a machine resolved (recovery / summary_ok). */
  machineResolved: number;
  /** Of those, the ones a human closed by hand. */
  humanClosed: number;
  /**
   * Cleared for a reason that is NOT a fix and is excluded from every
   * resolution count: the sensor went away, the check was removed, or the
   * device left the fleet. Reported as its own number rather than dropped, so
   * "we resolved nothing this week" cannot hide inside a shrinking backlog.
   */
  excludedFromResolution: number;
  /** Mean time to resolution, in seconds, over `resolved` only. */
  meanTimeToResolveS: number | null;
}

/**
 * The three numbers spec §13 requires, computed with its two rules applied.
 *
 * RULE 1 — `unavailable_timeout` IS EXCLUDED FROM EVERY RESOLUTION COUNT. An
 * alert cleared because the sensor was removed is not a fixed problem, and a
 * dashboard that counts it is a defect in the report, not in the rule. So are
 * `policy_removed` (someone deleted the check) and `device_inactive` (the
 * machine left) -- same reasoning, and
 * `CLEARED_REASON_PRESENTATION.countsAsResolution` is the one place that
 * judgement is made.
 *
 * RULE 2 — MACHINE-RESOLVED AND HUMAN-RESOLVED ARE SEPARATE NUMBERS. A
 * `recovery` is the condition going away; a `manual` close is a person deciding
 * to stop looking at it. Conflating them makes auto-remediation look effective
 * in exact proportion to how often people give up.
 *
 * WHAT THIS CANNOT SEE, STATED RATHER THAN APPROXIMATED. Spec §7.4 defines the
 * machine/human split on `ticket_status_history.actor_type='system'` for an
 * `origin='alert'` ticket. That history is not on any route this module has, so
 * the split below is made on the ALERT's own `cleared_reason`, which answers
 * "what closed the alert" and not "who closed its ticket". The two agree for
 * every alert whose ticket was auto-resolved on recovery, and diverge for an
 * alert a person closed after a machine had already fixed the underlying
 * problem. A faithful version of §7.4's measure needs a server-side report; it
 * is not built, and this is not it wearing its name.
 */
export function summariseAlerts(alerts: readonly AlertOut[]): AlertSummary {
  let open = 0;
  let machineResolved = 0;
  let humanClosed = 0;
  let excluded = 0;
  let resolutionSeconds = 0;

  for (const alert of alerts) {
    if (isAlertOpen(alert)) {
      open += 1;
      continue;
    }
    if (!countsAsResolved(alert)) {
      excluded += 1;
      continue;
    }
    if (isMachineResolved(alert)) machineResolved += 1;
    else humanClosed += 1;
    if (alert.cleared_at) {
      const seconds = (Date.parse(alert.cleared_at) - Date.parse(alert.opened_at)) / 1000;
      if (Number.isFinite(seconds) && seconds >= 0) resolutionSeconds += seconds;
    }
  }

  const resolved = machineResolved + humanClosed;
  return {
    total: alerts.length,
    open,
    resolved,
    machineResolved,
    humanClosed,
    excludedFromResolution: excluded,
    meanTimeToResolveS: resolved > 0 ? resolutionSeconds / resolved : null,
  };
}

// `describeError` USED TO LIVE HERE and has been deleted rather than kept as a
// wrapper (UX remediation cycle 4). It was
//
//     if (error instanceof Error) return error.message;
//     return String(error);
//
// and `ApiError` is constructed as `new ApiError(status, await
// response.text())`, so a BODYLESS refusal -- which a 503 from a proxy in
// front of the control plane is -- carried `message === ""`. Six call sites in
// this module rendered `{mutation.error && <p>{describeError(...)}</p>}`, and
// an empty string put an EMPTY paragraph on the screen: the operator pressed
// Approve, or Revoke, or Sample now, and watched it appear to succeed. That is
// Auditor A's BLOCKER, in this module, six times.
//
// `components/form.tsx#describeSubmitError` is driven off `status` first and
// the body second, so a refusal has a sentence whether or not the server wrote
// one, and it translates a raw Pydantic 422 body instead of printing it.
// Everything here now imports that. A wrapper kept for the name would only
// have preserved a second import path to the same function and invited the old
// one back.

// --------------------------------------------------------------------------
// Glyphs (the v3 port)
// --------------------------------------------------------------------------

/**
 * The glyph v3 leads an alert row with: what the check is ABOUT, so a disk
 * alert and a CPU alert read differently before the words do. Keyed on the
 * check type's first segment (`disk.free_pct` -> `disk`), so a check type
 * added to the closed registry later still gets a sensible glyph or the
 * heartbeat fallback.
 */
const CHECK_GLYPH: Readonly<Record<string, string>> = {
  cpu: "cpu",
  memory: "cpu",
  disk: "database",
  agent: "wifi",
  posture: "shield-check",
  service: "settings",
  cert: "certificate",
};

export function checkGlyph(checkType: string): string {
  return CHECK_GLYPH[checkType.split(".")[0] ?? ""] ?? "heartbeat";
}

/**
 * A CHECK NAMED AS A SENTENCE, NOT AS ITS WIRE KEY.
 *
 * `cpu.busy_pct` and `posture.antivirus_healthy` are the registry's
 * identifiers (spec §5.1, a closed set), and an alert row headed with one
 * makes a reader parse an identifier before they can read the incident. v3
 * writes the same rows as prose ("Disk free below 5%"), so the row's TITLE is
 * these words and the identifier keeps a place of its own -- the selector
 * beside it in the console row, the whole key in the alert drawer -- because
 * it is what an operator greps a log or an API for.
 *
 * A type this table does not know is returned VERBATIM. The registry is closed
 * today; a name added to it later reads as its own key rather than as a guess
 * this file made about what the key meant.
 */
// The registry's labels live in the catalogue (`enums.HealthCheckType`, keyed
// with `_` for `.`), so a check is named the same in the alert console, the
// drawer, the dashboard and a device's overview, in either language.
export function checkLabel(checkType: string): string {
  const key = `enums.HealthCheckType.${checkType.replace(/\./g, "_")}` as MessageKey;
  const label = translate(key);
  return label === key ? checkType : label;
}
