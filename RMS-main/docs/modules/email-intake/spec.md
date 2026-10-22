# Module Spec — Email intake

> Fourth of five sub-projects that together replace Odoo Helpdesk as this
> platform's ticketing system (`docs/modules/service-desk/spec.md` §14). Scoped
> from scratch during brainstorming — no scoping brief preceded this spec, so
> §0 carries more weight than usual.
>
> Control-plane only — no agent capability, no new job type, no signed-job path.
>
> **This module adds no ticket rules.** Like the portal, it is a surface: it
> turns an inbound message into a call to an existing ticket-core service. A
> behaviour change belongs in `app/modules/service_desk/`, where its tests live.

---

## 0. Resolved design decisions

Three were settled in brainstorming. Each rules out a whole family of designs,
so they are recorded before the design rather than inside it.

### 0.1 Mail is FETCHED over IMAP, never received

A Celery task logs into a mailbox the customer already owns
(`helpdesk@theircompany.com`), fetches unread messages, and marks them
processed. The platform opens **no inbound port** and requires **no MX change**.

This is the same posture as invariant #2, which requires agents to connect
outbound only and forbids the server opening a connection to an endpoint. A
design that made the control plane an internet-facing mail receiver would
contradict the security model the whole product is built on, and would make
on-prem customers re-route company mail to reach a helpdesk feature.

Two alternatives were rejected:

- **An SMTP listener the platform runs.** Real-time and needs no stored mailbox
  credentials, but requires inbound port 25, MX records pointed at the control
  plane, TLS termination for mail, and makes a domain-admin-equivalent system a
  standing spam and abuse target.
- **A mail provider's inbound-parse webhook.** Real-time and offloads spam
  filtering, but is a hard cloud dependency in an on-prem-first product, needs
  an internet-reachable inbound HTTPS endpoint, and routes customer mail through
  a third party — usually unacceptable to the IT-governance buyers this targets.

The cost accepted: **polling latency**. A ticket appears within one poll
interval, not instantly.

### 0.2 An inbound message is UNAUTHENTICATED; two independent checks gate it

This is the module's central security problem. `From:` is trivially forged, and
an inbound message becomes a **public comment on somebody's ticket**.

Two checks, and **both** must pass:

1. **`From:` must match a `users` row in the sending org.** An unrecognised
   sender never creates or comments on anything. It is **quarantined**, not
   dropped — an admin can see what arrived and why it was refused.
2. **A reply must carry an unguessable per-ticket token** in the recipient
   address (plus-addressing: `helpdesk+t-<token>@…`), generated with
   `secrets.token_urlsafe` and stored on the ticket.

Neither is sufficient alone, and the reason each is insufficient matters:

- **The token is not a bearer credential.** It appears in the headers of every
  message in that thread, is forwarded wherever the requester forwards the
  thread, and outlives their employment. Treating possession of it as authority
  would let anyone who ever saw the thread post as the requester.
- **`From:` alone is a forgeable string.** Threading on `In-Reply-To` /
  `References` was rejected for this reason: those headers are equally
  forgeable, and every notification this platform sends publishes a
  `Message-ID` that would become a ticket-injection key.

### 0.3 A reply reopens within a window; beyond it, a new ticket that cites the old

`lifecycle.py` already permits `resolved -> open` and `closed -> open` for an
incident, and `closed -> in_progress` for a request — the ticket core built the
reopen path deliberately (its §3.1/§3.3). **This module therefore needs no
lifecycle change**, and must not introduce one.

A reply within the reopen window (default **14 days**, per-org configurable)
reopens the ticket, matching the requester's reasonable expectation that
replying means "not finished". Beyond the window, a **new ticket** is filed
whose description cites the old ticket's number in text.

**Amendment A5 (Task 10) — the window is measured from a named anchor.** This
section originally said "beyond the window" without saying *from when*, and
which timestamp it is materially changes behaviour. The anchor implemented in
`intake._reopen_anchor` is, in order:

1. `ticket.closed_at` when the ticket's status is `closed`;
2. otherwise `ticket.resolved_at`;
3. otherwise `ticket.updated_at`, as a fallback only.

`transitions.change_status` stamps `resolved_at` on entering
`resolved`/`fulfilled` and `closed_at` on entering `closed`, so the anchor is
exactly "when the ticket entered the state this reply is arriving into" — the
thing the requester's "you closed this two days ago" intuition is about.

**`updated_at` alone was considered and rejected.** It moves on *any* write, so
a reassignment or a category correction last week would silently extend the
window on a ticket nobody had substantively touched for a year — reviving
precisely the stale-assignee, stale-context ticket this section exists to
prevent. It survives only as the third fallback, for a ticket whose status was
set without going through `change_status`, where the alternative is a crash
mid-message.

