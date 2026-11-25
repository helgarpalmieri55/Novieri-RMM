/**
 * IP addresses on the kit (redesign plan, wave O5, 5.5 to 5.11): the subnet
 * tree's filters and Client chip in the URL (and the old `?register=1` link),
 * a subnet's record with its tab in the URL, the drift page's class chips and
 * open finding, the search in `?q=` once sent, the registry's dialogs, the
 * scan authorizations' chip and grant dialog, and the import wizard that asks
 * before a file is lost. Over the fixture session, with the real client scope.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../fixtures/client";
import { ApiError } from "../../lib/api";
import { selectedClient, setSelectedClient } from "../../lib/clientChoice";
import { recordingNarrowing } from "../../test/narrowing";
import { renderPage } from "../../test/renderPage";
import { DriftPage } from "./DriftPage";
import { ImportPage } from "./ImportPage";
import { RegistryPage } from "./RegistryPage";
import { ScanAuthorizationsPage } from "./ScanAuthorizationsPage";
import { SearchPage } from "./SearchPage";
import { SubnetDetailPage } from "./SubnetDetailPage";
import { SubnetTreePage } from "./SubnetTreePage";
import type { DriftOut } from "./types";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

afterEach(() => setSelectedClient(null));

/** The fixture session with `/me` held: the chip's client is not decided yet. */
function meHeld() {
  return fixtureClient({ ...fixtureRegistry(), me: () => new Promise<never>(() => {}) });
}

describe("no empty state before the narrowed read (coordinator's ruling 10)", () => {
  it("the drift page draws loading while the Client chip's client is still being decided", async () => {
    mockUseApi.mockReturnValue(meHeld());
    renderPage(`/ipam/drift?client=${ANDES}`, <DriftPage />, "/ipam/drift");
    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
    expect(screen.queryByText("No drift: what is registered and what was seen agree.")).toBeNull();
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
  });
});

/**
 * The coordinator's ruling 10a (corrected): a tile with no data, pending or
 * failed, draws the kit's absent mark (`shell.viz.na`, "n/a") and no line
 * under it -- never "—", never "0".
 */
function expectNoFigure(tile: HTMLElement) {
  expect(tile.querySelector(".v")?.textContent).toBe("n/a");
  expect(tile.querySelector(".z")).toBeNull();
}

describe("no tile figure before its read (coordinator's ruling 10a)", () => {
  it("draws no tile and no claim on the tree while the chip's client is being decided", async () => {
    mockUseApi.mockReturnValue(meHeld());
    const { container } = renderPage(`/ipam?client=${ANDES}`, <SubnetTreePage />, "/ipam");
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(container.querySelector(".stat-card")).toBeNull();
    expect(screen.queryByText(/No subnet is registered, so there is no drift figure/)).toBeNull();
  });

  it.each([
    ["until the drift answers", () => new Promise<never>(() => {}), false],
    ["when the drift fails", () => Promise.reject(new Error("unavailable")), true],
  ])("draws no drift count and no line under it on the tree %s", async (_when, listIpamDrift, fails) => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listIpamDrift } as never));
    const { queryClient } = renderPage("/ipam", <SubnetTreePage />, "/ipam");
    const tile = await screen.findByRole("link", { name: /With drift/ });
    // The failed read has settled once nothing is fetching any more.
    if (fails) await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expectNoFigure(tile);
    expect(screen.queryByText("No subnet has an unresolved finding")).toBeNull();
  });

  it("draws no count and no claim on the drift page's tiles when its lists fail", async () => {
    const down = () => Promise.reject(new Error("unavailable"));
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listIpamDrift: down, listIpamOrphans: down } as never));
    const { container } = renderPage("/ipam/drift", <DriftPage />, "/ipam/drift");
    // The list beside the tiles says why.
    expect((await screen.findAllByRole("alert")).length).toBeGreaterThan(0);
    await waitFor(() => expect(container.querySelectorAll(".stat-card .v")[2]?.textContent).toBe("n/a"));
    for (const tile of container.querySelectorAll<HTMLElement>(".stat-card")) expectNoFigure(tile);
    expect(screen.queryByText("No unresolved finding")).toBeNull();
  });

  it("draws no count and no claim on the drift page's tiles while the chip's client is being decided", async () => {
    mockUseApi.mockReturnValue(meHeld());
    const { container } = renderPage(`/ipam/drift?client=${ANDES}`, <DriftPage />, "/ipam/drift");
    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
    // Drift, Dormant and Orphans (the nav's own "Drift" link shares a name).
    const tiles = [...container.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles).toHaveLength(3);
    for (const tile of tiles) expectNoFigure(tile);
    for (const claim of [
      "No unresolved finding",
      "Every documented address was verifiable",
      "Every observed address is inside a registered subnet",
    ]) {
      expect(screen.queryByText(claim)).toBeNull();
    }
  });
});

