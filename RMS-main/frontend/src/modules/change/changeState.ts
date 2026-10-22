import type { BadgeVariant } from "../jobs/jobState";
import { variantKind } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { ChangeDecision, ChangeRisk, ChangeState, ChangeType, LinkActionType } from "./types";

// change_request.state vocabulary exactly as written by the backend
// (backend/app/modules/change_mgmt/models.py:45
// ChangeRequest.state: Mapped[str] = mapped_column(String(16), default="draft");
// migration 0016_change_mgmt.py's RLS'd change_request table; the full state
// set and transitions are enumerated in
// backend/app/modules/change_mgmt/state.py's `next_state`, which only ever
// returns one of: "draft" | "submitted" | "approved" | "implementing" |
// "completed" | "rejected" | "cancelled"). No other state values are ever
// persisted to ChangeRequest.state — a made-up state name here would repeat
// the Phase-5 regression.
const CHANGE_STATE_VARIANTS: Record<string, BadgeVariant> = {
  draft: "muted",
  submitted: "info",
  approved: "info",
  implementing: "warning",
  completed: "success",
  rejected: "destructive",
  cancelled: "muted",
};

export function changeStateVariant(state: string): BadgeVariant {
  return CHANGE_STATE_VARIANTS[state] ?? "muted";
}

/**
 * The same vocabulary as a v3 kind, for the pill and the row gutter.
 *
 * `submitted` is the one state that differs from its badge variant: v3 draws
 * "Awaiting approval" as the warn pill, because somebody is blocked until a
 * person decides, and the queue tile above the list is already that colour.
 * Everything else is the variant's own kind.
 */
export function changeStateKind(state: string): Kind {
  if (state === "submitted") return "warn";
  return variantKind(changeStateVariant(state));
}

// change_request.risk is `Literal["low", "medium", "high"]` in
// change_mgmt/schema.py. v3 draws high as the critical pill, low as the
// muted one; medium takes the warning between them.
const RISK_KINDS: Record<string, Kind> = {
  high: "crit",
  medium: "warn",
  low: "mut",
};

/** The kind a risk level carries. An unknown level is muted, never a colour it has not earned. */
export function riskKind(risk: string): Kind {
  return RISK_KINDS[risk] ?? "mut";
}

const STATES: ReadonlySet<string> = new Set<ChangeState>([
  "draft",
  "submitted",
  "approved",
  "implementing",
  "completed",
  "rejected",
  "cancelled",
]);
const RISKS: ReadonlySet<string> = new Set<ChangeRisk>(["low", "medium", "high"]);
const TYPES: ReadonlySet<string> = new Set<ChangeType>(["standard", "normal", "emergency"]);
const DECISIONS: ReadonlySet<string> = new Set<ChangeDecision>(["approved", "rejected"]);
const LINK_TYPES: ReadonlySet<string> = new Set<LinkActionType>([
  "patch_run",
  "deployment",
  "provision_run",
  "patch_approval",
]);

// A value the console knows has its own label; one it has never met falls back
// to the shared status vocabulary, never to the raw wire word.

/** A change's state as a person reads it. */
export function changeStateLabel(state: string): string {
  return STATES.has(state) ? humaniseEnum(state, "ChangeState") : humaniseStatus(state);
}

/** A risk level as a person reads it. */
export function riskLabel(risk: string): string {
  return RISKS.has(risk) ? humaniseEnum(risk, "ChangeRisk") : humaniseStatus(risk);
}

/** A change's type as a person reads it. */
export function changeTypeLabel(type: string): string {
  return TYPES.has(type) ? humaniseEnum(type, "ChangeType") : humaniseStatus(type);
}

/** What an approver decided, as a person reads it. */
export function decisionLabel(decision: string): string {
  return DECISIONS.has(decision) ? humaniseEnum(decision, "ChangeDecision") : humaniseStatus(decision);
}

/** What kind of platform action a link points at. */
export function linkActionLabel(type: string): string {
  return LINK_TYPES.has(type) ? humaniseEnum(type, "LinkActionType") : humaniseStatus(type);
}
