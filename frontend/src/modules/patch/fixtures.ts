/**
 * Fixtures for the patch module: the update catalogue, each machine's
 * status against it, the rings, the approvals, the policies and the runs.
 *
 * ONE MATRIX, EVERY NUMBER DERIVED FROM IT. The page's own discipline is that
 * a tile and the table under it read the same response, so the fixture holds
 * one table of (device, patch) -> state and computes every route from it:
 * `listPatchUpdates` aggregates it per patch, `listPatchDeviceRollup` per
 * device, `getPatchFleetReport` per platform and per severity, and
 * `getDevicePatchStatus` for one machine. The counts on `/patch` cannot
 * disagree with each other because there is nothing for them to disagree
 * about. The machines are the shell's worked fleet (`fixtures/core.ts`), so
 * "3 of 3 Linux machines" on this page is the same three the devices page
 * lists.
 *
 * WHAT THE SCANNERS ACTUALLY PUBLISH, reproduced: Microsoft leaves most of
 * the Windows rows unrated (`unknown`), apt marks the security pocket
 * `important` and nothing else, and Apple publishes no severity at all, so
 * the macOS rows are `important` (recommended) or `unknown`. That is why the
 * three tabs' defaults differ, and a fixture that rated everything would hide
 * the difference the tabs exist for.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { devices } from "../../fixtures/core";
import { ApiError } from "../../lib/api";
import type {
  DeviceStatus,
  FleetReport,
  PatchApproval,
  PatchDeviceRollup,
  PatchDeviceRollupListResponse,
  PatchPolicy,
  PatchRestart,
  PatchRestartListItem,
  PatchRestartWindow,
  PatchRun,
  PatchUpdate,
  PatchUpdateListResponse,
} from "./types";

const MINUTE = 60_000;
const HOUR = 60;
const DAY = 24 * HOUR;
/** An ISO timestamp `minutes` before the call. */
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* --------------------------------------------------------------- catalogue */

interface CatalogueRow {
  id: string;
  os_family: "windows" | "linux" | "macos";
  source: string;
  identifier: string;
  title: string;
  severity: string;
  kb: string | null;
  cve: string[] | null;
  priority: number | null;
  firstSeenDays: number;
}

