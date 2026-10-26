// Pure presentation logic for the SLA module. Everything here is a display
// decision over values the SERVER already decided; nothing here decides
// whether a deadline was met.
//
// THE ONE RULE THIS FILE EXISTS TO KEEP: spec §9.1 -- "Breach state is never
// computed in the browser. The server owns whether a deadline was met." So
// `TargetState` arrives as a string from `views.target_state` and is only
// ever mapped to a label and a colour here. `describeTargetTiming` below
// takes that state as an INPUT and branches on it; it never infers a state
// from the timestamps, which is why a past-due `running` clock reads "past
// due, breach not yet recorded" rather than "breached", and why a `void`
// promise cannot render as a breach even when `breached_at` is set on the row
// (server precedence: void outranks breached).

import { translate, type MessageKey } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { BadgeVariant } from "../jobs/jobState";
import type { CalendarHealth, PolicyOut, SlaPriority, TargetState, WindowIn } from "./types";

// --------------------------------------------------------------------------
// Working windows (spec §2.2, migration 0035)
// --------------------------------------------------------------------------

export const MINUTES_PER_DAY = 1440;

// Monday=0 .. Sunday=6, matching `sla_calendar_window.weekday` and Python's
// `date.weekday()` (CHECK `weekday BETWEEN 0 AND 6`, migration 0034). An ISO
// Sunday-is-7 reading here would render every window against the wrong day.
export const WEEKDAYS: readonly number[] = [0, 1, 2, 3, 4, 5, 6];

// The names are looked up when read, never held as text: a constant array of
// the words would keep the language the page started in.
const WEEKDAY_KEYS: readonly MessageKey[] = [
  "sla.weekday.monday",
  "sla.weekday.tuesday",
  "sla.weekday.wednesday",
  "sla.weekday.thursday",
  "sla.weekday.friday",
  "sla.weekday.saturday",
  "sla.weekday.sunday",
];

const WEEKDAY_INLINE_KEYS: readonly MessageKey[] = [
  "sla.weekdayInline.monday",
  "sla.weekdayInline.tuesday",
  "sla.weekdayInline.wednesday",
  "sla.weekdayInline.thursday",
  "sla.weekdayInline.friday",
  "sla.weekdayInline.saturday",
  "sla.weekdayInline.sunday",
];

/** A weekday inside a label or a sentence: lower case in Spanish, capitalised in English. */
export function weekdayInlineLabel(weekday: number): string {
  const key = WEEKDAY_INLINE_KEYS[weekday];
  return key ? translate(key) : translate("sla.weekday.other", { weekday });
}

export function weekdayLabel(weekday: number): string {
  const key = WEEKDAY_KEYS[weekday];
  return key ? translate(key) : translate("sla.weekday.other", { weekday });
}

// "HH:MM" or "HH:MM:SS" (Pydantic serialises `time` as the latter) -> minutes
// from local midnight. Returns null for anything unparseable rather than a
// silent 0, which would render a broken window as a legal 00:00 start.
export function parseTimeToMinutes(value: string): number | null {
  const match = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(value.trim());
  if (!match) return null;
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  if (hours > 23 || minutes > 59) return null;
  return hours * 60 + minutes;
}

// `end_time == 00:00` is MIDNIGHT AT THE END OF THE DAY, not a zero-length
// window (spec §2.2 -- Postgres `TIME` has no 24:00, so this is the only
// legal encoding for an evening shift's second half and for a true 24-hour
// day). Mirrors `calendars._window_minutes`, which is the write-time
// validator, and `calendar._merge_windows`, which is the clock.
export function isMidnightEnd(endTime: string): boolean {
  return parseTimeToMinutes(endTime) === 0;
}

export function windowEndMinutes(endTime: string): number | null {
  const minutes = parseTimeToMinutes(endTime);
  if (minutes === null) return null;
  return minutes === 0 ? MINUTES_PER_DAY : minutes;
}

// Duration of a window in minutes, or null if either boundary is
// unparseable. A `00:00-00:00` window is 1440 minutes (a full 24-hour day),
// NEVER 0.
export function windowDurationMinutes(window: Pick<WindowIn, "start_time" | "end_time">): number | null {
  const start = parseTimeToMinutes(window.start_time);
  const end = windowEndMinutes(window.end_time);
  if (start === null || end === null) return null;
  return end - start;
}

