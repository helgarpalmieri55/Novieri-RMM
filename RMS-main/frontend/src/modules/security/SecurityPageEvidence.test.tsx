/**
 * Security logs as GOVERNANCE EVIDENCE — cycle 12, auditor A's findings A58
 * through A64.
 *
 * The theme running through all seven is that this page is read by somebody
 * who will quote it: into an incident ticket, into a regulator's request, into
 * a SOC 2 evidence pack. Every assertion below is about the page telling that
 * reader the truth about WHAT THEY ARE LOOKING AT — which window, which
 * filters, which of the three numbers is a total and which is a sample, and
 * whether the file the Export button produces is the thing on screen.
 *
 * Each test in this file was falsified against the pre-cycle implementation:
 * the aria assertions against three bare `<button>`s, the URL assertions
 * against `useState`, the stat-card assertions against "Failed logins
 * (loaded)", and the empty-state assertions against the single sentence "No
 * events in this window."
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me, SecurityEvent } from "../../lib/api";
import { SecurityPage } from "./SecurityPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "a@b.c",
  display_name: "Alex Morgan",
  permissions: ["audit.read"],
  timezone: "Asia/Tokyo",
} as Me;

function event(overrides: Partial<SecurityEvent> = {}): SecurityEvent {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    device_id: "d1",
    hostname: "DC-01",
    ts: "2026-08-16T02:32:08Z",
    event_type: "logon_failure",
    username: "jrivera",
    source: "10.20.16.87",
    outcome: "failure",
    ...overrides,
  };
}

/** The live URL, so "is this view addressable?" is asserted and not assumed. */
let currentUrl = "";
function LocationProbe() {
  const location = useLocation();
  currentUrl = `${location.pathname}${location.search}`;
  return null;
}

function renderPage(overrides: Partial<ApiClient> = {}, entry = "/security") {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue(ME),
    listSecurityEvents: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    getSecurityEvent: vi.fn(),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <LocationProbe />
        <SecurityPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return { api, ...render(ui) };
}

beforeEach(() => {
  mockUseApi.mockReset();
  currentUrl = "";
});

/** jsdom implements neither object URLs nor following an anchor. */
function stubSave(): void {
  Object.assign(URL, {
    createObjectURL: vi.fn().mockReturnValue("blob:evidence"),
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
}

// --------------------------------------------------------------- A62 the URL

describe("the evidence view is addressable (A62)", () => {
  it("hydrates all three filters from the query string on load", async () => {
    const { api } = renderPage({}, "/security?username=opsadmin&event_type=logon_failure&hours=24");

    // The request the page makes is the proof: a page that ignored the URL
    // would ask for no username, no event type and a 7-day window.
    await vi.waitFor(() =>
      expect(api.listSecurityEvents).toHaveBeenCalledWith(
        expect.objectContaining({ username: "opsadmin", event_type: "logon_failure" }),
      ),
    );
    expect(screen.getByLabelText("Search by username")).toHaveValue("opsadmin");
    expect(screen.getByLabelText("Filter by event type")).toHaveValue("logon_failure");
    expect(screen.getByRole("radio", { name: /24h/ })).toHaveAttribute("aria-checked", "true");
  });

  it("puts the username, the event type and the range INTO the url as they are set", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "Security logs" });

    await user.type(screen.getByLabelText("Search by username"), "opsadmin");
    await user.selectOptions(screen.getByLabelText("Filter by event type"), "logon_failure");
    await user.click(screen.getByRole("radio", { name: /24h/ }));

    expect(currentUrl).toContain("username=opsadmin");
    expect(currentUrl).toContain("event_type=logon_failure");
    expect(currentUrl).toContain("hours=24");
  });

  it("drops a filter from the url when it is cleared, rather than leaving a stale one", async () => {
    const user = userEvent.setup();
    renderPage({}, "/security?username=opsadmin");
    await screen.findByRole("heading", { name: "Security logs" });

    await user.clear(screen.getByLabelText("Search by username"));
    expect(currentUrl).not.toContain("username");
  });

  it("ignores a hand-edited range the page cannot express instead of inventing a window", async () => {
    renderPage({}, "/security?hours=9999");
    // Falls back to the 7d default; no fourth radio appears.
    expect(await screen.findByRole("radio", { name: /7d/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getAllByRole("radio")).toHaveLength(3);
  });
});

// ------------------------------------------------- A59 the time-range control

describe("the time-range control exposes its selection (A59)", () => {
  it("is a labelled radio group whose checked option is the one on screen", async () => {
    renderPage({}, "/security?hours=720");
    const group = await screen.findByRole("radiogroup", { name: "Time range" });
    const options = within(group).getAllByRole("radio");
    expect(options.map((option) => option.getAttribute("aria-checked"))).toEqual([
      "false",
      "false",
      "true",
    ]);
  });

  it("marks the selection without relying on colour", async () => {
    renderPage({}, "/security?hours=24");
    // A check glyph on the selected option, so a purple fill is not the only
    // difference between "24h" and "7d" for a reader who cannot see the fill.
    const selected = await screen.findByRole("radio", { name: /24h/ });
    expect(selected.textContent).toContain("✓");
    expect(screen.getByRole("radio", { name: /7d/ }).textContent).not.toContain("✓");
  });

  it("is one tab stop, and the arrow keys move within it", async () => {
    const user = userEvent.setup();
    renderPage({}, "/security?hours=24");
    const group = await screen.findByRole("radiogroup", { name: "Time range" });
    const options = within(group).getAllByRole("radio");
    expect(options.map((option) => option.getAttribute("tabindex"))).toEqual(["0", "-1", "-1"]);

    options[0]?.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: /7d/ })).toHaveAttribute("aria-checked", "true");
    expect(currentUrl).toContain("hours=168");
  });
});

