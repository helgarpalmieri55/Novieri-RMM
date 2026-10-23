import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import { expectCta, expectNoPanel, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { AutomationPrerequisites } from "./AutomationPrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const TITLE = "Before the assistant can act";

function stub(overrides: Record<string, unknown> = {}, permissions = ["ai.admin", "ai.use", "health.read"]) {
  const api = {
    me: meWith(permissions),
    getAiStatus: vi.fn().mockResolvedValue({ enabled: true, reason: "" }),
    listApprovedActions: vi.fn().mockResolvedValue([{ id: "a1" }]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("AutomationPrerequisites", () => {
  it("sends a server with the assistant off to its settings, with the server's reason", async () => {
    stub({ getAiStatus: vi.fn().mockResolvedValue({ enabled: false, reason: "No model provider is configured." }) });
    renderPanel(<AutomationPrerequisites />);
    await expectCta("Open assistant settings", "/assistant/settings");
    expect(screen.getByText(/No model provider is configured\./)).toBeInTheDocument();
  });

  it("sends an organization with no approved action to the approved actions", async () => {
    stub({ listApprovedActions: vi.fn().mockResolvedValue([]) });
    renderPanel(<AutomationPrerequisites />);
    await expectCta("Open approved actions", "/health#approved-actions");
  });

  it("is absent when the assistant is on and actions exist", async () => {
    const api = stub();
    renderPanel(<AutomationPrerequisites />);
    await expectNoPanel(TITLE, () => {
      expect(api.getAiStatus).toHaveBeenCalled();
      expect(api.listApprovedActions).toHaveBeenCalled();
    });
  });

  it("says what an administrator without the assistant or alerts permission cannot check", async () => {
    const api = stub({}, ["ai.admin"]);
    renderPanel(<AutomationPrerequisites />);
    expect((await screen.findAllByText(/cannot be checked from this account/)).length).toBe(2);
    expect(api.getAiStatus).not.toHaveBeenCalled();
    expect(api.listApprovedActions).not.toHaveBeenCalled();
  });
});
