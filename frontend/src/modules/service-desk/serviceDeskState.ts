import { translate, type MessageKey } from "../../i18n/core";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { BadgeVariant } from "../jobs/jobState";
import type { TargetState } from "../sla/types";
import type { CommentVisibility, TicketSlaSummary } from "./types";

// Ticket status vocabulary exactly as written by the backend
// (backend/app/modules/service_desk/lifecycle.py's INCIDENT_STATUSES /
// REQUEST_STATUSES frozensets -- the sole authority on legal statuses, per
// that module's docstring: "There is no second copy"). These two arrays
// widen the same two frozensets for the status-change dropdown; they name
// the universe of legal values for a ticket's type, never the transition
// RULES between them (which from-status may reach which to-status) --
// `lifecycle.assert_transition` is the only place that logic lives, and an
// illegal transition offered here is still rejected 400 server-side
// (invariant #6: the frontend never gates security, only usability).
export const INCIDENT_STATUSES: readonly string[] = [
  "new",
  "open",
  "pending",
  "resolved",
  "closed",
  "cancelled",
];

export const REQUEST_STATUSES: readonly string[] = [
  "new",
  "pending_approval",
  "in_progress",
  "fulfilled",
  "closed",
  "rejected",
  "cancelled",
];

// lifecycle.py's TERMINAL_STATUSES: gates field edits and new comments/
// attachments ONLY (spec §3.3) -- it must never gate the status-transition
// path itself (a closed ticket is still reopenable), so this set is used
// here to disable the comment composer, attachment upload, and ticket-field
// edit form, and nowhere near the status-change control.
export const TERMINAL_STATUSES: ReadonlySet<string> = new Set(["closed", "cancelled", "rejected"]);

export function statusesForType(type: string): readonly string[] {
  return type === "request" ? REQUEST_STATUSES : INCIDENT_STATUSES;
}

// Status badge coloring: "new"/"open"/"in_progress" are active work (info);
// "pending"/"pending_approval" are waiting on someone else (warning);
// "resolved"/"fulfilled" are a successful forward conclusion (success);
// "closed"/"cancelled" are normal terminal lifecycle ends, not failures
// (muted, mirroring budgetState.ts's "closed"/"void" -> muted precedent);
// "rejected" is the one outcome that reads as a negative decision
// (destructive), distinct from a plain cancellation.
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

export function ticketStatusVariant(status: string): BadgeVariant {
  return STATUS_VARIANTS[status] ?? "muted";
}

// schema.py's TicketPriority Literal: "low" | "normal" | "high" | "urgent".
const PRIORITY_VARIANTS: Record<string, BadgeVariant> = {
  low: "muted",
  normal: "muted",
  high: "warning",
  urgent: "destructive",
};

export function ticketPriorityVariant(priority: string): BadgeVariant {
  return PRIORITY_VARIANTS[priority] ?? "muted";
}

// schema.py's CommentVisibility Literal: "public" | "internal". Internal is
// the one an end user will never see once the portal ships (spec §6) --
// given a distinct look so a technician always notices before typing.
const VISIBILITY_VARIANTS: Record<string, BadgeVariant> = {
  public: "muted",
  internal: "info",
};

export function commentVisibilityVariant(visibility: string): BadgeVariant {
  return VISIBILITY_VARIANTS[visibility] ?? "muted";
}

// --------------------------------------------------------------------------
// UX remediation cycle 8 -- the transitions that are worth stopping for.
//
// lifecycle.py draws a line this module's UI did not: `closed` is terminal
// for EDITING but reopenable (`closed -> open`, `closed -> in_progress`),
// while `cancelled` and `rejected` are FINAL -- lifecycle.py's own
// `FINAL_STATUSES`, "the subset that nothing ever leaves". Mirrored here as
// a VALUE SET only, exactly as `TERMINAL_STATUSES` above already is; the
// transition RULES stay where that file's docstring puts them ("there is no
// second copy") and an illegal transition offered here is still refused 400.
//
// WHY THE DISTINCTION IS THE WHOLE POINT. Auditor B, on the same defect in
// another module: "Resolve is a one-click, unconfirmed, irreversible state
// change -- there is no way to reopen." A confirmation that says "this
// cannot be undone" about a `closed` ticket would be a lie, and a product
// that lies in its confirmations teaches people to click through them. So
// there are two sentences here and they say different things.
export const FINAL_STATUSES: ReadonlySet<string> = new Set(["cancelled", "rejected"]);

