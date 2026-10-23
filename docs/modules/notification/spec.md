# Module Spec — Notification core

> Second of five sub-projects that together replace Odoo Helpdesk as this
> platform's ticketing system (`docs/modules/service-desk/spec.md` §14). Scoped
> from scratch during brainstorming — no scoping brief preceded this spec, so
> §0 carries more weight than usual.
>
> **This is shared infrastructure, not a Service Desk private.** It is specced
> under `docs/modules/notification/` rather than under `service-desk/` because
> Phase 16 (endpoint health & alerting) publishes to the same facility and must
> not have to reach into another module's docs to learn the contract.
>
> Control-plane only — no agent capability, no new job type, no signed-job path.

---

## 0. Resolved design decisions

These four were settled in brainstorming and are the reason the rest of the
spec looks the way it does. Changing one of them invalidates a section, so they
are recorded before the design rather than inside it.

### 0.1 Webhook egress denies private space by default

A per-org, admin-configurable webhook URL is a **server-side request forgery
primitive by construction**. This platform is domain-admin-equivalent control
over a fleet and runs **on-prem, inside the customer's LAN**, next to the agent
gateway, the vault, and the database. An unguarded webhook lets anyone holding
the config permission aim the control plane at `169.254.169.254`, at the
gateway, or at any host on the internal network — and read the outcome back
through delivery status and timing.

No egress guard exists anywhere in the codebase today: `app/core/http.py`,
despite the name, is inbound request-body bounding only. This module therefore
**introduces one** (§4) rather than assuming one.

Denied by default; an org admin may add specific internal hosts to a per-org
allowlist to permit a legitimate LAN destination (a self-hosted Mattermost, an
internal relay). The default protects the naive case; the allowlist keeps
on-prem deployments usable. Both halves are required — a deny-only design would
have been quietly unusable for exactly the customers this platform targets.

### 0.2 Events reach this module through a kernel-owned transactional outbox

`docs/modules/service-desk/spec.md` §1 forbids the ticket core growing a
private notification path. It has no event hook at all today, deliberately.

Events therefore travel through a **kernel-owned outbox** (`app/core/events.py`
+ `domain_events`), written in the **same database transaction** as the state
change that produced them. This is a kernel extension point that modules
register into — the same shape as audit — not a private path, so §1 is
respected.

Two alternatives were rejected:

- **A service-desk-owned outbox.** Phase 16 alerting needs the identical
  facility and would either duplicate it or import from `service_desk`, making
  alerting depend on ticketing for infrastructure reasons. Dependency pointing
  the wrong way.
- **Deriving events from the audit log.** Zero changes to ticket core, and
  tempting because audit already records every privileged action immutably. But
  audit records privileged *actions*, not domain *events*: its metadata shape
  would become an accidental public contract, machine-origin events (alerts,
  later SLA breaches) are not privileged user actions at all, and it couples a
  tamper-evident security artifact to delivery retry. Rejected.

The transactional property is the point. An event written in the same
transaction as the state change can never describe a fact that was rolled back,
and a crash between "ticket assigned" and "notify the assignee" is not a
reachable state.

### 0.3 Routing is per-org rules over queue membership

An org admin configures which event types reach which channel; **queue
membership** decides the human recipients. This delivers the user-to-queue
membership model that the ticket core deferred here (its §11 G2 fix) precisely
because routing needs the same concept and should get to choose its shape.

**No per-user notification preferences in v1.** Deferred until there is evidence
of notification fatigue, rather than built speculatively — it would add a
subscriptions table, a preferences UI, and a default-for-new-users policy, all
of which are cheaper to add once the real complaint is known than to reshape
later.

### 0.4 Requesters receive email in v1; SMTP is configured per org

A requester is an ordinary `users` row with an `email` and an `org_id`, so
notifying them does not depend on the end-user portal (sub-project 3). Telling
a requester their ticket was resolved is most of the product value, so it ships
now.

SMTP is configured **per org**, not as one shared platform relay, because
per-org config is the shape a multi-tenant on-prem product needs and collapsing
it later is a migration.

**Amended.** The resolution email originally closed with *"If you have
further questions, reply to this ticket to reopen it."* Inbound email intake
is sub-project 4 and does not exist, so a requester who replied got silence —
the copy instructed an action the system could not perform. Portal sub-project
task 6 changed `_render_ticket_resolved` (`app/modules/notification/
delivery.py`) to instead point the requester at the portal (referred to by
name, with no hard-coded URL/host, since the portal's address is a deployment
concern) once the portal's own routes existed to make that instruction true.

