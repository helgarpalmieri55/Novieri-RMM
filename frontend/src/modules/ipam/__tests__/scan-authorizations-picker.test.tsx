import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../../lib/api";
import { ScanAuthorizationsPage } from "../ScanAuthorizationsPage";
import type { SubnetOut } from "../types";

// Authorising a registered subnet for scanning WITHOUT re-typing its prefix
// (operator, 2026-08-26: the scan-authorizations page "not showing the subnet i
// already created… double work there").
//
// THE DISTINCTION THIS REFUSES TO COLLAPSE: filing a subnet is NOT authorising a
// scan of it (deny-by-default, §0.12). The picker FILLS a prefix; it does not
// grant anything. And a scan authorization is a prefix, not a subnet row, so
// free-typing an arbitrary prefix — a scope that is not a registered subnet —
// still works.

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
    listIpamSubnets: vi.fn().mockResolvedValue([]),
    listIpamScanAuthorizations: vi.fn().mockResolvedValue([]),
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

async function renderScopes(api: ApiClient) {
  void api;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/ipam/scan-authorizations"]}>
        <ScanAuthorizationsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // The grant form lives in the dialog the head's primary action opens.
  await userEvent.click(await screen.findByRole("button", { name: "Authorize a subnet" }));
  return rendered;
}

const SCOPE_MANAGE = ["ipam.read", "ipam.scope.manage"];

describe("authorising a registered subnet", () => {
  it("fills the prefix from a picked registered subnet", async () => {
    const user = userEvent.setup();
    const api = stubApi(
      { listIpamSubnets: vi.fn().mockResolvedValue([subnet({ prefix: "10.4.7.0/24", name: "Core" })]) },
      SCOPE_MANAGE,
    );
    await renderScopes(api);

    const picker = await screen.findByLabelText("Fill from a registered subnet");
    await user.selectOptions(picker, "s1");

    const prefix = screen.getByPlaceholderText("10.4.7.0/24") as HTMLInputElement;
    expect(prefix.value).toBe("10.4.7.0/24");
    // The grant preview computes off the filled prefix, so it now reads.
    expect(screen.getByText(/lets sweeps probe 256 addresses/)).toBeInTheDocument();
  });

  it("still authorizes an arbitrary prefix that is not a registered subnet", async () => {
    const user = userEvent.setup();
    const create = vi.fn<ApiClient["createIpamScanAuthorization"]>().mockResolvedValue({
      client_id: "o1",
      id: "sc9",
      vrf_id: "v1",
      prefix: "192.168.9.0/24",
      note: "",
      authorized_by: "u1",
      authorized_at: "2026-08-01T00:00:00.000Z",
      expires_at: null,
      revoked_at: null,
    });
    const api = stubApi(
      {
        listIpamSubnets: vi.fn().mockResolvedValue([subnet()]),
        createIpamScanAuthorization: create,
      },
      SCOPE_MANAGE,
    );
    await renderScopes(api);

    // A prefix nobody filed — the picker offers no such option, and typing it is
    // still the way to authorise a scope that is not a subnet row.
    const prefix = (await screen.findByPlaceholderText("10.4.7.0/24")) as HTMLInputElement;
    await user.type(prefix, "192.168.9.0/24");
    await user.click(screen.getByRole("button", { name: "Authorize" }));

    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(create.mock.calls[0]?.[0]).toMatchObject({ prefix: "192.168.9.0/24" });
  });

  it("does not authorize merely by picking — filling a prefix is not a grant", async () => {
    const user = userEvent.setup();
    const create = vi.fn<ApiClient["createIpamScanAuthorization"]>();
    const api = stubApi(
      {
        listIpamSubnets: vi.fn().mockResolvedValue([subnet()]),
        createIpamScanAuthorization: create,
      },
      SCOPE_MANAGE,
    );
    await renderScopes(api);

    await user.selectOptions(await screen.findByLabelText("Fill from a registered subnet"), "s1");
    // Filling is not granting: nothing is sent until the operator authorises.
    expect(create).not.toHaveBeenCalled();
  });
});
