/**
 * Devices, the first page migrated onto the design.
 *
 * The assertions worth having here are about the THREE STATES the prototype
 * has no concept of. A mock is always loaded, always populated and never
 * fails; a console is regularly all three, and before this migration the page
 * rendered an empty table body for "still loading", "nothing enrolled" and
 * "the request failed" alike. An operator staring at an empty fleet could not
 * tell a broken integration from a quiet one.
 *
 * Mocks the auth-backed API client, mirroring
 * `modules/error-monitor/ErrorMonitorPage.test.tsx`.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type {
  ApiClient,
  Device,
  DeviceListParams,
  DevicePage,
  DeviceState,
  Me,
} from "../../lib/api";
import { NO_DATE } from "../../lib/datetime";
import { DEVICES_PAGE_SIZE, DevicesPage } from "./DevicesPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
// The live-events socket is not what this file is about, and jsdom has no
// WebSocket worth opening.
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    hostname: "ws-alpha",
    fqdn: "ws-alpha.corp.local",
    os_family: "windows",
    os_version: "11 23H2",
    architecture: "amd64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: "2026-08-16T09:00:00Z",
    cert_state: null,
    // `false`, not absent: a device with no agent has no certificate to
    // revoke, and that is what the API sends for one.
    cert_revoked: false,
    // Asset fields (asset-inventory spec §2), defaulted to what every
    // agent-enrolled device is -- so the tests written before assets existed
    // still describe the same fleet.
    lifecycle: "in_service",
    asset_type: "computer",
    source: "agent",
    provenance: "agent",
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...overrides,
  } as Device;
}

/**
 * **THE FIXTURES DESCRIBE AN ESTATE; THIS TURNS ONE INTO A SERVER.**
 *
 * The page no longer filters, searches, pages or counts — `GET /devices` does
 * all four in SQL, because since 2026-08-26 a sweep records every host it finds
 * and the estate stopped fitting in the browser. So a test that hands the page
 * a fixed list and then asserts a filtered table would be asserting nothing:
 * the page would render the list it was given, whatever the filter said.
 *
 * `fakeServer` is one implementation of the wire contract, and every test in
 * this file goes through it. That makes the assertions below end-to-end again:
 * *if the page stopped sending `state`*, this returns the whole estate and the
 * tab tests fail; *if it stopped sending `q`*, the search tests fail; *if it
 * recomputed a count locally*, its number stops matching the one served.
 * `test_devices_list.py` pins the same semantics against real Postgres — that
 * is what stops this fake from being a second, divergent definition.
 */
function certAttention(device: Device): boolean {
  return (
    device.cert_revoked ||
    device.cert_state === "expiring" ||
    device.cert_state === "expired"
  );
}

function matchesSearch(device: Device, term: string): boolean {
  return [
    device.hostname ?? "",
    device.fqdn ?? "",
    device.description ?? "",
    device.primary_ip ?? "",
    `${device.os_family ?? ""} ${device.os_version}`,
    device.architecture,
    // Who holds it -- which the server searches only for a `people.read`
    // caller, and only then sends at all, so a fixture carrying a holder is
    // already a fixture for that caller.
    device.assigned_to?.display_name ?? "",
  ]
    .join(" ")
    .toLowerCase()
    .includes(term.toLowerCase());
}

function inState(device: Device, state: DeviceState): boolean {
  if (state === "online") return device.agent_status === "online";
  if (state === "offline") return device.agent_status === "offline";
  if (state === "assets") return device.agent_status === null;
  if (state === "certs") return certAttention(device);
  return true;
}

function fakeServer(load: () => Promise<unknown>) {
  return async (params: DeviceListParams = {}): Promise<DevicePage> => {
    const estate = ((await load()) as { items: Device[] }).items;
    const scoped = estate.filter(
      (device) =>
        (!params.q || matchesSearch(device, params.q)) &&
        (!params.provenance || device.provenance === params.provenance),
    );
    const state = params.state ?? "all";
    const rows = scoped.filter((device) => inState(device, state));
    const offset = params.offset ?? 0;
    return {
      items: rows.slice(offset, offset + (params.limit ?? 100)),
      // `total` is the count for the ACTIVE tab, exactly as the server defines
      // it -- it is `state_counts[state]`, never counted a second time.
      total: rows.length,
      state_counts: {
        all: scoped.length,
        online: scoped.filter((d) => inState(d, "online")).length,
        offline: scoped.filter((d) => inState(d, "offline")).length,
        assets: scoped.filter((d) => inState(d, "assets")).length,
        certs: scoped.filter((d) => inState(d, "certs")).length,
      },
      // NO FILTER AT ALL. The stat tiles are claims about the estate.
      fleet: {
        total: estate.length,
        managed: estate.filter((d) => d.agent_status !== null).length,
        online: estate.filter((d) => d.agent_status === "online").length,
        cert_attention: estate.filter(certAttention).length,
      },
    };
  };
}

