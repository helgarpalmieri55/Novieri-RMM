/**
 * The device record on the record kit (redesign plan Task 6, 1.2; Receta F;
 * the "atera 8" capture), mounted with the fixture API under a data router:
 * dev-1 is `lis-ws-041`, a managed Windows workstation of the internal client
 * with open alerts, outstanding updates, a backup and the fixture session's
 * every code.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../fixtures/client";
import { renderPage } from "../../test/renderPage";
import { DeviceDetailPage } from "./DeviceDetailPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

beforeEach(() => mockUseApi.mockReturnValue(fixtureClient()));

const card = async (name: string) =>
  (await screen.findByRole("heading", { level: 2, name })).closest("section") as HTMLElement;

describe("the device record (Receta F)", () => {
  it("puts the tab in the URL, and an unknown tab opens the default", async () => {
    const { router } = renderPage("/devices/dev-1?tab=hardware", <DeviceDetailPage />, "/devices/:deviceId");
    expect(await screen.findByRole("tab", { name: "Hardware" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "Software" }));
    expect(router.state.location.search).toBe("?tab=software");
    await act(() => router.navigate("/devices/dev-1?tab=nope"));
    expect(await screen.findByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  });

  it("names the record in the browser tab", async () => {
    renderPage("/devices/dev-1", <DeviceDetailPage />, "/devices/:deviceId", { page: "shell.nav.item.devices" });
    await screen.findByRole("heading", { level: 1, name: "lis-ws-041" });
    // The title is written in an effect, after the heading's render.
    await waitFor(() => expect(document.title).toBe("lis-ws-041 · Devices · Novieri RMM"));
  });

  it("heads the record with its way back, its state and its client", async () => {
    // The fixture rows carry no `client_id` (src/fixtures/core.ts); this one is Andes'.
    const registry = fixtureRegistry();
    const getDevice = registry.getDevice!;
    mockUseApi.mockReturnValue(
      fixtureClient({ ...registry, getDevice: async (id) => ({ ...(await getDevice(id)), client_id: ANDES }) }),
    );
    renderPage("/devices/dev-1", <DeviceDetailPage />, "/devices/:deviceId");
    const head = (await screen.findByRole("heading", { level: 1, name: "lis-ws-041" })).closest("section") as HTMLElement;
    expect(within(head).getByRole("link", { name: "All devices" })).toHaveAttribute("href", "/devices");
    expect(within(head).getByText("Online")).toBeInTheDocument();
    expect(await within(head).findByRole("link", { name: "Andes Logistics" })).toHaveAttribute("href", `/clients/${ANDES}`);
    // Remote in, the capture's "Connect", leads the actions.
    const actions = within(head).getAllByRole("button");
    expect(actions[0]).toHaveTextContent("Remote in");
  });

  it("draws the alert and patch summaries and each leads to its tab", async () => {
    renderPage("/devices/dev-1", <DeviceDetailPage />, "/devices/:deviceId");
    const alerts = await card("Alert status");
    for (const severity of ["Critical", "Warning", "Info"]) {
      const link = await within(alerts).findByRole("link", { name: new RegExp(`^${severity}: \\d+, on the Health tab$`) });
      expect(link).toHaveAttribute("href", "/devices/dev-1?tab=health");
    }
    const patch = await card("Patch status");
    expect(await within(patch).findByRole("link", { name: "Review updates" })).toHaveAttribute("href", "/devices/dev-1?tab=patch");
    const counted = await within(patch).findAllByRole("link", { name: /updates outstanding, on the Updates tab$/ });
    expect(counted.length).toBeGreaterThan(0);
    for (const link of counted) expect(link).toHaveAttribute("href", "/devices/dev-1?tab=patch");
  });

  it("lays the Overview out as information on the left and what it reports on the right", async () => {
    renderPage("/devices/dev-1", <DeviceDetailPage />, "/devices/:deviceId");
    const panel = await screen.findByRole("tabpanel", { name: "Overview" });
    const grid = panel.querySelector(".detail-grid") as HTMLElement;
    const main = within(grid.querySelector(".detail-main") as HTMLElement);
    const aside = within(grid.querySelector(".detail-aside") as HTMLElement);
    expect(main.getByRole("heading", { name: "Device information" })).toBeInTheDocument();
    expect(main.getByRole("heading", { name: "Relations" })).toBeInTheDocument();
    expect(aside.getByRole("heading", { name: "Checks" })).toBeInTheDocument();
    expect(aside.getByRole("heading", { name: "Activity" })).toBeInTheDocument();
    // The latest reading of each check, and no chart: series are another project (decision 2026-11-24).
    expect(await aside.findAllByText("41")).not.toHaveLength(0);
    expect(panel.querySelector("svg.chart, .chart")).toBeNull();
  });

  it("keeps the backup and open-ticket panels of the overview", async () => {
    renderPage("/devices/dev-1", <DeviceDetailPage />, "/devices/:deviceId");
    const backup = await card("Backup");
    expect(within(backup).getByRole("link", { name: "Backup details" })).toHaveAttribute("href", "/devices/dev-1?tab=backup");
    const tickets = await card("Open tickets");
    expect(within(tickets).getByRole("link", { name: "All tickets" })).toHaveAttribute("href", "/service-desk?all=1");
  });
});
