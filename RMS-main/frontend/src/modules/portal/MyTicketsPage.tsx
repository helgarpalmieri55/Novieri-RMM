import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Badge, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { Icon } from "../../shell/icons";
import { describeAge } from "../jobs/jobState";
import { usePortalTickets } from "./api";
import { PortalNav } from "./PortalNav";
import { PortalPrerequisites } from "./PortalPrerequisites";
import { portalTicketStatusLabel, portalTicketStatusVariant } from "./portalState";
import { PageHead, Panel, QueryState } from "../../shell/components";

// "My tickets" (spec §7): number, subject, status, last update. Nothing
// else -- no queue column, no assignee column, because `PortalTicketSummary`
// (types.ts) carries neither field to render in the first place.
//
// The design's page (`P['portal']`): one bare panel of rows led by the
// ticket glyph, the subject as the title, "#4127 · opened 2h ago" under it,
// the state as a pill and when it last moved in mono.
export function MyTicketsPage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("portal.ticket.read");

  const tickets = usePortalTickets();
  const items = tickets.data ?? [];
  const now = Date.now();

  return (
    <div>
      {/* v3 draws the portal's one call to action as a primary button in the
          page head (`P['portal']`), and this page offered it only as the
          second tab -- so the thing the portal exists for had no primary
          weight anywhere on the screen. It keeps the prototype's own words,
          "New request": the tab beside it already says "New ticket" and points
          at the same route, and two controls reading identically one line
          apart is a page that looks like it has repeated itself. The design
          file is the authority on the wording either way.

          Hidden with the nav when the reader cannot read the portal, for the
          same reason the nav is: the server is the authority either way
          (invariant 6); this is only what is worth offering. */}
      <PageHead
        title={t("portal.myTickets.title")}
        description={t("portal.myTickets.description")}
        actions={
          canRead ? (
            <Link className="btn primary" to="/portal/new">
              <Icon name="plus" size={15} />
              <span>{t("portal.myTickets.newRequest")}</span>
            </Link>
          ) : undefined
        }
      />
      <PortalNav canRead={canRead} />
      <PortalPrerequisites />

      <Panel bare>
        <QueryState
          loading={tickets.isLoading}
          error={tickets.error}
          empty={items.length === 0}
          emptyLabel={t("portal.myTickets.empty")}
          emptyIcon="ticket"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("portal.myTickets.ticket")}</Th>
                <Th>{t("portal.myTickets.status")}</Th>
                <Th>{t("portal.myTickets.lastUpdate")}</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((ticket) => {
                const href = `/portal/tickets/${ticket.id}`;
                return (
                  <tr key={ticket.id}>
                    <Td lead={<Icon name="ticket" size={18} />}>
                      <div className="cell-title">
                        <Link to={href}>{ticket.subject}</Link>
                      </div>
                      <div className="cell-sub">
                        <Link className="mono" to={href}>
                          {ticket.number}
                        </Link>
                        {" · "}
                        {t("portal.myTickets.opened", { age: describeAge(ticket.created_at, now) })}
                      </div>
                    </Td>
                    <Td>
                      <Badge variant={portalTicketStatusVariant(ticket.status)}>
                        {portalTicketStatusLabel(ticket.status)}
                      </Badge>
                    </Td>
                    <Td>
                      <span className="mono">{formatDateTime(ticket.updated_at, me.data?.timezone)}</span>
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
