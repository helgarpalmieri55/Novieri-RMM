# 08 — Observability and platform operations

**What this is.** How the platform reports on *itself*: what it logs, how you
tell whether it is healthy, where its own failures are recorded, and what the
operational guarantees actually are. Endpoint monitoring — the health of the
machines you *manage* — is a different thing and lives in
`docs/modules/endpoint-health-alerting/spec.md`.

**Audience.** The engineer on call, and the reviewer asking "how would you know
if this broke?"

**Status of the numbers.** Every count and interval below was read from the tree
on **2026-08-16**. Design intent that is not yet measured is marked as such in
§6 rather than stated flatly.

**Where the design lives.** This document is the reference; the reasoning is in
four design specs under `docs/superpowers/specs/` — the logging kernel, platform
health and status, error monitoring, and deploy rollback. Read those to find out
*why*; read this to find out *what*.

---

## 1. Logging

**One JSON object per line, on stdout.** Nothing writes log files; the container
runtime owns collection. Every record carries `ts`, `level`, `logger`, and
`message`, plus whichever correlation ids are bound at the time.

**Five correlation ids**, held in contextvars and read by the formatter at
format time: `request_id`, `org_id`, `user_id`, `job_id`, `device_id`. Absent
ones are simply not written.

- `request_id` is **minted by the server** on every request and an inbound
  `X-Request-ID` is ignored — a client-supplied id would let a caller collide
  two unrelated requests in the log. The minted id is echoed in the
  `X-Request-ID` response header on every response the middleware returns
  normally. **Not on a 500:** the header is attached after the downstream call
  returns, and an unhandled exception skips that line. The *log* correlation is
  unaffected — the middleware logs the exception, and captures it, from inside
  the still-bound context, so the failure has exactly one correlated log line.
  Pinned by `test_an_unhandled_exception_has_no_request_id_header`, so the gap
  is recorded rather than rediscovered.
- `org_id`/`user_id` are bound by `app.api.deps.bound_auth_context` for staff
  routes and `app.modules.portal.principal.bound_portal_principal` for portal
  routes. Both are guarded: a test fails if any route depends on the unbound
  resolver, because such a route works fine and silently logs no tenant.

**Redaction happens at the sink, by field name.** A key passed via `extra=`
whose name matches `looks_like_secret_key` is replaced with `[redacted]`.

**The limit of that, stated plainly:** redaction can only see structured
fields. Anything interpolated into the message string — including a `%`-arg —
is outside the gate, and so is everything uvicorn logs, since uvicorn's records
carry no structured fields at all. Two consequences follow, and both are
enforced rather than hoped for:

1. **Message templates must be static.** An AST sweep over `app/` rejects
   f-strings, `.format()`, concatenation, and `str(exc)` inside a log call, and
   pins the exact number of call sites (**70**) so the discipline cannot drift
   unnoticed. Log the exception with `exc_info=True`; never as an argument.
2. **A secret must be kept out of the request line, not redacted out of the
   log.** This is why the console's WebSocket token moved from `?token=` to the
   WebSocket subprotocol header: uvicorn writes the URL verbatim and no logging
   change could reach it. See `app/api/v1/ws.py`.

---

## 2. Is it up? — probes and the status page

**Two unauthenticated liveness endpoints**, for the load balancer:

| Endpoint | Meaning |
|---|---|
| `GET /livez` | The process is running. Nothing else. |
| `GET /readyz` | The **hard** probes pass. 503 with the failing names otherwise. |

**Eight probes**, of which two are hard (`postgres.app`, `redis`) — failing
either means the API cannot serve, so `/readyz` fails and traffic should move
away. The other six (`postgres.admin`, `objectstore`, `vault`, `jwks`,
`signing`, `ca`) are reported but do not fail readiness: an object store that is
down breaks backups, not logins, and taking the whole API out of rotation for it
would turn a partial outage into a total one.

**`GET /api/v1/platform/status`** is the operator-facing view: probe results,
per-replica heartbeats, and the module registry (routes and job types per
module, computed once at app build).

**Heartbeats are per replica, every 15s.** A status request answered by a live
replica says nothing about a dead sibling behind the same proxy, so each API and
worker process writes its own heartbeat keyed by container hostname. A replica
goes **degraded** after 3 missed intervals and **down** after 10.

The `jwks` probe is deliberately excluded from the worker's sampled set: a
worker cannot reach the IdP on the API's path, and sampling it there would
report a false DOWN.

**Probe alerts go through `notification`** (added 2026-09-23,
`app/core/health/notify.py`). Every 60 s the worker reads the samples; a probe
whose last 3 samples are all not-ok is `unhealthy`, all ok is `healthy`, and a
change publishes `platform.probe_unhealthy` or `platform.probe_recovered` into
each org's outbox. To receive them, add a `channel_default` rule for those
event types on an email or webhook channel. A probe that goes unhealthy again
within an hour of the last notice changes state silently, recovery included,
so a flapping probe sends at most one pair per hour. Payloads carry the probe
name, status and reason code only. The sampler and this task both run in the
worker, so a dead worker is not reported this way: that still needs an outside
check on `/readyz` or the worker heartbeat.

---

## 3. What broke? — the error monitor

Unhandled exceptions in the API, the worker, and the gateway are captured,
fingerprinted, and stored.

