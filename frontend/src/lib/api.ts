// Typed API client mirroring backend/app/api/v1 response models.

import { getLanguage } from "../i18n/core";
import { isGlobalRoute } from "./globalRoutes";
import type {
  DashboardLayoutResponse,
  WidgetPlacement,
} from "../modules/dashboard/layout";
import type {
  HostKeyConfirm,
  HostKeyOut,
  PushDispatchOut,
  PushRequestCreate,
  PushAuthorizationCreate,
  PushAuthorizationOut,
  PushCredentialScopeBind,
  PushCredentialScopeOut,
  PushRunCreate,
  PushRunOut,
  PushRunPrefixOut,
  PushRunPreviewOut,
  PushRunTargetPage,
} from "../modules/agent-push/types";
import type {
  ApplyRequest,
  ApplyResponse,
  ApprovalCreateRequest,
  ApprovalListFilters,
  ApprovalListResponse,
  BulkApprovalCreateRequest,
  BulkApprovalResponse,
  DeviceStatus,
  FleetReport,
  PatchApproval,
  PatchDeviceRollupFilters,
  PatchDeviceRollupListResponse,
  PatchPolicy,
  PatchPolicyInput,
  PatchAppSourceSummaryResponse,
  PatchRestartListResponse,
  PatchRestartRequestResponse,
  PatchRingListResponse,
  PatchRunFilters,
  PatchRunListResponse,
  PatchUpdateFilters,
  PatchUpdateListResponse,
  ScanRequest,
  ScanResponse,
} from "../modules/patch/types";
import type {
  Deployment,
  DeploymentCreateRequest,
  DeploymentExecuteResponse,
  DeviceDeploymentHistory,
  Package,
  PackageCreateRequest,
  Script,
  ScriptCreateRequest,
  ScriptListItem,
  ScriptPolicy,
  ScriptPolicyCreateRequest,
  ScriptPolicyRun,
  ScriptPolicyUpdateRequest,
  ScriptRunRequest,
  ScriptRunResponse,
  ScriptUpdateRequest,
} from "../modules/deploy/types";
import type {
  BackupManifest,
  BackupPoint,
  BackupPolicy,
  DeviceBackupStatus,
  FleetBackupReport,
  OffsiteTarget,
  OffsiteTargetCreate,
  OffsiteTargetUpdate,
  PolicyUpsertRequest,
  RecoveryKey,
  RecoveryKeyChallenge,
  Repository,
  RepositoryCreateRequest,
  RepositoryOffsite,
  RestoreRequest,
  RestoreResponse,
  RunResponse,
} from "../modules/backup/types";
import type {
  DeviceProvisioning,
  FleetProvisioningReport,
  OsImage,
  OsImageCreateRequest,
  ProfileUpsertRequest,
  ProvisionProfile,
  ProvisionRun,
  RunStartRequest,
  RunStartResponse,
} from "../modules/provisioning/types";
import type {
  Approval,
  ApprovalDecisionRequest,
  Calendar,
  ChangeCreateRequest,
  ChangeGate,
  ChangeGateSetRequest,
  ChangeRequest,
  ChangeUpdateRequest,
  Evidence,
  Link as ChangeLink,
  LinkCreateRequest,
  TransitionRequest,
} from "../modules/change/types";
import type {
  FindingCreateRequest,
  FindingOut,
  FindingUpdateRequest,
  PeriodReportOut,
  PlanCreateRequest,
  PlanOut,
  PlanStatusOut,
  PlanUpdateRequest,
  StepOut,
  StepUpsertRequest,
  TestCreateRequest,
  TestOut,
  TestRecordRequest,
  TestReportOut,
} from "../modules/bcp/types";
import type {
  AllocationCreate,
  AllocationOut,
  ComplianceRow,
  CostRow,
  LicenseCreate,
  LicenseKeyOut,
  LicenseOut,
  LicenseUpdate,
  ProductCreate,
  ProductOut,
  ProductUpdate,
  RenewalRow,
  ReviewIgnoreOut,
  ReviewIgnoreRequest,
  ReviewMapRequest,
  ReviewQueueRow,
  VendorCreate,
  VendorOut,
  VendorUpdate,
} from "../modules/licenses/types";
import type {
  EventOut,
  IdleSeatRow,
  RenewalRow as SubscriptionRenewalRow,
  SeatAssignCreate,
  SeatAssignmentOut,
  SpendSummary,
  SubscriptionCreate,
  SubscriptionOut,
  SubscriptionUpdate,
} from "../modules/subscriptions/types";
import type {
  ChecklistKind,
  ChecklistOut,
  DevicePersonOut,
  LoginOut,
  PersonCreate,
  PersonDetailOut,
  PersonListRow,
  PersonOut,
  PersonUpdate,
  StepState,
  UnmatchedOut,
} from "../modules/people/types";
import type {
  BudgetCreate,
  BudgetLineInput,
  BudgetLineOut,
  BudgetOut,
  BudgetUpdate,
  CategoryCreate,
  CategoryOut,
  CategoryUpdate,
  CostCenterCreate,
  CostCenterOut,
  CostCenterUpdate,
  ExpenseConfirm,
  ExpenseCreate,
  ExpenseListFilters,
  ExpenseOut,
  ExpenseUpdate,
  FxRateCreate,
  FxRateOut,
  FxRateUpdate,
  MaterializeOut,
  MaterializeRequest,
  PositionOut,
  SettingsOut,
  SettingsUpdate,
  VarianceOut,
} from "../modules/budget/types";
import type {
  ApprovalDecision,
  ArticleSuggestion,
  AttachmentOut,
  CannedResponseCreate,
  CannedResponseOut,
  CannedResponseUpdate,
  CategoryCreate as TicketCategoryCreate,
  CategoryOut as TicketCategoryOut,
  CategoryUpdate as TicketCategoryUpdate,
  CommentCreate,
  CommentOut,
  QueueCreate as TicketQueueCreate,
  QueueOut as TicketQueueOut,
  QueueUpdate as TicketQueueUpdate,
  StatusHistoryOut,
  TicketAssign,
  TicketCreate,
  TicketListFilters,
  TicketOut,
  TicketResolve,
  TicketStatusChange,
  TicketUpdate,
} from "../modules/service-desk/types";
import type {
  AllowlistEntryCreate,
  AllowlistEntryOut,
  ChannelCreate as NotificationChannelCreate,
  ChannelOut as NotificationChannelOut,
  ChannelUpdate as NotificationChannelUpdate,
  DeliveryListFilters,
  DeliveryListResponse,
  QueueMemberOut as NotificationQueueMemberOut,
  RuleCreate as NotificationRuleCreate,
  RuleOut as NotificationRuleOut,
  RuleUpdate as NotificationRuleUpdate,
  TestSendResult,
} from "../modules/notification/types";
import type {
  MailboxCreate as EmailMailboxCreate,
  MailboxOut as EmailMailboxOut,
  MailboxTestResult as EmailMailboxTestResult,
  MailboxUpdate as EmailMailboxUpdate,
  OAuthAppOut as EmailOAuthAppOut,
  OAuthAppUpdate as EmailOAuthAppUpdate,
  OAuthStartOut as EmailOAuthStartOut,
  MessageLogFilters as EmailMessageLogFilters,
  MessageLogOut as EmailMessageLogOut,
} from "../modules/email-intake/types";
import type {
  CalendarCreate as SlaCalendarCreate,
  CalendarDetail as SlaCalendarDetail,
  CalendarOut as SlaCalendarOut,
  CalendarUpdate as SlaCalendarUpdate,
  HolidayCreate as SlaHolidayCreate,
  HolidayOut as SlaHolidayOut,
  PolicyCreate as SlaPolicyCreate,
  PolicyOut as SlaPolicyOut,
  PolicyUpdate as SlaPolicyUpdate,
  SettingsOut as SlaSettingsOut,
  SettingsUpdate as SlaSettingsUpdate,
  TicketSlaOut,
  WindowIn as SlaWindowIn,
  WindowOut as SlaWindowOut,
} from "../modules/sla/types";
import type {
  AlertDetailOut as HealthAlertDetailOut,
  AlertFilters as HealthAlertFilters,
  AlertOut as HealthAlertOut,
  AlertSummary as HealthAlertSummary,
  ApprovedActionCreate,
  ApprovedActionOut,
  ApprovedActionUpdate,
  AssignmentCreate as HealthAssignmentCreate,
  AssignmentOut as HealthAssignmentOut,
  CheckIn as HealthCheckIn,
  CheckTypeOut as HealthCheckTypeOut,
  DeviceHealthOut,
  JobDispatchedOut as HealthJobDispatchedOut,
  ProfileChecksOut as HealthProfileChecksOut,
  ProfileCreate as HealthProfileCreate,
  ProfileOut as HealthProfileOut,
  ProfileUpdate as HealthProfileUpdate,
  SettingsOut as HealthSettingsOut,
  SettingsUpdate as HealthSettingsUpdate,
} from "../modules/health/types";
import type {
  PortalAttachmentOut,
  PortalCategoryOut,
  PortalCommentCreate,
  PortalCommentOut,
  PortalHelpArticle,
  PortalHelpSuggestion,
  PortalTicketCreate,
  PortalTicketDetail,
  PortalTicketListFilters,
  PortalTicketSummary,
} from "../modules/portal/types";
import type {
  ReportCatalogue,
  ReportPage,
  ReportParams,
} from "../modules/reporting/types";
// Every IPAM name is aliased with an `Ipam` prefix: this file already imports
// `SettingsOut`, `ProfileOut` and friends from four other modules, and a bare
// `SubnetOut`/`RangeOut`/`ScopeOut` here would be one rename away from binding
// to the wrong module's shape without a type error.
import type {
  AddressCreate as IpamAddressCreate,
  AddressOut as IpamAddressOut,
  AddressUpdate as IpamAddressUpdate,
  AllocateRequest as IpamAllocateRequest,
  DocRecordConversionOut as IpamDocRecordConversionOut,
  DriftFilters as IpamDriftFilters,
  DriftOut as IpamDriftOut,
  GridEntryOut as IpamGridEntryOut,
  HistoryEntryOut as IpamHistoryEntryOut,
  ImportPreviewOut as IpamImportPreviewOut,
  ImportResultOut as IpamImportResultOut,
  ImportTarget as IpamImportTarget,
  OrphanOut as IpamOrphanOut,
  RangeCreate as IpamRangeCreate,
  RangeOut as IpamRangeOut,
  ScopeCreate as IpamScopeCreate,
  ScopeOut as IpamScopeOut,
  SearchOut as IpamSearchOut,
  SubnetCreate as IpamSubnetCreate,
  SubnetFilters as IpamSubnetFilters,
  SubnetOut as IpamSubnetOut,
  SubnetUpdate as IpamSubnetUpdate,
  SweepRunOut as IpamSweepRunOut,
  SweepSettings as IpamSweepSettings,
  UtilizationOut as IpamUtilizationOut,
  VlanCreate as IpamVlanCreate,
  VlanOut as IpamVlanOut,
  VrfCreate as IpamVrfCreate,
  VrfOut as IpamVrfOut,
} from "../modules/ipam/types";
import type {
  AiInjectionFlagOut,
  AiProviderOut,
  AiSessionOut,
  AiSettingsOut,
  AiSettingsUpdate,
  AiStatus,
  AiTranscriptOut,
  AiTurnOut,
  AiTurnRequest,
} from "../modules/ai-admin/types";
import type {
  AiCopilotSession,
  AiCopilotTurn,
  AiResolverSession,
  AiResolverTurn,
} from "../modules/ai-automation/personaTypes";
import type {
  AiActionConfirm,
  AiActionDispatched,
  AiActionOut,
  AiActionPreviewOut,
  AiActionReject,
  AiCeilingOut,
  AiCeilingUpdate,
} from "../modules/ai-automation/types";

export type JobStatus =
  | "queued"
  | "dispatched"
  | "running"
  | "succeeded"
  | "failed"
  | "expired"
  | "refused"
  /**
   * Terminal, and reached only by a deliberate human act. Adding it here is
   * what makes the compiler name every place that has to decide what to show
   * for it -- `jobState.ts`'s `Record<JobStatus, BadgeVariant>` is exhaustive
   * on purpose.
   */
  | "cancelled";

/** What the operator INTENDS a record to be. Labelled by `enums.DeviceLifecycle`. */
export type DeviceLifecycle = "in_service" | "stock" | "unmanageable" | "retired";

/** The kind of asset. Labelled by `enums.DeviceAssetType`. */
export type DeviceAssetType = "computer" | "server" | "network" | "printer" | "phone" | "appliance" | "other";

/** The mechanism that wrote the record. Labelled by `enums.DeviceSource`. */
export type DeviceSource = "agent" | "manual" | "discovery";

/** Who stood behind the record. Labelled by `enums.DeviceProvenance`. */
export type DeviceProvenance = "agent" | "operator" | "promoted" | "sweep";

/** Where a computer that joined through the install link stands. Labelled by `enums.DeviceReviewState`. */
export type DeviceReviewState = "approved" | "pending" | "rejected";

/**
 * A fleet record. Since the asset-inventory cycle this is not necessarily a
 * machine with an agent: it may be a switch, or hardware still in a box
 * (`docs/modules/asset-inventory/spec.md` §2).
 *
 * MANAGED IS `agent_status !== null`. It is a join server-side and is
 * deliberately not a stored column, so the console must not invent one either.
 */
export interface Device {
  id: string;
  /**
   * The device's client and site (MSP clients spec §1.5). OPTIONAL because
   * they are additive: an API deployed before they were served simply does
   * not send them. `PATCH /devices/{id}` with `site_id` moves a device to
   * another site of its own client.
   */
  client_id?: string;
  site_id?: string;
  /** NULL for an asset that has none: a switch, or hardware still in a box. */
  hostname: string | null;
  fqdn: string | null;
  os_family: "windows" | "linux" | "macos" | null;
  os_version: string;
  architecture: string;
  device_group_id: string | null;
  status: string;
  agent_status: "online" | "offline" | null;
  last_seen_at: string | null;
  /**
   * Certificate lifecycle (docs/modules/agent/cert-renewal.md §6).
   *
   * `cert_state` is `renewing` while the platform is rotating this device's
   * certificate (automatic, retried on every connect) and `expiring` once it
   * has been trying for a while without success -- which is the state a human
   * should act on. `expired` means the device needs re-deploying: the
   * certificate is the credential a rotation would have travelled over.
   *
   * Both are null for a device with no agent row, exactly as `agent_status` is.
   */
  cert_not_after: string | null;
  cert_state: "ok" | "renewing" | "expiring" | "expired" | null;
  /**
   * Has this device's agent certificate been REVOKED?
   *
   * `cert_state` is derived from `cert_not_after` alone and knows nothing
   * about revocation, so before this field a revoked certificate rendered as
   * `ok` until its natural expiry. `false` for a device with no agent, which
   * has no certificate — `cert_state === null` is what says that.
   */
  cert_revoked: boolean;
  /** What the operator INTENDS this record to be. Never derived. */
  lifecycle: DeviceLifecycle;
  asset_type: DeviceAssetType;
  /** How the record came to exist, and the first thing to check when one looks wrong. */
  source: DeviceSource;
  /**
   * WHO, IF ANYONE, STOOD BEHIND THIS ROW.
   *
   * Since the operator's ruling of 2026-08-26 a subnet sweep records every
   * host it finds as an asset, so the fleet list carries rows nobody chose
   * beside rows somebody typed in. `sweep` is the one that means "the platform
   * saw something answer at this address and wrote it down" -- no person has
   * confirmed the machine exists, what it is, or that it belongs here.
   *
   * NOT A DUPLICATE OF `source`, which names the mechanism. They differ
   * exactly once: a `discovery` row is `promoted` when an operator picked it
   * off the Discovered screen and `sweep` when the sweep recorded it.
   */
  provenance: DeviceProvenance;
  description: string | null;
  primary_ip: string | null;
  owner: string | null;
  location: string | null;
  /**
   * WHO HOLDS THIS DEVICE (operator decision 2026-09-23: People folded into
   * Devices): the person its link names, set by hand or from who signs in.
   *
   * Optional and nullable, and null is not only "nobody": the server sends it
   * only to a caller holding `people.read`, and only `GET /devices` resolves
   * it -- a single device's page asks `/people/devices/{id}`, which also says
   * who set it. Not `owner`, which is free text typed on the asset record.
   */
  assigned_to?: DeviceHolder | null;
  /**
   * CAN THIS DEVICE'S AGENT SHARE ITS SCREEN? The fact a screen session is
   * refused on (409 "agent too old to share its screen"), served by the same
   * rule the server refuses by, so Remote sessions can say so before asking.
   *
   * `null` when there is no agent at all (nothing to update: install one),
   * `false` when the agent predates screen share (update it). Optional
   * because a server older than the field omits it, and ABSENT MEANS UNKNOWN:
   * never read `undefined` as "too old".
   */
  screen_capable?: boolean | null;
  /**
   * WAITING FOR REVIEW (install-link spec §1.5). `pending` is a computer that
   * joined through the install link and that nobody has approved yet: it
   * receives inventory, health monitoring and certificate renewal, nothing
   * else. Optional because an older server omits it; absent means approved.
   * `rejected` is served only in the review tab, and only beside a
   * `rejection_outcome` that says the agent may still be installed.
   */
  review_state?: DeviceReviewState;
  /** Where it joined the install link from; null for any other way a row exists. */
  link_joined_from?: string | null;
  /** When it joined through the install link. */
  link_joined_at?: string | null;
  /**
   * How a rejection ended; null while the agent still has time to confirm and
   * on every row that was not rejected. `timeout`, `agent_did_not_confirm` and
   * `agent_cannot_uninstall` mean the agent may still be on the computer and a
   * technician has to remove it by hand. A wire value: `manualRemovalStatus`
   * says it in words. Optional because an older server omits it.
   */
  rejection_outcome?: "agent_confirmed" | "timeout" | "agent_did_not_confirm" | "agent_cannot_uninstall" | null;
}

/** A device's holder: enough for a cell and a link, nothing more. */
export interface DeviceHolder {
  id: string;
  display_name: string;
}

/** The fleet list's tab strip, as a vocabulary. Mirrors the server's. */
export type DeviceState = "all" | "online" | "offline" | "assets" | "certs" | "review";

/** What `GET /devices` accepts. Every field optional; absent means unfiltered. */
export interface DeviceListParams {
  q?: string;
  state?: DeviceState;
  provenance?: Device["provenance"] | "";
  osFamily?: Device["os_family"];
  /** One client's machines (`?client_id=`): what IPAM's sweeper picker lists. */
  clientId?: string;
  limit?: number;
  offset?: number;
}

/**
 * What each tab WOULD show from the current search — the tab itself excluded.
 *
 * Counted in SQL beside the page. The strip cannot disagree with the table
 * under it, because `DevicePage.total` is this object's entry for the active
 * tab: the server does not count it twice.
 */
export interface DeviceStateCounts {
  all: number;
  online: number;
  offline: number;
  assets: number;
  certs: number;
  /** Machines waiting for review. Optional: an older server omits it. */
  review?: number;
}

/**
 * The whole estate, with NO filter applied. The stat tiles are these numbers.
 *
 * Deliberately not narrowed by the search: "Managed endpoints" is a claim about
 * the estate, and a tile that shrank as somebody typed would be a worse version
 * of the defect server-side counting exists to fix.
 *
 * `managed` is a join — a device is managed exactly when an agent row exists
 * beside it — and is never a stored column.
 */
export interface DeviceFleetCounts {
  total: number;
  managed: number;
  online: number;
  cert_attention: number;
}

/** `Page<Device>` plus the two count sets the fleet screen renders. */
export interface DevicePage extends Page<Device> {
  state_counts: DeviceStateCounts;
  fleet: DeviceFleetCounts;
}

/** Approve all (GPO MSI spec 2026-09-30 §2.5): the list the dialog showed. */
export interface ApproveAllRequest {
  device_ids: string[];
  /** `reviewListDigest(device_ids)`: SHA-256 hex of the sorted ids, one per line. */
  list_digest: string;
}

/** What an Approve all did. */
export interface ApproveAllResult {
  bulk_id: string;
  /** Listed computers approved now. */
  approved: number;
  /** Listed computers that were no longer waiting. */
  not_pending: number;
  /** Computers still waiting: they joined after the list was shown. */
  still_waiting: number;
}

/** What a Reject did (install-link spec §1.5, §4). */
export interface DeviceRejected {
  device_id: string;
  /** `manual`: the agent is too old to remove itself; a technician must. */
  uninstall: "dispatched" | "manual";
  job_id: string | null;
  /** One sentence for the technician. Rendered as given. */
  message: string;
}

/**
 * An address the fleet has seen that the inventory cannot account for
 * (`docs/modules/asset-inventory/spec.md` §3).
 *
 * A candidate is NOT an asset. An echo reply is not a machine — a sweep on a
 * corporate LAN returns printers, phones, a colleague's laptop on the wrong
 * VLAN and the coffee machine — so these become rows only when an operator
 * promotes them.
 */
export interface AssetCandidate {
  address: string;
  mac: string | null;
  /** Null when the prefix is not in the platform's table. NEVER a guess. */
  vendor: string | null;
  /** Shown when `vendor` is null: a fact the operator can look up. */
  oui_prefix: string;
  hostname: string;
  first_seen_at: string;
  last_seen_at: string;
  sightings: number;
  /** `medium` = ARP saw it; `low` = only ICMP, i.e. something answered. */
  confidence: "high" | "medium" | "low";
  subnet_id: string | null;
  /** Whose network saw it. The same address in two clients is two candidates. */
  client_id: string;
}

export interface AssetCandidatePage {
  items: AssetCandidate[];
  /** How many exist, not how many are in `items`. */
  total: number;
  truncated: boolean;
  /**
   * Subnets whose most recent sweep found no agent able to run it.
   *
   * An empty list and "nothing could look" are indistinguishable on screen and
   * mean opposite things. This is what lets the page tell them apart.
   */
  unsweepable_subnets: number;
}

/**
 * Two device rows that MIGHT be one machine (asset-inventory spec §4).
 *
 * The platform links an enrolling agent to an existing record only on a
 * physical MAC. On weaker evidence it files one of these instead, because
 * auto-linking on a hostname merges two real machines that share a name across
 * sites and nothing afterwards recovers which data belonged to which.
 */
export interface AssetSuggestion {
  id: string;
  /** Why this was a question rather than a link. */
  basis: "hostname" | "ip" | "multiple";
  created_at: string;
  /** The row enrolment created. */
  device: SuggestionDevice;
  /** The row that already existed. */
  candidate: SuggestionDevice;
}

/** Just enough of a device row to compare it with another. */
export interface SuggestionDevice {
  id: string;
  hostname: string | null;
  description: string | null;
  primary_ip: string | null;
  source: Device["source"];
  lifecycle: Device["lifecycle"];
  last_seen_at: string | null;
  managed: boolean;
}

export interface PromoteResult {
  address: string;
  created: boolean;
  device_id: string | null;
  reason: string;
}

/** What an operator types about an asset, on create and on edit. `source` is
 *  stamped server-side, never sent. */
export interface AssetInput {
  hostname?: string | null;
  description?: string | null;
  primary_ip?: string | null;
  asset_type?: Device["asset_type"];
  lifecycle?: Device["lifecycle"];
  owner?: string | null;
  location?: string | null;
  os_family?: Device["os_family"];
  serial?: string | null;
  asset_tag?: string | null;
  /**
   * The machine's full DNS name, typed by a person. Windows push is Kerberos
   * only and needs it (design 2026-09-28 §2.3). Validated server-side as a
   * hostname with a dot, never an IP; stored lower-cased; `null` or `""`
   * clears it. Refused (409) on a device whose agent reports it.
   */
  fqdn?: string | null;
  /** The processor a push installs for. `null` clears it. Agent-owned when managed. */
  architecture?: "amd64" | "arm64" | null;
}

/**
 * The body of `createAsset`: what an operator types, and the SITE the asset is
 * filed under (MSP clients spec §1.5). Required by `POST /devices`; its client
 * is the site's, derived server-side and never sent. Not on an edit: a
 * device's client never changes.
 */
export interface AssetCreateInput extends AssetInput {
  site_id: string;
}

export interface JobLogEntry {
  ts: string;
  level: string;
  message: string;
  artifact_ref: string | null;
}

