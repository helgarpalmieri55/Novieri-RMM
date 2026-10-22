# Agent wire protocol v1

JSON Schemas generated from `backend/app/gateway/protocol.py` — the
authoritative contract between the control plane and the Go agent (`agent/`).
Regenerate after protocol changes:

```sh
backend/.venv/bin/python scripts/export_wire_schema.py
```

## This is a compatibility contract, and one repo does not change that

The agent used to live in a separate `it-suite-agent` repository, which made the
two-sidedness of these schemas obvious: changing them meant a second commit in a
second tree, and you could not miss it. Since `CLAUDE.md` commit `a32cead` the
agent lives under `agent/` here, so both sides can change in one commit. **That
is easier, not safer.**

A fleet is always mixed-version. An agent in the field was built from an older
commit than the control plane it is talking to and updates on its own schedule,
so "the repo is consistent" says nothing about what is deployed. The rule that
replaces cross-repo coordination:

- **Additive, or versioned, or it is a break.** A new optional field is fine. A
  field a new control plane *requires* and an old agent does not send is an
  outage. Removing or retyping a field, or narrowing an enum, is a break.
- **Loosen inbound before you tighten outbound.** Ship the server change that
  accepts both shapes, wait for the fleet to roll, then change what the agent
  emits — never the reverse.
- **The test suite will not catch this.** Every test here runs one version of
  both sides against itself. Compatibility is a review obligation, not a
  green check.

- `JobEnvelope` — server → agent. `sig` is a base64 Ed25519 signature over the
  canonical JSON bytes of the `job` object (sorted keys, compact separators,
  UTF-8, no ASCII escaping). The agent MUST verify signature, device identity,
  freshness (`issued_at`/`expires_at`), and nonce non-replay before executing —
  see `docs/05-agent-spec.md`.
- `JobStatusMsg`, `JobLogMsg`, `JobResultMsg`, `HeartbeatMsg` — agent → server.

Unknown `kind` values and versions other than `v: 1` are rejected by both
sides.

## Certificate rotation (`docs/modules/agent/cert-renewal.md`)

`AgentCertRenewParams` / `AgentCertRenewResult` and `AgentCertInstallParams` /
`AgentCertInstallResult` are the two legs of automatic certificate renewal —
the only job types whose effect is on **the agent's own identity** rather than
on the host. They are ordinary `JobEnvelope`s and are verified exactly like any
other job; invariant #1 gets no exemption here, least of all here.

The compatibility analysis, in both directions, because a fleet is always
mixed-version:

- **New control plane → old agent.** Additive. An agent built before this
  change has neither type in its capability registry, so its runner refuses the
  job `unknown_type` **before anything runs** — the correct behaviour, already
  implemented and already tested. Its certificate is untouched and it keeps
  working until its own `NotAfter`, which is what it needed anyway.
- **New agent → old control plane.** Rotation is entirely **server-initiated**.
  Nothing in the agent asks for it, so there is no request an older control
  plane would answer with a 404 or a validation error, and the agent behaves
  exactly as it did before the capability existed.

Two properties of these schemas are load-bearing and are not visible from the
JSON alone:

- **The private key never leaves the endpoint.** `AgentCertRenewResult` carries
  a CSR and nothing else. A future field carrying key material, in either
  direction, is not an additive change — it is a different and much worse
  design.
- **`AgentCertInstallParams.cert_serial` is written for the server and ignored
  by the agent.** It exists so the control plane records the serial string its
  own CA reported (OpenBao's colon-separated form differs from bare hex, and
  `revoke_agent` feeds `agents.cert_serial` straight back to `CAService.revoke`).
  An agent that failed on it would break the moment the control plane added a
  field — unknown members must stay ignorable.

## Interruption: `job.cancel` (`docs/superpowers/specs/2026-08-22-job-lifecycle-design.md` §3.1, §3.7)

`JobCancelParams` / `JobCancelResult` are the wire half of cycle 2. A cancel
that reaches an endpoint is a **command to that endpoint**, so invariant 1
governs it with no exemption: it is an ordinary `JobEnvelope`, signed over its
canonical bytes by the same signer, verified by the agent against the key
pinned at enrollment — signature, device, freshness, nonce, type, in that
order — before anything acts on it. A forged cancel would otherwise be a
denial-of-service primitive against the whole fleet.

### It is a new job TYPE and deliberately not a new `kind`

This is the compatibility decision, and it was made by measuring what a
deployed agent actually does with each.

| what arrives | what a pre-cancel agent does |
|---|---|
| an unknown wire `kind` | `wire.Decode` returns `ErrKind`; `Runner.Handle` logs `"undecodable frame from gateway; dropped without answer"` and **sends nothing**. The connection survives. |
| an unknown job **type** | `Verifier.Verify` refuses `unknown_type` **before anything runs** and sends a `job_result`; `JobEngine.refuse` writes `jobs.refused` into the hash-chained audit log. |

