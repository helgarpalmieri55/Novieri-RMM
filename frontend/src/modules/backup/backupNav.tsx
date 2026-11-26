import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { SectionNav } from "../../shell/components";

// The backup surfaces this module contributes, routed separately (App.tsx:
// /backup, /backup/coverage, /backup/repositories, /backup/policies,
// /backup/offsite) rather than as one route with view-state -- mirrors
// modules/licenses/licenseNav.tsx's LicensePageNav (the Phase-12 lesson: a
// page routed but unlinked is not reachable).
//
// ON THE KIT'S SECTION NAV (redesign plan, waves' common rules: "Los menús de
// sección ... pasan a dibujarse con SectionNav"). These are links between
// pages, not tabs over panels: the strip used to say `role="tablist"`, which
// promises arrow keys and panels that links do not have. `SectionNav` is a
// plain navigation and marks the current page with `aria-current="page"`.
//
// The design's Backup tabs are Overview / Backup jobs / Restore points /
// Policies. This app has no Overview screen and no separate "jobs" vs.
// "restore points" split -- BackupPointsPage covers both (every backup point
// carries its own restore action) -- so the nav names what actually routes,
// not the mock's four-way split.
// The labels are accessors over the catalogue, read when the strip is drawn.
export const BACKUP_NAV_ITEMS: ReadonlyArray<{ to: string; label: string }> = [
  { to: "/backup", get label() { return translate("backup.nav.points"); } },
  { to: "/backup/coverage", get label() { return translate("backup.nav.coverage"); } },
  { to: "/backup/repositories", get label() { return translate("backup.nav.repositories"); } },
  { to: "/backup/policies", get label() { return translate("backup.nav.policies"); } },
  { to: "/backup/offsite", get label() { return translate("backup.nav.offsite"); } },
];

export function BackupPageNav({ canRead }: { canRead: boolean }) {
  const t = useT();
  if (!canRead) return null;
  return (
    <SectionNav
      label={t("backup.nav.label")}
      items={BACKUP_NAV_ITEMS.map((item) => ({ to: item.to, label: item.label, end: item.to === "/backup" }))}
    />
  );
}
