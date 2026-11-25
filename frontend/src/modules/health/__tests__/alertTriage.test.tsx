/**
 * Triage on the Health page: which alert an operator sees first, where the
 * alert takes them, and whether the detail speaks their language.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry, type FixtureMap } from "../../../fixtures/client";
import type { ApiClient } from "../../../lib/api";
import { recordingNarrowing } from "../../../test/narrowing";
import { renderPage } from "../../../test/renderPage";
import { AlertConsole, sortAlerts } from "../AlertConsole";
import { AlertDetail } from "../AlertDetail";
import { HealthPage } from "../HealthPage";
import type { AlertActionOut, AlertDetailOut, AlertOut, AlertSummary } from "../types";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

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

function detail(overrides: Partial<AlertDetailOut> = {}): AlertDetailOut {
  return {
    ...alert(),
    profile_name: "Fleet baseline",
    ticket_number: null,
    previous_alert_ticket_number: null,
    previous_alert_opened_at: null,
    monitoring_deep_link: null,
    actions: [],
    ...overrides,
  };
}

function action(overrides: Partial<AlertActionOut> = {}): AlertActionOut {
  return {
    id: "ar1",
    alert_id: "a1",
    kind: "remediate",
    state: "blocked_flapping",
    approved_action_id: "act1",
    approved_action_key: "restart-spooler",
    approved_action_name: "Restart the spooler",
    script_id: "s1",
    script_name: "Restart Spooler",
    script_version: 4,
    autonomy: "autonomous",
    risk_class: "low",
    destructive: false,
    job_id: null,
    ticket_id: null,
    attempts: 0,
    next_attempt_at: null,
    approved_by: null,
    approved_at: null,
    expires_at: null,
    last_error:
      "actions suppressed: this key opened 4 times inside the flap window; suppressed until 2026-03-01T10:00:00Z",
    created_at: "2026-03-01T09:00:00.000Z",
    updated_at: "2026-03-01T09:00:00.000Z",
    ...overrides,
  };
}

/** Every call the Health page can make answers empty unless overridden. */
function stubApi(overrides: Record<string, unknown>): void {
  const base: Record<string, unknown> = {
    me: vi.fn().mockResolvedValue({
      user_id: "u1",
      org_id: "o1",
      email: "t@example.com",
      display_name: "Tech",
      permissions: ["health.read", "health.alert.manage"],
      timezone: null,
    }),
    listHealthAlerts: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  const api = new Proxy(base, {
    get: (target, key: string) => target[key] ?? vi.fn().mockReturnValue(new Promise(() => {})),
  });
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
}

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname + location.search}</p>;
}

function renderAt(node: React.ReactNode, at = "/health") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route
            path="/health"
            element={
              <>
                {node}
                <Where />
              </>
            }
          />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("alert order", () => {
  it("puts critical before warning before info, then newest first", () => {
    const rows = sortAlerts(
      [
        alert({ id: "info-new", severity: "info", opened_at: "2026-03-05T00:00:00Z" }),
        alert({ id: "warn-old", severity: "warning", opened_at: "2026-03-01T00:00:00Z" }),
        alert({ id: "crit-old", severity: "critical", opened_at: "2026-03-01T00:00:00Z" }),
        alert({ id: "warn-new", severity: "warning", opened_at: "2026-03-04T00:00:00Z" }),
        alert({ id: "crit-new", severity: "critical", opened_at: "2026-03-03T00:00:00Z" }),
      ],
      false,
    );
    expect(rows.map((row) => row.id)).toEqual([
      "crit-new",
      "crit-old",
      "warn-new",
      "warn-old",
      "info-new",
    ]);
  });

  it("keeps closed alerts below open ones, whatever their severity", () => {
    const rows = sortAlerts(
      [
        alert({ id: "cleared-crit", severity: "critical", state: "cleared" }),
        alert({ id: "open-info", severity: "info" }),
      ],
      false,
    );
    expect(rows.map((row) => row.id)).toEqual(["open-info", "cleared-crit"]);
  });

  it("still sorts repeating alerts first when asked", () => {
    const rows = sortAlerts(
      [
        alert({ id: "crit", severity: "critical" }),
        alert({ id: "repeat", severity: "info", flapping: true, flap_count: 3 }),
      ],
      true,
    );
    expect(rows[0]?.id).toBe("repeat");
  });
});

