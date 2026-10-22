import { Link, useLocation } from "react-router-dom";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";

// The module's six surfaces, in one place so App.tsx's Shell nav and every
// in-page nav cannot fall out of sync (the precedent SlaNav and
// NotificationNav set).
//
// "Drift" and "Scan authorizations" are separate entries on purpose. They are
// not two views of one thing: one is a list of findings anybody with
// `ipam.read` should be reading weekly, the other is the control surface that
// decides where this platform emits network traffic.
//
// STYLED WITH THE DESIGN'S `.tabs`/`.tab` CLASSES (the same markup `Tabs` in
// shell/components emits), not the shared `Tabs` component itself — this nav
// switches ROUTES, not in-page state, and `Tabs` takes an `onSelect` callback
// rather than a destination. No route changes; only the visual idiom moves.
//
// AND IT BORROWS THE LOOK WITHOUT BORROWING THE ROLES. This strip carried
// `role="tablist"` with `role="tab"` links and `aria-selected`, which told a
// screen reader "tab, selected, 2 of 6" about six links that navigate: there
// is no tabpanel for them to control, arrow-key movement inside a tablist is
// not implemented and could not be (each item is a destination, and moving
// focus would leave the page), and the design's own `.tabs` markup carries no
// ARIA at all. It is navigation, so it is announced as navigation: a named
// `<nav>` of links, with `aria-current="page"` on the one you are on.
//
// RENDERED AFTER THE PAGE HEAD, never before it. Every page in this console
// draws crumb, then h1, then description, then its tab strip (v3's `head()`
// then `tabs()`); a strip between the crumb and the h1 sits exactly where the
// design forbids an eyebrow, and at 390px its two wrapped rows pushed the h1
// most of a screen down the page.
const IPAM_NAV_ITEMS: ReadonlyArray<{ to: string; label: MessageKey }> = [
  { to: "/ipam", label: "ipam.nav.subnets" },
  { to: "/ipam/drift", label: "ipam.nav.drift" },
  { to: "/ipam/search", label: "ipam.nav.search" },
  { to: "/ipam/registry", label: "ipam.nav.registry" },
  { to: "/ipam/scan-authorizations", label: "ipam.nav.scanAuthorizations" },
  { to: "/ipam/import", label: "ipam.nav.importExport" },
];

export function IpamNav() {
  const t = useT();
  const location = useLocation();
  return (
    <nav aria-label={t("ipam.nav.label")} className="tabs">
      {IPAM_NAV_ITEMS.map((item) => {
        const active =
          item.to === "/ipam"
            ? location.pathname === "/ipam" || location.pathname.startsWith("/ipam/subnets")
            : location.pathname.startsWith(item.to);
        return (
          <Link
            key={item.to}
            to={item.to}
            className={`tab ${active ? "active" : ""}`}
            aria-current={active ? "page" : undefined}
          >
            {t(item.label)}
          </Link>
        );
      })}
    </nav>
  );
}
