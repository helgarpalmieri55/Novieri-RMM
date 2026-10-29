# Spec — Application Deployment (Phase 5)

> Full implementation spec, expanded from `docs/modules/app-deploy.md` via the
> brainstorming skill. Depends on Inventory (Phase 1) and reuses the per-OS
> dispatch/window/result plumbing built in Patch Management (Phase 4). Registers
> into the kernel via two signed job types: `app.deploy` and `script.run`.
> Date: 2026-07-20.

## 1. Purpose & scope

Push software to endpoints from the console — **install / update / uninstall**
applications, and run **signed custom scripts** — targeted at a device or a
(dynamic) group, across Windows, Linux, and macOS. This is the PDQ Deploy /
Intune-app-style capability. Inventory supplies current-state detection so we
never redundantly install; every deployment and script run is a signed,
audited kernel job.

**In scope for v1:** generic package registry (bring-your-own installer +
detection/uninstall rules), deployment jobs (install/update/uninstall) to
device or group, per-OS install engines (agent-side, defined by a shared wire
contract), maintenance-window scheduling reusing Phase 4's cron matcher,
per-device result reporting with logs and retry, and `script.run` as a
first-class signed job type.

**Explicitly out of scope for v1 (later phases):** self-service end-user
portal; multi-package dependency/sequencing; license-aware deployment; fully
built site-server LAN caching (a seam only — see §6); change-management gating
of deployments (Phase 9).

## 2. Design decisions (resolved in brainstorming)

- **AD-01 · Generic package model.** A package is a bring-your-own installer
  (binary in object storage) plus declarative `detection_rule` and
  `uninstall_rule` JSON. No curated catalog in v1; the generic model *is* the
  product. (Curated catalog = later phase.)
- **AD-02 · Signed script artifacts.** A script is a versioned, RBAC-gated
  object. The script **body travels inside the Ed25519-signed job envelope**,
  so the agent verifies it before running as SYSTEM/root. There is no inline or
  ad-hoc unsigned script path. Authoring requires `deploy.script.manage`;
  execution requires `deploy.script.run`; every run is audited with the script
  version and actor.
- **AD-03 · Shared per-OS engine wire contract.** Both `patch.apply` and
  `app.deploy`/`script.run` results conform to a common `InstallOutcome`
  contract (§5). Deployment reuses Phase 4's dispatch → window → result-ingestion
  shape. Build once, reuse.
