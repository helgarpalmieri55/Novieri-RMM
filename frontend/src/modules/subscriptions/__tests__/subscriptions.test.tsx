import { readFileSync } from "node:fs";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, ClientOut, Me } from "../../../lib/api";
import { RenewalCalendar } from "../RenewalCalendar";
import { SpendDashboard } from "../SpendDashboard";
import { SubscriptionDetail } from "../SubscriptionDetail";
import { SubscriptionsPage } from "../SubscriptionsPage";
import type { VendorOut } from "../../licenses/types";
import type { UserListOut } from "../../settings/types";
import {
  OWNER_PICKER_EMPTY_HINT,
  VENDOR_PICKER_EMPTY_HINT,
  ownerName,
  ownerPickerOptions,
} from "../pickerOptions";
import { actionVariant, statusVariant } from "../subscriptionState";
import type {
  EventOut,
  IdleSeatRow,
  RenewalRow,
  SeatAssignmentOut,
  SpendSummary,
  SubscriptionOut,
} from "../types";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch -- mirrors modules/licenses/__tests__/licenses.test.tsx.
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

// THE STATIC ROUTES App.tsx ACTUALLY SERVES, read out of the route table
// rather than copied into this file. A copied list keeps passing after
// somebody renames a route -- which is the moment a tile goes dead.
//
// DYNAMIC ROUTES ARE EXCLUDED, AND THAT EXCLUSION IS WHAT GIVES THIS TEETH.
// `/licenses/:licenseId` matches "/licenses/breaches", so a typo'd tile would
// still "resolve" -- onto a detail page for an id that does not exist -- and a
// looser assertion here passed against exactly that, which is why it is
// written this way. `*` is excluded for the same reason twice over: it matches
// every string and it redirects to /devices. Every tile in this cluster points
// at a list or a report, so an exact static route is both the truth and
// falsifiable; a tile that one day needs a detail route will fail here, and
// its author can say so in the assertion rather than inheriting a check that
// never objected to anything.
function staticRoutePaths(): string[] {
  // `import.meta.url` under vitest is the module's dev-server URL, not a
  // file: one, so the path is taken from its pathname -- which is this file's
  // absolute path on disk -- and walked back to the src root.
  const here = new URL(import.meta.url).pathname;
  const source = readFileSync(here.replace(/\/src\/.*$/, "/src/App.tsx"), "utf8");
  return [...source.matchAll(/<Route\s[^>]*?path="([^"]+)"/g)]
    .flatMap((match) => (match[1] ? [match[1]] : []))
    .filter((path) => !path.includes("*") && !path.includes(":"));
}

function isServedRoute(href: string | null, routes: string[]): boolean {
  return href !== null && routes.includes(href);
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["subscriptions.read", "subscriptions.manage"],
  // THE ZONE IS PINNED IN THE FIXTURE ON PURPOSE (cycle 5). Asserting a
  // rendered time is only meaningful if the expected string does not depend on
  // the machine the suite runs on -- and asserting against the *profile* zone
  // rather than the browser's is the whole point of the change.
  timezone: "Asia/Tokyo",
};

// A bodyless 503 is what a proxy returns, and `ApiError` is built as
// `new ApiError(status, await response.text())` -- so `message` is "". That
// empty string is why `String(error)` rendered "ApiError:" with nothing after
// it. It is the exact shape of Auditor A's BLOCKER, so it is the shape every
// error test below uses.
function bodylessUnavailable(): ApiError {
  return new ApiError(503, "");
}

/**
 * Choose an option from an `EntityPicker`, the way a person does: focus the
 * combobox, then click the row. The box FILTERS; it does not accept typed
 * text as a value, which is its own test below.
 */
async function pick(
  user: ReturnType<typeof userEvent.setup>,
  pickerLabel: string,
  optionName: RegExp,
): Promise<void> {
  await user.click(screen.getByRole("combobox", { name: pickerLabel }));
  // Scoped to THIS picker's listbox: a `<select>` elsewhere on the page also
  // reports role `option`, and a helper that matched the first one anywhere
  // would pass while clicking the wrong control.
  const list = await screen.findByRole("listbox", { name: pickerLabel });
  await user.click(await within(list).findByRole("option", { name: optionName }));
}

/**
 * Press the confirm button in an open `ConfirmDialog`.
 *
 * Looked up INSIDE a `role="dialog"`, so a test cannot pass because the
 * confirmation was never rendered -- the whole point of cycle 8 is that these
 * clicks no longer go straight through.
 */
async function confirmWith(
  user: ReturnType<typeof userEvent.setup>,
  confirmLabel: string,
): Promise<void> {
  const dialogs = await screen.findAllByRole("dialog");
  const button = dialogs
    .map((dialog) => within(dialog).queryByRole("button", { name: confirmLabel }))
    .find((found): found is HTMLElement => found != null);
  if (button == null) throw new Error(`No open dialog offers "${confirmLabel}"`);
  await user.click(button);
}

