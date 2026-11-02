import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { translate } from "../../i18n/core";
import type { ApiClient, Credential, MeClient } from "../../lib/api";
import { CredentialsPage } from "./CredentialsPage";

// MSP clients phase 3, design §3.1 and §3.4: a push credential goes to the
// client chosen at the top, or to the dialog's Client field with every client;
// any other type serves every client and asks nothing; the list says whose
// each credential is.

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: ["credentials.manage"] };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ["credentials.manage"] };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

function credential(overrides: Partial<Credential> = {}): Credential {
  return {
    id: "11111111-1111-1111-1111-111111111111", name: "Branch installer", type: "push_install",
    device_group_id: null, client_id: "c-acme", created_at: "2026-08-01T00:00:00Z", last_used_at: null,
    has_secret: true, ...overrides,
  };
}

function renderPage(api: Partial<ApiClient>) {
  mockUseApi.mockReturnValue({
    me: () => Promise.resolve({ permissions: ["credentials.manage"], org_id: "o1", clients: [INTERNAL, ACME] }),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CredentialsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o1"));

describe("a push credential's client", () => {
  it("with every client chosen, asks for it for a push type only, and files the credential there", async () => {
    selectClient(null);
    const createCredential = vi.fn().mockResolvedValue(credential());
    const user = userEvent.setup();
    renderPage({ listCredentials: () => Promise.resolve([]), createCredential });
    await user.click(await screen.findByRole("button", { name: /new credential/i }));
    expect(screen.queryByLabelText("Client")).toBeNull();

    await user.selectOptions(screen.getByLabelText("Type"), "push_install");
    await user.type(screen.getByLabelText("Name"), "Branch installer");
    await user.type(screen.getByLabelText("Secret"), "s3cret");
    await user.click(screen.getByRole("button", { name: "Store" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Client is required");
    expect(createCredential).not.toHaveBeenCalled();

    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Store" }));
    await waitFor(() =>
      expect(createCredential).toHaveBeenCalledExactlyOnceWith({
        name: "Branch installer", type: "push_install", client_id: "c-acme", secret: "s3cret",
      }),
    );
  });

  it("shows whose each credential is with every client chosen, and every client's for a shared one", async () => {
    selectClient(null);
    renderPage({
      listCredentials: () =>
        Promise.resolve([credential(), credential({ id: "22222222-2222-2222-2222-222222222222", name: "Domain admin", type: "windows_admin", client_id: null })]),
    });
    const row = (await screen.findByText("Branch installer")).closest("tr") as HTMLElement;
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(row).getByText("Acme")).toBeInTheDocument();
    const shared = screen.getByText("Domain admin").closest("tr") as HTMLElement;
    expect(within(shared).getByText(translate("lib.clients.everyClient"))).toBeInTheDocument();
  });
});
