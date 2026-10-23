import { Link, useLocation } from "react-router-dom";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";

// The portal's own two-item nav (spec §7: "a separate route namespace and
// its own minimal surface, not a variant of the technician UI"), shared by
// every page so it never falls out of sync (mirrors ServiceDeskNav /
// NotificationNav's precedent -- one copy, not five). Ticket detail is
// reached by clicking a row in the list, the same way service-desk's ticket
// detail is reached, so it has no nav entry of its own.
//
// Drawn as v3's tab strip, the way every module nav in the console now is:
// a link that navigates is drawn as a link, not as a button.
const PORTAL_NAV_ITEMS: ReadonlyArray<{ to: string; label: MessageKey }> = [
  { to: "/portal", label: "portal.nav.myTickets" },
  { to: "/portal/new", label: "portal.nav.newTicket" },
];

export function PortalNav({ canRead }: { canRead: boolean }) {
  const t = useT();
  const { pathname } = useLocation();
  if (!canRead) return null;
  const isActive = (to: string) => (to === "/portal" ? pathname !== "/portal/new" : pathname.startsWith(to));
  return (
    <nav aria-label={t("portal.nav.aria")} className="tabs">
      {PORTAL_NAV_ITEMS.map((item) => {
        const active = isActive(item.to);
        return (
          <Link
            key={item.to}
            to={item.to}
            className={active ? "tab active" : "tab"}
            aria-current={active ? "page" : undefined}
          >
            {t(item.label)}
          </Link>
        );
      })}
    </nav>
  );
}
