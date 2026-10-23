/**
 * `/health` and the device health panel in fixture mode.
 *
 * CONSISTENT WITH THE SHELL. `core.ts` answers `getHealthAlertSummary` with
 * twelve open alerts (nine active, two acknowledged, one muted; one critical,
 * eight warning, three info) because that is what the rail's badge and the
 * top bar's glyph read. The list below is those twelve rows, plus five
 * cleared ones so the reporting panel has a machine/human split and an
 * excluded non-fix to show -- and every alert names a device from the same
 * fourteen-row fleet the devices page draws.
 *
 * Filters are honoured the way `GET /health/alerts` honours them: one
 * concrete `state`, one `severity`, one `device_id`. "Open" is not a state
 * the route knows, and this file does not invent it either.
 */

import { ApiError } from "../../lib/api";
import type { FixtureMap } from "../../fixtures/client";
import { devices } from "../../fixtures/core";
import type {
  AlertActionOut,
  AlertDetailOut,
  AlertOut,
  ApprovedActionOut,
  AssignmentOut,
  CheckOut,
  CheckTypeOut,
  DeviceCheckStateOut,
  DeviceHealthOut,
  ProfileOut,
  SettingsOut,
} from "./types";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();
const ahead = (ms: number): string => new Date(Date.now() + ms).toISOString();

/* --------------------------------------------------------------- profiles */

const PROFILES: ProfileOut[] = [
  { id: "hp-1", name: "Fleet baseline", description: "Every enrolled device, unless a narrower profile resolves first.", tick_interval_s: 300, posture_interval_s: 3600, summary_interval_s: 1800, active: true, policy_version: 3, check_count: 7, assignment_count: 1, created_at: ago(120 * DAY), updated_at: ago(9 * DAY) },
  { id: "hp-2", name: "Servers", description: "Tighter disk and service checks for the server group.", tick_interval_s: 120, posture_interval_s: 1800, summary_interval_s: 900, active: true, policy_version: 5, check_count: 4, assignment_count: 1, created_at: ago(90 * DAY), updated_at: ago(2 * DAY) },
  { id: "hp-3", name: "Kiosks", description: "Retired with the last kiosk.", tick_interval_s: 600, posture_interval_s: 7200, summary_interval_s: 3600, active: false, policy_version: 2, check_count: 0, assignment_count: 0, created_at: ago(300 * DAY), updated_at: ago(60 * DAY) },
];

const ASSIGNMENTS: AssignmentOut[] = [
  { id: "ha-1", profile_id: "hp-1", profile_name: "Fleet baseline", device_id: null, device_group_id: null, level: "org", created_by: "usr-helgar", created_at: ago(120 * DAY) },
  { id: "ha-2", profile_id: "hp-2", profile_name: "Servers", device_id: null, device_group_id: "grp-servers", level: "group", created_by: "usr-helgar", created_at: ago(90 * DAY) },
];

const CHECK_TYPES: CheckTypeOut[] = [
  { name: "cpu.busy_pct", value_kind: "numeric", cadence: "tick", evaluated_by: "agent", os_families: ["windows", "linux", "macos"], legal_comparators: ["gt", "gte"], selector_meaning: "", enum_values: [] },
  { name: "memory.used_pct", value_kind: "numeric", cadence: "tick", evaluated_by: "agent", os_families: ["windows", "linux", "macos"], legal_comparators: ["gt", "gte"], selector_meaning: "", enum_values: [] },
  { name: "disk.free_pct", value_kind: "numeric", cadence: "tick", evaluated_by: "agent", os_families: ["windows", "linux", "macos"], legal_comparators: ["lt", "lte"], selector_meaning: "Mount point or drive letter", enum_values: [] },
  { name: "agent.offline", value_kind: "numeric", cadence: "posture", evaluated_by: "server", os_families: ["windows", "linux", "macos"], legal_comparators: ["gt"], selector_meaning: "", enum_values: [] },
  { name: "posture.antivirus_healthy", value_kind: "boolean", cadence: "posture", evaluated_by: "agent", os_families: ["windows"], legal_comparators: ["is"], selector_meaning: "", enum_values: [] },
  { name: "service.running", value_kind: "enum", cadence: "tick", evaluated_by: "agent", os_families: ["windows", "linux"], legal_comparators: ["is"], selector_meaning: "Service name", enum_values: ["running", "stopped"] },
  { name: "cert.days_left", value_kind: "numeric", cadence: "posture", evaluated_by: "server", os_families: ["windows", "linux", "macos"], legal_comparators: ["lt", "lte"], selector_meaning: "", enum_values: [] },
];

