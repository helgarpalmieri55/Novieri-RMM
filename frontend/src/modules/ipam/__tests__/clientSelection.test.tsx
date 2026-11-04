import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, ClientSiteOut, MeClient } from "../../../lib/api";
import { ImportPage } from "../ImportPage";
import { RegistryPage } from "../RegistryPage";
import { ScanAuthorizationsPage } from "../ScanAuthorizationsPage";
import { SubnetTreePage } from "../SubnetTreePage";
import type { ScopeOut, SubnetOut, VlanOut, VrfOut } from "../types";

// MSP clients phase 3, design §3.1-§3.4: a new IPAM row takes its client from
// its parent (a VRF, or for a VLAN a site), else from the client chosen at the
// top, else from a required Client field; pickers list the row's client's
// things; and with every client chosen the lists say whose each row is.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = ["ipam.read", "ipam.assign", "ipam.manage", "ipam.scope.manage"];
const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };
const BETA: MeClient = { id: "c-beta", name: "Beta", kind: "customer", enabled_modules: [], permissions: ["ipam.read"] };
const CLIENTS = [INTERNAL, ACME, BETA];

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: CLIENTS, ready: true });
}

const ME = {
  user_id: "u1",
  org_id: "o1",
  email: "a@b.c",
  display_name: "A",
  permissions: CODES,
  permissions_everywhere: ["ipam.read"],
  clients: CLIENTS,
};

function site(id: string, clientId: string, name: string): ClientSiteOut {
  return { id, client_id: clientId, name, address: "", description: "", egress_networks: [], archived_at: null, device_count: 0 };
}
const OWN_SITE = site("site-o", "o1", "Medellín");
const ACME_SITE = site("site-a", "c-acme", "Acme HQ");
const OWN_VRF: VrfOut = { id: "vrf-o", client_id: "o1", name: "lab", rd: null, description: "" };
const ACME_VRF: VrfOut = { id: "vrf-a", client_id: "c-acme", name: "acme-lan", rd: null, description: "" };

function subnet(overrides: Partial<SubnetOut> = {}): SubnetOut {
  return {
    client_id: "o1", id: "s1", vrf_id: "vrf-o", prefix: "10.4.7.0/24", kind: "assignable", name: "",
    description: "", site_id: null, vlan_id: null, gateway: null, dns_servers: [], parent_id: null,
    sweep_enabled: false, sweep_device_id: null, sweep_interval_hours: null, last_swept_at: null,
    ...overrides,
  };
}

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}): Record<string, ReturnType<typeof vi.fn>> {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listIpamVrfs: vi.fn().mockResolvedValue([OWN_VRF, ACME_VRF]),
    listClientSites: vi.fn((clientId: string) =>
      Promise.resolve(clientId === "c-acme" ? [ACME_SITE] : clientId === "o1" ? [OWN_SITE] : []),
    ),
    listIpamVlans: vi.fn().mockResolvedValue([]),
    listIpamSubnets: vi.fn().mockResolvedValue([]),
    listIpamDrift: vi.fn().mockResolvedValue([]),
    listIpamOrphans: vi.fn().mockResolvedValue([]),
    listIpamScanAuthorizations: vi.fn().mockResolvedValue([]),
    listIpamSweepRuns: vi.fn().mockResolvedValue([]),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listSiteServers: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api);
  return api as Record<string, ReturnType<typeof vi.fn>>;
}