// ---------------------------------------------------- A60 one answer to where

describe("the page agrees with the rest of the console about where it is (A60)", () => {
  it("takes its title from its nav label and draws no eyebrow of its own", async () => {
    renderPage();
    // `shell/nav.ts` files /security under the Fleet group; the breadcrumb
    // and top-bar title are rendered from that same table, so the title is
    // what makes all three agree. It is SENTENCE CASE here, which is the
    // port's rule and what the design titles this route; the nav label was
    // moved to sentence case in the same wave, so all three now agree in
    // wording AND in case (`shell/nav.ts`, "Security logs"). The
    // eyebrow that used to restate the group is gone on the v3 kit: the
    // shell's crumb is the one answer to "where am I", and a page-drawn
    // second one is what this test was written against.
    expect(await screen.findByRole("heading", { name: "Security logs" })).toBeInTheDocument();
    expect(document.querySelector(".eyebrow")).toBeNull();
    expect(screen.queryByText(/governance evidence/i)).not.toBeInTheDocument();
  });
});

// ----------------------------------------------------------- A61 stat qualifiers

describe("the stat cards say what they counted (A61)", () => {
  it("never qualifies a number with the implementation word 'loaded'", async () => {
    renderPage({ listSecurityEvents: () => Promise.resolve({ items: [event()], total: 40 }) });
    await screen.findByText("DC-01");
    expect(screen.queryByText(/\(loaded\)/)).not.toBeInTheDocument();
  });

  it("states in words that two of the three are computed over the page in hand", async () => {
    renderPage({
      listSecurityEvents: () =>
        Promise.resolve({ items: [event(), event({ id: "2", hostname: "DC-02" })], total: 40 }),
    });
    await screen.findByText("DC-02");
    const feet = [...document.querySelectorAll(".stat-card")].map(
      (card) => card.querySelector(".stat-foot")?.textContent,
    );
    expect(feet[0]).toBe("the last 7 days");
    expect(feet[1]).toBe("Across the 2 events loaded");
    expect(feet[2]).toBe("Across the 2 events loaded");
  });
});

// --------------------------------------------------------------- A58 the export

