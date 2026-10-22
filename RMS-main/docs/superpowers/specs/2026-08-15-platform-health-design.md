# Platform health and status

**Date:** 2026-08-15
**Scope:** sub-project 2 of 4 in the platform observability program
**Status:** design, approved for planning

---

## 0. What this is, and what it is not

`GET /healthz` today is four lines in `app/main.py`:

```python
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
```

It probes nothing. It returns `ok` with Postgres down, Redis down, the vault
sealed and the object store unreachable. Anything reading it — a load
balancer, a deploy gate, an operator — is being told the platform is healthy
by a function that cannot know.

This sub-project replaces it with probes that measure, a readiness signal that
means something, and an operator page that shows the result.

### 0.1 THIS IS NOT `app/modules/health/`

`app/modules/health/` is **endpoint health**: device monitoring, thirty-one
routes, alert profiles, remediation. It is about the fleet.

This is about **the control plane itself** — is Postgres reachable, is the
worker alive, is the vault sealed. The two share a word and nothing else.

The collision is resolved by never putting platform health in `modules/`:

| concern | lives at |
|---|---|
| probe registry and implementations | `app/core/health/` |
| operator-facing detail | `app/api/v1/platform.py` → `/api/v1/platform/status` |
| machine-facing probes | `/livez`, `/readyz` (root, unversioned) |
| the UI | `frontend/src/modules/platform-status/` |

Platform health is **kernel infrastructure, not a business module**. It does
not register through `modules.register_all`, because it is the thing that
reports on the kernel every module sits on.

### 0.2 This is not the audit log, and not the logs

Probe results are operational and disposable, like the logs and unlike
`app/core/audit.py`. Nothing here is evidence. A dependency being down is not
an auditable event.

---

## 1. Scope, decided

Four decisions were taken before design; they bound everything below.

1. **Infrastructure probes plus a module registry.** The shared dependencies
   are probed for real. The twenty-seven modules are *listed* from the live
   router — name, route count, job-type count — and **not individually
   probed**. A module-level health contributor is a later extension point,
   not this cycle's work. Nothing on the page claims a module is healthy;
   the page claims the module is *registered*, which is what it measures.
2. **A named hard set gates readiness.** Liveness means the process answers.
   Readiness fails only for dependencies the platform cannot serve without.
3. **Current state plus 48h of history**, sampled by the worker.
4. **The samples table carries no `org_id`**, with RLS enabled deny-all plus
   an explicit grant.

---

## 2. The probes

Each probe returns a `ProbeResult`:

```python
@dataclass(frozen=True)
class ProbeResult:
    name: str            # "postgres.app", "redis", ...
    status: Status       # ok | degraded | down
    duration_ms: int
    reason: ReasonCode | None   # STATIC enum member, never free text
    detail: dict[str, str | int] | None  # measured scalars only
```

| probe | what it does | hard? |
|---|---|---|
| `postgres.app` | `SELECT 1` on `app.state.app_engine` | **yes** |
| `redis` | `PING` on `app.state.redis` | **yes** |
| `postgres.admin` | `SELECT 1` on `app.state.admin_engine` | no |
| `objectstore` | bucket-exists on a **cached** `ObjectStore` | no |
| `vault` | seal/init status | no |
| `jwks` | cached key count and age, **no fetch** | no |
| `signing` | signer key material loadable | no |
| `ca` | CA material loadable | no |

### 2.1 Three probes constrained by how their subjects already work

- **`jwks` must never trigger a fetch.** `JwksCache` is deliberately hardened
  against unauthenticated refetch-amplification — single-flight lock, minimum
  refetch interval, failed refetch never clears the key set. A probe that
  fetched would hand an attacker the amplification vector the cache was built
  to deny, through an endpoint reachable without a token. The probe reads
  the cached key count and the age of the last refetch. It reports `degraded`
  when the cache is empty, never `down`, because an empty cache is
  indistinguishable from "no token has been validated yet".
- **`objectstore` must reuse one instance.** `ObjectStore.__init__` ensures
  the bucket exists, which is S3 I/O; `app/main.py` builds it lazily
  (`app.state.object_store = None`) precisely so the API process does not do
  storage I/O at every startup. The probe builds it at most once per process
  and caches it. A probe that constructed per request would turn a page
  refresh into a bucket-ensure storm.
- **`vault` needs a method that does not exist yet.** `VaultService`
  (`app/core/vault.py`) is a Protocol of `store` / `checkout` / `delete` /
  `seal_bytes` / `open_bytes` — every one of which moves secret material. The
  probe must not call any of them. Add `async def health(self) -> ProbeResult`
  to the Protocol: `OpenBaoVaultService` implements it via `GET /v1/sys/health`
  (initialised and seal state, no secret material, no token scope beyond what
  the service already holds); the in-memory implementation returns `ok`.
  **A sealed vault is `down`, not an error** — it is a normal operational
  state with a specific remedy, and reporting it as an exception would lose
  that.

