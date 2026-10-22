# 05 — Agent Specification

The agent is a **single lightweight Go binary** running as a service on every
managed Windows, Linux, and macOS endpoint. It is the one piece of software this
platform installs everywhere, so two properties dominate its design: it must be
**near-invisible when idle**, and it must **never execute anything the server did
not cryptographically authorize**.

The agent lives in **this repository, under `agent/`**, and is **not** shared with
the monitoring platform.

> **Decision reversed, 2026-08-05** (`CLAUDE.md`, commit `a32cead`). The agent
> previously lived in a separate `it-suite-agent` repository, and that separation
> was load-bearing in two ways that a directory move does not carry with it:
>
> 1. It made the wire protocol **visibly** a contract between two systems. It
>    still is one. A fleet is always mixed-version — a deployed agent is older
>    than the control plane it talks to and updates on its own schedule — so
>    every change to the envelopes below and to
>    `docs/kernel/wire-protocol/` is a compatibility decision: additive, or
>    versioned, or it breaks agents in the field. One repo makes it possible to
>    change both sides in a single commit; it does not make that safe, and no
>    test in this repository will show you the breakage.
> 2. It explained why nothing here measured the agent. That explanation has
>    expired. See the idle-budget accountability note at the end of the
>    capability section.
>
> Nothing about the agent became built by moving. At the time of writing there is
> no `agent/` directory and no Go code anywhere in this repository.

## Design principles

1. **Outbound-only, event-driven, asleep by default.** One long-lived mTLS
   connection, parked on a blocking read. No polling. Nothing runs until a signed
   job arrives or a local schedule fires.
2. **Verify everything.** Every job is checked for a valid server signature,
   correct target identity, freshness, and non-replay before it runs.
3. **Lazy and lean.** Heavy capabilities are dormant code paths that allocate and
   initialize only when invoked, then release.
4. **Single binary, native service.** No runtime to install. Windows service,
   systemd unit, launchd daemon.
5. **Resilient offline.** Scheduled work runs without the server; results and
   logs buffer (bounded) and flush on reconnect.

## Idle footprint budget (hard requirement)

The agent must not be something an admin notices on a busy server or a user's
laptop. Target and rules:

- **Resident memory:** low tens of MB at idle.
- **Idle CPU:** ~0%.
- **Mechanism:** a single goroutine blocked on the connection read; a timer wheel
  for scheduled tasks; **no busy loops anywhere**.
- **Capabilities are lazy:** patch, backup, remote, provisioning, and deployment
  code initialize only on an inbound job for them, and free their resources when
  the job completes. Nothing capability-specific is "always on" unless a brief
  explicitly justifies it (e.g. security-log collection uses OS event
  subscriptions, which are event-driven, not a poll).
- **Regression = defect.** Idle footprint is tracked; a change that inflates it is
  treated as a bug, not a tuning matter.

## Process & service model

- Runs as **SYSTEM (Windows) / root (Linux, macOS)** because management
  capabilities require it. Capabilities that don't need elevation run with the
  least privilege they can.