const CATALOGUE: CatalogueRow[] = [
  // Windows: cumulative updates rated by MSRC, the rest left unrated.
  { id: "w-cu-11-24h2", os_family: "windows", source: "wsus", identifier: "KB5041585", title: "2026-08 Cumulative Update for Windows 11 24H2", severity: "critical", kb: "KB5041585", cve: ["CVE-2026-38063", "CVE-2026-38189"], priority: 1, firstSeenDays: 24 },
  { id: "w-cu-10-22h2", os_family: "windows", source: "wsus", identifier: "KB5041580", title: "2026-08 Cumulative Update for Windows 10 22H2", severity: "critical", kb: "KB5041580", cve: ["CVE-2026-38063"], priority: 1, firstSeenDays: 24 },
  { id: "w-cu-srv2022", os_family: "windows", source: "wsus", identifier: "KB5041160", title: "2026-08 Cumulative Update for Windows Server 2022", severity: "important", kb: "KB5041160", cve: ["CVE-2026-38189"], priority: 2, firstSeenDays: 24 },
  { id: "w-net-481", os_family: "windows", source: "wsus", identifier: "KB5042099", title: "2026-08 Security update for .NET Framework 4.8.1", severity: "important", kb: "KB5042099", cve: ["CVE-2026-38167"], priority: 2, firstSeenDays: 22 },
  { id: "w-ssu", os_family: "windows", source: "wsus", identifier: "KB5041571", title: "Servicing stack update for Windows 11 24H2", severity: "moderate", kb: "KB5041571", cve: null, priority: 3, firstSeenDays: 30 },
  { id: "w-defender", os_family: "windows", source: "wsus", identifier: "KB4052623", title: "Microsoft Defender antimalware platform 4.18.26080", severity: "unknown", kb: "KB4052623", cve: null, priority: null, firstSeenDays: 5 },
  { id: "w-msrt", os_family: "windows", source: "wsus", identifier: "KB890830", title: "Malicious Software Removal Tool 5.138", severity: "unknown", kb: "KB890830", cve: null, priority: null, firstSeenDays: 24 },
  { id: "w-edge", os_family: "windows", source: "wsus", identifier: "KB5045230", title: "Microsoft Edge 140 stable channel", severity: "unknown", kb: "KB5045230", cve: null, priority: null, firstSeenDays: 3 },
  // Linux: the security pocket is `important`; everything else apt would
  // upgrade is unrated or low.
  { id: "l-kernel", os_family: "linux", source: "apt", identifier: "linux-image-6.8.0-45-generic", title: "linux-image-6.8.0-45-generic 6.8.0-45.45", severity: "important", kb: null, cve: ["CVE-2026-40910"], priority: 2, firstSeenDays: 9 },
  { id: "l-openssl", os_family: "linux", source: "apt", identifier: "openssl", title: "openssl 3.0.13-0ubuntu3.4", severity: "important", kb: null, cve: ["CVE-2026-41996"], priority: 2, firstSeenDays: 12 },
  { id: "l-libssl3", os_family: "linux", source: "apt", identifier: "libssl3t64", title: "libssl3t64 3.0.13-0ubuntu3.4", severity: "important", kb: null, cve: ["CVE-2026-41996"], priority: 2, firstSeenDays: 12 },
  { id: "l-curl", os_family: "linux", source: "apt", identifier: "curl", title: "curl 8.5.0-2ubuntu10.4", severity: "important", kb: null, cve: ["CVE-2026-45237"], priority: 2, firstSeenDays: 6 },
  { id: "l-sudo", os_family: "linux", source: "apt", identifier: "sudo", title: "sudo 1.9.15p5-3ubuntu5.1", severity: "important", kb: null, cve: ["CVE-2026-32462"], priority: 2, firstSeenDays: 15 },
  { id: "l-systemd", os_family: "linux", source: "apt", identifier: "systemd", title: "systemd 255.4-1ubuntu8.4", severity: "unknown", kb: null, cve: null, priority: null, firstSeenDays: 8 },
  { id: "l-git", os_family: "linux", source: "apt", identifier: "git", title: "git 1:2.43.0-1ubuntu7.2", severity: "unknown", kb: null, cve: null, priority: null, firstSeenDays: 20 },
  { id: "l-python3", os_family: "linux", source: "apt", identifier: "python3.12", title: "python3.12 3.12.3-1ubuntu0.3", severity: "unknown", kb: null, cve: null, priority: null, firstSeenDays: 11 },
  { id: "l-tzdata", os_family: "linux", source: "apt", identifier: "tzdata", title: "tzdata 2026a-0ubuntu0.24.04", severity: "unknown", kb: null, cve: null, priority: null, firstSeenDays: 4 },
  { id: "l-vim", os_family: "linux", source: "apt", identifier: "vim", title: "vim 2:9.1.0016-1ubuntu7.5", severity: "low", kb: null, cve: null, priority: 4, firstSeenDays: 17 },
  // macOS: `softwareupdate` says Recommended or not, and nothing else.
  { id: "m-sequoia", os_family: "macos", source: "softwareupdate", identifier: "macOS Sequoia 15.6.1-24G90", title: "macOS Sequoia 15.6.1", severity: "important", kb: null, cve: ["CVE-2026-43300"], priority: 2, firstSeenDays: 7 },
  { id: "m-safari", os_family: "macos", source: "softwareupdate", identifier: "Safari18.6.1SequoiaAuto-18.6.1", title: "Safari 18.6.1", severity: "important", kb: null, cve: null, priority: 2, firstSeenDays: 7 },
  { id: "m-xprotect", os_family: "macos", source: "softwareupdate", identifier: "XProtectPlistConfigData_10_15-5304", title: "XProtect configuration data 5304", severity: "unknown", kb: null, cve: null, priority: null, firstSeenDays: 1 },
];

const SEVERITY_RANK: Record<string, number> = { critical: 0, important: 1, moderate: 2, low: 3, unknown: 4 };
const rank = (severity: string | null) => (severity == null ? 99 : (SEVERITY_RANK[severity] ?? 4));

/* ------------------------------------------------------------------ matrix */

type State = "missing" | "approved" | "installed" | "failed" | "not_applicable";

interface Cell {
  state: State;
  error?: string;
  appliedDays?: number;
}