### 2.2 Timeouts and the total budget

Every probe runs under a hard 2s timeout and the whole set under a 5s budget,
concurrently. A status endpoint that can hang is a status endpoint that takes
the page down with the dependency it is describing. Timeout is a result
(`down`, reason `TIMEOUT`), not an exception.

Results are cached for 5 seconds. Without it, a dashboard left open on a wall
screen becomes a `SELECT 1` generator against a database that may already be
struggling.

### 2.3 Reason codes are static. This is a security requirement.

`reason` is an enum member — `UNREACHABLE`, `TIMEOUT`, `AUTH_FAILED`,
`SEALED`, `EMPTY_CACHE`, `MISCONFIGURED`. **The exception's text never appears
in the response, in the samples table, or in the page.**

This is not stylistic. Sub-project 1 spent four fix waves on exactly this
failure: `str(exc)` on a connection error carries the DSN, and a DSN carries a
password. A health endpoint is the worst possible place to relearn it, because
`/livez` and `/readyz` are unauthenticated by necessity (§5) and a connection
failure is the single most likely thing they will ever report.

The exception is not discarded — it is logged through the kernel from
sub-project 1, where `exc_info` yields `exc_type` alone and the correlation
fields say which request asked.

---

## 3. Liveness, readiness, and the hard set

- **`GET /livez`** — the process can answer. No dependency is consulted. It
  returns 200 for a process that is running and 503 never; if the process
  cannot serve this, it cannot serve the response either, which is the signal.
  This is what a container restart policy should watch.
- **`GET /readyz`** — 200 when every **hard** probe is `ok`, else 503 with the
  failing names (names only, no reasons — see §5). This is what a load
  balancer should watch.

**The hard set is `postgres.app` and `redis`, and nothing else.**

Postgres because no request is servable without it. Redis because it is the
job engine's result bus: without it the API would accept job dispatches whose
results can never be delivered — accepting work it cannot complete, which is
worse than refusing it.

Everything else is `degraded` on the page and invisible to `/readyz`. A MinIO
outage costs backup artifacts and remote recordings; taking the whole control
plane out of rotation for it would remove the operator's ability to diagnose
the outage through the very API they need.

**The hard set is a list in code with a test asserting its exact membership.**
Adding to it changes what takes the platform offline, and that should require
deciding, not editing.

---

## 4. The gateway and the worker: heartbeats

The API cannot probe the other two processes. The gateway is a mutual-TLS
WebSocket server with no HTTP surface; the worker is a Celery consumer. Both
are reachable only by things the API must not do.

So each **writes a heartbeat key to Redis** on an interval:

```
platform:heartbeat:gateway:<instance>  -> {"ts": <epoch>, "version": "<image tag>"}
platform:heartbeat:worker:<instance>   -> {"ts": <epoch>, "version": "<image tag>"}
platform:heartbeat:api:<instance>      -> {"ts": <epoch>, "version": "<image tag>"}
```

The status endpoint reports the **age** of each. Over 3× the interval is
`degraded`; over 10× is `down`.

**The API heartbeats too, and keys are per-instance.** The first draft of this
section had the API excluded, on the reasoning that a process answering the
status request is self-evidently alive. That is true only of the *one* replica
that answered. Behind traefik with several API containers, a dead replica is
invisible to a request the live one served — the page would show `api: ok`
while a third of requests fail. Per-instance keys make the count visible:
the page reports how many instances of each process are heartbeating, and a
drop from three to two is a fact rather than a silence. Keys carry a TTL of
10× the interval so a permanently retired instance disappears instead of
accumulating as a phantom `down`.

Two properties this buys:

- Worker-down becomes a *stale heartbeat*, not silence. An absent signal is
  invisible; an old timestamp is not.
- The same mechanism reports the sampler's own liveness (§5), so "the history
  stopped" and "the worker died" are the same fact rather than two.

The heartbeat carries no credential and no host detail — a timestamp and the
deployed version, which §7 of sub-project 4 will need anyway.

---

## 5. Exposure and RBAC

| endpoint | auth | content |
|---|---|---|
| `/livez` | none | `{"status":"ok"}` |
| `/readyz` | none | status plus **failing probe names only** |
| `/api/v1/platform/status` | `platform.status.read` | full detail, history, module registry |

The probes must be unauthenticated: Docker healthchecks and traefik cannot
present a token, and a probe that needs the auth stack to work cannot report
that the auth stack is broken.

**But detail is not free to give away.** `/readyz` returns names
(`["postgres.app"]`), never reasons, never versions, never hostnames — enough
for a load balancer to act, not enough for an unauthenticated caller to map
the internal topology or learn which backend is currently weak.

