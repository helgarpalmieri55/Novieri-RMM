/**
 * WHETHER THIS TAB'S SESSION HAS ENDED, as a value outside React (own-auth
 * design §1.1, §2.4).
 *
 * The session is the `__Host-itms_session` cookie, which no script on the page
 * can read (`HttpOnly`). So the console learns that it is over in one way
 * only: the server answers 401. `ApiClient` reports every such answer here
 * (`ApiProvider`'s `onUnauthorized`), and what has to stop reads it: the guard
 * and the shell's notice (`lib/session.ts::useSession`), the live socket
 * (`lib/ws.ts`), the screen viewer and the language save.
 *
 * ONE WAY, FOR THE LIFE OF THE PAGE. Nothing here un-ends a session. Signing
 * in again is a whole-document load (`lib/session.ts::enterConsole`), which
 * starts this module at `false`; a test starts from `false` through
 * `resetSessionEnd` (`src/test/setup.ts`).
 *
 * APART FROM `lib/session.ts` because `ApiContext.tsx` writes this and
 * `lib/session.ts` reads `useApi` from `ApiContext.tsx`: one module holding
 * both would be an import cycle -- the reason `clientChoice.ts` sits apart
 * from `clientScope.tsx`.
 */

let ended = false;
const listeners = new Set<() => void>();

/** The server answered 401: the session this tab used is gone. Listeners hear it once. */
export function sessionEnded(): void {
  if (ended) return;
  ended = true;
  for (const listener of listeners) listener();
}

export function isSessionEnded(): boolean {
  return ended;
}

/** For `useSyncExternalStore`. Returns an unsubscribe function. */
export function subscribeSessionEnd(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** For tests: the next test starts with a session that has not ended. */
export function resetSessionEnd(): void {
  ended = false;
}
