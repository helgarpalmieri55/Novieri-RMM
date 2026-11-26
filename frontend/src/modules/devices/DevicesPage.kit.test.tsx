/**
 * Devices on the list kit (redesign plan, Task 6, page 1.1; Receta L).
 *
 * Mounted under a data router with the fixture API and the real client scope
 * (`/me` from `src/fixtures/core.ts`: three clients, every code), which is what
 * the Client chip needs to be drawn at all; `DevicesPage.test.tsx` mocks the
 * scope and keeps one client, so the chip never shows there.
 */
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../fixtures/client";
import { ApiError, type Me } from "../../lib/api";
import { recordingNarrowing } from "../../test/narrowing";
import { renderPage } from "../../test/renderPage";
import { DEVICES_PAGE_SIZE, DevicesPage } from "./DevicesPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
// The live-events socket is not what this file is about.
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

beforeEach(() => mockUseApi.mockReturnValue(fixtureClient()));

/** The fixture session holding only `codes`, everywhere. */
function holding(codes: string[]) {
  const registry = fixtureRegistry();
  const me = registry.me as () => Me;
  return fixtureClient({
    ...registry,
    me: () => {
      const base = me();
      return {
        ...base,
        permissions: codes,
        permissions_everywhere: codes,
        clients: (base.clients ?? []).map((client) => ({ ...client, permissions: codes })),
      };
    },
  });
}

