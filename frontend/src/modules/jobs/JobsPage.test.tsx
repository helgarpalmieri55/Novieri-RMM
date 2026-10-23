/**
 * The jobs console: an issue time that lied, a queue that hid an incident, and
 * a detail panel that lost the job's identity.
 *
 * CYCLE 5. `Issued` was `new Date(job.issued_at).toLocaleString()` — the
 * BROWSER's zone, never the operator's profile zone, and the browser's format.
 * On a signed-job console the issue time is evidence: "who told that endpoint
 * to do that, and when" is the question this table exists to answer, and an
 * answer in an unstated zone is not one.
 *
 * CYCLE 4 / SWALLOWED FAILURE. The detail panel rendered on `detail.data`
 * alone and fell through to "Select a job to inspect it." otherwise — so a
 * detail request that 403'd or 503'd was reported as an idle panel.
 *
 * CYCLE 9 closes four more, all filed against this page:
 *   [HIGH] "Four of five jobs have sat `queued` with 0 attempts for over 24
 *          hours and nothing flags it … the KPI tiles read Jobs 5, Running 0,
 *          Failed 0, i.e. the dashboard presents an entirely healthy picture."
 *   [HIGH] "Job detail shows a raw JSON `{}` as the Result and identifies the
 *          job only by UUID."
 *   [MEDIUM] "Selecting a job does not change the URL, and rows use buttons
 *          instead of links."
 *   [MEDIUM] "Job list has no filter, search, sort, pagination or refresh
 *          control."
 *
 * The BLOCKER on this page — "Jobs cannot be cancelled, retried or requeued" —
 * is NOT closed here and is not asserted as if it were: the API exposes no
 * cancel or retry route, so the page states the limit instead of offering a
 * control that would 404.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient, Job, Me } from "../../lib/api";
import { JobsPage } from "./JobsPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
// The live-event socket is not the subject here and would open a real
// WebSocket in jsdom; the hook is stubbed so these stay pure component tests.
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => {} }));

function renderPage(ui: ReactElement, path = "/jobs") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["jobs.read"],
  // The zone the operator SET, which is the whole point: a test that reads
  // the machine's own zone passes on the machine that wrote it and proves
  // nothing about the defect.
  timezone: "America/New_York",
};

const JOB: Job = {
  id: "22222222-2222-2222-2222-222222222222",
  type: "inventory.collect",
  device_id: "11111111-1111-1111-1111-111111111111",
  status: "succeeded",
  attempts: 1,
  params: {},
  result: { ok: true },
  issued_at: "2026-08-20T09:00:00Z",
  expires_at: "2026-08-20T09:05:00Z",
  started_at: null,
  finished_at: null,
  cancel_requested_at: null,
  cancel_outcome: null,
  logs: [],
};

/**
 * The audit's actual queue: `patch.scan`, queued, 0 attempts, over a day old,
 * against an agent that was never going to answer.
 */
const STALE: Job = {
  ...JOB,
  id: "33333333-3333-3333-3333-333333333333",
  type: "patch.scan",
  status: "queued",
  attempts: 0,
  result: null,
  issued_at: "2026-08-19T09:00:00Z",
};

// "over 24 hours" is only a fact relative to a clock, so the clock is fixed.
const NOW = new Date("2026-08-20T10:00:00Z");

/**
 * The stat tile carrying `label`.
 *
 * `.closest(".stat-card")`, not `.closest("article")`: a tile is an ANCHOR
 * now (`shell/components.tsx`), because "I don't want dead tiles everywhere"
 * means every number is a link to the list behind it — keyboard reachable,
 * middle-clickable, pasteable into an incident channel. The old selector is
 * the reason two tests in this file went red on the redesign, and weakening
 * them to `closest("*")` would have hidden exactly the thing worth asserting.
 */
function tile(label: string): HTMLElement {
  // Located by the tile's own label, not by a page-wide text search: the
  // row pills now say "Failed" and "Running" in the same words as the tiles
  // (the wire status is humanised on the v3 kit), so a global lookup would
  // find the pill as readily as the tile.
  const card = [...document.querySelectorAll<HTMLElement>(".stat-card")].find(
    (candidate) => candidate.querySelector(".stat-label")?.textContent === label,
  );
  if (!card) throw new Error(`no stat tile labelled ${label}`);
  return card;
}

