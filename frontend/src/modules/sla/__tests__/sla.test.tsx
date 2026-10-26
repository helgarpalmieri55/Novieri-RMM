import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me } from "../../../lib/api";
import type { QueueOut } from "../../service-desk/types";
import { SlaAdminPage } from "../SlaAdminPage";
import { TicketSlaPanel } from "../TicketSlaPanel";
import type { CalendarDetail, CalendarOut, HolidayOut, PolicyOut, SettingsOut, TicketSlaOut } from "../types";

// The auth-backed API client is mocked so these are pure component tests: no
// real fetch -- mirrors modules/notification/__tests__/notification.test.tsx.
const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWithProviders(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/sla"]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

// Spec §8 seeds both SLA permissions to admin roles only, so these are the
// two shapes the page's gating ever needs to tell apart.
const ME_READ_ONLY: Me = {
  user_id: "reader-1",
  org_id: "o1",
  email: "reader@example.com",
  display_name: "Reader",
  permissions: ["sla.read", "ticket.read"],
};

const ME_ADMIN: Me = {
  ...ME_READ_ONLY,
  user_id: "admin-1",
  display_name: "Admin",
  permissions: ["sla.read", "sla.admin", "ticket.read"],
};

// The same operator, with the `timezone` /me has carried since cycle 2 set to
// somewhere that is NOT the test runner's zone (the runner is UTC). Every
// datetime assertion below uses this rather than the bare fixture, because a
// zone equal to the browser's cannot tell "reads the profile" apart from
// "ignores the profile" -- which is exactly the bug cycle 5 exists to close.
const ME_IN_NEW_YORK: Me = { ...ME_READ_ONLY, timezone: "America/New_York" };

/**
 * Choose the calendar in the Add-policy dialog.
 *
 * WAS `selectOptions(getByLabelText("Calendar"), "cal1")`. The control is an
 * EntityPicker now (cycle 6) rather than a <select>, because the audit found
 * the policy form unusable on an install whose calendar list is empty or
 * unreadable — a native select cannot say why it has no options. A combobox is
 * driven by opening it and choosing, not by setting a value.
 *
 * `getByRole("combobox", ...)` rather than `getByLabelText`: the picker's own
 * listbox carries `aria-label` too, so the label query is ambiguous whenever
 * the list is open.
 */
async function pickCalendar(
  user: ReturnType<typeof userEvent.setup>,
  dialog: HTMLElement,
): Promise<void> {
  await user.click(within(dialog).getByRole("combobox", { name: "Calendar" }));
  await user.click(await within(dialog).findByRole("option", { name: /UK business hours/ }));
}

const CALENDARS: CalendarOut[] = [
  {
    id: "cal1",
    name: "UK business hours",
    timezone: "Europe/London",
    active: true,
    health: "ok",
    created_at: "2026-01-01T00:00:00.000Z",
  },
  {
    id: "cal2",
    name: "Broken calendar",
    timezone: "Europe/London",
    active: true,
    health: "no_working_windows",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

// A Friday evening shift ending at midnight -- the shape a naive
// `end > start` renderer shows as empty or inverted.
const CALENDAR_DETAIL: CalendarDetail = {
  ...(CALENDARS[0] as CalendarOut),
  windows: [
    { id: "w1", weekday: 0, start_time: "09:00:00", end_time: "17:00:00" },
    { id: "w2", weekday: 4, start_time: "22:00:00", end_time: "00:00:00" },
  ],
};

const HOLIDAYS: HolidayOut[] = [
  { id: "h1", calendar_id: "cal1", holiday_date: "2026-12-25", name: "Christmas Day" },
];

const QUEUES: QueueOut[] = [
  {
    id: "q1",
    key: "helpdesk",
    name: "Helpdesk",
    description: "",
    active: true,
    sort_order: 0,
    created_by: "admin-1",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

function policy(overrides: Partial<PolicyOut>): PolicyOut {
  return {
    id: "p",
    client_id: null,
    queue_id: null,
    priority: null,
    calendar_id: "cal1",
    response_target_minutes: 60,
    resolution_target_minutes: 480,
    escalate_priority: false,
    escalate_to_queue_id: null,
    active: true,
    created_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

const POLICIES: PolicyOut[] = [
  policy({ id: "catch-all" }),
  policy({ id: "helpdesk-any", queue_id: "q1", response_target_minutes: 30 }),
  policy({ id: "any-urgent", priority: "urgent", response_target_minutes: 15 }),
];

const SETTINGS: SettingsOut = {
  auto_close_after_days: 7,
  configured: false,
  tickets_without_sla: 4,
  stale_ticket_sla: 0,
};

function baseApi(me: Me, settings: SettingsOut = SETTINGS): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(me),
    listSlaCalendars: vi.fn().mockResolvedValue(CALENDARS),
    getSlaCalendar: vi.fn().mockResolvedValue(CALENDAR_DETAIL),
    listSlaCalendarHolidays: vi.fn().mockResolvedValue(HOLIDAYS),
    listSlaPolicies: vi.fn().mockResolvedValue(POLICIES),
    getSlaSettings: vi.fn().mockResolvedValue(settings),
    listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
  };
}

describe("SLA administration", () => {
  it("hides mutation controls without sla.admin", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    // The calendar's name appears both in the calendar list and as the
    // calendar column of every policy that points at it.
    await screen.findAllByText("UK business hours");
    expect(screen.queryByRole("button", { name: /add calendar/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /add policy/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /save settings/i })).toBeNull();
  });

  it("shows mutation controls with sla.admin", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_ADMIN) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    expect(await screen.findByRole("button", { name: /add calendar/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add policy/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save settings/i })).toBeInTheDocument();
  });

  it("names the timezone and the fault on a calendar that cannot produce a deadline", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    expect(await screen.findByText("Broken calendar")).toBeInTheDocument();
    expect(screen.getAllByText("Europe/London").length).toBeGreaterThan(0);
    expect(screen.getByText("No working windows")).toBeInTheDocument();
    expect(
      screen.getByText(/Broken calendar: This calendar has no working windows/),
    ).toBeInTheDocument();
  });

  it("surfaces the count of tickets with no SLA and the unconfigured default", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    // THE COUNT IS STATED ONCE. It used to be headlined as a page-level stat
    // card AND restated as a label-plus-pill row in the settings panel
    // directly beneath the identical tile; the tile is the survivor and it
    // carries the diagnosis the settings row existed to hang off. Scoping to
    // the tile is what makes this an assertion about the number rather than
    // about "4" appearing somewhere on the page.
    const tile = await screen.findByRole("link", { name: /Tickets with no SLA/ });
    // `findByText`, because the tile renders an em dash until `GET
    // /sla/settings` answers -- "not measured yet" and "measured 0" are
    // deliberately different renders on this page.
    expect(await within(tile).findByText("4")).toBeInTheDocument();
    expect(within(tile).getByText(/No policy matched them/)).toBeInTheDocument();
    expect(screen.getByText(/running on the default of 7 days/)).toBeInTheDocument();
  });

  it("surfaces stale deadlines separately from missing ones", async () => {
    // The two counts are complementary blindnesses and must not be conflated:
    // `tickets_without_sla` counts promises that are MISSING,
    // `stale_ticket_sla` promises that are PRESENT and derived from a
    // configuration that no longer exists (a policy or calendar edit whose
    // recompute failed on those rows while the edit returned 200). The
    // fixture gives them DIFFERENT values, so a panel that renders one number
    // twice fails.
    mockUseApi.mockReturnValue(
      baseApi(ME_READ_ONLY, { ...SETTINGS, stale_ticket_sla: 2 }) as ApiClient,
    );
    renderWithProviders(<SlaAdminPage />);

    // Each count is stated once, on its own tile (see the test above); the
    // scoping is what proves the two are not one number rendered twice.
    const staleTile = await screen.findByRole("link", { name: /Deadlines out of date/ });
    const missingTile = screen.getByRole("link", { name: /Tickets with no SLA/ });
    expect(await within(staleTile).findByText("2")).toBeInTheDocument();
    expect(within(missingTile).getByText("4")).toBeInTheDocument();
    expect(
      screen.getByText(/policy or calendar edit could not be applied to them/),
    ).toBeInTheDocument();
  });

  it("renders policy wildcards as 'any', never as a blank cell", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    // "Helpdesk" legitimately renders twice once queues have resolved: once
    // as the policy table's resolved queue name, once as an option in the
    // policy resolver's queue picker (PolicyResolverPanel) -- both are real,
    // so this waits for and asserts the resolved name rather than assuming
    // it appears exactly once.
    expect((await screen.findAllByText("Helpdesk")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Any queue").length).toBeGreaterThan(0);
    // THE PRIORITY WILDCARD IS THE ROW'S KIND PILL. The cell led with a pill
    // reading "Any" and then repeated "Any priority" beside it, so the same
    // word was drawn twice on every row; the pill is the survivor and the
    // line next to it now carries the other half of the policy's identity,
    // its queue. Still "any", still never a blank cell -- which is what this
    // test is for.
    const wildcard = screen
      .getAllByRole("row")
      .find((row) => within(row).queryByText("Any queue") != null);
    expect(wildcard).toBeDefined();
    expect(within(wildcard as HTMLElement).getByText("Any")).toBeInTheDocument();
  });

  // The confusing case the resolver exists for: `(Helpdesk, any priority)`
  // and `(any queue, urgent)` both look applicable to an urgent Helpdesk
  // ticket, and only the queue-scoped one is ever used.
  it("shows which policy wins for a queue+priority and which are shadowed", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    await screen.findByText("Which policy applies?");
    // Wait for the cross-module queue read to populate the picker.
    await screen.findByRole("option", { name: "Helpdesk" });
    await user.selectOptions(screen.getByLabelText("Queue"), "q1");
    await user.selectOptions(screen.getByLabelText("Priority"), "urgent");

    expect(await screen.findByText(/Resolved at level 2/)).toBeInTheDocument();
    // Named both in the summary line and in the level table.
    expect(screen.getAllByText(/this queue \+ any priority/).length).toBe(2);
    expect(screen.getAllByText("Shadowed by a more specific level").length).toBe(2);
    expect(screen.getByText("Applies")).toBeInTheDocument();
  });

  // Review wave round 3 (harmony): the panel's only two controls sat in a
  // flex row at hardcoded widths (256 and 192), so they wrapped onto separate
  // lines at two different widths and shared no edge, beside a panel whose
  // number field fills its column. `.form-grid` is the kit's own two-column
  // field row, and a `<select>` wears the class the kit's drop-arrow rule is
  // written against.
  it("lays the resolver's two fields out as one field row, on the kit's classes", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    await screen.findByText("Which policy applies?");
    // Wait for the cross-module queue read, as the resolver test above does:
    // until it lands the panel is still showing its loading note.
    await screen.findByRole("option", { name: "Helpdesk" });
    const queue = screen.getByLabelText("Queue");
    const priority = screen.getByLabelText("Priority");
    expect(queue.className).toContain("select-field");
    expect(priority.className).toContain("select-field");
    // Same grid, so the same cell width: one edge, whatever the option text.
    const row = queue.closest(".form-grid");
    expect(row).not.toBeNull();
    expect(priority.closest(".form-grid")).toBe(row);
    // And neither field carries a width of its own any more.
    for (const field of [queue, priority]) {
      expect(field.closest(".field")?.getAttribute("style") ?? "").toBe("");
    }
  });

  it("degrades quietly when the calendars read 403s", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listSlaCalendars = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    expect(
      await screen.findByText("Calendars not available (insufficient permissions)."),
    ).toBeInTheDocument();
    // The sibling panels are independent.
    expect(screen.getByText("SLA policies")).toBeInTheDocument();
  });

  it("falls back to free-text queue ids when the cross-module queue read 403s", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.listTicketQueues = vi.fn().mockRejectedValue(new ApiError(403, "forbidden"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    await user.click(await screen.findByRole("button", { name: /add policy/i }));

    // WAS: two free-text "Queue ID" boxes plus an "Escalation queue ID" box.
    // Cycle 6 replaced the dialog's pair with pickers, because a free-text
    // UUID box is not a fallback -- a mistyped id is not a validation message,
    // it is the right policy silently attached to the wrong queue. Without
    // `ticket.read` there are no queues to offer, so the pickers say which
    // permission would produce them and the policy can still be saved against
    // Any queue.
    //
    // The narrowing is real and deliberate: an administrator who lacks
    // `ticket.read` can no longer scope a policy to a queue by typing its id.
    // An honest empty list was judged better than a box that accepts anything.
    expect(screen.queryAllByPlaceholderText("Queue ID")).toHaveLength(1); // the resolver's
    const dialog = screen.getByRole("dialog", { name: "Add policy" });
    expect(
      within(dialog).getByRole("combobox", { name: "Queue (blank = any queue)" }),
    ).toBeInTheDocument();
    // The explanation is stated above the two pickers rather than left to
    // `emptyHint`, which cannot fire while each list carries its own default
    // option.
    expect(within(dialog).getByText(/need permission to view tickets/)).toBeInTheDocument();
  });

  // ------------------------------------------------------------------
  // The three states a mock never has (UI port spec §6/§8): loading, empty
  // and error must be DISTINGUISHABLE, not all render as a blank table.
  // ------------------------------------------------------------------

  it("says the calendars list is loading rather than showing an empty table", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listSlaCalendars = vi.fn().mockImplementation(() => new Promise<never>(() => {}));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(/not available/i)).toBeNull();
  });

  // FALSIFIED GUARD: before CalendarsPanel gained a `failed` branch, a
  // non-403 error rendered nothing -- no 403 text (it only fires on
  // `instanceof ApiError && status === 403`), no row, indistinguishable from
  // both "still loading" and "no calendars configured". This is the test
  // that failed against the pre-fix component (confirmed in the session
  // report): reverting CalendarsPanel's `failed` branch times this test out.
  it("says the calendars list did NOT load on a genuine failure, distinctly from a 403 or an empty list", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listSlaCalendars = vi.fn().mockRejectedValue(new Error("boom"));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("Calendars not available (insufficient permissions).")).toBeNull();
    expect(screen.queryByText(/No calendars yet/i)).toBeNull();
  });
});