interface CheckSeed {
  id: string;
  profile: string;
  type: string;
  selector?: string;
  comparator: CheckOut["comparator"];
  num?: number;
  bool?: boolean;
  display: string;
  ticks: number;
  severity: CheckOut["severity"];
  os?: CheckOut["os_family"];
  notify?: boolean;
  ticket?: boolean;
  remediate?: string | null;
}

const CHECKS: CheckSeed[] = [
  { id: "hc-1", profile: "hp-1", type: "cpu.busy_pct", comparator: "gt", num: 90, display: "90", ticks: 3, severity: "warning" },
  { id: "hc-2", profile: "hp-1", type: "memory.used_pct", comparator: "gt", num: 88, display: "88", ticks: 3, severity: "warning" },
  { id: "hc-3", profile: "hp-1", type: "disk.free_pct", selector: "C:", comparator: "lt", num: 10, display: "10", ticks: 1, severity: "warning", os: "windows", ticket: true },
  { id: "hc-4", profile: "hp-1", type: "agent.offline", comparator: "gt", num: 900, display: "900", ticks: 1, severity: "warning", ticket: true },
  { id: "hc-5", profile: "hp-1", type: "posture.antivirus_healthy", comparator: "is", bool: true, display: "true", ticks: 1, severity: "info" },
  { id: "hc-6", profile: "hp-1", type: "service.running", selector: "spooler", comparator: "is", display: "running", ticks: 2, severity: "info", os: "windows", remediate: "act-2" },
  { id: "hc-7", profile: "hp-1", type: "cert.days_left", comparator: "lt", num: 14, display: "14", ticks: 1, severity: "info", notify: false },
  { id: "hc-8", profile: "hp-2", type: "disk.free_pct", selector: "/var/lib/postgresql", comparator: "lt", num: 5, display: "5", ticks: 1, severity: "critical", ticket: true, remediate: "act-3" },
  { id: "hc-9", profile: "hp-2", type: "cpu.busy_pct", comparator: "gt", num: 95, display: "95", ticks: 5, severity: "warning" },
  { id: "hc-10", profile: "hp-2", type: "memory.used_pct", comparator: "gt", num: 92, display: "92", ticks: 5, severity: "warning" },
  { id: "hc-11", profile: "hp-2", type: "service.running", selector: "postgresql", comparator: "is", display: "running", ticks: 1, severity: "critical", os: "linux", ticket: true },
];

function check(seed: CheckSeed): CheckOut {
  const profile = PROFILES.find((p) => p.id === seed.profile) ?? PROFILES[0]!;
  const type = CHECK_TYPES.find((t) => t.name === seed.type) ?? CHECK_TYPES[0]!;
  return {
    id: seed.id,
    profile_id: seed.profile,
    check_type: seed.type,
    selector: seed.selector ?? "",
    comparator: seed.comparator,
    threshold_num: seed.num ?? null,
    threshold_bool: seed.bool ?? null,
    threshold_display: seed.display,
    for_ticks: seed.ticks,
    for_seconds: seed.ticks * profile.tick_interval_s,
    tick_interval_s: profile.tick_interval_s,
    severity: seed.severity,
    os_family: seed.os ?? null,
    enabled: true,
    declares_notify: seed.notify ?? true,
    declares_open_ticket: seed.ticket ?? false,
    remediate_approved_action_id: seed.remediate ?? null,
    value_kind: type.value_kind,
    cadence: type.cadence,
    evaluated_by: type.evaluated_by,
    available_os_families: type.os_families,
    selector_meaning: type.selector_meaning,
    created_at: profile.created_at,
    updated_at: profile.updated_at,
  };
}

/* ------------------------------------------------------- approved actions */

