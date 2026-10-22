# Error monitoring

**Date:** 2026-08-15
**Scope:** sub-project 3 of 4 in the platform observability program
**Status:** design, approved for planning

---

## 0. What this is

There is no error tracking in this platform. Measured: no Sentry, no GlitchTip,
no error table, no exception handler that records anything. When a request
500s, a Celery task raises, or a gateway connection dies, the only trace is a
log line — and nobody counts them, groups them, or notices that the same
failure has happened four thousand times since Tuesday.

This sub-project adds that counting. It depends on sub-project 1: without
correlation ids an error record cannot be joined to the request that produced
it, which is why it was sequenced third rather than first.

### 0.1 THE CENTRAL CONSTRAINT

Error monitors conventionally store stack traces, including frame locals.
That is the single most reliable way to put a credential in a database: a
connection string in a local, a token in an argument, an exception message
built from both.

This platform spent sub-projects 1 and 2 ensuring exception **text** never
reaches a log line — four fix waves, three Criticals, and one retracted claim.
An error monitor that stored tracebacks would undo all of it, in a durable
store, on purpose.

**So an error record contains no exception text at all.** Not the message, not
the traceback, not the locals. What it contains is in §2, and what that costs
is in §7.

---

## 1. Where it lives

| concern | location |
|---|---|
| capture, fingerprinting, recording | `app/core/errors/` |
| operator API | `app/api/v1/errors.py` → `/api/v1/platform/errors` |
| UI | `frontend/src/modules/error-monitor/` |

Kernel infrastructure, like platform health: it reports on the platform every
module runs on, so it does not register through `modules.register_all`.

---

## 2. What an error record contains

```python
@dataclass(frozen=True, slots=True)
class CapturedError:
    fingerprint: str      # sha256 of (version, type, module, lineno, qualname), 16 hex
    fingerprint_version: int  # which grouping rule produced it -- see 2.1
    exc_type: str         # "ValueError" -- the TYPE NAME only
    module: str           # "app.modules.inventory.rules"
    lineno: int
    qualname: str         # "evaluate_rules"
    where: str            # "api" | "worker" | "gateway"
    # correlation, read from the contextvars sub-project 1 installed
    request_id: str | None
    org_id: str | None
    user_id: str | None
    job_id: str | None
    device_id: str | None
```

**No `message`. No `traceback`. No `locals`. No `args`.**

`exc_type` is `type(exc).__name__` and is put through the same shape gate the
logging kernel applies to type names — a dynamically-named class
(`type("postgres://u:pw@h/db", (Exception,), {})`) is not a hypothetical here;
sub-project 1 measured it.

### 2.1 The fingerprint groups; the message would not

Grouping is by `(exc_type, module, lineno, qualname)`, hashed. Deliberately
**not** the message — messages interpolate data (`"device 41f2 not found"`),
so message-grouping produces one group per device and defeats the purpose.
Not storing the message and not grouping by it are the same decision.

Consequence: two different `ValueError`s raised from the same line are one
group. That is correct for triage — same line, same bug — and the correlation
ids on individual events are how an operator separates them if they differ.

### 2.2 Which frame those four fields come from, and why it is versioned

*(Amended 2026-08-22, closing recommendation 9 / finding B79 of
`docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`. The original
rule is version 1 below and is still what the deployed rows were named by.)*

**Version 1 — the traceback's last frame.** Right about where the exception
came from, and right whenever this codebase is what raised. Wrong whenever
application code calls a library and the LIBRARY raises: the last frame is
then `httpx`/`asyncpg`/`sqlalchemy`, so the group is named after third-party
internals, the console shows a library path and line number for a bug in this
codebase, and every distinct failure funnelled through one library drifts
toward that library's handful of raise lines — the same collapse a catch-site
fingerprint would cause, one layer down.

**Version 2 — the DEEPEST application frame, else the last frame.** "Deepest"
means nearest the raise: traceback frames run outward from the raise to the
catch, `tb.tb_frame` is the catch site, and each `tb_next` steps one frame
closer to the raise, so the answer is the LAST `app.*` frame in `tb_next`
order. Three rules make that precise:

* **Application code is `app`, or anything beneath it**, derived from the
  capture module's own `__name__` rather than written out — one fact, one
  place, and it cannot go stale under a rename.
