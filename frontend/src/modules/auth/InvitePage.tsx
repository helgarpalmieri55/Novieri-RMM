/**
 * `/invite#<token>` — accept an invitation: choose a password, then the first
 * second factor (own-auth design §7).
 *
 * The invitation is single-use and lives 72 hours; the server keeps only its
 * hash. Accepting it leaves a session with a password and no factor, so the
 * next page is `/sign-in/enroll` and the console opens only after a factor and
 * the recovery codes (§2.2). An invitation the server no longer honours (410
 * `invalid_link`: expired, used, cancelled) reads as one state, and says who
 * can fix it. The token comes from the fragment and travels in bodies only
 * (`./flow.ts`).
 *
 * A CLIENT'S CONTACT (own-auth phase 2, Task 11, decision 2): the invitation
 * says `kind` and the shortest password allowed (15). A contact chooses a
 * password or a passkey and is signed in at once: there is no second factor
 * to add, and the next page is the portal.
 *
 * SAMESITE=STRICT IS WHY THIS IS A PAGE AND NOT A SERVER LINK (plan decision
 * D19). The email link is a cross-site top-level navigation, which carries no
 * Strict cookie; this page's own requests are same-origin and do.
 *
 * ACCEPTING WITH MICROSOFT (own-auth phase 2, Task 12, decision 5) is offered
 * only when the invitation says it can be (`microsoft`). A contact accepts
 * with Microsoft alone. A technician never does (operator decision C3): the
 * button sits under the password form and takes that password with it, which
 * the server stores before Microsoft is asked -- their way back if Microsoft
 * ever stops working. The password stays in this page's state: the button
 * calls the API directly, as the password's own accept does, and no
 * mutation cache holds it.
 */
import { useQuery } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Form } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { useApi } from "../../lib/ApiContext";
import { enterConsole } from "../../lib/session";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard } from "./AuthCard";
import { checkNewPassword, describeAuthError, isDeadLink } from "./errors";
import { useLinkToken } from "./flow";
import { describeMicrosoftError } from "./microsoft";
import { LEFT, MicrosoftButton, useBackFromMicrosoft, type MicrosoftGuard } from "./MicrosoftButton";
import { NewPasswordFields } from "./NewPasswordFields";
import { canUsePasskeys, createPasskey, describePasskeyError } from "./passkey";

