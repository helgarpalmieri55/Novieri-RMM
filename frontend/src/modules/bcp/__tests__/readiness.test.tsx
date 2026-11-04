import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, MeClient } from "../../../lib/api";
import { BcpReadiness, reviewerAssignments } from "../BcpReadiness";

// "Before continuity tests prove anything" (cross-module guidance, BCP rows 3
// and 4). Rows 1 and 2 are links inside the plan editor (bcp.test.tsx).

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

/** The internal client every test here works in unless it chooses otherwise. */
const SCOPE_INTERNAL: MeClient = {
  id: "o1",
  name: "Novieri",
  kind: "internal",
  enabled_modules: [],
  permissions: [],
};

/** The selector (Task 7): `selected` is the client chosen at the top, `null` for every client. */
function selectClient(selected: string | null, clients: MeClient[] = [SCOPE_INTERNAL]): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients, ready: true });
}

beforeEach(() => selectClient(SCOPE_INTERNAL.id));

const ALL = ["bcp.read", "backup.read", "roles.read"];

function role(codes: string[], assigned: number) {
  return { id: "r1", name: "Role", description: "", is_global: false, assigned_users: assigned, codes };
}

const POLICY = {
  id: "p1",
  device_group_id: null,
  repository_id: "repo1",
  sources: {},
  window_cron: null,
  retention_gfs: {},
  enabled: true,
};

function stubApi(overrides: Partial<ApiClient> & { permissions?: string[] } = {}): ApiClient {
  const { permissions = ALL, ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listBackupPolicies: vi.fn().mockResolvedValue([POLICY]),
    listRoles: vi.fn().mockResolvedValue({ roles: [role(["bcp.approve"], 1)] }),
    ...rest,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <BcpReadiness />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("reviewerAssignments", () => {
  it("counts only roles that allow plan review", () => {
    expect(reviewerAssignments([role(["bcp.approve"], 2), role(["bcp.read"], 5)])).toBe(2);
  });
});

describe("BcpReadiness", () => {
  it("renders nothing when a backup policy is enabled and one person can review", async () => {
    // One reviewer is enough: `review_plan` lets an author sign off their own plan.
    const api = stubApi();
    renderPanel();
    await waitFor(() => expect(api.listRoles).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("Before continuity tests prove anything")).toBeNull();
  });

  it("links a fleet with no enabled backup policy to backup policies", async () => {
    stubApi({ listBackupPolicies: vi.fn().mockResolvedValue([{ ...POLICY, enabled: false }]) });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Set up backups" })).toHaveAttribute("href", "/backup/policies");
  });

  it("links an organization with nobody to review plans to Settings → Roles", async () => {
    stubApi({ listRoles: vi.fn().mockResolvedValue({ roles: [role(["bcp.approve"], 0)] }) });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Give someone plan review" })).toHaveAttribute(
      "href",
      "/settings?tab=roles",
    );
  });

  it("says the backup check cannot be made without backup read access, and never asks", async () => {
    const api = stubApi({ permissions: ["bcp.read", "roles.read"] });
    renderPanel();
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Set up backups" })).toBeNull();
    expect(api.listBackupPolicies).not.toHaveBeenCalled();
  });
});
