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
 * A link the server no longer honours (410 `invalid_link`: used, expired or
 * unknown) reads as one state, and says who can make another. The token comes
 * from the fragment and travels in bodies only (`./flow.ts`).
 */
import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { useApi } from "../../lib/ApiContext";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard } from "./AuthCard";
import { describeAuthError, isDeadLink } from "./errors";
import { useLinkToken } from "./flow";

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
  const [dead, setDead] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  async function recover() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await api.acceptRecovery(token);
      navigate("/sign-in/enroll", { replace: true });
    } catch (cause: unknown) {
      if (isDeadLink(cause)) setDead(true);
      else setError(describeAuthError(cause, { canTimeOut: false }));
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
  return (
    <AuthCard
      title={t("auth.recover.title")}
      intro={
        <Trans
          k="auth.recover.intro"
          vars={{ email: recovery.data.email }}
          components={{ b: <b style={{ color: "var(--ink)" }} /> }}
        />
      }
    >
      <p className="note" style={{ margin: 0 }}>
        {t("auth.recover.what")}
      </p>
      <AuthAlert text={error} />
      <div className="form-actions">
        <Btn
          variant="primary"
          icon="key"
          label={busy ? t("auth.common.working") : t("auth.recover.submit")}
          disabled={busy}
          onClick={() => void recover()}
        />
      </div>
    </AuthCard>
  );
}
