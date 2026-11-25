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

/** The coordinator's ruling 10a: a tile whose read has not answered draws no figure and no claim. */
function expectNoFigure(tile: HTMLElement) {
  expect(tile.querySelector(".stat-value")).not.toHaveTextContent(/\d/);
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

  it("draws no drift count and no line under it on the tree until the drift answers", async () => {
    mockUseApi.mockReturnValue(
      fixtureClient({ ...fixtureRegistry(), listIpamDrift: () => new Promise<never>(() => {}) } as never),
    );
    renderPage("/ipam", <SubnetTreePage />, "/ipam");
    const tile = await screen.findByRole("link", { name: /With drift/ });
    expectNoFigure(tile);
    expect(screen.queryByText("No subnet has an unresolved finding")).toBeNull();
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
});

describe("the drift page on the list kit", () => {
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

  it("says why Suppress cannot be pressed yet", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/ipam/drift?finding=drift-1", <DriftPage />, "/ipam/drift");
    expect(await screen.findByRole("button", { name: "Suppress" })).toBeDisabled();
    expect(screen.getByText("Suppressing needs a note and a date.")).toBeInTheDocument();
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
