import { readFileSync } from "node:fs";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me } from "../../../lib/api";
import { BudgetAdminPage } from "../BudgetAdminPage";
import { BudgetDashboard } from "../BudgetDashboard";
import { BudgetPlannerPage } from "../BudgetPlannerPage";
import { ConfirmationQueuePage } from "../ConfirmationQueuePage";
import { ExpensesPage } from "../ExpensesPage";
import {
  useBudgetCategories,
  useBudgetSettings,
  useBudgets,
  useCostCenters,
  useCreateBudgetCategoryMutation,
  useCreateBudgetMutation,
  useCreateCostCenterMutation,
  useCreateExpenseMutation,
  useExpenses,
  useMaterializeExpensesMutation,
  useUpdateBudgetSettingsMutation,
  useUpdateExpenseMutation,
} from "../api";
import {
  budgetStatusVariant,
  expenseSourceVariant,
  expenseStatusVariant,
} from "../budgetState";
import {
  CATEGORY_PICKER_EMPTY_HINT,
  COST_CENTER_PICKER_EMPTY_HINT,
  categoryPickerOptions,
  costCenterPickerOptions,
} from "../pickerOptions";
import type {
  BudgetLineOut,
  BudgetOut,
  CategoryOut,
  CostCenterOut,
  ExpenseOut,
  FxRateOut,
  PositionOut,
  VarianceOut,
} from "../types";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch -- mirrors modules/subscriptions/__tests__/subscriptions.test.tsx.
const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWithProviders(ui: ReactElement, initialEntries: string[] = ["/"]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

// THE STATIC ROUTES App.tsx ACTUALLY SERVES, read out of the route table
// rather than copied into this file. A copied list keeps passing after
// somebody renames a route -- which is the moment a tile goes dead.
//
// DYNAMIC ROUTES ARE EXCLUDED, AND THAT EXCLUSION IS WHAT GIVES THIS TEETH.
// `/licenses/:licenseId` matches "/licenses/breaches", so a typo'd tile would
// still "resolve" -- onto a detail page for an id that does not exist -- and a
// looser assertion here passed against exactly that, which is why it is
// written this way. `*` is excluded for the same reason twice over: it matches
// every string and it redirects to /devices. Every tile in this cluster points
// at a list or a report, so an exact static route is both the truth and
// falsifiable; a tile that one day needs a detail route will fail here, and
// its author can say so in the assertion rather than inheriting a check that
// never objected to anything.
function staticRoutePaths(): string[] {
  // `import.meta.url` under vitest is the module's dev-server URL, not a
  // file: one, so the path is taken from its pathname -- which is this file's
  // absolute path on disk -- and walked back to the src root.
  const here = new URL(import.meta.url).pathname;
  const source = readFileSync(here.replace(/\/src\/.*$/, "/src/App.tsx"), "utf8");
  return [...source.matchAll(/<Route\s[^>]*?path="([^"]+)"/g)]
    .flatMap((match) => (match[1] ? [match[1]] : []))
    .filter((path) => !path.includes("*") && !path.includes(":"));
}

function isServedRoute(href: string | null, routes: string[]): boolean {
  return href !== null && routes.includes(href);
}

const ME_FULL: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["budget.read", "budget.manage", "expense.record", "expense.confirm"],
};

/**
 * Choose an option from an `EntityPicker`, the way a person does: focus the
 * combobox, then click the row. The box FILTERS; it does not accept typed
 * text as a value.
 */
async function pick(
  user: ReturnType<typeof userEvent.setup>,
  pickerLabel: string,
  optionName: RegExp,
): Promise<void> {
  await user.click(screen.getByRole("combobox", { name: pickerLabel }));
  // Scoped to THIS picker's listbox: a `<select>` elsewhere on the page also
  // reports role `option`, and a helper that matched the first one anywhere
  // would pass while clicking the wrong control.
  const list = await screen.findByRole("listbox", { name: pickerLabel });
  await user.click(await within(list).findByRole("option", { name: optionName }));
}

/**
 * Press the confirm button in an open `ConfirmDialog`.
 *
 * Looked up INSIDE a `role="dialog"`, so a test cannot pass because the
 * confirmation was never rendered -- the whole point of cycle 8 is that these
 * clicks no longer go straight through.
 */
async function confirmWith(
  user: ReturnType<typeof userEvent.setup>,
  confirmLabel: string,
): Promise<void> {
  const dialogs = await screen.findAllByRole("dialog");
  const button = dialogs
    .map((dialog) => within(dialog).queryByRole("button", { name: confirmLabel }))
    .find((found): found is HTMLElement => found != null);
  if (button == null) throw new Error(`No open dialog offers "${confirmLabel}"`);
  await user.click(button);
}

