# Module Spec — End-user portal

> Third of five sub-projects that together replace Odoo Helpdesk as this
> platform's ticketing system (`docs/modules/service-desk/spec.md` §14). Scoped
> from scratch during brainstorming — no scoping brief preceded this spec, so
> §0 carries more weight than usual.
>
> Control-plane only — no agent capability, no new job type, no signed-job path.
>
> **This module adds no business logic.** It is a second, tightly-scoped
> *surface* onto the ticket core's existing services. If a rule about tickets
> needs changing, it changes in `app/modules/service_desk/`, not here.

---

## 0. Resolved design decisions

Three were settled in brainstorming, plus one defect this sub-project inherits
and fixes. Changing any of them invalidates a section, so they are recorded
before the design.

### 0.1 Staff IdP now; one function is the seam for external users later

Portal users in v1 are Toeshee staff already present in the org's OIDC identity
provider, holding a requester-tier role instead of a technician one. No new
credential store, no password reset, no second login page — the IdP stays the
single gate.

The spec's "second auth surface" (`service-desk` §14) is therefore delivered as
**a second permission tier plus a separate UI**, not a second authentication
system.

External end users — a managed-service provider's clients, who are not in
Toeshee's IdP — are a real future requirement. The seam for them is exactly one
function, `resolve_portal_principal(request) -> PortalContext` (§3). Adding an
external credential store later means writing a second implementation of that
one signature. **No abstraction beyond that is built now**: a general-purpose
pluggable-auth framework for a case that does not yet exist would be
speculative, and the tenancy design (§2) is deliberately independent of how a
principal authenticated.

### 0.2 Row-level tenancy is enforced by a separate Postgres role, fail-closed

"A requester sees only their own tickets" is a **new access shape** for this
platform. Every existing tenancy mechanism is org-scoped RLS; this one is
row-scoped *within* an org.

A new `portal_user` database role carries its own policies, keyed on **two**
GUCs:

```sql
USING (org_id     = current_setting('app.current_org',       true)::uuid
   AND requester_id = current_setting('app.current_requester', true)::uuid)
```

This is fail-closed by construction. If either GUC is unset, `current_setting`
returns NULL, every comparison is NULL rather than true, and **no rows match**.
A bug that forgets to set the requester context yields an empty portal, not
someone else's tickets.

Two alternatives were rejected:

- **One conditional policy shared by staff and portal**
  (`... AND (requester GUC IS NULL OR requester_id = requester GUC)`). No new DB
  role, but it fails **open**: any code path that forgets to set the requester
  GUC on a portal session shows that user every ticket in the org. The failure
  is silent and total.
- **Application-layer filtering only.** Simplest to read, but invariant #5 calls
  a missing tenant filter a security bug rather than a style nit — and this
  project has already shipped a tenancy predicate that passed its test for the
  wrong reason because an upstream check masked it.

Staff policies on `app_user` are **not modified**. This module adds policies; it
does not alter existing ones.

### 0.3 A requester can raise, view, reply, and attach

The standard helpdesk triad plus attachments — a screenshot of the error is most
of what makes a ticket actionable, and the upload path is already hardened
(§6.3). Approval **outcome** is also exposed, closing a follow-up carried since
the ticket core: `ticket_approval.decided_by`/`decided_at`/`reason` had no read
path anywhere, and a requester whose request was rejected should be able to see
that it was, and why.

### 0.4 The resolution email's instruction is currently false, and this module fixes it

Notification core's resolution email says *"If you have further questions, reply
to this ticket to reopen it."* Inbound email intake is sub-project **4** and does
not exist, so a requester who replies gets silence. The copy instructs an action
the system cannot perform.

This module changes that copy to point at the portal, which is an action that
will actually work. When email intake ships it may revise the wording again.

---

## 1. Purpose & boundary

Let the person who reported a problem see what is happening to it, and add to it,
without giving them any view of the fleet, of other people's tickets, or of
internal staff discussion.

**In scope:** the `portal_user` role and its policies (§2), the auth seam (§3),
the requester permission tier (§4), the portal API (§5), reuse rules for ticket
core's services (§6), the portal UI (§7), tenancy/audit (§8), tests (§9).

**Not in this sub-project:** inbound email intake and the SLA engine, each its
own sub-project. Also deferred (§11): per-requester attachment quotas,
self-service ticket reopen, CSAT, a knowledge base, and any external credential
store.

