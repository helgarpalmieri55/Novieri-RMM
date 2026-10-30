# Phase 4 — Patch Management Module Specification (control plane)

Status: approved (brainstorming decisions PD-01…06, 2026-07-19)
Source brief: `docs/modules/patch.md`. Kernel contracts: `docs/kernel/spec.md`.
Hard dependency (built): **Inventory** (`app/modules/inventory`) — supplies OS +
installed-software state. Also builds on: signed-job gateway, RBAC, policy,
audit, the Celery worker.

## 1. Scope

Control-plane side of fleet patch management: detect missing OS and third-party
patches per endpoint, drive them through an **approval → deployment-ring →
maintenance-window → apply** loop with policy-controlled reboots, and produce
per-device + fleet **compliance reporting** (a direct SOC 2 evidence output),
across Windows, Linux, and macOS.

The per-OS agent patch engines run on the endpoint (agent-side, under `agent/`,
not built); this cycle builds the **full server slice with the agent engines mocked**
(PD-01), plus the agent wire contracts. A live per-OS apply is a deploy/agent
concern, mirroring how Phase 3 handled the live guacd seam.

**Locked decisions (design gate):**

- **PD-01 — Build scope:** full server slice (scan ingestion, patch catalog,
  per-device status, approval workflow, rings + windows, signed `patch.apply`
  dispatch, compliance reporting, wire contracts), agent engines mocked.
- **PD-02 — Approval posture:** approval-required by default; an opt-in
  per-policy rule may auto-approve **security-severity** patches into the
  **test** ring only (`auto_approve_security`, default **false**).
- **PD-03 — Third-party coverage:** OS updates **plus** whatever the agent's
  native package managers report (`winget`, `apt`/`dnf`/`zypper`, Homebrew). No
  server-curated catalog in v1 (that is a later phase).
- **PD-04 — Rings & windows:** named deployment rings (`test → pilot → broad`)
  scoped by device group via a `patch_policy`; maintenance windows as cron-like
  policy resolved to the agent's local scheduler; `patch.apply` dispatched only
  inside an open window.
- **PD-05 — macOS:** best-effort `softwareupdate` for scan + apply where
  permitted; OS-update **enforcement** on Apple Silicon is honestly reported as
  requiring the MDM fast-follow.
- **PD-06 — Sentinel:** stub the seam — a nullable `priority` field a future
  Sentinel feed populates; v1 orders/surfaces by source **severity**.

Out of scope this cycle: the agent-side `patch.scan`/`patch.apply` *engine*
implementations (agent-side, wire-contract-defined here); a server-curated
third-party catalog; pre-patch backup trigger (Backup module, later); change-
ticket gating (Change Mgmt, later); patch-success prediction; automated ring
auto-advance (operator advances rings in v1).

## 2. Architecture (registers into the kernel; forks nothing)

Two signed job types drive the endpoint, both with result handlers (the same
extension point Inventory uses):

```
                    scheduled (policy scan_interval)  or  operator on-demand
                                     │
                     signed  patch.scan  job ─────────────► agent per-OS scan
                                                                   │ PatchScanDocument
   patch catalog + device_patch_status  ◄── result_handler ◄───────┘

operator/policy approves patch × ring (patch_approval)
                                     │
        worker: for approved (device,patch) whose ring window is OPEN now
                                     │
                     signed  patch.apply  job ─────────────► agent per-OS apply
                                                                   │ PatchApplyResult
   device_patch_status + patch_run  ◄── result_handler ◄───────────┘  (+ reboot policy)
```

Control path rides the existing signed-job gateway. No core file changes; the
module registers job types, API routes, migrations, a worker sweep, and a
frontend folder.

## 3. Module layout

