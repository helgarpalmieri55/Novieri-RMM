/**
 * Fixtures for the budget module: the settings, the categories and cost
 * centres, the FX table, one open and one closed fiscal-year budget with its
 * planned lines, the expense ledger, and the two reports computed from them.
 *
 * ONE LEDGER, EVERY FIGURE DERIVED FROM IT. `getBudgetVariance` and
 * `getBudgetPosition` are computed from the planned lines and the confirmed
 * or expected expenses below, in whole cents, converted at the FX table's
 * rates -- so the tiles on `/budget`, the rows in its variance table and the
 * ledger on `/budget/expenses` cannot disagree, and the one expense whose
 * currency has no rate (the AWS invoice in GBP) shows up in every
 * unconvertible disclosure exactly as the real reports would show it.
 *
 * Money stays a STRING on the way out, two decimals, the way a Decimal
 * serialises; the arithmetic lives here and nowhere the pages can reach.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ApiError } from "../../lib/api";
import type {
  BudgetLineOut,
  BudgetOut,
  CategoryOut,
  CostCenterOut,
  ExpenseOut,
  FxRateOut,
  PositionOut,
  SettingsOut,
  VarianceRow,
} from "./types";

const MINUTE = 60_000;
const DAY = 24 * 60 * MINUTE;

/** An ISO timestamp `days` before the call. */
const ago = (days: number): string => new Date(Date.now() - days * DAY).toISOString();
/** A calendar date (no time, no zone) `days` from today. */
const dateIn = (days: number): string => new Date(Date.now() + days * DAY).toISOString().slice(0, 10);

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));
const money = (cents: number): string => (cents / 100).toFixed(2);
const cents = (amount: string): number => Math.round(Number(amount) * 100);

const ME = "usr-helgar";
const FISCAL_YEAR = new Date().getFullYear();
/** Periods elapsed in a January-start fiscal year, the current month included. */
const ELAPSED = new Date().getMonth() + 1;

/* ---------------------------------------------------------------- settings */

const SETTINGS: SettingsOut = {
  base_currency: "EUR",
  fiscal_year_start_month: 1,
  updated_by: ME,
  updated_at: ago(240),
};

/* ------------------------------------------------------------- categories */

const CATEGORIES: CategoryOut[] = [
  { id: "cat-infra", key: "infrastructure", name: "Infrastructure", system_key: null, active: true, sort_order: 10, created_at: ago(400) },
  { id: "cat-software", key: "software", name: "Software", system_key: "software", active: true, sort_order: 20, created_at: ago(400) },
  { id: "cat-hardware", key: "hardware_refresh", name: "Hardware refresh", system_key: null, active: true, sort_order: 30, created_at: ago(400) },
  { id: "cat-services", key: "services", name: "Services", system_key: null, active: true, sort_order: 40, created_at: ago(400) },
  { id: "cat-training", key: "training", name: "Training", system_key: null, active: false, sort_order: 90, created_at: ago(400) },
];

const COST_CENTERS: CostCenterOut[] = [
  { id: "cc-ops", code: "IT-OPS", name: "IT operations", external_ref: "SAP 4410", active: true, created_at: ago(400) },
  { id: "cc-sec", code: "IT-SEC", name: "Security", external_ref: "SAP 4412", active: true, created_at: ago(400) },
  { id: "cc-euc", code: "IT-EUC", name: "End-user computing", external_ref: "SAP 4415", active: true, created_at: ago(400) },
  { id: "cc-legacy", code: "IT-LEG", name: "Legacy datacentre", external_ref: null, active: false, created_at: ago(400) },
];

/* --------------------------------------------------------------- fx rates */

const FX_RATES: FxRateOut[] = [
  { id: "fx-usd-1", from_currency: "USD", to_currency: "EUR", rate: "0.92150000", effective_date: dateIn(-240), created_at: ago(240) },
  { id: "fx-usd-2", from_currency: "USD", to_currency: "EUR", rate: "0.90800000", effective_date: dateIn(-60), created_at: ago(60) },
  { id: "fx-chf-1", from_currency: "CHF", to_currency: "EUR", rate: "1.04300000", effective_date: dateIn(-240), created_at: ago(240) },
];