describe("the alert list", () => {
  it("links each hostname to its device", async () => {
    stubApi({ listHealthAlerts: vi.fn().mockResolvedValue([alert()]) });
    renderAt(<AlertConsole onSelect={() => {}} />);
    const link = await screen.findByRole("link", { name: "WS-01" });
    expect(link).toHaveAttribute("href", "/devices/d1");
  });
});

describe("the alert detail", () => {
  it("links to the device the alert is about", async () => {
    stubApi({ getHealthAlert: vi.fn().mockResolvedValue(detail()) });
    renderAt(<AlertDetail alertId="a1" canManageAlerts canRemediate />);
    const link = await screen.findByRole("link", { name: /WS-01/ });
    expect(link).toHaveAttribute("href", "/devices/d1");
  });

  it("names the check in words and keeps the key only as secondary text", async () => {
    stubApi({
      getHealthAlert: vi
        .fn()
        .mockResolvedValue(detail({ check_type: "service.running", selector: "Spooler" })),
    });
    renderAt(<AlertDetail alertId="a1" canManageAlerts canRemediate />);
    const check = (await screen.findByText("Check")).closest("div") as HTMLElement;
    expect(within(check).getByText("Service running")).toBeInTheDocument();
    expect(within(check).getByText("service.running")).toHaveClass("mono");
  });

  it("explains a paused action in plain words, not the engine's", async () => {
    stubApi({
      getHealthAlert: vi
        .fn()
        .mockResolvedValue(detail({ flapping: true, flap_count: 4, actions: [action()] })),
    });
    renderAt(<AlertDetail alertId="a1" canManageAlerts canRemediate />);
    const body = await screen.findByText(/come back 4 times/i);
    expect(body).toHaveTextContent(/automatic actions are paused/);
    // The engine's sentence is kept only behind a disclosure.
    const panel = screen.getByRole("heading", { name: "Run the fix" }).closest(".panel") as HTMLElement;
    const shown = panel.cloneNode(true) as HTMLElement;
    shown.querySelectorAll("details").forEach((node) => node.remove());
    expect(shown.textContent).not.toMatch(/suppress|flap/i);
    expect(within(panel).getByText(/actions suppressed/)).toHaveClass("mono");
  });

  it("titles each action in words, not by its kind key", async () => {
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue(
        detail({
          actions: [
            action({ id: "n1", kind: "notify", state: "done", last_error: null }),
            action({ id: "r1", kind: "remediate", state: "done", last_error: null }),
          ],
        }),
      ),
    });
    renderAt(<AlertDetail alertId="a1" canManageAlerts canRemediate />);
    expect(await screen.findByRole("heading", { name: "Notify people" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Run the fix" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "remediate" })).toBeNull();
  });
});

describe("opening an alert on the Health page", () => {
  it("opens the detail in a drawer and closes it again", async () => {
    const user = userEvent.setup();
    stubApi({
      listHealthAlerts: vi.fn().mockResolvedValue([alert()]),
      getHealthAlert: vi.fn().mockResolvedValue(detail()),
    });
    renderAt(<HealthPage />);

    const row = (await screen.findByRole("link", { name: "WS-01" })).closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Open" }));

    const drawer = await screen.findByRole("dialog");
    expect(await within(drawer).findByText(/CPU busy on WS-01/)).toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent("alert=a1");

    await user.click(within(drawer).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByTestId("where")).not.toHaveTextContent("alert=");
  });

  it("opens the alert a link names, so the dashboard can point at one", async () => {
    stubApi({
      listHealthAlerts: vi.fn().mockResolvedValue([alert()]),
      getHealthAlert: vi.fn().mockResolvedValue(detail()),
    });
    renderAt(<HealthPage />, "/health?alert=a1");
    const drawer = await screen.findByRole("dialog");
    expect(await within(drawer).findByText(/CPU busy on WS-01/)).toBeInTheDocument();
  });
});

// DEEP LINKS INTO THE PAGE (docs/design/cross-module-guidance.md §0.2). Other
// pages point at health profiles, approved actions and health settings; each
// is a panel on this page, so each needs an anchor that exists and an arrival
// that actually scrolls -- React Router pushes a fragment and stops.
describe("Health page anchors", () => {
  // Redesign decision D21 (open question 3, resolved 2026-11-24): each panel lives in its view now; an old anchor opens that view.
  it("gives profiles, approved actions and health settings an anchor each, around the right panel", async () => {
    const at = (id: string) => document.getElementById(id);
    for (const [view, id, heading] of [
      ["profiles", "profiles", "Health profiles"],
      ["actions", "approved-actions", "Approved actions"],
      ["settings", "health-settings", "Health settings"],
    ] as const) {
      stubApi({});
      const { unmount } = renderAt(<HealthPage />, `/health?view=${view}`);
      await screen.findByRole("heading", { name: "Health & alerts" });
      await waitFor(() => expect(at(id)).not.toBeNull());
      expect(within(at(id)!).getAllByRole("heading", { name: heading }).length).toBeGreaterThan(0);
      unmount();
    }
  });

  // Redesign decision D21 (open question 3, resolved 2026-11-24): each panel lives in its view now; an old anchor opens that view.
  it("scrolls to the panel a link names on arrival", async () => {
    const scrolled: string[] = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this.id);
    };
    try {
      stubApi({});
      renderAt(<HealthPage />, "/health#approved-actions");
      expect(await screen.findByRole("tab", { name: "Approved actions" })).toHaveAttribute("aria-selected", "true");
      await waitFor(() => expect(scrolled).toContain("approved-actions"));
      expect(scrolled).not.toContain("profiles");
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  it("opens each old anchor's view, keeping the fragment, and leaves the three links that write them as they are", async () => {
    for (const [hash, view, tab] of [
      ["#profiles", "profiles", "Profiles"],
      ["#approved-actions", "actions", "Approved actions"],
      ["#health-settings", "settings", "Settings"],
    ] as const) {
      stubApi({});
      const { router, unmount } = renderPage(`/health${hash}`, <HealthPage />, "/health");
      expect(await screen.findByRole("tab", { name: tab })).toHaveAttribute("aria-selected", "true");
      expect(router.state.location.search).toBe(`?view=${view}`);
      expect(router.state.location.hash).toBe(hash);
      unmount();
    }
  });
});

// THE VIEWS (redesign decision D21): the queue is the default and writes
// nothing; each configuration panel opens from its own `?view=`.
describe("the Health page's views", () => {
  it("opens each configuration view from the URL", async () => {
    stubApi({});
    const { router } = renderPage("/health?view=settings", <HealthPage />, "/health");
    expect(await screen.findByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "Health settings" })).toBeInTheDocument();
    // The queue is not drawn under another view.
    expect(screen.queryByRole("heading", { name: "Alerts", level: 2 })).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: "Profiles" }));
    expect(router.state.location.search).toBe("?view=profiles");
    expect(await screen.findByRole("heading", { name: "Health profiles" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Alerts" }));
    expect(router.state.location.search).toBe("");
    await act(() => router.navigate("/health?view=nope"));
    expect(await screen.findByRole("tab", { name: "Alerts" })).toHaveAttribute("aria-selected", "true");
  });
});

