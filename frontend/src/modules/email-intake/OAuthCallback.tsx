// Where Microsoft sends the browser back after "Connect with Microsoft".
//
// A CONSOLE ROUTE, NOT AN API ROUTE, and that is the security argument: a
// redirect arrives with no Authorization header, so the console lands here and
// makes an ordinary authenticated call with the operator's own token -- same
// permission check and same tenant scoping as every other write. `state` only
// guards against a forged or replayed return (mail-oauth design §4.3).
//
// The code is stripped from the address bar at once: it has no business in
// history, a bookmark or a screenshot.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { FormPage, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useRecordTitle } from "../../shell/title";
import { KIND_ICON, type Kind } from "../../shell/viz";
import { useAbandonMailboxOAuthMutation, useCompleteMailboxOAuthMutation } from "./api";

type Phase = "working" | "done" | "cancelled" | "failed";

/**
 * Why the sign-in failed, held as the cause and worded at render so a language
 * switch re-words it: either this page was not reached from a sign-in the
 * console started, or the call failed and the error itself is kept.
 */
type Failure = { kind: "notStarted" } | { kind: "error"; error: unknown };

/**
 * AN OUTCOME IS A NOTICE WITH ITS KIND'S ICON (final review I1, the critique's
 * P2), the way the auth screens and the Microsoft sign-in return say one: the
 * notice family's three tones, never the save bar's 12px line, and never
 * colour alone (The Second Carrier Rule).
 */
function Outcome({ kind, children }: { kind: Extract<Kind, "ok" | "info" | "crit">; children: ReactNode }) {
  return (
    <p role={kind === "crit" ? "alert" : "status"} className={kind === "crit" ? "notice red" : "notice"}>
      <Icon name={KIND_ICON[kind]} size={16} />
      <span>{children}</span>
    </p>
  );
}

export function OAuthCallback() {
  const t = useT();
  // A sub-page with no rail entry names itself in the tab, as its h1 does
  // (general ruling 1b): "Microsoft 365 sign-in · Email intake · Novieri RMM".
  useRecordTitle(t("emailIntake.oauth.title"));
  const complete = useCompleteMailboxOAuthMutation();
  const abandon = useAbandonMailboxOAuthMutation();
  const location = useLocation();
  const navigate = useNavigate();
  const [phase, setPhase] = useState<Phase>("working");
  const [failure, setFailure] = useState<Failure | null>(null);
  const [address, setAddress] = useState("");
  // The state is single use server-side, so a second call (StrictMode mounts
  // effects twice) would show a failure for a sign-in that worked.
  const fired = useRef(false);

  useEffect(() => {
    if (fired.current) return;
    fired.current = true;

    const query = new URLSearchParams(location.search);
    const state = query.get("state") ?? "";
    const code = query.get("code");
    const error = query.get("error");
    navigate(location.pathname, { replace: true });

    if (!state) {
      setPhase("failed");
      setFailure({ kind: "notStarted" });
      return;
    }
    // The promise, not per-call callbacks: StrictMode's simulated unmount
    // detaches a mutation's observer, and per-call callbacks would never fire.
    if (error || !code) {
      abandon
        .mutateAsync({ state })
        .catch(() => undefined)
        .finally(() => setPhase("cancelled"));
      return;
    }
    complete
      .mutateAsync({ state, code })
      .then((mailbox) => {
        setAddress(mailbox.address);
        setPhase("done");
      })
      .catch((err: unknown) => {
        setFailure({ kind: "error", error: err });
        setPhase("failed");
      });
    // Once, on mount: the query is read and erased in the same pass.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    // INSIDE THE CONSOLE'S FRAME (redesign plan, Task 7, 2.7): the page's own
    // head, with the way back to Email intake above it, and the same panel of
    // today under it. Microsoft sends the person back here mid-flow, so the
    // first thing on the page says where they are and how to return.
    <FormPage back={{ to: "/email-intake", label: t("shell.nav.item.emailIntake") }} title={t("emailIntake.oauth.title")}>
      <Panel>
        {phase === "working" ? <p className="note">{t("emailIntake.oauth.working")}</p> : null}
        {phase === "done" ? (
          <Outcome kind="ok">
            {address ? t("emailIntake.oauth.connectedAs", { address }) : t("emailIntake.oauth.connected")}
          </Outcome>
        ) : null}
        {phase === "cancelled" ? <Outcome kind="info">{t("emailIntake.oauth.cancelled")}</Outcome> : null}
        {phase === "failed" ? (
          <Outcome kind="crit">
            {t("emailIntake.oauth.failed", {
              reason: failure?.kind === "error" ? describeSubmitError(failure.error) : t("emailIntake.oauth.notStarted"),
            })}
          </Outcome>
        ) : null}
        {/* A ROUTE IS A LINK (the critique's P2): it opens in a new tab, copies
            and middle-clicks, and is announced as what it is. The button's
            skin, as BcpReport's way back wears it. */}
        {phase !== "working" ? (
          <div className="form-actions">
            <Link className="btn" to="/email-intake">
              {t("emailIntake.oauth.back")}
            </Link>
          </div>
        ) : null}
      </Panel>
    </FormPage>
  );
}