export function InvitePage() {
  const t = useT();
  const api = useApi();
  const navigate = useNavigate();
  const token = useLinkToken();
  // Asked even with no token: the server answers an empty one as it answers
  // any unknown link (410), and fixture mode -- which cannot add a fragment --
  // still draws the form for the harmony gate.
  //
  // ASKED ONCE, AND THE TOKEN IS NOT KEPT (final review D1, Minor 4). Every
  // ask spends the per-IP limit the accept step shares (Task 7, `_LINK`), so
  // `staleTime: "static"`: TanStack skips a static query when anything
  // invalidates it, and `LanguageBridge` invalidates every query but `me` on
  // a language switch -- `Infinity` did not stop that, and a refused refetch
  // replaced the form with an error. The token lives in the `queryFn` only,
  // never in the key (it was `["auth", "invite", token]`), and `gcTime: 0`
  // drops the answer as soon as the page unmounts.
  const invite = useQuery({
    queryKey: ["auth", "invite"],
    queryFn: () => api.getInvite(token),
    retry: false,
    staleTime: "static",
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [dead, setDead] = useState(false);
  const [busy, setBusy] = useState(false);
  const [passkeyName, setPasskeyName] = useState(() => translate("auth.enroll.passkeyDefaultName"));
  // ONE GUARD FOR EVERY WAY IN (fix round 1): the password, the passkey and
  // Microsoft. One action is in flight at a time, so a technician's password
  // cannot be accepted while the same password is on its way to Microsoft's
  // start, and the other way round.
  const inFlight = useRef(false);
  const reset = useCallback(() => {
    inFlight.current = false;
    setBusy(false);
  }, []);
  // A TECHNICIAN'S MICROSOFT STEP SPENDS THE INVITATION BEFORE IT LEAVES
  // (final review F, item 4). The server accepts the invitation with their
  // password, opens a session waiting for the first factor, and only then
  // answers Microsoft's address (C3, `entra_flows.start`). Back at Microsoft
  // restores this page from the browser's cache: turned back on, any click
  // would answer 410 and say the invitation expired -- of an account that is
  // active, with the password they chose, which nobody can invite again. So
  // a restore after that step goes on to the first factor, where the waiting
  // session continues (and, should it have run out, `/sign-in/enroll` says
  // so and offers the sign-in page, where the password works). A contact's
  // step spends nothing (`entra_flows.complete` consumes it), so their page
  // simply comes back on. Page state only: a ref, nothing in storage.
  const technicianLeft = useRef(false);
  const backFromMicrosoft = useCallback(() => {
    if (technicianLeft.current) navigate("/sign-in/enroll", { replace: true });
    else reset();
  }, [navigate, reset]);
  useBackFromMicrosoft(backFromMicrosoft);

  function landed(kind: "staff" | "contact", state: "enroll_required" | "signed_in") {
    if (state === "signed_in") enterConsole(kind === "contact" ? "/portal" : null);
    else navigate("/sign-in/enroll", { replace: true });
  }

  /**
   * Run one way in. A dead invitation (410), from any of them, is the page's
   * own state; any other refusal is one sentence. A step that has `LEFT` for
   * Microsoft keeps the page off: the document is unloading.
   */
  async function guarded(step: () => Promise<unknown>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    let left = false;
    try {
      left = (await step()) === LEFT;
    } catch (cause: unknown) {
      if (isDeadLink(cause)) setDead(true);
      else
        setError(
          describeMicrosoftError(cause) ?? describePasskeyError(cause) ?? describeAuthError(cause, { canTimeOut: false }),
        );
    } finally {
      if (!left) reset();
    }
  }

  const microsoftGuard: MicrosoftGuard = { busy, run: (step) => void guarded(step) };
  // A technician's step that has `LEFT` was answered by the server: the
  // invitation is spent (above).
  const technicianGuard: MicrosoftGuard = {
    busy,
    run: (step) =>
      void guarded(async () => {
        const left = await step();
        technicianLeft.current = true;
        return left;
      }),
  };

  function submit(kind: "staff" | "contact", minimum: number) {
    const problem = checkNewPassword(password, again, minimum);
    if (problem) {
      setError(problem);
      return;
    }
    void guarded(async () => {
      const result = await api.acceptInvite({ token, password });
      landed(kind, result.state);
    });
  }

  function withPasskey() {
    void guarded(async () => {
      // The options are fetched by this click and nothing else stands before
      // the browser's prompt (a passkey is made only from a click).
      const credential = await createPasskey(await api.invitePasskeyOptions(token));
      await api.invitePasskey({ token, credential, name: passkeyName.trim() });
      landed("contact", "signed_in");
    });
  }

  if (invite.isPending) {
    return (
      <AuthCard title={t("auth.invite.loading")}>
        <p className="note" style={{ margin: 0 }}>
          {t("auth.common.working")}
        </p>
      </AuthCard>
    );
  }
  if (dead || (invite.isError && isDeadLink(invite.error))) {
    return (
      <AuthCard title={t("auth.invite.heading")}>
        <AuthAlert text={t("auth.invite.invalid")} />
      </AuthCard>
    );
  }
  if (invite.isError) {
    return (
      <AuthCard title={t("auth.invite.heading")}>
        <AuthAlert text={describeAuthError(invite.error, { canTimeOut: false })} />
      </AuthCard>
    );
  }
  const { email, display_name, kind, password_minimum, passkey, microsoft } = invite.data;
  const offerPasskey = kind === "contact" && passkey && canUsePasskeys();
  return (
    <AuthCard
      title={t("auth.invite.title", { name: display_name || email })}
      intro={
        <Trans
          k={kind === "contact" ? "auth.invite.introContact" : "auth.invite.intro"}
          vars={{ email }}
          components={{ b: <b style={{ color: "var(--ink)" }} /> }}
        />
      }
    >
      {offerPasskey ? (
        <>
          <Field label={t("auth.invite.passkeyName")}>
            <input
              className={inputClass}
              value={passkeyName}
              maxLength={64}
              onChange={(event) => setPasskeyName(event.target.value)}
            />
          </Field>
          <div className="form-actions">
            <Btn
              variant="primary"
              icon="key"
              label={t("auth.invite.usePasskey")}
              disabled={busy || !passkeyName.trim()}
              onClick={withPasskey}
            />
          </div>
          <p className="note" style={{ margin: 0 }}>
            {t("auth.invite.or")}
          </p>
        </>
      ) : null}
      {microsoft && kind === "contact" ? (
        <>
          <MicrosoftButton
            start={{ intent: "invite", token }}
            back="/portal"
            labelKey="auth.microsoft.acceptInvite"
            variant={offerPasskey ? "secondary" : "primary"}
            guard={microsoftGuard}
          />
          <p className="note" style={{ margin: 0 }}>
            {t("auth.invite.or")}
          </p>
        </>
      ) : null}
      <Form onSubmit={() => submit(kind, password_minimum)}>
        {/* For the password manager: the account the new password belongs to. */}
        <Field label={t("auth.common.email")}>
          <input className={inputClass} type="email" autoComplete="username" value={email} readOnly />
        </Field>
        <NewPasswordFields
          password={password}
          again={again}
          minimum={password_minimum}
          onPassword={setPassword}
          onAgain={setAgain}
        />
        <AuthAlert text={error} />
        <div className="form-actions">
          <Btn
            type="submit"
            variant={offerPasskey ? "secondary" : "primary"}
            icon="arrow-right"
            label={busy ? t("auth.common.working") : t("auth.invite.submit")}
            disabled={busy || !password || !again}
          />
        </div>
      </Form>
      {microsoft && kind === "staff" ? (
        <>
          <p className="note" style={{ margin: 0 }}>
            {t("auth.invite.microsoftWithPassword")}
          </p>
          <MicrosoftButton
            start={{ intent: "invite", token, password }}
            back="/"
            labelKey="auth.microsoft.acceptInvite"
            check={() => checkNewPassword(password, again, password_minimum)}
            guard={technicianGuard}
          />
        </>
      ) : null}
    </AuthCard>
  );
}
