# 04 — Data Model

The data model is **tenant-aware from day one**. On-prem runs as a single
`Organization`; SaaS/MSP carries many. Isolation is enforced in the database, not
only in application code. This document defines the **core** entities the kernel
owns; each module adds its own tables that reference these (module tables are
listed in the module briefs).

## Multi-tenancy strategy

**Shared schema + `org_id` on every tenant-scoped table + PostgreSQL Row-Level
Security (RLS).**

- Every request establishes a **tenant context** (from the operator's session for
  API calls, or from the agent's client certificate on the gateway). The database
  session sets a parameter (e.g. `SET LOCAL app.current_org = …`).
- RLS policies restrict every tenant-scoped table to rows matching the current
  org context. A query that forgets to filter by `org_id` still cannot see other
  tenants' rows — the database enforces it.
- Application code *also* scopes by `org_id` (defense in depth), but RLS is the
  backstop.

Why shared-schema + RLS rather than schema-per-tenant or database-per-tenant: at
BPO/SMB scale it is dramatically simpler to migrate and operate (one schema, one
migration run), while RLS provides hard isolation at the storage layer. For very
large commercial customers, database-per-tenant remains available as a deployment
option without changing the model — the interface is "a tenant context per
request."

## Core entities

Relationships (crow's-foot approximation):

```
Organization ─┬─< User >─┬─< UserRole >── Role ──< RolePermission >── Permission
              │          │
              ├─< Device >─┬── Agent (1:1)
              │            ├──< DeviceTag
              │            └──> DeviceGroup (many-to-one)
              │
              ├─< DeviceGroup
              ├─< Policy (scoped to org / group)
              ├─< Credential (metadata; secret in vault)
              ├─< Job >──< JobLog
              │      └──> Device or DeviceGroup (target)
              └─< AuditEvent (hash-chained)
```

### Organization

The tenant. Everything else hangs off it.

- `id`, `name`, `slug`, `status`, `created_at`
- Settings: retention policies, feature/licence flags, branding (commercial)

### User

An operator identity. Authentication itself is delegated to Keycloak; this record
is the platform-side projection.

- `id`, `org_id`, `external_subject` (Keycloak subject), `email`,
  `display_name`, `status`, `last_login_at`
- Roles via `UserRole`

### Role / Permission / RolePermission / UserRole

RBAC storage.

- **Role**: `id`, `org_id` (null for built-in global roles), `name`,
  `description`
- **Permission**: `id`, `code` (e.g. `remote.session.start`, `patch.approve`),
  `module`, `action`
- **RolePermission**: role ↔ permission
- **UserRole**: user ↔ role, optionally scoped to a `DeviceGroup` (so a
  technician can be limited to part of the fleet)

Baseline roles are seeded (super-admin, org-admin, technician, auditor,
read-only) per `docs/03-security-model.md`.

### Device

A managed asset (computer or server). The central entity most modules attach to.

- `id`, `org_id`, `hostname`, `fqdn`, `os_family` (windows/linux/macos),
  `os_version`, `architecture`, `serial`, `asset_tag`
- Hardware summary: `cpu`, `ram_bytes`, `disks` (jsonb), `nics` (jsonb)
- `device_group_id`, `status` (active/inactive/decommissioned),
  `first_seen_at`, `last_seen_at`
- Detailed/volatile inventory (installed software, full hardware detail) lives in
  inventory module tables keyed by `device_id`, not inline here.

### DeviceGroup

Organizational grouping for scoping policies, jobs, and RBAC.

- `id`, `org_id`, `name`, `parent_group_id` (hierarchical), `description`
- Membership can be static or rule-based (rules stored as jsonb; evaluated by the
  inventory/policy engine)

### DeviceTag

Free-form labels for filtering and dynamic grouping.

- `device_id`, `key`, `value`

### Agent

The software identity installed on a Device (1:1 with Device).

- `id`, `org_id`, `device_id`, `cert_fingerprint`, `enrolled_at`,
  `agent_version`, `capabilities` (jsonb — which capability modules/versions are
  present), `status` (online/offline), `last_seen_at`, `last_ip`
- Certificate lifecycle references (issued/expiry/revoked)

### Policy

Declarative configuration and schedules applied to an org or device group:
inventory sweep cadence, patch windows/rings, log-shipping cadence, backup
schedules, etc.

- `id`, `org_id`, `scope` (org or `device_group_id`), `module`, `config` (jsonb),
  `enabled`, `created_by`, `updated_at`
- The agent's local scheduler is driven by the policies resolved for its device.

### Credential

**Metadata only** — the secret itself lives in the vault.

- `id`, `org_id`, `name`, `type` (windows_admin / ssh_key / domain_join / …),
  `scope` (org or `device_group_id`), `vault_ref`, `created_by`, `created_at`,
  `last_used_at`
- No plaintext, ever. Access is audited (`AuditEvent`). See
  `docs/03-security-model.md`.

### Job

A unit of work dispatched to one or more agents. Owned by the Job Engine.

- `id`, `org_id`, `type` (e.g. `inventory.collect`, `patch.apply`,
  `remote.session.open`, `backup.run`, `provision.deploy`), `target_type`
  (device/group), `target_id`, `params` (jsonb), `signature`, `nonce`,
  `issued_at`, `expires_at`
- Lifecycle: `status`
  (queued/dispatched/running/succeeded/failed/expired/refused/cancelled),
  `attempts`, `requested_by`, `started_at`, `finished_at`, `result` (jsonb)
- Cancellation (migration 0071): `cancel_requested_at`, `cancel_requested_by`
  (-> `users.id`), `cancel_outcome`. `cancelled` is terminal and is reached
  only by a deliberate human act; `withdrawn` -- the job was never written to
  a socket -- is the only outcome the server writes today. `cancel_requested_by`
  is returned by no API route: the actor is in the hash-chained audit log.
- `idempotency_key` where the capability supports safe retry
- The signed payload sent to the agent is derived from this record; the signature
  covers type + target identity + params + issued_at + expires_at + nonce.

### JobLog

Streamed status/output lines for a job (bounded, with large artifacts going to
object storage rather than the database).

- `id`, `job_id`, `org_id`, `ts`, `level`, `message`, `artifact_ref` (nullable)

### AuditEvent

Immutable, hash-chained record of every privileged action.

- `id`, `org_id`, `ts`, `actor_type` (user/agent/system), `actor_id`, `action`,
  `target_type`, `target_id`, `metadata` (jsonb), `prev_hash`, `hash`
- `hash = H(prev_hash || canonical(event))`. Append-only; no updates or deletes.

## Object storage layout (not in PostgreSQL)

Large artifacts are addressed by reference from the DB but stored in
S3-compatible object storage, per tenant prefix:

```
s3://<bucket>/<org_id>/agent-binaries/...
                      /packages/...
                      /images/...
                      /backups/<device_id>/<repo>/...
                      /job-artifacts/<job_id>/...
                      /evidence-exports/...
                      /session-recordings/<session_id>/...
                      /docs/documents/<document_id>/<version_id>
```

**The documents prefix carries the isolation an object store cannot.** There is
no row-level security in a bucket, so `{org_id}/docs/` is asserted in BOTH
directions of `ObjectStore` (`get_doc_document`, `delete_doc_document`), the
key is derived entirely server-side from ids the caller was already authorized
for, no API response anywhere contains an object key, and **no presigned URL is
ever minted for it** -- `deploy/docker-compose.yml` routes the bucket on the
console's own hostname, so a presigned GET of an uploaded HTML file would be an
unauthenticated same-origin script URL. `<version_id>` is what makes each
upload immutable: a new version never overwrites the previous object.
`docs/modules/documentation/spec-attachments.md` §3.

## Conventions

- **`org_id` is mandatory** on every tenant-scoped table and is covered by an RLS
  policy. Adding a tenant-scoped table without an RLS policy is a security bug.
- **UUID primary keys** (v7 preferred for index locality).
- **`created_at` / `updated_at`** on mutable entities; soft-delete only where a
  module explicitly needs it (Devices decommission rather than hard-delete to
  preserve audit/history).
- **jsonb** for evolving/semi-structured detail (hardware, capability lists,
  policy config, job params); promote to columns when queried heavily.
- Module tables live in the module's own migrations and reference core entities
  by `device_id` / `org_id`; they never duplicate core identity.
