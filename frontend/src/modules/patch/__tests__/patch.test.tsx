import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, DevicePage, Me, MeClient } from "../../../lib/api";
import type {
  ApprovalListResponse,
  BulkApprovalResponse,
  DeviceStatus,
  FleetReport,
  Patch,
  PatchApproval,
  PatchDeviceRollup,
  PatchDeviceRollupListResponse,
  PatchPolicy,
  PatchRingListResponse,
  PatchRun,
  PatchUpdate,
  PatchUpdateFilters,
  PatchUpdateListResponse,
} from "../types";
import { DevicePatchTab } from "../DevicePatchTab";
import { FleetPatchPage } from "../FleetPatchPage";
import { PatchPolicyEditor } from "../PatchPolicyEditor";
import { patchStateLabel, patchStateVariant, severityVariant } from "../patchState";
import { describeCron } from "../cron";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch. Patch data is plain TanStack Query state (no live-event
// stream), so freshness comes from invalidating the patch query keys after
// mutations succeed — that behavior is exercised by the hooks in api.ts, not
// re-asserted here (see SessionsPage.test.tsx for that pattern elsewhere).
//
// THE DEFAULTS LAYER UNDER EVERY TEST'S OWN MOCK, and it is not laziness. The
// fleet page fetches six things now; a factory that omitted one would put a
// second `role="alert"` on the page and quietly change what
// `findByRole("alert")` resolves to in the tests that assert on a specific
// failure. A test overrides exactly the call it is about.
const mockUseApi = vi.fn();
const mockDefaults = (): Partial<ApiClient> => ({
  me: vi.fn().mockResolvedValue(ME),
  listPatchApprovals: vi.fn().mockResolvedValue(NO_APPROVALS),
  listPatchUpdates: vi.fn().mockResolvedValue(updatePage(UPDATES)),
  listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([DEVICE_ROLLUP])),
  // No apply has run: `LastApplyCard` renders nothing, which is the
  // healthy default. A test about a run overrides this.
  listPatchRuns: vi.fn().mockResolvedValue({ items: [] }),
  getPatchFleetReport: vi.fn().mockResolvedValue(FLEET_REPORT),
  listPatchRings: vi.fn().mockResolvedValue(RINGS),
  bulkApprovePatches: vi.fn().mockResolvedValue(bulkResult(["p1"], "test")),
  triggerPatchScan: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
  applyPatches: vi.fn().mockResolvedValue({ job_ids: ["j-apply-1"] }),
  exportPatchReport: vi.fn().mockResolvedValue(new Blob(["identifier,state\n"])),
  // THE DENOMINATOR THE PATCH API CANNOT GIVE. `fleet_scan_coverage` counts the
  // whole org whatever `os_family` was filtered on, so a per-platform tab needs
  // `GET /devices?os_family=…` to say how many machines of THIS platform carry
  // an agent — which is the number that separates "no Windows fleet" from
  // "clean Windows fleet". `all - assets` is enrolment: `assets` is the
  // server's own name for a row with no agent.
  listDevices: vi.fn().mockResolvedValue(devicePage(60)),
  listPatchRestarts: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  listPatchAppSources: vi.fn().mockResolvedValue({ items: [] }),
  getCurrentOrg: vi.fn().mockResolvedValue({
    id: "o1",
    name: "Org",
    slug: "org",
    status: "active",
    settings: {},
  }),
});

/** The one field of `GET /devices` this module reads, in a whole response. */
function devicePage(managed: number, withoutAgent = 0): DevicePage {
  return {
    items: [],
    total: managed + withoutAgent,
    state_counts: {
      all: managed + withoutAgent,
      online: 0,
      offline: 0,
      assets: withoutAgent,
      certs: 0,
    },
    fleet: { total: managed + withoutAgent, managed, online: 0, cert_attention: 0 },
  };
}
// The Apps list (`AppsPanel`) asks for `kind: "apps"`; these tests are about
// the OS list, so app requests answer empty and never reach a test's own mock.
vi.mock("../../../lib/ApiContext", () => ({
  useApi: () => {
    const merged: Partial<ApiClient> = { ...mockDefaults(), ...mockUseApi() };
    const list = merged.listPatchUpdates;
    return {
      ...merged,
      listPatchUpdates: (params?: PatchUpdateFilters) =>
        params?.kind === "apps"
          ? Promise.resolve(updatePage([]))
          : list?.(params) ?? Promise.resolve(updatePage([])),
    };
  },
}));

function renderWithProviders(ui: ReactElement, path = "/patch") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  // `devices.read` is here because the patch page needs it for one number it
  // cannot get anywhere else: how many machines of the OPEN platform carry an
  // agent. `fleet_scan_coverage` counts the whole org whatever `os_family` was
  // filtered on, so the per-platform denominator — and the difference between
  // "no Windows fleet" and "clean Windows fleet" — comes from
  // `GET /devices?os_family=…`. An operator without it is a real configuration
  // and has its own test.
  permissions: ["patch.read", "patch.approve", "patch.apply", "patch.scan", "devices.read"],
};

const PATCHES: Patch[] = [
  {
    id: "p1",
    os_family: "windows",
    source: "wsus",
    identifier: "KB5031354",
    title: "2023-10 Cumulative Update",
    severity: "critical",
    kb: "KB5031354",
    cve: ["CVE-2023-1234"],
    superseded_by: null,
    priority: 1,
    first_seen_at: "2026-01-01T00:00:00.000Z",
  },
];

/** A second and third catalogue row, so "select everything shown" has
 *  something to select and a filter has something to exclude. */
const MANY_PATCHES: Patch[] = [
  ...PATCHES,
  {
    id: "p2",
    os_family: "windows",
    source: "wsus",
    identifier: "KB5031355",
    title: "Defender definition update",
    severity: "moderate",
    kb: "KB5031355",
    cve: null,
    superseded_by: null,
    priority: 5,
    first_seen_at: "2026-01-01T00:00:00.000Z",
  },
  {
    id: "p3",
    os_family: "linux",
    source: "apt",
    identifier: "openssl-3.0.11",
    title: "openssl security update",
    severity: "critical",
    kb: null,
    cve: ["CVE-2023-5678"],
    superseded_by: null,
    priority: 1,
    first_seen_at: "2026-01-01T00:00:00.000Z",
  },
];

/** THE FLEET ROLLUP ROW: one per patch, carrying the number the catalogue
 *  route could not say. `devices_outstanding` is what makes the row a decision
 *  rather than a line item. */
function update(patch: Patch, outstanding = 1, clearedRings: string[] = []): PatchUpdate {
  return {
    id: patch.id,
    os_family: patch.os_family,
    source: patch.source,
    identifier: patch.identifier,
    title: patch.title,
    severity: patch.severity,
    kb: patch.kb,
    cve: patch.cve,
    priority: patch.priority,
    first_seen_at: patch.first_seen_at,
    devices_missing: outstanding,
    devices_approved: 0,
    devices_failed: 0,
    devices_installed: 0,
    devices_outstanding: outstanding,
    device_state: null,
    device_last_error: null,
    device_applied_at: null,
    cleared_rings: clearedRings,
  };
}

const UPDATES: PatchUpdate[] = [update(PATCHES[0] as Patch, 41)];
const MANY_UPDATES: PatchUpdate[] = MANY_PATCHES.map((patch) => update(patch));

/** SCAN COVERAGE TRAVELS WITH EVERY ROLLUP. An empty page is the same shape on
 *  a clean fleet and on one nothing has scanned, and these three say which. */
const SCANNED = { last_scan_at: "2026-01-05T09:00:00.000Z", devices_scanned: 1, devices_total: 1 };
const NEVER_SCANNED_COVERAGE = { last_scan_at: null, devices_scanned: 0, devices_total: 1 };

function updatePage(
  items: PatchUpdate[],
  overrides: Partial<PatchUpdateListResponse> = {},
): PatchUpdateListResponse {
  const severityCounts: Record<string, number> = {};
  for (const row of items) severityCounts[row.severity] = (severityCounts[row.severity] ?? 0) + 1;
  return {
    items,
    total: items.length,
    total_unfiltered: items.length,
    severity_counts: severityCounts,
    ...SCANNED,
    ...overrides,
  };
}

const DEVICE_ROLLUP: PatchDeviceRollup = {
  device_id: "dev-1",
  // The internal client's machine: `ME.org_id`.
  client_id: "o1",
  hostname: "sentinel",
  os_family: "windows",
  outstanding: 41,
  failed: 0,
  worst_severity: "critical",
  counts_by_severity: { critical: 1, important: 40, moderate: 0, low: 0, unknown: 0 },
  last_scan_at: "2026-01-05T09:00:00.000Z",
};

function deviceRollupPage(
  items: PatchDeviceRollup[],
  overrides: Partial<PatchDeviceRollupListResponse> = {},
): PatchDeviceRollupListResponse {
  return { items, total: items.length, ...SCANNED, ...overrides };
}

/** The blast radius the console shows before an approval is authorised. The
 *  rings PARTITION the fleet, so the counts add up to `device_total`; each
 *  ring's `client_device_counts` add up to its `device_count`. "o1" is the
 *  internal client (`ME.org_id`), "c-acme" a customer. */
const RINGS: PatchRingListResponse = {
  items: [
    { ring_name: "test", ring_order: 0, device_count: 3, client_device_counts: [{ client_id: "o1", device_count: 2 }, { client_id: "c-acme", device_count: 1 }] },
    { ring_name: "pilot", ring_order: 50, device_count: 17, client_device_counts: [{ client_id: "o1", device_count: 17 }] },
    { ring_name: "broad", ring_order: 100, device_count: 220, client_device_counts: [{ client_id: "o1", device_count: 200 }, { client_id: "c-acme", device_count: 20 }] },
  ],
  total: 3,
  device_total: 240,
};

function bulkResult(patchIds: string[], ring: string, alreadyInForce = 0): BulkApprovalResponse {
  return {
    items: patchIds.map((patchId, index) => ({
      id: `bulk-a${index}`,
      client_id: "o1",
      patch_id: patchId,
      patch_identifier: `KB-${patchId}`,
      ring_name: ring,
      approved_by: "u1",
      approved_at: "2026-01-02T00:00:00.000Z",
      revoked: false,
      revoked_by: null,
      revoked_at: null,
    })),
    total: patchIds.length,
    newly_approved: patchIds.length - alreadyInForce,
    already_in_force: alreadyInForce,
  };
}

const FLEET_REPORT: FleetReport = {
  compliant_pct_by_os_family: { windows: 82.5 },
  compliant_pct_by_severity: { critical: 91.0 },
  exposures: [],
  exposed_device_count: 0,
  critical_device_count: 0,
  ...SCANNED,
};

const POLICY: PatchPolicy = {
  id: "pol1",
  device_group_id: null,
  ring_name: "broad",
  ring_order: 100,
  maintenance_window: "Sat 02:00-04:00",
  auto_approve_security: false,
  reboot: "if_required",
  reboot_deferral_hours: 24,
  scan_interval_hours: 24,
};

const APPROVAL: PatchApproval = {
  id: "a1",
  client_id: null,
  patch_id: "p1",
  patch_identifier: "KB5031354",
  ring_name: "pilot",
  approved_by: "u1",
  approved_at: "2026-01-02T00:00:00.000Z",
  revoked: false,
  revoked_by: null,
  revoked_at: null,
};

/** The same approval after `DELETE /patch/approvals/{id}`: the row SURVIVES
 *  the revoke (spec §5) so the trail holds, which is why the list must show
 *  it rather than drop it. */
const REVOKED_APPROVAL: PatchApproval = {
  id: "a2",
  client_id: null,
  patch_id: "p1",
  patch_identifier: "KB5031354",
  ring_name: "broad",
  approved_by: "u1",
  approved_at: "2026-01-01T00:00:00.000Z",
  revoked: true,
  revoked_by: "u1",
  revoked_at: "2026-01-03T00:00:00.000Z",
};

const NO_APPROVALS: ApprovalListResponse = { items: [], total: 0 };

/** The stat card carrying `label`. `getAllByText` then filter, because a stat
 *  label can also appear as a panel title or a table header on this page. */
function statCard(label: string): HTMLElement {
  const card = screen
    .getAllByText(label)
    .map((node) => node.closest(".stat-card"))
    .find((node): node is HTMLElement => node != null);
  if (!card) throw new Error(`no stat card labelled ${label}`);
  return card;
}

function statValues(): (string | undefined)[] {
  return [...document.querySelectorAll(".stat-card")].map(
    (card) => card.querySelector(".stat-value")?.textContent ?? undefined,
  );
}

/** A page where nothing has ever reported: no updates, no devices, no
 *  percentages, and `devices_scanned: 0` saying WHY. */
function neverScannedApi(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
  return {
    listPatchUpdates: vi
      .fn()
      .mockResolvedValue(updatePage([], { ...NEVER_SCANNED_COVERAGE, devices_total: 0 })),
    listPatchDeviceRollup: vi
      .fn()
      .mockResolvedValue(
        deviceRollupPage([], { ...NEVER_SCANNED_COVERAGE, devices_total: 0 }),
      ),
    getPatchFleetReport: vi.fn().mockResolvedValue({
      compliant_pct_by_os_family: {},
      compliant_pct_by_severity: {},
      exposures: [],
      exposed_device_count: 0,
      critical_device_count: 0,
      ...NEVER_SCANNED_COVERAGE,
      devices_total: 0,
    } satisfies FleetReport),
    ...overrides,
  };
}

// ===========================================================================
// THE OPERATOR'S REPORT, 2026-08-25: "patch management is still a super long
// list of missing updates and that's just one device online, that view needs
// to change".
//
// What they were looking at was an "Exposed devices" table of one row per
// (device, patch) pair -- `FleetReport.exposures`, uncapped -- above a
// catalogue table of the same updates again with no fleet count on them. One
// Ubuntu box with 300 upgradable packages rendered ~600 rows and a
// 29,000-pixel page. Measured in Chromium before this change: 601 `tbody tr`,
// scrollHeight 29317.
//
// These tests pin the shape that replaced it and the three things it must not
// lose: the primary row is a PATCH with a fleet count on it, no filtered count
// is ever presented as a total, and nothing a device is missing becomes
// invisible.
// ===========================================================================

