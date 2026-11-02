/**
 * The client selection, for components (phase 3 design §1.1, §1.4).
 *
 * WHAT A CHANGE OF CLIENT OWES THE PAGE. Nothing read for one client may stay
 * on screen under another: the query cache is emptied and the page under the
 * provider starts again, asking everything with the new header. A choice /me
 * does not list -- archived, or the person lost access -- falls back to every
 * client and says so once. A choice made in another tab is taken the same way.
 */

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import { createElement, useEffect, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Me, MeClient } from "./api";
import { CLIENT_STORAGE_KEY, selectedClient, setSelectedClient } from "./clientChoice";
import { ClientScopeProvider, enabledModules, useClientScope } from "./clientScope";

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

  it("keeps the setup gate's answer, which lives outside it and names no client, and nothing else", async () => {
    // `App.tsx`'s `SetupGate` sits ABOVE the provider and holds
    // `["setup", "status"]` with `staleTime: Infinity`. Emptied with the rest,
    // its next re-render found no query, drew "Starting..." in place of the
    // whole routes tree -- page state and all -- and asked again.
    const queryClient = newQueryClient();
    const setup = vi.fn().mockResolvedValue({ needed: false });
    function SetupGate({ children }: { children: ReactNode }) {
      const status = useQuery({ queryKey: ["setup", "status"], queryFn: setup, staleTime: Infinity });
      return status.isPending ? <p>Starting</p> : <>{children}</>;
    }
    mockUseApi.mockReturnValue({ me: vi.fn().mockResolvedValue(ME) } as unknown as ApiClient);
    const tree = () => (
      <QueryClientProvider client={queryClient}>
        <SetupGate>
          <ClientScopeProvider>
            <Page />
          </ClientScopeProvider>
        </SetupGate>
      </QueryClientProvider>
    );
    const { rerender } = render(tree());
    expect(await screen.findByText("ready:all")).toBeInTheDocument();
    queryClient.setQueryData(["devices"], ["a device of every client"]);
    queryClient.setQueryData(["clients"], ["every client's name"]);
    queryClient.getMutationCache().build(queryClient, { mutationKey: ["devices", "rename"] });

    act(() => setSelectedClient(ACME));

    expect(queryClient.getQueryData(["devices"])).toBeUndefined();
    expect(queryClient.getQueryData(["clients"])).toBeUndefined();
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);
    expect(queryClient.getQueryData(["setup", "status"])).toEqual({ needed: false });

    // The later re-render that used to find the gate's query gone.
    rerender(tree());
    expect(screen.queryByText("Starting")).toBeNull();
    expect(await screen.findByText(`ready:${ACME}`)).toBeInTheDocument();
    expect(setup).toHaveBeenCalledTimes(1);
  });

  it("offers the last list's names, and nothing else of /me, while /me is asked again", async () => {
    // The selector is drawn from this list. Emptied by the change, it
    // vanished until /me answered and the top bar jumped; the person's own
    // clients by id, name and kind are what it needs, and all it gets.
    let answer: (me: Me) => void = () => undefined;
    const me = vi
      .fn()
      .mockResolvedValueOnce(ME)
      .mockImplementation(
        () =>
          new Promise<Me>((resolve) => {
            answer = resolve;
          }),
      );
    mockUseApi.mockReturnValue({ me } as unknown as ApiClient);
    function Listed() {
      const scope = useClientScope();
      return <pre data-testid="listed">{JSON.stringify({ ready: scope.ready, clients: scope.clients })}</pre>;
    }
    const listed = () =>
      JSON.parse(screen.getByTestId("listed").textContent ?? "") as {
        ready: boolean;
        clients: Record<string, unknown>[];
      };
    render(
      <QueryClientProvider client={newQueryClient()}>
        <ClientScopeProvider>
          <Listed />
        </ClientScopeProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(listed().ready).toBe(true));

    act(() => setSelectedClient(ACME));

    await waitFor(() => expect(me).toHaveBeenCalledTimes(2));
    expect(listed().ready).toBe(false);
    expect(listed().clients).toEqual([
      { id: ACME, name: "Acme Dental", kind: "customer" },
      { id: INTERNAL, name: "Toeshee", kind: "internal" },
      { id: ZETA, name: "Zeta Foods", kind: "customer" },
      { id: UNASSIGNED, name: "Unassigned", kind: "unassigned" },
    ]);

    await act(async () => answer(ME));
    await waitFor(() => expect(listed().ready).toBe(true));
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

describe("enabledModules (phase 3 design §2)", () => {
  const withModules = (id: string, modules: string[]): MeClient => ({
    ...client(id, id),
    enabled_modules: modules,
  });
  const CLIENTS = [withModules(ACME, ["budget"]), withModules(ZETA, ["change"]), withModules(INTERNAL, [])];

  it("is the chosen client's modules", () => {
    expect([...enabledModules(CLIENTS, ACME)]).toEqual(["budget"]);
    expect([...enabledModules(CLIENTS, INTERNAL)]).toEqual([]);
  });

  it("is every module some client uses, with every client chosen", () => {
    expect([...enabledModules(CLIENTS, null)].sort()).toEqual(["budget", "change"]);
  });

  it("is every optional module from an API that names no clients", () => {
    expect([...enabledModules(undefined, null)].sort()).toEqual(["bcp", "budget", "change"]);
  });
});