Silence is the worst possible property for a verb whose whole difficulty is
knowing what the endpoint did: the control plane would have to tell "old agent,
cancel impossible" from "new agent, job already finished" by timeout, i.e. by
guessing. The job-type route turns exactly that question into an answer on the
record, in seconds.

### Both directions

- **New control plane → old agent.** Additive. The old agent has no
  `job.cancel` capability, refuses `unknown_type`, and its answer is what the
  control plane reads: `cancel_outcome` becomes `unsupported`, a
  `jobs.cancel_unsupported` audit event is written, a `job_logs` line is
  appended to the target, and the console says this endpoint's agent is too old
  to be interrupted. **The target job is untouched and runs or expires exactly
  as it would have.** No field is required of the old agent, no message it
  sends is rejected, and nothing it does changes.
- **New agent → old control plane.** The new agent gains a capability it never
  *initiates*: `job.cancel` arrives only if a control plane sends one, and an
  old one never does. `JobCancelResult` is only ever emitted in reply. There is
  no request an older control plane would answer with a 404 or a validation
  error.
- **No enum on the wire is widened.** `JobResultMsg.state` is untouched.
  Adding `"cancelled"` to it would be safe for a new server reading an old
  agent and fatal in the direction that bites — a *newer* agent against an
  *older* control plane, where `parse_agent_message` drops the whole message
  and the job hangs to expiry with only a `logger.warning`. The ordering race
  (a cancel overtaking its target) is answered with `refused`, a state
  `JobResultMsg` already carries.

### `cancelled_before_start` is a constant with two homes, and they must agree

The suppressed target's refusal reason is `cancelled_before_start`
(`ReasonCancelledBeforeStart` in `agent/internal/jobs/verify.go`,
`AGENT_CANCELLED_REFUSAL` in `backend/app/jobs/cancel.py`). It is not a
message — it is the token that tells the server the difference between "the
endpoint never started this job" (`cancel_outcome = "dropped"`) and "the
endpoint stopped it mid-flight" (`interrupted`), which the console renders as
two different sentences. Changing it on one side only degrades every such
cancellation to `interrupted`, **silently**, with no test in this repository
red — because both sides of every test here are built from the same commit.

### `dropped_before_start` is accepted and this agent never sends it

The schema carries four outcomes. This project's agent produces three of them:
`interrupted`, `already_finished` and `unknown`. It cannot produce
`dropped_before_start`, because §3.8's two pieces of cancellation state — a map
of *executing* jobs and a bounded set of cancelled ids — cannot distinguish "sitting
in the dispatcher's queue" from "never seen": both are simply absent from the
running map. That case is instead reported by the **target itself**, which is
suppressed when it reaches the runner and answers `refused:
cancelled_before_start`; the server upgrades `cancel_outcome` from `too_late`
to `dropped` on that answer.

The value stays in the schema and the server keeps accepting it. Narrowing an
enum an agent may already send is a break, and a future agent that tracks its
own queue can answer more precisely without a server change. This is the
additive direction; the reverse is not.

## `app.deploy` (`docs/modules/app-deploy/spec.md` §5.2, §6)

Two changes landed together in 2026-08, both server-side gaps the agent
capability found while being built and refused to work around.

### `artifact_ref` changed MEANING without changing TYPE, which is the risky kind

It was an `s3://<bucket>/<key>` reference copied off the `packages` row. It is
now a **presigned `https://` URL** minted at dispatch time. The JSON type is
unchanged — still a nullable string — so **no schema diff shows this and no test
in this repository can catch it**. It is safe only because of what the agent
already did:

- `defaultFetcher` accepts `https://` and REFUSES `s3://` with a reason naming
  the missing route. So an old agent given the new value works, and a new agent
  given the old value refuses loudly rather than failing mysteriously.
- The hash check is unchanged and unconditional. The URL is not the
  authorization; `artifact_sha256` in the signed params is.

The direction that would NOT have been safe is the reverse — shipping an agent
that required `https://` before the control plane emitted it. "Loosen inbound
before you tighten outbound" applied here by accident of good agent design, not
by design of this change, and that is worth saying out loud.

**The lifetime is the job's, exactly.** The URL is signed for
`ITS_JOB_DEFAULT_TTL_S` and the job is created with that same TTL. A URL
outliving its job is a standing grant on a job the agent already refuses as
stale; a URL dying inside its job is a 403 mid-deployment. Retries re-mint:
`resign_for_retry` gives an attempt a fresh window and `JobType.params_refresher`
sizes a fresh grant to it, because the first retry is at least 10 minutes after
dispatch and would otherwise carry a dead URL through the remaining attempts.

**It is a bearer credential for its lifetime.** Not logged, not audited, and
redacted out of every API read path by `JobType.params_redactor` — `jobs.read` is
a far weaker permission than the `deploy.execute` that authorized the dispatch.

