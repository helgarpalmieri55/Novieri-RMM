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

import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { Btn, Panel } from "../../shell/components";
import { useAbandonMailboxOAuthMutation, useCompleteMailboxOAuthMutation } from "./api";

type Phase = "working" | "done" | "cancelled" | "failed";

/**
 * Why the sign-in failed, held as the cause and worded at render so a language
 * switch re-words it: either this page was not reached from a sign-in the
 * console started, or the call failed and the error itself is kept.
 */
type Failure = { kind: "notStarted" } | { kind: "error"; error: unknown };

export function OAuthCallback() {
  const t = useT();
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
    <div style={{ maxWidth: 560, margin: "48px auto" }}>
      <Panel title={t("emailIntake.oauth.title")}>
        {phase === "working" ? <p className="note">{t("emailIntake.oauth.working")}</p> : null}
        {phase === "done" ? (
          <p role="status" className="form-saved">
            {address ? t("emailIntake.oauth.connectedAs", { address }) : t("emailIntake.oauth.connected")}
          </p>
        ) : null}
        {phase === "cancelled" ? (
          <p role="status">
            {t("emailIntake.oauth.cancelled")}
          </p>
        ) : null}
        {phase === "failed" ? (
          <p role="alert" className="form-error">
            {t("emailIntake.oauth.failed", {
              reason: failure?.kind === "error" ? describeSubmitError(failure.error) : t("emailIntake.oauth.notStarted"),
            })}
          </p>
        ) : null}
        {phase !== "working" ? (
          <div className="form-actions">
            <Btn label={t("emailIntake.oauth.back")} onClick={() => navigate("/email-intake")} />
          </div>
        ) : null}
      </Panel>
    </div>
  );
}
