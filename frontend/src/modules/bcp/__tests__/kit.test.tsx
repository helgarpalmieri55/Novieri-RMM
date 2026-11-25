/**
 * Continuity tests on the kit (redesign plan, Task 9, 4.5): the plans list
 * (Receta L, with the Client chip), a plan's record head and tab title
 * (Receta F), the test recorder's one guard (Receta FO: a half-recorded
 * exercise asks before it is left), and the printable report (Receta O).
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry, type FixtureMap } from "../../../fixtures/client";
import type { ApiClient, Me } from "../../../lib/api";
import { recordingNarrowing } from "../../../test/narrowing";
import { renderPage } from "../../../test/renderPage";
import { BcpReport } from "../BcpReport";
import { PlanDetail } from "../PlanDetail";
import { PlansPage } from "../PlansPage";
import { TestRecorder } from "../TestRecorder";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** The fixture session, given `bcp.manage` (the fixtures hold read codes only). */
function managing(): Me {
  const me = (fixtureRegistry().me as () => Me)();
  return { ...me, permissions: [...new Set([...me.permissions, "bcp.read", "bcp.manage"])] };
}

function api(over: FixtureMap = {}): ApiClient {
  return fixtureClient({ ...fixtureRegistry(), me: managing, ...over });
}

beforeEach(() => mockUseApi.mockReturnValue(api()));
afterEach(() => vi.unstubAllGlobals());

