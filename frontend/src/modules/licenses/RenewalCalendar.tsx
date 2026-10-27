import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { Table, Td, Th } from "../../components/ui";
import type { Me } from "../../lib/api";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { formatDate } from "../../lib/datetime";
import { useClientName } from "../settings/api";
import { useProducts, useRenewals } from "./api";
import { LicensePageNav } from "./licenseNav";
import { dueKind } from "./licenseState";
import { PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";

export function RenewalCalendar() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRead = (me.data?.permissions ?? []).includes("licenses.read");

  const renewals = useRenewals();
  const products = useProducts();
  const clientName = useClientName();
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

      <Panel bare>
        <QueryState
          loading={renewals.isLoading}
          error={renewals.error}
          empty={items.length === 0}
          emptyLabel={t("licenses.renewals.empty")}
          emptyIcon="clock"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("licenses.renewals.colProduct")}</Th>
                <Th>{t("licenses.renewals.colClient")}</Th>
                <Th>{t("licenses.renewals.colWhat")}</Th>
                <Th>{t("licenses.renewals.colDate")}</Th>
                <Th>{t("licenses.renewals.colIn")}</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => {
                const kind = dueKind(row.due_in_days);
                return (
                  <tr key={`${row.license_id}-${row.trigger}`}>
                    <Td lead={<Icon name="calendar-event" size={18} />} gutter={kind}>
                      <div className="cell-title">
                        {productNameById.get(row.product_id) ?? row.product_id}
                      </div>
                    </Td>
                    <Td>{clientName(row.client_id)}</Td>
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
          </Table>
        </QueryState>
      </Panel>
    </div>
  );
}
