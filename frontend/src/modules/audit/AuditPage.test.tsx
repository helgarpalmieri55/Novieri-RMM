/**
 * Immutable audit.
 *
 * Two families of assertion, and the second is the reason this file is longer
 * than the Devices one it copies.
 *
 * 1. THE THREE STATES (`DevicesPage.test.tsx`'s subject): loading, empty and
 *    error must be distinguishable. An empty audit log and a dead audit
 *    endpoint look identical to an operator otherwise, and one of those two is
 *    an incident.
 *
 * 2. THE INTEGRITY CLAIM. `GET /audit/verify` has three outcomes the page must
 *    keep apart — verified, broken, and *not known*. The failure mode worth
 *    testing is the quiet one: a page that renders "verified" because the
 *    request has not come back, or came back 500. Every assertion below that
 *    mentions the word checks it is absent as hard as it checks it is present.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient } from "../../lib/api";
import { AuditPage } from "./AuditPage";
import type { AuditEventOut, AuditListResponse, AuditVerifyResponse } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const listAuditEvents = vi.fn();
const verifyAuditChain = vi.fn();
vi.mock("./api", () => ({
  AUDIT_EVENTS_KEY: "audit-events",
  AUDIT_VERIFY_KEY: "audit-verify",
  listAuditEvents: (...args: unknown[]) => listAuditEvents(...args),
  verifyAuditChain: (...args: unknown[]) => verifyAuditChain(...args),
}));

function event(overrides: Partial<AuditEventOut> = {}): AuditEventOut {
  return {
    id: "aaaaaaaa-0000-0000-0000-000000000001",
    ts: "2026-08-16T14:32:18Z",
    actor_type: "user",
    actor_id: "bbbbbbbb-0000-0000-0000-000000000001",
    action: "job.dispatch",
    target_type: "device",
    target_id: "SRV-FS-01",
    metadata: {},
    hash: "0123456789abcdef0123456789abcdef",
    ...overrides,
  };
}

/**
 * A stat tile by its label — and NOT `getByText`, deliberately: "Chain
 * integrity" is both a tile and a tab, so the text query finds two elements and
 * a test written around that would be asserting about whichever came first.
 */
function tileByLabel(label: string): HTMLElement | undefined {
  return [...document.querySelectorAll<HTMLElement>("a.stat-card")].find(
    (card) => card.querySelector(".stat-label")?.textContent === label,
  );
}