---

## 1. Purpose & boundary

Deliver notice of things that happened in the control plane to the humans and
systems that need to know, over email and outbound webhook, reliably and
without becoming an attack surface.

**In scope:** the kernel outbox (§3), the egress guard (§4), channels and their
vault-held credentials (§5), routing rules and queue membership (§6), delivery
with retry and dead-lettering (§7), RBAC/tenancy/audit (§8), the API (§9),
worker integration (§10), and the admin frontend (§11).

**Not in this sub-project:** the end-user portal, inbound email intake, and the
SLA engine — each is its own sub-project. Per-user notification preferences,
digest/batching, a template-editing UI, and chat-native integrations (Slack
app, Teams connector) are deferred (§14); a webhook to those services' incoming
endpoints covers the need in v1.

**Boundary vs Service Desk.** This module knows nothing about tickets. It
consumes `domain_events` by `event_type` and renders from the event payload. It
imports nothing from `app.modules.service_desk`, and a test enforces that.
Ticket core's only change is to *publish* events.

**Boundary vs alerting (Phase 16).** Alerting will publish its own event types
to the same outbox and reuse channels, rules, and delivery unchanged. Nothing
in this spec may assume a human actor: an event carries a **nullable**
`actor_user_id`, and a machine-origin event is a first-class case, not an edge
one.

**Boundary vs audit.** Distinct and both required. Audit is the tamper-evident
record of privileged actions; the outbox is a delivery queue. They are written
from the same transactions but neither reads the other.

---

## 2. Data model

Migration `0025` (current head is `0024`). Every table below carries `org_id`
and gets RLS `ENABLE + FORCE` with a `tenant_isolation` policy — invariant 5,
enforced at the database layer, with a per-table isolation test (§12).

### 2.1 `domain_events` — the outbox (kernel)

`id (uuid7 PK), org_id FK, event_type (str 100), subject_type (str 50),
subject_id (uuid), payload (JSONB), actor_user_id (uuid FK users, NULLABLE),
occurred_at (tz-aware), claimed_at (tz-aware, NULL), processed_at (tz-aware,
NULL), created_at`.

- `actor_user_id` is nullable and **must stay nullable**: Phase 16 alert events
  and any future scheduled-sweep event have no human actor. The ticket core's
  `origin = 'alert'` tickets are the precedent.
- `payload` is the rendering contract. It carries *only* what a notification may
  contain (§6.4) — never a raw model dump, because a model dump is how internal
  content leaks into an external channel.
- Indexed on `(org_id, event_type)` for rule resolution, and — as of
  migration `0027` — a partial index on `org_id` `WHERE processed_at IS NULL`
  for the drain query. (Whole-branch review finding M11: an earlier revision
  of this section named migration `0025`'s `ix_domain_events_drain` — on
  `(claimed_at, occurred_at)` — "for the drain query", but the drain
  (`app.worker.drain_outbox`) actually filters `(org_id, processed_at IS
  NULL)`; neither `claimed_at` nor `occurred_at` appears in that predicate, so
  that index was never usable by it. `ix_domain_events_drain` is left in
  place, unmodified — migrations are forward-only — while migration `0027`
  adds the index that actually matches the query this section describes.)
- `event_type` is a dotted string (`ticket.assigned`, `ticket.status_changed`,
  `ticket.comment_added`, `ticket.internal_note_added`,
  `ticket.approval_requested`, `ticket.resolved`). **Public and internal
  comments are separate event types, not one type carrying a visibility flag** —
  §6.4 depends on this: a rule routing internal content to a requester can only
  be rejected at write time if the rule names an event type that is inherently
  internal. A single `comment_added` type with a flag would push the check to
  render time, which is one template edit away from leaking. The
  registry of valid types lives in `app/core/events.py` and publishing an
  unregistered type raises — a typo'd event type would otherwise silently match
  no rule and deliver nothing, which is invisible in production.

### 2.2 `notification_channels`

`id, org_id FK, kind ('webhook'|'email'), name (str 100), config (JSONB),
credential_id (uuid FK credentials, NULLABLE), active (bool default True),
created_by, created_at, updated_at`.

