import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me } from "../../../lib/api";
import { NO_DATE } from "../../../lib/datetime";
import type { QueueOut } from "../../service-desk/types";
import { NotificationAdminPage } from "../NotificationAdminPage";
import { EVENT_TYPE_LABELS, RECIPIENT_KIND_LABELS } from "../labels";
import { EVENT_TYPES } from "../types";
import type {
  AllowlistEntryOut,
  ChannelOut,
  DeliveryListFilters,
  DeliveryListResponse,
  QueueMemberOut,
  RuleOut,
} from "../types";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch -- mirrors modules/service-desk/__tests__/serviceDesk.test.tsx.
const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderAdmin(initialPath = "/notifications") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <NotificationAdminPage />
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

// Spec §8: both permissions are granted ONLY to super-admin/org-admin, so a
// technician holds neither -- the two Me fixtures below are the only two
// shapes this page's gating logic ever needs to distinguish.
const ME_READ_ONLY: Me = {
  user_id: "reader-1",
  org_id: "o1",
  email: "reader@example.com",
  display_name: "Reader",
  permissions: ["notification.read"],
};

const ME_ADMIN: Me = {
  ...ME_READ_ONLY,
  user_id: "admin-1",
  display_name: "Admin",
  permissions: ["notification.read", "notification.admin"],
};

const CHANNELS: ChannelOut[] = [
  {
    id: "ch1",
    kind: "webhook",
    name: "Ops Mattermost",
    config: { url: "https://mattermost.lan/hooks/xyz" },
    active: true,
    has_credential: false,
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const RULES: RuleOut[] = [
  {
    id: "r1",
    event_type: "ticket.assigned",
    channel_id: "ch1",
    recipient_kind: "channel_default",
    queue_id: null,
    active: true,
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const ALLOWLIST: AllowlistEntryOut[] = [
  {
    id: "a1",
    host: "mattermost.lan",
    port: 8065,
    note: "Ops chat",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

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

const MEMBERS: QueueMemberOut[] = [{ user_id: "u2", queue_id: "q1" }];

function deliveriesPage(total = 0): DeliveryListResponse {
  return { items: [], total };
}

// The base fixture every test starts from: a fully-populated, all-succeed
// API. Individual tests override one method with `vi.fn()` to exercise a
// specific failure/permission path, mirroring
// service-desk's `baseApi()` precedent.
function baseApi(me: Me): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(me),
    listNotificationChannels: vi.fn().mockResolvedValue(CHANNELS),
    listNotificationRules: vi.fn().mockResolvedValue(RULES),
    listNotificationAllowlist: vi.fn().mockResolvedValue(ALLOWLIST),
    listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
    listNotificationQueueMembers: vi.fn().mockResolvedValue(MEMBERS),
    listNotificationDeliveries: vi.fn().mockResolvedValue(deliveriesPage(0)),
  };
}

describe("notification admin", () => {
  it("hides mutation controls without notification.admin", async () => {
    const api = baseApi(ME_READ_ONLY);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await screen.findAllByText("Ops Mattermost");
    await waitFor(() => expect(screen.queryByRole("button", { name: /add channel/i })).toBeNull());
    expect(screen.queryByRole("button", { name: /add rule/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /add host/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /add member/i })).toBeNull();
  });

  it("shows mutation controls with notification.admin", async () => {
    const api = baseApi(ME_ADMIN);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(await screen.findByRole("button", { name: /add channel/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add rule/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add host/i })).toBeInTheDocument();
  });

  // DESIGN REVIEW M6. With no queue chosen -- which is how this panel opens
  // every time -- "Queue members" was a heading, a subtitle, one `Select a
  // queue` dropdown and about 60px of blank card. A surface that has not been
  // used and a surface that is broken look identical when both are white.
  // Every sibling on this console answers that with `EmptyState`: /software's
  // "No software selected", /bcp/report's "No period chosen", /security's
  // "Select an event".
  it("gives Queue members a resting state rather than blank card under the picker", async () => {
    const api = baseApi(ME_ADMIN);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    const picker = await screen.findByRole("combobox", { name: "Queue" });
    expect(picker).toHaveValue("");

    const panel = picker.closest(".panel") as HTMLElement;
    const empty = panel.querySelector(".empty") as HTMLElement;
    expect(empty).not.toBeNull();
    expect(empty.querySelector(".empty-icon")).not.toBeNull();
    // `h3`, not `h2`: the panel HAS a title, so its `h2` is the rung above.
    // An untitled panel would need `headingLevel={2}` here or axe reports
    // `heading-order` -- the rule the harmony gate does not fail on.
    expect(within(empty).getByRole("heading", { level: 3 })).toHaveTextContent(
      "No queue selected",
    );
    expect(empty).toHaveTextContent(/Pick a queue above/);
  });

  it("clears the resting state once a queue is picked", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    const picker = await screen.findByRole("combobox", { name: "Queue" });
    const panel = picker.closest(".panel") as HTMLElement;
    // The queue list is a cross-module read; wait for it before choosing.
    await within(picker).findByRole("option", { name: "Helpdesk" });
    await user.selectOptions(picker, "q1");

    await waitFor(() => expect(panel.querySelector(".empty")).toBeNull());
  });

  it("surfaces the dead-letter count prominently", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationDeliveries = vi
      .fn()
      .mockImplementation((filters: DeliveryListFilters = {}) =>
        Promise.resolve(deliveriesPage(filters.status === "dead" ? 3 : 0)),
      );
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(await screen.findByText(/3 gave up/i)).toBeInTheDocument();
  });

  it("warns that an allowlist entry permits reaching an internal host", async () => {
    const api = baseApi(ME_ADMIN);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(await screen.findByText(/internal host/i)).toBeInTheDocument();
  });

  it("collects host AND port together for a new allowlist entry (migration 0026)", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const createEntry = vi.fn().mockResolvedValue({
      id: "a2",
      host: "relay.lan",
      port: 25,
      note: "",
      created_at: "2026-01-01T00:00:00.000Z",
    });
    api.createNotificationAllowlistEntry = createEntry;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: /add host/i }));
    await user.type(screen.getByPlaceholderText("mattermost.lan"), "relay.lan");
    const portInput = screen.getByLabelText("Port");
    await user.clear(portInput);
    await user.type(portInput, "25");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(createEntry).toHaveBeenCalledWith({ host: "relay.lan", port: 25, note: "" }),
    );
  });

  it("degrades quietly when the deliveries endpoint 403s", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationDeliveries = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(
      await screen.findByText("Deliveries not available (insufficient permissions)."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^error/i)).toBeNull();
    // The sibling panel is independent -- deliveries 403ing does not take
    // down the rest of the page.
    expect((await screen.findAllByText("Ops Mattermost")).length).toBeGreaterThan(0);
  });

  it("degrades quietly when a secondary cross-module call (ticket queues) 403s", async () => {
    const api = baseApi(ME_ADMIN);
    api.listTicketQueues = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await screen.findAllByText("Ops Mattermost");
    expect(screen.queryByText(/^error/i)).toBeNull();
    // Falls back to a free-text queue-id field rather than blocking the
    // panel on a permission (ticket.read) this module does not itself
    // require (mirrors service-desk's DeviceSummaryPanel 403 precedent, the
    // reverse cross-module direction).
    expect(await screen.findByPlaceholderText("Queue ID")).toBeInTheDocument();
  });

  // ------------------------------------------------------------------
  // The three states a mock never has (UI port spec §6/§8): loading, empty
  // and error must be DISTINGUISHABLE, not all render as a blank table.
  // ------------------------------------------------------------------

  it("says the channels list is loading rather than showing an empty table", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationChannels = vi.fn().mockImplementation(() => new Promise<never>(() => {}));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(/not available/i)).toBeNull();
  });

  // FALSIFIED GUARD: before this page's migration, a non-403 error on any
  // panel's query rendered NOTHING -- neither the 403 fallback text (which
  // only fires `instanceof ApiError && status === 403`) nor any row, which
  // is visually identical to "still loading" and to "list happens to be
  // empty" alike. This asserts the distinct failure text that fix added, and
  // was confirmed to fail against the pre-fix component (see the session
  // report): reverting ChannelsPanel's `failed` branch makes this test time
  // out waiting for text that is never rendered.
  it("says the channels list did NOT load on a genuine failure, distinctly from a 403 or an empty list", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationChannels = vi.fn().mockRejectedValue(new Error("boom"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("Channels not available (insufficient permissions).")).toBeNull();
    expect(screen.queryByText(/No channels yet/i)).toBeNull();
  });
});

