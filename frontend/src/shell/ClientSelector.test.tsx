/**
 * The top bar's client selector (phase 3 design §1.1): "All my clients", then
 * the person's clients from /me by name, Unassigned last. A choice is stored
 * (`itms.client`) and narrows every request from then on.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import type { ApiClient, Me, MeClient } from "../lib/api";
import { CLIENT_STORAGE_KEY, selectedClient } from "../lib/clientChoice";
import { ClientSelector } from "./ClientSelector";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";
const INTERNAL = "9f8e7d6c-5b4a-4321-8fed-cba987654303";
const UNASSIGNED = "1a2b3c4d-5e6f-4789-8abc-def012345604";

function client(id: string, name: string, kind: MeClient["kind"] = "customer"): MeClient {
  return { id, name, kind, enabled_modules: [], permissions: ["devices.read"] };
}

const ME: Me = {
  user_id: "u1",
  org_id: INTERNAL,
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["devices.read"],
  clients: [
    client(ZETA, "Zeta Foods"),
    client(UNASSIGNED, "Unassigned", "unassigned"),
    client(INTERNAL, "Toeshee", "internal"),
    client(ACME, "Acme Dental"),
  ],
};

function renderSelector(me: Me = ME) {
  const meFn = vi.fn().mockResolvedValue(me);
  mockUseApi.mockReturnValue({ me: meFn } as unknown as ApiClient);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ClientSelector />
    </QueryClientProvider>,
  );
  return meFn;
}

describe("ClientSelector", () => {
  it("offers All my clients, then the person's clients by name, Unassigned last", async () => {
    renderSelector();
    const box = await screen.findByRole("combobox", { name: "Client" });
    expect(within(box).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "All my clients",
      "Acme Dental",
      "Toeshee",
      "Zeta Foods",
      "Unassigned",
    ]);
    expect(box).toHaveValue("all");
  });

  it("stores the choice, and every client as `all`", async () => {
    const user = userEvent.setup();
    renderSelector();
    const box = await screen.findByRole("combobox", { name: "Client" });

    await user.selectOptions(box, ACME);
    expect(selectedClient()).toBe(ACME);
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe(ACME);
    expect(box).toHaveValue(ACME);

    await user.selectOptions(box, "all");
    expect(selectedClient()).toBeNull();
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe("all");
  });

  it("is not drawn for someone /me lists no client for", async () => {
    const me = renderSelector({ ...ME, clients: [] });
    await waitFor(() => expect(me).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("speaks the person's language", async () => {
    setActiveLanguage("es");
    renderSelector();
    const box = await screen.findByRole("combobox", { name: "Cliente" });
    expect(within(box).getByRole("option", { name: "Todos mis clientes" })).toBeInTheDocument();
    expect(within(box).getByRole("option", { name: "Sin asignar" })).toBeInTheDocument();
  });
});
