/**
 * The backup lists on the list kit (redesign plan, Task 9, 4.1 to 4.3; Receta
 * L): filters in the URL, an empty list that says its cause, the create
 * dialogs opened from `?new=`, and the Client chip narrowing the list's READ
 * (decision D6) -- asserted on the narrowed double's own spy, with nothing
 * read un-narrowed (`recordingNarrowing`, src/test/narrowing.ts).
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../../fixtures/client";
import type { ApiClient } from "../../../lib/api";
import { recordingNarrowing } from "../../../test/narrowing";
import { renderPage } from "../../../test/renderPage";
import { BackupPointsPage } from "../BackupPointsPage";
import { BackupPolicyEditor } from "../BackupPolicyEditor";
import { CoveragePage } from "../CoveragePage";
import { RepositoriesPage } from "../RepositoriesPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** The fixtures, with some answers replaced. */
function withFixtures(over: Partial<Record<keyof ApiClient, unknown>> = {}): ApiClient {
  return fixtureClient({ ...fixtureRegistry(), ...over } as ReturnType<typeof fixtureRegistry>);
}

beforeEach(() => mockUseApi.mockReturnValue(fixtureClient()));

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

describe("backup points on the list kit (4.1)", () => {
  /**
   * What the page reads on the console's own API, un-narrowed, and why each
   * is not the list: the tiles' fleet report (`getBackupReport`), the names of
   * the rows' computers and repositories and the run dialog's picker, which a
   * write uses (`listDevices`, `listBackupRepositories`), and the readiness
   * check above the list (`listBackupPolicies`). The points themselves are
   * never among them.
   */
  const CONSOLE = ["getBackupReport", "listDevices", "listBackupRepositories", "listBackupPolicies"];

  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = renderPage("/backup?verify=pending", <BackupPointsPage />);
    const chips = await screen.findByRole("group", { name: "Filter backup points" });
    expect(within(chips).getByRole("button", { name: /Awaiting verification/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: /All backup points/ }));
    expect(router.state.location.search).not.toContain("verify=");
    await userEvent.click(within(chips).getByRole("button", { name: /Awaiting verification/ }));
    expect(router.state.location.search).toContain("verify=pending");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(withFixtures({ listBackupPoints: () => [] }));
    const { router } = renderPage(`/backup?verify=pending`, <BackupPointsPage />);
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(router.state.location.search).toBe("");
    // With no filter, the list says nothing exists yet, and offers the run.
    expect(await screen.findByText("No backup points recorded yet.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Run backup" })).toHaveLength(2);
  });

  it("narrows the points to one computer with the Device chip", async () => {
    const { router } = renderPage("/backup", <BackupPointsPage />);
    const device = await screen.findByRole("combobox", { name: "Device" });
    const table = await screen.findByRole("table", { name: "Restore points" });
    const before = within(table).getAllByRole("row").length;
    const first = within(device).getAllByRole("option")[1] as HTMLOptionElement;
    await userEvent.selectOptions(device, first.value);
    expect(router.state.location.search).toBe(`?device=${first.value}`);
    expect(within(screen.getByRole("table", { name: "Restore points" })).getAllByRole("row").length).toBeLessThan(before);
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    const narrowing = recordingNarrowing(fixtureClient(), { console: CONSOLE });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/backup?client=${ANDES}`, <BackupPointsPage />);
    expect(await screen.findByRole("table", { name: "Restore points" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listBackupPoints).toHaveBeenCalledWith();
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("opens Run backup from ?new=run", async () => {
    const { router } = renderPage("/backup?new=run", <BackupPointsPage />);
    const dialog = await screen.findByRole("dialog", { name: "Run backup" });
    expect(within(dialog).getByLabelText("Device")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    expect(screen.queryByRole("dialog", { name: "Run backup" })).toBeNull();
  });

  it("opens nothing from ?new=run for someone who may not run a backup", async () => {
    const registry = fixtureRegistry();
    const me = registry.me as () => Awaited<ReturnType<ApiClient["me"]>>;
    mockUseApi.mockReturnValue(
      withFixtures({ me: () => ({ ...me(), permissions: me().permissions.filter((code) => code !== "backup.run") }) }),
    );
    renderPage("/backup?new=run", <BackupPointsPage />);
    expect(await screen.findByRole("table", { name: "Restore points" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Run backup" })).toBeNull();
  });

  it("draws loading, not an empty list, while /me has not said whose the chip's client is (general ruling 10)", async () => {
    // `/me` never answers: the narrowed read is held back, and the page must
    // not say "nothing matches" or "nothing yet" about a read it never made.
    mockUseApi.mockReturnValue(withFixtures({ me: () => new Promise(() => {}) }));
    renderPage(`/backup?client=${ANDES}`, <BackupPointsPage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
    expect(screen.queryByText("No backup points recorded yet.")).toBeNull();
  });
});

describe("backup repositories on the list kit (4.2)", () => {
  it("draws no figure and no claim in its tiles before the list answers (clarification 10a)", async () => {
    mockUseApi.mockReturnValue(withFixtures({ listBackupRepositories: () => new Promise(() => {}) }));
    renderPage("/backup/repositories", <RepositoriesPage />);
    await expectTilesAbsent(3);
  });

  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = renderPage("/backup/repositories?state=retired", <RepositoriesPage />);
    const chips = await screen.findByRole("group", { name: "Filter repositories by state" });
    expect(within(chips).getByRole("button", { name: /Retired/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: /Active/ }));
    expect(router.state.location.search).toContain("state=active");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(
      withFixtures({ listBackupRepositories: () => (fixtureRegistry().listBackupRepositories as () => { state: string }[])().filter((repo) => repo.state === "active") }),
    );
    renderPage("/backup/repositories?state=retired", <RepositoriesPage />);
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("counts each state over the whole list, the chips adding up to it", async () => {
    renderPage("/backup/repositories", <RepositoriesPage />);
    const chips = await screen.findByRole("group", { name: "Filter repositories by state" });
    const count = (name: RegExp) => Number(within(within(chips).getByRole("button", { name })).getByText(/^\d+$/).textContent);
    await waitFor(() => expect(count(/All repositories/)).toBeGreaterThan(0));
    expect(count(/Active/) + count(/Retired/)).toBe(count(/All repositories/));
  });

  it("opens New repository from ?new=repository", async () => {
    const { router } = renderPage("/backup/repositories?new=repository", <RepositoriesPage />);
    const dialog = await screen.findByRole("dialog", { name: "New repository" });
    expect(within(dialog).getByLabelText("Name")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });

  it("asks before Cancel drops a typed name in New repository (general ruling 3)", async () => {
    const { router } = renderPage("/backup/repositories?new=repository", <RepositoriesPage />, "/backup/repositories");
    const dialog = await screen.findByRole("dialog", { name: "New repository" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "Vault");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=repository");
  });
});

describe("backup coverage on the list kit (4.3)", () => {
  it("draws no figure and no claim in its tiles before the report answers (clarification 10a)", async () => {
    mockUseApi.mockReturnValue(withFixtures({ runReport: () => new Promise(() => {}) }));
    renderPage("/backup/coverage", <CoveragePage />);
    await expectTilesAbsent(4);
  });

  /** The checks above the list (`BackupReadiness`): the page's own, under the top bar's choice, never the report. */
  const CONSOLE = ["listBackupRepositories", "listBackupPolicies", "listDevices"];

  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = renderPage("/backup/coverage?coverage=stale", <CoveragePage />);
    const chips = await screen.findByRole("group", { name: "Show devices" });
    expect(within(chips).getByRole("button", { name: /Out of date/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: /Failing/ }));
    expect(router.state.location.search).toContain("coverage=failing");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(
      withFixtures({ runReport: () => ({ meta: {}, totals: { covered: 1 }, rows: [{ device_id: "d1", hostname: "FS-01", coverage: "covered" }] }) }),
    );
    renderPage("/backup/coverage?coverage=stale", <CoveragePage />);
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("counts each verdict with the report's totals, the chips adding up to All", async () => {
    renderPage("/backup/coverage", <CoveragePage />);
    const chips = await screen.findByRole("group", { name: "Show devices" });
    const count = (name: RegExp) => Number(within(within(chips).getByRole("button", { name })).getByText(/^\d+$/).textContent);
    await waitFor(() => expect(count(/All devices/)).toBeGreaterThan(0));
    const verdicts = [/Protected/, /Out of date/, /Failing/, /Not protected/].map(count);
    expect(verdicts.reduce((sum, value) => sum + value, 0)).toBe(count(/All devices/));
    const report = await fixtureClient().runReport("backup-coverage", { limit: "500" });
    // ... which is the report's own count of every device it matched.
    expect(count(/All devices/)).toBe(report.totals["devices"]);
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    const narrowing = recordingNarrowing(fixtureClient(), { console: CONSOLE });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/backup/coverage?client=${ANDES}`, <CoveragePage />);
    expect(await screen.findByRole("table", { name: "Coverage" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).runReport).toHaveBeenCalledWith("backup-coverage", { limit: "500" });
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("draws loading, not an empty list, while /me has not said whose the chip's client is (general ruling 10)", async () => {
    // `/me` never answers: the narrowed read is held back, and the page must
    // not say "nothing matches" or "nothing yet" about a read it never made.
    mockUseApi.mockReturnValue(withFixtures({ me: () => new Promise(() => {}) }));
    renderPage(`/backup/coverage?client=${ANDES}`, <CoveragePage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("Nothing matches these filters.")).toBeNull();
    expect(screen.queryByText("No devices yet.")).toBeNull();
  });

  it("links each tile with the chip's client, so a tile opens the rows it counted (general ruling 2)", async () => {
    renderPage(`/backup/coverage?client=${ANDES}`, <CoveragePage />);
    await screen.findByRole("table", { name: "Coverage" });
    const hrefs = [...document.querySelectorAll(".stat-card")].map((tile) => tile.getAttribute("href"));
    expect(hrefs).toEqual(
      ["covered", "stale", "failing", "unprotected"].map((verdict) => `/backup/coverage?coverage=${verdict}&client=${ANDES}`),
    );
  });
});

