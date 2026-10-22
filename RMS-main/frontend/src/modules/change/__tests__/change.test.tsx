import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me } from "../../../lib/api";
import { ApprovalQueue } from "../ApprovalQueue";
import { ChangeCalendar } from "../ChangeCalendar";
import { availableChangeActions } from "../changeActions";
import { ChangeDetail } from "../ChangeDetail";
import { deploymentPickerOptions, provisionRunPickerOptions } from "../pickerOptions";
import { changeStateVariant } from "../changeState";
import { ChangeRequestForm } from "../ChangeRequestForm";
import { ChangesPage } from "../ChangesPage";
import type { ChangeRequest } from "../types";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch. Change data is plain TanStack Query state (no live-event
// stream) — mirrors modules/provisioning/__tests__/provisioning.test.tsx.
const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWithProviders(ui: ReactElement, initialEntries: string[] = ["/"]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["change.read", "change.request", "change.approve", "change.manage"],
};

const CHANGES: ChangeRequest[] = [
  {
    id: "change1",
    type: "standard",
    title: "Upgrade switch firmware",
    description: "Roll out firmware 2.1 to core switches.",
    risk: "medium",
    affected: [],
    window_start: "2026-02-01T00:00:00.000Z",
    window_end: "2026-02-01T04:00:00.000Z",
    rollback_plan: "Revert to firmware 2.0 image.",
    state: "draft",
    created_by: "u2",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const SUBMITTED_CHANGE: ChangeRequest = {
  ...(CHANGES[0] as ChangeRequest),
  id: "change2",
  title: "Patch prod database",
  state: "submitted",
};

// UX remediation cycle 5. The profile zone (`/me`'s `timezone`, cycle 2) is
// what every instant in this module is rendered in, and it is deliberately
// NOT the zone the test runner happens to sit in -- an assertion that passes
// in UTC and in New York alike is not testing the zone at all.
//
// America/New_York is UTC-5 in January and February, which is the operator's
// offset in the finding this cycle exists to close: "every datetime in the
// product is displayed 5 hours earlier than it was entered."
const ME_NY: Me = { ...ME, timezone: "America/New_York" };

describe("ChangesPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listChanges: vi.fn().mockResolvedValue(CHANGES),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("lists changes from the mocked api", async () => {
    renderWithProviders(<ChangesPage />);

    expect(await screen.findByText("Upgrade switch firmware")).toBeInTheDocument();
    // Read as a word, not as the wire's identifier (cycle 12).
    expect(screen.getByText("Draft")).toBeInTheDocument();
    expect(screen.queryByText("draft")).not.toBeInTheDocument();
  });

  // The approval queue is a real route (App.tsx: /changes/approvals) that no
  // shell nav entry points at, which is the mechanism behind Auditor B's
  // BLOCKER: "the approval workflow — the entire reason a change-management
  // module exists — cannot be exercised."
  it("points at the approval queue and the calendar from the module's own page", async () => {
    renderWithProviders(<ChangesPage />);

    expect(await screen.findByRole("link", { name: "Approval queue" })).toHaveAttribute(
      "href",
      "/changes/approvals",
    );
    expect(screen.getByRole("link", { name: "Change calendar" })).toHaveAttribute(
      "href",
      "/changes/calendar",
    );
  });

  it("hides the New change control when the caller lacks change.request", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "change.request"),
    });
    renderWithProviders(<ChangesPage />);

    await screen.findByText("Upgrade switch firmware");
    expect(screen.queryByRole("button", { name: "New change" })).not.toBeInTheDocument();
  });
});

/**
 * THE TILES ARE LINKS, AND EACH ONE OPENS THE ROWS IT COUNTED.
 *
 * "All tiles and all numbers or kpis need to be clickable and direct the user
 * to the specific menus, i dont want dead tiles everywhere" — the operator,
 * 2026-08-21. `StatCard` now requires `to`, so a tile with no destination is a
 * compile error; what the compiler CANNOT check is whether the destination is
 * the right one, or whether it still shows the rows behind the number. That is
 * what this block is for, and it is why the second test opens the URL the tile
 * points at and reads the table rather than only reading the href.
 */
