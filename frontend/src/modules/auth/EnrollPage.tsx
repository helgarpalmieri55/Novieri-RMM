/**
 * `/sign-in/enroll` — the first second factor (own-auth design §2.2): the
 * person does not reach the console until they have one.
 *
 * Reached from the sign-in step (`enroll_required`) and from an accepted
 * invitation, both of which leave a session that has a password and no
 * factor; every request here rides on it, and a 401 `auth_required` means it
 * has gone and only starting again helps.
 *
 * THE PASSKEY FIRST (§2.2). The options come from the server whole --
 * `userVerification: "required"`, no attestation, the person's existing
 * credentials excluded -- and the server verifies what comes back with user
 * verification required (own-auth Task 5, plan decision D22). This page adds
 * nothing to either: it only carries them.
 *
 * THE AUTHENTICATOR APP SHOWS ITS SETUP KEY, NOT A QR CODE. The secret arrives
 * once, as an `otpauth://` URI (own-auth Task 7); the page shows the key in
 * groups of four to type into the app, and the URI as a link, which a phone
 * opens straight in its authenticator. Drawing a QR code needs a QR encoder,
 * which this console does not carry (plan decision recorded in the plan).
 *
 * EITHER WAY, `enroll/finish` MAKES THE TEN RECOVERY CODES and the next page
 * shows them, handed over in memory (`./flow`), never in the URL or the
 * router's state.
 */
import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Form } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { useApi } from "../../lib/ApiContext";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard, AuthSection } from "./AuthCard";
import { describeAuthError } from "./errors";
import { carryNext, digitsOf, handOverCodes, setupKeyOf, useNext } from "./flow";
import { canUsePasskeys, createPasskey, describePasskeyError } from "./passkey";

export function EnrollPage() {
  const t = useT();
  const api = useApi();
  const navigate = useNavigate();
  const next = useNext();
  const passkeys = canUsePasskeys();
  const [name, setName] = useState(() => translate("auth.enroll.passkeyDefaultName"));
  const [otpauthUri, setOtpauthUri] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The factor is in; only the recovery codes are still owed. The server
  // refuses the factor steps again on a pending session, so a failed
  // `enroll/finish` is retried alone.
  const [factorAccepted, setFactorAccepted] = useState(false);
  const inFlight = useRef(false);

  async function run(step: () => Promise<void>, rejected: MessageKey) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await step();
    } catch (cause: unknown) {
      setCode("");
      setError(describePasskeyError(cause) ?? describeAuthError(cause, { rejected }));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  /** The factor is in: make the recovery codes and show them, once. */
  async function finished() {
    setFactorAccepted(true);
    const { recovery_codes } = await api.enrollFinish();
    handOverCodes(recovery_codes);
    navigate(carryNext("/sign-in/recovery-codes", next), { replace: true });
  }

  const setupKey = otpauthUri ? setupKeyOf(otpauthUri) : null;

  if (factorAccepted) {
    return (
      <AuthCard title={t("auth.enroll.title")} intro={t("auth.enroll.factorSaved")}>
        <AuthAlert text={error} />
        <div className="form-actions">
          <Btn
            variant="primary"
            label={busy ? t("auth.common.working") : t("auth.enroll.retryFinish")}
            disabled={busy}
            onClick={() => void run(finished, "auth.enroll.rejected")}
          />
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard title={t("auth.enroll.title")} intro={t("auth.enroll.intro")}>
      {passkeys ? (
        <AuthSection title={t("auth.enroll.passkeyTitle")} hint={t("auth.enroll.passkeyHint")}>
          <Form
            onSubmit={() =>
              void run(async () => {
                const options = await api.enrollPasskeyOptions();
                const credential = await createPasskey(options);
                await api.enrollPasskey(credential, name.trim());
                await finished();
              }, "auth.enroll.passkeyRejected")
            }
          >
            <Field label={t("auth.enroll.passkeyName")}>
              <input className={inputClass} value={name} maxLength={64} onChange={(event) => setName(event.target.value)} />
            </Field>
            <div className="form-actions">
              <Btn
                type="submit"
                variant="primary"
                icon="key"
                label={busy ? t("auth.common.working") : t("auth.enroll.createPasskey")}
                disabled={busy || !name.trim()}
              />
            </div>
          </Form>
        </AuthSection>
      ) : (
        <p className="note" style={{ margin: 0 }}>
          {t("auth.passkey.unsupported")}
        </p>
      )}

      <AuthSection title={t("auth.enroll.totpTitle")} hint={t("auth.enroll.totpHint")}>
        {otpauthUri === null ? (
          <div>
            <Btn
              icon="device-mobile"
              label={t("auth.enroll.startTotp")}
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  const { otpauth_uri } = await api.enrollTotpStart();
                  setOtpauthUri(otpauth_uri);
                }, "auth.enroll.rejected")
              }
            />
          </div>
        ) : (
          <Form
            onSubmit={() => {
              if (digitsOf(code)) {
                void run(async () => {
                  await api.enrollTotpConfirm(digitsOf(code));
                  await finished();
                }, "auth.enroll.rejected");
              }
            }}
          >
            <p className="note" style={{ margin: 0 }}>
              {t("auth.enroll.setupKey")}
            </p>
            {setupKey ? <code className="code">{setupKey}</code> : null}
            <p className="note" style={{ margin: 0 }}>
              <a href={otpauthUri}>{t("auth.enroll.openInApp")}</a>
            </p>
            <Field label={t("auth.enroll.totpLabel")}>
              <input
                className={inputClass}
                inputMode="numeric"
                autoComplete="one-time-code"
                spellCheck={false}
                value={code}
                onChange={(event) => setCode(event.target.value)}
              />
            </Field>
            <div className="form-actions">
              <Btn
                type="submit"
                variant="primary"
                label={busy ? t("auth.common.working") : t("auth.enroll.confirmTotp")}
                disabled={busy || !digitsOf(code)}
              />
            </div>
          </Form>
        )}
      </AuthSection>

      <AuthAlert text={error} />
      <p className="note" style={{ margin: 0 }}>
        <Link to={carryNext("/sign-in", next)}>{t("auth.common.startOver")}</Link>
      </p>
    </AuthCard>
  );
}
