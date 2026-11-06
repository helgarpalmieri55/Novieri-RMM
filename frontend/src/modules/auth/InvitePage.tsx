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
 * SAMESITE=STRICT IS WHY THIS IS A PAGE AND NOT A SERVER LINK (plan decision
 * D19). The email link is a cross-site top-level navigation, which carries no
 * Strict cookie; this page's own requests are same-origin and do.
 */
import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Form } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { useApi } from "../../lib/ApiContext";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard } from "./AuthCard";
import { checkNewPassword, describeAuthError, isDeadLink } from "./errors";
import { useLinkToken } from "./flow";
import { NewPasswordFields } from "./NewPasswordFields";

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
      await api.acceptInvite({ token, password });
      navigate("/sign-in/enroll", { replace: true });
    } catch (cause: unknown) {
      if (isDeadLink(cause)) setDead(true);
      else setError(describeAuthError(cause, { canTimeOut: false }));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
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
  const { email, display_name } = invite.data;
  return (
    <AuthCard
      title={t("auth.invite.title", { name: display_name })}
      intro={<Trans k="auth.invite.intro" vars={{ email }} components={{ b: <b style={{ color: "var(--ink)" }} /> }} />}
    >
      <Form onSubmit={() => void submit()}>
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
            icon="arrow-right"
            label={busy ? t("auth.common.working") : t("auth.invite.submit")}
            disabled={busy || !password || !again}
          />
        </div>
      </Form>
    </AuthCard>
  );
}
