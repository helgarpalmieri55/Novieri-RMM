/**
 * Subscriptions on the kit (redesign plan, wave O5, 5.4): the registry's
 * search, cycle and Client chips in the URL, New subscription from
 * `?new=subscription`, the section nav, a subscription's record head with its
 * tab title and the renewal terms on the one save bar, the spend totals that
 * open their list, and the renewal window in the URL. Over the fixture
 * session, with the real client scope.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../fixtures/client";
import { setSelectedClient } from "../../lib/clientChoice";
import { recordingNarrowing } from "../../test/narrowing";
import { renderPage } from "../../test/renderPage";
import { RenewalCalendar } from "./RenewalCalendar";
import { SpendDashboard } from "./SpendDashboard";
import { SubscriptionDetail } from "./SubscriptionDetail";
import { SubscriptionsPage } from "./SubscriptionsPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** The fixture session with `/me` held: the chip's client is not decided yet. */
function meHeld() {
  return fixtureClient({ ...fixtureRegistry(), me: () => new Promise<never>(() => {}) });
}

describe("no empty state before the narrowed read (coordinator's ruling 10)", () => {
  it.each([
    ["spend", `/subscriptions/spend?client=${ANDES}`, <SpendDashboard key="s" />, "/subscriptions/spend", "No spend data yet."],
    ["the renewal calendar", `/subscriptions/renewals?client=${ANDES}`, <RenewalCalendar key="r" />, "/subscriptions/renewals", "Nothing renews in the next 30 days."],
  ])("%s draws loading while the Client chip's client is still being decided", async (_name, path, page, pattern, empty) => {
    mockUseApi.mockReturnValue(meHeld());
    renderPage(path, page, pattern);
    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
    expect(screen.queryByText(empty)).toBeNull();
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
  });
});

/**
 * The coordinator's ruling 10a (corrected): a tile with no data, pending or
 * failed, draws the kit's absent mark (`shell.viz.na`, "n/a") and no line
 * under it -- never "—", never "0".
 */
function expectNoFigure(tile: HTMLElement) {
  expect(tile.querySelector(".v")?.textContent).toBe("n/a");
  expect(tile.querySelector(".z")).toBeNull();
}

describe("no tile figure before its read (coordinator's ruling 10a)", () => {
  it("draws no count and no claim on the registry's tiles while the chip's client is being decided", async () => {
    mockUseApi.mockReturnValue(meHeld());
    renderPage(`/subscriptions?client=${ANDES}`, <SubscriptionsPage />, "/subscriptions");
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    const tiles = [/Active subscriptions/, /Renewals in 90 days/, /Notice deadlines due/, /No owner/];
    for (const name of tiles) expectNoFigure(screen.getByRole("link", { name }));
    // The run rate has no tile until the spend answers.
    expect(screen.queryByRole("link", { name: /Monthly run rate/ })).toBeNull();
    for (const claim of ["none active", "nothing renews in this window", "no notice period closing", "every subscription has an owner"]) {
      expect(screen.queryByText(claim)).toBeNull();
    }
  });

  it("draws no spend total while the chip's client is being decided", async () => {
    mockUseApi.mockReturnValue(meHeld());
    const { container } = renderPage(`/subscriptions/spend?client=${ANDES}`, <SpendDashboard />, "/subscriptions/spend");
    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
    expect(container.querySelector(".stat-card")).toBeNull();
    expect(screen.queryByText("Never summed across clients or currencies.")).toBeNull();
  });
});

describe("the leave question is drawn on top of its dialog (coordinator's ruling 3a)", () => {
  it("New subscription", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/subscriptions?new=subscription", <SubscriptionsPage />, "/subscriptions");
    const form = await screen.findByRole("dialog", { name: "New subscription" });
    await userEvent.type(within(form).getByLabelText("Service name"), "Globex CRM");
    await userEvent.click(within(form).getByRole("button", { name: "Close" }));
    const question = await screen.findByRole("dialog", { name: "Leave without saving?" });
    // Drawn after the form's dialog, never before it or inside it: both are
    // `.modal`s at one z-index, so the later one paints on top.
    expect(form.contains(question)).toBe(false);
    expect(form.compareDocumentPosition(question) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });
});