/** No profile zone by default -- the page must work before `/me` resolves. */
function renderPage(
  listDevices: () => Promise<unknown>,
  me: Partial<Me> = {},
  path = "/devices",
  // The rest of the client this page reaches for. `exportInventory` is the
  // only one so far, and it is here rather than in the literal above because
  // most of these tests do not touch it and a stub they never call would say
  // nothing about the ones that do.
  extra: Partial<ApiClient> = {},
) {
  mockUseApi.mockReturnValue({
    listDevices: fakeServer(listDevices),
    me: vi.fn().mockResolvedValue({ permissions: [], ...me } as Me),
    ...extra,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/devices" element={<DevicesPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("DevicesPage", () => {
  it("says it is loading rather than showing an empty fleet", async () => {
    renderPage(() => new Promise<never>(() => {}));
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty fleet", async () => {
    // The distinction this whole state machine exists for: a dead backend and
    // a greenfield deployment must not look identical.
    renderPage(() => Promise.reject(new Error("boom")));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No devices enrolled/i)).not.toBeInTheDocument();
  });

  it("says the fleet is empty when it is genuinely empty", async () => {
    renderPage(() => Promise.resolve({ items: [], total: 0 }));
    expect(await screen.findByText("No devices enrolled yet.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists devices with the design's structure", async () => {
    renderPage(() =>
      Promise.resolve({
        items: [
          device(),
          // A DISTINCT fqdn, not the fixture default. Two rows sharing one
          // made `getByText` match both -- and a value constant across every
          // fixture is untested by construction, which is this project's
          // most-repeated defect.
          device({ id: "2", hostname: "srv-beta", fqdn: "srv-beta.dc.local" }),
        ],
        total: 2,
      }),
    );
    expect(await screen.findByText("ws-alpha")).toBeInTheDocument();
    expect(screen.getByText("srv-beta")).toBeInTheDocument();
    expect(screen.getByText("ws-alpha.corp.local")).toBeInTheDocument();
    // The page head, from the design, verbatim: crumb (the shell's), then
    // the h1, then the one-line sub. v3 draws no eyebrow between them.
    expect(screen.getByRole("heading", { name: "Devices" })).toBeInTheDocument();
    expect(screen.queryByText("Fleet & inventory")).not.toBeInTheDocument();
  });

  it("counts from the data rather than from a constant", async () => {
    // The prototype's stat cards read 286 / 272 / 18 / 94%. Those are mock
    // numbers, and a card that keeps them would be wrong on every deployment.
    renderPage(() =>
      Promise.resolve({
        items: [
          device(),
          device({ id: "2", hostname: "b", agent_status: "offline" }),
          device({ id: "3", hostname: "c", cert_state: "expiring" }),
        ],
        total: 3,
      }),
    );
    await screen.findByText("ws-alpha");
    const stats = document.querySelectorAll(".stat-card");
    const values = [...stats].map((card) => card.querySelector(".stat-value")?.textContent);
    // Managed / Assets / Online / Certificate attention. The second card was
    // added with agentless assets: all three rows here HAVE an agent, so
    // managed and total agree -- and the case where they must NOT is asserted
    // separately below.
    expect(values).toEqual(["3", "3", "2", "1"]);
    expect(values).not.toContain("286");
  });

  it("filters by tab", async () => {
    renderPage(() =>
      Promise.resolve({
        items: [device(), device({ id: "2", hostname: "srv-beta", agent_status: "offline" })],
        total: 2,
      }),
    );
    await screen.findByText("ws-alpha");
    await userEvent.click(screen.getByRole("tab", { name: /Offline/ }));
    expect(screen.getByText("srv-beta")).toBeInTheDocument();
    expect(screen.queryByText("ws-alpha")).not.toBeInTheDocument();
  });

  it("filters by search across hostname and fqdn", async () => {
    renderPage(() =>
      Promise.resolve({
        items: [device(), device({ id: "2", hostname: "srv-beta", fqdn: "srv-beta.dc.local" })],
        total: 2,
      }),
    );
    await screen.findByText("ws-alpha");
    await userEvent.type(screen.getByLabelText("Search devices"), "dc.local");
    expect(screen.getByText("srv-beta")).toBeInTheDocument();
    expect(screen.queryByText("ws-alpha")).not.toBeInTheDocument();
    // And the empty state distinguishes "no match" from "none enrolled", and
    // quotes the term that produced it.
    await userEvent.clear(screen.getByLabelText("Search devices"));
    await userEvent.type(screen.getByLabelText("Search devices"), "zzzz");
    expect(screen.getByText("No devices match \u201Czzzz\u201D.")).toBeInTheDocument();
  });

  /**
   * "[MEDIUM] Tab counts are absolute totals and contradict the visible result
   * set when a search is active" — the strip read "All devices 1" directly
   * above the empty state "No devices match that filter."
   */
  it("counts the tabs against the active search, not the whole fleet", async () => {
    renderPage(() =>
      Promise.resolve({
        items: [
          device(),
          device({ id: "2", hostname: "srv-beta", fqdn: "srv-beta.dc.local", agent_status: "offline" }),
        ],
        total: 2,
      }),
    );
    await screen.findByText("ws-alpha");
    const tabs = () => screen.getByRole("tablist");
    expect(within(tabs()).getByRole("tab", { name: /All devices/ })).toHaveTextContent("2");

    await userEvent.type(screen.getByLabelText("Search devices"), "dc.local");

    expect(within(tabs()).getByRole("tab", { name: /All devices/ })).toHaveTextContent("1");
    // And the tab for the rows the search excluded drops to zero rather than
    // advertising a device the table will not show.
    expect(within(tabs()).getByRole("tab", { name: /Online/ })).toHaveTextContent("0");
  });

  /**
   * "[MEDIUM] No-results empty state offers no way to clear the search … the
   * user must manually select the text and delete it. With a 500-character
   * value the input scrolls horizontally and shows only the trailing
   * characters, making manual clearing harder still."
   */
  it("offers a way out of a search, and only when there is one to take", async () => {
    renderPage(() => Promise.resolve({ items: [device()], total: 1 }));
    await screen.findByText("ws-alpha");
    expect(screen.queryByRole("button", { name: "Clear search" })).not.toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Search devices"), "zzzz");
    await userEvent.click(screen.getByRole("button", { name: "Clear search" }));

    expect(screen.getByText("ws-alpha")).toBeInTheDocument();
    expect((screen.getByLabelText("Search devices") as HTMLInputElement).value).toBe("");
  });

  it("does not paste 500 characters of search term into the empty state", async () => {
    // The audit typed exactly that. Echoing it verbatim pushes the message —
    // and the way out of it — off the card.
    renderPage(() => Promise.resolve({ items: [device()], total: 1 }));
    await screen.findByText("ws-alpha");
    // Pasted rather than typed: 500 keystrokes prove nothing extra.
    const box = screen.getByLabelText("Search devices");
    await userEvent.click(box);
    await userEvent.paste("z".repeat(500));

    const empty = screen.getByText(/No devices match/);
    expect(empty.textContent!.length).toBeLessThan(120);
    expect(empty.textContent).toContain("\u2026");
  });

  it("shows no fabricated column the API cannot fill", async () => {
    // The design's table has Site, Patch, Backup and Risk columns with no
    // field behind them. Omitted rather than faked -- a screen that looks
    // finished and lies is worse than one that is visibly incomplete, and it
    // lies most convincingly on the demo where somebody decides to trust it.
    renderPage(() => Promise.resolve({ items: [device()], total: 1 }));
    await screen.findByText("ws-alpha");
    const head = within(document.querySelector("thead") as HTMLElement);
    expect(head.queryByText("Site")).not.toBeInTheDocument();
    expect(head.queryByText("Patch")).not.toBeInTheDocument();
    expect(head.queryByText("Backup")).not.toBeInTheDocument();
    expect(head.queryByText("Risk")).not.toBeInTheDocument();
    // Non-vacuity: the columns that DO have data are present.
    expect(head.getByText("Device")).toBeInTheDocument();
    expect(head.getByText("Certificate")).toBeInTheDocument();
  });
});

/**
 * Assets: fleet rows with no agent (asset-inventory spec §2, §5, §9).
 *
 * These are the same standard the column tests above enforce, applied to a row
 * that is not a machine. An asset must be visible in the fleet list AND must
 * not borrow the vocabulary of one -- "offline" is a claim about something that
 * should be answering, and a switch is not offline.
 */
describe("assets with no agent", () => {
  it("counts managed endpoints and total assets as different numbers", async () => {
    // The spec's §9 question, answered in the console. "Managed endpoints"
    // must mean "has an agent": counting rows was correct until a row could be
    // warehouse stock, at which point the card quietly means something else
    // while reading exactly the same.
    renderPage(() =>
      Promise.resolve({
        items: [
          device({ id: "1", agent_status: "online" }),
          device({
            id: "2",
            hostname: "core-sw-01",
            agent_status: null,
            source: "manual",
            asset_type: "network",
            lifecycle: "unmanageable",
          }),
        ],
        total: 2,
      }),
    );
    await screen.findByText("core-sw-01");
    const values = [...document.querySelectorAll(".stat-value")].map((n) => n.textContent);
    expect(values[0]).toBe("1");
    expect(values[1]).toBe("2");
  });

  it("says an asset has no agent rather than showing it as offline", async () => {
    renderPage(() =>
      Promise.resolve({
        items: [device({ id: "2", hostname: "core-sw-01", agent_status: null })],
        total: 1,
      }),
    );
    const row = (await screen.findByText("core-sw-01")).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText(/no agent/i)).toBeInTheDocument();
    // Scoped to the ROW: "Offline" is also a legitimate tab label, and a bare
    // document-wide query would pass whatever the row said.
    expect(within(row as HTMLElement).queryByText(/offline/i)).not.toBeInTheDocument();
  });

  it("identifies an asset with no hostname by something real", async () => {
    renderPage(() =>
      Promise.resolve({
        items: [
          device({
            id: "3",
            hostname: null,
            fqdn: null,
            os_family: null,
            agent_status: null,
            description: "boxed laptop, PO 4471",
            primary_ip: "172.22.104.55",
          }),
        ],
        total: 1,
      }),
    );
    // The IP is the identity this row actually has. "None" and an empty cell
    // are not identities, and the first of the two reads as a hostname.
    expect(await screen.findByText("172.22.104.55")).toBeInTheDocument();
    expect(screen.queryByText("None")).not.toBeInTheDocument();
  });

  it("still shows a managed device as online", async () => {
    // Non-vacuity: a page that labelled every row "no agent" would pass the
    // second test above and tell an operator nothing about the real fleet.
    renderPage(() =>
      Promise.resolve({ items: [device({ agent_status: "online" })], total: 1 }),
    );
    const row = (await screen.findByText("ws-alpha")).closest("tr");
    // Scoped to the ROW: "No agent" is also a tab label now, so a
    // document-wide query would fail whatever the row said.
    expect(within(row as HTMLElement).queryByText(/no agent/i)).not.toBeInTheDocument();
    expect(within(row as HTMLElement).getByText(/online/i)).toBeInTheDocument();
  });
});

/**
 * Last seen, in the zone the signed-in user works in (UX remediation cycle 5).
 *
 * The audit's BLOCKER was "every datetime in the product is displayed 5 hours
 * earlier than it was entered" -- five being the operator's offset from UTC.
 * Reading the BROWSER's zone is right on the developer's laptop and wrong
 * everywhere else, so these assertions fix a profile zone that is not the
 * runner's and check the wall clock it implies.
 */
describe("last seen", () => {
  it("renders in the profile's zone, not the browser's", async () => {
    // 09:00Z in August is 05:00 in New York (EDT, UTC-4). Under the runner's
    // TZ=UTC a page reading the browser zone renders 09:00.
    renderPage(
      () => Promise.resolve({ items: [device()], total: 1 }),
      { timezone: "America/New_York" },
    );
    await screen.findByText("ws-alpha");
    // Relative in the cell, the instant on hover.
    expect(await screen.findByTitle("16 Aug 2026, 05:00")).toBeInTheDocument();
    expect(screen.queryByTitle("16 Aug 2026, 09:00")).not.toBeInTheDocument();
  });

  it("puts the name, status and last contact first, which is what fits on a phone", async () => {
    // At 390px the Status column was off-screen (review 2026-09-23).
    renderPage(() => Promise.resolve({ items: [device()], total: 1 }));
    await screen.findByText("ws-alpha");
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers.slice(0, 3)).toEqual(["Device", "Agent", "Last seen"]);
    // Not monospace: a date is prose.
    expect(screen.getByTitle(/2026/).closest("td")).not.toHaveClass("mono");
  });

  it("falls back to the browser zone when the profile carries none", async () => {
    // NON-VACUITY, and the real deployment case: `timezone` is nullable, and a
    // technician who has not set one must still get a time rather than an
    // empty cell.
    renderPage(() => Promise.resolve({ items: [device()], total: 1 }), {
      timezone: null,
    });
    await screen.findByText("ws-alpha");
    expect(await screen.findByTitle("16 Aug 2026, 09:00")).toBeInTheDocument();
  });

  it("says a device has never been seen rather than showing Invalid Date", async () => {
    // An asset in a warehouse has contacted nothing. `new Date(null)` is the
    // epoch and `new Date(undefined)` is "Invalid Date"; neither is a fact.
    renderPage(
      () =>
        Promise.resolve({
          items: [device({ id: "9", hostname: "boxed-01", agent_status: null, last_seen_at: null })],
          total: 1,
        }),
      { timezone: "America/New_York" },
    );
    const row = (await screen.findByText("boxed-01")).closest("tr") as HTMLElement;
    // THE LAST SEEN CELL specifically (the third: device, agent, last seen).
    // The certificate column also renders an em dash for a row with no certificate, so a document-
    // or row-wide query for NO_DATE would pass whatever this cell said.
    const cells = [...row.querySelectorAll("td")];
    expect(cells[2]?.textContent).toBe(NO_DATE);
    expect(document.body.textContent).not.toMatch(/Invalid Date/);
    expect(document.body.textContent).not.toMatch(/1970/);
  });

  /**
   * "[MEDIUM] Filter tabs do not change the URL, so filtered views cannot be
   * linked, bookmarked or restored … Reloading returns to 'All devices'; the
   * browser Back button does not undo the tab change."
   */
  describe("the filter state is in the URL", () => {
    function Where() {
      const location = useLocation();
      return <output data-testid="url">{`${location.pathname}${location.search}`}</output>;
    }

    function renderWithUrl(path: string) {
      mockUseApi.mockReturnValue({
        listDevices: fakeServer(() =>
          Promise.resolve({
            items: [
              device(),
              device({
                id: "2",
                hostname: "srv-beta",
                fqdn: "srv-beta.dc.local",
                agent_status: "offline",
              }),
            ],
            total: 2,
          }),
        ),
        me: vi.fn().mockResolvedValue({ permissions: [] } as unknown as Me),
      } as unknown as ApiClient);
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      return render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route
                path="/devices"
                element={
                  <>
                    <DevicesPage />
                    <Where />
                  </>
                }
              />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>,
      );
    }

    it("puts the chosen tab in the URL", async () => {
      renderWithUrl("/devices");
      await screen.findByText("ws-alpha");
      await userEvent.click(screen.getByRole("tab", { name: /Offline/ }));

      expect(screen.getByTestId("url")).toHaveTextContent("/devices?state=offline");
    });

    it("restores a tab and a search term from the URL on load", async () => {
      // The link a colleague was sent: the offline machines matching "beta".
      renderWithUrl("/devices?state=offline&q=beta");
      expect(await screen.findByText("srv-beta")).toBeInTheDocument();
      expect(screen.queryByText("ws-alpha")).not.toBeInTheDocument();
      expect((screen.getByLabelText("Search devices") as HTMLInputElement).value).toBe("beta");
    });

    it("drops the parameter rather than carrying an empty filter", async () => {
      // `?q=` reads as a filter set to nothing, which is not the same claim as
      // no filter at all — and it is what gets pasted into a ticket.
      renderWithUrl("/devices?q=beta");
      await screen.findByText("srv-beta");
      await userEvent.click(screen.getByRole("button", { name: "Clear search" }));

      expect(screen.getByTestId("url")).toHaveTextContent("/devices");
      expect(screen.getByTestId("url").textContent).not.toContain("q=");
    });
  });
});