interface Scan {
  /** Minutes ago, or null when the machine has never completed a scan. */
  scannedAgo: number | null;
  pendingReboot: boolean;
  cells: Record<string, Cell>;
}

/**
 * Every managed machine in the worked fleet, by id, and what its last scan
 * said about each row it knows. `dev-9` is the retired, revoked laptop: it
 * carries an agent and has never reported, which is the org-wide
 * `devices_scanned < devices_total` gap the page's copy talks about.
 */
const MATRIX: Record<string, Scan> = {
  "dev-1": {
    scannedAgo: 38,
    pendingReboot: false,
    cells: {
      "w-cu-11-24h2": { state: "missing" },
      "w-net-481": { state: "approved" },
      "w-ssu": { state: "installed", appliedDays: 26 },
      "w-defender": { state: "missing" },
      "w-msrt": { state: "installed", appliedDays: 20 },
      "w-edge": { state: "installed", appliedDays: 2 },
    },
  },
  "dev-2": {
    scannedAgo: 52,
    pendingReboot: true,
    cells: {
      "w-cu-11-24h2": { state: "installed", appliedDays: 1 },
      "w-net-481": { state: "installed", appliedDays: 1 },
      "w-ssu": { state: "installed", appliedDays: 26 },
      "w-defender": { state: "installed", appliedDays: 1 },
      "w-msrt": { state: "installed", appliedDays: 20 },
      "w-edge": { state: "installed", appliedDays: 1 },
    },
  },
  "dev-4": {
    scannedAgo: 3 * DAY,
    pendingReboot: false,
    cells: {
      "w-cu-10-22h2": { state: "failed", error: "0x8024402c: the update could not be downloaded from the update source" },
      "w-net-481": { state: "missing" },
      "w-defender": { state: "missing" },
      "w-msrt": { state: "missing" },
    },
  },
  "dev-7": {
    scannedAgo: 41,
    pendingReboot: false,
    cells: {
      "w-cu-srv2022": { state: "installed", appliedDays: 2 },
      "w-net-481": { state: "installed", appliedDays: 2 },
      "w-defender": { state: "installed", appliedDays: 2 },
      "w-msrt": { state: "installed", appliedDays: 20 },
    },
  },
  "dev-8": {
    scannedAgo: 26 * HOUR,
    pendingReboot: false,
    cells: {
      "w-cu-srv2022": { state: "not_applicable" },
      "w-net-481": { state: "missing" },
      "w-defender": { state: "missing" },
      "w-msrt": { state: "missing" },
    },
  },
  "dev-9": { scannedAgo: null, pendingReboot: false, cells: {} },
  "dev-5": {
    scannedAgo: 17,
    pendingReboot: false,
    cells: {
      "l-kernel": { state: "missing" },
      "l-openssl": { state: "approved" },
      "l-libssl3": { state: "approved" },
      "l-curl": { state: "missing" },
      "l-sudo": { state: "installed", appliedDays: 5 },
      "l-systemd": { state: "missing" },
      "l-git": { state: "missing" },
      "l-python3": { state: "missing" },
      "l-tzdata": { state: "installed", appliedDays: 3 },
      "l-vim": { state: "missing" },
    },
  },
  "dev-6": {
    scannedAgo: 22,
    pendingReboot: true,
    cells: {
      "l-kernel": { state: "installed", appliedDays: 1 },
      "l-openssl": { state: "approved" },
      "l-libssl3": { state: "approved" },
      "l-curl": { state: "missing" },
      "l-sudo": { state: "installed", appliedDays: 5 },
      "l-systemd": { state: "missing" },
      "l-git": { state: "failed", error: "dpkg was interrupted, you must manually run 'dpkg --configure -a' to correct the problem" },
      "l-tzdata": { state: "missing" },
    },
  },
  "dev-14": {
    scannedAgo: 65,
    pendingReboot: false,
    cells: {
      "l-curl": { state: "installed", appliedDays: 4 },
      "l-sudo": { state: "installed", appliedDays: 4 },
      "l-python3": { state: "installed", appliedDays: 4 },
      "l-vim": { state: "installed", appliedDays: 4 },
    },
  },
  "dev-3": {
    scannedAgo: 2 * HOUR,
    pendingReboot: false,
    cells: {
      "m-sequoia": { state: "missing" },
      "m-safari": { state: "installed", appliedDays: 6 },
      "m-xprotect": { state: "missing" },
    },
  },
};

