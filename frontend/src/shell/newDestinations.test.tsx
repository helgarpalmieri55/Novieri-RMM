import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient } from "../fixtures/client";
import { ChangesPage } from "../modules/change/ChangesPage";
import { ClientDetailPage } from "../modules/clients/ClientDetailPage";
import { ClientsPage } from "../modules/clients/ClientsPage";
import { DocsPage } from "../modules/docs/DocsPage";
import { TicketListPage } from "../modules/service-desk/TicketListPage";
import { renderPage } from "../test/renderPage";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

// The fixture person holds every code in every client (`src/fixtures/core.ts`).
beforeEach(() => mockUseApi.mockReturnValue(fixtureClient()));

describe("every New entry is a real destination (redesign design §1.2, decision D8)", () => {
  it("Ticket opens New ticket on the ticket list", async () => {
    renderPage("/service-desk?new=ticket", <TicketListPage />);
    expect(await screen.findByRole("dialog", { name: "New ticket" })).toBeInTheDocument();
  });

  it("Client opens New client on the client list", async () => {
    renderPage("/clients?new=client", <ClientsPage />);
    expect(await screen.findByRole("dialog", { name: "New client" })).toBeInTheDocument();
  });

  it("Contact, with every client shown, first asks which client", async () => {
    renderPage("/clients?new=contact", <ClientsPage />);
    expect(await screen.findByRole("dialog", { name: "New contact" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Client" })).toBeInTheDocument();
  });

  it("Contact's Continue takes the question's place in the history, so Back does not ask again", async () => {
    const { router } = renderPage("/clients?new=contact", <ClientsPage />);
    await screen.findByRole("dialog", { name: "New contact" });
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(router.state.location.search).toContain("new=contact");
    expect(router.state.location.pathname).toMatch(/^\/clients\/./);
    expect(router.state.historyAction).toBe("REPLACE");
  });

  it("Contact, for a chosen client, opens Add contact on its contacts tab", async () => {
    renderPage(
      "/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=contacts&new=contact",
      <ClientDetailPage />,
      "/clients/:clientId",
    );
    expect(await screen.findByRole("dialog", { name: "Add a contact" })).toBeInTheDocument();
  });

  it("Document opens the article editor", async () => {
    renderPage("/docs?new=article", <DocsPage />);
    expect(await screen.findByRole("textbox", { name: "Title" })).toBeInTheDocument();
  });

  it("Change opens New change", async () => {
    renderPage("/changes?new=change", <ChangesPage />);
    expect(await screen.findByRole("dialog", { name: "New change" })).toBeInTheDocument();
  });
});
