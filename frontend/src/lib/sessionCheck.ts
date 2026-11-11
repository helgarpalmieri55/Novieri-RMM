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
 * A client's contact is answered 403 `portal_only` by `/me`; for them, and
 * only then, `/portal/me` is asked instead (own-auth phase 2, Task 10).
 *
 * ONE CHECK AT A TIME: a burst of 401s shares the one in flight. The check is
 * a raw `fetch`, not `ApiClient`, so its own 401 cannot start another check.
 * It carries the console's usual headers, the background mark included
 * (`./api.ts::requestHeaders`): a check nobody's input caused renews nothing.
 */
import { requestHeaders } from "./api";
import { isSessionEnded, sessionEnded, sessionPerson } from "./sessionEnd";

let inFlight: Promise<void> | null = null;

async function ask(base: string, path: string): Promise<Response | null> {
  try {
    return await fetch(`${base}${path}`, {
      // Global: the person, whichever client the top bar names.
      headers: requestHeaders(undefined, { method: "GET", path, client: null, global: true }),
      credentials: "same-origin",
    });
  } catch {
    return null;
  }
}

/** The refusal's `code`, or `null` when the body says none. */
async function codeOf(response: Response): Promise<string | null> {
  try {
    const body = (await response.clone().json()) as { code?: unknown };
    return typeof body.code === "string" ? body.code : null;
  } catch {
    return null;
  }
}

async function check(base: string): Promise<void> {
  let response = await ask(base, "/me");
  if (response === null) return;
  // A CLIENT'S CONTACT (own-auth phase 2, Task 10, decision 4): the console's
  // door answers them 403 `portal_only`, which says nothing about their
  // session; the portal's own `/me` does, under the same two rules.
  if (response.status === 403 && (await codeOf(response)) === "portal_only") {
    response = await ask(base, "/portal/me");
    if (response === null) return;
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
