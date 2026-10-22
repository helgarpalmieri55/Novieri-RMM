import { translate } from "../../i18n/core";
import { lazyFields } from "../../i18n/lazy";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { BadgeVariant } from "../jobs/jobState";

// Ticket status vocabulary is the ticket core's own (lifecycle.py's
// INCIDENT_STATUSES/REQUEST_STATUSES) -- this module widens to `str` and
// never re-derives it (mirrors serviceDeskState.ts's ticketStatusVariant
// exactly; kept as a narrow local copy so the portal has zero import-time
// coupling to the service-desk module, the same discipline schema.py's
// module docstring applies to PortalTicketPriority server-side).
const STATUS_VARIANTS: Record<string, BadgeVariant> = {
  new: "info",
  open: "info",
  in_progress: "info",
  pending: "warning",
  pending_approval: "warning",
  resolved: "success",
  fulfilled: "success",
  closed: "muted",
  cancelled: "muted",
  rejected: "destructive",
};

export function portalTicketStatusVariant(status: string): BadgeVariant {
  return STATUS_VARIANTS[status] ?? "muted";
}

const PRIORITY_VARIANTS: Record<string, BadgeVariant> = {
  low: "muted",
  normal: "muted",
  high: "warning",
  urgent: "destructive",
};

export function portalTicketPriorityVariant(priority: string): BadgeVariant {
  return PRIORITY_VARIANTS[priority] ?? "muted";
}

// service_desk/approvals.py's own vocabulary: "pending" | "approved" |
// "rejected" -- a rejection is the one outcome the requester needs to notice
// immediately (spec §0.3), given the same "destructive" treatment
// ticketStatusVariant gives a rejected ticket.
const APPROVAL_VARIANTS: Record<string, BadgeVariant> = {
  pending: "warning",
  approved: "success",
  rejected: "destructive",
};

export function portalApprovalVariant(state: string): BadgeVariant {
  return APPROVAL_VARIANTS[state] ?? "muted";
}

// --------------------------------------------------------------------------
// What a requester is asked to choose between (UX remediation cycle 6/7).
//
// Auditor A (MEDIUM), /portal/new -> Category: "The options are, in order:
// Select a category, Access, Access request, Equipment request, Hardware,
// Network, Other, Other request, Software, Software request ... producing
// the confusable adjacent pairs 'Access' / 'Access request', 'Other' /
// 'Other request' and 'Software' / 'Software request'. An end user asking
// for new software must distinguish 'Software' from 'Software request' with
// no explanation of either, and the two sit next to each other."
//
// The fix is the audit's own second suggestion -- `<optgroup>` headings --
// because it needs no new field: `PortalCategoryOut.type` is already the
// incident/request split the staff form gets as a separate control, and the
// flat alphabetical list simply threw it away.
//
// The labels are accessors over the catalogue (`lazyFields`), so a language
// switch shows on the next render instead of freezing the language the module
// loaded in.
export const PORTAL_CATEGORY_GROUPS: ReadonlyArray<{ type: string; label: string }> = [
  lazyFields({ type: "incident" }, { label: "portal.state.groupIncident" }),
  lazyFields({ type: "request" }, { label: "portal.state.groupRequest" }),
];

/**
 * Categories bucketed by their group, in `PORTAL_CATEGORY_GROUPS` order,
 * with anything of an unrecognised type collected into a final group rather
 * than dropped.
 *
 * NOT DROPPED is the point: a category the server offers and this list does
 * not know about is still a category the requester may need, and silently
 * hiding it would be a worse defect than the one being fixed.
 */
export function groupPortalCategories<T extends { type: string }>(
  categories: readonly T[],
): Array<{ label: string; categories: T[] }> {
  const groups = PORTAL_CATEGORY_GROUPS.map((group) => ({
    label: group.label,
    categories: categories.filter((category) => category.type === group.type),
  }));
  const known = new Set(PORTAL_CATEGORY_GROUPS.map((group) => group.type));
  const rest = categories.filter((category) => !known.has(category.type));
  if (rest.length > 0) groups.push({ label: translate("portal.state.groupOther"), categories: rest });
  return groups.filter((group) => group.categories.length > 0);
}

// --------------------------------------------------------------------------
// Priority, said in a way a non-technical employee can answer honestly.
//
// Auditor A (MEDIUM): "End users choose their own ticket priority from bare
// lowercase database values, with no definitions, no default guidance, and
// no indication that priority affects SLA ... self-selected priority with no
// definitions reliably produces priority inflation -- every user picks
// 'urgent' -- which destroys the queue's signal."
//
// So each option states the situation it describes rather than its name.
// The wire value is untouched: this is a display layer over schema.py's
// PortalTicketPriority, not a second vocabulary.
// --------------------------------------------------------------------------