// ---------------------------------------------------------------------
// The form kernel on /notifications → Add channel (UX remediation cycle 3).
//
// Both findings on this panel are the SAME defect seen twice: the dialog
// refused the click and named nothing.
//   HIGH   -- "Webhook channels accept any string as a URL: no validation,
//              and the field is not even type=url".
//   MEDIUM -- "Add channel is the same shared component: same four gaps as
//              /deploy (not a <form>, no required on any control)".
// Each test below was confirmed to FAIL against the pre-fix component: with
// the old `disabled={create.isPending || !name || ...}` button the Save
// control is disabled, so the click is swallowed, no `role="alert"` is ever
// rendered, and the URL input carries no `type`.
// ---------------------------------------------------------------------

describe("notification channels form", () => {
  async function openAddChannel() {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const createChannel = vi.fn().mockResolvedValue(CHANNELS[0]);
    api.createNotificationChannel = createChannel;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: /add channel/i }));
    return { user, createChannel };
  }

  it("keeps Save clickable on an empty form and names every missing field", async () => {
    const { user, createChannel } = await openAddChannel();

    // THE WHOLE POINT: the control moves. A button that will not move is the
    // silent no-op -- the user clicks, nothing happens, and the interface
    // offers no theory about which field is at fault.
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();

    await user.click(save);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Name is required");
    expect(alert).toHaveTextContent("URL is required");
    // Named locally, so nothing is sent for the server to 422 over.
    expect(createChannel).not.toHaveBeenCalled();
  });

  it("refuses a webhook URL that is not a URL, before it can silently drop deliveries", async () => {
    // Nothing downstream catches this: `_validate_config` only checks that
    // `url` is non-empty, and the scheme/host decisions happen at DELIVERY
    // time. "chat.lan" saved cleanly and then failed every notification hours
    // later in a table nobody watches.
    const { user, createChannel } = await openAddChannel();

    await user.type(screen.getByLabelText("Name"), "Ops chat");
    await user.type(screen.getByLabelText("URL"), "chat.lan/hooks/abc");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /URL must be a full address such as https:\/\/chat\.lan\/hooks\/abc/,
    );
    expect(createChannel).not.toHaveBeenCalled();
  });

  it("refuses a URL whose scheme deliveries cannot post to", async () => {
    // `new URL` parses this happily -- "is it parseable" is not the question,
    // "can delivery.py POST to it" is (`scheme_not_allowed`).
    const { user, createChannel } = await openAddChannel();

    await user.type(screen.getByLabelText("Name"), "Ops chat");
    await user.type(screen.getByLabelText("URL"), "ftp://chat.lan/hooks/abc");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/must start with https:\/\/ or http:\/\//);
    expect(createChannel).not.toHaveBeenCalled();
  });

  it("types the URL field as a URL", async () => {
    await openAddChannel();
    expect(screen.getByLabelText("URL")).toHaveAttribute("type", "url");
  });

  it("submits on Enter, because the dialog is a real form", async () => {
    const { user, createChannel } = await openAddChannel();

    await user.type(screen.getByLabelText("Name"), "Ops chat");
    await user.type(screen.getByLabelText("URL"), "https://chat.lan/hooks/abc{Enter}");

    await waitFor(() =>
      expect(createChannel).toHaveBeenCalledWith({
        kind: "webhook",
        name: "Ops chat",
        config: { url: "https://chat.lan/hooks/abc" },
        secret: undefined,
      }),
    );
  });

  it("names the malformed SMTP fields on the email kind rather than sending NaN", async () => {
    // `Number("eight")` is NaN, which `JSON.stringify` writes as `null`: the
    // old dialog let that through as a missing port and the user read a raw
    // 422. `from_address` had no shape check at all.
    const { user, createChannel } = await openAddChannel();

    await user.selectOptions(screen.getByLabelText("Kind"), "email");
    await user.type(screen.getByLabelText("Name"), "Relay");
    await user.type(screen.getByLabelText("SMTP host"), "smtp.acme.test");
    const port = screen.getByLabelText("SMTP port");
    await user.clear(port);
    await user.type(port, "70000");
    await user.type(screen.getByLabelText("From address"), "alerts");
    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/SMTP port must be between 1 and 65535/);
    expect(alert).toHaveTextContent(/From address must be an email address/);
    expect(createChannel).not.toHaveBeenCalled();
  });

  it("says what the SERVER refused, including a bodyless 503", async () => {
    // The old line was `{create.isError && <p>{String(create.error)}</p>}`.
    // `ApiError(503, "")` stringifies to "Error: " -- a label and nothing
    // else, which is Auditor A's BLOCKER: a failed call reads as success.
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.createNotificationChannel = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: /add channel/i }));
    await user.type(screen.getByLabelText("Name"), "Ops chat");
    await user.type(screen.getByLabelText("URL"), "https://chat.lan/hooks/abc");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /That service is unavailable right now\. Nothing was changed/,
    );
  });
});