const SUBSCRIPTIONS: SubscriptionOut[] = [
  {
    id: "s1",
    client_id: "o1",
    vendor_id: "v1",
    service_name: "Acme SaaS",
    plan: "Business",
    category: "productivity",
    billing_cycle: "monthly",
    cost_per_cycle: "99.00",
    currency: "USD",
    seats: 10,
    owner_user_id: null,
    started_at: "2025-01-01",
    renews_at: "2026-08-01",
    auto_renew: true,
    notice_days: 30,
    payment_ref: "PO-42",
    license_ref: null,
    status: "active",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const SEATS: SeatAssignmentOut[] = [
  {
    id: "seat1",
    subscription_id: "s1",
    assignee_name: "Jane Doe",
    assignee_email: "jane@example.com",
    assigned_at: "2026-01-02T00:00:00.000Z",
    released_at: null,
  },
];

const EVENTS: EventOut[] = [
  {
    id: "e1",
    subscription_id: "s1",
    type: "created",
    details: {},
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

describe("SubscriptionsPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listSubscriptions: vi.fn().mockResolvedValue(SUBSCRIPTIONS),
      createSubscription: vi.fn().mockResolvedValue(SUBSCRIPTIONS[0]),
      renewals: vi.fn().mockResolvedValue([]),
      spend: vi.fn().mockResolvedValue({ by_currency: [], by_vendor: [], by_category: [], by_owner: [] }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // `{ selector: ".cell-title a" }` ON EVERY "Acme SaaS" IN THIS DESCRIBE, and
  // it is a narrowing, not a loosening. The "No owner" tile now names the
  // subscriptions behind its count, and this fixture's one subscription has
  // no owner -- so the service name is on the page twice, in the tile foot
  // and in the row. These assertions have always meant the ROW: naming it is
  // stronger than matching whichever single element happened to carry the
  // text.
  it("lists subscriptions from the mocked api", async () => {
    renderWithProviders(<SubscriptionsPage />);
    expect(await screen.findByText("Acme SaaS", { selector: ".cell-title a" })).toBeInTheDocument();
  });

  it("says it is loading rather than showing an empty table", async () => {
    api.listSubscriptions = () => new Promise<never>(() => {});
    renderWithProviders(<SubscriptionsPage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.listSubscriptions = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<SubscriptionsPage />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No subscriptions yet/i)).not.toBeInTheDocument();
  });

  it("says there are no subscriptions when the list is genuinely empty", async () => {
    api.listSubscriptions = vi.fn().mockResolvedValue([]);
    renderWithProviders(<SubscriptionsPage />);
    expect(await screen.findByText("No subscriptions yet.")).toBeInTheDocument();
  });

  it("counts stats from the data rather than a constant", async () => {
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });
    const stats = document.querySelectorAll(".stat-card");
    const values = [...stats].map((card) => card.querySelector(".stat-value")?.textContent);
    // 1 active subscription, 0 renewals in the 90-day window, 0 notices due,
    // 1 subscription with no owner -- never the prototype's mock figures
    // (42 / $31.8k / 6 / 3 / $11.2k).
    expect(values).toEqual(["1", "0", "0", "1"]);
    expect(values).not.toContain("42");
  });

  // Cycle 4 / the swallowed-failure class. `renewals.data ?? []` made a
  // refused /subscriptions/renewals render "Renewals in 90 days: 0" and
  // "Notice deadlines due: 0" -- two reassuring figures no data supports, on
  // the one number in this module that is a contractual deadline.
  it("does not report a failed renewals query as zero deadlines due", async () => {
    api.renewals = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderWithProviders(<SubscriptionsPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");

    const stats = document.querySelectorAll(".stat-card");
    const values = [...stats].map((card) => card.querySelector(".stat-value")?.textContent);
    // Active subscriptions and the owner count still count -- their own
    // query succeeded. The two fed by the failed one are "n/a", not "0".
    expect(values).toEqual(["1", "n/a", "n/a", "1"]);
  });

  // "i dont want dead tiles everywhere". Every tile is an anchor, and every
  // href is a path App.tsx actually serves. Both renewal figures point at the
  // renewal calendar, which is the page that tells them apart ("action
  // required" against "on track").
  it("gives every stat tile a link to the page its number lives on", async () => {
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });

    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles).toHaveLength(4);
    expect(tiles.map((tile) => tile.tagName)).toEqual(["A", "A", "A", "A"]);
    expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([
      "/subscriptions",
      "/subscriptions/renewals",
      "/subscriptions/renewals",
      "/subscriptions",
    ]);

    const routes = staticRoutePaths();
    expect(routes.length).toBeGreaterThan(10);
    for (const tile of tiles) {
      expect(isServedRoute(tile.getAttribute("href"), routes)).toBe(true);
    }
  });

  // The run-rate tile appears only when spend came back in a single currency,
  // leads the row as v3 draws it, and points at the by-currency roll-up it is
  // literally computed from.
  it("points the run-rate tile at the spend roll-up it is computed from", async () => {
    api.spend = vi.fn().mockResolvedValue({
      by_currency: [
        {
          group_key: null,
          group_id: null,
          currency: "USD",
          monthly_total: "100.00",
          annual_total: "1200.00",
        },
      ],
      by_vendor: [],
      by_category: [],
      by_owner: [],
    });
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });

    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([
      "/subscriptions/spend",
      "/subscriptions",
      "/subscriptions/renewals",
      "/subscriptions/renewals",
      "/subscriptions",
    ]);
  });

  // WEIGHT IS URGENCY. A renewal ninety days out is a thing to plan; a notice
  // period closing is a thing to act on this week or pay for another year, so
  // it is the only tile on the row allowed to shout.
  it("weights the notice-deadline tile only when a notice period is closing", async () => {
    api.renewals = vi.fn().mockResolvedValue([
      {
        subscription_id: "s1",
        service_name: "Acme SaaS",
        renews_at: "2026-08-01",
        notice_deadline: "2026-07-02",
        days_to_notice: 0,
        days_to_renewal: 8,
        action_required: true,
      },
    ]);
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });

    // The mocked subscription has no owner, so v3's "No owner" tile shouts
    // too: a live contract nobody is accountable for is the one that renews
    // unread. The renewal-window tile still does not.
    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles.map((tile) => tile.classList.contains("urgent"))).toEqual([false, false, true, true]);
    expect(tiles.map((tile) => tile.classList.contains("tone-orange"))).toEqual([
      false,
      false,
      true,
      false,
    ]);
  });

  // ZERO IS A MEASUREMENT. With the default mock nothing renews and no notice
  // is closing -- and the tiles say so, rather than leaving two bare noughts
  // that read identically to a report that never ran.
  it("says which zero a zero is on the renewal tiles", async () => {
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });

    expect(screen.getByText("nothing renews in this window")).toBeInTheDocument();
    expect(screen.getByText("no notice period closing")).toBeInTheDocument();
  });

  // A BILLING CADENCE IS NOT A SEVERITY. `billingCycleKind` mapped annual to
  // `info` and everything else to `mut`, so a live monthly subscription drew
  // the muted pill with the minus glyph -- this console's mark for "not
  // applicable" -- one column from its real Status. None of the three
  // cadences is a state anybody acts on, so the column is text.
  it("draws the billing cycle as an attribute rather than a severity pill", async () => {
    renderWithProviders(<SubscriptionsPage />);
    const row = (
      await screen.findByText("Acme SaaS", { selector: ".cell-title a" })
    ).closest("tr") as HTMLElement;

    expect(within(row).getByText("Monthly")).toBeInTheDocument();
    // One pill on the row, and it is the one that means something.
    const pills = [...row.querySelectorAll<HTMLElement>("[data-kind]")];
    expect(pills).toHaveLength(1);
    expect(pills[0]).toHaveTextContent("Active");
    expect(pills[0]).toHaveAttribute("data-kind", "ok");
  });

  // v3's tile carries a breakdown under its figure; these counts stood alone.
  // Each foot is computed from the same query its number is, so a foot can
  // never contradict the figure above it.
  it("breaks each count down with the records behind it", async () => {
    api.renewals = vi.fn().mockResolvedValue([
      {
        subscription_id: "s1",
        service_name: "Acme SaaS",
        renews_at: "2026-08-01",
        notice_deadline: "2026-07-02",
        days_to_notice: 0,
        days_to_renewal: 8,
        action_required: true,
      },
    ]);
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });

    const feet = [...document.querySelectorAll<HTMLElement>(".stat-card")].map(
      (tile) => tile.querySelector(".stat-foot")?.textContent ?? null,
    );
    // Active subscriptions: the fixture's one subscription is the whole list,
    // so there is no share to state and the tile keeps its bare count.
    expect(feet).toEqual([null, "next Acme SaaS, 01 Aug 2026", "Acme SaaS", "Acme SaaS"]);
  });

  // A breakdown beside "nothing renews in this window" would contradict the
  // label above it, and a failed query has nothing to break down.
  it("puts no breakdown under a zero or under a failed query", async () => {
    api.renewals = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });

    const feet = [...document.querySelectorAll<HTMLElement>(".stat-card")].map(
      (tile) => tile.querySelector(".stat-foot")?.textContent ?? null,
    );
    // The two renewal tiles read "n/a" from the refused query; neither offers
    // a breakdown of it. The owner tile's own query succeeded, so it still
    // names the subscription nobody owns.
    expect(feet).toEqual([null, null, null, "Acme SaaS"]);
  });

  it("hides the New subscription control when the caller lacks subscriptions.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "subscriptions.manage"),
    });
    renderWithProviders(<SubscriptionsPage />);

    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });
    expect(screen.queryByRole("button", { name: "New subscription" })).not.toBeInTheDocument();
  });

  it("shows the New subscription control when the caller has subscriptions.manage", async () => {
    renderWithProviders(<SubscriptionsPage />);

    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });
    expect(screen.getByRole("button", { name: "New subscription" })).toBeInTheDocument();
  });

  // Both report pages are routed in App.tsx but contribute no top-level nav
  // entry of their own, so these links are the only way a user reaches them.
  it("links to the spend and renewal report pages", async () => {
    renderWithProviders(<SubscriptionsPage />);

    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });
    expect(screen.getByRole("link", { name: "Spend" })).toHaveAttribute(
      "href",
      "/subscriptions/spend",
    );
    expect(screen.getByRole("link", { name: "Renewals" })).toHaveAttribute(
      "href",
      "/subscriptions/renewals",
    );
  });

  it("hides the report links when the caller lacks subscriptions.read", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "subscriptions.read"),
    });
    renderWithProviders(<SubscriptionsPage />);

    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });
    expect(screen.queryByRole("link", { name: "Spend" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Renewals" })).not.toBeInTheDocument();
  });

  // Pins the NewSubscriptionDialog submit path: the dialog is driven the way
  // a user drives it (by label and role) and the exact create body is
  // asserted, so a form that renders but is wired to nothing fails here.
  it("wires the New subscription dialog to the real createSubscription mutation with the entered values", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SubscriptionsPage />);

    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });
    await user.click(screen.getByRole("button", { name: "New subscription" }));

    await user.type(screen.getByLabelText("Service name"), "Globex CRM");
    await user.selectOptions(screen.getByLabelText("Billing cycle"), "annual");
    await user.clear(screen.getByLabelText("Cost per cycle"));
    await user.type(screen.getByLabelText("Cost per cycle"), "1200.00");
    await user.clear(screen.getByLabelText("Currency"));
    await user.type(screen.getByLabelText("Currency"), "EUR");
    await user.clear(screen.getByLabelText("Seats"));
    await user.type(screen.getByLabelText("Seats"), "25");
    await user.click(screen.getByRole("button", { name: "Save" }));

    // The internal client (`/me`'s `org_id`) until phase 3's selector.
    expect(api.createSubscription).toHaveBeenCalledWith({
      client_id: "o1",
      service_name: "Globex CRM",
      billing_cycle: "annual",
      // Money is a Decimal server-side and travels as a string -- asserted
      // as the typed string, never a number.
      cost_per_cycle: "1200.00",
      currency: "EUR",
      // `seats` is a count, so it is the one legitimate Number() conversion.
      seats: 25,
      // Left blank here. "Renewal date not known" is a different fact from a
      // date and must not reach the API as the empty string.
      renews_at: null,
      // Cycle 6/7 added the three fields the "Notice deadlines due" tile is
      // computed from, plus the vendor and owner lookups. Unchosen lookups are
      // `null`, never "" -- the API types both as `UUID | None`.
      vendor_id: null,
      owner_user_id: null,
      notice_days: 0,
      auto_renew: false,
    });
  });

  // The form-kernel findings on this route. Each of these fails against the
  // pre-kernel dialog, which rendered `<Button disabled={create.isPending ||
  // !serviceName}>` with no <form>, no rules and no live region.
  async function openNewSubscription() {
    const user = userEvent.setup();
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });
    await user.click(screen.getByRole("button", { name: "New subscription" }));
    return user;
  }

  // Audit: "Empty submit is a silent no-op on the subscription form". The
  // click used to land on a disabled control -- nothing moved and no field
  // was named.
  it("names the missing field on an empty submit instead of disabling Save", async () => {
    const user = await openNewSubscription();

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByRole("alert")).toHaveTextContent("Service name is required");
    expect(api.createSubscription).not.toHaveBeenCalled();
  });

  // Audit: "Cost and renewal fields accept nonsense values". Cost is a
  // Decimal carried as a string, so nothing between the keyboard and pydantic
  // parses it -- this used to be accepted here and 422 there.
  it("refuses a cost per cycle that is not a plain decimal amount", async () => {
    const user = await openNewSubscription();

    await user.type(screen.getByLabelText("Service name"), "Globex CRM");
    await user.clear(screen.getByLabelText("Cost per cycle"));
    await user.type(screen.getByLabelText("Cost per cycle"), "twelve hundred");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Cost per cycle must be an amount/);
    expect(api.createSubscription).not.toHaveBeenCalled();
  });

  // The three defaulted fields (0.00 / USD / 0) were never checked at all,
  // so clearing one submitted it empty.
  it("refuses a cleared Currency rather than sending an empty one", async () => {
    const user = await openNewSubscription();

    await user.type(screen.getByLabelText("Service name"), "Globex CRM");
    await user.clear(screen.getByLabelText("Currency"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Currency is required");
    expect(api.createSubscription).not.toHaveBeenCalled();
  });

  it("refuses a currency that is not a 3-letter code", async () => {
    const user = await openNewSubscription();

    await user.type(screen.getByLabelText("Service name"), "Globex CRM");
    await user.clear(screen.getByLabelText("Currency"));
    await user.type(screen.getByLabelText("Currency"), "US");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/3-letter code/);
    expect(api.createSubscription).not.toHaveBeenCalled();
  });

  // `Number("-4")` is -4, and the idle-seat report subtracts assignments from
  // whatever this sends.
  it("refuses a negative seat count", async () => {
    const user = await openNewSubscription();

    await user.type(screen.getByLabelText("Service name"), "Globex CRM");
    await user.clear(screen.getByLabelText("Seats"));
    await user.type(screen.getByLabelText("Seats"), "-4");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Seats must be a whole number/);
    expect(api.createSubscription).not.toHaveBeenCalled();
  });

  // The renewal date the module's own Renewals and Notice-deadline stats are
  // computed from had no input on this form at all, so every subscription
  // created here was invisible to both. It is a date input, so the only
  // values it can produce are empty or ISO.
  it("sends the renewal date as an ISO date from a real date input", async () => {
    const user = await openNewSubscription();

    await user.type(screen.getByLabelText("Service name"), "Globex CRM");
    const renews = screen.getByLabelText("Renews on (optional)");
    expect(renews).toHaveAttribute("type", "date");
    await user.type(renews, "2026-12-01");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createSubscription).toHaveBeenCalledWith(
      expect.objectContaining({ renews_at: "2026-12-01" }),
    );
  });

  // The dialog is a real <form> now, which is what makes Enter work at all --
  // ui.tsx's Button sets no type, so before this the submit control was an
  // orphan and Enter did nothing anywhere in the product.
  it("submits on Enter from a field, not only on a mouse click", async () => {
    const user = await openNewSubscription();

    await user.type(screen.getByLabelText("Service name"), "Globex CRM{Enter}");

    expect(api.createSubscription).toHaveBeenCalledWith(
      expect.objectContaining({ service_name: "Globex CRM" }),
    );
  });
});

