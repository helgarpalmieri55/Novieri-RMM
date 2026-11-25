import { useQuery } from "@tanstack/react-query";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import {
  ClientFilter,
  Panel,
  PageHead,
  QueryState,
  StatCard,
  StatsGrid,
  useNarrowedApi,
  useUrlFilters,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useClientName } from "../settings/api";
import { useIdleSeats, useSpend } from "./api";
import { SubscriptionPageNav } from "./subscriptionsNav";
import type { IdleSeatRow, SpendRow } from "./types";
import { useRecordTitle } from "../../shell/title";

// Mirrors backend/app/modules/subscriptions/reporting.py's spend_summary:
// four roll-ups (by_currency, by_vendor, by_category, by_owner), each never
// summed across currencies -- nor across clients: every row is one client's
// (MSP clients phase 2), and says which. `group_key`/`group_id` are `null` on
// the top-level by_currency rows.
function RollupTable({
  title,
  subtitle,
  rows,
  loading,
  error,
  groupLabel,
  glyph,
}: {
  title: string;
  subtitle?: string;
  rows: SpendRow[] | undefined;
  loading: boolean;
  error: unknown;
  groupLabel: string;
  glyph: string;
}) {
  const t = useT();
  const clientName = useClientName();
  // WHOSE EACH ROW IS, with every client chosen (design §3.4); with one
  // chosen every row is that client's, and the column would only repeat it.
  const { selected } = useClientScope();
  const showClient = selected === null;
  return (
    <Panel title={title} subtitle={subtitle} bare>
      <QueryState
        loading={loading}
        error={error}
        empty={(rows ?? []).length === 0}
        emptyLabel={t("subscriptions.spend.empty")}
        emptyIcon="chart-bar"
      >
        <Table>
          <thead>
            <tr>
              <Th>{groupLabel}</Th>
              {showClient && <Th>{t("subscriptions.spend.colClient")}</Th>}
              <Th>{t("subscriptions.spend.colCurrency")}</Th>
              <Th>{t("subscriptions.spend.colMonthly")}</Th>
              <Th>{t("subscriptions.spend.colAnnual")}</Th>
            </tr>
          </thead>
          <tbody>
            {(rows ?? []).map((row, i) => (
              <tr key={`${row.client_id}-${row.group_id ?? row.group_key ?? "none"}-${row.currency}-${i}`}>
                {/* A row with no group is the by-currency top line, or a
                    vendor or owner the org could not resolve; it says so
                    rather than drawing a placeholder as if it were a name. */}
                <Td lead={<Icon name={glyph} size={18} />}>
                  <div className="cell-title">{row.group_key ?? (row.group_type ? t("subscriptions.spend.notResolved") : t("subscriptions.spend.allSubscriptions"))}</div>
                </Td>
                {showClient && <Td>{clientName(row.client_id)}</Td>}
                <Td>
                  <span className="mono">{row.currency}</span>
                </Td>
                <Td>
                  <span className="mono">{row.monthly_total}</span>
                </Td>
                <Td>
                  <span className="mono">{row.annual_total}</span>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </Panel>
  );
}

// Mirrors backend/app/modules/subscriptions/reporting.py's idle_seats: one
// row per *active* subscription, `idle = seats - assigned`, and
// `idle_monthly_cost` a Decimal that arrives as a JSON string -- rendered
// verbatim, never coerced to a number (invariant #6, same as the spend
// roll-ups above).
function IdleSeatsTable({
  rows,
  loading,
  error,
}: {
  rows: IdleSeatRow[] | undefined;
  loading: boolean;
  error: unknown;
}) {
  const t = useT();
  return (
    <Panel title={t("subscriptions.spend.idleTitle")} subtitle={t("subscriptions.spend.idleSubtitle")} bare>
      <QueryState
        loading={loading}
        error={error}
        empty={(rows ?? []).length === 0}
        emptyLabel={t("subscriptions.spend.idleEmpty")}
        emptyIcon="users"
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("subscriptions.spend.colService")}</Th>
              <Th>{t("subscriptions.spend.colSeats")}</Th>
              <Th>{t("subscriptions.spend.colAssigned")}</Th>
              <Th>{t("subscriptions.spend.colIdle")}</Th>
              <Th>{t("subscriptions.spend.colCurrency")}</Th>
              <Th>{t("subscriptions.spend.colIdleCost")}</Th>
            </tr>
          </thead>
          <tbody>
            {(rows ?? []).map((row) => (
              <tr key={row.subscription_id}>
                <Td lead={<Icon name="credit-card" size={18} />} gutter={row.idle > 0 ? "warn" : undefined}>
                  <div className="cell-title">{row.service_name}</div>
                </Td>
                <Td>
                  <span className="mono">{row.seats}</span>
                </Td>
                <Td>
                  <span className="mono">{row.assigned}</span>
                </Td>
                <Td>
                  <span className="mono">{row.idle}</span>
                </Td>
                <Td>
                  <span className="mono">{row.currency}</span>
                </Td>
                <Td>
                  <span className="mono">{row.idle_monthly_cost}</span>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </Panel>
  );
}

export function SpendDashboard() {
  const t = useT();
  // A sub-page without a rail entry names itself in the tab, as its h1 does
  // (the coordinator's ruling 1b): "<this page> · <rail entry> · Novieri RMM".
  useRecordTitle(t("subscriptions.spend.title"));
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRead = (me.data?.permissions ?? []).includes("subscriptions.read");
  // THE CLIENT CHIP (decision D6): the registry's run-rate tile links here
  // with its client, so the page opens the spend that tile read, and a person
  // with every client shown can narrow it. Both reports narrow alike.
  const filters = useUrlFilters(["client"] as const);
  const { client: narrowedTo } = useNarrowedApi(filters.values.client);
  const spend = useSpend(filters.values.client);
  const idleSeats = useIdleSeats(filters.values.client);
  const clientName = useClientName();
  const { selected, clients } = useClientScope();
  const totals = spend.data?.by_currency ?? [];

  // EACH TOTAL OPENS THE LIST THAT EXPLAINS IT: the subscriptions of the
  // total's client, the registry narrowed to it by the Client chip (only a
  // client of the person's, so the chip never drops it on arrival). With one
  // client chosen at the top, or the chip already narrowing, every total is
  // that client's and the registry already reads only its rows.
  const listOf = (row: SpendRow): string => {
    if (narrowedTo !== null) return `/subscriptions?client=${narrowedTo}`;
    if (selected === null && clients.some((client) => client.id === row.client_id)) {
      return `/subscriptions?client=${row.client_id}`;
    }
    return "/subscriptions";
  };

  return (
    <div>
      <PageHead
        title={t("subscriptions.spend.title")}
        description={t("subscriptions.spend.description")}
      />

      <SubscriptionPageNav canRead={canRead} />

      {/* The Client chip, on its own row: the page has no list toolbar to
          hold it, and the chip draws only with every client and two or more
          of them (shell/listKit.tsx), so no empty bar is left behind. */}
      {selected === null && clients.length >= 2 ? (
        <div className="list-chips" style={{ marginBottom: 16 }}>
          <ClientFilter filters={filters} />
        </div>
      ) : null}

      {/* THE TOTALS, ONE TILE EACH (Task 10, 5.4): the by-currency roll-up,
          one row per (client, currency), never summed across either (module
          docstring, invariant #6). Each tile is the row's monthly total in its
          own currency, its annual total under it, and opens the subscriptions
          behind it. They ARE the "By currency" roll-up (its client, currency,
          monthly and annual figures), so the table that drew the same rows
          again is gone (the wave's critique, P1). */}
      <QueryState
        loading={spend.isPending}
        error={spend.error}
        empty={totals.length === 0}
        emptyLabel={t("subscriptions.spend.empty")}
        emptyIcon="chart-bar"
      >
        <StatsGrid columns={Math.min(4, Math.max(2, totals.length))}>
          {totals.map((row) => (
            <StatCard
              key={`${row.client_id}-${row.currency}`}
              label={
                selected === null
                  ? t("subscriptions.spend.tileMonthlyOf", { currency: row.currency, client: clientName(row.client_id) })
                  : t("subscriptions.spend.tileMonthly", { currency: row.currency })
              }
              value={`${row.monthly_total} ${row.currency}`}
              icon="wallet"
              to={listOf(row)}
              tone="primary"
              foot={t("subscriptions.spend.tileAnnual", { amount: row.annual_total, currency: row.currency })}
            />
          ))}
        </StatsGrid>
        <p className="note" style={{ margin: "-4px 0 16px" }}>
          {t("subscriptions.spend.neverSummed")}
        </p>
      </QueryState>

      {/* Stacked, not three-up: each roll-up is a four-column table, and a
          third of the content width truncates its last two columns behind
          the scroller. */}
      <RollupTable
        title={t("subscriptions.spend.byVendor")}
        rows={spend.data?.by_vendor}
        loading={spend.isPending}
        error={spend.error}
        groupLabel={t("subscriptions.spend.vendor")}
        glyph="building"
      />
      <RollupTable
        title={t("subscriptions.spend.byCategory")}
        rows={spend.data?.by_category}
        loading={spend.isPending}
        error={spend.error}
        groupLabel={t("subscriptions.spend.category")}
        glyph="tag"
      />
      <RollupTable
        title={t("subscriptions.spend.byOwner")}
        rows={spend.data?.by_owner}
        loading={spend.isPending}
        error={spend.error}
        groupLabel={t("subscriptions.spend.owner")}
        glyph="user"
      />
      <IdleSeatsTable rows={idleSeats.data} loading={idleSeats.isPending} error={idleSeats.error} />
    </div>
  );
}