const APPROVED_ACTIONS: ApprovedActionOut[] = [
  { id: "act-1", key: "collect-inventory", name: "Collect inventory", description: "Re-run the inventory sweep on one device.", job_type: "inventory.collect", params_template: {}, params_template_redacted_keys: [], script_id: null, script_name: null, script_version: null, script_live_version: null, risk_class: "low", destructive: false, max_target_scope: "device", autonomy: "require_approval", personas: ["health"], cooldown_s: 3600, max_per_device_per_day: 3, max_per_org_per_hour: 20, active: true, script_exemption: null, created_by: "usr-helgar", created_at: ago(100 * DAY), updated_at: ago(30 * DAY) },
  { id: "act-2", key: "restart-spooler", name: "Restart the print spooler", description: "Stops and starts the Windows spooler service.", job_type: "script.run", params_template: { script_id: "scr-spooler" }, params_template_redacted_keys: [], script_id: "scr-spooler", script_name: "Restart Spooler", script_version: 4, script_live_version: 4, risk_class: "medium", destructive: false, max_target_scope: "device", autonomy: "autonomous", personas: ["health"], cooldown_s: 1800, max_per_device_per_day: 4, max_per_org_per_hour: 30, active: true, script_exemption: { id: "ex-1", script_id: "scr-spooler", script_name: "Restart Spooler", script_version: 4, reason: "Read the body: it only restarts one service and touches no data.", granted_by: "usr-helgar", granted_at: ago(40 * DAY) }, created_by: "usr-helgar", created_at: ago(60 * DAY), updated_at: ago(40 * DAY) },
  { id: "act-3", key: "vacuum-postgres", name: "Vacuum and checkpoint PostgreSQL", description: "Reclaims WAL and dead tuples on the database primary.", job_type: "script.run", params_template: { script_id: "scr-vacuum" }, params_template_redacted_keys: [], script_id: "scr-vacuum", script_name: "Vacuum and checkpoint", script_version: 2, script_live_version: 3, risk_class: "high", destructive: true, max_target_scope: "device", autonomy: "require_approval", personas: ["health"], cooldown_s: 21600, max_per_device_per_day: 1, max_per_org_per_hour: 2, active: true, script_exemption: null, created_by: "usr-tiago", created_at: ago(45 * DAY), updated_at: ago(3 * DAY) },
  { id: "act-4", key: "clear-temp", name: "Clear the temporary folder", description: "Offered to an operator; never runs on its own.", job_type: "fs.clean_temp", params_template: { older_than_days: 7 }, params_template_redacted_keys: [], script_id: null, script_name: null, script_version: null, script_live_version: null, risk_class: "low", destructive: false, max_target_scope: "device", autonomy: "suggest", personas: ["health"], cooldown_s: 86400, max_per_device_per_day: 1, max_per_org_per_hour: 10, active: false, script_exemption: null, created_by: "usr-helgar", created_at: ago(80 * DAY), updated_at: ago(80 * DAY) },
];

/* ----------------------------------------------------------------- alerts */

interface AlertSeed {
  id: string;
  device: string;
  check: string;
  severity: AlertOut["severity"];
  state: AlertOut["state"];
  observed: string;
  /** How long ago it opened. */
  age: number;
  seen?: number;
  occurrences: number;
  flaps?: number;
  ackBy?: string;
  muteFor?: number;
  cleared?: { reason: NonNullable<AlertOut["cleared_reason"]>; after: number; by?: string };
  ticket?: string;
  escalated?: boolean;
}

/**
 * Twelve open (the summary's nine active, two acknowledged, one muted; one
 * critical, eight warning, three info) and five cleared: two the machine
 * recovered, one a person closed, one whose sensor went away and one whose
 * check was removed -- the three §7.4 treats differently.
 */