const CATEGORIES: CategoryOut[] = [
  {
    id: "cat1",
    client_id: "o1",
    key: "software_licenses",
    name: "Software licenses",
    system_key: "software_licenses",
    active: true,
    sort_order: 0,
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const COST_CENTERS: CostCenterOut[] = [];

const BUDGETS: BudgetOut[] = [
  {
    id: "b1",
    client_id: "o1",
    fiscal_year: 2026,
    name: "FY26 IT Budget",
    granularity: "month",
    status: "active",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const EXPENSE_EXPECTED: ExpenseOut = {
  id: "e1",
  client_id: "o1",
  expense_date: "2026-07-01",
  amount: "1234.56",
  currency: "USD",
  fx_rate: null,
  amount_base: null,
  vendor_id: null,
  category_id: "cat1",
  cost_center_id: null,
  description: "",
  source: "manual",
  link_type: null,
  link_id: null,
  period_key: null,
  invoice_ref: null,
  has_attachment: false,
  invoice_filename: null,
  status: "expected",
  entered_by: "u1",
  created_at: "2026-07-01T00:00:00.000Z",
  confirmed_by: null,
  confirmed_at: null,
  voided_by: null,
  voided_at: null,
};

const EMPTY_VARIANCE: VarianceOut = { rows: [], unconvertible_count: 0, unconvertible_by_currency: {} };

const POSITION_WITH_UNCONVERTIBLE: PositionOut = {
  ytd_actual: "1234.56",
  pct_of_plan: "45.00",
  avg_monthly_burn: "200.00",
  elapsed_periods: 6,
  projected_year_end: "2400.00",
  unconvertible_count: 2,
  unconvertible_by_currency: { EUR: "150.00" },
};

const POSITION_CLEAN: PositionOut = {
  ytd_actual: "1234.56",
  pct_of_plan: "45.00",
  avg_monthly_burn: "200.00",
  elapsed_periods: 6,
  projected_year_end: "2400.00",
  unconvertible_count: 0,
  unconvertible_by_currency: {},
};

const COST_CENTER_ONE: CostCenterOut[] = [
  {
    id: "cc1",
    client_id: "o1",
    code: "OPS",
    name: "Operations",
    external_ref: null,
    active: true,
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const FX_RATE_ONE: FxRateOut[] = [
  {
    id: "fx1",
    from_currency: "EUR",
    to_currency: "USD",
    rate: "1.10000000",
    effective_date: "2026-01-01",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const BUDGET_LINES: BudgetLineOut[] = [
  {
    id: "line1",
    budget_id: "b1",
    category_id: "cat1",
    cost_center_id: null,
    period_index: 1,
    planned_amount: "500.00",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

describe("BudgetDashboard navigation", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgets: vi.fn().mockResolvedValue(BUDGETS),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      getBudgetVariance: vi.fn().mockResolvedValue(EMPTY_VARIANCE),
      getBudgetPosition: vi.fn().mockResolvedValue(POSITION_CLEAN),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // The Phase-12 lesson: a page routed but unlinked is not reachable. All
  // five budget surfaces are linked from the same module nav rendered on
  // every budget page (BudgetPageNav, sourced from BUDGET_NAV_ITEMS).
  it("links to every budget surface from the module nav", async () => {
    renderWithProviders(<BudgetDashboard />);

    await screen.findByText("Budget dashboard");
    // `role="tab"` now, not "link": Auditor B, [MEDIUM] -- "role = null on
    // every tab, aria-selected = null on every tab, and the container's role =
    // null (no tablist) ... A screen reader user hears five unrelated buttons
    // with no indication that they form a set". They are still anchors with a
    // real `href`, which is the [LOW] beside it ("cannot be middle-clicked,
    // opened in a new tab, or copied via right-click").
    const tabs = within(await screen.findByRole("tablist", { name: "Budget" })).getAllByRole("tab");
    expect(tabs.map((tab) => tab.getAttribute("href"))).toEqual([
      "/budget",
      "/budget/planner",
      "/budget/expenses",
      "/budget/confirm",
      "/budget/admin",
    ]);
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      "Dashboard",
      "Planner",
      "Expenses",
      "Confirm queue",
      "Admin",
    ]);
    // A `<button>` nested inside an `<a>` was two interactive elements, one of
    // them unreachable as a link.
    expect(document.querySelector(".tabs button")).toBeNull();
  });

  it("says which tab is the current one, in both conventions", async () => {
    renderWithProviders(<BudgetDashboard />, ["/budget"]);

    await screen.findByText("Budget dashboard");
    const tabs = within(await screen.findByRole("tablist", { name: "Budget" })).getAllByRole("tab");
    expect(tabs.map((tab) => tab.getAttribute("aria-selected"))).toEqual([
      "true",
      "false",
      "false",
      "false",
      "false",
    ]);
    expect(tabs[0]).toHaveAttribute("aria-current", "page");
  });

  it("does not mark Dashboard active on the sibling budget routes", async () => {
    // `/budget` is a prefix of all four others, so a `startsWith` match would
    // light the Dashboard tab on every page in the module.
    renderWithProviders(<BudgetDashboard />, ["/budget/expenses"]);

    await screen.findByText("Budget dashboard");
    const tabs = within(await screen.findByRole("tablist", { name: "Budget" })).getAllByRole("tab");
    expect(tabs[0]).toHaveAttribute("aria-selected", "false");
    expect(tabs[2]).toHaveAttribute("aria-selected", "true");
  });

  it("hides budget navigation without budget.read", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME_FULL,
      permissions: ME_FULL.permissions.filter((p) => p !== "budget.read"),
    });
    renderWithProviders(<BudgetDashboard />);

    await screen.findByText("Budget dashboard");
    expect(screen.queryByRole("link", { name: "Planner" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Expenses" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Confirm queue" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Budget" })).not.toBeInTheDocument();
  });

  // "i dont want dead tiles everywhere". Three of the four position figures
  // are computed from recorded expenses, so they open the ledger; "% of plan"
  // is the one whose other half is the plan, so it opens the Planner.
  // "1,234.56" NOT "1234.56": the money tiles print a compact figure and the
  // exact amount under it, grouped -- v3's tile row for this page draws a
  // short human figure, and the raw Decimal string glued to an ISO code was
  // the review finding. This describe mocks no `getBudgetSettings`, so the
  // base currency is unknown and neither line carries a symbol or a code.
  it("gives every position tile a link to the page its number lives on", async () => {
    renderWithProviders(<BudgetDashboard />);
    await screen.findByText("1,234.56");

    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles).toHaveLength(4);
    expect(tiles.map((tile) => tile.tagName)).toEqual(["A", "A", "A", "A"]);
    expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([
      "/budget/expenses",
      "/budget/planner",
      "/budget/expenses",
      "/budget/expenses",
    ]);

    const routes = staticRoutePaths();
    expect(routes.length).toBeGreaterThan(10);
    for (const tile of tiles) {
      expect(isServedRoute(tile.getAttribute("href"), routes)).toBe(true);
    }
  });

  // Nothing on this row is a threshold the server has evaluated, so nothing on
  // it shouts. Over-budget is a judgement the variance table below can make
  // and these four numbers cannot -- a burn figure wearing an alarm colour
  // would be the page asserting something it has not measured.
  it("gives no position tile the urgent weight", async () => {
    renderWithProviders(<BudgetDashboard />);
    await screen.findByText("1,234.56");

    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles.some((tile) => tile.classList.contains("urgent"))).toBe(false);
  });

  // spec §3: every aggregate that sums `amount_base` also discloses what it
  // could not convert -- a total is never quietly understated, and this must
  // never be hidden behind a tooltip.
  it("shows the unconvertible disclosure when the API reports one", async () => {
    api.getBudgetPosition = vi.fn().mockResolvedValue(POSITION_WITH_UNCONVERTIBLE);
    renderWithProviders(<BudgetDashboard />);

    await screen.findByText("YTD actual");
    expect(
      await screen.findByText(
        "2 expenses could not be converted to the base currency and are excluded from the position figures above.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("150.00 EUR")).toBeInTheDocument();
    // The fix beside the finding: a rate is what brings them back into the totals.
    expect(screen.getByRole("link", { name: "Add an exchange rate" })).toHaveAttribute(
      "href",
      "/budget/admin#fx-rates",
    );
  });

  it("renders no disclosure when the API reports zero unconvertible rows", async () => {
    renderWithProviders(<BudgetDashboard />);

    await screen.findByText("YTD actual");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("ExpensesPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listCostCenters: vi.fn().mockResolvedValue(COST_CENTERS),
      listExpenses: vi.fn().mockResolvedValue([EXPENSE_EXPECTED]),
      createExpense: vi.fn().mockResolvedValue(EXPENSE_EXPECTED),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // Money is a Decimal server-side and travels as a JSON string -- rendered
  // verbatim here, never coerced through Number() (invariant #6).
  //
  // Q3 of the post-review controller fix (.git/sdd/task-13-report.md):
  // "1234.56" is deliberately NOT used for this assertion -- Number("1234.56")
  // .toString() is byte-identical to "1234.56", so a `Number(expense.amount)`
  // regression would pass this test unchanged. "10.50" is not round-trip
  // safe through Number() (it becomes "10.5"), which is what makes this test
  // load-bearing against exactly that regression -- verified by making the
  // substitution, watching this test fail, and restoring (see fix report).
  it("renders money as strings without float arithmetic", async () => {
    api.listExpenses = vi.fn().mockResolvedValue([{ ...EXPENSE_EXPECTED, amount: "10.50" }]);
    renderWithProviders(<ExpensesPage />);

    expect(await screen.findByText("10.50 USD")).toBeInTheDocument();
  });

  // Wiring test in the Phase-12 style: the dialog is driven the way a user
  // drives it (by label and role), and the exact create body is asserted --
  // a form that renders but is wired to nothing fails here.
  it("submits a new expense with the entered values", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ExpensesPage />);

    await user.click(await screen.findByRole("button", { name: "New expense" }));

    // The expense-date field keeps its pre-filled default (today, in
    // ISO/yyyy-mm-dd form) -- interacting with a native <input type="date">
    // through userEvent.type is unreliable under jsdom, and this test does
    // not assert on the date field anyway.
    await user.clear(screen.getByLabelText("Amount"));
    await user.type(screen.getByLabelText("Amount"), "1234.56");
    await user.clear(screen.getByLabelText("Currency"));
    await user.type(screen.getByLabelText("Currency"), "USD");
    await pick(user, "Category", /Software licenses/);
    await user.type(screen.getByLabelText("Description"), "Renewal invoice");
    await user.type(screen.getByLabelText("Invoice reference"), "INV-100");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createExpense).toHaveBeenCalledWith(
      expect.objectContaining({ amount: "1234.56", currency: "USD" }),
    );
  });

  it("hides the New expense control without expense.record", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME_FULL,
      permissions: ME_FULL.permissions.filter((p) => p !== "expense.record"),
    });
    renderWithProviders(<ExpensesPage />);

    await screen.findByText("1234.56 USD");
    expect(screen.queryByRole("button", { name: "New expense" })).not.toBeInTheDocument();
  });

  // Q1 of the post-review controller fix (.git/sdd/task-13-report.md):
  // `useUpdateExpenseMutation` was defined and never called from any page.
  // Wired here as the row-level "Edit" control (description/invoice
  // reference only -- amount/currency/category edits re-run FX conversion
  // server-side and are deliberately out of this dialog's scope).
  it("edits an expense's description and invoice reference", async () => {
    api.updateExpense = vi.fn().mockResolvedValue({ ...EXPENSE_EXPECTED, description: "Updated" });
    const user = userEvent.setup();
    renderWithProviders(<ExpensesPage />);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.clear(screen.getByLabelText("Description"));
    await user.type(screen.getByLabelText("Description"), "Updated description");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.updateExpense).toHaveBeenCalledWith("e1", {
      description: "Updated description",
      invoice_ref: null,
    });
  });

  // Q1: `useMaterializeExpensesMutation` was defined and never called.
  // Materialization is the module's headline auto-feed (spec §6.3) and is
  // deliberately an explicit operator action, never a background sweep --
  // the three returned counts are the whole point, since an operator who
  // cannot see them cannot tell a no-op from a failure.
  it("materializes expenses over a date window and shows the returned counts", async () => {
    api.materializeExpenses = vi
      .fn()
      .mockResolvedValue({ created: 3, skipped_existing: 1, skipped_unconvertible: 2 });
    const user = userEvent.setup();
    renderWithProviders(<ExpensesPage />);

    const today = new Date().toISOString().slice(0, 10);
    await user.click(await screen.findByRole("button", { name: "Add expected expenses" }));
    await user.click(screen.getByRole("button", { name: "Run" }));

    // The internal client's subscriptions and licenses (PHASE 3: the
    // selector's client), `/me`'s `org_id`.
    expect(api.materializeExpenses).toHaveBeenCalledWith({
      client_id: "o1",
      from_date: today,
      to_date: today,
    });
    expect(
      await screen.findByText("Added 3. Skipped 1 already recorded and 2 with no exchange rate."),
    ).toBeInTheDocument();
  });
});

// Minor fix (whole-branch review): `useCreateExpenseMutation`/
// `useUpdateExpenseMutation` invalidated only ["budget-expenses"], while
// confirm/void also invalidate ["budget-variance"]/["budget-position"].
// Creating a `confirmed` expense or editing an amount changes
// actual/committed, which those two reports sum -- without the extra
// invalidation the dashboard shows stale numbers after either action.
describe("useCreateExpenseMutation / useUpdateExpenseMutation invalidation", () => {
  function wrapperFor(queryClient: QueryClient) {
    return function Wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
    };
  }

  it("invalidates budget-expenses, budget-variance, and budget-position on create", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // `/me` already answered: the expense is the internal client's.
    queryClient.setQueryData(["me"], ME_FULL);
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const api = { createExpense: vi.fn().mockResolvedValue(EXPENSE_EXPECTED) };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);

    const { result } = renderHook(() => useCreateExpenseMutation(), {
      wrapper: wrapperFor(queryClient),
    });
    await act(async () => {
      await result.current.mutateAsync({
        expense_date: "2026-01-15",
        amount: "100.00",
        currency: "USD",
        category_id: "cat1",
        status: "confirmed",
      });
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["budget-expenses"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["budget-variance"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["budget-position"] });
  });

  it("invalidates budget-expenses, budget-variance, and budget-position on update", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const api = { updateExpense: vi.fn().mockResolvedValue(EXPENSE_EXPECTED) };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);

    const { result } = renderHook(() => useUpdateExpenseMutation(), {
      wrapper: wrapperFor(queryClient),
    });
    await act(async () => {
      await result.current.mutateAsync({ id: "e1", body: { amount: "150.00" } });
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["budget-expenses"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["budget-variance"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["budget-position"] });
  });

  // N2 (whole-branch review): `useMaterializeExpensesMutation` invalidated
  // only ["budget-expenses"]. Materialization moves money from projected to
  // committed, exactly like confirm/void/update above -- without the extra
  // invalidation the dashboard shows pre-materialization figures until a
  // manual refresh.
  it("invalidates budget-expenses, budget-variance, and budget-position on materialize", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // `/me` already answered: the materialization is the internal client's.
    queryClient.setQueryData(["me"], ME_FULL);
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const api = {
      materializeExpenses: vi
        .fn()
        .mockResolvedValue({ created: 1, skipped_existing: 0, skipped_unconvertible: 0 }),
    };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);

    const { result } = renderHook(() => useMaterializeExpensesMutation(), {
      wrapper: wrapperFor(queryClient),
    });
    await act(async () => {
      await result.current.mutateAsync({ from_date: "2026-01-01", to_date: "2026-01-31" });
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["budget-expenses"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["budget-variance"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["budget-position"] });
  });
});

describe("ConfirmationQueuePage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listExpenses: vi.fn().mockResolvedValue([EXPENSE_EXPECTED]),
      confirmExpense: vi.fn().mockResolvedValue({ ...EXPENSE_EXPECTED, status: "confirmed" }),
      voidExpense: vi.fn().mockResolvedValue({ ...EXPENSE_EXPECTED, status: "void" }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // Wiring test in the Phase-12 style: confirms with a CORRECTED amount that
  // differs from the projected one recorded on the expense (spec §0.3/§5).
  it("confirms an expected expense with a corrected amount", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfirmationQueuePage />);

    const label = "Corrected amount for Software licenses expense on 2026-07-01";
    await screen.findByLabelText(label);
    await user.clear(screen.getByLabelText(label));
    await user.type(screen.getByLabelText(label), "999.99");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    // Cycle 8: confirming commits the amount in the box beside the row as the
    // real cost, and a confirmed expense can never be re-confirmed or
    // re-valued -- so the figure is read back before it lands.
    await confirmWith(user, "Confirm expense");

    expect(api.confirmExpense).toHaveBeenCalledWith("e1", { amount: "999.99" });
  });

  it("voids an expected expense", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfirmationQueuePage />);

    await screen.findByText("Software licenses");
    await user.click(screen.getByRole("button", { name: "Void" }));
    await confirmWith(user, "Void expense");

    expect(api.voidExpense).toHaveBeenCalledWith("e1");
  });

  it("hides confirm controls without expense.confirm", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME_FULL,
      permissions: ME_FULL.permissions.filter((p) => p !== "expense.confirm"),
    });
    renderWithProviders(<ConfirmationQueuePage />);

    await screen.findByText("Software licenses");
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Void" })).not.toBeInTheDocument();
    // Read-only view of the projected amount still shows.
    expect(screen.getByText("1234.56 USD")).toBeInTheDocument();
  });
});

