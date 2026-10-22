# Module — Remote Control

> Scoping brief. Phase 3. First module to exercise the credential vault and
> session audit/recording — do not build until those kernel pieces are solid.
> Expand into a full spec via the brainstorming skill before implementation.

## Purpose

Let technicians remotely control managed endpoints — including **unattended**
sessions using **stored admin credentials** (IntelliAdmin-style) — from the
browser, with every session authorized, audited, and recorded. High-value for BPO
helpdesk operations.

## Scope — v1 (Guacamole gateway)

- **Clientless browser sessions** to endpoints via **Apache Guacamole** (guacd +
  web), brokering **RDP / VNC / SSH**. Apache-2.0 — safe for commercialization.
- **Unattended admin access:** connect using credentials from the **vault**
  (decrypt-at-use), so a technician doesn't handle raw passwords; supports the
  IntelliAdmin-style "just take control with admin rights" workflow.
- **Reachability:** endpoints expose no inbound port; the agent assists in
  establishing a brokered path (e.g. the agent opens/relays the RDP/VNC/SSH
  channel out to the gateway, or via a site-server relay) so control still flows
  without a listening service on the endpoint.
- **Authorization & scope:** RBAC gates who can control which devices (device-
  group scoping). Every session start/stop is an **audit event**.
- **Session recording:** sessions are recorded (Guacamole recording) and linked
  to their audit event; recordings stored in object storage per tenant.
- **Consent/notification policy:** configurable — attended (prompt the logged-in
  user) vs unattended (admin) — with unattended access still fully audited.
- **File transfer & clipboard:** policy-controlled.

## v2 — native WebRTC channel

- A native **WebRTC** channel in the agent for seamless **session shadowing** and
  attended support without exposing endpoint ports and without third-party client
  software. Built in-house specifically to **avoid AGPL** components (e.g.
  RustDesk core) that would contaminate the commercial offering.

## Agent capability

- `remote.session.open` (dormant until a session is requested).
- Establishes the brokered media path for the requested protocol; hands off to
  guacd (v1) or the native channel (v2). Optionally runs a low-privilege
  user-session helper to render consent UI in the logged-in user's context.

## Server side

- Guacamole gateway integration; session lifecycle; credential checkout from the
  vault (audited); recording capture and storage; RBAC/scoping enforcement.

## Data model additions

- `remote_session` (org_id, device_id, technician_id, protocol, started_at,
  ended_at, credential_ref, recording_ref, consent_mode)
- Links to `AuditEvent` (start/stop, credential access).

## Integration points

- **Credential vault** (hard dependency) for unattended admin creds.
- **Audit log** (hard dependency) — session and credential-access records.
- **Security-log** correlation (remote logons show up there too).
- **SOC 2 evidence** — privileged remote-access records.

## Open questions (resolve in spec)

- Exact brokering mechanism for zero-inbound-port reachability (agent-initiated
  reverse tunnel to gateway vs site-server relay) — decide with the gateway
  design.
- Consent policy defaults (when unattended admin is allowed vs user prompt
  required), which may be regulated by the customer's own policy.
- Recording retention and storage sizing.
