/**
 * The synthetic live event a page hears after its live socket had a GAP
 * (final review W1): the socket was down, events published meanwhile were
 * not delivered, and anything the page keeps fresh from live events must be
 * re-read. Its own module, not `ws.ts`, so a page test that stubs the socket
 * hook still has the constant.
 */
export const RESYNC_KIND = "resync";