export interface Job {
  id: string;
  type: string;
  device_id: string;
  status: JobStatus;
  attempts: number;
  params: Record<string, unknown>;
  result: Record<string, unknown> | null;
  issued_at: string;
  expires_at: string;
  started_at: string | null;
  finished_at: string | null;
  /**
   * When a human asked for this job to stop, or `null`.
   *
   * OPTIONAL THOUGH THE API ALWAYS SENDS IT, for the reason `Me.roles` is:
   * `?` here is a superset of the wire shape, and it also covers a console
   * talking to an API deployed before migration 0071, where the key is simply
   * absent. A required field would make that console crash rather than render
   * a job.
   */
  cancel_requested_at?: string | null;
  /**
   * What became of that request: `requested` (asked, the endpoint has not
   * answered), `withdrawn` (never sent to the device), `dropped` (the endpoint
   * discarded it before starting), `interrupted` (stopped mid-flight),
   * `too_late` (it had already finished), `unsupported` (this endpoint's agent
   * is too old to be interrupted).
   *
   * A BARE STRING RATHER THAN A UNION, deliberately. A control plane may add
   * outcomes this console has never seen, and a union pinned to today's set
   * would make tomorrow's response a type lie. `describeCancellation` renders
   * only the values it recognises and claims nothing for the rest.
   */
  cancel_outcome?: string | null;
  /**
   * The job this one re-runs, or `null`.
   *
   * OPTIONAL for the same reason `cancel_requested_at` is: it also covers a
   * console talking to an API deployed before migration 0076, where the key is
   * simply absent.
   */
  retry_of_job_id?: string | null;
  logs?: JobLogEntry[] | null;
}

/**
 * One client the signed-in person holds any role in (`GET /me`'s `clients`,
 * phase 3 design §1.3), archived ones left out. Mirrors
 * `backend/app/api/v1/me.py::MeClient`. Not narrowed by `X-Client-Id`: it is
 * what the top bar's selector offers and what a new row's Client field
 * filters (`lib/clientSelection.ts`). It is the person's own data, so it needs
 * no `clients.read`.
 */
export interface MeClient {
  id: string;
  name: string;
  kind: "customer" | "internal" | "unassigned";
  /** Optional modules on for this client: `budget`, `bcp`, `change`. */
  enabled_modules: string[];
  /** The codes the person holds IN THIS CLIENT, sorted. */
  permissions: string[];
}

export interface Me {
  user_id: string;
  org_id: string;
  email: string;
  display_name: string;
  /**
   * The codes the person holds in AT LEAST ONE client of the request's scope
   * -- their clients, narrowed by `X-Client-Id` when one is chosen (phase 3
   * design §1.3, `require_permission`'s rule). What the menu and the pages
   * render from.
   */
  permissions: string[];
  /**
   * The person's clients, archived ones left out, each with the codes held
   * there (phase 3 design §1.3). Not narrowed by `X-Client-Id`.
   *
   * OPTIONAL for the reason the profile fields below are: an API older than
   * phase 3 does not send it, and ~90 `Me` literals in the tests would
   * otherwise need it. Read as `?? []`.
   */
  clients?: MeClient[];
  /**
   * The codes held on EVERY client (`require_permission_on_every_client`'s
   * rule), not narrowed by `X-Client-Id`: what a screen of configuration every
   * client shares offers its writes on (phase 3 design §2).
   *
   * OPTIONAL like `clients`. Absent, `permissions` IS this set -- before phase
   * 3, `/me` returned only the codes held on every client -- and
   * `useEverywherePermissions` reads it so.
   */
  permissions_everywhere?: string[];
  /**
   * The role names the caller holds, sorted (`GET /me`).
   *
   * OPTIONAL for the same reason the profile fields below are: a console
   * talking to an API deployed before this field existed simply does not get
   * it, and the chrome must render a name and an org rather than crash on a
   * missing key.
   */
  roles?: string[];
  /**
   * The three profile fields ITMS owns (spec §7.3).
   *
   * OPTIONAL, THOUGH `MeResponse` ALWAYS SENDS THEM, and the reason is worth
   * recording rather than being read as sloppiness. `?` here is a superset of
   * the wire shape: it also covers a console talking to an API deployed before
   * migration 0066, where the keys are simply absent. Declaring them required
   * would be marginally tighter against today's server and would invalidate
   * the `Me` literal in ~20 unrelated test files across the frontend, which is
   * a wide blast radius for a type nicety — so the optionality stays until
   * something needs the stricter guarantee.
   *
   * They are read with `?? ""` at the one place they are rendered, so an
   * absent value is an empty box and never the string "undefined".
   *
   * `email` is NOT in that set — Keycloak owns the address, and this console
   * shows it rather than editing it.
   */
  phone?: string | null;
  timezone?: string | null;
  locale?: string | null;
  /**
   * Present only on the `PATCH /me` response, and only when `locale` was sent.
   * `false`: the language was saved here but could not be written to the
   * sign-in pages. `true` or absent/null: nothing for the person to be told.
   */
  identity_locale_synced?: boolean | null;
}

/**
 * The caller's own organization (`GET /orgs/current`).
 *
 * There is no by-id variant and there is not going to be one: the backend
 * resolves the org from the bearer token, so the client cannot name a target.
 */
export interface CurrentOrg {
  id: string;
  name: string;
  slug: string;
  status: string;
  settings: OrgSettings;
}

/**
 * One client of the organisation (`GET /api/v1/clients`, MSP clients spec
 * §1.1, §3.2): a company it serves, or one of the two system clients --
 * `internal` (its own IT, whose id is the organisation's) and `unassigned`
 * (where mail from unknown senders lands, and which no grant may name).
 * Mirrors `backend/app/api/v1/clients.py::ClientOut`.
 */
export interface ClientOut {
  id: string;
  name: string;
  kind: "customer" | "internal" | "unassigned";
  /** `active` or `archived`: clients are archived, never deleted. */
  status: string;
  email_domains: string[];
  phone: string;
  address: string;
  website: string;
  notes: string;
  enabled_modules: string[];
  device_count: number;
  site_count: number;
}

/**
 * One site of a client (`backend/app/api/v1/clients.py::SiteOut`): an office,
 * one LAN. What a device, an enrollment token, an IPAM VLAN or subnet and a
 * site server are filed under.
 */
export interface ClientSiteOut {
  id: string;
  client_id: string;
  name: string;
  address: string;
  description: string;
  egress_networks: string[];
  archived_at: string | null;
  device_count: number;
}

/**
 * One person of a client (`backend/app/api/v1/clients.py::ContactOut`): a
 * user of kind `contact`, who will use the portal and never the console.
 */
export interface ContactOut {
  id: string;
  client_id: string;
  kind: string;
  email: string;
  display_name: string;
  phone: string | null;
  /** `active` or `disabled`. */
  status: string;
}

/** A partial edit of a contact; `status` switches it off and on (`ContactUpdate`). */
export interface ContactUpdate {
  email?: string;
  display_name?: string;
  phone?: string | null;
  status?: "active" | "disabled";
}

/**
 * The closed allowlist of org settings keys, mirroring
 * `backend/app/api/v1/orgs.py::SETTINGS_ALLOWLIST`. Every key is optional and
 * every one is presentation or contact information.
 *
 * NOTHING SECRET GOES HERE, and the type is what says so: database DSNs, the
 * OpenBao token, TLS/CA material, S3 keys, the audit HMAC key and the
 * job-signing key are environment- and vault-owned, and the backend refuses
 * any key it does not recognise with a 422. Adding a field to this interface
 * without adding it to the backend allowlist produces a request the server
 * rejects, which is the intended direction of failure.
 */
export interface OrgSettings {
  timezone?: string;
  locale?: string;
  date_format?: "iso" | "dmy" | "mdy";
  support_email?: string;
  support_phone?: string;
  support_url?: string;
  console_banner?: string;
  console_banner_severity?: "info" | "warning" | "critical";
  brand_primary_color?: string;
  default_page_size?: number;
  show_device_serials?: boolean;
  /**
   * The organization's MCP switch: whether AI apps may connect here at all.
   * Off unless `true`; the MCP server reads it on every request
   * (`backend/app/mcp/server.py`). Edited on Settings → AI connections.
   */
  mcp_enabled?: boolean;
}

/**
 * One AI app that called tools in this organization in the last 90 days
 * (`backend/app/mcp/api.py::McpAppOut`).
 */
export interface McpAppOut {
  /** The app's OAuth client id, as the identity provider registered it. */
  client_id: string;
  /** How many people of this organization used it in the last 90 days. */
  people: number;
  /** When anyone here last called a tool through it. */
  last_used_at: string | null;
  /** You revoked it for yourself after your last use of it. */
  revoked_for_me: boolean;
  /** It was revoked for the organization after its last use. */
  revoked_for_everyone: boolean;
}

/** `GET /mcp/connections`: the switch, the address to give an AI app, and the apps. */
export interface McpConnectionsOut {
  enabled: boolean;
  /** `null` when the deployment has no public console address configured. */
  address: string | null;
  apps: McpAppOut[];
}

/** Whose access a revoke removes. `everyone` also needs `orgs.manage` server-side. */
export type McpRevokeScope = "me" | "everyone";

/**
 * `POST /mcp/connections/{client_id}/revoke`. The revocation is in force when
 * this returns; `keycloak_failures` counts the people whose consent could not
 * be withdrawn at the identity provider, for whom the app could still obtain
 * NEW tokens until a revoke succeeds.
 */
export interface McpRevokeOut {
  revoked: boolean;
  keycloak_failures: number;
}

/**
 * A partial update. `settings` is MERGED server-side key by key; sending
 * `null` for a key clears it. Omitting `settings` entirely leaves the stored
 * document alone.
 *
 * `slug` and `status` are absent deliberately -- they are read-only, and the
 * server answers a body containing either with a 422.
 */
export interface OrgUpdateRequest {
  name?: string;
  settings?: Partial<Record<keyof OrgSettings, OrgSettings[keyof OrgSettings] | null>>;
}

export interface Page<T> {
  items: T[];
  total: number;
}

/** What `GET /jobs` accepts for a page. `order` is the server's, never a
 *  re-sort of rows already in hand — see `ApiClient.listJobs`. */
export interface JobListParams {
  limit?: number;
  offset?: number;
  order?: "newest" | "oldest";
}

export interface JobDispatchRequest {
  type: string;
  device_id: string;
  params: Record<string, unknown>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

import type {
  SegmentCheckOut,
  SiteServerCreate,
  SiteServerFilters,
  SiteServerOut,
  SiteServerUpdate,
  SiteWakeOut,
  SiteWakeRequest,
} from "../modules/site-servers/types";
import type {
  CreatedUserOut,
  EffectivePermissionsOut,
  EmailSettingsOut,
  EmailSettingsUpdate,
  MailResultOut,
  MeProfileUpdate,
  PermissionCatalogueOut,
  RoleAssign,
  RoleAssignmentOut,
  RoleCreate,
  RoleListOut,
  RoleOut,
  RolePermissionsOut,
  RoleUpdate,
  TestSendIn,
  TestSendOut,
  UserCreate,
  UserListOut,
  UserOut,
} from "../modules/settings/types";
import type { PlatformStatus } from "../modules/platform-status/types";
import type { ErrorGroup, ErrorGroupDetail, ErrorGroupList } from "../modules/error-monitor/types";

import type {
  AuditListResponse,
  AuditVerifyResponse,
} from "../modules/audit/types";
import type {
  EnrollmentTokenCreateRequest,
  EnrollmentTokenCreated,
  EnrollmentTokenPage,
} from "../modules/enrollment/types";


/** One downloadable agent artefact. Mirrors `ReleaseOut` server-side. */
/** What removing a whole agent version did. */
export interface AgentVersionRemoved {
  version: string;
  removed: number;
  history_cleared: number;
  /** Stored files that could not be deleted; the catalogue rows are gone regardless. */
  files_not_removed: string[];
}

export interface AgentRelease {
  id: string;
  version: string;
  os: string;
  arch: string;
  size_bytes: number;
  sha256: string;
  signed: boolean;
  notes: string;
  published_at: string | null;
  /**
   * False until the bytes are uploaded. A release without an artefact cannot
   * produce an install command, and the page must say so rather than offering
   * a button that 409s.
   */
  has_artifact: boolean;
  /** Self-update maturity: a `pilot` release reaches pilot rings only. */
  channel?: "pilot" | "stable";
  /** Whether the offline release signature is recorded for this row. */
  release_signed?: boolean;
}

/**
 * `POST /agent/releases`. Mirrors `ReleaseCreateRequest` (`extra="forbid"`):
 * there is deliberately no `sha256` -- the server computes it from the bytes.
 * `signed` is an Authenticode / Developer ID claim, NOT the release signature.
 */
export interface AgentReleaseCreate {
  version: string;
  os: string;
  arch: string;
  signed: boolean;
  notes: string;
}

/** `POST /agent/releases/versions/{version}/promote`. Mirrors `PromoteOut`. */
export interface AgentReleasePromoted {
  version: string;
  channel: "pilot" | "stable";
  /** How many (os, arch) rows of the version were changed. */
  rows: number;
}

// ---- Agent self-update (spec-self-update.md §8.6) ---------------------------

export interface FleetVersions {
  total: number;
  versions: Record<string, number>;
  latest_stable: string | null;
  update_available: number;
  needs_manual_update: {
    device_id: string;
    hostname: string | null;
    version: string;
    reason: "version_unknown" | "no_agent_update";
  }[];
}

export interface UpdateRingSpec {
  name: string;
  device_group_id?: string | null;
  is_pilot?: boolean;
}

export interface UpdateRolloutRequest {
  target_version: string;
  is_rollback?: boolean;
  reason?: string;
  rings: UpdateRingSpec[];
  batch_size?: number;
  failure_threshold_pct?: number;
  health_window_s?: number;
}

export interface UpdateRingPreview {
  ordinal: number;
  name: string;
  is_pilot: boolean;
  device_group_id: string | null;
  total: number;
  eligible: number;
  needs_manual_update: number;
  no_artifact: number;
  already_current: number;
  other_skipped: number;
}

export interface UpdatePreview {
  target_version: string;
  is_rollback: boolean;
  channel: string | null;
  rings: UpdateRingPreview[];
}

export interface UpdateRing {
  id: string;
  ordinal: number;
  name: string;
  is_pilot: boolean;
  device_group_id: string | null;
  state: "pending" | "active" | "done";
  activated_at: string | null;
  counts: Record<string, number>;
}

export interface UpdateRollout {
  id: string;
  target_version: string;
  is_rollback: boolean;
  state: "draft" | "running" | "paused" | "completed" | "cancelled";
  batch_size: number;
  failure_threshold_pct: number;
  health_window_s: number;
  reason: string;
  pause_reason: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  rings: UpdateRing[];
}

export interface UpdateTarget {
  id: string;
  ring_id: string;
  device_id: string;
  hostname: string | null;
  from_version: string;
  state: string;
  reason: string;
  attempts: number;
  job_id: string | null;
  updated_at: string;
}

export type UpdateRolloutAction = "start" | "pause" | "resume" | "advance" | "cancel";

export interface AgentInstallCommand {
  release: AgentRelease;
  /** Shown exactly once, like every enrollment token in this platform. */
  token: string;
  token_expires_at: string;
  download_url: string;
  download_expires_in_s: number;
  sha256: string;
  command: string;
}

/**
 * The organisation's install link (install-link spec 2026-09-28 §1.1).
 *
 * THE LINK ITSELF IS NOT HERE. It exists server-side only as a hash; the full
 * link is in `InstallLinkSaved.url`, in the one response that generated it.
 */
export interface InstallLinkSettings {
  /** On right now: switched on and not past its automatic switch-off. */
  enabled: boolean;
  /** Whether a link has ever been generated. */
  has_code: boolean;
  networks: string[];
  disable_at: string | null;
  hourly_limit: number;
  /** When it was switched on, while it is on. */
  enabled_at: string | null;
  joined_since_enabled: number;
}

export interface InstallLinkSaved extends InstallLinkSettings {
  /** The full link, present ONLY in the response that generated it. */
  url: string | null;
}

export interface InstallLinkUpdate {
  enabled: boolean;
  networks: string[];
  disable_at: string | null;
  hourly_limit: number;
}

/**
 * The Group Policy MSI (GPO MSI spec 2026-09-30 §2.1): the file, the name the
 * server gave it and the SHA-256 it computed and audited.
 */
export interface InstallLinkMsi {
  blob: Blob;
  /** `ITMS-Agent-<host>[@<port>]-<arch>.msi`, from `Content-Disposition`. */
  filename: string;
  /** Hex SHA-256 of these bytes, from `X-Content-SHA256`. */
  sha256: string;
}

export type MsiArch = "amd64" | "arm64";

/**
 * Managed distribution — what a GPO or Intune deployment consumes.
 * `docs/modules/agent-distribution/spec-managed-distribution.md`.
 *
 * THERE IS NO TOKEN FIELD HERE AND THERE MUST NEVER BE ONE. A GPO
 * distribution point is read by every machine account in the domain by
 * construction, so a package carrying a credential IS a fleet-wide standing
 * secret. The credential is a separate call under a separate permission
 * (`mintAgentDeploymentToken`), and the backend asserts the negative from both
 * sides — no token in this body, and no enrollment token created by the call.
 */
export interface AgentDeploymentStep {
  title: string;
  detail: string;
  /** "" when there is nothing to paste. */
  command: string;
}

export interface AgentDeploymentIntegrity {
  sha256: string;
  /** Run by the ADMINISTRATOR on the file they downloaded, before it reaches a
   *  share. Not by the endpoint. */
  verify_command: string;
  /**
   * FALSE on GPO and Intune, and that is the whole point of the field. Both
   * copy a file from a distribution point and run it; neither verifies a
   * digest, so the endpoint-side check the one-machine install command performs
   * does not happen there. Rendering the same reassurance for every channel
   * would be a claim the mechanism does not support.
   */
  verified_at_install: boolean;
  notes: string[];
}

export type AgentDeploymentChannel = "gpo" | "intune" | "manual";

export interface AgentDeploymentPackage {
  release: AgentRelease;
  channel: string;
  download_url: string;
  download_expires_in_s: number;
  sha256: string;
  staging: AgentDeploymentStep[];
  /** Installs the service and does NOT enrol. Names no token. */
  install_command: string;
  /** Spends the credential, separately, token from the environment or stdin. */
  enrol_command: string;
  integrity: AgentDeploymentIntegrity;
}

/**
 * One bounded, multi-use enrolment credential, shown exactly once.
 *
 * MULTI-USE BY NECESSITY: a fleet cannot race for one use. What makes that
 * defensible is that `max_uses` and the lifetime are REQUIRED and CAPPED on
 * this route, where `POST /enrollment/tokens` defaults `max_uses` to null —
 * which on that table means unlimited.
 */
export interface AgentDeploymentToken {
  token: string;
  token_id: string;
  token_expires_at: string;
  max_uses: number;
  channel: string;
  /** What the holder gains if the distribution channel leaks it. Worded
   *  server-side and per channel: "who can read this" differs by an order of
   *  magnitude between GPO and Intune. */
  blast_radius: string[];
}


/* ------------------------------------------------------------ device groups */

/**
 * One device group -- the platform's targeting and scoping primitive.
 *
 * `id` is the value a deployment's `target_id`, a patch/backup/remote policy's
 * `device_group_id`, a health profile assignment and a `user_roles` RBAC grant
 * all take. `precedence` is what decides which membership rule claims a device
 * first: lower wins, and `name` is only the tiebreak. Before migration 0072
 * the alphabet decided it, so renaming a group moved machines between patch,
 * backup and remote policies.
 */
export interface DeviceGroup {
  id: string;
  /** The client whose devices the group organises, fixed for the group's life. */
  client_id: string;
  name: string;
  parent_group_id: string | null;
  description: string;
  /** True when membership is decided by rules rather than by hand. */
  dynamic: boolean;
  /** Direct members only. It is the blast radius of a group-targeted job,
   *  which does not descend into child groups. */
  device_count: number;
  precedence: number;
  archived_at: string | null;
}

/**
 * When the membership sweep last ran for this organization, and what it did.
 *
 * WHY IT IS ON THE LIST RESPONSE. Every `device_count` above is as of the last
 * sweep and can be up to `interval_seconds` behind; the design asks for "a
 * timestamp, never alone" precisely so a stale count is legible rather than
 * read as a wrong one. Serving it separately would put the number and the
 * thing that dates it on two requests, either of which can fail alone.
 *
 * IT COMES UNDER `devices.read`, WHICH IS THE WHOLE POINT. The same facts are
 * in the `device_group.membership_reevaluated` audit event, behind
 * `audit.read` -- a code the Groups page's gate does not entitle a caller to
 * hold. So the page could only state the INTERVAL, which cannot distinguish
 * "swept four minutes ago" from "the worker has been down since Tuesday".
 */
export interface MembershipEvaluation {
  /** When the sweep last COMPLETED. Written on every run, including for an
   *  organization holding no rule-driven group at all -- "the worker is alive"
   *  is what such an organization still needs to know. */
  last_evaluated_at: string;
  /** When it last read EVERY active device rather than only those whose
   *  inventory moved, or null if it never has. */
  last_full_pass_at: string | null;
  /** Whether the LAST run read every active device. `false` is the steady
   *  state once the incremental watermark is in force. */
  full_pass: boolean;
  groups_evaluated: number;
  /** The devices the last run considered -- only the changed ones, unless
   *  `full_pass`. */
  devices_evaluated: number;
  devices_moved: number;
  /** Devices with no inventory document. No rule can ever claim one, so an
   *  operator not told this concludes their rule is wrong. */
  devices_without_inventory: number;
  /** The sweep's beat interval in seconds -- the lag a membership change
   *  costs. THE SERVER'S CONSTANT, not a literal: a number copied into a
   *  component drifts from the schedule with nothing noticing. */
  interval_seconds: number;
}

export interface DeviceGroupPage {
  items: DeviceGroup[];
  total: number;
  /** Devices in NO group -- the population every policy table's org default
   *  governs, and a number no page of groups can carry. */
  ungrouped_device_count: number;
  /** The membership sweep's last run, or `null` if it has never run for this
   *  organization. `null` MUST NOT RENDER AS "just now": an organization whose
   *  first sweep has not landed and one whose worker died are different
   *  sentences. */
  evaluation: MembershipEvaluation | null;
}

/** Everything still pointing at one group. An archive is refused while any of
 *  these is non-zero, and each one is somewhere the operator has to go. */
export interface DeviceGroupReferences {
  devices: number;
  child_groups: number;
  policies: number;
  patch_policies: number;
  backup_policies: number;
  remote_policies: number;
  health_assignments: number;
  credentials: number;
  // No `role_grants`: a role grant is scoped to a client, not to a group,
  // since the MSP clients model (spec §2.1), so none can hold a group.
}

/**
 * The five comparisons a membership rule may use, and nothing else.
 *
 * CLOSED ON BOTH SIDES. `app/modules/inventory/rules.py` holds the same five in
 * the table its evaluator dispatches on, and a Python test pins its `Literal`
 * to that table in both directions. A sixth here would be a 422, not a feature.
 */
export type RuleOp = "eq" | "ne" | "contains" | "gt" | "lt";

/** One condition. All of a document's conditions must hold. */
export interface RuleCondition {
  /** A flat fact name, `facts.<key>`, or `software:<name>`. */
  fact: string;
  op: RuleOp;
  value: string | number | boolean;
}

export interface GroupRules {
  all: RuleCondition[];
}

export interface DeviceGroupDetail {
  group: DeviceGroup;
  /** The membership rules, or null when this group is hand-curated. The paged
   *  list route deliberately does not carry them. */
  rules: GroupRules | null;
  references: DeviceGroupReferences;
}

export interface DeviceGroupCreate {
  name: string;
  /**
   * The client whose devices the group organises (MSP clients spec §1.5).
   * Required on create and fixed for the group's life, so it is not part of
   * `DeviceGroupUpdate`: the server's update model has no such field.
   */
  client_id: string;
  description?: string;
  parent_group_id?: string | null;
  precedence?: number;
  /** Null for a hand-curated group. A group is one or the other, never both:
   *  a rule CLAIMS a device rather than joining it, so a hand placement inside
   *  a rule-driven group is an edit the next re-evaluation reverts. */
  rules?: GroupRules | null;
}

export type DeviceGroupUpdate = Partial<Omit<DeviceGroupCreate, "client_id">>;

/** One device a candidate rule would claim, and where it is now. */
export interface RuleMatch {
  device_id: string;
  hostname: string | null;
  /** Null means the ORGANISATION DEFAULT, which is a policy set rather than an
   *  absence -- every patch, backup, remote and health resolver reads
   *  `device_group_id IS NULL` as the fallback. */
  current_group_id: string | null;
  current_group_name: string | null;
}

/** What a candidate rule reaches, before it reaches it. */
export interface RulePreview {
  matched: number;
  /** Active devices with no inventory document. NO rule can ever match these,
   *  because the evaluator reads collected documents and never device rows. */
  devices_without_inventory: number;
  sample: RuleMatch[];
  sample_truncated: boolean;
}

export interface DeviceGroupMemberResult {
  device_id: string;
  moved: boolean;
  reason: string;
}

/**
 * Where a request goes, for the one thing `requestHeaders` decides beyond the
 * bearer and the language: whether it carries `X-Client-Id`.
 */
export interface RequestRoute {
  /** `GET`, `POST`, … */
  method: string;
  /** Relative to `/api/v1`. A query string is ignored. */
  path: string;
  /** The client chosen in the top bar, or `null` for every client. */
  client: string | null;
  /**
   * Global BY ITS BODY, which `globalRoutes.json` cannot see: it lists methods
   * and paths, never bodies. `POST /mcp/connections/{client_id}/revoke` is the
   * person's own with `scope: "me"`, and withdraws the app from the whole
   * organization with `scope: "everyone"` -- refused narrowed (403
   * ALL_CLIENTS_REQUIRED, `backend/tests/test_global_writes.py`). `true` never
   * carries `X-Client-Id`; set only by the `ApiClient` method that knows what
   * its body says (`request()`'s `global`).
   */
  global?: boolean;
}

/**
 * The one place request headers are built: bearer, language and client.
 *
 * EVERY fetch in the console goes through this, including the raw ones that
 * bypass `request()` for binary bodies. A path that builds its own `Headers`
 * sends no `Accept-Language`, so the server answers it in English whatever the
 * person chose; `api.language.test.ts` fails the build on such a path.
 *
 * `X-Client-Id` (phase 3 design §1.2) names the client chosen in the top bar,
 * EXCEPT on a route of global configuration (`globalRoutes.json`, generated
 * from `backend/tests/test_global_writes.py`): narrowed, those answer 403.
 * Decided here or nowhere: whatever a caller put in `extra` is dropped first,
 * because a second copy is a 400 and one on a global route a 403.
 * `api.clientScope.test.ts` fails the build on an `ApiClient` call that does
 * not say where it goes.
 */
export function requestHeaders(
  token: string | null | undefined,
  extra?: HeadersInit,
  route?: RequestRoute,
): Headers {
  const headers = new Headers(extra);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  headers.set("Accept-Language", getLanguage());
  headers.delete("X-Client-Id");
  if (route?.client && route.global !== true) {
    const path = route.path.split("?")[0] ?? route.path;
    if (!isGlobalRoute(route.method, path)) headers.set("X-Client-Id", route.client);
  }
  return headers;
}

export class ApiClient {
  constructor(
    private readonly getToken: () => string | null,
    private readonly onUnauthorized: () => void,
    private readonly base: string = "/api/v1",
    /**
     * The client chosen in the top bar (`lib/clientChoice.ts`), read on EVERY
     * request: a choice made after this client was built still reaches the
     * next request. `null` is every client. The default sends no header,
     * which is what a test or a caller with no selector wants.
     */
    private readonly getClient: () => string | null = () => null,
  ) {}

