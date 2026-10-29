import { existsSync, readFileSync } from "node:fs";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Device, Me } from "../../../lib/api";
import { BcpReport } from "../BcpReport";
import { outcomeVariant, remediationVariant, severityVariant } from "../bcpState";
import { PlanDetail } from "../PlanDetail";
import { PlanEditor } from "../PlanEditor";
import { PlansPage } from "../PlansPage";
import { TestRecorder } from "../TestRecorder";
import { procedurePickerOptions, scopeDevicePickerOptions } from "../pickerOptions";
import { needsReview, reviewStanding, reviewStandingLabel } from "../reviewState";
import type { FindingOut, PlanOut, PlanStatusOut, TestOut } from "../types";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch. BCP/DR data is plain TanStack Query state (no live-event
// stream) — mirrors modules/change/__tests__/change.test.tsx.
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
  permissions: ["bcp.read", "bcp.manage", "bcp.approve"],
};

const PLANS: PlanOut[] = [
  {
    id: "plan1",
    client_id: "o1",
    name: "Core datacenter failover",
    description: "Fail over core services to the DR site.",
    scope: [{ kind: "device_group", id: "core" }],
    rto_minutes: 60,
    rpo_minutes: 15,
    procedure_refs: ["doc-1"],
    dependencies: [],
    review_interval_days: 365,
    last_reviewed_at: null,
    approved_by: null,
    state: "active",
    created_by: "u2",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

// UX remediation cycle 5. The profile zone (`/me`'s `timezone`, cycle 2) is
// what every instant in this module is rendered in, and it is deliberately
// NOT the zone the test runner happens to sit in -- an assertion that passes
// in UTC and in New York alike is not testing the zone at all.
//
// America/New_York is UTC-5 in January, which is the operator's offset in the
// finding this cycle exists to close: "every datetime in the product is
// displayed 5 hours earlier than it was entered."
const ME_NY: Me = { ...ME, timezone: "America/New_York" };

const TEST_FIXTURE: TestOut = {
  id: "test1",
  plan_id: "plan1",
  type: "restore_drill",
  scheduled_for: "2026-01-15T19:00:00.000Z",
  executed_at: null,
  executed_by: null,
  outcome: "pending",
  rto_met: null,
  rpo_met: null,
  evidence: {},
  notes: "",
  created_at: "2026-01-01T00:00:00.000Z",
};

const FINDING_FIXTURE: FindingOut = {
  id: "finding1",
  test_id: "test1",
  description: "Restore of the file server exceeded RTO",
  severity: "high",
  remediation_state: "open",
  change_ref: null,
  created_at: "2026-01-15T19:00:00.000Z",
  resolved_at: null,
};

describe("PlansPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listPlans: vi.fn().mockResolvedValue(PLANS),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("lists plans from the mocked api", async () => {
    renderWithProviders(<PlansPage />);

    expect(await screen.findByText("Core datacenter failover")).toBeInTheDocument();
    // Read as a word, not as the wire's identifier (cycle 12).
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.queryByText("active")).not.toBeInTheDocument();
  });

  it("hides the New plan control when the caller lacks bcp.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "bcp.manage"),
    });
    renderWithProviders(<PlansPage />);

    await screen.findByText("Core datacenter failover");
    expect(screen.queryByRole("button", { name: "New plan" })).not.toBeInTheDocument();
  });

  it("shows the New plan control when the caller has bcp.manage", async () => {
    renderWithProviders(<PlansPage />);

    await screen.findByText("Core datacenter failover");
    expect(screen.getByRole("button", { name: "New plan" })).toBeInTheDocument();
  });
});

describe("PlanDetail", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      getPlan: vi.fn().mockResolvedValue(PLANS[0]),
      listPlanTests: vi.fn().mockResolvedValue([]),
      reviewPlan: vi.fn().mockResolvedValue(PLANS[0]),
      updatePlan: vi.fn().mockResolvedValue(PLANS[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/bcp/plans/:planId" element={<PlanDetail />} />
      </Routes>,
      ["/bcp/plans/plan1"],
    );
  }

  it("shows the Record review control when the caller has bcp.approve", async () => {
    renderDetail();

    await screen.findByText("Core datacenter failover");
    expect(screen.getByRole("button", { name: "Record review" })).toBeInTheDocument();
  });

  it("hides the Record review control when the caller lacks bcp.approve", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "bcp.approve"),
    });
    renderDetail();

    await screen.findByText("Core datacenter failover");
    expect(screen.queryByRole("button", { name: "Record review" })).not.toBeInTheDocument();
  });

  it("shows Archive for an active plan when the caller has bcp.manage", async () => {
    renderDetail();

    await screen.findByText("Core datacenter failover");
    expect(screen.getByRole("button", { name: "Archive" })).toBeInTheDocument();
  });

  it("shows Unarchive for an archived plan", async () => {
    api.getPlan = vi.fn().mockResolvedValue({ ...PLANS[0], state: "archived" });
    renderDetail();

    await screen.findByText("Core datacenter failover");
    expect(screen.getByRole("button", { name: "Unarchive" })).toBeInTheDocument();
  });

  it("hides the Archive control when the caller lacks bcp.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "bcp.manage"),
    });
    renderDetail();

    await screen.findByText("Core datacenter failover");
    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
  });
});