describe("the KPI tiles are links to the rows behind them", () => {
  const COMPLETED: ChangeRequest = {
    ...(CHANGES[0] as ChangeRequest),
    id: "change3",
    title: "Retire old firewall",
    state: "completed",
  };
  const EMERGENCY: ChangeRequest = {
    ...(CHANGES[0] as ChangeRequest),
    id: "change4",
    title: "Restart failed cluster node",
    type: "emergency",
    state: "implementing",
  };

  /** label -> href, which also pins the `.stat-label` class the tile now uses. */
  function tileHrefs(): Record<string, string | null> {
    return Object.fromEntries(
      [...document.querySelectorAll("a.stat-card")].map((card) => [
        card.querySelector(".stat-label")?.textContent ?? "",
        card.getAttribute("href"),
      ]),
    );
  }

  beforeEach(() => {
    const api: Partial<ApiClient> = {
      me: vi.fn().mockResolvedValue(ME),
      listChanges: vi.fn().mockResolvedValue([CHANGES[0], SUBMITTED_CHANGE, COMPLETED, EMERGENCY]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("gives every tile a real href — none is a dead tile", async () => {
    renderWithProviders(<ChangesPage />, ["/changes"]);
    await screen.findByText("Upgrade switch firmware");

    // Every destination is a route App.tsx declares: /changes, /changes/approvals.
    expect(tileHrefs()).toEqual({
      "Open changes": "/changes?state=open",
      "Awaiting approval": "/changes/approvals",
      "Emergency changes": "/changes?type=emergency",
    });
  });

  it("shows exactly the rows its number counted when that destination is opened", async () => {
    // Three of the four changes are open; the completed one is not. A tile
    // whose href merely LOOKED right would pass the test above and fail here.
    renderWithProviders(<ChangesPage />, ["/changes?state=open"]);

    await screen.findByText("Upgrade switch firmware");
    expect(screen.getByText("Patch prod database")).toBeInTheDocument();
    expect(screen.getByText("Restart failed cluster node")).toBeInTheDocument();
    expect(screen.queryByText("Retire old firewall")).not.toBeInTheDocument();
    // And the way back out is on screen, so a narrowed list does not read as a
    // list that lost its rows.
    expect(screen.getByRole("link", { name: "Show all changes" })).toHaveAttribute(
      "href",
      "/changes",
    );
  });

  it("shows only the emergency changes at the emergency tile's destination", async () => {
    renderWithProviders(<ChangesPage />, ["/changes?type=emergency"]);

    expect(await screen.findByText("Restart failed cluster node")).toBeInTheDocument();
    expect(screen.queryByText("Upgrade switch firmware")).not.toBeInTheDocument();
  });

  it("says WHICH zero a zero is, and never prints one before the list has answered", async () => {
    const api: Partial<ApiClient> = {
      me: vi.fn().mockResolvedValue(ME),
      listChanges: vi.fn().mockResolvedValue([]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ChangesPage />, ["/changes"]);

    // A measured zero, named: an unmeasured one would read identically.
    expect(await screen.findByText("Nothing is in flight")).toBeInTheDocument();
    expect(screen.getByText("Nobody is waiting on a decision")).toBeInTheDocument();
    // `urgent` is dropped on a zero, so nothing on an empty page is shouting.
    expect(document.querySelectorAll("a.stat-card.urgent")).toHaveLength(0);
  });

  it("prints an em dash, not 0, while the list has not answered", async () => {
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listChanges: vi.fn().mockReturnValue(new Promise<never>(() => {})),
    } as unknown as ApiClient);
    renderWithProviders(<ChangesPage />, ["/changes"]);

    await screen.findByText("Loading…");
    const values = [...document.querySelectorAll("a.stat-card .stat-value")].map(
      (value) => value.textContent,
    );
    expect(values).toEqual(["—", "—", "—"]);
  });
});

describe("ApprovalQueue", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listChanges: vi.fn().mockResolvedValue([SUBMITTED_CHANGE]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("shows the approve control when the caller has change.approve", async () => {
    renderWithProviders(<ApprovalQueue />);

    await screen.findByText("Patch prod database");
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("hides the approve control when the caller lacks change.approve", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "change.approve"),
    });
    renderWithProviders(<ApprovalQueue />);

    await screen.findByText("Patch prod database");
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });
});

describe("change state vocabulary", () => {
  it("maps a real backend change_request state to a non-fallback badge variant", () => {
    // change_request.state vocabulary is sourced from the backend
    // (backend/app/modules/change_mgmt/state.py next_state) — "implementing"
    // is a real, non-terminal state.
    expect(changeStateVariant("implementing")).not.toBe("muted");
  });

  it("falls back to muted for an unknown/invented state name", () => {
    // Guards against the Phase-5 regression: no invented state name may
    // resolve to a real variant.
    expect(changeStateVariant("not_a_real_state")).toBe("muted");
  });
});

// --------------------------------------------------------------------------
// The form kernel on /changes — the two silent no-ops Auditor B counted here
// --------------------------------------------------------------------------

describe("ChangeRequestForm", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      createChange: vi.fn().mockResolvedValue(CHANGES[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderNew() {
    return renderWithProviders(<ChangeRequestForm change="new" onClose={() => {}} />);
  }

  it("names the empty fields on submit instead of doing nothing", async () => {
    // AUDIT (HIGH): "Clicking Save on the completely empty form is a silent
    // no-op — the form stays open, `[role=alert]` count is 0." The control read
    // `disabled={mutation.isPending || !title || !description}`, so the click
    // landed on a dead button that named neither field. Save stays enabled and
    // answers.
    const user = userEvent.setup();
    renderNew();

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Title is required");
    expect(alert).toHaveTextContent("Description is required");
    expect(api.createChange).not.toHaveBeenCalled();
  });

  it("treats a whitespace-only title as empty rather than sending it", async () => {
    // `" "` is truthy, so the old disabled expression let a space through to a
    // server that then 422'd — the same silence one round trip later.
    const user = userEvent.setup();
    renderNew();

    await user.type(screen.getByLabelText("Title *"), "   ");
    await user.type(screen.getByLabelText("Description *"), "Rolling firmware forward.");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Title is required");
    expect(api.createChange).not.toHaveBeenCalled();
  });

  it("submits on Enter, because the controls are inside a real form", async () => {
    // There were six <form> elements in the entire product and none here, so
    // Enter in the Title field did nothing at all.
    const user = userEvent.setup();
    renderNew();

    await user.type(screen.getByLabelText("Description *"), "Roll out firmware 2.1.");
    await user.type(screen.getByLabelText("Title *"), "Upgrade switch firmware{Enter}");

    await waitFor(() =>
      expect(api.createChange).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Upgrade switch firmware",
          description: "Roll out firmware 2.1.",
        }),
      ),
    );
  });

  it("marks the two fields the server insists on, and only those", async () => {
    // AUDIT (MEDIUM): "All seven controls report `required === false`." Type and
    // Risk always hold a value and the window fields are nullable in
    // change_mgmt/schema.py, so marking them would be the same lie inverted.
    renderNew();

    expect(screen.getByLabelText("Title *")).toBeRequired();
    expect(screen.getByLabelText("Description *")).toBeRequired();
    expect(screen.getByLabelText("Window start")).not.toBeRequired();
    expect(screen.getByLabelText("Window end")).not.toBeRequired();
  });

  it("says what the server refused, even when the refusal carries no body", async () => {
    // A bodyless 503 is `new ApiError(503, "")`, and every call site rendering
    // `{error ? <span>{String(error)}</span> : null}` showed the user an object
    // or nothing at all. `describeSubmitError` is driven off the status first.
    const user = userEvent.setup();
    api.createChange = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderNew();

    await user.type(screen.getByLabelText("Title *"), "Upgrade switch firmware");
    await user.type(screen.getByLabelText("Description *"), "Roll out firmware 2.1.");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That service is unavailable right now",
    );
  });

  it("does not save when Cancel is pressed inside the form", async () => {
    // A <button> with no `type` inside a <form> IS a submit button. Cancel
    // carries `type="button"` for exactly that reason.
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderWithProviders(<ChangeRequestForm change="new" onClose={onClose} />);

    await user.type(screen.getByLabelText("Title *"), "Upgrade switch firmware");
    await user.type(screen.getByLabelText("Description *"), "Roll out firmware 2.1.");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onClose).toHaveBeenCalled();
    expect(api.createChange).not.toHaveBeenCalled();
  });
});