* **The traceback's first frame is never a candidate.** It is the catch seam,
  and every seam in this codebase is itself `app.*` (`app/api/request_context
  .py`, `app/worker.py`, `app/gateway/main.py`), so a rule that merely
  preferred application frames would answer "the seam" for exactly the case
  this amendment is about. That is the catch-site grouping §2.1 rejected.
* **No application frame at all** — a failure raised entirely inside a library
  from a library-scheduled callback — **falls back to the last frame**, i.e.
  version 1's answer. A library's own bug is still a failure to show, and the
  raise site is the only true thing left to say about it.

**THE VERSION IS PART OF THE HASHED INPUT** (`v2|type|module|lineno|qualname`),
not merely a column beside it. `platform_error_group.fingerprint` is UNIQUE and
the drain upserts `ON CONFLICT (fingerprint)`; a composite unique on
`(fingerprint_version, fingerprint)` was the alternative and would have made
every reader responsible for carrying the version, failing OPEN — merging two
eras — on the day one forgot. Hashing it in makes the disjointness a property
of the digest instead. The `fingerprint_version` column stays, for legibility:
it is the answer when an operator asks why two groups look like the same bug.

**THERE IS NO BACKFILL AND THERE CANNOT BE ONE.** This table stores the
DERIVED site only and never the traceback (§0.1) — so for a group already
named after a library, the application frame that should have named it does
not exist anywhere in this database, and no migration can recompute it.
Existing rows stay at version 1 and age out as their sites stop failing.

---

## 3. Capture, at seams that already exist

No new `try/except` is scattered through the codebase. Capture hangs off the
three places that already handle failure:

- **API** — `app/api/request_context.py`'s middleware already catches an
  unhandled exception, logs it, and re-raises. Capture goes there. It runs
  INSIDE the correlation contextvars, so the ids are populated; Starlette's
  `ServerErrorMiddleware` sits outside and would see them already reset.
- **Worker** — Celery's `task_failure` signal for task-level failures, and
  `_OrgCycleFailures.contain` (`app/worker.py`) which already receives every
  contained per-org sweep failure.
- **Gateway** — its existing exception paths in `app/gateway/main.py`.

One entry point: `capture_exception(exc, *, where)`. It must never raise —
a monitoring failure must not become the outage. It swallows and logs with
`exc_info`, exactly as the heartbeat writers do.

### 3.1 Recording is asynchronous and bounded

Capture writes to a bounded in-process queue; a background task drains it to
Postgres. A burst of ten thousand errors must not open ten thousand
transactions, and an error in the request path must not add a database round
trip to a request that is already failing.

**The queue drops on overflow, and drops are counted.** A silently truncated
error stream is worse than a visibly truncated one: the page shows
`dropped: N` so an operator knows the counts are floors, not totals.

---

## 4. Storage

```sql
CREATE TABLE platform_error_group (
    id           uuid PRIMARY KEY,
    fingerprint  text UNIQUE NOT NULL,
    fingerprint_version smallint NOT NULL DEFAULT 1,   -- §2.2; 1 = pre-0069 rows
    exc_type     text NOT NULL,
    module       text NOT NULL,
    lineno       integer NOT NULL,
    qualname     text NOT NULL,
    where_       text NOT NULL,          -- api | worker | gateway
    first_seen   timestamptz NOT NULL,
    last_seen    timestamptz NOT NULL,
    event_count  bigint NOT NULL DEFAULT 0,
    resolved_at  timestamptz NULL
);

CREATE TABLE platform_error_event (
    id          uuid PRIMARY KEY,
    group_id    uuid NOT NULL REFERENCES platform_error_group(id) ON DELETE CASCADE,
    occurred_at timestamptz NOT NULL,
    request_id  text NULL,
    org_id      uuid NULL,
    user_id     uuid NULL,
    job_id      uuid NULL,
    device_id   uuid NULL
);
```

### 4.1 Both tables are platform-scoped, and `org_id` is not a tenancy mechanism

Invariant #5 says all data access is tenant-scoped via RLS. These tables are
the second and third deliberate exceptions, after `platform_health_sample`.

A failure at process startup belongs to no organisation. A failure inside a
request belongs to one, but the *fact that a code path is broken* is platform
information, not that org's property — and grouping is across orgs by
construction, since the whole point is "this line has failed 4,000 times".

So: **`org_id` is a nullable correlation column, not the tenancy key.** RLS is
enabled and forced with a single `app_user` policy — deny-all to everyone
else, exactly as `platform_health_sample`. Authorisation is by the
`platform.errors.read` permission, which is platform-wide.

