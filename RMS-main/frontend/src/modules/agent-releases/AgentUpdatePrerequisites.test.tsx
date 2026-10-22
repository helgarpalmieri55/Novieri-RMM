import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import { expectCta, expectNoPanel, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { AgentUpdatePrerequisites } from "./AgentUpdatePrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const TITLE = "Before an agent update can roll out";
const READER = ["agent.update.read", "devices.read"];

const group = (id: string, device_count: number, archived = false) => ({
  id,
  name: id,
  parent_group_id: null,
  description: "",
  dynamic: false,
  device_count,
  precedence: 0,
  archived_at: archived ? "2026-09-01T00:00:00Z" : null,
});
const groups = (...items: ReturnType<typeof group>[]) => ({
  items,
  total: items.length,
  ungrouped_device_count: 0,
  last_evaluated_at: null,
});
const fleet = (managed: number) => ({
  items: [],
  total: 0,
  state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
  fleet: { total: managed, managed, online: managed, cert_attention: 0 },
});
const versions = (latest_stable: string | null) => ({
  total: 3,
  versions: { "1.0.0": 3 },
  latest_stable,
  update_available: 0,
  needs_manual_update: [],
});

function stub(overrides: Record<string, unknown> = {}, permissions = READER) {
  const api = {
    me: meWith(permissions),
    listDevices: vi.fn().mockResolvedValue(fleet(3)),
    listDeviceGroups: vi.fn().mockResolvedValue(groups(group("pilot", 5))),
    getFleetVersions: vi.fn().mockResolvedValue(versions("1.0.0")),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("AgentUpdatePrerequisites", () => {
  it("sends a fleet with only large or empty groups to Device groups for a pilot group, as blocking", async () => {
    stub({
      listDeviceGroups: vi
        .fn()
        .mockResolvedValue(groups(group("everyone", 400), group("empty", 0), group("old", 3, true))),
    });
    renderPanel(<AgentUpdatePrerequisites />);
    await expectCta("Create a pilot group (at most 25)", "/device-groups");
    expect(screen.getByText(/every device group with computers in it has more than 25/)).toBeInTheDocument();
  });

  it("sends a publisher with no stable release to the release channels", async () => {
    stub({ getFleetVersions: vi.fn().mockResolvedValue(versions(null)) }, [...READER, "agent.release.manage"]);
    renderPanel(<AgentUpdatePrerequisites />);
    await expectCta("Mark a release as stable", "/agent-packages#channels");
  });

  it("tells a non-publisher who marks a release stable, rather than linking to a section they are not shown", async () => {
    stub({ getFleetVersions: vi.fn().mockResolvedValue(versions(null)) });
    renderPanel(<AgentUpdatePrerequisites />);
    expect(await screen.findByText(/Whoever publishes agent releases marks one as stable/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Mark a release as stable" })).toBeNull();
  });

  it("sends a fleet with no agent to add a computer", async () => {
    stub({ listDevices: vi.fn().mockResolvedValue(fleet(0)) });
    renderPanel(<AgentUpdatePrerequisites />);
    await expectCta("Add a computer", "/devices/onboard");
  });

  it("is absent when a pilot-sized group, an agent and a stable release all exist", async () => {
    const api = stub();
    renderPanel(<AgentUpdatePrerequisites />);
    await expectNoPanel(TITLE, () => {
      expect(api.listDevices).toHaveBeenCalled();
      expect(api.listDeviceGroups).toHaveBeenCalled();
      expect(api.getFleetVersions).toHaveBeenCalled();
    });
  });

  it("says what cannot be checked without the device permission, and does not ask", async () => {
    const api = stub({}, ["agent.update.read"]);
    renderPanel(<AgentUpdatePrerequisites />);
    expect((await screen.findAllByText(/cannot be checked from this account/)).length).toBe(2);
    expect(api.listDeviceGroups).not.toHaveBeenCalled();
    expect(screen.queryByRole("link", { name: /pilot group/ })).toBeNull();
  });
});
