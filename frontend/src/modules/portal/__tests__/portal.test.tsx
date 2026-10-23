import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me } from "../../../lib/api";
import { MyTicketsPage } from "../MyTicketsPage";
import { NewTicketPage } from "../NewTicketPage";
import { PortalTicketDetailPage } from "../PortalTicketDetailPage";
import { portalApprovalVariant, portalTicketPriorityVariant, portalTicketStatusVariant } from "../portalState";
import type {
  PortalApprovalOut,
  PortalCategoryOut,
  PortalCommentOut,
  PortalStatusHistoryOut,
  PortalTicketDetail,
  PortalTicketSummary,
} from "../types";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch -- mirrors modules/service-desk/__tests__/serviceDesk.test.tsx.
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

// Spec §4: the `requester` role holds exactly `portal.ticket.read` and
// `portal.ticket.write` -- never any `ticket.*` code, and nothing outside
// `portal.*`.
const ME_REQUESTER: Me = {
  user_id: "req-1",
  org_id: "o1",
  email: "requester@example.com",
  display_name: "Requester",
  permissions: ["portal.ticket.read", "portal.ticket.write"],
};

const CATEGORIES: PortalCategoryOut[] = [
  { id: "cat1", key: "hardware", name: "Hardware", type: "incident" },
  { id: "cat2", key: "access_request", name: "Access request", type: "request" },
];

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

const COMMENTS: PortalCommentOut[] = [
  { id: "c1", body: "Checked the power cable.", created_at: "2026-07-01T01:00:00.000Z" },
];

const STATUS_HISTORY: PortalStatusHistoryOut[] = [
  { from_status: null, to_status: "new", created_at: "2026-07-01T00:00:00.000Z" },
  {
    from_status: "new",
    to_status: "in_progress",
    created_at: "2026-07-01T00:30:00.000Z",
  },
];

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
  comments: COMMENTS,
  status_history: STATUS_HISTORY,
  attachments: [],
  approval: null,
};

const REJECTED_APPROVAL: PortalApprovalOut = {
  state: "rejected",
  reason: "no budget",
  decided_at: "2026-07-03T00:00:00.000Z",
};

// --------------------------------------------------------------------------
// MyTicketsPage
// --------------------------------------------------------------------------

