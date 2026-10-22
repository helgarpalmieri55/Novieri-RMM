import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Panel, PageHead, QueryState } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useIdleSeats, useSpend } from "./api";
import type { IdleSeatRow, SpendRow } from "./types";

// Mirrors backend/app/modules/subscriptions/reporting.py's spend_summary:
// four roll-ups (by_currency, by_vendor, by_category, by_owner), each never
// summed across currencies. `group_key`/`group_id` are `null` on the
// top-level by_currency rows.
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
              <Th>{t("subscriptions.spend.colCurrency")}</Th>
              <Th>{t("subscriptions.spend.colMonthly")}</Th>
              <Th>{t("subscriptions.spend.colAnnual")}</Th>
            </tr>
          </thead>
          <tbody>
            {(rows ?? []).map((row, i) => (
              <tr key={`${row.group_id ?? row.group_key ?? "none"}-${row.currency}-${i}`}>
                {/* A row with no group is the by-currency top line, or a
                    vendor or owner the org could not resolve; it says so
                    rather than drawing a placeholder as if it were a name. */}
                <Td lead={<Icon name={glyph} size={18} />}>
                  <div className="cell-title">{row.group_key ?? (row.group_type ? t("subscriptions.spend.notResolved") : t("subscriptions.spend.allSubscriptions"))}</div>
                </Td>
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
  const spend = useSpend();
  const idleSeats = useIdleSeats();

  return (
    <div>
      <PageHead
        title={t("subscriptions.spend.title")}
        description={t("subscriptions.spend.description")}
      />

      {/* THE HEADER NAMES THE CELLS UNDER IT, which on this roll-up read "All
          subscriptions": the by-currency rows are the whole estate split by
          currency, and the currency itself is the column next to them.
          "Currency total" named the panel's grouping instead, so the first two
          columns read as a contradiction. */}
      <RollupTable
        title={t("subscriptions.spend.byCurrency")}
        subtitle={t("subscriptions.spend.neverSummed")}
        rows={spend.data?.by_currency}
        loading={spend.isLoading}
        error={spend.error}
        groupLabel={t("subscriptions.spend.scope")}
        glyph="wallet"
      />
      {/* Stacked, not three-up: each roll-up is a four-column table, and a
          third of the content width truncates its last two columns behind
          the scroller. */}
      <RollupTable
        title={t("subscriptions.spend.byVendor")}
        rows={spend.data?.by_vendor}
        loading={spend.isLoading}
        error={spend.error}
        groupLabel={t("subscriptions.spend.vendor")}
        glyph="building"
      />
      <RollupTable
        title={t("subscriptions.spend.byCategory")}
        rows={spend.data?.by_category}
        loading={spend.isLoading}
        error={spend.error}
        groupLabel={t("subscriptions.spend.category")}
        glyph="tag"
      />
      <RollupTable
        title={t("subscriptions.spend.byOwner")}
        rows={spend.data?.by_owner}
        loading={spend.isLoading}
        error={spend.error}
        groupLabel={t("subscriptions.spend.owner")}
        glyph="user"
      />
      <IdleSeatsTable rows={idleSeats.data} loading={idleSeats.isLoading} error={idleSeats.error} />
    </div>
  );
}
