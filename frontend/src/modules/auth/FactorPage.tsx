/**
 * `/sign-in/factor` — the second factor, after the password (own-auth design
 * §2.2, §2.3, §2.6).
 *
 * THE PASSKEY IS OFFERED FIRST (§2.2): it is the factor a phishing page cannot
 * collect, because the browser binds it to this origin. Then the
 * authenticator app; then, folded away, a recovery code.
 *
 * THE METHODS COME FROM THE SIGN-IN STEP, in the router's state. On a page
 * opened directly -- a reload, a bookmark, the harmony gate -- there are
 * none, and all three are offered. A method the account lacks is refused by
 * the server with 409 `method_unavailable`, whose `detail` sentence reaches the
 * page through `describeSubmitError`; the password was already checked, so
 * that reveals nothing the password step had not already established.
 *
 * "DO NOT ASK AGAIN ON THIS COMPUTER" (§2.6) is offered only when the
 * sign-in step said how long the organisation allows (`rememberDays`); absent
 * or `null`, the organisation switched it off or the page cannot know, and no
 * box is drawn. It is unticked every time: a shared computer is the case it
 * must not be ticked on, and the hint says so. The server decides what a
 * remembered computer may skip; the box only asks.
 *
 * EVERY REFUSAL READS THE SAME: "not accepted", whatever the reason (§2.3).
 * Only a pre-factor session that has gone (`auth_required`) and a wait (429)
 * are told apart, because a person can act on those.
 *
 * THE RECOVERY CODE IS A TEXT LINK, NOT A BUTTON (redesign design §4, plan
 * Task 13, 8.2): the quieter way through, and a view of this page with its
 * own address (`?use=recovery`). The link carries `next` and the router's
 * state with it, so the factors offered and the remember box do not change
 * under the person. "Do not ask again on this computer" stays a checkbox: it
 * is a choice sent with the factor (`remember_device`), which a link cannot
 * carry.
 */
import { useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { Form } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { enterConsole, signInPath } from "../../lib/session";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard, AuthSection } from "./AuthCard";
import { describeAuthError } from "./errors";
import { digitsOf, recoveryCodeOf, useNext } from "./flow";
import { canUsePasskeys, describePasskeyError, passkeyHiddenAdvice, signWithPasskey } from "./passkey";
import type { FactorMethod, SignedIn } from "./types";

const EVERY_METHOD: readonly FactorMethod[] = ["passkey", "totp", "recovery"];

/** `?use=recovery`: the view of this page with the recovery code open. */
const USE = "use";
const RECOVERY_VIEW = "recovery";

/** This page's address with the recovery code open, keeping the rest of its query (`next`). */
function recoveryView(search: string): string {
  const params = new URLSearchParams(search);
  params.set(USE, RECOVERY_VIEW);
  return `?${params.toString()}`;
}

/** What the sign-in step hands this page, read defensively: router state is whatever the history holds. */
function handedOver(state: unknown): { methods: readonly FactorMethod[]; rememberDays: number | null } {
  const given = (state ?? {}) as { methods?: unknown; rememberDays?: unknown };
  const methods = Array.isArray(given.methods)
    ? EVERY_METHOD.filter((method) => (given.methods as unknown[]).includes(method))
    : [];
  const days = given.rememberDays;
  return {
    methods: methods.length > 0 ? methods : EVERY_METHOD,
    rememberDays: typeof days === "number" && Number.isInteger(days) && days > 0 ? days : null,
  };
}

