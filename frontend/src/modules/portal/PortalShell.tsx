/**
 * THE PORTAL'S OWN FRAME, for a client's contact (own-auth design §3; plan
 * D42, Task 10). The console's frame -- the rail, the client selector, the
 * organisation, search, health -- reads the console's API, which a contact is
 * refused; this one reads `GET /portal/me` alone and offers the portal's two
 * pages and signing out.
 *
 * `RequireAuth` draws it when the console's door answers 403 `portal_only`
 * on a portal page. It remembers the person (`sessionPerson`), so a session
 * that ends here is hidden and emptied like any other (decision 2), and
 * signing out lands on the portal's sign-in page. It forgets the client the
 * console's top bar stored on this computer (decision 7).
 *
 * NOTHING HERE GATES ANYTHING (invariant 6).
 */
import { useLayoutEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { PORTAL_SIGN_IN_PATH, signOut } from "../../lib/session";
import { sessionPerson } from "../../lib/sessionEnd";
import { setSelectedClient } from "../../lib/clientChoice";
import { Btn, KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { usePortalMe } from "./usePortalMe";

export function PortalShell({ children }: { children: ReactNode }) {
  const t = useT();
  const api = useApi();
  // Decision 7 (A2-66): the client the console's top bar stored on this
  // computer -- a technician's, say -- is not this contact's, and as
  // `X-Client-Id` it would narrow their portal to nothing. Forgotten in the
  // first render, before `usePortalMe` fetches: a lazy initial state runs
  // during render, the query's fetch after it. Nothing here sets it again.
  useState(() => {
    setSelectedClient(null);
    return null;
  });
  const me = usePortalMe();
  const [signOutFailed, setSignOutFailed] = useState(false);
  const userId = me.data?.user_id;
  useLayoutEffect(() => {
    if (userId !== undefined) sessionPerson(userId);
  }, [userId]);

  return (
    <div data-testid="portal-shell" className="min-h-screen">
      <header className="flex flex-wrap items-center gap-4 border-b border-[var(--line)] px-6 py-3">
        <Link to="/portal" className="font-semibold">
          {t("portal.shell.brand")}
        </Link>
        <span className="ml-auto text-sm text-[var(--muted)]">
          {me.data ? me.data.display_name || me.data.email : me.isError ? "" : "…"}
        </span>
        <Btn
          small
          label={t("shell.accountMenu.signOut")}
          onClick={() => {
            setSignOutFailed(false);
            signOut(api, PORTAL_SIGN_IN_PATH).catch(() => setSignOutFailed(true));
          }}
        />
      </header>
      {signOutFailed ? (
        <p role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={16} />
          <span>{t("shell.app.signOutFailed")}</span>
        </p>
      ) : null}
      {me.isError ? (
        <div role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={16} />
          <span>{t("portal.shell.meFailed")}</span>
          <Btn small label={t("common.actions.retry")} onClick={() => void me.refetch()} />
        </div>
      ) : null}
      <main className="px-6 py-6">{children}</main>
    </div>
  );
}
