import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient, type Me } from "../../lib/api";
import { ERROR_GROUPS_LIST_LIMIT } from "./api";
import { ErrorMonitorPage, selectErrorGroups } from "./ErrorMonitorPage";
import type { ErrorGroup, ErrorGroupDetail, ErrorGroupList } from "./types";

// Pure component test: the auth-backed API client is mocked, mirroring
// modules/platform-status/PlatformStatusPage.test.tsx.
const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** The filters live in the query string (cycle 12), so the tests read the
 *  query string rather than a component's private state. */
function LocationProbe() {
  const location = useLocation();
  // A span, not an <output>: <output> carries an implicit role of
  // "status", which would collide with the page's live region.
  return <span data-testid="search">{location.search}</span>;
}

function renderWithProviders(ui: ReactElement, entry = "/error-monitor") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={queryClient}>
        {ui}
        <LocationProbe />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** Step two of the resolve flow: the dialog that now stands between the
 *  Resolve button and the write. */
async function confirmResolve(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole("dialog");
  await user.click(within(dialog).getByRole("button", { name: "Resolve group" }));
}

/**
 * The caller the triage controls are drawn for.
 *
 * BOTH WRITES TAKE ONE CODE. `POST /platform/errors/{id}/resolve` and its
 * inverse are gated on `platform.errors.write` server-side, so the console
 * draws neither control without it — see the read-only test below, which is
 * what stops that gate from being a comment.
 */
const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["platform.errors.read", "platform.errors.write"],
};

const READ_ONLY_ME: Me = { ...ME, permissions: ["platform.errors.read"] };

const OPEN_GROUP: ErrorGroup = {
  id: "11111111-1111-1111-1111-111111111111",
  fingerprint: "abc123def4567890",
  exc_type: "ValueError",
  module: "app.jobs.dispatch",
  lineno: 142,
  qualname: "dispatch_job",
  where: "worker",
  first_seen: "2026-08-10T12:00:00Z",
  last_seen: "2026-08-15T09:30:00Z",
  event_count: 7,
  resolved_at: null,
};

const RESOLVED_GROUP: ErrorGroup = {
  id: "22222222-2222-2222-2222-222222222222",
  fingerprint: "9990001112223334",
  exc_type: "TimeoutError",
  module: "app.gateway.conn",
  lineno: 88,
  qualname: "handle_connect",
  where: "gateway",
  first_seen: "2026-08-01T00:00:00Z",
  last_seen: "2026-08-02T00:00:00Z",
  event_count: 3,
  resolved_at: "2026-08-03T00:00:00Z",
};

const LIST: ErrorGroupList = { groups: [OPEN_GROUP, RESOLVED_GROUP], dropped: 0 };

const DETAIL: ErrorGroupDetail = {
  group: OPEN_GROUP,
  events: [
    {
      id: "33333333-3333-3333-3333-333333333333",
      occurred_at: "2026-08-15T09:30:00Z",
      request_id: "req-abc-999",
      org_id: null,
      user_id: null,
      job_id: "44444444-4444-4444-4444-444444444444",
      device_id: null,
    },
  ],
};

