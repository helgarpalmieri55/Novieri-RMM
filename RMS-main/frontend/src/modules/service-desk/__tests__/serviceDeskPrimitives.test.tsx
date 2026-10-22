/**
 * Service-desk adoption of the two shared primitives — cycle 4
 * (`describeSubmitError`) and cycle 5 (`lib/datetime`).
 *
 * WHY A SEPARATE FILE. `serviceDesk.test.tsx` pins what the module DOES; this
 * pins how it renders time and how it reports failure — cross-cutting concerns
 * that were previously each panel's private opinion (`String(error)` in eight
 * places, `new Date(x).toLocaleString()` in three).
 *
 * THE FIXTURE ZONE IS DELIBERATELY NOT UTC. Vitest runs in UTC, so a profile
 * zone of America/New_York makes "used the profile zone" and "fell back to the
 * browser" render two different strings. A test written against a UTC profile
 * would pass either way — the no-op class this project has been bitten by.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me } from "../../../lib/api";
import { NO_DATE } from "../../../lib/datetime";
import { ServiceDeskAdminPage } from "../ServiceDeskAdminPage";
import { TicketCopilotPanel } from "../TicketCopilotPanel";
import { TicketDetailPage } from "../TicketDetailPage";
import { devicePickerOptions, nameForOption, userPickerOptions } from "../pickerOptions";
import { statusChangeWarning } from "../serviceDeskState";
import type { AttachmentOut, CommentOut, QueueOut, StatusHistoryOut, TicketOut } from "../types";

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

const ME_NEW_YORK: Me = {
  user_id: "tech-1",
  org_id: "o1",
  email: "tech@example.com",
  display_name: "Technician",
  permissions: ["ticket.read", "ticket.write", "ticket.assign", "ticket.approve", "ticket.admin"],
  timezone: "America/New_York",
};

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
];

const TICKET_OPEN: TicketOut = {
  id: "t1",
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

const ATTACHMENTS: AttachmentOut[] = [
  {
    id: "a1",
    ticket_id: "t1",
    filename: "screenshot.png",
    byte_size: 12,
    uploaded_by: "tech-1",
    created_at: "2026-07-02T00:00:00.000Z",
  },
];

const STATUS_HISTORY: StatusHistoryOut[] = [
  {
    id: "h1",
    ticket_id: "t1",
    from_status: "new",
    to_status: "open",
    actor_type: "user",
    actor_id: "tech-1",
    reason: "Started work",
    created_at: "2026-07-01T00:30:00.000Z",
  },
];

/** A bodyless failure — what a proxy 503 actually looks like on the wire. */
const BODYLESS_503 = new ApiError(503, "");

function detailApi(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(ME_NEW_YORK),
    listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
    listTicketCategories: vi.fn().mockResolvedValue([]),
    getTicket: vi.fn().mockResolvedValue(TICKET_OPEN),
    listTicketComments: vi.fn().mockResolvedValue(COMMENTS),
    listTicketAttachments: vi.fn().mockResolvedValue(ATTACHMENTS),
    listTicketStatusHistory: vi.fn().mockResolvedValue(STATUS_HISTORY),
    // The two directories the cycle-6 pickers read. Not service-desk routes:
    // both carry their own server-side permissions.
    listUsers: vi.fn().mockResolvedValue({
      users: [
        {
          id: "tech-2",
          email: "tessa@example.com",
          display_name: "Tessa Technician",
          status: "active",
          roles: [],
        },
      ],
    }),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    // `TicketSlaPanel` (modules/sla, outside this cluster) renders its own
    // `role="alert"` when its query fails, and an unmocked method rejects. Left
    // pending so this page contributes exactly one alert -- the one under test.
    getTicketSla: () => new Promise<never>(() => {}),
    ...overrides,
  };
}

function renderDetail(api: Partial<ApiClient>) {
  mockUseApi.mockReturnValue(api as ApiClient);
  return renderWithProviders(
    <Routes>
      <Route path="/service-desk/tickets/:ticketId" element={<TicketDetailPage />} />
    </Routes>,
    ["/service-desk/tickets/t1"],
  );
}

// --------------------------------------------------------------------------
// A. Datetime
// --------------------------------------------------------------------------

