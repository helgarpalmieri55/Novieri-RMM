/**
 * The console's live socket across token renewals and gaps (final review W1).
 *
 * The server closes the socket 4401 once the token that opened it expires,
 * and Keycloak's access tokens live five minutes -- so a socket that kept the
 * token it was opened with closed every five minutes, waited out a 2 s
 * backoff, and every event published in that gap was lost. A job whose final
 * `job_status` fell into it stayed "running" on screen.
 *
 * Four properties, each against a fake WebSocket and fake timers:
 *   1. a renewed token opens a NEW socket, and the old one is closed only
 *      once the new one is open -- and the old one's close schedules nothing;
 *   2. a reopen after an UNPLANNED close tells the page to resync;
 *   3. a 4401 reconnects at once, with a fresh token, not after a backoff;
 *   4. any other close still backs off.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setSelectedClient } from "./clientChoice";
import { RESYNC_KIND } from "./resync";
import { SWAP_GRACE_MS, liveUrl, useLiveEvents } from "./ws";

const auth = vi.hoisted(() => ({
  token: "t1" as string | null,
  listeners: new Set<(token: string) => void>(),
  renew: vi.fn<() => Promise<string | null>>(),
}));

/**
 * The choice as anyone reading it now would see it, ahead of React. Unset,
 * `selectedClient` is the real one; set, it is a choice that has moved
 * without the store's listeners -- React -- having been told yet.
 */
const choice = vi.hoisted(() => ({ unrendered: undefined as string | null | undefined }));

vi.mock("./clientChoice", async (importOriginal) => {
  const real = await importOriginal<typeof import("./clientChoice")>();
  return {
    ...real,
    selectedClient: () => (choice.unrendered !== undefined ? choice.unrendered : real.selectedClient()),
  };
});

vi.mock("./auth", () => ({
  accessToken: () => auth.token,
  onTokenRenewed: (listener: (token: string) => void) => {
    auth.listeners.add(listener);
    return () => auth.listeners.delete(listener);
  },
  renewAccessToken: () => auth.renew(),
}));

class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  closed = false;

  constructor(
    readonly url: string,
    readonly protocols: string[],
  ) {
    FakeSocket.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  /** The server accepted the handshake. */
  open(): void {
    this.onopen?.(new Event("open"));
  }

  /** The connection ended, with `code` (the browser's close event). */
  ended(code: number): void {
    this.onclose?.({ code } as CloseEvent);
  }

  receive(event: unknown): void {
    this.onmessage?.({ data: JSON.stringify(event) } as MessageEvent);
  }
}

function sockets(): FakeSocket[] {
  return FakeSocket.instances;
}

let queryClient: QueryClient;

/** `useLiveEvents` asks `/me` again -- on a 4403, and on a close before open -- through the query client. */
function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

/** The `client` parameters a socket was opened with. */
function clientOf(socket: FakeSocket): string[] {
  return new URL(socket.url).searchParams.getAll("client");
}

