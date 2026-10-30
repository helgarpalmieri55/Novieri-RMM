// Mirrors `backend/app/modules/agent_push/schema.py` exactly (snake_case keys,
// matching the JSON). THE SHIPPED API IS THE CONTRACT: these types were
// written against `api.py`'s seven routes and their Pydantic models directly.
//
// Per invariant #5 nothing here carries `org_id`; per invariant #6 nothing
// here is a permission — the server's `require_permission` on every route is
// the only authority, and the permission strings this module reads out of
// `/me` decide only what RENDERS.
//
// **NOTHING IN THIS FILE CAN HOLD A SECRET, AND THAT IS STRUCTURAL RATHER THAN
// CAREFUL.** There is no `secret`, no `ciphertext`, no `vault_ref` and no
// `enroll_token` field, because the server's response models have none
// (invariant #3). A credential appears as its id and the label a human gave
// it, which is what an operator needs to answer "which secret may be presented
// where" and is the most that may ever cross this boundary.

// --------------------------------------------------------------------------
// Permission codes (spec §8) — presentation only
// --------------------------------------------------------------------------
//
// Named as constants so a typo cannot silently hide a control forever. NEITHER
// gates anything: the server re-checks both on every request (invariant #6).
//
// There is deliberately NO read code. Spec §8 names exactly two, and inventing
// a third in the console would be a claim about a server that does not have
// one — so both screens in this module are `agent.push.scope.manage` screens,
// and a holder of `agent.push.run` alone sees no nav entry rather than a link
// that answers 403.

export const AGENT_PUSH_RUN = "agent.push.run";
export const AGENT_PUSH_SCOPE_MANAGE = "agent.push.scope.manage";

/** The `credentials.type` an SSH push uses (spec §6.2). */
export const PUSH_CREDENTIAL_TYPE = "push_install";

/**
 * The `credentials.type` a Windows push uses: a domain account presented over
 * WinRM with Kerberos only (spec-push-install.md D10, overturned 2026-09-28;
 * design §2.5). Dedicated exactly as `push_install` is: never usable for
 * remote control or provisioning.
 */
export const WINDOWS_DOMAIN_CREDENTIAL_TYPE = "windows_domain";

/**
 * Every `credentials.type` a push may use. Mirrors the server's
 * `PUSHABLE_CREDENTIAL_TYPES` (`agent_push/credential_scope.py`), which is
 * the control; this list only decides what the picker offers and what the
 * readiness panel counts.
 */
export const PUSHABLE_CREDENTIAL_TYPES: readonly string[] = [PUSH_CREDENTIAL_TYPE, WINDOWS_DOMAIN_CREDENTIAL_TYPE];

export interface PushAuthorizationOut {
  id: string;
  /** The client whose network the prefix is (MSP clients phase 2, Task 12). */
  client_id: string;
  prefix: string;
  /** How many addresses the grant reaches. Server-computed, never derived here. */
  address_count: number;
  note: string;
  authorized_by: string;
  authorized_at: string;
  expires_at: string | null;
  /** Revocation STAMPS; the row survives, so the grant stays auditable. */
  revoked_at: string | null;
  revoked_by: string | null;
}

export interface PushAuthorizationCreate {
  /** The client whose network the prefix is: a push is authorized there for that client's machines only. */
  client_id: string;
  prefix: string;
  note: string;
  expires_at: string | null;
}

export interface PushCredentialScopeOut {
  id: string;
  /** The credential's client, which the scope is in (never sent: the server copies it). */
  client_id: string;
  credential_id: string;
  credential_name: string;
  prefix: string;
  address_count: number;
  note: string;
  bound_by: string;
  bound_at: string;
  unbound_at: string | null;
  unbound_by: string | null;
}

export interface PushCredentialScopeBind {
  credential_id: string;
  prefix: string;
  note: string;
}

// --------------------------------------------------------------------------
// Cycle B2 — the dispatch surface and the host-key crux
// --------------------------------------------------------------------------
//
// Mirrors `PushRequestCreate`, `PushDispatchOut`, `HostKeyOut` in
// `backend/app/modules/agent_push/schema.py`. Still nothing here can hold a
// secret: a host key is PUBLIC key material (the fingerprint the operator
// compares out-of-band), and a dispatch response names its jobs by id.

/** `probe` fetches the host key with NO credential; `install` presents it. */
export type PushMode = "probe" | "install";

export interface PushRequestCreate {
  mode: PushMode;
  credential_id: string;
  /** 1..25 host IP literals. Never a CIDR, a hostname, a group or a search. */
  targets: string[];
}

export interface PushDispatchTargetOut {
  address: string;
  job_id: string;
  site_server_device_id: string | null;
}

export interface PushDispatchOut {
  batch_id: string;
  kind: PushMode;
  targets: PushDispatchTargetOut[];
}

/** A target's host key: what a probe observed and whether a human confirmed
 * it. Public key material only — never a secret. */