```
backend/app/modules/patch/
├── __init__.py      # register(app): job-type result handlers + API routes
├── models.py        # Patch, DevicePatchStatus, PatchPolicy, PatchApproval, PatchRun
├── schema.py        # PatchScanDocument, PatchApplyResult (agent result contracts) + params
├── ingest.py        # ingest_scan(session, device, doc): catalog + status upsert + compliance
├── apply.py         # apply-result ingestion; approval→ring→window gating; dispatch helpers
├── policy.py        # per-device-group patch policy resolution (ring, window, posture, reboot)
├── approve.py       # approval/revocation service (RBAC-gated, audited)
├── reporting.py     # per-device + fleet compliance rollups; evidence export
└── api.py           # /api/v1/patch routes
frontend/src/modules/patch/   # FleetPatchPage (compliance + catalog/approvals) + DevicePatchTab
backend/migrations/versions/0011_patch.py
docs/kernel/wire-protocol/PatchScanDocument.schema.json      # exported agent contract
docs/kernel/wire-protocol/PatchApplyResult.schema.json       # exported agent contract
```

Kernel extension points used (no new ones): `JobType(..., result_handler=)`
(as Inventory) and the Celery beat schedule (as security-log retention).

## 4. Data model (migration 0011 — all tables `org_id` + FORCE RLS)

- **`patches`** (catalog, per org — built from the fleet's scans):
  `id` (uuid7 pk), `org_id`, `os_family` (`windows|linux|macos`), `source`
  (`windows_update|winget|apt|dnf|zypper|softwareupdate|homebrew`), `identifier`
  (stable per source: KB id, `pkg`, `pkg@version`), `title`, `severity`
  (`critical|important|moderate|low|unknown`), `kb` (nullable), `cve` (text[],
  nullable), `superseded_by` (nullable FK→patches), `priority` (int, nullable —
  **Sentinel seam**), `first_seen_at`. Unique `(org_id, os_family, source, identifier)`.
- **`device_patch_status`**: `id`, `org_id`, `device_id` FK, `patch_id` FK,
  `state` (`missing|approved|installed|failed|not_applicable`), `detected_at`,
  `applied_at` (nullable), `last_error` (nullable text). Unique `(device_id, patch_id)`.
  Index `(org_id, state)`, `(org_id, patch_id)`.
- **`patch_policies`** (device-group-scoped, org-default fallback = `device_group_id NULL`):
  `id`, `org_id`, `device_group_id` FK (nullable), `ring_name` (text, default
  `"broad"`), `ring_order` (int, default 100 — lower applies first),
  `maintenance_window` (cron string, nullable = anytime),
  `auto_approve_security` (bool, default **false** — PD-02),
  `reboot` (`never|if_required|forced`, default `if_required`),
  `reboot_deferral_hours` (int, default 24),
  `scan_interval_hours` (int, default 24). Unique `(org_id, device_group_id)`.
- **`patch_approvals`**: `id`, `org_id`, `patch_id` FK, `ring_name` (which ring
  this authorizes), `approved_by` FK→users, `approved_at`, `revoked` (bool,
  default false), `revoked_by`/`revoked_at` (nullable). Unique
  `(org_id, patch_id, ring_name)` — an approval authorizes a patch for a ring.
- **`patch_runs`** (per apply-job execution record): `id`, `org_id`, `job_id`
  FK→jobs, `device_id` FK, `results` jsonb (per-patch outcome), `reboot_required`
  (bool), `started_at`, `finished_at` (nullable). Index `(org_id, device_id, id)`.

All five tables get the standard `tenant_isolation` RLS policy (ENABLE + FORCE);
the Phase-0 RLS-completeness test requires them. `device_patch_status`,
`patch_approvals`, and `patch_runs` are service-owned (no operator UPDATE/DELETE
endpoint); approvals are revoked (soft), never deleted, so the audit trail holds.

## 5. Result document contracts (`schema.py`, exported)

`PatchScanDocument` (result of `patch.scan`):
```jsonc
{
  "os_family": "windows|linux|macos",
  "patches": [ { "source": "...", "identifier": "...", "title": "...",
                 "severity": "critical|important|moderate|low|unknown",
                 "kb": "KB5034123"?, "cve": ["CVE-…"]?, "superseded_by": "…"? } ],
  "installed_identifiers": ["<source>:<identifier>", …],   // for not_applicable/installed reconciliation
  "scanned_at": "RFC3339"
}
```
`PatchApplyResult` (result of `patch.apply`):
```jsonc
{
  "results": [ { "source": "...", "identifier": "...",
                 "state": "installed|failed", "error": "…"? } ],
  "reboot_required": true,
  "applied_at": "RFC3339"
}
```
Agent job params (also exported): `PatchScanParams{}` (empty — scan is
self-describing); `PatchApplyParams{ patches: [{source, identifier}],
reboot: "never|if_required|forced", deferral_hours: int }`.

## 6. Ingestion (`ingest.py`) — runs in the job's tenant transaction

`ingest_scan(session, device, doc)`:
1. Upsert each `patches` row (unique on `(org_id, os_family, source, identifier)`);
   set/clear `superseded_by`, keep `priority` untouched (Sentinel owns it).
2. Reconcile `device_patch_status`: available → `missing` (unless an approval
   already moved it to `approved`); anything in `installed_identifiers` →
   `installed` (set `applied_at` if newly so); rows no longer reported and not
   installed → `not_applicable`.
3. **Auto-approve rule (PD-02):** if the device's resolved policy
   `auto_approve_security` is true, for each newly-`missing` **security-severity**
   (`critical`/`important`) patch, create a `patch_approval` for the **test**
   ring only, of the device's client (§7, amended 2026-10-29), and flip the
   device's status to `approved` — audited `patch.auto_approved`. A revoked
   approval of that patch and ring, global or the client's, stops it (§7).
4. Queue a UI event `patch_status_updated` (post-commit, via the kernel drain).

## 7. Approvals, rings & windows

- **Rings** come from a device's resolved `patch_policy` (`ring_name`,
  `ring_order`). A device is "in" its group's ring; the org-default policy
  covers ungrouped devices.
