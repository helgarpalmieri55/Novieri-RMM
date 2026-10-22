/**
 * Fixtures for the backup module: the repositories, the policies that write
 * to them, the backup points taken under those policies, the fleet report
 * that summarises them, one point's manifest and one device's status.
 *
 * EVERY NUMBER IS DERIVED FROM THE POINTS. The report's coverage counts the
 * devices that have a point, its verification backlog counts the complete
 * points nobody has verified, and its run counts are the points' own states
 * plus the runs that produced no point (failed) or are still going. The
 * tiles on `/backup` therefore cannot disagree with the table under them.
 * The devices are the shell's worked fleet (`fixtures/core.ts`), so the
 * coverage gap is real: the switch, the printer and the two unmanaged rows
 * have no agent and cannot be backed up.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { devices } from "../../fixtures/core";
import { ApiError } from "../../lib/api";
import type {
  BackupManifest,
  BackupPoint,
  BackupPolicy,
  FleetBackupReport,
  OffsiteTarget,
  RecoveryKey,
  Repository,
} from "./types";

const MINUTE = 60_000;
const HOUR = 60;
const DAY = 24 * HOUR;
const GIB = 1024 ** 3;
/** An ISO timestamp `minutes` before the call. */
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* ------------------------------------------------------------ repositories */

const REPOSITORIES: Repository[] = [
  {
    id: "repo-minio-b4",
    name: "MinIO · rack B4",
    storage_ref: "s3://itms-backup/",
    state: "active",
    dedup_params: { chunk_min: 512 * 1024, chunk_avg: 2 * 1024 * 1024, chunk_max: 8 * 1024 * 1024 },
    created_at: ago(140 * DAY),
  },
  {
    id: "repo-offsite",
    name: "Offsite · Porto",
    storage_ref: "s3://itms-backup-porto/",
    state: "active",
    dedup_params: { chunk_min: 512 * 1024, chunk_avg: 2 * 1024 * 1024, chunk_max: 8 * 1024 * 1024 },
    created_at: ago(61 * DAY),
  },
  {
    id: "repo-legacy",
    name: "Legacy NAS",
    storage_ref: "s3://nas-archive/",
    state: "disabled",
    dedup_params: {},
    created_at: ago(400 * DAY),
  },
];

/* ---------------------------------------------------------------- policies */

const POLICIES: BackupPolicy[] = [
  {
    id: "pol-org-nightly",
    device_group_id: null,
    repository_id: "repo-minio-b4",
    sources: { include: ["/home", "C:\\Users"], exclude: ["**/node_modules", "**/.cache"] },
    window_cron: "0 1-4 * * *",
    retention_gfs: { daily: 14, weekly: 8, monthly: 12 },
    enabled: true,
  },
  {
    id: "pol-servers",
    device_group_id: "grp-servers",
    repository_id: "repo-offsite",
    sources: { include: ["/etc", "/var/lib", "/srv"], snapshot: "lvm" },
    window_cron: "0 2-3 * * 1-5",
    retention_gfs: { daily: 30, weekly: 12, monthly: 24 },
    enabled: true,
  },
  {
    id: "pol-pilot",
    device_group_id: "grp-pilot",
    repository_id: "repo-legacy",
    sources: {},
    window_cron: null,
    retention_gfs: {},
    enabled: false,
  },
];

/* ------------------------------------------------------------------ points */

interface PointSeed {
  id: string;
  device: string;
  repo: string;
  takenMinutes: number;
  sizeGiB: number;
  state: string;
  verifiedMinutes: number | null;
  testedMinutes: number | null;
}

/**
 * Nine points across the managed fleet: most complete and verified, three
 * complete and still awaiting verification (the oldest four days), one
 * failed, one pruned out of retention. `dev-4` (offline three days) and
 * `dev-8` have no point at all.
 */
