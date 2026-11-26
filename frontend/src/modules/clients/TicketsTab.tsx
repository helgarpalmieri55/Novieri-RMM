/**
 * The client's open tickets (design §1.3), the first 25 by the queue's own
 * order, and the way to all of them: the ticket list with its Client chip set.
 * `client_id` narrows the read on the server; `useNarrowedApi` narrows it again with
 * `X-Client-Id` (decision D6), and `enabled: ready` waits until that is decided.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { ListEmpty, Panel, QueryState, StatusPill, useNarrowedApi, withClient } from "../../shell/components";

export function TicketsTab({ client, enabled }: { client: ClientOut; enabled: boolean }) {
  const t = useT();
  const { api, client: sent, ready } = useNarrowedApi(client.id);
  const { clients } = useClientScope();
  // TicketListPage's own create gate (`ticket.write`), in THIS client. Cosmetic (invariant 6).
  const canWrite = clients.find((item) => item.id === client.id)?.permissions.includes("ticket.write") ?? false;
  const tickets = useQuery({
    queryKey: ["client360", client.id, "tickets", sent],
    queryFn: () => api.listTickets({ client_id: client.id, scope: "open", limit: 25 }),
    enabled: enabled && ready,
  });
  const rows = tickets.data?.items ?? [];
  return (
    <Panel
      title={t("clients.tickets.title")}
      bare
      actions={
        <Link className="btn small" to={withClient("/service-desk", client.id)}>
          {t("clients.tickets.seeAll")}
        </Link>
      }
    >
      <QueryState loading={!enabled || !ready || tickets.isLoading} error={tickets.error} empty={false} emptyLabel="">
        {rows.length === 0 ? (
          <ListEmpty
            filtered={false}
            onClear={() => {}}
            nothingYet={t("clients.tickets.empty")}
            icon="ticket"
            action={
              canWrite ? (
                <Link className="btn small" to="/service-desk?new=ticket">
                  {t("clients.tickets.add")}
                </Link>
              ) : undefined
            }
          />
        ) : (
          <Table label={t("clients.tickets.title")}>
            <thead>
              <tr>
                <Th>{t("clients.tickets.colTicket")}</Th>
                <Th>{t("clients.tickets.colPriority")}</Th>
                <Th>{t("clients.tickets.colStatus")}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((ticket) => (
                <tr key={ticket.id}>
                  <Td>
                    <div className="cell-title">
                      <Link to={`/service-desk/tickets/${ticket.id}`}>{ticket.subject}</Link>
                    </div>
                    <div className="cell-sub mono">{ticket.number}</div>
                  </Td>
                  <Td>
                    <StatusPill value={ticket.priority} />
                  </Td>
                  <Td>
                    <StatusPill value={ticket.status} />
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
