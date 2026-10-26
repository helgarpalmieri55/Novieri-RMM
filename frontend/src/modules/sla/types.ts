// Mirrors backend/app/modules/sla/schema.py request/response models exactly
// (snake_case keys, matching the JSON), plus api.py's route signatures. The
// SHIPPED API is the contract -- these types were written against
// `schema.py`/`api.py`/`views.py` directly, not against spec §9's prose,
// and the API wins on any disagreement.
//
// Per invariant #5 no response model here carries `org_id`; per invariant #6
// nothing here is a permission -- the server's `require_permission` on every
// route is the only authority.
//
// NOTHING HERE IS A SECOND IMPLEMENTATION OF THE CLOCK (schema.py's own
// header): `TicketSlaOut` carries the STORED instants plus the two state
// strings `views.target_state` derived from them server-side. There is no
// target-minutes/elapsed pair on it that a browser could subtract into a
// rival met/breached verdict, and this module never invents one.

// views.py: CalendarHealth = Literal["ok", "no_working_windows",
// "invalid_timezone"].
export type CalendarHealth = "ok" | "no_working_windows" | "invalid_timezone";

// views.py: TargetState = Literal["none", "void", "breached", "met",
// "paused", "running"]. The precedence between them is decided server-side
// (`target_state`) -- void outranks breached, breached outranks met -- and is
// never re-derived here.
export type TargetState = "none" | "void" | "breached" | "met" | "paused" | "running";

// schema.py: Priority = Literal["low", "normal", "high", "urgent"], the
// ticket core's own domain.
export type SlaPriority = "low" | "normal" | "high" | "urgent";

export const SLA_PRIORITIES: readonly SlaPriority[] = ["low", "normal", "high", "urgent"];

// settings.py's MAX_AUTO_CLOSE_AFTER_DAYS and sweep.py's
// DEFAULT_AUTO_CLOSE_AFTER_DAYS. Mirrored so the form can bound its input and
// name the fallback an unconfigured org is running on; the server re-checks
// both (`set_auto_close_after_days`).
export const MAX_AUTO_CLOSE_AFTER_DAYS = 3650;
export const DEFAULT_AUTO_CLOSE_AFTER_DAYS = 7;

// --- Calendars (schema.py's CalendarCreate/CalendarUpdate/CalendarOut/
// CalendarDetail) ----------------------------------------------------------

export interface CalendarCreate {
  name: string;
  timezone: string;
  active?: boolean;
}

// `CalendarUpdate` is PATCH-shaped: `calendars.update_calendar` applies only
// the keys actually sent (`supplied()` is `exclude_unset`). None of these are
// nullable server-side -- an explicit `null` is refused with a typed 400 --
// so none of them are typed `| null` here: an omitted key is the only way to
// say "leave it alone", and that is exactly what `undefined` serialises to.
export interface CalendarUpdate {
  name?: string;
  timezone?: string;
  active?: boolean;
}

// `start_time`/`end_time` are Pydantic `time` values -- "HH:MM:SS" over the
// wire. `end_time === "00:00:00"` is the MIDNIGHT-END-OF-DAY sentinel (spec
// §2.2, migration 0035, CHECK `ck_sla_calendar_window_start_before_end`), not
// a zero-length window: it is worth 1440 minutes, and it is the only legal
// encoding for an evening shift's second half and for a true 24-hour day.
// `slaState.ts` owns every reading of it; no component parses these strings.
export interface WindowIn {
  weekday: number;
  start_time: string;
  end_time: string;
}

export interface WindowsReplace {
  windows: WindowIn[];
}

export interface WindowOut {
  id: string;
  weekday: number;
  start_time: string;
  end_time: string;
}

export interface HolidayCreate {
  holiday_date: string;
  name: string;
}

export interface HolidayOut {
  id: string;
  calendar_id: string;
  holiday_date: string;
  name: string;
}

// The timezone is on EVERY view of a calendar (schema.py's header, spec
// §9.1): a working window is meaningless without it.
export interface CalendarOut {
  id: string;
  name: string;
  timezone: string;
  active: boolean;
  health: CalendarHealth;
  created_at: string;
}