/**
 * The coordinator's ruling 3a: the leave question is drawn AFTER its form's
 * dialog, never before it or inside it. Both are `.modal`s at one z-index, so
 * the later one in the document paints on top; drawn inside it, it was also
 * placed and clipped by the dialog's own box (`.modal.open` is transformed).
 */
describe("the leave question is drawn on top of its dialog (coordinator's ruling 3a)", () => {
  it.each([
    ["Register a subnet", "/ipam?new=subnet", <SubnetTreePage key="t" />, "/ipam", "Register a subnet", "Prefix"],
    ["New VRF", "/ipam/registry?new=vrf", <RegistryPage key="v" />, "/ipam/registry", "New VRF", "Name"],
    ["New VLAN", "/ipam/registry?new=vlan", <RegistryPage key="l" />, "/ipam/registry", "New VLAN", "VID"],
    ["Authorize a prefix", "/ipam/scan-authorizations?new=grant", <ScanAuthorizationsPage key="g" />, "/ipam/scan-authorizations", "Authorize a prefix", "Prefix"],
  ])("%s", async (_name, path, page, pattern, title, field) => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage(path, page, pattern);
    const form = await screen.findByRole("dialog", { name: title });
    await userEvent.type(within(form).getByLabelText(field), "10");
    await userEvent.click(within(form).getByRole("button", { name: "Close" }));
    const question = await screen.findByRole("dialog", { name: "Leave without saving?" });
    expect(form.contains(question)).toBe(false);
    expect(form.compareDocumentPosition(question) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });
});

describe("a sub-page names itself in the tab, as its h1 does (coordinator's ruling 1b)", () => {
  it.each([
    ["/ipam/drift", <DriftPage key="d" />, "Drift"],
    ["/ipam/search", <SearchPage key="s" />, "Search"],
    ["/ipam/registry", <RegistryPage key="r" />, "Registry"],
    ["/ipam/scan-authorizations", <ScanAuthorizationsPage key="a" />, "Scan authorizations"],
    ["/ipam/import", <ImportPage key="i" />, "Import and export"],
  ])("%s", async (path, page, title) => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage(path, page, path, { page: "shell.nav.item.ipAddresses" });
    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe(`${title} · IP addresses · Novieri RMM`));
  });
});

