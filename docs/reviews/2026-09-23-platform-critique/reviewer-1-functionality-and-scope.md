# ITMS: module functionality and scope critique (reviewer 1)

Lens: what each module actually does end to end today, and whether it earns its place for a small IT team running Microsoft 365 with a Windows 11 + macOS fleet (`HANDOFF.md` §3: "Endpoints are Windows 11 and macOS. No Linux endpoints").

Method: I read the code, not the specs' claims. I dumped the live router (475 method+path pairs, built by `tests/tool_surface.build_router_only_app`) and matched every `request()` call site in `frontend/src/lib/api.ts` against it. I compared the server's job-type registry with the agent's capability registry, read the Celery beat schedule, and ran `go test ./... -count=1` in `agent/`. All 27 agent packages pass on Linux. Evidence is cited as `path:line`.

State vocabulary used below:
- **Built and working**: wired agent → job → API → UI, and at least one real-environment run on record.
- **Built, unproven**: wired end to end in code, but it has never executed on the target OS.
- **Built but shallow**: wired, but missing what makes the category useful.
- **Backend+UI only**: no endpoint half, or nothing ever dispatches it.
- **Spec only**: a document and no code.

---

## Executive summary

1. **No part of the fleet side has ever run on the fleet.** Toeshee's endpoints are Windows 11 and macOS. `HANDOFF.md` §5 says no Windows or macOS endpoint has ever run the agent. The first-run record, `docs/modules/agent-distribution/first-run-verification.md:685-723`, is blank in every row. The only real install attempt in history (commit `20a7cfe`, 2026-08-27) found four defects in the Windows install command, which "installs nothing, on every platform". Every endpoint-facing module therefore sits at "Built, unproven" at best.
2. **Remote control does not work at all.** The server dispatches `remote.session.open` (`backend/app/modules/remote/service.py:257`, registered at `remote/jobtypes.py:28`). The agent registers no handler for it (`agent/internal/capabilities/registry.go:166-313`), and nothing under `agent/` mentions `relay_url` or `relay_token`. The agent therefore answers every session with `unknown_type`. The spec lists the agent half as out of scope (`docs/modules/remote-control/spec.md:36-39`), yet `docs/product.md` §3 lists "audited remote control" under "What it does today". The design (guacd → RDP/VNC to `127.0.0.1:3389/5900`) also cannot show a technician the user's own screen, which is what helpdesk work needs.
3. **The agent collects no installed software on any OS.** The comments say so directly: `inventory/facts_windows.go:34-36` ("Installed software ... Not built"), `facts_linux.go:40`, and `facts_darwin.go` has no software code at all. The `/software` page, license compliance (`licenses/matching.py` reconciles against `InstalledSoftware`) and server-side deploy detection (`deploy/detect.py`) are therefore all fed an empty table.
4. **Inventory and security logs are never collected automatically.** The beat schedule (`backend/app/worker.py:2443-2570`) has no `inventory.collect` entry and no `logs.ship` entry. The agent schedules neither locally (`agent/cmd/agent/main.go:236-260`). `backend/app/jobs/direct_dispatch.py:110-112` admits "nothing in the repository dispatches [logs.ship]". Inventory goes stale after the first manual run, and the SOC 2 access-evidence path produces nothing unless someone presses a button on each device.
5. **Email intake and email notifications cannot talk to Microsoft 365 today.** Intake does IMAP `LOGIN user password` (`email_intake/imap.py:601`), and Exchange Online disabled basic-auth IMAP. The team's own design doc says this in its §0 (`docs/superpowers/specs/2026-09-11-mail-oauth-design.md`). Notifications use plain `smtplib` (`notification/delivery.py:768`). For an M365 shop, the service desk's inbound channel is dead on arrival.
6. **Effort has gone to the periphery rather than the core.** Governance and spend modules (ipam, sla, budget, licenses, subscriptions, bcp, change_mgmt, documentation) total about **33,100** backend lines. The six core RMM modules (inventory, patch, deploy, remote, health, backup) total about **23,500**. IPAM alone (11,363 backend + 8,410 frontend lines) is roughly 8× inventory (1,413 + 1,349).
7. **Several modules duplicate what an M365 tenant may already pay for.** If Toeshee holds Business Premium, E3+EMS or E5 (unverified), they already have Intune (Windows Update for Business rings, Win32/Store app deployment, compliance for BitLocker/firewall/AV, Autopilot, ADE), Defender (EDR and device security logs) and Entra sign-in logs. Provisioning, MDM, security logs and much of health posture and patch overlap directly. Nothing in the tree integrates with Microsoft Graph, Intune or Defender. The only Intune touchpoint is instructions for distributing the agent (`agent_distribution/managed.py:37`).
8. **Genuinely strong: the kernel and its discipline.** Signed jobs with a closed agent registry, refusal-not-failure semantics, capped enrollment tokens (`api/v1/enrollment.py:15-52`), hash-verified artifact installs (`deploy/install.go:56-80`), FORCE-RLS, and a clean API contract. All 231 frontend `request()` call sites resolve to mounted routes, with no dangling client methods. The health module's server half is the best-engineered business module. These are real assets, and they are being spread across about 30 surfaces instead of made to work on 250 laptops.
9. **Some modules solve problems this fleet does not have.** `agent_push` is SSH-only (`pushinstall/pushinstall.go:249-250, 281-282`), and Windows 11 and macOS ship with no SSH server enabled. It carries its own security-invariant amendment (2a) and is redundant with the Intune channel that already exists. The MDM spec runs to 1,733 lines with zero code, for a job Intune already does.
10. **Recommendation: shrink to about 10 surfaces and finish them on real hardware.** That means inventory (with software), health/alerting, patch (including third-party apps), deploy+scripts, remote (by integration), service desk (merged with portal, intake and SLA), evidence reporting, audit and credentials. Freeze or cut the rest until one Windows and one Mac box pass the first-run table.