Text, not a link row: parent/child ticket linking is explicitly **not
sequenced** (`service-desk` §14), and inventing a link model here would be this
module defining ticket structure, which §1 forbids.

Without the window, a stray auto-reply to a years-old thread resurrects a ticket
whose assignee, category and context are all stale.

---

## 1. Purpose & boundary

Let a person raise and continue a ticket from their mail client, without the
mailbox becoming an unauthenticated injection channel into other people's
tickets.

**In scope:** mailbox configuration and its vault-held credentials (§2, §3),
the polling receiver and its idempotency (§4), sender resolution and the reply
token (§5), loop and bounce refusal (§6), MIME parsing and attachment handling
(§7), what an inbound message produces (§8), RBAC/tenancy/audit (§9), the API
and admin surface (§10), tests (§11).

**Not in this sub-project:** the SLA engine (sub-project 5). Deferred (§13):
per-sender allow/deny lists, spam scoring, DKIM/SPF verification, token
rotation, **staff** replying by email, and any inbound transport other than
IMAP.

**Amendment A3a (Task 10) — the deferral list above was wrong, and is
corrected.** It previously deferred "outbound reply-to-this-address threading",
which this module *ships*: §5.2's `Reply-To` is emitted on outbound ticket mail
by `notification/delivery.py::_send_email`, and without it no requester is ever
given an address to reply to and the entire inbound reply path is unreachable.
What §13 actually defers is the **staff** half — a technician replying from
their own mail client — which needs its own trust rules and a
public-versus-internal visibility decision. The two were conflated in one
phrase.

**Boundary vs the ticket core.** This module calls `tickets.create_ticket`,
`threads.add_comment` and `threads.add_attachment`. It adds no status, no
transition, and no second definition of who may do what. Inbound comments are
always `visibility="public"` — an internal note is not expressible from email,
by construction.

**Boundary vs notification core.** Notification sends mail *out*; this module
reads mail *in*. Their **configuration and their tables are separate**:
notification's per-org SMTP channel and this module's per-org mailbox are
distinct rows with distinct vault credentials, because the send and receive
accounts are frequently different. Neither module owns the other's schema.

**Amendment A3b (Task 10) — "they share no code" is now FALSE, and the claim
is withdrawn.** As originally written this sentence said the two modules share
no code and no table; the table half stands, the code half does not.
Publishing the reply address is a change to
`app/modules/notification/delivery.py`: `_send_email` imports
`email_intake.identity.reply_to_for_ticket` and `_send_smtp_sync` grew an
optional `reply_to` parameter that becomes a `Reply-To` header. That is a real,
one-directional dependency from the outbound module onto this one, and it is
deliberate — see §5.2 for why there is nowhere else it could live, and why the
alternative (this module reaching into the send path from outside) would be
worse. Nothing flows the other way: `email_intake` imports
`notification.models` only to read email channels' `from_address` for §6's
own-sender loop check.

**Boundary vs the portal.** Complementary surfaces onto the same tickets. The
portal's resolution email currently says "sign in to the portal and add a
comment"; once this module ships, that copy may be revised to offer replying —
but only when this module is actually deployed for that org, which is a
per-org configuration question, not a code constant.

---

## 2. Data model

Migration `0032` (current head is `0031`). Both tables are org-scoped and get
RLS `ENABLE + FORCE` with a `tenant_isolation` policy — invariant #5, enforced
at the database layer, with a behavioural per-table isolation test (§11).

### 2.1 `email_mailbox` — per-org inbound configuration

`id (uuid7 PK), org_id FK, address (str 320), host (str 255), port (int),
use_ssl (bool, default true), folder (str 100, default 'INBOX'),
username (str 320), credential_id (uuid FK credentials, NOT NULL),
default_category_id (uuid FK ticket_category, NOT NULL),
reopen_window_days (int, default 14), active (bool, default true),
last_polled_at (tz-aware, NULL), last_error (str 500, NULL),
claimed_at (tz-aware, NULL), created_by, created_at, updated_at`.

**Amendment A4 (Task 10) — `claimed_at` was missing from this list and is
added.** §4 requires the poller to claim a mailbox with `SELECT … FOR UPDATE
SKIP LOCKED`, and a claim that is not recorded on the row cannot exist: without
this column there is nothing for the claim to set, nothing for the release to
clear, and nothing for the stuck-claim sweep (`STUCK_POLL_CLAIM_AFTER`, ten
minutes, matching `worker.py`) to look at. It is NULL when unclaimed, stamped
inside the claim transaction, and cleared in a `finally` on the failure path as
well as the success path — a mailbox whose poll throws must not stay claimed
for ten minutes.

