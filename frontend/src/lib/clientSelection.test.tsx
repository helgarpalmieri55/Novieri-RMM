/**
 * THE CLIENT OF A NEW ROW (phase 3 design §3.1): the parent's, if it has one;
 * else the client chosen in the top bar; else, with "All my clients", a
 * required Client field offering only the clients where the person holds the
 * write's code -- never Unassigned, and never the internal client unasked
 * (Q1). Nothing is sent before the client is known (`requireKnown`).
 */

import { QueryClient, QueryClientProvider, type UseQueryResult } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import type { ApiClient, Me, MeClient } from "./api";
import { setSelectedClient } from "./clientChoice";
import { ClientNotChosen, loadingUntilClient, requireKnown, useNewRowClient } from "./clientSelection";

const mockUseApi = vi.fn();
vi.mock("./ApiContext", () => ({ useApi: () => mockUseApi() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";
const NORTH = "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f06";
const INTERNAL = "9f8e7d6c-5b4a-4321-8fed-cba987654303";
const UNASSIGNED = "1a2b3c4d-5e6f-4789-8abc-def012345604";
const STALE = "7e6d5c4b-3a29-4180-9fed-cba987654305";

function client(id: string, name: string, permissions: string[], kind: MeClient["kind"] = "customer"): MeClient {
  return { id, name, kind, enabled_modules: [], permissions };
}

const ME: Me = {
  user_id: "u1",
  org_id: INTERNAL,
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["ticket.read", "ticket.write"],
  clients: [
    client(ZETA, "Zeta Foods", ["ticket.read", "ticket.write"]),
    client(NORTH, "Northwind", ["ticket.read"]),
    client(INTERNAL, "Toeshee", ["ticket.read", "ticket.write"], "internal"),
    client(UNASSIGNED, "Unassigned", ["ticket.read", "ticket.write"], "unassigned"),
    client(ACME, "Acme Dental", ["ticket.read", "ticket.write"]),
  ],
};

function newRow(parent?: string | null, me: Promise<Me> = Promise.resolve(ME)) {
  const meFn = vi.fn().mockReturnValue(me);
  mockUseApi.mockReturnValue({ me: meFn } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
  return { ...renderHook(() => useNewRowClient("ticket.write", parent), { wrapper }), meFn };
}

describe("useNewRowClient", () => {
  it("takes the parent's client and asks nothing, whatever the top bar says", async () => {
    const { result, meFn } = newRow(ACME);
    await waitFor(() => expect(meFn).toHaveBeenCalled());
    expect(result.current.clientId).toBe(ACME);
    expect(result.current.needsField).toBe(false);
  });

  it("asks nothing and names nothing while the parent's client is not known", async () => {
    const { result, meFn } = newRow(null);
    await waitFor(() => expect(meFn).toHaveBeenCalled());
    expect(result.current).toMatchObject({ clientId: null, needsField: false });
  });

  it("takes the client chosen in the top bar", async () => {
    setSelectedClient(ZETA);
    const { result } = newRow();
    await waitFor(() => expect(result.current.clientId).toBe(ZETA));
    expect(result.current.needsField).toBe(false);
  });

  it("asks with every client, offering only the clients holding the code, never Unassigned", async () => {
    const { result } = newRow();
    await waitFor(() => expect(result.current.needsField).toBe(true));
    expect(result.current.clientId).toBeNull();
    expect(result.current.options.map((c) => c.name)).toEqual(["Acme Dental", "Toeshee", "Zeta Foods"]);
  });

  it("asks nothing and names nothing until /me answers", () => {
    const { result } = newRow(undefined, new Promise<Me>(() => {}));
    expect(result.current).toMatchObject({ clientId: null, needsField: false });
  });

  it("names nothing for a top-bar choice /me does not list", async () => {
    setSelectedClient(STALE);
    const { result, meFn } = newRow();
    await waitFor(() => expect(meFn).toHaveBeenCalled());
    await waitFor(() => expect(result.current.options).toHaveLength(3));
    expect(result.current).toMatchObject({ clientId: null, needsField: false });
  });
});

describe("requireKnown", () => {
  it("hands back a known id unchanged", () => {
    expect(requireKnown(ACME)).toBe(ACME);
  });

  it("refuses an unknown one with a sentence, in the person's language", () => {
    expect(() => requireKnown(null)).toThrow(ClientNotChosen);
    expect(() => requireKnown(null)).toThrow(/account has not loaded yet/);
    setActiveLanguage("es");
    expect(() => requireKnown(null)).toThrow(/tu cuenta aún no se ha cargado/i);
  });
});

describe("loadingUntilClient", () => {
  it("reads as loading while the client it waits for is unknown", () => {
    const idle = { isPending: true, isLoading: false } as UseQueryResult<unknown, unknown>;
    expect(loadingUntilClient(idle, null).isLoading).toBe(true);
    expect(loadingUntilClient(idle, ACME).isLoading).toBe(false);
  });
});