describe("the weekly working-window editor", () => {
  async function openHours(api: Partial<ApiClient>) {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);
    const openers = await screen.findAllByRole("button", { name: /hours & holidays/i });
    await user.click(openers[0] as HTMLElement);
    await screen.findByText("Working hours");
    return user;
  }

  // A window ending at 00:00 is midnight at the END of the day (1440), not a
  // zero-length window. Rendering it as empty is the specific bug this
  // asserts against.
  it("renders a midnight-ended window as 22:00–24:00 with its real duration", async () => {
    await openHours(baseApi(ME_ADMIN));

    expect(screen.getByText(/22:00–24:00 · 2h/)).toBeInTheDocument();
    expect(screen.getByText("ends at midnight (24:00)")).toBeInTheDocument();
    // 8h Monday + 2h Friday, and the Friday window is NOT counted as zero.
    expect(screen.getByText("10h of working time per week")).toBeInTheDocument();
  });

  it("replaces the whole week in one call, preserving the midnight sentinel", async () => {
    const api = baseApi(ME_ADMIN);
    const replace = vi.fn().mockResolvedValue([]);
    api.replaceSlaCalendarWindows = replace;
    const user = await openHours(api);

    await user.click(screen.getByRole("button", { name: "Add window (Tuesday)" }));
    await user.click(screen.getByRole("button", { name: /save week/i }));

    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith("cal1", [
        { weekday: 0, start_time: "09:00:00", end_time: "17:00:00" },
        { weekday: 4, start_time: "22:00:00", end_time: "00:00:00" },
        { weekday: 1, start_time: "09:00:00", end_time: "17:00:00" },
      ]),
    );
  });

  it("refuses to submit an inverted window and says why", async () => {
    const api = baseApi(ME_ADMIN);
    const replace = vi.fn().mockResolvedValue([]);
    api.replaceSlaCalendarWindows = replace;
    const user = await openHours(api);

    const start = screen.getByLabelText("Monday window 1 start");
    await user.clear(start);
    await user.type(start, "18:00");

    expect(
      await screen.findByText(/end 00:00 means midnight at the end of the day/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save week/i })).toBeDisabled();
    expect(replace).not.toHaveBeenCalled();
  });

  it("shows the calendar's holidays alongside its hours", async () => {
    await openHours(baseApi(ME_ADMIN));
    expect(await screen.findByText("Christmas Day")).toBeInTheDocument();
    expect(screen.getByText("25 Dec 2026")).toBeInTheDocument();
  });

  // A HOLIDAY IS A CALENDAR DATE, NOT AN INSTANT. "2026-12-25" parses as
  // midnight UTC, so rendering it in the READER's zone moves Christmas Day to
  // the 24th for everyone west of Greenwich -- the "off by a whole day"
  // failure `lib/datetime` warns about, and one an admin configuring a
  // business calendar would not spot until the wrong day was non-working.
  // The zone is pinned to UTC here on purpose, and this is the test that says
  // so: the operator's profile is five hours west and the date does not move.
  it("does not shift a holiday date into the reader's zone", async () => {
    await openHours(baseApi(ME_IN_NEW_YORK));
    expect(await screen.findByText("25 Dec 2026")).toBeInTheDocument();
    expect(screen.queryByText("24 Dec 2026")).toBeNull();
  });

  // ------------------------------------------------------------------
  // Cycle 12 — the two geometry findings on this editor.
  // ------------------------------------------------------------------

  it("names the empty holiday fields instead of swallowing the click", async () => {
    // Auditor B, HIGH, third of the three dead clicks: "'Add holiday' with an
    // empty Holiday date -> nothing happens, holiday table still says 'No
    // holidays on this calendar.'" The other two (Add calendar, Add policy)
    // are pinned below; this is the one that shared their defect and not
    // their coverage.
    const api = baseApi(ME_ADMIN);
    api.addSlaCalendarHoliday = vi.fn().mockResolvedValue(HOLIDAYS[0]);
    const user = await openHours(api);

    const add = screen.getByRole("button", { name: "Add holiday" });
    expect(add).toBeEnabled();
    await user.click(add);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Holiday date is required");
    expect(alert).toHaveTextContent("Holiday name is required");
    expect(api.addSlaCalendarHoliday).not.toHaveBeenCalled();
  });

  it("gives the time inputs room for a meridiem instead of clipping it", async () => {
    // Auditor B: the start/end inputs rendered "12:00 AM" with the trailing
    // character cut off by the input's right edge and the clock glyph
    // overlapping the text -- "reads 12:00 AN". AM/PM is precisely the
    // distinction that decides whether every deadline on the calendar is
    // twelve hours out. jsdom lays nothing out, so the pin is on the width
    // token itself: `w-28` (112px) is the measurement that clipped.
    await openHours(baseApi(ME_ADMIN));

    for (const name of ["Monday window 1 start", "Monday window 1 end"]) {
      const input = screen.getByLabelText(name);
      expect(input.className).not.toContain("w-28");
      expect(input.className).toContain("w-36");
    }
  });

  it("labels the day-row actions identically and keeps the day in the accessible name", async () => {
    // Auditor B: every row's buttons embedded the weekday, so the pair's width
    // varied per row and their right-aligned left edge stepped in and out by
    // ~40px down the list. The day name is already the row header on screen --
    // and is NOT available to a screen-reader user moving button to button, so
    // it stays in the accessible name.
    await openHours(baseApi(ME_ADMIN));

    const monday = screen.getByRole("button", { name: "Add window (Monday)" });
    const wednesday = screen.getByRole("button", { name: "Add window (Wednesday)" });
    // Same visible text on every row: one scan column, not seven widths.
    expect(monday).toHaveTextContent(/^Add window$/);
    expect(wednesday).toHaveTextContent(/^Add window$/);
    expect(screen.getByRole("button", { name: "Add 24-hour day (Monday)" })).toHaveTextContent(
      /^24-hour day$/,
    );
  });

  it("offers no window controls without sla.admin", async () => {
    await openHours(baseApi(ME_READ_ONLY));
    expect(screen.queryByRole("button", { name: /save week/i })).toBeNull();
    expect(screen.getByLabelText("Monday window 1 start")).toBeDisabled();
  });
});