const OUTSTANDING: readonly State[] = ["missing", "approved", "failed"];

/** The managed fleet, joined to its scan: the devices page's rows that carry an agent. */
function managed() {
  return devices()
    .filter((d) => d.agent_status !== null)
    .map((d) => ({ device: d, scan: MATRIX[d.id] ?? { scannedAgo: null, pendingReboot: false, cells: {} } }));
}

function scanCoverage() {
  const fleet = managed();
  const scanned = fleet.filter((m) => m.scan.scannedAgo !== null);
  const latest = Math.min(...scanned.map((m) => m.scan.scannedAgo as number));
  return {
    devices_total: fleet.length,
    devices_scanned: scanned.length,
    last_scan_at: scanned.length > 0 ? ago(latest) : null,
  };
}

/* --------------------------------------------------------------- approvals */

interface ApprovalSeed {
  id: string;
  patch: string;
  ring: string;
  by: string | null;
  daysAgo: number;
  revoked?: { by: string; daysAgo: number };
}

/**
 * `usr-helgar` and `usr-ic` ARE THE SHARED DIRECTORY'S IDS, not decoration.
 * The Approvals table names the person behind an approval by looking the id up
 * in `GET /users` (`useApprovalActorDirectory`), and the fixture answering that
 * call is the console-wide one in `service-desk/fixtures.ts`. An id outside it
 * renders as the raw handle -- which is the honest production fallback, and the
 * wrong thing for the design gate's screenshot to be showing.
 */
const APPROVALS: ApprovalSeed[] = [
  { id: "apr-1", patch: "w-net-481", ring: "test", by: "usr-ic", daysAgo: 6 },
  { id: "apr-2", patch: "w-net-481", ring: "pilot", by: "usr-helgar", daysAgo: 3 },
  { id: "apr-3", patch: "w-cu-srv2022", ring: "test", by: "usr-ic", daysAgo: 4 },
  { id: "apr-4", patch: "l-openssl", ring: "test", by: null, daysAgo: 12 },
  { id: "apr-5", patch: "l-libssl3", ring: "test", by: null, daysAgo: 12 },
  { id: "apr-6", patch: "w-cu-10-22h2", ring: "broad", by: "usr-helgar", daysAgo: 8, revoked: { by: "usr-helgar", daysAgo: 2 } },
];

function approval(seed: ApprovalSeed, clientId: string | null = null): PatchApproval {
  const row = CATALOGUE.find((c) => c.id === seed.patch);
  return {
    id: seed.id,
    // The seeded approvals predate per-client ones: every client's.
    client_id: clientId,
    patch_id: seed.patch,
    patch_identifier: row?.identifier ?? null,
    ring_name: seed.ring,
    approved_by: seed.by,
    approved_at: ago(seed.daysAgo * DAY),
    revoked: seed.revoked !== undefined,
    revoked_by: seed.revoked?.by ?? null,
    revoked_at: seed.revoked ? ago(seed.revoked.daysAgo * DAY) : null,
  };
}

const clearedRings = (patchId: string): string[] => [
  ...new Set(APPROVALS.filter((a) => a.patch === patchId && !a.revoked).map((a) => a.ring)),
];

/* --------------------------------------------------------------- policies */

const POLICIES: PatchPolicy[] = [
  { id: "pol-default", device_group_id: null, ring_name: "broad", ring_order: 100, maintenance_window: "0 2-5 * * 6", auto_approve_security: false, reboot: "if_required", reboot_deferral_hours: 24, scan_interval_hours: 24 },
  { id: "pol-servers", device_group_id: "grp-servers", ring_name: "test", ring_order: 10, maintenance_window: "0 22-23 * * 1-5", auto_approve_security: true, reboot: "never", reboot_deferral_hours: 72, scan_interval_hours: 6 },
  { id: "pol-workstations", device_group_id: "grp-workstations", ring_name: "pilot", ring_order: 20, maintenance_window: null, auto_approve_security: false, reboot: "if_required", reboot_deferral_hours: 8, scan_interval_hours: 12 },
];

