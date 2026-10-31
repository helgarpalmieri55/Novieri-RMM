# Phase 3 — Remote Control Module Specification (control plane)

Status: approved (brainstorming decisions DR-01…04, 2026-07-18)
Source brief: `docs/modules/remote-control.md`. Kernel contracts: `docs/kernel/spec.md`.
Hard dependencies (already built): credential vault, hash-chained audit log,
per-tenant object storage, RBAC, signed-job gateway.

## 0. Superseded in part: screen share (2026-09-23)

The operator decided the platform gets its own remote control that shows the
user's **actual** console screen. It is specified in
`docs/modules/remote-control/spec-screen-share.md`.

- **Superseded for attended helpdesk:** this spec's agent half, where the
  agent bridges the relay to `127.0.0.1:3389/5900` for guacd (§9, the
  `local_port` loopback design). RDP to loopback opens a *new* session and
  cannot show what the user is looking at, and it needs a stored admin
  credential. Attended and unattended console sessions use the new
  `remote.screen.open` job type instead, with no credential.
- **Still in force:** the session state machine (§5), per-session relay token
  (§6, with the relay moving into the gateway process as described in the new
  spec §2.2), recording and retention (§8), RBAC (§10, plus two new codes),
  audit events (§12), and the policy table (§4, plus new columns).
- **Frozen, not removed:** the guacd broker and the RDP/VNC/SSH protocols. The
  start dialog hides them until an agent half exists; whether to keep guacd for
  SSH to Linux servers is open question OQ-4 in the new spec.

## 1. Scope

Control-plane side of clientless, browser-based remote control of managed
endpoints via **Apache Guacamole** (guacd), brokering **RDP / VNC / SSH** — with
**unattended admin** access using vault-stored credentials (decrypt-at-use),
every session RBAC-gated, audited, and recorded to per-tenant object storage.

The endpoint exposes **no inbound port**: the agent establishes an
**agent-initiated reverse tunnel** out to a gateway relay, and guacd connects to
the control-plane end of that tunnel. This preserves the "agents outbound-only,
mutual TLS" invariant.

**Locked decisions (from the design gate):**

- **DR-01 — Reachability:** agent reverse tunnel to a gateway relay. No site
  server dependency.
- **DR-02 — Build scope this cycle:** the full server slice, with **guacd
  mocked** in tests. Session lifecycle, RBAC/scoping, vault credential checkout,
  audit, recording storage + retention, the guacd-protocol WebSocket broker, the
  relay, and the wire contract for the agent capability. A live guacd is wired in
  at deploy time only.
- **DR-03 — Consent:** **attended by default**; unattended admin allowed only
  where a device-group `remote` policy enables it. Unattended is fully audited.
- **DR-04 — Recording:** **always record**; per-tenant object storage; linked to
  the session's audit event; **90-day retention**, configurable per org.

