import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../../../lib/api";
import type { Me } from "../../../lib/api";
import { NO_DATE } from "../../../lib/datetime";
import type { CategoryOut } from "../../service-desk/types";
import { EmailIntakeAdminPage } from "../EmailIntakeAdminPage";
import type { InboundMessageOut, MailboxOut, MessageLogFilters, MessageLogOut } from "../types";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch -- mirrors modules/notification/__tests__/notification.test.tsx.
const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderAdmin(initialPath = "/email-intake") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <EmailIntakeAdminPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The tile whose label reads exactly `label`. */
function tile(label: string): HTMLElement {
  const found = [...document.querySelectorAll<HTMLElement>(".stat-card")].find(
    (card) => card.querySelector(".stat-label")?.textContent === label,
  );
  if (!found) throw new Error(`no stat tile labelled "${label}"`);
  return found;
}

// Spec §9: both permissions are seeded to super-admin/org-admin only, so a
// technician holds neither -- these are the only two shapes this page's
// gating logic distinguishes.
const ME_READ_ONLY: Me = {
  user_id: "reader-1",
  org_id: "o1",
  email: "reader@example.com",
  display_name: "Reader",
  permissions: ["email_intake.read"],
};

const ME_ADMIN: Me = {
  ...ME_READ_ONLY,
  user_id: "admin-1",
  display_name: "Admin",
  permissions: ["email_intake.read", "email_intake.admin"],
};

// `has_credential` is TRUE for every mailbox the backend can produce
// (`email_mailbox.credential_id` is NOT NULL -- there is no unauthenticated
// IMAP, schema.py's docstring). The `false` row below is therefore not a
// realistic API response; it exists so that "Configured" being RENDERED FROM
// THE FLAG is observable at all. Hardcoding the word passes against the
// first row and fails against the second.
const HEALTHY: MailboxOut = {
  id: "mb1",
  address: "helpdesk@acme.test",
  host: "imap.acme.test",
  port: 993,
  use_ssl: true,
  folder: "INBOX",
  username: "helpdesk@acme.test",
  has_credential: true,
  auth_kind: "password",
  credential_state: "ok",
  default_category_id: "cat-1",
  reopen_window_days: 14,
  active: true,
  last_polled_at: "2026-07-30T09:00:00.000Z",
  last_error: null,
  created_at: "2026-01-01T00:00:00.000Z",
};

// A mailbox that has STOPPED WORKING. A poison message stops its own mailbox
// by design (poller.py: the poll aborts, `last_error` names the stage, the
// sweep moves on), so this row is the only signal an operator ever gets.
const BROKEN: MailboxOut = {
  ...HEALTHY,
  id: "mb2",
  address: "support@acme.test",
  username: "support@acme.test",
  has_credential: false,
  last_polled_at: "2026-07-31T08:15:00.000Z",
  last_error: "login failed: rejected",
};

const CATEGORIES: CategoryOut[] = [
  {
    id: "cat-1",
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
];

function message(overrides: Partial<InboundMessageOut> = {}): InboundMessageOut {
  return {
    id: "im1",
    mailbox_id: "mb1",
    message_id: "<one@acme.test>",
    from_address: "alice@acme.test",
    subject: "Printer trouble",
    received_at: "2026-07-31T08:00:00.000Z",
    created_at: "2026-07-31T08:00:01.000Z",
    disposition: "created",
    reason: "",
    ticket_id: "t1",
    body_text: "The printer on 2 is jammed.",
    body_truncated: false,
    ...overrides,
  };
}

// THREE ROWS ON THE PAGE, 300 IN THE SCOPE, 42 OF THEM QUARANTINED. The
// three numbers are deliberately all different: a page tally would read "3",
// a `total` tally would read "300", and only the API's own
// `quarantined_count` reads 42.
function messageLog(overrides: Partial<MessageLogOut> = {}): MessageLogOut {
  return {
    messages: [message(), message({ id: "im2" }), message({ id: "im3" })],
    total: 300,
    quarantined_count: 42,
    ...overrides,
  };
}

function baseApi(me: Me): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(me),
    listEmailMailboxes: vi.fn().mockResolvedValue([HEALTHY, BROKEN]),
    listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES),
    listEmailIntakeMessages: vi.fn().mockResolvedValue(messageLog()),
    getEmailOAuthApp: vi.fn().mockResolvedValue({
      tenant_id: "",
      client_id: "",
      has_secret: false,
      redirect_uri: null,
    }),
  };
}

