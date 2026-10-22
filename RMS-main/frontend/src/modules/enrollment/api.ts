/**
 * The kernel enrollment routes, as shipped in
 * `backend/app/api/v1/enrollment.py`.
 *
 * All four are gated `enrollment.manage` SERVER-SIDE. This console's gating
 * decides only what renders (invariant 6).
 *
 * `POST /enroll/v1` is deliberately absent: that route is the AGENT's, it takes
 * a CSR and a bootstrap token and it is not authenticated as an operator. A
 * console method for it would be a console that can mint device identities.
 *
 * The request helper is shared with `modules/audit` — both are kernel surfaces
 * that `lib/api.ts` has no methods for; see `../audit/kernelHttp.ts` for why
 * that file exists and why it should not survive.
 */

import type { ApiClient } from "../../lib/api";
import type {
  EnrollmentTokenCreateRequest,
  EnrollmentTokenCreated,
  EnrollmentTokenPage,
} from "./types";

export const ENROLLMENT_TOKENS_KEY = "enrollment-tokens";

/**
 * How many tokens one page of the table holds.
 *
 * WELL UNDER THE ROUTE'S CEILING (500), on purpose. The bound that matters is
 * the one on the WIRE — `GET /enrollment-tokens` was unbounded, and tokens only
 * accumulate: one per device onboarded, one per provisioning run, and none ever
 * deleted, because a revoked row is the only record that the credential existed
 * — so an org onboarding a fleet grew this response until it timed out. Asking
 * for 500 would fix that and leave a 500-row table nobody can read; 25 is what
 * the patch rollups page by, and the honest `total` beside the pager is what
 * makes a short page distinguishable from the end of the list.
 */
export const ENROLLMENT_TOKENS_PAGE_SIZE = 25;

/**
 * One page of the org's enrollment tokens, REVOKED ONES EXCLUDED unless asked
 * for.
 *
 * `include_revoked` is the same shape `listDeviceGroups({ include_archived })`
 * uses, and for the same reason: a withdrawn record is kept and hidden, never
 * deleted. `limit`/`offset` is the shape every other paged list on this
 * platform takes; `total` on the response counts the MATCHING tokens, before
 * either, and `revoked_count` counts the org's revocations whatever page came
 * back.
 */
export function listEnrollmentTokens(
  api: ApiClient,
  params: {
    include_revoked?: boolean;
    unlimited_only?: boolean;
    limit?: number;
    offset?: number;
  } = {},
): Promise<EnrollmentTokenPage> {
  return api.listEnrollmentTokens(params);
}

/**
 * The server's ceiling on `max_uses`, restated so the operator learns it
 * before the round trip rather than from a 422.
 *
 * MIRRORS `services/enrollment.py::MAX_TOKEN_USES`, which is the kernel's own
 * constant and is held equal to `agent_distribution.MAX_DEPLOYMENT_USES` by
 * `backend/tests/test_enrollment.py`. There is no unlimited enrollment token
 * any more: `max_uses` is required, and above this the route answers 422.
 */
export const MAX_TOKEN_USES = 1000;

export function createEnrollmentToken(
  api: ApiClient,
  body: EnrollmentTokenCreateRequest,
): Promise<EnrollmentTokenCreated> {
  return api.createEnrollmentToken(body);
}

export function revokeEnrollmentToken(
  api: ApiClient,
  tokenId: string,
): Promise<{ revoked: boolean }> {
  return api.revokeEnrollmentToken(tokenId);
}

/**
 * Revokes an enrolled agent's certificate.
 *
 * This is destructive and immediate: the server sets the DB revocation flag,
 * updates the CA CRL and publishes a revoke event that drops the device's live
 * gateway connection. The device cannot come back without re-enrolling, because
 * the certificate was the credential a rotation would have travelled over.
 */
export function revokeAgent(api: ApiClient, deviceId: string): Promise<{ revoked: boolean }> {
  return api.revokeAgent(deviceId);
}
