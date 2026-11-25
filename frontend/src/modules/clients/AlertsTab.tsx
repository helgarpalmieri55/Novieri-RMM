/**
 * The client's ACTIVE alerts (design §1.3; decision D19). The alert list takes
 * one `state`, and "open" (active, acknowledged or muted) cannot be asked of
 * it (`modules/health/types.ts`, `AlertSummary`), so this lists the active
 * ones and says how many are open in all from the server's own count -- never
 * a total made from one page. Every read is this client's: `useNarrowedApi` narrows
 * it with `X-Client-Id` (decision D6), and `enabled: ready` waits until that is decided.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { RelTime } from "../../components/reltime";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { ListEmpty, Panel, QueryState, StatusPill, useNarrowedApi } from "../../shell/components";

export function AlertsTab({ client, enabled }: { client: ClientOut; enabled: boolean }) {
  const t = useT();
  const { api, client: sent, ready } = useNarrowedApi(client.id);
  const alerts = useQuery({
    queryKey: ["client360", client.id, "alerts", sent],
    queryFn: () => api.listHealthAlerts({ state: "active" }),
    enabled: enabled && ready,
  });
  // The same key as the 360's "Open alerts" tile, so the two read once.
  const summary = useQuery({
    queryKey: ["client360", client.id, "alertSummary", sent],
    queryFn: () => api.getHealthAlertSummary(),
    enabled: enabled && ready,
  });
  const rows = alerts.data ?? [];
  return (
    <Panel
      title={t("clients.alerts.title")}
      subtitle={summary.data ? t("clients.alerts.openInAll", { count: summary.data.open }) : undefined}
      bare
      actions={
        <Link className="btn small" to={`/health?client=${client.id}`}>
          {t("clients.alerts.seeAll")}
        </Link>
      }
    >
      <QueryState loading={!enabled || !ready || alerts.isLoading} error={alerts.error} empty={false} emptyLabel="">
        {rows.length === 0 ? (
          <ListEmpty filtered={false} onClear={() => {}} nothingYet={t("clients.alerts.empty")} icon="health" />
        ) : (
          <Table label={t("clients.alerts.title")}>
            <thead>
              <tr>
                <Th>{t("clients.alerts.colComputer")}</Th>
                <Th>{t("clients.alerts.colCheck")}</Th>
                <Th>{t("clients.alerts.colSeverity")}</Th>
                <Th>{t("clients.alerts.colOpened")}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((alert) => (
                <tr key={alert.id}>
                  <Td>
                    <Link to={`/devices/${alert.device_id}?tab=health`}>{alert.device_hostname}</Link>
                  </Td>
                  <Td>
                    <div className="cell-title">{alert.check_type}</div>
                    <div className="cell-sub mono">{alert.observed_value}</div>
                  </Td>
                  <Td>
                    <StatusPill value={alert.severity} />
                  </Td>
                  <Td>
                    <RelTime iso={alert.opened_at} />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </QueryState>
    </Panel>
  );
}
