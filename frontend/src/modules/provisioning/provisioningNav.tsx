import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { SectionNav } from "../../shell/components";

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
//
// ON THE KIT'S SECTION NAV (redesign plan, waves' common rules): links
// between pages, so a navigation with `aria-current="page"`, not the
// `role="tablist"` it used to claim.
export const PROVISIONING_NAV_ITEMS: ReadonlyArray<{ to: string; label: string }> = [
  { to: "/provisioning", get label() { return translate("provisioning.nav.runs"); } },
  { to: "/provisioning/images", get label() { return translate("provisioning.nav.images"); } },
  { to: "/provisioning/profiles", get label() { return translate("provisioning.nav.profiles"); } },
];

export function ProvisioningPageNav({ canRead }: { canRead: boolean }) {
  const t = useT();
  if (!canRead) return null;
  return (
    <SectionNav
      label={t("provisioning.nav.label")}
      items={PROVISIONING_NAV_ITEMS.map((item) => ({ to: item.to, label: item.label, end: item.to === "/provisioning" }))}
    />
  );
}
