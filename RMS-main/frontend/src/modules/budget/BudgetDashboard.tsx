/**
 * Budget dashboard — year-to-date position and category variance for one
 * fiscal-year budget. Migrated onto the design per the UI port spec §6,
 * following DevicesPage.tsx as the template.
 *
 * WHAT THE DESIGN ASKS FOR THAT THE API DOES NOT SERVE. The prototype's
 * Budget overview shows five stat cards: Annual budget, Actual YTD,
 * Committed, Forecast, Available. `GET /budget/budgets/{id}/position`
 * (useBudgetPosition) serves `ytd_actual`, `pct_of_plan`,
 * `avg_monthly_burn`, and `projected_year_end` -- close to Actual YTD and
 * Forecast, both already converted to the budget's base currency. "Annual
 * budget", "Committed" and "Available" have no matching field: they would
 * require summing `VarianceRow.planned` / `.committed` across every
 * category and period, and this codebase's invariant #6 is explicit that
 * Decimal money strings are never summed client-side -- only the backend,
 * which owns the FX conversion, is trusted to add money together. Shown
 * only as the per-row figures the variance table already carries below.
 */

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { BarChart } from "../../components/chart";
import { EmptyState, NoValue } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import {
  useBudgetCategories,
  useBudgetPosition,
  useBudgetSettings,
  useBudgetVariance,
  useBudgets,
} from "./api";
import { BudgetReadiness } from "./BudgetReadiness";
import { BudgetPageNav } from "./budgetState";
import { chartFigure, compactMoney, exactMoney } from "./money";
import { PageHead, Panel, Progress, QueryState, StatsGrid } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Pill, Tile } from "../../shell/viz";

// spec §3: every aggregate that sums `amount_base` also discloses what it
// could not convert -- a total is never quietly understated. Rendered
// explicitly here (not behind a tooltip), and only when there is something
// to disclose; a zero count is simply not shown, since there is nothing to
// warn about.
function UnconvertibleDisclosure({
  count,
  byCurrency,
  scope,
  repeated = false,
}: {
  count: number;
  byCurrency: Record<string, string>;
  /** What these rows are missing from: "the position figures above". Two
   *  aggregates on this page each disclose their own exclusions, and a notice
   *  that says "the totals above" twice on one page names neither. */
  scope: string;
  /** The same expenses, already disclosed higher up the page. Says so in one
   *  line instead of repeating the currency list word for word, which read as
   *  a second, separate problem. */
  repeated?: boolean;
}) {
  const t = useT();
  if (count === 0) return null;
  if (repeated) {
    return (
      <div role="status" className="notice gold" style={{ marginBottom: 14 }}>
        <Icon name="alert-triangle" size={16} />
        <div>
          <p>{t("budget.dashboard.unconvertibleAgain", { count, scope })}</p>
        </div>
      </div>
    );
  }
  return (
    // `.notice.gold`, NOT A HAND-BUILT BOX. This is the design's callout and
    // there was already one of it: the same wash, an edge that is `--line`
    // because a notice's tone is carried by its fill rather than by its border,
    // and `--gold-ink` rather than `--gold` for the sentence -- 5.71:1 against
    // the wash instead of a tone chosen to be seen on white. Nothing here is a
    // new value; the box simply stopped restating the component badly.
    <div role="status" className="notice gold" style={{ marginBottom: 14 }}>
      <Icon name="alert-triangle" size={16} />
      <div>
        <p>{t("budget.dashboard.unconvertible", { count, scope })}</p>
        <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
          {Object.entries(byCurrency).map(([currency, amount]) => (
            <li key={currency} className="mono">{`${amount} ${currency}`}</li>
          ))}
        </ul>
        {/* The fix, not just the finding: a rate from each listed currency to
            the base currency is what brings these back into the totals. */}
        <p style={{ marginTop: 6 }}>
          <Link className="panel-link" to="/budget/admin#fx-rates">
            {t("budget.dashboard.addRate")}
          </Link>
        </p>
      </div>
    </div>
  );
}