/** The rate in force on a date, or null when the currency has no rate at all. */
function rateFor(currency: string, date: string): string | null {
  if (currency === SETTINGS.base_currency) return "1.00000000";
  const candidates = FX_RATES.filter((fx) => fx.from_currency === currency && fx.effective_date <= date).sort((a, b) =>
    a.effective_date < b.effective_date ? 1 : -1,
  );
  return candidates[0]?.rate ?? null;
}

/* ----------------------------------------------------------------- budgets */

const BUDGETS: BudgetOut[] = [
  { id: "bud-1", fiscal_year: FISCAL_YEAR, name: `FY${String(FISCAL_YEAR).slice(2)} IT operating`, granularity: "month", status: "active", created_at: ago(280) },
  { id: "bud-2", fiscal_year: FISCAL_YEAR - 1, name: `FY${String(FISCAL_YEAR - 1).slice(2)} IT operating`, granularity: "month", status: "closed", created_at: ago(640) },
  { id: "bud-3", fiscal_year: FISCAL_YEAR + 1, name: `FY${String(FISCAL_YEAR + 1).slice(2)} draft`, granularity: "quarter", status: "draft", created_at: ago(12) },
];

/** Planned cents per category per month for the open budget. */
const PLAN_MONTHLY: Record<string, number> = {
  "cat-infra": 1_250_000,
  "cat-software": 1_180_000,
  "cat-hardware": 640_000,
  "cat-services": 360_000,
};

function linesFor(budget: BudgetOut): BudgetLineOut[] {
  const periods = budget.granularity === "quarter" ? 4 : 12;
  const scale = budget.granularity === "quarter" ? 3 : 1;
  const factor = budget.status === "closed" ? 0.94 : budget.status === "draft" ? 1.05 : 1;
  const lines: BudgetLineOut[] = [];
  for (const [category, monthly] of Object.entries(PLAN_MONTHLY)) {
    for (let period = 1; period <= periods; period += 1) {
      // The hardware refresh is planned in the second half, as a refresh is.
      const weight = category === "cat-hardware" ? (period > periods / 2 ? 2 : 0) : 1;
      lines.push({
        id: `${budget.id}-${category}-${period}`,
        budget_id: budget.id,
        category_id: category,
        cost_center_id: null,
        period_index: period,
        planned_amount: money(Math.round(monthly * scale * factor * weight)),
        created_at: budget.created_at,
      });
    }
  }
  return lines;
}

/* ---------------------------------------------------------------- expenses */

interface ExpenseSeed {
  id: string;
  daysAgo: number;
  amount: string;
  currency: string;
  vendor: string | null;
  category: string;
  costCenter: string | null;
  description: string;
  source: "manual" | "subscription" | "license";
  linkType: string | null;
  linkId: string | null;
  invoice: string | null;
  attachment: string | null;
  status: "expected" | "confirmed" | "void";
  enteredBy: string;
  confirmedBy: string | null;
}

