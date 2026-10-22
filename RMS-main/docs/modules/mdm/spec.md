# Module Spec — Mobile Device Management (Apple MDM + Android EMM)

> **Status: DECIDED, NOT BUILT.** `docs/00-vision.md` records the scope
> reversal of 2026-08-16: the platform's former non-goal — "not an MDM in the
> Apple/Google sense" — is reversed, and the operator has asked for **true MDM
> including phones and tablets**.
>
> **Nothing of this module exists.** There is no table, no migration, no route,
> no permission code, no frontend folder, no vendor credential, no test. Every
> "does", "returns", and "refuses" below is a specification of what must be
> built, not a report of what runs. Where this document describes existing
> code, it names the file and was checked against it.
>
> **The load-bearing question is settled first, in §1, and it is not settled by
> wording.** Apple MDM and Android EMM command devices that will never run this
> platform's Go agent. `CLAUDE.md` invariant 1 — "Every job the agent executes
> must be cryptographically signed by the server and verified by the agent
> before execution. No unsigned command path exists, ever" — is *literally*
> satisfied by a device that runs no agent and executes no jobs, and that
> literal reading is exactly the kind of lawyering an invariant exists to
> prevent. §1 states the conflict plainly, proposes an **additive** amendment
> in exact wording (§1.4), enumerates which of invariant 1's actual security
> properties survive the second path and which do not (§1.5), and lists what
> must never become possible (§1.6). **If the amendment is not accepted, this
> module cannot be built** — §1.9 says so and does not design around it.
>
> **Second-order honesty warning.** This spec was written by a model, from this
> repository's documents, without access to Apple's or Google's current
> developer documentation. Every external protocol fact below is marked and
> collected in **§16**. A spec that confidently states a wrong APNs requirement
> is worse than one that flags it, so nothing in §2–§13 may be implemented from
> this document alone; §16 must be closed against primary sources first.

---

## 0. What this document settles, and what it does not

**Settles (decisions, argued, with rejected alternatives):** the invariant-1
question (§1); whether an MDM device is a `Device` (§2.1); whether an MDM
command is a `Job` (§2.2); the two-CA separation that keeps the paths apart
(§2.3); the data model, tenancy, RBAC, audit, API and frontend shape (§10–§13);
the gate on remote wipe (§8); what is deferred (§17).

**Does not settle, and must not be guessed:** the exact wire details of Apple's
MDM check-in and command protocol, Apple Business Manager / ADE token
mechanics, APNs certificate renewal constraints, Declarative Device Management's
current necessity, Android Management API's exact capability surface, and every
version threshold. These are **§16**, and each carries the consequence of
getting it wrong.

**Requires an operator decision, not an engineering one:** the `CLAUDE.md`
amendment (§1.4); accepting Google as a party in the command path for Android
(§2.7); making the control plane internet-reachable (§3.7); the privacy posture
on device location / Lost Mode (§17); and who owns the APNs certificate Apple ID
(§4.2).

---

## 1. The invariant question

### 1.1 What invariant 1 actually protects

`docs/03-security-model.md` §"The signed-job invariant" states the property in
one sentence, and the property is not "there is a signature":

> "it means transport compromise, a stolen gateway session, or even write access
> to the application database is **not sufficient** to execute code on the fleet
> — the attacker would also need the signing key."

Three separable guarantees are bundled in that sentence:

- **G1 — Origin authenticity at the point of execution.** The *endpoint itself*
  refuses to act on anything not signed by a key it pinned at enrollment. The
  control plane is not trusted by the endpoint; it is verified by it.
- **G2 — Database write access is insufficient.** An attacker who can write
  `jobs` rows cannot cause execution, because a row without a valid signature is
  rejected downstream.
- **G3 — A bounded, audited, authorized dispatch.** Every command that leaves
  the control plane was RBAC-checked, tenant-scoped, nonce-bearing,
  expiry-bearing and written to the hash chain.

An amendment that preserves G2 and G3 but silently drops G1 is a weakening. An
amendment that says "G1 does not apply here" without saying what replaces it is
a hole. §1.5 does the accounting.

### 1.2 Why the literal reading is satisfied, and why that is not enough

Invariant 1 is scoped to "every job **the agent** executes". An iPhone runs no
agent, executes no `Job`, and holds no pinned job-signing public key. Nothing in
this module causes an agent to execute an unsigned job. On a strict reading, the
invariant is untouched.

That reading is correct and insufficient, for two reasons:

1. **The invariant's purpose is not agent-shaped, it is fleet-shaped.** The
   security model opens by describing the platform as "domain-admin-equivalent
   command-and-control over an entire fleet". An iPhone that this platform can
   erase is part of that fleet. A second command path that reaches fleet devices
   under no stated guarantee is precisely the thing the invariant exists to make
   impossible, whether or not the word "job" appears in it.
2. **Silence is how a hole gets built.** `docs/modules/site-server/spec.md` §0.3
   already refused the equivalent move — "a security invariant that a module
   reinterprets on its own authority is not an invariant" — and that was for a
   read-only content responder. This is a command path that can erase a phone.

**Conclusion: this needs an amendment, not an interpretation.** The amendment
below *adds* a clause governing the second path; it does not touch, narrow, or
qualify the existing invariant 1 by one word.

### 1.3 The resolution, in one paragraph

MDM commands are a **separate command path** with its **own** stated
guarantees, structurally isolated from the signed-job path. What makes it
equivalent-enough rather than a hole is not that it has "a signature somewhere"
— Apple's protocol has its own signing and trust model and we do not get to
impose ours on it. It is these five things together, each of which is checkable:
**(a)** the path carries no bytes this platform authored that the device will
execute — configuration and store-mediated app references only (§6.3), so
compromise of this path does not yield arbitrary code as root on the fleet;
**(b)** the two paths cannot reach each other's devices, enforced by two
separate certificate authorities and two separate target-resolution rules
(§2.3, §2.4); **(c)** every command is signed before dispatch and verified by
the dispatcher before transmission, so G2 survives (§1.7); **(d)** every command
is RBAC-checked, tenant-scoped and written to the same hash-chained audit log as
job dispatch, so G3 survives (§11); **(e)** the destructive subset (lock, erase)
is gated far more heavily than any signed job is (§8). **G1 does not survive,
and §1.5 says so rather than pretending otherwise.**

### 1.4 The exact proposed wording (operator decision)

> **Proposed addition to `CLAUDE.md`, "The security invariants", as invariant
> 1b. Invariant 1 is unchanged — not one word.**
>
> **1b. A device managed over a third-party management protocol (Apple MDM,
> Android EMM) is commanded over that protocol's trust model, never over the
> signed-job engine, and the two paths never meet.** Concretely, and all of the
> following are required:
>
> a. **No platform-authored executable payload.** The MDM path may carry
>    declarative configuration and references to applications signed and
>    distributed by the platform vendor's own store. It may not carry an
>    installer, a package, a script, a binary, or any payload authored by this
>    platform that the device will execute. Software installation on a device
>    that also runs the agent stays on the signed-job path.
> b. **Disjoint identities, disjoint trust anchors.** MDM device identity
>    certificates are issued by a dedicated intermediate CA that the agent
>    gateway does not trust, and agent certificates are issued by an
>    intermediate that the MDM ingress does not trust. An MDM command may target
>    only a device carrying an `mdm_device` enrollment row; a signed job may
>    target only a device carrying an `agents` row.
> c. **Device identity is proven, never asserted.** Enrollment issues a
>    per-device identity credential through a single-use, time-bounded,
>    per-device challenge. No static shared secret may authorize the issuance of
>    a device identity.
> d. **Commands are signed before dispatch and verified before transmission.**
>    Every MDM command is signed over its canonical bytes with the platform's
>    job-signing key and re-verified by the dispatcher immediately before it is
>    handed to the vendor transport. A command row whose signature does not
>    verify is never transmitted.
> e. **Same authorization and same audit chain.** Every MDM command is
>    RBAC-checked server-side and tenant-scoped, and its dispatch, refusal, and
>    terminal state are written to the immutable hash-chained audit log,
>    identically to job dispatch.
> f. **The destructive subset is gated separately.** Remote lock and remote
>    erase require their own permission codes, cannot be expressed against a
>    group or a filter, and are never reachable by any automated or AI-driven
>    actor.
>
> **What this clause deliberately does not claim:** the device does not verify
> the platform's signature (a). The endpoint-side guarantee of invariant 1 —
> that a compromised control plane cannot make an endpoint act — **does not hold
> on this path**, and no wording makes it hold. It is replaced by (a): a path
> that cannot deliver code has a materially smaller worst case than one that
> can.

*A second, weaker amendment is also possible and is **not** recommended:
rewriting invariant 1 to read "every command this platform issues to an endpoint
is signed, and verified by the endpoint where the endpoint's protocol permits
verification". That sentence is true and says nothing — "where the protocol
permits" is a hole that any future integration can widen. Recorded here so the
operator can reject it deliberately rather than have it proposed later as a
simplification.*

### 1.5 Property accounting: what survives, what does not

| Guarantee (§1.1) | Signed-job path | MDM path | What compensates |
|---|---|---|---|
| **G1 — endpoint refuses unsigned commands** | Holds. Agent verifies Ed25519 against a key pinned at enrollment (`docs/kernel/spec.md` §9). | **Does not hold.** The device verifies our TLS identity and the vendor's own trust chain; it has no notion of our signing key and cannot be given one. | (a) of §1.4: the path carries no code we authored. The worst case of a fully compromised MDM path is *configuration change plus lock/erase*, not *arbitrary code as root*. Lock/erase are gated by §8. This is a genuine reduction in guarantee and is stated as one. |
| **G2 — DB write access is insufficient** | Holds. | **Holds, by §1.7.** A forged or mutated `mdm_command` row fails dispatcher verification and is never transmitted. | — |
| **G3 — bounded, audited, authorized dispatch** | Holds. | **Holds** (§8, §11). | — |
| **Replay protection** | Nonce + expiry in the signed payload; agent rejects replays. | Partial. Our command records carry a nonce and expiry that *we* enforce; the device does not check them. A captured, replayed vendor-transport request is defended only by the transport. | Command records are single-use server-side and transition to a terminal state; the same command id cannot be re-dispatched without a new signature. |
| **Transport compromise is insufficient** | Holds — a MITM sees signed envelopes it cannot forge. | **Does not hold.** An attacker holding the MDM ingress TLS private key, the APNs push credential, and network reachability to devices can command them without the job-signing key. | Both live in the vault (invariant 3); the ingress terminates TLS in its own process (§2.8); pinning of the server's anchor in the enrollment profile is required **if Apple supports it** — **[VERIFY §16.5]**. This residual is real and is not eliminated. |
| **Cross-tenant isolation** | Cert → `Agent` → `(device_id, org_id)`. | Cert → `mdm_device_identity` → `(mdm_device_id, org_id)`. Structurally identical (§11.1). | — |

**The one-sentence summary an operator should carry away:** on the MDM path we
keep "nobody can command the fleet without authorization and an audit record",
we keep "database access alone is not enough", and we lose "the endpoint itself
will refuse a command we did not sign" — and we buy that loss down by making the
path incapable of delivering code.

### 1.6 What must never become possible

Six prohibitions. Each is a structural property with a build-time or test-time
check, listed again with falsifiers in §14.

1. **An MDM command must never reach a device through the agent.** No MDM code
   path may create a `Job`, publish to `dispatch:{device_id}`, or import from
   `app.gateway` or `app.jobs.engine`.
2. **A signed job must never be created for a device that cannot verify it.**
   A `Job` whose target device has no `agents` row is not a queued job, it is a
   command that will sit forever pretending to be pending (§15.2).
3. **An MDM device identity must never be accepted by the agent gateway, and an
   agent certificate must never be accepted by the MDM ingress.** Two CAs, two
   trust bundles, and a test that crosses them (§2.3).
4. **An MDM profile or command must never carry an executable payload authored
   by this platform** (§6.3). This is the clause that makes the whole trade
   defensible; if it is ever relaxed, §1.5's compensation evaporates and the
   amendment must be re-argued.
