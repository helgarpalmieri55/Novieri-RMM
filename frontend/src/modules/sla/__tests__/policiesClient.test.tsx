/**
 * WHOSE AN SLA POLICY IS (plan 2026-10-26 D2; phase 3 design §3.1, §3.4;
 * phase 3 final review C, Important 2).
 *
 * A policy is one client's -- its tickets resolve among its own policies
 * first -- or, with no client, every client's (a global row). The console
 * used to make every policy global and showed no client, so a client's policy
 * could not be made and looked global in the list. A global policy's writes
 * go without the top bar's client: narrowed, the server refuses them.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Me, MeClient } from "../../../lib/api";
import { PoliciesPanel } from "../PoliciesPanel";
import type { CalendarOut, PolicyOut } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const ADMIN_CODES = ["sla.read", "sla.admin"];
const INTERNAL: MeClient = { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: ADMIN_CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ADMIN_CODES };

function selectClient(selected: string | null, clients: MeClient[] = [INTERNAL, ACME]): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients, ready: true });
}

beforeEach(() => selectClient("c-acme"));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "admin@example.com",
  display_name: "Admin",
  permissions: ADMIN_CODES,
  permissions_everywhere: ADMIN_CODES,
  clients: [INTERNAL, ACME],
};

const CALENDAR: CalendarOut = {
  id: "cal1",
  name: "UK business hours",
  timezone: "Europe/London",
  active: true,
  health: "ok",
  created_at: "2026-01-01T00:00:00.000Z",
};

function policy(overrides: Partial<PolicyOut>): PolicyOut {
  return {
    id: "p",
    client_id: null,
    queue_id: null,
    priority: null,
    calendar_id: "cal1",
    response_target_minutes: 60,
    resolution_target_minutes: 480,
    escalate_priority: false,
    escalate_to_queue_id: null,
    active: true,
    created_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

const GLOBAL_POLICY = policy({ id: "p-global", priority: "urgent" });
const ACME_POLICY = policy({ id: "p-acme", client_id: "c-acme", priority: "high" });

function stubApi(overrides: Partial<Record<keyof ApiClient, ReturnType<typeof vi.fn>>> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listSlaCalendars: vi.fn().mockResolvedValue([CALENDAR]),
    listSlaPolicies: vi.fn().mockResolvedValue([GLOBAL_POLICY, ACME_POLICY]),
    listTicketQueues: vi.fn().mockResolvedValue([]),
    createSlaPolicy: vi.fn().mockResolvedValue(ACME_POLICY),
    updateSlaPolicy: vi.fn().mockResolvedValue(ACME_POLICY),
    deleteSlaPolicy: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api);
  return api;
}

function renderPanel(node: ReactNode = <PoliciesPanel canAdmin queueName={() => "Helpdesk"} />) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/sla"]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The row whose priority pill reads `priority`. */
async function rowOf(priority: string): Promise<HTMLElement> {
  const pill = await screen.findByText(priority);
  return pill.closest("tr") as HTMLElement;
}

describe("an existing policy says whether it is every client's", () => {
  it.each([
    { label: "every client's", priority: "Urgent", id: "p-global", global: true },
    { label: "a client's", priority: "High", id: "p-acme", global: false },
  ])("edits and deletes $label policy with global: $global", async ({ priority, id, global }) => {
    const api = stubApi();
    const user = userEvent.setup();
    renderPanel();

    await user.click(within(await rowOf(priority)).getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit policy" });
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.updateSlaPolicy).toHaveBeenCalledTimes(1));
    expect(api.updateSlaPolicy.mock.calls[0]?.[0]).toBe(id);
    expect(api.updateSlaPolicy.mock.calls[0]?.[2]).toEqual({ global });

    await user.click(within(await rowOf(priority)).getByRole("button", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: "Delete policy" }));
    await waitFor(() => expect(api.deleteSlaPolicy).toHaveBeenCalledWith(id, { global }));
  });
});

/* ------------------------------------------------------------ a new policy */

async function openAdd(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  await user.click(await screen.findByRole("button", { name: "Add policy" }));
  return screen.findByRole("dialog", { name: "Add policy" });
}