// ---------------------------------------------------------------------
// Datetime (UX remediation cycle 5) and failure text (cycle 4) on this page.
//
// EVERY TEST BELOW WAS FALSIFIED: the implementation was broken back to the
// shape it had before this cycle, the test was confirmed RED, and then
// restored and confirmed green. What each one breaks against is named in its
// own comment, because a test that passes against the defect it claims to
// cover is worse than no test at all -- this project has been bitten by
// exactly that.
// ---------------------------------------------------------------------

// A zone that is NOT the test runner's (UTC) and NOT a fixed offset: New York
// is -4 on 20 August, so a wrong-zone render is off by four hours here and by
// five in January. A fixture pinned to a fixed-offset zone would pass against
// a helper that quietly cached one offset for the whole year.
const ME_ZONED: Me = { ...ME_READ_ONLY, timezone: "America/New_York" };

const DELIVERY = {
  id: "d1",
  event_id: "e1",
  channel_id: "ch1",
  recipient: "ops@acme.test",
  status: "failed" as const,
  attempts: 2,
  last_error: "connect timeout",
  last_status_code: null,
  next_attempt_at: "2026-08-20T19:00:00.000Z",
  sent_at: null,
  created_at: "2026-08-20T18:00:00.000Z",
};

// The dead-letter badge and the list share one mocked method, so the fixture
// answers on `status` exactly as the endpoint does.
function deliveriesApi(me: Me, delivery: Record<string, unknown> = DELIVERY): Partial<ApiClient> {
  const api = baseApi(me);
  api.listNotificationDeliveries = vi
    .fn()
    .mockImplementation((filters: DeliveryListFilters = {}) =>
      Promise.resolve(
        filters.status === "dead"
          ? { items: [], total: 0 }
          : { items: [delivery], total: 1 },
      ),
    );
  return api;
}

describe("notification deliveries — times", () => {
  // BREAKS AGAINST: `{d.next_attempt_at ?? "—"}`, the raw ISO string this
  // column rendered before cycle 5 -- "2026-08-20T19:00:00.000Z" is present
  // in the DOM and "20 Aug 2026, 15:00" is not.
  it("renders the next retry in the operator's profile zone, not UTC and not the browser's", async () => {
    mockUseApi.mockReturnValue(deliveriesApi(ME_ZONED) as ApiClient);
    renderAdmin();

    const row = await screen.findByRole("row", { name: /ops@acme\.test/ });
    // 19:00Z is 15:00 in New York on 20 August (EDT, -4).
    expect(within(row).getByTitle("20 Aug 2026, 15:00")).toBeInTheDocument();
    expect(within(row).queryByText(/2026-08-20T19:00/)).toBeNull();
  });

  // BREAKS AGAINST: passing no zone at all (the "fall back to the browser"
  // shortcut). The runner's zone is UTC, so the same row would read 19:00.
  it("does not fall back to the browser zone when the profile carries one", async () => {
    mockUseApi.mockReturnValue(deliveriesApi(ME_ZONED) as ApiClient);
    renderAdmin();

    const row = await screen.findByRole("row", { name: /ops@acme\.test/ });
    expect(within(row).queryByTitle("20 Aug 2026, 19:00")).toBeNull();
  });

  // BREAKS AGAINST: `new Date(null).toLocaleString()`, which is the literal
  // string "Invalid Date" -- the rendering this cycle exists to abolish.
  it("renders an absent next-attempt as NO_DATE, never Invalid Date", async () => {
    mockUseApi.mockReturnValue(
      deliveriesApi(ME_ZONED, { ...DELIVERY, next_attempt_at: null }) as ApiClient,
    );
    renderAdmin();

    const row = await screen.findByRole("row", { name: /ops@acme\.test/ });
    expect(within(row).getByText(NO_DATE)).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });
});

// "Ops Mattermost" appears in the channels table AND in the rules table (as
// the rule's target channel), so a bare row query is ambiguous. The channel's
// own row is the one carrying a "Test" button.
function channelsPanel(rows: HTMLElement[]): HTMLElement {
  const row = rows.find((candidate) => within(candidate).queryByRole("button", { name: "Test" }));
  if (!row) throw new Error("no channels row with a Test button");
  return row;
}