describe("MyTicketsPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_REQUESTER),
      listPortalTickets: vi.fn().mockResolvedValue([TICKET_SUMMARY]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("shows my tickets with number, subject, and status", async () => {
    renderWithProviders(<MyTicketsPage />);

    expect(await screen.findByText("INC-1")).toBeInTheDocument();
    expect(screen.getByText("VPN")).toBeInTheDocument();
    // Cycle 12 -- Auditor A (MEDIUM), "enum values shown raw and lowercase
    // throughout the module": this cell used to read `in_progress`.
    expect(screen.getByText("In progress")).toBeInTheDocument();
    expect(screen.queryByText("in_progress")).not.toBeInTheDocument();
  });

  it("never renders a queue or assignee on the ticket list", async () => {
    renderWithProviders(<MyTicketsPage />);

    await screen.findByText("INC-1");
    expect(screen.queryByText(/queue/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/assignee/i)).not.toBeInTheDocument();
  });

  it("links to the nav's two portal surfaces", async () => {
    renderWithProviders(<MyTicketsPage />);

    expect(await screen.findByRole("link", { name: "My tickets" })).toHaveAttribute(
      "href",
      "/portal",
    );
    expect(screen.getByRole("link", { name: "New ticket" })).toHaveAttribute("href", "/portal/new");
  });

  it("hides portal navigation entirely without portal.ticket.read", async () => {
    api.me = vi.fn().mockResolvedValue({ ...ME_REQUESTER, permissions: [] });
    renderWithProviders(<MyTicketsPage />);

    await screen.findByText("My tickets", { selector: "h1" });
    expect(screen.queryByRole("navigation", { name: "Portal" })).not.toBeInTheDocument();
  });

  it("reaches ticket detail via a link from the ticket list row", async () => {
    renderWithProviders(<MyTicketsPage />);

    const link = await screen.findByRole("link", { name: "INC-1" });
    expect(link).toHaveAttribute("href", "/portal/tickets/t1");
  });

  it("shows an empty-state message when the requester has no tickets", async () => {
    api.listPortalTickets = vi.fn().mockResolvedValue([]);
    renderWithProviders(<MyTicketsPage />);

    expect(await screen.findByText("You have not filed any tickets yet.")).toBeInTheDocument();
  });

  // The three states a mock never has (UI port spec §6, §8 test 5): before
  // this migration the list showed an empty table body for "still loading"
  // and "nothing filed" alike, so a requester could not tell a slow request
  // from a quiet account.
  it("says it is loading rather than showing an empty ticket list", async () => {
    api.listPortalTickets = () => new Promise<never>(() => {});
    renderWithProviders(<MyTicketsPage />);

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty ticket list", async () => {
    api.listPortalTickets = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<MyTicketsPage />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/You have not filed any tickets yet/i)).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// NewTicketPage
// --------------------------------------------------------------------------

describe("NewTicketPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_REQUESTER),
      listPortalCategories: vi.fn().mockResolvedValue(CATEGORIES),
      createPortalTicket: vi.fn().mockResolvedValue(TICKET_DETAIL),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("submits a new ticket with the exact entered fields", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "VPN broken");
    await user.type(screen.getByLabelText("Description"), "Can't connect.");
    // Urgency is v3's chip row now, not a select: a real button per option,
    // and the wire value it sets is unchanged.
    await user.click(
      screen.getByRole("button", { name: "I cannot do an important part of my job" }),
    );
    await user.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => {
      expect(api.createPortalTicket).toHaveBeenCalledWith({
        category_id: "cat1",
        subject: "VPN broken",
        description: "Can't connect.",
        priority: "high",
      });
    });
  });

  it("submits no requester_id, type, queue, or assignee field -- the form has none to send", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "VPN broken");
    await user.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(api.createPortalTicket).toHaveBeenCalled());
    const calls = (api.createPortalTicket as ReturnType<typeof vi.fn>).mock.calls;
    const body = calls[0]?.[0];
    expect(body).not.toHaveProperty("requester_id");
    expect(body).not.toHaveProperty("type");
    expect(body).not.toHaveProperty("queue_id");
    expect(body).not.toHaveProperty("assigned_to");
  });

  // Requirement #4: degrade quietly on 403 -- a secondary call (categories)
  // failing must not render an error banner or a broken page.
  it("degrades quietly when the categories call 403s", async () => {
    api.listPortalCategories = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    renderWithProviders(<NewTicketPage />);

    await screen.findByText("New ticket", { selector: "h1" });
    await waitFor(() =>
      expect(
        screen.getByText("Categories are not available (insufficient permissions)."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/^error/i)).not.toBeInTheDocument();
    // Nothing valid can be submitted without a category -- the button stays
    // disabled rather than letting a submission reach the server with no
    // category id.
    expect(screen.getByRole("button", { name: "Submit" })).toBeDisabled();
  });

  // ------------------------------------------------------------------
  // Form kernel adoption (UX remediation cycle 3).
  //
  // Auditor A (HIGH), /portal/new -> Submit: "Submit with an empty form is a
  // silent no-op -- the seventh occurrence." A disabled button that never
  // says why IS the silent no-op, so these pin the opposite behaviour: the
  // control stays live, the click produces the reason, and nothing reaches
  // the server that the requester did not mean to send.
  // ------------------------------------------------------------------

  it("keeps Submit enabled on an empty form and names every missing field", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    const submit = screen.getByRole("button", { name: "Submit" });
    expect(submit).toBeEnabled();
    await user.click(submit);

    expect(api.createPortalTicket).not.toHaveBeenCalled();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Category is required");
    expect(alert).toHaveTextContent("Subject is required");
  });

  it("treats a whitespace-only Subject as missing", async () => {
    // `" "` is truthy, which is how a blank subject slipped past the old
    // `!subject` guard and 422'd at the server instead of here.
    const user = userEvent.setup();
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "   ");
    await user.click(screen.getByRole("button", { name: "Submit" }));

    expect(api.createPortalTicket).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Subject is required");
  });

  it("files the ticket on Enter from the Subject field", async () => {
    // The controls were not in a <form> and ui.tsx's Button sets no type, so
    // the keyboard path did not exist at all on this route.
    const user = userEvent.setup();
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "VPN broken{Enter}");

    await waitFor(() => expect(api.createPortalTicket).toHaveBeenCalledTimes(1));
  });

  it("announces a bodyless 503 instead of swallowing it", async () => {
    // `{String(create.error)}` rendered a raw object at best and, for the
    // bodyless 503 a proxy returns, an empty string -- the failure the form
    // kernel's describeSubmitError exists for.
    const user = userEvent.setup();
    api.createPortalTicket = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "VPN broken");
    await user.click(screen.getByRole("button", { name: "Submit" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent?.trim()).not.toBe("");
    expect(alert).toHaveTextContent(/unavailable/i);
  });

  it("says WHY Submit is disabled when the catalog is unreachable", async () => {
    // The one disable this page keeps -- no category list means no valid
    // category id to send -- must state its reason rather than leaving the
    // requester to infer it from a dead button.
    api.listPortalCategories = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    renderWithProviders(<NewTicketPage />);

    expect(await screen.findByText(/catalog is unavailable/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit" })).toBeDisabled();
  });

  it("uploads the selected file after the ticket is created", async () => {
    const user = userEvent.setup();
    api.uploadPortalAttachment = vi.fn().mockResolvedValue({
      id: "a1",
      filename: "screenshot.png",
      byte_size: 1024,
      created_at: "2026-07-01T00:00:00.000Z",
    });
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "VPN broken");
    const file = new File(["x"], "screenshot.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("Attachments (optional)"), file);
    await user.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => {
      expect(api.uploadPortalAttachment).toHaveBeenCalledWith("t1", file);
    });
  });

  // ------------------------------------------------------------------
  // UX remediation cycles 6-8 on the end-user form.
  // ------------------------------------------------------------------

  // Auditor A (HIGH): "Ticket Description is a single-line <input> here too
  // -- the defect is systemic to ticket creation ... This is the *end-user*
  // reporting form -- the page where a non-technical employee describes what
  // is broken, pastes an error message, or lists what they tried."
  it("gives Description a textarea that keeps the newlines a requester types", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    const description = screen.getByLabelText("Description");
    expect(description.tagName).toBe("TEXTAREA");

    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "VPN broken");
    await user.type(description, "It says:{Enter}connection refused");
    await user.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() =>
      expect(api.createPortalTicket).toHaveBeenCalledWith(
        expect.objectContaining({ description: "It says:\nconnection refused" }),
      ),
    );
  });

  // Auditor A (MEDIUM): "The Category list interleaves incident and request
  // categories in one flat alphabetical list, creating near-duplicate pairs
  // ... 'Access' / 'Access request', 'Other' / 'Other request' and
  // 'Software' / 'Software request'."
  it("groups the categories into reporting a problem and requesting something", async () => {
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    const select = screen.getByLabelText("Category") as HTMLSelectElement;
    const groups = Array.from(select.querySelectorAll("optgroup")).map((g) => g.label);
    expect(groups).toEqual(["Report a problem", "Request something"]);
    // And each category sits under the right heading, from its own `type`.
    expect(select.querySelector('optgroup[label="Report a problem"] option')).toHaveTextContent(
      "Hardware",
    );
    expect(select.querySelector('optgroup[label="Request something"] option')).toHaveTextContent(
      "Access request",
    );
  });

  // A category of a type this list does not know about must still be
  // offered. Hiding it would be a worse defect than the one being fixed.
  it("still offers a category whose type it does not recognise", async () => {
    api.listPortalCategories = vi
      .fn()
      .mockResolvedValue([
        ...CATEGORIES,
        { id: "cat9", key: "odd", name: "Something else", type: "unheard_of" },
      ]);
    renderWithProviders(<NewTicketPage />);

    expect(await screen.findByRole("option", { name: "Something else" })).toBeInTheDocument();
  });

  // Auditor A (MEDIUM): "End users choose their own ticket priority from
  // bare lowercase database values, with no definitions ... self-selected
  // priority with no definitions reliably produces priority inflation."
  it("defines each priority in words rather than offering raw enum values", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    // v3's chip row (`P['portal/new']`), so the options are buttons in a named
    // group rather than a dropdown -- the finding this test pins is about the
    // WORDS, and they are the same four definitions.
    const urgency = screen.getByRole("group", { name: "How urgent is it?" });
    expect(within(urgency).queryByRole("button", { name: "urgent" })).not.toBeInTheDocument();
    expect(
      within(urgency).getByRole("button", { name: "I cannot work at all, or others are blocked too" }),
    ).toBeInTheDocument();
    // The wire value is untouched -- this is a display layer, not a second
    // vocabulary -- and the grade the chosen sentence files under is named.
    expect(
      within(urgency).getByRole("button", { name: "It is slowing me down but I can work" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Filed as Normal priority.")).toBeInTheDocument();

    await user.click(
      within(urgency).getByRole("button", { name: "I cannot work at all, or others are blocked too" }),
    );
    expect(screen.getByText("Filed as Urgent priority.")).toBeInTheDocument();
  });

  // Auditor A (LOW): "No maximum size, permitted formats, or count is stated
  // anywhere on the page ... oversized or rejected uploads will fail -- and
  // given this page's silent-failure behaviour, they will fail without a
  // message."
  it("states the attachment limit and refuses an oversized file by name", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    expect(
      screen.getByText("Attach as many files as you need. Any file type, up to 10 MB per file."),
    ).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "VPN broken");
    const huge = new File(["x"], "dump.zip", { type: "application/zip" });
    Object.defineProperty(huge, "size", { value: 11 * 1024 * 1024 });
    await user.upload(screen.getByLabelText("Attachments (optional)"), huge);
    await user.click(screen.getByRole("button", { name: "Submit" }));

    expect(api.createPortalTicket).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "dump.zip is larger than the 10 MB limit.",
    );
  });

  // Auditor A (LOW), cycle 12: "The file input sets accept: null and
  // multiple: false -- any file type, exactly one file ... Users
  // troubleshooting an issue typically have several screenshots; being able
  // to attach only one, with no explanation, forces them to file multiple
  // tickets or omit evidence."
  it("accepts several files and uploads every one of them, in order", async () => {
    const user = userEvent.setup();
    api.uploadPortalAttachment = vi.fn().mockImplementation(async (_id: string, file: File) => ({
      id: file.name,
      filename: file.name,
      byte_size: 1,
      created_at: "2026-07-01T00:00:00.000Z",
    }));
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    const input = screen.getByLabelText("Attachments (optional)");
    expect(input).toHaveAttribute("multiple");

    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "VPN broken");
    const first = new File(["a"], "one.png", { type: "image/png" });
    const second = new File(["b"], "two.png", { type: "image/png" });
    await user.upload(input, [first, second]);
    await user.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(api.uploadPortalAttachment).toHaveBeenCalledTimes(2));
    expect(api.uploadPortalAttachment).toHaveBeenNthCalledWith(1, "t1", first);
    expect(api.uploadPortalAttachment).toHaveBeenNthCalledWith(2, "t1", second);
    expect(api.createPortalTicket).toHaveBeenCalledTimes(1);
  });

  // Resuming has to survive a batch, not just a single file: re-sending the
  // ones the server already took would duplicate them on the ticket.
  it("resumes at the file that failed rather than re-uploading the ones that landed", async () => {
    const user = userEvent.setup();
    let attempt = 0;
    api.uploadPortalAttachment = vi.fn().mockImplementation(async (_id: string, file: File) => {
      attempt += 1;
      // The first file lands; the second fails once, then succeeds.
      if (attempt === 2) throw new ApiError(503, "");
      return { id: file.name, filename: file.name, byte_size: 1, created_at: "2026-07-01T00:00:00.000Z" };
    });
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "VPN broken");
    const first = new File(["a"], "one.png", { type: "image/png" });
    const second = new File(["b"], "two.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("Attachments (optional)"), [first, second]);

    await user.click(screen.getByRole("button", { name: "Submit" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/i);

    await user.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(api.uploadPortalAttachment).toHaveBeenCalledTimes(3));
    // Three calls total: one.png ONCE, two.png twice -- not one.png again.
    expect(api.uploadPortalAttachment).toHaveBeenNthCalledWith(1, "t1", first);
    expect(api.uploadPortalAttachment).toHaveBeenNthCalledWith(2, "t1", second);
    expect(api.uploadPortalAttachment).toHaveBeenNthCalledWith(3, "t1", second);
    expect(api.createPortalTicket).toHaveBeenCalledTimes(1);
  });

  // Every oversized file in the batch is named, not just the first.
  it("names each oversized file in the batch rather than only the first", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "VPN broken");
    const huge = new File(["x"], "dump.zip", { type: "application/zip" });
    Object.defineProperty(huge, "size", { value: 11 * 1024 * 1024 });
    const alsoHuge = new File(["x"], "core.dmp", { type: "application/octet-stream" });
    Object.defineProperty(alsoHuge, "size", { value: 12 * 1024 * 1024 });
    await user.upload(screen.getByLabelText("Attachments (optional)"), [huge, alsoHuge]);
    await user.click(screen.getByRole("button", { name: "Submit" }));

    expect(api.createPortalTicket).not.toHaveBeenCalled();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("dump.zip is larger than the 10 MB limit.");
    expect(alert).toHaveTextContent("core.dmp is larger than the 10 MB limit.");
  });

  // The swallowed half of this flow: the ticket is POSTed first and the file
  // second, so a failed upload used to leave a real ticket behind AND file a
  // duplicate when the requester pressed Submit again.
  it("resumes rather than filing a second ticket when the attachment upload fails", async () => {
    const user = userEvent.setup();
    api.uploadPortalAttachment = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderWithProviders(<NewTicketPage />);

    await screen.findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.type(screen.getByLabelText("Subject"), "VPN broken");
    const file = new File(["x"], "screenshot.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("Attachments (optional)"), file);

    await user.click(screen.getByRole("button", { name: "Submit" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/i);

    await user.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(api.uploadPortalAttachment).toHaveBeenCalledTimes(2));
    expect(api.createPortalTicket).toHaveBeenCalledTimes(1);
  });
});

