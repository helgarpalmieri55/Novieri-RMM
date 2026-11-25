/**
 * Each mailbox's settings on the form kit (redesign plan, Task 7, 2.7; Receta
 * FO): a section per mailbox, ONE draft, ONE save bar and ONE guard for the
 * page; the save is the mailbox's own PATCH with only what changed. On the
 * fixture session (two mailboxes).
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../../fixtures/client";
import type { ApiClient } from "../../../lib/api";
import { renderPage } from "../../../test/renderPage";
import { EmailIntakeAdminPage } from "../EmailIntakeAdminPage";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

let api: ApiClient;
beforeEach(() => {
  api = fixtureClient();
  mockUseApi.mockReturnValue(api);
});

/** The fixture session with a spy on the mailbox's PATCH (the fixture's own answer behind it). */
function spyingUpdate() {
  const registry = fixtureRegistry();
  const original = registry.updateEmailMailbox as (...args: unknown[]) => unknown;
  const update = vi.fn((...args: unknown[]) => original(...args));
  mockUseApi.mockReturnValue(fixtureClient({ ...registry, updateEmailMailbox: update as never }));
  return update;
}

/** The settings section of one mailbox, by its address. */
async function section(address: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: `How ${address} files tickets` });
  return heading.closest("section") as HTMLElement;
}

describe("a mailbox's settings", () => {
  it("shows one save bar only while something changed, and Discard puts it back", async () => {
    renderPage("/email-intake", <EmailIntakeAdminPage />);
    const support = await section("support@toeshee.example");
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    const window = within(support).getByLabelText("Reopen window (days)");
    const before = (window as HTMLInputElement).value;
    await userEvent.clear(window);
    await userEvent.type(window, "30");
    expect(screen.getAllByRole("region", { name: "Unsaved changes" })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    expect(within(support).getByLabelText("Reopen window (days)")).toHaveValue(before);
  });

  it("asks before leaving with unsaved changes", async () => {
    const { router } = renderPage("/email-intake", <EmailIntakeAdminPage />, "/email-intake");
    const support = await section("support@toeshee.example");
    await userEvent.type(within(support).getByLabelText("Reopen window (days)"), "0");
    // Leave as a person does: through a link on the page (a message's ticket).
    const ticket = (await screen.findAllByRole("link")).find((link) =>
      link.getAttribute("href")?.startsWith("/service-desk/tickets/"),
    ) as HTMLElement;
    await userEvent.click(ticket);
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/email-intake");
  });

  it("saves two mailboxes' changes with one Save, each with only what changed", async () => {
    const update = spyingUpdate();
    renderPage("/email-intake", <EmailIntakeAdminPage />);
    const support = await section("support@toeshee.example");
    const legacy = await section("helpdesk@legacy.toeshee.example");
    await userEvent.clear(within(support).getByLabelText("Reopen window (days)"));
    await userEvent.type(within(support).getByLabelText("Reopen window (days)"), "30");
    await userEvent.clear(within(legacy).getByLabelText("Reopen window (days)"));
    await userEvent.type(within(legacy).getByLabelText("Reopen window (days)"), "45");
    await userEvent.click(within(screen.getByRole("region", { name: "Unsaved changes" })).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(2));
    for (const [, body] of update.mock.calls) {
      expect(Object.keys(body as object)).toEqual(["reopen_window_days"]);
    }
    await waitFor(() => expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull());
  });

  it("sends nothing and says why for a window the server would refuse", async () => {
    const update = spyingUpdate();
    renderPage("/email-intake", <EmailIntakeAdminPage />);
    const support = await section("support@toeshee.example");
    await userEvent.clear(within(support).getByLabelText("Reopen window (days)"));
    await userEvent.type(within(support).getByLabelText("Reopen window (days)"), "0");
    const bar = screen.getByRole("region", { name: "Unsaved changes" });
    await userEvent.click(within(bar).getByRole("button", { name: "Save" }));
    expect(within(bar).getByRole("alert")).toHaveTextContent("support@toeshee.example");
    expect(update).not.toHaveBeenCalled();
  });

  it("draws no settings without a mailbox", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listEmailMailboxes: () => [] }));
    renderPage("/email-intake", <EmailIntakeAdminPage />);
    expect(await screen.findByRole("heading", { level: 1, name: "Email intake" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("heading", { name: /files tickets$/ })).toBeNull());
  });
});
