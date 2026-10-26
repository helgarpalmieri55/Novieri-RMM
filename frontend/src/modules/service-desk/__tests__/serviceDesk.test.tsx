import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Device, DeviceInventory, Me, Page } from "../../../lib/api";
import type { UserOut } from "../../settings/types";
import type { DeviceStatus } from "../../patch/types";
import { MyWorkPage } from "../MyWorkPage";
import { NewTicketDialog } from "../NewTicketDialog";
import { ServiceDeskAdminPage } from "../ServiceDeskAdminPage";
import { TicketDetailPage } from "../TicketDetailPage";
import { TicketListPage } from "../TicketListPage";
import {
  commentVisibilityVariant,
  ticketPriorityVariant,
  ticketStatusVariant,
} from "../serviceDeskState";
import type {
  AttachmentOut,
  CategoryOut,
  CommentOut,
  QueueOut,
  StatusHistoryOut,
  TicketListFilters,
  TicketOut,
} from "../types";

/**
 * `GET /service-desk/tickets` answers `{items, total}` since closeout
 * recommendation 10 -- it returned a bare array, so a full page and a
 * complete queue were indistinguishable and the page could not say
 * "showing X of Y". `total` defaults to the number of rows handed in, i.e.
 * "this IS the whole set"; a test about truncation passes a larger one.
 */
function page(items: TicketOut[], total: number = items.length): Page<TicketOut> {
  return { items, total };
}

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch -- mirrors modules/budget/__tests__/budget.test.tsx.
const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWithProviders(ui: ReactElement, initialEntries: string[] = ["/"]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

// Spec §0.6 / §8.2: technician holds read/write/assign but NOT approve/admin
// -- the inverse of Budget. This is the permission set exercised throughout
// unless a test explicitly overrides it.
const ME_TECHNICIAN: Me = {
  user_id: "tech-1",
  org_id: "o1",
  email: "tech@example.com",
  display_name: "Technician",
  permissions: ["ticket.read", "ticket.write", "ticket.assign"],
};

const ME_FULL: Me = {
  ...ME_TECHNICIAN,
  user_id: "admin-1",
  permissions: ["ticket.read", "ticket.write", "ticket.assign", "ticket.approve", "ticket.admin"],
};

// --------------------------------------------------------------------------
// Directory fixtures (UX remediation cycle 6). `GET /users` and
// `GET /devices` are NOT service-desk routes -- they are the two directories
// the pickers read, and they carry their own server-side permissions.
// --------------------------------------------------------------------------

const USERS: UserOut[] = [
  {
    id: "tech-1",
    email: "tech@example.com",
    display_name: "Tessa Technician",
    status: "active",
    roles: [],
  },
  {
    id: "req-1",
    email: "rachel@example.com",
    display_name: "Rachel Requester",
    status: "active",
    roles: [],
  },
  {
    id: "gone-1",
    email: "gone@example.com",
    display_name: "Gordon Gone",
    status: "disabled",
    roles: [],
  },
];

function device(overrides: Partial<Device> & Pick<Device, "id">): Device {
  return {
    hostname: null,
    fqdn: null,
    os_family: null,
    os_version: "",
    architecture: "",
    device_group_id: null,
    status: "active",
    agent_status: null,
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "agent",
    // An enrolled machine proved its own identity at enrolment; nobody
    // had to vouch for the row (asset-inventory spec §3).
    provenance: "agent",
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...overrides,
  };
}

const DEVICES: Device[] = [
  device({ id: "dev-1", hostname: "WKS-042", os_family: "windows", agent_status: "online" }),
  device({ id: "dev-2", hostname: "BUILD-01", os_family: "linux", agent_status: "offline" }),
];

/**
 * Choose an option from an `EntityPicker`, the way a person does: focus the
 * combobox, then click the row. Deliberately NOT `user.type(...)` into the
 * box -- typing filters, it does not select, and a test that typed a UUID
 * and asserted it was submitted would pass against the very free-text field
 * this cycle removed.
 */
async function pickEntity(
  user: ReturnType<typeof userEvent.setup>,
  pickerLabel: string,
  optionName: RegExp,
) {
  const box = screen.getByLabelText(pickerLabel);
  await user.click(box);
  // SCOPED TO THE PICKER'S OWN LIST. Every picker in this module sits inside a
  // dialog drawn over the ticket list, and that list's Assignee filter is a
  // `<select>` over the same directory -- so a bare `findByRole("option")`
  // matches the filter's `<option>` as well as the picker's row, and reports
  // two elements for one person. The picker owns the listbox its combobox
  // points at (`aria-controls`), which is the only list this helper means.
  const listId = box.getAttribute("aria-controls") ?? "";
  const list = await screen.findByRole("listbox", { name: pickerLabel });
  if (list.id !== listId) throw new Error(`picker "${pickerLabel}" does not own its listbox`);
  await user.click(await within(list).findByRole("option", { name: optionName }));
}

/**
 * The value beside a term in a `KV` definition list.
 *
 * The "Linked device" panel used to be a hand-drawn grid of "Label: value"
 * text lines, so these assertions could match one string. It is the kit's
 * `KV` now -- the same piece the "Details" panel above it uses -- so the term
 * is a `<dt>` and the value the `<dd>` after it, and a test that still looked
 * for "Hostname: WKS-042" would be asserting the markup this port removed
 * rather than the fact it renders.
 */
function kvValue(term: string): HTMLElement {
  const dt = screen.getByText(term, { selector: "dt" });
  const dd = dt.nextElementSibling;
  if (!(dd instanceof HTMLElement) || dd.tagName !== "DD") {
    throw new Error(`no <dd> after the <dt> "${term}"`);
  }
  return dd;
}

const QUEUES: QueueOut[] = [
  {
    id: "q1",
    key: "helpdesk",
    name: "Helpdesk",
    description: "",
    active: true,
    sort_order: 0,
    created_by: "admin-1",
    created_at: "2026-01-01T00:00:00.000Z",
  },
  {
    id: "q2",
    key: "infrastructure",
    name: "Infrastructure",
    description: "",
    active: true,
    sort_order: 1,
    created_by: "admin-1",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const CATEGORIES: CategoryOut[] = [
  {
    id: "cat1",
    key: "hardware",
    name: "Hardware",
    type: "incident",
    requires_approval: false,
    approver_role_id: null,
    default_queue_id: "q1",
    active: true,
    sort_order: 0,
    created_by: "admin-1",
    created_at: "2026-01-01T00:00:00.000Z",
  },
  {
    id: "cat2",
    key: "access_request",
    name: "Access request",
    type: "request",
    requires_approval: true,
    approver_role_id: null,
    default_queue_id: "q1",
    active: true,
    sort_order: 0,
    created_by: "admin-1",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const TICKET_OPEN: TicketOut = {
  id: "t1",
  client_id: "o1",
  number: "INC-1",
  type: "incident",
  category_id: "cat1",
  queue_id: "q1",
  status: "open",
  priority: "normal",
  subject: "Laptop won't boot",
  description: "Black screen on power-on.",
  requester_id: "req-1",
  assigned_to: null,
  device_id: null,
  origin: "technician",
  source_alert_id: null,
  resolved_at: null,
  closed_at: null,
  has_attachments: false,
  created_by: "tech-1",
  created_at: "2026-07-01T00:00:00.000Z",
  updated_at: "2026-07-01T00:00:00.000Z",
};

const TICKET_PENDING_APPROVAL: TicketOut = {
  ...TICKET_OPEN,
  id: "t2",
  number: "REQ-1",
  type: "request",
  category_id: "cat2",
  status: "pending_approval",
};

const TICKET_CLOSED: TicketOut = {
  ...TICKET_OPEN,
  id: "t3",
  number: "INC-2",
  status: "closed",
  closed_at: "2026-07-02T00:00:00.000Z",
};

// C3 fixtures: a ticket with a linked device, and the two device-panel
// endpoints' response shapes (schema.py's DeviceInventoryOut/DeviceStatusOut,
// mirrored by lib/api.ts's DeviceInventory / patch/types.ts's DeviceStatus).
const TICKET_WITH_DEVICE: TicketOut = { ...TICKET_OPEN, id: "t4", number: "INC-3", device_id: "dev-1" };

const DEVICE_INVENTORY: DeviceInventory = {
  summary: { hostname: "WKS-042", os_family: "windows", os_version: "11" },
  hardware: [{ type: "cpu", identifier: "Intel i7", attributes: {} }],
  software: [{ name: "Chrome", version: "120", publisher: "Google", install_date: null }],
  facts: {},
  snapshot_taken_at: "2026-07-01T00:00:00.000Z",
};

const DEVICE_PATCH_STATUS: DeviceStatus = {
  device_id: "dev-1",
  counts_by_state: { missing: 2, installed: 10 },
  counts_by_severity: { critical: 1 },
  pending_reboot: true,
  // `DeviceStatus` has carried the device's own last scan since 2026-08-21 and
  // the interface had never been told; `backend/tests/test_patch_type_parity.py`
  // is what stops that drifting again.
  last_scan_at: "2026-07-01T00:00:00.000Z",
};

const COMMENTS: CommentOut[] = [
  {
    id: "c1",
    ticket_id: "t1",
    author_id: "tech-1",
    author_type: "user",
    body: "Checked the power cable.",
    visibility: "public",
    created_at: "2026-07-01T01:00:00.000Z",
  },
];

const ATTACHMENTS: AttachmentOut[] = [];

const STATUS_HISTORY: StatusHistoryOut[] = [
  {
    id: "h1",
    ticket_id: "t1",
    from_status: null,
    to_status: "new",
    actor_type: "user",
    actor_id: "tech-1",
    reason: "",
    created_at: "2026-07-01T00:00:00.000Z",
  },
  {
    id: "h2",
    ticket_id: "t1",
    from_status: "new",
    to_status: "open",
    actor_type: "user",
    actor_id: "tech-1",
    reason: "Started work",
    created_at: "2026-07-01T00:30:00.000Z",
  },
];

// --------------------------------------------------------------------------
// Navigation reachability (the Phase-12 lesson: a page routed but unlinked
// is not reachable). ServiceDeskNav is ONE shared component rendered by
// every page -- these assertions exercise it via TicketListPage.
// --------------------------------------------------------------------------

describe("ServiceDeskNav reachability", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTickets: vi.fn().mockResolvedValue(page([TICKET_OPEN])),
      listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listUsers: vi.fn().mockResolvedValue({ users: USERS }),
      listDevices: vi.fn().mockResolvedValue({ items: DEVICES, total: DEVICES.length }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("links to every service-desk surface, including Admin, for a full-permission caller", async () => {
    renderWithProviders(<TicketListPage />);

    expect(await screen.findByRole("link", { name: "Tickets" })).toHaveAttribute(
      "href",
      "/service-desk",
    );
    expect(screen.getByRole("link", { name: "My work" })).toHaveAttribute(
      "href",
      "/service-desk/my-work",
    );
    expect(screen.getByRole("link", { name: "Admin" })).toHaveAttribute(
      "href",
      "/service-desk/admin",
    );
  });

  it("hides service-desk navigation entirely without ticket.read", async () => {
    api.me = vi.fn().mockResolvedValue({ ...ME_FULL, permissions: [] });
    renderWithProviders(<TicketListPage />);

    await screen.findByText("Tickets", { selector: "h1" });
    expect(screen.queryByRole("link", { name: "Tickets" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "My work" })).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Service desk" })).not.toBeInTheDocument();
  });

  // Spec §0.6/§11: a technician holds ticket.read/write/assign but not
  // ticket.admin -- the Admin surface (queues/categories) must not be
  // offered to them even though they can reach every other surface.
  it("hides the Admin link for a technician who holds ticket.read but not ticket.admin", async () => {
    api.me = vi.fn().mockResolvedValue(ME_TECHNICIAN);
    renderWithProviders(<TicketListPage />);

    expect(await screen.findByRole("link", { name: "Tickets" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "My work" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
  });

  // The same shared nav renders on MyWorkPage too -- not a second copy.
  it("renders the identical nav on My work", async () => {
    renderWithProviders(<MyWorkPage />);

    expect(await screen.findByRole("link", { name: "Tickets" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Admin" })).toBeInTheDocument();
  });

  // And on the Admin page itself.
  it("renders the identical nav on the Admin page", async () => {
    api.listTicketQueues = vi.fn().mockResolvedValue(QUEUES);
    api.listTicketCategories = vi.fn().mockResolvedValue(CATEGORIES);
    api.listCannedResponses = vi.fn().mockResolvedValue([]);
    renderWithProviders(<ServiceDeskAdminPage />);

    expect(await screen.findByRole("link", { name: "Tickets" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "My work" })).toBeInTheDocument();
  });

  // The ticket detail surface is reached by clicking a row in the list, the
  // same way ChangeDetail is reached from ChangesPage -- exercised here so
  // the route is proven reachable, not merely declared in App.tsx.
  it("reaches ticket detail via a link from the ticket list row", async () => {
    renderWithProviders(<TicketListPage />);

    const link = await screen.findByRole("link", { name: "INC-1" });
    expect(link).toHaveAttribute("href", "/service-desk/tickets/t1");
  });
});

// --------------------------------------------------------------------------
// TicketListPage: the three states a mock never has (UI port spec §6/§8.5).
// Before this migration the page rendered an identical empty table body for
// "still loading", "nothing assigned" and "the request failed" alike.
// --------------------------------------------------------------------------

describe("TicketListPage query states", () => {
  function baseApi(listTickets: ApiClient["listTickets"]): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME_TECHNICIAN),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listTickets,
      listUsers: vi.fn().mockResolvedValue({ users: USERS }),
      listDevices: vi.fn().mockResolvedValue({ items: DEVICES, total: DEVICES.length }),
    };
  }

  it("says it is loading rather than showing an empty ticket list", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => new Promise<never>(() => {}))) as ApiClient);
    renderWithProviders(<TicketListPage />);

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty ticket list", async () => {
    // The distinction this whole state machine exists for: a dead backend and
    // a quiet queue must not look identical.
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.reject(new Error("boom")))) as ApiClient);
    renderWithProviders(<TicketListPage />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No tickets found.")).not.toBeInTheDocument();
  });

  it("says there are no tickets when the queue is genuinely empty", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.resolve(page([])))) as ApiClient);
    renderWithProviders(<TicketListPage />);

    expect(await screen.findByText("No tickets found.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("counts the stat cards from the visible tickets, not a fixed number", async () => {
    // The design's stat cards read mock figures (23 / 7 / 3 / 93% / 6). A
    // card that kept them would be wrong on every deployment.
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.resolve(page([TICKET_OPEN])))) as ApiClient);
    renderWithProviders(<TicketListPage />);

    await screen.findByRole("link", { name: "INC-1" });
    const stats = document.querySelectorAll(".stat-card");
    const values = [...stats].map((card) => card.querySelector(".stat-value")?.textContent);
    // total=1, unassigned=1 (TICKET_OPEN.assigned_to is null), urgent=0
    // (priority "normal"), fromAlerts=0 (origin "technician").
    expect(values).toEqual(["1", "1", "0", "0"]);
    expect(values).not.toContain("23");
  });
});

