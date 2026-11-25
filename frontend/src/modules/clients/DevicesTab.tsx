/**
 * The client's devices (design §1.3), the first 25 by the fleet list's own
 * order, and the way to all of them: the fleet list with its Client chip set.
 * Read only through `useNarrowedApi` (decision D6), with `enabled: ready`.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { NoValue } from "../../components/emptystate";
import { RelTime } from "../../components/reltime";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { ListEmpty, Panel, QueryState, StatusPill, useNarrowedApi, withClient } from "../../shell/components";
import { OsIcon } from "../../shell/viz";

export function DevicesTab({ client, enabled }: { client: ClientOut; enabled: boolean }) {
  const t = useT();
  const { api, client: sent, ready } = useNarrowedApi(client.id);
  const devices = useQuery({
    queryKey: ["client360", client.id, "devices", sent],
    queryFn: () => api.listDevices({ clientId: client.id, limit: 25 }),
    enabled: enabled && ready,
  });
  const rows = devices.data?.items ?? [];
  return (
    <Panel
      title={t("clients.devices.title")}
      bare
      actions={
        <Link className="btn small" to={withClient("/devices", client.id)}>
          {/* The server's total once it has answered; never an invented 0 before (The Absent Zero Rule). */}
          {devices.data ? t("clients.devices.seeAll", { count: devices.data.total }) : t("clients.devices.seeAllUncounted")}
        </Link>
      }
    >
      <QueryState loading={!enabled || !ready || devices.isLoading} error={devices.error} empty={false} emptyLabel="">
        {rows.length === 0 ? (
          <ListEmpty
            filtered={false}
            onClear={() => {}}
            nothingYet={t("clients.devices.empty")}
            icon="devices"
            action={
              <Link className="btn small" to="/devices/onboard">
                {t("clients.devices.add")}
              </Link>
            }
          />
        ) : (
          <Table label={t("clients.devices.title")}>
            <thead>
              <tr>
                <Th>{t("clients.devices.colComputer")}</Th>
                <Th>{t("clients.devices.colAgent")}</Th>
                <Th>{t("clients.devices.colLastSeen")}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((device) => (
                <tr key={device.id}>
                  <Td lead={<OsIcon os={device.os_family ?? ""} />}>
                    <div className="cell-title">
                      <Link to={`/devices/${device.id}`}>{device.hostname ?? device.fqdn ?? device.id}</Link>
                    </div>
                    <div className="cell-sub">{device.os_version}</div>
                  </Td>
                  <Td>{device.agent_status ? <StatusPill value={device.agent_status} /> : <NoValue label={t("clients.devices.noAgent")} />}</Td>
                  <Td>{device.last_seen_at ? <RelTime iso={device.last_seen_at} /> : <NoValue label={t("clients.devices.never")} />}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </QueryState>
    </Panel>
  );
}
