/**
 * Security logs on the list kit (redesign plan, O3 3.10): the event type as
 * chips on the same `?event_type=`, the Client chip narrowing the search, and
 * the event open in its drawer from `?event=`. Over the fixture session, with
 * the real client scope.
 */
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../fixtures/client";
import { recordingNarrowing } from "../../test/narrowing";
import { renderPage } from "../../test/renderPage";
import { SecurityPage } from "./SecurityPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
// The live socket is not under test here.
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => {} }));

describe("Security logs on the list kit", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/security?event_type=logon_failure", <SecurityPage />, "/security");
    const chips = await screen.findByRole("group", { name: "Filter by event type" });
    expect(within(chips).getByRole("button", { name: "Logon failure" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: "Account lockout" }));
    expect(router.state.location.search).toBe("?event_type=account_lockout");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(
      fixtureClient({ ...fixtureRegistry(), listSecurityEvents: () => ({ items: [], total: 0 }) }),
    );
    renderPage("/security?event_type=policy_change", <SecurityPage />, "/security");
    // The page's own sentence names the filter that emptied the list, and the ways out.
    expect(await screen.findByRole("button", { name: "Clear filters" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "No events match these filters" })).toBeInTheDocument();
    expect(screen.getByText(/matches event type “Policy change”/)).toBeInTheDocument();
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // What the page reads on the console's own API: the readiness panel's
    // checks (`listDevices`, `getSecurityLogSettings`), about the
    // organisation's setup, which the top bar decides (D20: the chip narrows
    // the list).
    const narrowing = recordingNarrowing(fixtureClient(), { console: ["listDevices", "getSecurityLogSettings"] });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/security?client=${ANDES}&hours=24`, <SecurityPage />, "/security");
    expect(await screen.findByRole("table", { name: "Security logs" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listSecurityEvents).toHaveBeenCalledWith(
      expect.objectContaining({ username: undefined, event_type: undefined, from_ts: expect.any(String) }),
    );
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("draws no empty state while the Client chip waits for /me (general ruling 10)", async () => {
    // General ruling 10: with the chip set and `/me` unanswered the narrowed
    // reads are disabled (`ready` false), and a disabled query is not
    // "loading" in TanStack v5 -- the page must still draw loading, not empty.
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), me: () => new Promise<never>(() => {}) }));
    renderPage(`/security?client=${ANDES}`, <SecurityPage />, "/security");
    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
    for (const empty of [
      "No events in this window",
      "No events match these filters",
    ]) {
      expect(screen.queryByText(empty)).toBeNull();
    }
  });

  it("opens an event from ?event=", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/security?event=sec-1", <SecurityPage />, "/security");
    const drawer = await screen.findByRole("dialog", { name: "Raw event" });
    expect(await within(drawer).findByText("185.12.4.9")).toBeInTheDocument();
    await userEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    expect(router.state.location.search).toBe("");
  });
});
