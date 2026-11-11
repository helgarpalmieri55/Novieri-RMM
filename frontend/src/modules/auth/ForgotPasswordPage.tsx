/**
 * `/forgot-password` — ask for a link to choose a new password (own-auth
 * design §2.8).
 *
 * THE SAME ANSWER WHETHER OR NOT THE ADDRESS HAS AN ACCOUNT. The server
 * answers 202 either way (OWASP Forgot Password, [S8]); this page says
 * "if that address belongs to an account" and nothing more definite, so it
 * cannot be used to learn who has one. The link works once, for 30 minutes,
 * and the second factor is still asked for afterwards: a mailbox alone never
 * gets anyone past it (§2.8).
 */
import { useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Form } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard, AuthDone } from "./AuthCard";
import { describeAuthError } from "./errors";
import { carryNext, useNext } from "./flow";

export function ForgotPasswordPage() {
  const t = useT();
  const api = useApi();
  const next = useNext();
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  async function submit() {
    if (inFlight.current || !email.trim()) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await api.forgotPassword({ email: email.trim() });
      setSent(true);
    } catch (cause: unknown) {
      setError(describeAuthError(cause, { canTimeOut: false }));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <AuthCard title={t("auth.forgot.title")} intro={sent ? undefined : t("auth.forgot.intro")}>
      {sent ? (
        <AuthDone>{t("auth.forgot.sent")}</AuthDone>
      ) : (
        <Form onSubmit={() => void submit()}>
          <Field label={t("auth.common.email")}>
            <input
              className={inputClass}
              type="email"
              autoComplete="username"
              spellCheck={false}
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </Field>
          <AuthAlert text={error} />
          <div className="form-actions">
            <Btn
              type="submit"
              variant="primary"
              icon="mail"
              label={busy ? t("auth.common.working") : t("auth.forgot.submit")}
              disabled={busy || !email.trim()}
            />
          </div>
        </Form>
      )}
      <p className="note" style={{ margin: 0 }}>
        <Link to={carryNext("/sign-in", next)}>{t("auth.common.backToSignIn")}</Link>
      </p>
    </AuthCard>
  );
}
