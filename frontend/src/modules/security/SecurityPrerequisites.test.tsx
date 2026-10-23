import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient, DeviceListParams } from "../../lib/api";
import { expectCta, expectNoPanel, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { SecurityPrerequisites } from "./SecurityPrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const TITLE = "Before security events can be collected";

/** `all` rows of this family, `assets` of them with no agent. */
function familyPage(all: number, assets: number) {
  return {
    items: [],
    total: all,
    state_counts: { all, online: 0, offline: 0, assets, certs: 0 },
    fleet: { total: 99, managed: 99, online: 99, cert_attention: 0 },
  };
}

function stub(byFamily: Record<string, ReturnType<typeof familyPage>>, permissions = ["devices.read"]) {
  const api = {
    me: meWith(permissions),
    listDevices: vi.fn((params: DeviceListParams) => Promise.resolve(byFamily[params.osFamily ?? ""])),
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("SecurityPrerequisites", () => {
  it("sends a fleet with no Windows or Mac agent to add one, whatever runs on Linux", async () => {
    // Windows and Mac rows exist, but only as assets with no agent.
    const api = stub({ windows: familyPage(3, 3), macos: familyPage(1, 1) });
    renderPanel(<SecurityPrerequisites />);
    await expectCta("Add a Windows or Mac computer", "/devices/onboard");
    expect(api.listDevices).toHaveBeenCalledWith({ osFamily: "windows", limit: 1 });
    expect(api.listDevices).toHaveBeenCalledWith({ osFamily: "macos", limit: 1 });
  });

  it("is absent once one Mac runs the agent", async () => {
    const api = stub({ windows: familyPage(0, 0), macos: familyPage(2, 1) });
    renderPanel(<SecurityPrerequisites />);
    await expectNoPanel(TITLE, () => expect(api.listDevices).toHaveBeenCalledTimes(2));
  });

  it("says it cannot be checked without the device permission, and does not ask", async () => {
    const api = stub({}, ["audit.read"]);
    renderPanel(<SecurityPrerequisites />);
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(api.listDevices).not.toHaveBeenCalled();
  });
});
