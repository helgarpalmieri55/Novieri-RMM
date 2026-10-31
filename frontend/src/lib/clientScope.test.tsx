/**
 * The client selection, for components (phase 3 design §1.1, §1.4).
 *
 * WHAT A CHANGE OF CLIENT OWES THE PAGE. Nothing read for one client may stay
 * on screen under another: the query cache is emptied and the page under the
 * provider starts again, asking everything with the new header. A choice /me
 * does not list -- archived, or the person lost access -- falls back to every
 * client and says so once. A choice made in another tab is taken the same way.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import { createElement, useEffect, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Me, MeClient } from "./api";
import { CLIENT_STORAGE_KEY, selectedClient, setSelectedClient } from "./clientChoice";
import { ClientScopeProvider, useClientScope } from "./clientScope";

const mockUseApi = vi.fn();
vi.mock("./ApiContext", () => ({ useApi: () => mockUseApi() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";
const INTERNAL = "9f8e7d6c-5b4a-4321-8fed-cba987654303";
const UNASSIGNED = "1a2b3c4d-5e6f-4789-8abc-def012345604";
const STALE = "7e6d5c4b-3a29-4180-9fed-cba987654305";

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

function newQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

let mounts = 0;

/** A page under the provider: counts its mounts and says what it sees. */
function Page() {
  useEffect(() => {
    mounts += 1;
  }, []);
  const scope = useClientScope();
  return <p data-testid="page">{scope.ready ? `ready:${scope.selected ?? "all"}` : "waiting"}</p>;
}

function renderScoped(me: Me = ME, queryClient: QueryClient = newQueryClient()) {
  const api = { me: vi.fn().mockResolvedValue(me) };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  render(
    <QueryClientProvider client={queryClient}>
      <ClientScopeProvider>
        <Page />
      </ClientScopeProvider>
    </QueryClientProvider>,
  );
  return { api, queryClient };
}

beforeEach(() => {
  mounts = 0;
});

describe("ClientScopeProvider", () => {
  it("empties the query cache and starts the page again when the client changes", async () => {
    const { api, queryClient } = renderScoped();
    expect(await screen.findByText("ready:all")).toBeInTheDocument();
    queryClient.setQueryData(["devices"], ["a device of every client"]);

    act(() => setSelectedClient(ACME));

    expect(queryClient.getQueryData(["devices"])).toBeUndefined();
    expect(await screen.findByText(`ready:${ACME}`)).toBeInTheDocument();
    expect(mounts).toBe(2);
    // `/me` itself is asked again, under the new narrowing.
    expect(api.me.mock.calls.length).toBeGreaterThan(1);
  });

  it("falls back to every client, and says so once, when /me does not list the choice", async () => {
    setSelectedClient(STALE);
    renderScoped();

    expect(
      await screen.findByText(
        "The client you had chosen is no longer available to you, so the console is showing all your clients.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("ready:all")).toBeInTheDocument();
    expect(selectedClient()).toBeNull();
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe("all");
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  it("takes a choice made in another tab like one made here", async () => {
    const { queryClient } = renderScoped();
    await screen.findByText("ready:all");
    queryClient.setQueryData(["devices"], ["a device of every client"]);

    window.localStorage.setItem(CLIENT_STORAGE_KEY, ACME);
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: CLIENT_STORAGE_KEY, newValue: ACME }));
    });

    expect(selectedClient()).toBe(ACME);
    expect(queryClient.getQueryData(["devices"])).toBeUndefined();
    expect(await screen.findByText(`ready:${ACME}`)).toBeInTheDocument();
  });
});

describe("useClientScope", () => {
  function hookScope(me: Me = ME) {
    mockUseApi.mockReturnValue({ me: vi.fn().mockResolvedValue(me) } as unknown as ApiClient);
    const queryClient = newQueryClient();
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    return renderHook(() => useClientScope(), { wrapper });
  }

  it("lists the person's clients by name, Unassigned last, once /me answers", async () => {
    const { result } = hookScope();
    expect(result.current.ready).toBe(false);
    expect(result.current.clients).toEqual([]);
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.clients.map((c) => c.name)).toEqual([
      "Acme Dental",
      "Toeshee",
      "Zeta Foods",
      "Unassigned",
    ]);
  });

  it("is not ready while the choice is not one of /me's clients", async () => {
    setSelectedClient(STALE);
    const { result } = hookScope();
    await waitFor(() => expect(result.current.clients).toHaveLength(4));
    expect(result.current.ready).toBe(false);
  });
});
