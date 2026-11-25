/**
 * The three Software deployment pages on the list kit (redesign plan, O3 3.3
 * to 3.5): filters and dialogs in the URL, an empty list that says why, and
 * the deployments read under the Client chip's client. Over the fixture
 * session, with the real client scope.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../../fixtures/client";
import { recordingNarrowing } from "../../../test/narrowing";
import { renderPage } from "../../../test/renderPage";
import { DeployPage } from "../DeployPage";
import { PackagesPage } from "../PackagesPage";
import { ScriptsPage } from "../ScriptsPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

describe("Deployments (/deploy)", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/deploy?state=failed", <DeployPage />, "/deploy");
    const chips = await screen.findByRole("group", { name: "Filter by state" });
    expect(within(chips).getByRole("button", { name: "Failed" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: "Pending" }));
    expect(router.state.location.search).toContain("state=pending");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listDeployments: () => [] }));
    renderPage("/deploy?state=failed", <DeployPage />, "/deploy");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("says nothing has been deployed yet, and offers the first deployment", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listDeployments: () => [] }));
    renderPage("/deploy", <DeployPage />, "/deploy");
    const empty = (await screen.findByText("No deployments yet.")).closest(".empty") as HTMLElement;
    expect(within(empty).getByRole("button", { name: "New deployment" })).toBeInTheDocument();
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // What the page reads on the console's own API, and why none is the list:
    // the catalogue (`listPackages`), the devices and groups (`listDevices`,
    // `listDeviceGroups`) and the people (`listUsers`) only put names on the
    // rows and fill the New deployment dialog, whose deployment is a write
    // that keeps `useApi()`; the readiness panel's reads (`listChangeGates`,
    // `listChanges`) ask about the organisation's setup, which the top bar
    // decides (D20: the chip narrows the list).
    const narrowing = recordingNarrowing(fixtureClient(), {
      console: ["listPackages", "listDevices", "listDeviceGroups", "listUsers", "listChangeGates", "listChanges"],
    });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/deploy?client=${ANDES}`, <DeployPage />, "/deploy");
    // The table is named for what it lists ("Deployments", the section nav's word): the heading
    // became the rail's "Software" (coordinator ruling, wave O3 fix round 1).
    expect(await screen.findByRole("table", { name: "Deployments" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listDeployments).toHaveBeenCalledWith();
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("opens New deployment from ?new=deployment", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/deploy?new=deployment", <DeployPage />, "/deploy");
    expect(await screen.findByRole("dialog", { name: "New deployment" })).toBeInTheDocument();
  });

  it("still asks before executing, through the same confirmation", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/deploy", <DeployPage />, "/deploy");
    const table = await screen.findByRole("table", { name: "Deployments" });
    await userEvent.click(within(table).getAllByRole("button", { name: "Execute" })[0]!);
    expect(await screen.findByRole("dialog", { name: "Execute deployment" })).toBeInTheDocument();
  });
});

describe("Packages (/deploy/packages)", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/deploy/packages?os=linux", <PackagesPage />, "/deploy/packages");
    const chips = await screen.findByRole("group", { name: "System" });
    expect(within(chips).getByRole("button", { name: "Linux" })).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByText("novieri-agent")).toBeInTheDocument();
    expect(screen.queryByText("Google Chrome")).toBeNull();
    await userEvent.click(within(chips).getByRole("button", { name: "Windows" }));
    expect(router.state.location.search).toBe("?os=windows");
  });

  it("keeps its search in the URL, so a reload finds the same rows", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/deploy/packages", <PackagesPage />, "/deploy/packages");
    await userEvent.type(await screen.findByRole("textbox", { name: "Search packages" }), "zip");
    expect(router.state.location.search).toBe("?q=zip");
    expect(await screen.findByText("7-Zip")).toBeInTheDocument();
    expect(screen.queryByText("Google Chrome")).toBeNull();
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/deploy/packages?q=nothing-like-this", <PackagesPage />, "/deploy/packages");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("opens New package from ?new=package", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/deploy/packages?new=package", <PackagesPage />, "/deploy/packages");
    expect(await screen.findByRole("dialog", { name: "New package" })).toBeInTheDocument();
  });

  it("still asks before deleting a package", async () => {
    const deletePackage = vi.fn();
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), deletePackage }));
    renderPage("/deploy/packages", <PackagesPage />, "/deploy/packages");
    const row = (await screen.findByText("7-Zip")).closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: /More actions/ }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    expect(await screen.findByRole("dialog", { name: "Delete package" })).toBeInTheDocument();
    expect(deletePackage).not.toHaveBeenCalled();
  });
});

describe("Scripts (/deploy/scripts)", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/deploy/scripts?interpreter=bash", <ScriptsPage />, "/deploy/scripts");
    const chips = await screen.findByRole("group", { name: "Interpreter" });
    expect(within(chips).getByRole("button", { name: "Bash" })).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByText("Clean apt cache", { selector: ".cell-title" })).toBeInTheDocument();
    expect(screen.queryByText("Clear print spooler", { selector: ".cell-title" })).toBeNull();
    await userEvent.click(within(screen.getByRole("group", { name: "System" })).getByRole("button", { name: "Windows" }));
    expect(router.state.location.search).toContain("os=windows");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/deploy/scripts?os=macos", <ScriptsPage />, "/deploy/scripts");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("opens New script from ?new=script", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/deploy/scripts?new=script", <ScriptsPage />, "/deploy/scripts");
    expect(await screen.findByRole("dialog", { name: "New script" })).toBeInTheDocument();
  });

  it("runs a script through the same signed-job dialog as today", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/deploy/scripts", <ScriptsPage />, "/deploy/scripts");
    const row = (await screen.findByText("Clean apt cache", { selector: ".cell-title" })).closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog", { name: /Run/ });
    await waitFor(() => expect(within(dialog).getByText(/Clean apt cache/)).toBeInTheDocument());
  });
});
