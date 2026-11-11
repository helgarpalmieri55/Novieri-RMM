/**
 * `/sign-in` — the first step: email address and password (own-auth design
 * §2.1, §2.3).
 *
 * WHAT THE SERVER ANSWERS DECIDES THE NEXT PAGE, AND NOTHING ELSE DOES:
 * `factor_required` -> `/sign-in/factor` (with the methods and the
 * remember-this-computer length, which are not secrets, in the router's
 * state); `enroll_required` -> `/sign-in/enroll`; `signed_in` -> the console,
 * which happens only on a computer the person chose to remember (§2.6). A
 * wrong password, an unknown address and a locked account read the same here,
 * as they do on the server (§2.3).
 *
 * A PAGE FOR PEOPLE WITHOUT A SESSION, AND IT DOES NOT CHECK FOR ONE: a person
 * who is signed in and opens this page signs in again, and the session they
 * had is replaced. Bouncing them to the console would need a `/me` here, and
 * the harmony gate, whose fixture session is always signed in, could then
 * never see this page.
 */
import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Form } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard } from "./AuthCard";
import { describeAuthError } from "./errors";
import { afterLogin, carryNext, useNext } from "./flow";

export function SignInPage() {
  const t = useT();
  const api = useApi();
  const navigate = useNavigate();
  const next = useNext();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A ref, not the state above: two submits in one frame both read `busy` as
  // false (the same guard as the first-boot screen's).
  const inFlight = useRef(false);

  async function submit() {
    if (inFlight.current || !email.trim() || !password) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await api.login({ email: email.trim(), password });
      afterLogin(result, next, navigate);
    } catch (cause: unknown) {
      setPassword("");
      setError(describeAuthError(cause, { rejected: "auth.signIn.rejected", canTimeOut: false }));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <AuthCard title={t("auth.signIn.title")} intro={t("auth.signIn.intro")}>
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
        <Field label={t("auth.common.password")}>
          <input
            className={inputClass}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
        <AuthAlert text={error} />
        <div className="form-actions">
          <Btn
            type="submit"
            variant="primary"
            icon="lock"
            label={busy ? t("auth.common.working") : t("auth.signIn.submit")}
            disabled={busy || !email.trim() || !password}
          />
        </div>
      </Form>
      <p className="note" style={{ margin: 0 }}>
        <Link to={carryNext("/forgot-password", next)}>{t("auth.signIn.forgot")}</Link>
      </p>
    </AuthCard>
  );
}