describe("ErrorMonitorPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listErrorGroups: vi.fn().mockResolvedValue(LIST),
      getErrorGroup: vi.fn().mockResolvedValue(DETAIL),
      resolveErrorGroup: vi.fn().mockResolvedValue({ ...OPEN_GROUP, resolved_at: "2026-08-15T10:00:00Z" }),
      unresolveErrorGroup: vi.fn().mockResolvedValue({ ...RESOLVED_GROUP, resolved_at: null }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("renders a group's exc_type and module:line structurally", async () => {
    renderWithProviders(<ErrorMonitorPage />);

    const typeCell = await screen.findByText("ValueError");
    const row = typeCell.closest("tr") as HTMLElement;
    // Structural: read the specific raise-site cell rather than a regex over
    // the row's concatenated text (toHaveTextContent joins cells with no
    // separator -- see PlatformStatusPage.test.tsx for the false-green this
    // produced previously).
    expect(within(row).getByText("app.jobs.dispatch:142")).toBeInTheDocument();
  });

  // NOT a data-leak guard: the fixtures above have no message-like field, so
  // no assertion in this test could ever observe one leaking. This is a
  // label tripwire only -- it fails if a future change adds a column or
  // heading literally called "message"/"traceback"/"detail text", as a
  // reminder that this page's invariant (no exception text, ever) would need
  // re-checking against whatever put that label there.
  it("has no message/traceback/detail-text label anywhere on the page", async () => {
    renderWithProviders(<ErrorMonitorPage />);

    await screen.findByText("ValueError");
    expect(screen.queryByText(/message/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/traceback/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/detail text/i)).not.toBeInTheDocument();
  });

  it("does not show the dropped-count banner when dropped is zero", async () => {
    renderWithProviders(<ErrorMonitorPage />);

    await screen.findByText("ValueError");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the dropped-count banner, naming it a floor, when dropped is non-zero", async () => {
    (api.listErrorGroups as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...LIST,
      dropped: 42,
    });
    renderWithProviders(<ErrorMonitorPage />);

    const banner = await screen.findByRole("alert");
    expect(within(banner).getByText(/42/)).toBeInTheDocument();
    expect(banner.textContent).toMatch(/floor/i);
  });

  it("resolve control calls the write endpoint for that group, not the read endpoint", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);

    const openCell = await screen.findByText("ValueError");
    const openRow = openCell.closest("tr") as HTMLElement;
    const resolveButton = within(openRow).getByRole("button", { name: /resolve/i });

    await user.click(resolveButton);
    await confirmResolve(user);

    expect(api.resolveErrorGroup).toHaveBeenCalledExactlyOnceWith(OPEN_GROUP.id);
    // The Resolve button's click must not also select the row -- if it did,
    // clicking Resolve would fire the read endpoint (via row selection) too.
    expect(api.getErrorGroup).not.toHaveBeenCalled();
  });

  // DESIGN REVIEW M3. `Events` and `Resolve…` / `Reopen` were inline-blocks
  // separated by a text space, so the cell broke wherever the longer label no
  // longer fitted: open rows stacked `Events` above `Resolve…` and grew about
  // 20px taller than resolved rows, which kept `Events | Reopen` on one line.
  // One table, two row heights, a ragged action column. A nowrap flex row
  // makes the column declare the width it actually needs -- the same three
  // utilities `health/ProfileList` uses on its four-control cell.
  it("keeps a row's controls on one line, so open and resolved rows are the same height", async () => {
    renderWithProviders(<ErrorMonitorPage />);

    const openRow = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;
    const resolvedRow = screen.getByText("TimeoutError").closest("tr") as HTMLElement;

    for (const [row, second] of [
      [openRow, /resolve/i],
      [resolvedRow, /reopen/i],
    ] as const) {
      const events = within(row).getByRole("button", { name: /^Events of/ });
      const other = within(row).getByRole("button", { name: second });
      const holder = events.parentElement as HTMLElement;
      // One container, and it does not wrap.
      expect(holder).toContainElement(other);
      expect(holder).toHaveClass("flex");
      expect(holder).toHaveClass("flex-nowrap");
      // And no stray text node between them to break on -- the space that was
      // doing the wrapping.
      expect(holder.textContent?.trim()).toBe(`${events.textContent}${other.textContent}`);
    }
  });

  it("gives an already-resolved group no resolve control", async () => {
    renderWithProviders(<ErrorMonitorPage />);

    const resolvedCell = await screen.findByText("TimeoutError");
    const resolvedRow = resolvedCell.closest("tr") as HTMLElement;
    expect(within(resolvedRow).queryByRole("button", { name: /resolve/i })).not.toBeInTheDocument();
  });

  it("selecting a group fetches and renders its recent events' correlation ids", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);

    const openCell = await screen.findByText("ValueError");
    // The detail query is gated on a group being selected -- before the
    // click, nothing has been selected yet, so it must not have fired. If
    // the `enabled` guard on useErrorGroupDetail were removed, this query
    // would run on mount with `groupId === undefined`, silently hitting
    // GET /api/v1/platform/errors/undefined.
    expect(api.getErrorGroup).not.toHaveBeenCalled();

    await user.click(openCell.closest("tr") as HTMLElement);

    expect(api.getErrorGroup).toHaveBeenCalledWith(OPEN_GROUP.id);
    const requestIdCell = await screen.findByText("req-abc-999");
    const eventRow = requestIdCell.closest("tr") as HTMLElement;
    // org_id/user_id/device_id are null for this event -- rendered as an
    // explicit placeholder, not blank, and not fabricated. Exactly three:
    // occurred_at, request_id and job_id all have real values, so a
    // regression that renders "—" for job_id too would still need to be
    // caught, not just outnumbered.
    const placeholders = within(eventRow).getAllByText("—");
    expect(placeholders).toHaveLength(3);
    expect(within(eventRow).getByText("44444444-4444-4444-4444-444444444444")).toBeInTheDocument();
  });

  it("surfaces a visible error when resolve fails, e.g. a 403 from an operator without write access", async () => {
    // RBAC is enforced server-side, deliberately not gated in this UI (see
    // ErrorMonitorPage.tsx's comment), so this error message is the entire
    // feedback path for read-only-but-not-write operators.
    (api.resolveErrorGroup as ReturnType<typeof vi.fn>).mockRejectedValue(
      new ApiError(403, "insufficient permissions"),
    );
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);

    const openCell = await screen.findByText("ValueError");
    const openRow = openCell.closest("tr") as HTMLElement;
    const resolveButton = within(openRow).getByRole("button", { name: /resolve/i });

    await user.click(resolveButton);
    await confirmResolve(user);

    expect(await screen.findByText(/insufficient permissions/i)).toBeInTheDocument();
  });

  it("shows an empty state instead of a bare table when there are no error groups", async () => {
    (api.listErrorGroups as ReturnType<typeof vi.fn>).mockResolvedValue({
      groups: [],
      dropped: 0,
    });
    renderWithProviders(<ErrorMonitorPage />);

    expect(await screen.findByText(/no error groups/i)).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Type" })).not.toBeInTheDocument();
  });

  it("tells the operator the list is truncated when the group count hits the limit", async () => {
    const manyGroups: ErrorGroup[] = Array.from({ length: ERROR_GROUPS_LIST_LIMIT }, (_, i) => ({
      ...OPEN_GROUP,
      id: `${i}`,
      fingerprint: `fp-${i}`,
    }));
    (api.listErrorGroups as ReturnType<typeof vi.fn>).mockResolvedValue({
      groups: manyGroups,
      dropped: 0,
    });
    renderWithProviders(<ErrorMonitorPage />);

    expect(await screen.findByText(/truncated/i)).toBeInTheDocument();
    expect(api.listErrorGroups).toHaveBeenCalledWith(ERROR_GROUPS_LIST_LIMIT);
  });

  it("does not claim truncation when the group count is under the limit", async () => {
    renderWithProviders(<ErrorMonitorPage />);

    await screen.findByText("ValueError");
    expect(screen.queryByText(/truncated/i)).not.toBeInTheDocument();
  });
});

