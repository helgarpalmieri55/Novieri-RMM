# The logging kernel: structured logs, correlation, and redaction

**Date:** 2026-08-14
**Scope:** sub-project 1 of 4 in the platform observability program
**Status:** design, approved for planning

---

## 0. What this is, and what it is not

The platform has **no structured logging**. Measured: no `structlog`, no JSON
formatter, no `dictConfig`, no logging configuration in `create_app` at all.
Sixty-one `logger.*` call sites exist across `backend/app`, all writing to a
root logger nobody configured, and **no request, job, org or device identifier
appears on any line**. An operator reading production output today cannot answer
"which request was this" or "which org did this belong to".

> **That "sixty-one" is the number AS MEASURED WHEN THIS SPEC WAS WRITTEN, and
> it is deliberately not updated in place.** §2.2 below carries the running
> count with a dated entry per change; the live figure is **70**, asserted
> exactly by `tests/test_logging_discipline.py`. Rewriting this paragraph on
> every change would erase the starting point the whole section is describing —
> the platform as it stood before the kernel existed. Editing it silently is
> also how it came to claim 62, then 60, then 61, each wrong. The assertion's
> failure message points here; what it wants updated is §2.2's running count,
> and this note is the pointer.

**CORRECTED 2026-08-14 (fix wave 4) — the count was 60, is 61, and this is the
number's SECOND drift, not its first.** §2.2 below documents the original
60-vs-62 correction (grep's over/under-counting against the AST sweep at that
measurement). This count moved again, independently, when the pre-merge review's
own blocker-4 fix added a `logger.exception` call to
`app/api/request_context.py` (§2 of that middleware's docstring) — a real,
legitimate site the AST sweep now finds, added after §2.2's measurement was
taken and never re-measured until this fix wave. A hand-maintained number that
no test pins has now been wrong twice; `tests/test_logging_discipline.py`'s
`test_the_sweep_actually_finds_log_calls` now asserts the AST sweep's total
**exactly** (61, not `>= 50`) precisely so a third drift fails a test instead
of aging silently in prose. **This "61" is the number AS OF fix wave 4 --
see the fix wave 6 correction immediately below, where the assertion moved
again, to 60, and stayed there through fix wave 7. The current, live number
is stated once, in §2.2's own closing paragraph and in
`tests/test_logging_discipline.py` itself; do not treat either count in this
paragraph as current.**

**CORRECTED AGAIN, fix wave 6 (M-12) — 61 is 60, the predicted third drift,
and the test caught it exactly as designed.** `app/worker.py`'s
`_OrgCycleFailures.contain` had an `if`/`else` whose two branches emitted
byte-identical `logger.error(...)` calls once fix wave 5 made `exc_info=exc`
uniform across both — a leftover of when they genuinely differed. Collapsing
the dead branch removed one call SITE (not one call at runtime: every
skipped org still gets its own ERROR line). `test_the_sweep_actually_finds_
log_calls` failed on the very next run, which is what "exactly, not `>= 50`"
at 61 was written to do — it just did it downward this time, not upward.

**CORRECTED AGAIN, platform health Task 6 — 60 is 64.** Platform health Task 1
already moved the live count to 61 (`app/core/health/registry.py`'s probe-
failure warning, noted below in §2.2's closing paragraph). Task 6 added
per-instance process heartbeats through Redis (`app/core/health/heartbeat.py`)
written by three separate processes — the API (`app/main.py`), the worker
(`app/worker.py`), and the gateway (`app/gateway/main.py`) — each of which
swallows and logs its own write failure so a heartbeat can never take its own
process down. That is three new ERROR call sites, one per writer, each a
static message with `exc_info=True`, no %-arg and no f-string. The count was
**64** as of this task; see the next correction below for why it moved again.

**CORRECTED AGAIN, the platform-heartbeat queued-task fix — 64 is 65.** The
worker's heartbeat above was written from a beat-scheduled Celery task,
sharing the worker's own task queue with every long sweep in this file (patch
scans, backup verification, retention). A worker whose pool slots were all
busy with one of those delayed the heartbeat behind them, and the status page
and the Docker healthcheck both read the resulting staleness as `degraded`,
then `down` — reporting queue saturation, not process death, while the
process was entirely alive. The fix moves the write onto a daemon thread the
worker process starts once, at `worker_process_init`, and owns directly —
the same posture the API and gateway already use — and removes the
`platform-heartbeat` beat entry and its task wrapper. That thread's loop
(`app.worker._worker_heartbeat_forever`) carries its own guard around each
iteration's call to `write_worker_heartbeat`, deliberately not a duplicate of
that coroutine's own guard: this loop has no external scheduler to retry a
failed iteration, so an uncaught exception here would silently end worker
heartbeats for the rest of the process's life, which the queued mechanism's
equivalent gap never risked (beat re-enqueues regardless of whether the
previous run raised). One new ERROR call site, same shape as every other one
in this document: a static message, `exc_info=True`, no %-arg, no f-string.
**CORRECTED AGAIN, error monitoring Task 1 — 65 is 66, and the drift shipped
unpinned.** `app/core/errors/capture.py`'s `capture_exception` wraps its whole
body in `try/except Exception` and logs one static-message ERROR with
`exc_info=True` when recording a captured error itself fails — the same
never-raise, log-and-swallow shape as every heartbeat writer above (spec
§3: "a monitoring failure must not become the outage"). Task 1's own commit
did not update this count or `test_the_sweep_actually_finds_log_calls`'s
assertion, so the AST sweep sat one call site ahead of what the test claimed
until error monitoring Task 2 caught it — the same "hand-maintained number
drifts silently" failure mode fix wave 4 already named twice, now a third
project in.

**CORRECTED AGAIN, error monitoring Task 2 — 66 is 67.** `app/core/errors/
store.py`'s drain parses each captured event's correlation-id strings
(`org_id`/`user_id`/`job_id`/`device_id`) into the `uuid` columns spec §4 of
`docs/superpowers/specs/2026-08-15-error-monitoring-design.md` declares for
them, and logs one static WARNING when a value fails to parse rather than
raising — a drain that could crash on a malformed correlation id would turn a
monitoring detail into an outage, the same property `capture_exception`
itself guards. One new call site: a static message, no `exc_info` (there is
no exception here to attach — parsing failed, nothing raised), no %-arg, no
f-string.

**CORRECTED AGAIN, error monitoring Task 3 — 67 is 69.** Two new call sites,
each a SINGLE shared definition installed into more than one process, not
one site per install:

- `app/core/errors/asyncio_handler.py::install_asyncio_exception_handler`'s
  inner `_handle` — the replacement `asyncio` loop exception handler both
  `app/main.py` (API) and `app/gateway/main.py` (gateway) install via
  `loop.set_exception_handler(...)`, closing spec §6's first follow-up
  (`docs/superpowers/specs/2026-08-15-error-monitoring-design.md`): asyncio's
  own default handler `repr()`s every context value, and a Task's own
  `__repr__` embeds `repr(exc)`, so exception text this kernel refuses to
  print via `exc_info` was printed anyway, pre-rendered into the default
  handler's message. `_handle` logs one static ERROR instead — never the
  context dict — with `exc_info` set only when the loop actually handed it
  an exception object. Defined once; installed twice, so it is one call
  SITE regardless.
- `app/main.py::drain_platform_errors_once` — the API's background task
  (alongside the platform heartbeat) that drains `app.core.errors.capture`'s
  queue to Postgres every `app.core.errors.store.DRAIN_INTERVAL_S`, logging
  one static ERROR with `exc_info=True` when a drain fails, matching every
  other never-raise-swallow-and-log site in this document. The WORKER's own
  drain (`app.worker.drain_platform_errors_all`, scheduled by a Celery beat
  entry on the same interval) deliberately does **not** swallow — its
  docstring explains why (a failure there becomes a `task_failure`-signalled
  capture instead) — so it adds no call site here.

**NOW 70, the API lifespan migration.** One new call site.
`app/main.py::lifespan` replaced three `@app.on_event("startup")` handlers,
and unlike them it has a shutdown half: it cancels the heartbeat and error-
drain tasks and waits, BOUNDED, for each to stop. When a task outlives that
bound it logs one static ERROR naming which one (`"background task %s did
not stop on shutdown"`, the task's own attribute name as the single %-arg —
a fixed string from a two-element tuple in this file, never operator or
exception data).

The bound is why the line exists. An unbounded `await` on a cancelled task
is a process that will not die, whose only symptom to an operator is a
container stuck in `Stopping`; a silent timeout would replace that with a
process that dies while a loop is still running. Measured while falsifying
the teardown: an unbounded version hung the test suite for four minutes.

**NOW 71, the identity admission gate.** One new call site.
`app/core/identity/__init__.py::log_admission_refusal` records a refusal at any
of the three token-acceptance doors (`app/api/deps.py`, `app/api/v1/ws.py`,
`app/modules/remote/tunnel.py`). It is ONE site rather than six because the
doors would otherwise drift apart in wording and in what they carried, and a
refusal is a security event that wants a single shape. Static WARNING template,
everything variable in `extra` (`door`, `sub`, `email`, `reason`) so it passes
through the redaction gate rather than being interpolated past it. The address
is a deliberate inclusion: it is not a credential, `auth.login`'s audit
metadata already carries it, and the lockout preflight prints that same column
— an operator diagnosing why they can no longer sign in has to be able to match
the log against the preflight's output.

The live number is **70**, asserted exactly by
`tests/test_logging_discipline.py`'s `test_the_sweep_actually_finds_log_calls`;
every count stated elsewhere in this document (60, 61, 62, 64, 65, 66, 67, 69)
is historical as of the fix wave or task that measured it and must not be
treated as current.

**NOW 77, the invisible-identity-layer cycle.** Six new sites, all on the
first-boot path (`app/core/setup_code.py`, `app/api/v1/setup.py`) plus one in
`app/core/identity`. First boot is the one path with no authenticated user, no
session and no console to look at — if the install code is wrong or the
provisioning secret has not been written yet, a log line is the only evidence
there is, because a platform with zero users has no audit chain either. Static
templates, variables in `extra`, and the install code never appears in any of
them.

**NOW 106, MCP final review.** One static ERROR in `app/mcp/server.py`, when
the shielded `mcp.tool_called` audit write is abandoned at its deadline (a
database that never answers would otherwise hold the handler, and shutdown,
for ever). `extra` = the tool name as the audit log records it, the status and
the deadline; never the arguments. The rate limit's Redis WARNING moved from
the tool handlers to the request gate, one site either way.

**BEFORE THAT, 105, MCP connections polish.** One more static WARNING in `app/mcp/api.py`
(`exc_info=True`, `extra` = the org id and the client id), when the post-commit
`mcp.app_revoke_incomplete` audit write fails. The revoke still answers 200 with
its `keycloak_failures` count rather than a 500 after a committed revocation.

**BEFORE THAT, 104, the MCP console admin API.** Two static WARNINGs in `app/mcp/api.py`,
both after a revocation is already committed, with `exc_info=True`: one when
removing an AI app's consent for one person at Keycloak fails, and one when the
whole removal runs out of its time budget or the provisioning client is
misconfigured. The response counts the people not reached (`keycloak_failures`).
They first shipped with no `extra`; the review's fix round added the org id and
the client id, so a failure can be traced to an app. No user id or token is
logged.

**BEFORE THAT, 102, MCP fix round 2.** Two more sites in `app/mcp/invoke.py`: a WARNING
when a write tool call that overran its caller's wait is cancelled at the hard
cap (`WRITE_HARD_CAP_S`), and an INFO when such a write finishes later on its
own, with the tool name and final status -- so an outcome the person was told
is "unknown" can be found afterwards. Nothing else reaches either.

**BEFORE THAT, 100, MCP fix round 1.** Three more sites: a write tool call that raised
in the MCP server's background task group (`app/mcp/invoke.py`, the tool name,
`exc_info=True`); a tool call that raised anywhere in the handler
(`app/mcp/server.py`, `logger.exception`, the tool name or `<invalid>`); and an
INFO for a rate-limited call that is not audited because the window's first
refusal already was (the person's id and the count). No argument, token or
payload reaches any of them.

**BEFORE THAT, 97, the MCP server's tool calls** (`docs/superpowers/specs/2026-10-09-mcp-server-design.md`
§4.3, §5.3, §7). Four new sites, all static WARNINGs: three in `app/mcp/invoke.py`
(a tool call the API answered with a 5xx, a 2xx that was not JSON, and a result
the scrub blocked) and one in `app/mcp/server.py` (the per-person rate limit
failing open on a Redis error, `exc_info=True`). `extra` carries the tool name,
the status and, for a block, the field PATH that matched -- a key name, never
its value. No argument, bearer token or result payload reaches any of them.