---

## Per-module table

| Module | State (verified) | Verdict | One-line reason |
|---|---|---|---|
| **devices / inventory** | Built but shallow; unproven on Win/mac | **KEEP & DEEPEN (priority 1)** | Hardware facts and NICs only; no software on any OS; no scheduled refresh |
| **health** (alerting/remediation) | Server built and verified live; agent measured on Linux, unexecuted on Win/mac | **KEEP & DEEPEN** | Best module; needs a default profile, real-device runs, and macOS CPU/memory |
| **patch** | Built, unproven; OS updates only | **KEEP & DEEPEN (reposition)** | No third-party apps, no reboot orchestration; WUA and `softwareupdate` never executed |
| **deploy** (+ scripts) | Built, unproven | **KEEP & DEEPEN** | Solid hash-verified path; winget as SYSTEM very likely broken; no scheduled scripts |
| **remote** | Backend+UI only; **no agent half** | **REPLACE WITH INTEGRATION** (MeshCentral / Quick Assist / Remote Help) | Cannot open a session today, and the RDP design does not shadow the user |
| **security_logs** | Both halves built; never dispatched; Linux collector absent | **KEEP (narrow) + fix scheduling**, or replace with Entra/Defender pull | Evidence path produces nothing without manual jobs |
| **backup** | Built; `backup.run` has **never completed**; restore read never measured | **DEFER** (integrate OneDrive KFM / Veeam / restic reporting) | Heaviest module, zero proven backups, stored on the same host |
| **provisioning** | Registry + answer-file render + verify-only agent job; no PXE | **CUT / REPLACE WITH INTEGRATION** (Autopilot, ABM/ADE) | Paperwork around imaging that does not happen |
| **site_server** | Built (WoL, sweep and push origin) | **DEFER** (keep as is) | No caching and no PXE; its main consumers are deferred |
| **service_desk** | Built and reasonably complete core | **KEEP & DEEPEN** (absorb portal, intake, SLA) | Device-linked tickets are a real differentiator |
| **portal** | Built | **MERGE INTO service_desk** | Needs Entra SSO live (`realm-it-suite.json:117` placeholder) |
| **email_intake** | Built; **cannot authenticate to M365** | **MERGE INTO service_desk**; ship OAuth first | Basic-auth IMAP only |
| **notification** | Built (webhook + SMTP) | **KEEP as kernel service** | SMTP basic auth; no Teams-native format |
| **sla** | Built (6,640 lines) | **MERGE INTO service_desk; simplify** | Overbuilt for a handful of technicians |
| **change_mgmt** | Built; advisory only | **MERGE INTO service_desk** as a change ticket type with a gating hook | Links records and gates nothing; ticket backend defaults to `null` |
| **bcp** | Built; manual records | **DEFER** | Forms and reports; useful at audit time only |
| **documentation** | Built (articles, typed records, files, FTS) | **KEEP (freeze) → becomes service desk KB** | SharePoint/OneNote/Hudu exist; device-linking is the only edge |
| **licenses** | Built; reconciliation input empty | **MERGE with subscriptions → "Spend"; DEFER** | Compliance is meaningless without software inventory; no M365 SKU pull |
| **subscriptions** | Built CRUD; no proactive alerts | **MERGE (with licenses)** | Renewal "alerts" appear only when someone opens the page |
| **budget** | Built (5,155 + 4,331 lines) | **CUT** (spreadsheet/Odoo) | Plan vs actual belongs in the ERP they already run |
| **ipam** | Built; largest module | **DEFER / freeze** (NetBox or phpIPAM if needed) | ICMP-only on Win/mac; 20k lines for a spreadsheet-sized problem |
| **reporting** | Built (4 reports + CSV) | **KEEP & DEEPEN** | Evidence is the stated differentiator; only 4 reports |
| **agent_distribution** | Built; never observed completing on Win/mac | **KEEP & DEEPEN** | Needs MSI/pkg, a real-device pass and self-update |
| **agent_push** | Built; SSH-only | **CUT** (freeze) | The fleet runs no SSH servers; Intune channel already exists |
| **ai_admin / ai_copilot / ai_portal** (+ `core/ai`) | Built; 2 staff read tools; never run against a live model | **DEFER** | About 9.5k backend + 3.7k frontend lines for two read tools |
| **keycloak_events** | Built (worker sweep) | **KEEP** | Small, kernel-level audit completeness |
| **user_admin** | Built | **KEEP** | Needed for RBAC lifecycle |
| **MDM** | **Spec only** (1,733 lines), no code | **CUT from roadmap** | Intune does this; invariant 1b is cost with no product |
| Kernel UI: jobs, audit, credentials, enrollment, settings, dashboard, platform-status, error-monitor, onboarding, setup | Built | **KEEP** | Kernel surfaces; fine |

