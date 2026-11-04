import { translate } from "../../i18n/core";
import type { NewRowClientField } from "../../lib/clientSelection";

/**
 * WHY NO SITE CAN BE NAMED YET for the page's install command, install link or
 * deployment token (design §3.2), as the sentence a mint shows instead of
 * sending -- or `null` when a site is chosen, or while the account has not
 * loaded (the mint's `requireKnown` then says so with `ClientNotChosen`).
 *
 * THE MISSING FIELD, NOT THE CLIENT (phase 3 final review C, Important 7):
 * with a client chosen and more than one site, the command used to answer
 * "...the client this is for has not been chosen", which named the one thing
 * that was known. In order: Unassigned chosen at the top (nothing new can be
 * filed there, whatever site a journey left behind), the Client field with
 * every client chosen, then the Site field.
 */
export function whereMissing(client: NewRowClientField, siteId: string): string | null {
  if (client.refused === "unassigned") return translate("components.unassignedRefusal");
  if (siteId !== "") return null;
  if (client.needsField && client.clientId === null) {
    return translate("validation.required", { label: translate("components.clientField.label") });
  }
  if (client.clientId !== null) {
    return translate("validation.required", { label: translate("components.siteField.label") });
  }
  return null;
}