describe("the subnet tree on the list kit", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/ipam?state=never-swept", <SubnetTreePage />, "/ipam");
    const views = await screen.findByRole("group", { name: "Filter by view" });
    expect(within(views).getByRole("button", { name: /Never swept/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(views).getByRole("button", { name: /Assignable only/ }));
    expect(router.state.location.search).toBe("?state=assignable");
    await userEvent.selectOptions(screen.getByLabelText("Filter by VRF"), screen.getAllByRole("option", { name: "global" })[0]!);
    expect(router.state.location.search).toMatch(/^\?state=assignable&vrf=/);
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/ipam?q=nothing-like-it", <SubnetTreePage />, "/ipam");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // What the page reads on the console's own API, and why none is the tree
    // or a tile's count: the VRF, site and VLAN pickers are the scoping
    // registry (`listIpamVrfs`, `listClientSites`, `listIpamVlans`, named by
    // the top bar's choice); the readiness panel asks for the scan
    // authorizations, the discovered machines, the site servers and the
    // notification rules (`listIpamScanAuthorizations`, `listAssetCandidates`,
    // `listSiteServers`, `listNotificationRules`: the organisation's setup,
    // D20). The subnets it counts, and each row's utilization, are narrowed.
    const narrowing = recordingNarrowing(fixtureClient(), {
      console: [
        "listIpamVrfs",
        "listClientSites",
        "listIpamVlans",
        "listIpamScanAuthorizations",
        "listAssetCandidates",
        "listSiteServers",
        "listNotificationRules",
      ],
    });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/ipam?client=${ANDES}`, <SubnetTreePage />, "/ipam");
    await waitFor(() => expect(narrowing.client(ANDES).listIpamSubnets).toHaveBeenCalledWith({}));
    // The drift tile counts the same client's subnets.
    expect(narrowing.client(ANDES).listIpamDrift).toHaveBeenCalledWith({});
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("narrows with ?client= and leaves the console's client alone (decision D20)", async () => {
    setSelectedClient(null);
    const narrowing = recordingNarrowing(fixtureClient(), {
      console: [
        "listIpamVrfs",
        "listClientSites",
        "listIpamVlans",
        "listIpamScanAuthorizations",
        "listAssetCandidates",
        "listSiteServers",
        "listNotificationRules",
      ],
    });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/ipam?client=${ANDES}`, <SubnetTreePage />, "/ipam");
    await waitFor(() => expect(narrowing.client(ANDES).listIpamSubnets).toHaveBeenCalled());
    expect(selectedClient()).toBeNull();
  });

  it("still switches the console's client from the journey's ?journey_client= (Task 5)", async () => {
    setSelectedClient(null);
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage(`/ipam?journey_client=${ANDES}`, <SubnetTreePage />, "/ipam");
    await waitFor(() => expect(selectedClient()).toBe(ANDES));
  });

  it("draws no empty state while the Client chip's client is still being decided", async () => {
    mockUseApi.mockReturnValue(meHeld());
    renderPage(`/ipam?client=${ANDES}`, <SubnetTreePage />, "/ipam");
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("No subnets registered yet.")).toBeNull();
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
  });

  it("opens, from a tile counted for the chip's client, exactly the rows it counted", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listIpamSubnets: () => fixtureRegistry().listIpamSubnets!({}) }));
    renderPage(`/ipam?client=${ANDES}`, <SubnetTreePage />, "/ipam");
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /Never swept/ })).toHaveAttribute(
        "href",
        `/ipam?state=never-swept&client=${ANDES}`,
      ),
    );
    expect(screen.getByRole("link", { name: /With drift/ })).toHaveAttribute("href", `/ipam/drift?client=${ANDES}`);
  });

  it("still opens the register dialog from an old ?register=1 link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/ipam?register=1", <SubnetTreePage />, "/ipam");
    expect(await screen.findByRole("dialog", { name: "Register a subnet" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=subnet");
  });

  it("asks before leaving the register dialog with a prefix typed", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/ipam?new=subnet", <SubnetTreePage />, "/ipam");
    const dialog = await screen.findByRole("dialog", { name: "Register a subnet" });
    await userEvent.type(within(dialog).getByPlaceholderText("10.4.7.0/24"), "10.9.9.0/24");
    await userEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=subnet");
  });
});

