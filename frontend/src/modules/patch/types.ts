// Mirrors backend/app/modules/patch/api.py response/request models exactly.
// No secret fields — the patch module has none (patch apply runs with the
// agent's local privilege, never a vaulted credential).
//
// "EXACTLY" IS NOW ENFORCED, because it was not true. `DeviceStatus` was
// missing `last_scan_at` and `FleetReport` was missing `last_scan_at`,
// `devices_scanned` and `devices_total` — all four had been in the responses
// since 2026-08-21, and a field TypeScript is never told about is a fact the
// console cannot show. `backend/tests/test_patch_type_parity.py` compares the
// field names of every model here against the Pydantic models, both ways, and
// requires the mapping to be complete: a response model with no interface, or
// an interface with a field the API does not send, fails there. Interfaces
// that describe query strings rather than bodies are listed in that file's
// TS_ONLY_INTERFACES.

export interface Patch {
  id: string;
  os_family: string;
  source: string;
  identifier: string;
  title: string;
  severity: string;
  kb: string | null;
  cve: string[] | null;
  superseded_by: string | null;
  priority: number | null;
  first_seen_at: string;
}

export interface PatchListResponse {
  items: Patch[];
  total: number;
}

export interface DeviceStatus {
  device_id: string;
  counts_by_state: Record<string, number>;
  counts_by_severity: Record<string, number>;
  pending_reboot: boolean;
  /** When this device last COMPLETED a scan; `null` = never. Without it the
   *  counts above are unreadable — an all-zero `counts_by_state` is the same
   *  shape on a fully patched machine and on one that has never answered. */
  last_scan_at: string | null;
  /** The device's latest restart record; null if it never owed one. */
  restart?: PatchRestart | null;
  /** What the latest scan said about winget / Homebrew; null where the OS has
   *  no app package manager or the agent predates it. */
  app_sources?: PatchAppSource[] | null;
  /** When a restart asked for now would happen. */
  restart_window?: PatchRestartWindow | null;
}

/** When the restart sweep would send a restart booked now. */
export interface PatchRestartWindow {
  open_now: boolean;
  /** Now while the window is open; null when it does not open within eight days. */
  opens_at: string | null;
  /** Notice a signed-in user on a workstation gets. Anyone else: five minutes. */
  user_warning_minutes: number;
}

/** `POST /patch/devices/{id}/restart`. */
export interface PatchRestartRequestResponse {
  restart: PatchRestart;
  /** A restart was already booked; nothing new was recorded. */
  already_requested: boolean;
  window: PatchRestartWindow;
}

/** One app package manager on one device. */
export interface PatchAppSource {
  source: string;
  /** scanned, unsupported or failed. */
  state: string;
  detail: string | null;
}

export interface PatchAppSourceSummary {
  source: string;
  state: string;
  devices: number;
  detail: string | null;
}

export interface PatchAppSourceSummaryResponse {
  items: PatchAppSourceSummary[];
}

/** A restart a device owes after patching. */
export interface PatchRestart {
  id: string;
  device_id: string;
  /** pending (waiting for the maintenance window), dispatched, scheduled,
   *  completed, cancelled or failed. */
  state: string;
  /** "apply" or "scan" (what found the restart was needed), or "operator"
   *  (someone asked for it from the device page). */
  origin: string;
  scheduled_for: string | null;
  user_present: boolean | null;
  reason: string | null;
  created_at: string;
  closed_at: string | null;
}

export interface PatchRestartListItem {
  id: string;
  device_id: string;
  state: string;
  origin: string;
  scheduled_for: string | null;
  user_present: boolean | null;
  reason: string | null;
  created_at: string;
  closed_at: string | null;
  hostname: string | null;
}

export interface PatchRestartListResponse {
  items: PatchRestartListItem[];
  total: number;
}

export interface ScanRequest {
  device_ids?: string[] | null;
}

export interface ScanResponse {
  job_ids: string[];
}

export interface ApprovalCreateRequest {
  patch_id: string;
  ring_name: string;
  /** An approved change this approval implements; required where a change
   *  gate covers the ring. */
  change_id?: string | null;
  /** The client whose machines in the ring this clears. Absent: every
   *  client's (a global approval), which needs access to every client. */
  client_id?: string;
}

/**
 * One approval of one patch into one ring.
 *
 * `id` IS THE VALUE `DELETE /patch/approvals/{approval_id}` TAKES, and until
 * `GET /patch/approvals` shipped it was reachable from exactly one place —
 * the POST response that created the approval. `revoked` is `true` for one
 * that has been withdrawn: the row survives the revoke (spec §5) so the trail
 * holds, which is why anything rendering this list must show a revoked
 * approval AS revoked rather than filtering it out. `approved_by` is `null`
 * for a policy auto-approval (PD-02), not for an unknown actor.
 */
