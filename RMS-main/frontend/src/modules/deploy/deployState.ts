import type { BadgeVariant } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";

// deployment_results state vocabulary exactly as written by the backend:
// pending/running (dispatch.py:172), installed/failed/not_applicable
// (dispatch.py:140,245,253-260; reporting.py:35 DEPLOYMENT_RESULT_STATES).
// No other state values are ever persisted to DeviceResultOut /
// DeviceDeploymentHistoryOut.
const DEPLOYMENT_RESULT_STATE_VARIANTS: Record<string, BadgeVariant> = {
  pending: "muted",
  running: "info",
  installed: "success",
  failed: "destructive",
  not_applicable: "muted",
};

export function deploymentResultStateVariant(state: string): BadgeVariant {
  return DEPLOYMENT_RESULT_STATE_VARIANTS[state] ?? "muted";
}

// deployments state vocabulary exactly as written by the backend
// (dispatch.py: "pending" default at creation, api.py:394; "dispatching" set
// on execute, dispatch.py:177; rolls up to "complete" or "failed" once every
// sibling DeploymentResult is terminal, dispatch.py:288-293).
const DEPLOYMENT_STATE_VARIANTS: Record<string, BadgeVariant> = {
  pending: "muted",
  dispatching: "info",
  complete: "success",
  failed: "destructive",
};

export function deploymentStateVariant(state: string): BadgeVariant {
  return DEPLOYMENT_STATE_VARIANTS[state] ?? "muted";
}

// script_runs state vocabulary exactly as written by the backend
// (scripts.py: "pending" default, models.py:122; "running" on dispatch,
// scripts.py:228; terminal "succeeded"/"failed", scripts.py:37,280-286).
const SCRIPT_RUN_STATE_VARIANTS: Record<string, BadgeVariant> = {
  pending: "muted",
  running: "info",
  succeeded: "success",
  failed: "destructive",
};

export function scriptRunStateVariant(state: string): BadgeVariant {
  return SCRIPT_RUN_STATE_VARIANTS[state] ?? "muted";
}

/**
 * The previous design's badge vocabulary as v3 kinds, so a `StatusPill`
 * carries the same severity the `Badge` did: muted is nothing happening,
 * info is the platform doing something, success is done, destructive is a
 * failure that has already happened, warning is something to watch.
 */
const VARIANT_KIND: Record<BadgeVariant, Kind> = {
  muted: "mut",
  info: "run",
  success: "ok",
  destructive: "crit",
  warning: "warn",
};

export function deploymentStateKind(state: string): Kind {
  return VARIANT_KIND[deploymentStateVariant(state)];
}

export function deploymentResultStateKind(state: string): Kind {
  return VARIANT_KIND[deploymentResultStateVariant(state)];
}

export function scriptRunStateKind(state: string): Kind {
  return VARIANT_KIND[scriptRunStateVariant(state)];
}
