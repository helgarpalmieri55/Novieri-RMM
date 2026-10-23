# Console cross-module dependency map

Research date 2026-09-24, from the code at `/home/user/Toeshee-RMS` (read-only; nothing modified).
Scope: every page reachable from `frontend/src/shell/nav.ts` (Daily, Governance, Assets & Spend, every Admin page), plus the four unlisted routes in `NAV_UNLISTED` that operators land on.

Pattern to copy: `frontend/src/modules/agent-push/PushReadiness.tsx` (`Panel` + `Steps` from `shell/viz.tsx:387-400`, one item per prerequisite, states done / now (crit|warn) / todo, "Checking…" while loading, "cannot be checked here" on 403, `<Link className="btn">` CTA when missing).

Paths below: frontend paths are relative to `frontend/src/`, backend to `backend/app/`.

---

## 0. Read this first: five things that shape the implementation

1. **Built (2026-09-24): `/settings?tab=<key>` works; build links with `settingsHref(tab)` from `modules/settings/tabs.ts`.** Was: **Settings tabs cannot be deep-linked.** `modules/settings/SettingsPage.tsx:325` keeps the active tab in `useState<TabKey>("organization")`; nothing reads `?tab=`. Nine CTAs below need Settings → Users / Roles / Health / Email. Add `?tab=<key>` support (keys already exist: `organization, email, roles, users, assistant, notifications, sla, health, budget`, lines 313-323) before building those CTAs, or they land on Organization.
2. **Built (2026-09-24): `/health#profiles`, `#approved-actions`, `#health-settings` exist and scroll on arrival (`lib/useScrollToHash.ts`).** Was: **`/health` has no anchors.** Profiles, approved actions and health settings are panels on one page (`modules/health/HealthPage.tsx:222-232`) with no `id`. Add `id="profiles"`, `id="approved-actions"`, `id="health-settings"` plus the scroll effect `useStatLinks` already uses (`modules/notification/NotificationAdminPage.tsx:61-80`).
   Anchors that already work: `/notifications#channels|#rules|#allowlist|#deliveries`, `/email-intake#mailboxes|#messages|#microsoft`, `/agent-push/authorizations#authorize`, `/agent-packages#releases`. Query params that already work: `/credentials?new=<type>`, `/ipam?register=1`, `/devices?state=online|offline|certs`, `/devices?provenance=sweep`, `/enrollment?tab=tokens`.
3. **Three shared detectors cover about 40% of the rows.** Built (2026-09-24) in `lib/detectors.ts` as `useFleetPrerequisite` / `useGroupsPrerequisite` / `useInstallerPrerequisite` / `useRulesPrerequisite` (plus `use…Query` hooks), rendered by `<Prerequisites>` in `components/prerequisites.tsx`:
   - **FLEET** `api.listDevices({ limit: 1 })` → `.fleet` (`lib/api.ts:498-503`, `devices.read`). `fleet.total === 0` means no asset at all. `fleet.managed === 0` means no machine has an agent. `fleet.online === 0` means no agent is connected. CTA `/devices/onboard` (`ADD_COMPUTER_TO`, `shell/nav.ts`), label "Add a computer".
   - **GROUPS** `api.listDeviceGroups()` → `.total === 0` (`lib/api.ts:1237`, `devices.read`). CTA `/device-groups`, label "Create a device group".
   - **INSTALLER** `api.listAgentReleases()` → `!some(r => r.has_artifact && r.published_at)` (`lib/api.ts:2709`, `agent.release.read`). CTA `/agent-packages#releases`, label "Publish an agent package".
   - **RULES** `api.listNotificationRules()` → `!some(r => r.active && r.event_type.startsWith(<prefix>))` (`notification.read`). CTA `/notifications#rules`, label "Send these to someone".
4. **"Cannot check" needs the permission list.** `api.me().permissions` (`lib/api.ts:660`, no permission needed). When the code in column 4 is missing, render "cannot be checked from this account", as `PushReadiness` does on `isError`. Don't render "missing". The journey's `gate()` helper (`modules/onboarding/journey.ts:78-80`) is the existing shape.
5. **Several guesses in the brief are not true in this code.** They are listed here so nobody builds CTAs for them:
   - *IPAM sweeps need a site server.* **False.** A site server is only *preferred*. Any enrolled, online agent observed inside the subnet can sweep (`modules/ipam/sweep.py:241-263, 286-340`). What a sweep does need is a scan authorization and an agent on the segment.
   - *Imaging needs credentials (domain join).* **False.** The domain-join and local-admin passwords are sealed on the provisioning profile itself (`modules/provisioning/repository.py:37-42`), not taken from `/credentials`. The site-server wake exists in the API (`provisioning/api.py:313-316`) but the console never offers `wake_on_lan`.
   - *Remote sessions need unattended credentials.* **False now.** Screen sessions refuse a credential outright (`modules/remote/service.py:403-404`).
   - *Notifications need Settings → Email.* **False.** Settings → Email is the identity realm's SMTP relay, used for invitations and password resets (`api/v1/realm_settings.py:1-13`). Notification email channels carry their own SMTP or Microsoft 365 sign-in (`modules/notification/api.py:217-248`). Do not point a notification CTA at Settings → Email.
   - *Tickets / Service levels need queues and categories.* Only barely. Both are lazily seeded (`modules/service_desk/refdata.py:52-69, 132-139`), so an empty list is almost impossible. The real gap is SLA *policies* (below).
   - *Patching needs maintenance windows / policies.* **No.** Without a policy the hard-coded safe defaults apply (`modules/patch/policy.py:1-4, 24-32`).

---

## 1. Existing guidance (reuse, don't duplicate)

