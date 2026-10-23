import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me } from "../../../lib/api";
import { AlertConsole } from "../AlertConsole";
import { AlertDetail } from "../AlertDetail";
import { ApprovedActionList, decisionBody } from "../ApprovedActionList";
import { DeviceHealthPanel } from "../DeviceHealthPanel";
import { HealthReportPanel } from "../HealthReportPanel";
import { HealthSettingsPanel } from "../HealthSettingsPanel";
import { ProfileEditor } from "../ProfileEditor";
import { ProfileList } from "../ProfileList";
import type {
  AlertDetailOut,
  AlertOut,
  ApprovedActionOut,
  CheckOut,
  CheckTypeOut,
  DeviceHealthOut,
  ProfileChecksOut,
} from "../types";

// Component tests against RENDERED OUTPUT. The auth-backed API client is mocked
// so nothing here touches fetch -- mirrors modules/sla/__tests__/sla.test.tsx.
//
// EVERY ASSERTION BELOW IS ABOUT A DISTINCTION, not about wording. Where a
// label is matched it is matched because two DIFFERENT inputs must produce
// different text: an acknowledged alert against a cleared one, an offline
// device against a reporting one, an `unavailable` check against an `ok` one, an
// autonomous approved action against a `require_approval` one. A test that
// asserted only "the page rendered" would pass against every collapse this
// module exists to prevent.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWithProviders(node: React.ReactNode, at = "/health") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[at]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * The signed-in operator, whose PROFILE carries the zone every instant in this
 * module is rendered in (`api.ts#useUserTimezone`, UX remediation cycle 5).
 *
 * `timezone: null` by default -- the shape for someone who never set one, and
 * the one every pre-existing test in this file was written against. The zone
 * tests pass an explicit one.
 */
function me(timezone: string | null = null): Me {
  return {
    user_id: "u1",
    org_id: "o1",
    email: "tech@example.com",
    display_name: "Tech",
    permissions: [],
    timezone,
  };
}