---

## Per-module detail

### Devices and inventory (core) — Built but shallow; unproven on target OS

**What exists.**
- `inventory.collect` is registered on both sides: `backend/app/modules/inventory/handler.py:53` and `agent/internal/capabilities/registry.go:169`.
- Ingest, change history, post-ingest hooks (IPAM listens) and a live `inventory_updated` UI event: `handler.py:24-51`.
- CSV/XLSX export: `inventory/api.py:485-521`.
- Agentless asset rows, rule-based dynamic device groups (`inventory/rules.py`) and a discovery "candidates" page.

**What the agent actually collects.**
- **Windows**: one PowerShell CIM call covering OS, computer system, BIOS, first CPU and disks (`inventory/winfacts.go:26-38`), plus NICs.
- **macOS**: `sw_vers`, `ioreg` serial, CPU brand, memsize and NICs. Disks are deliberately not enumerated (`facts_darwin.go:15-20`).
- **Installed software: none, on any platform** (`facts_windows.go:34-36`, `facts_linux.go:40`). `inventory.go:207` passes through an always-empty slice.

**What is missing for this to be an inventory.**
- **No refresh cadence.** The only way to run `inventory.collect` is the generic "Collect inventory" job in the Jobs page (`frontend/src/modules/jobs/jobTypes.ts:71`). No beat entry exists (`worker.py:2443-2570`), and there is no collect-on-enroll.
- **Missing fields**: logged-on user, last boot, BitLocker/FileVault status, warranty, model, Windows edition/SKU, Entra join state and Intune compliance state.
- **Never executed on target.** Windows and macOS fact gathering has never run; only the parsers are tested, on Linux.

**Compared with alternatives.** Intune device inventory, NinjaOne, Action1 (free up to 200 endpoints) and even Snipe-IT with an agent all deliver software inventory on day one. As it stands this is weaker than the free tier of every competitor.

**Verdict: KEEP & DEEPEN.** Everything downstream depends on it (licenses, deploy detection, patch targeting, IPAM, ticket context).

**Improvements.**
1. Windows software inventory from HKLM Uninstall (64- and 32-bit views), per-user HKU hives and Appx packages. macOS from `/Applications/*.app/Contents/Info.plist` and `pkgutil --pkgs`. Stream it within the existing 4,096 cap (`inventory.go:56`).
2. A server-side beat task that dispatches `inventory.collect` daily per device, plus once on first connect after enrollment.
3. Add security-relevant facts: BitLocker/FileVault state, TPM, Secure Boot, last boot, current console user, model and manufacturer.
4. Capture real output from one Windows 11 and one Mac and commit it as fixtures, replacing the hand-written ones (`HANDOFF.md` §5).

### Endpoint health, alerting and remediation (core) — the strongest module

**Server side.**
- About 10.8k backend and 5.4k frontend lines.
- 14 check types in a closed registry (`agent/internal/capabilities/health/checks.go:70-87`).
- Dedup, flap suppression, auto-clear, the offline sweep, and the action worker `remediate → open_ticket → notify` (`backend/app/modules/health/actions.py:1`).
- Rate fuses and approved-action gating.
- `docs/06-roadmap.md` records a live end-to-end run against real Postgres and Redis. This is the one business module whose control-plane loop has demonstrably run.

**Agent side.**
- Linux samplers and posture checks are measured, with falsifiers in CI.
- Windows samplers exist (`sampler_windows.go`, via GetSystemTimes and GlobalMemoryStatusEx) but have never executed.
- macOS answers disk and process checks. `cpu.busy_pct` and `memory.used_pct` are recorded refusals (`sampler_other.go:10-20` history; `sampler_darwin.go`).
- `agent.offline` is server-side and works regardless of OS. It is the single most useful alert here, and it works today.

**Gaps.**
- **No default profile is seeded**: grepping `health` for a default or seed profile finds none, and migration `0042_health.py` seeds none. A fresh install monitors nothing except offline.
- Posture checks (BitLocker, AV, firewall) duplicate Intune compliance policies and Defender, which the team may already pay for.

**Compared with alternatives.** The design (edge thresholds, event-only, no time series) is sound and comparable to Atera/NinjaOne conditions. It lacks their scale of built-in monitors and their scripted-condition monitors.

**Verdict: KEEP & DEEPEN.**

**Improvements.**
1. Seed a default "Workstation baseline" profile (disk free < 10%, offline > 30 min, BitLocker off, Defender unhealthy, firewall off) and auto-assign it to all devices, with open-ticket as the default action.
2. Run the Windows sampler and posture checks on a real box and commit the numbers, so the idle budget is verified on the fleet's actual OS.
3. Add a `script.exit_code` custom check backed by an approved script. This is the most-used monitor type in NinjaOne and Datto.
4. Read Intune compliance state instead of re-deriving it wherever it is licensed.

### Patch management (core) — Built, unproven; shallow for this fleet

