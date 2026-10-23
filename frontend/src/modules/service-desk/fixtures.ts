/**
 * Fixtures for the service desk: the people directory, the queues and
 * categories, and a small queue of tickets with their conversations. The
 * three routes this module owns read from here, and so do the SLA, email
 * intake and notification fixtures -- one directory and one queue list, so
 * a name on `/notifications` is the same person it is on a ticket.
 *
 * The seeds are the design's own tickets (`TIX` in
 * `docs/design/itms-prototype-v3.html`: #4127, #4125, #4119) plus enough
 * around them for every filter, every tile and the default view to have
 * something to show. Timestamps are relative to now, so "2h ago" stays
 * "2h ago" on every run.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { devices } from "../../fixtures/core";
import { ApiError } from "../../lib/api";
// Circular with the SLA fixtures, which read tickets from here; only called at request time.
import { ticketSlaSummary } from "../sla/fixtures";
import { ME_ID } from "../../fixtures/me";
import type { RoleAssignmentOut, UserOut } from "../settings/types";
import type {
  AttachmentOut,
  CannedResponseOut,
  CategoryOut,
  CommentCreate,
  CommentOut,
  QueueOut,
  StatusHistoryOut,
  TicketOut,
} from "./types";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** An ISO timestamp `minutes` before the call. */
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* ----------------------------------------------------------------- people */

// In its own module and re-exported: `sla/fixtures` needs it too, and
// importing it from here closed a cycle (this file imports `ticketSlaSummary`
// from there) that crashed fixture mode with "Cannot access 'ME_ID' before
// initialization" whenever the SLA fixtures happened to load first.
export { ME_ID } from "../../fixtures/me";

/**
 * WHO HOLDS WHAT. Mirrors `../settings/fixtures.ts`'s own `GRANTS` map --
 * that file's source of truth for role assignment -- so a person here is
 * the same person there. Before this, every seeded user carried `roles: []`
 * regardless of `GRANTS`, so the directory's chip, its `·` scope suffix and
 * its `×N` duplicate count had no rendered coverage in either browser
 * harness, and the Roles drawer read "holds no roles" for all seven people
 * at every viewport (design review, 2026-09-11).
 *
 * NOT IMPORTED FROM `GRANTS` ITSELF: `settings/fixtures.ts` already imports
 * `USERS` from this file, and importing `GRANTS` back the other way would
 * make the two fixture files circular. The values below are restated
 * instead -- keep them matched to `GRANTS` there if it changes.
 *
 * Tomas Kalu carries the SCOPED grant (`technician` at `grp-servers`, not
 * everywhere) and Adaeze Okonkwo's is deliberately DUPLICATED (two
 * identical org-wide `technician` grants) -- `GRANTS` itself holds no
 * duplicate to draw from, and the `×N` collapsing had no seed anywhere
 * without one.
 */
const roleGrant = (
  assignmentId: string,
  roleId: string,
  roleName: string,
  deviceGroupId: string | null = null,
): RoleAssignmentOut => ({
  assignment_id: assignmentId,
  role_id: roleId,
  role_name: roleName,
  device_group_id: deviceGroupId,
});

export const USERS: UserOut[] = [
  {
    id: ME_ID,
    email: "helgar.palmieri@toeshee.example",
    display_name: "Helgar Palmieri",
    status: "active",
    roles: [roleGrant("sd-1", "role-org-admin", "org-admin")],
  },
  {
    id: "usr-ao",
    email: "adaeze.okonkwo@toeshee.example",
    display_name: "Adaeze Okonkwo",
    status: "active",
    // Duplicated on purpose -- see the comment above `USERS`.
    roles: [
      roleGrant("sd-2a", "role-technician", "technician"),
      roleGrant("sd-2b", "role-technician", "technician"),
    ],
  },
  {
    id: "usr-mr",
    email: "mateus.ribeiro@toeshee.example",
    display_name: "Mateus Ribeiro",
    status: "active",
    roles: [
      roleGrant("sd-3", "role-technician", "technician"),
      roleGrant("sd-4", "role-service-desk", "service-desk"),
    ],
  },
  {
    id: "usr-tk",
    email: "tomas.kalu@toeshee.example",
    display_name: "Tomas Kalu",
    status: "active",
    // Scoped, not everywhere -- see the comment above `USERS`.
    roles: [roleGrant("sd-5", "role-technician", "technician", "grp-servers")],
  },
  {
    id: "usr-lf",
    email: "leila.farah@toeshee.example",
    display_name: "Leila Farah",
    status: "active",
    roles: [roleGrant("sd-6", "role-service-desk", "service-desk")],
  },
  {
    id: "usr-ic",
    email: "ines.caetano@toeshee.example",
    display_name: "Ines Caetano",
    status: "active",
    roles: [roleGrant("sd-7", "role-finance", "finance")],
  },
  {
    id: "usr-rs",
    email: "rui.santos@toeshee.example",
    display_name: "Rui Santos",
    status: "disabled",
    roles: [roleGrant("sd-8", "role-auditor", "auditor")],
  },
];

