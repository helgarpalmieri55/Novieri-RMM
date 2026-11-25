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

/**
 * General rulings 3 and 4 on a `?new=` dialog: Cancel with a change in the
 * form asks first, and staying keeps the change; leaving, then opening the
 * dialog again, starts from an empty form.
 */
async function guardsThenStartsEmpty(
  router: { state: { location: { search: string } } },
  title: string,
  search: string,
  change: (dialog: HTMLElement) => Promise<void>,
  holdsChange: (dialog: HTMLElement) => void,
  isEmpty: (dialog: HTMLElement) => void,
  reopen: string,
): Promise<void> {
  const dialog = await screen.findByRole("dialog", { name: title });
  await change(dialog);
  await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  const ask = await screen.findByRole("dialog", { name: "Leave without saving?" });
  // The question paints over the form it guards: both are `.modal` at one
  // z-index, so the later in the document is the one on top (fix round 2, C1).
  expect(dialog.compareDocumentPosition(ask) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(router.state.location.search).toBe(search);
  await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
  holdsChange(screen.getByRole("dialog", { name: title }));

  await userEvent.click(within(screen.getByRole("dialog", { name: title })).getByRole("button", { name: "Cancel" }));
  await userEvent.click(
    within(await screen.findByRole("dialog", { name: "Leave without saving?" })).getByRole("button", {
      name: "Leave without saving",
    }),
  );
  await waitFor(() => expect(screen.queryByRole("dialog", { name: title })).toBeNull());
  await userEvent.click(screen.getAllByRole("button", { name: reopen })[0]!);
  isEmpty(await screen.findByRole("dialog", { name: title }));
}

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

  it("draws no empty state while the Client chip waits for /me (general ruling 10)", async () => {
    // General ruling 10: with the chip set and `/me` unanswered the narrowed
    // reads are disabled (`ready` false), and a disabled query is not
    // "loading" in TanStack v5 -- the page must still draw loading, not empty.
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), me: () => new Promise<never>(() => {}) }));
    renderPage(`/deploy?client=${ANDES}`, <DeployPage />, "/deploy");
    expect((await screen.findAllByText("Loading…")).length).toBeGreaterThan(0);
    for (const empty of [
      "No deployments yet.",
      "Nothing matches these filters.",
    ]) {
      expect(screen.queryByText(empty)).toBeNull();
    }
  });

  it("opens each tile's rows under the same Client chip (general ruling 2)", async () => {
    // The tiles count the chip's client, so each opens that client's rows.
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage(`/deploy?client=${ANDES}`, <DeployPage />, "/deploy");
    await screen.findByRole("table", { name: "Deployments" });
    // The page's own links (the section nav) name pages, not counted rows.
    const hrefs = screen
      .getAllByRole("link")
      .filter((link) => link.closest("nav") === null)
      .map((link) => link.getAttribute("href") ?? "");
    expect(hrefs).toContain(`/deploy?client=${ANDES}`);
    expect(hrefs).toContain(`/deploy?state=week&client=${ANDES}`);
    expect(hrefs).toContain(`/deploy?state=complete&client=${ANDES}`);
    expect(hrefs).toContain(`/deploy?state=failed&client=${ANDES}`);
    const lists = hrefs.filter((href) => href === "/deploy" || href.startsWith("/deploy?"));
    expect(lists.filter((href) => !href.includes(`client=${ANDES}`))).toEqual([]);
  });

  it("asks before New deployment is left with a change, and opens it empty again (general rulings 3, 4)", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/deploy?new=deployment", <DeployPage />, "/deploy");
    await guardsThenStartsEmpty(
      router,
      "New deployment",
      "?new=deployment",
      (dialog) => userEvent.selectOptions(within(dialog).getByLabelText("Action"), "uninstall"),
      (dialog) => expect(within(dialog).getByLabelText("Action")).toHaveValue("uninstall"),
      (dialog) => expect(within(dialog).getByLabelText("Action")).toHaveValue("install"),
      "New deployment",
    );
    // Two dialogs, a week grid and typing: slow on a busy machine, not stuck.
  }, 20_000);

  it("draws no number and no claim in its tiles while the Client chip waits for /me (clarification 10a)", async () => {
    // A tile counts the narrowed read; before it answers there is nothing to
    // count, so no "0" and no "nothing has ever…": the kit's pending tile, the
    // absent mark ("n/a") and no sub (Tile pending).
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), me: () => new Promise<never>(() => {}) }));
    const { container } = renderPage(`/deploy?client=${ANDES}`, <DeployPage />, "/deploy");
    await screen.findAllByText("Loading…");
    const tiles = Array.from(container.querySelectorAll(".tile"));
    expect(tiles.length).toBe(4);
    for (const tile of tiles) {
      expect(tile.querySelector(".v")?.textContent).toBe("n/a");
      expect(tile.querySelector(".z")).toBeNull();
    }
  });

  it("draws no number and no claim in its tiles once the read they count has failed (clarification 10a)", async () => {
    // A failed read is no data either: `isPending` is false by then, and the
    // tiles used to count the empty list it left ("0", "nothing has ever…").
    // The kit's pending tile is the absent mark ("n/a") and no sub.
    const failed = () => Promise.reject(new Error("status 503"));
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listDeployments: failed }));
    const { container } = renderPage("/deploy", <DeployPage />, "/deploy");
    await screen.findAllByRole("alert");
    const tiles = Array.from(container.querySelectorAll(".tile"));
    expect(tiles.length).toBe(4);
    for (const tile of tiles) {
      expect(tile.querySelector(".v")?.textContent).toBe("n/a");
      expect(tile.querySelector(".z")).toBeNull();
    }
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

  it("asks before New package is left with a change, and opens it empty again (general rulings 3, 4)", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/deploy/packages?new=package", <PackagesPage />, "/deploy/packages");
    await guardsThenStartsEmpty(
      router,
      "New package",
      "?new=package",
      (dialog) => userEvent.type(within(dialog).getByLabelText("Name"), "7z"),
      (dialog) => expect(within(dialog).getByLabelText("Name")).toHaveValue("7z"),
      (dialog) => expect(within(dialog).getByLabelText("Name")).toHaveValue(""),
      "New package",
    );
    // Two dialogs, a week grid and typing: slow on a busy machine, not stuck.
  }, 20_000);

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

  it("asks before New script is left with a change, and opens it empty again (general rulings 3, 4)", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/deploy/scripts?new=script", <ScriptsPage />, "/deploy/scripts");
    await guardsThenStartsEmpty(
      router,
      "New script",
      "?new=script",
      (dialog) => userEvent.type(within(dialog).getByLabelText("Name"), "dns"),
      (dialog) => expect(within(dialog).getByLabelText("Name")).toHaveValue("dns"),
      (dialog) => expect(within(dialog).getByLabelText("Name")).toHaveValue(""),
      "New script",
    );
    // Two dialogs, a week grid and typing: slow on a busy machine, not stuck.
  }, 20_000);

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
