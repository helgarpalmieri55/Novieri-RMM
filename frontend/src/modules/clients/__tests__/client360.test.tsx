import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry, type FixtureMap } from "../../../fixtures/client";
import type { Me } from "../../../lib/api";
import { recordingNarrowing } from "../../../test/narrowing";
import { renderPage } from "../../../test/renderPage";
import { ClientDetailPage } from "../ClientDetailPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

/**
 * What the 360 may read on the console's own API, un-narrowed: `/me` (always
 * allowed by the double) and the client's own record (a global route). Every
 * tab's read must go through `forClient(ANDES)`, or the double rejects it.
 */
const CONSOLE = ["getClientRecord"];

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** The fixture session, holding only `codes` in Andes Logistics. */
function holding(codes: string[], extra: FixtureMap = {}) {
  const registry = fixtureRegistry();
  const me = registry.me as () => Me;
  return fixtureClient({
    ...registry,
    me: () => {
      const base = me();
      return {
        ...base,
        clients: (base.clients ?? []).map((item) => (item.id === ANDES ? { ...item, permissions: codes } : item)),
      };
    },
    ...extra,
  });
}

function page(query = "") {
  return renderPage(`/clients/${ANDES}${query}`, <ClientDetailPage />, "/clients/:clientId");
}

beforeEach(() => mockUseApi.mockReturnValue(fixtureClient()));