describe("ChangeDetail link action", () => {
  let api: Partial<ApiClient>;

  const CHANGE_ID = "06a85c19-55e2-7f3c-8000-831c7bdba357";
  const ACTION_ID = "1f2e3d4c-5b6a-4798-8899-aabbccddeeff";

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      getChange: vi.fn().mockResolvedValue({ ...(CHANGES[0] as ChangeRequest), id: CHANGE_ID }),
      listChangeLinks: vi.fn().mockResolvedValue([]),
      listChangeApprovals: vi.fn().mockResolvedValue([]),
      addChangeLink: vi.fn().mockResolvedValue({
        id: "link1",
        action_type: "patch_run",
        action_id: ACTION_ID,
        linked_at: "2026-01-01T00:00:00.000Z",
        linked_by: "u1",
      }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/changes/:changeId" element={<ChangeDetail />} />
      </Routes>,
      [`/changes/${CHANGE_ID}`],
    );
  }

  it("names the empty Action ID on submit instead of doing nothing", async () => {
    // AUDIT (HIGH): "Clicking 'Link' with the field empty does nothing at all —
    // no error, table still reads 'No linked actions.'" The control read
    // `disabled={addLink.isPending || !actionId}`; it now stays enabled and the
    // refusal is what the user reads.
    const user = userEvent.setup();
    renderDetail();

    const link = await screen.findByRole("button", { name: "Link" });
    expect(link).toBeEnabled();
    await user.click(link);

    expect(await screen.findByRole("alert")).toHaveTextContent("Action ID is required");
    expect(api.addChangeLink).not.toHaveBeenCalled();
  });

  it("rejects a hand-typed id that is not a UUID before the round trip", async () => {
    // `action_id` is a `UUID` in change_mgmt/schema.py, and the auditor's own
    // complaint is that this id must be transcribed by hand — a transcription
    // that lost a character used to be discovered only as a 422.
    const user = userEvent.setup();
    renderDetail();

    await user.type(await screen.findByLabelText("Action ID *"), "patch-run-7");
    await user.click(screen.getByRole("button", { name: "Link" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("must be a UUID");
    expect(api.addChangeLink).not.toHaveBeenCalled();
  });

  it("links on Enter, because the controls are inside a real form", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.type(await screen.findByLabelText("Action ID *"), `${ACTION_ID}{Enter}`);

    await waitFor(() =>
      expect(api.addChangeLink).toHaveBeenCalledWith(CHANGE_ID, {
        action_type: "patch_run",
        action_id: ACTION_ID,
      }),
    );
  });

  it("says what the server refused, even when the refusal carries no body", async () => {
    // The old `{String(addLink.error)}` line rendered "[object Object]" at best
    // and, for a bodyless 503, an empty string that read as success.
    const user = userEvent.setup();
    api.addChangeLink = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    await user.type(await screen.findByLabelText("Action ID *"), ACTION_ID);
    await user.click(screen.getByRole("button", { name: "Link" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That service is unavailable right now",
    );
  });
});

// --------------------------------------------------------------------------
// UX remediation cycles 4 and 5, in this module
// --------------------------------------------------------------------------

describe("change instants are rendered in the reader's profile zone", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NY),
      listChanges: vi.fn().mockResolvedValue(CHANGES),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // The window below runs 00:00–04:00 UTC on 1 February, which in the
  // operator's zone is 19:00–23:00 on 31 JANUARY. The old
  // `new Date(x).toLocaleString()` rendered the runner's zone in the runner's
  // locale, which is how "the same device reports two different last-contact
  // times in two different date formats" happened.
  it("renders a maintenance window in the profile zone, not the browser's", async () => {
    renderWithProviders(<ChangesPage />);

    await screen.findByText("Upgrade switch firmware");
    expect(screen.getByText(/31 Jan 2026, 19:00/)).toBeInTheDocument();
    expect(screen.getByText(/31 Jan 2026, 23:00/)).toBeInTheDocument();
    expect(screen.queryByText(/2\/1\/2026/)).not.toBeInTheDocument();
  });

  it("renders an absent window as an em dash, never Invalid Date", async () => {
    api.listChanges = vi.fn().mockResolvedValue([
      { ...(CHANGES[0] as ChangeRequest), window_start: null, window_end: null },
    ]);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<ChangesPage />);

    await screen.findByText("Upgrade switch firmware");
    expect(screen.getByText(/—\s+–\s+—/)).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
  });
});

