import { NavLink } from "react-router-dom";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";

// The three backup surfaces this module contributes, routed separately
// (App.tsx: /backup, /backup/repositories, /backup/policies) rather than as
// one route with view-state — mirrors modules/licenses/licenseNav.tsx's
// LicensePageNav (the Phase-12 lesson: a page routed but unlinked is not
// reachable). Rendered with the design's `.tabs`/`.tab` classes (UI port spec
// §5) since these routes stand in for the prototype's renderBackup() tabs.
//
// The design's Backup tabs are Overview / Backup jobs / Restore points /
// Policies. This app has no Overview screen and no separate "jobs" vs.
// "restore points" split — BackupPointsPage covers both (every backup point
// carries its own restore action) — so the nav names what actually routes,
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
    <nav aria-label={t("backup.nav.label")} className="tabs" role="tablist">
      {BACKUP_NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/backup"}
          role="tab"
          className={({ isActive }) => `tab${isActive ? " active" : ""}`}
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}
