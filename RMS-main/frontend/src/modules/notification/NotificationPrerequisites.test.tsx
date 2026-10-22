import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import { expectCta, expectNoPanel, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { NotificationPrerequisites } from "./NotificationPrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const TITLE = "Before notifications can reach people";
const user = (id: string, status = "active") => ({ id, email: `${id}@x`, display_name: id, status, roles: [] });

function stub(overrides: Record<string, unknown> = {}, permissions = ["users.read", "ticket.read"]) {
  const api = {
    me: meWith(permissions),
    listUsers: vi.fn().mockResolvedValue({ users: [user("a"), user("b")] }),
    listTicketQueues: vi.fn().mockResolvedValue([{ id: "q1" }]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("NotificationPrerequisites", () => {
  it("sends an organization of one to Settings → Users, counting only active people", async () => {
    stub({ listUsers: vi.fn().mockResolvedValue({ users: [user("a"), user("b", "disabled")] }) });
    renderPanel(<NotificationPrerequisites />);
    await expectCta("Add users", "/settings?tab=users");
    expect(screen.getByText("1 ticket queue exists.")).toBeInTheDocument();
  });

  it("sends a desk with no queue to the service desk settings", async () => {
    stub({ listTicketQueues: vi.fn().mockResolvedValue([]) });
    renderPanel(<NotificationPrerequisites />);
    await expectCta("Set up queues", "/service-desk/admin");
  });

  it("is absent when people and queues exist", async () => {
    const api = stub();
    renderPanel(<NotificationPrerequisites />);
    await expectNoPanel(TITLE, () => {
      expect(api.listUsers).toHaveBeenCalled();
      expect(api.listTicketQueues).toHaveBeenCalled();
    });
  });

  it("says what this account cannot check, and does not ask", async () => {
    const api = stub({}, ["ticket.read"]);
    renderPanel(<NotificationPrerequisites />);
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(api.listUsers).not.toHaveBeenCalled();
    expect(screen.queryByRole("link", { name: "Add users" })).toBeNull();
  });
});
