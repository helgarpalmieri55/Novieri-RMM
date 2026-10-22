# Module — Endpoint Health, Alerting & Auto-Remediation

> Scoping brief. Added after the Atera comparison surfaced the one genuine gap in
> the module set: proactive **health monitoring + alerting + alert-triggered
> automation** (Atera's "automation profiles execute when an alert occurs"). This
> is what makes an RMM proactive rather than a request-response tool, and it is
> the **signal source** the Service Desk (alert→ticket) and AI Automation
> (alert→autonomous fix) consume. Recommended roadmap position: **early-mid** —
> after Inventory, ideally before or alongside Service Desk, since both feed on
> its signals. Expand into a full spec via the brainstorming skill before
> implementation.

## Purpose

Lightweight, threshold-based endpoint health monitoring with configurable alerts,
where a breach can automatically **open a ticket**, **run an approved remediation
job**, or **notify** — closing the loop from "something went wrong" to "it was
fixed" without a human necessarily touching it.

## The boundary that makes this buildable (read first)

This overlaps, on paper, with the sister **monitoring platform**. The line is
deliberate and must hold:

- **This module = endpoint health & state, evaluated at the edge.** "Is this
  managed device healthy? Did a metric cross a threshold? Fire an alert / ticket /
  fix." Thresholds evaluated **on the agent**; only **breach events and a compact
  periodic health summary** reach the server. **No streamed metrics, no long-term
  time-series storage, no SNMP fleet polling, no network topology.**
- **The monitoring platform = deep observability.** High-resolution time-series,
  historical trends, network/infrastructure monitoring, SNMP, dashboards for
  investigation. Its own product, its own agent.

Rule of thumb: if the question is *"act on this device now,"* it belongs here; if
it's *"analyze this trend over time,"* it belongs to monitoring. When richer
signal is needed, this module can reference the monitoring platform rather than
duplicate it.

## Idle-budget constraint (also non-negotiable)

Threshold checking must respect the agent's idle budget (`docs/05-agent-spec.md`):

- A **slow timer tick** samples cheap OS counters (CPU, memory, disk free,
  service state, key event-log conditions); **event-driven** where the OS exposes
  it (service-stopped events, disk-space events) instead of polling.
- The agent holds only current values + thresholds; it does **not** retain metric
  history. It emits an event **only on breach / recovery**, plus an occasional
  compact health summary.
- No high-frequency sampling, no always-on collector beyond the light tick. If a
  use case needs high-resolution metrics, that is a monitoring-platform signal,
  not a reason to inflate this agent.

## Scope — v1

- **Health checks (per policy, per device group):** CPU, memory, disk free/SMART
  where cheap, service running/stopped, process presence, key Event Log / syslog
  conditions, agent/device online-offline, and security-posture facts already
  gathered by inventory (disk encryption on/off, AV present/healthy, firewall
  state).
- **Threshold profiles:** define conditions and durations ("CPU > 95% for 10 min",
  "free disk < 10%", "service X stopped", "BitLocker off") assigned to devices or
  groups — the direct analog of Atera's threshold/alert profiles.
- **Alerts:** raised on breach with severity, dedup/flap-suppression, and
  auto-clear on recovery; alert list/console; notification routing (email/chat).
- **Alert-triggered automation (the loop):** an alert can trigger an
  **approved-action** remediation — but **only by dispatching a signed job**
  through the existing runner, gated by the same approved-action model and
  autonomy dial as the AI layer (suggest → require-approval → autonomous). E.g.
  low-disk → run disk-cleanup job; service down → restart-service job.
- **Alert → ticket:** an alert can open (and later auto-close) a Service Desk
  ticket, with full context, so proactive issues enter the same queue as
  user-reported ones.
- **Escalation:** unremediated/critical alerts escalate to notification or a
  technician per policy.

## Later phases

- Correlated/composite alerts (multiple conditions), maintenance-window
  suppression, dependency-aware muting.
- Predictive flags (disk-fill-rate, memory-leak trend) — coordinated with the
  monitoring platform for the heavier trend analysis rather than storing series
  here.
- Richer auto-remediation playbooks (multi-step) — overlaps the AI layer's
  orchestration; build once, shared.

## Agent capability

- `health.check` — the light, scheduled/event-driven threshold evaluator;
  **dormant between ticks**, emits only breach/recovery/summary. This is the one
  new capability, and it is designed to be the *cheapest* resident behavior on the
  agent.
- Remediation itself reuses **existing** job types (script, service restart,
  cleanup, deploy) — no new action code, so no idle-budget cost beyond the tick.

## Server side

- Threshold/alert policy engine; alert lifecycle (raise/dedup/suppress/clear/
  escalate); the alert→job and alert→ticket triggers (reusing the approved-action
  registry and Service Desk); notification routing; a live alert console.

## Data model additions

- `threshold_profile` (org_id, scope, conditions jsonb, severity, actions jsonb:
  notify / run_action / open_ticket)
- `alert` (org_id, device_id, profile_id, condition, severity, state:
  active/acknowledged/cleared, raised_at, cleared_at, ticket_id nullable,
  action_job_id nullable)
- `device_health` (org_id, device_id, summary jsonb, updated_at) — **current**
  snapshot only; no history table (that is monitoring's domain).

## Integration points

- **Inventory** — supplies device/security-posture facts to evaluate.
- **Service Desk** — alert→ticket (and auto-close on recovery).
- **AI Automation** — alerts are a primary signal for autonomous remediation;
  both share the **approved-action registry + autonomy dial + audit**. An alert is
  exactly the "pre-ticket proactive remediation" trigger flagged in the AI brief.
- **Signed-job runner + audit** — every auto-remediation is a signed, audited job.
- **Monitoring platform** — the boundary partner: reference it for deep
  time-series; do not duplicate it.
- **Reporting & Analytics** — alert volume, MTTR, auto-remediation success rate.

## Open questions (resolve in spec)

- Exact v1 check set and default thresholds (start with CPU/mem/disk/service/
  encryption/AV/online).
- Tick cadence vs event-driven split per OS that stays inside the idle budget.
- Which remediations may ever be autonomous vs require-approval by default
  (align with the AI layer's mandatory-human categories — nothing destructive or
  fleet-wide auto-fires).
- The precise metric-depth line with the monitoring platform, written down so the
  two products never drift into duplicating each other.