- `kind='webhook'`: `config = {url}`. The delivery transport always sends a
  `POST` (`_send_webhook` never reads a `method` from `config`) and builds its
  own headers from scratch (`X-Toeshee-Event-Id`, and `Authorization` only
  when a credential is attached) — there is no `method`/`headers` key at all
  (whole-branch review finding M3: an earlier revision of this spec named
  both, and the frontend sent `method: "POST"` into a field nothing consumed;
  both are dropped here rather than implemented). A caller-configurable
  `headers` dict in particular is an invariant-3 hazard — it is a field the
  API returns, and a raw header dict is exactly where a secret ends up if an
  admin pastes a bearer token into it. Secret headers (a bearer token) go to
  the **vault** via `credential_id`, never into `config` — `config` is
  returned by the API and would otherwise be a credential disclosure
  (invariant 3).
- `kind='email'`: `config = {host, port, from_address, use_tls, auth?}`;
  `credential_id` holds the SMTP username/password. *(Amended 2026-09-23:
  `auth: "microsoft"` makes it hold the Microsoft 365 tenant ID, client ID and
  client secret instead, and pins the server to `smtp.office365.com:587` --
  `docs/superpowers/specs/2026-09-11-mail-oauth-design.md` §12.)*
- Unique on `(org_id, name)`.

### 2.3 `notification_rules`

`id, org_id FK, event_type (str 100), channel_id FK, queue_id (uuid FK
ticket_queues, NULLABLE), recipient_kind ('queue_members'|'assignee'|
'requester'|'channel_default'), active (bool default True), created_by,
created_at`.

- `queue_id IS NULL` means "any queue".
- `recipient_kind` decides *who* for email channels; a webhook channel uses
  `channel_default` (the configured URL) and ignores the rest.
- `queue_id` is validated **against the same org** on write. Postgres FK checks
  bypass RLS, so a cross-org id would otherwise be accepted — this is the exact
  bug the ticket core shipped and had to fix in review for
  `default_queue_id`/`approver_role_id`. Same trap, pre-empted here.

### 2.4 `queue_members` — the G2 model

`id, org_id FK, queue_id FK, user_id FK, created_by, created_at`. Unique on
`(queue_id, user_id)`.

Deferred here from the ticket core deliberately. Its consumers are this
module's routing (§6) and, once it exists, the ticket list's "my queues" filter
— which the ticket core's §11 currently approximates with "assigned to me plus
unassigned".

### 2.5 `notification_deliveries`

`id, org_id FK, event_id FK domain_events, channel_id FK, recipient (str 320),
status ('pending'|'sent'|'failed'|'dead'), attempts (int default 0),
last_error (str 500, NULL), last_status_code (int, NULL), next_attempt_at
(tz-aware, NULL), sent_at (tz-aware, NULL), created_at, updated_at`.

- `recipient` is an email address or the webhook URL's host — enough to
  diagnose, and **never** the full URL with a query string, which can carry a
  token.
- `last_error` is a **bounded, sanitized** excerpt (§4.4), never a raw response
  body.
- Indexed on `(status, next_attempt_at)` for the retry sweep.

### 2.6 `webhook_allowlist_entries`

`id, org_id FK, host (str 255), note (str 200), created_by, created_at`. Unique
on `(org_id, host)`.

An entry names a **host**, not a CIDR. Allowlisting a range is how an
allowlist quietly becomes a blanket permit; a host is auditable and specific.
Resolution still happens at send time (§4.2) — an allowlist entry permits a
host, it does not skip resolution.

---

## 3. The outbox (`app/core/events.py`)

```python
async def publish(
    session: AsyncSession, *, org_id: UUID, event_type: str,
    subject_type: str, subject_id: UUID, payload: dict,
    actor_user_id: UUID | None,
) -> None
```

Writes one `domain_events` row **on the caller's session**. It does not commit,
does not open its own transaction, and does not dispatch — those are the three
ways this pattern is normally broken. Committing would break atomicity;
dispatching inline would put an HTTP call inside a database transaction.

`event_type` is checked against the registry and raises `UnknownEventType` if
absent.

**Ticket core's change is exactly this and nothing more:** a `publish(...)` call
in the services that already own each state change — `tickets.assign`,
`tickets.transition`, `threads.add_comment`, `approvals.decide` — inside the
transaction each already has. No new dependency on `app.modules.notification`,
no notification config in the service desk, no behavior change if no rule
matches.

---

## 4. The egress guard (`app/core/egress.py`)

