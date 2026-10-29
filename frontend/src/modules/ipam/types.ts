// Mirrors backend/app/modules/ipam/api.py's request/response models exactly
// (snake_case keys, matching the JSON). THE SHIPPED API IS THE CONTRACT: these
// types were written against `api.py`'s routes (38 since MSP clients phase 2)
// and its Pydantic models directly, not against spec §9's prose, and where the
// two disagree the API wins.
//
// Per invariant #5 nothing here carries `org_id`; per invariant #6 nothing here
// is a permission — the server's `require_permission` on every one of those 38
// routes is the only authority, and the permission strings this module reads
// out of `/me` decide only what RENDERS.
//
// THE FOUR DISTINCTIONS THIS FILE REFUSES TO COLLAPSE, because collapsing any
// of them produces a UI that misstates what the module knows:
//
//   1. `state` AND `status` ARE DIFFERENT FIELDS (§0.6). `state` is what a
//      HUMAN asserted and is one of four values; `status` is DERIVED at read
//      time from (state, ranges, observations, gates) and is one of nine. They
//      arrive as two separate keys on `GridEntryOut` and are typed separately
//      here so no component can pass one where the other belongs.
//   2. `last_seen_at` IS DOCUMENTATION CURRENCY, NOT LIVENESS (§0.10). It is
//      the timestamp of the most recent evidence-producing event. There is no
//      `up`/`down` field anywhere in this contract and no RTT — deliberately,
//      §0.10 point 5 — and `ipamState.formatAge` is the only thing allowed to
//      render it.
//   3. `UtilizationOut.percentage` IS NULLABLE (§0.4, §0.5). Above the
//      enumeration ceiling and for every IPv6 prefix it is `null` while the
//      COUNTS still hold. `null` means "unavailable", never zero.
//   4. `dormant` IS NOT IN `DRIFT_CLASSES` (§4.3, reconcile.py). It is a
//      bucket, not a finding, and it is excluded from `/drift`'s default list,
//      from the count and from `export/drift.csv`.

// --------------------------------------------------------------------------
// Permission codes (spec §11.2) — presentation only
// --------------------------------------------------------------------------
//
// Named as constants so a typo cannot silently hide a control forever, and so
// the high-privilege pair is visible in one place. NONE of these gates
// anything: the server re-checks every one (invariant #6).

export const IPAM_READ = "ipam.read";
export const IPAM_ASSIGN = "ipam.assign";
export const IPAM_MANAGE = "ipam.manage";
export const IPAM_SWEEP_RUN = "ipam.sweep.run";
export const IPAM_SWEEP_MANAGE = "ipam.sweep.manage";
/** HIGH PRIVILEGE. Decides where this platform emits network traffic (§0.12). */
export const IPAM_SCOPE_MANAGE = "ipam.scope.manage";

// --------------------------------------------------------------------------
// Registry — VRF, VLAN
// --------------------------------------------------------------------------
//
// EVERY ROW IS A CLIENT'S (MSP clients phase 2, migration 0240). A create takes
// its client from its VRF, or a VLAN from its site; `client_id` is sent only
// when there is neither (`api.ts` adds it), and every output names it. There
// are no IPAM sites any more: a VLAN's or a subnet's site is one of its
// client's sites (`ClientSiteOut`, `GET /clients/{id}/sites`).

export interface VrfCreate {
  /** The client the VRF belongs to: nothing above a VRF could say. */
  client_id: string;
  name: string;
  rd?: string | null;
  description?: string;
}

export interface VrfOut {
  id: string;
  client_id: string;
  name: string;
  rd: string | null;
  description: string;
}

export interface VlanCreate {
  vid: number;
  /** One of the client's sites; its client is the VLAN's. */
  site_id?: string | null;
  /** Only for a VLAN with no site. */
  client_id?: string | null;
  name?: string;
  description?: string;
}

export interface VlanOut {
  id: string;
  client_id: string;
  vid: number;
  /** NULL is a VLAN of the WHOLE CLIENT, not a missing site: VLAN 10
   * legitimately exists at every site, and once per client without one (§0.7). */
  site_id: string | null;
  name: string;
  description: string;
}

// --------------------------------------------------------------------------
// Subnets
// --------------------------------------------------------------------------

