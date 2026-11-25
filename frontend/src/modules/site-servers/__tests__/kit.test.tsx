/**
 * Site servers on the kit (redesign plan, Task 9, 4.9 and 4.10): the list's
 * state chips in the URL, its empty states, Promote device from `?new=`, the
 * Client chip narrowing the list's READ (decision D6), and the record's head
 * and tab title.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry, type FixtureMap } from "../../../fixtures/client";
import type { ApiClient, Me } from "../../../lib/api";
import { recordingNarrowing } from "../../../test/narrowing";
import { renderPage } from "../../../test/renderPage";
import { WAKE_FORM_ID } from "../SiteServerDetail";
import { SiteServerDetailPage } from "../SiteServerDetailPage";
import { SiteServersPage } from "../SiteServersPage";
import type { SiteServerOut } from "../types";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** The fixture session, with `codes` added. */
function holding(codes: readonly string[]): () => Me {
  return () => {
    const me = (fixtureRegistry().me as () => Me)();
    return { ...me, permissions: [...new Set([...me.permissions, ...codes])] };
  };
}

function api(over: FixtureMap = {}): ApiClient {
  return fixtureClient({ ...fixtureRegistry(), me: holding(["site.read", "site.manage", "site.wake"]), ...over });
}

beforeEach(() => mockUseApi.mockReturnValue(api()));

/**
 * Every tile on the page draws the kit's absent mark and no zero sentence:
 * no number and no claim before the read it counts has answered
 * (clarification 10a; `StatCard pending`).
 */
async function expectTilesAbsent(count: number): Promise<void> {
  await waitFor(() => expect(document.querySelectorAll(".stat-card")).toHaveLength(count));
  for (const tile of document.querySelectorAll(".stat-card")) {
    expect(tile.querySelector(".stat-value")?.textContent).toBe("n/a");
    expect(tile.querySelector(".stat-zero")).toBeNull();
  }
}

describe("site servers on the list kit (4.9)", () => {
  it("draws no figure and no claim in its tiles before the list answers (clarification 10a)", async () => {
    mockUseApi.mockReturnValue(api({ listSiteServers: () => new Promise(() => {}) }));
    renderPage("/site-servers", <SiteServersPage />);
    await expectTilesAbsent(3);
  });

  /**
   * What the page reads on the console's own API, un-narrowed: the
   * prerequisites check above the list (the computers and the subnets a site
   * server needs) and Promote device's picker, which a write uses. Never the
   * site servers themselves.
   */
  const CONSOLE = ["listDevices", "listIpamSubnets"];

  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = renderPage("/site-servers?state=disabled", <SiteServersPage />);
    const chips = await screen.findByRole("group", { name: "Filter site servers by state" });
    expect(within(chips).getByRole("button", { name: "Disabled" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: "Enabled" }));
    expect(router.state.location.search).toContain("state=enabled");
  });

  it("says why the list is empty", async () => {
    const every = fixtureRegistry().listSiteServers as () => SiteServerOut[];
    mockUseApi.mockReturnValue(api({ listSiteServers: () => every().filter((server) => server.enabled) }));
    renderPage("/site-servers?state=disabled", <SiteServersPage />);
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("says there are none yet, with the way to make one, when there are none at all", async () => {
    mockUseApi.mockReturnValue(api({ listSiteServers: () => [] }));
    renderPage("/site-servers", <SiteServersPage />);
    expect(await screen.findByText("No site servers yet: promote a computer on the client's network.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Promote device" })).toHaveLength(2);
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    const narrowing = recordingNarrowing(api(), { console: CONSOLE });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/site-servers?client=${ANDES}`, <SiteServersPage />);
    expect(await screen.findByRole("table", { name: "Site servers" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listSiteServers).toHaveBeenCalledWith({});
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("opens Promote device from ?new=promote", async () => {
    const { router } = renderPage("/site-servers?new=promote", <SiteServersPage />);
    const dialog = await screen.findByRole("dialog", { name: "Promote a device to site server" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });

  it("draws loading, not an empty list, while /me has not said whose the chip's client is (general ruling 10)", async () => {
    // `/me` never answers: the narrowed read is held back, and the page must
    // not say "nothing matches" or "nothing yet" about a read it never made.
    mockUseApi.mockReturnValue(api({ me: () => new Promise(() => {}) }));
    renderPage(`/site-servers?client=${ANDES}`, <SiteServersPage />);
    expect(await screen.findByText("Loading site servers…")).toBeInTheDocument();
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
    expect(screen.queryByText("No site servers yet: promote a computer on the client's network.")).toBeNull();
  });

  it("links the tiles with the chip's client, so a tile opens the rows it counted (general ruling 2)", async () => {
    renderPage(`/site-servers?client=${ANDES}`, <SiteServersPage />);
    await screen.findByRole("table", { name: "Site servers" });
    const hrefs = [...document.querySelectorAll(".stat-card")].map((tile) => tile.getAttribute("href"));
    expect(hrefs).toEqual([`/site-servers?client=${ANDES}`, `/site-servers?state=enabled&client=${ANDES}`, `/site-servers?client=${ANDES}`]);
  });

  it("asks before Cancel drops a change in Promote device (general ruling 3)", async () => {
    const { router } = renderPage("/site-servers?new=promote", <SiteServersPage />, "/site-servers");
    const dialog = await screen.findByRole("dialog", { name: "Promote a device to site server" });
    await userEvent.type(within(dialog).getByLabelText("Segments (comma separated CIDRs)"), "10.0.0.0/24");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=promote");
  });
});

describe("a site server on the record kit (4.10)", () => {
  it("names the record in the browser tab", async () => {
    renderPage("/site-servers/ss-1", <SiteServerDetailPage />, "/site-servers/:siteServerId", {
      page: "shell.nav.item.siteServers",
    });
    await screen.findByRole("heading", { level: 1, name: "rack-b4-relay" });
    await waitFor(() => expect(document.title).toBe("rack-b4-relay · Site servers · Novieri RMM"));
  });

  it("leads back to the list, says the server's state, and takes Wake a machine to its form", async () => {
    renderPage("/site-servers/ss-1", <SiteServerDetailPage />, "/site-servers/:siteServerId");
    const head = (await screen.findByRole("heading", { level: 1, name: "rack-b4-relay" })).closest("section") as HTMLElement;
    expect(within(head).getByRole("link", { name: "All site servers" })).toHaveAttribute("href", "/site-servers");
    expect(within(head).getByText("Enabled")).toBeInTheDocument();
    await userEvent.click(within(head).getByRole("button", { name: "Wake a machine" }));
    const form = document.getElementById(WAKE_FORM_ID) as HTMLElement;
    expect(form).toContainElement(document.activeElement as HTMLElement);
  });

  it("offers no wake in the head of a server that is switched off", async () => {
    renderPage("/site-servers/ss-2", <SiteServerDetailPage />, "/site-servers/:siteServerId");
    const head = (await screen.findByRole("heading", { level: 1, name: "por-branch-relay" })).closest("section") as HTMLElement;
    expect(within(head).queryByRole("button", { name: "Wake a machine" })).toBeNull();
  });
});
