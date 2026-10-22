// Mirrors backend/app/modules/service_desk/schema.py request/response models
// exactly (snake_case keys, matching the JSON). Read the actual route
// signatures/response models there, not the spec prose -- the schema wins on
// any disagreement (this module's task brief).
//
// Per security invariant #5, no response model here carries `org_id`, and
// `AttachmentOut` never carries `object_key` -- mirrors
// `budget/types.ts`'s `ExpenseOut` withholding `invoice_object_key`.
//
// Task 13's G1 fix: `GET .../tickets/{ticket_id}/history` is now mounted
// (api.py), returning `StatusHistoryOut` below -- the status-history panel
// on TicketDetailPage consumes it.
//
// Likewise there is no dedicated "get approval" type/route: a request
// ticket's approval state is entirely inferable from `TicketOut.status`
// (`pending_approval` / `in_progress` / `rejected`), so the approve/reject
// panel reads status directly rather than a second endpoint that doesn't
// exist.

import type { TargetState } from "../sla/types";

// schema.py: TicketType = Literal["incident", "request"].
export type TicketType = "incident" | "request";

// schema.py: TicketPriority = Literal["low", "normal", "high", "urgent"].
export type TicketPriority = "low" | "normal" | "high" | "urgent";

// schema.py: TicketOrigin = Literal["technician", "alert", "portal", "email"].
export type TicketOrigin = "technician" | "alert" | "portal" | "email";

// schema.py: CommentVisibility = Literal["public", "internal"].
export type CommentVisibility = "public" | "internal";

// lifecycle.py: every status a ticket of either type can hold (the union of
// INCIDENT_STATUSES and REQUEST_STATUSES). `TicketOut.status` stays `string`,
// as the schema widens it; this names the values the console has a label for.
export type TicketStatus =
  | "new"
  | "open"
  | "pending"
  | "resolved"
  | "closed"
  | "cancelled"
  | "pending_approval"
  | "in_progress"
  | "fulfilled"
  | "rejected";

// lifecycle.py's FINAL_STATUSES: the two statuses nothing ever leaves.
export type FinalTicketStatus = "cancelled" | "rejected";

// threads.py's _VALID_AUTHOR_TYPES: who wrote a comment (`CommentOut.author_type`
// stays `string`, as the schema widens it).
export type CommentAuthorType = "user" | "system";

// The three values `users.status` is constrained to (`UserOut.status` stays
// `string`): shown beside a person in the pickers when they are not active.
export type UserStatus = "active" | "disabled" | "deleted";

// orchestrator.py's StopReason: why a copilot turn ended (`AiCopilotTurn.stop_reason`
// stays `string`).
export type CopilotStopReason =
  | "answer"
  | "tool_budget"
  | "byte_budget"
  | "token_budget"
  | "deadline"
  | "malformed"
  | "nonce_leak"
  | "provider_error"
  | "provider_rate_limited"
  | "session_expired"
  | "internal_error";

// --- Queues (spec §2 `ticket_queue`, §5) --------------------------------

export interface QueueCreate {
  key: string;
  name: string;
  description?: string;
  sort_order?: number;
}

export interface QueueUpdate {
  name?: string | null;
  description?: string | null;
  active?: boolean | null;
  sort_order?: number | null;
}

export interface QueueOut {
  id: string;
  key: string;
  name: string;
  description: string;
  active: boolean;
  sort_order: number;
  created_by: string;
  created_at: string;
}

// --- Categories (spec §2 `ticket_category`, §4) -------------------------

export interface CategoryCreate {
  key: string;
  name: string;
  type: TicketType;
  requires_approval?: boolean;
  approver_role_id?: string | null;
  default_queue_id?: string | null;
  sort_order?: number;
}

export interface CategoryUpdate {
  name?: string | null;
  requires_approval?: boolean | null;
  approver_role_id?: string | null;
  default_queue_id?: string | null;
  active?: boolean | null;
  sort_order?: number | null;
}

export interface CategoryOut {
  id: string;
  key: string;
  name: string;
  // `str`, not `TicketType`, in schema.py's CategoryOut -- the backend does
  // not re-narrow the type on the way out.
  type: string;
  requires_approval: boolean;
  approver_role_id: string | null;
  default_queue_id: string | null;
  active: boolean;
  sort_order: number;
  created_by: string;
  created_at: string;
}

// --- Tickets (spec §2 `ticket`, §3) -------------------------------------

export interface TicketCreate {
  type: TicketType;
  category_id: string;
  // Optional in the schema, but `create_ticket` (tickets.py) rejects a
  // technician-origin ticket (every ticket this API ever creates) with no
  // `requester_id` as a typed 400 -- so the New Ticket form treats it as
  // effectively required even though the type here stays optional to match
  // `TicketCreate` exactly.
  queue_id?: string | null;
  subject: string;
  description?: string;
  priority?: TicketPriority;
  requester_id?: string | null;
  assigned_to?: string | null;
  device_id?: string | null;
  // `origin` is NOT a field here -- api.py's create_ticket_route decides it
  // server-side (always "technician" for this API), never read from the
  // request body. There is nothing for this client to send.
}