// Q1 of the post-review controller fix (.git/sdd/task-13-report.md):
// `useUpdateBudgetMutation`, `useCloseBudgetMutation`, and
// `useDeleteBudgetLineMutation` were all defined and never called from any
// page. Wired here into the Planner's budget-detail section and line grid.
describe("BudgetPlannerPage wiring", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgets: vi.fn().mockResolvedValue(BUDGETS),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listBudgetLines: vi.fn().mockResolvedValue(BUDGET_LINES),
      updateBudget: vi.fn().mockResolvedValue({ ...BUDGETS[0], name: "FY26 Budget Renamed" }),
      closeBudget: vi.fn().mockResolvedValue({ ...BUDGETS[0], status: "closed" }),
      deleteBudgetLine: vi.fn().mockResolvedValue(undefined),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("says why there is no New budget button, rather than leaving a dead end", async () => {
    // THE COPY PASS CREATED THIS GAP AND THE FIXER REPORTED IT. Cutting the
    // `budget.manage` sentence out of the dashboard's "No budget yet" empty
    // state was right -- an empty state is one sentence and an action. But its
    // action is a link to THIS page, which renders "New budget" only for
    // `budget.manage`, so a reader without it arrived at a page with no button
    // and nothing to explain it. A dead end is a worse defect than the long
    // sentence that was removed.
    api.me = vi.fn().mockResolvedValue({
      ...ME_FULL,
      permissions: ME_FULL.permissions.filter((p) => p !== "budget.manage"),
    });
    renderWithProviders(<BudgetPlannerPage />);

    expect(await screen.findByText(/Creating or editing a budget needs/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New budget" })).toBeNull();
  });

  it("says nothing about the permission to an operator who has it", async () => {
    // NON-VACUITY, and it is the half that keeps the line honest: a note shown
    // unconditionally would satisfy the case above while telling every
    // operator a permission code they can already see they hold.
    renderWithProviders(<BudgetPlannerPage />);

    expect(await screen.findByRole("button", { name: "New budget" })).toBeInTheDocument();
    expect(screen.queryByText(/Creating or editing a budget needs/)).toBeNull();
  });

  it("updates a budget's name and status", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BudgetPlannerPage />);

    await user.click(await screen.findByRole("button", { name: "FY26 IT Budget" }));
    await screen.findByText("Budget details");
    await user.clear(screen.getByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "FY26 Budget Renamed");
    await user.selectOptions(screen.getByLabelText("Status"), "draft");
    // "Save details" is "Save" since the form kernel landed: `FormActions`
    // derives its in-flight label from this one ("Save" -> "Saving…"), and the
    // panel around it already says "Budget details".
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.updateBudget).toHaveBeenCalledWith("b1", {
      name: "FY26 Budget Renamed",
      status: "draft",
    });
  });

  // The consequence ("closing freezes all line edits and cannot be undone")
  // must be stated in the UI right next to the control that causes it, per
  // the fix directive -- checked alongside the call, not instead of it.
  it("closes a budget, stating the consequence in the UI", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BudgetPlannerPage />);

    await user.click(await screen.findByRole("button", { name: "FY26 IT Budget" }));
    await screen.findByText("Budget details");
    expect(screen.getByText(/Closing freezes all line edits/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close budget" }));
    // Closing is one-way with no reopen route anywhere in the platform, so the
    // confirmation asks for the budget's name to be typed back.
    await user.type(screen.getByLabelText(/Type/), "FY26 IT Budget (FY2026)");
    await confirmWith(user, "Close budget");

    expect(api.closeBudget).toHaveBeenCalledWith("b1");
  });

  it("deletes a persisted budget line", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BudgetPlannerPage />);

    await user.click(await screen.findByRole("button", { name: "FY26 IT Budget" }));
    await user.click(
      await screen.findByRole("button", { name: "Delete Software licenses period 1 line" }),
    );
    await confirmWith(user, "Delete line");

    expect(api.deleteBudgetLine).toHaveBeenCalledWith("b1", "line1");
  });
});