The security core of this module. Every outbound webhook goes through it; there
is no other outbound HTTP path, and a test asserts the notification module
opens no socket except through this helper.

### 4.1 Denied by default

Rejected unless an allowlist entry (§2.6) covers the host: RFC1918
(`10/8`, `172.16/12`, `192.168/16`), loopback (`127/8`, `::1`), link-local
(`169.254/16` — including the cloud metadata address — and `fe80::/10`),
unique-local (`fc00::/7`), multicast, unspecified (`0.0.0.0`), and the
platform's own gateway/vault/database hosts read from config.

The platform's own hosts are denied by **resolved address and destination
port**, not by hostname string. Each platform setting (`database_url`,
`gateway_url`, `gateway_relay_url`, `openbao_addr`) denotes a specific
(host, port); its host is resolved to its address(es), and a request is
refused when **both** the connected (pinned) address and the destination
port match a platform service's own address+port — regardless of what name
was used to reach it, and regardless of any allowlist entry naming that
host or that host:port. An allowlist entry can never grant access to a
platform service's own address+port.

Address-and-port matching, rather than name-only matching, is what makes the
allowlist usable in the single-box on-prem topology this product targets:
the database, vault, gateway, and an internal webhook receiver often share
one physical host, and a receiver on that host, on its own port, is
reachable once an admin explicitly allowlists that exact host:port — a
*different* port on the platform's hostname is not, by itself, the
platform's own service. Matching by name alone would make that host
unreachable on every port unconditionally; matching by address alone
(ignoring port) would make a legitimately different service on the same box
permanently unreachable even when explicitly allowlisted. Matching by name
only, with no address cross-check, is also defeated by reaching the
platform's address under a different name (an alias, a CNAME, a differently
configured hostname that happens to resolve to the same box) — a bypass
that address+port matching closes regardless of what name was used.

A cheap by-name check (no DNS required) still runs first, both as a fast
path and as the fail-closed behaviour when a platform host cannot be
resolved: a platform host stays refused by its own configured name even
during a DNS outage, and a destination host that itself fails to resolve is
refused (`dns_failure`), never silently permitted.

`https` only. Plain `http` is permitted **only** for an allowlisted host, since
an internal Mattermost on a LAN may legitimately not have TLS, and forcing
`https` there would push admins to disable the guard entirely.

### 4.2 Resolve-all, then pin

Resolve the hostname to **every** A/AAAA record. If *any* record lands in denied
space and the host is not allowlisted, reject — checking only the first record
lets a multi-record DNS answer walk past the guard.

Then **connect to the resolved IP that was checked**, with the `Host` header
preserved, rather than re-resolving inside the HTTP client. Re-resolution is the
DNS-rebinding hole: the guard validates a public answer and the client connects
to a private one seconds later.

### 4.3 No redirects

Redirects are **refused**, not followed and re-validated. A followed redirect is
a second request to an unvalidated destination, and per-hop re-validation is
easy to get subtly wrong. A `3xx` is a terminal delivery failure with a clear
error; a legitimate endpoint should not be redirecting a webhook.

### 4.4 The response is not an oracle

- The response **body is never returned through the API**, never stored in
  full, and never logged. `notification_deliveries` keeps the status code and a
  bounded (500-char), control-character-stripped excerpt for diagnosis.
- The read is bounded (64 KiB) and the whole request is deadlined (10s connect,
  30s total) — an endpoint that trickles bytes must not pin a worker.
- Egress rejections are recorded with the *reason category* (`blocked_private`,
  `dns_failure`, `redirect_refused`), not a per-IP detail that would let an
  admin map the internal network by reading delivery errors.

### 4.5 SMTP is deliberately NOT subject to this guard

§4's opening line ("every outbound webhook goes through it; there is no other
outbound HTTP path") describes the webhook transport only. It is silent about
SMTP, and that silence is a gap this section closes: an email channel's
admin-controlled `config.host`/`config.port` (§2.2) **is a connect primitive
to an arbitrary host:port**, exactly like a webhook URL is, and the guard
above does not apply to it.

This is a deliberate, recorded asymmetry, not an oversight (whole-branch
review, triage item 3). An on-prem LAN mail relay is the *normal* deployment
for this product (§0.1, §0.4) -- routing SMTP through a deny-private-address
guard would make email unusable for exactly the customers this platform
targets, since the relay itself is almost always a private address. Denying
private space by default on the one transport that is *supposed* to reach
private space would be the deny-only design §0.1 already rejects for
webhooks, applied to the wrong transport.

