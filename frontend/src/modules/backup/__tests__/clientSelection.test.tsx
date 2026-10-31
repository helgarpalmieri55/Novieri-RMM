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
});