export interface HostKeyOut {
  address: string;
  /**
   * The client whose machine holds the address -- two clients' machines can
   * share one (MSP clients phase 1). Null for a key recorded before its
   * machine was known, which serves every client.
   */
  client_id: string | null;
  /** A wire value (`ssh` or `winrm`). Compared, never rendered. */
  transport: string;
  observed_fingerprint: string;
  observed_key_type: string;
  observed_at: string;
  confirmed_fingerprint: string | null;
  confirmed_at: string | null;
  confirmed_by: string | null;
  /** True only when a confirmed key still equals the latest observation. A
   * changed key makes this false and an install of that host is refused. */
  installable: boolean;
  /**
   * What a credential-free WinRM probe found (design §2.4; plan Task 1's
   * `HostKeyOut`). All three are false on an SSH row, which says nothing
   * about Windows -- `windowsProbeReadiness` counts `winrm` rows only.
   *
   * 5986 (HTTPS) answered. */
  winrm_https: boolean;
  /** 5985 (HTTP, Kerberos message encryption) answered. */
  winrm_http: boolean;
  /** A domain controller for the target's realm answered from the site server. */
  kdc_reachable: boolean;
}

/** `HostKeyOut.transport` for a Windows target. A wire value: compared, never rendered. */
export const WINRM_TRANSPORT = "winrm";

/**
 * Markers a WinRM row may carry where an SSH row carries a fingerprint: the
 * probe's `winrm` observation marker (plan W3) and the install's `kerberos`
 * sentinel (W4). Neither is a key and neither is ever offered for confirmation.
 */
export const WINRM_FINGERPRINT_MARKERS: readonly string[] = ["winrm", "kerberos"];

/**
 * The most addresses one push may name: `preflight.PUSH_BATCH_MAX` on the
 * server, which refuses a longer list whatever this says (invariant 2a(f)).
 * Mirrored so the console refuses the 26th tick instead of letting an operator
 * build a list the server will throw away.
 */
export const PUSH_BATCH_MAX = 25;

export interface HostKeyConfirm {
  address: string;
  fingerprint: string;
}

/** The structured 422 body a refused push returns (spec §9): every failing
 * bound, named, per address. */
export interface PushRefusalDetail {
  message: string;
  targets: { address: string; reasons: string[] }[];
  reason_help: Record<string, string>;
}

// --------------------------------------------------------------------------
// Push runs (design 2026-09-30 §1). Mirrors `schema.py`'s Run* models. Still
// nothing here can hold a secret.
// --------------------------------------------------------------------------

/** `run_list.PUSH_RUN_MAX`: the most READY machines one run may hold. */
export const PUSH_RUN_MAX = 1024;
/** `runs.PUSH_RUN_CONCURRENCY`: the most installs in flight at once. */
export const PUSH_RUN_CONCURRENCY = 50;

export interface PushRunPrefixOut {
  prefix: string;
  /** The client whose network it is: the chosen credential's. */
  client_id: string;
  address_count: number;
  expires_at: string | null;
}

export interface PushRunListRowOut {
  device_id: string;
  address: string;
  hostname: string;
  os_family: string;
  ready: boolean;
  /** Wire codes. Shown through `readinessChip`, never raw. */
  reasons: string[];
}

export interface PushRunPreviewOut {
  prefix: string;
  /** The run's client: the credential's. */
  client_id: string;
  credential_id: string;
  credential_name: string;
  /** Computed by the server; echoed back unchanged to confirm. */
  list_digest: string;
  ready_count: number;
  blocked_count: number;
  /** Rows in the subnet nobody vouched for (e.g. found by a sweep): not listed. */
  unvouched_count: number;
  /** Vouched-for rows still waiting for review: not listed. */
  awaiting_review_count: number;
  /** Addresses that name no single machine: not listed. Always 0 inside one client. */
  ambiguous_count: number;
  run_max: number;
  concurrency: number;
  rows: PushRunListRowOut[];
  reason_help: Record<string, string>;
}

export interface PushRunCreate {
  prefix: string;
  credential_id: string;
  list_digest: string;
}

/** `stopped_credential`: the lockout guard stopped the run after repeated
 * sign-in failures (spec §1.4a). Only a person's Resume restarts it. */
export type PushRunState = "running" | "paused" | "stopped_credential" | "cancelled" | "completed";
export type PushRunTargetState = "waiting" | "in_flight" | "installed" | "failed" | "refused" | "cancelled";
export type PushRunCounts = Record<PushRunTargetState, number>;

export interface PushRunOut {
  id: string;
  /** The run's client: its credential's. */
  client_id: string;
  prefix: string;
  credential_id: string | null;
  credential_name: string;
  created_by: string;
  created_at: string;
  state: PushRunState;
  target_count: number;
  concurrency: number;
  list_digest: string;
  finished_at: string | null;
  counts: PushRunCounts;
  /** When the lockout guard stopped the run; null otherwise. */
  stopped_at: string | null;
  /** Sign-in failures since the run last started or resumed. */
  auth_failures: number;
  /** How many stop the run (`runs.PUSH_RUN_AUTH_FAILURE_LIMIT`). */
  auth_failure_limit: number;
  /** A sign-in has got through since then. False: one machine at a time. */
  signed_in: boolean;
}

export interface PushRunTargetOut {
  position: number;
  device_id: string | null;
  address: string;
  state: PushRunTargetState;
  job_id: string | null;
  outcome: string;
  detail: string;
  reasons: string[];
  updated_at: string;
}

export interface PushRunTargetPage {
  total: number;
  items: PushRunTargetOut[];
  reason_help: Record<string, string>;
}