/**
 * CYCLE 12 — TRIAGE. Auditor B, [HIGH]: *"Clicking Resolve immediately flipped
 * the Open KPI from 1 to 0. No confirmation dialog appeared … no undo."* And
 * [MEDIUM]: *"The page has zero inputs … no open-vs-resolved toggle, no search
 * on raise site … no way to view resolved groups at all."*
 *
 * Every test below was falsified against the pre-cycle-12 page: the
 * confirmation tests go red (the write fires on the first click), the filter
 * tests go red on `getByLabelText` (there were no inputs), and the correlation
 * test goes red because the blank cell carried no words.
 */
describe("ErrorMonitorPage resolve confirmation", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listErrorGroups: vi.fn().mockResolvedValue(LIST),
      getErrorGroup: vi.fn().mockResolvedValue(DETAIL),
      resolveErrorGroup: vi
        .fn()
        .mockResolvedValue({ ...OPEN_GROUP, resolved_at: "2026-08-15T10:00:00Z" }),
      unresolveErrorGroup: vi.fn().mockResolvedValue({ ...RESOLVED_GROUP, resolved_at: null }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("writes nothing on the first click and names the group in the confirmation", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);

    const row = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /resolve/i }));

    expect(api.resolveErrorGroup).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog");
    // The mistake a confirmation exists to catch is having the wrong row.
    expect(dialog).toHaveTextContent("ValueError — app.jobs.dispatch:142");
    // AND IT NO LONGER LIES ABOUT THE WAY BACK. This sentence used to read
    // "it cannot be reopened from here — the control plane serves no
    // unresolve route", which was true until `POST /platform/errors/{id}/
    // unresolve` shipped. A confirmation that overstates the consequence is
    // how operators learn to stop reading confirmations.
    expect(dialog).not.toHaveTextContent(/no unresolve route/i);
    expect(dialog).toHaveTextContent(/reopen/i);
  });

  it("resolves nothing when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);

    const row = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /resolve/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /cancel/i }));

    expect(api.resolveErrorGroup).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("announces the resolution instead of only changing a number", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);

    const row = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;
    // The live region exists BEFORE the change, empty: one injected at the
    // moment of the change is not reliably announced.
    expect(screen.getByRole("status")).toHaveTextContent("");

    await user.click(within(row).getByRole("button", { name: /resolve/i }));
    await confirmResolve(user);

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/Resolved ValueError at app\.jobs\.dispatch:142/);
    expect(status).toHaveTextContent(/still listed under the Resolved filter/i);
  });

});