/* ------------------------------------------------------------------ queues */

const SEEDED_AT = ago(180 * DAY / MINUTE);

export const QUEUES: QueueOut[] = [
  { id: "q-network", key: "network", name: "Network", description: "Connectivity, VPN, Wi-Fi", active: true, sort_order: 10, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "q-hardware", key: "hardware", name: "Hardware", description: "Laptops, peripherals, replacements", active: true, sort_order: 20, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "q-facilities", key: "facilities", name: "Facilities", description: "Printers, meeting rooms, desks", active: true, sort_order: 30, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "q-access", key: "access", name: "Access", description: "Accounts, shares, permissions", active: true, sort_order: 40, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "q-legacy", key: "legacy", name: "Legacy helpdesk", description: "Retired after the 2025 migration", active: false, sort_order: 90, created_by: ME_ID, created_at: SEEDED_AT },
];

export const CATEGORIES: CategoryOut[] = [
  { id: "cat-vpn", key: "vpn", name: "VPN and remote access", type: "incident", requires_approval: false, approver_role_id: null, default_queue_id: "q-network", active: true, sort_order: 10, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "cat-wifi", key: "wifi", name: "Wi-Fi and cabling", type: "incident", requires_approval: false, approver_role_id: null, default_queue_id: "q-network", active: true, sort_order: 11, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "cat-hardware", key: "hardware_fault", name: "Hardware fault", type: "incident", requires_approval: false, approver_role_id: null, default_queue_id: "q-hardware", active: true, sort_order: 20, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "cat-printer", key: "printers", name: "Printers", type: "incident", requires_approval: false, approver_role_id: null, default_queue_id: "q-facilities", active: true, sort_order: 30, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "cat-software", key: "software", name: "Software", type: "incident", requires_approval: false, approver_role_id: null, default_queue_id: "q-hardware", active: true, sort_order: 40, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "cat-laptop", key: "laptop_request", name: "New laptop", type: "request", requires_approval: true, approver_role_id: null, default_queue_id: "q-hardware", active: true, sort_order: 50, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "cat-access", key: "access_request", name: "Access request", type: "request", requires_approval: true, approver_role_id: null, default_queue_id: "q-access", active: true, sort_order: 60, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "cat-software-req", key: "software_request", name: "Software request", type: "request", requires_approval: false, approver_role_id: null, default_queue_id: "q-hardware", active: true, sort_order: 70, created_by: ME_ID, created_at: SEEDED_AT },
  { id: "cat-other", key: "other", name: "Something else", type: "incident", requires_approval: false, approver_role_id: null, default_queue_id: null, active: false, sort_order: 99, created_by: ME_ID, created_at: SEEDED_AT },
];

/* ----------------------------------------------------------------- tickets */

interface TicketSeed {
  id: string;
  number: string;
  type: "incident" | "request";
  category: string;
  queue: string;
  status: string;
  priority: string;
  subject: string;
  description: string;
  requester: string | null;
  assignee: string | null;
  device: string | null;
  origin: string;
  alert: string | null;
  /** Minutes ago. */
  opened: number;
  updated: number;
  resolved?: number;
  closed?: number;
  attachments?: boolean;
}