const POINT_SEEDS: PointSeed[] = [
  { id: "bp-db01-0903", device: "dev-6", repo: "repo-offsite", takenMinutes: 7 * HOUR + 46, sizeGiB: 412, state: "complete", verifiedMinutes: 6 * HOUR, testedMinutes: 6 * HOUR },
  { id: "bp-app02-0903", device: "dev-5", repo: "repo-offsite", takenMinutes: 7 * HOUR + 29, sizeGiB: 18, state: "complete", verifiedMinutes: null, testedMinutes: null },
  { id: "bp-dc01-0903", device: "dev-7", repo: "repo-offsite", takenMinutes: 7 * HOUR + 12, sizeGiB: 96, state: "complete", verifiedMinutes: 5 * HOUR, testedMinutes: null },
  { id: "bp-ws041-0903", device: "dev-1", repo: "repo-minio-b4", takenMinutes: 8 * HOUR + 4, sizeGiB: 42, state: "complete", verifiedMinutes: 7 * HOUR, testedMinutes: 20 * DAY },
  { id: "bp-ws042-0903", device: "dev-2", repo: "repo-minio-b4", takenMinutes: 8 * HOUR + 1, sizeGiB: 38, state: "complete", verifiedMinutes: null, testedMinutes: null },
  { id: "bp-mbp017-0903", device: "dev-3", repo: "repo-minio-b4", takenMinutes: 7 * HOUR + 55, sizeGiB: 120, state: "failed", verifiedMinutes: null, testedMinutes: null },
  { id: "bp-ws011-0903", device: "dev-14", repo: "repo-minio-b4", takenMinutes: 7 * HOUR + 40, sizeGiB: 27, state: "complete", verifiedMinutes: 6 * HOUR + 30, testedMinutes: null },
  { id: "bp-mbp017-0830", device: "dev-3", repo: "repo-minio-b4", takenMinutes: 4 * DAY + 8 * HOUR, sizeGiB: 118, state: "complete", verifiedMinutes: null, testedMinutes: null },
  { id: "bp-ws041-0720", device: "dev-1", repo: "repo-minio-b4", takenMinutes: 45 * DAY, sizeGiB: 40, state: "pruned", verifiedMinutes: 44 * DAY, testedMinutes: null },
];

function point(seed: PointSeed): BackupPoint {
  return {
    id: seed.id,
    device_id: seed.device,
    repository_id: seed.repo,
    backup_run_id: `run-${seed.id}`,
    taken_at: ago(seed.takenMinutes),
    size: Math.round(seed.sizeGiB * GIB),
    state: seed.state,
    verified_at: seed.verifiedMinutes === null ? null : ago(seed.verifiedMinutes),
    restore_tested_at: seed.testedMinutes === null ? null : ago(seed.testedMinutes),
  };
}

/** Newest first, as the server lists them. */
const points = (): BackupPoint[] => POINT_SEEDS.map(point).sort((a, b) => (a.taken_at < b.taken_at ? 1 : -1));

/* ------------------------------------------------------------------ report */

const getBackupReport: FixtureMap["getBackupReport"] = () => {
  const all = points();
  const fleet = devices();
  const covered = new Set(all.filter((p) => p.state === "complete").map((p) => p.device_id));
  const complete = all.filter((p) => p.state === "complete").length;
  const failed = all.filter((p) => p.state === "failed").length;
  // Two runs are in flight and one is queued; neither has a point yet.
  const running = 2;
  const pending = 1;
  const success = complete + all.filter((p) => p.state === "pruned").length;
  const terminal = success + failed;
  const bytesProtected = all.filter((p) => p.state !== "failed").reduce((sum, p) => sum + p.size, 0);
  const bytesStored = Math.round(bytesProtected / 3.1);
  const report: FleetBackupReport = {
    devices_total: fleet.length,
    devices_covered: covered.size,
    coverage_rate: covered.size / fleet.length,
    run_counts_by_state: { pending, running, success, failed },
    success_rate: terminal === 0 ? 0 : success / terminal,
    points_needing_verification: all.filter((p) => p.state === "complete" && p.verified_at === null).length,
    dedup: {
      bytes_protected_total: bytesProtected,
      bytes_stored_total: bytesStored,
      dedup_ratio: 3.1,
      savings_pct: Math.round((1 - bytesStored / bytesProtected) * 1000) / 10,
    },
  };
  return report;
};

/* ---------------------------------------------------------------- fixtures */

const listBackupRepositories: FixtureMap["listBackupRepositories"] = () => REPOSITORIES;

const createBackupRepository: FixtureMap["createBackupRepository"] = (body) => ({
  id: `repo-${Date.now()}`,
  name: body.name,
  storage_ref: `s3://itms-backup-${body.name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}/`,
  state: "active",
  dedup_params: body.dedup_params ?? {},
  created_at: ago(0),
});

const listBackupPolicies: FixtureMap["listBackupPolicies"] = () => POLICIES;

const upsertBackupPolicy: FixtureMap["upsertBackupPolicy"] = (body) => {
  const existing = POLICIES.find((p) => (p.device_group_id ?? null) === (body.device_group_id ?? null));
  return {
    id: existing?.id ?? `pol-${Date.now()}`,
    device_group_id: body.device_group_id ?? null,
    repository_id: body.repository_id,
    sources: body.sources ?? {},
    window_cron: body.window_cron ?? null,
    retention_gfs: body.retention_gfs ?? {},
    enabled: body.enabled ?? true,
  };
};

const listBackupPoints: FixtureMap["listBackupPoints"] = () => points();

const runBackup: FixtureMap["runBackup"] = (deviceId) => {
  const found = devices().find((d) => d.id === deviceId);
  if (!found) throw notFound("device");
  // A machine with no agent has nothing to run the job: the server dispatches nothing.
  return { job_ids: found.agent_status === null ? [] : [`job-backup-${deviceId}`] };
};

