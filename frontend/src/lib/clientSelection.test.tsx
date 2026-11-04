/**
 * THE CLIENT OF A NEW ROW (phase 3 design §3.1): the parent's, if it has one;
 * else the client chosen in the top bar; else, with "All my clients", a
 * required Client field offering only the clients where the person holds the
 * write's code -- never Unassigned, and never the internal client unasked
 * (Q1). Nothing is sent before the client is known (`requireKnown`).
 */

import { QueryClient, QueryClientProvider, type UseQueryResult } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import type { ApiClient, Me, MeClient } from "./api";
import { setSelectedClient } from "./clientChoice";
import {
  ClientNotChosen,
  loadingUntilClient,
  parentClientOf,
  requireKnown,
  useNewRowClient,
  useNewRowClientField,
} from "./clientSelection";

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
    // The top bar names another client first: the parent's still wins.
    setSelectedClient(ZETA);
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

/**
 * THE ONE HOOK A NEW ROW'S FORM USES (operator decision 2, final review (A)):
 * the decision above, plus the Client field's own state, so no form keeps a
 * copy of the glue.
 */
function newRowField(
  parent?: string | null,
  me: Promise<Me> = Promise.resolve(ME),
  onPick?: (clientId: string) => void,
) {
  const meFn = vi.fn().mockReturnValue(me);
  mockUseApi.mockReturnValue({ me: meFn } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
  const hook = renderHook(
    () => useNewRowClientField("ticket.write", parent, onPick ? { onPick } : undefined),
    { wrapper },
  );
  return { ...hook, meFn };
}

describe("useNewRowClientField: the decision, in order", () => {
  it("1. takes the parent's client over a client chosen at the top, and asks nothing", async () => {
    setSelectedClient(ZETA);
    const { result } = newRowField(ACME);
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current).toMatchObject({ clientId: ACME, needsField: false, refused: null });
  });

  it("1. takes the parent's client even with Unassigned chosen at the top: nothing is refused", async () => {
    setSelectedClient(UNASSIGNED);
    const { result } = newRowField(ACME);
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current).toMatchObject({ clientId: ACME, needsField: false, refused: null });
  });

  it("1. names nothing and asks nothing while the parent's client is not known", async () => {
    const { result } = newRowField(null);
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current).toMatchObject({ clientId: null, needsField: false, refused: null });
  });

  it("2. names nothing, asks nothing and refuses nothing until /me answers", () => {
    setSelectedClient(UNASSIGNED);
    const { result } = newRowField(undefined, new Promise<Me>(() => {}));
    expect(result.current).toMatchObject({
      clientId: null,
      needsField: false,
      refused: null,
      ready: false,
      picked: "",
    });
    expect(result.current.options).toEqual([]);
  });

  it("2. names nothing for a top-bar choice /me does not list", async () => {
    setSelectedClient(STALE);
    const { result } = newRowField();
    await waitFor(() => expect(result.current.options).toHaveLength(3));
    expect(result.current).toMatchObject({ clientId: null, needsField: false, refused: null, ready: false });
  });

  it("3. refuses with Unassigned chosen at the top: nothing new can be filed there", async () => {
    // The server files nothing new under Unassigned (tickets reach it only by
    // email), and the header confines the request to it, so a Client field
    // could not help either.
    setSelectedClient(UNASSIGNED);
    const { result } = newRowField();
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current).toMatchObject({ clientId: null, needsField: false, refused: "unassigned" });
  });

  it("4. takes the client chosen at the top, and asks nothing", async () => {
    setSelectedClient(ZETA);
    const { result } = newRowField();
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current).toMatchObject({ clientId: ZETA, needsField: false, refused: null });
  });

  it("5. asks with every client, offering only the clients holding the code, never Unassigned", async () => {
    const { result } = newRowField();
    await waitFor(() => expect(result.current.needsField).toBe(true));
    expect(result.current).toMatchObject({ clientId: null, picked: "", ready: true, refused: null });
    expect(result.current.options.map((c) => c.name)).toEqual(["Acme Dental", "Toeshee", "Zeta Foods"]);
  });

  it("5. names the pick once it is one of the options", async () => {
    const { result } = newRowField();
    await waitFor(() => expect(result.current.needsField).toBe(true));
    act(() => result.current.setPicked(ACME));
    expect(result.current).toMatchObject({ picked: ACME, clientId: ACME, needsField: true });
  });

  it("5. names nothing for a pick that is not among the options", async () => {
    const { result } = newRowField();
    await waitFor(() => expect(result.current.needsField).toBe(true));
    // Northwind holds only `ticket.read`; Unassigned is never offered.
    for (const outside of [NORTH, UNASSIGNED, STALE]) {
      act(() => result.current.setPicked(outside));
      expect(result.current).toMatchObject({ picked: outside, clientId: null });
    }
  });

  it("never names \"\": an emptied field is no client", async () => {
    const { result } = newRowField();
    await waitFor(() => expect(result.current.needsField).toBe(true));
    act(() => result.current.setPicked(ACME));
    act(() => result.current.setPicked(""));
    expect(result.current).toMatchObject({ picked: "", clientId: null });
  });
});

