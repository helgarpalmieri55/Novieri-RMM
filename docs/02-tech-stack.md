# 02 — Technology Stack

Every choice here has a rationale and, for major dependencies, a license note —
because the commercial posture is open-core and copyleft in the wrong place would
contaminate it. Choices are aligned with the existing Toeshee stack (notably the
Sentinel platform) so the two share patterns and operational knowledge.

## Control plane — backend

| Concern | Choice | Rationale |
|---|---|---|
| Language | **Python 3.12** | Matches Sentinel; team fluency; rich ecosystem |
| Web framework | **FastAPI** | Async, typed, first-class WebSocket support for the agent gateway, OpenAPI out of the box |
| ORM | **SQLAlchemy 2.x** (typed) | Mature, supports RLS/session-level tenant context |
| Migrations | **Alembic** | Forward-only, reviewed schema evolution |
| Validation | **Pydantic v2** | Request/response models, settings |
| Async work & scheduling | **Celery + Redis** | Server-side async jobs, beat scheduling; same broker as pub/sub |
| Agent gateway | FastAPI WebSocket, separable into its own asyncio service | Long-lived mTLS connections, scalable independently |

License posture: all permissive (MIT/BSD/Apache). Clean for open-core.

## Control plane — frontend

| Concern | Choice | Rationale |
|---|---|---|
| Framework | **React + TypeScript (strict)** | Team fluency; large asset grids and live views |
| Build | **Vite** | Fast dev/build |
| Server state | **TanStack Query** | Caching, background refetch for fleet views |
| Live updates | Native WebSocket to a UI channel | Real-time job/session/device status |
| Component layer | shadcn/ui **or** Mantine (decide in Phase 0) | Data-dense admin UI; both permissive |
| Tables | TanStack Table | Sorting/filtering/virtualization for large fleets |

## Agent

| Concern | Choice | Rationale |
|---|---|---|
| Language | **Go 1.22+** | Single static binary, cross-compiles to Windows/Linux/macOS, low idle footprint, easy fleet distribution — the decisive reason a Python agent was rejected |
| Distribution | One signed static binary per OS/arch | Trivial deployment; no runtime to install |
| WebSocket client | `coder/websocket` (formerly nhooyr) or `gorilla/websocket` | Small, well-maintained |
| Service integration | Windows SCM, systemd, launchd | Native per-OS service lifecycle |
| Footprint target | low-tens-of-MB resident, ~0% idle CPU | Hard requirement (`docs/05-agent-spec.md`) |

License posture: the agent is **original Apache-2.0 Go code**. It deliberately
does **not** embed AGPL components (e.g. RustDesk core) so it stays commercially
distributable.

## Remote control

| Concern | Choice | Rationale |
|---|---|---|
| Remote screen | Own **rsp/1** screen share (agent helper + relay + canvas viewer) | Shares the user's real console over the agent's outbound mTLS; no new dependency. Apache Guacamole (guacd) was the v1 plan and was retired 2026-09-23 (`docs/modules/remote-control/spec-screen-share.md`) |
| v2 channel | Native **WebRTC** channel in the agent | Seamless session shadowing / attended support without exposing endpoint ports; built in-house to avoid AGPL |

Detail in `docs/modules/remote-control.md`.

## Data & storage

| Concern | Choice | Rationale |
|---|---|---|
| Primary DB | **PostgreSQL 16** | Matches Sentinel; Row-Level Security for tenant isolation; strong indexing for fleet/audit queries |
| Cache / queue / pub-sub | **Redis** | Celery broker + result backend + live status fan-out across gateway replicas |
| Object storage | **S3-compatible** — MinIO on-prem, S3 in cloud | Backups, OS images, packages, agent binaries, evidence exports; keeps the DB lean; identical API on-prem and cloud |

## Identity & secrets

| Concern | Choice | Rationale |
|---|---|---|
| Identity broker | **Keycloak** | OIDC/SAML, AD/Entra federation, MFA, user/role storage out of the box — avoids building SSO/MFA/federation; clean commercialization story. (Apache-2.0) |
| Secrets / credential vault | **HashiCorp Vault** (transit + KV) | Consistent with Sentinel; envelope encryption for stored admin credentials; decrypt-at-use only |
| Agent PKI / internal CA | **Vault PKI** or **step-ca** | Issues and rotates agent client certificates for mTLS enrollment |

Vault license note: recent Vault is BSL. If BSL is unacceptable for the commercial
posture, **OpenBao** (the Linux Foundation fork, MPL-2.0) is the drop-in
substitute; the spec treats "Vault-compatible transit + PKI + KV" as the
interface. Decide in Phase 0.

## Packaging, delivery, ops

| Concern | Choice | Rationale |
|---|---|---|
| Containerization | **Docker** + Compose (on-prem), **Helm/Kubernetes** (cloud) | One artifact set, two deployment shapes |
| Reverse proxy / TLS | **Traefik** or **nginx** | TLS termination for web/API; routes agent-gateway |
| CI | GitHub Actions (or GitLab CI) | Build/test/sign agent binaries and container images |

## Per-OS toolchains (used by modules, not by the kernel)

These are the native mechanisms module capabilities drive on each OS. They are
recorded here so module specs reference a single source of truth.

**Patch management**
- Windows: Windows Update Agent API + `winget`; WSUS optional as an upstream
  source.
- Linux: `apt` / `dnf` / `zypper` per distro family.
- macOS: `softwareupdate` for OS/Apple updates (with MDM required for OS updates
  on Apple Silicon), Homebrew for third-party.

**Provisioning & imaging**
- Windows: WinPE + WDS-style image deploy driven by the site server's PXE, with
  `unattend.xml`.
- Linux: PXE + kickstart / preseed / cloud-init.
- macOS: **not imaging** — Automated Device Enrollment + MDM + `startosinstall`
  (Apple Silicon cannot be imaged). See `docs/modules/provisioning.md`.

**Application deployment**
- Windows: MSI/EXE with PSAppDeployToolkit, `winget`, Chocolatey.
- Linux: native packages + scripts.
- macOS: `pkg`/`dmg`, Homebrew, Installomator.

**Backup engine (built in-house)**
- Cross-OS file/folder capture; volume snapshots via **VSS** (Windows), **LVM**
  snapshots (Linux), **APFS** snapshots (macOS). Repository format, dedup,
  encryption, and retention are original code — see `docs/modules/backup.md`.

## Integrations

| System | Mechanism |
|---|---|
| AD / Entra ID | SSO via Keycloak federation (OIDC/SAML), MFA |
| Odoo Helpdesk | REST/JSON-RPC for change tickets and change/deploy records |
| Sentinel (vuln mgmt) | API pull of prioritized vulnerable targets to drive patching |
| Power BI / reporting | Evidence and inventory exports (CSV/API) |

## Decisions to finalize in Phase 0

- Component library: shadcn/ui vs Mantine.
- Vault vs OpenBao (license posture) — interface is identical.
- Internal CA: Vault PKI vs step-ca.
- Message encoding on the agent wire: JSON envelope for v1 (chosen for
  debuggability at low control-message volume) vs protobuf/msgpack later if
  profiling justifies it — see `docs/05-agent-spec.md`.
