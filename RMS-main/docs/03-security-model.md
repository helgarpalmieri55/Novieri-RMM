# 03 — Security Model

This platform is **domain-admin-equivalent command-and-control over an entire
fleet of computers and servers**. It can run arbitrary code as SYSTEM/root on
every managed endpoint, holds privileged credentials, and images machines. It is
therefore a **crown-jewel target**, and its security model is platform core — not
a module, not a later hardening pass. The invariants in `CLAUDE.md` derive from
this document.

## Trust boundaries

```
   TRUSTED            SEMI-TRUSTED / VERIFY           UNTRUSTED
   ┌──────────────┐   ┌───────────────────┐          ┌──────────────┐
   │ Control plane│   │ Agents            │          │ Everything   │
   │ Job Engine   │──▶│ (verify every job │◀────────▶│ else on the  │
   │ Auth / Vault │   │  before running)  │          │ network      │
   │ Audit        │   │ Site servers      │          └──────────────┘
   └──────────────┘   │ (mTLS, scoped)    │
        ▲             └───────────────────┘
        │ RBAC + MFA
   ┌────┴────┐
   │Operators│
   └─────────┘
```

- **Control plane** is the trusted core, but even it does not get an implicit
  path to run code on an endpoint — it must *sign* what it sends.
- **Agents** are semi-trusted: they run as root/SYSTEM but verify the signature
  and authorization envelope of everything before acting. A compromised control
  plane channel cannot make an agent run an unsigned command.
- **Site servers** are **ordinary agents**. A site server is a `Device` an
  operator has promoted (`docs/modules/site-server/spec.md` §0.1, §3.2): it
  enrols exactly as any endpoint does and holds an ordinary device-bound mTLS
  certificate. **There is no site-server certificate profile**, and this bullet
  used to imply one ("scoped to caching/PXE/relay"). The scoping is the
  `site_server` registry row — operator-entered, audited, revocable — plus, when
  the cache lands, a signed and time-bounded `site.serve` lease. It is not a
  property of the credential.

  A second certificate profile was considered and rejected: it would require a
  second fingerprint→identity resolution in `GatewayServer._resolve_agent`,
  which is the one function in the system where identity must have exactly one
  answer.

  "Not implicitly trusted to command endpoints" **stays true, and is now
  enforced rather than asserted.** An agent certificate authorises exactly one
  thing at the gateway: holding a connection bound to one
  `(device_id, org_id)`. It cannot mint a job — only the server signs — and it
  cannot speak about another endpoint's job:
  `app/gateway/dispatch.py::_load_job_for_device` discards and audits any
  message whose job's `target_id` is not the connected device
  (`gateway.identity_mismatch`). So "never to job command" is not a scope that
  needed carving out of a certificate; it is the default, enforced in two
  places.
- **Operators** are gated by SSO + MFA + RBAC.

## The signed-job invariant

The single most important control. **Every job an agent executes is signed by the
server's job-signing key and verified by the agent against a pinned public key
before execution.**

- The job-signing **private key** lives in the vault (ideally an HSM-backed or
  Vault transit key), separate from ordinary TLS material, and is used only by
  the Job Engine to sign job payloads.
- Each agent pins the corresponding **public key** at enrollment.
- A job payload includes: job type, target device identity, parameters, an
  issued-at timestamp, an expiry, and a nonce — all covered by the signature.
- The agent rejects any job that is unsigned, has an invalid signature, is
  expired, replays a seen nonce, or is addressed to a different device identity.

Why this matters: it means transport compromise, a stolen gateway session, or
even write access to the application database is **not sufficient** to execute
code on the fleet — the attacker would also need the signing key. There is no
"internal/testing" bypass of this path. Ever.

## Agent identity, enrollment, and transport

- **Transport:** TLS 1.3 with **mutual TLS**. The agent presents a client
  certificate; the gateway presents (and the agent pins) the server certificate.
  Outbound-only (`docs/01-architecture.md`).
- **Enrollment:** the agent ships/receives a per-org, time-bounded **enrollment
  token** (delivered via installer parameters, GPO, MDM, or kickstart for
  zero-touch). On first start it generates a keypair, submits a CSR, and the
  internal CA (Vault PKI / step-ca) issues a client certificate bound to a device
  identity. The private key never leaves the endpoint.
- **Rotation & revocation:** certificates are short-lived and rotated
  automatically; a decommissioned or compromised agent's certificate is revoked,
  after which the gateway refuses its connection.
