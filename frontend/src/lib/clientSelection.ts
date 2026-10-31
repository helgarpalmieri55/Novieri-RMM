/**
 * THE CLIENT OF A NEW ROW (phase 3 design §3.1). One rule for every form that
 * creates something a client owns:
 *
 * 1. A row that hangs from something with a client -- a device, a group, a
 *    VRF, a subnet, a group's policy -- goes to that client, and the form
 *    does not ask (`parentClientId`; the operator's rule of 2026-10-28).
 * 2. Otherwise, the client chosen in the top bar.
 * 3. With "All my clients", the form shows a Client field (`ClientField`),
 *    required, offering only the person's clients whose `/me` permissions
 *    hold the code of that write -- never the Unassigned client (C10), and
 *    never the internal one unless the person picks it (Q1).
 *
 * The body is still built INSIDE the mutation (`requireKnown`): nothing is
 * sent before the client is known.
 *
 * Replaces `lib/internalClient.ts`, which filed every new row under the
 * organisation's internal client until this phase. Its callers move here in
 * Tasks 10 and 11, and Task 12 deletes it.
 *
 * INVARIANT 6: THIS GATES NOTHING. The server refuses a client outside the
 * request's scope, or one where the person lacks the code, whatever a form
 * sent.
 */

import type { UseQueryResult } from "@tanstack/react-query";
import { useMemo } from "react";

import { translate } from "../i18n/core";
import type { MeClient } from "./api";
import { useClientScope } from "./clientScope";

export interface NewRowClient {
  /** Where the row goes when the form need not ask; `null` when it must, or while unknown. */
  clientId: string | null;
  /** The form shows a required Client field: "All my clients", and no parent decides. */
  needsField: boolean;
  /** What that field offers: the person's clients holding the code, never Unassigned. */
  options: MeClient[];
}

/** The person's clients where `code` is held, the Unassigned client never (C10). */
export function clientsHolding(clients: readonly MeClient[], code: string): MeClient[] {
  return clients.filter((client) => client.kind !== "unassigned" && client.permissions.includes(code));
}

/**
 * Where a new row goes. `code` is the permission its create route checks.
 * `parentClientId`: absent for a row that hangs from nothing; `null` for one
 * whose parent's client is not known yet (still loading) -- nothing is asked
 * and nothing can be sent until it is; an id, that client.
 */
export function useNewRowClient(code: string, parentClientId?: string | null): NewRowClient {
  const { selected, clients, ready } = useClientScope();
  const options = useMemo(() => clientsHolding(clients, code), [clients, code]);
  if (parentClientId !== undefined) return { clientId: parentClientId, needsField: false, options };
  if (!ready) return { clientId: null, needsField: false, options };
  if (selected !== null) return { clientId: selected, needsField: false, options };
  return { clientId: null, needsField: true, options };
}

/**
 * Thrown by a create that cannot name its client yet: `/me` has not answered,
 * the parent has not loaded, or the Client field is empty. A sentence, in the
 * person's language, so the form's error surface (`describeSubmitError`)
 * shows it as it is; and nothing is sent.
 */
export class ClientNotChosen extends Error {
  constructor() {
    super(translate("lib.clientSelection.notChosen"));
    this.name = "ClientNotChosen";
  }
}

/** `id`, or `ClientNotChosen` when it is not known yet. For a mutation's body. */
export function requireKnown(id: string | null): string {
  if (id === null) throw new ClientNotChosen();
  return id;
}

/**
 * A read that cannot be sent before its client is known is LOADING until
 * then, not empty: a disabled query reports `isLoading: false` with no data,
 * which a panel would draw as "nothing yet" while `/me` is still in flight.
 */
export function loadingUntilClient<R extends UseQueryResult<unknown, unknown>>(
  query: R,
  client: string | null,
): R {
  return client == null && query.isPending ? { ...query, isLoading: true } : query;
}
