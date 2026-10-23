/**
 * The device page for an asset with no agent (asset-inventory spec §5).
 *
 * THIS IS THE RULE `DevicesPage.test.tsx` ALREADY ENFORCES FOR COLUMNS,
 * applied to tabs: "a screen that looks finished and lies is worse than one
 * that is visibly incomplete, and it lies most convincingly on the demo where
 * somebody decides to trust it."
 *
 * A Patch tab on a switch can never populate. Rendering it — even empty, even
 * showing a zero — tells an operator this asset is patch-compliant. That is
 * not a weaker claim than a wrong number; it is the same claim.
 *
 * The three cases are different, and the difference matters to the operator:
 *   - no agent, could have one       -> say so, and link to the installer
 *   - no agent, will never have one  -> say so, and do NOT offer an installer
 *   - has an agent                   -> everything, unchanged
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Device, DeviceInventory, Me } from "../../lib/api";
import { DeviceDetailPage } from "./DeviceDetailPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

/** Every module tab granted, so a missing tab is about the ASSET, not a role. */
const ME: Me = {
  permissions: ["patch.read", "deploy.read", "backup.read", "provisioning.read"],
} as unknown as Me;

/** An asset has no inventory snapshot: nothing ever ran on it to take one. */
const EMPTY_INVENTORY: DeviceInventory = {
  summary: {},
  hardware: [],
  software: [],
  facts: {},
  snapshot_taken_at: null,
} as unknown as DeviceInventory;

function asset(overrides: Partial<Device> = {}): Device {
  return {
    id: "d1",
    hostname: "core-sw-01",
    fqdn: null,
    os_family: null,
    os_version: "",
    architecture: "",
    device_group_id: null,
    status: "active",
    agent_status: null,
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
    lifecycle: "in_service",
    asset_type: "network",
    source: "manual",
    description: "core switch, rack 3",
    primary_ip: "172.22.104.1",
    owner: null,
    location: null,
    ...overrides,
  } as Device;
}

function renderPage(
  device: Device,
  extra: { permissions?: string[]; updateAsset?: ReturnType<typeof vi.fn> } = {},
) {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue({ ...ME, permissions: extra.permissions ?? ME.permissions }),
    updateAsset: extra.updateAsset ?? vi.fn(),
    getDevice: vi.fn().mockResolvedValue(device),
    getDeviceInventory: vi.fn().mockResolvedValue(EMPTY_INVENTORY),
    getDeviceChanges: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listJobs: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/devices/d1"]}>
        <Routes>
          <Route path="/devices/:deviceId" element={<DeviceDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("a device page for an asset with no agent", () => {
  it("does not offer Patch, Deploy, Backup, Provisioning or Health", async () => {
    renderPage(asset());
    await screen.findByText(/no agent on this asset/i);
    for (const label of ["Updates", "Deployments", "Backup", "Imaging", "Health"]) {
      expect(screen.queryByRole("tab", { name: label })).not.toBeInTheDocument();
    }
  });

  it("says why, and points at the installer", async () => {
    renderPage(asset());
    expect(await screen.findByText(/no agent on this asset/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /agent packages/i })).toBeInTheDocument();
  });

  it("does not offer an agent for something that can never run one", async () => {
    // `unmanageable` means the operator said so. Offering an installer on every
    // row of a network estate is noise, and noise on a screen is how the useful
    // parts stop being read.
    renderPage(asset({ lifecycle: "unmanageable" }));
    await screen.findByText(/no agent on this asset/i);
    expect(screen.queryByRole("link", { name: /agent packages/i })).not.toBeInTheDocument();
  });

  it("shows what a push needs, and lets an asset manager set it", async () => {
    // Windows push needs a DNS name (Kerberos), and every push needs the OS
    // and processor. On a machine that never ran the agent nobody but a person
    // can supply them, and until 2026-09-29 no screen could.
    const updateAsset = vi.fn().mockResolvedValue(asset());
    renderPage(asset({ asset_type: "computer" }), {
      permissions: [...ME.permissions, "assets.manage"],
      updateAsset,
    });
    const panel = (await screen.findByText(/no agent on this asset/i)).closest("section, div")!;
    expect(document.body.textContent).toMatch(/DNS name: not recorded/);
    await userEvent.click(screen.getByRole("button", { name: /push install details/i }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/^dns name$/i), "pc1.corp.example.com");
    await userEvent.selectOptions(within(dialog).getByLabelText(/operating system/i), "windows");
    await userEvent.click(within(dialog).getByRole("button", { name: /^save$/i }));
    // ONLY what changed is sent, so the audit names only what changed.
    expect(updateAsset).toHaveBeenCalledWith("d1", { fqdn: "pc1.corp.example.com", os_family: "windows" });
    expect(panel).toBeTruthy();
  });

  it("does not offer push details without permission to manage assets", async () => {
    renderPage(asset());
    await screen.findByText(/no agent on this asset/i);
    expect(screen.queryByRole("button", { name: /push install details/i })).not.toBeInTheDocument();
  });

  it("still offers every tab for a managed device", async () => {
    // NON-VACUITY. A page that hid those tabs unconditionally would pass all
    // three tests above and remove Patch from the entire fleet.
    renderPage(asset({ agent_status: "online", os_family: "linux", source: "agent" }));
    expect(await screen.findByRole("tab", { name: "Health" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Updates" })).toBeInTheDocument();
    expect(screen.queryByText(/no agent on this asset/i)).not.toBeInTheDocument();
  });
});
