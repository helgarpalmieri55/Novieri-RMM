/**
 * THE PORTAL'S OWN FRAME, for a client's contact (own-auth design §3; plan
 * D42, Task 10). The console's frame -- the rail, the client selector, the
 * organisation, search, health -- reads the console's API, which a contact is
 * refused; this one reads `GET /portal/me` alone and offers the portal's
 * pages (tickets, through each page's own navigation), the contact's own
 * sign-in security (`/portal/security`, Task 11) and signing out.
 *
 * `RequireAuth` draws it when the console's door answers 403 `portal_only`
 * on a portal page. It remembers the person (`sessionPerson`), so a session
 * that ends here is hidden and emptied like any other (decision 2), and
 * signing out lands on the portal's sign-in page. It forgets the client the
 * console's top bar stored on this computer (decision 7).
 *
 * NOTHING HERE GATES ANYTHING (invariant 6).
 *
 * `embedded` (redesign plan, Task 3, pre-flight N1) draws the frame inside a
 * page that already has its own `<main>` -- `/__preview`, where the browser
 * pass measures it -- as a region named by the portal's name instead of a
 * second `<main>`, which axe refuses (`landmark-no-duplicate-main`). Embedded,
 * it is a picture of the frame and nobody's session: it forgets no stored
 * client and records no person, as `AuthCard`'s `embedded` leaves the tab's
 * title alone. Nor does it draw "Sign out" (the /__preview critique, answer 3
 * in `.impeccable/critica-respuestas.md`): a picture has no session to end, a
 * live button would end the developer's console session, and a disarmed one
 * would be a dead control.
 */
import { useLayoutEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { PORTAL_SIGN_IN_PATH, signOut } from "../../lib/session";
import { sessionPerson } from "../../lib/sessionEnd";
import { setSelectedClient } from "../../lib/clientChoice";
import { NovieriLockup } from "../../shell/brand";
import { Btn, KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { PortalNamesTab } from "./portalTitle";
import { ContactFrameContext, usePortalMe } from "./usePortalMe";

export function PortalShell({ children, embedded = false }: { children: ReactNode; embedded?: boolean }) {
  const t = useT();
  const api = useApi();
  // Decision 7 (A2-66): the client the console's top bar stored on this
  // computer -- a technician's, say -- is not this contact's, and as
  // `X-Client-Id` it would narrow their portal to nothing. Forgotten in the
  // first render, before `usePortalMe` fetches: a lazy initial state runs
  // during render, the query's fetch after it. Nothing here sets it again.
  useState(() => {
    if (!embedded) setSelectedClient(null);
    return null;
  });
  const me = usePortalMe();
  const [signOutFailed, setSignOutFailed] = useState(false);
  const userId = me.data?.user_id;
  useLayoutEffect(() => {
    if (!embedded && userId !== undefined) sessionPerson(userId);
  }, [embedded, userId]);
  const Body = embedded ? "section" : "main";

  return (
    // THE NOVIERI BRAND IN THE PORTAL'S OWN FRAME (design §4: "El portal del
    // usuario final ... recibe la misma marca dentro de su propio marco"): the
    // lockup on a white bar with the line below it, the section's name beside
    // it in the secondary ink, and the person with "Sign out" on the right;
    // the pages on the page ground, as wide as the console's workspace.
    <div data-testid="portal-shell" className={embedded ? "bg-[var(--bg)]" : "min-h-screen bg-[var(--bg)]"}>
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-[var(--line)] bg-[var(--surface)] px-4 py-3 sm:px-6">
        <div className="flex min-w-0 items-center gap-3">
          <Link to="/portal" className="inline-flex items-center rounded-[var(--r-ctl)] text-[var(--ink)]">
            <NovieriLockup height={20} />
          </Link>
          <span className="border-l border-[var(--line)] pl-3 text-[var(--muted)]">{t("portal.shell.brand")}</span>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-2">
          {/* The contact's own sign-in security (own-auth phase 2, Task 11). */}
          <Link to="/portal/security" className="text-sm">
            {t("portal.shell.security")}
          </Link>
          <span className="text-sm text-[var(--muted)]">
            {me.data ? me.data.display_name || me.data.email : me.isError ? "" : "…"}
          </span>
          {embedded ? null : (
            <Btn
              small
              label={t("shell.accountMenu.signOut")}
              onClick={() => {
                setSignOutFailed(false);
                signOut(api, PORTAL_SIGN_IN_PATH).catch(() => setSignOutFailed(true));
              }}
            />
          )}
        </div>
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
      <Body
        className="max-w-[1440px] px-4 pb-10 pt-[22px] sm:px-6"
        aria-label={embedded ? t("portal.shell.brand") : undefined}
      >
        {/* Its pages name the tab here; embedded, nothing touches it (portalTitle.tsx). */}
        <PortalNamesTab.Provider value={!embedded}>
          <ContactFrameContext.Provider value={true}>{children}</ContactFrameContext.Provider>
        </PortalNamesTab.Provider>
      </Body>
    </div>
  );
}
