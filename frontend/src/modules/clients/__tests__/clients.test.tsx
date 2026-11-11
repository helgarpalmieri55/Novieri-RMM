/**
 * The Clients page (MSP clients phase 3, design §4.1): the list, a client's
 * four tabs and their refusals. The console gates nothing (invariant 6): what
 * is asserted is what it OFFERS, from `/me` -- the server checks every route.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import type {
  ApiClient,
  ClientOut,
  ClientSiteOut,
  ClientUpdate,
  ContactOut,
  InstallLinkSettings,
  Me,
  MeClient,
} from "../../../lib/api";
import type { AssignmentOut } from "../../health/types";
import { ClientDetailPage } from "../ClientDetailPage";
import { ClientsPage } from "../ClientsPage";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const scope = vi.hoisted(() => ({
  selected: null as string | null,
  clients: [] as MeClient[],
  select: vi.fn(),
  // `false`: `/me` has not answered (or does not list the top bar's choice).
  ready: true,
}));
vi.mock("../../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientScope")>()),
  useClientScope: () => ({
    selected: scope.selected,
    select: scope.select,
    clients: scope.clients,
    ready: scope.ready,
  }),
}));

const ANDES: ClientOut = {
  id: "c-andes",
  name: "Andes Logistics",
  kind: "customer",
  status: "active",
  email_domains: ["andes.example"],
  phone: "+57 1 555 0100",
  address: "Calle 80 #10-20",
  website: "andes.example",
  notes: "",
  enabled_modules: ["budget"],
  device_count: 3,
  site_count: 2,
};
const INTERNAL: ClientOut = {
  ...ANDES,
  id: "org-1",
  name: "Toeshee",
  kind: "internal",
  email_domains: [],
  enabled_modules: ["budget", "bcp", "change"],
  device_count: 40,
  site_count: 1,
};
const UNASSIGNED: ClientOut = {
  ...ANDES,
  id: "c-unassigned",
  name: "Unassigned",
  kind: "unassigned",
  email_domains: [],
  enabled_modules: [],
  device_count: 0,
  site_count: 0,
};

const MAIN: ClientSiteOut = {
  id: "s-main",
  client_id: "c-andes",
  name: "Principal",
  address: "Calle 80 #10-20",
  description: "",
  egress_networks: [],
  archived_at: null,
  device_count: 3,
};
const NORTH: ClientSiteOut = { ...MAIN, id: "s-north", name: "Bodega norte", address: "", device_count: 0 };
const GONE: ClientSiteOut = {
  ...MAIN,
  id: "s-gone",
  name: "Old office",
  address: "",
  archived_at: "2026-09-01T00:00:00Z",
  device_count: 0,
};

const ANA: ContactOut = {
  id: "ct-ana",
  client_id: "c-andes",
  kind: "contact",
  email: "ana@andes.example",
  display_name: "Ana Rojas",
  phone: null,
  status: "active",
  access: "active",
  invite_expires_at: null,
};

const LINK: InstallLinkSettings = {
  enabled: false,
  has_code: false,
  networks: [],
  disable_at: null,
  hourly_limit: 20,
  enabled_at: null,
  joined_since_enabled: 0,
};

const HEALTH_ORG: AssignmentOut = {
  id: "ha-org",
  profile_id: "hp-1",
  profile_name: "Baseline",
  device_id: null,
  device_group_id: null,
  client_id: null,
  level: "org",
  created_by: null,
  created_at: "2026-10-01T00:00:00Z",
};
const HEALTH_ANDES: AssignmentOut = {
  ...HEALTH_ORG,
  id: "ha-andes",
  profile_id: "hp-2",
  profile_name: "Servers",
  client_id: "c-andes",
  level: "client",
};

const ALL_CODES = ["clients.read", "clients.manage", "sites.manage", "health.read", "enrollment.link.manage"];

function meClient(client: ClientOut, permissions: string[] = ALL_CODES): MeClient {
  return {
    id: client.id,
    name: client.name,
    kind: client.kind,
    enabled_modules: client.enabled_modules,
    permissions,
  };
}

function me(everywhere: string[] = ALL_CODES): Me {
  return {
    user_id: "u1",
    org_id: "org-1",
    email: "op@example.com",
    display_name: "Operator",
    permissions: ALL_CODES,
    permissions_everywhere: everywhere,
    clients: [],
  };
}

function api(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    me: vi.fn().mockResolvedValue(me()),
    listClients: vi.fn().mockResolvedValue([INTERNAL, ANDES, UNASSIGNED]),
    getClientRecord: vi.fn().mockResolvedValue(ANDES),
    listClientSites: vi.fn().mockResolvedValue([MAIN, NORTH, GONE]),
    listContacts: vi.fn().mockResolvedValue([ANA]),
    listHealthAssignments: vi.fn().mockResolvedValue([]),
    getInstallLink: vi.fn().mockResolvedValue(LINK),
    getMySecurity: vi.fn().mockResolvedValue({
      kind: "staff",
      has_password: true,
      microsoft: null,
      has_totp: true,
      passkeys: [],
      recovery_codes_left: 8,
      trusted_devices: [],
      sessions: [],
    }),
    stepUpTotp: vi.fn().mockResolvedValue(undefined),
    getCurrentOrg: vi.fn().mockResolvedValue({ id: "org-1", name: "Toeshee", settings: {} }),
    ...overrides,
  } as unknown as ApiClient;
}

function renderAt(entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/clients" element={<ClientsPage />} />
            <Route path="/clients/:clientId" element={<ClientDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    queryClient,
  };
}

beforeEach(() => {
  mockUseApi.mockReset();
  scope.selected = null;
  scope.clients = [meClient(INTERNAL), meClient(ANDES), meClient(UNASSIGNED)];
  scope.select = vi.fn();
  scope.ready = true;
});

describe("the Clients list", () => {
  it("lists each client with its kind, devices, sites and status, linking to its page", async () => {
    mockUseApi.mockReturnValue(api());
    renderAt("/clients");
    const link = await screen.findByRole("link", { name: "Andes Logistics" });
    expect(link).toHaveAttribute("href", "/clients/c-andes");
    const row = link.closest("tr") as HTMLElement;
    expect(within(row).getByText("Customer")).toBeInTheDocument();
    expect(within(row).getByText("3")).toBeInTheDocument();
    expect(within(row).getByText("2")).toBeInTheDocument();
    expect(within(row).getByText("Active")).toBeInTheDocument();
    const own = screen.getByRole("link", { name: "Toeshee" }).closest("tr") as HTMLElement;
    expect(within(own).getByText("Your own IT")).toBeInTheDocument();
  });

  it("reads archived clients only when asked to", async () => {
    const listClients = vi.fn().mockResolvedValue([INTERNAL, ANDES]);
    mockUseApi.mockReturnValue(api({ listClients }));
    const user = userEvent.setup();
    renderAt("/clients");
    await screen.findByRole("link", { name: "Andes Logistics" });
    expect(listClients).toHaveBeenLastCalledWith({});
    await user.click(screen.getByRole("switch", { name: "Show archived clients" }));
    await waitFor(() => expect(listClients).toHaveBeenLastCalledWith({ include_archived: true }));
  });

  it("offers New client only to a person holding clients.manage on every client", async () => {
    const without = vi.fn().mockResolvedValue(me(["clients.read"]));
    mockUseApi.mockReturnValue(api({ me: without }));
    const { unmount } = renderAt("/clients");
    await screen.findByRole("link", { name: "Andes Logistics" });
    await waitFor(() => expect(without).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "New client" })).toBeNull();
    unmount();

    mockUseApi.mockReturnValue(api());
    renderAt("/clients");
    expect(await screen.findByRole("button", { name: "New client" })).toBeInTheDocument();
  });

  it("creates a client with its domains and modules, then opens it", async () => {
    const created: ClientOut = {
      ...ANDES,
      id: "c-new",
      name: "Bodegas del Sur",
      email_domains: ["bodegas.example", "sur.example"],
      enabled_modules: ["change"],
      device_count: 0,
      site_count: 1,
    };
    const createClient = vi.fn().mockResolvedValue(created);
    mockUseApi.mockReturnValue(api({ createClient, getClientRecord: vi.fn().mockResolvedValue(created) }));
    const user = userEvent.setup();
    renderAt("/clients");
    await user.click(await screen.findByRole("button", { name: "New client" }));
    const dialog = await screen.findByRole("dialog", { name: "New client" });
    await user.type(within(dialog).getByLabelText("Name"), "Bodegas del Sur");
    await user.type(within(dialog).getByLabelText("Email domains"), "bodegas.example{enter}sur.example");
    await user.type(within(dialog).getByLabelText("Phone"), "+57 1 555 0199");
    await user.click(within(dialog).getByRole("checkbox", { name: "Changes" }));
    await user.click(within(dialog).getByRole("button", { name: "Create client" }));

    await waitFor(() => expect(createClient).toHaveBeenCalledTimes(1));
    expect(createClient).toHaveBeenCalledWith({
      name: "Bodegas del Sur",
      email_domains: ["bodegas.example", "sur.example"],
      phone: "+57 1 555 0199",
      address: "",
      website: "",
      notes: "",
      enabled_modules: ["change"],
    });
    expect(await screen.findByRole("heading", { name: "Bodegas del Sur", level: 1 })).toBeInTheDocument();
  });

  it("names the field a create is missing instead of sending it", async () => {
    const createClient = vi.fn();
    mockUseApi.mockReturnValue(api({ createClient }));
    const user = userEvent.setup();
    renderAt("/clients");
    await user.click(await screen.findByRole("button", { name: "New client" }));
    const dialog = await screen.findByRole("dialog", { name: "New client" });
    await user.click(within(dialog).getByRole("button", { name: "Create client" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Name");
    expect(createClient).not.toHaveBeenCalled();
  });
});

describe("a client's Summary", () => {
  it("shows the client's details and its own default health profile", async () => {
    mockUseApi.mockReturnValue(
      api({ listHealthAssignments: vi.fn().mockResolvedValue([HEALTH_ORG, HEALTH_ANDES]) }),
    );
    renderAt("/clients/c-andes");
    expect(await screen.findByRole("heading", { name: "Andes Logistics", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Calle 80 #10-20")).toBeInTheDocument();
    expect(await screen.findByText("Servers, this client's own default.")).toBeInTheDocument();
  });

  it("says the organisation default applies when the client has none of its own", async () => {
    mockUseApi.mockReturnValue(api({ listHealthAssignments: vi.fn().mockResolvedValue([HEALTH_ORG]) }));
    renderAt("/clients/c-andes");
    expect(
      await screen.findByText("None of its own: its devices use the organisation default, Baseline."),
    ).toBeInTheDocument();
  });

  it("does not read health profiles for a person without health.read on the client", async () => {
    scope.clients = [meClient(ANDES, ["clients.read"])];
    const listHealthAssignments = vi.fn().mockResolvedValue([]);
    mockUseApi.mockReturnValue(api({ listHealthAssignments }));
    renderAt("/clients/c-andes");
    await screen.findByRole("heading", { name: "Andes Logistics", level: 1 });
    expect(screen.queryByText("Default health profile")).toBeNull();
    expect(listHealthAssignments).not.toHaveBeenCalled();
  });

  it("explains a refused archive in the server's words", async () => {
    const archiveClient = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "this client has 3 active devices; retire them before archiving it" })),
    );
    mockUseApi.mockReturnValue(api({ archiveClient }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes");
    await user.click(await screen.findByRole("button", { name: "Archive client" }));
    const dialog = await screen.findByRole("dialog", { name: "Archive this client" });
    await user.click(within(dialog).getByRole("button", { name: "Archive" }));
    expect(archiveClient).toHaveBeenCalledWith("c-andes");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The client was not archived. this client has 3 active devices; retire them before archiving it",
    );
  });

  it("does not offer Archive for the internal or the unassigned client, nor without every client", async () => {
    for (const client of [INTERNAL, UNASSIGNED]) {
      scope.clients = [meClient(client)];
      const meFn = vi.fn().mockResolvedValue(me());
      mockUseApi.mockReturnValue(api({ me: meFn, getClientRecord: vi.fn().mockResolvedValue(client) }));
      const { unmount } = renderAt(`/clients/${client.id}`);
      await screen.findByRole("heading", { name: client.name, level: 1 });
      await waitFor(() => expect(meFn).toHaveBeenCalled());
      expect(screen.queryByRole("button", { name: "Archive client" })).toBeNull();
      unmount();
    }
    scope.clients = [meClient(ANDES)];
    const narrow = vi.fn().mockResolvedValue(me(["clients.read"]));
    mockUseApi.mockReturnValue(api({ me: narrow }));
    renderAt("/clients/c-andes");
    await screen.findByRole("heading", { name: "Andes Logistics", level: 1 });
    await waitFor(() => expect(narrow).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Archive client" })).toBeNull();
  });

  it("says when the console works on another client, and reads nothing", async () => {
    scope.selected = "org-1";
    const getClientRecord = vi.fn().mockResolvedValue(ANDES);
    mockUseApi.mockReturnValue(api({ getClientRecord }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes");
    expect(await screen.findByText(/The console is working on Toeshee/)).toBeInTheDocument();
    expect(getClientRecord).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Work on this client" }));
    expect(scope.select).toHaveBeenCalledWith("c-andes");
  });

  // Phase 3 final review D (T12): neither branch below had a test.
  it("offers to work on all my clients when the page's client is not one the person may choose", async () => {
    // `/me` lists no Andes (archived, say, or no role there), so the top bar
    // cannot choose it: the one act that reads it is "all my clients".
    scope.selected = "org-1";
    scope.clients = [meClient(INTERNAL), meClient(UNASSIGNED)];
    const getClientRecord = vi.fn().mockResolvedValue(ANDES);
    mockUseApi.mockReturnValue(api({ getClientRecord }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes");
    expect(await screen.findByText(/The console is working on Toeshee/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Work on this client" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Work on all my clients" }));
    expect(scope.select).toHaveBeenCalledExactlyOnceWith(null);
    expect(getClientRecord).not.toHaveBeenCalled();
  });

  // Phase 3 final review C (T12 minor): on a cold deep link the top bar's
  // choice is known before `/me` is, and the page named the chosen client by
  // its raw id and offered "Work on all my clients" for a client that is the
  // person's own. It waits for `/me` now, and reads nothing meanwhile.
  it("waits for /me before deciding anything on a cold deep link", async () => {
    scope.selected = "org-1";
    scope.clients = [];
    scope.ready = false;
    const getClientRecord = vi.fn().mockResolvedValue(ANDES);
    mockUseApi.mockReturnValue(api({ getClientRecord }));
    renderAt("/clients/c-andes");
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(/The console is working on/)).toBeNull();
    expect(screen.queryByText(/org-1/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Work on all my clients" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Work on this client" })).toBeNull();
    expect(getClientRecord).not.toHaveBeenCalled();
  });

  it("says an archived client is archived, above its tabs", async () => {
    // `/me` leaves an archived client out; the page is reached from the list's
    // "Show archived clients", with every client chosen.
    scope.clients = [meClient(INTERNAL), meClient(UNASSIGNED)];
    mockUseApi.mockReturnValue(api({ getClientRecord: vi.fn().mockResolvedValue({ ...ANDES, status: "archived" }) }));
    renderAt("/clients/c-andes");
    expect(await screen.findByRole("heading", { name: "Andes Logistics", level: 1 })).toBeInTheDocument();
    const notice = screen.getByText("This client is archived. Its records are kept and take no changes.");
    expect(notice.closest('[role="status"]')).not.toBeNull();
    expect(screen.getByRole("tab", { name: "Summary" })).toBeInTheDocument();
  });

  it("says nothing about archiving for an active client", async () => {
    mockUseApi.mockReturnValue(api());
    renderAt("/clients/c-andes");
    await screen.findByRole("heading", { name: "Andes Logistics", level: 1 });
    expect(screen.queryByText(/This client is archived/)).toBeNull();
  });
});

// Phase 3 final review C (T12 minor): the Unassigned client takes no email
// domains (a typed one is a 422), so its form does not offer the field, and
// sends none.
describe("editing a client's details", () => {
  it("offers email domains for a customer", async () => {
    scope.clients = [meClient(ANDES)];
    mockUseApi.mockReturnValue(api());
    const user = userEvent.setup();
    renderAt("/clients/c-andes");
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("Email domains")).toBeInTheDocument();
  });

  it("does not offer email domains for the Unassigned client, and sends none", async () => {
    scope.clients = [meClient(UNASSIGNED)];
    const updateClient = vi.fn().mockResolvedValue(UNASSIGNED);
    mockUseApi.mockReturnValue(api({ getClientRecord: vi.fn().mockResolvedValue(UNASSIGNED), updateClient }));
    const user = userEvent.setup();
    renderAt("/clients/c-unassigned");
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("Email domains")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(updateClient).toHaveBeenCalledTimes(1));
    expect(updateClient.mock.calls[0]?.[1]).not.toHaveProperty("email_domains");
  });
});

describe("a client's Sites", () => {
  it("lists the sites, adds one, and explains a refused archive", async () => {
    const createClientSite = vi.fn().mockResolvedValue({ ...NORTH, id: "s-new", name: "Sede sur" });
    const archiveSite = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "this site has 3 devices; a site with devices cannot be archived" })),
    );
    const listClientSites = vi.fn().mockResolvedValue([MAIN, NORTH, GONE]);
    mockUseApi.mockReturnValue(api({ createClientSite, archiveSite, listClientSites }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=sites");

    const main = (await screen.findByText("Principal")).closest("tr") as HTMLElement;
    expect(listClientSites).toHaveBeenCalledWith("c-andes", { include_archived: true });
    expect(within(main).getByText("In use")).toBeInTheDocument();
    const gone = screen.getByText("Old office").closest("tr") as HTMLElement;
    expect(within(gone).getByText(/^Archived /)).toBeInTheDocument();
    expect(within(gone).queryByRole("button", { name: "Archive Old office" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Add site" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a site" });
    await user.type(within(dialog).getByLabelText("Name"), "Sede sur");
    await user.type(within(dialog).getByLabelText("Address"), "Cra 7 #12-34");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(createClientSite).toHaveBeenCalledWith("c-andes", {
        name: "Sede sur",
        address: "Cra 7 #12-34",
        description: "",
      }),
    );

    await user.click(within(main).getByRole("button", { name: "Archive Principal" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Archive this site" })).getByRole("button", {
        name: "Archive",
      }),
    );
    expect(archiveSite).toHaveBeenCalledWith("s-main");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The site was not archived. this site has 3 devices; a site with devices cannot be archived",
    );
  });

  it("opens a site's install link for a person who manages install links on the client", async () => {
    const getInstallLink = vi.fn().mockResolvedValue(LINK);
    mockUseApi.mockReturnValue(api({ getInstallLink }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=sites");
    await user.click(await screen.findByRole("button", { name: "Install link of Principal" }));
    await waitFor(() => expect(getInstallLink).toHaveBeenCalledWith("s-main"));
  });

  it("offers no install link and no edits without the codes on the client", async () => {
    scope.clients = [meClient(ANDES, ["clients.read"])];
    mockUseApi.mockReturnValue(api());
    renderAt("/clients/c-andes?tab=sites");
    await screen.findByText("Principal");
    expect(screen.queryByRole("button", { name: "Install link of Principal" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add site" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Archive Principal" })).toBeNull();
  });
});

describe("a client's Contacts", () => {
  it("adds a contact and turns one off", async () => {
    const createContact = vi.fn().mockResolvedValue({ ...ANA, id: "ct-new", email: "luis@andes.example" });
    const updateContact = vi.fn().mockResolvedValue({ ...ANA, status: "disabled" });
    mockUseApi.mockReturnValue(api({ createContact, updateContact }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=contacts");

    const ana = (await screen.findByText("Ana Rojas")).closest("tr") as HTMLElement;
    await user.click(screen.getByRole("button", { name: "Add contact" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a contact" });
    await user.type(within(dialog).getByLabelText("Email"), "luis@andes.example");
    await user.type(within(dialog).getByLabelText("Name"), "Luis Pardo");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(createContact).toHaveBeenCalledWith("c-andes", {
        email: "luis@andes.example",
        display_name: "Luis Pardo",
        phone: null,
      }),
    );

    await user.click(within(ana).getByRole("button", { name: "Turn off Ana Rojas" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Turn off this contact" })).getByRole("button", {
        name: "Turn off",
      }),
    );
    expect(updateContact).toHaveBeenCalledWith("ct-ana", { status: "disabled" });
  });
});

describe("a client's Modules", () => {
  it("switches an optional module through enabled_modules", async () => {
    const updateClient = vi.fn().mockResolvedValue({ ...ANDES, enabled_modules: ["budget", "change"] });
    mockUseApi.mockReturnValue(api({ updateClient }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=modules");
    const changes = await screen.findByRole("switch", { name: "Changes" });
    expect(changes).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("switch", { name: "IT budget" })).toHaveAttribute("aria-checked", "true");
    await user.click(changes);
    await waitFor(() =>
      expect(updateClient).toHaveBeenCalledWith("c-andes", { enabled_modules: ["budget", "change"] }),
    );
  });

  it("builds a quick second switch on the first one's saved answer, not on the record before it", async () => {
    // The record is asked again after a save, and here that answer is slow:
    // the switches come back as soon as the save is answered, and the next
    // one must start from what the server saved, not from the old record.
    let server: ClientOut = ANDES;
    const saves: Array<() => void> = [];
    const refetches: Array<() => void> = [];
    const getClientRecord = vi
      .fn()
      .mockResolvedValueOnce(ANDES)
      .mockImplementation(() => {
        const answer = server;
        return new Promise<ClientOut>((resolve) => refetches.push(() => resolve(answer)));
      });
    const updateClient = vi.fn(
      (_clientId: string, body: ClientUpdate) =>
        new Promise<ClientOut>((resolve) =>
          saves.push(() => {
            server = { ...server, enabled_modules: body.enabled_modules ?? server.enabled_modules };
            resolve(server);
          }),
        ),
    );
    mockUseApi.mockReturnValue(api({ getClientRecord, updateClient }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=modules");

    await user.click(await screen.findByRole("switch", { name: "Changes" }));
    const continuity = screen.getByRole("switch", { name: "Continuity" });
    await waitFor(() => expect(continuity).toBeDisabled());
    await act(async () => saves[0]?.());
    await waitFor(() => expect(continuity).toBeEnabled());
    // The first switch shows what was saved, while the record is asked again.
    expect.soft(screen.getByRole("switch", { name: "Changes" })).toHaveAttribute("aria-checked", "true");
    await user.click(continuity);
    await waitFor(() => expect(updateClient).toHaveBeenCalledTimes(2));
    expect(updateClient).toHaveBeenNthCalledWith(1, "c-andes", { enabled_modules: ["budget", "change"] });
    expect(updateClient).toHaveBeenNthCalledWith(2, "c-andes", { enabled_modules: ["budget", "bcp", "change"] });
    // The second save answered, then the slow reads, the older last: the
    // page ends on the last saved answer, never on an older read.
    await act(async () => saves[1]?.());
    await act(async () => refetches.reverse().forEach((answer) => answer()));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Continuity" })).toBeEnabled());
    expect(screen.getByRole("switch", { name: "Changes" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("switch", { name: "Continuity" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("switch", { name: "IT budget" })).toHaveAttribute("aria-checked", "true");
  });

  it("offers no switch to a person without clients.manage on the client", async () => {
    scope.clients = [meClient(ANDES, ["clients.read"])];
    mockUseApi.mockReturnValue(api());
    renderAt("/clients/c-andes?tab=modules");
    expect(await screen.findByRole("switch", { name: "Changes" })).toBeDisabled();
  });
});

const LUIS: ContactOut = {
  ...ANA,
  id: "ct-luis",
  email: "luis@andes.example",
  display_name: "Luis Pardo",
  access: "none",
};
const MARTA: ContactOut = {
  ...ANA,
  id: "ct-marta",
  email: "marta@andes.example",
  display_name: "Marta Gil",
  access: "invited",
  invite_expires_at: "2026-11-09T12:00:00Z",
};
const STEP_UP = new ApiError(403, JSON.stringify({ detail: { code: "step_up_required" } }));

/** What TanStack still holds of every mutation: its answers and its bodies. */
function mutationCacheText(queryClient: QueryClient): string {
  return JSON.stringify(
    queryClient
      .getMutationCache()
      .getAll()
      .map((mutation) => ({ data: mutation.state.data, variables: mutation.state.variables })),
  );
}

