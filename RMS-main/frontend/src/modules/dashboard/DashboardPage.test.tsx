/**
 * Dashboard.
 *
 * The dashboard has no endpoint of its own, so its two failure modes are
 * different from every other page's:
 *
 * 1. **A number with no request behind it.** The design's dashboard is the
 *    densest set of invented figures in the prototype. The assertions below
 *    check the cards against the fixtures AND check that the mock's own
 *    numbers (286, 84%, 12, 23) are nowhere on screen.
 *
 * 2. **A panel the operator may not read.** Seven module queries on one screen
 *    means seven chances to 403 an operator who holds two read codes. The
 *    gating tests assert both directions: a panel is absent AND its query was
 *    never fired.
 *
 * The three states are asserted per panel rather than once, because each panel
 * has its own query and they fail independently — a dead health service must
 * not make the device table look empty.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Device } from "../../lib/api";
import { DashboardPage } from "./DashboardPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL_PERMISSIONS = [
  "devices.read",
  "jobs.read",
  "health.read",
  "ticket.read",
  "patch.read",
  "backup.read",
  "remote.control",
];

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    hostname: "ws-alpha",
    fqdn: "ws-alpha.corp.local",
    os_family: "windows",
    os_version: "11 23H2",
    architecture: "amd64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: "2026-08-16T09:00:00Z",
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    // Asset fields (asset-inventory spec §2). Defaulted to what every
    // agent-enrolled device is, so these tests describe the same fleet they
    // did before assets existed.
    lifecycle: "in_service",
    asset_type: "computer",
    source: "agent",
    // An enrolled machine proved its own identity at enrolment; nobody had
    // to vouch for the row (asset-inventory spec §3).
    provenance: "agent",
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...overrides,
  };
}

interface Overrides {
  permissions?: string[];
  /** `/me`'s `timezone`, the zone every date on this page is rendered in. */
  timezone?: string | null;
  listDevices?: () => Promise<unknown>;
  listJobs?: () => Promise<unknown>;
  listHealthAlerts?: () => Promise<unknown>;
  getHealthAlertSummary?: () => Promise<unknown>;
  listTickets?: () => Promise<unknown>;
  getPatchFleetReport?: () => Promise<unknown>;
  getBackupReport?: () => Promise<unknown>;
  listRemoteSessions?: () => Promise<unknown>;
  getChangeCalendar?: () => Promise<unknown>;
  /** The stored arrangement. `{ widgets: null }` is "never customized". */
  getDashboardLayout?: () => Promise<unknown>;
  saveDashboardLayout?: (widgets: unknown) => Promise<unknown>;
  resetDashboardLayout?: () => Promise<void>;
}

const calls = {
  listDevices: vi.fn(),
  listJobs: vi.fn(),
  listHealthAlerts: vi.fn(),
  getHealthAlertSummary: vi.fn(),
  listTickets: vi.fn(),
  getPatchFleetReport: vi.fn(),
  getBackupReport: vi.fn(),
  listRemoteSessions: vi.fn(),
  getChangeCalendar: vi.fn(),
  getDashboardLayout: vi.fn(),
  saveDashboardLayout: vi.fn(),
  resetDashboardLayout: vi.fn(),
};

