/**
 * `/reset-password#<token>` — choose a new password from the emailed link
 * (own-auth design §2.8).
 *
 * The token is single-use and lives 30 minutes; the server keeps only its
 * hash. A link it no longer honours (410 `invalid_link`: expired, used, never
 * issued) reads as one state with one way on: ask for a new link. After a change the person
 * signs in, and the second factor is still asked for -- the mailbox alone
 * never gets anyone past it (§2.8). A refused password says which rule
 * refused it (`./NewPasswordFields.tsx`).
 *
 * THE TOKEN IS READ FROM THE FRAGMENT AND SENT IN THE BODY (`./flow.ts`):
 * it never reaches an access log, a `Referer` or a URL of this console's own
 * requests.
 */
import { useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Form } from "../../components/form";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard, AuthDone } from "./AuthCard";
import { checkNewPassword, describeAuthError, isDeadLink } from "./errors";
import { useLinkToken } from "./flow";
import { NewPasswordFields } from "./NewPasswordFields";

export function ResetPasswordPage() {
  const t = useT();
  const api = useApi();
  const token = useLinkToken();
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<"form" | "done" | "dead">("form");
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  async function submit() {
    if (inFlight.current) return;
    const problem = checkNewPassword(password, again);
    if (problem) {
      setError(problem);
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await api.resetPassword({ token, password });
      setOutcome("done");
    } catch (cause: unknown) {
      if (isDeadLink(cause)) setOutcome("dead");
      else setError(describeAuthError(cause, { canTimeOut: false }));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  if (outcome === "done") {
    return (
      <AuthCard title={t("auth.reset.title")}>
        <AuthDone>{t("auth.reset.done")}</AuthDone>
        <div className="form-actions">
          <Link className="btn primary" to="/sign-in">
            {t("auth.reset.signIn")}
          </Link>
        </div>
      </AuthCard>
    );
  }
  if (outcome === "dead") {
    return (
      <AuthCard title={t("auth.reset.title")}>
        <AuthAlert text={t("auth.reset.invalid")} />
        <p className="note" style={{ margin: 0 }}>
          <Link to="/forgot-password">{t("auth.reset.askAgain")}</Link>
        </p>
      </AuthCard>
    );
  }
  return (
    <AuthCard title={t("auth.reset.title")}>
      <Form onSubmit={() => void submit()}>
        {/* So a password manager knows a password is being set for an account;
            the reset link names none, so the field is empty and read-only. */}
        <input
          type="text"
          name="username"
          autoComplete="username"
          value=""
          readOnly
          tabIndex={-1}
          aria-hidden="true"
          style={{ position: "absolute", width: 1, height: 1, opacity: 0, pointerEvents: "none" }}
        />
        <NewPasswordFields password={password} again={again} onPassword={setPassword} onAgain={setAgain} />
        <AuthAlert text={error} />
        <div className="form-actions">
          <Btn
            type="submit"
            variant="primary"
            icon="lock"
            label={busy ? t("auth.common.working") : t("auth.reset.submit")}
            disabled={busy || !password || !again}
          />
        </div>
      </Form>
    </AuthCard>
  );
}