// v3 draws "how urgent is it?" as a CHIP ROW of plain sentences
// (`P['portal/new']`), not as a dropdown of enum values, so these are chip
// labels now. Two consequences, both deliberate:
//
//   * the em dash is gone (the port forbids one in new UI copy), and with it
//     the "Low — " prefix: the chip IS the definition, which is what the
//     auditor asked for ("self-selected priority with no definitions reliably
//     produces priority inflation"), and a requester picking between four
//     sentences is not picking between four grades;
//   * the name each sentence maps to is still readable, because the chosen
//     chip's grade is named under the row -- the wire value is unchanged and
//     the ticket a requester filed still says "High" on the technician's side.
export const PORTAL_PRIORITY_OPTIONS: ReadonlyArray<{
  value: string;
  /** The grade the wire value is called, for the line under the chips. */
  name: string;
  label: string;
}> = [
  lazyFields({ value: "low" }, { name: "portal.state.priorityLowName", label: "portal.state.priorityLowLabel" }),
  lazyFields(
    { value: "normal" },
    { name: "portal.state.priorityNormalName", label: "portal.state.priorityNormalLabel" },
  ),
  lazyFields({ value: "high" }, { name: "portal.state.priorityHighName", label: "portal.state.priorityHighLabel" }),
  lazyFields(
    { value: "urgent" },
    { name: "portal.state.priorityUrgentName", label: "portal.state.priorityUrgentLabel" },
  ),
];

// --------------------------------------------------------------------------
// The attachment limit, said before it is hit.
//
// Auditor A (LOW), /portal/new -> "Attachment (optional)": "The file input
// sets accept: null and multiple: false ... No maximum size, permitted
// formats, or count is stated anywhere on the page. Absent size/type
// guidance, oversized or rejected uploads will fail -- and given this page's
// silent-failure behaviour, they will fail without a message."
//
// MIRRORS `service_desk/threads.py`'s `MAX_ATTACHMENT_BYTES` (10 MiB), which
// `portal/api.py` imports rather than redeclaring. This copy exists only to
// tell the requester the number BEFORE they pick a file; the server remains
// the authority and still refuses an oversized body (`read_bounded_body`).
// If the two ever diverge the client is merely pessimistic, never permissive.
export const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;

// Auditor A also asked for the COUNT to be stated, and for more than one
// file to be allowed: "Users troubleshooting an issue typically have several
// screenshots; being able to attach only one, with no explanation, forces
// them to file multiple tickets or omit evidence." There is no server-side
// count limit to quote (`POST /portal/tickets/{id}/attachments` takes one
// file per call and the form simply calls it once per file), so the sentence
// says exactly that rather than inventing a ceiling.
export const attachmentLimitHint = (): string => translate("portal.state.attachmentHint");

/** The message for a file the server would refuse, phrased for its owner. */
export function oversizedAttachmentMessage(file: { name: string; size: number }): string | null {
  if (file.size <= MAX_ATTACHMENT_BYTES) return null;
  return translate("portal.state.oversized", { name: file.name });
}

// --------------------------------------------------------------------------
// The enum display layer, for the values this module SHOWS as well as the
// ones it offers (Auditor A, MEDIUM: "Enum values shown raw and lowercase
// throughout the module" -- filed against the staff module, and true here
// too: the requester's own ticket read `in_progress` and `urgent` straight
// off the wire).
//
// `humaniseEnum` rather than a second hand-written map: the ticket core's
// status vocabulary (lifecycle.py) may grow a value before any map here
// does, and `pending_approval` -> "Pending approval" needs no table.
// --------------------------------------------------------------------------

// A value the console has a label for reads through the catalogue; one the
// ticket core grows before this does reads through the shared status words and
// then the derived label, so no wire word is ever printed raw.
const TICKET_STATUSES: ReadonlySet<string> = new Set([
  "new",
  "open",
  "pending",
  "resolved",
  "closed",
  "cancelled",
  "pending_approval",
  "in_progress",
  "fulfilled",
  "rejected",
]);

export function portalTicketStatusLabel(status: string): string {
  return TICKET_STATUSES.has(status) ? humaniseEnum(status, "TicketStatus") : humaniseStatus(status);
}

const TICKET_PRIORITIES: ReadonlySet<string> = new Set(["low", "normal", "high", "urgent"]);

export function portalTicketPriorityLabel(priority: string): string {
  return TICKET_PRIORITIES.has(priority) ? humaniseEnum(priority, "TicketPriority") : humaniseStatus(priority);
}

const TICKET_TYPES: ReadonlySet<string> = new Set(["incident", "request"]);

export function portalTicketTypeLabel(type: string): string {
  return TICKET_TYPES.has(type) ? humaniseEnum(type, "TicketType") : humaniseStatus(type);
}

const APPROVAL_STATES: ReadonlySet<string> = new Set(["pending", "approved", "rejected"]);

/** The outcome of the approval a request needed, as a requester reads it. */
export function portalApprovalLabel(state: string): string {
  return APPROVAL_STATES.has(state) ? humaniseEnum(state, "PortalApprovalState") : humaniseStatus(state);
}
