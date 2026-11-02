/**
 * A CHANGE OF CLIENT MADE THROUGH THE SELECTOR, remembered across the remount
 * it causes (WCAG 2.4.3, 3.2.2).
 *
 * A change starts the page again -- `lib/clientScope.tsx`'s keyed fragment --
 * and the shell with it, so the `<select>` that fired `change` is unmounted
 * and focus falls to `<body>`. Chrome fires `change` on every arrow key on a
 * closed select: a keyboard user stepping through the list was thrown to the
 * top of the document on the first press. The remounted selector takes focus
 * back (`ClientSelector`), and on a handset the shell keeps the drawer it is
 * in open (`AppShell`) -- but only for a change the selector made. One that
 * arrived from another tab (`storage`), or the fallback when `/me` no longer
 * lists the choice, is not the person's act here and moves nobody's focus.
 *
 * A module value, not React state: it has to outlive the very components
 * that read it. It names the choice it was made for, so a mark nobody took
 * cannot fire for a later change that came some other way.
 */

import { selectedClient } from "../lib/clientChoice";

let mark: { readonly choice: string | null } | null = null;

/** The selector is about to make `choice` the client (`null`: every client). */
export function markSelectorChange(choice: string | null): void {
  mark = { choice };
}

/** The current choice is one the selector made, and its remounted self has not taken the mark yet. */
export function selectorChangePending(): boolean {
  return mark !== null && mark.choice === selectedClient();
}

/** Take the mark, once: true when the current choice is one the selector made. */
export function takeSelectorChange(): boolean {
  const pending = selectorChangePending();
  mark = null;
  return pending;
}