**Built.**
- `patch.scan` and `patch.apply` on both sides (`patch/handler.py:76-77`, `registry.go:305-306`).
- Hourly scan sweep and 15-minute apply sweep (`worker.py:2481-2482`).
- Rings, approvals, maintenance windows (`patch/policy.py:28`), compliance export, and the largest UI in the product after IPAM (6.4k lines).
- Windows uses in-process WUA COM, and `wua_windows.go:17` says "NOTHING IN THIS FILE HAS EVER EXECUTED". macOS uses `/usr/sbin/softwareupdate`, whose fixture is named `...-RECONSTRUCTED.txt`.

**Shallow.**
- **The agent does not reboot.** It detects a pending reboot and reports it, and `reboot`/`deferral_hours` are parsed and ignored (`patch/patch.go:45-57, 551-564`). Nothing on the server acts on `reboot_required`, so Windows cumulative updates sit half-applied until the user reboots.
- **No third-party application patching.** No catalogue and no winget upgrade path. For a small shop this is the main reason to buy Action1, NinjaOne or PatchMyPC over plain Intune.
- The Sentinel priority seam is a nullable column with no client.
- A likely issue, unverified because it has never run: on Apple Silicon, `softwareupdate --install` of macOS updates as root normally needs a volume-owner credential or an MDM bootstrap token. Expect minor OS updates to fail or hang.

**Compared with alternatives.** If Intune is licensed, WUfB rings and Autopatch already handle Windows OS patching, with reboot UX, deadlines and reporting. Duplicating WUA orchestration is low-value.

**Verdict: KEEP & DEEPEN, repositioned.**

**Improvements.**
1. Third-party app patching via `winget upgrade --all` scoped to approved IDs, and `brew`/installer-based updates on macOS, reusing the deploy module's hash and allowlist machinery.
2. Reboot orchestration: honour `reboot` and `deferral_hours` agent-side with a user-visible prompt (this needs the user-session helper that was never built), or schedule reboot jobs server-side inside the maintenance window.
3. On Windows, add a "WUfB-managed" mode that reports WUA state and compliance without installing, so Intune can stay the installer where it is licensed.
4. Run one real scan and apply on Windows 11 and on macOS, and capture the fixtures.

### Software deployment and scripts (core) — Built, unproven

**Built.**
- `app.deploy` and `script.run` on both sides (`deploy/handler.py:156-174`, `registry.go:303-304`).
- Artifacts must match `artifact_sha256` or the job is refused before download (`deploy/install.go:56-80, 340-364`).
- Supported engines: MSI, EXE, winget and choco on Windows; `.pkg` on macOS (`deploy/plan.go:237-241`). DMG, DEB and RPM are refused by name.
- Artifact serving through MinIO on :443 is fixed and measured (`HANDOFF.md` §2, Option 1).
- The script library is versioned, the body travels inside the signed envelope, and there is a UI (`frontend/src/modules/deploy/ScriptsPage.tsx:63`).

**Likely defect, unverified because it never ran.** winget is resolved with `exec.LookPath("winget.exe")` (`deploy/plan.go:170-181, 319-330`). Under LocalSystem, winget is not on PATH: it is a per-user App Execution Alias under `WindowsApps`. Expect every `winget` deployment to be refused with "winget is not installed on this device" on stock Windows 11. Grepping `agent/` for `WindowsApps` or `DesktopAppInstaller` returns nothing.

**Shallow.**
- No scheduled or recurring script runs, and no "run on group when condition X". The RMM workhorse feature is on-demand only.
- No self-service catalogue for users.
- The server's `package_installed` detection is empty, because inventory collects no software (`deploy/detect.py:10-20`).

**Compared with alternatives.** PDQ Deploy, Intune Win32 apps and NinjaOne. The integrity story is stronger than PDQ's. The breadth is far thinner.

**Verdict: KEEP & DEEPEN.**

**Improvements.**
1. Resolve winget's real path under SYSTEM (DesktopAppInstaller package directory), with a test on a real box.
2. Scheduled script policies: script × group × cron, with output retention and an optional health check that alerts on non-zero exit.
3. Make detection rules first-class, so a deployment is idempotent and can be "assigned" (desired state) rather than "executed once".

### Remote control (core) — Backend+UI only; non-functional

**Built.**
- Session state machine, vault checkout, audit and recording (`remote/service.py:1-24`).
- guacd broker (`remote/broker.py`), relay and tunnel, recordings with retention.
- UI: sessions page, viewer and recording player.

**Not built.**
- **The agent half.** The capability list in `agent/internal/capabilities/registry.go:166-313` contains no `remote.session.open`. The server-side registry lists it. The spec defers it (`docs/modules/remote-control/spec.md:36-39`).
- The attended-consent prompt, which needs a user-session helper: grepping the agent for `WTSQueryUserToken`, `CreateProcessAsUser` or consent UI finds nothing.
- The org remote policy routes (`GET/PUT /api/v1/remote/policies`) have no frontend caller, so unattended mode cannot be enabled from the console.
- The UI gives no warning that sessions will never connect.

**Design problem even if the agent half is built.**
- guacd reaches the endpoint's own RDP (3389) or VNC (5900) through the relay (`remote/service.py:44`).
- RDP to a Windows 11 workstation needs Pro or Enterprise and the RDP listener enabled fleet-wide. That adds an OS listening service to every laptop.
- A new RDP session disconnects or locks the interactive user, so the technician cannot see what the user sees. That rules it out for attended helpdesk, which the vision says is the BPO's key need.
- macOS needs Screen Sharing enabled.
- The spec's own v2 (WebRTC shadowing) is what helpdesk actually needs.