const EXPENSE_SEEDS: ExpenseSeed[] = [
  { id: "exp-1", daysAgo: 5, amount: "15900.00", currency: "EUR", vendor: null, category: "cat-hardware", costCenter: "cc-euc", description: "Dell Latitude 5550 x 10", source: "manual", linkType: null, linkId: null, invoice: "PO-2291", attachment: "dell-inv-88213.pdf", status: "confirmed", enteredBy: "usr-mr", confirmedBy: ME },
  { id: "exp-2", daysAgo: 2, amount: "4200.00", currency: "EUR", vendor: null, category: "cat-infra", costCenter: "cc-ops", description: "MinIO support renewal", source: "manual", linkType: null, linkId: null, invoice: null, attachment: null, status: "expected", enteredBy: "usr-mr", confirmedBy: null },
  { id: "exp-3", daysAgo: 1, amount: "540.00", currency: "EUR", vendor: null, category: "cat-software", costCenter: "cc-euc", description: "Figma Organization, monthly", source: "subscription", linkType: "subscription", linkId: "sub-1", invoice: null, attachment: null, status: "expected", enteredBy: ME, confirmedBy: null },
  { id: "exp-4", daysAgo: 31, amount: "714.00", currency: "EUR", vendor: null, category: "cat-software", costCenter: "cc-ops", description: "GitHub Enterprise, monthly", source: "subscription", linkType: "subscription", linkId: "sub-2", invoice: "GH-2026-08", attachment: null, status: "confirmed", enteredBy: ME, confirmedBy: "usr-ic" },
  { id: "exp-5", daysAgo: 3, amount: "7200.00", currency: "EUR", vendor: "ven-adobe", category: "cat-software", costCenter: "cc-euc", description: "Adobe Acrobat Pro renewal", source: "license", linkType: "license", linkId: "lic-1", invoice: null, attachment: null, status: "expected", enteredBy: ME, confirmedBy: null },
  { id: "exp-6", daysAgo: 9, amount: "1250.00", currency: "GBP", vendor: null, category: "cat-infra", costCenter: "cc-ops", description: "AWS backup egress, August", source: "manual", linkType: null, linkId: null, invoice: "AWS-0834-2211", attachment: "aws-0834.pdf", status: "confirmed", enteredBy: "usr-mr", confirmedBy: ME },
  { id: "exp-7", daysAgo: 20, amount: "6400.00", currency: "EUR", vendor: null, category: "cat-services", costCenter: "cc-sec", description: "Penetration test, external perimeter", source: "manual", linkType: null, linkId: null, invoice: "INV-7781", attachment: "pentest-7781.pdf", status: "confirmed", enteredBy: "usr-ic", confirmedBy: ME },
  { id: "exp-8", daysAgo: 45, amount: "3300.00", currency: "USD", vendor: "ven-jetbrains", category: "cat-software", costCenter: "cc-ops", description: "JetBrains All Products Pack, annual", source: "subscription", linkType: "subscription", linkId: "sub-5", invoice: "JB-4411", attachment: null, status: "confirmed", enteredBy: ME, confirmedBy: "usr-ic" },
  { id: "exp-9", daysAgo: 60, amount: "980.00", currency: "EUR", vendor: null, category: "cat-hardware", costCenter: "cc-euc", description: "Monitor stands (duplicate entry)", source: "manual", linkType: null, linkId: null, invoice: null, attachment: null, status: "void", enteredBy: "usr-mr", confirmedBy: null },
  { id: "exp-10", daysAgo: 75, amount: "11800.00", currency: "EUR", vendor: null, category: "cat-infra", costCenter: "cc-ops", description: "Core switch, Lisbon rack B1", source: "manual", linkType: null, linkId: null, invoice: "PO-2204", attachment: "cisco-2204.pdf", status: "confirmed", enteredBy: "usr-mr", confirmedBy: ME },
  { id: "exp-11", daysAgo: 110, amount: "38200.00", currency: "EUR", vendor: null, category: "cat-hardware", costCenter: "cc-euc", description: "Laptop refresh, wave 1", source: "manual", linkType: null, linkId: null, invoice: "PO-2140", attachment: "dell-inv-87102.pdf", status: "confirmed", enteredBy: "usr-mr", confirmedBy: ME },
];

function expense(seed: ExpenseSeed): ExpenseOut {
  const date = dateIn(-seed.daysAgo);
  const rate = seed.status === "void" ? null : rateFor(seed.currency, date);
  const amountBase = rate === null ? null : money(Math.round(cents(seed.amount) * Number(rate)));
  const month = date.slice(0, 7);
  return {
    id: seed.id,
    expense_date: date,
    amount: seed.amount,
    currency: seed.currency,
    fx_rate: rate,
    amount_base: amountBase,
    vendor_id: seed.vendor,
    category_id: seed.category,
    cost_center_id: seed.costCenter,
    description: seed.description,
    source: seed.source,
    link_type: seed.linkType,
    link_id: seed.linkId,
    period_key: month,
    invoice_ref: seed.invoice,
    has_attachment: seed.attachment !== null,
    invoice_filename: seed.attachment,
    status: seed.status,
    entered_by: seed.enteredBy,
    created_at: ago(seed.daysAgo),
    confirmed_by: seed.confirmedBy,
    confirmed_at: seed.confirmedBy ? ago(Math.max(0, seed.daysAgo - 1)) : null,
    voided_by: seed.status === "void" ? ME : null,
    voided_at: seed.status === "void" ? ago(Math.max(0, seed.daysAgo - 2)) : null,
  };
}