const TICKET_SEEDS: TicketSeed[] = [
  { id: "tkt-1", number: "INC-4127", type: "incident", category: "cat-vpn", queue: "q-network", status: "open", priority: "urgent", subject: "VPN drops every 20 minutes", description: "Every ~20 minutes the VPN drops for about 30 seconds. Started after the update on Friday.\n\nHappens on the office Wi-Fi and at home.", requester: "usr-ao", assignee: "usr-mr", device: "dev-1", origin: "portal", alert: null, opened: 2 * 60, updated: 60, attachments: true },
  { id: "tkt-2", number: "REQ-4125", type: "request", category: "cat-laptop", queue: "q-hardware", status: "pending_approval", priority: "high", subject: "New laptop for hire starting Monday", description: "Sara Mendes joins Finance on Monday. Standard laptop, docking station and a second screen.", requester: "usr-lf", assignee: "usr-tk", device: null, origin: "portal", alert: null, opened: DAY / MINUTE, updated: 5 * 60 },
  { id: "tkt-3", number: "INC-4119", type: "incident", category: "cat-printer", queue: "q-facilities", status: "open", priority: "normal", subject: "Printer offline on 3rd floor", description: "The floor printer shows offline on every machine since this morning. The display says 'check paper path'.", requester: "usr-ic", assignee: "usr-tk", device: "dev-11", origin: "technician", alert: null, opened: 2 * DAY / MINUTE, updated: DAY / MINUTE },
  { id: "tkt-4", number: "INC-4131", type: "incident", category: "cat-hardware", queue: "q-hardware", status: "new", priority: "high", subject: "Disk 91% full on lis-db-01", description: "Raised by health check disk.usage: / is 91% full on lis-db-01, threshold 90%.", requester: null, assignee: null, device: "dev-6", origin: "alert", alert: "alert-disk-lis-db-01", opened: 40, updated: 40 },
  { id: "tkt-5", number: "INC-4118", type: "incident", category: "cat-software", queue: "q-hardware", status: "pending", priority: "low", subject: "Outlook keeps asking for the password", description: "Three or four times a day Outlook pops the credential prompt. Entering the password works until the next time.", requester: "usr-ao", assignee: ME_ID, device: "dev-2", origin: "email", alert: null, opened: 3 * DAY / MINUTE, updated: 6 * 60 },
  { id: "tkt-6", number: "INC-4129", type: "incident", category: "cat-wifi", queue: "q-network", status: "open", priority: "normal", subject: "Wi-Fi drops in meeting room B", description: "Calls from meeting room B drop every few minutes. Room A on the same floor is fine.", requester: "usr-lf", assignee: null, device: null, origin: "email", alert: null, opened: 5 * 60, updated: 5 * 60 },
  { id: "tkt-7", number: "INC-4124", type: "incident", category: "cat-hardware", queue: "q-hardware", status: "open", priority: "normal", subject: "Docking station not detecting second screen", description: "The second screen on the dock stays black after the laptop wakes. Unplugging the dock fixes it until the next sleep.", requester: "usr-ic", assignee: ME_ID, device: "dev-3", origin: "portal", alert: null, opened: 26 * 60, updated: 20 * 60 },
  { id: "tkt-8", number: "REQ-4110", type: "request", category: "cat-access", queue: "q-access", status: "fulfilled", priority: "normal", subject: "Access to the finance share", description: "Read access to \\\\lis-fs-01\\finance for the audit.", requester: ME_ID, assignee: "usr-mr", device: null, origin: "portal", alert: null, opened: 26 * DAY / MINUTE, updated: 24 * DAY / MINUTE, resolved: 24 * DAY / MINUTE },
  { id: "tkt-9", number: "INC-4102", type: "incident", category: "cat-vpn", queue: "q-network", status: "closed", priority: "normal", subject: "Cannot reach the intranet from home", description: "Intranet times out over VPN; everything else works.", requester: "usr-tk", assignee: "usr-mr", device: "dev-4", origin: "technician", alert: null, opened: 40 * DAY / MINUTE, updated: 33 * DAY / MINUTE, resolved: 34 * DAY / MINUTE, closed: 33 * DAY / MINUTE },
  { id: "tkt-10", number: "REQ-4098", type: "request", category: "cat-software-req", queue: "q-hardware", status: "rejected", priority: "low", subject: "Licence for a screen recorder", description: "Would like a licence for a paid screen recorder for training videos.", requester: "usr-lf", assignee: null, device: null, origin: "portal", alert: null, opened: 50 * DAY / MINUTE, updated: 48 * DAY / MINUTE, closed: 48 * DAY / MINUTE },
  { id: "tkt-12", number: "INC-4133", type: "incident", category: "cat-hardware", queue: "q-hardware", status: "open", priority: "normal", subject: "Second screen flickers after waking", description: "After the laptop wakes from sleep the external screen flickers for a minute or so, then settles. Started this week.", requester: ME_ID, assignee: "usr-tk", device: "dev-1", origin: "portal", alert: null, opened: 3 * 60, updated: 70 },
  { id: "tkt-11", number: "INC-4130", type: "incident", category: "cat-hardware", queue: "q-hardware", status: "resolved", priority: "high", subject: "Laptop will not power on", description: "No lights at all, even on the charger. Swapped the charger, no change.", requester: "usr-ic", assignee: "usr-tk", device: "dev-14", origin: "portal", alert: null, opened: 30 * 60, updated: 3 * 60, resolved: 3 * 60 },
];