describe("a subnet on the record kit", () => {
  it("puts the tab in the URL, and an unknown tab opens the default", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/ipam/subnets/subnet-1?tab=sweeps", <SubnetDetailPage />, "/ipam/subnets/:subnetId");
    expect(await screen.findByRole("tab", { name: "Sweeps" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "Ranges" }));
    expect(router.state.location.search).toBe("?tab=ranges");
    await act(() => router.navigate("/ipam/subnets/subnet-1?tab=nope"));
    expect(await screen.findByRole("tab", { name: "Addresses" })).toHaveAttribute("aria-selected", "true");
  });

  it("names the record in the browser tab", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/ipam/subnets/subnet-1", <SubnetDetailPage />, "/ipam/subnets/:subnetId", { page: "shell.nav.item.ipAddresses" });
    await screen.findByRole("heading", { level: 1, name: "10.20.9.0/24" });
    await waitFor(() => expect(document.title).toBe("10.20.9.0/24 · IP addresses · Novieri RMM"));
    expect(screen.getByRole("link", { name: "All subnets" })).toHaveAttribute("href", "/ipam");
  });

  it("sends nothing when the subnet's edit is saved untouched (coordinator's ruling 13)", async () => {
    const updateIpamSubnet = vi.fn();
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), updateIpamSubnet } as never));
    renderPage("/ipam/subnets/subnet-1", <SubnetDetailPage />, "/ipam/subnets/:subnetId");
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit subnet 10.20.9.0/24" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
    // It closes, as a save would, and writes nothing.
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit subnet 10.20.9.0/24" })).toBeNull());
    expect(updateIpamSubnet).not.toHaveBeenCalled();
  });

  it("sends nothing when an address's edits are saved untouched (coordinator's ruling 13)", async () => {
    const updateIpamAddress = vi.fn();
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), updateIpamAddress } as never));
    renderPage("/ipam/subnets/subnet-1", <SubnetDetailPage />, "/ipam/subnets/:subnetId");
    await userEvent.click((await screen.findAllByRole("button", { name: "Manage" }))[0]!);
    const dialog = await screen.findByRole("dialog", { name: "10.20.9.1" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save edits" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "10.20.9.1" })).toBeNull());
    expect(updateIpamAddress).not.toHaveBeenCalled();
  });

  it("offers no sweep-settings Save until something changed (coordinator's ruling 13, the kit's save-bar convention)", async () => {
    const updateIpamSweepSettings = vi.fn();
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), updateIpamSweepSettings } as never));
    renderPage("/ipam/subnets/subnet-1?tab=sweeps", <SubnetDetailPage />, "/ipam/subnets/:subnetId");
    const interval = await screen.findByLabelText("Interval (hours)");
    // Untouched: nothing to save, so no Save to press (and nothing is sent).
    expect(screen.queryByRole("button", { name: "Save sweep settings" })).toBeNull();
    await userEvent.clear(interval);
    await userEvent.type(interval, "48");
    expect(screen.getByRole("button", { name: "Save sweep settings" })).toBeInTheDocument();
    // Typed back to what is stored: nothing changed again.
    await userEvent.clear(interval);
    await userEvent.type(interval, "24");
    expect(screen.queryByRole("button", { name: "Save sweep settings" })).toBeNull();
    expect(updateIpamSweepSettings).not.toHaveBeenCalled();
  });

  it.each([
    // An empty stored interval is "on demand only"; the box shows the weekly
    // default (168 h) for it, and typing that default IS a change to save.
    ["168 h on an on-demand subnet", "subnet-3", "168", 168],
    ["24 h on an on-demand subnet", "subnet-3", "24", 24],
    ["24 h on a weekly subnet", "subnet-4", "24", 24],
  ])("sends %s", async (_what, id, typed, sent) => {
    const updateIpamSweepSettings = vi.fn((subnetId: string) => fixtureRegistry().getIpamSubnet!(subnetId));
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), updateIpamSweepSettings } as never));
    renderPage(`/ipam/subnets/${id}?tab=sweeps`, <SubnetDetailPage />, "/ipam/subnets/:subnetId");
    const interval = await screen.findByLabelText("Interval (hours)");
    await userEvent.clear(interval);
    await userEvent.type(interval, typed);
    await userEvent.click(screen.getByRole("button", { name: "Save sweep settings" }));
    await waitFor(() =>
      expect(updateIpamSweepSettings).toHaveBeenCalledWith(id, expect.objectContaining({ sweep_interval_hours: sent })),
    );
  });

  it("a sweep-settings edit left on one subnet does not reach the next (coordinator's ruling 12)", async () => {
    const updateIpamSweepSettings = vi.fn();
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), updateIpamSweepSettings } as never));
    // 10.20.4.0/24 first, so it is already read when the edit leaves for it.
    const { router } = renderPage("/ipam/subnets/subnet-2?tab=sweeps", <SubnetDetailPage />, "/ipam/subnets/:subnetId");
    await screen.findByRole("heading", { level: 1, name: "10.20.4.0/24" });
    await act(() => router.navigate("/ipam/subnets/subnet-1?tab=sweeps"));
    await screen.findByRole("heading", { level: 1, name: "10.20.9.0/24" });
    const interval = await screen.findByLabelText("Interval (hours)");
    await userEvent.clear(interval);
    await userEvent.type(interval, "48");
    await act(() => router.navigate("/ipam/subnets/subnet-2?tab=sweeps"));
    await screen.findByRole("heading", { level: 1, name: "10.20.4.0/24" });
    // Its own 24 hours, nothing of the other subnet's edit to save, and nothing sent.
    expect(await screen.findByLabelText("Interval (hours)")).toHaveValue(24);
    expect(screen.queryByRole("button", { name: "Save sweep settings" })).toBeNull();
    expect(updateIpamSweepSettings).not.toHaveBeenCalled();
  });
});