export interface PatchApproval {
  id: string;
  /** Whose machines it clears: a client's, or `null` for every client's. */
  client_id: string | null;
  patch_id: string;
  /**
   * The update this approval clears, NAMED — `KB5031354`, `openssl-3.0.11`.
   *
   * `null` means this organization has no `patches` row for `patch_id`, which
   * is a fact rather than a formatting gap (see `approve.ApprovalListing`
   * server-side). Anything rendering it falls back to `patch_id`, because the
   * id is the only handle on such a row and `DELETE /patch/approvals/{id}`
   * still has to be able to reach it.
   */
  patch_identifier: string | null;
  ring_name: string;
  approved_by: string | null;
  approved_at: string;
  revoked: boolean;
  revoked_by: string | null;
  revoked_at: string | null;
}

/** `total` is the count matching the filters BEFORE limit/offset, so a short
 *  page can be told from the end of the list. */
export interface ApprovalListResponse {
  items: PatchApproval[];
  total: number;
}

/** Mirrors the route's query parameters. `revoked` is deliberately optional
 *  and deliberately unset by default: omitting it lists revoked approvals
 *  alongside live ones, which is the only shape that agrees with the audit
 *  log about what was ever approved. */
export interface ApprovalListFilters {
  patch_id?: string;
  ring?: string;
  revoked?: boolean;
  limit?: number;
  offset?: number;
}

/**
 * One ring decision over a SELECTION of patches.
 *
 * `patch_ids` IS THE LIST THE OPERATOR TICKED, not a filter the server
 * re-evaluates. A filter ("everything critical") is resolved after the screen
 * was read, so the set that gets cleared is not the set that was looked at.
 */
export interface BulkApprovalCreateRequest {
  patch_ids: string[];
  ring_name: string;
  change_id?: string | null;
  /** As on `ApprovalCreateRequest`: absent is every client's. */
  client_id?: string;
}

/**
 * What a bulk approval actually did.
 *
 * `newly_approved` and `already_in_force` are separate because they are
 * different facts about the audit log: a patch already cleared for this ring
 * wrote no event, so reporting the batch size as approvals would describe
 * writes that never happened.
 */
export interface BulkApprovalResponse {
  items: PatchApproval[];
  total: number;
  newly_approved: number;
  already_in_force: number;
}

/**
 * One deployment ring and the machines it reaches — the blast radius of an
 * approval, since clearing a patch for a ring clears it for every device that
 * resolves into that ring now and at every future scan.
 */
/** How many of one ring's machines are one client's. */
export interface PatchRingClientCount {
  client_id: string;
  device_count: number;
}

export interface PatchRing {
  ring_name: string;
  ring_order: number;
  device_count: number;
  /** Of `device_count`, how many are each client's, one row per client with a
   *  machine in the ring; a client absent has none. A client's approval
   *  reaches only its own, a global one all of them (MSP clients plan
   *  2026-10-26 Task 14 review). Rows rather than a map: a response with an
   *  open key space can never be an AI read tool. */
  client_device_counts: PatchRingClientCount[];
}

/** The rings partition the fleet, so `sum(device_count) === device_total`. */
export interface PatchRingListResponse {
  items: PatchRing[];
  total: number;
  device_total: number;
}

export interface ApplyRequest {
  device_ids?: string[] | null;
}

export interface ApplyResponse {
  job_ids: string[];
}

/** The reboot behaviours a patch policy can name (the wire vocabulary). */
export type PatchReboot = "never" | "if_required" | "forced";

export interface PatchPolicy {
  id: string;
  device_group_id: string | null;
  ring_name: string;
  ring_order: number;
  maintenance_window: string | null;
  auto_approve_security: boolean;
  reboot: string;
  reboot_deferral_hours: number;
  scan_interval_hours: number;
}

export interface PatchPolicyInput {
  device_group_id?: string | null;
  ring_name: string;
  ring_order: number;
  maintenance_window?: string | null;
  auto_approve_security: boolean;
  reboot: string;
  reboot_deferral_hours: number;
  scan_interval_hours: number;
}

export interface Exposure {
  device_id: string;
  hostname: string;
  patch_id: string;
  identifier: string;
  severity: string;
  priority: number | null;
  state: string;
}

export interface FleetReport {
  compliant_pct_by_os_family: Record<string, number>;
  compliant_pct_by_severity: Record<string, number>;
  /** One entry per non-compliant (device, patch) pair — a findings list.
   *
   *  UNCAPPED, AND NO LONGER RENDERED. This is the combinatorial product the
   *  operator reported on 2026-08-25: one machine missing 300 packages is 300
   *  entries. `PatchDeviceRollup` and `PatchUpdate` are what the page reads
   *  now; this stays because the compliance percentages beside it are computed
   *  from the same query. */
  exposures: Exposure[];
  /** Distinct devices with at least one finding, counted server-side. */
  exposed_device_count: number;
  critical_device_count: number;
  /** WHEN, and OVER HOW MUCH OF THE FLEET, everything above was measured. An
   *  empty `exposures` is the same shape on a clean fleet and on one that has
   *  never been scanned; these three say which. */
  last_scan_at: string | null;
  devices_scanned: number;
  devices_total: number;
}

