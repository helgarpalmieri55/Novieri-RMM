import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../lib/api";
import { PlatformStatusPage } from "./PlatformStatusPage";
import type { PlatformStatus } from "./types";

// Pure component test: the auth-backed API client is mocked, mirroring
// modules/remote/SessionsPage.test.tsx.
const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** The page links a failing probe to the error monitor (cycle 12), so it needs
 *  the router the shell gives it. */
function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  );
}

const STATUS: PlatformStatus = {
  overall: "degraded",
  probes: [
    { name: "database", status: "ok", duration_ms: 4, reason: null, detail: null },
    {
      name: "vault",
      status: "down",
      duration_ms: 812,
      reason: "sealed",
      detail: null,
    },
    // `ok` AND NOT FORWARDING. The status is deliberately unalarming -- an
    // operator who has not enabled Keycloak event forwarding has not broken
    // anything -- so the entire distinction lives in `detail`.
    {
      name: "keycloak_events",
      status: "ok",
      duration_ms: 1,
      reason: null,
      detail: { configured: false },
    },
  ],
  processes: [
    {
      process: "api",
      instances: 2,
      healthy_instances: 2,
      oldest_age_s: 6,
      status: "ok",
      version: "1.4.0",
    },
    {
      process: "gateway",
      instances: 3,
      healthy_instances: 1,
      oldest_age_s: 190,
      status: "degraded",
      version: "1.4.0",
    },
  ],
  modules: [
    { name: "devices", routes: 12, job_types: 4 },
    { name: "patch", routes: 9, job_types: 3 },
  ],
};