async function pickCalendar(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement): Promise<void> {
  await user.click(within(dialog).getByRole("combobox", { name: "Calendar" }));
  await user.click(await within(dialog).findByRole("option", { name: /UK business hours/ }));
}

describe("a new policy is one client's, or every client's when asked (§3.1)", () => {
  it("files it under the client chosen at the top, which is not the organisation's own", async () => {
    const api = stubApi();
    const user = userEvent.setup();
    renderPanel();
    const dialog = await openAdd(user);
    expect(within(dialog).queryByLabelText("Client")).toBeNull();
    await pickCalendar(user, dialog);
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.createSlaPolicy).toHaveBeenCalledTimes(1));
    expect(api.createSlaPolicy.mock.calls[0]?.[0]).toMatchObject({ client_id: "c-acme", calendar_id: "cal1" });
  });

  it("files it for every client, with no client, when asked", async () => {
    const api = stubApi();
    const user = userEvent.setup();
    renderPanel();
    const dialog = await openAdd(user);
    await pickCalendar(user, dialog);
    const toggle = await within(dialog).findByRole("checkbox", { name: /For every client/ });
    expect(toggle.closest("label")).toHaveTextContent(
      "Ticked, it measures the tickets of every client that has no active policy of its own. Unticked, only the tickets of the client it is filed under.",
    );
    await user.click(toggle);
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.createSlaPolicy).toHaveBeenCalledTimes(1));
    expect(api.createSlaPolicy.mock.calls[0]?.[0]).not.toHaveProperty("client_id");
  });

  it("with every client chosen, asks for the client, offering only those holding sla.admin, never Unassigned", async () => {
    const reader: MeClient = { id: "c-beta", name: "Beta", kind: "customer", enabled_modules: [], permissions: ["sla.read"] };
    const unassigned: MeClient = { id: "c-unassigned", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: ADMIN_CODES };
    selectClient(null, [INTERNAL, ACME, reader, unassigned]);
    const api = stubApi();
    const user = userEvent.setup();
    renderPanel();
    const dialog = await openAdd(user);
    const field = within(dialog).getByLabelText("Client");
    expect(within(field).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Choose a client",
      "Novieri",
      "Acme",
    ]);

    await pickCalendar(user, dialog);
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Client is required");
    expect(api.createSlaPolicy).not.toHaveBeenCalled();

    await user.selectOptions(field, "c-acme");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.createSlaPolicy).toHaveBeenCalledTimes(1));
    expect(api.createSlaPolicy.mock.calls[0]?.[0]).toMatchObject({ client_id: "c-acme" });
  });

  it("with Unassigned chosen at the top, says so instead of offering Save, until it is for every client", async () => {
    const unassigned: MeClient = { id: "c-unassigned", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: ADMIN_CODES };
    selectClient(unassigned.id, [INTERNAL, unassigned]);
    const api = stubApi();
    const user = userEvent.setup();
    renderPanel();
    const dialog = await openAdd(user);
    await pickCalendar(user, dialog);
    expect(
      within(dialog).getByText(
        "Unassigned is chosen in the top bar, and nothing new can be filed under it. Choose another client, or All my clients, to save this.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Save" })).toBeNull();

    await user.click(within(dialog).getByRole("checkbox", { name: /For every client/ }));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.createSlaPolicy).toHaveBeenCalledTimes(1));
    expect(api.createSlaPolicy.mock.calls[0]?.[0]).not.toHaveProperty("client_id");
  });
});

/* ------------------------------------------------------- the Client column */

describe("the list says whose each policy is (§3.4)", () => {
  it("with every client chosen, names each policy's client, and every client's as All clients", async () => {
    selectClient(null);
    stubApi();
    renderPanel();
    expect(await screen.findByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(await rowOf("Urgent")).getByText("All clients")).toBeInTheDocument();
    expect(within(await rowOf("High")).getByText("Acme")).toBeInTheDocument();
  });

  it("hides the column with one client chosen", async () => {
    stubApi();
    renderPanel();
    await rowOf("Urgent");
    expect(screen.queryByRole("columnheader", { name: "Client" })).toBeNull();
  });
});