// --------------------------------------------------------------------------
// PortalTicketDetailPage
// --------------------------------------------------------------------------

describe("PortalTicketDetailPage", () => {
  function renderDetail(ticketId: string) {
    return renderWithProviders(
      <Routes>
        <Route path="/portal/tickets/:ticketId" element={<PortalTicketDetailPage />} />
      </Routes>,
      [`/portal/tickets/${ticketId}`],
    );
  }

  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_REQUESTER),
      getPortalTicket: vi.fn().mockResolvedValue(TICKET_DETAIL),
      createPortalComment: vi.fn().mockResolvedValue(COMMENTS[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("shows status, description, public comments, and status history", async () => {
    renderDetail("t1");

    expect(await screen.findByText("INC-1: VPN")).toBeInTheDocument();
    // "In progress" appears twice (the header badge and the status-history
    // row) -- assert both are present rather than picking one. Cycle 12: it
    // was the raw `in_progress` in both places.
    expect(screen.getAllByText("In progress").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("in_progress")).not.toBeInTheDocument();
    expect(screen.getByText("VPN client won't connect.")).toBeInTheDocument();
    expect(screen.getByText("Checked the power cable.")).toBeInTheDocument();
    // Status history shows the transition (new -> in_progress) and when, but
    // never a reason -- see the dedicated C1 test below. "New" appears twice
    // (the "From" cell and the badge), so assert both are present. Cycle 12:
    // both cells printed the raw `new` before the enum display layer.
    expect(screen.getAllByText("New").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("new")).not.toBeInTheDocument();
  });

  // The resolver is built for this placement (in-ticket only) and its routes
  // (`/portal/ai/sessions`) are mounted by `ai_portal`.
  it("offers the assistant inside the ticket", async () => {
    const user = userEvent.setup();
    api.createResolverSession = vi.fn().mockResolvedValue({ id: "s1" });
    api.createResolverTurn = vi.fn().mockResolvedValue({ answer: "Try restarting the VPN client." });
    renderDetail("t1");

    await screen.findByText("INC-1: VPN");
    expect(screen.getByRole("heading", { name: "Ask about your ticket" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Ask the assistant"), "What now?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Try restarting the VPN client.")).toBeInTheDocument();
    expect(api.createResolverTurn).toHaveBeenCalledWith("s1", "What now?");
  });

  it("never shows a staff-authored status-change reason anywhere on the page (C1)", async () => {
    api.getPortalTicket = vi.fn().mockResolvedValue({
      ...TICKET_DETAIL,
      status_history: [
        { from_status: "new", to_status: "open", created_at: "2026-07-01T00:30:00.000Z" },
      ],
    });
    renderDetail("t1");

    await screen.findByText("INC-1: VPN");
    // The type itself has no `reason` field to render (see the "types carry
    // no staff-only fields" suite below) -- this additionally confirms
    // nothing renders one even if a stray value slipped through at runtime.
    expect(screen.queryByText(/INTERNAL:/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Reason")).not.toBeInTheDocument();
  });

  it("never renders a queue, an assignee, or an internal-note affordance", async () => {
    renderDetail("t1");

    await screen.findByText("INC-1: VPN");
    expect(screen.queryByText(/queue/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/assignee/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/internal/i)).not.toBeInTheDocument();
    // No visibility picker at all -- every reply is public, unconditionally.
    expect(screen.queryByLabelText(/visibility/i)).not.toBeInTheDocument();
  });

  it("renders nothing approval-shaped when there is no approval outcome", async () => {
    renderDetail("t1");

    await screen.findByText("INC-1: VPN");
    expect(screen.queryByText("Approval")).not.toBeInTheDocument();
  });

  it("shows the approval outcome when there is one", async () => {
    api.getPortalTicket = vi.fn().mockResolvedValue({ ...TICKET_DETAIL, approval: REJECTED_APPROVAL });
    renderDetail("t1");

    await screen.findByText("INC-1: VPN");
    expect(await screen.findByText(/rejected/i)).toBeInTheDocument();
    expect(await screen.findByText(/no budget/i)).toBeInTheDocument();
  });

  it("posts a reply with the exact typed body and no visibility field", async () => {
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: VPN");
    await user.type(screen.getByLabelText("Reply"), "Still broken.");
    await user.click(screen.getByRole("button", { name: "Post reply" }));

    expect(api.createPortalComment).toHaveBeenCalledWith("t1", { body: "Still broken." });
  });

  // The same single-line defect Auditor A found on the Description field of
  // /portal/new, on the control a requester answers a technician with.
  it("gives the reply box a textarea that keeps newlines", async () => {
    const user = userEvent.setup();
    renderDetail("t1");

    await screen.findByText("INC-1: VPN");
    const reply = screen.getByLabelText("Reply");
    expect(reply.tagName).toBe("TEXTAREA");

    await user.type(reply, "It says:{Enter}connection refused");
    await user.click(screen.getByRole("button", { name: "Post reply" }));

    expect(api.createPortalComment).toHaveBeenCalledWith("t1", {
      body: "It says:\nconnection refused",
    });
  });

  it("hides the reply box without portal.ticket.write", async () => {
    api.me = vi.fn().mockResolvedValue({ ...ME_REQUESTER, permissions: ["portal.ticket.read"] });
    renderDetail("t1");

    await screen.findByText("INC-1: VPN");
    expect(screen.queryByLabelText("Reply")).not.toBeInTheDocument();
  });

  it("shows an attachment the server already returned -- survives a reload (I2)", async () => {
    // Unlike an earlier version of this page, attachments come from
    // `PortalTicketDetail.attachments` (the server's own response), not from
    // component state populated only during the current visit -- so a fresh
    // page load (no upload in this test at all) already shows it.
    api.getPortalTicket = vi.fn().mockResolvedValue({
      ...TICKET_DETAIL,
      attachments: [
        { id: "a0", filename: "existing.png", byte_size: 10, created_at: "2026-07-01T00:00:00.000Z" },
      ],
    });
    renderDetail("t1");

    expect(await screen.findByText("existing.png")).toBeInTheDocument();
  });

  it("uploads an attachment and shows it once the ticket query refetches (I2)", async () => {
    const user = userEvent.setup();
    const uploadedAttachment = {
      id: "a1",
      filename: "log.txt",
      byte_size: 42,
      created_at: "2026-07-01T00:00:00.000Z",
    };
    api.uploadPortalAttachment = vi.fn().mockResolvedValue(uploadedAttachment);
    // First call (initial render) returns no attachments; the upload
    // mutation invalidates the ticket query (api.ts), and the SECOND call
    // (the refetch) is what actually carries the new attachment -- this is
    // what proves the panel renders from the server response, not from local
    // state set directly from the upload's own return value.
    api.getPortalTicket = vi
      .fn()
      .mockResolvedValueOnce(TICKET_DETAIL)
      .mockResolvedValue({ ...TICKET_DETAIL, attachments: [uploadedAttachment] });
    renderDetail("t1");

    await screen.findByText("INC-1: VPN");
    expect(screen.getByText("No attachments yet.")).toBeInTheDocument();

    const file = new File(["x"], "log.txt", { type: "text/plain" });
    await user.upload(screen.getByLabelText("Attach a file"), file);

    expect(await screen.findByText("log.txt")).toBeInTheDocument();
    expect(api.uploadPortalAttachment).toHaveBeenCalledWith("t1", file);
  });

  // Requirement #4: a secondary/degradable failure elsewhere in the page
  // must not surface as an error banner. `me` (permissions) failing here
  // mirrors that discipline: the page still renders the ticket, just without
  // write affordances gated on permissions it could not confirm.
  it("degrades quietly when the permissions call fails, still rendering ticket data", async () => {
    api.me = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    renderDetail("t1");

    expect(await screen.findByText("INC-1: VPN")).toBeInTheDocument();
    expect(screen.queryByText(/^error/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Reply")).not.toBeInTheDocument();
  });

  // The three states this page had none of before this migration: it read
  // `{ticket.data && (...)}` alone, so a slow request and a 404 both rendered
  // as a blank page next to the nav.
  it("says it is loading rather than rendering a blank page", async () => {
    api.getPortalTicket = () => new Promise<never>(() => {});
    renderDetail("t1");

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than rendering a blank page", async () => {
    api.getPortalTicket = vi.fn().mockRejectedValue(new Error("boom"));
    renderDetail("t1");

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("INC-1: VPN")).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Structural guarantee: PortalTicketSummary/PortalTicketDetail carry no
// queue/assignee/internal-note field to render in the first place (mirrors
// schema.py's own `test_portal_detail_exposes_no_staff_fields`, one layer up
// the stack -- the TypeScript type, not just the runtime object).
// --------------------------------------------------------------------------

// M4 fix: the two tests that used to live here (`Object.keys(TICKET_SUMMARY)
// .not.toContain("queue_id")`, and likewise for TICKET_DETAIL) asserted
// against hand-written fixture object literals declared earlier in THIS
// file, not against the production `PortalTicketSummary`/`PortalTicketDetail`
// interfaces -- adding `queue_id?: string` to either interface in `types.ts`
// would leave both assertions green, because nothing forced the fixtures to
// be exhaustive. A test that cannot fail from the change it claims to guard
// against is worse than no test (the same class as three other no-op tests
// already fixed on this branch). The real guarantee here is structural and
// belongs to the type checker, not a runtime assertion: every field this
// suite renders is read off `types.ts`'s own `readonly`-shaped interfaces,
// so `npx tsc --noEmit` fails the build the moment a page tries to read a
// field the interface does not declare, and `schema.py`'s
// `test_portal_detail_exposes_no_staff_fields` already pins the SAME
// guarantee, structurally, on the server side these types are meant to
// mirror.

// --------------------------------------------------------------------------
// Badge vocabulary
// --------------------------------------------------------------------------

describe("portal state vocabulary", () => {
  it("maps a real backend status to a non-fallback variant", () => {
    expect(portalTicketStatusVariant("rejected")).toBe("destructive");
  });

  it("falls back to muted for an unknown/invented status", () => {
    expect(portalTicketStatusVariant("not_a_real_status")).toBe("muted");
  });

  it("maps a real backend priority to a non-fallback variant", () => {
    expect(portalTicketPriorityVariant("urgent")).toBe("destructive");
  });

  it("maps a real approval state to a non-fallback variant", () => {
    expect(portalApprovalVariant("rejected")).toBe("destructive");
    expect(portalApprovalVariant("approved")).toBe("success");
  });

  it("falls back to muted for an unknown/invented approval state", () => {
    expect(portalApprovalVariant("not_a_real_state")).toBe("muted");
  });
});