const expenses = (): ExpenseOut[] => EXPENSE_SEEDS.map(expense);

/* ----------------------------------------------------------------- reports */

/** The fiscal period (1-based month) an expense date falls in, January start. */
const periodOf = (date: string): number => Number(date.slice(5, 7));

function unconvertible(rows: ExpenseOut[]): { count: number; byCurrency: Record<string, string> } {
  const totals = new Map<string, number>();
  let count = 0;
  for (const row of rows) {
    if (row.status === "void" || row.amount_base !== null) continue;
    count += 1;
    totals.set(row.currency, (totals.get(row.currency) ?? 0) + cents(row.amount));
  }
  return { count, byCurrency: Object.fromEntries([...totals].map(([currency, total]) => [currency, money(total)])) };
}

const getBudgetVariance: FixtureMap["getBudgetVariance"] = (budgetId) => {
  const budget = BUDGETS.find((b) => b.id === budgetId);
  if (!budget) throw notFound("budget");
  const thisYear = budget.fiscal_year === FISCAL_YEAR;
  const ledger = thisYear ? expenses().filter((e) => e.expense_date.startsWith(String(FISCAL_YEAR))) : [];
  const rows: VarianceRow[] = [];
  for (const line of linesFor(budget)) {
    if (budget.granularity === "month" && thisYear && line.period_index > ELAPSED) continue;
    const inPeriod = ledger.filter((e) => e.category_id === line.category_id && periodOf(e.expense_date) === line.period_index);
    const actual = inPeriod.filter((e) => e.status === "confirmed" && e.amount_base !== null).reduce((sum, e) => sum + cents(e.amount_base as string), 0);
    const committed = inPeriod.filter((e) => e.status === "expected" && e.amount_base !== null).reduce((sum, e) => sum + cents(e.amount_base as string), 0);
    const planned = cents(line.planned_amount);
    const projected = actual + committed;
    const variance = projected - planned;
    rows.push({
      category_id: line.category_id,
      cost_center_id: null,
      period_index: line.period_index,
      planned: money(planned),
      actual: money(actual),
      committed: money(committed),
      projected: money(projected),
      variance: money(variance),
      variance_pct: planned === 0 ? null : ((variance / planned) * 100).toFixed(1),
    });
  }
  const gap = unconvertible(ledger);
  return { rows, unconvertible_count: gap.count, unconvertible_by_currency: gap.byCurrency };
};

const getBudgetPosition: FixtureMap["getBudgetPosition"] = (budgetId) => {
  const budget = BUDGETS.find((b) => b.id === budgetId);
  if (!budget) throw notFound("budget");
  const thisYear = budget.fiscal_year === FISCAL_YEAR;
  const ledger = thisYear ? expenses().filter((e) => e.expense_date.startsWith(String(FISCAL_YEAR))) : [];
  const ytd = ledger.filter((e) => e.status === "confirmed" && e.amount_base !== null).reduce((sum, e) => sum + cents(e.amount_base as string), 0);
  const elapsed = thisYear ? ELAPSED : budget.granularity === "quarter" ? 4 : 12;
  const planYtd = linesFor(budget)
    .filter((line) => line.period_index <= elapsed)
    .reduce((sum, line) => sum + cents(line.planned_amount), 0);
  const burn = elapsed > 0 ? Math.round(ytd / elapsed) : 0;
  const periods = budget.granularity === "quarter" ? 4 : 12;
  const gap = unconvertible(ledger);
  const position: PositionOut = {
    ytd_actual: money(ytd),
    pct_of_plan: planYtd === 0 ? null : ((ytd / planYtd) * 100).toFixed(1),
    avg_monthly_burn: money(burn),
    elapsed_periods: elapsed,
    projected_year_end: money(burn * periods),
    unconvertible_count: gap.count,
    unconvertible_by_currency: gap.byCurrency,
  };
  return position;
};