const ALERTS: AlertSeed[] = [
  { id: "al-1", device: "dev-6", check: "hc-8", severity: "critical", state: "active", observed: "3.1%", age: 2 * DAY + 4 * HOUR, seen: 2 * MINUTE, occurrences: 1_580, ticket: "tkt-4125", escalated: true },
  { id: "al-2", device: "dev-5", check: "hc-9", severity: "warning", state: "active", observed: "96", age: 41 * MINUTE, seen: MINUTE, occurrences: 20 },
  { id: "al-3", device: "dev-2", check: "hc-1", severity: "warning", state: "active", observed: "93", age: 3 * HOUR, seen: 4 * MINUTE, occurrences: 36 },
  { id: "al-4", device: "dev-5", check: "hc-10", severity: "warning", state: "active", observed: "91", age: 5 * HOUR, seen: MINUTE, occurrences: 150 },
  { id: "al-5", device: "dev-7", check: "hc-2", severity: "warning", state: "active", observed: "90", age: DAY, seen: 3 * MINUTE, occurrences: 288 },
  { id: "al-6", device: "dev-8", check: "hc-4", severity: "warning", state: "active", observed: "26h", age: 26 * HOUR, seen: 5 * MINUTE, occurrences: 26, ticket: "tkt-4120", escalated: true },
  { id: "al-7", device: "dev-4", check: "hc-4", severity: "warning", state: "active", observed: "3d 2h", age: 3 * DAY + 2 * HOUR, seen: 5 * MINUTE, occurrences: 74, escalated: true },
  { id: "al-8", device: "dev-1", check: "hc-6", severity: "info", state: "active", observed: "stopped", age: 20 * MINUTE, seen: 2 * MINUTE, occurrences: 4, flaps: 4 },
  { id: "al-9", device: "dev-3", check: "hc-7", severity: "info", state: "active", observed: "9", age: 5 * DAY, seen: 30 * MINUTE, occurrences: 5 },
  { id: "al-10", device: "dev-1", check: "hc-1", severity: "warning", state: "acknowledged", observed: "92", age: 6 * HOUR, seen: 2 * MINUTE, occurrences: 70, ackBy: "usr-tiago" },
  { id: "al-11", device: "dev-2", check: "hc-3", severity: "warning", state: "acknowledged", observed: "8%", age: 2 * DAY, seen: 4 * MINUTE, occurrences: 570, ackBy: "usr-helgar", ticket: "tkt-4118" },
  { id: "al-12", device: "dev-3", check: "hc-5", severity: "info", state: "muted", observed: "false", age: 4 * DAY, seen: 60 * MINUTE, occurrences: 96, muteFor: 2 * DAY },
  { id: "al-13", device: "dev-1", check: "hc-1", severity: "warning", state: "cleared", observed: "91", age: 5 * DAY, occurrences: 4, cleared: { reason: "recovery", after: 20 * MINUTE } },
  { id: "al-14", device: "dev-6", check: "hc-10", severity: "warning", state: "cleared", observed: "93", age: 4 * DAY, occurrences: 60, cleared: { reason: "recovery", after: 2 * HOUR } },
  { id: "al-15", device: "dev-9", check: "hc-3", severity: "warning", state: "cleared", observed: "6%", age: 9 * DAY, occurrences: 280, cleared: { reason: "manual", after: DAY, by: "usr-helgar" } },
  { id: "al-16", device: "dev-14", check: "hc-5", severity: "info", state: "cleared", observed: "", age: 12 * DAY, occurrences: 1, cleared: { reason: "unavailable_timeout", after: DAY } },
  { id: "al-17", device: "dev-7", check: "hc-6", severity: "info", state: "cleared", observed: "stopped", age: 14 * DAY, occurrences: 12, cleared: { reason: "policy_removed", after: DAY } },
];

function alertOut(seed: AlertSeed): AlertOut {
  const device = devices().find((d) => d.id === seed.device);
  const c = check(CHECKS.find((x) => x.id === seed.check) ?? CHECKS[0]!);
  const opened = Date.now() - seed.age;
  const clearedAt = seed.cleared ? opened + seed.cleared.after : null;
  return {
    id: seed.id,
    device_id: seed.device,
    device_hostname: device?.hostname ?? seed.device,
    device_group_id: device?.device_group_id ?? null,
    check_id: c.id,
    profile_id: c.profile_id,
    check_type: c.check_type,
    selector: c.selector,
    severity: seed.severity,
    state: seed.state,
    observed_value: seed.observed,
    threshold_display: `${c.comparator} ${c.threshold_display}`,
    opened_at: new Date(opened).toISOString(),
    opened_policy_version: PROFILES.find((p) => p.id === c.profile_id)?.policy_version ?? 1,
    last_seen_at: clearedAt ? new Date(clearedAt).toISOString() : ago(seed.seen ?? 5 * MINUTE),
    occurrence_count: seed.occurrences,
    flapping: (seed.flaps ?? 0) > 0,
    flap_count: seed.flaps ?? 0,
    escalated_at: seed.escalated ? new Date(opened + HOUR).toISOString() : null,
    acknowledged_at: seed.ackBy ? new Date(opened + 25 * MINUTE).toISOString() : null,
    acknowledged_by: seed.ackBy ?? null,
    muted_until: seed.muteFor ? ahead(seed.muteFor) : null,
    muted_by: seed.muteFor ? "usr-helgar" : null,
    cleared_at: clearedAt ? new Date(clearedAt).toISOString() : null,
    cleared_reason: seed.cleared?.reason ?? null,
    cleared_by: seed.cleared?.by ?? null,
    ticket_id: seed.ticket ?? null,
    previous_alert_id: seed.id === "al-10" ? "al-13" : null,
  };
}