// --------------------------------------------------------------------------
// TicketListPage: create-ticket wiring
// --------------------------------------------------------------------------

describe("TicketListPage wiring", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_TECHNICIAN),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listTickets: vi.fn().mockResolvedValue(page([])),
      createTicket: vi.fn().mockResolvedValue(TICKET_OPEN),
      listUsers: vi.fn().mockResolvedValue({ users: USERS }),
      listDevices: vi.fn().mockResolvedValue({ items: DEVICES, total: DEVICES.length }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("hides New ticket without ticket.write", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME_TECHNICIAN,
      permissions: ME_TECHNICIAN.permissions.filter((p) => p !== "ticket.write"),
    });
    renderWithProviders(<TicketListPage />);

    await screen.findByText("Tickets", { selector: "h1" });
    expect(screen.queryByRole("button", { name: "New ticket" })).not.toBeInTheDocument();
  });

  it("submits a new incident ticket with the exact entered fields", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TicketListPage />);

    await user.click(await screen.findByRole("button", { name: "New ticket" }));
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "Laptop won't boot");
    await user.type(screen.getByLabelText("Description"), "Black screen.");
    // Cycle 6: chosen from the directory, not typed as a UUID.
    await pickEntity(user, "Requester", /Rachel Requester/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createTicket).toHaveBeenCalledWith({
      type: "incident",
      category_id: "cat1",
      queue_id: null,
      subject: "Laptop won't boot",
      description: "Black screen.",
      requester_id: "req-1",
      assigned_to: null,
      device_id: null,
      // No device, so the ticket names its client: the internal client,
      // whose id is `/me`'s org_id until the phase-3 selector (MSP clients
      // plan 2026-10-26 Task 2).
      client_id: "o1",
    });
  });

  // A device decides the client on the server, which refuses a different
  // one; the console sends none rather than guessing the device's.
  it("sends no client for a ticket with a device", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TicketListPage />);

    await user.click(await screen.findByRole("button", { name: "New ticket" }));
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "Laptop won't boot");
    await pickEntity(user, "Requester", /Rachel Requester/);
    await pickEntity(user, "Device (optional)", /BUILD-01/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.createTicket).toHaveBeenCalledTimes(1));
    const body = vi.mocked(api.createTicket as ApiClient["createTicket"]).mock.calls[0]?.[0];
    expect(body).toMatchObject({ device_id: "dev-2" });
    expect(body).not.toHaveProperty("client_id");
  });

  // Without `/me` the console cannot name the internal client: it sends
  // nothing -- a body without a client is a 422 the person can do nothing
  // about -- and says why in the form's own error surface.
  it("sends nothing and says why when the client cannot be named yet", async () => {
    const user = userEvent.setup();
    api.me = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderWithProviders(<NewTicketDialog open onClose={() => {}} myUserId="req-1" />);

    await screen.findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "Laptop won't boot");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/has not loaded yet/);
    expect(api.createTicket).not.toHaveBeenCalled();
  });

  // ------------------------------------------------------------------
  // Cycle 6 -- Auditor A (HIGH): "Three raw ID fields in one dialog --
  // Requester ID, Assign to, Device ID -- all free text with no picker."
  // ------------------------------------------------------------------

  it("offers no free-text id field for requester, assignee or device", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TicketListPage />);
    await user.click(await screen.findByRole("button", { name: "New ticket" }));

    // The labels that named a UUID are gone...
    expect(screen.queryByLabelText("Requester ID")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Device ID (optional)")).not.toBeInTheDocument();
    // ...and all three fields are comboboxes over a list, not text boxes.
    for (const label of ["Requester", "Assign to (optional)", "Device (optional)"]) {
      const field = screen.getByLabelText(label);
      expect(field).toHaveAttribute("role", "combobox");
      expect(field).toHaveAttribute("aria-controls");
    }
  });

  it("sends the ids behind the names chosen for assignee and device", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TicketListPage />);

    await user.click(await screen.findByRole("button", { name: "New ticket" }));
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "Laptop won't boot");
    await pickEntity(user, "Requester", /Rachel Requester/);
    await pickEntity(user, "Assign to (optional)", /Tessa Technician/);
    await pickEntity(user, "Device (optional)", /BUILD-01/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createTicket).toHaveBeenCalledWith(
      expect.objectContaining({
        requester_id: "req-1",
        assigned_to: "tech-1",
        device_id: "dev-2",
      }),
    );
  });

  // The sublabel is the safety, not decoration (EntityPicker's own header):
  // it is what tells a technician the build box has been offline all week
  // before they link a ticket to it.
  it("shows os family and agent state beside every device, and the account state beside a person", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TicketListPage />);

    await user.click(await screen.findByRole("button", { name: "New ticket" }));
    await user.click(screen.getByLabelText("Device (optional)"));
    expect(await screen.findByRole("option", { name: /WKS-042\s*windows · online/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /BUILD-01\s*linux · offline/ })).toBeInTheDocument();

    await user.click(screen.getByLabelText("Assign to (optional)"));
    expect(
      await screen.findByRole("option", { name: /Gordon Gone\s*gone@example.com · disabled/ }),
    ).toBeInTheDocument();
  });

  // Cycle 7: a picker that is empty on a fresh install and says nothing is
  // the blocked-flow finding wearing a nicer control.
  it("says what would fill the device list rather than showing an empty one", async () => {
    const user = userEvent.setup();
    api.listDevices = vi.fn().mockResolvedValue({ items: [], total: 0 });
    renderWithProviders(<TicketListPage />);

    await user.click(await screen.findByRole("button", { name: "New ticket" }));
    await user.click(screen.getByLabelText("Device (optional)"));

    expect(await screen.findByText(/A computer appears here once it runs the agent/)).toBeInTheDocument();
  });

  // The directory carries its own permission, independent of ticket.write.
  // A 403 must leave the form usable and explained, never spinning.
  it("keeps the form usable and explains itself when the user directory is refused", async () => {
    const user = userEvent.setup();
    api.listUsers = vi.fn().mockRejectedValue(new ApiError(403, ""));
    renderWithProviders(<TicketListPage />);

    await user.click(await screen.findByRole("button", { name: "New ticket" }));
    await user.click(screen.getByLabelText("Requester"));

    expect(await screen.findByText(/you may also not have permission to list them/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  // Auditor A's own instruction: "Default Requester ID to the signed-in user
  // (the app already knows their ID -- it passes assigned_to=06a8438d-... in
  // its own queries)." This is the half of the blocked-flow finding a picker
  // alone does not close.
  it("defaults the requester to the signed-in technician, by name", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TicketListPage />);

    await user.click(await screen.findByRole("button", { name: "New ticket" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Requester")).toHaveValue("Tessa Technician"),
    );

    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "Laptop won't boot");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createTicket).toHaveBeenCalledWith(
      expect.objectContaining({ requester_id: "tech-1" }),
    );
  });

  // Auditor A (HIGH): "Ticket Description is a single-line <input
  // type="text">, not a textarea ... hasTextarea: false for the whole
  // dialog."
  it("gives Description a multi-line textarea that accepts newlines", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TicketListPage />);

    await user.click(await screen.findByRole("button", { name: "New ticket" }));
    const description = screen.getByLabelText("Description");
    expect(description.tagName).toBe("TEXTAREA");

    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "Laptop won't boot");
    await pickEntity(user, "Requester", /Rachel Requester/);
    await user.type(description, "line one{Enter}line two");
    await user.click(screen.getByRole("button", { name: "Save" }));

    // Enter inside the textarea inserted a newline rather than submitting.
    expect(api.createTicket).toHaveBeenCalledWith(
      expect.objectContaining({ description: "line one\nline two" }),
    );
  });

  // ------------------------------------------------------------------
  // Form kernel adoption (UX remediation cycle 3).
  //
  // Auditor B (HIGH), /service-desk -> New ticket modal -> Save: "Creating a
  // ticket is silently blocked by an undeclared required field (Requester
  // ID)." A disabled button that never says why IS the silent no-op, so
  // these pin the opposite behaviour: Save stays live and the click produces
  // the reason.
  // ------------------------------------------------------------------

  async function openNewTicket(user: ReturnType<typeof userEvent.setup>) {
    renderWithProviders(<TicketListPage />);
    await user.click(await screen.findByRole("button", { name: "New ticket" }));
  }

  it("keeps Save enabled on an empty form and names every missing field", async () => {
    // `/me` is what fills Requester now, so this exercises the case that
    // still cannot be filled: no profile, no directory, nothing chosen.
    const user = userEvent.setup();
    api.me = vi.fn().mockResolvedValue({ ...ME_TECHNICIAN, user_id: "" });
    await openNewTicket(user);

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(api.createTicket).not.toHaveBeenCalled();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Category is required");
    expect(alert).toHaveTextContent("Subject is required");
    expect(alert).toHaveTextContent("Requester is required");
  });

  it("names Requester as the field blocking an otherwise complete ticket", async () => {
    // The finding exactly: category and subject filled, Save dead, and the
    // undeclared third field never mentioned.
    const user = userEvent.setup();
    api.me = vi.fn().mockResolvedValue({ ...ME_TECHNICIAN, user_id: "" });
    await openNewTicket(user);

    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "Laptop won't boot");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createTicket).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Requester is required");
  });

  // The picker never yields a whitespace id, but `requiredProblems` trimming
  // is what makes that true of the submitted body as well -- pinned here
  // because a directory row with a blank id would otherwise sail through.
  it("treats a blank requester id from the directory as missing", async () => {
    const user = userEvent.setup();
    api.me = vi.fn().mockResolvedValue({ ...ME_TECHNICIAN, user_id: "   " });
    await openNewTicket(user);

    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "Laptop won't boot");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createTicket).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Requester is required");
  });

  it("saves on Enter from the Subject field", async () => {
    // ui.tsx's Button sets no type and these controls were not in a <form>,
    // so the submit control was an orphan and Enter did nothing.
    const user = userEvent.setup();
    await openNewTicket(user);

    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await pickEntity(user, "Requester", /Rachel Requester/);
    await user.type(screen.getByLabelText("Subject"), "Laptop won't boot{Enter}");

    await waitFor(() => expect(api.createTicket).toHaveBeenCalledTimes(1));
  });

  it("does not create the ticket when Cancel is pressed", async () => {
    // `type="button"` on Cancel is load-bearing inside the new <form>: an
    // untyped button in a form IS a submit button, so Cancel would have
    // created the ticket it was cancelling.
    const user = userEvent.setup();
    await openNewTicket(user);

    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "Laptop won't boot");
    await pickEntity(user, "Requester", /Rachel Requester/);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(api.createTicket).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "New ticket" })).not.toBeInTheDocument();
  });

  it("announces a bodyless 503 instead of swallowing it", async () => {
    // `{String(create.error)}` rendered a raw object at best and, for the
    // bodyless 503 a proxy returns, an empty string -- the failure the form
    // kernel's describeSubmitError exists for.
    const user = userEvent.setup();
    api.createTicket = vi.fn().mockRejectedValue(new ApiError(503, ""));
    await openNewTicket(user);

    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "Laptop won't boot");
    await pickEntity(user, "Requester", /Rachel Requester/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent?.trim()).not.toBe("");
    expect(alert).toHaveTextContent(/unavailable/i);
    expect(screen.getByRole("dialog", { name: "New ticket" })).toBeInTheDocument();
  });

  // C1 fix (spec §9's `q` filter): typed search text reaches the route.
  it("filters the ticket list by a typed free-text search term", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TicketListPage />);

    await screen.findByText("Tickets", { selector: "h1" });
    await user.type(screen.getByLabelText("Search"), "printer");

    await waitFor(() => {
      expect(api.listTickets).toHaveBeenCalledWith(
        expect.objectContaining({ q: "printer" } satisfies Partial<TicketListFilters>),
      );
    });
  });

  // C2 fix (spec §11): the assignee filter named in the spec was never
  // rendered. Setting it bypasses the default-view merge (see the
  // "TicketListPage default view" describe block below) and reaches the
  // route as an ordinary `assigned_to` equality filter.
  it("filters the ticket list by a chosen assignee", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TicketListPage />);

    await screen.findByText("Tickets", { selector: "h1" });
    // Cycle 6: the filter's placeholder used to be, literally, "User ID"; the
    // v3 port made it a `<select>` over the same directory rather than an
    // `EntityPicker` with a visible caption, so that the eight filters are one
    // bar and not two. Still a chooser over names, never a typed id.
    await screen.findByRole("option", { name: "Rachel Requester" });
    await user.selectOptions(screen.getByLabelText("Assignee"), "req-1");

    await waitFor(() => {
      expect(api.listTickets).toHaveBeenCalledWith(
        expect.objectContaining({
          assigned_to: "req-1",
        } satisfies Partial<TicketListFilters>),
      );
    });
  });

  // Auditor A (MEDIUM): "The Status filter is a free-text box while every
  // other filter beside it is a dropdown ... a typo or a wrong tense
  // silently returns nothing, and the user cannot discover the valid
  // values."
  it("filters by status from a dropdown of the real vocabulary, not a text box", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TicketListPage />);

    await screen.findByText("Tickets", { selector: "h1" });
    const status = screen.getByLabelText("Status");
    expect(status.tagName).toBe("SELECT");
    // Both type vocabularies are reachable -- the filter is not type-scoped.
    expect(within(status).getByRole("option", { name: "Pending approval" })).toBeInTheDocument();
    expect(within(status).getByRole("option", { name: "Resolved" })).toBeInTheDocument();

    await user.selectOptions(status, "resolved");

    await waitFor(() => {
      expect(api.listTickets).toHaveBeenCalledWith(
        expect.objectContaining({ status: "resolved" } satisfies Partial<TicketListFilters>),
      );
    });
  });

  // Auditor A (MEDIUM): "Dropdown options render as incident, request, low,
  // normal, high, urgent -- bare lowercase database values" beside Category
  // and Queue selects that use proper labels.
  it("labels the type and priority filters for people, not for the database", async () => {
    renderWithProviders(<TicketListPage />);

    await screen.findByText("Tickets", { selector: "h1" });
    const type = screen.getByLabelText("Type");
    expect(within(type).getByRole("option", { name: "Incident" })).toBeInTheDocument();
    expect(within(type).queryByRole("option", { name: "incident" })).not.toBeInTheDocument();

    const priority = screen.getByLabelText("Priority");
    expect(within(priority).getByRole("option", { name: "Urgent" })).toBeInTheDocument();
    expect(within(priority).queryByRole("option", { name: "urgent" })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// TicketListPage: default view (task 13's G2 fix -- spec §11, amended,
// "open tickets assigned to me, plus unassigned open tickets"). C5 fix
// (whole-branch review round C): the "assigned to me" half is now a REAL
// server-side `assigned_to` query rather than a client-side filter over one
// unfiltered page -- see the regression test below, which proves the
// caller's own ticket survives even when a generic/pool fetch would have
// missed it entirely. ME_TECHNICIAN's user_id is "tech-1".
// --------------------------------------------------------------------------

// --------------------------------------------------------------------------
// TicketListPage: what the ROWS say (UX remediation cycle 12).
//
// Auditor A (MEDIUM): "Enum values shown raw and lowercase throughout the
// module." Cycle 6 fixed the filter dropdowns; the table itself still printed
// `incident` / `open` / `normal` straight off the wire, which is the surface a
// technician actually reads.
//
// Auditor A (LOW): "Hard-coded limit=200 with no pagination control ... Once
// an organisation exceeds 200 tickets the list silently truncates with nothing
// telling the user rows are missing."
// --------------------------------------------------------------------------

describe("TicketListPage rows", () => {
  function apiWith(tickets: Page<TicketOut>): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME_TECHNICIAN),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES),
      listTickets: vi.fn().mockResolvedValue(tickets),
      listUsers: vi.fn().mockResolvedValue({ users: USERS }),
      listDevices: vi.fn().mockResolvedValue({ items: DEVICES, total: DEVICES.length }),
    };
  }

  it("labels type, status and priority in the row for people, not for the database", async () => {
    const request: TicketOut = {
      ...TICKET_OPEN,
      id: "t20",
      number: "REQ-20",
      type: "request",
      status: "pending_approval",
      priority: "urgent",
    };
    mockUseApi.mockReturnValue(apiWith(page([TICKET_OPEN, request])) as ApiClient);
    renderWithProviders(<TicketListPage />);

    const incidentRow = (await screen.findByRole("link", { name: "INC-1" })).closest(
      "tr",
    ) as HTMLElement;
    expect(within(incidentRow).getByText("Incident")).toBeInTheDocument();
    expect(within(incidentRow).getByText("Open")).toBeInTheDocument();
    expect(within(incidentRow).getByText("Normal")).toBeInTheDocument();

    const requestRow = (screen.getByRole("link", { name: "REQ-20" })).closest("tr") as HTMLElement;
    expect(within(requestRow).getByText("Request")).toBeInTheDocument();
    expect(within(requestRow).getByText("Pending approval")).toBeInTheDocument();
    expect(within(requestRow).getByText("Urgent")).toBeInTheDocument();

    // And none of the wire values survives anywhere on the page.
    for (const raw of ["incident", "request", "open", "pending_approval", "normal", "urgent"]) {
      expect(screen.queryByText(raw)).not.toBeInTheDocument();
    }
  });

  const FULL_PAGE = Array.from({ length: 200 }, (_, at) => ({
    ...TICKET_OPEN,
    id: `bulk-${at}`,
    number: `INC-${1000 + at}`,
  }));

  it("says the list is capped when the server says more matched than it sent", async () => {
    mockUseApi.mockReturnValue(apiWith(page(FULL_PAGE, 412)) as ApiClient);
    renderWithProviders(<TicketListPage />);

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent(
      "Showing 200 of 412 matching tickets. Narrow the filters above to see the rest.",
    );
  });

  it("names both numbers once a single filtered query owns the page", async () => {
    mockUseApi.mockReturnValue(apiWith(page(FULL_PAGE, 412)) as ApiClient);
    renderWithProviders(<TicketListPage />, ["/?assignee=tech-1"]);

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent(
      "Showing 200 of 412 matching tickets. Narrow the filters above to see the rest.",
    );
  });

  it("says nothing about a cap when a full page IS the whole queue", async () => {
    // The improvement the `total` bought: this used to warn on ANY page of
    // exactly 200, because a full page was the only truncation signal the
    // route offered. An org with exactly 200 tickets was told rows were
    // missing when none were.
    mockUseApi.mockReturnValue(apiWith(page(FULL_PAGE)) as ApiClient);
    renderWithProviders(<TicketListPage />);

    await screen.findByRole("link", { name: "INC-1000" });
    expect(screen.queryByText(/matching tickets/)).not.toBeInTheDocument();
  });

  it("says nothing about a cap when the whole queue fits on the page", async () => {
    mockUseApi.mockReturnValue(apiWith(page([TICKET_OPEN])) as ApiClient);
    renderWithProviders(<TicketListPage />);

    await screen.findByRole("link", { name: "INC-1" });
    expect(screen.queryByText(/matching tickets/)).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// TicketDetailPage: comment / status / assign / approve / reject wiring
// --------------------------------------------------------------------------

describe("TicketDetailPage wiring", () => {
  let api: Partial<ApiClient>;

  function renderDetail(ticketId: string) {
    return renderWithProviders(
      <Routes>
        <Route path="/service-desk/tickets/:ticketId" element={<TicketDetailPage />} />
      </Routes>,
      [`/service-desk/tickets/${ticketId}`],
    );
  }

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES),
      getTicket: vi.fn().mockResolvedValue(TICKET_OPEN),
      listTicketComments: vi.fn().mockResolvedValue(COMMENTS),
      listTicketAttachments: vi.fn().mockResolvedValue(ATTACHMENTS),
      listTicketStatusHistory: vi.fn().mockResolvedValue(STATUS_HISTORY),
      createTicketComment: vi.fn().mockResolvedValue(COMMENTS[0]),
      updateTicket: vi.fn().mockResolvedValue(TICKET_OPEN),
      changeTicketStatus: vi.fn().mockResolvedValue({ ...TICKET_OPEN, status: "resolved" }),
      assignTicket: vi.fn().mockResolvedValue({ ...TICKET_OPEN, assigned_to: "tech-1" }),
      approveTicket: vi.fn().mockResolvedValue({ ...TICKET_PENDING_APPROVAL, status: "in_progress" }),
      rejectTicket: vi.fn().mockResolvedValue({ ...TICKET_PENDING_APPROVAL, status: "rejected" }),
      listUsers: vi.fn().mockResolvedValue({ users: USERS }),
      listDevices: vi.fn().mockResolvedValue({ items: DEVICES, total: DEVICES.length }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // The three states a mock never has (UI port spec §6/§8.5): before this
  // migration, an unresolved ticket load and a failed one both rendered an
  // identical bare `<div></div>`.
  it("says it is loading rather than rendering a bare page", async () => {
    api.getTicket = vi.fn(() => new Promise<never>(() => {}));
    renderDetail("t1");

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than rendering a bare page", async () => {
    api.getTicket = vi.fn().mockRejectedValue(new Error("boom"));
    renderDetail("t1");

    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  // `ai_copilot` mounts `/service-desk/tickets/{id}/ai`, gated on the same
  // `ticket.read` this page needs, so the panel is offered to every reader.
  it("offers the copilot on the ticket", async () => {
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    expect(screen.getByRole("heading", { name: "Copilot" })).toBeInTheDocument();
    expect(screen.getByLabelText("Ask the copilot")).toBeInTheDocument();
  });

  it("posts a public comment with the exact typed body", async () => {
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    await user.type(screen.getByLabelText("Message"), "Replaced the cable.");
    await user.click(screen.getByRole("button", { name: "Send reply" }));

    expect(api.createTicketComment).toHaveBeenCalledWith("t1", {
      body: "Replaced the cable.",
      visibility: "public",
    });
  });

  it("posts an internal comment when internal visibility is selected", async () => {
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    await user.click(screen.getByRole("radio", { name: /Internal note/ }));
    await user.type(screen.getByLabelText("Message"), "Escalating internally.");
    await user.click(screen.getByRole("button", { name: "Add note" }));

    expect(api.createTicketComment).toHaveBeenCalledWith("t1", {
      body: "Escalating internally.",
      visibility: "internal",
    });
  });

  it("changes the ticket status through the dedicated transition endpoint with a reason", async () => {
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    await user.selectOptions(screen.getByLabelText("Status"), "resolved");
    await user.type(screen.getByLabelText("Why is the status changing? (optional)"), "Fixed.");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(api.changeTicketStatus).toHaveBeenCalledWith("t1", {
      to_status: "resolved",
      reason: "Fixed.",
    });
  });

  // Moving a ticket to another client (MSP clients plan 2026-10-26 Task 3):
  // the clients come from `GET /clients`, never "Unassigned" (C10), an
  // archived one, nor the ticket's own, and the server's refusal is shown in
  // its own words.
  describe("moving a ticket to another client", () => {
    const CLIENTS = [
      { id: "o1", name: "Novieri IT", kind: "internal" },
      { id: "c-acme", name: "Acme", kind: "customer" },
      { id: "u1", name: "Unassigned", kind: "unassigned" },
      { id: "c-beta", name: "Beta", kind: "customer" },
      // Archived: it takes nothing new, so a move there is only a 409.
      { id: "c-gone", name: "Gone Ltd", kind: "customer", status: "archived" },
    ].map((client) => ({
      status: "active", email_domains: [], phone: "", address: "", website: "", notes: "",
      enabled_modules: [], device_count: 0, site_count: 0, ...client,
    })) as Awaited<ReturnType<ApiClient["listClients"]>>;

    beforeEach(() => {
      api.me = vi.fn().mockResolvedValue({
        ...ME_FULL, permissions: [...ME_FULL.permissions, "clients.read"],
      });
      api.listClients = vi.fn().mockResolvedValue(CLIENTS);
      api.moveTicket = vi.fn().mockResolvedValue({ ...TICKET_OPEN, client_id: "c-acme" });
    });

    it("moves the ticket to a client chosen from the list", async () => {
      const user = userEvent.setup();
      renderDetail("t1");

      await screen.findByText("INC-1: Laptop won't boot");
      expect(await screen.findByText(/This ticket belongs to Novieri IT/)).toBeInTheDocument();
      const select = await screen.findByLabelText("Move to another client");
      const offered = within(select).getAllByRole("option").map((option) => option.textContent);
      expect(offered).toEqual(["Choose a client…", "Acme", "Beta"]);
      await user.selectOptions(select, "c-acme");
      await user.click(screen.getByRole("button", { name: "Move ticket" }));

      expect(api.moveTicket).toHaveBeenCalledWith("t1", "c-acme");
    });

    it("shows the server's refusal in its own words", async () => {
      api.moveTicket = vi.fn().mockRejectedValue(
        new ApiError(409, JSON.stringify({
          detail: "remove the ticket's device before moving it to another client",
        })),
      );
      const user = userEvent.setup();
      renderDetail("t1");

      await user.selectOptions(await screen.findByLabelText("Move to another client"), "c-beta");
      await user.click(screen.getByRole("button", { name: "Move ticket" }));

      expect(
        await screen.findByText(/remove the ticket's device before moving it to another client/),
      ).toBeInTheDocument();
    });

    it("is not offered without clients.read, the list it needs", async () => {
      api.me = vi.fn().mockResolvedValue(ME_FULL);
      renderDetail("t1");

      await screen.findByText("INC-1: Laptop won't boot");
      expect(screen.queryByLabelText("Move to another client")).not.toBeInTheDocument();
      expect(api.listClients).not.toHaveBeenCalled();
    });
  });

  it("assigns the ticket to a person chosen from the directory", async () => {
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    // Cycle 6: the label used to be "Assignee user ID" over a text box.
    expect(screen.queryByLabelText("Assignee user ID")).not.toBeInTheDocument();
    await pickEntity(user, "Assigned to", /Tessa Technician/);
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(api.assignTicket).toHaveBeenCalledWith("t1", { assigned_to: "tech-1" });
  });

  // ------------------------------------------------------------------
  // Cycle 6 (read side) -- Auditor B, on the identical shape in /audit:
  // "identifies every actor and target by raw UUID only -- you cannot tell
  // who did what."
  // ------------------------------------------------------------------

  it("names the requester and the assignee instead of printing their UUIDs", async () => {
    api.getTicket = vi.fn().mockResolvedValue({ ...TICKET_OPEN, assigned_to: "tech-1" });
    renderDetail("t1");

    // Both people are named in the details AND in the conversation (v3 draws
    // the requester's opening message and every comment author as a person),
    // so each name appears more than once; what matters is that the names
    // appear and the UUIDs do not.
    expect((await screen.findAllByText("Rachel Requester")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Tessa Technician").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("req-1")).not.toBeInTheDocument();
  });

  // Falls back to the id rather than to blank: an id whose directory row the
  // caller cannot read is still more than nothing, and "—" on a ticket that
  // IS assigned would be the worse lie.
  // Cycle 12 -- Auditor A (MEDIUM), "enum values shown raw and lowercase
  // throughout the module": the header badges, the Type field and the
  // comment-visibility column were the module's last raw wire values.
  it("labels status, priority, type and comment visibility for people", async () => {
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    expect(screen.getAllByText("Open").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Normal").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Incident").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Public").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Internal note").length).toBeGreaterThanOrEqual(1);
    for (const raw of ["open", "normal", "incident", "public", "internal"]) {
      expect(screen.queryByText(raw)).not.toBeInTheDocument();
    }
  });

  it("still shows the id when the directory cannot resolve it", async () => {
    api.listUsers = vi.fn().mockResolvedValue({ users: [] });
    api.getTicket = vi.fn().mockResolvedValue({ ...TICKET_OPEN, assigned_to: "ghost-9" });
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    await waitFor(() => expect(screen.getAllByText("ghost-9").length).toBeGreaterThanOrEqual(1));
  });

  it("links a device through a picker that shows whether it can answer", async () => {
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    expect(screen.queryByLabelText("Device ID")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Device"));
    expect(await screen.findByRole("option", { name: /BUILD-01\s*linux · offline/ })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: /WKS-042\s*windows · online/ }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(api.updateTicket).toHaveBeenCalledWith(
      "t1",
      expect.objectContaining({ device_id: "dev-1" }),
    );
  });

  it("edits Description and posts comments through textareas, not one-line inputs", async () => {
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    expect(screen.getByLabelText("Description").tagName).toBe("TEXTAREA");
    expect(screen.getByLabelText("Message").tagName).toBe("TEXTAREA");
  });

  // ------------------------------------------------------------------
  // Cycle 8 -- Auditor B (HIGH), on this exact shape: "Resolve is a
  // one-click, unconfirmed, irreversible state change -- there is no way to
  // reopen ... A misclick permanently dismisses a production error group
  // from the Open count, and the operator cannot put it back."
  // ------------------------------------------------------------------

  it("asks before a transition nothing can leave, naming the ticket", async () => {
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    await user.selectOptions(screen.getByLabelText("Status"), "cancelled");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    // Nothing has been sent yet -- the dialog is the gate, not a receipt.
    expect(api.changeTicketStatus).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog", { name: /Move this ticket to cancelled/ });
    expect(within(dialog).getByText("INC-1: Laptop won't boot")).toBeInTheDocument();
    expect(within(dialog).getByText(/can never be reopened/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Move to cancelled" }));
    expect(api.changeTicketStatus).toHaveBeenCalledWith("t1", {
      to_status: "cancelled",
      reason: "",
    });
  });

  it("cancels the transition when the confirmation is declined", async () => {
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    await user.selectOptions(screen.getByLabelText("Status"), "cancelled");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }),
    );

    expect(api.changeTicketStatus).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  // The distinction the whole design turns on: lifecycle.py makes `closed`
  // reopenable and `cancelled` final, so the two confirmations must not say
  // the same thing. A confirmation that claims a reversible action cannot be
  // undone teaches people to click through the one that matters.
  it("tells the truth about closing, which is reversible, and does not call it final", async () => {
    const user = userEvent.setup();
    api.getTicket = vi.fn().mockResolvedValue({ ...TICKET_OPEN, status: "resolved" });
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    await user.selectOptions(screen.getByLabelText("Status"), "closed");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    const dialog = await screen.findByRole("dialog", { name: "Close this ticket" });
    expect(within(dialog).getByText(/can be reopened from this same panel/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/never be reopened/)).not.toBeInTheDocument();
  });

  // `resolved` and `fulfilled` both have a documented reopen edge, so
  // stopping for them would be friction that buys nothing.
  it("does not interrupt a resolve, which lifecycle.py lets a technician undo", async () => {
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    await user.selectOptions(screen.getByLabelText("Status"), "resolved");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.changeTicketStatus).toHaveBeenCalledWith("t1", {
      to_status: "resolved",
      reason: "",
    });
  });

  // The half a dialog does not fix: a technician looking at a closed ticket
  // had nothing on screen telling them `closed -> open` was ever allowed.
  it("says on a closed ticket that it can be reopened from the edit form", async () => {
    api.getTicket = vi.fn().mockResolvedValue({ ...TICKET_OPEN, status: "closed" });
    renderDetail("t1");

    expect(
      await screen.findByText(
        "This ticket is closed, so its details cannot change. Set an active status to reopen it.",
      ),
    ).toBeInTheDocument();
  });

  // ...and says the opposite where the opposite is true. The same sentence
  // on both would be the lie the two-branch design exists to avoid.
  it("says on a cancelled ticket that nothing leaves it", async () => {
    api.getTicket = vi.fn().mockResolvedValue({ ...TICKET_OPEN, status: "cancelled" });
    renderDetail("t1");

    expect(
      await screen.findByText("Nothing leaves Cancelled. This ticket cannot be reopened."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/to reopen it/)).not.toBeInTheDocument();
  });

  it("unassigns an already-assigned ticket", async () => {
    api.getTicket = vi.fn().mockResolvedValue({ ...TICKET_OPEN, assigned_to: "tech-1" });
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    await pickEntity(user, "Assigned to", /^Nobody/);
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(api.assignTicket).toHaveBeenCalledWith("t1", { assigned_to: null });
  });

  it("moves the ticket to a different queue", async () => {
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    await user.selectOptions(screen.getByLabelText("Queue"), "q2");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(api.assignTicket).toHaveBeenCalledWith("t1", { queue_id: "q2" });
  });

  it("approves a pending_approval request with the typed reason", async () => {
    api.getTicket = vi.fn().mockResolvedValue(TICKET_PENDING_APPROVAL);
    const user = userEvent.setup();
    renderDetail("t2");

    await screen.findByText("REQ-1: Laptop won't boot");
    await user.type(screen.getByLabelText("Reason (required to reject)"), "Looks good.");
    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(api.approveTicket).toHaveBeenCalledWith("t2", { reason: "Looks good." });
  });

  it("rejects a pending_approval request with the typed reason", async () => {
    api.getTicket = vi.fn().mockResolvedValue(TICKET_PENDING_APPROVAL);
    const user = userEvent.setup();
    renderDetail("t2");

    await screen.findByText("REQ-1: Laptop won't boot");
    await user.type(screen.getByLabelText("Reason (required to reject)"), "Not authorized.");
    await user.click(screen.getByRole("button", { name: "Reject" }));

    expect(api.rejectTicket).toHaveBeenCalledWith("t2", { reason: "Not authorized." });
  });

  // Spec §4: a rejection requires a non-empty reason -- disabling the
  // control with an empty reason is presentational only (the server
  // enforces the real rule), but it must reflect the rule rather than let a
  // technician submit a request the server will 400.
  it("disables Reject until a reason is entered", async () => {
    api.getTicket = vi.fn().mockResolvedValue(TICKET_PENDING_APPROVAL);
    renderDetail("t2");

    await screen.findByText("REQ-1: Laptop won't boot");
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  });

  // Approval controls are hidden entirely without ticket.approve --
  // server-side RBAC is authoritative; hiding here is cosmetic only.
  it("hides the approval panel without ticket.approve", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME_FULL,
      permissions: ME_FULL.permissions.filter((p) => p !== "ticket.approve"),
    });
    api.getTicket = vi.fn().mockResolvedValue(TICKET_PENDING_APPROVAL);
    renderDetail("t2");

    await screen.findByText("REQ-1: Laptop won't boot");
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });

  // Spec §3.3/§6: a terminal ticket accepts no new comments or attachments.
  // The composer/upload controls are not merely disabled -- they are not
  // offered at all, with an explanatory message instead.
  it("hides the comment composer and attachment upload on a closed ticket", async () => {
    api.getTicket = vi.fn().mockResolvedValue(TICKET_CLOSED);
    renderDetail("t3");

    await screen.findByText("INC-2: Laptop won't boot");
    expect(screen.queryByLabelText("Message")).not.toBeInTheDocument();
    expect(
      screen.getByText("This ticket is closed and takes no new replies. Reopen it to reply."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Attach a file" })).not.toBeInTheDocument();
    expect(
      screen.getByText("This ticket is closed and accepts no new attachments."),
    ).toBeInTheDocument();
    // The status-transition control is NOT gated by the terminal freeze --
    // `closed` is reopenable (spec §3.3) -- so it must still be offered.
    expect(screen.getByLabelText("Status")).not.toBeDisabled();
  });

  // Task 13's G1 fix: the status-history panel consumes the newly-mounted
  // `GET .../history` route (via `listTicketStatusHistory`), rendered
  // oldest-first exactly as the backend returns it -- no client-side
  // re-sorting.
  it("renders the status history table oldest-first", async () => {
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    const heading = await screen.findByRole("heading", { name: "Status history" });
    // `.panel` RATHER THAN `div`: this panel is `<Panel>` now, which renders a
    // `<section>`, and the heading's nearest `div` is the panel head — which
    // holds the title and nothing else. The class is what the design gives
    // every panel whichever element carries it, so this finds the box the
    // assertion is about rather than the tag it happened to be built from.
    const card = heading.closest(".panel") as HTMLElement;
    // rows[0] is the header row (From/To/Reason/Changed); data rows follow
    // in the exact order the API returned them -- no client-side re-sort.
    const rows = within(card).getAllByRole("row");
    // Cycle 12: these cells read `new`/`open` off the wire before the enum
    // display layer reached the history table.
    expect(rows[1]).toHaveTextContent("New");
    expect(rows[2]).toHaveTextContent("Open");
    expect(rows[2]).toHaveTextContent("Started work");
    expect(api.listTicketStatusHistory).toHaveBeenCalledWith("t1");
  });

  it("shows a placeholder when a ticket has no status history", async () => {
    api.listTicketStatusHistory = vi.fn().mockResolvedValue([]);
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    expect(await screen.findByText("No status history yet.")).toBeInTheDocument();
  });

  // --------------------------------------------------------------------
  // C3 fix (spec §7.3): inline inventory summary + patch state panel for a
  // ticket with a linked device, degrading gracefully on 403.
  // --------------------------------------------------------------------

  it("does not render a device summary panel when the ticket has no linked device", async () => {
    renderDetail("t1");

    await screen.findByText("INC-1: Laptop won't boot");
    expect(screen.queryByText("Inventory summary")).not.toBeInTheDocument();
    expect(screen.queryByText("Patch state")).not.toBeInTheDocument();
  });

  it("renders inline inventory summary and patch state for a ticket with a linked device", async () => {
    api.getTicket = vi.fn().mockResolvedValue(TICKET_WITH_DEVICE);
    api.getDeviceInventory = vi.fn().mockResolvedValue(DEVICE_INVENTORY);
    api.getDevicePatchStatus = vi.fn().mockResolvedValue(DEVICE_PATCH_STATUS);
    renderDetail("t4");

    await screen.findByText("INC-3: Laptop won't boot");
    await screen.findByText("Inventory summary");
    expect(kvValue("Hostname")).toHaveTextContent("WKS-042");
    // The OS reads as a person writes it: the agent reports "windows".
    expect(kvValue("OS")).toHaveTextContent("Windows 11");
    expect(kvValue("Pending reboot")).toHaveTextContent("Yes");
    // And the patch counts are keyed by a label, not by the wire's state key
    // -- "missing: 2" sat directly under "Pending reboot: No".
    expect(kvValue("Missing")).toHaveTextContent("2");
    // Additive, not a replacement -- the full-device link is still there.
    // The link is labelled with the hostname the inventory read already
    // knows; the id stays on it as `title`.
    expect(screen.getByRole("link", { name: "WKS-042" })).toHaveAttribute("href", "/devices/dev-1");
  });

  it("degrades quietly when the inventory endpoint 403s for a ticket.read-only caller", async () => {
    api.getTicket = vi.fn().mockResolvedValue(TICKET_WITH_DEVICE);
    api.getDeviceInventory = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    api.getDevicePatchStatus = vi.fn().mockResolvedValue(DEVICE_PATCH_STATUS);
    renderDetail("t4");

    await screen.findByText("INC-3: Laptop won't boot");
    expect(
      await screen.findByText("Inventory summary not available (insufficient permissions)."),
    ).toBeInTheDocument();
    // The sibling panel is independent -- a 403 on inventory does not take
    // down patch state, and the page never renders a broken/error state.
    await screen.findByText("Patch state");
    expect(kvValue("Pending reboot")).toHaveTextContent("Yes");
    expect(screen.queryByText(/^error/i)).not.toBeInTheDocument();
  });

  it("degrades quietly when the patch-status endpoint 403s for a ticket.read-only caller", async () => {
    api.getTicket = vi.fn().mockResolvedValue(TICKET_WITH_DEVICE);
    api.getDeviceInventory = vi.fn().mockResolvedValue(DEVICE_INVENTORY);
    api.getDevicePatchStatus = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    renderDetail("t4");

    await screen.findByText("INC-3: Laptop won't boot");
    expect(
      await screen.findByText("Patch state not available (insufficient permissions)."),
    ).toBeInTheDocument();
    await screen.findByText("Inventory summary");
    expect(kvValue("Hostname")).toHaveTextContent("WKS-042");
  });
});

// --------------------------------------------------------------------------
// MyWorkPage: filters by the caller's own user id
// --------------------------------------------------------------------------

describe("MyWorkPage", () => {
  it("lists only tickets assigned to the caller, across queues", async () => {
    const api: Partial<ApiClient> = {
      me: vi.fn().mockResolvedValue(ME_TECHNICIAN),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTickets: vi.fn().mockResolvedValue(page([TICKET_OPEN])),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<MyWorkPage />);

    await screen.findByText("INC-1");
    expect(api.listTickets).toHaveBeenCalledWith({ scope: "mine", sort: "due", limit: 200 });
  });

  // Cycle 12 -- Auditor A (MEDIUM), "enum values shown raw and lowercase
  // throughout the module": this table printed the wire values too.
  it("labels type, status and priority for people, not for the database", async () => {
    const api: Partial<ApiClient> = {
      me: vi.fn().mockResolvedValue(ME_TECHNICIAN),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTickets: vi.fn().mockResolvedValue(page([TICKET_OPEN])),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<MyWorkPage />);

    const row = (await screen.findByRole("link", { name: "INC-1" })).closest("tr") as HTMLElement;
    expect(within(row).getByText("Incident")).toBeInTheDocument();
    expect(within(row).getByText("Open")).toBeInTheDocument();
    expect(within(row).getByText("Normal")).toBeInTheDocument();
    for (const raw of ["incident", "open", "normal"]) {
      expect(screen.queryByText(raw)).not.toBeInTheDocument();
    }
  });

  it("says the request FAILED rather than showing an empty work list", async () => {
    const api: Partial<ApiClient> = {
      me: vi.fn().mockResolvedValue(ME_TECHNICIAN),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTickets: vi.fn().mockRejectedValue(new Error("boom")),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<MyWorkPage />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No open tickets are assigned to you.")).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// ServiceDeskAdminPage: queue/category management wiring, ticket.admin-gated
// --------------------------------------------------------------------------

describe("ServiceDeskAdminPage wiring", () => {
  function baseApi(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME_FULL),
      listTicketQueues: vi.fn().mockResolvedValue([]),
      listTicketCategories: vi.fn().mockResolvedValue([]),
      listCannedResponses: vi.fn().mockResolvedValue([]),
      ...overrides,
    };
  }

  // Auditor B, cross-cutting: "Same silent-no-op failure repeats on every
  // form in the module." `disabled={create.isPending || !key || !name}` was
  // the last instance left in this module -- the click moved nothing and
  // named neither field.
  it("keeps the New queue Save enabled on an empty form and names every missing field", async () => {
    const user = userEvent.setup();
    const api = baseApi({ createTicketQueue: vi.fn() });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ServiceDeskAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New queue" }));
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(api.createTicketQueue).not.toHaveBeenCalled();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Key is required");
    expect(alert).toHaveTextContent("Name is required");
  });

  it("keeps the New category Save enabled on an empty form and names every missing field", async () => {
    const user = userEvent.setup();
    const api = baseApi({ createTicketCategory: vi.fn() });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ServiceDeskAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New category" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createTicketCategory).not.toHaveBeenCalled();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Key is required");
    expect(alert).toHaveTextContent("Name is required");
  });

  // `type="button"` on Cancel is load-bearing inside the new <form>: an
  // untyped button in a form IS a submit button.
  it("does not create the queue when Cancel is pressed", async () => {
    const user = userEvent.setup();
    const api = baseApi({ createTicketQueue: vi.fn() });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ServiceDeskAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New queue" }));
    await user.type(screen.getByLabelText("Key"), "helpdesk");
    await user.type(screen.getByLabelText("Name"), "Helpdesk");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(api.createTicketQueue).not.toHaveBeenCalled();
  });

  it("saves the queue on Enter from the Name field", async () => {
    const user = userEvent.setup();
    const api = baseApi({ createTicketQueue: vi.fn().mockResolvedValue(QUEUES[0]) });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ServiceDeskAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New queue" }));
    await user.type(screen.getByLabelText("Key"), "helpdesk");
    await user.type(screen.getByLabelText("Name"), "Helpdesk{Enter}");

    await waitFor(() =>
      expect(api.createTicketQueue).toHaveBeenCalledWith({ key: "helpdesk", name: "Helpdesk" }),
    );
  });

  it("creates a new queue with the typed key and name", async () => {
    const user = userEvent.setup();
    const api = baseApi({ createTicketQueue: vi.fn().mockResolvedValue(QUEUES[0]) });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ServiceDeskAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New queue" }));
    await user.type(screen.getByLabelText("Key"), "helpdesk");
    await user.type(screen.getByLabelText("Name"), "Helpdesk");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createTicketQueue).toHaveBeenCalledWith({ key: "helpdesk", name: "Helpdesk" });
  });

  it("renames a queue and toggles it inactive", async () => {
    const user = userEvent.setup();
    const api = baseApi({
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      updateTicketQueue: vi.fn().mockResolvedValue({ ...QUEUES[0], name: "Renamed", active: false }),
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ServiceDeskAdminPage />);

    await user.click((await screen.findAllByRole("button", { name: "Edit" }))[0] as HTMLElement);
    await user.clear(screen.getByLabelText("Queue name"));
    await user.type(screen.getByLabelText("Queue name"), "Renamed");
    await user.click(screen.getByLabelText("Queue active"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.updateTicketQueue).toHaveBeenCalledWith("q1", { name: "Renamed", active: false });
  });

  it("creates a new category with type and default queue", async () => {
    const user = userEvent.setup();
    const api = baseApi({
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      createTicketCategory: vi.fn().mockResolvedValue(CATEGORIES[0]),
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ServiceDeskAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New category" }));
    await user.type(screen.getByLabelText("Key"), "hardware");
    await user.type(screen.getByLabelText("Name"), "Hardware");
    await user.selectOptions(screen.getByLabelText("Default queue"), "q1");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createTicketCategory).toHaveBeenCalledWith({
      key: "hardware",
      name: "Hardware",
      type: "incident",
      requires_approval: false,
      default_queue_id: "q1",
    });
  });

  // Cycle 12 -- Auditor A (MEDIUM), "enum values shown raw and lowercase
  // throughout the module": the category table's Type column printed
  // `incident` beside a Type select that already said "Incident".
  it("labels the category type for people, not for the database", async () => {
    mockUseApi.mockReturnValue(
      baseApi({ listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES) }) as ApiClient,
    );
    renderWithProviders(<ServiceDeskAdminPage />);

    const row = (await screen.findByText("Hardware")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Incident")).toBeInTheDocument();
    expect(within(row).queryByText("incident")).not.toBeInTheDocument();
  });

  it("says the queue list request FAILED rather than showing an empty table", async () => {
    const api = baseApi({ listTicketQueues: vi.fn().mockRejectedValue(new Error("boom")) });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ServiceDeskAdminPage />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No queues yet.")).not.toBeInTheDocument();
  });

  it("hides all write controls without ticket.admin", async () => {
    const api = baseApi({
      me: vi.fn().mockResolvedValue(ME_TECHNICIAN),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES),
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ServiceDeskAdminPage />);

    await screen.findByText("Helpdesk");
    expect(screen.queryByRole("button", { name: "New queue" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New category" })).not.toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: "Edit" })).toHaveLength(0);
  });
});

// --------------------------------------------------------------------------
// Badge vocabulary: only real backend-emitted values, `?? "muted"` fallback.
// --------------------------------------------------------------------------

describe("service desk state vocabulary", () => {
  it("maps real backend statuses to non-fallback variants", () => {
    expect(ticketStatusVariant("resolved")).toBe("success");
    expect(ticketStatusVariant("pending_approval")).toBe("warning");
    expect(ticketStatusVariant("rejected")).toBe("destructive");
  });

  it("falls back to muted for an unknown/invented status", () => {
    expect(ticketStatusVariant("not_a_real_status")).toBe("muted");
  });

  it("maps a real backend priority to a non-fallback variant", () => {
    expect(ticketPriorityVariant("urgent")).toBe("destructive");
  });

  it("falls back to muted for an unknown/invented priority", () => {
    expect(ticketPriorityVariant("not_a_real_priority")).toBe("muted");
  });

  it("maps a real backend comment visibility to a non-fallback variant", () => {
    expect(commentVisibilityVariant("internal")).toBe("info");
  });

  it("falls back to muted for an unknown/invented visibility", () => {
    expect(commentVisibilityVariant("not_a_real_visibility")).toBe("muted");
  });
});

// --------------------------------------------------------------------------
// Stat tiles: every number is a LINK to the rows behind it.
//
// "All tiles and all numbers or kpis need to be clickable and direct the user
// to the specific menus, i dont want dead tiles everywhere" -- the operator,
// 2026-08-21. `StatCard`'s required `to` makes a tile without a destination a
// compile error, which is what stops the rule decaying; these tests are what
// stops the DESTINATION decaying, because a `to` that points at a URL nothing
// reads type-checks perfectly and is exactly as dead as no link at all.
//
// So there are two kinds of assertion here on purpose: one that the tiles are
// anchors with real hrefs, and -- the load-bearing one -- that FOLLOWING each
// href actually narrows the list to the rows that tile counted.
// --------------------------------------------------------------------------

describe("service-desk stat tiles", () => {
  // Four tickets that disagree on every axis a tile counts, so a filter that
  // silently matched everything (or nothing) cannot pass.
  //   MINE:       assigned, normal, technician
  //   FREE:       unassigned, normal, technician
  //   FREE_ALERT: unassigned, urgent, alert
  //   MINE_HIGH:  assigned, high, technician
  const MINE: TicketOut = { ...TICKET_OPEN, id: "t20", number: "INC-20", assigned_to: "tech-1" };
  const FREE: TicketOut = { ...TICKET_OPEN, id: "t21", number: "INC-21", assigned_to: null };
  const FREE_ALERT: TicketOut = {
    ...TICKET_OPEN, id: "t22", number: "INC-22", assigned_to: null,
    priority: "urgent", origin: "alert",
  };
  const MINE_HIGH: TicketOut = {
    ...TICKET_OPEN, id: "t23", number: "INC-23", assigned_to: "tech-1", priority: "high",
  };

  function baseApi(tickets: TicketOut[] = [MINE, FREE, FREE_ALERT, MINE_HIGH]): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME_TECHNICIAN),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES),
      // Honours `scope=unassigned` the way the route does.
      listTickets: vi.fn(async (filters: TicketListFilters = {}) =>
        page(
          filters.scope === "unassigned" ? tickets.filter((t) => t.assigned_to === null) : tickets,
        ),
      ),
      listUsers: vi.fn().mockResolvedValue({ users: USERS }),
      listDevices: vi.fn().mockResolvedValue({ items: DEVICES, total: DEVICES.length }),
    };
  }

  // The list is mounted ON its own route so a tile's href is followed for
  // real: rendering the component bare would let a click change the URL and
  // then render nothing, which proves only that the anchor exists.
  function renderOnRoute(node: ReactElement, path: string, at = path) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[at]}>
          <Routes>
            <Route path={path} element={node} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  const tiles = () => [...document.querySelectorAll<HTMLElement>(".stat-card")];
  const tile = (label: string): HTMLAnchorElement => {
    const found = tiles().find((card) => card.querySelector(".stat-label")?.textContent === label);
    if (!found) throw new Error(`no stat tile labelled ${label}`);
    return found as HTMLAnchorElement;
  };

  it("gives every ticket-list tile a real href, not a div nobody can click", async () => {
    mockUseApi.mockReturnValue(baseApi() as ApiClient);
    renderOnRoute(<TicketListPage />, "/service-desk");

    await screen.findByRole("link", { name: "INC-20" });
    expect(tiles()).toHaveLength(4);
    for (const card of tiles()) {
      // An <a> with an href: keyboard reachable, middle-clickable, and
      // pasteable into an incident channel, which is what an operator
      // actually does with "7 unassigned" at 2am.
      expect(card.tagName).toBe("A");
      expect(card.getAttribute("href")).toBeTruthy();
    }
    expect(tile("Tickets shown")).toHaveAttribute("href", "/service-desk");
    expect(tile("Unassigned")).toHaveAttribute("href", "/service-desk?scope=unassigned");
    expect(tile("High or urgent priority")).toHaveAttribute(
      "href",
      "/service-desk?priority=high,urgent",
    );
    expect(tile("From alerts")).toHaveAttribute("href", "/service-desk?origin=alert");
  });

  it("shows exactly the unassigned tickets its own card counted when that card is followed", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(baseApi() as ApiClient);
    renderOnRoute(<TicketListPage />, "/service-desk");

    await screen.findByRole("link", { name: "INC-20" });
    expect(tile("Unassigned").querySelector(".stat-value")?.textContent).toBe("2");

    await user.click(tile("Unassigned"));

    expect(await screen.findByRole("link", { name: "INC-21" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "INC-22" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "INC-20" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "INC-23" })).not.toBeInTheDocument();
  });

  it("shows exactly the high-or-urgent tickets its own card counted when that card is followed", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(baseApi() as ApiClient);
    renderOnRoute(<TicketListPage />, "/service-desk");

    await screen.findByRole("link", { name: "INC-20" });
    expect(tile("High or urgent priority").querySelector(".stat-value")?.textContent).toBe("2");

    await user.click(tile("High or urgent priority"));

    // BOTH priorities, which is the reason the URL carries a pair: a
    // single-value filter would have shown one of these two and the card
    // would have been counting something no page could show.
    expect(await screen.findByRole("link", { name: "INC-22" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "INC-23" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "INC-20" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "INC-21" })).not.toBeInTheDocument();
  });

  it("shows exactly the alert-raised tickets its own card counted when that card is followed", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(baseApi() as ApiClient);
    renderOnRoute(<TicketListPage />, "/service-desk");

    await screen.findByRole("link", { name: "INC-20" });
    expect(tile("From alerts").querySelector(".stat-value")?.textContent).toBe("1");

    await user.click(tile("From alerts"));

    expect(await screen.findByRole("link", { name: "INC-22" })).toBeInTheDocument();
    for (const gone of ["INC-20", "INC-21", "INC-23"]) {
      expect(screen.queryByRole("link", { name: gone })).not.toBeInTheDocument();
    }
  });

  // Review wave round 3: all four tiles carried a caption and a figure and
  // nothing else, so each drew ~40px of empty band under the number while
  // neighbouring pages in the same wave read "3 still to run". Every line
  // below is a second dimension of the SAME rows the figure counts.
  it("says what each figure is made of, on every tile", async () => {
    mockUseApi.mockReturnValue(baseApi() as ApiClient);
    renderOnRoute(<TicketListPage />, "/service-desk");

    await screen.findByRole("link", { name: "INC-20" });
    // A type is a countable noun and takes a plural; a priority is not
    // ("2 urgents"), so the urgent tile's parts are the bare adjective.
    expect(tile("Tickets shown")).toHaveTextContent("4 incidents");
    expect(tile("Unassigned")).toHaveTextContent("1 of them high or urgent");
    expect(tile("High or urgent priority")).toHaveTextContent("1 high · 1 urgent");
    expect(tile("From alerts")).toHaveTextContent("1 still unassigned");
    for (const card of tiles()) {
      expect(card.querySelector(".stat-foot")?.textContent ?? "").not.toBe("");
    }
  });

  it("keeps the breakdown out of the way of a zero, on every tile", async () => {
    // The breakdown is `undefined` at zero so the zero-state label -- the one
    // line that says WHICH zero this is -- keeps the sub-line to itself.
    mockUseApi.mockReturnValue(baseApi([]) as ApiClient);
    renderOnRoute(<TicketListPage />, "/service-desk");

    await screen.findByText("No tickets found.");
    for (const card of tiles()) {
      expect(card.querySelector(".stat-foot")).toBeNull();
      expect(card.querySelector(".stat-zero")?.textContent ?? "").not.toBe("");
    }
  });

  it("says WHICH zero a zero tile is, and drops the urgent treatment for it", async () => {
    // "0 unassigned" wearing an alarm colour reads as a perfect score. The
    // zero label is what tells "nothing is waiting" apart from "nothing was
    // measured" -- the audit found nine tiles reading 0 on a page where
    // nothing had been measured at all.
    mockUseApi.mockReturnValue(baseApi([]) as ApiClient);
    renderOnRoute(<TicketListPage />, "/service-desk");

    await screen.findByText("No tickets found.");
    const unassigned = tile("Unassigned");
    expect(unassigned.querySelector(".stat-value")?.textContent).toBe("0");
    expect(unassigned).toHaveTextContent("Every ticket here has an owner");
    expect(unassigned.className).not.toContain("urgent");
  });

  it("sends My work's tiles to the same tickets in the list that can filter them", async () => {
    const api: Partial<ApiClient> = {
      me: vi.fn().mockResolvedValue(ME_TECHNICIAN),
      listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      listTickets: vi.fn().mockResolvedValue(page([MINE, MINE_HIGH])),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
    renderOnRoute(<MyWorkPage />, "/service-desk/my-work");

    await screen.findByRole("link", { name: "INC-20" });
    expect(tiles()).toHaveLength(2);
    expect(tile("Assigned to you")).toHaveAttribute("href", "/service-desk?scope=mine");
    expect(tile("High or urgent priority")).toHaveAttribute(
      "href",
      "/service-desk?scope=mine&priority=high,urgent",
    );
  });
});

