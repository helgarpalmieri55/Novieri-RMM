# UX remediation programme — closeout

**Date:** 2026-08-21  
**Programme:** `docs/superpowers/specs/2026-08-20-ux-remediation-program.md`  
**Audits:** `docs/superpowers/audits/2026-08-19-ux-audit-auditor-{a,b}.md`  
**Branch:** `claude/handoff-spec-review-f45idd`

## Why this document exists in this shape

The programme spec set the bar and it is worth repeating before the numbers:

> A finding is retired when the behaviour it describes no longer occurs **and a
> test would fail if it returned.** … Cycle 12 ends by walking both documents
> finding by finding and reporting, in writing, which are closed, which are open,
> and which were judged not worth fixing — with the reason. A programme that
> reports "233 findings addressed" without that walk has not been checked; it has
> been asserted.

So this is the walk. Every one of the 233 findings appears below with a verdict
and its evidence. The verdicts were produced by twelve verification agents, one
per module cluster, each given the complete verbatim text of its findings and
told in as many words **not** to trust a commit message, a code comment, a
previous agent's report, or a plausible-looking component — only the code and a
test that can fail. Where an agent could not reach a file, it said so and named
what was missing; those were finished by hand afterwards and are marked as such.

## The count

| Verdict | BLOCKER | HIGH | MEDIUM | LOW | Total |
|---|--:|--:|--:|--:|--:|
| Closed | 15 | 76 | 86 | 32 | **209** |
| Open | 1 | 5 | 3 | 3 | **12** |
| Not a defect | 0 | 0 | 0 | 12 | **12** |
| **Total** | 16 | 81 | 89 | 47 | **233** |

The twelve **not a defect** rows are the audits' own entries headed `Positive:`
— things the auditors singled out as done well. They are counted here because
they are numbered findings in the source documents, not because anything was
skipped.

**Nothing was marked WONTFIX.** The verdict existed and was offered to the
agents; none of them used it. Everything still open below is open because it
needs work that does not belong to this programme, and each one says what.

## What is still open, and why

Twelve findings. Ten of them are blocked on the same thing: **the console
cannot show or do what the API does not expose.** Every one was checked by
searching the backend, and each entry names the routes that were looked for.
Nothing was built that would 404.

### [B41] BLOCKER — Jobs cannot be cancelled, retried or requeued — the detail panel has no actions at all

*/jobs — Signed Job Engine* · auditor B, line 366

No cancel/retry/requeue control exists, and none can be built from the console.
Backend route search in /home/user/Toeshee-RMS/backend/app/api/v1/: `grep -n
'^@router\.' jobs.py` -> POST "" (jobs.py:105), GET "" (:164), GET "/{job_id}"
(:190) and nothing else; `grep -rn cancel app/api/v1/` matches only ws.py:181
(an asyncio task.cancel); a repo-wide `grep -rn
'@router\.(post|patch|delete|put)' app/api/ app/modules/*/api.py | grep -i
'job|cancel|retry|requeue'` surfaces only POST /jobs, POST
/health/alerts/{id}/remediate, POST /devices/{id}/health/check-now and POST
/subscriptions/{id}/cancel. The console states the limit rather than faking it
— JobsPage.tsx:313-316 renders "Jobs cannot be cancelled or retried from the
console yet. A queued job expires on its own at the time above.", tested at
JobsPage.test.tsx:264 "states plainly that it cannot cancel or retry" — but
that is disclosure, not a kill switch. Needs POST /api/v1/jobs/{job_id}/cancel
(and a retry route) before any button here is honest.

### [A33] HIGH — Queued jobs cannot be cancelled, inspected, or retried — the Jobs tab is read-only text

*/devices/:id — Device detail (Toeshee-sentinel)* · auditor A, line 240

The INSPECT half is closed: DeviceDetailPage.tsx:618-630 gives every job row a
Link to /jobs?job=<id> with a row-naming aria-label, and the Waiting column
badges a stuck job (:600-611); tests DeviceDetailPage.test.tsx:349 and :114.
CANCEL AND RETRY ARE STILL MISSING and cannot be built from the console.
Backend route search in /home/user/Toeshee-RMS/backend: `grep -n '^@router\.'
app/api/v1/jobs.py` returns exactly three routes — POST "" (jobs.py:105), GET
"" (:164), GET "/{job_id}" (:190). `grep -rn '@router\.(post|patch|delete|put)'
app/api/ app/modules/*/api.py | grep -i 'job|cancel|retry|requeue'` returns
only POST /jobs, POST /health/alerts/{id}/remediate, POST
/devices/{id}/health/check-now and POST /subscriptions/{id}/cancel — nothing
that cancels or retries a job. A Cancel button here would 404, so it was not
built. Needs a backend route first.

### [A58] HIGH — "Export evidence CSV" ignores the active username and event-type filters — the export does not match what is on screen

*/security — Security Logs* · auditor A, line 478

STILL PRESENT and cannot be closed in the frontend. The CSV href is built from
`fromTs` only (src/modules/security/SecurityPage.tsx:160) because the route
accepts nothing else: `access_evidence_csv` in
backend/app/modules/security_logs/api.py:135-141 declares exactly `from_ts` and
`to_ts` — no `username`, no `event_type` (searched `grep -rn "access.csv"
backend --include=*.py`, and the sibling `GET /security-events` at api.py:88-98
is the only route that takes those two filters). Appending them to the href
would send parameters FastAPI discards while the link claimed otherwise — the
deception made worse. MITIGATION SHIPPED AND TESTED (the auditor's own second
option): the button now reads "Export all events (24h)" naming the window
(SecurityPage.tsx:217), and whenever a username or event-type filter is set the
page states that the CSV does not carry it (SecurityPage.tsx:220-225). Tests:
SecurityPageEvidence.test.tsx "names the window it exports rather than implying
the filtered set", "says out loud which filters the CSV does not carry,
whenever one is set", "keeps the disclosure off the screen when no filter is
set" — all three verified red when the label is reverted to "Export evidence
CSV" and the disclosure removed. WHAT IS MISSING: `username`/`event_type` query
parameters on GET /api/v1/reports/access.csv (backend change, out of scope
here).

**CLOSED `2f9df81`.** The route takes both, built from the same
`_filtered_query` as `GET /security-events` so the file and the screen agree by
construction rather than by two implementations happening to match — there is a
test asserting they return the same set for the same parameters. The label the
closeout shipped was a placeholder and both halves are now spent: the button
reads "Export evidence CSV (24h)" again, keeping the WINDOW in the name because
a period is the one thing a detached CSV cannot say about itself, and the
disclosure naming the filters it did not carry is gone — a page still
apologising for a limitation it no longer has is lying in the other direction.
Two things the recommendation did not ask for and the invariants did: the
export wrote NOTHING to the audit log (invariant #4 — it now writes
`security_logs.evidence_exported` inside the same transaction as the read,
carrying the filters, the row count and the format and never the rows), and
neither route carried an application-level `org_id` predicate on either side of
its join, which matters because `security_events.device_id` is a bare FK and
that join puts a device HOSTNAME into a SOC 2 evidence file.

### [B43] HIGH — The "Signed job engine" never shows any signature information

*/jobs — Signed Job Engine* · auditor B, line 380

No signature, key id, algorithm or verification status appears anywhere on
/jobs, and the data does not exist to show. The column is on the model —
backend/app/models/core.py:320 `signature: Mapped[str | None]` — but
backend/app/api/v1/jobs.py's JobOut (:31-44) exposes id, type, device_id,
status, attempts, params, result, issued_at, expires_at, started_at,
finished_at, logs and nothing else; `grep -rn 'signature|key_id' app/api/v1/
app/modules/*/api.py` returns only an unrelated docstring at
security_logs/api.py:68. The frontend Job interface (src/lib/api.ts:452-465)
mirrors that exactly. Building a Signature panel would mean inventing fields,
so nothing was added. Needs JobOut to carry signature / key id / algorithm /
per-attempt verification result first.

**BACKEND HALF LANDED `1130265`; the panel is still to build.** `JobOut` now
carries `signed`, `signature_algorithm` and `signature_digest` — a SHA-256 name
for the signature — and deliberately NOT the signature itself: the envelope an
agent acts on is `payload + sig`, so the signature IS the authorization, and
`jobs.read` is held by `auditor` and `read-only`. Two of the four facts the
finding asked for are answered, the algorithm is a documented constant with a
test that fails if a second signer ever appears, and the fourth is still open
and recorded rather than approximated: **there is no key id**, because `jobs`
stores none, and a key identifier read off the API's CURRENT key would be a
confident false statement about a job signed before a rotation. Per-attempt
verification is likewise unrecorded — the agent verifies and the server never
learns the result — so a "verified" badge would be inventing a field, which is
what this finding was closed against.

### [B64] HIGH — The audit log identifies every actor and target by raw UUID only — you cannot tell who did what

*/audit — Immutable Audit* · auditor B, line 552

STILL PRESENT: actor and target are still identified by raw id
(src/modules/audit/AuditPage.tsx:319-330). WHAT IS MISSING is a backend route
that resolves a user id to a display name or a record id to a title. Searched
/home/user/Toeshee-RMS/backend/app/api/v1/ (`ls` shows audit, credentials,
devices, enrollment, errors, jobs, me, orgs, platform, realm_settings, roles,
setup, ws; `grep -rn "@router.get" app/api/v1/orgs.py app/api/v1/roles.py
app/api/v1/me.py`): there is NO users route — `/api/v1/me` returns a
display_name for the CALLER only (app/api/v1/me.py:111), and `AuditEventOut`
carries `actor_id`/`target_id` UUIDs with no name. Building a lookup or linking
the target would 404 (the fixture target_id is a hostname, not a route param).
PARTIAL IMPROVEMENT SHIPPED: the TYPE words are no longer wire identifiers —
`health_profile` reads "Health profile" (AuditPage.tsx:320,326), and the full
id is in a `title`. Test: AuditPage.test.tsx "reads the actor and target types
as words, not as wire identifiers", falsified by reverting to
`{event.actor_type}`.

**BACKEND HALF LANDED `ed8282a`; the console still renders the id.**
`AuditEventOut` now carries `actor_display_name` and — for `target_type =
"user"`, the one target type whose id names a person — `target_display_name`,
resolved from `users` at read time in one statement per page. The premise in
this entry was wrong in a way worth recording: there IS a users route, `GET
/api/v1/users` in `app/modules/user_admin/api.py`, and it was rejected as the
instrument rather than merely missed — it is gated on `users.read`, which
`auditor` does not hold, so a client-side lookup would leave the one role that
exists to read this log staring at UUIDs. Nothing stored moved: a name is not
one of `_canonical_fields`, so the chain is untouched and `GET /audit/verify`
is unaffected.

### [B76] HIGH — "Resolve" is a one-click, unconfirmed, irreversible state change — there is no way to reopen a group

*/error-monitor — Error Monitor* · auditor B, line 694

PARTIAL FIX, NOT CLOSED. Fixed and tested this run: (1) Resolve is no longer
one-click — frontend/src/modules/error-monitor/ErrorMonitorPage.tsx:388
ConfirmDialog names the group and states the irreversibility; the button label
is now 'Resolve…' (:471). Test 'writes nothing on the first click and names the
group in the confirmation' + 'resolves nothing when the confirmation is
cancelled'. (2) The write is no longer silent — live region at
ErrorMonitorPage.tsx:228 (role=status, present-and-empty before the change),
announcement set at :404. Test 'announces the resolution instead of only
changing a number'. (3) A resolved group is no longer unreachable — state
filter (see B77). All three falsified by reverting to a direct resolve.mutate
(16 tests went red). STILL OPEN: there is no Reopen/Unresolve. Blocker is a
missing backend route. I read backend/app/api/v1/errors.py in full — it defines
exactly three routes: GET /platform/errors (:139), GET
/platform/errors/{group_id} (:158), POST /platform/errors/{group_id}/resolve
(:207) — and grepped 'unresolve|reopen|resolved_at' across /home/user/Toeshee-
RMS/backend/app/api/v1/ (only devices.py's unrelated SuggestedAssetMatch
matched). A Reopen control would 404, so none was built.

**CLOSED later the same day.** Both halves now exist. The route landed in
`bd04957` — `POST /api/v1/platform/errors/{group_id}/unresolve`, gated on
`platform.errors.write`, the same code as resolve. Building it surfaced a
second gap the audit had not seen: **resolve was never written to the audit
log at all**, which invariant 4 requires of every privileged action, so both
directions are now appended inside the same `org_session` as the row change.
The undo does not blank `resolved_at` and call that a restore — the group
carries no `resolved_by` and no note, so that column *is* the whole record
that anyone ever resolved it; the reopen event carries `previous_resolved_at`
and the actor instead, and the append-only log keeps what the column gives up.
The console half landed in `b657185`: a resolved row carries **Reopen** where
it carried a sentence saying there was nothing to offer. It takes no
confirmation — reopen removes nothing, and a dialog there would be the second
in a row for someone correcting a misclick, which is how confirmations get
clicked through. Resolve is now gated too: both writes carry one code
server-side, so a gated Reopen beside an ungated Resolve would invent a
distinction the API does not make.

### [A36] MEDIUM — "Reboot" card occupies a full third of the Patch tab to display one badge

*/devices/:id — Device detail (Toeshee-sentinel)* · auditor A, line 261

Half fixed, half present. The wording half is closed —
modules/patch/DevicePatchTab.tsx:18-21 renders "No reboot pending"/"Reboot
pending" instead of "Up to date", tested at
modules/patch/__tests__/patch.test.tsx:865. The LAYOUT half is untouched:
DevicePatchTab.tsx:11 is still `grid grid-cols-1 gap-4 p-4 md:grid-cols-3` with
the one-badge Reboot card as an equal third beside two five-row cards, so it
still spends a third of the tab on ~130px of white space. Fixing it means
letting cards size to content or collapsing the chip into the header — both
inside frontend/src/modules/patch, which is not in my editable set (jobs,
devices, inventory only).

### [A9] MEDIUM — All timestamps are raw locale strings with no relative time and no timezone

*(cross-cutting)* · auditor A, line 66

Behaviour partly fixed, remainder untouched. Fixed:
frontend/src/lib/datetime.ts:99 formatDateTime renders en-GB '18 Aug 2026,
06:25' — unambiguous month, no seconds, profile-timezone aware — and is adopted
in 50 module files (grep -rln formatDateTime src/modules), so the two-
formats/M-D-YYYY/seconds halves are gone. Still missing, exactly: (a) relative
time as the primary label ('25h ago') — no formatRelative, no 'ago', no
Timestamp component anywhere (grep -rn 'ago\|Relative' over src/lib,
src/components, src/shell returns only unrelated prose); (b) a timezone
indicator — grep -rn timeZoneName src returns nothing; (c) <time datetime="…">
markup. Not fixed here because the only safe lever is formatDateTime's output
string, and 27 test files in other agents' slices assert on its exact text
(e.g. modules/jobs/JobsPage.test.tsx:125 expects '20 Aug 2026, 05:00');
changing it mid-cycle would break them. Adding an unadopted helper would not
close the finding.

### [B46] MEDIUM — Job list has no filter, search, sort, pagination or refresh control

*/jobs — Signed Job Engine* · auditor B, line 401

