/**
 * Notifications on the form kit (redesign plan, Task 7, 2.8; Receta FO): a
 * section per group, Add rule opened from the URL (`?new=rule`, decision
 * D8), and the rules' on/off switches as the page's settings, held in one
 * draft and saved by the one save bar with the same PATCH the switch used to
 * send at once. On the fixture session.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../../fixtures/client";
import { renderPage } from "../../../test/renderPage";
import { NotificationAdminPage } from "../NotificationAdminPage";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

beforeEach(() => mockUseApi.mockReturnValue(fixtureClient()));

/** The fixture session with a spy on the rule's PATCH (the fixture's own answer behind it). */
function spyingRuleUpdate() {
  const registry = fixtureRegistry();
  const original = registry.updateNotificationRule as (...args: unknown[]) => unknown;
  const update = vi.fn((...args: unknown[]) => original(...args));
  mockUseApi.mockReturnValue(fixtureClient({ ...registry, updateNotificationRule: update as never }));
  return update;
}

async function rulesSection(): Promise<HTMLElement> {
  return (await screen.findByRole("heading", { name: "Rules" })).closest("section") as HTMLElement;
}

describe("notifications on the form kit", () => {
  it("opens Add rule from ?new=rule", async () => {
    renderPage("/notifications?new=rule", <NotificationAdminPage />);
    expect(await screen.findByRole("dialog", { name: "Add rule" })).toBeInTheDocument();
  });

  it("shows one save bar only while something changed, and Discard puts it back", async () => {
    renderPage("/notifications", <NotificationAdminPage />);
    const rules = await rulesSection();
    const first = (await within(rules).findAllByRole("switch"))[0] as HTMLElement;
    const before = first.getAttribute("aria-checked");
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    await userEvent.click(first);
    expect(first.getAttribute("aria-checked")).not.toBe(before);
    expect(screen.getAllByRole("region", { name: "Unsaved changes" })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    expect(first.getAttribute("aria-checked")).toBe(before);
  });

  it("asks before leaving with unsaved changes", async () => {
    const { router } = renderPage("/notifications", <NotificationAdminPage />, "/notifications");
    const rules = await rulesSection();
    await userEvent.click((await within(rules).findAllByRole("switch"))[0] as HTMLElement);
    // The page links only to itself (its tiles), so leave the way the rail does: to another route.
    act(() => {
      void router.navigate("/devices");
    });
    await screen.findByRole("dialog", { name: "Leave without saving?" });
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/notifications");
  });

  it("saves the switched rules with the PATCH the switch sent, and nothing before Save", async () => {
    const update = spyingRuleUpdate();
    renderPage("/notifications", <NotificationAdminPage />);
    const rules = await rulesSection();
    const first = (await within(rules).findAllByRole("switch"))[0] as HTMLElement;
    const next = first.getAttribute("aria-checked") !== "true";
    await userEvent.click(first);
    expect(update).not.toHaveBeenCalled();
    await userEvent.click(within(screen.getByRole("region", { name: "Unsaved changes" })).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update.mock.calls[0]?.[1]).toEqual({ active: next });
    await waitFor(() => expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull());
  });

  it("draws its groups as sections, with no primary button in the body", async () => {
    renderPage("/notifications", <NotificationAdminPage />);
    for (const name of ["Rules", "Channels"]) {
      expect(await screen.findByRole("heading", { name })).toBeInTheDocument();
    }
    await within(await rulesSection()).findAllByRole("switch");
    expect(document.querySelectorAll(".btn.primary")).toHaveLength(0);
  });
});