// UX remediation cycle 3, the two HIGH findings on this dialog. Both had the
// same cause -- `disabled={mutation.isPending || !name}` on a dialog with no
// <form> and no validation feedback -- so both are pinned here: the control
// must be LIVE, the click must produce the reason, and nothing may reach the
// API until the reason is gone.
describe("PlanEditor form validation", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      createPlan: vi.fn().mockResolvedValue(PLANS[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderEditor() {
    const user = userEvent.setup();
    renderWithProviders(<PlanEditor plan="new" onClose={vi.fn()} />);
    return user;
  }

  // Auditor (HIGH), /bcp → New plan → Save: "Empty submit is a silent no-op."
  // The click used to land on a disabled control -- nothing moved and no field
  // was named.
  it("names the missing field on an empty submit instead of disabling Save", async () => {
    const user = renderEditor();

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByRole("alert")).toHaveTextContent("Name is required");
    expect(api.createPlan).not.toHaveBeenCalled();
  });

  // Auditor (HIGH), /bcp → plan → RTO / RPO: "Negative RTO and RPO are
  // accepted and displayed as recovery objectives." `bcp/schema.py` bounds
  // neither (`rto_minutes: int | None`), so this dialog is the only thing
  // between "-90" and a plan that promises recovery before the outage.
  it("refuses a negative RTO rather than sending it", async () => {
    const user = renderEditor();

    await user.type(screen.getByLabelText("Name *"), "Core datacenter failover");
    await user.type(screen.getByLabelText("RTO (minutes)"), "-90");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /RTO \(minutes\) cannot be negative/,
    );
    expect(api.createPlan).not.toHaveBeenCalled();
  });

  it("refuses a negative RPO rather than sending it", async () => {
    const user = renderEditor();

    await user.type(screen.getByLabelText("Name *"), "Core datacenter failover");
    await user.type(screen.getByLabelText("RPO (minutes)"), "-15");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /RPO \(minutes\) cannot be negative/,
    );
    expect(api.createPlan).not.toHaveBeenCalled();
  });

  // `submit` sends `Number(reviewIntervalDays)` and `Number("") === 0`, so a
  // cleared interval used to enrol the plan as permanently overdue for review
  // instead of refusing the save.
  it("refuses a cleared review interval rather than sending 0", async () => {
    const user = renderEditor();

    await user.type(screen.getByLabelText("Name *"), "Core datacenter failover");
    await user.clear(screen.getByLabelText("Review interval (days) *"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Review interval (days) is required",
    );
    expect(api.createPlan).not.toHaveBeenCalled();
  });

  // The valid path, so the rules above cannot be satisfied by a dialog that
  // refuses everything. RTO/RPO stay genuinely optional (both are
  // `int | None` server-side), and zero is a real RPO -- "lose nothing" --
  // which is why the rule bans negatives and not zero.
  it("sends a plan with a zero RPO and no RTO once the fields are valid", async () => {
    const user = renderEditor();

    await user.type(screen.getByLabelText("Name *"), "Core datacenter failover");
    await user.type(screen.getByLabelText("RPO (minutes)"), "0");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.createPlan).toHaveBeenCalledWith({
      // The internal client, `/me`'s `org_id` (PHASE 3: the selector's).
      client_id: "o1",
      name: "Core datacenter failover",
      description: "",
      scope: [],
      rto_minutes: null,
      rpo_minutes: 0,
      procedure_refs: [],
      review_interval_days: 365,
    });
  });

  // MSP clients phase 2: continuity is a module a client may not use, and
  // the server's 404 for that is a sentence, which is what the person reads.
  it("says so when the client does not use continuity", async () => {
    api.createPlan = vi
      .fn()
      .mockRejectedValue(
        new ApiError(404, JSON.stringify({ detail: "this client does not use that module" })),
      );
    mockUseApi.mockReturnValue(api as ApiClient);
    const user = renderEditor();

    await user.type(screen.getByLabelText("Name *"), "Core datacenter failover");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "this client does not use that module",
    );
  });

  // A button with no `type` inside a <form> is a submit button, so the scope
  // controls would have saved the plan the moment they were pressed.
  it("adds a scope row without submitting the form", async () => {
    const user = renderEditor();

    await user.click(screen.getByRole("button", { name: "Add scope entry" }));

    expect(screen.getByLabelText("Scope 1 kind")).toBeInTheDocument();
    expect(api.createPlan).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("bcp state vocabulary", () => {
  it("maps a real backend outcome to a non-fallback badge variant", () => {
    // bcp_test.outcome vocabulary is sourced from the backend
    // (backend/app/modules/bcp/schema.py TestRecordRequest.outcome) —
    // "pass" is a real outcome value.
    expect(outcomeVariant("pass")).not.toBe("muted");
    expect(outcomeVariant("pass")).toBe("success");
  });

  it("falls back to muted for an unknown/invented outcome name", () => {
    // Guards against the Phase-5 regression: no invented outcome name may
    // resolve to a real variant.
    expect(outcomeVariant("not_a_real_outcome")).toBe("muted");
  });

  it("maps a real backend remediation_state to a non-fallback badge variant", () => {
    expect(remediationVariant("resolved")).toBe("success");
  });

  it("falls back to muted for an unknown/invented remediation_state", () => {
    expect(remediationVariant("not_a_real_state")).toBe("muted");
  });

  it("maps a real backend severity to a non-fallback badge variant", () => {
    expect(severityVariant("critical")).toBe("destructive");
  });

  it("falls back to muted for an unknown/invented severity", () => {
    expect(severityVariant("not_a_real_severity")).toBe("muted");
  });
});

// --------------------------------------------------------------------------
// UX remediation cycles 4 and 5, in this module
// --------------------------------------------------------------------------

describe("BCP instants are rendered in the reader's profile zone", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NY),
      listPlans: vi.fn().mockResolvedValue(PLANS),
      planStatus: vi.fn().mockResolvedValue([]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // `new Date(plan.created_at).toLocaleString()` rendered the runner's zone in
  // the runner's locale. The plan below was created at midnight UTC, which is
  // 19:00 the previous evening in the operator's zone -- a different DAY, not
  // just a different time, which is the version of this bug nobody notices.
  it("renders a plan's created_at in the profile zone, not the browser's", async () => {
    renderWithProviders(<PlansPage />);

    expect(await screen.findByText("31 Dec 2025, 19:00")).toBeInTheDocument();
    expect(screen.queryByText(/1\/1\/2026/)).not.toBeInTheDocument();
  });

  it("renders an absent test date as an em dash, never Invalid Date", async () => {
    api.getPlan = vi.fn().mockResolvedValue(PLANS[0]);
    api.listPlanTests = vi.fn().mockResolvedValue([TEST_FIXTURE]);
    mockUseApi.mockReturnValue(api as ApiClient);

    renderWithProviders(
      <Routes>
        <Route path="/bcp/plans/:planId" element={<PlanDetail />} />
      </Routes>,
      ["/bcp/plans/plan1"],
    );

    // scheduled_for is 19:00Z, which is 14:00 in New York; executed_at is null.
    expect(await screen.findByText("15 Jan 2026, 14:00")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
  });
});

describe("BCP datetime-local values are qualified before they are sent", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NY),
      getPlan: vi.fn().mockResolvedValue(PLANS[0]),
      listPlanTests: vi.fn().mockResolvedValue([]),
      createTest: vi.fn().mockResolvedValue(TEST_FIXTURE),
      getTest: vi.fn().mockResolvedValue(TEST_FIXTURE),
      listFindings: vi.fn().mockResolvedValue([]),
      recordTest: vi.fn().mockResolvedValue(TEST_FIXTURE),
      periodReport: vi.fn().mockResolvedValue({
        outcome_counts: {},
        plans_tested: [],
        open_findings_by_severity: {},
        review_overdue_plans: [],
      }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // THIS IS THE FIVE-HOUR BUG ITSELF. A `datetime-local` field hands back
  // "2026-01-15T14:00" with no zone; sent unqualified to an API that reads it
  // as UTC, a drill scheduled for 14:00 in New York is stored as 14:00Z and
  // read back as 09:00. 19:00Z is that same wall clock, correctly qualified.
  it("sends a scheduled DR test as an instant, not a bare wall clock", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <Routes>
        <Route path="/bcp/plans/:planId" element={<PlanDetail />} />
      </Routes>,
      ["/bcp/plans/plan1"],
    );

    await user.click(await screen.findByRole("button", { name: "New test" }));
    fireEvent.change(screen.getByLabelText("Scheduled for"), {
      target: { value: "2026-01-15T14:00" },
    });
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(api.createTest).toHaveBeenCalledWith("plan1", {
        type: "restore_drill",
        scheduled_for: "2026-01-15T19:00:00.000Z",
        notes: "",
      }),
    );
  });

  // The same field on the recorder, and the value that matters most: this one
  // is the moment a SOC 2 evidence pack claims the recovery was proven at.
  it("records an executed_at as an instant, not a bare wall clock", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <Routes>
        <Route path="/bcp/tests/:testId" element={<TestRecorder />} />
      </Routes>,
      ["/bcp/tests/test1"],
    );

    fireEvent.change(await screen.findByLabelText("Executed at"), {
      target: { value: "2026-01-15T14:00" },
    });
    await user.click(screen.getByRole("button", { name: "Record test" }));

    await waitFor(() =>
      expect(api.recordTest).toHaveBeenCalledWith("test1", {
        executed_at: "2026-01-15T19:00:00.000Z",
        rto_met: null,
        rpo_met: null,
        outcome: "pass",
        notes: "",
      }),
    );
  });

  // An untouched optional field must stay absent. `toInstant("")` is "", so
  // the `|| null` still holds -- the alternative is the epoch or the string
  // "Invalid Date", neither of which the operator typed.
  it("leaves an untouched executed_at null rather than sending the epoch", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <Routes>
        <Route path="/bcp/tests/:testId" element={<TestRecorder />} />
      </Routes>,
      ["/bcp/tests/test1"],
    );

    await user.click(await screen.findByRole("button", { name: "Record test" }));

    await waitFor(() =>
      expect(api.recordTest).toHaveBeenCalledWith(
        "test1",
        expect.objectContaining({ executed_at: null }),
      ),
    );
  });

  // The period report's boundaries ARE the evidence: an unqualified "from"
  // moved the start of a SOC 2 period by the reader's offset, quietly
  // including or excluding the tests that sit at either end of it.
  it("qualifies both ends of the period report before requesting it", async () => {
    renderWithProviders(
      <Routes>
        <Route path="/bcp/report" element={<BcpReport />} />
      </Routes>,
      ["/bcp/report"],
    );

    fireEvent.change(await screen.findByLabelText("From"), {
      target: { value: "2026-01-01T00:00" },
    });
    fireEvent.change(screen.getByLabelText("To"), {
      target: { value: "2026-01-31T23:59" },
    });

    await waitFor(() =>
      expect(api.periodReport).toHaveBeenCalledWith(
        "2026-01-01T05:00:00.000Z",
        "2026-02-01T04:59:00.000Z",
      ),
    );
  });

  // REVIEW WAVE, 390x844: `.form-grid` was two columns at every width, so each
  // period field was about 150px on a handset and the browser clipped the
  // calendar-picker button off the right-hand edge of a control that needs
  // roughly 190px to draw `mm/dd/yyyy, --:-- --` beside it. This page does
  // nothing at all until both ends are set, so the only remaining way in was
  // typing the raw US-format mask. `full` gave each field the row.
  //
  // REVIEW WAVE ROUND 3, 1440x900 (finding I7): `full` was then doing nothing
  // at 390 -- the kit's 640px fold had landed and already makes the grid one
  // column -- and everything at 1440, where it stacked a two-field RANGE down
  // the left of a 1140px card with ~1100px of empty panel beside it. Every
  // sibling filter surface pairs its controls inline. So the two ends now take
  // the grid's own behaviour: one row above the fold, stacked below it.
  //
  // FALSIFIER: put `full` back on either field and this fails.
  it("puts the two ends of the period on one row, and leaves the fold to stack them", async () => {
    renderWithProviders(
      <Routes>
        <Route path="/bcp/report" element={<BcpReport />} />
      </Routes>,
      ["/bcp/report"],
    );

    for (const label of ["From", "To"]) {
      const field = (await screen.findByLabelText(label)).closest(".field");
      expect(field, `${label} is a .field`).not.toBeNull();
      expect(field, `${label} must not span the grid`).not.toHaveClass("full");
    }
    // Both ends are in ONE grid, so the two columns are the two ends.
    const grid = document.querySelector(".form-grid");
    expect(grid?.querySelectorAll(":scope > .field")).toHaveLength(2);
  });

  // AND THE HELPER LINE UNDER THEM TAKES A GAP (finding I7, second half).
  // `.form-grid > .field` zeroes the field's bottom margin -- the grid's own
  // 14px gap does that job between rows -- and `<p>` is reset to no margin, so
  // the timezone sentence sat ~4px under the control it explains where every
  // sibling helper line uses the standard gap. One grid gap is that gap.
  // FALSIFIER: drop the style and this fails.
  it("sets the timezone helper a standard gap below the fields, not flush against them", async () => {
    renderWithProviders(
      <Routes>
        <Route path="/bcp/report" element={<BcpReport />} />
      </Routes>,
      ["/bcp/report"],
    );

    const helper = await screen.findByText(/^Times are in/);
    expect(helper).toHaveClass("note");
    expect((helper as HTMLElement).style.marginTop).toBe("14px");
  });

  // THE HALF OF THE 390 GUARANTEE THAT `full` USED TO CARRY now belongs to the
  // kit, and this is the assertion that says so out loud: without the fold,
  // dropping `full` above puts a `datetime-local` back into a ~150px column
  // and clips its picker away again, which is the original defect returning by
  // a different door. jsdom computes no layout, so the rule is read from the
  // stylesheet itself.
  it("relies on a 640px fold that the kit actually declares", () => {
    const path = existsSync("src/shell/components.css")
      ? "src/shell/components.css"
      : "frontend/src/shell/components.css";
    const css = readFileSync(path, "utf8").replace(/\s+/g, " ");
    expect(css).toMatch(/@media \(max-width: 640px\) \{ \.form-grid \{ grid-template-columns: minmax\(0, 1fr\); \}/);
  });
});