/**
 * CYCLE 13 — THE INVERSE. `POST /platform/errors/{group_id}/unresolve` (commit
 * bd04957) is the route the cycle-12 page could not call: it shipped with a
 * confirmation on Resolve and a cell reading "this group cannot be reopened
 * from the console", because at that point it genuinely could not be.
 *
 * WHY NO CONFIRMATION HERE, asserted rather than assumed. Reopening removes
 * nothing: it puts a group back in the Open count and the Open filter, which
 * is where it was before somebody resolved it. `ConfirmDialog`'s own docstring
 * scopes it to "something that cannot be undone", and this control IS the undo
 * — one click back the other way, whose own confirmation stands in front of
 * it. A dialog here would be the second click that teaches people to click
 * through the first.
 */
describe("ErrorMonitorPage reopen", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listErrorGroups: vi.fn().mockResolvedValue(LIST),
      getErrorGroup: vi.fn().mockResolvedValue(DETAIL),
      resolveErrorGroup: vi
        .fn()
        .mockResolvedValue({ ...OPEN_GROUP, resolved_at: "2026-08-15T10:00:00Z" }),
      unresolveErrorGroup: vi.fn().mockResolvedValue({ ...RESOLVED_GROUP, resolved_at: null }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("offers Reopen on a resolved group and never on an open one", async () => {
    renderWithProviders(<ErrorMonitorPage />);

    const resolvedRow = (await screen.findByText("TimeoutError")).closest("tr") as HTMLElement;
    const openRow = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;

    expect(within(resolvedRow).getByRole("button", { name: /reopen/i })).toBeInTheDocument();
    // The route answers 200 on an already-open group, so this is not about
    // avoiding an error — it is about not offering an action with no effect.
    expect(within(openRow).queryByRole("button", { name: /reopen/i })).not.toBeInTheDocument();
  });

  it("calls the unresolve route with that group's id, and does not select the row", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);

    const row = (await screen.findByText("TimeoutError")).closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /reopen/i }));

    expect(api.unresolveErrorGroup).toHaveBeenCalledExactlyOnceWith(RESOLVED_GROUP.id);
    expect(api.resolveErrorGroup).not.toHaveBeenCalled();
    // Same trap the Resolve button has: a click that bubbles to the row also
    // fires the detail read.
    expect(api.getErrorGroup).not.toHaveBeenCalled();
  });

  it("writes on the first click — no confirmation stands in front of the undo", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);

    const row = (await screen.findByText("TimeoutError")).closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /reopen/i }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.unresolveErrorGroup).toHaveBeenCalledOnce();
  });

  it("shows the group as open again without a manual refresh", async () => {
    const user = userEvent.setup();
    const reopened: ErrorGroup = { ...RESOLVED_GROUP, resolved_at: null };
    (api.listErrorGroups as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(LIST)
      .mockResolvedValue({ groups: [OPEN_GROUP, reopened], dropped: 0 } satisfies ErrorGroupList);
    renderWithProviders(<ErrorMonitorPage />);

    const row = (await screen.findByText("TimeoutError")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Resolved")).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: /reopen/i }));

    // THE LIST, not the mutation's own response: the row must come back from
    // an invalidated query, which is what a page with a stale cache would
    // fail to do.
    const afterRow = await screen.findByText("TimeoutError");
    const cells = afterRow.closest("tr") as HTMLElement;
    expect(await within(cells).findByText("Open")).toBeInTheDocument();
    expect(within(cells).getByRole("button", { name: /resolve/i })).toBeInTheDocument();
  });

  it("announces the reopen rather than only moving a badge", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);

    const row = (await screen.findByText("TimeoutError")).closest("tr") as HTMLElement;
    expect(screen.getByRole("status")).toHaveTextContent("");

    await user.click(within(row).getByRole("button", { name: /reopen/i }));

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/Reopened TimeoutError at app\.gateway\.conn:88/);
  });

  it("routes a refused reopen through the console's error sentence, never the raw body", async () => {
    (api.unresolveErrorGroup as ReturnType<typeof vi.fn>).mockRejectedValue(
      new ApiError(403, '{"detail":{"code":"forbidden"}}'),
    );
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);

    const row = (await screen.findByText("TimeoutError")).closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /reopen/i }));

    expect(await screen.findByText(/You do not have permission to do that/i)).toBeInTheDocument();
    // The audit finding this closes was a Pydantic payload printed into the
    // UI as the explanation.
    expect(screen.queryByText(/forbidden/)).not.toBeInTheDocument();
  });

  it("draws neither triage control for an operator without platform.errors.write", async () => {
    (api.me as ReturnType<typeof vi.fn>).mockResolvedValue(READ_ONLY_ME);
    renderWithProviders(<ErrorMonitorPage />);

    const openRow = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;
    const resolvedRow = (await screen.findByText("TimeoutError")).closest("tr") as HTMLElement;

    // RBAC is server-side and this hides nothing from anybody. It stops a
    // reader being offered two buttons that can only answer 403.
    expect(within(openRow).queryByRole("button", { name: /resolve/i })).not.toBeInTheDocument();
    expect(within(resolvedRow).queryByRole("button", { name: /reopen/i })).not.toBeInTheDocument();
    // And says so in words, not as a bare em dash a screen reader skips.
    expect(within(openRow).getByText(/needs permission to manage platform errors/)).toBeInTheDocument();
    expect(within(openRow).queryByText(/platform\.errors\.write/)).not.toBeInTheDocument();
  });
});

