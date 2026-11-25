/**
 * Job history on the list kit (redesign plan, O3 3.7): the status chips and
 * the search in the URL, an empty list that says why, the open job in its
 * drawer from `?job=`, and the queue read under the Client chip's client.
 * Over the fixture session, with the real client scope.
 */
import { screen, within } from "@testing-library/react";
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
    renderPage(`/jobs?client=${ANDES}`, <JobsPage />, "/jobs");
    expect(await screen.findByRole("table", { name: "Job queue" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listJobs).toHaveBeenCalledWith();
    expect(narrowing.client(ANDES).listDevices).toHaveBeenCalledWith();
    expect(narrowing.unnarrowed).toEqual([]);
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