describe("notification — failures that reach a person", () => {
  // BREAKS AGAINST: the pre-cycle `toggleActive`, whose mutation had no
  // `onError` and whose error was rendered NOWHERE. Disabling a noisy or
  // compromised webhook that silently did not disable is the swallowed
  // destructive call, one panel over from where the audit first found it.
  it("announces a refused Enable/Disable instead of leaving the old badge standing", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.updateNotificationChannel = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    // Scoped to the CHANNELS panel: RulesPanel carries its own switch, and
    // renders the channel's name in its own row too.
    //
    // A SWITCH, NOT A BUTTON. v3 draws a channel's on/off state as a toggle in
    // an "On" column, so Enable/Disable is no longer a text link in the action
    // cluster; the control it replaced fired the same mutation, and this test
    // still measures what happens when that mutation is refused.
    const panel = channelsPanel(await screen.findAllByRole("row", { name: /Ops Mattermost/ }));
    await user.click(within(panel).getByRole("switch"));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/The channel was not changed/);
    expect(alert).toHaveTextContent(/That service is unavailable right now/);
  });

  // BREAKS AGAINST: `<p>{String(test.error)}</p>`. `String(new ApiError(503,
  // ""))` is "Error: " -- a label with nothing after it, rendered where a
  // result should be, while the button has just stopped saying "Testing…".
  it("says what a failed test-send actually was, not \"Error: \"", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.testNotificationChannel = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    const panel = channelsPanel(await screen.findAllByRole("row", { name: /Ops Mattermost/ }));
    await user.click(within(panel).getByRole("button", { name: "Test" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /That service is unavailable right now\. Nothing was changed/,
    );
    expect(screen.queryByText("Error:")).toBeNull();
  });

  // BREAKS AGAINST: `<p>{String(del.error)}</p>` on the allowlist panel. The
  // row stays on screen either way, so without the sentence the operator has
  // no way to tell "not removed yet" from "still allowlisted".
  it("says an allowlist entry is STILL allowlisted when its removal is refused", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.deleteNotificationAllowlistEntry = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(
      await screen.findByRole("button", { name: "More actions for mattermost.lan:8065" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Remove" }));
    // Removal is confirmed now (cycle 8), so the refusal is reached THROUGH
    // the dialog rather than instead of it.
    await user.click(await screen.findByRole("button", { name: "Remove entry" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/still allowlisted/);
    expect(alert).toHaveTextContent(/That service is unavailable right now/);
  });

  // BREAKS AGAINST: the un-announced `<p className="text-[var(--red)]">…`.
  // Red text with no live region is invisible to a screen reader, and the
  // generic sentence alone never named the reason.
  it("announces a deliveries load failure and names the reason", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationDeliveries = vi
      .fn()
      .mockRejectedValue(new ApiError(500, '{"detail":"queue offline"}'));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Deliveries did not load/);
    expect(alert).toHaveTextContent(/queue offline/);
  });
});

/**
 * UX remediation cycles 6/7/8 on /notifications.
 *
 * The three findings these close, in the auditor's words:
 *   - HIGH: "Notification channels cannot be deleted — only disabled ... A
 *     channel created by mistake is permanent." The endpoint existed the whole
 *     time; the control did not.
 *   - HIGH: "Delivery failure is reported as a raw enum code ... The specific
 *     message is also unhelpful: the correct fix ('URLs must start with
 *     https://') is knowable and unstated."
 *   - The cross-cutting one: destructive actions undifferentiated and
 *     unconfirmed.
 */
describe("notification — cycles 6/7/8", () => {
  it("offers Delete on a channel at all", async () => {
    // The finding as measured: "Row action probe returned exactly
    // ['Disable','Test']."
    mockUseApi.mockReturnValue(baseApi(ME_ADMIN) as ApiClient);
    renderAdmin();

    await userEvent.click(await screen.findByRole("button", { name: "More actions for Ops Mattermost" }));
    expect(screen.getByRole("menuitem", { name: "Delete" })).toBeInTheDocument();
  });

  it("does NOT delete a channel on the first click, and names it before doing so", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const del = vi.fn().mockResolvedValue(undefined);
    api.deleteNotificationChannel = del;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "More actions for Ops Mattermost" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    expect(del).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog", { name: "Delete channel" });
    expect(within(dialog).getByText("Ops Mattermost")).toBeInTheDocument();
    // The audit's own extra ask: "a warning if rules reference the channel".
    // RULES routes through ch1, so this counts one, from the data.
    expect(within(dialog).getByText(/1 rule currently routes through it/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Delete channel" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("ch1"));
  });

  it("explains a failed test send instead of printing the backend's enum", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.testNotificationChannel = vi
      .fn()
      .mockResolvedValue({ delivered: false, status_code: null, error: "scheme_not_allowed" });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Test" }));

    // The exact string the auditor read on screen must be gone...
    await waitFor(() => expect(screen.queryByText(/scheme_not_allowed/)).toBeNull());
    // ...replaced by the remedy that was "knowable and unstated".
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/Only https:\/\/ is permitted/);
    expect(status).toHaveTextContent(/edit the channel's URL/i);
  });

  it("translates a delivery's stored last_error too, not only the test button", async () => {
    // `delivery.py` writes "egress blocked: <reason>" into `last_error`, so
    // the same machine code reaches the operator through a second render site.
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationDeliveries = vi.fn().mockResolvedValue({
      items: [
        {
          id: "d1",
          event_id: "e1",
          channel_id: "ch1",
          recipient: "mattermost.lan",
          status: "dead" as const,
          attempts: 6,
          last_error: "egress blocked: blocked_private",
          last_status_code: null,
          next_attempt_at: null,
          sent_at: null,
          created_at: "2026-01-01T00:00:00.000Z",
        },
      ],
      total: 1,
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(await screen.findByText(/private or loopback address space/)).toBeInTheDocument();
    expect(screen.queryByText("egress blocked: blocked_private")).toBeNull();
    // And the status badge reads as words rather than as a column value.
    expect(screen.getAllByText("Gave up").length).toBeGreaterThan(0);
  });

  it("reads a rule's event type and recipient as sentences", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();

    expect(await screen.findByText("Ticket assigned")).toBeInTheDocument();
    // docs/design/settings-language.md, "Notifications": `channel_default` ->
    // "The channel's own default" -- the direction doc's exact string, not
    // notificationState.ts's now-deleted "...default address" wording.
    expect(screen.getByText("The channel's own default")).toBeInTheDocument();
    expect(screen.queryByText("channel_default")).toBeNull();
  });

  // Defect 1 (lang-notifications-reconcile report): RulesPanel used to read
  // its labels from notificationState.ts's OWN event-type/recipient-kind
  // maps, worded differently from notification/labels.ts -- the map
  // docs/design/settings-language.md, "Notifications" actually specifies (and
  // that Settings -> Notifications already renders through, per
  // NotificationsTab.tsx). labels.ts is now the sole source; this pins every
  // `EVENT_TYPES` entry and every recipient kind rendering through it here
  // too, keyed off the wire value each cell's own `title` attribute carries
  // (RulesPanel.tsx), not by searching visible text a stale second map could
  // coincidentally still match.
  it("renders every event type and recipient kind through labels.ts, with no second map to disagree with", async () => {
    const RECIPIENT_KINDS_FOR_TEST = [
      "queue_members",
      "assignee",
      "requester",
      "channel_default",
    ] as const;
    const allRules: RuleOut[] = EVENT_TYPES.map((eventType, i) => ({
      id: `all-${i}`,
      event_type: eventType,
      channel_id: "ch1",
      recipient_kind: RECIPIENT_KINDS_FOR_TEST[i % RECIPIENT_KINDS_FOR_TEST.length] ?? "queue_members",
      queue_id: null,
      active: true,
      created_at: "2026-01-01T00:00:00.000Z",
    }));
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationRules = vi.fn().mockResolvedValue(allRules);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await screen.findByText(EVENT_TYPE_LABELS["ticket.assigned"]);

    for (const rule of allRules) {
      const eventSpan = document.querySelector(`span[title="${rule.event_type}"]`);
      expect(eventSpan, `no rendered span for event type ${rule.event_type}`).not.toBeNull();
      expect(eventSpan?.textContent).toBe(
        EVENT_TYPE_LABELS[rule.event_type as keyof typeof EVENT_TYPE_LABELS],
      );

      const recipientSpan = document.querySelector(`span[title="${rule.recipient_kind}"]`);
      expect(recipientSpan, `no rendered span for recipient kind ${rule.recipient_kind}`).not.toBeNull();
      expect(recipientSpan?.textContent).toBe(RECIPIENT_KIND_LABELS[rule.recipient_kind]);
    }
  });

  // The distinction the direction doc's own author had in view is preserved
  // even though both labels now come from one shared map: a rule pairing
  // "ticket.internal_note_added" with recipient_kind="requester" is rejected
  // server-side (spec §6.4/§2.1) precisely because the two comment events are
  // separate types, so the two labels must stay obviously distinguishable
  // rather than reading as one event with a visibility flag.
  it("keeps the public-reply and internal-note event types worded distinctly", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationRules = vi.fn().mockResolvedValue([
      {
        id: "r-public",
        event_type: "ticket.comment_added",
        channel_id: "ch1",
        recipient_kind: "requester",
        queue_id: null,
        active: true,
        created_at: "2026-01-01T00:00:00.000Z",
      },
      {
        id: "r-internal",
        event_type: "ticket.internal_note_added",
        channel_id: "ch1",
        recipient_kind: "channel_default",
        queue_id: null,
        active: true,
        created_at: "2026-01-01T00:00:00.000Z",
      },
    ]);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    expect(await screen.findByText("Public reply added")).toBeInTheDocument();
    expect(screen.getByText("Internal note added")).toBeInTheDocument();
  });

  // Guards the fix itself, not only its symptom: notificationState.ts must
  // never again carry its own copy of these maps. Two maps that agree today
  // disagree later -- the direction doc's own reasoning for deleting this
  // one rather than hand-syncing it.
  it("no longer exports an event-type or recipient-kind label map from notificationState.ts -- labels.ts is the only source", async () => {
    const notificationState: Record<string, unknown> = await import("../notificationState");
    expect(notificationState.eventTypeLabel).toBeUndefined();
    expect(notificationState.recipientKindLabel).toBeUndefined();
    expect(notificationState.EVENT_TYPE_LABELS).toBeUndefined();
    expect(notificationState.RECIPIENT_KIND_LABELS).toBeUndefined();
  });

  it("does NOT delete a rule on the first click", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const del = vi.fn().mockResolvedValue(undefined);
    api.deleteNotificationRule = del;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: /More actions for rule Ticket assigned/ }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    expect(del).not.toHaveBeenCalled();

    await user.click(await screen.findByRole("button", { name: "Delete rule" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("r1"));
  });

  it("says what would fill an empty channel picker rather than presenting a dead form", async () => {
    // Cycle 7. On a fresh install the Channel control was a select holding one
    // placeholder, with Save disabled and no statement of why.
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.listNotificationChannels = vi.fn().mockResolvedValue([]);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: /add rule/i }));
    await user.click(screen.getByLabelText("Channel"));

    expect(await screen.findByText(/Add one under Channels on this page/)).toBeInTheDocument();
  });

  it("distinguishes channels in the picker by kind AND whether they deliver", async () => {
    // The sublabel is the safety: routing a rule through a disabled channel is
    // a rule that silently delivers nothing.
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.listNotificationChannels = vi
      .fn()
      .mockResolvedValue([
        {
          id: "ch9",
          kind: "webhook",
          name: "Old hook",
          config: { url: "https://old.lan/hooks/x" },
          active: false,
          has_credential: false,
          created_at: "2026-01-01T00:00:00.000Z",
        } satisfies ChannelOut,
      ]);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: /add rule/i }));
    await user.click(screen.getByLabelText("Channel"));

    expect(
      await screen.findByText("Webhook · disabled, delivers nothing"),
    ).toBeInTheDocument();
  });

  it("shows a queue member as a person, not as a UUID", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.listUsers = vi.fn().mockResolvedValue({
      users: [
        { id: "u2", email: "sam@acme.test", display_name: "Sam Okafor", status: "active", roles: [] },
      ],
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    // The queue list is a cross-module fetch; wait for it before choosing.
    await screen.findByRole("option", { name: "Helpdesk" });
    await user.selectOptions(screen.getByLabelText("Queue"), "q1");

    expect(await screen.findByText("Sam Okafor")).toBeInTheDocument();
    expect(screen.queryByText("u2")).toBeNull();
  });

  it("does NOT remove an allowlist entry on the first click", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const del = vi.fn().mockResolvedValue(undefined);
    api.deleteNotificationAllowlistEntry = del;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "More actions for mattermost.lan:8065" }));
    await user.click(screen.getByRole("menuitem", { name: "Remove" }));
    expect(del).not.toHaveBeenCalled();

    // Host AND port together are the identity here (migration 0026), so both
    // are what the dialog has to name.
    const dialog = await screen.findByRole("dialog", { name: "Remove allowlist entry" });
    expect(within(dialog).getByText("mattermost.lan:8065")).toBeInTheDocument();
  });

  it("keeps a password manager away from the bearer token field", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(baseApi(ME_ADMIN) as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: /add channel/i }));

    expect(screen.getByLabelText("Bearer token (optional)")).toHaveAttribute(
      "autocomplete",
      "new-password",
    );
  });

  it("lets a mistyped webhook URL be corrected instead of abandoned", async () => {
    // The other half of the HIGH finding: "Because the URL cannot be edited
    // either, an operator who mistypes a webhook URL must abandon a
    // permanently-listed dead channel and create a second one."
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const update = vi.fn().mockResolvedValue(CHANNELS[0]);
    api.updateNotificationChannel = update;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Edit Ops Mattermost" }));

    // The dialog opens SEEDED FROM THE ROW, which is what makes it an edit
    // rather than a create with a different title.
    const url = screen.getByLabelText("URL");
    expect(url).toHaveValue("https://mattermost.lan/hooks/xyz");
    // Kind is immutable server-side (`ChannelUpdate` has no such field), so
    // the control is disabled and the reason is stated rather than the change
    // being accepted and dropped.
    expect(screen.getByLabelText("Kind")).toBeDisabled();
    expect(screen.getByText(/kind cannot be changed after it is created/i)).toBeInTheDocument();

    await user.clear(url);
    await user.type(url, "https://mattermost.lan/hooks/correct");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      // NO `secret` KEY in the body. Blank means "keep the stored one";
      // sending "" would be a rotation to an empty secret.
      expect(update).toHaveBeenCalledWith("ch1", {
        name: "Ops Mattermost",
        config: { url: "https://mattermost.lan/hooks/correct" },
      }),
    );
  });

  // Glossary, `docs/design/settings-language.md`: "relay" -> "mail server",
  // everywhere. Settings → Notifications made the same move on its own
  // subtitle; this pins the word on this page's Channels panel.
  it("calls the SMTP destination a mail server, not a relay", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_ADMIN) as ApiClient);
    renderAdmin();

    expect(await screen.findByText(/a webhook, or a mail server/i)).toBeInTheDocument();
    expect(screen.queryByText(/mail relay/i)).not.toBeInTheDocument();
  });

  // Design review: "Which event reaches which channel" here and "Which event
  // goes to which channel" on Settings → Notifications was one job in two
  // verbs. Picked "goes" (`NotificationsTab.tsx`'s own comment says why); this
  // pins the matching sentence on this page's Rules panel.
  it("describes what a rule does with the same verb Settings → Notifications uses", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_ADMIN) as ApiClient);
    renderAdmin();

    expect(
      await screen.findByText("Which event goes to which channel, and who receives it"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/reaches which channel/i)).not.toBeInTheDocument();
  });

  it("spells a channel kind the same way in the form as in the table", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(baseApi(ME_ADMIN) as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: /add channel/i }));

    const kind = screen.getByLabelText("Kind");
    expect(within(kind).getByRole("option", { name: "Webhook" })).toBeInTheDocument();
    expect(within(kind).queryByRole("option", { name: "webhook" })).toBeNull();
  });
});