- **Binary integrity:** self-update is a **signed `agent.update` job** (decided
  2026-09-23; `docs/modules/agent-distribution/spec-self-update.md`). The agent
  verifies the new binary's size and SHA-256 against the signed params before
  staging it, and never swaps without them. That is the same trust `app.deploy`
  and `script.run` already extend to the control plane, so it adds no power a
  signing control plane lacks. Code signing is still wanted, and Windows is
  still unsigned. Once Authenticode exists, the agent also verifies the
  signature against a publisher pin **compiled into the running binary**, never
  supplied by the job.

## Credential vault

The platform stores privileged credentials (unattended remote-control admin
accounts, provisioning/join credentials). These are the highest-value secrets in
the system.

- Stored **encrypted at rest** using envelope encryption: a per-organization data
  key wraps each secret; the data key is wrapped by a root key held in the vault
  (Vault/OpenBao transit). The application database stores only **metadata and a
  vault reference**, never plaintext.
- **Decrypted only at point of use** by the specific job/session that needs them,
  then discarded from memory.
- **Never** written to logs, returned in API responses, or exposed to the
  frontend.
- **Scoped** by RBAC and, where applicable, by device group. **Checked out with
  audit** — every access is an audit event.

## Authentication & RBAC

- **Authentication:** operators authenticate through **Keycloak** (OIDC/SAML),
  federated to AD/Entra where present, with **MFA enforced**. Sessions are
  server-managed with sane expiry.
- **Authorization:** role-based, enforced **server-side** on every privileged
  endpoint and every job dispatch. The frontend never makes security decisions.
- **Baseline roles** (extensible):

  | Role | Capability |
  |---|---|
  | Super-admin | Cross-org platform administration (SaaS operator) |
  | Org-admin | Full control within one organization |
  | Technician | Operate endpoints (remote, deploy, patch) within scope |
  | Auditor | Read-only access to inventory, records, and audit log |
  | Read-only | View dashboards and inventory only |

- Permissions are evaluated per **module + action**, scoped to `org_id` and
  optionally to device groups, so a technician can be limited to a subset of the
  fleet.

## Audit logging

- **Append-only and tamper-evident.** Each audit event includes the actor, the
  action, the target, a timestamp, and a hash chained to the previous event's
  hash, so any deletion or alteration is detectable.
- **Covers every privileged action:** job dispatched, credential accessed,
  remote session started/stopped (with recording reference), config/policy
  changed, enrollment/revocation, login/MFA events.
- **Doubles as compliance evidence.** The audit log and derived reports are a
  primary SOC 2 evidence source (change records, access records, patch actions).
- Remote sessions are **recorded** (Guacamole session recording) and linked to
  their audit event; unattended admin access is still fully audited.

## Tenant isolation

- Every core and module table carries `org_id`. Isolation is enforced at the
  **PostgreSQL layer via Row-Level Security** using a per-request tenant context,
  in addition to application-level scoping — defense in depth, so a forgotten
  `WHERE org_id = …` cannot leak across tenants.
- The agent gateway binds each connection's device (and therefore its `org_id`)
  from the client certificate; an agent cannot act outside its organization.
- See `docs/04-data-model.md` for the RLS mechanism.

## Threat model (primary threats and mitigations)

| Threat | Mitigation |
|---|---|
| Compromised control plane pushes malicious jobs | Signed-job invariant; signing key isolated in vault/HSM |
| Transport interception / MITM | mTLS 1.3, certificate pinning, outbound-only |
| Stolen agent certificate | Short-lived certs, rotation, revocation at the gateway |
| Tampered agent binary / malicious update | Update only by a signed job pinning the release SHA-256, re-checked at the install location; human-started ring rollout; local rollback; Authenticode against a compiled-in pin once signing exists (`spec-self-update.md`) |
| Credential theft from the vault | Envelope encryption, decrypt-at-use, no plaintext in DB/logs, audited checkout |
| Operator abuse / over-privilege | RBAC scoped to org + device group; MFA; full audit trail |
| Cross-tenant data leakage | `org_id` everywhere + PostgreSQL RLS + cert-bound tenant on gateway |
| Replay of a captured job | Nonce + expiry in signed payload; agent rejects replays |
| Audit tampering to hide activity | Hash-chained append-only log |
| Endpoint compromise pivoting to the network | Agent exposes no inbound port; no listening management service |

## Non-negotiables (restating the invariants)

1. No unsigned job path exists — agents verify every job.
2. Agents are outbound-only over mTLS; no inbound management port.
3. Stored credentials are vault-encrypted, decrypt-at-use, never logged or
   returned.
4. Every privileged action is written to the immutable, hash-chained audit log.
5. All data access is tenant-scoped, enforced in the database via RLS.
6. RBAC is enforced server-side on every privileged endpoint and job dispatch.