### `package_identifier` is a new OPTIONAL member

The identifier `winget`/`choco` install by. Additive in both directions:

- **New control plane → old agent.** Go's `encoding/json` ignores the unknown
  member. The agent keeps deriving the identifier from `detection_rule.name` —
  and the server's `manager_package_identifier` falls back to exactly that value
  when `packages.package_identifier` is unset, so both sides compute the same
  string and an unrevisited catalog behaves identically.
- **New agent → old control plane.** The member is absent, which is `null`,
  which is the state the agent already handles by falling back.
- Required would have been an outage. It is not required.

> **The in-tree agent went RED on this the moment the schema was exported, and
> that is the contract working.** `agent/internal/capabilities/deploy/schema_test.go`
> asserts the Go struct and the exported schema define the same properties in
> BOTH directions, so a new schema property with no matching field fails with
> "the server may already be sending it". It did, immediately, which is exactly
> what that assertion is for — the same signal a separate agent repository used
> to give by making you write a second commit in a second tree.
>
> It is closed: `deployParams.PackageIdentifier *string` plus a
> `managerIdentifier()` that prefers the member and falls through to
> `detection_rule.name` when it is absent OR blank. **Runtime compatibility was
> never at stake** — a deployed agent ignores an unknown member — so this was a
> build-time contract signal, not a field outage. The two fallbacks were then
> checked against each other rather than assumed compatible: server-side
> `manager_package_identifier` and agent-side `managerIdentifier` both prefer the
> explicit value, both strip whitespace, and both fall back to
> `detection_rule.name`, so the two sides compute the same string for every
> catalog row.

## The backup dedup exchange (`docs/modules/backup/spec.md` §5.2)

`BackupChunkQueryMsg` (agent → server) and `BackupChunkGrantMsg` (server →
agent) are the FIRST new message KINDS since the kernel envelope, so the
compatibility question is different from an added field.

**Both directions are safe, and for different reasons.**

- **New agent → old control plane.** `parse_agent_message` rejects an unknown
  kind at the parse layer and `gateway/main.py` logs "unparseable message" and
  keeps the connection. The agent gets no answer; its run fails on the
  unanswered query. That is not a regression: the agent it replaced refused
  `backup.run` outright.
- **New control plane → old agent.** The server sends a `backup_chunk_grant`
  only in reply to a query, and an old agent never sends one. `wire.Decode`
  would refuse the kind anyway, which is the correct behaviour — "acting on a
  frame the server has no code to send is worse than being offline" — and it
  never arrives.

**The 1024 bound is a compatibility surface, not a tuning knob.** It is
`maxItems` in the exported schema, `MAX_CHUNK_BATCH` on the server and
`maxChunkBatch` in the agent, and `internal/capabilities/backup/schema_test.go`
asserts the last two against the first. TIGHTENING it is what breaks a fleet:
a deployed agent keeps sending 1024, the new control plane drops the frame at
the parse layer, and the agent waits for an answer that will never come. There
is no error to see — the failure mode is silence. Widen it, or version it.

**The chunk-id pattern is likewise load-bearing in both places.** `^[0-9a-f]{64}$`
is what stops an id becoming a key outside the repository prefix, and the agent
asserts that what its own `chunkID` produces satisfies the exported pattern — a
mismatch would refuse every batch it ever sends, silently.

> **Not yet speakable.** `agent/internal/wire` implements neither kind:
> `Decode` refuses every inbound kind but `job`, and `Encode` refuses any
> outbound type not in its declared set. The agent-side PAYLOADS live in
> `internal/capabilities/backup` and are asserted against these two files
> there; the framing is the remaining step, and `internal/wire/schema_test.go`
> carries that as a `notImplemented` entry saying so rather than as silence.
>
> **Corrected, prefix-scoped-read cycle (2026-08-13).** Both kinds are now
> speakable: `Decode` returns `*wire.ChunkGrant` for an inbound
> `backup_chunk_grant`, `Encode` frames an outbound `backup_chunk_query`, and
> `main.go` routes grants to `internal/chunkreply` and everything else to the
> runner. This was measured on the wire, not only in unit tests: a real agent
> sent a real query and the control plane published a real grant for it (spec
> §6.1, "Still open after §5.2" item 2).

## The backup READ exchange (`docs/modules/backup/spec.md` §6.1 item 1)

`BackupReadQueryMsg` (agent → server) and `BackupReadGrantMsg` (server →
agent) are the SECOND pair of new message kinds since the kernel envelope,
added because the write half above cannot serve `backup.restore`: a restore's
chunk selection can run to six figures and SigV4 query signing has no way to
express a prefix-scoped READ.