/** `container` prefixes exist so the tree reads; they hold no addresses and
 * have no utilisation, no grid and no allocator (`allocation._assignable`). */
export type SubnetKind = "container" | "assignable";

export const SUBNET_KINDS: readonly SubnetKind[] = ["container", "assignable"];

export interface SubnetCreate {
  prefix: string;
  vrf_id?: string | null;
  /** Only with no `vrf_id`: the subnet goes into this client's `global` VRF. */
  client_id?: string | null;
  kind?: SubnetKind;
  name?: string;
  description?: string;
  site_id?: string | null;
  vlan_id?: string | null;
  gateway?: string | null;
  dns_servers?: string[] | null;
}

// `prefix`, `vrf_id` and `parent_id` are ABSENT for three different reasons
// (api.py's SubnetUpdate): `parent_id` is DERIVED from containment and a
// client-supplied value is exactly the state deriving it exists to make
// unrepresentable; `prefix` and `vrf_id` are the row's identity.
export interface SubnetUpdate {
  name?: string;
  description?: string;
  site_id?: string | null;
  vlan_id?: string | null;
  gateway?: string | null;
  dns_servers?: string[] | null;
}

export interface SubnetOut {
  id: string;
  /** Its VRF's client. */
  client_id: string;
  vrf_id: string;
  prefix: string;
  kind: string;
  name: string;
  description: string;
  site_id: string | null;
  vlan_id: string | null;
  gateway: string | null;
  dns_servers: string[];
  /** A CACHE of "the smallest other prefix in the same VRF containing this
   * one" (§0.2), recomputed server-side on every write. The tree is built from
   * it; nobody sets it. */
  parent_id: string | null;
  sweep_enabled: boolean;
  sweep_device_id: string | null;
  sweep_interval_hours: number | null;
  /** Documentation currency, never liveness (§0.10). Rendered as an AGE. */
  last_swept_at: string | null;
}

export type RangeKind = "dhcp_pool" | "reserved" | "excluded";

export const RANGE_KINDS: readonly RangeKind[] = ["dhcp_pool", "reserved", "excluded"];

export interface RangeCreate {
  kind: RangeKind;
  start_address: string;
  end_address: string;
  name?: string;
  description?: string;
}

export interface RangeOut {
  id: string;
  client_id: string;
  subnet_id: string;
  kind: string;
  start_address: string;
  end_address: string;
  name: string;
}

// --------------------------------------------------------------------------
// Utilisation and the grid
// --------------------------------------------------------------------------

/**
 * `allocation.Utilization`. Above §0.4's ceiling — and for EVERY IPv6 prefix,
 * §0.5 — `enumerable` is false and `capacity`, `free`, `used` and `percentage`
 * are all `null` while `assigned`/`reserved`/`deprecated`/`observed` still
 * hold. A renderer that coerced `percentage` to 0 would print "empty" where
 * the honest answer is "unknown".
 */
export interface UtilizationOut {
  subnet_id: string;
  prefix: string;
  enumerable: boolean;
  capacity: number | null;
  free: number | null;
  used: number | null;
  percentage: number | null;
  assigned: number;
  reserved: number;
  deprecated: number;
  observed: number;
}

/** `ipam_address.state` — what a HUMAN asserted (§0.6). Four values, and
 * `in_use`/`stale`/`conflicting` are deliberately NOT among them. */
export type AddressState = "free" | "reserved" | "assigned" | "deprecated";

export const ADDRESS_STATES: readonly AddressState[] = [
  "free",
  "reserved",
  "assigned",
  "deprecated",
];

/** The DERIVED status (§3.1's nine-row table, `allocation.derive_status`).
 * Computed server-side at read time and stored nowhere. */
export type AddressStatus =
  | "dhcp"
  | "conflicting"
  | "in_use"
  | "stale"
  | "assigned"
  | "dormant"
  | "reserved"
  | "undocumented"
  | "free";

export const ADDRESS_STATUSES: readonly AddressStatus[] = [
  "dhcp",
  "conflicting",
  "in_use",
  "stale",
  "assigned",
  "dormant",
  "reserved",
  "undocumented",
  "free",
];