export interface TicketUpdate {
  // `status`, `queue_id`, and `assigned_to` are deliberately absent -- see
  // schema.py's module docstring: status only moves through the dedicated
  // transition endpoint, and queue/assignee only through the assign
  // endpoint (both `ticket.assign`-gated, distinct from this `ticket.write`
  // PATCH). `type`/`requester_id`/`origin`/`source_alert_id` are immutable
  // after creation and are likewise absent.
  category_id?: string | null;
  priority?: TicketPriority | null;
  subject?: string | null;
  description?: string | null;
  device_id?: string | null;
}

// schema.py's TicketSlaSummary. The two states are the SLA engine's verdict;
// `next_due_at` is null when the clock is paused, void or fully met.
export interface TicketSlaSummary {
  response_due_at: string;
  resolution_due_at: string;
  response_state: TargetState;
  resolution_state: TargetState;
  next_due_at: string | null;
  next_due_target: "response" | "resolution" | null;
  breached: boolean;
  paused: boolean;
}

export interface TicketOut {
  // No `org_id` field here -- security invariant #5.
  id: string;
  number: string;
  // `str`, not `TicketType`, in schema.py's TicketOut.
  type: string;
  category_id: string;
  queue_id: string;
  // `str`, not a narrowed status Literal -- lifecycle.py is the sole
  // authority on the vocabulary (see serviceDeskState.ts for the exact
  // frozensets this widens from).
  status: string;
  priority: string;
  subject: string;
  description: string;
  requester_id: string | null;
  assigned_to: string | null;
  device_id: string | null;
  origin: string;
  source_alert_id: string | null;
  resolved_at: string | null;
  closed_at: string | null;
  has_attachments: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  // Absent from older servers; null when no SLA policy applies.
  sla?: TicketSlaSummary | null;
}

export interface TicketStatusChange {
  to_status: string;
  reason?: string;
}

// `POST .../resolve`: the optional reply and the move to resolved happen in
// one server transaction, so either both land or neither does.
export interface TicketResolve {
  reply?: CommentCreate;
}

export interface TicketAssign {
  assigned_to?: string | null;
  queue_id?: string | null;
}

export interface ApprovalDecision {
  reason?: string;
}

// --- Comments & attachments (spec §6) -----------------------------------

export interface CommentCreate {
  body: string;
  visibility?: CommentVisibility;
}

export interface CommentOut {
  id: string;
  ticket_id: string;
  author_id: string | null;
  author_type: string;
  body: string;
  visibility: string;
  created_at: string;
}

export interface AttachmentOut {
  id: string;
  ticket_id: string;
  filename: string;
  byte_size: number;
  uploaded_by: string;
  created_at: string;
}

// schema.py's StatusHistoryOut (spec §2 `ticket_status_history`, §11's
// ticket-detail "status history"). No `org_id` field here -- invariant #5.
export interface StatusHistoryOut {
  id: string;
  ticket_id: string;
  from_status: string | null;
  to_status: string;
  actor_type: string;
  actor_id: string | null;
  reason: string;
  created_at: string;
}

// GET /service-desk/tickets' query filters (api.py's list_tickets_route).
// `q` (C1 fix, whole-branch review round C) is a case-insensitive substring
// match over `subject`/`description` -- it was UNDELIVERED SPEC before this
// fix: declared in spec §9, but with no route parameter, no service-layer
// argument, and no client method anywhere.
export interface TicketListFilters {
  type?: string;
  status?: string;
  queue_id?: string;
  assigned_to?: string;
  requester_id?: string;
  device_id?: string;
  priority?: string;
  q?: string;
  // Server-side narrowing: open tickets that are mine / nobody's / anyone's,
  // or every ticket. "mine" is always the signed-in user.
  scope?: TicketScope;
  sort?: TicketSort;
  limit?: number;
  offset?: number;
}

export type TicketScope = "mine" | "unassigned" | "open" | "all";
export type TicketSort = "newest" | "due";

// --- Canned responses (saved replies) -----------------------------------

export interface CannedResponseOut {
  id: string;
  title: string;
  body: string;
  created_by: string;
  created_at: string;
  updated_by: string | null;
  updated_at: string | null;
}

export interface CannedResponseCreate {
  title: string;
  body: string;
}

export interface CannedResponseUpdate {
  title?: string;
  body?: string;
}

// GET /docs/suggest: an article that may answer a ticket.
export interface ArticleSuggestion {
  id: string;
  title: string;
  category: string;
}