export interface StatusChangeWarning {
  title: string;
  body: string;
  destructive: boolean;
}

/**
 * A final status as it is named inside a sentence ("Move this ticket to
 * cancelled"). The English is the wire word; the Spanish is the state's name
 * in quotes, written the way the status note on the same screen writes it.
 */
export function finalStatusWord(status: string): string {
  return FINAL_STATUSES.has(status) ? humaniseEnum(status, "FinalTicketStatus") : ticketStatusLabel(status).toLocaleLowerCase();
}

/**
 * What to tell a technician before moving a ticket to `toStatus`, or null
 * when the move needs no warning at all.
 *
 * `resolved`/`fulfilled` deliberately return null: both have a documented
 * reopen edge (`resolved -> open`, `fulfilled -> in_progress`), so stopping
 * for them would be friction that buys nothing -- the ConfirmDialog header's
 * own rule.
 */
export function statusChangeWarning(toStatus: string): StatusChangeWarning | null {
  if (FINAL_STATUSES.has(toStatus)) {
    const status = finalStatusWord(toStatus);
    return {
      title: translate("serviceDesk.state.finalTitle", { status }),
      body: translate("serviceDesk.state.finalBody", { status }),
      destructive: true,
    };
  }
  if (TERMINAL_STATUSES.has(toStatus)) {
    return {
      title: translate("serviceDesk.state.closeTitle"),
      body: translate("serviceDesk.state.closeBody"),
      destructive: false,
    };
  }
  return null;
}

// --------------------------------------------------------------------------
// The enum display layer (Auditor A, MEDIUM: "Enum values shown raw and
// lowercase throughout the module ... two labelling conventions appear
// inside a single dialog, so the interface visibly cannot decide whether it
// is speaking to a person or a database").
//
// Applied to the SELECTS the finding enumerates -- the Type/Priority/Status
// filters and the New-ticket Type control. A value the console has a label
// for reads through the catalogue; one lifecycle.py grows before this does
// reads through the shared status words and then the derived label, so no
// wire word is ever printed raw (and a dropdown never renders nothing).
// --------------------------------------------------------------------------

const TICKET_TYPES: ReadonlySet<string> = new Set(["incident", "request"]);

export function ticketTypeLabel(type: string): string {
  return TICKET_TYPES.has(type) ? humaniseEnum(type, "TicketType") : humaniseStatus(type);
}

const TICKET_PRIORITIES: ReadonlySet<string> = new Set(["low", "normal", "high", "urgent"]);

export function ticketPriorityLabel(priority: string): string {
  return TICKET_PRIORITIES.has(priority) ? humaniseEnum(priority, "TicketPriority") : humaniseStatus(priority);
}

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

export function ticketStatusLabel(status: string): string {
  return TICKET_STATUSES.has(status) ? humaniseEnum(status, "TicketStatus") : humaniseStatus(status);
}

/**
 * Every status a ticket of ANY type can hold, for the list filter.
 *
 * Auditor A (MEDIUM): "The Status filter is a free-text box while every
 * other filter beside it is a dropdown ... a typo or a wrong tense silently
 * returns nothing, and the user cannot discover the valid values." The list
 * is not scoped to a type because the filter is not either -- a caller may
 * filter by status without filtering by type.
 */
export const ALL_STATUSES: readonly string[] = [
  ...INCIDENT_STATUSES,
  ...REQUEST_STATUSES.filter((s) => !INCIDENT_STATUSES.includes(s)),
];

// --------------------------------------------------------------------------
// The queue's "Due" column and the ticket header's SLA line.
// --------------------------------------------------------------------------

const MINUTE_MS = 60_000;
const HOUR_MIN = 60;
const DAY_MIN = 24 * HOUR_MIN;

/**
 * "45 min", "2 h 30 min", "1 d 2 h": two units at most, rounded down. The unit
 * symbols (min, h, d) are the same in both languages, so only the sentence for
 * a span under a minute is catalogued.
 */
