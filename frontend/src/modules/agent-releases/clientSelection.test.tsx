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
