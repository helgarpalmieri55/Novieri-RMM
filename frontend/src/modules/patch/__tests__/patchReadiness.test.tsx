import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import type { ChangeGate } from "../../change/types";
import { PatchPolicyEditor } from "../PatchPolicyEditor";
import { PatchReadiness } from "../PatchReadiness";

// Patching's cross-module guidance (`docs/design/cross-module-guidance.md`
// §2 Patching): a change gate on patches, warned about before an approval is
// refused, and the per-group ring the policy editor could not offer while it
// believed no route listed device groups.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function gate(overrides: Partial<ChangeGate> = {}): ChangeGate {
  return {
    id: "g1",
    device_group_id: "grp-servers",
    device_group_name: "Servers",
    applies_to_patches: true,
    applies_to_deployments: false,
    created_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

function stubApi(overrides: Record<string, unknown> & { permissions?: string[] } = {}) {
  const { permissions = ["change.read", "patch.policy.manage", "devices.read"], ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listChangeGates: vi.fn().mockResolvedValue([]),
    listChanges: vi.fn().mockResolvedValue([]),
    ...rest,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderPanel(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

async function settled(client: QueryClient) {
  await waitFor(() => expect(client.isFetching()).toBe(0));
  await waitFor(() => expect(client.getQueryState(["me"])?.status).toBe("success"));
}

const LABEL = "An approved change covers gated updates";

describe("PatchReadiness — the change gate, before the 409", () => {
  it("names the gated group and sends the operator to raise a change", async () => {
    stubApi({ listChangeGates: vi.fn().mockResolvedValue([gate()]) });
    renderPanel(<PatchReadiness />);

    const item = (await screen.findByText(LABEL)).closest("li") as HTMLElement;
    expect(item).toHaveTextContent("Updates to devices in Servers need an approved change");
    expect(within(item).getByRole("link", { name: "Raise a change for these updates" })).toHaveAttribute(
      "href",
      "/changes",
    );
  });

  it("says every device when the gate names no group", async () => {
    stubApi({ listChangeGates: vi.fn().mockResolvedValue([gate({ device_group_id: null, device_group_name: null })]) });
    renderPanel(<PatchReadiness />);
    expect((await screen.findByText(LABEL)).closest("li")).toHaveTextContent("Updates to every device");
  });

  it("ignores a gate that covers deployments only", async () => {
    stubApi({
      listChangeGates: vi.fn().mockResolvedValue([gate({ applies_to_patches: false, applies_to_deployments: true })]),
    });
    const client = renderPanel(<PatchReadiness />);
    await settled(client);
    expect(screen.queryByText("Before updates can be approved")).not.toBeInTheDocument();
  });

  it("is not drawn while an approved change can be linked", async () => {
    stubApi({
      listChangeGates: vi.fn().mockResolvedValue([gate()]),
      listChanges: vi.fn().mockResolvedValue([{ id: "c1", state: "approved", title: "Patch Tuesday" }]),
    });
    const client = renderPanel(<PatchReadiness />);
    await settled(client);
    expect(screen.queryByText("Before updates can be approved")).not.toBeInTheDocument();
  });

  it("cannot be checked without change read, and asks nothing", async () => {
    const api = stubApi({ permissions: ["patch.read"] });
    renderPanel(<PatchReadiness />);
    expect((await screen.findByText(LABEL)).closest("li")).toHaveTextContent(
      "This cannot be checked from this account",
    );
    expect(api.listChangeGates).not.toHaveBeenCalled();
  });
});

describe("PatchPolicyEditor — a policy can be aimed at a device group", () => {
  const POLICY = {
    id: "pp1",
    device_group_id: null,
    ring_name: "broad",
    ring_order: 100,
    maintenance_window: null,
    auto_approve_security: false,
    reboot: "if_required",
    reboot_deferral_hours: 24,
    scan_interval_hours: 24,
  };
  const GROUP = {
    id: "grp-pilot",
    name: "Pilot",
    parent_group_id: null,
    device_count: 3,
    archived_at: null,
  };

  function policyApi(groups: unknown[], upsert = vi.fn().mockResolvedValue(POLICY)) {
    return stubApi({
      listPatchPolicies: vi.fn().mockResolvedValue([POLICY]),
      upsertPatchPolicy: upsert,
      listDeviceGroups: vi.fn().mockResolvedValue({
        items: groups,
        total: groups.length,
        ungrouped_device_count: 0,
        last_evaluated_at: null,
      }),
    });
  }

  it("saves a new ring for a group without replacing the organisation default", async () => {
    const upsert = vi.fn().mockResolvedValue(POLICY);
    policyApi([GROUP], upsert);
    const user = userEvent.setup();
    renderPanel(<PatchPolicyEditor />);

    await user.click(await screen.findByRole("button", { name: "New policy" }));
    const dialog = await screen.findByRole("dialog", { name: "New patch policy" });
    await user.selectOptions(await within(dialog).findByLabelText("Applies to"), "Pilot");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(upsert).toHaveBeenCalledWith(expect.objectContaining({ device_group_id: "grp-pilot" }));
    expect(
      screen.queryByRole("dialog", { name: "Replace the organisation-wide patch policy" }),
    ).not.toBeInTheDocument();
  });

  it("links to creating a device group when there is none", async () => {
    policyApi([]);
    const user = userEvent.setup();
    renderPanel(<PatchPolicyEditor />);

    await user.click(await screen.findByRole("button", { name: "New policy" }));
    const dialog = await screen.findByRole("dialog", { name: "New patch policy" });
    expect(await within(dialog).findByRole("link", { name: "Create a device group" })).toHaveAttribute(
      "href",
      "/device-groups",
    );
    expect(dialog).not.toHaveTextContent(/cannot list/);
  });
});
