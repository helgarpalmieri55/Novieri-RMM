import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient } from "../../../lib/api";
import { SubnetDetailPage } from "../SubnetDetailPage";
import type { SubnetOut, UtilizationOut } from "../types";

// Editing and deleting a REGISTERED subnet from the console (operator, 2026-08-26:
// "i create a subnet but i cannot edit it or delete it").
//
// THE DISTINCTION THESE TESTS REFUSE TO COLLAPSE: deleting an EMPTY subnet and
// deleting one that HOLDS addresses are not the same act. The first goes through
// without cascade; the second is refused by the server until cascade is asked
// for, and the operator is shown the server's own count before a second,
// explicit cascade confirmation. A test that only checked "delete was called"
// would let a one-click cascade ship.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function stubApi(overrides: Partial<ApiClient>, permissions: string[]): ApiClient {
  const api = {
    me: vi.fn().mockResolvedValue({
      user_id: "u1",
      org_id: "o1",
      email: "a@b.c",
      display_name: "A",
      permissions,
    }),
    listIpamVrfs: vi.fn().mockResolvedValue([]),
    listClientSites: vi.fn().mockResolvedValue([]),
    listIpamVlans: vi.fn().mockResolvedValue([]),
    listIpamSubnets: vi.fn().mockResolvedValue([]),
    getIpamSubnet: vi.fn(),
    getIpamSubnetUtilization: vi.fn(),
    listIpamRanges: vi.fn().mockResolvedValue([]),
    listIpamSubnetAddresses: vi.fn().mockResolvedValue([]),
    listIpamScanAuthorizations: vi.fn().mockResolvedValue([]),
    listIpamSweepRuns: vi.fn().mockResolvedValue([]),
    ...overrides,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

function subnet(overrides: Partial<SubnetOut> = {}): SubnetOut {
  return {
    client_id: "o1",
    id: "s1",
    vrf_id: "v1",
    prefix: "10.4.7.0/24",
    kind: "assignable",
    name: "Core",
    description: "",
    site_id: null,
    vlan_id: null,
    gateway: null,
    dns_servers: [],
    parent_id: null,
    sweep_enabled: false,
    sweep_device_id: null,
    sweep_interval_hours: null,
    last_swept_at: null,
    ...overrides,
  };
}

function utilization(overrides: Partial<UtilizationOut> = {}): UtilizationOut {
  return {
    subnet_id: "s1",
    prefix: "10.4.7.0/24",
    enumerable: true,
    capacity: 254,
    free: 254,
    used: 0,
    percentage: 0,
    assigned: 0,
    reserved: 0,
    deprecated: 0,
    observed: 0,
    ...overrides,
  };
}

function renderDetail(api: ApiClient) {
  void api;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/ipam/subnets/s1"]}>
        <Routes>
          <Route path="/ipam/subnets/:subnetId" element={<SubnetDetailPage />} />
          <Route path="/ipam" element={<div>the subnet tree</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const MANAGE = ["ipam.read", "ipam.assign", "ipam.manage"];

// --------------------------------------------------------------------------
// Editing a registered subnet
// --------------------------------------------------------------------------

describe("editing a subnet", () => {
  it("opens the edit dialog and saves the changed fields", async () => {
    const user = userEvent.setup();
    const update = vi.fn<ApiClient["updateIpamSubnet"]>().mockResolvedValue(subnet({ name: "Edge" }));
    const api = stubApi(
      {
        getIpamSubnet: vi.fn().mockResolvedValue(subnet({ name: "Core" })),
        getIpamSubnetUtilization: vi.fn().mockResolvedValue(utilization()),
        updateIpamSubnet: update,
      },
      MANAGE,
    );
    renderDetail(api);

    await user.click(await screen.findByRole("button", { name: "Edit" }));

    const dialog = within(await screen.findByRole("dialog"));
    const name = dialog.getByLabelText("Name");
    await user.clear(name);
    await user.type(name, "Edge");
    await user.click(dialog.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0]?.[0]).toBe("s1");
    expect(update.mock.calls[0]?.[1]).toMatchObject({ name: "Edge" });
  });

  it("does not offer an Edit control without ipam.manage", async () => {
    const api = stubApi(
      {
        getIpamSubnet: vi.fn().mockResolvedValue(subnet()),
        getIpamSubnetUtilization: vi.fn().mockResolvedValue(utilization()),
      },
      ["ipam.read"],
    );
    renderDetail(api);

    await screen.findByText("Gateway");
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("rejects a malformed gateway before sending anything", async () => {
    const user = userEvent.setup();
    const update = vi.fn<ApiClient["updateIpamSubnet"]>();
    const api = stubApi(
      {
        getIpamSubnet: vi.fn().mockResolvedValue(subnet()),
        getIpamSubnetUtilization: vi.fn().mockResolvedValue(utilization()),
        updateIpamSubnet: update,
      },
      MANAGE,
    );
    renderDetail(api);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.type(await screen.findByLabelText("Gateway"), "10.4.7.999");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Gateway must be an IP address");
    expect(update).not.toHaveBeenCalled();
  });
});

// --------------------------------------------------------------------------
// Deleting a registered subnet — the cascade trap
// --------------------------------------------------------------------------

describe("deleting a subnet", () => {
  it("deletes an empty subnet without cascade", async () => {
    const user = userEvent.setup();
    const del = vi.fn<ApiClient["deleteIpamSubnet"]>().mockResolvedValue(undefined);
    const api = stubApi(
      {
        getIpamSubnet: vi.fn().mockResolvedValue(subnet()),
        getIpamSubnetUtilization: vi.fn().mockResolvedValue(utilization()),
        listIpamSubnets: vi.fn().mockResolvedValue([subnet()]),
        deleteIpamSubnet: del,
      },
      MANAGE,
    );
    renderDetail(api);

    await user.click(await screen.findByRole("button", { name: "Delete" }));
    // The confirmation states the subnet holds nothing this page can see.
    expect(await screen.findByText(/holds nothing this page can see/)).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Delete subnet" }));

    await waitFor(() => expect(del).toHaveBeenCalled());
    expect(del.mock.calls[0]).toEqual(["s1", false]);
  });

  it("escalates to an explicit cascade confirmation when the server refuses a non-empty delete", async () => {
    const user = userEvent.setup();
    const message =
      "subnet 10.4.7.0/24 still holds 3 addresses, 1 range; delete with cascade to release them";
    const del = vi
      .fn<ApiClient["deleteIpamSubnet"]>()
      .mockImplementation((_id, cascade) =>
        cascade ? Promise.resolve(undefined) : Promise.reject(new ApiError(409, message)),
      );
    const api = stubApi(
      {
        getIpamSubnet: vi.fn().mockResolvedValue(subnet()),
        getIpamSubnetUtilization: vi
          .fn()
          .mockResolvedValue(utilization({ assigned: 3, used: 3, free: 251 })),
        listIpamSubnets: vi.fn().mockResolvedValue([subnet()]),
        deleteIpamSubnet: del,
      },
      MANAGE,
    );
    renderDetail(api);

    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: "Delete subnet" }));

    // First attempt is non-cascade; the server refuses it.
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
    expect(del.mock.calls[0]).toEqual(["s1", false]);

    // The refusal is surfaced as the server's own sentence, not a raw error, and
    // a SECOND confirmation appears for the cascade.
    expect(await screen.findByText(new RegExp(message))).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Delete with cascade" }));

    await waitFor(() => expect(del).toHaveBeenCalledTimes(2));
    expect(del.mock.calls[1]).toEqual(["s1", true]);
  });

  it("surfaces a non-conflict delete failure as a plain message without escalating", async () => {
    const user = userEvent.setup();
    const del = vi
      .fn<ApiClient["deleteIpamSubnet"]>()
      .mockRejectedValue(new ApiError(503, ""));
    const api = stubApi(
      {
        getIpamSubnet: vi.fn().mockResolvedValue(subnet()),
        getIpamSubnetUtilization: vi.fn().mockResolvedValue(utilization()),
        listIpamSubnets: vi.fn().mockResolvedValue([subnet()]),
        deleteIpamSubnet: del,
      },
      MANAGE,
    );
    renderDetail(api);

    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: "Delete subnet" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/);
    expect(screen.queryByRole("button", { name: "Delete with cascade" })).not.toBeInTheDocument();
    expect(del).toHaveBeenCalledTimes(1);
  });
});
