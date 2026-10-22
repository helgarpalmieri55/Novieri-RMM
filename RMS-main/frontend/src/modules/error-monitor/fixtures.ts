/**
 * `/error-monitor` in fixture mode: five fingerprinted groups from the three
 * capture seams, three still open, and the correlation ids behind each.
 *
 * NO EXCEPTION TEXT, here as on the wire: a group is a type, a raise site and
 * a count (`docs/08-observability.md` §3). The events carry only the ids that
 * were in scope when the exception was raised, and a worker failure outside
 * a request has no request id, exactly as the capture seam records it.
 */

import { ApiError } from "../../lib/api";
import type { FixtureMap } from "../../fixtures/client";
import type { ErrorEvent, ErrorGroup } from "./types";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();

const GROUPS: ErrorGroup[] = [
  {
    id: "eg-1",
    fingerprint: "7c1d9e0a2f4b8c31",
    exc_type: "PatchValidationError",
    module: "app.modules.patch.handler",
    lineno: 212,
    qualname: "ingest_scan_document",
    where: "worker",
    first_seen: ago(6 * HOUR),
    last_seen: ago(12 * MINUTE),
    event_count: 4,
    resolved_at: null,
  },
  {
    id: "eg-2",
    fingerprint: "b83f19c4a0d27e55",
    exc_type: "ConnectionResetError",
    module: "app.gateway.main",
    lineno: 341,
    qualname: "AgentSession.recv",
    where: "gateway",
    first_seen: ago(26 * DAY),
    last_seen: ago(3 * HOUR),
    event_count: 37,
    resolved_at: null,
  },
  {
    id: "eg-3",
    fingerprint: "e4a1c02d9b7f6310",
    exc_type: "IntegrityError",
    module: "app.modules.ipam.service",
    lineno: 588,
    qualname: "record_sweep_results",
    where: "worker",
    first_seen: ago(2 * DAY),
    last_seen: ago(DAY + 4 * HOUR),
    event_count: 2,
    resolved_at: null,
  },
  {
    id: "eg-4",
    fingerprint: "1f0a7c3e5d9b2486",
    exc_type: "TimeoutError",
    module: "app.core.vault.client",
    lineno: 97,
    qualname: "VaultClient.read_secret",
    where: "api",
    first_seen: ago(9 * DAY),
    last_seen: ago(8 * DAY),
    event_count: 11,
    resolved_at: ago(7 * DAY + 20 * HOUR),
  },
  {
    id: "eg-5",
    fingerprint: "5d2b8e4f1a9c7063",
    exc_type: "KeyError",
    module: "app.modules.backup.verify",
    lineno: 143,
    qualname: "verify_point",
    where: "worker",
    first_seen: ago(20 * DAY),
    last_seen: ago(19 * DAY),
    event_count: 1,
    resolved_at: ago(18 * DAY),
  },
];

/** The occurrences behind each group, newest first. */
const EVENTS: Record<string, ErrorEvent[]> = {
  "eg-1": [
    { id: "ev-1a", occurred_at: ago(12 * MINUTE), request_id: null, org_id: "org-toeshee", user_id: null, job_id: "job-2", device_id: "dev-5" },
    { id: "ev-1b", occurred_at: ago(2 * HOUR), request_id: null, org_id: "org-toeshee", user_id: null, job_id: "job-20", device_id: "dev-14" },
    { id: "ev-1c", occurred_at: ago(4 * HOUR), request_id: null, org_id: "org-toeshee", user_id: null, job_id: "job-8", device_id: "dev-8" },
    { id: "ev-1d", occurred_at: ago(6 * HOUR), request_id: null, org_id: "org-toeshee", user_id: null, job_id: "job-17", device_id: "dev-5" },
  ],
  "eg-2": [
    { id: "ev-2a", occurred_at: ago(3 * HOUR), request_id: null, org_id: "org-toeshee", user_id: null, job_id: null, device_id: "dev-4" },
    { id: "ev-2b", occurred_at: ago(DAY), request_id: null, org_id: "org-toeshee", user_id: null, job_id: null, device_id: "dev-8" },
    { id: "ev-2c", occurred_at: ago(DAY + 6 * HOUR), request_id: null, org_id: "org-toeshee", user_id: null, job_id: null, device_id: "dev-4" },
  ],
  "eg-3": [
    { id: "ev-3a", occurred_at: ago(DAY + 4 * HOUR), request_id: null, org_id: "org-toeshee", user_id: null, job_id: "job-31", device_id: null },
    { id: "ev-3b", occurred_at: ago(2 * DAY), request_id: null, org_id: "org-toeshee", user_id: null, job_id: "job-27", device_id: null },
  ],
  "eg-4": [
    { id: "ev-4a", occurred_at: ago(8 * DAY), request_id: "req-9f2c41a7", org_id: "org-toeshee", user_id: "usr-helgar", job_id: null, device_id: null },
    { id: "ev-4b", occurred_at: ago(8 * DAY + 3 * MINUTE), request_id: "req-0b17e3d9", org_id: "org-toeshee", user_id: "usr-tiago", job_id: null, device_id: null },
  ],
  "eg-5": [
    { id: "ev-5a", occurred_at: ago(19 * DAY), request_id: null, org_id: "org-toeshee", user_id: null, job_id: "job-44", device_id: "dev-6" },
  ],
};

const listErrorGroups: FixtureMap["listErrorGroups"] = (limit = 100) => ({
  groups: [...GROUPS]
    .sort((a, b) => Date.parse(b.last_seen) - Date.parse(a.last_seen))
    .slice(0, limit),
  dropped: 0,
});

const getErrorGroup: FixtureMap["getErrorGroup"] = (groupId, limit = 50) => {
  const group = GROUPS.find((g) => g.id === groupId);
  if (!group) throw new ApiError(404, JSON.stringify({ detail: "error group not found" }));
  return { group, events: (EVENTS[groupId] ?? []).slice(0, limit) };
};

const resolveErrorGroup: FixtureMap["resolveErrorGroup"] = (groupId) => {
  const group = GROUPS.find((g) => g.id === groupId);
  if (!group) throw new ApiError(404, JSON.stringify({ detail: "error group not found" }));
  group.resolved_at = new Date().toISOString();
  return group;
};

const unresolveErrorGroup: FixtureMap["unresolveErrorGroup"] = (groupId) => {
  const group = GROUPS.find((g) => g.id === groupId);
  if (!group) throw new ApiError(404, JSON.stringify({ detail: "error group not found" }));
  group.resolved_at = null;
  return group;
};

const fixtures: FixtureMap = { listErrorGroups, getErrorGroup, resolveErrorGroup, unresolveErrorGroup };

export default fixtures;