**Amendment A4b (Task 10) — what `use_ssl=false` now does, stated.** The bullet
below was written before the transport existed. As built, `use_ssl=false` does
not mean "plain IMAP": `imap.py` issues `STARTTLS` on the cleartext port and
**fails closed with `tls failed: starttls-unsupported`, with the password
unsent, if the server does not advertise it**. There is no code path that puts
the mailbox credential on an unencrypted or unverified channel, and
`tests/test_email_intake_imap.py` asserts that on the wire by inspecting what a
passive listener would have seen.

**Amendment A5 (2026-09-23, migration 0110) — `credential_id` is nullable, and
the property it protected is kept.** Microsoft 365 sign-in adds a state the old
schema could not express: a mailbox that exists and has not been connected yet.
`credential_id` becomes nullable and `CHECK (credential_id IS NOT NULL OR active
IS FALSE)` takes over, so a mailbox with no credential can never be polled.
Two columns are added: `auth_kind` (`password` | `microsoft_oauth`, default
`password`) and `credential_state` (`ok` | `needs_reconsent`, default `ok`;
the poller claims only `ok`). Every existing row takes the defaults and behaves
as before. See `docs/superpowers/specs/2026-09-11-mail-oauth-design.md` §4.7.

- `default_category_id` is **NOT NULL and load-bearing**: `ticket.category_id`
  is NOT NULL and an email carries no category, so without a configured default
  no inbound message could ever become a ticket. The queue follows from the
  category's `default_queue_id`, exactly as it does for a portal-filed ticket.
- `credential_id` is NOT NULL: there is no unauthenticated IMAP. *(Amended by
  A5: nullable, but never NULL on an active mailbox.)*
- `use_ssl` selects **implicit TLS (993) versus STARTTLS on a cleartext port
  (143)**. It does *not* select "encrypted" versus "not": both paths verify the
  server certificate with `ssl.create_default_context()` (`check_hostname`,
  `CERT_REQUIRED`) and **both fail closed before the LOGIN line is written** if
  verification fails or the server does not offer STARTTLS. There is no code
  path that sends the mailbox password over an unauthenticated channel —
  invariant #3 is about the credential's exposure, not only about where it gets
  written down. An operator with a genuinely cleartext-only relay gets
  `tls failed: starttls-unsupported` on the mailbox row.
- Unique on `(org_id, address)`.
- `last_error` is a **bounded, sanitized** excerpt — never a raw server
  response, and never the password. It is `"<stage> failed: <reason>"`, both
  halves drawn from fixed vocabularies the transport constructs
  (`connect|tls|login|select|fetch|process|flag|credential` ×
  `dns|timeout|connection-refused|certificate-invalid|starttls-unsupported|
  rejected|…`), never `str(exc)`. §10's admin surface has to distinguish a bad
  password from a missing folder from a refused certificate.

### 2.2 `inbound_message` — the idempotency and disposition record

`id (uuid7 PK), org_id FK, mailbox_id FK, message_id (str 998),
from_address (str 320), subject (str 512), received_at (tz-aware),
disposition (str 24), reason (str 200), ticket_id (uuid FK ticket, NULL),
sender_user_id (uuid FK users, NULL), body_text (text, NULL),
created_at`.

- **`UNIQUE (org_id, message_id)` is the idempotency key** and the reason this
  table exists (§4).
- `disposition` is one of `created`, `commented`, `reopened`, `quarantined`,
  `refused_loop`, `refused_rate`, `refused_parse`. **Every message that is
  fetched gets a row.** Nothing is ever silently discarded — a helpdesk mailbox
  that quietly eats mail is indistinguishable from one nobody is emailing.
- `message_id` is `str(998)` — RFC 5322's line-length limit. A message with no
  `Message-ID` gets a synthesised one derived from a hash of its envelope and
  content, so it still dedupes.
- `body_text` holds the **parsed, plain-text** body, not the raw RFC822
  message. Retaining full raw messages indefinitely is a privacy liability
  (complete headers, routing, every recipient) that buys forensics this product
  has not asked for.

### 2.3 One column on `ticket`

`ticket.email_token (str 43, NULL, UNIQUE)` — the per-ticket reply token
(§5.2), `secrets.token_urlsafe(32)`. NULL for tickets that have never been
emailed about. This is the module's only change to an existing table; it adds
no column to any other.

---

## 3. Credentials

