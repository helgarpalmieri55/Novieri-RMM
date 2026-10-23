# 01 — Architecture

## Three roles

The system is composed of three deployable roles. This separation is what makes
"on-prem now, cloud later" real rather than aspirational.

```
                        ┌───────────────────────────────────────────────┐
                        │                 CONTROL PLANE                   │
                        │                                                 │
   Operators ──HTTPS──▶ │  Web App (React)  ─┐                            │
                        │                     ├─▶ API (FastAPI)           │
                        │  Agent Gateway  ◀───┘   │                       │
                        │  (mTLS WebSocket)       ├─▶ Job Engine          │
                        │        ▲                │   (signs jobs)        │
                        │        │                ├─▶ Auth (Keycloak)     │
                        │        │                ├─▶ Audit (hash-chain)  │
                        │        │                └─▶ PostgreSQL + Redis  │
                        │        │                    Object storage (S3) │
                        └────────┼────────────────────────────────────────┘
                                 │ outbound-only mTLS (agent initiates)
              ┌──────────────────┼───────────────────────────────┐
              │                  │                                │
        ┌─────┴─────┐      ┌─────┴──────┐                  ┌──────┴───────┐
        │  Agent    │      │  Agent     │   ...            │ Site Server  │
        │ (Windows) │      │  (Linux)   │                  │ (LAN helper) │
        └───────────┘      └────────────┘                  │ PXE / cache  │
                                                           │ WoL / relay  │
                                                           └──────┬───────┘
                                                                  │ LAN
                                                        ┌─────────┴─────────┐
                                                        │ Bare-metal / PXE  │
                                                        │ provisioning tgts │
                                                        └───────────────────┘
```

### Control plane

The web application, HTTP API, agent gateway, job engine, auth, database, cache,
and object storage. On-prem this is a container stack on one host (or a small
cluster); in the cloud it is the same components behind managed services.

Internally decomposed as:

- **Web App** — React/TypeScript SPA. Talks only to the API and a UI WebSocket
  for live status.
- **API** — FastAPI. All business logic, RBAC enforcement, tenancy enforcement.
- **Agent Gateway** — the mTLS WebSocket endpoint agents connect to. Terminates
  agent connections, authenticates them by client certificate, routes jobs down
  and results up. **Designed as a separable service** so it can scale
  independently of the API when the fleet grows (see Scaling below).
- **Job Engine** — enqueues, **signs**, dispatches, and tracks jobs; owns the
  job lifecycle and retries. Backed by Celery + Redis for server-side async and
  scheduling.
- **Auth** — Keycloak as identity broker (OIDC/SAML, AD/Entra federation, MFA,
  user/role storage).
- **Audit** — append-only, hash-chained event log.
- **Data** — PostgreSQL 16 (relational + tenant isolation via RLS), Redis (queue,
  cache, live pub/sub), S3-compatible object storage (agent binaries, packages,
  images, backup repositories, evidence exports).

### Agent

A single lightweight Go binary running as a service on every managed endpoint
(Windows service / systemd unit / launchd daemon). It:

- Connects **outbound only** to the gateway over mTLS and parks on that
  connection.
- Executes **only signed jobs** pushed from the server, plus locally scheduled
  recurring tasks.
- Carries capability handlers (inventory, patch, remote, deploy, backup, logs,
  provisioning) that stay dormant until a job invokes them.
- Self-updates through a signed `agent.update` job pinned to the release
  SHA-256, with local rollback and ring rollout
  (`docs/modules/agent-distribution/spec-self-update.md`, decided 2026-09-23).

Full protocol and lifecycle in `docs/05-agent-spec.md`. The agent lives in this
repository under `agent/` and is **not** shared with the monitoring platform.
(It previously lived in a separate `it-suite-agent` repository; that decision
was reversed on 2026-08-05 — `CLAUDE.md`, commit `a32cead`. The agent is
specified and unbuilt either way. The gateway boundary above is unchanged, and
is still a **versioned wire contract** rather than an internal call: agents in
the field lag the control plane by however long they take to self-update.)

### Site server

An optional LAN-local role for work that is bandwidth-heavy or must originate on
the local network:

- **PXE / imaging** — hosts the boot environment and image payloads for
  provisioning, so gigabytes of image data never traverse the WAN.
- **Package / patch / binary cache** — caches deployment packages, patches, and
  agent binaries for the local subnet.
- **Wake-on-LAN relay** — sends magic packets on the local segment.
- **Connection relay (optional)** — can proxy agent↔control-plane traffic for
  isolated network segments.

This is the SCCM "distribution point" pattern. It is what lets the control plane
move to the cloud while LAN-bound operations stay fast and local. A site server
is itself an authenticated component (mTLS to the control plane); it is not
trusted implicitly.

**As built (2026-08-06) — `docs/modules/site-server/spec.md`.** A site server
**is a promoted `Device`**: an ordinary enrolled endpoint with an ordinary
agent and an ordinary device-bound certificate, plus one row in a `site_server`
registry table declaring which segments it serves. It is not a new job target,
not a new connection identity and not a new certificate profile — which is why
supporting it required no kernel change at all.

Of the four roles listed above, **v1 builds one**: Wake-on-LAN, as the
`site.wake` job type. The control plane computes the broadcast **address** from
the operator-declared segment containing the target and signs it into the job's
params; the agent broadcasts to that address and nothing else, so the endpoint
never decides what it may touch. v1 also makes IPAM prefer a site server for a
sweep of a segment it serves — one term in an existing `ORDER BY`.

The other three are **specified and not built**, each blocked on something
named:

- **PXE / imaging** and the **package / patch / binary cache** both need a
  listening socket, which `CLAUDE.md` invariant #2 forbids ("no agent listening
  port"). The amendment that would permit read-only content responders is
  drafted in site-server spec §4.4 and **has not been accepted**. The cache has
  a second, larger prerequisite: there is no agent-facing artefact origin-fetch
  path in this repository at all, so it would be a cache in front of nothing.
- **Connection relay** — if ever built, **opaque TCP forwarding only, never TLS
  termination**. A relay that terminated TLS would be a machine-in-the-middle on
  the control channel of every endpoint behind it, seeing every signed envelope
  in plaintext including any credential a job carries.

## Communication model

**Agents never accept inbound connections.** The agent dials the gateway,
authenticates with its client certificate, and holds a long-lived mTLS WebSocket.
The server pushes jobs down that channel; the agent streams status and results
back up. Consequences:

- No firewall exceptions on endpoints; works behind NAT and for remote/WFH
  machines with no VPN.
- Identical behavior whether the control plane is in your rack or in the cloud —
  only the endpoint the agent dials changes.
- The blast radius of a compromised endpoint does not include "listening service
  reachable from the network."

Operators reach the system over HTTPS to the web app / API. Remote-control media
is the agent's own screen share: the agent dials a second outbound mTLS
WebSocket to the gateway's relay and the api joins the browser to it (see
`docs/modules/remote-control/spec-screen-share.md`; guacd was retired
2026-09-23), so the endpoint still exposes no inbound management port.

## Multi-tenancy

Tenancy is designed in from day one. **On-prem is simply a single-tenant
deployment** (one `Organization` row); SaaS/MSP turns on many. Every core and
module entity carries `org_id`, and isolation is enforced at the **database
layer** via PostgreSQL Row-Level Security — not only in application code — so a
forgotten filter in a query cannot leak across tenants. Details and the RLS
strategy are in `docs/04-data-model.md`.

## Deployment topologies

### On-prem, single node (internal v1)

Docker Compose stack on one adequately-sized host:

```
[ Traefik/nginx TLS ] → [ web ] [ api ] [ agent-gateway ]
                         [ keycloak ] [ postgres ] [ redis ]
                         [ minio (S3) ]
                         [ internal CA (Vault PKI / step-ca) ]
Agents across the LAN + optional site server(s) per location.
```

The `redis` service runs **Valkey** (BSD-3) since 2026-09-23; the code uses
only Redis OSS 7.2 commands, so any Redis-protocol server works.

#### Open decision: the object store (recorded 2026-09-23, not decided)