**This is where the two exchanges diverge, and the divergence is structural,
not a missed optimization.** The write grant is **one POST policy over a
prefix** (`starts-with $key {storage_ref}/`) — S3 POST policies are the one
SigV4 signing form that can carry a prefix condition, so one signed document
authorises every write a run performs. A **read** has no equivalent signing
form: SigV4 query signing authorises exactly one key per signature, so a read
grant is signed **per object** and the exchange batches many signatures into
one reply instead of compressing them into one document. `BackupReadQueryMsg`
therefore carries repository-relative **suffixes**, never keys — the server
joins each onto the prefix it derives from the job, so an agent cannot
express a location outside its own repository — and `BackupReadGrantMsg`
answers with one signed URL per suffix, up to `READ_BATCH_MAX` (256) per
batch, at up to `min(900s, the job's remaining lifetime)` each.

**Both directions are safe, and for the same reasons as the write exchange:**

- **New agent → old control plane.** `parse_agent_message` rejects the
  unknown `backup_read_query` kind at the parse layer, exactly as it does for
  `backup_chunk_query`; the agent's pending `readreply` wait then times out
  and `backup.restore` fails naming the unanswered query. **This is the
  compatibility property that matters here**: a new agent talking to an old
  control plane gets a loud, stated failure — not a hang, and not a partial
  restore silently missing chunks the old server never signed.
- **New control plane → old agent.** The server sends a `backup_read_grant`
  only in reply to a query an old agent never sends, and `wire.Decode` would
  refuse the unrecognised kind anyway if one somehow arrived.

There is deliberately **no third kind and no mode field on the existing
one** — see `docs/superpowers/specs/2026-08-13-prefix-scoped-read-design.md`
§2.4 for why: an old server's inbound parsing is loose enough to ignore an
unknown discriminator on `backup_chunk_query` and answer with a WRITE grant,
which a new agent could then misread as a read authorization it does not
hold. A separate kind fails loudly at the parse layer instead of failing
silently at the semantic layer.

As with `BackupChunkGrantMsg`, there is **no error variant** on
`BackupReadGrantMsg`: every authorization refusal — unknown job, wrong
device, wrong job type, an expired job, a restore not currently running, a
`storage_ref` that does not match the prefix the server derives, an unsafe
suffix — is silence plus one `backup.read_refused` audit record, for the
reason `BackupChunkGrantMsg`'s own doc states: a reply that distinguished
"no such job" from "not your job" would be an oracle a compromised endpoint
could walk.

## `net.sweep` (IPAM, `docs/modules/ipam/spec.md` §5)