5. **A wipe must never be expressible against a group, a filter, a saved
   search, or "all"** (§8.3). The API surface must have no way to say it.
6. **No automated actor — scheduler, remediation engine, AI persona — may ever
   dispatch an MDM command.** Today this is free: the AI act path can only name
   job types drawn from `app.jobs.registry.registered_names()`
   (`app/core/approved_actions.py`), and no `mdm.*` string is or may be
   registered there. That freeness must be *pinned by a test*, not left as a
   happy accident of the current design.

   > **Amended by CLAUDE.md invariant 1c (2026-10-09):** once MDM is built, an
   > AI connected over the platform's MCP server acts as the person who connected
   > it and may send an MDM command, lock and erase included, within that
   > person's RBAC and 1b(f)'s bounds (no group or filter target). This item now
   > governs the scheduler, the remediation engine and the in-app AI only.

### 1.7 Command-manifest signing — how G2 is recovered

Every `mdm_command` row is signed at creation with the **same** Ed25519
job-signing key, over canonical bytes (sorted keys, compact separators, UTF-8 —
`app/jobs/canonical.py`) of:

```
{id, org_id, mdm_device_id, type, params, issued_at, expires_at, nonce}
```

The dispatcher — the only component that talks to the vendor transport —
re-verifies that signature against the signing **public** key immediately before
transmission, and refuses to transmit on failure, writing
`mdm.command_refused` with reason `signature_invalid`.

**What this buys:** an attacker with write access to the application database
cannot inject a command. They can insert an `mdm_command` row; it will never be
sent. G2 holds.

**What this does not buy, stated so nobody claims otherwise:** the device does
not check this signature and never will. Code execution inside the API or
dispatcher process reaches the signing service and can sign anything — but that
is equally true of the signed-job path, so it is not a new weakness. An attacker
with DB write can still *delete* a queued wipe or *cancel* one; integrity of the
queue's contents is protected, availability of the queue is not.

**Rejected: skip command signing because "the device ignores it anyway".** It
would cost one function call and would surrender G2, which is the guarantee the
security model's own summary sentence leads with.

### 1.8 Rejected alternatives to the whole approach

- **Rejected: run the Go agent on iOS/Android instead.** iOS does not permit a
  persistent background daemon with the privileges required, and an Android app
  with device-owner rights *is* an EMM DPC — i.e. this is not an alternative to
  MDM, it is a harder way to build one (see §17, "custom DPC"). Failure mode
  accepted by rejecting it: mobile devices are managed under weaker guarantees
  than laptops. That is the trade the operator asked for.
- **Rejected: model MDM commands as `Job` rows so "everything is signed".**
  This is the dangerous one, because it looks like more rigour and is less. A
  `jobs` row means "an agent verified this before executing it". Putting rows
  there that no agent will ever verify makes the single most security-relevant
  table in the platform ambiguous, and every existing test, report and audit
  reading of it becomes wrong. §2.2 develops this.
- **Rejected: a "trusted MDM bridge" service that holds vendor credentials and
  is commanded by signed jobs.** It adds a component, does not change any
  guarantee (the bridge still cannot make the phone verify anything), and
  creates a machine that holds the APNs key and takes instructions. Failure mode
  accepted by rejecting it: the control plane holds the vendor credentials
  directly, in the vault, like every other credential.
- **Rejected: defer MDM until Apple offers a mechanism to pin a third-party
  signing key.** No such mechanism is known to exist and waiting for one is
  indistinguishable from not building the module.

### 1.9 If the amendment is not accepted

Then this module is not built. There is no third option in which a phone is
managed without a command path that the phone's operating system, not this
platform, defines. The honest fallback positions, in descending order of value:

1. **Enrollment-and-inventory only** — enroll devices, read their reported
   state, assign a small set of configuration profiles, and *never* implement
   lock, erase, app assignment or any state-changing command. This still
   requires (b), (c), (d), (e) of §1.4 and still needs a clause, just a smaller
   one.
2. **Nothing.** `docs/00-vision.md` reverts its reversal, and mobile devices
   remain unmanaged. This is a legitimate outcome; it is the status quo.

---

## 2. Resolved design decisions

Eleven decisions. Each rules out a family of designs, and each was checked
against the code rather than assumed. Where a claim is about Apple's or Google's
protocol rather than this repository, it is marked **[VERIFY §16.n]**.

### 2.1 An MDM-managed device IS a `Device` row — plus an `mdm_device` row, and never an `agents` row

An enrolled iPhone gets an ordinary `devices` row. It does not get an `agents`
row, ever. Its MDM-side identity lives in a module-owned `mdm_device` table with
a unique FK to `devices.id`.

This mirrors `docs/modules/site-server/spec.md` §0.1 ("a site server IS a
`Device`") and for the same positive reason: a phone is a managed asset. It
belongs in the fleet list, in device groups, in RBAC device-group scoping, in
tickets (`docs/modules/service-desk/spec.md` §7.3 links tickets to devices), in
reporting, and in the audit log's `target_type="device"`. Modelled as a
first-class non-`Device` entity it silently drops out of every one of those and
the org acquires a population of managed hardware that the fleet console cannot
see.

**What makes this safe rather than sloppy** is that "is it agent-managed?" has
exactly one answer — the existence of an `agents` row — and that answer is
already how the gateway resolves identity (`app/gateway/main.py`, cert
fingerprint → `Agent` → `(device_id, org_id)`). Adding `devices` rows with no
`agents` row does not widen that resolution by one line.

**Rejected: a separate `mobile_device` entity outside `devices`.** Failure mode
accepted by rejecting it: `devices.os_family` must admit values that agent
modules do not understand (§15.1), and `GET /api/v1/devices` returns rows with
no agent status, which the frontend must render correctly rather than as
"offline" (§14, falsifier 12). Both are small and both are testable. The
alternative's failure modes — a second device concept, a second grouping
mechanism, a second RBAC scope, a second everything — are neither.

### 2.2 An MDM command is NOT a `Job`, and this is structural

`mdm_command` is its own table with its own lifecycle and its own dispatcher.
No MDM code path calls `JobEngine.create`.

The reasoning is about what the `jobs` table *means*. Today, a row in `jobs`
carries an implication that the platform's security model, its audit reports,
and its SOC 2 evidence all rely on: **an agent verified this signature against a
pinned key before acting.** A row that no agent will ever verify makes that
implication conditional, and nothing in the schema records the condition.
Everything downstream — `jobs.dispatched` audit events, patch-compliance
evidence, the AI act path's `approved_action` registry, the job-detail UI —
silently starts describing two different things with one word.

The mechanical consequences of the alternative are also bad, and they are
checkable facts about this repository:

| Kernel fact | File | Consequence for `Job`-modelled MDM commands |
|---|---|---|
| `create()` hard-sets `target_type="device"` and signs `{id, type, device, params, issued_at, expires_at, nonce}` | `app/jobs/engine.py` | The signed payload's `device` names a machine that will never see the signature |
| Dispatch publishes to Redis `dispatch:{device_id}`; offline devices keep jobs `queued` and drain on agent connect | `app/gateway/dispatch.py`, `engine.py::queued_for_device` | Every MDM command sits `queued` forever — the drain never fires, because no agent ever connects |
| `_load_job_for_device` discards any envelope whose `target_id` is not the connected device | `app/gateway/dispatch.py` | Untouched, and useless here — there is no connection |
| The Celery expiry sweep marks past-`expires_at` jobs `expired` | `app/worker.py` | Every MDM command eventually reports `expired` regardless of whether the phone did the thing |
| `APPROVABLE_JOB_TYPES` partitions **every registered job type** into approvable or explicitly refused, asserted by `tests/test_approved_actions_destructive.py` | `app/core/approved_actions.py` | Registering `mdm.erase_device` as a job type forces a decision about whether a machine may fire it. The right answer is "the question must not be expressible" |

**Rejected: `Job.target_type = "mdm_device"`.** Beyond everything above,
`jobs.target_type` is `varchar(10)` in `0001_core_schema.py` — the alternative
design opens with a migration widening a shipped column on the jobs table, which
is exactly the tell `docs/kernel/spec.md` §1 already installed a test for.

**What is deliberately reused:** the canonical-bytes serializer
(`app/jobs/canonical.py`), the `SigningService`, the audit service, the tenant
session, and the vault. This module forks none of them.

### 2.3 Two certificate authorities, and the trust bundles are disjoint

This is the single most important structural control in the module.

- Agent client certificates: issued as today by `CAService` under the existing
  PKI role. The agent gateway's TLS context loads **only** the agent CA bundle.
- MDM device identity certificates: issued under a **separate intermediate**
  (`mdm-device` role). The MDM ingress' TLS context loads **only** that bundle.

Consequences, and they are the point:

- An MDM device identity certificate presented to `wss://gateway:8443/agent`
  fails TLS client-certificate verification *before* any application code runs.
  Even if it somehow reached the application, `_resolve_agent` would find no
  `agents` row and reject.
- An agent certificate presented to the MDM ingress fails the same way.
- A compromise of the MDM identity CA does not mint anything the agent gateway
  accepts, and vice versa.

**Rejected: one CA, separated by certificate attributes (OU, EKU, a SAN
convention).** `docs/03-security-model.md` already refused a second certificate
*profile* on one resolution path because "identity must have exactly one
answer". The same logic applies in reverse here: two answers are fine as long as
they are asked at two different doors. Attribute-based separation puts both
answers behind one door and makes the separation a string comparison somebody
can get wrong. Failure mode accepted by rejecting attribute separation: one more
PKI role and one more bundle to configure.

### 2.4 Target resolution is by enrollment row, never by device id

An MDM command's target is resolved as `mdm_device` → `device_id`, never
`devices` → "is it a phone?". A request naming a `device_id` with no
`mdm_device` row is a **404**, not a 400 and not a 403 — the same treatment
site-server promotion gives a foreign device, so the API does not confirm
existence.

Symmetrically, §15.2 proposes that `JobEngine.create` refuse a target device
with no `agents` row. Together these two rules are prohibition 1 and 2 of §1.6,
enforced at the only two places a target is chosen.

### 2.5 Dual management is legal on macOS, and bounded

A Mac may legitimately have **both** an `agents` row and an `mdm_device` row.
This is the desirable configuration: MDM delivers what only MDM can (TCC/PPPC
consent profiles — `docs/modules/agent/open-questions.md` already notes the
agent needs them, FileVault escrow, OS-update enforcement on Apple Silicon which
`docs/modules/patch/spec.md` §342 records as needing "the MDM fast-follow"),
while the agent does inventory, patch, deploy, scripting and remote.

The bound is §1.4(a) and §6.3: on a dual-managed Mac, **software installation
and script execution stay on the signed-job path**. The MDM path delivers
configuration only. A Mac is the one platform where both paths exist, and it is
therefore the one platform where relaxing (a) would create exactly the hole §1.6
prohibition 4 names — MDM-delivered package installation runs vendor-authored
pre/post-install scripts as root **[VERIFY §16.7]**, on a machine that also runs
our agent, with none of invariant 1's guarantees.

For iOS, iPadOS and Android, dual management does not arise: there is no agent.

### 2.6 Apple is command-oriented; Android Management API is policy-oriented; the model carries both without pretending they are the same

Apple's classic MDM is a **command queue**: the server pushes via APNs, the
device connects and asks for work, the server hands back one command, the device
acknowledges **[VERIFY §16.2]**. Android Management API is **declarative**: the
server sets a policy on an enterprise, Google's infrastructure converges devices
to it, and only a small set of true commands (lock, reset password, wipe) exist
**[VERIFY §16.8]**.

The data model therefore keeps `mdm_command` and `mdm_profile` as *separate*
concepts rather than modelling profiles as commands:

- On Apple, assigning a profile **produces** an `InstallProfile` command.
- On Android, assigning a policy **produces** an API call that mutates policy
  state; there is no command row for it, and `mdm_device_profile_state` is
  reconciled from the reported policy-compliance state instead of from a command
  acknowledgement.

**Rejected: normalize Android into a fake command queue so both platforms share
one lifecycle.** The rows would be honest for exactly as long as nobody looked
at them: a synthesized "command" whose `acknowledged_at` was written by us
rather than reported by the device is a fabricated fact in an audited table.

**Rejected: model Apple declaratively to match Android.** That is Declarative
Device Management, is Apple's own direction, and is **deferred** (§17) — with
the explicit note that some capabilities, plausibly including software-update
enforcement on recent OS versions, may be DDM-only **[VERIFY §16.6]**. If §16.6
comes back "yes, DDM-only", this decision is re-opened before build, not after.

### 2.7 Android places a third party in the command path, and that is an operator decision

Using Android Management API means Google's infrastructure holds the enterprise,
stores the policy, and delivers it to the device. This platform never talks to
the phone. Consequences that must be accepted explicitly, not discovered:

- **Google can, in principle, command the device.** This is a materially
  different trust statement from Apple's, where the device connects to *our*
  server.
- **The policy content lives in Google's cloud**, which is in tension with this
  platform's on-prem-first posture (`docs/00-vision.md`).
- The alternative — writing our own DPC and becoming an EMM — is a separate
  product's worth of work and is deferred in §17.

**Recorded as an operator decision, not resolved here.** If the operator rejects
Google in the trust path, v1 ships Apple-only and Android moves to §17. The rest
of this spec is written so that outcome removes tables and routes but changes no
decision.

### 2.8 The MDM ingress is a third ASGI app, on its own port, terminating its own TLS

`docs/kernel/spec.md` §3 already runs the agent gateway as its own process
"built directly on asyncio + `websockets` (not uvicorn, whose ASGI scope hides
the peer certificate)… It terminates TLS itself with client certificates
required; Traefik routes it as TCP passthrough."

