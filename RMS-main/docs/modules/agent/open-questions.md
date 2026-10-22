# Agent — open questions

Questions raised by building the agent that the agent must not answer on its
own. Each one names what was found, what it costs, and what would have to
happen before it can be closed.

---

## OQ-1 — certificate renewal does not exist (blocking, 30-day fuse)

**Status: CLOSED** by `docs/modules/agent/cert-renewal.md`, which is the
spec/build cycle this question asked for. It takes the recommendation below —
option **(b)**, renewal as a signed job — and settles the two things this
write-up left open:

- **Certificates are 365 days, and rotation happens at two-thirds of life
  (day 243), re-attempted on every connect.** Not "once expired": an expired
  certificate is the credential the push would have travelled over, so there
  would be no channel left to push a replacement down. See that spec's §0.2.
- **The atomicity worry in option (b)'s *Against* column** — "the old identity
  must remain usable until the new one has connected successfully, or a failed
  renewal bricks the endpoint" — is answered in its §4.2 and §4.4: the swap is
  a transaction with a recovery marker, and the endpoint keeps the certificate
  it replaced and reverts to it once if the gateway closes it 4401.

Everything below is the record as it stood when the question was raised, kept
because the three candidate designs and the reasoning that chose between them
are the review this platform's identity path is owed. **The "What an operator
sees today" and "Until then" sections are historical and no longer describe the
system.**

*Original status:* open, blocking. Raised by Task 6. **No renewal path was
built.**

### The facts

| Fact | Where |
|---|---|
| Agent client certificates are issued with `ttl_days=30` | `backend/app/core/ca.py::CAService.sign_csr` |
| There is no renewal endpoint, no rotation job type, and nothing named `renew` outside licensing | `grep -rn renew backend/app` returns only license renewals |
| The only path that issues an agent certificate is `POST /enroll/v1` | `backend/app/api/v1/enrollment.py:144` |
| That path **consumes** an enrollment token, and tokens are use-capped and expire | `backend/app/services/enrollment.py::enroll` — one guarded `UPDATE … use_count + 1`; zero rows affected = 403 |
| An expired certificate is closed **4401** at the gateway, indistinguishably from a revoked one | `backend/app/gateway/main.py:183-186` (`agent.cert_not_after < now`) |
| `docs/05-agent-spec.md` describes agent certificates as "short-lived and **auto-rotated**" | the second half is unimplemented |

### The consequence, stated plainly

**Every agent deployed by this plan stops connecting thirty days after
enrollment.** It cannot re-enroll itself: the token it used is spent, and a
fresh token requires an operator with `enrollment.manage`. What the operator
sees is a device that goes offline and a gateway log line that reads
`unrecognized or revoked certificate` — the same message a genuinely revoked
agent produces. Nothing in either test suite goes red, because no test runs a
clock forward thirty days.

This is not a defect in the agent. It is a missing piece of the platform
kernel, and it is the single highest-priority follow-on to the agent work.

### What Task 6 did instead of improvising

`CLAUDE.md`: *"If a task seems to require violating an invariant … stop and
raise it rather than working around it."* A renewal endpoint is a **new
authenticated path into the fleet's identity system**. Designing one inside an
implementation task — where the pressure is to make the tests pass — is how a
platform acquires a credential path nobody reviewed.

So the agent was made *aware* of the fuse and nothing more:

- `agent/internal/enroll/lifecycle.go` — `ExpiryState(now, id) (remaining, state)`
  over the parsed leaf, `state ∈ {ok, expiring, expired}`. `expiring` begins
  when less than **one third of the certificate's own lifetime** remains (day 20
  of 30), derived from `NotAfter - NotBefore` rather than from a hardcoded 30,
  so a change to `ttl_days` moves the threshold with it.
- `LogExpiry` writes **WARN** while `expiring` and **ERROR** while `expired`,
  naming the exact `NotAfter` and the remedy. It is silent while `ok`.
- Both run at **connect** (`transport.Options.OnConnect`) and will run on the
  daily scheduler tick (Task 9) — no timer of their own, zero allocations per
  call, asserted by `TestExpiryStateAllocatesNothing`.

**There is deliberately no attempt to tell the control plane out of band.**
`JobLogMsg` requires a `job_id` bound to a real job for this device
(`dispatch.py::_load_job_for_device`), so no unsolicited log channel exists.
Inventing one would be a second agent-initiated path with no verification
story. The surfaces that exist are the endpoint's local log and the operator's
device list, which already stores `agents.cert_not_after`.

