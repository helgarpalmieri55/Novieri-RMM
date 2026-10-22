/**
 * Fixtures for the SLA engine: three business calendars, the standard policy
 * ladder, the org's settings, and a per-ticket SLA state derived from the
 * service desk's own tickets so `/service-desk/tickets/:id` and `/sla`
 * describe the same promises.
 *
 * The clock is a stand-in: response and resolution deadlines are the
 * ticket's opening time plus the matching policy's target, wall-clock rather
 * than business hours. The states it reports follow the server's precedence
 * (void over breached over met over paused over running) so the panel's
 * rendering rules are exercised, not the calendar arithmetic.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ApiError } from "../../lib/api";
import { ME_ID } from "../../fixtures/me";
import { QUEUES, allTickets, commentsFor, getTicket } from "../service-desk/fixtures";
import type { TicketOut, TicketSlaSummary } from "../service-desk/types";
import type {
  CalendarDetail,
  CalendarOut,
  HolidayOut,
  PolicyOut,
  SettingsOut,
  SlaPriority,
  TargetState,
  TicketSlaOut,
  WindowOut,
} from "./types";

const MINUTE = 60_000;
const DAY = 24 * 60;

const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();
const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* --------------------------------------------------------------- calendars */

interface CalendarSeed extends CalendarOut {
  windows: WindowOut[];
  holidays: HolidayOut[];
}

const weekdays = (id: string, days: number[], start: string, end: string): WindowOut[] =>
  days.map((weekday) => ({ id: `${id}-w${weekday}`, weekday, start_time: start, end_time: end }));

const calendars: CalendarSeed[] = [
  {
    id: "cal-standard",
    name: "Standard business hours",
    timezone: "Europe/Lisbon",
    active: true,
    health: "ok",
    created_at: ago(180 * DAY),
    windows: weekdays("cal-standard", [0, 1, 2, 3, 4], "08:00:00", "18:00:00"),
    holidays: [
      { id: "hol-1", calendar_id: "cal-standard", holiday_date: "2026-12-25", name: "Christmas Day" },
      { id: "hol-2", calendar_id: "cal-standard", holiday_date: "2026-12-01", name: "Restoration of Independence" },
      { id: "hol-3", calendar_id: "cal-standard", holiday_date: "2027-01-01", name: "New Year's Day" },
    ],
  },
  {
    id: "cal-24x7",
    name: "Around the clock",
    timezone: "UTC",
    active: true,
    health: "ok",
    created_at: ago(180 * DAY),
    windows: weekdays("cal-24x7", [0, 1, 2, 3, 4, 5, 6], "00:00:00", "00:00:00"),
    holidays: [],
  },
  {
    id: "cal-porto",
    name: "Porto warehouse",
    timezone: "Europe/Lisbon",
    active: true,
    health: "no_working_windows",
    created_at: ago(12 * DAY),
    windows: [],
    holidays: [],
  },
];

const calendarOut = (c: CalendarSeed): CalendarOut => ({
  id: c.id,
  name: c.name,
  timezone: c.timezone,
  active: c.active,
  health: c.health,
  created_at: c.created_at,
});

const calendarDetail = (c: CalendarSeed): CalendarDetail => ({ ...calendarOut(c), windows: [...c.windows] });

const findCalendar = (id: string): CalendarSeed => {
  const found = calendars.find((c) => c.id === id);
  if (!found) throw notFound("calendar");
  return found;
};

const listSlaCalendars: FixtureMap["listSlaCalendars"] = () => calendars.map(calendarOut);
const getSlaCalendar: FixtureMap["getSlaCalendar"] = (id) => calendarDetail(findCalendar(id));

const createSlaCalendar: FixtureMap["createSlaCalendar"] = (body) => {
  const next: CalendarSeed = {
    id: `cal-${calendars.length + 1}`,
    name: body.name,
    timezone: body.timezone,
    active: body.active ?? true,
    health: "no_working_windows",
    created_at: ago(0),
    windows: [],
    holidays: [],
  };
  calendars.push(next);
  return calendarOut(next);
};