**Compared with alternatives.**
- MeshCentral (Apache-2.0, so no AGPL issue) and Microsoft Quick Assist (free) or Remote Help (Intune add-on) all do attended screen sharing today.
- RustDesk server is AGPL, which the vision excludes.

**Verdict: REPLACE WITH INTEGRATION now, and revisit an in-house WebRTC agent later.** Keep the audit, credential and recording scaffolding as the "session record" around whichever tool launches.

**Improvements.**
1. Launch Quick Assist, Remote Help or MeshCentral from the device page, and write `remote.session_started`/`ended` audit events from the launch, consent and termination callbacks available.
2. Remove "audited remote control" from `docs/product.md` §3 until a session can connect.
3. If an in-house path is pursued, build the agent capability as screen capture plus input injection in the user session, not RDP loopback.

### Security logs — Both halves built, never dispatched

**Built.**
- Agent: Windows `EvtQuery` for event IDs 4624, 4625, 4634, 4647, 4740, 4672 and group changes (`logs/winevent.go:38, 79-81`), and the macOS unified log.
- Server: ingest with dedup (`security_logs/ingest.py`), search, SOC 2 CSV export (3 routes) and retention.
- Linux is refused (`logs/source_other.go:1-33`), which is fine for this fleet.

**Not wired.**
- No dispatcher exists. The org policy's `ship_interval_s: 60` (`security_logs/ingest.py:21`) is read by nothing.
- `logs.go` itself says the cadence "drives the CONTROL PLANE's dispatch", and the control plane does not do it (`direct_dispatch.py:110-112`).
- The Windows and macOS collectors have never executed.

**Compared with alternatives.** Defender for Endpoint and Entra ID sign-in logs already hold logon evidence for an M365 tenant, and auditors accept them. The console audit log, with `keycloak_events` forwarding, covers access to this platform.

**Verdict: KEEP (narrow), fix the scheduler.** Otherwise replace it with a Graph pull of Entra sign-in and audit logs.

**Improvements.**
1. A beat task that dispatches `logs.ship` per device on `ship_interval_s`. This is small.
2. Surface "devices that shipped no events in N days" as a health check, so the absence of evidence becomes visible.

### Backup — Built, never completed end to end

**Built.**
- A repository format with dedup, prefix-scoped presigned POST grants (measured against real MinIO: 403, 400, 204), retention GC and daily chunk verification (`worker.py:1275-1302`).
- Snapshot providers for VSS, APFS and LVM, bound in `main.go:343`.
- About 6.3k agent lines and 7.3k agent test lines.

**Status.**
- `HANDOFF.md` §5: "`backup.run` has still never completed end to end, on any platform". The restore read leg "has never run against a real object store".
- VSS "writer status never checked" can report success over torn data.
- The registry comment is stale and contradicts `main.go`: `registry.go:285-302` still says `OpenSnapshot` is nil and restore refuses.
- Storage is the same MinIO on the same single Compose host as the control plane, so backups share a failure domain with the thing that manages them.

**Compared with alternatives.**
- User data on M365 laptops is normally covered by OneDrive Known Folder Move.
- Servers: Veeam Agent (free tier), restic or Duplicati.
- M365 data backup (Exchange/OneDrive/SharePoint) is what auditors will ask for, and this module does not touch it.

**Verdict: DEFER.** Freeze the engine. Ingest backup status from whatever tool is chosen into `reporting/backup_coverage` so the evidence story survives.

**Improvements if resumed.**
1. Get one `backup.run` plus `backup.restore` round trip on one Windows 11 box.
2. Require an off-host target before calling any point "complete".
3. Scheduled test restores with a recorded `restore_tested_at` (the field already exists, at `backup/api.py:116`).

### Provisioning and imaging — Built but shallow (paperwork)

**What it is.**
- A registry of OS images and profiles, and a deterministic unattend/kickstart/preseed/cloud-init renderer with vault-sealed secrets.
- Run tracking, and a `provision.enroll` job that only **verifies** hostname, domain join and agent health (`agent/internal/capabilities/provision/provision.go:1-60`).
- The spec pins PXE, TFTP and LAN cache as a contract and does not build them (`docs/modules/provisioning/spec.md` §1).
- Nothing delivers the rendered answer file to a machine.

**Compared with alternatives.** For Windows 11 and macOS in an M365 tenant, the modern path is Autopilot and Apple Business Manager with ADE through Intune. Neither needs imaging. Domain-join credentials in unattend.xml target on-prem AD, which is likely the wrong model for an Entra tenant (unverified).

**Verdict: CUT** (or freeze), replaced by an Autopilot/ADE integration and a post-enrollment baseline: a deploy bundle plus a health profile applied when a device first enrolls.

### Site servers — Built

Built pieces:
- Promote a device (`site_server/__init__.py:1-12`).
- Wake-on-LAN (`site.wake`), which works in code.
- Segment declaration, used as the origin for IPAM sweeps and push installs.

There is no caching and no PXE. Its main consumers (provisioning, push, IPAM) are all candidates to freeze.

**Verdict: DEFER**, keeping it as is.

### Service desk — Built, reasonably complete core

