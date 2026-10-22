# Module Spec — Site servers

> The LAN-local helper role promised by `docs/01-architecture.md` ("Site
> server"), depended on by `docs/modules/provisioning.md` ("hard dependency —
> PXE, caching, WoL"), referenced as a preferred dispatch target by
> `docs/modules/ipam/spec.md` §5.3 and `docs/modules/provisioning/spec.md` §8,
> named as a seam by `docs/modules/app-deploy/spec.md` §6 and the backup
> section of `deploy/README.md`, and listed as **out of scope** by
> `docs/kernel/spec.md` §1.
>
> **Nothing of it exists.** There is no model, no table, no job type, no
> permission, and no code path. Six modules point at a hole. This spec fills
> the hole with the smallest thing that makes those references true, and pins
> the shape of the rest.
>
> **The load-bearing result: a site server is a `Device`.** Under that choice
> v1 requires **no kernel change at all** — not to `Job.target_type`, not to
> `JobEngine.create`, not to `gateway/dispatch.py::_load_job_for_device`, not
> to the gateway's certificate→device binding, not to the wire protocol. §12
> enumerates that claim item by item so it can be checked rather than trusted.
>
> **One invariant is genuinely in tension and it is not resolved by wording.**
> A PXE server or a package cache must listen on a socket; `CLAUDE.md`
> invariant #2 says "no agent listening port". §0.3 and §4 state the conflict
> plainly, and **v1 does not build a listener** — so v1 needs no amendment.
> The amendment is written out in §4.4 and attached to the specific later
> slice that cannot be built without it, so nobody builds that slice by
> accident.

---

## 0. Resolved design decisions

Ten decisions. Each rules out a family of designs, so they are recorded before
the design rather than discovered inside it. Every claim about existing code
below was checked against the code and the code won where the two disagreed
(§13 lists every place they disagreed).

### 0.1 A site server IS a `Device` with an ordinary agent, plus a registry row

A site server is a managed machine that has been **promoted**: it has a
`devices` row, an `agents` row, an ordinary mTLS client certificate, and one
additional row in a module-owned `site_server` table declaring which segments
it serves and which roles it fills.

It is not a new kind of job target, not a new connection identity, and not a
new certificate profile.

This is settled by reading the kernel as it actually is, not as it might be:

| Kernel fact | File | Consequence for a non-`Device` site server |
|---|---|---|
| `create(..., target_device_id)` hard-sets `target_type="device"` | `app/jobs/engine.py:86` | A second create path, or a parameter that changes what a signature covers |
| `_load_job_for_device` rejects any job whose `target_id != device_id` | `app/gateway/dispatch.py:159` | A second job-loading path, and the property that a device may complete only **its own** job stops being one rule |
| `queued_for_device` filters `Job.target_id == device_id` | `app/jobs/engine.py:240` | A second offline drain |
| Connection identity is cert fingerprint → `Agent` → `(device_id, org_id)` | `app/gateway/main.py:181-192` | A second identity resolution, in the one function where identity must have exactly one answer |
| `ConnectionRegistry` and Redis `dispatch:{device_id}` are keyed by device | `app/gateway/connections.py`, `dispatch.py:48` | A second key space and a second `psubscribe` pattern |
| `jobs.target_type` is `varchar(10)` | `migrations/versions/0001_core_schema.py:153` | `"site_server"` is **eleven characters**. The alternative design starts with a migration that widens a shipped column on the jobs table |

Six forks of core dispatch, to model a machine that is already a machine.
`CLAUDE.md` is explicit that modules "register into the kernel; they do not
fork it", and this is the module most tempted to.

The positive argument is stronger than the avoidance argument. A site server is
a real server. It needs inventory, patching, backup, health checks, and remote
control like any other server. Modelled as a non-`Device` it silently drops out
of every module that manages machines, and the fleet acquires a privileged host
that nothing manages.

**Rejected: a first-class `site_server` entity that is a job target**
(`Job.target_type = "site"`, `target_id` → `site_server.id`). Failure mode
accepted by rejecting it: a site server can never be a thing that has no agent
— a NAS, a switch-attached appliance, a container on someone else's hypervisor
— because every site server must be an enrolled endpoint. If that case ever
arrives it is a genuine re-open of this decision, and §16 records what it would
cost.

**Rejected: a flag or a `DeviceTag`.** `device_tags.value` is a free-text
`varchar(500)`. There is nowhere to put the segments it serves, the roles it
fills, the enable state, or the operator who granted it; nowhere to hang an FK;
nowhere for RLS to be tested; and no way to express "these two rows conflict".
Failure mode accepted by rejecting it: one more table and one more migration.

### 0.2 Promotion is a control-plane act, separate from enrollment

A machine becomes a site server when an operator holding `site.manage` creates
its `site_server` row. It never becomes one by asserting anything at
enrollment.

Enrollment is authenticated by a token that is deliberately broadly
distributable — `docs/05-agent-spec.md` calls it "zero-touch friendly, so it can
be pushed by GPO, MDM, kickstart, or an installer parameter", and
`EnrollmentToken.max_uses` is nullable, meaning unlimited by default. "This host
serves boot images and cached installers to an entire subnet" must not be
claimable by whoever holds such a token.

Consequence, stated as a rule: **the endpoint never declares its own site-server
scope.** The segments it serves are operator-entered in the control plane, and
they reach the endpoint only inside a signed job's params — the same discipline
IPAM applies to `net.sweep` targets ("the agent is never asked to decide what it
may touch", `app/modules/ipam/sweep.py`).

**Rejected: a `site_server` enrollment token kind, or a distinct CA role.**
Failure mode accepted: an operator must take one extra deliberate step after
imaging a branch server. That step is the audit record that says who granted
LAN-wide privilege, which is worth more than the keystroke.

### 0.3 v1 builds nothing that listens, because invariant #2 says nothing may

`CLAUDE.md` invariant #2: "**Agents connect outbound only, over mutual TLS.**
The server never opens an inbound connection to an endpoint. No agent listening
port." `docs/05-agent-spec.md`: "It does not listen for inbound connections or
expose any management port."

`docs/01-architecture.md` promises the same site server will host PXE and a
package cache. **A PXE server that does not listen is not a PXE server.** These
two documents have contradicted each other since day one and no amount of
wording dissolves it.

So v1 ships the two things that need no socket:

- **`site.wake`** — a Wake-on-LAN magic packet. Outbound UDP broadcast, one
  datagram, socket closed. No listener, no port, no residency.
- **the registry + sweeper preference** — pure control-plane state that makes
  IPAM's and provisioning's dangling references real.

Both are fully inside invariant #2 as written. Neither requires a word of
`CLAUDE.md` to change.

The cache (§7) and PXE (§8) are specified in full and **not built**, each gated
on the explicit amendment in §4.4. This follows the precedent the repo already
set twice — `docs/modules/provisioning/spec.md` §8 "pinned, deferred" and
`docs/modules/app-deploy/spec.md` §6 "a seam only".

**Rejected: build the cache in v1 and quietly reinterpret invariant #2 as
being about management ports.** That reinterpretation is probably correct (§4
argues it), but a security invariant that a module reinterprets on its own
authority is not an invariant. Failure mode accepted by rejecting it: the
bandwidth win — the thing that motivates site servers at all — is not in v1.

### 0.4 A cached artefact's integrity never depends on the cache

Whenever the cache is built, this is the property that makes it safe: the
consumer verifies a **sha256 that arrived inside its own signed job envelope**,
and the cache is not on the path that digest travelled. A poisoned cache is a
failed job, never a compromised endpoint.

This is already the shipped model on the consumer side —
`docs/modules/app-deploy/spec.md` §3 and `deploy/README.md`: "The agent verifies
the artifact SHA256 before execution; a mismatch fails the job", and
`AppDeployParams` carries `artifact_sha256` inside the signed params.

Therefore a site-server cache is **content-addressed and untrusted**. It is a
performance component. It is never an authority about what an endpoint should
run.

**Rejected: the cache re-signs, or vouches for, what it serves.** That would put
a signing capability on a LAN box, which is the one thing
`docs/03-security-model.md` says makes the whole design work ("an attacker would
also need the signing key"). Failure mode accepted: content whose digest is not
already in a signed envelope simply cannot be cached (§7.2 lists what that
excludes).

### 0.5 Nothing confidential is ever cached, and the rule is structural

Cacheable content is exactly: package installers, patch payloads, OS image and
build payloads, and agent self-update binaries. Everything else is not
cacheable, and cannot be, because the site server has no path to any other
object.

**Never cacheable**, named individually because each is a real object in this
repo that an eager implementer might reach for:

- rendered provisioning answer files (`{org_id}/provision/answers/{run_id}`) —
  they carry vault-decrypted domain-join and local-admin credentials
  (`docs/modules/provisioning/spec.md` §7). Invariant #3.
- backup chunks and manifests (`{org_id}/backup/…`) — other devices' data.
- remote-control session recordings; ticket, budget, and documentation
  attachments.
- anything from a job's `params`, including the job envelope itself.

**Rejected: "cache any object-storage ref the job names".** It reads as
generality and is a credential-disclosure bug: `answer_ref` is an object
reference in a job-adjacent envelope today. Failure mode accepted: each
cacheable class must be explicitly published to the cache namespace, which is
one more step per artefact type.

### 0.6 A signed job envelope never passes through a site server

Every endpoint dials the gateway itself and receives its own jobs on its own
mTLS connection. There is no relay in v1, and the deferred relay (§16) is pinned
as **opaque TCP forwarding only, never TLS termination**.

The reason is the trust boundary, not fastidiousness. A relay that terminates
TLS is a machine-in-the-middle on the control channel of every endpoint behind
it. It would see every signed envelope in plaintext — including the job-scoped
`repo_key` that backup places inside the envelope (`deploy/README.md`, backup
section) and any credential a job carries for the duration of its use.

**Rejected: a caching or aggregating relay.** Failure mode accepted: an isolated
segment with no route to the control plane stays isolated until the deferred
opaque relay is built.

### 0.7 Site servers slot into IPAM's selection as one ORDER BY term, not a second algorithm

`app/modules/ipam/sweep.py::select_sweeper` already has a complete, tested rule:
designated → deterministic auto-select → `no_agent`, with an advisory capability
filter and a 7-day refusal cooldown. Because a site server is a `Device` (§0.1)
it is **already a row in that candidate set**.

The entire change is one term prepended to the existing `ORDER BY`. The
`EXISTS` inventory-observation predicate, the refusal-cooldown `NOT EXISTS`, the
capability filter, the candidate limit, the `_designated` short-circuit and the
`no_agent` outcome are all untouched (§6).

**Rejected: a `select_site_server()` beside `select_sweeper()`.** Two selection
functions is two answers to "who probed this segment", and they diverge on the
first bug fixed in one of them. Failure mode accepted: none identified — this is
the design paying for itself.

### 0.8 Preference is granted on a claim; candidacy still requires evidence

A site server is preferred for a sweep only when it is **both** declared for the
segment in the registry **and** placed on that segment by a current inventory
observation — that is, the preference reorders the existing candidate set and
never adds to it.

The distinction is the point. A registry row is an operator's *claim*. An
`ipam_observation` is *evidence*. `select_sweeper`'s docstring states the safety
property the evidence protects: "A machine that is not on that wire cannot sweep
it, and dispatching anyway emits somebody's probe from the wrong place."
Preference may rest on a claim. Membership of the set may not.

This deliberately declines to copy an asymmetry in the shipped code:
`sweep.py::_designated` checks that the designated device's agent is online,
advertises the capability, and has not recently refused — but **never checks
that the device is on the subnet**. IPAM's own spec §5.3 step 1 does not state
that check either. It reads as emergent rather than chosen, and §12.3 raises it
to IPAM as a change to shipped behaviour rather than making it here.

**Rejected: "a declared site server is always a candidate".** It is the more
useful rule on day one — it works before inventory has run — and it is the rule
under which a typo'd CIDR sends a probe onto a segment nobody authorized.
Failure mode accepted: a freshly promoted site server does not get sweep
preference until inventory has observed its address, which is one
`inventory.collect` away and is visible on the segment-check view (§11).

### 0.9 `site.wake` reports that it sent, never that anything woke

A magic packet is an unauthenticated, unacknowledged, fire-and-forget L2
broadcast. There is no reply to wait for.

`SiteWakeResult` therefore carries `{sent, broadcast, sent_at}` and no
`woke: bool`, no reachability probe, and no retry-until-up loop. A result field
that cannot be true information is worse than a missing one, and a "did it come
up?" poll is how this module becomes the monitoring product — the same boundary
IPAM defends in its §0.10 and §7. Confirmation that a machine woke is its agent
connecting, or its provisioning run advancing, both of which are already
observed elsewhere.

**Rejected: `site.wake` blocks and reports reachability.** Failure mode
accepted: a caller who wants "wake and confirm" composes two observations
instead of reading one field.

### 0.10 `site.wake` gets a short TTL, because a late wake is a wrong wake

`SiteWakeParams` jobs are created with `ttl_s = 300`, not the kernel's 3600s
default (`app/jobs/engine.py:56`).

The kernel's offline behaviour is correct and, for this job type, hazardous: a
job for an offline device stays `queued` and is drained when that device next
connects (`GatewayServer._drain_queued`). A site server that reconnects at 04:00
would then broadcast a wake nobody wants any more, an hour after the maintenance
window it belonged to. IPAM pinned `SWEEP_TTL_S = 600` for the analogous reason
and this follows it.

**Rejected: the kernel default.** Failure mode accepted: a wake requested while
the site server is briefly offline expires instead of firing late, and the
operator sees `expired` and retries. That is the correct failure.

---

## 1. Purpose & boundary

**Purpose.** Give the platform a LAN-local, control-plane-authenticated helper
so that work which must originate on a local segment can, and so that
bandwidth-heavy work can stay local when the control plane moves to the cloud
(`docs/01-architecture.md`, "Cloud (commercial)").

**In scope, v1 (buildable now):**

- The `site_server` registry: promote / demote / edit, segments, roles, enable.
- The `site.wake` job type — Wake-on-LAN from the correct segment.
- Sweeper preference: one ORDER BY term in IPAM's existing selection (§6).
- A segment-check view: declared segments vs. observed addresses (§11).
- RBAC (`site.read`, `site.manage`, `site.wake`), tenancy, audit.
- The agent capability **contract** for `site.wake` (schemas exported; the Go
  handler that consumes them is agent-side work this module does not write —
  §5.4).

**Pinned and explicitly not built (each with the gate it waits on):**

- LAN artefact cache (§7) — gated on §4.4's invariant amendment **and** on an
  artefact origin-fetch path, which does not exist in this repo (§13.3).
- PXE / TFTP / proxyDHCP (§8) — gated on §4.4 plus its own security review.
- Connection relay (§16) — opaque TCP only, never TLS termination.
- Backup LAN staging (§16) — the hardest of the four; it is not a cache.

**Out of scope, permanently:**

- Any authority over another endpoint. A site server never signs, never
  verifies on another device's behalf, never enqueues, forwards, modifies, or
  reports on another device's job. `_load_job_for_device` enforces the last of
  those today and this module does not weaken it (§12.1).
- Being a second control plane, a cache of *decisions*, or an offline mode.
- Anything the monitoring product owns — reachability polling, latency,
  topology.

---

## 2. Data model

One table in v1. Module-owned, in `backend/app/modules/site_server/models.py`,
created by **migration `0050_site_server.py`** (`down_revision = "0049"`). This
paragraph used to say "head is `0046` as this spec is written; do not assume
`0047`" — four migrations landed from other workstreams between the spec and
the build, which is exactly why it said not to assume.

### 2.1 `site_server`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid7 PK | |
| `org_id` | uuid FK → `organizations.id`, NOT NULL | RLS (§10.1) |
| `device_id` | uuid FK → `devices.id`, **UNIQUE**, NOT NULL | The identity. One row per device |
| `name` | varchar(200) NOT NULL | Display label; defaults to the device hostname at promotion |
| `segments` | `cidr[]` NOT NULL | The LAN segments this server serves |
| `roles` | `text[]` NOT NULL | Subset of `{'wake','cache','pxe'}`; v1 only `'wake'` is honoured — see below |
| `ipam_site_id` | uuid FK → `ipam_site.id`, **nullable** | Optional grouping |
| `enabled` | bool NOT NULL default true | A demote-without-delete |
| `notes` | text NOT NULL default `''` | |
| `created_by` | uuid FK → `users.id`, nullable | |
| `created_at`, `updated_at` | timestamptz | |

Constraints:

- `UNIQUE (device_id)` — a device is a site server at most once. Not
  `(org_id, device_id)`: `devices.id` is already globally unique and the
  narrower key is the honest one (`agents.device_id` does the same).
- `CHECK cardinality(segments) BETWEEN 1 AND 32` — a site server with no
  segment serves nothing and is a configuration mistake, not a valid row; the
  ceiling stops "declare 0.0.0.0/0" arriving as a thousand rows instead.
- `CHECK segments <@ ...` is **not** expressible; validation that each element
  is a network address (host bits zero) is a service-layer rule, matching
  IPAM's §0.3 precedent for rules Postgres cannot state cheaply.
- `CHECK roles <@ ARRAY['wake','cache','pxe']::text[] AND cardinality(roles) >= 1`.

  **"v1 only `'wake'` is honoured" was ambiguous and was resolved at build time
  as REFUSED, not accepted-and-ignored.** `registry.promote` and
  `update_site_server` reject `cache` and `pxe` with a 400 naming §4.4 and the
  fact that the invariant amendment has not been accepted. The reasoning: a
  stored `roles=['cache']` on a machine that binds no socket is a registry row
  asserting that a service is running when none is, and the operator has no way
  to discover the difference — the page renders the lie. Refusing costs one
  later `PATCH` on the day §4.4 is accepted.

  **The DB CHECK stays wider than the service on purpose.** That vocabulary is
  the whole of the forward seam: admitting `cache` and `pxe` at the database
  layer is what stops the cache slice beginning with a migration that widens a
  shipped CHECK on a populated table. The gate is one tuple
  (`registry.SUPPORTED_ROLES`) in one place.
- Index `ix_site_server_org_enabled (org_id, enabled)`; GiST index on
  `segments` deferred until the cache needs containment lookups at volume —
  v1's fleet-sized `&&` scan is a handful of rows.

`cidr[]` native, not text, for the reason IPAM's §0.1 gives: containment
(`<<=`, `&&`) is the operation both this module and the sweeper preference need,
and text comparison of prefixes is wrong in ways that pass small tests.

**`ipam_site_id` is nullable and that is load-bearing.** IPAM is a pull-forward
module (`docs/06-roadmap.md` Phase 15 note) and site servers must work in a
deployment that never installs it. The FK is a convenience for grouping, never a
dependency: nothing in §5 or §6 requires it to be set.

**No `site_server_lease` table in v1.** Leases exist only to bound a listener's
lifetime, and v1 has no listener. The shape is pinned in §7.3 so the cache slice
does not invent a different one.

---

## 3. Enrollment, promotion, and identity

### 3.1 Enrollment is unchanged. Entirely.

`POST /enroll/v1`, `EnrollRequest`, `DeviceFacts`, `EnrollResponse`,
`app/services/enrollment.py::enroll`, and `CAService.sign_csr` are **not
modified**. A site server is enrolled exactly as any Windows/Linux/macOS
endpoint is: token → CSR → device-bound certificate → `Device` + `Agent` rows →
`enrollment.device_enrolled` audit event.

### 3.2 There is no site-server certificate profile

`docs/03-security-model.md` and `docs/modules/provisioning/spec.md` §8 both
imagine one: "a site-server-role certificate issued by the internal CA; scoped
to caching/PXE/relay, never to job command". This spec does not issue one, and
argues the property those documents wanted is **already what an ordinary agent
certificate gives**:

- An agent certificate authorizes exactly one thing at the gateway: hold a
  connection bound to one `(device_id, org_id)`.
- It confers no ability to command another endpoint. Jobs are signed *by the
  server* for a named `device`; the certificate cannot mint one.
- It confers no ability to speak about another endpoint's job:
  `_load_job_for_device` discards and audits any message whose job's
  `target_id` is not this device (`gateway.identity_mismatch`).

So "never to job command" is not a scope that needs to be carved out of a
certificate — it is the default, and enforced in two places already.

Introducing a second profile would mean a second fingerprint→identity
resolution in `GatewayServer._resolve_agent`, which is the one function in the
system where identity must have exactly one answer. §12.4 records this as a
**deliberate amendment** to two existing documents rather than a silent
divergence.

**Failure mode accepted, stated plainly:** the credential on a host that caches
LAN-wide artefacts is the same *kind* of credential as an ordinary endpoint's.
The blast radius of stealing it is that host's own jobs plus the cache contents
(which §0.5 confines to non-confidential, digest-pinned material) — not command
over the segment. Demotion is a row delete plus lease revocation; certificate
revocation is the existing `POST /api/v1/agents/{device_id}/revoke` path, which
already drops the live connection via `publish_revoke`.

### 3.3 Promotion

`POST /api/v1/site-servers` with `{device_id, name?, segments[], roles[],
ipam_site_id?}`, requiring `site.manage`. Validates:

- the device exists **in this org** (explicit `org_id` predicate as well as
  RLS, per IPAM's §11.1 precedent — through `org_session` RLS rejects first, so
  a cross-org test alone passes with the predicate deleted);
- it has an `agents` row (a device with no agent can never receive a job);
- it is not already promoted;
- every segment parses as a network prefix with host bits zero.

Writes `site.promoted` to the audit chain with the device, segments, and roles.

**Promotion does not check that the device is on the segments it claims**, and
does not fail if it is not. It surfaces the mismatch (§11, segment check) and
declines to silently correct an operator. §0.8's rule then makes the unverified
claim harmless: a claim earns preference, evidence earns candidacy.

### 3.4 How the control plane knows which segment a site server serves

`site_server.segments`, and nothing else. Not inference from inventory, not
DHCP scope discovery, not agent self-report.

Inventory NIC observations are used only to *check* the declaration (§11) and
to satisfy IPAM's existing candidacy predicate (§6). The authoritative
declaration is operator-entered, which is what makes it auditable and what
keeps §0.2's rule true.

---

## 4. The listening question (invariant #2)

This section exists because it is the part most likely to be got wrong quietly.

### 4.1 What a site server accepts from the control plane: nothing

There is no inbound path from the control plane to a site server. Not a port,
not a callback, not a webhook, not a management channel.

A site server receives exactly one thing from the control plane — a signed job
envelope — and receives it the way every agent does: down the mutual-TLS
WebSocket **that the site server itself dialled** (`GatewayServer._handle`).
Invariant #2's first clause, "the server never opens an inbound connection to an
endpoint", holds verbatim and is not narrowed by anything in this spec, in any
slice, including the deferred ones.

### 4.2 What v1 listens for on the LAN: nothing

`site.wake` opens a UDP socket, sets `SO_BROADCAST`, sends one datagram, and
closes it. No bind to a service port, no accept loop, nothing resident. Idle
cost is zero, per `docs/05-agent-spec.md`'s budget and `CLAUDE.md`'s
"capabilities stay dormant and only allocate when a job for them actually runs,
then release".

### 4.3 What a cache or PXE server would have to listen for

- **Cache:** HTTP `GET` on one port, serving byte ranges of blobs from a bounded
  local directory, keyed by sha256.
- **PXE:** TFTP (69/udp), proxyDHCP (67/68/4011 udp), and HTTP for the boot
  payload.

All of them read-only, all of them serving content, none of them accepting an
instruction. That is the real distinction: **invariant #2's second clause is
about a control path into a root/SYSTEM process, and a content responder is not
one.** A PXE responder cannot make the site server execute anything; it hands
out bytes whose digest was fixed by a signed job before the listener ever
existed.

### 4.4 The amendment, written out — required before §7 or §8 may be built

That argument is probably right. It is still a narrowing of a stated invariant,
and a module does not get to narrow one on its own authority. So:

> **Proposed replacement for `CLAUDE.md` invariant #2, clause 3.**
>
> Current: "No agent listening port."
>
> Proposed: "No agent accepts instructions from the network. An agent binds no
> listening socket, with one exception: a device promoted to **site server** may
> bind **read-only content responders** (HTTP artefact cache, TFTP/HTTP/proxyDHCP
> boot services) on the LAN segments named in its `site_server` registry row.
> Such a responder (a) may only return bytes whose digest was fixed by a signed
> job, (b) may never accept, forward, modify, or relay an instruction, a job, or
> a credential, (c) binds only while a signed `site.serve` lease is live and
> unbinds when it expires, and (d) binds only to interfaces on the segments
> carried inside that signed lease. Every other endpoint keeps this invariant
> verbatim."

Four properties, each with a build-time check named in §14.

**If that amendment is not accepted, the cache and PXE cannot be built and this
module ends at v1.** That is a legitimate outcome and this spec does not design
around it: the alternative — an endpoint that reaches *out* to the control plane
for every artefact — is exactly what happens today and is precisely the
bandwidth problem site servers exist to solve. There is no third option in which
LAN-local content serving happens without something listening on the LAN.

### 4.5 Why the lease, and not a config flag

If the cache is built, the listener must be leased rather than configured on.
A config flag makes the listener permanent, which turns a promoted device into a
box with a permanently open port whose justification is a database row somebody
set once. A lease makes the socket's existence a **signed, time-bounded,
audited, revocable** fact that matches every other privileged thing in this
platform. §7.3 pins its shape.

---

## 5. Capabilities and job types

### 5.1 Registration

`app/modules/site_server/handler.py` registers job types via
`app.jobs.registry.register`, exactly as `patch/handler.py` and
`ipam/handler.py` do. Registration is called from **both**
`app/modules/__init__.py::register_all` (API + worker processes) **and**
`app/gateway/main.py::register_module_job_types` (the gateway process, where
results actually complete). That second call is not optional: the gateway never
imports `register_all`, and a handler registered only in `register()` is dead in
production while every in-process test passes — the exact hole
`register_module_job_types`' docstring records IPAM falling into.

Params and result models are exported to `docs/kernel/wire-protocol/` by
`scripts/export_wire_schema.py` as `SiteWakeParams.schema.json` and
`SiteWakeResult.schema.json`. **This module writes no Go.** The export is not a
hand-off across a repository boundary any more — the agent lives under `agent/`
here since `CLAUDE.md` commit `a32cead` — but it is still a compatibility
contract rather than an internal detail: a deployed agent is older than the
control plane it talks to, so a change to these schemas is additive, or
versioned, or it breaks agents in the field, and no test here will show you
which (`docs/kernel/wire-protocol/README.md`).

### 5.2 `site.wake` — params (`extra="forbid"`)

```
target_mac:   str            # canonical lowercase colon form, validated server-side
broadcast:    str            # an explicit broadcast ADDRESS, computed server-side
port:         int  7 | 9     # default 9
repeat:       int  1..3      # default 3, ~200ms apart; magic packets are lossy UDP
```

`broadcast` is an **explicit address, never a CIDR and never "the segment"** —
computed by the control plane from the `site_server.segments` entry that
contains the target. The agent broadcasts to exactly that address and to nothing
else. Same principle as IPAM's enumerated `targets`: the endpoint is never asked
to decide what it may touch, and an authorization revoked in the control plane
does not depend on the endpoint re-deriving anything.

**The target MAC is a parameter, not a job target.** The signed `device` field
names the *site server*. The machine being woken frequently has no `devices` row
at all — waking bare metal for provisioning is the primary use — so it cannot be
a job target and must not be modelled as one.

`ttl_s = 300` (§0.10), a module constant `WAKE_TTL_S` rather than a literal, so
the dispatcher and the test cannot drift apart.

### 5.3 `site.wake` — result (`SiteWakeResult`)

```
sent:       bool
broadcast:  str      # must equal params.broadcast
sent_at:    RFC3339
```

No `woke`, no probe, no retry count (§0.9). The result handler rejects a result
whose `broadcast` differs from the signed params — a wake that went somewhere
else is not a successful wake, and the check is cheap.

### 5.4 Idle budget

One UDP socket, up to three datagrams, released. Nothing resident, nothing
scheduled, no timer entry. The capability compiles into the agent under
`agent/internal/capabilities/site/` and is dormant otherwise.

**Unwritten, and unmeasured — two separate facts.** Not a line of that
capability exists. It belongs under `agent/` in this repository — `CLAUDE.md`
commit `a32cead` moved the agent in-tree from the former `it-suite-agent`
repository — which changed *where* the work goes and changed nothing about
whether it is done. Nor is anyone currently obliged to do it:
`docs/modules/agent/plan.md` plans `inventory.collect`, `health.check` and
`net.sweep`, and does not mention `site.wake` at all.

The idle claim above is likewise a **design target, not a measured result** —
the same status `docs/06-roadmap.md` records for `health.check`.
`.github/workflows/ci.yml` gained an `agent` job in commit `0203612` (gofmt,
vet, staticcheck, `go test`, cross-compile), so the Go toolchain is no longer
the thing that is missing. It changes nothing here: a job that compiles and
tests what exists tests nothing for a capability that does not exist, and none
of its five steps is a benchmark. `CLAUDE.md`'s idle-budget section forbids
reporting a footprint number as verified until a benchmark runs **and** a
falsifier is stated, and this section states neither — the falsifiers this
capability would need are a socket or timer-wheel entry resident on an agent
with no wake job in flight, and resident memory that does not return to its
pre-job baseline after one. What the move removed is the *excuse*, not the gap:
"the code is in a repo this CI cannot see" was a true explanation until
2026-08-05, and there is now not even a toolchain left to blame. Do not let a
green build here read as agent-side verification.

### 5.5 What is deliberately *not* a new job type

- **`net.sweep`** — unchanged. Same type, same `NetSweepParams`, same
  `scope_digest`, same authorization path, same 600s TTL, same result handler.
  A site server sweeping a segment is IPAM's existing job dispatched to a
  different device. **This is the payoff of §0.1**: IPAM's contract-only note
  becomes real with zero change to IPAM's wire contract.
- **`provision.prepare`** — when PXE lands, this becomes an ordinary registered
  job type targeting the site server's device, replacing the bespoke
  `ProvisionPrepareEnvelope` (§8.2). That deletes a parallel signing path
  rather than adding one.

---

## 6. Selection and fallback

### 6.1 The change to IPAM's sweeper selection, in full

In `app/modules/ipam/sweep.py::select_sweeper`, one term is added to the
existing ordering — **below liveness, not above it**:

```sql
ORDER BY (a.status = 'online') DESC,
         (EXISTS (SELECT 1 FROM site_server s
                  WHERE s.org_id = :org AND s.device_id = d.id
                    AND s.enabled
                    AND EXISTS (SELECT 1 FROM unnest(s.segments) seg
                                WHERE CAST(:prefix AS cidr) <<= seg))) DESC,
         a.last_seen_at DESC NULLS LAST,
         d.id
```

> **Corrected at build time. This section used to put the preference term
> FIRST, and it contradicted §6.2 two paragraphs down.** §6.2's fallback table
> says an offline site server "loses the preference; an ordinary endpoint
> sweeps", explaining that "`(a.status='online') DESC` is already the second
> term" — which is only true if the preference is *not* first. Both could not
> hold.
>
> With preference first, an **offline** site server outranks an **online**
> ordinary endpoint: the sweep is addressed to a machine that cannot receive it,
> the job sits `queued` until the 600s TTL expires it, and the segment goes
> unswept for the cycle. §6.2's behaviour is what was built, on IPAM's own
> precedent — `_designated`'s docstring says designation "is not liveness", and
> a designated device whose agent is offline falls through to auto-selection
> rather than producing a job addressed to a machine that cannot receive it. An
> *inferred* preference is a weaker claim than an operator's *explicit*
> designation, so it cannot outrank liveness where designation does not.
>
> `backend/tests/test_ipam_site_server_preference.py::test_liveness_outranks_preference`
> is red under the ordering this section originally specified.

That is the entire change. Unchanged: the `EXISTS` inventory-observation
predicate that defines candidacy, the `NOT EXISTS` refusal-cooldown predicate,
`_CANDIDATE_LIMIT`, `_advertises_net_sweep`, the `_designated` short-circuit,
and the `no_agent` outcome. **No second selection function** (§0.7).

`<<=` reads "the subnet being swept is contained in a segment this server
serves", so a site server declared for `10.20.0.0/16` is preferred for
`10.20.7.0/24`. Containment, not equality — a branch server serves a branch, not
one VLAN.

The site server must still clear the candidacy `EXISTS` — an inventory
observation places it on the segment (§0.8). Preference reorders; it never adds.

### 6.2 Fallback: mostly, nothing new happens

| Situation | What happens | Why nothing new is needed |
|---|---|---|
| Site server agent offline | Loses the preference; an ordinary endpoint sweeps | `(a.status='online') DESC` is already the second term |
| Site server refuses `net.sweep` | Ineligible for 7 days, like any device | The existing `REFUSAL_COOLDOWN_DAYS` `NOT EXISTS`, unmodified — no site-server exemption |
| Site server demoted or `enabled=false` | Falls back to an ordinary endpoint on the next dispatch | The preference subquery stops matching |
| Site server declared but never observed on the segment | Not a candidate at all | §0.8; visible on the segment check (§11) |
| No candidate at all | `state='no_agent'`, nothing dispatched | The existing first-class outcome (IPAM §2.10, Gate E) |
| Operator designated a device **and** a site server exists | The designation wins | `_designated` short-circuits before the ordering — an explicit operator choice outranks an inferred preference, and that is correct |

### 6.3 Fallback for `site.wake`

If the site server's agent is offline the job stays `queued` (kernel behaviour)
and would fire on reconnect — which §0.10's 300s TTL converts into `expired`
instead. The caller sees `expired` and retries. There is no automatic
re-selection onto a different site server in v1: a wake must originate on the
target's own segment, and picking a substitute means picking a different segment,
which is not a fallback but a different action. Multi-site-server-per-segment
failover is §16.

### 6.4 Which site server a provisioning wake goes through

The wake route names a site server in its path. **Provisioning's run-start does
not** — `wake_on_lan` carries a MAC and (optionally) a segment, and the control
plane picks the server. That pick is subject to §0.2 exactly as
`_resolve_broadcast` is, and it is one layer ABOVE it: `_resolve_broadcast`
never guesses a wire, but it is only ever asked about the site server the
selection already chose, so choosing wrongly defeats it entirely and does so
**silently** — the caller gets a 202 and the audit log records a dispatched
wake, while the packet lands on a different LAN.

Two rules, and neither guesses:

1. **`segment` absent is a refusal when the org has more than one enabled site
   server.** Absent means no containment filter, so every enabled server
   matches. Defaulting to one of them is defaulting to "whichever row was
   promoted first", which is a fact about an operator's afternoon and not about
   the topology. It is defaulted only when there is nothing to choose between —
   which is `_resolve_broadcast`'s own rule for a single declared segment,
   applied one level up. The refusal happens before the `ProvisionRun` is
   created, so a refused wake leaves no half-provisioned run behind.

2. **`segment` present selects by containment and then by SPECIFICITY.**
   `list_site_servers(segment=...)` returns rows whose declared `cidr[]`
   contains the target, ordered by the masklen of their most specific matching
   segment, descending. Between a server declared for `10.20.0.0/16` and one
   declared for `10.20.7.0/24`, a target on `10.20.7.5` goes through the /24:
   that is the machine actually on the wire, and the /16 merely claims a
   supernet containing it. This is `net.containing_segment`'s "most specific,
   not first" rule applied ACROSS site servers instead of within one, and it is
   the same rule for the same reason.

`created_at, id` survives only as the tie-break between two site servers equally
specific about the same segment. That residual case is §6.3's: v1 prefers
deterministically and does not fail over, because failover needs a health notion
the registry has not got.

**The containment filter and `_resolve_broadcast` use the same notion of
"serves", deliberately.** A site server the filter excludes is one
`_resolve_broadcast` would refuse, so the two layers cannot disagree about which
wires a server is on — only about whether the refusal is loud or the selection
is empty. The one gap that notion cannot close is rule 1's, which is why rule 1
is a refusal and not a heuristic.

---

## 7. LAN artefact cache — pinned, NOT built

Gated on §4.4's amendment **and** on §7.4's prerequisite.

### 7.1 Shape

The site server holds a bounded, content-addressed directory:
`<cache_root>/<sha256[:2]>/<sha256>`. It serves `GET /c/<sha256>` over HTTP on
the leased port, on the leased segments only. It is filled lazily — first
request misses, the site server fetches from origin exactly as an endpoint
would, verifies the digest, stores, serves. It evicts LRU under a leased byte
ceiling.

No index, no listing, no search, no write endpoint, no delete endpoint. A
request for a digest not in the cache is a `404`, never a proxy fetch initiated
on the requester's behalf.

### 7.2 How a consumer learns a cache exists

Through the only channel that exists: **its own signed job envelope**. Each
artefact-carrying job type gains one optional field alongside the
`artifact_ref` / `artifact_sha256` it already has:

```
cache_endpoints: list[str]   # 0..4 base URLs, populated server-side at dispatch
```

The endpoint tries each in order, falls back to origin, and **verifies
`artifact_sha256` from the signed envelope either way** (§0.4).

This is per-module work, not a kernel change: `AppDeployParams`,
`PatchApplyParams`, `BackupRunParams` and the provisioning image params each own
their own params model. The site-server module exposes one helper —
`cache_endpoints_for(session, org_id, device_id) -> list[str]` — and the cache
slice wires **exactly one consumer** (app-deploy) as proof. The rest adopt it as
their own separately reviewed work.

`cache_endpoints` is advisory and its absence is normal. An endpoint with no
cache behaves exactly as it does today.

### 7.3 The `site.serve` lease (pinned shape)

A new job type, targeting the site server's device:

```
lease_id:     UUID
roles:        ["cache"]              # later: ["cache","pxe"]
segments:     list[str]              # explicit CIDRs, server-computed
bind_port:    int                    # cache HTTP port
max_bytes:    int                    # cache ceiling
expires_at:   RFC3339                # the socket's lifetime, inside the signature
```

Signed like every job. The agent binds only on the interfaces whose addresses
fall inside `segments`, only until `expires_at`, and unbinds unconditionally at
expiry — including if the control plane is unreachable. Renewal is a fresh
signed lease, never an extension of an old one, for the same reason the kernel
re-signs on retry with a fresh nonce.

Persisted as `site_server_lease (id, org_id, site_server_id, roles, segments,
bind_port, max_bytes, issued_at, expires_at, revoked_at, job_id)`, `org_id` RLS
like everything else. Revocation is a `site.serve` lease with `expires_at` in
the past, plus the audit event.

### 7.4 The prerequisite that is not the invariant

**There is no agent-facing artefact origin-fetch path in this repository.**
`ObjectStore` has no presigned-URL method; there is no download route (only
`POST /api/v1/deploy/packages/{id}/artifact` for operator upload); and
`AppDeployParams` carries a ref with no way for an agent to resolve it (§13.3).

So the cache is a cache in front of nothing. Building it first would mean
inventing the origin path inside this module, which belongs to whoever owns
artefact distribution. **The cache slice is blocked on that path existing**, and
that is a bigger prerequisite than the invariant amendment.

---

## 8. PXE and imaging — pinned, NOT built

Gated on §4.4 plus its own security review. PXE is the highest-consequence
listener on any network: an unauthorized responder redirects boots fleet-wide.

### 8.1 Additional constraints when it lands

- **One PXE responder per segment**, enforced as a service-layer rule under an
  advisory lock at promotion and at lease issue (IPAM §0.3's precedent for rules
  that arrays make awkward to state as constraints). Two proxyDHCP responders on
  one segment is a genuine conflict, not a preference.
- Leased identically to §7.3, with `roles: ["pxe"]`.
- Boot payload digests come from `os_image.artifact_sha256`, which already
  exists and is already integrity-checked.

### 8.2 `provision.prepare` becomes a real job type

Today `ProvisionPrepareEnvelope` is a bespoke shape, signed at
`app/modules/provisioning/dispatch.py:131` — and **the signature is discarded**:
the `await signer.sign(...)` result is not assigned, not persisted, and never
published, because there is no consumer (§13.2).

Under §0.1 it stops being bespoke: `provision.prepare` registers as an ordinary
job type with a `ProvisionPrepareParams` model, created through
`JobEngine.create(target_device_id=<site server's device>)`, signed and
dispatched by the kernel like everything else. That is a **deletion** of a
parallel signing path. `wake_on_lan` moves out of it entirely and becomes a
`site.wake` job (§13.5).

---

## 9. Trust: what a site server may and may not see

Restating §0.4–§0.6 as a single answer, because "what does it see" is the
question a security reviewer will ask first.

**Invariant #1 is untouched.** A site server runs only jobs signed by the server
and verified by its own agent against the public key it pinned at enrollment. It
holds no signing key, signs nothing, and verifies nothing on another device's
behalf. There is no site-server-specific signing path, and after §8.2 there is
one fewer signing path than today.

**It never sees another device's signed envelope.** No relay in v1; opaque TCP
only if ever (§0.6). Every endpoint's jobs travel on that endpoint's own mTLS
connection.

**It never sees anything an endpoint's own agent would not already have.** A
cached installer is the same bytes that endpoint was going to fetch anyway, and
the digest that decides whether they are acceptable travelled in the endpoint's
own signed job, not through the cache.

**It cannot see anything confidential**, because nothing confidential is
cacheable and it has no path to any other object (§0.5).

**It does see one thing an ordinary endpoint does not, and this is the residual
risk: LAN fetch patterns.** A compromised site server can observe that host X
requested digest Y at time T, and digests are correlatable with named patches
and packages. Mitigations, stated as v1 rules for the cache slice: the cache
emits **no per-request telemetry to the control plane**, keeps no request log
beyond serving, and the audited artefact is the **lease**, not the requests. The
residual is accepted and named: a compromised site server learns roughly what
its segment is installing. It does not learn what any of it contains beyond what
the segment itself already holds, and it cannot change any of it (§0.4).

---

## 10. Tenancy, RBAC, audit

### 10.1 Tenancy (invariant #5)

`site_server` (and later `site_server_lease`) carry `org_id`, get
`ENABLE ROW LEVEL SECURITY` **and** `FORCE ROW LEVEL SECURITY` with
`USING (org_id = current_setting('app.current_org')::uuid)` and the same
`WITH CHECK`, per `docs/kernel/spec.md` §5. The migration's RLS loop follows the
table list, and the kernel's `pg_policies` completeness test covers the new
table automatically.

Service functions carry an explicit `org_id` predicate **as well as** relying on
RLS, and the tests pin it on the admin engine — through `org_session` RLS
rejects first, so a cross-org test alone passes with the predicate deleted
(IPAM §11.1's finding, reused).

A site server serves only its own org. Cross-org caching is structurally
impossible: cacheable objects live under `{org_id}/…` prefixes like every other
object in this platform, and the cache namespace inherits that.

### 10.2 RBAC (invariant #6)

Module code `site`. Package `app/modules/site_server/`; routes
`/api/v1/site-servers`; frontend `frontend/src/modules/site-servers/`.

| Code | Description |
|---|---|
| `site.read` | View site servers, their segments, roles, and segment checks |
| `site.manage` | Promote, demote, edit segments/roles/enable; issue and revoke leases |
| `site.wake` | Send a Wake-on-LAN packet via a site server |

`site.manage` and `site.wake` are **deliberately separate**, for the reason IPAM
separates `ipam.sweep.run` from `ipam.scope.manage`: waking a machine inside an
existing configuration is routine technician work; deciding that a host may
serve an entire subnet is not. Collapsing them lets anyone who can wake a
machine also decide what a site server is.

Role grants (mirroring every other module's seed, so
`test_rbac.py::test_org_admin_has_all_permissions` keeps holding):

| Role | Grants |
|---|---|
| super-admin, org-admin | all three |
| technician | `site.read`, `site.wake` |
| auditor, read-only | `site.read` |

Enforced with `require_permission(...)` on every route. Job dispatch is checked
server-side at the service boundary, never in the frontend.

### 10.3 Audit (invariant #4)

| Action | Actor | Target | Metadata |
|---|---|---|---|
| `site.promoted` | user | `device` | site_server_id, segments, roles |
| `site.updated` | user | `site_server` | changed fields, before/after segments |
| `site.demoted` | user | `site_server` | device_id, reason |
| `site.wake_dispatched` | user | `site_server` | job_id, target_mac, broadcast, segment |
| `site.serve_leased` *(cache slice)* | user | `site_server` | lease_id, roles, segments, bind_port, expires_at |
| `site.serve_revoked` *(cache slice)* | user \| system | `site_server` | lease_id, reason |

Plus the kernel's own `jobs.dispatched` / `jobs.succeeded` / `jobs.failed` /
`jobs.refused`, which already fire for every `site.wake`.

**No per-cache-request audit event**, for the reason `_handle_health_report`
gives for health reports: volume would swamp a hash-chained log whose value is
that a human can read it. The lease is the audited fact (§9).

---

## 11. API

All under `/api/v1/site-servers`. Request models `extra="forbid"`.

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/` | `site.read` | List; filter by `enabled`, `ipam_site_id`, `segment` (containment) |
| POST | `/` | `site.manage` | Promote (§3.3) |
| GET | `/{id}` | `site.read` | Detail, including agent status and last seen |
| PATCH | `/{id}` | `site.manage` | Segments, roles, name, notes, enabled |
| DELETE | `/{id}` | `site.manage` | Demote; revokes any live lease |
| GET | `/{id}/segment-check` | `site.read` | Declared segments vs. current `ipam_observation` addresses for this device: `matched`, `declared_unobserved`, `observed_undeclared` |
| POST | `/{id}/wake` | `site.wake` | `{target_mac, segment?}` → creates and returns a `site.wake` job |

`POST /{id}/wake` resolves `broadcast` server-side: it picks the
`site_server.segments` entry containing `segment` (or the sole entry), computes
the broadcast address, and puts **that** in the signed params. A MAC with no
containing segment is a `400`, not a guess.

Provisioning calls the same service function (`wake_via_site_server`), which is
what finally makes `RunStartRequest.wake_on_lan` do something (§13.5).

The envelope is returned to the caller and **published to Redis only after the
transaction commits** — never inside it — the shape every `*_sweep_all_orgs` and
`create_sweep` already uses, so a job can never reach an agent describing state
that rolled back.

`GET /{id}/segment-check` degrades cleanly where IPAM is not installed: no
`ipam_observation` table means an empty evidence set and an explicit
`"evidence": "unavailable"`, not a 500.

**Frontend** (`frontend/src/modules/site-servers/`): a list with segment badges
and agent status, a promote dialog picking from devices, a detail pane with the
segment check and a wake action. TanStack Query, TypeScript strict, no `any`.

---

## 12. Kernel and cross-module changes required

The brief's central question. Answered as a checklist rather than a claim.

### 12.1 Kernel changes required for v1: none

| Kernel surface | Change | Verified against |
|---|---|---|
| `Job.target_type` | **None.** Stays `"device"` | `app/jobs/engine.py:86`; column is `varchar(10)`, and `"site_server"` would not fit anyway |
| `JobEngine.create` | **None.** Called with `target_device_id` = the site server's device | `app/jobs/engine.py:64` |
| `JobEngine._payload` / signing | **None.** `device` is the site server's device id | `app/jobs/engine.py:100` |
| `gateway/dispatch.py::_load_job_for_device` | **None, and deliberately.** A site server can complete only its **own** jobs | `dispatch.py:150` |
| `GatewayServer._handle` / `_resolve_agent` | **None.** Identity stays cert → `Agent` → `(device_id, org_id)` | `gateway/main.py:163-192` |
| `ConnectionRegistry`, `dispatch:{device_id}` | **None** | `gateway/connections.py`, `dispatch.py:48` |
| Wire protocol | **None.** No new server→agent or agent→server kind | `gateway/protocol.py` |
| Job-type registry | **None.** `site.wake` registers through the existing extension point | `app/jobs/registry.py` |
| Enrollment / CA | **None** (§3.1, §3.2) | `app/services/enrollment.py`, `app/core/ca.py` |
| Audit / RBAC / tenancy | **None.** New codes and actions, existing machinery | `core/rbac.py`, `core/audit.py`, `core/tenancy.py` |

~~If a builder finds themselves editing any file in `app/jobs/`, `app/gateway/`,
or `app/core/` to make this module work, the design has drifted from §0.1 and
that is the signal to stop.~~

> **Restated at build time, because that sentence is false on day one and this
> document contradicts it two sections earlier.** §5.1 *requires* editing
> `app/gateway/main.py::register_module_job_types`, and the repo's shipped
> permission pattern requires adding three codes to
> `app/core/rbac.py::PERMISSIONS`. Both are under the named packages. A guard
> built on the literal sentence would be permanently red, and a permanently red
> guard gets deleted or `xfail`ed — which is worse than no guard, because it
> reads as protection.
>
> The precise claim, and the one that is actually asserted:
>
> **v1 appends to SEVEN registration points and changes no kernel logic.**
> Every one is an existing extension point that every module in this repo
> appends to. **No kernel file gains a *branch*** — no `site_server` case in
> dispatch, no second identity resolution, no second job-loading path, no
> widened column.
>
> The seven:
>
> 1. `app/core/rbac.py::PERMISSIONS` — the three permission codes.
> 2. `app/models/__init__.py` — the `SiteServer` import.
> 3. `app/modules/__init__.py::register_all` — the API process's registration.
> 4. `app/gateway/main.py::register_module_job_types` — the gateway process's.
> 5. `app/worker.py::register_worker_job_types` — the worker process's.
> 6. `app/jobs/direct_dispatch.py` **and** `app/jobs/dispatch_permissions.py`.
> 7. `app/core/approved_actions.py`.
>
> **The plan said FOUR, and the full backend suite found the other three.**
> `plan.md` Task 5 named only the first four; it did not know that this repo
> maintains **three deny-by-default partitions over the live job registry**,
> each asserting that every registered job type has been classified so that a
> module registering a new one breaks a test rather than picking a side by
> accident. `site.wake` had to be classified in all three:
>
> * **`direct_dispatch.py` → module-governed.** Through the kernel route an
>   operator holding `jobs.dispatch` could supply an arbitrary `broadcast`
>   address in `params` — a datagram on any wire in reach of any promoted
>   device, with no registry row consulted, no `site.wake_dispatched` audit
>   event, and the kernel's 3600s TTL instead of 300s. Deny-by-default already
>   refused it; what was missing was the reason an operator is shown.
> * **`dispatch_permissions.py` → `site.wake`.** Which permission the module's
>   own route requires, so the AI confirm gate can ask whether the confirming
>   human could have run it by hand.
> * **`approved_actions.py` → NOT approvable, and `_DESTRUCTIVE_JOB_TYPES`,**
>   for the identical reason `net.sweep` is: it emits packets at addresses that
>   are **not the target device**, so what it costs is not bounded by the machine
>   the job is signed for, and `max_target_scope='device'` cannot bound it. A
>   frozen `params_template` would also carry a broadcast address nobody
>   re-derived from a current declaration — a segment demoted or edited
>   afterwards would not change it.
>
> **§5.1's "not registered in the worker process" is also overturned.**
> `plan.md` Task 3 declined worker registration on the grounds that no worker
> path creates a `site.wake`. That is still true and no longer the deciding
> fact: all three partition tests use `register_worker_job_types()` as their
> definition of "the live registry", so a job type absent there cannot be named
> in their maps — while the process-global registry means an earlier test
> importing the module makes it appear anyway, so the partition would hold or
> fail depending on test ordering. That is the exact silence those tests'
> docstrings say they exist to break.
>
> `backend/tests/test_site_server_kernel_boundary.py` asserts that four ways:
> an exact `{kernel file: mention count}` equality; a walk of every `If`/`IfExp`/
> `Match`/`BoolOp` in the three trees for a branch naming this module;
> `jobs.target_type` still `varchar(10)` against `len("site_server") == 11`;
> and an exact `{module file: forbidden imports}` equality. Docstrings and
> comments are stripped via the AST first — as a raw-text grep the guard would
> go red on the paragraph explaining why it is green.
>
> **One allowlist entry the plan got wrong.** `app/modules/site_server/api.py`
> imports `publish_job` from `app.gateway.dispatch`. The plan called that "the
> shape of a second egress path" and forbade it; it is in fact what `backup`,
> `deploy`, `health`, `ipam` and `patch` api.py all do, and what §11 of this
> document requires ("published to Redis only after the transaction commits").
> Publishing is the delivery half of the one egress path, and only the route
> knows when the transaction committed. It is allowlisted and **narrowed**: a
> separate assertion pins the imported names as exactly `{"publish_job"}`.

### 12.2 The one invariant change, and it is not for v1

`CLAUDE.md` invariant #2, wording in §4.4. **Required only for §7 (cache) and §8
(PXE); not required for v1.** Not to be adopted as a side effect of building
v1's registry or `site.wake`.

### 12.3 Changes requested in other modules (each theirs to accept)

1. **IPAM — the ORDER BY term** (§6.1). Additive, one clause. IPAM's spec §5.3
   and `plan.md` both already say the preference order was written so a site
   server "slots in at position 0"; this is that.
2. **IPAM — `_designated` does not check the subnet** (§0.8). Raised, not
   fixed here. Adding `AND EXISTS (observation inside prefix)` to `_designated`
   would align designation with auto-selection and with the docstring's own
   safety argument. It is a **behaviour change to shipped code** — a designation
   that works today would start falling through to auto-selection — so it is
   IPAM's call, and this module works either way.
3. **App-deploy — `cache_endpoints`** (§7.2). Cache slice only.
4. **Provisioning — `provision.prepare` as a registered job type**, and
   `wake_on_lan` moving to `site.wake` (§8.2, §13.5). PXE slice only, except the
   `wake_on_lan` fix, which v1 can deliver.

### 12.4 Documentation amendments required (deliberate, per `CLAUDE.md`)

| Document | Amendment |
|---|---|
| `docs/kernel/spec.md` §1 | Site servers move out of "out of scope"; annotate that the kernel needed no change to support them |
| `docs/03-security-model.md` trust boundary | "Site servers (mTLS, scoped)" → a site server is an ordinary agent; the scoping is the registry plus the signed lease, **not** a second certificate profile (§3.2) |
| `docs/modules/provisioning/spec.md` §8 | Same correction; plus `provision.prepare` becomes a job type, and the current signed-then-discarded envelope is retired (§8.2) |
| `docs/modules/ipam/spec.md` §5.3 | Replace the "no site-server entity exists" note with the ORDER BY term |
| `docs/01-architecture.md` | Site server section: add that it is a promoted `Device`, and that relay is opaque-TCP-only |
| `deploy/README.md` | New site-server section; correct the cache seam to name its two prerequisites (§7.4) |
| `CLAUDE.md` invariant #2 | **Only** with the cache/PXE slice (§4.4) |

---

## 13. Where the code contradicted the documents

Checked, not assumed. In each case the code is what is true today.

**13.1 `agents.capabilities` is written by nothing, and there is no wire path
for it to be written.** `sweep.py::_advertises_net_sweep` documents the column
as one "the agent writes at enrollment". It does not: `DeviceFacts`
(`app/services/enrollment.py:32`) has exactly five fields — hostname, os_family,
os_version, architecture, serial — and no capability field; `EnrollRequest` adds
nothing; and no agent→server message kind carries capabilities
(`gateway/protocol.py`). Nothing anywhere in `backend/app` writes the column.
IPAM's tolerance of an empty map is therefore not a graceful fallback for an
unusual case — it is the **only** path, always. **Consequence for this spec:
nothing here depends on capability advertisement.** Refusal (a real
`JobStatus`, with IPAM's 7-day cooldown) is the working mechanism, and `site.wake`
inherits it unchanged.

**13.2 The `provision.prepare` signature is computed and thrown away.**
`app/modules/provisioning/dispatch.py:131`: `await signer.sign(canonical_bytes(
envelope.model_dump()))` — the result is not assigned, not stored on the run,
not published. The discipline is real (no unsigned path is constructed) but
nothing consumes the output. §8.2 replaces it with a kernel-signed job rather
than preserving it.

**13.3 There is no agent-facing artefact fetch path at all.** `ObjectStore`
(`app/core/storage.py`) exposes `put_*`/`get_*` for server-side use and **no
presigned-URL method**; the only artefact route is `POST /api/v1/deploy/packages/
{id}/artifact` (operator upload). `deploy/README.md` and
`docs/modules/app-deploy/spec.md` §6 both describe agents fetching `artifact_ref`
from object storage and preferring a site-server cache when present — **neither
half exists**. This is why §7.4 names the origin path as the cache's real
prerequisite, ahead of the invariant.

**13.4 `AppDeployParams` has no cache field.** Its eight fields
(`app/modules/deploy/schema.py:27`) carry `artifact_ref` and `artifact_sha256`
and nothing about a cache — so "the agent prefers a site-server cache when
present" has, today, no server-provided way for the agent to learn that one is
present. §7.2 is what would supply it.

**13.5 Provisioning's `wake_on_lan` is silently dropped.**
`RunStartRequest.wake_on_lan` is accepted (`provisioning/schema.py:121`) and
never reaches `start_provision_run`; `dispatch.py:126` hard-codes
`wake_on_lan={}`. An operator can request a wake today and nothing happens, with
no error. `site.wake` plus §11's `wake_via_site_server` is the fix, and it is
deliverable in v1.

**13.6 `jobs.target_type` is `varchar(10)`.** `"site_server"` is eleven
characters (`migrations/versions/0001_core_schema.py:153`). Not a
contradiction so much as a tripwire: the rejected design in §0.1 begins with a
migration widening a shipped column on the jobs table, which is a good early
signal that it was the wrong design.

**13.7 `Job.target_type` claims `device | group` and is only ever `"device"`.**
`JobEngine.create` hard-sets it; group targeting is done by modules expanding to
per-device jobs (`app/modules/deploy/dispatch.py:105`, which reads its **own**
`deployment.target_type`, not the job's). The `"group"` value in the model
comment is aspirational. This spec adds no third value.

**13.8 This module's own handler denied a registration `app/worker.py`
performs.** `site_server/handler.py`'s docstring said `site.wake` is NOT
registered in the worker process. `app/worker.py::register_worker_job_types`
registers it and carries a comment explaining why it must: that function is what
three deny-by-default partitions over the job registry use as their definition
of "the live registry", so a type absent there cannot be classified in them —
and because the registry is process-global, whether it *looks* absent depends on
which tests imported which modules first. Two files, one fact, in contradiction,
and nothing could notice because **neither sentence was load-bearing on anything
that ran**. Corrected, and the three registrations (API, gateway, worker) are now
one test each. This entry is in the same list as the seven above for a reason:
those were contradictions between the code and documents written before it, and
this one appeared inside a single module in a single cycle. Prose in this
repository is treated as a claim owed a falsifier — §14 above, and `CLAUDE.md`'s
"measured, not asserted".

---

## 14. Testing requirements

Test-first, against real PostgreSQL 16, per `docs/kernel/spec.md` §14.

**Structural — the §0.1 claim, made checkable:**

- A test asserts `JobEngine.create` is still called with `target_device_id` and
  that no `site_server` code path sets `Job.target_type` — mirroring
  `test_ipam_sweep_dispatch.py`'s AST-level structural assertions.
- A test asserts no file under `app/modules/site_server/` imports from
  `app.gateway` or `app.jobs.engine` except the single dispatch module, and that
  the module adds no file to `app/jobs/`, `app/gateway/`, or `app/core/`.

**Tenancy:** two orgs; org A cannot read, promote, edit, or wake through org B's
site server, including via raw SQL with the org predicate removed. The kernel's
`pg_policies` completeness test must pass with the new table.

**RBAC:** each route 403s without its permission; `site.wake` alone cannot
promote; `site.manage` alone can promote; seeded roles grant exactly §10.2.

**Promotion:** a device from another org is a 404 (never a 403 — it must not
confirm existence); a device with no `agents` row is refused; double promotion
is refused; a segment with host bits set is refused; every path audits.

**`site.wake` dispatch:** `broadcast` is server-computed and equals the
containing segment's broadcast address; a MAC with no containing segment is a
400 and creates no job; `ttl_s == WAKE_TTL_S`; the envelope is published only
after commit; the result handler rejects a result whose `broadcast` differs
from the signed params.

**Provisioning's site-server selection (§6.4) — AND ITS FIXTURE IS PART OF THE
REQUIREMENT.** The org must hold **several** site servers, and the correct one
must be **last** by creation order, by id and by name. This is not fussiness: the
first version of these tests seeded at most one site server and never passed
`segment`, so `list_site_servers(segment=None)` and
`list_site_servers(segment=<anything>)` returned the same one-element list and
`candidates[0]`, `candidates[-1]` and any other pick were the same value.
Deleting the containment filter was a **no-op mutation** and every assertion
still passed. A fixture that makes the guarded and unguarded paths produce the
same observable value is not a test of the guard.

Assert the state of the world — `(provision runs, site.wake jobs, signed
broadcast, target device)` — not that an exception was or was not raised: the
defect this covers raises nothing. Each of the containment filter, the
specificity ordering and the `enabled` filter must, when removed, name a
DIFFERENT wrong site server.

**Wake status in the console:** the 202 from `POST /wake` describes a signed job,
not a datagram, and the UI must not say otherwise. `queued`, `dispatched` and a
reported result are three different sentences, and an absent or unrecognised job
status renders as `queued` — never as "sent".

**Selection (the §6 change, in IPAM's own suite):** with an eligible site server
on the segment it is chosen; with it offline an ordinary endpoint is chosen;
with it refused inside the cooldown it is skipped like any device; with it
declared but unobserved on the segment it is **not** a candidate (§0.8); a
`_designated` device still wins over a site server; `no_agent` still occurs when
nothing is eligible. Determinism: the same fixture picks the same device across
repeated runs.

**Signing boundary:** a `site.wake` job's signature covers the site server's
device id, and a result arriving from a *different* device for that job is
discarded and audited as `gateway.identity_mismatch` — i.e. §12.1's
"`_load_job_for_device` unchanged" is asserted, not assumed.

**Reserved for the cache/PXE slice** (written when §4.4 is accepted, not
before): the listener binds only within a live lease; unbinds at `expires_at`
even with the control plane unreachable; binds only on interfaces inside the
leased segments; serves only digests present in the cache and never proxies;
a corrupted cache entry produces a *failed job*, not an executed artefact.

---

## 15. Migration notes

- **One migration**, `0050_site_server.py`, `down_revision = "0049"`. This bullet
  used to say "head is `0046` ... do not hard-code `0047`"; four migrations
  landed from other workstreams in between.
- Creates `site_server`, its constraints and indexes, `ENABLE` + `FORCE` RLS
  with the org policy, and seeds the three permissions plus role grants in the
  same migration (the pattern `0043_ipam.py` uses).
- The `permissions` table is `(id, code, module, action)` — no `resource`, no
  `description`. Descriptions live only in `app/core/rbac.py::PERMISSIONS`.
- `downgrade` drops role grants, then permissions where `module = 'site'`, then
  the table.
- Model declarations and the migration must agree, since `test_schema_drift.py`
  deparses both sides.
- Migrations are forward-only. The cache slice's `site_server_lease` is a later
  migration, never an edit to this one.

---

## 16. Deferred (explicitly not in v1)

| Deferred | Gate / reason |
|---|---|
| LAN artefact cache | §4.4 amendment **and** an artefact origin-fetch path (§7.4) |
| PXE / TFTP / proxyDHCP | §4.4 amendment plus its own security review; one responder per segment |
| Connection relay | Opaque TCP forwarding only — **never TLS termination** (§0.6). Would need its own spec |
| Backup LAN staging | Not a cache: it would hold other devices' data. Needs encryption-at-source and a per-tenant story before it is even a design |
| Agent self-update from a site server | Rides the cache slice; the binary is already digest-and-code-signature verified |
| Multiple site servers per segment, with failover | v1 prefers deterministically and does not fail over (§6.3). Needs a health notion the registry does not have |
| A site server with no agent (appliance, NAS) | Re-opens §0.1. Cost is honest: a second job-target type, a `varchar(10)` widening, a second dispatch channel and a second identity resolution — i.e. everything §0.1 avoided |
| GiST index on `segments` | Until containment lookups are hot |

### 16.1 The seam that exists, and what is deliberately NOT a seam

Recorded at build time so the deferred slices attach later without rework, and
so nobody builds a placeholder that reads as one.

**What is built, and is the seam:**

- **the `roles` vocabulary.** The DB CHECK already admits `cache` and `pxe`, so
  the cache slice needs no migration to widen a shipped constraint on a
  populated table. The service refuses them until §4.4 is accepted, and that
  refusal is a single tuple — `registry.SUPPORTED_ROLES` — with the gate named
  in its own error message.
- **the registry row itself.** `site_server` is what a `site_server_lease` will
  hang a foreign key on.
- **the spec.** §7.3 pins the lease's shape, §7.2 pins `cache_endpoints`, §4.4
  pins the amendment's four testable properties.

**What is NOT built, and must not be:** no `site_server_lease` table, no
`site.serve` job type, no `cache_endpoints` field on any params model, and
**no `cache_endpoints_for()` helper returning `[]`**.

That last one is the trap worth naming. An empty-list helper looks like a seam
and is a fixture-blindness trap wearing a design pattern: every consumer test
would pass against a function that never ran, `[]` being what a correct-but-idle
implementation and a never-called one both produce — and on the day the cache
lands, nobody can tell which call sites were ever exercised. §14's "reserved for
the cache/PXE slice" tests are likewise **not** written in this cycle: a test
that cannot fail is worse than a missing one.

**`CLAUDE.md` invariant #2 was NOT amended**, and adopting it as a side effect
of shipping a registry and a UDP broadcast is exactly what §0.3 forbids. v1
needs no amendment because v1 binds no socket.

---

## 17. Related documents

- `CLAUDE.md` — the six invariants; #1, #2, #4, #5, #6 are all load-bearing here
- `docs/01-architecture.md` — the site-server role as originally promised
- `docs/03-security-model.md` — trust boundaries; the signed-job invariant
- `docs/05-agent-spec.md` — agent contract, capability model, idle budget
- `docs/kernel/spec.md` — §1 (scope), §5 (RLS), §7 (enrollment), §8 (gateway),
  §9 (job engine), §10 (audit)
- `docs/modules/ipam/spec.md` §5.3 and `app/modules/ipam/sweep.py` — the
  selection rule this module extends by one clause
- `docs/modules/provisioning/spec.md` §8 — the pinned site-server contract this
  spec supersedes
- `docs/modules/app-deploy/spec.md` §6, `deploy/README.md` — the cache seam
- `docs/06-roadmap.md` — Phase 7, the IPAM note, the cloud-deployment line, and
  the cross-cutting "site-server role matures incrementally" workstream