| Where | What it already does | Reuse note |
|---|---|---|
| `modules/onboarding/journey.ts` + `OnboardPage.tsx` (`/devices/onboard`) | Five-step journey: register subnet → authorize scan → sweep (incl. `no_agent` → `/agent-packages`) → review Discovered → agent package. Pure function, `unknown`/`blocked`/`partial`/`done`, `missingPermission`. | Model for the pure-function + test approach. Its register/authorize/sweep steps are the IPAM rows below. Link to it instead of copying it. |
| `modules/agent-push/PushReadiness.tsx` | Credential saved / bound to a range / range authorized. | Missing two org-level prerequisites the server refuses on: site server, published release (see Push install). |
| `modules/settings/UsersTab.tsx:289-304` | Gold banner "Email isn't set up, so invitations can't be sent — Settings → Email" when `getEmailSettings().configured === false`, gated on `orgs.manage`. | **Done**: a "Set up email" link to `settingsHref("email")`. |
| `modules/settings/SettingsPage.tsx:280-296` | "Set up / Not set up" on the Email, Service levels and Health tabs from each section's `configured`. | Same data a hub/checklist could use. |
| `modules/health/HealthSettingsPanel.tsx:187-205`, `settings/HealthTab.tsx:241` | "Alerts can't raise tickets yet — pick a queue and a category under Settings → Health" when `alert_tickets_enabled === false`. | **Done**: links to `settingsHref("health")` (panel) and `#alert-tickets` (tab). |
| `modules/patch/PatchTabPanel.tsx:515-575` | `tabEmptyReason`: `unenrolled` → "Add devices" → `/devices/onboard`; `unscanned`; `uncounted` (no `devices.read`). | Best existing empty-state pattern; copy for Backup, Health, Remote. |
| `modules/devices/DevicesPage.tsx:348` | "Add a computer" button in the page head. | Already covers Devices. |
| `modules/budget/BudgetDashboard.tsx:212-228` | "No budget yet" EmptyState with "Create a budget in the Planner". | Already covers Budget → planner. |
| `modules/ai-admin/ChatPanel.tsx:188-240` | Assistant off → the server's reason plus "Open assistant settings", or "whoever runs this server" text. | Already covers Assistant's hard dependency. |
| `modules/bcp/PlanEditor.tsx:449-455` | No docs articles → "Write the recovery procedure in IT documentation →". | Already covers BCP → Docs. |
| `modules/deploy/DeployPage.tsx:594`, `backup/BackupPolicyEditor.tsx:380`, `licenses/LicensesPage.tsx:504,520`, `licenses/ReviewQueue.tsx:216`, `subscriptions/SubscriptionsPage.tsx:674`, `budget/ExpensesPage.tsx:596,612`, `provisioning/ProfileEditor.tsx:170` | Inline "Add a …" link beside an EntityPicker whose list is empty. | Good. The **text-only** `emptyHint`s listed in §4 should get the same treatment. |
| `modules/devices/ScanSubnetDialog.tsx:112-160`, `devices/CandidatesPage.tsx:243` | Links to `/ipam` when no subnet or no designated agent. | Already covers Devices → IPAM. |
| `modules/sla/SlaAdminPage.tsx:128-170` | Tiles: no calendar, no policy, tickets with no SLA. | Covers SLA's own page. Tickets does not link here yet. |
| `modules/devices/GroupsPage.tsx:1330-1355` | "Still in use" dialog lists the referencing modules as links. | Reverse dependency, already done. |
| `modules/agent-releases/AgentUpdatesSection.tsx:89-107` | Lists devices that "need a manual update". | Already there. |

---

## 2. Per-page dependency tables

Severity: **B** = blocking (the page cannot do its job, or the server refuses). **A** = advisory (it works, but is emptier or silently weaker).
Status: **new** = no guidance today. **text** = a sentence exists but no link. **done** = already a CTA.

### Daily

