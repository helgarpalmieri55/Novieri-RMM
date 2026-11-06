// Live status: WebSocket to /api/v1/ws, events published by gateway/API.
//
// THE SESSION COOKIE IS THE CREDENTIAL (own-auth design §6.5, plan decision
// D15). The browser sends `__Host-itms_session` with this same-origin
// handshake as it does with every request; the server checks it and the
// handshake's `Origin`, and re-checks the open socket every 60 s. NO TOKEN
// TRAVELS: not in the URL, which an access log records verbatim, and no
// longer in the subprotocol list, which carried Keycloak's five-minute access
// token and made the console swap to a new socket on every renewal.
//
// 1. AFTER ANY UNPLANNED CLOSE the next open delivers `{kind: "resync"}`
//    (`RESYNC_KIND`): events may have been missed, and the page re-reads
//    what it keeps fresh from them.
// 2. A 4401 SAYS THE SESSION THE SOCKET WAS CHECKED AGAINST IS NOT THE ONE IN
//    THE COOKIE ANY MORE. Either it ended -- inactivity, the maximum, a
//    sign-out elsewhere, an administrator -- or its identifier rotated: the
//    person changed their password, which gives the caller's own session a
//    new token (final review B, Important 1). A step-up does not rotate it,
//    and the sign-in's own rotation (`mark_factor`) happens before any socket
//    opens. So the console asks `/me` again: a 401 there ends the session in
//    this tab (`./sessionEnd`) and no socket opens again; an answer naming the
//    same person means the cookie holds a live session, and the socket
//    reopens at once on it; an answer naming anybody else is this session
//    ending too (`sessionPerson`: the same rule the guard's `/me` applies).
// 3. Any other unplanned close keeps the doubling backoff (2 s, 4 s ... 30 s).
// 4. THE CHOSEN CLIENT (phase 3 design §1.5) travels as `?client=<id>` -- a
//    UUID, not a credential, so the query string is the right place for it --
//    and the socket opens again when the choice changes. A 4403 on an OPEN
//    socket says the scope is no longer the one subscribed: access to that
//    client went, or a grant changed (`backend/app/api/v1/ws.py` re-checks
//    every `RECHECK_INTERVAL_S`). The console asks `/me` again, which moves
//    the top bar back to every client when the client is gone
//    (`lib/clientScope.tsx`), and reconnects at once.
//    ONE IMMEDIATE RECONNECT PER QUIET SPELL, for 4401 and 4403 alike: when
//    the socket reopened that way closes 4401 or 4403 again before it has
//    delivered anything, the next attempt backs off. A server that accepts
//    every handshake and then refuses it (an `Origin` it does not expect,
//    say) cannot make this spin; an event delivered ends the spell.
// 5. A HANDSHAKE THE SERVER REFUSES NEVER ARRIVES AS A CLOSE CODE OF ITS OWN.
//    It refuses before `accept()` -- no session, a foreign `Origin`, an
//    identity not admitted, an empty scope -- so uvicorn answers the upgrade
//    with HTTP 403, and all a browser reports is a close before open, 1006:
//    the same as a server that is down. So a socket that closes without ever
//    opening asks `/me` again ONCE, until a socket next opens -- a session or
//    a client that went while the socket was down is noticed, and an outage
//    does not become a `/me` per retry -- and backs off like any other close:
//    reconnecting at once to a handshake the server has just refused would
//    loop.
// 6. NO SOCKET WHILE THE SESSION HAS ENDED (`./sessionEnd`), and none in
//    fixture mode, which has no server to stream from.

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useSyncExternalStore } from "react";
import { FIXTURE_MODE } from "../fixtures/mode";
import { selectedClient, subscribeClient } from "./clientChoice";
import { RESYNC_KIND } from "./resync";
import type { Me } from "./api";
import { isSessionEnded, sessionPerson, subscribeSessionEnd } from "./sessionEnd";

export interface UiEvent {
  kind: string;
  job_id?: string;
  state?: string;
  [key: string]: unknown;
}

