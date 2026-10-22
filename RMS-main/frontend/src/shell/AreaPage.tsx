/**
 * An area's landing page: the rail's second layer, listing every page in the
 * area this operator may open, grouped the way the rail opens them.
 *
 * Built from `visibleGroups`, so it offers exactly what the rail would and
 * never a link that answers 403. Cosmetic, like every rule in `nav.ts`: the
 * server refuses the rest.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Table, Td, Th } from "../components/ui";
import type { Me } from "../lib/api";
import { useT } from "../i18n/context";
import { useApi } from "../lib/ApiContext";
import { PageHead, Panel, QueryState } from "./components";
import { Icon } from "./icons";
import { isArea, NAV_GROUPS, navAreas, visibleGroups } from "./nav";

export function AreaPage({ to }: { to: string }) {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const area = navAreas(NAV_GROUPS).find((entry) => entry.to === to);
  const visible = visibleGroups(me.data?.permissions ?? [])
    .flatMap((group) => group.items)
    .find((entry) => entry.to === to);
  const sections = visible && isArea(visible) ? visible.sections : [];

  if (!area) return null;
  return (
    <>
      <PageHead title={area.label} description={area.description} />
      <QueryState
        loading={me.isLoading}
        error={me.error}
        empty={sections.length === 0}
        emptyLabel={t("shell.areaPage.empty")}
        emptyIcon="lock"
      >
        {sections.map((section) => (
            <Panel key={section.label} title={section.label} bare>
              <Table label={section.label}>
                <thead>
                  <tr>
                    <Th>{t("shell.areaPage.page")}</Th>
                    <Th label={t("shell.areaPage.open")} />
                  </tr>
                </thead>
                <tbody>
                  {section.items.map((item) => (
                    <tr key={item.to}>
                      <Td lead={<Icon name={item.icon} size={18} />}>
                        <div className="cell-title">
                          <Link to={item.to}>{item.label}</Link>
                        </div>
                        {item.hint ? <div className="cell-sub">{item.hint}</div> : null}
                      </Td>
                      <Td>
                        <Link className="btn small" to={item.to} aria-label={t("shell.areaPage.openPage", { label: item.label })}>
                          <Icon name="arrow-right" size={14} />
                          {t("shell.areaPage.open")}
                        </Link>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Panel>
        ))}
      </QueryState>
    </>
  );
}
