/**
 * ADMIN, AS ATERA DRAWS IT (redesign design §1.1, decision D10): the rail gives
 * way to a panel with a way back, a search and an accordion of the area's
 * sections, the one holding the current page open. The same pages and codes
 * as before; only Clients left, for the main rail. Folded, the panel is its
 * pages' icons.
 */
import { useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";

import { useT } from "../i18n/context";
import { SearchField } from "./components";
import { Icon } from "./icons";
import { activeNavTo, type NavArea } from "./nav";
import { RailLink } from "./Sidebar";

/** For matching a search: case and accents do not count ("auditoria" finds "auditoría"). */
export function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();
}

export function AdminPanel({
  area,
  pathname,
  collapsed,
  onBack,
}: {
  area: NavArea;
  pathname: string;
  collapsed: boolean;
  onBack: () => void;
}) {
  const t = useT();
  const base = useId();
  const [query, setQuery] = useState("");
  const pages = area.sections.flatMap((section) => section.items);
  const current = activeNavTo(pathname, [{ key: "main", label: "", items: pages }]);
  const holding = area.sections.find((section) => section.items.some((item) => item.to === current))?.label;
  const [openSection, setOpenSection] = useState<string | undefined>(holding ?? area.sections[0]?.label);
  // Arriving at another Admin page while the panel stays open (a search
  // result, an /admin card) opens that page's section, as Atera's panel does
  // (critique of the shell, 2026-11-24; Atera's walkthrough, line 343).
  useEffect(() => {
    if (holding !== undefined) setOpenSection(holding);
  }, [holding]);
  const wanted = fold(query);
  const sections = area.sections
    .map((section) => ({
      label: section.label,
      items: wanted === "" ? section.items : section.items.filter((item) => fold(item.label).includes(wanted)),
    }))
    .filter((section) => section.items.length > 0);

  return (
    <nav className="nav-scroll admin-panel" id="nav" aria-label={area.label}>
      <div className="admin-head">
        <button type="button" className="nav-item admin-back" onClick={onBack} title={t("shell.sidebar.backToMenu")}>
          <Icon name="chevron-left" size={16} />
          <span className="lbl">{t("shell.sidebar.backToMenu")}</span>
        </button>
        <Link
          className={pathname === area.to ? "admin-title active" : "admin-title"}
          to={area.to}
          title={area.label}
          aria-current={pathname === area.to ? "page" : undefined}
        >
          {area.label}
        </Link>
      </div>
      {collapsed ? null : (
        <div className="admin-search">
          <SearchField
            type="search"
            aria-label={t("shell.sidebar.adminSearch")}
            placeholder={t("shell.sidebar.adminSearch")}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      )}
      {sections.length === 0 ? <p className="admin-none">{t("shell.sidebar.adminNoMatch")}</p> : null}
      {sections.map((section, index) => {
        const open = collapsed || wanted !== "" || section.label === openSection;
        const id = `${base}-${index}`;
        return (
          <div key={section.label} className="admin-section">
            {collapsed ? null : (
              <button
                type="button"
                className="nav-item admin-acc"
                aria-expanded={open}
                aria-controls={id}
                onClick={() => setOpenSection(open && wanted === "" ? undefined : section.label)}
              >
                <span className="lbl">{section.label}</span>
                <span className="ct">{section.items.length}</span>
                <Icon name="chevron-down" size={14} className={open ? "chev open" : "chev"} />
              </button>
            )}
            <div id={id} hidden={!open}>
              {section.items.map((item) => (
                <RailLink key={item.to} item={item} lit={item.to === current} sub />
              ))}
            </div>
          </div>
        );
      })}
    </nav>
  );
}
