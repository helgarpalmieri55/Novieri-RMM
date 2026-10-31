import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient, MeClient } from "../../../lib/api";
import type { QueueOut } from "../../service-desk/types";
import { PolicyResolverPanel } from "../PolicyResolverPanel";
import type { PolicyOut } from "../types";

// MSP clients phase 3, design §3.5: the preview resolves for the client chosen
// at the top, with phase 2's D2 rule -- a client with active policies of its
// own resolves among those alone; with every client chosen it previews the
// global policies, and says so (plan Task 11 D11.4).

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ["sla.read"] };

function policy(overrides: Partial<PolicyOut>): PolicyOut {
  return {
    id: "p", client_id: null, queue_id: null, priority: null, calendar_id: "cal1",
    response_target_minutes: 60, resolution_target_minutes: 480, escalate_priority: false,
    escalate_to_queue_id: null, active: true, created_at: "2026-01-01T00:00:00.000Z", ...overrides,
  };
}

const POLICIES = [
  policy({ id: "global-helpdesk-urgent", queue_id: "q1", priority: "urgent" }),
  policy({ id: "acme-catch-all", client_id: "c-acme" }),
];

function renderPanel(selected: string | null) {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [ACME], ready: true });
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue({ user_id: "u", org_id: "o1", email: "a@b.c", display_name: "A", permissions: ["sla.read", "ticket.read"], clients: [ACME] }),
    listSlaPolicies: vi.fn().mockResolvedValue(POLICIES),
    listSlaCalendars: vi.fn().mockResolvedValue([]),
    listTicketQueues: vi.fn().mockResolvedValue([{ id: "q1", name: "Helpdesk" } as QueueOut]),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/sla"]}>
        <PolicyResolverPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function choose(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("option", { name: "Helpdesk" });
  await user.selectOptions(screen.getByLabelText("Queue"), "q1");
  await user.selectOptions(screen.getByLabelText("Priority"), "urgent");
}

describe("the SLA preview's client", () => {
  it("resolves among the selected client's own policies", async () => {
    const user = userEvent.setup();
    renderPanel("c-acme");
    await choose(user);
    expect(await screen.findByText(/Resolved at level 4/)).toBeInTheDocument();
    expect(screen.getByText("Previewing for Acme.")).toBeInTheDocument();
  });

  it("previews the global policies with every client chosen, and says so", async () => {
    const user = userEvent.setup();
    renderPanel(null);
    await choose(user);
    expect(await screen.findByText(/Resolved at level 1/)).toBeInTheDocument();
    expect(screen.getByText("With every client chosen, this previews the global policies.")).toBeInTheDocument();
  });
});