describe("a client's contacts' portal access", () => {
  it("says each contact's access and offers the act that fits it", async () => {
    mockUseApi.mockReturnValue(api({ listContacts: vi.fn().mockResolvedValue([ANA, LUIS, MARTA]) }));
    renderAt("/clients/c-andes?tab=contacts");
    const ana = (await screen.findByText("Ana Rojas")).closest("tr") as HTMLElement;
    const luis = screen.getByText("Luis Pardo").closest("tr") as HTMLElement;
    const marta = screen.getByText("Marta Gil").closest("tr") as HTMLElement;
    expect(within(ana).getByText("Can sign in")).toBeInTheDocument();
    expect(within(ana).getByRole("button", { name: "Reset portal access for Ana Rojas" })).toBeInTheDocument();
    expect(within(luis).getByText("No access")).toBeInTheDocument();
    expect(within(luis).getByRole("button", { name: "Invite Luis Pardo to the portal" })).toBeInTheDocument();
    expect(within(marta).getByText(/^Invited/)).toBeInTheDocument();
    expect(within(marta).getByRole("button", { name: "Send Marta Gil a new invitation" })).toBeInTheDocument();
    expect(within(marta).getByRole("button", { name: "Cancel Marta Gil's invitation" })).toBeInTheDocument();
  });

  it("invites by email and says so", async () => {
    const inviteContact = vi.fn().mockResolvedValue({ contact: { ...LUIS, access: "invited" }, invite_link: null });
    mockUseApi.mockReturnValue(api({ listContacts: vi.fn().mockResolvedValue([LUIS]), inviteContact }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=contacts");
    await user.click(await screen.findByRole("button", { name: "Invite Luis Pardo to the portal" }));
    expect(inviteContact).toHaveBeenCalledExactlyOnceWith("c-andes", "ct-luis");
    expect(await screen.findByText("Invitation sent to luis@andes.example.")).toBeInTheDocument();
  });

  it("without email, shows the link once and forgets it when done", async () => {
    const link = "https://itms.example/invite#tok-contact-1";
    const inviteContact = vi.fn().mockResolvedValue({ contact: { ...LUIS, access: "invited" }, invite_link: link });
    mockUseApi.mockReturnValue(api({ listContacts: vi.fn().mockResolvedValue([LUIS]), inviteContact }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=contacts");
    await user.click(await screen.findByRole("button", { name: "Invite Luis Pardo to the portal" }));
    const shown = await screen.findByRole("region", { name: "Portal invitation for luis@andes.example" });
    expect(within(shown).getByLabelText("Invitation link")).toHaveValue(link);
    await user.click(within(shown).getByRole("button", { name: "Done, I have passed it on" }));
    expect(screen.queryByDisplayValue(link)).not.toBeInTheDocument();
  });

  it("keeps a shown link in no mutation, while it is on screen or after (A2-69)", async () => {
    const link = "https://itms.example/invite#tok-contact-2";
    const inviteContact = vi.fn().mockResolvedValue({ contact: { ...LUIS, access: "invited" }, invite_link: link });
    mockUseApi.mockReturnValue(api({ listContacts: vi.fn().mockResolvedValue([LUIS]), inviteContact }));
    const user = userEvent.setup();
    const { queryClient } = renderAt("/clients/c-andes?tab=contacts");
    await user.click(await screen.findByRole("button", { name: "Invite Luis Pardo to the portal" }));
    await screen.findByRole("region", { name: "Portal invitation for luis@andes.example" });
    await waitFor(() => expect(mutationCacheText(queryClient)).not.toContain("tok-contact-2"));
  });

  it("cancels a pending invitation", async () => {
    const cancelContactInvite = vi.fn().mockResolvedValue(undefined);
    mockUseApi.mockReturnValue(api({ listContacts: vi.fn().mockResolvedValue([MARTA]), cancelContactInvite }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=contacts");
    await user.click(await screen.findByRole("button", { name: "Cancel Marta Gil's invitation" }));
    expect(cancelContactInvite).toHaveBeenCalledExactlyOnceWith("c-andes", "ct-marta");
  });

  it("resets access after a confirmation, asking for the technician's second factor when the server does", async () => {
    const resetContactAccess = vi
      .fn()
      .mockRejectedValueOnce(STEP_UP)
      .mockResolvedValue({ contact: { ...ANA, access: "invited" }, invite_link: null });
    const stepUpTotp = vi.fn().mockResolvedValue(undefined);
    mockUseApi.mockReturnValue(api({ resetContactAccess, stepUpTotp }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=contacts");
    await user.click(await screen.findByRole("button", { name: "Reset portal access for Ana Rojas" }));
    const confirm = await screen.findByRole("dialog", { name: "Reset this contact's portal access" });
    await user.click(within(confirm).getByRole("button", { name: "Reset access" }));
    const stepUp = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(stepUp).getByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(stepUp).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(resetContactAccess).toHaveBeenCalledTimes(2));
    expect(stepUpTotp).toHaveBeenCalledWith("123456");
    expect(resetContactAccess).toHaveBeenLastCalledWith("c-andes", "ct-ana");
  });

  it("keeps a reset's new link in no mutation (A2-69)", async () => {
    const link = "https://itms.example/invite#tok-reset-ana";
    const resetContactAccess = vi.fn().mockResolvedValue({ contact: { ...ANA, access: "invited" }, invite_link: link });
    mockUseApi.mockReturnValue(api({ resetContactAccess }));
    const user = userEvent.setup();
    const { queryClient } = renderAt("/clients/c-andes?tab=contacts");
    await user.click(await screen.findByRole("button", { name: "Reset portal access for Ana Rojas" }));
    const confirm = await screen.findByRole("dialog", { name: "Reset this contact's portal access" });
    await user.click(within(confirm).getByRole("button", { name: "Reset access" }));
    await screen.findByRole("region", { name: "Portal invitation for ana@andes.example" });
    await waitFor(() => expect(mutationCacheText(queryClient)).not.toContain("tok-reset-ana"));
  });

  it("offers nothing to a contact that is turned off", async () => {
    mockUseApi.mockReturnValue(api({ listContacts: vi.fn().mockResolvedValue([{ ...LUIS, status: "disabled" }]) }));
    renderAt("/clients/c-andes?tab=contacts");
    const luis = (await screen.findByText("Luis Pardo")).closest("tr") as HTMLElement;
    expect(within(luis).queryByRole("button", { name: /Invite/ })).not.toBeInTheDocument();
  });
});
