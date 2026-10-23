// The transcript, rendered.
//
// THREE THINGS THIS COMPONENT OWES THE ADMINISTRATOR, each a separate failure if
// it is missing:
//
//   1. **The answer, through `renderAiMarkdown` and nothing else** (§0.13). Model
//      output is derived from unauthenticated attacker-controlled text, so the
//      renderer is the last place this module can leak — see `aiMarkdown.ts`.
//   2. **A partial answer that says it is partial** (§10.1). The server appends
//      its own notice prose to the answer, because the deadline and
//      provider-error paths have no model left to ask. This component adds its
//      own marker from `stop_reason` as well: the two are independent, and a
//      panel that dropped `partial` would otherwise still show the server's
//      sentence and read as complete.
//   3. **A flagged record named on the answer, WITH the answer** (§8.4 clause
//      3). Refusing to answer because retrieved text tried to redirect the agent
//      is the denial of service §8.4 forbids — the summary stands, and the
//      record is named beside it.
//
// The stop-reason phrases below are PRESENTATION, not a second implementation of
// a server decision: the server decides whether a turn was partial and why, and
// this map only turns its enum into a sentence. Nothing here recomputes anything.
//
// THE QUESTION IS DRAWN IN THE CONTROL BOX (`.inp`'s edge, radius and wash),
// because it is the operator's own words echoed back; the answer sits on the
// panel itself. Every value is a token -- there is no chat piece in the kit,
// and this file does not invent one.

import { useT } from "../../i18n/context";
import { lazyLabels } from "../../i18n/lazy";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";
import { renderAiMarkdown } from "./aiMarkdown";
import { ToolTrace } from "./ToolTrace";
import type { AiFlagged, AiRenderedTurn } from "./types";

const STOP_REASON_TEXT = lazyLabels({
  tool_budget: "aiAdmin.messages.stop.tool_budget",
  byte_budget: "aiAdmin.messages.stop.byte_budget",
  token_budget: "aiAdmin.messages.stop.token_budget",
  deadline: "aiAdmin.messages.stop.deadline",
  malformed: "aiAdmin.messages.stop.malformed",
  nonce_leak: "aiAdmin.messages.stop.nonce_leak",
  provider_error: "aiAdmin.messages.stop.provider_error",
  session_expired: "aiAdmin.messages.stop.session_expired",
} as const);

function InjectionNotice({ flagged }: { flagged: AiFlagged[] }) {
  const t = useT();
  const records = flagged.map((flag) => flag.source_id).join(", ");
  return (
    <p data-testid="ai-injection-flag" className="notice gold" style={{ marginTop: 8 }}>
      <Icon name={KIND_ICON.warn} size={16} />
      <span>{t("aiAdmin.messages.injection", { records })}</span>
    </p>
  );
}

function PartialNotice({ stopReason }: { stopReason?: string }) {
  const t = useT();
  const because = stopReason ? (STOP_REASON_TEXT as Record<string, string | undefined>)[stopReason] : undefined;
  return (
    <p data-testid="ai-partial" className="note" style={{ marginTop: 8, fontWeight: 500 }}>
      {because
        ? t("aiAdmin.messages.partial", { because })
        : t("aiAdmin.messages.partialUnknown")}
    </p>
  );
}

export function MessageList({ turns }: { turns: AiRenderedTurn[] }) {
  useT();
  return (
    <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 14 }}>
      {turns.map((entry) => (
        <li
          key={entry.id}
          style={
            entry.role === "user"
              ? {
                  background: "var(--surface-2)",
                  border: "1px solid var(--line)",
                  borderRadius: "var(--r-ctl)",
                  padding: "8px 11px",
                }
              : undefined
          }
        >
          {entry.role === "user" ? (
            // A question is the administrator's own words and is NOT markdown:
            // rendering it through the parser would hand an admin's own typo a
            // formatting meaning, and there is nothing to gain from it.
            <p style={{ whiteSpace: "pre-wrap" }}>{entry.content}</p>
          ) : (
            <div
              className="ai-answer"
              // The ONLY `dangerouslySetInnerHTML` in this module, and its input
              // is always `renderAiMarkdown`'s output — never a server string,
              // never model output directly.
              dangerouslySetInnerHTML={{ __html: renderAiMarkdown(entry.content) }}
            />
          )}
          {entry.role === "assistant" && entry.flagged.length > 0 ? (
            <InjectionNotice flagged={entry.flagged} />
          ) : null}
          {entry.role === "assistant" && entry.partial ? (
            <PartialNotice stopReason={entry.stopReason} />
          ) : null}
          {entry.role === "assistant" ? <ToolTrace toolCalls={entry.toolCalls} /> : null}
        </li>
      ))}
    </ol>
  );
}