describe("email-intake admin", () => {
  // ------------------------------------------------------------------
  // 1. The mailbox form never renders a stored password back.
  // ------------------------------------------------------------------

  it("never renders a stored password back into the mailbox form", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(baseApi(ME_ADMIN) as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Edit helpdesk@acme.test" }));

    // The only password control is an empty, write-only input for setting a
    // NEW one. A form that round-tripped the stored secret through the
    // browser would be invariant #3 in a different coat.
    const password = screen.getByLabelText("New password");
    expect(password).toHaveValue("");
    expect(password).toHaveAttribute("type", "password");

    // Typing one and abandoning the dialog must not leave it in component
    // state to be re-rendered the next time the form opens.
    await user.type(password, "hunter2");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Edit helpdesk@acme.test" }));
    expect(screen.getByLabelText("New password")).toHaveValue("");
  });

  it("says whether a password is configured, from has_credential", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();

    const healthy = await screen.findByRole("row", { name: /helpdesk@acme\.test/ });
    // The column is "Credentials" with "Set"/"Not set" now (auditor A, MEDIUM:
    // a list-view column headed PASSWORD implies the value is retrievable).
    expect(within(healthy).getByText("Set")).toBeInTheDocument();
    const broken = screen.getByRole("row", { name: /support@acme\.test/ });
    expect(within(broken).getByText("Not set")).toBeInTheDocument();
    // The COLUMN is the finding, not only its cells: "a column headed PASSWORD
    // in a list view is a design smell regardless of its contents: it invites
    // shoulder-surfing, it will appear in screenshots and screen shares, and
    // it implies the value is retrievable". Without this the cells could read
    // "Set"/"Not set" under a header that still says PASSWORD.
    expect(screen.getByRole("columnheader", { name: "Credentials" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /password/i })).toBeNull();
  });

  it("omits the password from a PATCH that did not set a new one", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const update = vi.fn().mockResolvedValue(HEALTHY);
    api.updateEmailMailbox = update;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Edit helpdesk@acme.test" }));
    const folder = screen.getByLabelText("Folder");
    await user.clear(folder);
    await user.type(folder, "Support");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(update).toHaveBeenCalled());
    const [id, body] = update.mock.calls[0] as [string, Record<string, unknown>];
    expect(id).toBe("mb1");
    expect(body.folder).toBe("Support");
    // Not `password: ""` and not `password: null` -- MailboxUpdate treats
    // absent as "leave the stored password alone" (schema.py), and there is
    // no way to clear it, because `credential_id` is NOT NULL.
    expect("password" in body).toBe(false);
  });

  // ------------------------------------------------------------------
  // 2. The quarantined count comes from the API, over the whole scope.
  // ------------------------------------------------------------------

  it("shows the API's quarantined count, not a tally of the current page", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();

    // 3 rows on this page, 300 in the filtered list, 42 quarantined.
    expect(await screen.findByText("42 quarantined")).toBeInTheDocument();
    expect(screen.queryByText("3 quarantined")).toBeNull();
    expect(screen.queryByText("0 quarantined")).toBeNull();
  });

  it("labels the quarantined count as covering the scope, not the rows below", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listEmailIntakeMessages = vi
      .fn()
      .mockImplementation((filters: MessageLogFilters = {}) =>
        Promise.resolve(
          messageLog(
            filters.disposition === "created"
              ? { messages: [message()], total: 1, quarantined_count: 42 }
              : {},
          ),
        ),
      );
    mockUseApi.mockReturnValue(api as ApiClient);
    const user = userEvent.setup();
    renderAdmin();

    // `disposition` narrows the LIST but deliberately not the COUNT
    // (messages.py). The label has to say so, or the number reads as a lie
    // the moment an operator narrows the view: filtering to `created` still
    // reports 42, and "in this scope" is what makes that correct rather
    // than baffling.
    await user.selectOptions(await screen.findByLabelText("Filter by disposition"), "created");
    await waitFor(() => expect(screen.getByText("42 quarantined")).toBeInTheDocument());
    expect(screen.getByText(/in this scope/i)).toBeInTheDocument();
  });

  // ------------------------------------------------------------------
  // 3. Filtering by disposition is a server request, not a client filter.
  // ------------------------------------------------------------------

  it("refetches with the disposition filter instead of filtering the page client-side", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_READ_ONLY);
    // The quarantined page shares no row id, sender or subject with the
    // unfiltered page: a client-side filter over the rows already fetched
    // could never produce it, because none of them is quarantined.
    const listMessages = vi
      .fn()
      .mockImplementation((filters: MessageLogFilters = {}) =>
        Promise.resolve(
          filters.disposition === "quarantined"
            ? {
                messages: [
                  message({
                    id: "im9",
                    from_address: "stranger@elsewhere.test",
                    subject: "Who are you",
                    disposition: "quarantined",
                    reason: "unknown sender: stranger@elsewhere.test",
                    ticket_id: null,
                  }),
                ],
                total: 1,
                quarantined_count: 42,
              }
            : messageLog(),
        ),
      );
    api.listEmailIntakeMessages = listMessages;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await screen.findAllByText("Printer trouble");
    await user.selectOptions(screen.getByLabelText("Filter by disposition"), "quarantined");

    // The server-filtered row is on screen and the unfiltered ones are gone
    // -- neither is achievable by filtering the first response's array.
    expect(await screen.findByText("stranger@elsewhere.test")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Printer trouble")).toBeNull());
    expect(listMessages).toHaveBeenCalledWith(
      expect.objectContaining({ disposition: "quarantined" }),
    );
  });

  it("puts disposition on the query string of the real request", async () => {
    // The component test above proves the FILTER goes to the server; this
    // proves what the server actually receives. `?disposition=` is the
    // contract api.py's `disposition: Disposition | None = Query(...)`
    // enforces, and a client that posted it in a body, or spelled it
    // `status`, would satisfy the mock and fail here.
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ messages: [], total: 0, quarantined_count: 0 }), {
          status: 200,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "tok", vi.fn());
    await client.listEmailIntakeMessages({ disposition: "quarantined", limit: 50 });
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("/email-intake/messages?");
    expect(url).toContain("disposition=quarantined");
    vi.unstubAllGlobals();
  });

  // ------------------------------------------------------------------
  // The two signals an operator has no other way to learn.
  // ------------------------------------------------------------------

  it("surfaces last_error and last_polled_at per mailbox", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();

    const broken = await screen.findByRole("row", { name: /support@acme\.test/ });
    // A poison message stops its own mailbox by design; nothing else tells
    // the operator that this mailbox has gone quiet.
    expect(within(broken).getByText("login failed: rejected")).toBeInTheDocument();
    // One product-wide format (lib/datetime.ts), no longer the raw ISO
    // string this column used to print straight from the API.
    expect(within(broken).getByTitle("31 Jul 2026, 08:15")).toBeInTheDocument();

    const healthy = screen.getByRole("row", { name: /helpdesk@acme\.test/ });
    expect(within(healthy).queryByText(/failed/)).toBeNull();
  });

  it("says when a message body is only an excerpt", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listEmailIntakeMessages = vi.fn().mockResolvedValue(
      messageLog({
        messages: [
          message({ id: "long", subject: "Long one", body_truncated: true }),
          message({ id: "short", subject: "Short one", body_truncated: false }),
        ],
      }),
    );
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    // The list carries a 4 KiB excerpt (messages.BODY_EXCERPT_CHARS), never
    // the whole body -- an operator reading a quarantined message must know
    // the text can stop mid-sentence.
    const truncated = await screen.findByRole("row", { name: /Long one/ });
    expect(within(truncated).getByText(/truncated/i)).toBeInTheDocument();
    const whole = screen.getByRole("row", { name: /Short one/ });
    expect(within(whole).queryByText(/truncated/i)).toBeNull();
  });

  // ------------------------------------------------------------------
  // Gating and degradation, matching the notification module's precedent.
  // ------------------------------------------------------------------

  it("hides mutation controls without email_intake.admin", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();

    await screen.findByText("helpdesk@acme.test");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Add mailbox" })).toBeNull());
    expect(screen.queryByRole("button", { name: "Edit helpdesk@acme.test" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Test helpdesk@acme.test" })).toBeNull();
  });

  it("degrades quietly when the message log 403s", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listEmailIntakeMessages = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(
      await screen.findByText("Messages not available (insufficient permissions)."),
    ).toBeInTheDocument();
    // The sibling panel is independent.
    expect(screen.getByText("helpdesk@acme.test")).toBeInTheDocument();
  });

  it("reports a failed connection test inline without an error banner", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.testEmailMailbox = vi
      .fn()
      .mockResolvedValue({ ok: false, detail: "tls failed: certificate-invalid" });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Test helpdesk@acme.test" }));
    expect(await screen.findByText("tls failed: certificate-invalid")).toBeInTheDocument();
  });

  // ------------------------------------------------------------------
  // The three states a mock never has (UI port spec §6/§8): loading, empty
  // and error must be DISTINGUISHABLE, not all render as a blank table.
  // ------------------------------------------------------------------

  it("says the mailbox list is loading rather than showing an empty table", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listEmailMailboxes = vi.fn().mockImplementation(() => new Promise<never>(() => {}));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(/not available/i)).toBeNull();
  });

  // FALSIFIED GUARD: before MailboxList gained a `failed` branch, a non-403
  // error rendered nothing -- no 403 text (it only fires on `instanceof
  // ApiError && status === 403`), no row, indistinguishable from both
  // "still loading" and "no mailboxes configured". This is the test that
  // failed against the pre-fix component (confirmed in the session report):
  // reverting MailboxList's `failed` branch times this test out.
  it("says the mailbox list did NOT load on a genuine failure, distinctly from a 403 or an empty list", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listEmailMailboxes = vi.fn().mockRejectedValue(new Error("boom"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("Mailboxes not available (insufficient permissions).")).toBeNull();
    expect(screen.queryByText(/No mailboxes configured/i)).toBeNull();
  });
});