describe("FleetPatchPage — the fleet view is one row per update, not per pair", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {};
    mockUseApi.mockImplementation(() => api);
  });

  it("renders one row per update carrying how many devices are missing it", async () => {
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    const row = screen.getByText("2023-10 Cumulative Update").closest("tr") as HTMLElement;
    // THE NUMBER THE OLD PAGE COULD NOT SAY. `GET /patch/patches` returns bare
    // catalogue rows, so an update missing on one machine and one missing on
    // forty-one rendered identically.
    expect(within(row).getByText("41 devices")).toBeInTheDocument();
  });

  it("does not render one row per (device, patch) pair any more", async () => {
    // The panel is gone by name and by shape: 300 packages on one machine used
    // to be 300 rows under one hostname heading.
    api = {
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage(MANY_UPDATES)),
    };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    expect(screen.queryByText("Exposed devices")).not.toBeInTheDocument();
    // One row per update in the Updates panel; the device panel has one row per
    // machine. Three updates and one device is four rows, not three-times-one.
    expect(screen.getAllByRole("row").length).toBeLessThan(12);
  });

  it("asks the SERVER for the filter rather than filtering the fetched page", async () => {
    const listPatchUpdates = vi.fn().mockResolvedValue(updatePage(MANY_UPDATES));
    api = { listPatchUpdates };
    renderWithProviders(<FleetPatchPage />, "/patch?q=openssl&os=linux");

    await screen.findByText("openssl security update");
    // The previous console filtered the rows it had already fetched, so a
    // search could not reach past the cap and the table answered "No patches
    // match that filter" about updates it had never asked for.
    expect(listPatchUpdates).toHaveBeenCalledWith(
      expect.objectContaining({ q: "openssl", os_family: "linux" }),
    );
  });

  // ------------------------------------------------------------------------
  // THE SEVERITY DEFAULT, AND THE HALF THAT MAKES IT SAFE.
  //
  // The list defaults to the severities that demand a decision. That is only
  // honest if the page also says how big the set it is NOT showing is: "8
  // updates" on a machine missing 300 is a lie the reader cannot detect from
  // the page alone, and it would be the same defect as the long list wearing
  // a tidier face.
  // ------------------------------------------------------------------------

  // ------------------------------------------------------------------------
  // THE DEFAULT SEVERITY FILTER IS THE TAB'S, NOT THE MODULE'S, and the two
  // tests below are the same assertion pointed at platforms whose scanners
  // disagree. This is the whole reason the 2026-08-26 split is not cosmetic:
  //
  //   apt        marks a version `important` if and only if it came from the
  //              security pocket (`aptSeverity`), so the narrowed list IS the
  //              security list and narrowing is the right default.
  //   Windows    `winupdate.go` maps an unrated `MsrcSeverity` to `unknown`,
  //              and most Windows 11 cumulative updates carry no rating — so
  //              the SAME default would hide the monthly rollup.
  //
  // One constant cannot be correct on both. `defaultSeveritiesFor` is the
  // derivation; these are the two halves of it, observed through the page.
  // ------------------------------------------------------------------------

  it("narrows to the security pocket on Linux, and says so in the control", async () => {
    const listPatchUpdates = vi.fn().mockResolvedValue(updatePage(UPDATES));
    api = { listPatchUpdates };
    renderWithProviders(<FleetPatchPage />, "/patch?os=linux");

    await screen.findByText("2023-10 Cumulative Update");
    expect(listPatchUpdates).toHaveBeenCalledWith(
      expect.objectContaining({ severity: ["critical", "important"], os_family: "linux" }),
    );
    // VISIBLE, not silent: the narrowed state has to read as a choice that is
    // currently in force.
    expect(screen.getByRole("combobox", { name: "Filter Linux updates by severity" })).toHaveValue(
      "critical,important",
    );
  });

  it("does NOT narrow on Windows, because Microsoft leaves the rollup unrated", async () => {
    const listPatchUpdates = vi.fn().mockResolvedValue(updatePage(UPDATES));
    api = { listPatchUpdates };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    expect(listPatchUpdates).toHaveBeenCalledWith(
      expect.objectContaining({ severity: [], os_family: "windows" }),
    );
    expect(
      screen.getByRole("combobox", { name: "Filter Windows updates by severity" }),
    ).toHaveValue("all");
  });

  it("says how many updates the severity filter is hiding, and offers to widen", async () => {
    const user = userEvent.setup();
    const listPatchUpdates = vi
      .fn()
      .mockResolvedValue(updatePage(UPDATES, { total: 8, total_unfiltered: 300 }));
    api = { listPatchUpdates };
    // The Linux tab, because it is the one that narrows — this sentence exists
    // to stop a narrowed count reading as a total, and on a tab that does not
    // narrow there is nothing for it to say.
    renderWithProviders(<FleetPatchPage />, "/patch?os=linux");

    expect(
      await screen.findByText("292 more updates are outside the severity filter."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show all 300 updates" }));

    expect(listPatchUpdates).toHaveBeenLastCalledWith(
      expect.objectContaining({ severity: [] }),
    );
  });

  it("says nothing about hidden updates when the filter is hiding none", async () => {
    // A permanent notice about zero hidden rows is noise, and noise is what
    // teaches a reader to skip the notice that matters.
    api = { listPatchUpdates: vi.fn().mockResolvedValue(updatePage(UPDATES)) };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    expect(screen.queryByText(/outside the severity filter/)).not.toBeInTheDocument();
  });

  it("always says where the page sits inside the whole set", async () => {
    api = {
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage(UPDATES, { total: 300 })),
    };
    renderWithProviders(<FleetPatchPage />);

    // Not only when a cap bites. A page that always states its position is
    // what teaches the reader that `total` is the real number.
    expect(await screen.findByText("Showing 1–1 of 300 updates.")).toBeInTheDocument();
  });

  it("pages the list server-side instead of rendering the whole catalogue", async () => {
    const user = userEvent.setup();
    const listPatchUpdates = vi
      .fn()
      .mockResolvedValue(updatePage(UPDATES, { total: 300, total_unfiltered: 300 }));
    api = { listPatchUpdates };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(listPatchUpdates).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 25 }));
  });

  it("says which rings an update is already cleared for, from the row itself", async () => {
    // It used to be derived from a capped `GET /patch/approvals` page, and the
    // panel had to carry a disclaimer saying the column under-reports.
    api = {
      listPatchUpdates: vi
        .fn()
        .mockResolvedValue(updatePage([update(PATCHES[0] as Patch, 41, ["test", "pilot"])])),
    };
    renderWithProviders(<FleetPatchPage />);

    const row = (await screen.findByText("2023-10 Cumulative Update")).closest(
      "tr",
    ) as HTMLElement;
    expect(within(row).getByText("test, pilot")).toBeInTheDocument();
    expect(screen.queryByText(/read from the newest/)).not.toBeInTheDocument();
  });

  // ------------------------------------------------------------------------
  // The bulk approval, unchanged in substance: approval is (patch, ring) with
  // no device column, so one decision clears an update for a whole ring.
  // ------------------------------------------------------------------------

  it("approves a whole selection in one call instead of one call per row", async () => {
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn().mockResolvedValue(bulkResult(["p1", "p3"], "test"));
    api = {
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage(MANY_UPDATES)),
      bulkApprovePatches,
    };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select KB5031354" }));
    await user.click(screen.getByRole("checkbox", { name: "Select openssl-3.0.11" }));
    await user.click(screen.getByRole("button", { name: /^Approve 2 updates/ }));

    // The click on a selection authorises nothing on its own: an approval is
    // what lets the apply path ship these updates to a whole ring.
    expect(bulkApprovePatches).not.toHaveBeenCalled();
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));

    expect(bulkApprovePatches).toHaveBeenCalledTimes(1);
    // The approval is the internal client's -- `/me`'s `org_id` -- until the
    // client selector exists (MSP clients plan 2026-10-26 Task 14).
    expect(bulkApprovePatches).toHaveBeenCalledWith({
      patch_ids: ["p1", "p3"],
      ring_name: "test",
      client_id: "o1",
    });
  });

  it("approves for every client only when asked, sending no client", async () => {
    // A global approval clears the update for every client's machines in the
    // ring; it is a choice the person makes, not the default.
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn().mockResolvedValue(bulkResult(["p1"], "test"));
    api = {
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage(MANY_UPDATES)),
      bulkApprovePatches,
    };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select KB5031354" }));
    await user.click(screen.getByRole("checkbox", { name: /For every client/ }));
    await user.click(screen.getByRole("button", { name: /^Approve 1 update/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));

    expect(bulkApprovePatches).toHaveBeenCalledWith({ patch_ids: ["p1"], ring_name: "test" });
  });

  it("says how many machines the ring reaches before the approval is answered", async () => {
    const user = userEvent.setup();
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select KB5031354" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: /^Ring to approve into/ }),
      "pilot",
    );

    // On the page, beside the control, before anything is clicked -- and the
    // reach of THIS approval, the internal client's (final review E: it used
    // to say the ring's whole population whoever the approval was for).
    expect(
      await screen.findByText(
        "An approval here covers the 17 devices of your organisation's own client in the pilot ring.",
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Approve 1 update/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    expect(confirm).toHaveTextContent("1 update → pilot ring");
    expect(confirm).toHaveTextContent(/17 devices/);
  });

  it("reports what the batch changed, not the number of rows that were ticked", async () => {
    const user = userEvent.setup();
    api = {
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage(MANY_UPDATES)),
      bulkApprovePatches: vi.fn().mockResolvedValue(bulkResult(["p1", "p2", "p3"], "test", 2)),
    };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select every update on this page" }));
    await user.click(screen.getByRole("button", { name: /^Approve 3 updates/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "1 update approved for the test ring. 2 were already in force.",
    );
  });

  it("selects only the rows the server actually returned for this page", async () => {
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn().mockResolvedValue(bulkResult(["p1"], "test"));
    api = {
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage(UPDATES, { total_unfiltered: 3 })),
      bulkApprovePatches,
    };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select every update on this page" }));
    await user.click(screen.getByRole("button", { name: /^Approve 1 update/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));

    // A tick that survived a filter or a page change would let an operator
    // approve an update they can no longer see, which is the one thing a bulk
    // control must never do.
    expect(bulkApprovePatches).toHaveBeenCalledWith({
      patch_ids: ["p1"],
      ring_name: "test",
      client_id: "o1",
    });
  });

  it("offers no approval control at all until something is selected", async () => {
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    expect(screen.queryByRole("button", { name: /^Approve/ })).not.toBeInTheDocument();
  });

  it("approves nothing when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn();
    api = { bulkApprovePatches };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select KB5031354" }));
    await user.click(screen.getByRole("button", { name: /^Approve 1 update/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(bulkApprovePatches).not.toHaveBeenCalled();
  });

  it("hides every approval control when the caller lacks patch.approve", async () => {
    api = { me: vi.fn().mockResolvedValue({ ...ME, permissions: ["patch.read", "patch.scan"] }) };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    expect(screen.queryByRole("checkbox", { name: "Select KB5031354" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("checkbox", { name: "Select every update on this page" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Approve/ })).not.toBeInTheDocument();
  });

  it("hides the Scan control when the caller lacks patch.scan", async () => {
    api = { me: vi.fn().mockResolvedValue({ ...ME, permissions: ["patch.read", "patch.approve"] }) };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    expect(screen.queryByRole("button", { name: "Scan" })).not.toBeInTheDocument();
  });

  it("offers the test ring first, matching the test -> pilot -> broad rollout (PD-04)", async () => {
    const user = userEvent.setup();
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select KB5031354" }));
    const select = screen.getByRole("combobox", {
      name: /^Ring to approve into/,
    }) as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "test",
      "pilot",
      "broad",
    ]);
    expect(select.value).toBe("test");
    // Each option carries its own size, so choosing a ring and reading its
    // blast radius are the same act rather than two.
    expect(select.options[2]?.text).toContain("220 devices");
  });

  it("offers a ring this deployment named that the console does not hardcode", async () => {
    const user = userEvent.setup();
    api = {
      listPatchRings: vi.fn().mockResolvedValue({
        items: [
          { ring_name: "canary", ring_order: 5, device_count: 2, client_device_counts: [{ client_id: "o1", device_count: 2 }] },
          { ring_name: "broad", ring_order: 100, device_count: 40, client_device_counts: [{ client_id: "o1", device_count: 40 }] },
        ],
        total: 2,
        device_total: 42,
      } satisfies PatchRingListResponse),
    };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select KB5031354" }));
    const select = screen.getByRole("combobox", {
      name: /^Ring to approve into/,
    }) as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "canary",
      "broad",
      "test",
      "pilot",
    ]);
  });

  // ------------------------------------------------------------------------
  // Scan, export and their failure sentences. Unchanged in substance from the
  // page this replaced, and the reasons are in the comments there.
  // ------------------------------------------------------------------------

  it("says what the scan dispatched instead of returning silently to idle", async () => {
    const user = userEvent.setup();
    const triggerPatchScan = vi.fn().mockResolvedValue({ job_ids: ["j1"] });
    api = { triggerPatchScan };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("button", { name: "Scan" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Scan queued — 1 job dispatched.");
    expect(triggerPatchScan).toHaveBeenCalledTimes(1);
  });

  it("says no jobs were dispatched when the scan matched no devices", async () => {
    const user = userEvent.setup();
    api = { triggerPatchScan: vi.fn().mockResolvedValue({ job_ids: [] }) };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("button", { name: "Scan" }));

    expect(await screen.findByRole("status")).toHaveTextContent(/no devices were eligible/);
  });

  it("renders a sentence for a bodyless 503 on scan instead of an empty error", async () => {
    const user = userEvent.setup();
    api = { triggerPatchScan: vi.fn().mockRejectedValue(new ApiError(503, "")) };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("button", { name: "Scan" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("announces a bodyless 503 from Approve instead of an empty span", async () => {
    const user = userEvent.setup();
    api = { bulkApprovePatches: vi.fn().mockRejectedValue(new ApiError(503, "")) };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select KB5031354" }));
    await user.click(screen.getByRole("button", { name: /^Approve 1 update/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/);
  });

  it("announces a failed evidence export as a sentence", async () => {
    const user = userEvent.setup();
    api = { exportPatchReport: vi.fn().mockRejectedValue(new ApiError(503, "")) };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("button", { name: "Export evidence" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/);
  });

  it("calls exportPatchReport and triggers a download when Export evidence is clicked", async () => {
    const user = userEvent.setup();
    const exportPatchReport = vi.fn().mockResolvedValue(new Blob(["a,b\n"]));
    api = { exportPatchReport };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("button", { name: "Export evidence" }));

    expect(exportPatchReport).toHaveBeenCalled();
  });
});

// ===========================================================================
// THE THREE STATES A MOCK NEVER HAS, on the panel that is now the page's
// primary list.
// ===========================================================================

describe("FleetPatchPage query states", () => {
  it("says the update list is loading rather than showing an empty table", async () => {
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn(() => new Promise<never>(() => {})),
    });
    renderWithProviders(<FleetPatchPage />);

    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
  });

  it("says the update request FAILED rather than showing an empty table", async () => {
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn(() => Promise.reject(new ApiError(503, ""))),
    });
    renderWithProviders(<FleetPatchPage />);

    expect((await screen.findAllByRole("alert"))[0]).toHaveTextContent(/unavailable right now/);
    expect(screen.queryByText(/Nothing is outstanding/)).not.toBeInTheDocument();
  });

  // ------------------------------------------------------------------------
  // AN EMPTY TAB HAS THREE CAUSES AND THEY LEAD OPPOSITE WAYS. "No Windows
  // machine is enrolled", "no scan has run" and "every Windows machine is up
  // to date" render as the same empty table and mean entirely different
  // things; this module has already conflated them twice, most recently as a
  // row of 0% compliance bars on a fleet nothing had ever scanned. `GET
  // /devices?os_family=…` is what separates the first from the third, and
  // `devices_scanned` separates the second from both — so all four branches
  // (including the honest one for an operator who cannot read the device list)
  // are asserted here, on the page, not only in the pure function.
  // ------------------------------------------------------------------------

  it("says the platform is genuinely clear when it is, and how many machines that is", async () => {
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage([])),
      listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([])),
      listDevices: vi.fn().mockResolvedValue(devicePage(214)),
    });
    renderWithProviders(<FleetPatchPage />);

    expect(await screen.findByText("Every Windows machine is up to date")).toBeInTheDocument();
    expect(screen.getByText(/All 214 Windows machines with an agent reported/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says the platform has no machines at all rather than calling it compliant", async () => {
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage([])),
      listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([])),
      listDevices: vi.fn().mockResolvedValue(devicePage(0)),
    });
    renderWithProviders(<FleetPatchPage />, "/patch?os=macos");

    expect(await screen.findByText("No macOS machine is enrolled")).toBeInTheDocument();
    expect(screen.getByText(/empty here is the estate/)).toBeInTheDocument();
  });

  it("says nothing has been measured when no scan has ever completed", async () => {
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage([], { devices_scanned: 0 })),
      listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([])),
      listDevices: vi.fn().mockResolvedValue(devicePage(9)),
    });
    renderWithProviders(<FleetPatchPage />);

    expect(await screen.findByText("No patch scan has completed yet")).toBeInTheDocument();
    expect(await screen.findByText(/9 Windows machines carry an agent/)).toBeInTheDocument();
    // The distinction the whole branch exists for, in the page's own words.
    expect(screen.getByText(/not because nothing is outstanding/)).toBeInTheDocument();
  });

  it("refuses to guess which empty state applies when it cannot count the fleet", async () => {
    // `devices.read` absent. The console genuinely cannot tell an up-to-date
    // Windows fleet from an organisation with no Windows in it, and saying so
    // is worse news and better information than picking the nicer reading.
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue({ ...ME, permissions: ["patch.read"] }),
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage([])),
      listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([])),
    });
    renderWithProviders(<FleetPatchPage />);

    expect(
      await screen.findByText("Nothing outstanding on any Windows machine that has reported"),
    ).toBeInTheDocument();
    expect(screen.getByText(/before reading this as an all-clear/)).toBeInTheDocument();
  });

  it("does not say the list is empty when only the severity filter emptied it", async () => {
    // "Nothing is outstanding" and "nothing matched your filter" are opposite
    // facts, and the second one is the state the default filter creates.
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi
        .fn()
        .mockResolvedValue(updatePage([], { total: 0, total_unfiltered: 300 })),
    });
    renderWithProviders(<FleetPatchPage />);

    expect(
      await screen.findByText(/300 are outstanding across the fleet/),
    ).toBeInTheDocument();
  });
});

