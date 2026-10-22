// Mirrors backend/app/api/v1/platform.py's `PlatformStatusOut` exactly
// (snake_case keys, matching the JSON). THE SHIPPED API IS THE CONTRACT --
// these types were written against `platform.py` directly, not against the
// module brief's prose, and the route wins on any disagreement.
//
// `processes` is `list[dict[str, Any]]` server-side (built from
// `app.core.health.heartbeat.read_heartbeats`), not a Pydantic model -- but
// the dict is built with exactly these six keys on every entry, so it is
// typed here as a closed shape rather than `Record<string, unknown>`.

export type Status = "ok" | "degraded" | "down";

// `app.core.health.types.ReasonCode`. Codes only, never exception text --
// `/platform/status` is the one place that detail is allowed to surface at
// all, and it stops at the code (see api.py's module docstring).
export type ReasonCode =
  | "unreachable"
  | "timeout"
  | "auth_failed"
  | "sealed"
  | "empty_cache"
  | "misconfigured"
  | "stale"
  | "failed";

// THE NAMES THE CONSOLE HAS A LABEL FOR. `Probe.name`, a probe's `detail` keys
// and `RegisteredModule.name` stay open-ended strings on the wire: the backend
// may register one this console has not met, and that one reads as its derived
// English rather than being dropped. These unions are only the names catalogued
// under `enums.<Type>` (`enums.coverage.test.ts` checks every value has a label
// in both languages). A dotted probe name (`backup.nightly`) is keyed with `_`
// because `.` separates catalogue paths. `keycloak_events` is deliberately
// absent: its English label would name the identity vendor, which no catalogue
// string may do.
export type PlatformProbe =
  | "postgres_app"
  | "postgres_admin"
  | "database"
  | "redis"
  | "cache"
  | "vault"
  | "objectstore"
  | "object_store"
  | "identity"
  | "jwks"
  | "signing"
  | "ca"
  | "backup_nightly"
  | "backup_offsite"
  | "backup_drill";

export type PlatformProbeDetail =
  | "sealed"
  | "bucket"
  | "keys"
  | "configured"
  | "ever_run"
  | "age_s"
  | "written"
  | "hours_since_success"
  | "days_since_drill";

export type PlatformModule =
  | "agent_distribution"
  | "agent_push"
  | "agent_update"
  | "ai_admin"
  | "ai_copilot"
  | "ai_portal"
  | "backup"
  | "bcp"
  | "budget"
  | "change_mgmt"
  | "deploy"
  | "documentation"
  | "email_intake"
  | "health"
  | "inventory"
  | "ipam"
  | "licenses"
  | "notification"
  | "patch"
  | "people"
  | "portal"
  | "provisioning"
  | "remote"
  | "reporting"
  | "security_logs"
  | "service_desk"
  | "site_server"
  | "sla"
  | "subscriptions"
  | "user_admin";

export interface Probe {
  /** The probe's identifier (`ProbeOut.key`): never translated, so every lookup and React
   *  key uses it. Optional because older payloads carried only `name`. */
  key?: string;
  /** What the server PRINTS for the probe. It is worded in the request's language, so it
   *  is not an identifier: with a `key`, use the key for lookups. */
  name: string;
  status: Status;
  duration_ms: number;
  reason: ReasonCode | null;
  detail: Record<string, string | number | boolean> | null;
}

export interface ProcessHealth {
  process: string;
  instances: number;
  healthy_instances: number;
  oldest_age_s: number;
  status: Status;
  version: string | null;
}

// REGISTERED, not healthy. `app.core.module_registry.build_module_registry`
// lists what is registered -- name, route count, job-type count -- because
// that is what it measures. Modules are never probed (platform.py's module
// docstring, spec §1), so nothing here may carry a health-shaped field.
export interface RegisteredModule {
  name: string;
  routes: number;
  job_types: number;
}

export interface PlatformStatus {
  overall: Status;
  probes: Probe[];
  processes: ProcessHealth[];
  modules: RegisteredModule[];
}