// ---------------------------------------------------------------------
// The form kernel on /email-intake → Add mailbox (UX remediation cycle 3).
//
// Two findings, both MEDIUM, and the second is the auditors' THIRD sighting
// of the same shared-modal defect:
//   -- Mailbox "Address" is `type="text"`, not `type="email"`, and nothing
//      checked its shape.
//   -- "Add mailbox is the same shared modal: third confirmation of the four
//      shared gaps (not a <form>, no required)".
// Each test below was confirmed to FAIL against the pre-fix component: with
// `disabled={pending || !address || !host || !username || !categoryId ||
// (!editing && !password)}` the Save control is disabled, the click is
// swallowed, no `role="alert"` is rendered, and the Address input has no
// `type` attribute at all.
// ---------------------------------------------------------------------

describe("email-intake mailbox form", () => {
  async function openAddMailbox() {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const createMailbox = vi.fn().mockResolvedValue(HEALTHY);
    api.createEmailMailbox = createMailbox;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Add mailbox" }));
    return { user, createMailbox };
  }

  it("keeps Save clickable on an empty form and names every missing field", async () => {
    const { user, createMailbox } = await openAddMailbox();

    // THE WHOLE POINT: the control moves, and the refusal is something the
    // user can read. A disabled button that never says why IS the no-op.
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();

    await user.click(save);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Address is required");
    expect(alert).toHaveTextContent("IMAP host is required");
    expect(alert).toHaveTextContent("Username is required");
    expect(alert).toHaveTextContent("Password is required");
    expect(createMailbox).not.toHaveBeenCalled();
  });

  it("refuses an address that is not an email address", async () => {
    // The address is not decoration: `intake.py` matches inbound senders
    // against it and `poller.py` logs in as it, so "helpdesk" produced a
    // mailbox that polls, fails, and reports `last_error` on a row somebody
    // has to go looking for.
    const { user, createMailbox } = await openAddMailbox();

    await user.type(screen.getByLabelText("Address"), "helpdesk");
    await user.type(screen.getByLabelText("IMAP host"), "imap.acme.test");
    await user.type(screen.getByLabelText("Username"), "helpdesk");
    await user.type(screen.getByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Address must be an email address such as helpdesk@acme\.test/,
    );
    expect(createMailbox).not.toHaveBeenCalled();
  });

  it("types the Address field as an email address", async () => {
    await openAddMailbox();
    expect(screen.getByLabelText("Address")).toHaveAttribute("type", "email");
  });

  it("refuses a reopen window the server would 422 over, in the user's words", async () => {
    // `ge=1` (schema.py). Zero READS as "never reopen" and is exactly what an
    // operator types to mean it -- the old dialog sent it and returned raw
    // Pydantic JSON.
    const { user, createMailbox } = await openAddMailbox();

    await user.type(screen.getByLabelText("Address"), "helpdesk@acme.test");
    await user.type(screen.getByLabelText("IMAP host"), "imap.acme.test");
    await user.type(screen.getByLabelText("Username"), "helpdesk@acme.test");
    await user.type(screen.getByLabelText("Password"), "hunter2");
    const window = screen.getByLabelText("Reopen window (days)");
    await user.clear(window);
    await user.type(window, "0");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Reopen window \(days\) must be between 1 and 3650/,
    );
    expect(createMailbox).not.toHaveBeenCalled();
  });

  it("submits on Enter, because the dialog is a real form", async () => {
    const { user, createMailbox } = await openAddMailbox();

    await user.type(screen.getByLabelText("Address"), "helpdesk@acme.test");
    await user.type(screen.getByLabelText("IMAP host"), "imap.acme.test");
    await user.type(screen.getByLabelText("Username"), "helpdesk@acme.test");
    await user.type(screen.getByLabelText("Password"), "hunter2{Enter}");

    await waitFor(() => expect(createMailbox).toHaveBeenCalled());
    expect(createMailbox).toHaveBeenCalledWith(
      expect.objectContaining({ address: "helpdesk@acme.test", port: 993 }),
    );
  });

  it("does NOT demand a password when editing, where blank means keep the stored one", async () => {
    // The rule list drops `password` entirely on edit rather than carrying an
    // exception -- otherwise saving an untouched edit form announces
    // "Password is required" to someone who set one months ago.
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const update = vi.fn().mockResolvedValue(HEALTHY);
    api.updateEmailMailbox = update;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Edit helpdesk@acme.test" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says what the SERVER refused, including a bodyless 503", async () => {
    // The old line was `{error && <p>{String(error)}</p>}`. `ApiError(503,
    // "")` stringifies to "Error: " -- a label and nothing else, which is
    // Auditor A's BLOCKER: a failed call reads as if it worked.
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.createEmailMailbox = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Add mailbox" }));
    await user.type(screen.getByLabelText("Address"), "helpdesk@acme.test");
    await user.type(screen.getByLabelText("IMAP host"), "imap.acme.test");
    await user.type(screen.getByLabelText("Username"), "helpdesk@acme.test");
    await user.type(screen.getByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /That service is unavailable right now\. Nothing was changed/,
    );
  });
});