Three of the five asked-for controls are present and tested — a search box over
type/id/device_id (JobsPage.tsx:143-152, test JobsPage.test.tsx:200 "searches
by job type, and says so when nothing matches"), a status filter that includes
a "Waiting too long" option (:153-166, test :188 "filters the list by status"),
and live refresh via useLiveEvents invalidating ["jobs"] on job_status/job_log
(:66-70). SORT AND PAGINATION ARE STILL MISSING, and I deliberately did not add
client-side sorting: src/lib/api.ts:900 `listJobs()` sends no query string, so
the list silently takes the backend default of limit=100
(backend/app/api/v1/jobs.py:170) while the KPI tile shows the true total —
sorting a silently truncated page by "oldest first" would show the newest 100
re-sorted, which is worse than no sort. Both need listJobs to accept
limit/offset/ordering, and src/lib/api.ts is outside my editable set.

**THE BLOCKER IS GONE (`1130265`, `8d11f35`); the sort control is still to
build.** `listJobs()` now sends `limit`, `offset` and `order`, and `GET /jobs`
grew the `order` it did not actually have — this entry and recommendation 10
both say the server "already supports all three", and it supported two, with
the ordering fixed at `Job.id.desc()`. `newest`/`oldest` orders on `id` (a
uuid7, never rewritten) rather than on `issued_at`, which `resign_for_retry`
restamps and which would therefore make an offset page show a row twice or skip
one. A sort is now safe to add for the reason this entry withheld it: the
ordering happens where the LIMIT does, so a page of "oldest first" is the
oldest, not the newest hundred re-sorted.

### [A55] LOW — The error message pushes the Save button into a two-line wrap

*/health — Health & Alerts* · auditor A, line 454

Behaviour fixed, untested. The error is no longer a long JSON string, the
button label at this call site is the single word "Save"
(HealthSettingsPanel.tsx:271, so it cannot wrap mid-label), and .form-actions
is display:flex;flex-wrap:wrap (shell/shell.css:398), which sends an over-long
message to its own row rather than shrinking the button below its min-content
width. What is missing is a test: jsdom performs no layout, so nothing I can
write in src/modules/health would fail if the button deformed again. A real
guard needs the browser-side axe/geometry pass the contrast suite calls its
complement, or a flex-shrink:0/white-space:nowrap declaration on .btn pinned
from shell/components.css — a file outside this slice.

### [B5] LOW — Field labels sit inline and wrap mid-phrase, splitting label from control

*/sla — SLA Engine* · auditor B, line 35

Still present, and I cannot fix it inside this slice. components/ui.tsx#Field
renders <label class="field"><span>label</span>{input}</label>, and NO
stylesheet in the app defines `.field` — grep -rn '\.field' over src/*.css and
src/shell/*.css returns only .search-field/.select-field/.input-field. The
design file has the rule (docs/design/itms-prototype.html:733: `.field {
display: flex; flex-direction: column; gap: 6px; }`) and the port never
transcribed it, so a <label> is inline and its text still flows alongside the
control exactly as the auditor measured. The one-line fix belongs in
frontend/src/shell/components.css (not in my file list; another agent is mid-
edit in it) or in components/ui.tsx#Field.

### [B79] LOW — Raise site exposes internal library paths and line numbers

*/error-monitor — Error Monitor* · auditor B, line 715

Still present, and not fixable from the frontend: the group's
module/lineno/qualname are computed server-side.
backend/app/core/errors/capture.py:132 `fingerprint_of` hashes
`type|module|lineno|qualname` taken from `_raise_site(tb)`, which the module
docstring (:12) says walks to the traceback's LAST frame — i.e. the third-party
library frame, exactly as the auditor described for asyncpg. The auditor's fix
(prefer the deepest `app.*` frame, keep the library frame as supporting detail)
is a change to capture.py plus a re-fingerprinting decision for existing rows;
capture.py is outside my editable file list
(frontend/src/modules/{enrollment,error-monitor,platform-status}), and no
frontend change can re-group rows the server already grouped.

## The backlog this leaves, as recommendations

These are backend changes. Each unblocks one or more findings above, and none
of them is UI work, which is why they are recommendations rather than commits:
this programme's remit was the console. Each would be a normal module change —
brief, spec, plan, tests — under the build discipline in `CLAUDE.md`.

| # | Change | Unblocks | Note |
|---|---|---|---|
| 1 | `POST /jobs/{id}/cancel` and `/retry` (or `/requeue`) | B41 (BLOCKER), A33 | The signed-job engine has no lifecycle verbs at all. Cancellation of a job already dispatched is a security-relevant design question — an agent may already hold it — so this wants a brief before code. |
| 2 | Signature fields on `JobOut` | B43 | **LANDED `1130265`.** Not the signature, and the distinction is the whole change: an agent executes `payload + sig`, so the signature is not a description of an authorization, it IS the authorization — and `jobs.read` is held by `auditor` and `read-only`, far below the `jobs.dispatch` that authorized it. That is the reasoning `5417267` already wrote down when it put `jobs.signature` and `jobs.nonce` on reporting's `_NEVER_A_COLUMN`, and returning them from a route would route around the same gate one layer higher. `JobOut` carries `signed` (read off the nullable column, so a row with no signature says so), `signature_algorithm`, and `signature_digest` — SHA-256 over the stored base64, a stable one-way NAME for the signature, which is what lets a reader see that the list and the detail agree and that a retry re-signed the envelope. A test asserts the raw signature appears nowhere in the body under any key; the existing guard pinned only the absence of a `signature` KEY, which a careless digest field would satisfy. The algorithm is a constant with an alarm on it: the schema holds no per-job algorithm, `app/core/signing.py` has exactly one, and a test fails if a second appears. **Still open, and recorded rather than approximated: there is no key id.** B43 asked for one and `jobs` stores none, so any key identifier on a job row would name the key the API holds NOW, not the one that signed that row — a confident false statement after a rotation, about the exact thing an operator would be checking. It needs a column written at signing time, i.e. a migration and a change to the signing path. |
| 3 | `GET /device-groups` | patch/deploy group targeting; per-group patch policies | **LANDED `787461f`.** The scope was wider than this row states: `device_groups` is also what `deploy`/`scripts` resolve a `target_type="group"` job against, what `patch`, `backup` and `remote` key a policy on, and what `user_roles.device_group_id` narrows an RBAC grant to — so "can only ever address the org default" was true of targeting and policy and role scoping alike. Gated `devices.read`, the code `reporting/registry.py` already maps the table to. The row carries `parent_group_id` and a DIRECT-membership `device_count`, because deploy matches `device_group_id == target_id` without descending the tree while health assignment inherits up it; the envelope carries `ungrouped_device_count`, which is the org default's own population and is not derivable from a page of groups. Still open, and deliberately not bolted on here: **nothing creates a device group** — the only `DeviceGroup(` constructors in the repo are in `tests/`, so on a real deployment this list is correct and empty until a create route exists. |
| 4 | `GET /patch/approvals` | Revoking an approval | **LANDED `15ed5de`.** `DELETE /patch/approvals/{id}` exists and is unreachable, because nothing can produce the id. |
| 5 | `last_scan_at` on `GET /patch/report` | Patch tiles reading 0 | **LANDED `15ed5de`.** Read off succeeded scan jobs, not `detected_at`, which is an insert-time stamp; two further readers of that column were wrong the same way and were fixed in `cad191a` (the sweep re-scanned settled machines 24x a day) and `5417267` (the compliance report dated a settled fleet to enrolment day). Without it the UI cannot distinguish "zero exposures" from "never scanned", and guessing would be the /reports nine-zeros defect again. |
| 6 | A user-name lookup (or names on `AuditEventOut`) | B64 | **LANDED `ed8282a`.** Names on the response, and the row's other option was already built: `GET /api/v1/users` exists in `app/modules/user_admin/api.py` — the closeout searched `app/api/v1/` and missed it — gated on `users.read`. It is the wrong instrument twice. Migration 0003 gives `auditor` `devices.read`, `jobs.read`, `audit.read` and `credentials.read` and NOT `users.read`, so a client-side lookup leaves the one role that exists to read this log staring at UUIDs, and granting it `users.read` widens a privileged code to fix a display defect; and that route returns the whole directory, every email and role assignment, which this codebase's own tool-surface census calls the reconnaissance an attacker would want first. There is a test asserting the auditor refused `GET /users` gets the names anyway. **Nothing stored moves:** `record` hashes `_canonical_fields`, built from the columns, and a name is not one — it is joined from `users` at read time in one statement for the page. The test asserts the stored `hash` is byte-identical after a read and that `GET /audit/verify` still passes. `actor_id` stays exactly where it was, because the name is the directory's answer NOW and the id is what the log holds. `target_display_name` resolves only `target_type = "user"`, the one target type whose id names a person; the rest are hostnames and job ids no generic resolver could name. Where there is no name nothing is invented — a system actor, a deleted user and an empty `display_name` all resolve to null, and the email address is deliberately NOT a fallback, because an address is the directory route's disclosure and `audit.read` is not that gate. The tenancy predicate is pinned on the BYPASSRLS engine: deleting it turned no route test red, verified by doing it. |
| 7 | `username` and `event_type` on the evidence CSV route | A58 | **LANDED `2f9df81`, console in the same commit.** Both routes now build on one `_filtered_query`, which is the point rather than tidiness: A58 is not "the CSV takes no filters", it is "the CSV does not match the screen", and matching means the same predicate — there is a test asserting the two routes return the same set for the same parameters. The honest label was a placeholder and both halves are spent: the button reads "Export evidence CSV (24h)" again, keeping the WINDOW because a period is the one thing a detached CSV cannot say about itself, and the disclaimer naming the filters the file did not carry is gone, since a page still apologising for a limitation it no longer has is lying in the other direction. **Two things the row did not ask for and the invariants did.** The export recorded NOTHING in the audit log — a file of who signed in to what, leaving the platform, is a privileged act (#4), the same gap `bd04957` found behind the error monitor's resolve — so it now writes `security_logs.evidence_exported` inside the same transaction as the read, carrying the filters, the row count and the format and never the rows; a SEARCH still writes nothing, asserted in the same file. And neither route had an application-level `org_id` predicate at all, on either side of the join: `security_events.device_id` is a bare FK, so an event may legally name a neighbour's device, and this join exists to put that device's HOSTNAME into a SOC 2 evidence file. Both predicates are pinned on the BYPASSRLS engine, where RLS is not the thing rejecting. Spec §8 is amended. |
| 8 | `POST /platform/errors/{id}/unresolve` | B76 | **LANDED `bd04957`, console in `b657185`.** Resolve is one-way. The confirmation and the announcement are in; there is no route to undo it. |
| 9 | Prefer the deepest `app.*` frame in `fingerprint_of` | B79 | **LANDED `f5e8ab0`.** Capture prefers the deepest `app.*` frame — the last one in `tb_next` order, i.e. nearest the raise — and keeps the last frame as the fallback for a failure with no application frame at all. Two refinements the row does not state: the traceback's FIRST frame is never a candidate, because every capture seam in this codebase is itself `app.*` and preferring app frames naively would name library failures after the seam, which is the catch-site grouping spec §2.1 already rejected; and application code is derived from the capture module's own `__name__` rather than written out as a literal. The re-fingerprinting decision is that there is none to make: `platform_error_group` stores the derived site and never the traceback, so the app frame a library-named group should have carried is not in the database and **no backfill can be written** — migration 0069 says so at the site where someone would try. Existing rows keep `fingerprint_version = 1` and age out; new captures record 2, and the version is hashed into the digest so the two eras cannot collide on a `fingerprint` column that is still UNIQUE. Spec §2.2 is the amended rule. |
| 10 | A paged envelope (`items` + `total`) on `GET /service-desk/tickets`, and `limit`/`offset`/ordering on the client's `listJobs()` | B46 | **LANDED `8d11f35`, with the `GET /jobs` half in `1130265`.** The row's premise was half right: `GET /jobs` supported `limit` and `offset` and its ordering was FIXED at `Job.id.desc()`, so the ordering had to be built too. It is `newest`/`oldest` over `id` — a uuid7, issue-ordered and never rewritten, so a total order needing no tiebreak, and `newest` is byte-for-byte what the route always returned (asserted). `issued_at` is disqualified rather than overlooked: `resign_for_retry` restamps it, and a sort key that moves under an offset page makes that page show a row twice or skip one. An unknown value is a 422, not a silent fallback, because a caller told "oldest" and handed "newest" reproduces the deception elsewhere. `count_tickets` shares `list_tickets`' filter builder, so the total cannot drift from the rows beside it. The console half: the truncation notice inferred truncation from a FULL page and now reads `items.length < total`, which also fixes a case it used to get wrong — an org with exactly 200 tickets was told rows were missing when none were. Only the single-filtered-query view names numbers ("showing 200 of 412"); the default view runs two queries over overlapping sets and has no union total, so it keeps the unnumbered sentence rather than adding two counts into a number larger than the queue. Three callers broke on the shape and are fixed. **The census was re-measured once, against the tree, with all four recommendations present:** 204 → 204 GET routes (none of the four was a new endpoint), P5 166 → 162 — the bill went DOWN, and the four routes that left it are the four whose response models this cycle changed — 23 → 27 passing five, and `/api/v1/service-desk/tickets` passes all six and joins `PENDING_TOOL_REVIEW`, because `TicketOut` carries `subject` and `description` and the route is therefore every ticket in the tenant in full. |

## The walk

Every finding, in the order the audits list them. `A` is auditor A
(134 findings), `B` is auditor B (99).


### (cross-cutting)

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A1 | M | Post-login redirect discards the originally requested deep link | closed | frontend/src/lib/auth.ts:176-180 — signIn() records `window.location.pathname + search` into the OIDC state (excluding /auth/callback); frontend/src/App.tsx:141 — CallbackPage navigates to safeReturnTo(user?.state), with the open-redirect whitelist at auth.ts:270-275. Test that would fail if it returned: src/lib/auth.accountAction.test.ts > 'signIn' > 'records the page the user was on, so re-authentication returns them to it' (asserts signinRedirect is ca… |
| A2 | M | Login screen is a single unlabeled button with no context, no error surface, and no product/tenant identifica… | closed | The single-button login card no longer exists: frontend/src/App.tsx:224-246 RequireAuth redirects an unauthenticated visitor straight to Keycloak (guarded by a ref so strict mode does not start two PKCE flows). The failure surface the auditor said was missing is App.tsx:247-262 — it names the authority that failed (oidcAuthority) and, as of this run, carries role="alert" (App.tsx:250). Tests: src/App.auth.test.tsx:62 'is sent to Keycloak rather than shown… |
| A3 | B | Sign-out redirect is misconfigured — user is stranded on a raw Keycloak "Invalid redirect uri" error page wit… | closed | frontend/src/lib/auth.ts:69 sends post_logout_redirect_uri = `${window.location.origin}/` (trailing slash load-bearing); deploy/keycloak/realm-it-suite.json:38 registers `post.logout.redirect.uris: "/"` on it-suite-web — no longer the `+` shorthand that expanded to callback paths only. deploy/keycloak/themes/itms/login/error.ftl gives the terminal page a 'back to application' link. Test that would fail if it returned: backend/tests/test_console_auth_realm… |
| A4 | B | Clicking the profile chip signs the user out instantly — no menu, no confirmation, no undo | closed | frontend/src/shell/AccountMenu.tsx:88-96 — the chip is a button with aria-haspopup="menu" and aria-expanded, whose onClick toggles a role="menu" panel; sign-out is a separate destructive menu item at AccountMenu.tsx:118-129. Escape closes and returns focus (AccountMenu.tsx:49-58), arrows rove (:71-84). Tests: src/shell/AppShell.test.tsx:151 'declares itself a menu button rather than looking like one', :161 'opens a menu on click instead of signing the use… |
| A5 | H | Tenant switcher is a dead control — looks like a dropdown, does nothing | closed | The header tenant-switcher button is gone — frontend/src/shell/AppShell.tsx:176-192, .top-actions now holds only <AccountMenu>. The sidebar copy is no longer a control either: frontend/src/shell/Sidebar.tsx:94 renders a <div className="tenant-card"> and shell.css:364 removes the pointer cursor. grep for 'tenant-switcher' across src returns zero non-test hits. Test: src/shell/AppShell.test.tsx:237 'presents the tenant as identity, not as a control that doe… |
| A6 | M | Two controls compete to represent the same tenant, in two different visual languages | closed | One tenant affordance remains, in the sidebar footer the auditor recommended: frontend/src/shell/Sidebar.tsx:92-101. Test: src/shell/AppShell.test.tsx:246 'names the tenant once, not twice in two visual languages' — expects getAllByTestId('tenant-identity') to have length 1, so restoring a second header copy turns it red. |
| A7 | M | Dashboard two-column grid leaves an ~800px empty void in the left column | closed | frontend/src/shell/components.css:135 — `.grid-2 { … align-items: start; }`. The ~800px void was grid's default `stretch` dragging the single left panel to the height of the four-panel right stack; `start` lets each side end with its content. Scoped to .grid-2 only, so .grid-3/.grid-4 peer rows keep equal heights. Test: src/shell/chrome-responsive.test.ts 'does not stretch the short column to the tall one's height', with the non-vacuity sibling 'leaves th… |
| A8 | H | Jobs queued for over 24 hours are reported as healthy — "Jobs needing attention: 0" | closed | DashboardPage.tsx now counts stuck jobs alongside failed ones via `isStuck` from modules/jobs/jobState.ts — the same predicate /jobs badges its Waiting column with. Tests: 'counts a job that has sat queued for a day as needing attention', 'still reports zero when the queue is merely busy' (the negative control), 'adds a stuck job to the failures rather than replacing them'. Falsified. |
| A9 | M | All timestamps are raw locale strings with no relative time and no timezone | **open** | Behaviour partly fixed, remainder untouched. Fixed: frontend/src/lib/datetime.ts:99 formatDateTime renders en-GB '18 Aug 2026, 06:25' — unambiguous month, no seconds, profile-timezone aware — and is adopted in 50 module files (grep -rln formatDateTime src/modules), so the two-formats/M-D-YYYY/seconds halves are gone. Still missing, exactly: (a) relative time as the primary label ('25h ago') — no formatRelative, no 'ago', no Timestamp component anywhere (g… |
| A10 | M | Body copy and table headers rendered at 9-11px | closed | No stylesheet in the chrome declares text below 12px: `grep -no 'font-size: *[0-9]*px' src/shell/*.css src/index.css` yields only 12/13/14/15/17/25/27px. The last survivor was `.code`, written as the `font` SHORTHAND (`font: 10px/1.7 ui-monospace…`), which the cycle-9 sweep's `font-size:` regex could not see — it carries enrolment tokens, audit hashes and the JSON fallback. Raised to 12px at frontend/src/shell/components.css:354. Tests: src/test/contrast.… |
| A11 | H | Table column headers fail WCAG 1.4.3 contrast | closed | frontend/src/shell/base.css:34-48 — `th` now uses `color: var(--muted)` (#63626d) at 12px on its own #fbfafc, not #878590 at 9px; index.css:50-51 darkened --muted/--muted-2 to #63626d/#6f6e7a. Test: src/test/contrast.test.ts:144 'holds the table header above the floor on its own background' computes the ratio and requires >= 4.5:1, and :121 'would fail the palette the auditors measured' is the falsifier — it asserts the OLD #878590/#fbfafc pair measures u… |
| A12 | L | Missing-value placeholder is a bare em-dash at 9px with no accessible meaning | closed | DashboardPage renders `<NoValue label="No FQDN recorded" />` instead of a bare em-dash; NoValue hides the dash from assistive tech and carries the sentence. Covered by components/attributes.test.tsx 'hides the dash from assistive tech and names the missing field'. |

### /devices — Devices & Inventory

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A13 | H | Dispatching a signed job gives no confirmation of any kind — the form just disappears | closed | frontend/src/modules/jobs/DispatchJobDialog.tsx:84-91 — the mutation's onSuccess invalidates ["jobs"] and deliberately does NOT call onClose(); FormActions renders savedMessage `Job dispatched — <type> is <status>.` (DispatchJobDialog.tsx:193-197) inside role="status". Test that would fail if it returned: DispatchJobDialog.test.tsx:222 "confirms the dispatch instead of vanishing" (asserts findByRole("status") matches /dispatched/i, onClose not called, dia… |
| A14 | H | Job dispatch accepts an empty Message with no validation — server returns 201 | closed | The Message field is gone rather than validated: DispatchJobDialog.tsx:83 dispatches `params: {}` and neither production job type carries params (jobTypes.ts:52-66). The one remaining required field is validated client-side — requiredProblems(TYPE_RULES) at DispatchJobDialog.tsx:108-111 blocks submit and renders the problem. Tests: DispatchJobDialog.test.tsx:249 "sends the chosen type and the row's device id, with no params" (asserts the exact body {type,… |
| A15 | H | Jobs can be dispatched to an offline agent with no warning | closed | DispatchJobDialog.tsx:163-170 renders a role="status" gold notice naming the last-seen instant when agent_status === "offline", and DispatchJobDialog.tsx:114-122 repeats it in the confirmation body. Dispatch stays enabled (an offline agent is a legitimate queue target); what is closed is dispatching blind. Tests: DispatchJobDialog.test.tsx:177 "warns that an offline agent will not run the job now", :194 "repeats the offline state in the confirmation", :20… |
| A16 | H | Production job-type dropdown exposes a developer test job called "hello", pre-filled with "ping" | closed | jobTypes.ts:52-66 — DISPATCHABLE_JOB_TYPES is {inventory.collect, logs.ship}, each with a human label and a description of duration/disruption; `hello` is absent. DispatchJobDialog.tsx:62 defaults `type` to "" and the select's first option is "Select a job type…" (:137). Tests: jobTypes.test.ts:27 "does not offer the developer test job", :32 "offers only what the kernel dispatch route will actually accept", :41 "gives every option a human name and a descr… |
| A17 | M | The Run job panel is not a `<form>`, yet its button is `type="submit"` — Enter does not submit | closed | DispatchJobDialog.tsx:131 wraps the controls in <Form onSubmit={requestDispatch}>, and Cancel carries an explicit type="button" (:176) so it cannot submit. Test: DispatchJobDialog.test.tsx:262 "puts its controls in a real form, so submitting it is the dispatch path" — asserts container.querySelector("form") is non-null, that it owns a button[type=submit], and that fireEvent.submit reaches the confirmation. :286 (now :307 after this run's insert) "does not… |
| A18 | M | The Run job panel is visually unstyled and escapes the card it belongs to | closed | The panel is now components/ui.tsx#Dialog (DispatchJobDialog.tsx:126-130): overlay + .modal/.modal-head/.modal-body, role="dialog" aria-modal="true", and the title is a real <h2 class="modal-title">. Test WRITTEN THIS RUN: DispatchJobDialog.test.tsx:286 "is a bounded modal task with a real heading, not loose controls on the page" — asserts role=dialog + aria-modal, getByRole("heading", {name:"Run job on ws-alpha"}), and that the form and the Job type cont… |
| A19 | M | Filter tabs do not change the URL — filtered views cannot be linked, bookmarked or restored | closed | DevicesPage.tsx:113-134 — tab and search read from useSearchParams (`state`, `q`); setTab pushes, setSearch replaces so one history entry per keystroke does not break Back. Tests: DevicesPage.test.tsx:451 "puts the chosen tab in the URL", :459 "restores a tab and a search term from the URL on load", :467 "drops the parameter rather than carrying an empty filter". |
| A20 | M | Tabs are missing `aria-controls`/`tabpanel` wiring and roving tabindex | closed | Roving tabindex + Left/Right/Home/End were already in shell/components.tsx:164-205 (test shell/tabs.test.tsx:45 "is one tab stop, not one per tab" asserts ["0","-1","-1"]). The missing half — aria-controls and a role=tabpanel — was still absent on /devices and /devices/:id; FIXED THIS RUN: DevicesPage.tsx:286 passes panelId="devices-tabpanel" and :291-295 wraps the table in a role="tabpanel" div; DeviceDetailPage.tsx:348 + :352-356 do the same with "devic… |
| A21 | M | Tab counts are absolute totals and contradict the visible result set when a search is active | closed | DevicesPage.tsx:158-176 computes `searched` (search applied, tab not) and :198-208 derives every tab count from it, so the badge and the body always agree. Test: DevicesPage.test.tsx:188 "counts the tabs against the active search, not the whole fleet". |
| A22 | M | No-results empty state offers no way to clear the search | closed | DevicesPage.tsx:318 renders a "Clear search" button in the FilterBar, which sits OUTSIDE QueryState (:322) and therefore stays on screen in the no-results state; the empty label quotes the term via shortTerm() (:331-334). Tests: DevicesPage.test.tsx:216 "offers a way out of a search, and only when there is one to take", :228 "does not paste 500 characters of search term into the empty state", :467 (clicking it clears the URL param). No × glyph inside the… |
| A23 | L | Search input has no maxlength and accepts 500+ characters | closed | FIXED THIS RUN. DevicesPage.tsx:305 adds maxLength={128} to the aria-label="Search devices" input; the same bound applied to JobsPage.tsx:149 and DeviceDetailPage.tsx's software filter. Test written this run: DevicesPage.test.tsx:555 "bounds the search box rather than accepting a pasted document" (and JobsPage.test.tsx:318 for the job console). Falsification: deleting the attribute turned both red. |
| A24 | L | Table header cells omit `scope="col"` | closed | FIXED THIS RUN for the /devices table (DevicesPage.tsx:342-348, all seven headers scope="col") and for all four tables on /devices/:id (DeviceDetailPage.tsx:393-395, 476-479, 516-520, 574-578). Tests written this run: DevicesPage.test.tsx:491 "declares every column header as a column header" (asserts 7 headers and scope=col on each), DeviceDetailPage.test.tsx:619 "marks every column header as a column header". Falsification: stripping scope turned both re… |
| A25 | L | "ACTIONS" column header is announced but the actions have no per-row context | closed | FIXED THIS RUN. DevicesPage.tsx:384-393 replaces the shared Btn (which takes no aria-label and is used by ~30 screens) with a native button carrying aria-label={`Run job on ${assetLabel(device)}`}; visible text unchanged. Same treatment on the device page's Jobs tab: DeviceDetailPage.tsx:625 gives each "Inspect" link an aria-label naming the job type and issue time. Tests written this run: DevicesPage.test.tsx:501 "names the device each row action belongs… |
| A26 | L | Page has a single `<h1>` and no section headings | closed | FIXED THIS RUN within my files: DevicesPage.tsx:274-275 add sr-only h2 "Fleet summary" and "Device list"; DeviceDetailPage.tsx:255 "Summary" and :322 "Device details"; JobsPage.tsx:139 "Job queue" and :231 "Job detail". sr-only because the design gives these blocks no visible title — what was missing is the structure, not pixels. Tests written this run: DevicesPage.test.tsx:546 and JobsPage.test.tsx:326 "gives the page headings below the H1 to navigate by… |

### /devices/:id — Device detail (Toeshee-sentinel)

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A27 | H | Raw JSON blobs are rendered directly into the UI as table content | closed | WAS NOT CLOSED BEFORE THIS RUN. The previous fix (a hand-rolled describeAttributes join) fixed flat scalar cells but left BOTH of the audit's own examples intact: it printed `size_bytes 256022937600` unformatted and fell back to JSON.stringify for a NIC's addresses[] — the 225-character cell the finding quotes. FIXED THIS RUN: DeviceDetailPage.tsx:434-437 renders <Attributes value={component.attributes} empty="No attributes reported for this component."/>… |
| A28 | H | Device detail page never shows whether the agent is online or offline | closed | DeviceDetailPage.tsx:238-246 renders a StatusPill of device.agent_status (word + colour, not colour alone) beside the H1 with "Agent last seen <formatDateTime>", or "No agent enrolled" when agent_status is null. Test: DeviceDetailPage.test.tsx:318 "shows whether the agent is answering, and when it last did". |
| A29 | M | Hardware inventory is 91% network interfaces, 20 of which are Docker veth pairs | closed | FIXED THIS RUN. New frontend/src/modules/inventory/hardware.ts classifies veth*/docker*/br-*/virbr*/vnet*/tap*/cni* by attributes.name (the agent puts the MAC in `identifier`, so matching the identifier would have classified nothing on Linux — agent/internal/capabilities/inventory/nic.go#nicComponentFrom), and orders NICs holding a routable address above those carrying only fe80::. DeviceDetailPage.tsx:178-182 splits the rows; :373-383 renders a "Show vir… |
| A30 | M | Breadcrumb "ITMS / Fleet" is not navigable — there is no link back to the device list | closed | The breadcrumb is now `<nav aria-label="Breadcrumb">` with a real home link and `aria-current="page"` on the last segment; the section links to its group's first reachable route via the new optional `sectionTo`, and renders as plain text when no destination is supplied — a crumb that links nowhere is the false affordance the finding is about. DeviceDetailPage also gained an explicit 'All devices' link. Tests: four in AppShell.test.tsx, falsified. |
| A31 | M | "Serial" stat card shows a bare em-dash given equal visual weight to real data | closed | FIXED THIS RUN. DeviceDetailPage.tsx:296-306: when summary.serial is absent the card reads "Not reported" and carries a foot line "The agent did not report a serial number for this endpoint." — words an asset manager can act on rather than a dash in stat type. (StatCard takes a string value, so the remedy is the wording plus the explanatory foot, not a type-size change in a shared component I may not edit.) Test written this run: DeviceDetailPage.test.tsx… |
| A32 | L | "Last sweep" puts a full timestamp into a numeric stat-card slot, where it wraps to two lines | closed | FIXED THIS RUN. DeviceDetailPage.tsx:312-317: the Last sweep card's value is now describeAge(snapshot_taken_at, now) — "25h ago" — and the absolute instant moves to the card's foot, still through lib/datetime in the operator's profile zone. Tests written/updated this run: DeviceDetailPage.test.tsx:92 (clock frozen; asserts stat values end in "3h ago" and the foot carries "16 Aug 2026, 09:00") and :186 "renders the last sweep in the profile zone, not the b… |
| A33 | H | Queued jobs cannot be cancelled, inspected, or retried — the Jobs tab is read-only text | **open** | The INSPECT half is closed: DeviceDetailPage.tsx:618-630 gives every job row a Link to /jobs?job=<id> with a row-naming aria-label, and the Waiting column badges a stuck job (:600-611); tests DeviceDetailPage.test.tsx:349 and :114. CANCEL AND RETRY ARE STILL MISSING and cannot be built from the console. Backend route search in /home/user/Toeshee-RMS/backend: `grep -n '^@router\.' app/api/v1/jobs.py` returns exactly three routes — POST "" (jobs.py:105), GE… |
| A34 | H | The same device reports two different "last contact" times in two different date formats | closed | Format divergence gone: the health panel's ISO-ish renderer was replaced by lib/datetime — modules/health/DeviceHealthPanel.tsx:83-85 calls formatDateTime(device.agent_last_seen_at, zone), covered by modules/health/__tests__/health.test.tsx:1209 "renders a device's last contact and last report in the operator's zone" (pins both the zone and the one product format, so a UTC renderer fails it). Value divergence is now labelled rather than left ambiguous: De… |
| A35 | M | Patch tab exposes the raw enum value `not_applicable` as a user-facing badge label | closed | modules/patch/patchState.ts:54 patchStateLabel maps the enum, applied at modules/patch/DevicePatchTab.tsx:29. Tests that would fail if the raw value returned: modules/patch/__tests__/patch.test.tsx:784 (patchStateLabel("not_applicable") === "Not applicable") and :821 (findByText("Not applicable") rendered in the By-state card). Outside my editable set; verified read-only. |
| A36 | M | "Reboot" card occupies a full third of the Patch tab to display one badge | **open** | Half fixed, half present. The wording half is closed — modules/patch/DevicePatchTab.tsx:18-21 renders "No reboot pending"/"Reboot pending" instead of "Up to date", tested at modules/patch/__tests__/patch.test.tsx:865. The LAYOUT half is untouched: DevicePatchTab.tsx:11 is still `grid grid-cols-1 gap-4 p-4 md:grid-cols-3` with the one-badge Reboot card as an equal third beside two five-row cards, so it still spends a third of the tab on ~130px of white spa… |
| A37 | M | Empty states are implemented three different ways across the tabs of a single page | closed | DeviceDeployTab no longer keeps a header row with a stub row beneath it — the pattern the finding's own fix note forbids. It returns the shared EmptyState, the same one the other tabs reach through QueryState. |
| A38 | L | "Last good point: none" mixes a lowercase word into a column of em-dashes | closed | modules/backup/DeviceBackupTab.tsx:108 renders formatDateTime(data?.last_good_point?.taken_at, zone), which returns lib/datetime#NO_DATE ("—") when absent — the same token as its three neighbours; the literal "none" is gone (grep for "none" in that file returns nothing). Test that would fail if it returned: modules/backup/__tests__/backup.test.tsx:1138 "renders an absent date as an em dash, never \"Invalid Date\"" sets last_run_finished_at and last_good_p… |

### /assistant + /assistant/settings — AI Admin Copilot

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A39 | H | "Save" on Assistant settings does nothing and says nothing — no request, no validation message, no feedback | closed | frontend/src/modules/ai-admin/AiSettingsPage.tsx:92 `save()` runs `requiredProblems(ENABLING_RULES, …)` and passes the result to FormActions at :195, which renders one role="alert" summary and a role="status" "Assistant settings saved." on a 2xx; the Save button is never disabled for missing input. Tests: aiAdmin.test.tsx "stays enabled without the acknowledgement and says so on submit", "names the provider when an enabling change has none selected", "ann… |
| A40 | H | Model provider dropdown is empty — the assistant cannot be enabled and the UI never says why | closed | frontend/src/modules/ai-admin/providerOptions.ts `providerPickerOptions` returns [] rather than the single dead "None selected" option when no provider is configured, and AiSettingsPage.tsx:143 renders EntityPicker with PROVIDER_PICKER_EMPTY_HINT — repeated outside the list at :152 so it is visible without opening the dropdown. The hint names ITS_AI_PROVIDERS / ITS_AI_<KEY>_* and the restart, and states the consequence ("the assistant cannot be enabled").… |
| A41 | H | All three form controls on Assistant settings are programmatically unlabeled | closed | All three controls carry accessible names: AiSettingsPage.tsx:168/:174 (id="ai-ack" ↔ htmlFor), :181/:186 (id="ai-enabled" ↔ htmlFor), and the provider control is EntityPicker (components/picker.tsx) whose input is labelled "Model provider". FIXED THIS RUN: the finding's second half — aria-describedby="ai-egress-disclosure" added at AiSettingsPage.tsx:170 pointing at the disclosure <section id="ai-egress-disclosure">, so the egress list is announced with… |
| A42 | M | The feature is called three different things in three adjacent places | closed | shell/nav.ts's single label — which is both the sidebar item and the top-bar title — changed from 'AI Admin Copilot' to 'Assistant', matching the H1, the body copy and the URL. The duplicated H2 was removed earlier in this cycle. |
| A43 | M | Disabled-feature message is lowercase, unpunctuated, and offers no path forward for the operator | closed | FIXED THIS RUN. frontend/src/modules/ai-admin/ChatPanel.tsx:48 `sentence()` capitalises the first letter and closes the sentence — typography only, the server's words are untouched — and :180 renders the disabled-feature reason through it, inside a `notice` block with a warning Icon at :172. The action was already promoted to a real button ("Open assistant settings", :185) and non-admins are told who can act. Tests added: aiAdmin.test.tsx "renders the ser… |
| A44 | L | Untested: enabling the assistant | n/a | The auditor recorded a gap in their own coverage, not a product defect: "Why it's a problem: N/A — recorded for coverage completeness. Fix: N/A." They declined to tick the data-egress acknowledgement, which was the right call against a live org. The success path they asked the owner to verify is now covered by automated tests rather than by a live change: aiAdmin.test.tsx "shows the egress disclosure verbatim and posts the acknowledgement" asserts PUT /ai… |

### Authentication / session (root cause confirmed)

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A45 | B | Confirmed root cause of the broken sign-out: Keycloak returns HTTP 400 to the logout call because `post_logou… | closed | Same defect as A3, confirmed at the configuration level. deploy/keycloak/realm-it-suite.json:36-39 — it-suite-web's attributes carry `post.logout.redirect.uris: "/"`, replacing the `+` shorthand that resolved to the callback-only redirectUris set and refused every sign-out with HTTP 400. Test that would fail if it returned: backend/tests/test_console_auth_realm.py::test_the_post_logout_uri_the_browser_asks_for_is_one_the_realm_accepts — it explicitly asse… |
| A46 | H | A failed SSO round trip returns the user to /login with no error message — an invisible auth failure loop | closed | frontend/src/App.tsx:153 — the callback failure now renders `role="alert"` carrying 'Sign-in failed: <reason>' rather than a silent paragraph, and App.tsx:250 does the same for the unreachable-IdP card. Both screens are the end of a browser-performed redirect, so nothing is focused and nothing was clicked — an unannounced paragraph is the same silence the auditor measured. Tests: src/shell/authSurface.test.tsx 'announces the callback failure, rather than… |
| A47 | M | Access/ID token lifetime is 5 minutes, and the app drops to /login rather than recovering | closed | The two behaviours the finding names are handled. Proactive renewal: frontend/src/lib/auth.ts:67 automaticSilentRenew. Unrecoverable failure no longer hard-redirects — auth.ts:127-131 announces on addSilentRenewError (deliberately NOT on addAccessTokenExpiring, which fires before every successful renew), and frontend/src/shell/AppShell.tsx:212-220 renders a non-blocking role="status" banner with a 'Sign in again' action over the current page. Route is pre… |

### /health — Health & Alerts

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A48 | H | Server validation errors are dumped into the UI as raw FastAPI/Pydantic JSON | closed | Behaviour: HealthSettingsPanel.tsx:220 wraps the fields in <Form> and renders failures through FormActions (components/form.tsx#describeSubmitError), which translates FastAPI's {"detail":[{type,loc,msg,input,ctx}]} into a sentence, never reads `input`, and announces it in role="alert". No test pinned it AT THIS CALL SITE before (the existing 422 test used a plain-text body and would have passed against a raw `error.message` renderer), so I added one: heal… |
| A49 | H | `min`/`max` constraints on all eight settings fields are decorative — the panel is not a `<form>`, so nothing… | closed | HealthSettingsPanel.tsx:220 is a real <form> (components/form.tsx#Form) and every field carries a rule (SETTING_RULES, HealthSettingsPanel.tsx:79-92) checked on submit against SETTING_BOUNDS, so the bound is enforced client-side instead of hanging as a decorative min/max. Test: health.test.tsx:1019 "enforces the server's bound instead of hanging a decorative max on the input" — 5000 into Alert retention produces "must be between 90 and 3650" and updateHea… |
| A50 | H | Typing letters into a numeric field silently erases it, and the empty result passes validation | closed | Fields are type="text" inputMode="numeric" (HealthSettingsPanel.tsx:230-240) so "abc" is kept rather than silently erased, and the rule refuses it. Test: health.test.tsx:1041 "keeps letters typed into a numeric field and names them" — asserts the field still holds "abc" and the alert reads "Alert retention (days) must be a whole number". |
| A51 | M | Seven settings are in seconds and one is in days, with no unit shown in or beside any input | closed | Was genuinely open: eight bare numbers, no unit anywhere. Fixed at HealthSettingsPanel.tsx:40-110 (each field now carries name+unit, label renders "Offline after (seconds)" / "Flap threshold (opens per window)" / "Alert retention (days)"), :254 (the bound is quoted in the field's own unit and the stored value is translated), and :293/:300 (describeStored/formatDays gives Alert retention the derived hint its siblings had: "400 days (about 1.1 years)"). The… |
| A52 | M | A failed save silently reverts every field, so the user cannot correct the value the error complains about | closed | setEdits({}) runs only in the mutation's onSuccess (HealthSettingsPanel.tsx:140), so a 422 leaves every entered value on screen. Test: health.test.tsx:1064 "keeps every edit after a failed save, so the rejected value can be corrected" — after a rejected save the field still holds "3000" and the alert is displayed. |
| A53 | M | "Create profile" with an empty name is a silent no-op | closed | ProfileList.tsx:113-121 validates through requiredProblems and ProfileList.tsx:266 keeps Create enabled so the press produces the reason; FormActions renders it in role="alert". Test: health.test.tsx:942 "names the empty field rather than leaving Create inert" — asserts the button is enabled, the alert reads "New profile name is required", and createHealthProfile is not called. (Focus is not moved to the field; the auditor's defect — the silent no-op — is… |
| A54 | M | Profile names accept arbitrary markup-like strings with no format validation | closed | PROFILE_RULES at ProfileList.tsx:52-62 rejects < or > and names over 120 characters before the round trip; duplicates come back as the server's 409 translated to "That conflicts with something that already exists". Test: health.test.tsx:959 "refuses a markup-like profile name and says why" — "<b>Fleet</b>" produces "Profile name cannot contain < or >" and no POST. |
| A55 | L | The error message pushes the Save button into a two-line wrap | **open** | Behaviour fixed, untested. The error is no longer a long JSON string, the button label at this call site is the single word "Save" (HealthSettingsPanel.tsx:271, so it cannot wrap mid-label), and .form-actions is display:flex;flex-wrap:wrap (shell/shell.css:398), which sends an over-long message to its own row rather than shrinking the button below its min-content width. What is missing is a test: jsdom performs no layout, so nothing I can write in src/mod… |
| A56 | B | A failed destructive API call (HTTP 503) is silently swallowed — the UI shows success and the user is never t… | closed | ProfileList.tsx:303-309 renders every row-action mutation error through describeSubmitError in a labelled role="alert", and describeSubmitError (components/form.tsx) is driven off status first so a bodyless 503 still yields a sentence; the row is never optimistically removed. Test: health.test.tsx:904 "tells the operator a delete failed when the server refused with a bodyless 503" — asserts "Delete failed", "unavailable right now" and "Nothing was changed… |
| A57 | H | Delete destroys a record instantly with no confirmation, and is styled identically to the non-destructive act… | closed | Confirmation: ProfileList.tsx:284 ConfirmDialog naming the profile and its blast radius (describeDeleteImpact), pinned by health.test.tsx:904, which can only reach the failure by clicking through the "Delete profile" confirm button — remove the dialog and that query fails. Styling half was unpinned, so I added health.test.tsx:927 "does not style Delete like the Clone button beside it": Delete carries .danger (ProfileList.tsx:228) and Checks/Retire/Clone d… |

### /security — Security Logs

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A58 | H | "Export evidence CSV" ignores the active username and event-type filters — the export does not match what is… | **open** | STILL PRESENT and cannot be closed in the frontend. The CSV href is built from `fromTs` only (src/modules/security/SecurityPage.tsx:160) because the route accepts nothing else: `access_evidence_csv` in backend/app/modules/security_logs/api.py:135-141 declares exactly `from_ts` and `to_ts` — no `username`, no `event_type` (searched `grep -rn "access.csv" backend --include=*.py`, and the sibling `GET /security-events` at api.py:88-98 is the only route that… |
| A59 | H | The 24h/7d/30d time-range control conveys its selection by colour alone and exposes no state to assistive tec… | closed | src/modules/security/SecurityPage.tsx:289-317 — `role="radiogroup" aria-label="Time range"` wrapper, each option `role="radio" aria-checked={active}`, roving `tabIndex` (0 on the checked one, -1 on the rest), arrow/Home/End handling in `moveRange` (SecurityPage.tsx:172-186), and a non-colour cue (an aria-hidden ✓ on the selected option, SecurityPage.tsx:308). Tests: SecurityPageEvidence.test.tsx "is a labelled radio group whose checked option is the one o… |
| A60 | M | The module's own labelling contradicts itself: eyebrow says "GOVERNANCE EVIDENCE", breadcrumb and sidebar say… | closed | src/modules/security/SecurityPage.tsx:210-211 — eyebrow is now "Fleet" (the nav GROUP this page sits in, src/shell/nav.ts:53-64, which is also what the breadcrumb renders from, src/shell/AppShell.tsx:176-181) and the H1 is "Security Logs", character-for-character the nav label and top-bar title. shell/nav.ts is outside my file list and every other page in the console already sets eyebrow == nav group, so the page is the half that moved. Test: SecurityPage… |
| A61 | M | Stat cards are qualified with the implementation term "(loaded)" | closed | src/modules/security/SecurityPage.tsx:240-259 — the "(loaded)" parenthetical is gone; all three cards carry a `foot` in one consistent style: "the last 7 days" on the server-counted total, and "Across the N events loaded" (SecurityPage.tsx:170) on the two derived from the page in hand. Tests: SecurityPageEvidence.test.tsx "never qualifies a number with the implementation word 'loaded'" and "states in words that two of the three are computed over the page… |
| A62 | M | Filter state is invisible to the URL, so an evidence view cannot be shared or reproduced | closed | src/modules/security/SecurityPage.tsx:75-100 — username, event_type and hours are read from and written to the query string via `useSearchParams` (replace-mode so typing does not flood history); an unrecognised `?hours=` falls back to the default rather than inventing a window. A "Copy link to this view" action sits beside Export (SecurityPage.tsx:218-220, 188-198). Tests: SecurityPageEvidence.test.tsx "hydrates all three filters from the query string on… |
| A63 | M | Empty state gives no way to widen the search that produced it | closed | src/modules/security/SecurityPage.tsx:325-375 — QueryState keeps loading/error, and the empty case is a real `EmptyState` (components/emptystate.tsx) with two branches: filtered emptiness names the active username and event type and the window in words and offers "Widen to 30d" + "Clear filters"; unfiltered emptiness says the collector may not be enabled, which is the security-context distinction the auditor asked for. Tests: SecurityPageEvidence.test.tsx… |
| A64 | L | Untested: the "Raw event" detail panel | closed | The auditor's suspicion was correct — the panel WAS a `<pre>{JSON.stringify(detail.data.raw, null, 2)}</pre>`, i.e. the same defect as /devices/:id. Now src/modules/security/SecurityPage.tsx:436-439 renders `<Attributes>` (components/attributes.tsx), which labels each stored field, humanises enum-shaped values, and keeps the object behind a closed "View raw" disclosure — exactly the auditor's prescribed shape. Tests: SecurityPageEvidence.test.tsx "labels… |

### /patch — Patch Management

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A65 | H | "Scan now" queues a fleet scan with zero feedback — and this is demonstrably what produced the duplicate jobs… | closed | Handled at frontend/src/modules/patch/FleetPatchPage.tsx:226-235 — the Scan control is a real <Form> whose <FormActions> renders savedMessage={scanQueuedMessage(scan.data)} in a role="status" (form.tsx:284-288) and disables the submit button while pending (form.tsx:246). scanQueuedMessage is at FleetPatchPage.tsx:46-52 and reports the dispatched job count, zero included. Tests: patch.test.tsx "says what the scan dispatched instead of returning silently to… |
| A66 | M | A `<button>` is nested inside an `<a>` — invalid HTML and an ambiguous control | closed | frontend/src/modules/patch/FleetPatchPage.tsx:210-212 renders one element: <Link className="btn" to="/patch/policies">Policies</Link>, no nested <button>. Test: patch.test.tsx:825 "renders Policies as a single link, not a button nested in an anchor" — asserts the link's href, that link.querySelector('button') is null, and that no button named Policies exists. Falsified by restoring <Link><button className="btn">Policies</button></Link>: the test went red. |
| A67 | M | Two chart panels use a device/monitor icon and identical copy for their empty state | closed | Fixed this run. Both chart cards now render their own <EmptyState> (frontend/src/modules/patch/FleetPatchPage.tsx:310-318 and :340-348) instead of QueryState's empty branch, whose emptyIcon DEFAULTS to "devices" (shell/components.tsx:290) — the monitor glyph the audit saw. Icons are now chart and activity; titles are "No compliance percentages by OS family" / "...by severity"; bodies name the cause (no completed scan) and the way out (Scan at the top of t… |
| A68 | L | Four stat cards all read 0 with no indication that the module has never run | closed | Fixed this run. frontend/src/modules/patch/FleetPatchPage.tsx:160-170 derives neverScanned (both queries resolved, both compliance maps empty, no exposures, catalog total 0) and all four StatCards at :251-279 then render "—" with foot "Never scanned" instead of 0. Inferred rather than read, because no route serves a last-scan time (FleetReportOut, backend/app/modules/patch/api.py:141, has no timestamp) — the comment states exactly what the cards are entit… |

### /patch/policies — Patch policies

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A69 | B | "New policy" is completely non-functional — patch policies cannot be created at all | closed | New policy (frontend/src/modules/patch/PatchPolicyEditor.tsx:182) calls startEdit(null), which opens the <Dialog> at PatchPolicyEditor.tsx:246 containing a real <Form> (:250) with seven controls and a Save that calls upsertPatchPolicy. Tests: patch.test.tsx "opens a dialog with form controls in it" (line 555 — replays the audit's own probes: a role=dialog exists and it has labelled inputs), "creates a policy from the dialog, which was unreachable before"… |

### Session handling (second, cleaner observation)

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A70 | H | The session drops mid-task during continuous active use, with no warning, no expiry notice, and total loss of… | closed | Same mechanism as A47, and the finding's five asks map one-to-one: (1)(2) auth.ts:67 + auth.ts:127-131 announce instead of redirecting; (3) AppShell.tsx:212 banner offers 'Sign in again' rather than seizing the page; (4) auth.ts:176-180 records the exact route into OIDC state and App.tsx:141 restores it through safeReturnTo. There is no /login route to be dumped on any more (App.tsx:224-246). Tests that would fail if it returned: src/shell/AppShell.test.t… |

### /deploy — Software Deployment

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A71 | B | Deployments cannot be created — the Package dropdown is empty and Create silently does nothing | closed | frontend/src/modules/deploy/DeployPage.tsx:334-341 replaces the one-option <select> with <EntityPicker> carrying emptyHint={PACKAGE_PICKER_EMPTY_HINT} (pickerOptions.ts:43), and :343-350 renders a Link to /deploy/packages when the catalog is empty. Create is never a silent no-op: submit() at :286-300 builds problems from CREATE_RULES and FormActions announces them in a role="alert". Tests: deploy.test.tsx "says what would fill an empty catalog instead of… |
| A72 | H | The deployment modal has no Escape handler — Escape does not close it | closed | Fixed this run. New hook frontend/src/modules/deploy/modalChrome.ts:33-70 binds a document keydown handler while the dialog is open; wired at frontend/src/modules/deploy/DeployPage.tsx:323 (useModalChrome(open, dismiss) — dismiss, not onClose, so a keyboard exit also clears a stale 503). Applied to the module's other dialogs too: PackagesPage.tsx:222, ScriptsPage.tsx:152, :264, :377 (the last held off while its SYSTEM-run confirmation is stacked, since Co… |
| A73 | H | "Target ID" requires the operator to type a raw device UUID by hand — no picker, no search, no validation | closed | frontend/src/modules/deploy/DeployPage.tsx:383-391 renders an <EntityPicker> labelled "Target" over devicePickerOptions (pickerOptions.ts:60-75: hostname as the label, "os_family · agent_status" as the sublabel), resolving to the id internally; the free-text "Target ID" is gone. Tests: deploy.test.tsx "shows the agent state beside every device, so an offline one is visible first" (line 869), "submits the id behind the hostname the operator chose", "refuse… |
| A74 | M | Maintenance window is a raw cron expression with no timezone stated | closed | The zone is stated in the field's own label: frontend/src/modules/deploy/DeployPage.tsx:416 renders `Window (cron, ${CRON_ZONE_NOTE}, optional)` where CRON_ZONE_NOTE = "UTC" (deploy/cron.ts:44), matching backend/app/core/schedule.py's window_open being called on datetime.now(UTC); the confirmation repeats it (DeployPage.tsx:244). Validation exists and is enforced on submit via cronProblem (cron.ts:87-101), and the placeholder is now a value the matcher ac… |
| A75 | M | Modal does not lock background scrolling | closed | Fixed this run. frontend/src/modules/deploy/modalChrome.ts:52-68 sets document.body.style.overflow = "hidden" while the dialog is open, compensates the scrollbar gutter to avoid the layout jump, and restores both in cleanup; wired at DeployPage.tsx:323 and at the module's other dialogs (PackagesPage.tsx:222, ScriptsPage.tsx:152/:264/:377). Tests (new): "locks the page behind it, and gives the scrollbar back on close" (asserts "hidden" while open and "" af… |
| A76 | M | The modal is not a `<form>`, continuing the product-wide pattern | closed | The dialog's contents sit in a real <form>: frontend/src/modules/deploy/DeployPage.tsx:327 <Form onSubmit={submit}> (components/form.tsx:213-232 renders <form noValidate onSubmit>), Create is type="submit" (form.tsx:246) and Cancel is explicitly type="button" (DeployPage.tsx:424). Requiredness is expressed by CREATE_RULES (DeployPage.tsx:46-49) and announced on submit rather than by the native attribute — the form kernel's documented rule that a disabled/… |
| A77 | L | Positives: the dialog is correctly identified to assistive technology | n/a | The entry is headed "Positives: the dialog is correctly identified to assistive technology" and records no defect. Still true in code: components/ui.tsx:130 emits role="dialog" aria-modal="true" aria-label={title} and :140 the close button carries aria-label="Close"; every control in the deploy dialog is label-associated (the tests reach them by getByLabelText / getByRole name). Its closing suggestion has been half-taken this cycle — Escape is now bound (… |
| A78 | L | "Success rate" shows an em-dash while sibling cards show 0 | closed | Fixed this run. frontend/src/modules/deploy/DeployPage.tsx:113-126 computes rateFoot alongside the rate, and :156 renders <StatCard label="Success rate" value={stats.successRate} foot={stats.rateFoot} />: with no terminal runs the card reads "—" over "No completed runs yet", otherwise the percentage over "N of M completed". The sibling counts stay numeric on purpose — a fetched-and-empty list is knowledge, and dashing it out would be the false-unknown the… |

### /remote — Remote Control

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A79 | H | "Reason" is optional on an audited, consent-gated remote session | closed | Reason is a required FieldRule with a non-triviality rule: frontend/src/modules/remote/StartSessionDialog.tsx:36 (MIN_REASON = 6) and :39-48 (START_RULES reason rule), enforced at :126 (requiredProblems) and trimmed on the way out at :105. Tests in StartSessionDialog.test.tsx: "refuses with no device selected and says which fields are at fault", "names only Reason once a device is chosen", "treats a whitespace-only Reason as empty", "refuses a one-charact… |
| A80 | H | A remote session can be started against a device that is offline, and "Unattended" mode is one dropdown click… | closed | Offline half: devicePickerOptions annotates every option `os_family - offline - cannot connect` (frontend/src/modules/remote/pickerOptions.ts:78-93); StartSessionDialog.tsx:93 computes unreachableReason, renders it inline at :192-196 and gates dispatch behind a ConfirmDialog at :147-150 / :288-320. Unattended half: inline warning at :224-231, required vaulted credential at :131-140, destructive confirmation naming the consent consequence at :147-150 and :… |
| A81 | M | Start session with no device selected is a silent no-op | closed | The dialog is a real <form> (components/form.tsx Form) and Start session is never disabled for missing input; the click runs requiredProblems at frontend/src/modules/remote/StartSessionDialog.tsx:126 and FormActions renders one role="alert" summary naming Device and Reason. Tests: "leaves Start session enabled with no device selected", "refuses with no device selected and says which fields are at fault", "submits on Enter from the Reason field". VERIFIED… |
| A82 | M | "Credential (optional)" offers only "None" — the credential vault is empty and unexplained | closed | The lone "None" option is gone: the field is an EntityPicker whose empty hint names the surface that fills it (frontend/src/modules/remote/pickerOptions.ts:48-49 CREDENTIAL_PICKER_EMPTY_HINT) plus a link to /credentials at StartSessionDialog.tsx:246-252, and a "Use no credential" reset at :253-266. Tests: "says what would fill an empty vault, and links to the screen that does it" (StartSessionDialog.test.tsx), plus pickerOptions.test.ts "name the console… |
| A83 | L | Four stat cards read 0 with no distinction between "never used" and "none right now" | closed | Each stat card carries a timeframe qualifier: frontend/src/modules/remote/SessionsPage.tsx:99-124 - "Fleet, right now" (:103), "All time" (:109), "Not yet ended, in this list" (:115), "Sessions in this list with a recording" (:121). Test: SessionsPage.test.tsx "says which timeframe each stat card covers". VERIFIED FALSIFIABLE: deleting the four `foot` props turned it red; restored. |

### /service-desk — Service Desk

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A84 | H | Creating a ticket is silently blocked by an undeclared required field ("Requester ID") | closed | frontend/src/modules/service-desk/TicketListPage.tsx:463-465 — submit() runs requiredProblems(TICKET_RULES, {categoryId, subject, requesterId}), sets problems and returns without calling the mutation; FormActions (TicketListPage.tsx:600) renders them in a role="alert" (components/form.tsx:293), and Save is never disabled for missing input. Requester also defaults to the signed-in user (TicketListPage.tsx:435-449, useEffect on myUserId). Tests: serviceDesk… |
| A85 | H | Ticket "Description" is a single-line `<input type="text">`, not a textarea | closed | frontend/src/modules/service-desk/TicketListPage.tsx:549 — Description is <textarea rows={5}>. Test: serviceDesk.test.tsx:600 "gives Description a multi-line textarea that accepts newlines" asserts description.tagName === "TEXTAREA" and that a typed Enter produces "line one\nline two" in the POST body rather than submitting. |
| A86 | H | Three raw ID fields in one dialog — Requester ID, Assign to, Device ID — all free text with no picker | closed | frontend/src/modules/service-desk/TicketListPage.tsx:560/569/578 — Requester, Assign to and Device are EntityPicker comboboxes over the /users and /devices directories; the list-bar Assignee filter is an EntityPicker too (TicketListPage.tsx:278, placeholder "Anyone", not "User ID"). Tests: serviceDesk.test.tsx:493 "offers no free-text id field for requester, assignee or device" (asserts the "Requester ID"/"Device ID (optional)" labels are gone and all thr… |
| A87 | M | The "Status" filter is a free-text box while every other filter beside it is a dropdown | closed | frontend/src/modules/service-desk/TicketListPage.tsx:247-259 — Status is a <select> whose options come from ALL_STATUSES (serviceDeskState.ts:214), the union of lifecycle.py's INCIDENT_STATUSES/REQUEST_STATUSES, labelled via ticketStatusLabel. Test: serviceDesk.test.tsx:773 "filters by status from a dropdown of the real vocabulary, not a text box" asserts tagName === "SELECT", that both type vocabularies are reachable, and that selecting one sends status=… |
| A88 | M | Enum values shown raw and lowercase throughout the module | closed | Filters were already labelled (TicketListPage.tsx:234-297; test serviceDesk.test.tsx:796) but every ROW still printed wire values. Fixed this run: TicketListPage.tsx:372-383 (row Type/Status/Priority via ticketTypeLabel/ticketStatusLabel/ticketPriorityLabel), MyWorkPage.tsx:87-101, TicketDetailPage.tsx:99-104 (header badges), :112 (Type), :661-663 + :700-701 (comment visibility via humaniseEnum), :859-863 (status-history From/To), ServiceDeskAdminPage.tsx… |
| A89 | L | "Show all tickets" is an unexplained checkbox that silently changes the query | closed | frontend/src/modules/service-desk/TicketListPage.tsx:326 — the checkbox label reads "Showing open tickets assigned to me and unassigned ones — show all tickets", so the default scope is stated rather than inferred. Test: serviceDesk.test.tsx:854 "shows every ticket once 'Show all tickets' is checked" finds the control by that exact accessible name; I verified it goes red when the label is reverted to plain "Show all tickets". |
| A90 | L | Hard-coded `limit=200` with no pagination control | closed | Fixed this run: frontend/src/modules/service-desk/TicketListPage.tsx:189-194 computes `truncated` (any live query returned a full MAX_LIST_LIMIT page) and :329-334 renders a role="status" line "Showing the first 200 tickets — there are more than this. Narrow the filters above to see the rest."; the custom/"Show all" query now also asks for limit=200 (:148) so one page size governs the screen. Tests: serviceDesk.test.tsx:956 "says the list is capped when a… |

### /credentials — Credentials (vault)

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A91 | H | The Secret field sets no `autocomplete`, so browser password managers will treat the vault form as a login | closed | Secret carries autoComplete="new-password", spellCheck=false, data-1p-ignore and data-lpignore (frontend/src/modules/credentials/CredentialsPage.tsx:478-486); the Name field beside it carries autoComplete="off" plus the same manager opt-outs (:455-463); the dialog is a real <form> via components/form.tsx Form (:441). Tests: "tells password managers to keep out of the Secret field", "keeps the Name beside it out of the same heuristics". VERIFIED FALSIFIABL… |
| A92 | H | "Store" with every field empty is a silent no-op | closed | Store is never disabled for missing input; the click runs requiredProblems(CREATE_RULES, {name, secret}) at frontend/src/modules/credentials/CredentialsPage.tsx:408 and FormActions renders a role="alert" naming both fields. Tests: "keeps Store enabled on an empty form and names every missing field", "treats a whitespace-only Name as empty", "submits on Enter from the Name field", "keeps the dialog open and explains a bodyless 503". VERIFIED FALSIFIABLE: r… |
| A93 | M | Credential types are raw snake_case identifiers | closed | frontend/src/modules/credentials/credentialTypes.ts maps each value to a label AND a one-line description of how the secret is used; the select renders labels (CredentialsPage.tsx:465-476), the row badge renders credentialTypeLabel (:198), and the selected type's description renders as a hint (:487-495). Tests: "labels the type on every row rather than printing the enum", "says what the selected type means for how its secret is used", "still submits the w… |
| A94 | L | Strong security copy, no way to act on it | closed | Card relabelled "Types in use" with foot "3 types available" (frontend/src/modules/credentials/CredentialsPage.tsx:159-166), and the Vault policy panel now points at the action on each row with a distinct sentence for the empty vault (:295-300). Tests: "says the types card counts what is stored, and what the vault could hold", "says where to check a credential out once there is one to check out", "says what would make that action exist, when the vault is… |
| A95 | L | Not tested: storing a real secret and the audited checkout flow | n/a | The entry is the auditor's own coverage-honesty note ("Why it's a problem: N/A - recorded for coverage honesty. Entering credentials/secrets is outside what I will do"), not a product defect. For the record, the round trip the auditor left untested now has automated coverage in frontend/src/modules/credentials/CredentialsPage.test.tsx: "reveals a secret only after an audited checkout, and only inside the one-time dialog" (store -> checkout with a reason -… |

### /portal + /portal/new — End-user Portal

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A96 | H | Ticket Description is a single-line `<input>` here too — the defect is systemic to ticket creation | closed | frontend/src/modules/portal/NewTicketPage.tsx:197 — Description is <textarea rows={6}> with a prompting placeholder. Test: portal.test.tsx:367 "gives Description a textarea that keeps the newlines a requester types" asserts tagName === "TEXTAREA" and that the POST body carries "It says:\nconnection refused". |
| A97 | H | Submit with an empty form is a silent no-op — the seventh occurrence | closed | frontend/src/modules/portal/NewTicketPage.tsx:142 wraps the controls in a real <Form onSubmit={submit}>; :116-123 collects requiredProblems for Category/Subject (plus per-file attachment problems) and returns without calling createPortalTicket; FormActions (:243) announces them under role="alert", and Submit is only disabled when the catalog itself is unreachable, with a stated reason. Tests: portal.test.tsx:265 "keeps Submit enabled on an empty form and… |
| A98 | M | The Category list interleaves incident and request categories in one flat alphabetical list, creating near-du… | closed | frontend/src/modules/portal/NewTicketPage.tsx:163-171 renders <optgroup> headings from portalState.groupPortalCategories (portalState.ts:81-92), which buckets by PortalCategoryOut.type into "Report a problem" / "Request something" and keeps unknown types in a trailing group rather than dropping them. Tests: portal.test.tsx:391 "groups the categories into reporting a problem and requesting something" (asserts the optgroup labels and that Hardware/Access re… |
| A99 | M | Priority is exposed to end users as raw lowercase enums, with no guidance | closed | frontend/src/modules/portal/NewTicketPage.tsx:212-216 renders PORTAL_PRIORITY_OPTIONS (portalState.ts:108-113), each option defining the situation ("Urgent — I cannot work at all, or others are blocked too"), with the wire value untouched and the default "normal". Test: portal.test.tsx:424 "defines each priority in words rather than offering raw enum values" asserts no option named "urgent", an option matching /Urgent — I cannot work at all/, and that the… |
| A100 | L | Attachment input accepts any file type, one at a time, with no stated limits | closed | Limits were already stated; the one-file-at-a-time half was fixed this run. frontend/src/modules/portal/NewTicketPage.tsx:224 the input is now `multiple` (label renamed "Attachments (optional)"), :70-76 holds a File[] plus a uploadedCount ref, :106-111 uploads them sequentially and only advances past a file the server accepted (so a retry resumes mid-batch), :118-121 names EVERY oversized file, and portalState.ts:131-138 states "Attach as many files as yo… |
| A101 | L | Positive: the portal form correctly omits the requester and does not ask for any ID | n/a | The entry is headed "Positive:" and its Fix is "N/A". Still true in code: frontend/src/modules/portal/NewTicketPage.tsx has no requester field and no id input of any kind — the POST body (NewTicketPage.tsx:92-100) carries only category_id, subject, description and priority. Pinned by portal.test.tsx:218 "submits no requester_id, type, queue, or assignee field -- the form has none to send". Its recommendation (apply this to the staff dialog) is what A84/A8… |

### /notifications — Notifications

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A102 | H | Webhook channels accept any string as a URL — no validation, and the field is not even `type="url"` | closed | frontend/src/modules/notification/ChannelsPanel.tsx:36-50 (invalidWebhookUrl: new URL() parse, scheme and host checks), wired at :86 as the URL field rule, plus type="url"/inputMode at :472. Tests in __tests__/notification.test.tsx: "refuses a webhook URL that is not a URL, before it can silently drop deliveries", "refuses a URL whose scheme deliveries cannot post to", "types the URL field as a URL". I proved the first two can fail: adding `if (value) ret… |
| A103 | H | Delivery failure is reported as a raw enum code — a fourth, distinct error-presentation style | closed | frontend/src/modules/notification/notificationState.ts:90-129 maps every delivery.py reason code to a sentence with a remedy (scheme_not_allowed -> "Only https:// is permitted ... edit the channel's URL"); rendered in a live region at ChannelsPanel.tsx:359-367 (role="status"). Test: "explains a failed test send instead of printing the backend's enum" — asserts queryByText(/scheme_not_allowed/) is null AND findByRole("status") carries the remedy, so it fai… |
| A104 | H | Notification channels cannot be deleted — only disabled | closed | Edit and Delete row actions at frontend/src/modules/notification/ChannelsPanel.tsx:326-352, confirmation at :411-424 naming the channel and counting the rules that route through it (confirmDeleteBody, :272-281). Tests: "offers Delete on a channel at all", "does NOT delete a channel on the first click, and names it before doing so" (asserts deleteNotificationChannel NOT called on first click), "lets a mistyped webhook URL be corrected instead of abandoned"… |
| A105 | M | A failed delivery test is not recorded in the Deliveries log | closed | Behaviour was already fixed (frontend/src/modules/notification/DeliveriesPanel.tsx:60-64 explains that a test writes no delivery row and says where the result does appear) but had NO test. I added "says a test send is not a delivery, beside the log that does not list it" in __tests__/notification.test.tsx — it fails a Test, then asserts the failure in role=status AND the explanatory note AND "No deliveries yet." together. Proved it can fail by deleting th… |
| A106 | M | "Add channel" modal is the same shared component — same four gaps as /deploy | closed | Closed by the shared Dialog (components/ui.tsx): Escape, initial focus onto the dialog, focus restore, scroll lock. Tests: components/dialog.test.tsx, 8 assertions, falsified. |
| A107 | M | "Bearer token" is a password field with no `autocomplete`, repeating the /credentials defect | closed | frontend/src/modules/notification/ChannelsPanel.tsx:490 autoComplete="new-password" on the Bearer token field (and :528 on the SMTP password). Test: "keeps a password manager away from the bearer token field" asserts the attribute value exactly, so removing or weakening it goes red. |
| A108 | L | Channel kind is lowercase in the dropdown and capitalised in the table | closed | One label map per enum in frontend/src/modules/notification/notificationState.ts:21-23 (channelKindLabel) and :30-39 (deliveryStatusLabel), applied at every render site: table cell ChannelsPanel.tsx:316, select options :444-445, filter options DeliveriesPanel.tsx:78, status badge :114. Tests: "spells a channel kind the same way in the form as in the table" (asserts the option is "Webhook" and that no "webhook" option exists) and "translates a delivery's s… |
| A109 | L | Best heading structure in the audited product, and no datetime fields to corrupt | n/a | Audit entry is a recorded POSITIVE ("Best heading structure in the audited product") plus a negative cross-check for the timezone bug. The structure still holds: h1 from PageHead (frontend/src/shell/components.tsx:43) plus five h2s — ChannelsPanel.tsx:286, RulesPanel.tsx:133, QueueMembersPanel.tsx:82, AllowlistPanel.tsx:44, DeliveriesPanel.tsx:40. The page still renders no date/time/datetime-local input. |

### Responsive behaviour — static analysis

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A110 | H | The application declares `width=device-width` but ships no tablet or phone layout — 2 of 498 CSS rules are re… | closed | The application now authors three breakpoints, not one. frontend/src/shell/shell.css:463 (max-width:1024px — sidebar off-canvas, hamburger, topbar/main padding) and :504 (640px); frontend/src/shell/components.css:328 and :337 fold every grid; frontend/src/index.css:139 drops body min-width from 1120px to 320px, which is what made width=device-width the worst of both worlds. Test: src/test/responsive.test.ts > 'counts more app-authored breakpoints than the… |
| A111 | H | The sidebar is `position: fixed` at a hard-coded 258px and there is no mobile navigation control anywhere in… | closed | frontend/src/shell/AppShell.tsx:163-174 adds a real hamburger (44x44 per shell.css:441-452, aria-expanded, aria-controls="sidebar"), AppShell.tsx:143-146 a focusable scrim button, :100-106 Escape-to-close, :83-90 focus return, :93-95 close-on-navigate. shell.css:463-502 makes the sidebar a drawer with visibility:hidden so its 28 stops leave the tab order, locks body scroll, and retires the 30x30 chevron below 1024px. Tests: src/test/responsive.test.ts 'ta… |
| A112 | H | Data tables cannot scroll within their container — `main` is `overflow-x: visible` and tables are `table-layo… | closed | frontend/src/shell/components.css:158 `.table-wrap { overflow-x: auto }` and, added this run, :190 `.table-wrap > table { min-width: 560px }`. The wrapper alone was half a fix: an auto-layout table never exceeds its box, so it never reaches the wrapper's overflow — it compresses columns instead, which is the other degradation the auditor named. 59 of 60 <table> in src/modules are direct children of a TableWrap/.table-wrap (checked by grep -A1); only modul… |
| A113 | M | 42 interactive elements fall below the 44x44 tap-target minimum, including every sidebar navigation item | closed | frontend/src/shell/shell.css:516-548 — the @media (pointer: coarse) floor now covers .nav-item (the 227x39 rows), .top-icon-btn, .profile-btn, .btn/.btn.small, .tab, and this run added the form controls plus `.collapse-btn { min-width: 44px }` at :544 and `.btn.icon-only { width: 44px }`, because a min-height alone left the 30x30 chevron 30px wide — the axis the auditor called out. Scoped to coarse pointers so desktop density is unchanged. Tests: src/test… |
| A114 | L | Positive: `prefers-reduced-motion` and `prefers-color-scheme` are both honoured | n/a | The entry is headed 'Positive: prefers-reduced-motion and prefers-color-scheme are both honoured' and states 'Why it's a problem: N/A — recorded as a positive'. Verifying it anyway turned up the opposite for one half: grep -rn 'prefers-reduced-motion' over frontend/src returned nothing, so whatever the auditor's stylesheet scan saw came from the framework layer and covered no rule written here — while cycle 10 had since added a drawer that slides in over… |
| A115 | L | Untested: actual rendering at 1280x800, 768x900 and 390x844 | n/a | Not a defect claim — the entry records a coverage gap ('Why it's a problem: N/A — recorded so the gap is not mistaken for a clean result') after resize_window failed to change innerWidth from 1313. Nothing in the product is asserted to be wrong, so there is nothing to close. Stating the residual honestly: every guard I and cycle 10 wrote (src/test/responsive.test.ts, src/shell/chrome-responsive.test.ts) reads the stylesheets as TEXT, because jsdom impleme… |

### /backup — Backup & Restore

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A116 | H | Production page copy tells the user to read the page source to find out what works | closed | frontend/src/modules/backup/BackupPointsPage.tsx:167 — the PageHead description is now "File-level backup points captured from protected endpoints, with per-point verification and scoped restore." The "See the module notes in this page's source…" sentence is gone from the UI; the engineering caveat lives only in the file docstring (lines 1-35). Test: backup.test.tsx:1264 "does not tell the reader to open the page source" — asserts document.body.textConten… |
| A117 | H | "Run now" with an empty Device ID is a silent no-op — the eighth occurrence | closed | frontend/src/modules/backup/BackupPointsPage.tsx:89 (RUN_RULES) and :126 (submitRun calls requiredProblems, sets problems, returns before mutate). The Run button is deliberately enabled when empty (FormActions, line ~207) so the click produces a named refusal instead of nothing. Test: backup.test.tsx:296 "names the unpicked Device on submit instead of disabling Run" — asserts the button is enabled, then that a role=alert reads "Device is required" and api… |
| A118 | H | A fifth hand-typed raw ID field — "Device ID" is the only way to target a backup | closed | frontend/src/modules/backup/BackupPointsPage.tsx:189 — the raw `aria-label="Device ID"` text input is replaced by <EntityPicker label="Device"> fed by devicePickerOptions (pickerOptions.ts), with an emptyHint. Reading side too: table cells at :262-263 resolve device_id/repository_id through nameForOption instead of printing UUIDs. Test: backup.test.tsx:232 "offers devices as a searchable list rather than a raw ID box" — asserts queryByLabelText("Device ID… |
| A119 | M | "Backup success 0%" and "Protected endpoints 0 / 1" present an unprotected fleet as measured fact | closed | Half one: BackupPointsPage.tsx:220-226 renders "Never run" when totalRuns === 0 rather than 0%, covered by backup.test.tsx:1220 "says 'Never run' rather than 0% before any backup has executed" and :1227 "shows the real success rate once runs exist". Half two — the auditor's "surface '0 of 1 endpoints protected' as an alert state" — was implemented at :233 (gold={unprotected > 0}) but had NO test; I added the pair this run: backup.test.tsx:1239 "flags an u… |
| A120 | L | Timezone cross-check: no date/time inputs on this page | n/a | The auditor recorded this as a negative cross-check result, with "Why it's a problem: N/A" and "Fix: N/A". I re-ran the check the auditor could not finish: `grep -rn 'datetime-local\|type="date"\|type="time"' frontend/src/modules/backup frontend/src/modules/provisioning frontend/src/modules/site-servers` returns zero hits, including BackupPolicyEditor.tsx (the Policies tab the auditor did not reach) and RepositoriesPage.tsx. The −5h datetime-local shift is… |

### /email-intake — Email Intake

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A121 | H | `autocomplete="new-password"` is set correctly here but omitted on the two other secret fields in the product | closed | All three named fields carry the hint now: email-intake/MailboxForm.tsx:275 (new-password, the one that was already right), notification/ChannelsPanel.tsx:490 (bearer token) and credentials/CredentialsPage.tsx:482 (the vault Secret). The adjacent Username/Address fields set autoComplete="off" (MailboxForm.tsx:213, :259). Tests: email-intake "keeps a password manager away from the Username field beside the secret" (asserts BOTH Username=off and Password=ne… |
| A122 | M | The Mailboxes table exposes a "PASSWORD" column | closed | Column is "Credentials" (frontend/src/modules/email-intake/MailboxList.tsx:105) rendering "Set"/"Not set" from has_credential (:123) — no password value or mask is ever fetched. The existing test asserted only the CELLS, so it would have passed under a header still reading PASSWORD; I added the column assertions to "says whether a password is configured, from has_credential" (getByRole columnheader "Credentials"; queryByRole columnheader /password/i is nu… |
| A123 | M | Mailbox "Address" is `type="text"`, not `type="email"` | closed | frontend/src/modules/email-intake/MailboxForm.tsx:36-43 (invalidAddress) wired at :80, and type="email"/inputMode at :211. Tests: "refuses an address that is not an email address" and "types the Address field as an email address". The auditor's extra ask — a pre-save "Test connection" — exists as a per-row action (test "reports a failed connection test inline without an error banner"), not as a pre-save gate. |
| A124 | M | "Add mailbox" is the same shared modal — third confirmation of the four shared gaps | closed | Same shared Dialog fix; see A106. |
| A125 | L | Disposition filter exposes `refused_loop` and other raw enum values | closed | frontend/src/modules/email-intake/intakeLabels.ts:362-374 maps refused_loop -> "Refused — mail loop detected" (and the two sibling refusal codes), used at both render sites: filter options MessageLog.tsx:77 and row badge :123. Test: "reads dispositions as sentences in both the filter and the rows" asserts the sentence in the row, that "refused_loop" appears nowhere, AND that the filter option carries the same words. |
| A126 | L | "Reopen window (days)" carries its unit in the label — the pattern /health should adopt | n/a | Audit entry is a recorded POSITIVE. The convention still holds: frontend/src/modules/email-intake/MailboxForm.tsx:314 labels the field "Reopen window (days)", and the validator repeats the unit in its refusal (:61, :65), so the unit survives into the error text too. |
| A127 | L | Double-hyphen used where an em dash is used elsewhere | closed | Both descriptions use a true em dash: notification/NotificationAdminPage.tsx:63 ("modules — gated by") and email-intake/EmailIntakeAdminPage.tsx:59 ("threaded reply — verifying"). Untested until now; I added "writes the page description with the em dash the rest of the product uses" to BOTH module test files (asserts the em-dash construction is present and that "--" appears nowhere in the description). Proved both can fail by putting the double hyphen bac… |

### /provisioning — Provisioning & Imaging

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A128 | B | Provisioning cannot be started — the Profile dropdown is empty | closed | frontend/src/modules/provisioning/ProvisioningRunsPage.tsx:256 computes noProfiles from a SETTLED query; :286 the empty <select> is replaced by <EntityPicker> whose emptyHint (PROFILE_PICKER_EMPTY_HINT) renders in place of the empty list; :298 a <Link to="/provisioning/profiles">Create a provisioning profile →</Link> is the link the auditor said was missing; :322 disabledReason gives Start a stated reason ("No provisioning profiles exist yet — create one… |
| A129 | M | "Provision a device" is the same shared modal — fourth confirmation | closed | Same shared Dialog fix; see A106. |
| A130 | L | Methodology note: synthetic `.click()` failed where a real mouse click succeeded | n/a | The auditor's own text: "Not a product defect — recorded to document audit rigour", "Fix: N/A". It records that a synthetic element.click() failed where a real mouse click opened the dialog, as methodology support for a BLOCKER in a different slice (/patch/policies). Nothing in frontend/src/modules/provisioning is asserted to be broken. For completeness the Provision button is a real <button> (ProvisioningRunsPage.tsx:108, via ui.tsx Button) and provision… |

### /site-servers — Site Servers

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| A131 | H | Internal spec reference shipped in production UI copy | closed | WAS STILL PRESENT at the start of this run — the description still read "…cache and PXE delivery are specified but not yet built (site-server spec §4.4)." Fixed at frontend/src/modules/site-servers/SiteServersPage.tsx:77, now "LAN-local helpers for authorized Wake-on-LAN and IPAM sweep preference. A site server is a promoted device. Content caching and PXE delivery are not available in this release." — the limitation is still disclosed, the internal secti… |
| A132 | M | The "Promote a device" dialog is a different variant — it has a real `<form>` but its controls are unlabeled… | closed | Same shared Dialog fix; see A106. |
| A133 | L | Positive: the best empty state in the product | n/a | Headed "Positive: the best empty state in the product", "Why it's a problem: N/A", "Fix: N/A". Verified the praised behaviour is still there and did not regress: frontend/src/modules/site-servers/SiteServerList.tsx:36 still renders "No site servers. Promote a device to add one." — condition plus the exact next action, matching the "Promote device" button at SiteServersPage.tsx:83. It is distinguished from loading (:34) and from a failed request (:35, role… |
| A134 | L | Device selector here uses a proper dropdown, unlike /deploy and /backup | n/a | Headed as a positive, "Why it's a problem: N/A", "Fix: N/A" (reuse this select elsewhere). Verified unregressed: PromoteDialog.tsx:84-104 is a real <select> listing devices by hostname with a "Select a device…" placeholder, and it goes further than the audit saw — a device with no enrolled agent, or one already promoted, is rendered disabled with the reason rather than dropped. Pinned by siteServers.test.tsx:328 "shows a device with no agent, disabled, wi… |

### /sla — SLA Engine

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B1 | B | "Add policy" Save is a silent no-op when no calendar is selected | closed | POLICY_RULES (PoliciesPanel.tsx:62-69) makes Calendar required and Save is deliberately left enabled (PoliciesPanel.tsx:344), so the press produces "Calendar is required" in role="alert". Test: sla.test.tsx:796 "keeps Save pressable with no calendar chosen and says the calendar is required" — asserts Save is enabled, the alert text, and that createSlaPolicy is never called. |
| B2 | H | Calendar dropdown in the policy form is empty with no explanation or escape hatch | closed | Behaviour was already built (PoliciesPanel.tsx:521-541: the calendar picker's emptyHint says why the list is empty, plus an inline "Add a business calendar" button wired through SlaAdminPage.tsx:101 to CalendarsPanel's openCreateNonce) but nothing tested it. Added sla.test.tsx:812 "says why the calendar list is empty and offers the way out of it": with zero calendars the Add-policy dialog explains the dependency, and the escape hatch closes it and opens t… |
| B3 | M | "Add policy" button does not reflect its toggled state; heading duplicates the button label | closed | The create form is no longer an inline toggle: PoliciesPanel.tsx:319 renders it as a modal <Dialog> (role="dialog", aria-modal, its own h2 title), so there is no ambiguous pressed state to signal and no second "Add policy" line 300px below the button. Test: the openDialog helper at sla.test.tsx:607 does findByRole("dialog", { name: "Add policy" }) and every Add-policy test goes through it — revert to an inline panel and they all fail. |
| B4 | M | Inline form is injected below the empty-state message rather than replacing it | closed | Same change: the create form is a modal over the page (PoliciesPanel.tsx:319, CalendarsPanel.tsx:358), so the "No policies yet" empty row is no longer sitting directly above the form the user is filling in. Pinned by sla.test.tsx:607 (openDialog) via sla.test.tsx:796/812/828/847. |
| B5 | L | Field labels sit inline and wrap mid-phrase, splitting label from control | **open** | Still present, and I cannot fix it inside this slice. components/ui.tsx#Field renders <label class="field"><span>label</span>{input}</label>, and NO stylesheet in the app defines `.field` — grep -rn '\.field' over src/*.css and src/shell/*.css returns only .search-field/.select-field/.input-field. The design file has the rule (docs/design/itms-prototype.html:733: `.field { display: flex; flex-direction: column; gap: 6px; }`) and the port never transcribed… |
| B6 | L | Page `<title>` is the constant string "ITMS" on every route | closed | AppShell sets `document.title` to `<page> · ITMS` from the nav item's own label, and restores it on unmount so the login screen does not inherit it. Test: AppShell.test.tsx 'names the page in the browser tab, not just ITMS'. Falsified. |
| B7 | H | Destructive Delete executes immediately with no confirmation and no undo | closed | Confirmation: PoliciesPanel.tsx:301 and CalendarsPanel.tsx:333 ConfirmDialogs naming the record and what the tickets lose; pinned by sla.test.tsx:1001 and :1020, which must click through "Delete calendar"/"Delete policy" to reach the failure. Styling half was unpinned, so I added sla.test.tsx:984 "does not style Delete like the Edit button beside it" — Delete carries .danger (PoliciesPanel.tsx:246, CalendarsPanel.tsx:277) on both tables, Edit does not. Fa… |
| B8 | H | Number fields accept negative and zero SLA targets; label echoes the nonsense value | closed | targetMinutesRule (PoliciesPanel.tsx:42-56) refuses negative, non-integer and zero targets, and describeTarget (PoliciesPanel.tsx:73-76) returns "" rather than echoing "(-50m)"/"(0m)" for a value that could not be saved. Tests: sla.test.tsx:828 (zero → "must be at least 1 minute"), :847 ("-5" → no "-5m" anywhere in the dialog, and "cannot be negative" on submit), :868 (a usable 90 still renders "(1h 30m)", so the fix is not "stop translating"). |
| B9 | H | Same silent-no-op failure repeats on every form in the module | closed | All three forms answer through requiredProblems + FormActions now: Add calendar (CalendarsPanel.tsx:171, test sla.test.tsx:675), Add policy (PoliciesPanel.tsx:171, test sla.test.tsx:796), Add holiday (HolidaysPanel.tsx:53, which had NO test). Added sla.test.tsx:423 "names the empty holiday fields instead of swallowing the click" — Add holiday is enabled, the click yields an alert naming both missing fields, and addSlaCalendarHoliday is not called. Falsifi… |
| B10 | M | Timezone field's placeholder doubles as the silently-applied default value | closed | CalendarsPanel.tsx:452-462: the timezone field has NO placeholder and a real prefilled value (DEFAULT_TIMEZONE, reset in openCreate at :148), with a sentence underneath saying it will be saved as typed. Test: sla.test.tsx:733 "does not disguise the applied timezone default as a placeholder" — asserts value === "Europe/London", no placeholder attribute, and the "will be saved as typed" sentence. |
| B11 | M | Timezone free-text field has no validation or autocomplete for IANA names | closed | The field is backed by a datalist built from Intl.supportedValuesOf('timeZone') (CalendarsPanel.tsx:45-72, list="sla-timezone-options" at :456) and invalidTimezone (CalendarsPanel.tsx:83-95) rejects "London"/"GMT"-shaped input before the round trip. Tests: sla.test.tsx:716 "offers IANA autocomplete…" (datalist present and populated) and sla.test.tsx:687 "rejects a timezone that is not an IANA name before the round trip". |
| B12 | M | Time inputs in the Working hours editor are too narrow; the value is visibly clipped | closed | Was still w-28 (112px), which is what clipped the meridiem. Widened both time inputs to w-36 (144px) at WorkingWindowsEditor.tsx:162 and :171. Test: sla.test.tsx:443 "gives the time inputs room for a meridiem instead of clipping it" — jsdom lays nothing out, so the pin is on the width token itself: it asserts the inputs do NOT carry w-28 and DO carry w-36. Falsified: restoring w-28 turns it red. Stated plainly: this guards the measurement that caused the… |
| B13 | M | Date format is US-locale ("8/19/2026, 6:51:22 AM") on a calendar whose default timezone is Europe/London | closed | CalendarsPanel.tsx:259 renders created_at through lib/datetime#formatDateTime in the operator's profile zone — one app-wide format, no seconds, unambiguous day/month. Test: sla.test.tsx:914 "renders a calendar's creation time in the profile zone, not the browser's" — expects "31 Dec 2025, 19:00" for a 2026-01-01T00:00Z fixture read from New York, and asserts the raw ISO string is absent; a US-locale toLocaleString() renderer fails it. |
| B14 | L | Row action buttons are right-aligned with unequal widths, producing a ragged left edge | closed | Was still `Add window (Monday)` as visible text in a free-width flex box. Fixed at WorkingWindowsEditor.tsx:198-216: fixed-width right-aligned action column (w-64 shrink-0 justify-end), visible labels are now the identical "Add window" / "24-hour day" on every row, and the weekday stays in aria-label so a screen-reader user still gets it. Test: sla.test.tsx:459 "labels the day-row actions identically and keeps the day in the accessible name". Falsified: p… |
| B15 | H | No "skip to content" link — 36 tab stops before reaching the main content, on every route | closed | Outside my files but verified in code: frontend/src/shell/AppShell.tsx:139 renders <a class="skip-link" href="#main"> as the first element, the target at AppShell.tsx:223 carries tabIndex={-1}, and shell/shell.css:297-310 keeps it visually hidden until focused. Test: frontend/src/shell/AppShell.test.tsx:259 "offers a skip to the main content, pointing at something that exists". |
| B16 | H | Table column headers fail WCAG contrast at a 9px font size | closed | Outside my files but verified: frontend/src/shell/base.css:33-43 sets th to var(--muted) on #fbfafc at font-size 12px (was #878590 at 9px, 3.49:1). Tests: frontend/src/test/contrast.test.ts:144 "holds the table header above the floor on its own background" (ratio computed from the shipped tokens, fails below 4.5:1) and :163/:182 the type-scale floor, which catches a 9px size written in CSS or in JSX. |
| B17 | M | Sidebar section labels fail contrast (3.9:1) at 9px | closed | Outside my files but verified: the sidebar label tone is now checked against BOTH gradient stops. Test: frontend/src/test/contrast.test.ts:150 "keeps the sidebar's section labels legible against the darker gradient stop" — asserts >= 4.5:1 against --sidebar and --sidebar-2, and :121 pins that the palette the auditors measured (#777380 on #14131a, 3.9:1) would fail. |

### /docs — IT Documentation

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B18 | B | Raw API validation JSON is rendered to the user as the error message, and overflows the card | closed | The 422 body is translated rather than printed: frontend/src/components/form.tsx:128-158 (describeValidation reads only `msg` and the last non-"body" `loc` element, and NEVER `input`) and :160-191 (describeSubmitError refuses to print anything starting with { [ <). DocsPage renders exclusively through FormActions (DocsPage.tsx:453-458); no JSON.stringify or String(error) remains in the module. Tests: components/form.test.tsx "names the field and the probl… |
| B19 | H | Title field has no maxlength, so the 300-character limit is only discoverable by failing | closed | frontend/src/modules/docs/DocsPage.tsx:413 maxLength={TITLE_MAX} (300, taken from ArticleCreate), with a client-side rule for the paste path at :49-52. Tests: "caps Title at the server's 300 characters rather than letting it fail there" (asserts the attribute) and "explains an over-long Title in a sentence instead of shipping it to the server" (asserts createArticle NOT called). The live "n/300" counter the auditor suggested is not implemented — an enhanc… |
| B20 | H | Error surfacing is inconsistent within the same form: empty submit is silent, over-long submit dumps JSON | closed | Both failure modes go through one component and one role="alert" (components/form.tsx:287-296), reached from DocsPage.tsx:453-458; the old `disabled={save.isPending \|\| !draft.title}` silent no-op is gone (Save stays enabled, :448-452). Tests: "names the empty field on submit instead of doing nothing" and "states a server refusal as a sentence, through the same channel as the field errors" — the empty case and the server case asserted on the same alert nod… |
| B21 | H | Markdown article headings, lists and links are rendered with no typographic styling | closed | WAS STILL BROKEN when I opened it: the container carried class="prose prose-sm" and NOTHING in this application defines .prose (no @tailwindcss/typography in package.json, no .prose rule in any of the six stylesheets) — so a class-based test would have passed against the defect. Fixed in frontend/src/modules/docs/markdown.ts:48-69 (TAG_STYLES, the audit's own numbers: h1 24px/600, h2 19px/600, h3 16px/600, ul disc + 1.25rem, p margin-block .75rem, styled… |
| B22 | M | The global table style force-uppercases markdown table headers, altering the author's content | closed | WAS STILL BROKEN: the bare `th { text-transform: uppercase; font-size: 12px }` rule at frontend/src/shell/base.css:33-46 is product-wide chrome with no markdown scope. base.css is outside this slice, so the opt-out travels with the element instead: markdown.ts:66 emits `text-transform: none; font-size: 13px; letter-spacing: normal; color: var(--ink)` on every markdown <th>, and an inline declaration outranks an element selector. Test: "does not uppercase… |
| B23 | M | Markdown editor textarea is 190px wide inside a ~650px card | closed | WAS STILL BROKEN: nothing gave the textarea a width — 190px is <textarea>'s intrinsic cols=20, and .input-field (shell/components.css:220-228) sets height but no width. Fixed at frontend/src/modules/docs/DocsPage.tsx:453 (w-full) inside the existing md:grid-cols-2 split. Test: "gives the markdown textarea its column instead of the browser's 20 columns". Proved red by removing w-full. Caveat stated in the test's own header comment: jsdom computes no layout… |
| B24 | M | "Markdown" label sits below and to the left of its textarea while "Preview" sits above its pane | closed | WAS STILL BROKEN, for exactly the reason the auditor measured: both panes used <Field>, whose <label class="field"> matches NO rule in any stylesheet this app ships (I grepped all six .css files — there is no .field rule), so an inline label beside an inline-block textarea sits on the shared baseline, i.e. at the bottom. Fixed at frontend/src/modules/docs/DocsPage.tsx:448-472: both panes are flex-col with the label first, the textarea keeps its programmat… |
| B25 | M | Article editor is not a dialog: Escape does not close it and focus is never moved into it | closed | frontend/src/modules/docs/DocsPage.tsx:219-227 — focuses the Title input on open and binds Escape to the same state change as Cancel. Test: "moves focus into the editor and closes it on Escape" asserts toHaveFocus() and that the editor is gone after {Escape}. Residual (a suggested extra, not the reported behaviour): focus is not returned to the "New article" button on close. |
| B26 | M | Selecting an article does not change the URL — articles are not linkable | closed | Selection lives in the query string: frontend/src/modules/docs/DocsPage.tsx:93-105 (read from and written to ?article=, pushed not replaced so Back works) and :160 after a save. Tests: "puts the selected article in the URL so it can be linked" (LocationProbe asserts article=a1) and "opens the article named in the URL on load" (renders at /docs?article=a1). The suggested "Copy link" affordance is not implemented — an enhancement beyond the reported behavio… |
| B27 | L | Blocked `javascript:` links render as indistinguishable plain text with no indication they were stripped | closed | frontend/src/modules/docs/markdown.ts:110-115 marks a refused link with data-link-removed, a title of "Link removed — unsupported URL scheme." and a wavy strikethrough; the refusal itself is unchanged. Test: markdown.test.ts "says a link was removed rather than emitting silent plain text" — asserts the marker, that javascript: is still absent, and that a legitimate link carries NO marker (so a blanket marker would fail it). |
| B28 | B | The default value of the "New record" Type dropdown can never be saved, and the error tells the user to call… | closed | frontend/src/modules/docs/DocsPage.tsx:32 DEFAULT_RECORD_TYPE="service" (explicitly not RECORD_TYPES[0]); the network option remains listed but disabled and relabelled "network — moved to IP address management" (:29, :34-36, :608-611); the remedy is an in-app <Link to="/ipam"> (:617-623) instead of REST verbs, and the server's message can no longer reach the DOM as {"detail":…} (it goes through describeSubmitError). Tests: "does not default the record Typ… |
| B29 | H | Server error text persists after the user corrects the input and is never cleared | closed | Every edit clears the refusal: frontend/src/modules/docs/DocsPage.tsx:234-238 (editDraft, the single mutation point for the article draft) and :582-585 (clearStaleFeedback, called from the record dialog's Type, Name and Fields handlers at :604, :631, :641). Tests: "drops the server's complaint once the input it described has changed" in both the article-editor and the new-record describes — each asserts the alert exists after submit and is gone after one… |
| B30 | H | Invalid JSON in the "Fields (JSON)" editor produces no error at all | closed | frontend/src/modules/docs/DocsPage.tsx:56-76 — the JSON parse is a field rule, not a step inside the mutation, so a SyntaxError becomes "Fields (JSON) is not valid JSON" in the same alert as a missing Name; a non-object gets its own message. Tests: "says the JSON is invalid rather than failing somewhere the user cannot see", "rejects valid JSON that is not an object, which the server would 422", and "creates the record with the parsed fields when the form… |
| B31 | M | Search renders an empty "Articles" group header when a query matches only records | closed | frontend/src/modules/docs/DocsPage.tsx:315-317 and :330-332 — each group header renders only when its bucket is non-empty. Test: "renders no Articles heading when only records match", scoped to the role="region" search-results container so it cannot be answered by the sidebar or a stat card. |
| B32 | L | Search field has no clear ("×") affordance | closed | frontend/src/modules/docs/DocsPage.tsx:279 type="search" plus an explicit Clear button at :285-296 that returns focus to the input. Test: "clears the search without making the operator delete it by hand" asserts the type attribute, the emptied value AND toHaveFocus(). |
| B33 | L | Record type appears twice on every record chip | closed | Both lists render the type pill first: search results at frontend/src/modules/docs/DocsPage.tsx:337-342, records list at :391-396. Untested until now; I added "orders a record chip the same way in the search results and in the list" to DocsPage.test.tsx — it puts the same record in both lists at once and asserts both containers read "serviceAuditB Net". Proved red by swapping the badge and name in the records list (it reported 'AuditB Netservice'), then r… |

### /changes — Change Management

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B34 | B | A change request can be created but can never leave `draft` — there is no approval, submit, schedule or close… | closed | src/modules/change/ChangeDetail.tsx:305-364 is a Lifecycle card rendering exactly the transitions backend `change_mgmt/state.py` will accept from the current state and type, derived by `availableChangeActions` (src/modules/change/changeActions.ts), RBAC-filtered by permission, with a decision comment, ConfirmDialog on the destructive/decision actions, and the SoD refusal stated in words. Tests (src/modules/change/__tests__/change.test.tsx): "submits a dra… |
| B35 | H | An inverted maintenance window (end before start) is accepted and displayed as-is | closed | src/modules/change/ChangeRequestForm.tsx:44-61 `windowOrderRule` compares the two fields AS INSTANTS (via toInstant, so a DST boundary cannot fool a string compare) and refuses end <= start on submit with an inline message; applied only when both are filled (ChangeRequestForm.tsx:75), since both are `datetime \| None` server-side. `min={windowStart}` on the end input (ChangeRequestForm.tsx:242). Tests: change.test.tsx "refuses a window that ends before it… |
| B36 | H | Times are shifted by 5 hours between entry and display — what you type is not what you see | closed | The 5-hour shift is gone at both ends: writes qualify the naive wall clock with the reader's zone (`toInstant`, ChangeRequestForm.tsx:161-162) and reads render through `formatDateTime(value, zone)` with the profile zone from `useUserTimezone` (ChangesPage.tsx:113-117, ChangeDetail.tsx:287-289). The zone is now STATED on the form (ChangeRequestForm.tsx:252-254), which was the other half of the auditor's fix. Tests: change.test.tsx "sends a maintenance wind… |
| B37 | H | "Link action" requires the user to know and hand-type a raw UUID | closed | src/modules/change/ChangeDetail.tsx:502-514 replaces the bare UUID box with `EntityPicker` for the two action types this control plane can enumerate (deployment, provision_run), naming each option by package/profile and date (pickerOptions.ts). `patch_run` and `patch_approval` keep a hand-typed field WITH a UUID shape rule and an on-screen sentence saying why (ChangeDetail.tsx:519-556) — backend has no route listing either (`grep -n "@router.get" backend/… |
| B38 | M | Seven-field create form has zero required fields and no field-level guidance | closed | src/modules/change/ChangeRequestForm.tsx:21-24 marks the two fields the server actually insists on (Title, Description) — `required` on the inputs (lines 203-219), a visible asterisk, and the sentence "Fields marked * are required." (line 190). Save stays enabled and answers through `requiredProblems`, so the empty submit now names the fields instead of no-opping. Type/Risk are selects that always hold a value and the window fields are `datetime \| None` s… |
| B39 | M | Change detail page exposes a raw UUID in the URL and offers no in-page link back to the list | closed | The navigational half — the actual defect — is fixed: src/modules/change/ChangeDetail.tsx:255-257 renders "← All changes" as the first element inside the content area, above the PageHead. Test: change.test.tsx "offers a way back to the change list from inside the page", FALSIFIED THIS RUN (removing the link turned it red). RESIDUAL, stated rather than hidden: the URL still carries the raw UUID, and it cannot be anything else from the frontend — `ChangeOut… |
| B40 | L | Change detail H1 is the user-supplied title, so the page has no stable identity | closed | The stated consequence — "a user with several change tabs open cannot tell them apart" — no longer occurs: src/modules/change/ChangeDetail.tsx:161-176 sets `document.title` to `<title> · Change · ITMS` while the page is mounted and restores the previous value on unmount. Test: change.test.tsx "names the browser tab after the change, so two open changes are distinguishable" (asserts both the set and the restore); FALSIFIED (removing the assignment turned i… |

### /jobs — Signed Job Engine

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B41 | B | Jobs cannot be cancelled, retried or requeued — the detail panel has no actions at all | **open** | No cancel/retry/requeue control exists, and none can be built from the console. Backend route search in /home/user/Toeshee-RMS/backend/app/api/v1/: `grep -n '^@router\.' jobs.py` -> POST "" (jobs.py:105), GET "" (:164), GET "/{job_id}" (:190) and nothing else; `grep -rn cancel app/api/v1/` matches only ws.py:181 (an asyncio task.cancel); a repo-wide `grep -rn '@router\.(post\|patch\|delete\|put)' app/api/ app/modules/*/api.py \| grep -i 'job\|cancel\|retry\|requ… |
| B42 | H | Four of five jobs have sat `queued` with 0 attempts for over 24 hours and nothing flags it | closed | jobState.ts:44-71 defines STALE_WAIT_MS and isStuck(job, now) over the waiting statuses; JobsPage.tsx:82 counts them into a "Waiting over 15 min" KPI tile (:113-118) that goes gold, :185-191 adds the per-row Waiting age column, and :122-129 renders a role="status" banner when stuck > 0 AND running === 0. Tests: JobsPage.test.tsx:158 "flags a job that has been waiting far too long, and counts it" and :171 "does not flag a job that was issued moments ago" (… |
| B43 | H | The "Signed job engine" never shows any signature information | **open** | No signature, key id, algorithm or verification status appears anywhere on /jobs, and the data does not exist to show. The column is on the model — backend/app/models/core.py:320 `signature: Mapped[str \| None]` — but backend/app/api/v1/jobs.py's JobOut (:31-44) exposes id, type, device_id, status, attempts, params, result, issued_at, expires_at, started_at, finished_at, logs and nothing else; `grep -rn 'signature\|key_id' app/api/v1/ app/modules/*/api.py`… |
| B44 | H | Job detail shows a raw JSON `{}` as the Result and identifies the job only by UUID | closed | JobsPage.tsx:216-224 heads the detail panel with jobTypeLabel(type) + status badge + stale badge and demotes the UUID to a mono sub-line (:225); :227-254 adds a Target/Attempts/Issued/Expires/Started/Finished dl; the Blob helper (:26-29) renders the caller's sentence instead of `{}` for an empty object, and the dispatched Parameters are shown (:257-259). Tests: JobsPage.test.tsx:235 "identifies the open job by type and time, not by UUID alone", :246 "says… |
| B45 | M | Selecting a job does not change the URL, and rows use buttons instead of links | closed | JobsPage.tsx:47 reads the selection from useSearchParams (`?job=`) and :198-202 renders each row action as a real <Link className="btn small" to={`/jobs?job=${job.id}`}> — middle-clickable, bookmarkable, pasteable. Tests: JobsPage.test.tsx:215 "puts the selected job in the URL and opens it from there" and :229 "opens a job named in the URL without a click" (the second is what proves the URL is the source of truth rather than a cosmetic push). |
| B46 | M | Job list has no filter, search, sort, pagination or refresh control | **open** | Three of the five asked-for controls are present and tested — a search box over type/id/device_id (JobsPage.tsx:143-152, test JobsPage.test.tsx:200 "searches by job type, and says so when nothing matches"), a status filter that includes a "Waiting too long" option (:153-166, test :188 "filters the list by status"), and live refresh via useLiveEvents invalidating ["jobs"] on job_status/job_log (:66-70). SORT AND PAGINATION ARE STILL MISSING, and I delibera… |
| B47 | L | A junk job type named `hello` sits in the production queue | closed | The UI half the finding is about — "the UI gives no way to distinguish real job types from test ones" — is closed. jobTypes.ts:52-66 keeps `hello` OUT of the dispatchable set so it cannot be created from the console, while jobTypes.ts:87 keeps it IN the label map as "Developer test job" so an existing queue row reads as what it is rather than as a bare identifier; JobsPage.tsx:176-182 and DeviceDetailPage.tsx:583-586 render the label over the wire type. T… |

### /reports — Reporting & Evidence

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B48 | H | Report cards in the catalogue have no titles — the link text is a 200-word paragraph | closed | FIXED THIS RUN. frontend/src/modules/reporting/ReportCatalogue.tsx:32 derives the report's name from its key (new reportText.ts `reportName` → humaniseEnum, so `sla-attainment` → "SLA attainment"); :40 and :55 render it as the card's <h3>, with the link inside the heading, and the summary/description demoted to supporting text at :60/:61. ReportView.tsx:360 makes the same name the page <h1> instead of the summary sentence. Tests added: reporting.test.tsx… |
| B49 | H | There is no data visualisation anywhere in the reporting module | closed | FIXED THIS RUN. New frontend/src/modules/reporting/reportFigures.ts maps each report key to a figure built by SELECTING named values out of `page.totals` (no arithmetic — the module's structural no-aggregation sweep still passes), and returns null if any named total is absent rather than substituting a zero. ReportView.tsx:337 computes it and :540 renders components/chart.tsx BarChart with the window in the title, the unit on the axis, a text legend and t… |
| B50 | H | A zero-row result still renders nine metric tiles all showing "0" | closed | FIXED THIS RUN. frontend/src/modules/reporting/ReportView.tsx:523 branches on `page.meta.total === 0` and renders a single components/emptystate.tsx EmptyState ("Nothing matched these parameters") instead of the totals tiles, the figure and the table; tiles render only on the non-zero branch. Tests added: reporting.test.tsx "suppresses the totals tiles rather than rendering a screen of zeros" (asserts no tile label, no <dd>0, no figure) paired with "still… |
| B51 | M | Report parameters expose raw API concepts: a free-text "Queue Id" UUID, plus Limit and Offset | closed | FIXED THIS RUN, both halves. Queue Id: ReportView.tsx:397 renders components/picker.tsx EntityPicker for the `queue_id` parameter, fed by the new `useTicketQueues` hook in reporting/api.ts (GET /service-desk/queues, already exists on the client) and enabled only when the report publishes that parameter. Limit/Offset: :269 drops `offset` from the params bar entirely and :606/:614 replace it with Previous/Next rows buttons driven by the server's echoed meta… |
| B52 | M | Validation message names raw API parameters instead of the visible field labels, and is not announced | closed | frontend/src/modules/reporting/ReportView.tsx:116 `paramRules` labels every rule with `control.label` — the schema title printed above the box ("From", "To") — not the wire name, and the summary is rendered by components/form.tsx FormActions inside a role="alert". Test: reporting.test.tsx "names every unfilled required parameter, BY ITS PRINTED TITLE, when Run is pressed" (asserts "From is required"/"To is required" via findByRole("alert")). FALSIFIED THI… |
| B53 | L | Unrendered markdown backticks leak into report descriptions | closed | FIXED THIS RUN. New frontend/src/modules/reporting/reportText.ts `reportProse` strips backtick spans; applied to every server-authored summary and description in ReportCatalogue.tsx:41/:42/:60/:61 and ReportView.tsx (head description and the description paragraph below it). Normalised at render rather than in the Python definitions, because the same strings are the OpenAPI summaries and CSV headers where a backtick is correct. Test added: reporting.test.t… |
| B54 | L | Descriptions use `--` where an em dash is intended | closed | FIXED THIS RUN. Same `reportProse` in frontend/src/modules/reporting/reportText.ts converts ` -- ` to a spaced em dash, applied at the same four render sites; the one occurrence in the console's own copy (ReportCatalogue.tsx BiExportNotice, "dataset export -- there is no") was edited to an em dash directly. Same test asserts the card text contains "stale — so the device total" and no " -- ". FALSIFIED alongside B53. |

### /budget — Budget (Dashboard / Planner / Expenses / Confirm queue / Admin)

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B55 | H | The "Budget dashboard" for plan-vs-actual-vs-committed spend contains no chart | closed | frontend/src/modules/budget/BudgetDashboard.tsx:243 renders <BarChart> with three named series (Planned/Actual/Committed, built at :112-116) inside a 'Plan versus actual' Panel, unit={baseCurrency} from useBudgetSettings so the axis names the currency, and a period <select> at :231. Tests: budget.test.tsx:1582 'draws plan, actual and committed as three named series' (asserts each series name appears twice inside the <figure> - legend + the mirrored data t… |
| B56 | H | Fiscal year is an unbounded number input — it accepted `-3000` | closed | frontend/src/modules/budget/BudgetPlannerPage.tsx:59 invalidFiscalYear() rejects a leading '-', a non-4-digit value, and anything outside 2000..2100 (the server's own budget/schema.py _FiscalYear = Field(ge=2000, le=2100)); wired as a FieldRule at :52 and run by requiredProblems at :192 before create.mutate. The control itself carries min={2000} max={2100} at :215-221. Existing tests: budget.test.tsx:692 'refuses a negative fiscal year rather than sending… |
| B57 | M | Tab bar is built from plain buttons with no tab semantics | closed | frontend/src/modules/budget/budgetState.tsx:103 renders <nav aria-label="Budget" role="tablist"> and :107-113 each tab as a react-router <Link role="tab" aria-selected={active} aria-current={active?'page':undefined}>. Shared by all five budget pages via <BudgetPageNav>. Tests: budget.test.tsx:225 'links to every budget surface from the module nav' (queries getByRole('tablist',{name:'Budget'}) then getAllByRole('tab') - both throw if the roles are absent -… |
| B58 | M | Two adjacent empty states on one dashboard, each phrased as a dead end | closed | frontend/src/modules/budget/BudgetDashboard.tsx:91 computes noBudgets and :156-171 renders ONE first-run Panel 'No budget yet' containing an inline <Link to="/budget/planner">Create a budget in the Planner</Link> plus the permission and category prerequisites; :173 suppresses both old panels entirely when noBudgets, so 'No position data yet.'/'No variance data yet.' cannot both appear. Test: budget.test.tsx:1223 'replaces the two dead ends with one state… |
| B59 | M | New budget form has no required fields | closed | frontend/src/modules/budget/BudgetPlannerPage.tsx:217 (Name) and :219 (Fiscal year) carry required; BUDGET_RULES at :51-53 is the pre-submit validation and FormActions at :265 renders the result in role="alert". Granularity is a <select> that always holds one of its two values, so it is correctly not required. Post-submit signal tested by budget.test.tsx:678 'names the missing field on an empty submit instead of disabling Save'. The auditor's measurement… |
| B60 | L | Tabs change the URL but are not links | closed | frontend/src/modules/budget/budgetState.tsx:107 the tabs are react-router <Link> elements, so each renders a real <a href> that middle-clicks and right-click-copies. Test: budget.test.tsx:225 'links to every budget surface from the module nav' asserts tabs.map(getAttribute('href')) equals ['/budget','/budget/planner','/budget/expenses','/budget/confirm','/budget/admin'] - it fails the moment any tab goes back to being a <button>, and the same test pins do… |

### /ipam — IP Address Management

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B61 | H | An impossible CIDR (`999.999.999.999/99`) is accepted by the field and Register is a silent no-op | closed | invalidPrefix at frontend/src/modules/ipam/SubnetTreePage.tsx:362-377 rejects a non-CIDR with the expected form named in the message; :356 wires it into PREFIX_RULES; the panel is a real <form> and Register is never disabled for bad input (:493-501), so refusals land in FormActions' role="alert". Tests (ipam/__tests__/ipam.test.tsx, describe "registering a subnet"): "refuses an impossible CIDR with the form it expects, and sends nothing", "names the missi… |
| B62 | M | KPI tiles read 0/0/0/0 with no subnets registered, presenting an empty install as a clean one | closed | FIXED THIS RUN. frontend/src/modules/ipam/SubnetTreePage.tsx:139-158: the four tiles render only when at least one subnet exists; with none, a first-run sentence replaces the row saying there is "no drift figure to report, not a drift figure of zero", and a different sentence when the emptiness is the filters' doing. Nothing is claimed while the query is in flight or errored. Took the fix line's second form (replace the row) rather than "-", because StatC… |
| B63 | L | Six tabs on /ipam are unlabelled as a tab set (same defect as /budget) | closed | Behaviour was already in the code - frontend/src/modules/ipam/IpamNav.tsx:28 (nav aria-label="IPAM" role="tablist") and :39-40 (role="tab" aria-selected={active}) - but NO test existed, so it was not closed before this run. Tests added in ipam/__tests__/ipam.test.tsx, describe "the IPAM tab strip is a tab strip to a screen reader": "exposes all six surfaces as tabs in a named tablist" (asserts the six labels in order) and "marks exactly the current surfac… |

### /audit — Immutable Audit

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B64 | H | The audit log identifies every actor and target by raw UUID only — you cannot tell who did what | **open** | STILL PRESENT: actor and target are still identified by raw id (src/modules/audit/AuditPage.tsx:319-330). WHAT IS MISSING is a backend route that resolves a user id to a display name or a record id to a title. Searched /home/user/Toeshee-RMS/backend/app/api/v1/ (`ls` shows audit, credentials, devices, enrollment, errors, jobs, me, orgs, platform, realm_settings, roles, setup, ws; `grep -rn "@router.get" app/api/v1/orgs.py app/api/v1/roles.py app/api/v1/me… |
| B65 | M | The "Chain integrity" tab does not visibly change the view | closed | The Chain integrity tab renders a genuinely different panel — src/modules/audit/AuditPage.tsx:346-401, "Hash-chain integrity" with four mutually exclusive states (verifying / verification-did-not-run with the server's own reason / BROKEN naming `broken_at` and the count walked / verified with the count) driven by `GET /audit/verify`; the events table is not rendered on that tab (the `tab === "events"` branch at AuditPage.tsx:258). Tests: AuditPage.test.ts… |
| B66 | M | Chain hashes are truncated with no way to see or copy the full value | closed | src/modules/audit/AuditPage.tsx:94-113 — the hash cell keeps the 16-char visual token but carries the FULL hash in `title` (hover/expand) and a per-row copy button whose accessible name is the full hash, writing the whole value to the clipboard with a `role="status"` confirmation (AuditPage.tsx:122-131, 338-343). Tests: AuditPage.test.tsx "keeps the FULL chain hash reachable, not just its first sixteen characters" and "copies the whole hash, and says whic… |

### /licenses — Licenses

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B67 | H | "New license" cannot be completed on a fresh install: both required lookup dropdowns are empty | closed | frontend/src/modules/licenses/LicensesPage.tsx:395 replaces the empty <select> with <EntityPicker> carrying emptyHint=PRODUCT_PICKER_EMPTY_HINT (pickerOptions.ts), and :404 renders an inline <Link to="/licenses/products">Add a product to the catalog</Link> when catalogEmpty (:309, gated on !products.isLoading so a list in flight is not reported as empty). Save is no longer disabled: submit() at :349 runs requiredProblems(licenseRules(...)) and names 'Prod… |
| B68 | H | Empty submit in the license modal is a silent no-op | closed | frontend/src/modules/licenses/LicensesPage.tsx:510 FormActions has no disabled-on-empty condition (the old `disabled={create.isPending \|\| !productId}` is gone); submit() at :349-353 sets problems from requiredProblems(licenseRules(cost), ...) and returns without mutating, and FormActions renders the joined messages in a role="alert" (components/form.tsx:279). Tests: licenses.test.tsx:440 'names the missing field on an empty submit instead of disabling Sav… |
| B69 | M | The license modal has correct dialog semantics but no Escape, no initial focus, no scroll lock and no close b… | closed | Same shared Dialog fix; see A106. The labelled close button was already present. |
| B70 | M | Entitlements is an unbounded number field | closed | frontend/src/modules/licenses/LicensesPage.tsx:448-455 the Entitlements input is type="number" min={1} step={1} required, and wholeSeatCount() at :118 (wired as a FieldRule at :86) refuses anything that is not /^\d+$/ with value >= 1, before create.mutate. Existing behaviour tests: licenses.test.tsx:1088 'refuses a zero entitlement count rather than sending it' and :453 'refuses a cleared Entitlements rather than sending NaN' - both assert createLicense w… |
| B71 | L | License key is masked as a password field with no reveal toggle | closed | frontend/src/modules/licenses/LicensesPage.tsx:321 holds keyVisible state; :491 sets type={keyVisible ? "text" : "password"} and :494-497 is a labelled toggle button reading 'Show key'/'Hide key' so the current state is stated rather than inferred from the dots. Masked-by-default is kept deliberately (the key is sealed server-side and read back only through the audited reveal endpoint). Test: licenses.test.tsx:1072 'masks the license key by default and re… |

### /subscriptions — Subscriptions

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B72 | B | Raw Pydantic JSON errors confirmed in a THIRD module — this is a product-wide error-handling failure, not a /… | closed | frontend/src/components/form.tsx:130 describeValidation() parses a FastAPI 422 body, maps each detail[].loc to a screen name via humaniseField() ('cost_per_cycle' -> 'Cost per cycle') and joins every detail[].msg - and deliberately never reads detail[].input or ctx, so neither the submitted value nor max_length can reach the screen. describeSubmitError() at :167 also refuses to print any body starting with { [ or <. The subscriptions dialog routes its fai… |
| B73 | H | The two headline KPIs cannot be populated by the only create form | closed | frontend/src/modules/subscriptions/SubscriptionsPage.tsx now carries all four missing controls in the create form: 'Renews on (optional)' type="date" at :447-452, 'Notice period (days)' number min=0 max=365 at :462-472 with an explanatory paragraph at :474, an auto-renew checkbox at :480 defaulted FALSE to match schema.py's `auto_renew: bool = False` (a guessed true would manufacture notice deadlines), and an Owner EntityPicker at :487. All are sent by su… |
| B74 | H | Money and currency are unvalidated free-text fields | closed | frontend/src/modules/subscriptions/SubscriptionsPage.tsx:84 decimalAmount() refuses anything that is not /^\d+(\.\d{1,2})?$/ ('not-a-number!!', '1,200', '£1200') and :93 currencyCode() refuses anything that is not exactly 3 letters ('ZZZZZ', 'US', '$'); both are FieldRules (:72-73) run before create.mutate. The Cost control is type=text inputMode="decimal" (:414-421) rather than type=number on purpose - the value is a Decimal string on the wire and a numb… |
| B75 | M | Same modal pattern, same four gaps | closed | Same shared Dialog fix; see A106. |

### /error-monitor — Error Monitor

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B76 | H | "Resolve" is a one-click, unconfirmed, irreversible state change — there is no way to reopen a group | **open** | PARTIAL FIX, NOT CLOSED. Fixed and tested this run: (1) Resolve is no longer one-click — frontend/src/modules/error-monitor/ErrorMonitorPage.tsx:388 ConfirmDialog names the group and states the irreversibility; the button label is now 'Resolve…' (:471). Test 'writes nothing on the first click and names the group in the confirmation' + 'resolves nothing when the confirmation is cancelled'. (2) The write is no longer silent — live region at ErrorMonitorPage… |
| B77 | M | No filtering, search, sorting or time range on the error list | closed | frontend/src/modules/error-monitor/ErrorMonitorPage.tsx:246-330 is a FilterBar with search (maxLength 128), triage state (All/Open/Resolved), capture seam, last-seen range (24h/7d/30d) and sort (last seen / first seen / most events); the selection is held in the URL query string (setFilter at :194, read at :175-190, unknown values ignored) and a Clear filters control appears only when something is filtered. Matching/sorting is the exported pure function s… |
| B78 | M | Correlation columns are the module's stated purpose but every value is empty | closed | ErrorMonitorPage.tsx:497 `Correlation` renders a present id verbatim and an absent one as NoValue with the sentence 'No <dimension> was in scope when this occurrence was captured' — so the dash keeps its visual role but the cell now says which of 'not applicable' / 'not captured' it is, and a screen reader gets the sentence instead of 'em dash'. The panel subtitle (:359) states the same rule for the whole table. This is the honest distinction: the API set… |
| B79 | L | Raise site exposes internal library paths and line numbers | closed | CLOSED BY `f5e8ab0`, in the backend, as recommendation 9 below predicted it would have to be — capture now fingerprints at the deepest `app.*` frame, versioned, with no backfill possible for rows the old rule already named. AS FOUND ON THE WALK: still present, and not fixable from the frontend: the group's module/lineno/qualname are computed server-side. backend/app/core/errors/capture.py:132 `fingerprint_of` hashes `type\|module\|lineno\|qualname` taken from `_raise_site(tb)`, which the module docstring (:12) says walks to the traceback's LAST frame — i.e. the third-party library frame, exactly as the auditor described for asyncpg. The auditor's fix (prefer the deepest `app.*` frame, keep the libra… |

### /platform-status — Platform Status

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B80 | H | The platform is genuinely DOWN and the only global indicator is a 51×23px, 9px-type pill in the far corner | closed | frontend/src/modules/platform-status/PlatformStatusPage.tsx:167 renders a full-width role=alert banner ABOVE the KPI row whenever overall is not ok, any probe is not ok, or any process is short of instances; it names the count and each failing probe with its reason code ('1 of 2 dependency probes is failing — objectstore unreachable'), carries the reason's guidance sentence and a link onwards. The small pill stays as the healthy-state indicator (:137). Te… |
| B81 | H | Status pills fail WCAG contrast — including the "down" state | closed | Fixed in cycle 9 and verified in code this run, not taken on trust. frontend/src/shell/components.css:207 sets `.status` font-size to 12px (was the measured 9px), and :218-228 point every pill at the `-ink` text tones on their soft washes (`--red-ink`/`--red-soft`, `--green-ink`/`--green-soft`, …) rather than the fill tones the auditor measured. Test: frontend/src/test/contrast.test.ts — the it.each row '%s on %s clears 4.5:1' computes each pair from the… |
| B82 | M | No "as of" timestamp, no refresh control, and no auto-refresh on a live status page | closed | PlatformStatusPage.tsx:157 renders 'Probes read at <instant>, and re-read every 15 seconds.' from the query's dataUpdatedAt, formatted through lib/datetime.ts formatDateTime in the operator's profile zone (new useProfileZone in modules/platform-status/api.ts, mirroring the error monitor); the interval is derived from PLATFORM_STATUS_POLL_MS so the sentence cannot drift from what the query actually polls. A manual 'Re-probe now' button sits in the page hea… |
| B83 | M | A failing dependency offers no next step | closed | PlatformStatusPage.tsx:96 REASON_GUIDANCE gives every one of the seven codes in app.core.health.types.ReasonCode an operator-facing sentence ('unreachable' → 'The dependency did not answer. Check that it is running and that this host can still reach it.'); the failing row renders the humanised code, the raw code (kept greppable) and that sentence at :303-306, and the banner carries the same guidance plus a Re-probe control and a link to /error-monitor?sta… |

### /bcp — BCP / DR Tests

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B84 | B | TIMEZONE SHIFT REPRODUCED IN A THIRD MODULE — every datetime in the product is displayed 5 hours earlier than… | closed | The −5h shift is gone in BOTH modules the finding names. BCP: `toInstant(scheduledFor, zone)` on write (src/modules/bcp/PlanDetail.tsx:289) and `formatDateTime(..., zone)` on read (PlanDetail.tsx:235-236); same for the recorder's executed_at. Changes: ChangeRequestForm.tsx:161-162. The remaining half of the auditor's fix — "label every datetime input with that zone" — shipped this run: src/modules/bcp/PlanDetail.tsx:322-332 states "Times are in America/Ne… |
| B85 | H | Negative RTO and RPO are accepted and displayed as recovery objectives | closed | src/modules/bcp/PlanEditor.tsx:65-92 — `recoveryObjectiveRule` refuses a leading '-' and any non-integer on both RTO and RPO with a field-named message, applied on submit; `min={0}` on both inputs (PlanEditor.tsx:363,379). The auditor's second ask is there too: an RTO < RPO warning that does not refuse the save (PlanEditor.tsx:180-183, 391-396). Tests: bcp.test.tsx "refuses a negative RTO rather than sending it", "refuses a negative RPO rather than sendin… |
| B86 | H | A plan reviewed *never* is reported as "up to date", and an overdue review can never be detected | closed | src/modules/bcp/reviewState.ts:42-52 — `last_reviewed_at == null` returns the distinct standing "never" (label "never reviewed", destructive variant) and can never return "up to date"; "unknown" is a fourth state so a missing/failed status response no longer renders green. One function feeds BOTH the column and the KPI (planReviewStandings, reviewState.ts:91-102; PlansPage.tsx:44-48), which is what stopped them disagreeing. Tests: bcp.test.tsx "calls a pl… |
| B87 | M | Six-field plan form has no required fields and no explanation of RTO/RPO | closed | src/modules/bcp/PlanEditor.tsx:274-285,442 — "Fields marked * are required." plus `Name *` and `Review interval (days) *`, both carrying `required` (added this run; RTO/RPO stay unmarked because `rto_minutes: int \| None = None` in bcp/schema.py and marking them would be the same lie inverted). The definitions the auditor asked for are on the form (PlanEditor.tsx:375-382: "RTO — how long recovery may take before service is back", "RPO — how much recent dat… |
| B88 | M | "Archive" sits beside "Edit plan" with no confirmation | closed | src/modules/bcp/PlanDetail.tsx:100-118 and 152-163 — Archive now opens a ConfirmDialog naming the plan and what archiving does; Unarchive deliberately does not ask (it takes nothing away); Record review has its own confirmation too. Tests: bcp.test.tsx "does not archive on the first click", "archives once the dialog is confirmed", "leaves the plan alone when the archive dialog is dismissed", "unarchives without asking" (the deliberate asymmetry), "asks be… |

### /assistant/autonomy — AI Automation (Assistant autonomy)

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B89 | B | A security control is mutated and persisted by a single dropdown change with no Save, no confirmation and no… | closed | frontend/src/modules/ai-automation/AutonomyDialPage.tsx:121 holds edits in a `draft` map and :149 `edit()` only mutates that map — nothing writes until `applyChanges` runs behind the ConfirmDialog at :338, with role="status" unsaved/saved lines at :324 and :332. Tests: aiAutomationPrimitives.test.tsx "writes nothing when a cell is changed — the edit is a draft until Save", "asks before saving, names the cell and says what the new level permits", "writes e… |
| B90 | M | The matrix has no row/column explanation of what the three settings actually permit | closed | frontend/src/modules/ai-automation/AutonomyDialPage.tsx:78 `LEVEL_MEANING` defines all three values and :214 renders them as a <dl> legend on the page itself (not only inside the confirmation); the `(fixed)` end_user cell carries an explanatory title at :270. Test: aiAutomationPrimitives.test.tsx "defines all three levels on the page, not only inside the confirmation" and "keeps the end_user row fixed at off, with no control to change it". FALSIFIED THIS… |
| B91 | L | What LOW / MEDIUM / HIGH risk mean is never defined | closed | frontend/src/modules/ai-automation/AutonomyDialPage.tsx:223 states where the classification comes from — "Risk class is a property of each approved action in your organization's catalogue, not of this page. Every prepared action shows the class it was classified under." — rather than implying this screen defines it (it cannot: the tier is a column on each approved action). Test: aiAutomationPrimitives.test.tsx "defines all three levels on the page, not on… |

### /enrollment — Enrollment & Certificates

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B92 | B | Direct load or refresh of /enrollment returns raw API JSON with no application shell | closed | Traefik `PathPrefix(`/enroll`)` raw-prefix-matched the console's own `/enrollment` route and handed it to FastAPI. Narrowed to `PathPrefix(`/enroll/`)` in deploy/docker-compose.yml — the trailing slash cannot match `/enrollment` and still matches the only route under it, POST /enroll/v1. nginx's SPA fallback was correct all along. NOT TEST-COVERED: nothing in this repo exercises Traefik; verified by reading the rule against the route table, and confirmed… |
| B93 | H | "Create token" mints a live enrollment credential instantly — no form to fill, no confirmation | closed | Fixed in cycles 7/8; verified in code and by falsification this run. frontend/src/modules/enrollment/EnrollmentPage.tsx:486 'Create token' calls toggleForm (:341) which only reveals the form — it writes nothing; :327 submitIssue validates and sets pendingIssue, explicitly NOT calling the API; the ConfirmDialog at :826 is the only thing that reaches createEnrollmentToken, and the submit button is labelled 'Issue token…' with the ellipsis promising a furthe… |
| B94 | H | Enrollment tokens default to unlimited uses and are all "(unnamed)" | closed | Verified in code and by falsification. EnrollmentPage.tsx:153 BLANK_FORM sets maxUses to '1' (a blank field was what sent max_uses:null, which the API reads as unlimited); :159 NAME_AND_LIFETIME makes the token name a required rule (components/form.ts requiredProblems:66 trims, so whitespace is not a name); unlimited is an explicit opt-in checkbox worded as a risk (:530); and the table says 'unlimited' in words under the ∞ glyph (:643). Tests: 'defaults t… |
| B95 | M | Token secret is shown once in an alert the user can dismiss before copying, with no copy button | closed | Verified in code and by falsification. EnrollmentPage.tsx:439 is a 'Copy token' button using copyToClipboard (:196), which reports failure rather than swallowing it: success renders a role=status 'Copied to the clipboard' and failure a role=alert telling the operator to select the value by hand. 'I have copied it' dismisses straight through only when the copy button was actually used; otherwise it opens the ConfirmDialog at :848 ('Dismiss without copying'… |

### /settings — Settings

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B96 | M | Org profile is a one-field form whose only control is a Save button with no feedback path | closed | frontend/src/modules/settings/OrganizationTab.tsx:337 puts a ConfirmDialog naming old → new in front of a tenant rename only (not in front of ordinary settings keys), and FormActions at :331 announces success in a role="status". FIXED THIS RUN: the finding's remaining ask, a dirty-state indicator, added at :310 ("Unsaved changes on this tab. Nothing is written until you press Save."), deliberately not a live region so it does not collide with the announce… |
| B97 | L | Only 6 of the platform's 25 registered modules have a settings tab | closed | frontend/src/modules/settings/SettingsPage.tsx:158 no longer claims "Every configuration this platform serves" — it reads "The configuration this console can edit: the tabs below, and only those … Modules not listed here are configured on the server or from their own page", which both narrows the claim and tells the person hunting for IPAM where to look. Test: settingsPage.test.tsx "claims only the configuration it actually serves" (asserts the new senten… |

### Responsive sweep — attempted, partially blocked

| # | Sev | Finding | Verdict | Evidence |
|---|---|---|---|---|
| B98 | H | The application has essentially no responsive CSS — one breakpoint at 1300px and nothing below it | closed | Auditor B's version of A110, and its two specific asks are met: the sidebar becomes an overlay drawer below 1024px (frontend/src/shell/shell.css:463-478, opened by the 44px hamburger at AppShell.tsx:163) rather than by a manual button, and the KPI rows step 6→3→2→1 (components.css:317 / :328 / :337). Tables get the horizontal-scroll container it asked for (components.css:158 + :190). Test: src/test/responsive.test.ts 'counts more app-authored breakpoints… |
| B99 | M | 49 interactive controls are under the 44px minimum tap-target height on a single page | closed | The auditor measured `button, a, input, select` — and the cycle-10 coarse-pointer block covered only buttons and links, so every text field and select in the console kept its 36px desktop height under a thumb (components.css:220 sets height:36px on .search-field/.select-field/.input-field). Added .search-field, .select-field, .input-field, textarea, .menu-item and .session-notice-action to frontend/src/shell/shell.css:530-536; min-height clamps the declar… |

## What the programme changed, in one place

Twelve cycles. The shape that mattered was that most of the 233 findings were
not 233 problems — they were a handful of kernel defects observed on many
pages, and fixing a kernel closed findings in bulk:

| Cycle | Kernel | Findings it retired |
|---|---|---|
| 1 | Realm convergence in `keycloak-init` | 1 BLOCKER + the operator's live bug |
| 2 | App chrome: user menu, `/profile`, tenant, sign-out, session expiry, skip link | 2 BLOCKER, ~10 HIGH |
| 3 | Form kernel: end the silent no-op | 1 BLOCKER, ~45 |
| 4 | One error surface: no raw JSON in the UI | 3 BLOCKER, ~20 |
| 5 | Datetime: the five-hour shift | 1 BLOCKER, ~6 |
| 6 | `EntityPicker`: kill the hand-typed UUID | ~60 adoption sites |
| 7 | Module blockers: flows that could not complete | 6 BLOCKER |
| 8 | `ConfirmDialog`: confirm, and mean it | ~76 adoption sites |
| 9 | Contrast and type scale | ~15 |
| 10 | Responsive layout and the navigation drawer | ~6 |
| 11 | Data display: `humaniseEnum`, `Attributes`, `EmptyState`, charts | ~10 |
| 12 | The long tail, the shared `Dialog`, and this walk | ~30 |

## The guards that keep it closed

Tests that would fail if a whole class of defect returned, rather than one
instance of it:

- `frontend/src/test/contrast.test.ts` — computes every WCAG ratio from the
  shipped tokens, and carries the auditors' original measurements as a live
  falsifier, so a guard that stopped being able to see the defect goes red. It
  also enforces the 12px type floor across the four shell stylesheets **and**
  inline `fontSize` in JSX — the second half was added after the first half
  passed green while three sizes shipped below the floor.
- `frontend/src/test/responsive.test.ts` — 12 assertions over the breakpoints,
  the drawer, and the touch-target floor.
- `frontend/src/test/tokens.test.ts` — the palette cannot drift from
  `docs/design/itms-prototype.html`; a deliberate change has to be made in the
  design first.
- `frontend/src/components/dialog.test.tsx` — the shared modal's Escape, focus
  and scroll-lock contract, which is what six findings now rest on.
- `frontend/src/components/{form,confirm,picker,attributes,chart}.test.tsx` —
  the primitives, each fixture taken from the audits' own DOM evidence.

## What this document does not claim

Every verdict above is about **code and tests**. A test asserts that the right
text reached the DOM; it does not tell you whether a hatch pattern is
distinguishable, whether 12px uppercase is legible on a real panel, or whether
a chart legend collides with its axis at 640px. jsdom implements no cascade and
evaluates no media query, which is why the responsive guard reads CSS as text
and says so.

A browser pass over the running console — and an axe run against the rendered
page, which can see colours composited by opacity and painted by gradients that
a token-level check cannot — is the complement to this, and has not been done.
The dev-only harness at `/preview` now renders every primitive this programme
added beside the design they have to live inside, which is where that pass
should start.

