// frontend/src/modules/backup/__tests__/retire.test.tsx
/**
 * Retiring a backup repository (MSP clients phase 3, design §5.5): offered on
 * an active repository of a client where the person manages backup policies;
 * a refusal (409: an active policy names it) is explained; the retired ones
 * have their own tile; and a policy can no longer be pointed at one.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient } from "../../../fixtures/client";
import { ApiClient, ApiError } from "../../../lib/api";
import type { Me } from "../../../lib/api";
import { repositoriesOfClient, repositoryPickerOptions } from "../pickerOptions";
import { RepositoriesPage } from "../RepositoriesPage";
import type { Repository } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientScope")>()),
  useClientScope: () => ({ selected: null, select: vi.fn(), clients: [], ready: true }),
}));

const ACTIVE: Repository = {
  id: "repo-1",
  client_id: "c-andes",
  name: "primary-vault",
  storage_ref: "s3://andes/primary",
  state: "active",
  dedup_params: {},
  created_at: "2026-01-01T00:00:00Z",
};
const RETIRED: Repository = { ...ACTIVE, id: "repo-2", name: "old-vault", state: "retired" };
const THEIRS: Repository = { ...ACTIVE, id: "repo-3", client_id: "c-other", name: "their-vault" };

const CODES = ["backup.read", "backup.policy.manage"];
const ME: Me = {
  user_id: "u1",
  org_id: "org-1",
  email: "op@example.com",
  display_name: "Operator",
  permissions: CODES,
  permissions_everywhere: [],
  clients: [{ id: "c-andes", name: "Andes", kind: "customer", enabled_modules: [], permissions: CODES }],
};

function renderPage(api: Partial<ApiClient>, entry = "/backup/repositories") {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue(ME),
    listBackupRepositories: vi.fn().mockResolvedValue([ACTIVE, RETIRED, THEIRS]),
    listRecoveryKeys: vi.fn().mockResolvedValue([]),
    listOffsiteTargets: vi.fn().mockResolvedValue([]),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <RepositoriesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const rowOf = (name: string) => screen.getByText(name).closest("tr") as HTMLElement;

beforeEach(() => mockUseApi.mockReset());
afterEach(() => vi.unstubAllGlobals());

describe("retiring a repository", () => {
  it("is offered on an active repository of a client where the person manages policies", async () => {
    const retireBackupRepository = vi.fn().mockResolvedValue({ ...ACTIVE, state: "retired" });
    renderPage({ retireBackupRepository });
    const user = userEvent.setup();
    await screen.findByText("primary-vault");
    expect(within(rowOf("old-vault")).getByText("Retired")).toBeInTheDocument();
    expect(within(rowOf("old-vault")).queryByRole("button", { name: /^Retire/ })).toBeNull();
    expect(within(rowOf("their-vault")).queryByRole("button", { name: /^Retire/ })).toBeNull();
    await user.click(within(rowOf("primary-vault")).getByRole("button", { name: "Retire primary-vault" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Retire this repository" })).getByRole("button", {
        name: "Retire",
      }),
    );
    expect(retireBackupRepository).toHaveBeenCalledWith("repo-1");
  });

  it("explains a refusal while an active policy names it", async () => {
    const retireBackupRepository = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "an active backup policy still names this repository" })),
    );
    renderPage({ retireBackupRepository });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Retire primary-vault" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Retire this repository" })).getByRole("button", {
        name: "Retire",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The repository was not retired. an active backup policy still names this repository",
    );
  });

  it("lists the retired repositories behind their own tile", async () => {
    renderPage({}, "/backup/repositories?state=retired");
    expect(await screen.findByText("old-vault")).toBeInTheDocument();
    expect(screen.queryByText("primary-vault")).toBeNull();
    const hrefs = [...document.querySelectorAll(".stat-card")].map((tile) => tile.getAttribute("href"));
    expect(hrefs).toContain("/backup/repositories?state=retired");
  });

  it("leaves a retired repository out of what a policy may name", () => {
    expect(repositoriesOfClient([ACTIVE, RETIRED, THEIRS], "c-andes").map((repo) => repo.id)).toEqual(["repo-1"]);
    expect(repositoryPickerOptions([RETIRED])[0]?.sublabel).toBe("s3://andes/primary · retired");
  });

  it("offers only an ACTIVE repository, refusing by default a state it does not know", () => {
    // Phase 3 final review C, Minor (T14-1): `state` is free text server-side,
    // and `!== "retired"` would offer a state added later -- say one being
    // drained -- as somewhere to send new backups.
    const unknown = { ...ACTIVE, id: "repo-9", state: "draining" };
    expect(repositoriesOfClient([ACTIVE, unknown], "c-andes").map((repo) => repo.id)).toEqual(["repo-1"]);
  });

  it("sends the retire to its route", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(() => Promise.resolve(new Response(JSON.stringify(ACTIVE), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient(vi.fn(), "/api/v1", () => null).retireBackupRepository("repo 1");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect([init.method, url]).toEqual(["POST", "/api/v1/backup/repositories/repo%201/retire"]);
  });
});

describe("retiring a repository, in fixture mode", () => {
  // The fixture data is a state the route allows: a retired repository is
  // named by no enabled policy. And the retire answers as the route does,
  // rather than as a missing fixture.
  it("keeps every retired repository out of the enabled policies", async () => {
    const api = fixtureClient();
    const retired = new Set(
      (await api.listBackupRepositories()).filter((repo) => repo.state === "retired").map((repo) => repo.id),
    );
    expect(retired.size).toBeGreaterThan(0);
    const policies = await api.listBackupPolicies();
    expect(policies.filter((policy) => policy.enabled && retired.has(policy.repository_id))).toEqual([]);
  });

  it("refuses a repository an enabled policy names, one already retired, and one that is not there", async () => {
    const api = fixtureClient();
    const repositories = await api.listBackupRepositories();
    const policies = await api.listBackupPolicies();
    const named = repositories.find(
      (repo) => repo.state === "active" && policies.some((p) => p.enabled && p.repository_id === repo.id),
    );
    const retired = repositories.find((repo) => repo.state === "retired");
    await expect(api.retireBackupRepository(named?.id ?? "")).rejects.toMatchObject({ status: 409 });
    await expect(api.retireBackupRepository(retired?.id ?? "")).rejects.toMatchObject({ status: 409 });
    await expect(api.retireBackupRepository("repo-nowhere")).rejects.toMatchObject({ status: 404 });
  });
});
