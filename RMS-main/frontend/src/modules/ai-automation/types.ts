// Mirrors backend/app/modules/ai_admin/schema.py's act models exactly
// (snake_case keys, matching the JSON). THE SHIPPED API IS THE CONTRACT.
//
// **THE SPLIT BETWEEN `AiActionOut` AND `AiActionPreviewOut` IS A SECURITY
// PROPERTY, NOT A CONVENIENCE.** docs/modules/ai-automation.md §6.3 makes the
// confirmation screen's LAYOUT a control: what will happen is rendered only
// from catalogue-derived fields, and why it was suggested -- the model's
// `rationale` -- lives in a visually distinct, clearly-labelled region and never
// composes the first. The LIST model therefore carries no `rationale` at all,
// because a one-region list row cannot honour that separation and a laundered
// instruction rendered as a line in a queue of pending actions is an
// authoritative-looking action description with nothing beside it to say
// otherwise. The server omits it too; this type says so.

import { lazyLabels } from "../../i18n/lazy";
import { humaniseStatus } from "../../lib/labels";

/** One proposal, in the shape a LIST renders. No `rationale` — see above. */
export interface AiActionOut {
  id: string;
  session_id: string;
  persona: string;
  approved_action_key: string;
  job_type: string;
  target_device_id: string;
  risk_class: string;
  destructive: boolean;
  effective_autonomy: string;
  tainted: boolean;
  state: string;
  expires_at: string;
  confirmed_by: string | null;
  rejected_reason: string | null;
  job_id: string | null;
  created_at: string;
}

/**
 * §6.2's preview. Every field down to `state` is derived from the stored
 * catalogue row and the resolved target; `rationale` is the model's prose and
 * describes nothing.
 */
export interface AiActionPreviewOut {
  ai_action_id: string;
  approved_action_id: string;
  approved_action_key: string;
  name: string;
  description: string;
  job_type: string;
  rendered_params: Record<string, unknown>;
  redacted_param_paths: string[];
  script_name: string | null;
  script_version: number | null;
  target_device_id: string;
  target_hostname: string;
  risk_class: string;
  destructive: boolean;
  effective_autonomy: string;
  tainted: boolean;
  fuse_verdict: string | null;
  expires_at: string;
  preview_digest: string;
  state: string;
  /** THE MODEL'S ARGUMENT. Nothing above comes from it. */
  rationale: string;
}

/** `POST /ai/actions/{id}/confirm` — an id in the path and a digest in the body,
 * and nothing else (§6.4). There is no field here through which a job type, a
 * parameter or a target could reach the server. */
export interface AiActionConfirm {
  digest: string;
}

/** §8.2: a BOUNDED ENUM, not free text. Kept in step with
 * `app/models/ai.py::AI_ACTION_REJECTION_REASONS`, which the backend suite pins
 * against the request schema so the two literals cannot drift unnoticed. */
export const AI_REJECTION_REASONS = [
  "not_needed",
  "wrong_device",
  "wrong_action",
  "needs_change_request",
  "unsafe_now",
  "other",
] as const;

export type AiRejectionReason = (typeof AI_REJECTION_REASONS)[number];

export interface AiActionReject {
  reason: AiRejectionReason;
}

export interface AiActionDispatched {
  ai_action_id: string;
  job_id: string;
  state: string;
}

/**
 * The dial (§4). **THE CONTROL OFFERS EXACTLY THESE THREE AND NEVER
 * `autonomous`** — not because the UI is careful, but because
 * `ai_autonomy_ceiling`'s CHECK has no such value at all, so a request carrying
 * one is a 400 from the server and a stored row is impossible. The absence here
 * keeps the console honest about what the database can hold.
 */
export const AI_CEILING_LEVELS = ["off", "suggest", "require_approval"] as const;
export type AiCeilingLevel = (typeof AI_CEILING_LEVELS)[number];

/**
 * The three positions IN THE OPERATOR'S WORDS.
 *
 * `effective_autonomy` on a preview is the resolved dial position, and the
 * confirmation screen printed it as the wire identifier -- `require_approval`
 * in mono, beside facts that are all sentences. The page already translates
 * the outcome vocabulary (`OUTCOME` in `ActionsPage`); this is the same debt
 * on the value that says what the assistant was allowed to do.
 *
 * The words are read from the catalogue WHEN A FIELD IS READ (`lazyLabels`), so
 * a language switch shows on the next render. A level this table does not name
 * falls back to the shared status words rather than the raw wire token: an
 * unrecognised position must be visible, never silently relabelled as one of
 * these three.
 */