/**
 * One catalogued update, aggregated across the fleet — the primary object of
 * the fleet patch view.
 *
 * `devices_outstanding` IS THE NUMBER THAT MAKES THE ROW ACTIONABLE, and it is
 * the one `GET /patch/patches` could not say: an update missing on one machine
 * and one missing on forty rendered identically. It counts distinct devices in
 * a non-compliant state (`missing` + `approved` + `failed`) — cleared-but-not-
 * installed and tried-and-not-installed are still a patch the machine lacks.
 *
 * `cleared_rings` is a property of the row rather than a browser-side join
 * against a capped approvals page, which is what this console used to do and
 * had to disclaim. Revoked approvals are absent: it answers what is in force.
 */
export interface PatchUpdate {
  id: string;
  os_family: string;
  source: string;
  identifier: string;
  title: string;
  severity: string;
  kb: string | null;
  cve: string[] | null;
  priority: number | null;
  first_seen_at: string;
  devices_missing: number;
  devices_approved: number;
  devices_failed: number;
  devices_installed: number;
  devices_outstanding: number;
  cleared_rings: string[];
  /**
   * THE OUTCOME ON ONE MACHINE — populated only when the query named a device
   * (`device_id`), `null` otherwise.
   *
   * A device-scoped row without these cannot say WHY a patch is still
   * outstanding: `failed` and `missing` both mean "the machine does not have
   * it", so an apply that errored rendered exactly like one that was never
   * dispatched. Fleet-wide they would be an aggregate over machines with
   * different outcomes, which states nothing, so the server sends `null`.
   */
  device_state: string | null;
  device_last_error: string | null;
  device_applied_at: string | null;
}

/**
 * A page of updates, and everything needed to read the page honestly.
 *
 * `total` matches every filter; `total_unfiltered` matches them all EXCEPT
 * severity, and `severity_counts` breaks that set down. The three travel
 * together because this console defaults to the severities that demand a
 * decision — and a filtered count that reads as a total is worse than the long
 * list it replaced. `sum(severity_counts) === total_unfiltered`.
 */
export interface PatchUpdateListResponse {
  items: PatchUpdate[];
  total: number;
  total_unfiltered: number;
  severity_counts: Record<string, number>;
  last_scan_at: string | null;
  devices_scanned: number;
  devices_total: number;
}

/** Mirrors `GET /patch/updates`' query parameters. `severity` is repeatable
 *  and an empty array clears the filter rather than matching nothing. */
export interface PatchUpdateFilters {
  /** "os" for operating-system updates, "apps" for winget / Homebrew. */
  kind?: "os" | "apps";
  severity?: string[];
  os_family?: string;
  source?: string;
  ring?: string;
  device_id?: string;
  q?: string;
  outstanding_only?: boolean;
  sort?: string;
  limit?: number;
  offset?: number;
}

/**
 * One device that has something outstanding.
 *
 * `outstanding` is a TOTAL and is never severity-filtered, which is what makes
 * the update list's severity default safe: a machine whose only outstanding
 * update is unrated still visibly carries something here.
 */
export interface PatchDeviceRollup {
  device_id: string;
  /** The machine's client: an approval made from its drill-down is filed
   *  under it (the operator's rule of 2026-10-28). */
  client_id: string;
  hostname: string;
  os_family: string;
  outstanding: number;
  /** How many of `outstanding` were TRIED AND DID NOT TAKE. A failed row is
   *  still outstanding — the machine lacks the patch — so without this the
   *  number folds an errored apply into one that never ran. */
  failed: number;
  worst_severity: string | null;
  counts_by_severity: Record<string, number>;
  last_scan_at: string | null;
}

/** `total` is machines that need something; `devices_total` is the fleet. A
 *  count of 1 says nothing about whether that is the whole estate. */
export interface PatchDeviceRollupListResponse {
  items: PatchDeviceRollup[];
  total: number;
  last_scan_at: string | null;
  devices_scanned: number;
  devices_total: number;
}

export interface PatchDeviceRollupFilters {
  q?: string;
  os_family?: string;
  limit?: number;
  offset?: number;
}

/** One update that did not install, and the agent's own words for why. */
export interface PatchRunFailure {
  source: string | null;
  identifier: string | null;
  error: string | null;
}

/**
 * One apply RUN — the record that a dispatch happened and what came back.
 *
 * `finished_at === null` means the job is still out: the counts are zero and
 * say nothing yet, which is a different state from a finished run that
 * installed nothing. Anything rendering this must tell the two apart.
 */
export interface PatchRun {
  id: string;
  job_id: string;
  device_id: string;
  installed: number;
  failed: number;
  reboot_required: boolean;
  started_at: string;
  finished_at: string | null;
  failures: PatchRunFailure[];
}

export interface PatchRunListResponse {
  items: PatchRun[];
}

/** Mirrors `GET /patch/runs`' query parameters. */
export interface PatchRunFilters {
  device_id?: string;
  limit?: number;
}
