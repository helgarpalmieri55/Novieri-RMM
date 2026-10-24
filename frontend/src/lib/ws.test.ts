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

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RESYNC_KIND } from "./resync";
import { SWAP_GRACE_MS, useLiveEvents } from "./ws";

const auth = vi.hoisted(() => ({
  token: "t1" as string | null,
  listeners: new Set<(token: string) => void>(),
  renew: vi.fn<() => Promise<string | null>>(),
}));

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
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("useLiveEvents", () => {
  it("opens a new socket on a renewed token, and closes the old one only once the new one is open", () => {
    const handler = vi.fn();
    const { unmount } = renderHook(() => useLiveEvents(handler));
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
    renderHook(() => useLiveEvents(handler));
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
    renderHook(() => useLiveEvents(handler));
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
    renderHook(() => useLiveEvents(handler));
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
    renderHook(() => useLiveEvents(vi.fn()));
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
    renderHook(() => useLiveEvents(vi.fn()));
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
    renderHook(() => useLiveEvents(vi.fn()));
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
