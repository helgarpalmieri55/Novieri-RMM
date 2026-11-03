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
 * and a select with one option is furniture.
 *
 * A NATIVE `<select>` in the kit's `.select-field`, the control the filter
 * bars already use: this phase adds the selector, it does not restyle the
 * chrome (project 3 does).
 */

import { useT } from "../i18n/context";
import { useClientScope } from "../lib/clientScope";

/** The option standing for every client, the choice `null`. Never a client id. */
const EVERY_CLIENT = "all";

export function ClientSelector() {
  const t = useT();
  const { selected, select, clients } = useClientScope();
  if (clients.length === 0) return null;
  const value =
    selected !== null && clients.some((client) => client.id === selected) ? selected : EVERY_CLIENT;
  return (
    <select
      className="select-field top-client"
      aria-label={t("shell.clientSelector.label")}
      value={value}
      onChange={(event) => select(event.target.value === EVERY_CLIENT ? null : event.target.value)}
    >
      <option value={EVERY_CLIENT}>{t("shell.clientSelector.all")}</option>
      {clients.map((client) => (
        <option key={client.id} value={client.id}>
          {client.kind === "unassigned" ? t("lib.clients.unassigned") : client.name}
        </option>
      ))}
    </select>
  );
}