The MDM ingress needs the identical property for the identical reason: device
check-ins authenticate by client certificate, and a TLS-terminating reverse
proxy destroys that. So: `backend/app/mdm_ingress/`, its own container, its own
port, its own CA bundle (§2.3), TCP passthrough at the proxy.

**[VERIFY §16.5]:** Apple's protocol is understood to also support a
CMS-signed-request header as an alternative to client-certificate
authentication, for MDM servers behind terminating proxies. If that is accurate
and we choose to support it, the signature verification is a second identity
path and must resolve to the same `mdm_device_identity` row — i.e. it is subject
to §2.3's "identity must have exactly one answer" discipline and needs its own
argument. **v1 supports client-certificate authentication only**, and refuses
the header path, until §16.5 is closed.

### 2.9 Profile payloads are vault-sealed, because they routinely contain secrets

A Wi-Fi payload carries a PSK. A VPN payload carries a shared secret. An email
payload can carry a password. `CLAUDE.md` invariant 3 covers these without
amendment, and the pattern already exists: `docs/modules/provisioning/spec.md`
§7 stores domain-join credentials as **references only**, injected at render
time.

So: `mdm_profile` stores the non-secret payload structure plus, where secrets
are present, a `credential_id` FK into the kernel `credentials` table (vault
ciphertext). Secrets are materialized **only** in the profile bytes that leave
for the device, and:

- are never returned by any API response, at any permission level, including
  `mdm.profile.manage` and including auditor;
- never appear in `mdm_command.params` or in audit metadata;
- are never written to `job_logs` or module logs.

The API returns a redacted rendering with secret-bearing keys replaced by a
sentinel and a `has_secret: true` marker.

### 2.10 The APNs certificate is an operational hazard with a named owner, not a config value

Treated in full in §4. Recorded as a *decision* here because it changes the data
model: vendor credentials get their own table, their own expiry monitoring,
their own alerting path, and their own permission code (`mdm.credential.manage`)
distinct from `mdm.profile.manage` — because the person who edits a Wi-Fi
profile is not the person who should be able to replace the credential that
underpins the entire fleet's manageability.

### 2.11 MDM-reported inventory reaches `devices` through a narrow projection, and does not touch the inventory module's tables

Detail in §9. The decision: MDM writes a small, fixed set of columns on
`devices` and its own `mdm_device` columns. It does **not** write
`installed_software`, `inventory_changes`, `hardware_components`, or
`inventory_snapshots`.

Reason: those tables have a producer contract — `InventoryDocument`, produced by
the agent, exported as JSON Schema, with `inventory_changes` rows carrying
change provenance that assumes a single producer per device
(`docs/modules/inventory/spec.md` §6). A second producer with different
semantics, different fidelity and different collection timing turns the change
history into a stream of phantom installs and removals every time the two
sources disagree. Apps reported by MDM live in `mdm_device_app`.

**Rejected: write MDM app lists into `installed_software` so the fleet software
search covers phones.** Failure mode accepted: fleet software search
(`GET /api/v1/software`) does not cover mobile apps in v1. That is a missing
feature; the alternative is a corrupted change history, which is a wrong answer
presented as a right one. §17 records unification as deferred, with the
prerequisite named (a `source` discriminator on `installed_software` and a
per-source diff, i.e. an inventory-module change that inventory owns).

---

## 3. Enrollment — the second enrollment mechanism

Agent enrollment (`docs/kernel/spec.md` §7) is untouched by this module: token →
CSR → device-bound client certificate. **No MDM enrollment path may issue an
agent certificate, and no `enrollment_tokens` row may be consumed by an MDM
enrollment.** Those tables and that endpoint are not extended.

### 3.1 Apple Automated Device Enrollment (ADE) via Apple Business Manager

The zero-touch path for organization-owned hardware. Shape **[VERIFY §16.3]**:

1. The organization has an Apple Business Manager (or Apple School Manager)
   account and has linked its hardware purchases to it.
2. This platform is registered in ABM as an MDM server; the exchange produces a
   **server token** that the platform stores (vault) and uses to authenticate to
   Apple's device-enrollment service.
3. Serial numbers are assigned to this MDM server in ABM. The platform syncs
   that assignment list into `mdm_ade_assignment`.
4. The platform stores an **enrollment profile** with Apple (supervision,
   whether enrollment is mandatory and non-removable, which Setup Assistant
   panes are skipped) and assigns it to serials.
5. On first boot / after erase, the device contacts Apple, learns which MDM
   server it belongs to, and enrolls against our ingress.

Design consequences that are ours, not Apple's:

- **A `devices` row is created at enrollment, not at ADE assignment.** An ABM
  serial is a purchase record; a `devices` row means "this platform manages
  this". Pre-enrollment state lives in `mdm_ade_assignment`, exactly as
  `provision_run.target_ref` holds a bare-metal target that has no `Device` row
  yet (`docs/modules/provisioning/spec.md` §3).
- **The ADE sync is a reconciliation, not a mirror.** Serials in ABM but not
  enrolled, serials enrolled but no longer assigned in ABM, and serials assigned
  to a *different* MDM server are three distinct states, all surfaced, none
  auto-corrected.

### 3.2 Apple manual / user-initiated enrollment

For hardware not in ABM (existing fleet, BYO-ish company devices). The operator
issues an enrollment link or profile; the user installs it. Properties required:

- The enrollment link carries a **single-use, time-bounded, org-scoped**
  enrollment record — its own table (`mdm_enrollment_profile` +
  `mdm_enrollment_grant`), not `enrollment_tokens`.
- Manual enrollment cannot produce a **supervised** device on iOS **[VERIFY
  §16.4]**; the console must show supervision state truthfully and must not
  offer supervision-only capabilities on unsupervised devices as though they
  would work.
- Manual enrollment is **removable by the user**. The console must show that
  too. A device that silently un-enrolls and stops checking in is the normal
  end state of this path, and §9.4 defines how that is surfaced (it is not
  "offline").

**[VERIFY §16.4]** Apple's account-driven enrollment variants (user enrollment
and device enrollment initiated from Settings against a discovery endpoint) are
believed to exist and to have distinct capability envelopes. v1's decision is
**profile-based manual enrollment only**; account-driven variants are §17.

### 3.3 Apple device identity: SCEP or ACME — an open implementation decision

The enrollment profile must contain a payload that causes the device to obtain a
client identity certificate, which it then presents at every check-in (§2.8).
Two mechanisms are believed available **[VERIFY §16.5]**:

- **SCEP** — the long-standing mechanism. Requires an SCEP responder. The
  **challenge must be dynamic**: per-enrollment, single-use, time-bounded. A
  static shared challenge means anyone who reads one enrollment profile can
  obtain a valid device identity and impersonate a managed device to our
  ingress. This is §1.4(c) and is non-negotiable.
- **ACME with device attestation** — newer, and materially stronger: the
  certificate request is attested by Apple hardware and binds to the actual
  device, so a stolen enrollment profile cannot yield an identity for a
  different machine. Requires minimum OS versions **[VERIFY §16.5]**.

**Decision, conditional and stated as conditional:** prefer ACME with
attestation where the fleet's OS versions permit, fall back to dynamic-challenge
SCEP otherwise, and record which mechanism issued each identity on
`mdm_device_identity.issuance_method` so a fleet's actual assurance level is a
queryable fact rather than an assumption. **Both paths issue from the `mdm-device`
intermediate (§2.3), never the agent role.**

**Implementation risk, named:** `docs/kernel/spec.md` §2 selects **OpenBao PKI**
as the internal CA. Whether OpenBao exposes a usable SCEP responder and/or ACME
directory is **[VERIFY §16.9]**. If it does not, this module must implement a
minimal SCEP responder against the existing `CAService` interface — a
security-sensitive piece of protocol code, and the largest single implementation
risk in the module. It must not be estimated as small.

### 3.4 Android enrollment

**[VERIFY §16.8]** for all of the following. Under Android Management API:

- The platform creates an **enterprise** bound to the customer org, and
  authenticates with a Google service-account credential (vault).
- **Enrollment tokens** are minted per enrollment, rendered as a QR code or link,
  and are single-use and time-bounded by our own policy regardless of what the
  API's defaults are.
- **Zero-touch enrollment** binds devices at the reseller/manufacturer level to
  an enterprise, giving the ADE-equivalent out-of-box path.
- v1 targets **company-owned, fully managed** devices. Work profiles on
  personally-owned devices (BYOD) are §17 — they carry a different privacy
  contract, a different wipe semantic (wipe the profile, not the phone), and a
  different compliance surface, and merging them into v1's model would make
  every wipe route ambiguous about what it destroys.

### 3.5 The enrollment → device-row lifecycle

```
ADE assignment (Apple) / zero-touch binding (Android)   →  mdm_ade_assignment (no devices row)
        │
        ├─ device enrolls, identity issued (§3.3)
        ▼
resolve devices row by (org_id, serial)  ──found──▶  attach mdm_device to existing devices row
        │                                             (this is the dual-managed Mac case, §2.5)
        └──not found──▶ create devices row (os_family ∈ ios|ipados|android|macos)
        │
        ▼
mdm_device (state=enrolled) + mdm_device_identity (cert fingerprint, unique)
        │
        ├─ profiles/policies assigned → commands (Apple) / policy patch (Android)
        ├─ periodic check-ins refresh reported state
        │
        └─ unenroll: user removes profile / operator sends removal / device wiped
                 ▼
        mdm_device.state = unenrolled, identity revoked, devices row RETAINED
```

`devices` rows are never hard-deleted (`docs/04-data-model.md` conventions:
"Devices decommission rather than hard-delete to preserve audit/history").

### 3.6 Un-enrollment and revocation

- **Operator-initiated:** requires `mdm.device.unenroll`. Sends the removal
  command **[VERIFY §16.2]**, marks `mdm_device.state = unenrolled`, revokes the
  device identity certificate, and audits.
- **User-initiated (manual enrollments):** detected as an explicit check-out
  message where the protocol provides one **[VERIFY §16.2]**, otherwise inferred
  from check-in absence past a threshold. **The two are different states and are
  stored as different states** — `unenrolled` vs `not_checking_in`. Inferring
  "the user removed management" from silence is a guess, and the console must
  not present a guess as a fact.
