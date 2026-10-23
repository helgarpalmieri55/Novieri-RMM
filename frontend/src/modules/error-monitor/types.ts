// Mirrors backend/app/api/v1/errors.py exactly (snake_case keys, matching the
// JSON). THE SHIPPED API IS THE CONTRACT -- these types were written against
// errors.py's Pydantic models directly, not against the module brief's prose.
//
// NO FIELD HERE CAN CARRY EXCEPTION TEXT, DELIBERATELY. Neither
// `platform_error_group` nor `platform_error_event` has a message/traceback
// column (app.core.errors.capture's module docstring) -- a group is
// identified by `exc_type` + `module` + `lineno` + `qualname` + a fingerprint,
// nothing else. Do not add a field here that implies one exists.

// `where_` server-side ("WHERE" is a SQL keyword); matches `CapturedError.
// where`: "api" | "worker" | "gateway" -- the three capture seams.
//
// This is a CLIENT-SIDE narrowing of an open server type, not a server-backed
// union: `errors.py`'s `where` column is plain `str` (`String(16)`), so the
// three literals here are held true only by convention at the four capture
// call sites, verified against those sites as of this writing, not by the
// wire contract. That is a weaker guarantee than `platform-status`'s
// `ReasonCode`, which IS a real `enum.StrEnum` server-side
// (`app.core.health.types.ReasonCode`) and therefore genuinely server-backed
// -- do not cite that module as precedent for this one being safe from drift.
export type ErrorWhere = "api" | "worker" | "gateway";

export interface ErrorGroup {
  id: string;
  fingerprint: string;
  exc_type: string;
  module: string;
  lineno: number;
  qualname: string;
  where: ErrorWhere;
  first_seen: string;
  last_seen: string;
  event_count: number;
  resolved_at: string | null;
}

// The list route's payload. `dropped` is the capture queue's running
// overflow count since process start (spec §3.1) -- when non-zero, every
// count above it is a floor, not an exact total, and the page must say so.
export interface ErrorGroupList {
  groups: ErrorGroup[];
  dropped: number;
}

// One captured occurrence -- only the correlation ids read from contextvars
// at capture time. Each is `null`, never `""`, when capture happened outside
// that context.
export interface ErrorEvent {
  id: string;
  occurred_at: string;
  request_id: string | null;
  org_id: string | null;
  user_id: string | null;
  job_id: string | null;
  device_id: string | null;
}

export interface ErrorGroupDetail {
  group: ErrorGroup;
  events: ErrorEvent[];
}
