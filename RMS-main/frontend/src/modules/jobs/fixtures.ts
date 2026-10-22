/**
 * Fixtures for the signed-job engine: the queue `/jobs` lists, the detail it
 * opens, and the three mutations the page can send.
 *
 * Built against the core fleet (`fixtures/core.ts`) so every `device_id` here
 * is a row `/devices` shows, and the timestamps are relative to now so the
 * "Last six hours" timeline and the "waiting over 15 min" tile read the same
 * on every run: one failed patch run four hours ago, one inventory job that
 * has sat dispatched to an offline server, one patch run in flight, and a
 * day of ordinary successes behind them.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import type { Job, JobStatus } from "../../lib/api";
import { ApiError } from "../../lib/api";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** An ISO timestamp `minutes` before the call. */
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();
/** An ISO timestamp `minutes` after the call. */
const ahead = (minutes: number): string => new Date(Date.now() + minutes * MINUTE).toISOString();

interface Seed {
  id: string;
  type: string;
  device: string;
  status: JobStatus;
  attempts: number;
  /** Minutes ago the job was issued. */
  issued: number;
  /** Minutes after issue the job started, or null if it never did. */
  startAfter: number | null;
  /** Minutes the run lasted, or null if it has not finished. */
  ran: number | null;
  params?: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  cancel?: { at: number; outcome: string };
  retryOf?: string;
  logs?: Job["logs"];
}

const SEEDS: Seed[] = [
  {
    id: "job-1",
    type: "patch.apply",
    device: "dev-6",
    status: "failed",
    attempts: 3,
    issued: 4 * 60 + 20,
    startAfter: 2,
    ran: 11,
    params: { kbs: ["KB5041585"], reboot: "if_required" },
    result: { error: "apt returned 100: dpkg was interrupted", exit_code: 100 },
    logs: [
      { ts: ago(4 * 60 + 18), level: "info", message: "Applying 1 approved update", artifact_ref: null },
      { ts: ago(4 * 60 + 9), level: "error", message: "E: dpkg was interrupted, you must manually run 'dpkg --configure -a'", artifact_ref: null },
      { ts: ago(4 * 60 + 7), level: "error", message: "apt returned 100", artifact_ref: "art-8813" },
    ],
  },
  {
    id: "job-2",
    type: "inventory.collect",
    device: "dev-8",
    status: "dispatched",
    attempts: 1,
    issued: 3 * 60 + 12,
    startAfter: null,
    ran: null,
    params: {},
    result: null,
    logs: [],
  },
  {
    id: "job-3",
    type: "patch.apply",
    device: "dev-3",
    status: "running",
    attempts: 1,
    issued: 14,
    startAfter: 1,
    ran: null,
    params: { kbs: ["macOS 15.6.1"], reboot: "if_required" },
    result: null,
    logs: [{ ts: ago(13), level: "info", message: "Downloading macOS 15.6.1 (2.1 GB)", artifact_ref: null }],
  },
  {
    id: "job-4",
    type: "patch.scan",
    device: "dev-5",
    status: "succeeded",
    attempts: 1,
    issued: 3 * 60 + 40,
    startAfter: 1,
    ran: 3,
    params: {},
    result: { missing: 2, installed: 214 },
    logs: [{ ts: ago(3 * 60 + 37), level: "info", message: "2 updates available", artifact_ref: null }],
  },
  {
    id: "job-5",
    type: "backup.run",
    device: "dev-5",
    status: "succeeded",
    attempts: 1,
    issued: 2 * 60 + 30,
    startAfter: 1,
    ran: 24,
    params: { repository: "repo-lisbon-nas", policy: "servers-nightly" },
    result: { bytes: 41_284_772_912, files: 128_402, snapshot: "2026-09-07T04:31Z" },
    logs: [],
  },
  {
    id: "job-6",
    type: "inventory.collect",
    device: "dev-1",
    status: "succeeded",
    attempts: 1,
    issued: 58,
    startAfter: 1,
    ran: 1,
    params: {},
    result: { hardware: 3, software: 112 },
    logs: [],
  },
  {
    id: "job-7",
    type: "logs.ship",
    device: "dev-7",
    status: "succeeded",
    attempts: 1,
    issued: 47,
    startAfter: 1,
    ran: 1,
    params: {},
    result: { events: 3_412 },
    logs: [],
  },
  {
    id: "job-8",
    type: "health.check.now",
    device: "dev-14",
    status: "succeeded",
    attempts: 1,
    issued: 31,
    startAfter: 1,
    ran: 1,
    params: {},
    result: { checks: 9, failing: 0 },
    logs: [],
  },
  {
    id: "job-9",
    type: "patch.apply",
    device: "dev-6",
    status: "queued",
    attempts: 0,
    issued: 6,
    startAfter: null,
    ran: null,
    params: { kbs: ["KB5041585"], reboot: "if_required" },
    result: null,
    retryOf: "job-1",
    logs: [],
  },
  {
    id: "job-10",
    type: "script.run",
    device: "dev-2",
    status: "cancelled",
    attempts: 1,
    issued: 26 * 60,
    startAfter: 1,
    ran: 4,
    params: { script_id: "scr-clean-temp", args: [] },
    result: null,
    cancel: { at: 26 * 60 - 3, outcome: "interrupted" },
    logs: [],
  },
  {
    id: "job-11",
    type: "patch.scan",
    device: "dev-4",
    status: "expired",
    attempts: 0,
    issued: 3 * DAY / MINUTE,
    startAfter: null,
    ran: null,
    params: {},
    result: null,
    logs: [],
  },
  {
    id: "job-12",
    type: "agent.cert.renew",
    device: "dev-9",
    status: "refused",
    attempts: 1,
    issued: 2 * DAY / MINUTE + 40,
    startAfter: null,
    ran: null,
    params: {},
    result: { reason: "certificate revoked" },
    logs: [],
  },
];

