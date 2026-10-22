// Mirrors backend/app/modules/notification/schema.py request/response models
// exactly (snake_case keys, matching the JSON), plus api.py's request-body
// composition -- read the ACTUAL route/schema, not spec prose, on any
// disagreement (this module's task brief). The backend is complete and
// merged; this file was written against `schema.py`/`api.py` directly.
//
// Per security invariant #3, `ChannelOut` carries no field capable of
// holding a credential -- not even `credential_id` -- mirroring
// service-desk's `AttachmentOut` withholding `object_key`. `has_credential`
// is the one boolean surfaced instead.

export type ChannelKind = "webhook" | "email";
// An email channel's `config.auth`: how it signs in to its mail server.
// Absent on older channels, which means "password".
export type EmailAuth = "password" | "microsoft";
export type RecipientKind = "queue_members" | "assignee" | "requester" | "channel_default";
export type DeliveryStatus = "pending" | "sent" | "failed" | "dead";

// app/core/events.py's EVENT_TYPES registry -- the sole authority on legal
// event types (RuleCreate.event_type is validated against it server-side, a
// 400 on mismatch). Two comment events are separate types, not one type with
// a visibility flag (spec §2.1/§6.4): a rule pairing
// "ticket.internal_note_added" with recipient_kind="requester" is rejected
// at write time for exactly this reason.
export const EVENT_TYPES = [
  "ticket.assigned",
  "ticket.status_changed",
  "ticket.comment_added",
  "ticket.internal_note_added",
  "ticket.approval_requested",
  "ticket.approval_decided",
  "ticket.resolved",
  // Machine-origin, no ticket: route these with recipient "channel_default".
  "platform.probe_unhealthy",
  "platform.probe_recovered",
  "subscription.notice_deadline_approaching",
  "subscription.renewal_approaching",
  // A computer joined through the install link and waits for review
  // (install-link spec §1.5). Machine-origin: route with "channel_default".
  "device.joined_via_link",
  // A rejected computer's agent may still be installed: it did not confirm it
  // removed itself, or is too old to. Machine-origin: route with "channel_default".
  "device.removal_incomplete",
  // A confirmed subnet push finished (design 2026-09-30 §1.5).
  // Machine-origin: route with "channel_default".
  "push.run.completed",
  // A run stopped after repeated sign-in failures (design 2026-09-30 §1.4a).
  // Someone must check the credential and resume it. Machine-origin.
  "push.run.stopped",
] as const;

// The literal union of legal event types, derived from `EVENT_TYPES` itself
// (rather than declared separately) so the two can never drift. A display-
// name map keyed `Record<EventType, string>` (see `notification/labels.ts`)
// then fails to compile the moment a new event type is added here without a
// label -- an unmapped value becomes a build error, not a raw wire string on
// screen.
export type EventType = (typeof EVENT_TYPES)[number];

// --- Channels (schema.py's ChannelCreate/ChannelUpdate/ChannelOut, plus
// api.py's ChannelCreateRequest/ChannelUpdateRequest which each add exactly
// one API-layer-only `secret` field on top) --------------------------------

// api.py's `_SignInFields`: an email channel's sign-in, write-only. Vaulted
// server-side as one value and never returned (ChannelOut has no field for it).
export interface EmailSignInFields {
  smtp_username?: string;
  smtp_password?: string;
  microsoft_tenant_id?: string;
  microsoft_client_id?: string;
  microsoft_client_secret?: string;
}

export interface ChannelCreate extends EmailSignInFields {
  kind: ChannelKind;
  name: string;
  config: Record<string, unknown>;
  active?: boolean;
  // api.py's ChannelCreateRequest: a webhook's plaintext bearer token, kept
  // OUT of `config` (an email channel uses the sign-in fields above) -- `config` is returned by the API and
  // `_reject_secret_keys` rejects secret-shaped keys placed there (invariant
  // #3). The secret is checked into the vault server-side, never echoed back.
  secret?: string | null;
}

export interface ChannelUpdate extends EmailSignInFields {
  name?: string | null;
  config?: Record<string, unknown> | null;
  active?: boolean | null;
  secret?: string | null;
}

export interface ChannelOut {
  id: string;
  kind: ChannelKind;
  name: string;
  config: Record<string, unknown>;
  active: boolean;
  has_credential: boolean;
  created_at: string;
}

// --- Rules (schema.py's RuleCreate/RuleUpdate/RuleOut) --------------------

export interface RuleCreate {
  event_type: string;
  channel_id: string;
  recipient_kind: RecipientKind;
  // NULL means "any queue" (spec §2.3).
  queue_id?: string | null;
  active?: boolean;
}

// Only `active` is editable after create (schema.py's RuleUpdate docstring)
// -- event_type/channel_id/recipient_kind/queue_id together define WHAT the
// rule routes and are immutable.
export interface RuleUpdate {
  active?: boolean | null;
}

export interface RuleOut {
  id: string;
  event_type: string;
  channel_id: string;
  recipient_kind: RecipientKind;
  queue_id: string | null;
  active: boolean;
  created_at: string;
}

// --- Allowlist (schema.py's AllowlistEntryCreate/AllowlistEntryOut) -------

export interface AllowlistEntryCreate {
  host: string;
  // Migration 0026: `port` is required, NOT NULL, server_default 443, and
  // part of the unique key (org_id, host, port) -- an admin allowlists a
  // host AND a port together (`mattermost.lan:8065`), not a host and every
  // port on it. There is no NULL-means-"any port" escape hatch.
  port?: number;
  note?: string;
}

export interface AllowlistEntryOut {
  id: string;
  host: string;
  port: number;
  note: string;
  created_at: string;
}

// --- Queue members (schema.py's QueueMemberOut) ---------------------------

export interface QueueMemberOut {
  user_id: string;
  queue_id: string;
}

// --- Deliveries (schema.py's DeliveryOut/DeliveryListResponse) ------------
//
// `recipient` is a bare email address or webhook host, never a full URL with
// a query string (spec §2.5/§9); `last_error` is already a bounded,
// sanitized excerpt, never a raw response body. No response field here can
// carry a credential or a response body.

export interface DeliveryOut {
  id: string;
  event_id: string;
  channel_id: string;
  recipient: string;
  status: DeliveryStatus;
  attempts: number;
  last_error: string | null;
  last_status_code: number | null;
  next_attempt_at: string | null;
  sent_at: string | null;
  created_at: string;
}

export interface DeliveryListResponse {
  items: DeliveryOut[];
  total: number;
}

// GET /notifications/deliveries' query filters (api.py's
// list_deliveries_route).
export interface DeliveryListFilters {
  status?: DeliveryStatus;
  channel_id?: string;
  event_type?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}

// --- Test send (schema.py's TestSendResult) -------------------------------
//
// `error` is already the bounded, sanitized category string the service
// layer produced -- never a raw response body (spec §4.4/§9).

export interface TestSendResult {
  delivered: boolean;
  status_code?: number | null;
  error?: string | null;
}