function stubApi(overrides: Partial<ApiClient>): ApiClient {
  const api = {
    me: vi.fn().mockResolvedValue(me()),
    listHealthAlerts: vi.fn().mockResolvedValue([]),
    getHealthAlert: vi.fn(),
    getDeviceHealth: vi.fn(),
    getHealthProfileChecks: vi.fn(),
    listHealthCheckTypes: vi.fn().mockResolvedValue([]),
    listApprovedActions: vi.fn().mockResolvedValue([]),
    ...overrides,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

function alert(overrides: Partial<AlertOut> = {}): AlertOut {
  return {
    id: "a1",
    device_id: "d1",
    device_hostname: "WS-01",
    device_group_id: null,
    check_id: "c1",
    profile_id: "p1",
    check_type: "cpu.busy_pct",
    selector: "",
    severity: "warning",
    state: "active",
    observed_value: "97.4",
    threshold_display: "95",
    opened_at: "2026-03-01T09:00:00.000Z",
    opened_policy_version: 1,
    last_seen_at: "2026-03-01T09:05:00.000Z",
    occurrence_count: 2,
    flapping: false,
    flap_count: 0,
    escalated_at: null,
    acknowledged_at: null,
    acknowledged_by: null,
    muted_until: null,
    muted_by: null,
    cleared_at: null,
    cleared_reason: null,
    cleared_by: null,
    ticket_id: null,
    previous_alert_id: null,
    ...overrides,
  };
}

const PROFILE = {
  id: "p1",
  name: "Fleet baseline",
  description: "",
  tick_interval_s: 300,
  posture_interval_s: 3600,
  summary_interval_s: 1800,
  active: true,
  policy_version: 3,
  check_count: 2,
  assignment_count: 1,
  created_at: "2026-01-01T00:00:00.000Z",
  updated_at: "2026-01-01T00:00:00.000Z",
};

function deviceHealth(overrides: Partial<DeviceHealthOut> = {}): DeviceHealthOut {
  return {
    device_id: "d1",
    hostname: "WS-01",
    os_family: "windows",
    has_profile: true,
    profile: PROFILE,
    assignment: {
      id: "as1",
      profile_id: "p1",
      profile_name: "Fleet baseline",
      device_id: null,
      device_group_id: "grp-accounts",
      level: "group",
      created_by: null,
      created_at: "2026-01-01T00:00:00.000Z",
    },
    resolution_level: "group",
    acknowledged_policy_version: 3,
    policy_drift: false,
    checks: [],
    open_alerts: [],
    agent_last_seen_at: "2026-03-01T09:05:00.000Z",
    agent_online: true,
    offline_after_s: 900,
    ...overrides,
  };
}

function checkOut(overrides: Partial<CheckOut> = {}): CheckOut {
  return {
    id: "c1",
    profile_id: "p1",
    check_type: "cpu.busy_pct",
    selector: "",
    comparator: "gt",
    threshold_num: 95,
    threshold_bool: null,
    threshold_display: "95",
    for_ticks: 3,
    for_seconds: 900,
    tick_interval_s: 300,
    severity: "warning",
    os_family: null,
    enabled: true,
    declares_notify: true,
    declares_open_ticket: true,
    remediate_approved_action_id: null,
    value_kind: "numeric",
    cadence: "tick",
    evaluated_by: "agent",
    available_os_families: ["linux", "macos", "windows"],
    selector_meaning: "",
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

function approvedAction(overrides: Partial<ApprovedActionOut> = {}): ApprovedActionOut {
  return {
    id: "act1",
    key: "collect-inventory",
    name: "Collect inventory",
    description: "",
    job_type: "inventory.collect",
    params_template: {},
    params_template_redacted_keys: [],
    script_id: null,
    script_name: null,
    script_version: null,
    script_live_version: null,
    risk_class: "low",
    destructive: false,
    max_target_scope: "device",
    autonomy: "require_approval",
    personas: ["health"],
    cooldown_s: 3600,
    max_per_device_per_day: 3,
    max_per_org_per_hour: 20,
    active: true,
    script_exemption: null,
    created_by: "u1",
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

// --------------------------------------------------------------------------
// The alert console: acknowledged, muted and cleared must not render alike.
// --------------------------------------------------------------------------

describe("AlertConsole", () => {
  it("renders acknowledged, muted and cleared as three different rows", async () => {
    stubApi({
      listHealthAlerts: vi.fn().mockResolvedValue([
        alert({ id: "ack", device_hostname: "ACK-HOST", state: "acknowledged" }),
        alert({
          id: "muted",
          device_hostname: "MUTED-HOST",
          state: "muted",
          muted_until: "2026-03-02T09:00:00.000Z",
        }),
        alert({
          id: "cleared",
          device_hostname: "CLEARED-HOST",
          state: "cleared",
          cleared_at: "2026-03-01T10:00:00.000Z",
          cleared_reason: "recovery",
        }),
      ]),
    });
    renderWithProviders(<AlertConsole onSelect={() => {}} />);

    const ack = await waitFor(() => screen.getByText("ACK-HOST").closest("tr") as HTMLElement);
    const muted = screen.getByText("MUTED-HOST").closest("tr") as HTMLElement;
    const cleared = screen.getByText("CLEARED-HOST").closest("tr") as HTMLElement;

    expect(within(ack).getByText("Acknowledged")).toBeInTheDocument();
    expect(within(muted).getByText("Muted")).toBeInTheDocument();
    expect(within(cleared).getByText("Cleared")).toBeInTheDocument();

    // The acknowledged and muted rows carry NO clear outcome -- they are open.
    // The em-dash placeholder is what makes "open" visible rather than an empty
    // cell that could equally mean "we did not load it".
    expect(within(ack).getByText("—")).toBeInTheDocument();
    expect(within(muted).getByText("—")).toBeInTheDocument();
    // ... and the muted row says WHEN the silence ends, so it cannot read as
    // permanently handled.
    expect(within(muted).getByText(/^until/)).toBeInTheDocument();
    // Only the cleared row names an outcome.
    expect(within(cleared).getByText(/Cleared — Recovered/)).toBeInTheDocument();
    expect(within(cleared).queryByText("—")).not.toBeInTheDocument();

    // "1 open of 3 shown": the acknowledged and muted rows count as OPEN.
    expect(screen.getByText("2 open of 3 shown")).toBeInTheDocument();
  });

  it("narrows to one device when the device page links here, and offers the way out", async () => {
    const listHealthAlerts = vi
      .fn()
      .mockResolvedValue([alert({ id: "a", device_id: "d1", device_hostname: "WS-01" })]);
    stubApi({ listHealthAlerts });
    renderWithProviders(<AlertConsole onSelect={() => {}} />, "/health?device=d1&state=open");
    await screen.findByText("Show every device");
    // The server filters by device; the console does not fetch the fleet.
    expect(listHealthAlerts).toHaveBeenCalledWith(expect.objectContaining({ device_id: "d1" }));
    expect(screen.getByText(/One device/)).toBeInTheDocument();
  });

  it("puts the device and its state first, which is what fits on a phone", async () => {
    stubApi({ listHealthAlerts: vi.fn().mockResolvedValue([alert({ id: "a" })]) });
    renderWithProviders(<AlertConsole onSelect={() => {}} />);
    await screen.findByText("WS-01");
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers.slice(0, 2)).toEqual(["Device", "State"]);
  });

  it("heads a row with the check as a phrase, and keeps the selector under it", async () => {
    // The row was headed `disk.free_pct (/var/lib/postgresql)` -- the
    // registry's identifier and its argument -- so a reader parsed a key
    // before they read the incident. v3 writes the same row as prose.
    stubApi({
      listHealthAlerts: vi
        .fn()
        .mockResolvedValue([
          alert({ check_type: "disk.free_pct", selector: "/var/lib/postgresql" }),
        ]),
    });
    renderWithProviders(<AlertConsole onSelect={() => {}} />);

    const row = await waitFor(() => screen.getByText("WS-01").closest("tr") as HTMLElement);
    expect(within(row).getByText("Disk free")).toBeInTheDocument();
    // The selector is a fact of its own: "Disk free" on two mounts of one
    // machine is two different incidents.
    expect(within(row).getByText("/var/lib/postgresql")).toBeInTheDocument();
    expect(within(row).queryByText(/disk\.free_pct/)).not.toBeInTheDocument();
  });

  it("keeps a hostname on one line, because it is what a person scans for", async () => {
    // At 390px `lis-ws-041` broke at its own hyphens across three lines. The
    // table outgrows its scroller instead -- `.table-wrap` is what that
    // horizontal scroll and its tab stop exist for.
    stubApi({
      listHealthAlerts: vi.fn().mockResolvedValue([alert({ device_hostname: "lis-ws-041" })]),
    });
    renderWithProviders(<AlertConsole onSelect={() => {}} />);

    const host = await waitFor(() => screen.getByText("lis-ws-041"));
    expect(host).toHaveStyle({ whiteSpace: "nowrap" });
  });

  // ------------------------------------------------------------------
  // THE OTHER HALF OF "NO DEAD TILES". A tile's `href` is only a promise; these
  // two are the delivery. Each renders the console AT one of the report
  // panel's destinations and asserts the rows that come back are the ones the
  // figure counted -- so a filter that silently did nothing, or narrowed to
  // the wrong set, goes red here rather than in front of an operator.
  // ------------------------------------------------------------------
  it("selects every OPEN alert at ?state=open, not just the active one", async () => {
    // The whole reason "Still open" cannot link to `state=active`: three of
    // these four are open and they wear three different states.
    stubApi({
      listHealthAlerts: vi.fn().mockResolvedValue([
        alert({ id: "a", device_hostname: "ACTIVE-HOST", state: "active" }),
        alert({ id: "b", device_hostname: "ACK-HOST", state: "acknowledged" }),
        alert({
          id: "c",
          device_hostname: "MUTED-HOST",
          state: "muted",
          muted_until: "2026-03-02T09:00:00.000Z",
        }),
        alert({
          id: "d",
          device_hostname: "CLEARED-HOST",
          state: "cleared",
          cleared_reason: "recovery",
          cleared_at: "2026-03-01T10:00:00.000Z",
        }),
      ]),
    });
    renderWithProviders(<AlertConsole onSelect={() => {}} />, "/health?state=open");

    await screen.findByText("ACTIVE-HOST");
    expect(screen.getByText("ACK-HOST")).toBeInTheDocument();
    expect(screen.getByText("MUTED-HOST")).toBeInTheDocument();
    // ... and the cleared one is gone, which is what makes this a filter and
    // not a no-op that happens to render everything.
    expect(screen.queryByText("CLEARED-HOST")).toBeNull();
    expect(screen.getByText("3 open of 3 shown")).toBeInTheDocument();
  });

  it("splits machine-resolved from human-closed at ?resolution=, on the panel's own predicate", async () => {
    const alerts = [
      alert({
        id: "m",
        device_hostname: "RECOVERED-HOST",
        state: "cleared",
        cleared_reason: "recovery",
        cleared_at: "2026-03-01T09:10:00.000Z",
      }),
      alert({
        id: "h",
        device_hostname: "CLOSED-HOST",
        state: "cleared",
        cleared_reason: "manual",
        cleared_at: "2026-03-01T09:30:00.000Z",
      }),
      alert({
        id: "x",
        device_hostname: "SENSOR-GONE-HOST",
        state: "cleared",
        cleared_reason: "unavailable_timeout",
        cleared_at: "2026-03-02T09:00:00.000Z",
      }),
    ];

    stubApi({ listHealthAlerts: vi.fn().mockResolvedValue(alerts) });
    const machine = renderWithProviders(
      <AlertConsole onSelect={() => {}} />,
      "/health?state=cleared&resolution=machine",
    );
    await screen.findByText("RECOVERED-HOST");
    expect(screen.queryByText("CLOSED-HOST")).toBeNull();
    expect(screen.queryByText("SENSOR-GONE-HOST")).toBeNull();
    machine.unmount();

    stubApi({ listHealthAlerts: vi.fn().mockResolvedValue(alerts) });
    const human = renderWithProviders(
      <AlertConsole onSelect={() => {}} />,
      "/health?state=cleared&resolution=human",
    );
    await screen.findByText("CLOSED-HOST");
    expect(screen.queryByText("RECOVERED-HOST")).toBeNull();
    human.unmount();

    // The non-fix is reachable and is in NEITHER resolution bucket -- the rule
    // the report panel's "excluded" number exists to make visible.
    stubApi({ listHealthAlerts: vi.fn().mockResolvedValue(alerts) });
    renderWithProviders(
      <AlertConsole onSelect={() => {}} />,
      "/health?state=cleared&resolution=excluded",
    );
    await screen.findByText("SENSOR-GONE-HOST");
    expect(screen.queryByText("RECOVERED-HOST")).toBeNull();
    expect(screen.queryByText("CLOSED-HOST")).toBeNull();
  });

  it("treats an unreadable filter as no filter, rather than as a filter matching nothing", async () => {
    // A mistyped query string must not make the console say the fleet is
    // quiet. `?state=opne` is one keystroke from the real thing.
    stubApi({
      listHealthAlerts: vi.fn().mockResolvedValue([alert({ device_hostname: "WS-01" })]),
    });
    renderWithProviders(<AlertConsole onSelect={() => {}} />, "/health?state=opne&resolution=nope");
    expect(await screen.findByText("WS-01")).toBeInTheDocument();
    expect(screen.queryByText(/No alerts match this filter/)).toBeNull();
  });

  it("says so when the caller cannot read alerts, instead of showing an empty list", async () => {
    stubApi({
      listHealthAlerts: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")),
    });
    renderWithProviders(<AlertConsole onSelect={() => {}} />);
    expect(
      await screen.findByText(/Alerts not available \(insufficient permissions\)/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No alerts match this filter/)).not.toBeInTheDocument();
  });

  // ------------------------------------------------------------------
  // GLOSSARY (`docs/design/settings-language.md`): "flap/flapping" is the
  // engine's word; the operator-facing word is "repeating". The sort control
  // and the count badge are the two places on this page that used to say
  // "flapping" out loud.
  // ------------------------------------------------------------------
  it("offers the sort toggle as 'repeating', not 'flapping'", async () => {
    const user = (await import("@testing-library/user-event")).default;
    stubApi({ listHealthAlerts: vi.fn().mockResolvedValue([alert()]) });
    renderWithProviders(<AlertConsole onSelect={() => {}} />);

    const toggle = await screen.findByRole("button", { name: "Sort repeating first" });
    expect(screen.queryByRole("button", { name: /flapping/i })).toBeNull();
    await user.click(toggle);
    expect(await screen.findByRole("button", { name: "Sort by severity" })).toBeInTheDocument();
  });

  it("badges a repeating alert as 'repeating ×N', not 'flapping ×N'", async () => {
    stubApi({
      listHealthAlerts: vi.fn().mockResolvedValue([
        alert({ device_hostname: "FLAP-HOST", flapping: true, flap_count: 4 }),
      ]),
    });
    renderWithProviders(<AlertConsole onSelect={() => {}} />);

    const row = await waitFor(() => screen.getByText("FLAP-HOST").closest("tr") as HTMLElement);
    expect(within(row).getByText("repeating ×4")).toBeInTheDocument();
    expect(within(row).queryByText(/flapping/i)).toBeNull();
  });
});

// --------------------------------------------------------------------------
// The alert detail: remediation state, including WHY something was blocked.
// --------------------------------------------------------------------------

describe("AlertDetail", () => {
  function detail(overrides: Partial<AlertDetailOut> = {}): AlertDetailOut {
    return {
      ...alert(),
      profile_name: "Fleet baseline",
      ticket_number: "T-000123",
      previous_alert_ticket_number: null,
      previous_alert_opened_at: null,
      monitoring_deep_link: null,
      actions: [],
      ...overrides,
    };
  }

  it("renders a blocked remediation with the fuse, its window and the job ids", async () => {
    const fuse =
      "max_per_org_per_hour: 20 script.run job(s) already ran across this org in the last hour " +
      "(limit 20); first job ids ['job-a', 'job-b']";
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue(
        detail({
          actions: [
            {
              id: "ar1",
              alert_id: "a1",
              kind: "remediate",
              state: "blocked_rate_limit",
              approved_action_id: "act1",
              approved_action_key: "restart-spooler",
              approved_action_name: "Restart the spooler",
              script_id: "s1",
              script_name: "Restart Spooler",
              script_version: 4,
              autonomy: "require_approval",
              risk_class: "medium",
              destructive: false,
              job_id: null,
              ticket_id: null,
              attempts: 1,
              next_attempt_at: null,
              approved_by: null,
              approved_at: null,
              expires_at: null,
              last_error: fuse,
              created_at: "2026-03-01T09:00:00.000Z",
              updated_at: "2026-03-01T09:00:00.000Z",
            },
          ],
        }),
      ),
    });
    renderWithProviders(
      <AlertDetail alertId="a1" canManageAlerts canRemediate />,
    );

    expect(await screen.findByText("Blocked by a rate limit")).toBeInTheDocument();
    // WHY, verbatim: the fuse, its window and the job ids counted against it.
    expect(screen.getByText(new RegExp("max_per_org_per_hour"))).toBeInTheDocument();
    expect(screen.getByText(/job-a/)).toBeInTheDocument();
    // A blocked action is not offered an approve button -- it did not reach a
    // human decision.
    expect(screen.queryByRole("button", { name: /Approve and dispatch/ })).toBeNull();
  });

  it("names the pinned script and version on an action awaiting approval", async () => {
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue(
        detail({
          actions: [
            {
              id: "ar1",
              alert_id: "a1",
              kind: "remediate",
              state: "awaiting_approval",
              approved_action_id: "act1",
              approved_action_key: "restart-spooler",
              approved_action_name: "Restart the spooler",
              script_id: "s1",
              script_name: "Restart Spooler",
              script_version: 4,
              autonomy: "require_approval",
              risk_class: "medium",
              destructive: true,
              job_id: null,
              ticket_id: null,
              attempts: 0,
              next_attempt_at: null,
              approved_by: null,
              approved_at: null,
              expires_at: "2026-03-02T09:00:00.000Z",
              last_error: null,
              created_at: "2026-03-01T09:00:00.000Z",
              updated_at: "2026-03-01T09:00:00.000Z",
            },
          ],
        }),
      ),
    });
    renderWithProviders(<AlertDetail alertId="a1" canManageAlerts canRemediate />);

    expect(await screen.findByText("Awaiting approval")).toBeInTheDocument();
    // Spec §13: "nobody approves a script they cannot identify" -- the NAME and
    // the VERSION, both.
    const pinned = screen.getByText(/Pinned script:/);
    expect(pinned).toHaveTextContent("Restart Spooler");
    expect(pinned).toHaveTextContent("v4");
    expect(screen.getByRole("button", { name: /Approve and dispatch/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Refuse$/ })).toBeInTheDocument();
  });

  it("hides the approve control from a caller without health.remediate", async () => {
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue(
        detail({
          actions: [
            {
              id: "ar1",
              alert_id: "a1",
              kind: "remediate",
              state: "awaiting_approval",
              approved_action_id: "act1",
              approved_action_key: "restart-spooler",
              approved_action_name: "Restart the spooler",
              script_id: null,
              script_name: null,
              script_version: null,
              autonomy: "require_approval",
              risk_class: "low",
              destructive: false,
              job_id: null,
              ticket_id: null,
              attempts: 0,
              next_attempt_at: null,
              approved_by: null,
              approved_at: null,
              expires_at: null,
              last_error: null,
              created_at: "2026-03-01T09:00:00.000Z",
              updated_at: "2026-03-01T09:00:00.000Z",
            },
          ],
        }),
      ),
    });
    renderWithProviders(<AlertDetail alertId="a1" canManageAlerts canRemediate={false} />);
    // The row is still SHOWN -- a technician must see that a remediation is
    // waiting on somebody. Only the control is withheld, and the server would
    // refuse it anyway (invariant #6).
    expect(await screen.findByText("Awaiting approval")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve and dispatch/ })).toBeNull();
  });

  it("does not offer alert transitions on a cleared alert, and names why it cleared", async () => {
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue(
        detail({
          state: "cleared",
          cleared_at: "2026-03-02T09:00:00.000Z",
          cleared_reason: "unavailable_timeout",
        }),
      ),
    });
    renderWithProviders(<AlertDetail alertId="a1" canManageAlerts canRemediate />);

    expect(await screen.findByText("Sensor went away.")).toBeInTheDocument();
    // §7.4's exclusion, on the screen rather than only in the report.
    expect(screen.getByText("not counted as a fix")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Acknowledge/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Close manually/ })).toBeNull();
  });

  it("offers acknowledge on an active alert and only mute/close once acknowledged", async () => {
    stubApi({ getHealthAlert: vi.fn().mockResolvedValue(detail({ state: "acknowledged", acknowledged_at: "2026-03-01T09:30:00.000Z" })) });
    renderWithProviders(<AlertDetail alertId="a1" canManageAlerts canRemediate />);

    // Acknowledged is OPEN, so the transitions that remain are still offered...
    expect(await screen.findByRole("button", { name: /^Mute$/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Close manually/ })).toBeInTheDocument();
    // ... but acknowledging again is not a transition that exists.
    expect(screen.queryByRole("button", { name: /^Acknowledge$/ })).toBeNull();
    expect(screen.getByText(/STILL OPEN|still open/i)).toBeInTheDocument();
  });

  // ------------------------------------------------------------------
  // GLOSSARY: "repeating", not "flapping" (same badge as the console row,
  // same rule). And the acknowledged state's own explanation of what it does
  // NOT do says "fix", not "remediation".
  // ------------------------------------------------------------------
  it("badges a repeating alert as 'repeating ×N', not 'flapping ×N'", async () => {
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue(detail({ flapping: true, flap_count: 3 })),
    });
    renderWithProviders(<AlertDetail alertId="a1" canManageAlerts canRemediate />);

    expect(await screen.findByText("repeating ×3")).toBeInTheDocument();
    expect(screen.queryByText(/flapping/i)).toBeNull();
  });

  it("says 'fix', not 'remediation', in what acknowledging does not do", async () => {
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue(
        detail({ state: "acknowledged", acknowledged_at: "2026-03-01T09:30:00.000Z" }),
      ),
    });
    renderWithProviders(<AlertDetail alertId="a1" canManageAlerts canRemediate />);

    const subtitle = await screen.findByText(/still open/i);
    expect(subtitle).toHaveTextContent(/fix/i);
    expect(subtitle).not.toHaveTextContent(/remediation/i);
    expect(subtitle).not.toHaveTextContent(/suppress/i);
  });

  // ------------------------------------------------------------------
  // JUDGEMENT CALL: "Approve a destructive remediation" is a
  // destructive-action confirmation, not a routine label. Plain "fix" would
  // read as routine for a signed job that runs as SYSTEM/root and cannot be
  // undone from the console -- so the glossary's remediation -> fix rewrite
  // is deliberately NOT applied here. This test pins that exception so a
  // future sweep does not "fix" it by accident.
  // ------------------------------------------------------------------
  it("keeps the stronger word in the destructive-approval confirmation, on purpose", async () => {
    const user = (await import("@testing-library/user-event")).default;
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue(
        detail({
          actions: [
            {
              id: "ar1",
              alert_id: "a1",
              kind: "remediate",
              state: "awaiting_approval",
              approved_action_id: "act1",
              approved_action_key: "wipe-temp",
              approved_action_name: "Wipe temp directory",
              script_id: null,
              script_name: null,
              script_version: null,
              autonomy: "require_approval",
              risk_class: "high",
              destructive: true,
              job_id: null,
              ticket_id: null,
              attempts: 0,
              next_attempt_at: null,
              approved_by: null,
              approved_at: null,
              expires_at: null,
              last_error: null,
              created_at: "2026-03-01T09:00:00.000Z",
              updated_at: "2026-03-01T09:00:00.000Z",
            },
          ],
        }),
      ),
    });
    renderWithProviders(<AlertDetail alertId="a1" canManageAlerts canRemediate />);

    await user.click(await screen.findByRole("button", { name: "Approve and dispatch" }));
    expect(screen.getByText("Approve a destructive remediation")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// The device view.
// --------------------------------------------------------------------------

describe("DeviceHealthPanel", () => {
  it("says 'no health profile' rather than showing a healthy, empty device", async () => {
    stubApi({
      getDeviceHealth: vi
        .fn()
        .mockResolvedValue(deviceHealth({ has_profile: false, profile: null, assignment: null, resolution_level: null })),
    });
    renderWithProviders(<DeviceHealthPanel deviceId="d1" canCheckNow />);

    expect(await screen.findByText("No health profile")).toBeInTheDocument();
    expect(
      screen.getByText(/No health profile resolves to this device/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Healthy")).toBeNull();
  });

  it("names which assignment produced the effective profile", async () => {
    stubApi({ getDeviceHealth: vi.fn().mockResolvedValue(deviceHealth()) });
    renderWithProviders(<DeviceHealthPanel deviceId="d1" canCheckNow />);

    expect(await screen.findByText("Applied by")).toBeInTheDocument();
    // Spec §0.2: the LEVEL and the target, not merely "a profile applies".
    expect(screen.getByText(/a group assignment \(grp-accounts\)/)).toBeInTheDocument();
    expect(screen.getByText("Fleet baseline")).toBeInTheDocument();
  });

  it("shows unavailable and never-reported as their own bands, not as healthy", async () => {
    stubApi({
      getDeviceHealth: vi.fn().mockResolvedValue(
        deviceHealth({
          checks: [
            {
              check_id: "c1",
              check_type: "cpu.busy_pct",
              selector: "",
              comparator: "gt",
              threshold_display: "95",
              severity: "warning",
              for_ticks: 3,
              for_seconds: 900,
              evaluated_by: "agent",
              state: "ok",
              last_value: "12.5",
              policy_version: 3,
              reported_at: "2026-03-01T09:05:00.000Z",
            },
            {
              check_id: "c2",
              check_type: "posture.antivirus_healthy",
              selector: "",
              comparator: "is",
              threshold_display: "true",
              severity: "critical",
              for_ticks: 1,
              for_seconds: 300,
              evaluated_by: "agent",
              state: "unavailable",
              last_value: "",
              policy_version: 3,
              reported_at: "2026-03-01T09:05:00.000Z",
            },
            {
              check_id: "c3",
              check_type: "memory.used_pct",
              selector: "",
              comparator: "gt",
              threshold_display: "90",
              severity: "warning",
              for_ticks: 3,
              for_seconds: 900,
              evaluated_by: "agent",
              state: "unknown",
              last_value: "",
              policy_version: null,
              reported_at: null,
            },
          ],
        }),
      ),
    });
    renderWithProviders(<DeviceHealthPanel deviceId="d1" canCheckNow />);

    // FOUR bands, each with its own count. `unavailable` is not inside "within
    // threshold" and neither is "never reported".
    expect(await screen.findByText("1 within threshold")).toBeInTheDocument();
    expect(screen.getByText("0 breaching")).toBeInTheDocument();
    expect(screen.getByText("1 unavailable")).toBeInTheDocument();
    expect(screen.getByText("1 never reported")).toBeInTheDocument();
    // The device as a whole is NOT called healthy while a check is unanswered.
    expect(screen.queryByText("Healthy")).toBeNull();
    expect(screen.getByText("Partially answered")).toBeInTheDocument();
    // The per-row states are distinct too.
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("Never reported")).toBeInTheDocument();
  });

  it("does not let a quiet device read as a healthy one", async () => {
    stubApi({
      getDeviceHealth: vi.fn().mockResolvedValue(
        deviceHealth({
          agent_online: false,
          agent_last_seen_at: "2026-03-01T02:00:00.000Z",
          open_alerts: [alert({ id: "still-open", state: "acknowledged" })],
        }),
      ),
    });
    renderWithProviders(<DeviceHealthPanel deviceId="d1" canCheckNow />);

    expect(await screen.findByText("agent offline")).toBeInTheDocument();
    // Said twice on purpose, and both are asserted: the HEADLINE (an open alert
    // outranks "offline", and says the alert is still live) and the offline
    // BANNER (a quiet device is not a healthy one). A page carrying only one of
    // them still lets a reader conclude the wrong thing from the other.
    expect(
      screen.getByText(/Alerts do not clear when a device goes offline/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Alerts do not clear because a device went quiet/),
    ).toBeInTheDocument();
    // The open alert survived the device going quiet, and is shown as open.
    expect(screen.getByText("Acknowledged")).toBeInTheDocument();
    expect(screen.queryByText("No open alerts.")).toBeNull();
  });

  it("says an empty alert list on an offline device is not evidence of health", async () => {
    stubApi({
      getDeviceHealth: vi
        .fn()
        .mockResolvedValue(deviceHealth({ agent_online: false, open_alerts: [] })),
    });
    renderWithProviders(<DeviceHealthPanel deviceId="d1" canCheckNow={false} />);
    expect(
      await screen.findByText(/not reporting, so that is not evidence of health/),
    ).toBeInTheDocument();
    // And the sample-now control is withheld from a caller without
    // `health.alert.manage` (§10's amendment: it mints a signed job).
    expect(screen.queryByRole("button", { name: /Sample now/ })).toBeNull();
  });

  it("shows policy drift when the agent has not acknowledged the current version", async () => {
    stubApi({
      getDeviceHealth: vi
        .fn()
        .mockResolvedValue(
          deviceHealth({ acknowledged_policy_version: null, policy_drift: true }),
        ),
    });
    renderWithProviders(<DeviceHealthPanel deviceId="d1" canCheckNow />);
    expect(await screen.findByText(/agent has never acknowledged one/)).toBeInTheDocument();
    expect(screen.queryByText("acknowledged")).toBeNull();
  });
});

// --------------------------------------------------------------------------
// The profile editor.
// --------------------------------------------------------------------------

describe("ProfileEditor", () => {
  const CHECK_TYPES: CheckTypeOut[] = [
    {
      name: "posture.antivirus_healthy",
      value_kind: "boolean",
      cadence: "posture",
      evaluated_by: "agent",
      os_families: ["windows"],
      legal_comparators: ["is"],
      selector_meaning: "",
      enum_values: [],
    },
    {
      name: "agent.offline",
      value_kind: "numeric",
      cadence: "posture",
      evaluated_by: "server",
      os_families: ["linux", "macos", "windows"],
      legal_comparators: ["gt"],
      selector_meaning: "",
      enum_values: [],
    },
  ];

  function checks(rows: CheckOut[]): ProfileChecksOut {
    return { profile_id: "p1", policy_version: 3, tick_interval_s: 300, checks: rows };
  }

  it("renders a duration in both checks and minutes, naming how often the profile checks", async () => {
    stubApi({
      getHealthProfileChecks: vi.fn().mockResolvedValue(checks([checkOut()])),
      listHealthCheckTypes: vi.fn().mockResolvedValue(CHECK_TYPES),
    });
    renderWithProviders(<ProfileEditor profileId="p1" canAdmin />);

    const cell = await screen.findByText(/3 checks in a row/);
    expect(cell).toHaveTextContent("15 min");
    expect(cell).toHaveTextContent("5 min");
  });

  it("warns, inline, which OS families cannot answer a check", async () => {
    stubApi({
      getHealthProfileChecks: vi.fn().mockResolvedValue(
        checks([
          checkOut({
            id: "c2",
            check_type: "posture.antivirus_healthy",
            comparator: "is",
            threshold_num: null,
            threshold_bool: true,
            threshold_display: "true",
            value_kind: "boolean",
            available_os_families: ["windows"],
          }),
        ]),
      ),
      listHealthCheckTypes: vi.fn().mockResolvedValue(CHECK_TYPES),
    });
    renderWithProviders(<ProfileEditor profileId="p1" canAdmin />);

    // Spec §13: an operator must see BEFORE saving that this will be
    // `unavailable` on their Linux estate.
    expect(await screen.findByText("unavailable on linux, macos")).toBeInTheDocument();
  });

  it("shows a silent-by-design check as silent rather than as ordinary", async () => {
    stubApi({
      getHealthProfileChecks: vi
        .fn()
        .mockResolvedValue(
          checks([
            checkOut({ id: "loud" }),
            checkOut({ id: "quiet", check_type: "memory.used_pct", declares_notify: false }),
          ]),
        ),
      listHealthCheckTypes: vi.fn().mockResolvedValue(CHECK_TYPES),
    });
    renderWithProviders(<ProfileEditor profileId="p1" canAdmin />);

    expect(await screen.findByText("silent by design")).toBeInTheDocument();
    expect(screen.getByText("notify")).toBeInTheDocument();
  });

  it("warns that removing a check clears its open alerts as 'not a fix'", async () => {
    const user = (await import("@testing-library/user-event")).default;
    stubApi({
      getHealthProfileChecks: vi.fn().mockResolvedValue(checks([checkOut()])),
      listHealthCheckTypes: vi.fn().mockResolvedValue(CHECK_TYPES),
    });
    renderWithProviders(<ProfileEditor profileId="p1" canAdmin />);

    await user.click(await screen.findByRole("button", { name: "Remove" }));
    // "Remove a row" looks reversible; its effect on open alerts is not.
    expect(screen.getByText(/cleared as/)).toBeInTheDocument();
    expect(screen.getByText(/excluded from resolution reporting/)).toBeInTheDocument();
  });

  // ------------------------------------------------------------------
  // GLOSSARY: "remediation" is the engine's word; the operator-facing word
  // is "fix" (`docs/design/settings-language.md`).
  // ------------------------------------------------------------------
  it("badges a check with no declared action as 'no fix', not 'no remediation'", async () => {
    stubApi({
      getHealthProfileChecks: vi.fn().mockResolvedValue(checks([checkOut()])),
      listHealthCheckTypes: vi.fn().mockResolvedValue(CHECK_TYPES),
    });
    renderWithProviders(<ProfileEditor profileId="p1" canAdmin />);

    expect(await screen.findByText("no fix")).toBeInTheDocument();
    expect(screen.queryByText(/no remediation/i)).toBeNull();
  });

  it("badges a declared action as 'fix: <key>', not 'remediate: <key>'", async () => {
    stubApi({
      getHealthProfileChecks: vi.fn().mockResolvedValue(
        checks([checkOut({ remediate_approved_action_id: "act1" })]),
      ),
      listHealthCheckTypes: vi.fn().mockResolvedValue(CHECK_TYPES),
      listApprovedActions: vi
        .fn()
        .mockResolvedValue([approvedAction({ id: "act1", key: "restart-spooler" })]),
    });
    renderWithProviders(<ProfileEditor profileId="p1" canAdmin />);

    expect(await screen.findByText("fix: restart-spooler")).toBeInTheDocument();
    expect(screen.queryByText(/remediate:/i)).toBeNull();
  });
});

// --------------------------------------------------------------------------
// The approved-action registry.
// --------------------------------------------------------------------------

describe("ApprovedActionList", () => {
  it("renders an autonomous action distinctly from a require_approval one", async () => {
    stubApi({
      listApprovedActions: vi.fn().mockResolvedValue([
        approvedAction({ id: "auto", key: "auto-collect", autonomy: "autonomous" }),
        approvedAction({ id: "gated", key: "gated-collect", autonomy: "require_approval" }),
      ]),
    });
    renderWithProviders(<ApprovedActionList canActionAdmin />);

    const autoRow = (await screen.findByText("auto-collect")).closest("tr") as HTMLElement;
    const gatedRow = screen.getByText("gated-collect").closest("tr") as HTMLElement;

    // Spec §13: "a page where 'runs itself on 400 machines' looks like every
    // other row is a page that gets clicked past". The distinction is asserted
    // on the ROW, not only on the badge text.
    expect(autoRow.className).not.toEqual(gatedRow.className);
    expect(autoRow.getAttribute("data-autonomy")).toBe("autonomous");
    // Sentence case, like every other pill on the page: the distinction the
    // spec asks for is carried by the row above, not by shouting.
    expect(within(autoRow).getByText("Runs automatically")).toBeInTheDocument();
    expect(within(gatedRow).getByText("Needs approval")).toBeInTheDocument();
    // And the notice counts in English -- noun AND verb agreeing with the one.
    expect(screen.getByText("1 action runs automatically")).toBeInTheDocument();
    expect(screen.queryByText(/action\(s\)/)).not.toBeInTheDocument();
  });

  it("shows script exemptions as a first-class list, with grantor and reason", async () => {
    stubApi({
      listApprovedActions: vi.fn().mockResolvedValue([
        approvedAction({
          id: "s",
          key: "restart-spooler",
          job_type: "script.run",
          script_id: "sc1",
          script_name: "Restart Spooler",
          script_version: 4,
          script_live_version: 4,
          destructive: false,
          script_exemption: {
            id: "ex1",
            script_id: "sc1",
            script_name: "Restart Spooler",
            script_version: 4,
            reason: "reviewed by the platform team",
            granted_by: "u-42",
            granted_at: "2026-02-01T00:00:00.000Z",
          },
        }),
      ]),
    });
    renderWithProviders(<ApprovedActionList canActionAdmin />);

    // Wait on DATA, not on the static heading: the heading renders before the
    // query resolves, so awaiting it would assert over an empty list.
    expect(await screen.findByText("restart-spooler")).toBeInTheDocument();
    expect(screen.getByText("Scripts marked safe")).toBeInTheDocument();
    // An exemption outlives the action that motivated it, so every field a
    // reviewer needs to decide whether to revoke it is on the row.
    expect(screen.getByText("reviewed by the platform team")).toBeInTheDocument();
    expect(screen.getByText("u-42")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revoke" })).toBeInTheDocument();
  });

  it("flags an action whose pinned script version has been edited away", async () => {
    stubApi({
      listApprovedActions: vi.fn().mockResolvedValue([
        approvedAction({
          key: "stale-pin",
          job_type: "script.run",
          script_id: "sc1",
          script_name: "Restart Spooler",
          script_version: 4,
          script_live_version: 5,
          destructive: true,
        }),
      ]),
    });
    renderWithProviders(<ApprovedActionList canActionAdmin={false} />);
    // The action will REFUSE to dispatch. Finding that out from a `last_error`
    // at 03:00 with an alert already open is the failure this column prevents.
    // The pill says what class of problem it is; the consequence follows it as
    // prose, because twelve words in a pill cannot wrap and were setting the
    // width of the whole table.
    expect(await screen.findByText("Script edited")).toBeInTheDocument();
    expect(
      screen.getByText(/Live is v5, so this action will refuse to run\./),
    ).toBeInTheDocument();
  });

  // ------------------------------------------------------------------
  // GLOSSARY: "remediation" is the engine's word; the operator-facing word
  // is "fix" (`docs/design/settings-language.md`).
  // ------------------------------------------------------------------
  it("says 'no fixes', not 'no remediation', when the registry is empty", async () => {
    stubApi({});
    renderWithProviders(<ApprovedActionList canActionAdmin />);

    expect(
      await screen.findByText("No approved actions. No fixes can be dispatched by this organisation."),
    ).toBeInTheDocument();
  });

  it("says 'fix', not 'remediation', in the revoke and deactivate confirmation bodies", () => {
    const revoked = approvedAction({ key: "restart-spooler", autonomy: "autonomous" });
    expect(decisionBody({ kind: "revoke", action: revoked })).toMatch(
      /the fix people believe is running stops running/,
    );
    expect(decisionBody({ kind: "revoke", action: revoked })).not.toMatch(/remediation/i);

    const active = approvedAction({ key: "restart-spooler", active: true });
    expect(decisionBody({ kind: "active", action: active })).toMatch(
      /stops fixing.*the fix simply is never declared/,
    );
    expect(decisionBody({ kind: "active", action: active })).not.toMatch(/remediat/i);
  });
});

// --------------------------------------------------------------------------
// Reporting (spec §7.4's obligation).
// --------------------------------------------------------------------------

describe("HealthReportPanel", () => {
  it("separates machine-resolved from human-closed and excludes the non-fixes", async () => {
    stubApi({
      listHealthAlerts: vi.fn().mockResolvedValue([
        alert({
          id: "1",
          state: "cleared",
          cleared_reason: "recovery",
          opened_at: "2026-03-01T09:00:00.000Z",
          cleared_at: "2026-03-01T09:10:00.000Z",
        }),
        alert({
          id: "2",
          state: "cleared",
          cleared_reason: "manual",
          opened_at: "2026-03-01T09:00:00.000Z",
          cleared_at: "2026-03-01T09:30:00.000Z",
        }),
        alert({
          id: "3",
          state: "cleared",
          cleared_reason: "unavailable_timeout",
          opened_at: "2026-03-01T09:00:00.000Z",
          cleared_at: "2026-03-02T09:00:00.000Z",
        }),
      ]),
    });
    renderWithProviders(<HealthReportPanel />);

    // Wait on a DERIVED number, not on the static label: every label renders
    // before the query resolves, so awaiting one would assert over zeroes.
    // 50%, not 67%: the `unavailable_timeout` row is in neither numerator nor
    // denominator.
    expect(await screen.findByText("50%")).toBeInTheDocument();
    // `closest("a")`, NOT `closest("div")`: every figure on this panel is a
    // tile that links to the alerts it counted, so the tile IS the anchor. The
    // old selector climbed to the surrounding grid, where "1" matches several
    // tiles at once.
    const machine = screen.getByText("Machine-resolved").closest("a") as HTMLElement;
    const human = screen.getByText("Closed by a person").closest("a") as HTMLElement;
    expect(within(machine).getByText("1")).toBeInTheDocument();
    expect(within(human).getByText("1")).toBeInTheDocument();
    // And the two link to DIFFERENT populations. A panel whose whole point is
    // that machine-resolved and human-closed are separate numbers must not
    // send both to the same list.
    expect(machine).toHaveAttribute("href", "/health?state=cleared&resolution=machine");
    expect(human).toHaveAttribute("href", "/health?state=cleared&resolution=human");
    expect(screen.getByText("1 excluded from resolution counts")).toBeInTheDocument();
    // MTTR is (10 min + 30 min) / 2 = 20 min. Including the 24-hour non-fix
    // would make it over 8 hours, which is exactly how a conflating dashboard
    // lies.
    expect(screen.getByText("20 min")).toBeInTheDocument();
  });

  // ------------------------------------------------------------------
  // "All tiles and all numbers or KPIs need to be clickable and direct the
  // user to the specific menus, I don't want dead tiles everywhere."
  //
  // This test is the thing that stops that decaying. It asserts the EXACT
  // destination of each figure, not merely that an `href` exists: a tile
  // pointing at the wrong population is the defect this panel is built to
  // avoid, wearing a link. The two behavioural tests below then prove those
  // destinations are live — that `?state=open` and `?resolution=machine`
  // actually select the rows the numbers counted, rather than being strings
  // nothing reads.
  // ------------------------------------------------------------------
  it("links every figure to the alerts it was counted over", async () => {
    stubApi({
      listHealthAlerts: vi.fn().mockResolvedValue([
        alert({ id: "1", state: "active" }),
        alert({
          id: "2",
          state: "cleared",
          cleared_reason: "recovery",
          cleared_at: "2026-03-01T09:10:00.000Z",
        }),
      ]),
    });
    renderWithProviders(<HealthReportPanel />);

    await screen.findByText("100%");
    const destinations: ReadonlyArray<readonly [string, string]> = [
      ["Alerts in view", "/health"],
      ["Still open", "/health?state=open"],
      ["Mean time to resolve", "/health?state=cleared&resolution=resolved"],
      ["Machine-resolved", "/health?state=cleared&resolution=machine"],
      ["Closed by a person", "/health?state=cleared&resolution=human"],
      ["Auto-fix rate", "/health?state=cleared&resolution=resolved"],
    ];
    for (const [label, href] of destinations) {
      const tile = screen.getByText(label).closest("a");
      expect(tile, `${label} is not a link`).not.toBeNull();
      expect(tile).toHaveAttribute("href", href);
    }
    // The excluded count is a number too, and it had nowhere to go either.
    expect(screen.getByText("0 excluded from resolution counts").closest("a")).toHaveAttribute(
      "href",
      "/health?state=cleared&resolution=excluded",
    );
  });

  it("says WHICH zero a zero is, so 'nothing open' cannot read as 'nothing measured'", async () => {
    stubApi({ listHealthAlerts: vi.fn().mockResolvedValue([]) });
    renderWithProviders(<HealthReportPanel />);

    // Both are 0 and they mean different things: no alerts have ever been
    // raised, versus none of the ones raised are still open.
    expect(await screen.findByText("no alerts have been raised")).toBeInTheDocument();
    expect(screen.getByText("nothing is open")).toBeInTheDocument();
    // A zero never wears the urgent treatment, whatever the tile asked for.
    expect(screen.getByText("Still open").closest("a")).not.toHaveClass("urgent");
  });

  it("says the split is a proxy for the spec's measure, and claims no false reason", async () => {
    // THE FIGURES ARE NOT THE MEASURE THE SPEC DEFINES, and a panel that says
    // nothing is claiming they are. So the caveat stays -- what left it, under
    // the copy-voice spec, is the API archaeology explaining WHY the specified
    // measure cannot be computed (`GET /service-desk/tickets` has no `origin`
    // filter, so the alert-origin population is unselectable). That is a docs
    // fact; the proxy warning is the operator's.
    //
    // `no route exposes that history` is pinned because it is FALSE --
    // `GET /service-desk/tickets/{id}/history` exists and returns `actor_type`
    // -- and it shipped once. It must not come back with the shorter copy.
    stubApi({ listHealthAlerts: vi.fn().mockResolvedValue([]) });
    renderWithProviders(<HealthReportPanel />);

    const caveat = await screen.findByText(/read from each alert.s own clear reason/);
    expect(caveat.textContent).toContain("not from the ticket history the specification");
    expect(document.body.textContent).not.toContain("no route exposes that history");
  });
});

// --------------------------------------------------------------------------
// The two forms in this module, after the cycle-3 form kernel.
//
// EVERY TEST BELOW IS ABOUT A REFUSAL THAT USED TO BE SILENT. The module's
// create and save controls were `<Button disabled={pending || !field}>` with
// no `<form>` around them: the operator pressed, nothing moved, and the panel
// named no field. Asserting "the mutation was not called" alone would pass
// against exactly that defect, so each test asserts BOTH that the write did
// not happen AND that the reason was announced.
// --------------------------------------------------------------------------

describe("ProfileList — create and row actions", () => {
  function stubProfiles(overrides: Partial<ApiClient> = {}) {
    return stubApi({
      listHealthProfiles: vi.fn().mockResolvedValue([PROFILE]),
      listHealthAssignments: vi.fn().mockResolvedValue([]),
      createHealthProfile: vi.fn().mockResolvedValue(PROFILE),
      deleteHealthProfile: vi.fn().mockResolvedValue(undefined),
      ...overrides,
    });
  }

  it("tells the operator a delete failed when the server refused with a bodyless 503", async () => {
    // THE BLOCKER, in Auditor A's words: "A failed destructive API call (HTTP
    // 503) is silently swallowed — the UI shows success and the user is never
    // told." `ApiError` is built from `await response.text()`, so a proxy 503
    // carries `""`; the old `describeError(error)` is `error.message`, which
    // rendered an EMPTY paragraph over a row that looked deleted.
    const user = (await import("@testing-library/user-event")).default;
    const deleteHealthProfile = vi.fn().mockRejectedValue(new ApiError(503, ""));
    stubProfiles({ deleteHealthProfile });
    renderWithProviders(<ProfileList canAdmin onSelect={() => {}} />);

    await user.click(await screen.findByRole("button", { name: /^More actions for / }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    // Delete now asks first (cycle 8), so the refusal only happens on the far
    // side of the confirmation.
    await user.click(await screen.findByRole("button", { name: "Delete profile" }));

    const failure = await screen.findByRole("alert");
    // Which control failed, and what actually happened to the profile.
    expect(failure).toHaveTextContent("Delete failed");
    expect(failure).toHaveTextContent(/unavailable right now/);
    expect(failure).toHaveTextContent(/Nothing was changed/);
  });

  it("does not style Delete like the Clone action beside it", async () => {
    // Auditor A, HIGH: Delete's class was "btn ghost" -- byte-identical to
    // Clone and Retire, 68px away in a cramped 2x2 cluster. The confirmation
    // (pinned by the 503 test above) asks about a misclick that has already
    // happened; this is what makes it less likely in the first place.
    const user = userEvent.setup();
    stubProfiles();
    renderWithProviders(<ProfileList canAdmin onSelect={() => {}} />);

    await user.click(await screen.findByRole("button", { name: /^More actions for / }));
    expect(screen.getByRole("menuitem", { name: "Delete" }).className).toContain("menu-item-destructive");
    expect(screen.getByRole("menuitem", { name: "Clone" }).className).not.toContain("destructive");
    for (const label of ["Retire", "Checks"]) {
      expect(screen.getByRole("button", { name: label }).className).not.toContain("danger");
    }
  });

  it("names the empty field rather than leaving Create inert", async () => {
    // "'Create profile' with an empty name is a silent no-op." The control now
    // stays ENABLED with the field empty — pressing it is how the operator
    // finds out which field is at fault.
    const user = (await import("@testing-library/user-event")).default;
    const createHealthProfile = vi.fn().mockResolvedValue(PROFILE);
    stubProfiles({ createHealthProfile });
    renderWithProviders(<ProfileList canAdmin onSelect={() => {}} />);

    const create = await screen.findByRole("button", { name: "Create" });
    expect(create).toBeEnabled();
    await user.click(create);

    expect(await screen.findByRole("alert")).toHaveTextContent("New profile name is required");
    expect(createHealthProfile).not.toHaveBeenCalled();
  });

  it("refuses a markup-like profile name and says why", async () => {
    // "Profile names accept arbitrary markup-like strings with no format
    // validation." The name is echoed into alert text, ticket subjects and
    // audit entries; one that reads as markup is unusable in all three.
    const user = (await import("@testing-library/user-event")).default;
    const createHealthProfile = vi.fn().mockResolvedValue(PROFILE);
    stubProfiles({ createHealthProfile });
    renderWithProviders(<ProfileList canAdmin onSelect={() => {}} />);

    await user.type(await screen.findByLabelText("New profile name"), "<b>Fleet</b>");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Profile name cannot contain < or >",
    );
    expect(createHealthProfile).not.toHaveBeenCalled();
  });

  it("submits on Enter, because the controls are inside a real form", async () => {
    // There was no `<form>` here at all, so Enter in the name field did
    // nothing — the keystroke every operator tries first.
    const user = (await import("@testing-library/user-event")).default;
    const createHealthProfile = vi.fn().mockResolvedValue(PROFILE);
    stubProfiles({ createHealthProfile });
    renderWithProviders(<ProfileList canAdmin onSelect={() => {}} />);

    await user.type(await screen.findByLabelText("New profile name"), "Laptops{Enter}");

    await waitFor(() => expect(createHealthProfile).toHaveBeenCalledWith({ name: "Laptops" }));
  });
});

describe("HealthSettingsPanel", () => {
  const SETTINGS = {
    flap_window_s: 3600,
    flap_threshold: 5,
    flap_cooldown_s: 3600,
    unavailable_clear_after_s: 86400,
    alert_retention_days: 90,
    escalate_after_s: 3600,
    offline_after_s: 900,
    approval_ttl_s: 3600,
    default_alert_queue_id: "q1",
    default_ticket_category_id: "c1",
    monitoring_deep_link_template: null,
    auto_resolve_alert_tickets: true,
    configured: true,
    alert_tickets_enabled: true,
    updated_at: "2026-03-01T09:00:00.000Z",
    updated_by: "ops",
  };

  function stubSettings(overrides: Partial<ApiClient> = {}) {
    return stubApi({
      getHealthSettings: vi.fn().mockResolvedValue(SETTINGS),
      updateHealthSettings: vi.fn().mockResolvedValue(SETTINGS),
      ...overrides,
    });
  }

  it("enforces the server's bound instead of hanging a decorative max on the input", async () => {
    // "min/max constraints on all eight settings fields are decorative — the
    // panel is not a <form>, so nothing is validated client-side." `min`/`max`
    // are only ever enforced at form submission, and there was no form, so the
    // attributes did nothing at all.
    const user = (await import("@testing-library/user-event")).default;
    const updateHealthSettings = vi.fn().mockResolvedValue(SETTINGS);
    stubSettings({ updateHealthSettings });
    renderWithProviders(<HealthSettingsPanel canAdmin />);

    const retention = await screen.findByLabelText("Delete closed alerts after");
    await user.clear(retention);
    await user.type(retention, "5000");
    await user.click(screen.getByRole("button", { name: "Save" }));

    // The bound is quoted, so the operator can fix it without opening the spec.
    // `alert_retention_days` is a COUNT, not a duration, so the bound reads as
    // the raw number of days it always was -- see the "still sends days"
    // test below for why that distinction is load-bearing here. `and 3650
    // DAYS`, not a bare `3650`: the field's own unit went missing along with
    // the old label's `(days)` parenthetical, and this is the one place a
    // refusal quotes a raw count back at the operator, so it is the one
    // place the word cannot be allowed to stay silent.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Delete closed alerts after must be between 90 and 3650 days",
    );
    expect(updateHealthSettings).not.toHaveBeenCalled();
  });

  it("keeps letters typed into a numeric field and names them", async () => {
    // "Typing letters into a numeric field silently erases it, and the empty
    // result passes validation." A `type="number"` input reports `value === ""`
    // for "abc", so the keystrokes vanished and the field then looked merely
    // untouched — indistinguishable from one nobody had edited.
    const user = (await import("@testing-library/user-event")).default;
    const updateHealthSettings = vi.fn().mockResolvedValue(SETTINGS);
    stubSettings({ updateHealthSettings });
    renderWithProviders(<HealthSettingsPanel canAdmin />);

    const retention = await screen.findByLabelText("Delete closed alerts after");
    await user.clear(retention);
    await user.type(retention, "abc");
    expect(retention).toHaveValue("abc");

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Delete closed alerts after must be a whole number",
    );
    expect(updateHealthSettings).not.toHaveBeenCalled();
  });

  it("keeps every edit after a failed save, so the rejected value can be corrected", async () => {
    // "A failed save silently reverts every field, so the user cannot correct
    // the value the error complains about." `setEdits({})` sat beside
    // `mutate`, so the 422 arrived over fields that had already snapped back
    // to the server's values.
    const user = (await import("@testing-library/user-event")).default;
    const updateHealthSettings = vi
      .fn()
      .mockRejectedValue(new ApiError(422, "alert_retention_days is below the retention floor"));
    stubSettings({ updateHealthSettings });
    renderWithProviders(<HealthSettingsPanel canAdmin />);

    const retention = await screen.findByLabelText("Delete closed alerts after");
    await user.clear(retention);
    await user.type(retention, "3000");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("below the retention floor");
    // The value the server complained about is still on screen.
    expect(retention).toHaveValue("3000");
  });

  it("says nothing has changed rather than presenting an inert Save", async () => {
    // The last shape of the same defect: "nothing to save" was expressed by
    // disabling the control, which is a press that moves nothing and explains
    // nothing.
    const user = (await import("@testing-library/user-event")).default;
    const updateHealthSettings = vi.fn().mockResolvedValue(SETTINGS);
    stubSettings({ updateHealthSettings });
    renderWithProviders(<HealthSettingsPanel canAdmin />);

    const save = await screen.findByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByRole("alert")).toHaveTextContent("Nothing has changed yet");
    expect(updateHealthSettings).not.toHaveBeenCalled();
  });

  // ------------------------------------------------------------------
  // Cycle 12. The two settings findings the earlier cycles left unpinned.
  // ------------------------------------------------------------------

  it("translates the settings 422 instead of printing the Pydantic payload at the operator", async () => {
    // Auditor A, HIGH, at THIS call site: submitting `Offline after = -5`
    // printed the response body verbatim beside Save --
    // `{"detail":[{"type":"greater_than_equal","loc":["body","offline_after_s"],
    // "msg":"Input should be greater than or equal to 300","input":-5,
    // "ctx":{"ge":300}}]}`. The panel's own bound catches -5 first now, so the
    // payload is driven in from a bound the client does not hold: the operator
    // must still get a sentence, and must never get `loc`, `ctx` or the echoed
    // `input` (which is how a submitted value reaches a screenshot).
    const user = (await import("@testing-library/user-event")).default;
    const updateHealthSettings = vi.fn().mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: [
            {
              type: "greater_than_equal",
              loc: ["body", "offline_after_s"],
              msg: "Input should be greater than or equal to 300",
              input: -5,
              ctx: { ge: 300 },
            },
          ],
        }),
      ),
    );
    stubSettings({ updateHealthSettings });
    renderWithProviders(<HealthSettingsPanel canAdmin />);

    const offline = await screen.findByLabelText("Mark a device offline after");
    await user.clear(offline);
    await user.type(offline, "1200");
    await user.click(screen.getByRole("button", { name: "Save" }));

    const failure = await screen.findByRole("alert");
    // The field, then the message. (`describeSubmitError` humanises the wire
    // name and renders "Offline after s" for `offline_after_s` -- the trailing
    // unit suffix survives, which is cosmetic; what matters here is that the
    // operator gets the field and the sentence rather than the envelope.)
    expect(failure).toHaveTextContent(/Offline after/);
    expect(failure).toHaveTextContent("Input should be greater than or equal to 300");
    expect(failure.textContent).not.toContain("greater_than_equal");
    expect(failure.textContent).not.toContain("loc");
    expect(failure.textContent).not.toContain("ctx");
  });

  // ------------------------------------------------------------------
  // Language pass, 2026-09-11 (`docs/design/settings-language.md`, "Second
  // surfaces carrying the old words"). This panel used to hand-roll its own
  // copy of the Health fields -- old labels, raw seconds, no duration
  // control -- disagreeing with the shipped `Settings → Health` tab about
  // what the same setting is called. It now renders the SAME labels and help
  // strings, through the SAME `DurationInput`. The two tests below pin the
  // one thing that migration could get wrong in each direction: a duration
  // field must still put seconds on the wire, and the one field that is NOT
  // a duration (`alert_retention_days`, stored in DAYS --
  // `sweep.py`'s `timedelta(days=...)`) must never be run through a control
  // built to convert.
  it("keeps alert retention a plain number of days -- never run through the duration control", async () => {
    // `alert_retention_days` is a COUNT, not a duration: `120` here has to
    // stay "120". Fed through `DurationInput` it would read as 120 SECONDS,
    // and 120 is divisible by 60, so the bug would not even show up as
    // garbage -- it would confidently display "2" beside a "minutes" picker,
    // and a save would silently write a two-minute retention policy.
    const user = (await import("@testing-library/user-event")).default;
    const updateHealthSettings = vi.fn().mockResolvedValue(SETTINGS);
    stubSettings({
      getHealthSettings: vi.fn().mockResolvedValue({ ...SETTINGS, alert_retention_days: 120 }),
      updateHealthSettings,
    });
    renderWithProviders(<HealthSettingsPanel canAdmin />);

    const retention = await screen.findByLabelText("Delete closed alerts after");
    expect(retention).toHaveValue("120");
    // No unit picker exists for this field -- `DurationInput` always renders
    // one beside its number input, so its absence is itself the proof this
    // is the plain count control.
    expect(screen.queryByLabelText("Delete closed alerts after unit")).toBeNull();

    await user.clear(retention);
    await user.type(retention, "2000");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(updateHealthSettings).toHaveBeenCalledWith(
        expect.objectContaining({ alert_retention_days: 2000 }),
      ),
    );
  });

  it("puts seconds on the wire for a duration field, however the operator chose to read it", async () => {
    // `offline_after_s` (bounds 300-86400) seeded at 900 -- 15 minutes --
    // displays through the minutes unit `DurationInput` auto-selects, and an
    // edit in that unit has to convert BACK to seconds before it reaches
    // `updateHealthSettings`. The wire format is the one thing this control
    // is built to never change.
    const user = (await import("@testing-library/user-event")).default;
    const updateHealthSettings = vi.fn().mockResolvedValue(SETTINGS);
    stubSettings({ updateHealthSettings });
    renderWithProviders(<HealthSettingsPanel canAdmin />);

    const offline = await screen.findByLabelText("Mark a device offline after");
    expect(offline).toHaveValue(15);
    const unit = screen.getByLabelText("Mark a device offline after unit");
    expect(unit).toHaveValue("minutes");

    await user.clear(offline);
    await user.type(offline, "20");
    await user.click(screen.getByRole("button", { name: "Save" }));

    // 20 minutes on screen, 1200 seconds on the wire -- and ONLY the touched
    // field, since this panel's save is PATCH-shaped.
    await waitFor(() =>
      expect(updateHealthSettings).toHaveBeenCalledWith({ offline_after_s: 1200 }),
    );
  });

  it("names the number input by the field's own label alone, not by the wrapping label's full content", async () => {
    // `Field` wraps `DurationInput` inside a `<label>`, and the HTML label
    // algorithm resolves that wrapping label's CONTROL to the number input --
    // but a browser's ACCESSIBLE NAME for that control is computed from the
    // label's full rendered text, unit select included, unless the control
    // has its own explicit name. Without `aria-label`, the spinbutton read
    // as "Mark a device offline after minutes" and changed the moment the
    // operator picked a different unit -- `toHaveAccessibleName` here uses
    // the same name-computation algorithm a screen reader does (unlike
    // `getByLabelText`, which does not), so it is the one assertion in this
    // suite that can actually see the defect a browser would.
    const user = (await import("@testing-library/user-event")).default;
    stubSettings();
    renderWithProviders(<HealthSettingsPanel canAdmin />);

    const offline = await screen.findByLabelText("Mark a device offline after");
    expect(offline).toHaveAccessibleName("Mark a device offline after");

    // Switching units re-renders the select's own visible text ("hours"
    // instead of "minutes") -- the number input's name must not move with it.
    await user.selectOptions(screen.getByLabelText("Mark a device offline after unit"), "hours");
    expect(offline).toHaveAccessibleName("Mark a device offline after");

    // The unit select keeps its OWN distinct name throughout, never silently
    // inheriting (or polluting) the field's.
    expect(screen.getByLabelText("Mark a device offline after unit")).toHaveAccessibleName(
      "Mark a device offline after unit",
    );
  });

  it("quotes a duration field's bound as a duration, not as the raw seconds this control exists to hide", async () => {
    // `escalate_after_s` bounds are 300-604800 seconds. A bound message that
    // printed those numbers verbatim would put exactly what `DurationInput`
    // is for keeping off the operator's screen back onto it, one refusal
    // after they typed into a control showing minutes or hours.
    const user = (await import("@testing-library/user-event")).default;
    stubSettings();
    renderWithProviders(<HealthSettingsPanel canAdmin />);

    const escalate = await screen.findByLabelText("Escalate an unanswered alert after");
    await user.clear(escalate);
    await user.type(escalate, "999999");
    await user.click(screen.getByRole("button", { name: "Save" }));

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent(
      "Escalate an unanswered alert after must be between 5 min and 7 d",
    );
    expect(failure.textContent).not.toMatch(/\b(300|604800)\b/);
  });

  it("renders exactly one .field per row, not a hand-rolled one nested inside Field's own", async () => {
    // THE KNOWN TRAP THIS FILE USED TO CARRY: the old render put a hand-rolled
    // `<div className="field">` (holding the help paragraph) AROUND a
    // `<Field>` -- which is ALSO `.field` -- so every hint on the panel
    // doubled shell/components.css's `.field` margin and caption rule. The
    // operator's report was "every field's help sentence has a horizontal
    // line through its first line"; jsdom lays nothing out, so the ORIGINAL
    // version of this test could only pin a negative-margin proxy for that,
    // not the doubled wrapper itself. The migration to `Field` deletes the
    // outer div outright, which this test can now assert directly: there is
    // no `.field` nested inside another `.field` anywhere in the panel.
    stubSettings();
    const { container } = renderWithProviders(<HealthSettingsPanel canAdmin />);

    const retention = await screen.findByLabelText("Delete closed alerts after");
    expect(container.querySelector(".field .field")).toBeNull();

    // `Field` owns the caption/aria-describedby wiring now; the help
    // paragraph carries `Field`'s own `.help` class and sits OUTSIDE the
    // `<label>` -- inside it would silently append the whole hint to the
    // input's accessible NAME, announced twice: once as the name, once
    // through `aria-describedby`.
    const help = document.getElementById(
      retention.getAttribute("aria-describedby") as string,
    ) as HTMLElement;
    expect(help).not.toBeNull();
    expect(help.className).toBe("help");
    expect(retention).toHaveAccessibleName("Delete closed alerts after");
    expect(retention.closest("label")).not.toContainElement(help);
  });

  it("disables Save for a caller who may not edit, and says so beside it", async () => {
    // Disabled is reserved for "you may not do this at all" — and then the
    // reason is on screen, not in a tooltip.
    stubSettings();
    renderWithProviders(<HealthSettingsPanel canAdmin={false} />);

    expect(await screen.findByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByText(/do not have permission to change health settings/)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// UX remediation cycle 5 — one datetime format, in the reader's zone.
//
// WHAT WAS WRONG. Every instant in this module went through
// `AlertConsole#formatInstant`, which was
// `new Date(v).toISOString().replace("T", " ").slice(0, 16)`. That renders in
// UTC, ALWAYS, and says nowhere that it has. An operator in Bogota reading
// "Last contact 2026-03-01 09:05" was looking at a moment their own clock
// called 04:05 — Auditor B's five-hour BLOCKER, on the display side, in a
// second format that agreed with nothing else in the product.
//
// EACH TEST BELOW PINS THE ZONE AND THE FORMAT TOGETHER, because either alone
// passes against the defect: a UTC renderer produces the right digits for a
// UTC reader, and a correctly-zoned renderer in a bespoke format still gives
// the audit's "the same field reads two ways on one page".
// --------------------------------------------------------------------------

// UTC-5 year round, so a fixed offset is enough to state the expectation and
// no test here depends on which side of a DST boundary it runs.
const BOGOTA = "America/Bogota";
// `dd Mmm yyyy, HH:MM` — the one product format (`lib/datetime`).
const PRODUCT_FORMAT = /^\d{2} \w{3} \d{4}, \d{2}:\d{2}$/;

describe("datetime rendering — the reader's zone, one format", () => {
  it("renders an alert's last-seen in the operator's profile zone, not UTC", async () => {
    stubApi({
      me: vi.fn().mockResolvedValue(me(BOGOTA)),
      listHealthAlerts: vi
        .fn()
        .mockResolvedValue([alert({ last_seen_at: "2026-03-01T09:05:00.000Z" })]),
    });
    renderWithProviders(<AlertConsole onSelect={() => {}} />);

    const row = await waitFor(() => screen.getByText("WS-01").closest("tr") as HTMLElement);
    // 09:05Z is 04:05 in Bogota. The old renderer printed 09:05 here. Relative
    // in the row; the instant is on hover.
    expect(within(row).getByTitle("01 Mar 2026, 04:05")).toBeInTheDocument();
    expect(within(row).queryByTitle(/09:05/)).toBeNull();
  });

  it("renders a mute expiry in the operator's zone, so the silence ends when it says", async () => {
    // A mute is the one instant on this row that an operator plans around —
    // "it comes back at 4" — so five hours of error here is five hours of an
    // alarm everybody believes is armed.
    stubApi({
      me: vi.fn().mockResolvedValue(me(BOGOTA)),
      listHealthAlerts: vi.fn().mockResolvedValue([
        alert({ state: "muted", muted_until: "2026-03-02T21:00:00.000Z" }),
      ]),
    });
    renderWithProviders(<AlertConsole onSelect={() => {}} />);

    // Relative in the row, the instant on hover.
    expect(await screen.findByTitle("02 Mar 2026, 16:00")).toBeInTheDocument();
  });

  it("gives the alert detail one format for every instant on it", async () => {
    stubApi({
      me: vi.fn().mockResolvedValue(me(BOGOTA)),
      getHealthAlert: vi.fn().mockResolvedValue({
        ...alert({
          opened_at: "2026-03-01T09:00:00.000Z",
          last_seen_at: "2026-03-01T09:05:00.000Z",
          escalated_at: "2026-03-01T10:00:00.000Z",
          acknowledged_at: "2026-03-01T11:00:00.000Z",
          state: "muted",
          muted_until: "2026-03-02T21:00:00.000Z",
        }),
        profile_name: "Fleet baseline",
        ticket_number: "T-000123",
        previous_alert_ticket_number: null,
        previous_alert_opened_at: null,
        monitoring_deep_link: null,
        actions: [],
      }),
    });
    renderWithProviders(<AlertDetail alertId="a1" canManageAlerts canRemediate />);

    for (const label of ["Opened", "Last seen", "Escalated", "Muted until", "Acknowledged"]) {
      const value = (await screen.findByText(label)).nextElementSibling as HTMLElement;
      // ONE regex over all five: the audit's finding was that the same page
      // rendered two formats, which an assertion per field would not catch.
      expect(value.textContent).toMatch(PRODUCT_FORMAT);
    }
    // And the zone is the reader's: 09:00Z opened at 04:00 in Bogota.
    expect(screen.getByText("01 Mar 2026, 04:00")).toBeInTheDocument();
  });

  it("says there is no date rather than rendering Invalid Date or null", async () => {
    // `escalated_at`, `muted_until` and `acknowledged_at` are all nullable and
    // all three are null on a freshly-opened alert.
    stubApi({
      me: vi.fn().mockResolvedValue(me(BOGOTA)),
      getHealthAlert: vi.fn().mockResolvedValue({
        ...alert(),
        profile_name: "Fleet baseline",
        ticket_number: null,
        previous_alert_ticket_number: null,
        previous_alert_opened_at: null,
        monitoring_deep_link: null,
        actions: [],
      }),
    });
    renderWithProviders(<AlertDetail alertId="a1" canManageAlerts canRemediate />);

    for (const label of ["Escalated", "Muted until", "Acknowledged"]) {
      const value = (await screen.findByText(label)).nextElementSibling as HTMLElement;
      expect(value).toHaveTextContent("—");
      expect(value).not.toHaveTextContent(/Invalid Date|null|undefined|NaN/);
    }
  });

  it("renders a device's last contact and last report in the operator's zone", async () => {
    stubApi({
      me: vi.fn().mockResolvedValue(me(BOGOTA)),
      getDeviceHealth: vi.fn().mockResolvedValue(
        deviceHealth({
          agent_online: false,
          agent_last_seen_at: "2026-03-01T02:00:00.000Z",
          checks: [
            {
              check_id: "c1",
              check_type: "cpu.busy_pct",
              selector: "",
              comparator: "gt",
              threshold_display: "95",
              state: "ok",
              last_value: "12",
              reported_at: "2026-03-01T09:05:00.000Z",
            },
          ] as DeviceHealthOut["checks"],
        }),
      ),
    });
    renderWithProviders(<DeviceHealthPanel deviceId="d1" canCheckNow={false} />);

    // 02:00Z on the 1st is 21:00 on 28 Feb in Bogota — a whole DAY out, which
    // is the failure mode the old UTC renderer hid best.
    expect(await screen.findByText(/28 Feb 2026, 21:00/)).toBeInTheDocument();
    expect(screen.getByText("01 Mar 2026, 04:05")).toBeInTheDocument();
  });

  it("renders a standing script exemption's grant time in the operator's zone", async () => {
    // §2.10's whole mitigation is that a standing exemption is VISIBLE. "When
    // was this granted" is half of that, and it was being answered in UTC.
    stubApi({
      me: vi.fn().mockResolvedValue(me(BOGOTA)),
      listApprovedActions: vi.fn().mockResolvedValue([
        approvedAction({
          job_type: "script.run",
          script_id: "sc1",
          script_name: "Restart Spooler",
          script_version: 4,
          script_exemption: {
            id: "ex1",
            script_id: "sc1",
            script_name: "Restart Spooler",
            script_version: 4,
            granted_by: "admin@example.com",
            granted_at: "2026-03-01T09:05:00.000Z",
            reason: "read the body, it only restarts a service",
          },
        }),
      ]),
    });
    renderWithProviders(<ApprovedActionList canActionAdmin />);

    expect(await screen.findByText("01 Mar 2026, 04:05")).toBeInTheDocument();
  });

  it("falls back to the browser zone when the profile carries no timezone", async () => {
    // `timezone` is nullable on /me and most operators never set it. The
    // fallback must be a real rendering, not "—" and not "Invalid Date".
    stubApi({
      me: vi.fn().mockResolvedValue(me(null)),
      listHealthAlerts: vi
        .fn()
        .mockResolvedValue([alert({ last_seen_at: "2026-03-01T09:05:00.000Z" })]),
    });
    renderWithProviders(<AlertConsole onSelect={() => {}} />);

    const row = await waitFor(() => screen.getByText("WS-01").closest("tr") as HTMLElement);
    const seen = within(row).getByTitle(PRODUCT_FORMAT);
    expect(seen).toBeInTheDocument();
    expect(seen.getAttribute("title")).not.toMatch(/Invalid Date|—/);
  });
});

// --------------------------------------------------------------------------
// UX remediation cycle 4 — no failure reaches an operator as silence.
//
// TWO DEFECTS, ONE SYMPTOM. `healthState#describeError` was `error.message`,
// and `ApiError` is `new ApiError(status, await response.text())`, so a
// bodyless refusal — what a proxy 503 in front of the control plane is —
// carried `""` and rendered an EMPTY element. Separately, four panels wrote
// `query.data ?? []`, which turns a failed fetch into an empty list and then
// renders that list's empty-state sentence: "No alerts match this filter",
// "No approved actions", "No health profiles". Those sentences are CLAIMS
// ABOUT THE FLEET, made on no evidence.
//
// Every test below asserts `role="alert"`, not merely the presence of text: an
// operator who has just pressed a destructive control is not reading the
// corner of the page.
// --------------------------------------------------------------------------

describe("failures reach the operator — loads", () => {
  it("does not report an empty alert list when the alert list failed to load", async () => {
    stubApi({ listHealthAlerts: vi.fn().mockRejectedValue(new ApiError(500, "")) });
    renderWithProviders(<AlertConsole onSelect={() => {}} />);

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent(/could not be loaded/);
    expect(failure).toHaveTextContent(/Nothing was changed/);
    // THE POINT: the console must not say the fleet is quiet.
    expect(screen.queryByText(/No alerts match this filter/)).toBeNull();
    expect(screen.queryByText(/0 open of 0 shown/)).toBeNull();
  });

  it("prints no reporting figures at all when the alerts behind them did not load", async () => {
    // The zeroes are the danger: "Alerts in view 0 / Still open 0" reads as a
    // finding about a healthy fleet and is an artefact of a dead request.
    stubApi({ listHealthAlerts: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderWithProviders(<HealthReportPanel />);

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent(/could not be computed/);
    expect(screen.queryByText("Alerts in view")).toBeNull();
    expect(screen.queryByText("Still open")).toBeNull();
  });

  it("does not claim nothing can run automatically when the registry failed to load", async () => {
    stubApi({ listApprovedActions: vi.fn().mockRejectedValue(new ApiError(500, "")) });
    renderWithProviders(<ApprovedActionList canActionAdmin />);

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent(/could not be loaded/);
    expect(screen.queryByText(/No fixes can be dispatched/)).toBeNull();
  });

  it("does not claim nothing is being checked when the profile list failed to load", async () => {
    stubApi({
      listHealthProfiles: vi.fn().mockRejectedValue(new ApiError(500, "")),
      listHealthAssignments: vi.fn().mockResolvedValue([]),
    });
    renderWithProviders(<ProfileList canAdmin onSelect={() => {}} />);

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent(/could not be loaded/);
    expect(screen.queryByText(/Nothing in this organisation is being checked/)).toBeNull();
  });

  it("does not call an assigned profile unassigned when only the assignment list failed", async () => {
    // TWO QUERIES, FAILING SEPARATELY. `describeAssignments(profile, [])`
    // returns "unassigned" — over a profile whose own `assignment_count` is 1.
    // The row contradicted itself, and the half an operator acts on ("nothing
    // is applying this") was the false half.
    stubApi({
      listHealthProfiles: vi.fn().mockResolvedValue([PROFILE]),
      listHealthAssignments: vi.fn().mockRejectedValue(new ApiError(500, "")),
    });
    renderWithProviders(<ProfileList canAdmin onSelect={() => {}} />);

    // THE SAME TWO CLAIMS, over the badge and the line beneath it: the count
    // the profile itself reports, and the admission that the breakdown is
    // missing. (The sentence moved out of the badge so the row's actions fit
    // inside the panel at 1440px; nothing about it was dropped.)
    expect(await screen.findByText("1 assignment(s)")).toBeInTheDocument();
    expect(screen.getByText("breakdown unavailable")).toBeInTheDocument();
    expect(screen.queryByText("unassigned")).toBeNull();
    // Exact, not a regex: the panel's own subtitle explains what an
    // unassigned profile does, and matching that sentence would assert
    // nothing about the row.
    expect(screen.queryByText("applies to nothing")).toBeNull();
  });

  it("stops pretending to load when the settings request has already failed", async () => {
    // `!data` caught the failure and rendered "Loading…", indefinitely and
    // indistinguishably from a slow server.
    stubApi({ getHealthSettings: vi.fn().mockRejectedValue(new ApiError(500, "")) });
    renderWithProviders(<HealthSettingsPanel canAdmin />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be loaded/);
    expect(screen.queryByText("Loading…")).toBeNull();
  });

  it("reports a transport failure on the alert detail instead of loading for ever", async () => {
    // NOT an `ApiError`: a dropped connection rejects with a TypeError, which
    // fell past the `instanceof ApiError` branch into `!alert.data`.
    stubApi({ getHealthAlert: vi.fn().mockRejectedValue(new TypeError("Failed to fetch")) });
    renderWithProviders(<AlertDetail alertId="a1" canManageAlerts canRemediate />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to fetch");
    expect(screen.queryByText("Loading…")).toBeNull();
  });
});

describe("failures reach the operator — writes", () => {
  it("names the control when a bodyless 503 refuses Close manually", async () => {
    const user = (await import("@testing-library/user-event")).default;
    const closeHealthAlert = vi.fn().mockRejectedValue(new ApiError(503, ""));
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue({
        ...alert(),
        profile_name: "Fleet baseline",
        ticket_number: null,
        previous_alert_ticket_number: null,
        previous_alert_opened_at: null,
        monitoring_deep_link: null,
        actions: [],
      }),
      closeHealthAlert,
    });
    renderWithProviders(<AlertDetail alertId="a1" canManageAlerts canRemediate />);

    await user.click(await screen.findByRole("button", { name: /Close manually/ }));
    await user.click(await screen.findByRole("button", { name: "Close alert" }));

    const failure = await screen.findByRole("alert");
    // Three controls sit above this line; an unlabelled sentence names none.
    expect(failure).toHaveTextContent("Close manually failed");
    expect(failure).toHaveTextContent(/unavailable right now/);
    expect(failure).toHaveTextContent(/Nothing was changed/);
  });

  it("tells the operator when a bodyless 503 refuses Sample now", async () => {
    const user = (await import("@testing-library/user-event")).default;
    const checkDeviceHealthNow = vi.fn().mockRejectedValue(new ApiError(503, ""));
    stubApi({
      getDeviceHealth: vi.fn().mockResolvedValue(deviceHealth()),
      checkDeviceHealthNow,
    });
    renderWithProviders(<DeviceHealthPanel deviceId="d1" canCheckNow />);

    await user.click(await screen.findByRole("button", { name: "Sample now" }));

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("Sample now failed");
    expect(failure).toHaveTextContent(/unavailable right now/);
  });

  it("tells the operator when a bodyless 503 refuses Revoke on a standing exemption", async () => {
    // A governance change: revoking re-derives `destructive` and can demote an
    // autonomous action. A refusal that renders as an empty paragraph leaves
    // the operator believing an exemption is gone when it is still standing.
    const user = (await import("@testing-library/user-event")).default;
    const revokeApprovedActionScriptExemption = vi.fn().mockRejectedValue(new ApiError(503, ""));
    stubApi({
      listApprovedActions: vi.fn().mockResolvedValue([
        approvedAction({
          job_type: "script.run",
          script_id: "sc1",
          script_name: "Restart Spooler",
          script_version: 4,
          script_exemption: {
            id: "ex1",
            script_id: "sc1",
            script_name: "Restart Spooler",
            script_version: 4,
            granted_by: "admin@example.com",
            granted_at: "2026-03-01T09:05:00.000Z",
            reason: "safe",
          },
        }),
      ]),
      revokeApprovedActionScriptExemption,
    });
    renderWithProviders(<ApprovedActionList canActionAdmin />);

    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    // Revoking asks first (cycle 8): it re-derives `destructive` and can demote
    // an autonomous action, so the refusal is on the far side of the dialog.
    await user.click(await screen.findByRole("button", { name: "Revoke exemption" }));

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("Revoke exemption failed");
    expect(failure).toHaveTextContent(/unavailable right now/);
  });

  it("tells the operator when a bodyless 503 refuses a check-set save", async () => {
    // Saving RETIRES the removed checks and clears their open alerts. A silent
    // refusal leaves the operator believing a destructive edit landed.
    const user = (await import("@testing-library/user-event")).default;
    const replaceHealthProfileChecks = vi.fn().mockRejectedValue(new ApiError(503, ""));
    stubApi({
      getHealthProfileChecks: vi
        .fn()
        .mockResolvedValue({ profile_id: "p1", policy_version: 3, tick_interval_s: 300, checks: [checkOut()] }),
      listHealthCheckTypes: vi.fn().mockResolvedValue([]),
      replaceHealthProfileChecks,
    });
    renderWithProviders(<ProfileEditor profileId="p1" canAdmin />);

    await user.click(await screen.findByRole("button", { name: "Remove" }));
    await user.click(screen.getByRole("button", { name: "Save check set" }));

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("Save check set failed");
    expect(failure).toHaveTextContent(/unavailable right now/);
  });

  it("translates a Pydantic 422 body instead of printing it", async () => {
    // The audit found the raw validation payload rendered into the UI in three
    // modules. The operator gets the field and the message; `input` is never
    // read, because it echoes what was submitted and would put a secret on the
    // screen (invariant 3, through a third door).
    const user = (await import("@testing-library/user-event")).default;
    const replaceHealthProfileChecks = vi.fn().mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: [
            {
              type: "too_short",
              loc: ["body", "checks"],
              msg: "List should have at least 1 item",
              input: [],
            },
          ],
        }),
      ),
    );
    stubApi({
      getHealthProfileChecks: vi.fn().mockResolvedValue({
        profile_id: "p1",
        policy_version: 3,
        tick_interval_s: 300,
        checks: [checkOut()],
      }),
      listHealthCheckTypes: vi.fn().mockResolvedValue([]),
      replaceHealthProfileChecks,
    });
    renderWithProviders(<ProfileEditor profileId="p1" canAdmin />);

    await user.click(await screen.findByRole("button", { name: "Remove" }));
    await user.click(screen.getByRole("button", { name: "Save check set" }));

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("Checks: List should have at least 1 item");
    expect(failure).not.toHaveTextContent(/too_short|"loc"|\[\]/);
  });
});
