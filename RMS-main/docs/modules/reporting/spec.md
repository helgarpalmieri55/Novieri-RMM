# Module Spec — Reporting & Analytics

> **There is no scoping brief for this module.** `docs/modules/` has no
> `reporting.md`, and `docs/06-roadmap.md` has no Reporting & Analytics phase.
> The module exists in this repo's documentation only as a dependency named by
> Phase 17's row ("read/analyze mode ships early … pair it with Reporting &
> Analytics") and as a recorded absence in
> `docs/modules/ai-admin/spec.md` §15.2: *"There is no Reporting & Analytics
> module, in `docs/modules/` or in `backend/app/modules/`. The brief's 'share
> the query layer rather than duplicating it' has nothing to share with."*
> This spec is what that sentence is waiting for. §0 therefore carries more
> weight than usual: there is no prior decision to inherit.
>
> Control-plane only — no agent capability, no new job type, no signed-job
> path, no Celery beat entry.
>
> **This module owns no facts.** It stores no measurement, computes no value a
> module could not compute about itself, and adds no table that another module
> must keep current. It owns *how a cross-module question is asked, who is
> allowed to ask it, and what the answer is allowed to contain* — and nothing
> else.

---

## 0. Resolved design decisions

Eleven decisions. Each rules out a family of designs, so they are recorded
before the design rather than inside it. Every number quoted below was measured
against this repo at HEAD (`3b62966`), not taken from the brief; §16 lists the
places where the measurement and the brief disagreed.

### 0.1 Reporting is a fixed catalogue of code-defined reports, not a query builder

A report is a `ReportDefinition` in a Python module compiled into the image
(§3): a key, a typed params model, a typed row model with a description on
every field, a declared set of source tables, a declared set of required
permission codes, and one bounded query. Adding a report is a code change and a
release.