describe("PlatformStatusPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      getPlatformStatus: vi.fn().mockResolvedValue(STATUS),
      // The freshness line renders in the operator's profile zone (cycle 12).
      me: vi.fn().mockResolvedValue({
        user_id: "u1",
        org_id: "o1",
        email: "a@b.c",
        display_name: "Alex Morgan",
        permissions: ["platform.status.read"],
        timezone: "Europe/London",
      }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // The three states a mock never has (see DevicesPage.test.tsx) -- a status
  // page that cannot distinguish "still checking" and "the check itself
  // failed" from "everything is fine" is the specific failure this page's own
  // module docstring warns against.
  it("says it is loading rather than showing an empty status page", async () => {
    mockUseApi.mockReturnValue({
      getPlatformStatus: () => new Promise<never>(() => {}),
      me: () => new Promise<never>(() => {}),
    } as unknown as ApiClient);
    render(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <PlatformStatusPage />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(/registration, not health/i)).not.toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty status page", async () => {
    mockUseApi.mockReturnValue({
      getPlatformStatus: () => Promise.reject(new Error("boom")),
      me: () => Promise.reject(new Error("boom")),
    } as unknown as ApiClient);
    render(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <PlatformStatusPage />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/registration, not health/i)).not.toBeInTheDocument();
  });

  it("renders a down probe's reason code, never exception text", async () => {
    renderWithProviders(<PlatformStatusPage />);

    expect(await screen.findByText("vault")).toBeInTheDocument();
    expect(screen.getByText("sealed")).toBeInTheDocument();
    // The API never sends exception text -- confirm nothing invented one.
    expect(screen.queryByText(/traceback|exception|error:/i)).not.toBeInTheDocument();
  });

  it('heads the module section "Registered modules" and renders no status indicator for a module', async () => {
    renderWithProviders(<PlatformStatusPage />);

    expect(await screen.findByText("Registered modules")).toBeInTheDocument();
    const devicesRow = (await screen.findByText("devices")).closest("tr") as HTMLElement;

    // STRUCTURAL, NOT TEXTUAL, and that is the whole point. The first version
    // of this assertion was `expect(devicesRow).not.toHaveTextContent(
    // /\bok\b|\bdegraded\b|\bdown\b/i)` -- which CANNOT FAIL. `toHaveTextContent`
    // concatenates cell text with no separator, so a row of (devices, 12, 4)
    // plus a status cell reads "devices124ok", and `\bok\b` needs a word
    // boundary that does not exist between "4" and "o". Verified: /\bok\b/i
    // matches "devices 12 4 ok" and does NOT match "devices124ok". Adding a
    // literal <Td>ok</Td> to ModuleRow left the old assertion green.
    //
    // Counting cells cannot be defeated that way: a module row has exactly the
    // three the registry measures -- name, routes, job types -- so any fourth
    // column, whatever it renders, fails here.
    const cells = devicesRow.querySelectorAll("td");
    expect(cells).toHaveLength(3);
    expect(cells[0]).toHaveTextContent("devices");

    // And no status/health column header on the modules table at all. This one
    // is safe as text: a header cell's content is not concatenated with digits.
    const table = devicesRow.closest("table") as HTMLElement;
    expect(table).not.toHaveTextContent(/status/i);
    expect(table).not.toHaveTextContent(/health/i);
  });

  it("renders a process with healthy_instances < instances differently from one where they are equal", async () => {
    renderWithProviders(<PlatformStatusPage />);

    // "api": 2/2, fully healthy.
    expect(await screen.findByText("2 / 2")).toBeInTheDocument();
    // "gateway": 1/3, a partial outage -- must not collapse into the same
    // "N / N" shape or rely on colour alone to say so.
    const shortfallCell = screen.getByText(/1 \/ 3/);
    expect(shortfallCell).toBeInTheDocument();
    expect(shortfallCell.textContent).not.toBe("2 / 2");
    // A warn pill with its glyph, not a bolder shade of the same text: the
    // distinction has to survive without colour.
    expect(shortfallCell).toHaveAttribute("data-kind", "warn");
  });

  it("shows the overall status and each probe's own status", async () => {
    renderWithProviders(<PlatformStatusPage />);

    await screen.findByText("vault");
    // THE PILLS SAY IT IN WORDS, not in the wire enum. `label={status}` put a
    // lowercase "ok" on the page-head chip and on nine table rows, two lines
    // under prose that writes "OK" -- so the assertion is on the labels a
    // person reads, and on the raw word being gone from the pills.
    expect(screen.getAllByText("Degraded").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Down").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Healthy").length).toBeGreaterThan(0);
    expect(screen.queryByText("ok")).not.toBeInTheDocument();
  });
  it("renders a probe's detail, because for one probe that is the whole answer", async () => {
    // THE FAILURE THIS GUARDS is a page that reads `keycloak_events / ok / —`
    // on a deployment where Keycloak credential events are not reaching the
    // audit chain at all. The probe answers `ok` on purpose; `configured:
    // false` is the part that says forwarding is off, and a page that drops
    // `detail` turns a deliberately-visible gap back into a silent one.
    renderWithProviders(<PlatformStatusPage />);
    const row = (await screen.findByText("keycloak_events")).closest("tr");
    expect(row).not.toBeNull();
    // The DETAIL is still the whole answer; it is now said in the reader's
    // alphabet ("Configured: no"), and the raw payload fragment is asserted
    // gone rather than merely not asserted -- strictly more than the line it
    // replaces.
    expect(row!.textContent).toContain("Configured: no");
    expect(row!.textContent).not.toContain("configured: false");
  });
});

/**
 * CYCLE 12 — the three findings above the fold.
 *
 * Auditor B, [HIGH]: *"The platform is genuinely DOWN and the only global
 * indicator is a 51x23px, 9px-type pill in the far corner."*
 * Auditor B, [MEDIUM]: *"No 'as of' timestamp, no refresh control … A regex
 * for `as of|updated|refreshed` over main.innerText returns false."*
 * Auditor B, [MEDIUM]: *"A failing dependency offers no next step —
 * 'unreachable' is the entire diagnostic."*
 *
 * Each test below was falsified against the pre-cycle-12 page.
 */
