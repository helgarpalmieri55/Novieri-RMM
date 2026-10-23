/**
 * Mirrors `backend/app/api/v1/audit.py`'s Pydantic models exactly (snake_case
 * keys, matching the JSON). THE SHIPPED API IS THE CONTRACT — these were
 * written against the route file, not against the design.
 *
 * Note what the endpoint does NOT serve, because the design assumes all of it:
 * no correlation id, no date range filter, no export packs, no
 * restore-boundary records. Columns and panels for those are omitted rather
 * than invented; see `AuditPage.tsx`.
 */

// THE NAMES THE CONSOLE HAS A LABEL FOR. `action`, `actor_type` and `target_type`
// stay open strings on the wire (every module records its own), and an entry's
// stored text is never rewritten; these unions are only what `enums.<Type>`
// words in both languages, and `enums.coverage.test.ts` checks them. A dotted
// action code is keyed with `_` because `.` separates catalogue paths.
export type AuditAction =
  | "job_dispatch"
  | "job_retry"
  | "job_cancel"
  | "credential_checkout"
  | "credential_created"
  | "credential_use"
  | "remote_session_start"
  | "remote_session_requested"
  | "remote_session_credential_used"
  | "auth_signed_in"
  | "policy_change"
  | "patch_approved"
  | "ai_action_proposed";

export type AuditActorType = "user" | "agent" | "system";

export type AuditTargetType =
  | "device"
  | "job"
  | "credential"
  | "user"
  | "organization"
  | "ticket"
  | "change"
  | "push_target"
  | "email_mailbox"
  | "session"
  | "person"
  | "agent_release"
  | "health_alert"
  | "expense"
  | "org"
  | "role"
  | "license"
  | "group"
  | "approved_action"
  | "ai_action"
  | "subscription"
  | "sla_calendar"
  | "push_run"
  | "notification_channel"
  | "budget"
  | "bcp_test"
  | "backup_recovery_key"
  | "backup_offsite_target"
  | "sla_policy"
  | "site_server"
  | "seat_assignment"
  | "script"
  | "provision_run"
  | "product"
  | "patch"
  | "package"
  | "notification_rule"
  | "ipam_address"
  | "install_link"
  | "deployment"
  | "bcp_plan"
  | "backup_point"
  | "ai_session";

export interface AuditEventOut {
  id: string;
  ts: string;
  actor_type: string;
  actor_id: string | null;
  /** The directory's current name for a user actor, joined at read time.
   *  Null for a non-user, a deleted user, or one with no name. Optional
   *  because the fixture rows predate it. */
  actor_display_name?: string | null;
  action: string;
  target_type: string;
  target_id: string;
  /** As `actor_display_name`, and only when `target_type` is `"user"`. */
  target_display_name?: string | null;
  metadata: Record<string, unknown>;
  /** The hash-chain link for this row. */
  hash: string;
}

export interface AuditListResponse {
  items: AuditEventOut[];
  total: number;
}

/**
 * `GET /audit/verify`.
 *
 * `ok === false` is the single most important boolean in this console: it means
 * the append-only chain has a break, i.e. the evidence for every privileged
 * action on this tenant is no longer trustworthy from `broken_at` onward.
 * `broken_at` names the first event whose link did not verify.
 */
export interface AuditVerifyResponse {
  ok: boolean;
  count: number;
  broken_at: string | null;
}

export interface AuditListParams {
  action?: string;
  limit?: number;
  offset?: number;
}