export function shortDuration(ms: number): string {
  const minutes = Math.floor(Math.abs(ms) / MINUTE_MS);
  if (minutes < 1) return translate("serviceDesk.state.underMinute");
  if (minutes < HOUR_MIN) return `${minutes} min`;
  if (minutes < DAY_MIN) {
    const hours = Math.floor(minutes / HOUR_MIN);
    const rest = minutes % HOUR_MIN;
    return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`;
  }
  const days = Math.floor(minutes / DAY_MIN);
  const hours = Math.floor((minutes % DAY_MIN) / HOUR_MIN);
  return hours === 0 ? `${days} d` : `${days} d ${hours} h`;
}

export interface DueDescription {
  label: string;
  /** Which clock the label is about, when there is one. */
  detail?: string;
  variant: BadgeVariant;
}

const TARGET_LABELS: Record<string, MessageKey> = {
  response: "serviceDesk.state.due.firstResponse",
  resolution: "serviceDesk.state.due.resolution",
};

/**
 * One cell's worth of SLA: the next deadline relative to `now`. Whether a
 * promise was breached is the server's verdict (`breached`); a deadline that
 * has passed without one is "Overdue", not "Breached".
 */
export function describeDue(
  sla: TicketSlaSummary | null | undefined,
  now: number,
): DueDescription {
  if (!sla) return { label: translate("serviceDesk.state.due.noSla"), variant: "muted" };
  if (sla.paused) return { label: translate("serviceDesk.state.due.paused"), variant: "muted" };
  if (sla.next_due_at == null) {
    if (sla.breached) return { label: translate("serviceDesk.state.due.breached"), variant: "destructive" };
    if (sla.response_state === "void" || sla.resolution_state === "void") {
      return { label: translate("serviceDesk.state.due.noDeadline"), variant: "muted" };
    }
    return { label: translate("serviceDesk.state.due.met"), variant: "success" };
  }
  const targetKey = sla.next_due_target ? TARGET_LABELS[sla.next_due_target] : undefined;
  const detail = targetKey ? translate(targetKey) : undefined;
  const remaining = Date.parse(sla.next_due_at) - now;
  if (remaining < 0) {
    const duration = shortDuration(remaining);
    const label = sla.breached
      ? translate("serviceDesk.state.due.breachedAgo", { duration })
      : translate("serviceDesk.state.due.overdueBy", { duration });
    return { label, detail, variant: "destructive" };
  }
  return {
    label: translate("serviceDesk.state.due.dueIn", { duration: shortDuration(remaining) }),
    detail,
    variant: remaining < HOUR_MIN * MINUTE_MS ? "warning" : "muted",
  };
}

/** One clock (first response or resolution) for the ticket header. */
export function describeTarget(state: TargetState, dueAt: string, now: number): DueDescription {
  switch (state) {
    case "met":
      return { label: translate("serviceDesk.state.due.met"), variant: "success" };
    case "breached":
      return { label: translate("serviceDesk.state.due.breached"), variant: "destructive" };
    case "paused":
      return { label: translate("serviceDesk.state.due.paused"), variant: "muted" };
    case "void":
      return { label: translate("serviceDesk.state.due.noDeadline"), variant: "muted" };
    case "none":
      return { label: translate("serviceDesk.state.due.noSla"), variant: "muted" };
    default: {
      const remaining = Date.parse(dueAt) - now;
      if (remaining < 0) {
        return {
          label: translate("serviceDesk.state.due.overdueBy", { duration: shortDuration(remaining) }),
          variant: "destructive",
        };
      }
      return {
        label: translate("serviceDesk.state.due.dueIn", { duration: shortDuration(remaining) }),
        variant: remaining < HOUR_MIN * MINUTE_MS ? "warning" : "muted",
      };
    }
  }
}

// --------------------------------------------------------------------------
// One-click Resolve. The server walks the transitions (opening a new ticket
// first) in one transaction; this only decides whether to offer the button.
// Mirrors `RESOLVE_PATHS` in backend lifecycle.py.
// --------------------------------------------------------------------------

const RESOLVABLE: Record<string, ReadonlySet<string>> = {
  incident: new Set(["new", "open", "pending"]),
  request: new Set(["new", "in_progress"]),
};

/** Whether Resolve can be offered for a ticket in this state. */
export function canResolve(type: string, status: string): boolean {
  return RESOLVABLE[type]?.has(status) ?? false;
}

/**
 * A ticket nobody raised (an alert opened it) has nobody to reply to, so a
 * note is the safe default; otherwise the requester is who we answer.
 */
export function defaultReplyVisibility(ticket: {
  requester_id: string | null;
}): CommentVisibility {
  return ticket.requester_id ? "public" : "internal";
}