describe("PlatformStatusPage when something is failing", () => {
  const OBJECTSTORE_DOWN: PlatformStatus = {
    ...STATUS,
    overall: "down",
    probes: [
      { name: "database", status: "ok", duration_ms: 4, reason: null, detail: null },
      { name: "objectstore", status: "down", duration_ms: 0, reason: "unreachable", detail: null },
    ],
    processes: [
      {
        process: "api",
        instances: 2,
        healthy_instances: 2,
        oldest_age_s: 6,
        status: "ok",
        version: "1.4.0",
      },
    ],
  };

  const ALL_WELL: PlatformStatus = {
    ...OBJECTSTORE_DOWN,
    overall: "ok",
    probes: [{ name: "database", status: "ok", duration_ms: 4, reason: null, detail: null }],
  };

  function mock(status: PlatformStatus) {
    const api = {
      getPlatformStatus: vi.fn().mockResolvedValue(status),
      me: vi.fn().mockResolvedValue({
        user_id: "u1",
        org_id: "o1",
        email: "a@b.c",
        display_name: "Alex Morgan",
        permissions: ["platform.status.read"],
        timezone: "Europe/London",
      }),
    };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);
    return api;
  }

  it("raises a banner that NAMES the failing probe, not only a corner pill", async () => {
    mock(OBJECTSTORE_DOWN);
    renderWithProviders(<PlatformStatusPage />);

    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("1 of 2 dependency probes is failing");
    expect(banner).toHaveTextContent("objectstore unreachable");
  });

  it("gives the failing probe a next step instead of one word", async () => {
    mock(OBJECTSTORE_DOWN);
    renderWithProviders(<PlatformStatusPage />);

    const banner = await screen.findByRole("alert");
    // The gloss for `unreachable`, which the reason code alone never carried.
    expect(banner).toHaveTextContent(/Check that it is running/i);
    // And a way onwards, rather than a dead end.
    expect(within(banner).getByRole("link", { name: /open error groups/i })).toHaveAttribute(
      "href",
      "/error-monitor?state=open",
    );
  });

  it("carries the same guidance on the probe's own row, beside the code", async () => {
    mock(OBJECTSTORE_DOWN);
    renderWithProviders(<PlatformStatusPage />);

    const row = (await screen.findByText("objectstore")).closest("tr") as HTMLElement;
    // The raw code stays greppable AND is read as a word.
    expect(within(row).getByText("unreachable")).toBeInTheDocument();
    expect(within(row).getByText("Unreachable")).toBeInTheDocument();
    expect(row).toHaveTextContent(/Check that it is running/i);
  });

  it("raises no banner when every probe is OK", async () => {
    mock(ALL_WELL);
    renderWithProviders(<PlatformStatusPage />);

    await screen.findByText("database");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("raises the banner for a process shortfall even when every probe passes", async () => {
    mock({
      ...ALL_WELL,
      overall: "degraded",
      processes: [
        {
          process: "gateway",
          instances: 3,
          healthy_instances: 1,
          oldest_age_s: 12,
          status: "degraded",
          version: "1.4.0",
        },
      ],
    });
    renderWithProviders(<PlatformStatusPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "1 process is short of instances",
    );
  });

  it("says when the probes were read, and how often they are re-read", async () => {
    mock(OBJECTSTORE_DOWN);
    renderWithProviders(<PlatformStatusPage />);

    await screen.findByText("objectstore");
    // The zone is the operator's profile zone, and the interval is the one the
    // query actually polls at -- not a sentence that could drift from it.
    expect(screen.getByText(/Probes read at .*, and re-read every 15 seconds\./)).toBeInTheDocument();
  });

  it("re-probes on demand rather than leaving the operator to reload the tab", async () => {
    const api = mock(OBJECTSTORE_DOWN);
    const user = userEvent.setup();
    renderWithProviders(<PlatformStatusPage />);

    await screen.findByText("objectstore");
    expect(api.getPlatformStatus).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /re-probe now/i }));

    await waitFor(() => expect(api.getPlatformStatus).toHaveBeenCalledTimes(2));
  });
});

