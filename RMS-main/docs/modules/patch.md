# Module — Patch Management

> Scoping brief. Phase 4. Depends on Inventory (Phase 1). Expand into a full spec
> via the brainstorming skill before implementation.

## Purpose

Detect missing OS and third-party patches across the fleet, and apply them
safely — with approval workflows, maintenance windows, staged rollout, and
Sentinel-driven prioritization — across Windows, Linux, and macOS.

## Scope — v1

- **Scan:** determine missing/available OS updates and third-party app updates
  per endpoint, using inventory + per-OS update sources.
- **Approval workflow:** patches are reviewed and approved (not auto-applied by
  default). Approval scoped by RBAC.
- **Deployment rings & windows:** apply to test rings first, then broader rings,
  within defined maintenance windows (policy-driven, resolved to the agent's
  local scheduler).
- **Reboot handling:** policy-controlled reboots with user notification/deferral.
- **Compliance reporting:** per-device and fleet patch status; this is a direct
  **SOC 2 evidence** output.
- **Rollback awareness:** record what was applied; surface uninstall where the OS
  supports it.

## Later phases

- Automated third-party patch catalog (broad app coverage).
- Pre-patch backup trigger (integrate the backup module before risky patches).
- Patch success prediction / anomaly flags.

## Agent capability

- `patch.scan` and `patch.apply` (dormant until invoked — heavy work only during
  a window).
- Per-OS engines:
  - Windows: Windows Update Agent API + `winget`; optional WSUS upstream.
  - Linux: `apt` / `dnf` / `zypper`.
  - macOS: `softwareupdate` (OS/Apple; MDM required to enforce OS updates on
    Apple Silicon) + Homebrew (third-party).
- Streams progress and structured results; honors reboot policy.

## Server side

- Aggregates scan results into patch status; manages approvals, rings, windows.
- Dispatches signed `patch.apply` jobs per policy/window.
- Produces compliance reports and evidence exports.

## Data model additions

- `patch` (org_id, os_family, identifier, title, severity, source, superseded_by)
- `device_patch_status` (device_id, org_id, patch_id, state:
  missing/approved/installed/failed, detected_at, applied_at)
- `patch_approval` (org_id, patch_id/scope, approved_by, ring, window_ref)
- `patch_run` (job_id, org_id, device_id, results jsonb)

## Integration points

- **Inventory** supplies OS/software/version state (hard dependency).
- **Sentinel** feeds prioritized vulnerable targets → drives which patches matter
  most.
- **Backup** (later) triggers a pre-patch snapshot.
- **Change management** (later) can require patch campaigns to be change-tickets.
- **SOC 2 evidence** export of compliance status.

## Open questions (resolve in spec)

- Default posture: approval-required everywhere vs auto-approve low-risk security
  patches for certain rings.
- Third-party patch catalog scope in v1 (winget/Homebrew/Chocolatey coverage vs a
  curated set).
- macOS OS-update enforcement: how much to attempt via `softwareupdate` in v1
  before the MDM fast-follow lands.
