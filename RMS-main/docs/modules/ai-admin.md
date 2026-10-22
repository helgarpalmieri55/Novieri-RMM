# Module — AI Admin Agent (Platform Operations Copilot)

> Scoping brief. Completes the AI layer's persona set: `ai-automation.md` specs
> the **end-user tier-1 resolver** and the **technician copilot**; this brief adds
> the third persona — an **admin-facing agent that operates the platform
> itself**. It shares the same orchestration substrate (Claude integration,
> approved-action registry, autonomy dial, audit) — do **not** build a second AI
> stack. Roadmap: **read/analyze mode can ship early** (it is effectively
> conversational reporting); **act mode** rides the AI Automation layer's
> machinery. Expand into a full spec via the brainstorming skill before
> implementation.

## Purpose

A conversational operations agent for platform administrators: query the entire
estate in natural language, investigate incidents across modules, and compose and
execute platform operations — "patch the finance group in Friday's window," "why
did SRV-02's backup fail last night," "deploy Chrome to new-hires," "show me
devices with BitLocker off and remediate" — with every mutation previewed,
confirmed, and executed through the platform's own governed machinery.

## The governing principle (read first)

**The admin agent has no powers the admin doesn't have.** It runs as the
authenticated administrator, acts exclusively through the platform's own APIs and
signed-job engine, and is bounded by that admin's RBAC scope and the org's
approval/change rules. It is a faster pair of hands on the same controls — never
a privileged side channel, never a bypass of approvals, change windows, or the
signed-job invariant.

## Two modes

1. **Read / analyze — low risk, high value, ships first.** Natural-language
   queries and investigation across every module: inventory, alerts, tickets,
   patch status, deployments, backups, IPAM, licenses/subscriptions/spend, audit.
   Cross-module correlation ("device X is alerting — pull its patch history, disk
   state, and recent changes"). Read-only, RBAC-scoped, no approval friction.
2. **Act — governed.** Compose operations into concrete platform actions:
   dispatch approved-action jobs, run automation profiles, create tickets and
   change records, schedule patch/deploy campaigns, assign IPs, generate reports.
   Always **preview → confirm → execute**: the agent shows exactly what will run
   — action, parameters, target list and count, blast radius — before anything
   fires. The same per-action **autonomy dial** applies, with stricter admin
   defaults: **fleet-wide or destructive operations are never autonomous.**

## Scope — v1

- Chat surface in the admin console (Slack/Teams later).
- **Read/analyze across all modules present at build time**, via tool-use over
  the modules' own APIs.
- **Act mode for an initial governed set:** run existing automation profiles,
  dispatch approved-action jobs to scoped targets, create tickets/changes,
  trigger reports.
- **Preview-confirm UX** with explicit targets ("this runs on 37 devices in
  group *Finance* — confirm").
- **Full transparency:** every query and action logged (reusing
  `ai_session`/`ai_action` with a persona field) and audit-chained like any
  operator action, attributed to admin + agent.

## Later phases

- Scheduled **ops briefings** (morning digest: fleet health, overnight alerts,
  pending approvals, upcoming renewals and windows).
- **Proactive suggestions** (config drift, stale devices, unpatched clusters) —
  suggestions only; the dial governs anything more.
- **Policy/config authoring assistance** (draft a threshold profile, patch ring,
  or backup policy — human reviews and applies).
- Cross-module orchestrations (onboarding/offboarding) — shared with the AI
  layer's later-phase orchestrations; build once.

## Agent capability

**None. Zero endpoint code.** The admin agent lives entirely in the control plane
and touches endpoints only via existing signed jobs — so it adds nothing to the
endpoint agent's footprint or attack surface.

## Server side

- **Reuses the AI orchestration service** from `ai-automation.md` (same Claude
  integration, same tool-registry pattern); adds an **admin tool set**: read
  tools mapped to module query APIs, act tools mapped to job dispatch and
  platform mutations, each tagged with a risk class and autonomy default.
- **Identity-bound sessions:** the authenticated admin's identity, RBAC scope,
  and org context are injected into every tool call; the model cannot widen them.
- **Prompt-injection hardening as a spec-level requirement:** the agent
  constantly reads user-authored and device-originated text (tickets, hostnames,
  logs, docs). All retrieved content is treated as **data, never instructions**;
  tool invocation is constrained to the registry regardless of what retrieved
  text says.
- A side benefit worth naming: this persona is a **forcing function for clean,
  well-documented module APIs**, since every module's API doubles as the agent's
  tool surface.

## Data model additions

- Reuse `ai_session` / `ai_action` with `persona: end_user / technician / admin`.
- Extend the **single** `approved_action` registry with persona scope and risk
  class rather than creating a parallel admin registry — one registry, one dial,
  one audit story.

## Integration points

- **AI Automation substrate** (hard dependency for act mode): orchestration
  service, approved-action registry, autonomy dial, AI audit.
- **Every module's API** as the read tool surface.
- **RBAC / Keycloak** — sessions bound to the admin's real permissions.
- **Reporting & Analytics** — read mode is conversational reporting; share the
  query layer rather than duplicating it.
- **Endpoint Health & Alerting** — alerts are a primary investigation entry
  point ("what's alerting and why").
- **Audit** — every action attributed and chained.

## Open questions (resolve in spec)

- Registry model detail: persona + risk-class dimensions on the one
  approved-action registry (recommended) vs a separate admin tool registry.
- The initial act-mode set for internal v1 (start with: run automation profile,
  scoped approved-action dispatch, create ticket/change, generate report).
- Whether read mode ships ahead of the full AI layer (recommended: yes — pair it
  with Reporting; it is read-only and immediately valuable).
- Confirmation ergonomics: per-action confirm vs a batch plan with a single
  confirm; how previews render large target lists.
- Hard caps: maximum targets for any non-confirmed action (recommended: zero —
  every mutation confirms in v1), rate limits, and the mandatory-human category
  list shared with `ai-automation.md`.
