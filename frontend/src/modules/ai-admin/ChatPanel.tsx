// The chat panel (spec §5, §10.6).
//
// **THE PANEL NEVER BLOCKS THE CONSOLE.** §10.6: every other page works with the
// AI feature entirely off. With `ai_settings.enabled = false`, or no provider
// selected, or no provider configured on this server, `GET /ai/status` answers
// `{enabled: false, reason: "..."}` and this renders a DISABLED PANEL WITH THE
// SERVER'S OWN EXPLANATION — not an error, not a spinner, and never a thrown
// render that takes the surrounding page down with it. "The panel is disabled"
// and "the console still works" are different failures and the tests assert both.
//
// THE REASON IS RENDERED VERBATIM AND IS NOT RE-DERIVED HERE. "Disabled for this
// organisation" and "no provider is configured on this server" are different
// problems with different owners, and a single "AI is unavailable" sends an
// administrator to the wrong person.
//
// THE `POST` IS AUTHORITATIVE AND THIS PANEL DOES NOT OPEN THE SSE STREAM (§6).
// The turn request returns the complete answer; the stream is an optional
// side-channel that carries token deltas and nothing the answer does not already
// have. A client that never opens it loses nothing.
//
// THE PAGE IS v3's `assistant` SCREEN: the head, then a two-column `.g2` with
// the conversation on the left (an `.empty` invitation until the first
// question, the transcript after it, and the ask row under either) and the
// recent sessions on the right as a bare panel of rows. Every state a mock
// never draws -- checking, disabled, a refused session, a refused turn --
// keeps its own announced rendering.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Form, describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { Table, Td, Th } from "../../components/ui";
import { RelTime } from "../../components/reltime";
import { personaLabel } from "../ai-automation/types";
import { Icon } from "../../shell/icons";
import { Btn, KIND_ICON, PageHead, Panel, QueryState } from "../../shell/components";
import {
  useAiPermissions,
  useAiSession,
  useAiSessions,
  useAiStatus,
  useCreateAiSession,
  useCreateAiTurn,
  useProfileZone,
} from "./api";
import { MessageList } from "./MessageList";
import type { AiFlagged, AiRenderedTurn, AiSessionOut, AiToolCall, AiTurnOut } from "./types";

/**
 * The server's sentence, capitalised and closed.
 *
 * NOT A REWRITE, and the distinction matters here: "the AI assistant is turned
 * off for this organization" and "no provider is configured on this server" are
 * different problems with different owners, so the WORDS stay the server's.
 * Only the first letter and the final stop are this console's business.
 */
export function sentence(text: string): string {
  const trimmed = text.trim();
  if (trimmed === "") return trimmed;
  const capitalised = trimmed.replace(/^./, (c) => c.toUpperCase());
  return /[.!?]$/.test(capitalised) ? capitalised : `${capitalised}.`;
}

interface StoredMessage {
  id: string;
  role: string;
  content: string;
  tool_calls: AiToolCall[] | null;
  flags: Record<string, unknown>;
}

/**
 * The stored transcript as rendered turns, with the live turn's flagged records
 * carried across.
 *
 * **THE CARRY-ACROSS IS THE POINT, NOT AN OPTIMIZATION.** Stored transcripts
 * hold `stop_reason` and `partial` in `flags` (§3.2) but NOT the flagged
 * records: those live in `ai_injection_flag`, served on the `ai.admin` route,
 * because attacker-controlled prose read across a whole org is an
 * administrative act. So once the transcript refetches after a turn, a naive
 * render would REPLACE the answer that named TCK-1043 with the same answer
 * saying nothing — §8.4 clause 3 satisfied for a second and then quietly
 * withdrawn. `ai_message.id` IS the turn id (`persist_turn` writes it), which is
 * what makes the match exact rather than positional.
 */
export function mergeTurns(
  messages: StoredMessage[],
  live: AiTurnOut | null,
): AiRenderedTurn[] {
  return messages.map((message) => ({
    id: message.id,
    role: message.role === "user" ? "user" : "assistant",
    content: message.content,
    toolCalls: message.tool_calls ?? [],
    flagged: live && live.turn_id === message.id ? (live.flagged as AiFlagged[]) : [],
    partial: message.flags?.partial === true,
    stopReason:
      typeof message.flags?.stop_reason === "string"
        ? (message.flags.stop_reason as string)
        : undefined,
  }));
}

function liveTurn(turn: AiTurnOut): AiRenderedTurn {
  return {
    id: turn.turn_id,
    role: "assistant",
    content: turn.answer,
    toolCalls: turn.tool_calls,
    flagged: turn.flagged as AiFlagged[],
    partial: turn.partial,
    stopReason: turn.stop_reason,
  };
}