function ticket(seed: TicketSeed): TicketOut {
  return {
    id: seed.id,
    number: seed.number,
    type: seed.type,
    category_id: seed.category,
    queue_id: seed.queue,
    status: seed.status,
    priority: seed.priority,
    subject: seed.subject,
    description: seed.description,
    requester_id: seed.requester,
    assigned_to: seed.assignee,
    device_id: seed.device,
    origin: seed.origin,
    source_alert_id: seed.alert,
    resolved_at: seed.resolved === undefined ? null : ago(seed.resolved),
    closed_at: seed.closed === undefined ? null : ago(seed.closed),
    has_attachments: seed.attachments ?? false,
    created_by: seed.origin === "technician" ? ME_ID : null,
    created_at: ago(seed.opened),
    updated_at: ago(seed.updated),
  };
}

/** Mutable within a page session so a create or an edit shows up in the list. */
const tickets: TicketOut[] = TICKET_SEEDS.map(ticket);
const queues: QueueOut[] = [...QUEUES];
const categories: CategoryOut[] = [...CATEGORIES];

/** Every ticket, newest first, as `GET /service-desk/tickets` orders them. */
export const allTickets = (): TicketOut[] =>
  [...tickets].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));

const TERMINAL = new Set(["closed", "cancelled", "rejected"]);

const withSla = (t: TicketOut): TicketOut => ({ ...t, sla: ticketSlaSummary(t) });

/** The next deadline as `sort=due` orders it: owed deadlines first. */
const dueKey = (t: TicketOut): number => {
  const next = t.sla?.next_due_at;
  return next ? Date.parse(next) : Number.POSITIVE_INFINITY;
};

const listTickets: FixtureMap["listTickets"] = (filters = {}) => {
  const q = filters.q?.trim().toLowerCase();
  const scope = filters.scope ?? "all";
  const matched = allTickets().filter(
    (t) =>
      (scope === "all" || !TERMINAL.has(t.status)) &&
      (scope !== "mine" || t.assigned_to === ME_ID) &&
      (scope !== "unassigned" || t.assigned_to === null) &&
      (!filters.type || t.type === filters.type) &&
      (!filters.status || t.status === filters.status) &&
      (!filters.queue_id || t.queue_id === filters.queue_id) &&
      (!filters.assigned_to || t.assigned_to === filters.assigned_to) &&
      (!filters.requester_id || t.requester_id === filters.requester_id) &&
      (!filters.device_id || t.device_id === filters.device_id) &&
      (!filters.priority || t.priority === filters.priority) &&
      (!q || t.subject.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)),
  );
  const rows = matched.map(withSla);
  if (filters.sort === "due") rows.sort((a, b) => dueKey(a) - dueKey(b));
  const offset = filters.offset ?? 0;
  const limit = filters.limit ?? 50;
  return { items: rows.slice(offset, offset + limit), total: rows.length };
};

export function getTicket(id: string): TicketOut {
  const found = tickets.find((t) => t.id === id);
  if (!found) throw notFound("ticket");
  return found;
}

let created = 0;

/**
 * A new ticket row, the way `create_ticket` writes one: the category's
 * default queue unless one was named, status `new`, and the origin the
 * caller's route decides (the technician API says `technician`, the portal
 * says `portal`). Shared with the portal fixtures so both file into the same
 * queue.
 */