describe("the plans list on the list kit (4.5)", () => {
  /**
   * What the list reads on the console's own API, un-narrowed: the readiness
   * check above it (`BcpReadiness`: the backup policies a plan relies on, and
   * the roles that may sign a review off). The plans and their status rows
   * are never among them.
   */
  const CONSOLE = ["listBackupPolicies", "listRoles"];

  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = renderPage("/bcp?state=archived", <PlansPage />);
    const chips = await screen.findByRole("group", { name: "Plan state" });
    expect(within(chips).getByRole("button", { name: /Archived/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: /^Active/ }));
    expect(router.state.location.search).toContain("state=active");
    // The review view is one chip that turns on and off.
    const review = screen.getByRole("group", { name: "Review" });
    await userEvent.click(within(review).getByRole("button", { name: /Review overdue/ }));
    expect(router.state.location.search).toContain("review=overdue");
    await userEvent.click(within(review).getByRole("button", { name: /Review overdue/ }));
    expect(router.state.location.search).not.toContain("review=");
  });

  it("keeps its search in the URL, so a reload finds the same rows", async () => {
    const { router } = renderPage("/bcp", <PlansPage />);
    await userEvent.type(await screen.findByRole("textbox", { name: "Search plans" }), "porto");
    expect(router.state.location.search).toBe("?q=porto");
    expect(await screen.findByRole("link", { name: "Porto office WAN failover" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Database failover" })).toBeNull();
    await act(() => router.navigate("/bcp?q=porto"));
    expect(screen.getByRole("textbox", { name: "Search plans" })).toHaveValue("porto");
  });

  it("says why the list is empty", async () => {
    renderPage("/bcp?q=nothing-like-it", <PlansPage />);
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    const narrowing = recordingNarrowing(api(), { console: CONSOLE });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/bcp?client=${ANDES}`, <PlansPage />);
    expect(await screen.findByRole("table", { name: "Continuity tests" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listPlans).toHaveBeenCalledWith();
    // The Review, Last test and Findings columns are the status rows, joined by plan.
    expect(narrowing.client(ANDES).planStatus).toHaveBeenCalledWith();
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("opens New plan from ?new=plan", async () => {
    const { router } = renderPage("/bcp?new=plan", <PlansPage />);
    const dialog = await screen.findByRole("dialog", { name: "New plan" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });

  it("draws loading, not an empty list, while /me has not said whose the chip's client is (general ruling 10)", async () => {
    // `/me` never answers: the narrowed read is held back, and the page must
    // not say "nothing matches" or "nothing yet" about a read it never made.
    mockUseApi.mockReturnValue(api({ me: () => new Promise(() => {}) }));
    renderPage(`/bcp?client=${ANDES}`, <PlansPage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
    expect(screen.queryByText("No BCP/DR plans yet.")).toBeNull();
  });

  it("links the plan tiles with the chip's client, so a tile opens the rows it counted (general ruling 2)", async () => {
    renderPage(`/bcp?client=${ANDES}`, <PlansPage />);
    await screen.findByRole("table", { name: "Continuity tests" });
    const hrefs = [...document.querySelectorAll(".stat-card")].map((tile) => tile.getAttribute("href"));
    expect(hrefs).toContain(`/bcp?state=active&client=${ANDES}`);
    expect(hrefs).toContain(`/bcp?review=overdue&client=${ANDES}`);
  });

  it("asks before Cancel drops a change in New plan, and opens it empty again (general rulings 3 and 4)", async () => {
    const { router } = renderPage("/bcp?new=plan", <PlansPage />, "/bcp");
    let dialog = await screen.findByRole("dialog", { name: "New plan" });
    await userEvent.type(within(dialog).getByLabelText(/^Name/), "DR");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=plan");
    await userEvent.click(screen.getByRole("button", { name: "Leave without saving" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    await act(() => router.navigate("/bcp?new=plan"));
    dialog = await screen.findByRole("dialog", { name: "New plan" });
    expect(within(dialog).getByLabelText(/^Name/)).toHaveValue("");
  }, 15_000);
});

describe("a plan on the record kit (4.5)", () => {
  it("names the record in the browser tab", async () => {
    renderPage("/bcp/plans/plan-1", <PlanDetail />, "/bcp/plans/:planId", { page: "shell.nav.item.continuityTests" });
    await screen.findByRole("heading", { level: 1, name: "Database failover" });
    await waitFor(() => expect(document.title).toBe("Database failover · Continuity tests · Novieri RMM"));
  });

  it("leads back to the plans and says the plan's state in its head", async () => {
    renderPage("/bcp/plans/plan-1", <PlanDetail />, "/bcp/plans/:planId");
    const head = (await screen.findByRole("heading", { level: 1, name: "Database failover" })).closest("section") as HTMLElement;
    expect(within(head).getByRole("link", { name: "All plans" })).toHaveAttribute("href", "/bcp");
    expect(within(head).getByText("Active")).toBeInTheDocument();
    expect(within(head).getByRole("button", { name: "New test" })).toBeInTheDocument();
  });

  it("opens New test from ?new=test", async () => {
    const { router } = renderPage("/bcp/plans/plan-1?new=test", <PlanDetail />, "/bcp/plans/:planId");
    const dialog = await screen.findByRole("dialog", { name: "New DR test" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });

  it("asks before Cancel drops a change in New test (general ruling 3)", async () => {
    const { router } = renderPage("/bcp/plans/plan-1?new=test", <PlanDetail />, "/bcp/plans/:planId");
    const dialog = await screen.findByRole("dialog", { name: "New DR test" });
    await userEvent.type(within(dialog).getByLabelText("Notes"), "x");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=test");
  });
});

describe("the test recorder on the form kit (4.5)", () => {
  function recorder() {
    return renderPage("/bcp/tests/test-1", <TestRecorder />, "/bcp/tests/:testId");
  }

  it("asks before leaving with an outcome half recorded", async () => {
    const { router } = recorder();
    await userEvent.type(await screen.findByLabelText("Notes"), "x");
    await userEvent.click(screen.getByRole("link", { name: "Back to the plan" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/bcp/tests/test-1");
  });

  it("asks before leaving with a step typed and not saved", async () => {
    const { router } = recorder();
    await userEvent.type(await screen.findByLabelText("Step description"), "Fail over the database");
    await userEvent.click(screen.getByRole("link", { name: "View report" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/bcp/tests/test-1");
  });

  it("asks before leaving with a finding typed and not added (fix round 1, M5)", async () => {
    const { router } = recorder();
    await userEvent.type(await screen.findByLabelText("Finding description"), "DNS");
    await userEvent.click(screen.getByRole("link", { name: "Back to the plan" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/bcp/tests/test-1");
  });

  it("still asks before leaving after a recording failed (fix round 1, M5)", async () => {
    const recordTest = vi.fn(() => Promise.reject(new Error("The server is unavailable.")));
    mockUseApi.mockReturnValue(api({ recordTest } as unknown as FixtureMap));
    const { router } = recorder();
    await userEvent.type(await screen.findByLabelText("Notes"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Record test" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Re-record this DR test" })).getByRole("button", { name: "Replace the evidence" }));
    await waitFor(() => expect(recordTest).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Back to the plan" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/bcp/tests/test-1");
  }, 15_000);

  it("leaves without asking once the outcome is recorded, and keeps what was recorded on screen", async () => {
    const recordTest = vi.fn((id: string, body: object) => ({ ...(fixtureRegistry().getTest as (id: string) => object)(id), ...body }));
    mockUseApi.mockReturnValue(api({ recordTest } as unknown as FixtureMap));
    const { router } = recorder();
    await userEvent.type(await screen.findByLabelText("Notes"), "Back in 12 min");
    await userEvent.click(screen.getByRole("button", { name: "Record test" }));
    // test-1 was recorded before, so replacing its evidence asks first.
    const confirm = await screen.findByRole("dialog", { name: "Re-record this DR test" });
    await userEvent.click(within(confirm).getByRole("button", { name: "Replace the evidence" }));
    await waitFor(() => expect(recordTest).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Test recorded.")).toBeInTheDocument();
    expect(screen.getByLabelText("Notes")).toHaveValue("Back in 12 min");
    await userEvent.click(screen.getByRole("link", { name: "Back to the plan" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/bcp/plans/plan-1"));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    // Seven steps through a confirmation and a refetch: under a loaded suite
    // the default five seconds were measured short (the full run, 5340 ms).
  }, 15_000);
});

describe("the printable report (4.5)", () => {
  it("prints the page with the browser's own print", async () => {
    const print = vi.fn();
    vi.stubGlobal("print", print);
    renderPage("/bcp/tests/test-1/report", <BcpReport />, "/bcp/tests/:testId/report");
    await userEvent.click(await screen.findByRole("button", { name: "Print" }));
    expect(print).toHaveBeenCalledTimes(1);
  });
});