// ---------------------------------------------------------------------
// Datetime (cycle 5) and failure text (cycle 4) on this page.
//
// EVERY TEST BELOW WAS FALSIFIED: the implementation was reverted to its
// pre-cycle shape, the test was confirmed RED, and then the implementation
// was restored and the test confirmed green. The defect each one catches is
// named in its own comment.
// ---------------------------------------------------------------------

// NOT the runner's zone (UTC) and not a fixed offset: New York is -4 on 31
// July and -5 in January, so a helper that cached one offset per zone would
// still be caught by a July fixture read against a January expectation.
const ME_ZONED: Me = { ...ME_READ_ONLY, timezone: "America/New_York" };

describe("email-intake — times", () => {
  // BREAKS AGAINST: `{m.last_polled_at ?? "Never polled"}`, the raw ISO
  // string this column printed before cycle 5.
  it("renders last-polled in the operator's profile zone, not UTC", async () => {
    const api = baseApi(ME_ZONED);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    const broken = await screen.findByRole("row", { name: /support@acme\.test/ });
    // 08:15Z is 04:15 in New York on 31 July (EDT, -4).
    expect(within(broken).getByTitle("31 Jul 2026, 04:15")).toBeInTheDocument();
    expect(within(broken).queryByText(/2026-07-31T08:15/)).toBeNull();
    // And explicitly NOT the browser zone the runner is in.
    expect(within(broken).queryByTitle("31 Jul 2026, 08:15")).toBeNull();
  });

  // BREAKS AGAINST: `new Date(null).toLocaleString()` -- literally "Invalid
  // Date" -- and equally against leaving the raw null to render nothing.
  it("renders a never-polled mailbox as NO_DATE, never Invalid Date", async () => {
    const api = baseApi(ME_ZONED);
    api.listEmailMailboxes = vi
      .fn()
      // `last_error` is deliberately NON-null: the Last error column renders
      // an em dash of its own for absence, so a row with both cells empty
      // would satisfy this assertion no matter what the Last polled cell
      // said. One em dash in the row means the assertion is about the cell
      // under test.
      .mockResolvedValue([{ ...HEALTHY, last_polled_at: null, last_error: "login failed: rejected" }]);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    const row = await screen.findByRole("row", { name: /helpdesk@acme\.test/ });
    expect(within(row).getByText(NO_DATE)).toBeInTheDocument();
    expect(within(row).queryByText("Never polled")).toBeNull();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });

  // BREAKS AGAINST: `{m.created_at}`. "Recorded" is the column an operator
  // correlates against "I emailed you at half past two", so the zone is the
  // whole value of the column.
  it("renders the message log's Recorded column in the profile zone", async () => {
    const api = baseApi(ME_ZONED);
    api.listEmailIntakeMessages = vi
      .fn()
      .mockResolvedValue(messageLog({ messages: [message()] }));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    // created_at 2026-07-31T08:00:01Z -> 04:00 in New York.
    expect(await screen.findByTitle("31 Jul 2026, 04:00")).toBeInTheDocument();
    expect(screen.queryByText("2026-07-31T08:00:01.000Z")).toBeNull();
  });
});