describe("the subscriptions registry on the list kit", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/subscriptions?cycle=annual", <SubscriptionsPage />, "/subscriptions");
    const chips = await screen.findByRole("group", { name: "Billing cycle" });
    expect(await within(chips).findByRole("button", { name: /Annual/ })).toHaveAttribute("aria-pressed", "true");
    expect(within(chips).getByRole("button", { name: /Annual/ })).toHaveTextContent("2");
    expect(screen.getByRole("link", { name: "Atlassian Jira Cloud" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Figma Organization" })).toBeNull();
    await userEvent.click(within(chips).getByRole("button", { name: /Quarterly/ }));
    expect(router.state.location.search).toContain("cycle=quarterly");
    expect(await screen.findByRole("link", { name: "Slack Business+" })).toBeInTheDocument();
  });

  it("keeps its search in the URL, so a reload finds the same rows", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/subscriptions", <SubscriptionsPage />, "/subscriptions");
    await userEvent.type(await screen.findByRole("textbox", { name: "Search subscriptions" }), "zoom");
    expect(router.state.location.search).toBe("?q=zoom");
    await act(() => router.navigate("/subscriptions?q=zoom"));
    expect(screen.getByRole("textbox", { name: "Search subscriptions" })).toHaveValue("zoom");
    expect(screen.getByRole("link", { name: "Zoom Workplace" })).toBeInTheDocument();
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/subscriptions?q=nothing-like-it", <SubscriptionsPage />, "/subscriptions");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // What the page reads on the console's own API, and why none is the
    // registry or a tile's count: the people directory names the owners (it
    // is nobody's subscription); the readiness panel reads the unmatched
    // people and the notification rules (`listUnmatched`,
    // `listNotificationRules`: the organisation's setup, decided by the top
    // bar, D20).
    const narrowing = recordingNarrowing(fixtureClient(), {
      console: ["listUsers", "listUnmatched", "listNotificationRules"],
    });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/subscriptions?client=${ANDES}`, <SubscriptionsPage />, "/subscriptions");
    expect(await screen.findByRole("table", { name: "Subscriptions" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listSubscriptions).toHaveBeenCalledWith();
    // The tiles count the same client's subscriptions.
    expect(narrowing.client(ANDES).renewals).toHaveBeenCalledWith(90);
    expect(narrowing.client(ANDES).spend).toHaveBeenCalledWith();
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("draws no empty state while the Client chip's client is still being decided", async () => {
    mockUseApi.mockReturnValue(meHeld());
    renderPage(`/subscriptions?client=${ANDES}`, <SubscriptionsPage />, "/subscriptions");
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("No subscriptions yet.")).toBeNull();
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
  });

  it("opens, from a tile counted for the chip's client, exactly the rows it counted", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage(`/subscriptions?client=${ANDES}`, <SubscriptionsPage />, "/subscriptions");
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /Renewals in 90 days/ })).toHaveAttribute(
        "href",
        `/subscriptions/renewals?client=${ANDES}`,
      ),
    );
    expect(screen.getByRole("link", { name: /Active subscriptions/ })).toHaveAttribute("href", `/subscriptions?client=${ANDES}`);
    expect(screen.getByRole("link", { name: /Notice deadlines due/ })).toHaveAttribute(
      "href",
      `/subscriptions/renewals?client=${ANDES}`,
    );
  });

  it("asks before leaving New subscription with something typed, and opens empty again", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/subscriptions?new=subscription", <SubscriptionsPage />, "/subscriptions");
    let dialog = await screen.findByRole("dialog", { name: "New subscription" });
    await userEvent.type(within(dialog).getByLabelText("Service name"), "Globex CRM");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    const leave = await screen.findByRole("dialog", { name: "Leave without saving?" });
    await userEvent.click(within(leave).getByRole("button", { name: "Leave without saving" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    // The blocked Back has settled once both dialogs are gone.
    await waitFor(() => expect(screen.queryAllByRole("dialog")).toEqual([]));
    await userEvent.click(screen.getByRole("button", { name: "New subscription" }));
    dialog = await screen.findByRole("dialog", { name: "New subscription" });
    expect(within(dialog).getByLabelText("Service name")).toHaveValue("");
  });

  it("links its three pages in one section nav", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/subscriptions", <SubscriptionsPage />, "/subscriptions");
    const nav = await screen.findByRole("navigation", { name: "Subscriptions" });
    expect(within(nav).getByRole("link", { name: "Subscriptions" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Spend" })).toHaveAttribute("href", "/subscriptions/spend");
    expect(within(nav).getByRole("link", { name: "Renewals" })).toHaveAttribute("href", "/subscriptions/renewals");
  });
});

describe("a subscription on the record kit", () => {
  it("names the record in the browser tab", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/subscriptions/sub-1", <SubscriptionDetail />, "/subscriptions/:subscriptionId", {
      page: "shell.nav.item.subscriptions",
    });
    await screen.findByRole("heading", { level: 1, name: "Figma Organization" });
    await waitFor(() => expect(document.title).toBe("Figma Organization · Subscriptions · Novieri RMM"));
    expect(screen.getByRole("link", { name: "All subscriptions" })).toHaveAttribute("href", "/subscriptions");
  });

  it("shows one save bar only while something changed, and Discard puts it back", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/subscriptions/sub-1", <SubscriptionDetail />, "/subscriptions/:subscriptionId");
    const notice = await screen.findByLabelText("Notice period (days)");
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    await userEvent.type(notice, "5");
    expect(screen.getByRole("region", { name: "Unsaved changes" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    expect(screen.getByLabelText("Notice period (days)")).toHaveValue(30);
  });

  it("asks before leaving with unsaved changes", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/subscriptions/sub-1", <SubscriptionDetail />, "/subscriptions/:subscriptionId");
    await userEvent.type(await screen.findByLabelText("Notice period (days)"), "5");
    await userEvent.click(screen.getByRole("link", { name: "All subscriptions" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/subscriptions/sub-1");
  });

  it("sends nothing when the terms are submitted untouched (coordinator's ruling 13)", async () => {
    const updateSubscription = vi.fn();
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), updateSubscription } as never));
    renderPage("/subscriptions/sub-1", <SubscriptionDetail />, "/subscriptions/:subscriptionId");
    // Enter in a field submits the form; with nothing changed there is nothing to write.
    await userEvent.type(await screen.findByLabelText("Notice period (days)"), "{Enter}");
    expect(updateSubscription).not.toHaveBeenCalled();
  });
});

afterEach(() => setSelectedClient(null));

describe("an edit never carries to another client or record (coordinator's ruling 12)", () => {
  it("New subscription drops what was typed when the top bar's client changes", async () => {
    const createSubscription = vi.fn();
    setSelectedClient(ANDES);
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), createSubscription } as never));
    renderPage("/subscriptions?new=subscription", <SubscriptionsPage />, "/subscriptions");
    let form = await screen.findByRole("dialog", { name: "New subscription" });
    await userEvent.type(within(form).getByLabelText("Service name"), "Globex CRM");
    act(() => setSelectedClient("org-toeshee"));
    form = await screen.findByRole("dialog", { name: "New subscription" });
    expect(within(form).getByLabelText("Service name")).toHaveValue("");
    await userEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(createSubscription).not.toHaveBeenCalled();
  });

  it("a renewal-terms edit left on one subscription does not reach the next", async () => {
    const updateSubscription = vi.fn();
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), updateSubscription } as never));
    // Slack Business+ first, so it is already read when the edit leaves for it.
    const { router } = renderPage("/subscriptions/sub-3", <SubscriptionDetail />, "/subscriptions/:subscriptionId");
    await screen.findByRole("heading", { level: 1, name: "Slack Business+" });
    await act(() => router.navigate("/subscriptions/sub-1"));
    await screen.findByRole("heading", { level: 1, name: "Figma Organization" });
    const notice = await screen.findByLabelText("Notice period (days)");
    await userEvent.clear(notice);
    await userEvent.type(notice, "45");
    // Blocked by the leave question, so the navigation is not awaited here.
    void router.navigate("/subscriptions/sub-3");
    const leave = await screen.findByRole("dialog", { name: "Leave without saving?" });
    await userEvent.click(within(leave).getByRole("button", { name: "Leave without saving" }));
    // Slack Business+ with its own 60 days, and no change to save.
    expect(await screen.findByRole("heading", { level: 1, name: "Slack Business+" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Notice period (days)")).toHaveValue(60));
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    await userEvent.type(screen.getByLabelText("Notice period (days)"), "{Enter}");
    expect(updateSubscription).not.toHaveBeenCalled();
  });

  it("an assignee typed on one subscription does not reach the next", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/subscriptions/sub-1", <SubscriptionDetail />, "/subscriptions/:subscriptionId");
    await userEvent.type(await screen.findByLabelText("Assignee name"), "Ana Duarte");
    await act(() => router.navigate("/subscriptions/sub-3"));
    expect(await screen.findByRole("heading", { level: 1, name: "Slack Business+" })).toBeInTheDocument();
    expect(screen.getByLabelText("Assignee name")).toHaveValue("");
  });
});

describe("subscription spend", () => {
  it("leads each total to the list that explains it", async () => {
    mockUseApi.mockReturnValue(
      fixtureClient({
        ...fixtureRegistry(),
        spend: () => ({
          by_currency: [
            { group_key: null, group_type: null, group_id: null, currency: "USD", client_id: ANDES, monthly_total: "100.00", annual_total: "1200.00" },
          ],
          by_vendor: [],
          by_category: [],
          by_owner: [],
        }),
      }),
    );
    renderPage("/subscriptions/spend", <SpendDashboard />, "/subscriptions/spend");
    const tile = await screen.findByRole("link", { name: /Andes Logistics: monthly spend in USD/ });
    expect(tile).toHaveAttribute("href", `/subscriptions?client=${ANDES}`);
    // In its own currency, never added to another.
    expect(within(tile).getByText("100.00 USD")).toBeInTheDocument();
    expect(within(tile).getByText("1200.00 USD a year")).toBeInTheDocument();
  });

  it("reads the spend only through the narrowed API of the Client chip's client (decision D6)", async () => {
    const narrowing = recordingNarrowing(fixtureClient());
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/subscriptions/spend?client=${ANDES}`, <SpendDashboard />, "/subscriptions/spend");
    expect(await screen.findByRole("heading", { level: 2, name: "By vendor" })).toBeInTheDocument();
    await waitFor(() => expect(narrowing.client(ANDES).spend).toHaveBeenCalledWith());
    expect(narrowing.client(ANDES).idleSeats).toHaveBeenCalledWith();
    expect(narrowing.unnarrowed).toEqual([]);
  });
});

