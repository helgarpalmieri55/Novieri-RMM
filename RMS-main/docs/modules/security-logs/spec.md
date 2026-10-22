# Phase 2 — Security Log Collection Specification (control plane)

Status: approved (autonomous-build delegation, 2026-07-18)
Source brief: `docs/modules/security-logs.md`. Kernel: `docs/kernel/spec.md`.

## 1. Scope

Control-plane side: the wire contract for shipped events, gateway ingestion
with dedupe, immutable-to-the-app storage with audited retention, search API,
SOC 2 access-evidence CSV export, and the Security page in the console. The
agent-side collectors were **not built** in this phase; they belong under
`agent/` in this repository and build against the contract exported here.
(Until 2026-08-05 they were scoped to a separate `it-suite-agent` repository;
`CLAUDE.md` commit `a32cead` moved the agent in-tree.)

**Amended 2026-08-06 — §10.** The Windows and macOS collectors are now built, in
`agent/internal/capabilities/logs`. The Linux auditd/journald/pam collector is
still not; a `logs.ship` job on Linux is `refused` and says so. §10 records the
whole of what was built, including three things this section could not have
anticipated: that `logs.ship` has no exported params or result schema (§10.1),
that `event_uid` must be derived rather than generated or the server's dedupe is
inert (§10.2), and the complete list of processes the agent may spawn (§10.3 —
none on Windows, one on macOS).

Out of scope (later phases per brief): broader log sources, alerting rules,
correlation with remote-session/change records, SIEM forwarding.

## 2. Resolutions of the brief's open questions

- **Event set v1 (normalized taxonomy):** `logon_success`, `logon_failure`,
  `logoff`, `account_lockout`, `privilege_elevation`, `admin_group_change`,
  `policy_change`. The agent maps OS-native events to these types and carries
  the native identifiers in `raw` (guidance for the agent implementation: Windows event
  IDs 4624/4625/4634/4647/4740/4672/4728/4732/4756/4719; Linux pam/sshd/sudo
  via auditd or journald; macOS unified-log auth subsystem). The control plane
  validates only the normalized shape.
- **Storage:** plain PostgreSQL table with targeted indexes. At the internal
  fleet size (~300 endpoints) logon volume is thousands of rows/day — no
  partitioning yet; the retention sweep keeps the table bounded. Partitioning
  is the recorded scale path if a commercial fleet pushes volume, and
  high-volume telemetry stays out of scope per `docs/00-vision.md`.
- **Retention default:** **365 days** (covers a 12-month SOC 2 audit period),
  configurable per org via `Policy` (`module="security_logs"`,
  `config={"retention_days": 365, "ship_interval_s": 60}`), seeded on first
  ingest like the inventory policy.

## 3. Immutability posture (deliberate deviation from a full hash chain)

Security events are **append-only for the application**: the `app_user` role
gets `INSERT`/`SELECT` only (UPDATE/DELETE revoked). Unlike `audit_events`
there is **no per-event hash chain and no delete-blocking trigger**: retention
must legitimately delete old rows, and chaining high-volume events would buy
little (the platform's own privileged actions remain hash-chained in
`audit_events`). Retention deletes run only in the worker through the admin
engine and every sweep writes a `security_logs.retention_applied` audit event
carrying the org, cutoff, and deleted count — the evidence trail shows exactly
what was removed and when.

## 4. Wire contract (agent → server, over the existing mTLS WebSocket)

New protocol message (added to `gateway/protocol.py`, exported to
`docs/kernel/wire-protocol/`):

```jsonc
{ "v": 1, "kind": "security_events", "events": [
    { "event_uid": "<agent-generated uuid>",   // idempotency key
      "ts": "2026-07-18T09:12:03Z",            // RFC3339
      "event_type": "logon_success",           // the v1 taxonomy
      "username": "TOESHEE\\jsmith",
      "source": "win:4624",                    // OS-native channel/id
      "outcome": "success",                    // success | failure | n_a
      "raw": { ... }                           // native fields, free-form
    } ] }
```

- Max **500 events per message**; larger batches are rejected at parse.
- The device identity comes from the certificate-bound connection — never
  from message content.
- Delivery is at-least-once (the agent resends its buffer after reconnect);
  ingestion dedupes on `(device_id, event_uid)` with `ON CONFLICT DO NOTHING`.
- `logs.ship` is registered as a dispatchable job type (empty params, no
  result handler): dispatching it tells the agent "flush your buffer now";
  the scheduled cadence comes from the org policy, dispatched by the server.
  *(Amended 2026-09-23.)* The `logs-ship-sweep` beat (every 300 s,
  `app/modules/security_logs/schedule.py`) sends a signed `logs.ship` to each
  online Windows/macOS device with none in flight and none issued within the
  org's `ship_interval_s`, TTL one interval. The interval is clamped to
  **900 s – 86400 s**: the seeded 60 s would be a signature, a job row and an
  audit row per machine per minute, and one job already ships up to 5,000
  events. One `security_logs.ship_scheduled` audit row per org per pass; each
  job is also audited on delivery (`jobs.dispatched`).