`platform.status.read` is a new code in `app/core/rbac.py`'s registry,
alongside `devices.read` and `audit.read`. It is **not** tenant-scoped: the
data has no tenant (§6), so authorisation is by permission alone. The route is
gated server-side like every other privileged route, and is covered by the
existing whole-router gating assertion.

---

## 6. Storage, and the first table without a tenant

```sql
CREATE TABLE platform_health_sample (
    id           uuid PRIMARY KEY,
    probe        text        NOT NULL,
    status       text        NOT NULL,
    duration_ms  integer     NOT NULL,
    reason       text        NULL,      -- reason CODE, never text
    sampled_at   timestamptz NOT NULL
);
CREATE INDEX ON platform_health_sample (probe, sampled_at DESC);
```

**No `org_id`, deliberately.** Postgres being unreachable is not any
organisation's fact. A tenant column here would be a fiction, and every read
would carry a filter that means nothing.

Invariant #5 says all data access is tenant-scoped and enforced by RLS. This
table is the first exception in the codebase, so it is handled explicitly
rather than quietly:

- **RLS is ENABLED** with a deny-all default and a single policy granting the
  application role. The table is not an RLS-shaped hole; it is a table whose
  policy is "the application, and nobody else".
- **A test asserts this is the only table without a tenant column.** The
  guarantee that matters is not "this table is fine" but "no second one
  appears without somebody deciding". A schema sweep that names the permitted
  exception makes the next one fail loudly.

### 6.1 Sampling and retention

- A Celery beat task samples **every 60 seconds**, matching
  `sweep-expired-jobs`' cadence, and writes one row per probe.
- The page reads **48 hours** and computes per-probe uptime.
- A daily sweep deletes rows older than 48 hours.

Sampling from the worker rather than on request gives even spacing and one
writer. Request-driven sampling would produce dense samples while somebody
watches and none overnight — precisely inverted from what a history is for —
and several API processes would each write their own view of the same instant.

---

## 7. The page

`frontend/src/modules/platform-status/`, routed at `/platform-status`, gated
on `platform.status.read`, reachable from the app shell (the reachability
test in `App.routing.test.tsx` will require it — a module folder nothing
imports has shipped here before).

Three sections: the dependency grid (status, duration, reason code, 48h
uptime), the process row (api/gateway/worker with heartbeat ages and
versions), and the module registry (name, routes, job types) — labelled
**registered, not healthy**, because that is what it measures.

---

## 8. Testing requirements

1. `/livez` returns 200 with every dependency stubbed down.
2. `/readyz` returns 503 when `postgres.app` fails; 503 when `redis` fails.
3. `/readyz` returns **200** when only `objectstore` fails — the degraded-not-
   down property, and the one most likely to be broken by a well-meaning edit.
4. `/readyz`'s body contains failing names and **no** reason codes.
5. A probe raising an exception whose `str()` is a DSN produces `down` with a
   reason code, and the DSN appears **nowhere** in the response body.
6. A probe that hangs is cut off at its timeout and reports `TIMEOUT`.
7. The hard set has exactly the asserted membership.
8. `platform.status.read` gates the detail route (via the existing sweep).
9. The JWKS probe does not call the network — assert the cache's refetch
   counter is unchanged across a probe.
10. `platform_health_sample` is the only table without a tenant column.
11. RLS is enabled on it, and a non-application role cannot read it.
12. Heartbeat age thresholds: fresh → ok, 3× → degraded, 10× → down.
13. The retention sweep deletes beyond 48h and leaves the rest.
14. Two API instances heartbeating, then one stops: the page reports one
    healthy instance and one lost, **not** `api: ok` — the multi-replica
    blind spot §4 exists to close.

---

## 9. Exclusions

- **Per-module health checks.** Modules are listed, not probed (§1).
- **Alerting.** A page that shows red is not a system that tells anyone.
  Notification belongs to sub-project 3.
- **Synthetic transactions.** No signed-job round trip, no enrollment
  round trip. Considered and deferred: it performs privileged actions on a
  timer, which needs its own security review.
- **Dependency version reporting** beyond the deployed image tag.
- **Tracing.** Out of scope for the whole program.

---

## 10. Falsifiers

What observation would show this design broken:

1. `/readyz` returns 200 while the platform cannot serve a request.
2. `/readyz` returns 503 while every hard dependency is fine — a false
   negative pulls a healthy control plane out of rotation.
3. Any credential-bearing string appears in a status response. The specific
   shape to watch is a connection error's text reaching `reason` or `detail`.
4. The status page hangs or times out while a dependency is down. The page
   exists for exactly that moment.
5. An operator refreshing the page measurably loads Postgres or S3 — the
   cache or the cached `ObjectStore` has regressed.
6. A second table appears without a tenant column and without a decision.
