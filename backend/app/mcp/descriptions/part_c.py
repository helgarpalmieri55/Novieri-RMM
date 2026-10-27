"""MCP tool descriptions, part C (see `__init__`)."""

PART_C: dict[str, str] = {
    # --- allocations ---
    "POST /api/v1/allocations/{allocation_id}/release": (
        "Releases a license allocation, freeing the seat in its license pool, and returns the "
        "allocation with its release time. Refuses an allocation that was already released."
    ),
    # --- bcp ---
    "PATCH /api/v1/bcp/findings/{finding_id}": (
        "Updates a DR test finding's remediation state (open, in_progress, resolved), "
        "description, or linked change request. Resolving stamps the resolution time and "
        "reopening clears it; an unknown change, or one of another client, is refused."
    ),
    "GET /api/v1/bcp/plans": (
        "Lists the organization's business-continuity and disaster-recovery plans with scope, RTO "
        "and RPO targets, review interval, and state. Records no audit event."
    ),
    "POST /api/v1/bcp/plans": (
        "Creates a BCP/DR plan of the client given by client_id, with its scope, RTO and RPO "
        "targets in minutes, procedures, dependencies and review interval. The client must use "
        "continuity; scope and procedures of another client are refused."
    ),
    "GET /api/v1/bcp/plans/status": (
        "Returns a dashboard row per plan: whether its review is overdue, its last executed test "
        "outcome, its open finding count, and live restore-proven backup coverage of the devices "
        "in scope."
    ),
    "GET /api/v1/bcp/plans/{plan_id}": (
        "Returns one BCP/DR plan with its targets, procedures, dependencies, review dates and "
        "state."
    ),
    "PATCH /api/v1/bcp/plans/{plan_id}": (
        "Edits a BCP/DR plan's name, scope, RTO and RPO, procedures, dependencies, review "
        "interval, or state (active or archived). Returns the updated plan; invalid scope or "
        "procedure references, or ones of another client, are refused."
    ),
    "POST /api/v1/bcp/plans/{plan_id}/review": (
        "Records a review sign-off on a plan, stamping the review time and reviewer, which resets "
        "its overdue-review clock. The plan's author may sign off their own plan."
    ),
    "GET /api/v1/bcp/plans/{plan_id}/tests": (
        "Lists the DR tests of one plan, pending and executed, with outcome, RTO and RPO results "
        "and the frozen backup evidence captured when each was recorded."
    ),
    "POST /api/v1/bcp/plans/{plan_id}/tests": (
        "Schedules a DR test run (restore_drill, failover, tabletop or other) against a plan. The "
        "test starts pending with no outcome until PATCH /bcp/tests/{test_id} records it."
    ),
    "GET /api/v1/bcp/report/period": (
        "Returns an org-wide DR report for a time window: test outcome counts, plans tested, "
        "currently open findings by severity, and plans overdue for review. Writes a "
        "report-export audit event."
    ),
    "GET /api/v1/bcp/tests/{test_id}": (
        "Returns one DR test with its type, schedule, outcome, RTO and RPO results, notes, and "
        "the frozen backup evidence snapshot taken when it was recorded."
    ),
    "PATCH /api/v1/bcp/tests/{test_id}": (
        "Records a DR test's result (pass, partial or fail), RTO and RPO met flags, notes and "
        "execution time. Captures a frozen snapshot of backup restore evidence for the plan's "
        "scope as of now."
    ),
    "GET /api/v1/bcp/tests/{test_id}/findings": (
        "Lists the findings raised against one DR test, with severity and remediation state. "
        "Records no audit event."
    ),
    "POST /api/v1/bcp/tests/{test_id}/findings": (
        "Raises a finding of a given severity (low to critical) against a DR test. It starts "
        "open; use PATCH /bcp/findings/{finding_id} to progress or resolve it."
    ),
    "GET /api/v1/bcp/tests/{test_id}/report": (
        "Returns the SOC 2 style evidence bundle for one DR test: the test, its steps, findings "
        "and frozen backup evidence. Writes a report-export audit event."
    ),
    "PUT /api/v1/bcp/tests/{test_id}/steps": (
        "Creates or replaces the step at a given ordinal of a DR test, with its description, "
        "result (pending, pass, fail) and optional evidence reference. Returns the step."
    ),
    # --- changes ---
    "GET /api/v1/changes": (
        "Lists the organization's change requests with type, risk, affected scope, maintenance "
        "window, and current state."
    ),
    "POST /api/v1/changes": (
        "Creates a draft change request (standard, normal or emergency) of the client given by "
        "client_id, which must use change management, with risk, affected scope, window and "
        "rollback plan. It is not submitted until POST /changes/{change_id}/submit."
    ),
    "GET /api/v1/changes/calendar": (
        "Returns the changes whose maintenance window overlaps a from/to range, plus the pairs of "
        "those changes whose windows conflict. Changes without a scheduled window never appear."
    ),
    "GET /api/v1/changes/evidence": (
        "Returns evidence bundles (change, approvals, links, audit references) for every change "
        "in the organization. Unlike the single-change export, it writes no audit event."
    ),
    "GET /api/v1/changes/gates": (
        "Lists the change gates, each saying whether patch approvals, deployments or both require "
        "an approved change for one device group or for every device."
    ),
    "PUT /api/v1/changes/gates": (
        "Creates or replaces the change gate for one device group, whose client must use change "
        "management, or for all devices when none is given, covering patch approvals, deployments "
        "or both. Clients without change management meet no gate."
    ),
    "DELETE /api/v1/changes/gates/{gate_id}": (
        "Removes a change gate, so patch approvals and deployments on that scope no longer need "
        "an approved change."
    ),
    "GET /api/v1/changes/{change_id}": (
        "Returns one change request with its type, risk, affected scope, window, rollback plan, "
        "state and origin."
    ),
    "PATCH /api/v1/changes/{change_id}": (
        "Edits a change request's type, title, description, risk, scope, window or rollback plan. "
        "Only a draft can be edited; any other state is refused."
    ),
    "GET /api/v1/changes/{change_id}/approvals": (
        "Lists a change's approval and rejection decisions, with comments and whether each was "
        "retrospective. Records no audit event, unlike the evidence export."
    ),
    "POST /api/v1/changes/{change_id}/approve": (
        "Records an approval decision on a change, optionally with a comment. The change's "
        "requester cannot approve their own change; an emergency change already in progress takes "
        "it as a retrospective approval."
    ),
    "GET /api/v1/changes/{change_id}/evidence": (
        "Exports one change's evidence bundle: the change, its approvals, links to patch runs and "
        "deployments, and audit references. Writes a change evidence-exported audit event."
    ),
    "GET /api/v1/changes/{change_id}/links": (
        "Lists the patch runs, deployments, provisioning runs and patch approvals linked to a "
        "change as the work it authorizes."
    ),
    "POST /api/v1/changes/{change_id}/links": (
        "Links a patch run, deployment, provision run or patch approval to a change. The target "
        "must be of the change's client, or global; a duplicate link is refused."
    ),
    "DELETE /api/v1/changes/{change_id}/links/{link_id}": (
        "Removes a link between a change and the patch run, deployment or other action it was "
        "attached to."
    ),
    "POST /api/v1/changes/{change_id}/reject": (
        "Records a rejection decision on a change, optionally with a comment. The change's "
        "requester cannot reject their own change; a standard or normal change moves to rejected."
    ),
    "POST /api/v1/changes/{change_id}/submit": (
        "Submits a draft change for approval and pushes it to the connected ticketing adapter. "
        "Refused from any other state."
    ),
    "POST /api/v1/changes/{change_id}/transition": (
        "Moves a change through begin, complete, cancel or revise, refusing illegal moves. An "
        "emergency change cannot complete without a retrospective approval; terminal states close "
        "the linked ticket."
    ),
    # --- devices ---
    "GET /api/v1/devices": (
        "Lists the fleet one page at a time, filterable by OS, status, provenance, search text "
        "and tab, with total, per-tab and fleet-wide counts. The assigned person appears only on "
        "devices of clients where the caller also holds the people read permission."
    ),
    "POST /api/v1/devices": (
        "Records an asset by hand, such as warehouse stock or equipment that cannot run an agent. "
        "It creates a device row without an agent, marked as operator provenance, so the "
        "record counts as vouched for and may be a push install target."
    ),
    "GET /api/v1/devices/candidates": (
        "Lists addresses the fleet has seen that the inventory cannot account for, such as sweep "
        "findings. It is a read and creates no device rows."
    ),
    "POST /api/v1/devices/candidates/promote": (
        "Turns selected candidate addresses into asset records, with per-address results. "
        "Already-promoted addresses are not errors, and an address that is no longer a candidate "
        "is refused individually. A promoted record counts as vouched for and may be a push "
        "install target."
    ),
    "POST /api/v1/devices/review/approve-all": (
        "Approves every listed computer still waiting for review after joining through the "
        "install link, then returns the counts. The request must carry the SHA-256 digest of the "
        "exact list shown, up to 1,000 computers."
    ),
    "GET /api/v1/devices/suggestions": (
        "Lists open suggested matches: pairs of device records an enrolling agent could not "
        "safely be linked to, with the reason for each. Read-only."
    ),
    "POST /api/v1/devices/suggestions/{suggestion_id}/resolve": (
        "Resolves a suggested match as rejected (two different machines, nothing changes) or "
        "linked. Linking moves the agent onto the kept record and retires the enrolled row; an "
        "already-resolved suggestion is refused."
    ),
    "GET /api/v1/devices/{device_id}": (
        "Returns one device with its agent status, certificate expiry and revocation, "
        "screen-share capability, and asset fields. A rejected device is not found."
    ),
    "PATCH /api/v1/devices/{device_id}": (
        "Edits asset fields on a device; only the fields sent are written. Hostname, OS family, "
        "serial, FQDN and architecture are refused on a device with an agent, as inventory would "
        "overwrite them."
    ),
    "GET /api/v1/devices/{device_id}/health": (
        "Returns a device's effective health profile and the assignment that produced it, each "
        "check's current state, open alerts and policy version drift. No profile means nothing is "
        "monitored; a check with no state is unknown."
    ),
    "POST /api/v1/devices/{device_id}/health/check-now": (
        "Dispatches a signed job asking the device's agent to sample its health checks now. "
        "Limited to one per device per minute and 500 per organization per hour; over the limit "
        "it is refused."
    ),
    "GET /api/v1/devices/{device_id}/inventory": (
        "Returns a device's latest inventory: hardware components, installed software, reported "
        "facts, summary and the time of the last snapshot."
    ),
    "GET /api/v1/devices/{device_id}/inventory/changes": (
        "Returns a page of the device's inventory change history (hardware and software added, "
        "removed or changed), newest first, with a total count."
    ),
    "POST /api/v1/devices/{device_id}/review/acknowledge-removal": (
        "Records that the agent of a rejected computer was removed by hand. Only valid for a "
        "rejection that may have left the agent behind; the row then leaves the "
        "waiting-for-review list."
    ),
    "POST /api/v1/devices/{device_id}/review/approve": (
        "Approves a computer that joined through the install link and is waiting for review. From "
        "then on it is an ordinary device that receives every job its organization sends."
    ),
    "POST /api/v1/devices/{device_id}/review/reject": (
        "Rejects a computer that joined through the install link: its agent is told to uninstall "
        "itself and its certificate is revoked. Very old agents cannot, so the response says if "
        "manual removal is needed."
    ),
    # --- licenses ---
    "GET /api/v1/licenses": (
        "Lists the organization's software licenses with type, entitlement count, cost, renewal "
        "and maintenance dates, and whether a key is stored. The key itself is never returned."
    ),
    "POST /api/v1/licenses": (
        "Creates a license of the client given by client_id for a software product, with its "
        "entitlement count, cost, dates, vendor and optional key. The key is sealed in the vault "
        "and never returned. Unknown product or vendor is refused; Unassigned takes no licenses."
    ),
    "GET /api/v1/licenses/compliance": (
        "Returns compliance per client and product: that client's licensed entitlements, its "
        "devices found running the product, the difference, and a breach flag when installs "
        "exceed entitlements. Computed live; clients are never added together."
    ),
    "GET /api/v1/licenses/costs": (
        "Returns total license cost grouped by client, product and currency. Licenses lacking a "
        "cost or currency are skipped; clients and currencies are never added together."
    ),
    "GET /api/v1/licenses/export": (
        "Returns flat license rows (product, entitlements, cost, dates) for true-up or reporting, "
        "without keys. Writes a license export audit event."
    ),
    "GET /api/v1/licenses/products": (
        "Lists software products, each with the match rules used to recognize its installs in "
        "device inventory."
    ),
    "POST /api/v1/licenses/products": (
        "Creates a software product with a name, publisher and match rules that decide which "
        "installed software counts as it. Invalid match rules are refused."
    ),
    "GET /api/v1/licenses/products/{product_id}": (
        "Returns one software product with its publisher and match rules."
    ),
    "PATCH /api/v1/licenses/products/{product_id}": (
        "Edits a software product's name, publisher or match rules. Changed match rules change "
        "what compliance counts as installed; invalid rules are refused."
    ),
    "GET /api/v1/licenses/renewals": (
        "Lists licenses whose renewal or maintenance expiry falls within the next N days (default "
        "30), one row per triggering date with days until due, soonest first."
    ),
    "GET /api/v1/licenses/review-queue": (
        "Lists installed software names that match no product's rules and are not ignored, with "
        "how many devices run each, most common first. Resolve entries with the map or ignore "
        "tools."
    ),
    "POST /api/v1/licenses/review-queue/ignore": (
        "Marks a review-queue software name as ignored so it stops appearing there: on the "
        "devices of the client given by client_id, or, without one, on every client's (which "
        "needs access to every client). Safe to repeat for the same name."
    ),
    "POST /api/v1/licenses/review-queue/map": (
        "Resolves a review-queue software name by adding an exact-match rule for it to a chosen "
        "product, so those installs count toward that product's compliance. Returns the product."
    ),
    "DELETE /api/v1/licenses/{license_id}": (
        "Permanently deletes a license record. A license that still has allocations may be "
        "refused, so release them first. It is audited."
    ),
    "GET /api/v1/licenses/{license_id}": (
        "Returns one license with its type, entitlements, cost, dates and whether a key is "
        "stored. The key itself is never returned."
    ),
    "PATCH /api/v1/licenses/{license_id}": (
        "Edits a license's product, vendor, entitlements, cost or dates. A new key is resealed in "
        "the vault and an empty key clears it; the key is never returned."
    ),
    "GET /api/v1/licenses/{license_id}/allocations": (
        "Lists a license's seat allocations to devices, users or people, including released ones "
        "with their release time."
    ),
    "POST /api/v1/licenses/{license_id}/allocations": (
        "Allocates a seat of a license to a device, user or person of the license's client (staff "
        "hold only the internal client's licenses). Refused when all entitlements are in use; "
        "returns the allocation."
    ),
    # --- notifications ---
    "GET /api/v1/notifications/allowlist": (
        "Lists the webhook allowlist: the hosts and ports that webhook channels are permitted to "
        "call."
    ),
    "POST /api/v1/notifications/allowlist": (
        "Allowlists a host and port for webhook delivery. A single host, not a CIDR range, and "
        "the name is lowercased; a duplicate host and port is refused."
    ),
    "DELETE /api/v1/notifications/allowlist/{entry_id}": (
        "Removes a webhook allowlist entry, after which webhook channels can no longer deliver to "
        "that host and port."
    ),
    "DELETE /api/v1/notifications/channels/{channel_id}": (
        "Deletes a notification channel. Rules or delivery history that still reference it "
        "can cause the deletion to be refused, and delivery history cannot be removed, so a "
        "channel that has sent messages may be undeletable."
    ),
    "POST /api/v1/notifications/channels/{channel_id}/test": (
        "Sends a real test message to the channel's destination under the same egress guard as "
        "live delivery. Returns delivered, status code and a coarse error; limited to 5 per "
        "minute per organization."
    ),
    "GET /api/v1/notifications/deliveries": (
        "Returns a page of notification deliveries with status (pending, sent, failed, dead), "
        "attempts and last error, filterable by status, channel, event type and time range, with "
        "a total."
    ),
    "GET /api/v1/notifications/queues/{queue_id}/members": (
        "Lists the users who are members of a ticket queue, which queue_members notification "
        "rules deliver to."
    ),
    "DELETE /api/v1/notifications/queues/{queue_id}/members/{user_id}": (
        "Removes a user from a ticket queue, so queue_members notification rules stop reaching "
        "them."
    ),
    "PUT /api/v1/notifications/queues/{queue_id}/members/{user_id}": (
        "Adds a user to a ticket queue so queue_members notification rules reach them. Safe to "
        "repeat; an unknown queue or user is a not-found."
    ),
    "GET /api/v1/notifications/rules": (
        "Lists notification routing rules, each sending an event type to a channel for a "
        "recipient kind (queue members, assignee, requester or channel default)."
    ),
    "POST /api/v1/notifications/rules": (
        "Creates a rule routing an event type to a channel and recipient kind. Staff-only events "
        "cannot be sent to the requester, and the channel and queue must exist."
    ),
    "DELETE /api/v1/notifications/rules/{rule_id}": (
        "Deletes a notification routing rule, so its event type no longer notifies that channel "
        "and recipient."
    ),
    "PATCH /api/v1/notifications/rules/{rule_id}": (
        "Turns a notification rule on or off. Only the active flag can change; event type, "
        "channel and recipient are fixed at creation."
    ),
    # --- people ---
    "GET /api/v1/people": (
        "Lists people (staff, not just accounts) with status, email and how many devices each "
        "uses, filterable by name search and status. First creates a person for any account that "
        "lacks one."
    ),
    "POST /api/v1/people": (
        "Creates a person of the client given by client_id, with name and optional email, "
        "department, job title and start date. An email already held in that client is refused; "
        "Unassigned takes no people."
    ),
    "GET /api/v1/people/by-user/{user_id}": (
        "Returns the person behind a console or portal account. First creates a person for any "
        "account that lacks one; not found if the account does not exist."
    ),
    "GET /api/v1/people/devices/{device_id}": (
        "Returns who uses a device and how that was decided: an operator override or the sign-in "
        "name inventory last saw, with that observed login."
    ),
    "PUT /api/v1/people/devices/{device_id}": (
        "Sets, or with a null person clears, who uses a device as an operator override that "
        "inventory never replaces. The person must belong to the device's client. It also "
        "teaches the resolver the device's last observed sign-in name."
    ),
    "DELETE /api/v1/people/devices/{device_id}/override": (
        "Removes an operator's override of who uses a device and returns to automatic assignment, "
        "re-resolved from the last observed sign-in name."
    ),
    "GET /api/v1/people/unmatched": (
        "Lists subscription seats, devices and license allocations not yet linked to a person, so "
        "they can be matched up."
    ),
    "POST /api/v1/people/unmatched/seats/{seat_id}/link": (
        "Links a subscription seat to a person of the subscription's client. With no person given "
        "it reuses that client's person holding the seat's email address or creates one there "
        "from the seat's name and email."
    ),
    "GET /api/v1/people/{person_id}": (
        "Returns everything one person holds: devices, licenses, subscriptions, open tickets, "
        "account, sign-in names and open checklist. Sections the caller may not read are null, "
        "not empty."
    ),
    "PATCH /api/v1/people/{person_id}": (
        "Edits a person's name, email, department, job title, start date or end date. A name "
        "cannot be blank and an email already held by someone else is refused."
    ),
    "PUT /api/v1/people/{person_id}/account": (
        "Links a person to a console or portal account, or unlinks with a null user. A contact's "
        "person is of its client and staff's of the internal client; an account already "
        "belonging to another person is refused."
    ),
    "POST /api/v1/people/{person_id}/checklists": (
        "Starts a starter or leaver checklist for a person, setting their status to starting or "
        "leaving. A leaver checklist needs the offboard permission; it only records work and "
        "performs none."
    ),
    "POST /api/v1/people/{person_id}/checklists/{checklist_id}/cancel": (
        "Cancels an open checklist and returns the person to active. A leaver checklist needs the "
        "offboard permission."
    ),
    "POST /api/v1/people/{person_id}/checklists/{checklist_id}/complete": (
        "Completes an open checklist once no step is pending, setting a person active, or left "
        "(with an end date) for a leaver. It performs no offboarding itself; leaver needs the "
        "offboard permission."
    ),
    "PATCH /api/v1/people/{person_id}/checklists/{checklist_id}/steps/{key}": (
        "Marks a checklist step pending, done or skipped with a note. Skipping needs a note, as "
        "does marking done a step the live check shows is unmet. Leaver checklists need the "
        "offboard permission."
    ),
    "POST /api/v1/people/{person_id}/logins": (
        "Adds a sign-in name used to attribute devices to a person. Repeating it for the same "
        "person is harmless; a name belonging to someone else is refused."
    ),
    "DELETE /api/v1/people/{person_id}/logins/{login_id}": (
        "Removes a sign-in name from a person, so devices observed under it are no longer "
        "attributed to them automatically."
    ),
    # --- seats ---
    "POST /api/v1/seats/{seat_id}/release": (
        "Releases a subscription seat, freeing it in the subscription's pool, and records a "
        "seat-change event. Refuses a seat that was already released."
    ),
    # --- sla ---
    "GET /api/v1/sla/calendars": (
        "Lists SLA business-hours calendars with a health flag showing whether each has a valid "
        "timezone and working windows. A calendar without windows leaves tickets with no SLA "
        "clock."
    ),
    "POST /api/v1/sla/calendars": (
        "Creates an SLA calendar with a name and IANA timezone. It starts with no working "
        "windows, so set them with PUT /sla/calendars/{calendar_id}/windows before policies can "
        "use it."
    ),
    "DELETE /api/v1/sla/calendars/{calendar_id}": (
        "Deletes an SLA calendar. Refused with the blockers named if any policy or any ticket SLA "
        "record still uses it."
    ),
    "GET /api/v1/sla/calendars/{calendar_id}": (
        "Returns one SLA calendar with its timezone, weekly working windows and health flag."
    ),
    "PATCH /api/v1/sla/calendars/{calendar_id}": (
        "Edits an SLA calendar's name, timezone or active flag. A timezone change recomputes the "
        "deadlines of every running SLA clock on the calendar in the same transaction."
    ),
    "GET /api/v1/sla/calendars/{calendar_id}/holidays": (
        "Lists the non-working holiday dates on an SLA calendar."
    ),
    "POST /api/v1/sla/calendars/{calendar_id}/holidays": (
        "Adds a holiday date to an SLA calendar. The deadlines of running SLA clocks on that "
        "calendar are recomputed in the same transaction."
    ),
    "DELETE /api/v1/sla/calendars/{calendar_id}/holidays/{holiday_id}": (
        "Removes a holiday from an SLA calendar, recomputing the deadlines of running SLA clocks "
        "on it in the same transaction."
    ),
    "PUT /api/v1/sla/calendars/{calendar_id}/windows": (
        "Replaces the calendar's whole weekly set of working windows (weekday, start, end) rather "
        "than editing one. Running SLA deadlines on the calendar are recomputed in the same "
        "transaction."
    ),
    "GET /api/v1/sla/policies": (
        "Lists SLA policies, each with response and resolution targets in minutes, a calendar, "
        "optional queue and priority scope, and escalation settings."
    ),
    "POST /api/v1/sla/policies": (
        "Creates an SLA policy with a calendar, response and resolution targets in minutes, "
        "optional queue and priority scope, and optional escalation to a priority or queue on "
        "breach. With client_id it is that client's, whose tickets then use only its own "
        "policies; without, it is global."
    ),
    "DELETE /api/v1/sla/policies/{policy_id}": (
        "Deletes an SLA policy. Refused, naming the tickets, if any ticket's SLA record still "
        "references it; set it inactive to retire it instead."
    ),
    "GET /api/v1/sla/policies/{policy_id}": (
        "Returns one SLA policy with its targets, calendar, queue and priority scope, and "
        "escalation settings."
    ),
    "PATCH /api/v1/sla/policies/{policy_id}": (
        "Edits an SLA policy. Changing a target or its calendar recomputes the deadlines of every "
        "running SLA clock under the policy; a null queue or priority makes it a wildcard."
    ),
    "GET /api/v1/sla/settings": (
        "Returns the auto-close window in days, whether it was ever configured, and counts of "
        "misconfigured tickets: those with no SLA record and those with stale deadlines."
    ),
    "PUT /api/v1/sla/settings": (
        "Sets how many days a resolved or fulfilled ticket waits before the system automatically "
        "closes it. This changes when tickets are closed organization-wide."
    ),
}