export interface GridEntryOut {
  address: string;
  /** DERIVED (`derive_status`). Not `state`. */
  status: string;
  /** DOCUMENTED INTENT, or `null` when there is no `ipam_address` row at all —
   * which §0.4's sparseness makes the ordinary case. Not `status`. */
  state: string | null;
  address_id: string | null;
  device_id: string | null;
  asset_label: string;
  mac: string | null;
  hostname: string;
  /** Evidence currency (§0.10). Render as an age, never as a timestamp. */
  last_seen_at: string | null;
  range_kinds: string[];
  /**
   * §3.2, DERIVED server-side at read time (`allocation.derive_allocation_
   * method`) exactly as `status` is, and stored nowhere.
   *
   * NOT the same field as `AddressOut.allocation_method`. That one is the
   * COLUMN — what an operator asserted, or `unknown` if they never did. This one
   * is the answer: the operator's assertion when there is one, otherwise the
   * allocation origin the endpoint itself reported on its NIC (§14.1), otherwise
   * `unknown`.
   */
  allocation_method: string;
  /**
   * Which of those answered: `documented` | `observed` | `unknown`.
   *
   * RENDER IT. §0.6's thesis is that "a human asserted this" and "the fleet
   * reported this" are different facts, and a cell showing `dhcp` without saying
   * which is how an operator concludes somebody edited their record.
   */
  allocation_method_source: string;
}

export const MAX_NEXT_FREE = 256;

export interface AllocateRequest {
  device_id?: string | null;
  asset_label?: string;
  hostname?: string;
  description?: string;
  allocation_method?: string;
}

export type AllocationMethod = "static" | "dhcp" | "slaac" | "discovered" | "unknown";

export const ALLOCATION_METHODS: readonly AllocationMethod[] = [
  "static",
  "dhcp",
  "slaac",
  "discovered",
  "unknown",
];

// --------------------------------------------------------------------------
// Addresses
// --------------------------------------------------------------------------

/**
 * `state` IS ABSENT FROM THIS BODY, deliberately (api.py's AddressCreate,
 * §0.6): reserve and assign are separate transitions with separate predicates
 * and separate audit events, and `reserve: true` is how the caller picks
 * between them. A `state` field here would be a bypass of both.
 */
export interface AddressCreate {
  address: string;
  vrf_id?: string | null;
  /** Only with no `vrf_id`: the address is looked up in this client's `global` VRF. */
  client_id?: string | null;
  reserve?: boolean;
  device_id?: string | null;
  asset_label?: string;
  hostname?: string;
  description?: string;
  mac?: string | null;
  allocation_method?: string;
}

export interface AddressUpdate {
  hostname?: string;
  description?: string;
  asset_label?: string;
  allocation_method?: string;
}

export interface AddressOut {
  id: string;
  client_id: string;
  vrf_id: string;
  subnet_id: string | null;
  address: string;
  state: string;
  /** Who created the row: an operator, or ingestion. Not a state. */
  origin: string;
  allocation_method: string;
  device_id: string | null;
  asset_label: string;
  mac: string | null;
  hostname: string;
  description: string;
  assigned_at: string | null;
  released_at: string | null;
}

/**
 * `GET /addresses/{id}/history` — a NARROW projection of the hash-chained
 * audit log (§0.13), gated on `ipam.read` alone. There is no
 * `ipam_address_history` table.
 */
export interface HistoryEntryOut {
  ts: string;
  actor_type: string;
  actor_id: string | null;
  action: string;
  from_state: string | null;
  to_state: string | null;
  /**
   * `AuditEvent.meta` minus `from_state`/`to_state` — those are columns of this
   * row already (`api.HistoryEntryOut`, `addresses.AddressHistoryEntry`, both
   * `dict[str, Any]`).
   *
   * PART OF THE CONTRACT, NOT A DEBUG EXTRA (§0.13, §11.3): `ipam.address_assigned`
   * carries the claimant and the allocation method, `ipam.address_released` the
   * previous claimant. Without them the history says an address changed state
   * and refuses to say who to — the one question this panel exists to answer.
   *
   * The KEYS VARY BY ACTION and the server may grow more, so this is
   * `Record<string, unknown>` — the same shape `DriftOut.detail` uses, which is
   * why the two are rendered by one engine (`describeHistoryDetail` and
   * `describeDriftDetail` differ only in their rule sets). Unrecognised keys are
   * rendered, never dropped.
   */
  detail: Record<string, unknown>;
}

