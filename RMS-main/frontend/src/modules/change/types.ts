// Mirrors backend/app/modules/change_mgmt/schema.py response/request models
// exactly (snake_case keys, matching the JSON). Change management has no
// credential fields of its own — per security invariant #3 there is no
// secret field anywhere in this module's models (see schema.py's module
// docstring).
//
// State vocabulary is sourced from the backend, not invented — see
// changeState.ts for the exact citations backing this vocabulary.

/** The states a change request can be in (change_mgmt/state.py). The interfaces keep `string`: the server may report one this console has not met. */
export type ChangeState =
  | "draft"
  | "submitted"
  | "approved"
  | "implementing"
  | "completed"
  | "rejected"
  | "cancelled";
export type ChangeRisk = "low" | "medium" | "high";
export type ChangeType = "standard" | "normal" | "emergency";
export type ChangeDecision = "approved" | "rejected";

export interface ChangeRequest {
  id: string;
  type: string;
  title: string;
  description: string;
  risk: string;
  affected: Record<string, unknown>[];
  window_start: string | null;
  window_end: string | null;
  rollback_plan: string | null;
  state: string;
  created_by: string;
  created_at: string;
  /** "auto": recorded by the platform for a patch approval or deployment
   *  made without a change. */
  origin?: string;
}

export interface ChangeCreateRequest {
  type: string;
  title: string;
  description: string;
  risk: string;
  affected?: Record<string, unknown>[];
  window_start?: string | null;
  window_end?: string | null;
  rollback_plan?: string;
}

export interface ChangeUpdateRequest {
  type?: string;
  title?: string;
  description?: string;
  risk?: string;
  affected?: Record<string, unknown>[];
  window_start?: string | null;
  window_end?: string | null;
  rollback_plan?: string;
}

export interface Approval {
  id: string;
  approver_id: string;
  decision: string;
  comment: string | null;
  is_retrospective: boolean;
  decided_at: string;
}

export interface ApprovalDecisionRequest {
  comment?: string | null;
}

export type TransitionAction = "begin" | "complete" | "cancel" | "revise";

export interface TransitionRequest {
  action: TransitionAction;
}

export interface Link {
  id: string;
  action_type: string;
  action_id: string;
  linked_at: string;
  linked_by: string;
}

export type LinkActionType = "patch_run" | "deployment" | "provision_run" | "patch_approval";

export interface LinkCreateRequest {
  action_type: LinkActionType;
  action_id: string;
}

export interface CalendarEntry {
  change_id: string;
  title: string;
  type: string;
  risk: string;
  state: string;
  window_start: string | null;
  window_end: string | null;
}

export interface CalendarConflict {
  change_a: string;
  change_b: string;
}

export interface Calendar {
  entries: CalendarEntry[];
  conflicts: CalendarConflict[];
}

export interface Evidence {
  change: ChangeRequest | null;
  approvals: Approval[];
  links: Link[];
  audit_refs: string[];
}

/** Patch approvals and/or deployments reaching this group (or every device)
 *  need an approved change. */
export interface ChangeGate {
  id: string;
  device_group_id: string | null;
  device_group_name: string | null;
  applies_to_patches: boolean;
  applies_to_deployments: boolean;
  created_at: string;
}

export interface ChangeGateSetRequest {
  device_group_id: string | null;
  applies_to_patches: boolean;
  applies_to_deployments: boolean;
}
