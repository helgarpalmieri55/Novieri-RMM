import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { AgentRelease, ApiClient, DeviceFleetCounts } from "./api";
import {
  useFleetPrerequisite,
  useGroupsPrerequisite,
  useInstallerPrerequisite,
  useRulesPrerequisite,
} from "./detectors";
import { useMePermissions } from "./mePermissions";

// The shared detectors (`docs/design/cross-module-guidance.md` §0.3). What is
// asserted is the item an operator reads: missing with the right way out, done
// with a count, or "cannot be checked from this account" -- and that an
// account without the code does not fire a request that can only be refused.

const mockUseApi = vi.fn();
vi.mock("./ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = ["devices.read", "agent.release.read", "notification.read"];

function stubApi(overrides: Partial<Record<keyof ApiClient, unknown>> & { permissions?: string[] } = {}) {
  const { permissions = ALL, ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listDevices: vi.fn().mockResolvedValue(page({ total: 0, managed: 0, online: 0, cert_attention: 0 })),
    listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 0, ungrouped_device_count: 0, last_evaluated_at: null }),
    listAgentReleases: vi.fn().mockResolvedValue([]),
    listNotificationRules: vi.fn().mockResolvedValue([]),
    ...rest,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function page(fleet: DeviceFleetCounts) {
  return {
    items: [],
    total: fleet.total,
    limit: 1,
    offset: 0,
    state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
    fleet,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function release(overrides: Partial<AgentRelease> = {}): AgentRelease {
  return {
    id: "r1",
    version: "1.8.2",
    os: "linux",
    arch: "amd64",
    size_bytes: 1,
    sha256: "ab",
    signed: true,
    notes: "",
    published_at: "2026-09-01T00:00:00Z",
    has_artifact: true,
    ...overrides,
  };
}

async function settled<T extends { state: string }>(hook: () => T) {
  const { result } = renderHook(hook, { wrapper });
  await waitFor(() => expect(result.current.state).not.toBe("checking"));
  return result.current;
}

describe("useMePermissions", () => {
  it("knows neither has nor lacks until /me answers", async () => {
    stubApi({ permissions: ["devices.read"] });
    const { result } = renderHook(() => useMePermissions(), { wrapper });
    expect(result.current.status).toBe("loading");
    expect(result.current.has("devices.read")).toBe(false);
    expect(result.current.lacks("devices.read")).toBe(false);
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.has("devices.read")).toBe(true);
    expect(result.current.lacks("notification.read")).toBe(true);
  });

  it("does not claim a lack when /me fails", async () => {
    stubApi({ me: vi.fn().mockRejectedValue(new Error("down")) });
    const { result } = renderHook(() => useMePermissions(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.lacks("devices.read")).toBe(false);
  });
});

describe("FLEET", () => {
  it("asks for one row and reads the fleet counts", async () => {
    const api = stubApi();
    await settled(() => useFleetPrerequisite("asset"));
    expect(api.listDevices).toHaveBeenCalledWith({ limit: 1 });
  });

  it("says no computer is recorded and links to adding one", async () => {
    stubApi();
    const item = await settled(() => useFleetPrerequisite("asset", { severity: "crit" }));
    expect(item).toMatchObject({
      state: "missing",
      severity: "crit",
      cta: { to: "/devices/onboard", label: "Add a computer" },
    });
    expect(item.detail).toBe("No computer is recorded yet.");
  });

  it("says recorded computers do not run the agent", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(page({ total: 4, managed: 0, online: 0, cert_attention: 0 })) });
    const item = await settled(() => useFleetPrerequisite("agent"));
    expect(item.state).toBe("missing");
    expect(item.detail).toBe("None of the 4 recorded computers runs the agent yet.");
    expect(item.cta).toEqual({ to: "/devices/onboard", label: "Add a computer" });
  });

  it("counts computers that run the agent", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(page({ total: 4, managed: 3, online: 1, cert_attention: 0 })) });
    const item = await settled(() => useFleetPrerequisite("agent"));
    expect(item).toMatchObject({ state: "done", detail: "3 computers run the agent." });
  });

  it("tells offline agents apart from no agents, and points at the offline list", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(page({ total: 4, managed: 3, online: 0, cert_attention: 0 })) });
    const item = await settled(() => useFleetPrerequisite("online"));
    expect(item.state).toBe("missing");
    expect(item.detail).toBe("None of the 3 computers running the agent is connected right now.");
    expect(item.cta).toEqual({ to: "/devices?state=offline", label: "See offline computers" });
  });

  it("marks online done when an agent is connected", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(page({ total: 4, managed: 3, online: 2, cert_attention: 0 })) });
    const item = await settled(() => useFleetPrerequisite("online"));
    expect(item).toMatchObject({ state: "done", detail: "2 of 3 computers running the agent are connected." });
  });

  it("says an account without the code cannot check, and asks nothing", async () => {
    const api = stubApi({ permissions: ["notification.read"] });
    const item = await settled(() => useFleetPrerequisite("agent"));
    expect(item.state).toBe("unchecked");
    expect(item.detail).toBe("This cannot be checked from this account: it needs permission to view devices.");
    expect(api.listDevices).not.toHaveBeenCalled();
  });

  it("takes a page's own label, sentence and severity", async () => {
    stubApi();
    const item = await settled(() =>
      useFleetPrerequisite("asset", {
        label: "Devices exist to put in groups",
        missingDetail: "A group of nothing does nothing.",
        ctaLabel: "Add the first computer",
      }),
    );
    expect(item).toMatchObject({
      label: "Devices exist to put in groups",
      severity: "warn",
      detail: "A group of nothing does nothing.",
      cta: { to: "/devices/onboard", label: "Add the first computer" },
    });
  });
});