describe("email-intake — failures that reach a person", () => {
  // BREAKS AGAINST: `<p>{String(remove.error)}</p>`. Deleting a mailbox
  // destroys its vault secret, its Credential row and every inbound_message
  // for it, in one server-side transaction. `String(new ApiError(503, ""))`
  // is "Error: " -- a label with no sentence, beside a row that is still
  // there. That is Auditor A's BLOCKER exactly: a failed destructive call
  // that the UI presents as though nothing went wrong.
  it("announces a refused mailbox delete instead of leaving the row to speak for itself", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.deleteEmailMailbox = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "More actions for helpdesk@acme.test" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    // Deletion is confirmed now (cycle 8), so the refusal is reached THROUGH
    // the dialog rather than instead of it.
    await user.click(await screen.findByRole("button", { name: "Delete mailbox" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/The mailbox was not deleted/);
    expect(alert).toHaveTextContent(/That service is unavailable right now/);
    expect(screen.queryByText("Error:")).toBeNull();
  });

  // BREAKS AGAINST: `<p>{String(test.error)}</p>` on the connection test --
  // the button stops saying "Testing…" and "Error: " appears, which reads as
  // a test that ran and reported something.
  it("says what a failed connection test actually was", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.testEmailMailbox = vi.fn().mockRejectedValue(new ApiError(403, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Test helpdesk@acme.test" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /You do not have permission to do that/,
    );
  });

  // BREAKS AGAINST: the un-announced `<p className="text-[var(--red)]">`.
  // Red text carries no live region, and the generic sentence named no
  // reason -- here a FastAPI validation body, which the audit found rendered
  // raw as JSON in three modules.
  it("announces a message-log load failure and translates the server's reason", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listEmailIntakeMessages = vi
      .fn()
      .mockRejectedValue(
        new ApiError(
          422,
          '{"detail":[{"type":"int_parsing","loc":["body","limit"],"msg":"Input should be a valid integer","input":"x"}]}',
        ),
      );
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Messages did not load/);
    expect(alert).toHaveTextContent(/Limit: Input should be a valid integer/);
    // The raw payload is never printed, and the value the user submitted is
    // never echoed back (components/form.tsx: `input` is deliberately unread).
    expect(alert).not.toHaveTextContent(/int_parsing/);
  });
});

/**
 * UX remediation cycle 6/7/8, on this module's routes.
 *
 * The audits' own words for what these close:
 *   - "Clicking Delete removed the row instantly. No confirm dialog, no 'are
 *     you sure', no undo ... Delete sits directly beside Edit in the same row
 *     with identical size and no destructive colour."
 *   - "The Mailboxes table exposes a PASSWORD column ... it implies the value
 *     is retrievable."
 *   - "Disposition filter exposes `refused_loop` and other raw enum values."
 */