describe("change datetime values are qualified before they are sent", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NY),
      createChange: vi.fn().mockResolvedValue(CHANGES[0]),
      getChangeCalendar: vi.fn().mockResolvedValue({ entries: [], conflicts: [] }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // THE FIVE-HOUR BUG, on the field where it costs the most. A maintenance
  // window typed as 22:00–23:00 and sent unqualified is stored as 22:00Z --
  // 17:00 in the operator's zone, the middle of the working day the window
  // exists to avoid.
  it("sends a maintenance window as instants, not bare wall clocks", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ChangeRequestForm change="new" onClose={() => {}} />);

    await user.type(screen.getByLabelText("Title *"), "Upgrade switch firmware");
    await user.type(screen.getByLabelText("Description *"), "Roll out firmware 2.1.");
    fireEvent.change(screen.getByLabelText("Window start"), {
      target: { value: "2026-02-01T22:00" },
    });
    fireEvent.change(screen.getByLabelText("Window end"), {
      target: { value: "2026-02-01T23:00" },
    });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(api.createChange).toHaveBeenCalledWith(
        expect.objectContaining({
          window_start: "2026-02-02T03:00:00.000Z",
          window_end: "2026-02-02T04:00:00.000Z",
        }),
      ),
    );
  });

  // Both window fields are `datetime | None` server-side. An untouched field
  // must stay null rather than becoming the epoch.
  it("leaves an untouched window null rather than sending the epoch", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ChangeRequestForm change="new" onClose={() => {}} />);

    await user.type(screen.getByLabelText("Title *"), "Upgrade switch firmware");
    await user.type(screen.getByLabelText("Description *"), "Roll out firmware 2.1.");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(api.createChange).toHaveBeenCalledWith(
        expect.objectContaining({ window_start: null, window_end: null }),
      ),
    );
  });

  // `new Date("2026-02-01").toISOString()` reads a `type="date"` value as UTC
  // midnight, so an operator in New York asking for February got a window
  // that opened at 19:00 on 31 January -- and the conflict detection this
  // page exists for ran over the wrong range.
  it("asks for the calendar range in the reader's zone, not UTC", async () => {
    renderWithProviders(<ChangeCalendar />);

    fireEvent.change(await screen.findByLabelText("From"), {
      target: { value: "2026-02-01" },
    });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-02-28" } });

    await waitFor(() =>
      expect(api.getChangeCalendar).toHaveBeenCalledWith(
        "2026-02-01T05:00:00.000Z",
        "2026-02-28T05:00:00.000Z",
      ),
    );
  });

  // DESIGN REVIEW M1. /settings says "Day/month/year (08/09/2026)" and every
  // rendered date in the console says "22 Sept 2026", but a native date
  // input draws the USER AGENT'S mask -- `09/22/2026` under en-US -- and no
  // CSS reorders it. The control stays, because it is the one date widget
  // every keyboard, screen reader and locale already agrees on; what the page
  // owes the reader is the window spelled out in the console's own format,
  // which is the echo `agent-push/PushAuthorizationsPage` already carried and
  // no other page did.
  it("spells the window back in the console's own date format, not the browser's mask", async () => {
    renderWithProviders(<ChangeCalendar />);

    fireEvent.change(await screen.findByLabelText("From"), {
      target: { value: "2026-02-01" },
    });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-11-27" } });

    // "27 Nov" and not "11/27" -- the one reading of `11/27/2026` that cannot
    // be confused with 11 December.
    expect(await screen.findByText(/From 01 Feb 2026 to 27 Nov 2026/)).toBeInTheDocument();
  });

  // THE ECHO IS A DESCRIPTION, NOT A NAME. `Field` wraps its control in the
  // `<label>`, so an echo placed inside one becomes part of the field's label
  // and "From" stops naming the From input. This is the guard that caught it.
  it("leaves the range fields named From and To once the echo is on screen", async () => {
    renderWithProviders(<ChangeCalendar />);

    fireEvent.change(await screen.findByLabelText("From"), {
      target: { value: "2026-02-01" },
    });

    await screen.findByText(/From 01 Feb 2026/);
    expect(screen.getByLabelText("From")).toHaveValue("2026-02-01");
    expect(screen.getByLabelText("To")).toBeInTheDocument();
  });

  // DESIGN REVIEW M10 (the half that is a defect rather than a feature
  // decision). These two controls sat directly on the page background between
  // the header and the card, a surface nothing else in the console uses:
  // /bcp/report, /budget/expenses, /security and /ipam all keep their filters
  // inside a panel.
  it("keeps the range controls inside a panel rather than loose on the page", async () => {
    renderWithProviders(<ChangeCalendar />);

    const from = await screen.findByLabelText("From");
    const panel = from.closest(".panel");
    expect(panel).not.toBeNull();
    expect(panel).toContainElement(screen.getByLabelText("To"));
    // And the panel says what it is, the way /bcp/report's does.
    expect(panel?.querySelector(".panel-title")).toHaveTextContent("Period");
  });
});

describe("change failures reach the user as a sentence", () => {
  let api: Partial<ApiClient>;

  const CHANGE_ID = "06a85c19-55e2-7f3c-8000-831c7bdba357";

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NY),
      listChanges: vi.fn().mockResolvedValue([SUBMITTED_CHANGE]),
      approveChange: vi.fn().mockRejectedValue(new ApiError(503, "")),
      getChange: vi.fn().mockResolvedValue({ ...(CHANGES[0] as ChangeRequest), id: CHANGE_ID }),
      listChangeApprovals: vi.fn().mockResolvedValue([]),
      listChangeLinks: vi.fn().mockResolvedValue([
        {
          id: "link1",
          action_type: "patch_run",
          action_id: "1f2e3d4c-5b6a-4798-8899-aabbccddeeff",
          linked_at: "2026-01-01T00:00:00.000Z",
          linked_by: "u1",
        },
      ]),
      removeChangeLink: vi.fn().mockRejectedValue(new ApiError(403, "")),
      getChangeEvidence: vi.fn().mockRejectedValue(new ApiError(503, "")),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/changes/:changeId" element={<ChangeDetail />} />
      </Routes>,
      [`/changes/${CHANGE_ID}`],
    );
  }

  // `{String(approve.error ?? reject.error)}` rendered "[object Object]", and
  // for the bodyless 503 a proxy returns it rendered an EMPTY STRING: the
  // approval never landed and the queue said nothing.
  it("says what the server refused when an approval fails", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ApprovalQueue />);

    await screen.findByText("Patch prod database");
    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That service is unavailable right now",
    );
    expect(screen.queryByText(/object Object/)).not.toBeInTheDocument();
  });

  // SWALLOWED ENTIRELY before this cycle: `removeLink` had no `onError` and no
  // error surface anywhere on the page, so a refused unlink left the row in
  // place with no word about why -- the audit trail for this change read as
  // one link longer than the operator believed.
  it("announces a refused link removal that used to be swallowed", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Remove" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You do not have permission to do that.",
    );
  });

  // The evidence export is a `try/catch` around a fetch, and `String(err)`
  // produced "Error:" with nothing after it for a bodyless 503 -- the export
  // failed and the page showed a blank line where the reason should be.
  it("says why an evidence export failed instead of printing the object", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Export evidence" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That service is unavailable right now",
    );
    expect(screen.queryByText(/^Error:/)).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// UX remediation cycle 7 — the BLOCKER: "a change request can be created but