describe("the evidence export says what it will contain (A58)", () => {
  // A58 IS CLOSED FROM THE BACKEND SIDE NOW, so these three tests changed
  // shape rather than being deleted. `GET /api/v1/reports/access.csv` grew
  // `username` and `event_type` (closeout recommendation 7); until it did,
  // putting them in this href would have sent two parameters FastAPI
  // discarded while the link claimed otherwise, so the page named the window
  // instead and said out loud which filters the file did NOT carry. That
  // disclosure is now false, and a page that keeps disclaiming a limitation it
  // no longer has is lying in the other direction.
  it("puts the filters on screen into the request, so the file is the view", async () => {
    // AND IT IS A REQUEST NOW, WHICH IS THE OTHER HALF OF THIS. It was
    // `<a href={csvHref} download>`, and a browser following a link of its own
    // accord sends no `Authorization` header -- `audit.read` on the route saw
    // an anonymous caller, answered 401, and the browser saved the refusal as
    // the evidence file. The same anchor shipped on Devices and Software; the
    // templated href here is why a sweep for `href="/api` found only those
    // two. So the assertion moves from the markup to the call: a link cannot
    // carry a bearer token, and one that could would still be untestable
    // from here.
    const exportAccessEvidence = vi.fn().mockResolvedValue(new Blob(["ts\n"]));
    stubSave();
    renderPage(
      { exportAccessEvidence },
      "/security?username=opsadmin&event_type=logon_failure&hours=24",
    );
    expect(screen.queryByRole("link", { name: /Export evidence/ })).toBeNull();
    await userEvent.click(
      await screen.findByRole("button", { name: /Export evidence CSV \(24h\)/ }),
    );
    expect(exportAccessEvidence).toHaveBeenCalledWith(
      expect.objectContaining({ username: "opsadmin", event_type: "logon_failure" }),
    );
    expect(exportAccessEvidence.mock.calls[0]?.[0]?.from_ts).toBeTruthy();
  });

  it("says the export failed rather than saving the failure", async () => {
    // The anchor's failure mode was silent by construction, and an evidence
    // export that quietly writes the wrong file is worse here than anywhere
    // else in the product -- the file is destined for an audit pack.
    const exportAccessEvidence = vi.fn().mockRejectedValue(new ApiError(403, ""));
    renderPage({ exportAccessEvidence }, "/security?hours=24");
    await userEvent.click(
      await screen.findByRole("button", { name: /Export evidence CSV \(24h\)/ }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /You do not have permission to do that/i,
    );
  });

  it("says that the export carries the filters, whenever one is set", async () => {
    renderPage({}, "/security?username=opsadmin&hours=24");
    expect(await screen.findByText(/carries the username and event-type/i)).toBeInTheDocument();
    expect(screen.queryByText(/not applied to the export/i)).not.toBeInTheDocument();
  });

  it("names the window in the label, and sends no filter it does not have", async () => {
    // No `?hours=`, so this is the page's default window (7d) -- which is the
    // point: the label reports the window in force, not a hard-wired one.
    const exportAccessEvidence = vi.fn().mockResolvedValue(new Blob(["ts\n"]));
    stubSave();
    renderPage({ exportAccessEvidence });
    await userEvent.click(
      await screen.findByRole("button", { name: /Export evidence CSV \(7d\)/ }),
    );
    const sent = exportAccessEvidence.mock.calls[0]?.[0] ?? {};
    expect(sent).not.toHaveProperty("username");
    expect(sent).not.toHaveProperty("event_type");
    expect(screen.queryByText(/carries the username and event-type/i)).not.toBeInTheDocument();
  });

  it("offers a copy of the link to this exact view", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderPage({}, "/security?username=opsadmin&hours=24");
    await screen.findByRole("heading", { name: "Security logs" });

    await user.click(screen.getByRole("button", { name: /Copy link to this view/ }));
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining("username=opsadmin"));
    expect(await screen.findByRole("status")).toHaveTextContent(/Link copied/);
  });
});