const getBackupManifest: FixtureMap["getBackupManifest"] = (pointId) => {
  const found = points().find((p) => p.id === pointId);
  if (!found) throw notFound("backup point");
  const windows = devices().find((d) => d.id === found.device_id)?.os_family === "windows";
  const root = windows ? "C:\\Users\\ana.ribeiro" : "/home/ana";
  const sep = windows ? "\\" : "/";
  const mtime = Math.floor(new Date(found.taken_at).getTime() / 1000) - 3600;
  const manifest: BackupManifest = {
    schema_version: 1,
    streams: [
      {
        type: "files",
        entries: [
          { path: `${root}${sep}Documents${sep}Q3-forecast.xlsx`, kind: "file", mode: 0o644, size: 2_418_176, mtime, chunk_ids: ["c1", "c2"], target: null },
          { path: `${root}${sep}Documents${sep}board-pack.pdf`, kind: "file", mode: 0o644, size: 11_534_336, mtime: mtime - 86_400, chunk_ids: ["c3", "c4", "c5"], target: null },
          { path: `${root}${sep}Desktop${sep}notes.txt`, kind: "file", mode: 0o644, size: 4_096, mtime: mtime - 600, chunk_ids: ["c6"], target: null },
          { path: `${root}${sep}.ssh${sep}config`, kind: "file", mode: 0o600, size: 812, mtime: mtime - 900_000, chunk_ids: ["c7"], target: null },
        ],
      },
    ],
  };
  return manifest;
};

const getDeviceBackup: FixtureMap["getDeviceBackup"] = (deviceId) => {
  const found = devices().find((d) => d.id === deviceId);
  if (!found) throw notFound("device");
  const mine = points().filter((p) => p.device_id === deviceId);
  const latest = mine[0] ?? null;
  const good = mine.find((p) => p.state === "complete") ?? null;
  const repo = good ? REPOSITORIES.find((r) => r.id === good.repository_id) : null;
  return {
    device_id: deviceId,
    last_good_point: good
      ? { backup_point_id: good.id, taken_at: good.taken_at, size: good.size, repository_id: good.repository_id }
      : null,
    last_run_state: latest ? (latest.state === "failed" ? "failed" : "success") : null,
    last_run_finished_at: latest ? latest.taken_at : null,
    verified_at: good?.verified_at ?? null,
    restore_tested_at: good?.restore_tested_at ?? null,
    repository: repo ? { id: repo.id, name: repo.name } : null,
  };
};

const restoreBackupPoint: FixtureMap["restoreBackupPoint"] = (pointId) => {
  if (!points().some((p) => p.id === pointId)) throw notFound("backup point");
  return { job_ids: [`job-restore-${pointId}`] };
};

const verifyBackupPoint: FixtureMap["verifyBackupPoint"] = (pointId) => {
  if (!points().some((p) => p.id === pointId)) throw notFound("backup point");
  return { job_ids: [`job-verify-${pointId}`] };
};

// The off-site page: one healthy locked bucket and one proven recovery key,
// the state a finished setup is in. Without these the page drew its error
// state under the fixture harness (`listOffsiteTargets`, `listRecoveryKeys`).
const offsiteTargets = (): OffsiteTarget[] => [
  {
    id: "offsite-wasabi",
    name: "Wasabi (Amsterdam)",
    endpoint: "https://s3.eu-central-2.wasabisys.com",
    region: "eu-central-2",
    bucket: "toeshee-itms-offsite",
    prefix: "itms/",
    addressing: "virtual",
    lock_mode: "compliance",
    immutable_days: 30,
    retention_gfs: { daily: 14, weekly: 8, monthly: 12 },
    max_mbps: 50,
    state: "ok",
    test_requested_at: ago(3 * DAY),
    tested_at: ago(3 * DAY - 2),
    test_steps: [
      { step: "reach", ok: true, detail: "The bucket answered." },
      { step: "object_lock", ok: true, detail: "Object Lock is on, in compliance mode." },
      { step: "write", ok: true, detail: "A test object was written and locked." },
    ],
    last_ok_at: ago(40),
    last_error: null,
  },
];

const recoveryKeys = (): RecoveryKey[] => [
  {
    id: "rk-1",
    fingerprint: "SHA256:9f2c7a1e4b",
    public_key: "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3wqk2p",
    proven_at: ago(5 * DAY),
    challenge_expires_at: null,
    created_at: ago(5 * DAY + 10),
    retired_at: null,
  },
];

const listOffsiteTargets: FixtureMap["listOffsiteTargets"] = () => offsiteTargets();
const listRecoveryKeys: FixtureMap["listRecoveryKeys"] = () => recoveryKeys();

export const fixtures: FixtureMap = {
  listOffsiteTargets,
  listRecoveryKeys,
  listBackupRepositories,
  createBackupRepository,
  listBackupPolicies,
  upsertBackupPolicy,
  listBackupPoints,
  runBackup,
  getBackupManifest,
  getDeviceBackup,
  restoreBackupPoint,
  verifyBackupPoint,
  getBackupReport,
};

export default fixtures;
