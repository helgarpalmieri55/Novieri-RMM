import { existsSync, readFileSync } from "node:fs";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Device, Me, Page, RemoteSession } from "../../lib/api";
import { SessionsPage } from "./SessionsPage";

// The auth-backed API client is mocked so this is a pure component test: no
// real WebSocket, no real fetch. Sessions are plain TanStack Query state
// (no live-event stream), so freshness is verified by asserting the list is
// refetched after the start-session mutation invalidates it.
const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWithProviders(ui: ReactElement, entry = "/remote") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * THE ROUTE TABLE, READ FROM `App.tsx` ITSELF.
 *
 * "I don't want dead tiles everywhere" only stays kept if the destinations are
 * checked against the routes that exist. The catch-all is excluded: a tile that
 * matches only `*` is precisely the dead tile — it redirects to the device list
 * and calls that a destination.
 */
function appRoutes(): RegExp[] {
  // From the working directory, not `import.meta.url`: under jsdom that URL is
  // not a file: URL.
  const path = existsSync("src/App.tsx") ? "src/App.tsx" : "frontend/src/App.tsx";
  return [...readFileSync(path, "utf8").matchAll(/path="([^"]+)"/g)]
    .map((match) => match[1]!)
    .filter((route) => route !== "*")
    .map((route) => new RegExp(`^${route.replace(/:[^/]+/g, "[^/]+")}$`));
}

function expectTileLinks(expected: readonly string[]) {
  const tiles = [...document.querySelectorAll<HTMLAnchorElement>("a.stat-card")];
  expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([...expected]);
  const routes = appRoutes();
  for (const href of expected) {
    const path = href.split("?")[0]!;
    expect(routes.some((route) => route.test(path)), `${path} is not a route`).toBe(true);
  }
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["remote.control", "remote.recordings.read"],
};

