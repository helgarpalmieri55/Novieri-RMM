/**
 * Fixtures for the reporting module: the catalogue the server publishes and
 * one computed page per report.
 *
 * THE ROWS ARE THE SHELL'S WORKED FLEET (`fixtures/core.ts`), so the patch
 * compliance report lists the same machines `/devices` does and its totals
 * are counts over those rows. Every total is COUNTED here, in the fixture,
 * which stands in for the server; nothing in this folder's pages computes
 * one, and the structural sweep in `__tests__/reporting.test.tsx` walks this
 * file too, so it accumulates with `filter().length` rather than with an
 * accumulator.
 *
 * THE GATE WALKS A REAL REPORT KEY, and it used to walk `rpt-1`. That was a
 * placeholder aliased to the patch compliance entry so the route rendered
 * something -- and the cost of it only showed up in a screenshot review: the
 * page's `h1` is derived from the key (`reportText.reportName`), so the one
 * report page anybody ever looks at was titled "Rpt 1", the only page in 87
 * that read as an unfinished placeholder. It was not a defect in the page; it
 * was the sample key. `scripts/routes.mjs` now walks `patch-compliance`, a key
 * this catalogue really publishes, and the alias is gone.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { devices } from "../../fixtures/core";
import { ApiError } from "../../lib/api";
import type { CatalogueEntry, ParamsSchema, ReportPage, ReportRow } from "./types";

const MINUTE = 60_000;
const HOUR = 60;
const DAY = 24 * HOUR;
/** An ISO timestamp `minutes` before the call. */
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

/* --------------------------------------------------------------- catalogue */

const PAGING: ParamsSchema["properties"] = {
  limit: { type: "integer", title: "Rows", default: 50, minimum: 1, maximum: 500 },
  offset: { type: "integer", title: "Offset", default: 0, minimum: 0 },
};

const PATCH_SCHEMA: ParamsSchema = {
  type: "object",
  additionalProperties: false,
  properties: {
    os_family: {
      anyOf: [{ type: "string", enum: ["windows", "linux", "macos"] }, { type: "null" }],
      title: "OS family",
      description: "Restrict the report to one platform.",
      default: null,
    },
    ...PAGING,
  },
};

const FLEET_SCHEMA: ParamsSchema = {
  type: "object",
  additionalProperties: false,
  properties: {
    stale_after_days: {
      type: "integer",
      title: "Stale after (days)",
      description: "A device not seen for this many days counts as stale.",
      default: 7,
      minimum: 1,
      maximum: 365,
    },
    ...PAGING,
  },
};

const BACKUP_SCHEMA: ParamsSchema = {
  type: "object",
  additionalProperties: false,
  properties: {
    verified_within_days: {
      type: "integer",
      title: "Verified within (days)",
      default: 14,
      minimum: 1,
      maximum: 365,
    },
    ...PAGING,
  },
};

const SLA_SCHEMA: ParamsSchema = {
  type: "object",
  additionalProperties: false,
  required: ["from", "to"],
  properties: {
    from: { type: "string", format: "date-time", title: "From" },
    to: { type: "string", format: "date-time", title: "To" },
    queue_id: { anyOf: [{ type: "string", format: "uuid" }, { type: "null" }], title: "Queue", default: null },
    ...PAGING,
  },
};

const PATCH_ENTRY = {
  summary: "Every managed device with its outstanding updates and the time of its last scan.",
  description:
    "By OS family and group. A device is compliant when nothing approved is outstanding; one that has never been scanned is counted separately and never as compliant. Timestamps are those of each device's last scan.",
  params_schema: PATCH_SCHEMA,
  requires: ["devices.read", "patch.read"],
  entitled: true,
  missing_permissions: [],
};

const CATALOGUE: CatalogueEntry[] = [
  { key: "patch-compliance", ...PATCH_ENTRY },
  {
    key: "backup-coverage",
    summary: "Every device with its backup coverage verdict and the restore point behind it.",
    description:
      "Covered, stale, failing or unprotected, decided from the last verified restore point. Restore points and their proof come from the backup module as of its last verification pass.",
    params_schema: BACKUP_SCHEMA,
    requires: ["backup.read", "devices.read"],
    entitled: true,
    missing_permissions: [],
  },
  {
    key: "fleet-posture",
    summary: "Every device with its agent contact state and its open health alerts.",
    description:
      "One row per device. Stale contact is measured from the agent's last check-in; open alerts are counted by severity as of the moment the report runs.",
    params_schema: FLEET_SCHEMA,
    requires: ["devices.read", "health.read"],
    entitled: true,
    missing_permissions: [],
  },
  {
    key: "sla-attainment",
    summary: "SLA attainment by queue and priority over a window: tickets met against tickets breached.",
    description:
      "Response and resolution clocks, per queue and priority. A ticket re-triaged after the window recomputes its deadline, so this report is not reproducible.",
    params_schema: SLA_SCHEMA,
    requires: ["sla.read", "ticket.read"],
    entitled: true,
    missing_permissions: [],
  },
];

