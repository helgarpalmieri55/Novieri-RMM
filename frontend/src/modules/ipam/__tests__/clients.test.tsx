import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient, ClientSiteOut } from "../../../lib/api";
import { AddressDialog } from "../AddressDialog";
import { RegistryPage } from "../RegistryPage";
import { ScanAuthorizationsPage } from "../ScanAuthorizationsPage";
import { SubnetTreePage } from "../SubnetTreePage";
import type { GridEntryOut, ScopeOut, SubnetOut, VlanOut, VrfOut } from "../types";

// EVERY IPAM ROW IS ONE CLIENT'S (MSP clients phase 2, migration 0240). A create
// takes its client from its VRF, or a VLAN from its site, and the server refuses
// a body that contradicts that parent -- so the console names a client only
// where there is no parent: a VRF always, a VLAN without a site, and a subnet,
// an address or a scan authorization without a VRF. Until phase 3's selector
// that client is the internal one, `/me`'s `org_id` (`lib/internalClient.ts`),
// and nothing is sent before `/me` has answered. IPAM keeps no sites of its own:
// its pickers list the client's (`GET /clients/{id}/sites`).

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME = {
  user_id: "u1",
  org_id: "o1",
  email: "a@b.c",
  display_name: "A",
  permissions: ["ipam.read", "ipam.assign", "ipam.manage", "ipam.scope.manage"],
};

const SITE: ClientSiteOut = {
  id: "site-1",
  client_id: "o1",
  name: "Medellín",
  address: "",
  description: "Head office",
  egress_networks: [],
  archived_at: null,
  device_count: 0,
};

const VRF: VrfOut = { id: "vrf-lab", client_id: "o1", name: "lab", rd: null, description: "" };

function vlan(overrides: Partial<VlanOut>): VlanOut {
  return { id: "vl", client_id: "o1", vid: 10, site_id: null, name: "", description: "", ...overrides };
}

function subnet(overrides: Partial<SubnetOut> = {}): SubnetOut {
  return {
    client_id: "o1",
    id: "s1",
    vrf_id: "v1",
    prefix: "10.4.7.0/24",
    kind: "assignable",
    name: "",
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

function scope(): ScopeOut {
  return {
    client_id: "o1",
    id: "sc",
    vrf_id: "v1",
    prefix: "10.4.7.0/24",
    note: "",
    authorized_by: "u1",
    authorized_at: new Date().toISOString(),
    expires_at: null,
    revoked_at: null,
  };
}

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}): Record<string, ReturnType<typeof vi.fn>> {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listIpamVrfs: vi.fn().mockResolvedValue([VRF]),
    listClientSites: vi.fn().mockResolvedValue([SITE]),
    listIpamVlans: vi.fn().mockResolvedValue([]),
    listIpamSubnets: vi.fn().mockResolvedValue([]),
    listIpamDrift: vi.fn().mockResolvedValue([]),
    listIpamOrphans: vi.fn().mockResolvedValue([]),
    listIpamScanAuthorizations: vi.fn().mockResolvedValue([]),
    listIpamSweepRuns: vi.fn().mockResolvedValue([]),
    getIpamAddressHistory: vi.fn().mockResolvedValue([]),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listSiteServers: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api);
  return api as Record<string, ReturnType<typeof vi.fn>>;
}

function renderWithProviders(node: React.ReactNode, entry = "/ipam") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function bodyOf(mock: ReturnType<typeof vi.fn>): Record<string, unknown> {
  return mock.mock.calls[0]?.[0] as Record<string, unknown>;
}

describe("the registry names the client where nothing above a row does", () => {
  it("files a new VRF under the internal client", async () => {
    const createIpamVrf = vi.fn().mockResolvedValue(VRF);
    stubApi({ createIpamVrf });
    const user = userEvent.setup();
    renderWithProviders(<RegistryPage />, "/ipam/registry");

    await user.type((await screen.findAllByLabelText("Name"))[0]!, "lan");
    await user.click(screen.getByRole("button", { name: "Add VRF" }));

    await waitFor(() => expect(createIpamVrf).toHaveBeenCalled());
    // PHASE 3: the selected client; the internal one, `/me`'s `org_id`, until then.
    expect(bodyOf(createIpamVrf)).toMatchObject({ name: "lan", client_id: "o1" });
  });

  it("lists the client's sites, read-only, and picks a VLAN's site from them", async () => {
    const createIpamVlan = vi.fn().mockResolvedValue(vlan({ site_id: SITE.id }));
    const api = stubApi({ createIpamVlan });
    const user = userEvent.setup();
    renderWithProviders(<RegistryPage />, "/ipam/registry");

    expect((await screen.findAllByText("Medellín")).length).toBeGreaterThan(0);
    expect(api.listClientSites).toHaveBeenCalledWith("o1");
    // IPAM creates no sites: an office is the client's.
    expect(screen.queryByRole("button", { name: "Add site" })).toBeNull();

    await user.type(screen.getByLabelText("VID"), "40");
    await user.selectOptions(screen.getByLabelText("Site"), SITE.id);
    await user.click(screen.getByRole("button", { name: "Add VLAN" }));

    await waitFor(() => expect(createIpamVlan).toHaveBeenCalled());
    // The site's client is the VLAN's: the body names the site and no client.
    const body = bodyOf(createIpamVlan);
    expect(body).toMatchObject({ vid: 40, site_id: SITE.id });
    expect(body).not.toHaveProperty("client_id");
  });

  it("files a VLAN with no site under the internal client", async () => {
    const createIpamVlan = vi.fn().mockResolvedValue(vlan({}));
    stubApi({ createIpamVlan });
    const user = userEvent.setup();
    renderWithProviders(<RegistryPage />, "/ipam/registry");

    await user.type(await screen.findByLabelText("VID"), "10");
    await user.click(screen.getByRole("button", { name: "Add VLAN" }));

    await waitFor(() => expect(createIpamVlan).toHaveBeenCalled());
    expect(bodyOf(createIpamVlan)).toMatchObject({ vid: 10, site_id: null, client_id: "o1" });
  });
});

