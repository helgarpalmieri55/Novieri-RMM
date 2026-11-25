import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatCount } from "../../lib/format";
import { useDirectoryUsers, useTicketQueues, useTickets } from "./api";
import { userPickerOptions } from "./pickerOptions";
import { NewTicketDialog } from "./NewTicketDialog";
import { ServiceDeskNav } from "./ServiceDeskNav";
import { TicketReadiness } from "./TicketReadiness";
import {
  ALL_STATUSES,
  ticketPriorityLabel,
  ticketStatusLabel,
  ticketTypeLabel,
} from "./serviceDeskState";
import { TicketTable } from "./TicketTable";
import type { TicketOut, TicketScope, TicketSort } from "./types";
import {
  Btn,
  ChipSelect,
  ClientFilter,
  FilterBar,
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  SearchField,
  ShowingCount,
  StatCard,
  StatsGrid,
  useNewParam,
  useUrlFilters,
  withClient,
  writeQuery,
} from "../../shell/components";
import { intlLocale } from "../../lib/datetime";

// The two priorities a technician picks work from first.
const URGENT_PRIORITIES = new Set(["high", "urgent"]);

const TYPE_FILTERS = ["", "incident", "request"] as const;

// "High or urgent" is the set the red tile counts; the server filters one
// priority at a time, so the pair is narrowed on the page.
// Each label is a catalogue key, worded at render so a language switch shows.
const PRIORITY_FILTERS: ReadonlyArray<{ value: string; label: MessageKey | null }> = [
  { value: "", label: null },
  { value: "high,urgent", label: "serviceDesk.list.highOrUrgent" },
  { value: "low", label: null },
  { value: "normal", label: null },
  { value: "high", label: null },
  { value: "urgent", label: null },
];

// `origin` is not a list filter on the server, so it narrows the page.
const ORIGIN_FILTERS: ReadonlyArray<{ value: string; label: MessageKey }> = [
  { value: "", label: "serviceDesk.list.anyOrigin" },
  { value: "alert", label: "serviceDesk.list.originAlert" },
  { value: "technician", label: "serviceDesk.list.originTechnician" },
  { value: "portal", label: "serviceDesk.list.originPortal" },
  { value: "email", label: "serviceDesk.list.originEmail" },
  { value: "integration", label: "serviceDesk.list.originIntegration" },
];

/** Which tickets the queue shows. Every scope but "All" is open tickets only. */
const SCOPES: ReadonlyArray<{ value: TicketScope; label: MessageKey }> = [
  { value: "mine", label: "serviceDesk.list.scopeMine" },
  { value: "unassigned", label: "serviceDesk.list.scopeUnassigned" },
  { value: "open", label: "serviceDesk.list.scopeOpen" },
  { value: "all", label: "serviceDesk.list.scopeAll" },
];
const DEFAULT_SCOPE: TicketScope = "open";

const SORTS: ReadonlyArray<{ value: TicketSort; label: MessageKey }> = [
  { value: "due", label: "serviceDesk.list.sortDue" },
  { value: "newest", label: "serviceDesk.list.sortNewest" },
];
const DEFAULT_SORT: TicketSort = "due";

/** The URL keys of this list, unchanged by the redesign (Task 7, 2.2), and the Client chip's. */
const FILTER_KEYS = ["q", "scope", "status", "queue", "priority", "type", "origin", "assignee", "sort", "client"] as const;

// The route's largest page.
const MAX_LIST_LIMIT = 200;

function parseScope(value: string | null): TicketScope {
  return SCOPES.find((s) => s.value === value)?.value ?? DEFAULT_SCOPE;
}

function parseSort(value: string | null): TicketSort {
  return SORTS.find((s) => s.value === value)?.value ?? DEFAULT_SORT;
}