The mailbox password lives in the vault, is checked out at point of use inside
the worker, held for one poll, and never written to `email_mailbox`, a log
line, `last_error`, an audit metadata field, or an API response (invariant #3).
The existing `VaultService.store`/`checkout` surface is used unchanged.

Every checkout writes an audit event (invariant #4). A poller checking out a
password on every interval is a high-volume audit path, and it is intentional
rather than something to be discovered later as noise.

**Amendment A6 (Task 10) — this section's stated REASON was false and is
corrected.** It previously said the audit event was "the vault's existing
behaviour". It is not. `app/core/vault.py` contains no reference to audit at
all; `VaultService.checkout` decrypts and returns, and nothing else.
`notification/delivery.py` and `remote/service.py` both carry explicit comments
saying the *caller* must write the event, and this module does the same:
`poller._checkout_credential` records `email_intake.credential_used` — actor
`system` for a scheduled poll, the named operator for `POST
…/mailboxes/{id}/test` — in the **same transaction** as the checkout, so the
event cannot be absent for a checkout that happened. The metadata carries the
`mailbox_id` and never the secret.

Recording the correction matters beyond tidiness: a future module that read the
original sentence and relied on the vault to audit for it would silently ship a
credential-access path with no entry in the hash-chained log, which is an
invariant-#4 violation that no test would catch.

---

## 4. The receiver, and why idempotency is the hard part

`poll_mailbox(org_id, mailbox_id)` connects over IMAP-SSL, selects the
configured folder, searches `UNSEEN`, and processes each message.

**IMAP has no transactions, and this is the central engineering problem.** The
fetch, the ticket write, and the `\Seen` flag are three separate operations
across two systems. A crash between the ticket write and the flag re-delivers a
message that was already processed; naive handling creates the ticket twice.

Three rules, together, make redelivery safe:

1. **The `inbound_message` row is written FIRST**, inside the same transaction
   as the ticket or comment it produces. `UNIQUE (org_id, message_id)` means a
   redelivered message collides instead of duplicating.
2. **`\Seen` is set only after the database commit.** A crash before the flag
   leaves the message unread and the row present, so the next poll sees the
   collision and flags it. A crash after the flag but before the commit is
   impossible in that order.
3. **The unique-violation path is a normal outcome, not an error.** It marks
   `\Seen` and moves on. Treating it as a failure is how a single stuck message
   blocks a mailbox forever.

Polling is org-scoped and claims per mailbox, following `app/worker.py`'s
established `SELECT … FOR UPDATE SKIP LOCKED` pattern so two workers never poll
one mailbox concurrently. A stuck claim is returned by the same sweep that
covers job dispatch and notification delivery.

The IMAP connection is bounded by an explicit timeout, and a mailbox whose poll
fails records `last_error` and is retried on the next interval — a mailbox that
is down must not stall the others.

**One poll is bounded** (`MAX_MESSAGES_PER_POLL`, 200). Unbounded, a
first-enrolment backlog against an existing inbox — one `UID FETCH` round trip
plus one transaction per message — routinely outlives the ten-minute stuck-claim
threshold, at which point the next sweep releases the still-running poll's own
claim and a second worker starts the same mailbox. Rule 1 keeps that *correct*
(the overlap resolves as a collision) but it is unbounded wasted work and a
mailbox that may never converge. The remainder is still `UNSEEN`, so the next
interval simply continues: there is no cursor to keep.

**A refused `\Seen` STORE is per-message, not per-mailbox.** Rule 1 is what
makes that safe — the row is already committed, so the redelivery it causes is
rule 3's normal outcome. Aborting the batch instead wedges the mailbox
permanently: every later poll re-fetches the same first message, fails the same
STORE, and never reaches the second, which is precisely the failure rule 3
exists to prevent, arriving through a door rule 3 does not cover.

---

## 5. Sender resolution and the reply token

### 5.1 Resolving the sender

The `From:` address is parsed with `email.utils.parseaddr`, lower-cased, and
matched against `users.email` within the message's org. Exactly one match
yields the requester; no match yields disposition `quarantined` with the reason
recorded.

**An AMBIGUOUS match is also quarantined, and this case is real.** `users` is
unique on `(org_id, external_subject)` — *not* on email — so two users in one
org can legitimately share an address (a shared role mailbox, a re-created
account, a contractor with two identities). Picking one deterministically would
attribute a person's ticket to a colleague, and the resulting ticket would look
entirely normal. Verified against the live schema: the only indexes on `users`
are `users_pkey` and `users_org_id_external_subject_key`.

**Migration `0032` adds an index on `users (org_id, lower(email))`.** Sender
resolution runs on *every* inbound message, and without it each one is a
sequential scan of the org's users on the poller's hot path. This is an
additive, behaviour-neutral index on a kernel table — the module's only kernel
touch, and it adds no column and no constraint.

Quarantine is deliberate. Silently dropping mail from an unrecognised address
makes "the helpdesk ignored my email" indistinguishable from "the helpdesk
never received it", and an admin has no way to find out which.

### 5.2 The reply token

`ticket.email_token` is `secrets.token_urlsafe(32)` — 256 bits, generated when
a ticket first becomes the subject of outbound mail. Outbound notifications for
that ticket set `Reply-To: <local>+t-<token>@<domain>`.

**Amendment A3c (Task 10) — where the header is published, stated plainly.**
Emitting that `Reply-To` **is a change to
`app/modules/notification/delivery.py`**, and there is nowhere else it could
be. `_send_smtp_sync` takes an optional `reply_to` and adds the header only
when it is present; `_send_email` resolves it by calling
`email_intake.identity.reply_to_for_ticket(session, org_id, event.subject_id,
delivery.recipient)`. So §1's original "they share no code" was false as soon
as this shipped — see the amendment there. The conditions are:

- The condition is `event.subject_type == "ticket"`, not a list of event types:
  a future ticket event added to the registry is repliable for free, and a
  non-ticket event never is, because a reply to it would resolve to no ticket.
- **Only the ticket's requester is offered a reply address.** The same event
  also goes to the assignee and to queue members, and §13 defers staff replying
  by email — `intake.py` quarantines a reply whose sender is not the requester,
  silently. Handing a technician a repliable address would give them no
  comment, no bounce and no notice that their reply went nowhere.
- **No `Reply-To` is emitted when the org has no ACTIVE `email_mailbox`**, and
  no token is minted in that case either. Email intake is per-org
  configuration; an address nobody polls is worse than none, because the
  requester's reply then vanishes silently instead of bouncing. The same
  refusal covers a mailbox address that is not `local@domain`, one whose local
  part already contains a `+` (a second tag cannot be nested inside it), and
  one containing a character that cannot appear in a header at all.
- The token is minted **lazily and in its own committed transaction**, before
  the SMTP conversation: a token published on the wire but rolled back
  afterwards would leave an address in the requester's inbox resolving to no
  ticket, forever. It never changes once minted.

This resolution is also the ONLY thing in production that mints
`ticket.email_token`. Before it existed, every ticket carried NULL, no
requester was ever handed an address, and the whole inbound reply path was
unreachable while its unit tests passed — because each of them constructed its
own token.

An inbound message is a **reply** if a recipient address (`To`, `Cc`, or
`Delivered-To`) carries a `+t-<token>` matching a ticket in that org.

**The token identifies the ticket. It does not authorise the sender.** After
resolving the ticket by token, the sender must ALSO be that ticket's requester.
Both checks, every time — see §0.2 for why the token cannot be trusted alone.

A message with no recognised token, from a known sender, is a **new ticket**.

---

## 6. Loop and bounce refusal

The failure this section exists to prevent: this platform notifies a requester,
their out-of-office auto-replies, the reply becomes a comment, the comment
notifies them again, and the two systems generate mail until someone notices.
Mail loops are fast and expensive.

A message is refused (`refused_loop`, recorded) if **any** of:

- `Auto-Submitted` is present and not `no` (RFC 3834)
- `Precedence` is `bulk`, `list`, `junk`, or `auto_reply`
- `X-Auto-Response-Suppress` is present
- `Return-Path` is null (`<>`) — the standard bounce envelope
- `Content-Type` is `multipart/report` with `report-type=delivery-status`, or
  the message is otherwise a DSN
- the `From:` address is the org's own notification sender or this mailbox's own
  address

Additionally, a per-`(org, from_address)` rate limit and a per-ticket inbound
comment rate limit refuse with `refused_rate`. A legitimate human does not send
forty replies a minute; a loop does.

**Amendment A8 (Task 10) — the chosen values and windows, recorded.** They were
left unspecified here and are decided in `intake.py`:

| Limit | Value | Window | Scope |
| --- | --- | --- | --- |
| `SENDER_RATE_LIMIT` | 10 messages | `SENDER_RATE_WINDOW` = 1 minute | per `(org_id, lower(from_address))` |
| `TICKET_RATE_LIMIT` | 20 messages | `TICKET_RATE_WINDOW` = 1 hour | per `(org_id, ticket_id)` |

**Two limits, because they catch two different shapes of loop.** The
per-sender limit catches a *fast* flood — an auto-replier round-tripping with
this platform's own notifications, many messages a minute. The per-ticket limit
catches a *slow* one — a forwarded thread looping between two systems a few
times an hour, never fast enough to trip the per-sender limit, but still able
to bury one ticket in noise indefinitely. Either alone leaves the other case
open.

**Counted from `inbound_message`, never from an in-process counter.** The
poller runs in Celery workers and several may run at once, so a per-process
count would both reset on every worker restart and disagree with every other
worker's. The table is the only thing every worker shares.

The per-sender check runs immediately after sender resolution succeeds and
before any ticket write, so a flooding sender is refused whether it would have
created a ticket or commented on one. Both limits are additionally
`org_id`-scoped in the query itself, not only by RLS (invariant #5).

**Amendment A10 (final review, F1) — the per-ticket count is narrowed by
`disposition`, not by `ticket_id` alone.** It counts only rows whose
disposition is `commented`, `reopened` or `refused_rate` — the ones that
represent the ticket *actually receiving inbound content*. `ticket_id` alone is
the wrong predicate, because a wrong-sender quarantine also records the
targeted ticket (deliberately, so an operator can query "did somebody try to
post into this ticket as someone else"), while contributing nothing to it.
Counting those rows made the limit a weapon: anyone who had been forwarded the
thread and therefore held the token could send quarantined mail at the limit
rate and thereby refuse the ticket's **own requester's** genuine replies —
silently, with no comment and no bounce, and self-reinforcingly, since each
refusal wrote another `ticket_id`-bearing row. The per-sender limit does not
help the victim: it is per `(org, from_address)`, and the attacker's address is
not hers. `disposition` is the sole authority on whether a message landed; the
count now reads it that way.

Every refusal is **recorded with its reason**, never silently dropped — the
disposition record is how an operator diagnoses "why did my email not arrive".

---

## 7. MIME parsing and attachments

Parsed with the stdlib `email` package using `email.policy.default`. No new
dependency.

- **Body:** prefer the first `text/plain` part. If the message is HTML-only,
  strip tags to text. **HTML is never stored as HTML and never rendered** —
  the portal and staff UI would otherwise be rendering attacker-supplied markup
  from an unauthenticated source.
- **Quoted history** is trimmed by a bounded heuristic (leading `>` blocks and
  common client separators). The heuristic is best-effort and must never drop
  the whole body: if trimming would leave it empty, the untrimmed body is kept.
- **Attachments** are extracted per part and stored through the ticket core's
  existing hardened path, with server-derived object keys and the filename
  sanitizer in `app/core/attachments.py`.
- **Size bounds are enforced HERE**, because email bypasses the HTTP body caps
  entirely: per-part and per-message limits, and a per-message attachment count
  cap. `MAX_ATTACHMENT_BYTES` (10 MiB) is the per-part reference. A message
  exceeding the bounds is `refused_parse` with the reason recorded — its text
  body is still preserved on the ticket, so the person is not left silent.
- A part with no decodable content, or a malformed MIME structure, yields
  `refused_parse` rather than an exception escaping the poller. One bad message
  must not stop a mailbox.

---

## 8. What an inbound message produces

| Case | Result |
| --- | --- |
| Known sender, no token | New ticket: `origin='email'`, category from the mailbox config, queue from the category default, requester the matched user |
| Known sender, valid token, ticket open, sender is requester | Public comment on that ticket |
| Known sender, valid token, ticket resolved/fulfilled/closed within the window | **Reopen, then** a public comment — `resolved -> open`, `closed -> open` (incident), `fulfilled -> in_progress`, `closed -> in_progress` (request) |
| Known sender, valid token, ticket resolved/closed beyond the window | New ticket citing the old ticket number in its description |
| Known sender, valid token, ticket `cancelled` or `rejected` | New ticket citing the old ticket number — no reopen edge exists, and the ticket accepts no comments |
| Known sender, valid token, sender is NOT the requester | Quarantined — the token is not authority (§0.2) |
| Unknown sender | Quarantined, reason recorded |
| Sender matches TWO users in the org | Quarantined — never guess which human sent it (§5.1) |
| Auto-reply, bounce, or own address | `refused_loop`, reason recorded |
| Malformed or oversized | `refused_parse`, reason recorded |
| Rate-limited sender or rate-limited ticket (§6) | `refused_rate`, reason recorded |
| Ticket creation refused by the ticket core (e.g. a `default_category_id` whose category has no default queue) | Quarantined, with the refusal as the reason — the mailbox is misconfigured, and §10's prominent quarantined count is what announces it |

**Amendment A1 (Task 10) — the ORDER in row 3 is corrected: reopen runs
BEFORE the comment.** As originally written this row listed the comment first
("Public comment **and** reopen"). That order raises on every reply to a closed
ticket: `closed` is in `lifecycle.TERMINAL_STATUSES` and `threads.add_comment`
rejects a terminal ticket outright, so the comment would fail before the reopen
that would have made it legal ever ran. `intake.process_message` therefore
calls `transitions.change_status` first and `add_comment` second. This is not a
new lifecycle rule — the ticket core already permits every edge used here
(§0.3); it is the order in which this module invokes them.

**Amendment A2 (Task 10) — `fulfilled -> in_progress` is added.** The
parenthetical in this row previously listed three edges and omitted the
request-type one, which meant a reply to a *fulfilled* request had no reopen
target and fell through to filing a new ticket — a silently worse outcome for
the commonest request-type reopen there is. The four edges live in ONE map
(`intake._REOPEN_TARGETS`), asserted against `lifecycle.can_transition` at
import time so the map cannot drift from the state machine.

**Amendment A7a (Task 10) — the `cancelled`/`rejected` row is added.** Neither
status was covered by any row. They are terminal (`FINAL_STATUSES`) *and* have
no reopen edge, and never will, so a reply to one can neither comment nor
reopen. Rather than raise, it takes the same route as a beyond-the-window
reply: a new ticket citing the old number in text. Leaving this unstated is how
the case reaches production as an exception escaping the poller — which stops
the whole mailbox (§11.10), not just that message.

**Amendment A7b (Task 10) — a refusal that still produced a ticket is recorded
as the REFUSAL.** §7 requires an oversized message's text body to still reach
the ticket, so a `refused_parse` message can legitimately end up with a
`ticket_id`. When that happens the row's `disposition` is `refused_parse`, not
`created`: the disposition column answers "why did my email not arrive intact",
and `ticket_id` on the same row already answers "where did it land". Recording
it as `created` would report a damaged message as a clean one, which is exactly
what §2.2's disposition record exists to prevent.

**Amendment A11 (final review, F3) — a message that fails to parse is recorded
as the parse failure even when its sender does not resolve.** `parse_message`
returns `refused_parse` for mail with no `From:`, an unshaped `From:`, or a
broken MIME structure — and such a message has, by construction, no resolvable
sender, so it used to fall through to the "unknown sender" row above, be filed
`quarantined`, and lose the parser's reason. The two rows of this table are not
in conflict: the malformed row wins, because the message did not arrive intact
and that is the more specific fact. This matters because §10 designates
`quarantined_count` as the alarm for *"mail a human expected to become a ticket
and which did not"*, and against a published helpdesk address the quarantine
count would otherwise be dominated by malformed spam, which is not that. The
unknown-sender row is unchanged for mail that parsed fine: a well-formed
message from a non-user IS mail someone expected to become a ticket, and it
stays `quarantined`.

`origin='email'` already exists in `ticket`'s check constraint — the ticket core
declared it in advance (its §2) so this module needs no constraint migration.

An email-created ticket has a **real human requester**, unlike an alert-origin
ticket. The `origin='alert'` carve-out that permits a null requester does not
apply here and must not be reused.

---

## 9. RBAC, tenancy, audit

**Permissions** (added to `app/core/rbac.py`):

- `email_intake.read` — view mailbox config, poll status, and the inbound
  message log.
- `email_intake.admin` — create/modify/delete a mailbox and its credential.

`email_intake.admin` is a **high-privilege grant**: it sets which mailbox the
platform authenticates to and which category inbound tickets land in. Admin
tier only, never technician. Both are seeded to `super-admin`/`org-admin`.

Every route carries `require_permission` server-side (invariant #6), with
RBAC completeness derived from the router as in the notification and portal
modules.

**Audited** (invariant #4): mailbox create/update/delete, credential checkout,
and quarantine review actions. The ticket and comment writes are audited by the
ticket-core services already, with the **matched sender** as the actor — an
inbound message from a resolved user is that person's action, not the system's.
Poll cycles themselves are **not** audited; they are operational and would
swamp the log.

**Tenancy** (invariant #5): both new tables carry `org_id` with RLS
`ENABLE + FORCE`, and a behavioural cross-org test per table that fails if its
predicate is removed.

---

## 10. API and admin surface

Under `/api/v1/email-intake`, versioned per convention, literal segments before
`{id}` parameters:

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/email-intake/mailboxes` | `email_intake.read` |
| POST | `/email-intake/mailboxes` | `email_intake.admin` |
| GET/PATCH/DELETE | `/email-intake/mailboxes/{id}` | read / admin / admin |
| POST | `/email-intake/mailboxes/{id}/test` | `email_intake.admin` |
| GET | `/email-intake/messages` | `email_intake.read` |

`/messages` filters on `disposition`, `mailbox_id`, and a time range. The
**quarantined count is surfaced prominently** in the admin UI: quarantined mail
is mail a human expected to become a ticket and which did not, and it is
invisible unless shown.

The test-send route validates credentials by connecting and listing the folder.
It is `email_intake.admin`-gated, rate-limited per org, audited, and **never
returns the password or a raw server error**.

**No response contains** the mailbox password, a credential id, or a raw
message. `body_text` is available to `email_intake.read` holders on the
messages endpoint — this is deliberate and stated, because that is how an
operator diagnoses a quarantined message.

**Amendment A9 (Task 10) — what `body_text` actually is on the list, and what
the time filter means.**

- **`body_text` in the LIST is a 4 KiB excerpt** (`messages.BODY_EXCERPT_CHARS`),
  with a `body_truncated: bool` beside it saying when something was cut. This
  is what bounds the response: the parser caps a stored body at 1 MiB, the
  sender chooses that size, and a quarantined row carries a body without ever
  having become a ticket — so at `limit=200` the un-excerpted list would be a
  ~200 MB response any reader could ask for. A full page of excerpts is under
  1 MB of body text. Serving the whole body needs a per-message detail route,
  which would be an amendment to the route table above, not something to smuggle
  in; it is the right follow-up if operators find 4 KiB too short.
- **`since`/`until` and the ordering are on `created_at`, never on
  `received_at`.** `received_at` is the sender's own `Date:` header, falling
  back to `now()` only when absent or unparseable — so it is chosen by anyone
  who can send mail to a monitored mailbox. Ordering or filtering on it hands a
  sender the decision about whether their message is visible: dated 2001 it
  drops out of the operator's "last 24 hours" view *and* out of that view's
  quarantined count, silencing the alarm for precisely the message that was
  buried; dated 2099 it owns row 1 of every page forever. `received_at` remains
  in the response — it is genuinely useful and a wild value is itself
  diagnostic — but it decides nothing. Ties break on `id`, a uuid7 and
  therefore monotonic in insertion order.
- **`quarantined_count` is scoped, not paged, and `disposition` does not narrow
  it.** `mailbox_id` and the time range are *scope* and apply to both counts;
  `disposition` is a *view* over that scope and applies only to `total`.
  Applying it to the quarantined count would make `?disposition=created` report
  `quarantined_count: 0` — true of the filtered rows, and read by a human as
  "nothing is quarantined" at exactly the moment they narrowed the view to look
  at something else. The admin UI labels the number **"in this scope"** for
  that reason.

---

## 11. Testing requirements

Test-first per `CLAUDE.md`. Named because each corresponds to a specific way
this module fails silently:

1. **Redelivery is idempotent** — processing the same `Message-ID` twice
   produces one ticket and one `inbound_message` row. Must fail if the unique
   constraint is dropped.
2. **A crash before `\Seen` does not duplicate** — simulate by processing a
   message whose row exists but which is still `UNSEEN`.
3. **Unknown sender is quarantined, not dropped** — a row exists with
   disposition `quarantined` and no ticket is created.
4. **A valid token from the WRONG sender is refused** — the token alone must
   not authorise. This is §0.2's central claim and must be asserted on the
   outcome, not on the parsing.
5. **Auto-reply detection**, table-driven: one case per refusal header in §6,
   each asserting `refused_loop`.
6. **A bounce (DSN) is refused** and does not become a comment.
7. **Reopen within the window reopens; beyond it, a new ticket is created**
   citing the old number — both directions, with the boundary tested.
8. **An inbound comment is always public** — assert on the stored comment's
   visibility, never on the call.
9. **HTML-only mail is stored as text**, with no markup retained.
10. **An oversized or malformed message is refused without an exception
    escaping**, and the mailbox continues processing subsequent messages.
11. **Cross-org isolation** per table, behavioural, failing if the `org_id`
    predicate is removed.
12. **No credential appears** in any API response, log line, or `last_error`.
13. **Router-derived RBAC completeness** for every email-intake route.

---

## 12. Migration notes

Migration `0032`, forward-only: two new tables, one nullable unique column on
`ticket`, an index on `users (org_id, lower(email))` (§5.1), and two permission
codes with their role grants. No existing column is altered.

`test_schema_drift.py` compares `Base.metadata` to the live schema and must
pass with the new tables.

---

## 13. Deferred (explicitly not in v1)

- **DKIM/SPF verification.** The right long-term answer to a forged `From:`;
  deferred because §0.2's two checks already prevent the impersonation that
  matters, and DKIM validation is a substantial dependency.
- **Token rotation.** `email_token` is effectively permanent — it lives in the
  headers of every message in the thread. Rotation needs a policy for the mail
  already in flight and is a design in itself.
- **Quarantine retention.** Spam to a published address is constant and
  `inbound_message` grows without bound. A retention sweep is needed before this
  runs long in production; the disposition record makes the growth visible.
- **Per-sender allow/deny lists and spam scoring** — the mail server in front of
  the mailbox is the right place for both.
- **Staff replying by email.** Only the requester's mail is accepted. A
  technician replying from their client is plausible but needs its own trust
  rules and a visibility decision (staff mail could reasonably be internal),
  which is a design, not a parameter.
- **Any transport other than IMAP** (§0.1).

---

## 14. Related documents

- `docs/modules/service-desk/spec.md` — ticket core; §14 sequences this
  sub-project, §3.1/§3.3 define the reopen paths this module uses unchanged.
- `docs/modules/notification/spec.md` — outbound mail; separate config,
  separate credentials, no shared code.
- `docs/modules/portal/spec.md` — the complementary end-user surface.
- `docs/03-security-model.md` — the invariants this spec applies.