#### `/dashboard`: Dashboard (permission `null`; each widget has its own)
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | At least one computer runs the agent | FLEET `managed === 0` (the page already fetches `/devices`) | devices.read | `/devices/onboard` · "Add a computer" | `modules/dashboard/DashboardPage.tsx:522` zeroLabel "No asset has an agent"; `:695` "No devices enrolled yet." | A | **done** (2026-09-24, `dashboard/DashboardReadiness.tsx`) |
| 2 | A patch scan has run | `getPatchFleetReport().devices_scanned === 0` (`modules/patch/types.ts:296`) | patch.read | `/patch` · "Scan for updates" | `DashboardPage.tsx:856` "No patch scan has reported yet." | A | **done** (2026-09-24: the Patch compliance panel's link reads "Scan for updates" while `devices_scanned === 0`) |
| 3 | Backups are scheduled | `listBackupPolicies().filter(p => p.enabled).length === 0` | backup.read | `/backup/policies` · "Set up backups" | `DashboardPage.tsx:931` "No backup report available." | A | **done** (2026-09-24, `DashboardReadiness`). Rows whose widget the account cannot see are left out rather than shown as "cannot be checked": the dashboard is where an account without `devices.read` lands |

#### `/devices`: Devices
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Some computer is enrolled | FLEET `total === 0` | devices.read | `/devices/onboard` · "Add a computer" | `modules/devices/DevicesPage.tsx:535` "No devices enrolled yet." | A | done (head button, `:348`). Optional: repeat it in the empty state |

#### `/health`: Alerts
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Computers run the agent (alerts come from agent checks) | FLEET `managed === 0` | devices.read | `/devices/onboard` · "Add a computer" | Alert list shows only the filter empty state `AlertConsole.tsx:266-269` | B | **done** (2026-09-24, `health/AlertReadiness.tsx`) |
| 2 | A health profile is assigned (to the org, a group or a device) | `listHealthAssignments().length === 0` | health.read | `/health#profiles` · "Assign a health profile" (needs anchor, §0.2) | `modules/health/baseline.py:3-4` "A fresh org monitors nothing until someone writes a profile"; `health/api.py:863-866` no profile → explicit DISARM. The seeded baseline can be deleted and is never re-created (`baseline.py:9-12`). | B | **done** (2026-09-24, `AlertReadiness`) |
| 3 | Alerts can open tickets (default queue + category) | `getHealthSettings().alert_tickets_enabled === false` | health.read | `/settings?tab=health` · "Choose where alert tickets go" (needs §0.1) | `modules/health/declared_actions.py:85-86`; `HealthSettingsPanel.tsx:187-205` | A | **done** (2026-09-24: the notice in `HealthSettingsPanel` now links to `/settings?tab=health`; no second message) |
| 4 | Someone is notified of alerts | RULES prefix `"health.alert"` (`core/events.py`: `health.alert_raised/_escalated/_cleared`) | notification.read | `/notifications#rules` · "Notify someone about alerts" | `modules/notification/RulesPanel.tsx:239` "No rules yet, so nothing is delivered." | A | **done** (2026-09-24, `AlertReadiness`) |
| 5 | Remediation actions exist | `listApprovedActions().length === 0` | health.read | **No console page creates one.** `useCreateApprovedActionMutation` (`modules/health/api.ts:411`) is unused; the backend `POST /approved-actions` (health.action.admin) exists. Needs a create dialog first. | `modules/health/ApprovedActionList.tsx:133` "No approved actions. No fixes can be dispatched by this organisation." | A | text; needs UI |

#### `/service-desk`: Tickets
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Response and resolution targets exist | `listSlaPolicies().filter(p => p.active).length === 0`, or `getSlaSettings().tickets_without_sla > 0` (`modules/sla/types.ts:171-174`). Without `sla.read` (admin-only, `sla/api.py:651-656`), use per-ticket `getTicketSla(id).has_sla === false` (ticket.read). | sla.read (fallback ticket.read) | `/sla` · "Set response targets" | `modules/sla/policies.py:628`; `service-desk/TicketDetailPage.tsx:319-323` "No SLA"; `sla/TicketSlaPanel.tsx:111` | A | **done** (2026-09-24: `service-desk/TicketReadiness.tsx` on the list; `sla/TicketSlaPanel.tsx` links "Set response targets" for `sla.read`, and says who sets them otherwise) |
| 2 | A support mailbox turns mail into tickets | `listEmailMailboxes().length === 0` | email_intake.read | `/email-intake#mailboxes` · "Connect a support mailbox" | email is a ticket `origin` (`service_desk/models.py:116`) | A | **done** (2026-09-24, `TicketReadiness`) |
| 3 | People are told about ticket activity | RULES prefix `"ticket."` | notification.read | `/notifications#rules` · "Notify people about tickets" | `RulesPanel.tsx:239` | A | **done** (2026-09-24, `TicketReadiness`) |
| 4 | Alerts can become tickets | `getHealthSettings().alert_tickets_enabled === false` | health.read | `/settings?tab=health` · "Let alerts open tickets" | `TicketListPage.tsx:269` zeroLabel "No ticket here was raised by an alert"; `health/declared_actions.py:85` | A | **done** (2026-09-24, `TicketReadiness`) |
| 5 | Requesters exist (portal / email senders) | `listRoles().roles.find(r => r.name === "requester")?.assigned_users === 0` | roles.read | `/settings?tab=users` · "Invite requesters" | `migrations/versions/0028_portal.py:61` (global `requester` role) | A | new; **skipped** 2026-09-24: the requester role's assignment count says nothing about mail senders, so a zero does not mean nobody can raise a ticket |
| 6 | Each category has a default queue | `listTicketCategories().some(c => c.default_queue_id == null)` | ticket.read | `/service-desk/admin` · "Set a default queue" | `modules/service_desk/tickets.py:362-372` refuses creation "category … has no default_queue_id and no queue_id was supplied" | A (B for that category) | **done** (2026-09-24, `TicketReadiness`, names the categories) |

#### `/patch`: Patching
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Computers run the agent | `PatchTabPanel` `managedCount` (FLEET) | devices.read | `/devices/onboard` · "Add devices" | `modules/patch/PatchTabPanel.tsx:530-538` | B | **done** |
| 2 | Device groups exist for per-group rings and windows | GROUPS | devices.read | `/device-groups` · "Create a device group" | `modules/patch/PatchPolicyEditor.tsx:142-144, 334`: "Per-group rings need a device group, which this console cannot list yet". **Stale:** `GET /device-groups` exists (`api/v1/device_groups.py:246`). Add a group picker, then this CTA. | A | **done** (2026-09-24: `PatchPolicyEditor` has an "Applies to" group picker, the replace-confirmation names the group, and "Create a device group" links out when none exists) |
| 3 | An approved change exists where a change gate covers patches | `listChangeGates().some(g => g.applies_to_patches)`, then show which groups (`device_group_name ?? "Every device"`) | change.read | `/changes` · "Raise a change for these updates" | `modules/patch/api.py:1071` 409 refusal; `change_mgmt/gates.py:1-15`; `patch/apply.py:92-105` | B (for gated groups) | **done** (2026-09-24, `patch/PatchReadiness.tsx` via `change/gatePrerequisite.ts`; done when no gate covers patches or an approved/implementing change exists) |

#### `/deploy`: Software
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | A package is in the catalogue | `listPackages().length === 0` | deploy.read | `/deploy/packages` · "Add a package" | `modules/deploy/DeployPage.tsx:587-594` | B | **done** |
| 2 | Computers run the agent | FLEET `managed === 0` | devices.read | `/devices/onboard` · "Add a computer" | `modules/deploy/pickerOptions.ts:46-47` "Endpoints appear here once enrolled (Devices → Enrollment)". Stale path, no link. | B | **done** (2026-09-24, `deploy/DeployReadiness.tsx`; picker hint path fixed and "Add a computer" linked in the dialog) |
| 3 | Device groups exist (to target a group) | GROUPS | devices.read | `/device-groups` · "Create a device group" | `pickerOptions.ts:49-50` "Create one under Devices → Groups". Stale path, no link. | A | **done** (2026-09-24: picker hint path fixed, "Create a device group" linked in the dialog when the list is empty) |
| 4 | An approved change exists where a change gate covers deployments | `listChangeGates().some(g => g.applies_to_deployments)` | change.read | `/changes` · "Raise a change for this deployment" | `change_mgmt/gates.py:151-153` "Deployments to … need an approved change"; `deploy/api.py:601, 647` 409 | B (for gated targets) | **done** (2026-09-24, `DeployReadiness`) |
| 5 | The package's installer file is uploaded | `listPackages().some(p => p.artifact_ref && !p.artifact_sha256)` (**uncertain** that this is exactly the refused state) | deploy.read | `/deploy/packages` · "Upload the installer" | `modules/deploy/api.py:636-645` 422 "artifact-backed package with no uploaded artifact" | B (at execute) | **done** (2026-09-24, `DeployReadiness`; the rule is `deploy/artifacts.py`'s exactly: an artifact-backed `installer_type` with no `artifact_ref` or no `artifact_sha256`, so no longer uncertain) |

