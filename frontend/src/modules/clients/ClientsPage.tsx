/**
 * Clients (MSP clients phase 3, design §4.1): the companies this organisation
 * serves, and its two system clients -- its own IT (`internal`) and
 * `unassigned`, which `GET /clients` lists only to a person with every
 * client. Archived clients are hidden unless asked for.
 *
 * "NEW CLIENT" NEEDS `clients.manage` ON EVERY CLIENT (`POST /clients` is a
 * global route), so it is offered on `permissions_everywhere`. Offering it is
 * a courtesy: the server checks the code on every request (invariant 6).
 */
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useEverywherePermissions } from "../../lib/mePermissions";
import { Btn, FilterBar, PageHead, Panel, QueryState, StatusPill, Toggle } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useClientList } from "./api";
import { ClientFormDialog } from "./ClientForm";
import { clientKindLabel, clientStatusKind, clientStatusLabel } from "./labels";

export function ClientsPage() {
  const t = useT();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const archived = params.get("archived") === "1";
  const everywhere = useEverywherePermissions();
  const clients = useClientList(archived);
  const [creating, setCreating] = useState(false);
  const rows = clients.data ?? [];

  function showArchived(next: boolean) {
    const query = new URLSearchParams(params);
    if (next) query.set("archived", "1");
    else query.delete("archived");
    setParams(query, { replace: true });
  }

  return (
    <>
      <PageHead
        title={t("clients.list.title")}
        description={t("clients.list.description")}
        actions={
          everywhere.has("clients.manage") ? (
            <Btn
              label={t("clients.list.newClient")}
              icon="plus"
              variant="primary"
              onClick={() => setCreating(true)}
            />
          ) : undefined
        }
      />
      <Panel bare>
        <FilterBar>
          <Toggle checked={archived} onChange={showArchived} label={t("clients.list.showArchived")} />
          <span className="filter-spacer" />
        </FilterBar>
        <QueryState
          loading={clients.isLoading}
          error={clients.error}
          empty={rows.length === 0}
          emptyLabel={t("clients.list.empty")}
          emptyIcon="building"
        >
          <Table label={t("clients.list.tableLabel")}>
            <thead>
              <tr>
                <Th>{t("clients.list.colClient")}</Th>
                <Th>{t("clients.list.colKind")}</Th>
                <Th>{t("clients.list.colDevices")}</Th>
                <Th>{t("clients.list.colSites")}</Th>
                <Th>{t("clients.list.colStatus")}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((client) => (
                <tr key={client.id}>
                  <Td lead={<Icon name="building" size={18} />}>
                    <div className="cell-title">
                      <Link to={`/clients/${client.id}`}>{client.name}</Link>
                    </div>
                    {client.email_domains.length > 0 ? (
                      <div className="cell-sub">{client.email_domains.join(", ")}</div>
                    ) : null}
                  </Td>
                  <Td>{clientKindLabel(client.kind)}</Td>
                  <Td>
                    <span className="mono">{client.device_count}</span>
                  </Td>
                  <Td>
                    <span className="mono">{client.site_count}</span>
                  </Td>
                  <Td>
                    <StatusPill
                      value={client.status}
                      kind={clientStatusKind(client.status)}
                      label={clientStatusLabel(client.status)}
                    />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>
      {/* KEYED so a refusal cannot outlive the dialog that produced it. */}
      <ClientFormDialog
        key={creating ? "open" : "closed"}
        open={creating}
        client={null}
        onClose={() => setCreating(false)}
        onSaved={(saved) => {
          setCreating(false);
          navigate(`/clients/${saved.id}`);
        }}
      />
    </>
  );
}