const AUTONOMY_LABEL = lazyLabels({
  off: "aiAutomation.autonomy.off",
  suggest: "aiAutomation.autonomy.suggest",
  require_approval: "aiAutomation.autonomy.require_approval",
} as const);

export function autonomyLabel(level: string): string {
  return (AUTONOMY_LABEL as Record<string, string | undefined>)[level] ?? humaniseStatus(level);
}

export const AI_PERSONAS = ["end_user", "technician", "admin"] as const;
export type AiPersona = (typeof AI_PERSONAS)[number];

export const AI_RISK_CLASSES = ["low", "medium", "high"] as const;
export type AiRiskClass = (typeof AI_RISK_CLASSES)[number];

export interface AiCeilingOut {
  persona: string;
  risk_class: string;
  level: string;
  updated_by: string | null;
  updated_at: string | null;
}

export interface AiCeilingUpdate {
  persona: AiPersona;
  risk_class: AiRiskClass;
  level: AiCeilingLevel;
}

/**
 * THE SIX REJECTION REASONS IN WORDS.
 *
 * The picker used to render `value.replace(/_/g, " ")`, which left the wire
 * enum lowercase on screen: "not needed", "wrong device", "needs change
 * request". Every other label on that route is sentence case ("Decline
 * reason", "Confirm and run", "What will happen"), so the one thing a person
 * chooses from was the one thing written in the database's vocabulary.
 *
 * The wire value is what is sent and what the audit records; only the label
 * changes. A reason this table does not name falls back to the shared status
 * words, the same rule `autonomyLabel` follows.
 */
const REJECTION_REASON_LABEL = lazyLabels({
  not_needed: "aiAutomation.rejection.not_needed",
  wrong_device: "aiAutomation.rejection.wrong_device",
  wrong_action: "aiAutomation.rejection.wrong_action",
  needs_change_request: "aiAutomation.rejection.needs_change_request",
  unsafe_now: "aiAutomation.rejection.unsafe_now",
  other: "aiAutomation.rejection.other",
} as const);

export function rejectionReasonLabel(reason: string): string {
  return (REJECTION_REASON_LABEL as Record<string, string | undefined>)[reason] ?? humaniseStatus(reason);
}

/**
 * The dial's three positions AS A CHIP LABEL — short, because the control is
 * v3's `chips(['Answer only','Propose actions','Run low-risk actions'],1)` and
 * a chip is a few words wide.
 *
 * `autonomyLabel` above is the SENTENCE form, used where one value is reported
 * in a key/value row with a line to itself. Both are needed and they are not
 * duplicates: "Held for a person to confirm" is right beside "Autonomy" on the
 * confirmation screen and wrong inside a 90px chip in a nine-cell grid. The
 * wire token stays visible on the dial page as the legend's own term, so
 * neither form hides what is stored.
 */
const CEILING_LABEL = lazyLabels({
  off: "aiAutomation.ceiling.off",
  suggest: "aiAutomation.ceiling.suggest",
  require_approval: "aiAutomation.ceiling.require_approval",
} as const);

export function ceilingLabel(level: string): string {
  return (CEILING_LABEL as Record<string, string | undefined>)[level] ?? humaniseStatus(level);
}

/**
 * The personas in words. `end_user` was printed verbatim in the dial's first
 * column, which is the same debt as the levels: a wire identifier used as the
 * visible name of a row a person reads. The identifier is kept beside the
 * label rather than replaced, the way the confirmation screen keeps a device
 * id beside a hostname.
 */
const PERSONA_LABEL = lazyLabels({
  end_user: "aiAutomation.persona.end_user",
  technician: "aiAutomation.persona.technician",
  admin: "aiAutomation.persona.admin",
} as const);

export function personaLabel(persona: string): string {
  return (PERSONA_LABEL as Record<string, string | undefined>)[persona] ?? humaniseStatus(persona);
}

/**
 * A risk class in words (masculine, under "Riesgo"). The shared status words
 * for low / medium / high are feminine, written for a priority, so the class
 * carries its own.
 */
const RISK_LABEL = lazyLabels({
  low: "aiAutomation.risk.low",
  medium: "aiAutomation.risk.medium",
  high: "aiAutomation.risk.high",
} as const);

export function riskClassLabel(risk: string): string {
  return (RISK_LABEL as Record<string, string | undefined>)[risk] ?? humaniseStatus(risk);
}

/** The states a proposal can be in (`app/models/ai.py`). The interfaces keep `string`: the server may report one this console has not met. */
export type AiActionState = "previewed" | "confirmed" | "dispatched" | "rejected" | "expired";
