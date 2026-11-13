/**
 * `/oauth/authorize` — where an AI app sends a person to approve it (own-auth
 * design §5.2; plan 2026-11-06, Task 4).
 *
 * A CONSOLE PAGE, NOT AN API ROUTE (plan D2). The session cookie is
 * `SameSite=Strict`, so a navigation that starts on the AI app's site does not
 * carry it to the API; a same-origin `fetch` from this page does. So the page
 * hands the request's query to `POST /api/v1/oauth/authorize/details` and
 * `/decision`, which validate it every time.
 *
 * WHAT THE PERSON IS SHOWN, from the server's answer: FIRST the host an app
 * cannot fake -- its metadata document's for a CIMD client, its redirect's for
 * one that registered (B3-10; MCP [S30]: "MUST clearly display the redirect
 * URI hostname"; Anthropic [S33]: "name the host of the client_id URL as the
 * app asking for access") -- then the name it gave itself, as its claim; where
 * the answer goes; a warning when it only answers on this computer [S30]; what
 * approving hands over, how long it lasts and where it ends; and the account.
 *
 * ONLY AN APPROVAL LEAVES ON ITS OWN (B3-15; RFC 9700 §4.11.2). An error the
 * app should hear, a decline and "you cannot connect apps here" show the host
 * and wait for "Back to <host>": with open registration no redirect is one the
 * platform trusts. The rule holds here even if the server breaks it: a
 * redirect answering a decline is not followed (fix round 1).
 *
 * APPROVING ASKS FOR THE SECOND FACTOR when the server wants it (operator
 * decision C1): the decision goes through `stepUp.run`, which answers 403
 * `step_up_required` with the phase-1 dialog and sends it once more.
 *
 * THE APPROVAL'S REDIRECT CARRIES THE AUTHORIZATION CODE. The decision is a
 * `useSecretMutation` (`gcTime: 0`, reset once it settles), the redirect is
 * read once, in the handler, and the document leaves for it at once.
 *
 * NEVER INSIDE ANOTHER PAGE (B3-16): nginx sends `frame-ancestors 'none'`, and
 * the page draws nothing when it is framed anyway.
 *
 * A FAILURE THAT PASSES READS AS ONE (B3-33, A3-24): a details request that
 * got no answer, a 5xx or a 429 (the api restarting, the network, the limit),
 * and a `temporarily_unavailable` from the server at either step, say "cannot
 * be checked right now" with "Try again", which asks the server again; only a
 * refusal of the request itself reads as for good.
 *
 * NOTHING HERE DECIDES ANYTHING (invariant 6): a request the page shows is a
 * request the server accepted a moment ago, and the server accepts it again,
 * or not, at the decision.
 */
import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useLocation } from "react-router-dom";