export function ChatPanel() {
  const t = useT();
  const status = useAiStatus();
  const permissions = useAiPermissions();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [lastTurn, setLastTurn] = useState<AiTurnOut | null>(null);
  const createSession = useCreateAiSession();
  const transcript = useAiSession(sessionId);
  const createTurn = useCreateAiTurn(sessionId);

  /**
   * A QUESTION HANDED OVER FROM THE TOP BAR'S SEARCH, as `/assistant?q=...`.
   *
   * IT IS SENT, NOT PRE-FILLED. The operator typed the words and then chose
   * "Ask the Assistant" over the matching records — that click is the intent
   * to ask, and making them press Ask again is the same dead-end friction the
   * audit spent a whole programme removing.
   *
   * THE PARAMETER IS STRIPPED BEFORE THE REQUEST GOES OUT, and `replace`
   * rather than `push` so Back does not walk into a re-ask. A turn can cost
   * money and, on a provider configured for it, can move data off this
   * network; a question that re-fires on every refresh is the kind of bug
   * nobody notices until the bill or the audit log.
   *
   * IT DOES NOT FIRE WHEN THE ASSISTANT IS OFF. The disabled branch below
   * already says why; asking anyway would produce an error where an
   * explanation exists.
   */
  const [params, setParams] = useSearchParams();
  const handedOver = useRef(false);
  const handOff = params.get("q")?.trim() ?? "";
  useEffect(() => {
    if (!handOff || handedOver.current) return;
    if (!status.data?.enabled) return;
    handedOver.current = true;
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.delete("q");
        return next;
      },
      { replace: true },
    );
    void askQuestion(handOff);
  }, [handOff, status.data?.enabled, setParams]);

  // A FUNCTION, because only one of the three states this component renders
  // has a page-level action to offer. The head is otherwise identical in all
  // three and duplicating it to vary one prop is how the three drift apart.
  // The sentence says what the assistant reaches, and stops there. Where its
  // proposals go is said on the screen that confirms one, not here.
  const head = (actions?: ReactNode) => (
    <PageHead
      title={t("aiAdmin.chat.title")}
      description={t("aiAdmin.chat.description")}
      actions={actions}
    />
  );

  if (status.isPending) {
    return (
      <>
        {head()}
        <Panel>
          <p className="note">{t("aiAdmin.chat.checking")}</p>
        </Panel>
      </>
    );
  }

  // A failed status read is treated exactly like an unavailable assistant: the
  // panel explains itself and the console carries on. It is never an error
  // boundary and never a retry loop against a permission wall.
  const reason = status.data?.enabled
    ? ""
    : (status.data?.reason ?? t("aiAdmin.chat.unavailable"));

  // The settings link is shown to `ai.admin` holders in BOTH states. Since spec
  // §0.14 the disabled state has ONE cause -- this deployment has no model
  // provider configured -- and an org-admin cannot fix it; what the settings
  // page gives them is the sentence naming the `ITS_AI_*` variables to hand to
  // whoever runs the control plane, which is more than the disabled panel has
  // room for.
  const settingsLink = permissions.admin ? (
    <Link to="/assistant/settings" className="btn">
      <Icon name="settings" size={14} />
      <span>{t("aiAdmin.chat.settingsLink")}</span>
    </Link>
  ) : null;

  if (reason) {
    // AUDIT [MEDIUM], /assistant: "Disabled-feature message is lowercase,
    // unpunctuated, and offers no path forward for the operator ... Below it
    // sits a bare text link 'Assistant settings' with no button styling", and
    // separately: "H1: Assistant directly above H2: Assistant — which screen-
    // reader users hear twice in a row."
    //
    // THE SENTENCE ITSELF IS STILL THE SERVER'S, VERBATIM, and that matters
    // more rather than less now that there is only one of them: "no model
    // provider is configured on this server" is the operator's problem, and a
    // console that rewrote it into house style would eventually rewrite whose
    // problem it is. What changes is everything AROUND it: the duplicate
    // heading is gone, the action is a button rather than a bare link, and
    // somebody who cannot act is told who can instead of being shown nothing.
    return (
      <>
        {head()}
        <Panel>
          <div className="notice gold">
            <Icon name={KIND_ICON.warn} size={16} />
            {/* `sentence()` is TYPOGRAPHY, not a rewrite: it capitalises the
                first letter and closes the sentence. Which problem this is,
                and whose it is, stays the server's words. */}
            <p>{sentence(reason)}</p>
          </div>
          {settingsLink ? (
            <p style={{ marginTop: 12 }}>
              <Link to="/assistant/settings" className="btn">
                <Icon name="settings" size={14} />
                <span>{t("aiAdmin.chat.openSettings")}</span>
              </Link>
            </p>
          ) : (
            <p className="note" style={{ marginTop: 12 }}>
              {t("aiAdmin.chat.turnedOnBy")}
            </p>
          )}
        </Panel>
      </>
    );
  }

  const stored = mergeTurns(transcript.data?.messages ?? [], lastTurn);
  // The live turn is rendered only until the transcript that contains it
  // arrives, so an answer is never shown twice.
  const pending =
    lastTurn && !stored.some((entry) => entry.id === lastTurn.turn_id)
      ? [liveTurn(lastTurn)]
      : [];
  const turns = [...stored, ...pending];

  // WHICHEVER HALF FAILED, and `createSession` first because it fails FIRST:
  // opening the session is a separate request from asking the question, and it
  // used to have no rendering at all. A refused `POST /ai/sessions` left the
  // input holding the administrator's question, the button un-pressed-looking,
  // and nothing on screen — the swallowed-failure shape.
  const failure = createSession.error ?? createTurn.error ?? null;
  const busy = createTurn.isPending || createSession.isPending;

  async function ask(): Promise<void> {
    await askQuestion(question);
  }

  /**
   * The one path a question travels, whether it came from the box below or
   * from the top bar's search. Taking the text as an ARGUMENT rather than
   * reading state is what lets the hand-off skip a render — a `setQuestion`
   * followed by a read of `question` in the same tick reads the old value,
   * which would have sent an empty question and looked like the assistant
   * ignoring the operator.
   */
  async function askQuestion(raw: string): Promise<void> {
    const text = raw.trim();
    if (!text) return;
    setQuestion(text);
    let target = sessionId;
    try {
      if (target == null) {
        const created = await createSession.mutateAsync();
        target = created.id;
        setSessionId(target);
      }
      const answered = await createTurn.mutateAsync({ question: text });
      setLastTurn(answered);
      setQuestion("");
    } catch {
      // NOT A SWALLOW. Both mutations keep their own error, which `failure`
      // above renders as an announced alert; rethrowing from an async handler
      // invoked as `void ask()` would surface only as an unhandled rejection
      // in the console, which is where the user is not looking. The question
      // is deliberately left in the box so a retry costs no retyping.
    }
  }

  return (
    <>
      {head(settingsLink)}
      <div className="g2" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Panel>
          {/* AUDIT [MEDIUM]: "H1: Assistant directly above H2: Assistant —
              which screen-reader users hear twice in a row." The panel has no
              title: the `h1` already names it. Until the first question the
              body is the design's invitation, in `.empty`'s shape; after it,
              the transcript. */}
          {turns.length === 0 ? (
            <div className="empty">
              <span className="empty-icon" aria-hidden="true">
                <Icon name="sparkles" size={26} />
              </span>
              <b>{t("aiAdmin.chat.invitation")}</b>
              <p>{t("aiAdmin.chat.examples")}</p>
            </div>
          ) : (
            <MessageList turns={turns} />
          )}
          {/* A REAL FORM, so Enter asks. The button stays a submit so the
              click path and the keyboard path are one path. */}
          <Form className="tb" onSubmit={() => void ask()}>
            <label className="sr-only" htmlFor="ai-question">
              {t("aiAdmin.chat.askLabel")}
            </label>
            <input
              id="ai-question"
              type="text"
              className="search-field"
              placeholder={t("aiAdmin.chat.askPlaceholder")}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
            />
            <Btn
              label={busy ? t("aiAdmin.chat.asking") : t("aiAdmin.chat.ask")}
              icon="sparkles"
              variant="primary"
              type="submit"
              disabled={busy}
            />
          </Form>
          {failure ? (
            <p role="alert" className="notice red">
              <Icon name={KIND_ICON.crit} size={16} />
              <span>{describeSubmitError(failure)}</span>
            </p>
          ) : null}
        </Panel>
        <RecentSessionsPanel />
      </div>
    </>
  );
}