Rejected: **an ad-hoc query surface** — saved queries, a filter/group-by
builder, a `POST /reports/query` taking a structure the caller composes. It is
the obvious product answer and it fails every property this module exists to
establish. It cannot be typed (the response shape is the caller's), cannot be
self-describing (there is no schema to describe), cannot be bounded except by
guesswork, and cannot be authorized — §0.5's whole mechanism is that a report's
permission requirement is derivable from the tables it reads, and a query the
caller composes has no fixed table set until it runs.

This is deliberately the same shape as
`app/modules/ai_admin/tools/registry.py`'s allowlist, for the same stated
reason: *"whatever compromises the database would otherwise choose the agent's
capabilities."* A saved-query table would put the reporting surface's shape
under `app_user`'s INSERT rights.

**The cost, stated plainly:** every new report needs an engineer, a PR and a
deploy. An organisation that wants "the same report but grouped by site" waits
for a release. That is the price of the four properties above, and the growth
path (§15) is *more reports*, never a builder.

### 0.2 Reporting inherits the monitoring boundary unchanged, and does not cross it

`docs/00-vision.md` §63–67, `docs/01-architecture.md` §169, `docs/05-agent-spec.md`
§310, `docs/modules/endpoint-health-alerting.md` §29–31 and `docs/06-roadmap.md`
Phase 16 all draw the same line: deep metric collection, time-series history,
SNMP polling and network topology are the **separate monitoring product**. This
module does not move that line by one inch.

Concretely, reporting **will not**: store or serve a metric series, resample or
downsample anything, chart a value over time from samples it collected, poll a
device or a switch, or model a topology. "How much RAM did this host use last
Tuesday at 14:00" is not a reporting question here; it is a monitoring question,
and the answer is a pointer to the monitoring platform.

What reporting *does* do with time is read timestamps that a **control-plane
module already stores as part of its own record of record** — `ticket_sla.
response_breached_at`, `backup_points.taken_at`, `device_patch_status.
detected_at`, `health_alert.opened_at`. Counting rows in a window over a
relational table an operator can already page through is not observability, and
it is not a series: nothing samples, nothing is stored per-tick, and deleting
the module deletes no history. That is the whole of the crossing, and it is the
same crossing `licenses`, `budget` and `sla` already make inside their own
modules.

Rejected: **a small time-series table "just for reporting trends"** — a
`reporting_metric(org_id, metric, ts, value)` table. It is one migration and
one sweep away from being the monitoring product, built badly, inside the
product that said it would not build it. The first person to ask for a
five-minute resolution wins the argument, and there is no principled place to
stop.

### 0.3 v1 computes live, on request, and owns no snapshot table

Every v1 report is a bounded SQL query executed inside `org_session` at request
time. There is no materialised layer, no snapshot table, no worker.

Rejected: **PostgreSQL materialised views.** They are unusable here for a
tenancy reason, not a performance one. RLS policies cannot be attached to a
materialised view in PostgreSQL 16 (`deploy/docker-compose.yml` pins
`postgres:16`) — `CREATE POLICY` requires a table — and a matview's contents are
computed at `REFRESH` time under the refreshing role's rights, so the underlying
tables' policies do not filter what a later reader sees. A matview spanning
`devices`, `health_alert` or `ticket` would be a single object holding every
tenant's rows with nothing but application code between it and a caller. That is
invariant #5 inverted: the whole point of `org_session` pinning
`app.current_org` (`app/core/tenancy.py`) is that the database, not the
application, does the constraining. **Build task: confirm this against the
deployed server version before relying on it; if a future PostgreSQL supports
RLS on matviews, this rejection is re-openable and nothing else in the spec
changes.**

Rejected for v1: **a scheduled snapshot layer.** The codebase already shows
exactly what it costs. `app/worker.py::licenses_compliance_sweep_all_orgs`
writes a `computed_at`-stamped `LicenseComplianceSnapshot` row per org per
product per day, and its own docstring states the failure:

> *"a gap in a compliance series is not recoverable by the next run, because the
> snapshot is `computed_at`-stamped."*

That is why every `*_all_orgs` shell in `app/worker.py` now carries
`_OrgCycleFailures` containment — one org's bad data used to cost the other 299
their position for that day, permanently. Per-org containment makes the hole
smaller; it does not make it repairable. Reporting adding a second family of
`*_at`-stamped series would multiply that class of permanent hole across every
module it touches, in exchange for latency this module has not yet measured a
problem with.

**The cost, accepted and bounded rather than hidden:**

1. **Slow reports on large orgs.** Mitigated, not wished away: every report
   query runs under `SET LOCAL statement_timeout` issued inside the reporting
   service's own `org_session` block (§6.4). On expiry the caller gets a typed
   `504` naming the report and the window, never a partial page. No
   `statement_timeout` is set anywhere in this codebase today, so this is a new
   pattern — but it is one statement inside this module's own session, not a
   change to `org_session`, and it forks nothing.
2. **Reports are "as of now" and are NOT point-in-time reproducible.** This is
   sharpest for `sla-attainment`: `ticket_sla` is *mutable*, and SLA spec §0.3
   requires a human re-triage to recompute a deadline **from ticket start**, so
   a ticket that read "met" last month can read "breached" today. A live
   attainment report is therefore an honest statement about the current record,
   not a frozen historical fact. Every report response carries
   `meta.reproducible: false` and the UI renders "as of <timestamp>" rather than
   a bare month label. Anyone who quotes a number from it to a customer is
   quoting today's record of last month, and the API says so.
3. **A snapshot cannot be retrofitted backwards.** When §15's growth path adds
   snapshots for the reports that need reproducibility, they will start from the
   day they ship. There is no backfill for a mutable source, and the spec does
   not pretend otherwise.

If a snapshot layer is later built, it must be (a) per-org and written inside
`org_session`, (b) keyed `(org_id, report_key, period)` with an **upsert**, so a
missed period is repaired by re-running rather than lost forever — the explicit
contrast with `license_compliance_snapshot`, and (c) contained per org with
`_OrgCycleFailures`.

### 0.4 A report is authorized by `reporting.read` **AND** every source module's read code

This is the hard one, and the brief is right about why: `org_session` scopes by
org and nothing else. RLS can express "this row belongs to this tenant". It
cannot express "this role may see licence cost but not ticket bodies", because
the policies key on `app.current_org` and there is no role GUC. `app/api/deps.py::
require_permission` is the only thing that has ever decided *what* a caller may
read, and it decides it per route, with one code.

**The rule:** a report route is gated on `reporting.read` through the kernel's
`require_permission`, and — before any data query runs — the report runner
requires the caller to hold **every** code in that report's declared
`requires` set. Missing any one is a `403` naming the missing codes.
`reporting.read` is necessary and never sufficient. It grants access to the
*catalogue*, not to any org's data.

`requires` is not hand-written prose. Each definition declares the tables its
query reads; a module-level `TABLE_PERMISSIONS` map names the read code that
governs each table; `requires` is the union, and a test (§12.4) compiles each
report's statement, walks the froms, and fails the build if the query touches a
table the definition did not declare or the map does not cover. That is what
makes "an operator never sees through a report what the API would refuse them
directly" a checkable property rather than an intention.

Rejected: **per-report permission codes** (`reporting.exec_summary`,
`reporting.finance`). It is the conventional answer and it is a read-through
hole with a friendly name. It creates a second authorization vocabulary in which
an org-admin granting "the reporting role" has no way to know they just granted
sight of ticket subjects and licence spend to someone the ticket and licence
APIs refuse. The first time the two vocabularies disagree, the report wins,
silently.

Rejected: **degrade instead of deny** — return the report with the
unauthorized columns nulled or the unauthorized sources dropped. Two failures.
First, an aggregate leaks through its denominator: "devices with no backup: 12"
plus a device count the caller *is* allowed is most of the backup report. Second,
silently dropping content on the way out is precisely the exemption mechanism
`ai_admin/tools/registry.py` removed (`sensitive_fields`, *"one word in this
dict turned a fail-closed block into a silent drop"*). Denial is loud; degrading
is not.

**The cost, accepted:** the conjunction is coarse. A service-desk manager who
holds `ticket.read` and `sla.read` but not `devices.read` cannot run
`fleet-posture`, even for the parts of it they could reconstruct by hand from
routes they can call. The remedy is narrower reports (more definitions, smaller
`requires` sets), not per-column relaxation. §15 records this as the expected
growth pressure.

### 0.5 The conjunction is enforced in the service, and the route still reports one code to the router

`app/core/rbac.py::route_permission` recovers a route's gate by reflection over
`require_permission`'s closure, and that is what makes RBAC completeness
*derivable from the router* (invariant #6) rather than a hand-maintained list.
It returns **one** code, and it returns the first matching dependency it finds
while walking the dependant tree.

So the conjunction may not be expressed as a second `require_permission` on the
same route: two closures in one tree make `route_permission`'s answer
order-dependent, and every RBAC-completeness test in the repo would then be
asserting something no reader could predict.

Reporting therefore uses exactly one `require_permission("reporting.read")` per
route, plus a **separately named** dependency factory (`require_report_sources`,
whose qualname is deliberately *not* in `_PERMISSION_DEPENDENCY_QUALNAMES`) that
performs the conjunction. `route_permission` deterministically returns
`reporting.read`; the conjunction runs before the handler body; a test asserts
both facts for every report route.

There is a live counter-example in this repo and this decision exists partly to
avoid repeating it. `app/modules/remote/api.py` defines a local
`require_any_permission(*codes)` for its two read routes. It is a correct OR-gate
and it is **invisible to `route_permission`**, which returns `None` for both —
so `GET /api/v1/remote/sessions` and `GET /api/v1/remote/sessions/{session_id}`
read as *ungated* to every router-derived RBAC check in the codebase, though
they are not. (Measured: 3 of 173 `GET` routes under `/api/v1` return `None`
from `route_permission`; the third is `GET /api/v1/me`.) **That is a finding
about the remote module, not a defect this module fixes** — it is recorded here,
in §16, and belongs in that module's own cycle. Reporting's rule is: never fork
`require_permission`, never hide a gate from the router.

Rejected: **teaching the kernel a conjunction gate** (a
`require_all_permissions` in `app/api/deps.py` and a `route_permission` that
returns a set). It is the cleaner long-term shape and it is a kernel change to
the one function every module's completeness test and the AI registry's
`test_every_recorded_permission_is_correct` depend on, made on behalf of one
module that does not yet exist. If a second module needs a conjunction, that is
the moment to propose it — with the migration of every caller in the same PR.
§16 records it as the one kernel change this module deliberately did **not**
make.

### 0.6 A report is org-wide, and a group-scoped grant does not run it

`app/core/rbac.py::user_permission_codes` counts a `user_roles` row with a
`device_group_id` **only** when the question is asked about that same group;
`require_permission` asks with `device_group_id=None`. So a technician whose
`devices.read` is scoped to one device group already fails every report gate
today, without this spec doing anything.

That is the correct behaviour and it is made explicit rather than left as an
accident: a fleet aggregate computed for someone entitled to part of the fleet
is either wrong (the denominator includes devices they may not see) or a leak
(it tells them how many there are). Group-scoped reporting needs RBAC to express
"aggregate, limited to group G", which it cannot; §15 records it as **blocked**,
not deferred.

### 0.7 Running a report is not audited; exporting one is

Invariant #4 enumerates the privileged actions: job dispatch, credential access,
remote-session start/stop, config change, policy change. Reading is not on that
list, and every module in this repo already treats it that way — no `GET` in
`app/modules/*/api.py` writes an audit event for having been read.

The cost of the alternative is measurable, not theoretical.
`app/core/audit.py::record` takes `pg_advisory_xact_lock(hashtext('audit:' ||
org))` so the chain cannot fork, which means every append in an org is
serialised. Auditing every report run puts a dashboard's refresh loop — and, once
§10 lands, every AI turn that calls a reporting tool — on the audit chain's
critical path, in a table that is append-only by trigger and revoked UPDATE/
DELETE for `app_user` (`docs/07-backup-restore.md` §4) and therefore cannot be
pruned to recover. The chain would be dominated by "someone looked at a
dashboard", which makes the privileged events harder to find, not easier.

**Export is audited** (`report.exported`, with the report key, the validated
params, the row count and the format — never the rows). An export is the moment
control-plane data leaves the platform in a form nobody can revoke, which is the
same class of act as a credential checkout, and it is rare enough that the
advisory lock is free.

Consequence, stated because it interacts with §10: an export route is a `GET`
that performs a write, so it is not method-honest and is permanently excluded
from the AI tool surface. Note in passing that `GET` in this codebase is already
not side-effect-free — `app/api/deps.py::auth_context` updates
`user.last_login_at` on **every** authenticated request and appends an
`auth.login` audit event on first sight of a user — so this is a difference of
degree; the exclusion stands on the honest reading, not the literal one.

### 0.8 Export is CSV only, is capped, and refuses rather than truncates

CSV, via `PlainTextResponse(media_type="text/csv")`, matching the existing
exporters in `ipam`, `inventory`, `budget`, `backup`, `patch` and
`security_logs`. No XLSX (a binary writer is a new dependency and a new parser
surface for whoever opens it), no PDF (a layout engine to render numbers already
on the screen), no JSON export (the API *is* the JSON).

Every existing exporter is unbounded — `export_subnets_csv` and friends return
the whole table. Reporting's exports are **capped**, and on exceeding the cap
they return a typed `413` naming the ceiling and the parameter to narrow, rather
than a truncated file. A truncated CSV is indistinguishable from a complete one
once it is in a spreadsheet, and a report that is quietly missing rows is a
wrong-number defect, not a paging inconvenience.

Formula-injection escaping reuses `app/core/csv_export.py::escape_formula`,
applied **per column** to the definition's declared free-text columns only,
exactly as `budget/api.py::_csv_response` does — never blanket over a row, which
would corrupt signed amounts and dates for machine readers.

**Verified gap, and what this module does about it.** `escape_formula`'s
`_DANGEROUS_PREFIXES` is `("=", "+", "-", "@")`. It does **not** guard a leading
tab or carriage return, both of which some spreadsheet importers also treat as
formula lead-ins, and it does not strip embedded NUL or bare `\r`. Reporting
therefore normalises its own free-text columns (strip leading `\t`, `\r`, `\n`
and any NUL) *before* calling `escape_formula`. Extending the shared helper
would be the better fix and it is **not** done here: six modules call it
(`ipam`, `inventory`, `budget`, `backup`, `patch`, `security_logs` — its own
docstring still says five, which is itself out of date), one of them (budget's
Odoo export) is a pinned wire contract read by an ERP importer, and changing a
shared escaper on behalf of a module that does not yet exist is not a reviewed
change. §16 files it as a proposed kernel change with
its own review.

**An export must never contain:** any secret or key material (`credentials`,
`GET /licenses/{id}/key`, vault ciphertext, enrolment tokens, agent certificate
material); notification channel targets or the webhook allowlist, or the
email-intake mailbox configuration (reconnaissance for the SSRF surface
`app/core/egress.py` bounds); ticket comment bodies, internal notes, or
attachment content; audit `hash`/`prev_hash` columns (§0.9); a row from another
org; or any column not on the definition's declared column list. The last clause
is the enforceable one — a test asserts the CSV header equals the definition's
declared columns, so a column cannot arrive in an export without arriving in the
definition first.

### 0.9 Reporting reads the audit log only through counts, and never re-serves or re-shapes it

`audit_events` is hash-chained, HMAC-authenticated, `INSERT`-only for
`app_user`, and blocked from `UPDATE`/`DELETE` by trigger
(`app/core/audit.py`, `docs/07-backup-restore.md` §4). Its evidentiary value is
that the chain verifies — `GET /api/v1/audit/verify` is the artefact, not any
listing derived from it.

Therefore: no v1 report reads `audit_events` at all (§5 explains why the obvious
"control activity by actor" report is deferred), and when one does, three rules
bind it. It may return **aggregates and identifiers only**, never `hash` or
`prev_hash` — a chain link outside the chain invites someone to treat a CSV as
verifiable, and it is not. It may never be presented as *the* audit log; the
audit API is. And reporting writes to `audit_events` only through
`audit_service.record`, like every other module, never by any other path.

### 0.10 The reporting API is the module that actually satisfies the five properties, by construction

`docs/modules/ai-admin/spec.md` §0.3 defines "clean enough to be a tool" as five
mechanical properties, and the roadmap calls the AI persona "a forcing function
for clean, well-documented module APIs". Measured against this repo at HEAD
(script in §16), across **173** `GET` routes under `/api/v1`:

| | count |
|---|---|
| `GET` routes under `/api/v1` | 173 |
| carrying `summary=` on the decorator | **0** |
| carrying an endpoint docstring (FastAPI's `description`) | 23 |
| typed `response_model` (a model or `list[Model]`) | 154 |
| gated (`route_permission(route)` is not `None`) | 170 |
| `list[Model]` routes with no `limit` query param | 76 |
| passing all five as `test_every_registered_tool_passes_the_five_properties` implements them | **3** |
| passing all five under the strict reading of property 5 (`summary=` present **and** every field carries `Field(description=...)`) | **0** |

The three survivors are `GET /api/v1/health/alerts`,
`GET /api/v1/devices/{device_id}/health` and `GET /api/v1/ai/status`. There is
exactly **one** `summary=` in the entire backend and it is a dict argument in
`inventory/api.py`, not a route decorator; there is exactly **one**
`Field(description=...)` in any module's `schema.py` and it is in `sla`. Every
survivor passes property 5 through the docstring fallback, never through the
property as written.

The forcing function has, so far, forced nothing. This module makes it real for
one surface: **reporting's routes are generated from the definition registry in
a loop** (§6.1), so `summary=`, the typed `response_model`, the described row
fields, the `limit`/`offset` with `le=`, and the gate are supplied by the
definition or the loop and cannot be omitted by a route author — there is no
route author. A report that reaches the router has the five properties because
there is no code path by which it could not.

> **AMENDED 2026-08-14 — AND THIS IS THE UNCOMFORTABLE HALF.** Everything above
> is still true and still verified. **None of this module's five routes can be
> registered as an AI tool**, and the reason is a property none of the five
> covers.
>
> `docs/modules/ai-admin/spec.md` §0.3 gained a sixth property — **closed** —
> which `scrub.py` had enforced since the day it shipped and which no document
> stated. `ReadTool.response_fields` is pinned *equal* to the response model's
> own field names, so a payload that can carry a key which is not a model field
> name has no legal declaration at all. Two of this module's design decisions
> are exactly that shape:
>
>   - `ReportMeta.report_key` and `CatalogueEntry.key` are **secret-shaped**.
>     `looks_like_secret_key` matches word-wise and `key` is a secret word, so
>     `scrub` blocks any payload carrying either — verified against the real
>     gate, which reported `BLOCKED at 'meta.report_key'`.
>   - `ReportPage.totals` is `dict[str, int]` and `CatalogueEntry.params_schema`
>     is `dict[str, Any]`. Their keys are `SELECT` column labels and JSON Schema
>     keywords — data, not schema. Measured: `fleet-posture` alone contributes
>     `devices`, `stale_devices`, `devices_with_open_alerts`, none of them
>     declarable.
>
> **Re-measured across the whole router on the same day:** 188 `GET` routes (not
> 173), 10 passing properties 1–5 (not 3), and **3** passing all six. The
> superseded row in the table above — "passing all five … **3**" — was correct
> when written.
>
> **What this does to §0.10's argument.** It survives, narrowed and more
> honest: generating routes from a registry really does make properties 1–5
> unfailable, and that really is more than any hand-written module achieved.
> What it does not do — what nothing in this section ever claimed, and what a
> reader would reasonably have assumed — is make the routes *usable by the
> agent*. "Passes the properties" and "can be a tool" were the same sentence
> until 2026-08-14. They are not.
>
> The bill for changing that answer is in §10.1.

Rejected: **hand-written route functions with a review checklist**. That is what
every other module has, and the table above is what it produced.

### 0.11 Scheduled reports ship nothing in v1, because the only delivery substrate would leak the conjunction

The obvious v1 feature — "email me this every Monday" — is **not** built, and
the reason is a property of the code rather than a scoping preference.

Delivery in this platform means the notification core: publish to the
transactional outbox (`app/core/events.py`), and the worker routes and delivers.
`DomainEvent.payload` is a JSONB column on `domain_events`, and a webhook
delivery forwards it verbatim (`notification/delivery.py::_event_payload`:
*"This module does not additionally filter `event.payload`"*). Putting report
rows in that payload would:

- write a copy of the report's data into a table governed by `org_id` and
  nothing else, outside the §0.4 conjunction that is the only thing making the
  report safe — today no route serves `domain_events.payload`, but the copy is
  one convenience route away from being served under `notification.read`; and
- egress those rows to a webhook target chosen by whoever holds
  `notification.admin`, which `rbac.py` itself flags as *"HIGH PRIVILEGE: this
  grant controls where control-plane data is sent"*.

Email is no better: `notification/delivery.py` builds an `EmailMessage` with a
plain `(subject, body)` from a per-type renderer and has **no attachment path
at all**; adding one is forking the delivery contract, which `CLAUDE.md`
forbids.

So the deferred design, recorded now so it is not re-invented badly: a scheduled
report publishes an event carrying **only** `report_key`, the window, the row
count and a link — no rows, no aggregates, no names. The recipient follows the
link and is re-authorized at that moment against §0.4's conjunction, so the
notification carries no entitlement of its own. Registering that event type is
not free and the obligations are listed here because they are easy to miss:
`app/core/events.py::EVENT_TYPES` must gain the type; `_EMAIL_RENDERERS` must
gain a renderer (`test_every_registered_event_type_has_an_email_renderer`
asserts the dict's keys **equal** `EVENT_TYPES`); a pinned webhook wire sample
must be added (`test_every_registered_event_type_has_a_pinned_wire_format_
sample`); and a staff-only decision must be taken with, if it is staff-only, the
matching `notification_rules` CHECK constraint and its migration.

What remains genuinely blocked even then: an unauthenticated deep link would
have to be a signed, expiring, single-use token — a new credential class, which
is a kernel concern and not this module's to invent. §15 records it.

### 0.12 The v1 set is four reports, each of them cross-module

Reporting must not become a second front door to data a module already serves.
`licenses/reporting.py::compliance_summary`, `budget/reporting.py::variance`/
`position`/`forecast`, `ipam/allocation.py::utilization` and `sla/views.py`
already exist, are already gated on their own codes, and are already better
placed than anything this module could write — they live beside the invariants
they encode.

So a report earns its place here only if **no single module can answer it**:
every v1 report spans at least two modules and therefore has a `requires` set
with at least two codes. That also makes §0.4's conjunction the ordinary case
rather than an edge case — the mechanism is exercised by every report, on every
run, from day one.

Rejected: **wrapping the existing single-module reporters** to get a bigger v1.
It would double the number of places a licence position is computed, and the
second one would drift.

---

## 1. Purpose & boundary

**Reporting & Analytics is the cross-module read layer.** It answers questions
that span modules, over data those modules already own, under an authorization
rule that is the conjunction of those modules' own read permissions, through an
API deliberately built to the standard the rest of the codebase has not yet
reached.

### 1.1 In scope

- A code-defined catalogue of bounded, typed, described, gated cross-module
  reports (§3, §5).
- One HTTP route per report, plus one CSV export route per report, generated
  from the definitions (§6).
- The authorization rule and its enforcement (§4).
- The audit story for exports (§8).
- The tool surface Phase 17's read mode consumes (§10).

### 1.2 Explicitly out of scope

- **Time-series, SNMP, topology, metric collection** — §0.2; the monitoring
  product.
- **Charts, dashboards-as-data, pivot tables, drill-down state** — the frontend
  renders; the API returns rows and totals (§11).
- **Ad-hoc queries and saved queries** — §0.1.
- **Any module's own single-module views** — §0.12.
- **Writes of any kind**, other than the export audit event (§0.7).
- **The end-user portal.** No report is reachable from a portal principal;
  reporting registers no portal route and uses `org_session`, never
  `portal_session`.
- **Cross-org / MSP roll-up.** It requires `admin_session`, which bypasses RLS.
  §15 records it as blocked.

### 1.3 Relationship to the modules it reads

Reporting is a consumer with no privileges of its own. It reads module tables
directly, inside `org_session`, because a loopback HTTP fan-out across five
modules per report would multiply request latency by five and re-authenticate
five times to reach the same rows. It does not import module *services* — it
declares tables and reads them — so a module refactoring its service layer does
not break a report, and a module changing a table does (loudly, in §12.4's
test).

Reporting adds nothing to any module: no column, no event, no job type, no
sweep. If a report cannot be built without a module storing something new, the
report is **not shipped** and the requirement is filed against that module.

---

## 2. Where the numbers come from

Per §0.3: live, at request time, inside `org_session`, under a statement
timeout, bounded and paged.

**Freshness is a property of the source, and the response says which.** Every
report response carries `meta.generated_at` and `meta.sources`, where each
source names the module and the recency semantics of the data it read:

| source | recency the report inherits |
|---|---|
| `devices` / `agents` | `last_seen_at`, updated by the gateway on agent traffic |
| `health_alert` | current alert state; open/cleared as of the last check tick |
| `device_patch_status` | current per-device patch state, as last rewritten by a scan. **Not `detected_at`**, which dates a patch's first detection on the device and never moves again |
| `jobs` | as of each job's own `finished_at`; a job that has not reached a terminal state carries none |
| `backup_points` | as of the last completed run (`taken_at`) |
| `ticket` / `ticket_sla` | current, and **mutable** — see §0.3 cost 2 |

This matters more than it looks: "12 devices have no backup in 30 days"
computed from `backup_points` says nothing about devices whose agent has been
offline for 60 days, and a report that does not surface its own staleness
invites that misreading. Each v1 report therefore returns an explicit
`stale_device_count` (or equivalent) alongside its headline figure rather than
folding unknowns into either bucket.

**What reporting will not do to make a number look better:** infer a value a
module did not record, carry a value forward across a gap, or interpolate. A
device with no patch scan is `unknown`, not `compliant`.

---

## 3. The report definition registry

A Python module compiled into the image (§0.1), shaped after
`ai_admin/tools/registry.py` and for the same reasons.

```python
# app/modules/reporting/definitions.py

@dataclass(frozen=True)
class ReportDefinition:
    key: str                      # stable, URL-safe, e.g. "fleet-posture"
    summary: str                  # becomes the route's `summary=` (property 5)
    description: str              # becomes the route's `description`
    params: type[BaseModel]       # query params; ConfigDict(extra="forbid")
    row_model: type[BaseModel]    # every field carries Field(description=...)
    sources: frozenset[str]       # every table the query reads, by table name
    requires: frozenset[str]      # DERIVED: union of TABLE_PERMISSIONS[t]
    max_limit: int                # the `le=` on the route's `limit`
    export_columns: tuple[str, ...]      # CSV header, in order
    export_free_text: frozenset[str]     # columns escape_formula is applied to
    export_row_cap: int                  # §0.8; exceeded -> 413, never truncate
    build: Callable[..., Select]  # returns the bounded statement
```

`TABLE_PERMISSIONS: Mapping[str, str]` maps every table a report may read to the
permission code that governs reading it — `devices` → `devices.read`,
`health_alert` → `health.read`, `device_patch_status` → `patch.read`,
`backup_points` → `backup.read`, `ticket` → `ticket.read`, `ticket_sla` →
`sla.read`, and so on. A table absent from the map cannot be read by any report:
§12.4's test fails the build rather than defaulting to anything.

`requires` is computed from `sources` at import time and is not settable by a
definition author. A definition that could hand-write it could understate it.

**A denylist, for the same reason `ai_admin` keeps one alongside its allowlist.**
`_NEVER_A_SOURCE` names tables no report may read at any gate:
`credentials`, `org_vault_keys`, `enrollment_tokens`, `agents.cert_*` material,
`notification_channels` (webhook targets), the email-intake mailbox config, and
`audit_events.hash`/`prev_hash` (§0.9). A path can be renamed; the intent of
"never through a report" is stickier, and the tripwire must resolve to real
objects — `test_denylisted_sources_are_real_tables` pins it, because a denylist
whose names have drifted denies nothing.

The registry is exposed as a `MappingProxyType`, not a dict: a mapping any code
in the process can widen at runtime is not "compiled into the image".

---

## 4. Authorization

### 4.1 The check

```
403 unless  reporting.read ∈ granted
403 unless  definition.requires ⊆ granted
```

`granted` is `user_permission_codes(session, ctx.user_id)` — org-wide grants
only (§0.6). The 403 body names the missing codes, because a report the operator
cannot run is a permissions conversation and an opaque denial produces a support
ticket instead.

Order matters: `reporting.read` is checked by the route dependency
(`require_permission`), the conjunction by `require_report_sources(key)` (§0.5),
and **both** complete before the handler opens a data session. No report query
runs for an unauthorized caller, so a timing difference cannot report on data
the caller may not see.

### 4.2 Why this cannot leak past the direct API

The property to hold is: *no report shows an operator anything the module APIs
would refuse them.* It holds because (a) every table a report reads is declared,
(b) every declared table maps to the read code that governs it, (c) the caller
must hold all of them, and (d) (a) is verified against the compiled SQL rather
than trusted. A report can therefore only aggregate rows the caller could have
paged through directly.

The residual risk is a table whose *route-level* gate is finer than its
table-level mapping — a module that serves one table under two codes depending
on the column selected. `licenses` is exactly that shape: `licenses.read` for
the catalogue, `licenses.keys.read` for the decrypted key. The mapping is
therefore **column-aware where a module is**: `TABLE_PERMISSIONS` names the
weakest code that governs *any* column of the table, and the encrypted/secret
columns are on `_NEVER_A_SOURCE`'s column-level tail. A build task in §12
enumerates every column named in a report's `export_columns` and asserts none is
on that tail.

### 4.3 The single new permission code

`reporting.read` — *"View cross-module reports. Grants NOTHING on its own: every
report additionally requires the read permission of every module it draws from,
so a user holding this alone gets a 403 from every report."* One code, seeded by
migration (§13), with the same "grants nothing on its own" phrasing `ai.use`
already uses in `PERMISSIONS`, because it is the same kind of code.

No `reporting.admin`. There is nothing to administer: the catalogue is code,
there are no schedules in v1, and there is no per-org configuration. Adding an
admin code with nothing behind it would be a grant an org-admin has to reason
about for no benefit.

No separate `reporting.export`. Export returns exactly the rows the same caller
can already see on screen, and every existing exporter in this repo is gated on
its module's own read code (`ipam.read`, `budget.read`, `inventory` likewise).
A separate code would be ceremony without a boundary.

---

## 5. The v1 report set

Four reports. Each spans at least two modules (§0.12), each reads only tables
that exist today, and each has a distinct `requires` set — so the conjunction is
exercised four different ways.

### 5.1 `fleet-posture` — devices × health

`requires = {devices.read, health.read}`.
One row per device: `device_id`, `hostname`, `os_family`, `os_version`,
`device_group_id`, `agent_status`, `last_seen_at`, `stale` (no agent contact
within the window), `open_alerts_critical`, `open_alerts_warning`,
`newest_alert_opened_at`. Plus totals: devices, stale devices, devices with at
least one open alert.
Params: `os_family`, `device_group_id`, `stale_after_days` (default 7),
`severity_at_least`, `limit`/`offset`.
Sources: `devices`, `agents`, `health_alert`.
*Why v1:* it is the question every operator opens the product to ask, and no
module answers it — `health/api.py` answers it per device, `devices` answers it
without health.

### 5.2 `patch-compliance` — devices × patch

`requires = {devices.read, patch.read, jobs.read}`.
One row per device: `device_id`, `hostname`, `os_family`, `missing_approved`,
`missing_approved_security`, `oldest_missing_detected_at`, `applied_in_window`,
`failed_in_window`, `last_scan_at`, `scan_state` (`fresh` | `stale` | `never`).
Totals: devices covered, devices with zero missing approved patches, devices
never scanned.
Sources: `devices`, `device_patch_status`, `patches`, `patch_approvals`,
`patch_policies`, `device_groups`, `jobs`.

> **Amended 2026-08-21: `jobs` is a source, and `jobs.read` is therefore part of
> `requires`.** This section originally listed neither, on the assumption that
> `last_scan_at` could be read from `device_patch_status`. It cannot.
> `detected_at` is stamped when a status row is first created and no later scan
> rewrites it, so it dates the discovery of a *patch*, not the running of a
> *scan*; on a device whose patch set has not changed since enrolment it never
> moves, and the report dated a settled fleet to its enrolment day. The durable
> per-device record that a scan ran **and** that its document was ingested is a
> succeeded `patch.scan` job, because `JobEngine.complete` marks a job succeeded
> only after the patch module's handler returns without raising. No new column
> was added for this (§1.3: reporting adds nothing to any module); the fact
> already exists, in `jobs`, and reading it means declaring it. The widening is
> real but empty in practice — every seeded role holding `patch.read`
> (technician, auditor, read-only, admin) already holds `jobs.read`.
*Why v1:* "are we patched" is the single most-asked compliance question, and
answering it correctly requires joining approval state (per ring, per group) to
device state — which is precisely why nobody reconstructs it by hand correctly.
*Honesty requirement:* `scan_state = never` is its own bucket and is never
counted as compliant (§2).

### 5.3 `backup-coverage` — devices × backup

`requires = {devices.read, backup.read}`.
One row per device: `device_id`, `hostname`, `policy_present`,
`policy_enabled`, `repository_name`, `last_point_taken_at`, `last_point_state`,
`days_since_last_point`, `last_verified_at`, `last_restore_tested_at`,
`coverage` (`covered` | `stale` | `failing` | `unprotected`).
Totals per `coverage` value.
Sources: `devices`, `device_groups`, `backup_policies`, `backup_repositories`,
`backup_points`, `backup_runs`.
*Why v1:* the commercialization gate in `docs/06-roadmap.md` requires "backups
with **proven restores**, not just schedules", and `restore_tested_at` is a
column nothing currently surfaces in aggregate. This report is the evidence for
that gate.
*Excluded on purpose:* `backup_repositories.encryption_key_ref` and
`storage_ref` — a repository's storage target and key reference are
infrastructure identifiers, not coverage facts (§0.8).

### 5.4 `sla-attainment` — service desk × SLA

`requires = {ticket.read, sla.read}`.
Grouped, not per-ticket: one row per `(queue, priority)` over a closed window —
`queue_id`, `queue_name`, `priority`, `tickets`, `response_met`,
`response_breached`, `resolution_met`, `resolution_breached`, `void`,
`still_running`, `response_attainment_pct`, `resolution_attainment_pct`,
`breached_after_retriage` (rows whose `policy_source` is a human re-triage —
SLA spec §0.3's mitigation, finally rendered).
Params: `from`, `to` (required, max window 366 days), `queue_id`, `priority`.
Sources: `ticket`, `ticket_queue`, `ticket_sla`.
*Why v1:* SLA spec §0.3 accepts a real perverse incentive on the explicit
promise that attainment reporting will distinguish "we were slow" from "we
re-triaged at hour five". `policy_source` exists for that and nothing reads it.
*No ticket bodies, no subjects, no requester identity* — the report is counts by
queue and priority; a manager who wants the tickets follows
`GET /api/v1/tickets` with the same filters, gated as it already is.
*Reproducibility:* `meta.reproducible = false` (§0.3 cost 2).

### 5.5 Deliberately not in v1, each with its reason

- **Control activity / audit summary** (`audit_events` by actor and action).
  §0.9: an aggregate over the chain is a derived copy that verifies nothing, and
  the compliance artefact is `GET /api/v1/audit/verify` plus the existing
  security-logs export. It needs its own decision about what a count over
  tamper-evident data is *for*, not a quiet registry addition.
- **Licence position and spend.** `licenses/reporting.py::compliance_summary`
  and `budget/reporting.py` already answer these, gated correctly, inside the
  modules that own the arithmetic and the FX convention. Re-serving them here
  would be §0.12's second implementation. The genuinely cross-module version —
  spend against installed software against subscription seats — needs a decision
  about which module owns the base-currency conversion at the join, and that
  belongs in a financial-trio cycle.
- **Ticket volume / technician workload.** Single-module (service desk), and its
  natural home is the ticket core's own views.
- **IPAM utilisation roll-up.** `ipam/allocation.py::utilization` already
  computes it per subnet; an org-wide roll-up is a thin loop over an existing
  answer and is a candidate for the ipam module, not this one.
- **Anything with a trend line.** §0.2.

---

## 6. API

Prefix `/api/v1/reports`, router registered through
`app/modules/__init__.py::register_all` like every other module.

### 6.1 Routes are generated from the registry

```python
for definition in REGISTRY.values():
    router.add_api_route(
        path=f"/{definition.key}",
        endpoint=_make_handler(definition),
        methods=["GET"],
        response_model=ReportPage[definition.row_model],
        summary=definition.summary,
        description=definition.description,
        dependencies=[
            Depends(require_permission("reporting.read")),
            Depends(require_report_sources(definition.key)),
        ],
    )
```

This loop is the mechanism behind §0.10. `summary` cannot be forgotten because
the definition must supply it; the response model cannot be untyped because it
is built from `row_model`; the gate cannot be omitted because the loop supplies
it; `limit` cannot be unbounded because the params model declares
`le=definition.max_limit`. There is no per-report route function for anyone to
write badly.

### 6.2 The route table

| Method | Path | Gate | Response |
|---|---|---|---|
| `GET` | `/api/v1/reports` | `reporting.read` | `ReportCatalogue` |
| `GET` | `/api/v1/reports/fleet-posture` | `reporting.read` + `{devices.read, health.read}` | `ReportPage[FleetPostureRow]` |
| `GET` | `/api/v1/reports/patch-compliance` | `reporting.read` + `{devices.read, patch.read, jobs.read}` | `ReportPage[PatchComplianceRow]` |
| `GET` | `/api/v1/reports/backup-coverage` | `reporting.read` + `{devices.read, backup.read}` | `ReportPage[BackupCoverageRow]` |
| `GET` | `/api/v1/reports/sla-attainment` | `reporting.read` + `{ticket.read, sla.read}` | `ReportPage[SlaAttainmentRow]` |
| `GET` | `/api/v1/reports/{key}/export.csv` | as the report | `text/csv`, audited |

`GET /api/v1/reports` lists every definition with its `summary`, its
`params_schema`, its `requires` set, `entitled: bool` and
`missing_permissions: list[str]` **for the calling user**. It exposes no org data
— the catalogue is platform code — and it turns a 403 into an explicable one. It
is gated on `reporting.read` alone.

`params_schema` is the JSON Schema of the report's query parameters, **derived
from the same Pydantic model the route validates against** so the client cannot
be handed a second, drifting copy of the contract (§17.11). It describes the
gate; it never replaces it.

### 6.3 The response envelope

```python
class ReportSource(BaseModel):
    module: str = Field(description="The module whose tables this figure came from.")
    permission: str = Field(description="The read permission that governs it.")
    recency: str = Field(description="What the timestamps in this source mean, "
                                     "e.g. 'as of each device's last patch scan'.")

class ReportMeta(BaseModel):
    report_key: str = Field(description="Stable identifier of the report definition.")
    generated_at: datetime = Field(description="UTC instant the query ran. Rows "
                                               "reflect the record at this moment.")
    reproducible: bool = Field(description="False when a source is mutable, so "
                                           "re-running this report for the same "
                                           "window may return different numbers.")
    window_from: datetime | None = Field(description="Inclusive start of the window, "
                                                     "or null for a point-in-time report.")
    window_to: datetime | None = Field(description="Exclusive end of the window, or null.")
    total: int = Field(description="Total matching rows before limit/offset.")
    limit: int = Field(description="Maximum rows returned by this request.")
    offset: int = Field(description="Rows skipped before the first returned row.")
    sources: list[ReportSource] = Field(description="Every module this report read, "
                                                    "with the recency of its data.")

class ReportPage(BaseModel, Generic[RowT]):
    meta: ReportMeta = Field(description="How, when and from what this page was built.")
    totals: dict[str, int] = Field(description="Report-level aggregates over ALL "
                                               "matching rows, not just this page.")
    rows: list[RowT] = Field(description="One row per subject, ordered stably.")
```

A generic envelope rather than a hand-written page model per report: one place
where the meta fields are described, and one place a described field can be
added. `totals` are computed over the whole match, never over the page — a
paged total that changes as you page is worse than no total.

Ordering is stable and total (`(sort_key, id)`), so `offset` paging cannot skip
or duplicate a row between requests.

### 6.4 Execution

Handlers do exactly this, in order: validated params → conjunction already
satisfied by the dependency → open `org_session` → `SET LOCAL statement_timeout`
→ count → page → build the response. Business logic is in
`app/modules/reporting/service.py`; the generated handler is a shell.

`statement_timeout` is set with `SET LOCAL` inside the transaction, exactly as
`org_session` sets `app.current_org`, so it cannot leak to the next checkout of
that pooled connection. On timeout: `504` with the report key and the
suggestion to narrow the window — never a partial page, never a page silently
computed from a smaller window.

Every query carries an explicit `org_id ==` predicate **in addition to** RLS,
matching the discipline `notification/api.py` documents ("defense in depth, not
a fix for a reachable leak"). A report is a wide join; RLS is the guarantee and
the predicate is the thing a reviewer can see.

### 6.5 Errors

Typed, and named rather than generic: `403 report_permission_denied` (with
`missing`), `413 report_export_too_large` (with `cap` and `matched`),
`422` from the params model (unknown query parameters are rejected —
`extra="forbid"`), `504 report_timeout`.

---

## 7. Export

`GET /api/v1/reports/{key}/export.csv`, one per definition, generated by the
same loop and carrying the same two dependencies.

- Content type `text/csv` via `PlainTextResponse`, matching the existing
  exporters.
- Header row = `definition.export_columns`, in order. A test asserts the emitted
  header equals the declaration, so a column cannot appear in a file without
  appearing in the definition.
- `escape_formula` applied to `definition.export_free_text` only, after
  reporting's own leading-whitespace/NUL normalisation (§0.8).
- `NULL` renders as empty, never as `"None"`.
- Bounded by `export_row_cap`; over the cap, `413` naming the cap — never a
  truncated file (§0.8).
- Writes one `report.exported` audit event inside the same transaction as the
  read, with `target_type="report"`, `target_id=key`, and metadata
  `{params, row_count, format: "csv"}`. **The metadata never contains rows.**
- Excluded from the AI tool surface permanently (§0.7, §10.3).

---

## 8. Tenancy, RBAC, audit

**Tenancy (#5).** Every read is inside `org_session`, so RLS constrains it at
the database. No report uses `admin_session`; no report accepts an org
parameter; no report path contains an org placeholder. Cross-org reporting is
blocked (§15). The `org_id ==` predicate is additionally explicit on every
query (§6.4).

**RBAC (#6).** Server-side, before the handler, always: `reporting.read` through
the kernel's `require_permission` (so `route_permission` sees it and every
router-derived completeness test covers reporting), plus the source conjunction
through `require_report_sources`. Org-wide grants only (§0.6). The frontend
gates nothing — it reads `entitled` from the catalogue to grey out a card, and a
user who ignores that gets a 403.

**Audit (#4).** `report.exported` on every export. Nothing on a run (§0.7).
Reporting writes to `audit_events` only via `audit_service.record`, and reads it
in no v1 report (§0.9).

**Invariants #1, #2, #3** are untouched: this module dispatches no job, opens no
connection to an endpoint, and reads no vault material — the credential surface
is on `_NEVER_A_SOURCE` in its entirety, metadata included, for the same
conservative reason `ai_admin` gives (enumeration is reconnaissance).

---

## 9. Worker

**None.** Reporting adds no Celery task, no beat entry, and no `*_all_orgs`
shell in v1, because it stores nothing that needs computing on a schedule
(§0.3) and delivers nothing on a schedule (§0.11). This section exists to say
so, since every other recent module added one.

---

## 10. The seam with the AI admin agent (Phase 17)

`docs/modules/ai-admin/spec.md` §15.2 records that read mode had nothing to
share a query layer with. This is what it shares.

### 10.1 What reporting gives the agent

Four routes that pass the five properties by construction, plus a catalogue
route — the first module-scale addition to a tool set whose entire v1 is three
routes (§0.10). Each becomes a `ReadTool` entry in
`ai_admin/tools/registry.py` in the ordinary, deliberate, reviewed way; nothing
here auto-registers anything, and reporting does not import `ai_admin`.

> **AMENDED 2026-08-14: the reviewed act happened, and its answer was NO.**
> The registry lives at `app/core/ai/tools/registry.py` now, and it still holds
> two tools. Not an oversight and not a deferral — none of this module's five
> routes can be registered, for the reason §0.10's amendment gives.
>
> **The bill for changing that answer**, so that a future cycle can price it
> rather than rediscover it:
>
> 1. **Rename `ReportMeta.report_key`** and **`CatalogueEntry.key`** to
>    something carrying no secret word (`report_id`, `id`). Both are on the
>    wire, `params_schema` is published to clients as a binding contract (§11),
>    and the frontend reads both — so this is a **breaking API change**, not a
>    rename.
> 2. **Replace `ReportPage.totals: dict[str, int]`** with a typed model per
>    report. That contradicts §0.10's one-generic-envelope decision, whose whole
>    argument is that a per-report envelope pushes the description obligation
>    onto every future report author. Reconciling those two is design work, not
>    an edit.
> 3. **Decide what `CatalogueEntry.params_schema` becomes** for a tool caller.
>    A JSON Schema blob has no closed key space by definition. The catalogue may
>    simply be a route the agent never needs — the agent has a static tool list
>    and does not discover reports at runtime — in which case the answer is to
>    leave it and register only the four report routes once (1) and (2) are done.
>
> That is this module's own cycle, with its own spec. It was deliberately not
> done as part of the tool-surface cycle that discovered it, because reopening a
> just-shipped module's wire contract to serve a consumer that does not yet exist
> is how a forcing function turns into churn.

### 10.2 What the registry entry must record, and one thing to get right

`ReadTool.permission` is asserted equal to `route_permission(route)` by
`test_every_recorded_permission_is_correct`, so an entry for a report route
records **`reporting.read`** — the route gate — not the conjunction. That is
correct and it is also an understatement, so it must be written down: the
conjunction is enforced at execution, and an admin who lacks a source code gets
a `403` from the loopback call. This is exactly ai-admin §0.1's design ("the
agent gets exactly what the admin gets"), and it means the agent's reporting
tools are automatically narrower for a narrower admin, with no per-tool work.

`response_fields` must be written out for each tool, as that registry requires,
and the day a row model gains a field the tool goes dark until someone reviews
it. Reporting's row models are stable by construction (a field is a definition
change), which makes them unusually good citizens of that rule.

`max_limit` should be recorded even though the registry's boundedness test only
requires it for `list[Model]` responses — reporting's envelope is an object, so
the existing test would not ask. Reporting's own test (§12.6) asserts every
report route has a `limit` with an `le=` regardless, which closes that gap on
this module's side.

### 10.3 What the agent never gets

Export routes (§0.7 — a `GET` that writes, and `PlainTextResponse` fails
property 3 anyway). They go on reporting's own never-a-tool list and, per the
liveness discipline in `test_denylist_entries_resolve_to_real_routes`, that list
is tested to resolve to real routes.

---

## 11. Frontend (`frontend/src/modules/reporting/`)

- A catalogue page from `GET /api/v1/reports`: one card per report, greyed with
  the missing permission codes named when `entitled` is false. Nothing is
  hidden — a report you cannot run is a permissions conversation, not a secret.
- One page per report: a params bar bound to the report's params — built from
  the entry's published `params_schema`, one control per property, never a
  hardcoded list (§17.11) — a table bound
  to `rows`, a totals strip bound to `totals`, and — mandatory, not optional —
  an "as of `generated_at`" line plus a staleness note when
  `meta.reproducible` is false.
- CSV download links to the export route.
- No client-side aggregation of any kind. If the browser computes a percentage,
  there are two implementations of it and the browser's is the one someone
  screenshots (the same reasoning `sla/views.py` gives for owning breach state
  server-side).
- TanStack Query, strict TypeScript, no `any`; response types generated from or
  mirrored against the response models.

---

## 12. Testing requirements

Test-first, per `CLAUDE.md`. The security-relevant ones are 2–5.

1. **Every definition's `requires` equals the union of `TABLE_PERMISSIONS` over
   its `sources`** — it is derived, so this pins that it stays derived.
2. **Every table the compiled statement touches is declared.** For each
   definition, build the statement and walk its froms/joins; assert the set of
   table names ⊆ `sources`. This is the test that makes §0.4's guarantee real —
   an added join that reads a new module's table fails the build until the
   permission requirement grows with it.
3. **No definition reads a denylisted table, and no `export_columns` entry names
   a denylisted column.** Plus `test_denylisted_sources_are_real_tables`: every
   denylist entry resolves to a live table in `Base.metadata`, so the tripwire
   cannot rot into pointing at nothing.
4. **The conjunction denies.** For each report: a user holding `reporting.read`
   and every source code gets `200`; the same user missing exactly one source
   code gets `403` naming it — one parametrised case per code, so no report can
   accidentally under-require. Plus a group-scoped-grant case (§0.6) that must
   also be `403`.
5. **Tenancy.** A report run as org A never returns an org B row, asserted on
   seeded two-org data for every report; and no report route's params model
   declares a field named `org_id`/`org`/`organization_id`.
6. **Every report route passes the five properties**, run against the fully
   built app through the same flattener `tests/test_ai_admin_registry.py` uses
   (FastAPI 0.139.2's `include_router` appends a lazy `_IncludedRouter` whose
   own `.path` is `None`; a naive `app.routes` filter finds nothing and the test
   passes vacuously). Stricter than the ai-admin version on two counts: the
   route must carry a real `summary=` (not the docstring fallback), and every
   field of the row model **and** the envelope must carry
   `Field(description=...)` (not a class-docstring fallback). Also asserts a
   `limit` query param with an `le=` on every report route regardless of
   response shape.
7. **`route_permission` returns `reporting.read` for every report route** — i.e.
   the conjunction dependency did not shadow the kernel gate (§0.5).
8. **Export**: header equals `export_columns`; a value beginning `=`, `+`, `-`,
   `@`, a tab or a CR in a declared free-text column round-trips prefixed/
   normalised; a numeric column with a negative value is **not** escaped; over
   the cap returns `413` and emits no body rows.
9. **`report.exported` is written on export and nothing is written on a run** —
   both directions, because §0.7 is a decision that is easy to reverse by
   accident.
10. **`totals` are computed over the whole match, not the page** — assert
    identical `totals` across two different `offset`s.
11. **Ordering is stable** — paging through a seeded set with a deliberate tie
    on the sort key returns each row exactly once.
12. **The statement timeout is set** — assert `SET LOCAL statement_timeout`
    executes inside the report's transaction (and therefore does not leak to the
    next user of the pooled connection).
13. **`patch-compliance` never counts an unscanned device as compliant**, and
    `backup-coverage` never counts a device with no policy as covered — the two
    places §2's "no inference" rule is most tempting to break.

---

## 13. Migration notes

One migration, and it is small:

- Seed `reporting.read` into `permissions` with its description, following the
  seeding pattern the other modules' permission migrations use.
- **No tables.** Reporting owns none (§0.3), so there is no DDL, no RLS policy
  to add, and nothing for `test_schema_drift.py` to compare.

**The migration's number is confirmed at build time.** The head at the time of
writing is `0046_approved_action_fuse_ceilings`, but other module cycles are in
flight; the build takes the number that is actually next and never edits a
shipped migration.

---

## 14. Related documents

- `CLAUDE.md` — the six invariants, and the rule that modules register into the
  kernel rather than forking it.
- `docs/00-vision.md` §63–67 — the monitoring-product boundary this module
  inherits (§0.2).
- `docs/modules/ai-admin/spec.md` §0.3 (the five properties), §4 (the tool
  registry), §15 (the recorded absence of this module).
- `docs/modules/sla/spec.md` §0.3, §2.5, §9.1 — `policy_source`, the mutable
  clock, and "the server owns whether a deadline was met".
- `docs/07-backup-restore.md` §4 — the audit chain, and why nothing may imply it
  can be rewritten.
- `docs/06-roadmap.md` Phase 17 — the only place this module is named.

---

## 15. Deferred, and what is actually blocked

Deferred means "buildable later, on top of this". Blocked means "not buildable
today, and here is what would unblock it".

**Deferred**

- **More reports.** The expected growth pressure is narrower reports with
  smaller `requires` sets (§0.4's cost), not per-column relaxation.
- **A snapshot layer**, for the reports that need reproducibility, built to
  §0.3's three rules (per-org, upsert-keyed, contained). It starts from the day
  it ships; there is no backfill.
- **Control-activity reporting over `audit_events`**, under §0.9's three rules,
  once someone decides what a count over tamper-evident data is for.
- **A cross-module financial report** (spend × licences × subscriptions ×
  installed software), once the base-currency conversion at the join has an
  owner.
- **Extending `escape_formula` to leading tab and CR** in
  `app/core/csv_export.py`, with all six calling modules reviewed in the same
  PR (§0.8, §16).

**Blocked**

- **Scheduled delivery of report *data*.** §0.11: the outbox payload is a copy
  outside the conjunction gate, webhooks forward it unfiltered, and email has no
  attachment path. Unblocked by the link-only design in §0.11 *plus* a signed,
  expiring, single-use link credential — which is a kernel concern, not this
  module's to invent.
- **Group-scoped reports.** §0.6: RBAC cannot express "aggregate limited to
  group G", and RLS keys on org alone. Unblocked by a scoping mechanism the
  kernel does not have.
- **Cross-org / MSP roll-up.** Requires `admin_session`, which bypasses RLS by
  design. Unblocked only by a specified, audited cross-org read path — a kernel
  decision with an invariant-#5 review attached, not a reporting feature.
- **Point-in-time historical accuracy for `sla-attainment`.** §0.3 cost 2: the
  source is mutable by design (SLA spec §0.3). Unblocked only by the SLA module
  recording immutable attainment events, which is that module's decision to
  take.
- **Anything requiring a metric series.** §0.2. Not blocked — refused.

---

## 16. Notes on this spec's sources, and where the code disagreed

Recorded because `CLAUDE.md` says a decision already in `docs/` wins, and a
reader should know which premises came from the code rather than from the task
that commissioned this spec.

1. **There is no brief.** No `docs/modules/reporting.md`, no roadmap phase. The
   only reference is Phase 17's row and `ai-admin/spec.md` §15.2. Everything in
   §0 is decided here for the first time.
2. **The five-properties measurement is worse than "2–3 of ~173", and the shape
   of the failure matters.** Measured at HEAD: **173** `GET` routes under
   `/api/v1`; **3** pass all five as the ai-admin test implements them
   (`/health/alerts`, `/devices/{id}/health`, `/ai/status` — the third is
   ai-admin's own, and postdates the registry docstring's claim of two);
   **0** pass under the strict reading, because **no route in the backend
   carries `summary=`** (the repo's single `summary=` is a dict argument in
   `inventory/api.py`) and exactly one `Field(description=...)` exists in any
   module `schema.py` (in `sla`). The registry docstring's "13 routes carry an
   endpoint docstring" is now 23. Two failure modes it does not mention are
   large: **76** `list[Model]` routes have no `limit` query parameter at all,
   and **19** `GET` routes have no typed response model. §0.10 is built on this.

   **Re-measured 2026-08-14: 188 routes, 10 passing properties 1–5, 3 passing
   all six.** The figures above were correct when written and stale within a
   phase — they are now pinned by `backend/tests/test_ai_tool_surface.py`, which
   fails naming the count that moved.

   **And the failure mode this note did not mention is the one that mattered.**
   It enumerated the ways routes fail to be *documented*. Not one of the three
   surviving routes' problems was documentation: what actually stops a route
   becoming a tool is an **open key space** — a `dict` in the response model, or
   a field name containing a secret word — which no property stated until
   ai-admin §0.3 gained property 6. This module's own five routes are the
   casualty (§0.10's amendment).
3. **`route_permission` does not see every gate.**
   `app/modules/remote/api.py` defines a local `require_any_permission` for its
   two session-read routes; both are genuinely gated and both return `None` from
   `route_permission`, so they read as ungated to router-derived RBAC checks.
   With `GET /api/v1/me`, that is 3 of 173. This is a finding about the remote
   module and belongs in its cycle; it shaped §0.5, which is why reporting does
   not fork the gate.
4. **`escape_formula` guards `= + - @` and nothing else** — verified:
   `_DANGEROUS_PREFIXES = ("=", "+", "-", "@")`. No leading tab, no CR, no NUL
   handling. §0.8 normalises locally rather than changing a helper six modules
   and one pinned ERP contract depend on. (Its docstring says "five modules" and
   omits `ipam`, which acquired a call site later — a small drift, noted so the
   next reader does not trust the count.)
5. **The "missed day is a permanent hole" claim is in the code, verbatim.**
   `app/worker.py::licenses_compliance_sweep_all_orgs`: *"a gap in a compliance
   series is not recoverable by the next run, because the snapshot is
   `computed_at`-stamped."* That sentence is the load-bearing argument in §0.3.
6. **`org_session` scopes by org and nothing else** — confirmed in
   `app/core/tenancy.py`: one `set_config('app.current_org', …, true)` and no
   role or scope GUC. `portal_session` sets a second GUC (`app.current_requester`)
   and proves the pattern *could* extend, but only for a principal type with its
   own database role and its own policies — which a "reporting role" is not.
7. **RBAC is enforced only in route handlers** (ai-admin §15.4, re-verified):
   services take `org_id` and trust their caller for role. §0.5's
   `require_report_sources` is a dependency, not a service check, so this module
   does not become the first exception to that.
8. **A materialised view cannot carry RLS in PostgreSQL 16.** Asserted from
   PostgreSQL semantics, not measured against a live server here; §0.3 flags
   confirming it as a build task, and nothing else in the spec depends on the
   answer. **MEASURED DURING THE BUILD — see §17.5.** PostgreSQL 16.13 refuses
   both `ALTER` spellings and refuses a policy outright; the assertion holds.
9. **Notification has no attachment path.** `notification/delivery.py` builds an
   `EmailMessage` from a `(subject, body)` renderer; `_event_payload` forwards
   `event.payload` to webhooks unfiltered, by design. §0.11 rests on both.
10. **One kernel change is required, and it is the smallest possible one:** a
    new permission code in `app/core/rbac.py::PERMISSIONS` plus its seed
    migration — the same additive registration every module makes. **One kernel
    change was deliberately declined:** a conjunction gate in `app/api/deps.py`
    with a set-returning `route_permission` (§0.5). It is the better long-term
    shape; it is not a change to make on behalf of a single module, and the
    moment to propose it is when a second module needs it.

---

## 17. Build amendments (recorded during implementation)

Everything below is a decision the build forced or a claim the build measured.
Recorded here rather than left in commit messages, because §16's premises are
what a future reader will trust and several of them have moved.

### 17.1 The three numbers the spec left unset

The spec named no value for the report `limit` ceiling, the export cap or the
statement timeout. Shipped:

| Constant | Value | Where |
| --- | --- | --- |
| `DEFAULT_MAX_LIMIT` | `1_000` | `service.py`; every definition's `max_limit`, and therefore the `le=` on its `limit` query parameter |
| `export_row_cap` | `50_000` | per definition; all four v1 reports use this value |
| `REPORT_STATEMENT_TIMEOUT_MS` | `15_000` | `service.py` |

**All three were CHOSEN, NOT MEASURED**, and the code says so. The reasoning:
1_000 rows is more than any operator reads on a page and small enough that the
whole page fits one response without paging becoming theatre; 50_000 rows is a
spreadsheet's practical ceiling and well above any honest single-org export;
15_000 ms is far above any query against a seeded fleet and far below the point
at which a browser or a reverse proxy gives up on its own, so a report that hits
it is reporting a real problem rather than a slow afternoon. None of these is a
measurement of a production fleet, and the next reader should not treat them as
one. Revisiting them needs a real fleet, not an argument.

### 17.2 The statement timeout: the syntax and the exception that actually arrived

`SET LOCAL statement_timeout = '<n>ms'`, issued inside the caller's
`org_session` transaction, with the value INTERPOLATED — PostgreSQL does not
accept a bind parameter in `SET`, so the number is an `int` module constant and
never a request value.

**The plan asserted the wrong driver and the wrong exception, and the code won.**
The DSN is `asyncpg`, not `psycopg`. A cancellation surfaces as
`sqlalchemy.exc.DBAPIError` — **not** `OperationalError` — whose `.orig` is
asyncpg's generic SQLAlchemy adapter wrapper.
`asyncpg.exceptions.QueryCanceledError` appears only at `.orig.__cause__`. So
the narrow catch is on the **SQLSTATE**, `57014` (`query_canceled`), read from
`.orig.sqlstate`, which is also driver-independent — psycopg's `QueryCanceled`
carries the same code, so this survives a driver swap that a class check would
not. `run_bounded` catches nothing else: a syntax error, a permission error and
a constraint violation all arrive as `DBAPIError`, and turning them into a
`504 report_timeout` would tell an operator to narrow their window while the
real defect sat in the SQL.

Measured, both directions: `SHOW statement_timeout` returns `50ms` inside the
transaction that set it and the server default on the next checkout of the same
pooled connection (pool pinned to one connection, so the reuse is
deterministic). `SET LOCAL` does not leak.

### 17.3 Literal export paths, not a `/{key}` template

§6.2's route table prints the export as `/{key}/export.csv`, but §6.1's loop
generates one route per definition and the loop is what has to win. A templated
path cannot carry a per-definition `summary` (property 5), cannot bind
`require_report_sources(key)` at route-build time (the conjunction would have to
be resolved inside the handler, after a data session is a possibility), and
would let an unknown key reach a handler at all. `register()` refuses a key
containing `/` or an empty key, so a generated path can never be more than one
literal segment. The full path is produced by one function,
`registry.export_path(key)`, used by the generation loop, by `_NEVER_A_TOOL` and
by the tests, so the denylist cannot drift from the router by one edit.

### 17.4 `/api/v1/reports` is a SHARED prefix, and §6 was wrong to imply otherwise

`app/modules/security_logs/api.py:127` already mounts
`GET /api/v1/reports/access.csv` — a `PlainTextResponse` CSV gated on
`security_logs.read`, registered before this module existed. Consequences,
both of which shaped the tests:

- Every sweep over "reporting's routes" discriminates by the **endpoint's
  defining module**, never by a path prefix. A `path.startswith` filter folds
  the foreign route in and breaks the exact route count for a reason that has
  nothing to do with reporting.
- A future definition keyed `access.csv` would mount a second route at an
  existing path and FastAPI would serve whichever registered first, with the
  other module's gate and response type.
  `test_a_report_path_is_claimed_by_exactly_one_route` makes that a red test
  rather than a mystery, and `test_the_shared_prefix_finding_is_real` pins the
  premise so the warning cannot outlive the hazard.

### 17.5 §0.3's open build task, now MEASURED rather than asserted

§16.8 recorded "a materialised view cannot carry RLS in PostgreSQL 16" as
asserted from semantics. Measured against the deployed server —
**PostgreSQL 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)**:

```
ALTER MATERIALIZED VIEW mv ENABLE ROW LEVEL SECURITY;
  ERROR:  ALTER action ENABLE ROW SECURITY cannot be performed on relation "mv"
  DETAIL:  This operation is not supported for materialized views.
ALTER TABLE mv ENABLE ROW LEVEL SECURITY;      -- same error, same detail
CREATE POLICY p ON mv USING (true);
  ERROR:  "mv" is not a table
```

§0.3's rejection of a materialised-view snapshot layer therefore stands on a
measurement, not on a reading. If a future PostgreSQL supports it the rejection
is re-openable and nothing else in the spec changes.

### 17.6 `verify_registry()` runs at import, not only in the suite

`definitions/__init__.py` calls it at import time, so a definition that reads an
undeclared table aborts the application's import rather than only failing
pytest. A verifier that ran only under the suite would let a bad definition
reach a running server whenever the suite was skipped, and the failure it
prevents is a data disclosure — the wrong side of that trade.
`tests/test_reporting_runner.py::test_the_whole_registry_verifies` keeps the
version that names the file when it fails.

### 17.7 Where the models disagreed with the spec's column names

- **There is no `inventory.*` permission code.** `inventory/api.py` gates all
  six of its routes, both CSV exports included, on `devices.read`. No v1 report
  reads an inventory table; if one does, `TABLE_PERMISSIONS` maps it to
  `devices.read` and not to an invented code.
- **Health tables are singular** (`health_alert`, `health_check`,
  `health_profile`, `device_health_check_state`) and so are the service-desk
  tables (`ticket`, `ticket_queue`, `ticket_category`). Kernel, patch and backup
  tables are plural.
- **`AuditEvent`'s metadata attribute is `meta`**, mapped to the column
  `metadata`.
- **`device_patch_status` has no timestamp for a failure.** `patch/apply.py`
  sets `state = "failed"` and `last_error` and nothing else, so `updated_at` is
  the only record of when a failure landed — which is what `patch-compliance`'s
  windowed failure count reads.

### 17.8 The remote-module finding, restated as a filing against that module

`app/modules/remote/api.py` forks the kernel gate with a local
`require_any_permission`, so `GET /api/v1/remote/sessions` and
`GET /api/v1/remote/sessions/{session_id}` return `None` from
`route_permission` and read as **ungated** to every router-derived RBAC check,
though both are genuinely gated. With `GET /api/v1/me` that is 3 of 173 routes.

**This is not reporting's to fix, and the reasons are worth keeping.** The
kernel's reflection cannot express an OR-gate: `route_permission` returns one
code and reads it from a free variable literally named `code`, while
`require_any_permission` closes over a tuple named `codes` — so adding its
qualname to `_PERMISSION_DEPENDENCY_QUALNAMES` would make the reflection return
`None` anyway, or force `route_permission` to return a set, which is the exact
kernel change §0.5 declines to make to the one function every module's
completeness test depends on. The alternative fix splits remote's two routes so
each carries a single gate, which means either an auditor holding only
`remote.recordings.read` loses session listing or an operator holding only
`remote.control` does — a product decision for that module's owner, not a side
effect of a reporting cycle.

**Filed against the remote module's cycle.** Reporting's contribution is
negative and deliberate: it never forks the gate, never hides one from the
router, and its own completeness sweep is scoped to its own routes so that
nobody is tempted to convert this visible finding into a suppressed one with an
allowlist.

One naming correction to §10.2 while it is in view: the test the spec calls
`test_every_recorded_permission_is_correct` is really
`test_every_recorded_permission_matches_the_router`
(`tests/test_ai_admin_registry.py:107`).

### 17.9 Two divergences from §3's printed dataclass

`ReportDefinition` carries two fields §3 does not print, both because §6.3
requires behaviour §3's shape cannot express:

- **`build_totals`** — §6.3 requires `totals` computed over the WHOLE match and
  never over the page ("a paged total that changes as you page is worse than no
  total"), which is a second statement. With only `build`, totals would have to
  be computed in Python over every matching row, which is unbounded — the one
  thing §0.3 buys the statement timeout to avoid. `sources.py` verifies BOTH
  statements against `sources`, so the second one cannot become a back-door
  join.
- **`reproducible`** — `meta.reproducible` differs per report (`False` for
  `sla-attainment`, `True` for the current-state reports), so it is a property
  of the definition rather than a constant.

`requires` is `field(init=False)`: it is not a constructor parameter, so a
definition author cannot hand-write it and therefore cannot understate it.

### 17.10 The export: what `limit` does, and what the escaper does not

- **`limit`/`offset` page the JSON report and are IGNORED by the export.** They
  are still accepted (so a link can carry the same query string) and still
  recorded in the audit metadata, but they do not shrink the file. Honouring
  them would hand back a one-row CSV that looks exactly as complete as any
  other, and — the load-bearing half — a statement that is already `LIMIT`ed can
  never match more than its limit, so the cap would be unreachable and §0.8's
  refusal dead code.
- **`escape_formula` is still `("=", "+", "-", "@")` and nothing else**,
  re-verified at build time, and it is NOT extended here. Reporting normalises
  its own declared free-text columns first (strip leading `\t`/`\r`/`\n`, remove
  NUL) and then calls the shared helper. A test pins the premise, so if somebody
  does extend the shared helper this module's local stage goes red and can be
  reconsidered rather than silently becoming redundant. Extending it remains
  filed in §15, with all six calling modules reviewed in one PR.
- **The escaping is per column and only over `export_free_text`.** The
  falsification of "escape every column" needed a probe column that is a
  `Decimal` rather than an `int`: `escape_formula` only ever touches a `str`, so
  an `int` of `-5` survives the blanket implementation untouched and the test
  passed against the bug. A `Decimal` serialises to `"-5.00"` in `mode="json"` —
  budget's Odoo `amount` shape exactly — and the mutation goes red. Recorded
  because the next person to add a test here will reach for an `int` too.

### 17.11 The params schema, published — CLOSED

**The finding, as it stood.** §11's "a params bar bound to the report's params"
was half-delivered, and the gap was a backend one: `CatalogueEntry` published a
report's `summary`, `description` and `requires` but **not its params schema**,
so the browser could not bind a typed control per parameter. `ReportView` offered
the two parameters every definition happens to have (`limit`, `offset`) plus an
optional window it sent only when filled — safe, in that no report was handed a
parameter its `extra="forbid"` model would reject, and wrong in that the browser
held a *second copy* of a contract the server already owned. The two copies had
already drifted: eight real parameters were unreachable from the browser
entirely — `os_family`, `device_group_id` and `stale_after_days` (shared between
three reports), `fleet-posture`'s `severity_at_least`, `patch-compliance`'s
`window_days` and `scan_stale_after_intervals`, and `sla-attainment`'s
`queue_id` and `priority` — while `from`/`to` were offered for the three reports
that answer 422 for them.

**The fix.** `CatalogueEntry` gains `params_schema: dict[str, Any]`, populated
from `ReportDefinition.params_json_schema()`, which returns
`self.params.model_json_schema()`.

**It is DERIVED, and the derivation is the whole design.** `self.params` is the
same attribute `api.py` binds as the route's query model
(`Annotated[params_model, Query()]`), so the object described is the object that
validates. There is deliberately **no `params_schema=` constructor argument** —
the same discipline `requires` gets from `field(init=False)`, applied by having
no field at all: a definition author has no place to write a second copy.
`test_the_published_schema_is_the_model_the_route_validates_with` closes the loop
off the *built app*, reading the annotation from `route.dependant.query_params`,
so a refactor that bound a different model goes red rather than leaving the
catalogue describing something that does not validate.

**`by_alias=True` (Pydantic's default) is load-bearing.** `sla-attainment`'s
window is `window_from`/`window_to` in Python and `from`/`to` on the wire. A
schema naming the Python attribute would tell a client to send a parameter
`extra="forbid"` rejects — worse than publishing nothing — and it is the one
error that would still look plausible. Asserted both ways: the schema names
`from`/`to`, and a request using `window_from`/`window_to` is a 422.

**Publishing describes the gate; it does not widen it.** Asserted in both
directions against the live server, per report, per parameter: every name the
schema publishes is *accepted* by the route (so the schema cannot be narrower
than the gate and mislead a client into omitting a filter), and a name the schema
does not publish is a *422* (so `additionalProperties: false` is a report of
`extra="forbid"` rather than decoration). `test_publishing_a_bound_does_not_lift_it`
pins the other half: reading the published `limit.maximum` is not permission to
exceed it.

**Who it travels to, and why it is not filtered.** `params_schema` sits on the
same footing as `summary`, `description` and `requires`: compiled into the image,
byte-identical for every caller in every org, and no statement about any org's
data. The catalogue is **deliberately not filtered by entitlement** (§6.2, §11) —
every report is listed with `entitled` and `missing_permissions`, because a
catalogue that hid the reports you cannot run gives an under-permissioned
operator the same 200 as a smaller deployment, with no way to tell them apart.
So the schema is published for every entry, and the gate that applies is
`reporting.read` over the whole response: a caller holding every *source* code
and not `reporting.read` gets a 403 whose body names no parameter at all. Pinned
by `test_no_schema_reaches_a_caller_without_reporting_read`,
`test_the_schema_is_identical_for_an_entitled_and_an_unentitled_caller` and
`test_the_schema_is_identical_across_orgs` — the last of which seeds one org with
a device and a device group and asserts neither appears, because "helpfully"
enumerating an org's queues into a params enum is exactly the shape this field
invites.

**The frontend consumes it.** `paramsForm.ts` turns the schema into controls
(enum → `<select>` carrying the published options, `format: "date-time"` →
`datetime-local` sending an *instant*, integer with `ge`/`le` → number input
carrying the server's bounds, anything unrecognised → a text box, never
dropped), seeds the form from the schema's **own defaults**, and sends only the
names `properties` declares. A report whose required parameters are unfilled
(`sla-attainment`'s window) now runs nothing and says which are missing, instead
of running on load and rendering the 422. The frontend still gates nothing
(invariant #6): `required` and `min`/`max` save a round trip, and the server
validates regardless.

**Not covered, stated rather than implied.** The published schema is Pydantic's
JSON Schema verbatim, including each params model's class docstring as the
schema `description`. That prose is written for a definition author, not for an
operator, and no test asserts it reads well in a browser tooltip. Nothing
measures the response size the four schemas add to `GET /api/v1/reports`; it is a
few kilobytes of static text on a route that reads no org data, and it was not
benchmarked.

### 17.12 What the live smoke test actually covered

Run against the real app and a real PostgreSQL 16 through the suite's own
fixtures — no mocks, no monkeypatched service — with **two orgs seeded to
overlap deliberately**: the same two hostnames (`alpha.corp.local`,
`beta.corp.local`), the same OS family, a queue named `Support` in both, a
critical alert in both, and tickets inside the same window. Two orgs whose rows
differ in every column make a missing org predicate invisible.

Covered, and observed:

1. Catalogue as an entitled caller — four entries, all `entitled`. As a caller
   holding `reporting.read` alone — four entries, none entitled, each naming its
   own missing codes.
2. All four reports run; **no org B row appeared in any of them, checked by
   id**, and org B's `backup_repositories.storage_ref` sentinel appeared in no
   response body.
3. One source code revoked per report — `403` in every case, with exactly the
   revoked code in `missing`.
4. Export of `fleet-posture` — `text/csv`, header equal to `export_columns`,
   audit delta exactly **1**. Running the report again — audit delta **0**.
5. Export driven past a lowered cap — `413`, `application/json`, no `device_id`
   anywhere in the body, `cap` and `matched` present.
6. `GET /api/v1/audit/verify` afterwards — `{"ok": true, "count": 1,
   "broken_at": null}`. Reporting appends through `audit_service.record` and has
   not forked the chain.
7. An ISO timestamp in an **unencoded** query string is a `422` (the `+00:00`
   offset decodes to a space); through `urlencode` it is a `200`.

**Not covered, stated rather than implied:** the cap was exercised by lowering
`export_row_cap` to 1 rather than by seeding 50_001 devices, so what is proved
is the comparison and the refusal path, not the behaviour of a genuinely
50_000-row export. Nothing here measures query time on a large org, which is the
accepted cost §0.3 takes on and the thing `REPORT_STATEMENT_TIMEOUT_MS` was
chosen for without measuring.
