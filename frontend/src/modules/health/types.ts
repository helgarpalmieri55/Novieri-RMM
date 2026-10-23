// Mirrors backend/app/modules/health/schema.py request/response models exactly
// (snake_case keys, matching the JSON), plus api.py's route signatures. THE
// SHIPPED API IS THE CONTRACT -- these types were written against
// `schema.py`/`api.py` directly, not against spec §10's prose, and the API wins
// on any disagreement.
//
// Per invariant #5 no response model here carries `org_id`; per invariant #6
// nothing here is a permission -- the server's `require_permission` on every
// route is the only authority, and the `canX` flags this module passes around
// decide only what RENDERS.
//
// NOTHING HERE IS A SECOND IMPLEMENTATION OF A SERVER DECISION.
// `threshold_display` is rendered by `registry.render_threshold`, `for_seconds`
// by `profiles.effective_for_seconds`, `destructive` by
// `core.approved_actions.is_destructive`, `resolution_level` by
// `resolution.ResolvedProfile.level`. The browser is given the answer and never
// the operands to compute a rival one.

// --- Closed sets, each restated from a Python `Literal` in schema.py --------

export type HealthSeverity = "info" | "warning" | "critical";

// `health_alert.state`. FOUR values, and the console must keep all four apart:
// `acknowledged` is OWNED AND STILL OPEN, `muted` is SILENCED AND STILL OPEN,
// and only `cleared` is over. See `healthState.ts`, which is where that rule
// is enforced rather than restated.
export type AlertState = "active" | "acknowledged" | "muted" | "cleared";

// `health_alert.cleared_reason`. `manual` means a human closed it, which is not
// evidence that anything was fixed; `unavailable_timeout` means the SENSOR went
// away, which §7.4 excludes from every resolution count.
export type ClearedReason =
  | "recovery"
  | "summary_ok"
  | "unavailable_timeout"
  | "policy_removed"
  | "device_inactive"
  | "manual";

export type ActionKind = "remediate" | "open_ticket" | "notify";

// `health_alert_action.state`. `awaiting_approval` is waiting on a HUMAN,
// `blocked_rate_limit`/`blocked_flapping` are refusals with a reason in
// `last_error`, and `dead` is a give-up. None of them is "done".
export type ActionState =
  | "pending"
  | "awaiting_approval"
  | "dispatched"
  | "done"
  | "skipped"
  | "blocked_rate_limit"
  | "blocked_flapping"
  | "expired"
  | "dead";

export type OsFamily = "windows" | "linux" | "macos";
export type Comparator = "gt" | "gte" | "lt" | "lte" | "eq" | "neq" | "is";
export type RiskClass = "low" | "medium" | "high";
export type Autonomy = "suggest" | "require_approval" | "autonomous";
export type ValueKind = "numeric" | "boolean" | "enum";
export type Cadence = "tick" | "posture";
export type EvaluatedBy = "agent" | "server";
export type ResolutionLevel = "device" | "group" | "org";

// `device_health_check_state.state` is `ok|breaching|unavailable`; `unknown` is
// what api.py reports for a check the device has NEVER answered. Four values,
// deliberately: `unavailable` means asked-and-could-not-answer and `unknown`
// means never-heard-from, and neither is `ok` (spec §0.7).
export type DeviceCheckState = "ok" | "breaching" | "unavailable" | "unknown";

// --- Profiles --------------------------------------------------------------

export interface ProfileOut {
  id: string;
  name: string;
  description: string;
  tick_interval_s: number;
  posture_interval_s: number;
  summary_interval_s: number;
  active: boolean;
  // Server-owned and accepted by no request model (spec §2.1). It is what the
  // device view compares against the agent's acknowledgement to show drift.
  policy_version: number;
  check_count: number;
  assignment_count: number;
  created_at: string;
  updated_at: string;
}

export interface ProfileCreate {
  name: string;
  description?: string;
  tick_interval_s?: number;
  posture_interval_s?: number;
  summary_interval_s?: number;
  active?: boolean;
}

