import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import { expectCta, expectNoPanel, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { EmailIntakePrerequisites } from "./EmailIntakePrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const TITLE = "Before mail can become tickets";

function message(reason: string, from = "stranger@elsewhere.example") {
  return { id: reason, from_address: from, disposition: "quarantined", reason };
}

function stub(overrides: Record<string, unknown> = {}, permissions = ["ticket.read", "email_intake.read"]) {
  const api = {
    me: meWith(permissions),
    listTicketCategories: vi.fn().mockResolvedValue([{ id: "c1" }]),
    listEmailIntakeMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, quarantined_count: 0 }),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("EmailIntakePrerequisites", () => {
  it("sends a desk with no ticket category to the service desk settings, as blocking", async () => {
    stub({ listTicketCategories: vi.fn().mockResolvedValue([]) });
    renderPanel(<EmailIntakePrerequisites />);
    await expectCta("Create a category", "/service-desk/admin");
    expect(screen.getByText(/no mailbox can be set up/)).toBeInTheDocument();
  });

  it("counts unknown senders among recent quarantined mail and sends them to Settings → Users", async () => {
    const api = stub({
      listEmailIntakeMessages: vi.fn().mockResolvedValue({
        messages: [
          message("unknown sender: a@x.example", "a@x.example"),
          message("unknown sender: A@x.example", "A@x.example"),
          message("unknown sender: b@x.example", "b@x.example"),
          message("reply token did not verify", "c@x.example"),
        ],
        total: 4,
        quarantined_count: 4,
      }),
    });
    renderPanel(<EmailIntakePrerequisites />);
    await expectCta("Add these senders as users", "/settings?tab=users");
    // Two distinct addresses: case folds, and a different quarantine reason is not counted.
    expect(screen.getByText(/Mail from 2 addresses that are not a user here/)).toBeInTheDocument();
    expect(api.listEmailIntakeMessages).toHaveBeenCalledWith({ disposition: "quarantined", limit: 50 });
  });

  it("is absent when categories exist and no sender was unknown", async () => {
    const api = stub({
      listEmailIntakeMessages: vi.fn().mockResolvedValue({
        messages: [message("reply token did not verify")],
        total: 1,
        quarantined_count: 1,
      }),
    });
    renderPanel(<EmailIntakePrerequisites />);
    await expectNoPanel(TITLE, () => {
      expect(api.listTicketCategories).toHaveBeenCalled();
      expect(api.listEmailIntakeMessages).toHaveBeenCalled();
    });
  });

  it("says a category check cannot be made without the ticket permission, and does not ask", async () => {
    const api = stub({}, ["email_intake.read"]);
    renderPanel(<EmailIntakePrerequisites />);
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(api.listTicketCategories).not.toHaveBeenCalled();
    expect(screen.queryByRole("link", { name: "Create a category" })).toBeNull();
  });
});