- **Identity revocation is the enforcement**, not the state column: a revoked
  `mdm_device_identity` is refused at the ingress' TLS layer / identity lookup,
  so a device whose management was removed cannot keep transacting because a row
  says `unenrolled`.

### 3.7 Deployment consequence: the control plane becomes internet-reachable

A phone on a cellular network must reach the MDM ingress. Today the control
plane is an on-prem stack reachable by operators over HTTPS
(`docs/01-architecture.md`). MDM requires the ingress port to be reachable from
the public internet, or requires devices to be VPN'd (which defeats managing a
lost or newly-shipped phone).

**This does not touch invariant 2.** Invariant 2 says agents connect outbound
only and the server never opens an inbound connection to an endpoint. Under MDM:
the platform connects **outbound** to Apple's push service; Apple's push wakes
the device; the **device** then connects **outbound** to us. No connection is
ever opened toward an endpoint, and no endpoint binds a listening port. Stated
explicitly because "MDM means inbound" is the wrong intuition and would
otherwise be raised as an invariant-2 conflict that does not exist.

What *does* change is the control plane's own exposure, which is a deployment
decision for the operator: which host, which certificate, what rate limiting,
what DDoS posture on an unauthenticated-by-Keycloak ingress. §12.2 constrains
what that ingress may do.

---

## 4. Vendor credentials and the APNs certificate

### 4.1 What credentials exist

| Credential | Platform | Purpose | Typical lifetime | Failure if it lapses |
|---|---|---|---|---|
| **APNs MDM push certificate** | Apple | Authenticates the platform to Apple's push service so it may wake devices | ~1 year **[VERIFY §16.1]** | **Total loss of manageability.** Devices remain enrolled and configured but cannot be woken, so no command reaches any device |
| **ADE / ABM server token** | Apple | Authenticates to the device-enrollment service; syncs assignments | ~1 year **[VERIFY §16.3]** | New devices cannot be assigned or auto-enrolled; existing devices unaffected |
| **Apps and Books (VPP) content token** | Apple | App licence assignment | ~1 year **[VERIFY §16.10]** | App assignment stops; installed apps unaffected |
| **Google service-account key** | Android | Authenticates to the management API | Per organizational key policy | All Android management stops |

All four are stored via the kernel vault (invariant 3): ciphertext in
`credentials`, metadata in `mdm_vendor_credential`. None is ever returned by an
API response, at any permission level.

### 4.2 The APNs renewal trap

This is the item most likely to cause a real outage, and it is an operational
hazard rather than a technical one.

**What is believed true and must be verified [VERIFY §16.1]:** the APNs MDM push
certificate is obtained from Apple's push-certificates portal using an Apple ID,
against a CSR signed by an Apple MDM-vendor certificate. The certificate carries
a push **topic** in its subject, and enrolled devices are bound to that topic.
**Renewing the certificate must be done as a *renewal* of the existing
certificate, under the same Apple ID** — creating a *new* certificate instead
produces a new topic, and **every enrolled device must be re-enrolled**, which
for phones in users' hands means physically re-enrolling the fleet.

If that is accurate, three rules follow and they are design requirements, not
advice:

1. **The Apple ID that owns the APNs certificate is an organizational identity,
   not a person's.** `mdm_vendor_credential` stores an operator-entered
   `owner_hint` (e.g. the masked Apple ID) and an `owner_contact`, both
   mandatory at upload, both audited, both shown on the credential page — so the
   answer to "whose account is this under" is not "the admin who left".
2. **Renewal is a first-class console workflow**, not a file upload. The console
   generates the CSR, records which existing credential is being renewed
   (`renews_credential_id`), and refuses to accept an uploaded certificate whose
   push topic differs from the current one **without an explicit, typed
   acknowledgement that every enrolled device will require re-enrollment** —
   with the count of affected devices shown.
3. **Expiry is monitored and alerts loudly, early, and repeatedly.** A daily
   sweep emits notifications at 60, 30, 14, 7, 3 and 1 days before
   `not_after`, escalating in severity, through the existing notification module
   (`docs/modules/notification/spec.md`). After expiry the console shows a
   blocking banner on every MDM page. The 60-day lead exists because obtaining a
   renewal can require an Apple ID nobody has logged into for a year.

**A stated non-guarantee:** nothing in this platform can prevent an
administrator from creating a fresh certificate in Apple's portal instead of
renewing. Rule 2 catches it at upload time — after the mistake, before the
damage. That is the best available position and it is stated as such.

### 4.3 What the console shows

A dedicated **MDM → Credentials** page listing all four credential kinds with:
kind, label, owner hint and contact, push topic (Apple), `not_before`,
`not_after`, days remaining, a colour state, the renewal action, and the last
five audit events for that credential. No page anywhere renders a secret.

### 4.4 The other three tokens

Same table, same expiry sweep, same notification ladder, lower severity — each
degrades a capability rather than removing all of them (§4.1). The ADE token's
lapse is the second-nastiest: it is silent (existing devices keep working) and
only shows up when the next shipment of laptops fails to enroll, which is
exactly when nobody has slack. It gets the same 60-day ladder.

---

## 5. Configuration profile management

### 5.1 Model

- **`mdm_profile`** — an authored, versioned unit of configuration, per
  platform. Apple: a configuration profile with a stable `PayloadIdentifier` and
  a monotonically increasing `PayloadVersion` **[VERIFY §16.11]**. Android: a
  policy fragment.
- **`mdm_profile_assignment`** — binds a profile to a target: a device, or a
  `device_group` (reusing the kernel's grouping, including inventory's dynamic
  rules — a phone that matches a rule group gets the group's profiles).
- **`mdm_device_profile_state`** — per device × profile: `pending`, `installed`,
  `failed`, `removed`, with `reported_at` and an error string. **This is
  reconciled from what the device reports, and only from that.** A profile that
  we sent and never heard about stays `pending` forever and is surfaced as
  `pending`, never promoted to `installed` by optimism or by a timer.

### 5.2 The authoring boundary in v1

**v1 accepts operator-supplied profile payloads and a small set of curated
templates. It does not attempt a general payload-authoring UI.**

- **Curated templates** (v1): passcode policy, Wi-Fi, restrictions, and — for
  macOS — the PPPC/TCC consent profile the agent needs
  (`docs/modules/agent/open-questions.md`). Each template renders a validated
  structure; secrets go to the vault (§2.9).
- **Upload** (v1): an operator with `mdm.profile.manage` may upload a payload
  authored elsewhere. It is **validated for structure and rejected if it
  contains any payload type on the module's executable-payload denylist**
  (§6.3). Uploads are audited with a digest of the stored payload.
- **Not in v1:** a visual editor for arbitrary payload types.

**The denylist is a v1 hard requirement, not a nicety.** Without it, "upload a
profile" is a general-purpose bypass of §1.4(a) — and this is the module's
sharpest self-inflicted risk, because the denylist is only as good as our
knowledge of the payload vocabulary, which is Apple's and changes. So the check
is **allowlist-shaped in the implementation** (permitted payload types are
enumerated; anything unrecognized is refused, loudly, naming the type) even
though it is described as a denylist conceptually. An unrecognized payload type
is a refusal and a `mdm.profile_rejected` audit event, never a pass-through.

### 5.3 Reconciliation and drift

A profile assignment produces an install command (Apple) or a policy patch
(Android). Convergence is checked by periodically querying installed profiles
**[VERIFY §16.2]** and diffing against assignments. Three outcomes, all
surfaced, none auto-corrected in v1: `converged`, `missing` (assigned, not
installed), `unexpected` (installed, not assigned by us — common on a device
that was previously managed elsewhere).

**v1 does not auto-remediate drift.** An automatic re-push loop against a device
that keeps rejecting a profile is an infinite command generator, and the
platform has no evidence about *why* a profile failed. Drift is reported; a
human re-pushes.

### 5.4 Android policy

Under a policy-oriented API, "assignment" resolves to computing the effective
policy for a device from its assigned fragments and setting it. Two constraints:

- **The effective-policy computation is deterministic and ordered**, with the
  order stored, so two operators get the same answer and a conflict is a visible
  refusal rather than a last-writer-wins.
- **Conflicting fragments are a refusal at assignment time**, not a silent
  merge.

---

## 6. Application deployment

### 6.1 Apple

App assignment through Apple's Apps and Books / VPP mechanism **[VERIFY
§16.10]**: the org buys licences, the platform assigns a licence to a device (or
user), and the device installs the app from Apple's store. The app binary is
Apple's, signed by its developer and distributed by Apple.

`mdm_app` holds the store identifier, name, platform, and licence counts synced
from the content service. `mdm_app_assignment` binds an app to a device or
group, with `install_mode ∈ {required, available}`.

### 6.2 Android

Managed Google Play **[VERIFY §16.8]**: apps are approved for the enterprise and
included in the device's policy; Google delivers them. Same tables, same
semantics; `install_mode` maps to the policy's install type.

### 6.3 The prohibition, stated as a rule

**No application bytes authored by this platform, and no payload this platform
composed that the device will execute, may travel the MDM path in v1.** In
mechanism terms:

- Apple's enterprise-application install command and any equivalent that takes a
  URL to a package **[VERIFY §16.7]** are **not implemented**, and their payload
  types are absent from §5.2's allowlist.
- Android's private-app hosting of a self-authored APK is **not implemented**.
- macOS software installation stays on `app.deploy` / `script.run` — the signed
  job path, with `docs/modules/app-deploy/spec.md`'s artifact digest
  verification.

This is §1.4(a), and it is the compensation §1.5 charges against the loss of G1.
It is worth restating what relaxing it would cost: an MDM-delivered package on
macOS runs its own install scripts as root **[VERIFY §16.7]**, on a machine that
may also run our agent, with no signature this platform issued and no
verification this platform performs. That is precisely invariant 1's worst case,
arriving through the door invariant 1 does not guard.

### 6.4 Boundary with the app-deploy module

`docs/modules/app-deploy/spec.md` owns packages, artifacts and deployments for
agent-managed endpoints. This module owns store-mediated app assignment for
MDM-managed devices. They share no table and no code path. A device that is both
(a Mac, §2.5) can be a target of both, and they do different things: `app.deploy`
installs a package we hold; `mdm_app_assignment` grants a store licence.

---

## 7. Compliance and attestation

### 7.1 What can actually be observed

Device state under MDM is **what the device reports about itself, mediated by
its operating system** — richer and harder to spoof than an unmanaged
self-report, but still a report. Expected signals, all **[VERIFY §16.2, §16.8]**:

- Apple, via device-information and security-information queries: OS version and
  build, model, serial, supervision state, whether a passcode is present and
  whether it meets policy, hardware encryption state, and on macOS FileVault
  state.
- Android, via the management API's device resource: policy compliance state
  with per-setting non-compliance detail, security posture signals, and app
  inventory.

### 7.2 Attestation is a different word and gets a different column

`mdm_device_compliance.source ∈ {reported, attested, unknown}`.

- `reported` — the device told us.
- `attested` — the claim is backed by cryptographic evidence from the platform
  vendor's hardware attestation path (ACME device attestation on Apple, §3.3;
  Android's hardware-backed key attestation / integrity verdicts **[VERIFY
  §16.8]**) **and that evidence is stored**.
- `unknown` — no current report.

**Rule: nothing may write `attested` unless attestation evidence exists in the
row.** The console renders the three differently and never renders `reported` as
though it were `attested`. This is the same discipline
`docs/modules/endpoint-health-alerting/spec.md` applies with `unavailable` as a
first-class state: a fleet with no attestation must not display as verified.

### 7.3 Boundary with the health module

Phase 16 owns thresholds, alerts, dedup, flap suppression and remediation for
**agent-managed** endpoints; its `health_check.os_family` CHECK admits exactly
`windows|linux|macos` (`app/modules/health/models.py`) and **that is not
changed** (§15.1). MDM compliance is its own evaluation, over its own signals,
producing its own state.

**What is deliberately shared:** notification. Compliance transitions emit
through the notification module like any other event; this module builds no
private notification path (the rule `docs/modules/service-desk/spec.md` §1
states).