#### `/backup`: Backup (landing is Backup points; sub-pages `/backup/repositories|policies|coverage|offsite`)
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | A backup repository exists | `listBackupRepositories().length === 0` | backup.read | `/backup/repositories` · "Create a repository" | `modules/backup/api.py:95-98` (policy requires `repository_id`); link exists only inside the policy editor (`BackupPolicyEditor.tsx:380`) | B | **done** (2026-09-24, `backup/BackupReadiness.tsx` on `/backup`, `/backup/coverage` and `/backup/policies`) |
| 2 | An enabled backup policy covers devices | `listBackupPolicies().filter(p => p.enabled).length === 0` | backup.read | `/backup/policies` · "Create a backup policy" | `modules/backup/dispatch.py:91-100`: "Back up now" returns `[]` **silently** when no enabled policy applies | B | **done** (2026-09-24, `BackupReadiness`; a disabled policy links to `/backup/policies?enabled=false`) |
| 3 | Computers run the agent | FLEET `managed === 0` | devices.read | `/devices/onboard` · "Add a computer" | `modules/backup/CoveragePage.tsx:127` "No devices yet." | B | **done** (2026-09-24, `BackupReadiness`) |
| 4 | Device groups exist (per-group policies) | GROUPS | devices.read | `/device-groups` · "Create a device group" | `backup/api.py:96` `device_group_id` | A | **done** (2026-09-24, `PolicyReadiness` on `/backup/policies`) |
| 5 | A recovery key is set up and proven (before off-site copies) | `!listRecoveryKeys().some(k => k.proven_at && !k.retired_at)` | backup.read | `/backup/offsite` · "Set up a recovery key" | `modules/backup/offsite.py:80-83, 365-366` refusal | A (B for off-site) | **done** (2026-09-24, `OffsiteReadiness` on `/backup/repositories`; `/backup/offsite` itself already offers "Set up the recovery key") |
| 6 | An off-site target has passed its test | `!listOffsiteTargets().some(t => t.state === "ok")` | backup.read | `/backup/offsite` · "Test off-site storage" | `offsite.py:84, 367-368` | A | **done** (2026-09-24, `OffsiteReadiness`) |

#### `/remote`: Remote sessions
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | At least one agent is online | FLEET `online === 0` (`managed === 0` → onboard) | devices.read | `/devices?state=offline` · "See offline devices" / `/devices/onboard` · "Add a computer" | `modules/remote/SessionsPage.tsx:188-193` tile only; `remote/service.py` needs a live agent | B | **done** (2026-09-24, `remote/RemoteReadiness.tsx`) |
| 2 | The device's agent can share its screen | No existing field. Per-device capability is not on `Device`. Proxy: `getFleetVersions().update_available > 0` (agent.update.read). **Needs a new field**, e.g. `Device.screen_capable` or `capabilities` on `GET /devices`. | agent.update.read | `/agent-packages` · "Update the agent" | `modules/remote/service.py:406-410` 409 "agent too old to share its screen" | B (per device) | **done** (2026-09-24: `Device.screen_capable` on `GET /devices` (and `GET /devices/{id}`), derived by `remote/capability.py::can_share_screen`, the function the 409 is decided by; the Start session dialog's picker and hint say it and link "Update the agent" → `/agent-packages#fleet-versions`) |
| 3 | A remote policy allows unattended access (where wanted) | `GET /remote/policies` exists (`remote/api.py:369`, remote.control) but **no ApiClient method and no console editor** for `PUT /remote/policies` (remote.policy.manage). | remote.control | (no page yet) | `remote/service.py:441-447` unattended **silently downgrades** to attended; `remote/policy.py:24-26` default `allow_unattended=False` | A | new; needs frontend method + editor |