// UX remediation cycle 3, the two HIGH findings on this page. Both had the
// same cause -- `disabled={create.isPending || !name}` on a dialog with no
// <form> and no validation feedback -- so both are pinned here: the control
// must be LIVE, the click must produce the reason, and nothing may reach the
// API until the reason is gone.
describe("BudgetPlannerPage form validation", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgets: vi.fn().mockResolvedValue(BUDGETS),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listBudgetLines: vi.fn().mockResolvedValue(BUDGET_LINES),
      createBudget: vi.fn().mockResolvedValue(BUDGETS[0]),
      updateBudget: vi.fn().mockResolvedValue(BUDGETS[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openNewBudget() {
    const user = userEvent.setup();
    renderWithProviders(<BudgetPlannerPage />);
    await user.click(await screen.findByRole("button", { name: "New budget" }));
    return user;
  }

  // Auditor (HIGH), /budget → New budget line → Save: "Empty submit is a
  // silent no-op." The click used to land on a disabled control -- nothing
  // moved and no field was named.
  it("names the missing field on an empty submit instead of disabling Save", async () => {
    const user = await openNewBudget();

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByRole("alert")).toHaveTextContent("Name is required");
    expect(api.createBudget).not.toHaveBeenCalled();
  });

  // Auditor (HIGH), /budget → planner → Fiscal year: "Fiscal year is an
  // unbounded number input -- it accepted -3000." It travelled, and the 422 it
  // came back as was rendered raw.
  it("refuses a negative fiscal year rather than sending it", async () => {
    const user = await openNewBudget();

    await user.type(screen.getByLabelText("Name"), "FY26 IT Budget");
    await user.clear(screen.getByLabelText("Fiscal year"));
    await user.type(screen.getByLabelText("Fiscal year"), "-3000");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Fiscal year cannot be negative/);
    expect(api.createBudget).not.toHaveBeenCalled();
  });

  // The range is the server's own (`_FiscalYear = Field(ge=2000, le=2100)`,
  // spec §8), so a positive year outside it is refused here too rather than
  // being discovered by pydantic.
  it("refuses a fiscal year outside the range the API accepts", async () => {
    const user = await openNewBudget();

    await user.type(screen.getByLabelText("Name"), "FY26 IT Budget");
    await user.clear(screen.getByLabelText("Fiscal year"));
    await user.type(screen.getByLabelText("Fiscal year"), "1999");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/between 2000 and 2100/);
    expect(api.createBudget).not.toHaveBeenCalled();
  });

  it("refuses a cleared fiscal year rather than sending NaN", async () => {
    const user = await openNewBudget();

    await user.type(screen.getByLabelText("Name"), "FY26 IT Budget");
    await user.clear(screen.getByLabelText("Fiscal year"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Fiscal year is required");
    expect(api.createBudget).not.toHaveBeenCalled();
  });

  // The valid path, so the rules above cannot be satisfied by a dialog that
  // refuses everything -- and it is reached with Enter, which did nothing at
  // all before there was a <form> here.
  it("creates the budget on Enter once the fields are valid", async () => {
    const user = await openNewBudget();

    await user.clear(screen.getByLabelText("Fiscal year"));
    await user.type(screen.getByLabelText("Fiscal year"), "2026");
    await user.type(screen.getByLabelText("Name"), "FY26 IT Budget{Enter}");

    expect(api.createBudget).toHaveBeenCalledWith({
      fiscal_year: 2026,
      name: "FY26 IT Budget",
      granularity: "month",
      client_id: "o1",
    });
  });

  // Same defect one panel down: the rename could be emptied and the only
  // response was a control that stopped working.
  it("names the missing field when Budget details is saved with an empty name", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BudgetPlannerPage />);

    await user.click(await screen.findByRole("button", { name: "FY26 IT Budget" }));
    await screen.findByText("Budget details");
    await user.clear(screen.getByLabelText("Name"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Name is required");
    expect(api.updateBudget).not.toHaveBeenCalled();
  });
});

// Q1: `useUpdateBudgetCategoryMutation`, `useUpdateCostCenterMutation`, and
// `useUpdateFxRateMutation` were all defined and never called. Wired here as
// per-row inline edit on the admin page, gated on `budget.manage`.
describe("BudgetAdminPage wiring", () => {
  function baseApi(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME_FULL),
      getBudgetSettings: vi.fn().mockResolvedValue({
        base_currency: "USD",
        fiscal_year_start_month: 1,
        updated_by: null,
        updated_at: "2026-01-01T00:00:00.000Z",
      }),
      listBudgetCategories: vi.fn().mockResolvedValue([]),
      listCostCenters: vi.fn().mockResolvedValue([]),
      listFxRates: vi.fn().mockResolvedValue([]),
      ...overrides,
    };
  }

  // The setting is a month, and "Month 1" is the column it is stored in
  // rather than the fact it records. The design file writes the same setting
  // as "1 January".
  it("names the fiscal year's start month instead of printing its number", async () => {
    mockUseApi.mockReturnValue(baseApi() as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    // In the settings' key/value list, not the month picker's options.
    expect(await screen.findByText("1 January", { selector: "dd" })).toBeInTheDocument();
    expect(screen.queryByText("Month 1")).not.toBeInTheDocument();
  });

  // The dashboard's unconvertible notice links to `#fx-rates`; the anchor has
  // to exist and hold the FX rates panel, or the link lands at the page top.
  it("anchors the FX rates panel for the dashboard's link", async () => {
    mockUseApi.mockReturnValue(baseApi() as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    await screen.findByText("1 January", { selector: "dd" });
    const anchor = document.getElementById("fx-rates");
    expect(anchor).not.toBeNull();
    expect(anchor).toHaveTextContent("FX rates");
  });

  it("renames a category and toggles it inactive", async () => {
    const user = userEvent.setup();
    const api = baseApi({
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      updateBudgetCategory: vi
        .fn()
        .mockResolvedValue({ ...CATEGORIES[0], name: "Renamed", active: false }),
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.clear(screen.getByLabelText("Category name"));
    await user.type(screen.getByLabelText("Category name"), "Renamed");
    await user.click(screen.getByLabelText("Category active"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.updateBudgetCategory).toHaveBeenCalledWith("cat1", {
      name: "Renamed",
      active: false,
    });
  });

  it("edits a cost center's name", async () => {
    const user = userEvent.setup();
    const api = baseApi({
      listCostCenters: vi.fn().mockResolvedValue(COST_CENTER_ONE),
      updateCostCenter: vi.fn().mockResolvedValue({ ...COST_CENTER_ONE[0], name: "Renamed CC" }),
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.clear(screen.getByLabelText("Cost center name"));
    await user.type(screen.getByLabelText("Cost center name"), "Renamed CC");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.updateCostCenter).toHaveBeenCalledWith("cc1", {
      name: "Renamed CC",
      active: true,
    });
  });

  it("edits an FX rate", async () => {
    const user = userEvent.setup();
    const api = baseApi({
      listFxRates: vi.fn().mockResolvedValue(FX_RATE_ONE),
      updateFxRate: vi.fn().mockResolvedValue({ ...FX_RATE_ONE[0], rate: "1.25000000" }),
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.clear(screen.getByLabelText("FX rate"));
    await user.type(screen.getByLabelText("FX rate"), "1.25000000");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.updateFxRate).toHaveBeenCalledWith("fx1", { rate: "1.25000000" });
  });
});

describe("budget state vocabulary", () => {
  it("maps a real backend expense status to a non-fallback badge variant", () => {
    // Expense.status vocabulary is sourced from the backend
    // (backend/app/modules/budget/schema.py's ExpenseStatus Literal) --
    // "confirmed" is a real status value.
    expect(expenseStatusVariant("confirmed")).not.toBe("muted");
    expect(expenseStatusVariant("confirmed")).toBe("success");
  });

  it("falls back to muted for an unknown/invented expense status", () => {
    expect(expenseStatusVariant("not_a_real_status")).toBe("muted");
  });

  it("maps a real backend budget status to a non-fallback badge variant", () => {
    expect(budgetStatusVariant("active")).toBe("success");
  });

  it("falls back to muted for an unknown/invented budget status", () => {
    expect(budgetStatusVariant("not_a_real_status")).toBe("muted");
  });

  it("maps a real backend expense source to a non-fallback badge variant", () => {
    expect(expenseSourceVariant("subscription")).toBe("info");
  });

  it("falls back to muted for an unknown/invented expense source", () => {
    expect(expenseSourceVariant("not_a_real_source")).toBe("muted");
  });
});

// ---------------------------------------------------------------------------
// UX remediation cycles 6, 7 and 8 on this module.
// ---------------------------------------------------------------------------

// The Category `<select>` held one option -- "Select a category" -- until
// somebody had visited the Admin tab, above a Save disabled by `!categoryId`:
// the product-wide first-run pattern Auditor B filed as [HIGH] on /licenses
// and /sla, here on the module's primary create action.
describe("New expense: the empty-lookup blocked flow", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgetCategories: vi.fn().mockResolvedValue([]),
      listCostCenters: vi.fn().mockResolvedValue([]),
      listExpenses: vi.fn().mockResolvedValue([]),
      createExpense: vi.fn().mockResolvedValue(EXPENSE_EXPECTED),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openDialog() {
    const user = userEvent.setup();
    renderWithProviders(<ExpensesPage />);
    await user.click(await screen.findByRole("button", { name: "New expense" }));
    return user;
  }

  it("says what would fill an empty category list instead of offering a dead one", async () => {
    const user = await openDialog();

    await user.click(screen.getByRole("combobox", { name: "Category" }));

    const list = await screen.findByRole("listbox", { name: "Category" });
    expect(list).toHaveTextContent(/No budget categories yet/i);
    expect(list).toHaveTextContent(/Budget → Admin/);
    // `expense.record` does not imply `budget.manage`, so the reader may not
    // be able to create one themselves -- the hint must not pretend otherwise.
    expect(list).toHaveTextContent(/permission/i);
  });

  it("links to the tab that creates one", async () => {
    await openDialog();

    expect(await screen.findByRole("link", { name: /Create a budget category/ })).toHaveAttribute(
      "href",
      "/budget/admin",
    );
  });

  it("keeps Save live and names the field, instead of a control that will not move", async () => {
    const user = await openDialog();

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByRole("alert")).toHaveTextContent("Category is required");
    expect(api.createExpense).not.toHaveBeenCalled();
  });
});

describe("New expense: money that the server can actually parse", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listCostCenters: vi.fn().mockResolvedValue(COST_CENTERS),
      listExpenses: vi.fn().mockResolvedValue([]),
      createExpense: vi.fn().mockResolvedValue(EXPENSE_EXPECTED),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openDialog() {
    const user = userEvent.setup();
    renderWithProviders(<ExpensesPage />);
    await user.click(await screen.findByRole("button", { name: "New expense" }));
    return user;
  }

  it("refuses a thousands separator rather than sending it to be 422'd", async () => {
    const user = await openDialog();

    await pick(user, "Category", /Software licenses/);
    await user.clear(screen.getByLabelText("Amount"));
    await user.type(screen.getByLabelText("Amount"), "1,234.56");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Amount must be a figure/);
    expect(api.createExpense).not.toHaveBeenCalled();
  });

  it("refuses a currency that is not a 3-letter code", async () => {
    const user = await openDialog();

    await pick(user, "Category", /Software licenses/);
    await user.clear(screen.getByLabelText("Currency"));
    await user.type(screen.getByLabelText("Currency"), "US");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/3-letter code/);
    expect(api.createExpense).not.toHaveBeenCalled();
  });

  // Auditor B's [BLOCKER], raised "on the strength of the pattern": this
  // backend registers no custom 422 handler, and `String(create.error)`
  // printed the payload -- framework, internal field names and all.
  it("translates a 422 body into a sentence instead of printing the payload", async () => {
    const user = await openDialog();
    api.createExpense = vi.fn().mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: [
            {
              type: "decimal_parsing",
              loc: ["body", "amount"],
              msg: "Input should be a valid decimal",
              input: "12.345",
            },
          ],
        }),
      ),
    );

    await pick(user, "Category", /Software licenses/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Amount: Input should be a valid decimal");
    expect(alert.textContent).not.toContain("decimal_parsing");
    // `input` echoes what was submitted and is never rendered.
    expect(alert.textContent).not.toContain("12.345");
  });

  it("announces a bodyless 503 as a sentence rather than \"ApiError:\"", async () => {
    const user = await openDialog();
    api.createExpense = vi.fn().mockRejectedValue(new ApiError(503, ""));

    await pick(user, "Category", /Software licenses/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");
  });

  // Cycle 5. `expense_date` is a `date`: rendered in the product's one format
  // and pinned to UTC, so the day recorded is the day shown.
  it("renders the expense date in the product's format, not as a raw ISO string", async () => {
    api.listExpenses = vi.fn().mockResolvedValue([EXPENSE_EXPECTED]);
    renderWithProviders(<ExpensesPage />);

    expect(await screen.findByText("01 Jul 2026")).toBeInTheDocument();
    expect(screen.queryByText("2026-07-01")).not.toBeInTheDocument();
  });
});

