import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import { expectCta, expectNoPanel, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { ProvisioningPrerequisites } from "./ProvisioningPrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const TITLE = "Before a built machine can join";
const release = { id: "r1", version: "1.0.0", os: "windows", arch: "amd64", published_at: "2026-09-01T00:00:00Z", has_artifact: true };

function stub(releases: unknown[], permissions = ["provisioning.read", "agent.release.read"]) {
  const api = { me: meWith(permissions), listAgentReleases: vi.fn().mockResolvedValue(releases) };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("ProvisioningPrerequisites", () => {
  it("sends an image-builder with no published package to get the installer", async () => {
    stub([]);
    renderPanel(<ProvisioningPrerequisites />);
    await expectCta("Get the agent installer", "/agent-packages#releases");
    expect(screen.getByText(/A built machine joins by running the agent from its image/)).toBeInTheDocument();
  });

  it("is absent once a package is published", async () => {
    const api = stub([release]);
    renderPanel(<ProvisioningPrerequisites />);
    await expectNoPanel(TITLE, () => expect(api.listAgentReleases).toHaveBeenCalled());
  });

  it("says it cannot be checked without the release permission", async () => {
    const api = stub([], ["provisioning.read"]);
    renderPanel(<ProvisioningPrerequisites />);
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(api.listAgentReleases).not.toHaveBeenCalled();
  });
});
