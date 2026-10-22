import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import { expectCta, expectNoPanel, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { PortalPrerequisites } from "./PortalPrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const TITLE = "Before requesters can use the portal";
const OPERATOR = ["portal.ticket.read", "roles.read", "docs.read", "ai.use", "notification.read"];

const role = (name: string, assigned: number) => ({ id: name, name, assigned_users: assigned, codes: [] });
const article = (id: string, portal_visible: boolean) => ({ id, title: id, archived: false, portal_visible });
const rule = (recipient_kind: string, active = true) => ({ id: recipient_kind, event_type: "ticket.status_changed", recipient_kind, active });

function stub(overrides: Record<string, unknown> = {}, permissions = OPERATOR) {
  const api = {
    me: meWith(permissions),
    listRoles: vi.fn().mockResolvedValue({ roles: [role("requester", 12), role("technician", 3)] }),
    listArticles: vi.fn().mockResolvedValue({ items: [article("a1", true)], total: 1 }),
    getAiStatus: vi.fn().mockResolvedValue({ enabled: true, reason: "" }),
    listNotificationRules: vi.fn().mockResolvedValue([rule("requester")]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("PortalPrerequisites", () => {
  it("sends an organization with no requester accounts to Settings → Users, as blocking", async () => {
    stub({ listRoles: vi.fn().mockResolvedValue({ roles: [role("requester", 0), role("technician", 3)] }) });
    renderPanel(<PortalPrerequisites />);
    await expectCta("Invite requesters", "/settings?tab=users");
    expect(screen.getByText(/nobody can sign in here to ask for help/)).toBeInTheDocument();
  });

  it("sends a portal with no published article to IT documentation, and no rule to Notifications", async () => {
    stub({
      listArticles: vi.fn().mockResolvedValue({ items: [article("a1", false)], total: 1 }),
      listNotificationRules: vi.fn().mockResolvedValue([rule("assignee"), rule("requester", false)]),
    });
    renderPanel(<PortalPrerequisites />);
    await expectCta("Publish help articles", "/docs");
    await expectCta("Tell requesters about replies", "/notifications#rules");
    expect(screen.getByText("12 people hold the requester role.")).toBeInTheDocument();
  });

  it("does not call a partly read library empty", async () => {
    stub({ listArticles: vi.fn().mockResolvedValue({ items: [article("a1", false)], total: 250 }) });
    renderPanel(<PortalPrerequisites />);
    expect(await screen.findByText(/None of the first 1 of 250 articles/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Publish help articles" })).toBeNull();
  });

  it("says the assistant is server configuration, with the server's reason and no link", async () => {
    stub({ getAiStatus: vi.fn().mockResolvedValue({ enabled: false, reason: "No model provider is configured." }) });
    renderPanel(<PortalPrerequisites />);
    expect(await screen.findByText(/No model provider is configured\./)).toBeInTheDocument();
    expect(screen.getByText(/whoever runs this server/)).toBeInTheDocument();
  });

  it("is absent when everything is in place", async () => {
    const api = stub();
    renderPanel(<PortalPrerequisites />);
    await expectNoPanel(TITLE, () => {
      expect(api.listRoles).toHaveBeenCalled();
      expect(api.listArticles).toHaveBeenCalled();
      expect(api.getAiStatus).toHaveBeenCalled();
      expect(api.listNotificationRules).toHaveBeenCalled();
    });
  });

  it("is never shown to a requester, and asks nothing on their behalf", async () => {
    const api = stub({}, ["portal.ticket.read", "portal.ticket.write"]);
    renderPanel(<PortalPrerequisites />);
    await expectNoPanel(TITLE, () => expect(api.me).toHaveBeenCalled());
    expect(api.listRoles).not.toHaveBeenCalled();
    expect(api.listNotificationRules).not.toHaveBeenCalled();
  });

  it("says what an operator without the role permission cannot check", async () => {
    const api = stub({}, ["portal.ticket.read", "ticket.read"]);
    renderPanel(<PortalPrerequisites />);
    expect((await screen.findAllByText(/cannot be checked from this account/)).length).toBeGreaterThan(0);
    expect(api.listRoles).not.toHaveBeenCalled();
    expect(screen.queryByRole("link", { name: "Invite requesters" })).toBeNull();
  });
});