### What an operator sees today as day 30 approaches

1. **Days 1–19:** nothing. The certificate is `ok` and the agent is silent
   about it, on purpose.
2. **Day 20 onward:** one WARN per gateway connect in the endpoint's local log,
   naming `not_after`, the remaining time, and the remedy. **This is only
   visible on the endpoint** — it does not reach the console.
3. **`agents.cert_not_after` is already stored and already queryable.** It is
   the one server-side surface that can be turned into a fleet-wide view
   without any new wire contract, and no view currently reads it. *That is the
   cheapest partial mitigation available and it is not built here either.*
4. **Day 30:** the gateway closes the connection **4401**. `transport.Run`
   treats 4401 as terminal and stops reconnecting — deliberately, so a fleet
   whose certificates all expire on the same day does not become a retry storm
   against the control plane. The process exits 4; the service manager restarts
   it; it fails the same way. The device shows **offline**, and after
   `offline_after_s` health opens an `agent.offline` alert. The alert is
   correct; its stated cause will not mention certificates.
5. **Recovery** is manual: issue a fresh enrollment token, clear the device's
   data directory, re-enroll. That mints a **new Device row**, so history,
   group membership and policy assignment do not follow the machine.

### The three candidate designs

**(a) An mTLS-authenticated `POST /enroll/v1/renew` on the API listener.**
The agent presents its current (still-valid) certificate and a CSR over a fresh
keypair; the CA issues a new certificate bound to the same `device_id`.

- *For:* conceptually the smallest thing; one round trip; well-trodden ACME-ish
  shape.
- *Against:* **the API listener does not terminate client certificates today.**
  Only the gateway does (`GatewayServer.start` builds the `ssl.SSLContext` with
  `CERT_REQUIRED`). Making the API do so means a second mTLS terminator, a
  second place where "the certificate is the identity" has to be implemented
  correctly, and a deployment change (the reverse proxy in front of the API
  must pass the client certificate through, or stop terminating TLS). That is a
  large, security-critical surface for one endpoint.

**(b) `agent.cert.renew` as a signed job — RECOMMENDED.**
Two round trips over the path that already exists:

1. The control plane dispatches a signed `agent.cert.renew` job carrying no
   parameters. The agent verifies it exactly like any other job (signature over
   canonical bytes against the pinned key, target match, freshness, nonce), then
   generates a keypair and returns a CSR in the `job_result`.
2. The control plane signs the CSR and dispatches a second signed job carrying
   the issued certificate. The agent verifies that job, swaps its identity, and
   reconnects.

- *For:* **adds no new authenticated surface.** It reuses the signed-job path
  verbatim, so the verification story is the one already written and already
  tested. The dispatch is server-initiated, so renewal timing is an operator
  policy (a sweep over `agents.cert_not_after`) rather than agent behaviour.
  Both legs are audited by the existing job audit records. It composes with the
  scheduler the agent already needs.
- *Against:* two round trips; needs a server-side sweep to dispatch it; the
  second leg carries a certificate in a job payload, so the payload size bound
  and the `job_result` size bound both need checking; and the swap-and-reconnect
  step needs care to be atomic (the old identity must remain usable until the
  new one has connected successfully, or a failed renewal bricks the endpoint).
  It also introduces the only job type whose effect is on the agent's own
  identity rather than on the host, which deserves its own review.

**(c) Renewal over the gateway connection as a new message kind.**
A new server→agent frame alongside `JobEnvelope`.

- *For:* one connection, no new listener, immediate.
- *Against:* **this is a second server→agent instruction path**, and today there
  is exactly one (`protocol.py:150-153`) precisely so that "everything the
  server tells the agent to do is signed" is a structural fact rather than a
  convention. A new kind would need its own verification story before it could
  be considered at all, and the obvious one is "sign it" — at which point it is
  option (b) with a bespoke frame. It also breaks the wire contract in the
  direction with no precedent: a new agent against an old server, and an old
  agent against a new server that now sends a kind it will reject.

### Recommendation

**Option (b).** It is the only one that adds no new authenticated surface and no
new instruction path.

It **needs its own brainstorming/spec cycle**, not a task in this plan. In
particular it changes `docs/kernel/wire-protocol/` (a new job type and its
params/result schemas), so it is a **compatibility decision**: additive for the
server→agent direction (an old agent meets an unknown job type and refuses it,
which is the correct behaviour and already implemented), and it must be safe for
a **new agent against an old server**, which will never dispatch the job — so
the agent must keep working, and keep warning, exactly as it does today.

