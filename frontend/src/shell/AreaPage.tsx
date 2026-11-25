/**
 * An area's landing page (`/admin`, the Admin index): every page in the area
 * this operator may open, one card per section the way the Admin panel opens
 * them (redesign design §1.1, decision D10), with the panel's search, kept in
 * the URL (`?q=`).
 *
 * Built from `visibleGroups`, so it offers exactly what the rail would and
 * never a link that answers 403. Cosmetic, like every rule in `nav.ts`: the
 * server refuses the rest.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import type { Me } from "../lib/api";
import { useT } from "../i18n/context";
import { useApi } from "../lib/ApiContext";
import { useEnabledModules } from "../lib/clientScope";
import { ListEmpty, PageHead, Panel, QueryState, SearchField, useUrlFilters } from "./components";
import { Icon } from "./icons";
import { isArea, NAV_GROUPS, navAreas, sectionsMatching, visibleGroups } from "./nav";

export function AreaPage({ to }: { to: string }) {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const area = navAreas(NAV_GROUPS).find((entry) => entry.to === to);
  const modules = useEnabledModules();
  const visible = visibleGroups(me.data?.permissions ?? [], modules)
    .flatMap((group) => group.items)
    .find((entry) => "to" in entry && entry.to === to);
  const sections = visible && isArea(visible) ? visible.sections : [];

  // THE SEARCH IS IN THE URL (`?q=`, replacing the history entry as every
  // list's does): "Todo con URL" (Task 4 review, fix round 1). The panel's own
  // search in the rail stays local: it is chrome, not the page.
  const filters = useUrlFilters(["q"] as const);
  const shown = sectionsMatching(sections, filters.values.q);

  if (!area) return null;
  return (
    <>
      <PageHead title={area.label} description={area.description} />
      <QueryState loading={me.isLoading} error={me.error} empty={sections.length === 0} emptyLabel={t("shell.areaPage.empty")} emptyIcon="lock">
        {/* THE FIELD ON ITS OWN, above the cards: no panel strip around one
            control (the coordinator's answer to the critique's question 1). */}
        <div className="admin-index-search">
          <SearchField
            type="search"
            aria-label={t("shell.sidebar.adminSearch")}
            placeholder={t("shell.sidebar.adminSearch")}
            value={filters.values.q}
            onChange={(event) => filters.set("q", event.target.value)}
          />
        </div>
        {shown.length === 0 ? (
          <ListEmpty filtered onClear={filters.clear} nothingYet={t("shell.areaPage.empty")} />
        ) : (
          <div className="admin-index">
            {shown.map((section) => (
              <Panel key={section.label} title={section.label}>
                <ul className="admin-index-list">
                  {section.items.map((item) => (
                    <li key={item.to}>
                      <Icon name={item.icon} size={18} />
                      <div>
                        <Link to={item.to}>{item.label}</Link>
                        {item.hint ? <div className="cell-sub">{item.hint}</div> : null}
                      </div>
                    </li>
                  ))}
                </ul>
              </Panel>
            ))}
          </div>
        )}
      </QueryState>
    </>
  );
}