import { describeSubmitError } from "../../components/form";
import { StepUpCancelled, useStepUp } from "../../components/stepUp";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useSecretMutation } from "../../lib/secretMutation";
import { navigation } from "../../lib/session";
import { Btn, KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { AuthAlert, AuthCard } from "./AuthCard";
import { authorizationQuery, isFramed } from "./consent";
import type { ConsentAnswer } from "./types";

const LIST = { margin: 0, paddingLeft: 18, listStyle: "disc outside" } as const;
const BACK_TITLE = {
  error: "auth.consent.errorTitle",
  declined: "auth.consent.declinedTitle",
  denied: "auth.consent.deniedTitle",
} as const;

/** No answer, a 5xx or a 429: worth asking again (A3-24). */
function passes(cause: unknown): boolean {
  return !(cause instanceof ApiError) || cause.status >= 500 || cause.status === 429;
}

export function ConsentPage() {
  const t = useT();
  if (isFramed()) {
    return (
      <AuthCard title={t("auth.consent.framedTitle")}>
        <p className="note" style={{ margin: 0 }}>
          {t("auth.consent.framed")}
        </p>
      </AuthCard>
    );
  }
  return <ConsentRequest />;
}

function ConsentRequest() {
  const t = useT();
  const api = useApi();
  const stepUp = useStepUp();
  const { search } = useLocation();
  // READ ONCE (fix round 1): the card's details and the decision always
  // describe the same request, even if the address changes while the page
  // stays mounted.
  const [query] = useState(() => authorizationQuery(search));
  // ASKED ONCE, and the query is not part of the key: it is the request's,
  // not a cache entry's, and it leaves with the page.
  const details = useQuery({
    queryKey: ["oauth", "consent"],
    queryFn: () => api.oauthAuthorizeDetails(query),
    retry: false,
    staleTime: "static",
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
  const decision = useSecretMutation({
    mutationFn: (approve: boolean) => stepUp.run(() => api.oauthAuthorizeDecision(query, approve)),
  });
  // The answer on its way, if any: one at a time, and "Working…" on the
  // button that sent it (fix round 1).
  const [sending, setSending] = useState<"approve" | "decline" | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [answered, setAnswered] = useState<ConsentAnswer | null>(null);
  // A decline the server answered with a redirect, which is not followed.
  const [unfollowed, setUnfollowed] = useState(false);
  const inFlight = useRef(false);

  async function answer(approve: boolean) {
    if (inFlight.current) return;
    inFlight.current = true;
    setSending(approve ? "approve" : "decline");
    setFailure(null);
    try {
      const outcome = await decision.mutateAsync(approve);
      if (outcome.outcome !== "redirect") {
        setAnswered(outcome);
      } else if (approve) {
        // THE ONE NAVIGATION THE PAGE MAKES ON ITS OWN: the approval's code.
        // The buttons stay disabled until the document has left.
        navigation.assign(outcome.redirect_to);
        return;
      } else {
        // A DECLINE NEVER LEAVES ON ITS OWN (B3-15, fix round 1), whatever
        // the server answered. Not followed, and not kept: it may carry a code.
        setUnfollowed(true);
      }
    } catch (cause: unknown) {
      // A closed confirmation approved nothing, and says nothing.
      if (!(cause instanceof StepUpCancelled)) setFailure(describeSubmitError(cause));
    }
    inFlight.current = false;
    setSending(null);
  }

  if (details.isPending || details.isFetching) {
    return (
      <AuthCard title={t("auth.consent.loading")}>
        <p className="note" style={{ margin: 0 }}>
          {t("auth.common.working")}
        </p>
      </AuthCard>
    );
  }
  // Asking again starts from the question: an answer the decision gave is
  // dropped, and the server validates the request once more.
  function askAgain() {
    setAnswered(null);
    setFailure(null);
    void details.refetch();
  }
  if (details.isError) {
    const passing = passes(details.error);
    return (
      <AuthCard title={t(passing ? "auth.consent.unavailableTitle" : "auth.consent.refusedTitle")}>
        <AuthAlert text={describeSubmitError(details.error)} />
        {passing ? (
          <div className="form-actions">
            <Btn label={t("auth.consent.retry")} onClick={askAgain} />
          </div>
        ) : null}
      </AuthCard>
    );
  }
  if (unfollowed) {
    return (
      <AuthCard title={t("auth.consent.refusedTitle")}>
        <AuthAlert text={t("auth.consent.declineUnfollowed")} />
      </AuthCard>
    );
  }
  const shown = answered ?? details.data;
  if (shown.outcome === "refused") {
    const passing = shown.error === "temporarily_unavailable";
    return (
      <AuthCard title={t(passing ? "auth.consent.unavailableTitle" : "auth.consent.refusedTitle")}>
        <AuthAlert text={shown.detail} />
        {passing ? (
          <div className="form-actions">
            <Btn label={t("auth.consent.retry")} onClick={askAgain} />
          </div>
        ) : null}
      </AuthCard>
    );
  }
  if (shown.outcome === "back") {
    return (
      <AuthCard title={t(BACK_TITLE[shown.reason])}>
        <AuthAlert text={shown.detail} />
        <div className="form-actions">
          <Btn
            label={t("auth.consent.backTo", { host: shown.redirect_host })}
            onClick={() => navigation.assign(shown.redirect_to)}
          />
        </div>
      </AuthCard>
    );
  }
  if (shown.outcome !== "consent") return null;
  const { client, account } = shown;
  const host = client.kind === "metadata_document" ? (client.client_host ?? client.redirect_host) : client.redirect_host;
  return (
    <AuthCard title={t("auth.consent.title", { host })} intro={t("auth.consent.intro")}>
      <p className="note" style={{ margin: 0 }}>
        <span>{client.name ? t("auth.consent.calledItself", { name: client.name }) : t("auth.consent.unnamed")}</span>
        {client.kind === "registered" ? (
          <>
            {" "}
            <span>{t("auth.consent.selfRegistered")}</span>
          </>
        ) : null}
      </p>
      <p className="note" style={{ margin: 0 }}>
        {t("auth.consent.grants")}
      </p>
      <p className="note" style={{ margin: 0 }}>
        {t("auth.consent.lasts")}
      </p>
      {client.loopback_only ? (
        <p role="note" className="notice gold" style={{ margin: 0 }}>
          <Icon name={KIND_ICON.warn} size={16} />
          <span>{t("auth.consent.loopbackOnly")}</span>
        </p>
      ) : null}
      <ul className="note" style={LIST}>
        {client.client_host ? <li>{t("auth.consent.publishedBy", { host: client.client_host })}</li> : null}
        <li>{t("auth.consent.returnsTo", { host: client.redirect_host })}</li>
        <li className="mono" style={{ overflowWrap: "anywhere" }}>
          {t("auth.consent.appId", { id: client.client_id })}
        </li>
        <li>{t("auth.consent.account", { email: account })}</li>
      </ul>
      <AuthAlert text={failure} />
      <div className="form-actions">
        <Btn
          variant="primary"
          label={sending === "approve" && !stepUp.open ? t("auth.common.working") : t("auth.consent.approve")}
          disabled={sending !== null}
          onClick={() => void answer(true)}
        />
        <Btn
          label={sending === "decline" && !stepUp.open ? t("auth.common.working") : t("auth.consent.deny")}
          disabled={sending !== null}
          onClick={() => void answer(false)}
        />
      </div>
      {stepUp.dialog}
    </AuthCard>
  );
}
