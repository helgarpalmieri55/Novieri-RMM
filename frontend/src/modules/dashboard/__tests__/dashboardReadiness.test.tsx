import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { DashboardReadiness } from "../DashboardReadiness";

// "Before the dashboard fills in" (`docs/design/cross-module-guidance.md`
// §2 Dashboard). Unlike every other panel, a row this account cannot check is
// left out rather than shown as "cannot be checked": each dashboard widget
// exists only for an account holding its read permission.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function fleet(managed: number) {
  return {
    items: [],
    total: managed,
    state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
    fleet: { total: managed, managed, online: managed, cert_attention: 0 },
  };
}

function stubApi(overrides: Record<string, unknown> & { permissions?: string[] } = {}) {
  const { permissions = ["devices.read", "backup.read"], ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listDevices: vi.fn().mockResolvedValue(fleet(2)),
    listBackupPolicies: vi.fn().mockResolvedValue([{ id: "p1", enabled: true }]),
    ...rest,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DashboardReadiness />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

async function settled(client: QueryClient) {
  await waitFor(() => expect(client.isFetching()).toBe(0));
  await waitFor(() => expect(client.getQueryState(["me"])?.status).toBe("success"));
}

describe("DashboardReadiness", () => {
  it("links to adding a computer and to setting up backups", async () => {
    stubApi({
      listDevices: vi.fn().mockResolvedValue(fleet(0)),
      listBackupPolicies: vi.fn().mockResolvedValue([{ id: "p1", enabled: false }]),
    });
    renderPanel();

    const agent = (await screen.findByText("A computer runs the agent")).closest("li") as HTMLElement;
    expect(within(agent).getByRole("link", { name: "Add a computer" })).toHaveAttribute("href", "/devices/onboard");
    const backup = (await screen.findByText("Backups are scheduled")).closest("li") as HTMLElement;
    expect(within(backup).getByRole("link", { name: "Set up backups" })).toHaveAttribute("href", "/backup/policies");
  });

  it("is not drawn once both are in place", async () => {
    stubApi();
    const client = renderPanel();
    await settled(client);
    expect(screen.queryByText("Before the dashboard fills in")).not.toBeInTheDocument();
  });

  it("leaves out a row whose widget this account never sees, and asks nothing for it", async () => {
    const api = stubApi({ permissions: ["devices.read"], listDevices: vi.fn().mockResolvedValue(fleet(0)) });
    const client = renderPanel();

    await screen.findByText("A computer runs the agent");
    await settled(client);
    expect(screen.queryByText("Backups are scheduled")).not.toBeInTheDocument();
    expect(screen.queryByText(/cannot be checked/)).not.toBeInTheDocument();
    expect(api.listBackupPolicies).not.toHaveBeenCalled();
  });

  it("draws nothing for an account that holds neither read", async () => {
    const api = stubApi({ permissions: [] });
    const client = renderPanel();
    await settled(client);
    expect(screen.queryByText("Before the dashboard fills in")).not.toBeInTheDocument();
    expect(api.listDevices).not.toHaveBeenCalled();
  });
});