**What is deliberately not shared in v1:** alerts, auto-remediation, and the
`approved_action` registry. Auto-remediation of a phone would be an automated
actor dispatching an MDM command, which §1.6 prohibition 6 forbids.

### 7.4 Compliance is derived and recomputed, never asserted

`mdm_device_compliance` rows are derived from the latest reported/attested
signals against org compliance rules, recomputed on each report and on rule
change, with `evaluated_at` and the rule version. A device that has not reported
since before the current rule version is `unknown`, not `compliant`.

---

## 8. Remote lock and remote wipe

The destructive capability, and the module's highest-consequence surface.

### 8.1 Vocabulary

| Command | Effect | Permission |
|---|---|---|
| `mdm.lock` | Lock the device; optionally display a message and phone number | `mdm.device.lock` |
| `mdm.clear_passcode` | Clear the device passcode | `mdm.device.lock` |
| `mdm.erase` | Erase all content and settings | `mdm.device.wipe` |
| `mdm.unenroll` | Remove management, leave data | `mdm.device.unenroll` |

Non-destructive commands (refresh device information, install/remove an assigned
profile, refresh app state, restart/shutdown where supported **[VERIFY §16.2]**)
sit under `mdm.command.send`.

`mdm.clear_passcode` is grouped with lock rather than with routine commands
because it *removes* a security control; it is the command most likely to be
misfiled as benign.

### 8.2 The gate

Every clause below applies to `mdm.erase`; clauses 1, 2, 5, 6 and 7 apply to
`mdm.lock` and `mdm.clear_passcode` as well.

1. **Permission.** `mdm.device.wipe`, held by nobody by default beyond
   super-admin and org-admin (§11.2), and never by technician.
2. **Tenancy.** Target resolution is org-scoped and RLS-backed; a foreign device
   is a 404 (§2.4).
3. **Typed confirmation of the device's own serial.** The request body must
   carry `confirm_serial`, matching `mdm_device.serial` exactly
   (case-insensitive, whitespace-trimmed). A mismatch is a 400 and creates no
   command. This makes an erase impossible to fire by mis-click, because the
   operator must transcribe an identifier that is different for every device.
4. **No group, no filter, no "all"** — §8.3.
5. **A batch of more than one device requires a second human.** A multi-device
   erase is a `mdm_wipe_batch` in state `awaiting_approval`; it dispatches only
   after a **different** user holding `mdm.device.wipe` approves it. Requester
   ≠ approver is enforced server-side. Batches are additionally capped at
   `MDM_WIPE_BATCH_MAX` (default 25) — a request above the cap is refused
   outright with no override path in the API.
6. **A hold window on batches.** An approved batch is dispatched no earlier than
   15 minutes after approval and is cancellable throughout. Single-device erases
   dispatch immediately — a stolen phone is the case this feature exists for.
7. **A rate fuse.** Erases per org per rolling 24 hours are capped
   (`MDM_WIPE_RATE_24H`, default 50). Exceeding it refuses further erases and
   raises a high-severity notification. The pattern is Phase 16's rate fuses,
   reused rather than reinvented.
8. **Audit at every step**, on the same hash chain: `mdm.wipe_requested`,
   `mdm.wipe_approved`, `mdm.wipe_dispatched`, `mdm.wipe_cancelled`,
   `mdm.wipe_acknowledged`, `mdm.wipe_refused` (with reason).
9. **Never automated.** §1.6 prohibition 6.

### 8.3 What makes fleet-wide erase impossible by accident