Out of scope this cycle:
- The **native WebRTC** channel (brief's v2) — session shadowing without guacd.
- The **agent-side** `remote.session.open` capability *implementation* (belongs
  under `agent/`; unbuilt); this spec defines its wire contract and the relay
  protocol it implements, and it is a fast-follow like the inventory collector —
  which is also still unbuilt.
- File-transfer/clipboard **content inspection/DLP** — v1 exposes the policy
  toggles and passes them to guacd, but does not inspect transferred bytes.

## 2. Architecture (registers into the kernel; forks nothing)

```
browser (guacamole-common-js)
   │  WebSocket, Guacamole protocol
   ▼
[WS tunnel broker]  app/modules/remote/broker.py      ── FastAPI WS route
   │  Guacamole protocol (text instructions) over a socket
   ▼
guacd  (external daemon; MOCKED in tests via an in-process protocol stub)
   │  RDP/VNC/SSH, dialed at the relay's per-session local socket
   ▼
[relay]  app/modules/remote/relay.py                  ── asyncio TCP/mTLS
   │  per-session glue: guacd-side socket  ⇆  agent tunnel
   ▼  mutual TLS, agent-initiated
agent (agent/, remote.session.open)
   │  loopback TCP to 127.0.0.1:<3389|5900|22>
   ▼
endpoint RDP/VNC/SSH service
```

Control path (session setup) rides the **existing signed-job gateway**; the
media path rides the **relay**. The two never cross: the relay carries only
opaque protocol bytes and is authorized purely by a per-session token minted
during a verified, RBAC-checked session start.

## 3. Module layout

```
backend/app/modules/remote/
├── __init__.py       # register_module(): capability wire type + retention task
├── models.py         # RemoteSession, RemotePolicy (SQLAlchemy)
├── service.py        # session lifecycle: authorize → consent → checkout → dispatch → record
├── relay.py          # gateway relay: agent reverse-tunnel <-> guacd socket glue
├── guacd.py          # Guacamole-protocol client (handshake/instruction codec)
├── broker.py         # browser WebSocket <-> guacd tunnel + recording tap
├── recording.py      # recording capture finalize + ObjectStore upload + retention sweep
├── policy.py         # per-device-group consent/recording/transfer policy resolution
└── api.py            # /api/v1/remote routes
frontend/src/modules/remote/   # SessionsPage (list/audit) + SessionViewer (canvas)
backend/migrations/versions/0010_remote_control.py
docs/kernel/wire-protocol/RemoteSessionOpenJob.schema.json   # agent contract (exported)
```

No core file changes. The module registers a new **job type**
(`remote.session.open`, no result handler — completion is signaled by the relay
tunnel opening, not a job result) and a **retention task** on the existing
worker.

## 4. Data model (migration 0010 — all tables `org_id` + FORCE RLS)

- **`remote_sessions`**: `id` (uuid7 pk), `org_id`, `device_id` FK,
  `technician_id` FK→users, `protocol` (`rdp|vnc|ssh`), `consent_mode`
  (`attended|unattended`), `credential_id` FK→credentials (nullable; set for
  unattended), `state` (see §5), `relay_token_hash` (sha256; the token itself is
  never stored), `recording_ref` (object-store key, nullable until finalized),
  `recording_bytes` (bigint, nullable), `started_at`, `ended_at` (nullable),
  `end_reason` (`completed|denied_consent|revoked|error|timeout`, nullable),
  `audit_event_id` FK→audit_events (the session-start event).
  Indexes: `(org_id, device_id, id)`, `(org_id, state)`,
  `(org_id, recording_ref)` partial where not null (retention sweep).
- **`remote_policies`**: `id`, `org_id`, `device_group_id` FK (nullable = org
  default), `allow_unattended` (bool, default **false** — DR-03),
  `record_sessions` (bool, default **true** — DR-04),
  `allow_file_transfer` (bool, default false), `allow_clipboard` (bool, default
  false), `recording_retention_days` (int, default **90**).
  Unique `(org_id, device_group_id)`.

Both tables get the standard `tenant_isolation` RLS policy (ENABLE + FORCE); the
Phase-0 RLS-completeness test requires them. `remote_sessions` is **append-mostly**:
rows are only updated by the owning service through the state machine; there is
no operator UPDATE/DELETE endpoint.

## 5. Session state machine (`service.py`)

```
requested ─► authorized ─► awaiting_consent ─► connecting ─► active ─► ended
                  │               │                  │           │
                  └── denied ◄────┘ (attended: user rejects)     │
                  └───────────── error / timeout ────────────────┘
```

- **requested → authorized:** RBAC (`remote.control`) + device-group scope + a
  `remote_policy` lookup decide `consent_mode` and `record_sessions`. Unattended
  is only chosen when the resolved policy `allow_unattended` is true **and** the
  caller asked for it; otherwise the session is attended. Audit
  `remote.session.requested`.
- **authorized → awaiting_consent:** a signed `remote.session.open` job is
  dispatched to the agent carrying the relay URL + per-session token + protocol +
  consent_mode. For **unattended**, the vault credential is checked out here
  (§7). For **attended**, the agent's user-session helper renders the consent
  prompt; the agent reports accept/reject as a job status message.
- **→ connecting:** the agent dials the relay and presents the token; the relay
  authenticates it (§6) and opens the per-session guacd socket.
- **connecting → active:** guacd connects through the relay and the browser's
  WebSocket tunnel is live. Audit `remote.session.started` (this is
  `audit_event_id`).
- **→ ended:** browser disconnect, operator stop, revoke, timeout, or error.
  Recording is finalized and uploaded (§8); audit `remote.session.ended` with
  duration + `recording_ref`. The relay tunnel and any checked-out credential
  material are released.

A session has a hard **idle/lifetime timeout** (default 30 min idle, 8 h max);
the service transitions stale sessions to `ended (timeout)`.

## 6. Relay + per-session token (`relay.py`)

The relay is a gateway-side asyncio listener (its own mTLS port, same client-CA
trust as the agent gateway — the agent authenticates with its enrolled client
cert **and** the per-session token; both must match the session's device).

- **Token:** 32-byte URL-safe secret, minted at dispatch, returned to the agent
  inside the *signed* job (so only the real server could have issued it) and
  stored only as `relay_token_hash`. Single-use, bound to `(session_id,
  device_id)`, expires with `awaiting_consent`/`connecting` (default 60 s to
  connect).
  - **"Never stored" scope (accepted, mitigated exposure):** "only
    `relay_token_hash` is stored" applies to the **`remote_sessions` row** and to
    **every API response** — neither ever carries the raw token. The raw token
    *does* necessarily ride inside the **signed `remote.session.open` job
    payload** (the agent needs it to authenticate to the relay), and the job
    engine persists job params plaintext in the **`jobs` table**. This is an
    accepted exposure, not an open door: the token is **single-use**,
    **short-lived** (≤ ~60 s to connect), and additionally **device-bound** — the
    relay also requires the agent's mTLS device cert — so a leaked job-row token
    is defense-in-depth-mitigated. It is documented at the mint/dispatch site in
    `service.py`.
- **Glue:** on a valid tunnel, the relay exposes a per-session **loopback socket**
  (127.0.0.1, ephemeral port or unix socket) that guacd is told to connect to.
  The relay pipes bytes bidirectionally between the guacd socket and the agent
  tunnel until either side closes. It never parses the protocol payload.
- **Cross-device safety:** a tunnel whose client-cert device ≠ the session's
  device, or whose token hash doesn't match, is dropped and audited
  (`remote.relay.identity_mismatch`) — mirrors the gateway's message-identity
  rule.

Testable without a real agent: tests drive the relay with an in-process socket
pair standing in for the agent tunnel and assert byte-for-byte relaying + token
and identity rejection.

## 7. Credential checkout (unattended) — vault integration

Unattended sessions decrypt the admin credential **at point of use** and never
persist or log it:

- At `authorized → awaiting_consent`, `service.py` calls
  `vault.checkout(session, org_id=…, credential_id=…)` inside the tenant
  transaction, gated by the **`credentials.checkout`** permission the caller must
  hold (added in migration 0009). The plaintext is used only to build the guacd
  RDP/VNC/SSH connection parameters and is dropped immediately after.
- The secret is **never** returned to the browser, written to a log, or stored on
  the session row — only `credential_id` is referenced. guacd receives the
  password over its local (loopback) control socket, not over the browser tunnel.
- Audit `credential.checked_out` (from the vault path) **and**
  `remote.session.credential_used` linking credential → session.

## 8. Recording (`recording.py`) — DR-04

- **Capture:** guacd writes a session recording (Guacamole `recording-path` /
  `recording-name` connection params) to a per-session scratch path; the broker
  sets these params for every session where the resolved policy
  `record_sessions` is true (default true). Recording is **mandatory** unless a
  device-group policy explicitly disables it.
- **Finalize:** on session end, `recording.py` streams the scratch file to the
  ObjectStore under a per-tenant key
  `org/{org_id}/remote-recordings/{session_id}.guac`, sets `recording_ref` +
  `recording_bytes`, then deletes the scratch file. Uses the existing
  `ObjectStore` (`put`/`get`), mirroring `put_job_artifact`.
- **Playback:** `GET /api/v1/remote/sessions/{id}/recording` streams the object
  to authorized readers (`remote.recordings.read`), audited
  `remote.recording_accessed`. (Rendering the `.guac` recording in the browser
  reuses guacamole-common-js's player; server only streams bytes.)
- **Retention:** a worker task `remote.recording_retention` runs daily, deleting
  objects + nulling `recording_ref` for sessions whose `ended_at` is older than
  the org's `recording_retention_days` (default 90). The deletion is audited
  `remote.recording_expired`. Retention is **per org** (resolved from the
  org-default `remote_policy`).

## 9. Agent capability wire contract (to be implemented under `agent/`)

Exported to `docs/kernel/wire-protocol/RemoteSessionOpenJob.schema.json`. The
signed `remote.session.open` job payload params:

```jsonc
{
  "session_id": "uuid",
  "protocol":   "rdp|vnc|ssh",
  "local_port": 3389,                 // endpoint loopback port to bridge
  "relay_url":  "wss://gateway:8443/relay",
  "relay_token":"<per-session secret>",
  "consent_mode":"attended|unattended",
  "consent_prompt": { "technician": "…", "reason": "…" }   // attended only
}
```

Agent behavior (dormant until this job arrives — idle budget):
1. Verify the signed job (existing runner).
2. **Attended:** ask the low-privilege user-session helper to render the consent
   prompt; report `job_status` accept/reject. On reject → done, no tunnel.
3. Dial `relay_url` over mTLS, send the relay token, then pipe bytes between the
   tunnel and a fresh `127.0.0.1:local_port` TCP connection.
4. Release everything when the tunnel closes. No listening port is ever opened.

## 10. RBAC (migration 0010 seeds)

New permissions:

- `remote.control` — start/stop remote sessions. Granted to **super-admin**,
  **org-admin**, **technician**.
- `remote.recordings.read` — list/playback recordings. Granted to **super-admin**,
  **org-admin**, **auditor** (privileged-access evidence — read-only auditors get
  it, but *not* `remote.control`).
- `remote.policy.manage` — edit `remote_policies`. Granted to **super-admin**,
  **org-admin**.

Unattended additionally requires `credentials.checkout` (existing). Every
privileged route re-checks server-side (never gated in the frontend), and device
scope is enforced by the same device-group scoping used by `jobs.dispatch`.

> **Amended 2026-10-31 (phase 3 design §5.3, migration 0244):
> `remote.supervise`** — stop or join ANOTHER technician's session, asked on
> the session's client (its machine's). The owner-or-admin rail
> (`tunnel.py::owner_or_admin`, shared by `POST /sessions/{id}/stop` and the
> screen tunnel) asked `orgs.manage` until then; it now asks this code, and
> `orgs.manage` no longer carries the power. 0244 granted it to every role that
> held `orgs.manage` when it ran — `super-admin`, `org-admin` and any role an
> organisation made itself — so nobody lost it unawares. `technician`,
> `auditor` and `read-only` do not hold it.

## 11. API (`api.py`, prefix `/api/v1/remote`)

- `POST /sessions` (`remote.control`) — body `{device_id, protocol,
  mode?:"attended"|"unattended", credential_id?, reason}`. Authorizes, resolves
  policy/consent, mints the session + relay token, dispatches the agent job.
  Returns `{session_id, state, tunnel_url}` (the browser WS tunnel URL). Never
  returns the relay token or any secret.
- `GET /sessions` (`remote.control` or `remote.recordings.read`) — list with
  filters `state`, `device_id`, `technician_id`; paginated.
- `GET /sessions/{id}` — session detail incl. state, consent_mode, recording
  presence (never the token/secret).
- `POST /sessions/{id}/stop` (`remote.control`, own session or
  `remote.supervise` on the session's client) — transitions to
  `ended (completed)`; drops the relay tunnel. Audited `remote.session.ended`
  with whoever stopped it as the actor.
- `WS /sessions/{id}/tunnel` (`remote.control`) — the browser Guacamole tunnel;
  bridges to guacd via `broker.py`. Authorized per-message by the session owner.
- `GET /sessions/{id}/recording` (`remote.recordings.read`) — stream the `.guac`
  object; audited.
- `GET|PUT /policies` (`remote.policy.manage` for PUT; read with `remote.control`)
  — per-device-group consent/recording/transfer/retention policy.

## 12. Audit events (all via the existing hash-chained log)

`remote.session.requested`, `remote.session.started`, `remote.session.ended`,
`remote.session.denied` (attended reject), `remote.session.credential_used`,
`remote.relay.identity_mismatch`, `remote.recording_accessed`,
`remote.recording_expired`, `remote.policy.changed`. Session start/stop and every
credential use are privileged actions and therefore mandatory audit points
(kernel invariant #4). Because the audit log is now HMAC-authenticated
(security-review finding A), these records are tamper- **and** forge-evident.
Amended 2026-10-31 (phase 3 design §5.3): `remote.session.ended` carries the
session's client, and its actor is the person who stopped the session (it
named the owner even when another person stopped it).

## 13. Frontend (`frontend/src/modules/remote/`)

- **SessionsPage** — live/past sessions table (device, technician, protocol,
  consent mode, state, duration, recording badge), gated on `remote.control` or
  `remote.recordings.read`. Start-session dialog (device picker + protocol +
  attended/unattended + reason).
- **SessionViewer** — the Guacamole display: a `<canvas>` driven by
  guacamole-common-js over the tunnel WebSocket, with connection state, an end
  button, and (policy-permitting) clipboard/file-transfer controls.
- **RecordingPlayer** — plays a stored `.guac` recording via guacamole-common-js's
  player for `remote.recordings.read`.
- Nav item appears only when `/me` reports one of the remote permissions
  (existing permission-gated nav pattern). TanStack Query for session state;
  no `any`.

`guacamole-common-js` is Apache-2.0 (commercialization-safe) and is the only new
frontend dependency.

## 14. Security invariants preserved

1. **Signed jobs only:** session setup is a signed `remote.session.open` job; the
   agent verifies it before opening any tunnel. The relay token is delivered
   *inside* the signed payload, so it is unforgeable. As a consequence the raw
   token is persisted plaintext in the **`jobs` table** (job params are stored
   plaintext by the job engine); "only `relay_token_hash` is stored" applies to
   the `remote_sessions` row and API responses. This job-table presence is an
   accepted, mitigated exposure — the token is single-use, ~60 s-lived, and
   device-bound (the relay also requires the agent's mTLS device cert). See §6.
2. **Outbound-only mTLS:** the agent dials the relay out; no endpoint port is
   opened. The relay uses the same client-CA trust as the gateway.
3. **Credentials decrypt-at-use:** unattended creds come from the vault at point
   of use, never logged, never returned to the browser, never stored on the
   session.
4. **Immutable audit:** every start/stop and credential use is an HMAC-authenticated
   audit event.
5. **Tenant-scoped:** `remote_sessions`/`remote_policies` are `org_id` + FORCE
   RLS; recordings are per-tenant object keys; the relay binds each tunnel to the
   session's device.
6. **RBAC server-side:** every route and the tunnel re-check permissions and
   device scope on the server.

Revocation interaction: a revoked agent (security finding B) is dropped from the
gateway and cannot receive `remote.session.open`; an in-flight session's relay
tunnel is torn down when the agent connection closes.

## 15. Testing strategy (test-first)

- **service/state machine:** authorize→consent→connecting→active→ended paths;
  attended-reject; unattended gated on policy + `credentials.checkout`; timeout
  transitions. (DB + fakes.)
- **relay:** byte-exact bidirectional relaying via an in-process socket pair;
  token single-use + expiry; cross-device/cert mismatch rejection + audit.
- **guacd client:** Guacamole instruction encode/parse round-trips; handshake
  (`select`→args→`connect`) against an in-process mock guacd; recording params
  set when policy requires.
- **broker:** browser-WS ⇆ guacd bridging with a fake guacd; unauthorized/other-
  session message rejected.
- **credential path:** unattended checkout is audited, secret never appears in
  any API response, log, or the session row (explicit assertion).
- **recording + retention:** finalize uploads to ObjectStore and sets
  `recording_ref`; playback streams + audits; retention sweep deletes expired
  objects and audits, honoring per-org `recording_retention_days`.
- **API + RBAC + RLS:** permission matrix per route; cross-tenant isolation;
  migration 0010 RLS-completeness (auto).
- **frontend:** tsc strict + vitest for the session hook/state; build.

## 16. Deploy additions

`deploy/docker-compose.yml` gains a **guacd** service (Apache-2.0 image) reachable
by the API/broker on the internal network; the relay port is exposed alongside
the agent gateway. No guacamole-client Java webapp is deployed — the broker is
our own FastAPI tunnel. Object storage (MinIO, already present) holds recordings;
a bucket-lifecycle note documents the app-level retention sweep as the source of
truth.