describe("SubscriptionDetail", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      getSubscription: vi.fn().mockResolvedValue(SUBSCRIPTIONS[0]),
      listSeats: vi.fn().mockResolvedValue(SEATS),
      listEvents: vi.fn().mockResolvedValue(EVENTS),
      assignSeat: vi.fn().mockResolvedValue({
        id: "seat2",
        subscription_id: "s1",
        assignee_name: "New Hire",
        assignee_email: "new@example.com",
        assigned_at: "2026-02-01T00:00:00.000Z",
        released_at: null,
      }),
      releaseSeat: vi.fn().mockResolvedValue({ ...SEATS[0], released_at: "2026-02-02T00:00:00.000Z" }),
      cancelSubscription: vi.fn().mockResolvedValue({ ...SUBSCRIPTIONS[0], status: "cancelled" }),
      renewSubscription: vi.fn().mockResolvedValue(SUBSCRIPTIONS[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/subscriptions/:subscriptionId" element={<SubscriptionDetail />} />
      </Routes>,
      ["/subscriptions/s1"],
    );
  }

  it("shows the Assign seat and Cancel controls when the caller has subscriptions.manage", async () => {
    renderDetail();

    await screen.findByText("Acme SaaS");
    expect(screen.getByRole("button", { name: "Assign seat" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel subscription" })).toBeInTheDocument();
  });

  it("hides the Assign seat and Cancel controls when the caller lacks subscriptions.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "subscriptions.manage"),
    });
    renderDetail();

    await screen.findByText("Acme SaaS");
    expect(screen.queryByRole("button", { name: "Assign seat" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel subscription" })).not.toBeInTheDocument();
    // Read-only view of the seat still shows, only the mutating action goes away.
    expect(screen.getByText("Jane Doe")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Release" })).not.toBeInTheDocument();
  });

  // TWO PILLS, TWO KINDS OF MEANING, NOTHING TELLING THEM APART: the head set
  // the cadence in the same shape as the status, and for a monthly
  // subscription that shape carried the muted minus glyph beside a genuine
  // "Active". The cadence is an attribute of the record, so it reads as one.
  //
  // THE ASSERTION MOVED WITH THE METADATA, AND IT GOT STRICTER. It used to
  // read "exactly one `[data-kind]` inside `.ph`, and it says Active" --
  // correct while the status pill lived in the head's ACTION row, which is
  // where design review M14 found it: a thing that cannot be pressed, sitting
  // in the row of things you press, between the title and "Record renewal".
  // Every other record page in the console (/service-desk/tickets/:id,
  // /portal/tickets/:id, /devices/:id) puts state in a pill row UNDER the
  // head, so that is where these two went. The property this guards is
  // unchanged and now has a second half: the cadence is still not a pill, and
  // neither it nor the status is in the button row at all.
  it("does not dress the billing cadence as a second status pill, and keeps neither in the button row", async () => {
    renderDetail();

    await screen.findByText("Acme SaaS");

    const head = document.querySelector(".ph") as HTMLElement;
    expect(head.querySelectorAll("[data-kind]")).toHaveLength(0);
    expect(head).not.toHaveTextContent("Billed monthly");

    const cadence = screen.getByText("Billed monthly");
    // A chip, not a pill: no `data-kind`, so it claims no severity and carries
    // no kind glyph.
    expect(cadence).toHaveClass("chip");
    expect(cadence.hasAttribute("data-kind")).toBe(false);

    // The status and the cadence share ONE row, and it is not the head's.
    const meta = cadence.parentElement as HTMLElement;
    expect(meta.closest(".ph")).toBeNull();
    const pills = [...meta.querySelectorAll<HTMLElement>("[data-kind]")];
    expect(pills).toHaveLength(1);
    expect(pills[0]).toHaveTextContent("Active");
  });

  it("wires the Release control to the real releaseSeat mutation with the seat id", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Jane Doe");
    await user.click(screen.getByRole("button", { name: "Release" }));
    await confirmWith(user, "Release seat");

    expect(api.releaseSeat).toHaveBeenCalledWith("seat1");
  });

  // The Phase-11 lesson: a seats panel that looked right but was wired to
  // nothing. The Assign path is the one that creates a seat, so it gets a
  // test that drives the real form controls end to end.
  it("wires the Assign seat control to the real assignSeat mutation with the typed name and email", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Acme SaaS");
    await user.type(screen.getByLabelText("Assignee name"), "New Hire");
    await user.type(screen.getByLabelText("Assignee email"), "new@example.com");
    await user.click(screen.getByRole("button", { name: "Assign seat" }));

    expect(api.assignSeat).toHaveBeenCalledWith("s1", {
      assignee_name: "New Hire",
      assignee_email: "new@example.com",
    });
  });

  // Cycle 5. `assigned_at`/`released_at` are `datetime` -- instants -- so they
  // take the operator's PROFILE zone, and a seat still held renders NO_DATE
  // rather than "Invalid Date".
  it("renders seat instants in the profile timezone and an unreleased seat as an em dash", async () => {
    api.listSeats = vi
      .fn()
      .mockResolvedValue([{ ...SEATS[0], assigned_at: "2026-01-01T23:00:00.000Z" }]);
    renderDetail();

    // 23:00Z is 08:00 the NEXT DAY in Asia/Tokyo. A browser-zone rendering
    // cannot produce this string unless the runner happens to be in Tokyo.
    const row = (await screen.findByText("Jane Doe")).closest("tr") as HTMLElement;
    expect(within(row).getByText("02 Jan 2026, 08:00")).toBeInTheDocument();
    expect(within(row).getByText("—")).toBeInTheDocument();
    expect(row.textContent).not.toContain("Invalid Date");
  });

  it("renders an event timestamp in the profile timezone", async () => {
    api.listEvents = vi
      .fn()
      .mockResolvedValue([{ ...EVENTS[0], created_at: "2026-01-01T23:00:00.000Z" }]);
    renderDetail();

    expect(await screen.findByText("02 Jan 2026, 08:00")).toBeInTheDocument();
  });

  // The other half of cycle 5: `started_at`/`renews_at` are `date`, so they
  // must NOT move with the zone. West of UTC a zone-aware rendering of
  // "2026-08-01" reads 31 Jul -- an off-by-one-day that reads as real data and
  // that the renewal calendar's own arithmetic would then contradict.
  it("renders the renewal DATE as the day recorded, not a zone-shifted one", async () => {
    api.me = vi.fn().mockResolvedValue({ ...ME, timezone: "America/Los_Angeles" });
    renderDetail();

    await screen.findByText("Acme SaaS");
    expect(screen.getByText("01 Aug 2026")).toBeInTheDocument();
    expect(screen.queryByText(/31 Jul 2026/)).not.toBeInTheDocument();
  });

  // Cycle 4, on the destructive control. `String(cancel.error)` rendered
  // "ApiError:" and nothing after it for a bodyless 503, in no live region --
  // Auditor A's BLOCKER exactly: a failed destructive call that the UI did not
  // report.
  it("announces a bodyless 503 from Cancel subscription instead of looking successful", async () => {
    const user = userEvent.setup();
    api.cancelSubscription = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderDetail();

    await screen.findByText("Acme SaaS");
    await user.click(screen.getByRole("button", { name: "Cancel subscription" }));
    await confirmWith(user, "Cancel subscription");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert).toHaveTextContent(/Nothing was changed/i);
    expect(alert.textContent).not.toContain("ApiError");
    // And the subscription is still shown as active, which is now consistent
    // with what the page says rather than contradicting it.
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("announces a failed Release instead of leaving the seat looking released", async () => {
    const user = userEvent.setup();
    api.releaseSeat = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderDetail();

    await screen.findByText("Jane Doe");
    await user.click(screen.getByRole("button", { name: "Release" }));
    await confirmWith(user, "Release seat");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");
  });

  it("announces a failed Assign seat as a sentence", async () => {
    const user = userEvent.setup();
    api.assignSeat = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderDetail();

    await screen.findByText("Acme SaaS");
    await user.type(screen.getByLabelText("Assignee name"), "New Hire");
    await user.type(screen.getByLabelText("Assignee email"), "new@example.com");
    await user.click(screen.getByRole("button", { name: "Assign seat" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");
  });

  // `disabled={assignSeat.isPending || !assigneeName || !assigneeEmail}` was
  // the silent no-op: the click moved nothing and named neither field.
  it("names the missing seat fields on an empty submit instead of disabling Assign", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Acme SaaS");
    const assign = screen.getByRole("button", { name: "Assign seat" });
    expect(assign).toBeEnabled();
    await user.click(assign);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Assignee name is required");
    expect(alert).toHaveTextContent("Assignee email is required");
    expect(api.assignSeat).not.toHaveBeenCalled();
  });

  it("refuses an assignee email that is not an address", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Acme SaaS");
    await user.type(screen.getByLabelText("Assignee name"), "New Hire");
    await user.type(screen.getByLabelText("Assignee email"), "new-at-example");
    await user.click(screen.getByRole("button", { name: "Assign seat" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/must be an address/);
    expect(api.assignSeat).not.toHaveBeenCalled();
  });

  it("wires the Cancel subscription control to the real cancelSubscription mutation", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Acme SaaS");
    await user.click(screen.getByRole("button", { name: "Cancel subscription" }));
    await confirmWith(user, "Cancel subscription");

    expect(api.cancelSubscription).toHaveBeenCalledWith("s1");
  });

  it("wires the Record renewal control to the real renewSubscription mutation with the subscription id", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Acme SaaS");
    await user.click(screen.getByRole("button", { name: "Record renewal" }));
    await confirmWith(user, "Record renewal");

    expect(api.renewSubscription).toHaveBeenCalledWith("s1");
  });
});

describe("RenewalCalendar", () => {
  // reporting.py's renewal_calendar only returns subscriptions whose renewal
  // falls inside `within_days`, so a wider window returns a superset -- the
  // mock models that by keying off the window it is called with.
  const NEAR: RenewalRow = {
    subscription_id: "s1",
    service_name: "Acme SaaS",
    renews_at: "2026-08-01",
    notice_deadline: "2026-07-02",
    days_to_notice: 0,
    days_to_renewal: 8,
    action_required: true,
  };
  const FAR: RenewalRow = {
    subscription_id: "s2",
    service_name: "Globex CRM",
    renews_at: "2026-09-20",
    notice_deadline: "2026-08-21",
    days_to_notice: 28,
    days_to_renewal: 58,
    action_required: false,
  };

  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      renewals: vi
        .fn()
        .mockImplementation((withinDays?: number) =>
          Promise.resolve((withinDays ?? 30) >= 60 ? [NEAR, FAR] : [NEAR]),
        ),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("renders the renewal rows from the mocked api", async () => {
    renderWithProviders(<RenewalCalendar />);

    const row = (await screen.findByText("Acme SaaS")).closest("tr") as HTMLElement;
    // Cycle 5: one date format for the whole product. `notice_deadline` and
    // `renews_at` are `date` in schema.py -- calendar days with no clock -- so
    // they are pinned to UTC and read the same from every machine.
    expect(within(row).getByText("02 Jul 2026")).toBeInTheDocument();
    expect(within(row).getByText("01 Aug 2026")).toBeInTheDocument();
    expect(within(row).getByText("Action required")).toBeInTheDocument();
  });

  // reporting.py derives `notice_deadline` from `renews_at` in WHOLE DAYS.
  // Rendering either through a zone west of UTC moves it a day earlier, which
  // on a cancellation-notice deadline is the difference between "cancel today"
  // and "you were locked in yesterday". The suite's own runner zone must not
  // be able to change this.
  it("does not shift a notice deadline by a day into the operator's zone", async () => {
    api.renewals = vi.fn().mockResolvedValue([{ ...NEAR, notice_deadline: "2026-07-01" }]);
    renderWithProviders(<RenewalCalendar />);

    const row = (await screen.findByText("Acme SaaS")).closest("tr") as HTMLElement;
    expect(within(row).getByText("01 Jul 2026")).toBeInTheDocument();
    expect(row.textContent).not.toContain("30 Jun 2026");
  });

  // An absent deadline must be the em dash, never "Invalid Date" -- which is
  // what `new Date(null).toLocaleDateString()` produces.
  it("renders an absent notice deadline as an em dash, never Invalid Date", async () => {
    api.renewals = vi
      .fn()
      .mockResolvedValue([{ ...NEAR, notice_deadline: null, renews_at: null }]);
    renderWithProviders(<RenewalCalendar />);

    const row = (await screen.findByText("Acme SaaS")).closest("tr") as HTMLElement;
    expect(within(row).getAllByText("—")).toHaveLength(2);
    expect(row.textContent).not.toContain("Invalid Date");
  });

  it("requests the backend's default 30-day window on first render", async () => {
    renderWithProviders(<RenewalCalendar />);

    await screen.findByText("Acme SaaS");
    // api.py::renewals_route defaults `within_days` to 30.
    expect(api.renewals).toHaveBeenCalledWith(30);
    expect(screen.queryByText("Globex CRM")).not.toBeInTheDocument();
  });

  it("refetches with the wider window when the control changes, and the rows update", async () => {
    const user = userEvent.setup();
    renderWithProviders(<RenewalCalendar />);

    await screen.findByText("Acme SaaS");
    await user.selectOptions(screen.getByLabelText("Window"), "60");

    expect(await screen.findByText("Globex CRM")).toBeInTheDocument();
    expect(api.renewals).toHaveBeenCalledWith(60);
  });
});

describe("SpendDashboard", () => {
  const EMPTY_SPEND: SpendSummary = {
    by_currency: [],
    by_vendor: [],
    by_category: [],
    by_owner: [],
  };

  const IDLE_SEATS: IdleSeatRow[] = [
    {
      subscription_id: "s1",
      service_name: "Acme SaaS",
      seats: 10,
      assigned: 4,
      idle: 6,
      currency: "USD",
      // Decimal on the wire -- a string, asserted as a string.
      idle_monthly_cost: "59.40",
    },
  ];

  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      spend: vi.fn().mockResolvedValue(EMPTY_SPEND),
      idleSeats: vi.fn().mockResolvedValue(IDLE_SEATS),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // MSP clients phase 2: a subscription is one client's, so every roll-up
  // row is one client's -- never summed across clients -- and says whose.
  it("names each spend row's client, never adding two clients together", async () => {
    const client = (id: string, name: string): ClientOut => ({
      id, name, kind: "customer", status: "active", email_domains: [], phone: "", address: "",
      website: "", notes: "", enabled_modules: [], device_count: 0, site_count: 0,
    });
    api.me = vi.fn().mockResolvedValue({ ...ME, clients: [{ id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: ME.permissions }, { id: "c2", name: "Globex", kind: "customer", enabled_modules: [], permissions: ME.permissions }] });
    api.listClients = vi.fn().mockResolvedValue([client("o1", "Novieri"), client("c2", "Globex")]);
    const total = { group_key: null, group_type: null, group_id: null, currency: "USD" };
    api.spend = vi.fn().mockResolvedValue({
      ...EMPTY_SPEND,
      by_currency: [
        { ...total, client_id: "o1", monthly_total: "100.00", annual_total: "1200.00" },
        { ...total, client_id: "c2", monthly_total: "40.00", annual_total: "480.00" },
      ],
    });
    renderWithProviders(<SpendDashboard />);

    const novieri = (await screen.findByText("Novieri")).closest("tr") as HTMLElement;
    expect(within(novieri).getByText("100.00")).toBeInTheDocument();
    const globex = screen.getByText("Globex").closest("tr") as HTMLElement;
    expect(within(globex).getByText("40.00")).toBeInTheDocument();
    expect(screen.queryByText("140.00")).not.toBeInTheDocument();
  });

  it("renders the idle-seat rows from the mocked api", async () => {
    renderWithProviders(<SpendDashboard />);

    expect(await screen.findByText("Idle seats")).toBeInTheDocument();
    const row = (await screen.findByText("Acme SaaS")).closest("tr") as HTMLElement;
    expect(within(row).getByText("6")).toBeInTheDocument();
    expect(within(row).getByText("USD")).toBeInTheDocument();
    // The Decimal cost is rendered verbatim as the string the API returns --
    // never coerced to a number.
    expect(within(row).getByText("59.40")).toBeInTheDocument();
  });
});

describe("subscription state vocabulary", () => {
  it("maps a real backend status to a non-fallback badge variant", () => {
    // Subscription.status vocabulary is sourced from the backend
    // (backend/app/modules/subscriptions/schema.py's SubStatus Literal) --
    // "active" is a real status value.
    expect(statusVariant("active")).not.toBe("muted");
    expect(statusVariant("active")).toBe("success");
  });

  it("falls back to muted for an unknown/invented status", () => {
    // Guards against the Phase-5 regression: no invented status name may
    // resolve to a real variant.
    expect(statusVariant("not_a_real_status")).toBe("muted");
  });

  it("maps action_required=true to destructive", () => {
    expect(actionVariant(true)).toBe("destructive");
  });

  it("maps action_required=false to muted", () => {
    expect(actionVariant(false)).toBe("muted");
  });
});

// ---------------------------------------------------------------------------
// UX remediation cycles 6, 7 and 8 on this module.
// ---------------------------------------------------------------------------

const USERS: UserListOut = {
  users: [
    { id: "u9", email: "owner@example.com", display_name: "Priya Owner", status: "active", roles: [] },
    { id: "u8", email: "gone@example.com", display_name: "Former Owner", status: "disabled", roles: [] },
  ],
};

const VENDOR_ROWS: VendorOut[] = [
  {
    id: "v1",
    name: "Acme Software",
    website: "https://acme.example",
    contacts: [],
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

// Auditor B, [HIGH]: "There is no renewal date, no term end, no notice period
// and no owner field ... Two of three headline metrics -- and the module's
// stated differentiator, cancellation-notice deadlines -- depend on data the
// UI provides no way to enter."
describe("New subscription: the fields the headline KPIs are computed from", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listSubscriptions: vi.fn().mockResolvedValue(SUBSCRIPTIONS),
      renewals: vi.fn().mockResolvedValue([]),
      spend: vi.fn().mockResolvedValue({ by_currency: [], by_vendor: [], by_category: [], by_owner: [] }),
      listVendors: vi.fn().mockResolvedValue(VENDOR_ROWS),
      listUsers: vi.fn().mockResolvedValue(USERS),
      createSubscription: vi.fn().mockResolvedValue(SUBSCRIPTIONS[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openDialog() {
    const user = userEvent.setup();
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });
    await user.click(screen.getByRole("button", { name: "New subscription" }));
    return user;
  }

  it("sends the notice period, the auto-renew flag, the owner and the vendor", async () => {
    const user = await openDialog();

    await user.type(screen.getByLabelText("Service name"), "Globex CRM");
    await user.clear(screen.getByLabelText("Notice period (days)"));
    await user.type(screen.getByLabelText("Notice period (days)"), "60");
    await user.click(screen.getByLabelText(/Renews automatically/));
    await pick(user, "Owner (optional)", /Priya Owner/);
    await pick(user, "Vendor (optional)", /Acme Software/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createSubscription).toHaveBeenCalledWith(
      expect.objectContaining({
        service_name: "Globex CRM",
        notice_days: 60,
        auto_renew: true,
        owner_user_id: "u9",
        vendor_id: "v1",
      }),
    );
  });

  // The server's own default is `auto_renew: bool = False`, and the tile is
  // `action_required = auto_renew and today >= notice_deadline` -- so a
  // checkbox defaulted true would manufacture deadlines rather than report
  // them.
  it("defaults auto-renew to the server's own default rather than guessing true", async () => {
    await openDialog();
    expect(screen.getByLabelText(/Renews automatically/)).not.toBeChecked();
  });

  it("refuses a notice period that is not a plausible number of days", async () => {
    const user = await openDialog();

    await user.type(screen.getByLabelText("Service name"), "Globex CRM");
    await user.clear(screen.getByLabelText("Notice period (days)"));
    await user.type(screen.getByLabelText("Notice period (days)"), "400");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/365 days or fewer/);
    expect(api.createSubscription).not.toHaveBeenCalled();
  });

  it("explains, in the form, why the Notice deadlines tile can otherwise only read zero", async () => {
    await openDialog();

    // The number alone means nothing without the rule it feeds. Auditor B's
    // finding was that the tile was uncomputable from this form; saying so
    // beside the field is what stops it being left at 0 by accident.
    expect(
      screen.getByText(/counts auto-renewing subscriptions whose deadline has arrived/i),
    ).toBeInTheDocument();
  });

  it("says what would fill an empty owner list rather than offering a dead one", async () => {
    api.listUsers = vi.fn().mockResolvedValue({ users: [] });
    const user = await openDialog();

    await user.click(screen.getByRole("combobox", { name: "Owner (optional)" }));

    const list = await screen.findByRole("listbox", { name: "Owner (optional)" });
    // Users are created on Settings → Users, not Organization (the hint named
    // the wrong tab until cross-module guidance, doc §4), and the dialog links
    // straight there.
    expect(list).toHaveTextContent(/Settings → Users/);
    // `users.read` is not implied by `subscriptions.manage`, so the hint must
    // not assert that the directory is empty.
    expect(list).toHaveTextContent(/permission/i);
    expect(screen.getByRole("link", { name: "Add a user" })).toHaveAttribute("href", "/settings?tab=users");
  });

  it("keeps the form usable when the owner directory is refused outright", async () => {
    // A 403 on `/users` must not stop a subscription being recorded: owner is
    // an optional field.
    api.listUsers = vi.fn().mockRejectedValue(new ApiError(403, ""));
    const user = await openDialog();

    await user.type(screen.getByLabelText("Service name"), "Globex CRM");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createSubscription).toHaveBeenCalledWith(
      expect.objectContaining({ service_name: "Globex CRM", owner_user_id: null }),
    );
  });
});

// Cycle 8: three privileged controls that fired on one click.
describe("SubscriptionDetail: the privileged controls ask first", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      getSubscription: vi.fn().mockResolvedValue({ ...SUBSCRIPTIONS[0], owner_user_id: "u9" }),
      listSeats: vi.fn().mockResolvedValue(SEATS),
      listEvents: vi.fn().mockResolvedValue(EVENTS),
      listUsers: vi.fn().mockResolvedValue(USERS),
      releaseSeat: vi.fn().mockResolvedValue({ ...SEATS[0], released_at: "2026-02-02T00:00:00.000Z" }),
      cancelSubscription: vi.fn().mockResolvedValue({ ...SUBSCRIPTIONS[0], status: "cancelled" }),
      renewSubscription: vi.fn().mockResolvedValue(SUBSCRIPTIONS[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/subscriptions/:subscriptionId" element={<SubscriptionDetail />} />
      </Routes>,
      ["/subscriptions/s1"],
    );
  }

  it("cancels nothing on the first click, and names the contract and its cost", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Acme SaaS");
    await user.click(screen.getByRole("button", { name: "Cancel subscription" }));

    expect(api.cancelSubscription).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog", { name: /Cancel subscription/i });
    expect(dialog).toHaveTextContent("Acme SaaS, 99.00 USD per monthly cycle");
    // The consequence the operator most needs: cancelling here is a record,
    // not a notice to the vendor.
    expect(dialog).toHaveTextContent(/does not tell the vendor/i);
  });

  it("cancels nothing when the confirmation is dismissed", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Acme SaaS");
    await user.click(screen.getByRole("button", { name: "Cancel subscription" }));
    const dialog = await screen.findByRole("dialog", { name: /Cancel subscription/i });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(api.cancelSubscription).not.toHaveBeenCalled();
  });

  it("records no renewal on the first click, and says the date moves", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Acme SaaS");
    await user.click(screen.getByRole("button", { name: "Record renewal" }));

    expect(api.renewSubscription).not.toHaveBeenCalled();
    expect(await screen.findByRole("dialog", { name: /Record a renewal/i })).toHaveTextContent(
      /Moves the renewal date forward by one billing cycle/i,
    );
  });

  it("releases no seat on the first click, and names whose seat it is", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Jane Doe");
    await user.click(screen.getByRole("button", { name: "Release" }));

    expect(api.releaseSeat).not.toHaveBeenCalled();
    // The subject is the assignee: releasing the seat below the one intended
    // is exactly the mistake this catches.
    expect(await screen.findByRole("dialog", { name: /Release seat/i })).toHaveTextContent("Jane Doe");
  });

  // Cycle 6, read side: this line printed `owner_user_id` verbatim.
  it("renders the owner as a name, not as a raw user id", async () => {
    renderDetail();

    expect(await screen.findByText("Priya Owner")).toBeInTheDocument();
    expect(screen.queryByText("u9")).not.toBeInTheDocument();
  });

  it("falls back to the id rather than a blank when the directory is refused", async () => {
    // An id whose account this caller may not read is still more than nothing,
    // and "—" for a subscription that HAS an owner would be the worse lie.
    api.listUsers = vi.fn().mockRejectedValue(new ApiError(403, ""));
    renderDetail();

    expect(await screen.findByText("u9")).toBeInTheDocument();
  });
});

