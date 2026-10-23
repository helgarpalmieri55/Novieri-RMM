// Mirrors backend/app/modules/reporting/schema.py exactly (snake_case keys,
// matching the JSON on the wire). Read the ACTUAL schema on any disagreement
// with spec prose.
//
// THE ROW TYPE IS `Record<string, ReportCell>`, NOT A PER-REPORT INTERFACE, AND
// THAT IS A DELIBERATE CHOICE RATHER THAN LAZINESS. The backend's catalogue is
// a registry of code-defined reports whose row models are per-definition; the
// browser is handed the columns it must render by `export_columns` on the
// server side and renders whatever keys the rows carry. Writing four
// hand-mirrored row interfaces here would put this file on the critical path of
// every backend report change -- and, worse, would invite the page to *compute*
// from a typed field, which §11 forbids outright. `ReportCell` is a union of
// the JSON scalars a report row can hold; it is not `any`, so a component still
// cannot do arithmetic on a cell without narrowing it first.

export type ReportCell = string | number | boolean | null;

export type ReportRow = Record<string, ReportCell>;

// schema.py::ReportSource
export interface ReportSource {
  module: string;
  permission: string;
  // What the timestamps in this source MEAN. Rendered, never summarised: a
  // figure is only as fresh as the record it was read from (§2).
  recency: string;
}

// schema.py::ReportMeta
export interface ReportMeta {
  report_key: string;
  // The UTC instant the query ran. §11 makes rendering this MANDATORY: reports
  // are computed live, so a page without it is a number with no timestamp.
  generated_at: string;
  // False when a source is mutable, so re-running the report for the same
  // window may return different numbers. Drives the staleness note (§0.3
  // cost 2) -- `sla-attainment` is the v1 report where this is false.
  reproducible: boolean;
  window_from: string | null;
  window_to: string | null;
  total: number;
  limit: number;
  offset: number;
  sources: ReportSource[];
}

// schema.py::ReportPage[RowT]
export interface ReportPage {
  meta: ReportMeta;
  // Report-level aggregates over ALL matching rows, computed server-side.
  // §11: no client-side aggregation of any kind -- if the browser computed a
  // percentage there would be two implementations and the browser's is the one
  // someone screenshots.
  totals: Record<string, number>;
  rows: ReportRow[];
}

// One property of a report's params schema, as Pydantic v2 emits it for a
// `BaseModel` field (spec §17.11).
//
// PARTIAL BY DESIGN, AND THE PARTIALITY IS THE POINT. This is not an attempt to
// model JSON Schema; it is the subset `ReportView` binds a control to. A keyword
// the server emits and this type does not name is simply not used to render --
// it is never a reason to guess. Everything is optional because a params model
// that gains a `pattern` or loses a `maximum` must not break the build of a
// browser that renders neither.
export interface ParamProperty {
  type?: string;
  // "date-time" | "uuid" -- Pydantic's format for `datetime` and `UUID`.
  format?: string;
  enum?: string[];
  default?: ReportCell;
  description?: string;
  title?: string;
  minimum?: number;
  maximum?: number;
  // An OPTIONAL parameter is `anyOf: [<the real type>, {type: "null"}]`. The
  // non-null branch is the one a control binds to.
  anyOf?: ParamProperty[];
}

// schema.py::CatalogueEntry.params_schema -- the JSON Schema of one report's
// query parameters, derived server-side from the model the route validates
// against.
//
// `required` IS ABSENT, NOT EMPTY, when a report requires nothing: that is what
// Pydantic emits, so every reader here must treat it as `?? []` rather than
// trusting it to exist.
export interface ParamsSchema {
  properties?: Record<string, ParamProperty>;
  required?: string[];
  // `extra="forbid"` on the server. An unknown parameter is a 422, not a filter
  // that is ignored -- which is why this page sends only what `properties` names.
  additionalProperties?: boolean;
  type?: string;
  title?: string;
  description?: string;
}

// schema.py::CatalogueEntry
export interface CatalogueEntry {
  key: string;
  summary: string;
  description: string;
  // The report's params contract, PUBLISHED rather than hardcoded here (§17.11).
  // The browser binding a control per property is the whole reason the server
  // sends it: a hand-written params bar is a second copy of a contract, and two
  // copies drift.
  params_schema: ParamsSchema;
  // Every module read permission the report needs, sorted. Derived server-side
  // from the tables it reads, so it can never understate what the report shows.
  requires: string[];
  entitled: boolean;
  // The codes in `requires` the caller does not hold. NAMED IN THE UI: a report
  // you cannot run is a permissions conversation, not a secret (§11).
  missing_permissions: string[];
}

// schema.py::ReportCatalogue
export interface ReportCatalogue {
  reports: CatalogueEntry[];
}

// The query string a report is run with. Every report's params model differs
// and every field is optional from the browser's point of view except
// `sla-attainment`'s window, which the page supplies -- so this is a plain
// string map handed straight to `URLSearchParams`, never a computed value.
export type ReportParams = Record<string, string>;

// The vocabularies the console words itself. Each union is the set of values the
// server's definition documents for that field (`reporting/definitions/*.py`,
// `registry.py::TABLE_MODULE`); a value outside it reads through the shared
// status words, never raw. `enums.coverage.test.ts` checks every one has a label
// in both languages.

/** The built-in reports (`CatalogueEntry.key`). */
export type ReportKey = "fleet-posture" | "sla-attainment" | "backup-coverage" | "patch-compliance";

/** `ReportSource.module`: the module a report read from. */
export type ReportSourceModule = "devices" | "jobs" | "health" | "patch" | "backup" | "service_desk" | "sla";

/** The `scan_state` column of patch compliance. */
export type ReportScanState = "fresh" | "stale" | "never";

/** The `agent_status` column of fleet posture. */
export type ReportAgentStatus = "online" | "offline" | "never_enrolled";

/** The `last_point_state` column of backup coverage. */
export type ReportPointState = "complete" | "failed" | "pruned";

/** The `consistency_level` column of backup coverage. */
export type ReportConsistency = "snapshot" | "live" | "application" | "partial";

/** The `offsite` column of backup coverage. */
export type ReportOffsite = "off" | "current" | "behind";

/** The `coverage` column of backup coverage. */
export type ReportCoverage = "covered" | "stale" | "failing" | "unprotected";
