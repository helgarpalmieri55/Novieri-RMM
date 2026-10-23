/**
 * Fixtures for the end-user portal: the signed-in requester's view of the
 * same queue the service desk fixtures hold. `listPortalTickets` is the
 * tickets they raised; `getPortalTicket` is one ticket with the public half
 * of its conversation, its own attachments and a reason-less status history,
 * exactly the fields `PortalTicketDetail` carries and nothing the server
 * would withhold.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import {
  ME_ID,
  allTickets,
  attachmentsFor,
  categoriesFor,
  commentsFor,
  createTicketComment,
  downloadTicketAttachment,
  getTicket,
  historyFor,
  insertTicket,
  uploadTicketAttachment,
} from "../service-desk/fixtures";
import { ApiError } from "../../lib/api";
import type { TicketOut } from "../service-desk/types";
import type {
  PortalApprovalOut,
  PortalHelpArticle,
  PortalTicketDetail,
  PortalTicketSummary,
} from "./types";

function summary(t: TicketOut): PortalTicketSummary {
  return {
    id: t.id,
    number: t.number,
    type: t.type,
    status: t.status,
    priority: t.priority,
    subject: t.subject,
    created_at: t.created_at,
    updated_at: t.updated_at,
  };
}

/** The approval outcome a request's status implies, with the approver's reason from the history. */
function approvalFor(t: TicketOut): PortalApprovalOut | null {
  if (t.type !== "request") return null;
  const decided = historyFor(t.id).find((h) => h.from_status === "pending_approval");
  if (t.status === "pending_approval") return { state: "pending", reason: "", decided_at: null };
  if (t.status === "rejected") {
    return { state: "rejected", reason: decided?.reason || "Not approved by the budget holder.", decided_at: decided?.created_at ?? t.closed_at };
  }
  if (decided) return { state: "approved", reason: decided.reason, decided_at: decided.created_at };
  return null;
}

function detail(t: TicketOut): PortalTicketDetail {
  return {
    ...summary(t),
    description: t.description,
    comments: commentsFor(t.id, false).map((c) => ({ id: c.id, body: c.body, created_at: c.created_at })),
    status_history: historyFor(t.id).map((h) => ({
      from_status: h.from_status,
      to_status: h.to_status,
      created_at: h.created_at,
    })),
    attachments: attachmentsFor(t.id).map((a) => ({
      id: a.id,
      filename: a.filename,
      byte_size: a.byte_size,
      created_at: a.created_at,
    })),
    approval: approvalFor(t),
  };
}

const listPortalTickets: FixtureMap["listPortalTickets"] = (filters = {}) => {
  const mine = allTickets().filter((t) => t.requester_id === ME_ID);
  const offset = filters.offset ?? 0;
  const limit = filters.limit ?? 50;
  return mine.slice(offset, offset + limit).map(summary);
};

// Answers any ticket the desk holds, not only the requester's own: the
// gate's sample id (`/portal/tickets/tkt-1`) is the design's own #4127, and
// the row-level isolation that would refuse another person's ticket is the
// server's, not something a fixture can stand in for honestly.
const getPortalTicket: FixtureMap["getPortalTicket"] = (id) => detail(getTicket(id));

const listPortalCategories: FixtureMap["listPortalCategories"] = () =>
  categoriesFor().map((c) => ({ id: c.id, key: c.key, name: c.name, type: c.type }));

const createPortalTicket: FixtureMap["createPortalTicket"] = (body) => {
  const category = categoriesFor().find((c) => c.id === body.category_id);
  const created = insertTicket(
    {
      type: category?.type === "request" ? "request" : "incident",
      category_id: body.category_id,
      subject: body.subject,
      description: body.description ?? "",
      priority: body.priority ?? "normal",
      requester_id: ME_ID,
    },
    "portal",
  );
  return detail(created);
};

const createPortalComment: FixtureMap["createPortalComment"] = (id, body) => {
  const comment = createTicketComment(id, { body: body.body, visibility: "public" });
  return { id: comment.id, body: comment.body, created_at: comment.created_at };
};

const uploadPortalAttachment: FixtureMap["uploadPortalAttachment"] = (id, file) => {
  const a = uploadTicketAttachment(id, file);
  return { id: a.id, filename: a.filename, byte_size: a.byte_size, created_at: a.created_at };
};

const downloadPortalAttachment: FixtureMap["downloadPortalAttachment"] = (_ticketId, attachmentId) =>
  downloadTicketAttachment(attachmentId);

const createResolverSession: FixtureMap["createResolverSession"] = () => ({
  id: "resolver-1",
  mode: "read",
  message_count: 0,
});

const createResolverTurn: FixtureMap["createResolverTurn"] = (_sessionId, question) => ({
  answer: `You asked: "${question}".\n\nYour open ticket INC-4133 (second screen flickers after waking) is with Tomas Kalu in the Hardware queue. The last update was about an hour ago; you do not need to do anything right now.`,
  partial: false,
  flagged_count: 0,
});

// Help articles staff published to the portal.
const HELP_ARTICLES: PortalHelpArticle[] = [
  {
    id: "help-wifi",
    title: "Connect to the office Wi-Fi",
    body: "1. Choose the **Staff** network.\n2. Sign in with your usual email and password.\n\nIf it still will not connect, restart your laptop and try once more.",
  },
  {
    id: "help-vpn",
    title: "Work from home with the VPN",
    body: "Open **Toeshee VPN** from the Start menu and select *Connect*. You will be asked to approve the sign-in on your phone.",
  },
  {
    id: "help-printer",
    title: "Clear a printer jam",
    body: "Open tray 2, remove any creased paper, and close the tray firmly. The printer resumes by itself.",
  },
];

const suggestPortalHelp: FixtureMap["suggestPortalHelp"] = (q) => {
  const words = q.toLowerCase().match(/[a-z0-9]{3,}/g) ?? [];
  return HELP_ARTICLES.filter((article) =>
    words.some((word) => `${article.title} ${article.body}`.toLowerCase().includes(word)),
  ).map(({ id, title }) => ({ id, title }));
};

const getPortalHelpArticle: FixtureMap["getPortalHelpArticle"] = (id) => {
  const found = HELP_ARTICLES.find((article) => article.id === id);
  if (!found) throw new ApiError(404, JSON.stringify({ detail: "article not found" }));
  return found;
};

const fixtures: FixtureMap = {
  suggestPortalHelp,
  getPortalHelpArticle,
  listPortalTickets,
  getPortalTicket,
  listPortalCategories,
  createPortalTicket,
  createPortalComment,
  uploadPortalAttachment,
  downloadPortalAttachment,
  createResolverSession,
  createResolverTurn,
};

export default fixtures;
