/**
 * THE TOP BAR'S CLIENT SELECTOR (phase 3 design §1.1).
 *
 * "All my clients" first, then the person's clients from `/me` by name, the
 * Unassigned client last -- `/me` lists it only for someone holding a role on
 * every client, the same rule as `GET /clients`. Archived clients are not in
 * `/me`, so they are not here. A choice is stored (`itms.client`) and narrows
 * every request from then on (`X-Client-Id`); the provider empties the cache
 * and starts the page again (`lib/clientScope.tsx`).
 *
 * NOT DRAWN when `/me` lists no client: there is nothing to choose between,
 * and a select with one option is furniture. While `/me` is asked again after
 * a change it stays drawn from the list it had (`useClientScope`).
 *
 * FOCUS COMES BACK TO IT after a change it made: starting the page again
 * unmounts the select that fired `change`, and the one drawn in its place
 * takes focus once (`./selectorChange`). A change from another tab does not.
 *
 * On a handset it heads the drawer instead of the bar (`./handset`), and the
 * bar's drawer toggle marks the client chosen (`useClientMark`).
 *
 * A NATIVE `<select>` in the kit's `.select-field`, the control the filter
 * bars already use: this phase adds the selector, it does not restyle the
 * chrome (project 3 does).
 */

import { useEffect, useRef } from "react";

import { useT } from "../i18n/context";
import type { MeClient } from "../lib/api";
import { useClientScope } from "../lib/clientScope";
import { initialsOf } from "../lib/people";
import { markSelectorChange, takeSelectorChange } from "./selectorChange";

/** The option standing for every client, the choice `null`. Never a client id. */
const EVERY_CLIENT = "all";

/** A client as the selector names it: Unassigned in the person's language. */
function nameOf(client: MeClient, t: ReturnType<typeof useT>): string {
  return client.kind === "unassigned" ? t("lib.clients.unassigned") : client.name;
}

/**
 * THE CHOSEN CLIENT, FOR THE PHONE'S TOP BAR (operator decision 4,
 * 2026-11-02): its initials and its name, as the selector names it, for
 * `AppShell`'s `clientMark`. `undefined` with every client chosen, or a
 * choice the person's clients do not list.
 *
 * Read from the same list the selector draws (`useClientScope`), which is
 * kept while `/me` is asked again after a change (`LastListed`): the mark
 * stays on screen through the change instead of blinking off.
 */
export function useClientMark(): { short: string; name: string } | undefined {
  const t = useT();
  const { selected, clients } = useClientScope();
  const chosen = selected === null ? undefined : clients.find((client) => client.id === selected);
  if (chosen === undefined) return undefined;
  const name = nameOf(chosen, t);
  return { short: initialsOf(name), name };
}

export function ClientSelector() {
  const t = useT();
  const { selected, select, clients } = useClientScope();
  const box = useRef<HTMLSelectElement>(null);
  const drawn = clients.length > 0;
  // On the render that first draws it: the mark is taken by a select that
  // exists, never by a mount that rendered nothing.
  useEffect(() => {
    if (drawn && takeSelectorChange()) box.current?.focus();
  }, [drawn]);
  if (!drawn) return null;
  const value =
    selected !== null && clients.some((client) => client.id === selected) ? selected : EVERY_CLIENT;
  return (
    <select
      ref={box}
      className="select-field top-client"
      aria-label={t("shell.clientSelector.label")}
      value={value}
      onChange={(event) => {
        const choice = event.target.value === EVERY_CLIENT ? null : event.target.value;
        markSelectorChange(choice);
        select(choice);
      }}
    >
      <option value={EVERY_CLIENT}>{t("shell.clientSelector.all")}</option>
      {clients.map((client) => (
        <option key={client.id} value={client.id}>
          {nameOf(client, t)}
        </option>
      ))}
    </select>
  );
}