describe("Devices on the list kit (Receta L)", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = renderPage("/devices?state=offline", <DevicesPage />);
    expect(await screen.findByRole("button", { name: /^Offline/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: /^Online/ }));
    expect(router.state.location.search).toContain("state=online");
  });

  it("says why the list is empty", async () => {
    renderPage("/devices?q=nothing-is-called-this", <DevicesPage />);
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // `listClientSites` is a global route (`/clients/{id}/sites`, the client in
    // the path): the Site column's names, read for each row's client and never
    // the list itself.
    const narrowing = recordingNarrowing(fixtureClient(), { console: ["listClientSites"] });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/devices?client=${ANDES}`, <DevicesPage />);
    expect(await screen.findByRole("link", { name: "por-ws-008" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listDevices).toHaveBeenCalledWith({ state: "all", limit: DEVICES_PAGE_SIZE, offset: 0 });
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("puts Add a computer at the head of the page", async () => {
    renderPage("/devices", <DevicesPage />);
    const head = (await screen.findByRole("heading", { level: 1, name: "Devices" })).closest("section") as HTMLElement;
    const add = await within(head).findByRole("link", { name: "Add a computer" });
    expect(add).toHaveAttribute("href", "/devices/onboard");
    expect(add).toHaveClass("btn", "primary");
    // The page's one primary action (Receta L, 1).
    expect(head.querySelectorAll(".btn.primary")).toHaveLength(1);
  });

  it("offers Add a computer only with devices.read, as the New menu does (design §1.2)", async () => {
    mockUseApi.mockReturnValue(holding(["jobs.read"]));
    renderPage("/devices", <DevicesPage />);
    await screen.findByRole("heading", { level: 1, name: "Devices" });
    await screen.findByText("lis-ws-041");
    expect(screen.queryByRole("link", { name: "Add a computer" })).toBeNull();
  });

  it("says the fleet is empty with the way to its first computer", async () => {
    const registry = fixtureRegistry();
    mockUseApi.mockReturnValue(
      fixtureClient({
        ...registry,
        listDevices: () => ({
          items: [],
          total: 0,
          state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
          fleet: { total: 0, managed: 0, online: 0, cert_attention: 0 },
        }),
      }),
    );
    renderPage("/devices", <DevicesPage />);
    expect(await screen.findByText("No devices yet. Install the agent on the first one.")).toBeInTheDocument();
    // The page head's and the empty list's.
    expect(screen.getAllByRole("link", { name: "Add a computer" })).toHaveLength(2);
  });

  it("draws no bulk bar without a selection, and one with it", async () => {
    renderPage("/devices", <DevicesPage />);
    const box = await screen.findByRole("checkbox", { name: "Select lis-ws-055 for push install" });
    expect(screen.queryByRole("region", { name: "Bulk actions" })).toBeNull();
    await userEvent.click(box);
    const bar = screen.getByRole("region", { name: "Bulk actions" });
    expect(within(bar).getByRole("button", { name: "Push install the agent… (1)" })).toBeInTheDocument();
    await userEvent.click(within(bar).getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByRole("region", { name: "Bulk actions" })).toBeNull();
  });

  it("offers the site filter once the chip has chosen a client", async () => {
    const { router } = renderPage(`/devices`, <DevicesPage />);
    await screen.findByText("lis-ws-041");
    expect(screen.queryByRole("combobox", { name: "Filter by site" })).toBeNull();
    await act(() => router.navigate(`/devices?client=${ANDES}`));
    expect(await screen.findByRole("combobox", { name: "Filter by site" })).toBeInTheDocument();
  });
});

describe("push install from the URL", () => {
  it("opens Push install the agent from ?new=push, the way Push runs' empty list leads here", async () => {
    renderPage("/devices?new=push", <DevicesPage />);
    expect(await screen.findByRole("heading", { name: "Push install: choose the machines" })).toBeInTheDocument();
  });
});

describe("the tiles under the Client chip (fix round 1)", () => {
  it("keep the chip's client in every tile's link, as they count that client's machines", async () => {
    renderPage(`/devices?client=${ANDES}`, <DevicesPage />);
    await screen.findByRole("link", { name: "por-ws-008" });
    const tiles = [...document.querySelectorAll<HTMLAnchorElement>("a.stat-card")];
    expect(tiles).toHaveLength(4);
    for (const tile of tiles) expect(new URL(tile.href).searchParams.get("client")).toBe(ANDES);
  });

  it("link without a client when no chip is set", async () => {
    renderPage("/devices", <DevicesPage />);
    await screen.findByText("lis-ws-041");
    for (const tile of document.querySelectorAll<HTMLAnchorElement>("a.stat-card")) {
      expect(new URL(tile.href).searchParams.has("client")).toBe(false);
    }
  });
});

describe("before /me answers (coordinator ruling 10)", () => {
  it("draws loading, not an empty list, while /me has not said whose the chip's client is", async () => {
    // With the chip set and `/me` pending, the narrowed read is disabled, and
    // a disabled query is not "loading" to TanStack v5: the page must not say
    // "no devices yet" or "nothing matches" about a list nobody has read.
    const registry = fixtureRegistry();
    const listDevices = vi.fn(registry.listDevices);
    mockUseApi.mockReturnValue(fixtureClient({ ...registry, listDevices, me: () => new Promise<never>(() => {}) }));
    renderPage(`/devices?client=${ANDES}`, <DevicesPage />);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
    expect(screen.queryByText("No devices yet. Install the agent on the first one.")).toBeNull();
    expect(listDevices).not.toHaveBeenCalled();
  });

  it("draws no tile number or claim when the read failed, and the list says why (coordinator ruling 10a)", async () => {
    // No data is no data, pending or failed: "0 managed" under a refusal would
    // be the same invented count.
    mockUseApi.mockReturnValue(
      fixtureClient({ ...fixtureRegistry(), listDevices: () => Promise.reject(new ApiError(503, "fleet unavailable")) }),
    );
    renderPage("/devices", <DevicesPage />);
    expect(await screen.findByText(/fleet unavailable/)).toBeInTheDocument();
    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles).toHaveLength(4);
    for (const tile of tiles) {
      // The kit's one absent mark, never a dash (corrected ruling 10a; DESIGN.md's Absent Zero Rule).
      expect(tile.querySelector(".stat-value")?.textContent).toBe("n/a");
      expect(tile.querySelector(".stat-zero, .stat-foot")).toBeNull();
    }
  });

  it("draws no tile number or claim before the read answers (coordinator ruling 10a)", async () => {
    // A tile reading "0" with "No machine is managed yet" under it, while the
    // count has not been asked for, is the same lie as the empty list.
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), me: () => new Promise<never>(() => {}) }));
    renderPage(`/devices?client=${ANDES}`, <DevicesPage />);
    await screen.findByRole("heading", { level: 1 });
    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles).toHaveLength(4);
    for (const tile of tiles) {
      // The kit's one absent mark, never a dash (corrected ruling 10a; DESIGN.md's Absent Zero Rule).
      expect(tile.querySelector(".stat-value")?.textContent).toBe("n/a");
      expect(tile.querySelector(".stat-zero, .stat-foot")).toBeNull();
    }
  });
});
