# Module — Provisioning & Imaging

> Scoping brief. Phase 7. Infrastructure-heavy; requires the **site-server** role
> matured (PXE, caching). Named "OS image standardization and deployment" in the
> original request — reframed here because imaging means different things per OS.
> Expand into a full spec via the brainstorming skill before implementation.

## Purpose

Standardize and deploy operating systems to endpoints — golden-image deployment
where the OS supports it, modern enrollment where it doesn't — so new and
re-provisioned machines come up to a known, consistent baseline. The FOG / MDT /
WDS-style capability, done honestly across three OSes.

## The per-OS reality (important)

"Imaging" is not one thing:

- **Windows — image deployment.** WinPE + WDS-style deployment of a golden image
  driven by the **site server's PXE**, with `unattend.xml` for
  unattended setup, domain join, and post-config.
- **Linux — scripted install.** PXE + **kickstart / preseed / cloud-init** to
  build machines to a defined spec (not a captured image, per convention).
- **macOS — NOT imaging.** Apple Silicon **cannot be imaged**. Standardization is
  via **Automated Device Enrollment (ADE) + MDM** and `startosinstall` /
  erase-and-install workflows. This is enrollment, not image push.

So the module is **provisioning & standardization** with **three strategies**
under one workflow, not a single image engine.

## Scope — v1

- **Image / build management:** register golden images (Windows), build
  definitions (Linux kickstart/preseed/cloud-init), and enrollment profiles
  (macOS ADE/MDM).
- **PXE deployment (site server):** the site server hosts the boot environment and
  image/build payloads so large data stays on the LAN.
- **Bare-metal targets:** boot a machine via PXE, deploy the image/build, run
  post-config (naming, domain join, agent install/enrollment).
- **Agent auto-enrollment:** freshly provisioned machines auto-install and enroll
  the agent as part of post-config, so they appear managed immediately.
- **macOS enrollment:** integrate ADE/MDM so Apple devices standardize via
  enrollment; mark MDM-grade config profiles as the fast-follow noted in the
  vision.

## Later phases

- Driver management / hardware-model image variants (Windows).
- Image capture/build pipeline from a reference machine.
- Full MDM-grade macOS configuration management (APNs-driven profiles).

## Agent capability

- `provision.prepare` / `provision.enroll` — mostly light on the target agent;
  the **heavy lifting is on the site server** (PXE, payload serving). The agent's
  role is post-provision enrollment and configuration verification.

## Server side / site server

- Control plane: image/build/profile registry, provisioning jobs, post-config
  templates, ADE/MDM integration.
- Site server: PXE/boot environment, image/build payload hosting and caching,
  wake-on-LAN to power targets on.

## Data model additions

- `os_image` (org_id, os_family, name, type: image/build/enrollment, artifact_ref,
  params jsonb)
- `provision_profile` (org_id, name, image_id, post_config jsonb, target_scope)
- `provision_run` (job_id, org_id, device_id/target, state, log_ref)

## Integration points

- **Site servers** (hard dependency — PXE, caching, WoL).
- **Inventory** (post-provision the machine is inventoried immediately).
- **Agent enrollment** (provisioning ends with the agent enrolled).
- **Documentation** (build runbooks linked to images/profiles).

## Open questions (resolve in spec)

- Windows image format and toolchain specifics (WDS vs a lighter custom WinPE
  deploy).
- How far to integrate macOS MDM in v1 vs enrollment-only + fast-follow.
- Site-server hardening and PXE security (PXE is sensitive on a network).