#### AMENDED 2026-08-22: "platform-wide" is now true of the permission, and was not

The sentence above was written as a description and shipped as an assumption.
Migration `0057` granted `platform.errors.read`/`write` to **`org-admin`** —
the built-in role every tenant's own administrator holds — so the permission
this paragraph called platform-wide was in fact held by every tenant. The two
statements were made in different files by different reasoning and never met.

The consequence was live, and audit
`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` (F1) reproduced
it: `GET /api/v1/platform/errors/{group_id}` returned the `org_id`, `user_id`,
`job_id` and `device_id` of **every other tenant's** error events to any
tenant's `org-admin`. Nothing beneath the route objected, and that is the
structural point — the exemption recorded here removes RLS as the backstop, so
the permission code is not *a* control on these tables, it is the *only* one.

**The resolution: migration `0068` moves both codes to `super-admin`**
("Cross-org platform administration (SaaS operator)", `0003`), revokes them
from every other role, and refuses them on the org-scoped role-matrix surface
so a holder of `roles.manage` cannot grant them back. The tables stay
platform-scoped, exactly as §4.1 describes; §5's table below now reads
`super-admin` where it used to read "any holder".

**Two alternatives were considered and rejected.** Scoping the routes to the
caller's org, and nulling the tenant-bearing columns for a caller whose org
does not match, both delete the cross-org view this module exists to provide —
"this line has failed 4,000 times, for these orgs, users and devices" is the
triage the operator is here for. The disclosure was never that the data is
cross-org; it was who was allowed to read it.

**What a running deployment loses.** Every `org-admin` — on the single-tenant
on-prem install, the operator's own everyday account — gets `403` from all four
routes after upgrade, and the console's Error Monitor entry disappears on its
own because `frontend/src/shell/nav.ts` gates it on the same code. Restoring
access means assigning the global `super-admin` role, which is deliberately not
possible from any console surface.

`tests/test_tenant_column_sweep.py`'s `TENANTLESS_BY_DESIGN` grows from one
entry to three, each with a written reason. That set existing is the point:
the fourth exception has to be argued for.

### 4.2 Retention

Events older than **30 days** are pruned by a daily sweep. Groups are kept —
`first_seen` and `event_count` are the history worth having, and a group
carries no payload. A resolved group with no events in the window is not
deleted; an operator marking something resolved should still find it.

---

## 5. Surfacing

| endpoint | auth | returns |
|---|---|---|
| `GET /api/v1/platform/errors` | `platform.errors.read` | groups, newest `last_seen` first, with counts |
| `GET /api/v1/platform/errors/{id}` | `platform.errors.read` | one group plus its recent events' correlation ids |
| `POST /api/v1/platform/errors/{id}/resolve` | `platform.errors.write` | sets `resolved_at` |
| `POST /api/v1/platform/errors/{id}/unresolve` | `platform.errors.write` | clears `resolved_at` |

**Both codes are held by `super-admin` only** (migration `0068`; see the
amendment in §4.1). They are not grantable from the org-scoped role matrix —
`app/core/rbac_admin.py` omits them from `/api/v1/permissions` and refuses them
on write, and `set_role_permissions` refuses them again in the database — so
"a holder of the code" means the platform operator, which is what §4.1 assumed
all along.

Two codes, not one: reading which code paths are failing and *changing triage
state* are different privileges, and this project's convention is a read code
per module with writes gated separately.

**The reopen route was added 2026-08-21**, closing the backend half of audit
finding B76 (`docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`):
the "resolved toggle" promised two paragraphs below is a TOGGLE, and only one
of its two directions had a route. An operator who resolved a group by mistake
had no way back, and the console could not offer the control without 404ing. It carries the same code as resolve —
undoing triage puts a group back in front of whoever is on call, which is not
a lesser act than removing it.

**Both writes are audited** (`platform_error.resolved` /
`platform_error.unresolved`, target type `platform_error_group`), and the
reopen event carries the `previous_resolved_at` it cleared. This is required
rather than decorative: the group row has no `resolved_by` and no resolution
note, so `resolved_at` *is* the entire record of a resolution, and an undo that
blanked it silently would destroy history rather than restore state. The audit
row is tenant-scoped to the ACTING operator's org; the group itself stays
platform-scoped per §4.1, so any holder of the code may reopen any group --
and since `0068` that means the platform operator, not a tenant's own
administrator.

