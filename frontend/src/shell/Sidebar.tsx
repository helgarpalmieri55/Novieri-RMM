/**
 * The rail (redesign design §1.1; DESIGN.md v4, "Navigation"): Atera's order
 * with no captions, the Assets and More folders (decision D9), and the
 * Assistant and Admin anchored at the foot; Admin swaps the rail for its own
 * panel (decision D10, `AdminPanel`).
 *
 * THE HIDING IS COSMETIC, AND SAYING SO MATTERS. Invariant 6 -- the frontend
 * never gates security. Every route behind these links is enforced
 * server-side by `require_permission`; if this component rendered every link
 * to everyone, the platform would be exactly as secure and merely ruder.
 */
import { useEffect, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

import { useT } from "../i18n/context";
import { AdminPanel } from "./AdminPanel";
import { NovieriSymbol, NovieriWordmark } from "./brand";
import { useDrawer } from "./handset";
import { Icon } from "./icons";
import { activeNavTo, isArea, isFolder, OPTIONAL_MODULES, visibleGroups, type NavFolder, type NavItem } from "./nav";

export interface SidebarProps {
  permissions: readonly string[];
  /** Optional modules on for the current choice (phase 3 design §2). Absent: every one. */
  modules?: ReadonlySet<string>;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** Live counts, keyed by route. Absent = no badge, which is not the same as zero. */
  badges?: Readonly<Record<string, number>>;
  orgName: string;
  /** The client selector, on a handset only: it heads the drawer there (`./handset`). */
  clientSelector?: ReactNode;
}

/** One destination in the rail. The current one carries `aria-current` and its dot. */
export function RailLink({
  item,
  lit,
  count,
  sub = false,
}: {
  item: NavItem;
  lit: boolean;
  count?: number;
  sub?: boolean;
}) {
  // `.hot` is the alert count, drawn as a critical badge (DESIGN.md v4, D3).
  const hot = item.badgeTone !== undefined;
  return (
    <Link
      className={`nav-item${sub ? " sub" : ""}${lit ? " active" : ""}`}
      aria-current={lit ? "page" : undefined}
      to={item.to}
      title={item.label}
    >
      <Icon name={item.icon} size={sub ? 16 : 17} />
      <span className="lbl">{item.label}</span>
      {/* A zero renders nothing: "nothing to deal with" is not a 0 beside the label. */}
      {count ? <span className={hot ? "ct hot" : "ct"}>{count}</span> : null}
      {lit ? <span className="nav-dot" aria-hidden="true" /> : null}
    </Link>
  );
}

/**
 * Assets or More: a heading that opens its pages in place, open by itself when
 * it holds the current page. FOLDED (decision D9) its pages show as icons
 * under the folder's own icon, so none is a click further away; the folder is
 * then a mark, not a control, because there is nothing left to open.
 */
function Folder({
  folder,
  activeTo,
  badges,
  folded,
}: {
  folder: NavFolder;
  activeTo: string | undefined;
  badges: Readonly<Record<string, number>>;
  folded: boolean;
}) {
  const holds = folder.items.some((item) => item.to === activeTo);
  const [open, setOpen] = useState(holds);
  useEffect(() => {
    if (holds) setOpen(true);
  }, [holds]);
  const id = `nav-folder-${folder.key}`;
  return (
    <div className="nav-folder">
      {folded ? (
        <div className="nav-item nav-folder-h folded" title={folder.label} aria-hidden="true">
          <Icon name={folder.icon} size={17} />
        </div>
      ) : (
        <button
          type="button"
          className={holds && !open ? "nav-item nav-folder-h active" : "nav-item nav-folder-h"}
          aria-expanded={open}
          aria-controls={id}
          title={folder.label}
          onClick={() => setOpen((value) => !value)}
        >
          <Icon name={folder.icon} size={17} />
          <span className="lbl">{folder.label}</span>
          <Icon name="chevron-down" size={14} className={open ? "chev open" : "chev"} />
        </button>
      )}
      <div id={id} hidden={!(open || folded)}>
        {folder.items.map((item) => (
          <RailLink key={item.to} item={item} lit={item.to === activeTo} count={badges[item.to]} sub />
        ))}
      </div>
    </div>
  );
}

export function Sidebar({
  permissions,
  modules,
  collapsed,
  onToggleCollapsed,
  badges = {},
  orgName,
  clientSelector,
}: SidebarProps) {
  const t = useT();
  // THE FOLD IS A DESK STATE (Task 4 review, fix round 1). Below 1024px the
  // rail is a full-width drawer and `shell.css` reverts the stored fold there,
  // so the folders and the Admin panel draw their unfolded form too.
  const drawer = useDrawer();
  const folded = collapsed && !drawer;
  const groups = visibleGroups(permissions, modules ?? OPTIONAL_MODULES);
  const pathname = useLocation().pathname;
  const activeTo = activeNavTo(pathname, groups);
  // Every group but the foot: the operator's `main`, or a requester's `help`
  // (`PORTAL_GROUPS`), whose two pages are their whole rail.
  const main = groups.filter((group) => group.key !== "anchored").flatMap((group) => group.items);
  const anchored = groups.find((group) => group.key === "anchored")?.items ?? [];
  const admin = anchored.find(isArea);
  const inAdmin = admin !== undefined && activeTo === admin.to;
  const [view, setView] = useState<"main" | "admin">(inAdmin ? "admin" : "main");
  // Arriving on an Admin page opens its panel; leaving Admin puts the rail back.
  useEffect(() => setView(inAdmin ? "admin" : "main"), [inAdmin]);

  return (
    <aside className="sidebar" id="sidebar">
      <div className="brand">
        <NovieriSymbol tile size={28} />
        <NovieriWordmark className="brand-name" height={14} />
        {/* Density is a standing preference and the shell persists it. */}
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

      {clientSelector ? <div className="sidebar-client">{clientSelector}</div> : null}

      {view === "admin" && admin ? (
        <AdminPanel area={admin} pathname={pathname} collapsed={folded} onBack={() => setView("main")} />
      ) : (
        <nav className="nav-scroll" id="nav" aria-label={t("shell.sidebar.main")}>
          {main.map((entry) =>
            isFolder(entry) ? (
              <Folder key={entry.key} folder={entry} activeTo={activeTo} badges={badges} folded={folded} />
            ) : isArea(entry) ? null : (
              <RailLink key={entry.to} item={entry} lit={entry.to === activeTo} count={badges[entry.to]} />
            ),
          )}
        </nav>
      )}

      {/* THE FOOT, only when something is anchored there: a requester, or an
          operator with neither the Assistant nor an Admin page, gets no empty
          landmark and no divider (Task 4 review, fix round 1). */}
      {anchored.length > 0 ? (
        <nav className="nav-anchored" aria-label={t("shell.sidebar.anchored")}>
          {anchored.map((entry) =>
            isArea(entry) ? (
              <button
                key={entry.to}
                type="button"
                className={inAdmin ? "nav-item active" : "nav-item"}
                aria-expanded={view === "admin"}
                aria-controls="nav"
                title={entry.label}
                onClick={() => setView(view === "admin" ? "main" : "admin")}
              >
                <Icon name={entry.icon} size={17} />
                <span className="lbl">{entry.label}</span>
                {/* NO DOT HERE: the rail has one mark, and on an Admin page it is
                    the page's own, in the panel. This keeps the lifted ground. */}
              </button>
            ) : isFolder(entry) ? null : (
              <RailLink key={entry.to} item={entry} lit={entry.to === activeTo} count={badges[entry.to]} />
            ),
          )}
        </nav>
      ) : null}

      {/* IDENTITY, NOT A CONTROL: the one place the tenant is named. */}
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
