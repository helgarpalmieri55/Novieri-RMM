import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentRelease, ApiClient, ClientSiteOut, MeClient } from "../../lib/api";
import { AgentReleasesPage } from "./AgentReleasesPage";

// MSP clients phase 3, design §3.2: the install command, the install link and
// the managed deployment are one SITE's, chosen on the page among the sites of
// the client chosen at the top, or of the page's Client field with every client.

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = ["enrollment.manage", "enrollment.link.manage"];
const INTERNAL: MeClient = { id: "o", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

function site(id: string, clientId: string, name: string): ClientSiteOut {
  return { id, client_id: clientId, name, address: "", description: "", egress_networks: [], archived_at: null, device_count: 0 };
}

// The shape of `AgentReleasesPage.test.tsx:43-57`'s `release()`.
const RELEASE: AgentRelease = {
  id: "11111111-1111-1111-1111-111111111111",
  version: "873d666",
  os: "linux",
  arch: "amd64",
  size_bytes: 18_874_368,
  sha256: "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
  signed: false,
  notes: "",
  published_at: "2026-08-18T09:00:00Z",
  has_artifact: true,
};

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  const api = {
    me: () => Promise.resolve({ user_id: "u", org_id: "o", email: "a@b.c", display_name: "A", permissions: CODES, timezone: null, clients: [INTERNAL, ACME] }),
    listAgentReleases: () => Promise.resolve([RELEASE]),
    listClientSites: vi.fn((clientId: string) =>
      Promise.resolve(
        clientId === "c-acme" ? [site("site-a1", "c-acme", "Acme HQ"), site("site-a2", "c-acme", "Acme Lab")] : [site("site-o", "o", "Principal")],
      ),
    ),
    generateAgentInstallCommand: vi.fn().mockResolvedValue({ command: "curl …", expires_at: "2026-10-31T12:00:00Z" }),
    getInstallLink: vi.fn().mockResolvedValue({
      enabled: false, has_code: false, networks: [], disable_at: null, hourly_limit: 50, enabled_at: null, joined_since_enabled: 0,
    }),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AgentReleasesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o"));

describe("where new machines go", () => {
  it("uses the only site of the client chosen at the top, for the command and the link", async () => {
    const api = stubApi();
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.getInstallLink).toHaveBeenCalledWith("site-o"));
    expect(screen.queryByLabelText("Client")).toBeNull();

    await user.click(await screen.findByRole("button", { name: /install command/i }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Generate command" }));
    await waitFor(() => expect(api.generateAgentInstallCommand).toHaveBeenCalledWith(RELEASE.id, 4, "site-o"));
  });

  it("uses the only site of a client chosen at the top that is not the organisation's own", async () => {
    // `org_id` is "o": the phase 1-2 rule took the organisation's own client
    // whoever was chosen, so only a client other than "o" tells the two rules
    // apart (phase 3 final review D, Important 1).
    selectClient("c-acme");
    const api = stubApi({
      listClientSites: vi.fn((clientId: string) =>
        Promise.resolve(clientId === "c-acme" ? [site("site-a1", "c-acme", "Acme HQ")] : [site("site-o", "o", "Principal")]),
      ),
    });
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(api.getInstallLink).toHaveBeenCalledWith("site-a1"));
    expect(api.getInstallLink).not.toHaveBeenCalledWith("site-o");
    expect(screen.queryByLabelText("Client")).toBeNull();

    await user.click(await screen.findByRole("button", { name: /install command/i }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Generate command" }));
    await waitFor(() => expect(api.generateAgentInstallCommand).toHaveBeenCalledExactlyOnceWith(RELEASE.id, 4, "site-a1"));
  });

  it("with every client chosen, asks for the client, then the site, and mints for that site", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderPage();
    await user.selectOptions(await screen.findByLabelText("Client"), "c-acme");
    const picker = await screen.findByLabelText("Site");
    await within(picker).findByRole("option", { name: "Acme Lab" });
    expect(within(picker).queryByRole("option", { name: "Principal" })).toBeNull();
    await user.selectOptions(picker, "site-a2");
    await waitFor(() => expect(api.getInstallLink).toHaveBeenCalledWith("site-a2"));

    await user.click(screen.getByRole("button", { name: /install command/i }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Generate command" }));
    await waitFor(() => expect(api.generateAgentInstallCommand).toHaveBeenCalledWith(RELEASE.id, 4, "site-a2"));
  });

  it("mints nothing while no site is chosen", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderPage();
    await screen.findByLabelText("Client");
    await user.click(await screen.findByRole("button", { name: /install command/i }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Generate command" }));
    expect(api.generateAgentInstallCommand).not.toHaveBeenCalled();
    expect(api.getInstallLink).not.toHaveBeenCalled();
  });
});

// MSP clients phase 3, design §4.4: the add-a-computer journey links here with
// `?client=…&site=…`. Its own history entry each, so every render is an arrival
// (`onboarding/journeyTarget.ts` decides once per entry).
describe("opened from the add-a-computer journey", () => {
  function renderAt(path: string) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/", path]} initialIndex={1}>
          <AgentReleasesPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("starts on the journey's site, for the link and the command", async () => {
    selectClient("c-acme");
    const api = stubApi();
    const user = userEvent.setup();
    renderAt("/agent-packages?client=c-acme&site=site-a2");
    await waitFor(() => expect(api.getInstallLink).toHaveBeenCalledWith("site-a2"));
    await waitFor(() => expect((screen.getByLabelText("Site") as HTMLSelectElement).value).toBe("site-a2"));
    await user.click(screen.getByRole("button", { name: /install command/i }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Generate command" }));
    await waitFor(() => expect(api.generateAgentInstallCommand).toHaveBeenCalledWith(RELEASE.id, 4, "site-a2"));
  });

  it("with every client chosen, makes the console work on the journey's client", async () => {
    const select = vi.fn();
    mockClientScope.mockReturnValue({ selected: null, select, clients: [INTERNAL, ACME], ready: true });
    stubApi();
    renderAt("/agent-packages?client=c-acme&site=site-a2");
    await waitFor(() => expect(select).toHaveBeenCalledWith("c-acme"));
    expect(select).toHaveBeenCalledTimes(1);
  });

  it("ignores the journey's site under another client chosen at the top", async () => {
    const api = stubApi();
    renderAt("/agent-packages?client=c-acme&site=site-a2");
    // The internal client's only site, never Acme's.
    await waitFor(() => expect(api.getInstallLink).toHaveBeenCalledWith("site-o"));
    expect(api.getInstallLink).not.toHaveBeenCalledWith("site-a2");
  });
});

// Phase 3 final review C, Important 7: with a client chosen and more than one
// site, "Install command" -> confirm said "...the client this is for has not
// been chosen". The site is what is missing, and the page says so; with every
// client and no client picked it names the Client field; with Unassigned
// chosen at the top it says nothing new can be filed there.
describe("an install command or a deployment token with nothing to name its site", () => {
  const MANAGED_PACKAGE = {
    release: RELEASE, channel: "gpo", download_url: "https://minio.test/x", download_expires_in_s: 900,
    sha256: RELEASE.sha256, staging: [], install_command: "install", enrol_command: "enrol",
    integrity: { sha256: RELEASE.sha256, verify_command: "verify", verified_at_install: false, notes: [] },
  };

  async function askForCommand(user: ReturnType<typeof userEvent.setup>): Promise<void> {
    await user.click(await screen.findByRole("button", { name: /install command/i }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Generate command" }));
  }

  it("names the site, not the client, when the chosen client has more than one", async () => {
    selectClient("c-acme");
    const api = stubApi();
    const user = userEvent.setup();
    renderPage();
    await within(await screen.findByLabelText("Site")).findByRole("option", { name: "Acme Lab" });
    await askForCommand(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Site is required");
    expect(api.generateAgentInstallCommand).not.toHaveBeenCalled();
  });

  it("names the site for a deployment token too", async () => {
    selectClient("c-acme");
    const api = stubApi({
      getAgentDeploymentPackage: vi.fn().mockResolvedValue(MANAGED_PACKAGE),
      mintAgentDeploymentToken: vi.fn(),
    });
    const user = userEvent.setup();
    renderPage();
    await within(await screen.findByLabelText("Site")).findByRole("option", { name: "Acme Lab" });
    await user.click(screen.getByRole("button", { name: /managed deployment/i }));
    await user.click(await screen.findByRole("button", { name: /create a deployment token/i }));
    const confirm = await screen.findByRole("dialog", { name: "Create a deployment token" });
    await user.click(within(confirm).getByRole("button", { name: "Create token" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Site is required");
    expect(api.mintAgentDeploymentToken).not.toHaveBeenCalled();
  });

  it("names the Client field with every client chosen and none picked", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderPage();
    await screen.findByLabelText("Client");
    await askForCommand(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Client is required");
    expect(api.generateAgentInstallCommand).not.toHaveBeenCalled();
  });

  it("with Unassigned chosen at the top, says nothing new can be filed there", async () => {
    const UNASSIGNED: MeClient = { id: "c-unassigned", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: CODES };
    mockClientScope.mockReturnValue({ selected: UNASSIGNED.id, select: vi.fn(), clients: [INTERNAL, ACME, UNASSIGNED], ready: true });
    const api = stubApi();
    const user = userEvent.setup();
    renderPage();
    const refused = "Unassigned is the client chosen, and nothing new can be filed under it. Choose another client first.";
    expect(await screen.findByText(refused)).toBeInTheDocument();
    expect(screen.queryByLabelText("Site")).toBeNull();
    await askForCommand(user);
    expect(await screen.findByRole("alert")).toHaveTextContent(refused);
    expect(api.generateAgentInstallCommand).not.toHaveBeenCalled();
    expect(api.listClientSites).not.toHaveBeenCalled();
  });
});