#### `/assistant`: Assistant
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | A model provider is configured on the server | `getAiStatus().enabled === false` (`.reason` is the server's sentence) | ai.use | `/assistant/settings` · "Open assistant settings" (server `.env`; not console-fixable) | `modules/ai_admin/api.py:435-448`; `ai-admin/ChatPanel.tsx:205-240` | B | **done** |
| 2 | Act mode has actions it may propose | `listApprovedActions().length === 0` | health.read | `/health#approved-actions` (no create UI, see Alerts #5) | `core/ai/act.py:69, 339, 454` (catalogue is `ApprovedAction`) | A | new; **skipped** 2026-09-24: no console screen creates an approved action yet (Alerts #5), so a CTA would send the operator nowhere |

### Governance

#### `/docs`: IT documentation
No cross-module prerequisite found. The page works on its own. It is a *dependency* of Continuity tests (procedures) and the End-user portal (help articles).

#### `/changes`: Changes
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Someone other than the requester can approve | `sum(listRoles().roles.filter(r => r.codes.includes("change.approve")).map(r => r.assigned_users)) < 2`. **Approximate**: this counts assignments, not distinct people. An exact answer needs a new endpoint (e.g. count of distinct users holding a code). | roles.read | `/settings?tab=roles` · "Give someone change approval" | `modules/change_mgmt/state.py:71-73` "approver cannot be the change requester"; approve route needs `change.approve` | B (for approval) | **done** (`change/ChangeReadiness.tsx`; the detail says the count is of assignments, not people) |
| 2 | Device groups exist (to gate part of the fleet) | GROUPS | devices.read | `/device-groups` · "Create a device group" | `modules/change/GatesPanel.tsx:31, 96-100` (falls back to "Every device") | A | **done** (`change/ChangeReadiness.tsx`) |

#### `/bcp`: Continuity tests
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Recovery procedures are documented | `listArticles().items.length === 0` | docs.read | `/docs` · "Write the recovery procedure" | `modules/bcp/PlanEditor.tsx:439-455` | A | **done** |
| 2 | Devices / groups to scope a plan | FLEET / GROUPS | devices.read | `/devices/onboard`, `/device-groups` | `modules/bcp/pickerOptions.ts:34-38` (stale paths "Devices → Enrollment/Groups", no link) | A | **done** (links beside the empty scope pickers in `bcp/PlanEditor.tsx`; stale menu paths removed from the hints) |
| 3 | Backups exist, so a restore drill has evidence | `listBackupPolicies().filter(p => p.enabled).length === 0` or `getBackupReport()` shows nothing covered | backup.read | `/backup/coverage` · "Protect devices with backup" | `modules/bcp/TestRecorder.tsx:52, 79, 300-345` (frozen backup-evidence snapshot) | A | **done** (`bcp/BcpReadiness.tsx`, enabled-policy check; CTA `/backup/policies` "Set up backups", the same target as Reports #3) |
| 4 | Someone can review/approve plans | `listRoles()` has no role with `bcp.approve` and `assigned_users > 0` | roles.read | `/settings?tab=roles` · "Give someone plan review" | `modules/bcp/api.py` `POST /bcp/plans/{id}/review` requires `bcp.approve` | A | **done** (`bcp/BcpReadiness.tsx`). Self-review is allowed (`bcp/service.py` `review_plan`), so one holder is enough |

#### `/reports`: Reports (one row per report in the catalogue; `listReports()` already reports *permission* gaps via `missing_permissions`, `ReportCatalogue.tsx:54-60`)
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | fleet-posture: computers run the agent | FLEET `managed === 0` | devices.read | `/devices/onboard` · "Add a computer" | `modules/reporting/definitions/fleet_posture.py:230-231` | A | **done** (`reporting/ReportReadiness.tsx`) |
| 2 | patch-compliance: a scan ran and updates were approved | `getPatchFleetReport().devices_scanned === 0`; `listPatchApprovals()` empty | patch.read | `/patch` · "Scan and approve updates" | `definitions/patch_compliance.py:480-481` ("outstanding **approved** patches") | A | **done** (`reporting/ReportReadiness.tsx`; approvals asked with `revoked=false`) |
| 3 | backup-coverage: backup policies exist | enabled `listBackupPolicies()` empty | backup.read | `/backup/policies` · "Set up backups" | `definitions/backup_coverage.py:447-448` | A | **done** (`reporting/ReportReadiness.tsx`) |
| 4 | sla-attainment: SLA policies exist | active `listSlaPolicies()` empty | sla.read | `/sla` · "Set response targets" | `definitions/sla_attainment.py:343-344`. The report's empty state cannot tell "not set up" from "no rows" (`ReportView.tsx:599-604`) | A | **done** (`reporting/ReportReadiness.tsx`) |

### Assets & Spend

#### `/licenses`: Licenses
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Products and vendors exist | `listProducts()` / `listVendors()` empty | licenses.read | `/licenses/products`, `/licenses/vendors` | `LicensesPage.tsx:497-520` | B | **done** |
| 2 | Installed-software inventory exists (compliance counts installs) | FLEET `managed === 0` | devices.read | `/devices/onboard` · "Add a computer" | `modules/licenses/matching.py:1-8`; `ComplianceDashboard.tsx:33` "No compliance data yet." | B (for compliance) | **done** (`licenses/LicenseReadiness.tsx`, on `/licenses` and `/licenses/compliance`) |
| 3 | Installed software is mapped to products | `reviewQueue().length > 0` | licenses.read | `/licenses/review-queue` · "Map installed software" | `ReviewQueue.tsx:69` | A | **done** (`licenses/LicenseReadiness.tsx`; waits on #2) |
| 4 | Seats can be allocated to people/devices | **No console UI.** `useCreateAllocationMutation` (`modules/licenses/api.ts:176`) is unused; `POST /licenses/{id}/allocations` exists. | licenses.manage | (needs an "Allocate" dialog on `LicenseDetail`) | `LicenseDetail.tsx:213` "No allocations yet." | A | needs UI |

#### `/subscriptions`: Subscriptions
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Vendors exist | `listVendors()` empty | licenses.read | `/licenses/vendors` | `SubscriptionsPage.tsx:666-674` | A | **done** |
| 2 | Owners (console users) exist | `listUsers().users.length` | users.read | `/settings?tab=users` · "Add a user" | `modules/subscriptions/pickerOptions.ts:37-38` says "Settings → **Organization**". **Wrong tab**: users are on Settings → Users. | A | **done** (hint says Settings → Users; "Add a user" link beside the owner picker) |
| 3 | Seat holders are matched to people | `listUnmatched().seats.length > 0` | people.read | `/people/unmatched` · "Match seat holders" | `modules/people/types.ts:142-149` | A | **done** (`subscriptions/SubscriptionReadiness.tsx`) |
| 4 | Renewal and notice reminders reach someone | RULES prefix `"subscription."` (`renewal_approaching`, `notice_deadline_approaching`) | notification.read | `/notifications#rules` · "Get renewal reminders" | `core/events.py` event types | A | **done** (`subscriptions/SubscriptionReadiness.tsx`) |

#### `/budget`: IT budget
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | A fiscal-year budget exists | `listBudgets()` empty | budget.read | `/budget/planner` | `BudgetDashboard.tsx:212-228` | B | **done** |
| 2 | Categories / cost centres exist | `listBudgetCategories()` / `listCostCenters()` | budget.read | `/budget/admin` | `ExpensesPage.tsx:589-612` | A | **done** |
| 3 | FX rates exist for foreign-currency spend | the unconvertible count/byCurrency the dashboard already renders | budget.read | `/budget/admin` · "Add an exchange rate" | `BudgetDashboard.tsx:62-92` notice lists currencies, no link | A | **done** (link in the notice to `/budget/admin#fx-rates`; the anchor is new) |
| 4 | Subscriptions/licenses exist to feed expected spend | `listSubscriptions().length + listLicenses().length === 0` | subscriptions.read, licenses.read | `/subscriptions` · "Record subscriptions" | `modules/budget/feeds.py:1-10` (projection reads those two registries) | A | **done** (`budget/BudgetReadiness.tsx`) |

#### `/ipam`: IP addresses
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | The subnet is authorized for scanning | `effectiveSweepScope(subnet, listIpamScanAuthorizations(), now).authorized` (`ipam/ipamState`) | ipam.read | `/ipam/scan-authorizations` · "Authorize scanning" | `modules/ipam/SweepPanel.tsx:86-108` shows "Not authorized" with **no link**; `onboarding/journey.ts:141-190` | B | **done** ("Authorize scanning" in the sweep panel's notice; estate-wide row in `ipam/IpamReadiness.tsx` on `/ipam`) |
| 2 | An enrolled agent sits on the segment | `listAssetCandidates().unsweepable_subnets > 0` (`lib/api.ts:536-547`), or per subnet the latest `listIpamSweepRuns(id,1)[0].state === "no_agent"` | devices.read / ipam.read | INSTALLER → `/agent-packages` · "Install the agent on one machine there" | `modules/ipam/sweep.py:244-256`; `journey.ts:205-222` | B | **done** (sweep panel on a `no_agent` run; `unsweepable_subnets` row in `ipam/IpamReadiness.tsx`) |
| 3 | A site server serves the segment (preferred sweeper) | `listSiteServers({ segment: subnet.prefix, enabled: true }).length === 0` | site.read | `/site-servers` · "Promote a site server" | `sweep.py:258-263` (preference, **not** required) | A | **done** (`ipam/IpamReadiness.tsx`, per authorized subnet from one `listSiteServers({enabled})`, same containment rule as `sweep.py`) |
| 4 | Drift reaches someone | RULES prefix `"ipam."` | notification.read | `/notifications#rules` | `core/events.py` `ipam.drift_detected`, `ipam.sweep_rejected` | A | **done** (`ipam/IpamReadiness.tsx`) |

### Admin: Service desk

#### `/notifications`: Notifications
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Users exist to receive mail (queue members, assignees) | `listUsers().users.length <= 1` | users.read | `/settings?tab=users` · "Add users" | `modules/notification/QueueMembersPanel.tsx:197` text "Users are added under Settings → Users" | A | **done** (`NotificationPrerequisites`, "Before notifications can reach people"; counts active users) |
| 2 | Ticket queues exist (queue-member rules) | `listTicketQueues().length === 0` (lazily seeded, rarely true) | ticket.read | `/service-desk/admin` · "Set up queues" | `QueueMembersPanel.tsx:18-24` | A | **done** (`NotificationPrerequisites`) |
| (n/a) | Settings → Email is **not** a prerequisite | Channels carry their own SMTP/M365 sign-in | | | `notification/api.py:217-248` | | do not add |

#### `/email-intake`: Email intake
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | A ticket category exists for the mailbox default | `listTicketCategories().length === 0` (seeded, rarely true) | ticket.read | `/service-desk/admin` · "Create a category" | `modules/email_intake/schema.py:48` (`default_category_id` required); `MailboxForm.tsx:344` text-only hint | B | **done** (`EmailIntakePrerequisites`, "Before mail can become tickets"; the in-dialog hint stays, since a link there would drop the half-filled form) |
| 2 | Senders are known users | `listEmailIntakeMessages({ disposition: "quarantined" }).messages.filter(m => m.reason.startsWith("unknown sender"))` | email_intake.read | `/settings?tab=users` · "Add these senders as users" | `modules/email_intake/identity.py:105-106` "unknown sender" → quarantine | A (B per sender) | **done** (`EmailIntakePrerequisites`; reads the latest 50 quarantined messages) |
| 3 | The server's public console URL is set (Microsoft sign-in) | `getEmailOAuthApp().redirect_uri === null` | email_intake.read | none (server `ITS_PUBLIC_CONSOLE_URL`); say so | `email_intake/microsoft.py:126-130`; `email-intake/types.ts:116-120` | B (M365 only) | **done** (already said, with the server key to set, in `MicrosoftConnection.tsx`; not repeated) |

#### `/sla`: Service levels
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Ticket queues exist (to match on and escalate to) | `listTicketQueues().length === 0` | ticket.read | `/service-desk/admin` · "Set up queues" | `modules/sla/PoliciesPanel.tsx:546, 638` text only | A | **done** (`SlaPrerequisites`, "Before a target can match a queue"; the in-dialog picker hints stay) |

#### `/portal`: End-user portal
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Requester accounts exist | `listRoles().roles.find(r => r.name === "requester")?.assigned_users === 0` | roles.read | `/settings?tab=users` · "Invite requesters" | `migrations/versions/0028_portal.py:61` | B | **done** (`PortalPrerequisites`, "Before requesters can use the portal"; never shown to a portal-only account) |
| 2 | Help articles are published to the portal | `listArticles().items.some(a => a.portal_visible)`. Only the first 100 are checked (`documentation/api.py:180`). **Exact needs a new `portal_visible` filter** on `GET /docs/articles`. | docs.read | `/docs` · "Publish help articles" | `modules/portal/api.py:237-240`; `lib/api.ts:5014` | A | **done** (first page only; past it the row says it could not tell rather than "missing") |
| 3 | Portal assistant available | `getAiStatus().enabled` | ai.use | none (server config) | `modules/portal/PortalAssistantPanel.tsx` | A | **done** (no link; shows the server's reason) |
| 4 | Requesters hear about replies | `listNotificationRules().some(r => r.active && r.recipient_kind === "requester")` | notification.read | `/notifications#rules` · "Tell requesters about replies" | `modules/notification/routing.py:16-21` | A | **done** |

### Admin: Computers

#### `/enrollment`: Enrollment
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | An installer is published (a token alone installs nothing) | INSTALLER | agent.release.read | `/agent-packages#releases` · "Publish an agent package" | `modules/enrollment/EnrollmentPage.tsx:13-19` (explains the split); the file has **no** `<Link>` to Agent packages | B (for enrolling) | **done** (`EnrollmentPrerequisites`, "Before a token can enroll a computer"; shown to token issuers) |

#### `/agent-packages`: Agent packages (incl. Agent updates)
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | A device group for the pilot ring | GROUPS | devices.read | `/device-groups` · "Create a pilot group (≤ 25 devices)" | `modules/agent_update/service.py:60, 208-213`; `AgentUpdatesSection.tsx:356, 379-389` (Start disabled until a group is picked) | B (rollouts) | **done** (`AgentUpdatePrerequisites`, "Before an agent update can roll out"; a live group of 1 to 25 computers) |
| 2 | A stable release exists | `getFleetVersions().latest_stable === null` | agent.update.read | same page · "Promote a release to stable" | `lib/api.ts:864-868`; `agent_update/service.py:341` | A | **done** (CTA `/agent-packages#channels` for a publisher; everyone else is told who marks one) |
| 3 | Computers run the agent | FLEET `managed === 0` | devices.read | `/devices/onboard` | | A | **done** |

#### `/agent-push/authorizations` + `/agent-push/credential-scopes`: Push install
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1-3 | Credential saved; bound to a range; range authorized | `PushReadiness` | credentials.read, agent.push.scope.manage | existing | `PushReadiness.tsx` | B | **done** |
| 4 | **An enabled site server serves each authorized range** | `listSiteServers({ enabled: true })`, then client-side containment of each `listPushAuthorizations()` prefix in some `segments[]` (or `listSiteServers({ segment: prefix })` per range) | site.read | `/site-servers` · "Promote a site server" | `modules/agent_push/preflight.py:136-143` `no_site_server`; CLAUDE.md invariant 2a(a) | B | **done** (union of enabled segments per active range, `pushState.siteServerCoverage`; liveness is not checked, and the step says so) |
| 5 | **A published agent release exists for the target OS/arch** | INSTALLER, filtered to what `_device_os_arch` accepts: `linux`/`darwin` × `amd64`/`arm64` (push is SSH-first and refuses a Windows target, `dispatch.py:511-519`; the original "windows" here was wrong) | agent.release.read | `/agent-packages#releases` · "Publish an agent package" | `modules/agent_push/dispatch.py:160-191` "no published agent release exists for {os}/{arch}" | B | **done** |
| (6) | Target is an asset a person vouched for | per machine, belongs to the push itself (as PushReadiness says) | | | `preflight.py:118-124` | | leave |

#### `/device-groups`: Device groups
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Devices exist to put in groups | FLEET `total === 0` | devices.read | `/devices/onboard` · "Add a computer" | `modules/devices/GroupsPage.tsx:931-935` "No groups yet" | A | **done** ("Before a group has computers to hold") |

#### `/site-servers`: Site servers
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | A device with an agent on that segment | FLEET `managed === 0` | devices.read | `/devices/onboard` · "Add a computer" | `modules/site-servers/PromoteDialog.tsx:95` (no-agent devices disabled) | B | **done** (`SiteServerPrerequisites`, "Before a site server can be promoted") |
| 2 | The segment is registered in IPAM (segment check has evidence) | `getSiteServerSegmentCheck(id).evidence === "unavailable"` or `listIpamSubnets()` empty | site.read / ipam.read | `/ipam?register=1` · "Register the segment" | `modules/site-servers/types.ts:97-102` | A | **done** (`listIpamSubnets()` empty; worded as "nothing to sweep", since sweeps run per registered subnet) |
| note | Empty-state copy "A segment with no site server gets no sweeps" (`SiteServerList.tsx:73`) contradicts `ipam/sweep.py:258-263` (ordinary endpoints sweep). It is true for *wakes* and *push installs*. | | | | | | **done** (now "no wakes and no push installs; its sweeps are run by any computer with the agent there") |

#### `/provisioning`: Imaging
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | An OS image / a profile exist | `listOsImages()` / `listProvisionProfiles()` | provisioning.read | `/provisioning/images`, `/provisioning/profiles` | `ProfileEditor.tsx:165-170`; `ProvisioningRunsPage.tsx:423` | B | **done** |
| 2 | The agent is in the image (Windows answer file runs `agent.exe enroll --token`) | INSTALLER (**uncertain**: only a proxy; the platform cannot see inside an image) | agent.release.read | `/agent-packages` · "Get the agent installer" | `modules/provisioning/render.py:169`; Linux uses a placeholder URL `render.py:206` | A | **done** (`ProvisioningPrerequisites`, "Before a built machine can join"; the done text says an image's contents cannot be seen) |
| (n/a) | `/credentials` is **not** used | | | | `provisioning/repository.py:37-42` | | do not add |

### Admin: Automation

#### `/assistant/autonomy`: AI automation
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | The assistant is enabled | `getAiStatus().enabled === false` | ai.use (**uncertain** that every `ai.admin` holder has `ai.use`) | `/assistant/settings` | `ai_admin/api.py:435-448` | B | **done** (`AutomationPrerequisites`, "Before the assistant can act") |
| 2 | Approved actions exist to be proposed | `listApprovedActions().length === 0` | health.read | `/health#approved-actions` (needs a create UI; see Alerts #5) | `core/ai/act.py:69, 454` | B | **done** (links to the list; creating one still needs the UI in §3) |

#### `/jobs`: Job history
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | Computers run the agent | FLEET `managed === 0` | devices.read | `/devices/onboard` | `modules/jobs/JobsPage.tsx:530, 601` (empty states) | A | **done** ("Before jobs have somewhere to run") |

### Admin: Security

#### `/credentials`: Credentials
No prerequisite. **Reverse CTA, advisory:** a saved `push_install` credential with no range binding → `/agent-push/credential-scopes` ("Assign it to a range"). Detect with `listPushCredentialScopes(true)` (agent.push.scope.manage). **Done** (`CredentialPrerequisites`, "Before a saved credential can be used", reading the assigned-only list; shown only while a push install credential exists).
**Uncertain, verify before telling operators:** the types `windows_admin`, `ssh_key` and `domain_join` (`modules/credentials/credentialTypes.ts:27-45`) appear to have no consumer. Screen sessions refuse credentials (`remote/service.py:403-404`) and provisioning seals its own (`provisioning/repository.py:37-42`). The nav hint "Stored passwords for unattended sessions and imaging" (`shell/nav.ts`) would then be stale.

#### `/audit`: Audit log
No prerequisite.

#### `/security`: Security logs
| # | Prerequisite | Detect | Perm | CTA | Evidence | Sev | Status |
|---|---|---|---|---|---|---|---|
| 1 | A Windows or macOS computer runs the agent | `listDevices({ osFamily: "windows", limit: 1 }).total + (macos) === 0` | devices.read | `/devices/onboard` · "Add a Windows or Mac" | `modules/security_logs/schedule.py:23` "Only these platforms have a collector"; `SecurityPage.tsx:447-449` "No events in this window" | A | **done** (`SecurityPrerequisites`; agents per family are `state_counts.all - assets`) |

### Admin: Platform

- `/platform-status`, `/error-monitor`: no prerequisite.
- `/settings`: **Users** → email relay: **done**, the banner links "Set up email" to `settingsHref("email")`. **Health** tab → ticket queues/categories (seeded; no CTA needed): **done** anyway, the "pick a queue and a category" hint is now a link to the tab's own `#alert-tickets` group, and the empty queue/category hints on the Health and Notifications tabs link to `/service-desk/admin`. `HealthSettingsPanel` on `/health` links "Choose where alert tickets go" to `settingsHref("health")`. **Assistant** tab → provider is server `.env` (no CTA). No other cross-module prerequisite.

### Unlisted routes operators land on

- `/devices/onboard`: the journey (done).
- `/discovered`: links to `/ipam` exist (`CandidatesPage.tsx:243`). Done.
- `/software`: needs agents; `SoftwarePage.tsx:129` "No software discovered yet." → FLEET → `/devices/onboard`. A, new.
- `/people/*`: seat/licence holders. Covered by the Subscriptions #3 reverse link.

---

## 3. What needs something new beyond a frontend CTA

| Need | Kind | Why |
|---|---|---|
| Settings `?tab=` deep link | frontend | Nine CTAs target a Settings tab (§0.1) |
| `/health` anchors | frontend | Health profiles and approved actions (§0.2) |
| Approved-action **create** UI | frontend (backend exists) | Alerts #5, Assistant #2, AI automation #2 |
| License **allocate** UI | frontend (backend exists) | Licenses #4 |
| `ApiClient.listRemotePolicies()` + policy editor | frontend (backend exists: `GET/PUT /remote/policies`) | Remote #3 |
| Per-device screen-share capability on `GET /devices` | **new backend field**; **built 2026-09-24**: `screen_capable` (nullable, additive, no migration: derived from `agents.capabilities`) | Remote #2 |
| Distinct holders of a permission code (e.g. `change.approve`) | **new backend endpoint** (optional; `listRoles` gives an approximation) | Changes #1, BCP #4 |
| `portal_visible` filter on `GET /docs/articles` | **new backend query param** (optional; first-page check works up to 100 articles) | Portal #2 |

**Strictly needs a new backend endpoint/field: 1** (Remote #2). **Optional, for exactness: 2** (permission-holder count; portal_visible filter). **Backend exists but no frontend method or UI: 3** (approved-action create, licence allocate, remote policies).

## 4. Stale or wrong copy found along the way (fix while adding CTAs)

- `deploy/pickerOptions.ts:44-50`, `bcp/pickerOptions.ts:34-41`, `backup` `DEVICE_PICKER_EMPTY_HINT`: nav paths "Fleet → Packages", "Devices → Enrollment", "Devices → Groups", "Governance → IT documentation" no longer match `nav.ts`. Replace them with links. **Fixed 2026-09-24 for deploy, backup, remote, service-desk and bcp** (paths corrected or removed; the dialogs and the plan editor link "Add a computer" / "Create a device group" when the list loads empty).
- ~~`subscriptions/pickerOptions.ts:37-38`: says users are created under "Settings → Organization". They are on Settings → Users.~~ **Fixed**, with an "Add a user" link.
- ~~`patch/PatchPolicyEditor.tsx:142-144, 334`: "no route lists device groups". `GET /device-groups` exists.~~ **Fixed 2026-09-24**: the editor lists groups and aims a policy at one.
- `change/ChangeDetail.tsx:569-574`: "no route that lists patch runs or patch approvals". `listPatchRuns` (`GET /patch/runs`) and `listPatchApprovals` (`GET /patch/approvals`) exist, so both could be pickers. **Copy fixed** (the note and comments no longer claim the console cannot list them); **the pickers themselves are not built** and remain the real fix.
- ~~`site-servers/SiteServerList.tsx:73`: "A segment with no site server gets no sweeps". False for sweeps (true for wakes and push).~~ Fixed.
- `provisioning/render.py:206`: Linux answer file hard-codes `https://control-plane.local/agent/install.sh` (placeholder; out of scope, flagged only).

## 5. Counts

- Pages covered: **35** nav destinations (9 Daily + 4 Governance + 4 Assets & Spend + 18 Admin), plus 4 unlisted routes.
- Pages with at least one cross-module prerequisite: **29**. No prerequisite: IT documentation, Audit log, Platform status, Error monitor, Credentials (reverse only), and Settings (only its existing email banner).
- Dependency rows: **88** numbered rows in the tables (Push install's steps 1-3 count as 3; the unnumbered notes on Credentials, Settings and `/software` are not counted). **13** are already **done**. **75 need work**: 57 new, 18 where a sentence exists without a link or with stale wording.
- Rows whose detection needs a new backend endpoint or field: **1 required** (Remote #2), **2 optional** for exactness (Changes #1 / BCP #4 approver count; Portal #2 article filter).

## 6. The ten highest-value CTAs

1. **Push install: site server for the range** (Push #4). A blocking refusal that `PushReadiness` doesn't mention yet (`agent_push/preflight.py:136-143`). Add it as a fourth step.
2. **Push install: published release** (Push #5). Blocking (`agent_push/dispatch.py:190-191`). Add it as a fifth step.
3. **Backup: no enabled policy.** "Back up now" silently does nothing (`backup/dispatch.py:91-100`). CTA to `/backup/policies` (plus repository first).
4. **Alerts: no health profile assigned.** The fleet is silently unmonitored (`health/baseline.py:3`, `health/api.py:863-866`). CTA to profiles.
5. **Enrollment: no installer published.** A token alone installs nothing. CTA to `/agent-packages#releases`.
6. **Software and Patching: change gate needs an approved change.** A 409 surprise (`change_mgmt/gates.py:151-153`, `patch/api.py:1071`). Warn up front with `listChangeGates()` and CTA to `/changes`.
7. **Agent updates: no device group for the pilot ring.** Rollout cannot start (`agent_update/service.py:208-213`). CTA to `/device-groups`.
8. **IPAM sweep panel: "Not authorized" with no link.** CTA to `/ipam/scan-authorizations`; no-agent case → `/agent-packages`. **Built (2026-09-24)** in `ipam/SweepPanel.tsx`, plus the estate-wide panel on `/ipam`.
9. **Tickets: no SLA policy.** Tickets show "No SLA" with no way forward. CTA to `/sla` (sla.read, with `getTicketSla` fallback).
10. **Settings `?tab=` deep link.** Not a CTA itself, but it unlocks the email banner, alert-ticket defaults, requester invites, change approvers and portal requesters (nine rows).

Next in line: Remote "no agent online" (blocking, tile only); End-user portal "no requester accounts" (blocking); Changes "no second approver" (blocking; **built 2026-09-24** with the `listRoles` approximation); the approved-action create UI (it unblocks three pages).
