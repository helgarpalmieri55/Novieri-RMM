import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { BudgetReadiness } from "../BudgetReadiness";

// "Before expected spend can be projected" (cross-module guidance, IT budget
// row 4). Row 3's exchange-rate link is asserted in budget.test.tsx.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = ["budget.read", "subscriptions.read", "licenses.read"];

function stubApi(overrides: Partial<ApiClient> & { permissions?: string[] } = {}): ApiClient {
  const { permissions = ALL, ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listSubscriptions: vi.fn().mockResolvedValue([]),
    listLicenses: vi.fn().mockResolvedValue([{ id: "l1" }]),
    ...rest,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <BudgetReadiness />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("BudgetReadiness", () => {
  it("renders nothing when either register has something to project", async () => {
    const api = stubApi();
    renderPanel();
    await waitFor(() => expect(api.listLicenses).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("Before expected spend can be projected")).toBeNull();
  });

  it("links two empty registers to Subscriptions", async () => {
    stubApi({ listLicenses: vi.fn().mockResolvedValue([]) });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Record subscriptions" })).toHaveAttribute("href", "/subscriptions");
  });

  it("names the permission the account lacks, and asks neither register", async () => {
    // One empty register proves nothing when the other could not be read.
    const api = stubApi({ permissions: ["budget.read", "subscriptions.read"] });
    renderPanel();
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(api.listLicenses).not.toHaveBeenCalled();
    expect(screen.queryByRole("link", { name: "Record subscriptions" })).toBeNull();
  });
});
