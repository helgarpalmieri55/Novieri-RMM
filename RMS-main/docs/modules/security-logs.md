# Module — Security Log Collection

> Scoping brief. Phase 2. Contained, event-driven, and an early SOC 2 evidence
> source. Expand into a full spec via the brainstorming skill before
> implementation.

## Purpose

Centrally collect security-relevant logs from every endpoint — primarily
login/logout and privileged-action events — for search, retention, and
compliance evidence. This is **evidence collection, not threat detection** (EDR
is out of scope; vulnerability management is Sentinel's job).

## Scope — v1

- **Login/logout events:** interactive, remote (RDP/SSH), and service/scheduled
  logons; logon failures; account lockouts.
- **Privileged-action events:** privilege elevation, admin group changes, and
  security-relevant policy changes where the OS surfaces them.
- **Central ingest & search:** ship events to the control plane; store with
  retention; provide filtered search by device, user, event type, time.
- **Retention & tamper-evidence:** enforce a retention window; events are stored
  immutably (aligned with the platform's audit posture) for evidence integrity.
- **Evidence reporting:** access/logon reports suitable for **SOC 2** (who logged
  into what, when; privileged access over a period).

## Later phases

- Broader log sources (application/security channels beyond logon/privilege).
- Alerting rules (e.g. repeated failures) — carefully, without becoming an EDR.
- Correlation with remote-session audit and change records.

## Agent capability

- Job/stream type `logs.ship`.
- **Event-driven, not polling:** subscribe to OS logging channels —
  - Windows: Security Event Log subscription (logon/privilege event IDs).
  - Linux: `auth.log`/`journald`/auditd, pam session events.
  - macOS: unified log (`OSLog`) auth events.
- The subscription is the one capability that is "listening," but it is
  event-driven and lightweight; between events the agent is idle. Buffer and ship
  on cadence; **buffer offline** and flush on reconnect (bounded).

## Server side

- Ingests, normalizes, and stores security events keyed by device/org.
- Enforces retention; indexes for search.
- Produces access/logon evidence reports and exports.

## Data model additions

- `security_event` (org_id, device_id, ts, event_type, user, source, outcome,
  raw jsonb) — stored immutably, indexed on (org_id, device_id, ts, event_type).
- `security_event_retention` policy per org/group.

## Integration points

- **SOC 2 evidence** exports (access records).
- Correlates with **remote-control** session audit and **change** records.
- Exportable to **Power BI** / SIEM if a customer has one.

## Open questions (resolve in spec)

- Exact event set per OS for v1 (which Windows Event IDs, which Linux facilities).
- Storage strategy for volume: PostgreSQL with partitioning/retention vs a
  dedicated log store if fleet volume is high (keep relational unless proven
  otherwise — high-volume telemetry is the monitoring product's domain).
- Retention defaults and how they map to SOC 2 requirements.