// ===========================================================================
// THE DEVICE PANEL: one row per MACHINE, and it is what makes the severity
// default safe.
//
// The 2026-08-20 report, "it thinks there are 66 devices, it is just 1 device
// with 66 pending updates", was the same table read the other way round: its
// findings list was rendered one row per finding with the hostname repeated
// down all sixty-six of them. The count is server-side now and the row is the
// machine.
// ===========================================================================

describe("FleetPatchPage — devices are machines, not findings", () => {
  it("shows one row per device with its TOTAL outstanding count", async () => {
    mockUseApi.mockReturnValue({
      listPatchDeviceRollup: vi.fn().mockResolvedValue(
        deviceRollupPage([
          { ...DEVICE_ROLLUP, outstanding: 300, worst_severity: "important" },
        ]),
      ),
    });
    renderWithProviders(<FleetPatchPage />);

    const link = await screen.findByRole("link", { name: "sentinel" });
    const row = link.closest("tr") as HTMLElement;
    expect(within(row).getByText("300")).toBeInTheDocument();
    // Once, not once per finding.
    expect(screen.getAllByRole("link", { name: "sentinel" })).toHaveLength(1);
  });

  it("counts machines against THIS PLATFORM's fleet, not the organisation's", async () => {
    mockUseApi.mockReturnValue({
      listPatchDeviceRollup: vi
        .fn()
        .mockResolvedValue(deviceRollupPage([DEVICE_ROLLUP], { total: 1, devices_total: 600 })),
      listDevices: vi.fn().mockResolvedValue(devicePage(60)),
    });
    renderWithProviders(<FleetPatchPage />);

    // "1 device needs patches" says nothing about whether that is the whole
    // estate or a corner of it — AND the denominator has to be the tab's.
    // `devices_total` on the rollup response is `fleet_scan_coverage`'s, which
    // is org-wide with no `os_family` predicate anywhere in it: printing 600
    // under a Windows-only table would measure Windows machines against a fleet
    // that is mostly not Windows.
    expect(
      await screen.findByText(/1 of 60 Windows machines with an agent needs at least one update/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/of 600/)).not.toBeInTheDocument();
    expect(statCard("Machines needing updates")).toHaveTextContent("1");
  });

  it("is NOT narrowed by the severity filter the update list is using", async () => {
    // THE GUARANTEE THAT THE DEFAULT FILTER HIDES NO MACHINE. A device whose
    // only outstanding update is one Microsoft left unrated still shows its
    // whole count here.
    const listPatchDeviceRollup = vi.fn().mockResolvedValue(
      deviceRollupPage([
        {
          ...DEVICE_ROLLUP,
          outstanding: 292,
          worst_severity: "unknown",
          counts_by_severity: { critical: 0, important: 0, moderate: 0, low: 0, unknown: 292 },
        },
      ]),
    );
    mockUseApi.mockReturnValue({
      listPatchDeviceRollup,
      listPatchUpdates: vi
        .fn()
        .mockResolvedValue(updatePage([], { total: 0, total_unfiltered: 292 })),
    });
    renderWithProviders(<FleetPatchPage />);

    const link = await screen.findByRole("link", { name: "sentinel" });
    expect(within(link.closest("tr") as HTMLElement).getByText("292")).toBeInTheDocument();
    expect(listPatchDeviceRollup).not.toHaveBeenCalledWith(
      expect.objectContaining({ severity: expect.anything() }),
    );
  });

  it("keeps devices apart when there really are several, worst first", async () => {
    mockUseApi.mockReturnValue({
      listPatchDeviceRollup: vi.fn().mockResolvedValue(
        deviceRollupPage([
          DEVICE_ROLLUP,
          {
            ...DEVICE_ROLLUP,
            device_id: "dev-2",
            hostname: "workstation",
            outstanding: 2,
            worst_severity: "low",
          },
        ]),
      ),
    });
    renderWithProviders(<FleetPatchPage />);

    await screen.findByRole("link", { name: "sentinel" });
    expect(screen.getByRole("link", { name: "workstation" })).toBeInTheDocument();
    expect(statCard("Machines needing updates")).toHaveTextContent("2");
  });

  it("fetches ONE device's updates when its row is expanded, not the fleet's", async () => {
    const user = userEvent.setup();
    const listPatchUpdates = vi.fn().mockResolvedValue(updatePage(UPDATES));
    mockUseApi.mockReturnValue({ listPatchUpdates });
    renderWithProviders(<FleetPatchPage />);

    await screen.findByRole("link", { name: "sentinel" });
    // The (device, patch) detail still exists — one click down, per machine.
    // NAMED WITH THE MACHINE: twenty rows whose every control reads
    // "Show 41 updates" is twenty identical buttons to a screen reader.
    await user.click(
      screen.getByRole("button", { name: "Show sentinel's 41 outstanding updates" }),
    );

    await waitFor(() =>
      expect(listPatchUpdates).toHaveBeenCalledWith(
        expect.objectContaining({ device_id: "dev-1", severity: [] }),
      ),
    );
  });

  it("says a device has never been scanned rather than leaving the column blank", async () => {
    mockUseApi.mockReturnValue({
      listPatchDeviceRollup: vi
        .fn()
        .mockResolvedValue(deviceRollupPage([{ ...DEVICE_ROLLUP, last_scan_at: null }])),
    });
    renderWithProviders(<FleetPatchPage />);

    const link = await screen.findByRole("link", { name: "sentinel" });
    expect(within(link.closest("tr") as HTMLElement).getByText("Never scanned")).toBeInTheDocument();
  });
});

// ===========================================================================
// THE TILES ARE THE TABLES' OWN ARITHMETIC. The page owns the queries and
// hands the results down, so a tile and the panel under it read the same
// response object. A tile counted in the browser from a capped page is how
// this module shipped a tile/table disagreement twice.
// ===========================================================================

describe("every patch KPI tile is a link, and its number comes from the table below it", () => {
  it("renders every tile as an anchor with an href the router serves", async () => {
    mockUseApi.mockReturnValue({});
    renderWithProviders(<FleetPatchPage />);
    await screen.findByText("2023-10 Cumulative Update");

    const tiles = [...document.querySelectorAll(".stat-card")].map((card) => ({
      tag: card.tagName,
      href: card.getAttribute("href"),
    }));
    /* THE MACHINE TILE POINTS AT THE MACHINE LIST, WHICH IS ON THIS TAB.
       It used to point at `/devices`, which is a tile/table disagreement one
       level out: the tile says "1 machine needing updates" and the destination
       is every machine in the org, one of which is the one it counted. The list
       behind that number is the machines panel below, read from the same
       `GET /patch/device-rollup` the tile's figure comes from, so the fragment
       is where the number lives.

       AND THE SEVERITY TILES ARE COMPONENTS OF THE FIRST ONE, not three
       separate counts: `sum(severity_counts) == total_unfiltered` is the API's
       own contract, so "Critical" and "Important" cannot add up to more than
       "outstanding". What left this row is "Devices with critical exposure",
       which read `FleetReport.critical_device_count` — computed org-wide, with
       no `os_family` predicate anywhere in `reporting.fleet_report`, so on a
       platform tab it described a different set of machines from the list it
       linked to. */
    expect(tiles).toEqual([
      { tag: "A", href: "/patch?severity=all" },
      { tag: "A", href: "/patch?severity=critical" },
      { tag: "A", href: "/patch?severity=important" },
      { tag: "A", href: "/patch#machines-needing-updates" },
    ]);
    // ...and the fragment has somewhere to land.
    expect(document.getElementById("machines-needing-updates")).not.toBeNull();
  });

  it("prints the update totals the panel itself is paged from", async () => {
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn().mockResolvedValue(
        updatePage(UPDATES, {
          total: 8,
          total_unfiltered: 300,
          severity_counts: { critical: 2, important: 6, unknown: 292 },
        }),
      ),
    });
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    // Outstanding = the unfiltered total; the two severity tiles are BUCKETS OF
    // THAT HISTOGRAM rather than counts of their own, which is what makes them
    // incapable of disagreeing with it or with the table.
    expect(statCard("Updates outstanding")).toHaveTextContent("300");
    expect(statCard("Critical")).toHaveTextContent("2");
    expect(statCard("Important")).toHaveTextContent("6");
  });

  it("lands the severity tile on exactly the updates it counted", async () => {
    const user = userEvent.setup();
    const listPatchUpdates = vi.fn().mockResolvedValue(updatePage(UPDATES));
    mockUseApi.mockReturnValue({ listPatchUpdates });
    renderWithProviders(<FleetPatchPage />, "/patch?severity=all");

    await screen.findByText("2023-10 Cumulative Update");
    expect(listPatchUpdates).toHaveBeenCalledWith(expect.objectContaining({ severity: [] }));

    // A filter a tile can set and nothing on screen can undo is a trap door.
    const select = screen.getByRole("combobox", { name: "Filter Windows updates by severity" });
    expect(select).toHaveValue("all");
    await user.selectOptions(select, "critical,important");
    await waitFor(() =>
      expect(listPatchUpdates).toHaveBeenLastCalledWith(
        expect.objectContaining({ severity: ["critical", "important"] }),
      ),
    );
  });

  it("makes only the tile that needs a person loud, and colours by category", async () => {
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn().mockResolvedValue(
        updatePage(UPDATES, {
          total: 3,
          total_unfiltered: 3,
          severity_counts: { critical: 2, important: 1 },
        }),
      ),
    });
    renderWithProviders(<FleetPatchPage />);
    await screen.findByText("2023-10 Cumulative Update");

    expect(statCard("Critical")).toHaveClass("urgent");
    expect(statCard("Critical")).toHaveClass("tone-red");
    expect(statCard("Updates outstanding")).not.toHaveClass("urgent");
    expect(statCard("Machines needing updates")).toHaveClass("tone-blue");
  });
});

// ===========================================================================
// "NEVER SCANNED" IS MEASURED NOW, NOT INFERRED.
//
// [LOW] A68: "Four stat cards all read 0 with no indication that the module has
// never run ... '0 critical exposure' reads as an excellent security posture
// when the truth is 'we have never scanned anything, so we do not know'." The
// page used to infer this from four empty collections and admitted in a comment
// that it could only claim "no scan result has ever reached this console".
// `GET /patch/updates` carries `devices_scanned`, read off completed
// `patch.scan` jobs, so the page says the thing it means.
// ===========================================================================

describe("FleetPatchPage says 'never scanned' rather than reporting an all-clear", () => {
  it("shows an unknown state, not four confident zeros, before any scan result", async () => {
    mockUseApi.mockReturnValue(neverScannedApi());
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("No patch scan has completed yet");
    expect(statValues()).toEqual(["—", "—", "—", "—"]);
    expect(screen.getAllByText("Never scanned")).toHaveLength(4);
  });

  it("prints real counts the moment a scan result exists", async () => {
    // The other half of the same rule: 0 is a fact when something HAS
    // reported, and dashing it out would be its own lie.
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage([])),
      listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([])),
    });
    renderWithProviders(<FleetPatchPage />);

    // THE TILES SURVIVE AN EMPTY TAB, and that is the point of the rule: a zero
    // is a measurement, and dashing it out or deleting the row would destroy
    // the very distinction "Never scanned" exists to draw.
    await screen.findByText("Every Windows machine is up to date");
    expect(statValues()).toEqual(["0", "0", "0", "0"]);
    expect(screen.queryByText("Never scanned")).not.toBeInTheDocument();
  });

  it("does not claim the fleet is unexposed when nothing has ever reported", async () => {
    mockUseApi.mockReturnValue(neverScannedApi());
    renderWithProviders(<FleetPatchPage />);

    expect(
      await screen.findByText(/not because nothing is outstanding/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Every Windows machine is up to date"),
    ).not.toBeInTheDocument();
  });

  it("says a measured zero is measured, in words, on the tile", async () => {
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage([])),
      listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([])),
    });
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("Every Windows machine is up to date");
    const measured = statCard("Critical");
    expect(measured).toHaveTextContent("No outstanding update is rated critical");
    // An alarm-coloured 0 reads as a perfect score, so the urgent treatment
    // comes off a zero whatever the page asked for.
    expect(measured).not.toHaveClass("urgent");
  });
});

// --------------------------------------------------------------------------

describe("PatchPolicyEditor", () => {
  function baseApi(listPatchPolicies: ApiClient["listPatchPolicies"]): Partial<ApiClient> {
    return { me: vi.fn().mockResolvedValue(ME), listPatchPolicies };
  }

  it("says it is loading rather than showing an empty policy list", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => new Promise<never>(() => {}))) as ApiClient);
    renderWithProviders(<PatchPolicyEditor />);

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty policy list", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.reject(new Error("boom")))) as ApiClient);
    renderWithProviders(<PatchPolicyEditor />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No patch policies configured.")).not.toBeInTheDocument();
  });

  it("says there are no policies when there are genuinely none", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.resolve([]))) as ApiClient);
    renderWithProviders(<PatchPolicyEditor />);

    expect(await screen.findByText("No patch policies configured.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists policies from the mocked api", async () => {
    mockUseApi.mockReturnValue(baseApi(vi.fn(() => Promise.resolve([POLICY]))) as ApiClient);
    renderWithProviders(<PatchPolicyEditor />);

    expect(await screen.findByText("broad")).toBeInTheDocument();
  });
});

describe("patch state vocabulary", () => {
  it("gives red to critical alone, so the two severities the filter separates look different", () => {
    // The default filter is "critical & important" and both rendered as the
    // same red pill, in three tables. DESIGN.md, The Rationed Hue Rule: a hue
    // covering half the ramp is not carrying information. Every other module
    // already ramps destructive -> warning -> info -> muted.
    expect(severityVariant("critical")).toBe("destructive");
    expect(severityVariant("important")).toBe("warning");
    expect(severityVariant("moderate")).toBe("info");
    expect(severityVariant("low")).toBe("muted");
    // Windows Update leaves most cumulative updates unrated, so this is the
    // commonest value on a Windows fleet, and it means "nobody said".
    expect(severityVariant("unknown")).toBe("muted");
  });

  it("maps the real DevicePatchStatus.state values to non-default badge variants", () => {
    expect(patchStateVariant("installed")).toBe("success");
    expect(patchStateVariant("failed")).toBe("destructive");
    expect(patchStateVariant("approved")).toBe("info");
    expect(patchStateVariant("missing")).toBe("warning");
    expect(patchStateVariant("not_applicable")).toBe("muted");
    // Invented vocabulary from the old map must no longer resolve specially.
    expect(patchStateVariant("applied")).toBe("muted");
    expect(patchStateVariant("reboot_pending")).toBe("muted");
  });

  it("renders DevicePatchTab's 'By state' badges using the real state vocabulary", async () => {
    const api: Partial<ApiClient> = {
      getDevicePatchStatus: vi.fn().mockResolvedValue({
        device_id: "d1",
        counts_by_state: { installed: 3, not_applicable: 2 },
        counts_by_severity: {},
        pending_reboot: false,
        last_scan_at: "2026-01-02T00:00:00.000Z",
      } satisfies DeviceStatus),
    };
    mockUseApi.mockReturnValue(api as ApiClient);

    renderWithProviders(<DevicePatchTab deviceId="d1" />);

    const installedBadge = await screen.findByText("Installed");
    // THE FINDING, Auditor A [MEDIUM]: the raw enum was the badge's label.
    const notApplicableBadge = await screen.findByText("Not applicable");
    expect(screen.queryByText("not_applicable")).not.toBeInTheDocument();
    // The design's severity kinds, not Tailwind palette names: `Badge` renders
    // a v3 `.pill.p-<kind>` since the v3 port (2026-09-07; it was
    // `.status.<tone>` from 2026-08-16). The assertion is the same contract --
    // installed reads as ok, not-applicable as muted -- expressed against the
    // vocabulary the console actually uses.
    expect(installedBadge.className).toContain("p-ok"); // success variant
    expect(notApplicableBadge.className).toContain("p-mut"); // muted variant
  });
});

// THE BLOCKER, Auditor A, /patch/policies: "'New policy' is completely
// non-functional — patch policies cannot be created at all ... Clicking New
// policy does nothing whatsoever ... `inputs: 0`, `dialogs: 0`, no navigation,
// no POST ... `<main>` text is byte-identical before and after."
//
// The editor existed; it rendered as a card BELOW the table and below the
// empty state, so a click that did open it was indistinguishable from a click
// that did nothing. It is a dialog now — the finding's own fix ("wire the
// button to the policy-creation form/modal") — inside a real `<form>`, which
// also ends the `type="submit"`-outside-a-form pattern the audit called out on
// this very button.
// ==========================================================================