/**
 * Cycle 12 — the two findings on this module that were answered with words
 * rather than with behaviour, and so had nothing pinning them.
 */
describe("notification — cycle 12", () => {
  it("says a test send is not a delivery, beside the log that does not list it", async () => {
    // AUDITOR A (MEDIUM): "Immediately after a Test that visibly failed with
    // `scheme_not_allowed`, the Deliveries panel still reads '0 dead' and its
    // table shows 'No deliveries yet.' ... A user who sees a failure in one
    // panel and 'No deliveries yet' in the panel directly below cannot tell
    // whether failures are being recorded at all."
    //
    // `send_test` writes no delivery row and no outbox event, so the panel is
    // telling the truth; what it was not doing was SAYING so, which is the
    // audit's own second remedy ("add an explicit Test results area distinct
    // from the delivery log and explain the difference"). This asserts the
    // sentence and that it names where the result does appear — a note that
    // said only "tests are not listed" would leave the reader where they
    // started.
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.testNotificationChannel = vi
      .fn()
      .mockResolvedValue({ delivered: false, status_code: null, error: "scheme_not_allowed" });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Test" }));
    // The failure is on screen...
    expect(await screen.findByRole("status")).toHaveTextContent(/Only https:\/\/ is permitted/);
    // ...and so is the reason the panel below still says nothing happened.
    expect(screen.getByText(/Test sends are not listed here/)).toBeInTheDocument();
    expect(screen.getByText(/is not recorded as a delivery/)).toBeInTheDocument();
    expect(screen.getByText(/beside the Test button\s+in Channels/)).toBeInTheDocument();
    expect(screen.getByText("No deliveries yet.")).toBeInTheDocument();
  });

  it("writes the page description with the em dash the rest of the product uses", async () => {
    // AUDITOR A (LOW): "/email-intake and /notifications page descriptions ...
    // use a double hyphen, while /devices, /dashboard, /health and
    // /credentials all use a true em dash in the same construction."
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();

    // The v3 port then retired the em dash from page copy altogether (the
    // design's sentences are short and plain), so the construction the audit
    // filed is now a comma; the double hyphen stays banned either way.
    const description = await screen.findByText(/Who is told when something happens/);
    expect(description.textContent).not.toContain("--");
    expect(description.textContent).not.toContain("—");
  });
});