function renderPage(options: {
  events?: () => Promise<AuditListResponse>;
  verify?: () => Promise<AuditVerifyResponse>;
  permissions?: string[];
  /** The operator's profile zone. `null` models a profile that sets none. */
  timezone?: string | null;
  /** The URL this page is opened at — the tab now lives in the query string. */
  at?: string;
  /** `GET /devices`, which names device targets. */
  devices?: () => Promise<unknown>;
}) {
  listAuditEvents.mockImplementation(
    options.events ?? (() => Promise.resolve({ items: [], total: 0 })),
  );
  verifyAuditChain.mockImplementation(
    options.verify ?? (() => Promise.resolve({ ok: true, count: 0, broken_at: null })),
  );
  mockUseApi.mockReturnValue({
    me: () =>
      Promise.resolve({
        user_id: "u",
        org_id: "o",
        email: "op@example.com",
        display_name: "Operator",
        permissions: options.permissions ?? ["audit.read"],
        // ASIA/TOKYO BY DEFAULT, and this runner is UTC, so every timestamp
        // assertion below distinguishes "read the profile" from "read the
        // machine". +9 with no DST: one right answer all year.
        timezone: options.timezone === undefined ? "Asia/Tokyo" : options.timezone,
      }),
    listDevices: vi.fn(options.devices ?? (() => Promise.resolve({ items: [], total: 0 }))),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[options.at ?? "/audit"]}>
        <AuditPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

beforeEach(() => {
  mockUseApi.mockReset();
  listAuditEvents.mockReset();
  verifyAuditChain.mockReset();
});

describe("AuditPage", () => {
  it("says it is loading rather than showing an empty log", async () => {
    renderPage({ events: () => new Promise<never>(() => {}) });
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(/No audit events recorded yet/i)).not.toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty log", async () => {
    // A dead audit endpoint and a tenant that has genuinely done nothing must
    // not look the same: one of those is an incident.
    renderPage({ events: () => Promise.reject(new Error("boom")) });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No audit events recorded yet/i)).not.toBeInTheDocument();
  });

  it("says the log is empty when it is genuinely empty", async () => {
    renderPage({ events: () => Promise.resolve({ items: [], total: 0 }) });
    expect(await screen.findByText("No audit events recorded yet.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists events with the design's structure and real counts", async () => {
    renderPage({
      events: () =>
        Promise.resolve({
          items: [event(), event({ id: "2", action: "credential.use", target_id: "vault-7" })],
          total: 991842,
        }),
      verify: () => Promise.resolve({ ok: true, count: 991842, broken_at: null }),
    });
    expect(await screen.findByText("job.dispatch")).toBeInTheDocument();
    expect(screen.getByText("credential.use")).toBeInTheDocument();
    expect(screen.getByText("vault-7")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Audit log" })).toBeInTheDocument();
    // No page-drawn eyebrow on the v3 kit: the shell's crumb is the one
    // answer to "where am I", so the group name is not restated here.
    expect(document.querySelector(".eyebrow")).toBeNull();

    // Wait for the chain walk too, so the integrity card is settled rather
    // than still reading "Verifying…".
    await screen.findByText("Verified");
    const values = [...document.querySelectorAll(".stat-card")].map(
      (card) => card.querySelector(".stat-value")?.textContent,
    );
    // Total is the SERVER's total, not the page length; action types are
    // computed from the rows; the prototype's "991.8k / 100% / 184 / 12" are
    // nowhere.
    expect(values).toEqual(["991842", "Verified", "991842", "2"]);
    expect(values).not.toContain("184");
  });

  // ------------------------------------------------------------------
  // Timestamps (UX remediation cycle 5). An audit row IS a timestamp; the
  // action and the actor are what it happened to.
  // ------------------------------------------------------------------

  it("renders an event's timestamp in the operator's profile zone", async () => {
    // The event is stamped 14:32:18Z and the profile says Asia/Tokyo, so the
    // only correct rendering is 23:32. FALSIFIED by
    // `new Date(ts).toLocaleString()`, which under this runner prints the UTC
    // 14:32 — named below so the failure says which of the two it rendered.
    renderPage({ events: () => Promise.resolve({ items: [event()], total: 1 }) });
    await screen.findByText("job.dispatch");
    expect(await screen.findByText("16 Aug 2026, 23:32")).toBeInTheDocument();
    expect(screen.queryByText(/14:32/)).not.toBeInTheDocument();
  });

  it("falls back to the browser's zone when the profile sets none, never to a wrong one", async () => {
    // THE PAIR IS THE TEST: without this case, "renders 23:32" is also
    // satisfied by a page that hardcodes Tokyo. A profile with no zone is the
    // common case (the field is nullable) and must still render an instant.
    renderPage({
      events: () => Promise.resolve({ items: [event()], total: 1 }),
      timezone: null,
    });
    await screen.findByText("job.dispatch");
    // The runner is UTC, so this is the browser fallback rendering.
    expect(await screen.findByText("16 Aug 2026, 14:32")).toBeInTheDocument();
  });

  it("renders an em dash for an event with no timestamp, never 'Invalid Date'", async () => {
    // A row whose `ts` did not survive the wire is a defect worth seeing as a
    // gap, not as the words "Invalid Date" in an evidence table.
    renderPage({
      events: () =>
        Promise.resolve({ items: [event({ ts: "" as unknown as string })], total: 1 }),
    });
    await screen.findByText("job.dispatch");
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
    // The When cell is the row's THIRD cell on the v3 kit (action, actor,
    // when, hash: the action leads, as the design draws it).
    const cells = [...document.querySelectorAll("tbody td")].map((cell) => cell.textContent);
    expect(cells[2]).toBe("—");
  });

  it("filters the loaded page client-side and distinguishes no-match from empty", async () => {
    renderPage({
      events: () =>
        Promise.resolve({
          items: [event(), event({ id: "2", action: "credential.use", target_id: "vault-7" })],
          total: 2,
        }),
    });
    await screen.findByText("job.dispatch");
    await userEvent.type(screen.getByLabelText("Search the loaded page by action, actor or target"), "credential");
    expect(screen.getByText("credential.use")).toBeInTheDocument();
    expect(screen.queryByText("job.dispatch")).not.toBeInTheDocument();

    await userEvent.clear(screen.getByLabelText("Search the loaded page by action, actor or target"));
    await userEvent.type(screen.getByLabelText("Search the loaded page by action, actor or target"), "zzzz");
    expect(screen.getByText("No loaded audit event matches that filter.")).toBeInTheDocument();
  });

  it("sends the action filter to the SERVER, because that is where it is applied", async () => {
    renderPage({ events: () => Promise.resolve({ items: [event()], total: 1 }) });
    await screen.findByText("job.dispatch");
    await userEvent.type(screen.getByLabelText("Filter by exact action, server-side"), "policy.update");
    await waitFor(() => {
      // `(api, params)` since these calls were folded onto the shared
      // ApiClient (2026-08-16) and `modules/audit/kernelHttp.ts` was deleted.
      // What this test checks is unchanged: the filter goes to the SERVER.
      expect(listAuditEvents).toHaveBeenCalledWith(
        expect.anything(),
        expect.objectContaining({ action: "policy.update" }),
      );
    });
  });

  it("renders a BROKEN chain unmistakably, on every tab", async () => {
    renderPage({
      events: () => Promise.resolve({ items: [event()], total: 1 }),
      verify: () =>
        Promise.resolve({ ok: false, count: 41, broken_at: "cccccccc-0000-0000-0000-000000000009" }),
    });
    // The banner is above the tabs, so it is present while looking at events.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/FAILED/);
    expect(alert).toHaveTextContent("cccccccc-0000-0000-0000-000000000009");
    expect(alert.className).toContain("red");
    // And the stat card does not soften it.
    expect(screen.getByText("BROKEN")).toBeInTheDocument();
    expect(screen.queryByText("Verified")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /Chain integrity/ }));
    expect(await screen.findByText(/The chain is BROKEN/)).toBeInTheDocument();
    // A count from a failed walk is never presented as events verified.
    const values = [...document.querySelectorAll(".stat-card")].map(
      (card) => card.querySelector(".stat-value")?.textContent,
    );
    expect(values[2]).toBe("—");
  });

  it("never says verified for a verification that did not come back", async () => {
    // The quiet failure this page exists to prevent.
    renderPage({
      events: () => Promise.resolve({ items: [event()], total: 1 }),
      verify: () => Promise.reject(new Error("chain walk 500")),
    });
    await screen.findByText("job.dispatch");
    await screen.findByText("Unverified");
    expect(screen.queryByText("Verified")).not.toBeInTheDocument();
    expect(screen.queryByText("BROKEN")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /Chain integrity/ }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/did not run/i);
    expect(alert).toHaveTextContent(/not.*a statement that the chain is intact/i);
  });

  it("says WHY the chain walk did not run, so a 403 and a 503 are not the same message", async () => {
    // The caught failure used to reach nobody: the notice said "verification
    // did not run" and stopped, so an operator could not tell "this account
    // may read events but not walk the chain" from "the walker is down, try
    // again". Both are `unknown`; only one is worth escalating.
    renderPage({
      events: () => Promise.resolve({ items: [event()], total: 1 }),
      verify: () => Promise.reject(new ApiError(503, "")),
    });
    await screen.findByText("job.dispatch");
    await userEvent.click(screen.getByRole("tab", { name: /Chain integrity/ }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("That service is unavailable right now.");
    // And it still claims nothing about the chain.
    expect(screen.queryByText("Verified")).not.toBeInTheDocument();
  });

  it("says verified only when the chain walk returned ok", async () => {
    // Non-vacuity for the three assertions above: the affirmative path works.
    renderPage({
      events: () => Promise.resolve({ items: [event()], total: 1 }),
      verify: () => Promise.resolve({ ok: true, count: 7, broken_at: null }),
    });
    await screen.findByText("job.dispatch");
    await userEvent.click(screen.getByRole("tab", { name: /Chain integrity/ }));
    expect(await screen.findByText(/Chain verified/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  // ------------------------------------------------------------ cycle 12, B66
  //
  // "Hashes render as `d2e71d7d057bbdfb…` — first 16 hex characters plus an
  // ellipsis. There is no expand, no copy button, and the cell is not a link...
  // A truncated hash cannot be independently verified or quoted in an audit
  // response, so the column is decorative."
  it("keeps the FULL chain hash reachable, not just its first sixteen characters", async () => {
    const full = "0123456789abcdef0123456789abcdef";
    renderPage({ events: () => Promise.resolve({ items: [event({ hash: full })], total: 1 }) });

    const shown = await screen.findByText("0123456789abcdef…");
    // Hover/expand half: the cell carries the whole value.
    expect(shown).toHaveAttribute("title", full);
    // Quotable half: a control that puts it on the clipboard, named by the
    // hash so two hundred "Copy" buttons are distinguishable.
    expect(
      screen.getByRole("button", { name: `Copy full chain hash ${full}` }),
    ).toBeInTheDocument();
  });

  it("copies the whole hash, and says which one it copied", async () => {
    const full = "0123456789abcdef0123456789abcdef";
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderPage({ events: () => Promise.resolve({ items: [event({ hash: full })], total: 1 }) });

    await userEvent.click(
      await screen.findByRole("button", { name: `Copy full chain hash ${full}` }),
    );
    expect(writeText).toHaveBeenCalledWith(full);
    // Truncated in the cell, whole on the clipboard, and announced.
    expect(writeText).not.toHaveBeenCalledWith(expect.stringContaining("…"));
    expect(await screen.findByRole("status")).toHaveTextContent(full);
  });

  // ------------------------------------------------------------ cycle 12, B64
  //
  // The NAMES cannot be resolved — no route serves them (see the comment in
  // AuditPage.tsx's actor cell). What the page can stop doing is printing the
  // wire's identifier for the TYPE.
  it("reads the actor and target types as words, not as wire identifiers", async () => {
    renderPage({
      events: () =>
        Promise.resolve({
          items: [event({ actor_type: "service_account", target_type: "health_profile" })],
          total: 1,
        }),
    });
    expect(await screen.findByText("Service account")).toBeInTheDocument();
    expect(screen.getByText("Health profile")).toBeInTheDocument();
    expect(screen.queryByText("health_profile")).not.toBeInTheDocument();
  });

  it("does not query the audit log for an operator without audit.read", async () => {
    renderPage({ permissions: [] });
    expect(await screen.findByText(/does not hold audit\.read/)).toBeInTheDocument();
    expect(listAuditEvents).not.toHaveBeenCalled();
    expect(verifyAuditChain).not.toHaveBeenCalled();
  });

  it("shows no panel the audit API cannot fill", async () => {
    // The design has Exports and Restore-boundary tabs and "Privileged today" /
    // "Export packs" metrics, none of which has an endpoint. Omitted, not faked.
    renderPage({ events: () => Promise.resolve({ items: [event()], total: 1 }) });
    await screen.findByText("job.dispatch");
    expect(screen.queryByRole("tab", { name: /Exports/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /Restore/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Privileged today/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Export packs/)).not.toBeInTheDocument();
    // Non-vacuity: the tabs that DO have data are present.
    expect(screen.getByRole("tab", { name: /Event search/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Chain integrity/ })).toBeInTheDocument();
  });

  /**
   * THE TILES ARE LINKS, AND EACH ONE OPENS THE VIEW IT COUNTED.
   *
   * "All tiles and all numbers or kpis need to be clickable and direct the user
   * to the specific menus, i dont want dead tiles everywhere" — the operator,
   * 2026-08-21. Two of these four numbers describe the chain walk, which lived
   * on a tab held in `useState` and therefore had no address at all: the tile
   * could only point at the page it already sat on and land the reader back on
   * the event table. The tab is in the query string now, and these tests hold
   * both halves — the href, and that the href opens the view it claims.
   */
  it("gives every tile a real href — none is a dead tile", async () => {
    renderPage({
      events: () => Promise.resolve({ items: [event()], total: 7 }),
      verify: () => Promise.resolve({ ok: true, count: 7, broken_at: null }),
    });
    await screen.findByText("Verified");

    const hrefs = Object.fromEntries(
      [...document.querySelectorAll("a.stat-card")].map((card) => [
        card.querySelector(".stat-label")?.textContent ?? "",
        card.getAttribute("href"),
      ]),
    );
    expect(hrefs).toEqual({
      "Audit events": "/audit",
      "Chain integrity": "/audit?tab=integrity",
      "Events in verified chain": "/audit?tab=integrity",
      "Action types in view": "/audit",
    });
  });

  it("opens on the integrity view when the integrity tile's URL is followed", async () => {
    // The href above is only a promise; this is the view behind it. Opened
    // straight at that URL — as a pasted link in an incident channel would be.
    renderPage({
      at: "/audit?tab=integrity",
      events: () => Promise.resolve({ items: [event()], total: 1 }),
      verify: () =>
        Promise.resolve({ ok: false, count: 3, broken_at: "cccccccc-0000-0000-0000-000000000009" }),
    });

    expect(await screen.findByText(/The chain is BROKEN/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Chain integrity/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  // ----------------------------------------------------------------------
  // WHAT EACH FIGURE IS MADE OF, and above all what tells the first tile from
  // the third. On a small tenant "Audit events" and "Events in verified
  // chain" are the same number, and the port drew them as two identical
  // figures with nothing under either: one is the page the server sent, the
  // other is what its own walk counted, and the supporting line is where that
  // difference is stated.
  //
  // BREAKS AGAINST: the foots removed, and against the walk's line being
  // shown for a chain that did not verify.
  // ----------------------------------------------------------------------
  it("says where each figure comes from, so two equal numbers are not one fact twice", async () => {
    renderPage({
      events: () =>
        Promise.resolve({
          items: [event(), event({ id: "e2", action: "job.dispatch" }), event({ id: "e3", action: "credential.checkout" })],
          total: 3,
        }),
      verify: () => Promise.resolve({ ok: true, count: 3, broken_at: null }),
    });
    await screen.findByText("Verified");

    expect(tileByLabel("Audit events")).toHaveTextContent("The whole record held for this tenant");
    expect(tileByLabel("Events in verified chain")).toHaveTextContent(
      "Counted by the server's walk, not by this page",
    );
    expect(tileByLabel("Chain integrity")).toHaveTextContent("Recomputed on load, and on demand");
    expect(tileByLabel("Action types in view")).toHaveTextContent("Mostly “Job sent”, 2 of 3");
  });

  it("says how much of the record is loaded when the page is not all of it", async () => {
    renderPage({
      events: () => Promise.resolve({ items: [event()], total: 4000 }),
      verify: () => Promise.resolve({ ok: true, count: 4000, broken_at: null }),
    });
    await screen.findByText("Verified");
    expect(tileByLabel("Audit events")).toHaveTextContent("1 loaded, newest first");
  });

  it("claims nothing about a chain that did not verify", async () => {
    renderPage({
      events: () => Promise.resolve({ items: [event()], total: 1 }),
      verify: () => Promise.reject(new Error("chain walk 500")),
    });
    await screen.findByText("Unverified");
    expect(tileByLabel("Chain integrity")).toHaveTextContent("The chain has not been walked");
    expect(tileByLabel("Events in verified chain")).not.toHaveTextContent(
      "Counted by the server's walk",
    );
  });

  it("wears the colour of what it is saying, and shouts only when the chain is broken", async () => {
    renderPage({
      events: () => Promise.resolve({ items: [event()], total: 1 }),
      verify: () =>
        Promise.resolve({ ok: false, count: 3, broken_at: "cccccccc-0000-0000-0000-000000000009" }),
    });
    await screen.findByText("BROKEN");

    const integrity = tileByLabel("Chain integrity");
    expect(integrity?.className).toContain("tone-red");
    expect(integrity?.className).toContain("urgent");
    // Exactly one tile is loud. A row where "Audit events" shouted too would
    // say nothing about which number needs a person.
    expect(document.querySelectorAll("a.stat-card.urgent")).toHaveLength(1);
  });

  it("is green when the chain verified and gold when nobody knows", async () => {
    const { unmount } = renderPage({
      events: () => Promise.resolve({ items: [event()], total: 1 }),
      verify: () => Promise.resolve({ ok: true, count: 1, broken_at: null }),
    });
    await screen.findByText("Verified");
    expect(tileByLabel("Chain integrity")?.className).toContain("tone-green");
    // Nothing on a healthy page is loud.
    expect(document.querySelectorAll("a.stat-card.urgent")).toHaveLength(0);
    unmount();

    renderPage({
      events: () => Promise.resolve({ items: [event()], total: 1 }),
      verify: () => Promise.reject(new Error("chain walk 500")),
    });
    await screen.findByText("Unverified");
    expect(tileByLabel("Chain integrity")?.className).toContain("tone-gold");
    expect(document.querySelectorAll("a.stat-card.urgent")).toHaveLength(0);
  });
});

// ------------------------------------------------------------------
// WHO DID WHAT TO WHICH MACHINE. The route names people itself
// (`actor_display_name`, `target_display_name`); device targets are named
// from the device list when this account may read it. Every name falls back
// to the id, which is what the log actually holds.
// ------------------------------------------------------------------
describe("AuditPage names", () => {
  it("names the actor, keeping the id as secondary text", async () => {
    renderPage({
      events: () =>
        Promise.resolve({
          items: [event({ actor_display_name: "Helga Ramos" })],
          total: 1,
        }),
    });
    const name = await screen.findByText("Helga Ramos");
    expect(name.closest("td")).toHaveTextContent("bbbbbbbb-0000-0000-0000-000000000001");
  });

  it("falls back to the id when the directory has no name", async () => {
    renderPage({
      events: () => Promise.resolve({ items: [event({ actor_display_name: null })], total: 1 }),
    });
    expect(await screen.findByText("bbbbbbbb-0000-0000-0000-000000000001")).toBeInTheDocument();
  });

  it("names a device target by hostname when the account can read devices", async () => {
    renderPage({
      permissions: ["audit.read", "devices.read"],
      events: () =>
        Promise.resolve({
          items: [event({ target_type: "device", target_id: "dev-uuid-1" })],
          total: 1,
        }),
      devices: () =>
        Promise.resolve({ items: [{ id: "dev-uuid-1", hostname: "por-ws-008" }], total: 1 }),
    });
    expect(await screen.findByText("por-ws-008")).toBeInTheDocument();
  });

  it("does not ask for devices without devices.read, and shows the id", async () => {
    renderPage({
      events: () =>
        Promise.resolve({
          items: [event({ target_type: "device", target_id: "dev-uuid-1" })],
          total: 1,
        }),
    });
    expect(await screen.findByText("dev-uuid-1")).toBeInTheDocument();
    const api = mockUseApi.mock.results[0]?.value as { listDevices: ReturnType<typeof vi.fn> };
    expect(api.listDevices).not.toHaveBeenCalled();
  });

  it("names a person target from the route's own join", async () => {
    renderPage({
      events: () =>
        Promise.resolve({
          items: [
            event({
              action: "user.disabled",
              target_type: "user",
              target_id: "cccccccc-0000-0000-0000-000000000001",
              target_display_name: "Ines Caetano",
            }),
          ],
          total: 1,
        }),
    });
    expect(await screen.findByText("Ines Caetano")).toBeInTheDocument();
  });

  it("leads each row with the action in words and keeps the code beside it", async () => {
    renderPage({
      events: () =>
        Promise.resolve({ items: [event({ action: "remote.session.start" })], total: 1 }),
    });
    const code = await screen.findByText("remote.session.start");
    expect(code).toHaveClass("mono");
    expect(code.closest("td")?.querySelector(".cell-title")).toHaveTextContent(
      "Remote session started",
    );
  });

  it("finds a row by the actor's name", async () => {
    renderPage({
      events: () =>
        Promise.resolve({
          items: [
            event({ actor_display_name: "Helga Ramos" }),
            event({ id: "2", actor_display_name: "Ines Caetano", action: "credential.use" }),
          ],
          total: 2,
        }),
    });
    await screen.findByText("Helga Ramos");
    await userEvent.type(
      screen.getByLabelText("Search the loaded page by action, actor or target"),
      "ines",
    );
    expect(screen.queryByText("Helga Ramos")).not.toBeInTheDocument();
    expect(screen.getByText("Ines Caetano")).toBeInTheDocument();
  });
});