describe("BCP failures reach the user as a sentence", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NY),
      getPlan: vi.fn().mockResolvedValue(PLANS[0]),
      listPlanTests: vi.fn().mockResolvedValue([]),
      updatePlan: vi.fn().mockRejectedValue(new ApiError(503, "")),
      getTest: vi.fn().mockResolvedValue(TEST_FIXTURE),
      listFindings: vi.fn().mockResolvedValue([FINDING_FIXTURE]),
      updateFinding: vi.fn().mockRejectedValue(new ApiError(403, "")),
      recordTest: vi.fn().mockRejectedValue(
        new ApiError(422, JSON.stringify({ detail: [{ loc: ["body", "executed_at"], msg: "Input should be a valid datetime" }] })),
      ),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // `{String(updatePlan.error)}` rendered "[object Object]" at best; for the
  // bodyless 503 a proxy returns, `ApiError.message` is "" and the line
  // rendered NOTHING -- Archive looked like it worked.
  it("says what the server refused when archiving a plan, bodyless 503 and all", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <Routes>
        <Route path="/bcp/plans/:planId" element={<PlanDetail />} />
      </Routes>,
      ["/bcp/plans/plan1"],
    );

    await user.click(await screen.findByRole("button", { name: "Archive" }));
    // Archiving asks first now (cycle 8) — the refusal is what reaches the
    // user AFTER the dialog is confirmed, and it still has to be a sentence.
    await user.click(await screen.findByRole("button", { name: "Archive plan" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That service is unavailable right now",
    );
    expect(screen.queryByText(/object Object/)).not.toBeInTheDocument();
  });

  // The 422 body is `{"detail":[{"loc":…,"msg":…}]}`. Printed raw it is a
  // Pydantic payload on screen; `describeSubmitError` names the field.
  it("translates a validation refusal of a recorded test instead of printing it", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <Routes>
        <Route path="/bcp/tests/:testId" element={<TestRecorder />} />
      </Routes>,
      ["/bcp/tests/test1"],
    );

    await user.click(await screen.findByRole("button", { name: "Record test" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Executed at: Input should be a valid datetime");
    expect(alert).not.toHaveTextContent("detail");
  });

  // SWALLOWED ENTIRELY before this cycle: the remediation `<select>` had no
  // error surface at all, so a refused "resolved" left an open finding
  // reading as closed on a page whose whole purpose is being audit evidence.
  it("announces a refused remediation-state change that used to be swallowed", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <Routes>
        <Route path="/bcp/tests/:testId" element={<TestRecorder />} />
      </Routes>,
      ["/bcp/tests/test1"],
    );

    // "Open" on screen, `open` on the wire (cycle 12 humanised the labels; the
    // option VALUES are still the backend's).
    await user.selectOptions(await screen.findByDisplayValue("Open"), "resolved");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You do not have permission to do that.",
    );
  });
});