**Built.**
- 20 routes: tickets, queues, categories, assignment, per-category approvals, comments with internal notes, attachments, history, and device-linked tickets filterable by `device_id` (`service_desk/api.py:527-536`).
- Health alerts open tickets. An AI copilot hook exists.

**Missing compared with Freshservice, JSM or Zammad.**
- Canned responses and macros, a knowledge base (docs articles are not linked), ticket merge and parent/child links, time tracking, CSAT, and reply-by-email threading. That last one depends on intake working.

**Verdict: KEEP & DEEPEN.** A ticket showing the device's inventory, patch and alert state inline is the one place the platform beats a standalone helpdesk.

**Improvements.**
1. Make intake work with M365 (below).
2. Canned responses, plus KB articles taken from the documentation module and suggested in the portal.
3. A device panel inside the ticket with one-click "run approved script", "launch remote" and "collect inventory".

### Portal — Built

A second, scoped surface over the ticket core (`portal/__init__.py:1-10`). It is only reachable by end users once Entra federation is configured, and `deploy/keycloak/realm-it-suite.json:117` still holds `REPLACE_WITH_ENTRA_APPLICATION_ID`.

**Verdict: MERGE INTO service_desk** as one module. First step: finish Entra SSO for requesters.

### Email intake — Built, cannot authenticate to M365

IMAP basic auth only (`email_intake/imap.py:601`). OAuth is designed (`2026-09-11-mail-oauth-design.md`), and delegated IMAP is scheduled as a later cycle.

**Verdict: MERGE INTO service_desk.** Prefer the Graph mail API with application permissions scoped by an Exchange application access policy over delegated IMAP. That removes a token that expires when a person leaves.

### Notification — Built

Channels are `webhook` and `email` only (`notification/schema.py:39`). Email goes through `smtplib` with a vaulted password. There are rules, deliveries, retries and an allowlist.

**Verdict: KEEP as a kernel service.** Add a Teams channel format (Adaptive Card via a Workflows webhook), and move email to Graph `sendMail` or OAuth SMTP.

### SLA — Built, overbuilt

6,640 backend + 3,790 frontend lines for business-hours calendars, holidays, policies, a breach sweep and attainment reporting. That is more than inventory, deploy and remote combined on the backend (1,413 + 2,540 + 2,062 = 6,015).

**Verdict: MERGE INTO service_desk** and stop investing. For a handful of technicians, one calendar with response/resolve targets per priority is enough.

### Change management — Built, advisory only

- Linking is "purely advisory ... never reads back into or mutates the linked action" (`change_mgmt/links.py:1-10`).
- Patch, deploy and provisioning are not gated by an approved change.
- `change_ticket_backend` defaults to `"null"` (`core/config.py:147`), so changes do not even appear in the service desk unless an environment variable is set.

The vision's "governance is first-class" rests largely on this module, and today it is a form.

**Verdict: MERGE INTO service_desk** as a change ticket type.

**Improvements.**
1. An optional org policy, "patch ring X / deploy to group Y requires a linked approved change", enforced at dispatch.
2. Auto-create a change record from a patch approval or deployment, so evidence exists without extra typing.

### BCP / DR tests — Built, manual records

Plans, tests, steps, findings and a period report. It pulls backup evidence rows (`bcp/service.py:36`), and those rows are empty because no backup has ever completed.

**Verdict: DEFER.** This is an annual activity that a document template does equally well.

### Documentation — Built

Markdown articles with versions, typed records (network, service, vendor, location), versioned file documents, device and credential-metadata links, and Postgres FTS (26 routes). There are no templates, no relationship graph and no inventory auto-population (`docs/modules/documentation/spec.md` §1).

**Verdict: KEEP (freeze), then repurpose as the service desk knowledge base.** Against Hudu or IT Glue, its only edge is living beside the devices. For an M365 shop, SharePoint is the default competitor.

### Licenses — Built; its core function has no input

Software products, entitlements, matching and a review queue, plus a daily compliance sweep (`worker.py:2495`). Matching reads `InstalledSoftware` (`licenses/matching.py:28, 110-118`), which the agent never fills. There is no pull of Microsoft 365 SKUs and assigned seats (Graph `subscribedSkus`), which is this company's largest license line.

**Verdict: MERGE with subscriptions into one "Spend" module; DEFER** until software inventory exists. Snipe-IT covers the manual part for free.

### Subscriptions — Built CRUD

The spec says "Control-plane only, no worker. Notice-deadline and upcoming-renewal 'alerts' are computed at read time" (`docs/modules/subscriptions/spec.md:13-16`). A notice deadline only surfaces if someone opens the page, which defeats the point of renewal alerting.

**Verdict: MERGE** with licenses. If kept at all, push renewal and notice-deadline events through `notification`.

### Budget — Built (5,155 + 4,331 lines)

Plans, periods, FX, expenses with attachments, forecasts and an Odoo CSV export. Odoo is the ERP they already run (`docs/00-vision.md`, "Integration posture").

**Verdict: CUT.** It is a second finance system maintained by the IT team.

### IPAM — Built; the largest module

- 40 routes. Subnets, assignment, CSV import and export, drift reconciliation against inventory NICs, and a `net.sweep` capability.
- Sweeps on Windows and macOS are ICMP-only with no MAC data (`netsweep/neigh_other.go:1-19`). Windows 11's default firewall drops ICMP echo, so expect sparse discovery.

