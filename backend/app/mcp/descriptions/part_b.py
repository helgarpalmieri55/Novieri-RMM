"""MCP tool descriptions, part B (see `__init__`)."""

PART_B: dict[str, str] = {
    # --- agent ---
    "GET /api/v1/agent/fleet-versions": (
        "Counts this organization's devices by installed agent version and returns the latest "
        "stable release, how many devices are behind it, and the devices that need a manual "
        "update. Read-only."
    ),
    "GET /api/v1/agent/releases": (
        "Lists every published agent release (version, OS, architecture, channel, "
        "signature state), newest first. The catalogue is shared by all organizations."
    ),
    "POST /api/v1/agent/releases": (
        "Registers a release row for a version, OS and architecture so an artifact can be "
        "uploaded for it. Refuses a duplicate triple. The catalogue is shared by all "
        "organizations."
    ),
    "DELETE /api/v1/agent/releases/versions/{version}": (
        "Unpublishes every platform's package of one agent version and deletes the stored "
        "files. Refused while an active rollout uses it. The catalogue is shared, so this "
        "affects all organizations."
    ),
    "POST /api/v1/agent/releases/versions/{version}/promote": (
        "Sets the release channel (for example stable) of every OS and architecture row of "
        "one agent version. Non-pilot rollout rings accept only stable versions; pilot rings "
        "may use an unpromoted one. Affects the shared catalogue."
    ),
    "DELETE /api/v1/agent/releases/{release_id}": (
        "Unpublishes one agent release and deletes its stored file. Refused while an active "
        "rollout uses it. The catalogue is shared, so this affects all organizations."
    ),
    "GET /api/v1/agent/releases/{release_id}/deployment-package": (
        "Returns a staging and install procedure for one release (for example for GPO "
        "rollout) with a time-limited download URL and SHA-256. Mints no enrollment token. "
        "Refused if the release has no uploaded artifact."
    ),
    "PUT /api/v1/agent/releases/{release_id}/release-signature": (
        "Sets the detached signature recorded for one agent release, which agents verify "
        "before applying a self-update. Replaces any previous value and is audited."
    ),
    "GET /api/v1/agent/update-rollouts": (
        "Lists this organization's agent self-update rollouts with their state, target "
        "version and rings."
    ),
    "POST /api/v1/agent/update-rollouts": (
        "Creates a draft agent update rollout (target version, ordered rings, batch size, "
        "failure threshold). Sends nothing until it is started. Creating a rollback needs "
        "an additional permission."
    ),
    "POST /api/v1/agent/update-rollouts/preview": (
        "Dry run of a rollout: shows per ring how many devices would be targeted and how "
        "many are eligible for the target version. Writes and dispatches nothing."
    ),
    "GET /api/v1/agent/update-rollouts/{rollout_id}": (
        "Returns one agent update rollout with its state, pause reason, rings and "
        "per-ring target counts."
    ),
    "POST /api/v1/agent/update-rollouts/{rollout_id}/advance": (
        "Activates the next pending ring of a running rollout, which then dispatches "
        "agent updates to that ring's devices. Refused while a ring is still active. Always "
        "a person's decision."
    ),
    "POST /api/v1/agent/update-rollouts/{rollout_id}/cancel": (
        "Cancels a draft, running or paused rollout and skips its pending targets. Updates "
        "already in flight finish or roll back on their own."
    ),
    "POST /api/v1/agent/update-rollouts/{rollout_id}/pause": (
        "Pauses a running rollout so no further updates are dispatched. Updates already in "
        "flight continue. Requires the rollout to be running."
    ),
    "POST /api/v1/agent/update-rollouts/{rollout_id}/resume": (
        "Resumes a paused rollout, so it dispatches agent updates to pending targets again. "
        "Requires the rollout to be paused."
    ),
    "POST /api/v1/agent/update-rollouts/{rollout_id}/rings/{ring_id}/close": (
        "Closes the active ring by skipping its still-pending targets (typically offline "
        "devices) so the rollout can advance. Dispatches nothing."
    ),
    "POST /api/v1/agent/update-rollouts/{rollout_id}/start": (
        "Starts a draft rollout: fixes its targets and activates ring 0, after which signed "
        "agent-update jobs go to those devices. Refused if another rollout is active."
    ),
    "GET /api/v1/agent/update-rollouts/{rollout_id}/targets": (
        "Lists the per-device targets of a rollout with from-version, state, reason, "
        "attempts and job id."
    ),
    # --- agent-push ---
    "GET /api/v1/agent-push/authorizations": (
        "Lists the network prefixes each client has authorized for credentialed agent push "
        "installs, including expired ones. Revoked grants only if asked."
    ),
    "POST /api/v1/agent-push/authorizations": (
        "Authorizes a network prefix of one named client as a place where that client's "
        "push credentials may be presented to its machines. High privilege; widens what "
        "pushes can reach."
    ),
    "DELETE /api/v1/agent-push/authorizations/{authorization_id}": (
        "Revokes a push authorization so its prefix is no longer a valid push target. "
        "The row is kept for audit."
    ),
    "GET /api/v1/agent-push/credential-scopes": (
        "Lists which stored push-install credentials are bound to which address prefixes. "
        "Names the credentials but never returns a secret."
    ),
    "POST /api/v1/agent-push/credential-scopes": (
        "Binds a dedicated push-install credential to an address prefix of its client, "
        "allowing it to be presented there. High privilege. Refuses any other credential type."
    ),
    "DELETE /api/v1/agent-push/credential-scopes/{scope_id}": (
        "Unbinds a credential from a prefix so it can no longer be presented to addresses "
        "there. The row is kept for audit."
    ),
    "GET /api/v1/agent-push/host-keys": (
        "Lists the host-key fingerprints observed on push targets (SSH or WinRM) and whether "
        "a person confirmed each. Public key material only."
    ),
    "POST /api/v1/agent-push/host-keys/confirm": (
        "Confirms the observed host-key fingerprint of an address as the trust anchor for "
        "later pushes to it. High consequence; must equal the last probe's fingerprint."
    ),
    "POST /api/v1/agent-push/requests": (
        "Probes or installs the agent on explicitly named, unenrolled addresses of the "
        "credential's client, through that client's site server. An install presents the "
        "credential to each target. Refused whole if any target fails any bound."
    ),
    "GET /api/v1/agent-push/runs": (
        "Lists push install runs with their client, state and progress."
    ),
    "POST /api/v1/agent-push/runs": (
        "Starts a push run over the named machines of an authorized subnet of the "
        "credential's client, confirmed by the preview's digest. Up to 50 in flight until "
        "paused or cancelled; one active run per client. Refused if the list changed."
    ),
    "GET /api/v1/agent-push/runs/prefixes": (
        "Lists the authorized subnets a push run may be built from -- with credential_id, "
        "that credential's client's -- with address counts and expiry. Read-only."
    ),
    "GET /api/v1/agent-push/runs/preview": (
        "Expands an authorized subnet into the named machines a push run would cover, with "
        "per-address readiness, blocking reasons and the list digest to confirm. Read-only."
    ),
    "GET /api/v1/agent-push/runs/{run_id}": (
        "Returns one push install run with its state and counts of waiting, in-flight, "
        "installed, failed, refused and cancelled targets."
    ),
    "POST /api/v1/agent-push/runs/{run_id}/cancel": (
        "Cancels a push run for good: waiting targets are cancelled, installs already in "
        "flight finish. Cannot be resumed."
    ),
    "POST /api/v1/agent-push/runs/{run_id}/pause": (
        "Pauses a push run so no new installs are dispatched. Installs already in flight "
        "finish."
    ),
    "POST /api/v1/agent-push/runs/{run_id}/resume": (
        "Resumes a paused push run, or one stopped after repeated authentication failures, "
        "and dispatches installs again from one target. Each target is re-checked."
    ),
    "GET /api/v1/agent-push/runs/{run_id}/targets": (
        "Lists the per-address targets of a push run, a page at a time, with state and "
        "reason."
    ),
    # --- backup ---
    "GET /api/v1/backup/devices/{device_id}/backup": (
        "Returns one device's backup status: last good recovery point, last run state and "
        "time, last verification and restore-test times, and its repository. Read-only."
    ),
    "POST /api/v1/backup/devices/{device_id}/run": (
        "Dispatches a signed backup job to the device now, using its resolved backup policy "
        "and repository. Returns the job ids."
    ),
    "GET /api/v1/backup/offsite-targets": (
        "Lists the off-site (S3-style) backup copy targets with test state and last error. "
        "Credentials are never returned."
    ),
    "POST /api/v1/backup/offsite-targets": (
        "Creates an off-site backup target (bucket, region, lock mode, retention) and "
        "stores its access key in the vault. Refused if the name exists."
    ),
    "PUT /api/v1/backup/offsite-targets/{target_id}": (
        "Replaces the settings of an off-site target. Give both access key and secret to "
        "rotate the stored credential, or neither to keep it."
    ),
    "POST /api/v1/backup/offsite-targets/{target_id}/test": (
        "Requests a connectivity test of an off-site target, run later by a worker. Marks "
        "the target testing and returns at once; read it again for the outcome."
    ),
    "GET /api/v1/backup/points": (
        "Lists this organization's backup recovery points, newest first, with device, "
        "size and time taken."
    ),
    "GET /api/v1/backup/points/{point_id}/copies": (
        "Lists the off-site copies of one recovery point with state, bytes, lock expiry "
        "and last error."
    ),
    "GET /api/v1/backup/points/{point_id}/manifest": (
        "Reads and decrypts the file manifest of one recovery point so its contents can be "
        "browsed before restoring. Read-only."
    ),
    "POST /api/v1/backup/points/{point_id}/restore": (
        "Dispatches a signed restore job to the device that took the point, writing the "
        "selected files into the given target. Overwrites data on that endpoint."
    ),
    "POST /api/v1/backup/points/{point_id}/verify": (
        "Dispatches a signed verify job that restores the whole point into a scratch "
        "location on its device and checksums it. Does not touch any real destination."
    ),
    "GET /api/v1/backup/policies": (
        "Lists the backup policies the caller's clients can see (client, device group, "
        "sources, window, retention, repository): each client's default and each group's."
    ),
    "PUT /api/v1/backup/policies": (
        "Creates or replaces one device group's backup policy, or a client's default "
        "(client_id, no group). The repository must be that client's and not retired, except "
        "to switch a stored policy off naming the repository it already names. Governs "
        "scheduled backups on its devices. Audited."
    ),
    "GET /api/v1/backup/recovery-keys": (
        "Lists the backup recovery keys' public keys, fingerprints and proof status. "
        "No private material."
    ),
    "GET /api/v1/backup/report": (
        "Returns a fleet backup report: device coverage, run counts by state, success rate, "
        "points needing verification and deduplication savings. Read-only."
    ),
    "GET /api/v1/backup/repositories": (
        "Lists the backup repositories the caller's clients can see (client, name, state, "
        "dedup parameters, off-site target link). Keys are never returned."
    ),
    "POST /api/v1/backup/repositories": (
        "Creates an encrypted backup repository for one client (client_id) and generates "
        "its master key into the vault. Only that client's devices use it. The key is not "
        "returned."
    ),
    "PUT /api/v1/backup/repositories/{repository_id}/offsite": (
        "Links a repository to an off-site target, global or of the repository's client, so "
        "its recovery points get copied there, or unlinks it with a null target. Audited."
    ),
    "POST /api/v1/backup/repositories/{repository_id}/retire": (
        "Retires one backup repository: it takes no new backups and no policy may name it "
        "again, while its recovery points stay and can still be restored. Refused with 409 "
        "while an enabled policy uses it. There is no way back. Audited."
    ),
    # --- deploy ---
    "GET /api/v1/deploy/deployments": (
        "Lists the software deployments the caller's clients can see (package, action, "
        "target, client, state), newest first."
    ),
    "POST /api/v1/deploy/deployments": (
        "Creates a pending software deployment of a package to a device or group; it belongs "
        "to the target's client. Without a window it sends nothing until executed; with one it "
        "is dispatched when the window opens. A change gate on the target needs an approved "
        "change of that client."
    ),
    "GET /api/v1/deploy/deployments/{deployment_id}": (
        "Returns one deployment with its state and per-device results. Read-only."
    ),
    "POST /api/v1/deploy/deployments/{deployment_id}/execute": (
        "Dispatches signed install, update or uninstall jobs to every device the deployment "
        "targets. Changes software on those endpoints. Returns the job ids. A deployment that "
        "does not exist or is not visible is a 404."
    ),
    "GET /api/v1/deploy/devices/{device_id}/deployments": (
        "Lists the deployment results for one device: package, action, state, exit code "
        "and log reference, newest first."
    ),
    "GET /api/v1/deploy/packages": (
        "Lists the software packages available for deployment, with OS family, installer "
        "type and detection rule."
    ),
    "POST /api/v1/deploy/packages": (
        "Registers a software package definition (installer type, identifier, detection "
        "and uninstall rules, install arguments). Installs nothing until deployed."
    ),
    "DELETE /api/v1/deploy/packages/{package_id}": (
        "Deletes a package definition so it can no longer be deployed. Does not uninstall "
        "it from devices. Audited."
    ),
    "GET /api/v1/deploy/report": (
        "Returns a fleet deployment report: deployments with device counts and success "
        "rates, and recent script runs with their states. Read-only."
    ),
    "GET /api/v1/deploy/script-policies": (
        "Lists the scheduled scripts: which script runs on which device group, how often "
        "and its last run."
    ),
    "POST /api/v1/deploy/script-policies": (
        "Schedules a script to run repeatedly on every device of a group, optionally "
        "inside maintenance windows. Signed jobs are dispatched automatically from then on."
    ),
    "DELETE /api/v1/deploy/script-policies/{policy_id}": (
        "Deletes a scheduled script so it stops running on its group. Audited."
    ),
    "PATCH /api/v1/deploy/script-policies/{policy_id}": (
        "Changes a scheduled script's interval, window, alerting, retention or enabled "
        "flag. Takes effect on the next scheduled dispatch to the group's devices."
    ),
    "GET /api/v1/deploy/script-policies/{policy_id}/runs": (
        "Lists the latest runs of a scheduled script, newest first, with per-device "
        "output."
    ),
    "GET /api/v1/deploy/scripts": (
        "Lists the stored scripts (name, OS family, interpreter, version) without their "
        "bodies."
    ),
    "POST /api/v1/deploy/scripts": (
        "Stores a new script (name, OS family, interpreter, body). Runs nothing until "
        "dispatched or scheduled. Audited."
    ),
    "GET /api/v1/deploy/scripts/{script_id}": (
        "Returns one stored script including its body and version."
    ),
    "PUT /api/v1/deploy/scripts/{script_id}": (
        "Replaces a script's body and bumps its version. Later runs and schedules of this "
        "script execute the new body on endpoints."
    ),
    "POST /api/v1/deploy/scripts/{script_id}/run": (
        "Dispatches a signed job that runs the script on a device or on every device of a "
        "group, executing code on those endpoints with system (SYSTEM or root) privileges. "
        "Returns the job ids."
    ),
    # --- health ---
    "GET /api/v1/health/alerts": (
        "Lists health alerts, newest first, filterable by state, severity and device. An "
        "unknown filter value returns an empty list."
    ),
    "GET /api/v1/health/alerts/summary": (
        "Returns totals of open (active, acknowledged or muted) health alerts by state and "
        "severity, plus the worst severity. Read-only."
    ),
    "GET /api/v1/health/alerts/{alert_id}": (
        "Returns one health alert with its remediation action rows, linked ticket, "
        "previous occurrence and monitoring deep link."
    ),
    "POST /api/v1/health/alerts/{alert_id}/acknowledge": (
        "Marks an active alert acknowledged by the caller. It stays open and is not "
        "cleared; only re-notification and escalation stop."
    ),
    "POST /api/v1/health/alerts/{alert_id}/actions/{action_id}/approve": (
        "Approves a remediation awaiting approval and dispatches its signed job to the "
        "alert's device, changing that endpoint. Needs the job type's own permission too."
    ),
    "POST /api/v1/health/alerts/{alert_id}/actions/{action_id}/refuse": (
        "Refuses a remediation awaiting approval with a reason. The action is skipped "
        "permanently and cannot be retried for this alert."
    ),
    "POST /api/v1/health/alerts/{alert_id}/close": (
        "Manually closes an alert, recorded as a human close and not as proof the problem "
        "was fixed."
    ),
    "POST /api/v1/health/alerts/{alert_id}/mute": (
        "Mutes an open alert until a given time, at most 30 days after it opened. It stays "
        "open and counted as open."
    ),
    "POST /api/v1/health/alerts/{alert_id}/remediate": (
        "Dispatches an approved action as a signed job to the alert's device, changing "
        "that endpoint. Rate fuses and the job type's own permission still apply."
    ),
    "GET /api/v1/health/assignments": (
        "Lists which health profile applies to the organization, each client, device groups "
        "or single devices, optionally for one profile."
    ),
    "POST /api/v1/health/assignments": (
        "Assigns a health profile to a device, a group, a client (that client's default, which "
        "needs health.admin in that client) or the whole organization, then pushes the changed "
        "policy as signed jobs to the affected agents. One assignment per level."
    ),
    "DELETE /api/v1/health/assignments/{assignment_id}": (
        "Removes a profile assignment. Affected devices fall back to the next level or to "
        "no profile, and agents are sent signed policy updates, possibly disarming checks."
    ),
    "GET /api/v1/health/check-types": (
        "Lists the closed registry of health check types with value kind, cadence, "
        "supported OS families and legal comparators. Read-only."
    ),
    "GET /api/v1/health/profiles": (
        "Lists the health profiles with their intervals, check counts and assignment "
        "counts."
    ),
    "POST /api/v1/health/profiles": (
        "Creates an empty health profile. It has no checks and no assignment, so no device "
        "is affected until both are added."
    ),
    "DELETE /api/v1/health/profiles/{profile_id}": (
        "Deletes a health profile. Refused with 409 while an assignment or alert still "
        "references it; deactivate it instead."
    ),
    "GET /api/v1/health/profiles/{profile_id}": (
        "Returns one health profile with its settings, policy version and counts."
    ),
    "PATCH /api/v1/health/profiles/{profile_id}": (
        "Updates a health profile and bumps its policy version, then re-pushes the policy "
        "to assigned devices as signed jobs."
    ),
    "GET /api/v1/health/profiles/{profile_id}/checks": (
        "Lists the checks of a health profile with comparators, thresholds and policy "
        "version."
    ),
    "PUT /api/v1/health/profiles/{profile_id}/checks": (
        "Replaces a profile's whole check set. Dropped checks are retired and their open "
        "alerts cleared. Assigned devices get the new policy as signed jobs."
    ),
    "POST /api/v1/health/profiles/{profile_id}/clone": (
        "Copies a health profile and its checks under a new name, without its assignments. "
        "No device is affected."
    ),
    "GET /api/v1/health/settings": (
        "Returns the effective health settings (ticketing defaults, deep-link template and "
        "similar) and whether any were configured. Writes nothing."
    ),
    "PUT /api/v1/health/settings": (
        "Updates the organization's health settings, such as the queue and category used "
        "for alert tickets. Audited."
    ),
    # --- patch ---
    "GET /api/v1/patch/app-sources": (
        "Reports whether application update sources (winget, Homebrew) could be scanned, "
        "counted over each device's latest completed scan, optionally per OS family."
    ),
    "POST /api/v1/patch/apply": (
        "Dispatches signed patch-apply jobs installing approved patches on devices (all "
        "devices when none are given) that are inside their window. Changes endpoints."
    ),
    "GET /api/v1/patch/approvals": (
        "Lists patch approvals (patch and ring), filterable by patch, ring and revoked "
        "state, a page at a time."
    ),
    "POST /api/v1/patch/approvals": (
        "Approves one patch for a ring: with client_id, for that client's devices in it; "
        "without, for every client's. Signed install jobs then go to them inside their "
        "maintenance windows. Where a change gate covers them it needs an approved change: "
        "that client's, or the internal client's for every client."
    ),
    "POST /api/v1/patch/approvals/bulk": (
        "Approves many patches for one ring in a single decision, all or nothing: with "
        "client_id, for that client's devices in it; without, for every client's. Install "
        "jobs then go to them inside their windows. A change gate needs an approved change: "
        "that client's, or the internal client's for every client."
    ),
    "DELETE /api/v1/patch/approvals/{approval_id}": (
        "Revokes a patch approval so that patch is no longer eligible for that ring. "
        "Installs already done are not undone."
    ),
    "GET /api/v1/patch/device-rollup": (
        "Lists devices needing patch attention, one row per device, worst first, with "
        "search and OS filter and a page at a time."
    ),
    "POST /api/v1/patch/devices/{device_id}/restart": (
        "Books a restart of the device for its next maintenance window. Sends nothing now; "
        "the restart sweep later dispatches a signed reboot. Repeating changes nothing."
    ),
    "GET /api/v1/patch/devices/{device_id}/status": (
        "Returns one device's patch status (installed, missing, failed) with its restart "
        "window. Read-only."
    ),
    "GET /api/v1/patch/patches": (
        "Lists the patch catalogue, filterable by OS family, severity, per-device state "
        "and approved ring, a page at a time."
    ),
    "GET /api/v1/patch/policies": (
        "Lists the patch policies (ring, maintenance window, reboot rules, scan interval) "
        "for the organization default and device groups."
    ),
    "PUT /api/v1/patch/policies": (
        "Creates or replaces the patch policy for the organization default or one device "
        "group, setting its ring, window, auto-approval and reboot behaviour. Audited."
    ),
    "GET /api/v1/patch/report": (
        "Returns a fleet patch compliance report: compliance percentage by OS family and "
        "severity, the list of exposures and exposed device counts. Read-only."
    ),
    "GET /api/v1/patch/restarts": (
        "Lists devices still owed a restart after patching, oldest first."
    ),
    "GET /api/v1/patch/rings": (
        "Lists the deployment rings with how many devices each reaches. Read-only; check "
        "it before approving patches."
    ),
    "GET /api/v1/patch/runs": (
        "Lists the most recent patch apply runs and what they did, optionally for one "
        "device."
    ),
    "POST /api/v1/patch/scan": (
        "Dispatches signed patch-scan jobs to the named devices, or to every approved "
        "device when none are given. Scans only; installs nothing. Returns the job ids."
    ),
    "GET /api/v1/patch/updates": (
        "Lists the fleet update list, one row per patch aggregated across devices, "
        "filterable and sortable, with severity counts and scan coverage."
    ),
    # --- site-servers ---
    "GET /api/v1/site-servers": (
        "Lists site servers, devices declared to serve network segments, filterable by "
        "enabled state, site and a segment they contain."
    ),
    "POST /api/v1/site-servers": (
        "Promotes an enrolled device to site server for given network segments and roles, "
        "optionally filed under one of its client's sites. It then originates wake and "
        "push-install jobs there."
    ),
    "DELETE /api/v1/site-servers/{site_server_id}": (
        "Demotes a site server so it is no longer used as an originator for its segments. "
        "Pushes and wakes there are refused afterwards."
    ),
    "GET /api/v1/site-servers/{site_server_id}": (
        "Returns one site server with its device, segments, roles and state."
    ),
    "PATCH /api/v1/site-servers/{site_server_id}": (
        "Changes a site server's segments, roles, name, notes, site or enabled flag. "
        "Changing segments changes where it originates wake and push jobs."
    ),
    "GET /api/v1/site-servers/{site_server_id}/segment-check": (
        "Compares a site server's declared segments with those observed by IPAM, listing "
        "matched, declared-only and observed-only. Says unavailable without IPAM."
    ),
    "POST /api/v1/site-servers/{site_server_id}/wake": (
        "Dispatches a signed job to the site server to broadcast a Wake-on-LAN packet for "
        "a target MAC onto its LAN segment. Returns the job id and broadcast address."
    ),
}