/**
 * The design's "Recent questions" panel, from `useAiSessions()` — a hook
 * that already existed in `api.ts` but had no page wired to it. Session
 * TITLE, PERSONA and MESSAGE COUNT are real fields on `AiSessionOut`; the
 * design's per-row "2 reads · 1 confirmed action" detail has no equivalent
 * field on this model (that breakdown lives in the AI automation module's
 * `ai_action` rows, not on the session), so it is omitted rather than
 * approximated. Drawn as v3's bare panel of rows: a lead glyph, the title,
 * the sub-line, and a mono "when" column.
 */
function RecentSessionsPanel() {
  const t = useT();
  const sessions = useAiSessions();
  // The signed-in user's profile zone (`/me`.timezone). A session's last turn
  // rendered in the browser's zone is the same row reading differently from two
  // machines the same technician works from.
  const zone = useProfileZone();
  const items: AiSessionOut[] = sessions.data ?? [];
  return (
    <Panel
      title={t("aiAdmin.chat.recentTitle")}
      subtitle={t("aiAdmin.chat.recentSubtitle")}
      bare
    >
      <QueryState
        loading={sessions.isLoading}
        error={sessions.error}
        empty={items.length === 0}
        emptyLabel={t("aiAdmin.chat.noSessions")}
        emptyIcon="sparkles"
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("aiAdmin.chat.name")}</Th>
              <Th>{t("aiAdmin.chat.when")}</Th>
            </tr>
          </thead>
          <tbody>
            {items.slice(0, 6).map((session) => (
              <tr key={session.id}>
                <Td lead={<Icon name="sparkles" size={18} />}>
                  <div className="cell-title">{session.title || personaLabel(session.persona)}</div>
                  <div className="cell-sub">
                    {t("aiAdmin.chat.sessionSub", { persona: personaLabel(session.persona), count: session.message_count })}
                  </div>
                </Td>
                <Td>
                  <RelTime iso={session.last_turn_at} zone={zone} />
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </Panel>
  );
}
