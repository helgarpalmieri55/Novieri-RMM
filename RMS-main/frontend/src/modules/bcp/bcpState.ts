import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { BadgeVariant } from "../jobs/jobState";
import { variantKind } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";

// bcp_test.outcome vocabulary exactly as written by the backend
// (backend/app/modules/bcp/service.py's create_test sets outcome="pending";
// backend/app/modules/bcp/schema.py's TestRecordRequest.outcome is
// `Literal["pass", "partial", "fail"]`, the only values record_test ever
// writes). No other outcome value is ever persisted to BcpTest.outcome — a
// made-up outcome name here would repeat the Phase-5 regression.
const OUTCOME_VARIANTS: Record<string, BadgeVariant> = {
  pending: "muted",
  pass: "success",
  partial: "warning",
  fail: "destructive",
};

export function outcomeVariant(outcome: string): BadgeVariant {
  return OUTCOME_VARIANTS[outcome] ?? "muted";
}

/** The same outcome as a v3 kind, for the pill and the row gutter. */
export function outcomeKind(outcome: string): Kind {
  return variantKind(outcomeVariant(outcome));
}

// bcp_finding.remediation_state vocabulary exactly as written by the backend
// (backend/app/modules/bcp/models.py: BcpFinding.remediation_state defaults
// to "open"; backend/app/modules/bcp/schema.py's FindingUpdateRequest.
// remediation_state is `Literal["open", "in_progress", "resolved"]`, the
// only values update_finding ever writes). No other state value is ever
// persisted to BcpFinding.remediation_state.
const REMEDIATION_VARIANTS: Record<string, BadgeVariant> = {
  open: "destructive",
  in_progress: "warning",
  resolved: "success",
};

export function remediationVariant(remediationState: string): BadgeVariant {
  return REMEDIATION_VARIANTS[remediationState] ?? "muted";
}

export function remediationKind(remediationState: string): Kind {
  return variantKind(remediationVariant(remediationState));
}

// bcp_finding.severity vocabulary exactly as written by the backend
// (backend/app/modules/bcp/schema.py's FindingCreateRequest.severity is
// `Literal["low", "medium", "high", "critical"]`, the only values
// create_finding ever writes to BcpFinding.severity). No other severity
// value is ever persisted.
const SEVERITY_VARIANTS: Record<string, BadgeVariant> = {
  low: "muted",
  medium: "info",
  high: "warning",
  critical: "destructive",
};

export function severityVariant(severity: string): BadgeVariant {
  return SEVERITY_VARIANTS[severity] ?? "muted";
}

export function severityKind(severity: string): Kind {
  return variantKind(severityVariant(severity));
}

// bcp_test_step.result is `Literal["pending", "pass", "fail"]`
// (bcp/schema.py's StepUpsertRequest.result). A pending step is nothing
// measured, so it takes the muted kind rather than the warning `kindOf`
// gives the word.
const STEP_KINDS: Record<string, Kind> = {
  pending: "mut",
  pass: "ok",
  fail: "crit",
};

export function stepResultKind(result: string): Kind {
  return STEP_KINDS[result] ?? "mut";
}

// The labels a pill prints. A value the console knows has its own label (gendered
// to agree with what it describes); one it has never met falls back to the
// shared status vocabulary, then to the derived label, so no wire word is ever
// printed raw.
const OUTCOMES: ReadonlySet<string> = new Set(["pending", "pass", "partial", "fail"]);
const REMEDIATION_STATES: ReadonlySet<string> = new Set(["open", "in_progress", "resolved"]);
const SEVERITIES: ReadonlySet<string> = new Set(["low", "medium", "high", "critical"]);
const STEP_RESULTS: ReadonlySet<string> = new Set(["pending", "pass", "fail"]);
const PLAN_STATES: ReadonlySet<string> = new Set(["active", "archived"]);
const TEST_TYPES: ReadonlySet<string> = new Set(["restore_drill", "failover", "tabletop", "other"]);

export function outcomeLabel(outcome: string): string {
  return OUTCOMES.has(outcome) ? humaniseEnum(outcome, "BcpOutcome") : humaniseStatus(outcome);
}

export function remediationLabel(state: string): string {
  return REMEDIATION_STATES.has(state) ? humaniseEnum(state, "RemediationState") : humaniseStatus(state);
}

export function severityLabel(severity: string): string {
  return SEVERITIES.has(severity) ? humaniseEnum(severity, "FindingSeverity") : humaniseStatus(severity);
}

export function stepResultLabel(result: string): string {
  return STEP_RESULTS.has(result) ? humaniseEnum(result, "StepResult") : humaniseStatus(result);
}

export function planStateLabel(state: string): string {
  return PLAN_STATES.has(state) ? humaniseEnum(state, "PlanState") : humaniseStatus(state);
}

export function testTypeLabel(type: string): string {
  return TEST_TYPES.has(type) ? humaniseEnum(type, "TestType") : humaniseStatus(type);
}