// --------------------------------------------------------------------------
// The ticket-detail panel -- where a wrong state is a prioritisation error
// --------------------------------------------------------------------------

function ticketSla(overrides: Partial<TicketSlaOut>): TicketSlaOut {
  return {
    ticket_id: "t1",
    has_sla: true,
    response_state: "running",
    resolution_state: "running",
    policy_id: "p1",
    calendar_id: "cal1",
    response_due_at: null,
    resolution_due_at: null,
    response_met_at: null,
    resolution_met_at: null,
    response_breached_at: null,
    resolution_breached_at: null,
    paused_at: null,
    paused_minutes_total: 0,
    voided_at: null,
    escalated_at: null,
    policy_source: "initial",
    ...overrides,
  };
}

// Offsets are computed from the render-time clock with a small buffer, so the
// floor in `formatDurationMs` lands on a stable value without fake timers.
function fromNow(ms: number): string {
  return new Date(Date.now() + ms).toISOString();
}

const HOUR = 3_600_000;

// `me` is mocked alongside the SLA read because the panel now asks the profile
// which zone to render its deadlines in (cycle 5). It is the SAME `["me"]`
// query the surrounding ticket page already holds, so this costs no request in
// the app -- but a component test mounts the panel alone and must supply it.
function renderTicketSla(state: TicketSlaOut, me: Me = ME_READ_ONLY) {
  mockUseApi.mockReturnValue({
    getTicketSla: vi.fn().mockResolvedValue(state),
    me: vi.fn().mockResolvedValue(me),
  } as unknown as ApiClient);
  return renderWithProviders(<TicketSlaPanel ticketId="t1" />);
}

