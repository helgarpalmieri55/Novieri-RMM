import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, ClientSiteOut, DeviceGroup, MeClient } from "../../lib/api";
import { AddAssetDialog } from "./AddAssetDialog";
import { GroupsPage } from "./GroupsPage";

// MSP clients phase 3, design §3.1-§3.4: a new group takes its parent group's
// client, else the client chosen at the top, else a required Client field; its
// parent picker lists that client's groups; the list says whose each group is.
// A hand-made asset asks for its site (§3.2), among its client's.

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = ["devices.read", "devices.manage", "assets.manage"];
const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };
const CLIENTS = [INTERNAL, ACME];

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: CLIENTS, ready: true });
}

// Phase 3 final review D, Important 2: Beta's people may READ its devices but
// neither organise them nor add assets; the Unassigned client is never offered.
const BETA: MeClient = { id: "c-beta", name: "Beta", kind: "customer", enabled_modules: [], permissions: ["devices.read"] };
const UNASSIGNED: MeClient = { id: "c-unassigned", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: CODES };

/** Every client chosen, among clients one of which only reads. */
function selectEveryClientWithAReader(): void {
  mockClientScope.mockReturnValue({ selected: null, select: vi.fn(), clients: [...CLIENTS, BETA, UNASSIGNED], ready: true });
}

function offered(field: HTMLElement): (string | null)[] {
  return within(field).getAllByRole("option").map((option) => option.textContent);
}

const ME = { user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: CODES, permissions_everywhere: [], clients: CLIENTS };

function group(overrides: Partial<DeviceGroup>): DeviceGroup {
  return {
    id: "g1", client_id: "o1", name: "Servers", parent_group_id: null, description: "", dynamic: false,
    device_count: 0, precedence: 0, archived_at: null, ...overrides,
  };
}

const NOW = new Date().toISOString();
const GROUPS = {
  items: [group({ id: "g-own", name: "Own servers" }), group({ id: "g-acme", client_id: "c-acme", name: "Acme desks" })],
  total: 2,
  ungrouped_device_count: 0,
  evaluation: {
    last_evaluated_at: NOW, last_full_pass_at: NOW, full_pass: false, groups_evaluated: 2,
    devices_evaluated: 0, devices_moved: 0, devices_without_inventory: 0, interval_seconds: 300,
  },
};

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}): Record<string, ReturnType<typeof vi.fn>> {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listDeviceGroups: vi.fn().mockResolvedValue(GROUPS),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    createDeviceGroup: vi.fn().mockResolvedValue(group({})),
    previewDeviceGroupRules: vi.fn(),
    listClientSites: vi.fn((clientId: string) =>
      Promise.resolve<ClientSiteOut[]>(
        clientId === "c-acme"
          ? [{ id: "site-a", client_id: "c-acme", name: "Acme HQ", address: "", description: "", egress_networks: [], archived_at: null, device_count: 0 }]
          : [],
      ),
    ),
    createAsset: vi.fn().mockResolvedValue({ id: "d1" }),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api);
  return api as Record<string, ReturnType<typeof vi.fn>>;
}

