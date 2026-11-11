/**
 * `/portal/sign-in` — a client's contact signs in to the portal (own-auth
 * design §3; plan D25, D28, Task 10).
 *
 * THE SAME `POST /auth/login` AS THE CONSOLE'S PAGE (D25): the server finds
 * the person and applies their kind's rules. A contact without a second
 * factor is signed in at once; with one, the factor step follows, carrying
 * the portal as `next`. A technician who lands here signs in too, and the
 * console's guard puts them where they belong.
 *
 * A PASSKEY ALONE (D28): no address is asked for and none is sent; the
 * browser offers the person's own passkeys. Not offered where the browser
 * cannot use one. A refusal -- any refusal -- is one sentence, as on the
 * console's page (§2.3).
 */
import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Form } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { enterConsole } from "../../lib/session";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard } from "./AuthCard";
import { describeAuthError } from "./errors";
import { carryNext, useNext } from "./flow";
import { canUsePasskeys, describePasskeyError, signWithPasskey } from "./passkey";

const PORTAL = "/portal";

export function PortalSignInPage() {
  const t = useT();
  const api = useApi();
  const navigate = useNavigate();
  const next = useNext() ?? PORTAL;
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  async function guarded(step: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await step();
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function submit() {
    if (!email.trim() || !password) return;
    void guarded(async () => {
      try {
        const result = await api.login({ email: email.trim(), password });
        if (result.state === "signed_in") {
          enterConsole(next);
          return;
        }
        if (result.state === "enroll_required") {
          navigate(carryNext("/sign-in/enroll", next));
          return;
        }
        navigate(carryNext("/sign-in/factor", next), {
          state: { methods: result.methods, rememberDays: result.remember_device_days ?? null },
        });
      } catch (cause: unknown) {
        setPassword("");
        setError(describeAuthError(cause, { rejected: "auth.signIn.rejected", canTimeOut: false }));
      }
    });
  }

  function usePasskey() {
    void guarded(async () => {
      try {
        const credential = await signWithPasskey(await api.passkeySignInOptions());
        await api.passkeySignIn(credential);
        enterConsole(next);
      } catch (cause: unknown) {
        setError(
          describePasskeyError(cause) ??
            describeAuthError(cause, { rejected: "auth.portalSignIn.passkeyRejected", canTimeOut: false }),
        );
      }
    });
  }

  return (
    <AuthCard title={t("auth.portalSignIn.title")} intro={t("auth.portalSignIn.intro")}>
      {canUsePasskeys() ? (
        <div className="form-actions">
          <Btn
            variant="primary"
            icon="key"
            label={t("auth.portalSignIn.usePasskey")}
            disabled={busy}
            onClick={usePasskey}
          />
        </div>
      ) : null}
      <Form onSubmit={submit}>
        <Field label={t("auth.common.email")}>
          <input
            className={inputClass}
            type="email"
            autoComplete="username webauthn"
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
            variant="secondary"
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
