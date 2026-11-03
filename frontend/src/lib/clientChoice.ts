/**
 * THE CLIENT CHOSEN IN THE TOP BAR, as a value outside React (phase 3 design
 * §1.1, §1.2, §1.5). The two things that send it are not components:
 * `ApiClient` reads it on every request for `X-Client-Id`, and the live
 * socket for `?client=`. Components read and change it through
 * `lib/clientScope.tsx`, which also empties the query cache on every change.
 * The same shape as `i18n/core.ts`'s active language, which `requestHeaders`
 * already reads.
 *
 * `null` is "All my clients": no header, no `?client=`, the person's whole
 * scope. Kept in `localStorage["itms.client"]` as the id or `all`; project 4's
 * rename of the brand renames it with `itms.sidebar.collapsed`.
 *
 * ONLY AN ID IS READ BACK. The server answers a malformed `X-Client-Id` with
 * 400 on every route, `/me` included (`backend/app/api/deps.py`), so a stored
 * value it cannot read would leave the console unable even to learn that it
 * should drop it. Anything else stored there reads as every client.
 *
 * INVARIANT 6: THIS GATES NOTHING. The header narrows the person's scope and
 * never widens it: a client outside it answers empty lists and 404s.
 */

export const CLIENT_STORAGE_KEY = "itms.client";

const EVERY_CLIENT = "all";
const CLIENT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The stored choice: a client id, or `null` for every client (and for anything unreadable). */
export function readStoredClient(): string | null {
  // Storage throws in Safari private browsing and in a webview with site
  // data blocked (`shell/AppShell.tsx`'s `readCollapsed`): every client, then.
  try {
    const stored = window.localStorage.getItem(CLIENT_STORAGE_KEY);
    return stored !== null && CLIENT_ID.test(stored) ? stored : null;
  } catch {
    return null;
  }
}

function writeStoredClient(id: string | null): void {
  try {
    window.localStorage.setItem(CLIENT_STORAGE_KEY, id ?? EVERY_CLIENT);
  } catch {
    // Storage blocked: the choice lasts as long as this tab.
  }
}

let active: string | null = readStoredClient();
const listeners = new Set<() => void>();

/** The client chosen now, or `null` for every client. */
export function selectedClient(): string | null {
  return active;
}

/** Told of every change, synchronously, before React renders anything for it. */
export function subscribeClient(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function apply(id: string | null): void {
  if (id === active) return;
  active = id;
  listeners.forEach((listener) => listener());
}

/** Choose a client, or `null` for every client: stored, and sent from the next request on. */
export function setSelectedClient(id: string | null): void {
  writeStoredClient(id);
  apply(id);
}

/** Take what another tab stored (the browser's `storage` event), without writing it back. */
export function adoptStoredClient(): void {
  apply(readStoredClient());
}
