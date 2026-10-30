// Mirrors backend/app/modules/deploy/api.py response/request models exactly.
// No secret fields — the module has none (installer artifacts are
// content-addressed bytes, not credentials; script bodies are not secrets
// either, though list responses omit them to keep payloads light).
//
// State vocabularies are sourced from the backend, not invented:
//   - deployment_results (DeviceResultOut.state / DeviceDeploymentHistoryOut.state):
//     pending | running | installed | failed | not_applicable
//     (app/modules/deploy/reporting.py DEPLOYMENT_RESULT_STATES)
//   - deployments (DeploymentOut.state): pending | dispatching | complete | failed
//     (app/modules/deploy/dispatch.py: "pending" default, "dispatching" on
//     execute, rolls up to "complete"/"failed")
//   - script_runs (ExposedScriptRunOut.state): pending | running | succeeded | failed
//     (app/modules/deploy/scripts.py: "running" on dispatch, then
//     "succeeded"/"failed")

export interface Package {
  id: string;
  name: string;
  version: string;
  os_family: string;
  installer_type: string;
  artifact_ref: string | null;
  artifact_sha256: string | null;
  detection_rule: Record<string, unknown>;
  uninstall_rule: Record<string, unknown>;
  install_args: string | null;
  created_at: string;
}

export interface PackageCreateRequest {
  name: string;
  version: string;
  os_family: string;
  installer_type: string;
  detection_rule?: Record<string, unknown>;
  uninstall_rule?: Record<string, unknown>;
  install_args?: string | null;
}

export type DeploymentAction = "install" | "update" | "uninstall";
// The state vocabularies above as types, so each has a label in both languages
// (`enums.<Name>.<value>`) and the enum coverage test can check it. The
// interfaces keep `string` for `state`: the server may report a state this
// console has not met, and a label lookup falls back to the plain derivation.
export type DeploymentState = "pending" | "dispatching" | "complete" | "failed";
export type DeploymentResultState = "pending" | "running" | "installed" | "failed" | "not_applicable";
export type ScriptRunState = "pending" | "running" | "succeeded" | "failed";
/** The interpreters the console has a product name for; the field accepts any text. */
export type Interpreter = "powershell" | "pwsh" | "bash" | "sh" | "zsh" | "cmd" | "python";
export const INTERPRETERS: readonly Interpreter[] = ["powershell", "pwsh", "bash", "sh", "zsh", "cmd", "python"];
export type DeployTargetType = "device" | "group";

export interface Deployment {
  id: string;
  /** The target's client, written by the server from the target; never sent. */
  client_id: string;
  package_id: string;
  action: string;
  target_type: string;
  target_id: string;
  window_cron: string | null;
  requested_by: string | null;
  state: string;
  created_at: string;
}

export interface DeploymentCreateRequest {
  package_id: string;
  action: DeploymentAction;
  target_type: DeployTargetType;
  target_id: string;
  window_cron?: string | null;
  /** An approved change this deployment implements. */
  change_id?: string | null;
}

export interface DeploymentExecuteResponse {
  job_ids: string[];
}

export interface DeviceDeploymentHistory {
  deployment_id: string;
  /** The deployment's client: the device's, written by the server. */
  client_id: string;
  package_id: string;
  action: string;
  state: string;
  exit_code: number | null;
  log_ref: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface ScriptListItem {
  id: string;
  name: string;
  os_family: string;
  interpreter: string;
  version: number;
  created_by: string | null;
  created_at: string;
}

export interface Script extends ScriptListItem {
  body: string;
}

export interface ScriptCreateRequest {
  name: string;
  os_family: string;
  interpreter: string;
  body: string;
}

export interface ScriptUpdateRequest {
  body: string;
}

export interface ScriptRunRequest {
  target_type: DeployTargetType;
  target_id: string;
}

export interface ScriptRunResponse {
  job_ids: string[];
}

/** A scheduled script: which script, on which group, how often. */
export interface ScriptPolicy {
  id: string;
  name: string;
  script_id: string;
  script_name: string;
  device_group_id: string;
  device_group_name: string;
  interval_hours: number;
  in_maintenance_window: boolean;
  alert_on_failure: boolean;
  keep_runs: number;
  enabled: boolean;
  devices_failing: number;
  devices_succeeding: number;
  last_run_at: string | null;
}

export interface ScriptPolicyCreateRequest {
  name: string;
  script_id: string;
  device_group_id: string;
  interval_hours: number;
  in_maintenance_window: boolean;
  alert_on_failure: boolean;
  keep_runs: number;
  enabled: boolean;
}

export type ScriptPolicyUpdateRequest = Partial<
  Pick<
    ScriptPolicyCreateRequest,
    "name" | "interval_hours" | "in_maintenance_window" | "alert_on_failure" | "keep_runs" | "enabled"
  >
>;

/** One run of a scheduled script on one device. */
export interface ScriptPolicyRun {
  id: string;
  device_id: string;
  hostname: string | null;
  script_version: number;
  state: string;
  exit_code: number | null;
  output: string | null;
  created_at: string;
  finished_at: string | null;
}