const listReports: FixtureMap["listReports"] = () => ({ reports: CATALOGUE });

/* -------------------------------------------------------------------- rows */

const GROUP_NAMES: Record<string, string> = {
  "grp-servers": "Production servers",
  "grp-workstations": "Workstations",
};

/** The managed fleet: the rows a scanner could ever have visited. */
const managed = () => devices().filter((device) => device.agent_status !== null);

/** Outstanding approved updates per machine, and whether it was ever scanned. */
const PATCH_STATE: Record<string, { missing: number; scannedMinutesAgo: number | null }> = {
  "dev-1": { missing: 0, scannedMinutesAgo: 6 * HOUR },
  "dev-2": { missing: 2, scannedMinutesAgo: 7 * HOUR },
  "dev-3": { missing: 0, scannedMinutesAgo: 5 * HOUR },
  "dev-4": { missing: 11, scannedMinutesAgo: 3 * DAY },
  "dev-5": { missing: 0, scannedMinutesAgo: 2 * HOUR },
  "dev-6": { missing: 0, scannedMinutesAgo: 2 * HOUR },
  "dev-7": { missing: 1, scannedMinutesAgo: 6 * HOUR },
  "dev-8": { missing: 4, scannedMinutesAgo: 26 * HOUR },
  "dev-9": { missing: 0, scannedMinutesAgo: null },
  "dev-14": { missing: 0, scannedMinutesAgo: 4 * HOUR },
};

function patchRows(osFamily: string | undefined): ReportRow[] {
  return managed()
    .filter((device) => !osFamily || device.os_family === osFamily)
    .map((device) => {
      const state = PATCH_STATE[device.id] ?? { missing: 0, scannedMinutesAgo: null };
      return {
        hostname: device.hostname,
        os_family: device.os_family,
        group: device.device_group_id ? (GROUP_NAMES[device.device_group_id] ?? device.device_group_id) : null,
        missing_updates: state.missing,
        last_scanned_at: state.scannedMinutesAgo === null ? null : ago(state.scannedMinutesAgo),
        compliant: state.scannedMinutesAgo !== null && state.missing === 0,
      };
    });
}

function patchTotals(rows: ReportRow[]): Record<string, number> {
  return {
    devices: rows.length,
    devices_with_zero_missing: rows.filter((row) => row["compliant"] === true).length,
    devices_never_scanned: rows.filter((row) => row["last_scanned_at"] === null).length,
  };
}

const BACKUP_STATE: Record<string, { verdict: string; restoreDaysAgo: number | null }> = {
  "dev-1": { verdict: "covered", restoreDaysAgo: 1 },
  "dev-2": { verdict: "covered", restoreDaysAgo: 1 },
  "dev-3": { verdict: "stale", restoreDaysAgo: 19 },
  "dev-4": { verdict: "failing", restoreDaysAgo: 3 },
  "dev-5": { verdict: "covered", restoreDaysAgo: 1 },
  "dev-6": { verdict: "covered", restoreDaysAgo: 1 },
  "dev-7": { verdict: "covered", restoreDaysAgo: 2 },
  "dev-8": { verdict: "failing", restoreDaysAgo: 8 },
  "dev-9": { verdict: "unprotected", restoreDaysAgo: null },
  "dev-14": { verdict: "unprotected", restoreDaysAgo: null },
};

function backupRows(): ReportRow[] {
  return managed().map((device) => {
    const state = BACKUP_STATE[device.id] ?? { verdict: "unprotected", restoreDaysAgo: null };
    return {
      hostname: device.hostname,
      verdict: state.verdict,
      last_verified_restore_point: state.restoreDaysAgo === null ? null : ago(state.restoreDaysAgo * DAY),
    };
  });
}

function backupTotals(rows: ReportRow[]): Record<string, number> {
  const count = (verdict: string) => rows.filter((row) => row["verdict"] === verdict).length;
  return {
    devices: rows.length,
    covered: count("covered"),
    stale: count("stale"),
    failing: count("failing"),
    unprotected: count("unprotected"),
  };
}

const ALERTS: Record<string, { critical: number; warning: number }> = {
  "dev-4": { critical: 1, warning: 2 },
  "dev-8": { critical: 0, warning: 3 },
  "dev-2": { critical: 0, warning: 1 },
};