export function BudgetDashboard() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("budget.read");

  const budgets = useBudgets();
  const [budgetIdOverride, setBudgetIdOverride] = useState<string | undefined>(undefined);
  const budgetId = budgetIdOverride ?? budgets.data?.[0]?.id;

  const categories = useBudgetCategories();
  const categoryName = (id: string) => categories.data?.find((c) => c.id === id)?.name ?? id;

  const variance = useBudgetVariance(budgetId);
  // Read for one thing: the currency the converted figures are in, named on
  // the chart axis. An axis of money with no currency is a number nobody can
  // act on.
  const settings = useBudgetSettings();
  const position = useBudgetPosition(budgetId);

  // Auditor B, [MEDIUM]: "the Dashboard renders 'Year-to-date position → No
  // position data yet.' and 'Variance → No variance data yet.' — two separate,
  // similarly-worded dead ends, neither of which mentions that the prerequisite
  // is creating a budget in the adjacent Planner tab." One cause, one first-run
  // state, and the empty `<select>` beside it -- a control with no options and
  // no explanation -- goes with them.
  const noBudgets = !budgets.isLoading && !budgets.error && (budgets.data ?? []).length === 0;

  // The chart below plots ONE period, so the page has to choose one: the
  // latest period the server actually returned rows for, which is the period
  // an operator opening this page is looking at.
  const rows = variance.data?.rows ?? [];
  const periodsAvailable = useMemo(
    () => [...new Set(rows.map((row) => row.period_index))].sort((a, b) => a - b),
    [rows],
  );
  const [periodOverride, setPeriodOverride] = useState<number | undefined>(undefined);
  const period =
    periodOverride != null && periodsAvailable.includes(periodOverride)
      ? periodOverride
      : periodsAvailable.at(-1);

  const rowsForPeriod = rows.filter((row) => row.period_index === period);
  const chartCategories = rowsForPeriod.map((row) => categoryName(row.category_id));
  // `Number()` HERE AND NOWHERE ELSE IN THIS MODULE. Money is a Decimal string
  // on the wire and is never added, compared or rounded in the browser -- but a
  // bar has to be some number of pixels tall, and that geometry is the one
  // thing a float may decide. Every figure a person reads is still the server's
  // string: the variance table below, and the chart's own data table, which
  // formats these back to two places.
  const chartSeries = [
    { label: t("budget.dashboard.seriesPlanned"), values: rowsForPeriod.map((row) => Number(row.planned)) },
    { label: t("budget.dashboard.seriesActual"), values: rowsForPeriod.map((row) => Number(row.actual)) },
    { label: t("budget.dashboard.seriesCommitted"), values: rowsForPeriod.map((row) => Number(row.committed)) },
  ];
  // v3's "by cost centre" bars, per category here: the same floats as the
  // chart's geometry, driving a bar's width instead of its height. A category
  // with nothing planned has no fraction to draw and is left out rather than
  // drawn at zero.
  const categoryBars = rowsForPeriod
    .map((row) => ({
      id: row.category_id,
      name: categoryName(row.category_id),
      planned: Number(row.planned),
      projected: Number(row.projected),
    }))
    .filter((bar) => Number.isFinite(bar.planned) && bar.planned > 0)
    .map((bar) => ({ ...bar, pct: (bar.projected / bar.planned) * 100 }));
  const baseCurrency = settings.data?.base_currency ?? t("budget.dashboard.baseCurrencyFallback");
  // The ISO code alone, for the tiles. An unknown base currency (the settings
  // query failed) draws no symbol and no code rather than a guessed one: the
  // figures are still the server's, and a wrong currency on money is a worse
  // answer than an unlabelled number.
  const baseCurrencyCode = settings.data?.base_currency;

  // The two aggregates on this page each disclose what they could not convert,
  // and when the ledger is the same both disclosures are about the SAME
  // expenses -- drawn twice, word for word including the currency list, the
  // second one read as a second problem.
  const positionUnconvertible = position.data?.unconvertible_count ?? 0;
  const varianceRepeatsPosition =
    positionUnconvertible > 0 &&
    variance.data != null &&
    position.data != null &&
    variance.data.unconvertible_count === positionUnconvertible &&
    JSON.stringify(Object.entries(variance.data.unconvertible_by_currency).sort()) ===
      JSON.stringify(Object.entries(position.data.unconvertible_by_currency).sort());

  return (
    <div>
      <PageHead
        title={t("budget.dashboard.title")}
        description={t("budget.dashboard.description")}
        actions={
          noBudgets ? undefined : (
            <label className="flex items-center gap-2">
              <span className="note">{t("budget.dashboard.budget")}</span>
              <select
                className="select-field"
                style={{ width: "auto" }}
                value={budgetId ?? ""}
                onChange={(e) => setBudgetIdOverride(e.target.value)}
              >
                {budgets.data?.map((budget) => (
                  <option key={budget.id} value={budget.id}>
                    {budget.name} ({budget.fiscal_year})
                  </option>
                ))}
              </select>
            </label>
          )
        }
      />

      <BudgetPageNav canRead={canRead} />

      <BudgetReadiness />

      {noBudgets ? (
        // ONE first-run state, with the action in it, instead of two dead ends
        // that each say "nothing here" and neither says what to do. There is
        // no position and no variance for the same reason, and the reason is
        // fixable from the tab next door.
        <Panel>
          <EmptyState
            icon="wallet"
            title={t("budget.dashboard.noBudgetTitle")}
            body={t("budget.dashboard.noBudgetBody")}
            action={
              <Link className="btn" to="/budget/planner">
                <Icon name="plus" size={14} />
                <span>{t("budget.dashboard.noBudgetAction")}</span>
              </Link>
            }
          />
        </Panel>
      ) : null}

      {noBudgets ? null : (
        <>
      {/* THE TILES SIT AT PAGE LEVEL, as v3 draws them: crumb, head, tiles,
          then the panels. They were a panel inside a panel here once; the
          review's composition check against the prototype put them back
          under the head. */}
      <QueryState
        loading={position.isLoading}
        error={position.error}
        empty={!position.isLoading && !position.error && position.data == null}
        emptyLabel={t("budget.dashboard.noPosition")}
      >
        {position.data && (
          <>
            {/* EVERY TILE NAMES THE LIST BEHIND IT. Three of these four are
                derived from recorded expenses, so they open the expense
                ledger; "% of plan" is the only one whose other half is the
                plan, so it opens the Planner where those planned amounts
                are entered. None of them is urgent: nothing on this row is
                a threshold the server has evaluated, and a burn figure
                wearing an alarm colour would be this page asserting a
                judgement it has not made. Over-budget belongs to the
                variance table below, which knows planned vs actual per
                category. An absent "% of plan" is unmeasured (no plan to
                divide by), so it reads n/a, never 0. */}
            {/* `Tile` DIRECTLY, FOR THE FIGURE AND ITS FOOT. Three of these
                four figures are money, and they were printing the wire's
                Decimal string with the ISO code glued on as the unit slot --
                "76010.40EUR". v3's tile row for this page draws a short human
                figure instead (`v:'€412k'`), which is how the "% of plan"
                tile beside them already reads. So the figure is
                `compactMoney` and the exact amount sits under it in the `z`
                slot, where nothing is rounded away: a glance and a reading,
                not one at the cost of the other. `StatCard` has neither a
                `unit` nor a `sub` a page can fill (its `foot` is a stat-foot
                span inside `z`), so these render the v3 tile it wraps,
                keeping its `.stat-card` / `.stat-label` / `.stat-value` hooks
                and its links. None of them is urgent (no threshold on this
                row has been evaluated) and none is a zero that needs a label,
                which is all `StatCard` was adding here. */}
            <StatsGrid columns={4}>
              <Tile
                className="stat-card tone-primary"
                parts={{ label: "stat-label", value: "stat-value" }}
                label={t("budget.dashboard.tileYtd")}
                value={compactMoney(position.data.ytd_actual, baseCurrencyCode)}
                sub={<span className="stat-foot">{exactMoney(position.data.ytd_actual, baseCurrencyCode)}</span>}
                icon="subscription"
                to="/budget/expenses"
              />
              <Tile
                className="stat-card tone-blue"
                parts={{ label: "stat-label", value: "stat-value" }}
                label={t("budget.dashboard.tilePct")}
                value={position.data.pct_of_plan ?? t("budget.common.na")}
                unit={position.data.pct_of_plan != null ? "%" : undefined}
                /* The denominator, since the figure is a ratio: this is
                   actuals against the plan for the periods that have gone by,
                   not against the whole year. Absent when there is no ratio
                   (nothing planned) -- an unmeasured figure has no breakdown
                   to give. */
                sub={
                  position.data.pct_of_plan != null && position.data.elapsed_periods > 0 ? (
                    <span className="stat-foot">
                      {t("budget.dashboard.elapsed", { count: position.data.elapsed_periods })}
                    </span>
                  ) : undefined
                }
                icon="chart"
                to="/budget/planner"
              />
              <Tile
                className="stat-card tone-neutral"
                parts={{ label: "stat-label", value: "stat-value" }}
                label={t("budget.dashboard.tileBurn")}
                value={compactMoney(position.data.avg_monthly_burn, baseCurrencyCode)}
                sub={<span className="stat-foot">{exactMoney(position.data.avg_monthly_burn, baseCurrencyCode)}</span>}
                icon="budget"
                to="/budget/expenses"
              />
              <Tile
                className="stat-card tone-neutral"
                parts={{ label: "stat-label", value: "stat-value" }}
                label={t("budget.dashboard.tileYearEnd")}
                value={compactMoney(position.data.projected_year_end, baseCurrencyCode)}
                sub={<span className="stat-foot">{exactMoney(position.data.projected_year_end, baseCurrencyCode)}</span>}
                icon="clock"
                to="/budget/expenses"
              />
            </StatsGrid>
            {/* After the tiles, and it names them: "the totals above" said
                nothing about which totals on a page carrying two aggregates. */}
            <UnconvertibleDisclosure
              count={position.data.unconvertible_count}
              byCurrency={position.data.unconvertible_by_currency}
              scope={t("budget.dashboard.scopePosition")}
            />
          </>
        )}
      </QueryState>

      {categoryBars.length > 0 ? (
        <Panel
          title={t("budget.dashboard.byCategory")}
          subtitle={t("budget.dashboard.byCategorySub", { period: period ?? "", currency: baseCurrency })}
        >
          {/* v3's bar list: the name, the percentage, the bar. The track is
              the plan, so a row past 100% fills it exactly like a row AT
              100% -- the bar cannot draw what it has no room for, and a
              clamped bar plus a warn tint left the numeral as the only thing
              telling "on plan" from "over plan" apart. So an over-plan row
              carries the design's own answer beside the figure: v3's planner
              draws `pill('warn','+17%')` on exactly this fact. The pill is a
              shape the at-plan rows do not have, and it brings the warn
              kind's alert-triangle glyph with it, so neither colour nor a
              digit is carrying this alone. The overage is stated as its own
              number rather than repeating the total. */}
          {categoryBars.map((bar) => {
            const pct = Math.round(bar.pct);
            const over = bar.pct > 100;
            return (
              <div className="prog" key={bar.id}>
                <div className="prog-l">
                  <span>{bar.name}</span>
                  <span className="flex items-center gap-2">
                    {over ? <Pill kind="warn">{t("budget.dashboard.overPill", { over: pct - 100 })}</Pill> : null}
                    <span className="mono">{`${pct}%`}</span>
                  </span>
                </div>
                <Progress
                  value={bar.pct}
                  tone={over ? "gold" : "green"}
                  // The bar stops at the track; the name says where the value
                  // actually is, so a screen reader is not told 100%.
                  label={
                    over
                      ? t("budget.dashboard.barLabelOver", { name: bar.name, pct, over: pct - 100 })
                      : t("budget.dashboard.barLabel", { name: bar.name, pct })
                  }
                />
              </div>
            );
          })}
        </Panel>
      ) : null}

      <Panel title={t("budget.dashboard.planVersusActual")}>
        {/* Auditor B, [HIGH]: "The entire view is the sentence above plus two
            empty-state lines ... Plan vs actual vs committed is the canonical
            three-series comparison — it is a bar-with-target or waterfall, and
            reading it as numbers costs the user the whole point of a
            'dashboard'." `BarChart` renders the three series with a hatch and a
            legend per series and mirrors the whole figure in a real table, so
            nothing here depends on colour alone.

            ONE PERIOD AT A TIME, and that is not a layout preference. Rolling
            twelve periods into one bar per category means ADDING MONEY in the
            browser, which invariant #6 forbids: the backend owns FX conversion
            and is the only thing trusted to total. Each bar below is one
            `VarianceRow` exactly as the server computed it.

            NO CUMULATIVE BURN LINE, for the same reason -- a burn curve is a
            running total. `avg_monthly_burn` and `projected_year_end` above are
            the server's own answers to that question. */}
        <QueryState
          loading={variance.isLoading}
          error={variance.error}
          empty={chartCategories.length === 0}
          emptyLabel={t("budget.dashboard.noPeriodAmounts")}
        >
          <label className="flex items-center gap-2" style={{ marginBottom: 12 }}>
            <span className="note">{t("budget.dashboard.period")}</span>
            <select
              className="select-field"
              style={{ width: "auto" }}
              value={period ?? ""}
              onChange={(e) => setPeriodOverride(Number(e.target.value))}
            >
              {periodsAvailable.map((index) => (
                <option key={index} value={index}>
                  {t("budget.dashboard.periodOption", { index })}
                </option>
              ))}
            </select>
          </label>
          {/* NO WIDTH CAP HERE ANY MORE, AND THE CAP WAS NEVER THIS PAGE'S
              FIX. It read `maxWidth: 640` and its comment said why: the kit
              drew `width: 100%` over a fixed 640-unit viewBox, so every glyph
              the SVG painted scaled with the box -- a 12px tick landing near
              21px in this panel at 1440. Holding the drawing at the width it
              was drawn for was the only lever a page had, and the same comment
              recorded the other end of it as reported to the kit and NOT
              worked around here. The kit fixed it: `chart.tsx` measures the
              box it is in and sets the viewBox to that number, so one user
              unit is one CSS pixel and the type paints at its token size at
              any width. The cap outlived its reason, and a browser review
              found what it cost -- the plot and its grid stopping around
              x=900 inside a card running to x=1415, ~44% of the panel left
              blank to the right of the last category. */}
          <BarChart
            title={t("budget.dashboard.chartTitle", { period: period ?? "" })}
            categories={chartCategories}
            series={chartSeries}
            unit={baseCurrency}
            format={chartFigure}
          />
          <p className="note" style={{ marginTop: 8 }}>
            {t("budget.dashboard.chartNote")}
          </p>
        </QueryState>
      </Panel>

      <Panel title={t("budget.dashboard.variance")} bare>
        <QueryState
          loading={variance.isLoading}
          error={variance.error}
          empty={(variance.data?.rows ?? []).length === 0}
          emptyLabel={t("budget.dashboard.noVariance")}
        >
          {variance.data && (
            <>
              <UnconvertibleDisclosure
                count={variance.data.unconvertible_count}
                byCurrency={variance.data.unconvertible_by_currency}
                scope={t("budget.dashboard.scopeVariance")}
                repeated={varianceRepeatsPosition}
              />
              <Table>
                <thead>
                  <tr>
                    <Th>{t("budget.dashboard.colCategory")}</Th>
                    <Th>{t("budget.dashboard.colPeriod")}</Th>
                    {/* Four distinct columns -- never collapsed into one
                        "forecast" figure (the Critical bug this UI must
                        not repeat: spec §7, VarianceRow's docstring). */}
                    <Th>{t("budget.dashboard.colPlanned")}</Th>
                    <Th>{t("budget.dashboard.colActual")}</Th>
                    <Th>{t("budget.dashboard.colCommitted")}</Th>
                    <Th>{t("budget.dashboard.colProjected")}</Th>
                    <Th>{t("budget.dashboard.colVariance")}</Th>
                    <Th>{t("budget.dashboard.colVariancePct")}</Th>
                  </tr>
                </thead>
                <tbody>
                  {variance.data.rows.map((row, i) => {
                    // Over plan is a SIGN, read off the string: no money is
                    // parsed to decide it. A negative variance is under plan
                    // and needs nobody; a positive one wears the warning edge.
                    const over = !row.variance.startsWith("-") && !/^0(\.0+)?$/.test(row.variance);
                    return (
                      <tr key={`${row.category_id}-${row.cost_center_id ?? "none"}-${row.period_index}-${i}`}>
                        <Td lead={<Icon name="tag" size={18} />} gutter={over ? "warn" : undefined}>
                          <div className="cell-title">{categoryName(row.category_id)}</div>
                        </Td>
                        <Td>
                          <span className="mono">{row.period_index}</span>
                        </Td>
                        <Td>
                          <span className="mono">{row.planned}</span>
                        </Td>
                        <Td>
                          <span className="mono">{row.actual}</span>
                        </Td>
                        <Td>
                          <span className="mono">{row.committed}</span>
                        </Td>
                        <Td>
                          <span className="mono">{row.projected}</span>
                        </Td>
                        <Td>
                          <span className="mono">{row.variance}</span>
                        </Td>
                        <Td>
                          {row.variance_pct != null ? (
                            <span className="mono">{row.variance_pct}</span>
                          ) : (
                            <NoValue label={t("budget.dashboard.noVariancePct")} />
                          )}
                        </Td>
                      </tr>
                    );
                  })}
                </tbody>
              </Table>
            </>
          )}
        </QueryState>
      </Panel>
        </>
      )}
    </div>
  );
}
