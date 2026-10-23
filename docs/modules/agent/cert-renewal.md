# Kernel Spec — agent certificate renewal

> Closes **OQ-1** (`docs/modules/agent/open-questions.md`), which was raised by
> the agent build and deliberately left unimplemented because a renewal path is
> a new credential path into the fleet's identity system.
>
> Platform-kernel work, not a business module. It adds two job types, one
> agent capability package, and a state machine that runs on the gateway's
> connect path. It adds **no new authenticated surface**, **no second
> server→agent instruction path**, and **no database column**.
>
> This is the design OQ-1 recommended — option **(b)**, renewal as a signed
> job — with the two-leg shape it sketched and the atomicity problem it flagged
> ("the old identity must remain usable until the new one has connected
> successfully, or a failed renewal bricks the endpoint") solved explicitly in
> §4.

---

## 0. Resolved design decisions

Nine decisions. Each rules out a family of designs, so they are recorded before
the design rather than inside it.

### 0.1 Certificates are 365 days, not 30

`ttl_days` defaults to **365** (`app/core/ca.py::AGENT_CERT_TTL_DAYS`). The
product owner's decision, and it is the right one now that rotation is
automatic: a 30-day certificate buys nothing once renewal is a routine
background event, and it makes every rotation defect fire twelve times a year
instead of once.

The internal CA root is 3650 days and the gateway's server certificate is 365;
365 for the agent leaf puts it on the same footing as the server it dials.

### 0.2 Rotation happens at two-thirds of life, NOT on expiry

The instruction as first given was "rotated once expired". **That cannot be
built, and this spec does not build it.**

The certificate *is* the agent's mTLS credential. `GatewayServer._handle`
resolves the connection to an `Agent` row by `sha256(DER(leaf))` and closes
**4401** when `agent.cert_not_after < now`, before a single byte of application
protocol is read. Once the certificate has expired there is no TLS handshake,
therefore no WebSocket, therefore no channel down which a replacement could be
pushed. A renewal job dispatched to an expired agent is a job nobody can
deliver.

So renewal must happen **while the old certificate is still valid**, and the
window has to be wide enough that an endpoint which is merely switched off for
a while still lands inside it.

**Rotation begins when less than one third of the TTL remains** — day 243 of
365, i.e. `cert_not_after - now <= 122 days`
(`app/services/cert_renewal.py::RENEWAL_REMAINING_DAYS`). It is **re-attempted
on every connect** until it succeeds. A laptop off for a month, a VM suspended
for a quarter, a site behind a broken WAN link for ten weeks — all of them come
back inside the window and rotate on their first connect, with no operator
action and no scheduled sweep.

An endpoint offline for the whole remaining 122 days misses the window and is
**re-deployed**. That is the owner's stated fallback, and it is reached
honestly: the machine was unmanaged for four months.

> **Do not "fix" this back to on-expiry.** The failure it produces is not a
> failed rotation, it is a fleet that cannot be recovered over the network.

### 0.3 Renewal is a signed job, in two legs — and the private key never moves

OQ-1's option (b), unchanged. Two round trips over the path that already
exists:

| Leg | Direction | Carries |
|---|---|---|
| `agent.cert.renew` | server → agent | nothing (`{}`) |
| ↳ its `job_result` | agent → server | `csr_pem` |
| `agent.cert.install` | server → agent | `cert_pem`, `chain_pem`, `cert_serial` |
| ↳ its `job_result` | agent → server | `cert_fingerprint`, `not_after` |

Both legs are ordinary `JobEnvelope`s. The agent verifies each one exactly as
it verifies any other job — Ed25519 signature over canonical bytes against the
key pinned at enrollment, target device, freshness, nonce, known type, in that
order — before anything runs. **Invariant #1 gets no exemption for the job type
whose subject is the agent's own identity**; if anything, that is the job type
that most needs it.

**The private key is generated on the endpoint at renewal exactly as at
enrollment, and it never leaves.** Only the CSR travels. A design in which the
server generates a keypair and ships it down in a job payload is a different
and much worse design: it puts every endpoint's private key in the control
plane's memory, its logs, and its job table.

Rejected alternatives, both from OQ-1 and both still rejected:

- **(a) An mTLS-authenticated `POST /enroll/v1/renew`.** The API listener does
  not terminate client certificates; only the gateway does. Making it do so
  means a second place where "the certificate is the identity" has to be
  implemented correctly, plus a deployment change to stop the reverse proxy
  terminating TLS. A large security-critical surface for one endpoint.
- **(c) A new server→agent message kind.** There is exactly one server→agent
  kind today (`protocol.py`), which is what makes "everything the server tells
  the agent to do is signed" a structural fact rather than a convention. A
  second kind needs its own verification story, and the obvious one is "sign
  it" — at which point it is (b) with a bespoke frame.

### 0.4 Exactly one certificate is accepted at a time, and the overlap is held on the ENDPOINT

Between issuance and confirmation both certificates exist. Something has to
decide which one authenticates.

**The `agents` row is the single gate, and it holds exactly one fingerprint.**
It flips to the new certificate at one instant: when the `agent.cert.install`
result commits (§4.3). Before that instant only the old certificate is
accepted; after it, only the new one.

The overlap is resolved **on the endpoint**: the agent keeps the certificate it
replaced in `previous.json`, uses the new one on the next dial, and **reverts to
the previous one if the gateway closes it 4401** (§4.4). The endpoint is where
the ambiguity is observable — it is the only party that knows whether its
install succeeded — and it is the party that gets locked out if the guess is
wrong, so it is the party that should hold the fallback.

Rejected: **the gateway accepting two fingerprints during the overlap.** There
are only two ways to do it and both are worse.

- *A second column on `agents`* (`pending_cert_fingerprint`). This is the clean
  version and it is **not available to this change**: it needs an Alembic
  migration, and migration rights are held elsewhere. Recorded here as the
  thing to revisit if a migration is ever taken for this area — it would let
  the gateway answer "is this you?" without the endpoint-side revert, at the
  cost of a second admission path to keep correct.
- *Resolving identity from the in-flight `agent.cert.install` job row.* No
  migration needed, and rejected on merit: it makes **the certificate that
  authenticates you depend on a row in `jobs`**. Job rows expire (`ttl_s`
  defaults to 3600), get re-signed for retry, and are written by the job engine
  for reasons unrelated to identity. An admission decision that a job's TTL can
  silently revoke is not an admission decision.

### 0.5 The server never learns which device it is signing for from the agent

The CSR arrives inside a `job_result`. Everything about *who it is for* comes
from the **job row**, which the server wrote:

```python
issued = await ca.sign_csr(csr_pem, device_id=job.target_id, org_id=job.org_id, ...)
```

`job.target_id` and `job.org_id` were chosen by the server when it created the
renewal job for a specific `Agent`. The message that carries the CSR reaches
this code only via `_load_job_for_device`, which discards any `job_result`
whose `job_id` does not belong to the device the **mTLS certificate** resolved
to — and audits the attempt as `gateway.identity_mismatch`.

So a connected agent cannot obtain a certificate for another device or another
org: it would have to name a job that is not its own, and that message is
dropped before the handler sees it. The CSR's own subject is ignored entirely
(`CAService.sign_csr` overwrites CN and O), which is already true at
enrollment.

Additional checks before signing (§3.2): the CSR signature must be valid, the
key must meet `_require_strong_key`, and the `Agent` row must not be revoked. A
revoked agent is never issued a fresh certificate — that would be revocation
undone by a background process.

### 0.6 Rotation is driven from the gateway's connect path, not from a sweep

OQ-1 assumed "a server-side sweep over `agents.cert_not_after`". A sweep is a
worse fit: it dispatches to devices that are not connected (the job sits queued
and expires), it needs its own schedule and its own audit story, and it does
nothing for the machine that comes back after two months — which is the case
§0.2 exists for.

The gateway already loads the `Agent` row at every connect and already holds
`cert_not_after` in memory. The rotation check is therefore **free**: an integer
comparison on a value already fetched, on a code path that runs exactly when the
device can actually receive the job. Only when rotation is due does it touch the
database at all.

**`agents.cert_not_after` was stored and read by nothing.** It now drives
rotation, and it is exposed to operators on the device API (§6).

### 0.7 One in-flight rotation per device, and a failed leg discards the CSR

The state machine (§3.1) refuses to start a second rotation while any
`agent.cert.renew` or `agent.cert.install` job for that device is
queued/dispatched/running. Two concurrent rotations would race to flip the same
row.

A CSR is considered **consumed** the moment an `agent.cert.install` job exists
that was created after it — *whatever that job's outcome*. So an install that
fails (bad payload, the agent lost its pending key, a crash mid-swap) does not
cause the same CSR to be signed again forever; the next connect asks for a fresh
one. That is the whole loop-prevention argument, and it is why the check is
"does a later install job exist" rather than "did an install job succeed".

### 0.8 Certificates already issued at 30 days rotate on their next connect

Changing the default `ttl_days` affects **newly issued certificates only**. A
certificate already in the field keeps the `NotAfter` it was signed with.

That needs no special handling, because of how §0.2's threshold is written: it
is `remaining <= 122 days`, and a 30-day certificate never has more than 30 days
remaining. **Every already-issued agent certificate is inside the rotation
window by definition** and rotates to a 365-day certificate on its first connect
after this change is deployed — provided it connects before its own 30 days are
up. One that has already expired is re-deployed; nothing can reach it.

### 0.9 The expiry warning stays, and its remedy changes

`enroll.LogExpiry` (Task 6) still writes WARN in the last third of life and
ERROR past `NotAfter`. It is no longer the only thing standing between the fleet
and a cliff, so the message changes: the WARN now says rotation is expected and
automatic, and names re-deployment only in the ERROR case, where it is in fact
the remedy. A log line that tells an operator to re-enroll a machine that is
about to renew itself is a line that trains them to ignore it.

---

## 1. What changes on the wire

Four new schemas under `docs/kernel/wire-protocol/`, exported from the Pydantic
models as always:

| Schema | Direction | Shape |
|---|---|---|
| `AgentCertRenewParams` | server → agent | `{}` — `extra="forbid"` |
| `AgentCertRenewResult` | agent → server | `{ csr_pem }` |
| `AgentCertInstallParams` | server → agent | `{ cert_pem, chain_pem, cert_serial }` |
| `AgentCertInstallResult` | agent → server | `{ cert_fingerprint, not_after }` |

**This is a compatibility decision, in both directions**
(`docs/kernel/wire-protocol/README.md`):

- **New control plane → old agent.** Purely additive. An agent built before this
  change has no `agent.cert.renew` in its capability registry, so
  `Verifier.Verify` refuses the job `unknown_type` **before anything runs** —
  the correct behaviour, already implemented and already tested. The job is
  recorded `refused`; the old certificate is untouched; the agent keeps working
  and keeps warning exactly as it did. It will need re-deployment before its own
  `NotAfter`, which is what it needed anyway. Backed by
  `test_an_agent_that_does_not_know_the_job_type_is_refused_and_keeps_its_certificate`
  (server) and the pre-existing
  `TestAJobTypeThisBuildDoesNotHaveIsRefusedAndNothingRuns` (agent), which
  asserts the refusal is the terminal state and no capability was reached.
- **New agent → old control plane.** An old control plane never dispatches
  either job type, so the capability is never invoked and the agent behaves
  exactly as it did before: it dials, it works, and it warns in the last third
  of its life. Nothing in the agent initiates rotation, so there is no request
  an old server would answer with a 404 or a validation error. Backed by
  `TestAnAgentAgainstAServerThatNeverRotatesIsUnchanged`.

**Payload sizes**, checked rather than assumed. An Ed25519 CSR is ~230 bytes of
PEM; a leaf + root chain is ~1.3 KB. `agent.cert.install`'s envelope is
therefore ~2 KB against the agent's 1 MiB `SetReadLimit`, and the `job_result`
carrying a CSR is ~400 bytes against `websockets`' 1 MiB default frame limit.
Neither bound is near.

Neither job type is dispatchable through `POST /api/v1/jobs`: both are added to
`MODULE_GOVERNED_REASONS` (`app/jobs/direct_dispatch.py`), whose test asserts a
**partition** over the live registry, so a new job type cannot quietly become
operator-dispatchable. An operator-authored `agent.cert.install` would be a way
to push an arbitrary certificate onto an endpoint, which is the whole attack
this design otherwise closes.

---

## 2. Job types

Registered in `app/jobs/registry.py` alongside the other kernel job types.

```
agent.cert.renew    params: AgentCertRenewParams    result handler: none
agent.cert.install  params: AgentCertInstallParams  result handler: confirm_installed_cert
```

`agent.cert.renew` needs no result handler: the kernel already stores a
succeeded job's result in `jobs.result`, and that is where the CSR waits until
the next pass of the state machine reads it.

---

## 3. Server side

### 3.1 The state machine

`app/services/cert_renewal.py::advance_renewal(session, agent, ca, job_engine,
now)`, run inside the connection's tenant session. It returns the `Job` it
created, or `None`.

```
0.  agent.cert_revoked            -> None      (revocation is not undone here)
1.  not renewal_due(not_after)    -> None      (more than 122 days remain)
2.  an open renew/install job     -> None      (§0.7: one rotation at a time)
3.  an unconsumed succeeded CSR   -> sign it, create agent.cert.install
                                                audit agent.cert.issued
    ...and if signing raises      -> audit agent.cert.rotation_failed,
                                     create a fresh agent.cert.renew
4.  otherwise                     -> create agent.cert.renew
                                                audit agent.cert.renewal_requested
```

"Unconsumed" is §0.7's rule: the newest **succeeded** `agent.cert.renew` job,
with no `agent.cert.install` job created after it. Ordering uses `jobs.id`,
which is a UUIDv7 and therefore chronological.

Called from two places, both after the tenant transaction commits:

- **`GatewayServer._handle`, at connect**, immediately before `_drain_queued` —
  so a job created here is drained down the connection that triggered it, with
  no extra publish and no second round trip.
- **`handle_agent_message`, after a cert-renewal `job_result` commits** — so the
  second leg follows the first within the same connection rather than waiting
  for the next one. Published with `publish_job`, exactly like any other
  dispatch.

### 3.2 What is verified before the CA signs

| Check | Where |
|---|---|
| The reporting device owns the job | `_load_job_for_device` (existing) |
| `device_id`/`org_id` come from the job row, never the CSR | `advance_renewal` (§0.5) |
| The CSR's own signature is valid | `CAService.sign_csr` (existing) |
| The key is strong enough to be an mTLS identity | `_require_strong_key` (existing) |
| The agent is not revoked | `advance_renewal` step 0 |

### 3.3 Confirming the install

`confirm_installed_cert(session, job, result)` is the `agent.cert.install`
result handler. It runs inside the job's tenant transaction, and it:

1. re-derives the expected fingerprint from `job.params["cert_pem"]` — the
   server's own bytes, never the agent's claim;
2. **fails the job** if `result["cert_fingerprint"]` disagrees, leaving the
   `agents` row on the old certificate;
3. otherwise writes `cert_fingerprint`, `cert_serial` and `cert_not_after` from
   the certificate the server issued, and audits `agent.cert.rotated` with both
   the old and the new fingerprint and serial (invariant #4).

A raising result handler is converted to a failed job by `JobEngine.complete` —
so a mismatch is visible as a failed job and an unchanged certificate, never as
a silent half-rotation.

### 3.4 Audit (invariant #4)

Issuing or rotating a certificate is a privileged action, so all four states are
in the hash-chained log:

| Action | When |
|---|---|
| `agent.cert.renewal_requested` | a `agent.cert.renew` job is created |
| `agent.cert.issued` | the CA signs a CSR; carries serial, fingerprint, `not_after` |
| `agent.cert.rotated` | the agents row flips; carries old and new fingerprint/serial |
| `agent.cert.rotation_failed` | a CSR could not be signed; carries the reason |

`enrollment.device_enrolled` already covers the first issuance.

### 3.5 The old certificate stops being accepted at exactly one instant

**When the transaction containing `confirm_installed_cert` commits.** From that
commit, `agents.cert_fingerprint` holds the new value; the old fingerprint
resolves to no `Agent` row, and a connection presenting it is closed **4401**.

Nothing else ends the old certificate's acceptance:

- Not its own `NotAfter` — which is still ~122 days away, and is why §0.4's
  endpoint-side revert has something to revert to.
- Not CRL revocation. The gateway's admission gate is the fingerprint in the
  database, not the CRL; the superseded serial is deliberately **not** pushed to
  the CA's revocation list, because `revoke_agent`'s CRL path is about
  retiring a *device*, and reusing it here would conflate "this machine is out"
  with "this machine renewed".

---

## 4. Agent side

### 4.1 The two capabilities

`agent/internal/capabilities/cert` holds both handlers. They are the first
capabilities with **process state** — the data directory — and that is resolved
the way everything else in the agent resolves it: `cert.Process` calls
`config.Load()` **at job time**, reading the same environment the service
manager started the process with.

Not at package init, and not through a registry constructor taking dependencies.
The registry stays what it is: one list, built once, immutable, and a pure map
insert that opens nothing. Both types are registered **unconditionally** — an
agent that advertised renewal only when some precondition held would be an agent
whose certificate silently stops rotating, which is the failure this design
exists to remove.

`AgentCertInstallParams.cert_serial` has **no field in the endpoint's struct**.
It is there for the control plane's own bookkeeping, and unknown members must
stay ignorable or the server can never add one.

- **`agent.cert.renew`** generates a fresh Ed25519 keypair, writes the private
  half to `agent.key.pending` (0600, inside the 0700 data directory), builds a
  CSR with an **empty subject** (the CA overwrites CN and O; a hostname here
  would read as meaningful and be ignored), and returns `{csr_pem}`. It is a new
  keypair every time: renewal that reuses the old key renews the certificate and
  not the identity.
- **`agent.cert.install`** parses `cert_pem`, then refuses unless **all** of:
  - the leaf's public key is exactly the pending key's public key — this is what
    stops a certificate issued over somebody else's key being installed;
  - the leaf's CN is this agent's `device_id`;
  - the leaf chains to the **CA bundle pinned at enrollment**, with
    `ExtKeyUsageClientAuth`. A control plane cannot hand this endpoint an
    identity from a different root; if the root ever genuinely changes, that is
    a re-deployment, which is the same answer pinning has always given.

  Only then does it perform the swap in §4.2 and return
  `{cert_fingerprint, not_after}`.

### 4.2 The swap is a transaction with a recovery rule

An endpoint that loses power between writing the new key and writing the new
certificate must not come back with a key and a certificate that do not pair —
`enroll.build` would refuse it and the agent would not start. So:

```
1. write previous.json   {cert_pem, chain_pem, key_pem}  <- the CURRENT identity
2. write rotation.json   {cert_pem, chain_pem}           <- commit marker
3. write agent.key       <- agent.key.pending
4. write identity.json   <- the new cert and chain
5. remove rotation.json, then agent.key.pending
```

Every write is `writeFile`'s atomic temp-and-rename. `rotation.json` is the
commit marker, and it is removed **before** the pending key, so
`rotation.json exists ⇒ agent.key.pending exists`.

`enroll.RecoverRotation`, called from `LoadOrEnroll` before anything is loaded:

- no `rotation.json` → nothing to do (the overwhelmingly common case, one
  `os.Stat`);
- `rotation.json` and a pending key → redo 3–5, idempotently;
- `rotation.json` and no pending key → the invariant is broken (disk damage);
  restore from `previous.json` and delete the marker. **Fail back to the
  certificate that was working**, never forward to one that might not be.

### 4.3 Which certificate the agent dials with

`transport.Options` gains `ClientCertFn func() (tls.Certificate, error)`,
consulted on **every dial**; `ClientCert` remains and is used when it is nil, so
a nil hook is the previous behaviour exactly. The agent's hook re-reads the
identity from disk, which is what makes a mid-connection rotation take effect on
the next reconnect without a process restart.

The live connection is **not** re-keyed: it was authenticated at handshake time
and the gateway does not re-check. The agent finishes the job, sends the result
on the old connection, and picks up the new certificate whenever that connection
next drops.

### 4.4 The revert, and why 4401 stops being unconditionally terminal

The dangerous case is narrow and real: the agent installs, sends the result, and
the connection dies before the server commits it. The endpoint now holds a
certificate the control plane has never heard of, and 4401 is terminal.

`transport.Options` gains `OnUnauthorized func(context.Context) bool`. On a 4401
close, if the hook is set and returns true, the loop **reconnects instead of
terminating**. The agent's hook returns true exactly once per rotation: if
`previous.json` exists it restores that certificate and key, deletes
`previous.json`, and returns true. With no `previous.json` it returns false and
4401 is terminal exactly as before.

That is bounded by construction — a revert deletes the only thing that permits
another one — so the anti-storm property OQ-1 insisted on survives: a fleet
whose certificates have genuinely expired still stops dialling, because the
previous certificate has expired too and the second 4401 is terminal.

Revocation is not reachable from here. `OnUnauthorized` fires only on **4401**,
never on 4403, and the previous certificate's fingerprint is not in `agents` at
all — so a revoked agent that reverts is refused again and stops. One extra
dial, no changed verdict, asserted by `TestAFourFourZeroThreeIsNeverRecoverable`.

**`previous.json` is superseded, not cleared on connect** — and this is a
correction to the obvious design, made because the obvious one bricks the
endpoint. The tempting rule is "delete it on the first successful connect".
There is no such moment available: the gateway completes the WebSocket upgrade
**before** it resolves the fingerprint, so a dial that is about to be closed
4401 looks exactly like a good one at `OnConnect` time. Deleting the fallback
there would consume it precisely in the case it exists for. So `previous.json`
is overwritten by the next rotation's step 1 and deleted by a revert, and
nothing else touches it.

The cost is one superseded private key sitting in the data directory until the
next rotation. That is not a new exposure: the *live* key sits beside it under
the same 0700 directory and 0600 file modes, so anything that can read one can
read both, and the superseded certificate expires on its own schedule.

### 4.5 Idle budget

Nothing here is resident. The capabilities are map entries until a verified job
arrives — `cert.Process` resolves even its *configuration* at job time rather
than at package init, so building the registry stays a pure map insert. The
recovery check is one `os.Stat` at startup. The per-dial certificate read
happens on reconnects, which are rare by construction. No timer, no goroutine,
no allocation while idle, and `TestExpiryStateAllocatesNothing` and
`TestLookupAllocatesNothing` still hold.

---

## 5. Failure modes, accepted

| Failure | What happens | Recovery |
|---|---|---|
| Old agent does not know the job type | job `refused`, old certificate untouched | none needed; re-deploy before `NotAfter` |
| CSR malformed or key too weak | `agent.cert.rotation_failed` audited, fresh renew job | automatic, next connect |
| Install job never delivered | job expires; CSR consumed at the next pass | automatic, next connect |
| Agent crashes mid-swap | `rotation.json` recovery (§4.2) | automatic, next start |
| Install succeeded, result lost | agent reverts on 4401 (§4.4), rotation restarts | automatic, one extra dial |
| Fingerprint mismatch in the result | job `failed`, `agents` row unchanged | automatic, next connect |
| Gateway has no CA access | rotation fails and is audited every connect | operator: the gateway process needs the CA (it already mounts `its-ca`) |
| **Offline for the whole 122-day window** | certificate expires; 4401; device offline | **re-deploy the agent** — the owner's stated fallback |

The last row is the only one that needs a human, and it is the one the owner
accepted.

---

## 6. What an operator sees

**Before this change**, `agents.cert_not_after` was stored and read by nothing:
a device went offline on day 30 and the gateway logged `unrecognized or revoked
certificate`, indistinguishable from a revocation.

**Now:**

- `GET /api/v1/devices` and `GET /api/v1/devices/{id}` carry `cert_not_after`
  and `cert_state` ∈ `{ok, renewing, expiring, expired}` — `renewing` is the
  rotation window (§0.2), `expiring` is the last tenth, `expired` is past
  `NotAfter`. A fleet-wide view of what is approaching expiry is now a list
  filter rather than a database query.
- **Before a failed rotation**: `agent.cert.renewal_requested` in the audit log,
  and a `agent.cert.renew` or `agent.cert.install` job on the device's job list.
- **After a failed rotation**: the job is `failed` or `refused` with its reason,
  `agent.cert.rotation_failed` is in the audit log when the CA refused the CSR,
  and `cert_state` stays `renewing` — the certificate has not changed and the
  next connect tries again. `cert_not_after` is the deadline for that retrying
  to matter; once it passes, the device is re-deployed.
- The endpoint's own log still carries the WARN/ERROR from `LogExpiry` (§0.9).

---

## 7. Not built here

- **A second admission fingerprint on `agents`** (§0.4). Needs a migration.
- **CRL revocation of superseded serials** (§3.5). The gateway does not consult
  the CRL; adding it is a separate decision about admission, not about renewal.
- **An operator-triggered "rotate now"**. The state machine already re-attempts
  every connect, and a manual trigger would be a third caller of it with its own
  authorization story. Revocation plus re-deployment remains the answer for a
  compromised endpoint, which is a different problem.
- **Renewal of the gateway's own server certificate.** Out of scope; it is
  pinned by every agent's CA bundle and is a deployment artefact.