- **Approval** (`approve.py`, `patch.approve`): approving a `(patch, ring)`
  creates a `patch_approvals` row and flips every `missing` `device_patch_status`
  for devices in that ring to `approved`. Revoking sets `revoked` and flips
  `approved` (not yet applied) rows back to `missing`. Every approve/revoke is
  audited (`patch.approved` / `patch.revoked`) with actor + scope. **Staged
  rollout** = the operator approves ring by ring (test → pilot → broad); v1 does
  not auto-advance.
- **Windows:** `maintenance_window` (cron) on the policy. `patch.apply` is only
  dispatched when the device's window is open (`now` matches the cron and, for a
  window with a duration, within it). Resolved to the agent's local scheduler so
  the agent also honors it if the control plane is unreachable at window time.

> **Amended 2026-10-29 (MSP clients plan 2026-10-26, Task 14 and its review):
> an approval belongs to one client, or to every client.** `patch_approvals`
> is O: `POST /patch/approvals[/bulk]` takes an optional `client_id` (404 not
> visible, 409 archived, 422 Unassigned). With one, the approval is that
> client's and flips only that client's machines in the ring; without one it
> is global, covers every client's machines in the ring, and writing it needs
> every client. Every reader — the apply sweep, the ring's approved patches
> and the compliance report — accepts an approval that is global or of the
> device's own client, never another client's. Revoking a client's approval
> flips only that client's machines no other live approval still covers, and
> nothing while a global approval for the same patch and ring is live. The
> console files an approval under the internal client unless the person ticks
> **"For every client"**; one made from a machine's drill-down is filed under
> that machine's client. A client's approval is authorized only by a change of
> that client, **and a global one only by the internal client's change, whose
> automatic change it also is** (plan decision C8); a client without change
> management has no gate and gets no automatic change (decision D4,
> `docs/modules/change-mgmt/spec.md` §14). **Automatic approval (PD-02) is
> filed under the reporting device's client** (amended 2026-10-29, final
> review): the policy that asks for it is a policy of that client's group,
> and a global row would let one client's policy clear the patch for every
> client's test ring — a decision for every client that only a person with
> every client may take (invariant 5a). **A person's revocation wins:** a
> revoked approval of that patch and ring — the global one or the client's
> own — stops automatic approval for the client (no row, the device is not
> marked approved, nothing is audited); a revoked row of another client does
> not. A live approval of either owner already covers the client's machines,
> so nothing is inserted. Otherwise the client's row is inserted
> `ON CONFLICT DO NOTHING` on `uq_patch_approvals_client_patch_ring`, so two
> scans of the client's machines make one row, and the `patch.auto_approved`
> audit names the client. The plan is
> `docs/superpowers/plans/2026-10-26-msp-clientes-fase2-modulos.md`.

