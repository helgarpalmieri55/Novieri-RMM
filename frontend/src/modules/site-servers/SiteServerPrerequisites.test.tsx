import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import { expectCta, expectNoPanel, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { SiteServerPrerequisites } from "./SiteServerPrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const TITLE = "Before a site server can be promoted";
const fleet = (total: number, managed: number) => ({
  items: [],
  total: 0,
  state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
  fleet: { total, managed, online: managed, cert_attention: 0 },
});

function stub(overrides: Record<string, unknown> = {}, permissions = ["site.read", "devices.read", "ipam.read"]) {
  const api = {
    me: meWith(permissions),
    listDevices: vi.fn().mockResolvedValue(fleet(5, 2)),
    listIpamSubnets: vi.fn().mockResolvedValue([{ id: "s1", prefix: "10.0.0.0/24" }]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("SiteServerPrerequisites", () => {
  it("sends an estate where no computer runs the agent to add one, as blocking", async () => {
    stub({ listDevices: vi.fn().mockResolvedValue(fleet(5, 0)) });
    renderPanel(<SiteServerPrerequisites />);
    await expectCta("Add a computer", "/devices/onboard");
    expect(screen.getByText(/A site server is a computer that already runs the agent/)).toBeInTheDocument();
  });

  it("sends a network with no registered subnet to register one", async () => {
    stub({ listIpamSubnets: vi.fn().mockResolvedValue([]) });
    renderPanel(<SiteServerPrerequisites />);
    await expectCta("Register the segment", "/ipam?register=1");
  });

  it("is absent when an agent runs and a subnet is registered", async () => {
    const api = stub();
    renderPanel(<SiteServerPrerequisites />);
    await expectNoPanel(TITLE, () => {
      expect(api.listDevices).toHaveBeenCalled();
      expect(api.listIpamSubnets).toHaveBeenCalled();
    });
  });

  it("says the subnet check cannot be made without the IP address permission, and does not ask", async () => {
    const api = stub({}, ["site.read", "devices.read"]);
    renderPanel(<SiteServerPrerequisites />);
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(api.listIpamSubnets).not.toHaveBeenCalled();
  });
});
