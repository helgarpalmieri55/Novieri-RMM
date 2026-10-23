import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { CHECKED_REPORTS, ReportReadiness } from "../ReportReadiness";

// "Before these reports have something to show" (cross-module guidance,
// Reports rows 1-4): one row per report, each pointing at the module whose
// emptiness would make that report come back empty.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = ["devices.read", "patch.read", "backup.read", "sla.read"];

const FLEET = { total: 3, managed: 2, online: 1 };
const FLEET_REPORT = {
  compliant_pct_by_os_family: {},
  compliant_pct_by_severity: {},
  exposures: [],
  exposed_device_count: 0,
  critical_device_count: 0,
  last_scan_at: "2026-09-01T00:00:00Z",
  devices_scanned: 2,
  devices_total: 2,
};
const SLA_POLICY = {
  id: "s1",
  queue_id: null,
  priority: null,
  calendar_id: "c1",
  response_target_minutes: 60,
  resolution_target_minutes: 480,
  escalate_priority: false,
  escalate_to_queue_id: null,
  active: true,
  created_at: "2026-01-01T00:00:00Z",
};

function stubApi(overrides: Partial<ApiClient> & { permissions?: string[] } = {}): ApiClient {
  const { permissions = ALL, ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0, fleet: FLEET }),
    getPatchFleetReport: vi.fn().mockResolvedValue(FLEET_REPORT),
    listPatchApprovals: vi.fn().mockResolvedValue({ items: [], total: 4 }),
    listBackupPolicies: vi.fn().mockResolvedValue([{ id: "p1", enabled: true }]),
    listSlaPolicies: vi.fn().mockResolvedValue([SLA_POLICY]),
    ...rest,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

function renderPanel(keys: readonly string[] | undefined = CHECKED_REPORTS) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ReportReadiness keys={keys} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ReportReadiness", () => {
  it("renders nothing when every report has something to read", async () => {
    const api = stubApi();
    renderPanel();
    await waitFor(() => expect(api.listSlaPolicies).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("Before these reports have something to show")).toBeNull();
  });

  it("sends a fleet with no agent to add a computer", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue({ items: [], total: 0, fleet: { total: 0, managed: 0, online: 0 } }) });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Add a computer" })).toHaveAttribute("href", "/devices/onboard");
  });

  it("says a patch report with no approved update reads as compliant, and links to Patching", async () => {
    // Asking only for updates in force: a revoked approval clears nothing.
    const api = stubApi({ listPatchApprovals: vi.fn().mockResolvedValue({ items: [], total: 0 }) });
    renderPanel();
    expect(await screen.findByText(/read as fully compliant whatever is missing/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Scan and approve updates" })).toHaveAttribute("href", "/patch");
    expect(api.listPatchApprovals).toHaveBeenCalledWith({ revoked: false, limit: 1 });
  });

  it("says an unscanned fleet gives the patch report nothing to measure", async () => {
    stubApi({ getPatchFleetReport: vi.fn().mockResolvedValue({ ...FLEET_REPORT, devices_scanned: 0 }) });
    renderPanel();
    expect(await screen.findByText(/No computer has been scanned for updates/)).toBeInTheDocument();
  });

  it("links no enabled backup policy to backup policies", async () => {
    stubApi({ listBackupPolicies: vi.fn().mockResolvedValue([]) });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Set up backups" })).toHaveAttribute("href", "/backup/policies");
  });

  it("links no active service-level policy to Service levels", async () => {
    stubApi({ listSlaPolicies: vi.fn().mockResolvedValue([{ ...SLA_POLICY, active: false }]) });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Set response targets" })).toHaveAttribute("href", "/sla");
  });

  it("asks only about reports the catalogue publishes", async () => {
    stubApi({ listSlaPolicies: vi.fn().mockResolvedValue([]), listBackupPolicies: vi.fn().mockResolvedValue([]) });
    renderPanel(["backup-coverage"]);
    expect(await screen.findByRole("link", { name: "Set up backups" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Set response targets" })).toBeNull();
  });

  it("says a check this account cannot make cannot be checked, and never asks", async () => {
    const api = stubApi({ permissions: ["devices.read", "patch.read", "backup.read"] });
    renderPanel();
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(api.listSlaPolicies).not.toHaveBeenCalled();
    expect(screen.queryByRole("link", { name: "Set response targets" })).toBeNull();
  });
});
