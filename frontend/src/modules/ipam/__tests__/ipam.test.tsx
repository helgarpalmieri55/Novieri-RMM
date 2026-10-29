import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { existsSync, readFileSync } from "node:fs";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient } from "../../../lib/api";
import { AddressDialog } from "../AddressDialog";
import { AddressGrid } from "../AddressGrid";
import { DriftPage } from "../DriftPage";
import { ImportPage } from "../ImportPage";
import { ScanAuthorizationsPage } from "../ScanAuthorizationsPage";
import { SubnetTreePage } from "../SubnetTreePage";
import { SweepPanel } from "../SweepPanel";
import { UtilizationBar } from "../UtilizationBar";
import type {
  DriftOut,
  GridEntryOut,
  HistoryEntryOut,
  OrphanOut,
  ScopeOut,
  SubnetOut,
  SweepRunOut,
  UtilizationOut,
} from "../types";

// Component tests against RENDERED OUTPUT. The auth-backed API client is mocked
// so nothing here touches fetch — mirrors modules/health/__tests__/health.test.tsx.
//
// EVERY ASSERTION BELOW IS ABOUT A DISTINCTION, not about wording: a null
// utilisation percentage against a real 0%, a dormant row against a drift row,
// a derived `status` against a documented `state`, an age against the timestamp
// it came from, an authorised prefix against a merely documented one, and a
// previewed import file against a swapped one. A test that asserted only "the
// page rendered" would pass against every collapse this module exists to
// prevent.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWithProviders(node: React.ReactNode, entry = "/ipam") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * THE ROUTE TABLE, READ FROM `App.tsx` ITSELF.
 *
 * "I don't want dead tiles everywhere" is only kept if the destinations are
 * checked against the routes that exist, not against a list in this file that
 * would rot beside them. Every `path="…"` in App.tsx becomes a matcher; the
 * catch-all is excluded, because a tile that only matches `*` is exactly the
 * dead tile — it renders the device list and calls it a destination.
 */
function appRoutes(): RegExp[] {
  // Located from the working directory rather than from `import.meta.url`:
  // under jsdom that URL is not a file: URL, so `fileURLToPath` refuses it.
  const path = existsSync("src/App.tsx") ? "src/App.tsx" : "frontend/src/App.tsx";
  const source = readFileSync(path, "utf8");
  return [...source.matchAll(/path="([^"]+)"/g)]
    .map((match) => match[1]!)
    .filter((path) => path !== "*")
    .map((path) => new RegExp(`^${path.replace(/:[^/]+/g, "[^/]+")}$`));
}

/** Every tile is a link, to a path App.tsx actually routes. */
function expectTileLinks(expected: readonly string[]) {
  const tiles = [...document.querySelectorAll<HTMLAnchorElement>("a.stat-card")];
  expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([...expected]);
  const routes = appRoutes();
  for (const href of expected) {
    const path = href.split("?")[0]!;
    expect(routes.some((route) => route.test(path)), `${path} is not a route`).toBe(true);
  }
}