What actually constrains it, instead of the egress guard:

- `notification.admin` gates every write to `config.host`/`config.port` (§8)
  -- the same high-privilege grant that gates a webhook channel's URL.
- Every SMTP credential checkout is audited (§5, §8), same as a webhook
  channel's bearer token.
- `_send_email` returns only a classified `error_reason` category
  (`smtp_permanent_reject`, `smtp_transient_reject`, `timeout`,
  `connection_reset`) -- never a raw SMTP reply body -- so there is no
  response-content oracle back to the API, the SMTP-side equivalent of §4.4's
  webhook rule.
- `POST /channels/{id}/test` (§9) dispatches an email test through the
  identical `_send_email` path a real delivery uses, so a test send is not a
  looser variant of what a real delivery already does.

There is no plan to add DNS/IP-range checks to the SMTP path: doing so would
break the on-prem relay case this asymmetry exists to serve, for a transport
whose destination is already an admin-controlled, audited configuration
value, not attacker input.

---

## 5. Channels and credentials

Credentials live in the vault and are **checked out at point of use** inside the
worker, held for the duration of one delivery attempt, and never written to
`config`, an API response, a log line, or a delivery row (invariant 3). The
existing `VaultService.store`/`checkout` surface is used as-is.

Every checkout writes an audit event (invariant 4) — this is already the vault's
behavior and is called out because a delivery worker checking out an SMTP
password on every send is a high-volume audit path that should be understood as
intentional, not discovered later as noise.

**Test send** (§9) validates a channel against a real destination. It is gated
on `notification.admin`, rate-limited per org, audited, and **subject to the
identical egress guard** — a test-send path that skipped the guard would be a
trivially reachable SSRF bypass, and "it's only a test" is exactly how that ships.

---

## 6. Routing

### 6.1 Resolution

For each unprocessed event: select active rules for `(org_id, event_type)` whose
`queue_id` is NULL or matches the event's queue; for each rule, expand
`recipient_kind` to concrete recipients; write one `notification_deliveries` row
per `(event, channel, recipient)`.

Expansion is deduplicated — a user who is both the assignee and a queue member
receives one email, not two.

### 6.2 Zero rules is a valid state

An org with no rules generates no deliveries, and that is not an error. The
event is still marked processed.

### 6.3 Recipients must be in the event's org

Every resolved recipient is re-checked against the event's `org_id` before a
delivery row is written. Queue membership is already org-scoped, but this is the
last point where a cross-tenant recipient could enter, and a notification
delivered to the wrong tenant is a data breach, not a bug.

### 6.4 Internal content never reaches an external recipient

The ticket core distinguishes **public** from **internal** comments. A naive
"comment added" notification emails internal staff discussion to the requester.

Two mechanisms, both required:

1. **The event payload carries only publishable content.** A
   `ticket.internal_note_added` event carries the fact and the author, **not the
   body** — so even a mis-routed delivery cannot disclose the text.
2. **Visibility is part of routing, not just rendering.** Because internal notes
   have their own event type (§2.1), a rule pairing
   `ticket.internal_note_added` with `recipient_kind='requester'` is rejected at
   **write time**, with a clear error. The invalid configuration cannot be
   saved, rather than being saved and filtered later.

A rendering-only guard would be one template edit away from leaking. §12
requires an explicit test that a requester recipient never receives internal
comment text.

---

## 7. Delivery

**At-least-once.** Exponential backoff (1m, 5m, 25m, 2h, 10h) -- five gaps
between six total attempts -- then `dead`. `MAX_ATTEMPTS` is derived as
`len(BACKOFF_SECONDS) + 1`, never a second hand-picked number, specifically so
the schedule and the cap cannot silently drift out of sync again (an earlier
revision set `MAX_ATTEMPTS = len(BACKOFF_SECONDS)`, which gave five attempts
only four gaps and left the 10h delay dead code -- the real retry window was
2.52h, not the 12.52h the five delays sum to; whole-branch review finding I4).
Duplicates are possible by design; every webhook carries a stable
`X-Toeshee-Event-Id` header so a consumer can dedupe.