/** The server's close code for a session that is not (or no longer) the one the socket was checked against. */
export const CLOSE_SESSION_ENDED = 4401;
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
  const ended = useSyncExternalStore(subscribeSessionEnd, isSessionEnded, isSessionEnded);

  useEffect(() => {
    if (FIXTURE_MODE || ended) return undefined;
    let disposed = false;
    // The socket whose loss is a gap.
    let current: WebSocket | null = null;
    // Sockets the server accepted. A close on one that never got here is a
    // refused (or failed) handshake, whatever the server meant (point 5).
    const accepted = new WeakSet<WebSocket>();
    // `/me` asked again for this run of closes-before-open (point 5).
    let askedMe = false;
    // Reconnected at once after a 4401 or a 4403, and nothing delivered
    // since (point 4): the next such close backs off.
    let rechecked = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryMs = INITIAL_RETRY_MS;
    // Events may have been missed: the next open says so.
    let gap = false;

    const deliver = (event: UiEvent) => handler.current(event);

    const opened = () => {
      retryMs = INITIAL_RETRY_MS;
      askedMe = false;
      if (gap) {
        gap = false;
        deliver({ kind: RESYNC_KIND });
      }
    };

    const backoff = () => {
      retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
      retryTimer = setTimeout(() => connect(), retryMs);
    };

    // A 4401 (point 2) or a 4403 (point 4) on an open socket: `/me` first,
    // then at once -- unless `/me` said the session ended.
    //
    // `disposed` IS NOT A PROMISE THAT THE FALLBACK HAS LANDED. The await
    // ends when the refetch has answered, but TanStack hands the answer to
    // its observers through `setTimeout(0)` (its `notifyManager`), so the
    // provider may not have re-rendered: `disposed` can still be false and
    // the choice still the refused client. Where the choice has already
    // moved, `connect()` sees it and opens nothing. Where it has not, the
    // socket opened here is refused or closed by this effect's cleanup once
    // the fallback lands -- a planned close, no gap and no reconnect.
    const recheck = async () => {
      // WHO THE TAB WAS BEFORE: a cookie that now holds ANOTHER person's
      // session (someone signed in on this browser in another tab) is this
      // session ending, not a rotation -- reconnecting would stream the new
      // person's events into the previous person's cache. ONE RULE for this
      // and the guard's own `/me` (`./sessionEnd.ts::sessionPerson`, final
      // review D1, Minor 3), checked here before reconnecting: the guard
      // hears the refetch only after TanStack's next tick.
      const before = queryClient.getQueryData<Me>(["me"])?.user_id;
      if (before !== undefined) sessionPerson(before);
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      if (disposed) return;
      const after = queryClient.getQueryData<Me>(["me"])?.user_id;
      if (after !== undefined) sessionPerson(after);
      // A 401 from `/me`, or another person: either way the session ended,
      // and the cache was emptied with it.
      if (isSessionEnded()) return;
      if (rechecked) {
        backoff();
        return;
      }
      rechecked = true;
      connect();
    };

    const lost = (socket: WebSocket, code: number) => {
      if (disposed || socket !== current) return;
      current = null;
      gap = true;
      if (!accepted.has(socket)) {
        // Never opened (point 5): `/me` once, then the backoff.
        if (!askedMe) {
          askedMe = true;
          void queryClient.invalidateQueries({ queryKey: ["me"] });
        }
        backoff();
      } else if (code === CLOSE_SESSION_ENDED || code === CLOSE_SCOPE_REFUSED) void recheck();
      else backoff();
    };

    const connect = () => {
      retryTimer = null;
      // Not for a session that has ended, nor for a client the choice has
      // already left: this effect is about to stop, or to start over with
      // the new client (see `recheck`).
      if (disposed || isSessionEnded() || selectedClient() !== client) return;
      const socket = new WebSocket(liveUrl(client));
      current = socket;
      socket.onmessage = (message) => {
        if (socket !== current) return;
        rechecked = false;
        try {
          deliver(JSON.parse(message.data as string) as UiEvent);
        } catch {
          // ignore malformed frames
        }
      };
      socket.onopen = () => {
        accepted.add(socket);
        opened();
      };
      socket.onclose = (event) => lost(socket, event.code);
    };

    connect();
    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      current?.close();
    };
    // A new choice is a new socket, and an ended session is none: the old
    // one is closed by this cleanup, with `disposed` set, so its close
    // schedules nothing.
  }, [client, ended, queryClient]);
}
