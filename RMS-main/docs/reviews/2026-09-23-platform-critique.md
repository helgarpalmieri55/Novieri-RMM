# ITMS platform critique: functionality, scope, UI/UX and user journeys

**Date:** 2026-09-23 · **Code reviewed:** `main` at `ca9b002` (after PR #84)
**Method:** three independent reviewers, run in parallel, who did not see each other's work. Each covered every module through one lens:

| Reviewer | Lens | How | Full report |
|---|---|---|---|
| 1 | Module functionality and scope | Read the code end to end and matched it with the specs. Compared the server's job types against the agent's handlers, and all 231 frontend API calls against the 475 mounted routes. Checked the worker schedule. Ran the Go agent suite. | [reviewer-1-functionality-and-scope.md](2026-09-23-platform-critique/reviewer-1-functionality-and-scope.md) |
| 2 | UI/UX and user journeys | Ran the console in fixture mode and screenshotted 57 routes at 1440×900 and 13 at 390×844 with Playwright. Walked 10 journeys step by step, including an end-user (requester) session. | [reviewer-2-ux-and-journeys.md](2026-09-23-platform-critique/reviewer-2-ux-and-journeys.md) |
| 3 | Platform coherence, operations, strategy | Traced flows between modules, measured code and tests, ran the security test sample, the frontend suite, the Go suite and mypy, and read the deploy and runbook path. | [reviewer-3-platform-and-strategy.md](2026-09-23-platform-critique/reviewer-3-platform-and-strategy.md) |

I merged the three reports and re-checked the load-bearing claims against the code myself (listed in the appendix). Where the reviewers disagreed, I say so and give a ruling. Key screenshots are in [`2026-09-23-platform-critique/screenshots/`](2026-09-23-platform-critique/screenshots/).

This is an assessment, not a decision record. Anything here that contradicts a decision in `docs/` is a proposal to change that decision deliberately. Nothing has been changed on the strength of this report.

---

## 1. The honest opinion

**The foundation is genuinely good, and most of what sits on it has never met a real computer.**

The kernel is the best part of this codebase, and all three reviewers said so independently:
- Signed jobs with no unsigned path.
- Row-level security that is really forced.
- An append-only, hash-chained audit log.
- A vault used at the point of use.
- An AI that can only propose catalogued actions a human confirms.

The hygiene is real: 7,674 backend tests, 3,855 frontend tests, a green Go suite, mypy clean on 396 files, and a frontend API contract with no dangling calls. The security design would pass a serious review.

But the platform does not yet do the four jobs the vision document names for Toeshee's own IT team: *patching, imaging new-hire machines, helpdesk remote sessions, and backup assurance*.
- **No Windows or macOS machine has ever run the agent.** Toeshee's fleet is Windows 11 and macOS only (`HANDOFF.md:254`). The first-run record in `docs/modules/agent-distribution/first-run-verification.md` is blank.
- **Remote control cannot open a session.** The server sends `remote.session.open`, and the agent has no handler for it. `agent/internal/wire/schema_test.go:116` says so: *"no agent capability planned"*. The console still shows a Start Session button, and `docs/product.md` lists remote control under "what it does today".
- **Backup has never completed a run** on any platform (`HANDOFF.md:398`).
- **The agent collects no installed software** on any OS (`inventory/facts_windows.go:34-36`). The Software page, licence compliance and deploy detection all read from an empty table.
- **Inventory and security logs are never collected automatically.** Neither job is in the worker schedule (`backend/app/worker.py:2443+`).
- **Nothing talks to Microsoft 365.** Email intake uses password IMAP (`email_intake/imap.py:601`), which Exchange Online refuses. There is no Graph, Intune, Entra or Defender integration, and this is an M365 company.

Meanwhile, effort has gone to the edges:
- **IPAM** is the largest module (about 11.4k backend + 8.4k frontend lines). That is about 8× inventory, for a problem a spreadsheet covers at 300 endpoints.
- **SLA** (6.6k lines) is bigger than the service desk it serves.
- **Budget** (5.2k lines, with FX rates) duplicates the ERP the company already runs.
- Remote control (2.1k lines) and inventory (1.4k) are among the smallest modules. They are the ones a helpdesk lives in.

The UI shows the same imbalance:
- The console has **36 rail items**.
- The device page, where every incident ends up, has no *Remote in*, *Patch now* or *Open ticket* button. Its one action, **Run job**, offers "Collect inventory" and "Ship agent logs" ([screenshot](2026-09-23-platform-critique/screenshots/device_runjob.png)).
- Five forms ask operators to **paste a device-group ID** under a note saying groups "cannot be browsed from the console yet", while `/device-groups` exists and lists them ([screenshot](2026-09-23-platform-critique/screenshots/deploy_new.png)).
- The search box says *"Find a device, ticket, address…"* but only matches page names (`App.tsx:230-256`) ([screenshot](2026-09-23-platform-critique/screenshots/cmdk_search.png)).
- An end user who signs in lands in the operator console and is told *"This account holds no module read permissions. Ask an administrator for one."* ([screenshot](2026-09-23-platform-critique/screenshots/enduser__dashboard.png)).

Your complaint that it all "looks like DB tables and is too technical" is accurate across most of the console, not only Settings. The plain-language guide has only been applied to Settings.

**Is it worth it?** Not yet, as a replacement for anything. As things stand, Action1 (free up to 200 endpoints), Intune (probably already in your M365 licence) and a plain helpdesk would each do their part better today.

It *can* be worth it, and the reason is specific. It is one self-hosted tool where the thing that fixes the device also writes the evidence. Concretely:
- A ticket shows the device's live health, patch and backup state.
- An alert opens a ticket on its own.
- A change record gates the patch that follows.
- The whole trail comes out as an auditor-ready export.

No single competitor does that for a small team. It only holds once the endpoint layer works on Windows, and until then everything above it has nothing to act on.

**The fix is to narrow, not to add.** Stop building new modules. Make about eight surfaces work on real Windows and Mac machines, connect them into the journeys people actually follow, and freeze the rest.

---

## 2. Where all three reviewers agree

These came up independently in two or three reports and I verified them. Treat them as established.

| # | Finding | Reviewers | Evidence |
|---|---|---|---|
| 1 | Nothing has run on the real fleet (Windows 11 / macOS) | 1, 3 | first-run record blank; `HANDOFF.md:254`; Windows patch engine "has never executed" (`wua_windows.go:17`) |
| 2 | Remote control has no agent half; the console offers it anyway | 1, 2, 3 | `registry.go` has no handler; `wire/schema_test.go:116`; Start Session dialog ships |
| 3 | No M365 integration: mail intake is password-IMAP; no Graph/Intune/Entra/Defender | 1, 2, 3 | `email_intake/imap.py:601` |
| 4 | Scope is inverted: periphery modules are the largest | 1, 2, 3 | IPAM 19.8k lines vs inventory 2.8k; 36 rail items |
| 5 | People have no single identity (device owner, seat holder and licence holder are free text or polymorphic) | 2, 3 | `models/core.py:384`; `subscriptions/models.py:65-66`. No onboarding or offboarding journey is possible. |
| 6 | Change management is advisory only, and its ticket link is off by default | 1, 3 | `change_mgmt/links.py:1-10`; `core/config.py:147` |
| 7 | The kernel's security is real and well enforced | 1, 3 | signing with no no-op signer; FORCE RLS; audit REVOKE + trigger + HMAC chain |
| 8 | Summary docs overstate what works | 1, 3 | `HANDOFF.md:22` ("all 16 job types implemented on both sides") and `docs/product.md` §3 vs the code |
| 9 | Health → ticket → notify is the best cross-module seam, and should be the model | 1, 3 | `health/actions.py:692` |
| 10 | The AI layer is safely built but low value today (2 read tools, never run against a live model) | 1, 2, 3 | `core/ai/tools/registry.py:95-150`; copilot and portal panels are built but never mounted |

---

## 3. Module scorecard

**State** is what the code shows, not what the specs claim:
- **Working**: wired end to end and proven.
- **Unproven**: wired, but never executed on the target OS.
- **Shallow**: works, but lacks what makes the category useful.
- **Half**: one side of it is missing.
- **Spec**: a document only.

**Verdict:** DEEPEN (core; make excellent), KEEP (fine as is), MERGE, FREEZE (keep running, no new features), CUT, or INTEGRATE (use an existing tool).

| Module | State | Verdict | Worth it today? | The one thing that would make it worth it |
|---|---|---|---|---|
| **Devices / inventory** | Shallow, unproven | **DEEPEN (#1)** | No: weaker than every free competitor | Installed-software collection on Windows and macOS, plus a daily scheduled collect |
| **Agent distribution / enrollment** | Unproven | **DEEPEN** | No: never completed on Windows or macOS | Run the first-run checklist on real machines; signed Intune package; agent self-update |
| **Health & alerting** | Server proven; agent unproven on Windows/macOS | **DEEPEN** | Partly: offline alerts work today | A default "workstation baseline" profile applied to every device on enrolment |
| **Patch** | Unproven; OS updates only | **DEEPEN (reposition)** | No | Third-party apps via winget; reboot orchestration; a real Windows run |
| **Deploy + scripts** | Unproven | **DEEPEN** | No | Fix winget under SYSTEM (likely broken: `deploy/plan.go:170`); scheduled script policies; a group picker |
| **Remote control** | Half (no agent side) | **INTEGRATE** | No: cannot connect | Launch Quick Assist, Intune Remote Help or MeshCentral from the device and ticket pages, audited. Hide Start Session until then. |
| **Service desk** | Working core | **DEEPEN**; absorb portal, intake, SLA and change | Partly | Quick actions (Assign to me / Reply & resolve), an SLA column, device actions inside the ticket |
| **Portal** | Working, wrong frame | **MERGE into service desk** | Partly | Its own shell (not the operator console), Microsoft sign-in, the assistant panel mounted |
| **Email intake** | Cannot sign in to M365 | **MERGE into service desk**; fix auth first | No | OAuth or Graph mail (the plan exists; ship cycle B only) |
| **Notification** | Working | **KEEP (kernel)** | Yes | A Teams channel; OAuth/Graph email; route platform-health alerts through it |
| **SLA** | Working, overbuilt | **MERGE + FREEZE** | Overkill | One calendar with targets per priority is enough for this team |
| **Change management** | Advisory | **MERGE into service desk** | No: records, gates nothing | An optional "patch/deploy needs an approved change" policy, enforced at dispatch |
| **Security logs** | Both halves built; never dispatched | **KEEP narrow + schedule it** | No: produces nothing | A scheduled `logs.ship` task (small), or a Graph pull of Entra sign-in logs instead |
| **Backup** | Never completed | **DECIDE** (see §6) | No | One real backup and restore on Windows to an off-host target, or freeze it and report on Veeam/restic/OneDrive |
| **Reporting** | Working, 4 reports | **DEEPEN** | Partly (inputs hollow) | A monthly SOC 2 evidence pack once the inputs are real |
| **Audit** | Working | **KEEP** | Yes | Show names, not IDs, for actors and targets |
| **Credentials / vault** | Working | **KEEP** | Yes | Nothing urgent |
| **Documentation** | Working | **FREEZE → becomes the KB** | Marginal against SharePoint | Suggest articles in the portal and on tickets |
| **Licenses** | Working, no input | **MERGE with subscriptions** | No: nothing to reconcile against | Software inventory + an M365 SKU and seat pull from Graph |
| **Subscriptions** | CRUD | **MERGE with licenses** | Marginal | Renewal alerts through notification (today they only show when the page is opened) |
| **Budget** | Working | **CUT / FREEZE** | No: the ERP owns this | — |
| **IPAM** | Working, largest module | **FREEZE** | Overkill | — (NetBox or phpIPAM if ever needed) |
| **BCP / DR tests** | Manual records | **FREEZE** | Marginal | Default the report to the last quarter; otherwise leave it |
| **Provisioning / imaging** | Paperwork: no delivery, no PXE | **CUT → INTEGRATE** | No | Autopilot / Apple Business Manager, plus a "baseline on first enrol" bundle |
| **Site servers** | Working (Wake-on-LAN, sweep origin) | **FREEZE** | Marginal | — |
| **Push install** | Built; SSH only | **CUT / hide** | No: Windows 11 and macOS ship with no SSH server | Intune and GPO already cover this |
| **AI (admin / copilot / portal)** | Built, 2 read tools, never run live | **MERGE into one + FREEZE** | No | Mount the ticket copilot; give it inventory, patch and ticket tools once those hold real data |
| **MDM** | Spec only (1,733 lines) | **CUT from the roadmap** | — | Intune |
| **Kernel UI** (jobs, settings, platform status, error monitor, setup) | Working | **KEEP**; move config pages into Admin | Yes | See §4 |

**Where the reviewers disagreed, and my ruling:**
- **Security logs.** Reviewer 1: keep narrow, or replace with a Graph pull. Reviewer 3: keep separate; the split is coherent. **Ruling:** keep it, add the schedule (a small job), and decide on Graph later. It is the cheapest SOC 2 evidence you have.
- **Backup.** Reviewer 1: defer, and report on another tool. Reviewer 3: get one real run *or* remove it from the console. **Ruling:** decide within 30 days, and don't leave it as-is. Today it ships enabled and has never completed, which is the worst of both.
- **Documentation.** Reviewer 1: keep, frozen. Reviewer 3: freeze. **Ruling:** freeze, and reuse it as the service-desk knowledge base.
- **Agent self-update.** Reviewer 3 argues the refusal (`agent/packaging/README.md:535-552`) buys no security: `script.run` can already run signed code as SYSTEM, so a signed `agent.update` pinned to a SHA-256 adds no new power, and without it the fleet has no upgrade path. **Ruling:** I agree with the reasoning. It is still a *documented decision*, so it should be reopened deliberately and the doc changed, not worked around.

---

## 4. UI/UX and the user journeys

### The core problem
**The console is organised by module, and people work in tasks.** Every journey below crosses 3 to 6 top-level pages by hand, and nothing links them.

### The journeys, in order of severity

| Journey | Severity | What breaks |
|---|---|---|
| **Alert → investigate → fix** | Critical | The alert panel opens *below* a 17-row table and doesn't link to the device. Alerts are sorted by time, not severity (the only critical alert sits on row 9). The device page has no Remote, Patch now or Open ticket action. Remote has to be started from a separate page by searching for the device again. |
| **Onboard or offboard an employee** | Critical | There is no such workflow. A person's devices, licences, seats and tickets are not linked anywhere, and "Provisioning" means OS imaging. |
| **Deploy an app to a group** | Critical | The device-group field is a pasted ID, with out-of-date help text. "Create" doesn't deploy: each deployment then needs "Execute", which is also offered on finished runs. Times are shown in UTC to an operator in Lisbon. |
| **First-run setup** | High | No getting-started checklist. After claiming, the admin lands on `/devices`. The first password comes only by email, and a fresh install usually has no email set up. Eight or more modules need configuring, with no order. |
| **Enrol a laptop** | High | Six entry points. Platforms appear as `darwin/arm64`. A hard-coded caption tells every operator the Windows agent "has never been run by anyone". Nothing confirms that the new machine checked in. |
| **End user raises a ticket** | High | The requester lands in the operator console and sees a permissions error. There is no Microsoft sign-in. The *New ticket* form itself is the best-written page in the product. |
| **Operator works a ticket** | High | No SLA column in the queue. The ticket page is seven separate forms, with no "Assign to me" or "Reply & resolve". The reply defaults to Public. The copilot is built but not mounted. |
| **Backup and restore** | High | Coverage doesn't list *which* devices are unprotected. Restore is offered on failed and pruned points. Restoring to the original location overwrites files with no confirmation and no warning. |
| **Daily check** | High | Six KPI tiles and no "3 things need you today" list. The managed-endpoint count disagrees with `/devices` (14 vs 10 of 14), which undermines trust in the page meant to be trusted. |
| **Patch a device or group** | Medium | One of the strongest pages. "Rings" are never explained, approving doesn't say *when* updates will install, and schedules are written as cron (`0 2-5 * * 6`). |
| **Use the assistant** | Medium | It works, but it has three names ("Assistant", "Ask the AI Analyst", "AI Automation"). Hostnames in answers aren't links. It is absent from tickets, alerts, devices and the portal. |

### What's good and should be copied everywhere
- The portal urgency picker (*"I cannot work at all, or others are blocked too"*).
- The rewritten Health settings.
- The SLA calendar warning, which explains the consequence rather than the rule.
- The Run-job offline warning, the patch selection bar, and the email-intake outcome log.
- The Remote start dialog (reason required, recording stated).
- Consistent components, no sideways scroll on phones, a skip link, and labelled icon buttons.

The component library is sound. The problems are in the journeys, the page structure and the vocabulary.

### The shape of the fix
1. **Two-layer navigation.** About 8 daily destinations in the rail: Dashboard, Devices, Alerts, Tickets, Patching, Software, Backup, Assistant. Everything that is configuration goes into one **Admin** area: Notifications, Email, SLA, Site servers, Agent packages/Push/Enrollment, AI automation, Platform status, Error monitor.
2. **The device page becomes the incident hub.** The header shows status, open alerts, owner, and patch/backup/cert state. Its actions are Remote in, Run fix, Install updates, Restart, Back up now and Open ticket. The same actions appear in the alert drawer and on the ticket.
3. **A "Needs you" list on the Dashboard**, ranked across alerts, breaching tickets, failed jobs, unprotected devices and expiring certificates, each with its action.
4. **The portal gets its own shell**, without the operator rail, and requesters land on *My tickets*.
5. **Apply the language guide everywhere.** No wire names (`patch.apply`, `cpu.busy_pct`), permission codes, cron strings or architecture subtitles in primary text. Add a lint test for known wire tokens in JSX, like the existing `noVendorNames.test.ts`.

---

## 5. Platform and operations

**What running it costs one person today:**
- **16 services and a 3,467-line runbook.**
- **OpenBao comes up sealed after every reboot**, and until someone enters 3 of 5 key shares, job signing and the vault are dead. A power cut is a manual incident.
- **Keycloak realm drift:** config changes in the repo never reach the live host, and each one is fixed by hand with `kcadm`.
- **Platform backups are manual.** They sit on the same host as the endpoint backups (MinIO), and the OpenBao and Keycloak restore has never been rehearsed.
- **Nothing alerts anyone when the platform itself is sick**, although the notification module is sitting right there.

**Fixes, all small to medium:**
- An optional OpenBao auto-unseal (a key file on a separate mount or TPM).
- Platform-health probes routed through `notification`.
- A systemd timer for `backup.sh` plus an off-host mirror, with a monthly scripted restore drill.
- keycloak-config-cli in `keycloak-init`, owning an explicit allowlist of settings.

**Supply-chain risks (reviewer 3; not checked from the sandbox):**
- MinIO is pinned to a 2025 community image. It is AGPL, the community edition is reportedly no longer maintained, and that conflicts with the "avoid AGPL" posture.
- `redis:7-alpine` floats onto versions licensed under RSAL/SSPL.
- Candidates: Valkey for Redis, and Garage or SeaweedFS for MinIO.

**Process cost.** About 35% of the Python and 41% of the Go lines are comments. The docs total 97k lines of Markdown, and in the last month 118k Markdown lines were added against 307k lines of code. The rigour has caught real bugs. But it pins *counts* (routes, policies, call sites) rather than *outcomes* ("a Windows machine ran a signed job"), and summary docs have drifted from the code.

One afternoon with a Windows VM would retire more uncertainty than a month of specs. Proposal: cap spec and plan length, keep history in commit messages rather than code comments, and make "ran on the real OS" the definition of done for endpoint work.

---

## 6. The unified plan

### Now: days 0–30. Make it manage the real fleet
1. **Run the first-run verification on one Windows 11 and one Mac** (a VM is fine to start). Fill in the record, fix what breaks, and commit the captured real outputs as test fixtures. *(All three reviewers rank this #1.)*
2. **Software inventory** on Windows (registry Uninstall keys + Appx) and macOS (`/Applications` + `pkgutil`).
3. **Scheduled collection:** `inventory.collect` daily and on first contact; `logs.ship` on the policy interval.
4. **Fix winget under SYSTEM**, and verify it on the VM.
5. **Hide what cannot work.** Start Session (until remote is integrated), Push install, and the "never been run" caption once it is no longer true. Correct `docs/product.md` §3 and `HANDOFF.md:22`.
6. **Ops safety net:** auto-unseal option, platform-health alerts, a scheduled off-host platform backup.
7. **UI quick wins** (under a week in total; see the list below).
8. **Decide on backup:** one real run and restore, or freeze it and report on an external tool.

### Next: days 31–60. Close the helpdesk loop
9. **Remote by integration** (Quick Assist / Remote Help / MeshCentral), launched and audited from the device and ticket pages.
10. **M365 mail:** ship OAuth cycle B (or Graph mail) so intake works. Add Microsoft sign-in for requesters.
11. **One person identity:** `devices.primary_user_id` from the last logged-on user; seats and licences reference `users`; a *Person* page listing their devices, licences, seats and tickets. Then **New starter / Leaver checklists** on top.
12. **The device page as incident hub**, and the alert drawer with actions.
13. **Service desk:** quick actions, an SLA column, the copilot mounted, and the portal's own shell.
14. **Signed Intune Win32 package** and a rollout in rings (10 → 50 → all).
15. **Agent self-update as a signed job.** Reopen the documented decision first.

### Later: days 61–90. Evidence and depth
16. **Third-party patching** via winget for the top ~20 apps, plus reboot orchestration in maintenance windows.
17. **Scheduled script policies** (script × group × schedule, with exit-code alerts).
18. **Change gating:** turn on the ticket link by default, plus an optional "requires an approved change" policy.
19. **A monthly SOC 2 evidence pack** from real data: patch compliance, change records, privileged access, and a DR test.
20. **Navigation restructure:** the Admin area, a record-searching global search, and the dashboard "Needs you" list.
21. A rehearsed full-platform restore, timed. Replace MinIO and Redis.

**Exit test at day 90:** every Toeshee endpoint enrolled, and one real ticket that:
- came in by M365 email,
- showed the user's device,
- got a remote session and a patch,
- is fully traceable in the audit chain.

On top of that, one restore performed from backup.

### Freeze now (keep running, no new features)
IPAM, SLA, budget, BCP, site servers, documentation, provisioning/PXE, push install, the three AI modules (merge them into one when next touched), and the MDM spec (remove it from the roadmap).

### Quick wins: small, safe, visible (each under a day)
- The dialog close button has no icon (`frontend/src/components/ui.tsx:702`). Every dialog in the console shows an empty box.
- Replace pasted device-group IDs with the existing group list. Delete "cannot be browsed yet" in `DeployPage.tsx:644`, `ScriptsPage.tsx:479` and `UsersTab.tsx:605` (plus the Backup and BCP editors).
- Change the search placeholder to match what it does (pages), until record search exists.
- Make the dashboard managed-endpoint count match `/devices` (`DashboardPage.tsx:467-498`).
- Send users with portal-only permissions to the portal, not `/devices` (`App.tsx:1160`).
- Use the friendly job names from `/jobs` on the Dashboard instead of `patch.apply`.
- Sort alerts by severity. Disable Execute on finished deployments and Restore on failed or pruned points. Add a confirmation before an original-location restore.
- Fix two rail items being highlighted at once on nested routes.
- Resolve names in the Audit log (actors and targets).

---

## 7. Questions only you can answer

These change several verdicts above:
1. **Does Toeshee's M365 licence include Intune, Defender and Entra P1** (Business Premium, E3+EMS or E5)? If yes, OS patching, compliance, provisioning and mobile belong to Intune, and ITMS should *read* them via Graph rather than rebuild them. If no, patch and posture become more important to own.
2. **Which remote tool would you accept?** Quick Assist is free and needs a user present; Intune Remote Help is a paid add-on; MeshCentral is self-hosted.
3. **Backup scope:** do you need endpoint file backup, given OneDrive Known Folder Move covers user folders? Or is the real need server backup and M365 data backup, which this module doesn't address?
4. **Commercial intent and timing.** If selling is more than a year out, the commercial gaps below can wait: multi-tenant MSP console, a LICENSE file, billing, installer. Internal value should come first.

Separately, Microsoft has announced the retirement of basic authentication for SMTP AUTH as well as IMAP. Your SMTP login works today, but check your tenant's status. OAuth cycle B is also the hedge for outgoing mail.

---

## Appendix A: claims I re-verified against the code

| Claim | Where confirmed |
|---|---|
| The agent has no `remote.session.open` handler | `grep -rn "remote\.session" agent/` returns nothing; `agent/internal/wire/schema_test.go:116` |
| No installed-software collection | `agent/internal/capabilities/inventory/facts_windows.go:34-36` |
| No inventory or logs job in the worker schedule | `backend/app/worker.py:2443-2600` (schedule listed in full) |
| `HANDOFF.md` claims all 16 job types on both sides | `HANDOFF.md:22` |
| Backup has never completed | `HANDOFF.md:398` |
| Fleet is Windows 11 + macOS only | `HANDOFF.md:254` |
| Global search matches page names only | `frontend/src/App.tsx:230-256` (the code comment says so) |
| "Device groups cannot be browsed" while `/device-groups` exists | `DeployPage.tsx:644`, `ScriptsPage.tsx:479`, `UsersTab.tsx:605`; `App.tsx:1113` |
| Dialog close button has no icon | `frontend/src/components/ui.tsx:702` |
| Catch-all route sends everyone to `/devices` | `frontend/src/App.tsx:1160` |
| Requester sees "no module read permissions" | screenshot `enduser__dashboard.png` |

## Appendix B: what no reviewer could verify
- Runtime behaviour on Windows and macOS. There is no Windows or macOS in the sandbox, so the winget-under-SYSTEM and Apple Silicon `softwareupdate` concerns are inferences from how the platforms behave.
- The live host's enrolled devices, and whether the production role picker offers the requester role.
- Real loading, error and 403 states. Fixture mode answers instantly and grants every permission.
- The full backend suite (about 25 minutes). A 100-test security sample passed; the frontend and Go suites passed in full.
- MinIO and Redis licensing specifics, taken from upstream 2025 announcements.
- Whether RDP-via-guacd would log off the Windows user. That is standard Windows behaviour, not observed here.

## Appendix C: the screenshots
Ten key screenshots are in [`2026-09-23-platform-critique/screenshots/`](2026-09-23-platform-critique/screenshots/). They were taken in fixture mode, so the data is invented but the layout and copy are real. The reviewer's full set of 117 is referenced by filename in reviewer 2's report.