describe("subscriptions picker options", () => {
  it("distinguishes people by email", () => {
    expect(ownerPickerOptions(USERS.users)[0]).toEqual({
      id: "u9",
      label: "Priya Owner",
      sublabel: "owner@example.com",
    });
  });

  // A `deleted` or `disabled` row is RETAINED and still listed, and a renewal
  // deadline owned by a disabled account is owned by nobody.
  it("marks an account that is not active", () => {
    expect(ownerPickerOptions(USERS.users)[1]!.sublabel).toBe("gone@example.com · disabled");
  });

  it("falls back to the email when a person has no display name", () => {
    expect(ownerPickerOptions([{ ...USERS.users[0]!, display_name: "" }])[0]!.label).toBe(
      "owner@example.com",
    );
  });

  it("resolves a stored owner id to a name, and an unknown one to the id itself", () => {
    const options = ownerPickerOptions(USERS.users);
    expect(ownerName(options, "u9")).toBe("Priya Owner");
    expect(ownerName(options, "u-not-in-list")).toBe("u-not-in-list");
    expect(ownerName(options, null)).toBeNull();
  });

  it("names the surface that fills each list, and the permission that hides it", () => {
    expect(OWNER_PICKER_EMPTY_HINT).toMatch(/Settings → Users/);
    expect(OWNER_PICKER_EMPTY_HINT).toMatch(/permission/i);
    expect(VENDOR_PICKER_EMPTY_HINT).toMatch(/Licenses → Vendors/);
    expect(VENDOR_PICKER_EMPTY_HINT).toMatch(/permission/i);
  });
});

