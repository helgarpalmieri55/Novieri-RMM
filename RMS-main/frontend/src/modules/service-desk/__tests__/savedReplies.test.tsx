/** Saved replies on the service desk settings page. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Me } from "../../../lib/api";
import { ServiceDeskAdminPage } from "../ServiceDeskAdminPage";
import type { CannedResponseOut } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ADMIN: Me = {
  user_id: "admin-1",
  org_id: "o1",
  email: "a@x",
  display_name: "Admin",
  permissions: ["ticket.read", "ticket.write", "ticket.assign", "ticket.admin"],
};

const REPLY: CannedResponseOut = {
  id: "cr1",
  title: "Restart first",
  body: "Please restart and tell us.",
  created_by: "admin-1",
  created_at: "2026-09-01T00:00:00Z",
  updated_by: null,
  updated_at: null,
};

let api: Partial<ApiClient>;

function renderPage(me: Me = ADMIN) {
  api.me = vi.fn().mockResolvedValue(me);
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ServiceDeskAdminPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const panel = async () =>
  (await screen.findByRole("heading", { name: "Saved replies" })).closest(".panel") as HTMLElement;

beforeEach(() => {
  api = {
    listTicketQueues: vi.fn().mockResolvedValue([]),
    listTicketCategories: vi.fn().mockResolvedValue([]),
    listCannedResponses: vi.fn().mockResolvedValue([REPLY]),
    createCannedResponse: vi.fn().mockResolvedValue(REPLY),
    updateCannedResponse: vi.fn().mockResolvedValue(REPLY),
    deleteCannedResponse: vi.fn().mockResolvedValue(undefined),
  };
});

describe("saved replies", () => {
  it("lists the org's saved replies", async () => {
    renderPage();
    const box = await panel();
    expect(await within(box).findByText("Restart first")).toBeInTheDocument();
    expect(within(box).getByText("Please restart and tell us.")).toBeInTheDocument();
  });

  it("creates one with the typed name and text", async () => {
    const user = userEvent.setup();
    renderPage();
    const box = await panel();

    await user.click(await within(box).findByRole("button", { name: "New saved reply" }));
    const dialog = await screen.findByRole("dialog", { name: "New saved reply" });
    await user.type(within(dialog).getByLabelText("Name"), "  Mailbox full ");
    await user.type(within(dialog).getByLabelText("Reply text"), "Archive old mail.");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(api.createCannedResponse).toHaveBeenCalledWith({
      title: "Mailbox full",
      body: "Archive old mail.",
    });
  });

  it("names the missing field instead of doing nothing", async () => {
    const user = userEvent.setup();
    renderPage();
    const box = await panel();

    await user.click(await within(box).findByRole("button", { name: "New saved reply" }));
    const dialog = await screen.findByRole("dialog", { name: "New saved reply" });
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(within(dialog).getByRole("alert")).toHaveTextContent(/Name/);
    expect(api.createCannedResponse).not.toHaveBeenCalled();
  });

  it("edits one in place", async () => {
    const user = userEvent.setup();
    renderPage();
    const box = await panel();

    await user.click(await within(box).findByRole("button", { name: "Edit Restart first" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit saved reply" });
    await user.clear(within(dialog).getByLabelText("Reply text"));
    await user.type(within(dialog).getByLabelText("Reply text"), "Restart, then reply here.");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(api.updateCannedResponse).toHaveBeenCalledWith("cr1", {
      title: "Restart first",
      body: "Restart, then reply here.",
    });
  });

  it("asks before deleting, naming the reply", async () => {
    const user = userEvent.setup();
    renderPage();
    const box = await panel();

    await user.click(await within(box).findByRole("button", { name: "Delete Restart first" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete this saved reply" });
    expect(within(dialog).getByText("Restart first")).toBeInTheDocument();
    expect(api.deleteCannedResponse).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(api.deleteCannedResponse).toHaveBeenCalledWith("cr1");
  });

  it("shows no write controls without ticket.admin", async () => {
    renderPage({ ...ADMIN, permissions: ["ticket.read"] });
    const box = await panel();

    expect(await within(box).findByText("Restart first")).toBeInTheDocument();
    expect(within(box).queryByRole("button", { name: "New saved reply" })).toBeNull();
    expect(within(box).queryByRole("button", { name: /Edit|Delete/ })).toBeNull();
  });
});
