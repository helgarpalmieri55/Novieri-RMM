// Mirrors backend/app/modules/provisioning/schema.py response/request models
// exactly (snake_case keys, matching the JSON). No secret fields — a profile
// never carries a credential value, a `{"__sealed__": ...}` vault ref, a
// token, or answer-file bytes (see schema.py's module docstring and
// api.py._redact_post_config). Profiles expose only the two
// has_*_credential booleans as the sole signal that a credential is set.
//
// State vocabulary is sourced from the backend, not invented:
//   - provision_run.state: pending | awaiting_enrollment | enrolled | complete | failed
//     (backend/app/modules/provisioning/reporting.py:52 RUN_STATES)
// See provisioningState.ts for the exact citations backing this vocabulary.

// The vocabularies the console words, as unions: enums.coverage.test.ts reads
// them to prove every value has a label in both languages. The columns are
// free text server-side, so the row types keep `string`; a value outside
// these falls back to a derived label.
export type ProvisionRunState = "pending" | "awaiting_enrollment" | "enrolled" | "complete" | "failed";
export type OsImageKind = "install" | "answer_file" | "image" | "build" | "enrollment";
export type OsImageState = "active" | "draft";

export interface OsImage {
  id: string;
  os_family: string;
  name: string;
  kind: string;
  artifact_ref: string | null;
  artifact_sha256: string | null;
  params: Record<string, unknown>;
  state: string;
  created_at: string;
}

export interface OsImageCreateRequest {
  os_family: string;
  name: string;
  kind: string;
  params?: Record<string, unknown>;
}

export interface ProvisionProfile {
  id: string;
  name: string;
  image_id: string;
  post_config: Record<string, unknown>;
  target_scope: Record<string, unknown>;
  enabled: boolean;
  created_at: string;
  has_domain_join_credential: boolean;
  has_local_admin_credential: boolean;
}

export interface ProfileUpsertRequest {
  name: string;
  image_id: string;
  post_config?: Record<string, unknown>;
  target_scope?: Record<string, unknown>;
  enabled?: boolean;
}

export interface ProvisionRun {
  id: string;
  profile_id: string;
  device_id: string | null;
  target_ref: string | null;
  state: string;
  answer_ref: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface RunStartRequest {
  target_ref?: string | null;
  wake_on_lan?: Record<string, unknown> | null;
}

export interface RunStartResponse {
  run_id: string;
  state: string;
}

// success_rate is a FRACTION in [0,1] (schema.py FleetProvisioningReportOut) —
// multiply by 100 only at the point of display, never here.
export interface FleetProvisioningReport {
  run_counts_by_state: Record<string, number>;
  success_rate: number;
  machines_provisioned: number;
  per_profile: Record<string, unknown>;
}

export interface DeviceProvisioning {
  device_id: string;
  profile_id: string | null;
  image_id: string | null;
  enrolled_at: string | null;
  latest_run_state: string | null;
}
