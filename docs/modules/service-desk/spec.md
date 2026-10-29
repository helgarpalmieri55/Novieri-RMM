# Module Spec — Service Desk (ticket core)

> First of five sub-projects that together replace Odoo Helpdesk as this
> platform's ticketing system. This spec covers the **ticket core only**;
> notification, the end-user portal, email intake, and the SLA engine each get
> their own spec and build cycle (§14). Control-plane only — no agent
> capability, no worker.
>
> No scoping brief preceded this spec: the module was scoped from scratch
> during brainstorming, so §0 carries more weight than usual.

---

## 0. Resolved design decisions

1. **This platform now owns ticketing. `docs/00-vision.md` is amended
   deliberately.** That document listed "not a full ITSM suite … integrates
   with Odoo Helpdesk for ticketing rather than reimplementing a service desk"
   as an explicit non-goal, and named Odoo Helpdesk under integration posture.
   That decision is reversed by this spec, on the record, because the platform
   is being built to be commercialized standalone — a customer with no Odoo
   still needs a queue — and because a ticket that can show the device's
   inventory, patch state, and open alerts inline is something an external
   helpdesk structurally cannot do (§7.3). `docs/00-vision.md` and
   `docs/06-roadmap.md` are edited in the same commit as this spec; the code
   and the docs must never disagree about this.
2. **Clean break from Odoo Helpdesk — one system of record.** No `external_id`,
   no `synced_at`, no sync adapter, no dual-run. Odoo remains Toeshee's ERP and
   remains the Budget module's CSV export target (`docs/modules/budget/spec.md`
   §7); it is simply not where tickets live. Rejected alternatives: mirroring
   (two sources of truth and permanent reconciliation) and one-way export
   (defensible, but buys reporting continuity nobody asked for at the cost of
   an adapter to maintain).
3. **Two ticket types with different lifecycles** (§3): `incident` (something
   broke — restore service) and `request` (I need something — fulfil it). One
   table with a `type` discriminator, not two tables: comments, attachments,
   queues, assignment, and the unified "everything I am working on" list are all
   shared, and separate tables would duplicate every one of them and turn the
   primary technician view into a `UNION`.
4. **Service requests carry a per-category approval requirement** (§4).
   `ticket_category.requires_approval` decides whether a request needs
   authorization before fulfilment; categories that do not need it skip straight
   to `in_progress`. This is what justifies splitting the two types at all —
   without it a request is an incident with a different word on it. Incidents
   are never approved. Rejected: always-require-approval (technicians route
   around a signature on a password reset by filing an incident instead).
5. **Queues plus an optional assignee** (§5), not an assignee alone. A queue is
   what makes unassigned work visible — with only `assigned_to`, an unassigned
   ticket belongs to nobody and is found only by someone filtering for NULLs.
   The queue is also the natural anchor for the SLA rules and notification
   routing that arrive in later sub-projects, so it exists now rather than
   forcing a model reshape later.
6. **Technician holds read, write, and assign** (§8.2) — the inverse of Budget,
   which deliberately grants technician nothing. This is a technician's primary
   tool; a service desk they cannot fully use is pointless. `ticket.approve`
   and `ticket.admin` are separate and are not granted to technician.

---

## 1. Purpose & boundary

Record, route, and resolve IT work — incidents and service requests — inside
the platform that already manages the fleet, so a ticket can carry the device
context that makes it actionable.

**In scope for this sub-project:** ticket core (§2), the two lifecycles (§3),
request approval (§4), queues and assignment (§5), comments and attachments
(§6), the change-management adapter (§7.1), RBAC/tenancy/audit (§8), API (§9),
and the technician frontend (§11).

**Not in this sub-project, by sequencing decision:** notification dispatch
(webhook + SMTP), the end-user portal, email intake, and the SLA engine. Each
is specced and built separately (§14). This module must not grow a private
notification path or a private SLA field in the meantime — when those arrive
they arrive as their own thing.

**Boundary vs Change Management.** A change request is a *planned, intentional*
modification to the environment with approval and a rollback plan; a ticket is
*unplanned work* (incident) or *a request for something* (service request).
They are different objects and neither absorbs the other. The link between them
is one-directional and explicit (§7.1).

**Boundary vs the monitoring platform.** Unchanged and unaffected: this module
holds no metrics and no time series. It receives alerts as ticket origins from
Phase 16, which has shipped (§7.2).

---

## 2. Data model

All tables org-scoped with RLS `ENABLE + FORCE` and a `tenant_isolation` policy
(`USING (org_id = current_setting('app.current_org', true)::uuid)` plus matching
`WITH CHECK`), exactly as every prior module. `backend/tests/test_rls.py` and
`backend/tests/test_schema_drift.py` both derive their subjects by
introspection, so these tables are covered automatically with no per-module
wiring — but the migration must still declare the policies, or those tests fail.