MinIO is pinned to `RELEASE.2025-09-07T16-13-09Z`. It is AGPL-3.0, which
conflicts with the "avoid AGPL" posture for a commercial product, and the
community edition no longer ships binaries or images: the pin receives no
security fixes. It is **not** replaced yet, because two things depend on
behaviour a replacement must prove first:

- **Presigned POST policies** — endpoints upload backup data with a
  browser-style POST policy (`app/core/storage.py`), not only presigned PUT.
- **Object Lock** (compliance retention) — the off-site backup copy refuses a
  target bucket without it (`app/modules/backup/offsite.py`), and CI's
  `backup-e2e` job uses a second MinIO as that target.

Candidates, each to be measured against those two and against
`backend/tests/test_backup_e2e_minio.py` before any switch:

| Candidate | Licence | Known gap to check |
|---|---|---|
| Garage | AGPL-3.0 | same licence problem; Object Lock not known to be supported; POST policy to verify |
| SeaweedFS (S3 gateway) | Apache-2.0 | POST policy and Object Lock support are recent; verify retention semantics |
| A pinned, patched MinIO source build | AGPL-3.0 | keeps behaviour, keeps the licence problem, and we own the patching |

Until decided: keep the pin, track MinIO security advisories by hand, and
never expose MinIO beyond the presign-only route on `:443`.

**Image source: built here, from source.** On 2026-09-23 the `minio/minio`
repository stopped resolving on Docker Hub; on 2026-09-24 `quay.io/minio/minio`
began refusing anonymous pulls (401) and `dl.min.io`'s release archive answers
410. MinIO no longer distributes free images or binaries anywhere. The
operator chose to build it rather than trust a third party's rebuild:
`deploy/minio/Dockerfile` compiles `minio` and `mc` from their public source,
each pinned to a release tag AND the commit that tag names (the build refuses
if they disagree), into `itsuite/minio:<release>`. `docker compose up -d`
builds it the first time (a few minutes, needs github.com and the Go module
proxy); CI builds it with a layer cache. The release is still 2025-09-07, the
one that wrote the existing data; moving on is a separate change to the
Dockerfile's arguments and the compose tag together.

### On-prem, resilient

The same components with PostgreSQL replication, multiple API/gateway replicas
behind the proxy, and MinIO in distributed mode. Site servers per branch.

### Cloud (commercial)

Control plane on Kubernetes (Helm chart in `deploy/helm/`): API and agent-gateway
as independently scaled deployments, managed PostgreSQL, managed Redis, S3 for
object storage, Keycloak (or a managed OIDC IdP). Customer LANs run site servers
for imaging/caching; endpoints anywhere dial the cloud gateway. Multi-tenancy
carries multiple customer organizations in one control plane, or one control
plane per large customer.

The migration from on-prem to cloud is a deployment and configuration change, not
an application rewrite — because the data model is already tenant-aware, agents
are already outbound-only, and heavy LAN work is already delegated to site
servers.

## Scaling notes

- The **agent gateway is the fan-out point** and is designed to scale
  horizontally and independently of the API. Agent connections are long-lived;
  the gateway is I/O-bound, not CPU-bound. Redis pub/sub fans job dispatch and
  status across gateway replicas.
- The **API** scales as ordinary stateless web replicas.
- **PostgreSQL** is the primary state store; heavy read paths (inventory grids,
  audit search) use indexing and, where needed, read replicas. This platform is
  deliberately relational and control-oriented; high-volume time-series telemetry
  is out of scope (that is the monitoring product).
- **Object storage** absorbs the large artifacts (backups, images, packages),
  keeping the database lean.

## Error handling & resilience principles

- **Agents tolerate disconnection.** Locally scheduled tasks still run offline;
  results and logs are buffered (bounded) and flushed on reconnect. Jobs carry
  timeouts and report structured success/failure.
- **Jobs are idempotent where feasible** and safe to retry; the job engine tracks
  attempt state and does not double-apply where a capability declares idempotency.
- **The control plane degrades gracefully.** If the gateway is down, agents
  reconnect with backoff; queued jobs dispatch on reconnect. If object storage is
  unavailable, artifact-producing jobs fail cleanly rather than partially.
- **Nothing privileged happens silently.** Every dispatched job, credential
  access, and remote session is auditable regardless of outcome.