const alerts = (): AlertOut[] => ALERTS.map(alertOut);

const listHealthAlerts: FixtureMap["listHealthAlerts"] = (filters = {}) =>
  alerts()
    .filter(
      (a) =>
        (!filters.state || a.state === filters.state) &&
        (!filters.severity || a.severity === filters.severity) &&
        (!filters.device_id || a.device_id === filters.device_id),
    )
    .sort((a, b) => Date.parse(b.opened_at) - Date.parse(a.opened_at));

/** What each alert's check declared, as the rows the detail view shows. */
function actionsFor(alert: AlertOut): AlertActionOut[] {
  const base = {
    alert_id: alert.id,
    approved_action_id: null,
    approved_action_key: null,
    approved_action_name: null,
    script_id: null,
    script_name: null,
    script_version: null,
    autonomy: null,
    risk_class: null,
    destructive: null,
    job_id: null,
    ticket_id: null,
    attempts: 1,
    next_attempt_at: null,
    approved_by: null,
    approved_at: null,
    expires_at: null,
    last_error: null,
    created_at: alert.opened_at,
    updated_at: alert.last_seen_at,
  };
  const rows: AlertActionOut[] = [{ ...base, id: `${alert.id}-notify`, kind: "notify", state: "done" }];
  if (alert.ticket_id) {
    rows.push({ ...base, id: `${alert.id}-ticket`, kind: "open_ticket", state: "done", ticket_id: alert.ticket_id });
  }
  if (alert.id === "al-1") {
    const act = APPROVED_ACTIONS[2]!;
    rows.push({
      ...base,
      id: "al-1-remediate",
      kind: "remediate",
      state: "awaiting_approval",
      approved_action_id: act.id,
      approved_action_key: act.key,
      approved_action_name: act.name,
      script_id: act.script_id,
      script_name: act.script_name,
      script_version: act.script_version,
      autonomy: act.autonomy,
      risk_class: act.risk_class,
      destructive: act.destructive,
      attempts: 0,
      expires_at: ahead(50 * MINUTE),
    });
  }
  if (alert.id === "al-8") {
    const act = APPROVED_ACTIONS[1]!;
    rows.push({
      ...base,
      id: "al-8-remediate",
      kind: "remediate",
      state: "blocked_flapping",
      approved_action_id: act.id,
      approved_action_key: act.key,
      approved_action_name: act.name,
      script_id: act.script_id,
      script_name: act.script_name,
      script_version: act.script_version,
      autonomy: act.autonomy,
      risk_class: act.risk_class,
      destructive: act.destructive,
      last_error: "flapping: 4 opens in the last 3600 s (threshold 3); actions suppressed until the cooldown ends",
    });
  }
  return rows;
}

const getHealthAlert: FixtureMap["getHealthAlert"] = (id): AlertDetailOut => {
  const alert = alerts().find((a) => a.id === id);
  if (!alert) throw new ApiError(404, JSON.stringify({ detail: "alert not found" }));
  const previous = alert.previous_alert_id ? alerts().find((a) => a.id === alert.previous_alert_id) : undefined;
  return {
    ...alert,
    profile_name: PROFILES.find((p) => p.id === alert.profile_id)?.name ?? "Fleet baseline",
    ticket_number: alert.ticket_id ? `T-${alert.ticket_id.replace("tkt-", "").padStart(6, "0")}` : null,
    previous_alert_ticket_number: previous?.ticket_id ? `T-${previous.ticket_id.replace("tkt-", "").padStart(6, "0")}` : null,
    previous_alert_opened_at: previous?.opened_at ?? null,
    monitoring_deep_link: null,
    actions: actionsFor(alert),
  };
};