/** Which ring a managed device resolves into, by its group, then the default. */
function ringOf(groupId: string | null): PatchPolicy {
  return POLICIES.find((p) => p.device_group_id !== null && p.device_group_id === groupId) ?? (POLICIES[0] as PatchPolicy);
}

/* ------------------------------------------------------------------- reads */

const listPatchUpdates: FixtureMap["listPatchUpdates"] = (params = {}) => {
  const fleet = managed();
  const scoped = params.device_id ? fleet.filter((m) => m.device.id === params.device_id) : fleet;
  if (params.device_id && scoped.length === 0) throw notFound("device");

  const rows: PatchUpdate[] = CATALOGUE.filter((row) => !params.os_family || row.os_family === params.os_family)
    .filter((row) => !params.source || row.source === params.source)
    .filter((row) => !params.ring || clearedRings(row.id).includes(params.ring))
    .map((row) => {
      const cells = scoped
        .map((m) => m.scan.cells[row.id])
        .filter((cell): cell is Cell => cell !== undefined);
      const count = (state: State) => cells.filter((c) => c.state === state).length;
      const own = params.device_id ? scoped[0]?.scan.cells[row.id] : undefined;
      return {
        id: row.id,
        os_family: row.os_family,
        source: row.source,
        identifier: row.identifier,
        title: row.title,
        severity: row.severity,
        kb: row.kb,
        cve: row.cve,
        priority: row.priority,
        first_seen_at: ago(row.firstSeenDays * DAY),
        devices_missing: count("missing"),
        devices_approved: count("approved"),
        devices_failed: count("failed"),
        devices_installed: count("installed"),
        devices_outstanding: count("missing") + count("approved") + count("failed"),
        cleared_rings: clearedRings(row.id),
        device_state: own?.state ?? null,
        device_last_error: own?.error ?? null,
        device_applied_at: own?.appliedDays !== undefined ? ago(own.appliedDays * DAY) : null,
      };
    })
    .filter((row) => (params.outstanding_only ?? true) ? row.devices_outstanding > 0 : true);

  const needle = (params.q ?? "").trim().toLowerCase();
  const searched = needle
    ? rows.filter((row) => `${row.identifier} ${row.title}`.toLowerCase().includes(needle))
    : rows;
  searched.sort((a, b) => rank(a.severity) - rank(b.severity) || b.devices_outstanding - a.devices_outstanding || a.title.localeCompare(b.title));

  const severity_counts: Record<string, number> = {};
  for (const row of searched) severity_counts[row.severity] = (severity_counts[row.severity] ?? 0) + 1;

  const wanted = params.severity ?? [];
  const filtered = wanted.length > 0 ? searched.filter((row) => wanted.includes(row.severity)) : searched;
  const offset = params.offset ?? 0;
  const limit = params.limit ?? 25;
  const response: PatchUpdateListResponse = {
    items: filtered.slice(offset, offset + limit),
    total: filtered.length,
    total_unfiltered: searched.length,
    severity_counts,
    ...scanCoverage(),
  };
  return response;
};

function rollup(entry: ReturnType<typeof managed>[number]): PatchDeviceRollup | null {
  const outstanding = Object.entries(entry.scan.cells).filter(([, cell]) => OUTSTANDING.includes(cell.state));
  if (outstanding.length === 0) return null;
  const counts_by_severity: Record<string, number> = {};
  let worst: string | null = null;
  for (const [patchId] of outstanding) {
    const severity = CATALOGUE.find((c) => c.id === patchId)?.severity ?? "unknown";
    counts_by_severity[severity] = (counts_by_severity[severity] ?? 0) + 1;
    if (rank(severity) < rank(worst)) worst = severity;
  }
  return {
    device_id: entry.device.id,
    hostname: entry.device.hostname ?? entry.device.id,
    os_family: entry.device.os_family ?? "unknown",
    outstanding: outstanding.length,
    failed: outstanding.filter(([, cell]) => cell.state === "failed").length,
    worst_severity: worst,
    counts_by_severity,
    last_scan_at: entry.scan.scannedAgo === null ? null : ago(entry.scan.scannedAgo),
  };
}