// --------------------------------------------------------------------------
// UX remediation cycle 7 — "A plan reviewed *never* is reported as up to date"
// --------------------------------------------------------------------------

describe("reviewStanding", () => {
  // backend/app/modules/bcp/reporting.py#_is_review_overdue: "A plan that has
  // never been reviewed (`last_reviewed_at is None`) is always overdue." The
  // backend was right and the frontend threw the answer away.
  it("calls a plan with no review on record never reviewed, not up to date", () => {
    expect(reviewStanding(null, false)).toBe("never");
    expect(reviewStanding(null, undefined)).toBe("never");
  });

  it("reports an overdue plan as overdue and a current one as current", () => {
    expect(reviewStanding("2026-01-01T00:00:00.000Z", true)).toBe("overdue");
    expect(reviewStanding("2026-01-01T00:00:00.000Z", false)).toBe("current");
  });

  // THE `Map.get` DEFECT ITSELF. A plan the status response does not carry --
  // every plan while that query is in flight, and every plan if it 403s --
  // used to fall through to the green badge.
  it("never claims a plan is up to date when the status is unknown", () => {
    expect(reviewStanding("2026-01-01T00:00:00.000Z", undefined)).toBe("unknown");
    // CASE-INSENSITIVE: the labels are sentence case now ("Up to date"), and
    // an exact-string assertion would have started passing for the wrong
    // reason the moment the capitalisation changed.
    expect(
      reviewStandingLabel(reviewStanding("2026-01-01T00:00:00.000Z", undefined)),
    ).not.toMatch(/up to date/i);
  });

  it("counts never-reviewed and overdue towards the KPI, and unknown towards neither", () => {
    expect(needsReview("never")).toBe(true);
    expect(needsReview("overdue")).toBe(true);
    expect(needsReview("current")).toBe(false);
    expect(needsReview("unknown")).toBe(false);
  });
});

describe("PlansPage review column", () => {
  let api: Partial<ApiClient>;

  const REVIEWED_PLAN: PlanOut = {
    ...(PLANS[0] as PlanOut),
    id: "plan2",
    name: "Payroll continuity",
    last_reviewed_at: "2026-01-05T00:00:00.000Z",
  };

  function statusRow(id: string, overdue: boolean): PlanStatusOut {
    return {
      id,
      name: id,
      state: "active",
      review_overdue: overdue,
      last_reviewed_at: null,
      last_test_outcome: null,
      open_findings: 0,
      restore_proven_coverage: { total: 0, restore_proven: 0 },
    };
  }

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listPlans: vi.fn().mockResolvedValue(PLANS),
      planStatus: vi.fn().mockResolvedValue([statusRow("plan1", true)]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // AUDIT (HIGH): "The plan I created has `Last reviewed: never · Review
  // interval: 1 days`. The list's REVIEW column nonetheless reads 'up to
  // date', and the 'Review overdue' KPI reads 0."
  it("labels a plan that has never been reviewed, and never calls it up to date", async () => {
    renderWithProviders(<PlansPage />);

    await screen.findByText("Core datacenter failover");
    expect(await screen.findByText("Never reviewed")).toBeInTheDocument();
    expect(screen.queryByText(/up to date/i)).not.toBeInTheDocument();
  });

  it("counts a never-reviewed plan in the Review overdue KPI", async () => {
    // The status row deliberately says NOT overdue: the plan has never been
    // reviewed, and that alone must reach the KPI. With the status row also
    // marked overdue this test would pass against the very expression the
    // finding is about.
    api.planStatus = vi.fn().mockResolvedValue([statusRow("plan1", false)]);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<PlansPage />);

    await screen.findByText("Never reviewed");
    const kpi = screen.getByText("Review overdue").closest(".stat-card");
    expect(kpi).toHaveTextContent("1");
  });

  // The other half of the `Map.get(...) ? … : "up to date"` defect: a plan the
  // status response does not carry at all.
  it("says the standing is unknown for a plan missing from the status response", async () => {
    api.listPlans = vi.fn().mockResolvedValue([REVIEWED_PLAN]);
    api.planStatus = vi.fn().mockResolvedValue([]);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<PlansPage />);

    await screen.findByText("Payroll continuity");
    expect(screen.getByText("Review unknown")).toBeInTheDocument();
    expect(screen.queryByText(/up to date/i)).not.toBeInTheDocument();
  });

  // The honest positive, so the rules above cannot be satisfied by a page that
  // never says a plan is fine.
  it("still says up to date for a plan reviewed inside its interval", async () => {
    api.listPlans = vi.fn().mockResolvedValue([REVIEWED_PLAN]);
    api.planStatus = vi.fn().mockResolvedValue([statusRow("plan2", false)]);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<PlansPage />);

    await screen.findByText("Payroll continuity");
    expect(await screen.findByText("Up to date")).toBeInTheDocument();
  });
});

/**
 * THE TILES ARE LINKS, AND EACH ONE OPENS THE ROWS IT COUNTED.
 *
 * "All tiles and all numbers or kpis need to be clickable and direct the user
 * to the specific menus, i dont want dead tiles everywhere" — the operator,
 * 2026-08-21. The compiler now refuses a tile with no `to`; it cannot refuse a
 * `to` that goes somewhere unrelated, or one whose destination no longer shows
 * the rows behind the number. Those are the assertions below, and the second
 * kind is why these tests open the URL and read the table.
 */
