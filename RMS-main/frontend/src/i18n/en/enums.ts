/**
 * Labels for wire enums, keyed `enums.<TypeName>.<value>` where TypeName is the
 * union type in lib/api.ts. The English is exactly what `humaniseEnum` derives
 * from the value, so seeding it changes nothing on screen; the entry exists so
 * the Spanish one has something to be checked against (enums.coverage.test.ts).
 */
import { auditActions, auditTargetTypes } from "./auditVocabulary";

export const enums = {
  RemoteSessionState: {
    awaiting_consent: "Awaiting consent",
    connecting: "Connecting",
    active: "Active",
    ended: "Ended",
  },
  /**
   * The shared status vocabulary (`StatusPill`, `Attributes`): wire words that
   * are not one module's enum. Looked up by `humaniseStatus`; a word that is
   * not here falls back to the derivation. The English is that derivation, so
   * seeding it changes nothing on screen (`labels.status.test.ts` holds it).
   */
  StatusWord: {
    critical: "Critical",
    failed: "Failed",
    failure: "Failure",
    error: "Error",
    errored: "Errored",
    high: "High",
    urgent: "Urgent",
    overdue: "Overdue",
    offline: "Offline",
    expired: "Expired",
    revoked: "Revoked",
    breached: "Breached",
    breach: "Breach",
    dead: "Dead",
    down: "Down",
    denied: "Denied",
    refused: "Refused",
    rejected: "Rejected",
    blocked: "Blocked",
    unhealthy: "Unhealthy",
    missing: "Missing",
    stalled: "Stalled",
    escalated: "Escalated",
    warning: "Warning",
    pending: "Pending",
    medium: "Medium",
    due: "Due",
    expiring: "Expiring",
    degraded: "Degraded",
    at_risk: "At risk",
    stale: "Stale",
    behind: "Behind",
    partial: "Partial",
    paused: "Paused",
    suspended: "Suspended",
    waiting: "Waiting",
    needs_attention: "Needs attention",
    throttled: "Throttled",
    moderate: "Moderate",
    important: "Important",
    info: "Info",
    action_required: "Action required",
    success: "Success",
    succeeded: "Succeeded",
    successful: "Successful",
    healthy: "Healthy",
    online: "Online",
    completed: "Completed",
    complete: "Complete",
    done: "Done",
    low: "Low",
    ok: "OK",
    active: "Active",
    approved: "Approved",
    resolved: "Resolved",
    closed: "Closed",
    installed: "Installed",
    current: "Current",
    verified: "Verified",
    passed: "Passed",
    pass: "Pass",
    enrolled: "Enrolled",
    valid: "Valid",
    met: "Met",
    applied: "Applied",
    ready: "Ready",
    up: "Up",
    connected: "Connected",
    enabled: "Enabled",
    delivered: "Delivered",
    sent: "Sent",
    accepted: "Accepted",
    granted: "Granted",
    compliant: "Compliant",
    running: "Running",
    in_progress: "In progress",
    open: "Open",
    scheduled: "Scheduled",
    renewing: "Renewing",
    queued: "Queued",
    dispatched: "Dispatched",
    started: "Started",
    syncing: "Syncing",
    polling: "Polling",
    deploying: "Deploying",
    applying: "Applying",
    scanning: "Scanning",
    processing: "Processing",
    retrying: "Retrying",
    patching: "Patching",
    provisioning: "Provisioning",
    executing: "Executing",
    assigned: "Assigned",
    triaged: "Triaged",
    new: "New",
    proposed: "Proposed",
    informational: "Informational",
    note: "Note",
    neutral: "Neutral",
    unknown: "Unknown",
    none: "None",
    not_applicable: "Not applicable",
    cancelled: "Cancelled",
    canceled: "Canceled",
    archived: "Archived",
    disabled: "Disabled",
    inactive: "Inactive",
    skipped: "Skipped",
    withdrawn: "Withdrawn",
    retired: "Retired",
    muted: "Muted",
    never: "Never",
    draft: "Draft",
    unassigned: "Unassigned",
    not_enrolled: "Not enrolled",
    no_agent: "No agent",
  },
  /**
   * Platform-status names (modules/platform-status). `ReasonCode` is the probe
   * reason union in that module's types.ts. `PlatformProbe`, `PlatformModule`
   * and `PlatformProbeDetail` are the names the backend registers today; a name
   * it adds later is not here and reads as its derived English, which is the
   * `humaniseEnum` fallback. A dotted probe name (`backup.nightly`) is keyed
   * with `_` because a `.` is the catalogue's path separator; the derivation is
   * the same either way. `keycloak_events` is left out on purpose: its English
   * would name the identity vendor, which the no-vendor-names guard forbids in
   * any catalogue string.
   */
  ReasonCode: {
    unreachable: "Unreachable",
    timeout: "Timeout",
    auth_failed: "Auth failed",
    sealed: "Sealed",
    empty_cache: "Empty cache",
    misconfigured: "Misconfigured",
    stale: "Stale",
    failed: "Failed",
  },
  PlatformProbe: {
    postgres_app: "Postgres app",
    postgres_admin: "Postgres admin",
    database: "Database",
    redis: "Redis",
    cache: "Cache",
    vault: "Vault",
    objectstore: "Objectstore",
    object_store: "Object store",
    identity: "Identity",
    jwks: "Jwks",
    signing: "Signing",
    ca: "Ca",
    backup_nightly: "Backup nightly",
    backup_offsite: "Backup offsite",
    backup_drill: "Backup drill",
  },
  PlatformProbeDetail: {
    sealed: "Vault sealed",
    bucket: "Bucket",
    keys: "Keys",
    configured: "Configured",
    ever_run: "Ever run",
    age_s: "Age s",
    written: "Written",
    hours_since_success: "Hours since success",
    days_since_drill: "Days since drill",
  },
  PlatformModule: {
    agent_distribution: "Agent distribution",
    agent_push: "Agent push",
    agent_update: "Agent update",
    ai_admin: "AI admin",
    ai_copilot: "AI copilot",
    ai_portal: "AI portal",
    backup: "Backup",
    bcp: "BCP",
    budget: "Budget",
    change_mgmt: "Change mgmt",
    deploy: "Deploy",
    documentation: "Documentation",
    email_intake: "Email intake",
    health: "Health",
    inventory: "Inventory",
    ipam: "IPAM",
    licenses: "Licenses",
    notification: "Notification",
    patch: "Patch",
    people: "People",
    portal: "Portal",
    provisioning: "Provisioning",
    remote: "Remote",
    reporting: "Reporting",
    security_logs: "Security logs",
    service_desk: "Service desk",
    site_server: "Site server",
    sla: "SLA",
    subscriptions: "Subscriptions",
    user_admin: "User admin",
  },
  /** Error-monitor capture seams (`ErrorWhere` in modules/error-monitor/types.ts). */
  ErrorWhere: {
    api: "API",
    worker: "Worker",
    gateway: "Gateway",
  },
  /**
   * Audit-log names (modules/audit). `AuditAction` is the action codes the page
   * words itself; a dotted code is keyed with `_` because `.` separates
   * catalogue paths. `AuditActorType` is the closed set the log records;
   * `AuditTargetType` is the target kinds the backend records. Both are the
   * full vocabulary the backend can write (`auditVocabulary.ts`, held complete
   * by `auditVocabulary.coverage.test.ts`), plus the actions the console words
   * by hand below. A code or kind not listed would read as the derived English,
   * which is `humaniseEnum`'s fallback, and the raw code always stays beside
   * it. Nothing stored in an entry is rewritten (spec §3.6): only the labels
   * around it.
   */
  AuditAction: {
    ...auditActions,
    // Worded by hand before the vocabulary existed; kept as they were.
    job_dispatch: "Job sent",
    job_retry: "Job retried",
    job_cancel: "Job cancelled",
    credential_checkout: "Credential checked out",
    credential_created: "Credential created",
    credential_use: "Credential used",
    remote_session_start: "Remote session started",
    remote_session_requested: "Remote session requested",
    remote_session_credential_used: "Credential used for a remote session",
    auth_signed_in: "Signed in",
    policy_change: "Policy changed",
    patch_approved: "Update approved",
    ai_action_proposed: "Assistant proposed an action",
  },
  AuditActorType: {
    user: "User",
    agent: "Agent",
    system: "System",
  },
  AuditTargetType: {
    ...auditTargetTypes,
    device: "Device",
    job: "Job",
    credential: "Credential",
    user: "User",
    organization: "Organization",
    ticket: "Ticket",
    change: "Change",
    push_target: "Push target",
    email_mailbox: "Email mailbox",
    session: "Session",
    person: "Person",
    agent_release: "Agent release",
    health_alert: "Health alert",
    expense: "Expense",
    org: "Org",
    role: "Role",
    license: "License",
    group: "Group",
    approved_action: "Approved action",
    ai_action: "AI action",
    subscription: "Subscription",
    sla_calendar: "SLA calendar",
    push_run: "Push run",
    notification_channel: "Notification channel",
    budget: "Budget",
    bcp_test: "BCP test",
    backup_recovery_key: "Backup recovery key",
    backup_offsite_target: "Backup offsite target",
    sla_policy: "SLA policy",
    site_server: "Site server",
    seat_assignment: "Seat assignment",
    script: "Script",
    provision_run: "Provision run",
    product: "Product",
    patch: "Patch",
    package: "Package",
    notification_rule: "Notification rule",
    ipam_address: "IPAM address",
    install_link: "Install link",
    deployment: "Deployment",
    bcp_plan: "BCP plan",
    backup_point: "Backup point",
    ai_session: "AI session",
  },
  /**
   * The global roles migrations 0003 and 0028 insert, by the name they are
   * STORED under (`roles.name`, org_id NULL), shown through `roleDisplayName`
   * (modules/settings/types.ts). The English is that function's derivation for
   * these names, so the English page did not change. A role an operator makes
   * is not here and keeps its own words in either language; an org's copy of a
   * seeded role (`rbac_admin` clones keep the name) reads like the original.
   */
  SeededRole: {
    "super-admin": "Super admin",
    "org-admin": "Org admin",
    technician: "Technician",
    auditor: "Auditor",
    "read-only": "Read only",
    requester: "Requester",
  },
  /** Credential types (modules/credentials/credentialTypes.ts). */
  CredentialType: {
    windows_admin: "Windows administrator",
    ssh_key: "SSH key",
    domain_join: "Domain join account",
    push_install: "Push install",
    windows_domain: "Windows domain account",
  },
  /** Job statuses (`JobStatus` in lib/api.ts), as the status filter words them. */
  JobStatus: {
    queued: "Queued",
    dispatched: "Dispatched",
    running: "Running",
    succeeded: "Succeeded",
    failed: "Failed",
    expired: "Expired",
    refused: "Refused",
    cancelled: "Cancelled",
  },
  /**
   * Job types as the queue names them (modules/jobs/jobTypes.ts). The wire
   * type has a `.` and a `.` separates catalogue paths, so `patch.apply` is
   * keyed `patch_apply`. A type this table does not know reads as its wire
   * name, which `jobTypeLabel` keeps as the fallback.
   */
  JobType: {
    inventory_collect: "Collect inventory",
    logs_ship: "Ship agent logs",
    patch_scan: "Scan for patches",
    patch_apply: "Apply patches",
    patch_reboot: "Restart device",
    app_deploy: "Deploy application",
    script_run: "Run script",
    backup_run: "Run backup",
    backup_restore: "Restore from backup",
    provision_enroll: "Provision device",
    remote_session_open: "Open remote session",
    remote_screen_open: "Share screen",
    agent_update: "Update agent",
    agent_uninstall: "Uninstall agent",
    agent_push_install: "Install agent over the network",
    agent_push_probe: "Check a machine before installing",
    net_sweep: "Network sweep",
    site_wake: "Wake device",
    health_check_now: "Health check",
    health_policy_apply: "Apply health policy",
    agent_cert_renew: "Renew agent certificate",
    agent_cert_install: "Install agent certificate",
    job_cancel: "Stop a job on the device",
    hello: "Developer test job",
  },
  /** Enrollment token states (modules/enrollment/types.ts), as the token table's pills word them. */
  EnrollmentTokenState: {
    active: "Active",
    revoked: "Revoked",
    expired: "Expired",
    spent: "Fully used",
  },
  /** Agent certificate states `GET /devices` serves (modules/enrollment/types.ts). */
  CertificateState: {
    ok: "Valid",
    renewing: "Renewing",
    expiring: "Expiring",
    expired: "Expired",
  },
  /**
   * Device record vocabularies (`Device` in lib/api.ts), worded as the Devices
   * screens word them. `DeviceAssetType` and `DeviceSource` are the wire words
   * themselves, in lower case, exactly as the type and source selects always
   * showed them; the other three are written out.
   */
  DeviceProvenance: {
    agent: "Agent enrolment",
    operator: "Entered by hand",
    promoted: "Confirmed from discovery",
    sweep: "Found by a sweep",
  },
  DeviceLifecycle: {
    in_service: "In service",
    stock: "In stock (not deployed)",
    unmanageable: "Cannot run an agent (switch, printer, …)",
    retired: "Retired",
  },
  DeviceAssetType: {
    computer: "computer",
    server: "server",
    network: "network",
    printer: "printer",
    phone: "phone",
    appliance: "appliance",
    other: "other",
  },
  DeviceSource: {
    agent: "agent",
    manual: "manual",
    discovery: "discovery",
  },
  DeviceReviewState: {
    approved: "Approved",
    pending: "Waiting for review",
    rejected: "Rejected",
  },
  UpdateRolloutState: {
    draft: "Draft",
    running: "Running",
    paused: "Paused",
    completed: "Completed",
    cancelled: "Cancelled",
  },
  UpdateRingState: {
    pending: "Pending",
    active: "Active",
    done: "Done",
  },
  UpdateTargetState: {
    pending: "Pending",
    dispatched: "Dispatched",
    verifying: "Verifying",
    succeeded: "Succeeded",
    failed: "Failed",
    rolled_back: "Rolled back",
    refused: "Refused",
    lost: "Lost",
    skipped: "Skipped",
    needs_manual_update: "Needs manual update",
  },
  /** modules/patch: a patch policy's reboot behaviour. */
  PatchReboot: {
    never: "Never restart",
    if_required: "Restart when updates need it",
    forced: "Always restart after updates",
  },
  /** modules/patch: the platforms the patch page is split into. */
  PatchFamily: {
    windows: "Windows",
    linux: "Linux",
    macos: "macOS",
  },
  /** modules/inventory: what a change-history row records. */
  InventoryChangeType: {
    os_updated: "OS updated",
    software_installed: "Software installed",
    software_added: "Software added",
    software_updated: "Software updated",
    software_removed: "Software removed",
    fact_changed: "Fact changed",
    hardware_changed: "Hardware changed",
  },
  /** modules/inventory: what a hardware row is. */
  HardwareComponentType: {
    cpu: "Processor",
    memory: "Memory",
    disk: "Storage",
    nic: "Network interface",
    gpu: "Graphics",
    display: "Display",
    battery: "Battery",
    bios: "Bios",
    motherboard: "Motherboard",
  },
  /** modules/deploy */
  DeploymentAction: {
    install: "Install",
    update: "Update",
    uninstall: "Uninstall",
  },
  DeploymentState: {
    pending: "Pending",
    dispatching: "Dispatching",
    complete: "Complete",
    failed: "Failed",
  },
  DeploymentResultState: {
    pending: "Pending",
    running: "Running",
    installed: "Installed",
    failed: "Failed",
    not_applicable: "Not applicable",
  },
  ScriptRunState: {
    pending: "Pending",
    running: "Running",
    succeeded: "Succeeded",
    failed: "Failed",
  },
  Interpreter: {
    powershell: "PowerShell",
    pwsh: "PowerShell 7",
    bash: "Bash",
    sh: "Shell",
    zsh: "Zsh",
    cmd: "Command Prompt",
    python: "Python",
  },
  /** modules/health */
  RiskClass: {
    low: "Low",
    medium: "Medium",
    high: "High",
  },
  /** Health check types (modules/health), keyed with `_` for `.` like JobType: `cpu.busy_pct` is `cpu_busy_pct`. */
  HealthCheckType: {
    cpu_busy_pct: "CPU busy",
    memory_used_pct: "Memory used",
    disk_free_pct: "Disk free",
    disk_free_bytes: "Disk free space",
    disk_smart_ok: "Disk health (SMART)",
    process_present: "Program running",
    service_state: "Service state",
    posture_disk_encryption: "Disk encryption on",
    posture_firewall_enabled: "Firewall on",
    eventlog_disk_error: "Disk errors in the event log",
    eventlog_service_crash: "Services crashing",
    eventlog_kernel_fault: "System crashes",
    script_policy_failed: "Scheduled script failed",
    agent_offline: "No contact from the agent",
    backup_success_age_h: "Hours since the last good backup",
    backup_last_run_failed: "Last backup failed or was missed",
    backup_offsite_lag_h: "Hours the off-site copy is behind",
    backup_restore_test_age_d: "Days since a test restore",
    posture_antivirus_healthy: "Antivirus health",
    service_running: "Service running",
    cert_days_left: "Certificate expiry",
  },
  /** Subnet kinds (modules/ipam). */
  SubnetKind: {
    container: "Container",
    assignable: "Assignable",
  },
  /** What started a network sweep (modules/ipam). */
  SweepTrigger: {
    scheduled: "Scheduled",
    manual: "Manual",
  },
  /** A provisioning run's state (modules/provisioning). */
  ProvisionRunState: {
    pending: "Pending",
    awaiting_enrollment: "Awaiting enrollment",
    enrolled: "Enrolled",
    complete: "Complete",
    failed: "Failed",
  },
  /** What kind of build an OS image is (modules/provisioning). */
  OsImageKind: {
    install: "Install",
    answer_file: "Answer file",
    image: "Image",
    build: "Build",
    enrollment: "Enrollment",
  },
  /** An OS image's state (modules/provisioning). */
  OsImageState: {
    active: "Active",
    draft: "Draft",
  },
  /** A backup point's state (modules/backup). */
  BackupPointState: {
    complete: "Complete",
    failed: "Failed",
    pruned: "Pruned",
  },
  /** A backup run's state (modules/backup). */
  BackupRunState: {
    pending: "Pending",
    running: "Running",
    success: "Success",
    failed: "Failed",
    missed: "Missed",
  },
  /** A backup repository's state (modules/backup). */
  RepositoryState: {
    active: "Active",
    disabled: "Disabled",
  },
  /** What kind of continuity test a plan schedules (modules/bcp). */
  TestType: {
    restore_drill: "Restore drill",
    failover: "Failover",
    tabletop: "Tabletop",
    other: "Other",
  },
  /** A continuity test's outcome (modules/bcp). */
  BcpOutcome: {
    pending: "Pending",
    pass: "Pass",
    partial: "Partial",
    fail: "Fail",
  },
  /** A continuity test step's result (modules/bcp). */
  StepResult: {
    pending: "Pending",
    pass: "Pass",
    fail: "Fail",
  },
  /** How serious a continuity test finding is (modules/bcp). */
  FindingSeverity: {
    low: "Low",
    medium: "Medium",
    high: "High",
    critical: "Critical",
  },
  /** Where a continuity test finding stands (modules/bcp). */
  RemediationState: {
    open: "Open",
    in_progress: "In progress",
    resolved: "Resolved",
  },
  /** A continuity plan's lifecycle state (modules/bcp). */
  PlanState: {
    active: "Active",
    archived: "Archived",
  },
  /** modules/service-desk (also shown by modules/portal): what a ticket is. */
  TicketType: {
    incident: "Incident",
    request: "Request",
  },
  /** modules/service-desk (also shown by modules/portal): a ticket's lifecycle status. */
  TicketStatus: {
    new: "New",
    open: "Open",
    pending: "Pending",
    resolved: "Resolved",
    closed: "Closed",
    cancelled: "Cancelled",
    pending_approval: "Pending approval",
    in_progress: "In progress",
    fulfilled: "Fulfilled",
    rejected: "Rejected",
  },
  /** modules/service-desk (also shown by modules/portal): how soon a ticket needs attention. */
  TicketPriority: {
    low: "Low",
    normal: "Normal",
    high: "High",
    urgent: "Urgent",
  },
  /** modules/service-desk: a final status named inside a sentence; the English is the wire word. */
  FinalTicketStatus: {
    cancelled: "cancelled",
    rejected: "rejected",
  },
  /** modules/service-desk: who wrote a comment when it has no author id. */
  CommentAuthorType: {
    user: "User",
    system: "System",
  },
  /** modules/service-desk: an account's status, in the user pickers. */
  UserStatus: {
    active: "Active",
    disabled: "Disabled",
    deleted: "Deleted",
  },
  /** modules/service-desk: why the copilot stopped; the English is the wire word itself. */
  CopilotStopReason: {
    answer: "answer",
    tool_budget: "tool_budget",
    byte_budget: "byte_budget",
    token_budget: "token_budget",
    deadline: "deadline",
    malformed: "malformed",
    nonce_leak: "nonce_leak",
    provider_error: "provider_error",
    provider_rate_limited: "provider_rate_limited",
    session_expired: "session_expired",
    internal_error: "internal_error",
  },
  /** modules/portal: the outcome of the approval a request needed. */
  PortalApprovalState: {
    pending: "Pending",
    approved: "Approved",
    rejected: "Rejected",
  },
  /** modules/sla: whether a business calendar can produce a deadline. */
  CalendarHealth: {
    ok: "OK",
    no_working_windows: "No working windows",
    invalid_timezone: "Invalid timezone",
  },
  /** modules/sla: the server's verdict on one response or resolution target. */
  TargetState: {
    none: "No SLA",
    void: "Void",
    breached: "Breached",
    met: "Met",
    paused: "Paused",
    running: "Running",
  },
  /** modules/email-intake: what became of an inbound message. */
  Disposition: {
    created: "Created a ticket",
    commented: "Added a comment",
    reopened: "Reopened a ticket",
    quarantined: "Quarantined: not trusted",
    refused_loop: "Refused: mail loop detected",
    refused_rate: "Refused: rate limit reached",
    refused_parse: "Refused: could not be parsed",
  },
  /**
   * modules/notification: the events a rule can route (`EVENT_TYPES`). The wire
   * type has `.` and a `.` separates catalogue paths, so `ticket.assigned` is
   * keyed `ticket_assigned`. An event this table does not know reads humanised.
   */
  EventType: {
    ticket_assigned: "Ticket assigned",
    ticket_status_changed: "Ticket status changed",
    ticket_comment_added: "Public reply added",
    ticket_internal_note_added: "Internal note added",
    ticket_approval_requested: "Approval requested",
    ticket_approval_decided: "Approval decided",
    ticket_resolved: "Ticket resolved",
    platform_probe_unhealthy: "Platform problem detected",
    platform_probe_recovered: "Platform problem resolved",
    subscription_notice_deadline_approaching: "Subscription notice deadline approaching",
    subscription_renewal_approaching: "Subscription renewal approaching",
    device_joined_via_link: "Computer waiting for review",
    device_removal_incomplete: "Rejected computer still has the agent",
    push_run_completed: "Push install run finished",
    push_run_stopped: "Push install run stopped after sign-in failures",
  },
  /** modules/notification: who a rule tells. */
  RecipientKind: {
    queue_members: "Everyone in the queue",
    assignee: "Whoever it's assigned to",
    requester: "The person who raised it",
    channel_default: "The channel's own default",
  },
  /** modules/notification: where a delivery stands. */
  DeliveryStatus: {
    pending: "Pending",
    sent: "Sent",
    failed: "Failed, retrying",
    dead: "Gave up",
  },
  /** modules/notification: what kind of channel. */
  ChannelKind: {
    webhook: "Webhook",
    email: "Email",
  },
  /**
   * modules/security: the event types the security log carries. An event type
   * this table does not know reads through `humaniseStatus`, never raw.
   */
  SecurityEventType: {
    logon_success: "Logon success",
    logon_failure: "Logon failure",
    logoff: "Logoff",
    account_lockout: "Account lockout",
    privilege_elevation: "Privilege elevation",
    admin_group_change: "Admin group change",
    policy_change: "Policy change",
  },
  /**
   * modules/settings: a user's status in the directory. The English is the wire word
   * as the directory has always printed it (lower case); a status not listed reads through
   * `humaniseStatus`.
   */
  DirectoryUserStatus: {
    active: "active",
    disabled: "disabled",
    deleted: "deleted",
  },
  /**
   * modules/docs: what an article or document is linked to. The English is the wire word as
   * the page has always printed it (lower case); a target the console has not met reads
   * through `humaniseStatus`.
   */
  DocLinkTarget: {
    device: "device",
    device_group: "device_group",
    credential: "credential",
    record: "record",
    ipam_subnet: "ipam_subnet",
    ipam_address: "ipam_address",
  },
  /** modules/docs: the kind of a documentation record. */
  DocRecordType: {
    network: "Network",
    service: "Service",
    vendor: "Vendor",
    location: "Location",
  },
  /** modules/reporting: the built-in reports, named from the report key the URL carries. */
  ReportKey: {
    "fleet-posture": "Fleet posture",
    "sla-attainment": "SLA attainment",
    "backup-coverage": "Backup coverage",
    "patch-compliance": "Patch compliance",
  },
  /** modules/reporting: the module a report source was read from (`meta.sources[].module`). */
  ReportSourceModule: {
    devices: "Devices",
    jobs: "Jobs",
    health: "Health",
    patch: "Patch",
    backup: "Backup",
    service_desk: "Service desk",
    sla: "SLA",
  },
  /** modules/reporting: cell values of the `scan_state` column (patch compliance). */
  ReportScanState: {
    fresh: "Fresh",
    stale: "Stale",
    never: "Never",
  },
  /** modules/reporting: cell values of the `agent_status` column (fleet posture). */
  ReportAgentStatus: {
    online: "Online",
    offline: "Offline",
    never_enrolled: "Never enrolled",
  },
  /** modules/reporting: cell values of the `last_point_state` column (backup coverage). */
  ReportPointState: {
    complete: "Complete",
    failed: "Failed",
    pruned: "Pruned",
  },
  /**
   * modules/reporting: cell values of the `consistency_level`, `offsite` and `coverage`
   * columns. The English is the wire word as the table has always printed it (lower case),
   * because those three columns were never humanised.
   */
  ReportConsistency: {
    snapshot: "snapshot",
    live: "live",
    application: "application",
    partial: "partial",
  },
  ReportOffsite: {
    off: "off",
    current: "current",
    behind: "behind",
  },
  ReportCoverage: {
    covered: "covered",
    stale: "stale",
    failing: "failing",
    unprotected: "unprotected",
  },
  /** modules/licenses: a license's type; "Oem" and "Cal" are what the humaniser has always derived. */
  LicenseType: {
    perpetual: "Perpetual",
    volume: "Volume",
    oem: "Oem",
    cal: "Cal",
    per_device: "Per device",
    per_user: "Per user",
  },
  /** modules/subscriptions: how often a subscription bills. */
  BillingCycle: {
    monthly: "Monthly",
    quarterly: "Quarterly",
    annual: "Annual",
  },
  /** modules/subscriptions: a subscription's status. */
  SubStatus: {
    active: "Active",
    trial: "Trial",
    cancelled: "Cancelled",
  },
  /** modules/subscriptions: the type of an entry in a subscription's event history. */
  SubscriptionEventType: {
    created: "Created",
    renewal: "Renewal",
    renewed: "Renewed",
    cancel: "Cancel",
    cancelled: "Cancelled",
    price_change: "Price change",
    seat_change: "Seat change",
    seat_assigned: "Seat assigned",
    seat_released: "Seat released",
    plan_change: "Plan change",
    updated: "Updated",
  },
  /** modules/budget: an expense's status. */
  ExpenseStatus: {
    expected: "Expected",
    confirmed: "Confirmed",
    void: "Void",
  },
  /** modules/budget: a budget's status. */
  BudgetStatus: {
    draft: "Draft",
    active: "Active",
    closed: "Closed",
  },
  /** modules/budget: where an expense came from. */
  ExpenseSource: {
    manual: "Manual",
    subscription: "Subscription",
    license: "License",
  },
  /** modules/budget: how a budget's periods are cut. */
  Granularity: {
    month: "Month",
    quarter: "Quarter",
  },
  /** modules/change: a change request's state. */
  ChangeState: {
    draft: "Draft",
    submitted: "Submitted",
    approved: "Approved",
    implementing: "Implementing",
    completed: "Completed",
    rejected: "Rejected",
    cancelled: "Cancelled",
  },
  /** modules/change: a change request's risk level. */
  ChangeRisk: {
    low: "Low",
    medium: "Medium",
    high: "High",
  },
  /** modules/change: the kind of change request. */
  ChangeType: {
    standard: "Standard",
    normal: "Normal",
    emergency: "Emergency",
  },
  /** modules/change: what an approver decided. */
  ChangeDecision: {
    approved: "Approved",
    rejected: "Rejected",
  },
  /** modules/change: what kind of platform action a change is linked to. */
  LinkActionType: {
    patch_run: "Patch run",
    deployment: "Deployment",
    provision_run: "Provision run",
    patch_approval: "Patch approval",
  },
  /** modules/ai-automation: where a proposed action stands. */
  AiActionState: {
    previewed: "Previewed",
    confirmed: "Confirmed",
    dispatched: "Dispatched",
    rejected: "Rejected",
    expired: "Expired",
  },
  /** modules/ai-admin: the detector's rule names (`app/core/ai/detect.py`). */
  AiInjectionRule: {
    redirection: "Overrides instructions",
    role_assertion: "Poses as the system",
    tool_coercion: "Demands a tool call",
    nonce_forgery: "Forges the data marker",
  },
  /** modules/ai-admin: the assistant's tools (a `.` in the name is `_` here). */
  AiTool: {
    devices_get: "Devices get",
    devices_health: "Devices health",
    health_alerts: "Health alerts",
    portal_my_tickets: "Portal my tickets",
    portal_my_ticket: "Portal my ticket",
    portal_categories: "Portal categories",
    portal_help_suggest: "Portal help suggest",
    portal_help_article: "Portal help article",
    actions_propose: "Actions propose",
  },
  /** modules/ai-admin: what kind of record a flagged excerpt came from. */
  AiSourceType: {
    ticket: "Ticket",
    document: "Document",
    device: "Device",
    alert: "Alert",
    article: "Article",
    category: "Category",
  },
} as const;
