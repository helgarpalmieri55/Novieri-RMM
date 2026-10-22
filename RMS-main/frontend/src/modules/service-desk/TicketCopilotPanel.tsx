// The technician copilot, inside one ticket (docs/modules/ai-automation.md
// §0.3, §3.2, §12).
//
// **IT DRAFTS; IT NEVER POSTS.** There is no "post this reply" button in this
// file and there must never be one: the answer is text the technician copies
// into their own editor and sends under their own name — because a human does
// send it. The reasons are §0.3's and they are not about tidiness: the SLA
// engine's response clock stops at the first public comment from someone who is
// not the requester, so a copilot that can post is a metric-laundering machine
// with a natural-language interface; and a posted comment is customer-visible
// output attributed to a named human who did not write it.
//
// **A PROPOSAL RENDERS AS A LINK TO THE CONFIRMATION SCREEN, NOT AS A PREVIEW
// HERE.** §6.3's two-region separation is implemented once, in
// `ai-automation/ActionPreview.tsx`; a second preview rendering in this panel
// would be a second place that separation could be got wrong, in the surface
// that reads the most attacker-controlled text.

import { useState, type ReactElement } from "react";
import { Link } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { Button, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { useApi } from "../../lib/ApiContext";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import { Panel } from "../../shell/components";
import type { AiCopilotTurn } from "../ai-automation/personaTypes";

const STOP_REASONS: ReadonlySet<string> = new Set([
  "answer",
  "tool_budget",
  "byte_budget",
  "token_budget",
  "deadline",
  "malformed",
  "nonce_leak",
  "provider_error",
  "provider_rate_limited",
  "session_expired",
  "internal_error",
]);

/** Why a turn stopped, as a person reads it; a reason the console has not met reads through the status words. */
function stopReasonLabel(reason: string): string {
  return STOP_REASONS.has(reason)
    ? humaniseEnum(reason, "CopilotStopReason")
    : humaniseStatus(reason).toLocaleLowerCase();
}

export function TicketCopilotPanel({ ticketId }: { ticketId: string }): ReactElement {
  const t = useT();
  const api = useApi();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [turn, setTurn] = useState<AiCopilotTurn | null>(null);
  // The caught failure itself, worded at render: a sentence kept in state would
  // stay in the language the request failed in.
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function ask(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      let id = sessionId;
      if (id === null) {
        const created = await api.createCopilotSession(ticketId, "act");
        id = created.id;
        setSessionId(id);
      }
      setTurn(await api.createCopilotTurn(ticketId, id, question));
    } catch (caught) {
      // Cycle 4: `caught.message` is "" for a bodyless ApiError (a proxy 503),
      // which rendered as nothing at all -- the box did not even appear.
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    // A PANEL, NOT A HAND-ROLLED BOX -- the same `rounded border p-3` the
    // portal's resolver carried: a 4px radius off the ladder and a border in
    // `currentColor`, sitting beside real panels on the ticket page.
    <Panel title={t("serviceDesk.copilot.title")}>
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
        <div className="flex gap-2">
          <input
            aria-label={t("serviceDesk.copilot.askAria")}
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            className={`${inputClass} flex-1`}
            placeholder={t("serviceDesk.copilot.placeholder")}
          />
          {/* SECONDARY. This panel drafts and never posts (see the header), so
              nothing it does is the page's committing action. `Button` rather
              than `Btn` for the `disabled` an in-flight turn needs. */}
          <Button
            variant="outline"
            disabled={busy || question.trim() === ""}
            onClick={() => void ask()}
          >
            {t("serviceDesk.copilot.ask")}
          </Button>
        </div>

        {error !== null && (
          <p role="alert" className="notice red">
            {describeSubmitError(error)}
          </p>
        )}

        {turn !== null && (
          <>
            {/* A DRAFT, LABELLED AS ONE. Rendered as text so nothing the model
                wrote becomes markup in a technician's console; `.code` is the
                design's box for text somebody copies out. */}
            <pre className="code" style={{ font: "inherit" }}>{turn.answer}</pre>
            {turn.partial && (
              <p className="note">
                {t("serviceDesk.copilot.partial", { reason: stopReasonLabel(turn.stop_reason) })}
              </p>
            )}
            {turn.flagged.length > 0 && (
              <p className="notice gold">
                {t("serviceDesk.copilot.flagged", { count: turn.flagged.length })}
              </p>
            )}
            {turn.proposed.map((id) => (
              <p key={id}>
                <Trans
                  k="serviceDesk.copilot.proposed"
                  components={{ link: <Link className="underline" to={`/assistant/actions/${id}`} /> }}
                />
              </p>
            ))}
          </>
        )}
      </div>
    </Panel>
  );
}
