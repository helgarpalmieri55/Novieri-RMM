import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { approverAssignments, ChangeReadiness } from "../ChangeReadiness";

// "Before changes can be approved and gated" (cross-module guidance, Changes).
// Asserted on what an operator reads and where the button goes: the approver
// row is the one that decides whether a submitted change can ever move.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = ["change.read", "roles.read", "devices.read"];

function role(codes: string[], assigned: number, id = "r1") {
  return { id, name: `Role ${id}`, description: "", is_global: false, assigned_users: assigned, codes };
}

const GROUPS = { items: [], total: 1, ungrouped_device_count: 0, evaluation: null };

function stubApi(overrides: Partial<ApiClient> & { permissions?: string[] } = {}): ApiClient {
  const { permissions = ALL, ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listRoles: vi.fn().mockResolvedValue({
      roles: [role(["change.approve"], 1, "r1"), role(["change.approve", "change.read"], 2, "r2")],
    }),
    listDeviceGroups: vi.fn().mockResolvedValue(GROUPS),
    ...rest,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

function renderPanel(ui: ReactElement = <ChangeReadiness />) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("approverAssignments", () => {
  it("sums assignments of every role that carries change approval, and no other", () => {
    expect(
      approverAssignments([role(["change.approve"], 2), role(["change.read"], 9), role(["change.approve"], 1)]),
    ).toBe(3);
  });
});

describe("ChangeReadiness", () => {
  it("renders nothing when two approvers and a device group exist", async () => {
    const api = stubApi();
    renderPanel();
    await waitFor(() => expect(api.listRoles).toHaveBeenCalled());
    await waitFor(() => expect(api.listDeviceGroups).toHaveBeenCalled());
    // Give the queries a tick to settle; the panel must stay hidden.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("Before changes can be approved and gated")).toBeNull();
  });

  it("says nobody can approve and links to Settings → Roles", async () => {
    stubApi({ listRoles: vi.fn().mockResolvedValue({ roles: [role(["change.read"], 4)] }) });
    renderPanel();
    expect(await screen.findByText(/Nobody is allowed to approve a change/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Give someone change approval" })).toHaveAttribute(
      "href",
      "/settings?tab=roles",
    );
  });

  it("treats a single approver as blocking, because nobody approves their own change", async () => {
    stubApi({ listRoles: vi.fn().mockResolvedValue({ roles: [role(["change.approve"], 1)] }) });
    renderPanel();
    expect(await screen.findByText(/that person's own changes can never be approved/)).toBeInTheDocument();
  });

  it("says out loud that the count is of assignments, not people", async () => {
    stubApi({ listRoles: vi.fn().mockResolvedValue({ roles: [] }) });
    renderPanel();
    expect(await screen.findByText(/counts role assignments, not people/)).toBeInTheDocument();
  });

  it("links a missing device group to Device groups", async () => {
    stubApi({ listDeviceGroups: vi.fn().mockResolvedValue({ ...GROUPS, total: 0 }) });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Create a device group" })).toHaveAttribute(
      "href",
      "/device-groups",
    );
  });

  it("says the approver check cannot be made without role read access, and never asks", async () => {
    const api = stubApi({ permissions: ["change.read", "devices.read"] });
    renderPanel();
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Give someone change approval" })).toBeNull();
    expect(api.listRoles).not.toHaveBeenCalled();
  });
});