const MANAGER: Me = { ...ME, permissions: [...ME.permissions, "patch.policy.manage"] };

function policyApi(policies: PatchPolicy[], upsert = vi.fn()): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(MANAGER),
    listPatchPolicies: vi.fn().mockResolvedValue(policies),
    upsertPatchPolicy: upsert,
  };
}

async function openPolicyDialog(policies: PatchPolicy[], upsert = vi.fn()) {
  const user = userEvent.setup();
  mockUseApi.mockReturnValue(policyApi(policies, upsert) as ApiClient);
  renderWithProviders(<PatchPolicyEditor />);
  await screen.findByRole("button", { name: "New policy" });
  await user.click(screen.getByRole("button", { name: "New policy" }));
  const dialog = await screen.findByRole("dialog", { name: "New patch policy" });
  return { user, dialog, upsert };
}

describe("PatchPolicyEditor — New policy actually opens something", () => {
  it("opens a dialog with form controls in it", async () => {
    // The audit's own probes: `[role=dialog], dialog` → 0, and
    // `document.querySelectorAll('input,select,textarea').length` → 0.
    const { dialog } = await openPolicyDialog([]);
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Ring name")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Reboot")).toBeInTheDocument();
  });

  it("creates a policy from the dialog, which was unreachable before", async () => {
    const upsert = vi.fn().mockResolvedValue({ ...POLICY, ring_name: "pilot" });
    const { user, dialog } = await openPolicyDialog([], upsert);

    await user.clear(within(dialog).getByLabelText("Ring name"));
    await user.type(within(dialog).getByLabelText("Ring name"), "pilot");
    await user.clear(within(dialog).getByLabelText("Ring order"));
    await user.type(within(dialog).getByLabelText("Ring order"), "10");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(upsert).toHaveBeenCalledWith({
      device_group_id: null,
      ring_name: "pilot",
      ring_order: 10,
      maintenance_window: null,
      auto_approve_security: false,
      reboot: "if_required",
      reboot_deferral_hours: 24,
      scan_interval_hours: 24,
    });
  });

  it("submits on Enter, which a submit button outside a form could not do", async () => {
    const upsert = vi.fn().mockResolvedValue(POLICY);
    const { user, dialog } = await openPolicyDialog([], upsert);

    await user.type(within(dialog).getByLabelText("Ring name"), "{Enter}");

    expect(upsert).toHaveBeenCalled();
  });

  it("hides the whole editor from a caller without patch.policy.manage", async () => {
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listPatchPolicies: vi.fn().mockResolvedValue([]),
    } as unknown as ApiClient);
    renderWithProviders(<PatchPolicyEditor />);

    await screen.findByText("No patch policies configured.");
    expect(screen.queryByRole("button", { name: "New policy" })).not.toBeInTheDocument();
  });
});

describe("PatchPolicyEditor — what the form refuses to send", () => {
  it("names an empty ring name instead of PUTting one", async () => {
    const { user, dialog, upsert } = await openPolicyDialog([]);
    await user.clear(within(dialog).getByLabelText("Ring name"));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(upsert).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Ring name is required");
  });

  it("refuses a cleared scan interval rather than sending 0", async () => {
    // `Number("")` is 0, and the old form fed every keystroke through it: a
    // half-typed interval became "scan continuously", stored server-side.
    const { user, dialog, upsert } = await openPolicyDialog([]);
    await user.clear(within(dialog).getByLabelText("Scan interval (hours)"));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(upsert).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Scan interval (hours) is required",
    );
  });

  it("refuses a scan interval of zero, which the scheduler reads as continuous", async () => {
    const { user, dialog, upsert } = await openPolicyDialog([]);
    await user.clear(within(dialog).getByLabelText("Scan interval (hours)"));
    await user.type(within(dialog).getByLabelText("Scan interval (hours)"), "0");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(upsert).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("must be between 1 and 8760");
  });

  it("refuses the maintenance window this very field used to suggest", async () => {
    // The placeholder read `e.g. Sat 02:00-04:00`. `window_open` calls `int()`
    // on every cron term, so that value raises inside the apply path and the
    // policy gates nothing — a field whose own example is invalid.
    // STRUCTURE: the window is painted on `WeekPainter`'s grid now, and the
    // raw expression lives in its Advanced field, named "Cron expression".
    // The grid cannot draw this value, so the field is where it is typed.
    const { user, dialog, upsert } = await openPolicyDialog([]);
    await user.type(within(dialog).getByLabelText("Cron expression"), "Sat 02:00-04:00");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(upsert).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/5-field cron/);
  });

  it("accepts a window the matcher can parse, and states the zone", async () => {
    const upsert = vi.fn().mockResolvedValue(POLICY);
    const { user, dialog } = await openPolicyDialog([], upsert);

    // STRUCTURE: the caption is the painter's, the zone is still on it, and
    // the expression is typed into the painter's Advanced field.
    expect(within(dialog).getByText(/Maintenance window \(UTC, optional\)/)).toBeInTheDocument();
    expect(within(dialog).getByRole("grid", { name: "Maintenance window" })).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Cron expression"), "0 2 * * 6");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(upsert).toHaveBeenCalledWith(
      expect.objectContaining({ maintenance_window: "0 2 * * 6" }),
    );
  });

  it("renders a sentence for a bodyless 503, not the empty `String(error)`", async () => {
    const upsert = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const { user, dialog } = await openPolicyDialog([], upsert);
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/unavailable right now/);
  });
});

describe("PatchPolicyEditor — 'New' replaces the org default, and says so", () => {
  // `PUT /patch/policies` UPSERTS on `device_group_id` (patch/api.py:408-425).
  // "New policy" left on "Every device" with a default already present does
  // not add a second ring — it overwrites the policy governing every device.

  it("asks before replacing an existing organisation-wide policy", async () => {
    const upsert = vi.fn().mockResolvedValue(POLICY);
    const { user, dialog } = await openPolicyDialog([POLICY], upsert);
    await user.clear(within(dialog).getByLabelText("Ring name"));
    await user.type(within(dialog).getByLabelText("Ring name"), "pilot");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(upsert).not.toHaveBeenCalled();
    const confirm = await screen.findByRole("dialog", {
      name: "Replace the organisation-wide patch policy",
    });
    // The subject is the ring about to be replaced, not the one being typed.
    expect(confirm).toHaveTextContent("broad");
    await user.click(within(confirm).getByRole("button", { name: "Replace policy" }));

    expect(upsert).toHaveBeenCalledWith(expect.objectContaining({ ring_name: "pilot" }));
  });

  it("changes nothing when the replacement is cancelled", async () => {
    const upsert = vi.fn();
    const { user, dialog } = await openPolicyDialog([POLICY], upsert);
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    const confirm = await screen.findByRole("dialog", {
      name: "Replace the organisation-wide patch policy",
    });
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(upsert).not.toHaveBeenCalled();
  });

  it("does not ask when there is nothing to replace", async () => {
    const upsert = vi.fn().mockResolvedValue(POLICY);
    const { user, dialog } = await openPolicyDialog([], upsert);
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(upsert).toHaveBeenCalled();
    expect(
      screen.queryByRole("dialog", { name: "Replace the organisation-wide patch policy" }),
    ).not.toBeInTheDocument();
  });

  it("does not ask when EDITING that same policy — that is not a surprise", async () => {
    const upsert = vi.fn().mockResolvedValue(POLICY);
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(policyApi([POLICY], upsert) as ApiClient);
    renderWithProviders(<PatchPolicyEditor />);

    await screen.findByText("broad");
    await user.click(screen.getByRole("button", { name: /^Edit the / }));
    const dialog = await screen.findByRole("dialog", { name: "Edit patch policy" });
    // Prefilled from the row, as strings — including the stored window, which
    // in this fixture is the unparseable value the old placeholder taught.
    expect(within(dialog).getByLabelText("Ring order")).toHaveValue(100);
    // STRUCTURE: the stored expression is in the painter's Advanced field.
    await user.clear(within(dialog).getByLabelText("Cron expression"));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(upsert).toHaveBeenCalledWith(expect.objectContaining({ ring_name: "broad" }));
  });

  it("says a policy created here applies to the whole organisation", async () => {
    // With "Every device" chosen (the default) the policy is the org-wide
    // one. An operator who thinks they are adding a `pilot` ring alongside
    // `broad` has to learn that before saving, not from an unchanged table
    // after.
    const { dialog } = await openPolicyDialog([POLICY]);
    expect(within(dialog).getByText(/Applies to every device in the organisation/)).toBeInTheDocument();
  });
});

describe("FleetPatchPage — the approval confirmation can be declined", () => {
  it("approves nothing when the confirmation is cancelled", async () => {
    const approve = vi.fn();
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listPatchApprovals: vi.fn().mockResolvedValue(NO_APPROVALS),
          getPatchFleetReport: vi.fn().mockResolvedValue(FLEET_REPORT),
      bulkApprovePatches: approve,
      listPatchRings: vi.fn().mockResolvedValue(RINGS),
      triggerPatchScan: vi.fn(),
      exportPatchReport: vi.fn(),
    } as unknown as ApiClient);
    const user = userEvent.setup();
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select KB5031354" }));
    await user.click(screen.getByRole("button", { name: /^Approve 1 update/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    // The dialog used to end "Approvals cannot be revoked from this console",
    // which was true only while no route listed them. Saying it now would
    // send an operator looking for a control that is on this very page.
    expect(confirm).not.toHaveTextContent(/cannot be revoked/);
    expect(confirm).toHaveTextContent(/Approvals list/);
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(approve).not.toHaveBeenCalled();
  });
});

describe("patch states are labelled for people, and the Policies action is one control", () => {
  it("maps every backend state to a label with no underscore in it", () => {
    // Auditor A, [MEDIUM]: "exposes the raw enum value `not_applicable` as a
    // user-facing badge label".
    expect(patchStateLabel("not_applicable")).toBe("Not applicable");
    expect(patchStateLabel("installed")).toBe("Installed");
    expect(patchStateLabel("missing")).toBe("Missing");
  });

  it("shows an unknown state rather than hiding it", () => {
    // A state the server started writing that this table has not learned is
    // information; "—" would make a new backend state invisible here.
    expect(patchStateLabel("quarantined")).toBe("quarantined");
  });

  it("renders Policies as a single link, not a button nested in an anchor", async () => {
    // Auditor A, [MEDIUM]: `<a href="/patch/policies"><button>Policies</button></a>`
    // -- invalid HTML, two overlapping controls for one target, and
    // middle-click / Ctrl-click / "copy link address" all unreliable.
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue({ ...ME, permissions: [...ME.permissions, "patch.policy.manage"] }),
      listPatchApprovals: vi.fn().mockResolvedValue(NO_APPROVALS),
          getPatchFleetReport: vi.fn().mockResolvedValue(FLEET_REPORT),
      bulkApprovePatches: vi.fn(),
      listPatchRings: vi.fn().mockResolvedValue(RINGS),
      triggerPatchScan: vi.fn(),
      exportPatchReport: vi.fn(),
    } as unknown as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    const link = await screen.findByRole("link", { name: "Policies" });
    expect(link).toHaveAttribute("href", "/patch/policies");
    expect(link.querySelector("button")).toBeNull();
    expect(screen.queryByRole("button", { name: "Policies" })).not.toBeInTheDocument();
  });
});

describe("DevicePatchTab reports only what the endpoint knows", () => {
  function withStatus(pendingReboot: boolean) {
    mockUseApi.mockReturnValue({
      getDevicePatchStatus: vi.fn().mockResolvedValue({
        device_id: "d1",
        counts_by_state: {},
        counts_by_severity: {},
        pending_reboot: pendingReboot,
        last_scan_at: "2026-01-02T00:00:00.000Z",
      } satisfies DeviceStatus),
    } as unknown as ApiClient);
    renderWithProviders(<DevicePatchTab deviceId="d1" />);
  }

  it("does not claim a device is up to date when all it knows is that no reboot is pending", async () => {
    // `pending_reboot: false` means no INSTALLED patch has asked for a
    // restart. It says nothing about the patches still missing -- and this
    // card sat beside a "By state" list that may read "missing: 66".
    withStatus(false);

    expect(await screen.findByText("No restart pending")).toBeInTheDocument();
    expect(screen.queryByText("Up to date")).not.toBeInTheDocument();
  });

  it("still says so when a reboot IS pending", async () => {
    withStatus(true);
    expect(await screen.findByText("Restart pending")).toBeInTheDocument();
  });
});

/* THE TWO COMPLIANCE PANELS ARE ONE PANEL NOW.

   The operator, 2026-08-27: "two tiles on the bottom that shouldn't be there
   and don't look good and don't have the same theme of the other tiles". They
   were two `<Panel>`s of bare `<dl>` rows at the foot of the page, which is
   what a stat tile looks like once its treatment has been lost. The numbers
   survived — nothing above the table says "Linux is at 71% while Windows is at
   94%" — in one panel, as `Progress` bars, which is what a percentage is
   everywhere else in this console.

   THE AUDIT FINDING THESE TESTS WERE WRITTEN FOR SURVIVES WITH THEM. Auditor
   A's complaint was that two panels shared one glyph and one sentence and
   neither said WHY the data was absent. Collapsing them to one panel collapses
   the duplication the finding was about; what is still asserted is the part
   that mattered — the cause is named, the way out is named, the person who can
   take it is named, and the glyph is not the Devices module's monitor. */
