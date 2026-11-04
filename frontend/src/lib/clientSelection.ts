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
 * ONE HOOK FOR A FORM: `useNewRowClientField` answers all of it -- the
 * client, whether to ask, the field's value and options, and Unassigned
 * chosen at the top (`refused`) -- so no form keeps its own copy of the glue
 * (operator decision 2, 2026-11-02). Forms use `useNewRowClientField`;
 * `useNewRowClient` is the decision alone, kept exported for that hook and its
 * tests.
 *
 * Phases 1 and 2 filed every new row under the organisation's internal client,
 * whoever was working; this module replaced that helper in phase 3, and
 * `lib/noPhase3.test.ts` keeps it from coming back.
 *
 * INVARIANT 6: THIS GATES NOTHING. The server refuses a client outside the
 * request's scope, or one where the person lacks the code, whatever a form
 * sent.
 */

import type { UseQueryResult } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { translate } from "../i18n/core";
import type { MeClient } from "./api";
import { useClientScope } from "./clientScope";

/**
 * WHAT A FORM THAT CREATES A ROW NEEDS TO KNOW ABOUT ITS CLIENT, in one
 * answer (operator decision 2, 2026-11-02): the decision of §3.1 and the
 * Client field's own state, so a form copies none of the glue. The field is
 * drawn as `<ClientField field={…}/>` and the body built inside the mutation
 * with `requireKnown(field.clientId)`.
 */
export interface NewRowClientField {
  /**
   * Where the row goes: the parent's client, the one chosen at the top, or the
   * pick once it is one of `options`. `null` until known -- `/me`, the parent
   * or the pick -- and NEVER `""`.
   */
  readonly clientId: string | null;
  /** Draw `<ClientField field={…}/>`: "All my clients", and no parent decides. */
  readonly needsField: boolean;
  /** The field's value: `""` until the person picks. */
  readonly picked: string;
  /** The field's change. Runs `opts.onPick` when the pick really changes. */
  readonly setPicked: (clientId: string) => void;
  /** What the field offers: the person's clients holding `code`, never Unassigned (C10). */
  readonly options: readonly MeClient[];
  /** `/me` has answered and the top bar's choice is one of its clients. */
  readonly ready: boolean;
  /**
   * Unassigned is chosen at the top: nothing new can be filed. The server files
   * nothing new there, and the header confines the request to it, so a Client
   * field could not help either; the form says so instead of offering a Save
   * that can only fail.
   */
  readonly refused: "unassigned" | null;
}

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
 * Where a new row goes, and the Client field that asks when nothing else
 * decides (`NewRowClientField`). `code` is the permission its create route
 * checks. `parentClientId`: absent for a row that hangs from nothing; `null`
 * for one whose parent's client is not known yet (`parentClientOf`); an id,
 * that client. `opts.onPick` runs on a real change of the pick -- the form's
 * own reset of what depended on the client, such as its site.
 *
 * THE DECISION, IN ORDER:
 *   1. a parent is given: its client, whatever the top bar says;
 *   2. `/me` has not answered (or does not list the top bar's choice): no
 *      client yet, nothing asked, nothing refused;
 *   3. Unassigned is chosen at the top: `refused`;
 *   4. a client is chosen at the top: that one;
 *   5. otherwise ask: `needsField`, and the pick is the client only while it
 *      is one of `options`.
 */
export function useNewRowClientField(
  code: string,
  parentClientId?: string | null,
  opts?: { onPick?: (clientId: string) => void },
): NewRowClientField {
  const { selected, clients, ready } = useClientScope();
  const options = useMemo(() => clientsHolding(clients, code), [clients, code]);
  const [picked, setPickedState] = useState("");

  // THE LATEST CALLBACK, read when the pick changes: a form passes an inline
  // arrow, and the one it rendered with last is the one that knows its state.
  const onPick = useRef(opts?.onPick);
  useEffect(() => {
    onPick.current = opts?.onPick;
  });
  // A real change only: the same pick twice resets nothing.
  const current = useRef(picked);
  const setPicked = useCallback((clientId: string) => {
    if (clientId === current.current) return;
    current.current = clientId;
    setPickedState(clientId);
    onPick.current?.(clientId);
  }, []);

  const base = { picked, setPicked, options, ready };
  if (parentClientId !== undefined) {
    return { ...base, clientId: parentClientId, needsField: false, refused: null };
  }
  if (!ready) return { ...base, clientId: null, needsField: false, refused: null };
  if (selected !== null) {
    const unassigned = clients.some((client) => client.id === selected && client.kind === "unassigned");
    if (unassigned) return { ...base, clientId: null, needsField: false, refused: "unassigned" };
    return { ...base, clientId: selected, needsField: false, refused: null };
  }
  const listed = options.some((client) => client.id === picked);
  return { ...base, clientId: listed ? picked : null, needsField: true, refused: null };
}

/**
 * The client of a new row's parent, for `useNewRowClientField`'s second
 * argument, from the rows the parent is picked out of.
 *
 * `undefined` when there is no parent (`null`, or a form's `""`): the row
 * hangs from nothing. `null` while the rows are missing or do not hold the
 * id -- a parent not found YET is not "no parent", and reading it as one
 * would file the row under the top bar's client instead of the parent's.
 * Otherwise the parent's own `client_id`.
 */
export function parentClientOf(
  parentId: string | null | undefined,
  rows: readonly { id: string; client_id: string | null }[] | undefined,
): string | null | undefined {
  if (parentId === undefined || parentId === null || parentId === "") return undefined;
  if (rows === undefined) return null;
  const parent = rows.find((row) => row.id === parentId);
  return parent === undefined ? null : parent.client_id;
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

/**
 * `id`, or `ClientNotChosen` when it is not known yet. For a mutation's body.
 *
 * `""` IS NOT KNOWN EITHER. A form's field state is a string where `""` means
 * "not chosen", and the forms pass it as `pickedClient || null`; a copy that
 * drops the `|| null` must still send nothing, never `client_id: ""`.
 */
export function requireKnown(id: string | null): string {
  if (id === null || id === "") throw new ClientNotChosen();
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
