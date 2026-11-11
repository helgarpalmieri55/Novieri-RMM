/**
 * A 401 IS CONFIRMED BEFORE IT ENDS THE SESSION (final re-review, I1).
 *
 * A request can be answered 401 while the session lives on: a password change
 * gives the caller's session a new token in its own transaction (K2), and a
 * request that left with the old cookie a moment earlier -- the shell's poll,
 * another tab's refetch -- is answered after the commit. Believing that one
 * answer hid the page, emptied the cache and sent a person whose session was
 * alive to sign in again.
 *
 * SO HTTP GETS THE SOCKET'S RULE (`./ws.ts`, point 2): one `/me`, asked with
 * whatever cookie the browser holds now.
 *
 *   - 401: the session ended (`sessionEnded`);
 *   - an answer naming somebody else: it ended too (`sessionPerson`, C3);
 *   - an answer for the same person: it did not -- the request that was
 *     refused stays refused, and TanStack's retry asks a query again;
 *   - no answer at all (the network, an outage): nothing is decided.
 *
 * ONE CHECK AT A TIME: a burst of 401s shares the one in flight. The check is
 * a raw `fetch`, not `ApiClient`, so its own 401 cannot start another check.
 * It carries the console's usual headers, the background mark included
 * (`./api.ts::requestHeaders`): a check nobody's input caused renews nothing.
 */
import { requestHeaders } from "./api";
import { isSessionEnded, sessionEnded, sessionPerson } from "./sessionEnd";

let inFlight: Promise<void> | null = null;

async function check(base: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${base}/me`, {
      // Global: `/me` is the person, whichever client the top bar names.
      headers: requestHeaders(undefined, { method: "GET", path: "/me", client: null, global: true }),
      credentials: "same-origin",
    });
  } catch {
    return;
  }
  if (response.status === 401) {
    sessionEnded();
    return;
  }
  if (!response.ok) return;
  try {
    const me = (await response.json()) as { user_id?: unknown };
    if (typeof me.user_id === "string") sessionPerson(me.user_id);
  } catch {
    // A body that does not read is no evidence either way.
  }
}

/** A request with the session was answered 401: find out whether the session really ended. */
export function confirmSessionEnded(base: string = "/api/v1"): Promise<void> {
  if (isSessionEnded()) return Promise.resolve();
  if (inFlight === null) {
    inFlight = check(base).finally(() => {
      inFlight = null;
    });
  }
  return inFlight;
}