describe("the KPI tiles are links to the rows behind them", () => {
  const ARCHIVED_PLAN: PlanOut = {
    ...(PLANS[0] as PlanOut),
    id: "plan9",
    name: "Retired mail failover",
    state: "archived",
    last_reviewed_at: "2026-01-05T00:00:00.000Z",
  };

  function status(id: string, overdue: boolean, openFindings: number): PlanStatusOut {
    return {
      id,
      name: id,
      state: "active",
      review_overdue: overdue,
      last_reviewed_at: null,
      last_test_outcome: null,
      open_findings: openFindings,
      restore_proven_coverage: { total: 0, restore_proven: 0 },
    };
  }

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
      listPlans: vi.fn().mockResolvedValue([PLANS[0], ARCHIVED_PLAN]),
      planStatus: vi.fn().mockResolvedValue([status("plan1", true, 3), status("plan9", false, 0)]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("gives every tile a real href — none is a dead tile", async () => {
    renderWithProviders(<PlansPage />, ["/bcp"]);
    await screen.findByText("Core datacenter failover");

    // Every destination is a route App.tsx declares: /bcp and /bcp/report.
    expect(tileHrefs()).toEqual({
      "Active plans": "/bcp?state=active",
      "Review overdue": "/bcp?review=overdue",
      "Open findings": "/bcp/report",
    });
  });

  // WHAT THE FIGURE IS MADE OF, on the second line of each tile. All three
  // read as a bare count over an empty half-card while the same kit tile
  // elsewhere in the console carries a breakdown; and "Review overdue: 2" in
  // particular is two different facts added together -- a plan nobody has ever
  // reviewed and a plan whose interval has elapsed are not the same problem.
  it("says what each tile's number is made of", async () => {
    renderWithProviders(<PlansPage />, ["/bcp"]);
    await screen.findByText("Core datacenter failover");

    const card = (label: string) => screen.getByText(label).closest(".stat-card");
    // One active plan of the two on record.
    expect(card("Active plans")).toHaveTextContent("of 2 on record");
    // plan1 has never been reviewed; nothing else is past its interval.
    expect(card("Review overdue")).toHaveTextContent("1 never reviewed, 0 past the interval");
    // Three open findings, and they are all on one plan.
    expect(card("Open findings")).toHaveTextContent("across 1 plan");
  });

  it("carries no breakdown for a figure that was never measured", async () => {
    // THE FALSIFIER for the test above: a breakdown printed unconditionally
    // would put "across 0 plans" under a findings tile reading "—", which is
    // the reassuring-zero defect this module already paid for once.
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listPlans: vi.fn().mockResolvedValue([PLANS[0]]),
      planStatus: vi.fn().mockRejectedValue(new Error("status 503")),
    } as unknown as ApiClient);
    renderWithProviders(<PlansPage />, ["/bcp"]);

    await screen.findByText("Core datacenter failover");
    const findings = screen.getByText("Open findings").closest(".stat-card");
    await waitFor(() => expect(findings?.querySelector(".stat-value")?.textContent).toBe("—"));
    expect(findings).not.toHaveTextContent("across");
  });

  it("shows exactly the plans its number counted when that destination is opened", async () => {
    renderWithProviders(<PlansPage />, ["/bcp?state=active"]);

    expect(await screen.findByText("Core datacenter failover")).toBeInTheDocument();
    expect(screen.queryByText("Retired mail failover")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Show all plans" })).toHaveAttribute("href", "/bcp");
  });

  it("opens the overdue tile on the overdue plans and nothing else", async () => {
    // plan1 has never been reviewed AND its status row says overdue; the
    // archived one is current. The KPI's own predicate decides both the count
    // and this list — they were two expressions once, and that is how a plan
    // came to read "up to date" while the KPI counted it.
    renderWithProviders(<PlansPage />, ["/bcp?review=overdue"]);

    expect(await screen.findByText("Core datacenter failover")).toBeInTheDocument();
    expect(screen.queryByText("Retired mail failover")).not.toBeInTheDocument();
  });

  it("distinguishes a measured zero from an unmeasured one on the findings tile", async () => {
    // The status response is what carries `open_findings`. Without it the old
    // `(status.data ?? []).reduce(...)` printed a reassuring 0 for a request
    // that never came back.
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listPlans: vi.fn().mockResolvedValue([PLANS[0]]),
      planStatus: vi.fn().mockRejectedValue(new Error("status 503")),
    } as unknown as ApiClient);
    renderWithProviders(<PlansPage />, ["/bcp"]);

    await screen.findByText("Core datacenter failover");
    const findings = screen.getByText("Open findings").closest(".stat-card");
    await waitFor(() => expect(findings?.querySelector(".stat-value")?.textContent).toBe("—"));
    expect(findings).not.toHaveTextContent("No finding is open");

    // And the overdue tile names WHICH zero it would be showing: with no
    // status response the interval half of the question was never asked.
    const overdue = screen.getByText("Review overdue").closest(".stat-card");
    expect(overdue).toHaveTextContent("1");
  });

  it("names the measured zero when the status response did arrive", async () => {
    const api: Partial<ApiClient> = {
      me: vi.fn().mockResolvedValue(ME),
      listPlans: vi.fn().mockResolvedValue([ARCHIVED_PLAN]),
      planStatus: vi.fn().mockResolvedValue([status("plan9", false, 0)]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<PlansPage />, ["/bcp"]);

    await screen.findByText("Retired mail failover");
    expect(await screen.findByText("No finding is open")).toBeInTheDocument();
    expect(screen.getByText("Every plan's review is current")).toBeInTheDocument();
    // A zero never wears the urgent treatment, whatever the tile asked for.
    expect(document.querySelectorAll("a.stat-card.urgent")).toHaveLength(0);
  });
});

// --------------------------------------------------------------------------
// UX remediation cycle 8 — the three identically styled header buttons
// --------------------------------------------------------------------------