describe("the client 360 (redesign design §1.3)", () => {
  it("names the client, its status and its kind, and leads back to the list", async () => {
    page();
    expect(await screen.findByRole("heading", { level: 1, name: "Andes Logistics" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All clients" })).toHaveAttribute("href", "/clients");
    const head = screen.getByRole("heading", { level: 1 }).closest("section") as HTMLElement;
    expect(within(head).getByText("Active")).toBeInTheDocument();
    expect(within(head).getByText("Customer")).toBeInTheDocument();
    expect(within(head).getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("names the client in the browser tab", async () => {
    renderPage(`/clients/${ANDES}`, <ClientDetailPage />, "/clients/:clientId", { page: "shell.nav.item.clients" });
    await screen.findByRole("heading", { level: 1, name: "Andes Logistics" });
    // The name reaches the tab through two effects (`useRecordTitle`, then the
    // shell's), which run after the commit that drew the heading: the change
    // record's title test waits for it the same way.
    await waitFor(() => expect(document.title).toBe("Andes Logistics · Clients · Novieri RMM"));
  });

  it("has the eight tabs of the design, in one row, for someone who may read all of it", async () => {
    page();
    const tabs = await screen.findByRole("tablist", { name: "Client" });
    expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Summary",
      "Sites",
      "Contacts",
      // English keeps the rail's word (coordinator, Task 5 fix round 1); Spanish says «Equipos».
      "Devices",
      "Tickets",
      "Alerts",
      "Assets",
      "Modules",
    ]);
    expect(tabs).toHaveClass("scroll");
  });

  it("draws a tab, and its tile, only with its code in this client (cosmetic, invariant 6)", async () => {
    const listTickets = vi.fn();
    mockUseApi.mockReturnValue(holding(["clients.read"], { listTickets }));
    page("?tab=tickets");
    const tabs = await screen.findByRole("tablist", { name: "Client" });
    expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Summary", "Sites", "Contacts", "Modules"]);
    // A tab it may not read falls back to the default, and nothing is read for it.
    expect(screen.getByRole("tab", { name: "Summary" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("link", { name: /Open tickets/ })).toBeNull();
    expect(listTickets).not.toHaveBeenCalled();
  });

  it("puts the tab in the URL, so a tab reloads and links", async () => {
    const { router } = page("?tab=alerts");
    expect(await screen.findByRole("tab", { name: "Alerts" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "Devices" }));
    expect(router.state.location.search).toBe("?tab=devices");
  });

  it("lists the client's devices and leads to the fleet list narrowed to it", async () => {
    page("?tab=devices");
    const link = await screen.findByRole("link", { name: /See every device of this client/ });
    expect(link).toHaveAttribute("href", `/devices?client=${ANDES}`);
    // The fixtures give Andes the Porto office's machines, so the tab is not
    // empty under its non-zero tile (Task 5 review, fix round 1).
    expect(await screen.findByRole("link", { name: "por-ws-008" })).toHaveAttribute("href", "/devices/dev-4");
    expect(link).toHaveTextContent("(3)");
  });

  it("reads the devices tab only through the client's narrowed API (decision D6)", async () => {
    const narrowing = recordingNarrowing(fixtureClient(), { console: CONSOLE });
    mockUseApi.mockReturnValue(narrowing.api);
    page("?tab=devices");
    expect(await screen.findByRole("link", { name: /See every device of this client \(\d+\)/ })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listDevices).toHaveBeenCalledWith({ clientId: ANDES, limit: 25 });
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("counts nothing before the devices have answered, rather than an invented 0", async () => {
    mockUseApi.mockReturnValue(holding(["devices.read"], { listDevices: () => new Promise(() => {}) }));
    page("?tab=devices");
    expect(await screen.findByRole("link", { name: "See every device of this client" })).toBeInTheDocument();
  });

  it("reads the tickets tab only through the client's narrowed API, its open tickets, and leads to the ticket list narrowed to it", async () => {
    const narrowing = recordingNarrowing(
      holding(["ticket.read", "ticket.write"], { listTickets: () => ({ items: [], total: 0 }) }),
      { console: CONSOLE },
    );
    mockUseApi.mockReturnValue(narrowing.api);
    page("?tab=tickets");
    expect(await screen.findByText("This client has no open tickets.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See this client's tickets" })).toHaveAttribute("href", `/service-desk?client=${ANDES}`);
    expect(screen.getByRole("link", { name: "New ticket" })).toHaveAttribute("href", "/service-desk?new=ticket");
    expect(narrowing.client(ANDES).listTickets).toHaveBeenCalledWith({ client_id: ANDES, scope: "open", limit: 25 });
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("counts what is open for the client in the summary tiles, each leading to its tab", async () => {
    page();
    expect(await screen.findByRole("link", { name: /Open tickets/ })).toHaveAttribute("href", `/clients/${ANDES}?tab=tickets`);
    expect(screen.getByRole("link", { name: /Open alerts/ })).toHaveAttribute("href", `/clients/${ANDES}?tab=alerts`);
  });

  it("reads the alerts tab only through the client's narrowed API, the active ones, and leads to the alert list narrowed to it (D19)", async () => {
    const narrowing = recordingNarrowing(fixtureClient(), { console: CONSOLE });
    mockUseApi.mockReturnValue(narrowing.api);
    page("?tab=alerts");
    expect(await screen.findByRole("heading", { level: 2, name: "Active alerts" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See this client's alerts" })).toHaveAttribute("href", `/health?client=${ANDES}`);
    expect((await screen.findAllByText("CPU busy")).length).toBeGreaterThan(0);
    expect(narrowing.client(ANDES).listHealthAlerts).toHaveBeenCalledWith({ state: "active" });
    expect(narrowing.client(ANDES).getHealthAlertSummary).toHaveBeenCalled();
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("names each alert's check in words, as the alert console does, not by its wire key (critique P1)", async () => {
    page("?tab=alerts");
    expect((await screen.findAllByText("CPU busy")).length).toBeGreaterThan(0);
    expect(screen.queryByText("cpu.busy_pct")).toBeNull();
  });

  it("shows the assets as three counts, each leading to its list with the Client chip set (D14)", async () => {
    page("?tab=assets");
    expect(await screen.findByRole("link", { name: /Licenses/ })).toHaveAttribute("href", `/licenses?client=${ANDES}`);
    expect(screen.getByRole("link", { name: /Subscriptions/ })).toHaveAttribute("href", `/subscriptions?client=${ANDES}`);
    expect(screen.getByRole("link", { name: /Networks/ })).toHaveAttribute("href", `/ipam?client=${ANDES}`);
  });

  it("counts the assets only through the client's narrowed API (decision D6)", async () => {
    const narrowing = recordingNarrowing(fixtureClient(), { console: CONSOLE });
    mockUseApi.mockReturnValue(narrowing.api);
    page("?tab=assets");
    // Measured, not `n/a`: each count arrived through the narrowed double.
    await waitFor(() => expect(screen.getByRole("link", { name: /Licenses/ })).not.toHaveTextContent("n/a"));
    await waitFor(() => expect(screen.getByRole("link", { name: /Networks/ })).not.toHaveTextContent("n/a"));
    expect(narrowing.client(ANDES).listLicenses).toHaveBeenCalled();
    expect(narrowing.client(ANDES).listSubscriptions).toHaveBeenCalled();
    expect(narrowing.client(ANDES).listIpamSubnets).toHaveBeenCalled();
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("waits for /me before choosing the tab, so a cold ?tab=alerts never draws Summary first", async () => {
    let answer: (me: Me) => void = () => {};
    const registry = fixtureRegistry();
    const me = registry.me as () => Me;
    const listHealthAssignments = vi.fn(registry.listHealthAssignments);
    mockUseApi.mockReturnValue(
      fixtureClient({ ...registry, me: () => new Promise<Me>((resolve) => (answer = resolve)), listHealthAssignments }),
    );
    page("?tab=alerts");
    await screen.findByRole("heading", { level: 1, name: "Andes Logistics" });
    expect(screen.queryByRole("tablist", { name: "Client" })).toBeNull();
    answer(me());
    expect(await screen.findByRole("tab", { name: "Alerts" })).toHaveAttribute("aria-selected", "true");
    // Summary was never drawn: its health read never ran.
    expect(listHealthAssignments).not.toHaveBeenCalled();
  });
});