/**
 * The accessibility long tail — UX remediation cycle 12.
 *
 * Four LOW/MEDIUM findings that share one property: each is a single missing
 * attribute, each was filed against this page specifically, and each is
 * invisible to anybody testing with a mouse. They are grouped so the next
 * person changing this table can see in one place what the markup owes.
 */
describe("what the device table owes assistive technology", () => {
  const one = () => Promise.resolve({ items: [device()], total: 1 });

  it("declares every column header as a column header", async () => {
    // "[LOW] Table header cells omit `scope='col'` … Without `scope`,
    // assistive technology has to guess the header-to-cell association."
    renderPage(one);
    await screen.findByText("ws-alpha");
    const headers = [...document.querySelectorAll("thead th")];
    expect(headers).toHaveLength(8);
    for (const header of headers) expect(header).toHaveAttribute("scope", "col");
  });

  it("names the device each row action belongs to", async () => {
    // "[LOW] … a screen-reader user tabbing the column hears 'Run job, Run
    // job, Run job…' with no indication of which device each belongs to."
    renderPage(() =>
      Promise.resolve({
        items: [device(), device({ id: "2", hostname: "srv-beta" })],
        total: 2,
      }),
    );
    await screen.findByText("ws-alpha");
    expect(screen.getByRole("button", { name: "Run job on ws-alpha" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run job on srv-beta" })).toBeInTheDocument();
    // The VISIBLE text is unchanged, exactly as the finding asks.
    expect(
      screen.getByRole("button", { name: "Run job on ws-alpha" }).textContent,
    ).toContain("Run job");
  });

  // A signed job dispatches over the agent's own connection. A row recorded
  // by a sweep -- a printer, a switch, stock in a box -- has no agent to
  // dispatch to, so the control has nothing it could do and is not offered.
  it("does not offer Run job on an asset with no agent", async () => {
    renderPage(() =>
      Promise.resolve({
        items: [device(), device({ id: "2", hostname: "sw-core-1", agent_status: null })],
        total: 2,
      }),
    );
    await screen.findByText("ws-alpha");

    const agentRow = screen.getByText("ws-alpha").closest("tr") as HTMLElement;
    expect(within(agentRow).getByRole("button", { name: /Run job/ })).toBeInTheDocument();

    const agentlessRow = screen.getByText("sw-core-1").closest("tr") as HTMLElement;
    expect(within(agentlessRow).queryByRole("button", { name: /Run job/ })).not.toBeInTheDocument();
  });

  it("gives the tab strip a panel to control", async () => {
    // "[MEDIUM] Tabs are missing `aria-controls`/`tabpanel` wiring … no
    // element on the page has `role='tabpanel'` (count = 0), every tab has
    // `aria-controls: null`."
    renderPage(one);
    await screen.findByText("ws-alpha");

    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("id", "devices-tabpanel");
    const tabs = screen.getAllByRole("tab");
    // Six since the install link added "Waiting for review" (2026-09-28).
    expect(tabs).toHaveLength(6);
    for (const tab of tabs) expect(tab).toHaveAttribute("aria-controls", "devices-tabpanel");
  });

  it("keeps the whole tab strip to one tab stop", async () => {
    // The other half of the same finding: "all four tabs have `tabIndex: 0`
    // rather than the roving `0/-1` pattern", so keyboard users had to press
    // Tab four times to get past the strip.
    renderPage(one);
    await screen.findByText("ws-alpha");
    const reachable = screen
      .getAllByRole("tab")
      .filter((tab) => tab.getAttribute("tabindex") !== "-1");
    expect(reachable).toHaveLength(1);
    expect(reachable[0]).toHaveAttribute("aria-selected", "true");
  });

  it("gives the page headings below the H1 to navigate by", async () => {
    // "[LOW] Page has a single `<h1>` and no section headings … With one
    // heading per page, the heading shortcut is useless."
    renderPage(one);
    await screen.findByText("ws-alpha");
    expect(screen.getByRole("heading", { level: 1, name: "Devices" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 2 }).length).toBeGreaterThanOrEqual(2);
  });

  it("bounds the search box rather than accepting a pasted document", async () => {
    // "[LOW] Search input has no maxlength and accepts 500+ characters."
    renderPage(one);
    await screen.findByText("ws-alpha");
    expect(screen.getByLabelText("Search devices")).toHaveAttribute("maxlength", "128");
  });
});

/**
 * NO DEAD TILES.
 *
 * "all tiles and all numbers or kpis need to be clickable and direct the user
 * to the specific menus, i dont want dead tiles everywhere". `StatCard`'s
 * required `to` makes a tile without a destination a compile error; what the
 * type cannot check is whether the destination is the RIGHT one, and a tile
 * carrying `?state=certs` while the page filters on some other param would
 * look perfect and go nowhere useful. So the second test below follows the
 * link and reads the table it landed on.
 */
describe("the stat tiles are links to the rows they counted", () => {
  /** The tile with this label. `findByText` cannot be used: "Certificate
   *  attention" is also a TAB, which is rather the point -- the tile links to
   *  the tab. */
  function tile(label: string): HTMLElement {
    const card = [...document.querySelectorAll(".stat-card")].find(
      (element) => element.querySelector(".stat-label")?.textContent === label,
    );
    if (!card) throw new Error(`no stat card labelled ${label}`);
    return card as HTMLElement;
  }

  const fleet = () =>
    Promise.resolve({
      items: [
        device(),
        device({ id: "2", hostname: "offline-box", agent_status: "offline" }),
        device({ id: "3", hostname: "cert-box", cert_state: "expiring" }),
      ],
      total: 3,
    });

  it("gives every tile a real href", async () => {
    renderPage(fleet);
    await screen.findByText("ws-alpha");
    const tiles = [...document.querySelectorAll(".stat-card")].map((card) => [
      card.querySelector(".stat-label")?.textContent,
      card.tagName,
      card.getAttribute("href"),
    ]);
    expect(tiles).toEqual([
      // No `managed`-only tab exists and "Assets" is the whole estate, so both
      // go to the unfiltered list -- which is what clears a filter rather than
      // a route this page does not read.
      ["Managed endpoints", "A", "/devices"],
      ["Assets", "A", "/devices"],
      ["Online now", "A", "/devices?state=online"],
      ["Certificate attention", "A", "/devices?state=certs"],
    ]);
  });

  it("lands the certificate tile on exactly the rows it counted", async () => {
    const user = userEvent.setup();
    renderPage(fleet);
    await screen.findByText("ws-alpha");
    await user.click(tile("Certificate attention"));
    // One row in, two rows gone: the tile said 1 and the list now shows that 1.
    expect(await screen.findByText("cert-box")).toBeInTheDocument();
    expect(screen.queryByText("ws-alpha")).not.toBeInTheDocument();
    expect(screen.queryByText("offline-box")).not.toBeInTheDocument();
  });

  it("says which zero a certificate count of nothing is", async () => {
    renderPage(() => Promise.resolve({ items: [device()], total: 1 }));
    await screen.findByText("ws-alpha");
    const card = tile("Certificate attention");
    expect(within(card).getByText("Nothing revoked, expiring or expired")).toBeInTheDocument();
    // Weight is urgency, and a measured zero is not urgent -- "0 expiring" in
    // an alarm colour reads as a perfect score.
    expect(card.className).not.toContain("urgent");
  });

  /* --- getting the first agent onto a machine --------------------------- */

  it("offers one way to add a computer, the same one the top bar offers", async () => {
    // The review (2026-09-23) counted six install entry points. "Enroll
    // device" and "Add devices" were two of them on this head alone.
    renderPage(() => Promise.resolve({ items: [], total: 0 }));
    const add = await screen.findByRole("link", { name: "Add a computer" });
    expect(add).toHaveAttribute("href", "/devices/onboard");
    expect(screen.queryByRole("link", { name: "Enroll device" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Add devices" })).toBeNull();
  });

  /* --- getting the estate out of the console --------------------------- */

  it("exports through the API client, which is the only caller that is signed in", async () => {
    // THE DEFECT, AND IT MEANS THE EXPORT HAD NEVER WORKED. This was
    // `<a className="btn" href="/api/v1/export/devices.csv" download>`. The
    // browser follows a link on its own and sends no `Authorization` header,
    // so the route answered 401 and the browser saved the refusal body --
    // "export inventory exports a .json instead of an .xlsx", reported from
    // the deployed console. Nothing about it was intermittent; a link cannot
    // carry a bearer token.
    const exportInventory = vi.fn().mockResolvedValue(new Blob(["hostname\n"]));
    Object.assign(URL, {
      createObjectURL: vi.fn().mockReturnValue("blob:devices"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    renderPage(() => Promise.resolve({ items: [], total: 0 }), {}, "/devices", {
      exportInventory,
    });

    // NOT A LINK. `getByRole("link")` would still find the old anchor, so the
    // absence is asserted as well as the presence.
    expect(screen.queryByRole("link", { name: /export/i })).toBeNull();
    await userEvent.click(await screen.findByRole("button", { name: "Export inventory" }));
    expect(exportInventory).toHaveBeenCalledWith("devices");
  });

  it("says the export failed rather than saving the failure", async () => {
    // The old anchor's failure mode was silent by construction: the browser
    // saved the 401 body and the console never knew. A refusal has to reach
    // the operator as a sentence, through the same kernel every other failure
    // in the product goes through.
    const exportInventory = vi.fn().mockRejectedValue(new ApiError(403, ""));
    renderPage(() => Promise.resolve({ items: [], total: 0 }), {}, "/devices", {
      exportInventory,
    });
    await userEvent.click(await screen.findByRole("button", { name: "Export inventory" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /You do not have permission to do that/i,
    );
  });

  it("keeps exactly one primary action on the page", async () => {
    // DESIGN.md, Commit Magenta: "Reserved for the primary action, and there
    // is normally exactly one on a screen." Adding a second magenta button
    // would have made the new affordance compete with "Add asset" rather than
    // rank above it, so the primary MOVED rather than multiplied -- which is
    // also the ranking the design file itself gives this head.
    //
    const { container } = renderPage(() => Promise.resolve({ items: [], total: 0 }));
    await screen.findByRole("link", { name: "Add a computer" });
    const primaries = [...container.querySelectorAll(".btn.primary")].map(
      (el) => el.textContent?.trim(),
    );
    expect(primaries).toEqual(["Add a computer"]);
    // The secondary one is still THERE, which is the half a count cannot say.
    expect(screen.getByRole("button", { name: "Add asset" })).toBeTruthy();
  });
});

/**
 * A REVOKED AGENT CERTIFICATE, WHICH WAS ON THE WIRE AND NOWHERE ON THE PAGE.
 *
 * `GET /devices` has carried `cert_revoked` since the enrolment module learned
 * to revoke, and this page had no column, no badge and no filter for it. That
 * is not a cosmetic gap. `cert_state` is derived from `cert_not_after` ALONE
 * (`backend/app/api/v1/devices.py`), so a revoked certificate reports `ok`
 * right up to its natural expiry — and the fleet list showed a healthy pill
 * for a machine that can no longer open a connection to the control plane at
 * all. The console was not silent about revocation; it was actively wrong.
 *
 * HOW IT RELATES TO WHAT THE TAB ALREADY SHOWS. "Certificate attention" is the
 * set of certificates the platform is worried about, and revocation is the
 * third and worst member of it: `expiring` is a deadline, `expired` is a
 * deadline that passed, and `revoked` has already happened and cannot lapse
 * back. It joins that tab rather than getting a tab of its own — an operator
 * asking "which machines have certificate trouble" wants one list, and two
 * near-identical tabs is how the same question gets two different answers.
 *
 * ONE PREDICATE FEEDS THE TILE, THE TAB COUNT AND THE ROW FILTER. That is the
 * whole point of the last test here: a tile disagreeing with the table under
 * it is a defect this page has already produced twice, and it cannot recur
 * while the three read the same function.
 */
describe("revoked certificates", () => {
  const revoked = () =>
    device({
      id: "4",
      hostname: "ws-retired",
      // `ok` AND revoked, which is exactly the combination the server sends
      // and exactly the one the page used to render as healthy.
      cert_state: "ok",
      cert_revoked: true,
    });

  function certTile(): HTMLElement {
    const card = [...document.querySelectorAll(".stat-card")].find(
      (element) => element.querySelector(".stat-label")?.textContent === "Certificate attention",
    );
    if (!card) throw new Error("no certificate tile");
    return card as HTMLElement;
  }

  it("says revoked in the certificate column, not the expiry state that reads as fine", async () => {
    renderPage(() => Promise.resolve({ items: [revoked()], total: 1 }));
    const row = (await screen.findByText("ws-retired")).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("Revoked")).toBeInTheDocument();
    // THE FALSIFIER. Rendering `cert_state` beside the revocation would leave
    // an "Ok" pill on the row, which is the defect rather than a second fact:
    // the derived state knows nothing about revocation and is not true here.
    expect(within(row as HTMLElement).queryByText("Ok")).not.toBeInTheDocument();
  });

  it("counts a revocation as certificate attention", async () => {
    renderPage(() =>
      Promise.resolve({
        items: [device(), revoked(), device({ id: "3", hostname: "c", cert_state: "expiring" })],
        total: 3,
      }),
    );
    await screen.findByText("ws-alpha");
    expect(certTile().querySelector(".stat-value")?.textContent).toBe("2");
  });

  it("puts the revoked machine in the tab the tile links to", async () => {
    const user = userEvent.setup();
    renderPage(() =>
      Promise.resolve({
        items: [device(), revoked(), device({ id: "3", hostname: "c", cert_state: "expiring" })],
        total: 3,
      }),
    );
    await screen.findByText("ws-alpha");
    await user.click(certTile());
    expect(await screen.findByText("ws-retired")).toBeInTheDocument();
    expect(screen.getByText("c")).toBeInTheDocument();
    expect(screen.queryByText("ws-alpha")).not.toBeInTheDocument();
  });

  it("leaves `renewing` alone, because that is the healthy automatic path", async () => {
    // Revocation widens this set by one state, not by three. Surfacing the
    // rotation the platform performs by itself would train operators to ignore
    // the tab that also carries `expired` and now `revoked`.
    renderPage(() =>
      Promise.resolve({
        items: [device({ id: "5", hostname: "rotating", cert_state: "renewing" })],
        total: 1,
      }),
    );
    await screen.findByText("rotating");
    expect(certTile().querySelector(".stat-value")?.textContent).toBe("0");
  });

  it("names revocation in the zero label, so the zero says what was looked for", async () => {
    renderPage(() => Promise.resolve({ items: [device()], total: 1 }));
    await screen.findByText("ws-alpha");
    expect(
      within(certTile()).getByText("Nothing revoked, expiring or expired"),
    ).toBeInTheDocument();
  });

  it("makes the tile's number and the tab's rows the same arithmetic", async () => {
    // THE DEFECT THIS CYCLE HAS ALREADY PRODUCED TWICE, asserted rather than
    // hoped for: count the tile, follow it, count the rows. A second predicate
    // anywhere in this page is what makes these two numbers drift.
    const user = userEvent.setup();
    renderPage(() =>
      Promise.resolve({
        items: [
          device(),
          revoked(),
          device({ id: "3", hostname: "c", cert_state: "expiring" }),
          device({ id: "6", hostname: "d", cert_state: "expired" }),
          device({ id: "7", hostname: "e", cert_state: "renewing" }),
          device({ id: "8", hostname: "f", agent_status: null, cert_state: null }),
        ],
        total: 6,
      }),
    );
    await screen.findByText("ws-alpha");
    const counted = Number(certTile().querySelector(".stat-value")?.textContent);
    expect(counted).toBe(3);
    await user.click(certTile());
    await screen.findByText("ws-retired");
    const rows = document.querySelectorAll("tbody tr");
    expect(rows.length).toBe(counted);
    // ...and the tab strip's own count is the third reading of it.
    expect(screen.getByRole("tab", { name: /Certificate attention/ })).toHaveTextContent(
      String(counted),
    );
  });
});

/**
 * WHO STOOD BEHIND EACH ROW (asset-inventory spec §3, amended 2026-08-26).
 *
 * A sweep now records every host that answers it, so this table mixes rows a
 * person entered with rows the platform wrote from an echo reply. These tests
 * exist because those two are worth different amounts and used to render
 * identically.
 */
describe("which rows a person vouched for", () => {
  const mixed = () =>
    Promise.resolve({
      items: [
        device({ id: "a", hostname: "ws-alpha", fqdn: "a.corp" }),
        device({
          id: "b",
          hostname: null,
          fqdn: null,
          primary_ip: "10.70.0.11",
          agent_status: null,
          source: "discovery",
          provenance: "sweep",
        }),
        device({
          id: "c",
          hostname: "core-sw",
          fqdn: null,
          agent_status: null,
          source: "manual",
          provenance: "operator",
        }),
      ],
      total: 3,
    });

  it("says how each row came to exist", async () => {
    renderPage(mixed);
    await screen.findByText("ws-alpha");
    // SCOPED TO THE TABLE. The filter's `<option>` elements carry the same
    // words, and an unscoped query that matched either would pass with the
    // column deleted.
    const table = within(screen.getByRole("table"));
    expect(table.getByText("Found by a sweep")).toBeInTheDocument();
    expect(table.getByText("Entered by hand")).toBeInTheDocument();
    expect(table.getByText("Agent enrolment")).toBeInTheDocument();
    expect(table.getByRole("columnheader", { name: "Recorded by" })).toBeInTheDocument();
  });

  it("names a swept row by its address rather than inventing a hostname", async () => {
    // The backend leaves `hostname` null when the sweep had no reverse DNS.
    // A fabricated name would be indistinguishable from a real one here.
    renderPage(mixed);
    expect(await screen.findByText("10.70.0.11")).toBeInTheDocument();
  });

  it("filters the fleet down to what nobody vouched for", async () => {
    renderPage(mixed, {}, "/devices?provenance=sweep");
    expect(await screen.findByText("10.70.0.11")).toBeInTheDocument();
    expect(screen.queryByText("ws-alpha")).not.toBeInTheDocument();
    expect(screen.queryByText("core-sw")).not.toBeInTheDocument();
  });

  it("keeps the tab counts honest while that filter is on", async () => {
    // A strip reading "All devices 3" above one row is the defect this page
    // has already had once, with the search box.
    renderPage(mixed, {}, "/devices?provenance=sweep");
    await screen.findByText("10.70.0.11");
    expect(
      screen.getByRole("tab", { name: /All devices/ }).textContent,
    ).toContain("1");
  });

  it("puts the filter in the URL so it can be sent to somebody", async () => {
    // Same reasoning as the tab strip: "the eleven machines nobody vouched
    // for" is a view one technician sends another, and a filter held in
    // component state cannot be sent, bookmarked or restored by Back.
    function Where() {
      const location = useLocation();
      return <output data-testid="url">{`${location.pathname}${location.search}`}</output>;
    }
    mockUseApi.mockReturnValue({
      listDevices: fakeServer(mixed),
      me: vi.fn().mockResolvedValue({ permissions: [] } as unknown as Me),
    } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/devices"]}>
          <Routes>
            <Route
              path="/devices"
              element={
                <>
                  <DevicesPage />
                  <Where />
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText("ws-alpha");
    await userEvent.selectOptions(
      screen.getByLabelText("Filter by how the record came to exist"),
      "sweep",
    );
    expect(screen.getByTestId("url")).toHaveTextContent("/devices?provenance=sweep");
  });
});

/**
 * **THE FLEET LIST IS A PAGE OF A LARGER SET, AND SAYS SO.**
 *
 * This page fetched one capped page of `GET /devices` and did the search, the
 * provenance filter and every tab count in the browser over it. A /24 sweep
 * adds ~200 rows in one act; past the page limit the counts were wrong, the
 * search missed and the filter lied, and all three rendered as a
 * correct-looking empty answer.
 *
 * The estate in these fixtures is DELIBERATELY LARGER THAN ONE PAGE, which is
 * the only fixture shape that can tell the two implementations apart.
 */
describe("paging a fleet that does not fit on one page", () => {
  /** `DEVICES_PAGE_SIZE * 2 + 10` rows, each individually identifiable. */
  const bigEstate = () =>
    Promise.resolve({
      items: Array.from({ length: DEVICES_PAGE_SIZE * 2 + 10 }, (_, n) =>
        device({ id: `d${n}`, hostname: `host-${String(n).padStart(3, "0")}` }),
      ),
      total: DEVICES_PAGE_SIZE * 2 + 10,
    });

  it("asks the server for one page and says where that page sits", async () => {
    renderPage(bigEstate);
    await screen.findByText("host-000");
    expect(document.querySelectorAll("tbody tr")).toHaveLength(DEVICES_PAGE_SIZE);
    expect(
      screen.getByText(`Showing 1–${DEVICES_PAGE_SIZE} of ${DEVICES_PAGE_SIZE * 2 + 10} devices.`),
    ).toBeInTheDocument();
    // The row that is NOT on this page is genuinely absent, rather than
    // rendered and scrolled away.
    expect(screen.queryByText("host-100")).not.toBeInTheDocument();
  });

  it("walks forward and back through the set", async () => {
    const user = userEvent.setup();
    renderPage(bigEstate);
    await screen.findByText("host-000");
    expect(screen.getByRole("button", { name: "Previous devices" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Next devices" }));
    expect(await screen.findByText(`host-${String(DEVICES_PAGE_SIZE).padStart(3, "0")}`)).toBeInTheDocument();
    expect(screen.queryByText("host-000")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next devices" }));
    expect(await screen.findByText("host-100")).toBeInTheDocument();
    // The last page: 10 rows, and Next is spent.
    expect(document.querySelectorAll("tbody tr")).toHaveLength(10);
    expect(screen.getByRole("button", { name: "Next devices" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Previous devices" }));
    expect(
      await screen.findByText(`host-${String(DEVICES_PAGE_SIZE).padStart(3, "0")}`),
    ).toBeInTheDocument();
  });

  it("finds a row the first page does not contain", async () => {
    // **THE DEFECT, AS A TEST.** `host-109` is the LAST row of the third page.
    // *With the search in the browser over one page* this finds nothing and
    // the screen says "No devices match 'host-109'" -- indistinguishable from
    // a correct empty answer, which is why it was never reported as a bug.
    renderPage(bigEstate);
    await screen.findByText("host-000");
    await userEvent.type(screen.getByLabelText("Search devices"), "host-109");
    expect(await screen.findByText("host-109")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /All devices/ })).toHaveTextContent("1");
    // The tiles still describe the estate, not the search.
    const assets = [...document.querySelectorAll(".stat-card")].find(
      (card) => card.querySelector(".stat-label")?.textContent === "Assets",
    );
    expect(assets?.querySelector(".stat-value")?.textContent).toBe(
      String(DEVICES_PAGE_SIZE * 2 + 10),
    );
  });

  it("returns to the first page when a filter narrows the set", async () => {
    // Asking for rows 100-150 of a set that now has eleven returns an empty
    // table -- which is the one answer this page must never give by accident.
    const user = userEvent.setup();
    renderPage(bigEstate);
    await screen.findByText("host-000");
    await user.click(screen.getByRole("button", { name: "Next devices" }));
    await screen.findByText(`host-${String(DEVICES_PAGE_SIZE).padStart(3, "0")}`);

    await user.type(screen.getByLabelText("Search devices"), "host-10");
    // Ten matches -- host-100 through host-109 -- and all of them fit on page
    // one, which the old offset of 50 would have skipped clean past.
    expect(await screen.findByText("host-100")).toBeInTheDocument();
    expect(screen.getByText("Showing 1–10 of 10 devices.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous devices" })).toBeDisabled();
  });
});

/**
 * WHAT THE PAGE ACTUALLY ASKS FOR.
 *
 * The tests above prove the page renders whatever the (faithful) fake server
 * returns. This one proves the other half directly: that the filters reach the
 * request rather than being applied to the answer. A page that dropped `state`
 * from the query and filtered locally would pass every test above ONLY because
 * the fake server would then have handed it the whole estate — so this asserts
 * the parameters on the wire.
 */
describe("the filters travel to the server", () => {
  it("sends the search, the tab, the provenance filter and the page", async () => {
    const calls: DeviceListParams[] = [];
    const spy = async (params: DeviceListParams = {}) => {
      calls.push(params);
      return fakeServer(() => Promise.resolve({ items: [device()], total: 1 }))(params);
    };
    mockUseApi.mockReturnValue({
      listDevices: spy,
      me: vi.fn().mockResolvedValue({ permissions: [] } as unknown as Me),
    } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/devices?state=certs&q=beta&provenance=sweep&offset=50"]}>
          <Routes>
            <Route path="/devices" element={<DevicesPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // The fixture deliberately matches NOTHING (`ws-alpha` is not `beta`), so
    // the page renders its empty state -- what is under test is the request,
    // not the answer.
    await screen.findByText(/No devices match/);
    expect(calls[0]).toEqual({
      q: "beta",
      state: "certs",
      provenance: "sweep",
      limit: DEVICES_PAGE_SIZE,
      offset: 50,
    });
  });
});

/**
 * WHAT THE FLEET LIST SAYS IN WORDS, and both of these were review findings on
 * the migrated page rather than regressions: the column that printed the wire
 * enum, and four tiles that carried a figure and nothing else.
 */
describe("the estate in the operator's words", () => {
  it("writes the operating system as a person spells it, never as the enum", async () => {
    renderPage(() =>
      Promise.resolve({
        items: [
          device(),
          device({ id: "2", hostname: "mac-1", os_family: "macos", os_version: "15.6" }),
          device({
            id: "3",
            hostname: "srv-1",
            os_family: "linux",
            os_version: "Ubuntu 24.04",
          }),
        ],
        total: 3,
      }),
    );
    await screen.findByText("ws-alpha");
    // "windows 11 23H2", "macos 15.6" and "linux Ubuntu 24.04" is what this
    // column read on every row that had an OS at all.
    expect(screen.getByText("Windows 11 23H2")).toBeInTheDocument();
    expect(screen.getByText("macOS 15.6")).toBeInTheDocument();
    expect(screen.getByText("Linux Ubuntu 24.04")).toBeInTheDocument();
  });

  it("gives every tile the breakdown behind its figure", async () => {
    // v3 draws a second line on each of these ("272 of 286"). Only `zeroLabel`
    // was passed, so the breakdown appeared exactly when the figure happened
    // to be zero. Every foot below is two of the server's own fleet counts and
    // nothing derived from the rows on screen.
    renderPage(() =>
      Promise.resolve({
        items: [
          device(),
          device({ id: "2", hostname: "b", agent_status: "offline" }),
          device({ id: "3", hostname: "c", cert_state: "expiring" }),
          device({ id: "4", hostname: "switch-1", agent_status: null, cert_revoked: false }),
        ],
        total: 4,
      }),
    );
    await screen.findByText("ws-alpha");
    const foot = (label: string) =>
      [...document.querySelectorAll(".stat-card")]
        .find((card) => card.querySelector(".stat-label")?.textContent === label)
        ?.querySelector(".stat-foot")?.textContent;
    expect(foot("Managed endpoints")).toBe("of 4 assets");
    expect(foot("Assets")).toBe("1 with no agent");
    expect(foot("Online now")).toBe("of 3 with an agent");
    expect(foot("Certificate attention")).toBe("Revoked, expiring or expired");
  });

  it("says nothing beyond the zero label on a tile whose figure is zero", async () => {
    // Two sub-lines under "0" -- "Nothing revoked, expiring or expired" and a
    // denominator for a number that has none -- is noise where the tile has
    // one thing to say.
    renderPage(() => Promise.resolve({ items: [], total: 0 }));
    await screen.findByText(/No devices enrolled yet/);
    const cards = [...document.querySelectorAll(".stat-card")];
    expect(cards.some((card) => card.querySelector(".stat-foot"))).toBe(false);
    expect(cards.every((card) => card.querySelector(".stat-zero"))).toBe(true);
  });
});

/**
 * WHO HOLDS EACH DEVICE (operator decision 2026-09-23). People left the menu,
 * and a device's holder -- a person underneath, set by hand or taken from who
 * signs in -- is now a column of the fleet list. The server resolves it in the
 * page query and sends it only to a caller holding `people.read`; the column
 * follows the same code, because a column of blanks for someone who may not
 * see names would read as "nobody holds anything".
 */
describe("who each device is assigned to", () => {
  const ANA = { id: "p7", display_name: "Ana Ruiz" };
  const estate = () =>
    Promise.resolve({
      items: [
        device({ assigned_to: ANA }),
        device({ id: "2", hostname: "srv-beta", fqdn: "srv-beta.dc.local", assigned_to: null }),
      ],
      total: 2,
    });

  it("names the holder and links to their page", async () => {
    renderPage(estate, { permissions: ["devices.read", "people.read"] });
    await screen.findByText("ws-alpha");
    expect(await screen.findByRole("columnheader", { name: "Assigned to" })).toBeInTheDocument();
    const alpha = screen.getByText("ws-alpha").closest("tr") as HTMLElement;
    expect(within(alpha).getByRole("link", { name: "Ana Ruiz" })).toHaveAttribute(
      "href",
      "/people/p7",
    );
    // Nobody is said, not left blank.
    const beta = screen.getByText("srv-beta").closest("tr") as HTMLElement;
    expect(within(beta).getByText("Not assigned to anyone")).toBeInTheDocument();
  });

  it("finds a device by who it is assigned to", async () => {
    renderPage(estate, { permissions: ["devices.read", "people.read"] });
    await screen.findByText("ws-alpha");
    await vi.waitFor(() =>
      expect(screen.getByLabelText("Search devices")).toHaveAttribute(
        "placeholder",
        expect.stringContaining("who it's assigned to"),
      ),
    );
    await userEvent.type(screen.getByLabelText("Search devices"), "Ruiz");
    await vi.waitFor(() => expect(screen.queryByText("srv-beta")).not.toBeInTheDocument());
    expect(screen.getByText("ws-alpha")).toBeInTheDocument();
  });

  it("has no column, and promises no such search, without people.read", async () => {
    const view = renderPage(estate, { permissions: ["devices.read"] });
    await screen.findByText("ws-alpha");
    // `/me` has to have answered before an absent column means anything.
    const me = (mockUseApi.mock.results[0]?.value as ApiClient).me as ReturnType<typeof vi.fn>;
    await vi.waitFor(() => expect(me).toHaveBeenCalled());
    await vi.waitFor(() => expect(view.container.querySelectorAll("thead th")).toHaveLength(8));
    expect(screen.queryByRole("columnheader", { name: "Assigned to" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Ana Ruiz" })).toBeNull();
    expect(screen.getByLabelText("Search devices")).toHaveAttribute(
      "placeholder",
      expect.not.stringContaining("assigned"),
    );
  });
});