// A count of tickets of one type or priority, worded for the line under a tile.
// Known values have a plural of their own (a priority agrees with its count);
// one the console has not met falls back to a sentence with its label in it.
const FOOT_TYPE: Record<string, MessageKey> = {
  incident: "serviceDesk.list.footIncident",
  request: "serviceDesk.list.footRequest",
};
const FOOT_PRIORITY: Record<string, MessageKey> = {
  low: "serviceDesk.list.footLow",
  normal: "serviceDesk.list.footNormal",
  high: "serviceDesk.list.footHigh",
  urgent: "serviceDesk.list.footUrgent",
};

export function TicketListPage() {
  const t = useT();
  const api = useApi();
  // New → Ticket (redesign decision D8): `?new=ticket` opens New ticket.
  const created = useNewParam("ticket");

  // Every filter lives in the URL, so a tile, a pasted link or Back can carry
  // it (the kit's `useUrlFilters`; the keys are the ones this list always
  // had). An empty value is absent from the URL; typing replaces history.
  const filters = useUrlFilters(FILTER_KEYS);
  const [params, setParams] = useSearchParams();
  const scope = parseScope(filters.values.scope || null);
  const sort = parseSort(filters.values.sort || null);
  const typeFilter = filters.values.type;
  const statusFilter = filters.values.status;
  const queueFilter = filters.values.queue;
  const assigneeFilter = filters.values.assignee;
  const priorityFilter = filters.values.priority;
  const originFilter = filters.values.origin;
  const searchFilter = filters.values.q;
  // The Client chip only narrows the read (decision D20): it never changes
  // the console's client, and it never changes where a write goes.
  const clientChip = filters.values.client;
  // "Nothing matches these filters" is said of a filter, never of the sort.
  const filtered = FILTER_KEYS.some((key) => key !== "sort" && filters.values[key] !== "");

  /** "Clear filters" drops every filter and keeps the sort, which is not one (fix round 1). */
  function clearFilters() {
    update(Object.fromEntries(FILTER_KEYS.filter((key) => key !== "sort").map((key) => [key, ""])));
  }

  /** Several keys in one history entry (one Back undoes the change); only when the query changes. */
  function update(patch: Record<string, string>) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      if (value === "") next.delete(key);
      else next.set(key, value);
    }
    writeQuery(params, next, setParams);
  }

  // THE SCOPE IS NOT AN ORDINARY FILTER (pre-flight review, W-M4). Without
  // `?scope=` the queue shows the open tickets (`DEFAULT_SCOPE`), so "All
  // open" is the chip that writes nothing and "All" writes `scope=all`. "Mine"
  // and "Unassigned" already say who the ticket belongs to, so they clear a
  // picked assignee; picking one widens those two to "All open".
  const setScope = (value: TicketScope) =>
    update({
      scope: value === DEFAULT_SCOPE ? "" : value,
      ...(value === "mine" || value === "unassigned" ? { assignee: "" } : {}),
    });
  const setAssigneeFilter = (value: string) =>
    update({
      assignee: value,
      ...(value !== "" && (scope === "mine" || scope === "unassigned") ? { scope: "" } : {}),
    });

  const priorityValues = priorityFilter === "" ? [] : priorityFilter.split(",");
  const serverPriority = priorityValues.length === 1 ? priorityValues[0] : undefined;

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("ticket.read");
  const canAdmin = permissions.includes("ticket.admin");
  const canWrite = permissions.includes("ticket.write");
  const myUserId = me.data?.user_id;

  const queues = useTicketQueues();
  const queueName = (id: string) => queues.data?.find((q) => q.id === id)?.name ?? id;

  const users = useDirectoryUsers();
  // Not memoised: the sublabels are worded, and a memo would keep them in the
  // language the page opened in.
  const userOptions = userPickerOptions(users.data ?? []);

  const tickets = useTickets(
    {
      type: typeFilter || undefined,
      status: statusFilter || undefined,
      queue_id: queueFilter || undefined,
      priority: serverPriority,
      q: searchFilter || undefined,
      assigned_to: assigneeFilter || undefined,
      scope,
      sort,
      limit: MAX_LIST_LIMIT,
    },
    { chip: clientChip },
  );

  const visibleTickets = useMemo((): TicketOut[] => {
    const wanted = priorityFilter.includes(",") ? priorityFilter.split(",") : [];
    return (tickets.data?.items ?? []).filter(
      (t) =>
        (wanted.length === 0 || wanted.includes(t.priority)) &&
        (originFilter === "" || t.origin === originFilter),
    );
  }, [tickets.data, priorityFilter, originFilter]);

  const dataReady = tickets.data !== undefined;
  const truncated = tickets.data != null && tickets.data.items.length < tickets.data.total;
  // "Showing x of y": the server's total of the query, unless the page narrows
  // its rows further (a pair of priorities, an origin). Then the total is
  // known only when the whole query came back, and is otherwise not drawn.
  const narrowedOnPage = priorityFilter.includes(",") || originFilter !== "";
  const shownTotal = !tickets.data
    ? null
    : !narrowedOnPage
      ? tickets.data.total
      : truncated
        ? null
        : visibleTickets.length;

  // Every figure is counted from the rows on screen.
  const stats = useMemo(
    () => ({
      total: visibleTickets.length,
      unassigned: visibleTickets.filter((t) => t.assigned_to === null).length,
      urgent: visibleTickets.filter((t) => URGENT_PRIORITIES.has(t.priority)).length,
      fromAlerts: visibleTickets.filter((t) => t.origin === "alert").length,
    }),
    [visibleTickets],
  );

  // A second dimension of the same rows under each figure, kept as DATA (counts)
  // and worded at render below, so a language switch re-words it. A line is
  // absent at zero so the tile's zero label keeps the line.
  const footData = useMemo(() => {
    const rows = visibleTickets;
    const tally = (
      subset: readonly TicketOut[],
      key: (t: TicketOut) => string,
    ): Array<[string, number]> => {
      const counts = new Map<string, number>();
      for (const t of subset) counts.set(key(t), (counts.get(key(t)) ?? 0) + 1);
      return [...counts.entries()].sort(
        (a, b) => b[1] - a[1] || a[0].localeCompare(b[0], intlLocale()),
      );
    };

    const unassigned = rows.filter((t) => t.assigned_to === null);
    const urgent = rows.filter((t) => URGENT_PRIORITIES.has(t.priority));
    const alerts = rows.filter((t) => t.origin === "alert");
    return {
      types: tally(rows, (t) => t.type),
      unassigned: unassigned.length,
      unassignedUrgent: unassigned.filter((t) => URGENT_PRIORITIES.has(t.priority)).length,
      priorities: tally(urgent, (t) => t.priority),
      alerts: alerts.length,
      alertsUnassigned: alerts.filter((t) => t.assigned_to === null).length,
    };
  }, [visibleTickets]);

  const typePart = (n: number, type: string): string => {
    const key = FOOT_TYPE[type];
    return key
      ? t(key, { count: n })
      : t("serviceDesk.list.footTypeOther", { count: n, label: ticketTypeLabel(type).toLocaleLowerCase() });
  };
  const priorityPart = (n: number, priority: string): string => {
    const key = FOOT_PRIORITY[priority];
    return key
      ? t(key, { count: n })
      : t("serviceDesk.list.footPriorityOther", { count: n, label: ticketPriorityLabel(priority).toLocaleLowerCase() });
  };
  const foot = {
    total:
      footData.types.length === 0 ? undefined : footData.types.map(([type, n]) => typePart(n, type)).join(" · "),
    unassigned:
      footData.unassigned === 0
        ? undefined
        : footData.unassignedUrgent === 0
          ? t("serviceDesk.list.noneUrgent")
          : t("serviceDesk.list.someUrgent", { count: footData.unassignedUrgent }),
    urgent:
      footData.priorities.length === 0
        ? undefined
        : footData.priorities.map(([priority, n]) => priorityPart(n, priority)).join(" · "),
    fromAlerts:
      footData.alerts === 0
        ? undefined
        : footData.alertsUnassigned === 0
          ? t("serviceDesk.list.allOwned")
          : t("serviceDesk.list.stillUnassigned", { count: footData.alertsUnassigned }),
  };

  // One clock for every relative time in the table.
  const now = Date.now();
  const newTicket = canWrite ? (
    <Btn label={t("serviceDesk.list.newTicket")} icon="plus" variant="primary" onClick={created.start} />
  ) : undefined;

  return (
    <div>
      <PageHead title={t("shell.nav.item.tickets")} description={t("serviceDesk.list.description")} actions={newTicket} />
      <ServiceDeskNav canRead={canRead} canAdmin={canAdmin} />
      <TicketReadiness />

      {/* Each tile counts rows on screen and links at the URL that shows
          exactly those rows. A TILE COUNTED ON A NARROWED READ OPENS THE SAME
          CLIENT'S ROWS (general ruling 2): with the Client chip set, the kit's
          `withClient` adds it to every tile's link. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("serviceDesk.list.shown")}
          value={String(stats.total)}
          icon="ticket"
          to={withClient("/service-desk", clientChip)}
          // Not counted before the read (general ruling 10): "n/a", never 0.
          pending={!dataReady}
          tone="neutral"
          foot={foot.total}
          zeroLabel={t("serviceDesk.list.zeroShown")}
        />
        <StatCard
          label={t("serviceDesk.common.unassigned")}
          value={String(stats.unassigned)}
          icon="help-circle"
          to={withClient("/service-desk?scope=unassigned", clientChip)}
          // Not counted before the read (general ruling 10): "n/a", never 0.
          pending={!dataReady}
          tone="gold"
          urgent
          foot={foot.unassigned}
          zeroLabel={t("serviceDesk.list.zeroUnassigned")}
        />
        <StatCard
          label={t("serviceDesk.common.highOrUrgentPriority")}
          value={String(stats.urgent)}
          icon="alert-triangle"
          to={withClient("/service-desk?priority=high,urgent", clientChip)}
          // Not counted before the read (general ruling 10): "n/a", never 0.
          pending={!dataReady}
          tone="red"
          urgent
          foot={foot.urgent}
          zeroLabel={t("serviceDesk.list.zeroHighUrgent")}
        />
        <StatCard
          label={t("serviceDesk.list.fromAlerts")}
          value={String(stats.fromAlerts)}
          icon="bell-ringing"
          to={withClient("/service-desk?origin=alert", clientChip)}
          // Not counted before the read (general ruling 10): "n/a", never 0.
          pending={!dataReady}
          tone="blue"
          foot={foot.fromAlerts}
          zeroLabel={t("serviceDesk.list.zeroFromAlerts")}
        />
      </StatsGrid>

      <h2 className="sr-only">{t("serviceDesk.list.heading")}</h2>
      <Panel bare>
        {/* THE LIST'S TOOLBAR (Receta L; Atera's "Tickets": search, then the
            filters): the search, the scope (the question an operator asks
            before any filter: "mine, or what nobody has picked up?"), the
            status (a named select) and the Client chip; the count at the end. The rest of the
            filters sit in a second row of the same panel, each with its
            name on it, and the sort on the right. */}
        <ListToolbar
          search={
            <SearchField
              placeholder={t("serviceDesk.list.searchPlaceholder")}
              aria-label={t("serviceDesk.list.searchAria")}
              maxLength={128}
              value={searchFilter}
              onChange={(e) => filters.set("q", e.target.value)}
            />
          }
          chips={
            <>
              <FilterChips
                label={t("serviceDesk.list.show")}
                options={SCOPES.map((option) => ({
                  value: option.value === DEFAULT_SCOPE ? "" : option.value,
                  label: t(option.label),
                }))}
                value={scope === DEFAULT_SCOPE ? "" : scope}
                onChange={(value) => setScope(parseScope(value || null))}
              />
              {/* THE STATUS IS A NAMED SELECT, NOT ELEVEN CHIPS (the critique of
                  this page, P1): a row of every status wrapped to three lines at
                  1440 and pushed the first ticket below the fold. Still the
                  closed vocabulary, in the first row beside the scope. */}
              <ChipSelect
                label={t("serviceDesk.list.statusAria")}
                value={statusFilter}
                onChange={(value) => filters.set("status", value)}
                options={[
                  { value: "", label: t("serviceDesk.list.anyStatus") },
                  ...ALL_STATUSES.map((status) => ({ value: status, label: ticketStatusLabel(status) })),
                ]}
              />
              <ClientFilter filters={filters} />
            </>
          }
          // No count before the list has answered: "Showing 0" would be an invented zero.
          end={dataReady ? <ShowingCount shown={visibleTickets.length} total={shownTotal} /> : null}
        />
        <FilterBar>
          <ChipSelect
            label={t("serviceDesk.list.queueAria")}
            value={queueFilter}
            onChange={(value) => filters.set("queue", value)}
            options={[
              { value: "", label: t("serviceDesk.list.allQueues") },
              ...(queues.data ?? []).map((q) => ({ value: q.id, label: q.name })),
            ]}
          />
          <ChipSelect
            label={t("serviceDesk.list.priorityAria")}
            value={priorityFilter}
            onChange={(value) => filters.set("priority", value)}
            options={PRIORITY_FILTERS.map((p) => ({
              value: p.value,
              label:
                p.value === "" ? t("serviceDesk.list.allPriorities") : p.label ? t(p.label) : ticketPriorityLabel(p.value),
            }))}
          />
          <ChipSelect
            label={t("serviceDesk.list.typeAria")}
            value={typeFilter}
            onChange={(value) => filters.set("type", value)}
            options={TYPE_FILTERS.map((type) => ({
              value: type,
              label: type === "" ? t("serviceDesk.list.allTypes") : ticketTypeLabel(type),
            }))}
          />
          <ChipSelect
            label={t("serviceDesk.list.originAria")}
            value={originFilter}
            onChange={(value) => filters.set("origin", value)}
            options={ORIGIN_FILTERS.map((o) => ({ value: o.value, label: t(o.label) }))}
          />
          <ChipSelect
            label={t("serviceDesk.list.assigneeAria")}
            value={assigneeFilter}
            onChange={setAssigneeFilter}
            options={[
              { value: "", label: t("serviceDesk.list.anyone") },
              ...userOptions.map((option) => ({ value: option.id, label: option.label })),
            ]}
          />
          <span className="filter-spacer" />
          <select
            className="select-field"
            aria-label={t("serviceDesk.list.sortAria")}
            value={sort}
            onChange={(e) => filters.set("sort", e.target.value === DEFAULT_SORT ? "" : e.target.value)}
          >
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                {t(s.label)}
              </option>
            ))}
          </select>
        </FilterBar>

        {truncated && tickets.data && (
          <p
            role="status"
            className="note"
            style={{ padding: "10px 18px", borderBottom: "1px solid var(--line)" }}
          >
            {t("serviceDesk.list.truncated", {
              shown: formatCount(tickets.data.items.length),
              total: formatCount(tickets.data.total),
            })}
          </p>
        )}

        <QueryState loading={!dataReady && !tickets.error} error={tickets.error} empty={false} emptyLabel="">
          {visibleTickets.length === 0 ? (
            // NO SECOND "NEW TICKET" HERE: the default view is the OPEN
            // tickets, so an empty queue is "nothing open", not "nothing
            // yet", and the head offers New ticket one line above.
            <ListEmpty filtered={filtered} onClear={clearFilters} nothingYet={t("serviceDesk.list.empty")} icon="ticket" />
          ) : (
            <TicketTable
              tickets={visibleTickets}
              queueName={queueName}
              userOptions={userOptions}
              now={now}
              label={t("shell.nav.item.tickets")}
            />
          )}
        </QueryState>
      </Panel>

      {/* Open only for someone who may write a ticket: the same code the
          button checks, so a bare `?new=ticket` opens nothing for a reader. */}
      <NewTicketDialog open={canWrite && created.open} onClose={created.close} myUserId={myUserId} guardNew />
    </div>
  );
}
