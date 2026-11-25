/**
 * Remote sessions on the list kit (redesign plan, O3 3.6): the strip that
 * filtered the list is chips on the same `?state=`, Start remote session opens
 * from `?new=session`, and under the Client chip the sessions are read for
 * that client alone. Over the fixture session, with the real client scope.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../fixtures/client";
import { recordingNarrowing } from "../../test/narrowing";
import { renderPage } from "../../test/renderPage";
import { SessionsPage } from "./SessionsPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

describe("Remote sessions on the list kit", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/remote?state=active", <SessionsPage />, "/remote");
    const chips = await screen.findByRole("group", { name: "Sessions" });
    expect(within(chips).getByRole("button", { name: "Active sessions" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: "With recording" }));
    expect(router.state.location.search).toBe("?state=recorded");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(
      fixtureClient({ ...fixtureRegistry(), listRemoteSessions: () => ({ items: [], total: 0 }) }),
    );
    renderPage("/remote?state=recorded", <SessionsPage />, "/remote");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // What the page reads on the console's own API: `listDevices`, for the
    // readiness panel (is a computer running the agent at all: the
    // organisation's setup, which the top bar decides, D20) and for Start
    // remote session's machine picker (the start is a write, and keeps
    // `useApi()`). The narrowed `listDevices` below is the list's own, so the
    // assertion on it, not this allowance, is what holds the rows' names.
    const narrowing = recordingNarrowing(fixtureClient(), { console: ["listDevices"] });
    mockUseApi.mockReturnValue(narrowing.api);
    const { queryClient } = renderPage(`/remote?client=${ANDES}`, <SessionsPage />, "/remote");
    expect(await screen.findByRole("table", { name: "Remote sessions" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listRemoteSessions).toHaveBeenCalledWith();
    // Kept under an unambiguous key, `{ client }` as Job history's: a bare id
    // after "devices" is a device's own record (`["devices", deviceId]`).
    expect(queryClient.getQueryData(["remote-sessions", { client: ANDES }])).toBeDefined();
    await waitFor(() => expect(queryClient.getQueryData(["devices", { client: ANDES }])).toBeDefined());
    // The machines that name the rows and the online tile are the same client's.
    expect(narrowing.client(ANDES).listDevices).toHaveBeenCalledWith();
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("draws no empty state while the Client chip waits for /me (general ruling 10)", async () => {
    // General ruling 10: with the chip set and `/me` unanswered the narrowed
    // reads are disabled (`ready` false), and a disabled query is not
    // "loading" in TanStack v5 -- the page must still draw loading, not empty.
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), me: () => new Promise<never>(() => {}) }));
    renderPage(`/remote?client=${ANDES}`, <SessionsPage />, "/remote");
    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
    for (const empty of [
      "No remote sessions yet.",
      "Nothing matches these filters.",
    ]) {
      expect(screen.queryByText(empty)).toBeNull();
    }
  });

  it("opens each tile's rows under the same Client chip (general ruling 2)", async () => {
    // The tiles count the chip's client (its sessions, its machines), so each
    // opens that client's rows, not every client's.
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage(`/remote?client=${ANDES}`, <SessionsPage />, "/remote");
    await screen.findByRole("table", { name: "Remote sessions" });
    // The page's own links (the section nav) name pages, not counted rows.
    const hrefs = screen
      .getAllByRole("link")
      .filter((link) => link.closest("nav") === null)
      .map((link) => link.getAttribute("href") ?? "");
    expect(hrefs).toContain(`/devices?state=online&client=${ANDES}`);
    expect(hrefs).toContain(`/remote?client=${ANDES}`);
    expect(hrefs).toContain(`/remote?state=active&client=${ANDES}`);
    expect(hrefs).toContain(`/remote?state=recorded&client=${ANDES}`);
    const lists = hrefs.filter((href) => href === "/remote" || href.startsWith("/remote?"));
    expect(lists.filter((href) => !href.includes(`client=${ANDES}`))).toEqual([]);
  });

  it("asks before Start remote session is left with a change, and opens it empty again (general rulings 3, 4)", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/remote?new=session", <SessionsPage />, "/remote");
    const title = "Start remote session";
    const dialog = await screen.findByRole("dialog", { name: title });
    await userEvent.type(within(dialog).getByLabelText("Reason"), "VPN");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    const ask = await screen.findByRole("dialog", { name: "Leave without saving?" });
    expect(router.state.location.search).toBe("?new=session");
    await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    expect(within(screen.getByRole("dialog", { name: title })).getByLabelText("Reason")).toHaveValue("VPN");

    await userEvent.click(within(screen.getByRole("dialog", { name: title })).getByRole("button", { name: "Cancel" }));
    await userEvent.click(
      within(await screen.findByRole("dialog", { name: "Leave without saving?" })).getByRole("button", {
        name: "Leave without saving",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog", { name: title })).toBeNull());
    await userEvent.click(screen.getByRole("button", { name: "Start session" }));
    expect(within(await screen.findByRole("dialog", { name: title })).getByLabelText("Reason")).toHaveValue("");
    // Two dialogs, a week grid and typing: slow on a busy machine, not stuck.
  }, 20_000);

  it("opens Start remote session from ?new=session", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/remote?new=session", <SessionsPage />, "/remote");
    expect(await screen.findByRole("dialog", { name: "Start remote session" })).toBeInTheDocument();
  });
});
