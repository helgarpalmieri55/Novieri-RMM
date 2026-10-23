import { translate, type MessageKey } from "../../i18n/core";
import { lazyLabels } from "../../i18n/lazy";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { BadgeVariant } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";

// backup_runs state vocabulary exactly as written by the backend
// (models.py:72 BackupRun.state default "pending"; dispatch.py:149 set to
// "running" on dispatch; terminal "success"/"failed",
// dispatch.py:234-235,255 — also enumerated verbatim as reporting.py:60
// RUN_STATES = ("pending", "running", "success", "failed")). No other state
// values are ever persisted to BackupRun.state.
const BACKUP_RUN_STATE_VARIANTS: Record<string, BadgeVariant> = {
  pending: "muted",
  running: "info",
  success: "success",
  failed: "destructive",
  // dispatch.py `_record_missed`: a scheduled time whose start deadline passed.
  missed: "destructive",
};

export function backupRunStateVariant(state: string): BadgeVariant {
  return BACKUP_RUN_STATE_VARIANTS[state] ?? "muted";
}

// backup_points state vocabulary exactly as written by the backend
// (dispatch.py:251 sets "complete" once a run's manifest is durable;
// dispatch.py:255-256 sets "failed"; retention.py/gc.py filter on
// `state != "pruned"`, the terminal state once a point ages out of
// retention). No other state values are ever persisted to
// BackupPoint.state.
const BACKUP_POINT_STATE_VARIANTS: Record<string, BadgeVariant> = {
  complete: "success",
  failed: "destructive",
  pruned: "muted",
};

export function backupPointStateVariant(state: string): BadgeVariant {
  return BACKUP_POINT_STATE_VARIANTS[state] ?? "muted";
}

// restore_jobs state vocabulary exactly as written by the backend
// (models.py:113 RestoreJob.state default "pending"; restore.py:183 set to
// "running" on dispatch; terminal "success"/"failed", restore.py:270-273).
// No other state values are ever persisted to RestoreJob.state.
const RESTORE_JOB_STATE_VARIANTS: Record<string, BadgeVariant> = {
  pending: "muted",
  running: "info",
  success: "success",
  failed: "destructive",
};

export function restoreJobStateVariant(state: string): BadgeVariant {
  return RESTORE_JOB_STATE_VARIANTS[state] ?? "muted";
}

// backup_repositories state vocabulary exactly as written by the backend
// (models.py:45 BackupRepository.state default "active";
// repository.py:49 sets "active" on create; "disabled" is the only other
// value the 16-char column is documented to hold — the repository's
// off state once retired from use).
const REPOSITORY_STATE_VARIANTS: Record<string, BadgeVariant> = {
  active: "success",
  disabled: "muted",
};

export function repositoryStateVariant(state: string): BadgeVariant {
  return REPOSITORY_STATE_VARIANTS[state] ?? "muted";
}

/**
 * A state as a person reads it. A value the console knows has its own label
 * (gendered to agree with what it describes); one it has never met falls back
 * to the shared status vocabulary, then to the derived label, so no wire word
 * is ever printed raw.
 */
const BACKUP_POINT_STATES: ReadonlySet<string> = new Set(["complete", "failed", "pruned"]);
const BACKUP_RUN_STATES: ReadonlySet<string> = new Set(["pending", "running", "success", "failed", "missed"]);
const REPOSITORY_STATES: ReadonlySet<string> = new Set(["active", "disabled"]);

export function backupPointStateLabel(state: string): string {
  return BACKUP_POINT_STATES.has(state) ? humaniseEnum(state, "BackupPointState") : humaniseStatus(state);
}

export function backupRunStateLabel(state: string): string {
  return BACKUP_RUN_STATES.has(state) ? humaniseEnum(state, "BackupRunState") : humaniseStatus(state);
}

export function repositoryStateLabel(state: string): string {
  return REPOSITORY_STATES.has(state) ? humaniseEnum(state, "RepositoryState") : humaniseStatus(state);
}

/**
 * The previous design's badge vocabulary as v3 kinds, so a `StatusPill`
 * carries the same severity the `Badge` did.
 */
const VARIANT_KIND: Record<BadgeVariant, Kind> = {
  muted: "mut",
  info: "run",
  success: "ok",
  destructive: "crit",
  warning: "warn",
};

export function backupRunStateKind(state: string): Kind {
  return VARIANT_KIND[backupRunStateVariant(state)];
}

export function backupPointStateKind(state: string): Kind {
  return VARIANT_KIND[backupPointStateVariant(state)];
}

export function repositoryStateKind(state: string): Kind {
  return VARIANT_KIND[repositoryStateVariant(state)];
}

// backup_offsite_targets.state (migration 0090 ck_offsite_state).
const OFFSITE_TARGET_STATE_KIND: Record<string, Kind> = {
  untested: "mut",
  testing: "run",
  ok: "ok",
  failing: "crit",
};

export function offsiteTargetStateKind(state: string): Kind {
  return OFFSITE_TARGET_STATE_KIND[state] ?? "mut";
}

// The label tables read the catalogue when a field is read, so a held table
// follows a language switch.
export const OFFSITE_TARGET_STATE_LABEL: Readonly<Record<string, string>> = lazyLabels({
  untested: "backup.state.offsiteTarget.untested",
  testing: "backup.state.offsiteTarget.testing",
  ok: "backup.state.offsiteTarget.ok",
  failing: "backup.state.offsiteTarget.failing",
});

// backup-coverage `coverage` (reporting/definitions/backup_coverage.py).
const COVERAGE_KIND: Record<string, Kind> = {
  covered: "ok",
  stale: "warn",
  failing: "crit",
  unprotected: "crit",
};

export function coverageKind(value: string): Kind {
  return COVERAGE_KIND[value] ?? "mut";
}

export const COVERAGE_LABEL: Readonly<Record<string, string>> = lazyLabels({
  covered: "backup.state.coverage.covered",
  stale: "backup.state.coverage.stale",
  failing: "backup.state.coverage.failing",
  unprotected: "backup.state.coverage.unprotected",
});

export const OFFSITE_LABEL: Readonly<Record<string, string>> = lazyLabels({
  off: "backup.state.offsite.off",
  current: "backup.state.offsite.current",
  behind: "backup.state.offsite.behind",
});

/** What a live (not snapshot) point means for the person restoring it. */
export const notCrashConsistent = (): string => translate("backup.state.notCrashConsistent");

/**
 * The same sentence as a load-time snapshot, kept only because the module's
 * unit tests import the name. Components call `notCrashConsistent()`, which
 * follows the language.
 */
export const NOT_CRASH_CONSISTENT = notCrashConsistent();

// Agent warning codes (format-2 results) in the operator's words. An unknown
// code is shown as itself rather than guessed at.
const WARNING_CODES = new Set([
  "not_snapshot_consistent",
  "files_changed_during_read",
  "writer_post_backup_failed",
  "sacl_not_captured",
  "security_metadata_unreadable",
  "security_metadata_not_applied",
  "ownership_not_applied",
  "backup_privilege_not_held",
  "snapshot_release_failed",
]);

export function warningText(code: string): string {
  return WARNING_CODES.has(code) ? translate(`backup.state.warning.${code}` as MessageKey) : code;
}

/** "Tested" / "Tested (sample)", from what the last test restore proved. */
export function restoreTestLabel(testedAt: string | null, scope?: string | null): string {
  if (!testedAt) return translate("backup.state.restoreTest.notTested");
  return scope === "sampled"
    ? translate("backup.state.restoreTest.sampled")
    : translate("backup.state.restoreTest.tested");
}