function renderPage(overrides: Overrides = {}) {
  // `timezone` is excluded alongside `permissions`: both are values on the
  // `/me` fixture, not API stubs, and the loop below only builds stubs.
  const defaults: Required<Omit<Overrides, "permissions" | "timezone">> = {
    getChangeCalendar: () => Promise.resolve({ entries: [], conflicts: [] }),
    listDevices: () => Promise.resolve({ items: [device()], total: 1 }),
    listJobs: () => Promise.resolve({ items: [], total: 0 }),
    listHealthAlerts: () => Promise.resolve([]),
    getHealthAlertSummary: () =>
      Promise.resolve({
        open: 0,
        by_state: { active: 0, acknowledged: 0, muted: 0 },
        by_severity: { info: 0, warning: 0, critical: 0 },
        worst_severity: null,
      }),
    listTickets: () => Promise.resolve({ items: [], total: 0 }),
    getPatchFleetReport: () =>
      Promise.resolve({
        compliant_pct_by_os_family: {},
        compliant_pct_by_severity: {},
        exposed_devices: [],
      }),
    getBackupReport: () =>
      Promise.resolve({
        devices_total: 0,
        devices_covered: 0,
        coverage_rate: 0,
        run_counts_by_state: {},
        success_rate: 0,
        points_needing_verification: 0,
        dedup: {},
      }),
    listRemoteSessions: () => Promise.resolve({ items: [], total: 0 }),
    // NOBODY HAS ARRANGED ANYTHING, which is the state every assertion below
    // about the stock dashboard depends on. `null`, never `[]`.
    getDashboardLayout: () => Promise.resolve({ widgets: null }),
    saveDashboardLayout: (widgets: unknown) => Promise.resolve({ widgets }),
    resetDashboardLayout: () => Promise.resolve(undefined),
  };

  const client: Record<string, unknown> = {
    me: () =>
      Promise.resolve({
        user_id: "u",
        org_id: "o",
        email: "op@example.com",
        display_name: "Operator",
        permissions: overrides.permissions ?? ALL_PERMISSIONS,
        timezone: overrides.timezone ?? null,
      }),
  };
  for (const key of Object.keys(defaults) as (keyof typeof defaults)[]) {
    const impl = overrides[key] ?? defaults[key];
    calls[key].mockImplementation(impl);
    client[key] = calls[key];
  }

  mockUseApi.mockReturnValue(client as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

beforeEach(() => {
  mockUseApi.mockReset();
  for (const fn of Object.values(calls)) fn.mockReset();
});

/**
 * A donut's figure, as one string. v3's `Donut` draws `61<small>%</small>`,
 * and `getByText` reads an element's OWN text nodes, so "61%" is never one
 * node's text; this matches the `.don-v` element whose whole content is it.
 */
function donutReading(text: string) {
  return (_content: string, element: Element | null) =>
    element?.classList.contains("don-v") === true && element.textContent === text;
}

describe("DashboardPage", () => {
  it("says it is loading rather than showing an empty fleet", async () => {
    renderPage({ listDevices: () => new Promise<never>(() => {}) });
    // TWO widgets read the device list (the heat strip and the recent
    // devices table), and each states its own state: a widget the operator
    // has moved or hidden the other one away from must still say so.
    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
    expect(screen.queryByText(/No devices enrolled/i)).not.toBeInTheDocument();
  });

  it("says a panel's request FAILED rather than showing it empty", async () => {
    // The distinction the whole state machine exists for, applied per panel:
    // a dead device service must not read as a fleet of zero.
    renderPage({ listDevices: () => Promise.reject(new Error("boom")) });
    expect((await screen.findAllByRole("alert")).length).toBeGreaterThan(0);
    expect(screen.queryByText(/No devices enrolled yet/i)).not.toBeInTheDocument();
  });

  it("says a panel is empty when it is genuinely empty", async () => {
    renderPage({ listDevices: () => Promise.resolve({ items: [], total: 0 }) });
    expect(await screen.findByText("No devices enrolled yet.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  // DESIGN REVIEW M9. `Heat` is one 10px chip per device, so on a small fleet
  // it is a single short row at the top-left of a card that stretches to the
  // height of "Service desk" beside it -- about 100px of blank card under
  // four lines of content. The breakdown the subtitle was carrying as one
  // run-on sentence is the content that was missing, so it moved into the
  // body as the same `Facts` strip Service desk uses in the panel next door.
  // Nothing new is computed, and the subtitle keeps the SCOPE, which is the
  // half that has to stay beside the title: a breakdown of the fourteen most
  // recent devices must never read as a breakdown of the fleet.
  it("fills Fleet at a glance with the counts behind the strip, and keeps the scope in the subtitle", async () => {
    renderPage({
      listDevices: () =>
        Promise.resolve({
          items: [
            device(),
            device({ id: "2", hostname: "ws-beta", agent_status: "offline" }),
            device({ id: "3", hostname: "srv-gamma", agent_status: null }),
          ],
          total: 9,
        }),
    });

    const panel = (await screen.findByText("Fleet at a glance")).closest(".panel") as HTMLElement;

    // The strip is still the strip.
    expect(panel.querySelector(".heat")).not.toBeNull();

    // And the counts are IN the panel now, not only on its `aria-label`, so
    // colour is not the only carrier of what the chips mean.
    const facts = panel.querySelector(".facts") as HTMLElement;
    expect(facts).not.toBeNull();
    const reading = (label: string) =>
      [...facts.querySelectorAll<HTMLElement>("div")].find(
        (row) => row.querySelector(".k")?.textContent === label,
      )?.querySelector(".v")?.textContent;
    expect(reading("Need a person")).toBe("1");
    expect(reading("Healthy")).toBe("1");
    expect(reading("Without an agent")).toBe("1");

    // The subtitle says which population, and no longer restates the counts.
    const subtitle = panel.querySelector(".panel-h p") as HTMLElement;
    expect(subtitle).toHaveTextContent("Over the 3 most recent of 9 devices");
    expect(subtitle).not.toHaveTextContent(/need a person/);
  });

  // The agentless row is OMITTED at zero, exactly as the subtitle omitted it:
  // a fleet where every machine runs an agent should not be told it holds
  // none without one.
  it("leaves the agentless count out of Fleet at a glance when there are none", async () => {
    renderPage();

    const panel = (await screen.findByText("Fleet at a glance")).closest(".panel") as HTMLElement;
    expect(panel.querySelector(".facts")).not.toBeNull();
    expect(panel).not.toHaveTextContent("Without an agent");
  });

  it("fails panels independently, so one dead service does not blank the page", async () => {
    renderPage({
      listHealthAlerts: () => Promise.reject(new Error("health is down")),
    });
    // Devices still render.
    expect(await screen.findByText("ws-alpha")).toBeInTheDocument();
    // And the alerts panel says so rather than claiming zero alerts.
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No active alerts.")).not.toBeInTheDocument();
  });

  it("computes every stat card from a response, not from the mock's figures", async () => {
    renderPage({
      listDevices: () =>
        Promise.resolve({
          items: [
            device(),
            device({ id: "2", hostname: "srv-beta", agent_status: "offline" }),
            device({ id: "3", hostname: "srv-gamma" }),
          ],
          total: 3,
          fleet: { total: 3, managed: 3, online: 2, cert_attention: 0 },
        }),
      listJobs: () =>
        Promise.resolve({
          items: [
            { id: "j1", type: "patch.install", status: "failed", issued_at: "2026-08-16T10:00:00Z" },
            { id: "j2", type: "inventory.collect", status: "running", issued_at: "2026-08-16T10:01:00Z" },
          ],
          total: 2,
        }),
      listHealthAlerts: () =>
        Promise.resolve([
          {
            id: "a1",
            severity: "critical",
            check_type: "disk.free",
            selector: "C:",
            device_hostname: "ws-alpha",
            observed_value: "3%",
            threshold_display: "< 10%",
          },
        ]),
      // The tile does NOT count these rows -- it reads the summary route, and
      // this stub is what says so. One active row here, one open alert there.
      getHealthAlertSummary: () =>
        Promise.resolve({
          open: 1,
          by_state: { active: 1, acknowledged: 0, muted: 0 },
          by_severity: { info: 0, warning: 0, critical: 1 },
          worst_severity: "critical",
        }),
      // `{items, total}` since closeout recommendation 10: the route
      // returned a bare array and could not say how many matched.
      listTickets: () =>
        Promise.resolve({
          items: [
            { id: "t1", status: "open" },
            { id: "t2", status: "closed" },
            { id: "t3", status: "new" },
          ],
          total: 3,
        }),
      listRemoteSessions: () => Promise.resolve({ items: [], total: 4 }),
    });

    await screen.findByText("ws-alpha");
    const values = [...document.querySelectorAll(".stat-card")].map(
      (card) => card.querySelector(".stat-value")?.textContent,
    );
    // The stock order leads with the two tiles that need a person tonight,
    // as v3's does: jobs, open alerts, endpoints, online, tickets, sessions.
    expect(values).toEqual(["1", "1", "3", "2", "2", "4"]);
    // The prototype's own dashboard numbers are nowhere on this screen.
    expect(values).not.toContain("286");
    expect(values).not.toContain("23");
    expect(screen.queryByText(donutReading("84%"))).not.toBeInTheDocument();
  });

  it("sends a fleet no scan has reported on to scan, not merely to the page", async () => {
    // cross-module-guidance §2 Dashboard #2: the empty state said "No patch
    // scan has reported yet." with nowhere to go but "Open patching".
    renderPage({
      getPatchFleetReport: () =>
        Promise.resolve({
          compliant_pct_by_os_family: {},
          compliant_pct_by_severity: {},
          exposed_devices: [],
          last_scan_at: null,
          devices_scanned: 0,
          devices_total: 14,
        }),
    });
    expect(await screen.findByRole("link", { name: "Scan for updates" })).toHaveAttribute("href", "/patch");
    expect(screen.queryByRole("link", { name: "Open patching" })).not.toBeInTheDocument();
  });

  it("computes patch compliance from the fleet report rather than the mock's donuts", async () => {
    renderPage({
      getPatchFleetReport: () =>
        Promise.resolve({
          compliant_pct_by_os_family: { windows: 61, linux: 94 },
          compliant_pct_by_severity: {},
          exposed_devices: [],
          // WHEN the report was measured. A compliance percentage over a fleet
          // nothing has scanned is not a measurement, so the donut draws `n/a`
          // without this -- which is why the stub carries the field the route
          // always sends rather than the assertion being relaxed.
          last_scan_at: "2026-08-16T09:00:00Z",
          // OVER HOW MUCH OF THE FLEET. `GET /patch/report` carries no
          // per-family device count, so the panel cannot write "168 of 179"
          // under Windows without inventing it; what it can say truthfully is
          // the coverage of the whole measurement, and it does.
          devices_scanned: 9,
          devices_total: 14,
        }),
    });
    expect(await screen.findByText(donutReading("61%"))).toBeInTheDocument();
    expect(screen.getByText(donutReading("94%"))).toBeInTheDocument();
    expect(screen.getByText("Devices scanned")).toBeInTheDocument();
    expect(screen.getByText("9 of 14")).toBeInTheDocument();
    // THE PLATFORM'S OWN NAME, not the wire enum: the donuts read "windows",
    // "linux", "macos" until this asserted otherwise.
    expect(screen.getByText("Windows")).toBeInTheDocument();
    expect(screen.getByText("Linux")).toBeInTheDocument();
    expect(screen.queryByText("windows")).not.toBeInTheDocument();
    // The design's donuts read 94 / 71 and the previous design's 87 / 91 / 76 / 83.
    expect(screen.queryByText(donutReading("87%"))).not.toBeInTheDocument();
  });

  it("shows a person the platform's own words, never the wire's", async () => {
    // ONE DEFECT IN THREE PLACES, all of them enum values reaching a reader:
    // the OS cell read "windows 11 23H2", the alert row ended in a dangling
    // separator because `selector` is empty on a CPU check, and the sub line
    // printed the agent's comparator at them ("observed 96 against gt 95").
    renderPage({
      permissions: ALL_PERMISSIONS,
      listDevices: () =>
        Promise.resolve({
          items: [device({ os_family: "macos", os_version: "15.6" })],
          total: 1,
        }),
      listHealthAlerts: () =>
        Promise.resolve([
          {
            id: "a1",
            severity: "warning",
            check_type: "cpu.busy_pct",
            // NO SELECTOR. A CPU check has none, and five of the six rows on
            // the page were in exactly this state.
            selector: "",
            device_hostname: "ws-alpha",
            observed_value: "96",
            threshold_display: "gt 95",
          },
        ]),
    });

    await screen.findByText("ws-alpha");
    expect(screen.getByText("macOS 15.6")).toBeInTheDocument();
    // The check is named the way /health names it, and the registry key is
    // not on the tile at all: one incident, one name.
    const title = screen.getByText("CPU busy");
    expect(title.textContent).toBe("CPU busy");
    expect(screen.queryByText(/cpu\.busy_pct/)).not.toBeInTheDocument();
    expect(screen.getByText("ws-alpha · 96 over 95")).toBeInTheDocument();
    expect(screen.queryByText(/\bgt\b/)).not.toBeInTheDocument();
  });

  it("does not render — or fetch — a panel whose permission the operator lacks", async () => {
    // The reason gating is here at all: an operator without ticket.read must
    // not be shown a ticket panel that answers 403. Invariant 6 means this
    // decides only what renders; the server refuses regardless.
    renderPage({ permissions: ["devices.read"] });
    await screen.findByText("ws-alpha");
    expect(screen.queryByText("Open tickets")).not.toBeInTheDocument();
    expect(screen.queryByText("Active alerts")).not.toBeInTheDocument();
    expect(screen.queryByText("Patch compliance")).not.toBeInTheDocument();
    expect(calls.listTickets).not.toHaveBeenCalled();
    expect(calls.listHealthAlerts).not.toHaveBeenCalled();
    expect(calls.getPatchFleetReport).not.toHaveBeenCalled();
    expect(calls.getBackupReport).not.toHaveBeenCalled();
    expect(calls.listRemoteSessions).not.toHaveBeenCalled();
    // Non-vacuity: the panel they DO hold is present and was fetched.
    expect(calls.listDevices).toHaveBeenCalled();
    expect(screen.getByText("Managed endpoints")).toBeInTheDocument();
  });

  it("says so, rather than rendering an empty page, when no module is readable", async () => {
    renderPage({ permissions: [] });
    expect(await screen.findByText(/holds no module read permissions/i)).toBeInTheDocument();
    expect(calls.listDevices).not.toHaveBeenCalled();
  });

  it("shows no panel the platform has no endpoint for", async () => {
    // The design's fleet-health line chart needs a time series nothing stores,
    // and every card's "vs last 7 days" delta needs a previous value nothing
    // serves. Omitted, not faked.
    renderPage();
    await screen.findByText("ws-alpha");
    expect(screen.queryByText(/Fleet health overview/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Platform module directory/i)).not.toBeInTheDocument();
    expect(document.querySelector(".stat-foot")).toBeNull();
    expect(screen.queryByText(/vs last 7 days/i)).not.toBeInTheDocument();
    // Non-vacuity: the panels that DO have endpoints are present.
    expect(screen.getByText("Recent devices")).toBeInTheDocument();
    expect(screen.getByText("Backup coverage")).toBeInTheDocument();
  });

  // THE PANEL THAT WAS OMITTED FOR SCOPE, now built. `GET /changes/calendar`
  // always served it; the docstring named it a backlog item rather than
  // hiding it, and these are the assertions that make it real.
  it("lists upcoming changes from the calendar endpoint", async () => {
    renderPage({
      permissions: ["change.read"],
      getChangeCalendar: () =>
        Promise.resolve({
          entries: [
            {
              change_id: "c1",
              title: "Quarterly firmware rollout",
              type: "normal",
              risk: "medium",
              state: "approved",
              window_start: "2026-08-20T09:00:00Z",
              window_end: "2026-08-20T11:00:00Z",
            },
          ],
          conflicts: [],
        }),
    });
    expect(await screen.findByText("Quarterly firmware rollout")).toBeInTheDocument();
  });

  it("says the change calendar FAILED rather than showing nothing scheduled", async () => {
    renderPage({
      permissions: ["change.read"],
      getChangeCalendar: () => Promise.reject(new Error("boom")),
    });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing scheduled/i)).not.toBeInTheDocument();
  });

  /**
   * DATES (UX remediation cycle 5).
   *
   * This page rendered three dates through three separate
   * `new Date(x).toLocaleString()` calls, which is how the audit's "the same
   * device reports two different last-contact times in two different date
   * formats" happens: the dashboard's rendering of a device and the device
   * page's rendering of the same device agreed about nothing but the instant.
   *
   * The falsifier for all three assertions below is the same: revert any one
   * site to `new Date(x).toLocaleString()` and it renders the RUNNER's zone in
   * the runner's locale, so the expected string cannot appear.
   */
  it("renders every date in the signed-in user's zone, not the browser's", async () => {
    renderPage({
      timezone: "Asia/Tokyo",
      permissions: [...ALL_PERMISSIONS, "change.read"],
      listJobs: () =>
        Promise.resolve({
          items: [
            { id: "j1", type: "patch.install", status: "failed", issued_at: "2026-08-16T10:00:00Z" },
          ],
          total: 1,
        }),
      getChangeCalendar: () =>
        Promise.resolve({
          entries: [
            {
              change_id: "c1",
              title: "Quarterly firmware rollout",
              type: "normal",
              risk: "medium",
              state: "approved",
              // 23:30Z is the NEXT DAY in Tokyo. A window rendered a day out is
              // the version of this bug nobody notices until the change runs.
              window_start: "2026-08-16T23:30:00Z",
              window_end: "2026-08-17T01:00:00Z",
            },
          ],
          conflicts: [],
        }),
    });

    // last_seen_at 09:00Z -> 18:00 in Tokyo.
    expect(await screen.findByText("16 Aug 2026, 18:00")).toBeInTheDocument();
    // issued_at 10:00Z -> 19:00.
    expect(screen.getByText("16 Aug 2026, 19:00")).toBeInTheDocument();
    // window_start 23:30Z -> 08:30 the following day.
    expect(screen.getByText("17 Aug 2026, 08:30")).toBeInTheDocument();
    // And nothing renders the raw instant or a US-format fallback.
    expect(screen.queryByText(/09:00:00/)).not.toBeInTheDocument();
    expect(screen.queryByText(/8\/16\/2026/)).not.toBeInTheDocument();
  });

  it("renders an absent date as an em dash, never as Invalid Date", async () => {
    // An absent `last_seen_at` used to reach `new Date(...)`, and the two ways
    // that goes wrong are BOTH worse than an em dash: `new Date(undefined)` is
    // "Invalid Date", which reads as a broken agent, and `new Date(null)` is
    // the EPOCH, which reads as an agent last heard from in 1970.
    renderPage({
      timezone: "Asia/Tokyo",
      listDevices: () =>
        Promise.resolve({ items: [device({ last_seen_at: null })], total: 1 }),
    });
    const row = (await screen.findByText("ws-alpha")).closest("tr") as HTMLElement;
    // THE CELL, not the row: `fqdn`'s own em dash would satisfy a whole-row
    // assertion, and `new Date(null)` is the EPOCH rather than "Invalid Date",
    // so 1970 is checked for by name.
    const cells = [...row.querySelectorAll("td")].map((cell) => cell.textContent?.trim());
    expect(cells[3]).toBe("—");
    expect(row.textContent).not.toMatch(/1970|Invalid Date|null/);
  });

  it("falls back to the browser zone when the profile carries none", async () => {
    // `timezone` is nullable on `/me`, and a user who has never set one must
    // still get ONE format rather than a raw ISO string.
    renderPage({ timezone: null });
    const row = (await screen.findByText("ws-alpha")).closest("tr") as HTMLElement;
    expect(row.textContent).toMatch(/\d{2} \w{3} \d{4}, \d{2}:\d{2}/);
    expect(row.textContent).not.toContain("2026-08-16T09:00:00Z");
  });

  it("hides the change panel from an operator without change.read", async () => {
    renderPage({ permissions: ["devices.read"] });
    await screen.findByText(/Managed endpoints/i);
    expect(screen.queryByText("Upcoming changes")).not.toBeInTheDocument();
    // NON-VACUITY: it IS there for someone who holds the code.
    expect(calls.getChangeCalendar).not.toHaveBeenCalled();
  });
  /* --- "Jobs needing attention: 0" over day-old queued jobs (cycle 12) --- */

  /** A job as `/jobs` returns it, issued `agoMs` ago. */
  let jobSeq = 0;
  function job(status: string, agoMs: number) {
    jobSeq += 1;
    return {
      id: `j-${status}-${agoMs}-${jobSeq}`,
      type: "inventory.collect",
      device_id: "d1",
      status,
      attempts: 0,
      params: {},
      result: null,
      issued_at: new Date(Date.now() - agoMs).toISOString(),
      expires_at: new Date(Date.now() + 3_600_000).toISOString(),
      started_at: null,
      finished_at: null,
    };
  }

  const DAY = 24 * 60 * 60 * 1000;

  it("counts a job that has sat queued for a day as needing attention", async () => {
    // THE AUDIT'S HEADLINE NUMBER: "Four of five jobs have sat `queued` with 0
    // attempts for over 24 hours" while the tile read 0. A job that never
    // started is the one state an operator most needs to see, and it was the
    // one state this count ignored.
    renderPage({
      listJobs: () =>
        Promise.resolve({ items: [job("queued", DAY), job("queued", DAY)], total: 2 }),
    });

    const tile = (await screen.findByText("Jobs needing attention")).closest(
      ".stat-card",
    ) as HTMLElement;
    // The tile reads an em dash until the query resolves; asserting before
    // that would pass against a broken count as readily as a working one.
    await waitFor(() => expect(tile.textContent).toContain("2"));
  });

  it("still reports zero when the queue is merely busy", async () => {
    // THE NEGATIVE CONTROL, and the reason the test above means something: a
    // count that flagged every queued job would satisfy it while making the
    // tile useless. A job queued one minute ago is not in trouble.
    renderPage({
      listJobs: () => Promise.resolve({ items: [job("queued", 60_000)], total: 1 }),
    });

    const tile = (await screen.findByText("Jobs needing attention")).closest(
      ".stat-card",
    ) as HTMLElement;
    await waitFor(() => expect(tile.textContent).toContain("0"));
    expect(tile.textContent).not.toContain("—");
  });

  it("adds a stuck job to the failures rather than replacing them", async () => {
    renderPage({
      listJobs: () =>
        Promise.resolve({ items: [job("queued", DAY), job("failed", 60_000)], total: 2 }),
    });

    const tile = (await screen.findByText("Jobs needing attention")).closest(
      ".stat-card",
    ) as HTMLElement;
    await waitFor(() => expect(tile.textContent).toContain("2"));
  });
});

/**
 * NO DEAD TILES.
 *
 * This is the screen the instruction was really about: the dashboard is
 * nothing but numbers, and a number nobody can open is a number nobody can
 * check. `StatCard`'s required `to` makes an unlinked tile a compile error;
 * this pins WHERE each one goes, because the compiler cannot tell "/health"
 * from "/heath".
 *
 * Every href below is a path declared in src/App.tsx, and `?state=online` is
 * the param the devices list reads (DevicesPage's `useSearchParams`).
 */
describe("every tile is a link somewhere real", () => {
  /** The tile with this label. "Active alerts" is also a PANEL title on this
   *  page, so the lookup has to be by stat label rather than by text. The
   *  TILE is "Open alerts" and the PANEL is "Active alerts" -- they count
   *  different things and each is labelled with the one it answers. */
  function tile(label: string): HTMLElement {
    const card = [...document.querySelectorAll(".stat-card")].find(
      (element) => element.querySelector(".stat-label")?.textContent === label,
    );
    if (!card) throw new Error(`no stat card labelled ${label}`);
    return card as HTMLElement;
  }

  it("points each number at the list it was counted from", async () => {
    renderPage({
      permissions: ALL_PERMISSIONS,
      listDevices: () => Promise.resolve({ items: [device()], total: 1 }),
      listRemoteSessions: () => Promise.resolve({ items: [], total: 2 }),
    });
    await screen.findByText("ws-alpha");
    const tiles = [...document.querySelectorAll(".stat-card")].map((card) => [
      card.querySelector(".stat-label")?.textContent,
      card.tagName,
      card.getAttribute("href"),
    ]);
    expect(tiles).toEqual([
      ["Jobs needing attention", "A", "/jobs"],
      ["Open alerts", "A", "/health"],
      ["Managed endpoints", "A", "/devices"],
      ["Online now", "A", "/devices?state=online"],
      ["Open tickets", "A", "/service-desk"],
      ["Live remote sessions", "A", "/remote"],
    ]);
  });

  it("counts every open alert, not the page of active rows beside it", async () => {
    // THE DEFECT THIS PINS. The tile used to read `alertItems.length` -- the
    // length of `listHealthAlerts({ state: "active" })`. That under-reports
    // twice: the route's `state` filter takes ONE value, so acknowledged and
    // muted alerts (both open, per the health module) were never counted; and
    // a page of rows is bounded, so past the page size the tile silently
    // reported the cap as a total.
    //
    // The numbers below are chosen so the old implementation cannot pass:
    // one active row is returned, and the summary says nine are open. A tile
    // reading "1" is the bug; "9" is the fix.
    renderPage({
      permissions: ALL_PERMISSIONS,
      listHealthAlerts: () =>
        Promise.resolve([
          {
            id: "a1",
            severity: "critical",
            check_type: "disk.free",
            selector: "C:",
            device_hostname: "ws-alpha",
            observed_value: "3%",
            threshold_display: "< 10%",
          },
        ]),
      getHealthAlertSummary: () =>
        Promise.resolve({
          open: 9,
          by_state: { active: 1, acknowledged: 5, muted: 3 },
          by_severity: { info: 4, warning: 4, critical: 1 },
          worst_severity: "critical",
        }),
    });
    await screen.findByText("ws-alpha");
    const card = tile("Open alerts");
    await waitFor(() => expect(card.querySelector(".stat-value")?.textContent).toBe("9"));
  });

  it("says which zero it is on a quiet fleet, and does not shout it", async () => {
    // Nine tiles reading 0 on a page where nothing had been measured is what
    // this labelling exists to prevent: "no alert is open" is a measurement,
    // and it is not the same statement as "nothing has been measured".
    renderPage({ permissions: ALL_PERMISSIONS });
    await screen.findByText("ws-alpha");
    const card = tile("Open alerts");
    await waitFor(() => expect(card.querySelector(".stat-value")?.textContent).toBe("0"));
    expect(card.textContent).toContain("No alert is open");
    // `urgent` is dropped on a zero by StatCard itself; a red alarm card
    // reading 0 reads as a perfect score.
    expect(card.className).not.toContain("urgent");
  });

  it("makes the two counts that need a person tonight the loud ones", async () => {
    // Weight is urgency, colour is category. Six equally loud tiles are the
    // wall of numbers this row replaced.
    renderPage({
      permissions: ALL_PERMISSIONS,
      listHealthAlerts: () =>
        Promise.resolve([{ id: "a1", severity: "critical", check_type: "disk.free", selector: "C:", device_hostname: "ws-alpha", observed_value: "3%", threshold_display: "< 10%" }]),
      // `urgent` is dropped on a zero by StatCard, and the alert tile's number
      // is the summary's now -- so a non-zero summary is what keeps this tile
      // loud. Supplying rows alone no longer does it.
      getHealthAlertSummary: () =>
        Promise.resolve({
          open: 1,
          by_state: { active: 1, acknowledged: 0, muted: 0 },
          by_severity: { info: 0, warning: 0, critical: 1 },
          worst_severity: "critical",
        }),
      listJobs: () =>
        Promise.resolve({ items: [{ id: "j1", type: "inventory.collect", status: "failed", issued_at: "2026-08-16T09:00:00Z", attempts: 1 }], total: 1 }),
    });
    await screen.findByText("ws-alpha");
    await waitFor(() => {
      const loud = [...document.querySelectorAll(".stat-card.urgent")].map(
        (card) => card.querySelector(".stat-label")?.textContent,
      );
      expect(loud).toEqual(["Jobs needing attention", "Open alerts"]);
    });
  });

  it("gives every tile a second figure, and takes each from a response", async () => {
    // Three of the six carried no supporting line at all beside three that
    // did, so "Managed endpoints 14" read as the lesser fact next to "Open
    // tickets 3 · 1 untriaged". Each line below is a figure the page already
    // holds -- the fleet's two halves are each other's context, and a live
    // session's consent mode is the fact worth saying twice.
    renderPage({
      permissions: ALL_PERMISSIONS,
      listDevices: () =>
        Promise.resolve({
          items: [
            device(),
            device({ id: "2", hostname: "srv-beta", agent_status: "offline" }),
          ],
          total: 2,
          fleet: { total: 2, managed: 2, online: 1, cert_attention: 0 },
        }),
      listRemoteSessions: () =>
        Promise.resolve({
          items: [
            { id: "r1", consent_mode: "unattended" },
            { id: "r2", consent_mode: "attended" },
          ],
          total: 2,
        }),
    });
    await screen.findByText("ws-alpha");
    await waitFor(() => expect(tile("Managed endpoints").textContent).toContain("of 2 assets"));
    expect(tile("Online now").textContent).toContain("of 2 with an agent");
    // "Unattended" is somebody working on a machine with nobody watching. It
    // is the governance fact on this tile, not a decoration.
    expect(tile("Live remote sessions").textContent).toContain("1 attended · 1 unattended");
  });

  // THE REVIEW'S "14 here, 10 there". The tile counted every asset --
  // printers and switches included -- while /devices counts machines with an
  // agent. Both now read the server's fleet counts, which ignore the page.
  it("counts managed endpoints the way /devices does, not every asset", async () => {
    renderPage({
      permissions: ALL_PERMISSIONS,
      listDevices: () =>
        Promise.resolve({
          items: [device()],
          total: 14,
          fleet: { total: 14, managed: 10, online: 7, cert_attention: 0 },
        }),
    });
    await screen.findByText("ws-alpha");
    await waitFor(() =>
      expect(tile("Managed endpoints").querySelector(".stat-value")?.textContent).toBe("10"),
    );
    expect(tile("Managed endpoints").textContent).toContain("of 14 assets");
    expect(tile("Online now").querySelector(".stat-value")?.textContent).toBe("7");
    expect(tile("Online now").textContent).toContain("of 10 with an agent");
  });

  it("says nothing rather than counting a page as if it were the whole set", async () => {
    // `listRemoteSessions` returns a PAGE; a consent split over one row of
    // nine would be a sentence about the request.
    renderPage({
      permissions: ALL_PERMISSIONS,
      listRemoteSessions: () =>
        Promise.resolve({ items: [{ id: "r1", consent_mode: "attended" }], total: 9 }),
    });
    await screen.findByText("ws-alpha");
    await waitFor(() =>
      expect(tile("Live remote sessions").querySelector(".stat-value")?.textContent).toBe("9"),
    );
    expect(tile("Live remote sessions").textContent).not.toContain("attended");
  });

  it("counts the queue on the service desk panel, never the fetch", async () => {
    // The third fact read "Loaded 12": the number of rows this client happened
    // to receive, sitting between two real counts. Unassigned open work is the
    // queue fact it stood in front of.
    renderPage({
      permissions: ALL_PERMISSIONS,
      listTickets: () =>
        Promise.resolve({
          items: [
            { id: "t1", status: "open", assigned_to: null },
            { id: "t2", status: "open", assigned_to: "u1" },
            { id: "t3", status: "new", assigned_to: null },
            { id: "t4", status: "closed", assigned_to: null },
          ],
          total: 4,
        }),
    });
    await screen.findByText("ws-alpha");
    const panel = await screen.findByText("Service desk");
    const facts = panel.closest(".panel")?.textContent ?? "";
    expect(facts).toContain("Unassigned");
    expect(facts).not.toContain("Loaded");
    // Open: t1, t2, t3. Unassigned among those: t1 and t3 -- the closed one is
    // not open work whoever does or does not own it.
    const rows = [...(panel.closest(".panel")?.querySelectorAll(".facts > div") ?? [])].map(
      (row) => row.textContent,
    );
    expect(rows).toContain("Unassigned2");
  });

  it("admits when the ticket figures are counted over a page, not the queue", async () => {
    renderPage({
      permissions: ALL_PERMISSIONS,
      listTickets: () =>
        Promise.resolve({
          items: [{ id: "t1", status: "open", assigned_to: null }],
          total: 640,
        }),
    });
    await screen.findByText("ws-alpha");
    expect(
      await screen.findByText(/counted over the 1 most recent of 640 tickets/),
    ).toBeInTheDocument();
  });
});

/* ARRANGING THE DASHBOARD.

   The operator, 2026-09-01: "main dashboard should be customizable with tiles
   also give me the ability to resize and order in whichever way i want."

   THE MOVE/SIZE/HIDE BUTTONS ARE WHAT THESE TESTS DRIVE, and that is not
   because a drag is hard to simulate. WCAG 2.2 SC 2.5.7 requires a
   single-pointer alternative to every drag and SC 2.1.1 a keyboard one, and
   `docs/product.md` §11 makes AA binding here — so the buttons ARE the
   feature, and the drag is a convenience layered on top. A suite that only
   exercised dragging would go green on a dashboard nobody using a keyboard
   could rearrange. */
describe("customizing the dashboard", () => {
  /** The widget order as rendered, read off the grid. */
  function order(): string[] {
    return [...document.querySelectorAll(".dash-widget")].map(
      (node) => node.getAttribute("data-widget") ?? "",
    );
  }

  it("shows the stock arrangement, and no edit controls, until asked", async () => {
    renderPage();
    await screen.findByText("ws-alpha");
    expect(order()[0]).toBe("jobs-attention");
    // A dashboard whose tiles are always draggable fights its reader:
    // selecting a hostname to copy it becomes a drag.
    expect(screen.queryByRole("button", { name: /^Move /i })).not.toBeInTheDocument();
    expect(document.querySelector(".dash-widget.editing")).toBeNull();
  });

  it("moves a widget with a button, and saves the whole arrangement", async () => {
    const saved: unknown[] = [];
    renderPage({
      saveDashboardLayout: (widgets: unknown) => {
        saved.push(widgets);
        return Promise.resolve({ widgets });
      },
    });
    await screen.findByText("ws-alpha");
    await userEvent.click(screen.getByRole("button", { name: "Customize" }));

    // EVERY BUTTON NAMES ITS WIDGET. In edit mode there are three controls per
    // widget across a dozen widgets; three dozen buttons called "Move earlier"
    // are indistinguishable to anyone not looking at the screen.
    await userEvent.click(screen.getByRole("button", { name: "Move Open alerts earlier" }));

    expect(order().slice(0, 2)).toEqual(["open-alerts", "jobs-attention"]);
    await waitFor(() => expect(saved).toHaveLength(1));
    const first = saved[0] as { id: string }[];
    expect(first.map((entry) => entry.id).slice(0, 2)).toEqual(["open-alerts", "jobs-attention"]);
  });

  it("says it is a toggle, not two differently-named buttons", async () => {
    // A screen reader heard "Customize", then "Done", with nothing to say one
    // control had changed state. `Btn` has carried `pressed` since the kit
    // closed that gap; the caller simply never passed it.
    renderPage();
    await screen.findByText("ws-alpha");
    const customize = screen.getByRole("button", { name: "Customize" });
    expect(customize).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(customize);
    expect(screen.getByRole("button", { name: "Done" })).toHaveAttribute("aria-pressed", "true");
  });

  it("drops a half-finished drag when edit mode ends", async () => {
    // The stale state, not the styling, is the defect: `dragging`/`over`
    // survived Done, `DashboardWidgetFrame` paints `.dragging` from them
    // without consulting `editing`, so a widget kept the lifted look on a
    // read-only dashboard and re-entering edit mode resumed a drag nobody
    // started.
    renderPage();
    await screen.findByText("ws-alpha");
    await userEvent.click(screen.getByRole("button", { name: "Customize" }));

    const lifted = screen.getByRole("button", { name: /Move Open alerts earlier/ }).closest(".dash-widget");
    expect(lifted, "no widget frame for Open alerts").not.toBeNull();
    fireEvent.dragStart(lifted as Element);
    expect(lifted).toHaveClass("dragging");

    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(document.querySelector(".dash-widget.dragging")).toBeNull();
    expect(document.querySelector(".dash-widget.drop-target")).toBeNull();
  });

  it("disables the move that would fall off the end rather than doing nothing", async () => {
    // A button that silently no-ops reads as broken; disabled says why.
    renderPage();
    await screen.findByText("ws-alpha");
    await userEvent.click(screen.getByRole("button", { name: "Customize" }));
    expect(
      screen.getByRole("button", { name: "Move Jobs needing attention earlier" }),
    ).toBeDisabled();
  });

  it("cycles a widget's width and writes it through", async () => {
    const saved: { id: string; size: string }[][] = [];
    renderPage({
      saveDashboardLayout: (widgets: unknown) => {
        saved.push(widgets as { id: string; size: string }[]);
        return Promise.resolve({ widgets });
      },
    });
    await screen.findByText("ws-alpha");
    await userEvent.click(screen.getByRole("button", { name: "Customize" }));

    const tile = () => document.querySelector('[data-widget="managed-endpoints"]');
    expect(tile()?.getAttribute("data-size")).toBe("small");
    await userEvent.click(
      screen.getByRole("button", { name: /Managed endpoints is small; make it medium/ }),
    );
    expect(tile()?.getAttribute("data-size")).toBe("medium");
    await waitFor(() =>
      expect(saved.at(-1)?.find((entry) => entry.id === "managed-endpoints")?.size).toBe("medium"),
    );
  });

  it("hides a widget from the dashboard but keeps its place in edit mode", async () => {
    // Putting a widget back has to restore WHERE it was. A hidden widget
    // dropped from the arrangement makes "hide" and "forget where this was"
    // the same gesture, so it stays in the list, framed and empty.
    renderPage();
    await screen.findByText("ws-alpha");
    await userEvent.click(screen.getByRole("button", { name: "Customize" }));
    await userEvent.click(screen.getByRole("button", { name: "Hide Open alerts" }));

    expect(order()[1]).toBe("open-alerts");
    expect(screen.getByText("Hidden from your dashboard.")).toBeInTheDocument();
    expect(screen.getByText("1 tile is hidden.", { exact: false })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(order()).not.toContain("open-alerts");
    expect(order()[0]).toBe("jobs-attention");
  });

  it("renders a saved arrangement, and appends a widget it never heard of", async () => {
    // The reconciliation, through the page rather than the pure function: a
    // layout saved before this release must not hide the widgets this release
    // added.
    renderPage({
      getDashboardLayout: () =>
        Promise.resolve({
          widgets: [
            { id: "open-alerts", size: "wide", hidden: false },
            { id: "managed-endpoints", size: "small", hidden: false },
          ],
        }),
    });
    await screen.findByText("ws-alpha");
    await waitFor(() => expect(order()[0]).toBe("open-alerts"));
    expect(order()[1]).toBe("managed-endpoints");
    expect(document.querySelector('[data-widget="open-alerts"]')?.getAttribute("data-size")).toBe(
      "wide",
    );
    // Everything the stored row did not mention is still on the page.
    expect(order()).toContain("recent-devices");
  });

  it("keeps the arrangement on screen when the save fails, and says so", async () => {
    // Reverting would throw away work the operator can see themselves doing,
    // to reach a state they did not ask for.
    renderPage({
      saveDashboardLayout: () => Promise.reject(new Error("network down")),
    });
    await screen.findByText("ws-alpha");
    await userEvent.click(screen.getByRole("button", { name: "Customize" }));
    await userEvent.click(screen.getByRole("button", { name: "Move Open alerts earlier" }));

    expect(order()[0]).toBe("open-alerts");
    expect(await screen.findByRole("alert")).toHaveTextContent(/was not saved/i);
  });

  it("does not offer a widget the operator may not see", async () => {
    // Filtered BEFORE the layout resolves, so a role that changed for an
    // afternoon cannot rewrite somebody's saved arrangement.
    renderPage({ permissions: ["devices.read"] });
    await screen.findByText("ws-alpha");
    await userEvent.click(screen.getByRole("button", { name: "Customize" }));
    expect(order()).toEqual(["managed-endpoints", "online-now", "fleet-glance", "recent-devices"]);
    expect(screen.queryByRole("button", { name: /Open alerts/ })).not.toBeInTheDocument();
  });
});

describe("recent jobs speak the operator's language", () => {
  it("names each job the way /jobs does, never by its wire type", async () => {
    renderPage({
      listJobs: () =>
        Promise.resolve({
          items: [
            { id: "j1", type: "patch.apply", status: "succeeded", issued_at: "2026-08-16T10:00:00Z" },
            { id: "j2", type: "logs.ship", status: "running", issued_at: "2026-08-16T10:01:00Z" },
          ],
          total: 2,
        }),
    });
    const panel = (await screen.findByText("Recent jobs")).closest(".panel") as HTMLElement;
    await waitFor(() => expect(panel).toHaveTextContent("Apply patches"));
    expect(panel).toHaveTextContent("Ship agent logs");
    expect(panel).not.toHaveTextContent("patch.apply");
    expect(panel).not.toHaveTextContent("logs.ship");
  });

  it("keeps the panel subtitles free of engine vocabulary", async () => {
    renderPage();
    await screen.findByText("ws-alpha");
    expect(screen.queryByText(/flap-suppressed/)).not.toBeInTheDocument();
    expect(screen.queryByText(/outbound-only agent/)).not.toBeInTheDocument();
    // The page subtitle describes the page, not the account's permissions.
    expect(screen.queryByText(/this account may read/)).not.toBeInTheDocument();
  });
});

describe("Needs you", () => {
  function needsPanel(): HTMLElement {
    return screen.getByRole("heading", { name: "Needs you" }).closest(".panel") as HTMLElement;
  }

  it("leads the page", async () => {
    renderPage();
    await screen.findByText("ws-alpha");
    const panels = [...document.querySelectorAll(".panel")];
    expect(panels[0]).toBe(needsPanel());
  });

  it("says calmly when nothing needs anyone", async () => {
    renderPage({
      listDevices: () =>
        Promise.resolve({
          items: [device()],
          total: 1,
          fleet: { total: 1, managed: 1, online: 1, cert_attention: 0 },
        }),
    });
    await screen.findByText("ws-alpha");
    await waitFor(() => expect(needsPanel()).toHaveTextContent("Nothing needs you right now"));
  });

  it("ranks what needs a person and links each to where it is handled", async () => {
    renderPage({
      listDevices: () =>
        Promise.resolve({
          items: [device()],
          total: 5,
          fleet: { total: 5, managed: 4, online: 2, cert_attention: 0 },
        }),
      listJobs: () =>
        Promise.resolve({
          items: [
            { id: "j1", type: "patch.apply", status: "failed", issued_at: "2026-08-16T10:00:00Z" },
          ],
          total: 1,
        }),
      listHealthAlerts: () =>
        Promise.resolve([
          {
            id: "a9",
            severity: "critical",
            state: "active",
            check_type: "disk.free_pct",
            selector: "C:",
            device_hostname: "db-01",
            observed_value: "3.1",
            threshold_display: "10",
          },
        ]),
    });
    await screen.findByText("ws-alpha");
    await waitFor(() =>
      expect(
        [...needsPanel().querySelectorAll("a")].map((a) => [a.textContent, a.getAttribute("href")]),
      ).toEqual([
        [expect.stringContaining("Disk free on db-01"), "/health?alert=a9"],
        [expect.stringContaining("1 job failed"), "/jobs"],
        [expect.stringContaining("2 devices are offline"), "/devices?state=offline"],
      ]),
    );
  });

  it("is not drawn for an account that can read none of its sources", async () => {
    renderPage({ permissions: [] });
    await screen.findByText(/holds no module read permissions/i);
    expect(screen.queryByRole("heading", { name: "Needs you" })).not.toBeInTheDocument();
  });
});
