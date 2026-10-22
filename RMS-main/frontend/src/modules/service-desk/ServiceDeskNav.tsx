import { Link, useLocation } from "react-router-dom";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";

// The three service-desk surfaces (spec §11), shared by every page so the
// module nav renders identically everywhere -- the Phase-12 lesson: a page
// routed but unlinked is not reachable. Only ONE copy of this component
// exists (the Budget module shipped five verbatim copies and had to have
// them extracted into `BudgetPageNav`; this module starts with the shared
// version instead).
//
// DRAWN AS v3's TAB STRIP (`.tabs`), the way `PushNav` and `IpamNav` draw a
// module's surfaces, rather than as a row of outline buttons: a button that
// navigates is a link wearing the wrong costume, and the strip is what the
// design puts under a page head to move between views of one module.
//
// Labels are distinct from any in-page action button text so a query by
// accessible name never collides between the nav link and a page control.
const SERVICE_DESK_NAV_ITEMS: ReadonlyArray<{ to: string; label: MessageKey; admin?: true }> = [
  { to: "/service-desk", label: "serviceDesk.nav.tickets" },
  { to: "/service-desk/my-work", label: "serviceDesk.nav.myWork" },
  { to: "/service-desk/admin", label: "serviceDesk.nav.admin", admin: true },
];

export function ServiceDeskNav({
  canRead,
  canAdmin,
}: {
  canRead: boolean;
  // The Admin surface (queues/categories management) is gated on
  // `ticket.admin`, not merely `ticket.read` -- spec §11: "Admin -- queues
  // and categories, gated on ticket.admin". A technician (read/write/assign,
  // never admin) sees Tickets/My work but not the Admin link.
  canAdmin: boolean;
}) {
  const t = useT();
  const { pathname } = useLocation();
  if (!canRead) return null;
  const items = SERVICE_DESK_NAV_ITEMS.filter((item) => !item.admin || canAdmin);
  // `/service-desk` is a prefix of every other entry, so it is active only on
  // the exact path and on a ticket's detail page; the other two match by prefix.
  const isActive = (to: string) =>
    to === "/service-desk"
      ? pathname === to || pathname.startsWith("/service-desk/tickets")
      : pathname.startsWith(to);
  return (
    <nav aria-label={t("serviceDesk.nav.aria")} className="tabs">
      {items.map((item) => {
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
