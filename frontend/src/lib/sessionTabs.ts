/**
 * WHAT ONE TAB TELLS THE OTHERS WHEN ITS SESSION ENDS (final re-review, M1).
 *
 * Every tab of this browser shares the one session cookie, and a tab that has
 * learnt its session ended was the only one that knew: a second window left
 * visible kept the tenant's page drawn until its own next request -- and for
 * ever, on a page that asks nothing. OWASP Session Management Cheat Sheet:
 * take "active actions to invalidate the session on both sides".
 *
 * A `BroadcastChannel` where the browser has one, else a `storage` event (a
 * key written and removed at once: other documents of the origin hear the
 * write, the writer does not). Two notices:
 *
 *   - `signed_out`: the person signed out; every other tab ends at once;
 *   - `ended`: any other end; every other tab asks `/me` itself
 *     (`./sessionCheck.ts`), because the cookie is the same and its answer
 *     decides -- a person change in one tab is not one in a tab already on
 *     the new person.
 *
 * Each tab says it once (`./sessionEnd.ts` ends once), so the tabs that end
 * on hearing it, and say so in turn, stop when every tab has ended. A tab
 * never hears its own word: the one channel object both sends and listens.
 *
 * Nothing secret travels: two fixed words.
 */

export const SESSION_CHANNEL = "itms.session";
export const SESSION_STORAGE_KEY = "itms.session-ended";

export type TabNotice = "signed_out" | "ended";

function isNotice(value: unknown): value is TabNotice {
  return value === "signed_out" || value === "ended";
}

/** The tab's one channel, `null` where the browser has none; `undefined` until asked. */
let channel: BroadcastChannel | null | undefined;

function sharedChannel(): BroadcastChannel | null {
  if (channel !== undefined) return channel;
  try {
    channel = typeof BroadcastChannel === "function" ? new BroadcastChannel(SESSION_CHANNEL) : null;
    // Node (the test runner) keeps a process alive for an open channel;
    // browsers have no `unref`.
    (channel as unknown as { unref?: () => void } | null)?.unref?.();
  } catch {
    channel = null;
  }
  return channel;
}

/** Tell the other tabs this tab's session ended, and how. */
export function tellOtherTabs(notice: TabNotice): void {
  const shared = sharedChannel();
  if (shared !== null) {
    try {
      shared.postMessage(notice);
      return;
    } catch {
      // Fall back to storage below.
    }
  }
  try {
    window.localStorage.setItem(SESSION_STORAGE_KEY, `${notice} ${Date.now()}`);
    window.localStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // Storage blocked: the other tabs learn it at their next request.
  }
}

/** Listen for the other tabs. Returns the function that stops listening. */
export function hearOtherTabs(win: Window, onNotice: (notice: TabNotice) => void): () => void {
  const shared = sharedChannel();
  if (shared !== null) {
    const onMessage = (event: MessageEvent) => {
      if (isNotice(event.data)) onNotice(event.data);
    };
    shared.addEventListener("message", onMessage);
    return () => shared.removeEventListener("message", onMessage);
  }
  const onStorage = (event: StorageEvent) => {
    if (event.key !== SESSION_STORAGE_KEY || event.newValue === null) return;
    const word = event.newValue.split(" ")[0];
    if (isNotice(word)) onNotice(word);
  };
  win.addEventListener("storage", onStorage);
  return () => win.removeEventListener("storage", onStorage);
}

/** For tests: forget the channel, so the next one is made afresh (or not, with no `BroadcastChannel`). */
export function resetSessionTabs(): void {
  channel?.close();
  channel = undefined;
}