describe("the compliance panel explains its own emptiness", () => {
  it("names the cause rather than reporting an absence", async () => {
    mockUseApi.mockReturnValue(neverScannedApi() as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    expect(await screen.findByText("No compliance percentages yet")).toBeInTheDocument();
    // The sentence the audit found in both cards, verbatim.
    expect(screen.queryByText("No compliance data yet.")).not.toBeInTheDocument();
    // ...and it is ONE empty state now, not the same words twice.
    expect(screen.getAllByText("No compliance percentages yet")).toHaveLength(1);
  });

  it("names the cause and the control that fixes it", async () => {
    mockUseApi.mockReturnValue(neverScannedApi() as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    const cause = await screen.findByText(/Run Scan at the top of this page/);
    expect(cause).toHaveTextContent(/only once a scan has matched the update catalog/);
  });

  it("says who can start a scan when the reader cannot", async () => {
    mockUseApi.mockReturnValue(
      neverScannedApi({
        me: vi.fn().mockResolvedValue({ ...ME, permissions: ["patch.read"] }),
      }) as ApiClient,
    );
    renderWithProviders(<FleetPatchPage />);

    expect(
      // The compliance panel's sentence, not the device table's shorter one
      // beside it: both may be on screen once every query has settled.
      await screen.findByText(/No scan has reported yet\. Someone with permission to scan for updates can start one/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Run Scan at the top of this page/)).not.toBeInTheDocument();
  });

  it("drops the monitor glyph the Devices module uses", async () => {
    // The finding is the ICON as much as the copy: `QueryState`'s empty branch
    // defaults to `emptyIcon="devices"`, so both percentage charts rendered
    // the display glyph.
    mockUseApi.mockReturnValue(neverScannedApi() as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    const heading = await screen.findByText("No compliance percentages yet");
    const card = heading.closest(".empty") as HTMLElement;
    const paths = [...card.querySelectorAll("svg *")].map((node) => node.getAttribute("d"));
    // `chart`'s bars (Tabler `chart-bar`: three bars on a baseline), not
    // `devices`' monitor body (Tabler `devices`: two screens with a `M16 9h2`
    // stand; the old hand-drawn glyph was a `rect` + stand).
    expect(paths).toContain("M4 20h14");
    expect(paths).toContain(
      "M3 13a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v6a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1l0 -6",
    );
    expect(paths).not.toContain("M16 9h2");
    expect(card.querySelector("rect")).toBeNull();
  });

  it("still renders the percentages when there are some", async () => {
    mockUseApi.mockReturnValue(
      neverScannedApi({
        getPatchFleetReport: vi.fn().mockResolvedValue(FLEET_REPORT),
      }) as ApiClient,
    );
    renderWithProviders(<FleetPatchPage />);

    expect(await screen.findByText("82.5%")).toBeInTheDocument();
    expect(screen.getByText("91.0%")).toBeInTheDocument();
    expect(screen.queryByText("No compliance percentages yet")).not.toBeInTheDocument();
    // A PERCENTAGE IS A BAR IN THIS CONSOLE, and the bar is what carries the
    // comparison a split by platform exists to make. It announces its own
    // value, so the figure beside it is not the only channel.
    expect(
      screen.getByRole("progressbar", { name: /windows compliance/i }),
    ).toHaveAttribute("aria-valuetext", "83%");
  });

  it("says the report is loading rather than that no scan has run", async () => {
    // "Never scanned" during an in-flight request is a third state reported as
    // one of the other two.
    mockUseApi.mockReturnValue(
      neverScannedApi({
        getPatchFleetReport: vi.fn(() => new Promise<never>(() => {})),
      }) as ApiClient,
    );
    renderWithProviders(<FleetPatchPage />);

    expect(await screen.findAllByText("Loading…")).not.toHaveLength(0);
    expect(screen.queryByText("No compliance percentages yet")).not.toBeInTheDocument();
    expect(screen.queryByText("Never scanned")).not.toBeInTheDocument();
  });

  it("says the report FAILED rather than that no scan has run", async () => {
    mockUseApi.mockReturnValue(
      neverScannedApi({
        getPatchFleetReport: vi.fn(() => Promise.reject(new ApiError(503, ""))),
      }) as ApiClient,
    );
    renderWithProviders(<FleetPatchPage />);

    expect((await screen.findAllByRole("alert"))[0]).toHaveTextContent(/unavailable right now/);
    expect(screen.queryByText("No compliance percentages yet")).not.toBeInTheDocument();
  });
});

describe("Scan cannot be dispatched twice while the first request is in flight", () => {
  it("disables the control until the server answers", async () => {
    // A65's evidence: `patch.scan / 11:04:14 AM / queued` and `patch.scan /
    // 11:04:18 AM / queued` — four seconds is a click, nothing to read, and a
    // second click. The status message is the cure; this is the guard.
    const user = userEvent.setup();
    let release: ((value: { job_ids: string[] }) => void) | undefined;
    const triggerPatchScan = vi.fn(
      () => new Promise<{ job_ids: string[] }>((resolve) => (release = resolve)),
    );
    mockUseApi.mockReturnValue(neverScannedApi({ triggerPatchScan }) as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("No patch scan has completed yet");
    await user.click(screen.getByRole("button", { name: "Scan" }));

    const button = await screen.findByRole("button", { name: "Scan…" });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(triggerPatchScan).toHaveBeenCalledTimes(1);

    release?.({ job_ids: ["j1"] });
    expect(await screen.findByRole("status")).toHaveTextContent("Scan queued — 1 job dispatched.");
  });
});

// --------------------------------------------------------------------------
// The patch policy dialog is the same shared `Dialog` Auditor A measured on
// /deploy: [HIGH] "no Escape handler — Escape does not close it" and [MEDIUM]
// "does not lock background scrolling ... `getComputedStyle(document.body)
// .overflow` is `auto`".
// --------------------------------------------------------------------------

describe("PatchPolicyEditor's dialog can be left with the keyboard", () => {
  it("closes on Escape", async () => {
    const { user } = await openPolicyDialog([]);
    await user.keyboard("{Escape}");

    expect(
      screen.queryByRole("dialog", { name: "New patch policy" }),
    ).not.toBeInTheDocument();
  });

  it("locks the page behind it, and gives the scrollbar back on close", async () => {
    const { user } = await openPolicyDialog([]);
    expect(document.body.style.overflow).toBe("hidden");

    await user.keyboard("{Escape}");
    expect(document.body.style.overflow).toBe("");
  });

  it("leaves the half-typed policy alone when Escape answers the overwrite confirmation", async () => {
    // `ConfirmDialog` has its own document-level Escape handler and does not
    // stop propagation. Without the guard, one keypress cancels the
    // confirmation AND discards the form behind it.
    const { user, dialog } = await openPolicyDialog([POLICY]);
    await user.clear(within(dialog).getByLabelText("Ring name"));
    await user.type(within(dialog).getByLabelText("Ring name"), "pilot");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await screen.findByRole("dialog", { name: "Replace the organisation-wide patch policy" });

    await user.keyboard("{Escape}");

    expect(
      screen.queryByRole("dialog", { name: "Replace the organisation-wide patch policy" }),
    ).not.toBeInTheDocument();
    const stillOpen = screen.getByRole("dialog", { name: "New patch policy" });
    expect(within(stillOpen).getByLabelText("Ring name")).toHaveValue("pilot");
  });
});


// ---------------------------------------------------------------------------
// THE APPROVALS LIST, and the revoke it exists to make reachable.
//
// `DELETE /api/v1/patch/approvals/{approval_id}` shipped with the module and
// had never once been callable from here: an approval id was returned by
// exactly one thing, the POST that created it, so the moment that response
// scrolled away the clearance for a patch to reach a ring was permanent.
// `GET /patch/approvals` (commit 15ed5de) is what closes that, and a list
// without a revoke control on it would close nothing.
//
// A REVOKED APPROVAL STAYS IN THE LIST. The backend soft-revokes so the trail
// holds; a console that filtered them out would disagree with the audit log
// about what was ever approved. The test below is what stops a future "tidy"
// filter from being added without somebody arguing for it.
// ---------------------------------------------------------------------------

describe("FleetPatchPage approvals", () => {
  function approvalsApi(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(ME),
          listPatchApprovals: vi
        .fn()
        .mockResolvedValue({ items: [APPROVAL, REVOKED_APPROVAL], total: 2 }),
      revokePatchApproval: vi.fn().mockResolvedValue(undefined),
      getPatchFleetReport: vi.fn().mockResolvedValue(FLEET_REPORT),
      bulkApprovePatches: vi.fn(),
      listPatchRings: vi.fn().mockResolvedValue(RINGS),
      triggerPatchScan: vi.fn(),
      exportPatchReport: vi.fn(),
      ...overrides,
    };
  }

  /**
   * The approvals table's row for one approval, by the id its Revoke control
   * would send.
   *
   * BY THAT ATTRIBUTE AND NOT BY ITS TEXT: every ring name in this table is
   * also an `<option>` in the catalog's ring picker further down the page, so
   * `getByText("pilot")` matches two nodes and says nothing about which of
   * them is a row.
   */
  function approvalRow(approvalId: string): HTMLElement {
    const row = document.querySelector(`[data-approval-id="${approvalId}"]`);
    if (!row) throw new Error(`no approvals row for ${approvalId}`);
    return row as HTMLElement;
  }

  const findApprovalRow = (approvalId: string): Promise<HTMLElement> =>
    waitFor(() => approvalRow(approvalId));

  it("lists the approvals the POST response used to be the only record of", async () => {
    mockUseApi.mockReturnValue(approvalsApi() as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    const row = await findApprovalRow("a1");
    // The ring an approval covers, on the row — this is the whole fact the
    // list carries, and it was previously visible only in the response to
    // the POST that created it.
    expect(within(row).getByText("pilot")).toBeInTheDocument();
    // The patch is named, not left as the uuid the API keys it by. The name
    // comes off the approval row itself now (`patch_identifier`); it used to
    // come from a 500-row catalogue page fetched on every visit purely to
    // build the lookup, which was both the cost and, past the cap, wrong.
    expect(within(row).getByText("KB5031354")).toBeInTheDocument();
  });

  it("names the patch WITHOUT fetching the catalogue", async () => {
    const api = approvalsApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    const row = await findApprovalRow("a1");
    expect(within(row).getByText("KB5031354")).toBeInTheDocument();
    // THE WHOLE POINT. `GET /patch/patches` was requested on every visit to
    // this page for one thing: an id -> identifier map for the handful of
    // rows in this panel. `patch_identifier` on the approval deletes it.
    //
    // THE GUARD USED TO BE `expect(api.listPatches).not.toHaveBeenCalled()`
    // AND CANNOT BE ANY MORE, because `ApiClient.listPatches` no longer
    // exists -- a client method kept alive so a test could assert nobody
    // called it was the orphan, not the guard. It still falsifies, in two
    // places at once: the api object this page is handed carries NO catalogue
    // method, so a page that reached for one throws here and this test goes
    // red, and `tsc` refuses the call before the test ever runs.
    expect("listPatches" in api).toBe(false);
  });

  it("falls back to the uuid when the API could not name the patch", async () => {
    // `patch_identifier: null` means the org has no `patches` row for that id
    // -- the cross-tenant anomaly `UnknownPatchError` guards, surfaced rather
    // than hidden. The row must still render, and still offer its Revoke:
    // the id is the only handle anybody has on it.
    const api = approvalsApi({
      listPatchApprovals: vi.fn().mockResolvedValue({
        items: [{ ...APPROVAL, patch_identifier: null }],
        total: 1,
      }),
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    const row = await findApprovalRow("a1");
    expect(within(row).getByText("p1")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /more actions/i })).toBeInTheDocument();
  });

  it("shows a revoked approval as revoked instead of hiding it", async () => {
    const api = approvalsApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    await findApprovalRow("a1");
    const revoked = approvalRow("a2");
    expect(within(revoked).getByText("Revoked")).toBeInTheDocument();
    // And it offers no second revoke: the row is a record, not a live gate.
    expect(within(revoked).queryByRole("button", { name: /revoke|more actions/i })).not.toBeInTheDocument();
    // The default query asks for everything, revoked included — passing
    // `revoked: false` here would be the console disagreeing with the log.
    expect(api.listPatchApprovals).toHaveBeenCalledWith(
      expect.not.objectContaining({ revoked: expect.anything() }),
    );
  });

  it("hides the Revoke control from a caller without patch.approve", async () => {
    mockUseApi.mockReturnValue(
      approvalsApi({
        me: vi.fn().mockResolvedValue({ ...ME, permissions: ["patch.read", "patch.scan"] }),
      }) as ApiClient,
    );
    renderWithProviders(<FleetPatchPage />);

    // The list itself is `patch.read` and stays: seeing which patches are
    // cleared for which ring is read work.
    await findApprovalRow("a1");
    expect(screen.queryByRole("button", { name: /revoke|more actions/i })).not.toBeInTheDocument();
  });

  it("revokes nothing on the first click, and names the approval in the confirmation", async () => {
    const api = approvalsApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    const user = userEvent.setup();
    renderWithProviders(<FleetPatchPage />);

    await findApprovalRow("a1");
    await user.click(within(approvalRow("a1")).getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Revoke" }));

    expect(api.revokePatchApproval).not.toHaveBeenCalled();
    const confirm = await screen.findByRole("dialog", { name: "Revoke patch approval" });
    // The mistake a confirmation exists to catch is having the wrong row, and
    // on this page the ring is half of the identity.
    expect(confirm).toHaveTextContent("KB5031354 → pilot ring");
  });

  it("revokes nothing when the confirmation is cancelled", async () => {
    const api = approvalsApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    const user = userEvent.setup();
    renderWithProviders(<FleetPatchPage />);

    await findApprovalRow("a1");
    await user.click(within(approvalRow("a1")).getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Revoke" }));
    const confirm = await screen.findByRole("dialog", { name: "Revoke patch approval" });
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(api.revokePatchApproval).not.toHaveBeenCalled();
  });

  it("sends the approval's own id once the revoke is confirmed", async () => {
    const api = approvalsApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    const user = userEvent.setup();
    renderWithProviders(<FleetPatchPage />);

    await findApprovalRow("a1");
    await user.click(within(approvalRow("a1")).getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Revoke" }));
    const confirm = await screen.findByRole("dialog", { name: "Revoke patch approval" });
    await user.click(within(confirm).getByRole("button", { name: "Revoke approval" }));

    // THE WHOLE POINT OF THE LIST. This id existed nowhere a console could
    // reach before `GET /patch/approvals`.
    expect(api.revokePatchApproval).toHaveBeenCalledExactlyOnceWith("a1");
  });

  it("shows the approval as revoked afterwards without a manual refresh", async () => {
    const list = vi
      .fn()
      .mockResolvedValueOnce({ items: [APPROVAL], total: 1 })
      .mockResolvedValue({
        items: [{ ...APPROVAL, revoked: true, revoked_by: "u1", revoked_at: "2026-01-04T00:00:00.000Z" }],
        total: 1,
      });
    const api = approvalsApi({ listPatchApprovals: list });
    mockUseApi.mockReturnValue(api as ApiClient);
    const user = userEvent.setup();
    renderWithProviders(<FleetPatchPage />);

    await findApprovalRow("a1");
    expect(within(approvalRow("a1")).getByText("In force")).toBeInTheDocument();

    await user.click(within(approvalRow("a1")).getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Revoke" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Revoke patch approval" })).getByRole(
        "button",
        { name: "Revoke approval" },
      ),
    );

    // From the invalidated list, not from optimistic local state: a page that
    // did not refetch would still be showing "In force" on a withdrawn gate.
    expect(await within(approvalRow("a1")).findByText("Revoked")).toBeInTheDocument();
  });

  it("routes a refused revoke through the console's error sentence, never the raw body", async () => {
    const api = approvalsApi({
      revokePatchApproval: vi
        .fn()
        .mockRejectedValue(new ApiError(403, '{"detail":{"code":"forbidden"}}')),
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    const user = userEvent.setup();
    renderWithProviders(<FleetPatchPage />);

    await findApprovalRow("a1");
    await user.click(within(approvalRow("a1")).getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Revoke" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Revoke patch approval" })).getByRole(
        "button",
        { name: "Revoke approval" },
      ),
    );

    expect(await screen.findByText(/You do not have permission to do that/i)).toBeInTheDocument();
    expect(screen.queryByText(/forbidden/)).not.toBeInTheDocument();
  });

  it("says the list is empty in words rather than showing a bare table", async () => {
    mockUseApi.mockReturnValue(
      approvalsApi({ listPatchApprovals: vi.fn().mockResolvedValue(NO_APPROVALS) }) as ApiClient,
    );
    renderWithProviders(<FleetPatchPage />);

    expect(await screen.findByText(/No update is approved into a ring/i)).toBeInTheDocument();
  });

  it("says the approvals list FAILED rather than reading as an empty one", async () => {
    mockUseApi.mockReturnValue(
      approvalsApi({
        listPatchApprovals: vi.fn().mockRejectedValue(new ApiError(503, "")),
      }) as ApiClient,
    );
    renderWithProviders(<FleetPatchPage />);

    // "No approvals" and "we could not ask" are the same pixel otherwise, and
    // the second one means every ring's clearance is unknown.
    expect(await screen.findByText(/unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText(/No update has been approved into a ring/i)).not.toBeInTheDocument();
  });
});


// ===========================================================================
// THE OPERATOR'S REPORT, 2026-08-27: "i can now see the grouped pending
// updates per host, but when i open that host i can't approve the updates for
// it, i have to go back and approve 25 updates per page, that's not ideal".
//
// Two defects, and they meet in the same control. The device drill-down was a
// read-only `<ul>`, so drilling in was a dead end; and the selection was
// bounded by a 25-row page, so a 300-update machine was twelve page-loads of
// ticking. Both are now `DecisionBar`.
//
// THE MODEL TENSION THESE TESTS PIN. `patch_approvals` is
// `(org_id, patch_id, ring_name)` and has no device column, so "approve the
// updates for this host" is not expressible. The device is a LENS for choosing
// WHICH updates; the ring is the scope. Every assertion below that names a
// hostname is checking that the screen says so before the click, not that the
// approval is somehow scoped to the machine.
// ===========================================================================

describe("FleetPatchPage — approving from the machine you opened", () => {
  let api: Partial<ApiClient>;

  /** The fleet list and one device's list, served from the same route with one
   *  more predicate — which is exactly how the console asks for them. */
  function updatesByDevice(deviceRows: PatchUpdate[], total = deviceRows.length) {
    return vi.fn((params: { device_id?: string }) =>
      Promise.resolve(
        params.device_id
          ? updatePage(deviceRows, { total, total_unfiltered: total })
          : updatePage(UPDATES),
      ),
    );
  }

  const DEVICE_ROWS: PatchUpdate[] = MANY_PATCHES.map((patch) => update(patch, 4));

  beforeEach(() => {
    api = {};
    mockUseApi.mockImplementation(() => api);
  });

  async function openSentinel(user: ReturnType<typeof userEvent.setup>) {
    renderWithProviders(<FleetPatchPage />);
    await screen.findByRole("link", { name: "sentinel" });
    await user.click(
      screen.getByRole("button", { name: "Show sentinel's 41 outstanding updates" }),
    );
    return screen.getByRole("link", { name: "sentinel" }).closest("table") as HTMLElement;
  }

  it("offers a selection and an approval on the device's own list", async () => {
    const user = userEvent.setup();
    api = { listPatchUpdates: updatesByDevice(DEVICE_ROWS) };

    await openSentinel(user);

    // It was a read-only list: no checkbox, no ring, no approve, and a
    // navigation back to the fleet panel to do anything at all.
    await user.click(
      await screen.findByRole("checkbox", { name: "Select openssl-3.0.11 on sentinel" }),
    );
    expect(
      screen.getByRole("combobox", { name: "Ring to approve into — sentinel" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Approve 1 update/ })).toBeInTheDocument();
  });

  it("sends the updates chosen there, in one call, with the ring as the scope", async () => {
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn().mockResolvedValue(bulkResult(["p1", "p3"], "test"));
    api = { listPatchUpdates: updatesByDevice(DEVICE_ROWS), bulkApprovePatches };

    await openSentinel(user);
    await user.click(
      await screen.findByRole("checkbox", { name: "Select KB5031354 on sentinel" }),
    );
    await user.click(screen.getByRole("checkbox", { name: "Select openssl-3.0.11 on sentinel" }));
    await user.click(screen.getByRole("button", { name: /^Approve 2 updates/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));

    // NO DEVICE ID IN THE REQUEST, because there is no device column to put it
    // in. Inventing one in the console would be inventing a guarantee the
    // apply path does not honour.
    expect(bulkApprovePatches).toHaveBeenCalledTimes(1);
    expect(bulkApprovePatches).toHaveBeenCalledWith({
      patch_ids: ["p1", "p3"],
      ring_name: "test",
      client_id: "o1",
    });
  });

  it("says the ring is the scope and the host is only the lens, before the click", async () => {
    const user = userEvent.setup();
    api = { listPatchUpdates: updatesByDevice(DEVICE_ROWS) };

    await openSentinel(user);
    await user.click(
      await screen.findByRole("checkbox", { name: "Select KB5031354 on sentinel" }),
    );

    // On the page, beside the control, before anything is clicked.
    expect(
      screen.getByText(
        /You are choosing these from sentinel, but an approval covers the 2 devices of your organisation's own client in the test ring — not sentinel alone\./,
      ),
    ).toBeInTheDocument();

    // ...and again in the confirmation, which is the last screen before it
    // becomes true.
    await user.click(screen.getByRole("button", { name: /^Approve 1 update/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    expect(confirm).toHaveTextContent(/not sentinel alone/);
    expect(confirm).toHaveTextContent(
      /the 2 devices of your organisation's own client in the "test" ring/,
    );
  });

  it("names the blast radius of whichever ring is chosen, not the one it opened on", async () => {
    const user = userEvent.setup();
    api = { listPatchUpdates: updatesByDevice(DEVICE_ROWS) };

    await openSentinel(user);
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Ring to approve into — sentinel" }),
      "broad",
    );

    expect(
      screen.getByText(/the 200 devices of your organisation's own client in the broad ring/),
    ).toBeInTheDocument();
  });

  it("asks for that machine's whole list, never narrowed by the fleet severity filter", async () => {
    const user = userEvent.setup();
    const listPatchUpdates = updatesByDevice(DEVICE_ROWS);
    api = { listPatchUpdates };

    renderWithProviders(<FleetPatchPage />, "/patch?severity=critical");
    await screen.findByRole("link", { name: "sentinel" });
    await user.click(
      screen.getByRole("button", { name: "Show sentinel's 41 outstanding updates" }),
    );

    await waitFor(() =>
      expect(listPatchUpdates).toHaveBeenCalledWith(
        expect.objectContaining({ device_id: "dev-1", severity: [] }),
      ),
    );
  });

  it("discards the selection when the row is closed, not silently keeps it", async () => {
    const user = userEvent.setup();
    api = { listPatchUpdates: updatesByDevice(DEVICE_ROWS) };

    await openSentinel(user);
    await user.click(
      await screen.findByRole("checkbox", { name: "Select KB5031354 on sentinel" }),
    );
    expect(screen.getByRole("button", { name: /^Approve 1 update/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Hide sentinel's updates" }));
    await user.click(
      screen.getByRole("button", { name: "Show sentinel's 41 outstanding updates" }),
    );

    // A tick that survived a collapse would be a decision the operator can no
    // longer see, which is the one thing a bulk control must never do.
    await screen.findByRole("checkbox", { name: "Select KB5031354 on sentinel" });
    expect(screen.queryByRole("button", { name: /^Approve/ })).not.toBeInTheDocument();
  });
});

// ===========================================================================
// "i have to go back and approve 25 updates per page, that's not ideal".
//
// The fix has to keep the property `BulkApprovalCreateRequest` exists for:
// `patch_ids` is a list rather than a filter because "a filter is evaluated on
// the server AFTER the operator has read the screen, so the set that gets
// cleared is not the set they looked at". So the filter is resolved to ids in
// the browser, the exact count is shown before the commit, and the POST still
// carries a list.
// ===========================================================================

describe("FleetPatchPage — selecting everything the filter matches", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {};
    mockUseApi.mockImplementation(() => api);
  });

  /** A fleet of `total` updates served a window at a time, so the console has
   *  to walk it rather than read one page and call it everything. */
  function pagedFleet(total: number) {
    return vi.fn((params: { limit?: number; offset?: number }) => {
      const offset = params.offset ?? 0;
      const limit = params.limit ?? 25;
      const items: PatchUpdate[] = [];
      for (let index = offset; index < Math.min(total, offset + limit); index += 1) {
        items.push(update({ ...(PATCHES[0] as Patch), id: `p${index}`, identifier: `KB${index}` }));
      }
      return Promise.resolve(updatePage(items, { total, total_unfiltered: total }));
    });
  }

  it("offers to select everything matching, and says how many that is", async () => {
    api = { listPatchUpdates: pagedFleet(300) };
    renderWithProviders(<FleetPatchPage />);

    // BEFORE anything is ticked: the control that ends the 25-at-a-time
    // complaint is reachable without first building a selection by hand.
    expect(
      await screen.findByRole("button", { name: /^Select all 300 updates/ }),
    ).toBeInTheDocument();
  });

  it("resolves the filter to an explicit list and states the exact count first", async () => {
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn().mockResolvedValue(bulkResult(["p0"], "test"));
    api = { listPatchUpdates: pagedFleet(300), bulkApprovePatches };
    renderWithProviders(<FleetPatchPage />);

    await user.click(await screen.findByRole("button", { name: /^Select all 300 updates/ }));

    // The count is on the page before the operator commits to it.
    expect(await screen.findByText("300 updates selected of 300 matching.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Approve 300 updates/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    expect(confirm).toHaveTextContent("300 updates → test ring");
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));

    // AND IT IS STILL A LIST. The server never evaluates a filter of its own,
    // so the set cleared cannot differ from the set the operator was shown.
    const body = bulkApprovePatches.mock.calls[0]?.[0] as { patch_ids: string[] };
    expect(body.patch_ids).toHaveLength(300);
    expect(body.patch_ids[0]).toBe("p0");
    expect(body.patch_ids[299]).toBe("p299");
  });

  it("carries the filter in force into the resolution, not the whole catalogue", async () => {
    const user = userEvent.setup();
    const listPatchUpdates = pagedFleet(30);
    api = { listPatchUpdates };
    // WINDOWS, and the tab matters: on Linux this list is read-only evidence —
    // no checkboxes and no decision bar — because apt resolves a dependency
    // set and approving individual packages out of one is rarely actionable.
    // "Select all N matching" is a control of the approve-into-a-ring model, so
    // it lives on the two tabs that have that model.
    renderWithProviders(<FleetPatchPage />, "/patch?severity=critical&os=windows&q=openssl");

    await user.click(await screen.findByRole("button", { name: /^Select all 30 updates/ }));
    await screen.findByText("30 updates selected of 30 matching.");

    expect(listPatchUpdates).toHaveBeenCalledWith(
      expect.objectContaining({ severity: ["critical"], os_family: "windows", q: "openssl" }),
    );
  });

  it("says what will happen past the ceiling instead of approving the first 500 quietly", async () => {
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn().mockResolvedValue(bulkResult(["p0"], "test"));
    api = { listPatchUpdates: pagedFleet(812), bulkApprovePatches };
    renderWithProviders(<FleetPatchPage />);

    // The label does not claim "all", and the sentence beside it does the
    // arithmetic the operator would otherwise do after a 422.
    const select = await screen.findByRole("button", { name: /^Select the first 500 of 812/ });
    expect(
      screen.getByText(/one approval carries at most 500.*remaining 312/s),
    ).toBeInTheDocument();

    await user.click(select);
    expect(await screen.findByText("500 updates selected of 812 matching.")).toBeInTheDocument();
    expect(
      screen.getByText(/500 of 812 matching updates are selected — the most one approval can carry/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Approve 500 updates/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Approve updates into a deployment ring",
    });
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));

    const body = bulkApprovePatches.mock.calls[0]?.[0] as { patch_ids: string[] };
    expect(body.patch_ids).toHaveLength(500);
  });

  it("keeps the selection across a page turn and discards it when the filter changes", async () => {
    const user = userEvent.setup();
    api = { listPatchUpdates: pagedFleet(300) };
    renderWithProviders(<FleetPatchPage />);

    await user.click(await screen.findByRole("button", { name: /^Select all 300 updates/ }));
    await screen.findByText("300 updates selected of 300 matching.");

    // Ticking six rows on page 1 and four on page 2 is ONE decision.
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("300 updates selected of 300 matching.")).toBeInTheDocument();

    // Narrowing the filter is a different question being asked, and carrying
    // an answer to the old one across is how a batch comes to contain rows
    // nobody looked at.
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Filter Windows updates by severity" }),
      "critical",
    );
    await waitFor(() =>
      expect(screen.queryByText(/updates selected of/)).not.toBeInTheDocument(),
    );
  });

  it("says nothing was selected when the walk is refused, rather than looking inert", async () => {
    const user = userEvent.setup();
    api = {
      listPatchUpdates: vi
        .fn()
        .mockResolvedValueOnce(updatePage(UPDATES, { total: 300, total_unfiltered: 300 }))
        .mockRejectedValue(new ApiError(503, "")),
    };
    renderWithProviders(<FleetPatchPage />);

    await user.click(await screen.findByRole("button", { name: /^Select all 300 updates/ }));

    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((node) => /Nothing was selected/.test(node.textContent ?? ""))).toBe(true);
  });

  it("still ticks one page at a time, and says that is what it is doing", async () => {
    const user = userEvent.setup();
    api = { listPatchUpdates: pagedFleet(300) };
    renderWithProviders(<FleetPatchPage />);

    // Two controls, two clearly different promises: the header checkbox is
    // "this page", the bar's button is "everything matching".
    await user.click(
      await screen.findByRole("checkbox", { name: "Select every update on this page" }),
    );
    expect(await screen.findByText("25 updates selected of 300 matching.")).toBeInTheDocument();
  });
});

// ===========================================================================
// THREE PLATFORMS, THREE MODELS, ONE PAGE — the operator's 2026-08-26 decision.
//
// The split is not cosmetic and these are the assertions that hold it to that.
// `Patch.os_family` is `windows|linux|macos`, both rollups already take an
// `os_family` filter, and the three agent scanners disagree about what an
// update IS: apt publishes a security POCKET, Microsoft leaves most cumulative
// updates unrated, Apple publishes no severity at all. What follows from that
// is a different DEFAULT and a different panel ORDER per tab, which is exactly
// what one page serving all three could not express.
// ===========================================================================

describe("the platform tabs are navigation, and the URL is where they live", () => {
  beforeEach(() => mockUseApi.mockReturnValue({}));

  it("lands on the tab the URL names, so a link and a reload agree", async () => {
    renderWithProviders(<FleetPatchPage />, "/patch?os=macos");

    const strip = await screen.findByRole("tablist");
    expect(within(strip).getByRole("tab", { name: /macOS/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(within(strip).getByRole("tab", { name: /Windows/ })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("scopes BOTH rollups to the open tab, so no list can contain another platform", async () => {
    const listPatchUpdates = vi.fn().mockResolvedValue(updatePage(UPDATES));
    const listPatchDeviceRollup = vi.fn().mockResolvedValue(deviceRollupPage([DEVICE_ROLLUP]));
    mockUseApi.mockReturnValue({ listPatchUpdates, listPatchDeviceRollup });
    renderWithProviders(<FleetPatchPage />, "/patch?os=linux");

    await screen.findByText("2023-10 Cumulative Update");
    expect(listPatchUpdates).toHaveBeenCalledWith(
      expect.objectContaining({ os_family: "linux" }),
    );
    expect(listPatchDeviceRollup).toHaveBeenCalledWith(
      expect.objectContaining({ os_family: "linux" }),
    );
  });

  it("counts each tab from the same route the tab's own table is paged from", async () => {
    // A strip with no counts makes the default tab a coin toss: land on
    // Windows, see nothing, and never learn Linux is carrying three hundred
    // packages. The number is `total_unfiltered` off `GET /patch/updates`,
    // which is the same field the tab's first tile prints.
    const listPatchUpdates = vi.fn(({ os_family }: { os_family?: string }) =>
      Promise.resolve(
        updatePage(UPDATES, { total_unfiltered: os_family === "linux" ? 300 : 4 }),
      ),
    );
    mockUseApi.mockReturnValue({ listPatchUpdates });
    renderWithProviders(<FleetPatchPage />);

    const strip = await screen.findByRole("tablist");
    await waitFor(() =>
      expect(within(strip).getByRole("tab", { name: /Linux/ })).toHaveTextContent("300"),
    );
    expect(within(strip).getByRole("tab", { name: /macOS/ })).toHaveTextContent("4");
  });

  it("drops the old tab's filter, paging and selection when the platform changes", async () => {
    // Not tidiness. The severity DEFAULT differs per platform, so carrying
    // `?severity=critical,important` from Linux onto macOS applies a filter to
    // a field Apple does not publish; the offsets are positions in a different
    // list; and the selection is a set of ids belonging to the platform it was
    // made on.
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({});
    renderWithProviders(<FleetPatchPage />, "/patch?os=linux&severity=critical&q=ssl&offset=25");

    const strip = await screen.findByRole("tablist");
    await user.click(within(strip).getByRole("tab", { name: /macOS/ }));

    await waitFor(() =>
      expect(
        screen.getByRole("combobox", { name: "Filter macOS updates by severity" }),
      ).toHaveValue("all"),
    );
    expect(screen.getByRole("textbox", { name: "Search macOS updates" })).toHaveValue("");
  });

  it("names the panel the strip switches, so the tabs are not five orphan buttons", async () => {
    renderWithProviders(<FleetPatchPage />);

    const panel = await screen.findByRole("tabpanel");
    const strip = screen.getByRole("tablist");
    expect(within(strip).getByRole("tab", { name: /Windows/ })).toHaveAttribute(
      "aria-controls",
      panel.id,
    );
  });
});

describe("the Linux tab decides by machine, and says what installing really does", () => {
  let api: Partial<ApiClient>;
  const LINUX_HOST: PatchDeviceRollup = {
    ...DEVICE_ROLLUP,
    device_id: "dev-linux",
    hostname: "srv-app02",
    os_family: "linux",
    outstanding: 300,
    worst_severity: "important",
  };

  beforeEach(() => {
    api = {
      listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([LINUX_HOST])),
    };
    mockUseApi.mockImplementation(() => api);
  });

  function linuxPage() {
    return renderWithProviders(<FleetPatchPage />, "/patch?os=linux");
  }

  it("puts the machines first and the packages second, which is the model", async () => {
    linuxPage();

    const headings = (await screen.findAllByRole("heading", { level: 2 })).map(
      (node) => node.textContent,
    );
    expect(headings.indexOf("Linux machines")).toBeLessThan(
      headings.indexOf("What these machines are missing"),
    );
  });

  it("makes the package list read-only, because apt resolves a dependency SET", async () => {
    // The operator's own reasoning for choosing per-device apply:
    // "cherry-picking single packages often isn't actionable anyway". So the
    // list keeps its rows and loses its checkboxes.
    linuxPage();

    await screen.findByText("2023-10 Cumulative Update");
    expect(
      screen.queryByRole("checkbox", { name: "Select every update on this page" }),
    ).not.toBeInTheDocument();
    // The only "select all" on this tab is the one over MACHINES.
    expect(
      screen.getAllByRole("button", { name: /^Select all/ }).map((node) => node.textContent),
    ).toEqual(["Select all 1 host"]);
  });

  it("sends the machines the operator ticked, as an explicit list", async () => {
    const user = userEvent.setup();
    const applyPatches = vi.fn().mockResolvedValue({ job_ids: ["j-1"] });
    api = {
      listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([LINUX_HOST])),
      applyPatches,
    };
    linuxPage();

    await user.click(await screen.findByRole("checkbox", { name: "Select srv-app02" }));
    await user.click(screen.getByRole("button", { name: /^Install the approved updates/ }));

    const confirm = await screen.findByRole("dialog", {
      name: "Install approved updates now",
    });
    // THE MACHINE IS NAMED. "Install on 1 host" is a number somebody can agree
    // to without knowing which one, and the machine is the unit of the decision.
    expect(confirm).toHaveTextContent("srv-app02 is about to install.");
    await user.click(within(confirm).getByRole("button", { name: "Install" }));

    expect(applyPatches).toHaveBeenCalledWith({ device_ids: ["dev-linux"] });
  });

  it("states the two gates before the click, not after an empty result", async () => {
    const user = userEvent.setup();
    linuxPage();

    // `dispatch_apply` ships only what carries an un-revoked approval for the
    // device's ring, and skips the device entirely when `window_open` is false.
    // Both are said in the bar and repeated in the confirmation.
    expect(
      await screen.findByText(/only to hosts whose window is open right now/),
    ).toBeInTheDocument();

    await user.click(await screen.findByRole("checkbox", { name: "Select srv-app02" }));
    await user.click(screen.getByRole("button", { name: /^Install the approved updates/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Install approved updates now",
    });
    expect(confirm).toHaveTextContent(/already approved for its ring/);
    expect(confirm).toHaveTextContent(/maintenance window is shut is skipped/);
    // A restart follows the host's policy and window (spec §8.1).
    expect(confirm).toHaveTextContent(/only inside its maintenance window/);
  });

  it("reports the hosts that got nothing, rather than calling a request a result", async () => {
    // ONE JOB PER ELIGIBLE DEVICE. A device outside its window produces none,
    // so `job_ids.length` is routinely smaller than the selection — and a bar
    // reading "install queued" over a machine that received nothing is the
    // Scan defect wearing better arithmetic.
    const user = userEvent.setup();
    const second: PatchDeviceRollup = { ...LINUX_HOST, device_id: "dev-b", hostname: "srv-web01" };
    api = {
      listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([LINUX_HOST, second])),
      applyPatches: vi.fn().mockResolvedValue({ job_ids: ["j-1"] }),
    };
    linuxPage();

    await user.click(await screen.findByRole("checkbox", { name: "Select every host on this page" }));
    await user.click(screen.getByRole("button", { name: /^Install the approved updates/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Install approved updates now",
    });
    await user.click(within(confirm).getByRole("button", { name: "Install" }));

    expect(
      await screen.findByText(
        /Install queued on 1 host\. 1 host was skipped — either nothing on it is approved/,
      ),
    ).toBeInTheDocument();
  });

  it("dispatches nothing at all when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    const applyPatches = vi.fn();
    api = {
      listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([LINUX_HOST])),
      applyPatches,
    };
    linuxPage();

    await user.click(await screen.findByRole("checkbox", { name: "Select srv-app02" }));
    await user.click(screen.getByRole("button", { name: /^Install the approved updates/ }));
    const confirm = await screen.findByRole("dialog", {
      name: "Install approved updates now",
    });
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(applyPatches).not.toHaveBeenCalled();
  });

  it("offers no install control at all without patch.apply", async () => {
    api = {
      me: vi.fn().mockResolvedValue({ ...ME, permissions: ["patch.read", "devices.read"] }),
      listPatchDeviceRollup: vi.fn().mockResolvedValue(deviceRollupPage([LINUX_HOST])),
    };
    linuxPage();

    await screen.findByRole("link", { name: "srv-app02" });
    expect(screen.queryByRole("button", { name: /Install/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Select srv-app02" })).not.toBeInTheDocument();
  });
});

describe("each tab says what its own severity column actually means", () => {
  beforeEach(() => mockUseApi.mockReturnValue({}));

  it("names the security pocket on Linux, where the word is exact", async () => {
    renderWithProviders(<FleetPatchPage />, "/patch?os=linux");
    expect(
      await screen.findByText(/“important” means the version came from the security pocket/),
    ).toBeInTheDocument();
  });

  it("says Microsoft leaves the rollup unrated on Windows", async () => {
    renderWithProviders(<FleetPatchPage />);
    expect(
      await screen.findByText(/most Windows 11 cumulative updates carry none/),
    ).toBeInTheDocument();
  });

  it("says Apple publishes no severity at all, and names the other three limits", async () => {
    renderWithProviders(<FleetPatchPage />, "/patch?os=macos");

    expect(await screen.findByText(/Apple publishes no severity/)).toBeInTheDocument();
    // The three that are not about severity, each a property of `mac.go` or of
    // `agent-capabilities.md` §4 — and each one something WSUS can do that this
    // platform cannot, which is the whole reason they are on the screen.
    expect(screen.getByText(/No major-version upgrades, deliberately\./)).toBeInTheDocument();
    expect(screen.getByText(/No on-screen restart warning\./)).toBeInTheDocument();
    expect(
      screen.getByText(/Apple Silicon OS updates need a volume owner/),
    ).toBeInTheDocument();
  });
});

/* DID THE PATCHES I JUST APPLIED ACTUALLY INSTALL?

   The operator, 2026-09-01, after applying updates on one machine: "how do I
   know if the patches were applied successfully or not on that page?" They
   could not. The backend recorded everything — `DevicePatchStatus.state` went
   to `installed`/`failed` with `applied_at` and `last_error`, and `patch_runs`
   held the run — and the console surfaced none of it:

     · the device drill-down and the device Patch tab listed Update / Severity
       and a third column that could only ever say "only this device", so a
       `failed` row and a never-attempted one were the same three words;
     · `last_error` was reachable ONLY by downloading the CSV export;
     · NO route read `patch_runs` at all, so the fact that a run had happened
       lived in the job engine's raw result JSON on another page.

   Each test below fails if one of those surfaces goes away again. */

const RUN_WITH_A_FAILURE: PatchRun = {
  id: "run-1",
  job_id: "job-1",
  device_id: "d1",
  installed: 2,
  failed: 1,
  reboot_required: true,
  started_at: "2026-09-01T09:00:00.000Z",
  finished_at: "2026-09-01T09:04:00.000Z",
  failures: [{ source: "wsus", identifier: "KB5041585", error: "0x8024402c" }],
};

function withRunsAndUpdates(runs: PatchRun[], updates: PatchUpdate[]) {
  mockUseApi.mockReturnValue({
    getDevicePatchStatus: vi.fn().mockResolvedValue({
      device_id: "d1",
      counts_by_state: { failed: 1, missing: 1 },
      counts_by_severity: {},
      pending_reboot: false,
      last_scan_at: "2026-09-01T08:00:00.000Z",
    } satisfies DeviceStatus),
    listPatchRuns: vi.fn().mockResolvedValue({ items: runs }),
    listPatchUpdates: vi.fn().mockResolvedValue(updatePage(updates)),
  } as unknown as ApiClient);
  renderWithProviders(<DevicePatchTab deviceId="d1" />);
}

describe("the patch page says whether an apply worked", () => {
  it("reports the last apply's counts and names every update that failed", async () => {
    withRunsAndUpdates([RUN_WITH_A_FAILURE], []);

    expect(await screen.findByText("Last apply")).toBeInTheDocument();
    expect(screen.getByText("Finished with failures")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    // THE ERROR ITSELF, on the page. Before this it was in the CSV export and
    // nowhere else, so a count only told the operator to go and look elsewhere.
    expect(screen.getByText(/0x8024402c/)).toBeInTheDocument();
    expect(
      screen.getByText("A restart is needed before these take effect."),
    ).toBeInTheDocument();
  });

  it("does not report a dispatched-but-unanswered run as zero installed", async () => {
    // Both render `installed: 0`. "Still running" and "finished, installed
    // nothing" mean opposite things to the person waiting on the job, and
    // `finished_at` is the only field that separates them.
    withRunsAndUpdates(
      [{ ...RUN_WITH_A_FAILURE, installed: 0, failed: 0, failures: [], finished_at: null }],
      [],
    );

    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(screen.getByText(/has not reported back yet/)).toBeInTheDocument();
    expect(screen.queryByText("Finished with failures")).not.toBeInTheDocument();
  });

  it("distinguishes an update that was tried and failed from one nothing has attempted", async () => {
    const base = PATCHES[0] as Patch;
    withRunsAndUpdates(
      [],
      [
        {
          ...update({ ...base, id: "p-failed", identifier: "KB-FAILED" }),
          device_state: "failed",
          device_last_error: "0x80070422 the service cannot be started",
          device_applied_at: null,
        },
        {
          ...update({ ...base, id: "p-new", identifier: "KB-NEW" }),
          device_state: "missing",
          device_last_error: null,
          device_applied_at: null,
        },
      ],
    );

    // Scoped to the table: "Failed" is also a "By state" badge on the card
    // above, and the two are different statements — the card counts the
    // machine's standing position, the cell says what happened to THIS row.
    const table = within(await screen.findByRole("table"));
    expect(table.getByText("Failed")).toBeInTheDocument();
    // "Not attempted", not "Missing": in this column the backend's `missing`
    // means nothing has tried it yet, and that is the distinction being drawn.
    expect(table.getByText("Not attempted")).toBeInTheDocument();
    expect(screen.getByText(/0x80070422/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// The maintenance window, read to a person.
//
// The policy list printed `0 2-5 * * 6` into a table cell one row above
// another cell saying "always open" in words. Two languages for one fact, and
// the operator only speaks one of them. `describeCron` is the reading; these
// tests hold it to the two things a wrong reading would get wrong -- cron's
// inclusive end hour, and its two Sundays -- and to refusing rather than
// guessing when the expression is one the painter cannot draw.
// ---------------------------------------------------------------------------

describe("a maintenance window is read to the operator, not printed at them", () => {
  it("names the day and the hours the window is actually open", () => {
    // `2-5` is FOUR hours, and it is open for the whole of hour five: a
    // reading of "02:00 to 05:00" would cost an operator an hour.
    expect(describeCron("0 2-5 * * 6")).toBe("Saturday, 02:00 to 06:00");
  });

  it("contracts three or more consecutive days into a span", () => {
    expect(describeCron("0 22-23 * * 1-5")).toBe("Monday to Friday, 22:00 to 24:00");
  });

  it("lists two days rather than spanning them", () => {
    // "Saturday to Sunday" is a longer way of saying the same two days, and
    // it invites the reading that the window runs across the night between.
    expect(describeCron("0 3 * * 0,6")).toBe("Saturday, Sunday, 03:00 to 04:00");
  });

  it("reads cron's second Sunday as Sunday", () => {
    expect(describeCron("0 1 * * 7")).toBe("Sunday, 01:00 to 02:00");
  });

  it("joins two separate runs of hours rather than spanning the gap between", () => {
    expect(describeCron("0 2-3,22 * * 6")).toBe("Saturday, 02:00 to 04:00 and 22:00 to 23:00");
  });

  it("refuses to read an expression the painter cannot draw", () => {
    // A step, a named day, a day-of-month, a non-zero minute: each is valid
    // cron and none of them is a weekly grid. Inventing a sentence for one
    // would be worse than showing the expression.
    expect(describeCron("0 */4 * * 6")).toBeNull();
    expect(describeCron("0 2 * * SAT")).toBeNull();
    expect(describeCron("0 2 1 * *")).toBeNull();
    expect(describeCron("30 2 * * 6")).toBeNull();
    expect(describeCron("")).toBeNull();
  });

  it("prints the sentence and keeps the stored expression beside it", async () => {
    mockUseApi.mockReturnValue({
      listPatchPolicies: vi
        .fn()
        .mockResolvedValue([{ ...POLICY, maintenance_window: "0 2-5 * * 6" }]),
    });
    renderWithProviders(<PatchPolicyEditor />);

    expect(await screen.findByText("Saturday, 02:00 to 06:00")).toBeInTheDocument();
    // The raw value is what an operator pastes into a ticket and checks
    // against the matcher, so the reading is added to it, not swapped for it.
    expect(screen.getByText("0 2-5 * * 6")).toBeInTheDocument();
  });

  it("says the expression is custom rather than inventing a reading for it", async () => {
    mockUseApi.mockReturnValue({
      listPatchPolicies: vi
        .fn()
        .mockResolvedValue([{ ...POLICY, maintenance_window: "0 */4 * * 6" }]),
    });
    renderWithProviders(<PatchPolicyEditor />);

    expect(await screen.findByText("Custom expression")).toBeInTheDocument();
    expect(screen.getByText("0 */4 * * 6")).toBeInTheDocument();
  });

  it("still says a policy with no window is always open, in words", async () => {
    mockUseApi.mockReturnValue({
      listPatchPolicies: vi.fn().mockResolvedValue([{ ...POLICY, maintenance_window: null }]),
    });
    renderWithProviders(<PatchPolicyEditor />);

    expect(await screen.findByText("always open")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// A tile is a figure and a caption saying what it counts.
//
// These four carried a supporting line only when the fleet had never been
// scanned, so in the ordinary case an operator got a bare digit -- while the
// tiles on the neighbouring pages in this design all carry one. Three states,
// and they are different facts: unmeasured, a measured zero (which has a
// sentence of its own saying WHICH zero), and a count with its scope.
// ---------------------------------------------------------------------------

describe("every patch tile says what its number counts", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {};
    mockUseApi.mockImplementation(() => api);
  });

  it("puts the scope under each of the four figures", async () => {
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    expect(
      screen.getByText("Distinct updates offered to reporting Windows machines"),
    ).toBeInTheDocument();
    expect(screen.getByText("Of the updates outstanding, rated critical")).toBeInTheDocument();
    expect(
      screen.getByText("Reporting Windows machines with at least one outstanding update"),
    ).toBeInTheDocument();
  });

  it("does not stack a scope line under a measured zero, which already has its own sentence", async () => {
    api = { listPatchUpdates: vi.fn().mockResolvedValue(updatePage([])) };
    renderWithProviders(<FleetPatchPage />);

    expect(
      await screen.findByText("Every reporting Windows machine has everything it was offered"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Distinct updates offered to reporting Windows machines"),
    ).not.toBeInTheDocument();
  });

  it("says nothing has been measured rather than naming a scope, before any scan", async () => {
    api = neverScannedApi();
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("No patch scan has completed yet");
    expect(screen.getAllByText("Never scanned").length).toBeGreaterThan(0);
    expect(
      screen.queryByText("Distinct updates offered to reporting Windows machines"),
    ).not.toBeInTheDocument();
  });
});

/* REVIEW, 2026-09-08 [MINOR, copy]: "The Approvals table's BY column prints raw
   account ids ('usr-helgar', 'usr-caetano') where the rest of the console
   prints display names."

   The row carries `approved_by` as a uuid and no name, so this is a lookup and
   not a formatter — and the lookup is `users.read`-gated, which is the reason
   all three cases below are separate tests rather than one. What must NOT
   happen is the console printing "—" or a blank for an approval that has an
   actor: the id is the only handle on such a row, so it survives as the
   fallback. */
describe("the Approvals table names the person who approved, when it can", () => {
  const OPERATOR = {
    id: "u1",
    email: "ines.caetano@toeshee.example",
    display_name: "Ines Caetano",
    status: "active" as const,
    roles: [],
  };

  function namedApi(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
    return {
      ...mockDefaults(),
      me: vi.fn().mockResolvedValue({ ...ME, permissions: [...ME.permissions, "users.read"] }),
      listPatchApprovals: vi.fn().mockResolvedValue({ items: [APPROVAL], total: 1 }),
      listUsers: vi.fn().mockResolvedValue({ users: [OPERATOR] }),
      ...overrides,
    };
  }

  const approvalRow = async (): Promise<HTMLElement> =>
    (await waitFor(() => {
      const row = document.querySelector('[data-approval-id="a1"]');
      if (!row) throw new Error("no approvals row");
      return row as HTMLElement;
    })) as HTMLElement;

  it("prints the display name rather than the account id", async () => {
    mockUseApi.mockReturnValue(namedApi() as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    const row = await approvalRow();
    expect(await within(row).findByText("Ines Caetano")).toBeInTheDocument();
    expect(within(row).queryByText("u1")).not.toBeInTheDocument();
  });

  it("keeps the id when this operator cannot read the directory", async () => {
    // `users.read` is not implied by `patch.approve`. The name is unavailable,
    // the fact is not: an id beats a blank in the column that says who decided.
    const api = namedApi({ me: vi.fn().mockResolvedValue(ME) });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    const row = await approvalRow();
    expect(within(row).getByText("u1")).toBeInTheDocument();
    // ...and the page does not spend a request it knows will 403.
    expect(api.listUsers).not.toHaveBeenCalled();
  });

  it("keeps the id when the directory does not carry that account", async () => {
    // A deleted account is RETAINED and may not be listed; the approval it made
    // is still on the page.
    const api = namedApi({ listUsers: vi.fn().mockResolvedValue({ users: [] }) });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    const row = await approvalRow();
    await waitFor(() => expect(api.listUsers).toHaveBeenCalled());
    expect(within(row).getByText("u1")).toBeInTheDocument();
  });

  it("does not ask for the directory when every approval is the policy's own", async () => {
    const api = namedApi({
      listPatchApprovals: vi
        .fn()
        .mockResolvedValue({ items: [{ ...APPROVAL, approved_by: null }], total: 1 }),
    });
    mockUseApi.mockReturnValue(api as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    const row = await approvalRow();
    expect(within(row).getByText("Policy auto-approval")).toBeInTheDocument();
    expect(api.listUsers).not.toHaveBeenCalled();
  });
});

/* REVIEW, 2026-09-08 [MAJOR, copy] and [MINOR, a11y]: the compliance bars were
   labelled with the raw wire values ("windows", "critical") while the tabs
   above them said "Windows" and the table pills said "Critical"; and the bar's
   hue is the compliance band, which runs opposite to the severity palette the
   same words carry everywhere else on this page, with nothing saying so. */
describe("the compliance panel speaks the page's own vocabulary", () => {
  const SPLIT_REPORT: FleetReport = {
    ...FLEET_REPORT,
    compliant_pct_by_os_family: { windows: 82.5, linux: 33.3, macos: 100 },
    compliant_pct_by_severity: { critical: 91.0, important: 33.3, low: 55.0 },
  };

  function splitApi(): Partial<ApiClient> {
    return {
      ...mockDefaults(),
      getPatchFleetReport: vi.fn().mockResolvedValue(SPLIT_REPORT),
    };
  }

  const group = (heading: string): HTMLElement => {
    const section = screen.getByText(heading).closest("section");
    if (!section) throw new Error(`no compliance group headed ${heading}`);
    return section as HTMLElement;
  };

  it("labels the platform rows the way the tab strip does", async () => {
    mockUseApi.mockReturnValue(splitApi() as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("By platform");
    const platforms = group("By platform");
    expect(within(platforms).getByText("Windows")).toBeInTheDocument();
    expect(within(platforms).getByText("Linux")).toBeInTheDocument();
    // The one that a generic capitaliser gets wrong, which is why the tab
    // strip's own map is what this panel reads.
    expect(within(platforms).getByText("macOS")).toBeInTheDocument();
    expect(within(platforms).queryByText("windows")).not.toBeInTheDocument();
    expect(within(platforms).queryByText("macos")).not.toBeInTheDocument();
  });

  it("labels the severity rows the way the severity pill does", async () => {
    mockUseApi.mockReturnValue(splitApi() as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("By severity");
    const severities = group("By severity");
    expect(within(severities).getByText("Critical")).toBeInTheDocument();
    expect(within(severities).getByText("Important")).toBeInTheDocument();
    expect(within(severities).getByText("Low")).toBeInTheDocument();
    expect(within(severities).queryByText("critical")).not.toBeInTheDocument();
  });

  it("says on the page that the colour is the compliance band, not the severity", async () => {
    mockUseApi.mockReturnValue(splitApi() as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    expect(
      await screen.findByText(/Bar colour is the compliance band, not the severity/),
    ).toBeInTheDocument();
  });

  it("puts the band in each bar's name, so the hue is not the only carrier", async () => {
    mockUseApi.mockReturnValue(splitApi() as ApiClient);
    renderWithProviders(<FleetPatchPage />);

    // 91% against a target it is held to: the green is "at target", and a
    // reader who cannot see the green hears it.
    expect(
      await screen.findByRole("progressbar", { name: "Critical compliance, by severity, at target" }),
    ).toBeInTheDocument();
    // 33.3% against the same target. The RED here is the fleet being behind on
    // important updates, not the severity "important" being red.
    expect(
      screen.getByRole("progressbar", { name: "Important compliance, by severity, behind target" }),
    ).toBeInTheDocument();
    // And a row nobody is chasing 100% on says exactly that instead of taking
    // an alarm colour.
    expect(
      screen.getByRole("progressbar", {
        name: "Low compliance, by severity, not held to a target",
      }),
    ).toBeInTheDocument();
  });
});

// ===========================================================================
// AN APPROVAL'S REACH IS ITS OWNER'S (MSP clients plan 2026-10-26 Task 14
// review). A client's approval clears that client's machines in the ring and
// no other client's; a global one -- "For every client" -- clears every
// client's. The screen states the reach of the approval actually being made:
// the internal client's by default (phase 3 brings the selector), the ROW's
// own client on a machine's drill-down (the operator's rule of 2026-10-28),
// and every client's only when asked. And the Approvals list tells a global
// row from a client's row of the same update and ring.
// ===========================================================================

describe("FleetPatchPage — an approval reaches its owner's machines, and says so", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {};
    mockUseApi.mockImplementation(() => api);
  });

  const MY_CLIENTS: MeClient[] = [{ id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: ME.permissions }, { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ME.permissions }];
  /** `ME` with `clients.read`: the console can name a client. */
  const READS_CLIENTS: Me = { ...ME, permissions: [...ME.permissions, "clients.read"], clients: MY_CLIENTS };

  const CLIENTS = [
    {
      id: "o1",
      name: "Novieri",
      kind: "internal" as const,
      status: "active",
      email_domains: [],
      phone: "",
      address: "",
      website: "",
      notes: "",
      enabled_modules: [],
      device_count: 219,
      site_count: 1,
    },
    {
      id: "c-acme",
      name: "Acme",
      kind: "customer" as const,
      status: "active",
      email_domains: [],
      phone: "",
      address: "",
      website: "",
      notes: "",
      enabled_modules: [],
      device_count: 21,
      site_count: 1,
    },
  ];

  async function openApproval(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole("button", { name: /^Approve 1 update/ }));
    return screen.findByRole("dialog", { name: "Approve updates into a deployment ring" });
  }

  it("states the internal client's machines by default, and every client's when asked", async () => {
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn().mockResolvedValue(bulkResult(["p1"], "test"));
    api = { bulkApprovePatches };
    renderWithProviders(<FleetPatchPage />);

    await screen.findByText("2023-10 Cumulative Update");
    await user.click(screen.getByRole("checkbox", { name: "Select KB5031354" }));

    // Unticked: the internal client's 2 of the ring's 3 machines.
    let confirm = await openApproval(user);
    expect(confirm).toHaveTextContent(
      'This update becomes installable on the 2 devices of your organisation\'s own client in the "test" ring at their next maintenance window.',
    );
    expect(confirm).not.toHaveTextContent(/3 devices/);
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    // Ticked: all 3, of every client -- and no client is sent.
    await user.click(screen.getByRole("checkbox", { name: /For every client/ }));
    confirm = await openApproval(user);
    expect(confirm).toHaveTextContent(
      'This update becomes installable on the 3 devices of every client in the "test" ring at their next maintenance window.',
    );
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));
    expect(bulkApprovePatches).toHaveBeenCalledWith({ patch_ids: ["p1"], ring_name: "test" });
  });

  /** A customer's machine on the device list, and its own update list. */
  function customerMachine(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
    return {
      listPatchDeviceRollup: vi
        .fn()
        .mockResolvedValue(deviceRollupPage([{ ...DEVICE_ROLLUP, client_id: "c-acme" }])),
      listPatchUpdates: vi.fn((params?: PatchUpdateFilters) =>
        Promise.resolve(params?.device_id ? updatePage(MANY_UPDATES) : updatePage(UPDATES)),
      ),
      ...overrides,
    };
  }

  async function chooseOnSentinel(user: ReturnType<typeof userEvent.setup>) {
    renderWithProviders(<FleetPatchPage />);
    await screen.findByRole("link", { name: "sentinel" });
    await user.click(
      screen.getByRole("button", { name: "Show sentinel's 41 outstanding updates" }),
    );
    await user.click(
      await screen.findByRole("checkbox", { name: "Select KB5031354 on sentinel" }),
    );
  }

  it("files an approval from a customer's machine under that customer, and says its reach", async () => {
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn().mockResolvedValue(bulkResult(["p1"], "test"));
    api = customerMachine({
      me: vi.fn().mockResolvedValue(READS_CLIENTS),
      listClients: vi.fn().mockResolvedValue(CLIENTS),
      bulkApprovePatches,
    });
    await chooseOnSentinel(user);

    expect(
      await screen.findByText(
        "You are choosing these from sentinel, but an approval covers the 1 device of Acme in the test ring — not sentinel alone.",
      ),
    ).toBeInTheDocument();
    const confirm = await openApproval(user);
    expect(confirm).toHaveTextContent(/not sentinel alone/);
    expect(confirm).toHaveTextContent(/installable on the 1 device of Acme in the "test" ring/);
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));

    // The ROW's client, not the internal one.
    expect(bulkApprovePatches).toHaveBeenCalledExactlyOnceWith({
      patch_ids: ["p1"],
      ring_name: "test",
      client_id: "c-acme",
    });
  });

  it("says on the bar what an approval reaches, and what ticking For every client does", async () => {
    // Final review E Important 3. The hint said what the row would be visible
    // to ("Every client's technicians will see it ...") -- for a patch
    // approval the effect is reach: ticking makes the update installable on
    // every client's machines in the ring.
    const user = userEvent.setup();
    renderWithProviders(<FleetPatchPage />);
    await screen.findByText("2023-10 Cumulative Update");
    expect(
      await screen.findByText(
        "An approval here covers the 2 devices of your organisation's own client in the test ring.",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Select KB5031354" }));
    const toggle = screen.getByRole("checkbox", { name: /For every client/ });
    expect(toggle.closest("label")).toHaveTextContent(
      "Ticked, these updates become installable on every client's machines in the test ring. Unticked, only on the machines of your organisation's own client.",
    );
    expect(toggle.closest("label")).not.toHaveTextContent(/will see it/);
    await user.click(toggle);
    // A global approval does reach the ring's whole population.
    expect(screen.getByText("The test ring covers 3 of 240 devices.")).toBeInTheDocument();
  });

  it("names the machine's own client in the hint on a customer's drill-down", async () => {
    const user = userEvent.setup();
    api = customerMachine({
      me: vi.fn().mockResolvedValue(READS_CLIENTS),
      listClients: vi.fn().mockResolvedValue(CLIENTS),
    });
    await chooseOnSentinel(user);
    const section = screen.getByRole("combobox", { name: "Ring to approve into — sentinel" })
      .parentElement as HTMLElement;
    expect(
      (await within(section).findByRole("checkbox", { name: /For every client/ })).closest("label"),
    ).toHaveTextContent(
      "Ticked, these updates become installable on every client's machines in the test ring. Unticked, only on the machines of Acme.",
    );
  });

  it("names the customer from /me, without reading the clients list", async () => {
    const user = userEvent.setup();
    const listClients = vi.fn();
    api = customerMachine({ me: vi.fn().mockResolvedValue({ ...ME, clients: MY_CLIENTS }), listClients });
    await chooseOnSentinel(user);

    expect(
      await screen.findByText(
        "You are choosing these from sentinel, but an approval covers the 1 device of Acme in the test ring — not sentinel alone.",
      ),
    ).toBeInTheDocument();
    expect(listClients).not.toHaveBeenCalled();
  });

  it("states every client's reach from a customer's machine when asked, and sends no client", async () => {
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn().mockResolvedValue(bulkResult(["p1"], "test"));
    api = customerMachine({ bulkApprovePatches });
    await chooseOnSentinel(user);

    const section = screen.getByRole("combobox", { name: "Ring to approve into — sentinel" })
      .parentElement as HTMLElement;
    await user.click(within(section).getByRole("checkbox", { name: /For every client/ }));
    expect(
      screen.getByText(
        "You are choosing these from sentinel, but an approval covers all 3 devices of every client in the test ring — not sentinel alone.",
      ),
    ).toBeInTheDocument();
    const confirm = await openApproval(user);
    await user.click(within(confirm).getByRole("button", { name: "Approve" }));
    expect(bulkApprovePatches).toHaveBeenCalledExactlyOnceWith({
      patch_ids: ["p1"],
      ring_name: "test",
    });
  });

  // -------------------------------------------------------------------------
  // The Approvals list: a global row and a client's row of the same update
  // and ring, which used to read identically.
  // -------------------------------------------------------------------------

  const GLOBAL_ROW: PatchApproval = { ...APPROVAL, id: "a-global", client_id: null };
  const ACME_ROW: PatchApproval = { ...APPROVAL, id: "a-acme", client_id: "c-acme" };

  function twinApprovals(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
    return {
      me: vi.fn().mockResolvedValue(READS_CLIENTS),
      listClients: vi.fn().mockResolvedValue(CLIENTS),
      listPatchApprovals: vi.fn().mockResolvedValue({ items: [GLOBAL_ROW, ACME_ROW], total: 2 }),
      revokePatchApproval: vi.fn().mockResolvedValue(undefined),
      ...overrides,
    };
  }

  const rowOf = (id: string): Promise<HTMLElement> =>
    waitFor(() => {
      const row = document.querySelector(`[data-approval-id="${id}"]`);
      if (!row) throw new Error(`no approvals row for ${id}`);
      return row as HTMLElement;
    });

  it("tells a global approval from a client's approval of the same update and ring", async () => {
    api = twinApprovals();
    renderWithProviders(<FleetPatchPage />);

    const global = await rowOf("a-global");
    const acme = await rowOf("a-acme");
    expect(within(global).getByText("Every client")).toBeInTheDocument();
    expect(await within(acme).findByText("Acme")).toBeInTheDocument();
    expect(within(acme).queryByText("Every client")).not.toBeInTheDocument();
    // Their row menus, too, are two different controls to a screen reader.
    expect(
      within(global).getByRole("button", {
        name: "More actions for KB5031354 → pilot ring, every client",
      }),
    ).toBeInTheDocument();
    expect(
      within(acme).getByRole("button", { name: "More actions for KB5031354 → pilot ring, Acme" }),
    ).toBeInTheDocument();
  });

  it("names the client from /me in the list, without clients.read", async () => {
    api = twinApprovals({ me: vi.fn().mockResolvedValue({ ...ME, clients: MY_CLIENTS }) });
    renderWithProviders(<FleetPatchPage />);

    expect(within(await rowOf("a-acme")).getByText("Acme")).toBeInTheDocument();
    expect(within(await rowOf("a-global")).getByText("Every client")).toBeInTheDocument();
  });

  it("says whose machines stop when each one is revoked", async () => {
    const user = userEvent.setup();
    api = twinApprovals();
    renderWithProviders(<FleetPatchPage />);

    async function revokeDialog(id: string) {
      const row = await rowOf(id);
      await user.click(within(row).getByRole("button", { name: /more actions/i }));
      await user.click(screen.getByRole("menuitem", { name: "Revoke" }));
      return screen.findByRole("dialog", { name: "Revoke patch approval" });
    }

    let confirm = await revokeDialog("a-global");
    expect(confirm).toHaveTextContent(
      'The devices of every client in the "pilot" ring will no longer install this update, except the machines of a client whose own approval of it still stands,',
    );
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    await within(await rowOf("a-acme")).findByText("Acme");
    confirm = await revokeDialog("a-acme");
    expect(confirm).toHaveTextContent(
      'The devices of Acme in the "pilot" ring will no longer install this update, unless an approval for every client still covers them,',
    );
    expect(confirm).toHaveTextContent("KB5031354 → pilot ring, Acme");
  });
});