/**
 * NO DEAD TILES (visual cycle).
 *
 * "All tiles and all numbers or kpis need to be clickable and direct the user
 * to the specific menus, i dont want dead tiles everywhere" — the operator,
 * verbatim. `StatCard` requires `to`, so the compiler catches a MISSING
 * destination. It cannot catch a destination that goes nowhere, and on this
 * page that is the live risk: every tile points at a fragment of this same
 * page, because nothing else in the console renders a probe, a heartbeat or
 * the registry. A fragment with no matching element scrolls nowhere, which is
 * a dead tile that happens to have an href — so that is asserted structurally
 * rather than trusted.
 */
describe("PlatformStatusPage stat tiles", () => {
  function mockStatus(status: PlatformStatus) {
    mockUseApi.mockReturnValue({
      getPlatformStatus: vi.fn().mockResolvedValue(status),
      me: vi.fn().mockResolvedValue({
        user_id: "u1",
        org_id: "o1",
        email: "a@b.c",
        display_name: "Alex Morgan",
        permissions: ["platform.status.read"],
        timezone: "Europe/London",
      }),
    } as unknown as ApiClient);
  }

  it("renders every stat tile as a link with a real destination", async () => {
    mockStatus(STATUS);
    renderWithProviders(<PlatformStatusPage />);
    await screen.findByText("vault");

    const tiles = [...document.querySelectorAll(".stat-card")];
    expect(tiles).toHaveLength(4);

    for (const tile of tiles) {
      // AN ANCHOR, not a div with onClick: keyboard reachable, middle-
      // clickable into a new tab, pasteable into an incident channel.
      expect(tile.tagName).toBe("A");
      const href = tile.getAttribute("href");
      expect(href).toBeTruthy();
      expect(href).not.toBe("#");
    }

    expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([
      "/platform-status#dependencies",
      "/platform-status#dependencies",
      "/platform-status#processes",
      "/platform-status#modules",
    ]);
  });

  it("every fragment a tile points at is an element that exists on the page", async () => {
    // THE NON-VACUOUS HALF, and the one that actually decays. `#processes` is
    // a real href whether or not anything on the page carries that id, so a
    // test that only reads hrefs would stay green while all four tiles became
    // scroll-to-nowhere. This reads the DOM instead.
    mockStatus(STATUS);
    renderWithProviders(<PlatformStatusPage />);
    await screen.findByText("vault");

    const fragments = [...document.querySelectorAll(".stat-card")]
      .map((tile) => tile.getAttribute("href") ?? "")
      .filter((href) => href.includes("#"))
      .map((href) => decodeURIComponent(href.slice(href.indexOf("#") + 1)));

    // Non-vacuity: if the tiles ever stop using fragments this loop would
    // iterate zero times and assert nothing.
    expect(fragments).toHaveLength(4);
    for (const id of fragments) {
      expect(document.getElementById(id)).not.toBeNull();
    }
  });

  it("says WHICH zero each tile is reading, and drops the alarm on it", async () => {
    // A PENDING PROBE IS NOT A PASSING PROBE. Before this cycle every tile
    // here defaulted to "0", so an unmeasured platform rendered a clean bill
    // of health — the exact shape the audit filed nine times over.
    mockStatus({ overall: "ok", probes: [], processes: [], modules: [] });
    renderWithProviders(<PlatformStatusPage />);

    expect(await screen.findByText("This build registered no probes")).toBeInTheDocument();
    expect(screen.getByText("Every probe answered OK")).toBeInTheDocument();
    expect(screen.getByText("Every process is at full replica count")).toBeInTheDocument();
    expect(screen.getByText("Nothing registered at startup")).toBeInTheDocument();

    // Neither failure tile wears the saturated fill at zero: "0 probes not OK"
    // in alarm red reads as an emergency rather than as a clean bill.
    expect(document.querySelectorAll(".stat-card.urgent")).toHaveLength(0);
  });

  it("gives weight to the two failure counts, and to nothing else", async () => {
    // NON-VACUITY for the assertion above: "no tile is urgent" is trivially
    // true of a row where nothing can ever be urgent, which is the flat wall
    // of equally-loud numbers this replaces. STATUS has a down vault and a
    // gateway at 1/3 healthy, so exactly two tiles should carry weight.
    mockStatus(STATUS);
    renderWithProviders(<PlatformStatusPage />);
    await screen.findByText("vault");

    const urgent = [...document.querySelectorAll(".stat-card.urgent")];
    expect(urgent.map((card) => card.querySelector(".stat-label")?.textContent)).toEqual([
      "Probes not OK",
      "Processes short of instances",
    ]);
    // COLOUR IS CATEGORY: a failing dependency and a short replica count are
    // not the same event, and the page's own docstring says so.
    expect(urgent[0]!.className).toContain("tone-red");
    expect(urgent[1]!.className).toContain("tone-orange");
    // The two inventory counts stay inert.
    expect(document.querySelectorAll(".stat-card.tone-neutral")).toHaveLength(2);
  });

  it("gives every tile in the row a sub-line, not only the two that can read zero", async () => {
    // TWO OF FOUR CARRIED ONE AND TWO CARRIED NOTHING, so at the row's shared
    // height the inventory tiles showed an empty band under the figure -- most
    // of the tile at 390px, where each is full width. What goes there is the
    // fact the figure withholds: what the read cost, and what the registry
    // brought. Both are summaries of the columns in the tables below, never a
    // number invented here.
    mockStatus(STATUS);
    renderWithProviders(<PlatformStatusPage />);
    await screen.findByText("vault");

    const cards = [...document.querySelectorAll(".stat-card")];
    expect(cards).toHaveLength(4);
    for (const card of cards) {
      expect(card.querySelector(".z")?.textContent?.trim()).toBeTruthy();
    }
    // The slowest probe is the vault at 812 ms, and it is named.
    expect(screen.getByText("Slowest Vault, 812 ms")).toBeInTheDocument();
    // 12 + 9 routes, 4 + 3 job types, the registry table's own two columns.
    expect(screen.getByText("21 routes · 7 job types")).toBeInTheDocument();
  });

  it("draws no summary line where there is nothing to summarise", async () => {
    // A zero already says what it means through its `zeroLabel`; "0 routes ·
    // 0 job types" under it would be a second sentence about nothing.
    mockStatus({ overall: "ok", probes: [], processes: [], modules: [] });
    renderWithProviders(<PlatformStatusPage />);

    await screen.findByText("This build registered no probes");
    expect(screen.queryByText(/Slowest/)).not.toBeInTheDocument();
    expect(screen.queryByText(/job types/)).not.toBeInTheDocument();
  });

  it("does not render a zero label while the probes are still unread", async () => {
    // The other half of the same bug, and the half a zeroLabel cannot fix on
    // its own: an unresolved query must render an em dash, not "0", or the
    // zero label would be describing a measurement that never happened.
    mockUseApi.mockReturnValue({
      getPlatformStatus: () => new Promise<never>(() => {}),
      me: () => new Promise<never>(() => {}),
    } as unknown as ApiClient);
    renderWithProviders(<PlatformStatusPage />);

    await screen.findByText("Loading…");
    const values = [...document.querySelectorAll(".stat-value")].map((n) => n.textContent);
    expect(values).toEqual(["\u2014", "\u2014", "\u2014", "\u2014"]);
    expect(screen.queryByText("Every probe answered OK")).not.toBeInTheDocument();
  });
});