export function insertTicket(
  body: {
    type: "incident" | "request";
    category_id: string;
    queue_id?: string | null;
    subject: string;
    description?: string;
    priority?: string;
    requester_id?: string | null;
    assigned_to?: string | null;
    device_id?: string | null;
  },
  origin: "technician" | "portal",
): TicketOut {
  created += 1;
  const category = categories.find((c) => c.id === body.category_id);
  const next: TicketOut = {
    id: `tkt-new-${created}`,
    number: `${body.type === "request" ? "REQ" : "INC"}-${4132 + created}`,
    type: body.type,
    category_id: body.category_id,
    queue_id: body.queue_id ?? category?.default_queue_id ?? "q-hardware",
    status: category?.requires_approval ? "pending_approval" : "new",
    priority: body.priority ?? "normal",
    subject: body.subject,
    description: body.description ?? "",
    requester_id: body.requester_id ?? null,
    assigned_to: body.assigned_to ?? null,
    device_id: body.device_id ?? null,
    origin,
    source_alert_id: null,
    resolved_at: null,
    closed_at: null,
    has_attachments: false,
    created_by: origin === "technician" ? ME_ID : null,
    created_at: ago(0),
    updated_at: ago(0),
  };
  tickets.push(next);
  history.push({
    id: `hist-${next.id}`,
    ticket_id: next.id,
    from_status: null,
    to_status: next.status,
    actor_type: "user",
    actor_id: body.requester_id ?? ME_ID,
    reason: "",
    created_at: next.created_at,
  });
  return next;
}

const createTicket: FixtureMap["createTicket"] = (body) => insertTicket(body, "technician");

function patch(id: string, change: Partial<TicketOut>): TicketOut {
  const at = tickets.findIndex((t) => t.id === id);
  const current = tickets[at];
  if (at < 0 || !current) throw notFound("ticket");
  const next: TicketOut = { ...current, ...change, updated_at: ago(0) };
  tickets[at] = next;
  return next;
}

const updateTicket: FixtureMap["updateTicket"] = (id, body) =>
  patch(id, {
    ...(body.category_id != null ? { category_id: body.category_id } : {}),
    ...(body.priority != null ? { priority: body.priority } : {}),
    ...(body.subject != null ? { subject: body.subject } : {}),
    ...(body.description != null ? { description: body.description } : {}),
    ...(body.device_id !== undefined ? { device_id: body.device_id } : {}),
  });

const changeTicketStatus: FixtureMap["changeTicketStatus"] = (id, body) => {
  const before = getTicket(id);
  history.push({
    id: `hist-${id}-${history.length}`,
    ticket_id: id,
    from_status: before.status,
    to_status: body.to_status,
    actor_type: "user",
    actor_id: ME_ID,
    reason: body.reason ?? "",
    created_at: ago(0),
  });
  const terminal = body.to_status === "closed" || body.to_status === "cancelled" || body.to_status === "rejected";
  const done = body.to_status === "resolved" || body.to_status === "fulfilled";
  return patch(id, {
    status: body.to_status,
    ...(done ? { resolved_at: ago(0) } : {}),
    ...(terminal ? { closed_at: ago(0) } : {}),
  });
};

const assignTicket: FixtureMap["assignTicket"] = (id, body) =>
  patch(id, {
    ...(body.assigned_to !== undefined ? { assigned_to: body.assigned_to } : {}),
    ...(body.queue_id != null ? { queue_id: body.queue_id } : {}),
  });

const approveTicket: FixtureMap["approveTicket"] = (id) => changeTicketStatus(id, { to_status: "in_progress", reason: "Approved" }) as TicketOut;
const rejectTicket: FixtureMap["rejectTicket"] = (id, body) => changeTicketStatus(id, { to_status: "rejected", reason: body.reason ?? "" }) as TicketOut;

/* ------------------------------------------------------- conversations */