## 5. Data model (migration 0006; org_id + FORCE RLS via the standard pattern)

`security_events`: `id` (uuid7), `org_id`, `device_id` FK, `event_uid`
(String(64)), `ts`, `event_type` (String(30)), `username` (String(200)),
`source` (String(100)), `outcome` (String(10)), `raw` jsonb.
Constraints/indexes: unique `(device_id, event_uid)`; `(org_id, ts)`;
`(org_id, event_type, ts)`; `(org_id, username, ts)`.
Grants: revoke UPDATE/DELETE from `app_user` (migration + test-harness grants).

## 6. Ingestion (gateway)

`handle_agent_message` gains a `SecurityEventsMsg` branch → module service
`app/modules/security_logs/ingest.py`:
`async def ingest_events(session, device, events) -> int` (inserted count) —
validates each event's `event_type` against the taxonomy (unknown types are
rejected as a batch: the whole message is discarded and audited
`security_logs.ingest_rejected`, keeping the taxonomy authoritative), bulk
inserts with `ON CONFLICT (device_id, event_uid) DO NOTHING`, seeds the
default policy on first ingest, queues a `security_events_ingested` UI event
(post-commit, existing mechanism). No per-event audit records — volume.

## 7. Retention (worker)

Daily Celery beat task `apply_security_log_retention`: per org, read
`retention_days` from the org's `security_logs` policy (365 when absent),
delete `security_events` older than the cutoff via the **admin engine** in
batches of 10 000, then audit `security_logs.retention_applied`
(`metadata={"cutoff": ..., "deleted": n}`). Logic in a testable
`async def apply_retention(org_id, admin_engine, app_engine) -> int`.

## 8. API (`app/modules/security_logs/api.py`, permission `audit.read`)

- `GET /api/v1/security-events` — filters: `device_id`, `username`
  (substring, case-insensitive), `event_type`, `from`/`to` (RFC3339),
  paginated `{items, total}`, newest first. Response model
  `SecurityEventOut {id, device_id, hostname, ts, event_type, username,
  source, outcome}` (raw exposed only on the detail endpoint).
