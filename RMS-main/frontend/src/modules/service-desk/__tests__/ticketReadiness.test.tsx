import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { TicketSlaPanel } from "../../sla/TicketSlaPanel";
import { TicketReadiness } from "../TicketReadiness";

// "Before tickets are answered on time" (`docs/design/cross-module-guidance.md`
// §2 Tickets), and the ticket page's "No SLA" that now says where it is fixed.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = ["sla.read", "ticket.read", "email_intake.read", "health.read", "notification.read"];

const CATEGORY = { id: "c1", key: "hw", name: "Hardware", default_queue_id: "q1", active: true };

function stubApi(overrides: Record<string, unknown> & { permissions?: string[] } = {}) {
  const { permissions = ALL, ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listSlaPolicies: vi.fn().mockResolvedValue([{ id: "p1", name: "Standard", active: true }]),
    listTicketCategories: vi.fn().mockResolvedValue([CATEGORY]),
    listEmailMailboxes: vi.fn().mockResolvedValue([{ id: "m1" }]),
    getHealthSettings: vi.fn().mockResolvedValue({ alert_tickets_enabled: true }),
    listNotificationRules: vi.fn().mockResolvedValue([{ id: "r1", event_type: "ticket.assigned", active: true }]),
    ...rest,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderPanel(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

async function settled(client: QueryClient) {
  await waitFor(() => expect(client.isFetching()).toBe(0));
  await waitFor(() => expect(client.getQueryState(["me"])?.status).toBe("success"));
}

async function step(label: string) {
  const item = (await screen.findByText(label)).closest("li");
  if (!item) throw new Error(`no step for ${label}`);
  return item;
}

describe("TicketReadiness", () => {
  it("sends an org with no active SLA policy to Service levels", async () => {
    stubApi({ listSlaPolicies: vi.fn().mockResolvedValue([{ id: "p1", name: "Old", active: false }]) });
    renderPanel(<TicketReadiness />);

    const sla = await step("Response and resolution targets exist");
    expect(sla).toHaveTextContent(/every ticket shows No SLA/);
    expect(within(sla).getByRole("link", { name: "Set response targets" })).toHaveAttribute("href", "/sla");
  });

  it("names the category with no default queue", async () => {
    stubApi({
      listTicketCategories: vi.fn().mockResolvedValue([CATEGORY, { ...CATEGORY, id: "c2", name: "Access", default_queue_id: null }]),
    });
    renderPanel(<TicketReadiness />);

    const queue = await step("Every category has a default queue");
    expect(queue).toHaveTextContent("Access has no default queue");
    expect(within(queue).getByRole("link", { name: "Set a default queue" })).toHaveAttribute(
      "href",
      "/service-desk/admin",
    );
  });

  it("links mailbox, alert tickets and notification rules to where each is set up", async () => {
    stubApi({
      listEmailMailboxes: vi.fn().mockResolvedValue([]),
      getHealthSettings: vi.fn().mockResolvedValue({ alert_tickets_enabled: false }),
      listNotificationRules: vi.fn().mockResolvedValue([]),
    });
    renderPanel(<TicketReadiness />);

    expect(
      within(await step("A support mailbox turns mail into tickets")).getByRole("link", {
        name: "Connect a support mailbox",
      }),
    ).toHaveAttribute("href", "/email-intake#mailboxes");
    expect(
      within(await step("Alerts can open tickets")).getByRole("link", { name: "Let alerts open tickets" }),
    ).toHaveAttribute("href", "/settings?tab=health");
    expect(
      within(await step("People are told about ticket activity")).getByRole("link", {
        name: "Notify people about tickets",
      }),
    ).toHaveAttribute("href", "/notifications#rules");
  });

  it("is not drawn once everything is in place", async () => {
    stubApi();
    const client = renderPanel(<TicketReadiness />);
    await settled(client);
    expect(screen.queryByText("Before tickets are answered on time")).not.toBeInTheDocument();
  });

  it("says SLA cannot be checked without its permission, and asks nothing", async () => {
    const api = stubApi({ permissions: ALL.filter((code) => code !== "sla.read") });
    renderPanel(<TicketReadiness />);

    expect(await step("Response and resolution targets exist")).toHaveTextContent(
      "This cannot be checked from this account",
    );
    expect(api.listSlaPolicies).not.toHaveBeenCalled();
  });
});

describe("TicketSlaPanel — a ticket with no SLA says where targets are set", () => {
  const NO_SLA = { ticket_id: "t1", has_sla: false, response_state: "none", resolution_state: "none" };

  it("links an account that can open Service levels there", async () => {
    stubApi({ getTicketSla: vi.fn().mockResolvedValue(NO_SLA) });
    renderPanel(<TicketSlaPanel ticketId="t1" />);

    expect(await screen.findByRole("link", { name: "Set response targets" })).toHaveAttribute("href", "/sla");
  });

  it("tells anyone else who sets them, without a link they cannot follow", async () => {
    stubApi({ permissions: ["ticket.read"], getTicketSla: vi.fn().mockResolvedValue(NO_SLA) });
    renderPanel(<TicketSlaPanel ticketId="t1" />);

    expect(await screen.findByText(/set by an administrator under Service levels/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Set response targets" })).not.toBeInTheDocument();
  });
});