// ----------------------------------------------------------- A63 the empty state

describe("an empty result explains itself (A63)", () => {
  it("names the filters that produced the emptiness", async () => {
    renderPage({}, "/security?username=opsadmin&event_type=logon_failure&hours=24");
    const empty = await screen.findByText(/No events match these filters/);
    const body = empty.parentElement?.querySelector("p") as HTMLElement;
    expect(body).toHaveTextContent(/the last 24 hours/);
    expect(body).toHaveTextContent(/opsadmin/);
    expect(body).toHaveTextContent(/Logon failure/);
  });

  it("offers one click to widen the window and one to clear the filters", async () => {
    const user = userEvent.setup();
    renderPage({}, "/security?username=opsadmin&hours=24");
    await user.click(await screen.findByRole("button", { name: "Widen to 30d" }));
    expect(currentUrl).toContain("hours=720");

    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(currentUrl).not.toContain("username");
  });

  it("distinguishes 'nothing matched' from 'nothing was ever collected'", async () => {
    renderPage();
    expect(await screen.findByText("No events in this window")).toBeInTheDocument();
    // The security-context distinction the auditor asked for: an empty log and
    // an agent that is not shipping logs are not the same conclusion.
    expect(
      screen.getByText(/An empty log and an agent not collecting one look identical/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
  });
});

// -------------------------------------------------------- A64 the raw-event panel

describe("the raw-event panel is read, not dumped (A64)", () => {
  it("labels the stored fields instead of printing the JSON as the default view", async () => {
    const user = userEvent.setup();
    renderPage({
      listSecurityEvents: () => Promise.resolve({ items: [event()], total: 1 }),
      getSecurityEvent: vi.fn().mockResolvedValue({
        ...event(),
        raw: { event_id: 4625, logon_type: "remote_interactive", target_user: "opsadmin" },
      }),
    });
    const row = (await screen.findByText("DC-01")).closest("tr") as HTMLElement;
    await user.click(row);

    // Keys as words, enum values as words.
    const panel = await screen.findByText("Logon type");
    expect(panel).toHaveClass("attribute-key");
    expect(screen.getByText("Remote interactive")).toBeInTheDocument();
    // `event_id` reads as "Event", not as `event_id`.
    const keys = [...document.querySelectorAll(".attribute-key")].map((n) => n.textContent?.trim());
    expect(keys).toEqual(["Event", "Logon type", "Target user"]);
    // And the object is still reachable, behind a closed disclosure.
    expect(screen.getByText("View raw")).toBeInTheDocument();
    expect(document.querySelector("details.attribute-raw")).not.toHaveAttribute("open");
  });

  it("says the event carries no stored fields rather than rendering {}", async () => {
    const user = userEvent.setup();
    renderPage({
      listSecurityEvents: () => Promise.resolve({ items: [event()], total: 1 }),
      getSecurityEvent: vi.fn().mockResolvedValue({ ...event(), raw: {} }),
    });
    const row = (await screen.findByText("DC-01")).closest("tr") as HTMLElement;
    await user.click(row);
    expect(await screen.findByText("This event carries no stored fields.")).toBeInTheDocument();
  });
});

// ------------------------------------------------- the tiles are not dead ends

/**
 * THE TILES ARE LINKS, AND EACH ONE OPENS THE SEARCH THAT PRODUCED ITS NUMBER.
 *
 * "All tiles and all numbers or kpis need to be clickable and direct the user
 * to the specific menus, i dont want dead tiles everywhere" — the operator,
 * 2026-08-21. `StatCard` requires `to`, so the compiler catches a tile with no
 * destination; it cannot catch a destination that drops the window the number
 * was counted over, which is the assertion that matters on this page: "17
 * failed logins" means seventeen in THIS window, and a link that opened the
 * default seven days would not hold the number printed on it.
 */
describe("the stat tiles open the search behind them", () => {
  function tileHrefs(): Record<string, string | null> {
    return Object.fromEntries(
      [...document.querySelectorAll("a.stat-card")].map((card) => [
        card.querySelector(".stat-label")?.textContent ?? "",
        card.getAttribute("href"),
      ]),
    );
  }

  it("gives every tile a real href — none is a dead tile", async () => {
    renderPage({ listSecurityEvents: () => Promise.resolve({ items: [event()], total: 3 }) });
    await screen.findByText("DC-01");

    expect(tileHrefs()).toEqual({
      // The window is stated explicitly even when it is the default, so the
      // link still means these seven days if the default ever moves.
      "Events in window": "/security?hours=168",
      "Failed logins": "/security?event_type=logon_failure&hours=168",
      "Accounts seen": "/security?hours=168",
    });
  });

  it("carries the window the number was counted over into every destination", async () => {
    renderPage(
      { listSecurityEvents: () => Promise.resolve({ items: [event()], total: 3 }) },
      "/security?username=opsadmin&hours=24",
    );
    await screen.findByText("DC-01");

    expect(tileHrefs()).toEqual({
      // The server's total for the WINDOW, so its destination drops the
      // narrowing filters and keeps the 24 hours.
      "Events in window": "/security?hours=24",
      // The drill-down: same window, same principal, narrowed to the type.
      "Failed logins": "/security?username=opsadmin&hours=24&event_type=logon_failure",
      // Principals are counted across the window; a username filter would
      // leave exactly one, so it is dropped.
      "Accounts seen": "/security?hours=24",
    });
  });

  it("actually navigates and re-runs the search when a tile is followed", async () => {
    const user = userEvent.setup();
    const { api } = renderPage({
      listSecurityEvents: vi.fn().mockResolvedValue({ items: [event()], total: 3 }),
    });
    await screen.findByText("DC-01");

    const failed = [...document.querySelectorAll<HTMLElement>("a.stat-card")].find(
      (card) => card.querySelector(".stat-label")?.textContent === "Failed logins",
    ) as HTMLElement;
    await user.click(failed);

    expect(currentUrl).toBe("/security?event_type=logon_failure&hours=168");
    // The URL is the state, so following the tile refetches with the filter —
    // a link that changed the address bar and nothing else would be theatre.
    await vi.waitFor(() =>
      expect(api.listSecurityEvents).toHaveBeenCalledWith(
        expect.objectContaining({ event_type: "logon_failure" }),
      ),
    );
    expect(screen.getByLabelText("Filter by event type")).toHaveValue("logon_failure");
  });

  it("says WHICH zero a zero is", async () => {
    renderPage({ listSecurityEvents: () => Promise.resolve({ items: [], total: 0 }) });
    expect(await screen.findByText("Nothing was collected in this window")).toBeInTheDocument();
    expect(screen.getByText("No failed logon in this window")).toBeInTheDocument();
    // A zero never wears the urgent treatment: "0 failed logins" in an alarm
    // colour reads as a perfect score.
    expect(document.querySelectorAll("a.stat-card.urgent")).toHaveLength(0);
  });

  it("prints no number at all before the search has answered", async () => {
    // `?? 0` printed the same 0 for "none happened" and "nobody has looked
    // yet" — the two conclusions a security reader must never confuse.
    renderPage({ listSecurityEvents: () => new Promise<never>(() => {}) });
    await screen.findByText("Loading…");
    const values = [...document.querySelectorAll("a.stat-card .stat-value")].map(
      (value) => value.textContent,
    );
    expect(values).toEqual(["—", "—", "—"]);
  });

  it("shouts about the failures and nothing else", async () => {
    renderPage({
      listSecurityEvents: () =>
        Promise.resolve({
          items: [event(), event({ id: "2", hostname: "DC-02", event_type: "logon_success" })],
          total: 2,
        }),
    });
    await screen.findByText("DC-02");

    const urgent = [...document.querySelectorAll("a.stat-card.urgent")].map(
      (card) => card.querySelector(".stat-label")?.textContent,
    );
    expect(urgent).toEqual(["Failed logins"]);
  });
});