// Cycle 8. Both row actions were terminal server-side and both fired on one
// click, side by side, on rows that look alike.
describe("ConfirmationQueuePage: confirm and void ask first", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listExpenses: vi.fn().mockResolvedValue([EXPENSE_EXPECTED]),
      confirmExpense: vi.fn().mockResolvedValue({ ...EXPENSE_EXPECTED, status: "confirmed" }),
      voidExpense: vi.fn().mockResolvedValue({ ...EXPENSE_EXPECTED, status: "void" }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("confirms nothing on the first click, and reads the amount back", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfirmationQueuePage />);

    const label = "Corrected amount for Software licenses expense on 2026-07-01";
    await screen.findByLabelText(label);
    await user.clear(screen.getByLabelText(label));
    await user.type(screen.getByLabelText(label), "999.99");
    await user.click(screen.getByRole("button", { name: "Confirm" }));

    expect(api.confirmExpense).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog", { name: /Confirm this expense/i });
    // The EDITED figure, not the projected one: what is being asserted as the
    // real cost is the number in the box.
    expect(dialog).toHaveTextContent("Software licenses, 999.99 USD on 01 Jul 2026");
    expect(dialog).toHaveTextContent(/cannot be confirmed again or re-valued/i);
  });

  it("voids nothing on the first click, and says void is terminal", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfirmationQueuePage />);

    await screen.findByText("Software licenses");
    await user.click(screen.getByRole("button", { name: "Void" }));

    expect(api.voidExpense).not.toHaveBeenCalled();
    expect(await screen.findByRole("dialog", { name: /Void this expense/i })).toHaveTextContent(
      /Void is terminal/i,
    );
  });

  it("voids nothing when the confirmation is dismissed", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfirmationQueuePage />);

    await screen.findByText("Software licenses");
    await user.click(screen.getByRole("button", { name: "Void" }));
    const dialog = await screen.findByRole("dialog", { name: /Void this expense/i });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(api.voidExpense).not.toHaveBeenCalled();
  });

  it("announces a failed Void as a sentence instead of the raw error object", async () => {
    const user = userEvent.setup();
    api.voidExpense = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderWithProviders(<ConfirmationQueuePage />);

    await screen.findByText("Software licenses");
    await user.click(screen.getByRole("button", { name: "Void" }));
    await confirmWith(user, "Void expense");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");
  });
});

describe("BudgetPlannerPage: the irreversible controls ask first", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgets: vi.fn().mockResolvedValue(BUDGETS),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listBudgetLines: vi.fn().mockResolvedValue(BUDGET_LINES),
      updateBudget: vi.fn(),
      closeBudget: vi.fn().mockResolvedValue({ ...BUDGETS[0], status: "closed" }),
      deleteBudgetLine: vi.fn().mockResolvedValue(undefined),
      putBudgetLines: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openBudget() {
    const user = userEvent.setup();
    renderWithProviders(<BudgetPlannerPage />);
    await user.click(await screen.findByRole("button", { name: "FY26 IT Budget" }));
    await screen.findByText("Budget details");
    return user;
  }

  it("closes nothing on the first click, and names the budget and its year", async () => {
    const user = await openBudget();

    await user.click(screen.getByRole("button", { name: "Close budget" }));

    expect(api.closeBudget).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog", { name: /Close budget/i });
    // The year is the part people get wrong: a planner holds FY2025 and
    // FY2026 under names that differ by two characters.
    expect(dialog).toHaveTextContent("FY26 IT Budget (FY2026)");
    expect(dialog).toHaveTextContent(/a closed budget cannot be reopened/i);
  });

  it("will not close until the budget's name is typed back", async () => {
    const user = await openBudget();

    await user.click(screen.getByRole("button", { name: "Close budget" }));
    const dialog = await screen.findByRole("dialog", { name: /Close budget/i });
    // Unrecoverable AND wide -- every line in the year, for everyone -- which
    // is the narrow case `requireTyping` exists for.
    await user.click(within(dialog).getByRole("button", { name: "Close budget" }));
    expect(api.closeBudget).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText(/Type/), "FY26 IT Budget (FY2026)");
    await user.click(within(dialog).getByRole("button", { name: "Close budget" }));
    expect(api.closeBudget).toHaveBeenCalledWith("b1");
  });

  // The confirmation's own buttons carry no `type`, so nested inside the
  // details <form> they would submit it -- ui.tsx's Button sets no type and a
  // typeless button inside a form IS a submit button.
  it("does not save the budget details when the close confirmation is dismissed", async () => {
    const user = await openBudget();

    await user.click(screen.getByRole("button", { name: "Close budget" }));
    const dialog = await screen.findByRole("dialog", { name: /Close budget/i });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(api.closeBudget).not.toHaveBeenCalled();
    expect(api.updateBudget).not.toHaveBeenCalled();
  });

  it("deletes no planned amount on the first click, and names the cell", async () => {
    const user = await openBudget();

    await user.click(
      await screen.findByRole("button", { name: "Delete Software licenses period 1 line" }),
    );

    expect(api.deleteBudgetLine).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog", { name: /Delete planned amount/i });
    // Category, period AND the figure: the cells are indistinguishable at a
    // glance and the number is what makes one recognisable.
    expect(dialog).toHaveTextContent("Software licenses, period 1, 500.00");
  });

  it("announces a failed line delete as a sentence rather than the raw error", async () => {
    api.deleteBudgetLine = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const user = await openBudget();

    await user.click(
      await screen.findByRole("button", { name: "Delete Software licenses period 1 line" }),
    );
    await confirmWith(user, "Delete line");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");
  });
});

