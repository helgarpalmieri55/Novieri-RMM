// The tier-1 resolver, IN-TICKET ONLY (docs/modules/ai-automation.md §3.1,
// §11.3).
//
// **PLACED IN A TICKET AND NOWHERE ELSE, DELIBERATELY.** §11.3: the portal has
// no "search before you file" step, so the resolver's highest-value placement —
// pre-filing deflection — has nowhere to stand. This build does not INVENT one;
// a deflection surface is a portal-module change with its own decisions about
// what a requester sees before they have raised anything.
//
// **THERE IS NO ACTION UI HERE AND THERE CANNOT BE.** §0.9 gives this persona no
// act at any dial position: the surface registers no action route, the profile
// carries no act tool, and a database CHECK makes an `end_user` ceiling above
// `off` unstorable. This component has no confirm button because there is
// nothing on the server for one to call.

import { useState, type ReactElement } from "react";
import { describeSubmitError } from "../../components/form";
import { Button, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { Panel } from "../../shell/components";
import type { AiResolverTurn } from "../ai-automation/personaTypes";

export function PortalAssistantPanel(): ReactElement {
  const t = useT();
  const api = useApi();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<AiResolverTurn | null>(null);
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
        const created = await api.createResolverSession();
        id = created.id;
        setSessionId(id);
      }
      setAnswer(await api.createResolverTurn(id, question));
    } catch (caught) {
      // Cycle 4: `caught.message` is "" for a bodyless ApiError (a proxy 503),
      // which rendered as nothing at all -- the box did not even appear.
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    // A PANEL, NOT A HAND-ROLLED BOX. This was `rounded border p-3`, which in
    // Tailwind v4 means a 4px radius the ladder does not contain and a border
    // painted in `currentColor` -- a near-black edge beside every `.panel`'s
    // hairline, on the one surface a requester sees.
    <Panel title={t("portal.assistant.title")}>
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
        <p className="note">
          {t("portal.assistant.intro")}
        </p>
        <div className="flex gap-2">
          <input
            aria-label={t("portal.assistant.askAria")}
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            className={`${inputClass} flex-1`}
          />
          {/* SECONDARY, not the magenta primary: asking a question changes
              nothing on any endpoint, and DESIGN.md holds `--action` for the
              control that commits. `Button` rather than `Btn` because the
              control has to go disabled while a turn is in flight and `Btn`
              carries no `disabled`. */}
          <Button
            variant="outline"
            disabled={busy || question.trim() === ""}
            onClick={() => void ask()}
          >
            {t("portal.assistant.ask")}
          </Button>
        </div>
        {error !== null && (
          <p role="alert" className="notice red">
            {describeSubmitError(error)}
          </p>
        )}
        {answer !== null && (
          // TEXT, never markup: this string is downstream of ticket content that
          // anyone who can email the help address may have written. `.code` is
          // the design's box for text somebody reads and copies out.
          <pre className="code" style={{ font: "inherit" }}>{answer.answer}</pre>
        )}
      </div>
    </Panel>
  );
}