export function FactorPage() {
  const t = useT();
  const api = useApi();
  const location = useLocation();
  const next = useNext();
  const { methods, rememberDays } = handedOver(location.state);
  const passkey = methods.includes("passkey") && canUsePasskeys();
  const totp = methods.includes("totp");
  const recovery = methods.includes("recovery");
  // THE ACCOUNT HAS A PASSKEY THIS BROWSER CANNOT USE (final review D1, Minor
  // 6): the option is not drawn, and the page says why and what is left --
  // the app, a recovery code, or, with neither, another browser or an
  // administrator. "Use an authenticator app" was said to accounts that had
  // none.
  const passkeyHidden = methods.includes("passkey") && !passkey;
  const hiddenAdvice = passkeyHidden ? (passkeyHiddenAdvice({ totp, recovery }) ?? "auth.passkey.hiddenNoOther") : null;
  // Asked for through the link, or the only way left.
  const askedRecovery = new URLSearchParams(location.search).get(USE) === RECOVERY_VIEW;
  const showRecovery = askedRecovery || (!passkey && !totp);
  const [remember, setRemember] = useState(false);
  const [code, setCode] = useState("");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  async function finish(step: () => Promise<SignedIn>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await step();
      enterConsole(next);
    } catch (cause: unknown) {
      setCode("");
      setRecoveryCode("");
      setError(describePasskeyError(cause) ?? describeAuthError(cause, { rejected: "auth.factor.rejected" }));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <AuthCard title={t("auth.factor.title")} intro={t("auth.factor.intro")}>
      {rememberDays !== null ? (
        <div>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} />
            {t("auth.factor.remember", { count: rememberDays })}
          </label>
          <p className="note" style={{ margin: "4px 0 0" }}>
            {t("auth.factor.rememberHint")}
          </p>
        </div>
      ) : null}

      {hiddenAdvice ? (
        <p className="note" style={{ margin: 0 }}>
          {t(hiddenAdvice)}
        </p>
      ) : null}

      {passkey ? (
        <AuthSection title={t("auth.factor.passkeyTitle")} hint={t("auth.factor.passkeyHint")}>
          <div>
            <Btn
              wide
              variant="primary"
              icon="key"
              label={busy ? t("auth.common.working") : t("auth.factor.usePasskey")}
              disabled={busy}
              onClick={() =>
                void finish(async () => {
                  const options = await api.factorPasskeyOptions();
                  const credential = await signWithPasskey(options);
                  return api.factorPasskey({ credential, remember_device: remember });
                })
              }
            />
          </div>
        </AuthSection>
      ) : null}

      {totp ? (
        <AuthSection title={t("auth.factor.totpTitle")}>
          <Form
            onSubmit={() => {
              if (digitsOf(code)) void finish(() => api.factorTotp({ code: digitsOf(code), remember_device: remember }));
            }}
          >
            <Field label={t("auth.factor.totpLabel")}>
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
                wide
                type="submit"
                variant={passkey ? "secondary" : "primary"}
                label={t("auth.factor.totpSubmit")}
                disabled={busy || !digitsOf(code)}
              />
            </div>
          </Form>
        </AuthSection>
      ) : null}

      {recovery && !showRecovery ? (
        <p className="note" style={{ margin: 0 }}>
          <Link to={{ search: recoveryView(location.search) }} state={location.state}>
            {t("auth.factor.showRecovery")}
          </Link>
        </p>
      ) : null}
      {recovery && showRecovery ? (
        <AuthSection title={t("auth.factor.recoveryTitle")} hint={t("auth.factor.recoveryHint")}>
          <Form
            onSubmit={() => {
              if (recoveryCode.trim()) {
                void finish(() =>
                  api.factorRecovery({ code: recoveryCodeOf(recoveryCode), remember_device: remember }),
                );
              }
            }}
          >
            <Field label={t("auth.factor.recoveryLabel")}>
              {/* Focused when the person opened it with the link: the link is
                  gone from the page, and focus would otherwise fall to the top. */}
              <input
                className={inputClass}
                autoFocus={askedRecovery}
                autoComplete="off"
                autoCapitalize="characters"
                spellCheck={false}
                value={recoveryCode}
                onChange={(event) => setRecoveryCode(event.target.value)}
              />
            </Field>
            <div className="form-actions">
              <Btn
                wide
                type="submit"
                label={t("auth.factor.recoverySubmit")}
                disabled={busy || !recoveryCode.trim()}
              />
            </div>
          </Form>
        </AuthSection>
      ) : null}

      <AuthAlert text={error} />
      {/* Back to the sign-in page the person came from: the portal's for a
          contact, whose `next` is a portal page (final review F, item 5). */}
      <p className="note" style={{ margin: 0 }}>
        <Link to={signInPath(next ?? undefined)}>{t("auth.common.startOver")}</Link>
      </p>
    </AuthCard>
  );
}