/* ------------------------------------------------------------------ reads */

const getBudgetSettings: FixtureMap["getBudgetSettings"] = () => SETTINGS;

const listBudgetCategories: FixtureMap["listBudgetCategories"] = (includeInactive = false) =>
  CATEGORIES.filter((c) => includeInactive || c.active);

const listCostCenters: FixtureMap["listCostCenters"] = (includeInactive = false) =>
  COST_CENTERS.filter((c) => includeInactive || c.active);

const listFxRates: FixtureMap["listFxRates"] = (fromCurrency) =>
  FX_RATES.filter((fx) => !fromCurrency || fx.from_currency === fromCurrency);

const listBudgets: FixtureMap["listBudgets"] = () => BUDGETS;

const listBudgetLines: FixtureMap["listBudgetLines"] = (budgetId) => {
  const budget = BUDGETS.find((b) => b.id === budgetId);
  if (!budget) throw notFound("budget");
  return linesFor(budget);
};

const listExpenses: FixtureMap["listExpenses"] = (filters = {}) =>
  expenses()
    .filter((e) => !filters.status || e.status === filters.status)
    .filter((e) => !filters.category_id || e.category_id === filters.category_id)
    .filter((e) => !filters.cost_center_id || e.cost_center_id === filters.cost_center_id)
    .filter((e) => !filters.vendor_id || e.vendor_id === filters.vendor_id)
    .filter((e) => !filters.source || e.source === filters.source)
    .filter((e) => !filters.date_from || e.expense_date >= filters.date_from)
    .filter((e) => !filters.date_to || e.expense_date <= filters.date_to)
    .sort((a, b) => (a.expense_date < b.expense_date ? 1 : -1));

/* ----------------------------------------------------------------- writes */

const updateBudgetSettings: FixtureMap["updateBudgetSettings"] = (body) => ({
  ...SETTINGS,
  base_currency: body.base_currency ?? SETTINGS.base_currency,
  fiscal_year_start_month: body.fiscal_year_start_month ?? SETTINGS.fiscal_year_start_month,
  updated_at: new Date().toISOString(),
});

const createBudgetCategory: FixtureMap["createBudgetCategory"] = (body) => ({
  id: `cat-${Date.now()}`,
  key: body.key,
  name: body.name,
  system_key: null,
  active: true,
  sort_order: body.sort_order ?? 50,
  created_at: new Date().toISOString(),
});

const updateBudgetCategory: FixtureMap["updateBudgetCategory"] = (id, body) => {
  const found = CATEGORIES.find((c) => c.id === id);
  if (!found) throw notFound("category");
  return { ...found, name: body.name ?? found.name, active: body.active ?? found.active, sort_order: body.sort_order ?? found.sort_order };
};

const createCostCenter: FixtureMap["createCostCenter"] = (body) => ({
  id: `cc-${Date.now()}`,
  code: body.code,
  name: body.name,
  external_ref: body.external_ref ?? null,
  active: true,
  created_at: new Date().toISOString(),
});

const updateCostCenter: FixtureMap["updateCostCenter"] = (id, body) => {
  const found = COST_CENTERS.find((c) => c.id === id);
  if (!found) throw notFound("cost centre");
  return { ...found, name: body.name ?? found.name, external_ref: body.external_ref === undefined ? found.external_ref : body.external_ref, active: body.active ?? found.active };
};

const createFxRate: FixtureMap["createFxRate"] = (body) => ({
  id: `fx-${Date.now()}`,
  from_currency: body.from_currency,
  to_currency: SETTINGS.base_currency,
  rate: body.rate,
  effective_date: body.effective_date,
  created_at: new Date().toISOString(),
});

const updateFxRate: FixtureMap["updateFxRate"] = (id, body) => {
  const found = FX_RATES.find((fx) => fx.id === id);
  if (!found) throw notFound("FX rate");
  return { ...found, rate: body.rate ?? found.rate };
};

const createBudget: FixtureMap["createBudget"] = (body) => ({
  id: `bud-${Date.now()}`,
  fiscal_year: body.fiscal_year,
  name: body.name,
  granularity: body.granularity,
  status: "draft",
  created_at: new Date().toISOString(),
});