- **AD-04 · RBAC-gated, operator-initiated (no approval workflow).** Deployment
  is a direct operator action gated by RBAC (`deploy.create` / `deploy.execute`),
  not by an approval-ring workflow (that is patch's security gate). Change-ticket
  gating is Phase 9.
- **AD-05 · Reuse Phase 4 windows.** Scheduling reuses the cron `window_open`
  matcher and a per-target deployment window; "deploy now" bypasses the window.
- **AD-06 · Inventory-driven detection.** `detection_rule` is evaluated
  server-side against inventory before dispatch and re-checked agent-side; a
  device already in the desired state is skipped and reported `not_applicable`.
- **AD-07 · Object storage + sha256.** Binaries live in object storage (MinIO,
  already wired via `ObjectStore`) referenced by `artifact_ref`; the agent
  verifies the artifact `sha256` before executing. Site-server LAN caching is a
  seam that matures in Phase 7.
- **AD-08 · Data model.** `package`, `deployment`, `deployment_result` (from the
  brief) plus `script` + `script_run` for the `script.run` job type. All
  `org_id`-scoped with RLS `ENABLE + FORCE`; new `deploy.*` RBAC permissions
  seeded.

## 3. Reused kernel/Phase-4 seams (do not fork)

- `window_open(cron: str | None, now: datetime) -> bool` and the cron field
  parser — lift into a shared location (see §13 note) so patch and deploy share
  one implementation rather than copying.
- `JobType(name, params_model, result_handler=…)` + per-module
  `register_job_types()` registered in **all three** processes: operator app
  (`register_all`), gateway, and Celery worker (`register_worker_job_types`).
  (Phase 4's whole-branch review caught a missing worker registration — this
  module MUST register in the worker too, with the same un-masking test.)
- `ObjectStore` for artifacts (extend with `put_package_artifact` /
  `get_package_artifact`, mirroring `put_job_artifact`).
- Device-group → org-default policy resolution pattern (Phase 4's
  `resolve_patch_policy`), if a deployment-window policy is used.
- The signed-job engine (`signed_envelope` + `publish_job`) — the ONLY dispatch
  path. Result handlers run inside the job's tenant transaction.
- Post-commit UI events via `queue_ui_event` (no live-event subscription on the
  frontend — refetch after mutation, per the Phase-3/4 lesson).

## 4. Data model (§4)

Five tables, all with `org_id UUID NOT NULL`, RLS `ENABLE + FORCE` +
`tenant_isolation` policy using `current_setting('app.current_org', true)::uuid`,
and `created_at`/`updated_at` timestamps.

- **`packages`** — `id`, `org_id`, `name`, `version`, `os_family`
  (windows|linux|macos), `installer_type` (msi|exe|winget|choco|pkg|dmg|deb|rpm|
  script), `artifact_ref` (object-storage ref, nullable for manager-sourced like
  winget/choco/brew), `artifact_sha256` (nullable when no artifact),
  `detection_rule` JSONB, `uninstall_rule` JSONB, `install_args` (text, nullable).
  UNIQUE `(org_id, name, version, os_family)`.
- **`deployments`** — `id`, `org_id`, `package_id` FK, `action`
  (install|update|uninstall), `target_type` (device|group), `target_id`,
  `window_cron` (nullable = deploy now), `requested_by` (user id), `state`
  (pending|dispatching|complete|failed), `created_at`. Index `(org_id, state)`.
- **`deployment_results`** — `id`, `org_id`, `deployment_id` FK, `device_id`,
  `job_id` (nullable until dispatched), `state`
  (pending|running|installed|failed|not_applicable), `exit_code` (nullable),
  `log_ref` (object-storage ref for the install log, nullable), `detected_after`
  (bool, nullable), `finished_at` (nullable). UNIQUE `(deployment_id, device_id)`.
  Index `(org_id, state)`.
- **`scripts`** — `id`, `org_id`, `name`, `os_family`, `interpreter`
  (powershell|bash|python), `body` (text — the script source; travels inside the
  signed job payload at dispatch), `version` (int, bumped on edit), `created_by`.
  UNIQUE `(org_id, name, version)`.
- **`script_runs`** — `id`, `org_id`, `script_id` FK, `script_version` (int
  snapshot), `target_type`, `target_id`, `device_id`, `job_id` (nullable),
  `state` (pending|running|succeeded|failed), `exit_code` (nullable), `log_ref`
  (nullable), `requested_by`, `finished_at`. UNIQUE `(script_id, device_id,
  requested_by, created_at)`-style guard as appropriate. Index `(org_id, state)`.

Migration: additive Alembic revision after Phase 4's head, with the `_rls`
helper and the `deploy.*` permission seed + role grants.

> **Amended 2026-10-29 (MSP clients plan 2026-10-26, Task 9): a deployment
> belongs to its target's client** (migration 0239). `deployments` gains
> `client_id` (C) and the client policy of `CLAUDE.md` invariant 5a;
> `deployment_results` and `script_runs` already carried their device's
> client, and `packages` and `scripts` stay the organisation's. **The client
> is written by a trigger, `its_deployment_client`, from `target_type` and
> `target_id`** (existing rows took their target's, or the internal client's
> when the target was gone): a target the session cannot see, or a client
> that contradicts the target's, is refused by name. The request body names
> no client. `POST /deploy/deployments` resolves the device or group in the
> caller's own scope first, so another client's target is the 404 a missing
> one is; a target of an archived client is a 409, and one in Unassigned a
> 422 (plan decision C10). The deployment's client then decides its gates,
> its automatic change (`docs/modules/change-mgmt/spec.md` §14) and every
> audit event, and only a change of that client authorizes it. Dispatch,
> which the window sweep runs under the every-client scope, reaches only
> devices of the deployment's client, named or members of the group. The
> plan is `docs/superpowers/plans/2026-10-26-msp-clientes-fase2-modulos.md`.

## 5. Wire contracts & the shared install-engine interface (§5)

Pydantic v2 models with `ConfigDict(extra="forbid")`, exported to
`docs/kernel/wire-protocol/` via `scripts/export_wire_schema.py`.

- **`InstallOutcome`** (the shared contract AD-03) — `identifier` (str: package
  name+version or script name), `state` (installed|failed|not_applicable|
  succeeded), `exit_code` (int | None), `log` (str, truncated; full log uploaded
  as an artifact and referenced by `log_ref`), `detected_after` (bool | None:
  did the detection rule match after the operation). `patch.apply`'s result
  (`PatchApplyOutcome`) is refactored to be expressible as / aligned with this
  contract so both modules ingest results the same way.
- **`AppDeployParams`** (job params for `app.deploy`) — `action`, `installer_type`,
  `artifact_ref`, `artifact_sha256`, `package_identifier`, `install_args`,
  `detection_rule`, `uninstall_rule`, `deferral_hours`. See §5.2 for the two
  members whose meaning is not obvious from their names.
- **`AppDeployResult`** — `list[InstallOutcome]` (one device, one package →
  usually one outcome).
- **`ScriptRunParams`** (job params for `script.run`) — `interpreter`,
  `body` (the signed script source), `script_id`, `script_version`, `timeout_s`.
- **`ScriptRunResult`** — a single `InstallOutcome` (state succeeded|failed,
  exit_code, log/log_ref).

### 5.2 `artifact_ref` is a presigned URL, and `package_identifier` is additive

Both of these are server-side gaps the agent capability found while being built
and refused to work around. Recorded here because neither is visible from the
JSON schema alone.

**`artifact_ref` is a presigned `https://` URL, not the `s3://` reference on the
`packages` row.** `s3://` is a server-side addressing scheme: the agent has no
route that resolves one, and giving it one would mean an object-store credential
resident on every endpoint in the fleet — invariant #3 inverted, with one
compromised endpoint reaching every other tenant's artifacts. The control plane
resolves the reference at dispatch time instead
(`ObjectStore.presign_package_artifact`) and puts the URL in the SIGNED params,
covered by the same Ed25519 signature as the hash. The download is still not the
authorization: the agent's `stage` produces an executable path only when the
fetched bytes hash to `artifact_sha256`.

Four properties, each load-bearing:

- **Exactly one object.** The object key is DERIVED from `org_id` + `package_id`;
  the stored `artifact_ref` is only CHECKED against it. A `packages` row pointing
  at another tenant's object, at a prefix, or at another bucket yields no URL
  rather than a signed grant over whatever it names.
- **The lifetime is the job's, to the second.** The URL is minted for
  `job_engine.default_ttl_s` (`ITS_JOB_DEFAULT_TTL_S`) and the job is created
  with that same `ttl_s`, so the two windows are one window by construction
  rather than two settings agreeing. Longer is a standing grant on a job the
  agent already refuses as stale — `expire_due` sweeps it and
  `queued_for_device` will not hand it out — so every extra second is bearer
  credential with nothing to spend it on. Shorter 403s a download inside a job
  that is still legitimately runnable. A slow transfer is not a reason to widen
  it: SigV4 query expiry is evaluated when the request is RECEIVED, not for the
  duration of the transfer, so a download starting one second before expiry
  completes however long it takes (the agent caps it at 30 minutes itself).
- **Retries get a fresh grant.** `resign_for_retry` hands each attempt a whole
  new window and the first retry is at least `STUCK_DISPATCH_AFTER` (10 min)
  after dispatch, so re-signing the original params would re-sign a dead URL and
  burn attempts 2 and 3 of `ITS_JOB_MAX_ATTEMPTS` on an HTTP 403 no operator
  could diagnose from the endpoint. `JobType.params_refresher` (kernel extension
  point, symmetric with `result_handler`) mints a new one AFTER the new
  `expires_at` is written, so it is sized to the window that attempt gets. A
  refresher never raises for a business reason — it runs inside the org's
  job-expiry sweep, where raising costs that org every other job in the cycle —
  so a deleted package yields `artifact_ref: null`, which the agent refuses on
  arrival: terminal and legible.
- **It is a bearer credential and is treated as one.** Never logged (the dispatch
  log line names ids and the installer type, never the params dict), never
  written to audit metadata, and replaced wholesale by
  `JobType.params_redactor` on every API read path — `jobs.read` is a reporting
  permission, far weaker than the `deploy.execute` that authorized the dispatch.
  It is replaced rather than truncated: a "safe prefix" of a signed URL is still
  the endpoint, the bucket, the tenant id and the object id.

**`package_identifier` is additive and optional, and that is the whole
compatibility argument.** `winget`/`choco` install by identifier and the params
carried no field for one, so the agent read `detection_rule.name` and refused
when that was unusable — deliberately, rather than parsing `install_args` or
guessing, because the string it picks becomes an argument to a process running
as SYSTEM. Compatibility in both directions, since a fleet is always
mixed-version:

- **New control plane → old agent.** Go's `encoding/json` drops the unknown
  member, and the agent goes on deriving the identifier from
  `detection_rule.name` — which is exactly what `manager_package_identifier`
  falls back to when `packages.package_identifier` (migration 0052) is unset. So
  both sides compute the same string and a catalog nobody has revisited behaves
  identically before and after.
- **New agent → old control plane.** The member is absent, which is `null`,
  which is the state the agent already handles.
- A REQUIRED field would have been the outage: an old agent neither sends nor
  understands it.

`packages.package_identifier` is deliberately a THIRD column rather than a reuse
of `name` or `detection_rule.name`. `name` is the display name an operator reads
("Google Chrome"); the winget id is `Google.Chrome` and the choco id is
`googlechrome`. `detection_rule.name` is matched against installed-software
display names by `detect.py`, so it answers "is it here?", not "what do I ask the
manager for?". They coincide often enough to look interchangeable and differ
often enough that treating them as one installs the wrong package under a
plausible-looking argument.

**The partition cannot be violated from the server side.** `build_deploy_params`
is the single place both shapes are assembled: an artifact-backed job carries
`artifact_ref` + `artifact_sha256` and NO `package_identifier`; a manager-sourced
job carries `package_identifier` and NO artifact. The agent refuses either
mismatch outright and re-checks in `stage` rather than trusting its caller, so a
server that emitted one would produce a fleet-wide refusal.

The **detection rule** grammar (v1, minimal & declarative — no arbitrary code):
`{ "type": "file_exists" | "registry_value" | "package_installed" |
"command_exit_zero", ... }` evaluated against inventory server-side and
re-checked agent-side. Keep the grammar small and validated; document each type.

## 6. Package registry & artifact storage (§6)

- Upload flow: operator uploads an installer → server stores it via
  `ObjectStore.put_package_artifact(org_id, package_id, bytes)` → records
  `artifact_ref` + computes `artifact_sha256`. Manager-sourced installers
  (winget/choco/brew) have no artifact — `artifact_ref` NULL, agent resolves from
  the manager.
- Integrity: the agent verifies `artifact_sha256` after pull, before execution.
  A mismatch fails the job (never executes).
- Delivery to the endpoint: `AppDeployParams.artifact_ref` carries a presigned
  `https://` URL, NOT the `s3://` ref recorded on the row — see §5.2. The agent
  holds no object-store credential and must not.
- Site-server cache: the agent prefers a site-server cache when present, else the
  presigned URL. The cache negotiation is a documented seam, not fully
  implemented in v1.

## 7. Deployment targeting & scheduling (§7)

- Targeting: a deployment targets a single device or a group; group membership
  is resolved to a device set at dispatch time (dynamic groups re-resolve each
  window). Server-side detection (§6/AD-06) filters out devices already in the
  desired state.
- Scheduling: `window_cron` NULL = deploy now; otherwise the worker's
  deploy-sweep dispatches when `window_open(window_cron, now)` is true. User
  notification/deferral honored where the OS engine supports it
  (`deferral_hours`).

  > **Amended 2026-10-29 (MSP clients phase 2, final review B2).** The create
  > reads `window_cron` with `app.core.schedule.checked_window`, the check a
  > backup policy's window passes: five readable fields that can match, at
  > most 120 characters, blank stored as NULL; anything else is a 422 and
  > nothing is written (the MCP tool runs the same route). A value the parser
  > cannot read that is already stored is contained to its own deployment:
  > the window sweep runs each deployment in a savepoint and logs and skips
  > it, so it no longer rolls back every windowed deployment of the
  > organisation, and `POST .../execute` on it is a 422 that says so. No route
  > edits a deployment: the way out is a new deployment with a valid window.

## 8. Dispatch & result ingestion (§8)

- `dispatch_deployment(session, job_engine, redis, *, org_id, deployment_id,
  now, actor_id, actor_type)` — resolves targets, runs detection, builds+signs
  one `app.deploy` job per eligible device, creates `deployment_results` rows,
  audits `deploy.dispatched` with the real actor (operator via API =
  `actor_type="user"`, worker sweep = `"system"`). Mirrors Phase 4's
  `dispatch_apply` including the explicit `org_id` predicates and the
  human-vs-system actor threading (both were whole-branch-review findings in
  Phase 4 — get them right the first time here).
- `ingest_deploy_result(session, job, result: AppDeployResult)` — updates the
  `deployment_result` row (state/exit_code/log_ref/detected_after/finished_at),
  rolls the parent `deployment.state` when all devices report, audits
  `deploy.result` (`actor_type="system"`). Registered as the `app.deploy`
  result handler. Every query carries an explicit `org_id` predicate.
- `ingest_script_result(session, job, result: ScriptRunResult)` — analogous for
  `script_runs`, audits `script.result`.

## 9. Scripts — `script.run` (§9, AD-02)

- Authoring (`deploy.script.manage`): create/edit a `script` row; editing bumps
  `version`. Audited `script.authored`.
- Execution (`deploy.script.run`): dispatch a `script.run` job whose signed
  params include the exact `body` + `script_version` snapshot. The agent
  verifies the job signature (covering the body) before running under the
  interpreter as SYSTEM/root. Audited `script.dispatched` with actor + version.
- No unsigned/inline path exists. The body is immutable for a given
  `(script_id, version)`; a re-dispatch of the same version is byte-identical.

### 9.1 Scheduled scripts (2026-09-23, migration 0121)

Scripts used to run on demand only. A `script_policies` row names a script, a
device group and an interval (`interval_hours`, shown as "every hour / 4 hours /
day / week"; no schedule expression reaches the console).

- **Dispatch.** A beat sweep (15 min) runs the ordinary signed `script.run`,
  with the script's current body and version, on each device in the group
  whose agent is online, whose platform matches the script, whose last run for
  the policy is older than the interval (a run still `running` after two hours
  counts as lost), and, with `in_maintenance_window`, whose patch maintenance
  window is open (`core/schedule.in_window`). `script_runs.policy_id` records
  the schedule; `requested_by` is NULL and the audit event
  `script_policy.dispatched` is system-attributed.
- **Output.** `script_runs.output` keeps what the script printed (64 KiB cap,
  on every run). For scheduled runs, output older than the newest `keep_runs`
  per device is cleared; the run rows stay.
- **Alert on failure.** With `alert_on_failure`, a failed run (state `failed`
  or a non-zero exit) opens a health alert of the server-evaluated check type
  `script.policy_failed`, selector = the schedule name, through the device's
  health profile, which decides severity and whether `open_ticket` fires; the
  next successful run clears it (`recovery`). A profile with no such check
  routes nothing, and `script_policy.alert_unrouted` records why.
- **RBAC.** `deploy.read` lists schedules and runs; `deploy.script.schedule`
  (super-admin, org-admin) creates, edits, pauses and deletes them. Every
  change is audited (`script_policy.created/updated/deleted`). Deleting a
  schedule keeps its runs as runs of the script.
- **Routes.** `GET|POST /deploy/script-policies`,
  `PATCH|DELETE /deploy/script-policies/{id}`,
  `GET /deploy/script-policies/{id}/runs`. The console shows them as
  "Schedules" on the Scripts page.

## 10. Reporting (§10)

- `deployment_report(session, org_id, deployment_id) -> dict` — per-device
  states + rollup (installed/failed/not_applicable counts), log refs.
- `fleet_deploy_report(session, org_id) -> dict` — recent deployments with
  success rates; script-run history.
- Read-only aggregation, every query org-scoped; no writes.

## 11. RBAC (§11)

New permissions (seeded in the migration + `app/core/rbac.py`):

- `deploy.read` — view packages, deployments, results, scripts, script runs.
- `deploy.package.manage` — create/edit/delete packages + upload artifacts.
- `deploy.create` — create a deployment (targeting + scheduling).
- `deploy.execute` — dispatch/execute a deployment now.
- `deploy.script.manage` — author/edit scripts.
- `deploy.script.run` — execute a script.

Role grants (mirroring Phase 4's shape): super-admin + org-admin → all;
technician → read + create + execute + script.run (NOT package.manage /
script.manage); auditor → read only; read-only → read only. Adjust against the
existing role matrix in `app/core/rbac.py`.

## 12. API (§12)

Routes under `/api/v1/deploy`, each RBAC-gated server-side; Pydantic response
models, no secret fields:

- `GET /packages` (`deploy.read`), `POST /packages` (`deploy.package.manage`),
  `POST /packages/{id}/artifact` (upload, `deploy.package.manage`),
  `DELETE /packages/{id}` (`deploy.package.manage`).
- `GET /deployments` (`deploy.read`), `POST /deployments`
  (`deploy.create`), `POST /deployments/{id}/execute` (`deploy.execute`,
  computes `now=datetime.now(UTC)` in the handler),
  `GET /deployments/{id}` (`deploy.read`, includes per-device results).
- `GET /scripts` (`deploy.read`), `POST /scripts` (`deploy.script.manage`),
  `GET /scripts/{id}` (`deploy.read`, returns full script including body),
  `PUT /scripts/{id}` (`deploy.script.manage`, bumps version),
  `POST /scripts/{id}/run` (`deploy.script.run`).
- `GET /report` / `GET /devices/{device_id}/deployments` (`deploy.read`).

`register(app)` calls `register_job_types()` then mounts the router. `org_id`
and `actor_id` come from the `AuthContext`, never the request body.

## 13. Worker (§13)

- `deploy_window_sweep_all_orgs(now)` — per org, dispatch scheduled deployments
  whose window is open, via `dispatch_deployment(... actor_type="system")`.
  Beat entry `deploy-window-sweep` @ 900s, task `app.worker.deploy_window_sweep`.
- `register_worker_job_types()` MUST also register the deploy job types (extend
  the Phase-4 worker registration; add the un-masking test that drives
  `create_celery()`).
- **Refactor note:** move `window_open` + the cron parser out of
  `app/modules/patch/apply.py` into a shared module (e.g.
  `app/core/schedule.py`) and have both patch and deploy import it — one
  implementation, per AD-03/AD-05. This is a targeted, in-scope improvement.

## 14. Frontend (§14)

Feature folder `frontend/src/modules/deploy/`: `PackagesPage` (registry +
upload, `deploy.package.manage`-gated actions), `DeployPage` (create/target/
schedule a deployment, `deploy.create`/`deploy.execute` gates), `ScriptsPage`
(author + run, gated), `DeviceDeployTab` (per-device deploy history in the
device-detail page). ApiClient methods + module hooks with query-key
invalidation after mutations. Strict TS, no `any`. Nav gated on
`deploy.read`; backend RBAC authoritative.

## 15. Security invariants (§15)

1. **Signed jobs only** — `app.deploy` and `script.run` dispatch exclusively via
   the kernel job engine; the script body and installer coordinates ride inside
   the signed envelope. No unsigned path.
2. Agents outbound-only mTLS (kernel; unchanged).
3. No stored credentials introduced by this module (installers are artifacts,
   not secrets); if a package ever needs a credential it uses the vault at point
   of use — out of v1 scope.
4. **Audit** every privileged action: package create/delete, artifact upload,
   deployment create/dispatch/result, script author/dispatch/result — with
   `actor_type` user-vs-system correct (operator actions carry the real user id).
5. **Tenancy** — every query over the five tables + Device carries an explicit
   `org_id` predicate in addition to RLS `ENABLE+FORCE`.
6. **RBAC** — server-side on every route and every dispatch; the `deploy.*`
   matrix seeded and matrix-tested.

## 16. Testing (§16)

Test-first throughout. Security-relevant paths get explicit tests: signed-job
dispatch (no unsigned path), the script body is carried in the signed params,
RBAC matrix (negative + positive per route), tenancy (cross-org rows never
appear), detection-rule server-side skip (`not_applicable`), window gating
(closed window dispatches nothing), result ingestion state transitions, actor
attribution (API=user, sweep=system), and the worker-registration un-masking
test. Frontend: vitest render + RBAC-gating hidden-without-permission + mutation
invalidation.

## 17. Deploy (§17)

Docs-only unless a config knob is needed (prefer none — windows live in the
deployment row/policy). Document the package artifact storage, the sha256
integrity requirement, the site-server-cache seam, and the beat-driven deploy
window sweep. Validate `docker compose config`.

## 18. Spec coverage map

§4 data model, §5 wire contracts + shared engine interface (AD-03), §6 registry/
artifacts (AD-01/AD-07), §7 targeting/scheduling (AD-05/AD-06), §8 dispatch +
result, §9 scripts (AD-02), §10 reporting, §11 RBAC (AD-04), §12 API, §13 worker
+ shared-schedule refactor, §14 frontend, §15 security invariants, §16 testing,
§17 deploy. Integration seams (patch shares the engine contract, inventory
supplies detection state, site-server is a seam) are called out in §3/§5/§6.