/**
 * THE STAT ROW: no dead tiles.
 *
 * The operator's instruction was "all tiles and all numbers or kpis need to be
 * clickable and direct the user to the specific menus, i dont want dead tiles
 * everywhere". `StatCard`'s required `to` makes the compiler enforce that a
 * destination was NAMED; nothing makes the compiler check that the
 * destination EXISTS, and a fragment pointing at an id nobody added is exactly
 * as dead as no link at all while type-checking perfectly. That is what the
 * tests below are for, and why one of them resolves every fragment against
 * the rendered document rather than against a list of strings.
 */
describe("notification stat tiles", () => {
  // The label -> destination contract, in DOM order. `/notifications` is this
  // module's only route in App.tsx, so every destination is a view OF this
  // page: three fragments, and one genuinely different view of the deliveries
  // log.
  const DESTINATIONS: ReadonlyArray<readonly [string, string]> = [
    ["Channels", "/notifications#channels"],
    ["Rules", "/notifications#rules"],
    ["Allowlist entries", "/notifications#allowlist"],
    ["Deliveries that gave up", "/notifications?status=dead#deliveries"],
  ];

  it("renders every tile as a link with a real href", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();
    await screen.findAllByText("Ops Mattermost");

    const cards = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(cards).toHaveLength(4);
    for (const card of cards) {
      // An <a>, not a div with onClick: keyboard reachable, middle-clickable
      // into a new tab, and pasteable into an incident channel.
      expect(card.tagName).toBe("A");
      const href = card.getAttribute("href");
      expect(href).toBeTruthy();
      expect(href).not.toBe("#");
      expect(href?.startsWith("/notifications")).toBe(true);
    }
    expect(
      cards.map((card) => [card.querySelector(".stat-label")?.textContent, card.getAttribute("href")]),
    ).toEqual(DESTINATIONS.map(([label, to]) => [label, to]));
  });

  it("points every fragment at an element that is actually on the page", async () => {
    // The half a type cannot check. `#allowlist` is a valid string whether or
    // not AllowlistPanel carries that id; a tile aimed at an id nobody added
    // navigates and moves nothing, which is the dead tile in a new costume.
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();
    await screen.findAllByText("Ops Mattermost");

    const fragments = [...document.querySelectorAll<HTMLElement>(".stat-card")]
      .map((card) => card.getAttribute("href")?.split("#")[1])
      .filter((fragment): fragment is string => Boolean(fragment));
    expect(fragments).toHaveLength(4);
    for (const fragment of fragments) {
      expect(document.getElementById(fragment)).not.toBeNull();
    }
  });

  it("lands the dead-letter tile on the deliveries log ALREADY narrowed to dead letters", async () => {
    // The one destination that is a different view rather than a scroll, so
    // it is the one where "the link works" means something a reader can see.
    const user = userEvent.setup();
    const api = baseApi(ME_READ_ONLY);
    const listDeliveries = vi
      .fn()
      .mockImplementation((filters: DeliveryListFilters = {}) =>
        Promise.resolve(
          filters.status === "dead"
            ? { items: [], total: 3 }
            : { items: [], total: 0 },
        ),
      );
    api.listNotificationDeliveries = listDeliveries;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await waitFor(() => expect(tile("Deliveries that gave up")).toHaveTextContent("3"));
    await user.click(tile("Deliveries that gave up"));

    // The panel's own control agrees with the URL -- the filter is applied,
    // not merely requested once.
    await waitFor(() =>
      expect(screen.getByLabelText("Filter by status")).toHaveValue("dead"),
    );
    expect(listDeliveries).toHaveBeenCalledWith(
      expect.objectContaining({ status: "dead", limit: 50 }),
    );
  });

  it("applies ?status= from a pasted URL on first paint", async () => {
    // The other half of putting the filter in the query string: a link an
    // operator pastes into an incident channel has to open the same view for
    // the person who clicks it.
    const api = baseApi(ME_READ_ONLY);
    const listDeliveries = vi.fn().mockResolvedValue({ items: [], total: 0 });
    api.listNotificationDeliveries = listDeliveries;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin("/notifications?status=dead#deliveries");

    await waitFor(() =>
      expect(screen.getByLabelText("Filter by status")).toHaveValue("dead"),
    );
    expect(listDeliveries).toHaveBeenCalledWith(
      expect.objectContaining({ status: "dead", limit: 50 }),
    );
  });

  it("ignores an unrecognised ?status= rather than showing an empty log", async () => {
    const api = baseApi(ME_READ_ONLY);
    const listDeliveries = vi.fn().mockResolvedValue({ items: [], total: 0 });
    api.listNotificationDeliveries = listDeliveries;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin("/notifications?status=banana");

    await waitFor(() => expect(screen.getByLabelText("Filter by status")).toHaveValue(""));
    expect(listDeliveries).not.toHaveBeenCalledWith(
      expect.objectContaining({ status: "banana" }),
    );
  });

  it("says WHICH zero a zero is, and drops the alarm treatment for it", async () => {
    // "0 dead-lettered" wearing a saturated red fill reads as a perfect score
    // one moment and as a catastrophe the next. At zero the tile goes quiet
    // and says what the zero means.
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderAdmin();

    const dead = await waitFor(() => {
      const card = tile("Deliveries that gave up");
      expect(card.querySelector(".stat-value")?.textContent).toBe("0");
      return card;
    });
    expect(dead).toHaveTextContent("every delivery arrived or is still retrying");
    expect(dead.className).not.toContain("urgent");
  });

  it("carries weight only when a person has to act", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationDeliveries = vi.fn().mockResolvedValue({ items: [], total: 7 });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await waitFor(() => expect(tile("Deliveries that gave up").className).toContain("urgent"));
    // Colour is category, weight is urgency: the configuration counts stay
    // pale however many of them there are.
    expect(tile("Channels").className).not.toContain("urgent");
    expect(tile("Rules").className).not.toContain("urgent");
    expect(tile("Allowlist entries").className).not.toContain("urgent");
  });

  // Review wave round 2: v3's tile for this route draws the figure and then
  // what it is made of ("3 · Email · Teams · webhook"), and the console drew a
  // bare figure over a sub-line reserved for a zero-state label that, by
  // definition, never shows at a non-zero count.
  it("says what the channel and rule counts are made of", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationChannels = vi.fn().mockResolvedValue([
      ...CHANNELS,
      { ...CHANNELS[0], id: "ch2", kind: "email", name: "Helpdesk mail" },
      { ...CHANNELS[0], id: "ch3", kind: "email", name: "Escalation mail" },
    ]);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    const channels = await waitFor(() => {
      const card = tile("Channels");
      expect(card.querySelector(".stat-value")?.textContent).toBe("3");
      return card;
    });
    // Read off the rows the panel below already renders -- no extra request,
    // and the kinds are the wire's own `ChannelKind` values humanised once.
    expect(channels).toHaveTextContent("2 email · 1 webhook");
    // And the breakdown never competes with the zero-state label: at zero
    // there is nothing to break down and the label keeps the line.
    expect(tile("Rules")).toHaveTextContent("all routing");
  });

  // Review wave round 3: two of the four tiles carried a breakdown and two
  // carried none, so half the row sat with an empty band under the figure --
  // most visible on the crit-tinted dead-letter tile, where the gap falls
  // against the wash. Neither new line invents a count.
  it("gives every tile in the row a second line, not just two of them", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationAllowlist = vi.fn().mockResolvedValue([
      ...ALLOWLIST,
      { ...ALLOWLIST[0], id: "a2", host: "mattermost.lan", port: 443, note: "TLS" },
      { ...ALLOWLIST[0], id: "a3", host: "chat.lan", port: 443, note: "Chat" },
    ]);
    api.listNotificationDeliveries = vi.fn().mockResolvedValue(deliveriesPage(3));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await waitFor(() =>
      expect(tile("Allowlist entries").querySelector(".stat-value")?.textContent).toBe("3"),
    );
    // An entry is a host AND a port, so three entries over two hosts is a
    // different configuration from three over three -- read off the same rows
    // the panel below renders.
    expect(tile("Allowlist entries")).toHaveTextContent("across 2 hosts");
    expect(tile("Deliveries that gave up")).toHaveTextContent("retries exhausted");
    for (const label of ["Channels", "Rules", "Allowlist entries", "Deliveries that gave up"]) {
      expect(tile(label).querySelector(".stat-foot")?.textContent ?? "").not.toBe("");
    }
  });

  it("shows no number at all for a query that has not answered", async () => {
    // A pending query rendered "0" for the whole of the first paint, so
    // "nothing is allowlisted" and "we have not been told" were the same
    // pixels -- the defect zeroLabel exists to end, one level up.
    const api = baseApi(ME_READ_ONLY);
    api.listNotificationAllowlist = vi.fn().mockImplementation(() => new Promise<never>(() => {}));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await screen.findAllByText("Ops Mattermost");
    const allowlist = tile("Allowlist entries");
    expect(allowlist.querySelector(".stat-value")?.textContent).toBe("—");
    expect(allowlist).not.toHaveTextContent("nothing allowlisted");
  });
});