const comments: CommentOut[] = [
  { id: "cmt-1", ticket_id: "tkt-1", author_id: "usr-ao", author_type: "user", body: "It is worse on video calls: the call freezes, then reconnects about half a minute later.", visibility: "public", created_at: ago(110) },
  { id: "cmt-2", ticket_id: "tkt-1", author_id: "usr-mr", author_type: "user", body: "Seeing DHCP lease renewals at the same interval on lis-ws-041. Checking the client config.", visibility: "public", created_at: ago(60) },
  { id: "cmt-3", ticket_id: "tkt-1", author_id: "usr-mr", author_type: "user", body: "Lease time on the office scope is 20 minutes. Suspect the Friday client update stopped honouring renewals over the tunnel.", visibility: "internal", created_at: ago(45) },
  { id: "cmt-4", ticket_id: "tkt-2", author_id: "usr-tk", author_type: "user", body: "Standard build is in stock. Waiting on the manager's approval before I image it.", visibility: "public", created_at: ago(5 * 60) },
  { id: "cmt-5", ticket_id: "tkt-3", author_id: "usr-tk", author_type: "user", body: "Cleared a jam in the duplex unit; it printed a test page. Watching it for the day before resolving.", visibility: "public", created_at: ago(DAY / MINUTE) },
  { id: "cmt-6", ticket_id: "tkt-8", author_id: "usr-mr", author_type: "user", body: "Added you to the finance-readers group. It takes effect at your next sign-in.", visibility: "public", created_at: ago(24 * DAY / MINUTE) },
  { id: "cmt-7", ticket_id: "tkt-4", author_id: null, author_type: "system", body: "Opened automatically from health alert disk.usage on lis-db-01.", visibility: "internal", created_at: ago(40) },
];

const attachments: AttachmentOut[] = [
  { id: "att-1", ticket_id: "tkt-1", filename: "vpn-client.log", byte_size: 48_213, uploaded_by: "usr-ao", created_at: ago(115) },
  { id: "att-2", ticket_id: "tkt-1", filename: "disconnect-screenshot.png", byte_size: 312_884, uploaded_by: "usr-ao", created_at: ago(114) },
];

const history: StatusHistoryOut[] = [
  { id: "hist-1", ticket_id: "tkt-1", from_status: null, to_status: "new", actor_type: "user", actor_id: "usr-ao", reason: "", created_at: ago(120) },
  { id: "hist-2", ticket_id: "tkt-1", from_status: "new", to_status: "open", actor_type: "user", actor_id: "usr-mr", reason: "Picked up", created_at: ago(95) },
  { id: "hist-3", ticket_id: "tkt-2", from_status: null, to_status: "new", actor_type: "user", actor_id: "usr-lf", reason: "", created_at: ago(DAY / MINUTE) },
  { id: "hist-4", ticket_id: "tkt-2", from_status: "new", to_status: "pending_approval", actor_type: "system", actor_id: null, reason: "Category requires approval", created_at: ago(DAY / MINUTE - 1) },
  { id: "hist-5", ticket_id: "tkt-3", from_status: null, to_status: "new", actor_type: "user", actor_id: ME_ID, reason: "", created_at: ago(2 * DAY / MINUTE) },
  { id: "hist-6", ticket_id: "tkt-3", from_status: "new", to_status: "open", actor_type: "user", actor_id: "usr-tk", reason: "", created_at: ago(2 * DAY / MINUTE - 30) },
  { id: "hist-7", ticket_id: "tkt-8", from_status: null, to_status: "new", actor_type: "user", actor_id: ME_ID, reason: "", created_at: ago(26 * DAY / MINUTE) },
  { id: "hist-8", ticket_id: "tkt-8", from_status: "new", to_status: "pending_approval", actor_type: "system", actor_id: null, reason: "Category requires approval", created_at: ago(26 * DAY / MINUTE) },
  { id: "hist-9", ticket_id: "tkt-8", from_status: "pending_approval", to_status: "in_progress", actor_type: "user", actor_id: "usr-ao", reason: "Approved for the audit period", created_at: ago(25 * DAY / MINUTE) },
  { id: "hist-10", ticket_id: "tkt-8", from_status: "in_progress", to_status: "fulfilled", actor_type: "user", actor_id: "usr-mr", reason: "Group membership added", created_at: ago(24 * DAY / MINUTE) },
];

/** The conversation of one ticket, oldest first. Exported for the portal's view of the same ticket. */
export const commentsFor = (ticketId: string, includeInternal: boolean): CommentOut[] =>
  comments
    .filter((c) => c.ticket_id === ticketId && (includeInternal || c.visibility === "public"))
    .sort((a, b) => (a.created_at < b.created_at ? -1 : 1));

export const attachmentsFor = (ticketId: string): AttachmentOut[] =>
  attachments.filter((a) => a.ticket_id === ticketId);

export const historyFor = (ticketId: string): StatusHistoryOut[] =>
  history.filter((h) => h.ticket_id === ticketId).sort((a, b) => (a.created_at < b.created_at ? -1 : 1));