**Verdict: DEFER / freeze.** phpIPAM or NetBox exist for free if the address plan outgrows a spreadsheet.

### Reporting — Built

Four report definitions: backup coverage, fleet posture, patch compliance and SLA attainment (`reporting/definitions/`), with CSV export. Two of the four inputs are hollow today: backup has never completed, and posture is unexecuted on Windows and macOS.

**Verdict: KEEP & DEEPEN.** Evidence is the product's stated reason to exist (`docs/00-vision.md`).

**Improvements.** A monthly SOC 2 evidence pack (patch SLA, offline and unmanaged devices, privileged access events, change records), with the "0 = nothing measured" distinction that `docs/product.md` §8 already demands.

### Agent distribution — Built; never observed completing on Win/mac

- A one-machine install command with a capped, single-use or bounded token.
- GPO and Intune "managed deployment" instructions with a separate token route (`agent_distribution/managed.py:1-47`, `CHANNELS = ("gpo","intune","manual")`).
- There is no MSI and no macOS `.pkg`; packaging is PowerShell and shell scripts (`agent/packaging/windows/install.ps1`, `darwin/install.sh`).
- There is no agent self-update. OQ-3 deliberately defers it until code signing exists.

**Verdict: KEEP & DEEPEN.**

**Improvements.**
1. Complete Parts A and C of `first-run-verification.md` on real hardware. This is the single most important act in the whole project.
2. Build an Intune-ready Win32 package (`.intunewin` wrapping install.ps1, or an MSI) and a signed `.pkg`.
3. Authenticode signing, then signed self-update. Otherwise every agent fix means an Intune redeploy.

### Agent push install — Built; wrong transport for this fleet

- SSH is the only transport (`pushinstall/pushinstall.go:249-250, 281-282`). The fleet is Windows 11 and macOS: OpenSSH Server is an optional feature on Windows and Remote Login is off on macOS.
- It needs a declared site server per segment, a dedicated credential and scoped authorizations.
- It required a new security invariant, 2a (`CLAUDE.md`), and introduces a credential that "whatever answers an address receives".
- The UI contradicts itself. `frontend/src/modules/agent-push/PushNav.tsx:101` says "The install itself is not built yet", while `PushPreflightPanel.tsx:122` is "the screen that dispatches one".

**Verdict: CUT** (freeze, and hide the nav entry). The Intune channel already covers enrollment for an M365 shop, with no credential exposure.

### AI admin, copilot and portal (+ `app/core/ai`) — Built, very thin

- The staff tool registry is exactly two read tools, `devices.health` and `health.alerts` (`backend/app/core/ai/tools/registry.py:95-150`). The portal assistant has three (`ai_portal/tools.py:74-141`).
- Act mode runs through an approved-action catalogue.
- About 6.5k lines in `core/ai` plus about 3k across the three modules and about 3.7k of frontend.
- `HANDOFF.md` §5: "No live AI smoke test".

The safety engineering (catalogue keys, never effects, and an autonomy dial capped below `autonomous` by a CHECK constraint) is thoughtful. But an assistant that cannot read inventory, patch or tickets answers almost nothing an operator would ask.

**Verdict: DEFER.** Revisit once the core modules hold real data.

### keycloak_events, user_admin — Built, kernel-level

Small and necessary. They forward credential events into the audit chain (`keycloak_events/__init__.py:1-20`) and handle user lifecycle against Keycloak.

**Verdict: KEEP.**

### MDM — Spec only

`docs/modules/mdm/spec.md` is 1,733 lines, `docs/00-vision.md` says "Still nothing is built", and invariant 1b sits in `CLAUDE.md`. Building an APNs/ADE/EMM stack for a tenant that can use Intune (if licensed) is a multi-quarter project with a large security surface.

**Verdict: CUT from the roadmap.** Integrate with Intune for mobile if phones must appear in the device list.

### Kernel UI (devices list, jobs, audit, credentials, enrollment, settings, dashboard, platform status, error monitor, onboarding, setup) — Built

**Verdict: KEEP.** The API contract is clean: all 231 `request()` call sites in `frontend/src/lib/api.ts` and `modules/ai-admin/api.ts` resolve to mounted routes. Backend routes with no UI caller that matter:
- `GET/PUT /api/v1/remote/policies`: unattended mode cannot be configured.
- `POST /api/v1/users/{id}/enable|disable`: likely built as a template string, so probably a false positive of my scan.

---

## Cross-cutting conclusions

**1. The scope is wrong for the team size and the stage.**
- The console has about 35 navigation entries (`frontend/src/shell/nav.ts:57-194`) across 29 backend modules.
- There are 97k lines of Markdown under `docs/`, `deploy/README.md` runs to 3,467 lines, and there are 203k lines of backend tests.
- Meanwhile the MVP the roadmap itself defines ("Phases 0–3: enrolled fleet, live inventory, centralized security logs, audited admin remote control", `docs/06-roadmap.md` "MVP definition") is **not met**:
  - The fleet has not enrolled.
  - Inventory has no software and no refresh.
  - Security logs never ship.
  - Remote control cannot connect.
- A handful of operators cannot maintain 30 modules, and an early commercial buyer will judge the first four.

