// Mirrors backend/app/modules/portal/schema.py exactly (snake_case keys,
// matching the JSON). Read the actual route/response models there, not the
// task brief's example, which predates several backend changes -- e.g. the
// approval field is `state`, not `decision`.
//
// **These are NOT the staff `service-desk/types.ts` shapes, and this module
// does not import, extend, or widen them.** Per schema.py's own module
// docstring: "No portal response may contain an internal comment, another
// user's identity beyond a display name on a public comment, a queue, an
// assignee, or any field whose value is staff-only." There is no `queue_id`,
// no `assigned_to`, no `author_id`/`author_type`, and no `actor_id` anywhere
// below -- deliberately absent, not merely unused. Any attempt to render one
// of those fields fails `tsc --noEmit` because the field does not exist on
// the type at all: the structural guarantee `test_portal_detail_exposes_no_
// staff_fields` asserts server-side is mirrored here by strict typing on the
// client.

export type PortalTicketPriority = "low" | "normal" | "high" | "urgent";

// service_desk/approvals.py's own vocabulary (`PortalApprovalOut.state` stays
// `string`, widened on the way out like every other domain literal).
export type PortalApprovalState = "pending" | "approved" | "rejected";

// --- Categories (read-only org refdata; spec §2.1) -----------------------

export interface PortalCategoryOut {
  id: string;
  key: string;
  name: string;
  // `str`, not a narrowed Literal, in schema.py's PortalCategoryOut.
  type: string;
}

// --- Ticket create (spec §6.2) --------------------------------------------

export interface PortalTicketCreate {
  // No `requester_id`, no `type`, no `queue_id`, no `assigned_to`, no
  // `origin` -- none of these are the caller's to set (schema.py's
  // docstring). `type` is resolved server-side from the chosen category.
  category_id: string;
  subject: string;
  description?: string;
  priority?: PortalTicketPriority;
}

// --- Comments (spec §2.3, §6.2) -------------------------------------------

export interface PortalCommentCreate {
  // No `visibility` field -- `add_my_comment` forces "public" unconditionally.
  body: string;
}

export interface PortalCommentOut {
  // No `author_id`/`author_type`/`visibility` -- see module docstring. Note:
  // migration 0030 DOES grant `portal_user` a restricted SELECT on `users`
  // (own row only, via a `portal_isolation` policy) -- an earlier version of
  // this comment said "no SELECT grant on users at all", which is now stale.
  // The conclusion still holds for a different reason: that grant resolves
  // only the CALLER's own row, never a staff author's, so there is still no
  // join available to turn a staff `author_id` into a display name here, and
  // only ever-public comments are modelled in the first place.
  id: string;
  body: string;
  created_at: string;
}

// --- Attachments (spec §6.3) ----------------------------------------------

export interface PortalAttachmentOut {
  // No `object_key` (server-derived, would leak tenant layout) and no
  // `uploaded_by`: this model backs both the upload response and the
  // detail/download responses, and both are now scoped (I1 fix) to the
  // requester's OWN uploads only -- a staff-uploaded attachment on this
  // ticket never appears here at all.
  id: string;
  filename: string;
  byte_size: number;
  created_at: string;
}

// --- Status history & approval (spec §0.3, §5) ----------------------------

export interface PortalStatusHistoryOut {
  // No `actor_id`/`actor_type` -- see module docstring. No `reason` either --
  // this one is a security decision, not narrowness-by-default (spec §5,
  // amended): `ticket_status_history.reason` is unbounded staff free text
  // with no visibility column, so it cannot be shown to a requester safely.
  // A requester sees THAT and WHEN their ticket's status changed, never WHY
  // in staff's own words.
  from_status: string | null;
  to_status: string;
  created_at: string;
}

// The one genuinely new read this module closes (spec §0.3): approval
// outcome had no read path anywhere in the product before this. No
// `decided_by` -- a staff member's identity the portal has no way to
// resolve. `state` is service_desk/approvals.py's own vocabulary:
// "pending" | "approved" | "rejected" -- widened to `str` here exactly as
// every other *Out model widens a domain literal on the way out.
export interface PortalApprovalOut {
  state: string;
  reason: string;
  decided_at: string | null;
}

// --- Tickets: list & detail (spec §5) -------------------------------------

// Backs `GET /portal/tickets` ("My tickets", spec §7): status, last update,
// ticket number -- and nothing a requester cannot already read off their own
// tickets. No `queue_id`, no `assigned_to`, no `category_id`.
export interface PortalTicketSummary {
  id: string;
  number: string;
  type: string;
  status: string;
  priority: string;
  subject: string;
  created_at: string;
  updated_at: string;
}

// Backs `GET /portal/tickets/{id}` (spec §5). Deliberately absent (schema.py's
// module docstring, structural guarantee): `queue_id`, `assigned_to`,
// `category_id`, `device_id`, `origin`, `source_alert_id`, `created_by`, and
// `internal_notes` (no such field exists anywhere -- internal comments are
// filtered out entirely, both by RLS and by the portal's own query).
//
// I2 fix: `attachments` IS present here now (an earlier version of this
// module had no list field at all, so an uploaded file became unreachable
// after a page reload -- the detail page tracked uploads in React state
// only). There is still no separate `GET .../attachments` list route --
// this field is populated by the SAME `GET /portal/tickets/{id}` call, and
// lists only the requester's OWN attachments (I1's scoping decision:
// `ticket_attachment` has no visibility column, so a staff-uploaded file on
// this ticket is deliberately never included here or downloadable).
export interface PortalTicketDetail {
  id: string;
  number: string;
  type: string;
  status: string;
  priority: string;
  subject: string;
  description: string;
  created_at: string;
  updated_at: string;
  comments: PortalCommentOut[];
  status_history: PortalStatusHistoryOut[];
  attachments: PortalAttachmentOut[];
  approval: PortalApprovalOut | null;
}

// GET /portal/tickets' query parameters (api.py's list_my_tickets_route).
export interface PortalTicketListFilters {
  limit?: number;
  offset?: number;
}

// GET /portal/help/suggest: help articles staff published to the portal.
export interface PortalHelpSuggestion {
  id: string;
  title: string;
}

// GET /portal/help/articles/{id}: the article's title and text, nothing else.
export interface PortalHelpArticle {
  id: string;
  title: string;
  body: string;
}
