/**
 * The console's live socket across session checks and gaps.
 *
 * RE-POINTED (own-auth Task 10). This file held the socket to its Keycloak
 * life: the server closed it 4401 when the five-minute token it was opened
 * with expired, so the console opened a successor on every renewal and swapped
 * to it without a gap (final review W1). The session is a cookie now, sent by
 * the browser with the handshake; no token is renewed and no socket is
 * swapped. What each test protected, it protects here:
 *
 *   1. the handshake carries no credential of its own (was: the token in the
 *      subprotocol, never the URL) -- and the one socket stays open;
 *   2. a reopen after an UNPLANNED close tells the page to resync (unchanged);
 *   3. a 4401 reconnects at once, without a backoff, when `/me` says the
 *      cookie still holds a session (its identifier rotated on a second-factor
 *      confirmation) -- and opens nothing more when `/me` says it ended;
 *   4. a 4401 never spins: a refused handshake after it backs off;
 *   5. any other close still backs off, doubling (unchanged);
 *   6. no socket at all while the session has ended.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setSelectedClient } from "./clientChoice";
import { RESYNC_KIND } from "./resync";
import { isSessionEnded, sessionEnded } from "./sessionEnd";
import { CLOSE_SESSION_ENDED, liveUrl, useLiveEvents } from "./ws";

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

class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  closed = false;
  readonly protocols: string | string[] | undefined;

  constructor(
    readonly url: string,
    ...protocols: (string | string[])[]
  ) {
    this.protocols = protocols[0];
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

/** `useLiveEvents` asks `/me` again -- on a 4401, a 4403, and a close before open -- through the query client. */
function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

/** The `client` parameters a socket was opened with. */
function clientOf(socket: FakeSocket): string[] {
  return new URL(socket.url).searchParams.getAll("client");
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.instances = [];
  queryClient = new QueryClient();
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  choice.unrendered = undefined;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("useLiveEvents", () => {
  it("opens the stream with no credential of its own, and keeps the one socket open", () => {
    // The browser sends the session cookie with the handshake; nothing in
    // the URL or the subprotocol list can reach an access log or a header
    // dump. (Was: `["bearer", token]`, swapped on every renewal.)
    const handler = vi.fn();
    const { unmount } = renderHook(() => useLiveEvents(handler), { wrapper });
    const [first] = sockets();
    expect(first!.protocols).toBeUndefined();
    expect(new URL(first!.url).search).toBe("");
    act(() => first!.open());
    act(() => first!.receive({ kind: "job_status", job_id: "j1" }));
    expect(handler).toHaveBeenLastCalledWith({ kind: "job_status", job_id: "j1" });

    act(() => vi.advanceTimersByTime(10 * 60_000));
    expect(sockets()).toHaveLength(1);
    expect(first!.closed).toBe(false);
    unmount();
    expect(first!.closed).toBe(true);
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

  it("on 4401, asks /me and reconnects at once when the session is still there", async () => {
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
    const handler = vi.fn();
    renderHook(() => useLiveEvents(handler), { wrapper });
    act(() => sockets()[0]!.open());

    await act(async () => {
      sockets()[0]!.ended(CLOSE_SESSION_ENDED);
      await Promise.resolve();
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["me"] });
    // No backoff waited: the second socket is already there.
    expect(sockets()).toHaveLength(2);
    act(() => sockets()[1]!.open());
    expect(handler).toHaveBeenCalledWith({ kind: RESYNC_KIND });
  });

  it("on 4401, opens nothing more when /me says the session ended", async () => {
    // `/me` answering 401 is what ends the session in this tab
    // (`ApiProvider`'s `onUnauthorized`); here the refetch does it directly.
    vi.spyOn(queryClient, "invalidateQueries").mockImplementation(() => {
      sessionEnded();
      return Promise.resolve();
    });
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    act(() => sockets()[0]!.open());

    await act(async () => {
      sockets()[0]!.ended(CLOSE_SESSION_ENDED);
      await Promise.resolve();
    });
    act(() => vi.advanceTimersByTime(60_000));
    expect(sockets()).toHaveLength(1);
  });

  it("on 4401, treats another person's session in the cookie as the session ending", async () => {
    queryClient.setQueryData(["me"], { user_id: "u-1" });
    vi.spyOn(queryClient, "invalidateQueries").mockImplementation(() => {
      queryClient.setQueryData(["me"], { user_id: "u-2" });
      return Promise.resolve();
    });
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    act(() => sockets()[0]!.open());

    await act(async () => {
      sockets()[0]!.ended(CLOSE_SESSION_ENDED);
      await Promise.resolve();
    });
    act(() => vi.advanceTimersByTime(60_000));
    expect(sockets()).toHaveLength(1);
    expect(isSessionEnded()).toBe(true);
  });

  it("on 4401, reconnects when the same person is still there", async () => {
    queryClient.setQueryData(["me"], { user_id: "u-1" });
    vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    act(() => sockets()[0]!.open());
    await act(async () => {
      sockets()[0]!.ended(CLOSE_SESSION_ENDED);
      await Promise.resolve();
    });
    expect(sockets()).toHaveLength(2);
  });

  it("after a 4401, a refused handshake backs off rather than looping", async () => {
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    act(() => sockets()[0]!.open());

    await act(async () => {
      sockets()[0]!.ended(CLOSE_SESSION_ENDED);
      await Promise.resolve();
    });
    expect(sockets()).toHaveLength(2);
    act(() => sockets()[1]!.ended(1006));
    expect(invalidate).toHaveBeenCalledTimes(2);
    expect(sockets()).toHaveLength(2);
    act(() => vi.advanceTimersByTime(2_000));
    expect(sockets()).toHaveLength(3);
  });

  it("reconnects at once only once in a row: a second refusal with nothing delivered backs off", async () => {
    vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    act(() => sockets()[0]!.open());

    await act(async () => {
      sockets()[0]!.ended(4403);
      await Promise.resolve();
    });
    expect(sockets()).toHaveLength(2);
    act(() => sockets()[1]!.open());
    await act(async () => {
      sockets()[1]!.ended(CLOSE_SESSION_ENDED);
      await Promise.resolve();
    });
    // A server that accepts and refuses every handshake cannot make it spin.
    expect(sockets()).toHaveLength(2);
    act(() => vi.advanceTimersByTime(2_000));
    expect(sockets()).toHaveLength(3);

    // An event delivered ends the spell: the next refusal reconnects at once.
    act(() => sockets()[2]!.open());
    act(() => sockets()[2]!.receive({ kind: "job_status" }));
    await act(async () => {
      sockets()[2]!.ended(4403);
      await Promise.resolve();
    });
    expect(sockets()).toHaveLength(4);
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

  it("opens no socket while the session has ended, and closes the open one when it ends", () => {
    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    const [first] = sockets();
    act(() => first!.open());

    act(() => sessionEnded());
    expect(first!.closed).toBe(true);
    act(() => first!.ended(1000));
    act(() => vi.advanceTimersByTime(60_000));
    expect(sockets()).toHaveLength(1);

    renderHook(() => useLiveEvents(vi.fn()), { wrapper });
    expect(sockets()).toHaveLength(1);
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