// ---------------------------------------------------------------------
// Email sign-in: a password, or Microsoft 365 (client-credentials XOAUTH2).
// The sign-in values are write-only request fields the server vaults; the
// Microsoft server and port are fixed, not typed.
// ---------------------------------------------------------------------

describe("notification email sign-in", () => {
  async function openEmailChannel() {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    const createChannel = vi.fn().mockResolvedValue(CHANNELS[0]);
    api.createNotificationChannel = createChannel;
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();
    await user.click(await screen.findByRole("button", { name: /add channel/i }));
    await user.selectOptions(screen.getByLabelText("Kind"), "email");
    return { user, createChannel };
  }

  async function chooseMicrosoft(user: ReturnType<typeof userEvent.setup>) {
    const group = screen.getByRole("group", { name: "Sign-in" });
    await user.click(within(group).getByRole("button", { name: "Microsoft 365" }));
  }

  it("sends a Microsoft 365 channel through Microsoft's server with the three sign-in values", async () => {
    const { user, createChannel } = await openEmailChannel();
    await chooseMicrosoft(user);

    // The server and port are fixed for Microsoft 365, so they are not asked for.
    expect(screen.queryByLabelText("SMTP host")).toBeNull();
    expect(screen.getByText(/smtp\.office365\.com/)).toBeInTheDocument();

    await user.type(screen.getByLabelText("Name"), "Alerts");
    await user.type(screen.getByLabelText("From address"), "alerts@toeshee.com");
    await user.type(screen.getByLabelText("Directory (tenant) ID"), "contoso.onmicrosoft.com");
    await user.type(screen.getByLabelText("Application (client) ID"), "app-id");
    await user.type(screen.getByLabelText("Client secret"), "s3cret");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(createChannel).toHaveBeenCalledWith({
        kind: "email",
        name: "Alerts",
        config: {
          auth: "microsoft",
          host: "smtp.office365.com",
          port: 587,
          from_address: "alerts@toeshee.com",
          use_tls: true,
        },
        microsoft_tenant_id: "contoso.onmicrosoft.com",
        microsoft_client_id: "app-id",
        microsoft_client_secret: "s3cret",
      }),
    );
  });

  it("asks for all three Microsoft 365 values before saving", async () => {
    const { user, createChannel } = await openEmailChannel();
    await chooseMicrosoft(user);

    await user.type(screen.getByLabelText("Name"), "Alerts");
    await user.type(screen.getByLabelText("From address"), "alerts@toeshee.com");
    await user.type(screen.getByLabelText("Directory (tenant) ID"), "contoso.onmicrosoft.com");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /directory \(tenant\) ID, the application \(client\) ID and the client secret/,
    );
    expect(createChannel).not.toHaveBeenCalled();
  });

  it("sends a password sign-in as a username and a password, never as a bare secret", async () => {
    const { user, createChannel } = await openEmailChannel();

    await user.type(screen.getByLabelText("Name"), "Relay");
    await user.type(screen.getByLabelText("SMTP host"), "relay.lan");
    await user.type(screen.getByLabelText("From address"), "alerts@toeshee.com");
    await user.type(screen.getByLabelText("Username"), "svc");
    await user.type(screen.getByLabelText("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(createChannel).toHaveBeenCalled());
    const body = createChannel.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(body).toMatchObject({ smtp_username: "svc", smtp_password: "pw" });
    expect(body).not.toHaveProperty("secret");
    expect(body.config).toMatchObject({ auth: "password", host: "relay.lan", port: 587 });
  });

  it("explains a refused Microsoft 365 sign-in from the Test button", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.testNotificationChannel = vi
      .fn()
      .mockResolvedValue({ delivered: false, status_code: null, error: "sign_in_refused" });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderAdmin();

    await user.click(await screen.findByRole("button", { name: "Test" }));

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/Microsoft 365 refused the sign-in/);
    expect(status).not.toHaveTextContent("sign_in_refused");
  });
});
