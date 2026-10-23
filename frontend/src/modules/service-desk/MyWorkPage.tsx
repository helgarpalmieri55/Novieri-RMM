import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useDirectoryUsers, useTicketQueues, useTickets } from "./api";
import { userPickerOptions } from "./pickerOptions";
import { ServiceDeskNav } from "./ServiceDeskNav";
import { TicketTable } from "./TicketTable";
import { PageHead, Panel, QueryState, StatCard, StatsGrid } from "../../shell/components";

const URGENT_PRIORITIES = new Set(["high", "urgent"]);

// Spec §11's "My work": the caller's open tickets across every queue, the
// same set as the list's "My tickets" scope, soonest deadline first.
export function MyWorkPage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("ticket.read");
  const canAdmin = permissions.includes("ticket.admin");

  const tickets = useTickets({ scope: "mine", sort: "due", limit: 200 });
  const items = tickets.data?.items ?? [];
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
          icon="ticket"
          to={myTicketsHref}
          tone="neutral"
          zeroLabel={t("serviceDesk.myWork.zeroAssigned")}
        />
        <StatCard
          label={t("serviceDesk.common.highOrUrgentPriority")}
          value={String(urgentCount)}
          icon="alert-triangle"
          to={`${myTicketsHref}&priority=high,urgent`}
          tone="red"
          urgent
          zeroLabel={t("serviceDesk.myWork.zeroHighUrgent")}
        />
      </StatsGrid>

      <h2 className="sr-only">{t("serviceDesk.myWork.listHeading")}</h2>
      <Panel bare>
        <QueryState
          loading={tickets.isLoading}
          error={tickets.error}
          empty={tickets.data != null && items.length === 0}
          emptyIcon="ticket"
          emptyLabel={t("serviceDesk.myWork.empty")}
        >
          <TicketTable tickets={items} queueName={queueName} userOptions={userOptions} now={now} />
        </QueryState>
      </Panel>
    </div>
  );
}