function job(seed: Seed): Job {
  const started = seed.startAfter === null ? null : ago(seed.issued - seed.startAfter);
  const finished =
    seed.startAfter === null || seed.ran === null ? null : ago(seed.issued - seed.startAfter - seed.ran);
  return {
    id: seed.id,
    type: seed.type,
    device_id: seed.device,
    status: seed.status,
    attempts: seed.attempts,
    params: seed.params ?? {},
    result: seed.result ?? null,
    issued_at: ago(seed.issued),
    expires_at: ahead(24 * 60 - seed.issued),
    started_at: started,
    finished_at: finished,
    cancel_requested_at: seed.cancel ? ago(seed.cancel.at) : null,
    cancel_outcome: seed.cancel?.outcome ?? null,
    retry_of_job_id: seed.retryOf ?? null,
    logs: seed.logs ?? [],
  };
}

/** Every job, newest first, rebuilt per call so the relative times are fresh. */
const jobs = (): Job[] => SEEDS.map(job).sort((a, b) => Date.parse(b.issued_at) - Date.parse(a.issued_at));

function findJob(id: string): Job {
  const found = jobs().find((entry) => entry.id === id);
  if (!found) throw new ApiError(404, JSON.stringify({ detail: "job not found" }));
  return found;
}

/** The ids minted by dispatch and retry in this session, so a second call differs. */
let minted = 0;

const fixtures: FixtureMap = {
  listJobs: (params = {}) => {
    const all = params.order === "oldest" ? jobs().reverse() : jobs();
    const offset = params.offset ?? 0;
    const limit = params.limit ?? 50;
    return { items: all.slice(offset, offset + limit), total: all.length };
  },

  getJob: (id) => findJob(id),

  dispatchJob: (body) => {
    minted += 1;
    return {
      ...job({
        id: `job-new-${minted}`,
        type: body.type,
        device: body.device_id,
        status: "queued",
        attempts: 0,
        issued: 0,
        startAfter: null,
        ran: null,
        params: body.params,
      }),
    };
  },

  /** A queued job is withdrawn outright; one already on a device is asked to stop. */
  cancelJob: (id) => {
    const found = findJob(id);
    if (found.status === "queued") {
      return { ...found, status: "cancelled", cancel_requested_at: ago(0), cancel_outcome: "withdrawn" };
    }
    return { ...found, cancel_requested_at: ago(0), cancel_outcome: "requested" };
  },

  retryJob: (id) => {
    const found = findJob(id);
    minted += 1;
    return {
      ...found,
      id: `job-retry-${minted}`,
      status: "queued",
      attempts: 0,
      result: null,
      issued_at: ago(0),
      expires_at: ahead(24 * 60),
      started_at: null,
      finished_at: null,
      cancel_requested_at: null,
      cancel_outcome: null,
      retry_of_job_id: id,
      logs: [],
    };
  },
};

export default fixtures;