const updateSlaCalendar: FixtureMap["updateSlaCalendar"] = (id, body) => {
  const c = findCalendar(id);
  if (body.name !== undefined) c.name = body.name;
  if (body.timezone !== undefined) c.timezone = body.timezone;
  if (body.active !== undefined) c.active = body.active;
  return calendarDetail(c);
};

const deleteSlaCalendar: FixtureMap["deleteSlaCalendar"] = (id) => {
  const at = calendars.findIndex((c) => c.id === id);
  if (at < 0) throw notFound("calendar");
  calendars.splice(at, 1);
  return undefined;
};

const replaceSlaCalendarWindows: FixtureMap["replaceSlaCalendarWindows"] = (id, windows) => {
  const c = findCalendar(id);
  c.windows = windows.map((w, at) => ({ id: `${id}-w${at}`, ...w }));
  c.health = c.windows.length === 0 ? "no_working_windows" : "ok";
  return [...c.windows];
};

const listSlaCalendarHolidays: FixtureMap["listSlaCalendarHolidays"] = (id) => [...findCalendar(id).holidays];

const addSlaCalendarHoliday: FixtureMap["addSlaCalendarHoliday"] = (id, body) => {
  const c = findCalendar(id);
  const next: HolidayOut = { id: `hol-${id}-${c.holidays.length + 1}`, calendar_id: id, ...body };
  c.holidays.push(next);
  return next;
};

const removeSlaCalendarHoliday: FixtureMap["removeSlaCalendarHoliday"] = (id, holidayId) => {
  const c = findCalendar(id);
  c.holidays = c.holidays.filter((h) => h.id !== holidayId);
  return undefined;
};

/* ---------------------------------------------------------------- policies */

const policies: PolicyOut[] = [
  { id: "pol-urgent", queue_id: null, priority: "urgent", calendar_id: "cal-24x7", response_target_minutes: 15, resolution_target_minutes: 240, escalate_priority: false, escalate_to_queue_id: null, active: true, created_at: ago(180 * DAY) },
  { id: "pol-high", queue_id: null, priority: "high", calendar_id: "cal-standard", response_target_minutes: 60, resolution_target_minutes: 480, escalate_priority: true, escalate_to_queue_id: null, active: true, created_at: ago(180 * DAY) },
  { id: "pol-normal", queue_id: null, priority: "normal", calendar_id: "cal-standard", response_target_minutes: 240, resolution_target_minutes: 3 * DAY, escalate_priority: false, escalate_to_queue_id: null, active: true, created_at: ago(180 * DAY) },
  { id: "pol-low", queue_id: null, priority: "low", calendar_id: "cal-standard", response_target_minutes: 480, resolution_target_minutes: 7 * DAY, escalate_priority: false, escalate_to_queue_id: null, active: true, created_at: ago(180 * DAY) },
  { id: "pol-network-urgent", queue_id: "q-network", priority: "urgent", calendar_id: "cal-24x7", response_target_minutes: 10, resolution_target_minutes: 160, escalate_priority: false, escalate_to_queue_id: "q-network", active: true, created_at: ago(90 * DAY) },
  { id: "pol-facilities", queue_id: "q-facilities", priority: null, calendar_id: "cal-standard", response_target_minutes: 480, resolution_target_minutes: 5 * DAY, escalate_priority: false, escalate_to_queue_id: null, active: false, created_at: ago(200 * DAY) },
];

const listSlaPolicies: FixtureMap["listSlaPolicies"] = () => [...policies];