// PATCH-shaped: `update_profile` applies only the keys actually sent. None of
// these is nullable server-side, so none is typed `| null` -- an omitted key is
// the only way to say "leave it alone", which is what `undefined` serialises to.
export interface ProfileUpdate {
  name?: string;
  description?: string;
  tick_interval_s?: number;
  posture_interval_s?: number;
  summary_interval_s?: number;
  active?: boolean;
}

// --- Checks ----------------------------------------------------------------

// `for_ticks` and `for_seconds` are BOTH optional and supplying both is a 400
// ("two answers to one question"): the server converts `for_seconds` against
// the profile's own tick interval (spec §0.3). The editor sends one.
export interface CheckIn {
  check_type: string;
  selector?: string;
  comparator: Comparator;
  threshold_num?: number | null;
  threshold_bool?: boolean | null;
  for_ticks?: number;
  for_seconds?: number;
  severity?: HealthSeverity;
  os_family?: OsFamily | null;
  enabled?: boolean;
  // Spec §0.10's per-check action declaration. The remediation slot names an
  // approved action BY ID, and the read side returns the same id, so a UI that
  // GETs the check set and PUTs it back cannot silently disarm a remediation.
  declares_notify?: boolean;
  declares_open_ticket?: boolean;
  remediate_approved_action_id?: string | null;
}

export interface CheckOut {
  id: string;
  profile_id: string;
  check_type: string;
  selector: string;
  comparator: Comparator;
  threshold_num: number | null;
  threshold_bool: boolean | null;
  // What the AGENT is sent (spec §5.2): "95", "true", or an enum member name.
  // Rendered server-side so the console and the endpoint read one alphabet.
  threshold_display: string;
  for_ticks: number;
  // Both numbers, because spec §0.3 requires "3 ticks (~15 min at this
  // profile's 5-minute tick)": ticks alone are unreadable, and minutes alone
  // hide that changing the tick interval changes the duration.
  for_seconds: number;
  tick_interval_s: number;
  severity: HealthSeverity;
  os_family: OsFamily | null;
  enabled: boolean;
  declares_notify: boolean;
  declares_open_ticket: boolean;
  remediate_approved_action_id: string | null;
  value_kind: ValueKind;
  cadence: Cadence;
  evaluated_by: EvaluatedBy;
  // The per-OS availability matrix, inline, so the editor can show BEFORE
  // saving that a BitLocker check will report `unavailable` on a Linux estate.
  available_os_families: OsFamily[];
  selector_meaning: string;
  created_at: string;
  updated_at: string;
}

export interface ProfileChecksOut {
  profile_id: string;
  policy_version: number;
  tick_interval_s: number;
  checks: CheckOut[];
}

export interface CheckTypeOut {
  name: string;
  value_kind: ValueKind;
  cadence: Cadence;
  evaluated_by: EvaluatedBy;
  os_families: OsFamily[];
  legal_comparators: Comparator[];
  selector_meaning: string;
  enum_values: string[];
}

// --- Assignments -----------------------------------------------------------

export interface AssignmentCreate {
  profile_id: string;
  device_id?: string | null;
  device_group_id?: string | null;
}

export interface AssignmentOut {
  id: string;
  profile_id: string;
  profile_name: string;
  device_id: string | null;
  device_group_id: string | null;
  // Spec §0.2's resolution level, decided server-side. The console never infers
  // it from which id happens to be null.
  level: ResolutionLevel;
  created_by: string | null;
  created_at: string;
}

// --- Alerts ----------------------------------------------------------------

export interface AlertOut {
  id: string;
  device_id: string;
  device_hostname: string;
  device_group_id: string | null;
  check_id: string;
  profile_id: string;
  check_type: string;
  selector: string;
  severity: HealthSeverity;
  state: AlertState;
  observed_value: string;
  threshold_display: string;
  opened_at: string;
  opened_policy_version: number;
  last_seen_at: string;
  occurrence_count: number;
  flapping: boolean;
  flap_count: number;
  escalated_at: string | null;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  muted_until: string | null;
  muted_by: string | null;
  cleared_at: string | null;
  cleared_reason: ClearedReason | null;
  cleared_by: string | null;
  ticket_id: string | null;
  previous_alert_id: string | null;
}