describe("PlanDetail confirmations", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      getPlan: vi.fn().mockResolvedValue(PLANS[0]),
      listPlanTests: vi.fn().mockResolvedValue([]),
      planStatus: vi.fn().mockResolvedValue([]),
      reviewPlan: vi.fn().mockResolvedValue(PLANS[0]),
      updatePlan: vi.fn().mockResolvedValue({ ...(PLANS[0] as PlanOut), state: "archived" }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/bcp/plans/:planId" element={<PlanDetail />} />
      </Routes>,
      ["/bcp/plans/plan1"],
    );
  }

  // The plan row stores ids; the page printed them. "device:9f1f4a1e-…" tells
  // the continuity owner nothing about which machine is in scope.
  it("names the scoped device and the referenced procedure rather than printing ids", async () => {
    api.getPlan = vi.fn().mockResolvedValue({
      ...(PLANS[0] as PlanOut),
      scope: [{ kind: "device", id: "9f1f4a1e-0000-4000-8000-000000000001" }],
      procedure_refs: ["3c2b1a09-0000-4000-8000-000000000002"],
    });
    api.listDevices = vi.fn().mockResolvedValue({
      items: [
        {
          id: "9f1f4a1e-0000-4000-8000-000000000001",
          hostname: "WKS-042",
          fqdn: null,
          os_family: "windows",
          agent_status: "online",
        } as unknown as Device,
      ],
      total: 1,
    });
    api.listArticles = vi.fn().mockResolvedValue({
      items: [
        {
          id: "3c2b1a09-0000-4000-8000-000000000002",
          title: "Datacenter failover runbook",
          category: "runbook",
          archived: false,
          updated_at: "2026-02-01T00:00:00.000Z",
          link_count: 0,
        },
      ],
      total: 1,
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    // The scope and the procedures are a caption/value list now (v3's `kv`),
    // so the caption and the name sit in two cells; the name is what the
    // finding is about.
    expect(await screen.findByText(/WKS-042/)).toBeInTheDocument();
    expect(screen.getByText("Scope")).toBeInTheDocument();
    expect(screen.getByText(/Datacenter failover runbook/)).toBeInTheDocument();
    expect(screen.getByText("Procedures")).toBeInTheDocument();
    expect(screen.queryByText(/9f1f4a1e/)).not.toBeInTheDocument();
  });

  // CYCLE 9. A group-scoped row read "group core", the bare id -- the same
  // finding as the device row above, on the other scope kind.
  it("names a scoped device group rather than printing its id", async () => {
    api.getPlan = vi.fn().mockResolvedValue({
      ...(PLANS[0] as PlanOut),
      scope: [{ kind: "group", id: "06a85c19-55e2-7f3c-8000-831c7bdba357" }],
    });
    api.listDeviceGroups = vi.fn().mockResolvedValue({
      items: [
        {
          id: "06a85c19-55e2-7f3c-8000-831c7bdba357",
          name: "Core datacenter",
          parent_group_id: null,
          description: "",
          dynamic: false,
          device_count: 12,
          precedence: 0,
          archived_at: null,
        },
      ],
      total: 1,
      ungrouped_device_count: 0,
      evaluation: null,
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    await screen.findByText("Scope");
    // `dd`, not the page title: both contain "Core datacenter".
    expect(screen.getByText("Core datacenter", { selector: "dd" })).toBeInTheDocument();
    expect(screen.queryByText(/06a85c19-55e2-7f3c-8000-831c7bdba357/)).not.toBeInTheDocument();
  });

  // AUDIT (MEDIUM): "'Archive' sits beside 'Edit plan' with no confirmation...
  // The three header buttons are visually identical."
  it("does not archive on the first click", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Archive" }));

    expect(api.updatePlan).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog", { name: "Archive this continuity plan" });
    // The subject is what lets somebody notice they have the wrong plan open.
    expect(dialog).toHaveTextContent("Core datacenter failover");
  });

  it("archives once the dialog is confirmed", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Archive" }));
    await user.click(await screen.findByRole("button", { name: "Archive plan" }));

    await waitFor(() =>
      expect(api.updatePlan).toHaveBeenCalledWith("plan1", { state: "archived" }),
    );
  });

  it("leaves the plan alone when the archive dialog is dismissed", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Archive" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(api.updatePlan).not.toHaveBeenCalled();
  });

  // Restoring a plan takes nothing away, so a dialog there is friction that
  // buys nothing — the asymmetry is deliberate and pinned.
  it("unarchives without asking", async () => {
    const user = userEvent.setup();
    api.getPlan = vi.fn().mockResolvedValue({ ...(PLANS[0] as PlanOut), state: "archived" });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Unarchive" }));

    await waitFor(() => expect(api.updatePlan).toHaveBeenCalledWith("plan1", { state: "active" }));
  });

  // Recording a review stamps the date and the approver onto a compliance
  // record, writes it to the immutable audit log, and cannot be withdrawn.
  it("asks before recording a review", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Record review" }));
    expect(api.reviewPlan).not.toHaveBeenCalled();

    // Two controls read "Record review" once the dialog is open — the header
    // button and the dialog's own confirm. Scope the query rather than
    // guessing at order.
    const dialog = await screen.findByRole("dialog", { name: "Record a review of this plan" });
    expect(dialog).toHaveTextContent("Core datacenter failover");
    await user.click(within(dialog).getByRole("button", { name: "Record review" }));

    await waitFor(() => expect(api.reviewPlan).toHaveBeenCalledWith("plan1"));
  });
});

// --------------------------------------------------------------------------
// UX remediation cycle 6 — the two id fields that could not be typed correctly
// --------------------------------------------------------------------------

// --------------------------------------------------------------------------
// Cycle 12 — the residue of B84 and B87, and the wire vocabulary
// --------------------------------------------------------------------------

describe("BCP vocabulary and field marking (cycle 12)", () => {
  beforeEach(() => {
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME_NY),
      getPlan: vi.fn().mockResolvedValue(PLANS[0]),
      listPlanTests: vi
        .fn()
        .mockResolvedValue([{ ...TEST_FIXTURE, type: "restore_drill", outcome: "pending" }]),
      listPlans: vi.fn().mockResolvedValue(PLANS),
      planStatus: vi.fn().mockResolvedValue([]),
      listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      createTest: vi.fn(),
    } as unknown as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/bcp/plans/:planId" element={<PlanDetail />} />
      </Routes>,
      ["/bcp/plans/plan1"],
    );
  }

  // B84 [BLOCKER] closed the round trip in cycle 5; its remaining half is the
  // auditor's own instruction: "label every datetime input and output with that
  // zone". A correctly stored 09:00 that nobody can attribute to a clock is
  // still a DR test two teams turn up to at different hours.
  it("names the clock the DR test is scheduled in", async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(await screen.findByRole("button", { name: "New test" }));
    expect(screen.getByText("Times are in America/New_York.")).toBeInTheDocument();
  });

  // B87 [MEDIUM]: "All six controls report `required === false`."
  it("marks the two fields that really are required, and says what the mark means", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <Routes>
        <Route path="/bcp/plans/:planId" element={<PlanDetail />} />
      </Routes>,
      ["/bcp/plans/plan1"],
    );
    await user.click(await screen.findByRole("button", { name: "Edit plan" }));

    expect(screen.getByText("Fields marked * are required.")).toBeInTheDocument();
    expect(screen.getByLabelText("Name *")).toBeRequired();
    expect(screen.getByLabelText("Review interval (days) *")).toBeRequired();
    // And RTO/RPO are NOT marked: the API accepts them empty, and marking a
    // field the server does not insist on is the same lie pointing the other
    // way.
    expect(screen.getByLabelText("RTO (minutes)")).not.toBeRequired();
    expect(screen.getByLabelText("RPO (minutes)")).not.toBeRequired();
  });

  it("reads a DR test's type and outcome as words, not as wire values", async () => {
    renderDetail();
    expect(await screen.findByText("Restore drill")).toBeInTheDocument();
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(screen.queryByText("restore_drill")).not.toBeInTheDocument();
  });

  it("offers the test types as words while sending the backend's values", async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(await screen.findByRole("button", { name: "New test" }));
    const select = screen.getByLabelText("Type") as HTMLSelectElement;
    expect([...select.options].map((option) => option.textContent)).toEqual([
      "Restore drill",
      "Failover",
      "Tabletop",
      "Other",
    ]);
    expect([...select.options].map((option) => option.value)).toEqual([
      "restore_drill",
      "failover",
      "tabletop",
      "other",
    ]);
  });
});

