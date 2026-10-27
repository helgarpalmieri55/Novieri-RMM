// Mirrors backend/app/modules/bcp/schema.py response/request models exactly
// (snake_case keys, matching the JSON). Per security invariant #3, BCP/DR
// has no credential fields of its own — no secret value appears anywhere in
// this module's models (see schema.py's module docstring).
//
// The outcome/remediation_state/severity vocabularies are sourced from the
// backend, not invented — see bcpState.ts for the exact citations backing
// each vocabulary.

export interface PlanScopeEntry {
  kind: string;
  id: string;
}

export interface PlanOut {
  id: string;
  /** The client the plan is for (MSP clients phase 2); fixed for its life. */
  client_id: string;
  name: string;
  description: string;
  scope: PlanScopeEntry[];
  rto_minutes: number | null;
  rpo_minutes: number | null;
  procedure_refs: string[];
  dependencies: unknown[];
  review_interval_days: number;
  last_reviewed_at: string | null;
  approved_by: string | null;
  state: string;
  created_by: string;
  created_at: string;
}

export interface RestoreProvenCoverage {
  total: number;
  restore_proven: number;
}

export interface PlanStatusOut {
  id: string;
  name: string;
  state: string;
  review_overdue: boolean;
  last_reviewed_at: string | null;
  last_test_outcome: string | null;
  open_findings: number;
  restore_proven_coverage: RestoreProvenCoverage;
}

export interface BcpEvidenceDevice {
  device_id: string;
  hostname: string;
  covered: boolean;
  last_backup_taken_at: string | null;
  verified_at: string | null;
  restore_tested_at: string | null;
  restore_proven: boolean;
}

export interface BcpEvidenceSummary {
  total_n: number;
  covered_n: number;
  restore_proven_n: number;
}

export interface BcpEvidence {
  captured_at: string;
  devices: BcpEvidenceDevice[];
  summary: BcpEvidenceSummary;
}

// The frozen evidence snapshot is `{}` until a test has been recorded (see
// app/modules/bcp/service.py's record_test — `BcpTest.evidence` defaults to
// an empty JSONB dict and is only ever populated by that one write path).
export type BcpEvidenceValue = BcpEvidence | Record<string, never>;

export type TestType = "restore_drill" | "failover" | "tabletop" | "other";

// A test's outcome, as the backend writes it (see bcpState.ts). The column is
// free text server-side, so `TestOut.outcome` keeps `string`; this union is
// what enums.coverage.test.ts reads to prove every value has a label.
export type BcpOutcome = "pending" | "pass" | "partial" | "fail";

export interface TestOut {
  id: string;
  plan_id: string;
  type: string;
  scheduled_for: string | null;
  executed_at: string | null;
  executed_by: string | null;
  outcome: string;
  rto_met: boolean | null;
  rpo_met: boolean | null;
  evidence: BcpEvidenceValue;
  notes: string;
  created_at: string;
}

export type StepResult = "pending" | "pass" | "fail";

export interface StepOut {
  id: string;
  test_id: string;
  ordinal: number;
  description: string;
  result: string;
  evidence_ref: string | null;
}

export type FindingSeverity = "low" | "medium" | "high" | "critical";
export type RemediationState = "open" | "in_progress" | "resolved";

export interface FindingOut {
  id: string;
  test_id: string;
  description: string;
  severity: string;
  remediation_state: string;
  change_ref: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface TestReportOut {
  test: TestOut | null;
  plan: PlanOut | null;
  steps: StepOut[];
  findings: FindingOut[];
  evidence: BcpEvidenceValue;
}

export interface PeriodReportOut {
  outcome_counts: Record<string, number>;
  plans_tested: string[];
  open_findings_by_severity: Record<string, number>;
  review_overdue_plans: string[];
}

export interface PlanCreateRequest {
  /** The client the plan is for. It must use continuity, or the server
   *  answers 404 with the sentence that says so; the scope and procedures
   *  must be its own. */
  client_id: string;
  name: string;
  description?: string;
  scope?: PlanScopeEntry[];
  rto_minutes?: number | null;
  rpo_minutes?: number | null;
  procedure_refs?: string[];
  dependencies?: unknown[];
  review_interval_days?: number;
}

// Plan lifecycle state, exactly the backend vocabulary (bcp_plan.state /
// PlanUpdateRequest.state Literal). "archived" retires a plan from active DR
// coverage without deleting its historical tests/evidence.
export type PlanState = "active" | "archived";

export interface PlanUpdateRequest {
  name?: string;
  description?: string;
  scope?: PlanScopeEntry[];
  rto_minutes?: number | null;
  rpo_minutes?: number | null;
  procedure_refs?: string[];
  dependencies?: unknown[];
  review_interval_days?: number;
  state?: PlanState;
}

export interface TestCreateRequest {
  type: TestType;
  scheduled_for?: string | null;
  notes?: string;
}

export interface TestRecordRequest {
  executed_at?: string | null;
  rto_met?: boolean | null;
  rpo_met?: boolean | null;
  outcome: "pass" | "partial" | "fail";
  notes?: string;
}

export interface StepUpsertRequest {
  ordinal: number;
  description: string;
  result?: StepResult;
  evidence_ref?: string | null;
}

export interface FindingCreateRequest {
  description: string;
  severity: FindingSeverity;
}

export interface FindingUpdateRequest {
  remediation_state?: RemediationState;
  change_ref?: string | null;
  description?: string;
}
