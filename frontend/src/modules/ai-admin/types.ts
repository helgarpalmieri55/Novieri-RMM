// Mirrors backend/app/modules/ai_admin/schema.py exactly (snake_case keys,
// matching the JSON) plus api.py's route signatures. THE SHIPPED API IS THE
// CONTRACT — these types were written against `schema.py` directly, not against
// the spec's prose, and the API wins on any disagreement.
//
// NOTHING HERE CARRIES A BASE URL, A HOST OR A KEY (spec §0.10). That is not a
// convention this file follows out of tidiness: the server has no such field on
// any request model and `test_ai_admin_providers.py` asserts it over every
// Pydantic model in the package. And since §0.14 nothing here carries a
// provider SELECTION either — the provider, its model and its key are the
// deployment's `.env`, so an org-admin picks nothing at all.
//
// Per invariant #6 nothing here is a permission. The server's
// `require_permission` on every route is the only authority; the `canX` flags
// this module passes around decide only what RENDERS.

/** `GET /ai/status`. Two fields, deliberately: §10.6's disabled panel needs to
 * say WHY, and an `ai.use` holder is not entitled to anything about the
 * server's configuration. */
export interface AiStatus {
  enabled: boolean;
  reason: string;
}

export interface AiSessionOut {
  id: string;
  title: string;
  persona: string;
  mode: string;
  model_id: string;
  created_at: string;
  last_turn_at: string;
  message_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
}

/** `ai_message.tool_calls`' five keys (spec §3.2) — and **no result payload**.
 * `arg_hash` is present on stored rows and absent on the live turn's echo. */
export interface AiToolCall {
  tool: string;
  arg_hash?: string;
  result_count: number;
  truncated: boolean;
  status: string;
}

export interface AiMessageOut {
  id: string;
  seq: number;
  role: string;
  content: string;
  tool_calls: AiToolCall[] | null;
  flags: Record<string, unknown>;
  input_tokens: number | null;
  output_tokens: number | null;
  created_at: string;
}

export interface AiTranscriptOut {
  session: AiSessionOut;
  messages: AiMessageOut[];
}

export interface AiTurnRequest {
  question: string;
  turn_id?: string;
}

/** §8.4's third obligation, as it reaches the console: counts and record ids
 * only. The excerpt prose lives in `ai_injection_flag` and is served on the
 * `ai.admin` route, because attacker-controlled prose read across every
 * conversation in the org is an administrative act. */
export interface AiFlagged {
  tool: string;
  source_type: string;
  source_id: string;
  matched_rule: string;
}

export interface AiTurnOut {
  turn_id: string;
  answer: string;
  stop_reason: string;
  partial: boolean;
  tool_calls: AiToolCall[];
  input_tokens: number;
  output_tokens: number;
  duration_ms: number;
  model_id: string;
  flagged: AiFlagged[];
  warnings: string[];
}

/** The org's dials. **Whether the assistant runs is not one of them** — that is
 * `AiStatus`, which the server answers from the deployment's `ITS_AI_*`
 * configuration (spec §0.14). */
export interface AiSettingsOut {
  transcript_retention_days: number;
  max_turns_per_user_per_minute: number;
  max_turns_per_org_per_hour: number;
  daily_token_budget: number | null;
}

/** **Nothing here can turn the assistant on or aim it** (spec §0.14, the
 * operator's decision of 2026-08-25). This carried `enabled`, `provider_key`
 * and a required `egress_acknowledged` until that decision moved the provider,
 * its model and its key into server `.env`. `SettingsUpdate` on the server is
 * `extra="forbid"`, so re-adding any of the three here is a 422 rather than a
 * silently ignored field. */
export interface AiSettingsUpdate {
  transcript_retention_days?: number;
  max_turns_per_user_per_minute?: number;
  max_turns_per_org_per_hour?: number;
  daily_token_budget?: number | null;
}

/** The ONE provider this deployment is configured with — key, kind and model
 * name only. **Never the base URL, never key material** — a base URL in this
 * response is a base URL in a browser's network tab, a bug report and a
 * screenshot. Rendered as a statement of where data goes, never as a chooser
 * (spec §0.14). */
export interface AiProviderOut {
  key: string;
  kind: string;
  model: string;
}

export interface AiInjectionFlagOut {
  id: string;
  session_id: string;
  message_seq: number;
  source_tool: string;
  source_type: string;
  source_id: string;
  matched_rule: string;
  excerpt: string;
  created_at: string;
}

/** One rendered turn. The view model `MessageList` takes, so the list is
 * testable against props rather than against a mocked query client. */
export interface AiRenderedTurn {
  id: string;
  role: "user" | "assistant";
  content: string;
  toolCalls: AiToolCall[];
  flagged: AiFlagged[];
  partial?: boolean;
  stopReason?: string;
}

/** The detector's rule names (`app/core/ai/detect.py::_RULES`). The wire field stays `string`: a rule added server-side must still render. */
export type AiInjectionRule = "redirection" | "role_assertion" | "tool_coercion" | "nonce_forgery";

/**
 * The assistant's tools, as the wire names them with `.` written `_` (the
 * catalogue's path separator is a `.`, so `devices.health` is keyed
 * `devices_health`). The wire field stays `string`.
 */
export type AiTool =
  | "devices_get"
  | "devices_health"
  | "health_alerts"
  | "portal_my_tickets"
  | "portal_my_ticket"
  | "portal_categories"
  | "portal_help_suggest"
  | "portal_help_article"
  | "actions_propose";

/** What kind of record a flagged excerpt came from. The wire field stays `string`. */
export type AiSourceType = "ticket" | "document" | "device" | "alert" | "article" | "category";