// Auditor B, [MEDIUM]: "two separate, similarly-worded dead ends, neither of
// which mentions that the prerequisite is creating a budget in the adjacent
// Planner tab."
describe("BudgetDashboard: the first run", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgets: vi.fn().mockResolvedValue([]),
      listBudgetCategories: vi.fn().mockResolvedValue([]),
      getBudgetVariance: vi.fn(),
      getBudgetPosition: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("replaces the two dead ends with one state that says what to do", async () => {
    renderWithProviders(<BudgetDashboard />);

    await screen.findByText("Budget dashboard");
    expect(await screen.findByRole("link", { name: /Create a budget in the Planner/ })).toHaveAttribute(
      "href",
      "/budget/planner",
    );
    expect(screen.queryByText("No position data yet.")).not.toBeInTheDocument();
    expect(screen.queryByText("No variance data yet.")).not.toBeInTheDocument();
  });

  it("does not offer a budget selector with nothing in it", async () => {
    renderWithProviders(<BudgetDashboard />);

    // Waits for the first-run state, so this cannot pass merely because the
    // budget list had not arrived yet.
    await screen.findByText("No budget yet");
    // `queryByLabelText("Budget")` would match the module nav, which is also
    // labelled "Budget" -- the assertion is about the `<select>`.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("still shows position and variance once a budget exists", async () => {
    api.listBudgets = vi.fn().mockResolvedValue(BUDGETS);
    api.getBudgetVariance = vi.fn().mockResolvedValue(EMPTY_VARIANCE);
    api.getBudgetPosition = vi.fn().mockResolvedValue(POSITION_CLEAN);
    renderWithProviders(<BudgetDashboard />);

    expect(await screen.findByText("YTD actual")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Create a budget in the Planner/ }),
    ).not.toBeInTheDocument();
  });
});

// Cycle 8's BLOCKER clause: a consequential setting saved with no
// confirmation -- and the acknowledgement the server demands had no control at
// all, so its refusal ("pass acknowledge_mixed_basis=True to proceed") told
// the operator to call the API by hand.
describe("BudgetAdminPage: changing the base currency", () => {
  function settingsApi(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME_FULL),
      getBudgetSettings: vi.fn().mockResolvedValue({
        base_currency: "USD",
        fiscal_year_start_month: 1,
        updated_by: null,
        updated_at: "2026-01-01T00:00:00.000Z",
      }),
      listBudgetCategories: vi.fn().mockResolvedValue([]),
      listCostCenters: vi.fn().mockResolvedValue([]),
      listFxRates: vi.fn().mockResolvedValue([]),
      updateBudgetSettings: vi.fn().mockResolvedValue({
        base_currency: "EUR",
        fiscal_year_start_month: 1,
        updated_by: "u1",
        updated_at: "2026-01-02T00:00:00.000Z",
      }),
      ...overrides,
    };
  }

  it("changes nothing on the first click, and states the mixed-basis consequence", async () => {
    const user = userEvent.setup();
    const api = settingsApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    // The settings have landed once the kit's key/value list names the base.
    await screen.findByText("Base currency");
    await user.type(screen.getByLabelText("New base currency"), "EUR");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(api.updateBudgetSettings).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog", { name: /Change the budget base currency/i });
    // Old to new: the field holds only the new value, so "EUR" alone does not
    // say which direction this goes.
    expect(dialog).toHaveTextContent("USD to EUR");
    expect(dialog).toHaveTextContent(/nothing is recalculated/i);
  });

  it("sends the acknowledgement the server demands once it is confirmed", async () => {
    const user = userEvent.setup();
    const api = settingsApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    // The settings have landed once the kit's key/value list names the base.
    await screen.findByText("Base currency");
    await user.type(screen.getByLabelText("New base currency"), "EUR");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await confirmWith(user, "Change base currency");

    expect(api.updateBudgetSettings).toHaveBeenCalledWith("o1", {
      base_currency: "EUR",
      fiscal_year_start_month: undefined,
      // Without this the change is refused outright the moment any non-void
      // expense carries a converted amount -- and nothing in the UI could
      // send it.
      acknowledge_mixed_basis: true,
    });
  });

  it("does not interrupt a fiscal-year-start change, which changes no stored money", async () => {
    const user = userEvent.setup();
    const api = settingsApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    // The settings have landed once the kit's key/value list names the base.
    await screen.findByText("Base currency");
    // The month is chosen by name now ("1 April"), not typed as `4`; the
    // value that travels is unchanged.
    await user.selectOptions(screen.getByLabelText("New fiscal year start month"), "4");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(api.updateBudgetSettings).toHaveBeenCalledWith("o1", {
      base_currency: undefined,
      fiscal_year_start_month: 4,
    });
  });

  it("passes the server's own refusal through instead of printing the payload", async () => {
    const user = userEvent.setup();
    const api = settingsApi({
      updateBudgetSettings: vi
        .fn()
        .mockRejectedValue(
          new ApiError(
            400,
            JSON.stringify({
              detail:
                "base currency cannot change: at least one non-void expense already carries a converted amount_base",
            }),
          ),
        ),
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    // The settings have landed once the kit's key/value list names the base.
    await screen.findByText("Base currency");
    await user.type(screen.getByLabelText("New base currency"), "EUR");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await confirmWith(user, "Change base currency");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/at least one non-void expense/i);
    expect(alert.textContent).not.toContain("detail");
  });
});

describe("budget picker options", () => {
  // The fixture as a standalone row: `CATEGORIES[0]` is `CategoryOut |
  // undefined` under this project's index-access checking.
  const CATEGORY_ROW: CategoryOut = {
    id: "cat1",
    client_id: "o1",
    key: "software_licenses",
    name: "Software licenses",
    system_key: "software_licenses",
    active: true,
    sort_order: 0,
    created_at: "2026-01-01T00:00:00.000Z",
  };

  it("distinguishes categories by the key the ERP and the materializer address", () => {
    expect(categoryPickerOptions(CATEGORIES)).toEqual([
      { id: "cat1", label: "Software licenses", sublabel: "software_licenses" },
    ]);
  });

  // A retired category still accepts expenses server-side, but the planner
  // plans no lines against it -- so money filed there appears in no variance
  // row. A silent wrong number is worse than a refusal.
  it("marks a retired category rather than letting it pass for a live one", () => {
    const retired: CategoryOut = { ...CATEGORY_ROW, active: false };
    expect(categoryPickerOptions([retired])).toEqual([
      { id: "cat1", label: "Software licenses", sublabel: "software_licenses · retired" },
    ]);
  });

  it("distinguishes cost centers by code, and marks retired ones", () => {
    const costCenter: CostCenterOut = {
      id: "cc1",
      client_id: "o1",
      code: "IT-OPS",
      name: "IT Operations",
      external_ref: null,
      active: true,
      created_at: "2026-01-01T00:00:00.000Z",
    };
    expect(costCenterPickerOptions([costCenter])).toEqual([
      { id: "cc1", label: "IT Operations", sublabel: "IT-OPS" },
    ]);
    expect(costCenterPickerOptions([{ ...costCenter, active: false }])).toEqual([
      { id: "cc1", label: "IT Operations", sublabel: "IT-OPS · retired" },
    ]);
  });

  it("names the surface that fills each list, and the permission that hides it", () => {
    expect(CATEGORY_PICKER_EMPTY_HINT).toMatch(/Budget → Admin/);
    expect(CATEGORY_PICKER_EMPTY_HINT).toMatch(/permission/i);
    expect(COST_CENTER_PICKER_EMPTY_HINT).toMatch(/Budget → Admin/);
    expect(COST_CENTER_PICKER_EMPTY_HINT).toMatch(/permission/i);
  });
});

// The New expense dialog now links people here to create the category that
// unblocks it, so a silent no-op on THIS form would be the same dead end one
// door along. All three create dialogs read
// `disabled={create.isPending || !key || !name}` before this.
describe("BudgetAdminPage: the create forms say why instead of refusing to move", () => {
  function adminApi(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME_FULL),
      getBudgetSettings: vi.fn().mockResolvedValue({
        base_currency: "USD",
        fiscal_year_start_month: 1,
        updated_by: null,
        updated_at: "2026-01-01T00:00:00.000Z",
      }),
      listBudgetCategories: vi.fn().mockResolvedValue([]),
      listCostCenters: vi.fn().mockResolvedValue([]),
      listFxRates: vi.fn().mockResolvedValue([]),
      createBudgetCategory: vi.fn().mockResolvedValue(CATEGORIES[0]),
      createCostCenter: vi.fn().mockResolvedValue(undefined),
      createFxRate: vi.fn().mockResolvedValue(undefined),
      ...overrides,
    };
  }

  it("names the missing category fields on an empty submit", async () => {
    const user = userEvent.setup();
    const api = adminApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New category" }));
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Key is required");
    expect(alert).toHaveTextContent("Name is required");
    expect(api.createBudgetCategory).not.toHaveBeenCalled();
  });

  it("creates the category once both fields are given", async () => {
    const user = userEvent.setup();
    const api = adminApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New category" }));
    await user.type(screen.getByLabelText("Key"), "software_licenses");
    await user.type(screen.getByLabelText("Name"), "Software licenses");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createBudgetCategory).toHaveBeenCalledWith({
      key: "software_licenses",
      name: "Software licenses",
      client_id: "o1",
    });
  });

  it("refuses an FX rate of zero, which would convert every expense to nothing", async () => {
    const user = userEvent.setup();
    const api = adminApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New rate" }));
    await user.type(screen.getByLabelText("From currency"), "EUR");
    await user.clear(screen.getByLabelText("Rate"));
    await user.type(screen.getByLabelText("Rate"), "0");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Rate must be a positive number/);
    expect(api.createFxRate).not.toHaveBeenCalled();
  });

  it("keeps the FX rate a string all the way to the wire", async () => {
    const user = userEvent.setup();
    const api = adminApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<BudgetAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New rate" }));
    await user.type(screen.getByLabelText("From currency"), "eur");
    await user.clear(screen.getByLabelText("Rate"));
    // Not round-trip safe through Number(): "0.92500000" becomes "0.925", so
    // this fails against a Number() regression rather than passing silently.
    await user.type(screen.getByLabelText("Rate"), "0.92500000");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createFxRate).toHaveBeenCalledWith(
      expect.objectContaining({ from_currency: "EUR", rate: "0.92500000" }),
    );
  });
});