## 8. Apply flow (`apply.py`) — dispatch + result ingestion

- **Dispatch** (worker or operator, `patch.apply` permission): select
  `(device, patch)` where `device_patch_status.state == approved`, an un-revoked
  `patch_approval` covers the device's ring, and the device's window is open.
  Build a signed `patch.apply` job per device (batching that device's approved
  patches), carrying the resolved `reboot` policy + `deferral_hours`. Audit
  `patch.apply_dispatched`. Create a `patch_run` (job_id, device_id) on dispatch.
- **Result ingestion** (result_handler for `patch.apply`): update each
  `device_patch_status` to `installed` (set `applied_at`) or `failed` (set
  `last_error`); fill the `patch_run` `results` + `reboot_required` +
  `finished_at`; queue `patch_status_updated`. Audit `patch.applied` with the
  per-patch outcome summary.
- **Reboot:** the policy value rides in the job; see §8.1.

### 8.1 Restart orchestration (2026-09-23)

The review found the agent detected a pending restart and never performed one,
so Windows cumulative updates sat half-applied. Now:

- **In `patch.apply`** (agent, `result_version` 2 only): `never` reports and
  never restarts; `if_required` restarts when the OS asks; `forced` always.
  A signed-in user on a workstation gets `reboot_deferral_hours` of notice
  (at least 15 minutes, at most a week) through the OS's own restart
  notification (`shutdown /r /t` on Windows, `shutdown -r +minutes` on
  macOS); there is no user-session helper yet. A server, or a machine nobody
  is signed in to, restarts after five minutes, and only if that lands before
  `reboot_not_after`, the end of the window the apply was dispatched in.
  Linux machines count as servers. The result carries `reboot_scheduled_for`.
- **`patch_reboots`** (migration 0120) records every restart a device owes:
  from an apply that could not book one, or a scan reporting one pending.
  States: `pending` → `dispatched` → `scheduled` → `completed`, or
  `cancelled` / `failed` with a reason. One open row per device.
- **The restart sweep** (beat, 15 min) dispatches a signed `patch.reboot` for a
  pending row only while the device's maintenance window is open and its agent
  is connected. `only_if_required` is false for `forced`. A refused job
  (an agent without the capability) fails the row; other failures retry three
  times. Fifteen minutes after a booked time, one `patch.scan` confirms it:
  a `booted_at` after the row closes it as `completed`.
- **Windows are read in hour slots.** The week grid writes `0 2-5 * * 6` for
  "Saturday 02:00 to 06:00", but `window_open` matches the minute, so a
  15-minute sweep that did not land on :00 never saw the window open.
  `core/schedule.in_window` treats each fire time as the start of an hour;
  apply dispatch and the restart sweep use it.
- Every transition is audited: `patch.reboot_pending`, `_dispatched`,
  `_scheduled`, `_deferred`, `_verify_dispatched`, `_completed`,
  `_cancelled`, `_failed`.