function renew(token: string): void {
  auth.token = token;
  act(() => {
    for (const listener of auth.listeners) listener(token);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.instances = [];
  auth.token = "t1";
  auth.listeners.clear();
  auth.renew.mockReset();
  queryClient = new QueryClient();
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  choice.unrendered = undefined;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("useLiveEvents", () => {
  it("opens a new socket on a renewed token, and closes the old one only once the new one is open", () => {
    const handler = vi.fn();
    const { unmount } = renderHook(() => useLiveEvents(handler), { wrapper });
    const [first] = sockets();
    expect(first!.protocols).toEqual(["bearer", "t1"]);
    act(() => first!.open());

    renew("t2");
    expect(sockets()).toHaveLength(2);
    const second = sockets()[1]!;
    expect(second.protocols).toEqual(["bearer", "t2"]);
    // Not before the new one is open: that would be the gap again.
    expect(first!.closed).toBe(false);
    act(() => first!.receive({ kind: "job_status", job_id: "j1" }));
    expect(handler).toHaveBeenLastCalledWith({ kind: "job_status", job_id: "j1" });

    act(() => second.open());
    act(() => vi.advanceTimersByTime(SWAP_GRACE_MS));
    expect(first!.closed).toBe(true);

    // The old socket's own close is planned: it schedules nothing and says
    // nothing about a gap.
    act(() => first!.ended(1000));
    act(() => vi.advanceTimersByTime(60_000));
    expect(sockets()).toHaveLength(2);
    expect(handler).not.toHaveBeenCalledWith({ kind: RESYNC_KIND });

    act(() => second.receive({ kind: "job_status", job_id: "j2" }));
    expect(handler).toHaveBeenLastCalledWith({ kind: "job_status", job_id: "j2" });
    unmount();
  });

  it("keeps the old socket when the replacement fails to open", () => {
    const handler = vi.fn();
    renderHook(() => useLiveEvents(handler), { wrapper });
    const [first] = sockets();
    act(() => first!.open());

    renew("t2");
    act(() => sockets()[1]!.ended(1006));
    act(() => vi.advanceTimersByTime(60_000));
    expect(first!.closed).toBe(false);
    expect(sockets()).toHaveLength(2);
    act(() => first!.receive({ kind: "job_log" }));
    expect(handler).toHaveBeenLastCalledWith({ kind: "job_log" });
  });

  it("delivers a resync on the reopen after an unplanned close, and not on the first open", () => {
    const handler = vi.fn();
    renderHook(() => useLiveEvents(handler), { wrapper });
    act(() => sockets()[0]!.open());
    expect(handler).not.toHaveBeenCalled();

    act(() => sockets()[0]!.ended(1006));
    act(() => vi.advanceTimersByTime(2_000));
    expect(sockets()).toHaveLength(2);
    act(() => sockets()[1]!.open());
    expect(handler).toHaveBeenCalledWith({ kind: RESYNC_KIND });
  });

  it("reconnects at once on 4401, with the token already renewed", () => {
    const handler = vi.fn();
    renderHook(() => useLiveEvents(handler), { wrapper });
    act(() => sockets()[0]!.open());
    auth.token = "t2"; // renewed, but this socket kept the old one

    act(() => sockets()[0]!.ended(4401));
    expect(sockets()).toHaveLength(2);
    expect(sockets()[1]!.protocols).toEqual(["bearer", "t2"]);
    expect(auth.renew).not.toHaveBeenCalled();
    act(() => sockets()[1]!.open());
    expect(handler).toHaveBeenCalledWith({ kind: RESYNC_KIND });
  });

  it("on 4401 with no newer token, renews one and reconnects without a backoff", async () => {
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    act(() => sockets()[0]!.open());
    auth.renew.mockImplementation(() => {
      auth.token = "t3";
      return Promise.resolve("t3");
    });

    await act(async () => {
      sockets()[0]!.ended(4401);
      await Promise.resolve();
    });
    expect(auth.renew).toHaveBeenCalledTimes(1);
    expect(sockets()).toHaveLength(2);
    expect(sockets()[1]!.protocols).toEqual(["bearer", "t3"]);
  });

  it("on 4401 when renewal fails, backs off rather than looping", async () => {
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    act(() => sockets()[0]!.open());
    auth.renew.mockResolvedValue(null);

    await act(async () => {
      sockets()[0]!.ended(4401);
      await Promise.resolve();
    });
    expect(sockets()).toHaveLength(1);
    act(() => vi.advanceTimersByTime(2_000));
    expect(sockets()).toHaveLength(2);
  });

  it("backs off on any other close, doubling", () => {
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    act(() => sockets()[0]!.open());

    act(() => sockets()[0]!.ended(1011));
    expect(sockets()).toHaveLength(1);
    act(() => vi.advanceTimersByTime(1_999));
    expect(sockets()).toHaveLength(1);
    act(() => vi.advanceTimersByTime(1));
    expect(sockets()).toHaveLength(2);

    act(() => sockets()[1]!.ended(1006));
    act(() => vi.advanceTimersByTime(3_999));
    expect(sockets()).toHaveLength(2);
    act(() => vi.advanceTimersByTime(1));
    expect(sockets()).toHaveLength(3);
  });
});

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";

describe("useLiveEvents and the chosen client (phase 3 design §1.5)", () => {
  it("names the chosen client in ?client=, and none for every client", () => {
    expect(new URL(liveUrl(null)).pathname).toBe("/api/v1/ws");
    expect(new URL(liveUrl(null)).search).toBe("");
    expect(new URL(liveUrl(ACME)).searchParams.getAll("client")).toEqual([ACME]);

    const { unmount } = renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    expect(clientOf(sockets()[0]!)).toEqual([]);
    unmount();

    setSelectedClient(ACME);
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    expect(clientOf(sockets()[1]!)).toEqual([ACME]);
  });

  it("opens again for a new choice, and the old socket's close is planned", () => {
    const handler = vi.fn();
    renderHook(() => useLiveEvents(handler), { wrapper });
    const [first] = sockets();
    act(() => first!.open());

    act(() => setSelectedClient(ZETA));
    expect(sockets()).toHaveLength(2);
    expect(first!.closed).toBe(true);
    expect(clientOf(sockets()[1]!)).toEqual([ZETA]);

    // The old socket's own close schedules nothing and is no gap.
    act(() => first!.ended(1000));
    act(() => vi.advanceTimersByTime(60_000));
    expect(sockets()).toHaveLength(2);
    expect(handler).not.toHaveBeenCalledWith({ kind: RESYNC_KIND });
  });

  it("on a 4403 after the socket was open, asks /me again and reconnects at once", async () => {
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    act(() => sockets()[0]!.open());

    await act(async () => {
      sockets()[0]!.ended(4403);
      await Promise.resolve();
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["me"] });
    // No backoff waited: the second socket is already there.
    expect(sockets()).toHaveLength(2);
  });

  // A REFUSED HANDSHAKE IS NEVER A 4403 IN A BROWSER. The server refuses an
  // empty scope (and a token, and a malformed `client`) before `accept()`, so
  // uvicorn answers the upgrade with HTTP 403 and all the page sees is a
  // close before open, 1006 -- the same as a server that is down.
  it("on a close before the socket opened, asks /me again once and backs off", () => {
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
    setSelectedClient(ACME);
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });

    act(() => sockets()[0]!.ended(1006));
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["me"] });
    // Not at once: a refused scope must not spin.
    expect(sockets()).toHaveLength(1);
    act(() => vi.advanceTimersByTime(2_000));
    expect(sockets()).toHaveLength(2);

    // ONCE per run of refusals: an outage must not turn into a /me per retry.
    act(() => sockets()[1]!.ended(1006));
    act(() => vi.advanceTimersByTime(4_000));
    expect(sockets()).toHaveLength(3);
    expect(invalidate).toHaveBeenCalledTimes(1);

    // An open starts a new run: the next refusal asks again.
    act(() => sockets()[2]!.open());
    act(() => sockets()[2]!.ended(1006));
    expect(invalidate).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(2_000));
    act(() => sockets()[3]!.ended(1006));
    expect(invalidate).toHaveBeenCalledTimes(2);
  });

  it("opens nothing for a client the choice has left before this effect caught up", async () => {
    // `disposed` is set by React re-running the effect, and React learns of
    // a new choice only when it renders. TanStack hands a refetched /me to
    // its observers through `setTimeout(0)`, so nothing guarantees the
    // re-render has happened by the time this hook acts again. Here the
    // choice moves with React not yet told (`choice.unrendered`): the
    // effect is not disposed, and only `connect()`'s own look at the
    // current choice stops a socket for the client it has left.
    setSelectedClient(ACME);
    vi.spyOn(queryClient, "invalidateQueries").mockImplementation(() => {
      choice.unrendered = null;
      return Promise.resolve();
    });
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    act(() => sockets()[0]!.open());

    await act(async () => {
      sockets()[0]!.ended(4403);
      await Promise.resolve();
    });

    expect(sockets().map(clientOf)).toEqual([[ACME]]);
  });
});