describe("bcp picker options", () => {
  it("names a device by hostname and distinguishes it by os and agent state", () => {
    const [option] = scopeDevicePickerOptions([
      {
        id: "dev1",
        hostname: "WKS-042",
        fqdn: null,
        os_family: "windows",
        os_version: "11",
        architecture: "x86_64",
        device_group_id: null,
        status: "active",
        agent_status: "offline",
        last_seen_at: null,
        cert_not_after: null,
        cert_state: null,
      } as unknown as Device,
    ]);
    expect(option?.label).toBe("WKS-042");
    expect(option?.sublabel).toBe("windows · offline");
  });

  it("distinguishes two articles of the same title by category and flags archived ones", () => {
    const options = procedurePickerOptions(
      [
        {
          id: "doc1",
          client_id: "o1",
          title: "Failover",
          category: "runbook",
          archived: false,
          updated_at: "2026-02-01T00:00:00.000Z",
          link_count: 0,
        },
        {
          id: "doc2",
          client_id: "o1",
          title: "Failover",
          category: "policy",
          archived: true,
          updated_at: "2026-02-01T00:00:00.000Z",
          link_count: 0,
        },
      ],
      "America/New_York",
    );
    expect(options[0]?.sublabel).toContain("runbook");
    expect(options[1]?.sublabel).toContain("archived");
    // The reader's zone, not the runner's: 00:00 UTC on 1 February is 31 JAN.
    expect(options[0]?.sublabel).toContain("31 Jan 2026");
  });
});