describe("backup policies on the kit (the policies page)", () => {
  it("draws no figure and no claim in its tiles before the list answers (clarification 10a)", async () => {
    mockUseApi.mockReturnValue(withFixtures({ listBackupPolicies: () => new Promise(() => {}) }));
    renderPage("/backup/policies", <BackupPolicyEditor />);
    await expectTilesAbsent(3);
  });

  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = renderPage("/backup/policies?enabled=false", <BackupPolicyEditor />);
    const chips = await screen.findByRole("group", { name: "Filter policies by state" });
    expect(within(chips).getByRole("button", { name: /Disabled/ })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: /^Enabled/ }));
    expect(router.state.location.search).toContain("enabled=true");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(
      withFixtures({ listBackupPolicies: () => (fixtureRegistry().listBackupPolicies as () => { enabled: boolean }[])().filter((policy) => policy.enabled) }),
    );
    renderPage("/backup/policies?enabled=false", <BackupPolicyEditor />);
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("opens New policy from ?new=policy", async () => {
    renderPage("/backup/policies?new=policy", <BackupPolicyEditor />, "/backup/policies");
    expect(await screen.findByRole("dialog", { name: "New policy" })).toBeInTheDocument();
  });

  it("opens a policy's editor from ?edit=<id>, with that policy's own values", async () => {
    const { router } = renderPage("/backup/policies", <BackupPolicyEditor />, "/backup/policies");
    const row = (await screen.findAllByRole("row")).find((tr) => tr.textContent?.includes("Legacy NAS")) as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Edit" }));
    expect(router.state.location.search).toBe("?edit=pol-pilot");
    const dialog = await screen.findByRole("dialog", { name: "Edit policy" });
    expect(within(dialog).getByRole("checkbox")).not.toBeChecked();
  });

  it("closes an editor its Edit opened by going Back, so Back afterwards does not reopen it (useEditParam)", async () => {
    // Written through `useUrlFilters`, Cancel pushed a second entry and the
    // next Back landed on `?edit=` again, reopening the editor (O4 review,
    // item 7). The kit's `useEditParam` closes by undoing the opening.
    const { router } = renderPage("/backup/policies", <BackupPolicyEditor />, "/backup/policies");
    const row = (await screen.findAllByRole("row")).find((tr) => tr.textContent?.includes("Legacy NAS")) as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit policy" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    await act(() => router.navigate(-1));
    expect(router.state.location.search).toBe("");
    expect(screen.queryByRole("dialog", { name: "Edit policy" })).toBeNull();
  });

  it("asks before Cancel drops a change, and closes an unchanged editor at once", async () => {
    const { router } = renderPage("/backup/policies?edit=pol-pilot", <BackupPolicyEditor />, "/backup/policies");
    const dialog = await screen.findByRole("dialog", { name: "Edit policy" });
    await userEvent.click(within(dialog).getByRole("checkbox"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?edit=pol-pilot");
    await userEvent.click(screen.getByRole("button", { name: "Leave without saving" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    expect(screen.queryByRole("dialog", { name: "Edit policy" })).toBeNull();
  });

  it("closes after a save without asking", async () => {
    const upsertBackupPolicy = vi.fn((fixtureRegistry().upsertBackupPolicy as (...args: unknown[]) => unknown));
    mockUseApi.mockReturnValue(withFixtures({ upsertBackupPolicy }));
    // The servers' policy: its repository is active, so Save has one to send.
    const { router } = renderPage("/backup/policies?edit=pol-servers", <BackupPolicyEditor />, "/backup/policies");
    const dialog = await screen.findByRole("dialog", { name: "Edit policy" });
    await userEvent.click(within(dialog).getByRole("checkbox"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    expect(upsertBackupPolicy).toHaveBeenCalledWith(expect.objectContaining({ device_group_id: "grp-servers", enabled: false }));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });
});
