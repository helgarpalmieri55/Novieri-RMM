import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { translate } from "../../i18n/core";
import type { ApiClient, Credential, MeClient } from "../../lib/api";
import { CredentialsPage } from "./CredentialsPage";

// MSP clients phase 3, design §3.1 and §3.4: a credential goes to the client
// chosen at the top, or to the dialog's Client field with every client. A push
// credential always has one; any other type may instead be every client's,
// ticked "For every client" by someone holding credentials.manage on every
// client (phase 3 final review C, Important 2: the server accepts a client for
// any type, so §3.1 applies). The list says whose each credential is.

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
  it("files it under the client chosen at the top, which is not the organisation's own", async () => {
    // `org_id` is "o1": the phase 1-2 rule filed every new row there whoever
    // was chosen, so only a client other than "o1" tells the two rules apart
    // (phase 3 final review D, Important 1).
    selectClient("c-acme");
    const createCredential = vi.fn().mockResolvedValue(credential());
    const user = userEvent.setup();
    renderPage({ listCredentials: () => Promise.resolve([]), createCredential });
    await user.click(await screen.findByRole("button", { name: /new credential/i }));

    await user.selectOptions(screen.getByLabelText("Type"), "push_install");
    expect(screen.queryByLabelText("Client")).toBeNull();
    await user.type(screen.getByLabelText("Name"), "Branch installer");
    await user.type(screen.getByLabelText("Secret"), "s3cret");
    await user.click(screen.getByRole("button", { name: "Store" }));
    await waitFor(() =>
      expect(createCredential).toHaveBeenCalledExactlyOnceWith({
        name: "Branch installer", type: "push_install", client_id: "c-acme", secret: "s3cret",
      }),
    );
  });

  it("with every client chosen, asks for it, and files the credential there", async () => {
    // Before the final review this asserted NO Client field for a non-push
    // type: such a credential was always every client's. It is now §3.1's,
    // like the push types (see "a credential of any other type" below).
    selectClient(null);
    const createCredential = vi.fn().mockResolvedValue(credential());
    const user = userEvent.setup();
    renderPage({ listCredentials: () => Promise.resolve([]), createCredential });
    await user.click(await screen.findByRole("button", { name: /new credential/i }));
    expect(screen.getByLabelText("Client")).toBeInTheDocument();

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

describe("a credential of any other type", () => {
  /** `/me` for someone holding credentials.manage on every client, or on some only. */
  function meWith(everywhere: string[]) {
    return () =>
      Promise.resolve({
        user_id: "u1", email: "a@b.c", display_name: "A", permissions: ["credentials.manage"],
        permissions_everywhere: everywhere, org_id: "o1", clients: [INTERNAL, ACME],
      });
  }

  async function openNew(user: ReturnType<typeof userEvent.setup>): Promise<void> {
    await user.click(await screen.findByRole("button", { name: /new credential/i }));
    await user.type(screen.getByLabelText("Name"), "Domain admin");
    await user.type(screen.getByLabelText("Secret"), "s3cret");
  }

  it("files it under the client chosen at the top", async () => {
    selectClient("c-acme");
    const createCredential = vi.fn().mockResolvedValue(credential());
    const user = userEvent.setup();
    renderPage({ me: meWith([]), listCredentials: () => Promise.resolve([]), createCredential });
    await openNew(user);
    expect(screen.queryByLabelText("Client")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Store" }));
    await waitFor(() =>
      expect(createCredential).toHaveBeenCalledExactlyOnceWith({
        name: "Domain admin", type: "windows_admin", client_id: "c-acme", secret: "s3cret",
      }),
    );
  });

  it("is every client's when ticked so, which names no client and asks for none", async () => {
    selectClient(null);
    const createCredential = vi.fn().mockResolvedValue(credential());
    const user = userEvent.setup();
    renderPage({ me: meWith(["credentials.manage"]), listCredentials: () => Promise.resolve([]), createCredential });
    await openNew(user);
    expect(screen.getByLabelText("Client")).toBeInTheDocument();
    await user.click(await screen.findByRole("checkbox", { name: /For every client/ }));
    expect(screen.queryByLabelText("Client")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Store" }));
    await waitFor(() =>
      expect(createCredential).toHaveBeenCalledExactlyOnceWith({
        name: "Domain admin", type: "windows_admin", secret: "s3cret",
      }),
    );
  });

  it("offers For every client only on every client, and never for a push type", async () => {
    selectClient(null);
    const user = userEvent.setup();
    const { unmount } = renderPage({ me: meWith([]), listCredentials: () => Promise.resolve([]) });
    await user.click(await screen.findByRole("button", { name: /new credential/i }));
    await screen.findByLabelText("Client");
    expect(screen.queryByRole("checkbox", { name: /For every client/ })).toBeNull();
    unmount();

    renderPage({ me: meWith(["credentials.manage"]), listCredentials: () => Promise.resolve([]) });
    await user.click(await screen.findByRole("button", { name: /new credential/i }));
    expect(await screen.findByRole("checkbox", { name: /For every client/ })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Type"), "push_install");
    expect(screen.queryByRole("checkbox", { name: /For every client/ })).toBeNull();
  });

  it("with Unassigned chosen at the top, files nothing for a client, and says so", async () => {
    const UNASSIGNED: MeClient = { id: "c-unassigned", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: ["credentials.manage"] };
    mockClientScope.mockReturnValue({ selected: UNASSIGNED.id, select: vi.fn(), clients: [INTERNAL, ACME, UNASSIGNED], ready: true });
    const createCredential = vi.fn().mockResolvedValue(credential());
    const user = userEvent.setup();
    renderPage({ me: meWith(["credentials.manage"]), listCredentials: () => Promise.resolve([]), createCredential });
    await openNew(user);
    const refused = "Unassigned is the client chosen at the top, and nothing new can be filed under it. Choose another client first.";
    expect(screen.getByText(refused)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Store" })).toBeNull();
    // Every client's is no client's: it may still be stored, and goes global.
    await user.click(screen.getByRole("checkbox", { name: /For every client/ }));
    expect(screen.queryByText(refused)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Store" }));
    await waitFor(() =>
      expect(createCredential).toHaveBeenCalledExactlyOnceWith({
        name: "Domain admin", type: "windows_admin", secret: "s3cret",
      }),
    );
  });
});

describe("a checkout and a delete name the row's client, which decides X-Client-Id", () => {
  it("passes a client's credential's client, and null for every client's", async () => {
    selectClient("c-acme");
    const checkoutCredential = vi.fn().mockResolvedValue({ secret: "x" });
    const deleteCredential = vi.fn().mockResolvedValue({ deleted: true });
    const user = userEvent.setup();
    renderPage({
      me: () =>
        Promise.resolve({
          user_id: "u1", email: "a@b.c", display_name: "A", permissions: ["credentials.manage", "credentials.checkout"],
          org_id: "o1", clients: [INTERNAL, ACME],
        }),
      listCredentials: () =>
        Promise.resolve([
          credential({ id: "cr-acme", name: "Acme admin", type: "windows_admin", client_id: "c-acme" }),
          credential({ id: "cr-every", name: "Shared admin", type: "windows_admin", client_id: null }),
        ]),
      checkoutCredential,
      deleteCredential,
    });
    const acme = (await screen.findByText("Acme admin")).closest("tr") as HTMLElement;
    await user.click(within(acme).getByRole("button", { name: /check out/i }));
    await user.type(screen.getByPlaceholderText(/helpdesk ticket/i), "ticket 1");
    await user.click(screen.getByRole("button", { name: "Reveal" }));
    await waitFor(() => expect(checkoutCredential).toHaveBeenCalledExactlyOnceWith("cr-acme", "ticket 1", "c-acme"));

    await user.click(await screen.findByRole("button", { name: /done/i }));
    const shared = screen.getByText("Shared admin").closest("tr") as HTMLElement;
    await user.click(within(shared).getByRole("button", { name: /^More actions for / }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete stored credential" });
    await user.click(within(confirm).getByRole("button", { name: "Delete credential" }));
    await waitFor(() => expect(deleteCredential).toHaveBeenCalledExactlyOnceWith("cr-every", null));
  });
});