const listTicketComments: FixtureMap["listTicketComments"] = (id, includeInternal = true) => {
  getTicket(id);
  return commentsFor(id, includeInternal);
};

export function createTicketComment(id: string, body: CommentCreate): CommentOut {
  getTicket(id);
  const next: CommentOut = {
    id: `cmt-new-${comments.length}`,
    ticket_id: id,
    author_id: ME_ID,
    author_type: "user",
    body: body.body,
    visibility: body.visibility ?? "public",
    created_at: ago(0),
  };
  comments.push(next);
  patch(id, {});
  return next;
}

const resolveTicket: FixtureMap["resolveTicket"] = (id, body) => {
  const ticket = getTicket(id);
  if (body.reply) createTicketComment(id, body.reply);
  const incident = ticket.type === "incident";
  if (ticket.status === "new") changeTicketStatus(id, { to_status: incident ? "open" : "in_progress" });
  return changeTicketStatus(id, { to_status: incident ? "resolved" : "fulfilled" }) as TicketOut;
};

const listTicketAttachments: FixtureMap["listTicketAttachments"] = (id) => {
  getTicket(id);
  return attachmentsFor(id);
};

export function uploadTicketAttachment(id: string, file: File): AttachmentOut {
  getTicket(id);
  const next: AttachmentOut = {
    id: `att-new-${attachments.length}`,
    ticket_id: id,
    filename: file.name,
    byte_size: file.size,
    uploaded_by: ME_ID,
    created_at: ago(0),
  };
  attachments.push(next);
  patch(id, { has_attachments: true });
  return next;
}

export function downloadTicketAttachment(attachmentId: string): Blob {
  const found = attachments.find((a) => a.id === attachmentId);
  if (!found) throw notFound("attachment");
  return new Blob([`fixture attachment ${found.filename}`], { type: "text/plain" });
}

const listTicketStatusHistory: FixtureMap["listTicketStatusHistory"] = (id) => {
  getTicket(id);
  return historyFor(id);
};

/* -------------------------------------------------- queues, categories */

const listTicketQueues: FixtureMap["listTicketQueues"] = (includeInactive = false) =>
  queues.filter((q) => includeInactive || q.active).sort((a, b) => a.sort_order - b.sort_order);

const createTicketQueue: FixtureMap["createTicketQueue"] = (body) => {
  const next: QueueOut = {
    id: `q-${body.key}`,
    key: body.key,
    name: body.name,
    description: body.description ?? "",
    active: true,
    sort_order: body.sort_order ?? 50,
    created_by: ME_ID,
    created_at: ago(0),
  };
  queues.push(next);
  return next;
};

const updateTicketQueue: FixtureMap["updateTicketQueue"] = (id, body) => {
  const at = queues.findIndex((q) => q.id === id);
  const current = queues[at];
  if (at < 0 || !current) throw notFound("queue");
  const next: QueueOut = {
    ...current,
    ...(body.name != null ? { name: body.name } : {}),
    ...(body.description != null ? { description: body.description } : {}),
    ...(body.active != null ? { active: body.active } : {}),
    ...(body.sort_order != null ? { sort_order: body.sort_order } : {}),
  };
  queues[at] = next;
  return next;
};

/** The active categories, in their configured order. Exported for the portal's catalogue. */
export const categoriesFor = (includeInactive = false): CategoryOut[] =>
  categories.filter((c) => includeInactive || c.active).sort((a, b) => a.sort_order - b.sort_order);

const listTicketCategories: FixtureMap["listTicketCategories"] = (includeInactive = false) =>
  categoriesFor(includeInactive);

const createTicketCategory: FixtureMap["createTicketCategory"] = (body) => {
  const next: CategoryOut = {
    id: `cat-${body.key}`,
    key: body.key,
    name: body.name,
    type: body.type,
    requires_approval: body.requires_approval ?? false,
    approver_role_id: body.approver_role_id ?? null,
    default_queue_id: body.default_queue_id ?? null,
    active: true,
    sort_order: body.sort_order ?? 80,
    created_by: ME_ID,
    created_at: ago(0),
  };
  categories.push(next);
  return next;
};

