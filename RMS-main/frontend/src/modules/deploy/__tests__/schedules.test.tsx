import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { intervalLabel, outcomeLabel, SchedulesPanel, whenLabel } from "../SchedulesPanel";
import type { ScriptListItem, ScriptPolicy } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWith(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const SCRIPTS: ScriptListItem[] = [
  {
    id: "s1",
    name: "Clear temp",
    os_family: "windows",
    interpreter: "powershell",
    version: 2,
    created_by: null,
    created_at: "2026-09-01T00:00:00Z",
  },
];

const POLICY: ScriptPolicy = {
  id: "p1",
  name: "Nightly temp clean",
  script_id: "s1",
  script_name: "Clear temp",
  device_group_id: "g1",
  device_group_name: "Laptops",
  interval_hours: 24,
  in_maintenance_window: true,
  alert_on_failure: true,
  keep_runs: 30,
  enabled: true,
  devices_failing: 2,
  devices_succeeding: 10,
  last_run_at: "2026-09-23T01:00:00Z",
};

function api(overrides: Record<string, unknown> = {}) {
  return {
    me: vi.fn().mockResolvedValue({ user_id: "u", org_id: "o", email: "e", display_name: "d", permissions: [] }),
    getCurrentOrg: vi
      .fn()
      .mockResolvedValue({ id: "o", name: "o", slug: "o", status: "active", settings: { timezone: "Europe/London" } }),
    listScriptPolicies: vi.fn().mockResolvedValue([POLICY]),
    listDeviceGroups: vi.fn().mockResolvedValue({ items: [{ id: "g1", name: "Laptops" }], total: 1 }),
    createScriptPolicy: vi.fn().mockResolvedValue(POLICY),
    listScriptPolicyRuns: vi.fn().mockResolvedValue([
      {
        id: "r1",
        device_id: "d1",
        hostname: "LAPTOP-7",
        script_version: 2,
        state: "failed",
        exit_code: 3,
        output: "Access denied",
        created_at: "2026-09-23T01:00:00Z",
        finished_at: "2026-09-23T01:00:05Z",
      },
    ]),
    ...overrides,
  };
}

describe("schedule words", () => {
  it("never shows a schedule expression or raw hours", () => {
    expect(intervalLabel(1)).toBe("Every hour");
    expect(intervalLabel(4)).toBe("Every 4 hours");
    expect(intervalLabel(24)).toBe("Every day");
    expect(intervalLabel(168)).toBe("Every week");
    expect(whenLabel({ interval_hours: 24, in_maintenance_window: true })).toBe(
      "Every day, during each machine's maintenance window",
    );
    expect(outcomeLabel({ devices_failing: 1, devices_succeeding: 0 }).label).toBe(
      "Failing on 1 machine",
    );
  });
});

describe("SchedulesPanel", () => {
  it("lists schedules in words, with times in the org's zone", async () => {
    mockUseApi.mockReturnValue(api());
    renderWith(<SchedulesPanel scripts={SCRIPTS} canRead canSchedule={false} />);
    expect(await screen.findByText("Nightly temp clean")).toBeTruthy();
    expect(screen.getByText("Clear temp on Laptops")).toBeTruthy();
    expect(screen.getByText("Failing on 2 machines")).toBeTruthy();
    expect(screen.getByText("Alerts when it fails")).toBeTruthy();
    expect(await screen.findByText("23 Sept 2026, 02:00")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "New schedule" })).toBeNull();
  });

  it("creates a schedule from plain choices", async () => {
    const user = userEvent.setup();
    const client = api();
    mockUseApi.mockReturnValue(client);
    renderWith(<SchedulesPanel scripts={SCRIPTS} canRead canSchedule />);
    await user.click(await screen.findByRole("button", { name: "New schedule" }));
    await user.type(screen.getByLabelText("Name"), "Weekly clean");
    await user.selectOptions(screen.getByLabelText("Script"), "s1");
    await screen.findByRole("option", { name: "Laptops" });
    await user.selectOptions(screen.getByLabelText(/Device group/), "g1");
    await user.selectOptions(screen.getByLabelText("How often"), "168");
    await user.click(screen.getByRole("button", { name: "Create schedule" }));
    await waitFor(() => expect(client.createScriptPolicy).toHaveBeenCalled());
    expect(client.createScriptPolicy.mock.calls[0]?.[0]).toMatchObject({
      name: "Weekly clean",
      script_id: "s1",
      device_group_id: "g1",
      interval_hours: 168,
      in_maintenance_window: true,
      alert_on_failure: false,
    });
  });

  it("shows each run's output", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api());
    renderWith(<SchedulesPanel scripts={SCRIPTS} canRead canSchedule={false} />);
    await user.click(await screen.findByRole("button", { name: "Runs" }));
    expect(await screen.findByText("Access denied")).toBeTruthy();
    expect(screen.getByText("Failed (exit 3)")).toBeTruthy();
  });

  it("renders nothing for an operator who cannot read deployments", () => {
    mockUseApi.mockReturnValue(api());
    const { container } = renderWith(
      <SchedulesPanel scripts={SCRIPTS} canRead={false} canSchedule={false} />,
    );
    expect(container.textContent).toBe("");
  });
});
