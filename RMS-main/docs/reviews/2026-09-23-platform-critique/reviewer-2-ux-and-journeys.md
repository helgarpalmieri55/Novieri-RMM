# ITMS: UI/UX and user-journey critique (Reviewer 2)

**Scope:** the operator console, the end-user portal and the sign-in theme, reviewed on 2026-09-23.
**Method:** I ran the Vite dev server in fixture mode (`?__fixtures`) and used Playwright/Chromium to screenshot 57 routes at 1440×900 and 13 routes at 390×844. I clicked through the key dialogs and the journeys below. To see the requester's view, I simulated a user holding only `portal.ticket.*` by rewriting the fixture `me()` in the browser; no repository file was changed. Where a finding depends on code rather than pixels, I read the source to confirm it.
**Screenshots:** `/tmp/claude-0/-home-user-Toeshee-RMS/4c46d9ba-f00d-5183-8e75-98cd0cc995e8/scratchpad/critique/shots/`, with crops in `../crops/`. No route logged a console error or page error at either viewport, and no route scrolls sideways at 390px.

---

## Executive summary

1. **The console is organised around modules, but people work in tasks, and nothing connects the two.** The rail has **36 items in 7 groups**. The device page, where an operator ends up during almost every incident, has no *Remote control* action and no *Patch now*. Its one primary action, **"Run job"**, offers only "Collect inventory" and "Ship agent logs". An alert has no link to "fix it", "remote in" or "open the device". Every journey below crosses 3 to 6 top-level modules by hand.
2. **Raw IDs still reach the screen, and in several core forms the operator has to type them.** Software deployment, script runs, backup policies, BCP plans and user access grants all take a **pasted device-group ID**. The help text says *"Device groups cannot be browsed from the console yet"* or *"No endpoint serves device groups yet"*, but `/device-groups` exists and lists them. The Audit log shows actors and targets as IDs (`usr-helgar`, `dev-1`; UUIDs in production), which defeats its main reader, the auditor.
3. **The global search does not search anything except page names.** The placeholder says *"Find a device, ticket, address, or ask a question"*. Typing `por-ws` returns *"No records match that"* (`cmdk_search.png`). `App.tsx:230-256` confirms it only searches page names.
4. **The end-user portal is the operator console with most of it hidden.** A requester lands on `/devices`, which their permissions cannot read (`enduser__.png`). They see a dark admin rail with "Dashboard" and "End-user Portal", a search bar offering "Find a device… address", and a Dashboard that says *"This account holds no module read permissions. Ask an administrator for one."* (`enduser__dashboard.png`). The portal assistant (`PortalAssistantPanel.tsx`) is built but not mounted anywhere.
5. **The language rule is applied in patches, not everywhere.** `docs/design/settings-language.md` bans "flap", "suppression", event names and wire values. The Health page still says *"Blocked: flapping: 4 opens in the last 3600 s (threshold 3); actions suppressed until the cooldown ends"*, *"5 min tick / 1 h posture"*, *"Fuses"* and *"Script destructive-exemptions"*. The Dashboard lists jobs as `patch.apply` and `logs.ship`, even though `/jobs` already maps them to "Apply patches" and "Ship agent logs". Reports print *"Requires: devices.read, patch.read"*.
6. **Architecture shows through the copy.** Subtitles tell the operator how the system is built instead of what to do: *"Live inventory from the outbound-only agent"*, *"Asks the platform, never the endpoint"*, *"Both paths end at a device identity issued to a keypair the machine generated itself"*, *"a sweep is documentation currency, not monitoring"*. The nav has "Signed Job Engine", "Immutable Audit" and "SLA Engine". The operator's "DB tables / too technical" complaint is accurate across most of the console, not only in Settings.
7. **Daily work and one-time configuration share a page.** `/health` is 3,930px tall: the alert queue, then reporting, profiles, approved actions, script exemptions and org settings. Health settings also appear in Settings → Health. Notifications (channels, allowlist, dead letters) is a top-level *Service* destination and also a Settings tab.
8. **The Dashboard reports numbers but does not tell anyone what to do.** It has six KPI tiles, donuts and lists. There is no "3 things need you" list, no link from a red 0% macOS patch donut to the machines, and it contradicts `/devices`: **"Managed endpoints 14 … of 14 enrolled"** here, **"Managed endpoints 10 of 14 assets"** there (`DashboardPage.tsx:467-498` counts every asset, including printers and switches).
9. **Some journeys dead-end for Toeshee's own setup.** Email intake supports only IMAP with a username and password, which a Microsoft 365 tenant blocks by default. There is no employee onboarding or offboarding workflow anywhere, even though the vision document lists "imaging new-hire machines". Sign-in has no Microsoft button yet, so every end user needs another password.
10. **Some of the work is genuinely good and should be copied across the console.** The portal's urgency picker (*"I cannot work at all, or others are blocked too"*), the rewritten Health settings (durations, plain help), the SLA calendar warning (*"This calendar has no working windows, so no deadline can be computed…"*), the Run-job offline warning, the patch selection bar, the email-intake dispositions, the skip link, no sideways scroll on mobile, and one consistent visual system. The components are sound; the gaps are in the journeys, the page structure and the vocabulary.