**Boundary vs the ticket core.** This module owns no ticket rules. It calls
`tickets.create_ticket`, `threads.add_comment`, `threads.add_attachment` and the
existing read services under a portal-scoped session. It adds **no** new
lifecycle, no new status, and no second definition of who may do what to a
ticket. A behaviour change belongs in the ticket core, where its tests live.

**Boundary vs notification core.** Unchanged and unaffected, with one exception:
the email copy in §0.4. The portal is not the first end-user-facing surface —
requester email already ships — so this module makes the existing notice useful
rather than introducing end-user contact.

**What a portal principal must never reach.** The device fleet, jobs, other
people's tickets, internal comments, queues, other users' identities, or any
route outside `/api/v1/portal`. `ticket.read` is org-wide and a requester must
never hold it (§4).

---

## 2. The `portal_user` role and its policies

The security core of this module. Migration `0028`.

`portal_user` is a new `NOLOGIN`-capable application role with the **minimum**
table privileges below, then RLS policies on top. Both layers matter: a policy
cannot restrict a table the role has no grant on, and a grant without a policy
would expose the whole org.

### 2.1 Per-table access

Not every table takes the requester predicate — that is the subtlety of this
section. A requester needs to *choose a category* when filing, and categories
are org refdata, not requester-owned.

| Table | Privileges | Policy predicate |
| --- | --- | --- |
| `ticket` | SELECT, INSERT | org **and** `requester_id` = requester GUC |
| `ticket_comment` | SELECT, INSERT | org **and** parent ticket is the requester's **and** `visibility = 'public'` |
| `ticket_attachment` | SELECT, INSERT | org **and** parent ticket is the requester's (RLS policy, unchanged) — **but see the amendment below: the portal's own read query narrows further, to `uploaded_by` = requester, since this table carries no visibility column** |
| `ticket_status_history` | SELECT, INSERT (migration `0030`) | org **and** parent ticket is the requester's |
| `ticket_approval` | SELECT, INSERT (migration `0030`) | org **and** parent ticket is the requester's |
| `ticket_category` | SELECT | org only, `active = true` — needed to file a ticket |
| `ticket_number_counter` | SELECT, INSERT, UPDATE | org only — `create_ticket` allocates a number. **INSERT is required**: `numbering.py` creates the counter row lazily with `on_conflict_do_nothing` before its atomic `UPDATE ... RETURNING`, so without INSERT the *first* portal ticket in an org (or the first of a type) fails. |
| `ticket_queue` | SELECT (migration `0029`) | org only. **Amended:** the original "none" was wrong. A requester never *chooses* a queue, but `ticket.queue_id` is NOT NULL and `tickets.create_ticket` resolves it via `refdata.resolve_queue`, which issues `select(TicketQueue)` — with no grant that raises `permission denied` and every portal ticket creation fails with a 500. Read-only, no requester predicate; a requester still never *sees* a queue, because §5's response models carry no queue field. |
| `users` | SELECT (migration `0030`) | org (`tenant_isolation`) **and** `id` = requester GUC (new `portal_isolation` policy) |
| `audit_events` | SELECT, INSERT (migration `0030`) | org only (`tenant_isolation`) for SELECT — **no** requester predicate (the hash-chain lookup must see every actor's events); INSERT additionally carries a restrictive `actor_id = requester GUC` `WITH CHECK` (migration `0031`) so a portal connection can write an event only as itself, never attributing one to another actor |
| `domain_events` | INSERT (migration `0030`) | org only (`tenant_isolation`) — **no** requester predicate |
| everything else | **none** | — |

**Amendment — `ticket_attachment` has no author/visibility scoping at the RLS
layer, and the portal's own query is what closes that (finding I1).** Unlike
`ticket_comment`, `ticket_attachment` carries no `visibility` column, so the
RLS policy above (parent-ticket-is-mine) admits a STAFF-uploaded attachment
on the requester's own ticket exactly as readily as one the requester
uploaded themselves — a whole-branch review found this concretely: `GET
/portal/tickets/{id}/attachments/{attachment_id}` served a staff-only file
verbatim. The fix scopes the portal's own read (both the download route and
`PortalTicketDetail.attachments`, §5/§7) to `uploaded_by` = the requester's
own id — a requester may retrieve only what THEY attached, never something
staff attached to their ticket. Staff-to-requester file sharing (the
mirror-image want: staff deliberately attaching something FOR the requester
to see) is a real product need, but it needs `ticket_attachment` to grow a
real visibility column and a deliberate design, matching how `ticket_comment`
got one — deferred, §11, not invented here.