/**
 * `GET /health/alerts/summary` (`schema.py::AlertSummaryOut`).
 *
 * WHY THE SERVER COUNTS THIS. `AlertOut[]` is a PAGE -- 200 rows by the alert
 * route's own default, 500 at most -- and the list's `state` filter takes one
 * value, so "open" (active OR acknowledged OR muted) cannot be expressed
 * against it. The shell's health glyph used to fetch a page and filter it here
 * in the browser, which on a fleet with more than a page of open alerts
 * under-reports; it said "at least 40 needing attention" because 40 was a floor
 * and not a total. `open` below is the total, counted by Postgres.
 *
 * `by_state` CARRIES ONLY THE THREE OPEN STATES. `cleared` is not a key,
 * because a summary of what is open never counts one.
 *
 * `worst_severity` IS NULL ONLY WHEN NOTHING IS OPEN. It never means "not
 * known": a caller that could not take the reading gets an `ApiError`, not a
 * body with nulls in it.
 */
export interface AlertSummary {
  open: number;
  by_state: Record<Exclude<AlertState, "cleared">, number>;
  by_severity: Record<HealthSeverity, number>;
  worst_severity: HealthSeverity | null;
}

export interface AlertActionOut {
  id: string;
  alert_id: string;
  kind: ActionKind;
  state: ActionState;
  approved_action_id: string | null;
  approved_action_key: string | null;
  approved_action_name: string | null;
  script_id: string | null;
  // Spec §13: "nobody approves a script they cannot identify".
  script_name: string | null;
  script_version: number | null;
  autonomy: Autonomy | null;
  risk_class: RiskClass | null;
  destructive: boolean | null;
  job_id: string | null;
  ticket_id: string | null;
  attempts: number;
  next_attempt_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  expires_at: string | null;
  // For a blocked row the SERVICE wrote which fuse tripped, its window and the
  // JOB IDS counted against it -- and two approved actions sharing a `job_type`
  // share one fuse, so nothing else on the row can answer "what consumed the
  // budget". It is rendered verbatim.
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface AlertDetailOut extends AlertOut {
  profile_name: string;
  ticket_number: string | null;
  previous_alert_ticket_number: string | null;
  previous_alert_opened_at: string | null;
  monitoring_deep_link: string | null;
  actions: AlertActionOut[];
}

export interface AlertFilters {
  state?: AlertState | "";
  severity?: HealthSeverity | "";
  device_id?: string;
}

export interface JobDispatchedOut {
  job_id: string;
  device_id: string;
}

// --- Device health ---------------------------------------------------------

export interface DeviceCheckStateOut {
  check_id: string;
  check_type: string;
  selector: string;
  comparator: Comparator;
  threshold_display: string;
  severity: HealthSeverity;
  for_ticks: number;
  for_seconds: number;
  evaluated_by: EvaluatedBy;
  state: DeviceCheckState;
  last_value: string;
  policy_version: number | null;
  reported_at: string | null;
}

export interface DeviceHealthOut {
  device_id: string;
  hostname: string;
  os_family: string | null;
  // `false` is a FIRST-CLASS ANSWER, never an empty check list that reads as
  // "everything is fine" (spec §3).
  has_profile: boolean;
  profile: ProfileOut | null;
  // Spec §0.2: "a resolution nobody can see is a resolution nobody trusts".
  assignment: AssignmentOut | null;
  resolution_level: ResolutionLevel | null;
  acknowledged_policy_version: number | null;
  policy_drift: boolean;
  checks: DeviceCheckStateOut[];
  open_alerts: AlertOut[];
  agent_last_seen_at: string | null;
  // Reported SEPARATELY from the check states because an offline device does
  // not clear its alerts (spec §0.6): a quiet device is not a healthy one.
  agent_online: boolean;
  offline_after_s: number;
}

// --- Settings --------------------------------------------------------------

export interface SettingsOut {
  flap_window_s: number;
  flap_threshold: number;
  flap_cooldown_s: number;
  unavailable_clear_after_s: number;
  alert_retention_days: number;
  escalate_after_s: number;
  offline_after_s: number;
  approval_ttl_s: number;
  default_alert_queue_id: string | null;
  default_ticket_category_id: string | null;
  monitoring_deep_link_template: string | null;
  auto_resolve_alert_tickets: boolean;
  // `false` when this org has never written a row and is running on the
  // defaults. "We chose 3600" and "nobody has ever looked at this" lead to
  // different conversations.
  configured: boolean;
  // `open_ticket` is only declared when BOTH the queue and the category are
  // set, so an org without them gets alerts that silently never become tickets.
  alert_tickets_enabled: boolean;
  updated_at: string | null;
  updated_by: string | null;
}

export interface SettingsUpdate {
  flap_window_s?: number;
  flap_threshold?: number;
  flap_cooldown_s?: number;
  unavailable_clear_after_s?: number;
  alert_retention_days?: number;
  escalate_after_s?: number;
  offline_after_s?: number;
  approval_ttl_s?: number;
  default_alert_queue_id?: string | null;
  default_ticket_category_id?: string | null;
  monitoring_deep_link_template?: string | null;
  auto_resolve_alert_tickets?: boolean;
}

// The bounds `schema.SettingsUpdate` and `settings.SETTING_BOUNDS` both carry,
// mirrored so the form can bound its inputs. The server re-checks every one of
// them and the database CHECK constraints own the fact -- these are an
// affordance, not a validation.
export const SETTING_BOUNDS: Readonly<Record<string, readonly [number, number]>> = {
  flap_window_s: [300, 86400],
  flap_threshold: [2, 50],
  flap_cooldown_s: [300, 86400],
  unavailable_clear_after_s: [3600, 2592000],
  alert_retention_days: [90, 3650],
  escalate_after_s: [300, 604800],
  offline_after_s: [300, 86400],
  approval_ttl_s: [300, 604800],
};

// --- Approved actions ------------------------------------------------------

export interface ScriptExemptionOut {
  id: string;
  script_id: string;
  script_name: string | null;
  script_version: number;
  reason: string;
  granted_by: string;
  granted_at: string;
}

export interface ApprovedActionOut {
  id: string;
  key: string;
  name: string;
  description: string;
  job_type: string;
  // SERVED REDACTED (invariant #3). Rows written before the server-side write
  // gate shipped can carry a credential-named key; every such value arrives as
  // the placeholder string, never as the stored secret.
  params_template: Record<string, unknown>;
  // The dotted paths whose values were withheld. Non-empty means "this row
  // still holds a stored credential and needs rewriting" -- worth surfacing on
  // the registry screen rather than leaving the placeholder to explain itself.
  params_template_redacted_keys: string[];
  script_id: string | null;
  script_name: string | null;
  script_version: number | null;
  // The LIVE `scripts.version`. When it differs from the pin the action REFUSES
  // to dispatch, so the registry screen has to show it before anyone relies on
  // the action at 03:00.
  script_live_version: number | null;
  risk_class: RiskClass;
  // DERIVED server-side from job type + params + exemption, and read-only over
  // the wire (spec §8.4). No request model here carries it.
  destructive: boolean;
  max_target_scope: "device";
  autonomy: Autonomy;
  personas: string[];
  cooldown_s: number;
  max_per_device_per_day: number;
  max_per_org_per_hour: number;
  active: boolean;
  // Spec §2.10/§13: an exemption OUTLIVES the action that motivated it, so it
  // is a first-class part of this view rather than a hidden flag.
  script_exemption: ScriptExemptionOut | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface ApprovedActionCreate {
  key: string;
  name: string;
  description?: string;
  job_type: string;
  params_template?: Record<string, unknown>;
  script_id?: string | null;
  script_version?: number | null;
  risk_class: RiskClass;
  max_target_scope?: "device";
  autonomy: Autonomy;
  personas?: string[];
  cooldown_s?: number;
  max_per_device_per_day?: number;
  max_per_org_per_hour?: number;
  active?: boolean;
}

export interface ApprovedActionUpdate {
  key?: string;
  name?: string;
  description?: string;
  job_type?: string;
  params_template?: Record<string, unknown>;
  script_id?: string | null;
  script_version?: number | null;
  risk_class?: RiskClass;
  autonomy?: Autonomy;
  cooldown_s?: number;
  max_per_device_per_day?: number;
  max_per_org_per_hour?: number;
  active?: boolean;
}