/* --------------------------------------------------------------- settings */

const getHealthSettings: FixtureMap["getHealthSettings"] = (): SettingsOut => ({
  flap_window_s: 3600,
  flap_threshold: 3,
  flap_cooldown_s: 3600,
  unavailable_clear_after_s: 86_400,
  alert_retention_days: 400,
  escalate_after_s: 3600,
  offline_after_s: 900,
  approval_ttl_s: 3600,
  default_alert_queue_id: "q-infra",
  default_ticket_category_id: "cat-incident",
  monitoring_deep_link_template: null,
  auto_resolve_alert_tickets: true,
  configured: true,
  alert_tickets_enabled: true,
  updated_at: ago(9 * DAY),
  updated_by: "usr-helgar",
});

/* ---------------------------------------------------------- device health */

function profileFor(deviceId: string): ProfileOut | null {
  const device = devices().find((d) => d.id === deviceId);
  if (!device || device.agent_status === null) return null;
  return device.device_group_id === "grp-servers" ? PROFILES[1]! : PROFILES[0]!;
}

const getDeviceHealth: FixtureMap["getDeviceHealth"] = (deviceId): DeviceHealthOut => {
  const device = devices().find((d) => d.id === deviceId);
  if (!device) throw new ApiError(404, JSON.stringify({ detail: "device not found" }));
  const profile = profileFor(deviceId);
  const assignment = profile ? (ASSIGNMENTS.find((a) => a.profile_id === profile.id) ?? null) : null;
  const open = alerts().filter((a) => a.device_id === deviceId && a.state !== "cleared");
  const online = device.agent_status === "online";
  const checks: DeviceCheckStateOut[] = profile
    ? CHECKS.filter((c) => c.profile === profile.id)
        .map(check)
        .filter((c) => c.os_family === null || c.os_family === device.os_family)
        .map((c) => {
          const breaching = open.some((a) => a.check_id === c.id);
          const unavailable = !c.available_os_families.includes(device.os_family ?? "windows");
          const state = breaching ? "breaching" : unavailable ? "unavailable" : online ? "ok" : "unknown";
          return {
            check_id: c.id,
            check_type: c.check_type,
            selector: c.selector,
            comparator: c.comparator,
            threshold_display: c.threshold_display,
            severity: c.severity,
            for_ticks: c.for_ticks,
            for_seconds: c.for_seconds,
            evaluated_by: c.evaluated_by,
            state,
            last_value: state === "ok" ? (c.value_kind === "numeric" ? "41" : c.value_kind === "boolean" ? "true" : "running") : open.find((a) => a.check_id === c.id)?.observed_value ?? "",
            policy_version: state === "unknown" ? null : profile.policy_version,
            reported_at: state === "unknown" ? null : device.last_seen_at,
          };
        })
    : [];
  return {
    device_id: deviceId,
    hostname: device.hostname ?? deviceId,
    os_family: device.os_family,
    has_profile: profile !== null,
    profile,
    assignment,
    resolution_level: assignment?.level ?? null,
    acknowledged_policy_version: profile ? (online ? profile.policy_version : profile.policy_version - 1) : null,
    policy_drift: profile !== null && !online,
    checks,
    open_alerts: open,
    agent_last_seen_at: device.last_seen_at,
    agent_online: online,
    offline_after_s: 900,
  };
};

/* --------------------------------------------------------------- registry */

const fixtures: FixtureMap = {
  listHealthAlerts,
  getHealthAlert,
  listHealthProfiles: () => PROFILES,
  listHealthAssignments: (profileId) =>
    profileId ? ASSIGNMENTS.filter((a) => a.profile_id === profileId) : ASSIGNMENTS,
  getHealthProfileChecks: (id) => {
    const profile = PROFILES.find((p) => p.id === id);
    if (!profile) throw new ApiError(404, JSON.stringify({ detail: "profile not found" }));
    return {
      profile_id: id,
      policy_version: profile.policy_version,
      tick_interval_s: profile.tick_interval_s,
      checks: CHECKS.filter((c) => c.profile === id).map(check),
    };
  },
  listHealthCheckTypes: () => CHECK_TYPES,
  getHealthSettings,
  listApprovedActions: () => APPROVED_ACTIONS,
  getDeviceHealth,
};

export default fixtures;
