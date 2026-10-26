/**
 * Presentation for push runs (design 2026-09-30 §1.7).
 *
 * Every server code reaches the screen as words: a short chip for a readiness
 * reason (with the server's longer remedy as its title), a sentence for a
 * refusal or an outcome. A code this console has not been taught falls back
 * to a neutral word, never to the code itself (`noWireNames.test.ts`).
 */

import { translate } from "../../i18n/core";
import { lazyLabels } from "../../i18n/lazy";
import { ApiError } from "../../lib/api";
import type { Kind } from "../../shell/components";
import { refusalReasonLabel } from "./pushState";
import type { PushRunCounts, PushRunState, PushRunTargetState } from "./types";

// The labels below are accessors over the catalogue (`lazyLabels`): they are
// read when drawn, so a language switch re-words them.
export const RUN_STATE_LABELS: Readonly<Record<PushRunState, string>> = lazyLabels<PushRunState>({
  running: "agentPush.runState.running",
  paused: "agentPush.runState.paused",
  stopped_credential: "agentPush.runState.stopped_credential",
  cancelled: "agentPush.runState.cancelled",
  completed: "agentPush.runState.completed",
});

export const RUN_STATE_KIND: Readonly<Record<PushRunState, Kind>> = {
  running: "run",
  paused: "warn",
  stopped_credential: "crit",
  cancelled: "mut",
  completed: "ok",
};

/** The states a person may Resume from (spec §1.4a, §1.5). */
export function mayResume(state: PushRunState): boolean {
  return state === "paused" || state === "stopped_credential";
}

/** The states a person may Cancel from: every active one. */
export function mayCancel(state: PushRunState): boolean {
  return state === "running" || state === "paused" || state === "stopped_credential";
}

export const TARGET_STATE_LABELS: Readonly<Record<PushRunTargetState, string>> = lazyLabels<PushRunTargetState>({
  waiting: "agentPush.targetState.waiting",
  in_flight: "agentPush.targetState.in_flight",
  installed: "agentPush.targetState.installed",
  failed: "agentPush.targetState.failed",
  refused: "agentPush.targetState.refused",
  cancelled: "agentPush.targetState.cancelled",
});

export const TARGET_STATE_KIND: Readonly<Record<PushRunTargetState, Kind>> = {
  waiting: "mut",
  in_flight: "run",
  installed: "ok",
  failed: "crit",
  refused: "warn",
  cancelled: "mut",
};

/** Short chip text for a readiness reason; the long remedy goes in a title. */
export const READINESS_CHIPS: Readonly<Record<string, string>> = lazyLabels({
  no_asset: "agentPush.chip.no_asset",
  ambiguous_asset: "agentPush.chip.ambiguous_asset",
  no_authorization: "agentPush.chip.no_authorization",
  credential_not_scoped: "agentPush.chip.credential_not_scoped",
  no_site_server: "agentPush.chip.no_site_server",
  site_server_too_old: "agentPush.chip.site_server_too_old",
  no_winrm_site_server: "agentPush.chip.no_winrm_site_server",
  no_dns_name: "agentPush.chip.no_dns_name",
  invalid_dns_name: "agentPush.chip.invalid_dns_name",
  wrong_credential_kind: "agentPush.chip.wrong_credential_kind",
  no_console_url: "agentPush.chip.no_console_url",
  outside_realm: "agentPush.chip.outside_realm",
  realm_not_recorded: "agentPush.chip.realm_not_recorded",
  host_key_unconfirmed: "agentPush.chip.host_key_unconfirmed",
  host_key_changed: "agentPush.chip.host_key_changed",
  not_installable: "agentPush.chip.not_installable",
});

export function readinessChip(code: string): string {
  return READINESS_CHIPS[code] ?? translate("agentPush.chip.fallback");
}

/** What a target's job came to, once the site server answered. */
const OUTCOME_LABELS: Readonly<Record<string, string>> = lazyLabels({
  installed: "agentPush.outcome.installed",
  os_mismatch: "agentPush.outcome.os_mismatch",
  unreachable: "agentPush.outcome.unreachable",
  auth_failed: "agentPush.outcome.auth_failed",
  failed: "agentPush.outcome.failed",
  job_failed: "agentPush.outcome.job_failed",
  agent_refused: "agentPush.outcome.agent_refused",
  expired: "agentPush.outcome.expired",
  job_cancelled: "agentPush.outcome.job_cancelled",
  lost: "agentPush.outcome.lost",
  job_missing: "agentPush.outcome.job_missing",
});

export function outcomeLabel(code: string): string {
  return OUTCOME_LABELS[code] ?? translate("agentPush.outcome.fallback");
}

/** The refusals only a run reaches (`runs.RUN_REFUSAL_REASONS`). */
const RUN_REASON_LABELS: Readonly<Record<string, string>> = lazyLabels({
  run_owner_not_permitted: "agentPush.runReason.run_owner_not_permitted",
  credential_missing: "agentPush.runReason.credential_missing",
  target_changed: "agentPush.runReason.target_changed",
  already_enrolled: "agentPush.runReason.already_enrolled",
  not_installable: "agentPush.runReason.not_installable",
  no_release: "agentPush.runReason.no_release",
  credential_unreadable: "agentPush.runReason.credential_unreadable",
  site_server_awaiting_review: "agentPush.runReason.site_server_awaiting_review",
});

export function runReasonLabel(code: string, help?: string): string {
  return RUN_REASON_LABELS[code] ?? refusalReasonLabel(code, help);
}

/** Percent of the run that is finished, whatever the outcome. */
export function runProgress(counts: PushRunCounts, total: number): number {
  if (total <= 0) return 0;
  const done = counts.installed + counts.failed + counts.refused + counts.cancelled;
  return (done / total) * 100;
}

/** Still changing: running, or with installs in flight to settle. */
export function runIsLive(run: { state: PushRunState; counts: PushRunCounts }): boolean {
  return run.state === "running" || run.counts.in_flight > 0;
}

export function countsSentence(counts: PushRunCounts): string {
  return [
    translate("agentPush.counts.installed", { count: counts.installed }),
    translate("agentPush.counts.failed", { count: counts.failed }),
    translate("agentPush.counts.refused", { count: counts.refused }),
    translate("agentPush.counts.toGo", { count: counts.waiting + counts.in_flight }),
  ].join(" · ");
}

/** The server's structured 409 for a list that changed since it was shown. */
export function isListChanged(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  try {
    const body = JSON.parse(error.message) as { detail?: { code?: unknown } | string };
    return typeof body.detail === "object" && body.detail?.code === "list_changed";
  } catch {
    return false;
  }
}
