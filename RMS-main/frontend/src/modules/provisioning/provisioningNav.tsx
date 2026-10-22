import { NavLink } from "react-router-dom";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";

// The three provisioning surfaces this module contributes, routed separately
// (App.tsx: /provisioning, /provisioning/images, /provisioning/profiles)
// rather than as one route with view-state — mirrors
// modules/licenses/licenseNav.tsx's LicensePageNav.
//
// The design's Provisioning tabs are Overview / Images / Task sequences /
// Build history. This app has no Overview screen and no separate task
// sequence entity — a ProvisionProfile IS the closest analogue (image +
// post-config + target scope), so "Profiles" stands in for "Task sequences"
// rather than inventing a fourth entity the backend does not have.
// The labels are accessors over the catalogue, read when the strip is drawn.
export const PROVISIONING_NAV_ITEMS: ReadonlyArray<{ to: string; label: string }> = [
  { to: "/provisioning", get label() { return translate("provisioning.nav.runs"); } },
  { to: "/provisioning/images", get label() { return translate("provisioning.nav.images"); } },
  { to: "/provisioning/profiles", get label() { return translate("provisioning.nav.profiles"); } },
];

export function ProvisioningPageNav({ canRead }: { canRead: boolean }) {
  const t = useT();
  if (!canRead) return null;
  return (
    <nav aria-label={t("provisioning.nav.label")} className="tabs" role="tablist">
      {PROVISIONING_NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/provisioning"}
          role="tab"
          className={({ isActive }) => `tab${isActive ? " active" : ""}`}
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}