/** `kind` says HOW `q` WAS INTERPRETED (`addresses.SearchResult`), because an
 * operator who typed a hostname that happens to parse as something else needs
 * to see which question was answered. */
export type SearchKind = "address" | "cidr" | "mac" | "text";

export interface SearchOut {
  kind: string;
  query: string;
  subnets: string[];
  addresses: string[];
  observations: number;
  devices: string[];
}

// --------------------------------------------------------------------------
// Drift and orphans
// --------------------------------------------------------------------------

/**
 * `reconcile.DRIFT_CLASSES`, in spec order. `dormant` IS NOT HERE — it is a
 * bucket, not a finding, and `list_drift` excludes it from the default list.
 */
export type DriftClass =
  | "documented_absent"
  | "present_undocumented"
  | "mac_mismatch"
  | "claimant_mismatch"
  | "device_moved"
  | "conflict";

export const DRIFT_CLASSES: readonly DriftClass[] = [
  "documented_absent",
  "present_undocumented",
  "mac_mismatch",
  "claimant_mismatch",
  "device_moved",
  "conflict",
];

/** `reconcile.DORMANT_CLASS`. Fetchable only by naming it (`?class=dormant`),
 * and outside every count and export. */
export const DORMANT_CLASS = "dormant";

export type DriftSeverity = "low" | "medium" | "high";

export type DriftConfidence = "low" | "medium" | "high";

export interface DriftOut {
  id: string;
  client_id: string;
  vrf_id: string;
  subnet_id: string | null;
  address: string;
  drift_class: string;
  severity: string;
  confidence: string;
  /**
   * The evidence, verbatim from the reconcile pass. Shown, never summarised
   * away — an unexplainable finding is one nobody acts on.
   *
   * FREE-FORM AND CLASS-DEPENDENT (`reconcile._evaluate`): `macs` and
   * `vrf_count` on a conflict, `documented_mac`/`observed_macs` on a mismatch,
   * `documented_at`/`observed_at` — ADDRESSES, not instants — on a move,
   * `gate_e`/`gate_l` on an absence. `describeDriftDetail` renders it as facts,
   * keyed off which keys are present, so a class that grows a key stays
   * readable.
   */
  detail: Record<string, unknown>;
  device_id: string | null;
  first_detected_at: string;
  last_evaluated_at: string;
  /** A finding speaks only at 2 (§0.8): one sweep that ran while a switch was
   * rebooting is not news. */
  consecutive_detections: number;
  resolved_at: string | null;
  acknowledged_at: string | null;
  suppressed_until: string | null;
  note: string;
}

export interface DriftFilters {
  /** Sent as `?class=` — the query parameter is aliased server-side. */
  class?: string;
  severity?: string;
  subnet?: string;
  include_resolved?: boolean;
}

export interface AcknowledgeRequest {
  note?: string;
}

export interface SuppressRequest {
  until: string;
  /** REQUIRED server-side, `min_length=1`: a silenced finding with no reason is
   * how a drift list becomes permanently quiet. */
  note: string;
}

/**
 * `observations.OrphanGroup` — observed addresses inside NO registered subnet,
 * grouped by inferred /24 (IPv4) or /64 (IPv6).
 *
 * NOT DRIFT (§4.2): nothing is wrong with the plan, the plan is ABSENT. Its own
 * list, its own tab, outside the drift count.
 */
export interface OrphanOut {
  prefix: string;
  count: number;
  last_seen_at: string;
}

// --------------------------------------------------------------------------
// Sweep governance — the high-privilege surface (§6.1, §11.2)
// --------------------------------------------------------------------------

export interface ScopeCreate {
  prefix: string;
  vrf_id?: string | null;
  /** Only with no `vrf_id`: the authorization is for this client's `global` VRF. */
  client_id?: string | null;
  note?: string;
  expires_at?: string | null;
}

/**
 * One `ipam_scan_authorization`. Rows are NEVER hard-deleted: revocation stamps
 * `revoked_at` so "who authorized probing that segment in March" stays
 * answerable.
 */
export interface ScopeOut {
  id: string;
  client_id: string;
  vrf_id: string;
  prefix: string;
  note: string;
  /** NOT NULL server-side: there is no system-authorized scope. A human with
   * `ipam.scope.manage` put their name on every packet this module emits. */
  authorized_by: string;
  authorized_at: string;
  expires_at: string | null;
  revoked_at: string | null;
}

