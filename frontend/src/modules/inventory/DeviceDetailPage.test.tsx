/**
 * Device detail, migrated onto the shell components -- the design's closest
 * correlate is `deviceDrawer` in docs/design/itms-prototype.html. Mocks the
 * auth-backed API client, mirroring modules/devices/DevicesPage.test.tsx.
 *
 * The permission-gated tabs (patch/deploy/backup/provisioning) are left
 * ungated in these fixtures ONLY to prove they render when granted; most
 * tests use a `me` with no extra permissions so those foreign-module tabs
 * (owned elsewhere) never mount and never need their own API mocked here.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, DeviceInventory, Me } from "../../lib/api";
import { NO_DATE } from "../../lib/datetime";
import { DeviceDetailPage } from "./DeviceDetailPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

const ME: Me = { permissions: [] } as unknown as Me;

const INVENTORY: DeviceInventory = {
  summary: { hostname: "ws-alpha", os_family: "windows", os_version: "11 23H2", cpu: "Intel i7", ram_bytes: 17179869184, serial: "SN-001" },
  hardware: [{ type: "disk", identifier: "C:", attributes: { size_gb: 512 } }],
  software: [{ name: "Google Chrome", version: "140.0", publisher: "Google LLC", install_date: "2026-08-01" }],
  facts: {},
  snapshot_taken_at: "2026-08-16T09:00:00Z",
};

function renderPage(
  overrides: Partial<ApiClient> = {},
  me: Me = ME,
  /** The URL to land on. `?tab=` selects a tab, which is what makes this
   *  page's stat tiles links to somewhere rather than links to here. */
  entry = "/devices/d1?tab=hardware",
) {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue(me),
    // A MANAGED device by default: `agent_status` non-null is what the page
    // reads as "has an agent" (asset-inventory spec §2), and every test in
    // this file predates assets and describes an enrolled machine. The
    // agentless cases live in `DeviceDetailPage.assets.test.tsx`.
    getDevice: vi.fn().mockResolvedValue({
      id: "d1",
      hostname: "ws-alpha",
      agent_status: "online",
      lifecycle: "in_service",
      source: "agent",
    }),
    getDeviceInventory: vi.fn().mockResolvedValue(INVENTORY),
    getDeviceChanges: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listJobs: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/devices/:deviceId" element={<DeviceDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return { ...render(ui), api };
}

beforeEach(() => {
  mockUseApi.mockReset();
});

/**
 * The interface/identifier column of the hardware table, top to bottom.
 *
 * A name appears TWICE on a NIC row -- once as the row's identity and once
 * inside its attributes, because `attributes.name` is what the agent sends and
 * the attributes cell shows what the agent sent. `getByText` therefore cannot
 * distinguish "this row exists" from "this string is in the row's attribute
 * blob", which is exactly the distinction these tests are about.
 */
function rowNames(): (string | null)[] {
  return [...document.querySelectorAll("#hardware-table tbody tr")].map(
    (tr) => tr.querySelector("td:nth-child(2) div")?.textContent ?? null,
  );
}

