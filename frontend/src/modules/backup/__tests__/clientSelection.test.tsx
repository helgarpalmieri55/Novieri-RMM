import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, MeClient } from "../../../lib/api";
import { BackupPolicyEditor } from "../BackupPolicyEditor";
import { RepositoriesPage } from "../RepositoriesPage";
import type { Repository } from "../types";

// MSP clients phase 3, design §3.1, §3.3, §3.4.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const CODES = ["backup.read", "backup.run", "backup.restore", "backup.policy.manage"];
const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

// Phase 3 final review D, Important 2: Beta's people may read, run and restore
// its backups but not manage where they go; the Unassigned client is never offered.
const BETA: MeClient = { id: "c-beta", name: "Beta", kind: "customer", enabled_modules: [], permissions: ["backup.read", "backup.run", "backup.restore"] };
const UNASSIGNED: MeClient = { id: "c-unassigned", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: CODES };

/** Every client chosen, among clients one of which only reads. */
function selectEveryClientWithAReader(): void {
  mockClientScope.mockReturnValue({ selected: null, select: vi.fn(), clients: [INTERNAL, ACME, BETA, UNASSIGNED], ready: true });
}

function offered(field: HTMLElement): (string | null)[] {
  return within(field).getAllByRole("option").map((option) => option.textContent);
}

function repository(overrides: Partial<Repository>): Repository {
  return { id: "r-o", client_id: "o1", name: "Own vault", storage_ref: "s3://own", state: "active", dedup_params: {}, created_at: "2026-08-01T00:00:00Z", ...overrides };
}

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: CODES, clients: [INTERNAL, ACME] }),
    listBackupRepositories: vi.fn().mockResolvedValue([repository({}), repository({ id: "r-a", client_id: "c-acme", name: "Acme vault" })]),
    listBackupPolicies: vi.fn().mockResolvedValue([]),
    listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 0, ungrouped_device_count: 0, evaluation: null }),
    createBackupRepository: vi.fn().mockResolvedValue(repository({})),
    upsertBackupPolicy: vi.fn().mockResolvedValue({}),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderIt(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/backup"]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => selectClient("o1"));

describe("a new repository", () => {
  it("files it under the client chosen at the top, which is not the organisation's own", async () => {
    // `org_id` is "o1": the phase 1-2 rule filed every new row there whoever
    // was chosen, so only a client other than "o1" tells the two rules apart
    // (phase 3 final review D, Important 1).
    selectClient("c-acme");
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<RepositoriesPage />);
    await user.click(await screen.findByRole("button", { name: /new repository/i }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("Client")).toBeNull();
    await user.type(within(dialog).getByLabelText("Name"), "Acme offsite");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(api.createBackupRepository).toHaveBeenCalledTimes(1));
    expect((api.createBackupRepository as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]).toEqual({ client_id: "c-acme", name: "Acme offsite" });
  });

  it("with every client chosen, asks for the client and files the repository there", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<RepositoriesPage />);
    await user.click(await screen.findByRole("button", { name: /new repository/i }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "Acme offsite");
    await user.selectOptions(within(dialog).getByLabelText("Client"), "c-acme");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(api.createBackupRepository).toHaveBeenCalled());
    expect((api.createBackupRepository as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]).toMatchObject({ client_id: "c-acme", name: "Acme offsite" });
  });

  it("with every client chosen, offers only the clients holding backup.policy.manage, never Unassigned", async () => {
    selectEveryClientWithAReader();
    stubApi();
    const user = userEvent.setup();
    renderIt(<RepositoriesPage />);
    await user.click(await screen.findByRole("button", { name: /new repository/i }));
    const dialog = await screen.findByRole("dialog");
    expect(offered(within(dialog).getByLabelText("Client"))).toEqual(["Choose a client", "Novieri", "Acme"]);
  });

  it("shows whose each repository is with every client chosen", async () => {
    selectClient(null);
    stubApi();
    renderIt(<RepositoriesPage />);
    const row = (await screen.findByText("Acme vault")).closest("tr") as HTMLElement;
    expect(screen.getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(row).getByText("Acme")).toBeInTheDocument();
  });
});

