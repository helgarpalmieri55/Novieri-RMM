// Live status: WebSocket to /api/v1/ws, events published by gateway/API.
//
// THE SOCKET OUTLIVES ITS TOKEN, AND MUST NOT LOSE EVENTS WHEN IT DOES (final
// review W1). The server re-checks an open socket and closes it 4401 once the
// token it was opened with has expired; Keycloak's access tokens live five
// minutes. A socket that kept its first token therefore closed every five
// minutes, waited out a backoff, and every event published in that gap was
// gone -- and the pages that refresh only on live events (jobs, a device's
// page) kept showing a finished job as running. So:
//
// 1. ON A RENEWED TOKEN (`onTokenRenewed`) a NEW socket opens with it, and
//    the old one is closed only once the new one is open -- after a short
//    grace (`SWAP_GRACE_MS`) during which both deliver, because the server
//    subscribes to its channels just after it accepts the handshake. The old
//    socket is RETIRED: its close schedules no reconnect and is no gap.
//    Every consumer's handler is an idempotent invalidation, so an event
//    heard twice in that second costs a refetch, never a wrong screen.
// 2. AFTER ANY UNPLANNED CLOSE the next open delivers `{kind: "resync"}`
//    (`RESYNC_KIND`): events may have been missed, and the page re-reads
//    what it keeps fresh from them.
// 3. A 4401 RECONNECTS AT ONCE with a newer token -- the one the renewal
//    already fetched, or one renewed now -- instead of waiting out a backoff.
//    With no newer token to be had it backs off like any other close, so an
//    ended session cannot spin.
// 4. Any other unplanned close keeps the doubling backoff (2 s, 4 s ... 30 s).
// 5. THE CHOSEN CLIENT (phase 3 design §1.5) travels as `?client=<id>` -- a
//    UUID, not a credential, so the query string is the right place for it --
//    and the socket opens again when the choice changes. A 4403 says the
//    scope is empty or is no longer the one subscribed: access to that client
//    went, or a grant changed. The console asks `/me` again, which moves the
//    top bar back to every client when the client is gone
//    (`lib/clientScope.tsx`), and reconnects AT ONCE when the refused socket
//    had been open. One refused at the handshake backs off instead:
//    reconnecting at once to a scope the server has just refused would loop.

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useSyncExternalStore } from "react";
import { accessToken, onTokenRenewed, renewAccessToken } from "./auth";
import { selectedClient, subscribeClient } from "./clientChoice";
import { RESYNC_KIND } from "./resync";

export interface UiEvent {
  kind: string;
  job_id?: string;
  state?: string;
  [key: string]: unknown;
}

/** How long a replaced socket keeps delivering after its successor opened. */
export const SWAP_GRACE_MS = 1000;
/** The server's close code for a token that is not (or no longer) valid. */
export const CLOSE_TOKEN_EXPIRED = 4401;
/** The server's close code for a scope that is empty, or no longer the one subscribed. */
export const CLOSE_SCOPE_REFUSED = 4403;
const INITIAL_RETRY_MS = 1000;
const MAX_RETRY_MS = 30_000;

/** The socket's address: `?client=` names the chosen client, and is absent for every client. */
export function liveUrl(client: string | null): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const query = client === null ? "" : `?client=${encodeURIComponent(client)}`;
  return `${scheme}://${window.location.host}/api/v1/ws${query}`;
}