describe("JobsPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listJobs: vi.fn().mockResolvedValue({ items: [JOB], total: 1 }),
      getJob: vi.fn().mockResolvedValue(JOB),
      // The hostname join behind each row; an empty fleet leaves the id.
      listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      cancelJob: vi.fn().mockImplementation(async (id: string) => ({
        ...STALE,
        id,
        status: "cancelled" as const,
        cancel_outcome: "withdrawn",
        cancel_requested_at: "2026-08-20T09:59:00Z",
      })),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // 09:00Z is 05:00 in New York. The exact string is the assertion: a looser
  // check ("some date is rendered") passes against `toLocaleString()` and is
  // therefore worse than no test at all.
  it("shows Issued in the operator's profile zone, not the browser's", async () => {
    renderPage(<JobsPage />);
    expect(await screen.findByText("20 Aug 2026, 05:00")).toBeInTheDocument();
  });

  it("moves that same instant when the profile zone changes", async () => {
    api.me = vi.fn().mockResolvedValue({ ...ME, timezone: "Asia/Tokyo" });
    renderPage(<JobsPage />);
    expect(await screen.findByText("20 Aug 2026, 18:00")).toBeInTheDocument();
  });

  it("renders an em dash for a job with no issue time rather than \"Invalid Date\"", async () => {
    // The wire type says `issued_at: string`, but a console talking to an
    // older or partly-failed API must not print "Invalid Date" at an operator.
    api.listJobs = vi
      .fn()
      .mockResolvedValue({ items: [{ ...JOB, issued_at: "" } as Job], total: 1 });
    renderPage(<JobsPage />);

    await screen.findByText("Collect inventory");
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  // DESIGN REVIEW M8. A job issued inside the window that never started has
  // nothing to draw -- no `started_at`, so no bar, which is right: a
  // zero-width bar would claim a run happened and took no time. What that
  // produced on screen was one row among six with a label and a blank track,
  // reading as a mark the chart failed to paint rather than as a state the
  // fleet is in. The fixture case is the real one: `inventory.collect`
  // dispatched to an offline server, unstarted for hours.
  //
  // The console's answer everywhere else is a word, not a blank -- /backup's
  // "Never run", /ipam's "Never swept" -- so the panel names them under the
  // chart, derived from the same rows the chart draws.
  it("says which timeline rows have no bar, instead of leaving an empty track to be read as a missing mark", async () => {
    const RAN: Job = {
      ...JOB,
      id: "44444444-4444-4444-4444-444444444444",
      type: "patch.apply",
      started_at: "2026-08-20T09:10:00Z",
      finished_at: "2026-08-20T09:20:00Z",
    };
    api.listJobs = vi.fn().mockResolvedValue({ items: [JOB, RAN], total: 2 });
    renderPage(<JobsPage />);

    const timeline = (await screen.findByText("Last six hours")).closest(
      ".panel",
    ) as HTMLElement;

    // Two rows, one bar: the run that has a duration draws one, the job that
    // never started does not.
    expect(timeline.querySelectorAll(".tl-row")).toHaveLength(2);
    expect(timeline.querySelectorAll(".tl-bar")).toHaveLength(1);

    // And the empty track is accounted for in words, naming the row.
    expect(timeline).toHaveTextContent(/One job in this window has not started/);
    expect(timeline).toHaveTextContent(/inventory\.collect/);
  });

  it("names the job type in English while keeping the wire type", async () => {
    // "[MEDIUM] Patch tab exposes the raw enum value … Map every enum to a
    // display string at the presentation layer … Apply the same mapping to job
    // types (`inventory.collect` → 'Collect inventory')."
    renderPage(<JobsPage />);

    expect(await screen.findByText("Collect inventory")).toBeInTheDocument();
    // The identifier stays: it is the string an operator greps the backend for.
    // The wire type is on screen twice now: under the label in its row, and
    // as the mono key of its run on the six-hour timeline (v3 draws both).
    expect(screen.getAllByText("inventory.collect").length).toBeGreaterThan(0);
  });

  it("flags a job that has been waiting far too long, and counts it", async () => {
    // The finding, exactly: a day-old queued job with the tiles reading
    // "Running 0 / Failed 0" and nothing anywhere saying the queue is stuck.
    api.listJobs = vi.fn().mockResolvedValue({ items: [STALE], total: 1 });
    renderPage(<JobsPage />);

    expect(await screen.findByText("25h ago")).toBeInTheDocument();
    const stuckTile = tile("Waiting over 15 min");
    expect(within(stuckTile as HTMLElement).getByText("1")).toBeInTheDocument();
    // And the banner, because nothing is running either.
    expect(screen.getByRole("status")).toHaveTextContent(/waiting more than 15 minutes/i);
  });

  it("does not flag a job that was issued moments ago", async () => {
    // The falsifier for the flag: if "stuck" meant "queued", every healthy
    // queue would light up and the tile would be ignored within a week.
    api.listJobs = vi
      .fn()
      .mockResolvedValue({
        items: [{ ...STALE, issued_at: "2026-08-20T09:59:00Z" }],
        total: 1,
      });
    renderPage(<JobsPage />);

    await screen.findByText("Scan for patches");
    const stuckTile = tile("Waiting over 15 min");
    expect(within(stuckTile as HTMLElement).getByText("0")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  // ---------------------------------------------------------------------
  // WHAT EACH FIGURE IS MADE OF. v3 draws a supporting line under every tile
  // and the port passed only `zeroLabel`, so a tile said something beyond its
  // number exactly when the number happened to be zero. Every line is derived
  // from the rows on the page: a breakdown by type on the failures, the
  // longest run in flight, and which of the two waiting states the overdue
  // jobs are in.
  //
  // BREAKS AGAINST: the foots dropped, and against a foot invented where the
  // rows cannot support one.
  // ---------------------------------------------------------------------
  it("says what each non-zero figure is made of", async () => {
    api.listJobs = vi.fn().mockResolvedValue({
      items: [
        JOB,
        STALE,
        { ...JOB, id: "f1", type: "patch.apply", status: "failed" as const },
        { ...JOB, id: "f2", type: "patch.apply", status: "failed" as const },
        { ...JOB, id: "f3", type: "backup.run", status: "failed" as const },
        {
          ...JOB,
          id: "r1",
          type: "patch.apply",
          status: "running" as const,
          started_at: "2026-08-20T09:48:00Z",
        },
      ],
      total: 6,
    });
    renderPage(<JobsPage />);

    await screen.findByText("Scan for patches");
    expect(tile("Failed")).toHaveTextContent("2 patch.apply · 1 backup.run");
    expect(tile("Running")).toHaveTextContent("Longest run 12m");
    expect(tile("Waiting over 15 min")).toHaveTextContent("1 never sent");
    expect(tile("Jobs")).toHaveTextContent("1 succeeded · 5 in other states");
  });

  it("states the scope instead of breaking down a set it does not hold", async () => {
    // The figure is the server's total for the whole queue; the breakdown
    // could only ever describe the page in hand. When they differ, the line
    // says how much is loaded rather than claiming a breakdown of 200 rows
    // from 2.
    api.listJobs = vi.fn().mockResolvedValue({ items: [JOB, STALE], total: 200 });
    renderPage(<JobsPage />);

    await screen.findByText("Scan for patches");
    expect(tile("Jobs")).toHaveTextContent("2 loaded, newest first");
  });

  // The filter is v3's chip row rather than a native select (the design draws
  // it that way, and the browser's own control does not take the kit's
  // styling). Every value the select carried is a chip, and the chips say the
  // states in words instead of the wire's lowercase `queued`.
  it("filters the list by status", async () => {
    const user = userEvent.setup();
    api.listJobs = vi.fn().mockResolvedValue({ items: [JOB, STALE], total: 2 });
    renderPage(<JobsPage />);

    await screen.findByText("Collect inventory");
    const statusFilter = screen.getByRole("group", { name: "Filter by status" });
    await user.click(within(statusFilter).getByRole("button", { name: "Queued" }));

    expect(screen.getByText("Scan for patches")).toBeInTheDocument();
    expect(screen.queryByText("Collect inventory")).not.toBeInTheDocument();
    expect(within(statusFilter).getByRole("button", { name: "Queued" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // "Waiting too long" is the derived state the tiles link to; it survived
    // the move off the select.
    expect(within(statusFilter).getByRole("button", { name: "Waiting too long" })).toBeInTheDocument();
  });

  it("searches by job type, and says so when nothing matches", async () => {
    const user = userEvent.setup();
    api.listJobs = vi.fn().mockResolvedValue({ items: [JOB, STALE], total: 2 });
    renderPage(<JobsPage />);

    await screen.findByText("Collect inventory");
    await user.type(screen.getByLabelText("Search jobs"), "patch");
    expect(screen.queryByText("Collect inventory")).not.toBeInTheDocument();

    await user.clear(screen.getByLabelText("Search jobs"));
    await user.type(screen.getByLabelText("Search jobs"), "nothing-like-this");
    // "No jobs dispatched yet." would be a lie — there are two.
    expect(screen.getByText("No jobs match that filter.")).toBeInTheDocument();
  });

  it("puts the selected job in the URL and opens it from there", async () => {
    // The row action is an anchor now, so it can be middle-clicked, opened in
    // a new tab and pasted into an incident channel.
    const user = userEvent.setup();
    renderPage(<JobsPage />);

    await screen.findByText("Collect inventory");
    const detail = screen.getByRole("link", { name: "Detail" });
    expect(detail).toHaveAttribute("href", `/jobs?job=${JOB.id}`);

    await user.click(detail);
    expect(await screen.findByText(JOB.id)).toBeInTheDocument();
  });

  it("opens a job named in the URL without a click", async () => {
    renderPage(<JobsPage />, `/jobs?job=${JOB.id}`);
    expect(await screen.findByText(JOB.id)).toBeInTheDocument();
    expect(api.getJob).toHaveBeenCalledWith(JOB.id);
  });

  it("identifies the open job by type and time, not by UUID alone", async () => {
    // "Once the panel opens, the user has lost the only human-readable
    // identity the job had; they must cross-reference the row they clicked."
    renderPage(<JobsPage />, `/jobs?job=${JOB.id}`);

    const panel = (await screen.findByText(JOB.id)).closest("div")?.parentElement as HTMLElement;
    expect(within(panel).getByRole("heading", { name: "Collect inventory" })).toBeInTheDocument();
    expect(within(panel).getAllByText("20 Aug 2026, 05:00").length).toBeGreaterThan(0);
    expect(within(panel).getByText("1")).toBeInTheDocument();
  });

  it("says a job has produced no result instead of printing {}", async () => {
    // "`{}` is developer output, not a user-facing empty state — it reads as
    // 'something is broken' rather than 'this job hasn't produced a result
    // yet'."
    api.getJob = vi.fn().mockResolvedValue({ ...STALE, result: null, params: {} });
    renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

    expect(await screen.findByText("No result yet — this job has not run.")).toBeInTheDocument();
    expect(screen.queryByText("{}")).not.toBeInTheDocument();
  });

  it("still shows a real result payload verbatim", async () => {
    // The falsifier for the line above: an empty-state that swallowed real
    // results would be a much worse defect than the one it replaced.
    renderPage(<JobsPage />, `/jobs?job=${JOB.id}`);
    expect(await screen.findByText(/"ok": true/)).toBeInTheDocument();
  });

  /**
   * Cancellation, cycle 1 of the job-lifecycle design.
   *
   * The BLOCKER's first half closes here: a job still QUEUED can be withdrawn,
   * and the console may say so plainly because the platform can prove it --
   * `queued` means the signed envelope was never written to a socket. Every
   * other state still cannot be stopped, and the page says which and why
   * rather than leaving a control that would 409.
   */
  describe("cancelling a job", () => {
    // The panel reads whatever `getJob` answers, and the queued job is the
    // subject of every test here. Set explicitly rather than relying on the
    // `?job=` parameter: the id in the URL selects the row, the fetch decides
    // what the panel renders, and a test that conflated the two would pass
    // against a panel showing the wrong job.
    beforeEach(() => {
      api.getJob = vi.fn().mockResolvedValue(STALE);
    });

    it("offers withdrawal for a queued job and says what it means", async () => {
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);
      expect(
        await screen.findByRole("button", { name: /cancel this job/i }),
      ).toBeInTheDocument();
      expect(screen.getByText(/has not been sent to the device yet/i)).toBeInTheDocument();
    });

    it("confirms before withdrawing, and names the job it would withdraw", async () => {
      const user = userEvent.setup();
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

      await user.click(await screen.findByRole("button", { name: /cancel this job/i }));
      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByText(STALE.id)).toBeInTheDocument();
      expect(api.cancelJob).not.toHaveBeenCalled();

      await user.click(within(dialog).getByRole("button", { name: /withdraw the job/i }));
      expect(api.cancelJob).toHaveBeenCalledWith(STALE.id);
    });

    it("reports a withdrawn job as withdrawn, not merely cancelled", async () => {
      // The honesty requirement. "Cancelled" alone is what the platform can
      // say once it has evidence; "withdrawn before it was sent" is what the
      // evidence actually is, and it is the sentence an operator needs to know
      // the machine never saw the job.
      api.getJob = vi.fn().mockResolvedValue({
        ...STALE,
        status: "cancelled",
        cancel_outcome: "withdrawn",
        cancel_requested_at: "2026-08-20T09:59:00Z",
      });
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

      expect(
        await screen.findByText("Withdrawn before it was sent to the device."),
      ).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /cancel this job/i })).not.toBeInTheDocument();
    });

    it("offers to ASK a dispatched job's device to stop, and does not call it a withdrawal", async () => {
      // Cycle 2. The platform has no authority over a job a machine already
      // holds, so the control says what it will actually do: send a signed
      // instruction and report what comes back. Calling it "Cancel this job"
      // -- the withdrawal wording -- would promise an outcome the server
      // cannot deliver on its own.
      api.getJob = vi.fn().mockResolvedValue({ ...STALE, status: "dispatched" });
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

      expect(
        await screen.findByRole("button", { name: /ask the device to stop this job/i }),
      ).toBeInTheDocument();
      expect(screen.getByText(/already has this job/i)).toBeInTheDocument();
      // The expiry is rendered in the operator's PROFILE zone, like every
      // other instant on this page: 09:05Z is 05:05 in New York.
      expect(screen.getByText(/expires at 20 Aug 2026, 05:05/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^cancel this job$/i })).not.toBeInTheDocument();
    });

    it("names the endpoint's part in the dialog before sending a signed stop instruction", async () => {
      const user = userEvent.setup();
      api.getJob = vi.fn().mockResolvedValue({ ...STALE, status: "running" });
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

      await user.click(
        await screen.findByRole("button", { name: /ask the device to stop this job/i }),
      );
      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByText(/asks the device to stop/i)).toBeInTheDocument();
      expect(within(dialog).getByText(/half-done/i)).toBeInTheDocument();
      await user.click(within(dialog).getByRole("button", { name: /send the stop instruction/i }));
      expect(api.cancelJob).toHaveBeenCalledWith(STALE.id);
    });

    it("says a cancel was ASKED FOR while the job is still running", async () => {
      // The sentence the whole design exists to make possible: two true facts
      // shown together. The badge still says `running`, because it is.
      api.getJob = vi.fn().mockResolvedValue({
        ...STALE,
        status: "running",
        cancel_requested_at: "2026-08-20T09:59:00Z",
        cancel_outcome: "requested",
      });
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

      expect(await screen.findByText(/cancel requested at/i)).toBeInTheDocument();
      expect(screen.getByText(/has not confirmed/i)).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /ask the device to stop this job/i }),
      ).not.toBeInTheDocument();
    });

    it("tells the operator when the endpoint's agent is too old to be interrupted", async () => {
      api.getJob = vi.fn().mockResolvedValue({
        ...STALE,
        status: "running",
        cancel_requested_at: "2026-08-20T09:59:00Z",
        cancel_outcome: "unsupported",
      });
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

      expect(await screen.findByText(/too old to be interrupted/i)).toBeInTheDocument();
    });

    it("does not call a job cancelled when the cancel arrived too late", async () => {
      api.getJob = vi.fn().mockResolvedValue({
        ...STALE,
        status: "succeeded",
        cancel_requested_at: "2026-08-20T09:59:00Z",
        cancel_outcome: "requested",
      });
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

      expect(await screen.findByText(/cancel arrived too late/i)).toBeInTheDocument();
      expect(screen.queryByText(/^Cancelled\.$/)).not.toBeInTheDocument();
    });

    it("announces a refused cancel rather than leaving the button silent", async () => {
      // Authorization is server-side: the console does not know which
      // permission a job type needs, so it offers the control and reports the
      // refusal. A button that appeared to do nothing would be worse than one
      // that is not there.
      const user = userEvent.setup();
      api.cancelJob = vi.fn().mockRejectedValue(new ApiError(403, ""));
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

      await user.click(await screen.findByRole("button", { name: /cancel this job/i }));
      await user.click(
        within(await screen.findByRole("dialog")).getByRole("button", {
          name: /withdraw the job/i,
        }),
      );

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("You do not have permission to do that.");
    });

    it("does not offer to re-run a job that has not finished", async () => {
      // Cycle 3. The console owes the operator the truth about what it cannot
      // do: a queued job has nothing to re-run yet, and the platform's own
      // sweep already re-sends one that never reached its device.
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);
      expect(await screen.findByText(/nothing to re-run yet/i)).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /run this job again/i }),
      ).not.toBeInTheDocument();
    });

    it("re-runs a failed job as a NEW job and follows the new one", async () => {
      // **THE SELECTION MOVING IS THE INTERACTION DESIGN, NOT A NICETY.** A
      // retry is a new row with a new id. A console that left the operator
      // looking at the old one would show a failed job that stays failed
      // forever while the work they asked for runs off screen -- which, from
      // where they are sitting, is a button that did nothing.
      const user = userEvent.setup();
      const failed = { ...STALE, status: "failed" as const };
      const created = { ...STALE, id: "job-retry-1", status: "queued" as const,
        retry_of_job_id: STALE.id };
      api.getJob = vi.fn().mockImplementation(async (id: string) =>
        id === created.id ? created : failed,
      );
      api.retryJob = vi.fn().mockResolvedValue(created);
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

      await user.click(await screen.findByRole("button", { name: /run this job again/i }));
      const dialog = await screen.findByRole("dialog");
      // The dialog says what actually happens: this row is left alone and a
      // fresh job is created. Implying the row would come back to life would
      // be describing a replay, which is not what the platform does.
      expect(within(dialog).getByText(/does not re-send the original job/i)).toBeInTheDocument();
      await user.click(within(dialog).getByRole("button", { name: /create the new job/i }));

      expect(api.retryJob).toHaveBeenCalledWith(STALE.id);
      expect(await screen.findByText(/this job is a retry of/i)).toBeInTheDocument();
    });

    it("names the earlier cancellation when re-running a job somebody stopped", async () => {
      const user = userEvent.setup();
      api.getJob = vi.fn().mockResolvedValue({
        ...STALE,
        status: "cancelled",
        cancel_outcome: "withdrawn",
        cancel_requested_at: "2026-08-20T09:59:00Z",
      });
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

      await user.click(await screen.findByRole("button", { name: /run this job again/i }));
      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByText(/somebody cancelled this job/i)).toBeInTheDocument();
    });

    it("says a succeeded job is not a retry rather than hiding the control", async () => {
      api.getJob = vi.fn().mockResolvedValue({ ...STALE, status: "succeeded" });
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);
      expect(await screen.findByText(/running the same work again is a new job/i)).toBeInTheDocument();
    });

    it("announces a refused retry rather than leaving the button silent", async () => {
      // Authorization is server-side, and so is whether the kernel route may
      // re-run this job's TYPE at all: a governed type whose module has not
      // said how to repoint its own record is a 403 naming where it CAN be
      // re-run. Both refusals reach the operator as words.
      const user = userEvent.setup();
      api.getJob = vi.fn().mockResolvedValue({ ...STALE, status: "failed" });
      api.retryJob = vi.fn().mockRejectedValue(new ApiError(403, ""));
      renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);

      await user.click(await screen.findByRole("button", { name: /run this job again/i }));
      await user.click(
        within(await screen.findByRole("dialog")).getByRole("button", {
          name: /create the new job/i,
        }),
      );

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("You do not have permission to do that.");
    });
  });

  it("announces a failed job detail instead of claiming nothing is selected", async () => {
    const user = userEvent.setup();
    api.getJob = vi.fn().mockRejectedValue(new ApiError(403, ""));
    renderPage(<JobsPage />);

    await screen.findByText("Collect inventory");
    await user.click(screen.getByRole("link", { name: "Detail" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You do not have permission to do that.");
    expect(screen.queryByText("Select a job to inspect it.")).not.toBeInTheDocument();
  });

  it("renders a sentence for a bodyless 503 on the detail request", async () => {
    const user = userEvent.setup();
    api.getJob = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderPage(<JobsPage />);

    await screen.findByText("Collect inventory");
    await user.click(screen.getByRole("link", { name: "Detail" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/);
    expect(alert.textContent).not.toContain("Error:");
    expect(alert.textContent?.trim()).not.toBe("");
  });

  // The inspector is not rendered at all until a job is selected -- it held a
  // third of the width blank and squeezed the queue beside it. The state this
  // test guards is unchanged: nothing selected must not look like a failure.
  it("shows no inspector, and no alarm, when nothing is selected", async () => {
    renderPage(<JobsPage />);

    await screen.findByText("Collect inventory");
    expect(screen.queryByRole("heading", { name: "Job detail" })).not.toBeInTheDocument();
    expect(screen.queryByText("Select a job to inspect it.")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("the job console's own long tail", () => {
  beforeEach(() => {
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listJobs: vi.fn().mockResolvedValue({ items: [STALE], total: 1 }),
      getJob: vi.fn().mockResolvedValue(STALE),
    } as unknown as ApiClient);
  });

  it("bounds the search box rather than accepting a pasted document", async () => {
    // The same unbounded-input finding the audit filed against /devices; this
    // page grew its search after that cycle and must not reintroduce it.
    renderPage(<JobsPage />);
    await screen.findByText("Scan for patches");
    expect(screen.getByLabelText("Search jobs")).toHaveAttribute("maxlength", "128");
  });

  it("gives the page headings below the H1 to navigate by", async () => {
    // "[LOW] Page has a single `<h1>` and no section headings … With one
    // heading per page, the heading shortcut is useless and the page's
    // structure is invisible to assistive tech."
    renderPage(<JobsPage />);
    await screen.findByText("Scan for patches");
    const h2s = screen.getAllByRole("heading", { level: 2 }).map((el) => el.textContent);
    expect(h2s).toContain("Job queue");
  });

  it("gives the inspector its own heading once a job is selected", async () => {
    // The second section exists exactly when its pane does.
    renderPage(<JobsPage />, `/jobs?job=${STALE.id}`);
    // Both the row and the inspector's own title carry the job's name once
    // the pane is open, so this waits on the pane rather than on the word.
    await screen.findByRole("heading", { name: "Job detail" });
    const h2s = screen.getAllByRole("heading", { level: 2 }).map((el) => el.textContent);
    expect(h2s).toContain("Job detail");
  });
});


/**
 * NO DEAD TILES. The operator's instruction, verbatim: "all tiles and all
 * numbers or kpis need to be clickable and direct the user to the specific
 * menus, i dont want dead tiles everywhere".
 *
 * These are the tests that stop that decaying. A tile whose `to` drifts to a
 * route that does not exist, or to a filter this page stopped reading, is a
 * dead tile again — and the only difference a reader sees is that clicking it
 * does nothing. So the hrefs are asserted EXACTLY, and then each destination
 * is opened and the list it lands on is checked: a link to
 * `/jobs?status=failed` that shows the whole queue is the same dead tile in a
 * better costume.
 */
describe("every KPI tile is a link to the list it counts", () => {
  const FAILED: Job = {
    ...JOB,
    id: "44444444-4444-4444-4444-444444444444",
    type: "patch.apply",
    status: "failed",
  };

  beforeEach(() => {
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listJobs: vi.fn().mockResolvedValue({ items: [JOB, STALE, FAILED], total: 3 }),
      getJob: vi.fn().mockResolvedValue(JOB),
    } as unknown as ApiClient);
  });

  it("renders every tile as an anchor with a real href", async () => {
    renderPage(<JobsPage />);
    await screen.findByText("Collect inventory");

    const hrefs = [...document.querySelectorAll(".stat-card")].map((card) => ({
      tag: card.tagName,
      href: card.getAttribute("href"),
    }));
    expect(hrefs).toEqual([
      { tag: "A", href: "/jobs" },
      { tag: "A", href: "/jobs?status=running" },
      { tag: "A", href: "/jobs?status=failed" },
      { tag: "A", href: "/jobs?status=stuck" },
    ]);
    // Every destination is a route the router actually serves: /jobs is in
    // App.tsx's table, and the query strings are this page's own filter.
    expect(hrefs.every((tile) => tile.href?.startsWith("/jobs"))).toBe(true);
  });

  it("lands the Failed tile on the failed jobs, not on the whole queue", async () => {
    const user = userEvent.setup();
    renderPage(<JobsPage />);
    await screen.findByText("Collect inventory");

    await user.click(within(tile("Failed")).getByText("Failed"));

    expect(screen.getByText("Apply patches")).toBeInTheDocument();
    expect(screen.queryByText("Collect inventory")).not.toBeInTheDocument();
    expect(screen.queryByText("Scan for patches")).not.toBeInTheDocument();
  });

  it("lands the waiting tile on the jobs that have waited, keeping the tile's own count", async () => {
    renderPage(<JobsPage />, "/jobs?status=stuck");
    // STALE is the only one over the 15-minute threshold.
    expect(await screen.findByText("Scan for patches")).toBeInTheDocument();
    expect(screen.queryByText("Collect inventory")).not.toBeInTheDocument();
    // The tile stays FLEET-WIDE while the list is filtered: the count is a
    // fact about the queue, not about the current view.
    expect(within(tile("Waiting over 15 min")).getByText("1")).toBeInTheDocument();
  });

  it("takes the Jobs tile back to the unfiltered queue", async () => {
    const user = userEvent.setup();
    renderPage(<JobsPage />, "/jobs?status=failed");
    await screen.findByText("Apply patches");
    expect(screen.queryByText("Collect inventory")).not.toBeInTheDocument();

    await user.click(within(tile("Jobs")).getByText("Jobs"));
    expect(screen.getByText("Collect inventory")).toBeInTheDocument();
  });

  it("keeps the open job when a filter is applied, so the detail pane survives it", async () => {
    renderPage(<JobsPage />, `/jobs?status=failed&job=${JOB.id}`);
    await screen.findByText("Apply patches");
    const detail = screen.getByRole("link", { name: "Detail" });
    expect(detail).toHaveAttribute("href", `/jobs?status=failed&job=${FAILED.id}`);
  });

  it("makes only what needs a person loud, and says which zero a zero is", async () => {
    // WEIGHT IS URGENCY, COLOUR IS CATEGORY. Three failed jobs is a person's
    // problem; the queue's total is not, however large.
    renderPage(<JobsPage />);
    await screen.findByText("Collect inventory");

    expect(tile("Failed")).toHaveClass("urgent");
    expect(tile("Failed")).toHaveClass("tone-red");
    expect(tile("Jobs")).not.toHaveClass("urgent");
    expect(tile("Jobs")).toHaveClass("tone-neutral");
    // Nothing is running, and that zero says which zero it is rather than
    // sitting there as a bare 0 an operator has to interpret.
    expect(tile("Running")).not.toHaveClass("urgent");
    expect(tile("Running")).toHaveTextContent("Nothing is executing right now");
  });

  it("drops the urgent treatment from a zero — an alarm-coloured 0 reads as a perfect score", async () => {
    // The falsifier for the line above: if `urgent` survived a zero, "Failed
    // 0" would wear the same saturated red as "Failed 12".
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listJobs: vi.fn().mockResolvedValue({ items: [JOB], total: 1 }),
      getJob: vi.fn().mockResolvedValue(JOB),
    } as unknown as ApiClient);
    renderPage(<JobsPage />);
    await screen.findByText("Collect inventory");

    expect(tile("Failed")).toHaveTextContent("0");
    expect(tile("Failed")).not.toHaveClass("urgent");
    expect(tile("Failed")).toHaveTextContent("No job in this list failed");
  });
});

/**
 * A job runs on a computer with the agent, so an estate where none runs it is
 * the one thing from another module this history depends on
 * (docs/design/cross-module-guidance.md §2, Job history).
 */
describe("JobsPage — before jobs have somewhere to run", () => {
  const TITLE = "Before jobs have somewhere to run";
  const fleetPage = (managed: number) => ({
    items: [],
    total: 0,
    state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
    fleet: { total: 2, managed, online: managed, cert_attention: 0 },
  });

  function stub(managed: number, permissions: string[]) {
    const api = {
      me: vi.fn().mockResolvedValue({ ...ME, permissions }),
      listJobs: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      getJob: vi.fn(),
      listDevices: vi.fn().mockResolvedValue(fleetPage(managed)),
    };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);
    return api;
  }

  it("sends an estate where no computer runs the agent to add one", async () => {
    stub(0, ["jobs.read", "devices.read"]);
    renderPage(<JobsPage />);
    const link = await screen.findByRole("link", { name: "Add a computer" });
    expect(link).toHaveAttribute("href", "/devices/onboard");
    expect(screen.getByText(TITLE)).toBeInTheDocument();
  });

  it("is absent once a computer runs the agent", async () => {
    const api = stub(2, ["jobs.read", "devices.read"]);
    renderPage(<JobsPage />);
    await vi.waitFor(() => expect(api.listDevices).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(screen.queryByText(TITLE)).not.toBeInTheDocument();
  });

  it("says it cannot be checked from an account without the device permission", async () => {
    stub(0, ["jobs.read"]);
    renderPage(<JobsPage />);
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Add a computer" })).not.toBeInTheDocument();
  });
});
