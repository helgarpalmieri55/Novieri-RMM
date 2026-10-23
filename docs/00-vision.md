# 00 — Vision

## The product

A unified IT management platform that installs a single lightweight agent on
every managed computer and server, and from one web console lets an IT team
inventory, patch, remotely control, deploy software to, image, back up, document,
and govern that fleet.

The category is **RMM + Endpoint Management + IT-governance workflows** — the
space occupied by ManageEngine Endpoint Central, NinjaOne, Datto RMM, Atera, and
Kaseya VSA. What distinguishes this build:

- **One agent, three OSes, genuinely lightweight.** Windows, Linux, and macOS
  from v1, with a hard idle-footprint budget so it is safe to deploy on
  production servers and end-user machines alike.
- **Governance is first-class, not bolted on.** Change management, BCP/DR test
  reporting, and security-log evidence are built into the same platform that
  performs the changes — so patch records, deployment records, change tickets,
  and DR test evidence come out of the tool auditors already need.
- **On-prem first, cloud-native later, with the same code.** Multi-tenancy,
  outbound-only agents, and a site-server role are designed in from day one so
  commercialization is a deployment change, not a rewrite.

## Who it's for

**Phase one — internal.** Toeshee's own fleet (~250–300 endpoints across a BPO
operation). The internal deployment is the first customer and the proving ground:
it must reduce real IT toil (patching, imaging new-hire machines, helpdesk remote
sessions, backup assurance) and produce real SOC 2 evidence.

**Phase two — commercial.** Small and mid-sized IT teams and MSPs who want an
integrated alternative to stitching together separate RMM, deployment, and
documentation tools. Multi-tenancy makes the MSP case (one console, many client
organizations) native rather than retrofitted.

## Positioning against alternatives

| Alternative | Gap this fills |
|---|---|
| ManageEngine / Kaseya | Heavy, expensive, dated UX; this is a modern, integrated, self-hostable option |
| NinjaOne / Atera (SaaS-only) | No on-prem/self-host story; this runs in your rack first |
| Stitched point tools (PDQ + ITGlue + Veeam + a remote tool) | This unifies inventory, deployment, docs, backup, and remote under one agent and one console |
| Open-source RMM (Tactical RMM et al.) | Governance modules (change, BCP, evidence) and a cleaner commercialization/licensing posture |

## Licensing model

Target posture is **open-core**:

- A permissively licensed **core** (control-plane kernel + agent) — Apache-2.0 or
  similar — to make self-hosting and adoption frictionless.
- **Commercial modules / tiers** for the higher-value capabilities (advanced
  backup, imaging at scale, multi-tenant MSP management, SSO/compliance packs).

Every dependency decision in these specs protects that path. In particular the
agent, remote-control, and backup subsystems **avoid AGPL-licensed components**
(e.g. RustDesk's core), because AGPL would contaminate a commercial offering.
Remote control uses Apache-licensed Apache Guacamole; the agent is original Go.
See `docs/02-tech-stack.md` for the license posture of each major dependency.

## Scope boundaries (what this is NOT)

- **Not a network/infrastructure monitoring platform.** Deep metric collection,
  network topology, ISP/latency monitoring, and time-series observability are a
  **separate product** (the SolarWinds-style monitoring platform). This suite and
  that platform each ship their **own dedicated agent** and are **not** merged.
  This suite may surface basic device up/down and health, but time-series
  telemetry at scale is out of scope here.
- **~~Not an MDM in the Apple/Google sense (v1).~~ REVERSED 2026-08-16 —
  DECIDED, NOT BUILT.** *(Previous text: "The agent does inventory, patch,
  scripting, and remote on macOS in v1. True MDM-grade configuration profiles and
  APNs-driven management on Apple Silicon are a fast-follow, because they require
  a different enrollment mechanism (Apple MDM + APNs), not just another agent
  job.")* The operator has asked for **true MDM including phones and tablets**.
  The reasoning in the old text is not wrong and is the reason this is a module
  with a **second enrollment mechanism** rather than a new agent capability:
  Apple MDM + APNs and Android EMM manage devices that will never run our agent,
  so the mTLS gateway is not the transport and the signed-job engine is not the
  command path for them. What that means for the security invariants — above
  all invariant 1, "no unsigned command path" — is the first thing the MDM spec
  must settle, because Apple's protocol has its own signing and trust model and
  we do not get to impose ours on it.

  **APPROVED 2026-08-16.** The spec is written
  (`docs/modules/mdm/spec.md`, 1,733 lines) and the operator accepted its
  proposed **invariant 1b**, now recorded in `CLAUDE.md` — invariant 1
  unchanged, a second clause governing a second command path. The spec is
  explicit that invariant 1's endpoint-side guarantee does not survive on a
  protocol Apple controls, and what replaces it: the MDM path carries no
  platform-authored executable payload, ever.

  **Still nothing is built.** Do not read this bullet as a capability.
- **The platform owns ticketing.** *(Revised — this was previously a non-goal:
  "not a full ITSM suite … integrates with Odoo Helpdesk for ticketing rather
  than reimplementing a service desk." That decision is reversed deliberately;
  see `docs/modules/service-desk/spec.md` §0.1.)* Two reasons: the platform is
  built to be commercialized standalone, and a customer with no Odoo still needs
  a queue; and a ticket that shows the affected device's inventory, patch state,
  and open alerts inline is something an external helpdesk structurally cannot
  do. Scope is still bounded — Service Desk is incidents and service requests
  for IT, not a full ITSM suite with CMDB, problem management, and release
  management. Change management remains a separate, IT-change-focused module.
- **Not an EDR/antivirus.** Security-log collection feeds evidence and audit; it
  is not a threat-detection engine. Vulnerability management is handled by the
  separate **Sentinel** platform, which this suite integrates with to prioritize
  patching.

## Success criteria

**Internal (must hold before commercial pursuit):**

- Agent deployed fleet-wide across Windows/Linux/macOS with negligible idle
  footprint and no server-inbound exposure.
- IT can inventory the fleet, run remote helpdesk sessions with audited admin
  access, patch across OSes, and deploy standard software from the console.
- Backups run and **restores are proven**, not just scheduled.
- The platform emits usable SOC 2 evidence: patch compliance, change records,
  security logs, BCP/DR test reports.

**Commercial readiness:**

- Multi-tenant isolation verified.
- Cloud deployment path exercised (control plane in cloud, site servers on
  customer LANs).
- Clean open-core licensing boundary with no copyleft contamination.

## Integration posture

The platform is a good citizen in Toeshee's existing stack and a sellable one
outside it:

- **Identity:** AD / Entra ID via SSO (OIDC/SAML) with MFA.
- **Ticketing:** owned by the platform's own Service Desk module. Odoo remains
  the ERP — Budget exports expense records to it (`docs/modules/budget/spec.md`
  §7) — but it is no longer where tickets live. Change management's
  `ChangeTicketPort` now targets the internal Service Desk rather than Odoo
  Helpdesk.
- **Vulnerability data:** Sentinel feeds prioritized patch targets.
- **Reporting/BI:** evidence and inventory exportable (Power BI-friendly).