const updateTicketCategory: FixtureMap["updateTicketCategory"] = (id, body) => {
  const at = categories.findIndex((c) => c.id === id);
  const current = categories[at];
  if (at < 0 || !current) throw notFound("category");
  const next: CategoryOut = {
    ...current,
    ...(body.name != null ? { name: body.name } : {}),
    ...(body.requires_approval != null ? { requires_approval: body.requires_approval } : {}),
    ...(body.approver_role_id !== undefined ? { approver_role_id: body.approver_role_id } : {}),
    ...(body.default_queue_id !== undefined ? { default_queue_id: body.default_queue_id } : {}),
    ...(body.active != null ? { active: body.active } : {}),
    ...(body.sort_order != null ? { sort_order: body.sort_order } : {}),
  };
  categories[at] = next;
  return next;
};

/* ------------------------------------------------------------ saved replies */

const cannedResponses: CannedResponseOut[] = [
  {
    id: "cr-restart",
    title: "Restart and tell us",
    body: "Could you restart the machine and let us know here if it happens again? That rules out the most common cause.",
    created_by: ME_ID,
    created_at: ago(30 * DAY / MINUTE),
    updated_by: null,
    updated_at: null,
  },
  {
    id: "cr-looking",
    title: "We are looking into it",
    body: "Thanks for the detail. We are looking into this now and will update you here.",
    created_by: ME_ID,
    created_at: ago(30 * DAY / MINUTE),
    updated_by: null,
    updated_at: null,
  },
];

const byTitle = (a: CannedResponseOut, b: CannedResponseOut) =>
  a.title.toLowerCase().localeCompare(b.title.toLowerCase());

const listCannedResponses: FixtureMap["listCannedResponses"] = () => [...cannedResponses].sort(byTitle);

const createCannedResponse: FixtureMap["createCannedResponse"] = (body) => {
  const row: CannedResponseOut = {
    id: `cr-${cannedResponses.length + 1}`,
    title: body.title,
    body: body.body,
    created_by: ME_ID,
    created_at: new Date().toISOString(),
    updated_by: null,
    updated_at: null,
  };
  cannedResponses.push(row);
  return row;
};

const updateCannedResponse: FixtureMap["updateCannedResponse"] = (id, body) => {
  const row = cannedResponses.find((c) => c.id === id);
  if (!row) throw notFound("saved reply");
  Object.assign(row, body, { updated_by: ME_ID, updated_at: new Date().toISOString() });
  return row;
};

const deleteCannedResponse: FixtureMap["deleteCannedResponse"] = (id) => {
  const at = cannedResponses.findIndex((c) => c.id === id);
  if (at < 0) throw notFound("saved reply");
  cannedResponses.splice(at, 1);
};

/* --------------------------------------------------------------- directory */

const listUsers: FixtureMap["listUsers"] = () => ({ users: [...USERS] });

/* ----------------------------------------------------------------- copilot */

const createCopilotSession: FixtureMap["createCopilotSession"] = (ticketId, mode) => {
  getTicket(ticketId);
  return { id: `copilot-${ticketId}`, persona: "technician", mode, model_id: "fixture", message_count: 0 };
};

const createCopilotTurn: FixtureMap["createCopilotTurn"] = (ticketId, _sessionId, question) => {
  const found = getTicket(ticketId);
  return {
    answer: `Draft reply for ${found.number} (you asked: "${question}").\n\nThanks for the detail. The pattern you describe matches the DHCP lease renewals we can see on ${devices().find((d) => d.id === found.device_id)?.hostname ?? "your machine"}; we are checking the VPN client's renewal behaviour and will update you here.`,
    stop_reason: "end_turn",
    partial: false,
    tool_calls: [],
    flagged: [],
    proposed: [],
  };
};

const fixtures: FixtureMap = {
  listCannedResponses,
  createCannedResponse,
  updateCannedResponse,
  deleteCannedResponse,
  listTickets,
  getTicket: (id) => withSla(getTicket(id)),
  createTicket,
  updateTicket,
  changeTicketStatus,
  assignTicket,
  approveTicket,
  rejectTicket,
  listTicketComments,
  createTicketComment,
  resolveTicket,
  listTicketAttachments,
  uploadTicketAttachment,
  downloadTicketAttachment,
  listTicketStatusHistory,
  listTicketQueues,
  createTicketQueue,
  updateTicketQueue,
  listTicketCategories,
  createTicketCategory,
  updateTicketCategory,
  listUsers,
  createCopilotSession,
  createCopilotTurn,
};

export default fixtures;