const updateBudget: FixtureMap["updateBudget"] = (id, body) => {
  const found = BUDGETS.find((b) => b.id === id);
  if (!found) throw notFound("budget");
  return { ...found, name: body.name ?? found.name, status: body.status ?? found.status };
};

const closeBudget: FixtureMap["closeBudget"] = (id) => {
  const found = BUDGETS.find((b) => b.id === id);
  if (!found) throw notFound("budget");
  return { ...found, status: "closed" };
};

const putBudgetLines: FixtureMap["putBudgetLines"] = (budgetId, lines) => {
  const budget = BUDGETS.find((b) => b.id === budgetId);
  if (!budget) throw notFound("budget");
  const now = new Date().toISOString();
  return lines.map((line) => ({
    id: `${budgetId}-${line.category_id}-${line.period_index}`,
    budget_id: budgetId,
    category_id: line.category_id,
    cost_center_id: line.cost_center_id ?? null,
    period_index: line.period_index,
    planned_amount: line.planned_amount,
    created_at: now,
  }));
};

const deleteBudgetLine: FixtureMap["deleteBudgetLine"] = (budgetId) => {
  if (!BUDGETS.some((b) => b.id === budgetId)) throw notFound("budget");
  return undefined;
};

const createExpense: FixtureMap["createExpense"] = (body) =>
  expense({
    id: `exp-${Date.now()}`,
    daysAgo: 0,
    amount: body.amount,
    currency: body.currency,
    vendor: body.vendor_id ?? null,
    category: body.category_id,
    costCenter: body.cost_center_id ?? null,
    description: body.description ?? "",
    source: "manual",
    linkType: body.link_type ?? null,
    linkId: body.link_id ?? null,
    invoice: body.invoice_ref ?? null,
    attachment: null,
    status: body.status ?? "expected",
    enteredBy: ME,
    confirmedBy: null,
  });

const findExpense = (id: string): ExpenseOut => {
  const found = expenses().find((e) => e.id === id);
  if (!found) throw notFound("expense");
  return found;
};

const updateExpense: FixtureMap["updateExpense"] = (id, body) => {
  const found = findExpense(id);
  return {
    ...found,
    description: body.description ?? found.description,
    invoice_ref: body.invoice_ref === undefined ? found.invoice_ref : body.invoice_ref,
  };
};

const confirmExpense: FixtureMap["confirmExpense"] = (id, body) => {
  const found = findExpense(id);
  const amount = body.amount ?? found.amount;
  const rate = rateFor(found.currency, found.expense_date);
  return {
    ...found,
    amount,
    fx_rate: rate,
    amount_base: rate === null ? null : money(Math.round(cents(amount) * Number(rate))),
    status: "confirmed",
    confirmed_by: ME,
    confirmed_at: new Date().toISOString(),
  };
};

const voidExpense: FixtureMap["voidExpense"] = (id) => ({
  ...findExpense(id),
  status: "void",
  voided_by: ME,
  voided_at: new Date().toISOString(),
});

const materializeExpenses: FixtureMap["materializeExpenses"] = () => ({
  created: 2,
  skipped_existing: 3,
  skipped_unconvertible: 1,
});

const uploadExpenseAttachment: FixtureMap["uploadExpenseAttachment"] = (id, file) => ({
  ...findExpense(id),
  has_attachment: true,
  invoice_filename: file.name,
});

const fixtures: FixtureMap = {
  getBudgetSettings,
  updateBudgetSettings,
  listBudgetCategories,
  createBudgetCategory,
  updateBudgetCategory,
  listCostCenters,
  createCostCenter,
  updateCostCenter,
  listFxRates,
  createFxRate,
  updateFxRate,
  listBudgets,
  createBudget,
  updateBudget,
  closeBudget,
  listBudgetLines,
  putBudgetLines,
  deleteBudgetLine,
  getBudgetVariance,
  getBudgetPosition,
  listExpenses,
  createExpense,
  updateExpense,
  confirmExpense,
  voidExpense,
  materializeExpenses,
  uploadExpenseAttachment,
};

export default fixtures;