/** An answer of `/health/alerts/summary` with `critical` critical alerts open. */
function summary(critical: number): AlertSummary {
  return {
    open: critical,
    by_state: { active: critical, acknowledged: 0, muted: 0 },
    by_severity: { critical, warning: 0, info: 0 },
    worst_severity: critical > 0 ? "critical" : null,
  };
}

/** The fixture session (two clients and Unassigned, Andes among them), with the alert summary given. */
function withSummary(critical: number, extra: FixtureMap = {}): FixtureMap {
  return { ...fixtureRegistry(), getHealthAlertSummary: () => summary(critical), ...extra };
}

describe("the alert queue on the list kit (redesign plan, Receta L)", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/health?severity=warning", <HealthPage />, "/health");
    const severity = await screen.findByRole("group", { name: "Severity" });
    expect(within(severity).getByRole("button", { name: /Warning/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(severity).getByRole("button", { name: /Critical/ }));
    expect(router.state.location.search).toContain("severity=critical");
    await userEvent.click(within(screen.getByRole("group", { name: "State" })).getByRole("button", { name: /Muted/ }));
    expect(router.state.location.search).toContain("state=muted");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listHealthAlerts: () => [] }));
    renderPage("/health?state=muted", <HealthPage />, "/health");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("says no computer reports yet when there are none", async () => {
    const none = { items: [], total: 0, state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 }, fleet: { total: 0, managed: 0, online: 0, cert_attention: 0 } };
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listHealthAlerts: () => [], listDevices: () => none }));
    renderPage("/health", <HealthPage />, "/health");
    expect(await screen.findByText("No computer reports its health yet.")).toBeInTheDocument();
    // Its action leads to adding one, not to "adjusting the filters" (Atera's flaw).
    const add = screen
      .getAllByRole("link", { name: /Add a computer/ })
      .find((link) => link.closest(".empty") !== null);
    expect(add).toHaveAttribute("href", "/devices/onboard");
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
  });

  it("says no alert has opened yet when computers report and nothing has", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listHealthAlerts: () => [] }));
    renderPage("/health", <HealthPage />, "/health");
    expect(await screen.findByText("No alert has opened yet.")).toBeInTheDocument();
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // What the alerts view legitimately reads on the console's own API: the
    // readiness panel above the queue (a computer runs the agent, a profile is
    // assigned, a rule sends alerts) asks about the organisation's setup, which
    // the top bar's choice decides and the chip does not (decision D20: the
    // chip narrows the list). The queue, its counts and its report are not here.
    const narrowing = recordingNarrowing(fixtureClient(), {
      console: ["listDevices", "listHealthAssignments", "listNotificationRules"],
    });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/health?client=${ANDES}&severity=critical`, <HealthPage />, "/health");
    expect(await screen.findByRole("table", { name: "Alerts" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listHealthAlerts).toHaveBeenCalledWith({ state: "", severity: "critical" });
    // The tiles and the report read the unfiltered list, and the counts the summary, of the same client.
    expect(narrowing.client(ANDES).listHealthAlerts).toHaveBeenCalledWith({});
    expect(narrowing.client(ANDES).getHealthAlertSummary).toHaveBeenCalled();
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("counts the chips for the Client chip's client, not every client", async () => {
    const forAndes = fixtureClient(withSummary(1));
    const everyone = fixtureClient(withSummary(7));
    const api = new Proxy(everyone, {
      get: (target, prop) => (prop === "forClient" ? () => forAndes : Reflect.get(target, prop)),
    });
    mockUseApi.mockReturnValue(api);
    const chipCount = () =>
      within(within(screen.getByRole("group", { name: "Severity" })).getByRole("button", { name: /Critical/ })).queryByText(
        /^\d+$/,
      )?.textContent;

    const narrowed = renderPage(`/health?client=${ANDES}`, <HealthPage />, "/health");
    await waitFor(() => expect(chipCount()).toBe("1"));
    // The critical tile counts the same client, and opens its rows under the same chip.
    const tile = screen.getAllByRole("link").find((link) => link.getAttribute("href")?.startsWith("/health?severity=critical"));
    expect(tile).toHaveAttribute("href", `/health?severity=critical&client=${ANDES}`);
    expect(tile).toHaveTextContent("1");
    expect(tile).not.toHaveTextContent("7");
    narrowed.unmount();

    renderPage("/health", <HealthPage />, "/health");
    await waitFor(() => expect(chipCount()).toBe("7"));
  });
});