### Until then

- Certificates are 30 days and expiry is manual. Any pilot longer than a month
  needs an operator runbook.
- The cheapest interim mitigation that requires no new wire contract is a
  **console view or alert over `agents.cert_not_after`**, which is already
  stored. It is not built.
- `transport.Run` treating 4401 as terminal is what stops this becoming a
  fleet-wide retry storm on the day the first cohort expires. Do not "fix" that
  by retrying.

---

## OQ-2 — the agent emits `attributes.ip` and `attributes.addresses[]`, and dropping `ip` is a fleet decision

**Status: CLOSED 2026-08-05 — `attributes.ip` removed from both sides.** Raised
by Task 11.

The entry below is kept rather than deleted: it is the reasoning that made the
dual path correct while it existed, and the reasoning that says what would have
to be true before anything like it is added back. A closed question with its
argument deleted is how the same design gets re-litigated from scratch.

### The decision

The product owner confirmed that **the platform is not live and no agent or
control plane is deployed anywhere**. Both conditions under "The condition for
removal" below are therefore satisfied vacuously — there is no deployed control
plane that reads only `ip`, and no minimum agent version to be past, because
there is no fleet.

That removes the *only* thing the dual path bought. What was left was two code
paths for one fact, and two implementations of one thing drift: this repository
has already paid for that shape twice, in the address grid computing
`conflicting` differently from the drift page, and in the one-implementation
rule `scope_digest` needed for exactly the same reason
(`backend/app/modules/ipam/net.py`, property 1).

**What changed, in one commit:**

| Side | Change |
|---|---|
| Agent | `nicComponentFrom` no longer sets `attributes["ip"]`; `legacyIPv4` deleted |
| Server | `observations_from_components` no longer reads `attributes.get("ip")` |
| Agent test | `TestBothTheLegacyKeyAndTheBlockArePresent` and `TestTheLegacyKeyHoldsAnAddressAnOldServerWouldActuallyKeep` replaced by `TestTheLegacyIPKeyIsNotEmitted` |
| Server tests | every fixture reseeded into §14.1's block; the three tests whose subject was the two keys' *interaction* deleted, replaced by `test_the_removed_legacy_ip_key_is_not_read` |
| Spec | §4.1 and §14.1 no longer describe a fallback |

**What did NOT change: the parser's totality.** `nic.py` still degrades to "no
observation" rather than raising, for every malformed input, and the tests that
pin that are untouched — a raising result handler fails the whole
`inventory.collect` job (`JobEngine.complete`), and IPAM must not be able to
break Phase 1. Removing a *key* is not removing the *tolerance*.

**The removal exposed a class of test that removal makes vacuous rather than
red**, and it is worth recording because the next key-removal will meet it
again. A fixture seeding `{"ip": "127.0.0.1"}` and asserting "no observation"
goes on passing after `ip` stops being read — but it is then observing that
nothing was read, not that `is_documentable` discarded anything, and it would
keep passing with the discard rule deleted. Those fixtures were rewritten into
the shape the parser actually reads, not left green. See
`backend/tests/test_ipam_nic.py`'s module docstring.

**If a compatibility key is ever needed again**, it is a wire-protocol decision
under `docs/kernel/wire-protocol/README.md` — versioned and reviewed — not a
line added back to either file. The argument for why is preserved verbatim
below.

### The facts (as they stood when this was raised)

| Fact | Where |
|---|---|
| The agent emits BOTH `attributes.ip` and §14.1's `attributes.addresses[]` for every NIC | `agent/internal/capabilities/inventory/nic.go` |
| `app/modules/ipam/nic.py` reads both, deduplicated on `(address, mac)` — as of the same commit | `backend/app/modules/ipam/nic.py` |
| Every control plane **older** than that commit reads **only** `attributes.ip` | the previous revision of that file |
| That parser is **total by design**: it returns `[]` for anything it does not understand and never raises | `nic.py`'s docstring, `tests/test_ipam_nic.py` |
| `ComponentDoc.attributes` is `dict[str, Any]` / `additionalProperties: true`, so **this wire change produces no schema diff** | `docs/kernel/wire-protocol/InventoryDocument.schema.json`, unchanged by `scripts/export_wire_schema.py` |