function fleetRows(staleAfterDays: number): ReportRow[] {
  const cutoff = Date.now() - staleAfterDays * DAY * MINUTE;
  return managed().map((device) => {
    const alerts = ALERTS[device.id] ?? { critical: 0, warning: 0 };
    return {
      hostname: device.hostname,
      agent_status: device.agent_status,
      last_seen_at: device.last_seen_at,
      stale: device.last_seen_at === null || Date.parse(device.last_seen_at) < cutoff,
      open_alerts_critical: alerts.critical,
      open_alerts_warning: alerts.warning,
    };
  });
}

function fleetTotals(rows: ReportRow[]): Record<string, number> {
  return {
    devices: rows.length,
    stale_devices: rows.filter((row) => row["stale"] === true).length,
    devices_with_open_alerts: rows.filter(
      (row) => Number(row["open_alerts_critical"]) > 0 || Number(row["open_alerts_warning"]) > 0,
    ).length,
  };
}

const SLA_ROWS: ReportRow[] = [
  { queue: "Service desk", priority: "p1", response_met: 4, response_breached: 1, resolution_met: 3, resolution_breached: 2 },
  { queue: "Service desk", priority: "p2", response_met: 12, response_breached: 1, resolution_met: 11, resolution_breached: 2 },
  { queue: "Service desk", priority: "p3", response_met: 15, response_breached: 0, resolution_met: 14, resolution_breached: 1 },
];

function slaTotals(rows: ReportRow[]): Record<string, number> {
  const sum = (key: string) => {
    let total = 0;
    for (const row of rows) total = total + Number(row[key] ?? 0);
    return total;
  };
  return {
    tickets: sum("response_met") + sum("response_breached"),
    response_met: sum("response_met"),
    response_breached: sum("response_breached"),
    resolution_met: sum("resolution_met"),
    resolution_breached: sum("resolution_breached"),
  };
}

/* --------------------------------------------------------------- the pages */

function page(
  key: string,
  rows: ReportRow[],
  totals: Record<string, number>,
  params: Record<string, string>,
  extra: Partial<ReportPage["meta"]> = {},
): ReportPage {
  const limit = Math.max(1, Number(params["limit"] ?? 50) || 50);
  const offset = Math.max(0, Number(params["offset"] ?? 0) || 0);
  return {
    meta: {
      report_key: key,
      generated_at: ago(0),
      reproducible: true,
      window_from: null,
      window_to: null,
      total: rows.length,
      limit,
      offset,
      sources: [],
      ...extra,
    },
    totals,
    rows: rows.slice(offset, offset + limit),
  };
}

const runReport: FixtureMap["runReport"] = (key, params = {}) => {
  switch (key) {
    case "patch-compliance": {
      const rows = patchRows(params["os_family"] || undefined);
      return page(key, rows, patchTotals(rows), params, {
        sources: [
          { module: "patch", permission: "patch.read", recency: "as of each device's last scan" },
          { module: "devices", permission: "devices.read", recency: "as of last agent contact" },
        ],
      });
    }
    case "backup-coverage": {
      const rows = backupRows();
      return page(key, rows, backupTotals(rows), params, {
        sources: [
          { module: "backup", permission: "backup.read", recency: "as of the last verification pass" },
          { module: "devices", permission: "devices.read", recency: "as of last agent contact" },
        ],
      });
    }
    case "fleet-posture": {
      const rows = fleetRows(Math.max(1, Number(params["stale_after_days"] ?? 7) || 7));
      return page(key, rows, fleetTotals(rows), params, {
        sources: [
          { module: "devices", permission: "devices.read", recency: "as of last agent contact" },
          { module: "health", permission: "health.read", recency: "as of now" },
        ],
      });
    }
    case "sla-attainment": {
      if (!params["from"] || !params["to"]) {
        throw new ApiError(
          422,
          JSON.stringify({ detail: [{ loc: ["query", "from"], msg: "Field required", type: "missing" }] }),
        );
      }
      return page(key, SLA_ROWS, slaTotals(SLA_ROWS), params, {
        reproducible: false,
        window_from: params["from"],
        window_to: params["to"],
        sources: [{ module: "sla", permission: "sla.read", recency: "as of now" }],
      });
    }
    default:
      throw new ApiError(404, JSON.stringify({ detail: "report not found" }));
  }
};

/** The same rows as CSV, which is what the download hands the operator. */
const exportReport: FixtureMap["exportReport"] = (key, params = {}) => {
  const result = runReport(key, { ...params, limit: "500", offset: "0" }) as ReportPage;
  const columns = result.rows[0] ? Object.keys(result.rows[0]) : [];
  const lines = [columns.join(",")];
  for (const row of result.rows) lines.push(columns.map((column) => String(row[column] ?? "")).join(","));
  return new Blob([lines.join("\n")], { type: "text/csv" });
};

export const fixtures: FixtureMap = {
  listReports,
  runReport,
  exportReport,
};

export default fixtures;
