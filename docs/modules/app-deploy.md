# Module — Application Deployment

> Scoping brief. Phase 5. Shares per-OS package/install machinery with Patch
> (Phase 4). Expand into a full spec via the brainstorming skill before
> implementation.

## Purpose

Push software packages to endpoints from the console — install, update, and
uninstall applications across Windows, Linux, and macOS, targeted at individual
devices or (dynamic) groups. The PDQ Deploy / Intune-app style capability.

## Scope — v1

- **Package repository:** upload/register installers and metadata; binaries in
  object storage, cached by **site servers** for LAN-local delivery.
- **Deployment jobs:** install / update / uninstall to a device or group; detect
  current state via inventory to avoid redundant installs.
- **Per-OS install engines:**
  - Windows: MSI/EXE via **PSAppDeployToolkit**, `winget`, Chocolatey.
  - Linux: native packages + scripts.
  - macOS: `pkg`/`dmg`, Homebrew, **Installomator**.
- **Scheduling & windows:** deploy now or within a maintenance window (policy →
  agent scheduler); user notification/deferral where relevant.
- **Result reporting:** per-device success/failure with logs; retry semantics.
- **Custom scripts:** run signed scripts (pre/post install, or standalone) as a
  first-class, audited job type — with the same signed-job guarantees.

## Later phases

- Self-service software portal for end users (request/approve/install).
- Dependency/sequencing across multiple packages.
- License-aware deployment tied to inventory reconciliation.

## Agent capability

- `app.deploy` (install/update/uninstall) and `script.run` — dormant until a job
  runs.
- Pulls the package from the nearest **site-server cache** or object storage;
  verifies integrity; runs the per-OS installer; reports structured results.

## Server side

- Package registry and metadata; deployment targeting and scheduling; state
  reconciliation via inventory; result aggregation.

## Data model additions

- `package` (org_id, name, version, os_family, installer_type, artifact_ref,
  detection_rule jsonb, uninstall_rule jsonb)
- `deployment` (org_id, package_id, target_type, target_id, action, window_ref,
  requested_by)
- `deployment_result` (job_id, org_id, device_id, state, log_ref)

## Integration points

- **Inventory** for current-state detection and targeting (dependency).
- **Site servers** for cached LAN delivery of large installers.
- **Patch** shares the same per-OS engine plumbing (build once, reuse).
- **Change management** (later) can gate deployments behind change tickets.
- Audit for every deployment and script run.

## Open questions (resolve in spec)

- Package model: fully generic (bring-your-own installer + detection rules) vs a
  curated catalog + generic escape hatch, for v1.
- Script safety: how scripts are authored, signed, and reviewed given they run as
  SYSTEM/root (must still flow through the signed-job runner).
- Shared abstraction with patch: define the common per-OS install engine
  interface so both modules use it.
