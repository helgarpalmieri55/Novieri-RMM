/**
 * The Client field (phase 3 design §3.1, Q1): required, and offering only the
 * person's clients where they hold the write's code -- never Unassigned.
 * Nothing is chosen for the person.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import type { ApiClient, Me, MeClient } from "../lib/api";
import { ClientField } from "./ClientField";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";
const NORTH = "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f06";
const UNASSIGNED = "1a2b3c4d-5e6f-4789-8abc-def012345604";

function client(id: string, name: string, permissions: string[], kind: MeClient["kind"] = "customer"): MeClient {
  return { id, name, kind, enabled_modules: [], permissions };
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["ticket.read", "ticket.write"],
  clients: [
    client(ZETA, "Zeta Foods", ["ticket.write"]),
    client(NORTH, "Northwind", ["ticket.read"]),
    client(UNASSIGNED, "Unassigned", ["ticket.write"], "unassigned"),
    client(ACME, "Acme Dental", ["ticket.write"]),
  ],
};

function renderField(me: Me = ME, onChange = vi.fn()) {
  mockUseApi.mockReturnValue({ me: vi.fn().mockResolvedValue(me) } as unknown as ApiClient);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ClientField code="ticket.write" value="" onChange={onChange} />
    </QueryClientProvider>,
  );
  return onChange;
}

describe("ClientField", () => {
  it("offers the clients holding the code, by name, never Unassigned, and chooses none", async () => {
    renderField();
    const field = await screen.findByLabelText("Client");
    await within(field).findByRole("option", { name: "Acme Dental" });
    expect(within(field).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Choose a client",
      "Acme Dental",
      "Zeta Foods",
    ]);
    expect(field).toHaveValue("");
    expect(field).toBeRequired();
  });

  it("reports the client chosen", async () => {
    const user = userEvent.setup();
    const onChange = renderField();
    const field = await screen.findByLabelText("Client");
    await within(field).findByRole("option", { name: "Acme Dental" });
    await user.selectOptions(field, ACME);
    expect(onChange).toHaveBeenCalledWith(ACME);
  });

  it("says so when no client of the person allows the write", async () => {
    renderField({ ...ME, clients: [client(NORTH, "Northwind", ["ticket.read"])] });
    expect(await screen.findByText("None of your clients lets you do this.")).toBeInTheDocument();
  });

  it("speaks the person's language", async () => {
    setActiveLanguage("es");
    renderField();
    const field = await screen.findByLabelText("Cliente");
    expect(within(field).getByRole("option", { name: "Elige un cliente" })).toBeInTheDocument();
  });
});
