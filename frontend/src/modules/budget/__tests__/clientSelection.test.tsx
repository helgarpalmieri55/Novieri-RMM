import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, MeClient } from "../../../lib/api";
import { BudgetAdminPage } from "../BudgetAdminPage";
import { BudgetDashboard } from "../BudgetDashboard";
import { ExpensesPage } from "../ExpensesPage";

// MSP clients phase 3 (plan Task 11 D11.1): budget is one client's, in its own
// base currency. With a client chosen at the top every read names it; with
// every client chosen the pages ask for one and read nothing, and the global
// exchange rates stay on /budget/admin.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = ["budget.read", "budget.manage"];
const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: ["budget"], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: ["budget"], permissions: CODES };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

function stubApi() {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: CODES, clients: [INTERNAL, ACME] }),
    getBudgetSettings: vi.fn().mockResolvedValue({ client_id: "c-acme", base_currency: "EUR", fiscal_year_start_month: 1, updated_by: null, updated_at: null }),
    listBudgetCategories: vi.fn().mockResolvedValue([]),
    listCostCenters: vi.fn().mockResolvedValue([]),
    listBudgets: vi.fn().mockResolvedValue([]),
    listExpenses: vi.fn().mockResolvedValue([]),
    listFxRates: vi.fn().mockResolvedValue([]),
    listSubscriptions: vi.fn().mockResolvedValue([]),
    listLicenses: vi.fn().mockResolvedValue([]),
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderAt(node: React.ReactNode, entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o1"));

describe("budget follows the client chosen at the top", () => {
  it("reads that client's settings and expenses", async () => {
    selectClient("c-acme");
    const api = stubApi();
    renderAt(<ExpensesPage />, "/budget/expenses");
    await waitFor(() => expect(api.listExpenses).toHaveBeenCalledWith(expect.anything(), "c-acme"));
    expect(screen.queryByText("Choose a client at the top of the page")).toBeNull();
  });

  it("with every client chosen, asks for one and reads nothing of any client", async () => {
    selectClient(null);
    const api = stubApi();
    renderAt(<BudgetDashboard />, "/budget");
    expect(await screen.findByText("Choose a client at the top of the page")).toBeInTheDocument();
    expect(api.getBudgetSettings).not.toHaveBeenCalled();
    expect(api.listBudgets).not.toHaveBeenCalled();
  });

  it("keeps the global exchange rates on the admin page with every client chosen", async () => {
    selectClient(null);
    const api = stubApi();
    renderAt(<BudgetAdminPage />, "/budget/admin");
    expect(await screen.findByText("Choose a client at the top of the page")).toBeInTheDocument();
    await waitFor(() => expect(api.listFxRates).toHaveBeenCalled());
    expect(api.listBudgetCategories).not.toHaveBeenCalled();
    expect(api.getBudgetSettings).not.toHaveBeenCalled();
  });
});
