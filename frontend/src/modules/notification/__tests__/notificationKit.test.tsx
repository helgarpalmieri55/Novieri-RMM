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
import { ApiError } from "../../../lib/api";
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

  it("holds a channel's switch in the same draft as the rules', for the same one save bar", async () => {
    renderPage("/notifications", <NotificationAdminPage />);
    const rules = await rulesSection();
    const channels = (await screen.findByRole("heading", { name: "Channels" })).closest("section") as HTMLElement;
    await userEvent.click((await within(rules).findAllByRole("switch"))[0] as HTMLElement);
    await userEvent.click((await within(channels).findAllByRole("switch"))[0] as HTMLElement);
    expect(screen.getAllByRole("region", { name: "Unsaved changes" })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
  });

  it("draws its groups as sections, with no primary button in the body", async () => {
    renderPage("/notifications", <NotificationAdminPage />);
    for (const name of ["Rules", "Channels"]) {
      expect(await screen.findByRole("heading", { name })).toBeInTheDocument();
    }
    await within(await rulesSection()).findAllByRole("switch");
    expect(document.querySelectorAll(".btn.primary")).toHaveLength(0);
  });

  // FIX ROUND 1 (review): every PATCH goes out, then each list is read again
  // ONCE; a refetch between two PATCHes used to be adopted mid-save.
  it("saves every switched row, then reads the lists again once", async () => {
    const registry = fixtureRegistry();
    const listRules = vi.fn(registry.listNotificationRules as (...a: unknown[]) => unknown);
    mockUseApi.mockReturnValue(fixtureClient({ ...registry, listNotificationRules: listRules as never }));
    renderPage("/notifications", <NotificationAdminPage />);
    const rules = await rulesSection();
    const switches = await within(rules).findAllByRole("switch");
    await userEvent.click(switches[0] as HTMLElement);
    await userEvent.click(switches[1] as HTMLElement);
    const before = listRules.mock.calls.length;
    await userEvent.click(within(screen.getByRole("region", { name: "Unsaved changes" })).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull());
    expect(listRules.mock.calls.length - before).toBe(1);
  });

  it("switches a rule created while other switches were thrown", async () => {
    const { queryClient } = renderPage("/notifications", <NotificationAdminPage />);
    const rules = await rulesSection();
    await userEvent.click((await within(rules).findAllByRole("switch"))[0] as HTMLElement);
    const current = queryClient.getQueryData<{ id: string }[]>(["notification-rules"]) ?? [];
    const countBefore = current.length;
    expect(countBefore).toBeGreaterThan(0);
    act(() => {
      queryClient.setQueryData(["notification-rules"], [
        ...current,
        { ...(current[0] as object), id: "rule-new", event_type: "ticket.resolved", active: true },
      ]);
    });
    const count = within(rules).getAllByRole("switch").length;
    await waitFor(() => expect(within(rules).getAllByRole("switch").length).toBe(count + 1));
    const all = within(rules).getAllByRole("switch");
    const added = all[all.length - 1] as HTMLElement;
    expect(added).toHaveAttribute("aria-checked", "true");
    await userEvent.click(added);
    expect(added).toHaveAttribute("aria-checked", "false");
  });

  // General ruling 3: the Add rule form in `?new=rule` is covered by the page's
  // one guard, with the switches: Back asks, Cancel does not.
  it("asks before Back drops a half-written rule, and Cancel does not ask", async () => {
    const { router } = renderPage("/notifications", <NotificationAdminPage />);
    const rules = await rulesSection();
    await userEvent.click(await within(rules).findByRole("button", { name: "Add rule" }));
    const dialog = await screen.findByRole("dialog", { name: "Add rule" });
    await userEvent.selectOptions(within(dialog).getAllByRole("combobox")[0] as HTMLElement, within(dialog).getAllByRole("combobox")[0]?.querySelectorAll("option")[1]?.getAttribute("value") ?? "");
    // React reports the held Back's promise as an un-awaited suspension
    // (shell/formKit.test.tsx, "LEAVING IS A CLICK ON A LINK"); asserted, not tolerated.
    const heldBack = vi.spyOn(console, "error").mockImplementation(() => {});
    await act(() => router.navigate(-1));
    const leave = await screen.findByRole("dialog", { name: "Leave without saving?" });
    expect(heldBack.mock.calls.every(([message]) => String(message).includes("suspended inside an `act` scope"))).toBe(true);
    heldBack.mockRestore();
    await userEvent.click(within(leave).getByRole("button", { name: "Cancel" }));
    await userEvent.click(within(screen.getByRole("dialog", { name: "Add rule" })).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add rule" })).toBeNull());
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });

  // FIX ROUND 2 (re-review, rulings 4 and 8): the Add host form starts empty
  // on every open, and both Saves stay pressable, naming what is missing.
  it("opens Add host empty and without the error of a failed attempt", async () => {
    mockUseApi.mockReturnValue(
      fixtureClient({ ...fixtureRegistry(), createNotificationAllowlistEntry: () => Promise.reject(new ApiError(503, "")) }),
    );
    renderPage("/notifications", <NotificationAdminPage />);
    const section = (await screen.findByRole("heading", { name: "Allowlist" })).closest("section") as HTMLElement;
    await userEvent.click(await within(section).findByRole("button", { name: "Add host" }));
    let dialog = screen.getByRole("dialog", { name: "Add allowlist entry" });
    await userEvent.type(within(dialog).getByLabelText("Host"), "intranet.lan");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/unavailable/i);
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await userEvent.click(within(section).getByRole("button", { name: "Add host" }));
    dialog = screen.getByRole("dialog", { name: "Add allowlist entry" });
    expect(within(dialog).getByLabelText("Host")).toHaveValue("");
    expect(within(dialog).queryByRole("alert")).toBeNull();
  });

  it("keeps Add host's Save pressable and names the missing host", async () => {
    renderPage("/notifications", <NotificationAdminPage />);
    const section = (await screen.findByRole("heading", { name: "Allowlist" })).closest("section") as HTMLElement;
    await userEvent.click(await within(section).findByRole("button", { name: "Add host" }));
    const dialog = screen.getByRole("dialog", { name: "Add allowlist entry" });
    const save = within(dialog).getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await userEvent.click(save);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Host is required");
  });

  it("keeps Add rule's Save pressable and names the missing channel", async () => {
    renderPage("/notifications?new=rule", <NotificationAdminPage />);
    const dialog = await screen.findByRole("dialog", { name: "Add rule" });
    const save = within(dialog).getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await userEvent.click(save);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Channel is required");
  });

  // FIX ROUND 2, M2: Cancel on an untouched Add rule does not release the
  // page's one guard, which still holds the thrown switches.
  it("still asks before leaving with switches thrown after an untouched Add rule is cancelled", async () => {
    const { router } = renderPage("/notifications", <NotificationAdminPage />, "/notifications");
    const rules = await rulesSection();
    await userEvent.click((await within(rules).findAllByRole("switch"))[0] as HTMLElement);
    await userEvent.click(await within(rules).findByRole("button", { name: "Add rule" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Add rule" })).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add rule" })).toBeNull());
    act(() => {
      void router.navigate("/devices");
    });
    await screen.findByRole("dialog", { name: "Leave without saving?" });
    expect(router.state.location.pathname).toBe("/notifications");
  });

  /** The fixture session with a spy on one write (the fixture's own answer behind it). */
  function spyOnWrite(method: string) {
    const registry = fixtureRegistry() as Record<string, unknown>;
    const original = registry[method] as (...args: unknown[]) => unknown;
    const spy = vi.fn((...args: unknown[]) => original(...args));
    mockUseApi.mockReturnValue(fixtureClient({ ...registry, [method]: spy } as never));
    return spy;
  }

  // FIX ROUND 3 (general ruling 13): no write without a change.
  it("closes an untouched Edit channel on Save and sends nothing", async () => {
    const write = spyOnWrite("updateNotificationChannel");
    renderPage("/notifications", <NotificationAdminPage />);
    const channels = (await screen.findByRole("heading", { name: "Channels" })).closest("section") as HTMLElement;
    await userEvent.click((await within(channels).findAllByRole("button", { name: /^Edit/ }))[0] as HTMLElement);
    const dialog = screen.getByRole("dialog", { name: "Edit channel" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit channel" })).toBeNull());
    expect(write).not.toHaveBeenCalled();
  });
});
