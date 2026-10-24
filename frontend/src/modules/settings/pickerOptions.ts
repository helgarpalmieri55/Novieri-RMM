/**
 * The client a role grant's scope can name.
 *
 * WHY THIS EXISTS. The grant form took a device group as a pasted id, then
 * (cycle 9) from a picker fed by `GET /api/v1/device-groups`. The MSP clients
 * model scopes a grant to ONE CLIENT or to every client
 * (`user_roles.client_id`, spec §2.1), and device groups no longer scope
 * grants, so the same picker is fed by `GET /api/v1/clients` now.
 */

import type { PickerOption } from "../../components/picker";
import { translate } from "../../i18n/core";

/** What the picker reads of a client (`ClientOut` is one). */
export interface GrantableClient {
  id: string;
  name: string;
  kind: string;
  status: string;
  device_count: number;
}

/** Worded when it is read (a constant would freeze the language the page started in). */
export function clientPickerEmptyHint(): string {
  return translate("settings.clientPicker.emptyHint");
}

/**
 * The clients a grant may name. Never the `unassigned` client -- no grant may
 * point at it, and the server answers 422 (spec §2.2) -- and never an archived
 * one, which takes no new grant either. `GET /clients` already leaves archived
 * clients out; the filter says so here as well rather than trusting it.
 */
export function clientPickerOptions(clients: readonly GrantableClient[]): PickerOption[] {
  return clients
    .filter((client) => client.kind !== "unassigned" && client.status !== "archived")
    .map((client) => ({
      id: client.id,
      label: client.name,
      sublabel: translate("settings.clientPicker.devices", { count: client.device_count }),
    }));
}