describe("a new attempt clears the last one's outcome (coordinator's ruling 7)", () => {
  const unavailable = () => Promise.reject(new ApiError(503, ""));

  it("Register a subnet: the last 503 does not hide a malformed prefix", async () => {
    // A client chosen at the top, so the form needs no Client field.
    setSelectedClient(ANDES);
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), createIpamSubnet: unavailable } as never));
    renderPage("/ipam?new=subnet", <SubnetTreePage />, "/ipam");
    const form = await screen.findByRole("dialog", { name: "Register a subnet" });
    await userEvent.type(within(form).getByLabelText("Prefix"), "10.9.9.0/24");
    await userEvent.click(within(form).getByRole("button", { name: "Register" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent(/service is unavailable right now/i);
    await userEvent.clear(within(form).getByLabelText("Prefix"));
    await userEvent.type(within(form).getByLabelText("Prefix"), "nope");
    await userEvent.click(within(form).getByRole("button", { name: "Register" }));
    const alert = await within(form).findByRole("alert");
    expect(alert).toHaveTextContent(/nope/);
    expect(alert).not.toHaveTextContent(/unavailable/i);
  });

  it("Register a subnet: the last success's note does not stand beside a refused attempt", async () => {
    setSelectedClient(ANDES);
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/ipam?new=subnet", <SubnetTreePage />, "/ipam");
    const form = await screen.findByRole("dialog", { name: "Register a subnet" });
    await userEvent.type(within(form).getByLabelText("Prefix"), "10.9.9.0/24");
    await userEvent.click(within(form).getByRole("button", { name: "Register" }));
    // The dialog stays open after a registration, as it always did.
    expect(await within(form).findByText("Subnet registered.")).toBeInTheDocument();
    await userEvent.type(within(form).getByLabelText("Prefix"), "nope");
    await userEvent.click(within(form).getByRole("button", { name: "Register" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent(/nope/);
    expect(within(form).queryByText("Subnet registered.")).toBeNull();
  });

  it("Edit subnet: the last 503 does not hide a malformed gateway", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), updateIpamSubnet: unavailable } as never));
    renderPage("/ipam/subnets/subnet-1", <SubnetDetailPage />, "/ipam/subnets/:subnetId");
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit subnet 10.20.9.0/24" });
    await userEvent.type(within(dialog).getByLabelText("Name"), " 2");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/service is unavailable right now/i);
    const gateway = within(dialog).getByLabelText("Gateway");
    await userEvent.clear(gateway);
    await userEvent.type(gateway, "nope");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(within(dialog).getByRole("alert")).toHaveTextContent(/Gateway must be an IP address/));
    expect(within(dialog).getByRole("alert")).not.toHaveTextContent(/unavailable/i);
  });
});

