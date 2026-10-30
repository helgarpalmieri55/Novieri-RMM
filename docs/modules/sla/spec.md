# Module Spec — SLA engine

> Fifth and final sub-project of the Service Desk module
> (`docs/modules/service-desk/spec.md` §14 item 4), which scopes it as
> "response/resolution targets, business calendars, pause on `pending`, breach
> escalation, and the scheduled sweep that auto-close also needs."
>
> Control-plane only — no agent capability, no new job type, no signed-job path.
>
> **This module adds no status and no transition.** It calls the ticket core's
> existing services. It owns *when a promise is due and whether it was kept*,
> and nothing else about a ticket's lifecycle.

---

## 0. Resolved design decisions

Six decisions settled in brainstorming. Each rules out a family of designs, so
they are recorded before the design rather than inside it.

### 0.1 Business calendars are real: working hours, holidays, one timezone

The clock advances only inside a calendar's working windows. "4 business hours"
filed at 16:00 Friday is due Monday, not Saturday morning.

Rejected: **elapsed wall-clock only**. It is trivial to build and dishonest to
sell — an SLA engine that cannot express "business hours" acquires that
requirement within a month, and retrofitting a calendar into a clock that
assumed elapsed time means rewriting every stored deadline.

Deferred (§13): **per-customer calendar assignment**. Tiering ("gold gets
24/7") needs an end-customer entity this platform does not have — tickets
anchor to an org and a queue. Inventing that model here would be this module
defining shared ticket structure, which §1 forbids. The schema below is shaped
so per-queue calendar assignment is additive.

### 0.2 A policy is selected by `(queue, priority)`

The ticket core states that the queue is "the natural anchor for the SLA rules"
(its §2). Priority is the other axis every service-desk product uses, and the
two together are what a team actually owns a promise at.

Rejected: **priority alone** (a Facilities ticket and a production outage at
`high` would promise the same response) and **category + priority** (categories
are finer-grained and change more often; an org would maintain a row per
category with most rows identical).

### 0.3 A priority change recomputes the deadline from ticket start

A ticket filed `normal` (8h) at 09:00 and re-triaged to `urgent` (1h) at 14:00
is **already breached**. The work was urgent all along and nobody treated it
that way; the breach is information, not an artifact.

This choice has a real cost, stated plainly because designing around it later
is harder than accepting it now: **it creates a perverse incentive.** If
escalating instantly marks a ticket breached, people learn not to escalate, and
the triage signal is lost to protect a metric.

It is still correct, because the alternatives fail worse and less visibly.
Freezing the policy at creation lets a misfiled priority permanently cap the
promise — file everything `low` and never breach. Recomputing from the moment
of change has the same hole with extra steps: miss the target, bump priority,
get a fresh clock, repeat.

The mitigation is attribution, not a softer clock. `ticket_sla.policy_source`
records **why** the current policy applies (§2.4), so "breached because we were
slow" and "breached retroactively because we re-triaged at hour five" are
distinguishable in reporting. The second reads as a triage-quality problem
rather than a response-time failure.

### 0.4 A breach-escalation priority change does NOT recompute

§0.3 and breach escalation (§0.5) compose into a loop if left alone. A `normal`
ticket (8h) breaches at hour 8; escalation raises it to `high` (4h); §0.3
recomputes from creation; the ticket is 8h old against a 4h target, so it
breaches again *instantly*; escalation raises it to `urgent`; it breaches
again. It terminates only because `urgent` is the ceiling — after firing three
escalations and three breach notifications in the same second.

Therefore: a priority change **originating from breach escalation** is marked
system-originated and does not recompute. Human re-triage still recomputes
exactly as §0.3 requires — that is the case the rule exists for. A system bump
is not new information about the work; it is the engine reacting to its own
output.

This costs nothing extra: §0.3 already requires recording *why* a policy
applies, and `policy_source = 'escalation'` is that same field.

### 0.5 Breach mutates the ticket, and publishes an event rather than sending mail

On breach the module publishes `ticket.sla_breached` to the existing
transactional outbox and, if the policy enables it, escalates priority and/or
moves the ticket to an escalation queue.

Delivery is **not this module's job**. The notification module already owns
channels, routing rules, recipients, retries and the egress guard. This module
ships no mail, no webhook, and no channel config.

### 0.6 The response clock stops at the first public comment from someone who is not the requester

A response SLA is a promise to the person waiting, and this is the only
candidate they can perceive.

Rejected: **first assignment** and **first status change out of `new`**. Both
stop the clock on activity the requester cannot see, which turns the metric
into a measure of internal bookkeeping — and once that is true the number stops
meaning anything to the customer it was promised to.

An internal note does not stop the clock: `ticket.internal_note_added` is a
distinct event type precisely so staff discussion cannot be mistaken for
customer contact. **Resolution also stops the response clock** — a ticket
resolved outright with no reply (a duplicate, say) cannot owe a response
forever.

#### 0.6.1 Amendment (Task 6): a machine-generated public note does not stop the clock

A public comment with no human author (`author_type="system"`, `author_id`
`NULL` — `ck_comment_author` enforces that pairing at the database layer) does
**not** stop the response clock, even though it is visible to the requester.

The realistic producer is the change-management adapter: it posts a public
"Change X is now Y" note onto every ticket linked to a change
(`service_desk.adapters`). A machine broadcast about an unrelated record is
not customer contact — nobody acknowledged the ticket, nobody looked at it,
there is no owner who could have responded. If it counted, a single change
transition would auto-satisfy the response promise of **every** ticket linked
to that change, at once, with no one having answered anything. That is not a
loophole someone has to exploit; it is a metric-laundering machine that runs
on its own, and it is the automated form of exactly the internal-bookkeeping
metric this section already rejects for first assignment and first status
change — worse, because it needs no human action to trigger.

Recorded here now, before an auto-acknowledgement bot or a canned first-reply
feature gets added as ordinary ITSM functionality: **an automated public note
does not discharge a response promise.** If an org wants an automated
acknowledgement to count as a response, it must be posted with a human
author — this module is not to quietly invert the rule to keep an SLA number
green.

---

## 1. Purpose & boundary

Track whether each ticket met its response and resolution promises, measured on
a business calendar, and act when one is missed.

**In scope:** calendars and their windows and holidays (§2.1–2.3), policies and
their resolution order (§2.4, §3), the working-time clock (§4), pause (§5),
breach and escalation (§6), the sweep and auto-close (§7), RBAC/tenancy/audit
(§8), the API and admin surface (§9), tests (§10).

**Not in this sub-project.** Deferred in §13: per-customer calendar
assignment, SLA reporting and dashboards, multiple concurrent SLAs per ticket,
"time to next response" beyond the first, and pause on arbitrary custom
statuses.

**Boundary vs the ticket core.** This module calls `transitions.change_status`
(auto-close), `tickets.update_ticket` (priority escalation), and
`transitions.assign_ticket` (queue escalation). It defines no status, no
transition, and no second answer to who may do what. It requires **two small
changes to the ticket core**, stated here rather than discovered during the
build. Both are the same change: escalation is system-originated with no human
actor, and `change_status` already accepts `actor_type="system"` with
`actor_id=None`, so `update_ticket` (Task 8) and `assign_ticket` gain the same
keyword-only path, mirroring an existing pattern in the same module.

There are **two** calls, not one, because `queue_id` is not editable through
`update_ticket` — see §6's escalation contract for the reasoning. The
alternative — writing `priority`/`queue_id` directly from this module — would
bypass the ticket core's audit trail and its event publication, and is
rejected.

**Boundary vs notification.** This module publishes `ticket.sla_breached`;
notification delivers it. They share the outbox and nothing else. Recipients
for a breach are configured as an ordinary notification rule.

**Boundary vs email intake and the portal.** No code boundary: both create
tickets and comments through the ticket core, and an SLA record follows from
that automatically because §3's resolution runs at ticket creation regardless
of origin. There is one **grant** consequence, recorded here because it is not
discoverable from the module's own code (Task 8). The portal runs on its own
least-privilege database role (`portal_user`, portal spec §0.2/§2.1) and
delegates ticket creation to `tickets.create_ticket` on the requester's own
connection, so creation-time materialisation INSERTs into `ticket_sla` *as
`portal_user`*. Migration `0038` grants that role SELECT + INSERT on
`ticket_sla` under a **restrictive `portal_isolation` policy scoping every row
to the caller's own tickets**, plus SELECT on `sla_policy` and the three
calendar tables (org configuration the materialisation must read, on the same
footing as `ticket_category`). **No UPDATE and no DELETE**: every later SLA
write — pause, resume, both met stamps, both breach stamps, `escalated_at`,
`voided_at` — is an UPDATE driven by a staff-side transition on `app_user`, so
a requester can never move a deadline, mark their own promise met, or void it.
A blanket read of the org's `ticket_sla` rows was considered and rejected: it
exposes every colleague's deadlines and breaches to every requester, which is
the tenancy hole invariant #5 forbids. The read path (`note_public_comment`)
needs none of this because it returns at its requester comparison before
touching `ticket_sla`; an INSERT has no such ordering escape.

---

## 2. Data model

Migration `0033` (current head is `0032`). Every table is org-scoped and gets
RLS `ENABLE + FORCE` with a `tenant_isolation` policy carrying **both** `USING`
and `WITH CHECK` — invariant #5, with a behavioural cross-org test per table
(§10).

### 2.1 `sla_calendar`

`id (uuid7 PK), org_id FK, name (str 100), timezone (str 64), active (bool,
default true), created_by, created_at, updated_at`.

- `timezone` is an IANA name (`Europe/London`), validated against `zoneinfo` at
  write time. A calendar whose timezone does not resolve is rejected at the
  API, never at sweep time — a bad timezone discovered by the sweep would break
  every ticket on that calendar at once.
- Unique on `(org_id, name)`.

### 2.2 `sla_calendar_window`

`id, org_id FK, calendar_id FK, weekday (int 0-6, Monday=0), start_time (time),
end_time (time)`.

- **Multiple rows per weekday are expected**, so a lunch break is two windows
  rather than a special case in the clock.
- `start_time < end_time OR end_time = '00:00:00'` is a CHECK constraint
  (`ck_sla_calendar_window_start_before_end`; the `OR` clause is migration
  `0035`, added by the Task 3 review's finding F2). `end_time = '00:00:00'`
  means **midnight at the end of the day**, not a zero-length window: Postgres
  `TIME` has no "24:00" representation, so a plain `start_time < end_time`
  check can never be satisfied when `end_time` is midnight, for ANY
  `start_time` — making it physically impossible to store the one window
  shape an evening shift's second half or a genuine 24-hour day needs. It is
  the *only* legal encoding for both: a night shift is two windows on
  adjacent days (`22:00–00:00` on one, `00:00–06:00` on the next), and a true
  24/7 day is `00:00–00:00` (1440 minutes — `00:00–23:59` silently loses a
  minute a day). A window spanning midnight any other way is still rejected:
  `17:00–09:00` violates the CHECK, and the clock does not handle wrapping —
  it must not be asked to. See `app/modules/sla/calendar.py`'s `CalendarSpec`
  docstring for the clock-side reading of the sentinel.
- `weekday BETWEEN 0 AND 6` is a CHECK constraint (migration `0034`, added by
  the Task 1 review's finding F5). Python's `date.weekday()` only ever returns
  0-6, so an ISO Sunday-is-7 typo would otherwise be a window that silently
  never applies — the calendar quietly loses a working day, deadlines land later
  than the org configured, and nothing errors anywhere.
- A weekday with no rows is a non-working day.

### 2.3 `sla_calendar_holiday`

`id, org_id FK, calendar_id FK, holiday_date (date), name (str 100)`.

- A holiday removes that date's windows **entirely**. Half-days are expressed
  by not marking the day a holiday and accepting the normal windows; partial
  holidays are deferred (§13).
- Unique on `(calendar_id, holiday_date)`.

### 2.4 `sla_policy`

`id, org_id FK, queue_id FK NULL, priority (str 16) NULL, calendar_id FK,
response_target_minutes (int), resolution_target_minutes (int),
escalate_priority (bool, default false), escalate_to_queue_id FK NULL,
active (bool, default true), created_by, created_at, updated_at`.

- Unique on `(org_id, queue_id, priority)`. NULL in either column is a
  wildcard; see §3 for the resolution order.
- Both targets are **positive** integers (CHECK). A zero target would be
  breached at creation, which is never what anyone means.
- `priority` is constrained to the ticket core's domain
  (`low`/`normal`/`high`/`urgent`) by CHECK, mirroring `ck_ticket_origin_valid`.

### 2.5 `ticket_sla`

One row per ticket. `id, org_id FK, ticket_id FK UNIQUE, policy_id FK,
calendar_id FK, response_due_at, resolution_due_at, response_met_at NULL,
resolution_met_at NULL, response_breached_at NULL, resolution_breached_at NULL,
paused_at NULL, escalated_at NULL, policy_source (str 16),
last_recomputed_at, created_at, updated_at`.

- `ticket_id` is UNIQUE: one SLA per ticket. Multiple concurrent SLAs are
  deferred (§13).
- `policy_source` is one of `initial`, `retriage`, `escalation` — §0.3's
  attribution requirement and §0.4's loop guard are the same column.
- `escalated_at` makes escalation **once-only** regardless of how often the
  sweep sees the row.
- Indexes: `(org_id, response_due_at)` and `(org_id, resolution_due_at)`,
  partial on the corresponding `breached_at IS NULL` — these are what make the
  sweep an index scan rather than a table walk (§7).

#### 2.5.1 Amendment (Task 5 review): `paused_minutes_total`

The column list above is amended to add **`paused_minutes_total` (int, NOT
NULL, default 0)** — migration `0037`.

The list as originally written stored only `paused_at`, which is an *open*-pause
marker. Once a pause ended, the only surviving record of it was the offset
already folded into `response_due_at`/`resolution_due_at` — and §0.3's recompute
re-derives both deadlines from `ticket.created_at`, overwriting exactly that
offset with a value that never knew a pause had happened. **Every completed
pause's credit was silently destroyed by the next re-triage.** Measured on a
live database: 24/7 calendar, ticket filed Monday 09:00, 480-minute target →
due 17:00; a 120-minute completed pause → 19:00; a re-triage onto a policy with
*identical* targets → back to **17:00**.

The consequence is worse than the arithmetic looks. Credit is measured in
working minutes, so on a business-hours calendar the same defect re-derives a
resolution deadline back by whole working days — two working days already
breached at the instant of a correct routing action, after which §7's sweep
stamps `resolution_breached_at`, publishes `ticket.sla_breached`, escalates and
notifies staff for a promise that was never missed. The error is
one-directional (dropped credit can only shorten a deadline), so no real breach
is missed; but reported attainment is understated by the total paused time of
every ticket ever re-triaged after a pause, and the false breaches are
indistinguishable from real ones.

`paused_at` stays; the two columns answer different questions and neither is
derivable from the other. `paused_at` is *"is the clock stopped right now"* —
what §7's breach pass filters on. `paused_minutes_total` is *"how much working
time have we already given back"* — what §5's resume adds to and §0.3's
recompute folds back into the **target**, never onto the resulting instant
(adding it as elapsed time would spend the credit in wall clock and buy back
exactly the free weekend §5 forbids).

One accumulator serves both clocks. A clock only ever moves running → stopped,
never back, so a clock still running is owed every pause in the total, and a
clock that has stopped is never re-derived (§5's guard). Per-clock pause
accounting is a §13 concern, alongside the other per-target attribution §2.5's
single `policy_id` cannot express.

#### 2.5.2 Amendment (Task 8): `voided_at`

The column list above is amended to add **`voided_at` (timestamptz, NULL)** —
migration `0038`.

Nothing stopped the clocks on a ticket that reached `cancelled` or `rejected`
(`lifecycle.FINAL_STATUSES`). Both deadlines went on being due on a ticket
nobody would ever work again, so **every cancelled or rejected ticket breaches
with certainty** once past its deadline — roughly 0.1–0.2 phantom
`ticket.sla_breached` events per ticket filed at a 5–10% cancellation rate,
each indistinguishable in reporting from a promise the team really missed.
Worse, §6's escalation refuses a ticket in `TERMINAL_STATUSES` by *raising*, so
an org swept inside one transaction would have that raise roll back the cycle's
real breaches and retry the identical failure every interval, forever.

`transitions.change_status` stamps it from a `note_cancelled` hook on its
`FINAL_STATUSES` branch — the same shape §0.6's met-detection hooks use.
`closed` is deliberately **not** included: it is in the wider
`TERMINAL_STATUSES` but is reopenable, and a closed ticket's promise was
*discharged* on the way through `resolved`/`fulfilled`, not voided.

**A voided promise is neither met nor breached.** It needs its own column
rather than borrowing one of the four stop columns, and cancellation must
never met-stamp: that would claim the promise was *kept* and make "cancel it" a
one-click way to inflate SLA attainment. Symmetrically, voiding **erases
nothing** — a clock that had already stopped keeps its `*_met_at`/
`*_breached_at`, so cancelling cannot launder away a breach that really
happened.

A sweep-side filter (`WHERE ticket.status NOT IN ('cancelled','rejected')`) was
considered and rejected: it buries the invariant in the one consumer that knows
about it, so §9.1's ticket detail would still render a ticking red countdown on
a cancelled ticket and every future reader would have to rediscover the same
join. `voided_at` is column-local, needs no join, and is readable by everything
that already reads the row. §7's sweep excludes `voided_at IS NOT NULL`.

#### 2.5.3 Amendment (build review): the two new columns are API-visible, and the sweep indexes are no longer the ones described above

Two corrections to the column list and one to the index line, all of them
"the spec describes the first draft and the shipped code is the later one".

**Both amended columns are surfaced, not internal.** §2.5.1's
`paused_minutes_total` and §2.5.2's `voided_at` are returned by
`TicketSlaOut` on `GET /tickets/{id}/sla` alongside the four stop columns.
That is deliberate rather than incidental: §9.1 requires a paused clock to be
*visibly* paused and a void promise to be distinguishable from a kept one, and
neither is decidable in the browser from the stop columns alone —
`paused_minutes_total` is the only evidence that a deadline was moved for a
reason rather than drifting, and `voided_at` is the only column that says a
countdown should stop rendering at all. Exposing them is also what keeps
§9.1's "breach state is never computed in the browser" honest: the client is
handed the answer *and* the reason, so it never needs to reconstruct either.

**The indexes are migration `0039`'s, not the ones §2.5 names.** §2.5 says the
two sweep indexes are partial on the corresponding `breached_at IS NULL`;
`0033` shipped them partial on `<target>_breached_at IS NULL AND
<target>_met_at IS NULL`, which is §7's original predicate. Both are now
wrong. `0039` redefines each as:

```
(org_id, <target>_due_at)
  WHERE <target>_breached_at IS NULL
    AND paused_at IS NULL
    AND voided_at IS NULL
```

i.e. exactly the columns of §7.1's predicate that are pure `IS NULL` tests on
`ticket_sla` itself. `met_at` is deliberately **absent** from the partial
condition: it is now compared to `due_at` rather than to NULL, and a partial
index cannot express a cross-column comparison. This is not tuning. A partial
index on `met_at IS NULL` does not *contain* the late-met rows §7.1's
predicate must consider, so Postgres cannot use it and correctly refuses to —
the widening silently turned both sweep queries into sequential scans of the
whole table, twice per org, every 300 seconds, with the indexes still present
and still looking healthy.

### 2.6 `sla_org_settings`

`id, org_id FK UNIQUE, auto_close_after_days (int, default 7), created_by,
created_at, updated_at`.

A dedicated table because **no general per-org settings table exists** in this
codebase (`budget_settings` is module-scoped). Inventing a platform-wide one
from inside this module would be defining shared structure, which §1 forbids.
If a general settings table later arrives, this collapses into it.

---

## 3. Policy resolution

First match wins, most specific first:

1. `(queue_id = ticket.queue_id, priority = ticket.priority)`
2. `(queue_id = ticket.queue_id, priority IS NULL)`
3. `(queue_id IS NULL, priority = ticket.priority)`
4. `(queue_id IS NULL, priority IS NULL)`

Only `active` policies are considered.

**If nothing matches, no `ticket_sla` row is created** and the ticket visibly
has no SLA. It is not given a zero, a default, or a silent infinity — an org
that has configured no policy should see "no SLA", because a fabricated target
is worse than an absent one.

Resolution runs at ticket creation and on human re-triage (a priority or queue
change with a human actor). It does **not** run on a breach escalation (§0.4)
— and that applies to **both** escalation actions, the priority bump *and* the
queue move. A queue move re-resolves under the ordinary rule above, so an
escalation that changed the queue would otherwise pick up the escalation
queue's policy and recompute, which is the §0.4 cascade arriving by a second
door.

### 3.1 Amendment (Task 8): where resolution is wired, and what a broken calendar may cost

**The call sites.** `tickets.create_ticket` → `start_tracking`;
`tickets.update_ticket` (priority) and `transitions.assign_ticket` (queue) →
`recompute_for_retriage`; `transitions.change_status` → `pause`/`resume`,
`note_resolved`, `note_cancelled`. Both recompute call sites are gated on
`actor_type == "user"`, which is §0.4's loop guard in structural form: breach
escalation reaches those exact two services with the exact same field and value
and is distinguishable only by its actor. Both are also keyed on the value
having actually *changed*, so re-sending a ticket's current priority, or an
assignment that does not move the queue, restamps nothing.

**A misconfigured calendar may not fail a ticket write.** `start_tracking`
legitimately raises `UnsatisfiableCalendar` (§4) when a policy points at a
calendar with no working windows. Propagated out of `create_ticket`, that one
admin mistake means *nobody in the organisation can file a ticket* — not the
portal, not email intake, not the console. A customer locked out of raising an
incident is strictly worse than a ticket without an SLA row, and the latter is
not a new state: it is exactly §3's "no policy matched → no row", which this
degrades to, and which a later human re-triage repairs once the calendar is
fixed. Every hook call site therefore tolerates `UnsatisfiableCalendar` and
`ValueError` and logs at ERROR with the ticket id. The catch is deliberately
**not** `SlaValidationError`: the module's cross-org refusals are that type and
must stay loud (invariant #5), and a bare `except Exception` would swallow
`IntegrityError` on an already-aborted transaction.

### 3.2 Amendment (build review): the four-level order is as written, and the tolerated exception is `UnsatisfiableCalendar` ALONE

**The resolution order stands, verified against the implementation.**
`policies.resolve_policy` queries §3's four levels in exactly the order above,
first match wins, `active` only, and returns `None` rather than a fabricated
default when nothing matches. It additionally de-duplicates a level that has
collapsed into a lower one (a caller passing `queue_id=None` makes levels 1/2
and 3/4 the same pair), which skips a redundant repeat query and changes
nothing about which level wins. No correction is needed here; it is recorded
because "verified and unchanged" and "never checked" look identical in a
document.

**§3.1's tolerance list is wrong: `ValueError` is NOT swallowed.**
`sla_misconfiguration_is_not_fatal` catches `UnsatisfiableCalendar` and
nothing else, and that narrowing is the shipped behaviour *and* the correct
one. `calendar.py` raises `ValueError` in three places — a naive `datetime`
reaching `_to_utc`, a span past `MAX_LOOKAHEAD_DAYS`, and a negative
`minutes` — and all three are *programming*-error guards rather than admin
misconfiguration: a stored timezone is validated at write time,
`ticket.created_at` is a DB-supplied `TIMESTAMPTZ` and so never naive, and
`sla_policy`'s target columns carry `CHECK ... > 0`. Reaching one of them in
production means this module's own arithmetic produced a corrupt instant or
handed a negative credit into `add_working_minutes`, and tolerating *that*
degrades a genuine bug into "no SLA row and one ERROR line" for every ticket
in the org — the exact failure class §3.1 exists to end, one exception type
over. The tolerance is safe for `UnsatisfiableCalendar` specifically because
it is raised in pure Python before any statement is issued, so the caller's
transaction is still intact; that is not true of a DBAPI error, which is the
other half of why the catch may not be widened.

### 3.3 Amendment (2026-10-29, MSP clients plan 2026-10-26, Task 3): a client's own policies replace the global ones

`sla_policy` is an O table: a row with no client is every client's, and a row
may belong to one client (`POST /sla/policies` takes an optional `client_id`;
without one the row is global, and writing it needs every client).
`policies.resolve_policy` takes the ticket's client. **If that client has any
active policy of its own, §3's four levels are resolved among its policies
only; if it has none, among the global ones** (operator decision D2,
2026-10-26). The two sets are never mixed level by level, so a client that
defines only `(queue, priority)` does not inherit the global wildcard rows.
The client predicate is explicit, because the email poller and the sweeps run
under the every-client scope, where RLS shows every client's policies.

**Moving a ticket to another client** (`docs/modules/service-desk/spec.md`
§2) resolves its SLA again for the new client (`tracking.recompute_for_move`).
When a policy resolves, it is a re-triage. **When none does, the `ticket_sla`
row is deleted in the same transaction**, so the ticket has no SLA in its new
client — §3's "no policy matched → no row" — and the move's `ticket.moved`
audit event records `"sla": "voided"`. An ordinary re-triage into an
unpoliced level still leaves the promise intact, as before. The plan is
`docs/superpowers/plans/2026-10-26-msp-clientes-fase2-modulos.md`.

---

## 4. The clock

Two pure functions, no database, in `app/modules/sla/calendar.py`:

- `working_minutes_between(calendar, start, end) -> int`
- `add_working_minutes(calendar, start, minutes) -> datetime`

Everything else in this module is built on these two. Both convert to the
calendar's timezone via `zoneinfo`, walk day by day applying windows and
skipping holidays, and return UTC.

**DST is the hazard, not an edge case.** Adding four working hours across a
spring-forward boundary in local time is where this class of code always
breaks: the naive implementation adds a `timedelta` to a UTC instant and
silently lands an hour off for half the year. Both functions compute in the
calendar's local time and convert once at the boundary. §10 requires explicit
tests on both DST transitions.

**Bounded iteration.** `add_working_minutes` must terminate even when a
calendar has no windows at all — a target of 240 minutes against an empty
calendar is unsatisfiable, and the function must raise a typed error after a
bounded number of days rather than looping. A calendar with no windows is
otherwise a way to hang the sweep.

---

## 5. Pause

Entering `pending` (incident) or `pending_approval` (request) stamps
`ticket_sla.paused_at`. Leaving it adds
`working_minutes_between(paused_at, now)` to both outstanding deadlines and
clears `paused_at`.

Both statuses pause because both mean *blocked on someone who is not us* —
which is the reason the ticket core made `pending` a first-class state rather
than a flag (its §3.1).

**Pause is measured in working time.** A pause spanning a weekend on a
business-hours calendar extends the deadline by nothing, because none of that
was time we owed anyone. Measuring pause in wall-clock while measuring the
target in working time would let a Friday-afternoon pause buy two free days.

A deadline already met is not extended: `response_met_at`/`resolution_met_at`
being non-NULL means that clock has stopped for good.

### 5.1 Amendment (Task 7): the guard is met **or breached**, and resume
re-derives

Two clarifications the sentence above is too narrow for.

**"Already met" is really "already stopped".** A clock stops on *either*
`*_met_at` or `*_breached_at`, and neither may be extended: a breached
deadline's `due_at` is the timestamp reporting compares the breach against, and
moving it after the fact rewrites the record of what was promised. Reading the
guard as `met_at IS NULL` alone leaves half of it unwritten — the same
constant-pairing gap the Task 5 review found in that task's tests.

**Resume re-derives, it does not increment.** Each still-running deadline is
recomputed as `target + paused_minutes_total` working minutes from
`ticket.created_at` — character-for-character the formula §0.3's recompute
uses. The two paths then agree by construction rather than by coincidence; an
increment on the stored `due_at` is only *equivalent* for as long as every
other writer of that column also derives it from `created_at` under the same
calendar. The calendar used is `ticket_sla.calendar_id` — the row's own record
of the calendar its deadlines were derived on — so the pause measurement and
the re-derivation cannot disagree with each other.

### 5.2 Amendment (Task 8): a ticket **created** into a pausing status is born paused

§5 says entering `pending`/`pending_approval` stops the clock. Being *created*
into one is entering it, and that case is not reachable by any transition: a
request whose category requires approval is placed directly on
`pending_approval` by `lifecycle.initial_status`, and the request lifecycle
offers no other door into that status. A pause wired only to `change_status`
therefore misses **every approval-gated request** — its clock runs for the
whole time it is blocked on an approver, and the eventual `pending_approval →
in_progress` calls `resume` on a row with `paused_at IS NULL`, a no-op, so the
entire approval wait is charged to the service desk.

`_materialise` therefore writes `paused_at` **in the INSERT** when the ticket
is already in a pausing status — including when `recompute_for_retriage`
materialises a row for the first time on a ticket already sitting in `pending`.
That it is an INSERT rather than a follow-up `pause()` is load-bearing, not
incidental: `portal_user` holds INSERT on `ticket_sla` and deliberately no
UPDATE (§1's portal note), and a portal-filed approval-gated request reaches
exactly this line.

**Interaction with §0.4 that Task 9 must settle.** Escalation writes
`policy_source = 'escalation'` and deliberately does not recompute deadlines.
If it *also* repoints `ticket_sla.policy_id` at the escalated priority's policy,
then this re-derivation applies that shorter target to the still-running clock,
reintroducing §0.4's cascade through an unrelated status change. Task 9 must
either leave `policy_id` alone or accept that later human-originated
recomputation (this, and equally `recompute_for_retriage`) derives under it.

### 5.3 Amendment (build review): §5.2's open question, settled

Task 9 took the first branch. **Escalation leaves `ticket_sla.policy_id` and
`calendar_id` alone**; it writes `escalated_at` and `policy_source =
'escalation'` and nothing else about which promise applies. So a later
`resume` — or `recompute_for_retriage`, or §7's calendar recompute — re-derives
under the policy that actually produced the deadlines the row is carrying, and
§0.4's cascade has no second door through an unrelated status change.

That choice also makes two other things true that would otherwise need their
own guards. The breach event's `policy_id` means *the policy that was missed*
rather than *the policy we just moved to*, which is what a reader of a breach
notification assumes it means. And `policy_source = 'escalation'` is left
saying exactly what §0.4 says it says — *why the deadlines on this row were
not recomputed* — rather than doubling as a record of a policy repoint the
column cannot express.

---

## 6. Breach and escalation

The sweep marks a breach by stamping `response_breached_at` or
`resolution_breached_at` and publishing `ticket.sla_breached` to the outbox
with the ticket, which target breached, and the policy that applied.
`ticket.sla_breached` is added to `EVENT_TYPES` in `app/core/events.py`.

If the policy sets `escalate_priority`, the ticket's priority is raised one
step (`low → normal → high → urgent`; `urgent` does not escalate). If
`escalate_to_queue_id` is set, the ticket moves to that queue. Both set
`escalated_at` so escalation happens **once per ticket**, not once per sweep
cycle.

#### 6.0 Which ticket-core call each half uses (Task 8 review, F1)

The two halves go through **two different services**. This is not
interchangeable, and getting it wrong is silent:

| Escalation | Call | Audit action |
| --- | --- | --- |
| Priority bump | `tickets.update_ticket(session, org_id, None, ticket_id, {"priority": ...}, actor_type="system")` | `ticket.updated` |
| Queue move | `transitions.assign_ticket(session, org_id, None, ticket_id, {"queue_id": ...}, actor_type="system")` | `ticket.assigned` |

Both pass `actor_id=None` positionally with keyword-only
`actor_type="system"`.

**`queue_id` is not editable through `update_ticket` and must not be made
so.** Service-desk spec §5 requires a queue move to hold `ticket.assign`, not
`ticket.write`, and §8.3 files it under the `ticket.assigned` audit action;
commit `eb6b129` removed `queue_id`/`assigned_to` from
`TICKET_EDITABLE_FIELDS` and from `schema.TicketUpdate` for exactly that
reason. Re-adding it would make the `ticket.assign` permission decorative and
misfile every queue move in the immutable audit log. Queue is also
load-bearing *here*: `resolve_policy` keys on `(queue, priority)`, so a queue
move changes which policy applies — one more reason it belongs behind its own
audited, permission-gated service rather than a generic PATCH.

Passing `queue_id` to `update_ticket` **raises** `TicketValidationError`
naming `assign_ticket`; it is not silently dropped. It *was* silently dropped
before the Task 8 review, which is what made this worth pinning in the spec:
the queue half of escalation would have reported success while doing nothing.

A combined priority+queue escalation is therefore two calls and two audit
rows, not one. Both must succeed for the escalation to be complete; neither
recomputes deadlines (below).

Note that `assign_ticket` refuses a ticket in `FINAL_STATUSES`
(`cancelled`/`rejected`) while `update_ticket` refuses any ticket in
`TERMINAL_STATUSES` (those two plus `closed`). Escalation must not reach
either case — stale SLA promises on terminal tickets are voided at the source
so escalation is never attempted against one.

#### 6.0.1 Cascade guard

An escalation writes `policy_source = 'escalation'` and does **not** recompute
deadlines (§0.4) — neither for the priority bump **nor for the queue move**.
The queue move is the more dangerous of the two: re-resolving under §3 after
it would pick up the escalation queue's own policy, which is §0.4's cascade
through a second door.

Every escalation is audited (invariant #4) with the ticket, the target that
breached, and the old and new priority/queue.

### 6.1 Amendment (Task 1 review, finding F2): breach mail is staff-facing by construction

`ticket.sla_breached` is a **staff-only** event type. A notification rule
pairing it with `recipient_kind = 'requester'` is **refused at write time**, at
all three of the notification module's guard layers — `RuleCreate`'s Pydantic
validator, `rules.create_rule`'s service check, and the DB CHECK constraint
`ck_rule_no_sla_breach_to_requester` (migration `0034`). The forbidden set is
declared once, in `app.core.events.STAFF_ONLY_EVENT_TYPES`, so the three layers
cannot drift apart.

Why write time rather than render time is the same reasoning notification spec
§6.4 gives for internal notes: a render-time filter is one template edit away
from failing open, and the failure is silent — mail that has already left.

Why it needed saying at all: the renderer's copy is written staff-facing ("the
response SLA for ticket #… has been breached … sign in to Toeshee to review
it"). Before this amendment the only thing keeping that mail away from the
customer was §7's publisher omitting `requester_id` from the payload — a
convention, not a guarantee. `recipient_kind = 'requester'` resolves the
recipient from the **ticket**, not from the payload, so the mail would in fact
have been delivered: telling the external customer that their supplier missed a
contractual deadline, an outcome nobody chose. The payload's shape is now a
convenience, not the guard.

---

## 7. The sweep

One Celery beat task, `sla_sweep`, following `app/worker.py`'s established
per-org pattern.

Per org, in one pass:

1. **Breach detection.** Rows where `response_due_at <= now()` and
   `response_breached_at IS NULL` and `response_met_at IS NULL` and
   `paused_at IS NULL`; likewise for resolution. Mark, publish, escalate.
2. **Auto-close.** Tickets in `resolved`/`fulfilled` whose `resolved_at` is
   older than `sla_org_settings.auto_close_after_days` →
   `transitions.change_status(..., to_status="closed", actor_type="system",
   actor_id=None)`. The ticket core's `change_status` docstring names "future
   auto-close" as an intended caller of that path; this is it.

Both steps are **idempotent**: a breach already stamped is excluded by the
query, and a ticket already `closed` is not in `resolved`. Running the sweep
twice changes nothing the first run did.

The sweep is org-scoped throughout (invariant #5) and carries an explicit
`org_id` predicate on every query rather than relying on RLS alone.

**Calendar and policy edits recompute affected open tickets.** A materialised
deadline goes stale when the calendar under it changes, and the recompute is an
explicit, tested behaviour (§10) rather than something nobody triggers in
testing. Recompute touches only tickets whose clock is still running — a met or
breached target is history and is never rewritten.

### 7.1 Amendment (build review): the breach predicate as issued, and migration `0039`

The paragraph above writes pass 1's predicate as `due_at <= now AND
breached_at IS NULL AND met_at IS NULL AND paused_at IS NULL`. What
`sweep._breach_pass` actually issues is:

```
due_at <= now
AND breached_at IS NULL
AND (met_at IS NULL OR met_at > due_at)
AND paused_at IS NULL
AND voided_at IS NULL
AND ticket.status NOT IN ('cancelled', 'rejected')   -- joined
```

**`met_at > due_at`, because a LATE reply is not a kept promise.** `met_at IS
NULL` alone makes the outcome depend on which of two independent things ran
first. A response deadline falls at 10:00 and the beat fires at 10:04. If
nobody replied, the sweep stamps a breach and the running-clock guard then
refuses to met-stamp the 10:06 reply — the promise reads breached, correctly.
If the technician replies at 10:02 instead, four minutes into a deadline that
had *already passed*, `response_met_at` is stamped, the narrow predicate
excludes the row forever, and the identical failure to answer in time reads as
MET. Attainment then measures the phase of the beat schedule rather than the
service desk. Comparing `met_at` to `due_at` asks the question that actually
matters — was the promise kept *on time* — and is order-independent by
construction. The boundary is a strict `>`: `met_at == due_at` is met, because
"within 60 minutes" is satisfied at minute 60 exactly.

**The stamping half is `_response_breachable` / `_resolution_breachable`, and
that is deliberately NOT the `_running` pair.** Widening the SELECT only makes
the sweep *find* such a row. `breach.mark_breached` guards each target on
`breached_at IS NULL AND (met_at IS NULL OR met_at > due_at)` — this
predicate's per-row half, restated in Python — rather than on the
"is this clock still ticking" question its neighbours ask. The two disagree on
exactly one row shape, and that row is the whole reason two predicates exist:
a clock stopped *late* is not running (contact happened, the stamp is real, and
`note_public_comment`, `note_resolved`, `resume` and the recompute paths must
all keep their hands off it) but the promise was still broken. Reading the
running guard on the breach path was a shipped defect: every late-met row was
selected, handed to `mark_breached`, and silently skipped, every 300 seconds,
forever.

So **`met_at` and `breached_at` are not mutually exclusive**, and a row
carrying both is the correct record of a late reply rather than a
contradiction. `met_at` records *when contact happened*; `breached_at` records
*that the promise was broken*; attainment reads the second, response time reads
the first, and once a reply is late neither is derivable from the other.
`views.target_state` therefore ranks `breached` above `met` — reporting a
late-answered ticket as "met" would let any breach be laundered by eventually
replying.

**`voided_at IS NULL`, and the joined status exclusion, which is not redundant
with it.** §2.5.2 already requires the column filter. The join is the other
half: migration `0038` deliberately did not backfill `voided_at`, so every
ticket cancelled *before* that column existed reads `voided_at IS NULL`, and
the column filter alone excludes not one of them — the first sweep in any
existing deployment would emit a phantom breach per historical cancelled
ticket. The column is the fast path and carries the semantics for every later
reader with no join; the status join is what makes the invariant true for rows
written before the column existed. `FINAL_STATUSES` and not
`TERMINAL_STATUSES`, mirroring `note_cancelled`: a `closed` ticket's promise
was *discharged* on the way through `resolved`/`fulfilled`, not voided, and
suppressing its breach would quietly under-report. The join predicate carries
`ticket.org_id` too (invariant #5) — `ticket_sla.ticket_id` is a plain FK with
no org check, so without it this org's breach decision could be taken from
another tenant's ticket status.

**Migration `0039` repoints `0033`'s partial indexes at this predicate** — see
§2.5.3 for the redefinition and for why a partial index on `met_at IS NULL`
could not merely rank the late-met rows badly but could not contain them at
all.

### 7.2 Amendment (build review): where the recompute is wired, and what a policy edit moves

"Calendar and policy edits recompute affected open tickets" is one sentence
covering two functions and five call sites, and which edits recompute is a
decision rather than a detail.

`sweep.recompute_for_calendar_change` is called by `calendars.replace_windows`,
`add_holiday`, `remove_holiday`, and `update_calendar` **when and only when it
changed the timezone** — the four edits that are inputs to
`add_working_minutes`. Renaming a calendar or toggling `active` calls nothing,
because the recompute would be guaranteed churn.
`sweep.recompute_for_policy_change` is called by `policies.update_policy` for
`response_target_minutes`, `resolution_target_minutes` and `calendar_id`, and
for nothing else: `queue_id`/`priority`/`active` decide which policy a *future*
ticket resolves to (§3) and cannot move the deadline of a row that already
points at this one. Each runs inside the same transaction as the edit, so the
edit and its recompute commit or roll back together.

**A row recomputed for a policy change adopts that policy's `calendar_id`.**
The premise that settles it is that a row's *targets* already track the policy
row and always have — nothing copies `response_target_minutes` onto
`ticket_sla`, so "an edit to this policy changes what an open ticket under it
is promised" is existing behaviour arriving lazily at whatever event
recomputes the row next. Given that, leaving `calendar_id` alone is the
incoherent option rather than the conservative one: a policy repointed from
24/7 to Mon–Fri would leave open tickets deriving the policy's *new* target on
the *old* calendar — a (target, calendar) pair that exists nowhere in the
org's configuration and that nothing short of a human re-triage can reconcile,
because `resume` reads `ticket_sla.calendar_id` too. This does not reopen
§0.4's door: `policy_id` is never written (rows are selected by it),
`policy_source` is never written, and the trigger is an audited human
`sla.admin` edit rather than a system escalation.

The adoption happens **below** the running-clock guards, so a row this
function skips — frozen, paused-out or voided — is not repointed either, and
history keeps the calendar it was really promised under.

---

## 8. RBAC, tenancy, audit

**Permissions** (added to `app/core/rbac.py`):

- `sla.read` — view calendars, policies, and a ticket's SLA state.
- `sla.admin` — create/modify/delete calendars and policies, and edit
  auto-close settings.

`sla.admin` is high-privilege: it sets what the organisation has promised its
customers and can silence breach escalation entirely. Admin tier only, never
technician. Both seeded to `super-admin`/`org-admin`.

Every route carries `require_permission` server-side (invariant #6), with RBAC
completeness derived from the router.

**Audited** (invariant #4): calendar create/update/delete, policy
create/update/delete, auto-close settings changes, every escalation, and every
auto-close. Sweep cycles themselves are **not** audited — they are operational
and would swamp the log.

**Tenancy** (invariant #5): every table carries `org_id` with RLS
`ENABLE + FORCE`, and a behavioural cross-org test per table that fails if its
predicate is removed.

---

## 9. API and admin surface

Mostly under `/api/v1/sla`, versioned per convention, literal segments before
`{id}`. The one exception is the per-ticket read, which lives under
`/api/v1/tickets/{ticket_id}/sla` because it is a property of a ticket and is
gated on `ticket.read` — see the note below the table.

| Method | Path | Permission |
| --- | --- | --- |
| GET/POST | `/sla/calendars` | read / admin |
| GET/PATCH/DELETE | `/sla/calendars/{id}` | read / admin / admin |
| PUT | `/sla/calendars/{id}/windows` | `sla.admin` |
| GET/POST | `/sla/calendars/{id}/holidays` | read / admin |
| DELETE | `/sla/calendars/{id}/holidays/{holiday_id}` | `sla.admin` |
| GET/POST | `/sla/policies` | read / admin |
| GET/PATCH/DELETE | `/sla/policies/{id}` | read / admin / admin |
| GET/PUT | `/sla/settings` | read / admin |
| GET | `/tickets/{ticket_id}/sla` | `ticket.read` |

Windows are replaced as a set (`PUT`) rather than individually — a weekday's
windows only make sense together, and a partial edit can leave a calendar with
overlapping or inverted windows.

A calendar referenced by an active policy cannot be deleted; the error names
the policies. Deleting a policy leaves existing `ticket_sla` rows intact —
their `policy_id` records what was promised at the time, and rewriting history
because an admin deleted a row would destroy the audit value of the record.

The ticket SLA view is gated on `ticket.read`, not `sla.read`: a technician
looking at a ticket needs to see its deadline without holding SLA
administration rights. It carries `policy_name` and `calendar_name` beside the
two ids (added 2026-09-23). A policy has no name column, so its name describes
what it matches ("Urgent tickets in the Network queue", "Every ticket").

### 9.1 Frontend (`frontend/src/modules/sla/`)

Two admin surfaces plus one ticket-detail addition, matching
`frontend/src/modules/notification/`'s conventions — TanStack Query, strict
TypeScript, no `any`. This module introduces no new frontend pattern.

- **Calendars** — list, edit windows as a weekly grid, manage holidays. The
  timezone is shown on every view of a calendar, because a working window is
  meaningless without it and a reader who assumes their own timezone will
  misread every row.
- **Policies** — the `(queue, priority)` matrix, with wildcards rendered as
  "any" rather than blank so an unset cell is distinguishable from a cell that
  deliberately matches everything.
- **Ticket detail** — the ticket's response and resolution state: due, met, or
  breached, and paused where it applies. A paused clock must be visibly paused;
  a countdown that silently stops moving reads as a bug and prompts nobody to
  act.

Breach state is never computed in the browser. The server owns whether a
deadline was met (invariant #6's reasoning applies here even though this is
display rather than authorisation: two implementations of the clock would
disagree, and the browser's would be the one the customer quotes back).

### 9.2 Amendment (build review): what §9's route table does not say

The table is the authoritative route list and the shipped router matches it
exactly — seventeen registrations, sixteen under `/api/v1/sla` and the
per-ticket read under `/api/v1/tickets/{id}/sla`, every one carrying
`require_permission` server-side (invariant #6). Three things the table's
shape leaves unstated are load-bearing, and all three are decided in the
shipped code rather than here.

**Windows are readable, on the calendar detail.** §9's table gives
`/sla/calendars/{id}/windows` a `PUT` and no `GET`. Read literally that makes
the week **write-only**: §9.1's weekly-grid editor could never load the grid
it edits, and an admin could not read back what they had just replaced. The
shipped `GET /sla/calendars/{id}` returns them on `CalendarDetail.windows`,
sorted weekday-then-start-time (the order the grid reads in, rather than the
order the client happened to `PUT` them in). They hang off the calendar rather
than getting a route of their own precisely because that is what the table
says the surface is — a window is meaningless without the calendar's timezone,
and this shape makes it impossible to serve one without the other.

**`tickets_without_sla` and `configured` ride on `GET /sla/settings`.**
Neither appears in the table, and neither gets a route: they are fields on the
settings response, because the table is the authoritative route list and this
is the SLA administration screen's own payload. They exist because §3.1's
degradation is otherwise *invisible to the organisation*. A broken calendar
costs a ticket its SLA row and logs at ERROR on the API host — which is not a
surface anybody in the org can see, so the org's experience is that everything
looks normal, every ticket quietly has no promise attached, and the first
person to find out is a customer asking why the SLA report is empty.
`tickets_without_sla` is the consequence measure: 0 in a healthy org, growing
in a broken one, and it catches causes no enumeration covers — most
importantly §3's "no policy matched", which is not a fault at all until the
org believes it has configured SLAs. It counts only **non-terminal** tickets,
because a number that cannot return to 0 once the calendar is fixed stops
being read. `configured` distinguishes "we chose 7 days" from "nobody has ever
looked at this", which are different conversations. The cause half is
`CalendarOut.health` (`ok` / `no_working_windows` / `invalid_timezone`),
carried on every calendar read so a misconfigured calendar is visible on the
screen where it can be fixed rather than only discoverable by a ticket
failing to get a row.

**Deletion is stricter than "cannot be deleted if an active policy references
it".** As shipped, a calendar is refused if *any* policy references it, active
or not — the FK does not distinguish the two, and an inactive policy still
pins the row — and refused again if any `ticket_sla` row was materialised on
it. A **policy** is likewise refused while any `ticket_sla` row references it;
`active=False` is the retirement path, and since migration `0036` scoped the
four partial unique indexes to active rows that path is complete rather than a
dead end (a retired policy frees its `(queue, priority)` level for a
replacement, and both the retired row and every `ticket_sla` row pointing at
it stay exactly as they were). This is the stronger reading of §9's own
sentence: "deleting a policy leaves existing `ticket_sla` rows intact" is
guaranteed by refusing the delete outright, where allowing it and relying on a
RESTRICT FK to fail somewhere downstream would surface as a 500 rather than a
message naming the tickets.

### 9.3 Amendment (build review): `TargetState` has SIX values, and `void` is one of them

§9.1 names four states — due, met, breached, paused. The shipped
`views.target_state` returns six, and the two additions are first-class rather
than presentational:

* **`none`** — there is no `ticket_sla` row. §3's legitimate state, and the
  reason `GET /tickets/{id}/sla` answers `200 {"has_sla": false}` rather than
  404: "this org has configured no policy" and "no such ticket" are different
  answers, and conflating them both hides the misconfiguration §9.2 exists to
  surface and makes the 404 useless.
* **`void`** — the ticket reached `cancelled`/`rejected` and `voided_at`
  (migration `0038`) is stamped. **Treating `void` as a breach is a
  correctness bug, not a display choice.** A voided promise is neither kept
  nor broken; rendering it as breached would report every cancelled ticket as
  a missed deadline, which at a 5–10% cancellation rate is roughly 0.1–0.2
  phantom breaches per ticket filed, each indistinguishable in a report from a
  promise the team really missed. Rendering it as *met* is the symmetric
  error and worse, because it makes "cancel it" a one-click way to inflate
  attainment (§2.5.2).

Precedence is `none` → `void` → `breached` → `met` → `paused` → `running`, and
two steps of it are decisions rather than ordering convenience. `breached`
outranks `met` because the two genuinely co-occur on a late reply (§7.1) and
reporting that row as met would launder the breach. `paused` sits *below* both
stop columns because a paused clock that has already stopped is finished
rather than stopped — but it is a state of its own rather than folded into
`running`, because §9.1 requires a paused clock to be visibly paused and a
countdown that silently stops moving reads as a bug and prompts nobody to act.

**One known limit of the `void` precedence, recorded rather than hidden.** A
ticket that really breached and was *later* cancelled reports `void`, because
`voided_at` outranks the breach stamp. The stored columns keep the truth —
§2.5.2 is explicit that voiding erases nothing, and `*_breached_at` is
untouched — so attainment reporting (§13) is unaffected and the breach is not
laundered out of the record. It is only the single derived string §9.1's
ticket panel renders that shows the later fact rather than the earlier one.
That is the right default for a panel whose job is "what should I do about
this ticket now" (nothing: it is cancelled), and the alternative — ranking
`breached` above `void` — would put a red countdown-missed badge on every
cancelled ticket that had ever gone past due, which is the noise §2.5.2 added
the column to remove. Reporting must read the columns, never this string.

### 9.4 Amendment (build review): request models forbid unknown keys

Every request model in `schema.py` sets `extra="forbid"`, so an unknown or
mistyped key is a **422 naming the offending key**, not a silently-ignored
200. Response models are deliberately not strict: they are constructed
in-process by this module's own builders and never parsed from input.

The reason is specific, not tidiness. `policies.update_policy` deliberately
*refuses* an unknown field rather than ignoring it — an earlier review closed
exactly that defect, where a typo'd body returned 200 having changed nothing
and audited a no-op edit. Pydantic's default `extra="ignore"` strips the
unknown key **before the service can ever see it**, so a request model that
ignored extras would reinstate that defect for every caller arriving over
HTTP while the service's own guard stayed green in its unit tests. Forbidding
at the schema is what carries the service's decision across the wire boundary.

---

## 10. Testing requirements

Test-first per `CLAUDE.md`. Named because each corresponds to a specific way
this module fails silently:

1. **Working-time arithmetic**, table-driven: within one window, spanning a
   lunch break, spanning a weekend, spanning a holiday, starting outside
   working hours, starting exactly at a window boundary.
2. **Both DST transitions** — a four-working-hour target across spring-forward
   and autumn-back, asserting the local wall-clock result, not just that a
   value was produced.
3. **An unsatisfiable calendar terminates** — a target against a calendar with
   no windows raises a typed error rather than looping.
4. **Policy resolution order**, all four levels plus no-match, asserting that
   no-match creates **no** `ticket_sla` row.
5. **Re-triage recomputes from ticket start** and can produce an
   already-breached deadline (§0.3), with `policy_source = 'retriage'`.
6. **Breach escalation does NOT recompute** (§0.4) — the explicit loop guard.
   This test must fail if the exception is removed; without it the cascade in
   §0.4 is what ships.
7. **Escalation happens once**, not once per sweep cycle.
8. **Pause is measured in working time** — a weekend pause on a business-hours
   calendar extends the deadline by zero.
9. **The response clock stops on a public comment from a non-requester**, and
   **not** on an internal note, an assignment, or a status change (§0.6).
10. **Resolution stops the response clock.**
11. **The sweep is idempotent** — running it twice produces one breach record,
    one event, one escalation.
12. **Auto-close respects the per-org setting** and uses
    `actor_type="system"`.
13. **A calendar edit recomputes running deadlines** and leaves met/breached
    ones untouched.
14. **Cross-org isolation** per table, behavioural, failing if the `org_id`
    predicate is removed.
15. **Router-derived RBAC completeness** for every SLA route.

### 10.1 Amendment (build review): three more, each pinning an amendment above

16. **A LATE reply is breached, not skipped forever** (§7.1). A row whose
    `met_at > due_at` must be both selected by the sweep AND stamped by
    `mark_breached`, and the boundary must be parametrised at due−1s, due
    exactly and due+1s — `met_at == due_at` is met on both sides. This is the
    test the shipped defect survived: the sweep's half was widened while the
    stamping half still asked the running question, so the row was selected
    and skipped on every cycle, and every test that exercised only one of the
    two halves stayed green.
17. **A voided promise never breaches**, pinned by *two* fixtures that overlap
    on every row real code produces and so cannot pin each other: a row with
    `voided_at` set but a non-terminal status, and a terminal ticket with
    `voided_at` NULL (the pre-`0038` backlog migration `0038` deliberately did
    not backfill). Dropping either filter must fail exactly one of them.
18. **`extra="forbid"` on every request model** (§9.4) — an unknown key is a
    422, asserted over HTTP rather than against the service, because the
    defect it prevents is one that only exists on the wire path.

---

## 11. Migration notes

Migration `0033`, forward-only: six new tables, two permission codes with their
role grants, and one new event type registered in `app/core/events.py`. Two
changes to existing services (`tickets.update_ticket` and
`transitions.assign_ticket` each gain the system-actor path, §1, §6.0). No
existing column is altered.

Migration `0034`, forward-only, added by the Task 1 review: two CHECK
constraints and no schema change otherwise — `ck_rule_no_sla_breach_to_requester`
on `notification_rules` (§6.1) and `ck_sla_calendar_window_weekday_valid` on
`sla_calendar_window` (§2.2). `0033` is already shipped and is not edited.

Migration `0035`, forward-only, added by the Task 3 review (finding F2):
relaxes `ck_sla_calendar_window_start_before_end` from plain
`start_time < end_time` to `start_time < end_time OR end_time = '00:00:00'`
(§2.2) — the original CHECK made the midnight-end-of-day sentinel physically
impossible to store. `0033` and `0034` are already shipped and are not
edited.

Migration `0038`, forward-only, added by Task 8 (the ticket-lifecycle wiring):
`ticket_sla.voided_at` (§2.5.2), plus `portal_user`'s grants and its
restrictive `portal_isolation` policy on `ticket_sla` (§1's portal note). One
revision for both because both land on the same table for the same reason —
this is the revision that first makes `ticket_sla` a row real tickets actually
get. `0033`–`0037` are already shipped and are not edited.

`test_schema_drift.py` compares `Base.metadata` to the live schema and must
pass with the new tables. It deliberately does **not** compare indexes, so
`sla_policy`'s four partial unique indexes and `ticket_sla`'s two sweep indexes
are declared in `app/modules/sla/models.py`'s `__table_args__` as well as in the
migration — otherwise `alembic revision --autogenerate` would propose dropping
all six, including the ones enforcing policy uniqueness.

### 11.1 Amendment (build review): the migrations §11 does not list

§11 records `0033`, `0034`, `0035` and `0038`. Three more shipped, each
forward-only and each leaving every earlier revision unedited:

* **`0036`** — scopes `sla_policy`'s four partial unique indexes to `AND
  active`. Without it §9's retirement path was a dead end: a policy
  `delete_policy` refuses to delete could be deactivated but never
  *replaced*, because the retired row went on occupying its own `(queue,
  priority)` level and the collision message read "an active policy already
  exists" when none was active. §3 only ever considers active policies, so
  "at most one ACTIVE policy per level" is the invariant worth enforcing, and
  an inactive policy is now outside every arbiter index.
* **`0037`** — `ticket_sla.paused_minutes_total` (§2.5.1).
* **`0039`** — redefines the two breach-sweep indexes (§2.5.3, §7.1). No data
  change and no schema change beyond the two index predicates.

The paragraph above applies to `0039` with particular force: because
`test_schema_drift.py` does not compare indexes, **nothing in the suite would
have failed** had `0039` been written and `models.py`'s `__table_args__` left
on `0033`'s predicates. Both are updated, and they agree.

---

## 12. Worker

One beat entry, `sla-sweep`. Interval is a project constant, not per-org
configuration — breach detection granularity is an operational property, and a
per-org interval would let one org's setting delay another's escalation.

Nothing here touches the agent idle budget: no agent capability, no job type,
no gateway registration.

---

## 13. Deferred (explicitly not in v1)

- **Per-customer calendar assignment** (§0.1) — needs an end-customer entity
  this platform does not have.
- **SLA reporting and dashboards.** The data model supports them; the surfaces
  are their own design.
- **Multiple concurrent SLAs per ticket.** `ticket_sla.ticket_id` is UNIQUE.
- **"Time to next response"** beyond the first — needs a per-exchange clock and
  a rule for which exchanges count.
- **Partial holidays** (half-days) — expressible today only by leaving the day
  normal.
- **Pause on arbitrary custom statuses** — the pausing statuses are the two the
  ticket core defines as "blocked on someone else".

---

## 14. Related documents

- `docs/modules/service-desk/spec.md` — ticket core; §14 sequences this
  sub-project, §2 names the queue as the SLA anchor, §3.1 makes `pending`
  first-class for this clock, and §3.3 pins `resolved_at`'s meaning.
- `docs/modules/notification/spec.md` — delivers `ticket.sla_breached`.
- `docs/03-security-model.md` — the invariants this spec applies.
