/**
 * The off-site copy page on the form kit (redesign plan, Task 9, 4.4; Receta
 * FO): Add off-site storage from `?new=target`, and one storage edited at a
 * time on the page (`?edit=<id>`) with the console's one save bar, the
 * question before leaving, and the body the edit dialog sent.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry, type FixtureMap } from "../../../fixtures/client";
import type { ApiClient, Me } from "../../../lib/api";
import { renderPage } from "../../../test/renderPage";
import { OffsitePage } from "../OffsitePage";
import type { OffsiteTarget } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const TARGET = "offsite-wasabi"; // the fixture's one storage, "Wasabi (Amsterdam)"

/** The fixture session, given the off-site code (kept explicit though the fixture catalogue now holds it). */
function managing(): Me {
  const me = (fixtureRegistry().me as () => Me)();
  return { ...me, permissions: [...me.permissions, "backup.offsite.manage"] };
}

/** The fixture session without the off-site code (the fixture catalogue holds it since kit batch 1, item 7). */
function notManaging(): Me {
  const me = (fixtureRegistry().me as () => Me)();
  return { ...me, permissions: me.permissions.filter((code) => code !== "backup.offsite.manage") };
}

function api(over: FixtureMap = {}): ApiClient {
  return fixtureClient({ ...fixtureRegistry(), me: managing, ...over });
}

function page(query = `?edit=${TARGET}`) {
  return renderPage(`/backup/offsite${query}`, <OffsitePage />, "/backup/offsite");
}

async function editor(): Promise<HTMLElement> {
  return (await screen.findByRole("button", { name: "Edit Wasabi (Amsterdam)" })).closest("section") as HTMLElement;
}

beforeEach(() => mockUseApi.mockReturnValue(api()));

describe("the off-site copy page on the form kit (4.4)", () => {
  it("shows one save bar only while something changed, and Discard puts it back", async () => {
    page();
    const form = await editor();
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    await userEvent.type(within(form).getByLabelText("Name"), "x");
    expect(screen.getByRole("region", { name: "Unsaved changes" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    expect(within(form).getByLabelText("Name")).toHaveValue("Wasabi (Amsterdam)");
  });

  it("counts a typed key as a change, though the keys are never compared with the server", async () => {
    page();
    const form = await editor();
    await userEvent.type(within(form).getByLabelText("Access key"), "AKIA");
    expect(screen.getByRole("region", { name: "Unsaved changes" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(within(form).getByLabelText("Access key")).toHaveValue("");
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
  });

  it("asks before leaving with unsaved changes", async () => {
    const { router } = page();
    const form = await editor();
    await userEvent.type(within(form).getByLabelText("Name"), "x");
    // Leave as a person does: through a link (the section menu's).
    await userEvent.click(screen.getByRole("link", { name: "Backup points" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/backup/offsite");
  });

  it("asks before closing the editor with unsaved changes, and closes a clean one at once", async () => {
    const { router } = page();
    const form = await editor();
    await userEvent.type(within(form).getByLabelText("Bucket"), "x");
    await userEvent.click(within(form).getByRole("button", { name: "Close" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe(`?edit=${TARGET}`);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    await userEvent.click(within(form).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    expect(screen.queryByRole("button", { name: "Edit Wasabi (Amsterdam)" })).toBeNull();
  });

  it("opens a storage's editor from its Edit, in the URL", async () => {
    const { router } = page("");
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    expect(router.state.location.search).toBe(`?edit=${TARGET}`);
    expect(await editor()).toBeInTheDocument();
  });

  it("saves the same body the edit dialog sent, and says Saved", async () => {
    // A server that keeps what it is sent, as the real one does: the list read
    // after the save answers the stored storage.
    let stored = (fixtureRegistry().listOffsiteTargets as () => OffsiteTarget[])()[0] as OffsiteTarget;
    const updateOffsiteTarget = vi.fn((id: string, body: Partial<OffsiteTarget>) => {
      stored = { ...stored, ...body, id } as OffsiteTarget;
      return stored;
    });
    mockUseApi.mockReturnValue(api({ updateOffsiteTarget, listOffsiteTargets: () => [stored] } as FixtureMap));
    page();
    const form = await editor();
    const lock = within(form).getByLabelText("Lock each backup for (days)");
    await userEvent.clear(lock);
    await userEvent.type(lock, "45");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(updateOffsiteTarget).toHaveBeenCalledTimes(1));
    expect(updateOffsiteTarget).toHaveBeenCalledWith(TARGET, {
      name: "Wasabi (Amsterdam)",
      endpoint: "https://s3.eu-central-2.wasabisys.com",
      region: "eu-central-2",
      bucket: "toeshee-novieri-offsite",
      prefix: "novieri/",
      addressing: "virtual",
      lock_mode: "compliance",
      immutable_days: 45,
      retention_gfs: { daily: 14, weekly: 8, monthly: 12 },
      max_mbps: 50,
      access_key_id: null,
      secret_access_key: null,
    });
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("keeps the change when the save fails, and says why in the bar", async () => {
    const updateOffsiteTarget = vi.fn(() => Promise.reject(new Error("The bucket does not answer.")));
    mockUseApi.mockReturnValue(api({ updateOffsiteTarget } as unknown as FixtureMap));
    page();
    const form = await editor();
    await userEvent.type(within(form).getByLabelText("Region"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    const bar = screen.getByRole("region", { name: "Unsaved changes" });
    expect(await within(bar).findByRole("alert")).toBeInTheDocument();
    expect(within(form).getByLabelText("Region")).toHaveValue("eu-central-2x");
  });

  it("opens Add off-site storage from ?new=target", async () => {
    const { router } = page("?new=target");
    const dialog = await screen.findByRole("dialog", { name: "Add off-site storage" });
    expect(within(dialog).getByLabelText("Bucket")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });

  it("opens neither the dialog nor an editor for someone who may not manage storage", async () => {
    // A session without `backup.offsite.manage` (the fixture session holds it since kit batch 1, item 7).
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), me: notManaging }));
    page(`?edit=${TARGET}&new=target`);
    expect(await screen.findByText("Wasabi (Amsterdam)")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Add off-site storage" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit Wasabi (Amsterdam)" })).toBeNull();
  });

  it("asks before Cancel drops a change in Add off-site storage, and closes after a save without asking (general ruling 3)", async () => {
    const createOffsiteTarget = vi.fn((body: Partial<OffsiteTarget>) => ({ ...(fixtureRegistry().listOffsiteTargets as () => OffsiteTarget[])()[0], ...body, id: "new-target" }));
    mockUseApi.mockReturnValue(api({ createOffsiteTarget } as unknown as FixtureMap));
    const { router } = page("?new=target");
    let dialog = await screen.findByRole("dialog", { name: "Add off-site storage" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "NAS");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=target");
    await userEvent.click(screen.getByRole("button", { name: "Leave without saving" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    // Opened again, it starts empty (general ruling 4), and a save closes it without asking.
    await act(() => router.navigate("/backup/offsite?new=target"));
    dialog = await screen.findByRole("dialog", { name: "Add off-site storage" });
    expect(within(dialog).getByLabelText("Name")).toHaveValue("");
    await userEvent.type(within(dialog).getByLabelText("Name"), "NAS");
    await userEvent.type(within(dialog).getByLabelText("Bucket"), "acme");
    await userEvent.type(within(dialog).getByLabelText("Access key"), "AK");
    await userEvent.type(within(dialog).getByLabelText("Secret key"), "SK");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(createOffsiteTarget).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  }, 15_000);
});