`NetSweepParams` (server → agent) and `NetSweepDocument` (agent → server) are
the contract for the one capability in this platform that puts packets on
**someone else's** wire. Four requirements belong to the `net.sweep` agent
capability and are restated here because the schemas alone do not carry them
(spec §5.5, and `CLAUDE.md`'s idle budget):

> **Corrected 2026-08-06.** This paragraph said the capability was
> "**unbuilt**". It is built — `agent/internal/capabilities/netsweep/` — and has
> been since plan Task 13. Each of the four requirements below has a test named
> for it in `netsweep_test.go` (`TestTheCapabilityAddsNothingToTheTimerWheel`,
> `TestNothingIsOpenedBeforeAJobArrivesAndEverythingIsReleasedAfter`,
> `TestParamsOutsideTheSignedBoundsAreRefusedBeforeProbing`,
> `TestADigestMismatchRefusesBeforeAnyProbeIsEmitted`).

- **Dormant by default.** The capability allocates nothing until a `net.sweep`
  job arrives and releases its socket, worker pool and result buffer when the
  job completes. No resident scanner, no background thread, no cache.
- **No local schedule.** The cadence is server-side (§0.9). The agent's timer
  wheel gains no `net.sweep` entry: the authorized target list is computed at
  dispatch time and carried inside the **signed** params, so a locally scheduled
  sweep would be a standing authorization executed for weeks after the scope was
  revoked.
- **Bounded by construction.** `rate_pps` (≤ 200), `max_targets` (≤ 65,536) and
  `budget_s` (≤ 900) are all in the signed params, so the ceiling on what a
  sweep can cost is set by the server and covered by the signature. A sweep that
  hits its budget returns `truncated: true` rather than running longer.
- **Raw sockets only inside the job**, opened at job start and closed at job
  end.

`targets` is an explicit enumerated CIDR list — never "the subnet". The agent is
never asked to decide what it may touch and has no local configuration to
consult. `scope_digest` is `sha256` over the compact JSON encoding of that exact
list and must be echoed back unchanged; the server rejects the **whole** result
if it disagrees, or if any reported address falls outside `targets`.

`NetSweepDocument.hosts[]` forbids unknown fields. There is deliberately no RTT,
no per-probe timing, no retry count and no `reachable: false` entry: a host that
did not answer is simply absent. Adding any of them turns an address plan into a
monitoring product one field at a time (spec §0.10, §7).

## `site.wake` (site servers, `docs/modules/site-server/spec.md` §5)

`SiteWakeParams` (server → agent) and `SiteWakeResult` (agent → server) are the
contract for the second capability that acts on something other than the device
the job is signed for.

> **Built 2026-08-06.** This section used to read "**The agent half is
> unbuilt**", and that was true from the day the schemas were exported until the
> day `agent/internal/capabilities/site/` landed. It is now implemented and
> registered, and `agent/internal/capabilities/site/schema_test.go` asserts the
> Go structs against **these two files** — the port enum, the repeat bounds,
> both defaults and the absence of a `woke` field. So the paragraphs below are
> no longer only a description of what an implementation ought to do: each one
> has a test that fails when it stops being true. The test names are given
> inline.

What follows belongs to that capability and is restated here because the schemas
alone do not carry it.

- **`broadcast` is an explicit ADDRESS and never a CIDR.** The control plane
  picks the operator-declared `site_server.segments` entry containing the target
  and computes the broadcast address itself; the agent sends to exactly that
  address and to nothing else. It has no local configuration to consult and must
  not re-derive anything — an authorization revoked in the control plane must
  never depend on the endpoint's arithmetic. The schema enforces this: a value
  carrying a prefix length is rejected — and so does the agent, before it opens
  a socket (`TestABroadcastCarryingAPrefixLengthIsRefusedBeforeAnyDatagramIsSent`,
  which asserts the SEND COUNTER and not the returned error: an implementation
  that returned the right error and emitted the datagram anyway would satisfy an
  error-only assertion while doing the one thing the rule forbids).
- **`port` is `7` or `9` and nothing else.** A free destination port would turn
  a wake capability into a general "send a UDP datagram of my choosing to a
  broadcast address" primitive. Checked ON THE ENDPOINT too
  (`TestAPortOutsideSevenAndNineIsRefusedBeforeAnyDatagramIsSent`), because the
  server's `Literal[7, 9]` does not travel with the job in any form the agent
  can enforce. 69 and 4011 are in that test's refusal table by name: they are
  TFTP and proxyDHCP, and a wake that could reach them is the deferred PXE slice
  arriving through the back door.

- **The payload carries no instruction, and that is structural.** The magic
  packet is a pure function of six bytes of MAC — 6×`0xFF` then the MAC sixteen
  times, 102 bytes — so no `broadcast`, no `port`, no `repeat`, no job id, no
  nonce and no unknown params member can appear on the wire.
  `TestNothingButTheMACCanReachTheWire` drives two wakes differing in everything
  but the MAC, one of them carrying an unknown member holding an HTTP request,
  and compares the bytes.
- **The target MAC is a parameter, not a job target.** The signed `device` field
  names the SITE SERVER. The machine being woken usually has no `devices` row at
  all — waking bare metal for provisioning is the primary use.
- **Dormant by default.** One UDP socket with `SO_BROADCAST`, up to `repeat`
  datagrams ~200 ms apart, then closed. Nothing resident, no timer-wheel entry,
  no listener — `CLAUDE.md` invariant #2 is unamended and this capability binds
  nothing. The socket is **dialled**, not bound: `net.Dialer.DialContext` with a
  Control hook that sets `SO_BROADCAST` before connect. The obvious
  `net.ListenPacket` would have needed an allowance in
  `agent/internal/transport/listen_test.go` beside `net.sweep`'s raw ICMP
  socket; **this package took none, and the absence of that entry is the
  assertion.** A future change here that needs one is turning a send-only
  capability into a listener.
- **The TTL is 300 s, not the kernel's 3600 s.** A wake drained from the queue
  hours after it was requested fires into a maintenance window that has ended. A
  late wake is a wrong wake, and `expired` is the correct outcome.

`SiteWakeResult` reports `sent`, and deliberately has **no `woke` field**, no
reachability probe and no retry count. A magic packet is unauthenticated and
unacknowledged: there is no reply to wait for, so a field claiming the machine
came up could not carry true information. The server rejects a result whose
`broadcast` differs from the signed params — a packet sent somewhere else is not
that wake.

**The idle-budget numbers above are MEASURED as of 2026-08-06.** This paragraph
used to say they were a design target, because spec §5.4 named two falsifiers
and no benchmark. Both are now assertions that fail CI, in
`agent/internal/capabilities/site/budget_test.go`:

| §5.4 falsifier | Where | Measured |
|---|---|---|
| a socket or timer-wheel entry resident with no wake job in flight | `TestFalsifier1_AnIdleAgentHoldsNothingForThisCapability`, plus `TestNothingIsOpenedBeforeAJobArrivesAndEverythingIsReleasedAfter` (reads `/proc/self/fd` around a **real** wake) and `TestTheCapabilityAddsNothingToTheTimerWheel` | 0 allocs/op, 0 B/op, 0 goroutines, 0 new file descriptors, 0 wheel entries |
| resident memory that does not return to its pre-job baseline after one wake | `TestFalsifier2_ResidentMemoryReturnsToItsPreJobBaseline` | 1 wake: RSS −77,824 B (limit 4 KiB). 10,000 wakes: RSS +270,336 B (limit 2 MiB), retained heap +112 B across a hundredfold spread |

One wake costs 13 allocations and 528 bytes, gated at a 4 KiB ceiling.

**None of it is a `Benchmark` function, deliberately.** `go test -bench` exits 0
on a regression — measured on this project, where a planted 64 KiB/op regression
printed `65536 B/op` and still exited 0. Every number above is produced by
`testing.Benchmark` called from an ordinary `Test` that then compares, so a
regression turns the build red. The `agent` CI job additionally prints them by
name, in bytes.

**What is still a design target: `~0% CPU at idle` as wall-clock.** A shared
runner's variance exceeds the signal, so a threshold loose enough not to flake
is loose enough to pass a real regression. It is described as a target rather
than measured badly, per `CLAUDE.md`.

## Agent push install: `agent.push_probe` / `agent.push_install` (`docs/modules/agent-distribution/spec-push-install.md` §4, §7, §11)

`AgentPushProbeParams`/`AgentPushProbeResult` and `AgentPushInstallParams`/
`AgentPushInstallResult` are the wire half of cycle B2. They are consumed by
`agent/internal/capabilities/pushinstall`, whose `schema_test.go` asserts the Go
structs against these files (the chain model → schema → struct, guarded on each
link), and `internal/wire`'s `notImplemented` map defers them there.

**They are new JOB TYPES, not new wire `kind`s — the same compatibility decision
as `job.cancel`.** An agent built before this cycle has no `pushinstall`
capability, so its runner refuses `agent.push_probe`/`agent.push_install`
`unknown_type` LOUDLY, on the record, before anything runs — and the control
plane reads that `job_result` as "this endpoint cannot be pushed to". An unknown
inbound *kind* would be dropped in silence, which is the worst property a
credentialed install could have. So a mixed-version fleet degrades to "the older
agents refuse the push" rather than to "the push vanished".

**The additive rule holds in both directions.** A new optional field the server
adds is ignored by an older agent (encoding/json drops unknown members); the
agent requires nothing of an older control plane. The one field that is
load-bearing and must never become optional is `host_key_fingerprint` on the
install: the agent verifies the live host key against it and aborts on mismatch,
so an install that arrived without it would be trust-on-first-use with an
administrative credential — the capability refuses that outright.

**What is on the wire that is unusual, and why it is bounded rather than
removed.** `AgentPushInstallParams.credential` carries a decrypted
`{username, secret}`, because a push cannot authenticate to an unmanaged machine
without it (invariant #3, spec §2.4, §6). The exposure is bounded ON THE SERVER
SIDE, not by the wire: the credential is redacted from every `GET /jobs/{id}`
(the job type's `params_redactor`) and scrubbed from the `jobs.params` row the
instant the job reaches any terminal state (the `terminal_params_scrubber`,
§6.4). On the endpoint it is held in a zeroed `[]byte`, never in argv, never in a
log. This schema documents the contract; it does not weaken those bounds, and
the residual — the plaintext in a `jobs.params` row for up to the 300s TTL — is
the one §6.4 and §1.3 item 3 call out for the operator to weigh.

`AgentPushProbeParams` has NO credential field and never may: a probe learns the
host key before any secret is presented (§4.4), and `additionalProperties:false`
is what makes "no field could carry a credential" a property of the model rather
than of a review.

### Windows over WinRM (2026-09-28; `docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md` §2, §4)

D10 was overturned: a Windows target is pushed over WinRM, with Kerberos only.
The wire grew to carry it, and **every change is additive** — widened enums,
new optional fields, new default-false result fields. No property was removed
or renamed, and none became required.

- **`transport` enumerates `ssh | winrm`** on the probe params, the probe
  result and the install params (it was `const "ssh"`). `ssh` is Linux and
  macOS, unchanged; `winrm` is Windows.
- **`AgentPushInstallParams.os` gains `windows`** beside `linux` and `darwin`.
- **`dns_name` is a new optional field** (default null) on
  `AgentPushProbeParams` and `AgentPushInstallParams`. Kerberos needs the
  target's name (`HTTP/<fqdn>`), not its IP, so the **server** requires it for
  `winrm` and refuses a Windows target without one before any credential is
  decrypted; it is ignored for `ssh`. It is optional on the wire so the field is
  additive — the requirement lives in dispatch, not in the schema.
- **`enroll_url` is a new optional field** (default null) on
  `AgentPushInstallParams`: the console's bare https origin from the server's
  `ITS_PUBLIC_CONSOLE_URL`, which the `winrm` path writes into the installed
  service's `ITS_AGENT_ENROLL_URL` (the agent appends `/enroll/v1`). The server
  sets it on every install (`winrm` and `ssh`) and refuses an install before
  any credential is decrypted when it is not configured. Over `ssh` it is the
  one value in the fixed `its-agent install ... --enroll-url '<origin>'`
  command, which writes it where `packaging/linux/install.sh` and
  `packaging/darwin/install.sh` do. It is never derived from the presigned
  `release.url` (plan W2, superseded 2026-09-29).
- **`AgentPushProbeResult` gains `winrm_https`, `winrm_http` and
  `kdc_reachable`** (booleans, default false): 5986 answered, 5985 answered, a
  domain controller for the target's realm answered from the site server. The
  `winrm` probe stays credential-free — it records reachability, performs no
  Kerberos exchange, and its params still have no credential field.
- **`AgentPushProbeResult` relaxes `host_key_fingerprint`, `host_key_type` and
  `banner` to optional-with-defaults** (empty string). They are the SSH probe's;
  a `winrm` probe result omits them. For a reader this is additive — a member
  that used to be always present may now be absent, with a default — and it
  only happens on a `winrm` result, which only a control plane that sent a
  `winrm` probe will ever read. An `ssh` result still carries all three.
- **`host_key_fingerprint` on the install stays required**, and the paragraph
  above still binds the SSH path. For `winrm` the server signs the fixed
  sentinel `"kerberos"` into it: Kerberos mutual authentication, not a pinned
  key, is what authenticates a Windows target, and the agent's WinRM path
  ignores the value. The SSH path's refusal of an install without a confirmed
  key is unchanged.
- **`PushCredential.secret` carries a JSON blob for a `windows_domain` push** —
  `{"secret", "kdc", "ca_bundle"}` (the password, an optional explicit domain
  controller, an optional PEM CA bundle for 5986 trust) — and stays the bare
  secret for a `push_install` (SSH) push. **No schema change:** `PushCredential`
  is still `{username, secret}` and `secret` is still one opaque string, so the
  redactor and the terminal scrubber cover the KDC and CA bundle with the
  password, by construction.

**A mixed fleet is unaffected.** A site server whose agent does not advertise
the capability `agent.push_install.winrm` is never chosen as the originator of
a `winrm` push — the target is refused `no_winrm_site_server` and recorded —
so an older agent is never sent a `winrm` job and never has to understand one.
A control plane that never sends `winrm` behaves exactly as before, and an
older control plane reading a newer agent's `ssh` probe result sees every field
it expects.

## Screen share: `remote.screen.open` and rsp/1 (`docs/modules/remote-control/spec-screen-share.md` §3, §6)

`RemoteScreenOpenParams` / `RemoteScreenOpenResult` are the control half. The
media half is **rsp/1**, a binary framing over a second outbound mTLS WebSocket
to the gateway's `/relay` path; it is not JSON, so its contract is
[`remote-screen-protocol.md`](remote-screen-protocol.md) and its bytes are
pinned by [`rsp1-vectors.json`](rsp1-vectors.json), which the agent's and the
server's tests both read.

**Additive in both directions.** `remote.screen.open` is a new JOB TYPE: an
agent without the capability refuses it `unknown_type` before anything runs,
and the server does not dispatch it at all unless the agent advertised
`remote.screen.open` at enrollment (409 "agent too old" instead). An older
control plane never sends the job, and the agent never dials `/relay` on its
own. The gateway dispatches only the exact path `/relay` differently, and it
negotiates the `itms-rsp.1` subprotocol without requiring one, so clients on
`/agent` that offer none keep connecting. No existing enum is widened: consent
travels as an rsp/1 STATE frame, not as a new `JobStatusMsg.state`.

**What rides in the signed params.** `relay_token` is a one-shot bearer secret
valid until `connect_by` (60 s after the job is created), usable only by the
target device's certificate. It is redacted from `GET /jobs/{id}` and scrubbed
from `jobs.params` when the job ends. The job's own TTL is long
(`max_duration_s + consent timeout + 120 s`) because the job lasts as long as
the session; freshness comes from `connect_by`, and nonce non-replay still
applies.

**Phase 0 status.** The server half is built; the agent capability
(`internal/capabilities/remote`) is phase 1. `agent/internal/rsp` holds the
codec and a test double (`internal/rsp/fakeagent`) that the backend's
end-to-end test drives through the real gateway.
## Self-update: `agent.update` and the connect headers (`docs/modules/agent-distribution/spec-self-update.md` §3)

`AgentUpdateParams`/`AgentUpdateResult` are exported from
`backend/app/modules/agent_update/jobtypes.py` and consumed by
`agent/internal/capabilities/selfupdate`, which asserts against both files in
its own `schema_test.go`.

**A new job TYPE, not a new `kind`.** An agent built before it refuses
`unknown_type` before anything runs, and the reconciler reads that refusal as
"needs manual update". `JobResultMsg.state` is not widened: `rolled_back` and
`aborted` travel as `failed` with an `outcome`, because a new state would be
fatal from a newer agent to an older control plane.

**The result is deferred, and may arrive on a later connection from a later
process.** The process that receives the job stages it and returns; the
terminal `job_result` is enqueued in the persisted outbox by the new binary
once it confirms, or by the old binary after a rollback. Nothing about that is
new protocol: the outbox already delivers answers across reconnects.

**Two optional upgrade-request headers**, `X-ITS-Agent-Version` and
`X-ITS-Agent-Capabilities` (sorted, comma-separated, at most 4 KiB, each name
`^[a-z][a-z0-9._-]{0,63}$`). An older gateway ignores unknown request headers;
an older agent sends neither and its version reads "unknown". They are read
after the certificate authenticates the agent, and are used for display and
eligibility only. A heartbeat field was rejected: `HeartbeatMsg` is
`extra="forbid"`, so a newer agent talking to an older control plane would
have every heartbeat dropped.

**`release_signature` is optional in the schema and required by the agent
that pins a release key.** It is a base64 Ed25519 signature by the offline
release key over `its-agent-release/v1\n<version>\n<os>\n<arch>\n<sha256>\n`,
so a signed binary cannot be relabelled as another version or platform. The
control plane passes it through and cannot produce one.

The version comparator is pinned on both sides by
`testdata/semver-order.json`.
**Status.** Built on both sides for Windows (phase 1): the agent capability
is `internal/capabilities/remote`, registered only on Windows, and its
`schema_test.go` holds `Params`/`Result` to these schemas; the browser codec is
`frontend/src/modules/remote/rsp.ts`, tested against the same vectors.
`agent/internal/rsp` holds the agent's codec. Two never-packaged doubles run
against the real gateway in the backend's tests: `internal/rsp/fakeagent`
(scripted frames) and `internal/capabilities/remote/relayagent` (the real
capability behind the real signed-job runner, with a synthetic desktop).
Phase 1 added three vectors (`cursor_shape`, `monitor_select`, `quality`) and
the HELLO member `selected`; both are additive.

## Patch restarts: `result_version` and `patch.reboot` (`docs/modules/patch/spec.md` §8.1)

`PatchScanDocument` and `PatchApplyResult` are `extra="forbid"`, so a
member an agent adds would fail the whole document on an older control plane.
**The control plane therefore announces what it reads.** `PatchScanParams` and
`PatchApplyParams` gain `result_version` (default `2`):

- **New control plane, old agent.** The old agent ignores `result_version`
  and `reboot_not_after` (Go drops unknown members), sends the v1 documents,
  and never restarts. Every new server-side member defaults to `None`.
- **New agent, old control plane.** No `result_version` means 1: the agent
  emits exactly the v1 members and does not restart, because an older control
  plane cannot see `reboot_scheduled_for` and would read a restart as a lost
  device.

Version 2 adds `PatchScanDocument.reboot_required` / `booted_at` (both
nullable: macOS cannot tell a pending restart outside an install),
`PatchApplyResult.reboot_scheduled_for` / `user_present`, and
`PatchApplyParams.reboot_not_after` (the end of the maintenance window the
apply was dispatched in), and `PatchScanDocument.app_sources` with app updates
(`source` `winget` / `homebrew`) in `patches`. Under v1 the agent drops app
entries and `app_sources`, so an older control plane catalogues none.
`schema_test.go` pins the v1 structs to a subset.

`patch.reboot` is a new JOB TYPE (`PatchRebootParams` / `PatchRebootResult`),
so an agent without it refuses `unknown_type` and the control plane marks that
restart `failed` with "update the agent". `only_if_required` defaults to
true because its absence must never widen what the job does.

## Reject: `agent.uninstall` (`docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md` §1.5, §4)

A new job TYPE, not a new wire `kind` -- the `job.cancel` decision again. An
agent older than 1.6.0 refuses it `unknown_type`, loudly and before anything
runs; the control plane never sends it one in the first place, because
`app/services/device_review.py` creates the job only for an agent whose
`agents.capabilities` carries `agent.uninstall`, and revokes an older agent's
certificate instead.

**Params carry nothing** (`AgentUninstallParams`, `extra="forbid"`): what to
remove is the agent's own knowledge -- its service, its binary, its data
directory -- so no field exists that could point it at a path.

**`succeeded` means scheduled, not done** (`AgentUninstallResult.scheduled`).
The capability starts a detached helper that waits 15 seconds (so this result
reaches the gateway), then stops and deletes the service and removes the
binary and the data directory. The control plane reads the `succeeded` job as
the agent's confirmation and revokes the certificate within a minute; a result
lost with the data directory is covered by the 24-hour completion.
