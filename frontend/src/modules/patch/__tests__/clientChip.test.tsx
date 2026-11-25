/**
 * THE CLIENT CHIP ON THE PATCH PAGE (redesign plan, O3 3.2; decisions D6,
 * D20). Its own file because `patch.test.tsx` mocks the client scope with a
 * client chosen in the top bar, where the chip is not drawn and narrows
 * nothing; here the scope is the real one, over the fixture session's `/me`.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../../fixtures/client";
import { recordingNarrowing } from "../../../test/narrowing";
import { renderPage } from "../../../test/renderPage";
import { FleetPatchPage } from "../FleetPatchPage";
import type { PatchUpdateFilters, PatchUpdateListResponse } from "../types";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/**
 * What the page reads on the console's own API, un-narrowed, and why each is
 * not the list: the rings (`listPatchRings`) are the blast radius of an
 * APPROVAL, and the changes it can be linked to (`listChanges`) are that
 * approval's too -- a write that keeps `useApi()` and names its own client;
 * the people who approved (`listUsers`) are the organisation's, read to put a
 * name on an id; the zone (`getCurrentOrg`) is the organisation's; the
 * readiness panel (`listChangeGates`) asks about the organisation's setup,
 * which the top bar decides (D20: the chip narrows the list).
 */
const CONSOLE = ["listPatchRings", "listChanges", "listUsers", "getCurrentOrg", "listChangeGates"];

describe("FleetPatchPage under the Client chip", () => {
  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    const narrowing = recordingNarrowing(fixtureClient(), { console: CONSOLE });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/patch?client=${ANDES}`, <FleetPatchPage />, "/patch");
    expect(await screen.findByRole("table", { name: "Outstanding Windows updates" })).toBeInTheDocument();
    await waitFor(() => expect(narrowing.client(ANDES).listPatchDeviceRollup).toHaveBeenCalled());
    expect(narrowing.client(ANDES).listPatchUpdates).toHaveBeenCalledWith({
      limit: 25,
      kind: "os",
      severity: [],
      os_family: "windows",
      q: undefined,
      offset: undefined,
    });
    // The strip, the machines, the approvals and the compliance read the same client.
    expect(narrowing.client(ANDES).listPatchUpdates).toHaveBeenCalledWith({ os_family: "linux", limit: 1 });
    expect(narrowing.client(ANDES).listPatchDeviceRollup).toHaveBeenCalled();
    expect(narrowing.client(ANDES).listPatchApprovals).toHaveBeenCalled();
    expect(narrowing.client(ANDES).getPatchFleetReport).toHaveBeenCalled();
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("keeps its search, severity and platform in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/patch?os=linux", <FleetPatchPage />, "/patch");
    const severity = await screen.findByRole("group", { name: "Filter Linux updates by severity" });
    // The Linux default is in force, and pressing a chip writes the list format.
    expect(within(severity).getByRole("button", { name: /^Critical/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(severity).getByRole("button", { name: /^Moderate/ }));
    expect(router.state.location.search).toContain("severity=critical%2Cimportant%2Cmoderate");
    await userEvent.click(screen.getByRole("tab", { name: /macOS/ }));
    expect(router.state.location.search).toBe("?os=macos");
  });

  it("draws no empty state in any list while the Client chip waits for /me (general ruling 10)", async () => {
    // With the chip set and `/me` unanswered every narrowed read is disabled
    // (`ready` false), and a disabled query is not "loading" in TanStack v5:
    // each list must still draw loading, not "nothing outstanding".
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), me: () => new Promise<never>(() => {}) }));
    renderPage(`/patch?client=${ANDES}`, <FleetPatchPage />, "/patch");
    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
    for (const empty of [
      "No scan has completed, so no device has reported an outstanding update yet.",
      "Nothing is outstanding on any reporting device.",
      "No scan has completed, so no device's exposure is known yet.",
      "Every reporting device is up to date.",
      "No machine is waiting to restart after updates.",
      "No update is approved into a ring yet. Approve one from the Updates panel.",
    ]) {
      expect(screen.queryByText(empty)).toBeNull();
    }
    expect(screen.queryByText(/^No update matches these filters/)).toBeNull();
    // A patch list with no answer draws no table at all: an empty table is
    // this page's "nothing outstanding" (its sentences need an answer first).
    expect(screen.queryAllByRole("table")).toEqual([]);
  });

  it("says why the list is empty", async () => {
    // Nothing rated low: the filter is the cause, and the sentence says what it hides.
    const registry = fixtureRegistry();
    const real = registry.listPatchUpdates as (params?: PatchUpdateFilters) => unknown;
    mockUseApi.mockReturnValue(
      fixtureClient({
        ...registry,
        listPatchUpdates: async (params?: PatchUpdateFilters) => {
          const page = (await real(params)) as PatchUpdateListResponse;
          return params?.severity?.includes("low") ? { ...page, items: [], total: 0 } : page;
        },
      }),
    );
    renderPage("/patch?severity=low", <FleetPatchPage />, "/patch");
    expect(
      await screen.findByText(/^No update matches these filters\. \d+ are outstanding across the fleet\.$/),
    ).toBeInTheDocument();
    const severity = screen.getByRole("group", { name: "Filter Windows updates by severity" });
    expect(within(severity).getByRole("button", { name: /^Low/ })).toHaveAttribute("aria-pressed", "true");
  });
});