The UI lists groups with `exc_type`, `module:line`, count, first/last seen, and
a resolved toggle. A group's detail view lists recent events' correlation ids —
each one copyable, because the operator's next move is to grep the logs for
that `request_id`.

---

## 6. Two carried follow-ups fold in here

Both are error-handling defects recorded during sub-projects 1 and 2:

1. **`asyncio` pre-renders `repr(exc)` into its message.** Its
   `default_exception_handler` `repr()`s every context value, and
   `Future.__repr__` embeds `repr(exc)` — so exception text the logging kernel
   refuses to print via `exc_info` is printed anyway, pre-rendered into `msg`,
   on every non-Celery tree. Measured mechanism; no instance found in this
   codebase. **This sub-project installs an `asyncio` exception handler** that
   captures the error properly and logs a static line, which closes the door
   and gains a capture point at the same time.
2. **`app/services/cert_renewal.py:283`** writes `metadata={"reason": str(exc)}`
   into the **audit** record, which is served through the audit API. Same leak
   shape, different sink. Replaced with the exception type; the audit entry
   keeps naming what failed without carrying its text.

---

## 7. What this costs, stated plainly

An operator sees `ValueError at app/modules/inventory/rules.py:94, 412 times,
first seen Tuesday` and **not** which value was wrong.

That is a real loss. The mitigation is the correlation id: every event carries
the `request_id` that produced it, and the logs — structured JSON, redacted at
the sink, with the same id — carry whatever the developer chose to put in
`extra=`. The design's bet is that a developer who wants a value in the record
should name it as a field at the log site, where redaction can see it, rather
than have the monitor scrape it out of a traceback where redaction cannot.

If that bet proves wrong in practice — if operators routinely cannot diagnose
from type plus location plus logs — the answer is to add named fields at the
raise sites that matter, not to start storing tracebacks.

---

## 8. Testing requirements

1. A captured error record contains no exception message, under a raise whose
   `str()` is a DSN with a password.
2. `capture_exception` never raises: a database failure inside recording is
   swallowed and logged with `exc_info`.
3. Two raises from the same line share a fingerprint; two from different lines
   do not.
3b. §2.2, all four halves: a library raise beneath application code is named
   after the application frame nearest the RAISE (not the one nearest the
   catch — a reversed walk must go red); two failures through one library
   raise line are two groups; a raise inside application code records exactly
   what version 1 recorded; a failure with no application frame falls back to
   the raise site, and a library failure caught at an `app.*` seam is never
   named after the seam.
4. A raise whose exception type has a hostile dynamically-generated name does
   not put that name in the record verbatim.
5. Correlation ids are populated when capture happens inside a request, and
   absent (not empty-string) outside one.
6. Queue overflow drops and increments a counter; the API reports the count.
7. `platform.errors.read` gates the list route; `platform.errors.write` gates
   resolve — verified through the existing whole-router gating sweep.
8. The two new tables are the only additions to `TENANTLESS_BY_DESIGN`, and
   the sweep still fails for a fourth.
9. RLS denies a non-`app_user` role on both tables.
10. Retention prunes events beyond 30 days and keeps groups.
11. The `asyncio` handler captures and logs statically — asserted with a real
    unretrieved-task exception whose `repr()` carries a secret.
12. `cert_renewal`'s audit metadata carries the exception TYPE and not its
    text — a regression pin on the fix in §6.

---

## 9. Exclusions

- **Alerting and notification.** A page that shows red is not a system that
  tells anyone. Routing to the notification module needs its own design —
  thresholds, suppression, who is on call — and bolting it on here would
  produce either a pager that fires on every new group or one nobody trusts.
  Groups carry `resolved_at` so triage works; notification is sub-project 3's
  successor, not its scope.
- **Release/version attribution.** Belongs with sub-project 4, which is where
  a deployed version becomes a first-class value.
- **Client-side (browser) error capture.**
- **Sampling.** At this platform's scale the bounded queue is sufficient;
  sampling adds a lie about counts for no benefit yet.

---

## 10. Falsifiers

1. Any exception text appears in `platform_error_group`, `platform_error_event`
   or either API response.
2. `capture_exception` propagates and turns a handled failure into a 500.
3. Grouping collapses unrelated failures, or splits one failure into a group
   per occurrence — either makes the counts meaningless.
4. The error path adds measurable latency to a successful request.
5. A fourth table joins `TENANTLESS_BY_DESIGN` without a written reason.