describe("a subnet, an address and a scan authorization", () => {
  async function registerSubnet(vrf: string | null) {
    const createIpamSubnet = vi.fn().mockResolvedValue(subnet());
    stubApi({ createIpamSubnet });
    const user = userEvent.setup();
    renderWithProviders(<SubnetTreePage />);
    await user.click(await screen.findByRole("button", { name: "New subnet" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24");
    if (vrf !== null) {
      await within(dialog).findByRole("option", { name: "lab" });
      await user.selectOptions(within(dialog).getByLabelText("VRF"), vrf);
    }
    await user.click(within(dialog).getByRole("button", { name: "Register" }));
    await waitFor(() => expect(createIpamSubnet).toHaveBeenCalled());
    return bodyOf(createIpamSubnet);
  }

  it("files a subnet with no VRF under the internal client's global VRF", async () => {
    expect(await registerSubnet(null)).toMatchObject({ vrf_id: null, client_id: "o1" });
  });

  it("names no client for a subnet in a VRF: the VRF's is the subnet's", async () => {
    const body = await registerSubnet(VRF.id);
    expect(body).toMatchObject({ vrf_id: VRF.id });
    expect(body).not.toHaveProperty("client_id");
  });

  it("files an authorization with no VRF under the internal client", async () => {
    const createIpamScanAuthorization = vi.fn().mockResolvedValue(scope());
    stubApi({ createIpamScanAuthorization, listIpamSubnets: vi.fn().mockResolvedValue([subnet()]) });
    const user = userEvent.setup();
    renderWithProviders(<ScanAuthorizationsPage />);
    await user.click(await screen.findByRole("button", { name: "Authorize a subnet" }));
    await user.type(await screen.findByLabelText("Prefix"), "10.4.7.0/24");
    await user.click(screen.getByRole("button", { name: "Authorize" }));

    await waitFor(() => expect(createIpamScanAuthorization).toHaveBeenCalled());
    expect(bodyOf(createIpamScanAuthorization)).toMatchObject({
      prefix: "10.4.7.0/24",
      client_id: "o1",
    });
  });

  const entry: GridEntryOut = {
      address: "10.4.7.9",
      status: "free",
      state: null,
      address_id: null,
      device_id: null,
      asset_label: "",
      mac: null,
      hostname: "",
      last_seen_at: null,
      range_kinds: [],
      allocation_method: "unknown",
      allocation_method_source: "unknown",
    };

  it("reserves an address in the internal client's global VRF", async () => {
    const createIpamAddress = vi.fn().mockResolvedValue({});
    stubApi({ createIpamAddress });
    const user = userEvent.setup();
    renderWithProviders(
      <AddressDialog entry={entry} subnetPrefix="10.4.7.0/24" canAssign onClose={() => {}} />,
    );
    // The client comes from `/me`: wait for it before acting, as a person would.
    await waitFor(() => expect(mockUseApi().me).toHaveBeenCalled());
    await user.click(await screen.findByRole("button", { name: "Reserve" }));

    await waitFor(() => expect(createIpamAddress).toHaveBeenCalled());
    expect(bodyOf(createIpamAddress)).toMatchObject({
      address: "10.4.7.9",
      reserve: true,
      client_id: "o1",
    });
  });

  it("sends nothing before `/me` has answered, and says why", async () => {
    const createIpamAddress = vi.fn().mockResolvedValue({});
    // `/me` has not answered: the console cannot know the client yet.
    stubApi({ createIpamAddress, me: vi.fn(() => new Promise(() => {})) });
    const user = userEvent.setup();
    renderWithProviders(
      <AddressDialog entry={entry} subnetPrefix="10.4.7.0/24" canAssign onClose={() => {}} />,
    );
    await user.click(await screen.findByRole("button", { name: "Reserve" }));

    expect(await screen.findByText(/Your account has not loaded yet/)).toBeInTheDocument();
    expect(createIpamAddress).not.toHaveBeenCalled();
  });
});