describe("ticket SLA panel", () => {
  it("counts a running clock down", async () => {
    renderTicketSla(
      ticketSla({
        response_due_at: fromNow(2 * HOUR + 30_000),
        resolution_due_at: fromNow(8 * HOUR + 30_000),
      }),
    );

    expect(await screen.findByText("2h remaining")).toBeInTheDocument();
    expect(screen.getByText("8h remaining")).toBeInTheDocument();
  });

  it("says past due without claiming a breach the server has not recorded", async () => {
    renderTicketSla(
      ticketSla({ response_due_at: fromNow(-HOUR - 30_000), resolution_state: "running" }),
    );

    expect(await screen.findByText("1h past due")).toBeInTheDocument();
    expect(screen.getByText(/has not recorded a breach yet/)).toBeInTheDocument();
    expect(screen.queryByText("Breached")).toBeNull();
  });

  // A paused clock must be VISIBLY paused, and must never look like it is
  // still counting down towards the stored due instant.
  it("shows a paused clock as stopped, with no countdown", async () => {
    renderTicketSla(
      ticketSla({
        response_state: "paused",
        resolution_state: "paused",
        response_due_at: fromNow(2 * HOUR),
        resolution_due_at: fromNow(8 * HOUR),
        paused_at: fromNow(-3 * HOUR - 30_000),
        paused_minutes_total: 45,
      }),
    );

    const headlines = await screen.findAllByText(/Paused for 3h — clock stopped/);
    expect(headlines).toHaveLength(2);
    expect(screen.getAllByText("Paused")).toHaveLength(2);
    expect(screen.queryByText(/remaining/)).toBeNull();
    expect(screen.getAllByText(/45m of earlier pause already credited/).length).toBe(2);
  });

  // A voided promise is neither kept nor broken -- it must not render as a
  // breach even though the row carries a breach instant.
  it("renders a voided promise as void, never as a breach", async () => {
    renderTicketSla(
      ticketSla({
        response_state: "void",
        resolution_state: "void",
        response_due_at: fromNow(-5 * HOUR),
        response_breached_at: fromNow(-5 * HOUR),
        voided_at: fromNow(-HOUR),
      }),
    );

    expect(
      (await screen.findAllByText("Promise voided — neither met nor breached")).length,
    ).toBe(2);
    expect(screen.queryByText("Breached")).toBeNull();
    expect(screen.getAllByText("Void")).toHaveLength(2);
  });

  it("reports a late answer as breached rather than met", async () => {
    renderTicketSla(
      ticketSla({
        response_state: "breached",
        response_due_at: fromNow(-4 * HOUR),
        response_breached_at: fromNow(-3 * HOUR - 30_000),
        response_met_at: fromNow(-HOUR),
        resolution_state: "running",
        resolution_due_at: fromNow(HOUR + 30_000),
        policy_source: "retriage",
      }),
    );

    expect(await screen.findByText("Breached 3h ago")).toBeInTheDocument();
    expect(screen.getByText(/after the deadline/)).toBeInTheDocument();
    expect(screen.queryByText("Met")).toBeNull();
    // §0.3's attribution: a retriage breach reads differently from a slow one.
    // The headline answers "policy applied because" in a sentence; the
    // consequence a reviewer needs is the line under it.
    expect(screen.getByText("A person changed the priority or queue")).toBeInTheDocument();
    expect(
      screen.getByText("The deadline was recomputed from the ticket's start."),
    ).toBeInTheDocument();
  });

  it("names the policy and calendar instead of printing their ids", async () => {
    renderTicketSla(
      ticketSla({
        response_due_at: fromNow(2 * HOUR + 30_000),
        resolution_due_at: fromNow(8 * HOUR + 30_000),
        policy_id: "pol-high",
        calendar_id: "cal-standard",
        policy_name: "High priority tickets in any queue",
        calendar_name: "Office hours",
      }),
    );

    expect(await screen.findByText("High priority tickets in any queue")).toBeInTheDocument();
    expect(screen.getByText("Office hours")).toBeInTheDocument();
    expect(screen.queryByText("pol-high")).toBeNull();
    expect(screen.queryByText("cal-standard")).toBeNull();
  });

  it("renders 'no SLA' as a legitimate state, not an error", async () => {
    renderTicketSla({
      ticket_id: "t1",
      has_sla: false,
      response_state: "none",
      resolution_state: "none",
    });

    expect(await screen.findByText("No SLA")).toBeInTheDocument();
    expect(screen.getByText(/No policy matched this ticket/)).toBeInTheDocument();
  });

  it("degrades quietly when the per-ticket read 403s", async () => {
    mockUseApi.mockReturnValue({
      getTicketSla: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")),
    } as unknown as ApiClient);
    renderWithProviders(<TicketSlaPanel ticketId="t1" />);

    expect(
      await screen.findByText("SLA state not available (insufficient permissions)."),
    ).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// UX remediation cycle 3 — the form kernel in this module.
//
// WHAT THESE PREVENT. Both dialogs on this page shipped
// `<Button disabled={mutation.isPending || !someField}>` with no `<form>`, no
// validation feedback and no live region. Auditor A filed the policy dialog as
// a BLOCKER ("'Add policy' Save is a silent no-op when no calendar is
// selected") and auditor B filed the same shape across the module ("same
// silent-no-op failure repeats on every form"). The user pressed a control
// that would not move and the interface named no field.
//
// So every test below asserts the SAME TWO THINGS together: the submit control
// is ENABLED with the input in its bad state, and pressing it produces an
// announced sentence naming the field. A test that only checked for the
// message would still pass against a tooltip on a dead button, which is the
// same defect wearing a hat.
// --------------------------------------------------------------------------

function adminApiWithWrites(): Partial<ApiClient> {
  const api = baseApi(ME_ADMIN);
  api.createSlaCalendar = vi.fn().mockResolvedValue(CALENDARS[0]);
  api.updateSlaCalendar = vi.fn().mockResolvedValue(CALENDAR_DETAIL);
  api.createSlaPolicy = vi.fn().mockResolvedValue(policy({ id: "new" }));
  return api;
}

async function openDialog(api: Partial<ApiClient>, opener: RegExp, title: string) {
  const user = userEvent.setup();
  mockUseApi.mockReturnValue(api as ApiClient);
  renderWithProviders(<SlaAdminPage />);
  await user.click(await screen.findByRole("button", { name: opener }));
  const dialog = await screen.findByRole("dialog", { name: title });
  return { user, dialog, save: within(dialog).getByRole("button", { name: "Save" }) };
}

describe("Add calendar refuses out loud instead of doing nothing", () => {
  it("keeps Save pressable with an empty name and names the field on submit", async () => {
    const api = adminApiWithWrites();
    const { user, dialog, save } = await openDialog(api, /add calendar/i, "Add calendar");

    // The whole point: NOT disabled. A dead button is the silent no-op.
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Name is required");
    expect(api.createSlaCalendar).not.toHaveBeenCalled();
  });

  it("rejects a timezone that is not an IANA name before the round trip", async () => {
    const api = adminApiWithWrites();
    const { user, dialog, save } = await openDialog(api, /add calendar/i, "Add calendar");

    await user.type(within(dialog).getByLabelText("Name"), "UK hours");
    const timezone = within(dialog).getByLabelText("Timezone (IANA name)");
    await user.clear(timezone);
    // What people actually type. `zoneinfo` 422s on it server-side; the point
    // is that the user finds out here rather than after a failed Save.
    await user.type(timezone, "London");

    await user.click(save);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      'Timezone must be an IANA name such as Europe/London — "London" is not one',
    );
    expect(api.createSlaCalendar).not.toHaveBeenCalled();

    await user.clear(timezone);
    await user.type(timezone, "Europe/Paris");
    await user.click(save);
    await waitFor(() =>
      expect(api.createSlaCalendar).toHaveBeenCalledWith({
        name: "UK hours",
        timezone: "Europe/Paris",
        active: true,
      }),
    );
  });

  it("offers IANA autocomplete rather than leaving the field a blind free-text box", async () => {
    const { dialog } = await openDialog(adminApiWithWrites(), /add calendar/i, "Add calendar");

    const timezone = within(dialog).getByLabelText("Timezone (IANA name)");
    const listId = timezone.getAttribute("list");
    expect(listId).toBe("sla-timezone-options");
    const datalist = document.getElementById(listId as string) as HTMLDataListElement;
    expect(datalist).not.toBeNull();
    expect(
      Array.from(datalist.querySelectorAll("option")).map((option) => option.value),
    ).toEqual(expect.arrayContaining(["Europe/London", "America/New_York"]));
  });

  // The prefilled default and the placeholder were the SAME STRING, so a value
  // that would be saved was indistinguishable from grey hint text. Removing
  // the placeholder is half the fix; saying that the value is real is the
  // other half, because a prefilled field is still applied without being read.
  it("does not disguise the applied timezone default as a placeholder", async () => {
    const { dialog } = await openDialog(adminApiWithWrites(), /add calendar/i, "Add calendar");

    const timezone = within(dialog).getByLabelText("Timezone (IANA name)");
    expect(timezone).toHaveValue("Europe/London");
    expect(timezone).not.toHaveAttribute("placeholder");
    expect(within(dialog).getByText(/will be saved as typed/)).toBeInTheDocument();
  });

  it("submits on Enter, because the dialog is a real form", async () => {
    const api = adminApiWithWrites();
    const { user, dialog } = await openDialog(api, /add calendar/i, "Add calendar");

    await user.type(within(dialog).getByLabelText("Name"), "Nightshift{Enter}");

    await waitFor(() =>
      expect(api.createSlaCalendar).toHaveBeenCalledWith({
        name: "Nightshift",
        timezone: "Europe/London",
        active: true,
      }),
    );
  });

  // Cancel sits inside the <form> now, and a <button> with no `type` inside a
  // form IS a submit button -- so without `type="button"` this discards nothing
  // and saves instead.
  it("Cancel abandons the dialog rather than submitting it", async () => {
    const api = adminApiWithWrites();
    const { user, dialog } = await openDialog(api, /add calendar/i, "Add calendar");

    await user.type(within(dialog).getByLabelText("Name"), "Oops");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(api.createSlaCalendar).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "Add calendar" })).toBeNull();
  });

  // The bodyless-503 blocker at this call site: `describeError` is
  // `error.message`, and `new ApiError(503, await response.text())` carries ""
  // when a proxy refuses. The old red paragraph was guarded on that empty
  // string and rendered NOTHING -- the operator watched a Save do nothing and
  // had no idea it had failed.
  it("announces a refusal the server did not explain", async () => {
    const api = adminApiWithWrites();
    api.createSlaCalendar = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const { user, dialog, save } = await openDialog(api, /add calendar/i, "Add calendar");

    await user.type(within(dialog).getByLabelText("Name"), "UK hours");
    await user.click(save);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      /That service is unavailable right now/,
    );
    // No optimistic success, and the dialog stays open with the typing intact.
    expect(within(dialog).getByLabelText("Name")).toHaveValue("UK hours");
  });
});