describe("GROUPS", () => {
  it("says no group exists and links to creating one", async () => {
    stubApi();
    const item = await settled(() => useGroupsPrerequisite());
    expect(item).toMatchObject({
      state: "missing",
      detail: "No device group exists yet.",
      cta: { to: "/device-groups", label: "Create a device group" },
    });
  });

  it("counts groups", async () => {
    stubApi({
      listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 2, ungrouped_device_count: 0, last_evaluated_at: null }),
    });
    expect(await settled(() => useGroupsPrerequisite())).toMatchObject({ state: "done", detail: "2 device groups exist." });
  });

  it("cannot be checked without the code", async () => {
    const api = stubApi({ permissions: [] });
    expect((await settled(() => useGroupsPrerequisite())).state).toBe("unchecked");
    expect(api.listDeviceGroups).not.toHaveBeenCalled();
  });
});

describe("INSTALLER", () => {
  it("says nothing is published and links to publishing", async () => {
    stubApi();
    const item = await settled(() => useInstallerPrerequisite());
    expect(item).toMatchObject({
      state: "missing",
      detail: "No agent package is published yet, so there is nothing to install.",
      cta: { to: "/agent-packages#releases", label: "Publish an agent package" },
    });
  });

  it("does not count a release that is unpublished or has no file", async () => {
    stubApi({
      listAgentReleases: vi.fn().mockResolvedValue([
        release({ published_at: null }),
        release({ has_artifact: false, sha256: "" }),
      ]),
    });
    expect((await settled(() => useInstallerPrerequisite())).state).toBe("missing");
  });

  it("names the platforms a published package covers", async () => {
    stubApi({ listAgentReleases: vi.fn().mockResolvedValue([release(), release({ id: "r2", os: "windows" })]) });
    const item = await settled(() => useInstallerPrerequisite());
    expect(item).toMatchObject({ state: "done" });
    expect(item.detail).toBe("Published for Linux (64-bit Intel/AMD) and Windows (64-bit Intel/AMD).");
  });

  it("counts only the platforms asked for, and names the ones still without a package", async () => {
    const platforms = [
      { os: "linux", arch: "amd64" },
      { os: "darwin", arch: "arm64" },
    ];
    stubApi({ listAgentReleases: vi.fn().mockResolvedValue([release({ os: "windows" })]) });
    const none = await settled(() => useInstallerPrerequisite({ platforms }));
    expect(none.state).toBe("missing");
    expect(none.detail).toBe("No agent package is published for Linux (64-bit Intel/AMD) or macOS (Apple silicon) yet.");

    stubApi({ listAgentReleases: vi.fn().mockResolvedValue([release()]) });
    const some = await settled(() => useInstallerPrerequisite({ platforms }));
    expect(some.state).toBe("done");
    expect(some.detail).toBe("Published for Linux (64-bit Intel/AMD). Nothing yet for macOS (Apple silicon).");
  });

  it("cannot be checked without the code", async () => {
    const api = stubApi({ permissions: ["devices.read"] });
    const item = await settled(() => useInstallerPrerequisite());
    expect(item.state).toBe("unchecked");
    expect(item.detail).toBe("This cannot be checked from this account: it needs permission to install agents.");
    expect(api.listAgentReleases).not.toHaveBeenCalled();
  });
});

describe("RULES", () => {
  const rule = (event_type: string, active: boolean) => ({
    id: event_type,
    event_type,
    channel_id: "c1",
    recipient_kind: "channel",
    queue_id: null,
    active,
    created_at: "2026-09-01T00:00:00Z",
  });

  it("needs an ACTIVE rule for the event prefix", async () => {
    stubApi({
      listNotificationRules: vi.fn().mockResolvedValue([rule("health.alert.opened", false), rule("backup.failed", true)]),
    });
    const item = await settled(() => useRulesPrerequisite("health."));
    expect(item).toMatchObject({
      state: "missing",
      cta: { to: "/notifications#rules", label: "Send these to someone" },
    });
  });

  it("is done when one routes the events", async () => {
    stubApi({ listNotificationRules: vi.fn().mockResolvedValue([rule("health.alert.opened", true)]) });
    const item = await settled(() => useRulesPrerequisite("health."));
    expect(item).toMatchObject({ state: "done", detail: "1 active rule sends these." });
  });

  it("cannot be checked without the code", async () => {
    const api = stubApi({ permissions: ["devices.read"] });
    expect((await settled(() => useRulesPrerequisite("health."))).state).toBe("unchecked");
    expect(api.listNotificationRules).not.toHaveBeenCalled();
  });
});