Migration is the next in sequence (current head is `0022`; the implementation
plan's first task confirms it).

> **Amended 2026-10-29 (MSP clients plan 2026-10-26, Tasks 2 and 3): a ticket
> belongs to a client.** `ticket` carries `client_id` (C, migration 0234:
> existing tickets took their device's client, else a contact requester's,
> else the internal client), and its five children — comments, attachments,
> approvals, status history and `ticket_sla` — inherit it from the ticket (H).
> The restrictive client policy of `CLAUDE.md` invariant 5a is on all six,
> beside `portal_isolation`. A new ticket takes its device's client, or the
> client the request names; neither, or two that disagree, is a 422, and only
> mail files a ticket under Unassigned (plan decision C10).
> `its_ticket_client_guard` holds the device and a contact requester to the
> ticket's client, and an assignee must hold `ticket.read` there.
> `POST /service-desk/tickets/{id}/move` moves a ticket to another active
> client, which the mover must hold `ticket.write` in. A ticket with a device
> does not move (409, decision C9: take the device off first), nor does one an
> alert or a change opened (origin `alert` or `integration`), or one a health
> alert still names. The children follow the ticket, an assignee without
> `ticket.read` in the destination is taken off it, and the SLA is resolved
> again for the new client — or removed, when nothing resolves there
> (`docs/modules/sla/spec.md` §3.3). Notifications about a ticket reach only that client's contacts and
> staff with a role in that client (`app/modules/notification/routing.py`).
> The plan is `docs/superpowers/plans/2026-10-26-msp-clientes-fase2-modulos.md`.

### `ticket_queue`
`id, org_id, key (str(64)), name (str(255)), description (Text, default ""),
active (bool, default True), sort_order (int, default 0), created_by,
created_at`.
Unique `(org_id, key)`.

Seeded per org on first use with `helpdesk`, `infrastructure`, `security`,
following the lazy-seed pattern `refdata.seed_default_categories` established
(`docs/modules/budget/spec.md` §8) — concurrency-safe
`pg_insert(...).on_conflict_do_nothing()` plus an unconditional re-select, and
audited **once** as `ticket.queues_seeded` with `actor_type="system"`, never as
a per-row user action. An operator-created queue keeps its own audit event.

### `ticket_category`
`id, org_id, key (str(64)), name (str(255)), type (str(16): "incident" |
"request"), requires_approval (bool, default False), approver_role_id (FK
roles.id | None), default_queue_id (FK ticket_queue.id | None), active (bool,
default True), sort_order (int, default 0), created_by, created_at`.
Unique `(org_id, key)`.

`requires_approval` is meaningful only when `type = "request"`; a category with
`type = "incident"` and `requires_approval = True` is rejected at the service
layer as a typed validation error, and a check constraint pins the same rule so
it cannot be introduced by direct SQL.

`approver_role_id` names the RBAC role whose holders may decide the approval. It
is nullable: when NULL, any holder of `ticket.approve` may decide.

*(Corrected during the whole-branch security review, finding A2: this
paragraph previously claimed the FK to `roles` alone makes a cross-org role
id impossible. That is factually wrong. `roles.org_id` is nullable — a role
is either global or scoped to one specific org — so the FK is satisfied by
ANY row in `roles`, including one scoped to a different org entirely; and
Postgres FK checks always bypass row security, so RLS gives no protection
here either. The service layer (`refdata.resolve_role`) resolves
`approver_role_id` org-scoped before every write — usable if the role is
global (`org_id IS NULL`) or belongs to this org, rejected as
`TicketNotFoundError` otherwise, the identical treatment `resolve_queue`
already gives `default_queue_id` — because the FK is not, and cannot be, a
substitute for that check.)*

**Categories are seeded per org on first use, exactly like queues** — same
concurrency-safe `pg_insert(...).on_conflict_do_nothing()` + re-select, same
single system-attributed `ticket.categories_seeded` audit event, nothing emitted
when nothing is inserted.

*(Added after Task 6. The spec originally seeded queues only, which left a
brand-new org unable to create a single ticket: `ticket.category_id` is NOT
NULL, so with no categories there is no legal ticket to create until an
administrator hand-builds a taxonomy. That is the same "unusable on arrival"
shape Phase 13 shipped and had to fix in its whole-branch review, and it is
cheaper to close here.)*

| key | type | default queue | requires approval |
|---|---|---|---|
| `hardware` | incident | `helpdesk` | — |
| `software` | incident | `helpdesk` | — |
| `network` | incident | `infrastructure` | — |
| `access` | incident | `security` | — |
| `other` | incident | `helpdesk` | — |
| `access_request` | request | `security` | **yes** |
| `equipment_request` | request | `helpdesk` | **yes** |
| `software_request` | request | `helpdesk` | no |
| `other_request` | request | `helpdesk` | no |

The two approval-requiring defaults are deliberate: "grant me access" and "buy
me a laptop" are the canonical cases §4 exists for, so a fresh org exercises
the approval path without anyone configuring it, while a software install or a
general request does not sit behind a signature. An org may edit or deactivate
any of these — they are a starting taxonomy, not a fixed one.

Seeding categories requires the queues to exist first (`default_queue_id`
resolves against them), so the category seed calls the queue seed rather than
assuming it has run.

### `ticket_number_counter`
`org_id (PK), next_incident (int, default 1), next_request (int, default 1)`.

Backs the human-facing ticket number (§2.1). One row per org, created lazily.

### `ticket`
`id, org_id, number (str(24)), type (str(16)), category_id (FK
ticket_category), queue_id (FK ticket_queue), status (str(32)), priority
(str(16): "low" | "normal" | "high" | "urgent", default "normal"), subject
(str(255)), description (Text, default ""), requester_id (FK users.id | None),
assigned_to (FK users.id | None), device_id (FK devices.id | None), origin
(str(16): "technician" | "alert" | "portal" | "email" | "integration"),
source_alert_id (UUID | None), resolved_at (datetime | None), closed_at
(datetime | None), created_by (FK users.id | None), created_at, updated_at`.

`status` is `str(32)`, not `str(16)`: the longest value, `pending_approval`, is
exactly 16 characters, and a column sized to its current longest value has no
room for the next one. `test_schema_drift.py` enforces model/migration agreement
on the length, so widening later is a migration rather than an edit.

**`requester_id` and `created_by` are both nullable, and only for `origin =
"alert"` tickets.** A Phase-15 alert ticket has no human requester and no human
author; forcing a synthetic user row to satisfy a NOT NULL would put a fake
person in the audit trail. A check constraint requires both to be NOT NULL
whenever `origin != "alert"`, so a technician-, portal-, email-, or
integration-origin ticket can never lose its human attribution — see
`origin = "integration"` below: it is a machine-triggered CREATE, but not a
human-less one.

Unique `(org_id, number)`. Indexes on `(org_id, status)`, `(org_id, queue_id,
status)`, `(org_id, assigned_to, status)`, `(org_id, requester_id)`,
`(org_id, device_id)`.

- `status` is constrained per `type` by a check constraint (§3.3). The service
  layer validates transitions; the constraint guarantees no invalid
  `(type, status)` pair can exist at all.
- `device_id` is the differentiator (§7.3). Nullable — plenty of tickets are not
  about a device.
- `origin` records how the ticket entered the system. `"portal"` and `"email"`
  are declared now and unused until those sub-projects ship; declaring them now
  avoids a check-constraint migration later. `"integration"` (added by
  migration 0024, whole-branch security review finding A3) is used by another
  platform module's own adapter opening a ticket in reaction to ITS OWN event
  (§7.1's change-management adapter) rather than by a human filling in the
  create form or by Phase 16's alert pipeline; `create_ticket` attributes the
  CREATE action itself (`ticket_status_history`/`ticket.created`'s
  `actor_type`) to `"system"` for this origin, same as `"alert"` — but,
  unlike `"alert"`, `requester_id`/`created_by` are still required and still
  human, since the ticket genuinely has a real requester behind it (e.g. the
  triggering change's author); only the create act's own attribution differs.
  Migration 0024 also adds `ck_ticket_origin_valid`, the first DB-layer CHECK
  pinning this domain — previously enforced only by this schema's own
  `Literal`.
- `source_alert_id` was **reserved** for Phase 16 and is now in use (§7.2). It
  carries **no** foreign key,
  by the same reasoning `subscriptions.license_ref` uses: the two modules stay
  independently deployable and a dangling reference is tolerated rather than
  blocking. It is NULL for every ticket an alert did not open.

### `ticket_comment`
`id, org_id, ticket_id (FK ticket), author_id (FK users.id | None), author_type
(str(16): "user" | "system"), body (Text), visibility (str(16): "public" |
"internal", default "public"), created_at`.

`visibility` exists in v1 even though nothing external can read comments yet.
The portal sub-project will expose `public` comments to end users; introducing
the distinction later would mean auditing every comment written before it
existed to decide what an end user may now see. `author_id` is nullable only
when `author_type = "system"` (check constraint), so a machine comment is never
attributed to a person.

### `ticket_attachment`
`id, org_id, ticket_id (FK ticket), object_key (Text), filename (str(255)),
byte_size (int), uploaded_by (FK users.id), created_at`.

Storage follows the hardened path Budget landed (`docs/modules/budget/spec.md`
§5), and does not re-derive it:
- The object key is **entirely server-derived** —
  `{org_id}/service-desk/tickets/{ticket_id}/{attachment_id}` — with no
  client-supplied segment anywhere in it.
- `filename` is the sanitized **display** name only: `os.path.basename`, then an
  ASCII-only allow-list anchored with `fullmatch`. Both details are
  load-bearing and their reasons are recorded in budget's spec; do not
  "simplify" either.
- Upload is bounded on both the declared `Content-Length` and the bytes actually
  read, via `app/core/http.py`.

### `ticket_approval`
`id, org_id, ticket_id (FK ticket), state (str(16): "pending" | "approved" |
"rejected"), decided_by (FK users.id | None), decided_at (datetime | None),
reason (Text, default ""), created_at`.
Unique `(org_id, ticket_id)` — one approval record per request.

### `ticket_status_history`
`id, org_id, ticket_id (FK ticket), from_status (str(32) | None), to_status
(str(32)), actor_type (str(16): "user" | "system"), actor_id (FK users.id |
None), reason (Text, default ""), created_at`.

`from_status`/`to_status` are `str(32)` to match `ticket.status` exactly. A
history column narrower than the column it records would accept every status
today and fail on the first longer one added — and it would fail on write, after
the transition had already been applied.

Every transition, including the initial creation (`from_status` NULL). This is
the module's own history, complementary to the immutable audit log — the same
belt-and-braces change management uses.

### 2.1 Ticket numbers (race-safe by construction)

Users need `INC-1042`, not a UUID. The number is `INC-{n}` for incidents and
`REQ-{n}` for requests, with `n` per-org and per-type.

Allocation is a **single atomic statement**, never a read-then-write:

```sql
UPDATE ticket_number_counter
   SET next_incident = next_incident + 1
 WHERE org_id = :org
RETURNING next_incident - 1
```

The counter row is created lazily with `pg_insert(...).on_conflict_do_nothing()`
followed by the `UPDATE ... RETURNING`, so two concurrent first-ever creates in
one org cannot both insert, and neither can read a stale counter.

A check-then-increment here would be exactly the TOCTOU class the platform
hardening PR swept out of five modules, and it would produce duplicate ticket
numbers — visible to users and permanent. §12 requires a concurrency test.

Numbers are **not** reused when a ticket is cancelled, and gaps are acceptable.

---

## 3. Ticket lifecycle

### 3.1 Incident

```
                  ┌─────────────┐
                  ▼             │
new ──► open ──────────────► resolved ──► closed
          │  ▲                 ▲   ▲          │
          ▼  │                 │   └──────────┘ (reopen)
        pending ───────────────┘
                                (reopen: resolved ──► open,
                                         closed ──► open)

any non-terminal ──► cancelled
```

Read the edges, not the left-to-right order: `pending` is an **optional
detour**, not a mandatory stage. `open ──► resolved` is the common path — most
incidents never wait on anyone. `open ──► pending` when something blocks,
`pending ──► open` when it unblocks, and `pending ──► resolved` when the third
party comes back and the fix lands directly from there.

*(An earlier revision of this diagram drew row 1 as a single linear chain,
which literally read as "every incident must pass through `pending` to reach
`resolved`". That was a drawing error, not the intent, and it is recorded here
because a reviewer parsing the diagram by column position caught the code and
the picture disagreeing.)*

- `pending` means waiting on someone else (requester or a third party). It is a
  first-class state, not a flag, because the SLA engine will pause the clock on
  it and reporting needs to distinguish "we are stuck on them" from "we have not
  started".
- `resolved` is the technician asserting the work is done. `closed` is the
  final state, reached on requester confirmation or by the auto-close path.
- **Reopen** moves `resolved → open` or `closed → open` and is recorded in
  `ticket_status_history` like any other transition, so reopens are countable
  rather than a lost signal.

### 3.2 Service request

```
new ──► pending_approval ──► in_progress ──► fulfilled ──► closed
         │                        ▲              │
         │                        └──────────────┘ (reopen)
         └──► rejected

new ──► in_progress                    (category requires no approval)
any non-terminal ──► cancelled
```

- `pending_approval` is entered only when the category requires approval (§4).
  Otherwise creation lands directly in `in_progress`.
- `rejected` is finally terminal and is reached only from `pending_approval`.
- **Reopen** moves `fulfilled → in_progress` or `closed → in_progress`, the
  mirror of §3.1's incident reopen, and is recorded in `ticket_status_history`
  like any other transition.

### 3.3 Enforcement

- Allowed statuses per type and the full transition table live in one module
  (`lifecycle.py`), used by both the service layer and the tests. There is no
  second copy — a divergent second implementation of "what state can this be
  in" is precisely how the Budget module produced two Critical double-counts.
- A DB check constraint pins the valid `(type, status)` pairs.
- **Two different notions of "terminal", and conflating them is a bug.**
  - `cancelled` and `rejected` are **finally terminal**: no transition leaves
    them, ever. A cancelled ticket that can be revived is a ticket whose
    history lies.
  - `closed` is **terminal for editing, but reopenable**: it accepts no field
    edits and no new comments, yet `closed → open` (incident) and
    `closed → in_progress` (request) are sanctioned transitions (§3.1, §3.2).
    Reopens must stay countable, which is the whole reason `closed` is distinct
    from `resolved`/`fulfilled`.

  So `TERMINAL_STATUSES` may gate **field edits and comments** — and must never
  gate the status-transition path itself. A `if status in TERMINAL_STATUSES:
  reject()` guard on the transition endpoint would silently break every
  legitimate reopen. Use `lifecycle.assert_transition` there; it already knows
  which reopens are legal.

  The tests must assert the *invalid* transitions are rejected, not merely that
  valid ones work — a state machine tested only on its happy path is unproven.
- Every transition records an actor. A machine transition (Phase 16 alert
  recovery, future auto-close) uses `actor_type="system"` with a NULL
  `actor_id`, so it never reads as a technician's action.
- **`resolved_at`/`closed_at` stamping and reopen-clearing** (Task 8 fix
  round): entering `resolved` (incident) or `fulfilled` (request) stamps
  `resolved_at`; entering `closed` (either type) stamps `closed_at`. **Any
  reopen clears `resolved_at`, with no exception** — both `resolved`/
  `fulfilled → open`/`in_progress` and `closed → open`/`in_progress` are
  reopens, and both clear it. `closed_at` clears whenever `closed` is left,
  which — since `closed`'s only legal exits are the reopen edges — is always
  also a reopen. A plain forward progression (`resolved`/`fulfilled →
  closed`) or a cancellation is not a reopen and leaves `resolved_at` alone
  as the historical record that the ticket really was resolved/fulfilled.
  This is stated explicitly because the SLA sub-project (§14) reads
  `resolved_at` to mean "this ticket's open work concluded" and must not have
  to infer the rule from the code: two tickets reopened by different edges
  must agree on whether `resolved_at IS NOT NULL`.

---

## 4. Request approval

- On creating a `request` whose category has `requires_approval = True`, the
  service creates a `ticket_approval` row in `pending` and the ticket enters
  `pending_approval`.
- Deciding requires `ticket.approve` **and**, when
  `ticket_category.approver_role_id` is set, membership of that role. Both are
  checked server-side; the frontend never gates this.
- **The requester may not approve their own request**, even holding
  `ticket.approve`. This is checked explicitly and tested explicitly — it is the
  entire point of separating `approve` from `write`.
- Approval moves the ticket to `in_progress`; rejection moves it to `rejected`
  (terminal) and requires a non-empty `reason`.
- Both decisions are audited (`ticket.approved` / `ticket.rejected`) and written
  to `ticket_status_history`.
- **`pending_approval → in_progress` and `pending_approval → rejected` are
  reachable ONLY through this decision, never through the generic status
  endpoint** (whole-branch security review, finding A1). `lifecycle.py` marks
  both transitions legal for the `request` type — that is unchanged and
  remains the sole legality authority — but `transitions.change_status`
  refuses them for any caller that isn't this module's own `decide_approval`,
  via a private keyword only `decide_approval` passes. Without this, a
  `ticket.write`-only technician could file a request needing approval with
  themselves as requester and self-approve/self-reject it through `POST
  /tickets/{id}/status`, skipping `ticket.approve`, the self-approval
  prohibition above, `approver_role_id` membership, the mandatory rejection
  reason, and both audit events entirely. `pending_approval → cancelled`
  stays reachable through the generic endpoint — withdrawing your own
  request is a legitimate `ticket.write` action, not an approval decision.

---

## 5. Queues & assignment

- Every ticket belongs to exactly one queue. On creation the queue defaults from
  `ticket_category.default_queue_id`; an explicit `queue_id` overrides it. If
  neither resolves, creation fails with a typed validation error rather than
  silently landing the ticket somewhere arbitrary.
- `assigned_to` is nullable. Assigning, unassigning, and moving queue all
  require `ticket.assign`, and all are audited.
- **Assignment is exempt from the `closed` edit freeze** (Task 8 fix round;
  see §3.3's "no field edits and no new comments"). That freeze describes the
  generic ticket-fields PATCH path, which assignment is not: `ticket.assign`
  is its own permission and `ticket.assigned` its own audit action (§8.2,
  §8.3), and reassigning ownership or moving the queue of a closed ticket is
  administrative bookkeeping, not a content edit. A closed ticket may still
  be assigned, reassigned, unassigned, or moved to a different queue.
- `assigned_to` and `requester_id` are resolved org-scoped: a user id from
  another org is indistinguishable from a nonexistent one, both raising the
  module's not-found error (invariant #5).
- No auto-assignment in v1. Round-robin needs an availability/working-hours
  concept and a rebalancing story for absences, and wants tuning against real
  load; it is listed in §14.

---

## 6. Comments & attachments

- Comments are append-only. There is no edit and no delete in v1 — a ticket
  thread is a record of what was said, and an editable thread is worth less as
  evidence. Correction is a further comment.
- `visibility = "internal"` comments are invisible to end users when the portal
  ships. Until then the distinction is recorded but has no external consumer.
- Attachments attach to the ticket, not to a comment. Their storage rules are in
  §2 and are inherited wholesale from Budget's hardened implementation.
- **A ticket in `TERMINAL_STATUSES` accepts no new attachments**, the identical
  freeze §3.3 already places on new comments: an attachment is thread content
  in the same sense a comment is, so a ticket that "accepts no new comments"
  while silently accepting new file uploads would be incoherent, and the
  future end-user portal would otherwise let a requester upload into a
  ticket that is closed, cancelled, or rejected.
- Attachment download is `ticket.read`-gated and audited; upload is
  `ticket.write`-gated and audited. Every other read writes no audit event.

---

## 7. Integration points

### 7.1 Change management (the first real consumer)

`docs/modules/change-mgmt/spec.md` §5 pinned a `ChangeTicketPort` protocol with
a `NullChangeTicketAdapter` that records intent and performs no network call,
explicitly awaiting a real backend. This module provides
`ServiceDeskChangeTicketAdapter`, which opens a genuine internal ticket.

This is **additive**: change management's reviewed core is not modified. The
adapter is selected by the existing configuration switch
(`ITS_CHANGE_TICKET_BACKEND`), and the null adapter remains the default until
the operator opts in, so an existing deployment's behavior does not change
underneath it.

`push_change` creates the ticket with `origin = "integration"` (whole-branch
security review, finding A3; §2's origin bullet), not `"technician"`: the
CREATE act is the system reacting to a change transition, not the change's
author filing a ticket by hand, and the ticket's audit attribution must say
so. The ticket's own `requester_id`/`created_by` still name that author —
only the create action's `actor_type` is `"system"`.

The link is one-directional: a change record may reference the ticket it raised.
A ticket does not own or drive a change record.

### 7.2 Phase 16 (endpoint health & alerting) — BUILT

**Amendment, twice over: this section said "Phase 15" and it said "reserved, not
built". Both are wrong now.** Endpoint health, alerting & auto-remediation is
**Phase 16** on `docs/06-roadmap.md` (Phase 15 is IPAM), and it has shipped —
the alert→ticket loop is implemented and exercised end to end. The two things
this spec reserved are now *in use*:

- `ticket.origin = "alert"`, a declared value pinned at the database by
  `ck_ticket_origin_valid` (migration 0024) and exempted from human attribution
  by `ck_ticket_human_attribution` (migration 0023) — `origin = 'alert' OR
  (requester_id IS NOT NULL AND created_by IS NOT NULL)`, which is what lets an
  alert ticket exist with no human requester without inventing a synthetic user.
- `ticket.source_alert_id`, nullable and still carrying **no** foreign key, for
  the independent-deployability reason above. Its pairing with the origin is a
  service-layer refusal rather than a CHECK: `tickets.create_ticket` raises when
  `source_alert_id` is given with any `origin` other than `"alert"`, and both
  arguments are explicit keywords that are never read off the request body.

**The contract this spec pinned is satisfied verbatim, and the health spec now
owns the detail.** Alert-opened tickets are `type = "incident"`; they are
created with `actor_type="system"`; and recovery drives an **untouched** ticket
to `resolved` — never straight to `closed` — so a human still confirms the
underlying problem is genuinely gone. The full behaviour, including the
human-touched exception (an assignee, a `user`-actor status-history row, or a
`user`-authored comment means the ticket gets a system comment and **nothing
else**), the `new → open → resolved` two-hop the incident lifecycle forces, and
the reporting obligation that follows from auto-resolution, lives in
`docs/modules/endpoint-health-alerting/spec.md` §7.4. Read that section, not
this one, for what the health module actually does.

**One consequence for this module, stated here because it is this module's
data.** §7.4 of the health spec requires machine-resolved and human-resolved
alert tickets to be reported separately, defined on
`ticket_status_history.actor_type='system'` for an `origin='alert'` ticket.
**That is not computable as a report from this module's API**, for two reasons
that are both here and not in the health module:

**Amended 2026-08-22 (UX-closeout recommendation 10).** `GET
/service-desk/tickets` returns `{items, total}` rather than a bare
`list[TicketOut]`. As first built, a caller holding a full page could not tell
a complete queue from a truncated one — the console said exactly that in a
comment and declined to render "showing X of Y", because there was no Y. The
`total` is counted from the same filtered set as `items`, before
`limit`/`offset`, in the shape `GET /jobs`, `GET /patch/approvals` and
`GET /device-groups` already use.

- `GET /service-desk/tickets` has **no `origin` filter** (§9's list takes
  `type`, `status`, `queue_id`, `assigned_to`, `requester_id`, `device_id`,
  `priority`, `q`), so the population "alert-origin tickets in this period"
  cannot be selected at all.
- `GET /service-desk/tickets/{id}/history` **does** exist and **does** return
  `actor_type` — so the fact is reachable one ticket at a time — but there is no
  aggregate, so the split would be one request per ticket over a set the first
  bullet says you cannot enumerate.

The health console therefore approximates the split from the alert's own
`cleared_reason` and says so on the page. Closing the gap is work **here** — an
`origin` filter, or a reporting aggregate — not a change in the health module.

### 7.3 Inventory / devices

`ticket.device_id` links a ticket to a managed device. The ticket detail view
shows that device's inventory summary, patch state, and (later) open alerts
inline. This is the justification for owning ticketing rather than integrating
an external helpdesk, and it is why `device_id` is in the core rather than a
later addition.

Resolution is org-scoped against `devices`, with a real FK.

---

## 8. Security invariants

### 8.1 Tenancy
Every query carries an explicit `org_id` predicate in addition to RLS
(defense in depth, invariant #5). Every cross-reference — `category_id`,
`queue_id`, `requester_id`, `assigned_to`, `device_id`, `approver_role_id` — is
resolved `WHERE org_id AND id`, so a cross-org id is indistinguishable from a
nonexistent one. Cross-org tests run on a raw BYPASSRLS admin session and are
mutation-verified (§12).

### 8.2 RBAC (invariant #6, checked server-side on every route)

| Permission | Grants |
|---|---|
| `ticket.read` | View tickets, comments, attachments, queues, categories |
| `ticket.write` | Create tickets, comment, edit own/assigned ticket fields, upload attachments |
| `ticket.assign` | Assign, reassign, move queue |
| `ticket.approve` | Decide request approvals |
| `ticket.admin` | Manage queues and categories |

Role grants: super-admin and org-admin all five; **technician gets `read`,
`write`, `assign`** (§0.6); auditor gets `read` only; read-only gets `read`.

`approve` is separate from `write` deliberately, following
`change.request`/`change.approve` and `expense.record`/`expense.confirm`. §4's
self-approval prohibition is enforced on top of the permission, not instead of
it.

### 8.3 Audit (invariant #4)
Audited: `ticket.created`, `ticket.assigned`, `ticket.status_changed`,
`ticket.approved`, `ticket.rejected`, `ticket.comment_added`,
`ticket.attachment_uploaded`, `ticket.attachment_downloaded`,
`ticket.queue_created`/`updated`, `ticket.category_created`/`updated`, and the
one-off `ticket.queues_seeded` and `ticket.categories_seeded` (both system
actor, each emitted only when that seed actually inserts rows).

**No audit on view.** Lists, detail, and search write nothing. The only audited
read is an attachment download.

Audit metadata carries identifiers and state, never comment bodies — a comment
may contain anything a user typed, and the audit log is not the place to
duplicate it.

---

## 9. API (routes under `/api/v1`)

```
GET    /service-desk/queues                     ticket.read
POST   /service-desk/queues                     ticket.admin
PATCH  /service-desk/queues/{queue_id}          ticket.admin
GET    /service-desk/categories                 ticket.read
POST   /service-desk/categories                 ticket.admin
PATCH  /service-desk/categories/{category_id}   ticket.admin

GET    /service-desk/tickets                    ticket.read   (filters: type,
                                                status, queue_id, assigned_to,
                                                requester_id, device_id,
                                                priority, q,
                                                scope=mine|unassigned|open|all,
                                                sort=newest|due; returns
                                                {items, total}, each item with
                                                its SLA summary)
POST   /service-desk/tickets                    ticket.write
GET    /service-desk/tickets/{ticket_id}        ticket.read
PATCH  /service-desk/tickets/{ticket_id}        ticket.write
GET    /service-desk/tickets/{ticket_id}/history ticket.read
POST   /service-desk/tickets/{ticket_id}/status ticket.write
POST   /service-desk/tickets/{ticket_id}/resolve ticket.write (reply + resolve)
POST   /service-desk/tickets/{ticket_id}/assign ticket.assign
POST   /service-desk/tickets/{ticket_id}/approve ticket.approve
POST   /service-desk/tickets/{ticket_id}/reject  ticket.approve

GET    /service-desk/tickets/{ticket_id}/comments      ticket.read
POST   /service-desk/tickets/{ticket_id}/comments      ticket.write
GET    /service-desk/tickets/{ticket_id}/attachments   ticket.read
POST   /service-desk/tickets/{ticket_id}/attachments   ticket.write
GET    /service-desk/attachments/{attachment_id}       ticket.read  (audited)

GET    /service-desk/canned-responses                  ticket.read
POST   /service-desk/canned-responses                  ticket.admin (audited)
PATCH  /service-desk/canned-responses/{response_id}    ticket.admin (audited)
DELETE /service-desk/canned-responses/{response_id}    ticket.admin (audited)
```

- **Queue additions (2026-09-23).** Every `TicketOut` carries an optional `sla`
  summary (both deadlines, the SLA engine's met/breached/paused verdicts, and
  the next deadline still owed), loaded in one query per page. `scope` narrows
  server-side to open tickets that are the caller's, nobody's or anyone's
  (`mine` is always the authenticated user; combining it with another
  `assigned_to` is a 400). `sort=due` orders by the next owed deadline,
  tickets without one last. All three are optional; existing clients see no
  change.
- **Reply & resolve (2026-09-23).** `POST .../resolve` takes an optional
  `reply {body, visibility}` and, in one transaction, posts it and walks the
  ticket to `resolved` (incident) or `fulfilled` (request), opening a `new`
  ticket first (`lifecycle.RESOLVE_PATHS`). Each step is the ordinary
  comment/transition service call with its own audit and outbox events; any
  refusal rolls all of it back. Not offered from `pending_approval`.
- **Canned responses (migration 0130).** `ticket_canned_response`, one
  org-scoped table under forced RLS, titles unique per org ignoring case, at
  most 500 per org. Audit events carry the title only.

- `org_id` and `actor_id` always come from `AuthContext`, never from the body or
  path.
- Errors translate by **type**, never by matching message text:
  `TicketNotFoundError` (a subclass) → 404, every other `TicketValidationError`
  → 400. No path returns a raw 500.
- Route ordering: `/service-desk/attachments/{attachment_id}` is declared before
  any `/service-desk/{id}`-shaped route, and static segments precede
  `{ticket_id}` — the Phase-11/12 lesson.
- `PATCH` bodies use `model_dump(exclude_unset=True)`, so omitted and
  explicitly-null are distinguishable; explicit null on a non-nullable column is
  rejected as a typed 400 before any mutation is applied.

---

## 10. Worker

None. Control-plane only: no Celery task, no beat entry, no agent capability, no
gateway registration, no new compose service, no new required environment
variable. Nothing here touches the agent idle budget.

Auto-close-after-timeout is deliberately **not** in this sub-project, because it
would require a scheduled sweep. It arrives with the SLA engine, which needs a
scheduler anyway.

---

## 11. Frontend (`frontend/src/modules/service-desk/`)

Surfaces, all reachable from a shared module nav and gated on `ticket.read`:
- **Ticket list** — a scope switch (My tickets / Unassigned / All open / All,
  default All open), a Due column showing the next SLA deadline relative to
  now, sorted soonest first by default, and filters for type, status, queue,
  assignee, priority and origin. (Amended 2026-09-23: the earlier default of
  "open tickets assigned to me, plus unassigned" was one checkbox below six
  filters, and could not answer "which ticket breaches next?".)
- **Ticket detail** — a header with status, priority, both SLA clocks, the
  requester (linked to their Person page), the linked device with Remote in,
  and quick actions: Assign to me, Reply, Add internal note, Resolve (a new
  incident is opened first, as §3.1 requires). The reply box defaults to an
  internal note when the ticket has no requester, offers saved replies, Reply
  & resolve, and article suggestions for `docs.read` holders. Fields, status,
  queue and assignee are one form with one Save. Attachments, status history,
  the approval panel for requests and the linked device panel (§7.3) follow.
- **My work** — my open tickets, across queues, soonest deadline first.
- **Admin** — queues, categories and saved replies; changes gated on
  `ticket.admin`.

Conventions carried from prior modules: strict TypeScript with no `any`; badge
variant maps use only vocabulary the backend emits with a `?? "muted"` fallback
and a comment citing the source; every mutation hook's `invalidateQueries` keys
match keys actually registered; permission gating is presentational only.

**Every page must be reachable from navigation**, with a test asserting both the
links' presence and their absence without `ticket.read`. Phase 12 shipped two
routed-but-unlinked pages; that is not repeated.

---

## 12. Testing (test-first, security paths explicit)

Required, beyond ordinary coverage:

1. **Ticket numbering concurrency** — two concurrent creates in one org get
   different numbers. Deterministic interleave using the repo's established
   technique (intercept one session's statement, have a second commit inside the
   window), not a hopeful race.
2. **Invalid transitions rejected** — for both lifecycles, asserting the
   transitions that must fail, including every attempt to leave a terminal
   state. Testing only the happy path leaves the state machine unproven.
3. **Self-approval rejected** — the requester holding `ticket.approve` still
   cannot approve their own request.
4. **Cross-org, load-bearing and mutation-verified** — another org's ticket,
   queue, category, or attachment never appears. Raw `AsyncSession(admin_engine)`,
   with the other org's data seeded *inside* the test so the guard holds in
   isolation, then mutation-verified by deleting the predicate and confirming a
   real failure.
5. **RBAC completeness derived from the router** — `declared == covered` set
   equality, so a route added later without a permission test fails the suite.
6. **Technician access is asserted positively** — technician *can* read, write,
   and assign, and *cannot* approve or administer. The inverse of Budget's
   defining assertion, and equally load-bearing.
7. **Audit split** — a list call writes nothing; an attachment download writes
   exactly one event; a status change writes exactly one.
8. **Attachment filename sanitization** — unit-tested directly, not through the
   API client. The in-process ASGI harness does not run h11, so an HTTP-level
   test cannot observe a header-poisoning filename; Budget learned this the
   expensive way.

Any list-equality assertion on a query result requires an explicit `ORDER BY` in
that query.

---

## 13. Documentation changes made with this spec

Edited in the same commit as this spec, so no document contradicts another:
- `docs/00-vision.md` — the "not a full ITSM suite" non-goal and the
  "Ticketing: Odoo Helpdesk" integration line are rewritten to record that the
  platform now owns ticketing, and why (§0.1). The reversal is marked as a
  revision rather than silently overwritten, so the history of the decision
  stays legible.
- `docs/06-roadmap.md` — Service Desk added as Phase 14 with its five
  sub-projects and their order; IPAM, endpoint health, and the AI admin agent
  renumbered to 15/16/17 accordingly, including the note headings that
  referenced them. The Phase 9 row's "integrates Odoo Helpdesk" is corrected to
  say the port is repointed here.

**Deliberately not yet edited:** `docs/modules/change-mgmt/spec.md` §5, which
describes the Odoo adapter as pinned-and-deferred. It is amended in the commit
that actually builds `ServiceDeskChangeTicketAdapter` (§7.1) — amending it now
would make a shipped module's spec describe code that does not exist, which is
the same defect as leaving it stale, only in the other direction.

---

## 14. Deferred (explicitly not in v1)

**Sibling sub-projects, already sequenced:**
1. **Notification core** — outbound webhook + SMTP, per-org config, credentials
   in the vault, retry and failure handling. Shared infrastructure; alerting
   needs it too. Also owns **user-to-queue membership** (task 13's G2 fix,
   §11) — deferred here rather than added to this module as a join table with
   only a list-default consumer, since routing needs the same concept and
   should get to choose its shape.
2. **End-user portal** — a second auth surface, its own permission tier, its own
   UI, and tenancy guaranteeing a user sees only their own tickets.
3. **Email intake** — inbound receiver, MIME parsing, reply threading, loop and
   bounce protection, per-org mailbox credentials.
4. **SLA engine** — response/resolution targets, business calendars, pause on
   `pending`, breach escalation, and the scheduled sweep that auto-close also
   needs.

**Not currently sequenced:** round-robin auto-assignment; CSAT; knowledge base
and suggested articles; time tracking and billing; merge/split/parent-child
tickets; saved views and bulk actions; ticket templates; a public status page.
