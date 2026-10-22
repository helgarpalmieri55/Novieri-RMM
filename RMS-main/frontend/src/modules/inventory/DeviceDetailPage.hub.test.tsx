/**
 * The device page as the incident hub (review 2026-09-23, top-15 item 1):
 * a header that says whether the machine is all right, the actions a
 * technician takes on it, and an Overview as the default tab.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, DeviceInventory, Me } from "../../lib/api";
import { DeviceDetailPage } from "./DeviceDetailPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

const EVERYTHING = [
  "health.read",
  "patch.read",
  "patch.apply",
  "backup.read",
  "backup.run",
  "deploy.read",
  "deploy.script.run",
  "jobs.read",
  "jobs.dispatch",
  "ticket.read",
  "ticket.write",
  "remote.control",
];

const DEVICE = {
  id: "d1",
  hostname: "ws-alpha",
  os_family: "windows",
  agent_status: "online",
  last_seen_at: "2026-09-23T11:55:00Z",
  lifecycle: "in_service",
  source: "agent",
  provenance: "agent",
  cert_state: "expiring",
  cert_revoked: false,
  cert_not_after: "2026-09-26T12:00:00Z",
};

const INVENTORY: DeviceInventory = {
  summary: { hostname: "ws-alpha", os_family: "windows", os_version: "11 23H2" },
  hardware: [],
  software: [],
  facts: {},
  snapshot_taken_at: "2026-09-23T09:00:00Z",
};

const alert = (id: string, severity: string, check_type: string, opened_at: string) => ({
  id,
  device_id: "d1",
  device_hostname: "ws-alpha",
  check_type,
  selector: "",
  severity,
  state: "active",
  opened_at,
});

function renderHub(
  permissions: string[] = EVERYTHING,
  overrides: Partial<ApiClient> = {},
  entry = "/devices/d1",
) {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", permissions, timezone: "UTC" } as unknown as Me),
    getDevice: vi.fn().mockResolvedValue(DEVICE),
    getDeviceInventory: vi.fn().mockResolvedValue(INVENTORY),
    getDeviceHealth: vi.fn().mockResolvedValue({
      open_alerts: [
        alert("a1", "warning", "cpu.busy_pct", "2026-09-23T10:00:00Z"),
        alert("a2", "critical", "disk.free_pct", "2026-09-23T09:00:00Z"),
      ],
      checks: [],
    }),
    getDevicePatchStatus: vi.fn().mockResolvedValue({
      device_id: "d1",
      counts_by_state: { missing: 4, installed: 20 },
      counts_by_severity: {},
      pending_reboot: false,
      last_scan_at: "2026-09-23T06:00:00Z",
      restart: null,
    }),
    getDeviceBackup: vi.fn().mockResolvedValue({
      device_id: "d1",
      last_good_point: { backup_point_id: "p1", taken_at: "2026-09-21T12:00:00Z", size: 1, repository_id: "r1" },
      last_run_state: "succeeded",
      last_run_finished_at: "2026-09-21T12:00:00Z",
      verified_at: null,
      restore_tested_at: null,
      repository: { id: "r1", name: "Lisbon NAS" },
    }),
    listJobs: vi.fn().mockResolvedValue({
      items: [
        { id: "j1", type: "inventory.collect", status: "completed", issued_at: "2026-09-23T09:00:00Z", device_id: "d1" },
        { id: "j2", type: "inventory.collect", status: "completed", issued_at: "2026-09-23T09:00:00Z", device_id: "other" },
      ],
      total: 2,
    }),
    listTickets: vi.fn().mockResolvedValue({
      items: [
        { id: "t1", number: "INC-7", subject: "Slow at login", status: "open", device_id: "d1" },
        { id: "t2", number: "INC-3", subject: "Old one", status: "closed", device_id: "d1" },
      ],
      total: 2,
    }),
    getDeviceChanges: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/devices/:deviceId" element={<DeviceDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return api;
}

beforeEach(() => {
  mockUseApi.mockReset();
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-09-23T12:00:00Z"));
  return () => vi.useRealTimers();
});

describe("the header says whether the machine is all right", () => {
  it("counts the open alerts and links to them", async () => {
    renderHub();
    const status = await screen.findByRole("group", { name: "Device status" });
    const alerts = await within(status).findByText("2 open alerts");
    expect(alerts.closest("a")).toHaveAttribute("href", "/health?device=d1&state=open");
  });

  it("links to the device's own Health tab for an operator who cannot open the alert list", async () => {
    renderHub(EVERYTHING.filter((code) => code !== "health.read"));
    const status = await screen.findByRole("group", { name: "Device status" });
    expect((await within(status).findByText("2 open alerts")).closest("a")).toHaveAttribute(
      "href",
      "/devices/d1?tab=health",
    );
  });

  it("shows the certificate, updates and backup state", async () => {
    renderHub();
    const status = await screen.findByRole("group", { name: "Device status" });
    expect(within(status).getByText("Certificate expiring")).toBeInTheDocument();
    expect((await within(status).findByText("4 updates missing")).closest("a")).toHaveAttribute(
      "href",
      "/devices/d1?tab=patch",
    );
    expect(await within(status).findByText("2 days ago")).toBeInTheDocument();
  });

  it("leaves out what the operator cannot read, rather than showing it unknown", async () => {
    renderHub([]);
    const status = await screen.findByRole("group", { name: "Device status" });
    await within(status).findByText("2 open alerts");
    expect(within(status).queryByText(/updates/)).toBeNull();
    expect(within(status).queryByText(/Backed up|Never backed up/)).toBeNull();
  });

  it("shows the last contact as relative time, with the instant on hover", async () => {
    renderHub();
    const time = await screen.findByText("5 minutes ago", { selector: ".sub time" });
    expect(time).toHaveAttribute("title", "23 Sept 2026, 11:55");
  });
});

describe("the Overview is the default tab", () => {
  it("opens on Overview", async () => {
    renderHub();
    expect(await screen.findByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  });

  it("lists open alerts worst first, in words", async () => {
    renderHub();
    const panel = await screen.findByRole("tabpanel", { name: "Overview" });
    await within(panel).findByText("2 open");
    const links = within(panel)
      .getAllByRole("link")
      .filter((link) => link.getAttribute("href")?.startsWith("/health?alert="));
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/health?alert=a2",
      "/health?alert=a1",
    ]);
    expect(within(panel).queryByText("cpu.busy_pct")).toBeNull();
  });

  it("summarises updates, backup, this device's jobs and its open tickets", async () => {
    renderHub();
    const panel = await screen.findByRole("tabpanel", { name: "Overview" });
    expect(await within(panel).findByText("Lisbon NAS")).toBeInTheDocument();
    expect(await within(panel).findByRole("link", { name: "INC-7 · Slow at login" })).toHaveAttribute(
      "href",
      "/service-desk/tickets/t1",
    );
    expect(within(panel).queryByText(/INC-3/)).toBeNull();
    const jobs = await within(panel).findAllByRole("link", { name: "Collect inventory" });
    expect(jobs.map((link) => link.getAttribute("href"))).toEqual(["/jobs?job=j1"]);
  });

  it("asks for tickets by device, which the ticket route filters on", async () => {
    const api = renderHub();
    await waitFor(() => expect(api.listTickets).toHaveBeenCalledWith({ device_id: "d1", limit: 50 }));
  });

  it("uses a tab strip that scrolls rather than folding", async () => {
    renderHub();
    expect(await screen.findByRole("tablist")).toHaveClass("tabs", "scroll");
  });
});

describe("the actions", () => {
  it("offers the incident actions to an operator who may use them", async () => {
    renderHub();
    for (const name of ["Remote in", "Run a script", "Install updates", "Back up now", "Open ticket"]) {
      expect(await screen.findByRole("button", { name })).toBeEnabled();
    }
  });

  it("books a restart for the maintenance window, saying when in the org's zone and that a user is warned", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const window = { open_now: false, opens_at: "2026-09-26T01:00:00Z", user_warning_minutes: 480 };
    const requestDeviceRestart = vi.fn().mockResolvedValue({
      restart: { id: "r1", device_id: "d1", state: "pending", origin: "operator", scheduled_for: null, user_present: null, reason: null, created_at: "2026-09-23T12:00:00Z", closed_at: null },
      already_requested: false,
      window,
    });
    renderHub(EVERYTHING, {
      requestDeviceRestart,
      getCurrentOrg: vi.fn().mockResolvedValue({ settings: { timezone: "Europe/Lisbon" } }),
      getDevicePatchStatus: vi.fn().mockResolvedValue({
        device_id: "d1",
        counts_by_state: {},
        counts_by_severity: {},
        pending_reboot: false,
        last_scan_at: "2026-09-23T06:00:00Z",
        restart: null,
        restart_window: window,
      }),
    });
    await user.click(await screen.findByRole("button", { name: "Restart" }));
    const confirm = await screen.findByRole("dialog", { name: /Restart in the maintenance window/ });
    // 01:00 UTC is 02:00 in Lisbon (summer time).
    await waitFor(() => expect(confirm).toHaveTextContent("which opens 26 Sept 2026, 02:00 (Europe/Lisbon)"));
    expect(confirm).toHaveTextContent("If someone is signed in, they get 8 hours of warning first");
    expect(requestDeviceRestart).not.toHaveBeenCalled();
    await user.click(within(confirm).getByRole("button", { name: "Book restart" }));
    expect(requestDeviceRestart).toHaveBeenCalledWith("d1");
    expect(await screen.findByRole("status")).toHaveTextContent("Restart booked for ws-alpha.");
  });

  it("says so when a restart was already booked", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const requestDeviceRestart = vi.fn().mockResolvedValue({
      restart: { id: "r1", device_id: "d1", state: "pending", origin: "operator", scheduled_for: null, user_present: null, reason: null, created_at: "2026-09-23T12:00:00Z", closed_at: null },
      already_requested: true,
      window: { open_now: true, opens_at: "2026-09-23T12:00:00Z", user_warning_minutes: 15 },
    });
    renderHub(EVERYTHING, { requestDeviceRestart });
    await user.click(await screen.findByRole("button", { name: "Restart" }));
    const confirm = await screen.findByRole("dialog", { name: /Restart in the maintenance window/ });
    await user.click(within(confirm).getByRole("button", { name: "Book restart" }));
    expect(await screen.findByRole("status")).toHaveTextContent("A restart is already booked for ws-alpha");
  });

  it("hides the actions this operator lacks the permission for", async () => {
    renderHub(["ticket.write"]);
    await screen.findByRole("button", { name: "Open ticket" });
    for (const name of ["Remote in", "Run a script", "Install updates", "Restart", "Back up now", "Run job"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("offers only Open ticket on an asset with no agent", async () => {
    renderHub(EVERYTHING, {
      getDevice: vi.fn().mockResolvedValue({ ...DEVICE, agent_status: null, cert_state: null }),
    });
    await screen.findByRole("button", { name: "Open ticket" });
    expect(screen.queryByRole("button", { name: "Remote in" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Restart/ })).toBeNull();
  });

  it("installs approved updates on this device only, after asking, and says what happened", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const applyPatches = vi.fn().mockResolvedValue({ job_ids: ["j9"] });
    renderHub(EVERYTHING, { applyPatches });
    await user.click(await screen.findByRole("button", { name: "Install updates" }));
    const confirm = await screen.findByRole("dialog", { name: /Install approved updates/ });
    await user.click(within(confirm).getByRole("button", { name: "Install updates" }));
    expect(applyPatches).toHaveBeenCalledWith({ device_ids: ["d1"] });
    expect(await screen.findByRole("status")).toHaveTextContent("Install queued on 1 host.");
  });

  it("starts a backup of this device and says what happened", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const runBackup = vi.fn().mockResolvedValue({ job_ids: ["j8"] });
    renderHub(EVERYTHING, { runBackup });
    await user.click(await screen.findByRole("button", { name: "Back up now" }));
    expect(runBackup).toHaveBeenCalledWith("d1");
    expect(await screen.findByRole("status")).toHaveTextContent("Backup queued");
  });

  it("says a refused action was refused", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { ApiError } = await import("../../lib/api");
    renderHub(EVERYTHING, { runBackup: vi.fn().mockRejectedValue(new ApiError(409, "no backup policy")) });
    await user.click(await screen.findByRole("button", { name: "Back up now" }));
    const notice = await screen.findByRole("status");
    expect(notice).toHaveClass("red");
  });

  it("runs a script from the library on this device, after the SYSTEM confirmation", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const runScript = vi.fn().mockResolvedValue({ job_ids: ["j7"] });
    renderHub(EVERYTHING, {
      runScript,
      listScripts: vi.fn().mockResolvedValue([
        { id: "s1", name: "Clear temp", os_family: "windows", interpreter: "powershell", version: 1, created_by: null, created_at: "" },
      ]),
    });
    await user.click(await screen.findByRole("button", { name: "Run a script" }));
    const dialog = await screen.findByRole("dialog", { name: "Run a script on ws-alpha" });
    await user.click(within(dialog).getByRole("combobox", { name: "Script" }));
    await user.click(await screen.findByRole("option", { name: /Clear temp/ }));
    await user.click(within(dialog).getByRole("button", { name: "Run script" }));
    const confirm = await screen.findByRole("dialog", { name: /Run script as SYSTEM/ });
    await user.click(within(confirm).getByRole("button", { name: "Run script" }));
    expect(runScript).toHaveBeenCalledWith("s1", { target_type: "device", target_id: "d1" });
    expect(await screen.findByRole("status")).toHaveTextContent("Script sent");
  });

  it("opens a ticket with this device already chosen", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderHub(EVERYTHING, {
      listTicketCategories: vi.fn().mockResolvedValue([]),
      listTicketQueues: vi.fn().mockResolvedValue([]),
      listUsers: vi.fn().mockResolvedValue({ users: [] }),
      listDevices: vi.fn().mockResolvedValue({ items: [DEVICE], total: 1 }),
    });
    await user.click(await screen.findByRole("button", { name: "Open ticket" }));
    const dialog = await screen.findByRole("dialog", { name: "New ticket" });
    await waitFor(() =>
      expect(within(dialog).getByRole("combobox", { name: "Device (optional)" })).toHaveValue("ws-alpha"),
    );
  });
});