describe("email-intake — cycles 6/7/8", () => {
  // Review wave round 2 (a11y, 390x844): both tables put the column that
  // carries the panel's point outside a phone viewport, inside the table's
  // horizontal scroll -- Inbound messages showed MESSAGE and RECORDED with
  // the disposition pill off-screen entirely, and Mailboxes showed an address
  // and a credential state with Active and Last error past the fold. Nothing
  // was dropped; the load-bearing column moved to second place and the column
  // that only explains it folded into the same cell.
  it("puts what happened to the mail, and whether the mailbox works, in the second column", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();

    await screen.findByRole("row", { name: /helpdesk@acme\.test/ });
    const tables = screen.getAllByRole("table");
    const mailboxes = tables[0] as HTMLElement;
    const messages = tables[1] as HTMLElement;

    const mailboxHeaders = within(mailboxes)
      .getAllByRole("columnheader")
      .map((th) => th.textContent);
    expect(mailboxHeaders.slice(0, 2)).toEqual(["Address", "Status"]);
    // Every column that was there is still reachable.
    expect(mailboxHeaders).toContain("Credentials");
    expect(mailboxHeaders).toContain("Last polled");

    const messageHeaders = within(messages)
      .getAllByRole("columnheader")
      .map((th) => th.textContent);
    expect(messageHeaders.slice(0, 2)).toEqual(["Message", "Disposition"]);
    expect(messageHeaders).toContain("Recorded");
    expect(messageHeaders).toContain("Ticket");
    // Review wave round 3: BODY is no longer a trailing column. It was off
    // screen at 390px and still set the height of the row, so one long
    // message drew a ~200px row showing nothing but its subject. The excerpt
    // is not gone -- it folded into the Message cell, beside the subject it
    // belongs to, which is on screen at every width.
    expect(messageHeaders).not.toContain("Body");
    expect(within(messages).getAllByText("The printer on 2 is jammed.").length).toBe(3);
  });

  it("does NOT delete a mailbox on the first click", async () => {
    // FALSIFIED BY the pre-cycle code (`onClick={() => remove.mutate(m.id)}`):
    // that version deletes here, so this assertion goes red. It is the whole
    // finding in one line — the click used to BE the deletion.
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const del = vi.fn().mockResolvedValue(undefined);
    api.deleteEmailMailbox = del;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "More actions for helpdesk@acme.test" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));

    expect(del).not.toHaveBeenCalled();
    // And the row is still there to be re-read.
    expect(screen.getByRole("row", { name: /helpdesk@acme\.test/ })).toBeInTheDocument();
  });

  it("names the mailbox and what goes with it before deleting", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const del = vi.fn().mockResolvedValue(undefined);
    api.deleteEmailMailbox = del;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "More actions for support@acme.test" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));

    const dialog = await screen.findByRole("dialog", { name: "Delete mailbox" });
    // THE SUBJECT IS THE POINT. `confirm.tsx`: the subject "is what lets
    // somebody notice they have the wrong row selected". Asserting the SECOND
    // mailbox's address is what makes that observable — a dialog hardcoded to
    // the first row would pass a one-row test.
    expect(within(dialog).getByText("support@acme.test")).toBeInTheDocument();
    expect(within(dialog).getByText(/every inbound message recorded for it/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/cannot be undone/i)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Delete mailbox" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("mb2"));
  });

  it("lets the operator back out of a delete without deleting", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const del = vi.fn().mockResolvedValue(undefined);
    api.deleteEmailMailbox = del;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "More actions for helpdesk@acme.test" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(del).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "Delete mailbox" })).toBeNull();
  });

  it("keeps Delete away from the safe actions beside it, and red", async () => {
    // "no destructive colour, so a mis-click is one pixel of travel away from
    // irreversible data loss". Edit and Test sit on the row; Delete is behind
    // the row's menu, in the destructive tone.
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(baseApi(ME_ADMIN) as ApiClient);
    renderAdmin();

    const edit = await screen.findByRole("button", { name: "Edit helpdesk@acme.test" });
    expect(edit).not.toHaveClass("danger");
    expect(screen.queryByRole("button", { name: /^Delete helpdesk/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "More actions for helpdesk@acme.test" }));
    expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveClass("menu-item-destructive");
  });

  it("reads dispositions as sentences in both the filter and the rows", async () => {
    // One value, two spellings on one screen, is the finding elsewhere in the
    // product; `refused_loop` is the specific one the auditor called
    // "meaningless to a user".
    const api = baseApi(ME_READ_ONLY);
    api.listEmailIntakeMessages = vi
      .fn()
      .mockResolvedValue(messageLog({ messages: [message({ disposition: "refused_loop" })] }));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(await screen.findByText("Refused: mail loop detected")).toBeInTheDocument();
    expect(screen.queryByText("refused_loop")).toBeNull();
    // And the filter offers the same words, not the raw enum.
    expect(
      within(screen.getByLabelText("Filter by disposition")).getByRole("option", {
        name: "Refused: mail loop detected",
      }),
    ).toBeInTheDocument();
  });

  it("says what would fill an empty category picker instead of offering a UUID box", async () => {
    // Cycle 7: "Where a create form is dead because a dropdown is empty on a
    // fresh install, the fix is NOT to hide the form. It is to say what would
    // fill it." The old fallback was a free-text "Category ID" field, which is
    // the cycle-6 raw-UUID finding wearing a placeholder.
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.listTicketCategories = vi.fn().mockResolvedValue([]);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Add mailbox" }));
    await user.click(screen.getByLabelText("Default category"));

    expect(await screen.findByText(/No ticket categories exist yet/)).toBeInTheDocument();
    expect(screen.getByText(/Service desk → Admin → Categories/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Category ID")).toBeNull();
  });

  it("keeps a password manager away from the Username field beside the secret", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(baseApi(ME_ADMIN) as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Add mailbox" }));

    expect(screen.getByLabelText("Username")).toHaveAttribute("autocomplete", "off");
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "new-password");
  });
});