// can never leave `draft`"
// --------------------------------------------------------------------------

describe("availableChangeActions mirrors the backend state machine", () => {
  // Every case below is one branch of
  // backend/app/modules/change_mgmt/state.py#next_state. A transition offered
  // here that next_state refuses is a button that 400s, which is the finding
  // again wearing a nicer hat.
  function names(state: string, type: string) {
    return availableChangeActions(state, type).map((spec) => spec.action);
  }

  it("offers submit and cancel from draft", () => {
    expect(names("draft", "standard")).toEqual(["submit", "cancel"]);
  });

  it("offers the decision pair from submitted for a standard change", () => {
    expect(names("submitted", "standard")).toEqual(["approve", "reject", "cancel"]);
    expect(names("submitted", "normal")).toEqual(["approve", "reject", "cancel"]);
  });

  it("offers the emergency fast path from submitted, and no decision", () => {
    // state.py: `begin` from "submitted" is emergency-only, and approve/reject
    // on an emergency change are legal only once it is `implementing`.
    expect(names("submitted", "emergency")).toEqual(["begin", "cancel"]);
  });

  it("offers begin from approved", () => {
    expect(names("approved", "standard")).toEqual(["begin", "cancel"]);
  });

  it("offers only completion while implementing a standard change", () => {
    // There is no ("implementing","cancel") row in _SIMPLE_TRANSITIONS.
    expect(names("implementing", "standard")).toEqual(["complete"]);
  });

  it("offers the retrospective decision while implementing an emergency change", () => {
    expect(names("implementing", "emergency")).toEqual(["approve", "reject", "complete"]);
  });

  it("offers revise from rejected", () => {
    expect(names("rejected", "standard")).toEqual(["revise"]);
  });

  it("offers nothing from the two terminal states", () => {
    expect(names("completed", "standard")).toEqual([]);
    expect(names("cancelled", "standard")).toEqual([]);
  });

  it("puts a confirmation on the decisions and the endings, and not on submit", () => {
    // Cycle 8's own rule: confirming all seven trains people to click through
    // the dialog, which is worse than confirming none.
    const confirmed = (state: string, type: string, action: string) =>
      availableChangeActions(state, type).find((spec) => spec.action === action)?.confirm != null;

    expect(confirmed("draft", "standard", "submit")).toBe(false);
    expect(confirmed("submitted", "standard", "approve")).toBe(true);
    expect(confirmed("submitted", "standard", "reject")).toBe(true);
    expect(confirmed("draft", "standard", "cancel")).toBe(true);
    expect(confirmed("implementing", "standard", "complete")).toBe(true);
  });

  it("styles only the endings as destructive", () => {
    const cancel = availableChangeActions("draft", "standard").find((s) => s.action === "cancel");
    const submit = availableChangeActions("draft", "standard").find((s) => s.action === "submit");
    expect(cancel?.confirm?.destructive).toBe(true);
    expect(submit?.confirm).toBeNull();
  });
});

