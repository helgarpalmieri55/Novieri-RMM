import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { SectionNav } from "../../shell/components";

// The three service-desk surfaces (spec §11), shared by every page so the
// module nav renders identically everywhere -- the Phase-12 lesson: a page
// routed but unlinked is not reachable. Only ONE copy of this component
// exists (the Budget module shipped five verbatim copies and had to have
// them extracted into `BudgetPageNav`; this module starts with the shared
// version instead).
//
// THE KIT'S SECTION NAVIGATION (redesign plan, "Los menús de sección";
// Task 7, 2.2): a plain `<nav>` of links with the current one marked by
// `aria-current`, the way every module's menu is drawn now. Its name, its
// labels and the `ticket.admin` check on Admin are unchanged. "Tickets" is
// current on the queue alone (`end`); a ticket's own page draws its back
// link to the queue instead of this strip.
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
  if (!canRead) return null;
  const items = SERVICE_DESK_NAV_ITEMS.filter((item) => !item.admin || canAdmin);
  return (
    <SectionNav
      label={t("serviceDesk.nav.aria")}
      items={items.map((item) => ({ to: item.to, label: t(item.label), end: item.to === "/service-desk" }))}
    />
  );
}
