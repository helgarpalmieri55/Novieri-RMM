/**
 * The two audit routes the kernel serves, and nothing else.
 *
 * Both need `audit.read` ON EVERY CLIENT, server-side
 * (`require_permission_on_every_client("audit.read")` in
 * `backend/app/api/v1/audit.py`): the log describes every client, and phase 3
 * decided not to open it per client (design 2026-10-31, Q2). Whatever this
 * console chooses to render, the server decides what it may have -- invariant 6.
 */

import type { ApiClient } from "../../lib/api";
import type { AuditListParams, AuditListResponse, AuditVerifyResponse } from "./types";

export const AUDIT_EVENTS_KEY = "audit-events";
export const AUDIT_VERIFY_KEY = "audit-verify";

export function listAuditEvents(
  api: ApiClient,
  params: AuditListParams = {},
): Promise<AuditListResponse> {
  return api.listAuditEvents(params as Record<string, string | number | undefined>);
}

/**
 * Walks the chain server-side and reports whether it verified.
 *
 * THE RESULT IS NEVER ASSUMED. A caller that fails to reach this endpoint has
 * learned nothing about the chain, and must not render anything that reads as
 * "verified" — see `AuditPage.tsx`'s integrity panel.
 */
export function verifyAuditChain(api: ApiClient): Promise<AuditVerifyResponse> {
  return api.verifyAuditChain();
}