const SESSIONS: RemoteSession[] = [
  {
    id: "s1",
    device_id: "d1",
    technician_id: "u1",
    protocol: "rdp",
    consent_mode: "attended",
    state: "active",
    credential_id: null,
    has_recording: false,
    started_at: "2026-01-01T00:00:00.000Z",
    ended_at: null,
    end_reason: null,
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const DEVICES: Device[] = [
  {
    id: "d1",
    hostname: "workstation-1",
    fqdn: null,
    os_family: "windows",
    os_version: "11",
    architecture: "x64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    // Asset fields (asset-inventory spec §2). Defaulted to what every
    // agent-enrolled device is, so these tests describe the same fleet they
    // did before assets existed.
    lifecycle: "in_service" as const,
    asset_type: "computer" as const,
    source: "agent" as const,
    // An enrolled machine proved its own identity at enrolment; nobody
    // had to vouch for the row (asset-inventory spec §3).
    provenance: "agent" as const,
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
  },
];

describe("SessionsPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listRemoteSessions: vi
        .fn()
        .mockResolvedValue({ items: SESSIONS, total: 1 } satisfies Page<RemoteSession>),
      listDevices: vi.fn().mockResolvedValue({ items: DEVICES, total: 1 } satisfies Page<Device>),
      listCredentials: vi.fn().mockResolvedValue([]),
      startRemoteSession: vi.fn().mockResolvedValue({
        session_id: "s2",
        state: "awaiting_consent",
        consent_mode: "attended",
      }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("lists sessions with device hostname, protocol, consent, and state", async () => {
    renderWithProviders(<SessionsPage />);

    expect(await screen.findByText("workstation-1")).toBeInTheDocument();
    expect(screen.getByText("RDP")).toBeInTheDocument();
    expect(screen.getByText("Attended")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  // The product-wide raw-enum defect (lib/labels.ts documents it across seven
  // modules), in the column that says whether somebody is on an employee's
  // machine right now. BREAKS AGAINST `{session.state}`.
  it("renders the session state as words, not as the wire identifier", async () => {
    api.listRemoteSessions = vi.fn().mockResolvedValue({
      items: [{ ...SESSIONS[0]!, state: "awaiting_consent" }],
      total: 1,
    } satisfies Page<RemoteSession>);
    renderWithProviders(<SessionsPage />);

    expect(await screen.findByText("Awaiting consent")).toBeInTheDocument();
    expect(screen.queryByText("awaiting_consent")).not.toBeInTheDocument();
  });

  it("does not render Start session for a caller without remote.control", async () => {
    api.me = vi.fn().mockResolvedValue({ ...ME, permissions: ["remote.recordings.read"] });
    renderWithProviders(<SessionsPage />);

    await screen.findByText("workstation-1");
    expect(screen.queryByRole("button", { name: "Start session" })).not.toBeInTheDocument();
  });

  // THE THREE STATES A MOCK NEVER HAS — mirrors DevicesPage.test.tsx.
  it("says it is loading rather than showing an empty table", async () => {
    api.listRemoteSessions = vi.fn(() => new Promise<never>(() => {}));
    renderWithProviders(<SessionsPage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.listRemoteSessions = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<SessionsPage />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No remote sessions/i)).not.toBeInTheDocument();
  });

  it("says there are no sessions when there genuinely are none", async () => {
    api.listRemoteSessions = vi
      .fn()
      .mockResolvedValue({ items: [], total: 0 } satisfies Page<RemoteSession>);
    renderWithProviders(<SessionsPage />);
    expect(await screen.findByText("No remote sessions yet.")).toBeInTheDocument();
  });

  it("opens the start dialog and submits the create mutation with the right body", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SessionsPage />);

    await screen.findByText("workstation-1");
    await user.click(screen.getByRole("button", { name: "Start session" }));

    await screen.findByRole("heading", { name: "Start remote session" });
    // BY ROLE, not `heading.parentElement`. The old form assumed the title was
    // a direct child of the element wrapping the dialog's fields; the UI port
    // (2026-08-16) gave the dialog the design's `.modal-head` / `.modal-body`
    // structure and the title is now nested one level deeper. Nothing about
    // what this test checks changed -- only how it finds the dialog, and
    // `role="dialog"` is what it meant all along.
    const dialog = screen.getByRole("dialog") as HTMLElement;

    // The Device `<select>` became an `EntityPicker` (cycle 6): open the
    // combobox and click the row. `getByRole("combobox", ...)` rather than
    // `getByLabelText` because the picker's listbox carries the same
    // `aria-label` whenever it is open.
    await user.click(within(dialog).getByRole("combobox", { name: "Device" }));
    await user.click(await screen.findByRole("option", { name: /workstation-1/ }));
    await user.type(within(dialog).getByLabelText("Reason"), "helpdesk ticket 4821");
    await user.click(within(dialog).getByRole("button", { name: "Start session" }));

    expect(api.startRemoteSession).toHaveBeenCalledWith({
      device_id: "d1",
      protocol: "screen",
      mode: "attended",
      reason: "helpdesk ticket 4821",
    });
  });

  it("refetches the sessions list once the start-session mutation succeeds", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SessionsPage />);

    await screen.findByText("workstation-1");
    expect(api.listRemoteSessions).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Start session" }));
    await screen.findByRole("heading", { name: "Start remote session" });
    // BY ROLE, not `heading.parentElement`. The old form assumed the title was
    // a direct child of the element wrapping the dialog's fields; the UI port
    // (2026-08-16) gave the dialog the design's `.modal-head` / `.modal-body`
    // structure and the title is now nested one level deeper. Nothing about
    // what this test checks changed -- only how it finds the dialog, and
    // `role="dialog"` is what it meant all along.
    const dialog = screen.getByRole("dialog") as HTMLElement;

    // The Device `<select>` became an `EntityPicker` (cycle 6): open the
    // combobox and click the row. `getByRole("combobox", ...)` rather than
    // `getByLabelText` because the picker's listbox carries the same
    // `aria-label` whenever it is open.
    await user.click(within(dialog).getByRole("combobox", { name: "Device" }));
    await user.click(await screen.findByRole("option", { name: /workstation-1/ }));
    await user.type(within(dialog).getByLabelText("Reason"), "helpdesk ticket 4821");
    await user.click(within(dialog).getByRole("button", { name: "Start session" }));

    await waitFor(() => expect(api.startRemoteSession).toHaveBeenCalledTimes(1));
    // The mutation's onSuccess invalidates ["remote-sessions"], which — with
    // no live-event stream backing it — is the only thing that keeps the
    // list current, so this refetch is what makes the UI honest.
    await waitFor(() => expect(api.listRemoteSessions).toHaveBeenCalledTimes(2));
  });

  // AUDIT [LOW], /remote → stat cards: "'Online devices' is a live fleet
  // condition, 'Sessions' is an all-time total, and 'Active now' is a
  // point-in-time count — three different temporal meanings rendered
  // identically, so the row cannot be read at a glance."
  //
  // BREAKS AGAINST: the pre-cycle cards, which passed no `foot` at all.
  // "All tiles and all numbers or KPIs need to be clickable and direct the user
  // to the specific menus, i dont want dead tiles everywhere" — the operator,
  // 2026-08-21. Two claims, and the second is the one that rots: that a tile is
  // a link stays true on its own; that the link LANDS ON THE ROWS IT COUNTED
  // decays silently, because a `?state=` the page has stopped reading still
  // renders a page that looks fine.
  it("gives every tile a destination App.tsx actually routes", async () => {
    renderWithProviders(<SessionsPage />);
    await screen.findByText("workstation-1");

    expectTileLinks([
      "/devices?state=online",
      "/remote",
      "/remote?state=active",
      "/remote?state=recorded",
    ]);
  });

  it("opens the live sessions at the destination the Active now tile names", async () => {
    api.listRemoteSessions = vi.fn().mockResolvedValue({
      items: [
        SESSIONS[0]!,
        { ...SESSIONS[0]!, id: "s2", device_id: "d1", state: "ended", ended_at: "2026-01-01T01:00:00.000Z" },
      ],
      total: 2,
    } satisfies Page<RemoteSession>);
    renderWithProviders(<SessionsPage />, "/remote?state=active");

    // One row, and it is the one that has not ended. BREAKS AGAINST a
    // `?state=` this page renders and ignores.
    const rows = await screen.findAllByRole("row");
    expect(rows).toHaveLength(2); // header + the live session
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.queryByText("Ended")).not.toBeInTheDocument();
  });

  it("opens the recorded sessions at the destination the Recorded tile names", async () => {
    api.listRemoteSessions = vi.fn().mockResolvedValue({
      items: [SESSIONS[0]!, { ...SESSIONS[0]!, id: "s2", has_recording: true }],
      total: 2,
    } satisfies Page<RemoteSession>);
    renderWithProviders(<SessionsPage />, "/remote?state=recorded");

    const rows = await screen.findAllByRole("row");
    expect(rows).toHaveLength(2); // header + the one with a recording
    // The row's pill reads in sentence case, as every pill on the page does
    // (the Recorded tile above the table says the same word).
    expect(within(rows[1]!).getByText("Recorded")).toBeInTheDocument();
  });

  /**
   * DESIGN REVIEW M2, the mixed-cell case on this page. `Open` was
   * `.btn small` and `Playback` beside it was `.btn.ghost.small`, which paints
   * as bare accent text -- the shape a link has. One cell, two treatments, and
   * both of them navigate, so the difference in drawing was carrying no
   * meaning at all.
   *
   * THE CONVENTION IS THE CONSOLE'S OWN, written down at
   * `subscriptions/SubscriptionDetail.tsx`: "a row control is a control, not a
   * link". Every control in a row's action cell carries an edge; whether it is
   * a `<button>` or a `<Link>` stays a question for the ELEMENT, which is what
   * a keyboard, a middle click and a screen reader read -- not for a second
   * visual language on top of it. Both of these stay real `<a>` elements, for
   * the `nested-interactive` reason the code comment beside them gives.
   */
  it("draws both row controls with one treatment, and leaves them real links", async () => {
    api.listRemoteSessions = vi.fn().mockResolvedValue({
      items: [{ ...SESSIONS[0]!, id: "s2", has_recording: true }],
      total: 1,
    } satisfies Page<RemoteSession>);
    renderWithProviders(<SessionsPage />);

    const open = await screen.findByRole("link", { name: /Open/ });
    const playback = screen.getByRole("link", { name: /Playback/ });

    expect(open.closest("td")).toContainElement(playback);
    for (const control of [open, playback]) {
      expect(control.tagName).toBe("A");
      expect(control).toHaveClass("btn");
      expect(control).not.toHaveClass("ghost");
    }
  });

  /**
   * THE SEARCH BOX RUNS THE FULL WIDTH OF ITS BAR (design review I5).
   *
   * `.filter-spacer` is `flex: 1` and so is the kit's search wrapper, so a
   * spacer with NOTHING AFTER IT pushes nothing to the right: it takes half
   * the bar from the field beside it. Measured at 1440 the box stopped at
   * ~675px inside a 1140px card. /licenses earns its spacer (a result count
   * follows it); this row had no such thing to push.
   *
   * ASSERTED ON THE DOM, not on a width: jsdom lays nothing out, so a measured
   * width here would pass against any markup. The presence of a trailing
   * spacer IS the defect.
   */
  it("leaves no trailing spacer to take half the filter bar from the search box", async () => {
    const { container } = renderWithProviders(<SessionsPage />);
    await screen.findByText("workstation-1");

    const bar = container.querySelector(".filterbar");
    expect(bar).not.toBeNull();
    expect(bar?.querySelector(".srch-wrap")).not.toBeNull();
    // A spacer is only honest with something after it to push.
    const spacers = [...(bar?.querySelectorAll(".filter-spacer") ?? [])];
    for (const spacer of spacers) {
      expect(spacer.nextElementSibling).not.toBeNull();
    }
    expect(bar?.lastElementChild?.classList.contains("filter-spacer")).toBe(false);
  });

  it("says which timeframe each stat card covers", async () => {
    renderWithProviders(<SessionsPage />);
    await screen.findByText("workstation-1");

    const cards = [...document.querySelectorAll(".stat-card")].map((card) => card.textContent);
    expect(cards[0]).toMatch(/Online devices.*Fleet, right now/s);
    expect(cards[1]).toMatch(/Sessions.*All time/s);
    expect(cards[2]).toMatch(/Active now.*in this list/s);
    expect(cards[3]).toMatch(/Recorded.*in this list/s);
  });
});
