/**
 * No write without a change (general ruling 13; wave O2, fix round 3): an
 * edit dialog's Save on an untouched record closes the dialog and sends
 * nothing, because a PATCH that changes nothing writes a false audit entry.
 * Save stays enabled (ruling 8). On the fixture session.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../../fixtures/client";
import { renderPage } from "../../../test/renderPage";
import { ServiceDeskAdminPage } from "../ServiceDeskAdminPage";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** The fixture session with a spy on one write (the fixture's own answer behind it). */
function spyOnWrite(method: string) {
  const registry = fixtureRegistry() as Record<string, unknown>;
  const original = registry[method] as (...args: unknown[]) => unknown;
  const spy = vi.fn((...args: unknown[]) => original(...args));
  mockUseApi.mockReturnValue(fixtureClient({ ...registry, [method]: spy } as never));
  return spy;
}

describe("an untouched edit sends nothing", () => {
  // Queues and categories share one edit dialog (`EditRowDialog`).
  it.each([
    ["queue", "Queues", "Edit queue", "updateTicketQueue"],
    ["category", "Categories", "Edit category", "updateTicketCategory"],
  ] as const)("closes an untouched Edit %s on Save", async (_kind, heading, title, method) => {
    const write = spyOnWrite(method);
    renderPage("/service-desk/admin", <ServiceDeskAdminPage />);
    const section = (await screen.findByRole("heading", { name: heading })).closest("section") as HTMLElement;
    await userEvent.click((await within(section).findAllByRole("button", { name: "Edit" }))[0] as HTMLElement);
    const dialog = screen.getByRole("dialog", { name: title });
    const save = within(dialog).getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await userEvent.click(save);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: title })).toBeNull());
    expect(write).not.toHaveBeenCalled();
  });

  it("closes an untouched Edit saved reply on Save", async () => {
    const write = spyOnWrite("updateCannedResponse");
    renderPage("/service-desk/admin", <ServiceDeskAdminPage />);
    const section = (await screen.findByRole("heading", { name: "Saved replies" })).closest("section") as HTMLElement;
    await userEvent.click((await within(section).findAllByRole("button", { name: /^Edit / }))[0] as HTMLElement);
    const dialog = screen.getByRole("dialog", { name: "Edit saved reply" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit saved reply" })).toBeNull());
    expect(write).not.toHaveBeenCalled();
  });

  it("still sends a changed category, with the same body as before", async () => {
    const write = spyOnWrite("updateTicketCategory");
    renderPage("/service-desk/admin", <ServiceDeskAdminPage />);
    const section = (await screen.findByRole("heading", { name: "Categories" })).closest("section") as HTMLElement;
    await userEvent.click((await within(section).findAllByRole("button", { name: "Edit" }))[0] as HTMLElement);
    const dialog = screen.getByRole("dialog", { name: "Edit category" });
    await userEvent.click(within(dialog).getByLabelText("Category active"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(write).toHaveBeenCalledTimes(1));
    expect(Object.keys(write.mock.calls[0]?.[1] as object).sort()).toEqual(["active", "name"]);
  });
});