export function formatTimeOfDay(value: string): string {
  const minutes = parseTimeToMinutes(value);
  if (minutes === null) return value;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return `${String(hours).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

// The end boundary as a READER sees it: the midnight sentinel is "24:00",
// because rendering it as "00:00" makes a legal evening window look inverted
// and a 24-hour day look empty.
export function formatWindowEnd(endTime: string): string {
  return isMidnightEnd(endTime) ? "24:00" : formatTimeOfDay(endTime);
}

export function formatWindow(window: Pick<WindowIn, "start_time" | "end_time">): string {
  return `${formatTimeOfDay(window.start_time)}–${formatWindowEnd(window.end_time)}`;
}

export function totalWindowMinutes(
  windows: ReadonlyArray<Pick<WindowIn, "start_time" | "end_time">>,
): number {
  return windows.reduce((total, window) => total + (windowDurationMinutes(window) ?? 0), 0);
}

// Client-side mirror of `calendars._validate_windows`, offered as immediate
// feedback in the editor. It is NOT the authority: the server validates the
// whole set again before any row is touched, and an edit that slips past this
// still gets a 400. Kept in step with the server rule deliberately --
// `start >= end` is rejected EXCEPT for the midnight sentinel, and touching
// windows (one ending exactly where the next starts) are not an overlap,
// because `[start, end)` is half-open in the clock.
export function validateWindows(windows: ReadonlyArray<WindowIn>): string[] {
  const problems: string[] = [];
  const byWeekday = new Map<number, Array<[number, number]>>();

  for (const window of windows) {
    if (!Number.isInteger(window.weekday) || window.weekday < 0 || window.weekday > 6) {
      problems.push(translate("sla.windowProblem.weekdayRange", { weekday: window.weekday }));
      continue;
    }
    const start = parseTimeToMinutes(window.start_time);
    const end = windowEndMinutes(window.end_time);
    if (start === null || end === null) {
      problems.push(translate("sla.windowProblem.timesFormat", { day: weekdayLabel(window.weekday) }));
      continue;
    }
    if (start >= end) {
      problems.push(
        translate("sla.windowProblem.notBefore", {
          day: weekdayLabel(window.weekday),
          start: formatTimeOfDay(window.start_time),
          end: formatWindowEnd(window.end_time),
        }),
      );
      continue;
    }
    const spans = byWeekday.get(window.weekday) ?? [];
    spans.push([start, end]);
    byWeekday.set(window.weekday, spans);
  }

  for (const [weekday, spans] of byWeekday) {
    let runningEnd: number | null = null;
    for (const [start, end] of [...spans].sort((a, b) => a[0] - b[0])) {
      if (runningEnd !== null && start < runningEnd) {
        problems.push(translate("sla.windowProblem.overlap", { day: weekdayLabel(weekday) }));
        break;
      }
      runningEnd = runningEnd === null ? end : Math.max(runningEnd, end);
    }
  }
  return problems;
}

// --------------------------------------------------------------------------
// Durations
// --------------------------------------------------------------------------

// Minutes -> "8h", "1h 30m", "2d 4h". Targets are stored in minutes and read
// as durations; "480" tells a reader nothing about whether it is the four
// hours they configured.
export function formatMinutes(minutes: number): string {
  if (!Number.isFinite(minutes)) return "—";
  const whole = Math.floor(Math.abs(minutes));
  const sign = minutes < 0 ? "-" : "";
  if (whole === 0) return translate("sla.duration.zero");
  const days = Math.floor(whole / MINUTES_PER_DAY);
  const hours = Math.floor((whole % MINUTES_PER_DAY) / 60);
  const mins = whole % 60;
  const parts: string[] = [];
  if (days > 0) parts.push(translate("sla.duration.days", { count: days }));
  if (hours > 0) parts.push(translate("sla.duration.hours", { count: hours }));
  if (mins > 0 && days === 0) parts.push(translate("sla.duration.minutes", { count: mins }));
  return sign + parts.join(" ");
}

export function formatDurationMs(ms: number): string {
  const minutes = Math.floor(Math.abs(ms) / 60_000);
  if (minutes === 0) return translate("sla.duration.underMinute");
  return formatMinutes(minutes);
}

// `formatInstant` USED TO LIVE HERE and has been deleted rather than kept
// (UX remediation cycle 5). It was `new Date(value).toLocaleString()`, which
// is one of the ~70 ad-hoc renderings that produced the audit's "the same
// device reports two different last-contact times in two different date
// formats", and it rendered in the BROWSER's zone with no way to say which
// zone that was. Two of its three branches were also wrong on their own
// terms: an unparseable value was echoed back raw, and an absent one used a
// hand-typed em dash that no other module agreed on.
//
// `lib/datetime.formatDateTime` replaces all of it — one format for the whole
// product, `NO_DATE` for absent, and the zone taken from the signed-in
// profile (`api.useUserTimezone`) rather than from whichever machine the
// technician happens to be sitting at.

// --------------------------------------------------------------------------
// Calendar health (views.calendar_health)
// --------------------------------------------------------------------------

// SENTENCE CASE, because these are pill labels and every other pill in the
// console reads "Open", "Fulfilled", "Submitted". "ok" / "no working windows"
// were the wire's own words shown to a person, one column above an ACTIVE
// column that read "active" for the same reason. The words are the catalogue's
// (`enums.CalendarHealth`); a value the console has not met yet is humanised,
// never printed raw.
const HEALTH_VALUES: ReadonlySet<string> = new Set<CalendarHealth>(["ok", "no_working_windows", "invalid_timezone"]);

const HEALTH_VARIANTS: Record<CalendarHealth, BadgeVariant> = {
  ok: "success",
  // Both faults mean the same thing operationally: every ticket resolving to
  // this calendar silently gets NO SLA row (tracking's tolerant degradation).
  // That is a failure, not a warning.
  no_working_windows: "destructive",
  invalid_timezone: "destructive",
};

export function calendarHealthLabel(health: CalendarHealth): string {
  return HEALTH_VALUES.has(health) ? humaniseEnum(health, "CalendarHealth") : humaniseStatus(health);
}

export function calendarHealthVariant(health: CalendarHealth): BadgeVariant {
  return HEALTH_VARIANTS[health] ?? "muted";
}

export function calendarHealthExplanation(health: CalendarHealth): string | null {
  switch (health) {
    case "no_working_windows":
      return translate("sla.health.noWindowsExplanation");
    case "invalid_timezone":
      return translate("sla.health.invalidTimezoneExplanation");
    default:
      return null;
  }
}

// --------------------------------------------------------------------------
// Target state (views.target_state -- the server's verdict, rendered)
// --------------------------------------------------------------------------

// Sentence case, for the same reason as the health labels above: these are pill
// labels on the ticket page, sitting beside "Open", "Urgent" and "Incident".
// The words are `enums.TargetState`.
const STATE_VALUES: ReadonlySet<string> = new Set<TargetState>(["none", "void", "breached", "met", "paused", "running"]);

const STATE_VARIANTS: Record<TargetState, BadgeVariant> = {
  // "no SLA" is an absence, not a failure -- but it is never silent.
  none: "muted",
  // A VOID promise is neither kept nor broken (spec §2.5.2): it must not
  // carry the breach colour, or a technician reading the board will
  // prioritise a ticket whose promise no longer exists.
  void: "muted",
  breached: "destructive",
  met: "success",
  // Paused is its own colour precisely so a stopped clock is visibly stopped
  // rather than looking like a running one that quietly stopped moving.
  paused: "warning",
  running: "info",
};

export function targetStateLabel(state: TargetState): string {
  return STATE_VALUES.has(state) ? humaniseEnum(state, "TargetState") : humaniseStatus(state);
}

export function targetStateVariant(state: TargetState): BadgeVariant {
  return STATE_VARIANTS[state] ?? "muted";
}

// Only a RUNNING clock counts down. A paused one is stopped, a met/breached/
// void one is finished, and none has no deadline at all -- rendering a
// countdown for any of them would be a lie about what the clock is doing.
export function countsDown(state: TargetState): boolean {
  return state === "running";
}

export interface TargetFacts {
  state: TargetState;
  dueAt: string | null | undefined;
  metAt: string | null | undefined;
  breachedAt: string | null | undefined;
  pausedAt: string | null | undefined;
  pausedMinutesTotal: number | null | undefined;
  voidedAt: string | null | undefined;
}

export interface TargetTiming {
  // The one-line answer a technician prioritises from.
  headline: string;
  // The supporting instants; never a second verdict.
  detail: string | null;
  // True only while a countdown is genuinely running, so the caller can tick
  // it. False for paused/met/breached/void/none.
  ticking: boolean;
}

function instantMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : ms;
}

// The state string is an INPUT, never inferred. Each branch describes what
// the server already decided, in the units a technician acts on.
export function describeTargetTiming(
  facts: TargetFacts,
  nowMs: number,
  // The SIGNED-IN USER's zone (cycle 5). Optional, and `undefined`/`null`
  // falls back to the browser's — a technician who has not set a preference
  // sees exactly what they saw before, and one who has sees the same instant
  // on every machine they work from.
  zone?: string | null,
): TargetTiming {
  const due = instantMs(facts.dueAt);

  switch (facts.state) {
    case "none":
      return {
        headline: translate("sla.timing.none.headline"),
        detail: translate("sla.timing.none.detail"),
        ticking: false,
      };

    case "void": {
      // A voided promise is NEITHER kept NOR broken (spec §2.5.2). It must
      // never render as a breach, even though `breached_at` may be set on the
      // row -- the server's precedence already put void on top, and this
      // branch honours it rather than second-guessing it.
      const voided = instantMs(facts.voidedAt);
      return {
        headline: translate("sla.timing.void.headline"),
        detail:
          voided === null
            ? translate("sla.timing.void.detailUndated")
            : translate("sla.timing.void.detail", { date: formatDateTime(facts.voidedAt, zone) }),
        ticking: false,
      };
    }

    case "breached": {
      const breached = instantMs(facts.breachedAt);
      const headline =
        breached === null
          ? translate("sla.timing.breached.headline")
          : translate("sla.timing.breached.headlineAgo", { duration: formatDurationMs(nowMs - breached) });
      const parts: string[] = [];
      if (due !== null) parts.push(translate("sla.timing.breached.deadlineWas", { date: formatDateTime(facts.dueAt, zone) }));
      if (facts.metAt) {
        // Both columns can be set at once: a reply landing after the deadline
        // stamps `met_at` on an already-breached row. The server reports
        // breached, and so do we -- reporting "met" here would let any breach
        // be laundered by eventually answering.
        parts.push(translate("sla.timing.breached.answeredLate", { date: formatDateTime(facts.metAt, zone) }));
      }
      return { headline, detail: parts.length > 0 ? `${parts.join("; ")}.` : null, ticking: false };
    }

    case "met": {
      const met = instantMs(facts.metAt);
      const headline =
        met === null
          ? translate("sla.timing.met.headline")
          : translate("sla.timing.met.headlineAgo", { duration: formatDurationMs(nowMs - met) });
      let detail: string | null = null;
      if (met !== null && due !== null) {
        // `met` can only be later than `due` on a row the server would have
        // reported as breached (breached outranks met), so this branch is
        // defensive -- but it must not print a negative margin as "inside".
        detail = translate(due >= met ? "sla.timing.met.inside" : "sla.timing.met.after", {
          met: formatDateTime(facts.metAt, zone),
          duration: formatDurationMs(Math.abs(due - met)),
          due: formatDateTime(facts.dueAt, zone),
        });
      } else if (met !== null) {
        detail = translate("sla.timing.met.at", { met: formatDateTime(facts.metAt, zone) });
      }
      return { headline, detail, ticking: false };
    }

    case "paused": {
      // NO COUNTDOWN HERE, deliberately. The stored `*_due_at` is the
      // deadline as it stood when the clock stopped; `resume` re-derives it
      // with the pause credited (tracking.resume), so any "time remaining"
      // rendered now would be wrong the moment the ticket moves. What is
      // honest is how long it has been stopped, which counts UP.
      const paused = instantMs(facts.pausedAt);
      const headline =
        paused === null
          ? translate("sla.timing.paused.headline")
          : translate("sla.timing.paused.headlineFor", { duration: formatDurationMs(nowMs - paused) });
      const parts: string[] = [];
      if (paused !== null) parts.push(translate("sla.timing.paused.since", { date: formatDateTime(facts.pausedAt, zone) }));
      if (due !== null) {
        parts.push(translate("sla.timing.paused.extends", { date: formatDateTime(facts.dueAt, zone) }));
      }
      if (facts.pausedMinutesTotal != null && facts.pausedMinutesTotal > 0) {
        parts.push(translate("sla.timing.paused.credited", { duration: formatMinutes(facts.pausedMinutesTotal) }));
      }
      return { headline, detail: parts.length > 0 ? `${parts.join("; ")}.` : null, ticking: false };
    }

    case "running":
    default: {
      if (due === null) {
        return { headline: translate("sla.timing.running.headline"), detail: null, ticking: true };
      }
      const remaining = due - nowMs;
      if (remaining >= 0) {
        return {
          headline: translate("sla.timing.running.remaining", { duration: formatDurationMs(remaining) }),
          detail: translate("sla.timing.running.due", { date: formatDateTime(facts.dueAt, zone) }),
          ticking: true,
        };
      }
      // Past its due instant but the sweep has not recorded a breach yet.
      // Saying "breached" here would be the browser owning a verdict the
      // server owns (spec §9.1), so it says exactly what is true.
      return {
        headline: translate("sla.timing.running.pastDue", { duration: formatDurationMs(remaining) }),
        detail: translate("sla.timing.running.pastDueDetail", { date: formatDateTime(facts.dueAt, zone) }),
        ticking: true,
      };
    }
  }
}

// --------------------------------------------------------------------------
// Policy resolution (spec §3, mirroring policies.resolve_policy)
// --------------------------------------------------------------------------
//
// This is a PREVIEW over the policy list the API returned -- an explanation
// of a configuration, not an authority over any ticket. The server resolves a
// real ticket's policy at creation and on human re-triage; nothing here
// writes, and nothing here changes what a ticket got. It exists because
// overlapping wildcard policies are the confusing case this module is for:
// without it an admin cannot tell which of four candidate rows actually wins.

export interface ResolutionLevel {
  level: 1 | 2 | 3 | 4;
  label: string;
  queueId: string | null;
  priority: SlaPriority | null;
  policy: PolicyOut | null;
  // Why this level did not produce the winner, when it did not.
  wins: boolean;
}

export interface ResolutionPreview {
  levels: ResolutionLevel[];
  winner: ResolutionLevel | null;
}

// Only `active` policies are considered, exactly as `resolve_policy` does --
// an inactive row is invisible to resolution, which is the documented
// retirement path for a policy that cannot be deleted.
function matchAt(
  policies: ReadonlyArray<PolicyOut>,
  queueId: string | null,
  priority: SlaPriority | null,
): PolicyOut | null {
  return (
    policies.find(
      (policy) =>
        policy.active && policy.queue_id === queueId && policy.priority === priority,
    ) ?? null
  );
}

// WHOSE POLICIES (plan 2026-10-26 D2, mirroring `resolve_policy`): a client
// with any active policy of its own resolves among those alone; any other --
// and `clientId === null`, the global configuration -- among the global ones
// (`client_id === null`). The two are never mixed level by level.
function ownersPolicies(
  policies: ReadonlyArray<PolicyOut>,
  clientId: string | null,
): PolicyOut[] {
  const hasOwn =
    clientId !== null && policies.some((policy) => policy.active && policy.client_id === clientId);
  const owner = hasOwn ? clientId : null;
  return policies.filter((policy) => policy.client_id === owner);
}

export function resolutionPreview(
  allPolicies: ReadonlyArray<PolicyOut>,
  queueId: string,
  priority: SlaPriority,
  clientId: string | null,
): ResolutionPreview {
  const policies = ownersPolicies(allPolicies, clientId);
  const candidates: Array<{ level: 1 | 2 | 3 | 4; label: string; queueId: string | null; priority: SlaPriority | null }> = [
    { level: 1, label: translate("sla.resolver.level1"), queueId, priority },
    { level: 2, label: translate("sla.resolver.level2"), queueId, priority: null },
    { level: 3, label: translate("sla.resolver.level3"), queueId: null, priority },
    { level: 4, label: translate("sla.resolver.level4"), queueId: null, priority: null },
  ];

  let winnerFound = false;
  const levels: ResolutionLevel[] = candidates.map((candidate) => {
    const policy = matchAt(policies, candidate.queueId, candidate.priority);
    const wins = !winnerFound && policy !== null;
    if (wins) winnerFound = true;
    return { ...candidate, policy, wins };
  });

  return { levels, winner: levels.find((level) => level.wins) ?? null };
}

// --------------------------------------------------------------------------
// Errors
// --------------------------------------------------------------------------

// THERE IS NO `describeError` HERE ANY MORE (UX remediation cycle 4), and it
// was deleted rather than rewritten. It read the error's `.message`, tried
// `JSON.parse` on it, and returned the raw message on any other shape. That
// last fallback is the BLOCKER: `ApiClient` throws
// `new ApiError(status, await response.text())`, so a bodyless refusal — a
// 503 from a proxy is generally bodyless — arrived with `message === ""` and
// this function returned "". Every call site guarded on the result being
// truthy, so a failed DELETE of a calendar or a holiday rendered NOTHING and
// the operator watched a destructive action appear to succeed.
//
// `components/form.describeSubmitError` is the replacement and is driven off
// `status` FIRST, so a sentence exists whether or not the server wrote one.
// It also keeps the behaviour that was worth keeping: FastAPI's
// `{"detail": "..."}` body — which is where this module's service layer names
// the policies blocking a calendar delete, or the overlapping window — is
// still unwrapped and shown as the sentence it is.
