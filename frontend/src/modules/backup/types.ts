// Mirrors backend/app/modules/backup/api.py response/request models exactly.
// No secret fields — repositories never expose `encryption_key_ref`; the
// manifest response (`BackupManifest`) is metadata only (paths/sizes/chunk
// ids), never chunk bytes (see api.py's module docstring).
//
// State vocabularies are sourced from the backend, not invented:
//   - backup runs (BackupRun.state): pending | running | success | failed
//   - backup points (BackupPointOut.state): complete | failed | pruned
//   - restore jobs (restore job state): pending | running | success | failed
//   - repositories (RepositoryOut.state): active | disabled
// See backupState.ts for the exact citations backing each vocabulary.

// The vocabularies the console words, as unions: enums.coverage.test.ts reads
// them to prove every value has a label in both languages. The columns are
// free text server-side, so the row types keep `string`; a value outside
// these falls back to a derived label.
export type BackupPointState = "complete" | "failed" | "pruned";
export type BackupRunState = "pending" | "running" | "success" | "failed" | "missed";
export type RepositoryState = "active" | "disabled";

export interface Repository {
  id: string;
  /** The one client whose devices back up here (MSP clients phase 2, Task 13). */
  client_id: string;
  name: string;
  storage_ref: string;
  state: string;
  dedup_params: Record<string, unknown>;
  created_at: string;
  offsite_target_id?: string | null;
}

export interface RepositoryCreateRequest {
  /** Required: a repository is one client's, and nothing else in the body says which. */
  client_id: string;
  name: string;
  dedup_params?: Record<string, unknown> | null;
}

export interface BackupPolicy {
  id: string;
  /**
   * The group's client, or the client whose default this is (no group).
   * `null` only on a row from before the server's rule that a policy names a
   * repository of its own client.
   */
  client_id: string | null;
  device_group_id: string | null;
  repository_id: string;
  sources: Record<string, unknown>;
  window_cron: string | null;
  retention_gfs: Record<string, unknown>;
  enabled: boolean;
}

export interface PolicyUpsertRequest {
  device_group_id?: string | null;
  /** With no group, the client whose default policy this is; a group's is the group's. */
  client_id?: string | null;
  repository_id: string;
  sources?: Record<string, unknown>;
  window_cron?: string | null;
  retention_gfs?: Record<string, unknown>;
  enabled?: boolean;
}

export interface RunResponse {
  job_ids: string[];
}

export interface BackupPoint {
  id: string;
  device_id: string;
  repository_id: string;
  backup_run_id: string;
  taken_at: string;
  size: number;
  state: string;
  verified_at: string | null;
  restore_tested_at: string | null;
  // "read" and "sampled" | "full" (api.py BackupPointOut): what the last test
  // restore actually proved.
  restore_test_level?: string | null;
  restore_test_scope?: string | null;
  // Format-2 agents: "snapshot" | "live" | ..., and false when files were read
  // while in use. Absent from older agents.
  consistency_level?: string | null;
  crash_consistent?: boolean | null;
  warnings?: string[] | null;
}

export interface RestoreRequest {
  selection: string[];
  target: Record<string, unknown>;
}

export interface RestoreResponse {
  job_ids: string[];
}

export interface LastGoodPoint {
  backup_point_id: string;
  taken_at: string;
  size: number;
  repository_id: string;
}

export interface RepositoryRef {
  id: string;
  name: string;
}

export interface DeviceBackupStatus {
  device_id: string;
  last_good_point: LastGoodPoint | null;
  last_run_state: string | null;
  last_run_finished_at: string | null;
  verified_at: string | null;
  restore_tested_at: string | null;
  repository: RepositoryRef | null;
}

export interface DedupReport {
  bytes_protected_total: number;
  bytes_stored_total: number;
  dedup_ratio: number;
  savings_pct: number;
}

// coverage_rate / success_rate are FRACTIONS in [0,1] (api.py FleetBackupReportOut) —
// multiply by 100 only at the point of display, never here.
export interface FleetBackupReport {
  devices_total: number;
  devices_covered: number;
  coverage_rate: number;
  run_counts_by_state: Record<string, number>;
  success_rate: number;
  points_needing_verification: number;
  dedup: DedupReport;
}

export interface ManifestEntry {
  path: string;
  kind: string;
  mode: number;
  size: number;
  mtime: number;
  chunk_ids: string[];
  target: string | null;
}

export interface ManifestStream {
  type: string;
  entries: ManifestEntry[];
}

export interface BackupManifest {
  schema_version: number;
  streams: ManifestStream[];
}

// ---- Off-site copy and recovery key (backup/offsite_api.py) ----
// No secret is ever returned: the access key and secret go in once and are
// sealed server-side.

export type RetentionGfs = Partial<Record<"daily" | "weekly" | "monthly" | "yearly", number>>;

export interface OffsiteTargetSettings {
  name: string;
  endpoint: string | null;
  region: string;
  bucket: string;
  prefix: string;
  addressing: "path" | "virtual";
  lock_mode: "compliance" | "governance";
  immutable_days: number;
  retention_gfs: RetentionGfs;
  max_mbps: number | null;
}

export interface OffsiteTargetCreate extends OffsiteTargetSettings {
  access_key_id: string;
  secret_access_key: string;
}

export interface OffsiteTargetUpdate extends OffsiteTargetSettings {
  access_key_id: string | null;
  secret_access_key: string | null;
}

export interface OffsiteTestStep {
  step: string;
  ok: boolean;
  detail: string;
}

export interface OffsiteTarget extends OffsiteTargetSettings {
  id: string;
  state: string;
  test_requested_at: string | null;
  tested_at: string | null;
  test_steps: OffsiteTestStep[];
  last_ok_at: string | null;
  last_error: string | null;
}

export interface RepositoryOffsite {
  repository_id: string;
  offsite_target_id: string | null;
}

export interface RecoveryKey {
  id: string;
  fingerprint: string;
  public_key: string;
  proven_at: string | null;
  challenge_expires_at: string | null;
  created_at: string;
  retired_at: string | null;
}

export interface RecoveryKeyChallenge {
  key: RecoveryKey;
  challenge: string;
}
