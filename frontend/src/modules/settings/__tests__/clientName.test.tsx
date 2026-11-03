/**
 * A row's client by name (phase 3 design §3.4). From /me's `clients` -- the
 * person's own clients, which needs no `clients.read` -- so every technician
 * reads a name, never an id. A row every client shares, or an id /me does not
 * list, is "All clients". The Unassigned client is named in the person's
 * language.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../../i18n/core";
import type { ApiClient, Me } from "../../../lib/api";
import { useClientName } from "../api";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["licenses.read"],
  clients: [
    { id: "o1", name: "Novieri", kind: "internal", enabled_modules: [], permissions: ["licenses.read"] },
    { id: "c2", name: "Globex", kind: "customer", enabled_modules: [], permissions: ["licenses.read"] },
    { id: "c9", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: ["licenses.read"] },
  ],
};

function nameOf(me: Me = ME) {
  const listClients = vi.fn();
  mockUseApi.mockReturnValue({ me: vi.fn().mockResolvedValue(me), listClients } as unknown as ApiClient);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return { ...renderHook(() => useClientName(), { wrapper }), listClients };
}

describe("useClientName", () => {
  it("names a client from /me, with no clients.read and no read of the clients list", async () => {
    const { result, listClients } = nameOf();
    await waitFor(() => expect(result.current("c2")).toBe("Globex"));
    expect(result.current("o1")).toBe("Novieri");
    expect(listClients).not.toHaveBeenCalled();
  });

  it("names a row every client shares, and a client /me does not list, as all clients", async () => {
    const { result } = nameOf();
    await waitFor(() => expect(result.current("c2")).toBe("Globex"));
    expect(result.current(null)).toBe("All clients");
    expect(result.current("c-gone")).toBe("All clients");
  });

  it("names the Unassigned client, and every client, in the person's language", async () => {
    const { result } = nameOf();
    await waitFor(() => expect(result.current("c2")).toBe("Globex"));
    expect(result.current("c9")).toBe("Unassigned");
    setActiveLanguage("es");
    expect(result.current("c9")).toBe("Sin asignar");
    expect(result.current(null)).toBe("Todos los clientes");
  });
});
