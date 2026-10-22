// Mirrors backend/app/modules/email_intake/schema.py request/response models
// exactly (snake_case keys, matching the JSON), read against `schema.py` and
// `api.py` directly rather than against spec prose.
//
// Per security invariant #3, `MailboxOut` carries no field capable of holding
// a credential -- not the password, not `credential_id`. `has_credential` is
// the one boolean surfaced instead, exactly as `notification`'s `ChannelOut`
// does. NOTHING IN THIS FILE IS A WRITE-BACK OF A STORED SECRET: `password`
// exists only on the two REQUEST types, is always something the operator just
// typed, and is never populated from a response.

// `email_mailbox`/`inbound_message`'s disposition vocabulary. The check
// constraint `ck_inbound_message_disposition_valid` is the authority; this
// list is what the UI offers as a FILTER, which is untrusted input the server
// re-validates against `schema.Disposition`.
export type Disposition =
  | "created"
  | "commented"
  | "reopened"
  | "quarantined"
  | "refused_loop"
  | "refused_rate"
  | "refused_parse";

export const DISPOSITIONS: readonly Disposition[] = [
  "created",
  "commented",
  "reopened",
  "quarantined",
  "refused_loop",
  "refused_rate",
  "refused_parse",
];

// --- Mailboxes (schema.py's MailboxCreate/MailboxUpdate/MailboxOut) -------

/** How a mailbox signs in: a password, or a Microsoft 365 account. */
export type AuthKind = "password" | "microsoft_oauth";

export interface MailboxCreate {
  address: string;
  host: string;
  port: number;
  // Implicit TLS (993) versus STARTTLS on a cleartext port (143). NOT
  // "encrypted versus not": both paths verify the certificate and both fail
  // closed before the LOGIN line is written (spec §2.1).
  use_ssl?: boolean;
  folder?: string;
  username: string;
  auth_kind?: AuthKind;
  // Required for a password mailbox and refused for a Microsoft one, which
  // signs in through "Connect with Microsoft" instead.
  password?: string;
  default_category_id: string;
  reopen_window_days?: number;
  active?: boolean;
}

export interface MailboxUpdate {
  address?: string;
  host?: string;
  port?: number;
  use_ssl?: boolean;
  folder?: string;
  username?: string;
  // Switching to Microsoft drops the saved password and switches the mailbox
  // off until it is connected; switching back needs `password`.
  auth_kind?: AuthKind;
  // ABSENT means "leave the stored password alone" (schema.py's MailboxUpdate).
  // It does NOT mean "clear it" -- there is no way to clear it, because
  // `credential_id` is NOT NULL. So the form omits the key entirely when the
  // operator did not type a new one, rather than sending "" or null.
  password?: string;
  default_category_id?: string;
  reopen_window_days?: number;
  active?: boolean;
}

export interface MailboxOut {
  id: string;
  address: string;
  host: string;
  port: number;
  use_ssl: boolean;
  folder: string;
  username: string;
  has_credential: boolean;
  /** `password` or `microsoft_oauth`. */
  auth_kind: string;
  /**
   * `ok` or `needs_reconsent`. The latter is a state with an action
   * (Reconnect), not an error, and the list renders it as one.
   */
  credential_state: string;
  default_category_id: string;
  reopen_window_days: number;
  active: boolean;
  // The ONLY signal that a mailbox has stopped working. A poison message
  // stops its own mailbox by design (poller.py: the poll aborts, the stage
  // is recorded, the sweep moves on), so if these two are not rendered an
  // operator has no way at all to learn about it. `last_error` is already a
  // bounded, sanitized `"<stage> failed: <reason>"` from fixed vocabularies
  // -- never a raw server response and never the password.
  last_polled_at: string | null;
  last_error: string | null;
  created_at: string;
}

// --- Microsoft 365 sign-in (mail-oauth design §4.4) -----------------------

export interface OAuthAppOut {
  tenant_id: string;
  client_id: string;
  has_secret: boolean;
  /**
   * The exact return address to register on the Azure app. `null` when the
   * server's console address is not configured, in which case Connect with
   * Microsoft is unavailable.
   */
  redirect_uri: string | null;
}

export interface OAuthAppUpdate {
  tenant_id: string;
  client_id: string;
  /** Only when the operator typed one; absent means "keep the stored one". */
  client_secret?: string;
}

export interface OAuthStartOut {
  authorize_url: string;
}

// The `/mailboxes/{id}/test` result. `detail` is a constructed classification
// from `imap.py`'s fixed vocabularies, never `str(exc)` and never a server
// response line (invariant #3).
export interface MailboxTestResult {
  ok: boolean;
  detail: string;
}

// --- The message log (schema.py's InboundMessageOut/MessageLogOut) --------

export interface InboundMessageOut {
  id: string;
  mailbox_id: string;
  message_id: string;
  from_address: string;
  subject: string;
  // THE SENDER'S OWN `Date:` HEADER, and therefore attacker-chosen. Shown
  // because a wild value is itself diagnostic, but it decides nothing: the
  // list is filtered and ordered on `created_at` server-side.
  received_at: string;
  created_at: string;
  // `string`, not `Disposition`: the check constraint is the vocabulary's
  // authority and the backend deliberately passes the column through
  // unvalidated so one off-vocabulary row cannot 500 the whole page.
  disposition: string;
  reason: string;
  ticket_id: string | null;
  // The PARSED plain text, never the raw RFC822 message, and on the list an
  // EXCERPT (`messages.BODY_EXCERPT_CHARS`, 4 KiB) -- `body_truncated` says
  // when something was cut, so the excerpt never masquerades as the whole
  // message.
  body_text: string | null;
  body_truncated: boolean;
}

export interface MessageLogOut {
  messages: InboundMessageOut[];
  // The extent of the LIST -- scope AND `disposition` -- so a caller can page.
  total: number;
  // Counted over the whole SCOPE (`mailbox_id` + time range) and NOT narrowed
  // by `disposition`, deliberately: the two counts answer different questions
  // (messages.py). Rendering it as a tally of the returned page would make it
  // read "3" where the answer is 300.
  quarantined_count: number;
}

// GET /email-intake/messages' query filters (api.py's list_messages_route).
// `since`/`until` bound `created_at`, not the sender-supplied `received_at`.
export interface MessageLogFilters {
  mailbox_id?: string;
  disposition?: Disposition;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}
