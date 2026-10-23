/**
 * Portal adoption of the two shared primitives — cycle 4 (`describeSubmitError`)
 * and cycle 5 (`lib/datetime`).
 *
 * WHY A SEPARATE FILE. `portal.test.tsx` pins what the portal SHOWS a
 * requester; this pins how it renders time and how it reports failure, which
 * are cross-cutting and were previously each page's own private opinion. Every
 * assertion here was falsified against the pre-adoption code before it landed:
 * the zone tests go red when the `zone` argument is dropped, the alert tests go
 * red when `describeSubmitError` is swapped back for `String(error)`.
 *
 * THE FIXTURE ZONE IS DELIBERATELY NOT UTC. Vitest runs in UTC, so a profile
 * zone of America/New_York makes "used the profile zone" and "fell back to the
 * browser" render two different strings. A test written against a UTC profile
 * would pass either way, which is the exact class of no-op test this project
 * has been bitten by.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import {
  MAX_ATTACHMENT_BYTES,
  groupPortalCategories,
  oversizedAttachmentMessage,
} from "../portalState";
import type { ApiClient, Me } from "../../../lib/api";
import { NO_DATE } from "../../../lib/datetime";
import { MyTicketsPage } from "../MyTicketsPage";
import { PortalAssistantPanel } from "../PortalAssistantPanel";
import { PortalTicketDetailPage } from "../PortalTicketDetailPage";
import type { PortalTicketDetail, PortalTicketSummary } from "../types";

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
  user_id: "req-1",
  org_id: "o1",
  email: "requester@example.com",
  display_name: "Requester",
  permissions: ["portal.ticket.read", "portal.ticket.write"],
  timezone: "America/New_York",
};

const TICKET_SUMMARY: PortalTicketSummary = {
  id: "t1",
  number: "INC-1",
  type: "incident",
  status: "in_progress",
  priority: "normal",
  subject: "VPN",
  created_at: "2026-07-01T00:00:00.000Z",
  updated_at: "2026-07-02T00:00:00.000Z",
};

const TICKET_DETAIL: PortalTicketDetail = {
  id: "t1",
  number: "INC-1",
  type: "incident",
  status: "in_progress",
  priority: "normal",
  subject: "VPN",
  description: "VPN client won't connect.",
  created_at: "2026-07-01T00:00:00.000Z",
  updated_at: "2026-07-02T00:00:00.000Z",
  comments: [{ id: "c1", body: "Checked the power cable.", created_at: "2026-07-01T01:00:00.000Z" }],
  status_history: [{ from_status: null, to_status: "new", created_at: "2026-07-01T00:30:00.000Z" }],
  attachments: [
    { id: "a1", filename: "screenshot.png", byte_size: 12, created_at: "2026-07-01T00:00:00.000Z" },
  ],
  approval: { state: "rejected", reason: "no budget", decided_at: "2026-07-03T00:00:00.000Z" },
};

/** A bodyless failure — what a proxy 503 actually looks like on the wire. */
const BODYLESS_503 = new ApiError(503, "");

function renderDetail() {
  return renderWithProviders(
    <Routes>
      <Route path="/portal/tickets/:ticketId" element={<PortalTicketDetailPage />} />
    </Routes>,
    ["/portal/tickets/t1"],
  );
}

// --------------------------------------------------------------------------
// A. Datetime
// --------------------------------------------------------------------------