describe("ErrorMonitorPage filters", () => {
  const RARE: ErrorGroup = {
    ...OPEN_GROUP,
    id: "55555555-5555-5555-5555-555555555555",
    exc_type: "IntegrityError",
    module: "app.api.v1.orgs",
    lineno: 12,
    qualname: "create_org",
    where: "api",
    first_seen: "2026-07-01T00:00:00Z",
    last_seen: "2026-08-01T00:00:00Z",
    event_count: 99,
  };
  const THREE: ErrorGroupList = { groups: [OPEN_GROUP, RESOLVED_GROUP, RARE], dropped: 0 };

  beforeEach(() => {
    mockUseApi.mockReturnValue({
      listErrorGroups: vi.fn().mockResolvedValue(THREE),
      getErrorGroup: vi.fn().mockResolvedValue(DETAIL),
      resolveErrorGroup: vi.fn().mockResolvedValue(OPEN_GROUP),
    } as unknown as ApiClient);
  });

  /** The type cell of each body row, in render order. Read from the DOM
   *  rather than by role: a group row carries `role="button"` so it can be
   *  selected from the keyboard, which takes its implicit `row` role away. */
  function typeCells(): string[] {
    return [...document.querySelectorAll("tbody tr")].map(
      (row) => row.querySelector("td")?.textContent ?? "",
    );
  }

  it("shows every group by default, resolved ones included", async () => {
    renderWithProviders(<ErrorMonitorPage />);
    await screen.findByText("ValueError");
    expect(typeCells()).toEqual(["ValueError", "TimeoutError", "IntegrityError"]);
  });

  it("filters to open groups only, and writes the choice into the URL", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);
    await screen.findByText("ValueError");

    await user.selectOptions(screen.getByLabelText("Filter by triage state"), "open");

    expect(screen.queryByText("TimeoutError")).not.toBeInTheDocument();
    expect(screen.getByTestId("search").textContent).toContain("state=open");
  });

  it("reaches the resolved groups the Open view hides", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);
    await screen.findByText("ValueError");

    await user.selectOptions(screen.getByLabelText("Filter by triage state"), "resolved");

    expect(await screen.findByText("TimeoutError")).toBeInTheDocument();
    expect(screen.queryByText("ValueError")).not.toBeInTheDocument();
  });

  it("restores the filters from the URL, so a triage view can be pasted into a ticket", async () => {
    renderWithProviders(<ErrorMonitorPage />, "/error-monitor?state=resolved");

    expect(await screen.findByText("TimeoutError")).toBeInTheDocument();
    expect(screen.queryByText("ValueError")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Filter by triage state")).toHaveValue("resolved");
  });

  it("filters by capture seam", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);
    await screen.findByText("ValueError");

    await user.selectOptions(screen.getByLabelText("Filter by capture seam"), "api");

    expect(typeCells()).toEqual(["IntegrityError"]);
  });

  it("searches the raise site, not only the exception type", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);
    await screen.findByText("ValueError");

    await user.type(screen.getByLabelText("Search error groups"), "dispatch:142");

    expect(typeCells()).toEqual(["ValueError"]);
    expect(screen.getByTestId("search").textContent).toContain("q=dispatch");
  });

  it("sorts by event count when asked, rather than always by last seen", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);
    await screen.findByText("ValueError");

    await user.selectOptions(screen.getByLabelText("Sort by"), "events");

    // 99 events, but the oldest last-seen: the two orderings disagree, which
    // is the only way this assertion means anything.
    expect(typeCells()).toEqual(["IntegrityError", "ValueError", "TimeoutError"]);
  });

  it("says the filters are what emptied the table, and offers a way out", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);
    await screen.findByText("ValueError");

    await user.type(screen.getByLabelText("Search error groups"), "nothing-matches-this");

    expect(await screen.findByText(/no error group matches these filters/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /clear filters/i }));
    expect(await screen.findByText("ValueError")).toBeInTheDocument();
    expect(screen.getByTestId("search").textContent).toBe("");
  });

  it("offers no Clear filters control when nothing is filtered", async () => {
    renderWithProviders(<ErrorMonitorPage />);
    await screen.findByText("ValueError");
    expect(screen.queryByRole("button", { name: /clear filters/i })).not.toBeInTheDocument();
  });

  it("ignores a filter value the URL invented rather than rendering an empty table", async () => {
    renderWithProviders(<ErrorMonitorPage />, "/error-monitor?state=banana&sort=colour");
    await screen.findByText("ValueError");
    expect(typeCells()).toEqual(["ValueError", "TimeoutError", "IntegrityError"]);
  });

  it("narrows to a time range, and keeps the range in the URL", async () => {
    const user = userEvent.setup();
    // TimeoutError last_seen 2026-08-02, IntegrityError 2026-08-01,
    // ValueError 2026-08-15: only the newest is inside a 24-hour window
    // measured from the 15th.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-08-15T18:00:00Z"));
    try {
      renderWithProviders(<ErrorMonitorPage />);
      await screen.findByText("ValueError");

      await user.selectOptions(screen.getByLabelText("Filter by last seen"), "1");

      expect(typeCells()).toEqual(["ValueError"]);
      expect(screen.getByTestId("search").textContent).toContain("since=1");
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a row whose last_seen cannot be read rather than dropping it from a range", () => {
    const undated: ErrorGroup = { ...OPEN_GROUP, id: "z", last_seen: "" };
    const kept = selectErrorGroups([undated], {
      query: "",
      state: "all",
      where: "",
      sort: "last_seen",
      since: "1",
      now: Date.parse("2026-08-15T18:00:00Z"),
    });
    expect(kept.map((group) => group.id)).toEqual(["z"]);
  });

  it("orders by instant, not by string, when sorting on last seen", () => {
    // A string compare on ISO-8601 happens to agree with an instant compare
    // for same-shaped timestamps, and disagrees the moment an offset appears.
    const utc: ErrorGroup = { ...OPEN_GROUP, id: "a", last_seen: "2026-08-15T09:00:00Z" };
    const offset: ErrorGroup = { ...OPEN_GROUP, id: "b", last_seen: "2026-08-15T11:00:00+05:00" };
    const sorted = selectErrorGroups([offset, utc], {
      query: "",
      state: "all",
      where: "",
      sort: "last_seen",
      since: "",
    });
    // 11:00+05:00 is 06:00Z -- three hours BEFORE the 09:00Z row.
    expect(sorted.map((group) => group.id)).toEqual(["a", "b"]);
  });
});