describe("service desk renders every time in the profile's zone", () => {
  it("shows comment, attachment and status-history times in the technician's zone", async () => {
    renderDetail(detailApi());

    // 2026-07-01T01:00Z -> 30 Jun 21:00 New York. Under Vitest the browser
    // zone is UTC, so "01 Jul 2026, 01:00" is exactly the regression.
    expect(await screen.findByText("30 Jun 2026, 21:00")).toBeInTheDocument();
    expect(screen.queryByText("01 Jul 2026, 01:00")).not.toBeInTheDocument();
    // Attachment uploaded 2026-07-02T00:00Z.
    expect(screen.getByText("01 Jul 2026, 20:00")).toBeInTheDocument();
    // Status changed 2026-07-01T00:30Z.
    expect(screen.getByText("30 Jun 2026, 20:30")).toBeInTheDocument();
  });

  // Before adoption this was `new Date("").toLocaleString()` -> "Invalid Date"
  // rendered into the table as though it were a time.
  it("renders an absent time as the em dash, never 'Invalid Date'", async () => {
    renderDetail(
      detailApi({
        listTicketComments: vi.fn().mockResolvedValue([{ ...COMMENTS[0], created_at: "" }]),
      }),
    );

    expect(await screen.findByText("Checked the power cable.")).toBeInTheDocument();
    expect(screen.getAllByText(NO_DATE).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// B. Errors
// --------------------------------------------------------------------------

describe("service desk reports every failure as an announced sentence", () => {
  it("explains a bodyless 503 on Save changes instead of rendering the error object", async () => {
    const user = userEvent.setup();
    renderDetail(detailApi({ updateTicket: vi.fn().mockRejectedValue(BODYLESS_503) }));

    await user.type(await screen.findByLabelText("Subject"), " again");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert).not.toHaveTextContent(/ApiError/);
  });

  it("translates a 422 validation payload on a status change rather than printing it", async () => {
    const user = userEvent.setup();
    renderDetail(
      detailApi({
        changeTicketStatus: vi
          .fn()
          .mockRejectedValue(
            new ApiError(
              422,
              JSON.stringify({ detail: [{ loc: ["body", "to_status"], msg: "not a legal transition" }] }),
            ),
          ),
      }),
    );

    await user.selectOptions(await screen.findByLabelText("Status"), "resolved");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("To status: not a legal transition");
    expect(alert).not.toHaveTextContent(/detail|loc/);
  });

  it("explains a refused assignment", async () => {
    const user = userEvent.setup();
    renderDetail(detailApi({ assignTicket: vi.fn().mockRejectedValue(new ApiError(403, "")) }));

    // Cycle 6: the assignee is chosen from the directory now, so this file's
    // fixture serves one through `listUsers`.
    await user.click(await screen.findByLabelText("Assigned to"));
    await user.click(await screen.findByRole("option", { name: /Tessa Technician/ }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You do not have permission to do that.",
    );
  });

  it("explains a failed comment post", async () => {
    const user = userEvent.setup();
    renderDetail(detailApi({ createTicketComment: vi.fn().mockRejectedValue(BODYLESS_503) }));

    await user.type(await screen.findByLabelText("Message"), "on it");
    await user.click(screen.getByRole("button", { name: "Send reply" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/i);
  });

  it("explains a failed attachment upload", async () => {
    const user = userEvent.setup();
    renderDetail(detailApi({ uploadTicketAttachment: vi.fn().mockRejectedValue(BODYLESS_503) }));

    await user.upload(
      await screen.findByLabelText("Attach a file"),
      new File(["x"], "note.txt", { type: "text/plain" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/i);
  });

  // C: the swallowed one. `void download(...)` had no rejection path at all,
  // so a refused download produced an unhandled promise rejection and a page
  // that looked exactly as it had before the click.
  it("tells the technician when a download is refused instead of silently doing nothing", async () => {
    const user = userEvent.setup();
    renderDetail(
      detailApi({ downloadTicketAttachment: vi.fn().mockRejectedValue(new ApiError(403, "")) }),
    );

    await user.click(await screen.findByRole("button", { name: "Download" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You do not have permission to do that.",
    );
  });
});

// --------------------------------------------------------------------------
// Admin: errors, and the stale-error-on-reopen hole
// --------------------------------------------------------------------------

describe("ServiceDeskAdminPage failure reporting", () => {
  function adminApi(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME_NEW_YORK),
      listTicketQueues: vi.fn().mockResolvedValue([]),
      listCannedResponses: vi.fn().mockResolvedValue([]),
      listTicketCategories: vi.fn().mockResolvedValue([]),
      ...overrides,
    };
  }

  it("explains a bodyless 503 when creating a queue instead of rendering the error object", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(
      adminApi({ createTicketQueue: vi.fn().mockRejectedValue(BODYLESS_503) }) as ApiClient,
    );
    renderWithProviders(<ServiceDeskAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New queue" }));
    await user.type(screen.getByLabelText("Key"), "helpdesk");
    await user.type(screen.getByLabelText("Name"), "Helpdesk");
    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert).not.toHaveTextContent(/ApiError/);
  });

  // C: this panel stays mounted with the dialog shut, so without
  // `create.reset()` on close the NEXT open announced the refusal of a request
  // nobody had made yet. TicketListPage already fixed this for itself.
  it("does not announce the previous failure when the New queue dialog is reopened", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(
      adminApi({ createTicketQueue: vi.fn().mockRejectedValue(BODYLESS_503) }) as ApiClient,
    );
    renderWithProviders(<ServiceDeskAdminPage />);

    await user.click(await screen.findByRole("button", { name: "New queue" }));
    await user.type(screen.getByLabelText("Key"), "helpdesk");
    await user.type(screen.getByLabelText("Name"), "Helpdesk");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByRole("alert");

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "New queue" }));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// TicketCopilotPanel
// --------------------------------------------------------------------------

describe("TicketCopilotPanel failure", () => {
  beforeEach(() => {
    mockUseApi.mockReset();
  });

  // `caught.message` is "" for a bodyless ApiError, and `error !== ""` gated
  // the render — so the copilot failed and said absolutely nothing.
  it("announces a bodyless 503 rather than rendering nothing at all", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({
      createCopilotSession: vi.fn().mockRejectedValue(BODYLESS_503),
    } as unknown as ApiClient);
    renderWithProviders(<TicketCopilotPanel ticketId="t1" />);

    await user.type(screen.getByLabelText("Ask the copilot"), "what next?");
    await user.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/i);
  });
});

// --------------------------------------------------------------------------
// C. Picker options and lifecycle warnings — the pure halves of cycles 6
// and 8, pinned here rather than only through a DOM tree, because the
// SUBLABEL is what carries the safety and the two confirmation sentences
// must not be interchangeable.
// --------------------------------------------------------------------------

describe("picker options", () => {
  it("puts os family and agent state in the device sublabel", () => {
    const [option] = devicePickerOptions([
      {
        id: "dev-1",
        hostname: "WKS-042",
        fqdn: null,
        os_family: "linux",
        os_version: "",
        architecture: "",
        device_group_id: null,
        status: "active",
        agent_status: "online",
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
      },
    ]);

    expect(option?.label).toBe("WKS-042");
    expect(option?.sublabel).toBe("linux · online");
  });

  it("says 'no agent' rather than rendering a dangling separator", () => {
    const [option] = devicePickerOptions([
      {
        id: "sw-1",
        hostname: null,
        fqdn: "switch.example.com",
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
        asset_type: "network",
        source: "manual",
        // A switch somebody typed in: `manual` is how, `operator` is who.
        provenance: "operator",
        description: null,
        primary_ip: null,
        owner: null,
        location: null,
      },
    ]);

    // Falls back to the fqdn for a device with no hostname, and never emits
    // a bare " · ".
    expect(option?.label).toBe("switch.example.com");
    expect(option?.sublabel).toBe("no agent");
  });

  it("flags an account that is not active, so work is not assigned to somebody who left", () => {
    const options = userPickerOptions([
      { id: "u1", email: "a@example.com", display_name: "Active Person", status: "active", roles: [] },
      { id: "u2", email: "b@example.com", display_name: "Gone Person", status: "disabled", roles: [] },
    ]);

    expect(options[0]?.sublabel).toBe("a@example.com");
    expect(options[1]?.sublabel).toBe("b@example.com · disabled");
  });

  it("falls back to the id rather than to nothing when a name cannot be resolved", () => {
    const options = userPickerOptions([
      { id: "u1", email: "a@example.com", display_name: "Active Person", status: "active", roles: [] },
    ]);

    expect(nameForOption(options, "u1")).toBe("Active Person");
    expect(nameForOption(options, "ghost")).toBe("ghost");
    expect(nameForOption(options, null)).toBeNull();
  });
});

describe("statusChangeWarning", () => {
  // lifecycle.py: `cancelled` and `rejected` are FINAL_STATUSES, "the subset
  // that nothing ever leaves".
  it("warns that nothing leaves a final status", () => {
    const warning = statusChangeWarning("cancelled");
    expect(warning?.destructive).toBe(true);
    expect(warning?.body).toMatch(/can never be reopened/);
  });

  // ...while `closed` is terminal for EDITING and deliberately reopenable.
  // Saying "cannot be undone" here would be a lie, and a product that lies
  // in its confirmations teaches people to click through them.
  it("does not claim closing is irreversible, because it is not", () => {
    const warning = statusChangeWarning("closed");
    expect(warning?.destructive).toBe(false);
    expect(warning?.body).toMatch(/can be reopened/);
    expect(warning?.body).not.toMatch(/never/);
  });

  it("does not interrupt the statuses that have a reopen edge", () => {
    expect(statusChangeWarning("resolved")).toBeNull();
    expect(statusChangeWarning("fulfilled")).toBeNull();
    expect(statusChangeWarning("open")).toBeNull();
  });
});
