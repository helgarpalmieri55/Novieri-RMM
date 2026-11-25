/**
 * The ticket queue on the list kit (redesign plan, Task 7, 2.2; Receta L):
 * its filters in the URL, an empty list that says its cause, the scope that
 * is not an ordinary filter, and the Client chip, which narrows the read and
 * only the read (decision D6). On the fixture session, with the real client
 * scope, so the chip is drawn and `/me` decides whose clients are whose.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../../fixtures/client";
import type { ApiClient } from "../../../lib/api";
import { recordingNarrowing } from "../../../test/narrowing";
import { renderPage } from "../../../test/renderPage";
import { MyWorkPage } from "../MyWorkPage";
import { TicketListPage } from "../TicketListPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

beforeEach(() => mockUseApi.mockReturnValue(fixtureClient()));

/** The fixture session with no ticket at all. */
function emptyQueue(): ApiClient {
  return fixtureClient({ ...fixtureRegistry(), listTickets: () => ({ items: [], total: 0 }) });
}

describe("the ticket queue on the list kit", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = renderPage("/service-desk?scope=all", <TicketListPage />);
    const show = await screen.findByRole("group", { name: "Show" });
    expect(within(show).getByRole("button", { name: /^All$/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(show).getByRole("button", { name: /Unassigned/ }));
    expect(router.state.location.search).toContain("scope=unassigned");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Status" }), "open");
    expect(router.state.location.search).toContain("status=open");
    // The other filters keep their keys: a chip with its name on it.
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Origin" }), "email");
    expect(router.state.location.search).toContain("origin=email");
    await act(() => router.navigate("/service-desk?status=pending_approval&origin=portal"));
    expect(screen.getByRole("combobox", { name: "Origin" })).toHaveValue("portal");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(emptyQueue());
    const { router } = renderPage("/service-desk?status=resolved", <TicketListPage />);
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(router.state.location.search).toBe("");
    expect(await screen.findByText("No open tickets.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });

  it("clears the filters and keeps the sort (fix round 1)", async () => {
    mockUseApi.mockReturnValue(emptyQueue());
    const { router } = renderPage("/service-desk?status=resolved&sort=newest", <TicketListPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Clear filters" }));
    expect(router.state.location.search).toBe("?sort=newest");
  });

  it("does not call a sort a filter", async () => {
    mockUseApi.mockReturnValue(emptyQueue());
    renderPage("/service-desk?sort=newest", <TicketListPage />);
    expect(await screen.findByText("No open tickets.")).toBeInTheDocument();
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
  });

  // The scope is not an ordinary filter (pre-flight review, W-M4): without
  // `?scope=` the queue is the open tickets, so "All open" writes nothing and
  // "All" writes `scope=all`; "My tickets" and "Unassigned" drop a picked
  // assignee, as `setScope` always has.
  it("picking Mine clears the assignee, and Open is the chip that writes nothing", async () => {
    const { router } = renderPage("/service-desk?scope=all&assignee=usr-tk", <TicketListPage />);
    const show = await screen.findByRole("group", { name: "Show" });
    expect(within(show).getByRole("button", { name: /^All$/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(show).getByRole("button", { name: /My tickets/ }));
    expect(router.state.location.search).toBe("?scope=mine");
    await userEvent.click(within(show).getByRole("button", { name: /All open/ }));
    expect(router.state.location.search).toBe("");
    expect(within(show).getByRole("button", { name: /All open/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(show).getByRole("button", { name: /^All$/ }));
    expect(router.state.location.search).toBe("?scope=all");
  });

  it("opens on the open tickets with All open marked, writing nothing", async () => {
    const { router } = renderPage("/service-desk", <TicketListPage />);
    const show = await screen.findByRole("group", { name: "Show" });
    expect(within(show).getByRole("button", { name: /All open/ })).toHaveAttribute("aria-pressed", "true");
    expect(router.state.location.search).toBe("");
  });

  it("says how many it shows, of the server's total", async () => {
    renderPage("/service-desk", <TicketListPage />);
    expect(await screen.findByRole("table", { name: "Tickets" })).toBeInTheDocument();
    expect(screen.getByText(/^Showing \d+ of \d+$/)).toBeInTheDocument();
  });

  // `/service-desk?client=<id>` is the link the client's 360 page draws.
  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // What the queue reads on the console's own API, legitimately: the queue
    // names and the user directory are the organisation's reference data,
    // read to name a row's queue and people, never its rows; the readiness
    // checklist (SLA policies, categories, mailboxes, health settings and the
    // rules) is the organisation's own set-up, shown to whoever administers
    // it; and the New ticket form's device picker (`listDevices`), mounted
    // with the page, reads with the top bar's choice because a write goes
    // where the top bar says, never where the chip does. The list itself
    // must go through `forClient(ANDES)`.
    const narrowing = recordingNarrowing(fixtureClient(), {
      console: [
        "listTicketQueues",
        "listUsers",
        "listDevices",
        "listSlaPolicies",
        "listTicketCategories",
        "listEmailMailboxes",
        "getHealthSettings",
        "listNotificationRules",
      ],
    });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/service-desk?client=${ANDES}`, <TicketListPage />);
    expect(await screen.findByRole("table", { name: "Tickets" })).toBeInTheDocument();
    await waitFor(() =>
      expect(narrowing.client(ANDES).listTickets).toHaveBeenCalledWith({
        type: undefined,
        status: undefined,
        queue_id: undefined,
        priority: undefined,
        q: undefined,
        assigned_to: undefined,
        scope: "open",
        sort: "due",
        limit: 200,
      }),
    );
    expect(screen.getByRole("combobox", { name: "Client" })).toHaveValue(ANDES);
    // A tile counted on the narrowed read opens that client's rows (general ruling 2).
    const unassigned = [...document.querySelectorAll<HTMLAnchorElement>(".stat-card")].find(
      (card) => card.querySelector(".stat-label")?.textContent === "Unassigned",
    );
    expect(unassigned).toHaveAttribute("href", `/service-desk?scope=unassigned&client=${ANDES}`);
    expect(narrowing.unnarrowed).toEqual([]);
  });

  // General ruling 10: with the chip set and `/me` not answered, the read is
  // not enabled yet; the list says it is loading, never that nothing matches.
  it("draws no empty state while /me has not answered with the Client chip set", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), me: () => new Promise(() => {}) }));
    renderPage(`/service-desk?client=${ANDES}`, <TicketListPage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
    expect(screen.queryByText("No open tickets.")).toBeNull();
    // The tiles are not counted yet either: "n/a", never an invented 0.
    expect([...document.querySelectorAll(".stat-card .stat-value")].map((v) => v.textContent)).not.toContain("0");
  });

  // General ruling 3: the New ticket form lives in `?new=ticket`; Back asks
  // before a half-written ticket is dropped, Cancel drops it without asking.
  it("asks before Back drops a half-written ticket, and Cancel does not ask", async () => {
    const { router } = renderPage("/service-desk", <TicketListPage />);
    await userEvent.click(await screen.findByRole("button", { name: "New ticket" }));
    const dialog = await screen.findByRole("dialog", { name: "New ticket" });
    await userEvent.type(within(dialog).getByLabelText("Subject"), "Printer jams");
    // React reports the held Back's promise as an un-awaited suspension
    // (shell/formKit.test.tsx, "LEAVING IS A CLICK ON A LINK"); asserted, not tolerated.
    const heldBack = vi.spyOn(console, "error").mockImplementation(() => {});
    await act(() => router.navigate(-1));
    const leave = await screen.findByRole("dialog", { name: "Leave without saving?" });
    expect(heldBack.mock.calls.every(([message]) => String(message).includes("suspended inside an `act` scope"))).toBe(true);
    heldBack.mockRestore();
    await userEvent.click(within(leave).getByRole("button", { name: "Cancel" }));
    await userEvent.click(within(screen.getByRole("dialog", { name: "New ticket" })).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New ticket" })).toBeNull());
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });
});

describe("My work on the list kit", () => {
  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(emptyQueue());
    renderPage("/service-desk/my-work", <MyWorkPage />);
    expect(await screen.findByText("No open tickets are assigned to you.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See the ticket queue" })).toHaveAttribute("href", "/service-desk");
    expect(screen.getByText("Showing 0 of 0")).toBeInTheDocument();
  });

  it("puts the service desk's section navigation at the top, with My work current", async () => {
    renderPage("/service-desk/my-work", <MyWorkPage />);
    const nav = await screen.findByRole("navigation", { name: "Service desk" });
    expect(within(nav).getByRole("link", { name: "My work" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Tickets" })).not.toHaveAttribute("aria-current");
    // At the top: before the tiles and the list.
    const tiles = document.querySelector(".stats-grid") as HTMLElement;
    expect(nav.compareDocumentPosition(tiles) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  // FIX ROUND 2, M3: the New ticket guard counts the requester too.
  it("asks before Back drops a New ticket whose only change is its requester", async () => {
    const { router } = renderPage("/service-desk", <TicketListPage />);
    await userEvent.click(await screen.findByRole("button", { name: "New ticket" }));
    const dialog = await screen.findByRole("dialog", { name: "New ticket" });
    // The requester picker's own list (the queue's filters hold <option>s of the same people).
    await userEvent.click(within(dialog).getByLabelText("Requester"));
    const list = await screen.findByRole("listbox", { name: "Requester" });
    await userEvent.click(await within(list).findByRole("option", { name: /Adaeze Okonkwo/ }));
    const heldBack = vi.spyOn(console, "error").mockImplementation(() => {});
    await act(() => router.navigate(-1));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(heldBack.mock.calls.every(([message]) => String(message).includes("suspended inside an `act` scope"))).toBe(true);
    heldBack.mockRestore();
  });
});
