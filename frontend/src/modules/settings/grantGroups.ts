/**
 * A person's effective permissions grouped by client (MSP clients phase 3,
 * design §4.2): the grants that hold on every client first, then one group
 * per client, by name. `GET /users/{id}/effective-permissions` already says
 * which client each grant is for (`client_id`, `null` for every client).
 */
import { intlLocale } from "../../lib/datetime";
import type { CodeGrantOut } from "./types";

export interface GrantGroup {
  /** `null` for the grants that hold on every client. */
  readonly clientId: string | null;
  readonly grants: readonly CodeGrantOut[];
}

export function groupGrantsByClient(
  grants: readonly CodeGrantOut[],
  name: (clientId: string) => string,
): GrantGroup[] {
  const byClient = new Map<string | null, CodeGrantOut[]>();
  for (const grant of grants) {
    const list = byClient.get(grant.client_id) ?? [];
    list.push(grant);
    byClient.set(grant.client_id, list);
  }
  const sorted = (list: readonly CodeGrantOut[]) =>
    [...list].sort((a, b) => a.code.localeCompare(b.code) || a.via_role.localeCompare(b.via_role));
  const clients = [...byClient.keys()]
    .filter((id): id is string => id !== null)
    .sort((a, b) => name(a).localeCompare(name(b), intlLocale()) || a.localeCompare(b));
  const every = byClient.get(null);
  return [
    ...(every ? [{ clientId: null, grants: sorted(every) }] : []),
    ...clients.map((clientId) => ({ clientId, grants: sorted(byClient.get(clientId) ?? []) })),
  ];
}