**The store carries no exception text.** A group is keyed by a fingerprint over
`(exc_type, module, lineno, qualname)`; an event carries `occurred_at` and the
five correlation ids, and nothing else. This is not an oversight to be fixed
later — exception text on this platform routinely contains credentials,
connection strings, and tenant data, and a monitor that stored it would become a
second, unaudited copy of the things the vault exists to protect.
`backend/tests/test_error_store_carries_no_text.py` drains a genuinely
secret-bearing exception into Postgres and searches every column of both tables.

**To investigate an error, you correlate rather than read a traceback:** the
group tells you the site — the deepest `app.*` frame, so a failure inside a
library is named after the application code that called it (spec §2.2) — and
the event's `request_id`/`org_id`/`job_id`
takes you to the log lines for that specific occurrence. That is the whole
reason §1's correlation ids are enforced.

**Capture never raises and never blocks.** The in-process queue is bounded at
1000 and drops the newest on overflow; a drain loop flushes to Postgres every
5s, swallowing and logging its own failures. A reporting subsystem must not be
able to take down the thing it reports on.

| Route | Purpose |
|---|---|
| `GET /api/v1/platform/errors` | Groups, most recently seen first |
| `GET /api/v1/platform/errors/{id}` | One group and its recent events |
| `POST /api/v1/platform/errors/{id}/resolve` | Mark handled |
| `POST /api/v1/platform/errors/{id}/unresolve` | Put a group resolved by mistake back on the list |

**Not built: alerting.** Nothing pages anyone when a new group appears. The data
is there; the notification path is a decision that has not been made.

---

## 4. Backup, restore, and rollback

`docs/07-backup-restore.md` is authoritative for disaster recovery — including
the parts no script covers (the internal CA, the audit-chain HMAC key, the
Keycloak realm). Two scripts automate the database half:

- `deploy/scripts/backup.sh` — globals, both databases, and the values a
  restore checks itself against (`alembic_version`, RLS policy count, image
  version). Artefacts are `600`, and it fails rather than writing an empty one.
- `deploy/scripts/restore.sh` — refuses a non-empty target unless
  `ITS_RESTORE_FORCE=1`, creates only *absent* roles (applying a
  `pg_dumpall --roles-only` file wholesale would clobber live passwords), and
  asserts its own end state: policy count equal to the backup's, and the
  application role narrowed.

Since 2026-09-23 both run on a schedule (`deploy/README.md`, "Platform
backups"): a nightly systemd timer (`scheduled-backup.sh`: databases,
optionally objects, retention, optional encrypted off-host copy) and a
monthly restore drill (`restore-drill.sh`) into a throwaway compose project.
Their outcomes are plain files a health probe can read:
`/var/lib/it-suite/backup/{last-success,last-offsite,last-drill}`. Nothing
alerts on them yet.

**Rollback is a retag, not a rebuild.** Images are tagged by git SHA, so going
back is pulling the previous tag and restoring the database — in that order.
`deploy/README.md` has the runbook. Getting the order wrong runs the old
image's migrations against the new schema.

---

## 5. What CI actually proves

| Job | What it would catch |
|---|---|
| `backend` | The Python suite, mypy strict, ruff |
| `frontend` | vitest, `tsc` strict, the production build |
| `agent` | The Go suite — a control-plane change that breaks the agent goes red here rather than in the field |
| `migrations` | `upgrade head → downgrade base → upgrade head`, with a schema diff and a leftover-objects check that counts **enum types as well as tables** |
| `deploy` | The backup/restore round trip between two purpose-built clusters, run exactly as documented, plus a re-run with the narrowing neutralised so the check cannot pass vacuously |

---

## 6. Honest limits

Stated here so nobody has to discover them during an incident.

- **No metrics or tracing.** No Prometheus endpoint, no spans. Logs, probes,
  heartbeats, and the error store are the whole surface.
- **Alerting covers probe state only** (§2): no alert on new error groups
  (§3), and none when the worker itself is down, since the worker is what
  sends them.
- **Log retention is the container runtime's**, not the platform's. Nothing here
  ships or ages out log lines.
- **`docker compose up -d` reusing a tagged image rather than rebuilding it is
  unverified.** The CI check proves tag interpolation only, and says so in its
  own comment.
- **A live-host upgrade and a live-host rollback have not been exercised.**
  The Keycloak realm restore is now checked by the monthly drill (Keycloak
  boots on the restored database and serves the realm); a sign-in against a
  restored realm has not been.
- **The agent idle budget** (low-tens-of-MB resident, ~0% CPU) is measured for
  the capabilities that carry a benchmark, and remains a design target for those
  that do not. A capability's own spec says which it is; `CLAUDE.md` requires
  that distinction be stated rather than blurred.

---

## 6a. Backup ships enabled, and has never completed a run

**Operator decision, 2026-08-16: backup ships enabled**, to be exercised in a
controlled environment rather than held back. The state it ships in is
unchanged and stated in `docs/modules/backup/spec.md` §6.1: the storage
transport is measured against real MinIO, **no `backup.run` has completed end
to end on any platform**, and the restore leg has never run against a real
object store.

`docs/modules/backup/controlled-test.md` is the procedure for that first run —
what to collect, what each failure stage means, and what a green run would and
would not prove. The short version of the last part: a successful `backup.run`
with no restore attempted is the single most misleading outcome available here,
because it is the state every backup product fails in.

---

## 7. A note on maintenance

The counts in this document — eight probes, seventy log call sites, two hard
probes, the intervals — are the kind that rot silently. Each one is read from a
single source in the tree, and most are pinned by a test that fails when the
tree and the number disagree. If you change one, change it where the test reads
it, and re-date the header.
