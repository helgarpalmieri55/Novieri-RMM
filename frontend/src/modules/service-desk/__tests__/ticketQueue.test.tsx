/**
 * The queue: the SLA "Due" column, the due-soonest sort and the scope switch
 * (My tickets / Unassigned / All open / All). Scope and sort are answered by
 * the server, so the fake route below honours them the way the API does.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Me, Page } from "../../../lib/api";
import { MyWorkPage } from "../MyWorkPage";
import { TicketListPage } from "../TicketListPage";
import type { TicketListFilters, TicketOut, TicketSlaSummary } from "../types";

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

function sla(overrides: Partial<TicketSlaSummary>): TicketSlaSummary {
  return {
    response_due_at: at(120),
    resolution_due_at: at(600),
    response_state: "running",
    resolution_state: "running",
    next_due_at: at(120),
    next_due_target: "response",
    breached: false,
    paused: false,
    ...overrides,
  };
}

function ticket(id: string, overrides: Partial<TicketOut> = {}): TicketOut {
  return {
    id,
    client_id: "o1",
    number: `INC-${id}`,
    type: "incident",
    category_id: "cat1",
    queue_id: "q1",
    status: "open",
    priority: "normal",
    subject: `Ticket ${id}`,
    description: "",
    requester_id: "req-1",
    assigned_to: null,
    device_id: null,
    origin: "technician",
    source_alert_id: null,
    resolved_at: null,
    closed_at: null,
    has_attachments: false,
    created_by: "tech-1",
    created_at: at(-60),
    updated_at: at(-60),
    sla: null,
    ...overrides,
  };
}

const MINE = ticket("1", { assigned_to: "tech-1", sla: sla({ next_due_at: at(120) }) });
const FREE = ticket("2", {
  sla: sla({ next_due_at: at(-30), breached: true, response_state: "breached" }),
});
const THEIRS = ticket("3", { assigned_to: "tech-2" });
const MINE_CLOSED = ticket("4", { assigned_to: "tech-1", status: "closed" });
const ALL = [MINE, FREE, THEIRS, MINE_CLOSED];

const TERMINAL = new Set(["closed", "cancelled", "rejected"]);

/** `GET /service-desk/tickets`, honouring scope and assignee as the API does. */
function fakeListTickets(rows: TicketOut[]) {
  return vi.fn(async (filters: TicketListFilters = {}): Promise<Page<TicketOut>> => {
    const scope = filters.scope ?? "all";
    const matched = rows.filter((t) => {
      if (scope !== "all" && TERMINAL.has(t.status)) return false;
      if (scope === "mine" && t.assigned_to !== ME.user_id) return false;
      if (scope === "unassigned" && t.assigned_to !== null) return false;
      if (filters.assigned_to && t.assigned_to !== filters.assigned_to) return false;
      return true;
    });
    return { items: matched, total: matched.length };
  });
}

function api(listTickets = fakeListTickets(ALL)): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(ME),
    listTicketQueues: vi.fn().mockResolvedValue([]),
    listTicketCategories: vi.fn().mockResolvedValue([]),
    listTickets,
    listUsers: vi.fn().mockResolvedValue({
      users: [
        { id: "tech-1", email: "t@x", display_name: "Tessa Technician", status: "active", roles: [] },
        { id: "tech-2", email: "o@x", display_name: "Omar Other", status: "active", roles: [] },
      ],
    }),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  };
}

