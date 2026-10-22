/**
 * Fixtures for email intake: two mailboxes (one polling cleanly, one that a
 * poison message stopped) and a message log whose dispositions cover the
 * whole vocabulary, with the tickets they created pointing at the service
 * desk's own rows.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ApiError } from "../../lib/api";
import type {
  InboundMessageOut,
  MailboxOut,
  MailboxTestResult,
  OAuthAppOut,
} from "./types";

const MINUTE = 60_000;
const DAY = 24 * 60;

const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();
const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* --------------------------------------------------------------- mailboxes */

const mailboxes: MailboxOut[] = [
  {
    id: "mbx-support",
    address: "support@toeshee.example",
    host: "imap.toeshee.example",
    port: 993,
    use_ssl: true,
    folder: "INBOX",
    username: "support@toeshee.example",
    has_credential: true,
    auth_kind: "microsoft_oauth",
    credential_state: "ok",
    default_category_id: "cat-other",
    reopen_window_days: 14,
    active: true,
    last_polled_at: ago(2),
    last_error: null,
    created_at: ago(200 * DAY),
  },
  {
    id: "mbx-legacy",
    address: "helpdesk@legacy.toeshee.example",
    host: "mail.legacy.toeshee.example",
    port: 143,
    use_ssl: false,
    folder: "Helpdesk",
    username: "helpdesk",
    has_credential: true,
    auth_kind: "password",
    credential_state: "ok",
    default_category_id: "cat-software",
    reopen_window_days: 7,
    active: true,
    last_polled_at: ago(3 * DAY),
    last_error: "login failed: rejected",
    created_at: ago(400 * DAY),
  },
];

const listEmailMailboxes: FixtureMap["listEmailMailboxes"] = () => [...mailboxes];

const createEmailMailbox: FixtureMap["createEmailMailbox"] = (body) => {
  const next: MailboxOut = {
    id: `mbx-${mailboxes.length + 1}`,
    address: body.address,
    host: body.host,
    port: body.port,
    use_ssl: body.use_ssl ?? true,
    folder: body.folder ?? "INBOX",
    username: body.username,
    has_credential: body.auth_kind !== "microsoft_oauth",
    auth_kind: body.auth_kind ?? "password",
    credential_state: "ok",
    default_category_id: body.default_category_id,
    reopen_window_days: body.reopen_window_days ?? 14,
    // A Microsoft mailbox starts switched off, as the server does it.
    active: body.auth_kind === "microsoft_oauth" ? false : (body.active ?? true),
    last_polled_at: null,
    last_error: null,
    created_at: ago(0),
  };
  mailboxes.push(next);
  return next;
};

const updateEmailMailbox: FixtureMap["updateEmailMailbox"] = (id, body) => {
  const at = mailboxes.findIndex((m) => m.id === id);
  const current = mailboxes[at];
  if (at < 0 || !current) throw notFound("mailbox");
  const { password, ...rest } = body;
  const next: MailboxOut = { ...current, ...rest, has_credential: current.has_credential || password !== undefined };
  mailboxes[at] = next;
  return next;
};

const deleteEmailMailbox: FixtureMap["deleteEmailMailbox"] = (id) => {
  const at = mailboxes.findIndex((m) => m.id === id);
  if (at < 0) throw notFound("mailbox");
  mailboxes.splice(at, 1);
  return undefined;
};

const testEmailMailbox: FixtureMap["testEmailMailbox"] = (id): MailboxTestResult => {
  const m = mailboxes.find((box) => box.id === id);
  if (!m) throw notFound("mailbox");
  return m.last_error
    ? { ok: false, detail: m.last_error }
    : { ok: true, detail: `connected, logged in, ${m.folder} selected` };
};

/* ---------------------------------------------------------------- messages */

interface MessageSeed {
  id: string;
  mailbox: string;
  from: string;
  subject: string;
  /** Minutes ago. */
  at: number;
  disposition: string;
  reason: string;
  ticket: string | null;
  body: string | null;
  truncated?: boolean;
}