export interface SweepSettings {
  sweep_enabled: boolean;
  sweep_device_id?: string | null;
  sweep_interval_hours?: number | null;
}

/** `sweep.MIN_SWEEP_INTERVAL_HOURS`. A sub-hourly interval is the exact
 * mechanism by which an IPAM becomes a ping monitor (§0.10 point 6); the API
 * refuses anything shorter with a 400, and this bound is mirrored on the input
 * so the refusal is visible before the request. */
export const MIN_SWEEP_INTERVAL_HOURS = 1;

/** `sweep.DEFAULT_SWEEP_INTERVAL_HOURS` — weekly. */
export const DEFAULT_SWEEP_INTERVAL_HOURS = 168;

/** `ck_ipam_sweep_run_state`. `no_agent` and `refused` are FIRST-CLASS
 * OUTCOMES, not errors swallowed in a log. */
export type SweepRunState =
  | "queued"
  | "dispatched"
  | "succeeded"
  | "failed"
  | "refused"
  | "no_agent";

export type SweepTrigger = "scheduled" | "manual";

export interface SweepRunOut {
  id: string;
  client_id: string;
  subnet_id: string;
  job_id: string | null;
  device_id: string | null;
  state: string;
  trigger: string;
  /** ADDRESSES, not list entries — the same number the result handler bounds
   * `hosts` against. */
  target_count: number;
  /** The hash of the canonical authorized target list carried in the signed
   * params. A result whose digest does not match is rejected whole (§6.4). */
  scope_digest: string;
  hosts_found: number;
  truncated: boolean;
  started_at: string;
  finished_at: string | null;
  error: string;
}

// --------------------------------------------------------------------------
// Import / export (§8)
// --------------------------------------------------------------------------

/** `importer.IMPORT_FIELDS`. `state` IS ABSENT AND ALWAYS WILL BE: a column
 * that could set it would let a spreadsheet write the one field §0.6 reserves
 * for a human decision made through an audited transition. */
export const IMPORT_FIELDS = [
  "address",
  "hostname",
  "description",
  "asset_label",
  "mac",
  "allocation_method",
] as const;

export type ImportField = (typeof IMPORT_FIELDS)[number];

export const MAX_IMPORT_BYTES = 10 * 1024 * 1024;
export const MAX_IMPORT_ROWS = 50_000;

/** One row's verdict. EVERY input row produces exactly one — including the ones
 * that could not be parsed, which is the difference between "reported" and
 * "skipped". */
export interface ImportRowOut {
  line: number;
  action: string;
  address: string;
  error: string;
  detail: Record<string, unknown>;
}

/**
 * Where a CSV import goes: a VRF -- its client is the import's -- or, naming
 * no VRF, a client's `global` VRF (MSP clients phase 2). Sent as `vrf_id` or
 * `client_id`; the server's digest covers the VRF it resolves to.
 */
export type ImportTarget = { vrfId: string } | { clientId: string };

export interface ImportPreviewOut {
  /** The sha256 an `apply` must ECHO to prove it is the file that was
   * previewed. This is the whole binding between the two steps (§8.1). */
  digest: string;
  total_rows: number;
  creates: number;
  updates: number;
  conflicts: number;
  errors: number;
  rows: ImportRowOut[];
}

export interface ImportResultOut {
  digest: string;
  total_rows: number;
  applied: number;
  errors: number;
  rows: ImportRowOut[];
}

export interface ConvertedRecordOut {
  record_id: string;
  name: string;
  prefix: string;
  subnet_id: string | null;
  vlan_id: string | null;
  warnings: string[];
}

export interface SkippedRecordOut {
  record_id: string;
  name: string;
  reason: string;
}

export interface DocRecordConversionOut {
  /** DEFAULTS TO TRUE server-side, and the default is the safe one: a call with
   * no query parameter reports what WOULD convert and writes nothing (§8.3). */
  dry_run: boolean;
  converted: ConvertedRecordOut[];
  skipped: SkippedRecordOut[];
}

export interface SubnetFilters {
  vrf?: string;
  site?: string;
  vlan?: string;
  contains?: string;
  q?: string;
}