describe("the subscription renewal calendar", () => {
  it("keeps the window in the URL, and a reload asks for the same one", async () => {
    const renewals = vi.fn(() => []);
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), renewals }));
    const { router } = renderPage("/subscriptions/renewals?within=60", <RenewalCalendar />, "/subscriptions/renewals");
    const chips = await screen.findByRole("group", { name: "Window" });
    expect(within(chips).getByRole("button", { name: "Next 60 days" })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(renewals).toHaveBeenCalledWith(60));
    await userEvent.click(within(chips).getByRole("button", { name: "Next 90 days" }));
    expect(router.state.location.search).toBe("?within=90");
    await waitFor(() => expect(renewals).toHaveBeenCalledWith(90));
    // The default window writes nothing.
    await userEvent.click(within(chips).getByRole("button", { name: "Next 30 days" }));
    expect(router.state.location.search).toBe("");
  });

  it("says why the calendar is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), renewals: () => [] }));
    renderPage("/subscriptions/renewals?within=60", <RenewalCalendar />, "/subscriptions/renewals");
    expect(await screen.findByText("Nothing renews in the next 60 days.")).toBeInTheDocument();
  });

  it("reads the renewals only through the narrowed API of the Client chip's client (decision D6)", async () => {
    const narrowing = recordingNarrowing(fixtureClient());
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/subscriptions/renewals?client=${ANDES}`, <RenewalCalendar />, "/subscriptions/renewals");
    await waitFor(() => expect(narrowing.client(ANDES).renewals).toHaveBeenCalledWith(30));
    expect(narrowing.unnarrowed).toEqual([]);
  });
});