const listPatchDeviceRollup: FixtureMap["listPatchDeviceRollup"] = (params = {}) => {
  const needle = (params.q ?? "").trim().toLowerCase();
  const rows = managed()
    .filter((m) => !params.os_family || m.device.os_family === params.os_family)
    .map(rollup)
    .filter((row): row is PatchDeviceRollup => row !== null)
    .filter((row) => !needle || row.hostname.toLowerCase().includes(needle))
    .sort((a, b) => rank(a.worst_severity) - rank(b.worst_severity) || b.outstanding - a.outstanding || a.hostname.localeCompare(b.hostname));
  const offset = params.offset ?? 0;
  const limit = params.limit ?? 25;
  const response: PatchDeviceRollupListResponse = {
    items: rows.slice(offset, offset + limit),
    total: rows.length,
    ...scanCoverage(),
  };
  return response;
};

const listPatchRings: FixtureMap["listPatchRings"] = () => {
  const fleet = managed();
  const items = [...POLICIES]
    .sort((a, b) => a.ring_order - b.ring_order)
    .map((policy) => ({
      ring_name: policy.ring_name,
      ring_order: policy.ring_order,
      device_count: fleet.filter((m) => ringOf(m.device.device_group_id).id === policy.id).length,
    }));
  return { items, total: items.length, device_total: fleet.length };
};

const listPatchApprovals: FixtureMap["listPatchApprovals"] = (params = {}) => {
  const rows = APPROVALS.map((seed) => approval(seed))
    .filter((a) => !params.patch_id || a.patch_id === params.patch_id)
    .filter((a) => !params.ring || a.ring_name === params.ring)
    .filter((a) => params.revoked === undefined || a.revoked === params.revoked)
    .sort((a, b) => b.approved_at.localeCompare(a.approved_at));
  const offset = params.offset ?? 0;
  const limit = params.limit ?? 100;
  return { items: rows.slice(offset, offset + limit), total: rows.length };
};

function fleetReport(): FleetReport {
  const scanned = managed().filter((m) => m.scan.scannedAgo !== null);
  const pct = (part: number, whole: number) => (whole === 0 ? 0 : Math.round((part / whole) * 1000) / 10);

  const compliant_pct_by_os_family: Record<string, number> = {};
  for (const family of ["windows", "linux", "macos"] as const) {
    const of = scanned.filter((m) => m.device.os_family === family);
    if (of.length === 0) continue;
    compliant_pct_by_os_family[family] = pct(of.filter((m) => rollup(m) === null).length, of.length);
  }

  const compliant_pct_by_severity: Record<string, number> = {};
  for (const severity of ["critical", "important", "moderate", "low", "unknown"]) {
    const clean = scanned.filter((m) => (rollup(m)?.counts_by_severity[severity] ?? 0) === 0).length;
    compliant_pct_by_severity[severity] = pct(clean, scanned.length);
  }

  const exposures = scanned.flatMap((m) =>
    Object.entries(m.scan.cells)
      .filter(([, cell]) => OUTSTANDING.includes(cell.state))
      .map(([patchId, cell]) => {
        const row = CATALOGUE.find((c) => c.id === patchId) as CatalogueRow;
        return {
          device_id: m.device.id,
          hostname: m.device.hostname ?? m.device.id,
          patch_id: patchId,
          identifier: row.identifier,
          severity: row.severity,
          priority: row.priority,
          state: cell.state,
        };
      }),
  );
  const exposed = new Set(exposures.map((e) => e.device_id));
  const critical = new Set(exposures.filter((e) => e.severity === "critical").map((e) => e.device_id));
  return {
    compliant_pct_by_os_family,
    compliant_pct_by_severity,
    exposures,
    exposed_device_count: exposed.size,
    critical_device_count: critical.size,
    ...scanCoverage(),
  };
}

const getPatchFleetReport: FixtureMap["getPatchFleetReport"] = () => fleetReport();

const listPatchPolicies: FixtureMap["listPatchPolicies"] = () => [...POLICIES];