export function useLiveEvents(onEvent: (event: UiEvent) => void): void {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  const queryClient = useQueryClient();
  const client = useSyncExternalStore(subscribeClient, selectedClient, selectedClient);

  useEffect(() => {
    let disposed = false;
    // The socket whose loss is a gap.
    let current: WebSocket | null = null;
    // A replacement still opening, after a token renewal.
    let pending: WebSocket | null = null;
    // Replaced or abandoned on purpose: their close is planned.
    const retired = new Set<WebSocket>();
    // Sockets the server accepted: a 4403 on one of these is a scope that
    // changed under it, not one refused at the handshake.
    const accepted = new WeakSet<WebSocket>();
    const graceTimers = new Set<ReturnType<typeof setTimeout>>();
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryMs = INITIAL_RETRY_MS;
    // Events may have been missed: the next open says so.
    let gap = false;

    const deliver = (event: UiEvent) => handler.current(event);

    const opened = () => {
      retryMs = INITIAL_RETRY_MS;
      if (gap) {
        gap = false;
        deliver({ kind: RESYNC_KIND });
      }
    };

    // The token goes in the subprotocol list, NEVER in the URL: a query
    // string is written verbatim to the server's access log, and no
    // logging-side redaction can reach it. The server requires exactly
    // ["bearer", token] and echoes back "bearer".
    const socketWith = (token: string): WebSocket => {
      const socket = new WebSocket(liveUrl(client), ["bearer", token]);
      socket.onmessage = (message) => {
        if (socket !== current && !retired.has(socket)) return;
        try {
          deliver(JSON.parse(message.data as string) as UiEvent);
        } catch {
          // ignore malformed frames
        }
      };
      return socket;
    };

    const backoff = () => {
      retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
      retryTimer = setTimeout(() => connect(), retryMs);
    };

    const reauthenticate = async (stale: string) => {
      let token = accessToken();
      if (!token || token === stale) token = await renewAccessToken();
      if (disposed) return;
      if (token && token !== stale) connect(token);
      else backoff();
    };

    // A 4403 (point 5): `/me` first. When the chosen client is gone the top
    // bar falls back to every client and this effect starts over with that
    // choice, so `disposed` is set by the time the refetch has answered.
    const rescope = async (wasOpen: boolean) => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      if (disposed) return;
      if (wasOpen) connect();
      else backoff();
    };

    const lost = (socket: WebSocket, token: string, code: number) => {
      // A retired socket's close was planned: forget it, schedule nothing.
      if (retired.delete(socket)) return;
      if (disposed || socket !== current) return;
      current = null;
      gap = true;
      if (code === CLOSE_TOKEN_EXPIRED) void reauthenticate(token);
      else if (code === CLOSE_SCOPE_REFUSED) void rescope(accepted.has(socket));
      else backoff();
    };

    const connect = (given?: string) => {
      retryTimer = null;
      const token = given ?? accessToken();
      if (!token || disposed) return;
      const socket = socketWith(token);
      current = socket;
      socket.onopen = () => {
        accepted.add(socket);
        opened();
      };
      socket.onclose = (event) => lost(socket, token, event.code);
    };

    const renewed = (token: string) => {
      // No live socket: the reconnect already under way reads the new token.
      if (disposed || current === null) return;
      if (pending) {
        retired.add(pending);
        pending.close();
      }
      const next = socketWith(token);
      pending = next;
      next.onopen = () => {
        if (disposed || pending !== next) return;
        accepted.add(next);
        pending = null;
        const old = current;
        current = next;
        if (retryTimer) {
          clearTimeout(retryTimer);
          retryTimer = null;
        }
        if (old) {
          retired.add(old);
          const timer = setTimeout(() => {
            graceTimers.delete(timer);
            old.close();
          }, SWAP_GRACE_MS);
          graceTimers.add(timer);
        }
        opened();
      };
      next.onclose = (event) => {
        if (pending === next) {
          // It never opened: the old socket carries on, and the next
          // renewal (or its own 4401) tries again.
          pending = null;
          return;
        }
        lost(next, token, event.code);
      };
    };

    const unsubscribe = onTokenRenewed(renewed);
    connect();
    return () => {
      disposed = true;
      unsubscribe();
      if (retryTimer) clearTimeout(retryTimer);
      for (const timer of graceTimers) clearTimeout(timer);
      current?.close();
      pending?.close();
      for (const socket of retired) socket.close();
    };
    // A new choice is a new socket: the old one is closed by this cleanup,
    // with `disposed` set, so its close schedules nothing.
  }, [client, queryClient]);
}
