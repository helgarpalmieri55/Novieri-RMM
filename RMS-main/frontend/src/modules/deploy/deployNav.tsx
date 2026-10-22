import { NavLink } from "react-router-dom";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";

// The three deploy surfaces this module contributes, routed separately
// (App.tsx: /deploy, /deploy/packages, /deploy/scripts) — mirrors
// modules/backup/backupNav.tsx and modules/provisioning/provisioningNav.tsx,
// which draw the same strip for the same reason.
//
// REVIEW WAVE, round 2, /deploy/scripts (function): this module was the only
// one of the three in its group with no sub-nav, and NOTHING IN THE CONSOLE
// LINKED TO /deploy/scripts OR /deploy/packages at all — the rail carries one
// "Software Deployment" entry pointing at /deploy, so both sibling pages were
// reachable only by typing the URL. That is the Phase-12 lesson twice over: a
// page routed but unlinked is not reachable. The strip is the link.
//
// The design's Deployment screen has no tab strip of its own (its packages
// and scripts are sections of one long page); this app routes them as three
// pages, so the strip names what actually routes rather than the mock's
// single-page shape.
// The labels are accessors over the catalogue, read when the strip is drawn.
export const DEPLOY_NAV_ITEMS: ReadonlyArray<{ to: string; label: string }> = [
  { to: "/deploy", get label() { return translate("deploy.nav.deployments"); } },
  { to: "/deploy/packages", get label() { return translate("deploy.nav.packages"); } },
  { to: "/deploy/scripts", get label() { return translate("deploy.nav.scripts"); } },
];

export function DeployPageNav({ canRead }: { canRead: boolean }) {
  const t = useT();
  // Gated on the one permission every deploy route already requires
  // (`deploy.read`, shell/nav.ts): a strip offering three pages the caller
  // cannot open is three refusals waiting to happen.
  if (!canRead) return null;
  return (
    <nav aria-label={t("deploy.nav.label")} className="tabs" role="tablist">
      {DEPLOY_NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/deploy"}
          role="tab"
          className={({ isActive }) => `tab${isActive ? " active" : ""}`}
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}