describe("Add policy refuses out loud instead of doing nothing", () => {
  // THE BLOCKER. `disabled={create.isPending || !targetsValid}` folded "no
  // calendar", "bad response target" and "bad resolution target" into one dead
  // button that named none of them.
  it("keeps Save pressable with no calendar chosen and says the calendar is required", async () => {
    const api = adminApiWithWrites();
    const { user, dialog, save } = await openDialog(api, /add policy/i, "Add policy");

    expect(save).toBeEnabled();
    await user.click(save);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Calendar is required");
    expect(api.createSlaPolicy).not.toHaveBeenCalled();
  });

  // Auditor B, HIGH: "Calendar dropdown in the policy form is empty with no
  // explanation or escape hatch ... the Business calendars section that would
  // fix this is ~300px further down the page, below the form." On a fresh
  // install the list is ALWAYS empty, so this is the first thing a new tenant
  // meets.
  it("says why the calendar list is empty and offers the way out of it", async () => {
    const api = adminApiWithWrites();
    api.listSlaCalendars = vi.fn().mockResolvedValue([]);
    const { user, dialog } = await openDialog(api, /add policy/i, "Add policy");

    expect(
      within(dialog).getByText(/no business calendars, so no policy can be saved yet/i),
    ).toBeInTheDocument();

    // Not a dead end: the escape hatch opens the form that fixes it, without
    // making the user find the panel that owns it.
    await user.click(within(dialog).getByRole("button", { name: /add a business calendar/i }));
    expect(await screen.findByRole("dialog", { name: "Add calendar" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Add policy" })).toBeNull();
  });

  it("refuses a zero target and says which target, rather than sending it", async () => {
    const api = adminApiWithWrites();
    const { user, dialog, save } = await openDialog(api, /add policy/i, "Add policy");

    await pickCalendar(user, dialog);
    const response = within(dialog).getByLabelText(/^Response target in minutes/);
    await user.clear(response);
    await user.type(response, "0");
    await user.click(save);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Response target in minutes must be at least 1 minute",
    );
    expect(api.createSlaPolicy).not.toHaveBeenCalled();
  });

  // A negative target is a deadline before the ticket existed, and the label
  // echoed `formatMinutes(-5)` back as "(-5m)" -- the interface agreeing that
  // the nonsense was a duration.
  it("refuses a negative target and stops echoing it as a duration", async () => {
    const api = adminApiWithWrites();
    const { user, dialog, save } = await openDialog(api, /add policy/i, "Add policy");

    await pickCalendar(user, dialog);
    const resolution = within(dialog).getByLabelText(/^Resolution target in minutes/);
    await user.clear(resolution);
    await user.type(resolution, "-5");

    expect(within(dialog).queryByText(/-5m/)).toBeNull();
    expect(within(dialog).getByLabelText("Resolution target in minutes")).toBe(resolution);

    await user.click(save);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Resolution target in minutes cannot be negative",
    );
    expect(api.createSlaPolicy).not.toHaveBeenCalled();
  });

  // The label is a real affordance when the value is real -- the fix is not
  // "stop translating", it is "translate only what could be saved".
  it("still translates a usable target into a duration", async () => {
    const { user, dialog } = await openDialog(adminApiWithWrites(), /add policy/i, "Add policy");

    const response = within(dialog).getByLabelText(/^Response target in minutes/);
    await user.clear(response);
    await user.type(response, "90");

    expect(within(dialog).getByLabelText("Response target in minutes (1h 30m)")).toBe(response);
  });

  it("saves once every reason to refuse is gone", async () => {
    const api = adminApiWithWrites();
    const { user, dialog, save } = await openDialog(api, /add policy/i, "Add policy");

    await pickCalendar(user, dialog);
    await user.click(save);

    await waitFor(() => expect(api.createSlaPolicy).toHaveBeenCalledTimes(1));
    expect(api.createSlaPolicy).toHaveBeenCalledWith(
      expect.objectContaining({
        calendar_id: "cal1",
        response_target_minutes: 60,
        resolution_target_minutes: 480,
        queue_id: null,
        priority: null,
      }),
    );
  });
});

// --------------------------------------------------------------------------
// UX remediation cycle 5 — one datetime, in the reader's zone.
//
// WHAT THIS PREVENTS. Auditor B, filed as a BLOCKER: "every datetime in the
// product is displayed 5 hours earlier than it was entered." The module-local
// `formatInstant` was `new Date(x).toLocaleString()`: the browser's zone,
// unnamed, in whatever format that machine's locale produced. On an SLA
// deadline that is not cosmetic — a technician reads it to decide what to work
// on next, and a five-hour error changes the answer.
//
// Every test below sets the profile zone to somewhere the RUNNER IS NOT (the
// runner is UTC), because a test whose expected zone equals the browser's
// passes against a component that ignores the profile entirely.
// --------------------------------------------------------------------------

describe("instants render in the signed-in operator's zone", () => {
  it("renders a calendar's creation time in the profile zone, not the browser's", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_IN_NEW_YORK) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    // The fixture is 2026-01-01T00:00:00Z. In New York that is the evening
    // BEFORE — the case where a browser-zone renderer and a profile-zone one
    // disagree about the day as well as the hour.
    // Both fixture calendars were created at the same instant, so the cell
    // legitimately appears twice -- what matters is that neither reads 1 Jan.
    expect(await screen.findAllByText("31 Dec 2025, 19:00")).toHaveLength(2);
    expect(screen.queryByText(/01 Jan 2026/)).toBeNull();
    // And never the raw ISO string, nor a locale-dependent rendering of it.
    expect(screen.queryByText("2026-01-01T00:00:00.000Z")).toBeNull();
  });

  it("renders a ticket's deadline in the profile zone", async () => {
    renderTicketSla(
      ticketSla({ response_due_at: "2026-08-02T14:30:00.000Z", resolution_state: "none" }),
      ME_IN_NEW_YORK,
    );

    // -04:00 in August, because the offset is derived at the instant rather
    // than taken as a property of the zone.
    expect(await screen.findByText(/Due 02 Aug 2026, 10:30/)).toBeInTheDocument();
    expect(screen.queryByText(/14:30/)).toBeNull();
  });

  it("renders an escalation stamp in the profile zone and 'No' when there is none", async () => {
    const { unmount } = renderTicketSla(
      ticketSla({ escalated_at: "2026-08-02T14:30:00.000Z", response_state: "none", resolution_state: "none" }),
      ME_IN_NEW_YORK,
    );
    expect(await screen.findByText("02 Aug 2026, 10:30")).toBeInTheDocument();
    unmount();

    // Not escalated is a real answer, and must stay the word "No" rather than
    // becoming the absent-date marker, which claims something different.
    renderTicketSla(ticketSla({ escalated_at: null }), ME_IN_NEW_YORK);
    expect(await screen.findByText("No")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// UX remediation cycle 4 — every refusal reaches the operator as a sentence.
//
// THE BLOCKER THIS CLOSES, in this module's own words: `ApiClient` throws
// `new ApiError(status, await response.text())`, so a proxy's bodyless 503
// arrives carrying `message === ""`. The module-local `describeError` returned
// that empty string, and every red paragraph in the module was guarded on the
// result being truthy — so a DELETE that failed rendered NOTHING and the UI
// was indistinguishable from one that had succeeded. Auditor A: "A failed
// destructive API call (HTTP 503) is silently swallowed — the UI shows success
// and the user is never told."
//
// So each test below drives the call site with an error the OLD function
// rendered as nothing (or as a raw payload), and asserts an announced
// sentence — `role="alert"`, because these paragraphs are the only thing that
// changes on the page when the action fails.
// --------------------------------------------------------------------------

describe("destructive failures are announced, not swallowed", () => {
  // The page carries two tables whose row actions are both called "Delete".
  // Each is scoped by a column header only IT has, rather than by the order the
  // two panels happen to be laid out in.
  async function firstRowOf(columnHeader: string): Promise<HTMLElement> {
    const header = await screen.findByRole("columnheader", { name: columnHeader });
    const table = header.closest("table") as HTMLElement;
    return within(table).getAllByRole("row")[1] as HTMLElement;
  }

  it("does not style Delete like the Edit button beside it", async () => {
    // Auditor B, HIGH: "'Delete' sits directly beside 'Edit' in the same row
    // with identical size and no destructive colour, so a mis-click is one
    // pixel of travel away from irreversible data loss." The confirmation
    // (pinned by the two tests below, which have to click through it) answers
    // the misclick; the colour is what stops it. `.btn.danger` is the design's
    // destructive treatment -- red ink, red border, no gradient.
    mockUseApi.mockReturnValue(baseApi(ME_ADMIN) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    for (const column of ["On breach", "Health"]) {
      const row = await firstRowOf(column);
      expect(within(row).getByRole("button", { name: "Delete" }).className).toContain("danger");
      expect(within(row).getByRole("button", { name: "Edit" }).className).not.toContain("danger");
    }
  });

  it("says a calendar delete failed when the server refuses without a body", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.deleteSlaCalendar = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    const row = await firstRowOf("Health");
    await user.click(within(row).getByRole("button", { name: "Delete" }));
    // Deleting asks first (cycle 8); the refusal is on the far side of it.
    await user.click(await screen.findByRole("button", { name: "Delete calendar" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Delete failed/);
    expect(alert).toHaveTextContent(/That service is unavailable right now/);
    // The calendar is still listed: nothing about the page may suggest it went.
    expect(within(row).getByText("UK business hours")).toBeInTheDocument();
  });

  it("says a policy delete failed and repeats the reason the server gave", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    // The real refusal: `active: false` is the retirement path, and the
    // service layer names the tickets. That sentence is the useful part and
    // must not arrive wrapped in its JSON envelope.
    api.deleteSlaPolicy = vi
      .fn()
      .mockRejectedValue(
        new ApiError(400, '{"detail":"policy is referenced by 3 open tickets"}'),
      );
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    const row = await firstRowOf("On breach");
    await user.click(within(row).getByRole("button", { name: "Delete" }));
    // Deleting asks first (cycle 8); the refusal is on the far side of it.
    await user.click(await screen.findByRole("button", { name: "Delete policy" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Delete failed: policy is referenced by 3 open tickets");
    expect(alert.textContent).not.toContain("detail");
    expect(alert.textContent).not.toContain("{");
  });

  it("says a holiday removal failed when the server refuses without a body", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.removeSlaCalendarHoliday = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    const openers = await screen.findAllByRole("button", { name: /hours & holidays/i });
    await user.click(openers[0] as HTMLElement);
    // "Remove" is also the label on every working-window row above, so this
    // scopes to the holidays table by its own "Date" column.
    const row = await firstRowOf("Date");
    expect(within(row).getByText("25 Dec 2026")).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Remove" }));
    // Removing asks first (cycle 8); the refusal is on the far side of it.
    await user.click(await screen.findByRole("button", { name: "Remove holiday" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /That service is unavailable right now/,
    );
    // Still there. A removal that failed must not leave the row looking gone.
    expect(screen.getByText("Christmas Day")).toBeInTheDocument();
  });

  it("says the week was not saved rather than leaving a stale grid looking stored", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    api.replaceSlaCalendarWindows = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    const openers = await screen.findAllByRole("button", { name: /hours & holidays/i });
    await user.click(openers[0] as HTMLElement);
    await screen.findByText("Working hours");
    await user.click(screen.getByRole("button", { name: "Add window (Tuesday)" }));
    await user.click(screen.getByRole("button", { name: /save week/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/The week was not saved/);
    expect(alert).toHaveTextContent(/That service is unavailable right now/);
    // The draft is still dirty, which is the truthful state and what makes
    // Revert meaningful.
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  });

  it("translates a validation refusal of the settings save instead of printing its payload", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN);
    // FastAPI registers no custom handler on this backend, so a 422 arrives as
    // the raw Pydantic envelope. Rendering that at the user is the defect;
    // `loc` names the field and `msg` is the only human part.
    api.updateSlaSettings = vi.fn().mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: [
            {
              type: "less_than_equal",
              loc: ["body", "auto_close_after_days"],
              msg: "Input should be less than or equal to 365",
              input: 4000,
            },
          ],
        }),
      ),
    );
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    await user.click(await screen.findByRole("button", { name: /save settings/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Settings were not saved.");
    expect(alert).toHaveTextContent(
      "Auto close after days: Input should be less than or equal to 365",
    );
    expect(alert.textContent).not.toContain("less_than_equal");
    expect(alert.textContent).not.toContain("loc");
  });

  it("tells a technician the SLA panel failed rather than rendering an empty paragraph", async () => {
    mockUseApi.mockReturnValue({
      getTicketSla: vi.fn().mockRejectedValue(new ApiError(503, "")),
      me: vi.fn().mockResolvedValue(ME_READ_ONLY),
    } as unknown as ApiClient);
    renderWithProviders(<TicketSlaPanel ticketId="t1" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /That service is unavailable right now/,
    );
    // Not the 403 wording: "you may not see this" and "this is broken" are
    // different facts and lead to different next actions.
    expect(screen.queryByText(/insufficient permissions/)).toBeNull();
  });

  it("names the cause when a read fails, instead of one generic sentence for every cause", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.listSlaCalendars = vi.fn().mockRejectedValue(new ApiError(500, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Calendars did not load/);
    expect(alert).toHaveTextContent(/The server failed on this request/);
  });
});

// --------------------------------------------------------------------------
// Stat tiles: where each number lives, and what a zero on this page means.
//
// This is the page whose whole subject is promises that were not kept, which
// makes it the worst possible page to render a confident "0" for a question
// nobody has answered yet. Both halves are tested: the tiles are links, and
// an unanswered `GET /sla/settings` reads as an em dash rather than as good
// news.
// --------------------------------------------------------------------------

describe("SLA stat tiles", () => {
  const tiles = () => [...document.querySelectorAll<HTMLElement>(".stat-card")];
  const tile = (label: string): HTMLAnchorElement => {
    const found = tiles().find((card) => card.querySelector(".stat-label")?.textContent === label);
    if (!found) throw new Error(`no stat tile labelled ${label}`);
    return found as HTMLAnchorElement;
  };

  it("gives every tile a real href, and sends the ticket counts to the tickets", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    await screen.findAllByText("UK business hours");
    expect(tiles()).toHaveLength(4);
    for (const card of tiles()) {
      expect(card.tagName).toBe("A");
      expect(card.getAttribute("href")).toBeTruthy();
    }
    // The two configuration counts count the tables on THIS page; the two
    // consequence counts count TICKETS, which do not live here.
    expect(tile("Business calendars")).toHaveAttribute("href", "/sla");
    expect(tile("Active policies")).toHaveAttribute("href", "/sla");
    expect(tile("Tickets with no SLA")).toHaveAttribute("href", "/service-desk");
    expect(tile("Deadlines out of date")).toHaveAttribute("href", "/service-desk");
  });

  // Review wave round 3: the two consequence tiles carry a sentence at a
  // non-zero count and the two configuration tiles carried nothing, so half
  // the row sat with an empty band under the figure.
  it("says what the two configuration figures are made of", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    await screen.findAllByText("UK business hours");
    // A calendar with no working windows produces no deadline at all, so "2
    // calendars" with one of them broken is a different configuration from
    // two that work -- the fault this page exists to surface.
    expect(tile("Business calendars")).toHaveTextContent("1 cannot produce a deadline");
    // The figure counts ACTIVE policies; the line says how many exist.
    expect(tile("Active policies")).toHaveTextContent("3 of 3 configured");
  });

  it("reads an unanswered settings call as 'no answer', never as zero unkept promises", async () => {
    const api = baseApi(ME_READ_ONLY);
    api.getSlaSettings = vi.fn(() => new Promise<never>(() => {}));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    await screen.findAllByText("UK business hours");
    const missing = tile("Tickets with no SLA");
    expect(missing.querySelector(".stat-value")?.textContent).toBe("—");
    // And not loud: an alarm colour over "no answer yet" is the same lie as
    // a 0 over it, pointing the other way.
    expect(missing.className).not.toContain("urgent");
    expect(tile("Deadlines out of date").querySelector(".stat-value")?.textContent).toBe("—");
  });

  it("says WHICH zero a measured zero is", async () => {
    // SETTINGS.stale_ticket_sla is 0 and the call SUCCEEDED -- the opposite
    // case to the one above, and it must not look the same on screen.
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    renderWithProviders(<SlaAdminPage />);

    await screen.findAllByText("UK business hours");
    const stale = tile("Deadlines out of date");
    expect(stale.querySelector(".stat-value")?.textContent).toBe("0");
    expect(stale).toHaveTextContent("Every deadline matches its policy");
  });
});
