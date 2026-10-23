import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { AppsPanel, appSourceSentences } from "../AppsPanel";
import { appSourceLine } from "../DevicePatchTab";
import type { PatchUpdate, PatchUpdateFilters, PatchUpdateListResponse } from "../types";

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

const CHROME: PatchUpdate = {
  id: "p-chrome",
  os_family: "windows",
  source: "winget",
  identifier: "Google.Chrome",
  title: "Google Chrome 128.0 → 129.0",
  severity: "unknown",
  kb: null,
  cve: null,
  priority: null,
  first_seen_at: "2026-09-20T00:00:00Z",
  devices_outstanding: 3,
  devices_installed: 0,
  devices_failed: 0,
  cleared_rings: [],
} as unknown as PatchUpdate;

function page(items: PatchUpdate[]): PatchUpdateListResponse {
  return {
    items,
    total: items.length,
    total_unfiltered: items.length,
    severity_counts: { unknown: items.length },
    last_scan_at: "2026-09-23T08:00:00Z",
    devices_scanned: 4,
    devices_total: 4,
  } as unknown as PatchUpdateListResponse;
}

describe("app source copy", () => {
  it("says, in words, where app updates could and could not be read", () => {
    expect(
      appSourceSentences("winget", [
        { source: "winget", state: "scanned", devices: 12, detail: null },
        {
          source: "winget",
          state: "unsupported",
          devices: 1,
          detail: "winget (App Installer) is not installed on this device",
        },
      ]),
    ).toEqual([
      "winget checked on 12 machines.",
      "winget not available on 1 machine: winget (App Installer) is not installed on this device.",
    ]);
    expect(
      appSourceLine({ source: "homebrew", state: "failed", detail: "brew outdated failed (exit 1)" }),
    ).toBe("Apps from Homebrew could not be checked: brew outdated failed (exit 1).");
  });
});

describe("AppsPanel", () => {
  it("lists app updates only, and says how the manager fared", async () => {
    const listPatchUpdates = vi.fn((params: PatchUpdateFilters) =>
      Promise.resolve(page(params.kind === "apps" ? [CHROME] : [])),
    );
    mockUseApi.mockReturnValue({
      listPatchUpdates,
      listPatchAppSources: vi.fn().mockResolvedValue({
        items: [{ source: "winget", state: "scanned", devices: 4, detail: null }],
      }),
    });
    renderWith(
      <AppsPanel family="windows" canApprove ringOptions={[]} fleetDeviceTotal={4} />,
    );
    expect(await screen.findByText("Google Chrome 128.0 → 129.0")).toBeTruthy();
    expect(await screen.findByText("winget checked on 4 machines.")).toBeTruthy();
    await waitFor(() => expect(listPatchUpdates).toHaveBeenCalled());
    for (const [params] of listPatchUpdates.mock.calls) {
      expect(params).toMatchObject({ kind: "apps", os_family: "windows" });
    }
  });

  it("says when no machine has reported on Homebrew yet", async () => {
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn().mockResolvedValue(page([])),
      listPatchAppSources: vi.fn().mockResolvedValue({ items: [] }),
    });
    renderWith(<AppsPanel family="macos" canApprove={false} ringOptions={[]} fleetDeviceTotal={2} />);
    expect(
      await screen.findByText(/No machine has reported on Homebrew yet/),
    ).toBeTruthy();
    expect(
      await screen.findByText("No reporting machine has a newer version of an app from Homebrew."),
    ).toBeTruthy();
  });
});