describe("ErrorMonitorPage correlation cells", () => {
  beforeEach(() => {
    mockUseApi.mockReturnValue({
      listErrorGroups: vi.fn().mockResolvedValue(LIST),
      getErrorGroup: vi.fn().mockResolvedValue(DETAIL),
      resolveErrorGroup: vi.fn().mockResolvedValue(OPEN_GROUP),
    } as unknown as ApiClient);
  });

  it("says an absent correlation id was not in scope, rather than leaving a bare dash", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ErrorMonitorPage />);

    await user.click((await screen.findByText("ValueError")).closest("tr") as HTMLElement);
    const eventRow = (await screen.findByText("req-abc-999")).closest("tr") as HTMLElement;

    expect(
      within(eventRow).getByText(/No organization was in scope when this occurrence was captured/i),
    ).toBeInTheDocument();
    expect(
      within(eventRow).getByText(/No device was in scope when this occurrence was captured/i),
    ).toBeInTheDocument();
    // The dash stays as the visual token; it is the sentence beside it that
    // is new.
    expect(within(eventRow).getAllByText("—")).toHaveLength(3);
  });
});

describe("ErrorMonitorPage stat tiles", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      listErrorGroups: vi.fn().mockResolvedValue(LIST),
      getErrorGroup: vi.fn().mockResolvedValue(DETAIL),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  /* ------------------------------------------ no dead tiles (visual cycle) */

  /**
   * "All tiles and all numbers or KPIs need to be clickable and direct the
   * user to the specific menus, i dont want dead tiles everywhere" — the
   * operator, verbatim. `StatCard` now requires `to` so the compiler catches
   * an omitted destination; what the compiler cannot catch is a destination
   * that goes nowhere useful, which is what these assert.
   */
  it("renders every stat tile as a link with a real destination", async () => {
    renderWithProviders(<ErrorMonitorPage />);
    await screen.findByText("ValueError");

    const tiles = [...document.querySelectorAll(".stat-card")];
    expect(tiles).toHaveLength(3);

    for (const tile of tiles) {
      // AN ANCHOR, not a div with onClick: keyboard reachable, middle-
      // clickable into a new tab, and pasteable into an incident channel,
      // which is what somebody does with "67 open groups" at 2am.
      expect(tile.tagName).toBe("A");
      const href = tile.getAttribute("href");
      expect(href).toBeTruthy();
      expect(href).not.toBe("#");
    }

    // THE EXACT DESTINATIONS, PINNED. A tile that quietly loses its filter --
    // "12 open" opening the unfiltered table of 400 -- is invisible to a test
    // that only asserts an href is present, and it is the specific way this
    // decays.
    expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([
      "/error-monitor",
      "/error-monitor?state=open",
      "/error-monitor?sort=events",
    ]);
  });

  it("the Open tile's URL is one this page actually reads", async () => {
    // NON-VACUITY for the test above. `/error-monitor?state=open` being the
    // href proves nothing on its own; this proves the page honours it, which
    // is the difference between a link and a link-shaped decoration.
    renderWithProviders(<ErrorMonitorPage />, "/error-monitor?state=open");

    expect(await screen.findByText("ValueError")).toBeInTheDocument();
    expect(screen.queryByText("TimeoutError")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Filter by triage state")).toHaveValue("open");
  });

  it("labels its zeroes so an empty page cannot read as a clean one", async () => {
    (api.listErrorGroups as ReturnType<typeof vi.fn>).mockResolvedValue({
      groups: [],
      dropped: 0,
    } satisfies ErrorGroupList);
    renderWithProviders(<ErrorMonitorPage />);

    // "0 open" and "nothing has been captured" are the same pixel without
    // this, and the audit found nine such tiles on one page.
    expect(await screen.findByText("Nothing has been captured")).toBeInTheDocument();
    expect(screen.getByText("Every group is resolved")).toBeInTheDocument();
    // AND the zero is not wearing the alarm treatment: `urgent` is dropped on
    // a zero value, so "0 open" cannot read as an emergency.
    // Scoped to `.stat-label`: "Open" is also an <option> in the triage-state
    // select, so a bare getByText matches two nodes.
    const label = [...document.querySelectorAll(".stat-label")].find(
      (node) => node.textContent === "Open",
    ) as HTMLElement;
    expect(label).toBeDefined();
    expect((label.closest(".stat-card") as HTMLElement).className).not.toContain("urgent");
  });

  it("does give the Open tile weight when there is something to work", async () => {
    // NON-VACUITY for the assertion above: `not.toContain("urgent")` is
    // trivially true on a tile that is never urgent, and a row where nothing
    // can ever carry weight is the flat wall of numbers this replaces.
    renderWithProviders(<ErrorMonitorPage />);
    await screen.findByText("ValueError");

    const label = [...document.querySelectorAll(".stat-label")].find(
      (node) => node.textContent === "Open",
    ) as HTMLElement;
    const card = label.closest(".stat-card") as HTMLElement;
    expect(card.className).toContain("urgent");
    // Colour is category and it is the failure hue, not merely "some colour".
    expect(card.className).toContain("tone-red");
    // And the other two are deliberately quiet.
    expect(document.querySelectorAll(".stat-card.urgent")).toHaveLength(1);
  });
});