const SEEDS: MessageSeed[] = [
  { id: "msg-1", mailbox: "mbx-support", from: "adaeze.okonkwo@toeshee.example", subject: "VPN drops every 20 minutes", at: 2 * 60, disposition: "created", reason: "", ticket: "tkt-1", body: "Every ~20 minutes the VPN drops for about 30 seconds. Started after the update on Friday." },
  { id: "msg-2", mailbox: "mbx-support", from: "adaeze.okonkwo@toeshee.example", subject: "Re: [INC-4127] VPN drops every 20 minutes", at: 110, disposition: "commented", reason: "", ticket: "tkt-1", body: "It is worse on video calls: the call freezes, then reconnects about half a minute later." },
  { id: "msg-3", mailbox: "mbx-support", from: "leila.farah@toeshee.example", subject: "Wi-Fi drops in meeting room B", at: 5 * 60, disposition: "created", reason: "", ticket: "tkt-6", body: "Calls from meeting room B drop every few minutes. Room A on the same floor is fine." },
  { id: "msg-4", mailbox: "mbx-support", from: "adaeze.okonkwo@toeshee.example", subject: "Outlook keeps asking for the password", at: 3 * DAY, disposition: "created", reason: "", ticket: "tkt-5", body: "Three or four times a day Outlook pops the credential prompt.\n\nEntering the password works until the next time. I have attached the full log from the last three days, which is long, so I apologise in advance for the size of this message.", truncated: true },
  { id: "msg-5", mailbox: "mbx-support", from: "tomas.kalu@toeshee.example", subject: "Re: [INC-4102] Cannot reach the intranet from home", at: 30 * DAY, disposition: "reopened", reason: "reply inside the reopen window", ticket: "tkt-9", body: "It is happening again since this morning." },
  { id: "msg-6", mailbox: "mbx-support", from: "stranger@elsewhere.example", subject: "Invoice 88213 attached", at: 55, disposition: "quarantined", reason: "unknown sender: stranger@elsewhere.example", ticket: null, body: "Please find attached the invoice for last month. Open the attachment to view it." },
  { id: "msg-7", mailbox: "mbx-support", from: "noreply@vendor.example", subject: "Re: [INC-4127] VPN drops every 20 minutes", at: 40, disposition: "quarantined", reason: "reply token did not verify", ticket: null, body: "This is an automated message from the vendor portal." },
  { id: "msg-8", mailbox: "mbx-support", from: "support@toeshee.example", subject: "Automatic reply: [INC-4133] Second screen flickers after waking", at: 3 * 60 - 2, disposition: "refused_loop", reason: "auto-submitted reply to our own notification", ticket: null, body: null },
  { id: "msg-9", mailbox: "mbx-support", from: "printer-3f@toeshee.example", subject: "Scan_20260907_0931.pdf", at: 9 * 60, disposition: "refused_parse", reason: "no text part", ticket: null, body: null },
  { id: "msg-10", mailbox: "mbx-support", from: "leila.farah@toeshee.example", subject: "Re: Wi-Fi drops in meeting room B", at: 4 * 60, disposition: "refused_rate", reason: "more than 20 messages from this sender in an hour", ticket: null, body: "Sending again in case the first one did not arrive." },
  { id: "msg-11", mailbox: "mbx-legacy", from: "rui.santos@toeshee.example", subject: "Keyboard keys sticking", at: 3 * DAY + 20, disposition: "created", reason: "", ticket: "tkt-7", body: "Several keys on my keyboard stick after the coffee incident." },
];

const message = (seed: MessageSeed): InboundMessageOut => ({
  id: seed.id,
  mailbox_id: seed.mailbox,
  message_id: `<${seed.id}@${seed.from.split("@")[1] ?? "example"}>`,
  from_address: seed.from,
  subject: seed.subject,
  received_at: ago(seed.at + 1),
  created_at: ago(seed.at),
  disposition: seed.disposition,
  reason: seed.reason,
  ticket_id: seed.ticket,
  body_text: seed.body,
  body_truncated: seed.truncated ?? false,
});

const listEmailIntakeMessages: FixtureMap["listEmailIntakeMessages"] = (filters = {}) => {
  const inScope = SEEDS.filter((m) => !filters.mailbox_id || m.mailbox === filters.mailbox_id).sort((a, b) => a.at - b.at);
  const matched = inScope.filter((m) => !filters.disposition || m.disposition === filters.disposition);
  const offset = filters.offset ?? 0;
  const limit = filters.limit ?? 50;
  return {
    messages: matched.slice(offset, offset + limit).map(message),
    total: matched.length,
    // Counted over the whole SCOPE, not narrowed by `disposition` (messages.py).
    quarantined_count: inScope.filter((m) => m.disposition === "quarantined").length,
  };
};

/* ------------------------------------------------ Microsoft 365 sign-in */

let oauthApp: OAuthAppOut = {
  tenant_id: "toeshee.onmicrosoft.com",
  client_id: "00000000-0000-4000-8000-000000000001",
  has_secret: true,
  redirect_uri: "https://itms.toeshee.example/settings/email-intake/oauth/callback",
};

const getEmailOAuthApp: FixtureMap["getEmailOAuthApp"] = () => oauthApp;

const saveEmailOAuthApp: FixtureMap["saveEmailOAuthApp"] = (body) => {
  oauthApp = {
    ...oauthApp,
    tenant_id: body.tenant_id,
    client_id: body.client_id,
    has_secret: oauthApp.has_secret || body.client_secret !== undefined,
  };
  return oauthApp;
};

// Fixture mode never leaves for Microsoft: the "authorize" address is the
// console's own return page, which then completes against the fixture below.
const startMailboxOAuth: FixtureMap["startMailboxOAuth"] = (id) => ({
  authorize_url: `/settings/email-intake/oauth/callback?code=fixture-code&state=${id}`,
});

const completeMailboxOAuth: FixtureMap["completeMailboxOAuth"] = (body) => {
  const at = mailboxes.findIndex((m) => m.id === body.state);
  const current = mailboxes[at];
  if (at < 0 || !current) throw notFound("sign-in");
  const next: MailboxOut = { ...current, has_credential: true, credential_state: "ok", last_error: null };
  mailboxes[at] = next;
  return next;
};

const abandonMailboxOAuth: FixtureMap["abandonMailboxOAuth"] = () => undefined;

const disconnectMailboxOAuth: FixtureMap["disconnectMailboxOAuth"] = (id) => {
  const at = mailboxes.findIndex((m) => m.id === id);
  const current = mailboxes[at];
  if (at < 0 || !current) throw notFound("mailbox");
  const next: MailboxOut = { ...current, has_credential: false, active: false, credential_state: "ok" };
  mailboxes[at] = next;
  return next;
};

const fixtures: FixtureMap = {
  listEmailMailboxes,
  createEmailMailbox,
  updateEmailMailbox,
  deleteEmailMailbox,
  testEmailMailbox,
  listEmailIntakeMessages,
  getEmailOAuthApp,
  saveEmailOAuthApp,
  startMailboxOAuth,
  completeMailboxOAuth,
  abandonMailboxOAuth,
  disconnectMailboxOAuth,
};

export default fixtures;