Not a check — an absence. **The API has no way to express it.** `POST
/api/v1/mdm/devices/{id}/erase` takes one device. `POST /api/v1/mdm/wipe-batches`
takes an explicit array of device ids each paired with its own
`confirm_serial`, capped at 25. There is no group id parameter, no filter
parameter, no saved-search parameter, and no `all: true`. A request body
containing any of those keys is rejected by the request model
(`extra="forbid"`, as every other module's models are).

**Stated honestly:** none of this prevents an org-admin who holds
`mdm.device.wipe` from erasing an entire fleet 25 devices at a time over an
afternoon, and withholding the permission from org-admin would be theatre
because org-admin holds `orgs.manage` and can grant it. What the gate achieves is
that doing so is **slow, loud, individually attributable, second-person-approved,
rate-fused and fully reconstructible from the audit chain.** That is the
achievable property; "impossible" is not, and this spec does not claim it.

### 8.4 What cannot be undone, and must be shown before the button

Before an erase is confirmed the console must state, per device:

- **Activation Lock** (Apple): an erased device may remain locked to a personal
  Apple ID unless it is supervised and the org holds a bypass code **[VERIFY
  §16.12]**. Erasing an unsupervised personal-Apple-ID device can produce a
  brick from the organization's perspective.
- **Factory Reset Protection** (Android): the analogous Google-account lock
  **[VERIFY §16.8]**.
- **Escrowed secrets are lost with the device** unless escrowed: a macOS
  FileVault personal recovery key, an Apple unlock token, an Activation Lock
  bypass code. Where the protocol permits escrow **[VERIFY §16.12]**, these are
  stored in the **vault** as `credentials` rows referenced from `mdm_device`
  (never as a column, never in an API response), and the console states whether
  each exists **for this device** rather than in the abstract.

### 8.5 Lock is not a lesser wipe

Lock is genuinely routine helpdesk work for a lost phone, which is why
technician holds it. It is still a state change that can strand a user, so it
carries clauses 1, 2, 5, 6, 7 of §8.2 — but not the typed serial (clause 3):
requiring transcription for the *reversible* action trains operators to type
serials without reading them, which is exactly the habit clause 3 depends on for
erase.

---

## 9. Device inventory reconciliation

### 9.1 What MDM writes on `devices`

A narrow, fixed projection, and nothing else: `hostname` (the device name
reported by MDM — note it is user-editable on phones and is **not** an
identifier), `os_family`, `os_version`, `serial`, `last_seen_at`. Everything
else MDM knows lives in `mdm_device` and `mdm_device_app` (§2.11).

### 9.2 Matching

At enrollment, resolve an existing `devices` row by **`(org_id, lower(serial))`,
serial non-empty**. That is the only match key.

- Match → attach `mdm_device.device_id` to the existing row (the dual-managed
  Mac, §2.5).
- No match → create a `devices` row.
- Serial empty or unknown → create a row and set
  `mdm_device.needs_reconciliation = true`, surfaced in the console. Do not
  guess.

**Never match on hostname.** Phone names are user-editable, frequently
duplicated ("iPhone"), and would merge two people's devices into one row.

### 9.3 Collisions and re-enrollment

Two enrollments claiming one serial is the *normal* case — a wiped and
re-enrolled phone presents the same serial with a new device identity. Rules:

- If the existing `mdm_device` has a **revoked, expired, or absent** identity, or
  its state is `unenrolled`: the new enrollment takes over the row, the old
  identity is revoked, and `mdm.device_reenrolled` is audited with both
  identities' fingerprints.
- If the existing `mdm_device` has a **live** identity that has checked in
  recently: the second enrollment is **refused** and audited as
  `mdm.enrollment_conflict`. Two live identities for one serial is either a
  cloned identity or a serial collision, and neither should be resolved by
  last-writer-wins.

### 9.4 Which modules must not see MDM devices, and how that holds

The enforcement is §15.1's deliberate non-change: patch, deploy, provisioning,
health and inventory each constrain `os_family` to `windows|linux|macos` in
their own schemas (`app/modules/patch/schema.py`,
`app/modules/provisioning/schema.py`, `app/modules/health/models.py`'s CHECK
`ck_health_check_os_family`, `app/modules/deploy/{packages,scripts}.py`'s
`OS_FAMILIES`, `app/modules/inventory/schema.py`'s
`pattern="^(windows|linux|macos)$"`). An `ios` device is therefore not a legal
target for any of them, by rules that already exist and that this module does
not touch.

Two visible consequences, both requiring frontend work rather than backend
exceptions:

- `GET /api/v1/devices` returns MDM devices. The devices grid must render their
  management as **MDM**, and their connectivity from `mdm_device.last_seen_at`
  — never as agent `offline`, which would report every correctly-functioning
  phone as a problem. Falsifier 12.
- Device detail must not offer agent-only actions (dispatch job, remote control,
  patch now) for an MDM-only device. The frontend hiding them is presentation;
  the backend refusing them is the control (§15.2).

---

## 10. Data model

One migration, `0059_mdm.py`. **Head at the time of writing is
`0058_created_at_defaults`; confirm at implementation time** — the site-server
spec's §15 records four migrations landing from other workstreams between spec
and build.

Every table below is org-scoped: `org_id` column, `ENABLE ROW LEVEL SECURITY`
**and** `FORCE ROW LEVEL SECURITY`, policy
`USING (org_id = current_setting('app.current_org', true)::uuid)` with matching
`WITH CHECK`, per `docs/kernel/spec.md` §5. The kernel's `pg_policies`
completeness test covers them automatically once declared; declaring them is the
migration's job. Model declarations and migration must agree
(`test_schema_drift.py` deparses both).

### 10.1 `mdm_vendor_credential`

`id, org_id, kind (str16: apns|ade_token|vpp_token|android_service_account),
label (str255), credential_id (FK credentials.id — the vault-held secret),
push_topic (str255|null, Apple), owner_hint (str255), owner_contact (str255),
renews_credential_id (FK self|null), not_before, not_after, state
(active|expired|superseded), uploaded_by, created_at`.

Unique partial index: one `active` row per `(org_id, kind)` — except Apple
allows only one APNs certificate per MDM server, which the constraint expresses
rather than documents. No secret column. `owner_hint`/`owner_contact` are NOT
NULL (§4.2 rule 1).

### 10.2 `mdm_enrollment_profile`

`id, org_id, platform (apple|android), kind (ade|manual|android_token|zero_touch),
name, config jsonb (non-secret: supervision, removability, skipped setup panes,
Android provisioning mode), active, created_by, created_at`.

### 10.3 `mdm_enrollment_grant`

Single-use enrollment authorizations. `id, org_id, profile_id FK, token_hash
(str64 — hash only, plaintext shown once, exactly as `enrollment_tokens`),
challenge_hash (str64|null — the dynamic SCEP challenge, §3.3), expires_at,
used_at, used_by_serial, revoked, created_by, created_at`.

**This is a separate table from `enrollment_tokens` on purpose** (§3): an MDM
grant must never be redeemable at `POST /enroll/v1` and an agent token must
never be redeemable at the MDM ingress.

### 10.4 `mdm_ade_assignment`

Pre-enrollment ABM/zero-touch state. `id, org_id, platform, serial (str100),
model, description, assigned_profile_id FK|null, apple_state (str32 — as
reported), enrolled_device_id (FK devices.id|null), last_synced_at`. Unique
`(org_id, platform, serial)`.

### 10.5 `mdm_device`

`id, org_id, device_id (FK devices.id, unique), platform (apple|android),
os_kind (ios|ipados|macos|android), enrollment_kind, serial (str100), udid
(str128|null), imei (str32|null), meid (str32|null), model (str100), os_version
(str50), supervised (bool), user_enrolled (bool), state
(enrolled|not_checking_in|unenrolled|conflict), push_token (str512|null),
push_magic (str128|null), unlock_credential_id (FK credentials.id|null — escrowed
unlock token / recovery key / bypass code, §8.4), needs_reconciliation (bool),
enrolled_at, last_checkin_at, unenrolled_at|null, created_at, updated_at`.

Index `(org_id, state)`, `(org_id, lower(serial))`. **No secret column** — the
escrowed material is a vault reference.

### 10.6 `mdm_device_identity`

`id, org_id, mdm_device_id FK, cert_fingerprint (str64, unique), cert_serial
(str64), issuance_method (scep|acme_attested), not_after, revoked (bool),
issued_at`. The ingress resolves fingerprint → this row → `(mdm_device_id,
org_id)`, the exact shape `app/gateway/main.py` uses for agents (§11.1).

### 10.7 `mdm_profile`

`id, org_id, platform, os_kind|null, name, identifier (str255 — the stable
payload identifier), version (int), payload jsonb (structure, no secrets),
payload_digest (str64), secret_credential_id (FK credentials.id|null),
source (template|upload), template_key (str64|null), state (active|archived),
created_by, created_at`. Unique `(org_id, identifier, version)`.

### 10.8 `mdm_profile_assignment`

`id, org_id, profile_id FK, target_type (device|device_group), target_id,
enabled, created_by, created_at`. Unique `(org_id, profile_id, target_type,
target_id)`.

### 10.9 `mdm_device_profile_state`

`id, org_id, mdm_device_id FK, profile_id FK|null, reported_identifier (str255 —
so an `unexpected` profile we never assigned can be recorded), state
(pending|installed|failed|removed|unexpected), error (text), reported_at`.
Unique `(mdm_device_id, reported_identifier)`.

### 10.10 `mdm_app` / `mdm_app_assignment` / `mdm_device_app`

- `mdm_app`: `id, org_id, platform, source (vpp|managed_play), store_id (str128),
  name, licences_total (int|null), licences_used (int|null), last_synced_at`.
  Unique `(org_id, platform, store_id)`.
- `mdm_app_assignment`: `id, org_id, app_id FK, target_type, target_id,
  install_mode (required|available), enabled, created_by, created_at`.
- `mdm_device_app`: `id, org_id, mdm_device_id FK, store_id, name, version,
  managed (bool), reported_at`. **This is where MDM app inventory lives — not
  `installed_software`** (§2.11).

### 10.11 `mdm_command`

The deliberate mirror of `jobs`, in its own table (§2.2).

`id, org_id, mdm_device_id FK, type (str64), params jsonb, state
(queued|signed|sent|acknowledged|error|not_now|expired|cancelled|refused),
signature (str, §1.7), nonce (str36), issued_at, expires_at, sent_at|null,
acknowledged_at|null, result jsonb|null, error text, requested_by (FK users|null),
actor_type (str16), batch_id (FK mdm_wipe_batch|null), created_at`.

Index `(org_id, mdm_device_id, state)`, `(org_id, state, expires_at)`.

`not_now` is a first-class state, not an error: Apple's protocol is understood to
return it when the device cannot act right now (locked screen, etc.) **[VERIFY
§16.2]**, and collapsing it into `error` produces a console full of failures that
are not failures.

### 10.12 `mdm_wipe_batch`

`id, org_id, state (awaiting_approval|approved|dispatching|complete|cancelled),
device_count (int), requested_by, approved_by|null, requested_at, approved_at,
dispatch_after (timestamptz), cancelled_at|null, reason (text)`.

DB CHECK: `approved_by IS NULL OR approved_by <> requested_by` — §8.2 clause 5
pinned at the database layer so it cannot be introduced by direct SQL or lost in
a refactor. Precedent: `docs/modules/endpoint-health-alerting/spec.md`'s autonomy
CHECK.

### 10.13 `mdm_device_compliance`

`id, org_id, mdm_device_id FK, rule_version (int), status
(compliant|non_compliant|unknown), source (reported|attested|unknown), details
jsonb, attestation_ref (str|null), evaluated_at`. CHECK:
`source <> 'attested' OR attestation_ref IS NOT NULL` — §7.2's rule at the
database layer.

---

## 11. Tenancy, RBAC, audit, credentials

### 11.1 Tenancy (invariant 5)

Every table in §10 carries `org_id` with forced RLS (§10 preamble). Service
functions carry an explicit `org_id` predicate **as well as** relying on RLS,
and the tests pin the predicate on the admin engine — because through
`org_session` RLS rejects first, so a cross-org test alone still passes with the
predicate deleted (IPAM §11.1's finding, reused).

**The ingress's org resolution mirrors the gateway's exactly.** A check-in is
authenticated by the device identity certificate; the ingress resolves
fingerprint → `mdm_device_identity` → `(mdm_device_id, org_id)` and opens the
tenant session with that org. It never reads an org, a device id, a serial or a
UDID out of the request body for identity purposes. `docs/kernel/spec.md` §8
states the rule for agents — "the gateway never trusts message content for
identity" — and it is the same rule here. A check-in body referencing a
different device is discarded and audited (`mdm.identity_mismatch`), the
analogue of `gateway.identity_mismatch`.

**Multi-tenancy is per-org vendor credentials.** Each org brings its own APNs
certificate, its own ABM, its own Android enterprise. Push topics differ per org
by construction. There is no shared vendor identity across tenants, so an MSP
deployment does not create a single credential whose compromise reaches every
customer's fleet.

### 11.2 RBAC (invariant 6)

Module code `mdm`. Package `backend/app/modules/mdm/`; ingress
`backend/app/mdm_ingress/`; routes `/api/v1/mdm/...`; frontend
`frontend/src/modules/mdm/`.

| Code | Description |
|---|---|
| `mdm.read` | View enrolled devices, profiles, apps, compliance, commands, credential metadata |
| `mdm.enroll.manage` | Manage enrollment profiles and grants; sync and assign ADE/zero-touch |
| `mdm.credential.manage` | Upload, renew, and retire APNs / ADE / VPP / Android credentials |
| `mdm.profile.manage` | Author, edit, assign, and remove configuration profiles and policies |
| `mdm.app.manage` | Sync, assign, and revoke managed applications |
| `mdm.command.send` | Send non-destructive device commands |
| `mdm.device.lock` | Remote lock; clear passcode |
| `mdm.device.wipe` | Remote erase; approve a wipe batch |
| `mdm.device.unenroll` | Remove management from a device |

Role grants (mirroring every other module's seed so
`test_rbac.py::test_org_admin_has_all_permissions` keeps holding):

| Role | Grants |
|---|---|
| super-admin, org-admin | all nine |
| technician | `mdm.read`, `mdm.command.send`, `mdm.device.lock` |
| auditor | `mdm.read` |
| read-only | `mdm.read` |

Three separations are deliberate and each rules out a specific mistake:

- **`mdm.credential.manage` ≠ `mdm.profile.manage`.** The person who edits a
  Wi-Fi profile should not be able to replace the credential the entire fleet's
  manageability depends on (§4.2).
- **`mdm.device.wipe` ≠ `mdm.device.lock`.** The site-server spec's argument,
  reused: routine helpdesk work and irreversible destruction are not one grant.
- **`mdm.command.send` ≠ `mdm.device.lock`.** A refresh-inventory command and a
  passcode clear are not the same class of act (§8.1).

Every route guards with `require_permission(...)`. Command dispatch is checked
server-side at the service boundary. The frontend hides what the API would
refuse and gates nothing.

**Read is not uniformly read.** `mdm.read` returns credential *metadata* only,
and returns profiles in their redacted rendering (§2.9). No permission returns a
secret.

### 11.3 Audit (invariant 4)

| Action | Actor | Target | Metadata |
|---|---|---|---|
| `mdm.credential_uploaded` | user | `mdm_vendor_credential` | kind, topic, not_after, owner_hint, renews_credential_id |
| `mdm.credential_topic_changed` | user | `mdm_vendor_credential` | old_topic, new_topic, affected_device_count |
| `mdm.credential_expiring` | system | `mdm_vendor_credential` | kind, days_remaining |
| `mdm.credential_expired` | system | `mdm_vendor_credential` | kind, not_after |
| `mdm.enrollment_profile_created` / `_updated` / `_deleted` | user | `mdm_enrollment_profile` | changed fields |
| `mdm.enrollment_grant_issued` / `_revoked` | user | `mdm_enrollment_grant` | profile_id, expires_at |
| `mdm.device_enrolled` | system | `device` | mdm_device_id, platform, serial, enrollment_kind, supervised, issuance_method |
| `mdm.device_reenrolled` | system | `device` | old/new identity fingerprints |
| `mdm.enrollment_conflict` | system | `mdm_device` | serial, refused fingerprint |
| `mdm.device_unenrolled` | user \| system | `device` | reason, initiated_by |
| `mdm.identity_mismatch` | system | `mdm_device` | fingerprint, claimed target |
| `mdm.checkin_rejected` | system | — | reason, fingerprint (rate-limited, §11.3 note) |
| `mdm.profile_assigned` / `_removed` | user | `mdm_profile` | target_type, target_id |
| `mdm.profile_rejected` | user | `mdm_profile` | reason, unrecognized payload type (§5.2) |
| `mdm.app_assigned` / `_revoked` | user | `mdm_app` | target, install_mode |
| `mdm.command_dispatched` | user \| system | `mdm_device` | command_id, type, batch_id |
| `mdm.command_refused` | system | `mdm_device` | command_id, reason (`signature_invalid`, `rate_fused`, `scope_ceiling`, `rbac`) |
| `mdm.wipe_requested` / `_approved` / `_cancelled` / `_dispatched` / `_acknowledged` / `_refused` | user \| system | `mdm_device` \| `mdm_wipe_batch` | device ids, confirm-serial match, approver, reason |
| `mdm.device_locked` | user | `mdm_device` | command_id |

**No per-check-in audit event.** A fleet of phones checking in produces volume
that would swamp a hash-chained log whose value is that a human can read it —
the reason `_handle_health_report` gives, cited by site-server §10.3. Enrollment,
un-enrollment, commands and refusals are audited; routine check-ins are not.
`mdm.checkin_rejected` is **coalesced** (one event per fingerprint per hour with
a count) for the same reason — an unauthenticated internet-facing port is
scannable, and an uncoalesced rejection event is an audit-log flooding primitive.

### 11.4 Credentials (invariant 3)

Vendor credentials (§4.1), profile secrets (§2.9) and escrowed device secrets
(§8.4) are all `credentials` rows with vault ciphertext, referenced by FK. This
module stores no plaintext secret in any column, returns none in any response,
and writes none to any log. Checkout is audited by the existing vault service.

---

## 12. API

### 12.1 Operator API — `/api/v1/mdm/...`

All request models `extra="forbid"`. All routes `require_permission`.

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/credentials` | `mdm.read` | Metadata only; never a secret |
| POST | `/credentials/csr` | `mdm.credential.manage` | Generate a CSR for an APNs renewal |
| POST | `/credentials` | `mdm.credential.manage` | Upload; refuses a changed push topic without `acknowledge_reenrollment: true` (§4.2) |
| DELETE | `/credentials/{id}` | `mdm.credential.manage` | Retire |
| GET/POST/PATCH/DELETE | `/enrollment-profiles[/{id}]` | `mdm.read` / `mdm.enroll.manage` | |
| POST | `/enrollment-grants` | `mdm.enroll.manage` | Returns the plaintext grant **once** |
| POST | `/ade/sync` | `mdm.enroll.manage` | Sync ABM assignments into `mdm_ade_assignment` |
| GET | `/ade/assignments` | `mdm.read` | Includes the three reconciliation states (§3.1) |
| GET | `/devices` | `mdm.read` | MDM view: enrollment kind, supervision, compliance, last check-in |
| GET | `/devices/{id}` | `mdm.read` | Detail incl. profile state, apps, command history |
| POST | `/devices/{id}/commands` | `mdm.command.send` | Non-destructive vocabulary only; a destructive type here is a 400 |
| POST | `/devices/{id}/lock` | `mdm.device.lock` | |
| POST | `/devices/{id}/clear-passcode` | `mdm.device.lock` | |
| POST | `/devices/{id}/erase` | `mdm.device.wipe` | Body `{confirm_serial, reason}`. One device. No group, no filter (§8.3) |
| POST | `/devices/{id}/unenroll` | `mdm.device.unenroll` | |
| POST | `/wipe-batches` | `mdm.device.wipe` | Body: explicit `[{device_id, confirm_serial}]`, ≤ `MDM_WIPE_BATCH_MAX`, plus `reason` |
| POST | `/wipe-batches/{id}/approve` | `mdm.device.wipe` | Requester ≠ approver, enforced server-side and by DB CHECK |
| POST | `/wipe-batches/{id}/cancel` | `mdm.device.wipe` | Legal until dispatch |
| GET/POST/PATCH | `/profiles[/{id}]` | `mdm.read` / `mdm.profile.manage` | Redacted rendering on read (§2.9) |
| POST | `/profiles/{id}/assignments` | `mdm.profile.manage` | |
| GET | `/apps`, POST `/apps/sync`, POST `/apps/{id}/assignments` | `mdm.read` / `mdm.app.manage` | |
| GET | `/compliance` | `mdm.read` | With `source` (§7.2) on every row |
| GET | `/commands` | `mdm.read` | Filterable by device, state, type |

**Deliberately absent, and their absence is the control:** any route that erases
by group, by filter, by tag, or by saved search; any route returning a secret;
any route that creates an agent `Job`.

### 12.2 Device-facing ingress — its own app, its own port

`backend/app/mdm_ingress/`, TLS terminated in-process with the `mdm-device` CA
bundle required for client certificates (§2.3, §2.8). Traefik TCP passthrough.

Endpoints (shapes **[VERIFY §16.2]**): the check-in endpoint (device
authenticate / token update / check-out), the command endpoint (device polls,
server returns at most one queued command, device reports the previous result),
and the identity endpoint (SCEP or ACME, §3.3).

Constraints on this app, all testable:

- It imports nothing from `app.gateway` or `app.jobs`.
- It creates no `Job` and publishes to no `dispatch:*` channel.
- It resolves identity **only** from the peer certificate (§11.1).
- It writes only to this module's tables and the narrow `devices` projection
  (§9.1).
- It is rate-limited per source and per identity, and its rejection auditing is
  coalesced (§11.3).

---

## 13. Frontend — `frontend/src/modules/mdm/`

React + TypeScript strict, TanStack Query, shadcn/ui, per `docs/kernel/spec.md`
§12. No `any`.

- **Devices** — MDM device list: platform, model, OS, enrollment kind,
  supervision, compliance (with the `source` distinction rendered, §7.2), last
  check-in. Detail pane: profiles and their state, apps, command history,
  identity and its expiry, escrow status (§8.4 — "recovery key escrowed: yes/no",
  never the key).
- **Enrollment** — enrollment profiles, grant issuance (plaintext shown once),
  ADE assignment reconciliation with its three states.
- **Profiles** — list, template-based creation, upload with the allowlist
  refusal surfaced by payload type, assignments, drift view.
- **Apps** — catalogue, licence counts, assignments.
- **Credentials** — §4.3. The expiry banner lives at module level and is
  unmissable.
- **Commands** — history and states, with `not_now` rendered as its own thing.
- **Erase flow** — a dedicated, deliberately unpleasant path: shows what cannot
  be undone (§8.4) for *this* device, requires the serial typed, and for batches
  shows requester/approver and the hold countdown.

**Cross-module frontend change, owned by devices not by this module:** the main
devices grid must render MDM-managed devices' management and connectivity
correctly (§9.4). Requested in §15.4.

---

## 14. Testing requirements

Test-first, against real PostgreSQL 16, per `docs/kernel/spec.md` §14. Every row
below states what observation would show the requirement broken. No row claims a
property that no test measures.

### 14.1 The invariant boundary (§1.6) — the rows that matter most

| # | Requirement | Falsifier |
|---|---|---|
| 1 | No MDM code path creates a job | An AST-level scan of `app/modules/mdm/` and `app/mdm_ingress/` finds a call to `JobEngine.create`, an import of `app.jobs.engine` or `app.gateway`, or a publish to a `dispatch:*` channel. (Structural-assertion precedent: `test_ipam_sweep_dispatch.py`, `test_site_server_kernel_boundary.py`) |
| 2 | The two CAs are disjoint | An MDM device identity certificate completes a TLS handshake with the agent gateway, **or** an agent client certificate completes a handshake with the MDM ingress. Tested with real certificates from both roles against both listeners |
| 3 | An MDM command cannot target an agent-only device | `POST /mdm/devices/{id}/commands` for a `devices` row with an `agents` row and no `mdm_device` row returns anything other than 404 |
| 4 | A job cannot target an MDM-only device | `JobEngine.create` for a device with no `agents` row returns a job instead of raising (§15.2). If §15.2 is rejected, this becomes: a module-level dispatch guard is absent from any dispatch route |
| 5 | A forged command is never transmitted | An `mdm_command` row inserted by raw SQL, or an existing row whose `params` were mutated after signing, is handed to the vendor transport. Test drives a fake transport that records everything it is asked to send |
| 6 | No executable payload traverses the MDM path | A profile containing a payload type outside the §5.2 allowlist is accepted by upload; **or** any command type in the module's vocabulary takes a URL or bytes that the device will execute |
| 7 | No automated actor can send an MDM command | Any `mdm.*` string is accepted by `app/core/approved_actions.py`'s registry, appears in `APPROVABLE_JOB_TYPES`, or is registered in `app.jobs.registry`; **or** any `/api/v1/mdm/` route appears in the AI read-mode GET allowlist (`app/core/ai/tools/registry.py`) |

### 14.2 Tenancy, RBAC, audit

| # | Requirement | Falsifier |
|---|---|---|
| 8 | Full org isolation | Org A reads, commands, or erases through org B's `mdm_device`, including via raw SQL with the org predicate removed; **or** the kernel's `pg_policies` completeness test fails on any §10 table |
| 9 | Predicate and RLS are independent defences | Deleting the explicit `org_id` predicate from a service function leaves every test green (i.e. the suite is testing only RLS). Pinned on the admin engine, per IPAM §11.1 |
| 10 | Ingress identity comes only from the certificate | A check-in body naming a different device id, serial, or UDID changes which row is written; **or** a check-in with an unknown or revoked fingerprint is processed rather than rejected and audited |
| 11 | RBAC on every route | Any route in §12.1 returns 2xx without its permission; **or** seeded role grants differ from §11.2; **or** `technician` can erase |
| 12 | Secrets never leave | Any API response, audit metadata field, log line, or error message under any permission contains a vendor credential, a profile secret value, an escrowed key, or an unlock token. Tested by a response-tree scan for the planted secret values, not by inspecting the fields we remembered to check |
| 13 | The audit chain covers the second path | Any command dispatch, refusal, or terminal state produces no audit event; **or** the chain fails `AuditService.verify` after a full enrollment→command→acknowledge cycle |

### 14.3 The destructive path (§8)

| # | Requirement | Falsifier |
|---|---|---|
| 14 | Erase cannot be expressed at scale | Any route accepts a group id, a tag, a filter, a saved search, or `all: true` for erase; **or** a batch above `MDM_WIPE_BATCH_MAX` is accepted |
| 15 | Typed serial is load-bearing | An erase with a wrong, empty, or omitted `confirm_serial` creates a command |
| 16 | Second-person approval holds | A batch dispatches with `approved_by == requested_by`, at the API **or** by direct SQL (the DB CHECK is the second test) |
| 17 | The hold window is real | An approved batch dispatches before `dispatch_after`; **or** a cancel during the window still results in a transmitted command |
| 18 | The rate fuse fires | Erases beyond `MDM_WIPE_RATE_24H` in a rolling window are dispatched rather than refused and alerted |
| 19 | Irreversibility is shown before, not after | The erase confirmation payload omits activation-lock / escrow status for that specific device. (Asserted on the API response the UI renders from, so the check does not depend on the frontend) |

### 14.4 Enrollment and identity

| # | Requirement | Falsifier |
|---|---|---|
| 20 | Enrollment grants are single-use and time-bounded | A grant redeems twice, redeems after `expires_at`, or redeems after revocation |
| 21 | The two enrollment systems do not cross | An `enrollment_tokens` row is redeemable at the MDM ingress, **or** an `mdm_enrollment_grant` is redeemable at `POST /enroll/v1` |
| 22 | No static identity challenge | The SCEP challenge is equal across two enrollments, is reusable, or has no expiry (§3.3). *If ACME-with-attestation is chosen, this becomes: an identity is issued without a stored attestation and `issuance_method` still records `acme_attested`* |
| 23 | Serial matching is the only match key | Two devices with the same user-set name merge into one `devices` row; **or** a blank serial silently matches an existing row instead of setting `needs_reconciliation` |
| 24 | Re-enrollment vs conflict are distinguished | A second live identity for one serial takes over the row instead of being refused and audited (§9.3) |

### 14.5 State honesty

| # | Requirement | Falsifier |
|---|---|---|
| 25 | Profile state is reported, not assumed | A `mdm_device_profile_state` row reaches `installed` without a device report — e.g. by a timer, on dispatch, or on command acknowledgement alone where acknowledgement means "received", not "applied" |
| 26 | `attested` requires evidence | A compliance row with `source='attested'` exists with `attestation_ref IS NULL` (API layer **and** DB CHECK) |
| 27 | `not_now` is not an error | A `not_now` response marks the command `error`, or is retried without bound |
| 28 | Silence is not un-enrollment | A device that stops checking in is recorded as `unenrolled` rather than `not_checking_in` (§3.6) |
| 29 | An MDM device is not an offline agent | The devices grid renders an enrolled, healthy, recently-checked-in phone as agent-`offline`, or offers it agent-only actions |
| 30 | Credential expiry alerts actually fire | The sweep produces no notification at each of the 60/30/14/7/3/1-day thresholds, or produces one only once |

### 14.6 Deliberate non-changes, pinned

| # | Requirement | Falsifier |
|---|---|---|
| 31 | Agent modules still see three OS families | `app/modules/{patch,provisioning,health,deploy,inventory}` admit `ios`, `ipados`, or `android` anywhere — including `ck_health_check_os_family` (§15.1) |
| 32 | Agent enrollment is unchanged | `app/services/enrollment.py`'s `os_family` pattern admits a mobile family, i.e. an agent can enroll as a phone |
| 33 | The kernel is not forked | The module adds a file to `app/jobs/`, `app/gateway/`, or `app/core/` beyond the §15 changes the operator explicitly accepted |

### 14.7 What is NOT tested, and is therefore not claimed

- **Nothing here exercises a real Apple or Google service.** Every vendor
  interaction is a fake transport. This suite can show that our side is
  internally consistent; it cannot show that Apple accepts our check-in
  response, that our profile parses on a real iPhone, or that a real erase
  erases. Those require physical devices, an Apple Developer/ABM account, and a
  Google enterprise — none of which exist in this project, exactly as no macOS
  or Windows machine exists for `docs/modules/endpoint-health-alerting/spec.md`.
- **The module must therefore describe its Apple/Android behaviour as
  unexecuted** in its status notes, in the same terms the agent's macOS path is
  described (`docs/modules/agent/README.md`: "deployable via MDM, still
  unexecuted by anybody"). A green suite here means the control plane is
  consistent, not that the fleet is managed.
- **No idle-budget claim is made or owed.** This module adds no agent code and
  no agent capability. `CLAUDE.md`'s idle budget is not engaged, and this spec
  makes no footprint claim to be verified.

---

## 15. Changes outside this module (surfaced, not designed around)

### 15.1 `os_family` — one widening, and four deliberate non-changes

`devices.os_family` is `String(20)` with **no database CHECK**
(`app/models/core.py`, `0001_core_schema.py`), so admitting `ios`, `ipados` and
`android` needs no migration of that column. What it needs is a decision,
recorded here:

- **`devices.os_family` admits `ios|ipados|android`** in addition to
  `windows|linux|macos`. Written only by this module.
- **`app/services/enrollment.py`'s `os_family` pattern stays
  `^(windows|linux|macos)$`.** An agent must never enroll as a phone.
- **`app/modules/inventory/schema.py`, `patch/schema.py`,
  `provisioning/schema.py`, `deploy/{packages,scripts}.py`, and
  `health/models.py`'s `ck_health_check_os_family` all stay three-valued.** This
  is not an oversight to fix later; it is the mechanism by which MDM devices
  stay out of agent modules (§9.4), and falsifier 31 pins it.

### 15.2 One proposed kernel change: `JobEngine.create` refuses an agentless target

**Proposed:** `JobEngine.create` raises `NoAgentForDevice` when the target
device has no `agents` row.

**Why it belongs in the kernel and not in this module:** it is §1.6 prohibition
2, and a guard that lives in one module's dispatch route is a guard that the
next module forgets. It also fixes a defect that predates this module: a job
created for any agentless device today is signed, queued, never delivered, and
eventually swept to `expired` — reporting a timeout for a command that was never
deliverable.

**Cost and risk, stated:** it is a kernel change, and `CLAUDE.md` says modules
register into the kernel rather than fork it — so it is the operator's or kernel
owner's call, not this spec's. The migration-path risk to check before accepting
is provisioning, which creates jobs around enrollment
(`docs/modules/provisioning/spec.md` §3: `provision_run.device_id` is nullable
until a target enrolls); if any path there creates a job before the `agents` row
exists, this guard breaks it.

**If rejected:** the module enforces the same rule at its own boundary plus a
structural test, and prohibition 2 becomes a module-local property rather than a
platform one. That is weaker and is stated as weaker.

### 15.3 CA: a second issuing role

`CAService` (`app/core/ca.py`) needs to issue from an `mdm-device` role/
intermediate distinct from the agent role, and the gateway's TLS context must be
narrowed to load the agent bundle only (it may today load a bundle that would
become over-broad once a second intermediate exists under the same root).
Additive to the interface; the narrowing is the security-relevant half and
falsifier 2 measures it.

### 15.4 Requested in other modules (each theirs to accept)

- **Devices frontend** — render management mode and MDM connectivity (§9.4,
  falsifier 29).
- **Notification** — one new event category for MDM credential expiry and
  compliance transitions. No new dispatch path; this module builds none.
- **Reporting** — MDM device counts and compliance as report subjects. Optional
  for v1.

### 15.5 Documentation amendments required (deliberate, per `CLAUDE.md`)

Owned by whoever lands the build, not by this file, and required **in the same
commit as the build** — the service-desk precedent ("the code and the docs must
never disagree about this"):

- **`CLAUDE.md`** — invariant 1b (§1.4). **This is the blocking one.**
- **`docs/00-vision.md`** — the MDM bullet moves from "DECIDED, NOT BUILT" to a
  scope statement, and the scope-boundaries list records what MDM is *not*
  (Windows MDM, BYOD, DDM — §17).
- **`docs/03-security-model.md`** — a trust-boundary entry for MDM-managed
  devices, and a threat-model row for "MDM path compromised" with §1.5's honest
  answer.
- **`docs/06-roadmap.md`** — a phase row. The current table ends at Phase 18;
  MDM is Phase 19 unless the operator reorders. The roadmap's own guard note
  says the set of module specs without a roadmap phase is pinned by a test, so
  adding this spec without adding the row will break that test — which is the
  intended behaviour.
- **`docs/01-architecture.md`** — the MDM ingress as a fourth control-plane
  component and the internet-reachability consequence (§3.7).
- **`docs/04-data-model.md`** — `os_family` now includes mobile families.

### 15.6 Deployment

New container for the MDM ingress, new port, Traefik **TCP passthrough** (not
HTTP routing — §2.8), external DNS and a publicly trusted certificate for the
ingress host, and outbound reachability to Apple's push and enrollment services
and/or Google's APIs. All in `deploy/docker-compose.yml` and its README.

---

## 16. External facts requiring verification before implementation

**Nothing in §2–§13 may be implemented until the relevant row here is closed
against primary vendor documentation.** Each row states the consequence of the
assumption being wrong, because that is what decides which to check first.

| # | Assumption made in this spec | Where used | Consequence if wrong |
|---|---|---|---|
| 16.1 | The APNs MDM push certificate is ~1-year-lived; **renewal must occur under the same Apple ID and preserve the push topic**, and creating a new certificate instead forces re-enrollment of every device | §4.2 — the entire renewal workflow, rule 2's topic check, the 60-day ladder | If renewal is more forgiving, §4.2 is over-engineered (cheap). If it is *stricter* (e.g. the vendor-signed CSR also expires, or the portal requires an active MDM-vendor membership), the workflow is incomplete and a fleet becomes unmanageable at the worst possible moment |
| 16.2 | Classic Apple MDM is a server-side command queue: APNs push → device connects → server returns one command → device acknowledges; with distinct check-in messages for authenticate / token update / check-out, a `NotNow` response, and query commands for device information, security information, installed profiles and installed applications | §2.6, §3.6, §5.3, §7.1, §8.1, §10.11, §12.2 | The `mdm_command` lifecycle, the `not_now` state, the reconciliation design and the ingress route shapes are all wrong together. **This is the largest single verification item** |
| 16.3 | ADE/ABM: the platform registers as an MDM server, receives a server token that expires ~annually, syncs serial assignments, and stores enrollment profiles with Apple which are assigned to serials | §3.1, §4.1, §12.1 | The enrollment model and `mdm_ade_assignment` are wrong; the ADE credential's expiry ladder may be mistimed |
| 16.4 | Supervision is available through ADE (and Apple Configurator) but not through profile-based manual enrollment on iOS; account-driven enrollment variants exist with different capability envelopes | §3.2 | The console would offer supervision-only capabilities on devices that cannot honour them — the exact failure §3.2 is written to avoid |
| 16.5 | Device identity is obtained via a SCEP payload (dynamic challenge required) or ACME with device attestation on recent OS versions; the device presents it as a TLS client certificate at check-in; a CMS-signed-request header exists as an alternative for proxied deployments; the enrollment profile can pin the server's anchor certificate | §2.8, §3.3, §1.5's transport row | If attestation is unavailable, §7.2's `attested` source is unreachable and must be removed rather than faked. If anchor pinning is unavailable, §1.5's transport residual is larger than stated and must be restated |
| 16.6 | Declarative Device Management is Apple's forward direction, and some capabilities — plausibly including software-update enforcement on recent OS versions — may be DDM-only | §2.6, §17 | If a v1 requirement (notably the macOS OS-update enforcement `docs/modules/patch/spec.md` §342 defers to "the MDM fast-follow") is DDM-only, §2.6 is re-opened **before** build and v1 grows a DDM path |
| 16.7 | MDM can install an enterprise application/package, and such a package can execute installer scripts as root on macOS | §2.5, §6.3, §1.6 prohibition 4 | If this is *not* possible, §6.3's prohibition costs less than stated. If it is possible in more ways than one command, the §5.2 allowlist must enumerate more refusals |
| 16.8 | Android Management API: Google-hosted enterprises and policies; service-account authentication; enrollment tokens and zero-touch; a small true-command set (lock, reset password, wipe); reported compliance detail; hardware-backed attestation/integrity signals; managed Google Play app delivery; factory-reset protection | §2.7, §3.4, §6.2, §7.1, §8.4 | The whole Android half of the model. If the capability surface is materially different, Android becomes its own spec rather than a platform column in this one |
| 16.9 | OpenBao PKI can act as a SCEP responder and/or an ACME directory | §3.3 | If not, this module must implement a minimal SCEP responder against `CAService` — security-sensitive protocol code and the module's largest implementation risk. **Check this early**, because it changes the estimate, not just the design |
| 16.10 | Apps and Books (VPP) uses a content token, ~annual, with device- or user-assigned licences | §4.1, §6.1 | App assignment model and the fourth credential's lifecycle |
| 16.11 | Apple configuration profiles carry a stable payload identifier and an integer version, and re-installing a higher version replaces the installed one | §5.1, §10.7 | Profile versioning and the `(org_id, identifier, version)` uniqueness are wrong; drift detection would misreport |
| 16.12 | Activation Lock can strand an erased device; a bypass code is obtainable for supervised devices; unlock tokens / FileVault recovery keys can be escrowed to the MDM server | §8.4 | §8.4's pre-erase disclosure is wrong in either direction — either alarming about a non-problem, or silent about a real one |

**Verification order, by consequence:** 16.2 and 16.9 first (they change the
design and the estimate), then 16.1 and 16.6 (they change what v1 must contain),
then the rest.

---

## 17. Explicitly deferred from v1

| Deferred | Gate / reason |
|---|---|
| **Declarative Device Management (Apple)** | §2.6. Re-opened immediately if 16.6 shows a v1 requirement is DDM-only |
| **BYOD: Apple User Enrollment, Android work profile on personal devices** | Different privacy contract, different wipe semantic (profile vs device). Merging it into v1 would make every erase route ambiguous about what it destroys (§3.4) |
| **Account-driven Apple enrollment variants** | §3.2, pending 16.4 |
| **Windows MDM (OMA-DM / Autopilot)** | Windows is agent-managed on this platform. Adding a second management path for a platform that already has a working one buys nothing and doubles the surface |
| **ChromeOS management** | No brief, no demand recorded |
| **A custom Android DPC (becoming an EMM ourselves)** | The alternative to §2.7's Google-in-the-path trade. A separate product's worth of work; re-open only if the operator rejects §2.7 *and* still wants Android |
| **MDM-delivered software installation of platform-authored packages** | §6.3, §1.4(a). This is not a scheduling deferral — relaxing it requires re-arguing the amendment |
| **Auto-remediation of configuration drift** | §5.3. Needs evidence about *why* a profile failed; without it, a re-push loop is an infinite command generator |
| **Managed Lost Mode and device location** | **Operator decision, not an engineering one.** Location of staff-carried phones is a privacy and employment-law question for a BPO, not a feature toggle. Requires an explicit policy decision, a consent posture, and probably a separate permission and retention rule before it is even designed |
| **Unifying `mdm_device_app` with `installed_software`** | §2.11. Prerequisite is a `source` discriminator and per-source diffing in the **inventory** module — inventory's change to accept, not this module's to make |
| **MDM devices in patch, health, backup, remote, provisioning** | §9.4. Each would need its own argument about what the capability even means on a phone; the current three-family constraints are the enforcement, not an accident |
| **Telecom / cellular data usage reporting** | No requirement recorded |
| **A visual editor for arbitrary profile payload types** | §5.2. Would need the allowlist to become a full payload-type model |
| **Certificate-based Wi-Fi/VPN identity issuance to managed devices (per-device 802.1X certs)** | A second identity-issuance purpose on top of §3.3's; wants 16.9 closed first |
| **Sentinel integration for mobile OS vulnerabilities** | Sentinel feeds patch prioritization for agent-managed endpoints; mobile has no patch module to prioritize |

---

## 18. Related documents

- `CLAUDE.md` — the invariants; §1.4 proposes an addition to them
- `docs/00-vision.md` — the reversal that commissioned this spec
- `docs/01-architecture.md` — three roles; the MDM ingress is a fourth
  control-plane component (§15.5)
- `docs/03-security-model.md` — trust boundaries, the signed-job invariant, the
  threat model §1.5 extends
- `docs/04-data-model.md` — core entities; `devices` is reused, not forked
- `docs/kernel/spec.md` — §5 tenancy/RLS, §7 agent enrollment (untouched), §8
  gateway identity (mirrored), §9 the job engine (not used), §12 frontend
  conventions
- `docs/kernel/vault.md` — envelope encryption for every credential in §4
- `docs/modules/site-server/spec.md` — the precedent for stating an invariant
  conflict plainly, writing the amendment out, and attaching it to the slice
  that cannot be built without it (§0.3, §4.4)
- `docs/modules/service-desk/spec.md` §0.1 — the precedent for a spec that
  reverses a stated non-goal, and for amending the vision doc in the same commit
- `docs/modules/provisioning/spec.md` — macOS as `enrollment` rather than
  `image`; vault-sealed render-time secrets; pre-enrollment targets with no
  `Device` row
- `docs/modules/inventory/spec.md` — the producer contract §2.11 refuses to
  violate
- `docs/modules/endpoint-health-alerting/spec.md` — `unavailable` as a
  first-class state (§7.2's discipline), rate fuses (§8.2 clause 7), and the
  approved-action registry §1.6 prohibition 6 relies on
- `docs/modules/agent/open-questions.md` — why macOS needs MDM-delivered
  TCC/PPPC profiles (§2.5)
- `docs/modules/patch/spec.md` §342 — the macOS update-enforcement gap this
  module is expected to close, subject to 16.6
