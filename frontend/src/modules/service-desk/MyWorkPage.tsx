import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useDirectoryUsers, useTicketQueues, useTickets } from "./api";
import { userPickerOptions } from "./pickerOptions";
import { ServiceDeskNav } from "./ServiceDeskNav";
import { TicketTable } from "./TicketTable";
import { ListEmpty, ListToolbar, PageHead, Panel, QueryState, ShowingCount, StatCard, StatsGrid } from "../../shell/components";
import { useRecordTitle } from "../../shell/title";

const URGENT_PRIORITIES = new Set(["high", "urgent"]);

// Spec §11's "My work": the caller's open tickets across every queue, the
// same set as the list's "My tickets" scope, soonest deadline first.
//
// A LIST WITH NO FILTER (redesign plan, Task 7, 2.4; Receta L, 8): the
// section navigation at the top, the two tiles, then the list with how many
// it shows of the server's total, and an empty list that says so and leads
// to the queue. No Client chip: these are the person's own tickets, from every
// client they work for, so the read is the console's own (`useTickets`
// without a chip).
export function MyWorkPage() {
  const t = useT();
  // A sub-page without a rail entry names itself in the tab, as its h1 does
  // (general ruling 1b): "<this page> · <rail entry> · Novieri RMM".
  useRecordTitle(t("serviceDesk.myWork.title"));
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("ticket.read");
  const canAdmin = permissions.includes("ticket.admin");

  const tickets = useTickets({ scope: "mine", sort: "due", limit: 200 });
  const items = tickets.data?.items ?? [];
  // The tiles' one condition (ruling 10a): no tickets read, pending or
  // failed. StatCard's `pending` draws the kit's absent mark, never a 0 and
  // its "nothing assigned" sentence over a read that has not answered.
  const noTickets = tickets.data === undefined;
  const urgentCount = useMemo(
    () => items.filter((t) => URGENT_PRIORITIES.has(t.priority)).length,
    [items],
  );

  const queues = useTicketQueues();
  const queueName = (id: string) => queues.data?.find((q) => q.id === id)?.name ?? id;
  const users = useDirectoryUsers();
  // Not memoised: the sublabels are worded, and a memo would keep them in the
  // language the page opened in.
  const userOptions = userPickerOptions(users.data ?? []);

  // The same tickets in the list, where they can be filtered further.
  const myTicketsHref = "/service-desk?scope=mine";

  const now = Date.now();

  return (
    <div>
      <PageHead title={t("serviceDesk.myWork.title")} description={t("serviceDesk.myWork.description")} />
      <ServiceDeskNav canRead={canRead} canAdmin={canAdmin} />

      <StatsGrid columns={2}>
        <StatCard
          label={t("serviceDesk.myWork.assignedToYou")}
          value={String(items.length)}
          pending={noTickets}
          icon="ticket"
          to={myTicketsHref}
          tone="neutral"
          zeroLabel={t("serviceDesk.myWork.zeroAssigned")}
        />
        <StatCard
          label={t("serviceDesk.common.highOrUrgentPriority")}
          value={String(urgentCount)}
          pending={noTickets}
          icon="alert-triangle"
          to={`${myTicketsHref}&priority=high,urgent`}
          tone="red"
          urgent
          zeroLabel={t("serviceDesk.myWork.zeroHighUrgent")}
        />
      </StatsGrid>

      <h2 className="sr-only">{t("serviceDesk.myWork.listHeading")}</h2>
      <Panel bare>
        <ListToolbar
          // No count before the list has answered: "Showing 0" would be an invented zero.
          end={tickets.data ? <ShowingCount shown={items.length} total={tickets.data.total} /> : null}
        />
        <QueryState loading={tickets.isLoading} error={tickets.error} empty={false} emptyLabel="">
          {items.length === 0 ? (
            <ListEmpty
              filtered={false}
              onClear={() => {}}
              nothingYet={t("serviceDesk.myWork.empty")}
              icon="ticket"
              action={
                <Link className="btn small" to="/service-desk">
                  {t("serviceDesk.myWork.toQueue")}
                </Link>
              }
            />
          ) : (
            <TicketTable
              tickets={items}
              queueName={queueName}
              userOptions={userOptions}
              now={now}
              label={t("serviceDesk.myWork.listHeading")}
            />
          )}
        </QueryState>
      </Panel>
    </div>
  );
}