describe("email-intake — cycle 12", () => {
  it("writes the page description with the em dash the rest of the product uses", async () => {
    // AUDITOR A (LOW): "/email-intake and /notifications page descriptions ...
    // read '... before anything is written --' using a double hyphen, while
    // /devices, /dashboard, /health and /credentials all use a true em dash in
    // the same construction."
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();

    // The v3 port then retired the em dash from page copy altogether (the
    // design's sentences are short and plain), and the 2026-09-09 copy cut
    // took the description down to one clause each side of a semicolon. The
    // double hyphen stays banned either way, which is what this asserts.
    const description = await screen.findByText(/Mail to the support address becomes a ticket/);
    expect(description.textContent).toContain("replies thread back");
    expect(description.textContent).not.toContain("--");
    expect(description.textContent).not.toContain("—");
  });
});

/**
 * THE STAT ROW: no dead tiles.
 *
 * "All tiles and all numbers or kpis need to be clickable and direct the user
 * to the specific menus, i dont want dead tiles everywhere." `StatCard`'s
 * required `to` makes the compiler insist a destination was NAMED; only a test
 * can check the destination EXISTS. A fragment aimed at an id nobody added
 * type-checks perfectly and moves nothing.
 */
describe("email-intake stat tiles", () => {
  // Label -> destination, in DOM order. `/email-intake` is the module's only
  // route in App.tsx, so three of these are views of this page and the fourth
  // is the message log genuinely narrowed to the rows the number counts.
  const DESTINATIONS: ReadonlyArray<readonly [string, string]> = [
    ["Mailboxes configured", "/email-intake#mailboxes"],
    ["Mailboxes reporting errors", "/email-intake#mailboxes"],
    ["Messages in scope", "/email-intake#messages"],
    ["Quarantined", "/email-intake?disposition=quarantined#messages"],
  ];

  it("renders every tile as a link with a real href", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();
    await screen.findAllByText("helpdesk@acme.test");

    const cards = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(cards).toHaveLength(4);
    for (const card of cards) {
      // An <a>, not a div with onClick: keyboard reachable and middle-
      // clickable into a new tab.
      expect(card.tagName).toBe("A");
      const href = card.getAttribute("href");
      expect(href).toBeTruthy();
      expect(href).not.toBe("#");
      expect(href?.startsWith("/email-intake")).toBe(true);
    }
    expect(
      cards.map((card) => [card.querySelector(".stat-label")?.textContent, card.getAttribute("href")]),
    ).toEqual(DESTINATIONS.map(([label, to]) => [label, to]));
  });

  // Review wave round 3: every tile carried a caption and a figure and
  // nothing else, so each left an empty band under the number -- worst on the
  // two tinted tiles, where the gap falls against the wash.
  it("says what each figure is made of, on every tile", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();
    await screen.findAllByText("helpdesk@acme.test");

    await waitFor(() => expect(tile("Mailboxes configured")).toHaveTextContent("2 polling"));
    expect(tile("Mailboxes reporting errors")).toHaveTextContent("1 of 2 configured");
    // THREE ROWS ON THE PAGE, 300 IN THE SCOPE. A disposition tally over one
    // page of 50 would read "3 reached a ticket" where the answer is 300 --
    // the page-local tally this module's message log already refuses for the
    // quarantined count -- so the line says what is on screen instead.
    expect(tile("Messages in scope")).toHaveTextContent("3 of them listed below");
    expect(tile("Quarantined")).toHaveTextContent("42 of 300 messages");
    for (const card of document.querySelectorAll<HTMLElement>(".stat-card")) {
      expect(card.querySelector(".stat-foot")?.textContent ?? "").not.toBe("");
    }
  });

  it("tallies what became of the mail only when it holds all of it", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listEmailIntakeMessages = vi.fn().mockResolvedValue({
      messages: [
        message(),
        message({ id: "im2", disposition: "commented" }),
        message({ id: "im3", disposition: "quarantined", reason: "unknown sender", ticket_id: null }),
      ],
      total: 3,
      quarantined_count: 1,
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();
    await screen.findAllByText("helpdesk@acme.test");

    await waitFor(() =>
      expect(tile("Messages in scope")).toHaveTextContent("2 reached a ticket · 1 held back"),
    );
  });

  it("points every fragment at an element that is actually on the page", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();
    await screen.findAllByText("helpdesk@acme.test");

    const fragments = [...document.querySelectorAll<HTMLElement>(".stat-card")]
      .map((card) => card.getAttribute("href")?.split("#")[1])
      .filter((fragment): fragment is string => Boolean(fragment));
    expect(fragments).toHaveLength(4);
    for (const fragment of fragments) {
      expect(document.getElementById(fragment)).not.toBeNull();
    }
  });

  it("lands the Quarantined tile on the message log ALREADY narrowed to quarantined mail", async () => {
    // Spec §10 surfaces the quarantined count prominently precisely because
    // quarantined mail is invisible unless shown -- and until the tile
    // carried the filter, the only route from the number to the rows was a
    // dropdown further down the page.
    const user = userEvent.setup();
    const api = baseApi(ME_READ_ONLY);
    const listMessages = vi
      .fn()
      .mockImplementation((filters: MessageLogFilters = {}) =>
        Promise.resolve(
          filters.disposition === "quarantined"
            ? {
                messages: [
                  message({
                    id: "imq",
                    from_address: "stranger@elsewhere.test",
                    subject: "Who are you",
                    disposition: "quarantined",
                    reason: "unknown sender",
                    ticket_id: null,
                  }),
                ],
                total: 1,
                quarantined_count: 42,
              }
            : messageLog(),
        ),
      );
    api.listEmailIntakeMessages = listMessages;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await screen.findAllByText("Printer trouble");
    await user.click(tile("Quarantined"));

    // A row that could not exist in the unfiltered response is on screen, and
    // the panel's own control agrees with the URL.
    expect(await screen.findByText("stranger@elsewhere.test")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByLabelText("Filter by disposition")).toHaveValue("quarantined"),
    );
    expect(listMessages).toHaveBeenCalledWith(
      expect.objectContaining({ disposition: "quarantined" }),
    );
  });

  it("applies ?disposition= from a pasted URL on first paint", async () => {
    const api = baseApi(ME_READ_ONLY);
    const listMessages = vi.fn().mockResolvedValue(messageLog());
    api.listEmailIntakeMessages = listMessages;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin("/email-intake?disposition=quarantined#messages");

    await waitFor(() =>
      expect(screen.getByLabelText("Filter by disposition")).toHaveValue("quarantined"),
    );
    expect(listMessages).toHaveBeenCalledWith(
      expect.objectContaining({ disposition: "quarantined" }),
    );
  });

  it("ignores an unrecognised ?disposition= rather than emptying the log", async () => {
    // An empty table reads as "no mail has ever arrived", which is the
    // opposite of what a typo in a pasted URL means.
    const api = baseApi(ME_READ_ONLY);
    const listMessages = vi.fn().mockResolvedValue(messageLog());
    api.listEmailIntakeMessages = listMessages;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin("/email-intake?disposition=banana");

    await waitFor(() => expect(screen.getByLabelText("Filter by disposition")).toHaveValue(""));
    expect(listMessages).not.toHaveBeenCalledWith(
      expect.objectContaining({ disposition: "banana" }),
    );
    expect(await screen.findAllByText("Printer trouble")).not.toHaveLength(0);
  });

  it("says WHICH zero a zero is, and drops the alarm treatment for it", async () => {
    const api = baseApi(ME_READ_ONLY);
    // Every mailbox polling cleanly, nothing quarantined: the two tiles that
    // carry weight are the two that go quiet.
    api.listEmailMailboxes = vi.fn().mockResolvedValue([HEALTHY]);
    api.listEmailIntakeMessages = vi
      .fn()
      .mockResolvedValue(messageLog({ quarantined_count: 0 }));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await screen.findAllByText("helpdesk@acme.test");
    await waitFor(() =>
      expect(tile("Mailboxes reporting errors").querySelector(".stat-value")?.textContent).toBe("0"),
    );
    expect(tile("Mailboxes reporting errors")).toHaveTextContent("every mailbox polling cleanly");
    expect(tile("Mailboxes reporting errors").className).not.toContain("urgent");
    expect(tile("Quarantined")).toHaveTextContent("nothing quarantined");
    expect(tile("Quarantined").className).not.toContain("urgent");
  });

  it("carries weight only on the two counts that need a person", async () => {
    // BROKEN has a `last_error`, and the fixture log is 42 quarantined.
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();

    await waitFor(() => expect(tile("Mailboxes reporting errors").className).toContain("urgent"));
    expect(tile("Quarantined").className).toContain("urgent");
    // Colour is category, weight is urgency: the counts of what merely EXISTS
    // stay pale however large they get (300 messages in scope, here).
    expect(tile("Mailboxes configured").className).not.toContain("urgent");
    expect(tile("Messages in scope").className).not.toContain("urgent");
  });

  it("shows no number at all for a query that has not answered", async () => {
    // "0 mailboxes reporting errors" printed during the first paint says
    // "intake is healthy" before anything has been measured.
    const api = baseApi(ME_READ_ONLY);
    api.listEmailMailboxes = vi.fn().mockImplementation(() => new Promise<never>(() => {}));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await screen.findAllByText("Printer trouble");
    expect(tile("Mailboxes reporting errors").querySelector(".stat-value")?.textContent).toBe("—");
    expect(tile("Mailboxes configured").querySelector(".stat-value")?.textContent).toBe("—");
    expect(tile("Mailboxes reporting errors")).not.toHaveTextContent("every mailbox polling cleanly");
  });
});
