import { NavLink } from "react-router-dom";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";

// The six licenses surfaces this module contributes. App.tsx's top-level
// Shell nav links only to "/licenses" (LicensesPage) -- these are what make
// the other five reachable at all, mirroring modules/budget/budgetState.tsx's
// BudgetPageNav (the Phase-12 lesson: a page routed but unlinked is not
// reachable). Rendered with the design's `.tabs`/`.tab` classes (§5) since
// these routes stand in for the prototype's renderLicenses() tabs -- the
// mock switches view within one route; this app routes each view
// separately, so the "tabs" are real links, not view-state.
// Labels are distinct from in-page action-button text (e.g. LicensesPage's
// own "New license") so an accessible-name query never collides between the
// nav link and a page control.
// The labels are accessors over the catalogue, read when the strip is drawn.
export const LICENSE_NAV_ITEMS: ReadonlyArray<{ to: string; label: string }> = [
  { to: "/licenses", get label() { return translate("licenses.nav.licenses"); } },
  { to: "/licenses/compliance", get label() { return translate("licenses.nav.compliance"); } },
  { to: "/licenses/review-queue", get label() { return translate("licenses.nav.reviewQueue"); } },
  { to: "/licenses/renewals", get label() { return translate("licenses.nav.renewals"); } },
  { to: "/licenses/products", get label() { return translate("licenses.nav.products"); } },
  { to: "/licenses/vendors", get label() { return translate("licenses.nav.vendors"); } },
];

export function LicensePageNav({ canRead }: { canRead: boolean }) {
  const t = useT();
  if (!canRead) return null;
  return (
    <nav aria-label={t("licenses.nav.label")} className="tabs" role="tablist">
      {LICENSE_NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/licenses"}
          role="tab"
          className={({ isActive }) => `tab${isActive ? " active" : ""}`}
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}
