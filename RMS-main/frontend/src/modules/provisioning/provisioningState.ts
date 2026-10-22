import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { BadgeVariant } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";

// provision_run state vocabulary exactly as written by the backend
// (backend/app/modules/provisioning/reporting.py:52
// RUN_STATES = ("pending", "awaiting_enrollment", "enrolled", "complete", "failed");
// models.py:71 ProvisionRun.state default "pending"; dispatch.py:83 sets
// "pending" on creation, dispatch.py:150 advances to "awaiting_enrollment";
// enroll.py:96 advances to "enrolled" on real agent enrollment; enroll.py:278
// sets "complete" on a successful provision.enroll result, enroll.py:281 sets
// "failed" otherwise). No other state values are ever persisted to
// ProvisionRun.state — a made-up state name here would repeat the Phase-5
// regression.
const PROVISION_RUN_STATE_VARIANTS: Record<string, BadgeVariant> = {
  pending: "muted",
  awaiting_enrollment: "info",
  enrolled: "info",
  complete: "success",
  failed: "destructive",
};

export function provisionRunStateVariant(state: string): BadgeVariant {
  return PROVISION_RUN_STATE_VARIANTS[state] ?? "muted";
}

/** The badge vocabulary as v3 kinds, so a `StatusPill` carries the severity the `Badge` did. */
const VARIANT_KIND: Record<BadgeVariant, Kind> = {
  muted: "mut",
  info: "run",
  success: "ok",
  destructive: "crit",
  warning: "warn",
};

export function provisionRunStateKind(state: string): Kind {
  return VARIANT_KIND[provisionRunStateVariant(state)];
}

const PROVISION_RUN_STATES: ReadonlySet<string> = new Set([
  "pending",
  "awaiting_enrollment",
  "enrolled",
  "complete",
  "failed",
]);

/**
 * A run's state as a person reads it. A state the console knows has its own
 * label; one it has never met falls back to the shared status vocabulary, then
 * to the derived label, so no wire word is ever printed raw.
 */
export function provisionRunStateLabel(state: string): string {
  return PROVISION_RUN_STATES.has(state) ? humaniseEnum(state, "ProvisionRunState") : humaniseStatus(state);
}
