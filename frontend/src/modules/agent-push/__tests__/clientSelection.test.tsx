import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Device, MeClient } from "../../../lib/api";
import { PushAuthorizationsPage } from "../PushAuthorizationsPage";
import { PushInstallFlow } from "../PushInstallFlow";
import { AGENT_PUSH_SCOPE_MANAGE, type PushAuthorizationOut } from "../types";

// MSP clients phase 3, design §3.1, §3.3 and §3.4.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = [AGENT_PUSH_SCOPE_MANAGE, "credentials.read", "agent.push.run"];
const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: CODES, clients: [INTERNAL, ACME] }),
    listPushAuthorizations: vi.fn().mockResolvedValue([]),
    createPushAuthorization: vi.fn().mockResolvedValue({}),
    listPushCredentialScopes: vi.fn().mockResolvedValue([]),
    listCredentials: vi.fn().mockResolvedValue([]),
    listSiteServers: vi.fn().mockResolvedValue([]),
    listAgentReleases: vi.fn().mockResolvedValue([]),
    listPushHostKeys: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderIt(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/agent-push/authorizations"]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o1"));

describe("a push authorization", () => {
  it("with every client chosen, asks for the client and authorizes the prefix in it", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<PushAuthorizationsPage />);
    await user.type(await screen.findByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24");
    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Authorize" }));
    await waitFor(() =>
      expect(api.createPushAuthorization).toHaveBeenCalledExactlyOnceWith({
        client_id: "c-acme", prefix: "10.4.7.0/24", note: "", expires_at: null,
      }),
    );
  });

  it("shows whose each authorization is with every client chosen", async () => {
    selectClient(null);
    const grant = {
      id: "a1", client_id: "c-acme", prefix: "10.9.0.0/24", address_count: 256, note: "", authorized_by: "u1",
      authorized_at: new Date().toISOString(), expires_at: null, revoked_at: null,
    } as unknown as PushAuthorizationOut;
    stubApi({ listPushAuthorizations: vi.fn().mockResolvedValue([grant]) });
    renderIt(<PushAuthorizationsPage />);
    const row = (await screen.findByText("10.9.0.0/24")).closest("tr") as HTMLElement;
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(row).getByText("Acme")).toBeInTheDocument();
  });
});

describe("a push install offers only the machines' client's credentials", () => {
  function machine(id: string, clientId: string): Device {
    return { id, hostname: id, primary_ip: "10.4.7.9", os_family: "linux", architecture: "amd64", client_id: clientId } as unknown as Device;
  }
  const CREDENTIALS = [
    { id: "cr-o", name: "Own key", type: "push_install", device_group_id: null, client_id: "o1", created_at: null, last_used_at: null, has_secret: true },
    { id: "cr-a", name: "Acme key", type: "push_install", device_group_id: null, client_id: "c-acme", created_at: null, last_used_at: null, has_secret: true },
  ];

  it("lists the one client's credentials", async () => {
    stubApi({ listCredentials: vi.fn().mockResolvedValue(CREDENTIALS) });
    renderIt(<PushInstallFlow machines={[machine("m1", "c-acme")]} onRemove={() => {}} onClose={() => {}} />);
    await screen.findByRole("option", { name: "Acme key" });
    expect(screen.queryByRole("option", { name: "Own key" })).toBeNull();
  });

  it("offers none for machines of two clients, and says why", async () => {
    stubApi({ listCredentials: vi.fn().mockResolvedValue(CREDENTIALS) });
    renderIt(<PushInstallFlow machines={[machine("m1", "c-acme"), machine("m2", "o1")]} onRemove={() => {}} onClose={() => {}} />);
    expect(await screen.findByText("These machines belong to more than one client. Push to one client's machines at a time.")).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Acme key" })).toBeNull();
  });
});