- **Watchdog / supervision:** the OS service manager restarts the agent on crash;
  the agent optionally supervises a low-privilege helper for user-session tasks
  (e.g. showing a remote-session consent prompt in the logged-in user's context).
- **Single instance** per host, identified by its enrolled certificate.

## Enrollment & identity

Zero-touch friendly, so it can be pushed by GPO, MDM, kickstart, or an installer
parameter.

1. The installer provides a **per-org enrollment token** (time-bounded).
2. On first start the agent generates a keypair (private key never leaves the
   host), builds a CSR, and submits it with the enrollment token to the
   enrollment endpoint.
3. The internal CA (Vault PKI / step-ca) issues a **client certificate** bound to
   a device identity and `org_id`. The agent pins the **server certificate** and
   the **job-signing public key**.
4. The agent registers device facts (hostname, OS, hardware summary).
5. Thereafter the agent authenticates by mTLS client certificate; certificates
   are **auto-rotated** (365 days, replaced at roughly two-thirds of life);
   revocation at the gateway disconnects a retired or compromised agent.

**"Auto-rotated" is now true, and it says two-thirds of life rather than
"short-lived" for a reason.** This line claimed automatic rotation for an entire
phase while nothing implemented it, and every agent deployed stopped connecting
on day 30 (`docs/modules/agent/open-questions.md` OQ-1). It is implemented by
`docs/modules/agent/cert-renewal.md`: the control plane dispatches
`agent.cert.renew` and `agent.cert.install` as **ordinary signed jobs** — no new
authenticated surface, no second server→agent instruction path — starting when
less than a third of the certificate's life remains and retrying on every
connect.

It is deliberately NOT rotation on expiry. The certificate is the credential the
push would travel over: once it has expired the handshake fails, the gateway
closes 4401 before any application protocol is read, and there is no channel
left to deliver a replacement. An endpoint offline for the whole remaining
window is **re-deployed**; that is the accepted fallback, and it is the only one
that exists.

The private key is generated on the endpoint at renewal exactly as at step 2 and
never leaves it. Only the CSR travels.

**Correction — enrollment does NOT return a policy, and there is no policy
channel.** Step 4 previously read "and receives its resolved **policy**
(schedules for its device group)". `EnrollResponse`
(`app/api/v1/enrollment.py`) carries exactly six fields — `device_id`,
`cert_pem`, `chain_pem`, `ca_bundle_pem`, `gateway_url`,
`job_signing_public_key_pem` — and none of them is a policy. Nor is there a
server→agent message that could carry one: `app/gateway/protocol.py` defines a
single server→agent kind, `JobEnvelope`.

That is not an omission, it is the invariant. **Policy reaches an agent as a
signed job and by no other route.** A policy delivered in the enrollment
response, or down a second unsigned channel, would be an instruction path into a
root/SYSTEM process that the signed-job runner never verifies — invariant #1,
straightforwardly. Endpoint health is the worked example: its threshold policy
travels as `health.policy.apply`, a registered job type whose params model is
`HealthPolicyApplyParams`, and the agent verifies signature, target identity,
freshness and non-replay on it exactly as on any other job before installing
anything. Anything else a device "receives on enrollment" must arrive the same
way. See `docs/modules/endpoint-health-alerting/spec.md` §0.4 and §5.2.

## Transport & wire protocol

- **Transport:** TLS 1.3, **mutual TLS**, WebSocket, **outbound only**. The agent
  dials the gateway and holds the connection; the server pushes down it.
- **Encoding (v1):** a **versioned JSON envelope**, chosen for debuggability at
  the low volume of control messages. Protobuf/msgpack is a later optimization if
  profiling justifies it; the envelope carries a `v` field so the encoding can
  evolve.
- **Envelope shape (illustrative):**

  ```jsonc
  // server → agent: a job
  {
    "v": 1,
    "kind": "job",
    "job": {
      "id": "…", "type": "inventory.collect",
      "device": "<device-id>",           // must match this agent's identity
      "params": { … },
      "issued_at": "…", "expires_at": "…", "nonce": "…"
    },
    "sig": "<server signature over the canonical job>"
  }

  // agent → server: status / result / logs
  { "v": 1, "kind": "job_status", "job_id": "…", "state": "running" }
  { "v": 1, "kind": "job_log",    "job_id": "…", "level": "info", "msg": "…" }
  { "v": 1, "kind": "job_result", "job_id": "…", "state": "succeeded",
    "result": { … }, "artifact_ref": "s3://…" }

  // agent → server: threshold edges and periodic summaries (health.check)
  { "v": 1, "kind": "health_report", "policy_version": 7,
    "reports": [
      { "check_id": "…", "selector": "C:", "kind": "breach",
        "state": "breaching", "value": "3.1", "observed_at": "…" }
    ] }

  // liveness
  { "v": 1, "kind": "heartbeat", "ts": "…" }
  ```

- **Heartbeats** are lightweight and infrequent, enough to detect dead
  connections; reconnection uses exponential backoff with jitter.

### The `health_report` wire kind

`HealthReportMsg` in `app/gateway/protocol.py`; full contract in
`docs/modules/endpoint-health-alerting/spec.md` §5.1. Four properties are
load-bearing and each closes a specific failure:

- **It carries no device identity.** The binding comes from the mTLS
  connection; `handle_agent_message` resolves `device_id`/`org_id` from it and
  audits a mismatch as `gateway.identity_mismatch`. A `device` field here would
  be a second, forgeable source of the same fact.
- **It carries no sample series.** Each report line is six scalars —
  `check_id`, `selector`, `kind` (`breach`/`recovery`/`summary`), `state`
  (`ok`/`breaching`/`unavailable`), `value`, `observed_at` — and there is no
  array, window or rolling average anywhere in it. A series on this wire is a
  series a consumer can retain, which is the monitoring boundary this platform
  keeps. `value` is a **string** for the same reason: typing it numerically is
  the first step toward someone aggregating it over time.
- **`unavailable` is a real third state**, distinct from both `ok` and
  `breaching`. A check the agent could not answer is not a healthy check; the
  server neither opens nor clears an alert on it.
- **It is at-least-once and is not deduplicated on the wire.** Re-delivery after
  a reconnect is expected — a re-delivered health edge is not a distinct fact
  the way a security event is, so there is no `event_uid`. The server dedups by
  a partial unique index on the alert key instead. `reports` is bounded at 64
  (a profile is capped at 32 checks, so a full summary plus edges fits in one
  message); an agent with more to say sends more messages.

`policy_version` on the envelope is the policy the agent believes it is
running. It is how the server detects drift and how it discards a breach
reported against a policy more than one version stale.

## Signed-job runner (the core safety mechanism)

For **every** inbound job, before any capability runs, the agent:

1. Verifies the **server signature** against the pinned job-signing public key.
2. Confirms the **target device identity** matches this agent's enrolled
   identity.
3. Checks **freshness** (`issued_at`/`expires_at`) and rejects **replays**
   (unseen `nonce`).
4. Confirms the job **type** maps to a known capability handler.

If any check fails, the job is rejected, logged locally, and reported back as
refused. **There is no code path that executes an unsigned or unverified job** —
not for development, not for internal tooling. This is the invariant from
`docs/03-security-model.md`.

### Job execution: concurrent across capabilities, serial within one

The four checks above happen inline, on the transport's single read goroutine,
in wire order — that part is unchanged. What no longer happens on that
goroutine is the capability itself. The runner submits the verified job to a
`Dispatcher` (`agent/internal/jobs/dispatcher.go`) and returns immediately.
The dispatcher runs one worker goroutine per capability key (everything before
the first `.` in the job type, so `backup.run` and `backup.restore` share a
key and never overlap, while `inventory.collect` and `patch.apply` do not
share one and run concurrently). A worker is spawned on a capability's first
job and exits when its queue drains, so an idle agent runs zero dispatcher
goroutines. Declared-heavy capabilities (`backup`, `patch`, `app`, `script`)
share a budget of 2 concurrent execution slots; light capabilities never touch
it. Each capability's inbound queue is bounded at depth 8; a full queue
refuses the job through the existing refusal path, with a named reason, rather
than blocking the reader or growing without bound.

**Why this changed.** The runner used to block on the read goroutine for the
whole capability, deliberately, so job ordering was the wire's ordering. The
backup module's dedup exchange (`docs/modules/backup/spec.md` §5.2) needs a
reply *while a job is running*: `backup.run` blocks on a chunk grant that only
the read goroutine can deliver, and the read goroutine was the one the job was
occupying. See `docs/modules/backup/spec.md` §6.1 item 5 and `HANDOFF.md` §2
for the measured failure and the fix.

**Verified, not just built.** A dedicated harness
(`agent/internal/capabilities/concurrency_harness_test.go`) composes the real
read loop, the runner, the dispatcher and a real capability over a fake
gateway — the seam every prior test of the dedup exchange bypassed by
answering its query in-process. Against that harness: the deadlock reproduces
against the pre-fix code (a genuine ~5s timeout) and is gone after (0.02–0.05s,
measured on this run, on Linux, against the fake gateway); no job is lost
across a worker restart; a job survives the connection being dropped under it
and answers after the reconnect; no worker goroutine survives an idle queue;
the heavy budget holds while light capabilities are exempt; and the
replay-defence tests no longer depend on goroutine scheduling to pass.

**These tests are gated in CI.** They are ordinary Go tests under
`agent/internal/`, and `.github/workflows/ci.yml`'s `agent` job runs
`go test ./... -count=1` on every push. **What is not gated is `-race`**: the
CI test step omits it deliberately, because the race detector's shadow memory
would corrupt the RSS thresholds the idle-budget step in the same job measures.

**One caveat, verified on the real run rather than assumed.** The `agent` job's
steps run in order `gofmt` → `vet` → `staticcheck` → `test` → idle budget →
cross-compile, and `staticcheck` currently FAILS on ten pre-existing findings
(unused functions and constants in `capabilities/{backup,logs,patch}`, two
`ST1018` bidi-character literals in a deploy test that put them there on
purpose). GitHub Actions then skips every later step, so the test step has not
actually executed on this branch — run 31333534459, `agent` job: staticcheck
`failure`, test `skipped`. The gate is configured and correct; it is blocked by
an unrelated red step above it. Unblocking it is its own piece of work.

The result is verified on Linux, against a fake gateway — it says nothing about a real
gateway under real load, which is the operator's deploy-validation step to
confirm, not something this suite can claim. Queue depth (8) and the heavy
budget (2) are reasoned defaults, not measured ones.

**Two gaps found and deliberately not fixed this cycle:**

- If a heavy capability's job is still queued waiting for a budget slot when
  the **process** context is cancelled, the dispatcher logs a warning and
  drops it — no job result is sent, so the control plane observes only the
  job's own expiry, not a distinguishable cause. It used to fire on a mere
  disconnect, which made it an outage rather than a shutdown artefact; the
  dispatcher now runs jobs under a process-scoped `BaseContext`, so this path
  is reachable only when the agent is stopping and a result would have nowhere
  to go anyway.
- Panic recovery now has two independent layers with different outcomes: the
  inner one (around the capability call itself) reports a `failed` job
  result, the normal path for a capability that panics. The outer one (around
  the whole worker call) exists only to stop a panic outside that inner layer
  from crashing the process — it produces no job result at all, only a local
  log line, so a job caught there sits `running` until the control plane
  expires it: a silent hang, not a crash, and harder for an operator to
  notice than either alone would be.

## Local scheduler

A small cron-like scheduler runs recurring tasks defined by the device's resolved
policy (inventory sweeps, log shipping, backup windows). This means:

- Recurring work does not require the server to fire each tick.
- Scheduled work continues during brief disconnection; results buffer and flush
  on reconnect.
- Schedules are timer-driven (no polling); the scheduler wakes, runs the task via
  the matching capability, sleeps again.

Scheduled tasks that mutate the system are still bound by policy — and that
policy itself arrived as a **signed job**, not with the enrollment response (see
the correction above); ad-hoc privileged actions always arrive as signed jobs
too.

## Capability model

Capabilities are **compiled-in Go packages** gated by policy/licence — **not**
dynamically loaded plugins (`.so`/`.dll`), which would add weight, complexity,
and attack surface across three OSes and undermine the single-binary goal.
"Modular" here means an internal capability **registry**: each capability
registers the job types it handles; only enabled capabilities are active for a
given device.

Capabilities present at v1 (each detailed in its module brief):

| Capability | Job types (illustrative) | Idle behavior |
|---|---|---|
| Inventory | `inventory.collect` | Scheduled sweep; otherwise dormant |
| Security logs | `logs.ship` | **Event-driven** OS log subscription; dormant otherwise |
| Patch | `patch.scan`, `patch.apply` | Dormant until a job runs |
| Remote control | `remote.session.open` | Dormant until a session is requested |
| App deployment | `app.deploy` | Dormant until a job runs |
| Provisioning | `provision.prepare`, `provision.enroll` | Dormant; heavy work on the site server |
| Backup | `backup.run`, `backup.restore` | Dormant until a window/job; releases after |
| Endpoint health | `health.policy.apply`, `health.check.now` | **One entry on the existing timer wheel** per signed policy; dormant between ticks; emits `breach`/`recovery`/`summary` only |
| Job cancellation | `job.cancel` | Two bounded structures and one mutex on the runner; **no goroutine and no timer** |

Adding a capability = a new package in `agent/internal/capabilities/`, registering its
job types, plus the corresponding server-side job type and UI. It does not touch
the transport, enrollment, or signed-job runner.

**`job.cancel` IS AN ORDINARY SIGNED JOB, and that is the whole of its security
argument** (`docs/superpowers/specs/2026-08-22-job-lifecycle-design.md` §3.1,
§3.8). A mechanism that makes an endpoint abandon work is a command to that
endpoint, so invariant #1 governs it with no exemption: an unsigned one would
be a fleet-wide denial-of-service primitive, one frame per running job. It is
the one capability whose subject is another job, and it still reaches
`internal/capabilities/jobcancel` only through the runner, only as a
`jobs.VerifiedJob`, after the same five checks.

It is also the reason the type is named `job.cancel` rather than
`backup.cancel`: `capabilityKey` splits on the first `.`, so a cancel gets the
`"job"` queue -- which is deliberately **not** in `jobs.DefaultHeavyKeys`, so a
cancel never waits on the heavy semaphore behind the very job it is cancelling.

Its idle cost is **measured, not asserted**
(`internal/jobs/interrupt_test.go::TestFalsifier_CancellationStateStartsNoGoroutineAndDoesNotGrow`).
The falsifiers are the design's own: any goroutine growth across a cancel
cycle, or retention that scales with jobs seen. Observed at 20,000 cycles:
goroutine delta 0, and retention bounded by the 256-entry cancelled set rather
than by the number of cancels -- 12.6 KB at 200 cycles against 36.3 KB at
20,000, where an unbounded map would be megabytes.

### The `health.check` capability contract

Server-side spec: `docs/modules/endpoint-health-alerting/spec.md` §4, §5.
**The agent-side implementation is unbuilt.** It belongs under `agent/` in this
repository and nothing of it has been written — see the accountability note at
the end of this section.

- **It is configured only by a signed job.** `health.policy.apply` carries
  `policy_version`, the three intervals (`tick_interval_s`,
  `posture_interval_s`, `summary_interval_s`) and up to 32 check specs, each
  `{check_id, check_type, selector, comparator, threshold (string), for_ticks,
  cadence}`. **An empty `checks` list is a legal policy and means "run
  nothing"** — that is how a device whose profile was unassigned is *disarmed*.
  A disarm must be an explicit instruction, not the absence of one.
- **`health.check.now` takes no parameters.** Dispatching it means "sample every
  check now and emit a full summary" — exactly the shape of `logs.ship`. A
  parameter here would be a second, unversioned way to configure the tick.
- **The agent holds a fixed-size struct per check and no sample history at
  all**: a consecutive-tick counter, a state byte, the last value. No ring
  buffer, no rolling average, no "last 10 values".
- **The agent emits edges, not state.** A `breach` line is sent when a check has
  compared true for `for_ticks` consecutive ticks; a `recovery` line when it
  stops; a `summary` at `summary_interval_s` regardless. **A tick with no
  transition and no summary due produces no network write.** The whole alert
  lifecycle — dedup, flap suppression, clearing, actions — is the server's, and
  the agent owns none of it.
- **`agent.offline` is never pushed to an agent.** An agent that is offline
  cannot report that it is offline; it is the one server-evaluated check, and
  the server filters it out of every policy push. Checks unavailable on the
  device's OS are filtered out server-side too.
- **The `eventlog.*` check types reuse the security-logs capability's existing
  OS event subscription and open none of their own.** If security-logs is not
  enabled for a device, every `eventlog.*` check reports `unavailable`. Two
  subscriptions to the same log for two modules is exactly the always-on
  collector the idle budget forbids.
- **Remediation is not this capability's business.** An alert-triggered fix
  arrives as an ordinary signed job for whatever capability owns it
  (`script.run`, `inventory.collect`, …), through the unmodified signed-job
  runner. `health.check` never executes anything.

**Idle-budget accountability, stated plainly — and this paragraph has now been
rewritten twice, in opposite directions.**

*What it said until 2026-08-05:* the falsifiers are unmeasured because the agent
lives in a repository this CI cannot see.

*What it said from 2026-08-05:* "**Nothing in this repository checks any of
that** — `.github/workflows/ci.yml` runs `backend`, `deploy` and `frontend`, and
has no Go toolchain and no benchmark of any kind." The move removed the *excuse*
and not the *gap*, and the gap was real.

*What is true from 2026-08-06:* CI has an `agent` job with a Go toolchain, and
the falsifiers are measured. Per capability:

| Capability | Falsifiers | Where |
|---|---|---|
| the timer wheel | zero allocations per wakeup; no goroutine per entry; an idle wheel never wakes | `agent/internal/budget/budget_test.go` |
| `health.check` | `docs/modules/endpoint-health-alerting/spec.md` §0.5's **six**, one test each, named for it | `agent/internal/capabilities/health/budget_test.go` |
| `site.wake` | `docs/modules/site-server/spec.md` §5.4's **two** | `agent/internal/capabilities/site/budget_test.go` |

**The mechanism matters as much as the coverage.** None of those is a
`Benchmark` function. `go test -bench` **exits 0 on a regression** — measured on
this project rather than assumed: a planted 64 KiB/op regression printed
`65536 B/op` and the build stayed green. Every number is therefore produced by
`testing.Benchmark` called from an ordinary `Test` that then **compares** against
a threshold, so a regression turns CI red. The `agent` job additionally prints
each measurement by name, in bytes, in its own step — a budget nobody can read
the value of drifts back into being a target.

**What is still a design target, deliberately: the wall-clock CPU percentage.**
`< 0.1% of one core` is not asserted, and that is a refusal rather than an
oversight. A shared runner's variance exceeds the signal, so a threshold loose
enough not to flake is loose enough to pass a real regression, and a flaky
budget test gets skipped — which reads as coverage while measuring nothing. It
is approached instead through machine-independent proxies: allocation counts,
goroutine counts, network writes on a quiet tick, file descriptors, and retained
heap across a hundredfold spread. Describe that number as a target.

## Self-update

**Decided 2026-09-23. The full design is
[`modules/agent-distribution/spec-self-update.md`](modules/agent-distribution/spec-self-update.md).
It is not built yet.** It replaces the earlier sequence here, which required a
code signature before any swap and was therefore refused as OQ-3.

1. A human starts a **rollout** on the server. It moves in rings: a pilot ring
   of at most 25 devices, then later rings, each advanced by a human. Within a
   ring the server dispatches in batches, and a failure threshold pauses the
   rollout.
2. Each device receives a **signed `agent.update` job**. The job names the
   target version, the artifact for that device's platform, its SHA-256 and
   size, and a presigned HTTPS download reference. The agent checks the params,
   the platform, the version ordering (no downgrade unless the job says
   `rollback`), the disk space, and that no other job is running. It does all
   of this **before fetching anything**.
3. The agent downloads the artifact and checks the size and the SHA-256. On
   Windows, once Authenticode exists, it also verifies the signature against a
   publisher pin compiled into the running binary, never taken from the job.
4. A short-lived helper process, running the **previous** binary, puts the new
   file at the same path by an atomic rename, restarts the service, and keeps
   the previous binary.
5. **Rollback is local.** If the new binary does not reach the gateway and hold
   the connection within the health window, or if it crash-loops, the old
   binary is restored and restarted. The job's result is reported by whichever
   binary is running at the end.

Every update is a signed job, verified like any other (invariant #1). The
helper opens no port and takes no network input (invariant #2).

## Offline behavior & buffering

- Scheduled tasks run offline; **results, job logs, and shipped security logs
  buffer locally within a bounded size** and flush in order on reconnect.
- On reconnect the agent re-authenticates by certificate, reports current state,
  and drains its buffer.
- Bounded buffers prevent unbounded disk growth on a long-disconnected endpoint;
  oldest non-critical data is dropped first per policy.

## Per-OS packaging & installation

| OS | Service | Packaging | Notes |
|---|---|---|---|
| Windows | Windows Service (SCM) | Signed `.exe` + MSI; GPO/Intune deployable; enrollment token as install param | Runs as SYSTEM; user-session helper for consent UI |
| Linux | systemd unit | `.deb`/`.rpm` + static binary; kickstart/cloud-init friendly | Runs as root; LVM snapshot access for backup |
| macOS | launchd daemon | Signed/notarized `.pkg`; MDM-deployable | Runs as root; APFS snapshots; MDM required for OS-update enforcement on Apple Silicon |

All binaries are **code-signed** (and notarized on macOS). Installers accept the
enrollment token and control-plane address as parameters for zero-touch rollout.

**Implementation status, and a distinction this table elides.** The target above
is not met yet, and "signed" on macOS means three separable things that must not
be traded for one another:

1. **Ad-hoc signature** — enforced by the *kernel* at `exec` on Apple Silicon,
   which refuses to start an arm64 Mach-O carrying no signature at all. No
   Gatekeeper setting, quarantine state, or MDM profile can waive it. **Built,
   and asserted in CI**: `CGO_ENABLED=0` selects the Go internal linker, which
   embeds the signature itself when cross-compiling from Linux, and
   `agent/packaging/machosign_test.go` verifies the artifact page hash by page
   hash.
2. **Developer ID + notarization** — enforced by *Gatekeeper*, on first launch
   of a *quarantined* artifact. **Not built.** An MDM-delivered payload is not
   quarantined, so this gates distribution outside managed devices —
   commercialization — rather than the internal rollout. Open as
   [OQ-3](modules/agent/open-questions.md).
3. **TCC / PPPC** — permissions, delivered by MDM profile. A PPPC profile can
   pin an ad-hoc-signed binary only by *path*; a Developer ID signature lets it
   pin to a *team identifier*, which survives a path change and does not extend
   to a substitute binary. A reason to want (2) eventually, not a blocker now.

Windows is genuinely unsigned. Ad-hoc means signed by *nobody*, so (1) attests
that the bytes are unchanged since linking and says nothing about who produced
them. An attacker's binary is equally ad-hoc-signable.
[Self-update](#self-update) used to wait for (2) or a Windows certificate. Since
2026-09-23 it does not: it is a signed job pinned to the release SHA-256, the
same trust `app.deploy` and `script.run` already extend to the control plane. A
certificate-backed signature, once it exists, is **added** as a check against a
compiled-in pin (`spec-self-update.md` §10). It is not a precondition.

## What the agent explicitly does NOT do

- It does not listen for inbound connections or expose any management port.
- It does not execute unsigned instructions from any source.
- It does not hold plaintext credentials at rest; credentials arrive only for the
  duration of a job/session and are discarded after use.
- It does not run heavy telemetry collection — deep metric/time-series monitoring
  is the separate monitoring product's job.
