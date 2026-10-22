import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { LicenseReadiness } from "../LicenseReadiness";

// "Before compliance can be counted" (cross-module guidance, Licenses rows 2
// and 3).

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = ["licenses.read", "devices.read"];

function stubApi(overrides: Partial<ApiClient> & { permissions?: string[] } = {}): ApiClient {
  const { permissions = ALL, ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0, fleet: { total: 4, managed: 3, online: 2 } }),
    reviewQueue: vi.fn().mockResolvedValue([]),
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
        <LicenseReadiness />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LicenseReadiness", () => {
  it("renders nothing when computers report software and every title is mapped", async () => {
    const api = stubApi();
    renderPanel();
    await waitFor(() => expect(api.reviewQueue).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("Before compliance can be counted")).toBeNull();
  });

  it("blocks compliance on a fleet with no agent, and waits the mapping row on it", async () => {
    // An empty review queue on a fleet with no inventory means nothing was
    // found, not that everything is mapped -- so it must not read "done".
    stubApi({ listDevices: vi.fn().mockResolvedValue({ items: [], total: 0, fleet: { total: 0, managed: 0, online: 0 } }) });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Add a computer" })).toHaveAttribute("href", "/devices/onboard");
    expect(screen.getByText("Inventory installed software first.")).toBeInTheDocument();
  });

  it("links unmapped installed software to the review queue", async () => {
    stubApi({ reviewQueue: vi.fn().mockResolvedValue([{ name: "Visio", publisher: "Microsoft", device_count: 3 }]) });
    renderPanel();
    expect(await screen.findByText(/1 installed title is not mapped/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Map installed software" })).toHaveAttribute(
      "href",
      "/licenses/review-queue",
    );
  });

  it("says the inventory cannot be checked without device read access, and never asks", async () => {
    const api = stubApi({ permissions: ["licenses.read"] });
    renderPanel();
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(api.listDevices).not.toHaveBeenCalled();
    expect(screen.queryByRole("link", { name: "Add a computer" })).toBeNull();
  });
});
