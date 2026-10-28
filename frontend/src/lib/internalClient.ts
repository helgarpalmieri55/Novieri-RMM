/**
 * THE INTERNAL CLIENT, UNTIL THE CLIENT SELECTOR EXISTS.
 *
 * MSP clients, phase 1 (`docs/superpowers/specs/2026-10-23-msp-clientes-design.md`
 * §1.5, §3.2). Every route that creates something a machine will belong to now
 * names where: enrollment tokens, install commands, deployment tokens, a
 * site's install link and a hand-made asset take a SITE (its client is derived
 * server-side, never sent); a device group, and the preview of one not saved
 * yet, take a CLIENT. The server refuses a body without them.
 *
 * This console has no client selector yet -- phase 3 builds it (spec §3.1) --
 * so until then it works on the organisation's INTERNAL client, its own IT.
 * That client and its "Principal" site carry the organisation's own id (the
 * phase-1 plan's id convention, spec §7 point 2), and `GET /me` already
 * returns it as `org_id`: no request is added to learn it.
 *
 * PHASE 3: the client/site selector. Every caller of this module is marked
 * with that line, and each one takes the client or site the person chose
 * instead.
 */

import type { UseQueryResult } from "@tanstack/react-query";
import { translate } from "../i18n/core";
import type { Me } from "./api";

/** The internal client's id: the organisation's. `null` until `/me` has answered. */
export function internalClientId(me: Pick<Me, "org_id"> | undefined): string | null {
  return me?.org_id ?? null;
}

/** The internal client's "Principal" site's id: the organisation's too. */
export function internalSiteId(me: Pick<Me, "org_id"> | undefined): string | null {
  return me?.org_id ?? null;
}

/**
 * Thrown by a create that cannot name its client or site yet: `/me` has not
 * answered, or failed. A sentence, in the person's language, so the form's
 * error surface (`describeSubmitError`) shows it as it is; and nothing is
 * sent, because a body without the id is a 422 the person can do nothing
 * about.
 */
export class InternalScopeUnknown extends Error {
  constructor() {
    super(translate("lib.internalClient.unknown"));
    this.name = "InternalScopeUnknown";
  }
}

/** `id`, or `InternalScopeUnknown` when it is not known yet. For a mutation's body. */
export function requireKnown(id: string | null): string {
  if (id === null) throw new InternalScopeUnknown();
  return id;
}

/**
 * A read that cannot be sent before its client is known is LOADING until
 * then, not empty: a disabled query reports `isLoading: false` with no data,
 * which a panel would draw as "nothing yet" (or, on the budget dashboard, as
 * the first-run state) while `/me` is still in flight. `client` is the id the
 * query waits for -- `internalClientId(me)` until phase 3.
 */
export function loadingUntilClient<R extends UseQueryResult<unknown, unknown>>(
  query: R,
  client: string | null,
): R {
  return client == null && query.isPending ? { ...query, isLoading: true } : query;
}