const createSlaPolicy: FixtureMap["createSlaPolicy"] = (body) => {
  const next: PolicyOut = {
    id: `pol-${policies.length + 1}`,
    queue_id: body.queue_id ?? null,
    priority: body.priority ?? null,
    calendar_id: body.calendar_id,
    response_target_minutes: body.response_target_minutes,
    resolution_target_minutes: body.resolution_target_minutes,
    escalate_priority: body.escalate_priority ?? false,
    escalate_to_queue_id: body.escalate_to_queue_id ?? null,
    active: body.active ?? true,
    created_at: ago(0),
  };
  policies.push(next);
  return next;
};

const updateSlaPolicy: FixtureMap["updateSlaPolicy"] = (id, body) => {
  const at = policies.findIndex((p) => p.id === id);
  const current = policies[at];
  if (at < 0 || !current) throw notFound("policy");
  const next: PolicyOut = { ...current, ...body } as PolicyOut;
  policies[at] = next;
  return next;
};

const deleteSlaPolicy: FixtureMap["deleteSlaPolicy"] = (id) => {
  const at = policies.findIndex((p) => p.id === id);
  if (at < 0) throw notFound("policy");
  policies.splice(at, 1);
  return undefined;
};

/** Spec §3's resolution: queue+priority, queue, priority, then the wildcard. */
function resolvePolicy(queueId: string, priority: string): PolicyOut | null {
  const active = policies.filter((p) => p.active);
  return (
    active.find((p) => p.queue_id === queueId && p.priority === priority) ??
    active.find((p) => p.queue_id === queueId && p.priority === null) ??
    active.find((p) => p.queue_id === null && p.priority === priority) ??
    active.find((p) => p.queue_id === null && p.priority === null) ??
    null
  );
}

/* ---------------------------------------------------------------- settings */

const settings = { auto_close_after_days: 7, configured: true };

const TERMINAL = new Set(["closed", "cancelled", "rejected", "resolved", "fulfilled"]);

const getSlaSettings: FixtureMap["getSlaSettings"] = (): SettingsOut => ({
  ...settings,
  tickets_without_sla: allTickets().filter((t) => !TERMINAL.has(t.status) && resolvePolicy(t.queue_id, t.priority) === null).length,
  stale_ticket_sla: 0,
});

const updateSlaSettings: FixtureMap["updateSlaSettings"] = (body) => {
  settings.auto_close_after_days = body.auto_close_after_days;
  settings.configured = true;
  return getSlaSettings();
};

/* -------------------------------------------------------------- per ticket */

const PAUSED = new Set(["pending", "pending_approval"]);
const VOIDED = new Set(["cancelled", "rejected"]);

function ticketSla(t: TicketOut): TicketSlaOut {
  const policy = resolvePolicy(t.queue_id, t.priority as SlaPriority);
  if (!policy) return { ticket_id: t.id, has_sla: false, response_state: "none", resolution_state: "none" };
  const opened = Date.parse(t.created_at);
  const now = Date.now();
  const responseDue = opened + policy.response_target_minutes * MINUTE;
  const resolutionDue = opened + policy.resolution_target_minutes * MINUTE;
  // The first public word from someone who is not the requester stops the
  // response clock, exactly as the engine defines it.
  const firstReply = commentsFor(t.id, false).find((c) => c.author_id !== null && c.author_id !== t.requester_id);
  const respondedAt = firstReply ? Date.parse(firstReply.created_at) : null;
  const resolvedAt = t.resolved_at ? Date.parse(t.resolved_at) : null;
  const voided = VOIDED.has(t.status);
  const paused = PAUSED.has(t.status);

  const responseState: TargetState = voided
    ? "void"
    : respondedAt !== null
      ? respondedAt > responseDue ? "breached" : "met"
      : now > responseDue ? "breached" : paused ? "paused" : "running";
  const resolutionState: TargetState = voided
    ? "void"
    : resolvedAt !== null
      ? resolvedAt > resolutionDue ? "breached" : "met"
      : now > resolutionDue ? "breached" : paused ? "paused" : "running";

  const iso = (ms: number | null) => (ms === null ? null : new Date(ms).toISOString());
  return {
    ticket_id: t.id,
    has_sla: true,
    response_state: responseState,
    resolution_state: resolutionState,
    policy_id: policy.id,
    calendar_id: policy.calendar_id,
    policy_name: describePolicy(policy),
    calendar_name: calendars.find((c) => c.id === policy.calendar_id)?.name ?? null,
    response_due_at: iso(responseDue),
    resolution_due_at: iso(resolutionDue),
    response_met_at: responseState === "met" ? iso(respondedAt) : null,
    resolution_met_at: resolutionState === "met" ? iso(resolvedAt) : null,
    response_breached_at: responseState === "breached" ? iso(respondedAt ?? responseDue) : null,
    resolution_breached_at: resolutionState === "breached" ? iso(resolvedAt ?? resolutionDue) : null,
    paused_at: paused ? t.updated_at : null,
    paused_minutes_total: paused ? Math.round((now - Date.parse(t.updated_at)) / MINUTE) : 0,
    voided_at: voided ? t.closed_at : null,
    escalated_at: policy.escalate_priority && resolutionState === "breached" ? iso(resolutionDue) : null,
    policy_source: t.origin === "alert" ? "retriage" : "initial",
  };
}

