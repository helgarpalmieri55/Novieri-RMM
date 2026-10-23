/**
 * The self-update section (spec-self-update.md §8.6): "Needs manual update",
 * the rollout list, the create-preview-start flow and ring controls.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { FleetVersions, Me, UpdateRollout } from "../../lib/api";
import { AgentUpdatesSection } from "./AgentUpdatesSection";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function me(permissions: string[]): Me {
  return {
    user_id: "u",
    org_id: "o",
    email: "op@example.com",
    display_name: "Operator",
    permissions,
    timezone: "UTC",
  } as Me;
}

const FLEET: FleetVersions = {
  total: 3,
  versions: { "1.8.2": 1, unknown: 1, "1.0.0": 1 },
  latest_stable: "1.8.2",
  update_available: 0,
  needs_manual_update: [
    { device_id: "d-old", hostname: "old-laptop", version: "", reason: "version_unknown" },
    { device_id: "d-pre", hostname: "pre-update", version: "1.0.0", reason: "no_agent_update" },
  ],
};

function rollout(overrides: Partial<UpdateRollout> = {}): UpdateRollout {
  return {
    id: "r1",
    target_version: "1.8.2",
    is_rollback: false,
    state: "running",
    batch_size: 10,
    failure_threshold_pct: 5,
    health_window_s: 600,
    reason: "",
    pause_reason: null,
    created_at: "2026-09-23T09:00:00Z",
    started_at: "2026-09-23T09:01:00Z",
    completed_at: null,
    rings: [
      {
        id: "g0",
        ordinal: 0,
        name: "Pilot",
        is_pilot: true,
        device_group_id: "grp",
        state: "active",
        activated_at: "2026-09-23T09:01:00Z",
        counts: { dispatched: 1, succeeded: 2 },
      },
    ],
    ...overrides,
  };
}

function renderSection(permissions: string[], api: Record<string, unknown>) {
  mockUseApi.mockReturnValue({
    getFleetVersions: () => Promise.resolve(FLEET),
    listUpdateRollouts: () => Promise.resolve([]),
    listUpdateTargets: () => Promise.resolve([]),
    listAgentReleases: () =>
      Promise.resolve([
        {
          id: "a",
          version: "1.8.2",
          os: "linux",
          arch: "amd64",
          has_artifact: true,
          channel: "stable",
        },
      ]),
    listDeviceGroups: () =>
      Promise.resolve({ items: [{ id: "grp", name: "Pilot machines" }], total: 1 }),
    ...api,
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AgentUpdatesSection me={me(permissions)} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AgentUpdatesSection", () => {
  beforeEach(() => mockUseApi.mockReset());

  it("lists the agents that need a manual update and says why", async () => {
    renderSection(["agent.update.read"], {});
    const table = await screen.findByRole("table", { name: "Agents that need a manual update" });
    expect(within(table).getByText("old-laptop")).toBeInTheDocument();
    expect(within(table).getByText(/installed before self-update existed/)).toBeInTheDocument();
    expect(within(table).getByText(/cannot run agent.update/)).toBeInTheDocument();
    expect(within(table).getAllByText("Needs manual update")).toHaveLength(2);
    expect(screen.getByRole("link", { name: /install command/ })).toHaveAttribute(
      "href",
      "#releases",
    );
  });

  it("offers no rollout controls to a reader", async () => {
    renderSection(["agent.update.read"], {
      listUpdateRollouts: () => Promise.resolve([rollout()]),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(screen.queryByRole("button", { name: "New rollout…" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
  });

  it("renders the controls a running rollout's state allows", async () => {
    renderSection(["agent.update.read", "agent.update.rollout"], {
      listUpdateRollouts: () => Promise.resolve([rollout()]),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close ring" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    // The active ring has not finished, so there is nothing to advance to.
    expect(screen.queryByRole("button", { name: "Advance ring" })).toBeNull();
  });

  it("pauses through the API", async () => {
    const action = vi.fn(() => Promise.resolve(rollout({ state: "paused" })));
    renderSection(["agent.update.read", "agent.update.rollout"], {
      listUpdateRollouts: () => Promise.resolve([rollout()]),
      updateRolloutAction: action,
    });
    await userEvent.click(await screen.findByRole("button", { name: "Details" }));
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(action).toHaveBeenCalledWith("r1", "pause");
  });

  it("previews, then creates and starts a rollout with a pilot ring and everyone else", async () => {
    const preview = vi.fn(() =>
      Promise.resolve({
        target_version: "1.8.2",
        is_rollback: false,
        channel: "stable",
        rings: [
          {
            ordinal: 0,
            name: "Pilot",
            is_pilot: true,
            device_group_id: "grp",
            total: 3,
            eligible: 2,
            needs_manual_update: 1,
            no_artifact: 0,
            already_current: 0,
            other_skipped: 0,
          },
          {
            ordinal: 1,
            name: "Everyone else",
            is_pilot: false,
            device_group_id: null,
            total: 5,
            eligible: 5,
            needs_manual_update: 0,
            no_artifact: 0,
            already_current: 0,
            other_skipped: 0,
          },
        ],
      }),
    );
    const create = vi.fn(() => Promise.resolve(rollout({ id: "new", state: "draft" })));
    const action = vi.fn(() => Promise.resolve(rollout({ id: "new" })));
    renderSection(["agent.update.read", "agent.update.rollout"], {
      previewUpdateRollout: preview,
      createUpdateRollout: create,
      updateRolloutAction: action,
    });

    await userEvent.click(await screen.findByRole("button", { name: "New rollout…" }));
    const dialog = screen.getByRole("dialog", { name: "New agent update rollout" });
    const versionSelect = within(dialog).getByRole("combobox", { name: "Version" });
    const groupSelect = within(dialog).getByRole("combobox", { name: /Pilot ring/ });
    await screen.findByRole("option", { name: "1.8.2 (stable)" });
    await userEvent.selectOptions(versionSelect, "1.8.2");
    await screen.findByRole("option", { name: "Pilot machines" });
    await userEvent.selectOptions(groupSelect, "grp");
    expect(within(dialog).getByRole("button", { name: "Create and start" })).toBeDisabled();

    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    const table = await within(dialog).findByRole("table", { name: "Preview" });
    expect(within(table).getByText("Everyone else")).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: "Create and start" }));
    expect(create).toHaveBeenCalledWith({
      target_version: "1.8.2",
      batch_size: 10,
      rings: [
        { name: "Pilot", device_group_id: "grp", is_pilot: true },
        { name: "Everyone else", device_group_id: null },
      ],
    });
    await vi.waitFor(() => expect(action).toHaveBeenCalledWith("new", "start"));
  });
});