describe("useNewRowClientField: onPick", () => {
  it("runs on a real change of the pick, and only then", async () => {
    const onPick = vi.fn();
    const { result } = newRowField(undefined, Promise.resolve(ME), onPick);
    await waitFor(() => expect(result.current.needsField).toBe(true));

    act(() => result.current.setPicked(ACME));
    expect(onPick).toHaveBeenCalledTimes(1);
    expect(onPick).toHaveBeenLastCalledWith(ACME);

    act(() => result.current.setPicked(ACME));
    expect(onPick).toHaveBeenCalledTimes(1);

    act(() => result.current.setPicked(ZETA));
    expect(onPick).toHaveBeenCalledTimes(2);
    expect(onPick).toHaveBeenLastCalledWith(ZETA);
  });

  it("reads the latest callback, not the one of the first render", async () => {
    const first = vi.fn();
    const second = vi.fn();
    const meFn = vi.fn().mockResolvedValue(ME);
    mockUseApi.mockReturnValue({ me: meFn } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result, rerender } = renderHook(
      ({ onPick }: { onPick: (clientId: string) => void }) =>
        useNewRowClientField("ticket.write", undefined, { onPick }),
      { wrapper, initialProps: { onPick: first } },
    );
    await waitFor(() => expect(result.current.needsField).toBe(true));
    rerender({ onPick: second });
    act(() => result.current.setPicked(ACME));
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledWith(ACME);
  });
});

describe("parentClientOf", () => {
  const ROWS = [
    { id: "vrf-acme", client_id: ACME },
    { id: "vrf-zeta", client_id: ZETA },
  ];

  it("is undefined when there is no parent: the row hangs from nothing", () => {
    expect(parentClientOf(undefined, ROWS)).toBeUndefined();
    expect(parentClientOf(null, ROWS)).toBeUndefined();
    // A form's field state for "no parent chosen".
    expect(parentClientOf("", ROWS)).toBeUndefined();
  });

  it("is null while the rows have not loaded: a parent not found yet is not \"no parent\"", () => {
    expect(parentClientOf("vrf-acme", undefined)).toBeNull();
  });

  it("is null for a parent the rows do not hold", () => {
    expect(parentClientOf("vrf-gone", ROWS)).toBeNull();
    expect(parentClientOf("vrf-gone", [])).toBeNull();
  });

  it("is the parent's client once it is found", () => {
    expect(parentClientOf("vrf-zeta", ROWS)).toBe(ZETA);
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

  it("refuses an empty one: a form's field before a client is chosen", () => {
    // Field state is a string where "" means "not chosen"; a form that
    // forgets `|| null` must still send nothing, never `client_id: ""`.
    expect(() => requireKnown("")).toThrow(ClientNotChosen);
  });
});

describe("loadingUntilClient", () => {
  it("reads as loading while the client it waits for is unknown", () => {
    const idle = { isPending: true, isLoading: false } as UseQueryResult<unknown, unknown>;
    expect(loadingUntilClient(idle, null).isLoading).toBe(true);
    expect(loadingUntilClient(idle, ACME).isLoading).toBe(false);
  });
});