**BEFORE THAT, 93, the language mirror to the sign-in provider.** One new site:
`app/api/v1/me.py::_mirror_locale` logs a static WARNING (`exc_info=True`, no
`extra`) when `PATCH /me` could not write the saved language to the caller's
Keycloak user. The PATCH still answers 200 with `identity_locale_synced: false`.
The token and the user representation are never passed to the log call.

**BEFORE THAT, 92, the Spanish server catalogue.** One new site: `app/core/i18n/__init__.py::tr`
logs a static WARNING (`exc_info=True`, no `extra`, no parameter) when a Spanish
catalogue value cannot be formatted with the parameters its English template takes,
and answers in English instead of failing the request. The log carries no template
text and no parameter; the exception names the offending field.

**BEFORE THAT, 91, subnet push runs** (`docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md`
§1.4, review item 9, 2026-09-30). One new site:
`modules/agent_push/runs.py::_record_dispatch_failure`, an ERROR when one run
target's dispatch fails after its pass already dispatched others, and the pass
stops there. Static template; `extra` carries the run id, the target address
and the cause's exception TYPE, never its message, which may quote the signed
install parameters and with them the secret.

**BEFORE THAT, 90, scheduling and health defaults.** Two sites: a first
inventory collect that could not be queued (`modules/inventory`) and a baseline
health profile that could not be seeded (`modules/health/baseline.py`). They
name the device or org and nothing else. (Recorded at the time only in
`tests/test_logging_discipline.py`; entered here with the next change.)

**BEFORE THAT, 87, guacd retired** (`docs/modules/remote-control/spec-screen-share.md`
phase 1, 2026-09-23). Six sites removed with the code that dialled guacd: four
in the media tunnel (`modules/remote/tunnel.py`) and two in the byte bridge
(`broker.py`). Nothing replaced them; the screen share's own sites are the
thirteen below.

**BEFORE THAT, 93, screen share phase 0** (`docs/modules/remote-control/spec-screen-share.md`,
2026-09-23). Thirteen new sites: seven in `modules/remote/screen_relay.py` (the
gateway's relay), four in `modules/remote/screen_tunnel.py` (the api's viewer
leg), one in `modules/remote/jobtypes.py` and one in `gateway/main.py`. They
record refusals by reason code, legs ending, failed audits, ends and uploads,
and dropped-frame counts. None carries the relay token, the viewer ticket or a
frame body, which is the endpoint user's screen.
**NOW 81, the agent self-update cycle** (`docs/modules/agent-distribution/spec-self-update.md`,
2026-09-23). One new site, in `modules/agent_update/reconcile.py`: one org's
rollout tick failed and the others carry on, the same containment as every
`*_all_orgs` sweep. It names the org and nothing else.

**BEFORE THAT, 80, the device-groups rules cycle** (`docs/modules/device-groups/`,
2026-09-07). One new site, in `services/device_groups.py`: the rule evaluator
meets an inventory snapshot Pydantic cannot parse, counts that device as
un-collected, and carries on. That recovery is correct and it is also silent —
without the line, a group that had quietly stopped matching a machine would be
indistinguishable from a rule that no longer fits it. The record names the
device and the org and nothing out of the document itself, which is the
machine's own hardware and software inventory.