function stubApi(overrides: Partial<ApiClient>): ApiClient {
  const api = {
    me: vi.fn().mockResolvedValue({
      user_id: "u1",
      org_id: "o1",
      email: "a@b.c",
      display_name: "A",
      permissions: ["ipam.read", "ipam.assign", "ipam.manage", "ipam.sweep.run"],
    }),
    listIpamVrfs: vi.fn().mockResolvedValue([]),
    listClientSites: vi.fn().mockResolvedValue([]),
    listIpamVlans: vi.fn().mockResolvedValue([]),
    listIpamSubnets: vi.fn().mockResolvedValue([]),
    getIpamSubnet: vi.fn(),
    listIpamSubnetAddresses: vi.fn().mockResolvedValue([]),
    getIpamSubnetUtilization: vi.fn().mockResolvedValue(utilization()),
    listIpamRanges: vi.fn().mockResolvedValue([]),
    listIpamDrift: vi.fn().mockResolvedValue([]),
    listIpamOrphans: vi.fn().mockResolvedValue([]),
    listIpamScanAuthorizations: vi.fn().mockResolvedValue([]),
    listIpamSweepRuns: vi.fn().mockResolvedValue([]),
    getIpamAddressHistory: vi.fn().mockResolvedValue([]),
    // The drift page names the device ids in a finding's evidence out of the
    // console's shared `["devices"]` read. Empty by default: a page that reads
    // it must still render every id it cannot resolve.
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    ...overrides,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

function subnet(overrides: Partial<SubnetOut> = {}): SubnetOut {
  return {
    client_id: "o1",
    id: "s1",
    vrf_id: "v1",
    prefix: "10.4.7.0/24",
    kind: "assignable",
    name: "Core",
    description: "",
    site_id: null,
    vlan_id: null,
    gateway: null,
    dns_servers: [],
    parent_id: null,
    sweep_enabled: false,
    sweep_device_id: null,
    sweep_interval_hours: null,
    last_swept_at: null,
    ...overrides,
  };
}

function utilization(overrides: Partial<UtilizationOut> = {}): UtilizationOut {
  return {
    subnet_id: "s1",
    prefix: "10.4.7.0/24",
    enumerable: true,
    capacity: 254,
    free: 200,
    used: 54,
    percentage: 21.26,
    assigned: 40,
    reserved: 10,
    deprecated: 4,
    observed: 44,
    ...overrides,
  };
}

function gridEntry(overrides: Partial<GridEntryOut> = {}): GridEntryOut {
  return {
    address: "10.4.7.9",
    status: "in_use",
    state: "assigned",
    address_id: "a1",
    device_id: "dev1",
    asset_label: "",
    mac: "a4:bb:6d:11:22:33",
    hostname: "WS-01",
    last_seen_at: null,
    // DELIBERATELY NOT `unknown`/`unknown` as the default. The whole defect
    // §3.2 recorded was that this field was CONSTANT (`unknown`) everywhere, so
    // a fixture that made it constant again would leave every assertion below it
    // decorative. The default is a documented `static`, and the tests that are
    // about this column vary BOTH fields together.
    allocation_method: "static",
    allocation_method_source: "documented",
    range_kinds: [],
    ...overrides,
  };
}

function drift(overrides: Partial<DriftOut> = {}): DriftOut {
  return {
    client_id: "o1",
    id: "d1",
    vrf_id: "v1",
    subnet_id: "s1",
    address: "10.4.7.9",
    drift_class: "documented_absent",
    severity: "medium",
    confidence: "medium",
    detail: { last_seen_days: 30 },
    device_id: null,
    first_detected_at: "2026-07-01T00:00:00.000Z",
    last_evaluated_at: "2026-07-31T00:00:00.000Z",
    consecutive_detections: 3,
    resolved_at: null,
    acknowledged_at: null,
    suppressed_until: null,
    note: "",
    ...overrides,
  };
}

function scope(overrides: Partial<ScopeOut> = {}): ScopeOut {
  return {
    client_id: "o1",
    id: "sc1",
    vrf_id: "v1",
    prefix: "10.4.7.0/24",
    note: "",
    authorized_by: "u1",
    authorized_at: "2026-07-01T00:00:00.000Z",
    expires_at: null,
    revoked_at: null,
    ...overrides,
  };
}

// --------------------------------------------------------------------------
// The enumeration ceiling
// --------------------------------------------------------------------------

describe("utilization above the enumeration ceiling", () => {
  it("renders `unavailable` and NO percentage — never 0%", () => {
    stubApi({});
    renderWithProviders(
      <UtilizationBar
        utilization={utilization({
          prefix: "10.0.0.0/12",
          enumerable: false,
          capacity: null,
          free: null,
          used: null,
          percentage: null,
        })}
      />,
    );
    expect(screen.getByText("Utilization unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/0\.0%/)).not.toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("renders a real percentage for an enumerable prefix, so the two are distinguishable", () => {
    stubApi({});
    renderWithProviders(<UtilizationBar utilization={utilization({ percentage: 0 })} />);
    // A genuinely empty /24 says 0.0%; the unenumerable one above says nothing.
    expect(screen.getByText("0.0%")).toBeInTheDocument();
    expect(screen.queryByText("Utilization unavailable")).not.toBeInTheDocument();
  });

  it("replaces the grid with a counted panel on the 422, rather than showing an empty table", async () => {
    stubApi({
      listIpamSubnetAddresses: vi
        .fn()
        .mockRejectedValue(new ApiError(422, "subnet too large to enumerate")),
    });
    renderWithProviders(
      <AddressGrid
        subnetId="s1"
        subnetPrefix="10.0.0.0/12"
        vrfId="v1"
        utilization={utilization({
          enumerable: false,
          capacity: null,
          free: null,
          used: null,
          percentage: null,
          assigned: 40,
          observed: 44,
        })}
        canAssign
      />,
    );
    expect(await screen.findByText("Too large to enumerate")).toBeInTheDocument();
    expect(screen.getByText("40")).toBeInTheDocument();
    expect(screen.getByText(/is not computed for this prefix and is not zero/)).toBeInTheDocument();
    expect(screen.queryByText("Address")).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Age, not timestamps; state versus status
// --------------------------------------------------------------------------

describe("the address grid", () => {
  it("renders last-seen as an age and never prints the timestamp", async () => {
    vi.setSystemTime(new Date("2026-08-02T12:00:00.000Z"));
    stubApi({
      listIpamSubnetAddresses: vi
        .fn()
        .mockResolvedValue([gridEntry({ last_seen_at: "2026-07-30T09:42:07.000Z" })]),
    });
    renderWithProviders(
      <AddressGrid subnetId="s1" subnetPrefix="10.4.7.0/24" vrfId="v1" utilization={undefined} canAssign />,
    );
    expect(await screen.findByText("3 days ago")).toBeInTheDocument();
    expect(screen.queryByText(/09:42/)).not.toBeInTheDocument();
    expect(screen.queryByText(/2026-07-30/)).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("shows the derived status and the documented state as two different cells", async () => {
    stubApi({
      listIpamSubnetAddresses: vi
        .fn()
        // The case that matters: the plan says `assigned`, the fleet says
        // nothing, and the derived answer is `Dormant` — the plan did not
        // change.
        .mockResolvedValue([gridEntry({ status: "dormant", state: "assigned" })]),
    });
    renderWithProviders(
      <AddressGrid subnetId="s1" subnetPrefix="10.4.7.0/24" vrfId="v1" utilization={undefined} canAssign />,
    );
    const row = (await screen.findByText("10.4.7.9")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Dormant")).toBeInTheDocument();
    expect(within(row).getByText("Assigned")).toBeInTheDocument();
  });

  it("says `Not documented` for an address with no row, rather than repeating the status", async () => {
    stubApi({
      listIpamSubnetAddresses: vi.fn().mockResolvedValue([
        gridEntry({
          address: "10.4.7.50",
          status: "undocumented",
          state: null,
          address_id: null,
          device_id: null,
          mac: null,
        }),
      ]),
    });
    renderWithProviders(
      <AddressGrid subnetId="s1" subnetPrefix="10.4.7.0/24" vrfId="v1" utilization={undefined} canAssign />,
    );
    const row = (await screen.findByText("10.4.7.50")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Undocumented")).toBeInTheDocument();
    expect(within(row).getByText("Not documented")).toBeInTheDocument();
  });

  it("distinguishes a documented allocation method from an observed one", async () => {
    // THE TWO ROWS CARRY THE SAME `allocation_method`. If the component rendered
    // only the method, these rows would be identical on screen and this test
    // could not fail — which is exactly §0.6's collapse, one column across: an
    // operator seeing `DHCP` on an address they set to static, with nothing
    // saying the endpoint is what reported it, concludes the IPAM edited their
    // plan.
    stubApi({
      listIpamSubnetAddresses: vi.fn().mockResolvedValue([
        gridEntry({
          address: "10.4.7.9",
          allocation_method: "dhcp",
          allocation_method_source: "documented",
        }),
        gridEntry({
          address: "10.4.7.10",
          allocation_method: "dhcp",
          allocation_method_source: "observed",
        }),
      ]),
    });
    renderWithProviders(
      <AddressGrid subnetId="s1" subnetPrefix="10.4.7.0/24" vrfId="v1" utilization={undefined} canAssign />,
    );
    const documented = (await screen.findByText("10.4.7.9")).closest("tr") as HTMLElement;
    const observed = (await screen.findByText("10.4.7.10")).closest("tr") as HTMLElement;

    expect(within(documented).getByText("DHCP")).toBeInTheDocument();
    expect(within(observed).getByText("DHCP")).toBeInTheDocument();
    // The distinction, asserted on the rendered row and not on the fixture.
    expect(within(documented).queryByText("Observed")).not.toBeInTheDocument();
    expect(within(observed).getByText("Observed")).toBeInTheDocument();
    expect(within(observed).getByTitle(/reported this address as "dhcp"/i)).toBeInTheDocument();
    expect(within(documented).getByTitle(/an operator set/i)).toBeInTheDocument();
  });

  it("renders an em dash, not the word `unknown`, when nobody has said", async () => {
    // `unknown` is an ABSENCE, and every other absence in this grid is an em
    // dash. Printing the word would put a value in a cell where there is none —
    // and it is what a sweep-only address will read forever, because ARP and
    // ICMP cannot establish how an address was configured.
    stubApi({
      listIpamSubnetAddresses: vi.fn().mockResolvedValue([
        gridEntry({
          address: "10.4.7.60",
          allocation_method: "unknown",
          allocation_method_source: "unknown",
        }),
      ]),
    });
    renderWithProviders(
      <AddressGrid subnetId="s1" subnetPrefix="10.4.7.0/24" vrfId="v1" utilization={undefined} canAssign />,
    );
    const row = (await screen.findByText("10.4.7.60")).closest("tr") as HTMLElement;
    expect(within(row).queryByText(/unknown/i)).not.toBeInTheDocument();
    expect(within(row).getByTitle(/Nobody has said how this address is allocated/i))
      .toBeInTheDocument();
  });

  it("bands an address inside a DHCP pool", async () => {
    stubApi({
      listIpamSubnetAddresses: vi
        .fn()
        .mockResolvedValue([gridEntry({ status: "dhcp", range_kinds: ["dhcp_pool"] })]),
    });
    renderWithProviders(
      <AddressGrid subnetId="s1" subnetPrefix="10.4.7.0/24" vrfId="v1" utilization={undefined} canAssign />,
    );
    expect(await screen.findByText("DHCP pool")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// The history panel — §11.3's payload, not just the state change
// --------------------------------------------------------------------------
//
// `GET /addresses/{id}/history` returns `detail` (§0.13; the API's own
// `HistoryEntryOut` calls it "PART OF THE CONTRACT, NOT A DEBUG EXTRA"). Every
// case below is written so the RENDERED and the UNRENDERED page differ: the
// claimant, the method and the previous claimant appear in the fixture ONLY
// inside `detail`, never on the grid entry, so deleting the rendering fails on
// a value rather than on a label the component would print anyway. The dialog
// is mounted WITHOUT `ipam.assign` on purpose — the assign form carries an
// allocation-method `<select>` whose option text would otherwise satisfy an
// assertion about a method that was never read out of `detail`.

function historyEntry(overrides: Partial<HistoryEntryOut> = {}): HistoryEntryOut {
  return {
    ts: "2026-07-30T09:42:07.000Z",
    actor_type: "user",
    actor_id: "u1",
    action: "ipam.address_state_changed",
    from_state: null,
    to_state: "free",
    // The three context keys every `_audit_state_change` writes.
    detail: {
      address: "10.4.7.9",
      vrf_id: "8f1c9b52-0000-4000-8000-00000000v1rf",
      subnet_id: "3a7d2e64-0000-4000-8000-0000000sub1",
    },
    ...overrides,
  };
}

function renderHistory(entries: HistoryEntryOut[]) {
  stubApi({ getIpamAddressHistory: vi.fn().mockResolvedValue(entries) });
  renderWithProviders(
    <AddressDialog
      // Deliberately a DIFFERENT claimant than any fixture below records: the
      // grid row is "who holds it now", the history is "who held it then", and
      // a test that let the two share a string could not tell them apart.
      entry={gridEntry({ device_id: "dev-currently-holding-it" })}
      subnetPrefix="10.4.7.0/24"
      vrfId="v1"
      canAssign={false}
      onClose={() => {}}
    />,
  );
}

describe("the address history panel", () => {
  it("names the claimant and the allocation method an assignment recorded", async () => {
    renderHistory([
      historyEntry({
        action: "ipam.address_assigned",
        from_state: "free",
        to_state: "assigned",
        detail: {
          address: "10.4.7.9",
          subnet_id: "s1",
          vrf_id: "v1",
          claimant: "dev-7f3a-took-it",
          claimant_type: "device",
          allocation_method: "slaac",
        },
      }),
    ]);

    // The whole point of the fix: the state change alone does not say who or
    // how. VALUES ASSERTED FIRST, deliberately — a label is text the component
    // owns, so a suite that trips on "Claimed by" has not yet proved the
    // claimant itself made it out of `detail`.
    expect(await screen.findByText("dev-7f3a-took-it (device)")).toBeInTheDocument();
    expect(screen.getByText("slaac")).toBeInTheDocument();
    expect(screen.getByText("Claimed by")).toBeInTheDocument();
    expect(screen.getByText("Method")).toBeInTheDocument();
    // ...and not as a blob somebody has to parse.
    expect(screen.queryAllByText(/"claimant"/)).toHaveLength(0);
    expect(screen.queryAllByText(/[{}]/)).toHaveLength(0);
  });

  it("distinguishes an asset-label claimant from a device one", async () => {
    renderHistory([
      historyEntry({
        action: "ipam.address_assigned",
        to_state: "assigned",
        detail: { claimant: "PRINTER-3", claimant_type: "asset_label" },
      }),
    ]);
    // "PRINTER-3 (device)" would be a lie about what the org knows: an asset
    // label is a string somebody typed, a device id resolves to an inventory row.
    expect(await screen.findByText("PRINTER-3 (asset label)")).toBeInTheDocument();
    expect(screen.queryByText("PRINTER-3 (device)")).not.toBeInTheDocument();
  });

  it("names the PREVIOUS claimant on a release", async () => {
    renderHistory([
      historyEntry({
        action: "ipam.address_released",
        from_state: "assigned",
        to_state: "free",
        detail: { address: "10.4.7.9", previous_claimant: "dev-9c11-let-it-go" },
      }),
    ]);
    expect(await screen.findByText("dev-9c11-let-it-go")).toBeInTheDocument();
    expect(screen.getByText("Released from")).toBeInTheDocument();
  });

  it("says a released RESERVATION was held by nobody, rather than showing a blank", async () => {
    // `addresses.release` records `previous_claimant` as the device id or the
    // asset label — and a reservation has neither, so it writes "". A blank
    // there reads as a value that went missing.
    renderHistory([
      historyEntry({
        action: "ipam.address_released",
        from_state: "reserved",
        to_state: "free",
        detail: { previous_claimant: "" },
      }),
    ]);
    expect(await screen.findByText(/nobody, it was held, not claimed/)).toBeInTheDocument();
    expect(screen.queryByText("(empty)")).not.toBeInTheDocument();
  });

  it("spells out an edit as old → new per field, not as two objects", async () => {
    renderHistory([
      historyEntry({
        action: "ipam.address_updated",
        from_state: null,
        to_state: null,
        detail: {
          address: "10.4.7.9",
          from: { hostname: "WS-OLD", allocation_method: "static" },
          to: { hostname: "WS-NEW", allocation_method: "dhcp" },
        },
      }),
    ]);
    expect(await screen.findByText("WS-OLD → WS-NEW")).toBeInTheDocument();
    expect(screen.getByText("static → dhcp")).toBeInTheDocument();
  });

  it("renders a key it has never seen instead of dropping it or blanking the panel", async () => {
    // The server owns this shape and may grow keys; §0.13's projection is
    // whatever the audit event carried. An unanticipated key must survive.
    renderHistory([
      historyEntry({
        action: "ipam.address_quarantined",
        detail: {
          address: "10.4.7.9",
          quarantine_reason: "conflict-with-9c11",
          evidence_macs: ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"],
          nested_thing: { depth: 2, why: "because servers grow keys" },
        },
      }),
    ]);
    expect(await screen.findByText("conflict-with-9c11")).toBeInTheDocument();
    expect(screen.getByText("Quarantine reason")).toBeInTheDocument();
    expect(screen.getByText("aa:bb:cc:dd:ee:ff, 11:22:33:44:55:66")).toBeInTheDocument();
    expect(
      screen.getByText("Depth: 2, Why: because servers grow keys"),
    ).toBeInTheDocument();
  });

  it("keeps the age convention inside detail — no clock time anywhere in the list", async () => {
    vi.setSystemTime(new Date("2026-08-02T12:00:00.000Z"));
    renderHistory([
      historyEntry({
        ts: "2026-07-30T09:42:07.000Z",
        action: "ipam.address_assigned",
        to_state: "assigned",
        detail: { claimant: "dev-7f3a-took-it", claimant_type: "device", observed_at: "2026-07-31T09:42:07.000Z" },
      }),
    ]);
    expect(await screen.findByText("3 days ago")).toBeInTheDocument();
    expect(screen.getByText("2 days ago")).toBeInTheDocument();
    expect(screen.queryAllByText(/09:42/)).toHaveLength(0);
    expect(screen.queryAllByText(/2026-07-3/)).toHaveLength(0);
    vi.useRealTimers();
  });

  it("does not repeat the address, VRF and subnet the dialog already states", async () => {
    renderHistory([historyEntry()]);
    expect(await screen.findByText(/ipam.address_state_changed/)).toBeInTheDocument();
    // De-duplication, not redaction: the server decided the response, and the
    // three constants would drown the two facts §11.3 exists for.
    expect(screen.queryByText("Vrf id")).not.toBeInTheDocument();
    expect(screen.queryByText("Subnet id")).not.toBeInTheDocument();
    expect(screen.queryByText("8f1c9b52-0000-4000-8000-00000000v1rf")).not.toBeInTheDocument();
    expect(screen.queryByText("3a7d2e64-0000-4000-8000-0000000sub1")).not.toBeInTheDocument();
  });

  it("still renders an entry whose detail is empty", async () => {
    renderHistory([historyEntry({ action: "ipam.address_reserved", detail: {} })]);
    expect(await screen.findByText(/ipam.address_reserved/)).toBeInTheDocument();
    expect(screen.queryByText("No recorded events for this address.")).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Drift versus dormant versus orphans
// --------------------------------------------------------------------------

describe("the drift page", () => {
  function stubDrift(driftRows: DriftOut[], dormantRows: DriftOut[], orphans: OrphanOut[]) {
    return stubApi({
      listIpamDrift: vi.fn().mockImplementation((filters: { class?: string } = {}) =>
        Promise.resolve(filters.class === "dormant" ? dormantRows : driftRows),
      ),
      listIpamOrphans: vi.fn().mockResolvedValue(orphans),
    });
  }

  it("keeps dormant and orphans OUT of the drift headline", async () => {
    stubDrift(
      [drift({ id: "a" }), drift({ id: "b", drift_class: "conflict" })],
      [
        drift({ id: "c", drift_class: "dormant" }),
        drift({ id: "d", drift_class: "dormant" }),
        drift({ id: "e", drift_class: "dormant" }),
      ],
      [{ prefix: "10.7.3.0/24", count: 24, last_seen_at: "2026-08-01T00:00:00.000Z" }],
    );
    renderWithProviders(<DriftPage />);

    // Each stat card is located by its own explanatory line, which is unique —
    // the words "Drift", "Dormant" and "Orphans" also appear on the tab
    // buttons and in the module nav. A stat card is now an `<a class="stat-card">`:
    // every tile is a link to the rows it counted, so `closest("a")` IS the
    // card, and a tile that stopped being a link would fail here.
    const driftCard = (
      await screen.findByText(/Unresolved findings\./)
    ).closest("a") as HTMLElement;
    expect(driftCard).toHaveAttribute("href", "/ipam/drift");
    expect(await within(driftCard).findByText("2")).toBeInTheDocument();

    const dormantCard = screen
      .getByText(/Cannot currently be verified\./)
      .closest("a") as HTMLElement;
    expect(dormantCard).toHaveAttribute("href", "/ipam/drift?class=dormant");
    expect(await within(dormantCard).findByText("3")).toBeInTheDocument();

    const orphanCard = screen
      .getByText(/Observed addresses inside no registered subnet\./)
      .closest("a") as HTMLElement;
    expect(orphanCard).toHaveAttribute("href", "/ipam/drift?class=orphans");
    expect(await within(orphanCard).findByText("24")).toBeInTheDocument();

    // The collapse this guards against: 2 + 3 + 24 on the drift headline.
    expect(within(driftCard).queryByText("5")).not.toBeInTheDocument();
    expect(within(driftCard).queryByText("29")).not.toBeInTheDocument();
  });

  it("gives a conflict and a MAC mismatch different remedies", async () => {
    stubDrift(
      [
        drift({ id: "a", drift_class: "conflict", severity: "high" }),
        drift({ id: "b", drift_class: "mac_mismatch", address: "10.4.7.10" }),
      ],
      [],
      [],
    );
    renderWithProviders(<DriftPage />);

    // The remedy rides on the class pill's title (a per-row paragraph pushed
    // the Actions column past the panel edge); the class notice above the
    // table spells it out when a chip is selected.
    const conflictRow = (await screen.findByText("10.4.7.9")).closest("tr") as HTMLElement;
    const mismatchRow = screen.getByText("10.4.7.10").closest("tr") as HTMLElement;
    const remedyOf = (row: HTMLElement, pillLabel: string) =>
      within(row).getByText(pillLabel).closest("[title]")?.getAttribute("title") ?? "";
    expect(remedyOf(conflictRow, "Conflict")).toMatch(/operational fix first/i);
    expect(remedyOf(mismatchRow, "MAC mismatch")).toMatch(/documentation correction/i);
    // The two must not carry the same instruction.
    expect(remedyOf(mismatchRow, "MAC mismatch")).not.toMatch(/operational fix first/i);
  });

  it("shows the evidence for each finding as facts, not as JSON", async () => {
    // The payload `reconcile._evaluate` writes for a conflict. An operator has
    // to be able to read WHICH MACs, WHICH claimant and HOW MANY VRFs out of
    // the cell — `JSON.stringify(detail, null, 1)` in a `<pre>` "shows" all
    // three and answers none of them.
    stubDrift(
      [
        drift({
          drift_class: "conflict",
          detail: {
            macs: ["a4:bb:6d:11:22:33", "f0:18:98:aa:bb:cc"],
            claimant_mismatch: true,
            vrf_count: 2,
          },
        }),
      ],
      [],
      [],
    );
    renderWithProviders(<DriftPage />);

    const row = (await screen.findByText("10.4.7.9")).closest("tr") as HTMLElement;
    expect(within(row).getByText("a4:bb:6d:11:22:33, f0:18:98:aa:bb:cc")).toBeInTheDocument();
    expect(within(row).getByText(/inventory names a different device/)).toBeInTheDocument();
    expect(within(row).getByText("2")).toBeInTheDocument();
    // The defect itself: no braces, brackets or quoted keys anywhere in the row.
    expect(row.textContent).not.toMatch(/[{}[\]]/);
    expect(row.textContent).not.toMatch(/"macs"/);
  });

  it("shows a moved device's two addresses instead of ageing them into `unknown`", async () => {
    // `documented_at`/`observed_at` are ADDRESSES in a `device_moved` payload.
    // Rule 1's `_at` convention applied blindly turns both into "unknown" and
    // deletes the only two facts that make "move the assignment" actionable.
    stubDrift(
      [
        drift({
          drift_class: "device_moved",
          detail: {
            documented_at: "10.4.7.9",
            observed_at: "10.4.7.57",
            device_id: "0d1f8a2c-dev",
            observed_subnet_id: "9c11-subnet",
          },
        }),
      ],
      [],
      [],
    );
    renderWithProviders(<DriftPage />);

    const row = (await screen.findByText("0d1f8a2c-dev")).closest("tr") as HTMLElement;
    expect(within(row).getByText("10.4.7.57")).toBeInTheDocument();
    // Twice: the address column, and `documented_at` in the evidence — "where
    // it should be" is half of what "move the assignment" means.
    expect(within(row).getAllByText("10.4.7.9")).toHaveLength(2);
    expect(within(row).queryByText("unknown")).not.toBeInTheDocument();
  });

  it("puts dormant behind its own tab, with the sentence that says it is not a problem", async () => {
    const user = userEvent.setup();
    stubDrift([], [drift({ id: "c", drift_class: "dormant", address: "10.4.7.99" })], []);
    renderWithProviders(<DriftPage />);

    await user.click(await screen.findByRole("button", { name: /^Dormant \(1\)$/ }));
    expect(await screen.findByText("10.4.7.99")).toBeInTheDocument();
    expect(screen.getByText(/switched-off laptop/i)).toBeInTheDocument();
  });

  it("says which gate failed on a dormant row, and still counts it as no drift", async () => {
    const user = userEvent.setup();
    stubDrift(
      [],
      [
        drift({
          id: "c",
          drift_class: "dormant",
          address: "10.4.7.99",
          detail: { state: "assigned", device_id: "0d1f8a2c-dev", gate_e: true, gate_l: false },
        }),
      ],
      [],
    );
    renderWithProviders(<DriftPage />);

    // The headline is unchanged by anything the evidence column does: a bucket
    // an operator can read is not a bucket they are charged for.
    const driftCard = (await screen.findByText(/Unresolved findings\./)).closest(
      "a",
    ) as HTMLElement;
    expect(within(driftCard).getByText("0")).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: /^Dormant \(1\)$/ }));
    const row = (await screen.findByText("10.4.7.99")).closest("tr") as HTMLElement;
    // WHY it cannot be verified — which is the difference between "we cannot
    // see it" and "it is wrong".
    expect(
      within(row).getByText(/offline past the window/),
    ).toBeInTheDocument();
    expect(
      within(row).getByText(/Yes, inside the window/),
    ).toBeInTheDocument();
  });

  it("puts orphans behind their own tab, with a register-the-subnet action", async () => {
    const user = userEvent.setup();
    stubDrift([], [], [
      { prefix: "10.7.3.0/24", count: 24, last_seen_at: "2026-08-01T00:00:00.000Z" },
    ]);
    renderWithProviders(<DriftPage />);

    await user.click(await screen.findByRole("button", { name: /^Orphans \(24\)$/ }));
    expect(await screen.findByText("10.7.3.0/24")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Register this subnet" })).toHaveAttribute(
      "href",
      "/ipam?register=1",
    );
    expect(screen.getByText(/grouped by inferred \/24 or \/64/)).toBeInTheDocument();
  });

  it("names the device and the subnet an evidence id stands for", async () => {
    // THE FINDING: every other column on this page names what it is about, and
    // the evidence beside them read "Observed by dev-5", "Documented device
    // dev-3", "Observed in subnet subnet-1". An id is what `reconcile` holds,
    // not what an operator recognises.
    const moved = drift({
      drift_class: "device_moved",
      detail: { device_id: "dev-3", observed_by: "dev-5", observed_subnet_id: "s1" },
    });
    stubApi({
      listIpamDrift: vi.fn().mockImplementation((filters: { class?: string } = {}) =>
        Promise.resolve(filters.class === "dormant" ? [] : [moved]),
      ),
      listIpamOrphans: vi.fn().mockResolvedValue([]),
      listIpamSubnets: vi.fn().mockResolvedValue([subnet()]),
      listDevices: vi.fn().mockResolvedValue({
        items: [
          { id: "dev-3", hostname: "fs-archive-01", fqdn: null },
          { id: "dev-5", hostname: "rack-b4-relay", fqdn: null },
        ],
        total: 2,
      }),
    });
    renderWithProviders(<DriftPage />);

    const row = (await screen.findByText("10.4.7.9")).closest("tr") as HTMLElement;
    await waitFor(() =>
      expect(within(row).getByText("fs-archive-01")).toBeInTheDocument(),
    );
    expect(within(row).getByText("rack-b4-relay")).toBeInTheDocument();
    // The subnet id resolves through the SAME registry the row's own link uses.
    expect(within(row).getAllByText(/10\.4\.7\.0\/24/).length).toBeGreaterThan(0);
    expect(within(row).queryByText("dev-3")).not.toBeInTheDocument();
    expect(within(row).queryByText("dev-5")).not.toBeInTheDocument();
  });

  it("still renders an id no lookup knows, rather than blanking the fact", async () => {
    // The names are a courtesy over ids the page can already render. A device
    // that has left the fleet must not take its evidence with it.
    stubDrift(
      [drift({ drift_class: "device_moved", detail: { device_id: "dev-gone" } })],
      [],
      [],
    );
    renderWithProviders(<DriftPage />);
    const row = (await screen.findByText("10.4.7.9")).closest("tr") as HTMLElement;
    expect(within(row).getByText("dev-gone")).toBeInTheDocument();
  });

  it("marks a single detection as not yet published", async () => {
    stubDrift([drift({ consecutive_detections: 1 })], [], []);
    renderWithProviders(<DriftPage />);
    expect(await screen.findByText(/seen once, not published yet/)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Scan authorization
// --------------------------------------------------------------------------

describe("scan authorizations", () => {
  it("states a grant's reach in addresses, not just its mask", async () => {
    stubApi({
      listIpamScanAuthorizations: vi.fn().mockResolvedValue([scope({ prefix: "10.0.0.0/8" })]),
      listIpamSubnets: vi.fn().mockResolvedValue([subnet()]),
    });
    renderWithProviders(<ScanAuthorizationsPage />);
    expect(await screen.findByText("16,777,216 addresses")).toBeInTheDocument();
    // And which registered subnets it opens.
    expect(screen.getByText(/Covers 1 registered subnet/)).toBeInTheDocument();
  });

  it("says what an active grant permits once, not once per row", async () => {
    // Four rows carried four copies of "Sweeps may put ICMP/ARP packets on any
    // address inside <prefix>, <n> addresses, in this VRF" — beside a Prefix
    // cell already reading "<prefix> · <n> addresses". On a phone those four
    // wrapped lines sat outside the card's horizontal scroll and set a row
    // height with nothing visible in it.
    stubApi({
      listIpamScanAuthorizations: vi
        .fn()
        .mockResolvedValue([
          scope({ id: "sc1", prefix: "10.20.4.0/24" }),
          scope({ id: "sc2", prefix: "10.20.9.0/24" }),
        ]),
      listIpamSubnets: vi.fn().mockResolvedValue([]),
    });
    renderWithProviders(<ScanAuthorizationsPage />);

    expect(await screen.findByText("10.20.4.0/24")).toBeInTheDocument();
    expect(screen.getAllByText(/An active authorization lets sweeps probe \(ping and ARP\)/)).toHaveLength(
      1,
    );
    // Said once above the table, so it is above the first row rather than
    // inside any of them.
    const table = screen.getByRole("table");
    expect(table.textContent).not.toMatch(/ping and ARP/);
    // What DOES vary per row is still in the row.
    expect(screen.getAllByText(/No registered subnet falls inside it yet/)).toHaveLength(2);
  });

  it("keeps an inactive grant's own summary, which is a fact about that row", async () => {
    // "It covered this while it was active" is true of one row and of no other,
    // so unlike the sentence above it does not move out of the table.
    stubApi({
      listIpamScanAuthorizations: vi
        .fn()
        .mockResolvedValue([scope({ revoked_at: "2026-07-15T00:00:00.000Z" })]),
    });
    renderWithProviders(<ScanAuthorizationsPage />);
    const table = await screen.findByRole("table");
    expect(table.textContent).toMatch(/Authorizes nothing/);
  });

  it("says an empty list authorizes nothing at all", async () => {
    stubApi({ listIpamScanAuthorizations: vi.fn().mockResolvedValue([]) });
    renderWithProviders(<ScanAuthorizationsPage />);
    expect(
      await screen.findByText(/No prefix is authorized for sweeping/),
    ).toBeInTheDocument();
  });

  it("distinguishes a revoked grant from an active one", async () => {
    stubApi({
      listIpamScanAuthorizations: vi
        .fn()
        .mockResolvedValue([scope({ revoked_at: "2026-07-15T00:00:00.000Z" })]),
    });
    renderWithProviders(<ScanAuthorizationsPage />);
    expect(await screen.findByText("Revoked")).toBeInTheDocument();
    expect(screen.getByText(/Authorizes nothing/)).toBeInTheDocument();
    expect(screen.queryByText("Active, packets allowed")).not.toBeInTheDocument();
  });
});

describe("the sweep panel", () => {
  function stubSweep(scopes: ScopeOut[], runs: SweepRunOut[] = []) {
    return stubApi({
      listIpamScanAuthorizations: vi.fn().mockResolvedValue(scopes),
      listIpamSweepRuns: vi.fn().mockResolvedValue(runs),
    });
  }

  it("says NOT AUTHORIZED for a documented subnet nobody authorized", async () => {
    stubSweep([]);
    renderWithProviders(<SweepPanel subnet={subnet()} canRun canManage />);
    expect(await screen.findByText("Not authorized")).toBeInTheDocument();
    expect(screen.getByText(/documenting a subnet does not authorize probing it/i)).toBeInTheDocument();
    // The way out of the refusal, not just the refusal (cross-module guidance §6 #8).
    expect(await screen.findByRole("link", { name: "Authorize scanning" })).toHaveAttribute(
      "href",
      "/ipam/scan-authorizations",
    );
  });

  it("offers no authorize link while the authorizations are still unknown or already cover it", async () => {
    stubApi({
      listIpamScanAuthorizations: vi.fn().mockReturnValue(new Promise(() => {})),
      listIpamSweepRuns: vi.fn().mockResolvedValue([]),
    });
    renderWithProviders(<SweepPanel subnet={subnet()} canRun canManage />);
    await screen.findByText("Not authorized");
    expect(screen.queryByRole("link", { name: "Authorize scanning" })).toBeNull();
  });

  it("says authorized, and names the grant, when one covers the prefix", async () => {
    stubSweep([scope({ prefix: "10.0.0.0/8", note: "customer A LAN" })]);
    renderWithProviders(<SweepPanel subnet={subnet()} canRun canManage />);
    expect(await screen.findByText("Authorized")).toBeInTheDocument();
    expect(screen.getByText("customer A LAN")).toBeInTheDocument();
    expect(screen.getByText(/never wider than the subnet/)).toBeInTheDocument();
  });

  it("renders `no_agent` as its own outcome rather than as a failure", async () => {
    stubSweep(
      [scope()],
      [
        {
          client_id: "o1",
          id: "r1",
          subnet_id: "s1",
          job_id: null,
          device_id: null,
          state: "no_agent",
          trigger: "scheduled",
          target_count: 0,
          scope_digest: "",
          hosts_found: 0,
          truncated: false,
          started_at: "2026-08-01T00:00:00.000Z",
          finished_at: "2026-08-01T00:00:01.000Z",
          error: "",
        },
      ],
    );
    renderWithProviders(<SweepPanel subnet={subnet()} canRun canManage />);
    const cells = await screen.findAllByText("No agent could sweep");
    expect(cells.length).toBeGreaterThan(0);
    expect(screen.queryByText(/^Failed$/)).not.toBeInTheDocument();
    // ...with the remedy nobody guesses: an agent on the segment, installed by hand.
    expect(screen.getByText(/no enrolled agent is on this segment/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Install the agent on one machine there" })).toHaveAttribute(
      "href",
      "/agent-packages",
    );
    expect(screen.queryByRole("link", { name: "Authorize scanning" })).toBeNull();
  });

  it("refuses a sub-hourly interval in the UI, with the reason", async () => {
    const user = userEvent.setup();
    stubSweep([]);
    renderWithProviders(<SweepPanel subnet={subnet()} canRun canManage />);
    const input = await screen.findByLabelText("Interval (hours)");
    await user.clear(input);
    await user.type(input, "0");
    expect(screen.getByText(/how an address plan turns into a ping monitor/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save sweep settings" })).toBeDisabled();
  });
});

// --------------------------------------------------------------------------
// Import's two-step guard
// --------------------------------------------------------------------------

describe("the import page", () => {
  it("refuses Apply until the chosen file has been previewed", async () => {
    stubApi({ previewIpamImport: vi.fn(), applyIpamImport: vi.fn() });
    renderWithProviders(<ImportPage />);
    expect(await screen.findByRole("button", { name: "Apply" })).toBeDisabled();
    expect(screen.getByText(/Choose a CSV file/)).toBeInTheDocument();
  });

  it("enables Apply only after a preview of that exact file, and disables it again on swap", async () => {
    const user = userEvent.setup();
    const preview = vi.fn().mockResolvedValue({
      digest: "a".repeat(64),
      total_rows: 1,
      creates: 1,
      updates: 0,
      conflicts: 0,
      errors: 0,
      rows: [{ line: 2, action: "create", address: "10.4.7.9", error: "", detail: {} }],
    });
    const apply = vi.fn();
    stubApi({ previewIpamImport: preview, applyIpamImport: apply });
    renderWithProviders(<ImportPage />);

    const input = (await screen.findByLabelText("CSV file")) as HTMLInputElement;
    const first = new File(["address,hostname\n10.4.7.9,WS-01\n"], "plan.csv", {
      type: "text/csv",
    });
    await user.upload(input, first);

    // Map the address column, which the server requires.
    const select = await screen.findByLabelText("address");
    await user.selectOptions(select, "address");

    expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
    expect(screen.getByText(/Preview this file first/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Apply" })).not.toBeDisabled(),
    );
    expect(screen.getByText(/nothing has been written/i)).toBeInTheDocument();

    // Swapping the file must revoke the permission the preview granted.
    const second = new File(["address,hostname\n10.4.7.10,WS-02\n"], "other.csv", {
      type: "text/csv",
    });
    await user.upload(input, second);
    await waitFor(() => expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled());
    expect(apply).not.toHaveBeenCalled();
  });

  it("says the drift export excludes dormant", async () => {
    stubApi({});
    renderWithProviders(<ImportPage />);
    expect(await screen.findByText(/Dormant addresses are not in it/)).toBeInTheDocument();
  });

  // The verdict table's Detail column was `row.error || JSON.stringify(row.detail)`
  // — the raw-payload defect both auditors filed elsewhere, in the one table an
  // operator reads before writing a CSV into the plan of record.
  it("renders a row's detail as labelled facts, never as a JSON payload", async () => {
    const user = userEvent.setup();
    const preview = vi.fn().mockResolvedValue({
      digest: "a".repeat(64),
      total_rows: 2,
      creates: 1,
      updates: 0,
      conflicts: 0,
      errors: 1,
      rows: [
        {
          line: 2,
          action: "create",
          address: "10.4.7.9",
          error: "",
          detail: { asset_label: "Laptop 4" },
        },
        // The importer sends `asset_label: ""` for every accepted row whether
        // the column was mapped or not — "(empty)" on 200 lines hides the rows
        // that carry something.
        { line: 3, action: "create", address: "10.4.7.10", error: "", detail: { asset_label: "" } },
      ],
    });
    stubApi({ previewIpamImport: preview, applyIpamImport: vi.fn() });
    renderWithProviders(<ImportPage />);

    const input = (await screen.findByLabelText("CSV file")) as HTMLInputElement;
    await user.upload(
      input,
      new File(["address\n10.4.7.9\n"], "plan.csv", { type: "text/csv" }),
    );
    await user.selectOptions(await screen.findByLabelText("address"), "address");
    await user.click(screen.getByRole("button", { name: "Preview" }));

    // Scoped to the verdict table: the column-mapping selects above it list
    // the wire field names on purpose, and those are not what this asserts.
    const verdicts = within(await screen.findByRole("table"));
    expect(verdicts.getByText("Asset label")).toBeInTheDocument();
    expect(verdicts.getByText("Laptop 4")).toBeInTheDocument();
    // Neither the braces nor the quoted key reaches the cell.
    expect(verdicts.queryByText(/asset_label/)).not.toBeInTheDocument();
    expect(verdicts.queryByText(/[{}]/)).not.toBeInTheDocument();
    // And the row that carried nothing says so, rather than printing `{}`.
    expect(verdicts.getByText("Nothing further reported")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// The tree
// --------------------------------------------------------------------------

describe("the subnet tree", () => {
  it("renders a container without a utilization bar", async () => {
    stubApi({
      listIpamSubnets: vi
        .fn()
        .mockResolvedValue([subnet({ kind: "container", prefix: "10.0.0.0/8" })]),
      getIpamSubnetUtilization: vi.fn(),
    });
    renderWithProviders(<SubnetTreePage />);
    expect(await screen.findByText("Container, no addresses")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("indents a child under its derived parent", async () => {
    stubApi({
      listIpamSubnets: vi.fn().mockResolvedValue([
        subnet({ id: "a", prefix: "10.0.0.0/8", kind: "container", parent_id: null }),
        subnet({ id: "b", prefix: "10.4.7.0/24", parent_id: "a" }),
      ]),
      getIpamSubnetUtilization: vi.fn().mockResolvedValue(utilization()),
    });
    renderWithProviders(<SubnetTreePage />);
    const child = await screen.findByText("10.4.7.0/24");
    expect(child.parentElement?.getAttribute("style")).toContain("padding-left: 16px");
  });

  it("renders the last-swept column as an age, and `never` when it has none", async () => {
    stubApi({ listIpamSubnets: vi.fn().mockResolvedValue([subnet({ last_swept_at: null })]) });
    renderWithProviders(<SubnetTreePage />);
    expect(await screen.findByText("never")).toBeInTheDocument();
  });

  /**
   * EVERY FILTER CONTROL ON THIS ROW TAKES THE KIT'S GEOMETRY (design review
   * I5).
   *
   * The "contains" box carried the console's ONLY `wrapperStyle` --
   * `flex: 0 1 280px`. Below the kit's 640px fold a `.filterbar` puts one
   * control per line and the growing ones fill it; a box pinned to
   * `flex-grow: 0` stopped 44px short of its five siblings, so the stack had a
   * ragged right edge at 390.
   *
   * THE ASSERTION IS ON THE INLINE STYLE, not on a measured width. jsdom
   * computes no layout, so a width assertion here would pass against anything;
   * what is checkable, and what is actually the finding, is that this control
   * states no geometry of its own and therefore cannot differ from the box
   * beside it.
   */
  it("gives neither search box a geometry of its own, so the stack cannot go ragged", async () => {
    stubApi({ listIpamSubnets: vi.fn().mockResolvedValue([subnet()]) });
    const { container } = renderWithProviders(<SubnetTreePage />);
    await screen.findByLabelText("Filter by contained address");

    const wrappers = Array.from(container.querySelectorAll(".filterbar .srch-wrap"));
    expect(wrappers).toHaveLength(2);
    for (const wrapper of wrappers) {
      expect(wrapper.getAttribute("style")).toBeNull();
    }
  });
});

// --------------------------------------------------------------------------
// AUDITOR B [MEDIUM]: "KPI tiles read 0/0/0/0 with no subnets registered,
// presenting an empty install as a clean one ... 'With drift 0' and 'Never
// swept 0' read as healthy findings when nothing has been measured. In a
// drift-detection module this is the most misleading possible zero."
//
// The distinction under test is BETWEEN TWO ZEROS: a fleet whose one
// registered subnet has no drift (a measured, true zero, which must still be
// shown) and a fleet where nothing has been measured at all (no zero to
// report). The pre-cycle page rendered both as `With drift 0`.
// --------------------------------------------------------------------------

describe("the tile row does not report zeros it has not measured", () => {
  it("replaces the tiles with first-run guidance when no subnet is registered", async () => {
    stubApi({ listIpamSubnets: vi.fn().mockResolvedValue([]) });
    renderWithProviders(<SubnetTreePage />);

    await screen.findByText("No subnets registered yet.");
    expect(document.querySelectorAll(".stat-card")).toHaveLength(0);
    // Not merely absent — the page says WHICH of the two zeros this is.
    expect(screen.getByText(/no drift figure to report, not a drift figure of zero/i))
      .toBeInTheDocument();
    // Read off the TILE LABELS specifically (`.stat-label`): "Never swept" is
    // also an option in the view picker, which is present whether or not any
    // subnet exists. The claim under test is that no tile reports it.
    const labels = [...document.querySelectorAll(".stat-label")].map((el) => el.textContent);
    expect(labels).not.toContain("With drift");
    expect(labels).not.toContain("Never swept");
  });

  it("still reports a measured zero once one subnet exists", async () => {
    stubApi({
      listIpamSubnets: vi.fn().mockResolvedValue([subnet({ last_swept_at: null })]),
      listIpamDrift: vi.fn().mockResolvedValue([]),
    });
    renderWithProviders(<SubnetTreePage />);

    await screen.findByText("10.4.7.0/24");
    const cards = [...document.querySelectorAll(".stat-card")].map((card) => card.textContent);
    expect(cards).toHaveLength(4);
    expect(cards[2]).toMatch(/With drift.*0/s);
    expect(screen.queryByText(/no drift figure to report/i)).not.toBeInTheDocument();
  });

  it("says the filters are why there is nothing to total, not the fleet", async () => {
    const user = userEvent.setup();
    const list = vi.fn<ApiClient["listIpamSubnets"]>();
    list.mockResolvedValueOnce([subnet()]).mockResolvedValue([]);
    stubApi({ listIpamSubnets: list });
    renderWithProviders(<SubnetTreePage />);

    await screen.findByText("10.4.7.0/24");
    await user.type(screen.getByLabelText("Search subnets"), "nothing-matches");

    expect(await screen.findByText(/Clear the filters to see the whole plan of record/i))
      .toBeInTheDocument();
    expect(screen.queryByText(/no drift figure to report/i)).not.toBeInTheDocument();
  });

  it("claims nothing at all while the subnets are still being fetched", async () => {
    // A sentence saying nothing is registered is as false as a zero when the
    // truth is that nothing has been fetched.
    stubApi({ listIpamSubnets: vi.fn(() => new Promise<never>(() => {})) });
    renderWithProviders(<SubnetTreePage />);

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(document.querySelectorAll(".stat-card")).toHaveLength(0);
    expect(screen.queryByText(/no drift figure to report/i)).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// REVIEW WAVE ROUND 3: "The 'Never swept' tile carries the warn alert-triangle
// on a plain white card, immediately beside 'With drift' which is tinted amber
// for the same class of problem. The code sets tone='orange' on it, but
// tileKind only returns a kind when `loud` is also set, so the tone draws
// nothing at all."
//
// The tint is `Tile`'s kind class, so this asserts the class the CSS keys off
// rather than a computed colour, which jsdom does not have. It is a test about
// a DISTINCTION like every other in this file: the two tiles that count a
// backlog somebody has to work are tinted; the two that describe the estate are
// not; and neither tint survives a measured zero, which is the invariant the
// row above this one exists to protect.
// --------------------------------------------------------------------------

describe("the attention tiles are tinted, and only when they have counted something", () => {
  async function renderTiles(subnets: SubnetOut[], driftRows: DriftOut[]) {
    stubApi({
      listIpamSubnets: vi.fn().mockResolvedValue(subnets),
      listIpamDrift: vi.fn().mockResolvedValue(driftRows),
      getIpamSubnetUtilization: vi.fn().mockResolvedValue(utilization()),
    });
    renderWithProviders(<SubnetTreePage />);
    await screen.findByText("10.4.7.0/24");
    return [...document.querySelectorAll(".stat-card")].map((card) => ({
      text: card.textContent ?? "",
      classes: (card.className ?? "").split(/\s+/),
    }));
  }

  it("tints both With drift and Never swept amber when each has rows", async () => {
    const cards = await renderTiles([subnet({ last_swept_at: null })], [drift()]);

    expect(cards).toHaveLength(4);
    expect(cards[2]!.text).toMatch(/With drift.*1/s);
    expect(cards[3]!.text).toMatch(/Never swept.*1/s);
    // Same class of problem, same treatment. Before this fix the fourth tile
    // carried the warn glyph on a plain card.
    expect(cards[2]!.classes).toContain("warn");
    expect(cards[3]!.classes).toContain("warn");
    // Amber, never the incident red: nobody is paged by a documentation gap.
    expect(cards[2]!.classes).not.toContain("crit");
    expect(cards[3]!.classes).not.toContain("crit");
    // The two tiles that describe the estate rather than a backlog stay plain.
    expect(cards[0]!.classes).not.toContain("warn");
    expect(cards[1]!.classes).not.toContain("warn");
  });

  it("drops the wash from Never swept when the zero is a measured one", async () => {
    const cards = await renderTiles([subnet({ last_swept_at: "2026-08-19T00:00:00.000Z" })], []);

    expect(cards[3]!.text).toMatch(/Never swept.*0/s);
    expect(cards[3]!.classes).not.toContain("warn");
    expect(cards[3]!.text).toContain("Every subnet has been swept");
    expect(cards[2]!.classes).not.toContain("warn");
  });
});

// --------------------------------------------------------------------------
// "All tiles and all numbers or KPIs need to be clickable and direct the user
// to the specific menus, i dont want dead tiles everywhere" — the operator,
// 2026-08-21.
//
// TWO CLAIMS, AND THE SECOND IS THE ONE THAT ROTS. That a tile is a link is
// cheap to keep true; that the link LANDS ON THE ROWS IT COUNTED is what
// decays, because a `?state=` a page stops reading still renders a page and
// still looks fine. So each destination is opened and the rows behind it are
// asserted.
// --------------------------------------------------------------------------

describe("no dead tiles — /ipam", () => {
  it("points every tile at a route App.tsx serves", async () => {
    stubApi({
      listIpamSubnets: vi.fn().mockResolvedValue([subnet()]),
      listIpamDrift: vi.fn().mockResolvedValue([]),
    });
    renderWithProviders(<SubnetTreePage />);
    await screen.findByText("10.4.7.0/24");

    expectTileLinks(["/ipam", "/ipam?state=assignable", "/ipam/drift", "/ipam?state=never-swept"]);
  });

  it("shows only assignable subnets at the destination the Assignable tile names", async () => {
    stubApi({
      listIpamSubnets: vi.fn().mockResolvedValue([
        subnet({ id: "a", prefix: "10.0.0.0/8", kind: "container", parent_id: null }),
        subnet({ id: "b", prefix: "10.4.7.0/24", parent_id: "a" }),
      ]),
      listIpamDrift: vi.fn().mockResolvedValue([]),
      getIpamSubnetUtilization: vi.fn().mockResolvedValue(utilization()),
    });
    renderWithProviders(<SubnetTreePage />, "/ipam?state=assignable");

    expect(await screen.findByText("10.4.7.0/24")).toBeInTheDocument();
    // The container is the row this view exists to leave out. BREAKS AGAINST a
    // `?state=` the page renders and ignores.
    expect(screen.queryByText("10.0.0.0/8")).not.toBeInTheDocument();
    // Promoted to a root rather than left indented under an absent parent.
    expect(screen.getByText("10.4.7.0/24").parentElement?.getAttribute("style")).toContain(
      "padding-left: 0px",
    );
  });

  it("shows only never-swept subnets at the destination that tile names", async () => {
    stubApi({
      listIpamSubnets: vi.fn().mockResolvedValue([
        subnet({ id: "a", prefix: "10.4.7.0/24", last_swept_at: null }),
        subnet({ id: "b", prefix: "10.9.9.0/24", last_swept_at: "2026-08-19T00:00:00.000Z" }),
      ]),
      listIpamDrift: vi.fn().mockResolvedValue([]),
      getIpamSubnetUtilization: vi.fn().mockResolvedValue(utilization()),
    });
    renderWithProviders(<SubnetTreePage />, "/ipam?state=never-swept");

    expect(await screen.findByText("10.4.7.0/24")).toBeInTheDocument();
    expect(screen.queryByText("10.9.9.0/24")).not.toBeInTheDocument();
    // The tile row still counts the WHOLE plan of record: a number that
    // changed when you clicked it was lying before or is lying now.
    const cards = [...document.querySelectorAll(".stat-card")].map((card) => card.textContent);
    expect(cards[0]).toMatch(/Managed subnets.*2/s);
  });
});

describe("no dead tiles — /ipam/drift", () => {
  function stub(driftRows: DriftOut[], dormantRows: DriftOut[], orphans: OrphanOut[]) {
    return stubApi({
      listIpamDrift: vi.fn().mockImplementation((filters: { class?: string } = {}) =>
        Promise.resolve(filters.class === "dormant" ? dormantRows : driftRows),
      ),
      listIpamOrphans: vi.fn().mockResolvedValue(orphans),
    });
  }

  it("points every tile at a route App.tsx serves", async () => {
    stub([drift({ id: "a" })], [], []);
    renderWithProviders(<DriftPage />, "/ipam/drift");
    await screen.findByText("10.4.7.9");

    expectTileLinks(["/ipam/drift", "/ipam/drift?class=dormant", "/ipam/drift?class=orphans"]);
  });

  it("opens the dormant list at the destination the Dormant tile names", async () => {
    stub(
      [drift({ id: "a", address: "10.4.7.9" })],
      [drift({ id: "c", drift_class: "dormant", address: "10.4.7.99" })],
      [],
    );
    renderWithProviders(<DriftPage />, "/ipam/drift?class=dormant");

    // The dormant row, not the drift row — the tab came from the URL.
    expect(await screen.findByText("10.4.7.99")).toBeInTheDocument();
    expect(screen.queryByText("10.4.7.9")).not.toBeInTheDocument();
  });

  it("opens the orphan list at the destination the Orphans tile names", async () => {
    stub([], [], [{ prefix: "10.7.3.0/24", count: 24, last_seen_at: "2026-08-01T00:00:00.000Z" }]);
    renderWithProviders(<DriftPage />, "/ipam/drift?class=orphans");

    expect(await screen.findByText("10.7.3.0/24")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Register this subnet" })).toHaveAttribute(
      "href",
      "/ipam?register=1",
    );
  });
});

// --------------------------------------------------------------------------
// AUDITOR B [LOW]: "Six tabs on /ipam are unlabelled as a tab set ... plain
// buttons, no roles, nothing naming the group." WCAG 4.1.2. The strip is
// NAMED and its current item announced — but as what it is. It was answered
// with `role="tablist"`/`role="tab"`/`aria-selected`, which announced "tab,
// selected, 2 of 6" for six links that navigate: no tabpanel exists for them
// to control, and arrow-key movement within a tablist is neither implemented
// nor implementable here (each item is a destination). The review wave filed
// that back. It is navigation, so these assert navigation.
// --------------------------------------------------------------------------

describe("the IPAM strip is announced as the navigation it is", () => {
  it("exposes all six surfaces as links in a named nav", async () => {
    stubApi({});
    renderWithProviders(<SubnetTreePage />);

    const nav = await screen.findByRole("navigation", { name: "IPAM" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual([
      "Subnets",
      "Drift",
      "Search",
      "Registry",
      "Scan authorizations",
      "Import / export",
    ]);
  });

  it("marks exactly the current surface as the current page, and every other one not", async () => {
    stubApi({});
    renderWithProviders(<SubnetTreePage />);

    const nav = await screen.findByRole("navigation", { name: "IPAM" });
    const current = within(nav)
      .getAllByRole("link")
      .filter((link) => link.getAttribute("aria-current") === "page");
    expect(current.map((link) => link.textContent)).toEqual(["Subnets"]);
    // And no ARIA left over from the tablist it is not: a `role="tab"` outside
    // a tablist, or an `aria-selected` on a link, is the defect this replaced.
    expect(within(nav).queryAllByRole("tab")).toHaveLength(0);
    for (const link of within(nav).getAllByRole("link")) {
      expect(link).not.toHaveAttribute("aria-selected");
    }
  });

  it("draws the strip after the page head, never between the crumb and the h1", async () => {
    stubApi({});
    renderWithProviders(<SubnetTreePage />);

    const heading = await screen.findByRole("heading", { name: "IP address management" });
    const nav = screen.getByRole("navigation", { name: "IPAM" });
    // `DOCUMENT_POSITION_FOLLOWING` on the h1 => the nav comes after it.
    expect(heading.compareDocumentPosition(nav) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

// --------------------------------------------------------------------------
// Registering a subnet — the form kernel
// --------------------------------------------------------------------------

describe("registering a subnet", () => {
  // Auditor B (HIGH): "An impossible CIDR (999.999.999.999/99) is accepted by
  // the field and Register is a silent no-op" — the post-click DOM read
  // `{live: 0, err: [], "No subnets registered yet": true}`. Each test below
  // asserts the thing that read was missing: a click that produces a sentence
  // naming the field, and a request that is NOT sent.

  // Typed off the client rather than off `vi.fn()`: a mock with no
  // implementation infers as an untyped Procedure, and `stubApi` would then
  // accept a mock whose shape has nothing to do with `createIpamSubnet`.
  async function openTree(createIpamSubnet: Mock<ApiClient["createIpamSubnet"]>) {
    stubApi({ createIpamSubnet });
    const user = userEvent.setup();
    renderWithProviders(<SubnetTreePage />);
    // The form lives in the dialog the head's primary action opens (v3 draws
    // "New subnet" there); the fields and the button are what they were.
    await user.click(await screen.findByRole("button", { name: "New subnet" }));
    const register = await screen.findByRole("button", { name: "Register" });
    return { user, register };
  }

  // The Kind select offered the wire identifiers — `container`, `assignable` —
  // as its visible options. BREAKS AGAINST `{value}` in place of
  // `{humaniseEnum(value)}`.
  it("offers the Kind options as words while still submitting the wire value", async () => {
    const create = vi.fn<ApiClient["createIpamSubnet"]>();
    const { user, register } = await openTree(create);

    const kind = screen.getByLabelText("Kind");
    expect([...kind.querySelectorAll("option")].map((o) => o.textContent)).toEqual([
      "Container",
      "Assignable",
    ]);
    expect(screen.queryByRole("option", { name: "assignable" })).not.toBeInTheDocument();

    await user.selectOptions(kind, "container");
    await user.type(screen.getByPlaceholderText("10.4.7.0/24"), "10.0.0.0/8");
    await user.click(register);

    expect(create).toHaveBeenCalledWith(expect.objectContaining({ kind: "container" }));
  });

  it("names the missing Prefix on submit rather than sitting disabled", async () => {
    const create = vi.fn<ApiClient["createIpamSubnet"]>();
    const { user, register } = await openTree(create);

    // The disabled button WAS the defect: it moved nothing and named nothing.
    expect(register).not.toBeDisabled();
    await user.click(register);

    expect(await screen.findByRole("alert")).toHaveTextContent("Prefix is required");
    expect(create).not.toHaveBeenCalled();
  });

  it("refuses an impossible CIDR with the form it expects, and sends nothing", async () => {
    const create = vi.fn<ApiClient["createIpamSubnet"]>();
    const { user, register } = await openTree(create);

    await user.type(screen.getByPlaceholderText("10.4.7.0/24"), "999.999.999.999/99");
    await user.click(register);

    expect(await screen.findByRole("alert")).toHaveTextContent(/CIDR notation/);
    expect(await screen.findByRole("alert")).toHaveTextContent("10.4.7.0/24");
    expect(create).not.toHaveBeenCalled();
  });

  it("says which of /32 and /128 applies when the prefix length is out of range", async () => {
    const create = vi.fn<ApiClient["createIpamSubnet"]>();
    const { user, register } = await openTree(create);

    await user.type(screen.getByPlaceholderText("10.4.7.0/24"), "10.4.7.0/64");
    await user.click(register);

    expect(await screen.findByRole("alert")).toHaveTextContent("at most /32");
    expect(create).not.toHaveBeenCalled();
  });

  // A validator that rejected everything would satisfy the three above and be
  // a worse wall than the one it replaced, so: the literals the server takes
  // must get through. `ip_network(..., strict=False)` accepts host bits, an
  // IPv6 literal and a compressed one, so this form must too.
  it.each([
    ["10.4.7.9/24", "host bits, which the server normalises rather than rejects"],
    ["2001:db8::/32", "a compressed IPv6 prefix"],
    ["10.4.7.0", "a bare address, which is a host prefix"],
  ])("accepts %s (%s)", async (prefix) => {
    const create = vi.fn<ApiClient["createIpamSubnet"]>().mockResolvedValue(subnet());
    const { user, register } = await openTree(create);

    await user.type(screen.getByPlaceholderText("10.4.7.0/24"), prefix);
    await user.click(register);

    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(create.mock.calls[0]?.[0]).toMatchObject({ prefix });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("rejects a malformed Gateway, which is the same silent 422 as the prefix", async () => {
    const create = vi.fn<ApiClient["createIpamSubnet"]>();
    const { user, register } = await openTree(create);

    await user.type(screen.getByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24");
    await user.type(screen.getByPlaceholderText("10.4.7.1"), "10.4.7.999");
    await user.click(register);

    expect(await screen.findByRole("alert")).toHaveTextContent("Gateway must be an IP address");
    expect(create).not.toHaveBeenCalled();
  });

  it("submits on Enter, which it could not do before it was a real form", async () => {
    const create = vi.fn<ApiClient["createIpamSubnet"]>().mockResolvedValue(subnet());
    const { user } = await openTree(create);

    await user.type(screen.getByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24{Enter}");

    await waitFor(() => expect(create).toHaveBeenCalled());
  });

  it("renders a bodyless 503 as a sentence instead of swallowing it", async () => {
    // `new ApiError(503, "")` — the message is an empty string, which every
    // `{error && <span>{error.message}</span>}` call site rendered as nothing.
    const create = vi.fn<ApiClient["createIpamSubnet"]>().mockRejectedValue(new ApiError(503, ""));
    const { user, register } = await openTree(create);

    await user.type(screen.getByPlaceholderText("10.4.7.0/24"), "10.4.7.0/24");
    await user.click(register);

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
