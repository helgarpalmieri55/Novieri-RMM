/**
 * Remote sessions on the list kit (redesign plan, O3 3.6): the strip that
 * filtered the list is chips on the same `?state=`, Start remote session opens
 * from `?new=session`, and under the Client chip the sessions are read for
 * that client alone. Over the fixture session, with the real client scope.
 */
import { screen, within } from "@testing-library/react";
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
    renderPage(`/remote?client=${ANDES}`, <SessionsPage />, "/remote");
    expect(await screen.findByRole("table", { name: "Remote sessions" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listRemoteSessions).toHaveBeenCalledWith();
    // The machines that name the rows and the online tile are the same client's.
    expect(narrowing.client(ANDES).listDevices).toHaveBeenCalledWith();
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("opens Start remote session from ?new=session", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/remote?new=session", <SessionsPage />, "/remote");
    expect(await screen.findByRole("dialog", { name: "Start remote session" })).toBeInTheDocument();
  });
});