// Cycle 7. Adding the deadline fields to the create form fixes tomorrow's
// subscriptions and nothing already recorded -- and
// `PATCH /subscriptions/{subscription_id}` has served this since the module
// shipped, with no caller.
describe("SubscriptionDetail: editing the renewal terms", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      getSubscription: vi
        .fn()
        .mockResolvedValue({ ...SUBSCRIPTIONS[0], notice_days: 30, auto_renew: true }),
      listSeats: vi.fn().mockResolvedValue(SEATS),
      listEvents: vi.fn().mockResolvedValue(EVENTS),
      listUsers: vi.fn().mockResolvedValue(USERS),
      updateSubscription: vi.fn().mockResolvedValue(SUBSCRIPTIONS[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/subscriptions/:subscriptionId" element={<SubscriptionDetail />} />
      </Routes>,
      ["/subscriptions/s1"],
    );
  }

  it("loads the terms already recorded rather than starting blank", async () => {
    renderDetail();

    await screen.findByText("Renewal terms");
    expect(screen.getByLabelText("Notice period (days)")).toHaveValue(30);
    expect(screen.getByLabelText(/Renews automatically/)).toBeChecked();
  });

  it("saves the notice period, the auto-renew flag and the owner", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Renewal terms");
    await user.clear(screen.getByLabelText("Notice period (days)"));
    await user.type(screen.getByLabelText("Notice period (days)"), "90");
    await user.click(screen.getByLabelText(/Renews automatically/));
    await pick(user, "Owner", /Priya Owner/);
    await user.click(screen.getByRole("button", { name: "Save renewal terms" }));

    expect(api.updateSubscription).toHaveBeenCalledWith("s1", {
      renews_at: "2026-08-01",
      notice_days: 90,
      auto_renew: false,
      owner_user_id: "u9",
    });
  });

  it("sends a cleared renewal date as null, not as an empty string", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Renewal terms");
    await user.clear(screen.getByLabelText("Renews on"));
    await user.click(screen.getByRole("button", { name: "Save renewal terms" }));

    expect(api.updateSubscription).toHaveBeenCalledWith(
      "s1",
      expect.objectContaining({ renews_at: null }),
    );
  });

  it("names the bad field instead of leaving the save silent", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Renewal terms");
    await user.clear(screen.getByLabelText("Notice period (days)"));
    await user.type(screen.getByLabelText("Notice period (days)"), "400");
    await user.click(screen.getByRole("button", { name: "Save renewal terms" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/365 days or fewer/);
    expect(api.updateSubscription).not.toHaveBeenCalled();
  });

  it("announces a bodyless 503 rather than reporting a save that did not happen", async () => {
    const user = userEvent.setup();
    api.updateSubscription = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderDetail();

    await screen.findByText("Renewal terms");
    await user.click(screen.getByRole("button", { name: "Save renewal terms" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");
    expect(screen.queryByText("Renewal terms saved.")).not.toBeInTheDocument();
  });

  it("is not offered on a cancelled subscription, whose terms no longer apply", async () => {
    api.getSubscription = vi
      .fn()
      .mockResolvedValue({ ...SUBSCRIPTIONS[0], status: "cancelled" });
    renderDetail();

    await screen.findByText("Acme SaaS");
    expect(screen.queryByText("Renewal terms")).not.toBeInTheDocument();
  });

  it("is not offered to a caller without subscriptions.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "subscriptions.manage"),
    });
    renderDetail();

    await screen.findByText("Acme SaaS");
    expect(screen.queryByText("Renewal terms")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Cycle 12.
// ---------------------------------------------------------------------------

/**
 * Auditor B, [BLOCKER]: "Submitting `Cost per cycle = "not-a-number!!"` and
 * `Currency = "ZZZZZ"` renders this literal string inside the modal:
 * `Error: {"detail":[{"type":"decimal_parsing", ... "ctx":{"max_length":3}}]}`
 * -- two distinct field errors concatenated into one unreadable blob."
 *
 * The client-side rules added in cycle 6 stop those two exact values ever
 * reaching the wire, and there are tests above for both. THEY DO NOT CLOSE
 * THIS FINDING: the finding is about what this dialog does with a 422 body it
 * DID receive, and every field this form does not validate (and every rule the
 * server tightens after this build ships) still lands there. So the payload is
 * injected directly, with the form filled in values the client rules accept.
 *
 * This is the last unguarded rendering path in the module: `SubscriptionsPage`
 * used to print `String(create.error)`, which is the string above.
 */
describe("New subscription: a server 422 is a sentence, not its payload", () => {
  // The auditor's captured body, verbatim.
  const PYDANTIC_422 = JSON.stringify({
    detail: [
      {
        type: "decimal_parsing",
        loc: ["body", "cost_per_cycle"],
        msg: "Input should be a valid decimal",
        input: "not-a-number!!",
      },
      {
        type: "string_too_long",
        loc: ["body", "currency"],
        msg: "String should have at most 3 characters",
        input: "ZZZZZ",
        ctx: { max_length: 3 },
      },
    ],
  });

  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listSubscriptions: vi.fn().mockResolvedValue(SUBSCRIPTIONS),
      renewals: vi.fn().mockResolvedValue([]),
      spend: vi.fn().mockResolvedValue({ by_currency: [], by_vendor: [], by_category: [], by_owner: [] }),
      listVendors: vi.fn().mockResolvedValue(VENDOR_ROWS),
      listUsers: vi.fn().mockResolvedValue(USERS),
      createSubscription: vi.fn().mockRejectedValue(new ApiError(422, PYDANTIC_422)),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function submitValid() {
    const user = userEvent.setup();
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });
    await user.click(screen.getByRole("button", { name: "New subscription" }));
    await user.type(screen.getByLabelText("Service name"), "Globex CRM");
    await user.click(screen.getByRole("button", { name: "Save" }));
    return user;
  }

  it("renders both field errors as prose instead of the JSON payload", async () => {
    await submitValid();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Cost per cycle: Input should be a valid decimal");
    expect(alert).toHaveTextContent("Currency: String should have at most 3 characters");
  });

  it("leaks neither the framework's payload nor its internal field names", async () => {
    await submitValid();

    const alert = await screen.findByRole("alert");
    const text = alert.textContent ?? "";
    // The blob itself, and every machine-facing part of it.
    expect(text).not.toContain('{"detail"');
    expect(text).not.toContain("decimal_parsing");
    expect(text).not.toContain("string_too_long");
    expect(text).not.toContain("max_length");
    expect(text).not.toContain("ctx");
    // `cost_per_cycle`, the wire name, is what the auditor called out as
    // exposed. "Cost per cycle" is the name on the screen.
    expect(text).not.toContain("cost_per_cycle");
    // `input` echoes the submitted value; rendering it puts whatever was typed
    // into a field -- a key, a token -- onto the screen and into a screenshot.
    expect(text).not.toContain("not-a-number!!");
    expect(text).not.toContain("ZZZZZ");
  });

  // "the raw JSON error rendered inside the modal ... even the product's most
  // prominent error is unannounced" -- `[role=alert]/[role=status]/[aria-live]`
  // was 0 at that exact moment.
  it("announces the refusal in a live region rather than leaving it silent", async () => {
    await submitValid();

    const alert = await screen.findByRole("alert");
    expect(alert).toBeInTheDocument();
    // And the dialog stays open with the entered values intact, so the
    // sentence is next to the fields it is about.
    expect(screen.getByRole("dialog", { name: "New subscription" })).toBeInTheDocument();
    expect(screen.getByLabelText("Service name")).toHaveValue("Globex CRM");
  });
});

/**
 * Auditor B, [HIGH]: "Both are `input[type="text"]` with no `pattern`, no
 * `inputmode`, no `maxlength`." And [MEDIUM] on the same dialog: "None of the
 * five fields is `required`."
 *
 * The submit-time refusals are tested above. These are the attributes the
 * auditor read off the controls, which act before a submit exists: the decimal
 * keypad on a phone, the 3-character cap on the currency box, and the
 * "required" a screen reader announces on arrival at each field.
 */
describe("New subscription: the money fields carry their own constraints", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listSubscriptions: vi.fn().mockResolvedValue(SUBSCRIPTIONS),
      renewals: vi.fn().mockResolvedValue([]),
      spend: vi.fn().mockResolvedValue({ by_currency: [], by_vendor: [], by_category: [], by_owner: [] }),
      listVendors: vi.fn().mockResolvedValue(VENDOR_ROWS),
      listUsers: vi.fn().mockResolvedValue(USERS),
      createSubscription: vi.fn().mockResolvedValue(SUBSCRIPTIONS[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openDialog() {
    const user = userEvent.setup();
    renderWithProviders(<SubscriptionsPage />);
    await screen.findByText("Acme SaaS", { selector: ".cell-title a" });
    await user.click(screen.getByRole("button", { name: "New subscription" }));
    return user;
  }

  it("gives Cost per cycle a decimal keypad rather than a bare text box", async () => {
    await openDialog();

    const cost = screen.getByLabelText("Cost per cycle");
    // NOT type="number": the value is a Decimal string on the wire, and a
    // number input hands back a locale-formatted or exponent-notation value.
    expect(cost).toHaveAttribute("inputmode", "decimal");
    expect(cost).toBeRequired();
  });

  it("caps Currency at the length of an ISO 4217 code", async () => {
    await openDialog();

    const currency = screen.getByLabelText("Currency");
    expect(currency).toHaveAttribute("maxlength", "3");
    expect(currency).toBeRequired();
  });

  it("marks the fields a subscription cannot be recorded without", async () => {
    await openDialog();

    expect(screen.getByLabelText("Service name")).toBeRequired();
    expect(screen.getByLabelText("Seats")).toBeRequired();
    expect(screen.getByLabelText("Seats")).toHaveAttribute("min", "0");
  });
});