**Migration `0030` — closing the write-path grant gap.** Migration `0028`
gave `portal_user` enough to create/read a `ticket`/`ticket_comment`/
`ticket_attachment` row directly, but the ticket-core functions the portal is
*required* to delegate to (§6.1) — `tickets.create_ticket`,
`threads.add_comment`, `threads.add_attachment` — each also unconditionally
touch `users` (resolving the requester), `audit_events` (every one of the
three calls `audit_service.record`), and `domain_events` (`create_ticket`
when the category requires approval; `add_comment`/`add_attachment`
unconditionally, via `publish()`), and insert into `ticket_status_history`/
`ticket_approval`, none of which `0028` granted. Without those grants the
ticket core's real write path is not merely undesirable to call from a
`portal_session` connection — it is impossible, so the first implementation
of this module (task 4) verified ownership on the portal connection and then
opened a *second*, `org_session`-scoped connection to perform the actual
write. That satisfied "not-mine is 404" but meant portal writes were enforced
by application code, not the database — exactly what §0.2 rejected.
Migration `0030` grants the minimum the ticket core's write paths actually
need, determined empirically (run the write path on a `portal_session`
connection, let each `permission denied` error name the next grant, stop once
every write succeeds) rather than re-derived from a description:

- **`users`: SELECT, plus a restrictive `id = requester GUC` policy.** A
  plain SELECT grant would let a requester read every colleague's name and
  email in the org (the existing `tenant_isolation` policy is org-scoped,
  not row-scoped) — a new leak §0.2's invariant #5 discipline forbids. The
  restrictive policy is what limits it to the caller's own row, verified
  with a second real user present in the same org.