**Retryable vs terminal.** A 5xx, a timeout, or a connection reset retries. A
4xx (other than 429), an SMTP permanent reject, a DNS failure, an egress
rejection, and a refused redirect are **terminal** — they go straight to `dead`
without burning five attempts, because none of them will succeed on a retry and
retrying an allowlist-blocked host five times is just five more SSRF attempts.

**Why `failed` is a due status, not just `pending`.** A retryable failure sets
`status='failed'` and a future `next_attempt_at`; it never returns the row to
`pending`. A due-query matching only `pending` would therefore make the entire
retry mechanism unreachable dead code — dead-lettering would still work, retries
would silently never happen, and no test asserting "a 500 is retryable" would
fail, because that assertion is about classification rather than about the row
ever being picked up again. (This spec said `status='pending'` until Task 11
caught it.)

**Claim before send.** The worker claims with `SELECT … FOR UPDATE SKIP LOCKED`,
so concurrent workers cannot double-claim, and marks `sent` only after the
attempt returns. A worker that dies mid-attempt leaves the row claimed-but-
unsent, and the stuck-claim sweep (§10) returns it — the same pattern
`app/worker.py` already uses for stuck job dispatch (`STUCK_DISPATCH_AFTER`).

**Dead letters are visible.** `GET /notifications/deliveries?status=dead` and a
count surfaced in the admin UI. A dead-lettering notification system that fails
silently is indistinguishable from one that works.

---

## 8. Security, tenancy, audit

**Permissions** (added to `app/core/rbac.py`, `<noun>.<verb>` per convention):

- `notification.read` — view channels, rules, queue membership, delivery status.
- `notification.admin` — create/modify channels, rules, allowlist entries, queue
  membership; run a test send.

Every route carries `require_permission` server-side (invariant 6). Following
the ticket core's pattern, RBAC completeness is **derived from the router** so a
route added later without a permission test fails the suite.

`notification.admin` is a **high-privilege grant** and the spec says so plainly:
it configures where control-plane data is sent and which internal hosts are
reachable. It belongs with the admin tier, not the technician tier.

**Audited** (invariant 4): channel create/update/delete, rule create/update/
delete, allowlist add/remove, queue-membership change, test send, and every
credential checkout. Delivery *attempts* are **not** audited — they are
operational records in `notification_deliveries`, and writing thousands of
attempt rows into a hash-chained audit log would degrade the artifact that
matters for privileged actions.

**Tenancy** (invariant 5): RLS `ENABLE + FORCE` + `tenant_isolation` on all six
tables, with a per-table cross-org isolation test. Every isolation test must
fail if its `org_id` predicate is deleted — the ticket core shipped one that
passed for the wrong reason because an upstream check rejected the case first,
and that is the mistake this line exists to prevent.

---

## 9. API