// Auditor B, [HIGH]: "The 'Budget dashboard' for plan-vs-actual-vs-committed
// spend contains no chart ... `main svg` = 2 (both KPI-tile icons), `main
// canvas` = 0, `main table` = 0."
describe("BudgetDashboard: the chart the dashboard was missing", () => {
  // `BarChart` renders a <figure> captioned by its title; the caption is what
  // identifies which chart this is.
  async function chartFigure(title: RegExp): Promise<HTMLElement> {
    // `selector: "figcaption"` disambiguates from the page description, which
    // uses the same words -- and `find` waits for the variance query rather
    // than asserting against a chart that has not been handed its data yet.
    const caption = await screen.findByText(title, { selector: "figcaption" });
    const figure = caption.closest("figure");
    if (figure == null) throw new Error("chart caption is not inside a <figure>");
    return figure as HTMLElement;
  }

  const VARIANCE_ROWS: VarianceOut = {
    rows: [
      {
        category_id: "cat1",
        cost_center_id: null,
        period_index: 1,
        planned: "500.00",
        actual: "420.50",
        committed: "60.00",
        projected: "480.50",
        variance: "79.50",
        variance_pct: "15.90",
      },
      {
        category_id: "cat1",
        cost_center_id: null,
        period_index: 2,
        planned: "500.00",
        actual: "610.00",
        committed: "0.00",
        projected: "610.00",
        variance: "-110.00",
        variance_pct: "-22.00",
      },
    ],
    unconvertible_count: 0,
    unconvertible_by_currency: {},
  };

  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgets: vi.fn().mockResolvedValue(BUDGETS),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      getBudgetVariance: vi.fn().mockResolvedValue(VARIANCE_ROWS),
      getBudgetPosition: vi.fn().mockResolvedValue(POSITION_CLEAN),
      getBudgetSettings: vi.fn().mockResolvedValue({
        base_currency: "USD",
        fiscal_year_start_month: 1,
        updated_by: null,
        updated_at: "2026-01-01T00:00:00.000Z",
      }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("draws plan, actual and committed as three named series", async () => {
    renderWithProviders(<BudgetDashboard />);

    await screen.findByText("Plan versus actual");
    // The chart mirrors itself in a real table, which is what a screen reader
    // reads -- so the series names are assertable text, not colour.
    const figure = await chartFigure(/Plan versus actual versus committed/i);
    // Each series is named twice inside the figure -- once in the legend and
    // once in the data table that IS the figure's accessible content -- so the
    // assertion is on both appearances, not on a single one.
    expect(within(figure).getAllByText("Planned")).toHaveLength(2);
    expect(within(figure).getAllByText("Actual")).toHaveLength(2);
    expect(within(figure).getAllByText("Committed")).toHaveLength(2);
  });

  it("plots the latest period the server returned, one row per bar group", async () => {
    renderWithProviders(<BudgetDashboard />);

    await screen.findByText("Plan versus actual");
    // Period 2's figures, not period 1's, and not the two added together --
    // adding money in the browser is what invariant #6 forbids.
    const figure = await chartFigure(/period 2/i);
    expect(within(figure).getByText("610.00")).toBeInTheDocument();
    expect(within(figure).queryByText("1110.00")).not.toBeInTheDocument();
  });

  it("lets the operator read an earlier period without summing anything", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BudgetDashboard />);

    // Waits for the chart, so the period control is on screen before it is
    // driven -- the selector only exists once the variance rows arrive.
    await chartFigure(/period 2/i);
    await user.selectOptions(screen.getByLabelText("Period"), "1");

    const figure = await chartFigure(/period 1/i);
    expect(within(figure).getByText("420.50")).toBeInTheDocument();
  });

  it("names the currency the bars are in", async () => {
    renderWithProviders(<BudgetDashboard />);

    await screen.findByText("Plan versus actual");
    // An axis of money with no currency is a number nobody can act on.
    expect(await chartFigure(/period 2/i)).toHaveTextContent("USD");
  });

  it("shows an empty state rather than an axis with no bars", async () => {
    api.getBudgetVariance = vi.fn().mockResolvedValue(EMPTY_VARIANCE);
    renderWithProviders(<BudgetDashboard />);

    await screen.findByText("Plan versus actual");
    expect(
      await screen.findByText("No planned or recorded amounts for this period yet."),
    ).toBeInTheDocument();
    expect(document.querySelector("figure")).toBeNull();
  });
});

// Review wave, round 2: the two findings against this page's own figures --
// money printed as the raw wire string with the ISO code glued on, and an
// over-plan category bar that draws exactly like an at-plan one.
describe("BudgetDashboard: the figures read as money and an over-plan row says so", () => {
  const VARIANCE_ROWS: VarianceOut = {
    rows: [
      // 480.50 projected against 500.00 planned: 96%, under plan.
      {
        category_id: "cat1",
        cost_center_id: null,
        period_index: 1,
        planned: "500.00",
        actual: "420.50",
        committed: "60.00",
        projected: "480.50",
        variance: "-19.50",
        variance_pct: "-3.90",
      },
      // 610.00 against 500.00: 122%, over plan -- and the bar cannot draw it,
      // because the track IS the plan.
      {
        category_id: "cat1",
        cost_center_id: null,
        period_index: 2,
        planned: "500.00",
        actual: "610.00",
        committed: "0.00",
        projected: "610.00",
        variance: "110.00",
        variance_pct: "22.00",
      },
    ],
    unconvertible_count: 0,
    unconvertible_by_currency: {},
  };

  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgets: vi.fn().mockResolvedValue(BUDGETS),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      getBudgetVariance: vi.fn().mockResolvedValue(VARIANCE_ROWS),
      getBudgetPosition: vi.fn().mockResolvedValue(POSITION_CLEAN),
      getBudgetSettings: vi.fn().mockResolvedValue({
        base_currency: "USD",
        fiscal_year_start_month: 1,
        updated_by: null,
        updated_at: "2026-01-01T00:00:00.000Z",
      }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // "76010.40EUR" was the finding: no separator, no symbol, no space. The
  // figure is now the short human one v3 draws, and the exact amount is under
  // it -- a compact figure has rounded real money away, so the record stays on
  // the tile rather than being replaced by an approximation.
  it("prints a money tile as a compact figure with the exact amount beneath it", async () => {
    renderWithProviders(<BudgetDashboard />);

    const tile = (await screen.findByText("YTD actual")).closest(".stat-card") as HTMLElement;
    expect(within(tile).getByText("US$1.2k")).toBeInTheDocument();
    expect(within(tile).getByText("1,234.56 USD")).toBeInTheDocument();
    // The old shape, with the code glued to an ungrouped Decimal.
    expect(tile.textContent).not.toContain("1234.56USD");
  });

  // The percentage tile is not money and keeps its own unit.
  it("leaves the percentage tile reading as a percentage", async () => {
    renderWithProviders(<BudgetDashboard />);

    const tile = (await screen.findByText("% of plan")).closest(".stat-card") as HTMLElement;
    expect(tile.textContent).toContain("45.00%");
  });

  it("marks an over-plan category with a shape, not only a colour and a numeral", async () => {
    renderWithProviders(<BudgetDashboard />);

    // Period 2 is the latest the server returned, so it is what the page opens
    // on: 610.00 projected against 500.00 planned.
    const row = (await screen.findByText("122%")).closest(".prog") as HTMLElement;
    const pill = within(row).getByText("+22% over plan");
    // A pill, carrying the warn kind's own glyph -- so neither the colour nor
    // the digits are the only thing separating this row from one at 100%.
    expect(pill).toHaveAttribute("data-kind", "warn");
    // And the bar says where the value actually is, since it can only draw to
    // the end of the track.
    expect(within(row).getByRole("progressbar")).toHaveAccessibleName(
      "Software licenses: 122% of plan, over plan by 22%",
    );
  });

  it("puts no over-plan mark on a category inside its plan", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BudgetDashboard />);

    await screen.findByText("122%");
    await user.selectOptions(screen.getByLabelText("Period"), "1");

    const row = (await screen.findByText("96%")).closest(".prog") as HTMLElement;
    expect(within(row).queryByText(/over plan/)).not.toBeInTheDocument();
    expect(within(row).getByRole("progressbar")).toHaveAccessibleName("Software licenses: 96% of plan");
  });
});

