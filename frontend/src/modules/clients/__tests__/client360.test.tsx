import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry, type FixtureMap } from "../../../fixtures/client";
import type { Me } from "../../../lib/api";
import { recordingNarrowing } from "../../../test/narrowing";
import { renderPage } from "../../../test/renderPage";
import { ClientDetailPage } from "../ClientDetailPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

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
      "Computers",
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
    await userEvent.click(screen.getByRole("tab", { name: "Computers" }));
    expect(router.state.location.search).toBe("?tab=devices");
  });

  it("lists the client's computers and leads to the fleet list narrowed to it", async () => {
    page("?tab=devices");
    const link = await screen.findByRole("link", { name: /See every computer of this client/ });
    expect(link).toHaveAttribute("href", `/devices?client=${ANDES}`);
  });

  it("reads the tickets tab narrowed to the client, its open tickets, and leads to the ticket list narrowed to it", async () => {
    const listTickets = vi.fn().mockResolvedValue({ items: [], total: 0 });
    const { api, narrowedTo } = recordingNarrowing(holding(["ticket.read", "ticket.write"], { listTickets }));
    mockUseApi.mockReturnValue(api);
    page("?tab=tickets");
    expect(await screen.findByText("This client has no open tickets.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See this client's tickets" })).toHaveAttribute("href", `/service-desk?client=${ANDES}`);
    expect(screen.getByRole("link", { name: "New ticket" })).toHaveAttribute("href", "/service-desk?new=ticket");
    expect(listTickets).toHaveBeenCalledWith({ client_id: ANDES, scope: "open", limit: 25 });
    expect(narrowedTo).toContain(ANDES);
  });

  it("counts what is open for the client in the summary tiles, each leading to its tab", async () => {
    page();
    expect(await screen.findByRole("link", { name: /Open tickets/ })).toHaveAttribute("href", `/clients/${ANDES}?tab=tickets`);
    expect(screen.getByRole("link", { name: /Open alerts/ })).toHaveAttribute("href", `/clients/${ANDES}?tab=alerts`);
  });

  it("reads the alerts tab narrowed to the client, the active ones, and leads to the alert list narrowed to it (D19)", async () => {
    const { api, narrowedTo } = recordingNarrowing(fixtureClient());
    mockUseApi.mockReturnValue(api);
    page("?tab=alerts");
    expect(await screen.findByRole("heading", { level: 2, name: "Active alerts" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See this client's alerts" })).toHaveAttribute("href", `/health?client=${ANDES}`);
    expect(narrowedTo).toContain(ANDES);
  });

  it("shows the assets as three counts, each leading to its list with the Client chip set (D14)", async () => {
    page("?tab=assets");
    expect(await screen.findByRole("link", { name: /Licenses/ })).toHaveAttribute("href", `/licenses?client=${ANDES}`);
    expect(screen.getByRole("link", { name: /Subscriptions/ })).toHaveAttribute("href", `/subscriptions?client=${ANDES}`);
    expect(screen.getByRole("link", { name: /Networks/ })).toHaveAttribute("href", `/ipam?client=${ANDES}`);
  });
});