const getDevicePatchStatus: FixtureMap["getDevicePatchStatus"] = (deviceId) => {
  const entry = managed().find((m) => m.device.id === deviceId);
  if (!entry) throw notFound("device");
  const counts_by_state: Record<string, number> = {};
  const counts_by_severity: Record<string, number> = {};
  for (const [patchId, cell] of Object.entries(entry.scan.cells)) {
    counts_by_state[cell.state] = (counts_by_state[cell.state] ?? 0) + 1;
    if (!OUTSTANDING.includes(cell.state)) continue;
    const severity = CATALOGUE.find((c) => c.id === patchId)?.severity ?? "unknown";
    counts_by_severity[severity] = (counts_by_severity[severity] ?? 0) + 1;
  }
  const status: DeviceStatus = {
    device_id: deviceId,
    counts_by_state,
    counts_by_severity,
    pending_reboot: entry.scan.pendingReboot,
    last_scan_at: entry.scan.scannedAgo === null ? null : ago(entry.scan.scannedAgo),
    restart: restartsFor().find((r) => r.device_id === deviceId) ?? null,
    restart_window: windowFor(deviceId),
  };
  return status;
};

/** The fixture fleet's windows: an always-open ring is open now, the rest open tonight. */
function windowFor(deviceId: string): PatchRestartWindow {
  const entry = managed().find((m) => m.device.id === deviceId);
  const policy = entry ? ringOf(entry.device.device_group_id) : null;
  const open = policy?.maintenance_window === null;
  return {
    open_now: open,
    opens_at: open ? ago(0) : ago(-6 * 60),
    user_warning_minutes: (policy?.reboot_deferral_hours ?? 24) * 60,
  };
}

const requestDeviceRestart: FixtureMap["requestDeviceRestart"] = (deviceId) => {
  if (!managed().some((m) => m.device.id === deviceId)) throw notFound("device");
  const booked = restartsFor().find((r) => r.device_id === deviceId);
  const restart: PatchRestart = booked ?? {
    id: `rst-${deviceId}`,
    device_id: deviceId,
    state: "pending",
    origin: "operator",
    scheduled_for: null,
    user_present: null,
    reason: null,
    created_at: ago(0),
    closed_at: null,
  };
  return { restart, already_requested: booked != null, window: windowFor(deviceId) };
};

/** Every machine whose scan says a restart is pending owes one; the first is
 *  booked, the rest wait for their window. */
function restartsFor(): PatchRestartListItem[] {
  return managed()
    .filter((m) => m.scan.pendingReboot)
    .map((m, at) => ({
      id: `rst-${m.device.id}`,
      device_id: m.device.id,
      hostname: m.device.hostname,
      state: at === 0 ? "scheduled" : "pending",
      origin: "apply",
      scheduled_for: at === 0 ? ago(-90) : null,
      user_present: at === 0 ? true : null,
      reason: null,
      created_at: ago(DAY),
      closed_at: null,
    }));
}

const listPatchAppSources: FixtureMap["listPatchAppSources"] = (osFamily) => ({
  items:
    osFamily === "macos"
      ? [{ source: "homebrew", state: "unsupported", devices: 1, detail: "Homebrew is not installed, so app updates cannot be listed on this Mac" }]
      : osFamily === "windows"
        ? [{ source: "winget", state: "scanned", devices: 3, detail: null }]
        : [],
});

const listPatchRestarts: FixtureMap["listPatchRestarts"] = () => {
  const items = restartsFor();
  return { items, total: items.length };
};

const RUNS: PatchRun[] = [
  { id: "run-1", job_id: "job-patch-apply-1", device_id: "dev-5", installed: 0, failed: 0, reboot_required: false, started_at: ago(4), finished_at: null, failures: [] },
  { id: "run-2", job_id: "job-patch-apply-2", device_id: "dev-6", installed: 3, failed: 1, reboot_required: true, started_at: ago(DAY + 40), finished_at: ago(DAY + 31), failures: [{ source: "apt", identifier: "git", error: "dpkg was interrupted, you must manually run 'dpkg --configure -a' to correct the problem" }] },
  { id: "run-3", job_id: "job-patch-apply-3", device_id: "dev-4", installed: 1, failed: 1, reboot_required: false, started_at: ago(3 * DAY + 20), finished_at: ago(3 * DAY + 2), failures: [{ source: "wsus", identifier: "KB5041580", error: "0x8024402c: the update could not be downloaded from the update source" }] },
  { id: "run-4", job_id: "job-patch-apply-4", device_id: "dev-2", installed: 4, failed: 0, reboot_required: true, started_at: ago(DAY + 5), finished_at: ago(DAY), failures: [] },
  { id: "run-5", job_id: "job-patch-apply-5", device_id: "dev-7", installed: 3, failed: 0, reboot_required: false, started_at: ago(2 * DAY + 15), finished_at: ago(2 * DAY), failures: [] },
];