// The by-id view. Spec §9's table gives windows a PUT and no GET; the shipped
// API hangs them off `CalendarDetail` instead, which is what makes the weekly
// grid loadable at all.
export interface CalendarDetail extends CalendarOut {
  windows: WindowOut[];
}

// --- Policies (schema.py's PolicyCreate/PolicyUpdate/PolicyOut) -----------
//
// `queue_id`/`priority` NULL is a WILDCARD ("any"), not an unset field (spec
// §2.4/§3's resolution levels). The UI renders it as the word "any", never as
// a blank cell.

export interface PolicyCreate {
  queue_id?: string | null;
  priority?: SlaPriority | null;
  calendar_id: string;
  response_target_minutes: number;
  resolution_target_minutes: number;
  escalate_priority?: boolean;
  escalate_to_queue_id?: string | null;
  active?: boolean;
}

// An explicit `null` on a wildcard-capable field is a REAL edit ("apply to
// any queue") -- `policies.update_policy` keys on presence, not on
// null-ness. The three NOT NULL columns are deliberately NOT typed `| null`
// so a wildcarding edit stays expressible while a null the server would
// refuse stays unrepresentable.
export interface PolicyUpdate {
  queue_id?: string | null;
  priority?: SlaPriority | null;
  calendar_id?: string;
  response_target_minutes?: number;
  resolution_target_minutes?: number;
  escalate_priority?: boolean;
  escalate_to_queue_id?: string | null;
  active?: boolean;
}

export interface PolicyOut {
  id: string;
  // The client whose tickets it is for; `null`, every client's (a global
  // policy). Plan 2026-10-26 D2: a client with active policies of its own
  // resolves among those alone.
  client_id: string | null;
  queue_id: string | null;
  priority: SlaPriority | null;
  calendar_id: string;
  response_target_minutes: number;
  resolution_target_minutes: number;
  escalate_priority: boolean;
  escalate_to_queue_id: string | null;
  active: boolean;
  created_at: string;
}

// --- Settings (schema.py's SettingsUpdate/SettingsOut) --------------------

export interface SettingsUpdate {
  auto_close_after_days: number;
}

export interface SettingsOut {
  auto_close_after_days: number;
  // False when the org has never configured one and is running on
  // DEFAULT_AUTO_CLOSE_AFTER_DAYS -- "we chose 7" and "nobody has ever looked
  // at this" lead to different conversations.
  configured: boolean;
  // views.count_tickets_without_sla: non-terminal tickets with no ticket_sla
  // row. 0 in a healthy org, growing in one whose calendar or policy
  // configuration is broken.
  tickets_without_sla: number;
  // views.count_stale_ticket_sla: running clocks whose stored deadline is not
  // what the configuration in force would produce -- what a policy or calendar
  // edit leaves behind when its recompute failed on those rows and the edit
  // returned 200 anyway. The complement of the count above: that one counts
  // promises that are MISSING, this one promises that are PRESENT and wrong.
  stale_ticket_sla: number;
}

// --- Ticket SLA state (schema.py's TicketSlaOut, GET
// /tickets/{ticket_id}/sla, gated on `ticket.read`) ------------------------
//
// `has_sla: false` is a 200, not a 404 (api.py): spec §3 requires "no SLA" to
// be a visible, legitimate state. Both state strings are the server's verdict.

export interface TicketSlaOut {
  ticket_id: string;
  has_sla: boolean;
  response_state: TargetState;
  resolution_state: TargetState;
  policy_id?: string | null;
  calendar_id?: string | null;
  // Display names for the two ids; a policy is described by what it matches.
  policy_name?: string | null;
  calendar_name?: string | null;
  response_due_at?: string | null;
  resolution_due_at?: string | null;
  response_met_at?: string | null;
  resolution_met_at?: string | null;
  response_breached_at?: string | null;
  resolution_breached_at?: string | null;
  paused_at?: string | null;
  paused_minutes_total?: number | null;
  voided_at?: string | null;
  escalated_at?: string | null;
  // §0.3's attribution requirement: `initial` | `retriage` | `escalation` --
  // "breached because we were slow" and "breached retroactively because we
  // re-triaged at hour five" must be distinguishable. Typed `string` because
  // schema.py types it `str | None`, not a Literal.
  policy_source?: string | null;
}