  /**
   * What `requestHeaders` needs to know about one request to this API.
   * `global` is a request global by its body (`RequestRoute.global`); a
   * request global by its path needs nothing here, `globalRoutes.json` says so.
   */
  private route(method: string | undefined, path: string, global = false): RequestRoute {
    return { method: (method ?? "GET").toUpperCase(), path, client: this.getClient(), global };
  }

  private async request<T>(
    path: string,
    init: RequestInit = {},
    { global = false }: { global?: boolean } = {},
  ): Promise<T> {
    const headers = requestHeaders(this.getToken(), init.headers, this.route(init.method, path, global));
    headers.set("Content-Type", "application/json");
    const response = await fetch(`${this.base}${path}`, { ...init, headers });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  /* ---------------------------------------------------------- device groups */

  /**
   * This org's device groups, in the `(precedence, name)` order the ingest
   * path uses to decide which membership rule wins.
   *
   * THE CALLER MUST NOT RE-SORT. `ORDER BY` runs in the database's collation
   * and `deploy/` sets none, so a console sorting client-side with
   * `localeCompare` can disagree with the evaluator about which rule claims a
   * device -- which is the console answering a question the server decides
   * differently.
   */
  listDeviceGroups(params: { include_archived?: boolean } = {}): Promise<DeviceGroupPage> {
    const query = new URLSearchParams();
    if (params.include_archived) query.set("include_archived", "true");
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<DeviceGroupPage>(`/device-groups${suffix}`);
  }

  /** One group, live or archived, with every live reference counted. */
  getDeviceGroup(id: string): Promise<DeviceGroupDetail> {
    return this.request<DeviceGroupDetail>(`/device-groups/${id}`);
  }

  createDeviceGroup(body: DeviceGroupCreate): Promise<DeviceGroup> {
    return this.request<DeviceGroup>("/device-groups", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateDeviceGroup(id: string, body: DeviceGroupUpdate): Promise<DeviceGroup> {
    return this.request<DeviceGroup>(`/device-groups/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  /**
   * Archive a group. The server answers 409 with a `blocked_by` body while
   * anything still points at it; the caller renders those as places to go
   * rather than as a blob.
   */
  archiveDeviceGroup(id: string): Promise<void> {
    return this.request<void>(`/device-groups/${id}`, { method: "DELETE" });
  }

  unarchiveDeviceGroup(id: string): Promise<DeviceGroup> {
    return this.request<DeviceGroup>(`/device-groups/${id}/unarchive`, { method: "POST" });
  }

  /** Per-id results, never a batch failure: one stale entry in a selection
   *  must not silently discard the rest. */
  addDeviceGroupMembers(
    id: string,
    deviceIds: readonly string[],
  ): Promise<{ results: DeviceGroupMemberResult[] }> {
    return this.request<{ results: DeviceGroupMemberResult[] }>(
      `/device-groups/${id}/members`,
      { method: "POST", body: JSON.stringify({ device_ids: deviceIds }) },
    );
  }

  /**
   * Dry-run a rules document: what would it claim, and what cannot be claimed.
   *
   * WHY THE CONSOLE CALLS THIS ON EVERY SETTLED EDIT. A group is what a
   * deployment's `target_id` takes, so a rule matching more machines than its
   * author intended is a way to aim a signed job at machines nobody chose.
   * Re-evaluation happens in a background sweep, so nothing moves at the
   * moment of saving -- which is exactly why the author has to be shown the
   * reach before they save rather than after.
   *
   * It takes `devices.manage`, not `devices.read`: answering it is the
   * authoring act.
   */
  previewDeviceGroupRules(
    rules: GroupRules,
    groupId: string | null,
    /**
     * The client of a group NOT SAVED YET (MSP clients spec §1.5): a preview
     * runs over one client's devices, as the sweep claims them. A saved
     * group's preview uses the group's own client, so this is `null` then;
     * the server refuses a preview that names neither.
     */
    clientId: string | null,
  ): Promise<RulePreview> {
    return this.request<RulePreview>("/device-groups/preview", {
      method: "POST",
      body: JSON.stringify({ rules, group_id: groupId, client_id: clientId }),
    });
  }

  removeDeviceGroupMember(id: string, deviceId: string): Promise<void> {
    return this.request<void>(`/device-groups/${id}/members/${deviceId}`, {
      method: "DELETE",
    });
  }

  me(): Promise<Me> {
    return this.request<Me>("/me");
  }

  /** The caller's own organization. Any authenticated operator may read it. */
  getCurrentOrg(): Promise<CurrentOrg> {
    return this.request<CurrentOrg>("/orgs/current");
  }

  /**
   * The clients of the request's scope, archived ones left out
   * (`clients.read`). The Users tab's grant scope picks from these.
   */
  listClients(params: { include_archived?: boolean } = {}): Promise<ClientOut[]> {
    return this.request<ClientOut[]>(
      params.include_archived ? "/clients?include_archived=true" : "/clients",
    );
  }

  /** A client's sites, archived ones left out (`clients.read`): IPAM's site pickers. */
  listClientSites(clientId: string): Promise<ClientSiteOut[]> {
    return this.request<ClientSiteOut[]>(`/clients/${encodeURIComponent(clientId)}/sites`);
  }

  /**
   * Update the caller's own organization. Requires `orgs.manage` server-side;
   * the frontend never gates security, so hiding the form is presentation
   * only and the server answers 403 regardless of what the console shows.
   */
  updateCurrentOrg(body: OrgUpdateRequest): Promise<CurrentOrg> {
    return this.request<CurrentOrg>("/orgs/current", {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  /**
   * The caller's own profile fields (spec §7.3).
   *
   * NO `user_id` GOES IN THE BODY AND THERE IS NO BY-ID VARIANT. The server
   * resolves the target from the validated token, so this client cannot name
   * one — the same rule `updateCurrentOrg` follows, and the reason an
   * administrator cannot escalate through a self-service form.
   */
  updateMe(body: Partial<MeProfileUpdate>): Promise<Me> {
    return this.request<Me>("/me", { method: "PATCH", body: JSON.stringify(body) });
  }

  // ------------------------------------------------------------------ email

  /**
   * How this platform sends mail. `orgs.manage` server-side.
   *
   * `configured: false` is an ANSWER, not an error: it is the state this
   * platform ships in, and it is what the Users tab's banner reads.
   */
  getEmailSettings(): Promise<EmailSettingsOut> {
    return this.request<EmailSettingsOut>("/settings/email");
  }

  /**
   * Save the relay settings.
   *
   * THE ONE REQUEST IN THIS CLIENT THAT CARRIES A PASSWORD, and only when the
   * operator typed one. It is written straight through to the identity
   * provider and is never stored by this platform or returned by any response —
   * so `EmailSettingsOut` has no field that could echo it back.
   */
  updateEmailSettings(body: EmailSettingsUpdate): Promise<EmailSettingsOut> {
    return this.request<EmailSettingsOut>("/settings/email", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  /**
   * Send a test message to the saved sending address, which comes back as
   * `recipient`.
   *
   * NO BODY, deliberately: the caller never chooses the destination. A
   * test-send that accepted an address would be an authenticated
   * mail-sending primitive aimed at anyone, from the organization's own
   * domain.
   */
  sendTestEmail(body: TestSendIn = {}): Promise<TestSendOut> {
    return this.request<TestSendOut>("/settings/email/test", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // ------------------------------------------------------------------ roles

  /**
   * Every permission code the platform defines.
   *
   * Served from `app.core.rbac.PERMISSIONS` — the same dict `has_permission`
   * validates against — so the console and the enforcement cannot disagree.
   * Nothing in this repository re-lists these codes, deliberately.
   */
  listPermissions(): Promise<PermissionCatalogueOut> {
    return this.request<PermissionCatalogueOut>("/permissions");
  }

  listRoles(): Promise<RoleListOut> {
    return this.request<RoleListOut>("/roles");
  }

  createRole(body: RoleCreate): Promise<RoleOut> {
    return this.request<RoleOut>("/roles", { method: "POST", body: JSON.stringify(body) });
  }

  updateRole(roleId: string, body: RoleUpdate): Promise<RoleOut> {
    return this.request<RoleOut>(`/roles/${roleId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  /**
   * Replace the role's COMPLETE code set — the matrix is a set, not a log.
   *
   * The response's `role_id` may differ from the argument: editing a
   * platform-wide role copies it into this organization first (spec §1.3).
   * Callers address the returned id from then on.
   */
  setRolePermissions(roleId: string, codes: string[]): Promise<RolePermissionsOut> {
    return this.request<RolePermissionsOut>(`/roles/${roleId}/permissions`, {
      method: "PUT",
      body: JSON.stringify({ codes }),
    });
  }

  deleteRole(roleId: string): Promise<void> {
    return this.request<void>(`/roles/${roleId}`, { method: "DELETE" });
  }

  // ------------------------------------------------------------------ users

  listUsers(): Promise<UserListOut> {
    return this.request<UserListOut>("/users");
  }

  getUserEffectivePermissions(userId: string): Promise<EffectivePermissionsOut> {
    return this.request<EffectivePermissionsOut>(`/users/${userId}/effective-permissions`);
  }

  createUser(body: UserCreate): Promise<CreatedUserOut> {
    return this.request<CreatedUserOut>("/users", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  assignUserRole(userId: string, body: RoleAssign): Promise<RoleAssignmentOut> {
    return this.request<RoleAssignmentOut>(`/users/${userId}/roles`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /** Addressed to the ASSIGNMENT, not the role — the same role may be held twice. */
  removeUserRole(userId: string, assignmentId: string): Promise<void> {
    return this.request<void>(`/users/${userId}/roles/${assignmentId}`, { method: "DELETE" });
  }

  /** Two routes, one method: `/disable` and `/enable` differ only in direction. */
  setUserEnabled(userId: string, enabled: boolean): Promise<UserOut> {
    return this.request<UserOut>(`/users/${userId}/${enabled ? "enable" : "disable"}`, {
      method: "POST",
    });
  }

  /**
   * Edit a client's contact, or switch it off and on (`PATCH /contacts/{id}`,
   * `clients.manage`). `/users` is staff-only: a contact's sign-in is turned
   * off here.
   */
  updateContact(contactId: string, body: ContactUpdate): Promise<ContactOut> {
    return this.request<ContactOut>(`/contacts/${contactId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  /**
   * Ask Keycloak to email an `UPDATE_PASSWORD` link.
   *
   * NO PASSWORD TRANSITS ITMS (invariant 3, spec §2.2). There is no argument to
   * this method and no credential in its result — the response carries `sent`
   * and nothing else.
   */
  resetUserPassword(userId: string): Promise<MailResultOut> {
    return this.request<MailResultOut>(`/users/${userId}/reset-password`, { method: "POST" });
  }

  resendUserEnrolment(userId: string): Promise<MailResultOut> {
    return this.request<MailResultOut>(`/users/${userId}/resend-enrolment`, { method: "POST" });
  }

  /** Removes the Keycloak identity. The ITMS row is retained, marked `deleted`. */
  deleteUser(userId: string): Promise<void> {
    return this.request<void>(`/users/${userId}`, { method: "DELETE" });
  }

  /**
   * The fleet list. **PAGING, FILTERING AND COUNTING HAPPEN ON THE SERVER.**
   *
   * This took no arguments and returned one capped page, and the console then
   * computed the search, the provenance filter and every tab count in the
   * browser over that page. Since the operator's ruling of 2026-08-26 a
   * `net.sweep` records every host it finds, so a /24 adds ~200 rows in one act
   * — past the page limit the counts were wrong, the search missed, and the
   * provenance filter reported nothing on an estate full of swept rows. All
   * three render as a correct-looking empty answer.
   *
   * EVERY ARGUMENT IS OPTIONAL and the no-argument call is unchanged, which is
   * what the other four callers (`deploy`, `service-desk`, `enrollment`, the
   * job dispatch picker) rely on.
   */
  listDevices(params: DeviceListParams = {}): Promise<DevicePage> {
    const query = new URLSearchParams();
    if (params.q) query.set("q", params.q);
    if (params.state && params.state !== "all") query.set("state", params.state);
    if (params.provenance) query.set("provenance", params.provenance);
    if (params.osFamily) query.set("os_family", params.osFamily);
    if (params.clientId) query.set("client_id", params.clientId);
    if (params.limit != null) query.set("limit", String(params.limit));
    if (params.offset) query.set("offset", String(params.offset));
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<DevicePage>(`/devices${suffix}`);
  }

  getDevice(id: string): Promise<Device> {
    return this.request<Device>(`/devices/${id}`);
  }

  /** `devices.review`. From now on it is an ordinary device. */
  approveDevice(id: string): Promise<Device> {
    return this.request<Device>(`/devices/${id}/review/approve`, { method: "POST" });
  }

  /** `devices.review`. Its agent is told to uninstall itself; its certificate is revoked. */
  rejectDevice(id: string): Promise<DeviceRejected> {
    return this.request<DeviceRejected>(`/devices/${id}/review/reject`, { method: "POST" });
  }

  /**
   * `devices.review`. A technician removed a rejected computer's agent by
   * hand; the row leaves Waiting for review. 409 for any row whose rejection
   * did not leave the agent behind.
   */
  acknowledgeDeviceRemoval(id: string): Promise<void> {
    return this.request<void>(`/devices/${id}/review/acknowledge-removal`, { method: "POST" });
  }

  /**
   * `devices.review`. Approves the LISTED computers that are still waiting; a
   * computer that joined after the list was shown stays waiting and is
   * counted in `still_waiting`.
   */
  approveAllDevices(body: ApproveAllRequest): Promise<ApproveAllResult> {
    return this.request<ApproveAllResult>("/devices/review/approve-all", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  listAssetCandidates(): Promise<AssetCandidatePage> {
    return this.request<AssetCandidatePage>("/devices/candidates");
  }

  /**
   * `client_id` names whose candidates these are; each machine is filed under
   * its subnet's client and site, and a subnet without a site refuses it.
   */
  promoteAssetCandidates(
    body: { client_id: string; addresses: string[] } & AssetInput,
  ): Promise<{ results: PromoteResult[] }> {
    return this.request<{ results: PromoteResult[] }>("/devices/candidates/promote", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  listAssetSuggestions(): Promise<{ items: AssetSuggestion[] }> {
    return this.request<{ items: AssetSuggestion[] }>("/devices/suggestions");
  }

  resolveAssetSuggestion(
    id: string,
    resolution: "linked" | "rejected",
  ): Promise<{ items: AssetSuggestion[] }> {
    return this.request<{ items: AssetSuggestion[] }>(
      `/devices/suggestions/${id}/resolve`,
      { method: "POST", body: JSON.stringify({ resolution }) },
    );
  }

  createAsset(body: AssetCreateInput): Promise<Device> {
    return this.request<Device>("/devices", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateAsset(id: string, body: AssetInput): Promise<Device> {
    return this.request<Device>(`/devices/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  /**
   * One page of the job queue, in a stated order.
   *
   * This sent no query string at all, so the console took the server's
   * default `limit=100` while its own KPI tile showed the true total
   * (finding B46). The closeout deliberately did NOT add a client-side sort
   * on top of that: sorting a silently truncated page by "oldest first"
   * shows the newest hundred re-sorted and labels them oldest, which is
   * worse than no sort. So paging and ordering arrive together, and both are
   * the server's — `order` is `newest` (the long-standing default) or
   * `oldest`, and anything else is a 422 rather than a silent fallback.
   *
   * With no arguments the URL is unchanged, so nothing that already calls it
   * gets a different page.
   */
  listJobs(params: JobListParams = {}): Promise<Page<Job>> {
    const query = new URLSearchParams();
    if (params.limit != null) query.set("limit", String(params.limit));
    if (params.offset != null) query.set("offset", String(params.offset));
    if (params.order != null) query.set("order", params.order);
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<Page<Job>>(`/jobs${suffix}`);
  }

  getJob(id: string): Promise<Job> {
    return this.request<Job>(`/jobs/${id}?include=logs`);
  }

  dispatchJob(body: JobDispatchRequest): Promise<Job> {
    return this.request<Job>("/jobs", { method: "POST", body: JSON.stringify(body) });
  }

  /**
   * Withdraw a job that has not been sent to its device yet.
   *
   * The server refuses anything else with a 409 naming the state, and refuses
   * a caller who could not have DISPATCHED this job type with a 403 -- the
   * console gates neither, because authorization is server-side and a console
   * that hid the button would only be guessing at the answer.
   */
  cancelJob(id: string): Promise<Job> {
    return this.request<Job>(`/jobs/${id}/cancel`, { method: "POST" });
  }

  /**
   * Re-run a finished job. Resolves to the NEW job, not the old one.
   *
   * A retry is a fresh dispatch server-side -- new id, new nonce, a signature
   * over different bytes -- so the response is a different `Job` and the
   * caller should follow it rather than the row they clicked.
   */
  retryJob(id: string): Promise<Job> {
    return this.request<Job>(`/jobs/${id}/retry`, { method: "POST" });
  }

  getDeviceInventory(deviceId: string): Promise<DeviceInventory> {
    return this.request<DeviceInventory>(`/devices/${deviceId}/inventory`);
  }

  getDeviceChanges(deviceId: string): Promise<Page<InventoryChange>> {
    return this.request<Page<InventoryChange>>(`/devices/${deviceId}/inventory/changes`);
  }

  searchSoftware(name: string): Promise<Page<SoftwareAggregate>> {
    const query = name ? `?name=${encodeURIComponent(name)}` : "";
    return this.request<Page<SoftwareAggregate>>(`/software${query}`);
  }

  softwareDevices(name: string): Promise<SoftwareDevice[]> {
    return this.request<SoftwareDevice[]>(`/software/${encodeURIComponent(name)}/devices`);
  }

  /**
   * The whole estate as a spreadsheet workbook.
   *
   * NOT ROUTED THROUGH `request` -- the body is a binary `.xlsx`, not JSON --
   * and A FETCH RATHER THAN A LINK, which is the entire point of the method.
   * Both export buttons in this console were `<a href="/api/v1/export/…"
   * download>`, and a browser following a link of its own accord sends no
   * `Authorization` header: the route answered 401 and the browser saved the
   * refusal body under the name the URL ended in. "Export inventory exports a
   * .json instead of an .xlsx" is that, reported from the deployed console.
   *
   * Mirrors `exportPatchReport`/`exportReport`, which had already reached the
   * same conclusion on their own routes.
   */
  exportInventory(dataset: "devices" | "software"): Promise<Blob> {
    return this.fetchFile(`/export/${dataset}.xlsx`);
  }

  /**
   * SOC 2 access evidence for the window and filters on screen.
   *
   * Same story as `exportInventory`: this was an `<a href>` built from a
   * template, which is why the sweep for `href="/api` missed it while
   * catching the other two. `audit.read` gates the route, and the server
   * writes the audit event -- the console's job is only to arrive
   * authenticated, which a link never did.
   */
  exportAccessEvidence(params: {
    from_ts: string;
    username?: string;
    event_type?: string;
  }): Promise<Blob> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value != null && value !== "") query.set(key, value);
    }
    return this.fetchFile(`/reports/access.csv?${query.toString()}`);
  }

  /**
   * One authenticated GET whose body is a FILE rather than JSON.
   *
   * Not `request<T>`, which parses JSON and would choke on a workbook. The
   * 401 branch is the same one `request` takes, so an expired session still
   * ends in the sign-in flow rather than in a downloaded error page --
   * which is precisely what the anchors this replaces produced.
   */
  private async fetchFile(path: string): Promise<Blob> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("GET", path));
    const response = await fetch(`${this.base}${path}`, { headers });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return response.blob();
  }

  listSecurityEvents(params: SecurityEventFilters): Promise<Page<SecurityEvent>> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<Page<SecurityEvent>>(`/security-events${suffix}`);
  }

  getSecurityEvent(id: string): Promise<SecurityEventDetail> {
    return this.request<SecurityEventDetail>(`/security-events/${id}`);
  }

  listArticles(params: {
    category?: string;
    target_type?: string;
    target_id?: string;
    include_archived?: boolean;
  } = {}): Promise<Page<DocArticleListItem>> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<Page<DocArticleListItem>>(`/docs/articles${suffix}`);
  }

  getArticle(id: string): Promise<DocArticle> {
    return this.request<DocArticle>(`/docs/articles/${id}`);
  }

  createArticle(body: DocArticleInput & NewDocClient): Promise<DocArticle> {
    return this.request<DocArticle>("/docs/articles", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateArticle(id: string, body: DocArticleInput): Promise<DocArticle> {
    return this.request<DocArticle>(`/docs/articles/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  archiveArticle(id: string): Promise<{ archived: boolean }> {
    return this.request<{ archived: boolean }>(`/docs/articles/${id}/archive`, {
      method: "POST",
    });
  }

  setArticlePortalVisibility(id: string, portalVisible: boolean): Promise<{ portal_visible: boolean }> {
    return this.request<{ portal_visible: boolean }>(`/docs/articles/${id}/portal-visibility`, {
      method: "PUT",
      body: JSON.stringify({ portal_visible: portalVisible }),
    });
  }

  articleVersions(id: string): Promise<DocVersion[]> {
    return this.request<DocVersion[]>(`/docs/articles/${id}/versions`);
  }

  addArticleLink(id: string, link: { target_type: string; target_id: string }): Promise<DocLink> {
    return this.request<DocLink>(`/docs/articles/${id}/links`, {
      method: "POST",
      body: JSON.stringify(link),
    });
  }

  removeArticleLink(id: string, linkId: string): Promise<{ removed: boolean }> {
    return this.request<{ removed: boolean }>(`/docs/articles/${id}/links/${linkId}`, {
      method: "DELETE",
    });
  }

  listDocRecords(recordType?: string): Promise<Page<DocRecordOut>> {
    const suffix = recordType ? `?record_type=${recordType}` : "";
    return this.request<Page<DocRecordOut>>(`/docs/records${suffix}`);
  }

  createDocRecord(
    body: {
      record_type: string;
      name: string;
      fields: Record<string, unknown>;
    } & NewDocClient,
  ): Promise<DocRecordOut> {
    return this.request<DocRecordOut>("/docs/records", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  searchDocs(q: string): Promise<DocSearchHit[]> {
    return this.request<DocSearchHit[]>(`/docs/search?q=${encodeURIComponent(q)}`);
  }

  // --- Stored documents (backend/app/modules/documentation/documents_api.py)
  //
  // Eleven routes under /api/v1/docs/documents, read against
  // `documents_api.py` directly. `docs.read` for the reads (including the
  // bytes), `docs.manage` for the mutations, `docs.document.purge` for the one
  // irreversible act -- all enforced server-side (invariant #6); nothing below
  // assumes any of them.
  //
  // NO SHAPE HERE CARRIES AN OBJECT KEY, and none can: the server never puts
  // one in a response body (`documents_api.py`'s module docstring, F1b). The
  // client therefore has no way to name a stored object, which is one of the
  // four mechanisms standing in for the row-level security an object store
  // cannot have (spec-attachments.md §3.3).
  //
  // AND NO PRESIGNED URL IS EVER MINTED OR FETCHED (§0.3). MinIO is routed on
  // the console's OWN hostname in `deploy/docker-compose.yml`, so a presigned
  // GET of an uploaded HTML file would be an unauthenticated, same-origin,
  // script-executing URL for the whole tenant. Bytes come back through
  // `downloadDocumentVersion` below, on the caller's own token, and nowhere
  // else.

  listDocuments(
    params: {
      category?: string;
      target_type?: string;
      target_id?: string;
      include_archived?: boolean;
      limit?: number;
      offset?: number;
    } = {},
  ): Promise<Page<DocDocumentListItem>> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<Page<DocDocumentListItem>>(`/docs/documents${suffix}`);
  }

  getDocument(id: string): Promise<DocDocument> {
    return this.request<DocDocument>(`/docs/documents/${id}`);
  }

  createDocument(body: DocDocumentInput & NewDocClient): Promise<DocDocument> {
    return this.request<DocDocument>("/docs/documents", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateDocument(id: string, body: DocDocumentInput): Promise<DocDocument> {
    return this.request<DocDocument>(`/docs/documents/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  archiveDocument(id: string): Promise<{ archived: boolean }> {
    return this.request<{ archived: boolean }>(`/docs/documents/${id}/archive`, {
      method: "POST",
    });
  }

  documentVersions(id: string): Promise<DocDocumentVersion[]> {
    return this.request<DocDocumentVersion[]>(`/docs/documents/${id}/versions`);
  }

  // The version route takes raw bytes as the request body
  // (`documents_api.py::upload_version`: `read_bounded_body(request, ...)`),
  // not JSON -- bypasses `request<T>`'s JSON.stringify/Content-Type handling,
  // mirroring `uploadTicketAttachment`'s binary-upload pattern. `filename` is
  // a query parameter, sanitized server-side before it ever reaches the object
  // key.
  //
  // THE `Content-Type` HEADER BELOW IS NOT A DECLARATION OF THE FILE'S TYPE
  // and cannot become one. The server sniffs the leading bytes and ignores
  // every header (`filetypes.py::sniff`); "a file whose content-type lies" is
  // a sentence that cannot be formed against this route (§0.5). It is set
  // only so proxies do not attempt a transformation on the body.
  async uploadDocumentVersion(id: string, file: File): Promise<DocDocumentVersion> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/docs/documents/${id}/versions`));
    headers.set("Content-Type", "application/octet-stream");
    const response = await fetch(
      `${this.base}/docs/documents/${id}/versions?filename=${encodeURIComponent(file.name)}`,
      { method: "POST", headers, body: file },
    );
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return (await response.json()) as DocDocumentVersion;
  }

  // The bytes, on the caller's own token. Not routed through `request` since
  // the body is binary, not JSON; mirrors `downloadTicketAttachment`.
  //
  // WHAT COMES BACK IS ALWAYS `application/octet-stream`, whatever the file
  // is (§5.1, wall 1) -- so the Blob this returns carries THAT type and the
  // caller must never render from it directly. The console builds its own
  // Blob from the render map's constant instead (`modules/docs/viewer.ts`),
  // which is why this returns the response Blob rather than a typed one.
  //
  // EVERY CALL IS AN AUDITED DOWNLOAD (§12.3, D10). The server cannot tell
  // viewing from saving -- the bytes left the control plane either way -- so
  // opening the viewer writes `docs.document_downloaded` exactly as saving the
  // file does. Nothing in this console may imply otherwise.
  async downloadDocumentVersion(id: string, version: number): Promise<Blob> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/docs/documents/${id}/versions/${version}/content`));
    const response = await fetch(
      `${this.base}/docs/documents/${id}/versions/${version}/content`,
      { headers },
    );
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return response.blob();
  }

  purgeDocumentVersion(id: string, version: number): Promise<{ purged: boolean }> {
    return this.request<{ purged: boolean }>(
      `/docs/documents/${id}/versions/${version}/purge`,
      { method: "POST" },
    );
  }

  addDocumentLink(
    id: string,
    link: { target_type: string; target_id: string },
  ): Promise<DocLink> {
    return this.request<DocLink>(`/docs/documents/${id}/links`, {
      method: "POST",
      body: JSON.stringify(link),
    });
  }

  removeDocumentLink(id: string, linkId: string): Promise<{ removed: boolean }> {
    return this.request<{ removed: boolean }>(`/docs/documents/${id}/links/${linkId}`, {
      method: "DELETE",
    });
  }

  listCredentials(): Promise<Credential[]> {
    return this.request<Credential[]>("/credentials");
  }

  // ------------------------------------------------------------------------
  // Agent push install, cycle B1 (`CLAUDE.md` invariant 2a).
  //
  // SEVEN ROUTES, AND NONE OF THEM PUSHES ANYTHING. Two of the four bounds on
  // where this platform may present an administrative credential live behind
  // these calls; the request route runs the whole refusal ladder and then
  // answers 501, because the job type, the signing and the agent capability
  // are cycle B2 and are not built.
  //
  // NO RESPONSE HERE CARRIES A SECRET, A CIPHERTEXT OR A VAULT REFERENCE, and
  // none can: the server's response models have no such field.
  // ------------------------------------------------------------------------

  listPushAuthorizations(includeRevoked = false): Promise<PushAuthorizationOut[]> {
    const suffix = includeRevoked ? "?include_revoked=true" : "";
    return this.request<PushAuthorizationOut[]>(`/agent-push/authorizations${suffix}`);
  }

  // HIGH PRIVILEGE (`agent.push.scope.manage`): this grant decides where the
  // platform may present administrative credentials to machines nobody has
  // enrolled and nothing can authenticate.
  createPushAuthorization(body: PushAuthorizationCreate): Promise<PushAuthorizationOut> {
    return this.request<PushAuthorizationOut>("/agent-push/authorizations", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // Revocation STAMPS; the row stays, so the grant remains auditable.
  revokePushAuthorization(id: string): Promise<void> {
    return this.request<void>(`/agent-push/authorizations/${id}`, { method: "DELETE" });
  }

  listPushCredentialScopes(includeUnbound = false): Promise<PushCredentialScopeOut[]> {
    const suffix = includeUnbound ? "?include_unbound=true" : "";
    return this.request<PushCredentialScopeOut[]>(`/agent-push/credential-scopes${suffix}`);
  }

  // The credential itself carries where it may be presented. Refused for any
  // credential whose type is not `push_install`.
  bindPushCredentialScope(body: PushCredentialScopeBind): Promise<PushCredentialScopeOut> {
    return this.request<PushCredentialScopeOut>("/agent-push/credential-scopes", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // Unbinding STAMPS; the row stays, so "which addresses was this credential
  // bound to when it was used" survives somebody narrowing the scope.
  unbindPushCredentialScope(id: string): Promise<void> {
    return this.request<void>(`/agent-push/credential-scopes/${id}`, { method: "DELETE" });
  }

  // Every target this org has probed (`agent.push.run`). An SSH row is a host
  // key; a Windows row is the credential-free WinRM probe's readiness (design
  // §2.4). Public material only -- never a secret. The "Before a push can
  // run" panel reads it here; the push pre-flight panel reads the same route
  // through the module's own fetch (`modules/agent-push/api.ts`).
  listPushHostKeys(): Promise<HostKeyOut[]> {
    return this.request<HostKeyOut[]>("/agent-push/host-keys");
  }

  // HIGH CONSEQUENCE (`agent.push.run`): the confirmed fingerprint becomes the
  // trust anchor every later SSH push to this host is verified against. The
  // server refuses anything but what the last probe observed.
  confirmPushHostKey(body: HostKeyConfirm): Promise<HostKeyOut> {
    return this.request<HostKeyOut>("/agent-push/host-keys/confirm", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // A probe (no credential presented) or an install, against 1..25 explicitly
  // named addresses (`agent.push.run` + `credentials.checkout`, server-side).
  // All-or-nothing: a 422 names every failing bound per address and creates
  // nothing. NEVER retried by the caller -- a retry across a batch is how a
  // domain account gets locked out (spec §5.3).
  requestPush(body: PushRequestCreate): Promise<PushDispatchOut> {
    return this.request<PushDispatchOut>("/agent-push/requests", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // Push runs (design 2026-09-30). Every route is `agent.push.run`; preview,
  // create and resume also need `credentials.checkout`, server-side. None is
  // ever retried by the caller.
  // The subnets a run with `credentialId` may be built from: its client's
  // active authorizations (MSP clients phase 2, Task 12).
  listPushRunPrefixes(credentialId: string): Promise<PushRunPrefixOut[]> {
    const query = new URLSearchParams({ credential_id: credentialId });
    return this.request<PushRunPrefixOut[]>(`/agent-push/runs/prefixes?${query.toString()}`);
  }

  previewPushRun(prefix: string, credentialId: string): Promise<PushRunPreviewOut> {
    const query = new URLSearchParams({ prefix, credential_id: credentialId });
    return this.request<PushRunPreviewOut>(`/agent-push/runs/preview?${query.toString()}`);
  }

  createPushRun(body: PushRunCreate): Promise<PushRunOut> {
    return this.request<PushRunOut>("/agent-push/runs", { method: "POST", body: JSON.stringify(body) });
  }

  listPushRuns(): Promise<PushRunOut[]> {
    return this.request<PushRunOut[]>("/agent-push/runs");
  }

  getPushRun(id: string): Promise<PushRunOut> {
    return this.request<PushRunOut>(`/agent-push/runs/${id}`);
  }

  listPushRunTargets(id: string, params: { offset: number; limit: number }): Promise<PushRunTargetPage> {
    const query = new URLSearchParams({ offset: String(params.offset), limit: String(params.limit) });
    return this.request<PushRunTargetPage>(`/agent-push/runs/${id}/targets?${query.toString()}`);
  }

  pausePushRun(id: string): Promise<PushRunOut> {
    return this.request<PushRunOut>(`/agent-push/runs/${id}/pause`, { method: "POST" });
  }

  resumePushRun(id: string): Promise<PushRunOut> {
    return this.request<PushRunOut>(`/agent-push/runs/${id}/resume`, { method: "POST" });
  }

  cancelPushRun(id: string): Promise<PushRunOut> {
    return this.request<PushRunOut>(`/agent-push/runs/${id}/cancel`, { method: "POST" });
  }

  createCredential(body: {
    name: string;
    type: string;
    secret: string;
    /** Required for a push credential (`push_install`, `windows_domain`): it has exactly one client. */
    client_id?: string;
  }): Promise<Credential> {
    return this.request<Credential>("/credentials", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // Returns the plaintext to the caller once; never cache this in query state.
  checkoutCredential(id: string, reason: string): Promise<{ secret: string }> {
    return this.request<{ secret: string }>(`/credentials/${id}/checkout`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
  }

  deleteCredential(id: string): Promise<{ deleted: boolean }> {
    return this.request<{ deleted: boolean }>(`/credentials/${id}`, { method: "DELETE" });
  }

  listRemoteSessions(params: RemoteSessionFilters = {}): Promise<Page<RemoteSession>> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<Page<RemoteSession>>(`/remote/sessions${suffix}`);
  }

  getRemoteSession(id: string): Promise<RemoteSession> {
    return this.request<RemoteSession>(`/remote/sessions/${id}`);
  }

  startRemoteSession(body: RemoteSessionCreateRequest): Promise<RemoteSessionCreateResponse> {
    return this.request<RemoteSessionCreateResponse>("/remote/sessions", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  stopRemoteSession(id: string): Promise<RemoteSession> {
    return this.request<RemoteSession>(`/remote/sessions/${id}/stop`, { method: "POST" });
  }

  // Streams the raw recording bytes; not routed through `request` since the
  // body is binary, not JSON. Never cached in query state as text.
  async getRemoteRecording(id: string): Promise<Blob> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/remote/sessions/${id}/recording`));
    const response = await fetch(`${this.base}/remote/sessions/${id}/recording`, { headers });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return response.blob();
  }

  /*
   * `listPatches` WAS HERE AND IS GONE, and the route it called is not.
   *
   * `GET /patch/patches` is a shipped, `patch.read`-gated v1 route and stays:
   * it is the only read that returns the raw catalogue, so a `patches` row no
   * device has ever reported a status for appears in no other response --
   * `GET /patch/updates` aggregates `device_patch_status`, so a catalogue
   * entry with no per-device rows is not in it at any value of
   * `outstanding_only`. `reporting/registry.py` maps the same table under the
   * same permission code.
   *
   * WHAT WENT WAS THE CONSOLE'S ABILITY TO CALL IT, because no screen wants
   * it. The one caller fetched the route's 500-row ceiling on every visit to
   * the fleet page to build a `patch_id -> identifier` map, rendered none of
   * the rows, and past the cap named an approval with a bare uuid;
   * `ApprovalOut.patch_identifier` put the name on the row and deleted the
   * caller. A client method nothing calls is not a smaller version of a
   * feature, it is an invitation to re-add the fetch -- and the type system is
   * now what refuses it. `modules/patch/types.ts` keeps `Patch` and
   * `PatchListResponse` because `backend/tests/test_patch_type_parity.py`
   * requires an interface for every shipped response model; that is a mirror
   * of the API, not a caller of it.
   */

  getDevicePatchStatus(deviceId: string): Promise<DeviceStatus> {
    return this.request<DeviceStatus>(`/patch/devices/${deviceId}/status`);
  }

  /** Whether winget / Homebrew could be scanned, per device count (`patch.read`). */
  listPatchAppSources(osFamily?: string): Promise<PatchAppSourceSummaryResponse> {
    const suffix = osFamily ? `?os_family=${encodeURIComponent(osFamily)}` : "";
    return this.request<PatchAppSourceSummaryResponse>(`/patch/app-sources${suffix}`);
  }

  /** Devices still owed a restart after patching (`patch.read`). */
  listPatchRestarts(): Promise<PatchRestartListResponse> {
    return this.request<PatchRestartListResponse>("/patch/restarts");
  }

  /**
   * The fleet update list: ONE ROW PER PATCH, aggregated across devices.
   *
   * `listPatches` above returns bare catalogue rows, so it cannot say the one
   * number that makes an update actionable -- how many machines are missing it
   * -- and the page that tried to say it rendered one row per (device, patch)
   * pair instead. Filtering, searching, sorting and paging are all the
   * server's here: the previous console did them over the fetched page, so a
   * search could not reach past the cap and answered "no patches match" about
   * rows it had never asked for.
   *
   * `severity` is REPEATED, not comma-joined -- FastAPI reads a repeatable
   * query parameter, and an empty array sends none, which the route reads as
   * "no severity filter".
   */
  listPatchUpdates(params: PatchUpdateFilters = {}): Promise<PatchUpdateListResponse> {
    const query = new URLSearchParams();
    const { severity, ...rest } = params;
    for (const value of severity ?? []) if (value) query.append("severity", value);
    for (const [key, value] of Object.entries(rest)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<PatchUpdateListResponse>(`/patch/updates${suffix}`);
  }

  /**
   * Which machines need attention, worst first -- ONE ROW PER DEVICE.
   *
   * This is what replaced reading `FleetReport.exposures` and grouping it in
   * the browser. That list is one entry per (device, patch) pair and uncapped,
   * so a single machine missing 300 packages arrived as 300 entries and the
   * page rendered every one of them under a hostname heading.
   */
  listPatchDeviceRollup(
    params: PatchDeviceRollupFilters = {},
  ): Promise<PatchDeviceRollupListResponse> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<PatchDeviceRollupListResponse>(`/patch/device-rollup${suffix}`);
  }

  /**
   * The caller's own dashboard arrangement. Auth-gated only, no permission
   * code: it writes one row and it is theirs (`api/v1/dashboard.py`).
   */
  getDashboardLayout(): Promise<DashboardLayoutResponse> {
    return this.request<DashboardLayoutResponse>("/dashboard/layout");
  }

  saveDashboardLayout(widgets: WidgetPlacement[]): Promise<DashboardLayoutResponse> {
    return this.request<DashboardLayoutResponse>("/dashboard/layout", {
      method: "PUT",
      body: JSON.stringify({ widgets }),
    });
  }

  /** Reset to the stock arrangement — deletes the row. 204, idempotent. */
  async resetDashboardLayout(): Promise<void> {
    await this.request<void>("/dashboard/layout", { method: "DELETE" });
  }

  /** The apply RUNS — what a dispatch actually did (`patch.read`). */
  listPatchRuns(params: PatchRunFilters = {}): Promise<PatchRunListResponse> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<PatchRunListResponse>(`/patch/runs${suffix}`);
  }

  /**
   * Book a restart for the device's next maintenance window (`patch.apply`).
   * Nothing is sent now; asking again while one is booked changes nothing.
   */
  requestDeviceRestart(deviceId: string): Promise<PatchRestartRequestResponse> {
    return this.request<PatchRestartRequestResponse>(`/patch/devices/${deviceId}/restart`, {
      method: "POST",
    });
  }

  triggerPatchScan(body: ScanRequest = {}): Promise<ScanResponse> {
    return this.request<ScanResponse>("/patch/scan", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /**
   * Install what is already approved, on these machines, now
   * (`POST /patch/apply`, `patch.apply`).
   *
   * THE ROUTE HAS SHIPPED SINCE THE MODULE WAS BUILT AND HAD NO CLIENT: until
   * the 2026-08-27 platform split there was no screen that dispatched an
   * install, because approving into a ring and letting the worker's window
   * sweep do the rest was the whole model. The Linux tab's unit of decision is
   * the machine, so it needs the one call that takes machines.
   *
   * WHAT COMES BACK IS ONE JOB PER ELIGIBLE DEVICE, not one per patch --
   * `dispatch_apply` batches every eligible patch on a device into a single
   * signed `patch.apply`. A device outside its maintenance window, or with
   * nothing approved for its ring, produces NO job and no error, so
   * `job_ids.length` is smaller than `device_ids.length` in the ordinary case
   * and the caller has to say so. See `applyOutcomeMessage`.
   */
  applyPatches(body: ApplyRequest = {}): Promise<ApplyResponse> {
    return this.request<ApplyResponse>("/patch/apply", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  approvePatch(body: ApprovalCreateRequest): Promise<PatchApproval> {
    return this.request<PatchApproval>("/patch/approvals", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /**
   * Clear a SELECTION of patches for one ring, in one decision
   * (`patch.approve`).
   *
   * Approval has always been fleet-wide — `patch_approvals` is
   * `(org_id, patch_id, ring_name)` and carries no device column — so what was
   * per-row was this console, which spent one POST and one confirmation dialog
   * on each catalogue line. The server still writes ONE audit event per
   * approval; the batch is a shape for the decision, never for the trail.
   *
   * A patch id that is not this org's refuses the WHOLE batch with 404, so a
   * refused call leaves nothing partly cleared.
   */
  bulkApprovePatches(body: BulkApprovalCreateRequest): Promise<BulkApprovalResponse> {
    return this.request<BulkApprovalResponse>("/patch/approvals/bulk", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /**
   * Every deployment ring in this org, with the devices each reaches
   * (`patch.read`).
   *
   * THE BLAST RADIUS OF AN APPROVAL, and it is not derivable here: it needs
   * `patch_policies` joined to `devices` through `device_groups` with the
   * org-default fallback, which is the same resolver approval itself uses — so
   * the number on screen and the machines flipped cannot disagree.
   */
  listPatchRings(): Promise<PatchRingListResponse> {
    return this.request<PatchRingListResponse>("/patch/rings");
  }

  /**
   * This org's approvals, newest first (`patch.read`).
   *
   * REVOKED ONES COME BACK TOO unless `revoked` is passed. That is the
   * route's behaviour and it is deliberate on both sides: approvals are
   * soft-revoked so the trail holds, so a caller that quietly asked for
   * `revoked=false` would be showing a different history from the audit log.
   */
  listPatchApprovals(params: ApprovalListFilters = {}): Promise<ApprovalListResponse> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<ApprovalListResponse>(`/patch/approvals${suffix}`);
  }

  /**
   * Withdraw an approval (`patch.approve`; 204, soft-revoke).
   *
   * The id this takes was unobtainable until `listPatchApprovals` existed —
   * the route has been in the module since it shipped and had never been
   * callable.
   */
  revokePatchApproval(approvalId: string): Promise<void> {
    return this.request<void>(`/patch/approvals/${approvalId}`, { method: "DELETE" });
  }

  listPatchPolicies(): Promise<PatchPolicy[]> {
    return this.request<PatchPolicy[]>("/patch/policies");
  }

  upsertPatchPolicy(body: PatchPolicyInput): Promise<PatchPolicy> {
    return this.request<PatchPolicy>("/patch/policies", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  getPatchFleetReport(): Promise<FleetReport> {
    return this.request<FleetReport>("/patch/report");
  }

  // Streams the CSV export; not routed through `request` since the body is
  // text/csv, not JSON. Mirrors getRemoteRecording's binary-download pattern.
  async exportPatchReport(): Promise<Blob> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("GET", "/patch/report/export"));
    const response = await fetch(`${this.base}/patch/report/export`, { headers });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return response.blob();
  }

  listPackages(): Promise<Package[]> {
    return this.request<Package[]>("/deploy/packages");
  }

  createPackage(body: PackageCreateRequest): Promise<Package> {
    return this.request<Package>("/deploy/packages", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // The artifact route takes raw bytes as the request body (see
  // upload_package_artifact in app/modules/deploy/api.py: `data = await
  // request.body()`), not JSON — bypass `request<T>`'s JSON.stringify/
  // Content-Type handling, mirroring getRemoteRecording's binary pattern but
  // for an upload instead of a download.
  async uploadPackageArtifact(packageId: string, file: File): Promise<Package> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/deploy/packages/${packageId}/artifact`));
    headers.set("Content-Type", "application/octet-stream");
    const response = await fetch(`${this.base}/deploy/packages/${packageId}/artifact`, {
      method: "POST",
      headers,
      body: file,
    });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return (await response.json()) as Package;
  }

  deletePackage(id: string): Promise<void> {
    return this.request<void>(`/deploy/packages/${id}`, { method: "DELETE" });
  }

  listDeployments(): Promise<Deployment[]> {
    return this.request<Deployment[]>("/deploy/deployments");
  }

  createDeployment(body: DeploymentCreateRequest): Promise<Deployment> {
    return this.request<Deployment>("/deploy/deployments", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  executeDeployment(id: string): Promise<DeploymentExecuteResponse> {
    return this.request<DeploymentExecuteResponse>(`/deploy/deployments/${id}/execute`, {
      method: "POST",
    });
  }

  getDeviceDeployments(deviceId: string): Promise<DeviceDeploymentHistory[]> {
    return this.request<DeviceDeploymentHistory[]>(`/deploy/devices/${deviceId}/deployments`);
  }

  listScripts(): Promise<ScriptListItem[]> {
    return this.request<ScriptListItem[]>("/deploy/scripts");
  }

  getScript(id: string): Promise<Script> {
    return this.request<Script>(`/deploy/scripts/${id}`);
  }

  authorScript(body: ScriptCreateRequest): Promise<Script> {
    return this.request<Script>("/deploy/scripts", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  editScript(id: string, body: ScriptUpdateRequest): Promise<Script> {
    return this.request<Script>(`/deploy/scripts/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  runScript(id: string, body: ScriptRunRequest): Promise<ScriptRunResponse> {
    return this.request<ScriptRunResponse>(`/deploy/scripts/${id}/run`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  listScriptPolicies(): Promise<ScriptPolicy[]> {
    return this.request<ScriptPolicy[]>("/deploy/script-policies");
  }

  createScriptPolicy(body: ScriptPolicyCreateRequest): Promise<ScriptPolicy> {
    return this.request<ScriptPolicy>("/deploy/script-policies", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateScriptPolicy(id: string, body: ScriptPolicyUpdateRequest): Promise<ScriptPolicy> {
    return this.request<ScriptPolicy>(`/deploy/script-policies/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteScriptPolicy(id: string): Promise<void> {
    return this.request<void>(`/deploy/script-policies/${id}`, { method: "DELETE" });
  }

  listScriptPolicyRuns(id: string): Promise<ScriptPolicyRun[]> {
    return this.request<ScriptPolicyRun[]>(`/deploy/script-policies/${id}/runs`);
  }

  listBackupRepositories(): Promise<Repository[]> {
    return this.request<Repository[]>("/backup/repositories");
  }

  createBackupRepository(body: RepositoryCreateRequest): Promise<Repository> {
    return this.request<Repository>("/backup/repositories", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  listBackupPolicies(): Promise<BackupPolicy[]> {
    return this.request<BackupPolicy[]>("/backup/policies");
  }

  upsertBackupPolicy(body: PolicyUpsertRequest): Promise<BackupPolicy> {
    return this.request<BackupPolicy>("/backup/policies", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  runBackup(deviceId: string): Promise<RunResponse> {
    return this.request<RunResponse>(`/backup/devices/${deviceId}/run`, { method: "POST" });
  }

  listBackupPoints(): Promise<BackupPoint[]> {
    return this.request<BackupPoint[]>("/backup/points");
  }

  getBackupManifest(pointId: string): Promise<BackupManifest> {
    return this.request<BackupManifest>(`/backup/points/${pointId}/manifest`);
  }

  getDeviceBackup(deviceId: string): Promise<DeviceBackupStatus> {
    return this.request<DeviceBackupStatus>(`/backup/devices/${deviceId}/backup`);
  }

  restoreBackupPoint(pointId: string, body: RestoreRequest): Promise<RestoreResponse> {
    return this.request<RestoreResponse>(`/backup/points/${pointId}/restore`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // POST /backup/points/{id}/verify takes no request body — the backend
  // (app/modules/backup/api.py verify_point) always dispatches with an empty
  // selection/target itself (mode="verify" covers the whole point), so there
  // is nothing for the caller to supply.
  verifyBackupPoint(pointId: string): Promise<RestoreResponse> {
    return this.request<RestoreResponse>(`/backup/points/${pointId}/verify`, {
      method: "POST",
    });
  }

  listOffsiteTargets(): Promise<OffsiteTarget[]> {
    return this.request<OffsiteTarget[]>("/backup/offsite-targets");
  }

  createOffsiteTarget(body: OffsiteTargetCreate): Promise<OffsiteTarget> {
    return this.request<OffsiteTarget>("/backup/offsite-targets", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateOffsiteTarget(id: string, body: OffsiteTargetUpdate): Promise<OffsiteTarget> {
    return this.request<OffsiteTarget>(`/backup/offsite-targets/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  testOffsiteTarget(id: string): Promise<OffsiteTarget> {
    return this.request<OffsiteTarget>(`/backup/offsite-targets/${id}/test`, { method: "POST" });
  }

  setRepositoryOffsite(repositoryId: string, targetId: string | null): Promise<RepositoryOffsite> {
    return this.request<RepositoryOffsite>(`/backup/repositories/${repositoryId}/offsite`, {
      method: "PUT",
      body: JSON.stringify({ target_id: targetId }),
    });
  }

  listRecoveryKeys(): Promise<RecoveryKey[]> {
    return this.request<RecoveryKey[]>("/backup/recovery-keys");
  }

  registerRecoveryKey(publicKey: string): Promise<RecoveryKeyChallenge> {
    return this.request<RecoveryKeyChallenge>("/backup/recovery-keys", {
      method: "POST",
      body: JSON.stringify({ public_key: publicKey, stored_confirmed: true }),
    });
  }

  newRecoveryKeyChallenge(keyId: string): Promise<RecoveryKeyChallenge> {
    return this.request<RecoveryKeyChallenge>(`/backup/recovery-keys/${keyId}/challenge`, {
      method: "POST",
    });
  }

  proveRecoveryKey(keyId: string, answer: string): Promise<RecoveryKey> {
    return this.request<RecoveryKey>(`/backup/recovery-keys/${keyId}/prove`, {
      method: "POST",
      body: JSON.stringify({ answer }),
    });
  }

  getBackupReport(): Promise<FleetBackupReport> {
    return this.request<FleetBackupReport>("/backup/report");
  }

  listOsImages(): Promise<OsImage[]> {
    return this.request<OsImage[]>("/provisioning/images");
  }

  createOsImage(body: OsImageCreateRequest): Promise<OsImage> {
    return this.request<OsImage>("/provisioning/images", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getOsImage(id: string): Promise<OsImage> {
    return this.request<OsImage>(`/provisioning/images/${id}`);
  }

  listProvisionProfiles(): Promise<ProvisionProfile[]> {
    return this.request<ProvisionProfile[]>("/provisioning/profiles");
  }

  createProvisionProfile(body: ProfileUpsertRequest): Promise<ProvisionProfile> {
    return this.request<ProvisionProfile>("/provisioning/profiles", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateProvisionProfile(id: string, body: ProfileUpsertRequest): Promise<ProvisionProfile> {
    return this.request<ProvisionProfile>(`/provisioning/profiles/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  getProvisionProfile(id: string): Promise<ProvisionProfile> {
    return this.request<ProvisionProfile>(`/provisioning/profiles/${id}`);
  }

  startProvisionRun(profileId: string, body: RunStartRequest): Promise<RunStartResponse> {
    return this.request<RunStartResponse>(`/provisioning/profiles/${profileId}/run`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  listProvisionRuns(): Promise<ProvisionRun[]> {
    return this.request<ProvisionRun[]>("/provisioning/runs");
  }

  getProvisionRun(id: string): Promise<ProvisionRun> {
    return this.request<ProvisionRun>(`/provisioning/runs/${id}`);
  }

  getDeviceProvisioning(deviceId: string): Promise<DeviceProvisioning> {
    return this.request<DeviceProvisioning>(`/provisioning/devices/${deviceId}/provisioning`);
  }

  getProvisioningReport(): Promise<FleetProvisioningReport> {
    return this.request<FleetProvisioningReport>("/provisioning/report");
  }

  listChanges(): Promise<ChangeRequest[]> {
    return this.request<ChangeRequest[]>("/changes");
  }

  /** Where patch approvals and deployments need an approved change. */
  listChangeGates(): Promise<ChangeGate[]> {
    return this.request<ChangeGate[]>("/changes/gates");
  }

  setChangeGate(body: ChangeGateSetRequest): Promise<ChangeGate> {
    return this.request<ChangeGate>("/changes/gates", { method: "PUT", body: JSON.stringify(body) });
  }

  removeChangeGate(id: string): Promise<void> {
    return this.request<void>(`/changes/gates/${id}`, { method: "DELETE" });
  }

  createChange(body: ChangeCreateRequest): Promise<ChangeRequest> {
    return this.request<ChangeRequest>("/changes", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getChange(id: string): Promise<ChangeRequest> {
    return this.request<ChangeRequest>(`/changes/${id}`);
  }

  updateChange(id: string, body: ChangeUpdateRequest): Promise<ChangeRequest> {
    return this.request<ChangeRequest>(`/changes/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  submitChange(id: string): Promise<ChangeRequest> {
    return this.request<ChangeRequest>(`/changes/${id}/submit`, { method: "POST" });
  }

  approveChange(id: string, body: ApprovalDecisionRequest): Promise<Approval> {
    return this.request<Approval>(`/changes/${id}/approve`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  rejectChange(id: string, body: ApprovalDecisionRequest): Promise<Approval> {
    return this.request<Approval>(`/changes/${id}/reject`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  transitionChange(id: string, body: TransitionRequest): Promise<ChangeRequest> {
    return this.request<ChangeRequest>(`/changes/${id}/transition`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  listChangeLinks(id: string): Promise<ChangeLink[]> {
    return this.request<ChangeLink[]>(`/changes/${id}/links`);
  }

  addChangeLink(id: string, body: LinkCreateRequest): Promise<ChangeLink> {
    return this.request<ChangeLink>(`/changes/${id}/links`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  removeChangeLink(id: string, linkId: string): Promise<void> {
    return this.request<void>(`/changes/${id}/links/${linkId}`, { method: "DELETE" });
  }

  // ---- Kernel: audit ------------------------------------------------------
  // These routes have shipped since the platform kernel and this client never
  // grew methods for them, so `modules/audit` carried its own ~20-line copy of
  // `request()`. Folded in here (2026-08-16) and that file deleted: a second,
  // subtly different copy of an auth path is what gets discovered during an
  // incident.
  listAuditEvents(params: Record<string, string | number | undefined> = {}): Promise<AuditListResponse> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<AuditListResponse>(`/audit${suffix}`);
  }

  /** Walks the chain server-side. A caller that cannot reach this has learned
   * NOTHING about the chain and must not render anything reading as verified. */
  verifyAuditChain(): Promise<AuditVerifyResponse> {
    return this.request<AuditVerifyResponse>("/audit/verify");
  }

  // ---- Agent distribution -------------------------------------------------
  listAgentReleases(): Promise<AgentRelease[]> {
    return this.request<AgentRelease[]>("/agent/releases");
  }

  // The four publishing routes below all require `agent.release.manage`
  // (super-admin). The console hides their buttons from anyone else as a
  // convenience only; the server refuses them whatever the console shows.

  createAgentRelease(body: AgentReleaseCreate): Promise<AgentRelease> {
    return this.request<AgentRelease>("/agent/releases", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // The artifact route takes raw bytes as the request body
  // (`upload_artifact_route`: `read_bounded_body(request, 128 MiB)`), not JSON
  // -- bypasses `request<T>`'s JSON.stringify/Content-Type handling, mirroring
  // `uploadPackageArtifact`. The `sha256` in the response is the SERVER's
  // digest of what it stored, which is what a release signature is compared
  // against before it is sent.
  async uploadAgentReleaseArtifact(releaseId: string, file: Blob): Promise<AgentRelease> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/agent/releases/${releaseId}/artifact`));
    headers.set("Content-Type", "application/octet-stream");
    const response = await fetch(`${this.base}/agent/releases/${releaseId}/artifact`, {
      method: "POST",
      headers,
      body: file,
    });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return (await response.json()) as AgentRelease;
  }

  /** Records the offline Ed25519 release signature (`ReleaseSignatureIn`). 204. */
  setAgentReleaseSignature(releaseId: string, releaseSignature: string): Promise<void> {
    return this.request<void>(`/agent/releases/${releaseId}/release-signature`, {
      method: "PUT",
      body: JSON.stringify({ release_signature: releaseSignature }),
    });
  }

  /** Sets the channel of EVERY (os, arch) row of a version at once. */
  promoteAgentReleaseVersion(
    version: string,
    channel: "pilot" | "stable",
  ): Promise<AgentReleasePromoted> {
    return this.request<AgentReleasePromoted>(
      `/agent/releases/versions/${encodeURIComponent(version)}/promote`,
      { method: "POST", body: JSON.stringify({ channel }) },
    );
  }

  /** 409 while an active update rollout still uses the release. */
  deleteAgentRelease(releaseId: string): Promise<void> {
    return this.request<void>(`/agent/releases/${releaseId}`, { method: "DELETE" });
  }

  /**
   * Every platform's package of one version, all or none, files included.
   * 409 while an active update rollout still uses any of them.
   */
  deleteAgentReleaseVersion(version: string): Promise<AgentVersionRemoved> {
    return this.request<AgentVersionRemoved>(
      `/agent/releases/versions/${encodeURIComponent(version)}`,
      { method: "DELETE" },
    );
  }

  // ---- Agent self-update: the server enforces every permission ----------
  getFleetVersions(): Promise<FleetVersions> {
    return this.request<FleetVersions>("/agent/fleet-versions");
  }

  listUpdateRollouts(): Promise<UpdateRollout[]> {
    return this.request<UpdateRollout[]>("/agent/update-rollouts");
  }

  listUpdateTargets(rolloutId: string): Promise<UpdateTarget[]> {
    return this.request<UpdateTarget[]>(`/agent/update-rollouts/${rolloutId}/targets`);
  }

  previewUpdateRollout(body: UpdateRolloutRequest): Promise<UpdatePreview> {
    return this.request<UpdatePreview>("/agent/update-rollouts/preview", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  createUpdateRollout(body: UpdateRolloutRequest): Promise<UpdateRollout> {
    return this.request<UpdateRollout>("/agent/update-rollouts", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateRolloutAction(rolloutId: string, action: UpdateRolloutAction): Promise<UpdateRollout> {
    return this.request<UpdateRollout>(`/agent/update-rollouts/${rolloutId}/${action}`, {
      method: "POST",
    });
  }

  closeUpdateRing(rolloutId: string, ringId: string): Promise<UpdateRollout> {
    return this.request<UpdateRollout>(
      `/agent/update-rollouts/${rolloutId}/rings/${ringId}/close`,
      { method: "POST" },
    );
  }

  generateAgentInstallCommand(
    releaseId: string,
    expiresInHours: number,
    /** The site the machine lands in; its client is the site's (MSP clients spec §1.5). */
    siteId: string,
  ): Promise<AgentInstallCommand> {
    return this.request<AgentInstallCommand>(
      `/agent/releases/${releaseId}/install-command`,
      {
        method: "POST",
        body: JSON.stringify({ expires_in_hours: expiresInHours, site_id: siteId }),
      },
    );
  }

  /**
   * The artefact half of a managed deployment. `agent.release.read`.
   *
   * MINTS NOTHING. Kept separate from the token call below under a weaker
   * permission precisely so an operator who may not mint credentials can still
   * collect the artefact and the staging procedure.
   */
  getAgentDeploymentPackage(
    releaseId: string,
    channel: AgentDeploymentChannel,
  ): Promise<AgentDeploymentPackage> {
    return this.request<AgentDeploymentPackage>(
      `/agent/releases/${releaseId}/deployment-package?channel=${channel}`,
    );
  }

  /**
   * `enrollment.link.manage`, on every verb including this read.
   *
   * ONE LINK PER SITE (MSP clients spec §1.5, §3.2): a machine that joins
   * through it lands in that site and its client, and the networks it answers
   * are the site's egress networks. The organisation-level `/install-link`
   * is gone and answers 404.
   */
  getInstallLink(siteId: string): Promise<InstallLinkSettings> {
    return this.request<InstallLinkSettings>(`/sites/${siteId}/install-link`);
  }

  saveInstallLink(siteId: string, body: InstallLinkUpdate): Promise<InstallLinkSaved> {
    return this.request<InstallLinkSaved>(`/sites/${siteId}/install-link`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  /** A new code: the old link stops working the moment this returns. */
  regenerateInstallLink(siteId: string): Promise<InstallLinkSaved> {
    return this.request<InstallLinkSaved>(`/sites/${siteId}/install-link/regenerate`, {
      method: "POST",
    });
  }

  /**
   * `enrollment.link.manage`. The Group Policy MSI for one site's install
   * link. THE CODE GOES IN THE BODY, never in the URL, which access logs
   * keep; the server holds only its hash and checks the code against it. The
   * site id in the path is not a secret. Not routed through `request`: the
   * body is a file, and its name and SHA-256 come back in headers.
   */
  async downloadInstallLinkMsi(
    siteId: string,
    body: { code: string; arch: MsiArch },
  ): Promise<InstallLinkMsi> {
    const headers = requestHeaders(this.getToken(), { "Content-Type": "application/json" }, this.route("POST", `/sites/${siteId}/install-link/msi`));
    const response = await fetch(`${this.base}/sites/${siteId}/install-link/msi`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `ITMS-Agent-${body.arch}.msi`;
    return {
      blob: await response.blob(),
      filename,
      sha256: response.headers.get("X-Content-SHA256") ?? "",
    };
  }

  /**
   * The credential half. `enrollment.manage`.
   *
   * BOTH BOUNDS ARE REQUIRED by the server and neither is defaulted: they are
   * the blast radius, and a default would be the platform choosing it on the
   * operator's behalf, once, for every rollout regardless of size.
   */
  mintAgentDeploymentToken(
    releaseId: string,
    body: {
      max_uses: number;
      expires_in_hours: number;
      channel: AgentDeploymentChannel;
      /** The site the machines land in; their client is the site's (MSP clients spec §1.5). */
      site_id: string;
    },
  ): Promise<AgentDeploymentToken> {
    return this.request<AgentDeploymentToken>(
      `/agent/releases/${releaseId}/deployment-token`,
      { method: "POST", body: JSON.stringify(body) },
    );
  }

  // ---- Kernel: enrollment -------------------------------------------------
  /** Revoked tokens are excluded unless `include_revoked` is set; the response
   *  carries `revoked_count` either way, so the console can say what it hid. */
  listEnrollmentTokens(
    params: {
      include_revoked?: boolean;
      /**
       * Only the tokens that are still redeemable and carry NO use limit --
       * the grandfathered rows `EnrollmentTokenPage.unlimited_count` counts.
       * The route filters on the same predicate it counts with, so the badge
       * and the list it opens cannot disagree about which rows they mean.
       */
      unlimited_only?: boolean;
      limit?: number;
      offset?: number;
    } = {},
  ): Promise<EnrollmentTokenPage> {
    const query = new URLSearchParams();
    if (params.include_revoked) query.set("include_revoked", "true");
    if (params.unlimited_only) query.set("unlimited_only", "true");
    // `limit` is CAPPED SERVER-SIDE at 500 and 422s above it; sending one at
    // all is what stops this route returning every token an org ever issued.
    if (params.limit != null) query.set("limit", String(params.limit));
    if (params.offset) query.set("offset", String(params.offset));
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<EnrollmentTokenPage>(`/enrollment-tokens${suffix}`);
  }

  createEnrollmentToken(body: EnrollmentTokenCreateRequest): Promise<EnrollmentTokenCreated> {
    return this.request<EnrollmentTokenCreated>("/enrollment-tokens", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  revokeEnrollmentToken(tokenId: string): Promise<{ revoked: boolean }> {
    return this.request<{ revoked: boolean }>(`/enrollment-tokens/${tokenId}/revoke`, {
      method: "POST",
    });
  }

  /** Destructive and immediate: sets the revocation flag, updates the CA CRL
   * and drops the device's live gateway connection. The device cannot return
   * without re-enrolling -- the certificate was the credential a rotation
   * would have travelled over. */
  revokeAgent(deviceId: string): Promise<{ revoked: boolean }> {
    return this.request<{ revoked: boolean }>(`/agents/${deviceId}/revoke`, { method: "POST" });
  }

  getChangeCalendar(from: string, to: string): Promise<Calendar> {
    const query = new URLSearchParams({ from, to });
    return this.request<Calendar>(`/changes/calendar?${query.toString()}`);
  }

  listChangeApprovals(id: string): Promise<Approval[]> {
    return this.request<Approval[]>(`/changes/${id}/approvals`);
  }

  getChangeEvidence(id: string): Promise<Evidence> {
    return this.request<Evidence>(`/changes/${id}/evidence`);
  }

  listPlans(): Promise<PlanOut[]> {
    return this.request<PlanOut[]>("/bcp/plans");
  }

  createPlan(body: PlanCreateRequest): Promise<PlanOut> {
    return this.request<PlanOut>("/bcp/plans", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getPlan(id: string): Promise<PlanOut> {
    return this.request<PlanOut>(`/bcp/plans/${id}`);
  }

  updatePlan(id: string, body: PlanUpdateRequest): Promise<PlanOut> {
    return this.request<PlanOut>(`/bcp/plans/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  reviewPlan(id: string): Promise<PlanOut> {
    return this.request<PlanOut>(`/bcp/plans/${id}/review`, { method: "POST" });
  }

  planStatus(): Promise<PlanStatusOut[]> {
    return this.request<PlanStatusOut[]>("/bcp/plans/status");
  }

  listPlanTests(planId: string): Promise<TestOut[]> {
    return this.request<TestOut[]>(`/bcp/plans/${planId}/tests`);
  }

  createTest(planId: string, body: TestCreateRequest): Promise<TestOut> {
    return this.request<TestOut>(`/bcp/plans/${planId}/tests`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getTest(id: string): Promise<TestOut> {
    return this.request<TestOut>(`/bcp/tests/${id}`);
  }

  recordTest(id: string, body: TestRecordRequest): Promise<TestOut> {
    return this.request<TestOut>(`/bcp/tests/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  upsertStep(testId: string, body: StepUpsertRequest): Promise<StepOut> {
    return this.request<StepOut>(`/bcp/tests/${testId}/steps`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  listFindings(testId: string): Promise<FindingOut[]> {
    return this.request<FindingOut[]>(`/bcp/tests/${testId}/findings`);
  }

  createFinding(testId: string, body: FindingCreateRequest): Promise<FindingOut> {
    return this.request<FindingOut>(`/bcp/tests/${testId}/findings`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateFinding(id: string, body: FindingUpdateRequest): Promise<FindingOut> {
    return this.request<FindingOut>(`/bcp/findings/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  testReport(testId: string): Promise<TestReportOut> {
    return this.request<TestReportOut>(`/bcp/tests/${testId}/report`);
  }

  periodReport(from: string, to: string): Promise<PeriodReportOut> {
    const query = new URLSearchParams({ from, to });
    return this.request<PeriodReportOut>(`/bcp/report/period?${query.toString()}`);
  }

  listVendors(): Promise<VendorOut[]> {
    return this.request<VendorOut[]>("/vendors");
  }

  createVendor(body: VendorCreate): Promise<VendorOut> {
    return this.request<VendorOut>("/vendors", { method: "POST", body: JSON.stringify(body) });
  }

  getVendor(id: string): Promise<VendorOut> {
    return this.request<VendorOut>(`/vendors/${id}`);
  }

  updateVendor(id: string, body: VendorUpdate): Promise<VendorOut> {
    return this.request<VendorOut>(`/vendors/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  listProducts(): Promise<ProductOut[]> {
    return this.request<ProductOut[]>("/licenses/products");
  }

  createProduct(body: ProductCreate): Promise<ProductOut> {
    return this.request<ProductOut>("/licenses/products", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getProduct(id: string): Promise<ProductOut> {
    return this.request<ProductOut>(`/licenses/products/${id}`);
  }

  updateProduct(id: string, body: ProductUpdate): Promise<ProductOut> {
    return this.request<ProductOut>(`/licenses/products/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  listLicenses(): Promise<LicenseOut[]> {
    return this.request<LicenseOut[]>("/licenses");
  }

  createLicense(body: LicenseCreate): Promise<LicenseOut> {
    return this.request<LicenseOut>("/licenses", { method: "POST", body: JSON.stringify(body) });
  }

  getLicense(id: string): Promise<LicenseOut> {
    return this.request<LicenseOut>(`/licenses/${id}`);
  }

  updateLicense(id: string, body: LicenseUpdate): Promise<LicenseOut> {
    return this.request<LicenseOut>(`/licenses/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteLicense(id: string): Promise<void> {
    return this.request<void>(`/licenses/${id}`, { method: "DELETE" });
  }

  // Returns the plaintext key to the caller once; never cache this in query
  // state (spec §5 / security invariant #3) -- mirrors checkoutCredential.
  revealLicenseKey(id: string): Promise<LicenseKeyOut> {
    return this.request<LicenseKeyOut>(`/licenses/${id}/key`);
  }

  listAllocations(licenseId: string): Promise<AllocationOut[]> {
    return this.request<AllocationOut[]>(`/licenses/${licenseId}/allocations`);
  }

  createAllocation(licenseId: string, body: AllocationCreate): Promise<AllocationOut> {
    return this.request<AllocationOut>(`/licenses/${licenseId}/allocations`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  releaseAllocation(allocationId: string): Promise<AllocationOut> {
    return this.request<AllocationOut>(`/allocations/${allocationId}/release`, {
      method: "POST",
    });
  }

  licenseCompliance(): Promise<ComplianceRow[]> {
    return this.request<ComplianceRow[]>("/licenses/compliance");
  }

  reviewQueue(): Promise<ReviewQueueRow[]> {
    return this.request<ReviewQueueRow[]>("/licenses/review-queue");
  }

  mapSoftware(body: ReviewMapRequest): Promise<ProductOut> {
    return this.request<ProductOut>("/licenses/review-queue/map", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  ignoreSoftware(body: ReviewIgnoreRequest): Promise<ReviewIgnoreOut> {
    return this.request<ReviewIgnoreOut>("/licenses/review-queue/ignore", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  licenseRenewals(withinDays?: number): Promise<RenewalRow[]> {
    const suffix = withinDays != null ? `?within_days=${withinDays}` : "";
    return this.request<RenewalRow[]>(`/licenses/renewals${suffix}`);
  }

  licenseCosts(): Promise<CostRow[]> {
    return this.request<CostRow[]>("/licenses/costs");
  }

  listSubscriptions(): Promise<SubscriptionOut[]> {
    return this.request<SubscriptionOut[]>("/subscriptions");
  }

  createSubscription(body: SubscriptionCreate): Promise<SubscriptionOut> {
    return this.request<SubscriptionOut>("/subscriptions", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getSubscription(id: string): Promise<SubscriptionOut> {
    return this.request<SubscriptionOut>(`/subscriptions/${id}`);
  }

  updateSubscription(id: string, body: SubscriptionUpdate): Promise<SubscriptionOut> {
    return this.request<SubscriptionOut>(`/subscriptions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  cancelSubscription(id: string): Promise<SubscriptionOut> {
    return this.request<SubscriptionOut>(`/subscriptions/${id}/cancel`, { method: "POST" });
  }

  renewSubscription(id: string): Promise<SubscriptionOut> {
    return this.request<SubscriptionOut>(`/subscriptions/${id}/renew`, { method: "POST" });
  }

  listEvents(subscriptionId: string): Promise<EventOut[]> {
    return this.request<EventOut[]>(`/subscriptions/${subscriptionId}/events`);
  }

  listSeats(subscriptionId: string): Promise<SeatAssignmentOut[]> {
    return this.request<SeatAssignmentOut[]>(`/subscriptions/${subscriptionId}/seats`);
  }

  assignSeat(subscriptionId: string, body: SeatAssignCreate): Promise<SeatAssignmentOut> {
    return this.request<SeatAssignmentOut>(`/subscriptions/${subscriptionId}/seats`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  releaseSeat(seatId: string): Promise<SeatAssignmentOut> {
    return this.request<SeatAssignmentOut>(`/seats/${seatId}/release`, { method: "POST" });
  }

  spend(): Promise<SpendSummary> {
    return this.request<SpendSummary>("/subscriptions/spend");
  }

  // `within_days` is the look-ahead window
  // (backend/app/modules/subscriptions/api.py::renewals_route: default 30,
  // ge=1, le=365). Omitting it lets the server apply its own default, the
  // same shape as `licenseRenewals` above.
  renewals(withinDays?: number): Promise<SubscriptionRenewalRow[]> {
    const suffix = withinDays != null ? `?within_days=${withinDays}` : "";
    return this.request<SubscriptionRenewalRow[]>(`/subscriptions/renewals${suffix}`);
  }

  idleSeats(): Promise<IdleSeatRow[]> {
    return this.request<IdleSeatRow[]>("/subscriptions/idle-seats");
  }

  // ------------------------------------------------------------------ people

  listPeople(params: { q?: string; status?: string } = {}): Promise<PersonListRow[]> {
    const query = new URLSearchParams();
    if (params.q) query.set("q", params.q);
    if (params.status) query.set("status", params.status);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return this.request<PersonListRow[]>(`/people${suffix}`);
  }

  createPerson(body: PersonCreate): Promise<PersonOut> {
    return this.request<PersonOut>("/people", { method: "POST", body: JSON.stringify(body) });
  }

  getPerson(personId: string): Promise<PersonDetailOut> {
    return this.request<PersonDetailOut>(`/people/${personId}`);
  }

  updatePerson(personId: string, body: PersonUpdate): Promise<PersonOut> {
    return this.request<PersonOut>(`/people/${personId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  linkPersonAccount(personId: string, userId: string | null): Promise<PersonOut> {
    return this.request<PersonOut>(`/people/${personId}/account`, {
      method: "PUT",
      body: JSON.stringify({ user_id: userId }),
    });
  }

  addPersonLogin(personId: string, login: string): Promise<LoginOut> {
    return this.request<LoginOut>(`/people/${personId}/logins`, {
      method: "POST",
      body: JSON.stringify({ login }),
    });
  }

  removePersonLogin(personId: string, loginId: string): Promise<void> {
    return this.request<void>(`/people/${personId}/logins/${loginId}`, { method: "DELETE" });
  }

  personForUser(userId: string): Promise<PersonOut> {
    return this.request<PersonOut>(`/people/by-user/${userId}`);
  }

  getDevicePerson(deviceId: string): Promise<DevicePersonOut> {
    return this.request<DevicePersonOut>(`/people/devices/${deviceId}`);
  }

  setDevicePerson(deviceId: string, personId: string | null): Promise<DevicePersonOut> {
    return this.request<DevicePersonOut>(`/people/devices/${deviceId}`, {
      method: "PUT",
      body: JSON.stringify({ person_id: personId }),
    });
  }

  clearDevicePersonOverride(deviceId: string): Promise<DevicePersonOut> {
    return this.request<DevicePersonOut>(`/people/devices/${deviceId}/override`, {
      method: "DELETE",
    });
  }

  listUnmatched(): Promise<UnmatchedOut> {
    return this.request<UnmatchedOut>("/people/unmatched");
  }

  linkSeatToPerson(seatId: string, personId: string | null): Promise<PersonOut> {
    return this.request<PersonOut>(`/people/unmatched/seats/${seatId}/link`, {
      method: "POST",
      body: JSON.stringify({ person_id: personId }),
    });
  }

  startChecklist(personId: string, kind: ChecklistKind): Promise<ChecklistOut> {
    return this.request<ChecklistOut>(`/people/${personId}/checklists`, {
      method: "POST",
      body: JSON.stringify({ kind }),
    });
  }

  updateChecklistStep(
    personId: string,
    checklistId: string,
    key: string,
    body: { state: StepState; note: string },
  ): Promise<ChecklistOut> {
    return this.request<ChecklistOut>(
      `/people/${personId}/checklists/${checklistId}/steps/${key}`,
      { method: "PATCH", body: JSON.stringify(body) },
    );
  }

  closeChecklist(
    personId: string,
    checklistId: string,
    outcome: "complete" | "cancel",
  ): Promise<ChecklistOut> {
    return this.request<ChecklistOut>(
      `/people/${personId}/checklists/${checklistId}/${outcome}`,
      { method: "POST" },
    );
  }

  // Budget settings are ONE CLIENT'S (each has its own base currency and
  // fiscal year), named in the query; the lists take a client as a filter.
  getBudgetSettings(clientId: string): Promise<SettingsOut> {
    return this.request<SettingsOut>(
      `/budget/settings?client_id=${encodeURIComponent(clientId)}`,
    );
  }

  updateBudgetSettings(clientId: string, body: SettingsUpdate): Promise<SettingsOut> {
    return this.request<SettingsOut>(
      `/budget/settings?client_id=${encodeURIComponent(clientId)}`,
      { method: "PUT", body: JSON.stringify(body) },
    );
  }

  listBudgetCategories(includeInactive = false, clientId?: string): Promise<CategoryOut[]> {
    const query = new URLSearchParams();
    if (clientId) query.set("client_id", clientId);
    if (includeInactive) query.set("include_inactive", "true");
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<CategoryOut[]>(`/budget/categories${suffix}`);
  }

  createBudgetCategory(body: CategoryCreate): Promise<CategoryOut> {
    return this.request<CategoryOut>("/budget/categories", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateBudgetCategory(id: string, body: CategoryUpdate): Promise<CategoryOut> {
    return this.request<CategoryOut>(`/budget/categories/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  listCostCenters(includeInactive = false, clientId?: string): Promise<CostCenterOut[]> {
    const query = new URLSearchParams();
    if (clientId) query.set("client_id", clientId);
    if (includeInactive) query.set("include_inactive", "true");
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<CostCenterOut[]>(`/budget/cost-centers${suffix}`);
  }

  createCostCenter(body: CostCenterCreate): Promise<CostCenterOut> {
    return this.request<CostCenterOut>("/budget/cost-centers", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateCostCenter(id: string, body: CostCenterUpdate): Promise<CostCenterOut> {
    return this.request<CostCenterOut>(`/budget/cost-centers/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  listFxRates(fromCurrency?: string): Promise<FxRateOut[]> {
    const suffix = fromCurrency ? `?from_currency=${encodeURIComponent(fromCurrency)}` : "";
    return this.request<FxRateOut[]>(`/budget/fx-rates${suffix}`);
  }

  createFxRate(body: FxRateCreate): Promise<FxRateOut> {
    return this.request<FxRateOut>("/budget/fx-rates", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateFxRate(id: string, body: FxRateUpdate): Promise<FxRateOut> {
    return this.request<FxRateOut>(`/budget/fx-rates/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  listBudgets(clientId?: string): Promise<BudgetOut[]> {
    const suffix = clientId ? `?client_id=${encodeURIComponent(clientId)}` : "";
    return this.request<BudgetOut[]>(`/budget/budgets${suffix}`);
  }

  createBudget(body: BudgetCreate): Promise<BudgetOut> {
    return this.request<BudgetOut>("/budget/budgets", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getBudget(id: string): Promise<BudgetOut> {
    return this.request<BudgetOut>(`/budget/budgets/${id}`);
  }

  updateBudget(id: string, body: BudgetUpdate): Promise<BudgetOut> {
    return this.request<BudgetOut>(`/budget/budgets/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  closeBudget(id: string): Promise<BudgetOut> {
    return this.request<BudgetOut>(`/budget/budgets/${id}/close`, { method: "POST" });
  }

  listBudgetLines(budgetId: string): Promise<BudgetLineOut[]> {
    return this.request<BudgetLineOut[]>(`/budget/budgets/${budgetId}/lines`);
  }

  putBudgetLines(budgetId: string, lines: BudgetLineInput[]): Promise<BudgetLineOut[]> {
    return this.request<BudgetLineOut[]>(`/budget/budgets/${budgetId}/lines`, {
      method: "PUT",
      body: JSON.stringify({ lines }),
    });
  }

  deleteBudgetLine(budgetId: string, lineId: string): Promise<void> {
    return this.request<void>(`/budget/budgets/${budgetId}/lines/${lineId}`, {
      method: "DELETE",
    });
  }

  getBudgetVariance(budgetId: string): Promise<VarianceOut> {
    return this.request<VarianceOut>(`/budget/budgets/${budgetId}/variance`);
  }

  getBudgetPosition(budgetId: string, asOf?: string): Promise<PositionOut> {
    const suffix = asOf ? `?as_of=${asOf}` : "";
    return this.request<PositionOut>(`/budget/budgets/${budgetId}/position${suffix}`);
  }

  listExpenses(filters: ExpenseListFilters = {}, clientId?: string): Promise<ExpenseOut[]> {
    const query = new URLSearchParams();
    if (clientId) query.set("client_id", clientId);
    for (const [key, value] of Object.entries(filters)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<ExpenseOut[]>(`/budget/expenses${suffix}`);
  }

  createExpense(body: ExpenseCreate): Promise<ExpenseOut> {
    return this.request<ExpenseOut>("/budget/expenses", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getExpense(id: string): Promise<ExpenseOut> {
    return this.request<ExpenseOut>(`/budget/expenses/${id}`);
  }

  updateExpense(id: string, body: ExpenseUpdate): Promise<ExpenseOut> {
    return this.request<ExpenseOut>(`/budget/expenses/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  confirmExpense(id: string, body: ExpenseConfirm): Promise<ExpenseOut> {
    return this.request<ExpenseOut>(`/budget/expenses/${id}/confirm`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  voidExpense(id: string): Promise<ExpenseOut> {
    return this.request<ExpenseOut>(`/budget/expenses/${id}/void`, { method: "POST" });
  }

  materializeExpenses(body: MaterializeRequest): Promise<MaterializeOut> {
    return this.request<MaterializeOut>("/budget/expenses/materialize", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // The attachment route takes raw bytes as the request body (api.py's
  // upload_attachment_route: `_read_bounded_body(request)`), not JSON --
  // bypasses `request<T>`'s JSON.stringify/Content-Type handling, mirroring
  // uploadPackageArtifact's binary-upload pattern. `filename` is a query
  // parameter, sanitized server-side before it ever reaches the object key.
  async uploadExpenseAttachment(id: string, file: File): Promise<ExpenseOut> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/budget/expenses/${id}/attachment`));
    headers.set("Content-Type", "application/octet-stream");
    const response = await fetch(
      `${this.base}/budget/expenses/${id}/attachment?filename=${encodeURIComponent(file.name)}`,
      { method: "POST", headers, body: file },
    );
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return (await response.json()) as ExpenseOut;
  }

  // Streams the raw attachment bytes; not routed through `request` since the
  // body is binary, not JSON. Mirrors getRemoteRecording's binary-download
  // pattern.
  async downloadExpenseAttachment(id: string): Promise<Blob> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/budget/expenses/${id}/attachment`));
    const response = await fetch(`${this.base}/budget/expenses/${id}/attachment`, { headers });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return response.blob();
  }

  // --- Service desk (backend/app/modules/service_desk/api.py) -----------

  listTicketQueues(includeInactive = false): Promise<TicketQueueOut[]> {
    const suffix = includeInactive ? "?include_inactive=true" : "";
    return this.request<TicketQueueOut[]>(`/service-desk/queues${suffix}`);
  }

  createTicketQueue(body: TicketQueueCreate): Promise<TicketQueueOut> {
    return this.request<TicketQueueOut>("/service-desk/queues", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateTicketQueue(id: string, body: TicketQueueUpdate): Promise<TicketQueueOut> {
    return this.request<TicketQueueOut>(`/service-desk/queues/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  listTicketCategories(includeInactive = false): Promise<TicketCategoryOut[]> {
    const suffix = includeInactive ? "?include_inactive=true" : "";
    return this.request<TicketCategoryOut[]>(`/service-desk/categories${suffix}`);
  }

  createTicketCategory(body: TicketCategoryCreate): Promise<TicketCategoryOut> {
    return this.request<TicketCategoryOut>("/service-desk/categories", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateTicketCategory(id: string, body: TicketCategoryUpdate): Promise<TicketCategoryOut> {
    return this.request<TicketCategoryOut>(`/service-desk/categories/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  /**
   * One page of tickets, plus the size of the set it was cut from.
   *
   * The route returned a bare array capped at 200, so a full page and a
   * complete queue were indistinguishable here and `TicketListPage` could
   * say only "the list is capped and there is probably more". `{items,
   * total}` since closeout recommendation 10.
   */
  listTickets(filters: TicketListFilters = {}): Promise<Page<TicketOut>> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<Page<TicketOut>>(`/service-desk/tickets${suffix}`);
  }

  createTicket(body: TicketCreate): Promise<TicketOut> {
    return this.request<TicketOut>("/service-desk/tickets", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getTicket(id: string): Promise<TicketOut> {
    return this.request<TicketOut>(`/service-desk/tickets/${id}`);
  }

  updateTicket(id: string, body: TicketUpdate): Promise<TicketOut> {
    return this.request<TicketOut>(`/service-desk/tickets/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  // Status moves ONLY through this dedicated endpoint (schema.py's module
  // docstring) -- never a generic PATCH; the backend deliberately excludes
  // `status` from `TicketUpdate` for exactly this reason.
  changeTicketStatus(id: string, body: TicketStatusChange): Promise<TicketOut> {
    return this.request<TicketOut>(`/service-desk/tickets/${id}/status`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // Reply (optional) and resolve in one server transaction; the server opens
  // a new ticket first where the lifecycle requires it.
  resolveTicket(id: string, body: TicketResolve): Promise<TicketOut> {
    return this.request<TicketOut>(`/service-desk/tickets/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  assignTicket(id: string, body: TicketAssign): Promise<TicketOut> {
    return this.request<TicketOut>(`/service-desk/tickets/${id}/assign`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /**
   * Move a ticket to another client (`POST .../move`, MSP clients spec §2.4).
   * The server needs `ticket.write` in both clients and refuses -- a 409 with
   * a sentence -- a ticket with a device, or one an alert or a change opened.
   */
  moveTicket(id: string, clientId: string): Promise<TicketOut> {
    return this.request<TicketOut>(`/service-desk/tickets/${id}/move`, {
      method: "POST",
      body: JSON.stringify({ client_id: clientId }),
    });
  }

  approveTicket(id: string, body: ApprovalDecision): Promise<TicketOut> {
    return this.request<TicketOut>(`/service-desk/tickets/${id}/approve`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  rejectTicket(id: string, body: ApprovalDecision): Promise<TicketOut> {
    return this.request<TicketOut>(`/service-desk/tickets/${id}/reject`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // Task 13's G1 fix: `GET .../tickets/{ticket_id}/history` (api.py's
  // `list_status_history_route`) -- oldest first, per schema.py's
  // `StatusHistoryOut`.
  listTicketStatusHistory(id: string): Promise<StatusHistoryOut[]> {
    return this.request<StatusHistoryOut[]>(`/service-desk/tickets/${id}/history`);
  }

  listTicketComments(id: string, includeInternal = true): Promise<CommentOut[]> {
    const suffix = includeInternal ? "" : "?include_internal=false";
    return this.request<CommentOut[]>(`/service-desk/tickets/${id}/comments${suffix}`);
  }

  createTicketComment(id: string, body: CommentCreate): Promise<CommentOut> {
    return this.request<CommentOut>(`/service-desk/tickets/${id}/comments`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // Saved replies: listed with ticket.read, changed with ticket.admin.
  listCannedResponses(): Promise<CannedResponseOut[]> {
    return this.request<CannedResponseOut[]>("/service-desk/canned-responses");
  }

  createCannedResponse(body: CannedResponseCreate): Promise<CannedResponseOut> {
    return this.request<CannedResponseOut>("/service-desk/canned-responses", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateCannedResponse(id: string, body: CannedResponseUpdate): Promise<CannedResponseOut> {
    return this.request<CannedResponseOut>(`/service-desk/canned-responses/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteCannedResponse(id: string): Promise<void> {
    return this.request<void>(`/service-desk/canned-responses/${id}`, { method: "DELETE" });
  }

  // Articles matching any word of a ticket subject (needs docs.read).
  suggestDocArticles(q: string, limit = 5): Promise<ArticleSuggestion[]> {
    const query = new URLSearchParams({ q, limit: String(limit) });
    return this.request<ArticleSuggestion[]>(`/docs/suggest?${query.toString()}`);
  }

  listTicketAttachments(id: string): Promise<AttachmentOut[]> {
    return this.request<AttachmentOut[]>(`/service-desk/tickets/${id}/attachments`);
  }

  // The attachment route takes raw bytes as the request body (api.py's
  // upload_attachment_route: `read_bounded_body(request, ...)`), not JSON --
  // bypasses `request<T>`'s JSON.stringify/Content-Type handling, mirroring
  // `uploadExpenseAttachment`'s binary-upload pattern. `filename` is a query
  // parameter, sanitized server-side before it ever reaches the object key.
  async uploadTicketAttachment(id: string, file: File): Promise<AttachmentOut> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/service-desk/tickets/${id}/attachments`));
    headers.set("Content-Type", "application/octet-stream");
    const response = await fetch(
      `${this.base}/service-desk/tickets/${id}/attachments?filename=${encodeURIComponent(file.name)}`,
      { method: "POST", headers, body: file },
    );
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return (await response.json()) as AttachmentOut;
  }

  // Streams the raw attachment bytes; not routed through `request` since the
  // body is binary, not JSON. Mirrors `downloadExpenseAttachment`'s pattern.
  // Note the route is keyed by ATTACHMENT id, not ticket id (api.py's
  // `GET /service-desk/attachments/{attachment_id}`).
  async downloadTicketAttachment(attachmentId: string): Promise<Blob> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/service-desk/attachments/${attachmentId}`));
    const response = await fetch(`${this.base}/service-desk/attachments/${attachmentId}`, {
      headers,
    });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return response.blob();
  }

  // --- Notification (backend/app/modules/notification/api.py) -----------
  //
  // 17 routes under /api/v1/notifications, read against api.py/schema.py
  // directly (spec §9's table predates the exact shapes below). No response
  // model here ever carries a credential, a full webhook URL with a query
  // string, or a response body (invariant #3, spec §9).

  listNotificationChannels(): Promise<NotificationChannelOut[]> {
    return this.request<NotificationChannelOut[]>("/notifications/channels");
  }

  createNotificationChannel(body: NotificationChannelCreate): Promise<NotificationChannelOut> {
    return this.request<NotificationChannelOut>("/notifications/channels", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateNotificationChannel(
    id: string,
    body: NotificationChannelUpdate,
  ): Promise<NotificationChannelOut> {
    return this.request<NotificationChannelOut>(`/notifications/channels/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteNotificationChannel(id: string): Promise<void> {
    return this.request<void>(`/notifications/channels/${id}`, { method: "DELETE" });
  }

  // Subject to the identical egress guard as a real delivery (spec §5/§12.6)
  // -- there is no separate, guard-skipping test-send path on the server,
  // and this client method dials the exact same route a real send does.
  testNotificationChannel(id: string): Promise<TestSendResult> {
    return this.request<TestSendResult>(`/notifications/channels/${id}/test`, {
      method: "POST",
    });
  }

  listNotificationRules(): Promise<NotificationRuleOut[]> {
    return this.request<NotificationRuleOut[]>("/notifications/rules");
  }

  createNotificationRule(body: NotificationRuleCreate): Promise<NotificationRuleOut> {
    return this.request<NotificationRuleOut>("/notifications/rules", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateNotificationRule(id: string, body: NotificationRuleUpdate): Promise<NotificationRuleOut> {
    return this.request<NotificationRuleOut>(`/notifications/rules/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteNotificationRule(id: string): Promise<void> {
    return this.request<void>(`/notifications/rules/${id}`, { method: "DELETE" });
  }

  listNotificationAllowlist(): Promise<AllowlistEntryOut[]> {
    return this.request<AllowlistEntryOut[]>("/notifications/allowlist");
  }

  createNotificationAllowlistEntry(body: AllowlistEntryCreate): Promise<AllowlistEntryOut> {
    return this.request<AllowlistEntryOut>("/notifications/allowlist", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  deleteNotificationAllowlistEntry(id: string): Promise<void> {
    return this.request<void>(`/notifications/allowlist/${id}`, { method: "DELETE" });
  }

  listNotificationQueueMembers(queueId: string): Promise<NotificationQueueMemberOut[]> {
    return this.request<NotificationQueueMemberOut[]>(
      `/notifications/queues/${queueId}/members`,
    );
  }

  addNotificationQueueMember(queueId: string, userId: string): Promise<NotificationQueueMemberOut> {
    return this.request<NotificationQueueMemberOut>(
      `/notifications/queues/${queueId}/members/${userId}`,
      { method: "PUT" },
    );
  }

  removeNotificationQueueMember(queueId: string, userId: string): Promise<void> {
    return this.request<void>(`/notifications/queues/${queueId}/members/${userId}`, {
      method: "DELETE",
    });
  }

  listNotificationDeliveries(filters: DeliveryListFilters = {}): Promise<DeliveryListResponse> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<DeliveryListResponse>(`/notifications/deliveries${suffix}`);
  }

  // --- Email intake (backend/app/modules/email_intake/api.py) -----------
  //
  // Seven route registrations under /api/v1/email-intake, read against
  // api.py/schema.py directly. NO RESPONSE HERE CARRIES THE MAILBOX PASSWORD
  // OR A CREDENTIAL ID (invariant #3): `MailboxOut` structurally has no
  // field that could, and `MailboxTestResult.detail` is a constructed
  // `stage failed: reason` from `imap.py`'s fixed vocabularies, never a raw
  // server response line.

  listEmailMailboxes(): Promise<EmailMailboxOut[]> {
    return this.request<EmailMailboxOut[]>("/email-intake/mailboxes");
  }

  createEmailMailbox(body: EmailMailboxCreate): Promise<EmailMailboxOut> {
    return this.request<EmailMailboxOut>("/email-intake/mailboxes", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // `body` OMITS `password` entirely when the operator did not type a new
  // one -- schema.py's MailboxUpdate reads absent as "leave the stored
  // password alone", and there is no way to clear it (`credential_id` is
  // NOT NULL). Sending `""` would be a validation error and `null` would
  // read as an attempt to clear.
  updateEmailMailbox(id: string, body: EmailMailboxUpdate): Promise<EmailMailboxOut> {
    return this.request<EmailMailboxOut>(`/email-intake/mailboxes/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteEmailMailbox(id: string): Promise<void> {
    return this.request<void>(`/email-intake/mailboxes/${id}`, { method: "DELETE" });
  }

  // A failed connection is `200 {"ok": false, ...}`, not a 4xx: the REQUEST
  // succeeded and its answer is "this configuration does not work". The
  // route is rate-limited per org (5/min), so a 429 is an expected outcome
  // of impatient retrying, not a defect.
  testEmailMailbox(id: string): Promise<EmailMailboxTestResult> {
    return this.request<EmailMailboxTestResult>(`/email-intake/mailboxes/${id}/test`, {
      method: "POST",
    });
  }

  // Microsoft 365 sign-in. No response carries the client secret or a token.
  getEmailOAuthApp(): Promise<EmailOAuthAppOut> {
    return this.request<EmailOAuthAppOut>("/email-intake/oauth-app");
  }

  saveEmailOAuthApp(body: EmailOAuthAppUpdate): Promise<EmailOAuthAppOut> {
    return this.request<EmailOAuthAppOut>("/email-intake/oauth-app", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  startMailboxOAuth(id: string): Promise<EmailOAuthStartOut> {
    return this.request<EmailOAuthStartOut>(`/email-intake/mailboxes/${id}/oauth/start`, {
      method: "POST",
    });
  }

  completeMailboxOAuth(body: { state: string; code: string }): Promise<EmailMailboxOut> {
    return this.request<EmailMailboxOut>("/email-intake/oauth/complete", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  abandonMailboxOAuth(body: { state: string }): Promise<void> {
    return this.request<void>("/email-intake/oauth/abandon", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  disconnectMailboxOAuth(id: string): Promise<EmailMailboxOut> {
    return this.request<EmailMailboxOut>(`/email-intake/mailboxes/${id}/oauth/disconnect`, {
      method: "POST",
    });
  }

  listEmailIntakeMessages(filters: EmailMessageLogFilters = {}): Promise<EmailMessageLogOut> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<EmailMessageLogOut>(`/email-intake/messages${suffix}`);
  }

  // --- Portal (backend/app/modules/portal/api.py) ------------------------
  //
  // The routes under /api/v1/portal, read against api.py/schema.py
  // directly (the task brief predates several backend changes). No response
  // model here ever carries a queue, an assignee, or an internal comment --
  // the server's own response models are structurally narrow (schema.py),
  // and this client widens nothing beyond them.

  suggestPortalHelp(q: string, limit = 5): Promise<PortalHelpSuggestion[]> {
    const query = new URLSearchParams({ q, limit: String(limit) });
    return this.request<PortalHelpSuggestion[]>(`/portal/help/suggest?${query.toString()}`);
  }

  getPortalHelpArticle(id: string): Promise<PortalHelpArticle> {
    return this.request<PortalHelpArticle>(`/portal/help/articles/${id}`);
  }

  listPortalCategories(): Promise<PortalCategoryOut[]> {
    return this.request<PortalCategoryOut[]>("/portal/categories");
  }

  listPortalTickets(filters: PortalTicketListFilters = {}): Promise<PortalTicketSummary[]> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<PortalTicketSummary[]>(`/portal/tickets${suffix}`);
  }

  createPortalTicket(body: PortalTicketCreate): Promise<PortalTicketDetail> {
    return this.request<PortalTicketDetail>("/portal/tickets", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getPortalTicket(id: string): Promise<PortalTicketDetail> {
    return this.request<PortalTicketDetail>(`/portal/tickets/${id}`);
  }

  createPortalComment(id: string, body: PortalCommentCreate): Promise<PortalCommentOut> {
    return this.request<PortalCommentOut>(`/portal/tickets/${id}/comments`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // The attachment route takes raw bytes as the request body (api.py's
  // upload_my_attachment_route: `read_bounded_body(request, ...)`), not JSON
  // -- bypasses `request<T>`'s JSON.stringify/Content-Type handling,
  // mirroring `uploadTicketAttachment`'s binary-upload pattern. `filename`
  // is a query parameter, sanitized server-side before it ever reaches the
  // object key.
  async uploadPortalAttachment(id: string, file: File): Promise<PortalAttachmentOut> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("POST", `/portal/tickets/${id}/attachments`));
    headers.set("Content-Type", "application/octet-stream");
    const response = await fetch(
      `${this.base}/portal/tickets/${id}/attachments?filename=${encodeURIComponent(file.name)}`,
      { method: "POST", headers, body: file },
    );
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return (await response.json()) as PortalAttachmentOut;
  }

  // Streams the raw attachment bytes; not routed through `request` since the
  // body is binary, not JSON. Mirrors `downloadTicketAttachment`'s pattern,
  // but the route is keyed by BOTH ticket id and attachment id (api.py's
  // `GET /portal/tickets/{ticket_id}/attachments/{attachment_id}`) -- unlike
  // the staff surface's attachment-id-only route, because a portal caller's
  // ownership check is scoped through the ticket (module docstring:
  // `_load_my_ticket_row`).
  async downloadPortalAttachment(ticketId: string, attachmentId: string): Promise<Blob> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/portal/tickets/${ticketId}/attachments/${attachmentId}`));
    const response = await fetch(
      `${this.base}/portal/tickets/${ticketId}/attachments/${attachmentId}`,
      { headers },
    );
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return response.blob();
  }

  // --- SLA engine (backend/app/modules/sla/api.py) ----------------------
  //
  // Seventeen route registrations: sixteen under /api/v1/sla and one under
  // /api/v1/tickets/{ticket_id}/sla, read against api.py/schema.py directly.
  // Reads take `sla.read`, mutations `sla.admin`, and the per-ticket read
  // takes `ticket.read` -- all enforced server-side (invariant #6); nothing
  // below assumes any of them.
  //
  // The 204 routes (calendar delete, holiday delete, policy delete) return
  // no body -- `request` already maps 204 to undefined.

  listSlaCalendars(): Promise<SlaCalendarOut[]> {
    return this.request<SlaCalendarOut[]>("/sla/calendars");
  }

  createSlaCalendar(body: SlaCalendarCreate): Promise<SlaCalendarOut> {
    return this.request<SlaCalendarOut>("/sla/calendars", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // The by-id read is the ONLY route that returns a calendar's windows
  // (schema.py's CalendarDetail: spec §9's table gives windows a PUT and no
  // GET, so without this they would be write-only).
  getSlaCalendar(id: string): Promise<SlaCalendarDetail> {
    return this.request<SlaCalendarDetail>(`/sla/calendars/${id}`);
  }

  updateSlaCalendar(id: string, body: SlaCalendarUpdate): Promise<SlaCalendarDetail> {
    return this.request<SlaCalendarDetail>(`/sla/calendars/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteSlaCalendar(id: string): Promise<void> {
    return this.request<void>(`/sla/calendars/${id}`, { method: "DELETE" });
  }

  // Windows are replaced as a SET, never individually (api.py): a weekday's
  // windows only make sense together, and the whole set is validated before
  // any row is touched.
  replaceSlaCalendarWindows(id: string, windows: SlaWindowIn[]): Promise<SlaWindowOut[]> {
    return this.request<SlaWindowOut[]>(`/sla/calendars/${id}/windows`, {
      method: "PUT",
      body: JSON.stringify({ windows }),
    });
  }

  listSlaCalendarHolidays(id: string): Promise<SlaHolidayOut[]> {
    return this.request<SlaHolidayOut[]>(`/sla/calendars/${id}/holidays`);
  }

  addSlaCalendarHoliday(id: string, body: SlaHolidayCreate): Promise<SlaHolidayOut> {
    return this.request<SlaHolidayOut>(`/sla/calendars/${id}/holidays`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  removeSlaCalendarHoliday(id: string, holidayId: string): Promise<void> {
    return this.request<void>(`/sla/calendars/${id}/holidays/${holidayId}`, {
      method: "DELETE",
    });
  }

  listSlaPolicies(): Promise<SlaPolicyOut[]> {
    return this.request<SlaPolicyOut[]>("/sla/policies");
  }

  createSlaPolicy(body: SlaPolicyCreate): Promise<SlaPolicyOut> {
    return this.request<SlaPolicyOut>("/sla/policies", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getSlaPolicy(id: string): Promise<SlaPolicyOut> {
    return this.request<SlaPolicyOut>(`/sla/policies/${id}`);
  }

  updateSlaPolicy(id: string, body: SlaPolicyUpdate): Promise<SlaPolicyOut> {
    return this.request<SlaPolicyOut>(`/sla/policies/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteSlaPolicy(id: string): Promise<void> {
    return this.request<void>(`/sla/policies/${id}`, { method: "DELETE" });
  }

  getSlaSettings(): Promise<SlaSettingsOut> {
    return this.request<SlaSettingsOut>("/sla/settings");
  }

  updateSlaSettings(body: SlaSettingsUpdate): Promise<SlaSettingsOut> {
    return this.request<SlaSettingsOut>("/sla/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  // Gated on `ticket.read`, not `sla.read` (api.py's deliberate exception): a
  // technician needs a ticket's deadline without holding SLA administration
  // rights. A ticket with no SLA row is a 200 with `has_sla: false`, not a
  // 404 -- "no SLA" is a legitimate, visible state.
  getTicketSla(ticketId: string): Promise<TicketSlaOut> {
    return this.request<TicketSlaOut>(`/tickets/${ticketId}/sla`);
  }

  // --- Endpoint health (backend/app/modules/health/api.py) ---------------
  //
  // Thirty-one route registrations: twenty-two under /api/v1/health, two under
  // /api/v1/devices/{id}, seven under /api/v1/approved-actions. Read against
  // api.py/schema.py directly.
  //
  // FIVE PERMISSIONS GATE THESE SERVER-SIDE and nothing below assumes any of
  // them (invariant #6): `health.read` on reads, `health.admin` on profile /
  // check / assignment / settings writes, `health.alert.manage` on the three
  // alert transitions, `health.remediate` on dispatch/approve/refuse, and
  // `health.action.admin` on every approved-action write. Two exceptions worth
  // naming here because they look like mistakes otherwise: the device health
  // read takes `devices.read` (a technician needs a device's health without
  // health administration rights) and `checkDeviceHealthNow` takes
  // `health.alert.manage` -- §10's amendment: it mints a signed job, so it is
  // not a read code, and it is additionally fused per device and per org.

  listHealthProfiles(): Promise<HealthProfileOut[]> {
    return this.request<HealthProfileOut[]>("/health/profiles");
  }

  createHealthProfile(body: HealthProfileCreate): Promise<HealthProfileOut> {
    return this.request<HealthProfileOut>("/health/profiles", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getHealthProfile(id: string): Promise<HealthProfileOut> {
    return this.request<HealthProfileOut>(`/health/profiles/${id}`);
  }

  updateHealthProfile(id: string, body: HealthProfileUpdate): Promise<HealthProfileOut> {
    return this.request<HealthProfileOut>(`/health/profiles/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteHealthProfile(id: string): Promise<void> {
    return this.request<void>(`/health/profiles/${id}`, { method: "DELETE" });
  }

  cloneHealthProfile(id: string, name: string): Promise<HealthProfileOut> {
    return this.request<HealthProfileOut>(`/health/profiles/${id}/clone`, {
      method: "POST",
      body: JSON.stringify({ name }),
    });
  }

  getHealthProfileChecks(id: string): Promise<HealthProfileChecksOut> {
    return this.request<HealthProfileChecksOut>(`/health/profiles/${id}/checks`);
  }

  // Checks are replaced as a SET, never individually (api.py): a profile's
  // checks only make sense together, and a partial edit can leave a profile
  // that no longer matches the `policy_version` an agent acknowledged. A check
  // this body drops is RETIRED, and its open alerts are cleared
  // `policy_removed` — the alert rows survive.
  replaceHealthProfileChecks(
    id: string,
    checks: HealthCheckIn[],
  ): Promise<HealthProfileChecksOut> {
    return this.request<HealthProfileChecksOut>(`/health/profiles/${id}/checks`, {
      method: "PUT",
      body: JSON.stringify({ checks }),
    });
  }

  listHealthAssignments(profileId?: string): Promise<HealthAssignmentOut[]> {
    const query = profileId ? `?profile_id=${encodeURIComponent(profileId)}` : "";
    return this.request<HealthAssignmentOut[]>(`/health/assignments${query}`);
  }

  createHealthAssignment(body: HealthAssignmentCreate): Promise<HealthAssignmentOut> {
    return this.request<HealthAssignmentOut>("/health/assignments", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  deleteHealthAssignment(id: string): Promise<void> {
    return this.request<void>(`/health/assignments/${id}`, { method: "DELETE" });
  }

  listHealthCheckTypes(): Promise<HealthCheckTypeOut[]> {
    return this.request<HealthCheckTypeOut[]>("/health/check-types");
  }

  listHealthAlerts(filters: HealthAlertFilters = {}): Promise<HealthAlertOut[]> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<HealthAlertOut[]>(`/health/alerts${suffix}`);
  }

  /**
   * The org's open-alert TOTALS, counted server-side.
   *
   * NOT `listHealthAlerts().length`. The list is a page (200 by the route's
   * default, 500 at most) and its `state` filter takes one value, so "open" --
   * active OR acknowledged OR muted -- is not a query it can express. The
   * shell's health glyph read a page and filtered it in the browser until this
   * existed, which under-reports on any fleet with more open alerts than fit
   * and is why the badge used to say "at least N".
   */
  getHealthAlertSummary(): Promise<HealthAlertSummary> {
    return this.request<HealthAlertSummary>("/health/alerts/summary");
  }

  getHealthAlert(id: string): Promise<HealthAlertDetailOut> {
    return this.request<HealthAlertDetailOut>(`/health/alerts/${id}`);
  }

  acknowledgeHealthAlert(id: string): Promise<HealthAlertOut> {
    return this.request<HealthAlertOut>(`/health/alerts/${id}/acknowledge`, { method: "POST" });
  }

  // Bounded to 30 days from the alert's `opened_at`, server-side: unbounded
  // mutes are how a fleet accumulates permanently silent alarms.
  muteHealthAlert(id: string, mutedUntil: string): Promise<HealthAlertOut> {
    return this.request<HealthAlertOut>(`/health/alerts/${id}/mute`, {
      method: "POST",
      body: JSON.stringify({ muted_until: mutedUntil }),
    });
  }

  closeHealthAlert(id: string): Promise<HealthAlertOut> {
    return this.request<HealthAlertOut>(`/health/alerts/${id}/close`, { method: "POST" });
  }

  remediateHealthAlert(id: string, actionKey: string): Promise<HealthJobDispatchedOut> {
    return this.request<HealthJobDispatchedOut>(`/health/alerts/${id}/remediate`, {
      method: "POST",
      body: JSON.stringify({ action_key: actionKey }),
    });
  }

  approveHealthAlertAction(
    alertId: string,
    actionId: string,
  ): Promise<HealthJobDispatchedOut> {
    return this.request<HealthJobDispatchedOut>(
      `/health/alerts/${alertId}/actions/${actionId}/approve`,
      { method: "POST" },
    );
  }

  refuseHealthAlertAction(alertId: string, actionId: string, reason: string): Promise<unknown> {
    return this.request<unknown>(`/health/alerts/${alertId}/actions/${actionId}/refuse`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
  }

  getHealthSettings(): Promise<HealthSettingsOut> {
    return this.request<HealthSettingsOut>("/health/settings");
  }

  updateHealthSettings(body: HealthSettingsUpdate): Promise<HealthSettingsOut> {
    return this.request<HealthSettingsOut>("/health/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  getDeviceHealth(deviceId: string): Promise<DeviceHealthOut> {
    return this.request<DeviceHealthOut>(`/devices/${deviceId}/health`);
  }

  checkDeviceHealthNow(deviceId: string): Promise<HealthJobDispatchedOut> {
    return this.request<HealthJobDispatchedOut>(`/devices/${deviceId}/health/check-now`, {
      method: "POST",
    });
  }

  listApprovedActions(): Promise<ApprovedActionOut[]> {
    return this.request<ApprovedActionOut[]>("/approved-actions");
  }

  // `destructive` is DERIVED server-side and is not on the create body: sending
  // it is a 422, deliberately, because a caller who believed they set
  // `destructive: false` and got a 201 would believe they disarmed the
  // autonomy gate.
  createApprovedAction(body: ApprovedActionCreate): Promise<ApprovedActionOut> {
    return this.request<ApprovedActionOut>("/approved-actions", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getApprovedAction(id: string): Promise<ApprovedActionOut> {
    return this.request<ApprovedActionOut>(`/approved-actions/${id}`);
  }

  updateApprovedAction(id: string, body: ApprovedActionUpdate): Promise<ApprovedActionOut> {
    return this.request<ApprovedActionOut>(`/approved-actions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteApprovedAction(id: string): Promise<void> {
    return this.request<void>(`/approved-actions/${id}`, { method: "DELETE" });
  }

  // The `(script_id, script_version)` pair comes from the ACTION'S OWN PIN and
  // is not in this body, so an exemption can never be granted for a version the
  // action does not name. Both routes return the ACTION, because granting or
  // revoking re-derives `destructive` on it.
  exemptApprovedActionScript(id: string, reason: string): Promise<ApprovedActionOut> {
    return this.request<ApprovedActionOut>(`/approved-actions/${id}/exempt-script`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
  }

  revokeApprovedActionScriptExemption(id: string): Promise<ApprovedActionOut> {
    return this.request<ApprovedActionOut>(`/approved-actions/${id}/exempt-script`, {
      method: "DELETE",
    });
  }

  // --- IPAM (backend/app/modules/ipam/api.py) ---------------------------
  //
  // All THIRTY-EIGHT routes of that router, derived from `api.py` itself rather
  // than from spec §9's table — the router is pinned by an exact-count test
  // (`tests/test_ipam_api.py::EXPECTED_ROUTE_COUNT = 38`) and is the contract.
  // `/ipam/sites` is gone (MSP clients phase 2): a VLAN's or a subnet's site is
  // one of its client's sites, `listClientSites`. Every create's body names its
  // client where nothing above it does (`modules/ipam/api.ts`).
  //
  // Three shapes here are NOT the obvious ones, and each is the API's choice:
  //   * `/drift` takes the class filter as `?class=`, aliased server-side away
  //     from the `drift_class` field name.
  //   * import preview/apply take the CSV as the RAW REQUEST BODY with the
  //     column mapping as a JSON-encoded QUERY parameter. §8.1 says multipart;
  //     this codebase has no multipart parser and two other modules already
  //     took raw bytes instead, so the shipped routes do too.
  //   * the three exports return `text/csv` through `PlainTextResponse`, not
  //     JSON, so they bypass `request<T>` exactly as the other binary/text
  //     downloads here do.

  listIpamVrfs(): Promise<IpamVrfOut[]> {
    return this.request<IpamVrfOut[]>("/ipam/vrfs");
  }

  createIpamVrf(body: IpamVrfCreate): Promise<IpamVrfOut> {
    return this.request<IpamVrfOut>("/ipam/vrfs", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  listIpamVlans(siteId?: string): Promise<IpamVlanOut[]> {
    const suffix = siteId ? `?site_id=${encodeURIComponent(siteId)}` : "";
    return this.request<IpamVlanOut[]>(`/ipam/vlans${suffix}`);
  }

  createIpamVlan(body: IpamVlanCreate): Promise<IpamVlanOut> {
    return this.request<IpamVlanOut>("/ipam/vlans", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  listIpamSubnets(filters: IpamSubnetFilters = {}): Promise<IpamSubnetOut[]> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<IpamSubnetOut[]>(`/ipam/subnets${suffix}`);
  }

  createIpamSubnet(body: IpamSubnetCreate): Promise<IpamSubnetOut> {
    return this.request<IpamSubnetOut>("/ipam/subnets", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getIpamSubnet(id: string): Promise<IpamSubnetOut> {
    return this.request<IpamSubnetOut>(`/ipam/subnets/${id}`);
  }

  updateIpamSubnet(id: string, body: IpamSubnetUpdate): Promise<IpamSubnetOut> {
    return this.request<IpamSubnetOut>(`/ipam/subnets/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  // `cascade` deletes the subnet's children too; without it a subnet with
  // children is refused.
  deleteIpamSubnet(id: string, cascade = false): Promise<void> {
    return this.request<void>(`/ipam/subnets/${id}${cascade ? "?cascade=true" : ""}`, {
      method: "DELETE",
    });
  }

  // 422 above the org's enumeration ceiling, and for every IPv6 prefix: one
  // rule, two applications. The caller renders the refusal, not a blank grid.
  listIpamSubnetAddresses(id: string, offset = 0, limit = 256): Promise<IpamGridEntryOut[]> {
    return this.request<IpamGridEntryOut[]>(
      `/ipam/subnets/${id}/addresses?offset=${offset}&limit=${limit}`,
    );
  }

  getIpamSubnetUtilization(id: string): Promise<IpamUtilizationOut> {
    return this.request<IpamUtilizationOut>(`/ipam/subnets/${id}/utilization`);
  }

  // ADVISORY: two operators who both call this race for the same address. The
  // non-racing path is `allocateIpamAddress`.
  getIpamSubnetNextFree(id: string, count = 1): Promise<string[]> {
    return this.request<string[]>(`/ipam/subnets/${id}/next-free?count=${count}`);
  }

  allocateIpamAddress(id: string, body: IpamAllocateRequest): Promise<IpamAddressOut> {
    return this.request<IpamAddressOut>(`/ipam/subnets/${id}/allocate`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  listIpamRanges(subnetId: string): Promise<IpamRangeOut[]> {
    return this.request<IpamRangeOut[]>(`/ipam/subnets/${subnetId}/ranges`);
  }

  createIpamRange(subnetId: string, body: IpamRangeCreate): Promise<IpamRangeOut> {
    return this.request<IpamRangeOut>(`/ipam/subnets/${subnetId}/ranges`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  deleteIpamRange(subnetId: string, rangeId: string): Promise<void> {
    return this.request<void>(`/ipam/subnets/${subnetId}/ranges/${rangeId}`, {
      method: "DELETE",
    });
  }

  getIpamAddress(id: string): Promise<IpamAddressOut> {
    return this.request<IpamAddressOut>(`/ipam/addresses/${id}`);
  }

  // `reserve: true` picks the RESERVE transition; otherwise this assigns.
  // There is no `state` field on the body, deliberately: the two transitions
  // have separate predicates and separate audit events.
  createIpamAddress(body: IpamAddressCreate): Promise<IpamAddressOut> {
    return this.request<IpamAddressOut>("/ipam/addresses", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateIpamAddress(id: string, body: IpamAddressUpdate): Promise<IpamAddressOut> {
    return this.request<IpamAddressOut>(`/ipam/addresses/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  releaseIpamAddress(id: string): Promise<IpamAddressOut> {
    return this.request<IpamAddressOut>(`/ipam/addresses/${id}/release`, { method: "POST" });
  }

  // A narrow projection of the hash-chained audit log, gated on `ipam.read`
  // alone. There is no `ipam_address_history` table.
  getIpamAddressHistory(id: string): Promise<IpamHistoryEntryOut[]> {
    return this.request<IpamHistoryEntryOut[]>(`/ipam/addresses/${id}/history`);
  }

  searchIpam(q: string): Promise<IpamSearchOut> {
    return this.request<IpamSearchOut>(`/ipam/search?q=${encodeURIComponent(q)}`);
  }

  // The class filter is `?class=`, NOT `?drift_class=`. `dormant` is excluded
  // from the default list and reachable only by naming it.
  listIpamDrift(filters: IpamDriftFilters = {}): Promise<IpamDriftOut[]> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<IpamDriftOut[]>(`/ipam/drift${suffix}`);
  }

  acknowledgeIpamDrift(id: string, note: string): Promise<IpamDriftOut> {
    return this.request<IpamDriftOut>(`/ipam/drift/${id}/acknowledge`, {
      method: "POST",
      body: JSON.stringify({ note }),
    });
  }

  // The note is REQUIRED server-side (`min_length=1`): a silenced finding with
  // no reason is how a drift list goes permanently quiet.
  suppressIpamDrift(id: string, until: string, note: string): Promise<IpamDriftOut> {
    return this.request<IpamDriftOut>(`/ipam/drift/${id}/suppress`, {
      method: "POST",
      body: JSON.stringify({ until, note }),
    });
  }

  listIpamOrphans(): Promise<IpamOrphanOut[]> {
    return this.request<IpamOrphanOut[]>("/ipam/orphans");
  }

  listIpamScanAuthorizations(includeRevoked = false): Promise<IpamScopeOut[]> {
    const suffix = includeRevoked ? "?include_revoked=true" : "";
    return this.request<IpamScopeOut[]>(`/ipam/scan-authorizations${suffix}`);
  }

  // HIGH PRIVILEGE (`ipam.scope.manage`): this grant decides where the platform
  // emits network traffic.
  createIpamScanAuthorization(body: IpamScopeCreate): Promise<IpamScopeOut> {
    return this.request<IpamScopeOut>("/ipam/scan-authorizations", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // Revocation STAMPS; the row stays, so the grant remains auditable.
  revokeIpamScanAuthorization(id: string): Promise<void> {
    return this.request<void>(`/ipam/scan-authorizations/${id}`, { method: "DELETE" });
  }

  // 202. Dispatches WITHIN ALREADY-AUTHORIZED SCOPE ONLY — it cannot create or
  // widen an authorization, which is a different permission and a different
  // person's decision.
  dispatchIpamSweep(subnetId: string): Promise<IpamSweepRunOut> {
    return this.request<IpamSweepRunOut>(`/ipam/subnets/${subnetId}/sweep`, { method: "POST" });
  }

  updateIpamSweepSettings(subnetId: string, body: IpamSweepSettings): Promise<IpamSubnetOut> {
    return this.request<IpamSubnetOut>(`/ipam/subnets/${subnetId}/sweep-settings`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  listIpamSweepRuns(subnetId: string, limit = 50): Promise<IpamSweepRunOut[]> {
    return this.request<IpamSweepRunOut[]>(
      `/ipam/subnets/${subnetId}/sweep-runs?limit=${limit}`,
    );
  }

  // Step one of two. WRITES NOTHING; the digest it returns is what `apply` must
  // echo, which is the only thing binding the two steps to one file.
  // `target` is where the sheet goes: a VRF, whose client is the import's, or
  // a client's `global` VRF. The server refuses an import that names neither.
  async previewIpamImport(
    file: File,
    mapping: Record<string, string>,
    target: IpamImportTarget,
  ): Promise<IpamImportPreviewOut> {
    return this.ipamImportRequest<IpamImportPreviewOut>(
      `/ipam/import/preview?mapping=${encodeURIComponent(JSON.stringify(mapping))}` +
        this.ipamImportTargetQuery(target),
      file,
    );
  }

  // Step two. The digest is REQUIRED and must be the one preview returned for
  // this exact file -- into the same target.
  async applyIpamImport(
    file: File,
    mapping: Record<string, string>,
    digest: string,
    target: IpamImportTarget,
  ): Promise<IpamImportResultOut> {
    return this.ipamImportRequest<IpamImportResultOut>(
      `/ipam/import/apply?mapping=${encodeURIComponent(JSON.stringify(mapping))}` +
        `&digest=${encodeURIComponent(digest)}` +
        this.ipamImportTargetQuery(target),
      file,
    );
  }

  // Where the import goes, as the query string names it: `vrf_id` or `client_id`.
  private ipamImportTargetQuery(target: IpamImportTarget): string {
    return "vrfId" in target
      ? `&vrf_id=${encodeURIComponent(target.vrfId)}`
      : `&client_id=${encodeURIComponent(target.clientId)}`;
  }

  // The CSV goes up as the RAW BODY (`_read_csv` -> `read_bounded_body`), not
  // as multipart: this codebase has no multipart parser and the shipped route
  // takes raw bytes, mirroring uploadExpenseAttachment's pattern.
  private async ipamImportRequest<T>(path: string, file: File): Promise<T> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("POST", path));
    headers.set("Content-Type", "text/csv");
    const response = await fetch(`${this.base}${path}`, { method: "POST", headers, body: file });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return (await response.json()) as T;
  }

  // `dry_run` DEFAULTS TO TRUE server-side and the default here matches: a call
  // with no argument reports what WOULD convert and writes nothing.
  convertIpamDocRecords(dryRun = true): Promise<IpamDocRecordConversionOut> {
    return this.request<IpamDocRecordConversionOut>(
      `/ipam/import/doc-records?dry_run=${dryRun ? "true" : "false"}`,
      { method: "POST" },
    );
  }

  exportIpamSubnetsCsv(): Promise<string> {
    return this.ipamCsv("/ipam/export/subnets.csv");
  }

  exportIpamAddressesCsv(): Promise<string> {
    return this.ipamCsv("/ipam/export/addresses.csv");
  }

  // `dormant` IS EXCLUDED from this export. An export called "drift" that
  // contained it would be the mixed list nobody opens twice.
  exportIpamDriftCsv(includeResolved = false): Promise<string> {
    return this.ipamCsv(
      `/ipam/export/drift.csv${includeResolved ? "?include_resolved=true" : ""}`,
    );
  }

  // text/csv, not JSON — bypasses `request<T>` for the same reason the binary
  // downloads above do.
  private async ipamCsv(path: string): Promise<string> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("GET", path));
    const response = await fetch(`${this.base}${path}`, { headers });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    return response.text();
  }

  // ------------------------------------------------------------------------
  // AI admin agent (spec §6.1). Eleven routes, every one `require_permission`-
  // gated server-side; nothing below decides anything.
  //
  // THE SSE STREAM IS NOT HERE ON PURPOSE. §6: the `POST` is authoritative and
  // returns the complete answer, and the `GET .../stream` is an optional
  // side-channel a client may open to watch deltas arrive. A client that never
  // opens it loses nothing, so the client this console ships does not.
  // ------------------------------------------------------------------------

  getAiStatus(): Promise<AiStatus> {
    return this.request<AiStatus>("/ai/status");
  }

  listAiSessions(limit = 50): Promise<AiSessionOut[]> {
    return this.request<AiSessionOut[]>(`/ai/sessions?limit=${limit}`);
  }

  createAiSession(): Promise<AiSessionOut> {
    return this.request<AiSessionOut>("/ai/sessions", { method: "POST" });
  }

  getAiSession(id: string): Promise<AiTranscriptOut> {
    return this.request<AiTranscriptOut>(`/ai/sessions/${id}`);
  }

  deleteAiSession(id: string): Promise<void> {
    return this.request<void>(`/ai/sessions/${id}`, { method: "DELETE" });
  }

  createAiTurn(sessionId: string, body: AiTurnRequest): Promise<AiTurnOut> {
    return this.request<AiTurnOut>(`/ai/sessions/${sessionId}/turns`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /** Settings → AI connections. `mcp.use` server-side. */
  getMcpConnections(): Promise<McpConnectionsOut> {
    return this.request<McpConnectionsOut>("/mcp/connections");
  }

  /**
   * Revoke an AI app for the caller or for the whole organization. The client
   * id is whatever the app registered, so it is encoded as ONE path segment:
   * a `/` or `?` in it must never become path or query.
   *
   * FOR EVERYONE IT IS GLOBAL, BY ITS BODY (phase 3 design §1.2): the server
   * refuses `scope: "everyone"` narrowed by `X-Client-Id` (403
   * ALL_CLIENTS_REQUIRED), and `globalRoutes.json` cannot tell the two scopes
   * apart, so this request says it. `scope: "me"` is narrowed like any other.
   */
  revokeMcpConnection(clientId: string, scope: McpRevokeScope): Promise<McpRevokeOut> {
    return this.request<McpRevokeOut>(
      `/mcp/connections/${encodeURIComponent(clientId)}/revoke`,
      { method: "POST", body: JSON.stringify({ scope }) },
      { global: scope === "everyone" },
    );
  }

  getAiSettings(): Promise<AiSettingsOut> {
    return this.request<AiSettingsOut>("/ai/settings");
  }

  updateAiSettings(body: AiSettingsUpdate): Promise<AiSettingsOut> {
    return this.request<AiSettingsOut>("/ai/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  listAiProviders(): Promise<AiProviderOut[]> {
    return this.request<AiProviderOut[]>("/ai/providers");
  }

  listAiInjectionFlags(limit = 100): Promise<AiInjectionFlagOut[]> {
    return this.request<AiInjectionFlagOut[]>(`/ai/injection-flags?limit=${limit}`);
  }

  // --- Act mode (docs/modules/ai-automation.md §6, §12) -------------------
  //
  // **THE CONFIRM BODY IS A DIGEST AND NOTHING ELSE.** No job type, no params,
  // no target: everything dispatched is read off the stored `ai_action` row, and
  // the server refuses any other field BY NAME (`extra="forbid"`). A method here
  // that took a job type would be a method whose existence contradicts §0.7.

  listAiActions(sessionId?: string, limit = 50): Promise<AiActionOut[]> {
    const query = sessionId ? `?session_id=${sessionId}&limit=${limit}` : `?limit=${limit}`;
    return this.request<AiActionOut[]>(`/ai/actions${query}`);
  }

  getAiAction(id: string): Promise<AiActionPreviewOut> {
    return this.request<AiActionPreviewOut>(`/ai/actions/${id}`);
  }

  confirmAiAction(id: string, body: AiActionConfirm): Promise<AiActionDispatched> {
    return this.request<AiActionDispatched>(`/ai/actions/${id}/confirm`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  rejectAiAction(id: string, body: AiActionReject): Promise<AiActionOut> {
    return this.request<AiActionOut>(`/ai/actions/${id}/reject`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // --- The other two personas' surfaces (§12) ----------------------------
  //
  // Separate methods per persona rather than one parameterised call. The three
  // surfaces have DIFFERENT principals and different response shapes -- the
  // resolver's answer deliberately carries nothing about the platform, because
  // it is read by a customer -- and one method with a persona argument is how
  // a staff response shape ends up rendered on a portal page.

  createCopilotSession(ticketId: string, mode: "read" | "act"): Promise<AiCopilotSession> {
    return this.request<AiCopilotSession>(`/service-desk/tickets/${ticketId}/ai/sessions`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
  }

  createCopilotTurn(
    ticketId: string,
    sessionId: string,
    question: string,
  ): Promise<AiCopilotTurn> {
    return this.request<AiCopilotTurn>(
      `/service-desk/tickets/${ticketId}/ai/sessions/${sessionId}/turns`,
      { method: "POST", body: JSON.stringify({ question }) },
    );
  }

  createResolverSession(): Promise<AiResolverSession> {
    return this.request<AiResolverSession>("/portal/ai/sessions", { method: "POST" });
  }

  createResolverTurn(sessionId: string, question: string): Promise<AiResolverTurn> {
    return this.request<AiResolverTurn>(`/portal/ai/sessions/${sessionId}/turns`, {
      method: "POST",
      body: JSON.stringify({ question }),
    });
  }

  listAiCeilings(): Promise<AiCeilingOut[]> {
    return this.request<AiCeilingOut[]>("/ai/autonomy");
  }

  setAiCeiling(body: AiCeilingUpdate): Promise<AiCeilingOut> {
    return this.request<AiCeilingOut>("/ai/autonomy", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  // --- Reporting & analytics (backend/app/modules/reporting/api.py) ------
  //
  // Two routes per report definition plus the catalogue, all generated
  // server-side from the registry. Nothing here enumerates report keys: the
  // catalogue is the enumeration, and a key added to the backend registry
  // shows up in this UI without a frontend change.

  listReports(): Promise<ReportCatalogue> {
    return this.request<ReportCatalogue>("/reports");
  }

  runReport(key: string, params: ReportParams = {}): Promise<ReportPage> {
    const query = new URLSearchParams(params).toString();
    return this.request<ReportPage>(`/reports/${key}${query ? `?${query}` : ""}`);
  }

  // Streams the CSV export. Not routed through `request` (the body is
  // text/csv, not JSON), and it CARRIES THE SAME `Authorization` HEADER as
  // every other call -- mirrors `exportPatchReport`/`getRemoteRecording`. A
  // bare `<a href>` to the export path would send no bearer token at all and
  // land on a 401, which is why this is a fetch and not a link.
  async exportReport(key: string, params: ReportParams = {}): Promise<Blob> {
    const headers = requestHeaders(this.getToken(), undefined, this.route("GET", `/reports/${key}/export.csv`));
    const query = new URLSearchParams(params).toString();
    const response = await fetch(
      `${this.base}/reports/${key}/export.csv${query ? `?${query}` : ""}`,
      { headers },
    );
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      // A 413 lands here with its typed body intact: the server refuses an
      // over-cap export rather than truncating it (spec §0.8), and the caller
      // needs the message to know what to narrow.
      throw new ApiError(response.status, await response.text());
    }
    return response.blob();
  }

  // ------------------------------------------------------------------------
  // Site servers (docs/modules/site-server/spec.md §11)
  //
  // A site server IS a promoted `Device`, so there is no separate device
  // registry here -- `listDevices()` above is what the promote dialog offers
  // from, filtered to devices that have an agent.
  //
  // `wake` returns 202 with the SERVER-COMPUTED broadcast address. The client
  // never supplies one and cannot influence it beyond naming which declared
  // segment the target is on: the control plane picks the containing segment
  // and computes the address, so the endpoint never decides what it may touch.
  // ------------------------------------------------------------------------

  listSiteServers(filters: SiteServerFilters = {}): Promise<SiteServerOut[]> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value != null && value !== "") query.set(key, String(value));
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return this.request<SiteServerOut[]>(`/site-servers${suffix}`);
  }

  getSiteServer(id: string): Promise<SiteServerOut> {
    return this.request<SiteServerOut>(`/site-servers/${id}`);
  }

  promoteSiteServer(body: SiteServerCreate): Promise<SiteServerOut> {
    return this.request<SiteServerOut>("/site-servers", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  updateSiteServer(id: string, body: SiteServerUpdate): Promise<SiteServerOut> {
    return this.request<SiteServerOut>(`/site-servers/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  demoteSiteServer(id: string, reason = ""): Promise<void> {
    const suffix = reason ? `?reason=${encodeURIComponent(reason)}` : "";
    return this.request<void>(`/site-servers/${id}${suffix}`, { method: "DELETE" });
  }

  getSiteServerSegmentCheck(id: string): Promise<SegmentCheckOut> {
    return this.request<SegmentCheckOut>(`/site-servers/${id}/segment-check`);
  }

  wakeViaSiteServer(id: string, body: SiteWakeRequest): Promise<SiteWakeOut> {
    return this.request<SiteWakeOut>(`/site-servers/${id}/wake`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  // ------------------------------------------------------------------------
  // Platform status (gated on `platform.status.read`; app/api/v1/platform.py)
  // ------------------------------------------------------------------------

  getPlatformStatus(): Promise<PlatformStatus> {
    return this.request<PlatformStatus>("/platform/status");
  }

  // ------------------------------------------------------------------------
  // Error monitor (gated on `platform.errors.read` / `platform.errors.write`;
  // app/api/v1/errors.py)
  // ------------------------------------------------------------------------

  listErrorGroups(limit?: number): Promise<ErrorGroupList> {
    const query = limit != null ? `?limit=${limit}` : "";
    return this.request<ErrorGroupList>(`/platform/errors${query}`);
  }

  getErrorGroup(groupId: string, limit?: number): Promise<ErrorGroupDetail> {
    const query = limit != null ? `?limit=${limit}` : "";
    return this.request<ErrorGroupDetail>(`/platform/errors/${groupId}${query}`);
  }

  resolveErrorGroup(groupId: string): Promise<ErrorGroup> {
    return this.request<ErrorGroup>(`/platform/errors/${groupId}/resolve`, { method: "POST" });
  }

  /**
   * The inverse of `resolveErrorGroup`, on the SAME permission code.
   *
   * Undoing triage puts a group back in front of whoever is on call, so it is
   * not a lesser privilege than doing it — `platform.errors.write` gates both.
   * Reopening an already-open group answers 200 and is still audited; the
   * console does not lean on that (it offers the control only on a resolved
   * group) but a double-click cannot produce an error either.
   */
  unresolveErrorGroup(groupId: string): Promise<ErrorGroup> {
    return this.request<ErrorGroup>(`/platform/errors/${groupId}/unresolve`, { method: "POST" });
  }
}

// "screen" is the only protocol the console starts; the guacd ones are
// retired server-side and stay here only because old session rows carry them.
export type RemoteProtocol = "screen" | "rdp" | "vnc" | "ssh";
export type RemoteMode = "attended" | "unattended";

// Server-side state machine (app/modules/remote/service.py):
// awaiting_consent -> connecting -> active -> ended (terminal, reachable from
// every live state via deny/error/timeout/normal completion).
export type RemoteSessionState = "awaiting_consent" | "connecting" | "active" | "ended";

export interface RemoteSessionFilters {
  state?: string;
  device_id?: string;
  limit?: number;
  offset?: number;
}

export interface RemoteSessionCreateRequest {
  device_id: string;
  protocol: RemoteProtocol;
  mode?: RemoteMode;
  credential_id?: string;
  reason: string;
}

export interface RemoteSessionCreateResponse {
  session_id: string;
  state: string;
  consent_mode: string;
}

// Mirrors SessionOut (backend/app/modules/remote/api.py). No token, no secret
// material — only credential_id and non-sensitive metadata.
export interface RemoteSession {
  id: string;
  device_id: string;
  technician_id: string;
  protocol: string;
  consent_mode: string;
  state: string;
  credential_id: string | null;
  has_recording: boolean;
  started_at: string | null;
  ended_at: string | null;
  end_reason: string | null;
  created_at: string;
  /** Screen sessions: whether the technician may take control (additive). */
  input_enabled?: boolean;
}

export type {
  PushAuthorizationCreate,
  PushAuthorizationOut,
  PushCredentialScopeBind,
  PushCredentialScopeOut,
} from "../modules/agent-push/types";

export interface Credential {
  id: string;
  name: string;
  type: string;
  device_group_id: string | null;
  /** The client the credential serves; null is every client's. A push credential always has one. */
  client_id: string | null;
  created_at: string | null;
  last_used_at: string | null;
  has_secret: boolean;
}

export interface DocArticleListItem {
  id: string;
  /** The client it belongs to; `null` for every client's (global). */
  client_id: string | null;
  title: string;
  category: string;
  archived: boolean;
  // Readable by requesters in the portal. Absent from older servers.
  portal_visible?: boolean;
  updated_at: string;
  link_count: number;
}

export interface DocLink {
  id: string;
  target_type: string;
  target_id: string;
}

export interface DocArticle {
  id: string;
  /** The client it belongs to; `null` for every client's (global). */
  client_id: string | null;
  title: string;
  body: string;
  category: string;
  archived: boolean;
  // Readable by requesters in the portal. Absent from older servers.
  portal_visible?: boolean;
  current_version: number;
  updated_at: string;
  links: DocLink[];
}

export interface DocArticleInput {
  title: string;
  body: string;
  category: string;
}

/**
 * A new article, record or document names its client; without one it is
 * every client's (global), which only someone with access to every client may
 * create. Set once at creation: the edit bodies have no client.
 */
export interface NewDocClient {
  client_id?: string;
}

export interface DocVersion {
  version: number;
  snapshot: Record<string, unknown>;
  changed_by: string | null;
  ts: string;
}

export interface DocRecordOut {
  id: string;
  /** The client it belongs to; `null` for every client's (global). */
  client_id: string | null;
  record_type: string;
  name: string;
  fields: Record<string, unknown>;
  archived: boolean;
  current_version: number;
}

export interface DocSearchHit {
  // `document` is the third kind (spec-attachments.md §8): one more `kind` on
  // the SAME route with the SAME shape, so `groupHits` gains a group and the
  // console gains no second query.
  kind: "article" | "record" | "document";
  id: string;
  title: string;
  category: string;
  rank: number;
}

/**
 * One uploaded file. Every field here is SERVER-DERIVED.
 *
 * `content_type` and `viewer` are sniffed from the leading bytes
 * (`filetypes.py`), never from the filename and never from anything the
 * client sent -- there is no field in the upload request in which a content
 * type could arrive (§0.5). `viewer` is the single key the console's render
 * map is allowed to use (§0.4 wall 3, `modules/docs/viewer.ts`); rendering off
 * `filename`, off an extension, or off a response header is the stored-XSS
 * this module exists to prevent.
 *
 * `purged_at` non-null is a TOMBSTONE: the bytes were destroyed on purpose and
 * the filename, size and digest survive to say what was destroyed. The content
 * route answers 410 for one, never 404 -- "deliberately destroyed" and "never
 * existed" are different facts and only one of them is worth keeping.
 */
export interface DocDocumentVersion {
  version: number;
  filename: string;
  byte_size: number;
  content_type: string;
  viewer: string;
  sha256: string;
  created_at: string;
  uploaded_by: string | null;
  purged_at: string | null;
}

export interface DocDocumentListItem {
  id: string;
  /** The client it belongs to; `null` for every client's (global). */
  client_id: string | null;
  title: string;
  description: string;
  category: string;
  archived: boolean;
  /** 0 for a document whose file has not been uploaded yet — a legal state
   *  (§4.1), rendered as "No file uploaded yet" rather than as an error. */
  current_version: number;
  updated_at: string;
  link_count: number;
}

export interface DocDocument {
  id: string;
  /** The client it belongs to; `null` for every client's (global). */
  client_id: string | null;
  title: string;
  description: string;
  category: string;
  archived: boolean;
  current_version: number;
  updated_at: string;
  current: DocDocumentVersion | null;
  links: DocLink[];
}

export interface DocDocumentInput {
  title: string;
  description: string;
  category: string;
}

export interface SecurityEventFilters {
  device_id?: string;
  username?: string;
  event_type?: string;
  from_ts?: string;
  to_ts?: string;
  limit?: number;
  offset?: number;
}

export interface SecurityEvent {
  id: string;
  device_id: string;
  hostname: string;
  ts: string;
  event_type: string;
  username: string;
  source: string;
  outcome: string;
}

export interface SecurityEventDetail extends SecurityEvent {
  raw: Record<string, unknown>;
}

export interface HardwareComponent {
  type: string;
  identifier: string;
  attributes: Record<string, unknown>;
}

export interface InstalledSoftwareItem {
  name: string;
  version: string;
  publisher: string;
  install_date: string | null;
}

export interface DeviceInventory {
  summary: Record<string, unknown>;
  hardware: HardwareComponent[];
  software: InstalledSoftwareItem[];
  facts: Record<string, unknown>;
  snapshot_taken_at: string | null;
}

export interface InventoryChange {
  id: string;
  ts: string;
  change_type: string;
  subject: string;
  from_value: string | null;
  to_value: string | null;
}

export interface SoftwareAggregate {
  name: string;
  version: string;
  publisher: string;
  device_count: number;
}

export interface SoftwareDevice {
  device_id: string;
  hostname: string;
  version: string;
}
