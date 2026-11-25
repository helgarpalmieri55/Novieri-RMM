/**
 * Job history on the list kit (redesign plan, O3 3.7): the status chips and
 * the search in the URL, an empty list that says why, the open job in its
 * drawer from `?job=`, and the queue read under the Client chip's client.
 * Over the fixture session, with the real client scope.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../fixtures/client";
import { recordingNarrowing } from "../../test/narrowing";
import { renderPage } from "../../test/renderPage";
import { JobsPage } from "./JobsPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
// The live socket is not under test here.
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => {} }));

describe("Job history on the list kit", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/jobs?status=failed", <JobsPage />, "/jobs");
    const chips = await screen.findByRole("group", { name: "Filter by status" });
    expect(within(chips).getByRole("button", { name: "Failed" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: "Queued" }));
    expect(router.state.location.search).toBe("?status=queued");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listJobs: () => ({ items: [], total: 0 }) }));
    renderPage("/jobs?status=failed", <JobsPage />, "/jobs");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // What the page reads on the console's own API, and why none is the
    // queue: the readiness panel asks whether a computer runs the agent at all
    // (`listDevices`, the organisation's setup, which the top bar decides,
    // D20). The machines that name the rows are read narrowed, asserted below.
    const narrowing = recordingNarrowing(fixtureClient(), { console: ["listDevices"] });
    mockUseApi.mockReturnValue(narrowing.api);
    const { queryClient } = renderPage(`/jobs?client=${ANDES}`, <JobsPage />, "/jobs");
    expect(await screen.findByRole("table", { name: "Job queue" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listJobs).toHaveBeenCalledWith();
    expect(narrowing.client(ANDES).listDevices).toHaveBeenCalledWith();
    // Kept under unambiguous keys: a bare id after "devices" is a device's own
    // record (`["devices", deviceId]`).
    expect(queryClient.getQueryData(["jobs", { client: ANDES }])).toBeDefined();
    await waitFor(() => expect(queryClient.getQueryData(["devices", { client: ANDES }])).toBeDefined());
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("draws no empty state while the Client chip waits for /me (general ruling 10)", async () => {
    // General ruling 10: with the chip set and `/me` unanswered the narrowed
    // reads are disabled (`ready` false), and a disabled query is not
    // "loading" in TanStack v5 -- the page must still draw loading, not empty.
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), me: () => new Promise<never>(() => {}) }));
    renderPage(`/jobs?client=${ANDES}`, <JobsPage />, "/jobs");
    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
    for (const empty of [
      "No jobs dispatched yet.",
      "Nothing matches these filters.",
    ]) {
      expect(screen.queryByText(empty)).toBeNull();
    }
  });

  it("opens each tile's rows under the same Client chip (general ruling 2)", async () => {
    // The tiles count the chip's client, so each opens that client's rows.
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage(`/jobs?client=${ANDES}`, <JobsPage />, "/jobs");
    await screen.findByRole("table", { name: "Job queue" });
    // The page's own links (the section nav) name pages, not counted rows.
    const hrefs = screen
      .getAllByRole("link")
      .filter((link) => link.closest("nav") === null)
      .map((link) => link.getAttribute("href") ?? "");
    expect(hrefs).toContain(`/jobs?client=${ANDES}`);
    expect(hrefs).toContain(`/jobs?status=running&client=${ANDES}`);
    expect(hrefs).toContain(`/jobs?status=failed&client=${ANDES}`);
    expect(hrefs).toContain(`/jobs?status=stuck&client=${ANDES}`);
    const lists = hrefs.filter((href) => href === "/jobs" || href.startsWith("/jobs?"));
    expect(lists.filter((href) => !href.includes(`client=${ANDES}`))).toEqual([]);
  });

  it("draws no number and no claim in its tiles while the Client chip waits for /me (clarification 10a)", async () => {
    // A tile counts the narrowed read; before it answers there is nothing to
    // count, so no "0" and no "nothing has ever…" (Health's tiles: "—", no sub).
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), me: () => new Promise<never>(() => {}) }));
    const { container } = renderPage(`/jobs?client=${ANDES}`, <JobsPage />, "/jobs");
    await screen.findAllByText("Loading…");
    const tiles = Array.from(container.querySelectorAll(".tile"));
    expect(tiles.length).toBe(4);
    for (const tile of tiles) {
      expect(tile.querySelector(".v")?.textContent).toBe("—");
      expect(tile.querySelector(".z")).toBeNull();
    }
  });

  it("opens a job from ?job= in its drawer", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/jobs?status=failed&job=job-1", <JobsPage />, "/jobs");
    const drawer = await screen.findByRole("dialog", { name: "Job detail" });
    expect(await within(drawer).findByText("job-1")).toBeInTheDocument();
    await userEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    // Closing keeps the filter the drawer opened over.
    expect(router.state.location.search).toBe("?status=failed");
    expect(screen.queryByRole("dialog", { name: "Job detail" })).toBeNull();
  });

  it("still asks before re-running a job", async () => {
    const retryJob = vi.fn();
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), retryJob }));
    renderPage("/jobs?job=job-1", <JobsPage />, "/jobs");
    const drawer = await screen.findByRole("dialog", { name: "Job detail" });
    await userEvent.click(await within(drawer).findByRole("button", { name: "Run this job again" }));
    expect(await screen.findByRole("dialog", { name: "Run this job again" })).toBeInTheDocument();
    expect(retryJob).not.toHaveBeenCalled();
  });
});
