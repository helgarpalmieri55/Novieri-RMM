import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { SectionNav } from "../../shell/components";

// The three subscriptions surfaces (App.tsx: /subscriptions,
// /subscriptions/spend, /subscriptions/renewals). The rail links only to the
// registry; the spend and renewal reports were reachable from two buttons in
// its head and from its tiles, and from nowhere on each other. The kit's
// section nav makes the three one module (redesign plan, Task 10, 5.4), the
// way `licenseNav.tsx` does for licenses: a plain navigation of links, the
// current page marked with `aria-current="page"`.
//
// The labels are accessors over the catalogue, read when the strip is drawn.
export const SUBSCRIPTION_NAV_ITEMS: ReadonlyArray<{ to: string; label: string }> = [
  { to: "/subscriptions", get label() { return translate("subscriptions.nav.subscriptions"); } },
  { to: "/subscriptions/spend", get label() { return translate("subscriptions.nav.spend"); } },
  { to: "/subscriptions/renewals", get label() { return translate("subscriptions.nav.renewals"); } },
];

/**
 * Drawn for whoever may read subscriptions: the same condition the rail entry
 * uses, and presentational only (`require_permission` gates each route).
 */
export function SubscriptionPageNav({ canRead }: { canRead: boolean }) {
  const t = useT();
  if (!canRead) return null;
  return (
    <SectionNav
      label={t("subscriptions.nav.label")}
      items={SUBSCRIPTION_NAV_ITEMS.map((item) => ({ to: item.to, label: item.label, end: item.to === "/subscriptions" }))}
    />
  );
}