---

## 1. Information architecture and navigation

**The rail** (`frontend/src/shell/nav.ts`) has 36 items:

| Group | Items |
|---|---|
| Overview | Dashboard, Assistant |
| Fleet (9) | Devices & Inventory, Add Devices, Device Groups, Health & Alerts, Security logs, Patch Management, Software Deployment, Remote Control, Credentials |
| Continuity | Backup & Restore, Provisioning & Imaging, Site Servers |
| Service | Service Desk, End-user Portal, Notifications, Email Intake, SLA Engine |
| Governance | IT Documentation, Change Management, BCP / DR Tests, Reporting & Evidence, Immutable Audit |
| Assets & Spend | Licenses, Subscriptions, IT Budget, IP Address Management |
| Platform (8) | Signed Job Engine, Enrollment & Certificates, Agent Packages, Push Install, AI Automation, Platform Status, Settings, Error Monitor |

Problems:

- **Too many peers.** A team of "a handful of operators" with 250–300 endpoints needs about 8 daily destinations: Dashboard, Devices, Alerts, Tickets, Patching, Software, Backup and Assistant. The other 28 items are configuration, governance or platform operations, and they get the same visual weight. At 1440×900 the rail cuts off after *Email Intake*, so Governance, Assets & Spend, Platform and **Settings** are below the fold (`devices.png`).
- **Configuration is spread across the rail.** *Notifications*, *Email Intake*, *SLA Engine*, *Site Servers*, *Push Install*, *AI Automation*, *Enrollment & Certificates* and *Agent Packages* are all settings-type pages at top level. Settings itself says *"Other modules are configured from their own page."* (`settings.png`). An operator looking for "where do I change X" has 10 possible places.
- **Six ways to add a device.** Top-bar *Install agent*, *Devices → Add asset / Enroll device / Add devices*, rail *Add Devices*, *Enrollment & Certificates*, *Agent Packages* and *Push Install* (`devices.png`, `devices_onboard.png`). These are not six different tasks, but they are presented as six places.
- **Grouping mistakes.** *Credentials* sits in Fleet but is a vault. *Security logs* sits in Fleet. *End-user Portal* sits in the operator rail as a destination for operators. *Error Monitor* and *Platform Status* are server-operator pages inside a tenant console. *IPAM* is under "Assets & Spend", next to Budget.
- **Some item names describe the machinery instead of the task:** "Signed Job Engine", "SLA Engine", "Immutable Audit", "Enrollment & Certificates". An operator would say "Job history", "Service levels", "Audit log", "Add a computer".
- **The same feature has three names.** The rail says *Assistant*, the search dropdown and top-bar button say *"Ask the AI Analyst"*, and the Platform group has *AI Automation*. The spelling "artifact" appears in Deploy and "artefact" on Agent packages.
- **Two rail items are highlighted at once** on nested routes. On `/assistant/autonomy` both "Assistant" and "AI Automation" are active. On `/devices/onboard` both "Devices & Inventory" and "Add Devices" are active (checked from the DOM's `aria-current` state).
- **Breadcrumbs name only the group** ("ITMS › Fleet"). On a device page there is no "Devices › por-ws-008" trail; the page offers an "All devices" button instead.
- **Tab state is inconsistent.** Settings tabs live in React state, so a reload returns to *Organization*; the code comments admit this. Other modules use real routes (`/deploy/packages`).

**Recommendation:** use two layers. Put about 8 task destinations in the rail, and move configuration (Notifications, Email, SLA, Site servers, Agent packages/Push/Enrollment, AI, Platform status, Error monitor) into one **Admin** area with its own sub-navigation. Governance and Assets & Spend can each collapse to a single entry.

---

## 2. Journey walkthroughs

Severity: **Critical** (the journey cannot be finished, or finishing it depends on something the UI does not provide), **High** (finishable, but only by someone who knows the system), **Medium** (friction), **Low** (polish).

### a. First-run setup of a new installation. Severity: High

1. **The Setup page** (`modules/setup/SetupPage.tsx`, reviewed from source because fixture mode bypasses `SetupGate`). It asks for install code, name, email and organisation, then *"Finish setup"*. The copy is honest and good: *"You will be sent a link to choose a password. No password is entered here."*
2. **Friction: invitation by email is the only way to get the first password.** The code's own comment says *"A fresh installation usually has no email configured, so the invitation is the step most likely to fail."* When it does fail, the page says so, but it offers no alternative such as a one-time link printed to the server log. The first administrator can be stuck on the last screen of the install.
3. **After signing in** the catch-all route sends the admin to `/devices` (`App.tsx:1160`), not to the Dashboard and not to a checklist. The console has **no getting-started checklist**. The only "Getting started" steps component is in the dev-only `/__preview` harness (`PreviewPage.tsx:1112`). Settings shows small **"SET UP"** tags on Email, Service levels and Health, which are the only hint of required setup, and they sit inside a page that is itself below the fold of the rail.
4. **What a new admin actually has to do:** configure email (Settings → Email), create queues and categories (Service Desk → Admin), set SLA calendars (SLA Engine), register networks (IPAM), publish agent packages, create enrollment tokens, create groups, write patch policies and backup repositories. That is 8 or more modules with no ordering and no progress indicator.

*Fix:* a post-claim **Setup checklist** on the Dashboard with 6–8 steps, each linking to the page that completes it and each ticking itself from real data. *Add Devices* (`OnboardPage.tsx`) already implements this pattern for networks, so it can be reused.

### b. Enrolling a new laptop / deploying the agent. Severity: High

1. The operator clicks top-bar **Install agent** and lands on **Agent packages** (`agent-packages.png`). Rows are labelled `darwin/arm64`, `linux/amd64`, `windows/amd64` (Go target names, not "macOS (Apple silicon)" or "Windows 64-bit"). Each row has two buttons, *"Install command…"* and *"Managed deployment…"*, and a caption **"Never executed: Cross-compiled and signed, but the Windows service has never been run by anyone."** That caption is hard-coded in `AgentReleasesPage.tsx:338-351`, not fixture data, so every production operator will read that the Windows agent has never been run.
2. *Install command…* opens a dialog titled **"Generate install command"**: *"This mints a single-use enrollment token now and embeds it in the command as a literal."* (`agent_install_cmd.png`). "Mints" and "as a literal" are internal terms. The flow itself (one click to get a one-machine command) is right.
3. *Managed deployment…* (`agent_managed.png`) is a long technical dialog: curl, sha256sum, SYSVOL. It also says *"There is no MSI, so GPO's Software Installation policy is not the mechanism."* That is reasonable for an administrator, but Intune (which Toeshee on M365 is likely to use) is only a dropdown option below the fold.
4. The alternative entry, **Add devices** (`devices_onboard.png`), starts with *"This platform never connects to a machine, so the first machine on a new segment is always a manual install."* The common case, *"Already have the machine in front of you?"*, is at the bottom of the page and sends the operator to two more pages. Step 4 is a 10-line paragraph ("A ping reply is not a machine: a sweep returns printers, phones, a colleague's laptop on the wrong VLAN and the coffee machine…").
5. After install, **nothing in the console confirms "your new laptop just checked in".** There is no waiting state and no toast; the operator has to go and find the device in the list.

*Fix:* one **"Add a computer"** wizard: choose OS in plain words, get a command or download, then a live *"Waiting for it to check in…"* panel that turns into the device page. Put sweep and IPAM behind "Add many computers on a network".

### c. "A device is unhealthy": alert → investigate → fix. Severity: Critical (the loop does not close)

1. **Dashboard → Active alerts** shows "CPU busy · lis-app-02 · 96 over 95". The unit is missing (percent). *"No contact from the agent · por-fs-01 · 26h over 900"* compares hours with a bare number of seconds (the display string comes from the server, so this may be partly fixture).
2. **Health & Alerts** (`health.png`, `crops/health_top.png`). Rows are sorted by recency, not severity: the only **Critical** alert (DB disk at 3.1%) is **row 9**. There are no inline Acknowledge or Mute actions; every row has "Open".
3. **Open** does not open a drawer or a new page. The alert panel renders **below the whole 17-row table**, and the page scrolls to it (`alert_open_viewport.png`, `alert_detail.png`). The panel shows `check service.running`, lowercase step names **"notify"** and **"remediate"**, *"This alert key is repeating, so the ACTION was paused"*, and **"Blocked: flapping: 4 opens in the last 3600 s (threshold 3); actions suppressed until the cooldown ends"**. Almost every word there is on the banned list in `settings-language.md`. Controls: Acknowledge, "Mute for (hours)" as a raw number box, and **Close manually** in red.
4. **The panel does not link to the device, and has no Remote in, Run the fix now, Open ticket or Ask assistant action.** The only way on is typing the hostname somewhere else.
5. **Device page** (`devices_dev-4.png`): the device is offline and its certificate expires in 3 days, but the header does not say "this device needs attention" or show the open alert count. The default tab is **Hardware**, and **Health** is the last of 10 tabs. The primary action is **Run job**, whose dialog offers only *Collect inventory* and *Ship agent logs* (`device_runjob.png`). The dialog's close button is **an empty box**: `components/ui.tsx:702` renders `<button className="close-btn" aria-label="Close" />` with no icon, and every Dialog in the console shares it.
6. **Remote control cannot be started from the device page.** I searched `DeviceDetailPage.tsx`; there is no remote action. The operator goes to *Remote Control → Start session* and searches for the device again (`remote_start.png`). That dialog is good: it requires a reason, it is clear about attended versus unattended, and it tells you the session will be recorded.
7. The **Health tab** on the device (`device_health_tab.png`) is informative ("Last contact … outside the 15 min offline window"). It shows check keys in monospace (`cpu.busy_pct`, `posture.antivirus_healthy`), and the text *"Absent is not healthy: there is no evidence either way yet"* is repeated six times.

*Fix:* make the device page the incident hub. The header shows status and open alerts; the actions are Remote in, Run fix/script, Install updates, Restart and Open ticket; alerts open in a drawer with those same actions and link to the device.

### d. Patching a device or group. Severity: Medium

1. **Patch management** (`patch.png`) is one of the stronger pages. It has per-OS tabs, updates listed by severity with devices missing, machines listed worst first, and an approvals list.
2. Selecting an update brings up a clear selection bar: *"1 update selected of 5 matching. [test, 4 devices] Approve 1 update"* (`patch_selected.png`). But **"rings"** (test/pilot/broad) are never explained on the page, and approving does not say **when** the update will install. That is decided on *Patch policies* (`patch_policies.png`), which uses *"Window (UTC)"* and cron (`0 2-5 * * 6`).
3. Explanatory paragraphs sit in the working area: *"Severity here is Microsoft's MSRC rating, and most Windows 11 cumulative updates carry none; they arrive as 'unknown'. That is why this tab does not narrow by severity…"*.
4. There is **no "Patch this device now"** on the device page. The device's Patch tab and the machines list offer *"Show 4 updates"*, then approve per ring.
5. Compliance bars colour by band ("green at 90%…"), which a sentence has to explain. *"Critical 77.8%"* shows gold and *"Important 33.3%"* red, which reads backwards at a glance.

### e. End user raises a ticket → operator works it → SLA. Severity: High

1. **Sign-in** (Keycloak theme, from source). It is well built: fields render only when the flow asks for them, and passkey and TOTP pages use plain language (*"6 digits, from your authenticator app"*). But there is **no Microsoft sign-in yet**; the template notes *"this theme can ship before the Microsoft provider exists"*. For an M365 company, end users need another password. The footer *"Authorized access only. Activity is monitored and audited."* is addressed to administrators and is off-putting for someone reporting a broken printer.
2. **Landing:** the requester is redirected to **`/devices`** (`enduser__.png`; the catch-all route is `<Navigate to="/devices">`). In fixture mode the device data appears because fixtures do not enforce permissions; in production this is a permission error page. **Dashboard** says *"This account holds no module read permissions. Ask an administrator for one."* (`enduser__dashboard.png`). The rail labels the requester's own page **"End-user Portal"**.
3. **New ticket** (`enduser__portal_new.png`, `portal_new-m.png`) is the best form in the product: *"Tell us what is wrong. You do not need to know who will fix it."*, a guided description placeholder, and four urgency choices in human terms. Missing: nothing says which fields are required, there are no self-help articles, no "this is about my laptop" link, and the built portal assistant is not shown.
4. **Email path.** *Email intake* (`email-intake.png`) supports **IMAP with username and password only** (`MailboxForm.tsx`; no OAuth or Graph option). Exchange Online disables basic authentication for IMAP, so Toeshee's own support mailbox probably cannot be connected as built. The intake log itself is very good: every message shows its outcome ("Quarantined: not trusted — reply token did not verify", "Refused: mail loop detected").
5. **Operator queue** (`service-desk.png`). Clean. **But there is no SLA column**: the code (`TicketTable.tsx:26`) says the list API has no deadline. So "which ticket breaches next?" cannot be answered from the queue. The *"Show all tickets, not just open ones assigned to you or unassigned"* checkbox sits below six filter dropdowns and four quick-filter chips.
6. **Ticket detail** (`ticket_detail.png`) is **seven form panels**, each with its own submit button: *Post comment*, *Save fields*, *Change status*, *Assign*, *Unassign*, *Move queue*, *Attach a file*. There is no **Assign to me**, no one-click **Resolve**, and no "Reply and resolve". The reply box defaults to **Public** even on an alert-generated ticket with no requester. SLA shows `pol-high` and `cal-standard` slugs. The linked device has no *Remote in* action. `TicketCopilotPanel.tsx` exists but is **not rendered anywhere**.
7. **SLA engine** (`sla.png`) is clear, and the calendar warning is a model of explaining consequences. "Auto-close resolved tickets after (days)" is still a raw number field.

### f. Onboarding and offboarding an employee. Severity: Critical (not supported)

- There is **no employee lifecycle feature**. *Provisioning & Imaging* (`provisioning.png`) images machines ("from bare metal to enrolled endpoint"). *Settings → Users* (`settings_users.png`) manages console users, and its role checklist (Auditor, Finance, Org admin, Service desk, Technician) **does not include the requester role** that the portal migration creates (`0028_portal.py`), at least in the fixture. How an end user gets a portal account is not visible anywhere in the UI.
- Offboarding means going through Users (*More actions → Disable*), Devices (reassign or retire; no owner field is editable in the list), Licenses and Subscriptions (seat reclaim; *"No owner 2"*), Credentials and Remote by hand. No page links them. A new hire is the same in reverse: image the machine, create the user, assign licences, add to a group.
- For an M365 shop this is the most common IT task after tickets, and the product has no workflow for it. At minimum add a **"New starter" / "Leaver" checklist** that links the existing modules around one person, and show a person's devices, licences and subscriptions on one page.

### g. Deploying an app to machines. Severity: Critical for group targets

1. **Packages** (`deploy_packages.png`). You upload your own MSI or pkg; there is no catalogue. Rows show `sha256 9f3a…3c5e` as the subtitle, and every row has a red **Delete**.
2. **New deployment** (`deploy_new.png`). *Target type: Device / Device group*. For a group, the field is a text box with placeholder **"Device group id"** and the note **"Device groups cannot be browsed from the console yet. Paste the group id, or target a single device instead."** (`DeployPage.tsx:639-645`). `/device-groups` exists and lists named groups (`device-groups.png`), so this copy is out of date and the task is harder than it should be. The same applies in `ScriptsPage.tsx:474-479`, `BackupPolicyEditor.tsx:330` (*"Device group ID (blank = org-wide)"*), `bcp/PlanEditor.tsx:342` (placeholder `06a85c19-55e2-7f3c-8000-831c7bdba357`) and `settings/UsersTab.tsx:604` (*"No endpoint serves device groups yet…"*).
3. The window grid is labelled **"Window (cron, UTC, optional)"**, but it is a painter, and "UTC" is shown to an operator in Lisbon. *"Nothing painted: the deployment dispatches as soon as it is executed."*
4. **Create** does not deploy. The row then needs **Execute**, and every row shows *Execute*, including *Complete* and *Failed* ones (`deploy.png`). The Target column shows the raw group ID (`grp-workstations` here; a UUID in production; `DeployPage.tsx:416`).
5. There is no per-device progress view from the list, and no "retry failed devices".

### h. Backup and restore of a device. Severity: High

1. **Backup & Restore** (`backup.png`) lists restore points, not devices. The tile *"Protected endpoints 7 / 14"* does not link to **which 7 are unprotected**, which is the question the page exists to answer.
2. Every row has **Restore**, including a **Failed** point and a **Pruned** point.
3. **Restore dialog** (`backup_restore.png`): a file list with raw paths, "Original location / Alternate location" radios, **Verify** and **Restore**. It does not say that *Original location* overwrites the user's current files, and there is no confirmation step before a restore to the original location. The destructive default is not marked.
4. *"Verification"* versus *"Restore test"* (Verified/Not verified versus Tested/Not tested) are two columns that most operators will read as the same thing.
5. The device page has a Backup tab, but there is no "Back up now" or "Restore a file for this user" starting from the device or the ticket.

### i. Daily check: "is everything OK?" Severity: High

- **Dashboard** (`dashboard.png`, `dashboard-m.png`): six KPI tiles, "Fleet at a glance", Service desk counts, Recent devices, Active alerts, Recent signed jobs (raw `patch.apply`, `health.check.now`), Patch donuts, Backup donuts and Upcoming changes. It is decorative rather than useful: there is **no single verdict** ("2 things need you today") and no ranked to-do list. The red 0% macOS donut links to "Open patching", not to the machines.
- The **subtitle** *"Figures from every module this account may read."* describes permissions, not the fleet.
- **The numbers disagree with `/devices`** (see summary item 8), which damages trust in the page that is supposed to be the trusted one.
- On mobile the dashboard is **4,582px tall**, and six tiles stack one per row before any list.
- The top-bar health and status glyphs (`HealthGlyph`, `PlatformGlyph`) are icon-only on desktop. They have good aria-labels, but a sighted user has to learn what they mean.

### j. Using the AI copilot or assistant. Severity: Medium

1. **Assistant** (`assistant.png`, `assistant_ask.png`). A clean question box with good example prompts. The answer is sensible, but hostnames come back **bold, not linked**, and the answer ends with the tool trace **`devices.list · 14 records`**. The subtitle *"Asks the platform, never the endpoint."* is architecture.
2. **Ask from the search bar** works: every query offers *"Ask the AI Analyst"*, which is a third name for the feature.
3. **The assistant is missing where it would help most:** it is not on the ticket (`TicketCopilotPanel` is not mounted), not on the alert, not on the device, and not in the portal (`PortalAssistantPanel` is not mounted).
4. **AI automation** (`assistant_autonomy.png`) shows internal values next to labels: `off`, `suggest`, `require_approval`, `end_user`, "persona", *"Confirming dispatches a signed job through the unmodified job engine"*, *"the database refuses a row for this persona"*. **Assistant actions** (`assistant_actions.png`) shows *Catalog key* `patch.approve.ring`, *Job type* `patch.approve`. **Assistant settings** is mostly a disclosure essay. This is the right content for an administrator, but it is written at the level of the implementation.

---

## 3. Cross-cutting heuristics

### Plain language vs jargon (real strings on screen)
- **Wire and internal values shown verbatim:** `patch.apply` (Dashboard); `service.running`, `cpu.busy_pct`, `agent.offline` (Health, device Health tab); `inventory.collect`, `script.run`, `fs.clean_temp` and *"A person holding health.remediate must approve"* (Approved actions); `pol-high`, `cal-standard` (ticket SLA); *"Requires: devices.read, patch.read"* (Reports); `darwin/arm64` (Agent packages); `usr-helgar` (Health exemptions *Granted by*, Audit actors); `grp-servers` ("Technician · limited to grp-servers" in Users); `require_approval` (AI automation).
- **Mechanism instead of consequence:** "tick / posture", "Fuses", "flapping… suppressed", "dead-lettered deliveries", "egress guard", "mints a single-use enrollment token … as a literal", "Tenant identity and the settings keys this server admits" (Settings), "Configuration that applies everywhere in this tenant".
- **Good examples to copy:** the Health settings form (`health_bot.png`) and the portal urgency choices, which show the language guide works where it has been applied. The guide has not been applied beyond Settings.

### Empty, loading and error states
- The empty states are consistently designed and honest ("No token is being issued — Choose Create token to set a name, a lifetime and a use limit"; "No period chosen"; "No attachments yet").
- But an empty state aimed at the wrong reader is worse than none: *"This account holds no module read permissions. Ask an administrator for one"* is shown to an end user.
- Error copy is good where I found it: the Run-job offline warning (*"The job will sit in the queue until it checks in, and the console cannot cancel a queued job"*) and the Setup failure paths.
- I could not see loading or error states in fixture mode (everything resolves instantly). See "Not verified".

### Density vs clarity
- Nearly every page follows the same template: 3–4 KPI tiles, then a table. The tiles often count obvious things ("Platforms 3", "Releases 5", "Types in use 4", "Registered modules 7") and push the actionable table below the fold, especially on mobile.
- Explanatory paragraphs sit inside working areas (Patch, Add devices, Health reporting footnote, Push authorizations warning, Managed deployment).
- Long "god pages" mix operations and configuration: Health (3,930px), Notifications (2,778px), Budget (2,971px, with a 36-row variance table of mostly `0.00` and `-100.0`).

### Consistency
- Timestamps are absolute and monospace in some places ("23 Sept 2026, 01:59") and relative in others ("38m ago", "26h ago"), sometimes on the same page (device header).
- Three names for the AI feature, and "artifact" versus "artefact".
- Destructive styling is used inconsistently. **Deactivate**, **Revoke** (patch approval) and **Delete** (a package) are all bordered red buttons at equal weight on every row, while the one genuinely destructive default (restore to the original location) is not marked at all.
- Every Dialog has an **empty close box** (`components/ui.tsx:702`). The Drawer passes an icon child; Dialog does not.

### Dangerous actions
- Red "Delete" appears on nearly every table row: packages, SLA policies, calendars, channels, rules, mailboxes, profiles and credentials. Warnings stop registering when everything is red.
- Row actions that should be disabled are offered: *Execute* on completed deployments, *Restore* on failed or pruned backups, *Run job* on printers and switches with no agent (`devices.png`).
- Good: Remote start requires a reason and states that the session is recorded; Credentials "Check out" says the reason is written to the audit log.

### Accessibility
- **Good:** skip link first in tab order; labelled icon buttons in the top bar ("Health and alerts, 12 needing attention"); status carried by icon and text as well as colour; coarse-pointer target sizes; no sideways scroll at 390px on any route tested.
- **Issues:**
  - The empty Dialog close button shows no visible glyph; its accessible name is still present.
  - The alert panel opening far below its trigger is a focus and context problem.
  - Monospace is used heavily for dates and numbers, which slows reading.
  - Rail items keep double `aria-current`.
  - Native date inputs render `mm/dd/yyyy` (`bcp_report.png`, `changes_calendar.png`) even though the org is set to day/month/year. This depends on the browser locale, but it contradicts the org setting.

### Mobile
- There is no sideways page scroll, but wide tables scroll inside their panels. On `/devices` at 390px **the Status column is off-screen** (`devices-m.png`), and it is the only column a phone user needs.
- Device tabs wrap to three rows (`devices_dev-4-m.png`). KPI tiles stack one per row.
- The portal new-ticket form works well on a phone (`portal_new-m.png`), which matters because end users are the most likely phone users.

### "What should I do next?"
- Only **Add devices** (a stepper with a "You are here" marker) and the **SLA calendar warning** answer it.
- Most pages report state without a recommendation. An Offline device, a red donut, 3 "Needs verification" backups and 2 "Review overdue" BCP plans all have no primary action attached.

---

## 4. Per-module UI notes (short)

- **Dashboard** (`modules/dashboard/DashboardPage.tsx`): counts disagree with Devices; raw job types; no to-do list; the *Customize* button is good.
- **Devices** (`modules/devices/DevicesPage.tsx`): four header buttons plus the top-bar Install agent. No health, patch or backup columns, so the fleet list cannot answer "which machines are in trouble". "Architecture: amd64" and "Recorded by" are low-value columns. *Run job* appears on every row, including no-agent assets.
- **Device detail** (`modules/inventory/DeviceDetailPage.tsx`): 10 tabs; defaults to Hardware; no remote, patch-now or open-ticket action; owner and location not in the header.
- **Health** (`modules/health/*`): split alerts from configuration; sort by severity; open alerts in a drawer; remove "flapping/suppressed/tick/posture/fuses"; "Script destructive-exemptions" needs renaming.
- **Patch** (`modules/patch/*`): strong. Explain rings and show when an approved update will install; remove the MSRC essay from the list.
- **Deploy** (`modules/deploy/*`): group picker (the data exists); merge *Create* and *Execute*; hide *Execute* on finished runs; use the local timezone.
- **Remote** (`modules/remote/*`): good start dialog; needs an entry point from device, alert and ticket.
- **Credentials:** clear; the "Point-of-use boundary" panel is architecture.
- **Backup** (`modules/backup/*`): device-centric coverage view; confirm before an original-location restore; disable Restore on failed or pruned points; merge Verified and Tested.
- **Provisioning:** clear run list; errors are human ("PXE never answered: no DHCP offer…"); the name suggests people provisioning, which it is not.
- **Site servers / IPAM / Push install:** expert tools. The push authorisation warning is a 9-line amber block. Fine for an admin area, too prominent for the main rail.
- **Service desk** (`modules/service-desk/*`): no SLA column; ticket detail is seven forms; no quick actions; the copilot panel is not mounted.
- **Portal** (`modules/portal/*`): the best copy in the product, inside the wrong frame (operator shell, wrong landing page, "Dashboard" in the rail).
- **Notifications:** configuration presented as a service destination; "dead-lettered", "egress guard". The delivery log is useful.
- **Email intake:** excellent disposition log; IMAP and password only.
- **SLA:** good consequence-first warnings; raw day count.
- **Docs / Changes / BCP / Licenses / Subscriptions:** conventional and readable tables; the Licenses compliance bars are genuinely useful at a glance. The BCP period report opens empty until two datetime fields are filled; default to the last quarter.
- **Reports:** only four reports; permission codes on screen; *"this report is not reproducible"* is alarming without context.
- **Audit** (`modules/audit/AuditPage.tsx`): IDs instead of names for actors and targets (confirmed in the file header comment: "no name resolution exists"); `remote.session.start` shown as the action label.
- **Jobs:** good human names ("Apply patches") with the raw type as a sub-line, which is the right pattern. Rename "Signed job engine". The "Waiting" column shows age.
- **Assistant / AI automation:** see journey j.
- **Settings** (`modules/settings/*`): Organization uses free-text Locale (`en-GB`) and Timezone (`Europe/Lisbon`) fields; tabs not in the URL; Users is readable after the language pass, though "limited to grp-servers" leaks an ID.
- **Platform status / Error monitor:** fine for a server operator; these should not be in a tenant operator's main rail (raise sites like `app.modules.patch.handler:212`).
- **Budget:** chart plus a 36-row variance table of mostly zeros; collapse it to periods that have actuals.
- **Sign-in theme** (`deploy/keycloak/themes/itms/login/`): careful and accessible; needs Microsoft SSO and a softer end-user footnote.

### Issues that come from the fixture data, not the UI
- "Video memory 0 B" on every device.
- "Policy applied because: A person changed the priority or queue" on an alert-created ticket.
- Slugs such as `grp-workstations`, `usr-helgar`, `dev-1`, `pol-high`. **But** the code prints the raw ID field in each of these places, so production will show UUIDs instead, which is worse.
- The end user seeing fleet data on `/devices`: fixtures do not enforce permissions. The landing redirect itself is real.
- "26h over 900": the threshold display is built by the server, so the unit mix may be fixture-specific.
- Requester missing from the Users role checklist: may reflect the fixture role list.
- `mm/dd/yyyy` date inputs follow headless Chromium's en-US locale.

---

## 5. Ranked top-15 UI/UX improvements

| # | Improvement | Impact | Effort |
|---|---|---|---|
| 1 | **Make the device page the incident hub.** Header shows status, open alerts, cert/patch/backup state and owner. Actions: Remote in, Run script/fix, Install updates, Restart, Back up now, Open ticket. Default to an Overview tab. | Very high | M |
| 2 | **Replace every pasted-ID field with a picker** (device groups in Deploy, Scripts, Backup, BCP, Users; queues and categories in Health, Notifications, SLA, Email; devices in IPAM), and **resolve IDs to names** in Audit, Deploy target, Users scope and Health exemptions. Remove the "cannot be browsed yet" copy. | Very high | M |
| 3 | **Give the portal its own shell** (no operator rail, no fleet search, lands on My tickets / New ticket), mount `PortalAssistantPanel`, and route users with portal-only permissions there instead of `/devices`. | High | M |
| 4 | **Rebuild the IA:** about 8 task destinations in the rail and one Admin area for configuration (Notifications, Email, SLA, Site servers, Agent packages/Push/Enrollment, AI automation, Platform status, Error monitor). One "Add a computer" entry. One name for the AI feature. Fix double `aria-current`. | High | M |
| 5 | **Dashboard to-do list:** a "Needs you" list ranked across alerts, failed jobs, breaching tickets, unprotected devices and expiring certificates, each with its action. Fix the managed-endpoints count to match `/devices`. | High | M |
| 6 | **Make global search search records** (devices, tickets, alerts, docs, people), or change the placeholder until it does. | High | M |
| 7 | **Split `/health`:** the Alerts queue (sorted by severity, inline Ack/Mute, drawer detail with device link, Remote, Run fix and Open ticket) separate from Health configuration (profiles, actions, exemptions, settings; delete the duplicate in Settings or here). | High | M |
| 8 | **Apply `settings-language.md` to the whole console:** friendly job names everywhere (the `/jobs` map already exists), ban check keys, permission codes and enum values from primary text, and rewrite architecture subtitles as task subtitles. Add a lint similar to `noVendorNames.test.ts` for known wire tokens in JSX. | High | M |
| 9 | **Ticket detail quick actions:** Assign to me, Reply, Resolve, Reply & resolve; one Save for fields; SLA "due in" in the header and **an SLA column in the queue**; mount the copilot; link Remote in from the linked device. | High | M |
| 10 | **First-run setup checklist** after claim, plus a fallback when the invitation email cannot be sent. | High | S–M |
| 11 | **Employee lifecycle:** "New starter" and "Leaver" checklists tying Users, Devices, Licences, Subscriptions and Credentials to a person; a person page. | High | L |
| 12 | **Email intake for Microsoft 365** (OAuth/Graph), and **Microsoft sign-in** on the login theme. | High for Toeshee | M–L |
| 13 | **Tidy dangerous actions:** disable invalid row actions (Execute on finished runs, Restore on failed or pruned points, Run job on no-agent assets); confirm and label original-location restores; move Delete and Deactivate into an overflow menu so red means something. | Medium | S |
| 14 | **Fix the Dialog close button** (`components/ui.tsx:702`: add the `x` icon) and one timestamp convention (relative with absolute on hover; no monospace for dates). | Medium | S |
| 15 | **Mobile table priority:** on narrow screens show the name and status columns first (or render cards), cap KPI tiles at a 2×2 grid, and use a scrollable tab strip instead of wrapping. | Medium | S–M |

---

## Not verified
- **Real loading, error and 403 states:** fixture mode resolves instantly and grants every permission. The requester landing on a 403 `/devices` page is inferred from routing code and RBAC, not seen against a live backend.
- **The Setup page, after-claim behaviour and Keycloak pages as rendered:** reviewed from source only (fixture mode bypasses `SetupGate`, and there is no Keycloak here).
- **Whether the requester role is offered in production Users/Roles:** the fixture's list lacked it.
- **Whether Exchange Online would reject this tenant's IMAP login:** inferred from Microsoft's basic-authentication retirement, not tested.
- **Screen-reader and keyboard behaviour beyond tab order:** not tested with a screen reader, and no axe run in this pass.
