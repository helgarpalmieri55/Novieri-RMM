/**
 * `/recover#<token>` — the link the server's recovery command prints, for an
 * administrator who lost every second factor and has nobody to reset them
 * (own-auth design §7, Q5: `python -m app.core.auth.recover --email …`, own-auth
 * Task 8). It exists only on the server host: there is no way to ask for one
 * over HTTP, and it lives 15 minutes.
 *
 * WHAT IT DOES, SAID BEFORE IT DOES IT. Accepting removes the account's second
 * factors and recovery codes and leaves a session that has none, so the next
 * page is `/sign-in/enroll`, and the console opens only after a new factor and
 * new codes (§2.2). The page shows whose account the link is for and asks for
 * one deliberate press; opening the link does nothing by itself.
 *
 * A NEW PASSWORD IS OFFERED ON EVERY RECOVERY (operator decision 2026-11-05,
 * «Ofrecerla siempre, aplicar»; design §7), and the server applies one that
 * is given. Optional, it may be left empty: only the token travels, and the
 * password stays.
 *
 * REQUIRED WHEN AN EMAILED LINK SET THE PASSWORD (operator decision
 * 2026-11-05, «Pedir contraseña nueva si hay marca»). That password may be
 * whoever holds the mailbox's, so the server then asks for a new one before
 * the recovery finishes. The page knows from the details (`password_required`)
 * or, when a mail reset landed after the page opened, from the accept's 409
 * `recovery_needs_password`: then it shows the server's sentence, the field
 * becomes required, nothing is spent, and the person presses again.
 *
 * The field is the invitation's (`NewPasswordFields`, `checkNewPassword`, the
 * server's `password_rejected` sentence shown as sent); a secret, so
 * `type="password"` and never `data-keep`, and the accept is a
 * `useSecretMutation` (`src/test/secretMutations.test.ts`): neither the
 * password nor the token stays in TanStack's mutation cache.
 *
 * A link the server no longer honours (410 `invalid_link`: used, expired or
 * unknown) reads as one state, and says who can make another. The token comes
 * from the fragment and travels in bodies only (`./flow.ts`).
 */
import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Form } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { apiErrorCode } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useSecretMutation } from "../../lib/secretMutation";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard } from "./AuthCard";
import { checkNewPassword, describeAuthError, isDeadLink } from "./errors";
import { useLinkToken } from "./flow";
import { NewPasswordFields } from "./NewPasswordFields";
import type { RecoveryAccept } from "./types";

/** The accept's 409 while an emailed link set the password and none was sent (`flows.RECOVERY_NEEDS_PASSWORD`). */
const NEEDS_PASSWORD = "recovery_needs_password";

export function RecoverPage() {
  const t = useT();
  const api = useApi();
  const navigate = useNavigate();
  const token = useLinkToken();
  // Asked even with no token, as `InvitePage` is: the server's 410 decides, and
  // fixture mode still draws the page for the harmony gate.
  // ASKED ONCE, AND THE TOKEN IS NOT KEPT, for `InvitePage`'s reasons (final
  // review D1, Minor 4): every ask spends the per-IP limit the accept step
  // shares, a language switch invalidates every query but a static one, and
  // the token stays out of the key and the answer leaves with the page.
  const recovery = useQuery({
    queryKey: ["auth", "recover"],
    queryFn: () => api.getRecovery(token),
    retry: false,
    staleTime: "static",
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
  const accept = useSecretMutation({
    mutationFn: (body: RecoveryAccept) => api.acceptRecovery(body),
  });
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  // The accept said a new password is needed: a mail reset after the details.
  const [raced, setRaced] = useState(false);
  const [dead, setDead] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  async function recover(required: boolean) {
    if (inFlight.current) return;
    // Optional and left empty: only the token, and the password stays.
    const withPassword = required || password !== "" || again !== "";
    if (withPassword) {
      const problem = checkNewPassword(password, again);
      if (problem) {
        setError(problem);
        return;
      }
    }
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await accept.mutateAsync(withPassword ? { token, password } : { token });
      navigate("/sign-in/enroll", { replace: true });
    } catch (cause: unknown) {
      if (isDeadLink(cause)) setDead(true);
      else {
        // The link is not spent: the server's sentence, the field, and another press.
        if (apiErrorCode(cause) === NEEDS_PASSWORD) setRaced(true);
        setError(describeAuthError(cause, { canTimeOut: false }));
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  if (recovery.isPending) {
    return (
      <AuthCard title={t("auth.recover.loading")}>
        <p className="note" style={{ margin: 0 }}>
          {t("auth.common.working")}
        </p>
      </AuthCard>
    );
  }
  if (dead || (recovery.isError && isDeadLink(recovery.error))) {
    return (
      <AuthCard title={t("auth.recover.heading")}>
        <AuthAlert text={t("auth.recover.invalid")} />
      </AuthCard>
    );
  }
  if (recovery.isError) {
    return (
      <AuthCard title={t("auth.recover.heading")}>
        <AuthAlert text={describeAuthError(recovery.error, { canTimeOut: false })} />
      </AuthCard>
    );
  }
  const { email, password_required } = recovery.data;
  // Required while an emailed link set the password, or once the accept said so (409).
  const required = password_required || raced;
  return (
    <AuthCard
      title={t("auth.recover.title")}
      intro={
        <Trans k="auth.recover.intro" vars={{ email }} components={{ b: <b style={{ color: "var(--ink)" }} /> }} />
      }
    >
      <p className="note" style={{ margin: 0 }}>
        {t("auth.recover.what")}
      </p>
      <Form onSubmit={() => void recover(required)}>
        {password_required ? (
          <p className="note" style={{ margin: 0 }}>
            {t("auth.recover.newPassword")}
          </p>
        ) : raced ? null : (
          <p className="note" style={{ margin: 0 }}>
            {t("auth.recover.optionalPassword")}
          </p>
        )}
        {/* For the password manager: the account the new password belongs to. */}
        <Field label={t("auth.common.email")}>
          <input className={inputClass} type="email" autoComplete="username" value={email} readOnly />
        </Field>
        <NewPasswordFields password={password} again={again} onPassword={setPassword} onAgain={setAgain} />
        <AuthAlert text={error} />
        <div className="form-actions">
          <Btn
            type="submit"
            variant="primary"
            icon="key"
            label={busy ? t("auth.common.working") : t("auth.recover.submit")}
            disabled={busy || (required && (!password || !again))}
          />
        </div>
      </Form>
    </AuthCard>
  );
}