**BEFORE THAT, 79, the assistant-ships-enabled cycle** (`docs/modules/ai-admin/spec.md`
§0.14, 2026-08-25). Two new sites, both in `create_app`, one per branch of the
same question: this control plane has a model provider configured, or it does
not. They exist because that configuration became **the only switch on the AI
assistant** — the per-org `enabled` flag was removed — so "is the assistant on,
and where does it send" stopped being answerable from the database and became
answerable only from the process environment the operator wrote. A startup line
is where an operator checks that their `.env` edit took effect.

Static templates with %-args. The configured branch names the provider key, its
kind and its model and **nothing else**: not the base URL, not the API key path,
because a log line is a place internal hostnames and secret paths go to be
copied into tickets. Two sites rather than one interpolated sentence, so each
message template stays a constant a log aggregator can group on.

This sub-project builds the kernel that fixes that. Three other sub-projects
follow and are **not** in this spec: platform health and status, error
monitoring, and deploy rollback. Error monitoring depends on this one — it is
near-useless without correlation IDs — which is why this is first.

### 0.1 THIS IS NOT THE AUDIT LOG, and the distinction is load-bearing

`app/core/audit.py` maintains an **immutable, hash-chained, append-only**
record, blocked from `UPDATE`/`DELETE` by trigger, whose evidentiary value is
that the chain verifies (invariant #4). These logs are **operational and
disposable**: rotated by the log driver, unordered across processes, and
carrying no integrity guarantee whatsoever.

Nothing in this design may be read as making logs an audit source, and no
future change may route an audit event *only* through the logger. Stated here
because the failure is quiet: a hash-chained guarantee silently acquires a
second, mutable copy that somebody eventually quotes in an investigation.

---

## 1. Redaction: the invariant this kernel carries

Invariant #3: stored credentials "are **never written to logs**, never returned
in API responses, never stored in the application database in plaintext."

Today nothing enforces the log half. The only redaction machinery in the
codebase lives inside `app/core/ai/` and exists to protect the AI tool surface,
not the log path.

### 1.1 One predicate, promoted to the kernel

`app/core/ai/scrub.py::looks_like_secret_key` is a pure, well-tested predicate:
word-level, NFKC-normalised, matching over **two** decompositions (case-split
for `apiKey`, flattened for `sEcReT`) because each alone has a hole, and
handling plurals. It was hardened by measurement — an earlier version requiring
`_` missed 35 of 48 measured secret names.

It moves to **`app/core/secrets.py`**, and `scrub.py` imports it from there.

**The move must be lossless, and there is a ready-made check.** Four test files
import it via `app.core.ai.scrub` (`tool_surface.py`, `test_ai_read_tools.py`,
`test_ai_admin_scrub.py`, and transitively `test_ai_tool_surface.py`). A
re-export keeps every one of those import paths working, and
`test_ai_admin_scrub.py` asserts the predicate's behaviour directly. **Those
tests must pass untouched** — that is what makes the promotion a move rather
than a rewrite.

### 1.2 What is redacted, and what is not

The `JsonFormatter` redacts any **structured field whose NAME** matches
`looks_like_secret_key`, replacing the value with `"[redacted]"`. Fields arrive
through `extra={...}`.

**A second, unconditional gate sits alongside that predicate: `_NEVER_EMIT`**
(fix wave 7, widened fix wave 8, finding 3). A small, fixed frozenset of field
names — `traceback`, `exc`, `exc_text`, `einfo`, `args`, `kwargs`,
`stacktrace`, `stack_trace`, `tb`, `exception`, `exc_info` — that are redacted
regardless of what `looks_like_secret_key` says, because they structurally
carry rendered exception text or a call's raw arguments rather than a value a
developer chose to log. None of these NAMES reads as a secret to
`looks_like_secret_key` — that is precisely why a second gate exists — but a
field named `traceback` cannot, by construction, be anything other than
exception text, and `str(exc)` is the leak class §2 exists to prevent, reached
here through a field name a word-matching predicate has no reason to flag.
Compared **case-folded** against the field name (fix wave 8): the set is
otherwise redundant with a caller who happens to spell it `Traceback` or
`TRACEBACK`, and `looks_like_secret_key` itself is already case-insensitive —
this gate did not used to agree with it on that.

**AND HERE IS THE HOLE, STATED PLAINLY RATHER THAN DISCOVERED LATER.** Every
existing call site uses stdlib lazy formatting — `logger.warning("blocked
(tool=%s, key_path=%s)", name, path)` — and those `%`-args are rendered *into
the message string*. A name-based redactor cannot see them: there is no field
name to match, only text.

So the rule this spec sets is:

> **A value that could carry secret material goes in `extra=`, never in a
> `%`-arg.** `%`-args are permitted — they are idiomatic, already used 61 times,
> and their template is static — but they are outside the redaction gate, and
> putting a credential, token, URL or connection string in one is a leak the
> kernel will not catch.

This is a real limitation, not a wrinkle to be smoothed over. It is written
here, in the module docstring, and in the spec's §5 testing table, because the
alternative — implying the gate covers everything — is how somebody puts a
presigned URL in a `%`-arg believing they are protected.

**Key position has its own gate, stated here as it actually behaves after fix
wave 4 (a Critical: the widening below leaked this platform's own JWTs into
plaintext — see that fix wave's report for the full incident).**

- A dict key that is **not** a `str` is redacted **unless its exact type is on
  the key allowlist** — `int`, `float`, `bool`, `UUID`, `date`, `Decimal` — in
  which case it is coerced with the same guarded `str()` value position uses
  and then subjected to the same shape gate and secret-word predicate as any
  `str` key. **CORRECTED, fix wave 6 (I-2)**: earlier text here said a non-str
  key "is never stringified and is always redacted… no matter what its
  `__str__()` would have returned" — true of fix wave 4, false again once fix
  wave 5's key allowlist landed, and the spec kept asserting the stronger,
  now-false claim while the code moved on. Two types on the **value**-position
  allowlist do *not* work here despite being structurally credential-free —
  `Path` (`type(Path("/x"))` is `PosixPath`, never `pathlib.Path`, so
  exact-type membership never fires) and `datetime` (`str(datetime(...))`
  contains a space and colons, so it always fails the shape gate) — both still
  redact closed: `{Path("app.log"): "rotated"}` → `[redacted-key]`. Anything
  neither already a `str` nor one of the six coercible types — an exception,
  an arbitrary object — is never stringified at all and redacts outright:
  the `str(exc)` leak class §2 exists to prevent, reopened in key position (an
  exception used as a dict key), closed by never calling `str()` on a key that
  was not already trusted.
- A `str` key is emitted only if it is **ASCII**, **at most 40 characters**,
  and drawn from `[A-Za-z0-9_.-]`; anything else — non-ASCII (including a
  homoglyph), too long, or carrying any other character — redacts **closed**,
  key and value both. The 40-character cap is measured, not guessed: every
  dict-literal string key, annotated attribute name and keyword-argument name
  under `backend/app/` was swept — 1941 distinct names, 100% ASCII, longest 36
  characters, p99 = 26 — so the cap admits every real field name in this
  codebase with margin while rejecting an 85-character JWT. The ASCII
  requirement exists because `looks_like_secret_key` deliberately does not
  fold homoglyphs (`app/core/secrets.py`) — it relies on a declared-contract
  check that only the AI tool gate has; the log sink has none, so the shape
  gate itself must reject non-ASCII key material rather than trust the
  predicate to.
- **The residual, stated plainly:** a *short* (≤ 40 character), ASCII,
  field-name-shaped opaque token in key position — an AWS access key ID, a
  short PAT, a `xoxb-` token — is **still not** redacted, because
  `looks_like_secret_key` matches secret *words* and an opaque token has none.
  Long ones (JWTs, base64 blobs over 40 characters) are now caught by the
  cap, but the class is not closed for short ones — pinned by
  `test_an_opaque_token_in_key_POSITION_is_NOT_redacted`, the key-position
  twin of the `%`-arg pin above. **The rule this leaves in place: secret
  material never goes in key position — put it in a value under a field name
  you chose.**
- **A hostname used as a key is deliberately permitted, not an oversight —
  and the 40-character cap applies to it too.** `{"db-01": "online"}` and
  `{"db-prod-01.internal.toeshee.corp": "fail"}` (32 characters, this
  section's own example) both survive intact. An earlier review required
  hyphenated and dotted keys to keep surviving after the Critical fix that
  first tightened this gate — a hostname is infrastructure metadata, not a
  stored credential under invariant #3 — and that requirement still holds
  today; it is recorded here so the decision reads as a decision, not a hole.
  **But the cap above does not carve out an exception for hostnames**, and DNS
  permits 253 characters: `db-prod-01.eu-west-1.internal.toeshee.corp` (42
  characters) redacts closed, key and value both, exactly like an opaque
  token would (fix wave 5, I-3;
  `test_a_hostname_over_the_cap_redacts_closed`). That is a **loss of
  operator signal, not a leak** — the failure direction is the safe one, the
  same trade the rest of this gate makes — but "hostnames are permitted"
  read alone overstates it; the accurate statement is "hostnames under 41
  characters are permitted."
  **The falsifier, restated against the right population (fix wave 5,
  I-3):** the earlier wording — "if a legitimate field name ever exceeds 40
  characters" — pointed at the 1941-name measurement in the `_FIELD_NAME`
  comment, which swept **source-code identifiers** (dict-literal keys,
  annotated attributes, kwarg names). That population cannot falsify this
  cap: this gate judges **runtime** dict keys arriving through `extra=`, and
  this bullet's own permitted example — a hostname — comes from exactly that
  runtime population, not from source. What would actually falsify the cap:
  an operator finding `[redacted-key]` in place of a hostname, device id, or
  other runtime key they recognise and expected to see. If that happens in
  practice, the cap is what should change, and it should change against a
  measurement of runtime keys, not source identifiers.

### 1.3 Why name-based redaction is still the right foundation

It would not have caught either leak this project has actually suffered: a
presigned URL inside a `*url.Error`, and a hostname inside a `*net.DNSError`,
both **values inside exception text**. §2 is the answer to those.

Name-based redaction earns its place anyway because it is the only defence that
works on data the developer did not think about — a model dumped to `extra=`, a
config object logged whole. The exception-text rule handles the case they *did*
think about and got wrong.

---

## 2. Static templates, and exceptions by type

**The logger accepts a constant message plus keyword fields. Never an
interpolated string.** Exceptions are logged as their **type name** plus
explicitly chosen safe fields — never `str(exc)`, never `{exc}`, never
`exc_info` rendering into the message body.

This is what would have caught both real leaks: `*url.Error.Error()` returns
the URL, `*net.DNSError.Error()` returns the hostname, and both would have been
written by an innocent-looking `logger.error("upload failed: %s", exc)`.

### 2.1 Enforced by AST, not grep

`tests/test_logging_discipline.py` parses every module under `backend/app` with
`ast` and fails when:

1. a `logger.*()` call's first positional argument is not a string literal
   (an f-string is `ast.JoinedStr`, a `%`/`.format()` on the message is a
   `BinOp`/`Call` — all three are distinguishable from `ast.Constant`);
2. `str(...)` of anything appears among a log call's arguments;
3. an f-string appears **anywhere** among a log call's arguments, not merely as
   the message. Zero exist today (§2.2), so a blanket rule costs nothing and
   removes the judgement call about which interpolation is "safe".

**Grep cannot do this correctly.** It misses calls split across lines — which
is how the majority of this codebase's 61 sites are written — and it flags
matching text inside comments and docstrings. The AST knows the difference.

### 2.2 The good news, measured

All existing sites already comply: **zero** f-strings, **zero** `.format()`
on a message, **zero** `str(exc)` in a log call. This test therefore locks in
practice the codebase already follows rather than forcing a migration, and it
should be green the moment it is written. If it is not, that is a finding.

**CORRECTED 2026-08-14 — the count was 62, is 60, and HOW it was wrong is the
point.** The 62 came from a grep run while writing this section. The AST sweep
measures 60, and the difference decomposes exactly:

- **grep over-counted by 3.** Three `logger.debug(ctx)` occurrences live inside
  DOCSTRINGS — `app/core/ai/types.py:71`, `app/modules/ai_admin/api.py:46` and
  `:1022`. That is verbatim the failure §2.1 accuses grep of ("it flags matching
  text inside comments and docstrings"), committed by the author of §2.1 while
  writing §2.1.
- **grep under-counted by 1.** `app/modules/remote/tunnel.py:360` calls
  `t.exception()` on an asyncio Task. `_is_log_call` matches on the ATTRIBUTE
  name alone, deliberately, so `logger`, `log`, `self._log` and
  `logging.getLogger(x)` are all covered without a naming convention — and that
  looseness also catches `Task.exception()`.

**The loose match is KEPT and the false positive recorded rather than fixed.**
Requiring a logger-shaped receiver would reintroduce the naming dependence the
attribute match avoids, and would risk false NEGATIVES — a missed real log call
— which is the dangerous direction. The false positive is harmless:
`Task.exception()` takes no arguments, so every check in the sweep skips it.

**This 60 is itself now historical.** It is the count as measured when this
section was written, and the 62-vs-60 breakdown above is specific to that
measurement — it stays as-is. The count has since moved to **61** (see the
correction at the top of §0): a real `logger.exception` call was added to
`app/api/request_context.py` afterward. `tests/test_logging_discipline.py`
now asserts the AST sweep's total exactly, so this number cannot drift a
third time without a test going red.

---

## 3. Correlation

The kernel **always mints its own** `request_id` and never trusts an inbound
`X-Request-ID`. A caller-supplied id can collide with another org's or inject
misleading values into an operator's log search, and the tracing benefit does
not pay for that in a fleet-control plane.

`RequestContextMiddleware` sets contextvars and echoes `X-Request-ID` on the
response so a caller can still correlate.

**CORRECTED 2026-08-14 — the plan reversed on this and the spec was never
updated.** An earlier draft of this section said the middleware "emits one
access line per request with method, path, status and duration." It does not:
`app/api/request_context.py::request_context_middleware` only sets the
`request_id` contextvar, calls `call_next`, and echoes the response header
(plus, as of the pre-merge fix wave, logs the exception on an unhandled
failure — see below). Per-request access lines come from uvicorn's own access
logger propagating to the root handler (§4), not from this middleware, and
that line is unredacted framework output (§4/blocker 2), not a kernel-built
one.

| field | source | present when |
| --- | --- | --- |
| `request_id` | minted per request (uuid4) | every HTTP request |
| `org_id`, `user_id` | `AuthContext` | after authentication — **UNWIRED**, see below |
| `job_id` | job engine, via `bind()` | inside job execution — **UNWIRED**, see below |
| `device_id` | agent gateway, via `bind()` | inside gateway handling — **UNWIRED**, see below |

**UNWIRED, STATED PLAINLY.** `bind()` exists, is tested directly (§5), and is
exported — but it has **zero production call sites**. No code in
`app/core/ai`, the job engine, or the agent gateway calls it. The table above
describes the *design* for `org_id`, `user_id`, `job_id` and `device_id`; only
`request_id` is actually populated by running code today. Wiring the other
four into `AuthContext`, the job engine and the gateway is future work, not
part of this sub-project's delivered scope.

**Contextvars, not thread-locals** — the app is async throughout, and a
thread-local would bleed between concurrent requests sharing a worker thread.
§5 tests that directly rather than trusting it.

---

## 4. Output and library

**JSON lines to stdout.** The deployment is `deploy/docker-compose.yml`; the log
driver is the collector. No file handling, no rotation, no log shipping in this
sub-project.

**stdlib `logging` with a custom formatter — not `structlog`.** Two reasons, and
the second is the deciding one:

1. The redaction hook and template enforcement are custom either way, so
   `structlog` would add a dependency without removing custom code.
2. **uvicorn and FastAPI already log through stdlib.** A `dictConfig` formatter
   captures `uvicorn.access` and `uvicorn.error` too, giving their lines the
   same JSON *shape* as everything else this process logs. Under `structlog`
   those lines need separate wiring and, until somebody does it, escape even
   that.

   **THIS IS FORMATTING, NOT REDACTION, AND THE TWO MUST NOT BE CONFLATED
   (blocker 2 of the pre-merge review).** `JsonFormatter` redacts by `extra=`
   field name; uvicorn's own records carry no `extra=` at all; their content
   is entirely `%`-args uvicorn renders into the message string itself, which
   §1.2 already documents as outside the redaction gate. Routing
   `uvicorn.access`/`uvicorn.error` to the root handler changes their format
   and changes nothing about their redaction. This was a live gap, not a
   theoretical one: `app/api/v1/ws.py` read the WebSocket auth token from
   `websocket.query_params` (browser clients cannot set headers on a
   WebSocket handshake), so uvicorn's handshake line for that endpoint wrote a
   live bearer token to stdout, JSON-formatted and completely unredacted.

   **CLOSED 2026-08-16, in the transport rather than in this kernel** — the
   only place it could be closed, since there is no `extra=` field for the
   redactor to match. `ws.py` now reads the token from the WebSocket
   subprotocol header (`new WebSocket(url, ["bearer", token])`, which becomes
   a request header, and request headers are not in the log line) and refuses
   a query-string token outright. `backend/tests/test_ui_ws.py` measures it
   against a real uvicorn: the token is absent from everything the server logs
   after a real handshake, and the same capture still shows it when a URL-borne
   token is used as the control. The kernel's own boundary is unchanged —
   anything uvicorn renders into its message string remains outside the
   redaction gate, so the rule is to keep secrets out of the request line.

---

## 5. Testing requirements

Test-first per `CLAUDE.md`. Each row names what makes it fail.

| # | Test | Falsifier |
| --- | --- | --- |
| 1 | The formatter emits one valid JSON object per record, with `ts`, `level`, `logger`, `message` | emit a malformed record |
| 2 | A field named `password`, `api_key`, `token`, `secret` in `extra=` is `[redacted]` | remove the redaction hook |
| 3 | A field with a *non*-secret name survives intact | a redactor that flags everything passes #2 while destroying every log line |
| 4 | The AST check rejects an interpolated message | plant `logger.info(f"x {y}")` |
| 5 | The AST check rejects `str(exc)` in a log call | plant one |
| 6 | The AST check passes on all 61 existing sites | should be green immediately (§2.2) |
| 7 | `request_id` appears on every line inside a request and is echoed in `X-Request-ID` | drop the contextvar |
| 8 | **uvicorn's own access/error lines pass through the same formatter** | the most likely thing to be quietly false; asserted by capturing a real request's output |
| 9 | **Contextvars do not bleed between concurrent requests** | two interleaved async requests must never observe each other's `request_id` |
| 10 | `scrub.py`'s existing tests pass **untouched** after the promotion | proves §1.1's move was lossless |
| 11 | **A secret-looking value passed as a `%`-arg is NOT redacted** | pinned deliberately — see below |

**Test 11 asserts a gap, on purpose.** §1.2's limitation is otherwise a claim in
prose that nobody re-checks, and this project has repeatedly found prose claims
drifting from code. Pinning it executably means two things: a reader who assumes
the gate covers `%`-args is contradicted by a test, and the day somebody extends
redaction to cover them, **this test goes red and §1.2 must be rewritten** —
which is the correct trigger for updating the documentation rather than leaving
a now-false limitation on the record.

Tests 8 and 9 are where a naive implementation is wrong: framework logs
escaping the formatter, and context bleeding under async concurrency.

---

## 6. What this sub-project deliberately does not do

- **No log shipping, aggregation, or search.** stdout and the docker log driver.
- **No metrics.** Counters and histograms are a different concern.
- **No tracing spans.** `request_id` correlates; it does not measure.
- **No changes to the audit log** (§0.1).
- **No error monitoring** — sub-project 3, which builds on this.
- **No agent-side logging changes.** The Go agent has its own logger and its own
  idle budget; changing it is not in scope and would need its own falsifiers.
- **UPDATED, fix wave 6 (C-1): gateway and worker coverage is no longer out of
  scope — it is done, and it had to be.** This bullet used to say
  `configure_logging()` was reachable only from `create_app` (`app/main.py`)
  and that `run_gateway()`/the Celery worker were future work. That framing
  was itself the bug: fix wave 5 rewrote four exception-log sites across all
  three processes to `exc_info=exc` on the premise that `JsonFormatter`
  renders only `exc_type` from it — true only in a process that installs
  `JsonFormatter`, which was true for exactly one of the three. Measured
  under each process's actual pre-fix formatter: `logging.basicConfig`
  (`run_gateway`) and Celery's own installed `Formatter` (the worker) both
  call `formatException` and print `str(exc)` in the traceback tail —
  `LeakyErr: postgres://user:SECRETPW@db/prod`, credential present. `§4`'s
  "JSON to stdout" was therefore only ever true for the API process, not a
  platform-wide property, while the log sites written against it assumed it
  was one. `run_gateway()` now calls `configure_logging()` directly;
  `create_celery()` connects a receiver to Celery's `setup_logging` signal,
  which is what suppresses Celery's own logging configuration rather than
  merely running alongside it. All three processes now install
  `JsonFormatter` on their root logger.

  **NARROWED, fix wave 7 (C-1): "it is done" was true of this codebase's OWN
  log calls and false of a fourth source on the worker process --
  Celery's OWN logging.** Installing `JsonFormatter` on the worker's root
  logger routes every record through it, including ones this codebase never
  wrote: `celery.app.trace.TraceInfo._log_error`, the handler for every
  unhandled task exception, logs the full traceback, `repr(exc)` and the
  task's raw args/kwargs under field names (`traceback`, `exc`, `args`,
  `kwargs`) this kernel's name-based redaction has no reason to flag, AND
  embeds the exception into the message itself as a `%`-arg — outside the
  gate this spec already documents as a deliberate hole (§1.2). Measured: a
  DB password and a task kwarg secret both reached worker stdout. §6's old
  claim conflated "this process's OWN log calls are covered" with "this
  process's log OUTPUT is covered" — true only for a process that logs
  nothing but its own calls, which the worker never did. See §9 for the fix.

  **WIDENED AGAIN, fix wave 9 (C-1): §9's fix was itself scoped to one
  logger, `celery.app.trace`, and a second Celery-internal logger leaked
  the same class of secret through the same door.** `celery/worker/
  consumer/consumer.py` and `celery/worker/strategy.py` are both genuine
  `celery`-tree loggers, neither of them `celery.app.trace`, so §9's own
  fix never touched a record from either one. §10 has the full account.

  **CORRECTED, fix wave 10: the specific "measured" leak originally cited
  here — that a redis-py connection error's own text embeds the broker
  password, unmasked — is FALSE and is retracted, not softened or kept as
  a secondary example.** Measured against the pinned dependencies (redis
  8.1.0, kombu 5.6.2, celery 5.6.3) pointed at a dead broker on
  `redis://:S3CRETBROKERPW@127.0.0.1:16399/0`, both redis-py and kombu
  report only `Error 111 connecting to 127.0.0.1:16399. Connection
  refused.` — `redis/utils.py::format_error_message` interpolates
  `Connection._host_error()` (`f"{self.host}:{self.port}"`), never the URL,
  never the userinfo. The password does not appear. §10's real,
  reproducible justification for this same widening is `celery/worker/
  consumer/consumer.py`'s `dump_body(message, body)`, which renders the RAW
  SERIALIZED TASK BODY — this platform's provisioning and remote-control
  material — into the message at four call sites
  (`on_unknown_task`/`on_invalid_task`/`on_decode_error`, plus the
  message-report helper). The widening itself was still correct; only this
  stated reason for it was wrong.

  **The fix widens the ATTACHMENT POINT, not the guesswork**: the dedicated
  handler now sits on `logging.getLogger("celery")`, the tree root every
  Celery-internal logger descends from, with propagation off there instead
  of at `celery.app.trace` alone — Python's own logger hierarchy already
  encodes "every logger named `celery` or `celery.<anything>`" without this
  kernel enumerating submodules.

  **NAMED, fix wave 10 (F-3): the poison-message path is a further,
  distinct loss of operator signal, and it was not named until now.**
  `on_unknown_task`, `on_invalid_task` and `on_decode_error` carry no
  `extra=` at all, so a record from any of them renders with
  `name`/`id`/`hostname`/`description` all at the handler's
  `_CELERY_FIELD_UNKNOWN` placeholder — at exactly the moment Celery is
  **permanently** rejecting and discarding a task. The operator gets no
  task name, no id, no reason: nothing to correlate with the job that
  vanished. This is reduced, not merely accepted: `_synthesize_celery_
  record` now also reads a `message.headers`-shaped extra (`id`/`task`)
  the same way it already reads `data`, so a future call site that attaches
  one recovers task **identity** — structurally not credential-bearing,
  unlike the body — without ever reading Celery's own message text. No
  Celery-internal call site populates that extra today, so the three sites
  above still render fully unattributable; that residual is the accepted
  loss, named here rather than left implied-safe.

  **THE OPERABILITY COST, RECORDED HERE RATHER THAN LEFT IMPLIED-SAFE.**
  Every Celery-internal logger under this tree now loses its OWN message
  text the way `celery.app.trace` already did: "worker ready", "Connection
  restored", the connection-error text itself. What survives: the logger
  name, the level, the task identity when the record carries Celery's own
  `data` extra, the exception TYPE, and this kernel's correlation fields.
  That is a real loss of operator signal — under invariant #3, the right
  trade, but a trade and not a free win. **The remedy, if a specific
  message proves worth getting back:** a named, safe template built from
  known-safe fields the way `_synthesize_celery_record` already builds
  `celery.app.trace`'s rendering — never by letting the framework's own
  message text through unmodified again, which is the exact mistake this
  spec has now recorded three times (fix wave 7's filter, and the two
  scoping gaps fix waves 8 and 9 each closed one layer of).

---

## 7. What would falsify this design

- **If uvicorn's lines cannot be captured** by a `dictConfig` formatter in this
  app's startup order, §4's deciding reason is wrong and the library choice
  should be revisited.
- **If contextvars do not survive** the middleware → route → service call chain
  under this FastAPI version, §3 needs a different propagation mechanism.
- **If the AST check fires on existing sites**, §2.2's measurement was wrong and
  the discipline is a migration rather than a codification — which changes this
  sub-project's size materially.

---

## 8. Sources

- `CLAUDE.md` invariants #3 and #4
- `backend/app/core/ai/scrub.py`, `backend/app/core/audit.py`, `backend/app/main.py`
- Measurements run 2026-08-14: 61 log sites (60 at first measurement, +1 from
  the pre-merge review's blocker-4 fix — see §0 and §2.2), 0 interpolated,
  0 `str(exc)`; 4 test files importing `looks_like_secret_key`; fix wave 4's
  key-name sweep: 1941 distinct field names under `backend/app/`, 100% ASCII,
  longest 36 characters, p99 = 26

---

## 9. Fix wave 7 — the worker fix opened a second door, and closing it took two layers

**C-1.** Fix wave 6 installed `JsonFormatter` on the Celery worker's root
logger to stop `exc_info=exc` (used throughout `app/worker.py`'s
`_OrgCycleFailures.contain`) from rendering a traceback under Celery's own,
previously-installed formatter. That also routed Celery's OWN task-failure
record through the same handler for the first time:
`celery.app.trace.TraceInfo._log_error`, the handler for every unhandled task
exception, logs::

    context = {'hostname': ..., 'id': ..., 'name': ..., 'exc': safe_repr(eobj),
               'traceback': safe_str(einfo.traceback), 'args': ..., 'kwargs': ...}
    logger.log(severity, policy.format.strip(), context,
               exc_info=..., extra={'data': context})

`data` is not reserved, is field-name-shaped, and `looks_like_secret_key`
returns `False` for `data`, `traceback`, `exc`, `args` and `kwargs` — none of
those NAMES reads as a secret — so `_safe_value` walked the dict and emitted
every member verbatim. Measured before this fix: a DB password (in
`exc`/`traceback`) and a task kwarg secret (in `kwargs`) both reached worker
stdout. `_OrgCycleFailures.raise_if_total` makes this reachable, not
theoretical: it deliberately re-raises the first contained exception when
every org in a cycle failed (the DB-down / vault-unreachable case), and
`_log_error` prints that same credential-bearing exception two lines later on
the same stdout.

**A field-name denylist on `extra=` alone does not close this.** Celery's
`policy.format` embeds the exception into the MESSAGE itself as a `%`-arg —
`"Task %(name)s[%(id)s] raised unexpected: %(exc)s"` — and §1.2 already
documents `%`-args as outside the redaction gate BY DESIGN. Sanitising
`extra=` alone leaves the DSN sitting in the message text.

**The fix is two layers, both in `backend/app/core/logging.py`:**

1. **`_CeleryTraceSanitiser`**, a `logging.Filter` installed on the same
   handler `configure_logging()` builds. It recognises `_log_error`'s
   specific record shape (`record.name` starting with `celery.app.trace`, AND
   a `data` extra field that is a mapping carrying `traceback` — the marker
   unique to that call) and rewrites BOTH the message and its args to a
   static, credential-free template (`"celery task failed: %(name)s[%(id)s]
   on %(hostname)s"`) built from `name`/`id`/`hostname` only — the three
   `context` members that are pure operator signal and structurally cannot
   carry a credential. `exc_info` is left untouched, so `JsonFormatter` still
   renders `exc_type` from it, exactly what §2 permits. This targets ONE
   specific, known framework record shape; if Celery ever changes it, the
   filter simply stops matching, which is a DEGRADATION to fix wave 6's prior
   state, not a reopening — layer 2 is why.
2. **`_NEVER_EMIT`**, a frozenset of field names that structurally carry
   exception text or a call's raw arguments — `traceback`, `exc`,
   `exc_text`, `einfo`, `args`, `kwargs` — redacted UNCONDITIONALLY, at any
   depth, alongside `looks_like_secret_key`. This is §2 restated at the sink:
   exception text and a task's raw call arguments never go on a log line,
   regardless of who put them there — a future Celery version, a different
   task-tracing framework, or a caller who names a field `traceback`
   believing it is just another string.

   **CORRECTED, fix wave 8 (F-7):** this used to also claim "in both key
   paths," meaning both the TOP-LEVEL `extra=` loop and the NESTED walk one
   level down inside a dict value. False for `args` and `exc_text` at the top
   level specifically: stdlib `Logger.makeRecord` raises `KeyError` for an
   `extra=` key that already exists on the record, and both `args` and
   `exc_text` already do (`args` is a genuine `LogRecord` constructor
   parameter; `exc_text` is one `Formatter.format` sets during formatting) —
   see `_RESERVED`, which excludes exactly these `LogRecord`-owned names from
   ever reaching the top-level walk at all. A caller cannot pass
   `extra={"args": ...}` or `extra={"exc_info": ...}` through an ordinary
   logging call; both can still appear NESTED (`extra={"data": {"args":
   ...}}`), which `_NEVER_EMIT` does redact. The production code comment on
   `_RESERVED` already states this correctly; this was the spec's own claim
   that had drifted from it.

`§6`'s "gateway and worker coverage is done" claim (fix wave 6) is narrowed
accordingly: it was true of every log call this codebase writes, and false of
a fourth source the worker process also emits.

**I-1/M-4 — the top-level and nested key-collision rules had drifted.** The
module docstring in `JsonFormatter.format` claimed the top-level `extra=` loop
and `_safe_value`'s nested dict walk "can't drift" because they shared
`_shaped_key` — true of the SHAPE gate, false of the COLLISION handling fix
wave 6 (M-8) added, which only ever landed in the nested copy. Measured:
`{200: "from-int-key", "200": "from-str-key"}` as a record's own TOP-LEVEL
attributes lost the first entry outright; the identical dict one level down
did not. Both loops now call one shared function, `_shape_dict`, making the
claim true of the code rather than aspirational. That refactor also exposed
M-4: the collision suffix M-8 introduced (`"200-2"`) was itself
field-name-shaped, so it could collide with a THIRD, genuinely distinct
literal key spelling the same text (`{200: "a", "200": "b", "200-2": "c"}`
silently dropped `"b"`). The marker is now bracketed (`"200[dup-2]"`, via
`_collision_slot`), mirroring `_REDACTED_KEY`'s own reasoning: `[`/`]` are
outside `_FIELD_NAME`'s character class, so no key that has ever passed the
shape gate can equal it.

**I-2 — an exception with required keyword-only attributes did not survive
Celery's own exception handling.** `app/modules/sla/errors.py`'s
`UnsatisfiableCalendar` carries four attributes an operator needs (timezone,
requested minutes, the instant requested from, minutes still outstanding) as
required keyword-only constructor arguments. `BaseException.__reduce__`'s
default reconstruction calls `type(self)(*self.args)`, and `self.args` is
only `(message,)` — pickle and `copy.deepcopy` both raised `TypeError` for
the four missing arguments, and `celery.utils.serialization.
get_pickleable_exception` did NOT raise: it caught that `TypeError` and
silently substituted the nearest picklable base, `SlaValidationError` — the
exception's own TYPE was lost. §2 only lets a log line carry a type; losing
it here loses the one thing that was safe to keep. Fixed with an explicit
`__reduce__` returning a module-level rebuilder function plus every
constructor argument positionally, which keeps `UnsatisfiableCalendar`'s own
constructor keyword-only (a raise site still cannot forget an attribute)
while giving pickle a path back in that does not go through it directly.

**I-3/I-4/M-6 — the worker's `setup_logging` wiring had three latent gaps,**
all in `create_celery()` (`app/worker.py`):

- **I-3**: nothing tested the mechanism the C-1 fix wave 6 wiring depends on.
  `tests/test_logging_kernel.py::
  test_configure_logging_is_wired_into_every_process_entrypoint` asserts by
  AST that `app/worker.py` *contains* a call to `configure_logging` — true
  even if that call only happens inside a receiver connected with
  `weak=False`, and deleting that flag would garbage-collect the receiver the
  instant `create_celery()` returns while the AST check kept passing and the
  worker silently reverted to Celery's own formatter. A behavioural test
  (`tests/test_worker_sweep_containment.py::
  test_the_setup_logging_receiver_still_configures_the_worker_after_gc`) now
  builds the real Celery app, forces a GC pass, fires the signal, and asserts
  `JsonFormatter` is actually installed.
- **I-4**: the receiver discarded the `loglevel` Celery's
  `setup_logging_subsystem` sends on this signal (resolved from
  `--loglevel`, `deploy/docker-compose.yml`'s `--loglevel=info`), hardcoding
  `INFO` — changing that flag during an incident silently did nothing. The
  receiver now accepts and forwards it. Also documented, where it was
  previously implicit: connecting `setup_logging` is what makes Celery's
  `Logging.setup_logging_subsystem` see a non-empty `receivers` result and
  skip its own `_configure_logger` call — which is what disables
  `worker_redirect_stdouts`'s stdout/stderr → logger redirection, avoiding a
  `StreamHandler(sys.stdout)` ↔ `LoggingProxy` recursion. Benign today (no
  bare `print()` under `app/`) and load-bearing.
- **M-6**: every call to `create_celery()` left a NEW strong-referenced
  receiver connected forever (celery signals are never disconnected
  automatically) — one call per process in production, but many per test
  session. `dispatch_uid="app.worker.configure_worker_logging"` makes repeat
  `.connect()` calls idempotent.

---

## 10. Fix wave 9 — the suppression was one logger wide; the leak is tree wide

**C-1.** Fix wave 8's redesign (§9, wave 8's own account is folded into the
class docstrings in `app/core/logging.py` rather than restated in this spec)
was correct about the *shape* of the defence: suppress every record a
dangerous logger might emit, rather than detect and rewrite the ones that
happen to match a known-bad pattern. It was still wrong about *scope* —
scoped to `celery.app.trace` alone, which is a guess about which Celery
logger is dangerous, the exact style of guesswork the shape fix itself
argues against.

Measured through the real `configure_logging()` handler:
`celery/worker/consumer/consumer.py` logs a broker-connection failure as

    error(CONNECTION_ERROR, self.conninfo.as_uri(), exc, next_step)
    # CONNECTION_ERROR = 'consumer: Cannot connect to %s: %s.\n%s\n'

Celery is careful with the URL it prints itself — `as_uri()` masks the
password, giving `redis://:**@localhost:6379/0` — and `exc`, the second
`%`-arg, is the raw driver exception.

**CORRECTED, fix wave 10: this section originally claimed here that a
redis-py connection error's own text embeds the connection string
unmasked. That claim is FALSE, and it is retracted — not softened, not
kept as a secondary example.** Measured against the pinned dependencies
(redis 8.1.0, kombu 5.6.2, celery 5.6.3) pointed at a dead broker on
`redis://:S3CRETBROKERPW@127.0.0.1:16399/0`:

    redis-py -> ConnectionError | Error 111 connecting to 127.0.0.1:16399. Connection refused.
                password present: False
    kombu    -> ConnectionError | Error 111 connecting to 127.0.0.1:16399. Connection refused.
                password present: False

`redis/utils.py::format_error_message` interpolates `Connection._host_
error()`, which is `f"{self.host}:{self.port}"` — never the URL, never the
userinfo. The password does not appear, on either driver, at this measured
version pin. The two tests offered as proof
(`test_celery_worker_consumer_broker_connection_failure_never_leaks_the_
password`, `test_celery_worker_strategy_eta_conversion_failure_never_leaks_
the_dsn`, both since replaced) did not exercise redis-py at all:
`_BrokerConnErr.__str__` and `_StrategyDsnErr.__init__` hardcoded the
string the prose claimed, so both passed by construction against any
dependency version and could never falsify the premise. That is the actual
failure this correction records: a fabricated "measured" claim, proven only
by a test built to match its own prose rather than the dependency's real
behaviour.

**THE REAL, REPRODUCIBLE JUSTIFICATION FOR THIS SAME WIDENING, fix wave
10.** `celery/worker/consumer/consumer.py` logs `dump_body(message, body)`
at four call sites — `on_unknown_task` (`UNKNOWN_TASK_ERROR`), the
message-report helper and `on_invalid_task` (`INVALID_TASK_ERROR`), and
`on_decode_error` (`MESSAGE_DECODE_ERROR`) — and `dump_body` returns the
**raw serialized task body**:

    dump_body(m, b'[[], {"password": "PROVISIONING-SECRET", "device": "d1"}, {}]')
      -> 'b\'[[], {"password": "PROVISIONING-SECRET", "device": "d1"}, {}]\' (61b)'

That is task **kwargs** — this platform's provisioning and remote-control
material — rendered into the message string, exactly where §1.2 says
name-based redaction cannot reach. It fires on the same class of ordinary
operational event the retracted claim pointed at (Celery permanently
rejecting a poison message, not an exotic shape), and it reproduces against
the pinned dependencies rather than assuming them. `celery/worker/
strategy.py`'s `req.info(safe=True)` (`argsrepr`/`kwargsrepr`, logged at the
ETA-conversion error site — `error("Couldn't convert ETA %r to timestamp:
%r. Task: %r", req.eta, exc, req.info(safe=True), exc_info=True)`) is a
second, smaller instance of the same shape. Neither logger is
`celery.app.trace`, so fix wave 8's handler never saw either record; both
reached root's `JsonFormatter` unfiltered, which has no field name to
redact a value sitting in the **message string** (§1.2's own documented
hole, restated here because it is exactly what bit again).

**The fix widens the attachment point, not the guesswork.**
`configure_logging` now attaches the dedicated handler — renamed
`_CeleryHandler`, `celery.app.trace` no longer being the whole story it
renders — to `logging.getLogger("celery")`, the **tree root** every
Celery-internal logger descends from. `celery.utils.log.get_logger`
(Celery's own logger-construction helper, which every Celery-internal
`logger = get_logger(__name__)` call site uses, including `consumer.py` and
`strategy.py`) explicitly reparents any logger it hands out onto this same
root when it is not already a descendant of it — so this is not a
convention this kernel has to trust Celery to keep; it is enforced by
Celery's own logger factory. Propagation is turned off at `celery` itself,
not at `celery.app.trace`, and `celery.app.trace`'s own dedicated wiring is
**removed** rather than left alongside the new one: it is a descendant of
`celery` and reaches the identical handler by ordinary propagation, so a
second attachment point would be two places for this to drift apart, exactly
the kind of drift fix wave 7's untested install line already caused once.

**This is not a longer enumeration of "which submodules are risky" — it
does not enumerate submodules at all.** Python's own logger hierarchy
already encodes the predicate this wave needs: `record.name == "celery"` or
`record.name.startswith("celery.")`. A logger below `celery` that installs
no handler of its own — true of every stdlib-`logging`-based Celery
submodule — propagates up until something stops it, and that something is
now `celery` itself, regardless of how many submodules Celery adds in a
future version this kernel was never updated for.

**CORRECTED, fix wave 10 (F-4): the paragraph above is true only for names
beginning `celery` — it does not mean the underlying leak class is closed
platform-wide, and the wording read as though it might.** The mechanism
this whole section fixes — an exception's own `repr()`/`str()` rendered
into a message string, outside the name-based redaction gate (§1.2) — is
not specific to Celery. It is **open**, measured, on every OTHER logger
tree in this same gateway/API process, which is where the vault lives:

    {"level":"ERROR","logger":"asyncio",
     "message":"Task exception was never retrieved\nfuture: <Task finished …
     exception=RedisConnectionError('Error 111 connecting to …')>"}

`asyncio`'s `default_exception_handler` calls `repr()` on its context
values, and `Future.__repr__` embeds `repr(exc)` — exception text this
kernel refuses to print via `exc_info` reaches output anyway, pre-rendered
into a message this process's own `JsonFormatter` has no field name to
redact. `kombu.asynchronous.hub` (`logger.error('Error in timer: %r', exc,
exc_info=1)`, at two call sites, plus `'Callback %r raised exception: %r',
callback, exc`) and `kombu.asynchronous.timer` (`logger.error('Error in
timer: %r', exc, exc_info=True)`) have the identical shape, and neither is
a descendant of `celery` — `kombu`'s own logger tree is a separate
top-level root this kernel's `celery`-tree attachment never reaches.

**Recorded as an OPEN ITEM, not fixed this wave.** `asyncio`'s diagnostics
are broadly useful for reasons that have nothing to do with this one leak
class, and trading them away for suppression — the same trade this section
makes for Celery's own prose — deserves its own deliberate decision, not a
reflex extension of this wave's scope. The mechanism is stated here,
measured, so the wave that picks this up starts from a reproduction, not a
guess: closing it needs a `_synthesize`-style rendering built from
known-safe fields for `asyncio`/`kombu`, the same shape `_synthesize_
celery_record` already uses, never a blanket suppression that also throws
away diagnostics nothing else in the process provides.

**The anchoring property fix wave 8 established (finding 3) still holds at
the wider scope, and is pinned again at that scope rather than assumed to
carry over.** `celery.app.tracer_metrics` — fix wave 8's own proof that an
UNANCHORED `startswith("celery.app.trace")` swallows a sibling logger it
should not — is a genuine descendant of `celery` (`record.name.startswith
("celery.")` is true of it), so under this wave's widened rule it is now
correctly suppressed along with every other Celery-internal logger; keeping
it exempt would have been the opposite of "widen to the whole tree."
`celerybeat_custom` is the test case for the failure mode that must **not**
recur at the wider scope: a `startswith("celery")` check **without the dot**
would swallow it too, even though it shares only the letters, not the
hierarchy — this kernel's attachment is an exact `logging.getLogger
("celery")` lookup plus ordinary propagation, never a string comparison
against `record.name`, so `celerybeat_custom` (no `.` after "celery" at all)
is never touched.

**The operability cost, which must be documented, not hidden.** Celery's own
prose disappears from the logs: "worker ready", "Connection restored", the
connection-error text. What operators keep is the logger name, the level,
the task identity when the record carries `data`, the exception **type**, and
the correlation fields. That is a real loss and it is the right trade under
invariant #3 — a security invariant wins over convenience. **The remedy**, if
a specific Celery message proves essential to keep: it can be re-added
deliberately as a **named, safe template** built from known-safe fields — the
same shape `_synthesize_celery_record` already uses for `celery.app.trace`'s
own rendering — never by letting the framework's own text through again.

**NAMED, fix wave 10 (F-3): the poison-message path is a further, distinct
loss, and it was not named until now.** `on_unknown_task`, `on_invalid_task`
and `on_decode_error` (`celery/worker/consumer/consumer.py`) carry no
`extra=` at all, so every record from any of them renders with
`name`/`id`/`hostname`/`description` all at `_CELERY_FIELD_UNKNOWN` — at
exactly the moment Celery is **permanently** rejecting and discarding a
task. The operator gets no task name, no id, no reason: nothing to
correlate with the job that vanished. This is partly reduced, not left as
pure loss: `_synthesize_celery_record` now also reads a `message.headers`-
shaped extra (`id`/`task`) the same way it already reads `data`, so a
future call site that attaches one recovers task **identity** — never
credential-bearing, unlike the body — without reading Celery's own message
text. No Celery-internal call site populates that extra today, so the
three sites above still render fully unattributable; that residual is the
accepted loss.

**Tests** (`tests/test_logging_kernel.py`): the measured `dump_body` shapes
from `on_unknown_task` and `on_invalid_task` (fix wave 10, replacing the
retracted `CONNECTION_ERROR`/ETA-conversion tests — see the correction
above), each asserting the task body's credential is absent while the
logger name/level survive; a plain `celery` record with no `data` and no
exception, still well-formed; the `celerybeat_custom` anchoring proof;
`celery.app.tracer_metrics` now also suppressed (documented as an
intentional scope change, not a regression); the `message.headers`-shaped
extra recovering task identity (F-3); and the reverse proof — deleting
either line that installs the widened defence (`celery_logger.handlers[:]
= [...]` or `celery_logger.propagate = False`) turns the install test red,
exactly as fix wave 8's equivalent test did for the narrower scope.

**I-1 — fix wave 8's constructor defaults produced fake operator data.** To
make Celery's JSON-serializer path (`cls(*exc_message)`, i.e. no keywords at
all) reconstruct `UnsatisfiableCalendar` without raising, fix wave 8 gave its
four keyword-only parameters defaults: `timezone: str = ""`,
`requested_minutes: int = 0`, `start: datetime = _UNKNOWN_START` (an epoch
sentinel), `outstanding_minutes: int = 0`. Measured:
`UnsatisfiableCalendar("forgot everything")` constructs cleanly with
`requested_minutes=0`, `timezone=""` — and those render into
`tracking.py`'s SLA-misconfiguration log line as ordinary named fields,
**indistinguishable from measured values**: an operator reading
`requested_minutes: 0` has no way to tell "the calendar genuinely asked for
zero working minutes" from "this value was never provided." Fix wave 7 kept
the four arguments required precisely so a raise site could not produce that
ambiguity; fix wave 8 had to loosen it for reconstruction and, in doing so,
reopened it from a different direction.

**Fix:** all four now default to `None`, typed `str | None`, `int | None`,
`datetime | None`, `int | None`. The `_UNKNOWN_START` sentinel is dropped
entirely — an invented 1970 date sorting into real time-series data was
itself a symptom of the ambiguity, not a fix for it. `app/modules/sla/
tracking.py`'s `sla_misconfiguration_is_not_fatal` — the exception's only
log call site — now builds its `extra=` payload through
`_unsatisfiable_calendar_fields`, which **omits** any attribute that is
`None` rather than logging it (writing `None` through would only trade one
present-looking fake value, `0`/`""`, for another, a JSON `null` — omission
is what actually makes a reconstructed exception's line look different from
a properly raised one). A reconstructed exception's log line is now missing
all four fields; a properly raised one (every real raise site in this
codebase, `app/modules/sla/calendar.py`, still names all four) carries all
four unchanged — pinned by both directions in `tests/test_logging_kernel.py`,
through the exception constructor directly and through the actual log line
`sla_misconfiguration_is_not_fatal` writes.