- **`audit_events`: SELECT *and* INSERT — not INSERT-only, which is what was
  expected going in.** `audit_service.record` SELECTs the latest hash in the
  org's chain before computing the next link; INSERT alone still raised
  `permission denied`, because that SELECT runs first. No portal policy is
  added: the hash-chain SELECT must see the true latest event in the org
  regardless of which actor wrote it, or a portal-authored link would
  record the wrong `prev_hash` and break chain verification the moment a
  staff-authored and a portal-authored event interleave in the same org.
  This is the same reasoning `ticket_category`/`ticket_queue`/
  `ticket_number_counter` already established: some operational/structural
  data is legitimately org-scoped-only for a portal write to consult, with
  no additional requester predicate. UPDATE and DELETE remain revoked
  (invariant #4).
- **`domain_events`: INSERT only.** `publish()` only ever appends; nothing
  in the portal's call paths reads it back, so no SELECT is granted.
- **`ticket_status_history`/`ticket_approval`: INSERT added to the existing
  SELECT.** Both already carried a `portal_isolation` RESTRICTIVE policy
  (migration `0028`) with a `USING` clause and no explicit `WITH CHECK`.
  Verified empirically, not assumed: Postgres uses `USING` for the
  write-side check too when a policy applies to INSERT/UPDATE and defines no
  separate `WITH CHECK` — a direct cross-requester INSERT into either table
  is refused with `new row violates row-level security policy
  "portal_isolation"`. No new policy was needed for these two tables.

### 2.2 `WITH CHECK` is where the write rules live

Three writes are possible from a portal principal, and each is constrained by
`WITH CHECK` so the database refuses a malformed one even if application code
were wrong:

- **Ticket insert:** `WITH CHECK (requester_id = requester GUC)`. A requester
  cannot file a ticket naming somebody else as the requester — which would
  otherwise be a way to write into another person's portal view.
- **Comment insert:** `WITH CHECK (visibility = 'public' AND <parent is mine>)`.
  A requester cannot create an internal note. Without this, the portal would
  hold a write path into staff-only discussion.
- **Attachment insert:** `WITH CHECK (<parent is mine>)`.

### 2.3 Comment visibility is enforced in the policy, not the query

`ticket_comment`'s SELECT policy carries `visibility = 'public'`. The portal's
queries will also filter it, but the policy is what makes an internal-note leak
**structurally impossible** rather than merely unlikely: a future portal query,
a new endpoint, or a careless join cannot expose an internal note, because the
role cannot see the row at all.

Disclosing internal staff discussion about a person *to that person* is the
worst thing this module could do. It gets the strongest available mechanism.

### 2.4 No new tables

This module adds **no** tables and **no** columns. It is a role, a set of
policies, a permission tier, an API surface, and a UI.

---

## 3. The auth seam

```python
async def resolve_portal_principal(request: Request) -> PortalContext
```

`PortalContext` carries `org_id` and `requester_user_id` — deliberately **not**
an `AuthContext`, so a portal principal cannot be passed to a staff dependency
by mistake.

v1 resolves it from the existing OIDC path, additionally requiring **a portal
permission** (`portal.ticket.read` or `portal.ticket.write`), not `requester`
role membership specifically. The two are not the same test: those permission
codes are *also* granted to `super-admin`/`org-admin` (§4, migration `0028`),
because a pre-existing invariant test requires every admin to hold every
permission code in the system — there is no way to seed a new permission
without granting it to admins too. Gating on the permission is therefore one
authorization concept rather than two (role-check *and* permission-check), and
it is deliberately not treated as a hole: **an admin can use the portal**, and
when they do, they are confined by the database to their own row exactly like
any other requester — there is no admin carve-out in the `portal_user` RLS
policy predicate (§2). Auto-provisioning of an unknown `sub` is **kept**: an
unrecognised person gets a `User` row with no roles and therefore no access,
which is a clean 403 rather than a 500. Note explicitly that having a `User` row
is not access; the permission grant is.

**`portal_session`** mirrors `app.core.tenancy.org_session` but connects as
`portal_user` and sets both GUCs:

```sql
SELECT set_config('app.current_org',       :org,       true);
SELECT set_config('app.current_requester', :requester, true);
```

It must be the **only** way a portal request reaches the database. A portal
route that opened an `org_session` would run as `app_user` and see the whole
org — §9 requires a test that no portal route can do this.

---

## 4. The requester permission tier

A new role `requester`, seeded in migration `0028`, holding exactly:

- `portal.ticket.read` — view my own tickets and their public history
- `portal.ticket.write` — file a ticket, comment on mine, attach to mine

These are **new permission codes**, not reuses. `ticket.read` is org-wide
("view tickets, comments, attachments, queues, and categories") and granting it
to a requester would defeat the entire module regardless of RLS, because it also
gates staff routes. A test asserts the `requester` role holds **no** `ticket.*`
permission and no permission outside the `portal.*` namespace.

Every portal route is gated server-side on one of the two codes (invariant #6).
RBAC completeness is derived from the router, as in the notification module, so a
portal route added later without a permission fails the suite.

---

## 5. API

Under `/api/v1/portal`, versioned per convention. Literal segments are declared
before `{id}` parameters — the lesson from Phases 11/12 and the ticket core.

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/portal/categories` | `portal.ticket.read` |
| GET | `/portal/tickets` | `portal.ticket.read` |
| POST | `/portal/tickets` | `portal.ticket.write` |
| GET | `/portal/tickets/{id}` | `portal.ticket.read` |
| POST | `/portal/tickets/{id}/comments` | `portal.ticket.write` |
| POST | `/portal/tickets/{id}/attachments` | `portal.ticket.write` |
| GET | `/portal/tickets/{id}/attachments/{attachment_id}` | `portal.ticket.read` |
| GET | `/portal/help/suggest?q=` | `portal.ticket.read` |
| GET | `/portal/help/articles/{id}` | `portal.ticket.read` |

**Help articles (added 2026-09-23, operator-approved).** The two `help` routes
serve documentation articles staff marked "visible in the portal"
(`doc_articles.portal_visible`, default off) that are not archived. Suggestions
return `id`/`title`; an article returns `id`/`title`/`body` only. `portal_user`
gets a column-level SELECT on `doc_articles` and a restrictive
`portal_isolation` policy (`portal_visible AND NOT archived`), and no grant on
`doc_links`/`doc_versions`, so an internal article is a 404 whatever id is asked
for. The new-ticket form shows matching articles as the subject is typed.

`GET /portal/tickets/{id}` returns the ticket, its **public** comments, its
status history (**WITHOUT the `reason` text** — see below), its **own**
attachments, and its approval outcome when one exists (§0.3).

**A ticket that is not the requester's returns 404, never 403.** A 403 confirms
the id exists, which is a cross-requester existence oracle. The two cases must be
indistinguishable — the same discipline the ticket core had to fix for
`default_queue_id`.

**No portal response may contain** an internal comment, another user's identity
beyond a display name on a public comment, a queue, an assignee, or any field
whose value is staff-only. Response models are portal-specific and narrow; they
do not reuse the staff `TicketOut`.

**Status history is returned WITHOUT `ticket_status_history.reason` — resolving
a contradiction in this section, deliberately.** The line above ("returns …
its status history") and the "no field whose value is staff-only" rule directly
above it conflict for this one column, and nothing in the original version of
this spec resolved that conflict — a whole-branch review (`.git/sdd/portal-
final-review.md`, finding C1) found it delivering a technician's private
reason text about the requester directly to that requester (e.g. `reason=
"INTERNAL: repeat offender, see HR case 42"`), through a route this spec
itself defines. `ticket_status_history.reason` is unbounded staff free text
with **no visibility column** — unlike `ticket_comment`, which has one
(`visibility`) and an RLS predicate that makes an internal note structurally
unreadable regardless of any application bug (§2.3). There is no way to tell
a customer-safe reason from a staff-only one on that column today, so
`PortalStatusHistoryOut` **omits `reason` entirely**: a requester sees THAT
their ticket moved from one status to another, and WHEN — never WHY, in
staff's own words. This is the module's existing posture ("narrow response
models; internal content never leaves the service desk") applied consistently,
not a new rule. A customer-visible explanation of a status change is a real
future want, but it needs its **own** field with **explicit** visibility,
mirroring how `ticket_comment.visibility` was built deliberately rather than
inferred — deferred, §11, not invented here. (`ticket_approval.reason` is
NOT affected by this: §0.3 made that a deliberate, explicit trade — a
rejected requester should see why — and it is exposed as designed.)

---

## 6. Reuse rules

### 6.1 Services are called, not reimplemented

Ticket creation goes through `tickets.create_ticket`; comments through
`threads.add_comment`; attachments through `threads.add_attachment`. A second
implementation of "how a ticket is created" is how the two surfaces drift, and
the ticket core's tests are the ones that pin that behaviour.

**Portal writes run natively under `portal_session`, on a single connection —
no second, elevated connection (migration `0030`, §2.1).** `create_my_ticket`,
`add_my_comment`, and `add_my_attachment` call the ticket core's own write
functions directly on the caller's `portal_session`-opened session; there is
no `org_session`/`app_user`-scoped hop for the delegated write. The three
functions still verify ownership first (`_load_my_ticket`, spec §5's
"not-mine is 404") — that check remains as belt-and-braces documentation of
intent — but it is no longer the only thing standing between a requester and
a ticket that is not theirs: the database's own grants and policies refuse a
cross-requester write even if that check were skipped or wrong, which is the
property §0.2 chose a separate `portal_user` role to get in the first place.

### 6.2 What the portal must pass explicitly

`requester_id` and `created_by` come from `PortalContext`, never from the request
body — the same rule staff routes follow for `org_id`/`actor_id`.

### 6.3 Attachments inherit the hardened path

The ticket core's upload route already: bounds the declared `Content-Length`
**and** the bytes actually read; derives the object-storage key entirely
server-side with no client-supplied segment; and verifies the ticket exists and
is in scope **before** buffering any body. The portal reuses that ordering
exactly. Per-requester quota is deferred (§11) — per-request size is already
bounded.

---

## 7. Frontend

A separate route namespace and its own minimal surface, not a variant of the
technician UI:

- **My tickets** — list with status, last update, ticket number.
- **Ticket detail** — status, history, public comment thread, attachments,
  approval outcome when present, and a reply box.
- **New ticket** — category, subject, description, optional attachment.

Strict TypeScript, no `any`; TanStack Query; conventions carried from prior
modules. The UI shows a requester nothing about queues, assignees, or internal
notes — and the server would refuse to serve them regardless, because the UI
never gates security.

---

## 8. Tenancy and audit

**Tenancy** (invariant #5): §2. The portal's row scoping is a database
guarantee, not an application convention.

**Audit** (invariant #4): a portal ticket creation, comment, and attachment are
privileged actions and are audited exactly as the staff equivalents are — the
existing services already write those events, with the actor being the requester.
No audit on reads.

**RBAC** (invariant #6): §4, server-side on every route, router-derived
completeness test.

**Credentials** (invariant #3): unaffected — this module touches no credential.

---

## 9. Testing requirements

Test-first per `CLAUDE.md`. These are named because each corresponds to a
specific way this module could fail silently:

1. **Fail-closed on unset context** — every portal read returns empty when the
   requester GUC is unset. This is the assertion that proves §0.2's central
   claim rather than assuming it.
2. **Cross-requester isolation** — requester A cannot read, comment on, or
   attach to requester B's ticket, in the same org. Must fail if the policy's
   requester predicate is removed.
3. **Internal notes are invisible** — a ticket with both a public comment and an
   internal note returns only the public one, asserted on the response. Must
   fail if only the query filter (not the policy) is removed.
4. **A requester cannot write an internal note** — an attempt is refused by the
   database, not merely by the API.
5. **A requester cannot file for someone else** — an insert naming another
   `requester_id` is refused.
6. **Not-mine is 404, not 403** — no existence oracle.
7. **No portal route opens an `org_session`** — enforced by inspecting the
   module, so a future route cannot silently run as `app_user`.
8. **The `requester` role holds no `ticket.*` permission** and nothing outside
   `portal.*`.
9. **Router-derived RBAC completeness** for all portal routes.
10. **Staff behaviour is unchanged** — the existing service-desk suite passes
    untouched. Any pre-existing test that changes behaviour is a defect in this
    module, not a test to update.

---

## 10. Migration notes

Migration `0028`, forward-only: create the `portal_user` role, grant the minimum
privileges in §2.1, create its policies, and seed the `requester` role with the
two new permission codes. No table or column is added or altered.

`backend/tests/test_rls.py` auto-covers every table with an `org_id` column for
`ENABLE + FORCE` and a `tenant_isolation` policy; the new portal policies are
**additional** policies on existing tables and must not disturb that.

---

## 11. Deferred (explicitly not in v1)

- **An external credential store** for non-IdP end users — §0.1's seam is the
  extension point; the store itself is a separate piece of work.
- **Per-requester attachment quota.** Per-request size is bounded; cumulative
  storage per requester is not. A busy or hostile requester can accumulate.
- **Self-service reopen.** Deliberately not built: reopen semantics belong with
  email intake (sub-project 4), which needs reply-threading anyway, and with the
  SLA engine's auto-close.
- **CSAT, a knowledge base, suggested articles** — not sequenced.
- **Ticket withdrawal/cancellation by the requester** — plausible, but it is a
  lifecycle change and lifecycle belongs to the ticket core.
- **A customer-visible status-change reason.** §5 now returns status history
  WITHOUT `ticket_status_history.reason` (finding C1): that column is
  unbounded staff free text with no visibility column, so it cannot be shown
  to a requester safely today. A genuinely customer-facing explanation of a
  status change is a real want, but it needs its own field with **explicit**
  visibility — the same design `ticket_comment.visibility` already is — not
  a reuse of the staff-only column.
- **Staff-to-requester attachment sharing.** §2.1's amendment scopes the
  portal's attachment reads to `uploaded_by` = the requester (finding I1):
  a requester can retrieve what they sent, never what staff attached. Staff
  deliberately sharing a file *with* a requester is a real want, but
  `ticket_attachment` needs a real visibility column and a deliberate design
  first, mirroring how `ticket_comment` got one.
- **A `SECURITY DEFINER` audit hash-chain-head function**, so `portal_user`'s
  SELECT on `audit_events` can be revoked entirely (currently org-wide, by
  design — the hash-chain lookup must see every actor's events; migration
  `0031` restricts the INSERT side to the caller's own `actor_id` but does
  not touch the SELECT). Tracked as a follow-up, not built here.
- **Replacing `route_permission`'s qualname/closure reflection** (`app/core/
  rbac.py`) with an explicit `__its_permission__` marker attribute on the
  dependency closures. The reflection works and is tested today; the marker
  would remove the "core RBAC edits a frozenset every time a new principal
  type is added" pattern, but that is a mechanism change to shared code, not
  a portal-module change, and is tracked as a follow-up.

---

## 12. Related documents

- `docs/modules/service-desk/spec.md` — ticket core; §14 sequences this
  sub-project, §6 defines the public/internal comment distinction this module is
  the consumer of.
- `docs/modules/notification/spec.md` — §0.4's email copy is amended by this
  module.
- `docs/03-security-model.md` — the invariants this spec applies.
- `docs/06-roadmap.md` — Phase 14's five sub-projects.