function renderIt(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o1"));

describe("a new device group", () => {
  it("files it under the client chosen at the top, which is not the organisation's own", async () => {
    // `org_id` is "o1": the phase 1-2 rule filed every new row there whoever
    // was chosen, so only a client other than "o1" tells the two rules apart
    // (phase 3 final review D, Important 1).
    selectClient("c-acme");
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    await user.click(await screen.findByRole("button", { name: "New group" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("Client")).toBeNull();
    await user.type(within(dialog).getByLabelText("Name"), "desks");
    await user.click(within(dialog).getByRole("button", { name: "Create group" }));
    await waitFor(() => expect(api.createDeviceGroup).toHaveBeenCalledTimes(1));
    expect(api.createDeviceGroup!.mock.calls[0]?.[0]).toMatchObject({ name: "desks", client_id: "c-acme" });
  });

  it("with every client chosen, asks for the client and files the group there", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    await user.click(await screen.findByRole("button", { name: "New group" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "desks");
    await user.selectOptions(within(dialog).getByLabelText("Client"), "c-acme");
    await user.click(within(dialog).getByRole("button", { name: "Create group" }));
    await waitFor(() => expect(api.createDeviceGroup).toHaveBeenCalled());
    expect(api.createDeviceGroup!.mock.calls[0]?.[0]).toMatchObject({ name: "desks", client_id: "c-acme" });
  });

  it("with every client chosen, offers only the clients holding devices.manage, never Unassigned", async () => {
    selectEveryClientWithAReader();
    stubApi();
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    await user.click(await screen.findByRole("button", { name: "New group" }));
    const dialog = await screen.findByRole("dialog");
    expect(offered(within(dialog).getByLabelText("Client"))).toEqual(["Choose a client", "Novieri", "Acme"]);
  });

  it("takes the parent group's client and stops asking once a parent is picked", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    await user.click(await screen.findByRole("button", { name: "New group" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "desks floor 2");
    await user.click(within(dialog).getByRole("combobox", { name: /Parent group/ }));
    await user.click(await screen.findByRole("option", { name: /Acme desks/ }));
    expect(within(dialog).queryByLabelText("Client")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Create group" }));
    await waitFor(() => expect(api.createDeviceGroup).toHaveBeenCalled());
    expect(api.createDeviceGroup!.mock.calls[0]?.[0]).toMatchObject({ parent_group_id: "g-acme", client_id: "c-acme" });
  });

  it("offers as parents only the groups of the client the new group is for", async () => {
    selectClient("c-acme");
    stubApi();
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    await user.click(await screen.findByRole("button", { name: "New group" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: /Parent group/ }));
    expect(await screen.findByRole("option", { name: /Acme desks/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Own servers/ })).toBeNull();
  });

  it("shows a Client column with every client chosen, and hides it with one", async () => {
    selectClient(null);
    stubApi();
    const { unmount } = renderIt(<GroupsPage />);
    const row = (await screen.findByText("Acme desks")).closest("tr") as HTMLElement;
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(row).getByText("Acme")).toBeInTheDocument();
    unmount();

    selectClient("o1");
    renderIt(<GroupsPage />);
    await screen.findByText("Own servers");
    expect(screen.queryByRole("columnheader", { name: "Client" })).toBeNull();
  });
});

describe("a hand-made asset", () => {
  it("with a client chosen at the top that is not the organisation's own, files it at that client's site", async () => {
    // The body names a site, and the server takes the client from it; so the
    // site offered must be the chosen client's ("c-acme"), not `org_id`'s ("o1").
    selectClient("c-acme");
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<AddAssetDialog open onClose={() => {}} />);
    await waitFor(() => expect((screen.getByLabelText("Site") as HTMLSelectElement).value).toBe("site-a"));
    expect(screen.queryByLabelText("Client")).toBeNull();
    expect(api.listClientSites).toHaveBeenCalledWith("c-acme");
    expect(api.listClientSites).not.toHaveBeenCalledWith("o1");
    await user.click(screen.getByRole("button", { name: /add asset/i }));
    await waitFor(() => expect(api.createAsset).toHaveBeenCalledTimes(1));
    expect(api.createAsset!.mock.calls[0]?.[0]).toMatchObject({ site_id: "site-a" });
  });

  it("asks for the client and then its site, and files the asset there", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<AddAssetDialog open onClose={() => {}} />);
    await user.selectOptions(await screen.findByLabelText("Client"), "c-acme");
    // The only site of Acme is chosen for the person (`SiteField`).
    await waitFor(() => expect((screen.getByLabelText("Site") as HTMLSelectElement).value).toBe("site-a"));
    await user.click(screen.getByRole("button", { name: /add asset/i }));
    await waitFor(() => expect(api.createAsset).toHaveBeenCalled());
    expect(api.createAsset!.mock.calls[0]?.[0]).toMatchObject({ site_id: "site-a" });
  });

  it("with every client chosen, offers only the clients holding assets.manage, never Unassigned", async () => {
    selectEveryClientWithAReader();
    stubApi();
    renderIt(<AddAssetDialog open onClose={() => {}} />);
    expect(offered(await screen.findByLabelText("Client"))).toEqual(["Choose a client", "Novieri", "Acme"]);
  });

  it("sends nothing without a site, and names the field", async () => {
    selectClient("o1");
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<AddAssetDialog open onClose={() => {}} />);
    await user.click(await screen.findByRole("button", { name: /add asset/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Site is required");
    expect(api.createAsset).not.toHaveBeenCalled();
  });
});

// Phase 3 final review C, Important 1 (operator decision 2): the shared hook
// brings the "Client is required" rule the group editor lacked, and with
// Unassigned chosen at the top the forms say nothing new can be filed there
// instead of offering a create that can only fail.
const REFUSED = "Unassigned is the client chosen, and nothing new can be filed under it. Choose another client first.";

function selectUnassigned(): void {
  mockClientScope.mockReturnValue({ selected: UNASSIGNED.id, select: vi.fn(), clients: [...CLIENTS, UNASSIGNED], ready: true });
}

describe("the new-row client, through the shared hook", () => {
  it("a new group, with every client chosen, names the empty Client field and sends nothing", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    await user.click(await screen.findByRole("button", { name: "New group" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "desks");
    await user.click(within(dialog).getByRole("button", { name: "Create group" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Client is required");
    expect(api.createDeviceGroup).not.toHaveBeenCalled();
  });

  it("a new group, with Unassigned chosen at the top, says so and offers no create", async () => {
    selectUnassigned();
    stubApi({ listDeviceGroups: vi.fn().mockResolvedValue({ ...GROUPS, items: [], total: 0 }) });
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    // Unassigned holds no group, so the empty state offers "New group" too: the head's is first.
    await screen.findByText("No groups yet");
    await user.click(screen.getAllByRole("button", { name: "New group" })[0]!);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(REFUSED)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Create group" })).toBeNull();
  });

  it("a hand-made asset, with Unassigned chosen at the top, says so and offers no create", async () => {
    selectUnassigned();
    const api = stubApi();
    renderIt(<AddAssetDialog open onClose={() => {}} />);
    expect(await screen.findByText(REFUSED)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add asset/i })).toBeNull();
    expect(screen.queryByLabelText("Client")).toBeNull();
    expect(api.listClientSites).not.toHaveBeenCalled();
  });
});

// Phase 3 final review C, Important 3: the member picker read one unfiltered
// page of every client's devices, and the server's per-device refusal
// (`moved: false`, "belongs to another client") was thrown away.
describe("a group's member picker", () => {
  function machine(id: string, clientId: string, hostname: string, groupId: string | null = null) {
    return { id, client_id: clientId, hostname, description: null, device_group_id: groupId };
  }

  async function openMembers(user: ReturnType<typeof userEvent.setup>, name: string): Promise<HTMLElement> {
    const row = (await screen.findByText(name)).closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Members" }));
    return screen.getByRole("dialog", { name: `Members of ${name}` });
  }

  it("reads the devices of the group's own client, and offers only those", async () => {
    selectClient(null);
    const listDevices = vi.fn((params?: { clientId?: string }) =>
      Promise.resolve(
        params?.clientId === "c-acme"
          ? { items: [machine("d-a", "c-acme", "acme-ws")], total: 1 }
          : { items: [machine("d-a", "c-acme", "acme-ws"), machine("d-o", "o1", "own-ws")], total: 2 },
      ),
    );
    stubApi({ listDevices });
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    const dialog = await openMembers(user, "Acme desks");
    await waitFor(() => expect(listDevices).toHaveBeenCalledWith(expect.objectContaining({ clientId: "c-acme" })));
    await user.click(within(dialog).getByRole("combobox", { name: "Add a device" }));
    expect(await within(dialog).findByRole("option", { name: /acme-ws/ })).toBeInTheDocument();
    expect(within(dialog).queryByRole("option", { name: /own-ws/ })).toBeNull();
  });

  it("says why a device was not moved, in the person's language", async () => {
    selectClient(null);
    const addDeviceGroupMembers = vi.fn().mockResolvedValue({
      results: [{ device_id: "d-a", moved: false, reason: "belongs to another client" }],
    });
    stubApi({
      listDevices: vi.fn().mockResolvedValue({ items: [machine("d-a", "c-acme", "acme-ws")], total: 1 }),
      addDeviceGroupMembers,
    });
    const user = userEvent.setup();
    renderIt(<GroupsPage />);
    const dialog = await openMembers(user, "Acme desks");
    await user.click(within(dialog).getByRole("combobox", { name: "Add a device" }));
    await user.click(await within(dialog).findByRole("option", { name: /acme-ws/ }));
    await user.click(within(dialog).getByRole("button", { name: "Add to group" }));
    await waitFor(() => expect(addDeviceGroupMembers).toHaveBeenCalledWith("g-acme", ["d-a"]));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "acme-ws was not added: it belongs to another client.",
    );
  });
});
