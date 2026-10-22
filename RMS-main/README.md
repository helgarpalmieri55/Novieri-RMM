# ITMS

**ITMS** — the IT Management System. A modular, web-based IT management and
endpoint control platform. Think
ManageEngine Endpoint Central / NinjaOne / Datto RMM in category: unified
inventory, patching, remote control, software deployment, imaging, backup, and
IT-governance workflows, driven by a single lightweight agent installed on every
managed computer and server.

Built **internal-first** (dogfooded across the fleet at Toeshee) and designed
from day one to be **commercialized** — on-premise now, cloud-ready later.

## Status

**The kernel and all 18 roadmap phases have shipped surface**, and the agent is
built: 376 routes under `/api/v1` (358 across 25 backend modules, 18 from the
kernel), 85 permission codes, 133 RLS policies, and ten agent capabilities in
Go. Measured 2026-08-16.

**This section deliberately does not enumerate phases.** It used to, and it was
wrong by fifteen of them — a hand-maintained list of "what's built" is stale the
week after it is written, and nothing forces anyone to re-check it. Two places
are authoritative instead, and both are derived from the tree rather than
recalled:

- [`docs/product.md`](docs/product.md) — what the platform is and does, with
  every number measured and dated.
- [`docs/06-roadmap.md`](docs/06-roadmap.md) — build order and per-phase status,
  with `backend/tests/test_roadmap_status.py` as the authority: it reads the
  live router, so a phase cannot be claimed built without routes to show for it.

Specs live beside the code they describe: [`docs/kernel/spec.md`](docs/kernel/spec.md)
for the platform kernel, `docs/modules/<module>/spec.md` per module, and
[`docs/kernel/wire-protocol/`](docs/kernel/wire-protocol/) for the agent wire
contract — which stays a **compatibility** contract even though both sides now
live in one tree, because a deployed agent is always older than the control
plane it talks to.

### Development quickstart

Backend (needs local PostgreSQL 16 with a `postgres` superuser and Redis):

```sh
cd backend
uv venv --python 3.12 && uv pip install -e ".[dev]"
.venv/bin/pytest              # full suite incl. RLS, signing, mTLS gateway
.venv/bin/mypy app            # strict
.venv/bin/ruff check app tests
```

Frontend:

```sh
cd frontend && npm install
npm test && npm run build     # vitest + tsc strict + vite build
```

End-to-end kernel demo (enroll → mTLS connect → signed job → verified result
→ audit chain):

```sh
backend/.venv/bin/python scripts/demo_e2e.py
```

Deployment: see [`deploy/README.md`](deploy/README.md). Disaster recovery:
[`docs/07-backup-restore.md`](docs/07-backup-restore.md) — what to back up,
how to restore into a clean stack, and how to confirm the audit chain and the
internal CA survived.

## What it does

[`docs/product.md`](docs/product.md) is the full, measured answer. In short:

| Area | Capabilities |
|---|---|
| **Fleet & inventory** | Enrollment and certificate lifecycle, hardware/software inventory, IPAM, network discovery |
| **Keeping machines current** | Patch management, application deployment, provisioning & imaging |
| **Access & support** | Audited remote control (RDP/VNC/SSH, decrypt-at-use credentials), service desk with SLAs, end-user portal, email intake |
| **Continuity** | File-level backup with dedup/encryption/retention and restore, BCP/DR test records, endpoint health & alerting |
| **Governance & paperwork** | Change management, security-log collection (SOC 2 evidence), IT documentation, licensing, subscriptions, budget, reporting |
| **Platform operations** | Structured logging, health & status page, error monitor, verified backup/restore and rollback |
| **AI assistance** | Operator copilot, portal assistant, and the admin surface that governs both |

## Architecture at a glance

Three roles:

- **Control plane** — the web application, API, database, job engine, and agent
  gateway. Runs on-prem in a container stack; the same components deploy to the
  cloud later.
- **Agent** — a single lightweight Go binary running as a service on every
  Windows, Linux, and macOS endpoint. Outbound-only mTLS connection; does
  nothing until the server sends it a signed job or a scheduled task fires.
  Lives under `agent/` in this repository, with its own CI job.
- **Site server** — an optional LAN-local helper for bandwidth-heavy or
  network-bound work (PXE/imaging, package caching, wake-on-LAN). Lets the
  control plane move to the cloud while heavy payloads stay local.

Full detail in [`docs/01-architecture.md`](docs/01-architecture.md).

## Documentation map

| Doc | Purpose |
|---|---|
| [`docs/product.md`](docs/product.md) | **Start here.** What the platform is and does, measured and dated |
| [`docs/00-vision.md`](docs/00-vision.md) | Product vision, positioning, licensing model, scope boundaries |
| [`docs/01-architecture.md`](docs/01-architecture.md) | System architecture, deployment topologies, on-prem→cloud path |
| [`docs/02-tech-stack.md`](docs/02-tech-stack.md) | Concrete technology choices and rationale |
| [`docs/03-security-model.md`](docs/03-security-model.md) | Trust model, mTLS, RBAC, credential vault, signed jobs, audit |
| [`docs/04-data-model.md`](docs/04-data-model.md) | Tenant-aware core data model |
| [`docs/05-agent-spec.md`](docs/05-agent-spec.md) | Agent comms protocol, job engine, self-update, idle budget |
| [`docs/06-roadmap.md`](docs/06-roadmap.md) | Phased build order and per-module spec cycle |
| [`docs/07-backup-restore.md`](docs/07-backup-restore.md) | Control-plane backup & restore runbook, audit-chain and CA recovery |
| [`docs/08-observability.md`](docs/08-observability.md) | Logging, probes and status, the error monitor, rollback, and what CI proves |
| [`docs/modules/`](docs/modules/) | One scoping brief per module |

## For Claude Code

Read [`CLAUDE.md`](CLAUDE.md) first. It is the operating contract for every
build session: repo conventions, the security invariants that must never be
violated, and the module build discipline.

## Licensing

Intended commercial posture is **open-core**: a permissively licensed core with
commercial modules/features. Every third-party dependency choice in these specs
was made to keep that path clean — notably avoiding AGPL components in the
agent, remote-control, and backup paths. See [`docs/00-vision.md`](docs/00-vision.md).