- `GET /api/v1/security-events/{id}` — includes `raw`.
- `GET /api/v1/reports/access.csv?from_ts=&to_ts=&username=&event_type=` —
  SOC 2 access-evidence export: columns
  `ts,hostname,username,event_type,source,outcome`, streamed CSV. Same
  permission.

  **Amended 2026-08-22 (finding A58, UX-closeout recommendation 7).** As first
  written this route took the period and nothing else, so an operator looking
  at a screen narrowed by `username` or `event_type` could not produce a file
  of what they were looking at — the export silently widened to every
  principal and every event type. The console could not correct that from its
  side and named the button after the file instead. It now takes the same two
  filters as `GET /security-events`, through the same query builder, so the
  file and the screen agree by construction rather than by coincidence.

  **The export writes one `security_logs.evidence_exported` audit event**
  (invariant #4), inside the same transaction as the read, carrying
  `{filters, row_count, format}` — never rows. It recorded nothing at all
  before this amendment. A search writes nothing, and there is a test in each
  direction.

## 9. Frontend

`frontend/src/modules/security/SecurityPage.tsx` (route `/security`, nav item
"Security"): filter bar (device select, username search, event-type select,
time range presets 24h/7d/30d/custom), event table (ts, hostname, user, type
badge — failures/lockouts get `destructive`/`warning` variants), detail
drawer showing `raw`, and an "Export evidence CSV" link honoring the current
period filter. Live refresh on the `security_events_ingested` UI event.
The page renders only when `/me` permissions include `audit.read` (nav item
hidden otherwise — the server still enforces).

## 10. Amended 2026-08-06 — the agent collectors, as built

`agent/internal/capabilities/logs` implements `logs.ship`. §1 said the
collectors were "**not built**"; this section is what replaces that sentence.
Windows and macOS are built (the fleet). Linux is not — see §10.5.

### 10.1 A gap in the exported contract, recorded

**There is no `LogsShipParams.schema.json` and no `LogsShipResult.schema.json`
in `docs/kernel/wire-protocol/`.** `logs.ship` is registered in
`app/jobs/registry.py` with an empty `LogsShipParams` and no result handler, and
neither model was ever exported by `scripts/export_wire_schema.py`. So unlike
every other capability, the agent's params handling could not be built against
an exported file; it is built against the Pydantic model directly, and the one
property that model has — emptiness — is pinned agent-side instead: the
capability reads **no** params member at all, and unknown members are ignored
rather than refused (`docs/kernel/wire-protocol/README.md`'s "loosen inbound").
A test fails the moment either schema file appears, because the most likely
reason for it to appear is that `logs.ship` grew a parameter — and a parameter
on `logs.ship` would be a second, unversioned way to configure what an agent
reads off an endpoint.

The message contract, `SecurityEventsMsg.schema.json`, **is** exported and the
agent is built against the file: the taxonomy, the 500-event batch bound and the
three length limits are all read out of it by `schema_test.go`.

### 10.2 `event_uid` is DERIVED, not generated (amends §4)

§4's example annotates `event_uid` as an "agent-generated uuid". **A fresh UUID
per read defeats the server's dedupe entirely.** Ingestion is
`ON CONFLICT (device_id, event_uid) DO NOTHING` and delivery is at-least-once —
the outbox replays after every dropped uplink — so a random key would insert the
same logon again on every resend, and the SOC 2 access-evidence CSV would show
one login four times. The agent therefore derives the key from the record:

- **Windows:** `win:<EventRecordID>:<compact UTC timestamp>`, e.g.
  `win:184392:20260718T091203Z`. The timestamp is in the key and not decoration:
  **`EventRecordID` resets to 1 when the Security log is cleared**, so a key of
  `win:<recordID>` alone would collide every post-clear record with a pre-clear
  one and the server would silently discard them — destroying precisely the
  evidence that matters most, since clearing the Security log is the most
  interesting thing that happens on a compromised machine.
- **macOS:** `mac:<sha256(ts | process | pid/thread | message)[:40]>`. The
  unified log has no record id, and `log show --start` is inclusive, so the
  boundary record is re-read by construction — which a deterministic key
  absorbs.

### 10.3 Processes the agent may spawn (the EDR list)

Reading the Security event log and shipping it off-box is behaviourally
identical to credential-harvesting reconnaissance, and the agent runs as SYSTEM.
The complete list, for the EDR team:

| Platform | Processes spawned by `logs.ship` |
| --- | --- |
| **Windows** | **None.** Collection is `wevtapi.dll` — `EvtQuery` / `EvtNext` / `EvtRender` / `EvtClose` — called directly via `golang.org/x/sys/windows`. |
| **macOS** | **Exactly one:** `/usr/bin/log show --style ndjson --predicate <fixed> --info [--start <cursor> | --last 24h]`. |
| Linux / other | None. The capability refuses (§10.5). |

Not used, and deliberately: **`powershell.exe -Command Get-WinEvent`** (a SYSTEM
service spawning PowerShell is among the loudest signals an EDR has, trips ASR
child-process rules, and costs ~60 MB per invocation) and **`wevtutil`** (better,
but still a process creation per job — and `exec.CommandContext` does not bound
it: `Wait` blocks until the stdout pipe closes, measured at 30.003 s under a
200 ms deadline elsewhere in this agent).

The macOS process is launched with an **argv** (never a shell), **no stdin**, a
job-scoped context **and** `cmd.WaitDelay = 2s`, and its stdout is decoded from
the **pipe**, one NDJSON line at a time, never buffered whole. The predicate is
an allowlist of seven authenticating processes (`opendirectoryd`, `authd`,
`authorizationhost`, `loginwindow`, `sshd`, `sudo`, `screensharingd`) — a test
fails on any clause that is not process-scoped, and on any process named that
has no mapping rule.

The Windows query is an XPath evaluated **by the Event Log service**
(`*[System[EventRecordID>N and (EventID=4624 or …)]]`), so the high-volume ids
this agent does not collect — 4688 process creation, 4663 object access — are
never rendered and never enter the agent's address space.

### 10.4 What is collected, and the macOS caveat

Windows: exactly the ten event ids §2 names (4624/4625/4634/4647/4740/4672/
4728/4732/4756/4719) and nothing else. A widening of that map widens what leaves
every endpoint in the fleet, and a test fails on any id the spec does not name.
`raw` carries an **allowlist** of EventData fields (logon type, source address,
sub-status, privilege list, …), never a denylist — a denylist would ship whatever
Microsoft adds next, and some of that is command lines.

**macOS mapping is message-pattern based, and that is the weakest link in this
capability.** The unified log carries prose written by whichever process emitted
it, not a structured record with an id, so rules match on process + message
substring. Apple rewords these strings between releases: a reworded message
**stops matching and is dropped**, silently, rather than being misclassified. The
alternative is an OSLog collector, which needs cgo and would cost the agent its
single static cross-compiled binary. Recorded here so that a macOS evidence gap
is a known limitation rather than a surprise.

**The taxonomy filter runs on the agent, and that is not an optimisation.**
`ingest_events` rejects the **whole batch** on an unknown `event_type` (§6), so
one unmapped record shipped alongside 499 good ones discards all 500.

### 10.5 Linux is not built, and the agent says so

§2 names Linux auditd/journald/pam as a v1 source. It is **not implemented**. A
`logs.ship` job on a Linux endpoint is **`refused`** (not `failed`): the
condition is permanently true, `refused` is how a capability asks to be taken out
of rotation, and the refusal reason names the platform and states that no
security log was read. `failed` would have the control plane retrying on a timer
forever with nothing in the console explaining why.

### 10.6 Idle budget for `logs.ship` — measured, with falsifiers

This spec never stated one (the collectors were out of scope). Measured by
`agent/internal/capabilities/logs/budget_test.go` as ordinary `Test` functions
calling `testing.Benchmark` — a `Benchmark` cannot gate anything, since
`go test -bench` exits 0 on a regression.

| Falsifier | Observation that would break it | Measured (linux/amd64, this runner) |
| --- | --- | --- |
| 1. Anything resident with no ship job in flight | allocations on the idle path, a goroutine, an fd, or a log subscription opened by `Bind` | 0 allocs/op, 0 B/op over 569,411,779 iterations; `Bind` opens 0 log handles, 0 fds, 0 goroutines; 0 fd growth over 20 jobs against a real cursor file |
| 2. **Memory scaling with the SIZE of the log** | retained heap growing with the record count | 1,000-record log retained 4,048 B; **100,000-record log retained 3,408 B** (delta **−640 B**). The same job shipped 3,500 events in 7 batches |
| 3. RSS not returning to baseline / accumulating per job | RSS delta above the limit for a stated job count, or retained heap scaling over a 100× job spread | −20.1 MB after 1 job (2,000 records); −40,960 B after 30 jobs; retained heap 3,312 B at 1 job vs 3,984 B at 100 (delta 672 B) |
| 4. Unbounded per-event cost | > 32 KiB per event | 10,348 B/event (15,523,076 B/op over 1,500 records) |

**Bounds that make falsifier 2 true:** one reused 500-entry batch, never grown;
`maxEventsPerJob = 5,000` per job (ten full batches), so an endpoint with a year
of backlog catches up over several jobs instead of trying to send a year at once
— and cannot overflow the outbox's byte cap, which evicts by priority and would
drop the very frames it was asked to carry.

**Not asserted: wall-clock CPU**, for the reason in `internal/budget`'s package
comment — a shared runner's variance exceeds the signal, and a flaky budget test
gets skipped, which reads as coverage while measuring nothing.

**State at rest:** one file, `<data-dir>/security-logs-cursor.json`, 0600 inside
the 0700 data directory, **117 bytes** measured. It holds a version, a source
name, an opaque position and a timestamp. It is written only during a verified
job. The source name is what makes a cursor written by a different collector
detectable rather than misread — a Windows record id handed to the macOS
collector as a `--start` timestamp collects nothing and reports success.

**No standing subscription.** `health`'s `BorrowEventLog` stays nil and must:
this capability holds no subscription to lend, so `eventlog.*` check types
continue to report `unavailable` per the health spec §0.7.

### 10.7 What has never executed

- `EvtQuery`/`EvtNext`/`EvtRender`/`EvtClose` (`source_windows.go`) — compiled by
  `GOOS=windows go vet` and by cross-compilation, run nowhere.
- `/usr/bin/log show` and the process launch around it (`source_darwin.go`) —
  same.

Everything downstream of both — the XML parse, the NDJSON decode, the taxonomy
mapping, username assembly, timestamp normalisation, `event_uid` derivation,
cursor handling, batching, clamping and error classification — is unit-tested on
Linux through the `Source` seam.

**The fixtures in `agent/internal/capabilities/logs/testdata/` are hand-written,
not captured.** No Windows endpoint and no Mac was available. `testdata/README.md`
states exactly which parts of them to trust; replacing them with real captured
output from one Windows 11 box and one Mac is the highest-value follow-up for
this capability.

---

## 11. Testing

- Protocol: `security_events` message parses; >500 events rejected; unknown
  kind still rejected.
- Ingest: batch inserts; redelivery of the same `event_uid`s inserts nothing
  new; unknown `event_type` rejects the batch + audits; policy seeded once;
  UI event queued post-commit.
- Immutability: UPDATE/DELETE as `app_user` fail on `security_events`.
- Retention: old rows deleted per policy, recent kept, audit event written
  with the count; org with custom `retention_days` honored.
- API: RBAC (technician 403), filters (device/user/type/time), org isolation,
  CSV parses with the exact header and expected rows.
- Gateway integration: fake agent ships a batch over mTLS; events appear via
  the API; a second identical ship changes nothing (extends the kernel e2e).
- Frontend: vitest for the event-type badge mapping and API client params;
  tsc strict + build.
