/**
 * Imaging on the list kit (redesign plan, Task 9, 4.6 to 4.8): the state
 * filters as chips in the URL, an empty list that says its cause, and every
 * create or edit dialog opened from the URL (`?new=`, `?edit=`). No Client
 * chip: imaging works in the client chosen at the top.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry, type FixtureMap } from "../../../fixtures/client";
import type { ApiClient, Me } from "../../../lib/api";
import { renderPage } from "../../../test/renderPage";
import { ImagesPage } from "../ImagesPage";
import { ProfilesPage } from "../ProfilesPage";
import { ProvisioningRunsPage } from "../ProvisioningRunsPage";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** The fixture session, with `codes` added on every client (the fixtures hold read codes only). */
function holding(codes: readonly string[]): () => Me {
  return () => {
    const me = (fixtureRegistry().me as () => Me)();
    const add = (list: readonly string[]) => [...new Set([...list, ...codes])];
    return {
      ...me,
      permissions: add(me.permissions),
      permissions_everywhere: add(me.permissions_everywhere ?? []),
      clients: (me.clients ?? []).map((client) => ({ ...client, permissions: add(client.permissions) })),
    };
  };
}

/** The fixture session, without `codes` anywhere. */
function lacking(codes: readonly string[]): () => Me {
  return () => {
    const me = (fixtureRegistry().me as () => Me)();
    const drop = (list: readonly string[]) => list.filter((code) => !codes.includes(code));
    return {
      ...me,
      permissions: drop(me.permissions),
      permissions_everywhere: drop(me.permissions_everywhere ?? []),
      clients: (me.clients ?? []).map((client) => ({ ...client, permissions: drop(client.permissions) })),
    };
  };
}

