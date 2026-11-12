/**
 * The consent page's one rule about the request: it does not read it. The
 * query goes to the server as the browser has it (plan 2026-11-06, Task 4,
 * decision 1), so `/oauth/authorize?a=1` sends `a=1`.
 */
export function authorizationQuery(search: string): string {
  return search.startsWith("?") ? search.slice(1) : search;
}

/**
 * Whether this document is inside another one (Task 4, decision 6): the
 * frame-busting fallback for a browser that ignores `frame-ancestors`. A
 * cross-origin parent makes `window.top` unreadable, which is also "framed".
 */
export function isFramed(): boolean {
  try {
    return window.top !== window.self;
  } catch {
    return true;
  }
}
