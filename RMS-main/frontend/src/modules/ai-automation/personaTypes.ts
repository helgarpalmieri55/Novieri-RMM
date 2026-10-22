// The two non-admin personas' wire shapes (§12).
//
// **THE RESOLVER'S RESPONSE IS DELIBERATELY SMALLER THAN THE COPILOT'S**, and
// the asymmetry is the point rather than an oversight. `ResolverTurnOut` carries
// no `model_id`, no token counts, no `stop_reason` vocabulary and no tool names,
// because it is read by a CUSTOMER and every one of those is internal detail
// about how the answer was produced. `flagged_count` survives where the
// copilot's `flagged` list does: "something in this conversation looked odd" is
// actionable for a requester; a rule name and a record id are reconnaissance.
//
// Mirrors `app/modules/ai_copilot/schema.py` and
// `app/modules/ai_portal/schema.py` exactly.

export interface AiCopilotSession {
  id: string;
  persona: string;
  mode: string;
  model_id: string;
  message_count: number;
}

export interface AiCopilotFlag {
  tool: string;
  source_type: string;
  source_id: string;
  matched_rule: string;
}

export interface AiCopilotTurn {
  answer: string;
  stop_reason: string;
  partial: boolean;
  tool_calls: Record<string, unknown>[];
  flagged: AiCopilotFlag[];
  /** The ids of any `ai_action` rows this turn proposed. IDS ONLY: the preview a
   * human confirms is served by the admin surface's own route, so §6.3's
   * two-region layout is implemented in exactly one place. */
  proposed: string[];
}

export interface AiResolverSession {
  id: string;
  mode: string;
  message_count: number;
}

export interface AiResolverTurn {
  answer: string;
  partial: boolean;
  flagged_count: number;
}