const getTicketSla: FixtureMap["getTicketSla"] = (ticketId) => ticketSla(getTicket(ticketId));

const PRIORITY_NOUNS: Record<string, string> = {
  low: "Low priority tickets",
  normal: "Normal priority tickets",
  high: "High priority tickets",
  urgent: "Urgent tickets",
};

/** `sla.views.describe_policy`, for the fixture's policies. */
function describePolicy(policy: PolicyOut): string {
  const who = policy.priority ? PRIORITY_NOUNS[policy.priority] ?? policy.priority : "Every ticket";
  const queue = policy.queue_id ? QUEUES.find((q) => q.id === policy.queue_id)?.name : null;
  if (queue) return `${who} in the ${queue} queue`;
  return policy.priority ? `${who} in any queue` : who;
}

/** The list row's SLA summary, derived from the same fixture clock. */
export function ticketSlaSummary(t: TicketOut): TicketSlaSummary | null {
  const full = ticketSla(t);
  if (!full.has_sla || !full.response_due_at || !full.resolution_due_at) return null;
  const voided = full.response_state === "void";
  const paused = full.response_state === "paused" || full.resolution_state === "paused";
  const owed: Array<[string, "response" | "resolution"]> = [];
  if (!voided && !paused) {
    if (full.response_met_at == null) owed.push([full.response_due_at, "response"]);
    if (full.resolution_met_at == null) owed.push([full.resolution_due_at, "resolution"]);
  }
  owed.sort((a, b) => Date.parse(a[0]) - Date.parse(b[0]));
  const next = owed[0];
  return {
    response_due_at: full.response_due_at,
    resolution_due_at: full.resolution_due_at,
    response_state: full.response_state,
    resolution_state: full.resolution_state,
    next_due_at: next ? next[0] : null,
    next_due_target: next ? next[1] : null,
    breached: full.response_state === "breached" || full.resolution_state === "breached",
    paused,
  };
}

/** Who edits the org's SLA configuration in the fixture: the signed-in admin. */
export const SLA_ADMIN = ME_ID;

const fixtures: FixtureMap = {
  listSlaCalendars,
  getSlaCalendar,
  createSlaCalendar,
  updateSlaCalendar,
  deleteSlaCalendar,
  replaceSlaCalendarWindows,
  listSlaCalendarHolidays,
  addSlaCalendarHoliday,
  removeSlaCalendarHoliday,
  listSlaPolicies,
  createSlaPolicy,
  updateSlaPolicy,
  deleteSlaPolicy,
  getSlaSettings,
  updateSlaSettings,
  getTicketSla,
};

export default fixtures;
