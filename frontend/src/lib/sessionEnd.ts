/**
 * WHETHER THIS TAB'S SESSION HAS ENDED, as a value outside React (own-auth
 * design §1.1, §2.4).
 *
 * The session is the `__Host-itms_session` cookie, which no script on the page
 * can read (`HttpOnly`). So the console learns that it is over in three ways,
 * and every one of them lands here:
 *
 *   - THE SERVER ANSWERS 401, AND `/me` CONFIRMS IT. `ApiClient` reports
 *     every such answer (`ApiProvider`'s `onUnauthorized`), and one raw `/me`
 *     decides (`./sessionCheck.ts`, final re-review I1): a request that left
 *     with the cookie a password change just rotated is answered 401 while
 *     the session lives on. The 30 minutes of inactivity, the 12-hour
 *     maximum, an administrator, a sign-out in another tab: all confirmed. A
 *     `/ws` 4401 is learnt the same way: the socket asks `/me` (`./ws.ts`).
 *   - `/me` NAMES SOMEBODY ELSE (`sessionPerson`, final review D1, Minor 3).
 *     Someone signed in on this browser in another tab; the cookie now holds
 *     their session, and this tab's person is gone. One rule for every path
 *     that reads `/me`: the guard's own query (`./session.ts::useSession`)
 *     and the socket's re-check alike.
 *   - THE PERSON SIGNED OUT here (`sessionSignedOut`, `./session.ts::signOut`).
 *
 * What has to stop reads it: the guard, which hides the page and empties the
 * query cache (`App.tsx`'s `RequireAuth`, operator decision 3, 2026-11-05),
 * the live socket (`./ws.ts`), the screen viewer and the language save.
 *
 * ONE WAY, FOR THE LIFE OF THE PAGE. Nothing here un-ends a session. Signing
 * in again is a whole-document load (`./session.ts::enterConsole`), which
 * starts this module at "not ended" with nobody known; a test starts there
 * through `resetSessionEnd` (`src/test/setup.ts`).
 *
 * APART FROM `./session.ts` because `ApiContext.tsx` writes this and
 * `./session.ts` reads `useApi` from `ApiContext.tsx`: one module holding
 * both would be an import cycle -- the reason `clientChoice.ts` sits apart
 * from `clientScope.tsx`.
 */

/**
 * How the session ended: under the person (`ended`); because somebody else's
 * session is in the cookie now (`replaced`: the person at the browser is not
 * the one who typed, final re-review M4); or because they signed out here
 * (`signed_out`).
 */
export type SessionEndReason = "ended" | "replaced" | "signed_out";

let reason: SessionEndReason | null = null;
/** The person `/me` first named in this tab, or `null` until it has answered. */
let person: string | null = null;
const listeners = new Set<() => void>();

function end(why: SessionEndReason): void {
  if (reason !== null) return;
  reason = why;
  for (const listener of listeners) listener();
}

/** The session this tab used is gone. Listeners hear it once, synchronously, before React renders again. */
export function sessionEnded(): void {
  end("ended");
}

/** The person signed out here: the server ended the session they asked it to end. */
export function sessionSignedOut(): void {
  end("signed_out");
}

export function isSessionEnded(): boolean {
  return reason !== null;
}

/** How it ended, or `null` while it has not. The first word stands. */
export function sessionEndReason(): SessionEndReason | null {
  return reason;
}

/**
 * `/me` answered for `userId`. The first answer names this tab's person; a
 * later one naming anybody else ends the session -- reconnecting, or drawing
 * the page again, would put the new person's data under the previous
 * person's page and form.
 */
export function sessionPerson(userId: string): void {
  if (person === null) {
    person = userId;
    return;
  }
  if (person !== userId) end("replaced");
}

/**
 * Whether `/me` ever answered in this tab. An ended session that had one is
 * a person who was HERE (the guard shows the notice); one that never had
 * one is a visitor with no session (the guard sends them to sign in).
 */
export function hadSession(): boolean {
  return person !== null;
}

/** For `useSyncExternalStore`. Returns an unsubscribe function. */
export function subscribeSessionEnd(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** For tests: the next test starts with a session that has not ended, and nobody known. */
export function resetSessionEnd(): void {
  reason = null;
  person = null;
}
