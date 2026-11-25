/**
 * GENERAL RULING 12, FOR THE TOP BAR'S CLIENT: an edit never carries to
 * another client. Budget works one client at a time (the top bar's), and its
 * settings are that client's: a base currency typed for one client must never
 * be saved onto the next (O7 reproduced exactly that elsewhere).
 *
 * What protects the budget pages is the console's own provider, which keys
 * everything under it by the chosen client (`lib/clientScope.tsx`, "a new key
 * is a new page"), so this test mounts the page under the REAL provider and
 * moves the REAL choice, rather than the stand-in the other budget tests use.
 */
import userEvent from "@testing-library/user-event";
import { act, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, MeClient } from "../../../lib/api";
import { setSelectedClient } from "../../../lib/clientChoice";
import { ClientScopeProvider } from "../../../lib/clientScope";
import { renderPage } from "../../../test/renderPage";
import { BudgetAdminPage } from "../BudgetAdminPage";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const CODES = ["budget.read", "budget.manage"];
const ANDES: MeClient = { id: "c-andes", name: "Andes Logistics", kind: "customer", enabled_modules: ["budget"], permissions: CODES };
const BETA: MeClient = { id: "c-beta", name: "Beta Foods", kind: "customer", enabled_modules: ["budget"], permissions: CODES };

afterEach(() => act(() => setSelectedClient(null)));

describe("the budget settings when the top bar's client changes", () => {
  it("drops a base currency typed for one client, and saves nothing of it onto the next (ruling 12)", async () => {
    act(() => setSelectedClient(ANDES.id));
    const getBudgetSettings = vi.fn((client: string) =>
      Promise.resolve({
        client_id: client,
        base_currency: client === ANDES.id ? "EUR" : "GBP",
        fiscal_year_start_month: 1,
        updated_by: null,
        updated_at: null,
      }),
    );
    const updateBudgetSettings = vi.fn().mockResolvedValue({});
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue({
        user_id: "u1",
        org_id: "o1",
        email: "a@b.c",
        display_name: "A",
        permissions: CODES,
        clients: [ANDES, BETA],
      }),
      getBudgetSettings,
      updateBudgetSettings,
      listBudgetCategories: vi.fn().mockResolvedValue([]),
      listCostCenters: vi.fn().mockResolvedValue([]),
      listFxRates: vi.fn().mockResolvedValue([]),
    } as unknown as ApiClient);

    renderPage(
      "/budget/admin",
      <ClientScopeProvider>
        <BudgetAdminPage />
      </ClientScopeProvider>,
      "/budget/admin",
    );
    await screen.findByText("EUR");
    await userEvent.type(screen.getByLabelText("New base currency"), "USD");
    expect(screen.getByRole("region", { name: "Unsaved changes" })).toBeInTheDocument();

    act(() => setSelectedClient(BETA.id));

    // The other client's own settings, and nothing of the edit typed before.
    expect(await screen.findByText("GBP")).toBeInTheDocument();
    expect(screen.getByLabelText("New base currency")).toHaveValue("");
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    await waitFor(() => expect(getBudgetSettings).toHaveBeenCalledWith(BETA.id));
    expect(updateBudgetSettings).not.toHaveBeenCalled();
  });
});