describe("portal renders every time in the profile's zone", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NEW_YORK),
      listPortalTickets: vi.fn().mockResolvedValue([TICKET_SUMMARY]),
      getPortalTicket: vi.fn().mockResolvedValue(TICKET_DETAIL),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // 2026-07-02T00:00Z is 01 Jul 20:00 in New York. The browser zone under
  // Vitest is UTC, so "02 Jul 2026, 00:00" is precisely the failure.
  it("shows the ticket list's last update in the requester's zone, not the browser's", async () => {
    renderWithProviders(<MyTicketsPage />);

    expect(await screen.findByText("01 Jul 2026, 20:00")).toBeInTheDocument();
    expect(screen.queryByText("02 Jul 2026, 00:00")).not.toBeInTheDocument();
  });

  it("shows opened, updated, approval, comment, attachment and history times in the requester's zone", async () => {
    renderDetail();

    // Opened (2026-07-01T00:00Z) and the attachment share one instant.
    expect(await screen.findAllByText("30 Jun 2026, 20:00")).toHaveLength(2);
    // Last update.
    expect(screen.getByText("01 Jul 2026, 20:00")).toBeInTheDocument();
    // Approval decided.
    expect(screen.getByText("02 Jul 2026, 20:00")).toBeInTheDocument();
    // Comment posted.
    expect(screen.getByText("30 Jun 2026, 21:00")).toBeInTheDocument();
    // Status history changed.
    expect(screen.getByText("30 Jun 2026, 20:30")).toBeInTheDocument();
  });

  // Before adoption this was `new Date("").toLocaleString()` -> "Invalid Date"
  // rendered into the table as though it were a time.
  it("renders an absent time as the em dash, never 'Invalid Date'", async () => {
    api.getPortalTicket = vi.fn().mockResolvedValue({
      ...TICKET_DETAIL,
      comments: [{ id: "c1", body: "Checked the power cable.", created_at: "" }],
    });
    renderDetail();

    expect(await screen.findByText("Checked the power cable.")).toBeInTheDocument();
    // `getAllBy`: the status-history "From" cell renders the same em dash for a
    // ticket's first transition, so this must not assume uniqueness.
    expect(screen.getAllByText(NO_DATE).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// B. Errors
// --------------------------------------------------------------------------

describe("portal reports every failure as an announced sentence", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NEW_YORK),
      getPortalTicket: vi.fn().mockResolvedValue(TICKET_DETAIL),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("explains a bodyless 503 on a reply instead of rendering the error object", async () => {
    const user = userEvent.setup();
    api.createPortalComment = vi.fn().mockRejectedValue(BODYLESS_503);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    await user.type(await screen.findByLabelText("Reply"), "any update?");
    await user.click(screen.getByRole("button", { name: "Post reply" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert).not.toHaveTextContent(/ApiError/);
  });

  it("translates a 422 validation payload on a reply rather than printing it", async () => {
    const user = userEvent.setup();
    api.createPortalComment = vi
      .fn()
      .mockRejectedValue(
        new ApiError(422, JSON.stringify({ detail: [{ loc: ["body", "body"], msg: "too long" }] })),
      );
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    await user.type(await screen.findByLabelText("Reply"), "any update?");
    await user.click(screen.getByRole("button", { name: "Post reply" }));

    // `loc: ["body", "body"]` is the real shape for this route -- the request
    // part and the field share a name -- and `describeValidation` drops the
    // request part, so the sentence is the server's `msg` and nothing else.
    // What matters is that the JSON envelope never reaches the screen.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("too long");
    expect(alert).not.toHaveTextContent(/detail|\{|loc/);
  });

  it("explains a failed attachment upload", async () => {
    const user = userEvent.setup();
    api.uploadPortalAttachment = vi.fn().mockRejectedValue(BODYLESS_503);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    const input = await screen.findByLabelText("Attach a file");
    await user.upload(input, new File(["x"], "note.txt", { type: "text/plain" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/i);
  });

  // C: the swallowed one. `void download(...)` had no rejection path at all,
  // so a refused download produced an unhandled promise rejection and a page
  // that looked untouched.
  it("tells the requester when a download is refused instead of silently doing nothing", async () => {
    const user = userEvent.setup();
    api.downloadPortalAttachment = vi.fn().mockRejectedValue(new ApiError(403, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Download" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You do not have permission to do that.",
    );
  });
});

describe("PortalAssistantPanel failure", () => {
  // `caught.message` is "" for a bodyless ApiError, and `error !== ""` gated
  // the render — so the assistant failed and said absolutely nothing.
  it("announces a bodyless 503 rather than rendering nothing at all", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({
      createResolverSession: vi.fn().mockRejectedValue(BODYLESS_503),
    } as unknown as ApiClient);
    renderWithProviders(<PortalAssistantPanel />);

    await user.type(screen.getByLabelText("Ask the assistant"), "why is my ticket open?");
    await user.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/i);
  });
});

// --------------------------------------------------------------------------
// Cycles 6/7 on the portal: the pure halves of the category grouping and the
// attachment limit.
// --------------------------------------------------------------------------

describe("groupPortalCategories", () => {
  // The finding is the FLAT list: "Access" landed directly beside "Access
  // request" with nothing on screen to tell them apart, even though
  // `PortalCategoryOut.type` already carried the distinction.
  it("splits the flat list back into the two questions it was hiding", () => {
    const groups = groupPortalCategories([
      { id: "1", type: "incident", name: "Access" },
      { id: "2", type: "request", name: "Access request" },
      { id: "3", type: "incident", name: "Software" },
    ]);

    expect(groups.map((g) => g.label)).toEqual(["Report a problem", "Request something"]);
    expect(groups[0]?.categories.map((c) => c.name)).toEqual(["Access", "Software"]);
    expect(groups[1]?.categories.map((c) => c.name)).toEqual(["Access request"]);
  });

  // Hiding a category the server offers would be a worse defect than the one
  // being fixed: a requester who needs it simply could not file.
  it("keeps a category of an unrecognised type rather than dropping it", () => {
    const groups = groupPortalCategories([{ id: "9", type: "unheard_of", name: "Something else" }]);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.label).toBe("Other");
    expect(groups[0]?.categories[0]?.name).toBe("Something else");
  });

  it("emits no empty headings", () => {
    const groups = groupPortalCategories([{ id: "1", type: "incident", name: "Access" }]);
    expect(groups.map((g) => g.label)).toEqual(["Report a problem"]);
  });
});

describe("oversizedAttachmentMessage", () => {
  // Mirrors `service_desk/threads.py`'s MAX_ATTACHMENT_BYTES (10 MiB). The
  // client is allowed to be pessimistic, never permissive.
  it("names the file rather than reporting a bare limit", () => {
    expect(oversizedAttachmentMessage({ name: "dump.zip", size: 11 * 1024 * 1024 })).toBe(
      "dump.zip is larger than the 10 MB limit. Please attach a smaller file.",
    );
  });

  it("passes a file exactly on the limit, which the server accepts", () => {
    expect(oversizedAttachmentMessage({ name: "ok.png", size: MAX_ATTACHMENT_BYTES })).toBeNull();
  });
});
