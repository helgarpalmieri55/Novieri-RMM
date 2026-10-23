/**
 * Triage on the Health page: which alert an operator sees first, where the
 * alert takes them, and whether the detail speaks their language.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { AlertConsole, sortAlerts } from "../AlertConsole";
import { AlertDetail } from "../AlertDetail";
import { HealthPage } from "../HealthPage";
import type { AlertActionOut, AlertDetailOut, AlertOut } from "../types";

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
// is a panel on this one page, so each needs an anchor that exists and an
// arrival that actually scrolls -- React Router pushes a fragment and stops.
describe("Health page anchors", () => {
  it("gives profiles, approved actions and health settings an anchor each, around the right panel", async () => {
    stubApi({});
    renderAt(<HealthPage />);
    await screen.findByRole("heading", { name: "Health & alerts" });
    const at = (id: string) => document.getElementById(id);
    await waitFor(() => expect(at("profiles")).not.toBeNull());
    expect(within(at("profiles")!).getByRole("heading", { name: "Health profiles" })).toBeInTheDocument();
    expect(
      within(at("approved-actions")!).getAllByRole("heading", { name: "Approved actions" }).length,
    ).toBeGreaterThan(0);
    expect(within(at("health-settings")!).getByRole("heading", { name: "Health settings" })).toBeInTheDocument();
  });

  it("scrolls to the panel a link names on arrival", async () => {
    const scrolled: string[] = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this.id);
    };
    try {
      stubApi({});
      renderAt(<HealthPage />, "/health#approved-actions");
      await waitFor(() => expect(scrolled).toContain("approved-actions"));
      expect(scrolled).not.toContain("profiles");
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});