describe("an edit never carries to another client (coordinator's ruling 12)", () => {
  it.each([
    ["Register a subnet", "/ipam?new=subnet", <SubnetTreePage key="t" />, "/ipam", "Prefix", "10.9.9.0/24", "Register", "createIpamSubnet"],
    ["New VRF", "/ipam/registry?new=vrf", <RegistryPage key="v" />, "/ipam/registry", "Name", "lab2", "Add VRF", "createIpamVrf"],
    ["New VLAN", "/ipam/registry?new=vlan", <RegistryPage key="l" />, "/ipam/registry", "VID", "70", "Add VLAN", "createIpamVlan"],
    ["Authorize a prefix", "/ipam/scan-authorizations?new=grant", <ScanAuthorizationsPage key="g" />, "/ipam/scan-authorizations", "Prefix", "10.9.9.0/24", "Authorize", "createIpamScanAuthorization"],
  ])("%s drops what was typed when the top bar's client changes", async (title, path, page, pattern, field, typed, submit, create) => {
    const spy = vi.fn();
    setSelectedClient(ANDES);
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), [create]: spy } as never));
    renderPage(path, page, pattern);
    let form = await screen.findByRole("dialog", { name: title });
    await userEvent.type(within(form).getByLabelText(field), typed);
    act(() => setSelectedClient("org-toeshee"));
    // The form is the new client's, empty; its button sends nothing of the old edit.
    form = await screen.findByRole("dialog", { name: title });
    expect(within(form).getByLabelText(field)).toHaveValue(field === "VID" ? null : "");
    const button = within(form).getByRole("button", { name: submit });
    if (!(button as HTMLButtonElement).disabled) await userEvent.click(button);
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("the drift page on the list kit", () => {
  // Review I5: the drift page has the Client chip (Q2), so its lists are read
  // narrowed. `listDevices` is the evidence's names for the machines a finding
  // mentions (`useIpamDeviceNames`): a courtesy over ids, read through the
  // console like the other organisation-wide lookups, never a list of rows.
  it("reads the drift lists only through the narrowed API of the Client chip's client (decision D6)", async () => {
    const narrowing = recordingNarrowing(fixtureClient(), { console: ["listDevices"] });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/ipam/drift?client=${ANDES}`, <DriftPage />, "/ipam/drift");
    expect(await screen.findByRole("table", { name: "Drift" })).toBeInTheDocument();
    await waitFor(() => expect(narrowing.client(ANDES).listIpamOrphans).toHaveBeenCalledWith());
    expect(narrowing.client(ANDES).listIpamDrift).toHaveBeenCalledWith({});
    expect(narrowing.client(ANDES).listIpamDrift).toHaveBeenCalledWith({ class: "dormant" });
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("opens, from a tile counted for the chip's client, exactly the rows it counted", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { container } = renderPage(`/ipam/drift?client=${ANDES}`, <DriftPage />, "/ipam/drift");
    // Once the chip's client is decided (`/me`), each tile carries it.
    await waitFor(() =>
      expect([...container.querySelectorAll("a.stat-card")].map((a) => a.getAttribute("href"))).toEqual([
        `/ipam/drift?client=${ANDES}`,
        `/ipam/drift?class=dormant&client=${ANDES}`,
        `/ipam/drift?class=orphans&client=${ANDES}`,
      ]),
    );
  });

  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/ipam/drift?class=conflict", <DriftPage />, "/ipam/drift");
    const chips = await screen.findByRole("group", { name: "Drift class" });
    expect(await within(chips).findByRole("button", { name: /^Conflict/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: /^Dormant/ }));
    expect(router.state.location.search).toBe("?class=dormant");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listIpamDrift: () => [] }));
    renderPage("/ipam/drift?class=conflict", <DriftPage />, "/ipam/drift");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("says there is no drift, with its cause, when nothing drifts", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listIpamDrift: () => [] }));
    renderPage("/ipam/drift", <DriftPage />, "/ipam/drift");
    expect(await screen.findByText("No drift: what is registered and what was seen agree.")).toBeInTheDocument();
  });

  it("opens a finding from ?finding=<id>", async () => {
    const finding = (fixtureRegistry().listIpamDrift!({}) as DriftOut[])[0]!;
    const acknowledgeIpamDrift = vi.fn((id: string, note: string) => ({ ...finding, id, note }));
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), acknowledgeIpamDrift } as never));
    const { router } = renderPage("/ipam/drift?finding=drift-1", <DriftPage />, "/ipam/drift");
    expect(await screen.findByText("Acknowledge or suppress")).toBeInTheDocument();
    // The panel names the finding it handles.
    expect(screen.getAllByText("10.20.9.14").length).toBeGreaterThan(1);
    // Acknowledging sends what it did before, and leaves without asking.
    await userEvent.type(screen.getByLabelText("Note"), "known printer");
    await userEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(acknowledgeIpamDrift).toHaveBeenCalledWith("drift-1", "known printer"));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });

  // Review M2 (ruling 7): a failed Acknowledge's error stayed on screen while
  // Suppress was being tried, and stood in for Suppress's own outcome.
  it("clears a failed Acknowledge's error when Suppress is tried", async () => {
    mockUseApi.mockReturnValue(
      fixtureClient({
        ...fixtureRegistry(),
        acknowledgeIpamDrift: () => Promise.reject(new ApiError(503, "")),
        suppressIpamDrift: () => new Promise<never>(() => {}),
      } as never),
    );
    renderPage("/ipam/drift?finding=drift-1", <DriftPage />, "/ipam/drift");
    await userEvent.type(await screen.findByLabelText("Note"), "known printer");
    await userEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    expect(await screen.findByText(/service is unavailable right now/i)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Suppress until"), "2026-12-31");
    await userEvent.click(screen.getByRole("button", { name: "Suppress" }));
    await waitFor(() => expect(screen.queryByText(/service is unavailable right now/i)).toBeNull());
  });

  it("says why Suppress cannot be pressed yet", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/ipam/drift?finding=drift-1", <DriftPage />, "/ipam/drift");
    expect(await screen.findByRole("button", { name: "Suppress" })).toBeDisabled();
    expect(screen.getByText("Suppressing needs a note and a date.")).toBeInTheDocument();
  });

  // Review I4: the orphans view's "Clear filters" did nothing (a dead control).
  it("clears the Client chip from an empty orphans view, keeping the view", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listIpamOrphans: () => [] }));
    const { router } = renderPage(`/ipam/drift?class=orphans&client=${ANDES}`, <DriftPage />, "/ipam/drift");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(router.state.location.search).toBe("?class=orphans"));
  });

  it("keeps the register-the-subnet link of an orphan as it was", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/ipam/drift?class=orphans", <DriftPage />, "/ipam/drift");
    expect((await screen.findAllByRole("link", { name: "Register this subnet" }))[0]).toHaveAttribute("href", "/ipam?register=1");
  });
});

describe("the IPAM search", () => {
  it("keeps the search in the URL once it is sent", async () => {
    const searchIpam = vi.fn((q: string) => fixtureRegistry().searchIpam!(q));
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), searchIpam } as never));
    const { router } = renderPage("/ipam/search", <SearchPage />, "/ipam/search");
    const box = await screen.findByRole("textbox", { name: "Search addresses, subnets and devices" });
    await userEvent.type(box, "10.20.9.14");
    // Typing does not search.
    expect(router.state.location.search).toBe("");
    expect(searchIpam).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(router.state.location.search).toBe("?q=10.20.9.14");
    await waitFor(() => expect(searchIpam).toHaveBeenCalledWith("10.20.9.14"));
    // A second search is its own entry: Back returns to the first.
    await userEvent.clear(box);
    await userEvent.type(box, "10.20.9.0/24");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(router.state.location.search).toBe("?q=10.20.9.0%2F24");
    await act(() => router.navigate(-1));
    expect(router.state.location.search).toBe("?q=10.20.9.14");
    expect(screen.getByRole("textbox", { name: "Search addresses, subnets and devices" })).toHaveValue("10.20.9.14");
  });

  it("repeats the search a link carries", async () => {
    const searchIpam = vi.fn((q: string) => fixtureRegistry().searchIpam!(q));
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), searchIpam } as never));
    renderPage("/ipam/search?q=10.20.9.14", <SearchPage />, "/ipam/search");
    await waitFor(() => expect(searchIpam).toHaveBeenCalledWith("10.20.9.14"));
    expect(screen.getByRole("textbox", { name: "Search addresses, subnets and devices" })).toHaveValue("10.20.9.14");
  });
});

describe("the registry's dialogs", () => {
  it("opens New VRF from ?new=vrf and sends the body it always sent", async () => {
    const createIpamVrf = vi.fn((body: { name: string }) => ({ id: "vrf-new", client_id: "org-toeshee", name: body.name, rd: null, description: "" }));
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), createIpamVrf } as never));
    const { router } = renderPage("/ipam/registry?new=vrf", <RegistryPage />, "/ipam/registry");
    const dialog = await screen.findByRole("dialog", { name: "New VRF" });
    // The fixture session holds every client: the dialog asks whose VRF it is.
    await userEvent.selectOptions(await within(dialog).findByLabelText("Client"), "org-toeshee");
    await userEvent.type(within(dialog).getByLabelText("Name"), "lab2");
    await userEvent.type(within(dialog).getByLabelText("Route distinguisher"), "65000:30");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add VRF" }));
    await waitFor(() =>
      expect(createIpamVrf).toHaveBeenCalledWith({ name: "lab2", rd: "65000:30", client_id: "org-toeshee" }),
    );
    // Saved, it closes without asking.
    await waitFor(() => expect(router.state.location.search).toBe(""));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });

  it("opens New VLAN from ?new=vlan and still chooses its client and its site", async () => {
    const createIpamVlan = vi.fn((body: object) => ({ id: "vl-new", description: "", ...body }));
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), createIpamVlan } as never));
    renderPage("/ipam/registry?new=vlan", <RegistryPage />, "/ipam/registry");
    const dialog = await screen.findByRole("dialog", { name: "New VLAN" });
    await userEvent.selectOptions(await within(dialog).findByLabelText("Client"), "org-toeshee");
    await userEvent.type(within(dialog).getByLabelText("VID"), "70");
    await userEvent.type(within(dialog).getByLabelText("Name"), "voice");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add VLAN" }));
    await waitFor(() =>
      expect(createIpamVlan).toHaveBeenCalledWith({ vid: 70, site_id: null, client_id: "org-toeshee", name: "voice" }),
    );
  });

  it("names a VLAN's missing VID on Add instead of sitting disabled", async () => {
    const createIpamVlan = vi.fn();
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), createIpamVlan } as never));
    renderPage("/ipam/registry?new=vlan", <RegistryPage />, "/ipam/registry");
    const dialog = await screen.findByRole("dialog", { name: "New VLAN" });
    await userEvent.selectOptions(await within(dialog).findByLabelText("Client"), "org-toeshee");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add VLAN" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("VID is required");
    expect(createIpamVlan).not.toHaveBeenCalled();
  });
});

describe("scan authorizations on the list kit", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const listIpamScanAuthorizations = vi.fn((includeRevoked?: boolean) => fixtureRegistry().listIpamScanAuthorizations!(includeRevoked));
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listIpamScanAuthorizations } as never));
    const { router } = renderPage("/ipam/scan-authorizations?revoked=include", <ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    const chips = await screen.findByRole("group", { name: "Which authorizations" });
    expect(within(chips).getByRole("button", { name: "Revoked and expired too" })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(listIpamScanAuthorizations).toHaveBeenCalledWith(true));
    await userEvent.click(within(chips).getByRole("button", { name: "In force" }));
    expect(router.state.location.search).toBe("");
    await waitFor(() => expect(listIpamScanAuthorizations).toHaveBeenLastCalledWith(false));
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listIpamScanAuthorizations: () => [] }));
    renderPage("/ipam/scan-authorizations?revoked=include", <ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("opens Grant from ?new=grant, and says why Authorize waits for a prefix", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/ipam/scan-authorizations?new=grant", <ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    const dialog = await screen.findByRole("dialog", { name: "Authorize a prefix" });
    expect(within(dialog).getByRole("button", { name: "Authorize" })).toBeDisabled();
    expect(within(dialog).getByText("Type a prefix or pick a subnet to see what it would open.")).toBeInTheDocument();
  });
});

describe("the import wizard", () => {
  it("drops a chosen file and its mapping when the top bar's client changes (coordinator's ruling 12)", async () => {
    const previewIpamImport = vi.fn();
    setSelectedClient(ANDES);
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), previewIpamImport } as never));
    renderPage("/ipam/import", <ImportPage />, "/ipam/import");
    const input = (await screen.findByLabelText("CSV file")) as HTMLInputElement;
    await userEvent.upload(input, new File(["address,hostname\n10.20.9.40,WS-40\n"], "plan.csv", { type: "text/csv" }));
    expect(await screen.findByText("plan.csv", { exact: false })).toBeInTheDocument();
    act(() => setSelectedClient("org-toeshee"));
    // The wizard is the new client's, back at its first step; Preview sends nothing of the old file.
    expect(await screen.findByText("Choose a CSV first: its first line names the columns to map.")).toBeInTheDocument();
    expect(((await screen.findByLabelText("CSV file")) as HTMLInputElement).files?.length ?? 0).toBe(0);
    const previewButton = screen.getByRole("button", { name: "Preview" });
    if (!(previewButton as HTMLButtonElement).disabled) await userEvent.click(previewButton);
    expect(previewIpamImport).not.toHaveBeenCalled();
  });

  it("asks before leaving with a file that is not applied", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/ipam/import", <ImportPage />, "/ipam/import");
    const input = (await screen.findByLabelText("CSV file")) as HTMLInputElement;
    await userEvent.upload(input, new File(["address,hostname\n10.20.9.40,WS-40\n"], "plan.csv", { type: "text/csv" }));
    await userEvent.click(screen.getByRole("link", { name: "Drift" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/ipam/import");
  });

  it("leaves without asking once the import is applied", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/ipam/import", <ImportPage />, "/ipam/import");
    const input = (await screen.findByLabelText("CSV file")) as HTMLInputElement;
    await userEvent.upload(input, new File(["address,hostname\n10.20.9.40,WS-40\n"], "plan.csv", { type: "text/csv" }));
    await userEvent.selectOptions(await screen.findByLabelText("address"), "address");
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText("Applied")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Drift" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/ipam/drift"));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });
});