describe("a new client-default policy", () => {
  it("files it under the client chosen at the top, and offers only that client's repositories", async () => {
    // Not the organisation's own ("o1"): the old rule's answer whatever the top said.
    selectClient("c-acme");
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<BackupPolicyEditor />);
    await user.click(await screen.findByRole("button", { name: /new policy/i }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("Client")).toBeNull();
    await user.click(within(dialog).getByRole("combobox", { name: /Repository/ }));
    await user.click(await screen.findByRole("option", { name: /Acme vault/ }));
    expect(screen.queryByRole("option", { name: /Own vault/ })).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.upsertBackupPolicy).toHaveBeenCalledTimes(1));
    expect((api.upsertBackupPolicy as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]).toMatchObject({ device_group_id: null, client_id: "c-acme", repository_id: "r-a" });
  });

  it("with every client chosen, asks for the client and offers only its repositories", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<BackupPolicyEditor />);
    await user.click(await screen.findByRole("button", { name: /new policy/i }));
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Client"), "c-acme");
    await user.click(within(dialog).getByRole("combobox", { name: /Repository/ }));
    await user.click(await screen.findByRole("option", { name: /Acme vault/ }));
    expect(screen.queryByRole("option", { name: /Own vault/ })).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.upsertBackupPolicy).toHaveBeenCalled());
    expect((api.upsertBackupPolicy as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]).toMatchObject({ device_group_id: null, client_id: "c-acme", repository_id: "r-a" });
  });

  it("with every client chosen, offers for a client default only the clients holding backup.policy.manage", async () => {
    selectEveryClientWithAReader();
    stubApi();
    const user = userEvent.setup();
    renderIt(<BackupPolicyEditor />);
    await user.click(await screen.findByRole("button", { name: /new policy/i }));
    const dialog = await screen.findByRole("dialog");
    expect(offered(within(dialog).getByLabelText("Client"))).toEqual(["Choose a client", "Novieri", "Acme"]);
  });

  it("with every client chosen and none picked yet, neither says the client has no repository nor links to make one", async () => {
    // No client is no list of repositories -- not a client without one. The
    // dialog said "No backup repository of this policy's client exists" and
    // linked to the Repositories page before anyone had named the client.
    selectClient(null);
    stubApi();
    const user = userEvent.setup();
    renderIt(<BackupPolicyEditor />);
    await user.click(await screen.findByRole("button", { name: /new policy/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: /Repository/ }));
    const list = await within(dialog).findByRole("listbox", { name: /Repository/ });
    // Settled: the repositories have arrived, so the note is not "Loading…".
    await waitFor(() => expect(list).not.toHaveTextContent(/Loading/));

    expect(within(dialog).queryByText(/No backup repository of this policy’s client exists/)).toBeNull();
    expect(within(dialog).queryByRole("link", { name: /Create a backup repository/ })).toBeNull();
    expect(list).toHaveTextContent("Choose the client first: its repositories are listed here.");
  });

  it("with every client chosen, Save without a client names the Client and sends nothing", async () => {
    selectClient(null);
    const api = stubApi();
    const user = userEvent.setup();
    renderIt(<BackupPolicyEditor />);
    await user.click(await screen.findByRole("button", { name: /new policy/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Client is required");
    expect(api.upsertBackupPolicy).not.toHaveBeenCalled();
  });

  it("still links to the Repositories page once a client with no repository is picked", async () => {
    // The gate is on the client being known, not on the link: a client
    // without a repository still says so, and still says where to make one.
    selectClient(null);
    stubApi({ listBackupRepositories: vi.fn().mockResolvedValue([repository({})]) });
    const user = userEvent.setup();
    renderIt(<BackupPolicyEditor />);
    await user.click(await screen.findByRole("button", { name: /new policy/i }));
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Client"), "c-acme");

    expect(await within(dialog).findByRole("link", { name: /Create a backup repository/ })).toHaveAttribute(
      "href",
      "/backup/repositories",
    );
    await user.click(within(dialog).getByRole("combobox", { name: /Repository/ }));
    expect(await within(dialog).findByText(/No backup repository of this policy’s client exists\./)).toBeInTheDocument();
  });
});

// Phase 3 final review B, Important 4 (operator decision 2): with Unassigned
// chosen at the top nothing new can be filed, and the header confines the
// request to Unassigned, so a Save could only fail; the forms say so.
describe("with Unassigned chosen at the top", () => {
  const REFUSED = "Unassigned is the client chosen, and nothing new can be filed under it. Choose another client first.";

  function selectUnassigned(): void {
    mockClientScope.mockReturnValue({ selected: UNASSIGNED.id, select: vi.fn(), clients: [INTERNAL, ACME, UNASSIGNED], ready: true });
  }

  it("a new repository says so, and offers no Create", async () => {
    selectUnassigned();
    stubApi({ listBackupRepositories: vi.fn().mockResolvedValue([]) });
    const user = userEvent.setup();
    renderIt(<RepositoriesPage />);
    await user.click((await screen.findAllByRole("button", { name: /new repository/i }))[0]!);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(REFUSED)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Create" })).toBeNull();
  });

  it("a new client default says so, and offers no Save", async () => {
    selectUnassigned();
    stubApi({ listBackupRepositories: vi.fn().mockResolvedValue([]) });
    const user = userEvent.setup();
    renderIt(<BackupPolicyEditor />);
    await user.click((await screen.findAllByRole("button", { name: /new policy/i }))[0]!);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(REFUSED)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Save" })).toBeNull();
  });
});