**2. The core that must be excellent:**
- Enrollment and distribution.
- Inventory, with software and scheduled refresh.
- Health/alerting with offline detection.
- Patch, including third-party apps and reboots.
- Deploy and scripts, including scheduled scripts.
- Remote access, by integration.
- Service desk, as one merged module.
- Evidence reporting on top of the audit chain.

These are the jobs the vision lists for the internal deployment ("patching, imaging new-hire machines, helpdesk remote sessions, backup assurance"). Of those four jobs, the platform can do none on a real Windows 11 laptop today, as far as the repository can show.

**3. Distractions, in rough order of cost with no return:** IPAM, SLA's depth, budget, agent_push, provisioning, the MDM spec, AI act mode, BCP, subscriptions and licenses as standalone modules, and backup's engine (until it completes once).

**4. The M365 question has not been asked in the code.**
- Nothing reads Graph, Intune, Entra or Defender.
- For a tenant on Business Premium or E3+, Intune is the natural owner of OS patch deployment, compliance posture, Autopilot/ADE provisioning, and mobile.
- The platform's defensible value is what sits **across** those: device-linked tickets, cross-OS third-party patching and scripting, one audit chain, and auditor-ready evidence.
- A read-only Graph connector (devices, compliance, Defender alerts, SKUs and seats) would add more value than several of the modules above combined.

**5. Process observation (functionality impact only).**
- Status prose and code disagree in several places: `registry.go:285-302` vs `main.go:343`; `PushNav.tsx:101` vs `PushPreflightPanel.tsx`; `docs/product.md` §3 vs the absent remote agent half.
- The roadmap's Phase 16 cell has been "corrected" about six times in place.
- The team is paying heavily in prose to track the state of code that has not met a real device. One real Windows box would retire most of that prose.

---

## Ranked top-15 improvements

| # | Improvement | Expected impact | Effort |
|---|---|---|---|
| 1 | Run `first-run-verification.md` Parts A (Windows 11) and C (macOS) on real hardware. Fix what breaks. Commit captured fixtures for CIM, WUA, `softwareupdate`, EvtQuery and unified log | Converts every endpoint module from "compiles" to "works"; unblocks everything else | M |
| 2 | Software inventory on Windows (HKLM/HKU Uninstall, Appx) and macOS (Info.plist, pkgutil) | Lights up the `/software` page, licenses, deploy detection, patch targeting and ticket context | M |
| 3 | Server beat tasks: `inventory.collect` daily + on first connect; `logs.ship` at the policy's `ship_interval_s` | Inventory stays current; SOC 2 logon evidence actually accrues | S |
| 4 | Remote access by integration: launch Quick Assist, Remote Help or MeshCentral from the device and ticket pages, audited. Correct `product.md` | Delivers the single highest-value helpdesk feature now instead of never | M |
| 5 | Mail via Microsoft Graph (intake with an app access policy; notifications via `sendMail`) | Service desk gets an inbound channel in an M365 tenant | M |
| 6 | Default health baseline profile, auto-assigned, with alert → ticket enabled | Monitoring works on day one instead of after manual configuration | S |
| 7 | Fix winget resolution under SYSTEM and verify on a real box | Unblocks manager-sourced deploys, the cheapest software path on Windows | S |
| 8 | Third-party app patching via winget upgrade (Windows) and a curated mac catalogue, reusing deploy's allowlist and hash machinery | The feature that justifies an RMM over plain Intune/WUfB | M |
| 9 | Reboot orchestration for patch (maintenance-window reboot jobs, user notification and deferral) | Updates actually finish installing; compliance numbers become true | M |
| 10 | Scheduled script policies (script × group × cron, output retention, exit-code health check) | The RMM workhorse; covers countless small automations | M |
| 11 | Signed Intune Win32 package and signed macOS `.pkg`, then signed agent self-update | Fleet-scale rollout and patchable agents without redeploys | L |
| 12 | Read-only Microsoft Graph connector (Entra/Intune devices, compliance, Defender alerts, subscribedSkus and seats) into devices, health and licenses | Uses what the tenant already pays for; fills licenses/subscriptions with real data | L |
| 13 | Merge service desk, portal, email intake, SLA and change into one ITSM module. Add canned responses, KB from docs articles, and a change-gating hook at patch/deploy dispatch | Fewer surfaces; governance that gates something rather than recording it | M |
| 14 | Prune: hide or freeze agent_push, provisioning, budget, IPAM expansion, BCP, AI act mode and the MDM spec. Fix contradictory UI copy and stale code comments | Cuts about a third of the navigation and the ongoing maintenance/prose load; focus | S |
| 15 | Backup decision: either get one real `backup.run` + `backup.restore` on Windows 11 with an off-host target, or freeze it and ingest Veeam/restic/OneDrive status into backup-coverage reporting | Ends the "heaviest module, zero proven backups" state either way | S (decide) / M (execute) |

---

## Could not verify

- Whether Toeshee's M365 licence includes Intune, Defender or Entra P1. Several verdicts are conditional on that.
- Runtime behaviour on Windows and macOS, because this container has neither. The winget-under-SYSTEM and Apple Silicon `softwareupdate` concerns are inferences from known platform behaviour applied to the code as written, not observations.
- Whether GitHub Actions is currently green. I ran the agent suite locally (all pass on Linux). I did not run the 25-minute backend suite or the frontend suite.
