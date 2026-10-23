import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import { expectCta, expectNoPanel, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { EnrollmentPrerequisites } from "./EnrollmentPrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const TITLE = "Before a token can enroll a computer";
const release = (published: boolean, has_artifact: boolean) => ({
  id: "r1",
  version: "1.0.0",
  os: "windows",
  arch: "amd64",
  published_at: published ? "2026-09-01T00:00:00Z" : null,
  has_artifact,
});

function stub(releases: unknown[], permissions = ["enrollment.manage", "agent.release.read"]) {
  const api = { me: meWith(permissions), listAgentReleases: vi.fn().mockResolvedValue(releases) };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("EnrollmentPrerequisites", () => {
  it("sends a catalogue with nothing installable to Agent packages, as blocking", async () => {
    stub([release(true, false)]);
    renderPanel(<EnrollmentPrerequisites />);
    await expectCta("Publish an agent package", "/agent-packages#releases");
    expect(screen.getByText(/A token lets a computer join once the agent is installed/)).toBeInTheDocument();
  });

  it("is absent once a package is published", async () => {
    const api = stub([release(true, true)]);
    renderPanel(<EnrollmentPrerequisites />);
    await expectNoPanel(TITLE, () => expect(api.listAgentReleases).toHaveBeenCalled());
  });

  it("says it cannot be checked without the release permission, and does not ask", async () => {
    const api = stub([], ["enrollment.manage"]);
    renderPanel(<EnrollmentPrerequisites />);
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(api.listAgentReleases).not.toHaveBeenCalled();
  });
});