function renderAt(node: ReactElement, path: string, at = path) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path={path.split("?")[0]} element={node} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const lastFilters = (fn: ReturnType<typeof fakeListTickets>): TicketListFilters =>
  fn.mock.calls[fn.mock.calls.length - 1]?.[0] ?? {};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the ticket queue", () => {
  it("opens on every open ticket, soonest deadline first", async () => {
    const listTickets = fakeListTickets(ALL);
    mockUseApi.mockReturnValue(api(listTickets) as ApiClient);
    renderAt(<TicketListPage />, "/service-desk");

    await screen.findByRole("link", { name: "INC-1" });
    expect(lastFilters(listTickets)).toMatchObject({ scope: "open", sort: "due", limit: 200 });
    const scopes = screen.getByRole("group", { name: "Show" });
    expect(within(scopes).getByRole("button", { name: "All open" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByLabelText("Sort")).toHaveValue("due");
    expect(screen.queryByRole("link", { name: "INC-4" })).not.toBeInTheDocument();
  });

  it("shows when each ticket is due, relative to now", async () => {
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderAt(<TicketListPage />, "/service-desk");

    const mine = (await screen.findByRole("link", { name: "INC-1" })).closest("tr") as HTMLElement;
    expect(within(mine).getByText("Due in 2 h")).toBeInTheDocument();
    expect(within(mine).getByText("First response")).toBeInTheDocument();
    const free = screen.getByRole("link", { name: "INC-2" }).closest("tr") as HTMLElement;
    expect(within(free).getByText("Breached 30 min ago")).toBeInTheDocument();
    const theirs = screen.getByRole("link", { name: "INC-3" }).closest("tr") as HTMLElement;
    expect(within(theirs).getByText("No SLA")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Due" })).toBeInTheDocument();
  });

  it.each([
    ["My tickets", "mine", ["INC-1"]],
    ["Unassigned", "unassigned", ["INC-2"]],
    ["All", "all", ["INC-1", "INC-2", "INC-3", "INC-4"]],
  ] as const)("narrows to %s on the server", async (label, scope, visible) => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const listTickets = fakeListTickets(ALL);
    mockUseApi.mockReturnValue(api(listTickets) as ApiClient);
    renderAt(<TicketListPage />, "/service-desk");

    await screen.findByRole("link", { name: "INC-1" });
    await user.click(within(screen.getByRole("group", { name: "Show" })).getByRole("button", { name: label }));

    await waitFor(() => expect(lastFilters(listTickets).scope).toBe(scope));
    for (const number of ["INC-1", "INC-2", "INC-3", "INC-4"]) {
      if (visible.includes(number as never)) {
        expect(await screen.findByRole("link", { name: number })).toBeInTheDocument();
      } else {
        await waitFor(() =>
          expect(screen.queryByRole("link", { name: number })).not.toBeInTheDocument(),
        );
      }
    }
  });

  it("sorts newest first on request", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const listTickets = fakeListTickets(ALL);
    mockUseApi.mockReturnValue(api(listTickets) as ApiClient);
    renderAt(<TicketListPage />, "/service-desk");

    await screen.findByRole("link", { name: "INC-1" });
    await user.selectOptions(screen.getByLabelText("Sort"), "newest");
    await waitFor(() => expect(lastFilters(listTickets).sort).toBe("newest"));
  });

  it("widens My tickets to All open when a person is picked", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const listTickets = fakeListTickets(ALL);
    mockUseApi.mockReturnValue(api(listTickets) as ApiClient);
    renderAt(<TicketListPage />, "/service-desk", "/service-desk?scope=mine");

    await screen.findByRole("link", { name: "INC-1" });
    await screen.findByRole("option", { name: "Omar Other" });
    await user.selectOptions(screen.getByLabelText("Assignee"), "tech-2");

    await waitFor(() =>
      expect(lastFilters(listTickets)).toMatchObject({ scope: "open", assigned_to: "tech-2" }),
    );
    expect(await screen.findByRole("link", { name: "INC-3" })).toBeInTheDocument();
  });

  it("points the Unassigned tile at the Unassigned scope", async () => {
    mockUseApi.mockReturnValue(api() as ApiClient);
    renderAt(<TicketListPage />, "/service-desk");

    await screen.findByRole("link", { name: "INC-1" });
    const tile = [...document.querySelectorAll<HTMLAnchorElement>(".stat-card")].find(
      (card) => card.querySelector(".stat-label")?.textContent === "Unassigned",
    );
    expect(tile).toHaveAttribute("href", "/service-desk?scope=unassigned");
  });
});

describe("My work", () => {
  it("lists my open tickets, soonest deadline first, and links to the same scope", async () => {
    const listTickets = fakeListTickets(ALL);
    mockUseApi.mockReturnValue(api(listTickets) as ApiClient);
    renderAt(<MyWorkPage />, "/service-desk/my-work");

    expect(await screen.findByRole("link", { name: "INC-1" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "INC-4" })).not.toBeInTheDocument();
    expect(lastFilters(listTickets)).toMatchObject({ scope: "mine", sort: "due" });
    const tile = [...document.querySelectorAll<HTMLAnchorElement>(".stat-card")].find(
      (card) => card.querySelector(".stat-label")?.textContent === "Assigned to you",
    );
    expect(tile).toHaveAttribute("href", "/service-desk?scope=mine");
  });
});
