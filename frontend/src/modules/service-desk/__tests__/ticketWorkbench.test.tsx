/**
 * The ticket page as a workbench: a header that states the deadlines, who
 * raised it and which machine it is about; quick actions (Assign to me,
 * Reply, Add internal note, Resolve, Reply & resolve); one edit form with one
 * Save; saved replies and article suggestions in the reply box.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Me } from "../../../lib/api";
import { TicketDetailPage } from "../TicketDetailPage";
import type { CannedResponseOut, QueueOut, TicketOut, TicketSlaSummary } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const NOW = Date.parse("2026-09-23T12:00:00Z");
const at = (minutes: number) => new Date(NOW + minutes * 60_000).toISOString();

const ME: Me = {
  user_id: "tech-1",
  org_id: "o1",
  email: "tech@example.com",
  display_name: "Tessa Technician",
  permissions: ["ticket.read", "ticket.write", "ticket.assign"],
};

const QUEUES: QueueOut[] = [
  { id: "q1", key: "helpdesk", name: "Helpdesk", description: "", active: true, sort_order: 0, created_by: "a", created_at: at(-9999) },
  { id: "q2", key: "network", name: "Network", description: "", active: true, sort_order: 1, created_by: "a", created_at: at(-9999) },
];

const SLA: TicketSlaSummary = {
  response_due_at: at(120),
  resolution_due_at: at(-45),
  response_state: "running",
  resolution_state: "running",
  next_due_at: at(-45),
  next_due_target: "resolution",
  breached: false,
  paused: false,
};

const TICKET: TicketOut = {
  id: "t1",
  client_id: "o1",
  number: "INC-1",
  type: "incident",
  category_id: "cat1",
  queue_id: "q1",
  status: "open",
  priority: "normal",
  subject: "Printer jams on tray 2",
  description: "It jams every time.",
  requester_id: "req-1",
  assigned_to: null,
  device_id: "dev-1",
  origin: "portal",
  source_alert_id: null,
  resolved_at: null,
  closed_at: null,
  has_attachments: false,
  created_by: "req-1",
  created_at: at(-120),
  updated_at: at(-60),
  sla: SLA,
};

const CANNED: CannedResponseOut[] = [
  {
    id: "cr1",
    title: "Restart first",
    body: "Please restart the printer and tell us if it happens again.",
    created_by: "a",
    created_at: at(-999),
    updated_by: null,
    updated_at: null,
  },
];

function makeApi(ticket: TicketOut = TICKET, me: Me = ME): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(me),
    getTicket: vi.fn().mockResolvedValue(ticket),
    listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
    listTicketCategories: vi.fn().mockResolvedValue([
      {
        id: "cat1", key: "hw", name: "Hardware", type: "incident", requires_approval: false,
        approver_role_id: null, default_queue_id: "q1", active: true, sort_order: 0,
        created_by: "a", created_at: at(-999),
      },
    ]),
    listTicketComments: vi.fn().mockResolvedValue([]),
    listTicketAttachments: vi.fn().mockResolvedValue([]),
    listTicketStatusHistory: vi.fn().mockResolvedValue([]),
    listUsers: vi.fn().mockResolvedValue({
      users: [
        { id: "tech-1", email: "t@x", display_name: "Tessa Technician", status: "active", roles: [] },
        { id: "req-1", email: "r@x", display_name: "Rachel Requester", status: "active", roles: [] },
      ],
    }),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    getDeviceInventory: vi.fn().mockResolvedValue({
      summary: { hostname: "WKS-042" }, hardware: [], software: [], facts: {}, snapshot_taken_at: null,
    }),
    getDevicePatchStatus: vi.fn().mockResolvedValue({
      device_id: "dev-1", counts_by_state: {}, counts_by_severity: {}, pending_reboot: false, last_scan_at: null,
    }),
    getTicketSla: vi.fn().mockResolvedValue({
      ticket_id: "t1", has_sla: false, response_state: "none", resolution_state: "none",
    }),
    listCannedResponses: vi.fn().mockResolvedValue(CANNED),
    suggestDocArticles: vi.fn().mockResolvedValue([]),
    personForUser: vi.fn().mockResolvedValue({ id: "person-9" }),
    createTicketComment: vi.fn().mockResolvedValue({}),
    changeTicketStatus: vi.fn().mockResolvedValue(ticket),
    resolveTicket: vi.fn().mockResolvedValue({ ...ticket, status: "resolved" }),
    assignTicket: vi.fn().mockResolvedValue(ticket),
    updateTicket: vi.fn().mockResolvedValue(ticket),
  };
}

let api: Partial<ApiClient>;

function renderTicket(overrides: Partial<ApiClient> = {}) {
  api = { ...api, ...overrides };
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/service-desk/tickets/t1"]}>
        <Routes>
          <Route path="/service-desk/tickets/:ticketId" element={<TicketDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const header = async () => {
  await screen.findByText("INC-1: Printer jams on tray 2");
  return document.querySelector(".facts") as HTMLElement;
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  api = makeApi();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the ticket header", () => {
  it("states both SLA clocks, the requester and the device", async () => {
    renderTicket();
    const facts = await header();

    expect(within(facts).getByText("Due in 2 h")).toBeInTheDocument();
    expect(within(facts).getByText("Overdue by 45 min")).toBeInTheDocument();
    expect(await within(facts).findByRole("link", { name: /WKS-042/ })).toHaveAttribute(
      "href",
      "/devices/dev-1",
    );
    expect(within(facts).getByRole("button", { name: "Remote in" })).toBeInTheDocument();
    expect(within(facts).getByText("Nobody yet")).toBeInTheDocument();
  });

  it("links the requester to their Person page when they have one", async () => {
    api = makeApi(TICKET, { ...ME, permissions: [...ME.permissions, "people.read"] });
    renderTicket();
    const facts = await header();

    const link = await within(facts).findByRole("link", { name: /Rachel Requester/ });
    expect(link).toHaveAttribute("href", "/people/person-9");
    expect(api.personForUser).toHaveBeenCalledWith("req-1");
  });

  it("names the requester without a link when people cannot be read", async () => {
    renderTicket();
    const facts = await header();

    expect(await within(facts).findByText("Rachel Requester")).toBeInTheDocument();
    expect(within(facts).queryByRole("link", { name: /Rachel Requester/ })).toBeNull();
    expect(api.personForUser).not.toHaveBeenCalled();
  });

  it("says an alert raised a ticket that has no requester", async () => {
    api = makeApi({ ...TICKET, requester_id: null, origin: "alert", created_by: null });
    renderTicket();
    const facts = await header();

    expect(within(facts).getByText("None: an alert raised this")).toBeInTheDocument();
  });

  it("says No SLA when no policy applies", async () => {
    api = makeApi({ ...TICKET, sla: null });
    renderTicket();
    const facts = await header();

    expect(within(facts).getAllByText("No SLA")).toHaveLength(2);
  });
});

describe("quick actions", () => {
  it("assigns the ticket to the signed-in person in one click", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderTicket();
    await header();

    await user.click(screen.getByRole("button", { name: "Assign to me" }));
    expect(api.assignTicket).toHaveBeenCalledWith("t1", { assigned_to: "tech-1" });
  });

  it("does not offer Assign to me on a ticket that is already mine", async () => {
    api = makeApi({ ...TICKET, assigned_to: "tech-1" });
    renderTicket();
    await header();

    expect(screen.queryByRole("button", { name: "Assign to me" })).toBeNull();
  });

  it("resolves a new incident in one server call, which opens it first", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    api = makeApi({ ...TICKET, status: "new" });
    renderTicket();
    await header();

    await user.click(screen.getByRole("button", { name: "Resolve" }));

    await waitFor(() => expect(api.resolveTicket).toHaveBeenCalledWith("t1", {}));
    expect(api.resolveTicket).toHaveBeenCalledTimes(1);
    expect(api.changeTicketStatus).not.toHaveBeenCalled();
  });

  it("does not offer Resolve where the lifecycle forbids it", async () => {
    api = makeApi({ ...TICKET, status: "resolved" });
    renderTicket();
    await header();

    expect(screen.queryByRole("button", { name: "Resolve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reply & resolve" })).toBeNull();
  });

  it("hides every write action from a read-only viewer", async () => {
    api = makeApi(TICKET, { ...ME, permissions: ["ticket.read"] });
    renderTicket();
    await header();

    for (const name of ["Assign to me", "Reply", "Add internal note", "Resolve"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(screen.queryByLabelText("Message")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
  });

  it("switches the reply box to an internal note from the header", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderTicket();
    await header();

    expect(screen.getByRole("radio", { name: /Public reply/ })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Add internal note" }));
    expect(screen.getByRole("radio", { name: /Internal note/ })).toBeChecked();
    expect(screen.getByLabelText("Message")).toHaveFocus();
  });
});

describe("the reply box", () => {
  it("defaults to an internal note on a ticket nobody raised", async () => {
    api = makeApi({ ...TICKET, requester_id: null, origin: "alert", created_by: null });
    renderTicket();
    await header();

    expect(screen.getByRole("radio", { name: /Internal note/ })).toBeChecked();
    expect(screen.getByText(/there is no one to reply to/)).toBeInTheDocument();
  });

  it("defaults to a public reply, labelled as one, when there is a requester", async () => {
    renderTicket();
    await header();

    const publicChoice = screen.getByRole("radio", { name: /Public reply/ });
    expect(publicChoice).toBeChecked();
    expect(publicChoice.closest("label")).toHaveTextContent("The requester sees this.");
  });

  it("replies and resolves in one server call", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderTicket();
    await header();

    await user.type(screen.getByLabelText("Message"), "Fixed the rollers.");
    await user.click(screen.getByRole("button", { name: "Reply & resolve" }));

    await waitFor(() =>
      expect(api.resolveTicket).toHaveBeenCalledWith("t1", {
        reply: { body: "Fixed the rollers.", visibility: "public" },
      }),
    );
    expect(api.createTicketComment).not.toHaveBeenCalled();
    expect(api.changeTicketStatus).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByLabelText("Message")).toHaveValue(""));
  });

  it("sends an internal note with the resolve when the note is chosen", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderTicket();
    await header();

    await user.click(screen.getByRole("radio", { name: /Internal note/ }));
    await user.type(screen.getByLabelText("Message"), "Swapped the fuser.");
    await user.click(screen.getByRole("button", { name: "Add note & resolve" }));

    await waitFor(() =>
      expect(api.resolveTicket).toHaveBeenCalledWith("t1", {
        reply: { body: "Swapped the fuser.", visibility: "internal" },
      }),
    );
  });

  it("says nothing was sent when Reply & resolve fails, and keeps the message", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderTicket({ resolveTicket: vi.fn().mockRejectedValue(new Error("down")) });
    await header();

    await user.type(screen.getByLabelText("Message"), "Fixed.");
    await user.click(screen.getByRole("button", { name: "Reply & resolve" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Nothing was sent and the ticket was not resolved.");
    expect(alert).not.toHaveTextContent(/reply was posted/i);
    expect(screen.getByLabelText("Message")).toHaveValue("Fixed.");
    expect(api.createTicketComment).not.toHaveBeenCalled();
  });

  it("inserts a saved reply into the message", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderTicket();
    await header();

    await user.selectOptions(await screen.findByLabelText("Insert a saved reply"), "cr1");
    expect(screen.getByLabelText("Message")).toHaveValue(
      "Please restart the printer and tell us if it happens again.",
    );
  });

  it("suggests articles from the subject to someone who can read the docs", async () => {
    api = makeApi(TICKET, { ...ME, permissions: [...ME.permissions, "docs.read"] });
    renderTicket({
      suggestDocArticles: vi
        .fn()
        .mockResolvedValue([{ id: "a1", title: "Printer jams", category: "Runbooks" }]),
    });
    await header();

    const link = await screen.findByRole("link", { name: "Printer jams" });
    expect(link).toHaveAttribute("href", "/docs?article=a1");
    expect(api.suggestDocArticles).toHaveBeenCalledWith("Printer jams on tray 2");
  });

  it("does not ask for articles without docs.read", async () => {
    renderTicket();
    await header();

    expect(screen.queryByText("Articles that may help")).toBeNull();
    expect(api.suggestDocArticles).not.toHaveBeenCalled();
  });
});

describe("one edit form", () => {
  it("saves fields, assignment and status with one Save, fields first and status last", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const order: string[] = [];
    renderTicket({
      updateTicket: vi.fn(async () => {
        order.push("fields");
        return TICKET;
      }),
      assignTicket: vi.fn(async () => {
        order.push("assign");
        return TICKET;
      }),
      changeTicketStatus: vi.fn(async () => {
        order.push("status");
        return TICKET;
      }),
    });
    await header();

    const save = screen.getByRole("button", { name: "Save changes" });
    expect(save).toBeDisabled();

    await user.selectOptions(screen.getByLabelText("Priority"), "high");
    await user.selectOptions(screen.getByLabelText("Queue"), "q2");
    await user.selectOptions(screen.getByLabelText("Status"), "pending");
    await user.click(save);

    await waitFor(() => expect(order).toEqual(["fields", "assign", "status"]));
    expect(api.updateTicket).toHaveBeenCalledWith("t1", { priority: "high" });
    expect(api.assignTicket).toHaveBeenCalledWith("t1", { queue_id: "q2" });
    expect(api.changeTicketStatus).toHaveBeenCalledWith("t1", { to_status: "pending", reason: "" });
  });

  it("reopens a closed ticket before saving its fields", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const order: string[] = [];
    api = makeApi({ ...TICKET, status: "closed" });
    renderTicket({
      changeTicketStatus: vi.fn(async () => {
        order.push("status");
        return TICKET;
      }),
      assignTicket: vi.fn(async () => {
        order.push("assign");
        return TICKET;
      }),
    });
    await header();

    // Frozen fields cannot be edited while closed; assignment and status can.
    expect(screen.getByLabelText("Priority")).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Status"), "open");
    await user.selectOptions(screen.getByLabelText("Queue"), "q2");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(order).toEqual(["status", "assign"]));
  });

  it("shows the Remote in dialog's entry point only when a device is linked", async () => {
    api = makeApi({ ...TICKET, device_id: null });
    renderTicket();
    await header();

    expect(screen.queryByRole("button", { name: "Remote in" })).toBeNull();
  });
});