function api(over: FixtureMap = {}): ApiClient {
  return fixtureClient({ ...fixtureRegistry(), me: holding(["provisioning.run", "provisioning.manage"]), ...over });
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

describe("provisioning runs on the list kit (4.6)", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = renderPage("/provisioning?state=failed", <ProvisioningRunsPage />);
    const chips = await screen.findByRole("group", { name: "Filter runs by state" });
    expect(within(chips).getByRole("button", { name: "Failed" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: "Complete" }));
    expect(router.state.location.search).toContain("state=complete");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(api({ listProvisionRuns: () => [] }));
    const { router } = renderPage("/provisioning?state=failed", <ProvisioningRunsPage />);
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(router.state.location.search).toBe("");
    // The brief's sentence for a run history with nothing in it (redesign plan, Task 9, 4.6; fix round 1).
    expect(await screen.findByText("No computer has been built yet.")).toBeInTheDocument();
    // The head's button and the empty state's: one act, one name (fix round 1).
    expect(screen.getAllByRole("button", { name: "Build a computer" })).toHaveLength(2);
  });

  it("names one act once: the head's button and the empty state's both open the Build a computer dialog (fix round 1)", async () => {
    mockUseApi.mockReturnValue(api({ listProvisionRuns: () => [] }));
    const { router } = renderPage("/provisioning", <ProvisioningRunsPage />);
    await screen.findByText("No computer has been built yet.");
    const [head, empty] = screen.getAllByRole("button", { name: "Build a computer" });
    for (const button of [head, empty]) {
      await userEvent.click(button as HTMLElement);
      expect(router.state.location.search).toBe("?new=run");
      const dialog = await screen.findByRole("dialog", { name: "Build a computer" });
      await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
      await waitFor(() => expect(router.state.location.search).toBe(""));
    }
    expect(screen.queryByRole("button", { name: "Provision" })).toBeNull();
  });

  it("opens Build a computer from ?new=run", async () => {
    const { router } = renderPage("/provisioning?new=run", <ProvisioningRunsPage />);
    const dialog = await screen.findByRole("dialog", { name: "Build a computer" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });

  it("opens nothing from ?new=run for someone who may not start a run", async () => {
    mockUseApi.mockReturnValue(api({ me: lacking(["provisioning.run"]) }));
    renderPage("/provisioning?new=run", <ProvisioningRunsPage />);
    expect(await screen.findByRole("table", { name: "Imaging" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Build a computer" })).toBeNull();
  });
});

describe("OS images on the list kit (4.7)", () => {
  it("draws no figure and no claim in its tiles before the list answers (clarification 10a)", async () => {
    mockUseApi.mockReturnValue(api({ listOsImages: () => new Promise(() => {}) }));
    renderPage("/provisioning/images", <ImagesPage />);
    await expectTilesAbsent(3);
  });

  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = renderPage("/provisioning/images?state=inactive", <ImagesPage />);
    const chips = await screen.findByRole("group", { name: "Filter images by state" });
    expect(within(chips).getByRole("button", { name: "Not active" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: "Active" }));
    expect(router.state.location.search).toContain("state=active");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(api({ listOsImages: () => [] }));
    renderPage("/provisioning/images?state=active", <ImagesPage />);
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("opens New image from ?new=image only for someone who may manage images", async () => {
    const first = renderPage("/provisioning/images?new=image", <ImagesPage />);
    expect(await screen.findByRole("dialog", { name: "New OS image" })).toBeInTheDocument();
    first.unmount();
    mockUseApi.mockReturnValue(api({ me: lacking(["provisioning.manage"]) }));
    renderPage("/provisioning/images?new=image", <ImagesPage />);
    expect(await screen.findByRole("table", { name: "Images" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "New OS image" })).toBeNull();
  });

  it("asks before Cancel drops a typed name in New image (general ruling 3)", async () => {
    const { router } = renderPage("/provisioning/images?new=image", <ImagesPage />, "/provisioning/images");
    const dialog = await screen.findByRole("dialog", { name: "New OS image" });
    await userEvent.type(within(dialog).getByLabelText(/^Name/), "Win");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=image");
  });
});

describe("provisioning profiles on the list kit (4.8)", () => {
  it("draws no figure and no claim in its tiles before the list answers (clarification 10a)", async () => {
    mockUseApi.mockReturnValue(api({ listProvisionProfiles: () => new Promise(() => {}) }));
    renderPage("/provisioning/profiles", <ProfilesPage />);
    await expectTilesAbsent(3);
  });

  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = renderPage("/provisioning/profiles?enabled=false", <ProfilesPage />);
    const chips = await screen.findByRole("group", { name: "Filter profiles by state" });
    expect(within(chips).getByRole("button", { name: "Disabled" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: "Enabled" }));
    expect(router.state.location.search).toContain("enabled=true");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(api({ listProvisionProfiles: () => [] }));
    renderPage("/provisioning/profiles?enabled=true", <ProfilesPage />);
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("opens New profile from ?new=profile", async () => {
    renderPage("/provisioning/profiles?new=profile", <ProfilesPage />);
    const dialog = await screen.findByRole("dialog", { name: "New profile" });
    expect(within(dialog).getByLabelText(/^Name/)).toHaveValue("");
  });

  it("opens a profile's editor from ?edit=<id>, seeded with that profile", async () => {
    const { router } = renderPage("/provisioning/profiles", <ProfilesPage />);
    const row = (await screen.findByText("Workstation standard")).closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Edit" }));
    expect(router.state.location.search).toBe("?edit=prof-workstation-std");
    const dialog = await screen.findByRole("dialog", { name: "Edit profile" });
    expect(within(dialog).getByLabelText(/^Name/)).toHaveValue("Workstation standard");
    expect(within(dialog).getByDisplayValue("corp.toeshee.example")).toBeInTheDocument();
    expect(within(dialog).getByDisplayValue("svc-join")).toBeInTheDocument();
  });

  it("closes an editor its Edit opened by going Back, so Back afterwards does not reopen it (useEditParam)", async () => {
    // Written through `useUrlFilters`, Cancel pushed a second entry and the
    // next Back landed on `?edit=` again, reopening the editor (O4 review,
    // item 7). The kit's `useEditParam` closes by undoing the opening.
    const { router } = renderPage("/provisioning/profiles", <ProfilesPage />, "/provisioning/profiles");
    const row = (await screen.findByText("Workstation standard")).closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit profile" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    await act(() => router.navigate(-1));
    expect(router.state.location.search).toBe("");
    expect(screen.queryByRole("dialog", { name: "Edit profile" })).toBeNull();
  });

  it("asks before Cancel drops a change in a profile's editor (general ruling 3)", async () => {
    const { router } = renderPage("/provisioning/profiles?edit=prof-workstation-std", <ProfilesPage />, "/provisioning/profiles");
    const dialog = await screen.findByRole("dialog", { name: "Edit profile" });
    await userEvent.type(within(dialog).getByLabelText(/^Name/), "x");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?edit=prof-workstation-std");
  });
});