Under `/api/v1/notifications`, versioned per convention. Route ordering places
literal segments before `{id}` parameters — the Phase-11/12 lesson, repeated in
the ticket core.

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/channels` | `notification.read` |
| POST | `/channels` | `notification.admin` |
| GET/PATCH/DELETE | `/channels/{id}` | read / admin / admin |
| POST | `/channels/{id}/test` | `notification.admin` |
| GET | `/rules` | `notification.read` |
| POST | `/rules` | `notification.admin` |
| PATCH/DELETE | `/rules/{id}` | `notification.admin` |
| GET | `/allowlist` | `notification.read` |
| POST/DELETE | `/allowlist`, `/allowlist/{id}` | `notification.admin` |
| GET | `/queues/{queue_id}/members` | `notification.read` |
| PUT/DELETE | `/queues/{queue_id}/members/{user_id}` | `notification.admin` |
| GET | `/deliveries` | `notification.read` |

`/deliveries` filters on `status`, `channel_id`, `event_type`, and a time range,
with pagination matching the ticket core's convention. Any list-equality
assertion in a test requires an explicit `ORDER BY` in the query — the
convention carried from prior modules.

**No response ever contains a credential, a full webhook URL with query
string, or the raw remote response body.** (Whole-branch review finding M8:
an earlier revision of this sentence said flatly "or a response body", which
contradicts §2.5/§4.4 below and was never true of `GET /deliveries` in the
first place -- `DeliveryOut.last_error` **is**, deliberately, a bounded
(500-char), control-character-stripped **excerpt** of a real failed
delivery's remote response, kept for diagnosis, and it **is** readable by any
`notification.read` holder. What must never appear in any response is the
*raw, unbounded* body -- unread, unlogged, unreturned in full, per §4.4 -- not
the excerpt itself, which is the one piece of the response this module
deliberately keeps.)

---

## 10. Worker integration

Two Celery tasks added to the existing `app/worker.py`, following its
established shape — async logic in a testable function, the task a thin
scheduling shell:

- `drain_outbox(org_id)` — claim unprocessed `domain_events`, resolve rules,
  write delivery rows, mark processed.
- `deliver_pending(org_id)` — claim due deliveries (`status IN ('pending',
  'failed')`,
  `next_attempt_at <= now`), attempt each, record the outcome; and return
  stuck claims older than the sweep threshold.

Both are org-scoped and idempotent. Neither holds a database transaction open
across an outbound network call — the delivery attempt happens outside the
claim transaction, which is why the stuck-claim sweep exists.

---

## 11. Frontend

One admin surface under the existing module nav, gated on `notification.read`
with mutations gated on `notification.admin`:

- **Channels** — list, create/edit (webhook or email), test-send with an
  inline result, active toggle.
- **Rules** — event type → channel → recipient kind, optional queue scope.
- **Queue members** — add/remove users per queue.
- **Allowlist** — host entries with their notes, and a visible warning that an
  entry permits the control plane to reach an internal host.
- **Deliveries** — recent deliveries with status, attempt count, and the dead
  count surfaced prominently.

Strict TypeScript, no `any`; TanStack Query for server state; badge and empty-
state conventions carried from prior modules. The UI **never** gates security —
it hides what the caller cannot use, and the server rejects it regardless.

---

## 12. Testing requirements

Test-first per `CLAUDE.md`. Beyond the usual per-endpoint and per-service
coverage, these are required and named because each corresponds to a specific
way this module can fail silently:

1. **Egress guard, table-driven** — one case per denied range (RFC1918 ×3,
   loopback v4/v6, link-local incl. `169.254.169.254`, ULA, multicast,
   unspecified), each asserting rejection.
2. **DNS rebinding** — a resolver returning a public address on the first call
   and a private one on the second must not result in a connection to the
   private address.
3. **Multi-record DNS** — a host resolving to one public and one private record
   is rejected.
4. **Redirect refusal** — a `3xx` is a terminal failure, and no second request
   is issued.
5. **Allowlist opt-back-in** — an allowlisted internal host is permitted, and
   only that host.
6. **Test-send uses the guard** — a test send to a private address is rejected
   exactly as a real delivery is.
7. **Internal-comment leak** — a requester recipient never receives internal
   comment text, asserted on the delivered payload, not on the rule.
8. **Outbox atomicity** — an event published in a transaction that rolls back
   leaves no `domain_events` row.
9. **Retry and dead-lettering** — backoff schedule, cap, terminal-vs-retryable
   classification, and the stuck-claim sweep.
10. **Tenancy isolation, per table** — each must fail if its `org_id` predicate
    is removed.
11. **No secret egress** — credentials appear in no API response, no log line,
    and no delivery row.
12. **Module boundary** — `app.modules.notification` imports nothing from
    `app.modules.service_desk`.

---

## 13. Migration notes

Migration `0025`, forward-only. Six new tables plus two new permissions. No
existing table is altered except that ticket core's services gain `publish(...)`
calls, which are code-only.

`test_schema_drift.py` enforces model/migration agreement and must pass with
the new tables.

---

## 14. Deferred (explicitly not in v1)

- **Per-user notification preferences and opt-out** (§0.3) — until there is
  evidence of fatigue.
- **Digest and batching** — a busy queue emitting one email per comment is the
  most likely first complaint; the delivery model can absorb batching later
  without a reshape.
- **Template editing UI** — payload rendering is code in v1.
- **Chat-native integrations** (Slack app, Teams connector) — a webhook to their
  incoming endpoints covers v1.
- **In-app notification centre** — a read surface inside the platform, distinct
  from outbound delivery.
- **Escalation on non-acknowledgement** — belongs with the SLA engine
  (sub-project 5), which owns the scheduled sweep it needs.

---

## 15. Related documents

- `docs/modules/service-desk/spec.md` — ticket core; §14 sequences this
  sub-project and §11 defers queue membership to it.
- `docs/modules/endpoint-health-alerting.md` — Phase 16, the second consumer of
  the outbox, channels, and delivery.
- `docs/03-security-model.md` — the invariants this spec applies.
- `docs/06-roadmap.md` — Phase 14's five sub-projects.