const listPatchRuns: FixtureMap["listPatchRuns"] = (params = {}) => {
  const rows = RUNS.filter((run) => !params.device_id || run.device_id === params.device_id).sort((a, b) =>
    b.started_at.localeCompare(a.started_at),
  );
  return { items: rows.slice(0, params.limit ?? 50) };
};

/* ------------------------------------------------------------------ writes */

/** One job per device that can run a scan: those carrying an agent. */
const triggerPatchScan: FixtureMap["triggerPatchScan"] = (body = {}) => {
  const targets = body.device_ids ?? managed().map((m) => m.device.id);
  return { job_ids: targets.map((id) => `job-scan-${id}`) };
};

/** One job per host whose maintenance window is open: the always-open pilot ring here. */
const applyPatches: FixtureMap["applyPatches"] = (body = {}) => {
  const targets = managed().filter((m) => !body.device_ids || body.device_ids.includes(m.device.id));
  return {
    job_ids: targets.filter((m) => ringOf(m.device.device_group_id).maintenance_window === null).map((m) => `job-apply-${m.device.id}`),
  };
};

const approvePatch: FixtureMap["approvePatch"] = (body) => {
  if (!CATALOGUE.some((c) => c.id === body.patch_id)) throw notFound("patch");
  return approval(
    { id: `apr-${Date.now()}`, patch: body.patch_id, ring: body.ring_name, by: "usr-helgar", daysAgo: 0 },
    body.client_id ?? null,
  );
};

const bulkApprovePatches: FixtureMap["bulkApprovePatches"] = (body) => {
  const items = body.patch_ids.map((patch, at) =>
    approval(
      { id: `apr-bulk-${at}`, patch, ring: body.ring_name, by: "usr-helgar", daysAgo: 0 },
      body.client_id ?? null,
    ),
  );
  const standing = body.patch_ids.filter((patch) => clearedRings(patch).includes(body.ring_name)).length;
  return { items, total: items.length, newly_approved: items.length - standing, already_in_force: standing };
};

const revokePatchApproval: FixtureMap["revokePatchApproval"] = (approvalId) => {
  if (!APPROVALS.some((a) => a.id === approvalId)) throw notFound("approval");
  return undefined;
};

const upsertPatchPolicy: FixtureMap["upsertPatchPolicy"] = (body) => {
  const existing = POLICIES.find((p) => (p.device_group_id ?? null) === (body.device_group_id ?? null));
  const policy: PatchPolicy = {
    id: existing?.id ?? `pol-${Date.now()}`,
    device_group_id: body.device_group_id ?? null,
    ring_name: body.ring_name,
    ring_order: body.ring_order,
    maintenance_window: body.maintenance_window ?? null,
    auto_approve_security: body.auto_approve_security,
    reboot: body.reboot,
    reboot_deferral_hours: body.reboot_deferral_hours,
    scan_interval_hours: body.scan_interval_hours,
  };
  return policy;
};

const exportPatchReport: FixtureMap["exportPatchReport"] = () => {
  const report = fleetReport();
  const lines = ["device_id,hostname,patch_id,identifier,severity,state"];
  for (const e of report.exposures) lines.push([e.device_id, e.hostname, e.patch_id, e.identifier, e.severity, e.state].join(","));
  return new Blob([lines.join("\n")], { type: "text/csv" });
};

const fixtures: FixtureMap = {
  listPatchUpdates,
  listPatchDeviceRollup,
  listPatchRings,
  listPatchApprovals,
  getPatchFleetReport,
  listPatchPolicies,
  getDevicePatchStatus,
  listPatchAppSources,
  listPatchRestarts,
  requestDeviceRestart,
  listPatchRuns,
  triggerPatchScan,
  applyPatches,
  approvePatch,
  bulkApprovePatches,
  revokePatchApproval,
  upsertPatchPolicy,
  exportPatchReport,
};

export default fixtures;