describe("ChangeDetail lifecycle controls", () => {
  let api: Partial<ApiClient>;

  const CHANGE_ID = "06a85c19-55e2-7f3c-8000-831c7bdba357";

  function changeIn(state: string, extra: Partial<ChangeRequest> = {}): ChangeRequest {
    return { ...(CHANGES[0] as ChangeRequest), id: CHANGE_ID, state, ...extra };
  }

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      getChange: vi.fn().mockResolvedValue(changeIn("draft")),
      listChangeLinks: vi.fn().mockResolvedValue([]),
      listChangeApprovals: vi.fn().mockResolvedValue([]),
      submitChange: vi.fn().mockResolvedValue(changeIn("submitted")),
      approveChange: vi.fn().mockResolvedValue({
        id: "a1",
        approver_id: "u1",
        decision: "approved",
        comment: null,
        is_retrospective: false,
        decided_at: "2026-01-02T00:00:00.000Z",
      }),
      rejectChange: vi.fn().mockResolvedValue({
        id: "a2",
        approver_id: "u1",
        decision: "rejected",
        comment: null,
        is_retrospective: false,
        decided_at: "2026-01-02T00:00:00.000Z",
      }),
      transitionChange: vi.fn().mockResolvedValue(changeIn("cancelled")),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/changes/:changeId" element={<ChangeDetail />} />
      </Routes>,
      [`/changes/${CHANGE_ID}`],
    );
  }

  // THE BLOCKER. Auditor B: "An exhaustive enumeration of every interactive
  // element on the page returns exactly four controls: Export evidence, a
  // select of action types, a text input, and Link. There is no Submit for
  // approval, Approve, Reject, Schedule, Implement, Close or Cancel control."
  it("submits a draft change for approval", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Submit for approval" }));

    await waitFor(() => expect(api.submitChange).toHaveBeenCalledWith(CHANGE_ID));
  });

  it("asks before approving, and does not approve until the dialog is confirmed", async () => {
    // Cycle 8. The decision is written to the immutable audit log against the
    // approver's account and cannot be withdrawn, so the click that fires it
    // is not allowed to be the only click.
    const user = userEvent.setup();
    api.getChange = vi.fn().mockResolvedValue(changeIn("submitted"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(api.approveChange).not.toHaveBeenCalled();

    // The subject is the load-bearing part of the dialog: it is what lets
    // somebody notice they have the wrong change open.
    const dialog = await screen.findByRole("dialog", { name: "Approve this change" });
    expect(dialog).toHaveTextContent("Upgrade switch firmware");

    await user.click(screen.getByRole("button", { name: "Approve change" }));

    await waitFor(() =>
      expect(api.approveChange).toHaveBeenCalledWith(CHANGE_ID, { comment: null }),
    );
  });

  it("carries the decision comment into the approval", async () => {
    const user = userEvent.setup();
    api.getChange = vi.fn().mockResolvedValue(changeIn("submitted"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    await user.type(
      await screen.findByLabelText("Decision comment (optional)"),
      "Reviewed the rollback plan.",
    );
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.click(await screen.findByRole("button", { name: "Approve change" }));

    await waitFor(() =>
      expect(api.approveChange).toHaveBeenCalledWith(CHANGE_ID, {
        comment: "Reviewed the rollback plan.",
      }),
    );
  });

  it("abandons the transition when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Cancel change" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(api.transitionChange).not.toHaveBeenCalled();
  });

  it("cancels a change through the transition route once confirmed", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Cancel change" }));
    // Two controls read "Cancel change" once the dialog is open — the button
    // that opened it and the dialog's own confirm. The dialog's is inside the
    // `role="dialog"`, so scope the query rather than guessing at order.
    const dialog = await screen.findByRole("dialog", { name: "Cancel this change" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel change" }));

    await waitFor(() =>
      expect(api.transitionChange).toHaveBeenCalledWith(CHANGE_ID, { action: "cancel" }),
    );
  });

  // Auditor B (MEDIUM): "Within <main> there is no 'back to changes'
  // affordance; the only route back is the global header breadcrumb, which
  // sits outside the content area."
  it("offers a way back to the change list from inside the page", async () => {
    renderDetail();

    expect(await screen.findByRole("link", { name: /All changes/ })).toHaveAttribute(
      "href",
      "/changes",
    );
  });

  it("says why a completed change offers no transitions", async () => {
    api.getChange = vi.fn().mockResolvedValue(changeIn("completed"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    expect(await screen.findByText(/Completed changes are closed for good/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark complete" })).not.toBeInTheDocument();
  });

  it("says why the decision controls are absent for a caller without change.approve", async () => {
    api.getChange = vi.fn().mockResolvedValue(changeIn("submitted"));
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "change.approve"),
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    expect(await screen.findByText(/does not hold change.approve/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("refuses to let the requester decide their own change, and says so", async () => {
    // state.py checks `actor_id == created_by` before any state-specific logic
    // for both approve and reject. The server is authoritative; this is the
    // half that stops the operator learning it from a 400.
    api.getChange = vi.fn().mockResolvedValue(changeIn("submitted", { created_by: ME.user_id }));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    expect(await screen.findByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(screen.getByText(/You raised this change, so you cannot decide it/)).toBeInTheDocument();
  });

  it("says what the server refused when a transition fails", async () => {
    const user = userEvent.setup();
    api.submitChange = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Submit for approval" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That service is unavailable right now",
    );
  });

  it("renders the server's own refusal for an illegal transition", async () => {
    // `_http_error` turns ChangeValidationError into an HTTPException whose
    // detail is a sentence — "emergency change requires retrospective approval
    // before completion" is the one that matters most, and it is the server's
    // words, not ours.
    const user = userEvent.setup();
    api.getChange = vi.fn().mockResolvedValue(changeIn("implementing", { type: "emergency" }));
    api.transitionChange = vi
      .fn()
      .mockRejectedValue(
        new ApiError(
          400,
          JSON.stringify({ detail: "emergency change requires retrospective approval before completion" }),
        ),
      );
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Mark complete" }));
    const dialog = await screen.findByRole("dialog", { name: "Complete this change" });
    await user.click(within(dialog).getByRole("button", { name: "Mark complete" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "emergency change requires retrospective approval before completion",
    );
  });
});

// --------------------------------------------------------------------------
// UX remediation cycle 6 — "Link action requires the user to know and
// hand-type a raw UUID"
// --------------------------------------------------------------------------

// ---------------------------------------------------------------- cycle 12
//
// B40 [LOW]: "Combined with the constant `document.title` of 'ITMS', a user
// with several change tabs open cannot tell them apart."
// B39/B64-adjacent: the wire's identifiers reaching the screen.
describe("ChangeDetail identity and vocabulary (cycle 12)", () => {
  const CHANGE_ID = "06a85c19-55e2-7f3c-8000-831c7bdba357";

  function api(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME),
      getChange: vi
        .fn()
        .mockResolvedValue({ ...(CHANGES[0] as ChangeRequest), id: CHANGE_ID, state: "draft" }),
      listChangeLinks: vi.fn().mockResolvedValue([]),
      listChangeApprovals: vi.fn().mockResolvedValue([]),
      ...overrides,
    };
  }

  function renderDetail(overrides: Partial<ApiClient> = {}) {
    mockUseApi.mockReturnValue(api(overrides) as ApiClient);
    return renderWithProviders(
      <Routes>
        <Route path="/changes/:changeId" element={<ChangeDetail />} />
      </Routes>,
      [`/changes/${CHANGE_ID}`],
    );
  }

  it("names the browser tab after the change, so two open changes are distinguishable", async () => {
    document.title = "ITMS";
    const view = renderDetail();
    await screen.findByRole("heading", { name: "Upgrade switch firmware" });
    await waitFor(() => expect(document.title).toBe("Upgrade switch firmware · Change · ITMS"));

    // And gives the name back on the way out, so the next page is not left
    // wearing this change's title.
    view.unmount();
    expect(document.title).toBe("ITMS");
  });

  it("reads the state, type and risk as words rather than as wire values", async () => {
    renderDetail();
    expect(await screen.findByText("Draft")).toBeInTheDocument();
    // The type and the risk are two cells of the facts strip now (v3's
    // `facts`), not one sentence: each is asserted as its own word.
    expect(screen.getByText("Standard")).toBeInTheDocument();
    expect(screen.getByText("Medium")).toBeInTheDocument();
    expect(screen.queryByText("draft")).not.toBeInTheDocument();
  });

  it("reads a linked action's type as words in the table and in the picker", async () => {
    renderDetail({
      listChangeLinks: vi.fn().mockResolvedValue([
        {
          id: "l1",
          action_type: "provision_run",
          action_id: "06a85c19-55e2-7f3c-8000-831c7bdba300",
          linked_at: "2026-02-01T00:00:00.000Z",
          linked_by: "u1",
        },
      ]),
    });
    // The table cell (the <option> of the same name is the second match). The
    // cell leads with the link glyph, so the words sit in a span inside it.
    expect((await screen.findAllByText("Provision run"))[0]?.closest("td")).not.toBeNull();
    expect(screen.queryByText("provision_run")).not.toBeInTheDocument();
    // The select offers the same words; the VALUES stay the wire's.
    const select = screen.getByLabelText("Action type") as HTMLSelectElement;
    expect([...select.options].map((option) => option.textContent)).toEqual([
      "Patch run",
      "Deployment",
      "Provision run",
      "Patch approval",
    ]);
    expect([...select.options].map((option) => option.value)).toEqual([
      "patch_run",
      "deployment",
      "provision_run",
      "patch_approval",
    ]);
  });

  it("reads an approval decision as a word", async () => {
    renderDetail({
      listChangeApprovals: vi.fn().mockResolvedValue([
        {
          id: "a1",
          approver_id: "u2",
          decision: "approved",
          comment: null,
          is_retrospective: false,
          decided_at: "2026-02-01T00:00:00.000Z",
        },
      ]),
    });
    expect(await screen.findByText("Approved")).toBeInTheDocument();
  });
});

describe("change link picker options", () => {
  const DEPLOYMENT = {
    id: "dep1",
    package_id: "pkg1",
    action: "install",
    target_type: "device",
    target_id: "dev1",
    window_cron: null,
    requested_by: "u1",
    state: "complete",
    created_at: "2026-02-01T00:00:00.000Z",
  };
  const PACKAGE = {
    id: "pkg1",
    name: "CoreSwitch firmware",
    version: "2.1",
    os_family: "linux",
    installer_type: "bin",
    artifact_ref: null,
    artifact_sha256: "abc",
    detection_rule: {},
    uninstall_rule: {},
    install_args: null,
    created_at: "2026-01-01T00:00:00.000Z",
  };

  it("names a deployment by its package, not by its id", () => {
    const [option] = deploymentPickerOptions([DEPLOYMENT], [PACKAGE], "America/New_York");
    expect(option?.label).toBe("CoreSwitch firmware 2.1 · install");
    expect(option?.id).toBe("dep1");
  });

  // The sublabel is the safety, not decoration: the same package is deployed
  // repeatedly, and the run being evidenced is the one from this change's
  // window. It is also rendered in the reader's zone — 00:00 UTC on 1 February
  // is 19:00 on 31 JANUARY in New York.
  it("distinguishes two deployments of one package by state and time", () => {
    const [option] = deploymentPickerOptions([DEPLOYMENT], [PACKAGE], "America/New_York");
    expect(option?.sublabel).toContain("complete");
    expect(option?.sublabel).toContain("31 Jan 2026, 19:00");
  });

  it("falls back to the package id rather than blank when the package is unreadable", () => {
    const [option] = deploymentPickerOptions([DEPLOYMENT], [], null);
    expect(option?.label).toContain("pkg1");
  });

  it("names a provisioning run by its profile and distinguishes runs by state and target", () => {
    const [option] = provisionRunPickerOptions(
      [
        {
          id: "run1",
          profile_id: "prof1",
          device_id: "WKS-042",
          target_ref: null,
          state: "succeeded",
          answer_ref: null,
          started_at: "2026-02-01T00:00:00.000Z",
          finished_at: null,
          error: null,
        },
      ],
      [
        {
          id: "prof1",
          name: "Standard laptop build",
          image_id: "img1",
          post_config: {},
          target_scope: {},
          enabled: true,
          created_at: "2026-01-01T00:00:00.000Z",
          has_domain_join_credential: false,
          has_local_admin_credential: false,
        },
      ],
      "America/New_York",
    );
    expect(option?.label).toBe("Standard laptop build");
    expect(option?.sublabel).toContain("succeeded");
    expect(option?.sublabel).toContain("WKS-042");
  });
});

describe("ChangeDetail link action picker", () => {
  let api: Partial<ApiClient>;

  const CHANGE_ID = "06a85c19-55e2-7f3c-8000-831c7bdba357";

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      getChange: vi.fn().mockResolvedValue({ ...(CHANGES[0] as ChangeRequest), id: CHANGE_ID }),
      listChangeLinks: vi.fn().mockResolvedValue([]),
      listChangeApprovals: vi.fn().mockResolvedValue([]),
      addChangeLink: vi.fn().mockResolvedValue({
        id: "link1",
        action_type: "deployment",
        action_id: "dep1",
        linked_at: "2026-01-01T00:00:00.000Z",
        linked_by: "u1",
      }),
      listDeployments: vi.fn().mockResolvedValue([
        {
          id: "dep1",
          package_id: "pkg1",
          action: "install",
          target_type: "device",
          target_id: "dev1",
          window_cron: null,
          requested_by: "u1",
          state: "complete",
          created_at: "2026-02-01T00:00:00.000Z",
        },
      ]),
      listPackages: vi.fn().mockResolvedValue([
        {
          id: "pkg1",
          name: "CoreSwitch firmware",
          version: "2.1",
          os_family: "linux",
          installer_type: "bin",
          artifact_ref: null,
          artifact_sha256: "abc",
          detection_rule: {},
          uninstall_rule: {},
          install_args: null,
          created_at: "2026-01-01T00:00:00.000Z",
        },
      ]),
      listProvisionRuns: vi.fn().mockResolvedValue([]),
      listProvisionProfiles: vi.fn().mockResolvedValue([]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/changes/:changeId" element={<ChangeDetail />} />
      </Routes>,
      [`/changes/${CHANGE_ID}`],
    );
  }

  // AUDIT (HIGH): "There is no picker, no search, no recent-actions list...
  // the ID format is a 36-character UUID which the user must find in another
  // module and transcribe."
  it("links a deployment chosen from the list, never typed", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.selectOptions(await screen.findByLabelText("Action type"), "deployment");
    // `getByRole("combobox", ...)`, not `getByLabelText`: the picker's own
    // listbox carries `aria-label` too. Clicking the row is how a person
    // chooses — typing into the box filters, it does not select, so a test
    // that typed the id would pass against the free-text field this replaced.
    await user.click(screen.getByRole("combobox", { name: "Deployment" }));
    await user.click(await screen.findByRole("option", { name: /CoreSwitch firmware 2\.1/ }));
    await user.click(screen.getByRole("button", { name: "Link" }));

    await waitFor(() =>
      expect(api.addChangeLink).toHaveBeenCalledWith(CHANGE_ID, {
        action_type: "deployment",
        action_id: "dep1",
      }),
    );
  });

  it("says what would fill an empty deployment list instead of showing nothing", async () => {
    // Cycle 7: a picker that is empty on a fresh install and says nothing is
    // the blocked flow wearing a nicer control.
    const user = userEvent.setup();
    api.listDeployments = vi.fn().mockResolvedValue([]);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    await user.selectOptions(await screen.findByLabelText("Action type"), "deployment");
    await user.click(screen.getByRole("combobox", { name: "Deployment" }));

    expect(await screen.findByText(/Fleet → Software deployment/)).toBeInTheDocument();
  });

  it("names the unchosen deployment on submit rather than doing nothing", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.selectOptions(await screen.findByLabelText("Action type"), "deployment");
    await user.click(screen.getByRole("button", { name: "Link" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Deployment is required");
    expect(api.addChangeLink).not.toHaveBeenCalled();
  });

  it("keeps the hand-typed field for the two types with no picker yet, and says where they are listed", async () => {
    // The note used to claim the console could not list patch runs at all.
    // `GET /patch/runs` and `GET /patch/approvals` exist (cross-module
    // guidance doc §4), so the note now says where they are shown instead.
    renderDetail();

    expect(await screen.findByLabelText("Action ID *")).toBeInTheDocument();
    expect(screen.getByText(/Patch runs are listed on each computer's Patching tab/)).toBeInTheDocument();
    expect(screen.queryByText(/available in the console/)).toBeNull();
  });

  it("drops the chosen id when the action type changes", async () => {
    // A deployment id is not a provisioning run id, and the server
    // existence-checks the id against the table the TYPE names — carrying it
    // across would link an id of the wrong kind.
    const user = userEvent.setup();
    renderDetail();

    await user.selectOptions(await screen.findByLabelText("Action type"), "deployment");
    await user.click(screen.getByRole("combobox", { name: "Deployment" }));
    await user.click(await screen.findByRole("option", { name: /CoreSwitch firmware/ }));
    await user.selectOptions(screen.getByLabelText("Action type"), "provision_run");
    await user.click(screen.getByRole("button", { name: "Link" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Provisioning run is required");
    expect(api.addChangeLink).not.toHaveBeenCalled();
  });
});

// --------------------------------------------------------------------------
// UX remediation cycle 4 — "An inverted maintenance window is accepted"
// --------------------------------------------------------------------------

describe("ChangeRequestForm maintenance window", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NY),
      createChange: vi.fn().mockResolvedValue(CHANGES[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function fillTitleAndDescription(user: ReturnType<typeof userEvent.setup>) {
    await user.type(screen.getByLabelText("Title *"), "Upgrade switch firmware");
    await user.type(screen.getByLabelText("Description *"), "Roll out firmware 2.1.");
  }

  // AUDIT (HIGH): "I deliberately set Window start = 2026-08-25T09:00 and
  // Window end = 2026-08-20T09:00 — an end five days before the start. The
  // record saved with no complaint."
  it("refuses a window that ends before it opens", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ChangeRequestForm change="new" onClose={() => {}} />);

    await fillTitleAndDescription(user);
    fireEvent.change(screen.getByLabelText("Window start"), {
      target: { value: "2026-08-25T09:00" },
    });
    fireEvent.change(screen.getByLabelText("Window end"), {
      target: { value: "2026-08-20T09:00" },
    });
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Window end must be after Window start/,
    );
    expect(api.createChange).not.toHaveBeenCalled();
  });

  it("refuses a zero-length window", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ChangeRequestForm change="new" onClose={() => {}} />);

    await fillTitleAndDescription(user);
    fireEvent.change(screen.getByLabelText("Window start"), {
      target: { value: "2026-08-25T09:00" },
    });
    fireEvent.change(screen.getByLabelText("Window end"), {
      target: { value: "2026-08-25T09:00" },
    });
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Window end must be after Window start/,
    );
    expect(api.createChange).not.toHaveBeenCalled();
  });

  // The valid path, so the rule cannot be satisfied by a form that refuses
  // every window.
  it("accepts a window that runs forwards", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ChangeRequestForm change="new" onClose={() => {}} />);

    await fillTitleAndDescription(user);
    fireEvent.change(screen.getByLabelText("Window start"), {
      target: { value: "2026-08-25T09:00" },
    });
    fireEvent.change(screen.getByLabelText("Window end"), {
      target: { value: "2026-08-25T11:00" },
    });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(api.createChange).toHaveBeenCalledWith(
        expect.objectContaining({
          window_start: "2026-08-25T13:00:00.000Z",
          window_end: "2026-08-25T15:00:00.000Z",
        }),
      ),
    );
  });

  // AUDIT (HIGH): "Nothing in the UI states which timezone either the input or
  // the output is in."
  it("states the zone the window is entered in", async () => {
    renderWithProviders(<ChangeRequestForm change="new" onClose={() => {}} />);

    expect(await screen.findByText(/Window times are in America\/New_York/)).toBeInTheDocument();
  });
});
