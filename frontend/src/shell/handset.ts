/**
 * THE HANDSET WIDTH, for chrome that MOVES at it rather than restyling.
 *
 * Everything else in the shell answers a narrow screen in `shell.css` alone.
 * The client selector cannot: at 390px the top bar's other five controls take
 * 256 of the 274px its cluster has, so a select there got 8px and still
 * pushed the bar to 415px -- a sideways page scroll on every route, which
 * harmony measured in both languages. A control that cannot fit has to be
 * drawn somewhere else, and only the component tree can draw it somewhere
 * else: on a handset `AppShell` hands the selector to the drawer instead of
 * the bar. ONE element either way, never two kept in step by `display: none`.
 *
 * THE SAME QUERY AS `shell.css`'s 640px block, which styles the selector in
 * the drawer; `handset.test.ts` holds the two to one number.
 *
 * NO `matchMedia` (jsdom) READS AS "NOT A HANDSET": the desktop bar every
 * other shell test was written against.
 */

import { useSyncExternalStore } from "react";

export const HANDSET_QUERY = "(max-width: 640px)";

function media(): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(HANDSET_QUERY) : null;
}

function subscribe(listener: () => void): () => void {
  const list = media();
  if (list === null) return () => undefined;
  list.addEventListener("change", listener);
  return () => list.removeEventListener("change", listener);
}

function snapshot(): boolean {
  return media()?.matches ?? false;
}

/** True while the viewport is a handset's (`(max-width: 640px)`), and re-rendered when it stops being one. */
export function useHandset(): boolean {
  return useSyncExternalStore(subscribe, snapshot, () => false);
}

/**
 * THE DRAWER WIDTH (`(max-width: 1024px)`, `shell.css`'s 1024px block): below
 * it the rail is a full-width drawer and the stored fold is reverted, so the
 * rail's components draw their unfolded form there (Task 4 review, fix round
 * 1: a folded desktop rail used to bring its folded Admin panel into the
 * drawer, without search or accordion). `handset.test.ts` holds the two to one
 * number. No `matchMedia` (jsdom) reads as "not the drawer".
 */
export const DRAWER_QUERY = "(max-width: 1024px)";

function drawerMedia(): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(DRAWER_QUERY) : null;
}

function subscribeDrawer(listener: () => void): () => void {
  const list = drawerMedia();
  if (list === null) return () => undefined;
  list.addEventListener("change", listener);
  return () => list.removeEventListener("change", listener);
}

/** True while the viewport is the drawer's (`(max-width: 1024px)`), and re-rendered when it stops being one. */
export function useDrawer(): boolean {
  return useSyncExternalStore(subscribeDrawer, () => drawerMedia()?.matches ?? false, () => false);
}
