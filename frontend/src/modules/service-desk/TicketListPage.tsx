import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
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
import { Btn, FilterBar, PageHead, Panel, QueryState, SearchField, StatCard, StatsGrid } from "../../shell/components";
import { Chips } from "../../shell/viz";
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
  const [creating, setCreating] = useState(false);

  // Every filter lives in the URL, so a tile, a pasted link or Back can carry
  // it. An empty value is absent from the URL; typing replaces history.
  const [params, setParams] = useSearchParams();
  const scope = parseScope(params.get("scope"));
  const sort = parseSort(params.get("sort"));
  const typeFilter = params.get("type") ?? "";
  const statusFilter = params.get("status") ?? "";
  const queueFilter = params.get("queue") ?? "";
  const assigneeFilter = params.get("assignee") ?? "";
  const priorityFilter = params.get("priority") ?? "";
  const originFilter = params.get("origin") ?? "";
  const searchFilter = params.get("q") ?? "";

  function update(patch: Record<string, string>, replace = false) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      if (value === "") next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace });
  }

  // "Mine" and "Unassigned" already say who the ticket belongs to, so they
  // clear a picked assignee; picking one widens those two to "All open".
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

  const tickets = useTickets({
    type: typeFilter || undefined,
    status: statusFilter || undefined,
    queue_id: queueFilter || undefined,
    priority: serverPriority,
    q: searchFilter || undefined,
    assigned_to: assigneeFilter || undefined,
    scope,
    sort,
    limit: MAX_LIST_LIMIT,
  });

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

  return (
    <div>
      <PageHead
        title={t("serviceDesk.list.title")}
        description={t("serviceDesk.list.description")}
        actions={
          canWrite ? (
            <Btn label={t("serviceDesk.list.newTicket")} icon="plus" variant="primary" onClick={() => setCreating(true)} />
          ) : undefined
        }
      />
      <ServiceDeskNav canRead={canRead} canAdmin={canAdmin} />
      <TicketReadiness />

      {/* Each tile counts rows on screen and links at the URL that shows
          exactly those rows. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("serviceDesk.list.shown")}
          value={String(stats.total)}
          icon="ticket"
          to="/service-desk"
          tone="neutral"
          foot={foot.total}
          zeroLabel={t("serviceDesk.list.zeroShown")}
        />
        <StatCard
          label={t("serviceDesk.common.unassigned")}
          value={String(stats.unassigned)}
          icon="help-circle"
          to="/service-desk?scope=unassigned"
          tone="gold"
          urgent
          foot={foot.unassigned}
          zeroLabel={t("serviceDesk.list.zeroUnassigned")}
        />
        <StatCard
          label={t("serviceDesk.common.highOrUrgentPriority")}
          value={String(stats.urgent)}
          icon="alert-triangle"
          to="/service-desk?priority=high,urgent"
          tone="red"
          urgent
          foot={foot.urgent}
          zeroLabel={t("serviceDesk.list.zeroHighUrgent")}
        />
        <StatCard
          label={t("serviceDesk.list.fromAlerts")}
          value={String(stats.fromAlerts)}
          icon="bell-ringing"
          to="/service-desk?origin=alert"
          tone="blue"
          foot={foot.fromAlerts}
          zeroLabel={t("serviceDesk.list.zeroFromAlerts")}
        />
      </StatsGrid>

      <h2 className="sr-only">{t("serviceDesk.list.heading")}</h2>
      <Panel bare>
        <FilterBar>
          {/* The scope comes first: it is the question an operator asks
              before any filter ("mine, or what nobody has picked up?"). */}
          <Chips
            label={t("serviceDesk.list.show")}
            items={SCOPES.map((s) => t(s.label))}
            active={SCOPES.findIndex((s) => s.value === scope)}
            onSelect={(index) => {
              const chosen = SCOPES[index];
              if (chosen) setScope(chosen.value);
            }}
          />
          <SearchField
            placeholder={t("serviceDesk.list.searchPlaceholder")}
            aria-label={t("serviceDesk.list.searchAria")}
            maxLength={128}
            value={searchFilter}
            onChange={(e) => update({ q: e.target.value }, true)}
          />
          <select
            className="select-field"
            aria-label={t("serviceDesk.list.sortAria")}
            value={sort}
            onChange={(e) =>
              update({ sort: e.target.value === DEFAULT_SORT ? "" : e.target.value })
            }
          >
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                {t(s.label)}
              </option>
            ))}
          </select>
          <span aria-hidden="true" style={{ flexBasis: "100%", height: 0 }} />
          <select
            className="select-field"
            aria-label={t("serviceDesk.list.typeAria")}
            value={typeFilter}
            onChange={(e) => update({ type: e.target.value })}
          >
            {TYPE_FILTERS.map((type) => (
              <option key={type} value={type}>
                {type === "" ? t("serviceDesk.list.allTypes") : ticketTypeLabel(type)}
              </option>
            ))}
          </select>
          <select
            className="select-field"
            aria-label={t("serviceDesk.list.statusAria")}
            value={statusFilter}
            onChange={(e) => update({ status: e.target.value })}
          >
            <option value="">{t("serviceDesk.list.anyStatus")}</option>
            {ALL_STATUSES.map((s) => (
              <option key={s} value={s}>
                {ticketStatusLabel(s)}
              </option>
            ))}
          </select>
          <select
            className="select-field"
            aria-label={t("serviceDesk.list.queueAria")}
            value={queueFilter}
            onChange={(e) => update({ queue: e.target.value })}
          >
            <option value="">{t("serviceDesk.list.allQueues")}</option>
            {queues.data?.map((q) => (
              <option key={q.id} value={q.id}>
                {q.name}
              </option>
            ))}
          </select>
          <select
            className="select-field"
            aria-label={t("serviceDesk.list.priorityAria")}
            value={priorityFilter}
            onChange={(e) => update({ priority: e.target.value })}
          >
            {PRIORITY_FILTERS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.value === ""
                  ? t("serviceDesk.list.allPriorities")
                  : p.label
                    ? t(p.label)
                    : ticketPriorityLabel(p.value)}
              </option>
            ))}
          </select>
          <select
            className="select-field"
            aria-label={t("serviceDesk.list.originAria")}
            value={originFilter}
            onChange={(e) => update({ origin: e.target.value })}
          >
            {ORIGIN_FILTERS.map((o) => (
              <option key={o.value} value={o.value}>
                {t(o.label)}
              </option>
            ))}
          </select>
          <select
            className="select-field"
            aria-label={t("serviceDesk.list.assigneeAria")}
            value={assigneeFilter}
            onChange={(e) => setAssigneeFilter(e.target.value)}
          >
            <option value="">{t("serviceDesk.list.anyone")}</option>
            {userOptions.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
          <span className="filter-spacer" />
          {dataReady ? (
            <span className="mono cell-sub">
              {t("serviceDesk.list.shownCount", { count: visibleTickets.length, n: formatCount(visibleTickets.length) })}
            </span>
          ) : null}
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

        <QueryState
          loading={!dataReady && !tickets.error}
          error={tickets.error}
          empty={dataReady && visibleTickets.length === 0}
          emptyIcon="ticket"
          emptyLabel={t("serviceDesk.list.empty")}
        >
          <TicketTable
            tickets={visibleTickets}
            queueName={queueName}
            userOptions={userOptions}
            now={now}
          />
        </QueryState>
      </Panel>

      <NewTicketDialog open={creating} onClose={() => setCreating(false)} myUserId={myUserId} />
    </div>
  );
}
