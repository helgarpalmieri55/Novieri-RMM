/**
 * A row's client by name (phase 3 design §3.4). From /me's `clients` -- the
 * person's own clients, which needs no `clients.read` -- so every technician
 * reads a name, never an id. A row every client shares, or an id /me does not
 * list, is "All clients". The Unassigned client is named in the person's
 * language. While /me is unknown a client's row names nothing ("" rather
 * than "All clients"), and after a change of client the names the top bar
 * kept are still read.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../../i18n/core";
import type { ApiClient, Me } from "../../../lib/api";
import { setSelectedClient } from "../../../lib/clientChoice";
import { ClientScopeProvider } from "../../../lib/clientScope";
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

function nameOf(me: Me = ME, answer: Promise<Me> = Promise.resolve(me)) {
  const listClients = vi.fn();
  mockUseApi.mockReturnValue({ me: vi.fn().mockReturnValue(answer), listClients } as unknown as ApiClient);
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

  it("names no client while /me is unknown: a client's row never reads as one every client shares", () => {
    const { result } = nameOf(ME, new Promise<Me>(() => {}));
    expect(result.current("c2")).toBe("");
    expect(result.current("c-gone")).toBe("");
    // A row with no client IS every client's, known or not.
    expect(result.current(null)).toBe("All clients");
  });

  it("keeps naming the person's clients while /me is asked again after a change of client", async () => {
    // A change empties the cache (`lib/clientScope.tsx`); the top bar's list
    // survives it, and so do the names read from it.
    const me = vi.fn().mockResolvedValueOnce(ME).mockReturnValue(new Promise<Me>(() => {}));
    mockUseApi.mockReturnValue({ me } as unknown as ApiClient);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, createElement(ClientScopeProvider, null, children));
    const { result } = renderHook(() => useClientName(), { wrapper });
    await waitFor(() => expect(result.current("c2")).toBe("Globex"));

    act(() => setSelectedClient("c2"));

    await waitFor(() => expect(me).toHaveBeenCalledTimes(2));
    expect(result.current("c2")).toBe("Globex");
    expect(result.current("o1")).toBe("Novieri");
  });
});