describe("DeviceDetailPage", () => {
  it("says it is loading rather than showing an empty hardware tab", async () => {
    renderPage({ getDeviceInventory: () => new Promise<never>(() => {}) });
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty hardware tab", async () => {
    renderPage({ getDeviceInventory: () => Promise.reject(new Error("boom")) });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("says hardware is empty when it is genuinely empty", async () => {
    renderPage({
      getDeviceInventory: () => Promise.resolve({ ...INVENTORY, hardware: [] }),
    });
    expect(await screen.findByText("No hardware inventory collected yet.")).toBeInTheDocument();
  });

  it("renders the hostname as the title and real summary facts in the facts strip, not constants", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-08-16T12:00:00Z"));
    try {
      renderPage();
      expect(await screen.findByRole("heading", { name: "ws-alpha" })).toBeInTheDocument();
      // The four inventory facts, in v3's `facts` strip (the stat tiles the
      // previous design drew here); `.stat-value` is the hook each value keeps.
      const values = [...document.querySelectorAll(".facts .stat-value")].map(
        (el) => el.textContent,
      );
      // The last fact carries the AGE (cycle 12), because "[LOW] 'Last sweep'
      // puts a full timestamp into a numeric stat-card slot, where it wraps to
      // two lines … it fails to convey the staleness that actually matters".
      expect(values).toEqual(["Intel i7", "16.0 GiB", "SN-001", "3h ago"]);
      // The instant is not lost: it moves to the fact's secondary line, in the
      // SHARED format (lib/datetime), never `toLocaleString()`.
      const feet = [...document.querySelectorAll(".facts .stat-foot")].map(
        (el) => el.textContent,
      );
      expect(feet).toContain("16 Aug 2026, 09:00");
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the strip to the six cells its row draws, with the record's origin on the chip line", async () => {
    // A seventh cell opened a second row that held one value and a band of
    // bordered white under the page head: `.facts` is
    // `repeat(auto-fit, minmax(170px, 1fr))`, six tracks at the desktop
    // width, so the cell count has to divide the row. Provenance is the one
    // that was never a measurement, and it moves to the chips beside the
    // agent state rather than being dropped.
    renderPage({
      getDevice: vi.fn().mockResolvedValue({
        id: "d1",
        hostname: "ws-alpha",
        agent_status: "online",
        lifecycle: "in_service",
        source: "agent",
        provenance: "sweep",
      }),
    });
    await screen.findByRole("heading", { name: "ws-alpha" });
    const cells = [...document.querySelectorAll(".facts > div")];
    expect(cells.map((cell) => cell.querySelector(".k")?.textContent)).toEqual([
      "Agent",
      "Certificate",
      "CPU",
      "Memory",
      "Serial",
      "Last sweep",
    ]);
    // Moved, not lost -- same words as the fleet list's own provenance column.
    expect(await screen.findByText("Found by a sweep")).toBeInTheDocument();
  });

  it("says the serial is not reported instead of a bare em dash in stat type", async () => {
    // "[MEDIUM] 'Serial' stat card shows a bare em-dash given equal visual
    // weight to real data … Nothing explains why the serial is missing."
    renderPage({
      getDeviceInventory: vi
        .fn()
        .mockResolvedValue({ ...INVENTORY, summary: { ...INVENTORY.summary, serial: null } }),
    });
    await screen.findByRole("heading", { name: "ws-alpha" });
    const card = (await screen.findByText("Serial")).closest("div") as HTMLElement;
    expect(within(card).getByText("Not reported")).toBeInTheDocument();
    expect(within(card).getByText(/did not report a serial number/)).toBeInTheDocument();
    expect(card.querySelector(".stat-value")?.textContent).not.toBe("—");
  });

  it("switches to the software tab and filters client-side", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "ws-alpha" });
    await user.click(screen.getByRole("tab", { name: "Software" }));
    expect(await screen.findByText("Google Chrome")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Filter software"), "zzz-no-match");
    expect(await screen.findByText("No software matches that filter.")).toBeInTheDocument();
  });

  it("fetches jobs only once the Jobs tab is selected, and shows only this device's jobs", async () => {
    const user = userEvent.setup();
    const listJobs = vi.fn().mockResolvedValue({
      items: [
        { id: "j1", type: "inventory.collect", status: "completed", issued_at: "2026-08-16T09:00:00Z", device_id: "d1" },
        { id: "j2", type: "patch.scan", status: "completed", issued_at: "2026-08-16T09:00:00Z", device_id: "other-device" },
      ],
      total: 2,
    });
    renderPage({ listJobs });
    await screen.findByRole("heading", { name: "ws-alpha" });
    expect(listJobs).not.toHaveBeenCalled();

    await user.click(screen.getByRole("tab", { name: "Jobs" }));
    expect(await screen.findByText("inventory.collect")).toBeInTheDocument();
    expect(screen.queryByText("patch.scan")).not.toBeInTheDocument();
  });

  it("does not offer a Patch tab without patch.read", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "ws-alpha" });
    expect(screen.queryByRole("tab", { name: "Updates" })).not.toBeInTheDocument();
  });

  it("offers a Patch tab with patch.read, and Health on any managed device", async () => {
    // "Unconditionally" was true until agentless assets existed: Health now
    // needs an agent as well as `devices.read`, because there is nothing to
    // report health FOR without one. The permission half is unchanged --
    // health for a device is gated `devices.read` server-side, not
    // `health.read` (health spec §10).
    renderPage({}, { permissions: ["patch.read"] } as unknown as Me);
    await screen.findByRole("heading", { name: "ws-alpha" });
    expect(screen.getByRole("tab", { name: "Updates" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Health" })).toBeInTheDocument();
  });

  it("shows no fabricated Disk or Patch stat card the API cannot fill", async () => {
    // DeviceInventory has no disk-usage or patch-compliance field -- see the
    // comment atop DeviceDetailPage.tsx. Omitted rather than faked, mirroring
    // DevicesPage.test.tsx's "no fabricated column" test.
    renderPage();
    await screen.findByRole("heading", { name: "ws-alpha" });
    expect(screen.queryByText("Disk")).not.toBeInTheDocument();
    expect(screen.queryByText("Patch")).not.toBeInTheDocument();
  });
});

/**
 * Times, in the zone the signed-in user works in (UX remediation cycle 5).
 *
 * THE MECHANISM, because it is the reason these assertions look fussy: the
 * audit's BLOCKER was "every datetime in the product is displayed 5 hours
 * earlier than it was entered", and five is the operator's offset. A page that
 * renders through the BROWSER's zone is correct on the developer's laptop and
 * wrong on every machine the technician actually sits at -- so the test fixes
 * a profile zone that is not the runner's and asserts the wall clock it
 * implies.
 */
describe("times are read in the profile's zone", () => {
  /** `/me` with a zone that is west of UTC, and not the CI runner's. */
  const NY: Me = {
    permissions: [],
    timezone: "America/New_York",
  } as unknown as Me;

  it("renders the last sweep in the profile zone, not the browser's", async () => {
    // 09:00Z in August is 05:00 in New York (EDT, UTC-4). A page reading the
    // browser zone renders 09:00 under the runner's default TZ=UTC. The
    // instant lives on the card's secondary line since cycle 12; the zone
    // requirement is unchanged.
    renderPage({}, NY);
    await screen.findByRole("heading", { name: "ws-alpha" });
    const card = (await screen.findByText("Last sweep")).closest("div") as HTMLElement;
    expect(card.querySelector(".stat-foot")?.textContent).toBe("16 Aug 2026, 05:00");
    expect(card.textContent).not.toContain("09:00");
  });

  it("renders changes and jobs in that same zone", async () => {
    // ONE zone across the tabs. The audit's other finding was two different
    // renderings of one device's timestamps sitting a tab apart.
    const user = userEvent.setup();
    renderPage(
      {
        getDeviceChanges: vi.fn().mockResolvedValue({
          items: [
            {
              id: "c1",
              // Distinct from the snapshot above, and late enough in the UTC
              // day that New York is still on the PREVIOUS date -- the
              // off-by-one the zone-aware formatter has to get right.
              ts: "2026-08-16T02:30:00Z",
              change_type: "software_added",
              subject: "Google Chrome",
              from_value: null,
              to_value: "140.0",
            },
          ],
          total: 1,
        }),
        listJobs: vi.fn().mockResolvedValue({
          items: [
            {
              id: "j1",
              type: "inventory.collect",
              status: "completed",
              issued_at: "2026-08-16T13:45:00Z",
              device_id: "d1",
            },
          ],
          total: 1,
        }),
      },
      NY,
    );
    await screen.findByRole("heading", { name: "ws-alpha" });

    await user.click(screen.getByRole("tab", { name: "Changes" }));
    // 02:30Z on the 16th is 22:30 on the 15th in New York.
    expect(await screen.findByTitle("15 Aug 2026, 22:30")).toBeInTheDocument();
    expect(screen.queryByTitle("16 Aug 2026, 02:30")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Jobs" }));
    expect(await screen.findByTitle("16 Aug 2026, 09:45")).toBeInTheDocument();
  });

  it("does not move an install DATE into the previous day", async () => {
    // `install_date` is a SQL `date` -- a calendar day with no time and no
    // zone. `new Date("2026-08-01")` is midnight UTC, so rendering it in a
    // zone west of UTC reports 31 July: an off-by-one-day that reads as real
    // data rather than as a bug. This is the case a blanket "put every date
    // through the user's zone" sweep gets wrong.
    const user = userEvent.setup();
    renderPage({}, NY);
    await screen.findByRole("heading", { name: "ws-alpha" });
    await user.click(screen.getByRole("tab", { name: "Software" }));
    expect(await screen.findByText("01 Aug 2026")).toBeInTheDocument();
    expect(screen.queryByText("31 Jul 2026")).not.toBeInTheDocument();
  });

  it("says a missing date is missing rather than showing Invalid Date", async () => {
    renderPage(
      {
        getDeviceInventory: vi
          .fn()
          .mockResolvedValue({ ...INVENTORY, snapshot_taken_at: null }),
      },
      NY,
    );
    await screen.findByRole("heading", { name: "ws-alpha" });
    const values = [...document.querySelectorAll(".facts .stat-value")].map(
      (el) => el.textContent,
    );
    expect(values[3]).toBe(NO_DATE);
    expect(document.body.textContent).not.toMatch(/Invalid Date/);
  });

  it("still renders a software row with no install date at all", async () => {
    // NON-VACUITY for the date-only case above: a component that rendered
    // nothing in that column would pass the "not 31 Jul" assertion.
    const user = userEvent.setup();
    renderPage(
      {
        getDeviceInventory: vi.fn().mockResolvedValue({
          ...INVENTORY,
          software: [
            { name: "Google Chrome", version: "140.0", publisher: "Google LLC", install_date: null },
          ],
        }),
      },
      NY,
    );
    await screen.findByRole("heading", { name: "ws-alpha" });
    await user.click(screen.getByRole("tab", { name: "Software" }));
    const row = (await screen.findByText("Google Chrome")).closest("tr");
    expect(within(row as HTMLElement).getByText(NO_DATE)).toBeInTheDocument();
  });
});

/**
 * Cycle 9: the four things this page did not say.
 *
 *   [HIGH] "Device detail page never shows whether the agent is online or
 *          offline … Agent connectivity is the single most decision-relevant
 *          fact about an endpoint, and it is present on the list but lost on
 *          the detail page."
 *   [HIGH] "Queued jobs cannot be cancelled, inspected, or retried — the Jobs
 *          tab is read-only text … Every row contains zero buttons and zero
 *          links."
 *   [HIGH] "Raw JSON blobs are rendered directly into the UI as table content."
 *   [HIGH] "Jobs can be dispatched to an offline agent with no warning" — which
 *          this page made structurally impossible to fix by handing the
 *          dispatch dialog a two-field `{ id, hostname } as Device`.
 */
describe("what the detail page owes the operator", () => {
  const NY: Me = { permissions: [], timezone: "America/New_York" } as unknown as Me;
  // The two actions below are offered only to an operator who may use them.
  const NY_ACTING: Me = {
    permissions: ["jobs.dispatch", "remote.control"],
    timezone: "America/New_York",
  } as unknown as Me;

  const OFFLINE_DEVICE = {
    id: "d1",
    hostname: "ws-alpha",
    agent_status: "offline",
    last_seen_at: "2026-08-16T09:00:00Z",
    lifecycle: "in_service",
    source: "agent",
  };

  it("shows whether the agent is answering, and when it last did", async () => {
    renderPage({ getDevice: vi.fn().mockResolvedValue(OFFLINE_DEVICE) }, NY);
    await screen.findByRole("heading", { name: "ws-alpha" });

    // Timeout raised over the 1s default: this assertion waits on a SECOND
    // query (`getDevice`) after the heading's, and under a full parallel suite
    // run the default was tight enough to flake.
    // Case-insensitive: the shared StatusPill humanises its value ("offline"
    // → "Offline"), and this assertion is about the FACT being on the page,
    // not about which layer capitalises it.
    expect(await screen.findByText(/^offline$/i)).toBeInTheDocument();
    // "Agent last seen", not a bare "Last contact": the same audit found this
    // page reporting two different last-contact times in two formats, so the
    // subsystem each belongs to has to be named. Relative in the text, the
    // instant on hover: 09:00Z is 05:00 in New York.
    const fact = screen.getByText(/^Agent last seen/);
    expect(fact.querySelector("time")).toHaveAttribute("title", "16 Aug 2026, 05:00");
  });

  it("hands the dispatch dialog the real device, so it can warn about the agent", async () => {
    // THE `as Device` CAST WAS THE DEFECT. `{ id, hostname } as Device`
    // compiled fine and carried no `agent_status`, so the dialog could not
    // know it was aiming at a machine that had been silent for days.
    const user = userEvent.setup();
    renderPage({ getDevice: vi.fn().mockResolvedValue(OFFLINE_DEVICE) }, NY_ACTING);
    await screen.findByRole("heading", { name: "ws-alpha" });

    await user.click(await screen.findByRole("button", { name: "Run job" }));

    const dialog = await screen.findByRole("dialog", { name: /Run job on ws-alpha/ });
    expect(within(dialog).getByRole("status")).toHaveTextContent(/agent is offline/i);
  });

  it("offers Remote in on a device with an agent, aimed at this device", async () => {
    const user = userEvent.setup();
    renderPage({ getDevice: vi.fn().mockResolvedValue(OFFLINE_DEVICE) }, NY_ACTING);
    await screen.findByRole("heading", { name: "ws-alpha" });

    await user.click(await screen.findByRole("button", { name: "Remote in" }));

    const dialog = await screen.findByRole("dialog", { name: "Start remote session" });
    expect(within(dialog).getByLabelText("Reason")).toHaveValue("");
  });

  it("flags a job on this device that has been waiting far too long, and links to it", async () => {
    const user = userEvent.setup();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-08-20T10:00:00Z"));
    try {
      renderPage(
        {
          listJobs: vi.fn().mockResolvedValue({
            items: [
              {
                id: "j1",
                type: "patch.scan",
                status: "queued",
                attempts: 0,
                issued_at: "2026-08-19T09:00:00Z",
                device_id: "d1",
              },
            ],
            total: 1,
          }),
        },
        NY,
      );
      await screen.findByRole("heading", { name: "ws-alpha" });
      await user.click(screen.getByRole("tab", { name: "Jobs" }));

      const row = (await screen.findByText("Scan for patches")).closest("tr") as HTMLElement;
      // The age, so "is this stuck?" needs no date arithmetic.
      expect(within(row).getByText("25 hours ago")).toBeInTheDocument();
      expect(within(row).getByText("Still waiting")).toBeInTheDocument();
      // And a way off a read-only row into the full record.
      // The link NAMES ITS ROW. "[LOW] 'ACTIONS' column header is announced
      // but the actions have no per-row context" — a column of identical
      // "Inspect" links tells a screen-reader user nothing about which job.
      expect(
        within(row).getByRole("link", { name: /Inspect Scan for patches job issued/ }),
      ).toHaveAttribute("href", "/jobs?job=j1");
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders hardware attributes as values, not as a JSON literal", async () => {
    renderPage(
      {
        getDeviceInventory: vi.fn().mockResolvedValue({
          ...INVENTORY,
          hardware: [
            {
              type: "nic",
              identifier: "eth0",
              attributes: { mac: "02:42:ac:11:00:02", mtu: 1500 },
            },
          ],
        }),
      },
      NY,
    );
    const row = (await screen.findByText("eth0")).closest("tr") as HTMLElement;

    // Labelled values, not a JSON literal — and `<Attributes>` humanises the
    // wire keys, so `mac` reads as the initialism it is.
    expect(within(row).getByText("02:42:ac:11:00:02")).toBeInTheDocument();
    expect(within(row).getByText(/MAC/)).toBeInTheDocument();
    expect(within(row).getByText("1500")).toBeInTheDocument();
    // The raw object is still one disclosure away — the finding asks for it —
    // but it is not what the cell reads as.
    expect(within(row).getByText("View raw")).toBeInTheDocument();
    expect(row.querySelector(".attributes")?.textContent).not.toContain('{"mac"');
  });

  it("formats a byte count in the attributes column with the Memory card's formatter", async () => {
    // "[HIGH] … Byte counts are unformatted (`256022937600` instead of
    // '238 GiB' — note the Memory stat card *does* format correctly to
    // '15.0 GiB', proving the formatter exists and simply is not applied
    // here)." Both of the audit's own numbers, on one row.
    renderPage({
      getDeviceInventory: vi.fn().mockResolvedValue({
        ...INVENTORY,
        hardware: [
          {
            type: "disk",
            identifier: "sda",
            attributes: { model: "LOGICAL VOLUME", size_bytes: 256022937600 },
          },
        ],
      }),
    });
    const row = (await screen.findByText("sda")).closest("tr") as HTMLElement;
    expect(within(row).getByText("238 GiB")).toBeInTheDocument();
    // The `.attributes` line is what the cell READS as; the byte count is
    // still exact inside the "View raw" disclosure, which is the point of it.
    expect(row.querySelector(".attributes")?.textContent).not.toContain("256022937600");
    // The type column says what the thing IS, not what the table stores it as.
    expect(within(row).getByText("Storage")).toBeInTheDocument();
  });

  it("flattens a NIC address block instead of printing 225 characters of JSON", async () => {
    // The finding's longest cell, verbatim from the audit.
    renderPage({
      getDeviceInventory: vi.fn().mockResolvedValue({
        ...INVENTORY,
        hardware: [
          {
            type: "nic",
            identifier: "3c:a8:2a:15:6b:11",
            attributes: {
              mac: "3c:a8:2a:15:6b:11",
              name: "eno2",
              addresses: [
                { family: "ipv4", origin: "static", address: "172.22.104.28/29" },
                {
                  family: "ipv6",
                  origin: "link_local",
                  address: "fe80::3ea8:2aff:fe15:6b11%eno2/64",
                },
              ],
            },
          },
        ],
      }),
    });
    // The interface NAME leads the row; the MAC the agent keys it by is the
    // secondary line, not the only thing on screen.
    await screen.findAllByText("eno2");
    const row = document.querySelector("#hardware-table tbody tr") as HTMLElement;
    expect(rowNames()).toEqual(["eno2"]);
    expect(row.querySelector("td:nth-child(2) .cell-sub")?.textContent).toBe(
      "3c:a8:2a:15:6b:11",
    );
    const attributes = row.querySelector(".attributes") as HTMLElement;
    expect(attributes.textContent).toContain("172.22.104.28/29");
    expect(attributes.textContent).not.toContain('{"family"');
    expect(attributes.textContent).not.toContain("origin");
  });

  it("hides container plumbing behind a toggle that says how much it hides", async () => {
    // "[MEDIUM] Hardware inventory is 91% network interfaces, 20 of which are
    // Docker veth pairs … The signal an operator needs is diluted 1:8 by
    // container plumbing."
    const user = userEvent.setup();
    renderPage({
      getDeviceInventory: vi.fn().mockResolvedValue({
        ...INVENTORY,
        hardware: [
          {
            type: "nic",
            identifier: "aa:bb:cc:dd:ee:01",
            attributes: {
              name: "eno1",
              addresses: [{ family: "ipv4", origin: "static", address: "172.22.104.2/29" }],
            },
          },
          {
            type: "nic",
            identifier: "aa:bb:cc:dd:ee:02",
            attributes: {
              name: "veth7670993",
              addresses: [{ family: "ipv6", origin: "link_local", address: "fe80::1" }],
            },
          },
          { type: "nic", identifier: "aa:bb:cc:dd:ee:03", attributes: { name: "docker0" } },
        ],
      }),
    });

    await screen.findAllByText("eno1");
    expect(rowNames()).toEqual(["eno1"]);

    const toggle = screen.getByRole("button", { name: "Show virtual interfaces (2)" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);

    // HIDDEN, NEVER DROPPED.
    expect(rowNames()).toEqual(["eno1", "veth7670993", "docker0"]);
    expect(
      screen.getByRole("button", { name: "Hide virtual interfaces (2)" }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("puts the real interface above the virtual ones when both are shown", async () => {
    const user = userEvent.setup();
    renderPage({
      getDeviceInventory: vi.fn().mockResolvedValue({
        ...INVENTORY,
        hardware: [
          { type: "nic", identifier: "aa:00:01", attributes: { name: "veth7670993" } },
          {
            type: "nic",
            identifier: "aa:00:02",
            attributes: {
              name: "eno1",
              addresses: [{ family: "ipv4", origin: "static", address: "172.22.104.2/29" }],
            },
          },
        ],
      }),
    });
    await screen.findAllByText("eno1");
    await user.click(screen.getByRole("button", { name: "Show virtual interfaces (1)" }));
    // The virtual row was BELOW the real one in the fixture's reverse order.
    expect(rowNames()).toEqual(["eno1", "veth7670993"]);
  });

  it("wires the tab strip to a real tabpanel", async () => {
    // "[MEDIUM] Tabs are missing `aria-controls`/`tabpanel` wiring … no
    // element on the page has `role='tabpanel'` (count = 0)."
    renderPage();
    await screen.findByRole("heading", { name: "ws-alpha" });
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("id", "device-tabpanel");
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab).toHaveAttribute("aria-controls", "device-tabpanel");
    }
  });

  it("offers a way back to the device list", async () => {
    // "[MEDIUM] Breadcrumb 'ITMS / Fleet' is not navigable — there is no link
    // back to the device list … a classic navigation dead end."
    renderPage();
    await screen.findByRole("heading", { name: "ws-alpha" });
    expect(screen.getByRole("link", { name: "All devices" })).toHaveAttribute(
      "href",
      "/devices",
    );
  });

  it("marks every column header as a column header", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "ws-alpha" });
    const headers = [...document.querySelectorAll("#hardware-table th")];
    expect(headers.length).toBeGreaterThan(0);
    for (const header of headers) expect(header).toHaveAttribute("scope", "col");
  });

  it("says a component with no attributes has none, rather than printing {}", async () => {
    renderPage(
      {
        getDeviceInventory: vi.fn().mockResolvedValue({
          ...INVENTORY,
          hardware: [{ type: "cpu", identifier: "cpu0", attributes: {} }],
        }),
      },
      NY,
    );
    const row = (await screen.findByText("cpu0")).closest("tr") as HTMLElement;
    expect(within(row).getByText(NO_DATE)).toBeInTheDocument();
    expect(row.textContent).not.toContain("{}");
  });
});

/**
 * NO DEAD TILES, and on this page that required moving the tab into the URL.
 *
 * CPU, memory and serial are facts whose detail lives on the Hardware tab of
 * this very page, and while the tab was `useState` there was no address for
 * that -- a tile could only have linked to the URL the reader was already on,
 * which is the dead tile the operator asked us to stop shipping. So the tab is
 * `?tab=`, and these tests hold both halves of that: the hrefs, and the fact
 * that the hrefs actually select a tab.
 */
describe("the inventory facts are links, and the tab is addressable", () => {
  it("points each fact at the tab holding its detail", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "ws-alpha" });
    // Each inventory fact in the strip is an anchor: the fact's caption, the
    // element that carries the href, and where it goes.
    const tiles = [...document.querySelectorAll(".facts .stat-value")].map((value) => [
      value.closest("div")?.querySelector(".k")?.textContent,
      value.tagName,
      value.getAttribute("href"),
    ]);
    expect(tiles).toEqual([
      ["CPU", "A", "/devices/d1?tab=hardware"],
      ["Memory", "A", "/devices/d1?tab=hardware"],
      ["Serial", "A", "/devices/d1?tab=hardware"],
      // The sweep's detail is the job that ran it, not the hardware it found:
      // "why is this snapshot two days old" is a question about a job.
      ["Last sweep", "A", "/devices/d1?tab=jobs"],
    ]);
  });

  it("opens the tab named in the URL, which is what stops those hrefs being no-ops", async () => {
    const listJobs = vi.fn().mockResolvedValue({ items: [], total: 0 });
    renderPage({ listJobs }, ME, "/devices/d1?tab=jobs");
    await screen.findByRole("heading", { name: "ws-alpha" });
    expect(screen.getByRole("tab", { name: "Jobs" })).toHaveAttribute("aria-selected", "true");
    // The jobs query is `enabled: tab === "jobs"`, so the REQUEST is the
    // evidence -- an aria attribute alone could be true of a tab strip whose
    // panel never switched.
    await waitFor(() => expect(listJobs).toHaveBeenCalled());
  });

  it("falls back to the Overview when the URL names a tab this account cannot see", async () => {
    // `?tab=patch` without `patch.read`: an empty panel under a strip with
    // nothing selected reads as a page that failed to load, not as a tab that
    // was never offered.
    renderPage({}, ME, "/devices/d1?tab=patch");
    await screen.findByRole("heading", { name: "ws-alpha" });
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
});

/**
 * The Docs tab: ONE TAB, TWO LISTS (`spec-attachments.md` §11.5).
 *
 * "What is written about this machine" is one question. A second tab for
 * documents would split the answer across two clicks, and the reader who
 * checked only the first would conclude nothing is written.
 *
 * This tab had NO test at all before this block, which is how it kept a link
 * that went to `/docs` and not to the article it named.
 */
describe("the Docs tab", () => {
  const ARTICLE = {
    id: "a1",
    title: "Windows workstation build standard",
    category: "Procedure",
    archived: false,
    updated_at: "2026-08-01T00:00:00.000Z",
    link_count: 1,
  };
  const DOCUMENT = {
    id: "doc1",
    title: "Acme support contract",
    description: "",
    category: "Contracts",
    archived: false,
    current_version: 2,
    updated_at: "2026-08-20T00:00:00.000Z",
    link_count: 1,
  };

  it("shows both lists, each deep-linked to the thing it names", async () => {
    renderPage(
      {
        listArticles: vi.fn().mockResolvedValue({ items: [ARTICLE], total: 1 }),
        listDocuments: vi.fn().mockResolvedValue({ items: [DOCUMENT], total: 1 }),
      },
      ME,
      "/devices/d1?tab=docs",
    );
    // The HEADINGS render before either query answers, so waiting on one of
    // them proves nothing about the lists. Wait for the content.
    expect(await screen.findByText("Acme support contract")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Articles" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Documents" })).toBeInTheDocument();
    // Not "somewhere on /docs": the docs page carries its selection in the
    // query string precisely so these links can name one object.
    expect(screen.getByRole("link", { name: /Windows workstation build standard/ })).toHaveAttribute(
      "href",
      "/docs?article=a1",
    );
    expect(screen.getByRole("link", { name: /Acme support contract/ })).toHaveAttribute(
      "href",
      "/docs?document=doc1",
    );
  });

  it("queries both by TARGET, which is what the link table is indexed for", async () => {
    const listArticles = vi.fn().mockResolvedValue({ items: [], total: 0 });
    const listDocuments = vi.fn().mockResolvedValue({ items: [], total: 0 });
    renderPage({ listArticles, listDocuments }, ME, "/devices/d1?tab=docs");
    await screen.findByText("No documents linked to this device.");
    const filter = { target_type: "device", target_id: "d1" };
    expect(listArticles).toHaveBeenCalledWith(filter);
    expect(listDocuments).toHaveBeenCalledWith(filter);
  });

  it("keeps the two lists' states separate", async () => {
    // A failing documents request must not render as "no documents", and it
    // must not blank the articles list beside it either. Two queries, two
    // `QueryState`s, and this is what says the wiring is not shared.
    renderPage(
      {
        listArticles: vi.fn().mockResolvedValue({ items: [ARTICLE], total: 1 }),
        listDocuments: vi.fn().mockRejectedValue(new Error("boom")),
      },
      ME,
      "/devices/d1?tab=docs",
    );
    expect(await screen.findByText("Windows workstation build standard")).toBeInTheDocument();
    expect(screen.queryByText("No documents linked to this device.")).not.toBeInTheDocument();
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
  });

  it("says a document has no file yet rather than showing it as v0", async () => {
    renderPage(
      {
        listDocuments: vi
          .fn()
          .mockResolvedValue({ items: [{ ...DOCUMENT, current_version: 0 }], total: 1 }),
      },
      ME,
      "/devices/d1?tab=docs",
    );
    expect(await screen.findByText(/no file yet/)).toBeInTheDocument();
  });
});