// Directive D on this route: a failure that was swallowed whole.
describe("ExpensesPage: attaching an invoice reports its outcome", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listCostCenters: vi.fn().mockResolvedValue(COST_CENTERS),
      listExpenses: vi.fn().mockResolvedValue([EXPENSE_EXPECTED]),
      uploadExpenseAttachment: vi.fn().mockResolvedValue(EXPENSE_EXPECTED),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  const INVOICE = new File(["invoice bytes"], "invoice.pdf", { type: "application/pdf" });

  it("announces a refused upload instead of leaving the row looking attached", async () => {
    const user = userEvent.setup();
    api.uploadExpenseAttachment = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderWithProviders(<ExpensesPage />);

    // By label, not by text: "Software licenses" is also an option in the
    // category filter above the table.
    await user.upload(
      await screen.findByLabelText("Attach invoice for Software licenses expense on 2026-07-01"),
      INVOICE,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");
  });

  it("says so when the upload succeeds, since the row itself barely changes", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ExpensesPage />);

    await user.upload(
      await screen.findByLabelText("Attach invoice for Software licenses expense on 2026-07-01"),
      INVOICE,
    );

    expect(await screen.findByRole("status")).toHaveTextContent("Invoice attached.");
  });
});

// ---------------------------------------------------------------------------
// Cycle 12.
// ---------------------------------------------------------------------------

/**
 * Auditor B, [HIGH]: "The Fiscal year control is `INPUT[number]` pre-filled
 * `2026` with `min=""` and `max=""` (both unset)." And [MEDIUM], the same
 * form: "All three controls report `required === false` ... users have no
 * pre-submit signal and the silent-no-op failure mode gives no post-submit
 * signal either."
 *
 * The post-submit signal has its own tests above (`refuses a negative fiscal
 * year`, `names the missing field on an empty submit`). THIS pins the
 * PRE-submit half, which those cannot see: the attributes on the controls
 * themselves, which are what a spinner obeys, what a screen reader announces
 * as "required", and what the auditor actually measured.
 */
describe("New budget: the bounds and the required markers are on the controls", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgets: vi.fn().mockResolvedValue(BUDGETS),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listBudgetLines: vi.fn().mockResolvedValue(BUDGET_LINES),
      createBudget: vi.fn().mockResolvedValue(BUDGETS[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openNewBudget() {
    const user = userEvent.setup();
    renderWithProviders(<BudgetPlannerPage />);
    await user.click(await screen.findByRole("button", { name: "New budget" }));
    return user;
  }

  it("bounds the fiscal year spinner to the range the API accepts", async () => {
    await openNewBudget();

    const year = screen.getByLabelText("Fiscal year");
    expect(year).toHaveAttribute("type", "number");
    // `budget/schema.py`'s `_FiscalYear = Field(ge=2000, le=2100)`, not a
    // second opinion about what a year is.
    expect(year).toHaveAttribute("min", "2000");
    expect(year).toHaveAttribute("max", "2100");
  });

  it("marks Name and Fiscal year as required rather than leaving both unmarked", async () => {
    await openNewBudget();

    expect(screen.getByLabelText("Name")).toBeRequired();
    expect(screen.getByLabelText("Fiscal year")).toBeRequired();
  });
});

/**
 * BUDGET IS ONE CLIENT'S (MSP clients phase 2, plan 2026-10-26 Task 8). Until
 * the client selector exists (PHASE 3) this console works on the internal
 * client, whose id is `/me`'s `org_id` ("o1" here): every create names it,
 * and every read -- the settings, and the four lists -- asks for it.
 */
describe("budget belongs to a client", () => {
  function clientWrapper(queryClient: QueryClient) {
    return function Wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
    };
  }

  function knownClient(): QueryClient {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(["me"], ME_FULL);
    return queryClient;
  }

  it("files every new row under the internal client", async () => {
    const api = {
      createBudgetCategory: vi.fn().mockResolvedValue(CATEGORIES[0]),
      createCostCenter: vi.fn().mockResolvedValue(COST_CENTER_ONE[0]),
      createBudget: vi.fn().mockResolvedValue(BUDGETS[0]),
      createExpense: vi.fn().mockResolvedValue(EXPENSE_EXPECTED),
      materializeExpenses: vi
        .fn()
        .mockResolvedValue({ created: 0, skipped_existing: 0, skipped_unconvertible: 0 }),
      updateBudgetSettings: vi.fn().mockResolvedValue({}),
    };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);
    const { result } = renderHook(
      () => ({
        category: useCreateBudgetCategoryMutation(),
        costCenter: useCreateCostCenterMutation(),
        budget: useCreateBudgetMutation(),
        expense: useCreateExpenseMutation(),
        materialize: useMaterializeExpensesMutation(),
        settings: useUpdateBudgetSettingsMutation(),
      }),
      { wrapper: clientWrapper(knownClient()) },
    );
    await act(async () => {
      await result.current.category.mutateAsync({ key: "hw", name: "Hardware" });
      await result.current.costCenter.mutateAsync({ code: "OPS", name: "Operations" });
      await result.current.budget.mutateAsync({ fiscal_year: 2026, name: "FY26", granularity: "month" });
      await result.current.expense.mutateAsync({
        expense_date: "2026-01-15", amount: "1.00", currency: "USD", category_id: "cat1",
      });
      await result.current.materialize.mutateAsync({ from_date: "2026-01-01", to_date: "2026-01-31" });
      await result.current.settings.mutateAsync({ base_currency: "EUR" });
    });

    for (const create of [
      api.createBudgetCategory,
      api.createCostCenter,
      api.createBudget,
      api.createExpense,
      api.materializeExpenses,
    ]) {
      expect(create).toHaveBeenCalledWith(expect.objectContaining({ client_id: "o1" }));
    }
    expect(api.updateBudgetSettings).toHaveBeenCalledWith("o1", { base_currency: "EUR" });
  });

  it("reads the internal client's settings and lists", async () => {
    const api = {
      getBudgetSettings: vi.fn().mockResolvedValue({}),
      listBudgetCategories: vi.fn().mockResolvedValue([]),
      listCostCenters: vi.fn().mockResolvedValue([]),
      listBudgets: vi.fn().mockResolvedValue([]),
      listExpenses: vi.fn().mockResolvedValue([]),
    };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);
    renderHook(
      () => [
        useBudgetSettings(),
        useBudgetCategories(true),
        useCostCenters(),
        useBudgets(),
        useExpenses({ status: "expected" }),
      ],
      { wrapper: clientWrapper(knownClient()) },
    );

    await waitFor(() => expect(api.listExpenses).toHaveBeenCalled());
    expect(api.getBudgetSettings).toHaveBeenCalledWith("o1");
    // Naming the client is also what seeds its default categories.
    expect(api.listBudgetCategories).toHaveBeenCalledWith(true, "o1");
    expect(api.listCostCenters).toHaveBeenCalledWith(false, "o1");
    expect(api.listBudgets).toHaveBeenCalledWith("o1");
    expect(api.listExpenses).toHaveBeenCalledWith({ status: "expected" }, "o1");
  });

  it("sends nothing while it cannot name the client, and reads as loading meanwhile", async () => {
    const api = {
      me: vi.fn(() => new Promise<Me>(() => {})),
      listBudgetCategories: vi.fn().mockResolvedValue([]),
      getBudgetSettings: vi.fn().mockResolvedValue({}),
      createExpense: vi.fn().mockResolvedValue(EXPENSE_EXPECTED),
    };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(
      () => ({
        categories: useBudgetCategories(),
        settings: useBudgetSettings(),
        create: useCreateExpenseMutation(),
      }),
      { wrapper: clientWrapper(queryClient) },
    );

    let refusal: unknown;
    await act(async () => {
      refusal = await result.current.create
        .mutateAsync({ expense_date: "2026-01-15", amount: "1.00", currency: "USD", category_id: "cat1" })
        .catch((error: unknown) => error);
    });
    expect(String(refusal)).toMatch(/Nothing was sent/);
    expect(api.createExpense).not.toHaveBeenCalled();
    expect(api.listBudgetCategories).not.toHaveBeenCalled();
    expect(api.getBudgetSettings).not.toHaveBeenCalled();
    // Waiting for the client is loading, not "no categories yet".
    expect(result.current.categories.isLoading).toBe(true);
    expect(result.current.settings.isLoading).toBe(true);
  });

  // Budget is a module a client may not use, and the server's 404 for that
  // is a sentence, which is what the person reads.
  it("says so when the client does not use budget", async () => {
    const user = userEvent.setup();
    const api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listBudgetCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listCostCenters: vi.fn().mockResolvedValue(COST_CENTERS),
      listExpenses: vi.fn().mockResolvedValue([]),
      createExpense: vi
        .fn()
        .mockRejectedValue(
          new ApiError(404, JSON.stringify({ detail: "this client does not use that module" })),
        ),
    };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);
    renderWithProviders(<ExpensesPage />);

    await user.click(await screen.findByRole("button", { name: "New expense" }));
    await user.clear(screen.getByLabelText("Amount"));
    await user.type(screen.getByLabelText("Amount"), "10.00");
    await pick(user, "Category", /Software licenses/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "this client does not use that module",
    );
    expect(api.createExpense).toHaveBeenCalledWith(expect.objectContaining({ client_id: "o1" }));
  });
});
