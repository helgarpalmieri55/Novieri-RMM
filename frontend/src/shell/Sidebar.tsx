/**
 * The rail -- v3's `.rail` markup (`rail()` in `docs/design/itms-prototype-v3
 * .html`: a brand row, then per group a `.grp` caption and one `<a>` per
 * destination with a Tabler glyph, a `.lbl` and an optional mono `.ct` count,
 * `.hot` for the alert count), with authorization.
 *
 * Structure, class names and order are the design's (see `shell.css`). What
 * this adds is `visibleGroups`: the prototype renders all thirty items because
 * a mock has no operator, and a real console must not offer a technician a
 * link that answers 403.
 *
 * THE HIDING IS COSMETIC, AND SAYING SO MATTERS. Invariant 6 -- the frontend
 * never gates security. Every route behind these links is enforced server-side
 * by `require_permission`, and RBAC completeness is derived from the router by
 * reflection rather than from any list here. If this component rendered every
 * link to everyone, the platform would be exactly as secure and merely ruder.
 */

import { Fragment } from "react";
import { Link, useLocation } from "react-router-dom";

import { useT } from "../i18n/context";
import { Icon } from "./icons";
import { activeNavTo, isArea, OPTIONAL_MODULES, visibleGroups } from "./nav";

export interface SidebarProps {
  permissions: readonly string[];
  /** Optional modules on for the current choice (phase 3 design §2). Absent: every one. */
  modules?: ReadonlySet<string>;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** Live counts, keyed by route. Absent = no badge, which is not the same as zero. */
  badges?: Readonly<Record<string, number>>;
  orgName: string;
}

export function Sidebar({
  permissions,
  modules,
  collapsed,
  onToggleCollapsed,
  badges = {},
  orgName,
}: SidebarProps) {
  const t = useT();
  const groups = visibleGroups(permissions, modules ?? OPTIONAL_MODULES);
  const pathname = useLocation().pathname;
  const activeTo = activeNavTo(pathname, groups);

  return (
    <aside className="sidebar" id="sidebar">
      <div className="brand">
        {/* v3 `.brand i`: the shield on the accent. */}
        <span className="brand-mark" aria-hidden="true">
          <Icon name="shield-check" size={16} />
        </span>
        <span className="brand-name">ITMS</span>
        {/* Not in v3, which has no collapse. Kept: density is a standing
            preference and the shell persists it (`itms.sidebar.collapsed`). */}
        <button
          className="collapse-btn"
          onClick={onToggleCollapsed}
          title={collapsed ? t("shell.sidebar.expand") : t("shell.sidebar.collapse")}
          aria-label={collapsed ? t("shell.sidebar.expand") : t("shell.sidebar.collapse")}
          aria-expanded={!collapsed}
        >
          <Icon name={collapsed ? "chevron-right" : "chevron-left"} size={15} />
        </button>
      </div>

      <nav className="nav-scroll" id="nav" aria-label={t("shell.sidebar.main")}>
        {groups.map((group) => (
          <section className="nav-group" key={group.label}>
            <div className="grp">{group.label}</div>
            {group.items.map((entry) => {
              // A count of zero renders no `.ct`. "Nothing to deal with" and
              // "a 0 sitting beside the label" read differently, and the
              // design uses the count to draw the eye.
              const count = badges[entry.to];
              // `.hot` is the alert count. The nav model marks it with a
              // badge tone rather than this file naming a route.
              const hot = !isArea(entry) && entry.badgeTone !== undefined;
              const lit = entry.to === activeTo;
              // THE SECOND LAYER OPENS UNDER ITS AREA while the operator is in
              // it, so moving between two Admin pages is one click rather than
              // a trip through the landing page. One link carries
              // `aria-current`: the page, or the area on its own landing page.
              const sections = lit && isArea(entry) ? entry.sections : [];
              const pages = sections.flatMap((s) => s.items);
              const pageTo = activeNavTo(pathname, [{ label: "", items: pages }]);
              return (
                <Fragment key={`${group.label}:${entry.label}`}>
                  <Link
                    className={`nav-item${lit ? " active" : ""}`}
                    aria-current={lit && pageTo === undefined ? "page" : undefined}
                    aria-expanded={isArea(entry) ? lit : undefined}
                    to={entry.to}
                    title={entry.label}
                  >
                    <Icon name={entry.icon} size={17} />
                    <span className="lbl">{entry.label}</span>
                    {count ? <span className={`ct${hot ? " hot" : ""}`}>{count}</span> : null}
                  </Link>
                  {/* FULL-SIZE, WITH ICONS AND THEIR SECTION HEADINGS. The first
                      version drew these as small grey text with no icon, and
                      the operator read that as "without importance". They are
                      the same kind of destination as the items above; the
                      indent and the headings are what say they belong to Admin. */}
                  {sections.map((section) => (
                    <Fragment key={section.label}>
                      <div className="grp sub">{section.label}</div>
                      {section.items.map((item) => (
                        <Link
                          className={`nav-item sub${item.to === pageTo ? " active" : ""}`}
                          aria-current={item.to === pageTo ? "page" : undefined}
                          to={item.to}
                          key={item.to}
                          title={item.label}
                        >
                          <Icon name={item.icon} size={16} />
                          <span className="lbl">{item.label}</span>
                        </Link>
                      ))}
                    </Fragment>
                  ))}
                </Fragment>
              );
            })}
          </section>
        ))}
      </nav>

      {/* IDENTITY, NOT A CONTROL, and it was a `<button>` with no handler --
          filed by both auditors. A button that does nothing on click leaves the
          user unable to tell whether the click registered, the app hung, or
          they lack permission. This is the ONE place the tenant is named; v3
          puts the org in the account chip's small line instead, and the spec
          (§3) keeps the card here so the chip stays the account and the rail
          foot stays the workspace. It becomes a switcher again when this
          deployment has a second tenant to switch to. */}
      <div className="sidebar-footer">
        <div className="tenant-card" data-testid="tenant-identity">
          <div className="tenant-avatar">{orgName.slice(0, 2).toUpperCase()}</div>
          <div className="tenant-meta">
            <div className="tenant-name">{orgName}</div>
            <div className="tenant-plan">{t("shell.sidebar.plan")}</div>
          </div>
        </div>
      </div>
    </aside>
  );
}