describe("PlanEditor scope and procedure references", () => {
  let api: Partial<ApiClient>;

  const DEVICE = {
    id: "9f1f4a1e-0000-4000-8000-000000000001",
    hostname: "WKS-042",
    fqdn: null,
    os_family: "windows",
    os_version: "11",
    architecture: "x86_64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
  } as unknown as Device;

  const ARTICLE = {
    id: "3c2b1a09-0000-4000-8000-000000000002",
    title: "Datacenter failover runbook",
    category: "runbook",
    archived: false,
    updated_at: "2026-02-01T00:00:00.000Z",
    link_count: 0,
  };

  const GROUP = {
    id: "06a85c19-55e2-7f3c-8000-831c7bdba357",
    name: "Core datacenter",
    parent_group_id: null,
    description: "",
    dynamic: false,
    device_count: 12,
    precedence: 0,
    archived_at: null,
  };

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      createPlan: vi.fn().mockResolvedValue(PLANS[0]),
      listDevices: vi.fn().mockResolvedValue({ items: [DEVICE], total: 1 }),
      listDeviceGroups: vi.fn().mockResolvedValue({
        items: [GROUP],
        total: 1,
        ungrouped_device_count: 0,
        evaluation: null,
      }),
      listArticles: vi.fn().mockResolvedValue({ items: [ARTICLE], total: 1 }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderEditor() {
    const user = userEvent.setup();
    renderWithProviders(<PlanEditor plan="new" onClose={vi.fn()} />);
    return user;
  }

  // `_SCOPE_KIND_TO_MODEL` is {"device": Device, "group": DeviceGroup} — the
  // dialog seeded every new row with "device_group", so adding a scope entry
  // and saving could only ever produce a 400.
  it("offers only the two scope kinds the server accepts", async () => {
    const user = renderEditor();

    await user.click(screen.getByRole("button", { name: "Add scope entry" }));

    const kinds = [...screen.getByLabelText("Scope 1 kind").querySelectorAll("option")].map(
      (option) => option.getAttribute("value"),
    );
    expect(kinds).toEqual(["device", "group"]);
    expect(kinds).not.toContain("device_group");
  });

  it("scopes a plan to a device chosen from the list, never typed", async () => {
    const user = renderEditor();

    await user.type(screen.getByLabelText("Name *"), "Core datacenter failover");
    await user.click(screen.getByRole("button", { name: "Add scope entry" }));
    await user.click(screen.getByRole("combobox", { name: "Scope 1 device" }));
    await user.click(await screen.findByRole("option", { name: /WKS-042/ }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(api.createPlan).toHaveBeenCalledWith(
        expect.objectContaining({ scope: [{ kind: "device", id: DEVICE.id }] }),
      ),
    );
  });

  // CYCLE 9. The pasted "Scope N group id" field is a picker now, fed by the
  // same endpoint the Device groups page lists from.
  it("scopes a plan to a device group chosen from the list, never typed", async () => {
    const user = renderEditor();

    await user.type(screen.getByLabelText("Name *"), "Core datacenter failover");
    await user.click(screen.getByRole("button", { name: "Add scope entry" }));
    await user.selectOptions(screen.getByLabelText("Scope 1 kind"), "group");
    await user.click(screen.getByRole("combobox", { name: "Scope 1 group" }));
    await user.click(await screen.findByRole("option", { name: /Core datacenter/ }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(api.createPlan).toHaveBeenCalledWith(
        expect.objectContaining({ scope: [{ kind: "group", id: GROUP.id }] }),
      ),
    );
  });

  it("names an unfilled scope row on submit instead of sending it to a 400", async () => {
    const user = renderEditor();

    await user.type(screen.getByLabelText("Name *"), "Core datacenter failover");
    await user.click(screen.getByRole("button", { name: "Add scope entry" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Scope 1 .* is required/);
    expect(api.createPlan).not.toHaveBeenCalled();
  });

  // AUDIT (MEDIUM): "procedure refs are a comma-separated free-text string".
  // Worse than dirty: `_validate_procedure_refs` resolves every entry against
  // `doc_articles.id`, so anything typed there made the plan unsaveable.
  it("references a procedure by choosing a documentation article", async () => {
    const user = renderEditor();

    await user.type(screen.getByLabelText("Name *"), "Core datacenter failover");
    await user.click(screen.getByRole("combobox", { name: "Add a procedure" }));
    await user.click(await screen.findByRole("option", { name: /Datacenter failover runbook/ }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(api.createPlan).toHaveBeenCalledWith(
        expect.objectContaining({ procedure_refs: [ARTICLE.id] }),
      ),
    );
  });

  it("shows the chosen procedure by title rather than by id", async () => {
    const user = renderEditor();

    await user.click(screen.getByRole("combobox", { name: "Add a procedure" }));
    await user.click(await screen.findByRole("option", { name: /Datacenter failover runbook/ }));

    expect(screen.getAllByText("Datacenter failover runbook").length).toBeGreaterThan(0);
  });

  // Cycle 7: a picker that is empty on a fresh install and says nothing is the
  // blocked flow wearing a nicer control. Both halves are asserted — the hint
  // inside the list, and the link that puts the screen which fills it one
  // click away.
  it("says where procedures come from when there are none, and links to it", async () => {
    api.listArticles = vi.fn().mockResolvedValue({ items: [], total: 0 });
    mockUseApi.mockReturnValue(api as ApiClient);
    const user = renderEditor();

    expect(
      await screen.findByRole("link", { name: /Write the recovery procedure in IT documentation/ }),
    ).toHaveAttribute("href", "/docs");

    await user.click(screen.getByRole("combobox", { name: "Add a procedure" }));
    // The hint no longer names a menu path ("Governance → IT documentation"
    // stopped matching the navigation); the link above is the way there.
    expect(await screen.findByText(/No documentation articles to reference yet/)).toBeInTheDocument();
    expect(screen.queryByText(/Governance →/)).toBeNull();
  });

  it("links an empty device scope list to adding a computer", async () => {
    api.listDevices = vi.fn().mockResolvedValue({ items: [], total: 0 });
    mockUseApi.mockReturnValue(api as ApiClient);
    const user = renderEditor();

    // Nothing to fix until a scope row asks for a device.
    expect(screen.queryByRole("link", { name: /Add a computer/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add scope entry" }));

    expect(await screen.findByRole("link", { name: /Add a computer/ })).toHaveAttribute(
      "href",
      "/devices/onboard",
    );
    expect(screen.queryByText(/Devices → Enrollment/)).toBeNull();
  });

  it("links an empty group scope list to creating a device group", async () => {
    api.listDeviceGroups = vi.fn().mockResolvedValue({
      items: [],
      total: 0,
      ungrouped_device_count: 0,
      evaluation: null,
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    const user = renderEditor();

    await user.click(screen.getByRole("button", { name: "Add scope entry" }));
    // A device row does not ask about groups.
    expect(screen.queryByRole("link", { name: /Create a device group/ })).toBeNull();
    await user.selectOptions(screen.getByLabelText("Scope 1 kind"), "group");

    expect(await screen.findByRole("link", { name: /Create a device group/ })).toHaveAttribute(
      "href",
      "/device-groups",
    );
  });

  it("explains RTO and RPO on the form rather than assuming the reader knows", async () => {
    renderEditor();

    expect(screen.getByText(/RTO — how long recovery may take/)).toBeInTheDocument();
    expect(screen.getByText(/RPO — how much recent data may be lost/)).toBeInTheDocument();
  });

  it("warns when RTO is lower than RPO without refusing the save", async () => {
    const user = renderEditor();

    await user.type(screen.getByLabelText("Name *"), "Core datacenter failover");
    await user.type(screen.getByLabelText("RTO (minutes)"), "5");
    await user.type(screen.getByLabelText("RPO (minutes)"), "60");

    expect(await screen.findByRole("status")).toHaveTextContent(/RTO is lower than RPO/);

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(api.createPlan).toHaveBeenCalledWith(
        expect.objectContaining({ rto_minutes: 5, rpo_minutes: 60 }),
      ),
    );
  });
});

// --------------------------------------------------------------------------
// UX remediation cycles 3 and 8, on the DR test record
// --------------------------------------------------------------------------

describe("TestRecorder", () => {
  let api: Partial<ApiClient>;

  const RECORDED: TestOut = {
    ...TEST_FIXTURE,
    executed_at: "2026-01-15T19:00:00.000Z",
    outcome: "pass",
  };

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NY),
      getTest: vi.fn().mockResolvedValue(TEST_FIXTURE),
      listFindings: vi.fn().mockResolvedValue([]),
      recordTest: vi.fn().mockResolvedValue(RECORDED),
      upsertStep: vi.fn().mockResolvedValue({
        id: "step1",
        test_id: "test1",
        ordinal: 1,
        description: "Restore the file server",
        result: "pending",
        evidence_ref: null,
      }),
      createFinding: vi.fn().mockResolvedValue(FINDING_FIXTURE),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderRecorder() {
    return renderWithProviders(
      <Routes>
        <Route path="/bcp/tests/:testId" element={<TestRecorder />} />
      </Routes>,
      ["/bcp/tests/test1"],
    );
  }

  // REVIEW WAVE, 390x844: the same clipping as the period fields on
  // `/bcp/report` -- a `datetime-local` in a 150px grid column loses the
  // browser's calendar-picker button off its right edge, so the picker cannot
  // be opened on a handset at all. `full` gives the field the row.
  it("gives the executed-at field the whole row, so its picker is reachable", async () => {
    renderRecorder();

    const field = (await screen.findByLabelText("Executed at")).closest(".field");
    expect(field).not.toBeNull();
    expect(field).toHaveClass("full");
  });

  // REVIEW WAVE ROUND 3, 1440x900: `full` plus a flat `18rem` cap rendered a
  // ~287px control over three selects filling their 553px columns, so the
  // outcome form opened on a stub. The cap is now one grid column wide
  // (`calc(50% - 7px)`, the 14px gap halved) with the 18rem floor kept for the
  // 390 case above, where that expression collapses to ~150px.
  // FALSIFIER: restore the flat `18rem` and this fails.
  it("caps the executed-at control at one grid column, not at a fixed stub", async () => {
    renderRecorder();

    const input = await screen.findByLabelText("Executed at");
    expect(input.style.maxWidth).toBe("max(18rem, calc(50% - 7px))");
  });

  // The first recording creates the evidence and asking then would be
  // friction, so the dialog is deliberately absent here.
  it("records an unexecuted test on the first click, without a dialog", async () => {
    const user = userEvent.setup();
    renderRecorder();

    await user.click(await screen.findByRole("button", { name: "Record test" }));

    await waitFor(() => expect(api.recordTest).toHaveBeenCalled());
  });

  // `record_test` OVERWRITES `test.evidence` with a snapshot taken now: the
  // frozen proof of what backup coverage looked like when the drill ran is
  // destroyed, and nothing keeps a copy.
  it("asks before replacing the frozen evidence of a test already recorded", async () => {
    const user = userEvent.setup();
    api.getTest = vi.fn().mockResolvedValue(RECORDED);
    mockUseApi.mockReturnValue(api as ApiClient);
    renderRecorder();

    await user.click(await screen.findByRole("button", { name: "Record test" }));
    expect(api.recordTest).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog", { name: "Re-record this DR test" });
    expect(dialog).toHaveTextContent(/cannot be recovered/);

    await user.click(within(dialog).getByRole("button", { name: "Replace the evidence" }));
    await waitFor(() => expect(api.recordTest).toHaveBeenCalled());
  });

  // Cycle 3, two more instances of the same dead control:
  // `disabled={upsertStep.isPending || !description}` and
  // `disabled={createFinding.isPending || !description}`.
  it("names the empty step description instead of refusing the click", async () => {
    const user = userEvent.setup();
    renderRecorder();

    const save = await screen.findByRole("button", { name: "Save step" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Step description is required",
    );
    expect(api.upsertStep).not.toHaveBeenCalled();
  });

  it("refuses a step whose ordinal is not a whole number", async () => {
    // `Number("") === 0` would silently rewrite step 0, and the ordinal is the
    // key `upsert_step` matches on.
    const user = userEvent.setup();
    renderRecorder();

    await user.type(await screen.findByLabelText("Step description"), "Restore the file server");
    await user.clear(screen.getByLabelText("Step number"));
    await user.click(screen.getByRole("button", { name: "Save step" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Step number is required");
    expect(api.upsertStep).not.toHaveBeenCalled();
  });

  it("saves a valid step", async () => {
    const user = userEvent.setup();
    renderRecorder();

    await user.type(await screen.findByLabelText("Step description"), "Restore the file server");
    await user.click(screen.getByRole("button", { name: "Save step" }));

    await waitFor(() =>
      expect(api.upsertStep).toHaveBeenCalledWith("test1", {
        ordinal: 1,
        description: "Restore the file server",
        result: "pending",
        evidence_ref: null,
      }),
    );
  });

  it("names the empty finding description instead of refusing the click", async () => {
    const user = userEvent.setup();
    renderRecorder();

    const add = await screen.findByRole("button", { name: "Add finding" });
    expect(add).toBeEnabled();
    await user.click(add);

    const alerts = await screen.findAllByRole("alert");
    expect(
      alerts.some((el) => el.textContent?.includes("Finding description is required")),
    ).toBe(true);
    expect(api.createFinding).not.toHaveBeenCalled();
  });
});
