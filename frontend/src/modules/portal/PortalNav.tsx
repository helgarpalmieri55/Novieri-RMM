import { useT } from "../../i18n/context";
import { SectionNav } from "../../shell/components";

// The portal's own two-item nav (spec §7: "a separate route namespace and
// its own minimal surface, not a variant of the technician UI"), shared by
// every page so it never falls out of sync (mirrors ServiceDeskNav /
// NotificationNav's precedent -- one copy, not five). Ticket detail is
// reached by clicking a row in the list, the same way service-desk's ticket
// detail is reached, so it has no nav entry of its own.
//
// THE KIT'S SECTION NAVIGATION (redesign plan, "Los menús de sección"): a
// plain `<nav>` of links, the current one marked by `aria-current`, the way
// every module's menu is drawn; its name and its two labels are unchanged.
// "My tickets" is current on `/portal` alone (`end`): New ticket is a form
// page with its own back link, and a ticket's page draws its back link too.
export function PortalNav({ canRead }: { canRead: boolean }) {
  const t = useT();
  if (!canRead) return null;
  return (
    <SectionNav
      label={t("portal.nav.aria")}
      items={[
        { to: "/portal", label: t("portal.nav.myTickets"), end: true },
        { to: "/portal/new", label: t("portal.nav.newTicket") },
      ]}
    />
  );
}