### Why `ip` was emitted at all

An agent that emitted `addresses[]` and dropped `ip` would produce **zero**
observations against any control plane that had not yet learned the new key.
Not an error, not a partial answer — the entire passive reconciliation stream
silent, because the parser degrades to "no observation" rather than raising.
Nothing goes red on either side, no alert fires, and the symptom is an IPAM
address plan that quietly stops learning anything from inventory.

`docs/kernel/wire-protocol/README.md`'s rule covers exactly this: **loosen
inbound before you tighten outbound**, and **the test suite will not catch
this**. The suite in this repository cannot: it only ever runs one version of
each side.

The export guard cannot catch it either. `attributes` is an untyped object, so
this change moves no schema byte — the compatibility analysis is the *only*
control on it. That is worth stating plainly, because it means the usual
mechanism ("the exported schema changed, so somebody looked") does not apply.

### The condition for removal

`attributes.ip` may be dropped from the agent when **both** hold:

1. The control-plane change (`nic.py` reading `addresses[]`) is deployed on
   **every** control plane this fleet's agents can reach — including any
   on-prem installation upgraded on its own schedule.
2. The fleet's **minimum agent version** is past the release that introduced
   the block, so no deployed agent still depends on the legacy path in the
   other direction.

Neither is knowable from inside this repository. Both are operational facts
about a deployment, which is why this was an open question and not a `TODO` —
and why it took the product owner, not an agent, to answer it. **Answered
2026-08-05: there is no deployment, so both hold vacuously.** That is the whole
basis for the removal; nothing about the argument above was found to be wrong.

### The cost of keeping it — which is why it did not stay

One string per NIC on the inventory document and one address per NIC that the
server deduplicates away was the *wire* cost, and it was trivial. The cost that
mattered was on the code: two readers and two writers for one fact, which is
exactly the shape that drifts. Once the compatibility argument evaporated there
was nothing on the other side of the ledger.

`TestTheLegacyIPKeyIsNotEmitted`
(`agent/internal/capabilities/inventory/nic_test.go`) now pins the key's
**absence**, on an interface the removed `legacyIPv4` would definitely have
labelled — an interface whose only address is link-local would pass that
assertion against the old code too.

### What is still degraded, and by what