- `GET /api/v1/patch/restarts` lists open restarts; `GET /devices/{id}/status`
  carries the latest one as `restart`, and `restart_window` (open now, when it
  next opens, the signed-in user's notice) so the console can say when a
  restart would happen before anyone books one.

### 8.2 A restart an operator asks for (2026-09-23)

`POST /api/v1/patch/devices/{id}/restart` (`patch.apply`) books a restart from
the device page. It writes a `patch_reboots` row with origin `operator`
(migration 0150) and audits `patch.restart_requested` as the user; it never
dispatches a job. The sweep sends it like any other, inside the window, with
the same notice, except that `only_if_required` is false and a `never` policy
does not cancel it (that policy governs restarts patching causes). A scan
reporting no restart pending cancels only `scan` rows, so it leaves this one
alone. Refused with 409 when the device has no agent, its certificate is
revoked, or its agent does not advertise `patch.reboot`. Asking again while a
restart is open records nothing; a `pending` row from patching becomes the
operator's, so a later scan cannot cancel it.

## 9. Compliance reporting (`reporting.py`) — SOC 2 evidence

- **Per-device:** counts by state (missing/approved/installed/failed/n-a),
  broken down by severity; last scan time; pending reboots.
- **Fleet:** % compliant (no missing security patches) by OS family, by ring,
  by severity; oldest unpatched security patch age; a list of the most-exposed
  devices (ordered by `priority` when Sentinel has set it, else severity).
- **Evidence export:** `GET /api/v1/patch/report/export` streams a point-in-time
  CSV/JSON of device × patch status (audited `patch.report_exported`) — mirrors
  the security-logs/inventory export pattern.

## 10. Agent capability wire contracts

> **IMPLEMENTED.** `agent/internal/capabilities/patch/`. This section is the
> contract; **`agent-capabilities.md` in this directory is what was actually
> built** — including three things this section does not say and a reader must
> not assume. (a) Windows uses the Windows Update Agent **COM API in process**
> rather than PowerShell, because a SYSTEM process spawning PowerShell trips ASR
> rules; that engine has **never executed** — there is no Windows runner here.
> (b) The agent **detects** a required reboot and does **not perform** one:
> `PatchApplyResult` has no member that could tell the control plane it had, so
> the "honor the reboot policy" clause below was only half-implemented until
> §8.1 closed it. (c) `dnf` and `zypper` are **not implemented**; winget and
> Homebrew casks are, as app sources (§10.1).

Two dormant capabilities (heavy work only during a scan/window):
- `patch.scan` — enumerate missing/available OS + native-manager updates; return
  `PatchScanDocument`. Per-OS: Windows Update Agent API + `winget`; `apt`/`dnf`/
  `zypper`; `softwareupdate` + Homebrew.
- `patch.apply` — apply the requested `patches` via the per-OS engine, stream
  progress (`job_log`), honor the reboot policy, return `PatchApplyResult`.
The result/param JSON Schemas are exported to `docs/kernel/wire-protocol/` via
`scripts/export_wire_schema.py`.

### 10.1 Third-party apps (2026-09-23)

winget (Windows) and Homebrew casks (macOS) are app sources beside the OS
engine. Their updates are ordinary catalogue rows with `source` `winget` or
`homebrew` and the package id as identifier, approved into rings exactly like
OS updates: only a package id approved for the device's ring is ever sent in
`patch.apply`, and the agent upgrades it only if the manager listed it in the
same job. Because the id does not carry a version, an `installed` app offered
again is `missing` again, and the standing approval dispatches the newer
version. `PatchScanDocument.app_sources` says per manager whether it was
scanned, unsupported (with the reason, e.g. no brew, or a root-owned prefix) or
failed. `GET /patch/updates?kind=apps|os` splits the list;
`GET /patch/app-sources?os_family=` counts the managers' states over each
device's latest scan. The console shows them as "Apps" on the Windows and
macOS tabs. See `agent-capabilities.md` §2 for what has never executed.

## 11. RBAC (migration 0011 seeds)

- `patch.read` — view catalog, per-device status, reports/exports. Granted:
  super-admin, org-admin, technician, auditor, read-only.
- `patch.scan` — trigger an on-demand scan. Granted: super-admin, org-admin,
  technician.
- `patch.approve` — approve/revoke a patch for a ring (**privileged** — gates
  what applies). Granted: super-admin, org-admin.
- `patch.apply` — dispatch an apply within approved scope + window. Granted:
  super-admin, org-admin, technician.
- `patch.policy.manage` — edit `patch_policies` (rings/windows/posture/reboot).
  Granted: super-admin, org-admin.

Every route re-checks server-side; device scope enforced as `jobs.dispatch`.
Apply can only ever touch `(device, patch)` pairs already `approved` — approval
is the security gate, checked server-side at dispatch, independent of who
dispatches.

## 12. API (`api.py`, prefix `/api/v1/patch`)

- `GET /patches` (`patch.read`) — catalog with filters (os_family, severity,
  state, ring); paginated.
- `GET /devices/{device_id}/status` (`patch.read`) — per-device patch status,
  including `last_scan_at` (see `GET /report` below).
- `POST /scan` (`patch.scan`) — body `{device_ids?}`; dispatch `patch.scan` (all
  in scope, or the given devices).
- `GET /approvals` (`patch.read`, **not** `patch.approve` — listing is read
  work) — this org's approvals, newest first, `{items, total}`; filters
  `patch_id`, `ring`, `revoked`, `limit`, `offset`. Revoked approvals are
  included by default, because §5 soft-revokes them precisely so the trail
  holds. Added 2026-08-21: without it `DELETE /approvals/{id}` was unreachable
  over HTTP, since an approval id was only ever returned by the POST that
  created it, so an approval made in an earlier session could not be revoked
  at all.
- `POST /approvals` (`patch.approve`) — body `{patch_id, ring_name}`; create
  approval + flip statuses. `DELETE /approvals/{id}` (`patch.approve`) — revoke.
- `POST /approvals/bulk` (`patch.approve`) — body `{patch_ids[], ring_name}`;
  clear a SELECTION of patches for one ring in one decision. Answers
  `{items, total, newly_approved, already_in_force}`, `items` in request order.
  Added 2026-08-24 on the operator's report that approving a scan's output "one
  by one" was unworkable. **The model was never the constraint** — a
  `patch_approvals` row is `(org_id, patch_id, ring_name)` with no device
  column, so one approval has always covered a whole ring; the console was
  spending one POST and one confirmation dialog per catalogue row. Three
  properties are load-bearing and each has a test: ONE `patch.approved` audit
  event per approval (never one for the batch, or the log stops answering "who
  cleared this patch"); an already-in-force approval is a no-op that writes no
  event, which is why the response separates `newly_approved` from
  `already_in_force`; and a `patch_id` from another org refuses the WHOLE batch
  with 404 rather than applying the rest. `patch_ids` is capped at 500, the
  ceiling `GET /patches` puts on a page.
- `GET /rings` (`patch.read`) — every deployment ring this org has, in rollout
  order, with `device_count` each and `device_total` for the fleet:
  `{items, total, device_total}`. **The blast radius of an approval, before the
  click.** Clearing a patch for a ring clears it for every device that resolves
  into that ring now AND at every future scan, so the ring's population is the
  reach of the decision itself. Not derivable in a browser — it needs
  `patch_policies` joined to `devices` through `device_groups` with the
  org-default fallback — so it is computed by `resolve_ring_populations`, the
  same resolver `approve_patch` uses to decide which statuses to flip; the
  number shown and the machines flipped cannot disagree. The rings PARTITION
  the fleet (a device in no group, or in a group naming no policy, lands in the
  org-default ring), so `sum(device_count) == device_total`. A ring named by a
  policy with no devices in it is listed at zero rather than hidden, because a
  ring an operator can approve into and cannot see is one they will approve
  into by accident.
- `POST /apply` (`patch.apply`) — body `{device_ids?, ring_name?}`; dispatch the
  approved patches for the in-scope devices whose maintenance window is currently
  open. The window is always honored (no override) — a device outside its window
  is skipped and reported as such. **Note:** each device's apply ring is resolved
  server-side from its policy (`dispatch_apply`), not from the request body; the
  device's approval for its assigned ring is the gate that determines whether
  patches dispatch. Returns the dispatched job ids.
- `GET|PUT /policies` (`patch.read` / `patch.policy.manage`) — per-group policy;
  `PUT` upserts a single org-default (device_group_id null) like remote-control.
- `GET /report` (`patch.read`) — fleet compliance rollup, plus `last_scan_at`
  / `devices_scanned` / `devices_total`. **A zero is not a measurement.** An
  empty `exposures` list is the same shape on a clean fleet and on one that
  has never been scanned; these three say which. `last_scan_at` is derived
  from the `jobs` table (`patch.scan`, `succeeded`, `MAX(finished_at)`) — no
  new column, because a succeeded scan job already IS the durable per-device
  record that a scan ran and its document was ingested. It is a per-device
  fact first: the fleet value is a maximum, and never travels without its
  coverage counts, since one scanned machine in five hundred would otherwise
  date the whole report as freshly measured.
- `GET /report/export` (`patch.read`) — point-in-time evidence export (audited).

## 13. Worker (`app/worker.py` additions)

- **Scan sweep** (beat, hourly check): for each org, dispatch `patch.scan` to
  devices whose last scan is older than their policy `scan_interval_hours`.
- **Window apply sweep** (beat, every ~15 min): for each org, dispatch
  `patch.apply` for approved `(device, patch)` pairs whose ring window is open
  now. Both mirror the existing `sweep_all_orgs`/`retention_all_orgs` shape
  (async fn, engine-injected, tested directly; thin Celery shell).

## 14. Frontend (`frontend/src/modules/patch/`)

- **FleetPatchPage** — compliance dashboard (compliant % by OS/severity, exposed
  devices) + patch catalog table with **Approve for ring** actions (gated on
  `patch.approve`) and a Scan-now button; nav item gated on `patch.read`.
- **DevicePatchTab** — per-device patch status + pending reboots (a tab on the
  existing device detail page).
- **PatchPolicyEditor** — rings/windows/posture/reboot per group
  (`patch.policy.manage`). TanStack Query; strict TS; no `any`; server-side RBAC
  is authoritative (nav gating is cosmetic).

## 15. Security invariants preserved

1. **Signed jobs only:** `patch.apply` is a signed job the agent verifies; no
   unsigned apply path. Approval is a server-side gate at dispatch — an
   unapproved `(device, patch)` can never be dispatched, regardless of caller.
2. **RBAC server-side:** approve is privileged (org-admin+); every route + the
   dispatch path re-check permission and device scope.
3. **Immutable audit:** scan-ingest auto-approvals, manual approvals/revocations,
   apply dispatch, apply results, policy changes, and report exports are all
   HMAC-authenticated audit events (invariant #4).
4. **Tenant-scoped:** all five tables are `org_id` + FORCE RLS; the catalog,
   status, approvals, and runs are per-tenant; every query runs inside
   `org_session`.
5. **No credential/secret exposure:** patch apply uses the agent's local
   privilege (SYSTEM/root); no vault credential flows through this module.

## 16. Testing strategy (test-first)

- **ingest:** scan doc → catalog upsert, status reconciliation (missing/
  installed/not_applicable), supersedence; auto-approve-security-into-test rule
  fires only when policy enables it and only for security severity + test ring.
- **approve:** approving a `(patch, ring)` flips in-ring devices to `approved`;
  revoke flips back; both audited; cross-ring isolation.
- **apply gating:** dispatch selects only approved + window-open pairs; an
  unapproved or wrong-ring pair is never dispatched (explicit negative test);
  reboot policy threaded into the job; `patch_run` created.
- **apply result:** result handler sets installed/failed + last_error, fills the
  run, honors reboot_required, audits.
- **policy resolution:** device-group → org-default → defaults (robust to
  duplicate NULL org-default rows, `order_by(id).limit(1)` as Phase 3).
- **worker:** scan sweep respects scan_interval; window sweep dispatches only in
  open windows (inject the clock) — tested directly.
- **API + RBAC + RLS:** permission matrix per route (approve requires
  `patch.approve`; read open to auditor/read-only); cross-tenant isolation;
  migration 0011 RLS-completeness (auto).
- **frontend:** tsc strict + vitest for the compliance view + approve action;
  build.

## 17. Deploy

No new infrastructure. The scan/window beat entries are added to the existing
Celery `beat_schedule`. `deploy/.env.example` documents the default scan cadence
and that patch **enforcement** on macOS Apple Silicon needs the MDM fast-follow.
