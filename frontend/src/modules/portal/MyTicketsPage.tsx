import { useContext, type ComponentProps } from "react";
import { Link } from "react-router-dom";
import { Badge, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { formatDateTime } from "../../lib/datetime";
import { Icon } from "../../shell/icons";
import { describeAge } from "../jobs/jobState";
import { usePortalTickets } from "./api";
import { usePortalMe } from "./usePortalMe";
import { PortalNav } from "./PortalNav";
import { PortalPrerequisites } from "./PortalPrerequisites";
import { PortalTitle } from "./portalTitle";
import { ContactFrameContext } from "./usePortalMe";
import { useMePermissions } from "../../lib/mePermissions";
import { isPortalOnly } from "../../shell/nav";
import { isPortalTicketOpen, portalTicketStatusLabel, portalTicketStatusVariant } from "./portalState";
import {
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  ShowingCount,
  useUrlFilters,
} from "../../shell/components";

/**
 * `GET /portal/tickets` answers its default page when it is not asked for
 * one (`limit=50`, `backend/app/modules/portal/api.py`), and says nothing
 * about how many there are in all. A full page may not be all of them, so
 * "of how many" is said only for a page that is not full (The Absent Zero
 * Rule: a total nobody measured is never drawn).
 */
const PORTAL_LIST_PAGE = 50;

// "My tickets" (spec §7): number, subject, status, last update. Nothing
// else -- no queue column, no assignee column, because `PortalTicketSummary`
// (types.ts) carries neither field to render in the first place.
//
// ON THE LIST KIT (redesign plan, Task 7, 2.9; Receta L): the state chips live
// in the URL (`?state=`: Open writes nothing, All writes `state=all`), the
// toolbar says how many are shown, and an empty list says its real cause.
// No Client chip: the portal is one contact's, and a contact has one client.
//
// THE SERVER DOES NOT FILTER BY STATE: the route lists every ticket of the
// requester, newest first, so the chip narrows the rows it answered. The
// chips carry no number for the same reason (The Absent Zero Rule: the
// server sends no count of each).
export function MyTicketsPage() {
  const t = useT();
  const me = usePortalMe();
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("portal.ticket.read");
  const canWrite = permissions.includes("portal.ticket.write");
  const filters = useUrlFilters(["state"] as const);
  const showAll = filters.values.state === "all";

  const tickets = usePortalTickets();
  const all = tickets.data ?? [];
  const items = showAll ? all : all.filter((ticket) => isPortalTicketOpen(ticket.status));
  const now = Date.now();
  // Filing one needs `portal.ticket.write`; offering it is a courtesy, the
  // server checks the code (invariant 6).
  const newTicket = canWrite ? (
    <Link className="btn small" to="/portal/new">
      <Icon name="plus" size={14} />
      <span>{t("portal.nav.newTicket")}</span>
    </Link>
  ) : undefined;

  return (
    <div>
      <PortalTitle page={t("shell.nav.item.myTickets")} />
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
      <FramedHead
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
        <ListToolbar
          chips={
            <FilterChips
              label={t("portal.myTickets.stateLabel")}
              options={[
                { value: "", label: t("portal.myTickets.stateOpen") },
                { value: "all", label: t("portal.myTickets.stateAll") },
              ]}
              value={showAll ? "all" : ""}
              onChange={(value) => filters.set("state", value)}
            />
          }
          // No count before the list has answered: "Showing 0" would be an invented zero.
          end={
            tickets.data ? (
              // Of how many MATCH the chip: all of them are on screen when the
              // page is not full; a full page may hide more, so it says none.
              <ShowingCount shown={items.length} total={all.length < PORTAL_LIST_PAGE ? items.length : null} />
            ) : null
          }
        />
        <QueryState loading={tickets.isLoading} error={tickets.error} empty={false} emptyLabel="">
          {items.length === 0 ? (
            // NOTHING YET, OR NOTHING OPEN. A requester whose tickets are all
            // closed has not "filed none": Open is the filter hiding them,
            // and its way out shows them all.
            <ListEmpty
              filtered={all.length > 0}
              onClear={() => filters.set("state", "all")}
              nothingYet={t("portal.myTickets.empty")}
              icon="ticket"
              action={newTicket}
            />
          ) : (
            <Table label={t("shell.nav.item.myTickets")}>
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
          )}
        </QueryState>
      </Panel>
    </div>
  );
}

/**
 * THE PAGE IS NAMED BY THE FRAME IT IS DRAWN IN (general ruling 1a; fix round
 * 2). In the contact's own portal frame it is the portal's "My tickets". In
 * the console it is the console rail's entry for `/portal`, the label the
 * shell also gives the tab: "End-user portal" for an operator (Admin), and
 * "My tickets" for an account that holds only portal codes, whose console
 * rail is the portal's (`isPortalOnly`, the same test `App.tsx` makes). The
 * frame is known from `ContactFrameContext`; the console's `/me` is read only
 * outside the contact frame, where a contact may not ask for it.
 */
type HeadProps = Omit<ComponentProps<typeof PageHead>, "title">;

function FramedHead(props: HeadProps) {
  const contactFrame = useContext(ContactFrameContext);
  return contactFrame ? <ContactHead {...props} /> : <ConsoleHead {...props} />;
}

function ContactHead(props: HeadProps) {
  const t = useT();
  return <PageHead title={t("shell.nav.item.myTickets")} {...props} />;
}

function ConsoleHead(props: HeadProps) {
  const t = useT();
  const me = useMePermissions();
  const portalOnly = me.status === "ready" && isPortalOnly(me.codes);
  return <PageHead title={t(portalOnly ? "shell.nav.item.myTickets" : "shell.nav.item.endUserPortal")} {...props} />;
}