**All four** of the degradations `docs/modules/ipam/plan.md` Task 13 tabulated
are now closed. (This sentence read "two … and two" while the table below listed
three closed rows; corrected 2026-08-05 against the table, which was right. The
fourth closed in IPAM's own cycle — see the row for it below.)

| Degradation | Status |
|---|---|
| One observation per NIC rather than one per address | **closed** — `addresses[]` yields one per address |
| No IPv6 reconciliation from inventory | **closed** — the block carries both families |
| `mac_mismatch` cannot fire when the NIC identifier is a friendly name | **closed** — `attributes.mac` is read, and preferred over `identifier` |
| `allocation_method` permanently `unknown` | **closed** — IPAM reads `origin`; §3.2's method is derived at read time |

All four are now closed, and the fourth was closed where it always belonged: in
IPAM's own cycle, with a migration and its own tests. `nic.py` reads `origin`
into `NicObservation.allocation_origin`, migration **0051** adds
`ipam_observation.allocation_origin`, and
`allocation.derive_allocation_method` answers §3.2 from (what an operator
asserted, what the endpoint reported) at read time — never by writing an
operator-asserted column, which §0.6 forbids. See `docs/modules/ipam/spec.md`
§3.2 and `backend/tests/test_ipam_allocation_method.py`.

**Nothing about the AGENT changed to close it**, which is worth stating because
this file spent two entries predicting the opposite. The wire contract, the
emitted values and `origin.go` are all untouched; the whole of the fix was on
the control-plane side, exactly as "the fact arrives on the wire and stops at
the parser" implied.

### One deviation from the plan, recorded here because it is a compatibility decision

`docs/modules/agent/plan.md` Task 11 step 1 lists "an `origin` outside the enum"
among the inputs that must degrade to "no observation for that entry". The
implementation does **not** drop the address in that case.

Dropping it would be *tightening inbound*, which the wire-protocol rules forbid:
the day anything adds a sixth origin (`dhcpv6` is the obvious candidate), every
address carrying it would vanish from the passive stream — silently, for the
same reason as above. `origin` is not read by `nic.py` at all today, so an
unrecognised value costs nothing. The enum is the **agent's** contract to
honour, and `agent/internal/capabilities/inventory/origin.go` honours it: a
prober that returns anything outside the five is overridden to `unknown` before
the value can leave the process.

Backed by `test_an_origin_outside_the_enum_does_not_cost_the_address`
(server) and `TestNothingOutsideTheEnumCanEscape` (agent).

---

## OQ-3 — the agent ships without a Developer ID, and self-update cannot be built until it does not

> **The self-update half was decided on 2026-09-23 and no longer waits on
> signing.** The operator approved self-update as a signed `agent.update` job,
> pinned to the release SHA-256, with local rollback and ring rollout:
> `docs/modules/agent-distribution/spec-self-update.md`. The "shortcut is
> dangerous" argument below was weighed and found to buy nothing. `script.run`
> and `app.deploy` already let a signing control plane run arbitrary code as
> SYSTEM/root, and `app.deploy` does it against a control-plane-supplied hash
> (platform review 2026-09-23, reviewer 3 §4). What remains open here is
> **signing itself**: Windows Authenticode and Apple Developer ID. When signing
> lands, self-update adds a check against a compiled-in pin (spec §10). The
> text below is kept as the record of the earlier position.

**Status: open for Windows and for macOS *distribution*; NOT blocking the MDM
rollout.** Raised by Task 14 Step 5, which asks for exactly this entry.
Narrowed once the macOS claim was actually measured — see "Three different
things" below.

Two gaps, and they are one question because the second is a consequence of the
first: **no platform's binary carries a *certificate-backed* signature**, and
**there is no self-update path** — because a self-update that cannot verify a
signature is a remote code execution primitive with a version number on it.

### Three different things, which this entry used to blur into one

"Signing" on macOS names three separate mechanisms with three separate costs,
three separate enforcement points, and — the part that was wrong here — three
separate answers to "does this block us today".

| # | Mechanism | Enforced | Cost | Status |
|---|---|---|---|---|
| 1 | **Ad-hoc signature** | by the **kernel, at `exec`**, on Apple Silicon only | free | **Done.** Emitted by the Go linker; asserted in CI |
| 2 | **Developer ID + notarization** | by **Gatekeeper**, on first launch of a *downloaded* artifact | Apple Developer Program membership (~$99/yr) + a release workflow | **Open** — needed for commercialization, not for MDM |
| 3 | **TCC / PPPC** | by **TCC**, when the agent touches protected data | free, but delivered by MDM profile | Separate; see below |

**#1 is not a policy and cannot be switched off.** On Apple Silicon the kernel
refuses to `exec` an arm64 Mach-O with no code signature at all. This is
independent of Gatekeeper, of the `com.apple.quarantine` attribute, of
notarization, and of MDM — an unsigned arm64 binary is dead on arrival, and no
profile can permit it. It is therefore the only one of the three that could
have made the macOS path *undeployable*, which is what this entry previously
claimed on Gatekeeper's behalf.

**We are not exposed to it.** Cross-compiling `darwin/arm64` with
`CGO_ENABLED=0` selects the Go **internal** linker, which embeds an ad-hoc
signature itself, in Go code, rather than shelling out to the Mac-only
`codesign(1)`. So the binary CI builds on Linux already satisfies the kernel.
Measured on the artifact `packaging/build.sh` produces, not assumed:
`LC_CODE_SIGNATURE` present, 54,642 bytes, `CodeDirectory` flags `0x20002`
(`CS_ADHOC | CS_LINKER_SIGNED`), SHA-256, 1,704 page slots, `codeLimit`
reaching the signature, and all 1,704 page hashes recomputed and matching.

That is now a **checked invariant** rather than a lucky toolchain default:
`agent/packaging/machosign_test.go` drives the real `build.sh` and fails if the
load command is missing, if the CodeDirectory is not ad-hoc, or if any page
hash disagrees with the bytes on disk. It carries its own negative tests,
because an unfalsified checker is not evidence. The two changes that would
silently undo the signature are a capability that **actually imports `"C"`**
(forcing external linking, which leaves darwin signing to the Mac-only
`codesign(1)`) and any **post-link rewrite** of the binary (`strip`, `upx`, an
appended payload), which leaves the load command in place while invalidating
the hashes — macOS rejects that exactly as hard as no signature.

The first trigger is real cgo *use*, not the `CGO_ENABLED` flag: setting
`CGO_ENABLED=1` today still yields a correctly signed binary, because no
package imports `"C"` and the internal linker is chosen regardless. That was
checked rather than assumed, and it is the more dangerous shape — the flag
looks like the control and is not, which is why the check measures the produced
artifact instead of auditing build flags.

`darwin/amd64` carries no signature and does not need one: the ad-hoc
requirement is arm64-only, and Intel Macs `exec` an unsigned Mach-O fine.

**#3, TCC/PPPC, is where #2 still earns its keep operationally.** A PPPC
profile grants the daemon access to protected resources, and it identifies the
binary it is granting to. For an **ad-hoc-signed** binary it can only pin by
**path** — which means anything that replaces the file at that path inherits
the grant, and the grant has to be re-checked whenever the binary moves. A
**Developer ID** signature lets the profile pin to a **team identifier**
instead, which survives a path change and does not extend to an attacker's
substitute. That is a real reason to want #2 eventually. It is **not** a reason
it blocks today: a path-pinned PPPC profile works.

### The facts

| Fact | Where |
|---|---|
| `docs/05-agent-spec.md` requires signed binaries on all platforms and notarization on macOS | that spec's platform table, "Signed/notarized `.pkg`" |
| `packaging/build.sh` produces an **ad-hoc-signed** `darwin/arm64` binary, and nothing certificate-backed on any platform | `agent/packaging/README.md`, "Code signing" |
| The ad-hoc signature is verified in CI, page hash by page hash | `agent/packaging/machosign_test.go`; the `darwin/arm64 ad-hoc signature` step in `.github/workflows/ci.yml` |
| The remaining signing steps are **written down** — the exact commands, where they slot in, and the CI secret names each would need | the same README section's table (`signtool`, `codesign`+`notarytool`+`stapler`, `dpkg-sig`/`Release.gpg`) |
| The keys are not in this repository and must not be | `CLAUDE.md`: "Secrets never in code or git" |
| `docs/05-agent-spec.md`'s self-update sequence has "**verifies the code signature and hash** before doing anything with it" as step 3 | that spec's §"Self-update", steps 1–5 |
| No self-update code exists — no version signal, no download, no swap, no rollback | `grep -rn "self.update\|selfupdate" agent/` returns nothing |
| `docs/modules/agent/plan.md` does not schedule either | its Self-review names self-update under "Not covered, deliberately" |

### The risk, stated plainly

**Unsigned binaries.** On Windows, SmartScreen and most EDR products treat an
unsigned executable running as SYSTEM as exactly what it looks like, so the
practical consequence today is an install that is blocked, quarantined, or
loudly warned about — an operational failure, not a security one. The security
consequence is subtler and worse: an operator who has been taught to click
through the warning for *our* agent has been taught to click through it for
anything shaped like our agent. **Windows remains genuinely unsigned and this
paragraph applies to it in full.**

**macOS, corrected.** This entry used to say Gatekeeper "refuses an unnotarized
daemon outright", making the macOS path **undeployable**. That is too strong,
and it was wrong in a way worth naming rather than quietly editing: it
attributed to Gatekeeper an absolute that only the *kernel's* ad-hoc check has,
and it did not account for how this operator deploys.

Gatekeeper assesses artifacts carrying the `com.apple.quarantine` attribute —
which is set by the browsers, mail clients and archive utilities that
*download* software. **An MDM-delivered payload does not go through that path**,
and a daemon installed by an MDM under a device's own management is not
Gatekeeper-assessed on first launch the way a double-clicked download is. The
operator has MDM and will deploy through it. Notarization is therefore not a
gate on *this* rollout.

What *is* absolute is #1 above, and it is satisfied. The honest status of the
macOS path is: **deployable via MDM, still unexecuted by anybody** — the
distinction `packaging/README.md` "What is untested" draws. Nobody on this
project has a Mac, so the ad-hoc signature is verified by arithmetic over the
Mach-O rather than by having watched the binary start.

Notarization stays open for **commercialization**: the moment the agent is
distributed to a customer who downloads it rather than receiving it from an MDM
we control, quarantine applies, Gatekeeper assesses, and #2 becomes a hard
requirement. That is a business-model gate, not an engineering one.

**No self-update.** Every endpoint is pinned at the version it was installed
with. A defect in the agent — including a security defect in the signed-job
verification path, which is the one piece of endpoint code that must never be
stale — is fixed by an operator touching every machine. For a fleet, that is not
a fix; it is a project.

**And the tempting shortcut is the dangerous one.** The obvious way to close the
second gap without the first is to have the agent download a binary over the
already-authenticated mTLS channel and check a hash the control plane supplied.
That is not equivalent, and the difference is the whole point: it makes a
compromised (or merely mis-operated) control plane able to replace root/SYSTEM
code on every endpoint, with no second factor and no offline-verifiable
artifact. A code signature is checkable by the endpoint against a key that is
**not** the control plane's; a hash from the control plane is the control plane
vouching for itself. The mTLS channel already carries signed jobs precisely
because "the transport is authenticated" was never accepted as sufficient there
either (invariant #1).

### The gate

Code signing cannot be done from inside this repository, and that is why this is
an open question rather than a `TODO`. It needs, from outside:

1. **An EV or OV Windows code-signing certificate**, purchased and held in the
   CI secret store as `WINDOWS_SIGNING_CERT` / `WINDOWS_SIGNING_PASSWORD`.
2. **An Apple Developer ID** plus a notarization key —
   `APPLE_SIGNING_IDENTITY`, `APPLE_TEAM_ID`, `APPLE_NOTARY_KEY`. Needed for
   **commercialization** (distribution outside managed devices) and for pinning
   a PPPC profile to a team identifier rather than a path. **Not** needed for
   the MDM rollout: the kernel's ad-hoc requirement is already satisfied.
3. **A decision about the Linux path**: signing the `.deb` (`dpkg-sig`) or
   signing an apt repository (`Release.gpg`) — they are different operational
   commitments, and only the second gives endpoints a continuing update channel.
4. **A release workflow separate from the per-PR build.** The signing steps must
   not run on a pull request from a fork, because that is handing the signing
   key to whoever opened the PR.

None of the four is a decision an implementation task should make on its own.
Items 1–3 are procurement and cost; item 4 is a supply-chain boundary.

### What a decision would need to answer

- **Who holds the keys, and what is the recovery story if one is lost or
  compromised?** A revoked code-signing certificate invalidates every binary
  already deployed under it. There must be an answer before the first signature,
  not after the first incident.
- **Is the signature checked at install time only, or at every start?** The
  first is what the OS gives for free; the second is what makes tampering with
  an already-installed binary detectable, and it costs a verification on every
  service start.
- **What key does the agent verify a self-update against, and how does it get
  there?** If the answer is "one pinned at enrollment", then rotating it is a
  new signed-job type and its own review. If the answer is "the platform's
  native code-signing trust store", then self-update is thinner but the
  behaviour differs per OS. Both are defensible; picking neither is how the
  hash-from-the-control-plane shortcut gets built by default.
- **Does self-update stage (rings), and who decides the ring?**
  `docs/05-agent-spec.md` step 1 says "optionally staged"; optional in a spec
  becomes absent in an implementation unless someone chooses.
- **What is the rollback trigger, concretely?** Step 5 says "fails a
  post-update health check". An agent that cannot connect cannot be told to roll
  back, so the trigger has to be local, and a local trigger that is too eager
  turns one bad health sample into a fleet-wide downgrade.

### Until then

- ~~**Do not build self-update.** Not a "temporary" hash-only version, not behind
  a flag, not for internal use. It is a code-execution path into every endpoint
  and it gets a spec cycle, after signing exists.~~ **Superseded 2026-09-23.**
  It got its spec cycle (`spec-self-update.md`), and the operator approved the
  hash-pinned signed job without waiting for signing. See the note at the top
  of this entry.
- **The Linux/Debian path is the deployable one** and it is unaffected by this:
  `apt` does not require a signed binary inside the package, and
  `packaging/README.md` records the `.deb` lifecycle as the tested path.
- **The macOS path is shippable via MDM, and remains unexecuted.** It is *not*
  blocked on notarization — the only absolute barrier, the kernel's ad-hoc
  signature requirement on Apple Silicon, is already satisfied and asserted in
  CI. Treat it the way `packaging/README.md` treats the Windows service: built
  and structurally checked, never run. Run the suite on a Mac before a fleet
  rollout, and expect the first real finding to be TCC, not signing.
- **The Windows path is the one that is genuinely unsigned** and carries the
  SmartScreen/EDR consequence above.
- **Updating an endpoint is `dpkg -i` of a newer package**, which preserves
  `/var/lib/its-agent` and therefore the device identity (the `remove` keeps
  state / `purge` destroys it split is asserted in CI).