function renderAt(node: React.ReactNode, entry: string) {
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

beforeEach(() => selectClient("o1"));

describe("a VRF goes to the client chosen at the top, or the one the form names", () => {
  it("files it under the selected client without asking", async () => {
    selectClient("c-acme");
    const createIpamVrf = vi.fn().mockResolvedValue(ACME_VRF);
    stubApi({ createIpamVrf });
    const user = userEvent.setup();
    renderAt(<RegistryPage />, "/ipam/registry");

    await user.type((await screen.findAllByLabelText("Name"))[0]!, "lan");
    expect(screen.queryByLabelText("Client")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add VRF" }));

    await waitFor(() => expect(createIpamVrf).toHaveBeenCalled());
    expect(bodyOf(createIpamVrf)).toMatchObject({ name: "lan", client_id: "c-acme" });
  });

  it("with every client chosen, asks for the client, offers only those that may, and sends nothing without one", async () => {
    selectClient(null);
    const createIpamVrf = vi.fn().mockResolvedValue(ACME_VRF);
    stubApi({ createIpamVrf });
    const user = userEvent.setup();
    renderAt(<RegistryPage />, "/ipam/registry");

    const field = (await screen.findAllByLabelText("Client"))[0]!;
    // Beta lacks `ipam.manage`; "Unassigned" is never offered (design §3.1, C10).
    expect(within(field).queryByRole("option", { name: "Beta" })).toBeNull();
    await user.type(screen.getAllByLabelText("Name")[0]!, "lan");
    await user.click(screen.getByRole("button", { name: "Add VRF" }));
    expect(createIpamVrf).not.toHaveBeenCalled();

    await user.selectOptions(field, "c-acme");
    await user.click(screen.getByRole("button", { name: "Add VRF" }));
    await waitFor(() => expect(createIpamVrf).toHaveBeenCalledTimes(1));
    expect(bodyOf(createIpamVrf)).toMatchObject({ name: "lan", client_id: "c-acme" });
  });
});

describe("a VLAN: its site decides, and its site picker is its client's", () => {
  it("lists the chosen client's sites and names no client when a site is picked", async () => {
    selectClient(null);
    const createIpamVlan = vi.fn().mockResolvedValue({} as VlanOut);
    const api = stubApi({ createIpamVlan });
    const user = userEvent.setup();
    renderAt(<RegistryPage />, "/ipam/registry");

    const fields = await screen.findAllByLabelText("Client");
    await user.selectOptions(fields[fields.length - 1]!, "c-acme");
    await waitFor(() => expect(api.listClientSites).toHaveBeenCalledWith("c-acme"));
    const picker = screen.getByLabelText("Site");
    await within(picker).findByRole("option", { name: "Acme HQ" });
    expect(within(picker).queryByRole("option", { name: "Medellín" })).toBeNull();

    await user.type(screen.getByLabelText("VID"), "40");
    await user.selectOptions(picker, "site-a");
    await user.click(screen.getByRole("button", { name: "Add VLAN" }));
    await waitFor(() => expect(createIpamVlan).toHaveBeenCalled());
    const body = bodyOf(createIpamVlan);
    expect(body).toMatchObject({ vid: 40, site_id: "site-a" });
    expect(body).not.toHaveProperty("client_id");
  });

  it("files a VLAN with no site under the chosen client", async () => {
    selectClient(null);
    const createIpamVlan = vi.fn().mockResolvedValue({} as VlanOut);
    stubApi({ createIpamVlan });
    const user = userEvent.setup();
    renderAt(<RegistryPage />, "/ipam/registry");

    const fields = await screen.findAllByLabelText("Client");
    await user.selectOptions(fields[fields.length - 1]!, "c-acme");
    await user.type(screen.getByLabelText("VID"), "10");
    await user.click(screen.getByRole("button", { name: "Add VLAN" }));
    await waitFor(() => expect(createIpamVlan).toHaveBeenCalled());
    expect(bodyOf(createIpamVlan)).toMatchObject({ vid: 10, site_id: null, client_id: "c-acme" });
  });
});

describe("with every client chosen, the lists say whose each row is", () => {
  it("shows a Client column on the VLAN list, named from `/me`, and names each VLAN's site", async () => {
    selectClient(null);
    stubApi({
      listIpamVlans: vi.fn().mockResolvedValue([
        { id: "vl-a", client_id: "c-acme", vid: 20, site_id: "site-a", name: "acme", description: "" },
      ]),
    });
    renderAt(<RegistryPage />, "/ipam/registry");

    const table = (await screen.findByText("VLAN 20")).closest("table") as HTMLElement;
    expect(within(table).getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    const row = (await within(table).findByText("VLAN 20")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Acme")).toBeInTheDocument();
    expect(await within(row).findByText("Acme HQ")).toBeInTheDocument();
  });

  it("hides the Client column once a client is chosen at the top", async () => {
    selectClient("c-acme");
    stubApi({
      listIpamVlans: vi.fn().mockResolvedValue([
        { id: "vl-a", client_id: "c-acme", vid: 20, site_id: null, name: "acme", description: "" },
      ]),
    });
    renderAt(<RegistryPage />, "/ipam/registry");
    const table = (await screen.findByText("VLAN 20")).closest("table") as HTMLElement;
    expect(within(table).queryByRole("columnheader", { name: "Client" })).toBeNull();
  });

  it("shows the Client column on the subnet tree and on the scan authorizations", async () => {
    selectClient(null);
    const scope: ScopeOut = {
      client_id: "c-acme", id: "sc", vrf_id: "vrf-a", prefix: "10.9.0.0/24", note: "", authorized_by: "u1",
      authorized_at: new Date().toISOString(), expires_at: null, revoked_at: null,
    };
    stubApi({
      listIpamSubnets: vi.fn().mockResolvedValue([subnet({ client_id: "c-acme", vrf_id: "vrf-a" })]),
      listIpamScanAuthorizations: vi.fn().mockResolvedValue([scope]),
    });
    const { unmount } = renderAt(<SubnetTreePage />, "/ipam");
    const tree = (await screen.findByText("10.4.7.0/24")).closest("table") as HTMLElement;
    expect(within(tree).getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(tree).getByText("Acme")).toBeInTheDocument();
    unmount();

    renderAt(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    const grants = (await screen.findByText("10.9.0.0/24")).closest("table") as HTMLElement;
    expect(within(grants).getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(grants).getByText("Acme")).toBeInTheDocument();
  });
});

describe("a subnet and a scan authorization: the VRF decides, else the client", () => {
  it("asks for the client only while no VRF is chosen", async () => {
    selectClient(null);
    const createIpamSubnet = vi.fn().mockResolvedValue(subnet());
    stubApi({ createIpamSubnet });
    const user = userEvent.setup();
    renderAt(<SubnetTreePage />, "/ipam");
    await user.click(await screen.findByRole("button", { name: "New subnet" }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24");
    await user.selectOptions(within(dialog).getByLabelText("Client"), "c-acme");
    await user.click(within(dialog).getByRole("button", { name: "Register" }));
    await waitFor(() => expect(createIpamSubnet).toHaveBeenCalledTimes(1));
    expect(bodyOf(createIpamSubnet)).toMatchObject({ vrf_id: null, client_id: "c-acme" });

    await user.selectOptions(within(dialog).getByLabelText("VRF"), "vrf-o");
    expect(within(dialog).queryByLabelText("Client")).toBeNull();
  });

  it("files an authorization with no VRF under the client chosen at the top", async () => {
    selectClient("c-acme");
    const createIpamScanAuthorization = vi.fn().mockResolvedValue({});
    stubApi({ createIpamScanAuthorization });
    const user = userEvent.setup();
    renderAt(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    await user.click(await screen.findByRole("button", { name: "Authorize a subnet" }));
    await user.type(await screen.findByLabelText("Prefix"), "10.4.7.0/24");
    expect(screen.queryByLabelText("Client")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Authorize" }));
    await waitFor(() => expect(createIpamScanAuthorization).toHaveBeenCalled());
    expect(bodyOf(createIpamScanAuthorization)).toMatchObject({ prefix: "10.4.7.0/24", vrf_id: null, client_id: "c-acme" });
  });
});

describe("an import with no VRF listed names a client", () => {
  const PREVIEW = {
    digest: "a".repeat(64), total_rows: 1, creates: 1, updates: 0, conflicts: 0, errors: 0,
    rows: [{ line: 2, action: "create", address: "10.4.7.9", error: "", detail: {} }],
  };

  it("asks for it with every client chosen, and a new choice clears the preview", async () => {
    selectClient(null);
    const previewIpamImport = vi.fn().mockResolvedValue(PREVIEW);
    const applyIpamImport = vi.fn().mockResolvedValue({ ...PREVIEW, applied: 1 });
    stubApi({ previewIpamImport, applyIpamImport, listIpamVrfs: vi.fn().mockResolvedValue([]) });
    const user = userEvent.setup();
    renderAt(<ImportPage />, "/ipam/import");

    const input = (await screen.findByLabelText("CSV file")) as HTMLInputElement;
    await user.upload(input, new File(["address\n10.4.7.9\n"], "plan.csv", { type: "text/csv" }));
    await user.selectOptions(await screen.findByLabelText("address"), "address");
    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Apply" })).not.toBeDisabled());
    expect(previewIpamImport.mock.calls[0]?.[2]).toEqual({ clientId: "c-acme" });

    await user.selectOptions(screen.getByLabelText("Client"), "o1");
    expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  });
});

// Phase 3 final review C, Important 1 (operator decision 2): the shared hook
// `useNewRowClientField` brings the "Client is required" rule these forms
// lacked -- they sent nothing and said only "the client this is for has not
// been chosen" -- and, with Unassigned chosen at the top, they say that nothing
// new can be filed there instead of offering a create that can only fail.
const UNASSIGNED: MeClient = { id: "c-unassigned", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: CODES };
const REFUSED = "Unassigned is the client chosen, and nothing new can be filed under it. Choose another client first.";

function selectUnassigned(): void {
  mockClientScope.mockReturnValue({ selected: UNASSIGNED.id, select: vi.fn(), clients: [...CLIENTS, UNASSIGNED], ready: true });
}

describe("with every client chosen, an empty Client field is named on create", () => {
  it("a VRF", async () => {
    selectClient(null);
    const createIpamVrf = vi.fn().mockResolvedValue(ACME_VRF);
    stubApi({ createIpamVrf });
    const user = userEvent.setup();
    renderAt(<RegistryPage />, "/ipam/registry");
    await user.type((await screen.findAllByLabelText("Name"))[0]!, "lan");
    await user.click(screen.getByRole("button", { name: "Add VRF" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Client is required");
    expect(createIpamVrf).not.toHaveBeenCalled();
  });

  it("a VLAN", async () => {
    selectClient(null);
    const createIpamVlan = vi.fn().mockResolvedValue({} as VlanOut);
    stubApi({ createIpamVlan });
    const user = userEvent.setup();
    renderAt(<RegistryPage />, "/ipam/registry");
    await user.type(await screen.findByLabelText("VID"), "40");
    await user.click(screen.getByRole("button", { name: "Add VLAN" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Client is required");
    expect(createIpamVlan).not.toHaveBeenCalled();
  });

  it("a subnet with no VRF", async () => {
    selectClient(null);
    const createIpamSubnet = vi.fn().mockResolvedValue(subnet());
    stubApi({ createIpamSubnet });
    const user = userEvent.setup();
    renderAt(<SubnetTreePage />, "/ipam");
    await user.click(await screen.findByRole("button", { name: "New subnet" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24");
    await user.click(within(dialog).getByRole("button", { name: "Register" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Client is required");
    expect(createIpamSubnet).not.toHaveBeenCalled();
  });

  it("a scan authorization with no VRF", async () => {
    selectClient(null);
    const createIpamScanAuthorization = vi.fn().mockResolvedValue({});
    stubApi({ createIpamScanAuthorization });
    const user = userEvent.setup();
    renderAt(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    await user.click(await screen.findByRole("button", { name: "Authorize a subnet" }));
    await user.type(await screen.findByLabelText("Prefix"), "10.4.7.0/24");
    await user.click(screen.getByRole("button", { name: "Authorize" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Client is required");
    expect(createIpamScanAuthorization).not.toHaveBeenCalled();
  });

  it("an import with no VRF listed", async () => {
    selectClient(null);
    const previewIpamImport = vi.fn();
    stubApi({ previewIpamImport, listIpamVrfs: vi.fn().mockResolvedValue([]) });
    const user = userEvent.setup();
    renderAt(<ImportPage />, "/ipam/import");
    const input = (await screen.findByLabelText("CSV file")) as HTMLInputElement;
    await user.upload(input, new File(["address\n10.4.7.9\n"], "plan.csv", { type: "text/csv" }));
    await user.selectOptions(await screen.findByLabelText("address"), "address");
    await screen.findByLabelText("Client");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Client is required");
    expect(previewIpamImport).not.toHaveBeenCalled();
  });
});

describe("with Unassigned chosen at the top, nothing new is offered and the page says why", () => {
  it("the VRF and VLAN add rows", async () => {
    selectUnassigned();
    stubApi({ listIpamVrfs: vi.fn().mockResolvedValue([]) });
    renderAt(<RegistryPage />, "/ipam/registry");
    expect(await screen.findAllByText(REFUSED)).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Add VRF" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add VLAN" })).toBeNull();
    expect(screen.queryByLabelText("Client")).toBeNull();
  });

  it("a subnet, until a VRF names its client", async () => {
    selectUnassigned();
    stubApi();
    const user = userEvent.setup();
    renderAt(<SubnetTreePage />, "/ipam");
    await user.click(await screen.findByRole("button", { name: "New subnet" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(REFUSED)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Register" })).toBeNull();
    // A VRF is a parent: the subnet is its client's, whatever the top bar says.
    await user.selectOptions(within(dialog).getByLabelText("VRF"), "vrf-a");
    expect(within(dialog).queryByText(REFUSED)).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Register" })).toBeInTheDocument();
  });

  it("a scan authorization, until a VRF names its client", async () => {
    selectUnassigned();
    stubApi();
    const user = userEvent.setup();
    renderAt(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    await user.click(await screen.findByRole("button", { name: "Authorize a subnet" }));
    expect(await screen.findByText(REFUSED)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Authorize" })).toBeNull();
    await user.selectOptions(screen.getByLabelText("VRF"), "vrf-a");
    expect(screen.queryByText(REFUSED)).toBeNull();
    expect(screen.getByRole("button", { name: "Authorize" })).toBeInTheDocument();
  });

  it("an import with no VRF listed", async () => {
    selectUnassigned();
    const previewIpamImport = vi.fn();
    stubApi({ previewIpamImport, listIpamVrfs: vi.fn().mockResolvedValue([]) });
    renderAt(<ImportPage />, "/ipam/import");
    expect(await screen.findByText(REFUSED)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Preview" })).toBeNull();
  });
});

describe("the site directory beside the VRFs", () => {
  it("is loading, not empty, while the VRFs and VLANs that name its clients load", async () => {
    // Phase 3 final review C, Minor (`ipam/api.ts` `useIpamSiteDirectory`):
    // with every client chosen the clients come from the VRF and VLAN reads,
    // and before they answered the panel said "no sites".
    selectClient(null);
    stubApi({
      listIpamVrfs: vi.fn(() => new Promise(() => {})),
      listIpamVlans: vi.fn(() => new Promise(() => {})),
    });
    renderAt(<RegistryPage />, "/ipam/registry");
    await screen.findByText("Sites");
    expect(screen.queryByText("This client has no sites yet.")).toBeNull();
  });
});
