import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { Table, Td, Th } from "../../components/ui";
import type { Me } from "../../lib/api";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import { formatDate, intlLocale } from "../../lib/datetime";
import { useClientName } from "../settings/api";
import { useProducts, useRenewals } from "./api";
import { LicensePageNav } from "./licenseNav";
import { dueKind } from "./licenseState";
import {
  ClientFilter,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  ShowingCount,
  StatusPill,
  useUrlFilters,
} from "../../shell/components";
import type { RenewalRow } from "./types";

/**
 * The month a due date falls in, as its key ("2026-08") and its heading
 * ("August 2026", "agosto de 2026"). `due_date` is a calendar day pinned to
 * UTC (see the row below), so the month is read in UTC too: a day never moves
 * into the month before it.
 */
function monthOf(day: string): { key: string; label: string } {
  const at = new Date(`${day.slice(0, 10)}T00:00:00Z`);
  return {
    key: day.slice(0, 7),
    label: new Intl.DateTimeFormat(intlLocale(), { timeZone: "UTC", year: "numeric", month: "long" }).format(at),
  };
}

/** The rows by month, the months in calendar order and each month's rows as the server ordered them. */
function byMonth(rows: readonly RenewalRow[]): { key: string; label: string; rows: RenewalRow[] }[] {
  const groups = new Map<string, { key: string; label: string; rows: RenewalRow[] }>();
  for (const row of rows) {
    const month = monthOf(row.due_date);
    const group = groups.get(month.key) ?? { ...month, rows: [] };
    group.rows.push(row);
    groups.set(month.key, group);
  }
  return [...groups.values()].sort((a, b) => a.key.localeCompare(b.key));
}
import { Icon } from "../../shell/icons";
import { useRecordTitle } from "../../shell/title";

export function RenewalCalendar() {
  const t = useT();
  // A sub-page without a rail entry names itself in the tab, as its h1 does
  // (the coordinator's ruling 1b): "<this page> · <rail entry> · Novieri RMM".
  useRecordTitle(t("licenses.renewals.title"));
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRead = (me.data?.permissions ?? []).includes("licenses.read");

  // THE CLIENT CHIP (decision D6): the register's renewal tile links here
  // with its client (`?client=`), so the calendar opens exactly the renewals
  // the tile counted, and a person with every client shown can narrow it.
  const filters = useUrlFilters(["client"] as const);
  const renewals = useRenewals(undefined, filters.values.client);
  const products = useProducts();
  const clientName = useClientName();
  // WHOSE EACH ROW IS, with every client chosen (design §3.4); with one
  // chosen every row is that client's, and the column would only repeat it.
  const { selected } = useClientScope();
  const showClient = selected === null;
  const productNameById = useMemo(
    () => new Map((products.data ?? []).map((p) => [p.id, p.name])),
    [products.data],
  );
  const items = renewals.data ?? [];

  return (
    <div>
      <PageHead
        title={t("licenses.renewals.title")}
        description={t("licenses.renewals.description")}
      />

      <LicensePageNav canRead={canRead} />

      {/* THE CALENDAR, BY MONTH (Task 10, 5.1): one table, a heading row
          for each month, so a quarter reads as three blocks instead of one
          long list. Nothing to show is a sentence with its cause. */}
      <Panel bare>
        <ListToolbar
          chips={<ClientFilter filters={filters} />}
          end={renewals.data ? <ShowingCount shown={items.length} total={items.length} /> : null}
        />
        <QueryState loading={renewals.isPending} error={renewals.error} empty={false} emptyLabel="">
          {items.length === 0 ? (
            <ListEmpty
              filtered={filters.active}
              onClear={filters.clear}
              nothingYet={t("licenses.renewals.empty")}
              icon="calendar-event"
            />
          ) : (
          <Table label={t("licenses.renewals.title")}>
            <thead>
              <tr>
                <Th>{t("licenses.renewals.colProduct")}</Th>
                {showClient && <Th>{t("licenses.renewals.colClient")}</Th>}
                <Th>{t("licenses.renewals.colWhat")}</Th>
                <Th>{t("licenses.renewals.colDate")}</Th>
                <Th>{t("licenses.renewals.colIn")}</Th>
              </tr>
            </thead>
            {byMonth(items).map((month) => (
              <tbody key={month.key}>
                <tr>
                  <th scope="rowgroup" colSpan={showClient ? 5 : 4}>
                    {month.label}
                  </th>
                </tr>
                {month.rows.map((row) => {
                  const kind = dueKind(row.due_in_days);
                  return (
                    <tr key={`${row.license_id}-${row.trigger}`}>
                      <Td lead={<Icon name="calendar-event" size={18} />} gutter={kind}>
                        <div className="cell-title">
                          {productNameById.get(row.product_id) ?? row.product_id}
                        </div>
                      </Td>
                      {showClient && <Td>{clientName(row.client_id)}</Td>}
                      <Td>{row.trigger === "maintenance" ? t("licenses.renewals.maintenance") : t("licenses.renewals.renewal")}</Td>
                      {/* `due_date` is a `date` in schema.py (reporting's
                          renewal/maintenance trigger day), so it is pinned to
                          UTC: rendered through a zone west of UTC it would show
                          the day before the one the report computed, and
                          `due_in_days` beside it would then contradict it. */}
                      <Td>{formatDate(row.due_date, "UTC")}</Td>
                      <Td>
                        <StatusPill
                          value={kind === "crit" ? "overdue" : kind === "warn" ? "due" : "scheduled"}
                          kind={kind}
                          label={
                            row.due_in_days < 0
                              ? t("licenses.renewals.overdue", { count: Math.abs(row.due_in_days) })
                              : t("licenses.renewals.inDays", { count: row.due_in_days })
                          }
                        />
                      </Td>
                    </tr>
                  );
                })}
              </tbody>
            ))}
          </Table>
          )}
        </QueryState>
      </Panel>
    </div>
  );
}
